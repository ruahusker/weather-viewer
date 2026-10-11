'use strict';

/** Flag old model initialization times without confusing them with valid time. */
function modelRunIsOld(runId, model, now = Date.now()) {
  if (!/^\d{10}$/.test(runId || '')) return false;
  const init = Date.UTC(+runId.slice(0, 4), +runId.slice(4, 6) - 1, +runId.slice(6, 8), +runId.slice(8, 10));
  const limitHours = ['hrrr', 'rap', 'rtma'].includes(model) ? 6 : 24;
  return now - init > limitHours * 3600000;
}


/** Prefer recent complete runs, then fresh partial data, before old archives. */
function prioritizeModelRuns(runs, model) {
  return [...runs.filter((run) => run.is_complete && !modelRunIsOld(run.run_id, model)),
    ...runs.filter((run) => !run.is_complete && !modelRunIsOld(run.run_id, model)),
    ...runs.filter((run) => modelRunIsOld(run.run_id, model))];
}



/* ═══════════════════════════════════════════════════════════════
   WX Viewer — Mobile PWA
   ═══════════════════════════════════════════════════════════════ */

const BASE = (() => {
  const p = window.location.pathname || '';
  if (p.startsWith('/wx/')) return '/wx';
  return '';
})();
const MODEL_IMAGE_VERSION = '20261008a';
const URL_PARAMS = new URLSearchParams(window.location.search || '');
const MOBILE_VIEWS = new Set(['radar', 'surface', 'models', 'satellite']);
const MOBILE_MODELS = new Set(['ecmwf', 'gdps', 'gefs', 'gfs', 'href', 'hrrr', 'nam12km', 'nam3km', 'rap', 'rrfs', 'rtma']);
const MOBILE_SATELLITES = new Set(['goes18', 'goes19']);
const MOBILE_SHARE_QUERY_KEYS = [
  'view', 'source', 'model', 'run', 'param', 'fxx', 'satellite', 'sat_product',
  'sat_region', 'site', 'product', 'tilt', 'frame', 'lat', 'lon', 'z',
];

function getMobileSharedConfig() {
  const cleanId = (value) => (/^[a-z0-9_]+$/i.test(value || '') ? value : null);
  const rawView = URL_PARAMS.get('view');
  const view = (MOBILE_VIEWS.has(rawView) ? rawView : URL_PARAMS.get('source') || '').toLowerCase();
  const site = (URL_PARAMS.get('site') || '').toUpperCase();
  const product = (URL_PARAMS.get('product') || '').toUpperCase();
  const lat = Number.parseFloat(URL_PARAMS.get('lat') || '');
  const lon = Number.parseFloat(URL_PARAMS.get('lon') || '');
  const zoom = Number.parseFloat(URL_PARAMS.get('z') || '');
  return {
    view: MOBILE_VIEWS.has(view) ? view : 'radar',
    model: MOBILE_MODELS.has(URL_PARAMS.get('model') || '') ? URL_PARAMS.get('model') : null,
    runId: /^\d{10}$/.test(URL_PARAMS.get('run') || '') ? URL_PARAMS.get('run') : null,
    param: cleanId(URL_PARAMS.get('param')),
    fxx: URL_PARAMS.has('fxx') ? URL_PARAMS.get('fxx') : null,
    satellite: MOBILE_SATELLITES.has(URL_PARAMS.get('satellite') || '')
      ? URL_PARAMS.get('satellite')
      : null,
    satProduct: cleanId(URL_PARAMS.get('sat_product')),
    satRegion: cleanId(URL_PARAMS.get('sat_region')) || '',
    radarSite: /^[A-Z0-9]{3,4}$/.test(site) ? site : null,
    radarProduct: /^[A-Z0-9_]{2,12}$/.test(product) ? product : null,
    radarTilt: Number.parseFloat(URL_PARAMS.get('tilt') || ''),
    frame: URL_PARAMS.get('frame') || null,
    mapView: Number.isFinite(lat) && lat >= -90 && lat <= 90
      && Number.isFinite(lon) && lon >= -180 && lon <= 180
      && Number.isFinite(zoom) && zoom >= 0 && zoom <= 20
      ? { lat, lon, zoom }
      : null,
  };
}

// ── State ──────────────────────────────────────────────────────
const S = {
  tab: 'home',

  // Models
  model: 'hrrr',
  runId: null,
  modelRuns: [],  // cached list of available runs
  modelAvailability: {},
  vectorAvailability: {},
  paletteVersion: null,
  param: 'temp_2m',
  modelParamGroupsOpen: {},
  fxx: 0,
  fxxList: [],
  overlayLayer: null,
  overlayBounds: null,
  preloaded: {},
  isPlaying: false,
  playTimer: null,
  speedIdx: 0,  // 0=1x, 1=2x, 2=5x

  // Satellite
  sat: 'goes19',
  satProduct: 'ir',
  satRegion: '',
  satScope: 'national',
  satFrames: [],
  satIdx: 0,
  satAutoFitPending: false,

  // Radar
  radarSite: 'KOAX',
  radarProduct: 'REF',
  radarTilt: 0.5,
  radarFrames: [],
  radarIdx: 0,
  radarTilts: [0.5],  // available tilts for current site
  radarSites: [],  // cached from API

  // Home
  homeRadarFrames: [],
  homeIdx: 0,

  // Surface
  surfaceFrames: [],
  surfaceIdx: 0,
  surfacePayloadCache: {},

  // Alerts
  alertsOn: true,
  alertsLayer: null,
  spcLayer: null,
  mcdLayer: null,
  spcOn: true,
  chasersOn: false,
  chaserLayer: null,
  featuredChaserLayer: null,

  // Boundaries
  statesLayer: null,
  countyLayers: [],
  coastlineLayers: [],
  satelliteCityLayer: null,

  // Sounding
  soundingOpen: false,
  soundingData: null,
  soundingPoint: null,
  soundingSeq: 0,
};

const MOBILE_SHARED_CONFIG = getMobileSharedConfig();
S.tab = MOBILE_SHARED_CONFIG.view;
if (MOBILE_SHARED_CONFIG.model) S.model = MOBILE_SHARED_CONFIG.model;
if (MOBILE_SHARED_CONFIG.runId) S.runId = MOBILE_SHARED_CONFIG.runId;
if (MOBILE_SHARED_CONFIG.param) S.param = MOBILE_SHARED_CONFIG.param;
if (MOBILE_SHARED_CONFIG.fxx !== null) {
  S.fxx = /^\d+$/.test(MOBILE_SHARED_CONFIG.fxx)
    ? Number.parseInt(MOBILE_SHARED_CONFIG.fxx, 10)
    : MOBILE_SHARED_CONFIG.fxx;
}
if (MOBILE_SHARED_CONFIG.satellite) S.sat = MOBILE_SHARED_CONFIG.satellite;
if (MOBILE_SHARED_CONFIG.satProduct) S.satProduct = MOBILE_SHARED_CONFIG.satProduct;
S.satRegion = MOBILE_SHARED_CONFIG.satRegion;
S.satScope = S.satRegion ? 'regional' : 'national';
if (MOBILE_SHARED_CONFIG.radarSite) S.radarSite = MOBILE_SHARED_CONFIG.radarSite;
if (MOBILE_SHARED_CONFIG.radarProduct) S.radarProduct = MOBILE_SHARED_CONFIG.radarProduct;
if (Number.isFinite(MOBILE_SHARED_CONFIG.radarTilt)) S.radarTilt = MOBILE_SHARED_CONFIG.radarTilt;

let mobileSharedModelPending = MOBILE_SHARED_CONFIG.view === 'models';
let mobileSharedFramePending = MOBILE_SHARED_CONFIG.frame;
let mobileSharedMapPending = MOBILE_SHARED_CONFIG.mapView;
let mobileUrlSyncTimer = null;
let mobileUrlSyncEnabled = false;

let mobileDataRequestId = 0;
let mobileLegendRequestId = 0;
let mobileSurfaceRequestId = 0;

/** Reset the visible frame before loading a different dataset. */
function beginMobileDataLoad() {
  const requestId = ++mobileDataRequestId;
  ++mobileLegendRequestId;
  ++mobileSurfaceRequestId;
  pause();
  clearLoadQueue();
  clearOverlay();
  legendBar.classList.add('hidden');
  topInfo.textContent = 'Loading…';
  topValue.textContent = '';
  scrubLabel.textContent = 'Loading frames…';
  scrubFrameCount.textContent = '';
  setupScrubber(0, 0);
  return requestId;
}

function isCurrentMobileRequest(requestId, tab) {
  return requestId === mobileDataRequestId && S.tab === tab;
}

function showMobileDataMessage(message) {
  topInfo.textContent = message;
  scrubLabel.textContent = message;
  scrubFrameCount.textContent = '';
}

// Featured chasers — shown as pulsing dots
const FEATURED_CHASERS = ['Steve Blum', 'Chad Alcares', 'Mike Lachendro', 'Daniel Castro'];

// ── DOM refs ───────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const topInfo = $('topInfo');
const topValue = $('topValue');
const pillLabel = $('pillLabel');
const selectorPill = $('selectorPill');
const legendBar = $('legendBar');
const legendCanvas = $('legendCanvas');
const legendTicks = $('legendTicks');
const scrubberWrap = $('scrubber');
const scrubSlider = $('scrubSlider');
const scrubLabel = $('scrubLabel');
const scrubFrameCount = $('scrubFrameCount');
const playBtn = $('playBtn');
const playIcon = $('playIcon');
const pauseIcon = $('pauseIcon');
const shareBtn = $('shareBtn');
const shareSheet = $('shareSheet');
const bottomSheet = $('bottomSheet');
const sheetBackdrop = $('sheetBackdrop');
const sheetContent = $('sheetContent');
const soundingPanel = $('soundingPanel');
const soundingTitle = $('soundingTitle');
const soundingMeta = $('soundingMeta');
const soundingClose = $('soundingClose');
const soundingStatus = $('soundingStatus');
const soundingChartBox = $('soundingChartBox');
const soundingHodoBox = $('soundingHodoBox');
const soundingDiagBox = $('soundingDiagBox');

let leafletMap, baseTile, labelsTile;
const DEFAULT_CENTER = [39, -96];
const DEFAULT_ZOOM = 5;
const DEFAULT_RADAR_CENTER = [41.2565, -95.9345];
const DEFAULT_RADAR_ZOOM = 7;
const SPEEDS = [
  { label: '1x', ms: 200 },
  { label: '2x', ms: 80 },
  { label: '5x', ms: 40 },
];

function currentMobileFrame() {
  if (S.tab === 'satellite') return S.satFrames[S.satIdx]?.timestamp || null;
  if (S.tab === 'radar') return S.radarFrames[S.radarIdx]?.timestamp || null;
  return null;
}

function buildMobileShareUrl() {
  const url = new URL(window.location.href);
  for (const key of [...MOBILE_SHARE_QUERY_KEYS, 'desktop']) url.searchParams.delete(key);
  if (url.pathname.endsWith('/mobile.html')) url.pathname = `${BASE}/`;
  url.searchParams.set('view', S.tab);

  if (S.tab === 'models') {
    if (S.model) url.searchParams.set('model', S.model);
    if (S.runId) url.searchParams.set('run', S.runId);
    if (S.param) url.searchParams.set('param', S.param);
    if (S.fxx !== null && S.fxx !== undefined) url.searchParams.set('fxx', String(S.fxx));
  } else if (S.tab === 'satellite') {
    if (S.sat) url.searchParams.set('satellite', S.sat);
    if (S.satProduct) url.searchParams.set('sat_product', S.satProduct);
    if (S.satRegion) url.searchParams.set('sat_region', S.satRegion);
  } else if (S.tab === 'radar') {
    if (S.radarSite) url.searchParams.set('site', S.radarSite);
    if (S.radarProduct) url.searchParams.set('product', S.radarProduct);
    if (Number.isFinite(S.radarTilt)) url.searchParams.set('tilt', String(S.radarTilt));
  }

  const frame = currentMobileFrame();
  if (frame) url.searchParams.set('frame', frame);

  if (leafletMap && ['radar', 'surface'].includes(S.tab)) {
    const center = leafletMap.getCenter();
    const zoom = leafletMap.getZoom();
    if (center && Number.isFinite(zoom)) {
      url.searchParams.set('lat', center.lat.toFixed(4));
      url.searchParams.set('lon', center.lng.toFixed(4));
      url.searchParams.set('z', Number(zoom.toFixed(2)).toString());
    }
  }
  url.hash = '';
  return url;
}

function syncMobileUrl() {
  if (!mobileUrlSyncEnabled) return;
  if (mobileUrlSyncTimer) clearTimeout(mobileUrlSyncTimer);
  mobileUrlSyncTimer = setTimeout(() => {
    mobileUrlSyncTimer = null;
    const url = buildMobileShareUrl();
    const target = `${url.pathname}${url.search}`;
    const current = `${window.location.pathname}${window.location.search}`;
    if (target !== current) history.replaceState(null, '', target);
  }, 120);
}

function restoreMobileSharedMap() {
  if (!leafletMap || !mobileSharedMapPending || MOBILE_SHARED_CONFIG.view !== S.tab) return false;
  const { lat, lon, zoom } = mobileSharedMapPending;
  mobileSharedMapPending = null;
  leafletMap.setView([lat, lon], zoom, { animate: false });
  return true;
}

function setMobileBasemapMode(mode = 'default') {
  const baseEl = baseTile?.getContainer?.();
  const labelsEl = labelsTile?.getContainer?.();
  if (baseEl) baseEl.style.display = '';
  if (labelsEl) labelsEl.style.display = '';
}

// ── Image preload cache ─────────────────────────────────────────
// Maps url → true (loaded/errored) | false (pending)
const _imgCache = new Map();
const _loadQueue = [];
let _loadActive = 0;
const PRELOAD_CONCURRENCY = 4;
let _productUrls = [];   // URLs belonging to the current product

function _pumpQueue() {
  while (_loadActive < PRELOAD_CONCURRENCY && _loadQueue.length) {
    const url = _loadQueue.shift();
    const s = _imgCache.get(url);
    if (s === 'ok' || s === 'error') continue;
    _loadActive++;
    if (window.PressureContours?.isUrl(url)) {
      window.PressureContours.load(url).then(
        () => { _imgCache.set(url, 'ok'); },
        () => { _imgCache.set(url, 'error'); },
      ).finally(() => { _loadActive--; _pumpQueue(); updateCacheStatus(); });
      continue;
    }
    const img = new Image();
    img.onload  = () => { _imgCache.set(url, 'ok');    _loadActive--; _pumpQueue(); updateCacheStatus(); };
    img.onerror = () => { _imgCache.set(url, 'error'); _loadActive--; _pumpQueue(); updateCacheStatus(); };
    img.src = url;
  }
}

// Queue urls for background loading. front=true puts them at the head.
function cacheFrames(urls, front = false) {
  const pending = urls.filter(u => u && !_imgCache.has(u));
  for (const u of pending) _imgCache.set(u, 'pending');
  if (front) _loadQueue.unshift(...pending);
  else _loadQueue.push(...pending);
  _pumpQueue();
}

// Returns true only if image loaded successfully (not error, not pending)
function isCached(url) {
  return url ? _imgCache.get(url) === 'ok' : false;
}

function isErrored(url) {
  return url ? _imgCache.get(url) === 'error' : false;
}

// Clear pending queue when switching products (cached images stay in browser)
function clearLoadQueue() {
  for (const url of _loadQueue) _imgCache.delete(url);
  _loadQueue.length = 0;
  _productUrls = [];
}

