// Render a fixed dense cloud through the existing Three.js / EDL renderer.
// PLAYWRIGHT_MODULE accepts the same optional ESM entry point as browser-check.mjs.
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { copyFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const { values } = parseArgs({ options: {
  'data-dir': { type: 'string' }, 'output-dir': { type: 'string' },
  width: { type: 'string', default: '1400' }, height: { type: 'string', default: '1050' },
  frames: { type: 'string', default: '320' }, fps: { type: 'string', default: '20' },
  'point-size': { type: 'string', default: '1.6' },
  help: { type: 'boolean', default: false },
} });
if (values.help) {
  console.log('node scripts/render_map_rotation.mjs --data-dir output/rotation-data --output-dir output/rotation [--frames 320 --fps 20 --width 1400 --height 1050 --point-size 1.6]');
  process.exit(0);
}
if (!values['data-dir'] || !values['output-dir']) throw new Error('--data-dir and --output-dir are required');
const settings = Object.fromEntries(['width', 'height', 'frames', 'fps'].map((key) => [key, Number(values[key])]));
for (const [key, value] of Object.entries(settings)) {
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`Invalid ${key}`);
}
if (settings.width < 800 || settings.height < 600 || settings.width % 2 || settings.height % 2
  || settings.frames < 4 || 100 % settings.fps !== 0) {
  throw new Error('Use even dimensions >= 800 x 600, at least four frames, and an fps that divides 100');
}
settings.pointSize = Number(values['point-size']);
if (!Number.isFinite(settings.pointSize) || settings.pointSize < .5 || settings.pointSize > 4) {
  throw new Error('Point size must be between 0.5 and 4 CSS pixels');
}
Object.assign(settings, { elevationDegrees: 48, startAzimuthDegrees: 60, edlStrength: .45,
  fieldOfViewDegrees: 28, frameMargin: .94,
  devicePixelRatio: 1.5, colorMode: 'height', fixedPointSet: true });

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const dataDirectory = resolve(values['data-dir']);
const outputDirectory = resolve(values['output-dir']);
const viewerDirectory = join(root, 'docs/viewer');
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');
const preparationBytes = await readFile(join(dataDirectory, 'preparation.json'));
const preparation = JSON.parse(preparationBytes);
const pointBytes = await readFile(join(dataDirectory, 'points.bin'));
const trajectoryBytes = await readFile(join(dataDirectory, 'trajectory.json'));
if (digest(pointBytes) !== preparation.point_file_sha256
  || pointBytes.length !== preparation.point_count * 16
  || digest(trajectoryBytes) !== preparation.trajectory_sha256) throw new Error('Prepared input checksum mismatch');
await mkdir(outputDirectory); // Refuse existing directories; never overwrite a previous export.

