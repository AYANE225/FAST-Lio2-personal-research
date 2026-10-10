import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { gzipSync } from 'node:zlib';
import { TileLoader } from '../tile-loader.mjs';
import { fetchPointTile } from '../point-cloud-data.mjs';

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
      res.statusCode = 404;
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
  assert.equal(attempts, 1);
  loader.retry();
  await waitFor(loader, (stats) => stats.loadedPoints === 2);
  assert.equal(loader.stats.failedTiles, 0);
});

test('temporary HTTP 503 is retried before reporting a tile failure', async (t) => {
  let attempts = 0;
  const base = await startServer(t, (_req, res) => {
    if (++attempts === 1) {
      res.writeHead(503);
      res.end();
    } else {
      res.end(compressedPoints(2));
    }
  });
  const errors = [];
  const snapshots = [];
  const loader = new TileLoader({
    create: (points) => points, release: () => {},
    onError: (error) => errors.push(error),
    onChange: (stats) => snapshots.push(stats),
  });
  t.after(() => loader.dispose());
  loader.setSelection([descriptor(base, 'a', 2, 'temporary')], 2);
  await waitFor(loader, (stats) => stats.loadedPoints === 2 || stats.failedTiles > 0);
  assert.equal(loader.stats.loadedPoints, 2);
  assert.equal(attempts, 2);
  assert.deepEqual(errors, []);
  assert.ok(snapshots.every((stats) => stats.loadedPoints + stats.loadingPoints <= 2));
});

test('persistent temporary HTTP failures stop after three requests', async (t) => {
  let attempts = 0;
  const base = await startServer(t, (_req, res) => {
    attempts += 1;
    res.writeHead(503);
    res.end();
  });
  await assert.rejects(fetchPointTile(descriptor(base, 'a', 2, 'unavailable')), /503/);
  assert.equal(attempts, 3);
});

test('Retry-After delays a repeated rate-limited request', async (t) => {
  const requestTimes = [];
  const base = await startServer(t, (_req, res) => {
    requestTimes.push(performance.now());
    if (requestTimes.length === 1) {
      res.writeHead(429, { 'Retry-After': '1' });
      res.end();
    } else {
      res.end(compressedPoints(2));
    }
  });
  const points = await fetchPointTile(descriptor(base, 'a', 2, 'rate-limited'));
  assert.equal(points.length, 8);
  assert.equal(requestTimes.length, 2);
  assert.ok(requestTimes[1] - requestTimes[0] >= 990);
});

test('a long Retry-After is reported instead of issuing an early retry', async (t) => {
  let attempts = 0;
  const base = await startServer(t, (_req, res) => {
    attempts += 1;
    res.writeHead(503, { 'Retry-After': '120' });
    res.end();
  });
  await assert.rejects(fetchPointTile(descriptor(base, 'a', 2, 'long-delay')), /503/);
  assert.equal(attempts, 1);
});

test('HTTP dates in Retry-After are honoured', async (t) => {
  let attempts = 0;
  let retryTimeMs;
  let nextRequestMs;
  const base = await startServer(t, (_req, res) => {
    if (++attempts === 1) {
      const date = new Date(Date.now() + 2000).toUTCString();
      retryTimeMs = Date.parse(date);
      res.writeHead(503, { 'Retry-After': date });
      res.end();
    } else {
      nextRequestMs = Date.now();
      res.end(compressedPoints(2));
    }
  });
  const points = await fetchPointTile(descriptor(base, 'a', 2, 'date-delay'));
  assert.equal(points.length, 8);
  assert.equal(attempts, 2);
  assert.ok(nextRequestMs >= retryTimeMs);
});

test('moving away cancels a pending retry and frees its request slot', async (t) => {
  let responded;
  const firstResponse = new Promise((resolve) => { responded = resolve; });
  let delayedRequests = 0;
  const base = await startServer(t, (req, res) => {
    if (req.url.includes('delayed')) {
      delayedRequests += 1;
      res.writeHead(503, { 'Retry-After': '30' });
      res.end();
      responded();
    } else {
      res.end(compressedPoints(2));
    }
  });
  const created = [];
  const errors = [];
  const loader = new TileLoader({
    concurrency: 1,
    create: (points, tile) => { created.push(tile.key); return points; },
    release: () => {}, onError: (error) => errors.push(error),
  });
  t.after(() => loader.dispose());
  loader.setSelection([descriptor(base, 'a', 2, 'delayed')], 2);
  await firstResponse;
  // Let the first response reach fetch() before switching the visible selection.
  await new Promise((resolve) => setTimeout(resolve, 50));
  loader.setSelection([descriptor(base, 'b', 2, 'fast')], 2);
  await waitFor(loader, (stats) => stats.loadedPoints === 2);
  assert.equal(delayedRequests, 1);
  assert.deepEqual(created, ['b-fast']);
  assert.deepEqual(errors, []);
});

test('malformed point data is rejected without another HTTP request', async (t) => {
  let attempts = 0;
  const base = await startServer(t, (_req, res) => {
    attempts += 1;
    res.end(new Uint8Array(12));
  });
  await assert.rejects(fetchPointTile(descriptor(base, 'a', 2, 'malformed')), /长度/);
  assert.equal(attempts, 1);
});

test('an oversized plan or duplicate cell is rejected before creating resources', () => {
  const loader = new TileLoader({ create: (points) => points, release: () => {} });
  const base = 'http://127.0.0.1:1';
  assert.throws(() => loader.setSelection([descriptor(base, 'a', 5, 'coarse')], 4), /上限/);
  assert.throws(() => loader.setSelection([descriptor(base, 'a', 2, 'coarse'), descriptor(base, 'a', 2, 'fine')], 4), /重复/);
  assert.equal(loader.stats.selectedPoints, 0);
  loader.dispose();
});
