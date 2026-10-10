import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { gzipSync } from 'node:zlib';
import { TileLoader } from '../tile-loader.mjs';

const bounds = { min: [0, 0, 0], max: [10, 10, 10] };

async function startServer(t, handler) {
  const server = createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => {
    server.closeAllConnections();
    server.close();
  });
  return `http://127.0.0.1:${server.address().port}`;
}

async function waitFor(loader, predicate) {
  const deadline = Date.now() + 3000;
  while (!predicate(loader.stats)) {
    if (Date.now() > deadline) throw new Error(`Loader did not settle: ${JSON.stringify(loader.stats)}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

const compressedPoints = (count) => gzipSync(new Float32Array(Array.from({ length: count }, () => [1, 2, 3, 4]).flat()));
const descriptor = (base, spatialId, count, suffix) => ({
  key: `${spatialId}-${suffix}`, spatialId, point_count: count,
  bounds, url: `${base}/${count}/${suffix}.bin.gz`,
});

test('real gzip loads stay within budget and replaced resources are released', async (t) => {
  const base = await startServer(t, (req, res) => {
    res.end(compressedPoints(Number(req.url.split('/')[1])));
  });
  const resources = new Set();
  const snapshots = [];
  const loader = new TileLoader({
    create: (points) => {
      resources.add(points);
      return points;
    },
    release: (points) => resources.delete(points),
    onChange: (stats) => snapshots.push(stats),
  });
  t.after(() => loader.dispose());
  loader.setSelection([descriptor(base, 'a', 2, 'coarse'), descriptor(base, 'b', 2, 'coarse')], 4);
  await waitFor(loader, (stats) => stats.loadedPoints === 4 && stats.loadingTiles === 0);
  assert.equal(resources.size, 2);
  loader.setSelection([descriptor(base, 'a', 4, 'fine')], 4);
  assert.equal(resources.size, 0);
  await waitFor(loader, (stats) => stats.loadedPoints === 4 && stats.loadingTiles === 0);
  assert.equal(resources.size, 1);
  assert.ok(snapshots.every((stats) => stats.loadedPoints + stats.loadingPoints <= 4));
  assert.ok(snapshots.every((stats) => stats.selectedPoints <= 4));
  loader.dispose();
  assert.equal(resources.size, 0);
});

test('moving away cancels the HTTP request and prevents stale point creation', async (t) => {
  let requestStarted;
  const started = new Promise((resolve) => { requestStarted = resolve; });
  let requestAborted;
  const aborted = new Promise((resolve) => { requestAborted = resolve; });
  const base = await startServer(t, (req, res) => {
    if (req.url.includes('slow')) {
      requestStarted();
      const timer = setTimeout(() => res.end(compressedPoints(2)), 1000);
      res.on('close', () => {
        clearTimeout(timer);
        if (!res.writableEnded) requestAborted();
      });
    } else {
      res.end(compressedPoints(2));
    }
  });
  const createdKeys = [];
  const loader = new TileLoader({
    concurrency: 1,
    create: (points, tile) => { createdKeys.push(tile.key); return points; },
    release: () => {},
  });
  t.after(() => loader.dispose());
  loader.setSelection([descriptor(base, 'a', 2, 'slow')], 2);
  await started;
  loader.setSelection([descriptor(base, 'b', 2, 'fast')], 2);
  await waitFor(loader, (stats) => stats.loadedPoints === 2);
  await Promise.race([aborted, new Promise((_, reject) => setTimeout(() => reject(new Error('HTTP request was not aborted')), 1000))]);
  assert.deepEqual(createdKeys, ['b-fast']);
});

test('a failed tile is reported and may be retried without exceeding the budget', async (t) => {
  let attempts = 0;
  const base = await startServer(t, (_req, res) => {
    if (++attempts === 1) {
      res.statusCode = 503;
      res.end();
    } else {
      res.end(compressedPoints(2));
    }
  });
  const loader = new TileLoader({ create: (points) => points, release: () => {} });
  t.after(() => loader.dispose());
  loader.setSelection([descriptor(base, 'a', 2, 'retry')], 2);
  await waitFor(loader, (stats) => stats.failedTiles === 1);
  assert.equal(loader.stats.loadedPoints, 0);
  loader.retry();
  await waitFor(loader, (stats) => stats.loadedPoints === 2);
  assert.equal(loader.stats.failedTiles, 0);
});

test('an oversized plan or duplicate cell is rejected before creating resources', () => {
  const loader = new TileLoader({ create: (points) => points, release: () => {} });
  const base = 'http://127.0.0.1:1';
  assert.throws(() => loader.setSelection([descriptor(base, 'a', 5, 'coarse')], 4), /上限/);
  assert.throws(() => loader.setSelection([descriptor(base, 'a', 2, 'coarse'), descriptor(base, 'a', 2, 'fine')], 4), /重复/);
  assert.equal(loader.stats.selectedPoints, 0);
  loader.dispose();
});
