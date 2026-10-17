import * as THREE from 'three';

const $ = (id) => document.getElementById(id);
const SITE_INFO = {
  KOAX: { name: 'Omaha', center: [-96.3668, 41.3203] },
  KUEX: { name: 'Hastings', center: [-98.4419, 40.3208] },
};
const state = {
  selected: 'both', live: true, playing: false, busy: false, generation: 0,
  frames: {}, volumes: {}, errors: {}, cache: new Map(),
  threshold: 10, opacity: .65, height: 1, rulers: true, warnings: true,
};
let map;
let warningTimer;
const layers = {};
const markers = {};
const formatTime = (time) => new Date(time).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
const activeSites = () => state.selected === 'both' ? Object.keys(SITE_INFO) : [state.selected];
const homeZoom = (both) => window.matchMedia('(max-width:600px)').matches ? (both ? 6.3 : 7.0) : (both ? 7.1 : 7.7);
function notice(message) { $('notice').textContent = message; }
async function getJSON(url) {
  const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(100000) });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || `Radar request failed (${response.status})`);
  }
  return response.json();
}

const vertexShader = `
precision highp float;
uniform mat4 clipMatrix;
in vec3 position;
void main() { gl_Position = clipMatrix * vec4(position + 0.5, 1.0); }
`;
const fragmentShader = `
precision highp float;
precision highp sampler3D;
uniform sampler3D radarVolume;
uniform sampler3D blockMaximum;
uniform vec3 blockDimensions;
uniform sampler2D palette;
uniform mat4 inverseClip;
uniform vec2 viewport;
uniform float threshold;
uniform float opacity;
uniform float paletteMin;
uniform float paletteMax;
uniform bool splitOverlap;
uniform vec2 otherSite;
uniform float otherRadius;
out vec4 fragColor;
void main() {
  vec2 ndc = gl_FragCoord.xy / viewport * 2.0 - 1.0;
  vec4 n = inverseClip * vec4(ndc, -1.0, 1.0);
  vec4 f = inverseClip * vec4(ndc, 1.0, 1.0);
  vec3 origin = n.xyz / n.w;
  vec3 direction = normalize(f.xyz / f.w - origin);
  vec3 safeDirection = mix(vec3(1e-7), direction, greaterThan(abs(direction), vec3(1e-7)));
  vec3 a = -origin / safeDirection;
  vec3 b = (vec3(1.0) - origin) / safeDirection;
  vec3 lo = min(a, b), hi = max(a, b);
  float nearT = max(max(lo.x, lo.y), lo.z);
  float farT = min(min(hi.x, hi.y), hi.z);
  nearT = max(nearT, 0.0);
  if (nearT >= farT) discard;
  float stepLength = (farT - nearT) / 128.0;
  float t = nearT + 0.5 * stepLength;
  vec4 accumulated = vec4(0.0);
  for (int i = 0; i < 256; i++) {
    if (t >= farT) break;
    vec3 p = origin + direction * t;
    float blockEncoded = texture(blockMaximum, p).r * 255.0;
    if (blockEncoded < 1.0 || blockEncoded * 0.5 - 32.0 < threshold) {
      vec3 cell = floor(clamp(p, 0.0, 0.999999) * blockDimensions);
      vec3 edge = (cell + step(vec3(0.0), direction)) / blockDimensions;
      vec3 edgeT = (edge - p) / safeDirection;
      float skip = min(min(edgeT.x, edgeT.y), edgeT.z);
      t += max(skip + 0.000001, 0.000001);
      continue;
    }
    t += stepLength;
    if (splitOverlap) {
      float ownDistance = distance(p.xy, vec2(0.5));
      float otherDistance = distance(p.xy, otherSite);
      if (otherDistance < otherRadius && otherDistance < ownDistance) continue;
    }
    float encoded = texture(radarVolume, p).r * 255.0;
    float dbz = encoded * 0.5 - 32.0;
    if (encoded < 1.0 || dbz < threshold) continue;
    vec4 color = texture(palette, vec2(clamp((dbz - paletteMin) / (paletteMax - paletteMin), 0.0, 1.0), 0.5));
    float density = mix(0.025, 0.32, smoothstep(0.0, 70.0, dbz));
    float alpha = 1.0 - exp(-opacity * density * stepLength * 192.0);
    accumulated.rgb += (1.0 - accumulated.a) * color.rgb * alpha;
    accumulated.a += (1.0 - accumulated.a) * alpha;
    if (accumulated.a > 0.97) break;
  }
  if (accumulated.a < 0.005) discard;
  fragColor = vec4(accumulated.rgb / max(accumulated.a, 0.001), accumulated.a);
}
`;