// Update the scrubber info row with loading progress or current frame position.
function updateCacheStatus() {
  if (!scrubFrameCount) return;
  if (S.tab === 'surface') {
    scrubFrameCount.textContent = S.surfaceFrames.length ? `${S.surfaceIdx + 1}/${S.surfaceFrames.length}` : '';
    scrubFrameCount.classList.remove('loading');
    return;
  }
  const total = _productUrls.length;
  if (!total) { scrubFrameCount.textContent = ''; scrubFrameCount.classList.remove('loading'); return; }
  const loaded = _productUrls.reduce((n, u) => { const s = _imgCache.get(u); return n + (s === 'ok' || s === 'error' ? 1 : 0); }, 0);
  if (loaded < total) {
    scrubFrameCount.textContent = `↓ ${loaded}/${total}`;
    scrubFrameCount.classList.add('loading');
    if (scrubLabel && scrubLabel.textContent === '--') scrubLabel.textContent = 'Loading frames…';
  } else {
    const curIdx = parseInt(scrubSlider.value);
    scrubFrameCount.textContent = `${curIdx + 1}/${total}`;
    scrubFrameCount.classList.remove('loading');
  }
}

// Return the image URL for frame index in the current tab
function frameUrl(idx) {
  if (S.tab === 'models') {
    const f = S.fxxList[idx];
    if (f == null) return null;
    return mobileModelFrameUrl(f);
  }
  if (S.tab === 'satellite') {
    const f = S.satFrames[idx];
    return f ? `${BASE}/rendered/${String(f.png_path||'').replace(/^\//,'')}` : null;
  }
  if (S.tab === 'radar') {
    const f = S.radarFrames[idx];
    return f ? `${BASE}/rendered/${String(f.png_path||'').replace(/^\//,'')}?palette=${f.palette_version || 1}` : null;
  }
  if (S.tab === 'home') {
    const f = S.homeRadarFrames[idx];
    if (!f) return null;
    const p = f.png_path || (f.image ? `home/${f.image}` : '');
    return p ? `${BASE}/rendered/${p.replace(/^\//,'')}` : null;
  }
  if (S.tab === 'surface') {
    return `surface:${S.surfaceFrames[idx]?.generated ?? idx}`;
  }
  return null;
}

// Aggressively preload all frames for the current product.
// Prioritises the next 6 frames ahead of current position.
function preloadAllFrames() {
  const cur = parseInt(scrubSlider.value);
  const count = parseInt(scrubSlider.max) + 1;
  _productUrls = [];
  for (let i = 0; i < count; i++) {
    const url = frameUrl(i);
    if (url) _productUrls.push(url);
  }
  const priority = _productUrls.filter((_, i) => i > cur && i <= cur + 6);
  const rest     = _productUrls.filter((_, i) => i <= cur || i > cur + 6);
  cacheFrames(priority, true);
  cacheFrames(rest, false);
  updateCacheStatus();
}

// ── Config ─────────────────────────────────────────────────────
const MODELS = {
  hrrr:   { label: 'HRRR', maxFxx: 48 },
  nam3km: { label: 'NAM 3km', maxFxx: 60 },
  nam12km: { label: 'NAM 12km', maxFxx: 84 },
  rap:    { label: 'RAP', maxFxx: 21 },
  rrfs:   { label: 'RRFS', maxFxx: 60 },
  href:   { label: 'HREF', maxFxx: 48 },
  gfs:    { label: 'GFS', maxFxx: 120 },
  gefs:   { label: 'GEFS', maxFxx: 240 },
  ecmwf:  { label: 'ECMWF', maxFxx: 144 },
  gdps:   { label: 'GDPS', maxFxx: 240 },
  rtma:   { label: 'RTMA', maxFxx: 0 },
};

