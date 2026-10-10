import * as THREE from './vendor/three.module.min.js';
import { OrbitControls } from './vendor/OrbitControls.js';

export const COLOR_MAPS = Object.freeze({
  height: [
    [0, '#394baf'], [0.2, '#1b99bf'], [0.42, '#42c8a4'],
    [0.66, '#dce570'], [0.82, '#f5ae65'], [1, '#e26979'],
  ],
  intensity: [
    [0, '#18364d'], [0.25, '#316575'], [0.5, '#68a08d'],
    [0.75, '#c1c98c'], [1, '#ffe0a4'],
  ],
});

function paletteTexture(stops) {
  const bytes = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let right = stops.findIndex(([offset]) => offset >= t);
    if (right <= 0) right = 1;
    const [leftOffset, leftHex] = stops[right - 1];
    const [rightOffset, rightHex] = stops[right];
    const mix = (t - leftOffset) / (rightOffset - leftOffset);
    const left = parseInt(leftHex.slice(1), 16);
    const rightColor = parseInt(rightHex.slice(1), 16);
    for (let channel = 0; channel < 3; channel++) {
      const shift = (2 - channel) * 8;
      bytes[i * 4 + channel] = Math.round(((left >> shift) & 255) * (1 - mix) + ((rightColor >> shift) & 255) * mix);
    }
    bytes[i * 4 + 3] = 255;
  }
  const texture = new THREE.DataTexture(bytes, 256, 1, THREE.RGBAFormat);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.minFilter = THREE.LinearFilter;
  texture.magFilter = THREE.LinearFilter;
  texture.needsUpdate = true;
  return texture;
}

const pointVertexShader = /* glsl */ `
  attribute float intensity;
  uniform float pointSize;
  uniform float pixelRatio;
  uniform float maxPointSize;
  uniform int colorMode;
  uniform vec2 colorRange;
  varying float colorValue;
  void main() {
    vec4 viewPosition = modelViewMatrix * vec4(position, 1.0);
    float value = colorMode == 0 ? position.z : intensity;
    colorValue = clamp((value - colorRange.x) / (colorRange.y - colorRange.x), 0.0, 1.0);
    gl_Position = projectionMatrix * viewPosition;
    gl_PointSize = min(maxPointSize, pointSize * pixelRatio);
  }
`;

const pointFragmentShader = /* glsl */ `
  uniform int colorMode;
  uniform sampler2D palette;
  uniform vec3 monochrome;
  varying float colorValue;
  void main() {
    vec2 pointOffset = gl_PointCoord - vec2(0.5);
    if (dot(pointOffset, pointOffset) > 0.25) discard;
    vec3 color = colorMode == 2 ? monochrome : texture2D(palette, vec2(colorValue, 0.5)).rgb;
    gl_FragColor = vec4(color, 1.0);
    #include <colorspace_fragment>
  }
`;

// Eye-Dome Lighting: compare reconstructed logarithmic view depths at eight
// neighbours. This uses actual point depth, without normals or a light source.
const edlFragmentShader = /* glsl */ `
  uniform sampler2D sceneColor;
  uniform sampler2D sceneDepth;
  uniform vec2 resolution;
  uniform float cameraNear;
  uniform float cameraFar;
  uniform float radius;
  uniform float strength;
  varying vec2 uvPosition;
  float viewDepth(float depth) {
    return (cameraNear * cameraFar) / (cameraFar - depth * (cameraFar - cameraNear));
  }
  float difference(vec2 direction, float centerDepth) {
    float depth = texture2D(sceneDepth, uvPosition + direction * radius / resolution).r;
    if (depth >= 0.9999999) return 0.0;
    return max(0.0, centerDepth - log2(max(viewDepth(depth), cameraNear)));
  }
  void main() {
    vec4 color = texture2D(sceneColor, uvPosition);
    float depth = texture2D(sceneDepth, uvPosition).r;
    float shade = 1.0;
    if (strength > 0.0 && depth < 0.9999999) {
      float centerDepth = log2(max(viewDepth(depth), cameraNear));
      float response = difference(vec2(1.0, 0.0), centerDepth)
        + difference(vec2(-1.0, 0.0), centerDepth)
        + difference(vec2(0.0, 1.0), centerDepth)
        + difference(vec2(0.0, -1.0), centerDepth)
        + difference(vec2(0.7071, 0.7071), centerDepth)
        + difference(vec2(-0.7071, 0.7071), centerDepth)
        + difference(vec2(0.7071, -0.7071), centerDepth)
        + difference(vec2(-0.7071, -0.7071), centerDepth);
      shade = exp(-response * strength * 150.0 / 8.0);
    }
    gl_FragColor = vec4(color.rgb * shade, color.a);
    #include <colorspace_fragment>
  }
`;

