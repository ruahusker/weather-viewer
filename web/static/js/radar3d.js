import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const BASE = window.WX_BASE || '';
const params = new URLSearchParams(window.location.search);
const site = params.get('site') || 'KOAX';

const viewport = document.getElementById('radar3dViewport');
const statusEl = document.getElementById('radar3dStatus');
const metaSite = document.getElementById('metaSite');
const metaScan = document.getElementById('metaScan');
const metaTilts = document.getElementById('metaTilts');
const metaDbz = document.getElementById('metaDbz');
const metaTop = document.getElementById('metaTop');
const heightScaleEl = document.getElementById('heightScale');
const hazeOpacityEl = document.getElementById('hazeOpacity');
const coreScaleEl = document.getElementById('coreScale');
const refreshBtn = document.getElementById('refreshVolume');

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(viewport.clientWidth || window.innerWidth, viewport.clientHeight || window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
viewport.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0x070d14, 80, 260);

const camera = new THREE.PerspectiveCamera(39, (viewport.clientWidth || 1) / (viewport.clientHeight || 1), 0.1, 1500);
camera.position.set(68, 36, -58);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.06;
controls.maxPolarAngle = Math.PI * 0.49;
controls.minDistance = 14;
controls.maxDistance = 220;
controls.target.set(0, 10, 0);

scene.add(new THREE.HemisphereLight(0xc8e2ff, 0x17212c, 1.15));
const keyLight = new THREE.DirectionalLight(0xffffff, 1.15);
keyLight.position.set(46, 84, 24);
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0x7cb8ff, 0.42);
fillLight.position.set(-42, 24, -36);
scene.add(fillLight);

const floorGroup = new THREE.Group();
scene.add(floorGroup);

let volumeMesh = null;
let currentPayload = null;
let currentVolumeSize = { x: 64, y: 64, z: 18 };
let needsRender = true;
let idleFrames = 0;

const renderState = {
  heightScale: parseFloat(heightScaleEl.value) || 1.4,
  hazeOpacity: parseFloat(hazeOpacityEl.value) || 0.18,
  contrast: parseFloat(coreScaleEl.value) || 1.0,
};