const document = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Outdoor01 rotation</title>
<style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#0a111c;color:#e4eef6;font-family:"Noto Sans CJK SC","Noto Sans",sans-serif}
header{position:absolute;top:28px;left:38px;right:38px;display:flex;justify-content:space-between;align-items:center}
h1{margin:0;font-size:32px;font-weight:600;letter-spacing:1px}h1 span{font-size:20px;color:#a9bac8;font-weight:400;margin-left:14px}
.eyebrow{font-size:12px;letter-spacing:3px;color:#76cdbc;margin-bottom:8px}.metrics{text-align:right;font-size:22px;font-weight:500}.metrics small{display:block;color:#9aafc0;font-size:14px;font-weight:400;margin-top:7px}
#viewport{position:absolute;top:116px;bottom:98px;left:24px;right:24px;border:1px solid #1e3041;border-radius:18px;overflow:hidden}
#viewport canvas{display:block;width:100%;height:100%}
footer{position:absolute;bottom:27px;left:38px;right:38px;display:flex;justify-content:space-between;align-items:center;color:#9fb3c4;font-size:13px}
.legend{display:flex;gap:14px;align-items:center}.bar{width:185px;height:7px;border-radius:5px;background:linear-gradient(90deg,#394baf,#1b99bf 20%,#42c8a4 42%,#dce570 66%,#f5ae65 82%,#e26979)}
.labels{display:flex;justify-content:space-between;margin-top:7px;color:#c2d0db;font-size:12px}.route{display:inline-block;width:24px;height:2px;background:#d9fff2;vertical-align:middle;margin-right:9px}
#rotation{font-variant-numeric:tabular-nums;color:#b7c9d7;min-width:96px;text-align:right}.source{font-size:12px;margin-top:8px;color:#7d94a8}
</style><body>
<header><div><div class="eyebrow">LIDAR / IMU · M3DGR OUTDOOR01</div><h1>FAST-LIO2<span>个人研究版</span></h1></div><div class="metrics"><span id="points"></span><small id="processing"></small></div></header>
<div id="viewport"></div>
<footer><div><span class="route"></span>估计轨迹<div class="source">真实点云 · 离线旋转展示</div></div><div class="legend"><span>高度 / m</span><div><div class="bar"></div><div class="labels"><span>−2</span><span>9</span><span>20+</span></div></div></div><div id="rotation">旋转 000°</div></footer>
</body></html>`;

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const chromiumArgs = ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'];
const browser = await chromium.launch({ headless: true, args: chromiumArgs });
const page = await browser.newPage({ viewport: { width: settings.width, height: settings.height },
  deviceScaleFactor: settings.devicePixelRatio });
const errors = [];
page.on('pageerror', (error) => errors.push(error.message));
const origin = 'http://127.0.0.1:8797';
const virtualFiles = new Map([
  ['/', { body: document, contentType: 'text/html; charset=utf-8' }],
  ['/points.bin', { body: pointBytes, contentType: 'application/octet-stream' }],
  ['/trajectory.json', { body: trajectoryBytes, contentType: 'application/json' }],
]);
await page.route(`${origin}/**`, async (route) => {
  try {
    const path = decodeURIComponent(new URL(route.request().url()).pathname);
    if (virtualFiles.has(path)) return await route.fulfill(virtualFiles.get(path));
    if (path === '/favicon.ico') return await route.fulfill({ status: 204 });
    const file = resolve(viewerDirectory, path.replace(/^\/viewer\//, ''));
    if (!path.startsWith('/viewer/') || !file.startsWith(viewerDirectory + sep)
      || !['.mjs', '.js'].includes(extname(file))) throw new Error(`Unexpected resource: ${path}`);
    await route.fulfill({ body: await readFile(file), contentType: 'text/javascript' });
  } catch (error) {
    errors.push(error.message);
    await route.fulfill({ status: 500, body: 'Rendering resource failed' });
  }
});

const started = new Date().toISOString();
const frameHashes = [];
let rendering;
try {
  await page.goto(origin, { waitUntil: 'load' });
  rendering = await page.evaluate(async ({ preparation, settings }) => {
    const [{ PointCloudRenderer }, pointResponse, trajectoryResponse] = await Promise.all([
      import('/viewer/point-cloud-renderer.mjs'), fetch('/points.bin'), fetch('/trajectory.json'),
    ]);
    const points = new Float32Array(await pointResponse.arrayBuffer());
    const trajectory = (await trajectoryResponse.json()).positions;
    if (points.length !== preparation.point_count * 4) throw new Error('Unexpected point buffer length');
    const renderer = new PointCloudRenderer(document.getElementById('viewport'), {
      onContextLost: () => { throw new Error('WebGL context lost during export'); },
    });
    cancelAnimationFrame(renderer.animationFrame);
    renderer.controls.enabled = false;
    renderer.controls.enableDamping = false;
    renderer.setDataset(preparation, trajectory);
    renderer.addTile(points, { id: 'dense', bounds: preparation.bounds, point_count: preparation.point_count });
    renderer.setPointSize(settings.pointSize);
    renderer.setEdlStrength(settings.edlStrength);
    renderer.showGrid(false);
    renderer.setSpin(false);
    if (!renderer.edlSupported) throw new Error('EDL depth textures are required for this export');
    const center = preparation.bounds.min.map((v, i) => (v + preparation.bounds.max[i]) / 2);
    let heightSum = 0;
    for (let i = 2; i < points.length; i += 4) heightSum += points[i];
    center[2] = heightSum / preparation.point_count;
    const elevation = settings.elevationDegrees * Math.PI / 180;
    const sinElevation = Math.sin(elevation);
    const cosElevation = Math.cos(elevation);
    renderer.camera.fov = settings.fieldOfViewDegrees;
    const tanVertical = Math.tan(renderer.camera.fov * Math.PI / 360) * settings.frameMargin;
    const tanHorizontal = tanVertical * renderer.camera.aspect;
    // Maximize each frustum inequality over all azimuths, for every point.
    // A fixed distance keeps the full cloud in frame throughout the orbit.
    let distance = 0;
    let radius = 0;
    const fitPoint = (x, y, z) => {
      const radial = Math.hypot(x - center[0], y - center[1]);
      const height = z - center[2];
      radius = Math.max(radius, Math.hypot(radial, height));
      distance = Math.max(distance,
        radial * Math.hypot(cosElevation, 1 / tanHorizontal) + height * sinElevation,
        radial * Math.abs(cosElevation - sinElevation / tanVertical) + height * (sinElevation + cosElevation / tanVertical),
        radial * Math.abs(cosElevation + sinElevation / tanVertical) + height * (sinElevation - cosElevation / tanVertical));
    };
    for (let i = 0; i < points.length; i += 4) fitPoint(points[i], points[i + 1], points[i + 2]);
    for (const position of trajectory) fitPoint(...position);
    renderer.camera.near = Math.max(.15, distance - radius * 1.1);
    renderer.camera.far = distance + radius * 1.1;
    renderer.camera.updateProjectionMatrix();
    renderer.postMaterial.uniforms.cameraNear.value = renderer.camera.near;
    renderer.postMaterial.uniforms.cameraFar.value = renderer.camera.far;
    renderer.controls.target.set(...center);
    const canvas = renderer.renderer.domElement;
    const viewport = renderer.container;
    if (canvas.clientWidth !== viewport.clientWidth || canvas.clientHeight !== viewport.clientHeight) {
      throw new Error('Canvas CSS dimensions must match the viewport');
    }
    document.getElementById('points').textContent = `${preparation.point_count.toLocaleString('en-US')} 点`;
    document.getElementById('processing').textContent = `${preparation.voxel_size_m.toFixed(2)} m 体素 · SOR · EDL`;
    window.drawRotationFrame = (index) => {
      const rotation = 360 * index / settings.frames;
      const azimuth = (settings.startAzimuthDegrees + rotation) * Math.PI / 180;
      renderer.camera.position.set(center[0] + distance * Math.cos(elevation) * Math.cos(azimuth),
        center[1] + distance * Math.cos(elevation) * Math.sin(azimuth), center[2] + distance * Math.sin(elevation));
      renderer.camera.lookAt(renderer.controls.target);
      renderer.camera.updateMatrixWorld();
      renderer.render();
      document.getElementById('rotation').textContent = `旋转 ${String(Math.floor(rotation)).padStart(3, '0')}°`;
    };
    const gl = renderer.renderer.getContext();
    const debug = gl.getExtension('WEBGL_debug_renderer_info');
    await document.fonts.ready;
    window.drawRotationFrame(0);
    const loadedPoints = renderer.cloud.children.reduce((sum, cloud) => sum + cloud.geometry.getAttribute('position').count, 0);
    if (loadedPoints !== preparation.point_count) throw new Error('Renderer point count mismatch');
    return { center_m: center, distance_m: distance, near_m: renderer.camera.near, far_m: renderer.camera.far,
      viewport_css_pixels: [viewport.clientWidth, viewport.clientHeight],
      drawing_buffer_pixels: [canvas.width, canvas.height], loaded_points: loadedPoints,
      edl_enabled: renderer.edlEnabled, webgl_renderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER) };
  }, { preparation, settings });
  for (let frame = 0; frame < settings.frames; frame++) {
    await page.evaluate((index) => window.drawRotationFrame(index), frame);
    const path = join(outputDirectory, `frame-${String(frame).padStart(4, '0')}.png`);
    const bytes = await page.screenshot({ path, scale: 'css' });
    frameHashes.push(digest(bytes));
    if (errors.length) throw new Error(errors.join('\n'));
    if (frame % 20 === 0 || frame === settings.frames - 1) console.log(`Rendered ${frame + 1}/${settings.frames} frames`);
  }
} finally {
  await browser.close();
}

const ffmpegCommands = [];
function ffmpeg(args) {
  const command = ['-hide_banner', '-loglevel', 'error', '-n', ...args];
  ffmpegCommands.push(command);
  const result = spawnSync('ffmpeg', command, { encoding: 'utf8' });
  if (result.error || result.status !== 0) throw new Error(result.error?.message || result.stderr || 'FFmpeg failed');
}
const frames = join(outputDirectory, 'frame-%04d.png');
const palette = join(outputDirectory, 'palette.png');
ffmpeg(['-framerate', String(settings.fps), '-i', frames, '-vf', 'palettegen=max_colors=256:reserve_transparent=1', '-frames:v', '1', palette]);
ffmpeg(['-framerate', String(settings.fps), '-i', frames, '-i', palette,
  '-lavfi', 'paletteuse=dither=none:diff_mode=rectangle', '-loop', '0', join(outputDirectory, 'outdoor01_rotation.gif')]);
ffmpeg(['-framerate', String(settings.fps), '-i', frames, '-c:v', 'libx264', '-preset', 'slow',
  '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', join(outputDirectory, 'outdoor01_rotation.mp4')]);
await copyFile(join(outputDirectory, 'frame-0000.png'), join(outputDirectory, 'outdoor01_rotation.png'));
const outputs = {};
for (const name of ['outdoor01_rotation.gif', 'outdoor01_rotation.mp4', 'outdoor01_rotation.png']) {
  const bytes = await readFile(join(outputDirectory, name));
  outputs[name] = { size_bytes: bytes.length, sha256: digest(bytes) };
}
const sourceHashes = {};
for (const name of ['scripts/render_map_rotation.mjs', 'docs/viewer/point-cloud-renderer.mjs',
  'docs/viewer/vendor/three.module.min.js', 'docs/viewer/vendor/OrbitControls.js']) {
  sourceHashes[name] = digest(await readFile(join(root, name)));
}
const report = { schema_version: 1, started_utc: started, finished_utc: new Date().toISOString(),
  preparation, preparation_sha256: digest(preparationBytes), settings, rendering,
  duration_seconds: settings.frames / settings.fps, loop: '360 degrees; no repeated endpoint frame; infinite GIF loop',
  frames_sha256: frameHashes, outputs, source_sha256: sourceHashes,
  chromium_version: browser.version(), chromium_args: chromiumArgs, ffmpeg_commands: ffmpegCommands,
  scope: 'Offline visualization using fixed points and estimated poses; export fps is not online SLAM performance.' };
await writeFile(join(outputDirectory, 'rendering.json'), JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify({ outputs, rendering, duration_seconds: report.duration_seconds }, null, 2));
