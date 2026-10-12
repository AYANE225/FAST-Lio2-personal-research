// Export real chronological map growth using the existing Three.js / EDL renderer.
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {copyFile, mkdir, readFile, writeFile} from 'node:fs/promises';
import {dirname, extname, join, resolve, sep} from 'node:path';
import {fileURLToPath} from 'node:url';
import {parseArgs} from 'node:util';
import {renderSchedule, validateTimeline} from './mapping_progress.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const {values} = parseArgs({options: {
  'data-dir': {type: 'string'}, 'output-dir': {type: 'string'},
  width: {type: 'string', default: '1280'}, height: {type: 'string', default: '900'},
  frames: {type: 'string', default: '240'}, 'hold-frames': {type: 'string', default: '40'},
  fps: {type: 'string', default: '20'}, help: {type: 'boolean', default: false},
}});
if (values.help) {
  console.log('node scripts/render_mapping_progress.mjs --data-dir output/mapping-progress-data --output-dir output/mapping-progress [--width 720 --height 940]');
  process.exit(0);
}
if (!values['data-dir'] || !values['output-dir']) throw new Error('--data-dir and --output-dir are required');
const settings = Object.fromEntries(['width', 'height', 'frames', 'hold-frames', 'fps'].map(key => [key, Number(values[key])]));
if (Object.entries(settings).some(([key, value]) => !Number.isSafeInteger(value) || value < (key === 'hold-frames' ? 0 : 1))
  || settings.width < 600 || settings.height < 700 || settings.width % 2 || settings.height % 2
  || settings.width > 2400 || settings.height > 2400 || settings.frames < 2 || 100 % settings.fps) {
  throw new Error('Use even dimensions between 600×700 and 2400×2400, >=2 frames, and an fps that divides 100');
}
Object.assign(settings, {azimuth_degrees: 110, elevation_degrees: 48, field_of_view_degrees: 28,
  frame_margin: .94, point_size_css_px: 1.65, edl_strength: .45, device_pixel_ratio: 1.5});
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const dataDirectory = resolve(values['data-dir']), outputDirectory = resolve(values['output-dir']);
const preparationBytes = await readFile(join(dataDirectory, 'preparation.json'));
const preparation = JSON.parse(preparationBytes);
const pointBytes = await readFile(join(dataDirectory, 'points.bin'));
const timelineBytes = await readFile(join(dataDirectory, 'timeline.json'));
for (const [name, bytes] of [['points.bin', pointBytes], ['timeline.json', timelineBytes]]) {
  if (digest(bytes) !== preparation.outputs[name]?.sha256 || bytes.length !== preparation.outputs[name]?.size_bytes) {
    throw new Error(`Prepared input checksum mismatch: ${name}`);
  }
}
if (pointBytes.length !== preparation.point_count * 16) throw new Error('Point count differs from the prepared buffer');
const timeline = JSON.parse(timelineBytes);
validateTimeline(preparation, timeline);
const schedule = renderSchedule(timeline, settings.frames, settings['hold-frames']);
await mkdir(outputDirectory); // Preserve earlier exports.