function decodeBase64ToUint8(value) {
  const raw = atob(value);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

function makeFloorTexture(radiusKm = 48) {
  const size = 1024;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#09111b';
  ctx.fillRect(0, 0, size, size);

  ctx.translate(size / 2, size / 2);
  ctx.strokeStyle = 'rgba(132, 170, 210, 0.18)';
  ctx.lineWidth = 2;
  for (let ring = 12; ring <= radiusKm; ring += 12) {
    const r = (ring / radiusKm) * (size * 0.44);
    ctx.beginPath();
    ctx.arc(0, 0, r, 0, Math.PI * 2);
    ctx.stroke();
  }

  ctx.strokeStyle = 'rgba(132, 170, 210, 0.14)';
  ctx.lineWidth = 1;
  for (let angle = 0; angle < 180; angle += 30) {
    const rad = (angle * Math.PI) / 180;
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(Math.cos(rad) * size * 0.46, Math.sin(rad) * size * 0.46);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(-Math.cos(rad) * size * 0.46, -Math.sin(rad) * size * 0.46);
    ctx.stroke();
  }

  ctx.fillStyle = 'rgba(237, 245, 255, 0.82)';
  ctx.font = '28px sans-serif';
  ctx.fillText('N', -10, -size * 0.46 + 28);
  ctx.fillText('S', -9, size * 0.46 - 12);
  ctx.fillText('W', -size * 0.46 + 12, 10);
  ctx.fillText('E', size * 0.46 - 28, 10);

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function rebuildFloor(payload) {
  floorGroup.clear();
  const radiusKm = payload?.grid?.radius_km || 48;
  const planeSize = radiusKm * 2;
  const texture = makeFloorTexture(radiusKm);

  const plane = new THREE.Mesh(
    new THREE.PlaneGeometry(planeSize, planeSize),
    new THREE.MeshStandardMaterial({
      map: texture,
      color: 0x36506e,
      transparent: true,
      opacity: 0.92,
      roughness: 0.95,
      metalness: 0.02,
      side: THREE.DoubleSide,
    })
  );
  plane.rotation.x = -Math.PI / 2;
  floorGroup.add(plane);

  const siteMarker = new THREE.Mesh(
    new THREE.CylinderGeometry(0.45, 0.45, 2.0, 16),
    new THREE.MeshBasicMaterial({ color: 0x9bd5ff })
  );
  siteMarker.position.set(0, 1.0, 0);
  floorGroup.add(siteMarker);
}

const VOLUME_VERTEX = /* glsl */`
  out vec3 vOrigin;
  out vec3 vDirection;

  void main() {
    vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
    vOrigin = (inverse(modelMatrix) * vec4(cameraPosition, 1.0)).xyz;
    vDirection = position - vOrigin;
    gl_Position = projectionMatrix * mvPosition;
  }
`;

const VOLUME_FRAGMENT = /* glsl */`
  precision highp float;
  precision highp sampler3D;

  in vec3 vOrigin;
  in vec3 vDirection;
  out vec4 outColor;

  uniform sampler3D uVolume;
  uniform vec2 uDbzRange;
  uniform float uOpacityScale;
  uniform float uContrast;
  uniform float uThreshold;
  uniform float uSteps;
  uniform vec3 uTexelSize;

  vec3 sampleNormal(vec3 texPos) {
    vec3 stepSize = uTexelSize;
    float dx = texture(uVolume, clamp(texPos + vec3(stepSize.x, 0.0, 0.0), 0.0, 1.0)).r
             - texture(uVolume, clamp(texPos - vec3(stepSize.x, 0.0, 0.0), 0.0, 1.0)).r;
    float dy = texture(uVolume, clamp(texPos + vec3(0.0, stepSize.y, 0.0), 0.0, 1.0)).r
             - texture(uVolume, clamp(texPos - vec3(0.0, stepSize.y, 0.0), 0.0, 1.0)).r;
    float dz = texture(uVolume, clamp(texPos + vec3(0.0, 0.0, stepSize.z), 0.0, 1.0)).r
             - texture(uVolume, clamp(texPos - vec3(0.0, 0.0, stepSize.z), 0.0, 1.0)).r;
    return normalize(vec3(dx, dz, dy) + vec3(0.0001));
  }

  vec2 hitBox(vec3 orig, vec3 dir) {
    const vec3 boxMin = vec3(-0.5);
    const vec3 boxMax = vec3(0.5);
    vec3 invDir = 1.0 / dir;
    vec3 tminTmp = (boxMin - orig) * invDir;
    vec3 tmaxTmp = (boxMax - orig) * invDir;
    vec3 tmin = min(tminTmp, tmaxTmp);
    vec3 tmax = max(tminTmp, tmaxTmp);
    float t0 = max(tmin.x, max(tmin.y, tmin.z));
    float t1 = min(tmax.x, min(tmax.y, tmax.z));
    return vec2(t0, t1);
  }

  vec3 dbzColor(float sampleValue) {
    float dbz = mix(uDbzRange.x, uDbzRange.y, sampleValue);
    vec3 c0 = vec3(0.31, 0.84, 1.0);
    vec3 c1 = vec3(0.56, 1.0, 0.45);
    vec3 c2 = vec3(1.0, 0.76, 0.29);
    vec3 c3 = vec3(1.0, 0.38, 0.34);
    vec3 c4 = vec3(1.0, 0.95, 0.95);

    if (dbz < 35.0) {
      return mix(c0, c1, clamp((dbz - 20.0) / 15.0, 0.0, 1.0));
    }
    if (dbz < 50.0) {
      return mix(c1, c2, clamp((dbz - 35.0) / 15.0, 0.0, 1.0));
    }
    if (dbz < 62.0) {
      return mix(c2, c3, clamp((dbz - 50.0) / 12.0, 0.0, 1.0));
    }
    return mix(c3, c4, clamp((dbz - 62.0) / 13.0, 0.0, 1.0));
  }

  void main() {
    vec3 rayDir = normalize(vDirection);
    vec2 bounds = hitBox(vOrigin, rayDir);
    if (bounds.x > bounds.y) discard;
    bounds.x = max(bounds.x, 0.0);

    float delta = (bounds.y - bounds.x) / uSteps;
    vec3 stepVec = rayDir * delta;
    vec3 pos = vOrigin + bounds.x * rayDir;
    vec4 accum = vec4(0.0);
    vec3 lightDir = normalize(vec3(0.45, 0.78, 0.42));

    const int MAX_STEPS = 420;
    for (int i = 0; i < MAX_STEPS; i++) {
      if (float(i) >= uSteps) break;
      vec3 texPos = pos + vec3(0.5);
      float raw = texture(uVolume, texPos).r;
      float density = pow(raw, uContrast);

      if (density < uThreshold * 0.34) {
        pos += stepVec * 1.35;
        continue;
      }

      if (density > uThreshold) {
        vec3 color = dbzColor(raw);
        vec3 normal = sampleNormal(texPos);
        float diffuse = max(dot(normal, lightDir), 0.0);
        float rim = pow(1.0 - max(dot(normal, -rayDir), 0.0), 2.0);
        color *= 0.56 + diffuse * 0.96 + rim * 0.12;
        float edge = smoothstep(uThreshold * 0.985, min(1.0, uThreshold + 0.09), density);
        float alpha = edge * uOpacityScale * (0.56 + density * 0.84);
        alpha *= clamp(delta * 1.35, 0.28, 0.82);
        alpha = clamp(alpha, 0.0, 0.115);
        accum.rgb += (1.0 - accum.a) * alpha * color;
        accum.a += (1.0 - accum.a) * alpha;
        if (accum.a > 0.96) break;
      }
      pos += stepVec;
    }

    if (accum.a <= 0.01) discard;
    outColor = accum;
  }
`;

function disposeVolumeMesh() {
  if (!volumeMesh) return;
  volumeMesh.geometry.dispose();
  volumeMesh.material.uniforms.uVolume.value.dispose();
  volumeMesh.material.dispose();
  scene.remove(volumeMesh);
  volumeMesh = null;
}

function buildVolumeTexture(payload) {
  const textureInfo = payload.volume_texture;
  const dims = textureInfo.dims;
  const data = decodeBase64ToUint8(textureInfo.data);
  const texture = new THREE.Data3DTexture(data, dims.x, dims.y, dims.z);
  texture.format = THREE.RedFormat;
  texture.type = THREE.UnsignedByteType;
  texture.minFilter = THREE.LinearFilter;
  texture.magFilter = THREE.LinearFilter;
  texture.unpackAlignment = 1;
  texture.needsUpdate = true;
  return texture;
}

function rebuildVolume(payload) {
  currentPayload = payload;
  disposeVolumeMesh();
  rebuildFloor(payload);

  const texture = buildVolumeTexture(payload);
  const size = payload.volume_texture.size_km;
  currentVolumeSize = { x: size.x, y: size.y, z: size.z };

  const material = new THREE.ShaderMaterial({
    glslVersion: THREE.GLSL3,
    transparent: true,
    side: THREE.BackSide,
    depthWrite: false,
    uniforms: {
      uVolume: { value: texture },
      uDbzRange: {
        value: new THREE.Vector2(
          payload.volume_texture.dbz_range.min,
          payload.volume_texture.dbz_range.max
        ),
      },
      uOpacityScale: { value: 0.28 + renderState.hazeOpacity * 1.1 },
      uContrast: { value: 1.02 + renderState.contrast * 0.72 },
      uThreshold: { value: Math.max(0.03, 0.088 - renderState.hazeOpacity * 0.035) },
      uSteps: { value: 420.0 },
      uTexelSize: { value: new THREE.Vector3(1 / texture.image.width, 1 / texture.image.height, 1 / texture.image.depth) },
    },
    vertexShader: VOLUME_VERTEX,
    fragmentShader: VOLUME_FRAGMENT,
  });

  volumeMesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), material);
  volumeMesh.scale.set(size.x, size.z * renderState.heightScale, size.y);
  volumeMesh.position.y = (size.z * renderState.heightScale) / 2;
  scene.add(volumeMesh);

  const topKm = Number(payload.summary?.top_km || 12);
  controls.target.set(0, Math.max(6, topKm * 0.52 * renderState.heightScale), 0);
  controls.update();
  needsRender = true;
  idleFrames = 0;
}

