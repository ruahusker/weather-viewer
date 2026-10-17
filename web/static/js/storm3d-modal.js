/**
 * 3D Storm Profile — Inline modal with multi-channel volumetric raymarching.
 *
 * Loaded after app.js as a classic <script>. Accesses `leafletMap` and `state`
 * from the shared global lexical scope. Three.js is loaded dynamically on
 * first activation to avoid impacting initial page load.
 */
(function () {
  'use strict';

  /* ====================================================================
   * State
   * ==================================================================== */

  let THREE = null;
  let OrbitControls = null;
  let threeLoaded = false;

  let renderer = null;
  let scene = null;
  let camera = null;
  let controls = null;
  let floorGroup = null;
  let volumeMesh = null;
  let animFrameId = null;
  let needsRender = true;
  let idleFrames = 0;
  let sceneInited = false;

  let crosshairMode = false;
  let boxSelectDownHandler = null;
  let boxSelectStartPoint = null;
  let boxSelectMoveHandler = null;
  let boxSelectUpHandler = null;
  let selectionBoxEl = null;
  let currentPayload = null;
  let currentVolumeSize = { x: 50, y: 50, z: 20 };
  let lastClickLatLon = null;
  let lastSelectionBox = null;

  const renderState = {
    heightScale: 1.5,
    hazeOpacity: 0.15,
    contrast: 1.0,
    autoSensitivity: 0.0,
    velocityTint: 0.5,
    hailHighlight: 0.6,
  };

  /* ====================================================================
   * DOM refs
   * ==================================================================== */

  const modal = document.getElementById('storm3dModal');
  const modalContainer = modal.querySelector('.storm3d-container');
  const backdrop = modal.querySelector('.storm3d-backdrop');
  const viewport = document.getElementById('storm3dViewport');
  const loaderEl = document.getElementById('storm3dLoader');
  const statusEl = document.getElementById('storm3dModalStatus');
  const banner = document.getElementById('storm3dCrosshairBanner');
  const downloadBtn = document.getElementById('storm3dDownload');
  const copyBtn = document.getElementById('storm3dCopy');

  const metaSite = document.getElementById('s3dMetaSite');
  const metaScan = document.getElementById('s3dMetaScan');
  const metaTilts = document.getElementById('s3dMetaTilts');
  const metaDbz = document.getElementById('s3dMetaDbz');
  const metaTop = document.getElementById('s3dMetaTop');

  const sliderHeight = document.getElementById('s3dHeightScale');
  const sliderHaze = document.getElementById('s3dHazeOpacity');
  const sliderContrast = document.getElementById('s3dCoreContrast');
  const sliderAutoSensitivity = document.getElementById('s3dAutoSensitivity');
  const sliderVelocity = document.getElementById('s3dVelocityTint');
  const sliderHail = document.getElementById('s3dHailHighlight');

  /* ====================================================================
   * Three.js dynamic loading
   * ==================================================================== */

  async function ensureThree() {
    if (threeLoaded) return;
    THREE = await import('three');
    const orbitMod = await import('three/addons/controls/OrbitControls.js');
    OrbitControls = orbitMod.OrbitControls;
    threeLoaded = true;
  }

  /* ====================================================================
   * Scene setup (called once)
   * ==================================================================== */

  function initScene() {
    if (sceneInited) return;
    sceneInited = true;

    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    viewport.appendChild(renderer.domElement);

    scene = new THREE.Scene();
    scene.fog = new THREE.Fog(0x070d14, 80, 280);

    camera = new THREE.PerspectiveCamera(39, 1, 0.1, 1500);
    camera.position.set(68, 36, -58);

    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.06;
    controls.minPolarAngle = Math.PI * 0.26;
    controls.maxPolarAngle = Math.PI * 0.6;
    controls.minDistance = 10;
    controls.maxDistance = 320;
    controls.target.set(0, 10, 0);

    scene.add(new THREE.HemisphereLight(0xc8e2ff, 0x17212c, 1.15));
    const key = new THREE.DirectionalLight(0xffffff, 1.15);
    key.position.set(46, 84, 24);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x7cb8ff, 0.42);
    fill.position.set(-42, 24, -36);
    scene.add(fill);

    floorGroup = new THREE.Group();
    scene.add(floorGroup);

    controls.addEventListener('change', () => { needsRender = true; idleFrames = 0; });

    sizeRenderer();

    const ro = new ResizeObserver(() => { sizeRenderer(); needsRender = true; idleFrames = 0; });
    ro.observe(viewport);
  }

  function sizeRenderer() {
    if (!renderer) return;
    const w = viewport.clientWidth || 800;
    const h = viewport.clientHeight || 600;
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }

  /* ====================================================================
   * Floor (range rings + compass)
   * ==================================================================== */

  function makeFloorTexture(radiusKm) {
    const size = 1024;
    const c = document.createElement('canvas');
    c.width = size; c.height = size;
    const ctx = c.getContext('2d');
    const bg = ctx.createLinearGradient(0, 0, 0, size);
    bg.addColorStop(0, '#6f8e8e');
    bg.addColorStop(0.45, '#53706d');
    bg.addColorStop(1, '#324845');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, size, size);

    for (let i = 0; i < 1800; i++) {
      const x = Math.random() * size;
      const y = Math.random() * size;
      const alpha = 0.04 + Math.random() * 0.06;
      const shade = 122 + Math.floor(Math.random() * 50);
      ctx.fillStyle = `rgba(${shade}, ${148 + Math.floor(Math.random() * 26)}, ${98 + Math.floor(Math.random() * 18)}, ${alpha})`;
      ctx.fillRect(x, y, 2, 2);
    }

    const vignette = ctx.createRadialGradient(size / 2, size / 2, size * 0.12, size / 2, size / 2, size * 0.52);
    vignette.addColorStop(0, 'rgba(228, 235, 176, 0.24)');
    vignette.addColorStop(0.55, 'rgba(122, 150, 108, 0.12)');
    vignette.addColorStop(1, 'rgba(18, 26, 28, 0.20)');
    ctx.fillStyle = vignette;
    ctx.fillRect(0, 0, size, size);

    ctx.translate(size / 2, size / 2);

    ctx.strokeStyle = 'rgba(255, 255, 255, 0.96)';
    ctx.lineWidth = 4.2;
    for (let r = 10; r <= radiusKm; r += 10) {
      const px = (r / radiusKm) * (size * 0.44);
      ctx.beginPath(); ctx.arc(0, 0, px, 0, Math.PI * 2); ctx.stroke();

      ctx.save();
      ctx.strokeStyle = 'rgba(18, 28, 30, 0.32)';
      ctx.lineWidth = 7.5;
      ctx.beginPath(); ctx.arc(0, 0, px, 0, Math.PI * 2); ctx.stroke();
      ctx.restore();

      ctx.save();
      ctx.fillStyle = 'rgba(255, 255, 255, 1.0)';
      ctx.font = '800 23px sans-serif';
      ctx.shadowColor = 'rgba(10, 18, 22, 0.82)';
      ctx.shadowBlur = 8;
      ctx.fillText(`${r} km`, px + 6, -4);
      ctx.restore();
    }
    ctx.strokeStyle = 'rgba(246, 250, 252, 0.58)';
    ctx.lineWidth = 2.2;
    for (let a = 0; a < 180; a += 30) {
      const rad = a * Math.PI / 180;
      ctx.beginPath(); ctx.moveTo(0, 0);
      ctx.lineTo(Math.cos(rad) * size * 0.46, Math.sin(rad) * size * 0.46); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, 0);
      ctx.lineTo(-Math.cos(rad) * size * 0.46, -Math.sin(rad) * size * 0.46); ctx.stroke();
    }
    ctx.strokeStyle = 'rgba(248, 252, 252, 0.20)';
    ctx.lineWidth = 1.2;
    for (let offset = -size * 0.42; offset <= size * 0.42; offset += size * 0.07) {
      ctx.beginPath();
      ctx.moveTo(-size * 0.44, offset);
      ctx.lineTo(size * 0.44, offset);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(offset, -size * 0.44);
      ctx.lineTo(offset, size * 0.44);
      ctx.stroke();
    }

    ctx.fillStyle = 'rgba(255, 255, 255, 1.0)';
    ctx.font = '900 40px sans-serif';
    ctx.shadowColor = 'rgba(8, 14, 18, 0.95)';
    ctx.shadowBlur = 10;
    ctx.lineWidth = 4;
    ctx.strokeStyle = 'rgba(12, 18, 20, 0.58)';
    ctx.strokeText('N', -10, -size * 0.46 + 28);
    ctx.strokeText('S', -9, size * 0.46 - 12);
    ctx.strokeText('W', -size * 0.46 + 12, 10);
    ctx.strokeText('E', size * 0.46 - 28, 10);
    ctx.fillText('N', -10, -size * 0.46 + 28);
    ctx.fillText('S', -9, size * 0.46 - 12);
    ctx.fillText('W', -size * 0.46 + 12, 10);
    ctx.fillText('E', size * 0.46 - 28, 10);

    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  function rebuildFloor(payload) {
    floorGroup.clear();
    floorGroup.position.y = -0.18;
    const radiusKm = payload?.grid?.radius_km || 25;
    const planeSize = radiusKm * 2;
    const tex = makeFloorTexture(radiusKm);

    const plane = new THREE.Mesh(
      new THREE.PlaneGeometry(planeSize, planeSize),
      new THREE.MeshStandardMaterial({
        map: tex, color: 0xd4d7a8, transparent: true, opacity: 1.0,
        emissive: 0x54645b,
        emissiveIntensity: 0.36,
        roughness: 0.9, metalness: 0.02, side: THREE.DoubleSide,
        polygonOffset: true,
        polygonOffsetFactor: 1,
        polygonOffsetUnits: 1,
      })
    );
    plane.rotation.x = -Math.PI / 2;
    plane.renderOrder = 0;
    floorGroup.add(plane);

    const underlay = new THREE.Mesh(
      new THREE.CircleGeometry(planeSize * 0.54, 80),
      new THREE.MeshBasicMaterial({
        color: 0x395455,
        transparent: true,
        opacity: 0.18,
        side: THREE.DoubleSide,
      })
    );
    underlay.rotation.x = -Math.PI / 2;
    underlay.position.y = -0.12;
    underlay.renderOrder = 1;
    floorGroup.add(underlay);

    const perimeterRing = new THREE.Mesh(
      new THREE.RingGeometry(planeSize * 0.49, planeSize * 0.505, 96),
      new THREE.MeshBasicMaterial({
        color: 0xffffff,
        transparent: true,
        opacity: 0.52,
        side: THREE.DoubleSide,
      })
    );
    perimeterRing.rotation.x = -Math.PI / 2;
    perimeterRing.position.y = 0.02;
    perimeterRing.renderOrder = 2;
    floorGroup.add(perimeterRing);

    const marker = new THREE.Mesh(
      new THREE.CylinderGeometry(0.4, 0.4, 1.8, 16),
      new THREE.MeshBasicMaterial({ color: 0xffffff })
    );
    marker.position.set(0, 0.9, 0);
    marker.scale.set(1.18, 1.0, 1.18);
    marker.renderOrder = 3;
    floorGroup.add(marker);

    const siteRing = new THREE.Mesh(
      new THREE.RingGeometry(0.9, 1.25, 48),
      new THREE.MeshBasicMaterial({
        color: 0xffffff,
        transparent: true,
        opacity: 1.0,
        side: THREE.DoubleSide,
      })
    );
    siteRing.rotation.x = -Math.PI / 2;
    siteRing.position.y = 0.03;
    siteRing.renderOrder = 4;
    floorGroup.add(siteRing);
  }

  /* ====================================================================
   * GLSL Shaders — Enhanced multi-channel raymarching
   * ==================================================================== */

  const VOLUME_VERTEX = /* glsl */`
    out vec3 vOrigin;
    out vec3 vDirection;
    void main() {
      vec4 mvPos = modelViewMatrix * vec4(position, 1.0);
      vOrigin = (inverse(modelMatrix) * vec4(cameraPosition, 1.0)).xyz;
      vDirection = position - vOrigin;
      gl_Position = projectionMatrix * mvPos;
    }
  `;

  const VOLUME_FRAGMENT = /* glsl */`
    precision highp float;
    precision highp sampler3D;

    in vec3 vOrigin;
    in vec3 vDirection;
    out vec4 outColor;

    uniform sampler3D uVolume;
    uniform vec2 uDbzRange;        // min, max dBZ for reflectivity channel
    uniform float uOpacityScale;
    uniform float uContrast;
    uniform float uThreshold;
    uniform float uSteps;
    uniform vec3 uTexelSize;
    uniform float uVelocityTint;
    uniform float uHailHighlight;
    uniform float uDensityFloor;
    uniform float uSkipFactor;
    uniform float uAlphaCap;
    uniform float uHasVelocity;
    uniform float uHasDualPol;

    /* ---- ray-box intersection ---- */
    vec2 hitBox(vec3 orig, vec3 dir) {
      const vec3 bMin = vec3(-0.5);
      const vec3 bMax = vec3(0.5);
      vec3 inv = 1.0 / dir;
      vec3 tA = (bMin - orig) * inv;
      vec3 tB = (bMax - orig) * inv;
      vec3 tMin = min(tA, tB);
      vec3 tMax = max(tA, tB);
      float t0 = max(tMin.x, max(tMin.y, tMin.z));
      float t1 = min(tMax.x, min(tMax.y, tMax.z));
      return vec2(t0, t1);
    }

    /* ---- gradient normal for lighting ---- */
    vec3 sampleNormal(vec3 p) {
      vec3 s = uTexelSize;
      float dx = texture(uVolume, clamp(p + vec3(s.x,0,0), 0.0, 1.0)).r
               - texture(uVolume, clamp(p - vec3(s.x,0,0), 0.0, 1.0)).r;
      float dy = texture(uVolume, clamp(p + vec3(0,s.y,0), 0.0, 1.0)).r
               - texture(uVolume, clamp(p - vec3(0,s.y,0), 0.0, 1.0)).r;
      float dz = texture(uVolume, clamp(p + vec3(0,0,s.z), 0.0, 1.0)).r
               - texture(uVolume, clamp(p - vec3(0,0,s.z), 0.0, 1.0)).r;
      return normalize(vec3(dx, dz, dy) + 1e-4);
    }

    /* ---- NWS-style 7-stop color ramp ---- */
    vec3 nwsColor(float dbz) {
      // Stops:  5   15   20   35   50   60   75
      vec3 c0 = vec3(0.15, 0.45, 0.65);  // dark teal  (5 dBZ)
      vec3 c1 = vec3(0.25, 0.72, 0.88);  // teal       (15)
      vec3 c2 = vec3(0.30, 0.90, 0.42);  // green      (20)
      vec3 c3 = vec3(0.95, 0.92, 0.25);  // yellow     (35)
      vec3 c4 = vec3(1.00, 0.50, 0.18);  // orange     (50)
      vec3 c5 = vec3(0.95, 0.22, 0.22);  // red        (60)
      vec3 c6 = vec3(1.00, 0.60, 0.95);  // magenta    (70)
      vec3 c7 = vec3(1.00, 0.95, 0.95);  // white      (75+)

      if (dbz < 15.0) return mix(c0, c1, clamp((dbz - 5.0) / 10.0, 0.0, 1.0));
      if (dbz < 20.0) return mix(c1, c2, clamp((dbz - 15.0) / 5.0, 0.0, 1.0));
      if (dbz < 35.0) return mix(c2, c3, clamp((dbz - 20.0) / 15.0, 0.0, 1.0));
      if (dbz < 50.0) return mix(c3, c4, clamp((dbz - 35.0) / 15.0, 0.0, 1.0));
      if (dbz < 60.0) return mix(c4, c5, clamp((dbz - 50.0) / 10.0, 0.0, 1.0));
      if (dbz < 70.0) return mix(c5, c6, clamp((dbz - 60.0) / 10.0, 0.0, 1.0));
      return mix(c6, c7, clamp((dbz - 70.0) / 5.0, 0.0, 1.0));
    }

    /* ---- main ---- */
    void main() {
      vec3 rayDir = normalize(vDirection);
      vec2 bounds = hitBox(vOrigin, rayDir);
      if (bounds.x > bounds.y) discard;
      bounds.x = max(bounds.x, 0.0);

      float delta = (bounds.y - bounds.x) / uSteps;
      vec3 stepVec = rayDir * delta;
      vec3 pos = vOrigin + (bounds.x + delta * 0.5) * rayDir;
      vec4 accum = vec4(0.0);
      vec3 lightDir = normalize(vec3(0.45, 0.78, 0.42));

      const int MAX_STEPS = 768;
      for (int i = 0; i < MAX_STEPS; i++) {
        if (float(i) >= uSteps) break;
        vec3 texPos = pos + vec3(0.5);

        // Sample all 4 channels: R=refl, G=vel, B=ZDR, A=CC quality
        vec4 sample4 = texture(uVolume, texPos);
        float refl   = sample4.r;
        float vel    = sample4.g;
        float zdr    = sample4.b;
        float ccMask = sample4.a;

        float density = pow(refl, uContrast);
        float densityPresence = smoothstep(uThreshold * 0.5, uThreshold + 0.06, density);

        // Empty-space skip
        if (densityPresence <= 0.001) {
          pos += stepVec * min(uSkipFactor, 1.06);
          continue;
        }

        float metWeight = uHasDualPol > 0.5 ? smoothstep(0.28, 0.62, ccMask) : 1.0;

        if (densityPresence > 0.0) {
          // Physical dBZ from normalized reflectivity
          float dbz = mix(uDbzRange.x, uDbzRange.y, refl);

          // --- Base color from NWS ramp ---
          vec3 color = nwsColor(dbz);

          // --- Velocity tint (mesocyclone highlighting) ---
          if (uHasVelocity > 0.5 && uVelocityTint > 0.0) {
            float velDelta = abs(vel - 0.5) * 2.0;  // 0=calm, 1=max
            if (velDelta > 0.45) {
              vec3 velColor = vel > 0.5
                ? vec3(1.0, 0.25, 0.25)   // outbound = red
                : vec3(0.25, 0.55, 1.0);  // inbound = blue
              float blend = smoothstep(0.45, 0.85, velDelta) * uVelocityTint * 0.35;
              color = mix(color, velColor, blend);
            }
          }

          // --- Hail highlight (high Z + low ZDR) ---
          if (uHasDualPol > 0.5 && uHailHighlight > 0.0 && dbz > 48.0) {
            float zdrPhys = mix(-2.0, 6.0, zdr);
            if (zdrPhys < 1.2) {
              float hailStr = smoothstep(1.2, -0.5, zdrPhys) * smoothstep(48.0, 58.0, dbz);
              color = mix(color, vec3(1.0, 0.82, 1.0), hailStr * uHailHighlight * 0.45);
            }
          }

          // --- Lighting ---
          vec3 normal = sampleNormal(texPos);
          float diffuse = max(dot(normal, lightDir), 0.0);
          float rim = pow(1.0 - max(dot(normal, -rayDir), 0.0), 1.7);
          color *= 0.62 + diffuse * 0.72 + rim * 0.08;

          // --- Opacity tiers (the "secret sauce" transfer function) ---
          float alpha;
          if (dbz < 15.0) {
            alpha = refl * 0.11;                                     // cloud shell
          } else if (dbz < 35.0) {
            alpha = 0.11 + (dbz - 15.0) / 20.0 * 0.15;             // wispy body
          } else if (dbz < 50.0) {
            alpha = 0.26 + (dbz - 35.0) / 15.0 * 0.36;             // solid structure
          } else {
            alpha = 0.62 + min((dbz - 50.0) / 25.0, 1.0) * 0.24;   // dense core
          }

          alpha *= uOpacityScale;
          alpha *= densityPresence;
          alpha *= metWeight;
          alpha *= clamp(delta * 1.1, 0.22, 0.72);
          alpha = clamp(alpha, 0.0, uAlphaCap);

          // Front-to-back compositing
          accum.rgb += (1.0 - accum.a) * alpha * color;
          accum.a   += (1.0 - accum.a) * alpha;
          if (accum.a > 0.96) break;
        }
        pos += stepVec;
      }

      if (accum.a <= 0.008) discard;
      outColor = accum;
    }
  `;

  /* ====================================================================
   * Volume texture + mesh building
   * ==================================================================== */

  function decodeBase64(b64) {
    const raw = atob(b64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function disposeVolume() {
    if (!volumeMesh) return;
    volumeMesh.geometry.dispose();
    volumeMesh.material.uniforms.uVolume.value.dispose();
    volumeMesh.material.dispose();
    scene.remove(volumeMesh);
    volumeMesh = null;
  }

  function buildTexture(texInfo) {
    const dims = texInfo.dims;
    const data = decodeBase64(texInfo.data);
    const isRGBA = texInfo.encoding === 'base64-rgba-u8';

    const tex = new THREE.Data3DTexture(data, dims.x, dims.y, dims.z);
    tex.format = isRGBA ? THREE.RGBAFormat : THREE.RedFormat;
    tex.type = THREE.UnsignedByteType;
    tex.minFilter = THREE.LinearFilter;
    tex.magFilter = THREE.LinearFilter;
    tex.wrapS = THREE.ClampToEdgeWrapping;
    tex.wrapT = THREE.ClampToEdgeWrapping;
    tex.wrapR = THREE.ClampToEdgeWrapping;
    tex.generateMipmaps = false;
    tex.unpackAlignment = 1;
    tex.needsUpdate = true;
    return tex;
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function buildUniformConfig(payload) {
    const maxDbz = Number(payload?.summary?.max_dbz || 45);
    const stormNorm = clamp((maxDbz - 25) / 40, 0, 1);
    const weakBoost = 1 - stormNorm;
    const manualBias = clamp(renderState.autoSensitivity || 0, -1, 1);
    const weakBias = Math.max(manualBias, 0);
    const strongBias = Math.max(-manualBias, 0);

    return {
      opacityScale: 0.26 + renderState.hazeOpacity * (
        0.95 + weakBoost * (0.85 + weakBias * 0.7) - stormNorm * (0.2 + strongBias * 0.4)
      ),
      contrast: 0.92 + renderState.contrast * (
        0.52 + stormNorm * (0.38 + strongBias * 0.28) - weakBoost * (0.1 + weakBias * 0.06)
      ),
      threshold: clamp(
        0.07
          - renderState.hazeOpacity * (0.026 + weakBoost * (0.022 + weakBias * 0.018))
          + stormNorm * (0.022 + strongBias * 0.028)
          - weakBias * 0.012
          + strongBias * 0.01,
        0.012,
        0.1
      ),
      densityFloor: clamp(
        0.22 - weakBoost * (0.08 + weakBias * 0.08) + stormNorm * (0.11 + strongBias * 0.08),
        0.08,
        0.42
      ),
      skipFactor: clamp(
        1.04 + stormNorm * (0.08 + strongBias * 0.06) - weakBoost * (0.04 + weakBias * 0.05),
        1.0,
        1.18
      ),
      alphaCap: clamp(
        0.145 - stormNorm * (0.03 + strongBias * 0.024) + weakBoost * (0.01 + weakBias * 0.02),
        0.09,
        0.17
      ),
    };
  }

  function applyAdaptiveUniforms(payloadOverride) {
    const payload = payloadOverride || currentPayload;
    if (!volumeMesh || !payload) return;
    const cfg = buildUniformConfig(payload);
    const u = volumeMesh.material.uniforms;
    u.uOpacityScale.value = cfg.opacityScale;
    u.uContrast.value = cfg.contrast;
    u.uThreshold.value = cfg.threshold;
    u.uDensityFloor.value = cfg.densityFloor;
    u.uSkipFactor.value = cfg.skipFactor;
    u.uAlphaCap.value = cfg.alphaCap;
  }

  function rebuildVolume(payload) {
    currentPayload = payload;
    disposeVolume();
    rebuildFloor(payload);

    const texInfo = payload.volume_texture;
    const tex = buildTexture(texInfo);
    const size = texInfo.size_km;
    currentVolumeSize = { x: size.x, y: size.y, z: size.z };

    const dbzMin = texInfo.channels ? texInfo.channels.r.min : (texInfo.dbz_range?.min || 5);
    const dbzMax = texInfo.channels ? texInfo.channels.r.max : (texInfo.dbz_range?.max || 75);
    const hasVelocity = Boolean(texInfo.channels && texInfo.has_velocity);
    const hasDualPol = Boolean(texInfo.channels && texInfo.has_dual_pol);
    const uniformCfg = buildUniformConfig(payload);

    const mat = new THREE.ShaderMaterial({
      glslVersion: THREE.GLSL3,
      transparent: true,
      side: THREE.BackSide,
      depthTest: true,
      depthWrite: false,
      uniforms: {
        uVolume: { value: tex },
        uDbzRange: { value: new THREE.Vector2(dbzMin, dbzMax) },
        uOpacityScale: { value: uniformCfg.opacityScale },
        uContrast: { value: uniformCfg.contrast },
        uThreshold: { value: uniformCfg.threshold },
        uSteps: { value: 720.0 },
        uTexelSize: { value: new THREE.Vector3(
          1 / tex.image.width, 1 / tex.image.height, 1 / tex.image.depth
        )},
        uVelocityTint: { value: hasVelocity ? renderState.velocityTint : 0.0 },
        uHailHighlight: { value: hasDualPol ? renderState.hailHighlight : 0.0 },
        uDensityFloor: { value: uniformCfg.densityFloor },
        uSkipFactor: { value: uniformCfg.skipFactor },
        uAlphaCap: { value: uniformCfg.alphaCap },
        uHasVelocity: { value: hasVelocity ? 1.0 : 0.0 },
        uHasDualPol: { value: hasDualPol ? 1.0 : 0.0 },
      },
      vertexShader: VOLUME_VERTEX,
      fragmentShader: VOLUME_FRAGMENT,
    });

    volumeMesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), mat);
    volumeMesh.renderOrder = 30;
    volumeMesh.scale.set(size.x, size.z * renderState.heightScale, size.y);
    volumeMesh.position.y = (size.z * renderState.heightScale) / 2 + 0.12;
    scene.add(volumeMesh);

    // Target the camera at the actual storm's vertical midpoint, not a fixed height
    const topKm = Number(payload.summary?.top_km || 6);
    const baseKm = Number(payload.summary?.base_km || 0);
    const midKm = (topKm + baseKm) / 2;
    const targetY = Math.max(4, baseKm * renderState.heightScale + (topKm - baseKm) * renderState.heightScale * 0.42);
    controls.target.set(0, targetY, 0);

    // Position camera between the radar site and the storm for best presentation.
    // The volume grid is centered on the click point (storm), so the radar site
    // is at (-click_x_km, -click_y_km) relative to grid center.  We place the
    // camera on that side — looking from the radar toward the storm.
    const sel = payload.selection || {};
    const stormX = Number(sel.click_x_km || 0);   // storm position in radar-relative km
    const stormY = Number(sel.click_y_km || 0);
    // Direction FROM storm TO radar (in scene XZ plane, where scene-X = east, scene-Z = -north)
    let dirX = -stormX;   // radar is at origin, storm at (stormX,stormY), so radar-storm = -storm
    let dirZ = stormY;    // scene Z is flipped relative to north
    const dirLen = Math.hypot(dirX, dirZ);
    if (dirLen < 8) {
      dirX = -0.78;
      dirZ = 0.62;
    } else {
      dirX /= dirLen;
      dirZ /= dirLen;
    }

    const viewDist = Math.max(88, size.x * 1.95);
    const viewHeight = Math.max(targetY + 6, baseKm * renderState.heightScale + (topKm - baseKm) * renderState.heightScale * 0.24 + 9);
    camera.position.set(dirX * viewDist, viewHeight, dirZ * viewDist);
    camera.lookAt(controls.target);
    controls.update();
    needsRender = true;
    idleFrames = 0;
  }

  /* ====================================================================
   * Metadata display
   * ==================================================================== */

  function updateMeta(p) {
    const topKm = Number(p.summary?.top_km || 0);
    const topFt = topKm ? Math.round(topKm * 3280.84) : null;
    metaSite.textContent = p.site || '--';
    metaScan.textContent = p.timestamp || '--';
    metaTilts.textContent = p.volume_meta?.tilts?.join(', ') || '--';
    metaDbz.textContent = p.summary?.max_dbz ? `${p.summary.max_dbz} dBZ` : '--';
    metaTop.textContent = topFt ? `${topFt.toLocaleString()} ft` : '--';
  }

  /* ====================================================================
   * Data fetching
   * ==================================================================== */

  async function fetchVolume(site, lat, lon) {
    // Reuse the BASE constant from app.js (handles /wx/ prefix on production)
    const base = (typeof BASE !== 'undefined' ? BASE : '');
    const url = `${base}/api/radar/volume3d/${encodeURIComponent(site)}?lat=${lat}&lon=${lon}&mode=standard`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    return await resp.json();
  }

  async function fetchVolumeForSelection(site, selection) {
    const base = (typeof BASE !== 'undefined' ? BASE : '');
    const params = new URLSearchParams({
      lat: String(selection.center.lat),
      lon: String(selection.center.lon),
      mode: 'standard',
    });
    if (selection.box) {
      params.set('box_lat_min', String(selection.box.latMin));
      params.set('box_lon_min', String(selection.box.lonMin));
      params.set('box_lat_max', String(selection.box.latMax));
      params.set('box_lon_max', String(selection.box.lonMax));
    }
    const url = `${base}/api/radar/volume3d/${encodeURIComponent(site)}?${params.toString()}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    return await resp.json();
  }

  async function canvasToBlob(canvas) {
    return await new Promise((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (blob) resolve(blob);
        else reject(new Error('Failed to encode screenshot'));
      }, 'image/png');
    });
  }

  function buildCaptureFilename() {
    const site = (currentPayload?.site || (typeof state !== 'undefined' ? state.radarSite : 'radar') || 'radar').toLowerCase();
    const scan = String(currentPayload?.timestamp || 'latest').replace(/[^0-9a-z_]+/gi, '_');
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    return `wxviewer_storm3d_${site}_${scan}_${timestamp}.png`;
  }

  async function captureStorm3dCanvas() {
    if (modal.classList.contains('hidden')) {
      throw new Error('3D panel is not open');
    }
    try {
      await loadHtml2Canvas();
    } catch (err) {
      console.error('html2canvas failed to load:', err);
      throw new Error('Screenshot capture is unavailable');
    }
    needsRender = true;
    idleFrames = 0;
    if (renderer && scene && camera) {
      renderer.render(scene, camera);
    }
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return await html2canvas(modalContainer, {
      useCORS: true,
      allowTaint: true,
      backgroundColor: null,
      width: Math.ceil(modalContainer.clientWidth),
      height: Math.ceil(modalContainer.clientHeight),
      ignoreElements: (el) =>
        el?.classList?.contains('storm3d-export-btn') ||
        el?.id === 'storm3dClose',
    });
  }

  async function downloadStorm3dScreenshot() {
    const canvas = await captureStorm3dCanvas();
    const blob = await canvasToBlob(canvas);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = buildCaptureFilename();
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function copyStorm3dScreenshot() {
    const canvas = await captureStorm3dCanvas();
    const blob = await canvasToBlob(canvas);
    if (!navigator.clipboard || typeof ClipboardItem === 'undefined') {
      throw new Error('Clipboard image copy is not supported in this browser');
    }
    await navigator.clipboard.write([
      new ClipboardItem({ 'image/png': blob }),
    ]);
  }

  /* ====================================================================
   * Animation loop
   * ==================================================================== */

  function animate() {
    animFrameId = requestAnimationFrame(animate);
    const changed = controls.update();
    if (changed) { needsRender = true; idleFrames = 0; }
    if (needsRender || idleFrames < 2) {
      renderer.render(scene, camera);
      idleFrames++;
      if (!changed && idleFrames >= 2) needsRender = false;
    }
  }

  function stopAnimate() {
    if (animFrameId != null) { cancelAnimationFrame(animFrameId); animFrameId = null; }
  }

  /* ====================================================================
   * Slider → uniform sync
   * ==================================================================== */

  function syncControls() {
    renderState.heightScale = parseFloat(sliderHeight.value) || 1.5;
    renderState.hazeOpacity = parseFloat(sliderHaze.value) || 0.15;
    renderState.contrast = parseFloat(sliderContrast.value) || 1.0;
    renderState.autoSensitivity = parseFloat(sliderAutoSensitivity.value) || 0.0;
    renderState.velocityTint = parseFloat(sliderVelocity.value) || 0.5;
    renderState.hailHighlight = parseFloat(sliderHail.value) || 0.6;

    if (volumeMesh) {
      const u = volumeMesh.material.uniforms;
      applyAdaptiveUniforms();
      u.uVelocityTint.value = renderState.velocityTint;
      u.uHailHighlight.value = renderState.hailHighlight;

      volumeMesh.scale.set(currentVolumeSize.x, currentVolumeSize.z * renderState.heightScale, currentVolumeSize.y);
      volumeMesh.position.y = (currentVolumeSize.z * renderState.heightScale) / 2;
    }
    needsRender = true;
    idleFrames = 0;
  }

  /* ====================================================================
   * Camera presets
   * ==================================================================== */

  function applyPreset(name) {
    const targetY = controls.target.y;
    if (name === 'top') {
      camera.position.set(0.1, targetY + 150, 0.1);
    } else if (name === 'inflow') {
      camera.position.set(92, targetY + 26, -74);
    } else {
      camera.position.set(98, targetY + 40, -82);
    }
    controls.update();
    needsRender = true;
    idleFrames = 0;
  }

  /* ====================================================================
   * Modal open / close
   * ==================================================================== */

  function showLoader() { loaderEl.classList.remove('hidden'); }
  function hideLoader() { loaderEl.classList.add('hidden'); }

  function showError(msg) {
    let errEl = viewport.querySelector('.storm3d-error');
    if (!errEl) {
      errEl = document.createElement('div');
      errEl.className = 'storm3d-error';
      viewport.appendChild(errEl);
    }
    errEl.textContent = msg;
    errEl.classList.remove('hidden');
  }
  function hideError() {
    const e = viewport.querySelector('.storm3d-error');
    if (e) e.classList.add('hidden');
  }

  function openModal() {
    modal.classList.remove('hidden');
    showLoader();
    hideError();
    statusEl.textContent = 'Loading...';
  }

  function closeModal() {
    modal.classList.add('hidden');
    stopAnimate();
  }

  /* ====================================================================
   * Full flow: crosshair → click → fetch → render
   * ==================================================================== */

  function clearSelectionBox() {
    if (selectionBoxEl) {
      selectionBoxEl.remove();
      selectionBoxEl = null;
    }
  }

  function setLeafletInteractionEnabled(enabled) {
    if (typeof leafletMap === 'undefined' || !leafletMap) return;
    const method = enabled ? 'enable' : 'disable';
    leafletMap.dragging?.[method]?.();
    leafletMap.boxZoom?.[method]?.();
    leafletMap.doubleClickZoom?.[method]?.();
    leafletMap.keyboard?.[method]?.();
    if (enabled) {
      leafletMap.scrollWheelZoom?.enable?.();
      leafletMap.touchZoom?.enable?.();
    } else {
      leafletMap.scrollWheelZoom?.disable?.();
      leafletMap.touchZoom?.disable?.();
    }
  }

  function exitCrosshair() {
    crosshairMode = false;
    boxSelectStartPoint = null;
    document.body.classList.remove('storm3d-crosshair-active');
    banner.classList.add('hidden');
    clearSelectionBox();
    setLeafletInteractionEnabled(true);
    if (boxSelectMoveHandler) {
      window.removeEventListener('pointermove', boxSelectMoveHandler);
      boxSelectMoveHandler = null;
    }
    if (boxSelectUpHandler) {
      window.removeEventListener('pointerup', boxSelectUpHandler);
      window.removeEventListener('pointercancel', boxSelectUpHandler);
      boxSelectUpHandler = null;
    }
    if (boxSelectDownHandler && typeof leafletMap !== 'undefined' && leafletMap) {
      leafletMap.getContainer().removeEventListener('pointerdown', boxSelectDownHandler, true);
      boxSelectDownHandler = null;
    }
  }

  function pointFromClient(event) {
    const rect = leafletMap.getContainer().getBoundingClientRect();
    const x = clamp(event.clientX - rect.left, 0, rect.width);
    const y = clamp(event.clientY - rect.top, 0, rect.height);
    return { x, y };
  }

  function ensureSelectionBox() {
    if (selectionBoxEl) return selectionBoxEl;
    selectionBoxEl = document.createElement('div');
    selectionBoxEl.className = 'storm3d-selection-box';
    leafletMap.getContainer().appendChild(selectionBoxEl);
    return selectionBoxEl;
  }

  function updateSelectionBox(startPoint, endPoint) {
    const box = ensureSelectionBox();
    const left = Math.min(startPoint.x, endPoint.x);
    const top = Math.min(startPoint.y, endPoint.y);
    const width = Math.abs(endPoint.x - startPoint.x);
    const height = Math.abs(endPoint.y - startPoint.y);
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
    box.style.width = `${width}px`;
    box.style.height = `${height}px`;
  }

  function buildSelectionFromPoints(startPoint, endPoint) {
    const minX = Math.min(startPoint.x, endPoint.x);
    const maxX = Math.max(startPoint.x, endPoint.x);
    const minY = Math.min(startPoint.y, endPoint.y);
    const maxY = Math.max(startPoint.y, endPoint.y);
    const centerPoint = L.point((minX + maxX) * 0.5, (minY + maxY) * 0.5);
    const centerLatLng = leafletMap.containerPointToLatLng(centerPoint);
    const sw = leafletMap.containerPointToLatLng(L.point(minX, maxY));
    const ne = leafletMap.containerPointToLatLng(L.point(maxX, minY));
    const width = maxX - minX;
    const height = maxY - minY;
    if (width < 8 && height < 8) {
      return {
        center: { lat: centerLatLng.lat, lon: centerLatLng.lng },
        box: null,
      };
    }
    return {
      center: { lat: centerLatLng.lat, lon: centerLatLng.lng },
      box: {
        latMin: Math.min(sw.lat, ne.lat),
        lonMin: Math.min(sw.lng, ne.lng),
        latMax: Math.max(sw.lat, ne.lat),
        lonMax: Math.max(sw.lng, ne.lng),
      },
    };
  }

  async function loadSelection(selection) {
    lastClickLatLon = selection.center;
    lastSelectionBox = selection.box;
    openModal();

    try {
      await ensureThree();
      initScene();
      sizeRenderer();
      if (!animFrameId) animate();

      const site = (typeof state !== 'undefined' && state.radarSite) ? state.radarSite : 'KOAX';
      statusEl.textContent = selection.box
        ? `Fetching enhanced volume for ${site} selection...`
        : `Fetching enhanced volume for ${site}...`;
      const payload = await fetchVolumeForSelection(site, selection);
      updateMeta(payload);
      rebuildVolume(payload);
      hideLoader();
      const mode = payload.selection_mode === 'peak'
        ? 'dominant storm core'
        : payload.selection_mode === 'box'
          ? 'selected storm region'
          : 'selected storm';
      statusEl.textContent = `Raymarched ${mode} from ${payload.volume_meta?.sweep_count || '--'} tilts.`;
    } catch (err) {
      hideLoader();
      showError(err.message || 'Failed to build 3D volume');
      statusEl.textContent = `Error: ${err.message || err}`;
    }
  }

  function activateCrosshair() {
    if (crosshairMode) { exitCrosshair(); return; }
    crosshairMode = true;
    document.body.classList.add('storm3d-crosshair-active');
    banner.classList.remove('hidden');
    banner.textContent = 'Click and drag a box around the storm you want in 3D — ESC to cancel';
    setLeafletInteractionEnabled(false);
    if (typeof leafletMap === 'undefined' || !leafletMap) return;
    const container = leafletMap.getContainer();
    boxSelectDownHandler = (event) => {
      if (!crosshairMode) return;
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
      boxSelectStartPoint = pointFromClient(event);
      updateSelectionBox(boxSelectStartPoint, boxSelectStartPoint);

      boxSelectMoveHandler = (moveEvent) => {
        if (!crosshairMode || !boxSelectStartPoint) return;
        moveEvent.preventDefault();
        updateSelectionBox(boxSelectStartPoint, pointFromClient(moveEvent));
      };

      boxSelectUpHandler = async (upEvent) => {
        if (!crosshairMode || !boxSelectStartPoint) return;
        upEvent.preventDefault();
        const endPoint = pointFromClient(upEvent);
        const selection = buildSelectionFromPoints(boxSelectStartPoint, endPoint);
        exitCrosshair();
        await loadSelection(selection);
      };

      window.addEventListener('pointermove', boxSelectMoveHandler);
      window.addEventListener('pointerup', boxSelectUpHandler, { once: true });
      window.addEventListener('pointercancel', boxSelectUpHandler, { once: true });
    };

    container.addEventListener('pointerdown', boxSelectDownHandler, true);
  }

  async function reloadScan() {
    if (!lastClickLatLon) return;
    showLoader();
    hideError();
    statusEl.textContent = 'Reloading latest scan...';
    try {
      const site = (typeof state !== 'undefined' && state.radarSite) ? state.radarSite : 'KOAX';
      const payload = await fetchVolumeForSelection(site, {
        center: lastClickLatLon,
        box: lastSelectionBox,
      });
      updateMeta(payload);
      rebuildVolume(payload);
      hideLoader();
      statusEl.textContent = `Reloaded from ${payload.volume_meta?.sweep_count || '--'} tilts.`;
    } catch (err) {
      hideLoader();
      showError(err.message || 'Reload failed');
      statusEl.textContent = `Error: ${err.message || err}`;
    }
  }

  /* ====================================================================
   * Event wiring
   * ==================================================================== */

  // The sidebar launcher was removed; keep the modal working if one returns.
  document.getElementById('storm3dActivate')?.addEventListener('click', activateCrosshair);
  if (downloadBtn) {
    downloadBtn.addEventListener('click', async () => {
      try {
        await downloadStorm3dScreenshot();
      } catch (err) {
        showError(err.message || 'Download failed');
      }
    });
  }
  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      try {
        await copyStorm3dScreenshot();
      } catch (err) {
        showError(err.message || 'Copy failed');
      }
    });
  }
  document.getElementById('storm3dClose').addEventListener('click', closeModal);
  backdrop.addEventListener('click', closeModal);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (crosshairMode) { exitCrosshair(); e.stopPropagation(); return; }
      if (!modal.classList.contains('hidden')) { closeModal(); e.stopPropagation(); }
    }
  });

  sliderHeight.addEventListener('input', syncControls);
  sliderHaze.addEventListener('input', syncControls);
  sliderContrast.addEventListener('input', syncControls);
  sliderAutoSensitivity.addEventListener('input', syncControls);
  sliderVelocity.addEventListener('input', syncControls);
  sliderHail.addEventListener('input', syncControls);

  document.getElementById('s3dPresetOblique').addEventListener('click', () => applyPreset('oblique'));
  document.getElementById('s3dPresetTop').addEventListener('click', () => applyPreset('top'));
  document.getElementById('s3dPresetInflow').addEventListener('click', () => applyPreset('inflow'));
  document.getElementById('s3dReload').addEventListener('click', reloadScan);

})();