export class PointCloudRenderer {
  constructor(container, { onCameraChange = () => {}, onFrame = () => {}, onContextLost = () => {} } = {}) {
    this.container = container;
    this.onCameraChange = onCameraChange;
    this.onFrame = onFrame;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: true, powerPreference: 'high-performance' });
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.setClearColor('#0a111c', 1);
    this.renderer.domElement.setAttribute('aria-label', '可拖动、缩放的三维激光点云地图');
    this.renderer.domElement.setAttribute('role', 'img');
    container.appendChild(this.renderer.domElement);
    this.renderer.domElement.addEventListener('webglcontextlost', (event) => {
      event.preventDefault();
      this.contextLost = true;
      onContextLost();
    });
    this.scene = new THREE.Scene();
    this.cloud = new THREE.Group();
    this.scene.add(this.cloud);
    this.camera = new THREE.PerspectiveCamera(46, 1, 0.15, 2400);
    this.camera.up.set(0, 0, 1);
    this.camera.position.set(-80, -150, 140);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.085;
    this.controls.screenSpacePanning = false;
    this.controls.minDistance = 4;
    this.controls.maxDistance = 1500;
    this.controls.maxPolarAngle = Math.PI * 0.49;
    this.controls.minPolarAngle = 0.018;
    this.controls.autoRotateSpeed = 0.38;
    this.controls.addEventListener('change', () => {
      this.needsRender = true;
      this.onCameraChange();
    });
    this.controls.addEventListener('start', () => { this.cameraAnimation = null; });
    this.textures = Object.fromEntries(Object.entries(COLOR_MAPS).map(([name, stops]) => [name, paletteTexture(stops)]));
    this.mode = 'height';
    this.colorRanges = { height: [-2, 20], intensity: [0, 106] };
    const gl = this.renderer.getContext();
    const pointRange = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE);
    this.pointMaterial = new THREE.ShaderMaterial({
      uniforms: {
        pointSize: { value: 2 }, pixelRatio: { value: 1 }, maxPointSize: { value: pointRange[1] },
        colorMode: { value: 0 }, colorRange: { value: new THREE.Vector2(-2, 20) },
        palette: { value: this.textures.height }, monochrome: { value: new THREE.Color('#c7dee9') },
      },
      vertexShader: pointVertexShader, fragmentShader: pointFragmentShader,
      depthTest: true, depthWrite: true,
    });
    this.grid = new THREE.GridHelper(600, 60, '#294055', '#1d2e41');
    this.grid.rotation.x = Math.PI / 2;
    this.grid.position.z = -2.1;
    this.grid.material.transparent = true;
    this.grid.material.opacity = 0.24;
    this.grid.material.depthWrite = false;
    this.grid.visible = false;
    this.scene.add(this.grid);
    this.edlSupported = this.renderer.capabilities.isWebGL2 || this.renderer.extensions.has('WEBGL_depth_texture');
    this.edlEnabled = this.edlSupported;
    this.edlStrength = 0.4;
    this.target = new THREE.WebGLRenderTarget(1, 1, {
      minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter,
      depthBuffer: true, stencilBuffer: false,
    });
    if (this.edlSupported) this.target.depthTexture = new THREE.DepthTexture(1, 1, THREE.UnsignedIntType);
    this.postMaterial = new THREE.ShaderMaterial({
      uniforms: {
        sceneColor: { value: this.target.texture }, sceneDepth: { value: this.target.depthTexture },
        resolution: { value: new THREE.Vector2(1, 1) }, cameraNear: { value: this.camera.near }, cameraFar: { value: this.camera.far },
        radius: { value: 2.1 }, strength: { value: this.edlEnabled ? this.edlStrength : 0 },
      },
      vertexShader: 'varying vec2 uvPosition; void main() { uvPosition = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }',
      fragmentShader: edlFragmentShader, depthTest: false, depthWrite: false,
    });
    this.postScene = new THREE.Scene();
    this.postQuad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.postMaterial);
    this.postQuad.frustumCulled = false;
    this.postScene.add(this.postQuad);
    this.postCamera = new THREE.Camera();
    this.frustum = new THREE.Frustum();
    this.projectionView = new THREE.Matrix4();
    this.frameBounds = { min: [-80, -100, -5], max: [210, 130, 98] };
    this.focus = new THREE.Vector3(65, 12, 6);
    this.focusSpan = 165;
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
    this.needsRender = true;
    this.lastFrameTime = performance.now();
    this.frame = this.frame.bind(this);
    this.animationFrame = requestAnimationFrame(this.frame);
  }

  setDataset(manifest, trajectory = []) {
    this.frameBounds = manifest.bounds;
    this.colorRanges = manifest.color_ranges;
    const { min, max } = manifest.bounds;
    this.focus.set((min[0] + max[0]) / 2, (min[1] + max[1]) / 2,
      (this.colorRanges.height[0] + this.colorRanges.height[1]) / 2 - 3);
    this.focusSpan = Math.min(190, Math.max(100, (max[0] - min[0]) * 0.6));
    this.grid.position.z = this.colorRanges.height[0] - 0.15;
    if (trajectory.length) {
      const box = new THREE.Box3().setFromPoints(trajectory.map((point) => new THREE.Vector3(...point)));
      box.getCenter(this.focus);
      this.focus.z += 4;
      const size = box.getSize(new THREE.Vector3());
      this.focusSpan = Math.max(110, Math.min(205, Math.max(size.x, size.y) * 1.14));
      this.detailFocus = new THREE.Vector3(...trajectory[Math.floor(trajectory.length * 0.6)]);
      this.detailFocus.z += 4;
    } else {
      this.detailFocus = this.focus.clone();
    }
    this.setColorMode(this.mode);
    this.setTrajectory(trajectory);
    this.setView('oblique', false);
  }

  addTile(values, tile) {
    const geometry = new THREE.BufferGeometry();
    const buffer = new THREE.InterleavedBuffer(values, 4);
    geometry.setAttribute('position', new THREE.InterleavedBufferAttribute(buffer, 3, 0));
    geometry.setAttribute('intensity', new THREE.InterleavedBufferAttribute(buffer, 1, 3));
    geometry.boundingBox = new THREE.Box3(new THREE.Vector3(...tile.bounds.min), new THREE.Vector3(...tile.bounds.max));
    geometry.boundingSphere = geometry.boundingBox.getBoundingSphere(new THREE.Sphere());
    const points = new THREE.Points(geometry, this.pointMaterial);
    points.userData.tile = tile;
    this.cloud.add(points);
    this.needsRender = true;
    return points;
  }

  removeTile(points) {
    this.cloud.remove(points);
    points.geometry.dispose();
    this.needsRender = true;
  }

  setColorMode(mode) {
    if (!['height', 'intensity', 'monochrome'].includes(mode)) return;
    this.mode = mode;
    const uniforms = this.pointMaterial.uniforms;
    uniforms.colorMode.value = { height: 0, intensity: 1, monochrome: 2 }[mode];
    const range = this.colorRanges[mode] ?? this.colorRanges.height;
    uniforms.colorRange.value.set(...range);
    uniforms.palette.value = this.textures[mode] ?? this.textures.height;
    this.needsRender = true;
  }

  setPointSize(size) {
    this.pointMaterial.uniforms.pointSize.value = size;
    this.needsRender = true;
  }

  setEdl(enabled) {
    this.edlEnabled = Boolean(enabled && this.edlSupported);
    this.postMaterial.uniforms.strength.value = this.edlEnabled ? this.edlStrength : 0;
    this.needsRender = true;
  }

  setEdlStrength(value) {
    if (!Number.isFinite(value)) return;
    this.edlStrength = Math.min(1, Math.max(0, value));
    this.setEdl(this.edlEnabled);
  }

  setTrajectory(positions) {
    if (this.trajectory) {
      this.scene.remove(this.trajectory);
      this.trajectory.traverse((object) => {
        object.geometry?.dispose();
        object.material?.dispose();
      });
    }
    this.trajectory = new THREE.Group();
    if (positions.length > 1) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions.flat(), 3));
      const material = new THREE.LineBasicMaterial({ color: '#d9fff2', transparent: true, opacity: 0.88, depthWrite: false });
      const line = new THREE.Line(geometry, material);
      line.renderOrder = 1;
      this.trajectory.add(line);
      const start = new THREE.Mesh(new THREE.SphereGeometry(0.65, 12, 8), new THREE.MeshBasicMaterial({ color: '#77e9c2' }));
      start.position.set(...positions[0]);
      this.trajectory.add(start);
    }
    this.scene.add(this.trajectory);
    this.needsRender = true;
  }

  showTrajectory(enabled) {
    if (this.trajectory) this.trajectory.visible = enabled;
    this.needsRender = true;
  }

  showGrid(enabled) {
    this.grid.visible = enabled;
    this.needsRender = true;
  }

  setSpin(enabled) {
    this.controls.autoRotate = enabled;
    this.needsRender = true;
  }

  setView(name, animate = true) {
    const target = this.focus.clone();
    const aspectAdjustment = Math.max(1, 1 / this.camera.aspect);
    let distance = this.focusSpan * 1.42 * aspectAdjustment;
    const direction = new THREE.Vector3(-0.58, -1, 0.86).normalize();
    if (name === 'top') {
      direction.set(0, -0.018, 1).normalize();
      distance = this.focusSpan * 1.35 * aspectAdjustment;
    } else if (name === 'detail') {
      target.copy(this.detailFocus ?? this.focus);
      distance = 70 * aspectAdjustment;
      direction.set(-0.55, -1, 0.56).normalize();
    } else if (name === 'full') {
      const box = new THREE.Box3(new THREE.Vector3(...this.frameBounds.min), new THREE.Vector3(...this.frameBounds.max));
      const sphere = box.getBoundingSphere(new THREE.Sphere());
      target.copy(sphere.center);
      distance = sphere.radius / Math.sin(THREE.MathUtils.degToRad(this.camera.fov / 2)) * 1.08 * aspectAdjustment;
    }
    const position = target.clone().addScaledVector(direction, distance);
    this.controls.autoRotate = false;
    if (!animate || matchMedia('(prefers-reduced-motion: reduce)').matches) {
      this.camera.position.copy(position);
      this.controls.target.copy(target);
      this.controls.update();
      this.needsRender = true;
      return;
    }
    this.cameraAnimation = {
      start: performance.now(), duration: 650,
      fromPosition: this.camera.position.clone(), fromTarget: this.controls.target.clone(),
      position, target,
    };
  }

  prepareSelection() {
    this.camera.updateMatrixWorld();
    this.projectionView.multiplyMatrices(this.camera.projectionMatrix, this.camera.matrixWorldInverse);
    this.frustum.setFromProjectionMatrix(this.projectionView);
    const height = this.container.clientHeight;
    const focalLength = height / (2 * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2)));
    return (bounds) => {
      const box = new THREE.Box3(new THREE.Vector3(...bounds.min), new THREE.Vector3(...bounds.max));
      const center = box.getCenter(new THREE.Vector3());
      const radius = box.getSize(new THREE.Vector3()).length() / 2;
      const distance = Math.max(4, this.camera.position.distanceTo(center) - radius * 0.3);
      const pixelScale = focalLength / distance;
      const screen = center.clone().project(this.camera);
      const centrality = 1 / (1 + (screen.x * screen.x + screen.y * screen.y) * 0.3);
      return { visible: this.frustum.intersectsBox(box), pixelScale, priority: Math.max(1, radius * radius * pixelScale * pixelScale) * centrality };
    };
  }

  getAxes() {
    const inverse = this.camera.quaternion.clone().invert();
    return [new THREE.Vector3(1, 0, 0), new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, 0, 1)]
      .map((axis) => axis.applyQuaternion(inverse));
  }

  resize() {
    const width = Math.max(1, this.container.clientWidth);
    const height = Math.max(1, this.container.clientHeight);
    const ratio = Math.min(devicePixelRatio || 1, width < 700 ? 1.5 : 1.75);
    this.renderer.setPixelRatio(ratio);
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    const drawSize = this.renderer.getDrawingBufferSize(new THREE.Vector2());
    this.target.setSize(drawSize.x, drawSize.y);
    this.postMaterial.uniforms.resolution.value.copy(drawSize);
    this.postMaterial.uniforms.radius.value = 1.65 * ratio;
    this.pointMaterial.uniforms.pixelRatio.value = ratio;
    this.needsRender = true;
    this.onCameraChange();
  }

  render() {
    if (this.contextLost) return;
    if (this.edlSupported) {
      this.renderer.setRenderTarget(this.target);
      this.renderer.render(this.scene, this.camera);
      this.renderer.setRenderTarget(null);
      this.renderer.render(this.postScene, this.postCamera);
    } else {
      this.renderer.render(this.scene, this.camera);
    }
    this.needsRender = false;
    this.onFrame();
  }

  frame(time) {
    if (this.disposed) return;
    const delta = Math.min(0.1, (time - this.lastFrameTime) / 1000);
    this.lastFrameTime = time;
    if (this.cameraAnimation) {
      const animation = this.cameraAnimation;
      const t = Math.min(1, (time - animation.start) / animation.duration);
      const eased = 1 - (1 - t) ** 3;
      this.camera.position.lerpVectors(animation.fromPosition, animation.position, eased);
      this.controls.target.lerpVectors(animation.fromTarget, animation.target, eased);
      this.needsRender = true;
      if (t >= 1) this.cameraAnimation = null;
    }
    if (this.controls.update(delta)) this.needsRender = true;
    if (this.needsRender) this.render();
    this.animationFrame = requestAnimationFrame(this.frame);
  }

  screenshot() {
    this.render();
    return this.renderer.domElement;
  }

  dispose() {
    this.disposed = true;
    cancelAnimationFrame(this.animationFrame);
    this.resizeObserver.disconnect();
    this.controls.dispose();
    for (const points of [...this.cloud.children]) this.removeTile(points);
    this.trajectory?.traverse((object) => { object.geometry?.dispose(); object.material?.dispose(); });
    this.pointMaterial.dispose();
    this.grid.geometry.dispose();
    this.grid.material.dispose();
    this.postQuad.geometry.dispose();
    this.postMaterial.dispose();
    this.target.dispose();
    for (const texture of Object.values(this.textures)) texture.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