function makeVolumeLayer(site, manifest, data) {
  const scene = new THREE.Scene();
  const camera = new THREE.Camera();
  const texture = new THREE.Data3DTexture(data, ...manifest.dimensions);
  texture.format = THREE.RedFormat;
  texture.type = THREE.UnsignedByteType;
  texture.minFilter = texture.magFilter = THREE.NearestFilter;
  texture.unpackAlignment = 1;
  texture.needsUpdate = true;
  // Maxima in 4×4×4 blocks let the GPU skip empty or below-threshold space.
  const [nx, ny, nz] = manifest.dimensions;
  const bx = nx / 4, by = ny / 4, bz = nz / 4;
  const maxima = new Uint8Array(bx * by * bz);
  for (let z = 0; z < nz; z++) for (let y = 0; y < ny; y++) for (let x = 0; x < nx; x++) {
    const block = ((z >> 2) * by + (y >> 2)) * bx + (x >> 2);
    maxima[block] = Math.max(maxima[block], data[(z * ny + y) * nx + x]);
  }
  const maximumTexture = new THREE.Data3DTexture(maxima, bx, by, bz);
  maximumTexture.format = THREE.RedFormat;
  maximumTexture.type = THREE.UnsignedByteType;
  maximumTexture.minFilter = maximumTexture.magFilter = THREE.NearestFilter;
  maximumTexture.unpackAlignment = 1;
  maximumTexture.needsUpdate = true;
  const paletteData = new Uint8Array(manifest.palette.flat());
  const palette = new THREE.DataTexture(paletteData, 256, 1, THREE.RGBAFormat);
  palette.minFilter = palette.magFilter = THREE.LinearFilter;
  palette.needsUpdate = true;
  const uniforms = {
    radarVolume: { value: texture }, palette: { value: palette },
    blockMaximum: { value: maximumTexture }, blockDimensions: { value: new THREE.Vector3(bx, by, bz) },
    clipMatrix: { value: new THREE.Matrix4() }, inverseClip: { value: new THREE.Matrix4() },
    viewport: { value: new THREE.Vector2() }, threshold: { value: state.threshold },
    opacity: { value: state.opacity }, paletteMin: { value: manifest.palette_min },
    paletteMax: { value: manifest.palette_max }, splitOverlap: { value: false },
    otherSite: { value: new THREE.Vector2() }, otherRadius: { value: .5 },
  };
  const material = new THREE.RawShaderMaterial({
    glslVersion: THREE.GLSL3, vertexShader, fragmentShader, uniforms,
    side: THREE.BackSide, transparent: true, depthTest: false, depthWrite: false,
  });
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), material);
  mesh.frustumCulled = false;
  scene.add(mesh);
  const rulerVertices = [.5, .5, 0, .5, .5, 18288 / manifest.height_m];
  const labels = [];
  for (const feet of [0, 20000, 40000, 60000]) {
    const z = feet * .3048 / manifest.height_m;
    rulerVertices.push(.495, .5, z, .505, .5, z);
    const element = document.createElement('span');
    element.className = 'height-label';
    element.textContent = `${feet.toLocaleString()} ft`;
    $('heightLabels').appendChild(element);
    labels.push({ z, element });
  }
  const rulerGeometry = new THREE.BufferGeometry();
  rulerGeometry.setAttribute('position', new THREE.Float32BufferAttribute(rulerVertices, 3));
  const ruler = new THREE.LineSegments(rulerGeometry, new THREE.LineBasicMaterial({ color: 0xd5e5f1, transparent: true, opacity: .55, depthTest: false }));
  ruler.frustumCulled = false;
  const rulerScene = new THREE.Scene();
  rulerScene.add(ruler);
  const coordinate = maplibregl.MercatorCoordinate.fromLngLat([manifest.site_lon, manifest.site_lat], manifest.base_altitude_m);
  const units = coordinate.meterInMercatorCoordinateUnits();
  const span = 2 * manifest.radius_m * units;
  const model = new THREE.Matrix4();
  const layer = {
    id: `radar-volume-${site}`, type: 'custom', renderingMode: '3d',
    onAdd(mapInstance, gl) {
      this.renderer = new THREE.WebGLRenderer({ canvas: mapInstance.getCanvas(), context: gl });
      this.renderer.autoClear = false;
    },
    render(gl, args) {
      const visible = activeSites().includes(site) && state.volumes[site]?.key === manifest.key;
      labels.forEach(({ element }) => { element.hidden = !visible || !state.rulers; });
      if (!visible) return;
      const projection = new THREE.Matrix4().fromArray(args.defaultProjectionData.mainMatrix);
      model.makeScale(span, span, manifest.height_m * units * state.height);
      model.setPosition(coordinate.x - span / 2, coordinate.y - span / 2, coordinate.z);
      const clip = uniforms.clipMatrix.value.copy(projection).multiply(model);
      uniforms.inverseClip.value.copy(clip).invert();
      uniforms.viewport.value.set(gl.drawingBufferWidth, gl.drawingBufferHeight);
      uniforms.threshold.value = state.threshold;
      uniforms.opacity.value = state.opacity;
      const otherId = site === 'KOAX' ? 'KUEX' : 'KOAX';
      const other = state.volumes[otherId];
      uniforms.splitOverlap.value = state.selected === 'both' && Boolean(other && layers[otherId]);
      if (other) {
        const point = maplibregl.MercatorCoordinate.fromLngLat([other.site_lon, other.site_lat]);
        uniforms.otherSite.value.set((point.x - coordinate.x) / span + .5, (point.y - coordinate.y) / span + .5);
        uniforms.otherRadius.value = other.radius_m * point.meterInMercatorCoordinateUnits() / span;
      }
      this.renderer.resetState();
      this.renderer.render(scene, camera);
      if (state.rulers) {
        camera.projectionMatrix.copy(clip);
        this.renderer.render(rulerScene, camera);
        const width = map.getContainer().clientWidth, height = map.getContainer().clientHeight;
        for (const { z, element } of labels) {
          const point = new THREE.Vector4(.505, .5, z, 1).applyMatrix4(clip);
          const x = (point.x / point.w + 1) * width / 2;
          const y = (1 - point.y / point.w) * height / 2;
          element.hidden = point.w <= 0 || x < 0 || x > width || y < 65 || y > height - 100 || map.getPitch() < 15;
          element.style.transform = `translate(${x + 5}px, ${y - 8}px)`;
        }
      }
      this.renderer.resetState();
    },
    onRemove() {
      labels.forEach(({ element }) => element.remove());
      mesh.geometry.dispose(); material.dispose(); texture.dispose(); palette.dispose(); maximumTexture.dispose();
      rulerGeometry.dispose(); ruler.material.dispose(); this.renderer.dispose();
    },
  };
  return layer;
}