function applyPreset(name) {
  if (name === 'top') {
    camera.position.set(0.1, 112, 0.1);
  } else if (name === 'inflow') {
    camera.position.set(74, 34, -46);
  } else {
    camera.position.set(68, 36, -58);
  }
  controls.update();
  needsRender = true;
  idleFrames = 0;
}

function updateMeta(payload) {
  const topKm = Number(payload.summary?.top_km || 0);
  const topFt = topKm ? Math.round(topKm * 3280.84) : null;
  metaSite.textContent = payload.site || site;
  metaScan.textContent = payload.timestamp || '--';
  metaTilts.textContent = payload.volume_meta?.tilts?.join(', ') || '--';
  metaDbz.textContent = payload.summary?.max_dbz ? `${payload.summary.max_dbz} dBZ` : '--';
  metaTop.textContent = topFt ? `${topFt.toLocaleString()} ft` : '--';
}

async function loadVolume() {
  statusEl.textContent = 'Building latest multi-tilt volume...';
  try {
    const resp = await fetch(`${BASE}/api/radar/volume3d/${encodeURIComponent(site)}`);
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    const payload = await resp.json();
    updateMeta(payload);
    rebuildVolume(payload);
    statusEl.textContent = `Raymarched ${payload.selection_mode === 'peak' ? 'dominant storm core' : 'selected storm'} from ${payload.volume_meta?.sweep_count || '--'} tilts.`;
  } catch (err) {
    statusEl.textContent = `3D render failed: ${err.message || err}`;
  }
}

