const BYTES_PER_POINT = 16;
const MAX_TILE_POINTS = 16_000_000;
const littleEndian = new Uint8Array(new Uint32Array([0x12345678]).buffer)[0] === 0x78;

function requireCount(value, label, allowZero = false) {
  if (!Number.isSafeInteger(value) || value < (allowZero ? 0 : 1)) {
    throw new Error(`${label}点数无效。`);
  }
  return value;
}

function validateBounds(value, label) {
  if (!value || !['min', 'max'].every((key) => Array.isArray(value[key])
    && value[key].length === 3 && value[key].every(Number.isFinite))
    || value.min.some((v, i) => v > value.max[i])) {
    throw new Error(`${label}的坐标范围无效。`);
  }
  return { min: [...value.min], max: [...value.max] };
}

function unionBounds(a, b) {
  return {
    min: a.min.map((v, i) => Math.min(v, b.min[i])),
    max: a.max.map((v, i) => Math.max(v, b.max[i])),
  };
}

function validateIdentifier(value, label) {
  if (typeof value !== 'string' || !value.trim() || value.length > 160) {
    throw new Error(`${label}标识无效。`);
  }
  return value;
}

export function resolveAssetUrl(path, manifestUrl) {
  if (typeof path !== 'string' || !path || /^(?:[a-z][a-z\d+.-]*:|\/|\\)/i.test(path)) {
    throw new Error('地图数据路径必须是同目录下的相对路径。');
  }
  const directory = new URL('.', manifestUrl);
  const resolved = new URL(path, directory);
  if (resolved.origin !== directory.origin || !resolved.pathname.startsWith(directory.pathname)
    || resolved.username || resolved.password || resolved.hash || resolved.search) {
    throw new Error('地图数据路径超出了数据目录。');
  }
  return resolved.href;
}

export function validateManifest(value, manifestUrl) {
  if (!value || value.schema_version !== 1) throw new Error('不支持此地图数据版本。');
  const bounds = validateBounds(value.bounds, '地图');
  const colorRanges = {};
  for (const name of ['height', 'intensity']) {
    const range = value.color_ranges?.[name];
    if (!Array.isArray(range) || range.length !== 2 || !range.every(Number.isFinite) || range[0] >= range[1]) {
      throw new Error('地图的固定色域无效。');
    }
    colorRanges[name] = [...range];
  }
  if (!Array.isArray(value.variants) || !value.variants.length) throw new Error('没有可显示的地图版本。');
  const variantIds = new Set();
  const variants = value.variants.map((variant) => {
    const id = validateIdentifier(variant.id, '地图版本');
    if (variantIds.has(id)) throw new Error('地图版本标识重复。');
    variantIds.add(id);
    requireCount(variant.point_count, '地图版本');
    const variantBounds = validateBounds(variant.bounds ?? bounds, '地图版本');
    if (!Array.isArray(variant.lods) || !variant.lods.length) throw new Error('地图没有分辨率数据。');
    const lodIds = new Set();
    const cells = new Map();
    const lods = [...variant.lods].sort((a, b) => b.voxel_size_m - a.voxel_size_m).map((lod) => {
      const lodId = validateIdentifier(lod.id, '分辨率');
      if (lodIds.has(lodId)) throw new Error('分辨率标识重复。');
      lodIds.add(lodId);
      if (!Number.isFinite(lod.voxel_size_m) || lod.voxel_size_m <= 0) throw new Error('体素尺寸无效。');
      if (!Array.isArray(lod.tiles) || !lod.tiles.length) throw new Error('地图分块列表为空。');
      requireCount(lod.point_count, '分辨率');
      const spatialIds = new Set();
      const tiles = lod.tiles.map((entry) => {
        const tileId = validateIdentifier(entry.id, '分块');
        const spatialId = validateIdentifier(entry.spatial_id ?? tileId, '空间分块');
        if (spatialIds.has(spatialId)) throw new Error('同一分辨率的空间分块重复。');
        spatialIds.add(spatialId);
        const pointCount = requireCount(entry.point_count, '分块');
        if (pointCount > MAX_TILE_POINTS) throw new Error('单块地图的点数过大。');
        if (entry.byte_size !== undefined && (!Number.isSafeInteger(entry.byte_size) || entry.byte_size <= 0)) {
          throw new Error('分块字节长度无效。');
        }
        if (entry.sha256 !== undefined && !/^[a-f\d]{64}$/i.test(entry.sha256)) throw new Error('地图文件校验值无效。');
        const tileBounds = validateBounds(entry.bounds, '分块');
        const tile = {
          ...entry, id: tileId, spatialId, lodId, variantId: id,
          key: JSON.stringify([id, lodId, spatialId]),
          voxelSize: lod.voxel_size_m, bounds: tileBounds,
          url: resolveAssetUrl(entry.url, manifestUrl),
        };
        const cell = cells.get(spatialId);
        if (cell) {
          cell.bounds = unionBounds(cell.bounds, tileBounds);
          cell.levels.push(tile);
        } else {
          cells.set(spatialId, { spatialId, bounds: tileBounds, levels: [tile] });
        }
        return tile;
      });
      if (tiles.reduce((sum, entry) => sum + entry.point_count, 0) !== lod.point_count) {
        throw new Error('分块点数总和与地图声明不一致。');
      }
      return { ...lod, tiles };
    });
    return {
      ...variant, id, label: variant.label || id, description: variant.description || '',
      bounds: variantBounds, lods, cells: [...cells.values()],
    };
  });
  const trajectory = value.trajectory ? {
    ...value.trajectory,
    url: resolveAssetUrl(value.trajectory.url, manifestUrl),
    count: requireCount(value.trajectory.count, '轨迹', true),
  } : null;
  return { ...value, bounds, variants, color_ranges: colorRanges, trajectory };
}

