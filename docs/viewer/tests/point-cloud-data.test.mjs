import test from 'node:test';
import assert from 'node:assert/strict';
import { gzipSync } from 'node:zlib';
import {
  validateManifest, selectTiles, decodePointTile, resolveAssetUrl,
} from '../point-cloud-data.mjs';

const bounds = { min: [0, 0, 0], max: [10, 10, 10] };
const tile = (id, count, url) => ({ id, point_count: count, bounds, url, byte_size: 32 });
const makeManifest = () => ({
  schema_version: 1,
  bounds,
  color_ranges: { height: [-2, 20], intensity: [0, 106] },
  variants: [{
    id: 'voxel', label: '体素地图', point_count: 100, bounds,
    lods: [
      { id: 'coarse', voxel_size_m: 0.5, point_count: 40, tiles: [tile('a', 20, 'a-coarse.bin.gz'), tile('b', 20, 'b-coarse.bin.gz')] },
      { id: 'fine', voxel_size_m: 0.1, point_count: 100, tiles: [tile('a', 50, 'a-fine.bin.gz'), tile('b', 50, 'b-fine.bin.gz')] },
    ],
  }],
});

test('LOD selection replaces a spatial cell without exceeding its point budget', () => {
  const variant = validateManifest(makeManifest(), 'https://example.test/viewer/data/manifest.json').variants[0];
  const selection = selectTiles(variant, {
    budget: 70,
    metrics: (_bounds, id) => ({ visible: true, pixelScale: 100, priority: id === 'a' ? 20 : 10 }),
  });
  assert.equal(selection.reduce((n, item) => n + item.point_count, 0), 70);
  assert.deepEqual(selection.map((item) => [item.spatialId, item.lodId]), [['a', 'fine'], ['b', 'coarse']]);
  assert.equal(new Set(selection.map((item) => item.spatialId)).size, selection.length);
});

test('hidden cells are excluded and a cell larger than the budget is not partially counted', () => {
  const variant = validateManifest(makeManifest(), 'https://example.test/viewer/data/manifest.json').variants[0];
  const metrics = (_bounds, id) => ({ visible: id === 'a', pixelScale: 100, priority: 1 });
  assert.deepEqual(selectTiles(variant, { budget: 10, metrics }), []);
  assert.equal(selectTiles(variant, { budget: 100, metrics }).reduce((n, item) => n + item.point_count, 0), 50);
});

test('zooming out retains coarse coverage when its projected spacing is small', () => {
  const variant = validateManifest(makeManifest(), 'https://example.test/viewer/data/manifest.json').variants[0];
  const selection = selectTiles(variant, {
    budget: 100,
    metrics: () => ({ visible: true, pixelScale: 0.5, priority: 1 }),
  });
  assert.deepEqual(selection.map((item) => item.lodId), ['coarse', 'coarse']);
});

test('manifest rejects false point totals, duplicate spatial cells, and invalid fixed ranges', () => {
  const wrongCount = makeManifest();
  wrongCount.variants[0].lods[0].point_count = 99;
  assert.throws(() => validateManifest(wrongCount, 'https://example.test/data/manifest.json'), /点数/);
  const duplicate = makeManifest();
  duplicate.variants[0].lods[0].tiles[1].id = 'a';
  assert.throws(() => validateManifest(duplicate, 'https://example.test/data/manifest.json'), /重复/);
  const invalidRange = makeManifest();
  invalidRange.color_ranges.height = [20, -2];
  assert.throws(() => validateManifest(invalidRange, 'https://example.test/data/manifest.json'), /色域/);
});

test('asset URLs stay within the manifest directory', () => {
  assert.equal(resolveAssetUrl('voxel/a.bin.gz', 'https://example.test/project/data/manifest.json'), 'https://example.test/project/data/voxel/a.bin.gz');
  for (const path of ['../private', 'https://other.test/a', '/other/a', 'data:text/plain,test']) {
    assert.throws(() => resolveAssetUrl(path, 'https://example.test/project/data/manifest.json'), /路径/);
  }
});

function binaryFixture() {
  const values = new Float32Array([1, 2, 3, 4, 5, 6, 7, 8]);
  return new Uint8Array(values.buffer);
}

test('gzip tile decoding preserves interleaved XYZI values', async () => {
  const compressed = gzipSync(binaryFixture());
  const values = await decodePointTile(compressed, { ...tile('a', 2, 'a.bin.gz'), byte_size: compressed.byteLength });
  assert.deepEqual([...values], [1, 2, 3, 4, 5, 6, 7, 8]);
});

test('truncated, overlong, and non-finite point tiles fail before rendering', async () => {
  const descriptor = { ...tile('a', 2, 'a.bin.gz'), byte_size: undefined };
  await assert.rejects(decodePointTile(binaryFixture().slice(0, 28), descriptor), /长度/);
  await assert.rejects(decodePointTile(new Uint8Array(48), descriptor), /长度/);
  const nonFinite = binaryFixture();
  new DataView(nonFinite.buffer).setFloat32(0, Number.NaN, true);
  await assert.rejects(decodePointTile(nonFinite, descriptor), /有限/);
});

test('points outside declared spatial bounds are rejected', async () => {
  const descriptor = { ...tile('a', 2, 'a.bin.gz'), bounds: { min: [0, 0, 0], max: [4, 4, 4] }, byte_size: undefined };
  await assert.rejects(decodePointTile(binaryFixture(), descriptor), /范围/);
});

test('decompression is bounded by the declared number of points', async () => {
  const compressed = gzipSync(new Uint8Array(1024 * 1024));
  await assert.rejects(decodePointTile(compressed, { ...tile('a', 2, 'a.bin.gz'), byte_size: compressed.byteLength }), /长度/);
});
