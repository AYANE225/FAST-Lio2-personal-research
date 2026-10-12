// Export curated Outdoor01 views with the existing perspective / EDL renderer.
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const { values } = parseArgs({options: {
  'data-dir': {type: 'string'}, 'output-dir': {type: 'string'},
  config: {type: 'string', default: join(root, 'configs/map_gallery.json')},
  help: {type: 'boolean', default: false},
}});
if (values.help) {
  console.log('node scripts/render_map_gallery.mjs --data-dir output/rotation-data --output-dir output/gallery-render [--config configs/map_gallery.json]');
  process.exit(0);
}
if (!values['data-dir'] || !values['output-dir']) throw new Error('--data-dir and --output-dir are required');
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const dataDirectory = resolve(values['data-dir']);
const outputDirectory = resolve(values['output-dir']);
const preparationBytes = await readFile(join(dataDirectory, 'preparation.json'));
const preparation = JSON.parse(preparationBytes);
const pointBytes = await readFile(join(dataDirectory, 'points.bin'));
const trajectoryBytes = await readFile(join(dataDirectory, 'trajectory.json'));
const configBytes = await readFile(values.config);
const config = JSON.parse(configBytes);
if (digest(pointBytes) !== preparation.point_file_sha256 || pointBytes.length !== preparation.point_count * 16
  || digest(trajectoryBytes) !== preparation.trajectory_sha256
  || digest(await readFile(join(root, 'docs/viewer/data/manifest.json'))) !== preparation.source_manifest_sha256) {
  throw new Error('Prepared inputs do not match their checksums or the published point-cloud manifest');
}
if (config.schema_version !== 1 || !Array.isArray(config.views) || !config.views.length
  || !Number.isFinite(config.field_of_view_degrees) || config.field_of_view_degrees < 10 || config.field_of_view_degrees > 90
  || !Number.isFinite(config.frame_margin) || config.frame_margin < .5 || config.frame_margin > .98
  || !Number.isFinite(config.device_pixel_ratio) || config.device_pixel_ratio < 1 || config.device_pixel_ratio > 1.75) {
  throw new Error('Invalid gallery camera settings');
}
const identifiers = new Set();
for (const view of config.views) {
  if (!/^[a-z][a-z0-9_]*$/.test(view.id) || identifiers.has(view.id)
    || !['width', 'height'].every(key => Number.isSafeInteger(view[key]) && view[key] >= 600 && view[key] <= 4000)
    || !Number.isFinite(view.azimuth_degrees) || !Number.isFinite(view.elevation_degrees)
    || view.elevation_degrees <= 0 || view.elevation_degrees >= 90
    || !['height', 'intensity', 'monochrome'].includes(view.color_mode)
    || !Number.isFinite(view.point_size_css_px) || view.point_size_css_px < .5 || view.point_size_css_px > 4
    || !Number.isFinite(view.edl_strength) || view.edl_strength < 0 || view.edl_strength > 1
    || typeof view.show_trajectory !== 'boolean') throw new Error('Invalid gallery view');
  if (view.crop_bounds_m && !['min', 'max'].every(key => Array.isArray(view.crop_bounds_m[key])
    && view.crop_bounds_m[key].length === 3 && view.crop_bounds_m[key].every(Number.isFinite))) throw new Error('Invalid crop bounds');
  if (view.crop_bounds_m && !view.crop_bounds_m.min.every((value, i) => value < view.crop_bounds_m.max[i])) throw new Error('Empty crop bounds');
  if (view.palette && (!Array.isArray(view.palette) || view.palette.length < 2
    || view.palette[0][0] !== 0 || view.palette.at(-1)[0] !== 1
    || !view.palette.every((stop, i) => Array.isArray(stop) && stop.length === 2
      && Number.isFinite(stop[0]) && (i === 0 || stop[0] > view.palette[i - 1][0])
      && /^#[a-fA-F0-9]{6}$/.test(stop[1])))) throw new Error('Invalid palette stops');
  identifiers.add(view.id);
}
await mkdir(outputDirectory); // Refuse an existing export directory.

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({headless: true,
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader']});
const page = await browser.newPage({viewport: {width: 1800, height: 1050}, deviceScaleFactor: config.device_pixel_ratio});
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const origin = 'http://127.0.0.1:8798';
const viewerDirectory = join(root, 'docs/viewer');
const virtualFiles = new Map([
  ['/', {body: '<!doctype html><meta charset="utf-8"><style>html,body,#viewport{margin:0;width:100%;height:100%;overflow:hidden}canvas{display:block;width:100%;height:100%}</style><div id="viewport"></div>', contentType: 'text/html'}],
  ['/points.bin', {body: pointBytes, contentType: 'application/octet-stream'}],
  ['/trajectory.json', {body: trajectoryBytes, contentType: 'application/json'}],
]);
await page.route('**/*', async route => {
  try {
    const url = new URL(route.request().url());
    if (url.origin !== origin) throw new Error('The gallery renderer only reads local resources');
    const path = decodeURIComponent(url.pathname);
    if (virtualFiles.has(path)) return await route.fulfill(virtualFiles.get(path));
    if (path === '/favicon.ico') return await route.fulfill({status: 204});
    const file = resolve(viewerDirectory, path.replace(/^\/viewer\//, ''));
    if (!path.startsWith('/viewer/') || !file.startsWith(viewerDirectory + sep)
      || !['.mjs', '.js'].includes(extname(file))) throw new Error(`Unexpected resource: ${path}`);
    await route.fulfill({body: await readFile(file), contentType: 'text/javascript'});
  } catch (error) {
    errors.push(error.message);
    await route.fulfill({status: 500, body: 'Rendering resource failed'});
  }
});
const outputs = {};
const views = [];
let graphics;
try {
  await page.goto(origin, {waitUntil: 'load'});
  graphics = await page.evaluate(async ({preparation, config}) => {
    const [{PointCloudRenderer, COLOR_MAPS}, pointResponse, trajectoryResponse] = await Promise.all([
      import('/viewer/point-cloud-renderer.mjs'), fetch('/points.bin'), fetch('/trajectory.json'),
    ]);
    const points = new Float32Array(await pointResponse.arrayBuffer());
    if (points.length !== preparation.point_count * 4 || !points.every(Number.isFinite)) throw new Error('Invalid point buffer');
    const trajectory = (await trajectoryResponse.json()).positions;
    if (trajectory.length !== preparation.trajectory_count
      || !trajectory.every(position => position.length === 3 && position.every(Number.isFinite))) throw new Error('Invalid trajectory');
    const renderer = new PointCloudRenderer(document.getElementById('viewport'), {
      onContextLost: () => { throw new Error('WebGL context lost during gallery export'); },
    });
    cancelAnimationFrame(renderer.animationFrame);
    renderer.controls.enabled = false;
    renderer.controls.enableDamping = false;
    renderer.setDataset(preparation, trajectory);
    renderer.setSpin(false);
    renderer.showGrid(false);
    if (!renderer.edlSupported) throw new Error('EDL requires depth textures');
    window.drawGalleryView = async view => {
      for (const cloud of [...renderer.cloud.children]) renderer.removeTile(cloud);
      let selected = points;
      if (view.crop_bounds_m) {
        const {min, max} = view.crop_bounds_m;
        const inside = offset => [0, 1, 2].every(i => points[offset + i] >= min[i] && points[offset + i] <= max[i]);
        let count = 0;
        for (let i = 0; i < points.length; i += 4) if (inside(i)) count++;
        if (!count) throw new Error('The selected region contains no points');
        selected = new Float32Array(count * 4);
        let offset = 0;
        for (let i = 0; i < points.length; i += 4) if (inside(i)) {
          selected.set(points.subarray(i, i + 4), offset);
          offset += 4;
        }
      }
      const bounds = {min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity]};
      for (let i = 0; i < selected.length; i += 4) for (let axis = 0; axis < 3; axis++) {
        bounds.min[axis] = Math.min(bounds.min[axis], selected[i + axis]);
        bounds.max[axis] = Math.max(bounds.max[axis], selected[i + axis]);
      }
      renderer.addTile(selected, {id: view.id, bounds, point_count: selected.length / 4});
      renderer.resize();
      renderer.setColorMode(view.color_mode);
      if (view.color_mode !== 'monochrome') {
        const palette = view.palette ?? COLOR_MAPS[view.color_mode];
        const texture = renderer.pointMaterial.uniforms.palette.value;
        for (let i = 0; i < 256; i++) {
          const t = i / 255;
          const right = Math.max(1, palette.findIndex(stop => stop[0] >= t));
          const [start, leftHex] = palette[right - 1];
          const [end, rightHex] = palette[right];
          const mix = (t - start) / (end - start);
          const leftColor = parseInt(leftHex.slice(1), 16), rightColor = parseInt(rightHex.slice(1), 16);
          for (let channel = 0; channel < 3; channel++) {
            const shift = (2 - channel) * 8;
            texture.image.data[i * 4 + channel] = Math.round(((leftColor >> shift) & 255) * (1 - mix) + ((rightColor >> shift) & 255) * mix);
          }
        }
        texture.needsUpdate = true;
      }
      renderer.setPointSize(view.point_size_css_px);
      renderer.setEdlStrength(view.edl_strength);
      renderer.showTrajectory(view.show_trajectory);
      const azimuth = view.azimuth_degrees * Math.PI / 180;
      const elevation = view.elevation_degrees * Math.PI / 180;
      const d = [Math.cos(elevation) * Math.cos(azimuth), Math.cos(elevation) * Math.sin(azimuth), Math.sin(elevation)];
      const r = [-Math.sin(azimuth), Math.cos(azimuth), 0];
      const u = [-Math.sin(elevation) * Math.cos(azimuth), -Math.sin(elevation) * Math.sin(azimuth), Math.cos(elevation)];
      const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
      const visit = callback => {
        for (let i = 0; i < selected.length; i += 4) callback([selected[i], selected[i + 1], selected[i + 2]]);
        if (view.show_trajectory) trajectory.forEach(callback);
      };
      const ranges = [r, u, d].map(axis => {
        let min = Infinity, max = -Infinity;
        visit(point => {const value = dot(point, axis); min = Math.min(min, value); max = Math.max(max, value);});
        return [min, max];
      });
      const center = [0, 1, 2].map(i => [r, u, d].reduce((sum, axis, j) => sum + axis[i] * (ranges[j][0] + ranges[j][1]) / 2, 0));
      renderer.camera.fov = config.field_of_view_degrees;
      const tanV = Math.tan(renderer.camera.fov * Math.PI / 360);
      const tanH = tanV * renderer.camera.aspect;
      let distance = 0;
      let depthMin = Infinity, depthMax = -Infinity;
      visit(point => {
        const relative = point.map((value, i) => value - center[i]);
        const depth = dot(relative, d);
        depthMin = Math.min(depthMin, depth); depthMax = Math.max(depthMax, depth);
        distance = Math.max(distance, depth + Math.abs(dot(relative, r)) / (tanH * config.frame_margin),
          depth + Math.abs(dot(relative, u)) / (tanV * config.frame_margin));
      });
      distance += 1;
      renderer.camera.near = Math.max(.1, distance - depthMax - 1);
      renderer.camera.far = distance - depthMin + 1;
      renderer.camera.position.set(...center.map((value, i) => value + distance * d[i]));
      renderer.controls.target.set(...center);
      renderer.camera.lookAt(renderer.controls.target);
      renderer.camera.updateProjectionMatrix();
      renderer.camera.updateMatrixWorld();
      renderer.postMaterial.uniforms.cameraNear.value = renderer.camera.near;
      renderer.postMaterial.uniforms.cameraFar.value = renderer.camera.far;
      renderer.render();
      const hash = await crypto.subtle.digest('SHA-256', selected);
      const selectedSha = Array.from(new Uint8Array(hash), value => value.toString(16).padStart(2, '0')).join('');
      const clip = [0, 0];
      visit(point => {
        const relative = point.map((value, i) => value - center[i]);
        const depth = distance - dot(relative, d);
        clip[0] = Math.max(clip[0], Math.abs(dot(relative, r)) / (depth * tanH));
        clip[1] = Math.max(clip[1], Math.abs(dot(relative, u)) / (depth * tanV));
      });
      if (clip.some(value => value > config.frame_margin)) throw new Error('A selected point falls outside the camera margin');
      return {id: view.id, selected_points: selected.length / 4, selected_xyzi_sha256: selectedSha,
        bounds_m: bounds, center_m: center, camera_position_m: renderer.camera.position.toArray(),
        near_m: renderer.camera.near, far_m: renderer.camera.far, max_abs_ndc_xy: clip,
        viewport_css_pixels: [renderer.container.clientWidth, renderer.container.clientHeight],
        drawing_buffer_pixels: [renderer.renderer.domElement.width, renderer.renderer.domElement.height]};
    };
    const gl = renderer.renderer.getContext();
    const debug = gl.getExtension('WEBGL_debug_renderer_info');
    return {renderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
      edl: true, color_maps: COLOR_MAPS};
  }, {preparation, config});
  for (const view of config.views) {
    await page.setViewportSize({width: view.width, height: view.height});
    views.push(await page.evaluate(view => window.drawGalleryView(view), view));
    const name = `${view.id}.png`;
    const bytes = await page.screenshot({path: join(outputDirectory, name), scale: 'css'});
    if (errors.length) throw new Error(errors.join('\n'));
    outputs[name] = {size_bytes: bytes.length, sha256: digest(bytes)};
    console.log(`${view.id}: ${views.at(-1).selected_points.toLocaleString('en-US')} points`);
  }
} finally {
  await browser.close();
}
const sources = {};
for (const name of ['scripts/render_map_gallery.mjs', 'docs/viewer/point-cloud-renderer.mjs',
  'docs/viewer/vendor/three.module.min.js', 'docs/viewer/vendor/OrbitControls.js']) sources[name] = digest(await readFile(join(root, name)));
const report = {schema_version: 1, scope: 'Static views of the published point cloud. Crops and display shading only; unchanged coordinates and poses.',
  preparation_sha256: digest(preparationBytes), point_file_sha256: digest(pointBytes), trajectory_sha256: digest(trajectoryBytes),
  source_manifest_sha256: preparation.source_manifest_sha256, config, config_sha256: digest(configBytes),
  color_ranges: preparation.color_ranges, browser_version: browser.version(), graphics, views, source_sha256: sources, outputs};
await writeFile(join(outputDirectory, 'rendering.json'), JSON.stringify(report, null, 2) + '\n');
console.log('Saved gallery views and rendering.json.');