/** Select exactly one independent LOD for each visible spatial cell. */
export function selectTiles(variant, { budget, metrics, targetPixelSpacing = 1.15 }) {
  if (!Number.isSafeInteger(budget) || budget < 1) throw new Error('显示点数上限无效。');
  const candidates = variant.cells.map((cell) => {
    const measured = metrics(cell.bounds, cell.spatialId);
    return { ...cell, ...measured, level: 0 };
  }).filter((cell) => cell.visible).sort((a, b) => (b.priority - a.priority)
    || a.spatialId.localeCompare(b.spatialId));
  let points = 0;
  const selected = [];
  for (const cell of candidates) {
    const count = cell.levels[0].point_count;
    if (points + count <= budget) {
      selected.push(cell);
      points += count;
    }
  }
  // Start with coarse coverage, then spend remaining points on nearby cells.
  // Each upgrade replaces its previous level, never overlays it.
  for (;;) {
    const upgrades = selected.filter((cell) => cell.level + 1 < cell.levels.length
      && cell.levels[cell.level].voxelSize * cell.pixelScale > targetPixelSpacing).map((cell) => {
      const current = cell.levels[cell.level];
      const next = cell.levels[cell.level + 1];
      const cost = next.point_count - current.point_count;
      return { cell, cost, score: cell.priority * Math.log(current.voxelSize / next.voxelSize) / Math.max(1, cost) };
    }).sort((a, b) => b.score - a.score);
    let upgraded = false;
    for (const { cell, cost } of upgrades) {
      if (points + cost > budget) continue;
      cell.level += 1;
      points += cost;
      upgraded = true;
    }
    if (!upgraded) break;
  }
  return selected.map((cell) => cell.levels[cell.level]);
}

function checkAbort(signal) {
  if (signal?.aborted) throw signal.reason ?? new DOMException('加载已取消。', 'AbortError');
}

