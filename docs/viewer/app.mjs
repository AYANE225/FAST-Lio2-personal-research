import { validateManifest, validateTrajectory, selectTiles } from './point-cloud-data.mjs';
import { TileLoader } from './tile-loader.mjs';
import { PointCloudRenderer, COLOR_MAPS } from './point-cloud-renderer.mjs';

const $ = (id) => document.getElementById(id);
const integer = new Intl.NumberFormat('zh-CN');
const decimal = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 });
const viewNames = { oblique: '斜视', top: '俯视', detail: '街区细节', full: '全图' };
const modeNames = { height: '高度', intensity: '强度', monochrome: '单色' };
const mobileQuery = matchMedia('(max-width: 700px)');
const state = {
  phase: 'loading', manifest: null, variant: null, colorMode: 'height',
  pointBudget: mobileQuery.matches ? 200000 : 500000, pointSize: 2, edlStrength: 0.4,
  view: 'oblique', spin: false, exporting: false, hasDisplayedPoints: false,
  trajectoryCount: 0, lastError: '',
};
let renderer;
let loader;
let bootController;
let selectionTimer;
let toastTimer;
let lastSelection = '';

function showToast(message) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3800);
}

function setPanel(open) {
  $('app').dataset.panelOpen = String(open);
  $('open-controls').setAttribute('aria-expanded', String(open));
  const hidden = mobileQuery.matches && !open;
  $('sidebar').inert = hidden;
  $('sidebar').setAttribute('aria-hidden', String(hidden));
}

function setLoading(title, detail, failed = false) {
  $('loading-state').hidden = false;
  $('loading-state').classList.toggle('error', failed);
  $('loading-title').textContent = title;
  $('loading-detail').textContent = detail;
  $('loading-symbol').hidden = failed;
  $('retry-load').hidden = !failed;
}

function updateLegend() {
  const mode = state.colorMode;
  const singleColor = mode === 'monochrome';
  $('legend-title').textContent = modeNames[mode];
  $('legend-unit').textContent = mode === 'height' ? 'm' : mode === 'intensity' ? '数值' : '';
  $('legend-scale').hidden = singleColor;
  $('monochrome-legend').hidden = !singleColor;
  $('legend-note').textContent = singleColor ? '深度显示形状' : '固定色域';
  $('legend').setAttribute('aria-label', singleColor ? '统一单色显示' : `${modeNames[mode]}固定颜色范围`);
  if (singleColor || !state.manifest) return;
  const range = state.manifest.color_ranges[mode];
  $('legend-gradient').style.background = `linear-gradient(to top, ${COLOR_MAPS[mode].map(([offset, color]) => `${color} ${offset * 100}%`).join(', ')})`;
  $('legend-ticks').replaceChildren(...[1, 0.75, 0.5, 0.25, 0].map((fraction) => {
    const tick = document.createElement('span');
    tick.textContent = decimal.format(range[0] + (range[1] - range[0]) * fraction);
    return tick;
  }));
  $('legend-note').title = '色域随地图版本与视角保持不变；超出范围的数值使用端点颜色。';
}

function updateStats(stats) {
  const loading = stats.loadingTiles + stats.queuedTiles > 0;
  if (!stats.failedTiles && !loading) state.lastError = '';
  $('loaded-point-count').textContent = integer.format(stats.loadedPoints);
  $('status-dot').className = `status-dot ${stats.failedTiles ? 'error' : loading ? 'loading' : 'ready'}`;
  $('status-text').textContent = stats.failedTiles ? '部分地图未载入' : loading ? '正在加载地图' : stats.loadedPoints ? '地图已就绪' : '当前视野内没有点';
  const fraction = stats.selectedPoints ? stats.loadedPoints / stats.selectedPoints : 0;
  $('load-progress-fill').style.width = `${Math.round(fraction * 100)}%`;
  $('load-progress').setAttribute('aria-valuenow', String(Math.round(fraction * 100)));
  $('screenshot-button').disabled = !stats.loadedPoints || state.exporting;
  if (stats.loadedPoints) {
    state.hasDisplayedPoints = true;
    $('loading-state').hidden = true;
  } else if (!loading && !stats.failedTiles) {
    $('loading-state').hidden = true;
  }
  $('notice').hidden = !stats.failedTiles;
  if (stats.failedTiles) {
    $('notice-message').textContent = `${stats.failedTiles} 个地图分块未能载入。${state.lastError}`;
    if (!stats.loadedPoints && !loading) {
      setLoading('地图未能载入', state.lastError || '请检查网络连接后重试。', true);
    }
  }
}

