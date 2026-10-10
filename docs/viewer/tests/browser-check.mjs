import assert from 'node:assert/strict';
import { mkdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';

// Serve docs/ over HTTP, then run this script with Playwright installed.
// PLAYWRIGHT_MODULE may point to an existing Playwright ESM entry point.
// VIEWER_SCREENSHOT_DIR optionally saves a fixed-camera EDL comparison.
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.VIEWER_URL || 'http://127.0.0.1:8767/viewer/';
const screenshotDirectory = process.env.VIEWER_SCREENSHOT_DIR;
const browser = await chromium.launch({
  headless: true,
  // Explicit ANGLE + SwiftShader avoids unstable GPU selection in headless CI.
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const readState = () => page.evaluate(() => window.pointCloudViewer.getState());
  async function waitForMap() {
    await page.waitForFunction(() => {
      const state = window.pointCloudViewer?.getState();
      return state && (state.phase === 'error' || (state.phase === 'ready'
        && state.loadingTiles === 0 && state.queuedTiles === 0));
    }, null, { timeout: 90000 });
    const state = await readState();
    assert.equal(state.phase, 'ready', state.error);
    assert.equal(state.failedTiles, 0, state.error);
    assert.ok(state.loadedPoints > 0);
    assert.ok(state.loadedPoints <= state.pointBudget);
    assert.equal(state.residentPoints, state.loadedPoints);
    assert.equal(state.pointBufferBytes, state.loadedPoints * 16);
    assert.equal(new Set(state.selectedTiles.map((tile) => tile.spatialId)).size, state.selectedTiles.length);
    assert.ok(state.selectedTiles.reduce((sum, tile) => sum + tile.pointCount, 0) <= state.pointBudget);
    return state;
  }

  await page.goto(url, { waitUntil: 'domcontentloaded' });
  const initial = await waitForMap();
  const manifest = await page.evaluate(async () => {
    const response = await fetch('./data/manifest.json');
    return response.json();
  });
  const expectedVariant = manifest.variants.find((variant) => variant.id === manifest.default_variant)
    ?? manifest.variants[0];
  assert.equal(initial.variant, expectedVariant.id);
  assert.equal(initial.edlStrength, 0.4);

  if (manifest.variants.some((variant) => variant.id === 'sor') && initial.variant !== 'sor') {
    await page.selectOption('#variant-select', 'sor');
    await waitForMap();
  }
  async function setStrength(value) {
    await page.locator('#edl-strength').evaluate((input, strength) => {
      input.value = String(strength);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }, value);
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal((await readState()).edlStrength, value);
  }
  if (screenshotDirectory) {
    await mkdir(screenshotDirectory, { recursive: true });
    await setStrength(0.4);
    await page.screenshot({ path: join(screenshotDirectory, 'edl-strength-040.png') });
    await setStrength(1);
    await page.screenshot({ path: join(screenshotDirectory, 'edl-strength-100.png') });
  }
  await setStrength(0.4);
  await page.uncheck('#edl-toggle');
  assert.equal(await page.locator('#edl-strength').isDisabled(), true);
  await page.check('#edl-toggle');
  assert.equal(await page.locator('#edl-strength').isDisabled(), false);
  const beforeLoss = await waitForMap();

  await page.evaluate(() => {
    const canvas = document.querySelector('#viewport canvas');
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    const extension = gl.getExtension('WEBGL_lose_context');
    if (!extension) throw new Error('WEBGL_lose_context is required for this regression check.');
    window.viewerTestContextLoss = extension;
    window.viewerTestContextRestored = false;
    canvas.addEventListener('webglcontextrestored', () => { window.viewerTestContextRestored = true; }, { once: true });
    extension.loseContext();
  });
  await page.waitForFunction(() => window.pointCloudViewer.getState().phase === 'error');
  assert.equal((await readState()).residentPoints, 0);
  await page.evaluate(() => window.viewerTestContextLoss.restoreContext());
  await page.waitForFunction(() => window.viewerTestContextRestored);
  await page.click('#retry-load');
  const restored = await waitForMap();
  assert.equal(restored.variant, beforeLoss.variant);
  assert.equal(restored.loadedPoints, beforeLoss.loadedPoints);
  assert.equal(restored.edlStrength, 0.4);
  assert.equal(await page.locator('#viewport canvas').count(), 1);

  // Exercise actual replacement of LODs/variants and release of old point buffers.
  for (const variant of ['voxel', 'sor']) {
    await page.selectOption('#variant-select', variant);
    for (const budget of ['200000', '1000000']) {
      await page.selectOption('#point-budget', budget);
      const state = await waitForMap();
      assert.equal(state.variant, variant);
      assert.equal(state.pointBudget, Number(budget));
    }
  }
  await page.selectOption('#point-budget', '500000');
  await waitForMap();
  for (const mode of ['height', 'intensity', 'monochrome']) {
    await page.click(`[data-color="${mode}"]`);
    assert.equal((await readState()).colorMode, mode);
    if (screenshotDirectory) await page.screenshot({ path: join(screenshotDirectory, `color-${mode}.png`) });
  }
  await page.click('[data-color="height"]');
  await page.uncheck('#edl-toggle');
  if (screenshotDirectory) await page.screenshot({ path: join(screenshotDirectory, 'edl-off.png') });
  await page.check('#edl-toggle');
  for (const view of ['top', 'detail', 'full', 'oblique']) {
    await page.click(`[data-view="${view}"]`);
    // Camera presets animate for 650 ms; allow that animation to settle.
    await page.waitForTimeout(900);
    await waitForMap();
    assert.equal((await readState()).view, view);
    if (screenshotDirectory) await page.screenshot({ path: join(screenshotDirectory, `view-${view}.png`) });
  }
  const downloadPromise = page.waitForEvent('download');
  await page.click('#screenshot-button');
  const download = await downloadPromise;
  assert.match(download.suggestedFilename(), /^outdoor01_sor_height\.png$/);
  const png = await readFile(await download.path());
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.ok(png.readUInt32BE(16) > 1000);
  if (screenshotDirectory) {
    await download.saveAs(join(screenshotDirectory, 'exported-map.png'));
    await page.locator('#toast').waitFor({ state: 'hidden' });
    await page.screenshot({ path: join(screenshotDirectory, 'desktop.png') });
  }
  assert.deepEqual(errors, []);
  const { selectedTiles, ...summary } = await readState();
  await page.close();

  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  mobile.on('pageerror', (error) => errors.push(error.message));
  await mobile.goto(url, { waitUntil: 'domcontentloaded' });
  await mobile.waitForFunction(() => {
    const state = window.pointCloudViewer?.getState();
    return state?.phase === 'ready' && state.loadedPoints > 0 && !state.loadingTiles && !state.queuedTiles;
  }, null, { timeout: 90000 });
  const mobileState = await mobile.evaluate(() => window.pointCloudViewer.getState());
  assert.equal(mobileState.pointBudget, 200000);
  assert.ok(mobileState.loadedPoints <= mobileState.pointBudget);
  assert.equal(mobileState.failedTiles, 0);
  assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await mobile.click('#open-controls');
  await mobile.locator('#variant-select').waitFor({ state: 'visible' });
  await mobile.waitForFunction(() => getComputedStyle(document.querySelector('#sidebar')).opacity === '1');
  if (screenshotDirectory) await mobile.screenshot({ path: join(screenshotDirectory, 'mobile-controls.png') });
  await mobile.click('#close-controls');
  await mobile.waitForFunction(() => getComputedStyle(document.querySelector('#sidebar')).opacity === '0');
  assert.equal(await mobile.locator('#open-controls').getAttribute('aria-expanded'), 'false');
  assert.equal(await mobile.locator('#sidebar').getAttribute('aria-hidden'), 'true');
  if (screenshotDirectory) await mobile.screenshot({ path: join(screenshotDirectory, 'mobile.png') });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ browserCheck: 'passed', state: summary,
    mobile: { width: 390, loadedPoints: mobileState.loadedPoints, pointBudget: mobileState.pointBudget },
    exportedPngBytes: png.length, pageErrors: errors }, null, 2));
} finally {
  await browser.close();
}