async function readLimitedStream(stream, maxBytes, signal) {
  const reader = stream.getReader();
  const chunks = [];
  let length = 0;
  try {
    for (;;) {
      checkAbort(signal);
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > maxBytes) throw new Error('地图文件长度超过声明值。');
      chunks.push(value);
    }
    checkAbort(signal);
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

export async function decodePointTile(input, tile, signal) {
  checkAbort(signal);
  const pointCount = requireCount(tile.point_count, '分块');
  if (pointCount > MAX_TILE_POINTS) throw new Error('单块地图的点数过大。');
  const expectedBytes = pointCount * BYTES_PER_POINT;
  let bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  const gzip = bytes[0] === 0x1f && bytes[1] === 0x8b;
  if (gzip) {
    if (tile.byte_size !== undefined && bytes.byteLength !== tile.byte_size) throw new Error('压缩地图文件长度不符。');
    if (tile.sha256 && globalThis.crypto?.subtle) {
      const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
      const hex = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
      if (hex !== tile.sha256.toLowerCase()) throw new Error('地图文件校验失败，请重新加载。');
    }
    checkAbort(signal);
    if (typeof DecompressionStream !== 'function') {
      throw new Error('此浏览器无法解压地图，请使用新版 Chrome、Edge、Firefox 或 Safari。');
    }
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
    bytes = await readLimitedStream(stream, expectedBytes, signal);
  }
  // A static server may already decode gzip through Content-Encoding.
  if (bytes.byteLength !== expectedBytes) throw new Error('地图点数与解压后的文件长度不一致。');
  checkAbort(signal);
  let values;
  if (littleEndian) {
    if (bytes.byteOffset % Float32Array.BYTES_PER_ELEMENT) bytes = bytes.slice();
    values = new Float32Array(bytes.buffer, bytes.byteOffset, pointCount * 4);
  } else {
    values = new Float32Array(pointCount * 4);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    for (let i = 0; i < values.length; i++) values[i] = view.getFloat32(i * 4, true);
  }
  const min = tile.bounds.min;
  const max = tile.bounds.max;
  const tolerance = 0.002; // metres; covers float32 rounding in the exported coordinates.
  for (let i = 0; i < values.length; i += 4) {
    if (!Number.isFinite(values[i]) || !Number.isFinite(values[i + 1])
      || !Number.isFinite(values[i + 2]) || !Number.isFinite(values[i + 3])) {
      throw new Error('地图包含非有限坐标或强度值。');
    }
    for (let axis = 0; axis < 3; axis++) {
      if (values[i + axis] < min[axis] - tolerance || values[i + axis] > max[axis] + tolerance) {
        throw new Error('地图坐标超出了声明的空间范围。');
      }
    }
  }
  return values;
}

export async function fetchPointTile(tile, signal) {
  const response = await fetch(tile.url, { signal, credentials: 'same-origin' });
  if (!response.ok) throw new Error(`地图文件读取失败（HTTP ${response.status}）。`);
  const expectedBytes = tile.point_count * BYTES_PER_POINT;
  const maxBytes = Math.max(tile.byte_size ?? expectedBytes + 65536, expectedBytes);
  const headerLength = Number(response.headers.get('Content-Length'));
  if (Number.isFinite(headerLength) && headerLength > maxBytes) throw new Error('地图文件长度超过声明值。');
  const bytes = response.body
    ? await readLimitedStream(response.body, maxBytes, signal)
    : new Uint8Array(await response.arrayBuffer());
  return decodePointTile(bytes, tile, signal);
}

export function validateTrajectory(value, expectedCount) {
  if (!value || !Array.isArray(value.positions) || value.positions.length !== expectedCount
    || !value.positions.every((point) => Array.isArray(point) && point.length === 3 && point.every(Number.isFinite))) {
    throw new Error('估计轨迹的坐标或点数无效。');
  }
  if (value.timestamps !== undefined && (!Array.isArray(value.timestamps)
    || value.timestamps.length !== expectedCount || !value.timestamps.every(Number.isFinite))) {
    throw new Error('估计轨迹的时间戳无效。');
  }
  return value.positions;
}