function refreshSelection() {
  clearTimeout(selectionTimer);
  selectionTimer = null;
  if (!renderer || !loader || !state.variant || state.phase !== 'ready') return;
  const selection = selectTiles(state.variant, {
    budget: state.pointBudget, metrics: renderer.prepareSelection(),
  });
  const signature = `${state.pointBudget}:${selection.map((tile) => tile.key).join('|')}`;
  if (signature === lastSelection) return;
  lastSelection = signature;
  loader.setSelection(selection, state.pointBudget);
}

function scheduleSelection() {
  if (!selectionTimer) selectionTimer = setTimeout(refreshSelection, 160);
}

function updateAxes() {
  if (!renderer) return;
  const axes = renderer.getAxes();
  for (let i = 0; i < 3; i++) {
    const group = $(`axis-${'xyz'[i]}`);
    const line = group.querySelector('line');
    const label = group.querySelector('text');
    const axis = axes[i];
    line.setAttribute('x1', '38');
    line.setAttribute('y1', '39');
    line.setAttribute('x2', String(38 + axis.x * 24));
    line.setAttribute('y2', String(39 - axis.y * 24));
    label.setAttribute('x', String(38 + axis.x * 32));
    label.setAttribute('y', String(39 - axis.y * 32));
    group.style.opacity = axis.z < -0.2 ? '0.5' : '1';
  }
}

function setSpin(enabled) {
  state.spin = enabled;
  renderer?.setSpin(enabled);
  $('spin-toggle').setAttribute('aria-pressed', String(enabled));
  $('spin-toggle').setAttribute('aria-label', enabled ? '暂停旋转' : '自动旋转');
  $('spin-toggle').title = enabled ? '暂停旋转' : '自动旋转';
  $('spin-icon').setAttribute('href', enabled ? '#icon-pause' : '#icon-play');
}