const PARAMS = [
  { id: 'surface', group: 'Surface', items: [
    {"id": "temp_2m", "label": "Temp 2m", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs", "rtma"]},
    {"id": "dewpoint_2m", "label": "Dewpoint", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs", "rtma"]},
    {"id": "wind_speed", "label": "Wind", "models": ["ecmwf", "gdps", "gefs", "gfs", "hrrr", "nam12km", "nam3km", "rap", "rrfs", "rtma"]},
    {"id": "wind_gusts", "label": "Wind Gusts", "models": ["ecmwf", "gdps", "gfs", "hrrr", "nam12km", "nam3km", "rap", "rrfs", "rtma"]},
    {"id": "max_wind", "label": "Max 10m Wind", "models": ["hrrr", "rrfs"]},
    {"id": "mslp", "label": "MSLP", "models": ["ecmwf", "gdps", "gfs", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "psfc", "label": "Surface Pressure", "models": ["rtma"]},
    {"id": "pwat", "label": "PWAT", "models": ["ecmwf", "gefs", "gfs", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "cloud_cover", "label": "Cloud Cover", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs", "rtma"]},
    {"id": "pbl_height", "label": "PBL Height", "models": ["gfs", "hrrr", "nam3km", "rap", "rrfs"]},
    {"id": "rh_2m", "label": "2m RH", "models": ["rtma"]},
    {"id": "max_temp_1h", "label": "1hr Max Temp", "models": ["rrfs"]},
    {"id": "min_temp_1h", "label": "1hr Min Temp", "models": ["rrfs"]},
    {"id": "refl_1km", "label": "1km Reflectivity", "models": ["rrfs"]},
    {"id": "cape_ml", "label": "MLCAPE", "models": ["rrfs"]},
    {"id": "cin_ml", "label": "MLCIN", "models": ["rrfs"]},
    {"id": "cape_surface", "label": "Surface CAPE", "models": ["gdps"]},
    {"id": "cin_surface", "label": "Surface CIN", "models": ["gdps"]},
    {"id": "llmt", "label": "Low-Level Moisture Transport", "models": ["hrrr", "nam3km"]},
    {"id": "ascent", "label": "Forcing for Ascent", "models": ["hrrr", "nam3km"]},
    {"id": "hp_risk", "label": "Storm Obscuration / HP Risk", "models": ["hrrr", "nam3km"]},
    {"id": "weasd", "label": "Snow Water Eq.", "models": ["gfs", "hrrr", "rap", "rrfs"]},
    {"id": "cloud_cover_low", "label": "Low Cloud Cover", "models": ["gefs"]},
    {"id": "cloud_cover_mid", "label": "Mid Cloud Cover", "models": ["gefs"]},
    {"id": "cloud_cover_high", "label": "High Cloud Cover", "models": ["gefs"]},
  ]},
  { id: 'precip', group: 'Precipitation', items: [
    {"id": "reflectivity", "label": "Reflectivity", "models": ["gfs", "hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "precip", "label": "Precip", "models": ["ecmwf", "gfs", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "lightning", "label": "Lightning", "models": ["hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "echo_tops", "label": "Echo Tops", "models": ["hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "precip_6h", "label": "6hr Precip", "models": ["gefs"]},
  ]},
  { id: 'instability', group: 'Instability', items: [
    {"id": "mlcape", "label": "MLCAPE", "models": ["gfs", "hrrr", "nam12km", "nam3km", "rap"]},
    {"id": "mlcin", "label": "MLCIN", "models": ["gfs", "hrrr", "nam12km", "nam3km", "rap"]},
    {"id": "mucape", "label": "MUCAPE", "models": ["ecmwf", "gfs", "hrrr", "nam12km", "nam3km", "rap"]},
    {"id": "mucin", "label": "MUCIN", "models": ["gfs", "hrrr", "nam12km", "nam3km", "rap"]},
    {"id": "cape", "label": "SBCAPE", "models": ["gefs", "gfs", "href", "hrrr", "nam12km", "rap", "rrfs"]},
    {"id": "cin", "label": "SBCIN", "models": ["gfs", "href", "hrrr", "nam12km", "rap", "rrfs"]},
    {"id": "cape_03km", "label": "0-3km CAPE", "models": ["hrrr", "nam12km", "rap"]},
    {"id": "lifted_index", "label": "Lifted Index", "models": ["gfs", "hrrr", "nam12km", "nam3km", "rap"]},
    {"id": "lcl", "label": "LCL Height", "models": ["hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
  ]},
  { id: 'composites', group: 'Composites', items: [
    {"id": "stp", "label": "STP", "models": ["hrrr", "nam3km"]},
    {"id": "scp", "label": "Supercell Comp", "models": ["hrrr", "nam3km"]},
    {"id": "vil", "label": "VIL", "models": ["hrrr", "nam12km", "nam3km", "rrfs"]},
    {"id": "uh_25km", "label": "2-5km UH", "models": ["hrrr", "nam3km", "rrfs"]},
    {"id": "uh_02km", "label": "0-2km UH", "models": ["hrrr", "rrfs"]},
    {"id": "uh_03km", "label": "0-3km UH", "models": ["hrrr", "nam3km"]},
    {"id": "uh_25km_swath", "label": "2-5km UH Swath", "models": ["hrrr", "nam3km", "rrfs"]},
    {"id": "uh_03km_swath", "label": "0-3km UH Swath", "models": ["hrrr", "nam3km"]},
    {"id": "hail", "label": "Max Hail Size", "models": ["hrrr"]},
    {"id": "cti", "label": "Chase Target", "models": ["hrrr", "nam3km"]},
    {"id": "msi", "label": "Meso Strength", "models": ["hrrr"]},
    {"id": "smi", "label": "Storm Mode", "models": ["hrrr", "nam3km"]},
    {"id": "ship", "label": "Sig Hail", "models": ["hrrr", "nam3km"]},
    {"id": "dcp", "label": "Derecho Comp", "models": ["hrrr", "nam3km"]},
    {"id": "citi", "label": "CI Timing", "models": ["hrrr", "nam3km"]},
    {"id": "cvi", "label": "Chase Visibility", "models": ["hrrr", "nam3km"]},
  ]},
  { id: 'kinematics', group: 'Kinematics', items: [
    {"id": "srh_01km", "label": "0-1km SRH", "models": ["hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "srh_03km", "label": "0-3km SRH", "models": ["hrrr", "nam3km", "rap", "nam12km", "gfs", "href", "rrfs"]},
    {"id": "efhl", "label": "Eff. Helicity", "models": ["hrrr"]},
    {"id": "shear_06km", "label": "0-6km Shear", "models": ["hrrr", "nam3km", "rap", "nam12km", "rrfs"]},
    {"id": "shear_01km", "label": "0-1km Shear", "models": ["hrrr", "nam12km", "rrfs"]},
  ]},
  { id: 'winter', group: 'Winter', items: [
    {"id": "visibility", "label": "Visibility", "models": ["gfs", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs", "rtma"]},
    {"id": "ceil", "label": "Ceiling Height", "models": ["rrfs"]},
    {"id": "snow_depth", "label": "Snow Depth", "models": ["ecmwf", "gdps", "gfs", "hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "snowfall", "label": "Snowfall", "models": ["ecmwf"]},
    {"id": "frozen_pct", "label": "Frozen Precip %", "models": ["hrrr", "rrfs"]},
    {"id": "frzlvl", "label": "Freezing Level", "models": ["gfs", "hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "kuchera_6hr", "label": "6hr Kuchera Snow", "models": ["hrrr"]},
    {"id": "kuchera_12hr", "label": "12hr Kuchera Snow", "models": ["hrrr"]},
    {"id": "snow10_12hr", "label": "12hr 10:1 Snow", "models": ["hrrr"]},
    {"id": "snowfall_6h", "label": "6hr Snowfall", "models": ["gefs"]},
  ]},
  { id: 'pressure', group: 'Pressure Levels', items: [
    {"id": "temp_925mb", "label": "925mb Temp", "models": ["gefs", "gfs", "hrrr", "nam12km", "rap", "rrfs"]},
    {"id": "temp_850mb", "label": "850mb Heights / Temp", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "hrrr", "nam3km", "nam12km", "rap", "rrfs"]},
    {"id": "dewpoint_850mb", "label": "850mb Heights / Dewpoint", "models": ["hrrr", "nam3km"]},
    {"id": "rh_850mb", "label": "850mb Heights / RH", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "nam3km", "nam12km", "rap", "rrfs"]},
    {"id": "wind_850mb", "label": "850mb Heights / Wind", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "temp_700mb", "label": "700mb Heights / Temp", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "hrrr", "nam3km", "nam12km", "rap", "rrfs"]},
    {"id": "rh_700mb", "label": "700mb Heights / RH", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "nam3km", "nam12km", "rap", "rrfs"]},
    {"id": "omega_700mb", "label": "700mb Heights / Vert Vel", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "wind_700mb", "label": "700mb Heights / Wind", "models": ["gdps", "gefs", "href", "hrrr", "nam3km", "rrfs"]},
    {"id": "temp_500mb", "label": "500mb Heights / Temp", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "hrrr", "nam3km", "nam12km", "rap", "rrfs"]},
    {"id": "vort_500mb", "label": "500mb Heights / Vorticity", "models": ["gdps", "hrrr", "nam12km", "nam3km", "rap", "rrfs"]},
    {"id": "wind_500mb", "label": "500mb Heights / Wind", "models": ["ecmwf", "gdps", "gefs", "gfs", "href", "hrrr", "nam3km", "rap", "rrfs"]},
    {"id": "wind_300mb", "label": "300mb Heights / Wind", "models": ["ecmwf", "gdps", "gefs", "gfs", "hrrr", "rap", "rrfs"]},
    {"id": "wind_250mb", "label": "250mb Heights / Wind", "models": ["gefs", "gfs", "hrrr", "nam3km", "rap", "rrfs"]},
    {"id": "wind_925mb", "label": "925mb Wind", "models": ["gefs", "rrfs"]},
    {"id": "rh_500mb", "label": "500mb Heights / RH", "models": ["gefs", "rrfs"]},
    {"id": "temp_300mb", "label": "300mb Heights / Temp", "models": ["gefs", "rrfs"]},
    {"id": "temp_250mb", "label": "250mb Heights / Temp", "models": ["gefs", "rrfs"]},
    {"id": "temp_200mb", "label": "200mb Heights / Temp", "models": ["gefs"]},
    {"id": "wind_200mb", "label": "200mb Heights / Wind", "models": ["gefs"]},
    {"id": "gh_500mb_anomaly", "label": "500mb Hgt Anomaly", "models": ["gefs"]},
  ]},
  { id: 'ensemble', group: 'Ensemble', items: [
    {"id": "gh_500mb_spread", "label": "500mb Hgt Spread", "models": ["gefs"]},
    {"id": "refl_probability_40dbz", "label": "40dBZ Probability", "models": ["href"]},
    {"id": "precip_probability", "label": "Precip Probability", "models": ["href"]},
    {"id": "precip_probability_6h_05in", "label": "6hr &gt;0.5&quot; Prob", "models": ["gefs"]},
    {"id": "precip_probability_6h_1in", "label": "6hr &gt;1&quot; Prob", "models": ["gefs"]},
    {"id": "cape_probability", "label": "CAPE Probability", "models": ["href"]},
    {"id": "cape_probability_1000", "label": "CAPE &gt;1000 Prob", "models": ["gefs"]},
    {"id": "uh_25km_probability", "label": "2-5km UH Probability", "models": ["href"]},
    {"id": "snowfall_probability_6h_1in", "label": "6hr Snow &gt;1&quot; Prob", "models": ["gefs"]},
    {"id": "pwat_probability_1in", "label": "PWAT &gt;1&quot; Prob", "models": ["gefs"]},
  ]},
];

const SAT_PRODUCTS = [
  { group: 'Visible/Near-IR', items: [
    { id: 'vis_1km', label: 'Visible 1km', detail: 'Band 2 CMIP' },
    { id: 'vis', label: 'Visible Red', detail: 'Band 2 enhanced' },
    { id: 'vis_blue', label: 'Visible Blue', detail: 'Band 1 enhanced' },
    { id: 'veggie', label: 'Veggie', detail: 'Band 3 near-IR' },
    { id: 'cirrus', label: 'Cirrus', detail: 'Band 4' },
    { id: 'snow_ice', label: 'Snow/Ice', detail: 'Band 5' },
    { id: 'cloud_particle', label: 'Cloud Particle', detail: 'Band 6 shortwave IR' },
  ]},
  { group: 'Infrared', items: [
    { id: 'ir_native', label: 'Clean IR Native', detail: 'Band 13 CMIP' },
    { id: 'ir', label: 'Clean IR', detail: 'Band 13 enhanced' },
    { id: 'ir_14', label: 'IR Band 14', detail: 'Longwave IR' },
    { id: 'ir_dirty', label: 'Dirty IR', detail: 'Band 15' },
    { id: 'co2', label: 'CO2', detail: 'Band 16 absorption' },
    { id: 'cloud_top', label: 'Cloud Top', detail: 'Band 11' },
    { id: 'ozone', label: 'Ozone', detail: 'Band 12' },
    { id: 'sw_ir', label: 'Shortwave IR', detail: 'Band 7 fire/fog' },
  ]},
  { group: 'Water Vapor', items: [
    { id: 'upper_wv', label: 'Upper WV', detail: 'Band 8' },
    { id: 'wv', label: 'Mid-Level WV', detail: 'Band 9' },
    { id: 'lower_wv', label: 'Lower WV', detail: 'Band 10' },
  ]},
  { group: 'RGB Composites', items: [
    { id: 'natural_color', label: 'Natural Color', detail: 'RGB composite' },
    { id: 'day_cloud_phase', label: 'Day Cloud Phase', detail: 'RGB composite' },
  ]},
  { group: 'Derived', items: [
    { id: 'pwat', label: 'PWAT', detail: 'Total precipitable water' },
    { id: 'sst', label: 'Sea Surface Temp', detail: 'Derived product' },
    { id: 'rain_rate', label: 'Rainfall Rate', detail: 'QPE estimate' },
    { id: 'stability', label: 'Stability', detail: 'Lifted index' },
    { id: 'fire', label: 'Fire/Hot Spot', detail: 'Thermal anomalies' },
  ]},
];

const SATELLITE_REGION_NAMES = {
  central_plains: 'Central Plains',
  northern_plains: 'Northern Plains',
  upper_midwest: 'Upper Midwest',
  southern_plains: 'Southern Plains',
  southeast: 'Southeast',
  northeast: 'Northeast',
  southwest: 'Southwest',
  west_coast: 'West Coast',
};

const SATELLITE_REGION_SOURCES = {
  central_plains: 'goes19',
  northern_plains: 'goes19',
  upper_midwest: 'goes19',
  southern_plains: 'goes19',
  southeast: 'goes19',
  northeast: 'goes19',
  southwest: 'goes18',
  west_coast: 'goes18',
};

const SATELLITE_SCOPE_DEFAULTS = {
  national: { product: 'ir', region: '' },
  regional: { product: 'vis_1km', region: 'central_plains' },
};

const MOBILE_SATELLITE_REFERENCE_CITIES = [
  ['Seattle', 47.6062, -122.3321],
  ['Portland', 45.5152, -122.6784],
  ['San Francisco', 37.7749, -122.4194],
  ['Los Angeles', 34.0522, -118.2437],
  ['San Diego', 32.7157, -117.1611],
  ['Las Vegas', 36.1699, -115.1398],
  ['Phoenix', 33.4484, -112.0740],
  ['Denver', 39.7392, -104.9903],
  ['Dallas', 32.7767, -96.7970],
  ['Houston', 29.7604, -95.3698],
  ['Minneapolis', 44.9778, -93.2650],
  ['Chicago', 41.8781, -87.6298],
  ['St. Louis', 38.6270, -90.1994],
  ['Atlanta', 33.7490, -84.3880],
  ['Miami', 25.7617, -80.1918],
  ['Washington', 38.9072, -77.0369],
  ['New York', 40.7128, -74.0060],
  ['Philadelphia', 39.9526, -75.1652],
  ['Boston', 42.3601, -71.0589],
];

const SAT_REGIONAL_PRODUCTS = [
  { id: 'vis_1km', label: 'Visible 1km', detail: 'Band 2 CMIP' },
  { id: 'ir_native', label: 'Clean IR Native', detail: 'Band 13 CMIP' },
  { id: 'ir_14', label: 'IR Band 14', detail: 'Longwave IR' },
  { id: 'veggie', label: 'Veggie', detail: 'Band 3 Near-IR' },
  { id: 'cloud_particle', label: 'Cloud Particle', detail: 'Band 6 Shortwave IR' },
];

const SAT_REGIONAL_PRODUCT_IDS = new Set(SAT_REGIONAL_PRODUCTS.map((product) => product.id));

const RADAR_PRODUCTS = [
  // Base moments
  { id: 'REF',  label: 'Reflectivity', legend: 'radar_reflectivity' },
  { id: 'VEL',  label: 'Velocity', legend: 'radar_velocity' },
  { id: 'SRM',  label: 'Storm-Rel Vel', legend: 'radar_srm' },
  { id: 'SW',   label: 'Spectrum Width', legend: 'radar_sw' },
  // Dual-pol
  { id: 'CC',   label: 'Corr. Coeff.', legend: 'radar_cc' },
  { id: 'ZDR',  label: 'Diff. Refl.', legend: 'radar_zdr' },
  { id: 'KDP',  label: 'Spec. Phase', legend: 'radar_kdp' },
  // Derived
  { id: 'AZS',  label: 'Az. Shear', legend: 'radar_azs' },
  { id: 'DIV',  label: 'Divergence', legend: 'radar_div' },
  { id: 'CREF', label: 'Composite Refl', legend: 'radar_cref' },
  { id: 'ETOP', label: 'Echo Tops', legend: 'radar_etop' },
  { id: 'VIL',  label: 'VIL', legend: 'radar_vil' },
  { id: 'HAIL', label: 'Hail Prob.', legend: 'radar_hail' },
  { id: 'RAIN', label: 'Rainfall Rate', legend: 'radar_rain' },
  { id: 'TDS',  label: 'Debris Sig.', legend: 'radar_tds' },
];

const MOBILE_PREFERRED_RADAR_TILTS = [0.5, 0.9, 1.3, 1.8, 2.4, 3.4];

// Radar site → city, state lookup for search
const ST = {AL:'Alabama',AK:'Alaska',AZ:'Arizona',AR:'Arkansas',CA:'California',CO:'Colorado',CT:'Connecticut',DE:'Delaware',FL:'Florida',GA:'Georgia',HI:'Hawaii',ID:'Idaho',IL:'Illinois',IN:'Indiana',IA:'Iowa',KS:'Kansas',KY:'Kentucky',LA:'Louisiana',ME:'Maine',MD:'Maryland',MA:'Massachusetts',MI:'Michigan',MN:'Minnesota',MS:'Mississippi',MO:'Missouri',MT:'Montana',NE:'Nebraska',NV:'Nevada',NH:'New Hampshire',NJ:'New Jersey',NM:'New Mexico',NY:'New York',NC:'North Carolina',ND:'North Dakota',OH:'Ohio',OK:'Oklahoma',OR:'Oregon',PA:'Pennsylvania',PR:'Puerto Rico',SC:'South Carolina',SD:'South Dakota',TN:'Tennessee',TX:'Texas',UT:'Utah',VT:'Vermont',VA:'Virginia',WA:'Washington',WV:'West Virginia',WI:'Wisconsin',WY:'Wyoming'};
const RADAR_CITIES = {
  KABR:'Aberdeen, SD',KABX:'Albuquerque, NM',KAKQ:'Wakefield, VA',KAMA:'Amarillo, TX',
  KAMX:'Miami, FL',KAPX:'Gaylord, MI',KARX:'La Crosse, WI',KATX:'Seattle, WA',
  KBBX:'Beale AFB, CA',KBGM:'Binghamton, NY',KBHX:'Eureka, CA',KBIS:'Bismarck, ND',
  KBLX:'Billings, MT',KBMX:'Birmingham, AL',KBOX:'Boston, MA',KBRO:'Brownsville, TX',
  KBUF:'Buffalo, NY',KBYX:'Key West, FL',KCAE:'Columbia, SC',KCBW:'Caribou, ME',
  KCBX:'Boise, ID',KCCX:'State College, PA',KCLE:'Cleveland, OH',KCLX:'Charleston, SC',
  KCRP:'Corpus Christi, TX',KCXX:'Burlington, VT',KCYS:'Cheyenne, WY',KDAX:'Sacramento, CA',
  KDDC:'Dodge City, KS',KDFX:'Laughlin AFB, TX',KDGX:'Jackson, MS',KDIX:'Philadelphia, PA',
  KDLH:'Duluth, MN',KDMX:'Des Moines, IA',KDOX:'Dover AFB, DE',KDTX:'Detroit, MI',
  KDVN:'Davenport, IA',KDYX:'Dyess AFB, TX',KEAX:'Kansas City, MO',KEMX:'Tucson, AZ',
  KENX:'Albany, NY',KEOX:'Fort Rucker, AL',KEPZ:'El Paso, TX',KESX:'Las Vegas, NV',
  KEVX:'Eglin AFB, FL',KEWX:'Austin/San Antonio, TX',KEYX:'Edwards AFB, CA',
  KFCX:'Roanoke, VA',KFDR:'Frederick, OK',KFFC:'Atlanta, GA',KFSD:'Sioux Falls, SD',
  KFSX:'Flagstaff, AZ',KFTG:'Denver, CO',KFWS:'Dallas/Fort Worth, TX',KGGW:'Glasgow, MT',
  KGJX:'Grand Junction, CO',KGLD:'Goodland, KS',KGRB:'Green Bay, WI',KGRK:'Central Texas, TX',
  KGRR:'Grand Rapids, MI',KGSP:'Greenville/Spartanburg, SC',KGWX:'Columbus AFB, MS',
  KGYX:'Portland, ME',KHDX:'Holloman AFB, NM',KHGX:'Houston, TX',KHNX:'Hanford, CA',
  KHPX:'Fort Campbell, KY',KHTX:'Huntsville, AL',KICT:'Wichita, KS',KICX:'Cedar City, UT',
  KILN:'Wilmington, OH',KILX:'Lincoln, IL',KIND:'Indianapolis, IN',KINX:'Tulsa, OK',
  KIWA:'Phoenix, AZ',KIWX:'Fort Wayne, IN',KJAX:'Jacksonville, FL',KJGX:'Robins AFB, GA',
  KJKL:'Jackson, KY',KLBB:'Lubbock, TX',KLCH:'Lake Charles, LA',KHDC:'New Orleans, LA',
  KLNX:'North Platte, NE',KLOT:'Chicago, IL',KLRX:'Elko, NV',KLSX:'St. Louis, MO',
  KLTX:'Wilmington, NC',KLVX:'Louisville, KY',KLWX:'Sterling, VA',KLZK:'Little Rock, AR',
  KMAF:'Midland/Odessa, TX',KMAX:'Medford, OR',KMBX:'Minot AFB, ND',KMHX:'Morehead City, NC',
  KMKX:'Milwaukee, WI',KMLB:'Melbourne, FL',KMOB:'Mobile, AL',KMPX:'Minneapolis, MN',
  KMQT:'Marquette, MI',KMRX:'Knoxville, TN',KMSX:'Missoula, MT',KMTX:'Salt Lake City, UT',
  KMUX:'San Francisco, CA',KMVX:'Grand Forks, ND',KMXX:'Maxwell AFB, AL',
  KNKX:'San Diego, CA',KNQA:'Memphis, TN',KOAX:'Omaha, NE',KOHX:'Nashville, TN',
  KOKX:'New York City, NY',KOTX:'Spokane, WA',KPAH:'Paducah, KY',KPBZ:'Pittsburgh, PA',
  KPDT:'Pendleton, OR',KPOE:'Fort Polk, LA',KPUX:'Pueblo, CO',KRAX:'Raleigh, NC',
  KRGX:'Reno, NV',KRIW:'Riverton, WY',KRLX:'Charleston, WV',KRMX:'Griffiss AFB, NY',
  KRNK:'Blacksburg, VA',KRTX:'Portland, OR',KSFX:'Pocatello, ID',KSGF:'Springfield, MO',
  KSHV:'Shreveport, LA',KSJT:'San Angelo, TX',KSOX:'Santa Ana Mountains, CA',
  KSRX:'Fort Smith, AR',KTBW:'Tampa, FL',KTFX:'Great Falls, MT',KTLH:'Tallahassee, FL',
  KTLX:'Oklahoma City, OK',KTWX:'Topeka, KS',KTYX:'Montague, NY',KUDX:'Rapid City, SD',
  KUEX:'Hastings, NE',KVAX:'Moody AFB, GA',KVBX:'Vandenberg AFB, CA',KVNX:'Vance AFB, OK',
  KVTX:'Los Angeles, CA',KVWX:'Evansville, IN',KYUX:'Yuma, AZ',
  PABC:'Bethel, AK',PACG:'Juneau, AK',PAEC:'Nome, AK',PAHG:'Anchorage, AK',
  PAIH:'Middleton Island, AK',PAKC:'King Salmon, AK',PAPD:'Fairbanks, AK',
  PGUA:'Guam',PHKI:'South Kauai, HI',PHKM:'Kohala, HI',PHMO:'Molokai, HI',PHWA:'South Shore, HI',
  TJUA:'San Juan, PR',
};

// ═══════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════

async function init() {
  initMap();
  initEvents();
  initServiceWorker();

  const initialTabLoad = switchTab(S.tab, { syncUrl: false });

  // Load alerts on startup (on by default)
  $('spcToggle').classList.add('on');
  S.spcOn = true;
  S.alertsOn = true;
  void loadAlerts();
  void loadSPC();
  void loadMCDs();
  await initialTabLoad;
  mobileUrlSyncEnabled = true;
  syncMobileUrl();
}

function initMap() {
  leafletMap = L.map('map', {
    center: [39, -98],
    zoom: 5,
    zoomControl: false,
    attributionControl: true,
    zoomSnap: 0.5,
    maxBounds: [[12, -150], [60, -45]],
    maxBoundsViscosity: 1.0,
  });

  baseTile = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png?key=cb1_2l3n_1_eb188b07acba78da03ca3c9c',
    { subdomains: 'abcd', maxZoom: 19, crossOrigin: true,
      attribution: '&copy; CARTO &copy; OSM' }
  ).addTo(leafletMap);

  // Custom panes — ordered bottom to top
  leafletMap.createPane('wxOverlay').style.zIndex = 420;       // weather data images
  leafletMap.createPane('boundaryPane').style.zIndex = 445;    // state/country lines
  leafletMap.getPane('boundaryPane').style.pointerEvents = 'none';
  leafletMap.createPane('spcPane').style.zIndex = 450;         // SPC outlooks
  const chaserPane = leafletMap.createPane('chaserPane');
  chaserPane.style.zIndex = 452;
  chaserPane.style.pointerEvents = 'auto';
  const featChaserPane = leafletMap.createPane('featuredChaserPane');
  featChaserPane.style.zIndex = 453;
  featChaserPane.style.pointerEvents = 'auto';
  leafletMap.createPane('alertsPane').style.zIndex = 455;      // warnings
  leafletMap.createPane('labelsPane').style.zIndex = 460;      // city/road labels on top
  leafletMap.getPane('labelsPane').style.pointerEvents = 'none';
  const radarSitePane = leafletMap.createPane('radarSitePane');  // clickable NEXRAD dots
  radarSitePane.style.zIndex = 465;
  radarSitePane.style.pointerEvents = 'auto';
  const locPane = leafletMap.createPane('locationPane');        // GPS dot — above everything
  locPane.style.zIndex = 470;
  locPane.style.pointerEvents = 'none';

  // Labels tile on top of data + boundaries
  labelsTile = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png?key=cb1_2l3n_1_eb188b07acba78da03ca3c9c',
    { subdomains: 'abcd', maxZoom: 19, crossOrigin: true, pane: 'labelsPane' }
  ).addTo(leafletMap);

  if (typeof SurfaceObs !== 'undefined') {
    SurfaceObs.init(leafletMap, BASE);
  }

  // Load state boundaries
  loadBoundaries();

  leafletMap.on('click', onMapTap);
  leafletMap.on('zoomend', _updateCountyVisibility);
  leafletMap.on('moveend', syncMobileUrl);

  // Long-press on map → open sounding (models tab only)
  {
    let _lpTimer = null;
    const mapContainer = leafletMap.getContainer();
    mapContainer.addEventListener('touchstart', (e) => {
      clearTimeout(_lpTimer);
      if (S.tab !== 'models' || !S.runId) return;
      const t = e.touches[0];
      const startX = t.clientX, startY = t.clientY;
      const onMove = (ev) => {
        const dx = ev.touches[0].clientX - startX, dy = ev.touches[0].clientY - startY;
        if (Math.hypot(dx, dy) > 10) cleanup();
      };
      const cleanup = () => {
        clearTimeout(_lpTimer);
        mapContainer.removeEventListener('touchmove', onMove);
        mapContainer.removeEventListener('touchend', cleanup);
        mapContainer.removeEventListener('touchcancel', cleanup);
      };
      mapContainer.addEventListener('touchmove', onMove, { passive: true });
      mapContainer.addEventListener('touchend', cleanup, { once: true });
      mapContainer.addEventListener('touchcancel', cleanup, { once: true });
      _lpTimer = setTimeout(() => {
        cleanup();
        const rect = mapContainer.getBoundingClientRect();
        const pt = leafletMap.containerPointToLatLng([startX - rect.left, startY - rect.top]);
        loadMobileSounding(pt.lat, pt.lng);
      }, 600);
    }, { passive: true });
  }
}

function buildMobileSatelliteCityLayer() {
  return L.layerGroup(
    MOBILE_SATELLITE_REFERENCE_CITIES.flatMap(([name, lat, lon]) => {
      const dot = L.circleMarker([lat, lon], {
        pane: 'labelsPane',
        interactive: false,
        radius: 2.3,
        weight: 0.9,
        color: 'rgba(6, 10, 16, 0.92)',
        fillColor: 'rgba(244, 247, 251, 0.95)',
        fillOpacity: 1,
      });
      dot.bindTooltip(name, {
        permanent: true,
        direction: 'right',
        offset: [7, 0],
        opacity: 1,
        className: 'mobile-sat-city-tooltip',
      });
      return [dot];
    })
  );
}

function updateMobileReferenceLayers() {
  const satelliteMode = S.tab === 'satellite';
  S.coastlineLayers.forEach((layer) => {
    if (satelliteMode) {
      if (!leafletMap.hasLayer(layer)) leafletMap.addLayer(layer);
    } else if (leafletMap.hasLayer(layer)) {
      leafletMap.removeLayer(layer);
    }
  });
  if (S.satelliteCityLayer) {
    // The tile label pane already supplies city names at the correct zoom.
    if (leafletMap.hasLayer(S.satelliteCityLayer)) {
      leafletMap.removeLayer(S.satelliteCityLayer);
    }
  }
}

async function loadBoundaries() {
  try {
    const [statesResp, countriesResp, coastlineResp] = await Promise.all([
      fetch(`${BASE}/static/boundaries_states.json`),
      fetch(`${BASE}/static/boundaries_countries.json`),
      fetch(`${BASE}/static/boundaries_coastline.json`),
    ]);
    // Counties (2.5MB) load lazily on first zoom past COUNTY_MIN_ZOOM —
    // see _loadCountyData().
    // States — drawn on top of counties
    if (statesResp.ok) {
      const statesData = await statesResp.json();
      L.geoJSON(statesData, {
        pane: 'boundaryPane',
        style: { color: '#000', weight: 3.5, fillOpacity: 0, opacity: 0.6, interactive: false },
      }).addTo(leafletMap);
      S.statesLayer = L.geoJSON(statesData, {
        pane: 'boundaryPane',
        style: { color: '#fff', weight: 1.2, fillOpacity: 0, opacity: 0.8, interactive: false },
      }).addTo(leafletMap);
    }
    // Countries — on top of everything
    if (countriesResp.ok) {
      const countriesData = await countriesResp.json();
      L.geoJSON(countriesData, {
        pane: 'boundaryPane',
        style: { color: '#000', weight: 4, fillOpacity: 0, opacity: 0.6, interactive: false },
      }).addTo(leafletMap);
      L.geoJSON(countriesData, {
        pane: 'boundaryPane',
        style: { color: '#fff', weight: 1.5, fillOpacity: 0, opacity: 0.8, interactive: false },
      }).addTo(leafletMap);
    }
    if (coastlineResp.ok) {
      const coastlineData = await coastlineResp.json();
      const coastOutline = L.geoJSON(coastlineData, {
        pane: 'boundaryPane',
        style: { color: '#05080d', weight: 2.2, fillOpacity: 0, opacity: 0.34, interactive: false },
      });
      const coastLine = L.geoJSON(coastlineData, {
        pane: 'boundaryPane',
        style: { color: '#f4f7fb', weight: 1.0, fillOpacity: 0, opacity: 0.62, interactive: false },
      });
      S.coastlineLayers = [coastOutline, coastLine];
    }
    S.satelliteCityLayer = buildMobileSatelliteCityLayer();
    updateMobileReferenceLayers();
  } catch {}
}

function initEvents() {
  // Tabs
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Selector pill
  selectorPill.addEventListener('click', openSheet);
  sheetBackdrop.addEventListener('click', closeSheet);

  // Alerts toggle
  $('spcToggle').addEventListener('click', toggleSPC);
  $('chaserToggle').addEventListener('click', toggleChasers);

  // Scrubber
  playBtn.addEventListener('click', togglePlay);
  scrubSlider.addEventListener('input', onScrub);
  $('speedBtn').addEventListener('click', cycleSpeed);

  // Screenshot button — capture map to clipboard with camera flash
  $('screenshotBtn').addEventListener('click', async () => {
    const btn = $('screenshotBtn');

    // Camera flash effect
    const flash = document.createElement('div');
    flash.className = 'screen-flash';
    document.body.appendChild(flash);

    let cleanupPressureCapture = null;
    try {
      if (S.overlayLayer?.isPressureContours) {
        await S.overlayLayer._loading;
        cleanupPressureCapture = S.overlayLayer.prepareCapture();
      }
      // Build a composite canvas manually from the map
      const mapEl = document.getElementById('map');
      const w = mapEl.offsetWidth * 2;  // 2x for retina
      const h = mapEl.offsetHeight * 2;
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext('2d');
      ctx.scale(2, 2);
      const captureMetadata = S.tab === 'models'
        ? window.MapCaptureMetadata.model({ model: S.model, product: paramLabel(S.param), runId: S.runId, fxx: S.fxx })
        : [topInfo.textContent, scrubLabel.textContent].filter(Boolean).join('\n');
      const captionHeight = window.MapCaptureMetadata.height(ctx, captureMetadata, mapEl.offsetWidth);

      // Draw background
      ctx.fillStyle = '#08090e';
      ctx.fillRect(0, 0, mapEl.offsetWidth, mapEl.offsetHeight);

      // Collect all visible images/canvases from the map in z-order
      // Note: Leaflet puts class "leaflet-image-layer" ON the <img>, not a wrapper
      const layers = [...mapEl.querySelectorAll('.leaflet-tile, img.leaflet-image-layer, canvas')];
      // Sort by z-index (pane order) so overlays draw on top of base tiles
      const mapRect = mapEl.getBoundingClientRect();
      layers.sort((a, b) => {
        const za = (a.closest('.leaflet-pane') || a).style.zIndex || 0;
        const zb = (b.closest('.leaflet-pane') || b).style.zIndex || 0;
        return (parseInt(za) || 0) - (parseInt(zb) || 0);
      });
      for (const el of layers) {
        if (!el.offsetParent && el.tagName !== 'CANVAS') continue;
        try {
          const rect = el.getBoundingClientRect();
          const x = rect.left - mapRect.left;
          const y = rect.top - mapRect.top;
          ctx.drawImage(el, x, y, rect.width, rect.height);
        } catch { /* cross-origin tile, skip */ }
      }

      // Draw surface obs markers via SurfaceObs.drawToCanvas() —
      // avoids foreignObject (taints iOS canvas) and DOM querying fragility.
      if (typeof SurfaceObs !== 'undefined' && SurfaceObs.isVisible()) {
        await SurfaceObs.drawToCanvas(ctx);
      }

      // WXViewer logo — top left
      ctx.save();
      ctx.shadowColor = 'rgba(0,0,0,0.6)';
      ctx.shadowBlur = 8;
      ctx.shadowOffsetX = 2;
      ctx.shadowOffsetY = 2;
      ctx.font = 'bold 28px -apple-system, "SF Pro Display", "Helvetica Neue", sans-serif';
      ctx.fillStyle = 'rgba(220,225,234,0.92)';
      ctx.textAlign = 'left';
      ctx.fillText('WX', 16, 38);
      const wxW = ctx.measureText('WX').width;
      ctx.fillStyle = 'rgba(123,159,206,0.85)';
      ctx.fillText('VIEWER', 16 + wxW, 38);
      ctx.restore();

      // Legend bar
      const legEl = $('legendBar');
      const legCanvas = $('legendCanvas');
      const legTicks = $('legendTicks');
      if (legEl && !legEl.classList.contains('hidden')) {
        const lw = 200, lh = 14, lpad = 10, ltickH = 14;
        const lx = mapEl.offsetWidth - lw - lpad - 16;
        const ly = mapEl.offsetHeight - captionHeight - 16 - ltickH - lh - 20;
        // Background pill
        ctx.fillStyle = 'rgba(8,9,14,0.82)';
        ctx.beginPath();
        ctx.roundRect(lx - 10, ly - 8, lw + 20, lh + ltickH + 18, 8);
        ctx.fill();
        // Draw gradient from legend canvas
        try { ctx.drawImage(legCanvas, lx, ly, lw, lh); } catch {}
        // Tick labels
        const ticks = legTicks.querySelectorAll('span');
        if (ticks.length) {
          ctx.fillStyle = 'rgba(69,75,92,0.9)';
          ctx.font = '9px "SF Mono", Menlo, Consolas, monospace';
          ctx.textAlign = 'center';
          ticks.forEach((t, i) => {
            const tx = lx + (lw * i / (ticks.length - 1));
            ctx.fillText(t.textContent, tx, ly + lh + ltickH - 2);
          });
        }
      }

      window.MapCaptureMetadata.stamp(canvas, captureMetadata, { scale: 2 });

      // Try clipboard first, fall back to share, then download
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
      let saved = false;

      // Try clipboard (works on some browsers)
      if (navigator.clipboard && typeof ClipboardItem !== 'undefined') {
        try {
          await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
          saved = true;
        } catch {}
      }

      // Try native share (iOS)
      if (!saved && navigator.canShare) {
        const file = new File([blob], 'wxviewer.png', { type: 'image/png' });
        if (navigator.canShare({ files: [file] })) {
          try {
            await navigator.share({ files: [file], title: 'WxViewer' });
            saved = true;
          } catch {}
        }
      }

      // Final fallback: download
      if (!saved) {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'wxviewer.png';
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      }

      // Show confirmation
      btn.classList.add('flash');
      setTimeout(() => btn.classList.remove('flash'), 400);

    } catch (err) {
      console.error('Screenshot failed:', err);
    } finally {
      cleanupPressureCapture?.();
    }

    // Remove flash
    setTimeout(() => flash.remove(), 350);
  });

  // Share button
  document.querySelectorAll('[data-desktop-link]').forEach((link) => {
    link.addEventListener('click', () => {
      const url = buildMobileShareUrl();
      url.searchParams.set('source', S.tab);
      url.searchParams.set('view', 'desktop');
      url.searchParams.set('desktop', '1');
      link.href = url.href;
    });
  });
  $('shareNative').hidden = !navigator.share;
  shareBtn.addEventListener('click', () => shareSheet.classList.remove('hidden'));
  $('shareCancel').addEventListener('click', () => shareSheet.classList.add('hidden'));
  shareSheet.addEventListener('click', (e) => { if (e.target === shareSheet) shareSheet.classList.add('hidden'); });
  $('shareCopyLink').addEventListener('click', () => {
    navigator.clipboard.writeText(buildMobileShareUrl().href).then(() => {
      $('shareCopyLink').textContent = 'Copied!';
      setTimeout(() => { $('shareCopyLink').textContent = 'Copy Link'; shareSheet.classList.add('hidden'); }, 1000);
    }).catch(() => {});
  });
  $('shareNative').addEventListener('click', () => {
    if (navigator.share) {
      navigator.share({ title: 'WxViewer', url: buildMobileShareUrl().href }).catch(() => {});
    }
    shareSheet.classList.add('hidden');
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { closeSheet(); shareSheet.classList.add('hidden'); closeSounding(); }
  });

  // Sounding close
  if (soundingClose) soundingClose.addEventListener('click', closeSounding);

  // Swipe-to-dismiss sheet
  let startY = 0;
  bottomSheet.querySelector('.sheet-handle').addEventListener('touchstart', (e) => { startY = e.touches[0].clientY; });
  bottomSheet.querySelector('.sheet-handle').addEventListener('touchend', (e) => {
    const dy = e.changedTouches[0].clientY - startY;
    if (dy > 80) closeSheet();
  });
}

function initServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js', { scope: '/static/' }).catch(() => {});
  }
}

// ═══════════════════════════════════════════════════════════════
// API helper
// ═══════════════════════════════════════════════════════════════

async function api(path, { timeoutMs = 15_000, ...options } = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(`${BASE}${path}`, { ...options, signal: controller.signal });
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } finally {
    clearTimeout(timeoutId);
  }
}

// ═══════════════════════════════════════════════════════════════
// GPS location
// ═══════════════════════════════════════════════════════════════

let _geoWatchId  = null;
let _locMarker   = null;
let _locRing     = null;

function startLocationWatch() {
  if (!('geolocation' in navigator)) return;
  if (_geoWatchId !== null) return;
  _geoWatchId = navigator.geolocation.watchPosition(
    (pos) => _updateLocMarker(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy),
    () => {},
    { enableHighAccuracy: true, maximumAge: 10000, timeout: 30000 }
  );
}

function stopLocationWatch() {
  if (_geoWatchId !== null) {
    navigator.geolocation.clearWatch(_geoWatchId);
    _geoWatchId = null;
  }
  if (_locMarker)  { leafletMap.removeLayer(_locMarker);  _locMarker  = null; }
  if (_locRing)    { leafletMap.removeLayer(_locRing);    _locRing    = null; }
}

function _updateLocMarker(lat, lng, accuracy) {
  const ll = [lat, lng];
  if (!_locMarker) {
    const icon = L.divIcon({
      className: '',
      html: '<div class="loc-dot"></div>',
      iconSize: [16, 16],
      iconAnchor: [8, 8],
    });
    _locRing = L.circle(ll, {
      radius: accuracy,
      pane: 'locationPane',
      color: '#4a9ede', fillColor: '#4a9ede',
      fillOpacity: 0.12, weight: 1.5, opacity: 0.5,
      interactive: false,
    }).addTo(leafletMap);
    _locMarker = L.marker(ll, {
      icon, pane: 'locationPane', interactive: false,
    }).addTo(leafletMap);
  } else {
    _locMarker.setLatLng(ll);
    _locRing.setLatLng(ll);
    _locRing.setRadius(accuracy);
  }
}

// ═══════════════════════════════════════════════════════════════
// Tab switching
// ═══════════════════════════════════════════════════════════════

async function switchTab(tab, { syncUrl = true } = {}) {
  ++mobileDataRequestId;
  ++mobileLegendRequestId;
  ++mobileSurfaceRequestId;
  closeSheet();
  pause();
  stopRadarAutoRefresh();
  clearLoadQueue();
  clearOverlay();
  if (typeof SurfaceObs !== 'undefined') {
    SurfaceObs.hide();
  }
  if (!S.alertsOn) clearAlerts();
  if (tab !== 'radar') stopLocationWatch();
  S.tab = tab;
  try { localStorage.setItem('wx_mobile_tab', tab); } catch {}

  document.querySelectorAll('.tab').forEach((b) =>
    { b.classList.toggle('active', b.dataset.tab === tab);
      b.setAttribute('aria-current', b.dataset.tab === tab ? 'page' : 'false'); }
  );

  topValue.textContent = '';
  setMobileBasemapMode(tab === 'satellite' ? 'satellite' : 'default');
  updateMobileReferenceLayers();

  let loadPromise = Promise.resolve();
  if (tab === 'home') loadPromise = initHome();
  else if (tab === 'models') loadPromise = initModels();
  else if (tab === 'satellite') loadPromise = initSatellite();
  else if (tab === 'radar') loadPromise = initRadar();
  else if (tab === 'surface') loadPromise = initSurface();

  // Show radar site markers only on radar tab
  if (tab === 'radar') {
    _radarSiteBuildPromise = buildRadarSiteMarkers().then(showRadarSiteMarkers);
  } else {
    hideRadarSiteMarkers();
  }
  await loadPromise;
  if (S.tab !== tab) return;
  restoreMobileSharedMap();
  if (syncUrl) syncMobileUrl();
}

// ═══════════════════════════════════════════════════════════════
// HOME — uses local radar composite (same as desktop)
// ═══════════════════════════════════════════════════════════════

async function initHome() {
  clearLoadQueue();
  selectorPill.classList.add('hidden');
  legendBar.classList.add('hidden');
  scrubberWrap?.classList.remove('hidden');
  topInfo.textContent = '';
  showCounties(true);

  // Fit full CONUS in view
  leafletMap.setView(DEFAULT_CENTER, DEFAULT_ZOOM, { animate: true });

  try {
    // Fetch composite frames JSON (same source as desktop)
    const resp = await fetch(`${BASE}/rendered/home/radar_composite_frames.json?_=${Date.now()}`, { cache: 'no-store' });
    let frames = [];
    if (resp.ok) {
      const data = await resp.json();
      if (Array.isArray(data)) frames = data;
    }

    if (!frames.length) {
      // Fallback: try latest single frame
      const metaResp = await fetch(`${BASE}/rendered/home/radar_composite_latest.json?_=${Date.now()}`, { cache: 'no-store' });
      if (metaResp.ok) {
        const meta = await metaResp.json();
        if (meta.image) frames = [meta];
      }
    }

    if (frames.length) {
      S.homeRadarFrames = frames;
      S.homeIdx = frames.length - 1;
      setupScrubber(frames.length, S.homeIdx);
      showHomeFrame();
      // Defer loop preload so the visible frame wins the bandwidth race
      setTimeout(() => { if (S.tab === 'home') preloadAllFrames(); }, 1500);
    } else {
      topInfo.textContent = 'No radar data';
    }
  } catch {
    topInfo.textContent = 'Load error';
  }

  loadAlerts();
  loadSPC();
}

function showHomeFrame() {
  const f = S.homeRadarFrames[S.homeIdx];
  if (!f) return;
  // png_path is relative like "home/radar_composite_YYYYMMDD_HHMMSS.png"
  const pngPath = f.png_path || (f.image ? `home/${f.image}` : '');
  if (!pngPath) return;
  const url = `${BASE}/rendered/${pngPath.replace(/^\//, '')}`;
  const b = f.bounds;
  const bounds = b ? [[b.south, b.west], [b.north, b.east]] : [[23, -129], [53, -66]];
  setImageOverlay(url, bounds);
  topInfo.textContent = `Radar ${formatTs(f.timestamp)}`;
  scrubLabel.textContent = formatTs(f.timestamp);
  updateCacheStatus();
}

function toggleAlerts() {
  S.alertsOn = !S.alertsOn;
  $('alertsToggle').classList.toggle('on', S.alertsOn);
  if (S.alertsOn) {
    loadAlerts();
  } else {
    if (S.alertsLayer) { leafletMap.removeLayer(S.alertsLayer); S.alertsLayer = null; }
  }
}

const ALERT_TYPES = [
  'Tornado Warning', 'Tornado Watch', 'Tornado Emergency',
  'Severe Thunderstorm Warning', 'Severe Thunderstorm Watch',
];
const ALERT_COLORS = {
  'Tornado Warning': '#ff0040',
  'Tornado Emergency': '#ff00ff',
  'Tornado Watch': '#ffff00',
  'Severe Thunderstorm Warning': '#ff8c00',
  'Severe Thunderstorm Watch': '#4682b4',
};

async function loadAlerts() {
  try {
    const types = encodeURIComponent(ALERT_TYPES.join(','));
    const data = await api(`/api/alerts?types=${types}`);
    if (S.alertsLayer) { leafletMap.removeLayer(S.alertsLayer); S.alertsLayer = null; }
    if (!data.features || !data.features.length) return;

    // Filter to only severe/tornado types
    const filtered = {
      ...data,
      features: data.features.filter((f) => {
        const ev = f.properties.event || '';
        return ALERT_TYPES.some((t) => ev.includes(t) || t.includes(ev));
      }),
    };
    if (!filtered.features.length) return;

    S.alertsLayer = L.geoJSON(filtered, {
      pane: 'alertsPane',
      style: (f) => {
        const ev = f.properties.event || '';
        const color = ALERT_COLORS[ev] || '#ff8c00';
        const isWatch = ev.includes('Watch');
        return { color, weight: isWatch ? 1.5 : 2.5, fillOpacity: isWatch ? 0.08 : 0.18, opacity: 0.9 };
      },
      onEachFeature: (f, layer) => {
        layer.bindPopup(`<b>${f.properties.event}</b><br>${f.properties.headline || ''}`,
          { maxWidth: 260 });
      },
    }).addTo(leafletMap);
  } catch {}
}

async function loadSPC() {
  try {
    const data = await api('/api/spc/day1_cat');
    if (S.spcLayer) { leafletMap.removeLayer(S.spcLayer); S.spcLayer = null; }
    if (!data.features || !data.features.length) return;
    S.spcLayer = L.geoJSON(data, {
      pane: 'spcPane',
      style: (f) => {
        // Use built-in style from API if available
        const s = f.properties.style;
        if (s) return { color: s.color, fillColor: s.fillColor, weight: s.weight || 1.5, fillOpacity: s.fillOpacity ?? 0.18, opacity: 0.8 };
        return { color: '#888', weight: 1.5, fillOpacity: 0.10, opacity: 0.7 };
      },
      onEachFeature: (f, layer) => {
        const lbl = f.properties.label || '';
        if (lbl) layer.bindPopup(`<b>SPC Day 1</b><br>${lbl}`, { maxWidth: 240 });
      },
    }).addTo(leafletMap);
  } catch {}
}

async function loadMCDs() {
  try {
    const mcdData = await api('/api/spc/mcds?limit=25');
    if (S.mcdLayer) { leafletMap.removeLayer(S.mcdLayer); S.mcdLayer = null; }
    if (!mcdData.features || !mcdData.features.length) return;
    S.mcdLayer = L.geoJSON(mcdData, {
      pane: 'alertsPane',
      style: () => ({ color: '#66ccff', weight: 2, fillOpacity: 0.10, opacity: 0.8, dashArray: '6 4' }),
      onEachFeature: (f, layer) => {
        const title = f.properties.title || f.properties.name || 'MCD';
        layer.bindPopup(`<b>${title}</b>`, { maxWidth: 260 });
      },
    }).addTo(leafletMap);
  } catch {}
}

// ═══════════════════════════════════════════════════════════════
// MODELS
// ═══════════════════════════════════════════════════════════════

async function initModels() {
  S.param = firstSupportedModelParam(S.model, S.param);
  ensureActiveModelGroupOpen();
  selectorPill.classList.remove('hidden');
  scrubberWrap?.classList.remove('hidden');
  updatePill(`${MODELS[S.model]?.label || S.model} / ${paramLabel(S.param)}`);
  clearOverlay();
  showCounties(true);
  leafletMap.setView(DEFAULT_CENTER, DEFAULT_ZOOM, { animate: true });
  await loadRun(mobileSharedModelPending ? MOBILE_SHARED_CONFIG.runId : undefined);
}

async function loadRun(explicitRunId) {
  const requestId = beginMobileDataLoad();
  const model = S.model;
  const param = S.param;
  const previousFxx = S.fxx;
  const isCurrent = () => isCurrentMobileRequest(requestId, 'models');
  S.fxxList = [];
  S.modelAvailability = {};
  S.vectorAvailability = {};
  S.overlayBounds = null;
  try {
    const runs = await api(`/api/runs?model=${model}`);
    if (!isCurrent()) return;
    S.modelRuns = runs;
    S.runId = null;
    if (!runs.length) { showMobileDataMessage('No runs available'); return; }

    let selectedRun = runs.find((run) => run.run_id === explicitRunId);
    let avail = null;
    if (selectedRun) {
      avail = await api(`/api/available/${model}/${selectedRun.run_id}`);
    } else {
      // Prefer a complete run that actually contains the requested product.
      const candidates = prioritizeModelRuns(runs, model);
      for (const run of candidates) {
        const candidate = await api(`/api/available/${model}/${run.run_id}`);
        if (!isCurrent()) return;
        if (candidate[param]?.length) { selectedRun = run; avail = candidate; break; }
      }
    }
    if (!isCurrent()) return;
    if (!selectedRun || !avail?.[param]?.length) {
      showMobileDataMessage('No frames for this product');
      return;
    }
    const [bounds, vectors] = await Promise.all([
      api(`/api/bounds/${model}/${selectedRun.run_id}`),
      api(`/api/available/${model}/${selectedRun.run_id}/vectors`).catch(() => ({})),
    ]);
    if (!isCurrent()) return;
    S.runId = selectedRun.run_id;
    S.paletteVersion = selectedRun.palette_version || 1;
    S.modelAvailability = avail;
    S.vectorAvailability = vectors;
    S.fxxList = [...avail[param]].sort((a, b) => mobileForecastMinutes(a) - mobileForecastMinutes(b));
    S.overlayBounds = [[bounds.south, bounds.west], [bounds.north, bounds.east]];
    const requestedFxx = mobileSharedModelPending ? MOBILE_SHARED_CONFIG.fxx : previousFxx;
    S.fxx = S.fxxList.find((value) => String(value) === String(requestedFxx)) ?? S.fxxList[0];
    const fxxIndex = S.fxxList.indexOf(S.fxx);
    setupScrubber(S.fxxList.length, fxxIndex);
    showModelFrame();
    updateLegend(param);
    preloadAllFrames();
  } catch (e) {
    if (isCurrent()) showMobileDataMessage('Unable to load. Select a product to retry.');
  } finally {
    if (isCurrent()) mobileSharedModelPending = false;
  }
}

/** Forecast offsets may include a subhourly suffix, e.g. 01_15min. */
function mobileForecastMinutes(fxx) {
  const match = String(fxx).match(/^(\d+)(?:_(\d+)min)?$/);
  return match ? Number(match[1]) * 60 + Number(match[2] || 0) : 0;
}

/** Choose vectors only for pressure frames that have been generated. */
function mobileModelFrameUrl(fxx) {
  const vector = window.PressureContours && S.vectorAvailability?.[S.param]?.some((frame) => String(frame) === String(fxx));
  return `${BASE}/rendered/${S.model}/${S.runId}/${S.param}/f${String(fxx).padStart(2,'0')}.${vector ? 'contours.json' : 'png'}?v=${MODEL_IMAGE_VERSION}`;
}

function showModelFrame() {
  if (!S.runId || !S.fxxList.length || !S.overlayBounds) return;
  const url = mobileModelFrameUrl(S.fxx);
  if (isErrored(url)) return;
  setImageOverlay(url, S.overlayBounds);

  const runH = S.runId ? S.runId.slice(-2) : '??';
  topInfo.textContent = `${MODELS[S.model]?.label || S.model} ${S.runId?.slice(0,8)||''} ${runH}Z${modelRunIsOld(S.runId, S.model) ? ' · Older run' : ''}`;

  // Compute valid time string matching desktop format
  let labelText = `F${String(S.fxx).padStart(2,'0')}`;
  if (S.runId && S.runId.length >= 10) {
    const yyyy = parseInt(S.runId.substring(0, 4), 10);
    const mm   = parseInt(S.runId.substring(4, 6), 10) - 1;
    const dd   = parseInt(S.runId.substring(6, 8), 10);
    const hh   = parseInt(S.runId.substring(8, 10), 10);
    const init  = new Date(Date.UTC(yyyy, mm, dd, hh));
    const valid = new Date(init.getTime() + mobileForecastMinutes(S.fxx) * 60000);
    const days  = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const utcH  = `${String(valid.getUTCHours()).padStart(2, '0')}:${String(valid.getUTCMinutes()).padStart(2, '0')}`;
    labelText += `  Valid: ${days[valid.getUTCDay()]} ${utcH}z ${valid.getUTCMonth()+1}/${valid.getUTCDate()}`;
  }
  scrubLabel.textContent = labelText;

  updateCacheStatus();
  updatePill(`${MODELS[S.model]?.label || S.model} / ${paramLabel(S.param)}`);
  syncMobileUrl();
}


// ═══════════════════════════════════════════════════════════════
// SATELLITE
// ═══════════════════════════════════════════════════════════════

async function initSatellite() {
  syncSatelliteSelection();
  selectorPill.classList.remove('hidden');
  scrubberWrap?.classList.remove('hidden');
  updatePill(satPillLabel());
  legendBar.classList.add('hidden');
  clearOverlay();
  showCounties(false);
  if (S.satScope === 'national') {
    leafletMap.setView(DEFAULT_CENTER, DEFAULT_ZOOM, { animate: true });
  } else {
    S.satAutoFitPending = true;
  }
  await loadSatFrames();
}

async function loadSatFrames() {
  const requestId = beginMobileDataLoad();
  S.satFrames = [];
  try {
    // API returns a bare array of frame objects
    const regionQuery = S.satRegion ? `?region=${encodeURIComponent(S.satRegion)}` : '';
    const frames = await api(`/api/satellite/frames/${S.sat}/${S.satProduct}${regionQuery}`);
    if (!isCurrentMobileRequest(requestId, 'satellite')) return;
    S.satFrames = Array.isArray(frames) ? frames : (frames.frames || []);
    if (!S.satFrames.length) { showMobileDataMessage('No satellite frames available'); return; }
    S.satIdx = S.satFrames.length - 1;
    if (mobileSharedFramePending && MOBILE_SHARED_CONFIG.view === 'satellite') {
      const sharedIdx = S.satFrames.findIndex((frame) => String(frame.timestamp) === mobileSharedFramePending);
      if (sharedIdx >= 0) S.satIdx = sharedIdx;
      mobileSharedFramePending = null;
    }
    setupScrubber(S.satFrames.length, S.satIdx);
    showSatFrame();
    preloadAllFrames();
  } catch {
    if (isCurrentMobileRequest(requestId, 'satellite')) showMobileDataMessage('Unable to load satellite imagery');
  }
}

function showSatFrame() {
  const f = S.satFrames[S.satIdx];
  if (!f) return;
  // png_path is relative like "satellite/png/goes19_natural_color_2026076_0041.png"
  const pngPath = String(f.png_path || '').replace(/^\//, '');
  const url = `${BASE}/rendered/${pngPath}?palette=${f.palette_version || 1}`;
  if (radarLegendPaletteVersion !== (f.palette_version || 1)) {
    radarLegendPaletteVersion = f.palette_version || 1;
    const product = RADAR_PRODUCTS.find(p => p.id === S.radarProduct);
    if (product) updateLegend(product.legend);
  }
  const b = f.bounds;
  const bounds = b ? [[b.south, b.west], [b.north, b.east]] : [[15, -145], [58, -50]];
  setImageOverlay(url, bounds);
  topInfo.textContent = satTopInfo();
  scrubLabel.textContent = formatTs(f.timestamp);
  updateCacheStatus();
  if (S.satAutoFitPending && b) {
    S.satAutoFitPending = false;
    leafletMap.flyToBounds(bounds, {
      paddingTopLeft: [18, 72],
      paddingBottomRight: [18, 128],
      duration: 0.9,
      maxZoom: 6.5,
    });
  }
  syncMobileUrl();
}

// ═══════════════════════════════════════════════════════════════
// RADAR
// ═══════════════════════════════════════════════════════════════

async function initRadar() {
  selectorPill.classList.remove('hidden');
  scrubberWrap?.classList.remove('hidden');
  updatePill(`${S.radarSite} / ${radarLabel(S.radarProduct)}`);
  clearOverlay();
  showCounties(true);
  startLocationWatch();
  const requestId = beginMobileDataLoad();
  const site = S.radarSite;
  await fetchRadarTilts(site);
  if (!isCurrentMobileRequest(requestId, 'radar') || site !== S.radarSite) return;
  await loadRadarFrames();
  if (S.tab !== 'radar') return;

}

function initSurface() {
  clearLoadQueue();
  selectorPill.classList.add('hidden');
  legendBar.classList.add('hidden');
  scrubberWrap?.classList.remove('hidden');
  clearOverlay();
  showCounties(true);
  leafletMap.setView(DEFAULT_RADAR_CENTER, DEFAULT_RADAR_ZOOM, { animate: true });
  topInfo.textContent = 'Surface observations';
  return loadSurfaceHistory();
}

async function loadSurfaceHistory() {
  const requestId = beginMobileDataLoad();
  S.surfaceFrames = [];
  try {
    const data = await api('/api/surface/history?hours=3');
    if (!isCurrentMobileRequest(requestId, 'surface')) return;
    S.surfaceFrames = Array.isArray(data.frames) ? data.frames : [];
    if (!S.surfaceFrames.length) {
      topInfo.textContent = 'No surface history';
      scrubLabel.textContent = '--';
      scrubFrameCount.textContent = '';
      return;
    }
    S.surfaceIdx = S.surfaceFrames.length - 1;
    setupScrubber(S.surfaceFrames.length, S.surfaceIdx);
    if (typeof SurfaceObs !== 'undefined') {
      SurfaceObs.show({ autoRefresh: false });
    }
    await showSurfaceFrame(S.surfaceIdx);
  } catch {
    if (isCurrentMobileRequest(requestId, 'surface')) showMobileDataMessage('Unable to load surface observations');
  }
}

async function showSurfaceFrame(idx) {
  const frame = S.surfaceFrames[idx];
  if (!frame?.generated) return;
  const requestId = ++mobileSurfaceRequestId;
  try {
    let payload = S.surfacePayloadCache[frame.generated];
    if (!payload) {
      payload = await api(`/api/surface/frame/${frame.generated}`);
      S.surfacePayloadCache[frame.generated] = payload;
    }
    if (S.tab !== 'surface' || requestId !== mobileSurfaceRequestId) return;
    S.surfaceIdx = idx;
    if (typeof SurfaceObs !== 'undefined') SurfaceObs.setPayload(payload);
    topInfo.textContent = 'Surface observations';
    scrubLabel.textContent = formatTs(new Date(frame.generated * 1000).toISOString());
    updateCacheStatus();
    syncMobileUrl();
  } catch {
    if (S.tab === 'surface' && requestId === mobileSurfaceRequestId) {
      pause();
      showMobileDataMessage('Unable to load this observation frame');
    }
  }
}

async function fetchRadarTilts(site) {
  try {
    const data = await api(`/api/radar/tilts/${site}`);
    if (S.tab !== 'radar' || site !== S.radarSite) return;
    const rawTilts = Array.isArray(data) ? data : data?.tilts;
    if (Array.isArray(rawTilts) && rawTilts.length) {
      const available = rawTilts
        .map(Number)
        .filter((t) => Number.isFinite(t) && t >= 0.3)
        .sort((a, b) => a - b);
      if (!available.length) return;
      const selected = [];
      const seen = new Set();
      for (const target of MOBILE_PREFERRED_RADAR_TILTS) {
        let best = null;
        let bestDiff = Infinity;
        for (const tilt of available) {
          const diff = Math.abs(tilt - target);
          if (diff < bestDiff) {
            best = tilt;
            bestDiff = diff;
          }
        }
        if (best != null && !seen.has(best)) {
          seen.add(best);
          selected.push(best);
        }
      }
      if (!seen.has(available[0])) {
        selected.unshift(available[0]);
      }
      S.radarTilts = selected.sort((a, b) => a - b);
      // Reset to 0.5 if current tilt isn't available
      if (!S.radarTilts.includes(S.radarTilt)) {
        S.radarTilt = S.radarTilts[0];
      }
      if (S.tab === 'radar' && !bottomSheet.classList.contains('hidden')) {
        buildRadarSheet();
      }
    }
  } catch { if (S.tab === 'radar' && site === S.radarSite) S.radarTilts = [0.5]; }
}

let _radarAutoRefreshTimer = null;

function useRenderedRadarTilt(frame) {
  if (!frame?.png_path || !Number.isFinite(frame.tilt)) return;
  S.radarTilt = frame.tilt;
  if (!S.radarTilts.includes(frame.tilt)) S.radarTilts.push(frame.tilt);
  S.radarTilts.sort((a, b) => a - b);
  if (!bottomSheet.classList.contains('hidden')) buildRadarSheet();
}

async function loadRadarFrames() {
  const requestId = beginMobileDataLoad();
  const { radarSite: site, radarProduct: product, radarTilt: tilt } = S;
  const isCurrent = () => isCurrentMobileRequest(requestId, 'radar');
  S.radarFrames = [];
  try {
    let frames = await api(`/api/radar/frames/${site}/${product}?tilt=${tilt}`);
    if (!isCurrent()) return;
    frames = Array.isArray(frames) ? frames : (frames.frames || []);
    if (!frames.length || tilt > 0.65) {
      const frame = await api(`/api/radar/render/${site}/${product}/${tilt}`, { timeoutMs: 60_000 });
      if (!isCurrent()) return;
      useRenderedRadarTilt(frame);
      frames = frame?.png_path ? [frame] : [];
    }
    S.radarFrames = frames;
    if (!frames.length) { showMobileDataMessage('No radar frames available'); return; }
    S.radarIdx = frames.length - 1;
    if (mobileSharedFramePending && MOBILE_SHARED_CONFIG.view === 'radar') {
      const sharedIdx = frames.findIndex((frame) => String(frame.timestamp) === mobileSharedFramePending);
      if (sharedIdx >= 0) S.radarIdx = sharedIdx;
      mobileSharedFramePending = null;
    }
    setupScrubber(frames.length, S.radarIdx);
    showRadarFrame(!mobileSharedMapPending);
    preloadAllFrames();
    startRadarAutoRefresh();
    const legend = RADAR_PRODUCTS.find((p) => p.id === product)?.legend;
    if (legend) updateLegend(legend);
  } catch {
    if (isCurrent()) showMobileDataMessage('No radar data available. Select a product to retry.');
  }
}

function startRadarAutoRefresh() {
  stopRadarAutoRefresh();
  _radarAutoRefreshTimer = setInterval(refreshRadarFrames, 30_000);
}

function stopRadarAutoRefresh() {
  if (_radarAutoRefreshTimer) { clearInterval(_radarAutoRefreshTimer); _radarAutoRefreshTimer = null; }
}

async function refreshRadarFrames() {
  if (S.tab !== 'radar') { stopRadarAutoRefresh(); return; }
  const requestId = mobileDataRequestId;
  try {
    const frames = await api(`/api/radar/frames/${S.radarSite}/${S.radarProduct}?tilt=${S.radarTilt}`);
    if (!isCurrentMobileRequest(requestId, 'radar')) return;
    let newFrames = Array.isArray(frames) ? frames : (frames.frames || []);
    if (S.radarTilt > 0.65) {
      const frame = await api(`/api/radar/render/${S.radarSite}/${S.radarProduct}/${S.radarTilt}`, { timeoutMs: 60_000 });
      if (!isCurrentMobileRequest(requestId, 'radar')) return;
      useRenderedRadarTilt(frame);
      newFrames = frame?.png_path ? [frame] : [];
    }
    if (!newFrames.length) return;

    const wasAtEnd = S.radarIdx >= S.radarFrames.length - 1;
    const oldLatest = S.radarFrames.length ? S.radarFrames[S.radarFrames.length - 1]?.timestamp : null;
    const newLatest = newFrames[newFrames.length - 1]?.timestamp;

    if (newLatest === oldLatest && newFrames.length === S.radarFrames.length) return;

    const selectedTimestamp = S.radarFrames[S.radarIdx]?.timestamp;
    S.radarFrames = newFrames;
    S.radarIdx = wasAtEnd ? newFrames.length - 1 : Math.max(0, newFrames.findIndex((frame) => frame.timestamp === selectedTimestamp));
    showRadarFrame(false);
    setupScrubber(newFrames.length, S.radarIdx);
    preloadAllFrames();
  } catch {}
}

let radarLegendPaletteVersion = null;

function showRadarFrame(fitBounds) {
  const f = S.radarFrames[S.radarIdx];
  if (!f) return;
  // png_path is relative like "radar/png/KOAX_REF_0.5_20260317_003400.png"
  const pngPath = String(f.png_path || '').replace(/^\//, '');
  const url = `${BASE}/rendered/${pngPath}?palette=${f.palette_version || 1}`;
  if (radarLegendPaletteVersion !== (f.palette_version || 1)) {
    radarLegendPaletteVersion = f.palette_version || 1;
    const product = RADAR_PRODUCTS.find(p => p.id === S.radarProduct);
    if (product) updateLegend(product.legend);
  }
  const b = f.bounds;
  const bounds = b ? [[b.south, b.west], [b.north, b.east]] : [[20, -130], [55, -65]];
  setImageOverlay(url, bounds);
  topInfo.textContent = `${S.radarSite} ${radarLabel(S.radarProduct)}`;
  scrubLabel.textContent = formatTs(f.timestamp);
  updateCacheStatus();

  // Fly to radar site center
  if (fitBounds && b) {
    leafletMap.flyTo([b.center_lat, b.center_lon], 7, { duration: 1 });
  }
  syncMobileUrl();
}

// ═══════════════════════════════════════════════════════════════
// Shared overlay & scrubber
// ═══════════════════════════════════════════════════════════════

function setImageOverlay(url, bounds) {
  if (!bounds) return;
  const vector = !!window.PressureContours?.isUrl(url);
  if (S.overlayLayer && !!S.overlayLayer.isPressureContours !== vector) {
    leafletMap.removeLayer(S.overlayLayer);
    S.overlayLayer = null;
  }
  if (S.overlayLayer) {
    S.overlayLayer.setUrl(url);
    S.overlayLayer.setBounds(bounds);
  } else if (vector) {
    S.overlayLayer = window.PressureContours.layer(url).addTo(leafletMap);
  } else {
    S.overlayLayer = L.imageOverlay(url, bounds, {
      opacity: 1, pane: 'wxOverlay', crossOrigin: true,
    }).addTo(leafletMap);
  }
  // Hide broken-image icon if the file doesn't exist; show again on success
  const el = S.overlayLayer.getElement?.();
  if (el && !vector) {
    el.classList.toggle('radar-overlay', S.tab === 'radar');
    el.onerror = () => { el.style.visibility = 'hidden'; };
    el.onload  = () => { el.style.visibility = ''; };
  }
}

let _countiesEnabled = true;
const COUNTY_MIN_ZOOM = 6;

function showCounties(enable) {
  _countiesEnabled = enable;
  _updateCountyVisibility();
}

let _countiesLoadStarted = false;

async function _loadCountyData() {
  _countiesLoadStarted = true;
  try {
    const resp = await fetch(`${BASE}/static/boundaries_counties.json`);
    if (!resp.ok) return;
    const countiesData = await resp.json();
    const cOutline = L.geoJSON(countiesData, {
      pane: 'boundaryPane',
      style: { color: '#000', weight: 0.85, fillOpacity: 0, opacity: 0.14, interactive: false },
    });
    const cLine = L.geoJSON(countiesData, {
      pane: 'boundaryPane',
      style: { color: '#fff', weight: 0.22, fillOpacity: 0, opacity: 0.16, interactive: false },
    });
    S.countyLayers = [cOutline, cLine];
    _updateCountyVisibility();
  } catch {}
}

function _updateCountyVisibility() {
  const show = _countiesEnabled && leafletMap.getZoom() >= COUNTY_MIN_ZOOM;
  if (show && !_countiesLoadStarted) _loadCountyData();
  S.countyLayers.forEach((l) => {
    if (show) { if (!leafletMap.hasLayer(l)) leafletMap.addLayer(l); }
    else { if (leafletMap.hasLayer(l)) leafletMap.removeLayer(l); }
  });
}

function clearOverlay() {
  if (S.overlayLayer) { leafletMap.removeLayer(S.overlayLayer); S.overlayLayer = null; }
}

function clearAlerts() {
  if (S.alertsLayer) { leafletMap.removeLayer(S.alertsLayer); S.alertsLayer = null; }
  if (S.spcLayer) { leafletMap.removeLayer(S.spcLayer); S.spcLayer = null; }
  if (S.mcdLayer) { leafletMap.removeLayer(S.mcdLayer); S.mcdLayer = null; }
}

function toggleSPC() {
  S.spcOn = !S.spcOn;
  S.alertsOn = S.spcOn;
  $('spcToggle').classList.toggle('on', S.spcOn);
  if (S.spcOn) {
    loadAlerts();
    loadSPC();
    loadMCDs();
  } else {
    if (S.alertsLayer) { leafletMap.removeLayer(S.alertsLayer); S.alertsLayer = null; }
    if (S.spcLayer) { leafletMap.removeLayer(S.spcLayer); S.spcLayer = null; }
    if (S.mcdLayer) { leafletMap.removeLayer(S.mcdLayer); S.mcdLayer = null; }
  }
}

function toggleChasers() {
  S.chasersOn = !S.chasersOn;
  $('chaserToggle').classList.toggle('on', S.chasersOn);
  if (S.chasersOn) {
    loadChasers();
  } else {
    clearChasers();
  }
}

async function loadChasers() {
  try {
    const data = await api('/api/spotters?max_age_min=1440&moving_only=false');
    if (S.chaserLayer) { leafletMap.removeLayer(S.chaserLayer); S.chaserLayer = null; }
    if (S.featuredChaserLayer) { leafletMap.removeLayer(S.featuredChaserLayer); S.featuredChaserLayer = null; }
    if (!data.features || !data.features.length) return;

    // Separate featured from regular
    const namesLower = FEATURED_CHASERS.map(n => n.toLowerCase());
    const featured = [];
    const regular = [];
    for (const f of data.features) {
      const name = (f.properties?.name || '').toLowerCase();
      if (namesLower.some(n => name.includes(n))) {
        featured.push(f);
      } else {
        regular.push(f);
      }
    }

    // Regular chasers — small red dots
    if (regular.length) {
      S.chaserLayer = L.geoJSON({ type: 'FeatureCollection', features: regular }, {
        pane: 'chaserPane',
        interactive: true,
        pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
          radius: 3,
          color: '#ff4444',
          weight: 1,
          fillColor: '#cc0000',
          fillOpacity: 0.85,
          interactive: true,
        }),
        onEachFeature: (f, layer) => {
          const p = f.properties || {};
          layer.bindPopup('<b>' + (p.name || 'Spotter') + '</b>' + (p.status ? '<br>' + p.status : ''), { maxWidth: 240 });
        },
      }).addTo(leafletMap);
    }

    // Featured chasers — pulsing cyan dots
    if (featured.length) {
      S.featuredChaserLayer = L.geoJSON({ type: 'FeatureCollection', features: featured }, {
        pane: 'featuredChaserPane',
        interactive: true,
        pointToLayer: (feature, latlng) => {
          return L.marker(latlng, {
            pane: 'featuredChaserPane',
            icon: L.divIcon({
              className: 'featured-chaser-icon',
              html: '<div class="featured-chaser-pulse"></div><div class="featured-chaser-dot"></div>',
              iconSize: [14, 14],
              iconAnchor: [7, 7],
            }),
          });
        },
        onEachFeature: (f, layer) => {
          const p = f.properties || {};
          layer.bindPopup('<b style="color:var(--accent);">' + (p.name || 'Chaser') + '</b>' + (p.status ? '<br>' + p.status : ''), { maxWidth: 240 });
        },
      }).addTo(leafletMap);
    }
  } catch (e) {
    console.error('Failed to load chasers:', e);
  }
}

function clearChasers() {
  if (S.chaserLayer) { leafletMap.removeLayer(S.chaserLayer); S.chaserLayer = null; }
  if (S.featuredChaserLayer) { leafletMap.removeLayer(S.featuredChaserLayer); S.featuredChaserLayer = null; }
}

function setupScrubber(count, idx) {
  scrubSlider.max = Math.max(0, count - 1);
  scrubSlider.value = idx;
  scrubSlider.disabled = count < 2;
  playBtn.disabled = count < 2;
}

function onScrub() {
  const idx = parseInt(scrubSlider.value);
  if (S.tab === 'home')       { S.homeIdx = idx; showHomeFrame(); }
  else if (S.tab === 'models')    { S.fxx = S.fxxList[idx] ?? 0; showModelFrame(); }
  else if (S.tab === 'satellite') { S.satIdx = idx; showSatFrame(); }
  else if (S.tab === 'radar')     { S.radarIdx = idx; showRadarFrame(false); }
  else if (S.tab === 'surface')   { showSurfaceFrame(idx); }
  syncMobileUrl();
}

function togglePlay() {
  S.isPlaying ? pause() : play();
}

function play() {
  if (playBtn.disabled || S.isPlaying) return;
  playBtn.setAttribute('aria-label', 'Pause animation');
  S.isPlaying = true;
  playIcon.classList.add('hidden');
  pauseIcon.classList.remove('hidden');
  advance();
}

function pause() {
  playBtn.setAttribute('aria-label', 'Play animation');
  S.isPlaying = false;
  playIcon.classList.remove('hidden');
  pauseIcon.classList.add('hidden');
  if (S.playTimer) { clearTimeout(S.playTimer); S.playTimer = null; }
}

function advance() {
  if (!S.isPlaying) return;
  let idx = parseInt(scrubSlider.value) + 1;
  const max = parseInt(scrubSlider.max);
  if (idx > max) idx = 0;
  const frameDelay = SPEEDS[S.speedIdx].ms * (S.tab === 'surface' ? 3 : 1);
  const delay = idx === max ? Math.max(1000, frameDelay * 1.75) : frameDelay;

  if (S.tab === 'surface') {
    scrubSlider.value = idx;
    onScrub();
    S.playTimer = setTimeout(advance, delay);
    return;
  }

  const url = frameUrl(idx);
  if (url && !isCached(url) && !isErrored(url)) {
    // Still downloading — retry shortly
    S.playTimer = setTimeout(advance, 50);
    return;
  }
  // If the frame errored (missing file), silently skip it
  if (url && isErrored(url)) {
    scrubSlider.value = idx;
    S.playTimer = setTimeout(advance, frameDelay);
    return;
  }

  scrubSlider.value = idx;
  onScrub();
  S.playTimer = setTimeout(advance, delay);
}

function cycleSpeed() {
  S.speedIdx = (S.speedIdx + 1) % SPEEDS.length;
  const btn = $('speedBtn');
  btn.textContent = SPEEDS[S.speedIdx].label;
  btn.classList.toggle('fast', S.speedIdx > 0);
}

// ═══════════════════════════════════════════════════════════════
// Legend
// ═══════════════════════════════════════════════════════════════

async function updateLegend(param) {
  const requestId = ++mobileLegendRequestId;
  const paletteQuery = S.tab === 'radar' ? `?radar_palette_version=${S.radarFrames[S.radarIdx]?.palette_version || 1}`
    : S.tab === 'home' ? '?radar_palette_version=1'
    : S.tab === 'models' && S.paletteVersion ? `?palette_version=${S.paletteVersion}` : '';
  try {
    const data = await api(`/api/legend/${param}${paletteQuery}`);
    if (requestId !== mobileLegendRequestId) return;
    const title = data.label || paramLabel(param);
    $('legendLabel').textContent = `${title}${data.unit && !title.includes(data.unit) ? ` · ${data.unit}` : ''}`;
    $('legendNote').textContent = data.annotation || '';
    legendCanvas.hidden = Boolean(data.contour_only);
    legendTicks.hidden = Boolean(data.contour_only);
    if ((!data.stops || !data.stops.length) && !data.contour_only) { legendBar.classList.add('hidden'); return; }
    legendBar.classList.remove('hidden');

    const ctx = legendCanvas.getContext('2d');
    ctx.clearRect(0, 0, legendCanvas.width, legendCanvas.height);
    if (data.discrete && data.bounds?.length > 1 && data.colors?.length) {
      const min = data.bounds[0];
      const range = data.bounds[data.bounds.length - 1] - min;
      data.colors.forEach((color, i) => {
        const x = (data.bounds[i] - min) / range * legendCanvas.width;
        const end = (data.bounds[i + 1] - min) / range * legendCanvas.width;
        ctx.fillStyle = color;
        ctx.fillRect(x, 0, end - x, legendCanvas.height);
      });
    } else {
      const grad = ctx.createLinearGradient(0, 0, legendCanvas.width, 0);
      for (const stop of data.stops) grad.addColorStop(stop.pos, stop.color);
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, legendCanvas.width, legendCanvas.height);
    }

    legendTicks.innerHTML = '';
    const ticks = data.tick_vals?.length ? data.tick_vals
      : Array.from({length: 5}, (_, i) => data.vmin + (data.vmax - data.vmin) * i / 4);
    for (const v of ticks) {
      const sp = document.createElement('span');
      sp.style.left = `${(v - data.vmin) / (data.vmax - data.vmin) * 100}%`;
      sp.textContent = Number(v.toFixed(Math.abs(data.vmax - data.vmin) < 5 ? 2 : 0));
      legendTicks.appendChild(sp);
    }
  } catch { if (requestId === mobileLegendRequestId) legendBar.classList.add('hidden'); }
}

// ═══════════════════════════════════════════════════════════════
// Map tap → value query (models only)
// ═══════════════════════════════════════════════════════════════

let _valueAbort = null;
async function onMapTap(e) {
  if (S.tab !== 'models' || !S.runId || S.fxx == null) return;
  if (_valueAbort) _valueAbort.abort();
  _valueAbort = new AbortController();
  try {
    const lat = e.latlng.lat.toFixed(4);
    const lon = e.latlng.lng.toFixed(4);
    const r = await fetch(
      `${BASE}/api/value-png/${S.model}/${S.runId}/${S.fxx}/${S.param}?lat=${lat}&lon=${lon}`,
      { signal: _valueAbort.signal }
    );
    if (!r.ok) { topValue.textContent = ''; return; }
    const d = await r.json();
    topValue.textContent = d.value != null ? `${d.value.toFixed(1)}${d.unit||''}` : '';
  } catch { /* abort or network error */ }
}

// ═══════════════════════════════════════════════════════════════
// Bottom sheet
// ═══════════════════════════════════════════════════════════════

let sheetCloseTimer = null;

function openSheet() {
  clearTimeout(sheetCloseTimer);
  selectorPill.setAttribute('aria-expanded', 'true');
  if (S.tab === 'models')         buildModelSheet();
  else if (S.tab === 'satellite') buildSatSheet();
  else if (S.tab === 'radar')     buildRadarSheet();
  else if (S.tab === 'home')      return;

  sheetBackdrop.classList.remove('hidden');
  bottomSheet.classList.remove('hidden');
  requestAnimationFrame(() => {
    sheetBackdrop.classList.add('visible');
    bottomSheet.classList.add('visible');
  });
}

function closeSheet() {
  clearTimeout(sheetCloseTimer);
  selectorPill.setAttribute('aria-expanded', 'false');
  bottomSheet.classList.remove('visible');
  sheetBackdrop.classList.remove('visible');
  sheetCloseTimer = setTimeout(() => {
    bottomSheet.classList.add('hidden');
    sheetBackdrop.classList.add('hidden');
  }, 300);
}

function formatMobileRunId(runId) {
  if (runId && runId.length === 10) {
    const mm = runId.slice(4,6), dd = runId.slice(6,8), hh = runId.slice(8,10);
    return `${mm}/${dd} ${hh}Z`;
  }
  return runId || '';
}

function modelSupportsParam(model, param) {
  return !param?.models?.length || param.models.includes(model);
}

function paramGroupFor(id) {
  return PARAMS.find((group) => group.items.some((item) => item.id === id)) || null;
}

function modelGroupIsOpen(groupId) {
  if (Object.prototype.hasOwnProperty.call(S.modelParamGroupsOpen, groupId)) {
    return !!S.modelParamGroupsOpen[groupId];
  }
  const activeGroup = paramGroupFor(S.param);
  return groupId === 'surface' || groupId === 'precip' || activeGroup?.id === groupId;
}

function ensureActiveModelGroupOpen() {
  const activeGroup = paramGroupFor(S.param);
  if (activeGroup) S.modelParamGroupsOpen[activeGroup.id] = true;
}

function firstSupportedModelParam(model, preferred) {
  if (preferred) {
    const preferredDef = PARAMS.flatMap((group) => group.items).find((item) => item.id === preferred);
    if (preferredDef && modelSupportsParam(model, preferredDef)) return preferred;
  }
  for (const group of PARAMS) {
    const match = group.items.find((item) => modelSupportsParam(model, item));
    if (match) return match.id;
  }
  return 'temp_2m';
}

function buildModelSheet() {
  let html = '<div class="sheet-section"><div class="sheet-section-title">Model</div><div class="sheet-chips">';
  for (const [id, m] of Object.entries(MODELS)) {
    html += `<button type="button" class="chip${S.model===id?' active':''}" data-model="${id}">${m.label}</button>`;
  }
  html += '</div></div>';

  // Run selector
  if (S.modelRuns.length > 0) {
    html += '<div class="sheet-section"><label class="sheet-section-title" for="mobileRunSelect">Run (UTC)</label><select id="mobileRunSelect" class="sheet-run-select">';
    for (const r of S.modelRuns) {
      const rid = r.run_id || r;
      const isActive = rid === S.runId;
      const status = window.ViewerUI.runStatus(r, modelRunIsOld(rid, S.model));
      html += `<option value="${rid}"${isActive ? ' selected' : ''}>${formatMobileRunId(rid)} · ${status}</option>`;
    }
    html += '</select></div>';
  }

  const favoriteItems = PARAMS.flatMap((group) => group.items).filter((item) => modelSupportsParam(S.model, item) && window.ViewerUI.favorites.has(item.id));
  html += '<div class="sheet-section sheet-favorites"><div class="sheet-section-title">Favorites</div><div class="sheet-list">';
  for (const item of favoriteItems) html += mobileProductRow(item);
  if (!favoriteItems.length) html += '<p class="sheet-favorites-note">Tap ☆ beside a product to save it here.</p>';
  html += '</div></div>';

  html += '<label class="sheet-search-label" for="modelProductSearch">Find a product</label><input id="modelProductSearch" class="sheet-search" type="search" placeholder="Temperature, snow, helicity…" autocomplete="off">';
  for (const g of PARAMS) {
    const items = g.items.filter((p) => modelSupportsParam(S.model, p));
    if (!items.length) continue;
    const isOpen = modelGroupIsOpen(g.id);
    html += `<div class="sheet-accordion${isOpen ? ' open' : ''}" data-model-group="${g.id}">
      <button class="sheet-accordion-toggle" data-model-group-toggle="${g.id}" aria-expanded="${isOpen ? 'true' : 'false'}">
        <span>${g.group}</span>
        <svg class="sheet-accordion-chevron" width="14" height="14" viewBox="0 0 12 12" fill="none"><path d="M3.5 2.5l3 3-3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
      <div class="sheet-accordion-body">
        <div class="sheet-list">`;
    for (const p of items) {
      html += mobileProductRow(p);
    }
    html += `</div>
      </div>
    </div>`;
  }
  sheetContent.innerHTML = html;

  $('modelProductSearch').addEventListener('input', filterMobileProducts);
  sheetContent.querySelectorAll('[data-favorite-toggle]').forEach((button) => {
    button.addEventListener('click', toggleMobileFavorite);
  });
  sheetContent.querySelectorAll('.chip[data-model]').forEach((el) => {
    el.addEventListener('click', () => {
      S.model = el.dataset.model;
      S.runId = null;
      S.modelRuns = [];
      S.modelAvailability = {};
      S.param = firstSupportedModelParam(S.model, S.param);
      ensureActiveModelGroupOpen();
      closeSheet();
      initModels();
    });
  });
  $('mobileRunSelect')?.addEventListener('change', function changeMobileRun(event) {
      closeSheet();
      loadRun(event.target.value);
  });
  sheetContent.querySelectorAll('[data-model-group-toggle]').forEach((el) => {
    el.addEventListener('click', () => {
      const groupId = el.dataset.modelGroupToggle;
      S.modelParamGroupsOpen[groupId] = !modelGroupIsOpen(groupId);
      buildModelSheet();
    });
  });
  sheetContent.querySelectorAll('.sheet-item[data-param]').forEach((el) => {
    el.addEventListener('click', () => {
      S.param = el.dataset.param;
      ensureActiveModelGroupOpen();
      closeSheet();
      updatePill(`${MODELS[S.model]?.label || S.model} / ${paramLabel(S.param)}`);
      loadRun(S.runId);
    });
  });
}

/** Separate the product action and favorite action into two touch targets. */
function mobileProductRow(product) {
  const saved = window.ViewerUI.favorites.has(product.id);
  return `<div class="sheet-product-row"><button type="button" class="sheet-item${S.param===product.id?' active':''}" data-param="${product.id}">${product.label}</button><button type="button" class="favorite-star" data-favorite-toggle="${product.id}" aria-pressed="${saved}" aria-label="${saved ? 'Remove' : 'Save'} ${product.label} ${saved ? 'from' : 'to'} favorites">${saved ? '★' : '☆'}</button></div>`;
}

function toggleMobileFavorite(event) {
  const scroll = sheetContent.scrollTop;
  const query = $('modelProductSearch').value;
  window.ViewerUI.favorites.toggle(event.currentTarget.dataset.favoriteToggle);
  buildModelSheet();
  $('modelProductSearch').value = query;
  filterMobileProducts();
  sheetContent.scrollTop = scroll;
}

/** Filter product groups while preserving their original expanded state. */
function filterMobileProducts() {
  const query = $('modelProductSearch').value.trim().toLowerCase();
  sheetContent.querySelectorAll('[data-model-group]').forEach((group) => {
    let matches = 0;
    group.querySelectorAll('[data-param]').forEach((button) => {
      const visible = !query || button.textContent.toLowerCase().includes(query) || button.dataset.param.includes(query);
      button.hidden = !visible;
      const row = button.closest('.sheet-product-row');
      if (row) row.hidden = !visible;
      if (visible) matches++;
    });
    group.hidden = matches === 0;
    const open = query ? matches > 0 : modelGroupIsOpen(group.dataset.modelGroup);
    group.classList.toggle('open', open);
    group.querySelector('[aria-expanded]').setAttribute('aria-expanded', String(open));
  });
  const favorites = sheetContent.querySelector('.sheet-favorites');
  if (favorites) {
    favorites.querySelectorAll('.sheet-product-row').forEach((row) => {
      const button = row.querySelector('[data-param]');
      row.hidden = !!query && !button.textContent.toLowerCase().includes(query) && !button.dataset.param.includes(query);
    });
  }
}

function buildSatSheet() {
  const regional = S.satScope === 'regional';
  const regionLabel = SATELLITE_REGION_NAMES[S.satRegion] || 'Auto';
  const sourceLabel = satelliteSourceLabel(S.sat);

  let html = `<div class="sheet-note">
    <div class="sheet-note-title">Satellite View</div>
    <div class="sheet-note-text">${regional ? `${regionLabel} imagery auto-uses ${sourceLabel}.` : `${sourceLabel} national imagery with recent products from the desktop site.`}</div>
  </div>`;

  html += '<div class="sheet-section"><div class="sheet-section-title">Coverage</div><div class="sheet-chips">';
  html += `<button type="button" class="chip${regional ? '' : ' active'}" data-sat-scope="national">National</button>`;
  html += `<button type="button" class="chip${regional ? ' active' : ''}" data-sat-scope="regional">Regional</button>`;
  html += '</div></div>';

  if (regional) {
    html += '<div class="sheet-section"><div class="sheet-section-title">Region</div><div class="sheet-chip-grid">';
    for (const [id, label] of Object.entries(SATELLITE_REGION_NAMES)) {
      html += `<button type="button" class="chip chip--grid${S.satRegion===id?' active':''}" data-sat-region="${id}">${label}</button>`;
    }
    html += '</div></div>';

    html += '<div class="sheet-section"><div class="sheet-section-title">Regional Product</div><div class="sheet-list">';
    for (const p of SAT_REGIONAL_PRODUCTS) {
      html += satSheetItem(p, S.satProduct === p.id, 'data-sat');
    }
    html += '</div></div>';
  } else {
    for (const g of SAT_PRODUCTS) {
      html += `<div class="sheet-section"><div class="sheet-section-title">${g.group}</div><div class="sheet-list">`;
      for (const p of g.items) {
        html += satSheetItem(p, S.satProduct === p.id, 'data-sat');
      }
      html += '</div></div>';
    }
  }
  sheetContent.innerHTML = html;

  sheetContent.querySelectorAll('.chip[data-sat-scope]').forEach((el) => {
    el.addEventListener('click', () => {
      const nextScope = el.dataset.satScope === 'regional' ? 'regional' : 'national';
      if (nextScope === S.satScope) return;
      S.satScope = nextScope;
      if (nextScope === 'regional') {
        S.satRegion = S.satRegion || SATELLITE_SCOPE_DEFAULTS.regional.region;
        if (!SAT_REGIONAL_PRODUCT_IDS.has(S.satProduct)) {
          S.satProduct = SATELLITE_SCOPE_DEFAULTS.regional.product;
        }
      } else {
        S.satRegion = '';
        if (!S.satProduct) S.satProduct = SATELLITE_SCOPE_DEFAULTS.national.product;
      }
      syncSatelliteSelection();
      buildSatSheet();
    });
  });

  sheetContent.querySelectorAll('.chip[data-sat-region]').forEach((el) => {
    el.addEventListener('click', () => {
      S.satRegion = el.dataset.satRegion || SATELLITE_SCOPE_DEFAULTS.regional.region;
      syncSatelliteSelection();
      buildSatSheet();
    });
  });

  sheetContent.querySelectorAll('.sheet-item[data-sat]').forEach((el) => {
    el.addEventListener('click', () => {
      S.satProduct = el.dataset.sat;
      syncSatelliteSelection();
      closeSheet();
      initSatellite();
    });
  });
}

function buildRadarSheet() {
  // Product chips first — always visible at top
  let html = '<div class="sheet-section"><div class="sheet-section-title">Product</div><div class="sheet-chips">';
  for (const p of RADAR_PRODUCTS) {
    html += `<button type="button" class="chip${S.radarProduct===p.id?' active':''}" data-rprod="${p.id}">${p.label}</button>`;
  }
  html += '</div></div>';

  // Tilt selector
  const tilts = S.radarTilts || [0.5];
  html += '<div class="sheet-section"><div class="sheet-section-title">Elevation Tilt</div><div class="sheet-chips">';
  for (const t of tilts) {
    html += `<button type="button" class="chip${S.radarTilt===t?' active':''}" data-tilt="${t}">${t.toFixed(1)}\u00B0</button>`;
  }
  html += '</div></div>';

  // Current site indicator + search below
  const cityName = RADAR_CITIES[S.radarSite] || S.radarSite;
  html += `<div class="sheet-section">
    <div class="sheet-section-title">Radar Site — ${S.radarSite} (${cityName})</div>
    <input class="sheet-search" type="text" placeholder="Search city, state, or radar ID..." id="radarSearch" autocomplete="off" autocorrect="off" autocapitalize="off">
    <div id="radarResults" class="sheet-list"></div>
  </div>`;
  sheetContent.innerHTML = html;

  const searchInput = $('radarSearch');
  const resultsDiv = $('radarResults');

  // Don't show full list by default — only when user types
  searchInput.addEventListener('input', () => {
    const q = searchInput.value.trim();
    if (q.length > 0) {
      showRadarSiteResults(resultsDiv, q);
    } else {
      resultsDiv.innerHTML = '';
    }
  });

  // Focus shows all sites
  searchInput.addEventListener('focus', () => {
    if (searchInput.value.trim().length === 0) {
      showRadarSiteResults(resultsDiv, '');
    }
  });

  // Handle direct 3-4 char radar ID entry
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const v = searchInput.value.trim().toUpperCase();
      if (v.length >= 3 && v.length <= 4) {
        selectRadarSite(v.length === 3 ? 'K' + v : v);
      }
    }
  });

  sheetContent.querySelectorAll('.chip[data-rprod]').forEach((el) => {
    el.addEventListener('click', () => {
      S.radarProduct = el.dataset.rprod;
      closeSheet();
      initRadar();
    });
  });
  sheetContent.querySelectorAll('.chip[data-tilt]').forEach((el) => {
    el.addEventListener('click', () => {
      S.radarTilt = parseFloat(el.dataset.tilt);
      closeSheet();
      initRadar();
    });
  });
}

function showRadarSiteResults(container, query) {
  const q = query.toLowerCase();
  let matches = [];

  if (q.length === 0) {
    // Show all sites sorted by ID
    matches = Object.entries(RADAR_CITIES).map(([id, city]) => ({ id, city }));
  } else {
    // Search by site ID, city name, or full state name
    for (const [id, city] of Object.entries(RADAR_CITIES)) {
      const idMatch = id.toLowerCase().includes(q);
      const cityMatch = city.toLowerCase().includes(q);
      // Extract state abbrev from "City, ST" and resolve to full name
      const stAbbr = city.split(', ').pop();
      const fullState = ST[stAbbr] || '';
      const stateMatch = fullState && fullState.toLowerCase().includes(q);
      if (idMatch || cityMatch || stateMatch) {
        matches.push({ id, city, score: idMatch && cityMatch ? 0 : (idMatch ? 1 : (cityMatch ? 2 : 3)) });
      }
    }
    matches.sort((a, b) => (a.score - b.score) || a.city.localeCompare(b.city));
  }

  // Limit displayed results
  const show = matches.slice(0, 20);
  container.innerHTML = show.map((m) =>
    `<div class="sheet-item${S.radarSite===m.id?' active':''}" data-site="${m.id}">
      <span>${m.city}</span>
      <span class="item-sub">${m.id}</span>
    </div>`
  ).join('');

  container.querySelectorAll('.sheet-item[data-site]').forEach((el) => {
    el.addEventListener('click', () => selectRadarSite(el.dataset.site));
  });
}

function selectRadarSite(site) {
  S.radarSite = site;
  localStorage.setItem('wx_radar_site', site);
  closeSheet();
  initRadar();
}

// ═══════════════════════════════════════════════════════════════
// Radar site marker dots (clickable NEXRAD station locations)
// ═══════════════════════════════════════════════════════════════
let _radarSiteMarkerLayer = null;
let _radarSiteLocationsCache = null;
let _radarSiteBuildPromise = null;

async function buildRadarSiteMarkers() {
  if (_radarSiteMarkerLayer) return;
  if (!_radarSiteLocationsCache) {
    try {
      const resp = await fetch(`${BASE}/api/radar/sites`);
      if (resp.ok) {
        const data = await resp.json();
        _radarSiteLocationsCache = data.sites || {};
      }
    } catch (e) {
      console.log('Failed to fetch radar site locations:', e);
      return;
    }
  }
  if (!_radarSiteLocationsCache) return;

  _radarSiteMarkerLayer = L.layerGroup();
  for (const [siteId, info] of Object.entries(_radarSiteLocationsCache)) {
    if (!info.lat || !info.lon) continue;
    const radarIconSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="10"/>
      <path d="M12 12l-7-7"/>
      <path d="M12 12a3 3 0 0 0-3-3"/>
      <path d="M12 12a7 7 0 0 0-7-7"/>
    </svg>`;
    const marker = L.marker([info.lat, info.lon], {
      icon: L.divIcon({
        html: radarIconSvg,
        className: 'radar-site-icon',
        iconSize: [18, 18],
        iconAnchor: [9, 9],
      }),
      interactive: true,
      pane: 'radarSitePane',
    });
    marker.bindTooltip(`${siteId} – ${info.name || ''}`, {
      direction: 'top',
      offset: [0, -12],
      className: 'radar-site-tooltip',
    });
    marker.on('click', () => {
      selectRadarSite(siteId);
    });
    _radarSiteMarkerLayer.addLayer(marker);
  }
}

function showRadarSiteMarkers() {
  const pane = leafletMap?.getPane('radarSitePane');
  if (pane) pane.style.display = '';
  if (!leafletMap || !_radarSiteMarkerLayer) return;
  if (!leafletMap.hasLayer(_radarSiteMarkerLayer)) {
    _radarSiteMarkerLayer.addTo(leafletMap);
  }
}

function hideRadarSiteMarkers() {
  const pane = leafletMap?.getPane('radarSitePane');
  if (pane) pane.style.display = 'none';
}

// ═══════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════

function updatePill(text) { pillLabel.textContent = text; }

function satSheetItem(product, active, attrName) {
  const detail = product.detail ? `<span class="item-sub">${product.detail}</span>` : '';
  return `<button type="button" class="sheet-item${active ? ' active' : ''}" ${attrName}="${product.id}"><span>${product.label}</span>${detail}</button>`;
}

function paramLabel(id) {
  for (const g of PARAMS) for (const p of g.items) if (p.id === id) return p.label;
  return id;
}

function satLabel(id) {
  for (const g of SAT_PRODUCTS) for (const p of g.items) if (p.id === id) return p.label;
  for (const p of SAT_REGIONAL_PRODUCTS) if (p.id === id) return p.label;
  return id;
}

function satelliteSourceLabel(id) {
  if (id === 'goes18') return 'GOES-18 West';
  return 'GOES-19 East';
}

function satPillLabel() {
  const region = S.satRegion ? ` / ${SATELLITE_REGION_NAMES[S.satRegion] || S.satRegion}` : '';
  return `${satelliteSourceLabel(S.sat)}${region} / ${satLabel(S.satProduct)}`;
}

function satTopInfo() {
  const region = S.satRegion ? ` • ${SATELLITE_REGION_NAMES[S.satRegion] || S.satRegion}` : '';
  return `${satelliteSourceLabel(S.sat)}${region}`;
}

function syncSatelliteSelection() {
  if (S.satScope === 'regional') {
    S.satRegion = S.satRegion || SATELLITE_SCOPE_DEFAULTS.regional.region;
    S.sat = SATELLITE_REGION_SOURCES[S.satRegion] || 'goes19';
    if (!SAT_REGIONAL_PRODUCT_IDS.has(S.satProduct)) {
      S.satProduct = SATELLITE_SCOPE_DEFAULTS.regional.product;
    }
    return;
  }
  S.satRegion = '';
  S.sat = 'goes19';
  if (!S.satProduct) {
    S.satProduct = SATELLITE_SCOPE_DEFAULTS.national.product;
  }
}

function radarLabel(id) {
  const p = RADAR_PRODUCTS.find((r) => r.id === id);
  return p ? p.label : id;
}

function formatTs(ts) {
  if (!ts) return '';
  try {
    ts = ts.replace(/^(\d{4})-(\d{3})T/, (_, year, day) => {
      return new Date(Date.UTC(Number(year), 0, Number(day))).toISOString().slice(0, 10) + 'T';
    });
    // Handle formats: "2026-03-17T00-36-18.2Z" or "20260317_003400"
    let d;
    if (ts.includes('T')) {
      // ISO-ish format — replace dashes in time portion
      d = new Date(ts.replace(/(\d{2})-(\d{2})-(\d{2}(?:\.\d+)?)Z?$/, '$1:$2:$3Z'));
    } else if (/^\d{8}_\d{6}$/.test(ts)) {
      // YYYYMMDD_HHMMSS
      const y = ts.slice(0,4), mo = ts.slice(4,6), dy = ts.slice(6,8);
      const h = ts.slice(9,11), mi = ts.slice(11,13), s = ts.slice(13,15);
      d = new Date(`${y}-${mo}-${dy}T${h}:${mi}:${s}Z`);
    } else {
      return ts;
    }
    if (isNaN(d)) return ts;
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true, timeZoneName: 'short' });
  } catch { return ts; }
}

// ═══════════════════════════════════════════════════════════════
// Sounding
// ═══════════════════════════════════════════════════════════════

async function loadMobileSounding(lat, lng) {
  if (!S.runId || S.fxx == null) return;
  S.soundingOpen = true;
  S.soundingPoint = { lat, lng };
  S.soundingData = null;
  const seq = ++S.soundingSeq;

  // Show panel with loading state
  if (soundingTitle) soundingTitle.textContent = `${lat.toFixed(3)}, ${lng.toFixed(3)}`;
  if (soundingMeta) soundingMeta.textContent = `${S.model.toUpperCase()} ${formatMobileRunId(S.runId)} F${String(S.fxx).padStart(2, '0')}`;
  if (soundingStatus) { soundingStatus.textContent = 'Loading sounding…'; soundingStatus.classList.remove('hidden', 'warn'); }
  if (soundingChartBox) soundingChartBox.innerHTML = '';
  if (soundingHodoBox) soundingHodoBox.innerHTML = '';
  if (soundingDiagBox) soundingDiagBox.innerHTML = '';
  if (soundingPanel) soundingPanel.classList.remove('hidden');

  try {
    const data = await api(`/api/sounding/${encodeURIComponent(S.model)}/${encodeURIComponent(S.runId)}/${S.fxx}?lat=${lat}&lon=${lng}`);
    if (seq !== S.soundingSeq) return;
    S.soundingData = data;
    if (soundingStatus) soundingStatus.classList.add('hidden');
    if (data.quality === 'sparse' && soundingStatus) {
      soundingStatus.textContent = 'Sparse profile — some standard levels missing.';
      soundingStatus.classList.remove('hidden');
      soundingStatus.classList.add('warn');
    }
    if (typeof renderMobileSkewT === 'function') renderMobileSkewT(soundingChartBox, data);
    if (typeof renderMobileHodograph === 'function') renderMobileHodograph(soundingHodoBox, data);
    if (typeof renderMobileSoundingDiag === 'function') renderMobileSoundingDiag(soundingDiagBox, data);
  } catch (err) {
    if (seq !== S.soundingSeq) return;
    if (soundingStatus) {
      soundingStatus.textContent = 'No sounding data available for this location/frame.';
      soundingStatus.classList.remove('hidden');
      soundingStatus.classList.add('warn');
    }
  }
}

function closeSounding() {
  S.soundingOpen = false;
  S.soundingSeq++;
  if (soundingPanel) soundingPanel.classList.add('hidden');
}

// ═══════════════════════════════════════════════════════════════
// Boot
// ═══════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', init);
