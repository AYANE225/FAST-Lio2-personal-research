import { fetchPointTile } from './point-cloud-data.mjs';

/** Own the currently selected point buffers, requests, and GPU resources. */
export class TileLoader {
  constructor({ create, release, onChange = () => {}, onError = () => {}, concurrency = 4 }) {
    this.create = create;
    this.release = release;
    this.onChange = onChange;
    this.onError = onError;
    this.concurrency = concurrency;
    this.desired = new Map();
    this.loaded = new Map();
    this.pending = new Map();
    this.failed = new Map();
    this.budget = 0;
    this.closed = false;
  }

  get stats() {
    let selectedPoints = 0;
    let loadedPoints = 0;
    let loadingPoints = 0;
    let loadingTiles = 0;
    let queuedTiles = 0;
    for (const tile of this.desired.values()) {
      selectedPoints += tile.point_count;
      if (this.loaded.has(tile.key)) {
        loadedPoints += tile.point_count;
      } else if (this.pending.has(tile.key) && !this.pending.get(tile.key).controller.signal.aborted) {
        loadingPoints += tile.point_count;
        loadingTiles += 1;
      } else if (!this.failed.has(tile.key)) {
        queuedTiles += 1;
      }
    }
    return {
      budget: this.budget, selectedPoints, loadedPoints, loadingPoints,
      loadedTiles: this.loaded.size, selectedTiles: this.desired.size,
      loadingTiles, queuedTiles, failedTiles: this.failed.size,
    };
  }

  setSelection(tiles, budget) {
    if (this.closed) return;
    if (!Number.isSafeInteger(budget) || budget <= 0
      || tiles.reduce((sum, tile) => sum + tile.point_count, 0) > budget) {
      throw new Error('所选地图超过显示点数上限。');
    }
    if (new Set(tiles.map((tile) => tile.spatialId)).size !== tiles.length
      || new Set(tiles.map((tile) => tile.key)).size !== tiles.length) {
      throw new Error('同一个空间分块不能重复显示。');
    }
    this.budget = budget;
    this.desired = new Map(tiles.map((tile) => [tile.key, tile]));
    // Dispose old GPU data before starting replacements, including LOD upgrades.
    for (const [key, item] of this.loaded) {
      if (!this.desired.has(key)) {
        this.release(item.resource);
        this.loaded.delete(key);
      }
    }
    for (const [key, request] of this.pending) {
      if (!this.desired.has(key)) request.controller.abort();
    }
    for (const key of this.failed.keys()) {
      if (!this.desired.has(key)) this.failed.delete(key);
    }
    this.pump();
    this.onChange(this.stats);
  }

  retry() {
    this.failed.clear();
    this.pump();
    this.onChange(this.stats);
  }

  pump() {
    if (this.closed) return;
    for (const tile of this.desired.values()) {
      if (this.pending.size >= this.concurrency) break;
      if (this.loaded.has(tile.key) || this.pending.has(tile.key) || this.failed.has(tile.key)) continue;
      const request = { controller: new AbortController(), tile };
      this.pending.set(tile.key, request);
      this.load(request);
    }
  }

  async load(request) {
    const { tile, controller } = request;
    try {
      const points = await fetchPointTile(tile, controller.signal);
      if (this.closed || controller.signal.aborted || !this.desired.has(tile.key)) return;
      const resource = this.create(points, tile);
      this.loaded.set(tile.key, { tile, resource });
    } catch (error) {
      if (!this.closed && !controller.signal.aborted && this.desired.has(tile.key)) {
        this.failed.set(tile.key, error);
        this.onError(error, tile);
      }
    } finally {
      this.pending.delete(tile.key);
      if (!this.closed) {
        this.pump();
        this.onChange(this.stats);
      }
    }
  }

  dispose() {
    if (this.closed) return;
    this.closed = true;
    for (const request of this.pending.values()) request.controller.abort();
    for (const item of this.loaded.values()) this.release(item.resource);
    this.desired.clear();
    this.loaded.clear();
    this.failed.clear();
  }
}