const document = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Outdoor01 mapping progress</title>
<style>
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#0d0d1c;color:#e7eff5;font-family:"Noto Sans CJK SC",sans-serif}
header{position:absolute;top:27px;left:36px;right:36px;display:flex;justify-content:space-between;align-items:center}.kicker{font-size:15px;color:#7fd8cd;letter-spacing:2px;margin-bottom:8px}h1{font-size:38px;line-height:1.25;margin:0;font-weight:700}.run-note{text-align:right;color:#9ab0c1;font-size:19px;line-height:1.8}.run-note b{color:#e7eff5;font-size:23px;font-weight:500}
#viewport{position:absolute;left:28px;right:28px;top:125px;bottom:240px;border:1px solid #263748;border-radius:18px;overflow:hidden}canvas{display:block;width:100%;height:100%}.map-label{position:absolute;top:18px;left:20px;font-size:14px;color:#9ab0c1;z-index:2;pointer-events:none;background:#0d0d1cd9;padding:7px 12px;border-radius:7px}.map-label span{color:#ffd38b;margin-right:8px}
footer{position:absolute;bottom:24px;left:36px;right:36px}.progress-label{display:flex;justify-content:space-between;font-size:17px;color:#9ab0c1;margin-bottom:11px}#status{color:#7fd8cd}.progress{height:5px;border-radius:4px;background:#263748;overflow:hidden}.progress div{height:100%;width:0;background:#7fd8cd}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;margin-top:22px}.stat{border-left:1px solid #263748;padding-left:20px}.stat:first-child{border:0;padding-left:0}.stat small{font-size:16px;color:#9ab0c1;display:block;margin-bottom:7px}.stat strong{font-size:31px;font-weight:500;font-variant-numeric:tabular-nums}.stat strong span{color:#9ab0c1;font-size:19px}.legend{display:flex;align-items:center;justify-content:space-between;margin-top:23px;font-size:14px;color:#9ab0c1}.height-legend{display:flex;gap:9px;align-items:center}.colorbar{width:135px;height:6px;background:linear-gradient(90deg,#394baf,#1b99bf 20%,#42c8a4 42%,#dce570 66%,#f5ae65 82%,#e26979);border-radius:4px}
.sampling .processing::before{content:' · '}
@media(max-width:800px){header{left:28px;right:28px;top:26px}.kicker{font-size:19px;letter-spacing:1px}h1{font-size:40px}.run-note{display:none}#viewport{left:20px;right:20px;top:137px;bottom:350px}.map-label{font-size:17px;top:12px;left:12px}footer{left:28px;right:28px;bottom:26px}.progress-label{font-size:21px}.stats{grid-template-columns:1fr 1fr;gap:20px;margin-top:20px}.stat{padding-left:18px}.stat:nth-child(3){border:0;padding-left:0}.stat small{font-size:21px;margin-bottom:3px}.stat strong{font-size:33px}.stat strong span{font-size:21px}.legend{font-size:18px;margin-top:22px;gap:14px}.height-legend{gap:7px}.colorbar{width:86px}.legend .sampling{max-width:310px}.sampling .processing{display:block;margin-top:4px}.sampling .processing::before{content:''}}
</style><body>
<header><div><div class="kicker">01 / MID-360 · OUTDOOR01 · 离线回放</div><h1>真实建图过程</h1></div><div class="run-note"><b>逐帧点云 + 估计轨迹</b><br>410.8 s 记录 · 离线缩时回放</div></header>
<div id="viewport"><div class="map-label"><span>●</span>当前估计位置<span id="view-range"></span></div></div>
<footer><div class="progress-label"><span id="status">按采集时间回放</span><span id="percent">0%</span></div><div class="progress"><div id="bar"></div></div>
<div class="stats"><div class="stat"><small>记录帧 / <span id="total-frames"></span></small><strong id="frames"></strong></div><div class="stat"><small>累计观测 / 点</small><strong id="recorded"></strong></div><div class="stat"><small>显示点 / 点</small><strong id="display"></strong></div><div class="stat"><small>采集时间 / s</small><strong id="time"></strong></div></div>
<div class="legend"><span class="sampling" id="sampling"></span><span class="height-legend">高度 −2<div class="colorbar"></div>20+ m</span></div></footer>
</body></html>`;

const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const chromiumArgs = ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'];
const browser = await chromium.launch({headless: true, args: chromiumArgs});
const page = await browser.newPage({viewport: {width: settings.width, height: settings.height}, deviceScaleFactor: settings.device_pixel_ratio});
const origin = 'http://127.0.0.1:8799', viewerDirectory = join(root, 'docs/viewer');
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const virtual = new Map([
  ['/', {body: document, contentType: 'text/html; charset=utf-8'}],
  ['/points.bin', {body: pointBytes, contentType: 'application/octet-stream'}],
]);
await page.route('**/*', async route => {
  try {
    const url = new URL(route.request().url()), path = decodeURIComponent(url.pathname);
    if (url.origin !== origin) throw new Error('Only local resources are allowed during export');
    if (virtual.has(path)) return await route.fulfill(virtual.get(path));
    if (path === '/favicon.ico') return await route.fulfill({status: 204});
    const file = resolve(viewerDirectory, path.replace(/^\/viewer\//, ''));
    if (!path.startsWith('/viewer/') || !file.startsWith(viewerDirectory + sep)
      || !['.mjs', '.js'].includes(extname(file))) throw new Error(`Unexpected resource: ${path}`);
    await route.fulfill({body: await readFile(file), contentType: 'text/javascript'});
  } catch (error) {
    errors.push(error.message);
    await route.fulfill({status: 500, body: 'Local rendering resource failed'});
  }
});
const started = new Date().toISOString(), frameHashes = [], sampled = [];
let graphics;
try {
  await page.goto(origin, {waitUntil: 'load'});
  graphics = await page.evaluate(async ({preparation, timeline, settings}) => {
    const [{PointCloudRenderer}, THREE, response] = await Promise.all([
      import('/viewer/point-cloud-renderer.mjs'), import('/viewer/vendor/three.module.min.js'), fetch('/points.bin'),
    ]);
    const points = new Float32Array(await response.arrayBuffer());
    if (!points.every(Number.isFinite)) throw new Error('Nonfinite point buffer');
    const renderer = new PointCloudRenderer(document.getElementById('viewport'), {
      onContextLost: () => {throw new Error('WebGL context lost during export');},
    });
    cancelAnimationFrame(renderer.animationFrame);
    renderer.controls.enabled = false;
    renderer.controls.enableDamping = false;
    renderer.setDataset(preparation, timeline.positions);
    renderer.setSpin(false);
    renderer.showGrid(false);
    renderer.setPointSize(settings.point_size_css_px);
    renderer.setEdlStrength(settings.edl_strength);
    if (!renderer.edlSupported) throw new Error('EDL requires depth textures');
    const cloud = renderer.addTile(points, {id: 'progress', bounds: preparation.bounds, point_count: preparation.point_count});
    const route = renderer.trajectory.children.find(child => child.isLine);
    route.material.color.set('#ffd38b');
    route.material.opacity = 1;
    route.material.depthTest = false;
    route.renderOrder = 4;
    const marker = new THREE.Mesh(new THREE.SphereGeometry(1.15, 16, 10), new THREE.MeshBasicMaterial({color: '#ffd38b', depthTest: false}));
    marker.renderOrder = 5;
    renderer.scene.add(marker);
    const ring = new THREE.Mesh(new THREE.RingGeometry(2.6, 3.2, 36), new THREE.MeshBasicMaterial({color: '#ffd38b', side: THREE.DoubleSide, transparent: true, opacity: .85, depthTest: false}));
    ring.renderOrder = 5;
    renderer.scene.add(ring);
    const azimuth = settings.azimuth_degrees * Math.PI / 180, elevation = settings.elevation_degrees * Math.PI / 180;
    const d = [Math.cos(elevation) * Math.cos(azimuth), Math.cos(elevation) * Math.sin(azimuth), Math.sin(elevation)];
    const r = [-Math.sin(azimuth), Math.cos(azimuth), 0];
    const u = [-Math.sin(elevation) * Math.cos(azimuth), -Math.sin(elevation) * Math.sin(azimuth), Math.cos(elevation)];
    const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    const visit = callback => {
      for (let i = 0; i < points.length; i += 4) callback([points[i], points[i + 1], points[i + 2]]);
      timeline.positions.forEach(callback);
    };
    const ranges = [r, u, d].map(axis => {
      let min = Infinity, max = -Infinity;
      visit(point => {const value = dot(point, axis); min = Math.min(min, value); max = Math.max(max, value);});
      return [min, max];
    });
    const center = [0, 1, 2].map(i => [r, u, d].reduce((sum, axis, j) => sum + axis[i] * (ranges[j][0] + ranges[j][1]) / 2, 0));
    renderer.camera.fov = settings.field_of_view_degrees;
    const tanV = Math.tan(renderer.camera.fov * Math.PI / 360), tanH = tanV * renderer.camera.aspect;
    let distance = 0, depthMin = Infinity, depthMax = -Infinity;
    visit(point => {
      const relative = point.map((value, i) => value - center[i]), depth = dot(relative, d);
      depthMin = Math.min(depthMin, depth); depthMax = Math.max(depthMax, depth);
      distance = Math.max(distance, depth + Math.abs(dot(relative, r)) / (tanH * settings.frame_margin),
        depth + Math.abs(dot(relative, u)) / (tanV * settings.frame_margin));
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
    document.getElementById('total-frames').textContent = preparation.frame_count.toLocaleString('en-US');
    document.getElementById('sampling').innerHTML = `<span>${preparation.voxel_size_m.toFixed(2)} m 体素 · 首次观测点</span><span class="processing">${preparation.statistical_filter ? '全程离线 SOR · EDL' : 'EDL'}</span>`;
    document.getElementById('view-range').textContent = preparation.height_range_m
      ? ` · 局部高度 ${preparation.height_range_m[0]}～${preparation.height_range_m[1]} m` : ' · 固定视角';
    await document.fonts.ready;
    window.drawProgress = index => {
      const count = timeline.display_point_prefix_counts[index], time = timeline.elapsed_s[index];
      cloud.geometry.setDrawRange(0, count);
      route.geometry.setDrawRange(0, index + 1);
      marker.position.set(...timeline.positions[index]);
      ring.position.copy(marker.position);
      renderer.render();
      const percent = time / preparation.source_duration_s * 100;
      document.getElementById('frames').textContent = (index + 1).toLocaleString('en-US');
      document.getElementById('recorded').textContent = timeline.recorded_point_prefix_counts[index].toLocaleString('en-US');
      document.getElementById('display').textContent = count.toLocaleString('en-US');
      document.getElementById('time').innerHTML = `${time.toFixed(1)} <span>/ ${preparation.source_duration_s.toFixed(1)}</span>`;
      document.getElementById('percent').textContent = `${percent.toFixed(1)}%`;
      document.getElementById('bar').style.width = `${percent}%`;
      document.getElementById('status').textContent = index === preparation.frame_count - 1 ? '建图记录回放完成' : '按采集时间回放';
      if (cloud.geometry.drawRange.count !== count || route.geometry.drawRange.count !== index + 1) throw new Error('Incorrect visible prefix');
      return {recorded_frame_index: index, elapsed_s: time, display_points: count, trajectory_positions: index + 1};
    };
    const gl = renderer.renderer.getContext(), debug = gl.getExtension('WEBGL_debug_renderer_info');
    return {renderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
      edl: true, camera_position_m: renderer.camera.position.toArray(), center_m: center,
      near_m: renderer.camera.near, far_m: renderer.camera.far,
      viewport_css_pixels: [renderer.container.clientWidth, renderer.container.clientHeight],
      drawing_buffer_pixels: [renderer.renderer.domElement.width, renderer.renderer.domElement.height]};
  }, {preparation, timeline, settings});
  for (let i = 0; i < schedule.length; i++) {
    const path = join(outputDirectory, `frame-${String(i).padStart(4, '0')}.png`);
    if (schedule[i].hold) {
      await copyFile(join(outputDirectory, `frame-${String(settings.frames - 1).padStart(4, '0')}.png`), path);
      frameHashes.push(frameHashes[settings.frames - 1]);
      sampled.push(sampled[settings.frames - 1]);
    } else {
      sampled.push(await page.evaluate(index => window.drawProgress(index), schedule[i].frame_index));
      const bytes = await page.screenshot({path, scale: 'css'});
      frameHashes.push(digest(bytes));
      if (errors.length) throw new Error(errors.join('\n'));
      if (i % 40 === 0 || i === settings.frames - 1) console.log(`Rendered ${i + 1}/${settings.frames} progression frames`);
    }
  }
  const layout = await page.evaluate(() => {
    const elements = [...document.querySelectorAll('header,footer,.stat,.legend')];
    return elements.map(element => {const r = element.getBoundingClientRect(); return {element: element.className || element.tagName,
      x: r.x, y: r.y, width: r.width, height: r.height, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth};});
  });
  if (layout.some(r => r.x < 0 || r.y < 0 || r.x + r.width > settings.width + 1
    || r.y + r.height > settings.height + 1 || r.scrollWidth > r.clientWidth + 1)) throw new Error('Text layout overflows the export');
  graphics.layout = layout;
} finally {
  await browser.close();
}
const commands = [];
function ffmpeg(args) {
  const command = ['-hide_banner', '-loglevel', 'error', '-n', ...args];
  commands.push(command.map(argument => argument.replaceAll(outputDirectory, '<output-dir>')));
  const result = spawnSync('ffmpeg', command, {encoding: 'utf8'});
  if (result.error || result.status !== 0) throw new Error(result.error?.message || result.stderr || 'FFmpeg failed');
}
const framesPath = join(outputDirectory, 'frame-%04d.png'), palette = join(outputDirectory, 'palette.png');
ffmpeg(['-framerate', String(settings.fps), '-i', framesPath, '-vf', 'palettegen=max_colors=256:reserve_transparent=1', '-frames:v', '1', palette]);
ffmpeg(['-framerate', String(settings.fps), '-i', framesPath, '-i', palette,
  '-lavfi', 'paletteuse=dither=none:diff_mode=rectangle', '-loop', '0', join(outputDirectory, 'outdoor01_mapping.gif')]);
ffmpeg(['-framerate', String(settings.fps), '-i', framesPath, '-c:v', 'libx264', '-preset', 'slow', '-crf', '17',
  '-pix_fmt', 'yuv420p', '-movflags', '+faststart', join(outputDirectory, 'outdoor01_mapping.mp4')]);
await copyFile(join(outputDirectory, `frame-${String(settings.frames - 1).padStart(4, '0')}.png`), join(outputDirectory, 'outdoor01_mapping_poster.png'));
const outputs = {};
for (const name of ['outdoor01_mapping.gif', 'outdoor01_mapping.mp4', 'outdoor01_mapping_poster.png']) {
  const bytes = await readFile(join(outputDirectory, name));
  outputs[name] = {size_bytes: bytes.length, sha256: digest(bytes)};
}
const sources = {};
for (const name of ['scripts/render_mapping_progress.mjs', 'scripts/mapping_progress.mjs', 'docs/viewer/point-cloud-renderer.mjs',
  'docs/viewer/vendor/three.module.min.js', 'docs/viewer/vendor/OrbitControls.js']) sources[name] = digest(await readFile(join(root, name)));
const report = {schema_version: 1, started_utc: started, finished_utc: new Date().toISOString(),
  preparation_sha256: digest(preparationBytes), point_file_sha256: digest(pointBytes), timeline_sha256: digest(timelineBytes),
  settings, graphics, source_duration_s: preparation.source_duration_s,
  duration_seconds: schedule.length / settings.fps, progression_frames: settings.frames, hold_frames: settings['hold-frames'],
  frame_schedule: schedule.map((row, i) => ({...row, ...sampled[i]})), frames_sha256: frameHashes, outputs,
  source_sha256: sources, chromium_version: browser.version(), chromium_args: chromiumArgs, ffmpeg_commands: commands,
  scope: 'Recorded scan-end prefixes. Fixed camera fits the complete scene; geometry and trajectory reveal only past observations. Optional full-recording SOR is an offline display selection, not causal online filtering. Offline export fps is not SLAM throughput.'};
await writeFile(join(outputDirectory, 'rendering.json'), JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify({outputs, duration_seconds: report.duration_seconds}, null, 2));