function rerenderFromControls() {
  renderState.heightScale = parseFloat(heightScaleEl.value) || 1.4;
  renderState.hazeOpacity = parseFloat(hazeOpacityEl.value) || 0.18;
  renderState.contrast = parseFloat(coreScaleEl.value) || 1.0;

  if (volumeMesh) {
    volumeMesh.scale.set(currentVolumeSize.x, currentVolumeSize.z * renderState.heightScale, currentVolumeSize.y);
    volumeMesh.position.y = (currentVolumeSize.z * renderState.heightScale) / 2;
    volumeMesh.material.uniforms.uOpacityScale.value = 0.28 + renderState.hazeOpacity * 1.1;
    volumeMesh.material.uniforms.uContrast.value = 1.02 + renderState.contrast * 0.72;
    volumeMesh.material.uniforms.uThreshold.value = Math.max(0.03, 0.088 - renderState.hazeOpacity * 0.035);
  }
  needsRender = true;
  idleFrames = 0;
}

heightScaleEl.addEventListener('input', rerenderFromControls);
hazeOpacityEl.addEventListener('input', rerenderFromControls);
coreScaleEl.addEventListener('input', rerenderFromControls);
document.getElementById('presetOblique').addEventListener('click', () => applyPreset('oblique'));
document.getElementById('presetTop').addEventListener('click', () => applyPreset('top'));
document.getElementById('presetInflow').addEventListener('click', () => applyPreset('inflow'));
refreshBtn.addEventListener('click', () => loadVolume());
controls.addEventListener('change', () => {
  needsRender = true;
  idleFrames = 0;
});

window.addEventListener('resize', () => {
  const width = viewport.clientWidth || window.innerWidth;
  const height = viewport.clientHeight || window.innerHeight;
  camera.aspect = width / Math.max(height, 1);
  camera.updateProjectionMatrix();
  renderer.setSize(width, height);
  needsRender = true;
  idleFrames = 0;
});

function animate() {
  requestAnimationFrame(animate);
  const changed = controls.update();
  if (changed) {
    needsRender = true;
    idleFrames = 0;
  }
  if (needsRender || idleFrames < 2) {
    renderer.render(scene, camera);
    idleFrames += 1;
    if (!changed && idleFrames >= 2) {
      needsRender = false;
    }
  }
}

applyPreset('oblique');
loadVolume();
animate();