function setLegend(manifest) {
  const colors = [];
  for (let dbz = 0; dbz <= 80; dbz += 5) {
    const i = Math.max(0, Math.min(255, Math.round((dbz - manifest.palette_min) / (manifest.palette_max - manifest.palette_min) * 255)));
    const [r, g, b] = manifest.palette[i];
    colors.push(`rgb(${r},${g},${b}) ${dbz / 80 * 100}%`);
  }
  $('legendRamp').style.background = `linear-gradient(90deg,${colors.join(',')})`;
}

async function loadVolume(site, frame, generation) {
  const scan = frame.scan;
  const cacheKey = `${site}:${frame.key}`;
  let item = state.cache.get(cacheKey);
  if (!item) {
    const manifest = await getJSON(`/api/radar/map3d/${site}?scan=${encodeURIComponent(scan)}`);
    const response = await fetch(manifest.data_url, { signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error('This frame expired. Refresh to load the latest scan.');
    const data = new Uint8Array(await response.arrayBuffer());
    if (data.byteLength !== manifest.byte_length) throw new Error('Incomplete radar volume received. Please refresh.');
    item = { manifest, data };
    state.cache.set(cacheKey, item);
    // Retain at most six decoded frames, independent of playback duration.
    while (state.cache.size > 6) state.cache.delete(state.cache.keys().next().value);
  }
  if (generation !== state.generation) return;
  if (state.volumes[site]?.key === item.manifest.key && layers[site]) return;
  if (map.getLayer(`radar-volume-${site}`)) map.removeLayer(`radar-volume-${site}`);
  state.volumes[site] = item.manifest;
  const layer = makeVolumeLayer(site, item.manifest, item.data);
  layers[site] = layer;
  map.addLayer(layer);
  setLegend(item.manifest);
  delete state.errors[site];
}

function updateStatus() {
  const fragment = document.createDocumentFragment();
  for (const site of activeSites()) {
    const row = document.createElement('div'); row.className = 'site-row';
    const title = document.createElement('strong'); title.textContent = `${site} · ${SITE_INFO[site].name}`; row.appendChild(title);
    const text = document.createElement('span');
    const volume = state.volumes[site];
    if (state.errors[site]) { text.textContent = state.errors[site]; text.className = 'failed'; }
    else if (volume) {
      const age = Math.max(0, Math.round((Date.now() - Date.parse(volume.scan_time)) / 60000));
      text.textContent = `${formatTime(volume.scan_time)} · ${age} min old · ${volume.tilts.length} tilts`;
      if (age > 12 && state.live) { text.className = 'stale'; text.textContent += ' · delayed'; }
      const detail = document.createElement('span');
      detail.textContent = volume.occupied_voxels ? `Maximum observed reflectivity ${volume.max_dbz.toFixed(0)} dBZ` : 'No echoes at or above 0 dBZ in this scan';
      row.appendChild(text); row.appendChild(detail); fragment.appendChild(row); continue;
    } else text.textContent = 'Preparing full volume…';
    row.appendChild(text); fragment.appendChild(row);
  }
  $('siteStatus').replaceChildren(fragment);
  const delayed = activeSites().some(site => state.errors[site] || !state.volumes[site] || Date.now() - Date.parse(state.volumes[site].scan_time) > 12 * 60000);
  $('liveBadge').textContent = state.live ? (delayed ? 'DELAYED' : 'LIVE') : 'HISTORY';
  $('liveBadge').classList.toggle('historical', !state.live || delayed);
  $('goLive').classList.toggle('selected', state.live);
  for (const [site, marker] of Object.entries(markers)) marker.getElement().hidden = !activeSites().includes(site);
}

function referenceFrames() { return state.frames[state.selected === 'KUEX' ? 'KUEX' : 'KOAX'] || []; }
function syncTimeline() {
  const frames = referenceFrames();
  $('timeline').max = Math.max(0, frames.length - 1);
  if (state.live) $('timeline').value = Math.max(0, frames.length - 1);
  $('historyStatus').textContent = `${frames.length} recent scans`;
  $('play').disabled = frames.length < 2;
}

async function showFrame(index = null) {
  if (state.busy) return;
  state.busy = true;
  const generation = ++state.generation;
  notice(state.live ? 'Updating complete radar volumes…' : 'Loading recorded radar scan…');
  const reference = index === null ? null : referenceFrames()[index];
  const selected = activeSites();
  try {
    await Promise.all(selected.map(async (site) => {
      let frame = state.frames[site]?.at(-1);
      if (reference) {
        frame = [...(state.frames[site] || [])].sort((a, b) => Math.abs(Date.parse(a.scan_time) - Date.parse(reference.scan_time)) - Math.abs(Date.parse(b.scan_time) - Date.parse(reference.scan_time)))[0];
        if (frame && Math.abs(Date.parse(frame.scan_time) - Date.parse(reference.scan_time)) > 6 * 60000) frame = null;
      }
      if (!frame) {
        state.errors[site] = 'No complete scan available for this time';
        if (map.getLayer(`radar-volume-${site}`)) map.removeLayer(`radar-volume-${site}`);
        delete layers[site]; delete state.volumes[site]; return;
      }
      try { await loadVolume(site, frame, generation); }
      catch (error) { state.errors[site] = `${error.message}${state.volumes[site] ? ' Showing previous scan.' : ''}`; }
    }));
    $('frameTime').textContent = reference ? formatTime(reference.scan_time) : 'Latest complete scans';
    const errors = selected.filter(site => state.errors[site]);
    notice(errors.length ? 'Some radar data is unavailable. See station status.' : '');
    updateStatus(); map.triggerRepaint();
  } finally { state.busy = false; }
}

async function refresh() {
  if (state.busy) return;
  await Promise.all(Object.keys(SITE_INFO).map(async (site) => {
    try {
      const result = await getJSON(`/api/radar/map3d/${site}/frames`);
      state.frames[site] = result.frames.reverse();
      delete state.errors[site];
    } catch (error) { state.errors[site] = error.message; }
  }));
  syncTimeline();
  if (state.live) await showFrame();
  else updateStatus();
}

function stopPlayback() { state.playing = false; $('play').textContent = '▶'; $('play').setAttribute('aria-label', 'Play recent scans'); }
async function play() {
  if (state.playing) { stopPlayback(); return; }
  if (state.busy) return;
  state.playing = true; state.live = false; $('play').textContent = 'Ⅱ'; $('play').setAttribute('aria-label', 'Pause playback');
  let index = Number($('timeline').value);
  if (index >= referenceFrames().length - 1) index = 0;
  while (state.playing) {
    $('timeline').value = index;
    await showFrame(index);
    await new Promise(resolve => setTimeout(resolve, 1100));
    if (!state.playing) break;
    index = (index + 1) % Math.max(1, referenceFrames().length);
  }
}

async function loadWarnings() {
  try {
    const data = await getJSON('/api/alerts');
    const features = (data.features || []).filter(f => f.geometry && /Tornado Warning|Severe Thunderstorm Warning|Flash Flood Warning/.test(f.properties?.event || ''));
    map.getSource('warnings')?.setData({ type: 'FeatureCollection', features });
    $('warningStatus').textContent = `Warning feed checked ${formatTime(new Date().toISOString())}`;
  } catch { $('warningStatus').textContent = 'Warning feed unavailable · polygons may be outdated'; }
}

function bindControls() {
  const phone = window.matchMedia('(max-width:600px)');
  const setPanel = (open) => {
    $('radarControlsPanel').hidden = !open;
    $('controlsToggle').setAttribute('aria-expanded', String(open));
  };
  setPanel(!phone.matches);
  phone.addEventListener('change', () => setPanel(!phone.matches));
  $('controlsToggle').addEventListener('click', () => setPanel($('radarControlsPanel').hidden));
  for (const button of document.querySelectorAll('[data-site]')) {
    button.setAttribute('aria-pressed', String(button.dataset.site === state.selected));
    button.addEventListener('click', async () => {
      if (state.busy) return;
      stopPlayback(); state.selected = button.dataset.site; state.live = true;
      document.querySelectorAll('[data-site]').forEach(b => { b.classList.toggle('selected', b === button); b.setAttribute('aria-pressed', String(b === button)); });
      map.flyTo({ center: state.selected === 'both' ? [-97.5, 40.8] : SITE_INFO[state.selected].center, zoom: homeZoom(state.selected === 'both'), pitch: 62, duration: 1000 });
      syncTimeline(); updateStatus(); map.triggerRepaint(); await showFrame();
    });
  }
  for (const key of ['threshold', 'opacity', 'height']) $('' + key).addEventListener('input', (event) => {
    const value = Number(event.target.value);
    state[key] = key === 'opacity' ? value / 100 : value;
    $({ threshold: 'thresholdValue', opacity: 'opacityValue', height: 'heightValue' }[key]).textContent = key === 'threshold' ? `${value} dBZ` : key === 'opacity' ? `${value}%` : `${value}×`;
    map.triggerRepaint();
  });
  $('rulers').addEventListener('change', (event) => {
    state.rulers = event.target.checked; $('heightLabels').hidden = !state.rulers; map.triggerRepaint();
  });
  $('imagery').addEventListener('change', (event) => {
    map.setLayoutProperty('imagery', 'visibility', event.target.checked ? 'visible' : 'none');
  });
  $('warnings').addEventListener('change', (event) => {
    state.warnings = event.target.checked;
    for (const id of ['warning-fill', 'warning-line']) map.setLayoutProperty(id, 'visibility', state.warnings ? 'visible' : 'none');
  });
  $('oblique').addEventListener('click', () => map.easeTo({ pitch: 65, bearing: -12, duration: 700 }));
  $('top').addEventListener('click', () => map.easeTo({ pitch: 0, bearing: 0, duration: 700 }));
  $('refresh').addEventListener('click', () => { stopPlayback(); state.live = true; refresh(); loadWarnings(); });
  $('play').addEventListener('click', play);
  $('timeline').addEventListener('input', () => {
    if (state.busy) return;
    stopPlayback(); state.live = false; showFrame(Number($('timeline').value));
  });
  $('goLive').addEventListener('click', () => { if (state.busy) return; stopPlayback(); state.live = true; refresh(); });
}

async function init() {
  if (!window.maplibregl) throw new Error('The 3D map library could not load. Please refresh.');
  const canvas = document.createElement('canvas');
  if (!canvas.getContext('webgl2')) throw new Error('This view requires WebGL 2. Enable hardware acceleration or try another browser.');
  map = new maplibregl.Map({
    container: 'map', center: [-97.5, 40.8], zoom: homeZoom(true), pitch: 62, bearing: -12, maxPitch: 80,
    minZoom: 5, maxZoom: 12, maxBounds: [[-106, 34], [-89, 47]],
    canvasContextAttributes: { antialias: true, preserveDrawingBuffer: true },
    pixelRatio: Math.min(window.devicePixelRatio || 1, 1.5),
    style: { version: 8, sources: {
      basemap: { type: 'raster', tiles: ['https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png?key=cb1_2l3n_1_eb188b07acba78da03ca3c9c'], tileSize: 256, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> © <a href="https://carto.com/attributions">CARTO</a>' },
      imagery: { type: 'raster', tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'], tileSize: 256, attribution: 'Imagery © Esri, Maxar, Earthstar Geographics and the GIS User Community' },
    }, layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#101c2a' } }, { id: 'basemap', type: 'raster', source: 'basemap' }, { id: 'imagery', type: 'raster', source: 'imagery', layout: { visibility: 'none' }, paint: { 'raster-brightness-max': .7, 'raster-saturation': -.25 } }] },
  });
  // Expose a small read-only diagnostics surface for browser verification.
  window.radarMap3d = { map, state };
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'imperial' }), 'bottom-left');
  map.on('error', event => {
    if (event.error?.message?.includes('Failed to fetch')) notice('Map tiles could not load. Radar observations may still be available.');
  });
  await new Promise(resolve => map.once('load', resolve));
  map.addSource('warnings', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addLayer({ id: 'warning-fill', type: 'fill', source: 'warnings', paint: { 'fill-color': ['match', ['get', 'event'], 'Tornado Warning', '#ff375f', 'Flash Flood Warning', '#39e7a4', '#ffbe44'], 'fill-opacity': .06 } });
  map.addLayer({ id: 'warning-line', type: 'line', source: 'warnings', paint: { 'line-color': ['match', ['get', 'event'], 'Tornado Warning', '#ff375f', 'Flash Flood Warning', '#39e7a4', '#ffbe44'], 'line-width': 2 } });
  for (const [site, info] of Object.entries(SITE_INFO)) {
    const element = document.createElement('div'); element.className = 'site-marker'; element.textContent = site;
    markers[site] = new maplibregl.Marker({ element, anchor: 'left' }).setLngLat(info.center).addTo(map);
  }
  for (const [name, lng, lat] of [['Omaha', -95.998, 41.257], ['Lincoln', -96.682, 40.814], ['Hastings', -98.389, 40.586], ['Grand Island', -98.342, 40.926], ['Kearney', -99.081, 40.699], ['Norfolk', -97.417, 42.033], ['Columbus', -97.368, 41.430], ['Council Bluffs', -95.86, 41.261]]) {
    const element = document.createElement('div'); element.className = 'town-label'; element.textContent = name;
    new maplibregl.Marker({ element, anchor: 'bottom' }).setLngLat([lng, lat]).addTo(map);
  }
  bindControls();
  await refresh(); await loadWarnings();
  setInterval(() => { if (!document.hidden && state.live && !state.playing) refresh(); }, 30000);
  setInterval(updateStatus, 15000);
  warningTimer = setInterval(() => { if (!document.hidden) loadWarnings(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopPlayback(); else if (state.live) refresh(); });
}

init().catch(error => { notice(error.message); $('siteStatus').textContent = '3D map unavailable'; console.error(error); });