function setView(name, animate = true) {
  if (!renderer || !viewNames[name]) return;
  state.view = name;
  setSpin(false);
  renderer.setView(name, animate);
  $('view-caption').textContent = viewNames[name];
  document.querySelectorAll('[data-view]').forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function updateVariantDescription() {
  $('variant-description').textContent = state.variant.description || '从 Outdoor01 实测点云生成的可浏览地图。';
  $('variant-point-count').textContent = integer.format(state.variant.point_count);
}

async function readTrajectory(manifest, signal) {
  if (!manifest.trajectory || !manifest.trajectory.count) return [];
  try {
    const response = await fetch(manifest.trajectory.url, { signal, credentials: 'same-origin' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return validateTrajectory(await response.json(), manifest.trajectory.count);
  } catch (error) {
    if (signal.aborted) throw error;
    showToast('估计轨迹未能载入，地图仍可正常浏览。');
    return [];
  }
}

async function boot() {
  bootController?.abort();
  bootController = new AbortController();
  const { signal } = bootController;
  clearTimeout(selectionTimer);
  selectionTimer = null;
  loader?.dispose();
  renderer?.dispose();
  loader = undefined;
  renderer = undefined;
  state.phase = 'loading';
  state.hasDisplayedPoints = false;
  state.lastError = '';
  lastSelection = '';
  $('notice').hidden = true;
  $('variant-select').disabled = true;
  $('screenshot-button').disabled = true;
  $('loaded-point-count').textContent = '0';
  $('status-dot').className = 'status-dot loading';
  $('status-text').textContent = '正在读取地图';
  $('load-progress-fill').style.width = '0%';
  $('load-progress').setAttribute('aria-valuenow', '0');
  setLoading('正在读取实测地图', '首次载入需要片刻');
  try {
    if (location.protocol === 'file:') throw new Error('请通过 HTTP 地址访问此页面；直接打开本地 HTML 无法读取分块地图。');
    const manifestUrl = new URL('./data/manifest.json', import.meta.url).href;
    const response = await fetch(manifestUrl, { signal, cache: 'no-cache', credentials: 'same-origin' });
    if (!response.ok) throw new Error(`地图目录读取失败（HTTP ${response.status}）。`);
    const manifest = validateManifest(await response.json(), manifestUrl);
    const trajectory = await readTrajectory(manifest, signal);
    if (signal.aborted) return;
    state.manifest = manifest;
    state.trajectoryCount = trajectory.length;
    state.variant = manifest.variants.find((variant) => variant.id === $('variant-select').value)
      ?? manifest.variants.find((variant) => variant.id === manifest.default_variant)
      ?? manifest.variants[0];
    $('variant-select').replaceChildren(...manifest.variants.map((variant) => {
      const option = document.createElement('option');
      option.value = variant.id;
      option.textContent = variant.label;
      return option;
    }));
    $('variant-select').value = state.variant.id;
    $('variant-select').disabled = false;
    updateVariantDescription();
    renderer = new PointCloudRenderer($('viewport'), {
      onCameraChange: scheduleSelection,
      onFrame: updateAxes,
      onContextLost: () => {
        state.phase = 'error';
        loader?.dispose();
        if (loader) updateStats(loader.stats);
        setLoading('图形显示已中断', '浏览器暂停了图形资源。重新加载可恢复地图。', true);
        $('status-dot').className = 'status-dot error';
        $('status-text').textContent = '图形显示已中断';
      },
    });
    renderer.setDataset(manifest, trajectory);
    renderer.setColorMode(state.colorMode);
    renderer.setPointSize(state.pointSize);
    renderer.setEdlStrength(state.edlStrength);
    renderer.setEdl($('edl-toggle').checked);
    renderer.showGrid($('grid-toggle').checked);
    $('edl-toggle').disabled = !renderer.edlSupported;
    $('edl-toggle').checked = renderer.edlEnabled;
    $('edl-strength').disabled = !renderer.edlEnabled;
    if (!renderer.edlSupported) $('edl-toggle').title = '此浏览器不支持深度纹理。';
    $('trajectory-toggle').disabled = trajectory.length < 2;
    if (trajectory.length < 2) $('trajectory-toggle').checked = false;
    renderer.showTrajectory($('trajectory-toggle').checked);
    renderer.controls.addEventListener('start', () => {
      state.view = 'free';
      $('view-caption').textContent = '自由视角';
      document.querySelectorAll('[data-view]').forEach((button) => {
        button.classList.remove('active');
        button.setAttribute('aria-pressed', 'false');
      });
    });
    loader = new TileLoader({
      create: (points, tile) => renderer.addTile(points, tile),
      release: (points) => renderer.removeTile(points),
      onChange: updateStats,
      onError: (error) => { state.lastError = error.message || '地图文件读取失败。'; },
    });
    state.phase = 'ready';
    setView('oblique', false);
    updateLegend();
    refreshSelection();
  } catch (error) {
    if (signal.aborted) return;
    state.phase = 'error';
    state.lastError = error.message || '地图暂时无法显示。';
    setLoading('地图暂时无法显示', state.lastError, true);
    $('status-dot').className = 'status-dot error';
    $('status-text').textContent = '载入失败';
  }
}

async function exportScreenshot() {
  if (!renderer || !loader?.stats.loadedPoints || state.exporting) return;
  state.exporting = true;
  $('screenshot-button').disabled = true;
  try {
    const source = renderer.screenshot();
    const canvas = document.createElement('canvas');
    const scale = Math.max(1, source.width / 1400);
    canvas.width = source.width;
    canvas.height = source.height + Math.round(76 * scale);
    const context = canvas.getContext('2d');
    if (!context) throw new Error('浏览器无法导出图片。');
    context.drawImage(source, 0, 0);
    context.fillStyle = '#0c1725';
    context.fillRect(0, source.height, canvas.width, canvas.height - source.height);
    const legendWidth = state.colorMode === 'monochrome' ? 95 * scale : Math.min(220 * scale, canvas.width * 0.3);
    const legendLeft = canvas.width - legendWidth - 23 * scale;
    const textWidth = legendLeft - 48 * scale;
    context.fillStyle = '#d8e7ef';
    context.font = `${13 * scale}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
    context.fillText(`Outdoor01 · ${state.variant.label}`, 23 * scale, source.height + 29 * scale, textWidth);
    context.fillStyle = '#88a1b7';
    context.font = `${10 * scale}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
    context.fillText(`FAST-LIO2 · ${integer.format(loader.stats.loadedPoints)} 点 · ${renderer.edlEnabled ? `EDL ${state.edlStrength.toFixed(2)}` : 'EDL 关闭'}`, 23 * scale, source.height + 51 * scale, textWidth);
    context.fillStyle = '#bbcedb';
    context.font = `${9 * scale}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
    if (state.colorMode === 'monochrome') {
      context.fillText('单色显示', legendLeft, source.height + 26 * scale);
      context.fillStyle = '#c7dee9';
      context.fillRect(legendLeft, source.height + 36 * scale, legendWidth, 5 * scale);
    } else {
      const range = state.manifest.color_ranges[state.colorMode];
      const unit = state.colorMode === 'height' ? 'm' : '原始数值';
      context.fillText(`${modeNames[state.colorMode]} / ${unit} · 固定色域`, legendLeft, source.height + 23 * scale);
      const gradient = context.createLinearGradient(legendLeft, 0, legendLeft + legendWidth, 0);
      for (const [offset, color] of COLOR_MAPS[state.colorMode]) gradient.addColorStop(offset, color);
      context.fillStyle = gradient;
      context.fillRect(legendLeft, source.height + 31 * scale, legendWidth, 6 * scale);
      context.fillStyle = '#a6bed0';
      context.fillText(decimal.format(range[0]), legendLeft, source.height + 52 * scale);
      context.textAlign = 'right';
      context.fillText(decimal.format(range[1]), legendLeft + legendWidth, source.height + 52 * scale);
    }
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
    if (!blob) throw new Error('图片生成失败，请重试。');
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `outdoor01_${state.variant.id.replace(/[^a-z\d_-]/gi, '')}_${state.colorMode}.png`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    showToast('已导出当前地图图片。');
  } catch (error) {
    showToast(error.message || '图片导出失败，请重试。');
  } finally {
    state.exporting = false;
    $('screenshot-button').disabled = !loader?.stats.loadedPoints;
  }
}

$('point-budget').value = String(state.pointBudget);
$('open-controls').addEventListener('click', () => setPanel($('app').dataset.panelOpen !== 'true'));
$('close-controls').addEventListener('click', () => { setPanel(false); $('open-controls').focus(); });
mobileQuery.addEventListener('change', () => setPanel(!mobileQuery.matches));
setPanel(!mobileQuery.matches);
$('variant-select').addEventListener('change', (event) => {
  if (!state.manifest) return;
  state.variant = state.manifest.variants.find((variant) => variant.id === event.target.value);
  updateVariantDescription();
  refreshSelection();
});
$('color-controls').addEventListener('click', (event) => {
  const button = event.target.closest('[data-color]');
  if (!button) return;
  state.colorMode = button.dataset.color;
  renderer?.setColorMode(state.colorMode);
  document.querySelectorAll('[data-color]').forEach((item) => {
    const active = item === button;
    item.classList.toggle('active', active);
    item.setAttribute('aria-pressed', String(active));
  });
  updateLegend();
});
$('point-size').addEventListener('input', (event) => {
  state.pointSize = Number(event.target.value);
  $('point-size-value').textContent = state.pointSize.toFixed(1);
  event.target.style.setProperty('--range-progress', `${(state.pointSize - 1) / 4 * 100}%`);
  renderer?.setPointSize(state.pointSize);
});
$('point-budget').addEventListener('change', (event) => {
  state.pointBudget = Number(event.target.value);
  refreshSelection();
});
$('edl-toggle').addEventListener('change', (event) => {
  renderer?.setEdl(event.target.checked);
  $('edl-strength').disabled = !renderer?.edlEnabled;
});
$('edl-strength').addEventListener('input', (event) => {
  state.edlStrength = Number(event.target.value);
  $('edl-strength-value').textContent = state.edlStrength.toFixed(2);
  event.target.style.setProperty('--range-progress', `${state.edlStrength * 100}%`);
  renderer?.setEdlStrength(state.edlStrength);
});
$('trajectory-toggle').addEventListener('change', (event) => renderer?.showTrajectory(event.target.checked));
$('grid-toggle').addEventListener('change', (event) => renderer?.showGrid(event.target.checked));
$('spin-toggle').addEventListener('click', () => setSpin(!state.spin));
$('reset-view').addEventListener('click', () => setView('oblique'));
document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
$('screenshot-button').addEventListener('click', exportScreenshot);
$('retry-load').addEventListener('click', () => {
  if (loader?.stats.failedTiles && renderer && state.phase === 'ready') {
    setLoading('正在重新读取地图', '请稍候');
    loader.retry();
  } else {
    boot();
  }
});
$('retry-tiles').addEventListener('click', () => loader?.retry());
window.addEventListener('keydown', (event) => {
  if (event.target.closest('input, select, textarea, button, a') || event.metaKey || event.ctrlKey || event.altKey) return;
  const key = event.key.toLowerCase();
  if (key === 'r') setView('oblique');
  if (key === 's') exportScreenshot();
  if (['1', '2', '3', '4'].includes(key)) setView(['oblique', 'top', 'detail', 'full'][Number(key) - 1]);
});
window.addEventListener('pagehide', (event) => {
  if (event.persisted) return;
  clearTimeout(selectionTimer);
  clearTimeout(toastTimer);
  bootController?.abort();
  loader?.dispose();
  renderer?.dispose();
});

// Read-only diagnostics for resource, budget, and data validation in a browser.
window.pointCloudViewer = Object.freeze({
  getState() {
    const clouds = renderer?.cloud.children ?? [];
    const lodCounts = {};
    for (const points of clouds) {
      const id = points.userData.tile.lodId;
      lodCounts[id] = (lodCounts[id] ?? 0) + points.geometry.getAttribute('position').count;
    }
    return {
      phase: state.phase, variant: state.variant?.id, colorMode: state.colorMode,
      pointBudget: state.pointBudget, pointSize: state.pointSize,
      ...(loader?.stats ?? {}),
      residentPoints: clouds.reduce((sum, points) => sum + points.geometry.getAttribute('position').count, 0),
      pointBufferBytes: clouds.reduce((sum, points) => sum + points.geometry.getAttribute('position').data.array.byteLength, 0),
      residentTiles: clouds.length, lodCounts,
      selectedTiles: loader ? [...loader.desired.values()].map((tile) => ({ key: tile.key, spatialId: tile.spatialId, lodId: tile.lodId, pointCount: tile.point_count })) : [],
      edlEnabled: renderer?.edlEnabled ?? false, edlSupported: renderer?.edlSupported ?? false,
      edlStrength: state.edlStrength,
      trajectoryVisible: renderer?.trajectory?.visible ?? false, trajectoryCount: state.trajectoryCount,
      view: state.view, spin: state.spin, error: state.lastError,
      camera: renderer ? { position: renderer.camera.position.toArray(), target: renderer.controls.target.toArray() } : null,
      colorRanges: state.manifest ? structuredClone(state.manifest.color_ranges) : null,
      gpuGeometries: renderer?.renderer.info.memory.geometries ?? 0,
    };
  },
});

boot();
