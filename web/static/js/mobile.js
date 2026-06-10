'use strict';

/* ═══════════════════════════════════════════════════════════════
   WX Viewer — Mobile PWA
   ═══════════════════════════════════════════════════════════════ */

const BASE = (() => {
  const p = window.location.pathname || '';
  if (p.startsWith('/wx/')) return '/wx';
  return '';
})();

// ── State ──────────────────────────────────────────────────────
const S = {
  tab: 'home',

  // Models
  model: 'hrrr',
  runId: null,
  modelRuns: [],  // cached list of available runs
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
    if (s === 'ok' || s === 'error') { _pumpQueue(); return; }
    _loadActive++;
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
  _loadQueue.length = 0;
  _productUrls = [];
}

// Update the scrubber info row with loading progress or current frame position.
function updateCacheStatus() {
  if (!scrubFrameCount) return;
  if (S.tab === 'surface') {
    const total = S.surfaceFrames.length;
    if (!total) return;
    const loaded = S.surfaceFrames.reduce((n, frame) => n + (S.surfacePayloadCache[frame.generated] ? 1 : 0), 0);
    const curIdx = parseInt(scrubSlider.value);
    if (loaded < total) {
      scrubFrameCount.textContent = `↓ ${loaded}/${total}`;
      scrubFrameCount.classList.add('loading');
    } else {
      scrubFrameCount.textContent = `${curIdx + 1}/${total}`;
      scrubFrameCount.classList.remove('loading');
    }
    return;
  }
  const total = _productUrls.length;
  if (!total) return;
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
    return `${BASE}/rendered/${S.model}/${S.runId}/${S.param}/f${String(f).padStart(2,'0')}.png`;
  }
  if (S.tab === 'satellite') {
    const f = S.satFrames[idx];
    return f ? `${BASE}/rendered/${String(f.png_path||'').replace(/^\//,'')}` : null;
  }
  if (S.tab === 'radar') {
    const f = S.radarFrames[idx];
    return f ? `${BASE}/rendered/${String(f.png_path||'').replace(/^\//,'')}` : null;
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
  rap:    { label: 'RAP', maxFxx: 21 },
  rrfs:   { label: 'RRFS', maxFxx: 60 },
  href:   { label: 'HREF', maxFxx: 48 },
  gfs:    { label: 'GFS', maxFxx: 120 },
  gefs:   { label: 'GEFS', maxFxx: 240 },
  ecmwf:  { label: 'ECMWF', maxFxx: 144 },
  gdps:   { label: 'GDPS', maxFxx: 240 },
  rtma:   { label: 'RTMA', maxFxx: 0 },
};

const MODEL_PARAM_ALIASES = {
  nam3km: ['nam'],
  nam12km: ['nam'],
  rap: ['hrrr', 'gfs', 'nam'],
  rrfs: ['hrrr', 'gfs'],
  href: ['hrrr', 'gfs'],
  gefs: ['gfs'],
  gdps: ['ecmwf', 'gfs'],
};

const PARAMS = [
  { id: 'surface', group: 'Surface', items: [
    { id: 'temp_2m', label: 'Temp 2m', models: ['ecmwf', 'gfs', 'hrrr', 'nam', 'rtma'] },
    { id: 'dewpoint_2m', label: 'Dewpoint', models: ['ecmwf', 'gfs', 'hrrr', 'nam', 'rtma'] },
    { id: 'wind_speed', label: 'Wind', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'wind_gusts', label: 'Wind Gusts', models: ['ecmwf', 'gfs', 'hrrr', 'nam', 'rtma'] },
    { id: 'max_wind', label: 'Max 10m Wind', models: ['hrrr'] },
    { id: 'mslp', label: 'MSLP', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'psfc', label: 'Surface Pressure', models: ['rtma'] },
    { id: 'pwat', label: 'PWAT', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'cloud_cover', label: 'Cloud Cover', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'pbl_height', label: 'PBL Height', models: ['hrrr', 'nam'] },
  ]},
  { id: 'precip', group: 'Precipitation', items: [
    { id: 'reflectivity', label: 'Reflectivity', models: ['gfs', 'hrrr', 'nam', 'rrfs'] },
    { id: 'precip', label: 'Precip', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'lightning', label: 'Lightning', models: ['hrrr', 'nam'] },
    { id: 'echo_tops', label: 'Echo Tops', models: ['hrrr', 'nam'] },
  ]},
  { id: 'instability', group: 'Instability', items: [
    { id: 'mlcape', label: 'MLCAPE', models: ['gfs', 'hrrr', 'nam'] },
    { id: 'mlcin', label: 'MLCIN', models: ['gfs', 'hrrr', 'nam'] },
    { id: 'mucape', label: 'MUCAPE', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'mucin', label: 'MUCIN', models: ['gfs', 'hrrr', 'nam'] },
    { id: 'cape', label: 'SBCAPE', models: ['gfs', 'hrrr', 'nam'] },
    { id: 'cin', label: 'SBCIN', models: ['gfs', 'hrrr', 'nam'] },
    { id: 'cape_03km', label: '0-3km CAPE', models: ['hrrr', 'nam'] },
    { id: 'lifted_index', label: 'Lifted Index', models: ['gfs', 'hrrr', 'nam'] },
    { id: 'lcl', label: 'LCL Height', models: ['hrrr', 'nam'] },
  ]},
  { id: 'composites', group: 'Composites', items: [
    { id: 'stp', label: 'STP', models: ['hrrr', 'nam'] },
    { id: 'scp', label: 'Supercell Comp', models: ['hrrr', 'nam'] },
    { id: 'vil', label: 'VIL', models: ['hrrr', 'nam'] },
    { id: 'uh_25km', label: '2-5km UH', models: ['hrrr', 'nam'] },
    { id: 'uh_02km', label: '0-2km UH', models: ['hrrr'] },
    { id: 'uh_03km', label: '0-3km UH', models: ['hrrr', 'nam'] },
    { id: 'uh_25km_swath', label: '2-5km UH Swath', models: ['hrrr', 'nam'] },
    { id: 'uh_03km_swath', label: '0-3km UH Swath', models: ['hrrr', 'nam'] },
    { id: 'hail', label: 'Max Hail Size', models: ['hrrr'] },
    { id: 'cti', label: 'Chase Target', models: ['hrrr', 'nam'] },
    { id: 'msi', label: 'Meso Strength', models: ['hrrr'] },
    { id: 'smi', label: 'Storm Mode', models: ['hrrr', 'nam'] },
    { id: 'ship', label: 'Sig Hail', models: ['hrrr', 'nam'] },
    { id: 'dcp', label: 'Derecho Comp', models: ['hrrr', 'nam'] },
    { id: 'citi', label: 'CI Timing', models: ['hrrr', 'nam'] },
    { id: 'cvi', label: 'Chase Visibility', models: ['hrrr', 'nam'] },
  ]},
  { id: 'kinematics', group: 'Kinematics', items: [
    { id: 'srh_01km', label: '0-1km SRH', models: ['hrrr', 'nam'] },
    { id: 'srh_03km', label: '0-3km SRH', models: ['gfs', 'hrrr', 'nam'] },
    { id: 'efhl', label: 'Eff. Helicity', models: ['hrrr'] },
    { id: 'shear_06km', label: '0-6km Shear', models: ['hrrr', 'nam'] },
    { id: 'shear_01km', label: '0-1km Shear', models: ['hrrr', 'nam'] },
  ]},
  { id: 'winter', group: 'Winter', items: [
    { id: 'visibility', label: 'Visibility', models: ['gfs', 'hrrr', 'nam', 'rtma'] },
    { id: 'ceil', label: 'Ceiling Height', models: ['rrfs'] },
    { id: 'snow_depth', label: 'Snow Depth', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'snowfall', label: 'Snowfall', models: ['ecmwf'] },
    { id: 'frozen_pct', label: 'Frozen Precip %', models: ['hrrr', 'rrfs'] },
    { id: 'frzlvl', label: 'Freezing Level', models: ['gfs', 'hrrr', 'nam'] },
    { id: 'kuchera_6hr', label: '6hr Kuchera Snow', models: ['hrrr'] },
    { id: 'kuchera_12hr', label: '12hr Kuchera Snow', models: ['hrrr'] },
    { id: 'snow10_12hr', label: '12hr 10:1 Snow', models: ['hrrr'] },
  ]},
  { id: 'pressure', group: 'Pressure Levels', items: [
    { id: 'temp_925mb', label: '925mb Temp', models: ['gfs', 'hrrr', 'nam'] },
    { id: 'temp_850mb', label: '850mb Heights / Temp', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'dewpoint_850mb', label: '850mb Heights / Dewpoint', models: ['hrrr', 'nam'] },
    { id: 'rh_850mb', label: '850mb Heights / RH', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'wind_850mb', label: '850mb Heights / Wind', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'temp_700mb', label: '700mb Heights / Temp', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'rh_700mb', label: '700mb Heights / RH', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'omega_700mb', label: '700mb Heights / Vert Vel', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'wind_700mb', label: '700mb Heights / Wind', models: ['hrrr', 'nam'] },
    { id: 'temp_500mb', label: '500mb Heights / Temp', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'vort_500mb', label: '500mb Heights / Vorticity', models: ['hrrr', 'nam'] },
    { id: 'wind_500mb', label: '500mb Heights / Wind', models: ['ecmwf', 'gfs', 'hrrr', 'nam'] },
    { id: 'wind_300mb', label: '300mb Wind (Jet)', models: ['ecmwf', 'gfs', 'hrrr'] },
    { id: 'wind_250mb', label: '250mb Heights / Wind', models: ['gfs', 'hrrr', 'nam'] },
  ]},
  { id: 'ensemble', group: 'Ensemble', items: [
    { id: 'gh_500mb_spread', label: '500mb Hgt Spread', models: ['gefs'] },
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

function init() {
  initMap();
  initEvents();
  initServiceWorker();

  switchTab('radar');

  // Load alerts on startup (on by default)
  $('spcToggle').classList.add('on');
  S.spcOn = true;
  S.alertsOn = true;
  loadAlerts();
  loadSPC();
  loadMCDs();
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
    'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png',
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
    'https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png',
    { subdomains: 'abcd', maxZoom: 19, crossOrigin: true, pane: 'labelsPane' }
  ).addTo(leafletMap);

  if (typeof SurfaceObs !== 'undefined') {
    SurfaceObs.init(leafletMap, BASE);
  }

  // Load state boundaries
  loadBoundaries();

  leafletMap.on('click', onMapTap);
  leafletMap.on('zoomend', _updateCountyVisibility);

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
    if (satelliteMode) {
      if (!leafletMap.hasLayer(S.satelliteCityLayer)) leafletMap.addLayer(S.satelliteCityLayer);
    } else if (leafletMap.hasLayer(S.satelliteCityLayer)) {
      leafletMap.removeLayer(S.satelliteCityLayer);
    }
  }
}

async function loadBoundaries() {
  try {
    const [statesResp, countriesResp, countiesResp, coastlineResp] = await Promise.all([
      fetch(`${BASE}/static/boundaries_states.json`),
      fetch(`${BASE}/static/boundaries_countries.json`),
      fetch(`${BASE}/static/boundaries_counties.json`),
      fetch(`${BASE}/static/boundaries_coastline.json`),
    ]);
    // Counties — thin, subtle, visible when zoomed in
    if (countiesResp.ok) {
      const countiesData = await countiesResp.json();
      const cOutline = L.geoJSON(countiesData, {
        pane: 'boundaryPane',
        style: { color: '#000', weight: 0.85, fillOpacity: 0, opacity: 0.14, interactive: false },
      }).addTo(leafletMap);
      const cLine = L.geoJSON(countiesData, {
        pane: 'boundaryPane',
        style: { color: '#fff', weight: 0.22, fillOpacity: 0, opacity: 0.16, interactive: false },
      }).addTo(leafletMap);
      S.countyLayers = [cOutline, cLine];
    }
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

    try {
      // Build a composite canvas manually from the map
      const mapEl = document.getElementById('map');
      const w = mapEl.offsetWidth * 2;  // 2x for retina
      const h = mapEl.offsetHeight * 2;
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext('2d');
      ctx.scale(2, 2);

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
        const ly = mapEl.offsetHeight - 28 - ltickH - lh - 20;
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

      // Bottom info bar
      const info = topInfo.textContent;
      const label = scrubLabel.textContent;
      if (info || label) {
        ctx.fillStyle = 'rgba(0,0,0,0.5)';
        ctx.fillRect(0, mapEl.offsetHeight - 28, mapEl.offsetWidth, 28);
        ctx.fillStyle = '#c8cdd6';
        ctx.font = '12px -apple-system, sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(`${info}  ${label}`, 10, mapEl.offsetHeight - 10);
      }

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
    }

    // Remove flash
    setTimeout(() => flash.remove(), 350);
  });

  // Share button
  shareBtn.addEventListener('click', () => shareSheet.classList.remove('hidden'));
  $('shareCancel').addEventListener('click', () => shareSheet.classList.add('hidden'));
  shareSheet.addEventListener('click', (e) => { if (e.target === shareSheet) shareSheet.classList.add('hidden'); });
  $('shareCopyLink').addEventListener('click', () => {
    navigator.clipboard.writeText(window.location.href).then(() => {
      $('shareCopyLink').textContent = 'Copied!';
      setTimeout(() => { $('shareCopyLink').textContent = 'Copy Link'; shareSheet.classList.add('hidden'); }, 1000);
    }).catch(() => {});
  });
  $('shareNative').addEventListener('click', () => {
    if (navigator.share) {
      navigator.share({ title: 'WxViewer', url: window.location.href }).catch(() => {});
    }
    shareSheet.classList.add('hidden');
  });

  // Sounding close
  if (soundingClose) soundingClose.addEventListener('click', closeSounding);

  // Swipe-to-dismiss sheet
  let startY = 0;
  bottomSheet.addEventListener('touchstart', (e) => { startY = e.touches[0].clientY; });
  bottomSheet.addEventListener('touchend', (e) => {
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

async function api(path) {
  const r = await fetch(`${BASE}${path}`);
  if (!r.ok) throw new Error(r.status);
  return r.json();
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

function switchTab(tab) {
  pause();
  stopRadarAutoRefresh();
  clearOverlay();
  if (typeof SurfaceObs !== 'undefined') {
    SurfaceObs.hide();
  }
  if (!S.alertsOn) clearAlerts();
  if (tab !== 'radar') stopLocationWatch();
  S.tab = tab;
  localStorage.setItem('wx_mobile_tab', tab);

  document.querySelectorAll('.tab').forEach((b) =>
    b.classList.toggle('active', b.dataset.tab === tab)
  );

  topValue.textContent = '';
  setMobileBasemapMode(tab === 'satellite' ? 'satellite' : 'default');
  updateMobileReferenceLayers();

  if (tab === 'home')       initHome();
  else if (tab === 'models')    initModels();
  else if (tab === 'satellite') initSatellite();
  else if (tab === 'radar')     initRadar();
  else if (tab === 'surface')   initSurface();

  // Show radar site markers only on radar tab
  if (tab === 'radar') {
    _radarSiteBuildPromise = buildRadarSiteMarkers().then(showRadarSiteMarkers);
  } else {
    hideRadarSiteMarkers();
  }
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
      preloadAllFrames();
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
    const data = await api('/api/alerts');
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
    const data = await api('/api/spc/day1_cat');  // MCDs via SPC endpoint
    // Try fetching MCDs from NWS mesoscale discussions
    const resp = await fetch(`${BASE}/api/mcd`).catch(() => null);
    if (!resp || !resp.ok) return;
    const mcdData = await resp.json();
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
  await loadRun();
}

async function loadRun(explicitRunId) {
  clearLoadQueue();
  try {
    const runs = await api(`/api/runs?model=${S.model}`);
    if (!runs.length) { topInfo.textContent = 'No runs'; return; }
    S.modelRuns = runs;

    let avail = null;

    if (explicitRunId) {
      // User picked a specific run
      S.runId = explicitRunId;
      avail = await api(`/api/available/${S.model}/${S.runId}`);
    } else {
      // Auto-select: prefer newest complete run, fall back to newest with any frames
      let fallbackRun = null, fallbackAvail = null;
      for (const r of S.modelRuns) {
        const a = await api(`/api/available/${S.model}/${r.run_id}`);
        const frames = (a[S.param] || a.reflectivity || []);
        if (!frames.length) continue;
        if (!fallbackRun) { fallbackRun = r; fallbackAvail = a; }
        if (r.is_complete) {
          S.runId = r.run_id;
          avail = a;
          break;
        }
      }
      if (!avail && fallbackRun) {
        S.runId = fallbackRun.run_id;
        avail = fallbackAvail;
      }
    }
    if (!avail || !Object.keys(avail).length) { topInfo.textContent = 'No frames'; return; }

    S.fxxList = (avail[S.param] || []).sort((a, b) => a - b);
    if (!S.fxxList.length) {
      if (S.param !== 'reflectivity' && avail.reflectivity) {
        S.param = 'reflectivity';
        S.fxxList = avail.reflectivity.sort((a, b) => a - b);
      }
      if (!S.fxxList.length) { topInfo.textContent = 'No frames'; return; }
    }

    const bounds = await api(`/api/bounds/${S.model}/${S.runId}`);
    S.overlayBounds = [[bounds.south, bounds.west], [bounds.north, bounds.east]];

    S.fxx = S.fxxList[0];
    setupScrubber(S.fxxList.length, 0);
    showModelFrame();
    updateLegend(S.param);
    preloadAllFrames();
  } catch (e) { topInfo.textContent = 'Load error'; }
}

function showModelFrame() {
  if (!S.runId || !S.fxxList.length || !S.overlayBounds) return;
  const url = `${BASE}/rendered/${S.model}/${S.runId}/${S.param}/f${String(S.fxx).padStart(2,'0')}.png`;
  if (isErrored(url)) return;
  setImageOverlay(url, S.overlayBounds);

  const runH = S.runId ? S.runId.slice(-2) : '??';
  topInfo.textContent = `${MODELS[S.model]?.label || S.model} ${S.runId?.slice(0,8)||''} ${runH}Z`;

  // Compute valid time string matching desktop format
  let labelText = `F${String(S.fxx).padStart(2,'0')}`;
  if (S.runId && S.runId.length >= 10) {
    const yyyy = parseInt(S.runId.substring(0, 4), 10);
    const mm   = parseInt(S.runId.substring(4, 6), 10) - 1;
    const dd   = parseInt(S.runId.substring(6, 8), 10);
    const hh   = parseInt(S.runId.substring(8, 10), 10);
    const init  = new Date(Date.UTC(yyyy, mm, dd, hh));
    const valid = new Date(init.getTime() + S.fxx * 3600000);
    const days  = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const utcH  = String(valid.getUTCHours()).padStart(2, '0');
    labelText += `  Valid: ${days[valid.getUTCDay()]} ${utcH}z ${valid.getUTCMonth()+1}/${valid.getUTCDate()}`;
  }
  scrubLabel.textContent = labelText;

  updateCacheStatus();
  updatePill(`${MODELS[S.model]?.label || S.model} / ${paramLabel(S.param)}`);
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
  clearLoadQueue();
  try {
    // API returns a bare array of frame objects
    const regionQuery = S.satRegion ? `?region=${encodeURIComponent(S.satRegion)}` : '';
    const frames = await api(`/api/satellite/frames/${S.sat}/${S.satProduct}${regionQuery}`);
    S.satFrames = Array.isArray(frames) ? frames : (frames.frames || []);
    if (!S.satFrames.length) { topInfo.textContent = 'No frames'; return; }
    S.satIdx = S.satFrames.length - 1;
    setupScrubber(S.satFrames.length, S.satIdx);
    showSatFrame();
    preloadAllFrames();
  } catch { topInfo.textContent = 'Load error'; }
}

function showSatFrame() {
  const f = S.satFrames[S.satIdx];
  if (!f) return;
  // png_path is relative like "satellite/png/goes19_natural_color_2026076_0041.png"
  const pngPath = String(f.png_path || '').replace(/^\//, '');
  const url = `${BASE}/rendered/${pngPath}`;
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
  // Fetch available tilts for this site (non-blocking)
  fetchRadarTilts(S.radarSite);
  await loadRadarFrames();
  const rp = RADAR_PRODUCTS.find((p) => p.id === S.radarProduct);
  if (rp && rp.legend) updateLegend(rp.legend);
  else legendBar.classList.add('hidden');
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
  loadSurfaceHistory();
}

async function loadSurfaceHistory() {
  try {
    const data = await api('/api/surface/history?hours=3');
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
      SurfaceObs.show();
    }
    await showSurfaceFrame(S.surfaceIdx);
  } catch {
    topInfo.textContent = 'Load error';
  }
}

async function showSurfaceFrame(idx) {
  const frame = S.surfaceFrames[idx];
  if (!frame?.generated) return;
  let payload = S.surfacePayloadCache[frame.generated];
  if (!payload) {
    payload = await api(`/api/surface/frame/${frame.generated}`);
    S.surfacePayloadCache[frame.generated] = payload;
  }
  S.surfaceIdx = idx;
  if (typeof SurfaceObs !== 'undefined') {
    SurfaceObs.setPayload(payload);
  }
  const label = formatTs(new Date(frame.generated * 1000).toISOString());
  topInfo.textContent = 'Surface observations';
  scrubLabel.textContent = label;
  updateCacheStatus();
}

async function fetchRadarTilts(site) {
  try {
    const data = await api(`/api/radar/tilts/${site}`);
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
  } catch { S.radarTilts = [0.5]; }
}

let _radarAutoRefreshTimer = null;

async function loadRadarFrames() {
  clearLoadQueue();
  try {
    // API returns a bare array of frame objects
    const frames = await api(`/api/radar/frames/${S.radarSite}/${S.radarProduct}?tilt=${S.radarTilt}`);
    if (S.tab !== 'radar') return; // tab changed during fetch
    S.radarFrames = Array.isArray(frames) ? frames : (frames.frames || []);
    if (!S.radarFrames.length) { topInfo.textContent = 'No frames'; return; }
    S.radarIdx = S.radarFrames.length - 1;
    setupScrubber(S.radarFrames.length, S.radarIdx);
    showRadarFrame(true);
    preloadAllFrames();
    startRadarAutoRefresh();
  } catch { topInfo.textContent = 'Load error'; }
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
  try {
    const frames = await api(`/api/radar/frames/${S.radarSite}/${S.radarProduct}?tilt=${S.radarTilt}`);
    if (S.tab !== 'radar') return;
    const newFrames = Array.isArray(frames) ? frames : (frames.frames || []);
    if (!newFrames.length) return;

    const wasAtEnd = S.radarIdx >= S.radarFrames.length - 1;
    const oldLatest = S.radarFrames.length ? S.radarFrames[S.radarFrames.length - 1]?.timestamp : null;
    const newLatest = newFrames[newFrames.length - 1]?.timestamp;

    if (newLatest === oldLatest && newFrames.length === S.radarFrames.length) return;

    S.radarFrames = newFrames;
    if (wasAtEnd) {
      S.radarIdx = newFrames.length - 1;
      showRadarFrame(false);
    }
    setupScrubber(newFrames.length, S.radarIdx);
    preloadAllFrames();
  } catch {}
}

function showRadarFrame(fitBounds) {
  const f = S.radarFrames[S.radarIdx];
  if (!f) return;
  // png_path is relative like "radar/png/KOAX_REF_0.5_20260317_003400.png"
  const pngPath = String(f.png_path || '').replace(/^\//, '');
  const url = `${BASE}/rendered/${pngPath}`;
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
}

// ═══════════════════════════════════════════════════════════════
// Shared overlay & scrubber
// ═══════════════════════════════════════════════════════════════

function setImageOverlay(url, bounds) {
  if (!bounds) return;
  if (S.overlayLayer) {
    S.overlayLayer.setUrl(url);
    S.overlayLayer.setBounds(bounds);
  } else {
    S.overlayLayer = L.imageOverlay(url, bounds, {
      opacity: 1, pane: 'wxOverlay', crossOrigin: true,
    }).addTo(leafletMap);
  }
  // Hide broken-image icon if the file doesn't exist; show again on success
  const el = S.overlayLayer.getElement?.();
  if (el) {
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

function _updateCountyVisibility() {
  const show = _countiesEnabled && leafletMap.getZoom() >= COUNTY_MIN_ZOOM;
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
}

function onScrub() {
  const idx = parseInt(scrubSlider.value);
  if (S.tab === 'home')       { S.homeIdx = idx; showHomeFrame(); }
  else if (S.tab === 'models')    { S.fxx = S.fxxList[idx] ?? 0; showModelFrame(); }
  else if (S.tab === 'satellite') { S.satIdx = idx; showSatFrame(); }
  else if (S.tab === 'radar')     { S.radarIdx = idx; showRadarFrame(false); }
  else if (S.tab === 'surface')   { showSurfaceFrame(idx); }
}

function togglePlay() {
  S.isPlaying ? pause() : play();
}

function play() {
  S.isPlaying = true;
  playIcon.classList.add('hidden');
  pauseIcon.classList.remove('hidden');
  advance();
}

function pause() {
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

  if (S.tab === 'surface') {
    scrubSlider.value = idx;
    onScrub();
    S.playTimer = setTimeout(advance, SPEEDS[S.speedIdx].ms * 3);
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
    S.playTimer = setTimeout(advance, SPEEDS[S.speedIdx].ms);
    return;
  }

  scrubSlider.value = idx;
  onScrub();
  S.playTimer = setTimeout(advance, SPEEDS[S.speedIdx].ms);
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
  try {
    const data = await api(`/api/legend/${param}`);
    if (!data.stops || !data.stops.length) { legendBar.classList.add('hidden'); return; }
    legendBar.classList.remove('hidden');

    const ctx = legendCanvas.getContext('2d');
    ctx.clearRect(0, 0, legendCanvas.width, legendCanvas.height);
    const grad = ctx.createLinearGradient(0, 0, legendCanvas.width, 0);
    for (const s of data.stops) grad.addColorStop(s.pos, s.color);
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, legendCanvas.width, legendCanvas.height);

    legendTicks.innerHTML = '';
    const steps = 4;
    for (let i = 0; i <= steps; i++) {
      const v = data.vmin + (data.vmax - data.vmin) * (i / steps);
      const sp = document.createElement('span');
      sp.textContent = Math.round(v);
      legendTicks.appendChild(sp);
    }
  } catch { legendBar.classList.add('hidden'); }
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

function openSheet() {
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
  bottomSheet.classList.remove('visible');
  sheetBackdrop.classList.remove('visible');
  setTimeout(() => {
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
  if (!param?.models?.length) return true;
  if (param.models.includes(model)) return true;
  const aliases = MODEL_PARAM_ALIASES[model] || [];
  if (aliases.some((alias) => param.models.includes(alias))) return true;
  return false;
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
    html += `<div class="chip${S.model===id?' active':''}" data-model="${id}">${m.label}</div>`;
  }
  html += '</div></div>';

  // Run selector
  if (S.modelRuns.length > 0) {
    html += '<div class="sheet-section"><div class="sheet-section-title">Run</div><div class="sheet-chips">';
    for (const r of S.modelRuns) {
      const rid = r.run_id || r;
      const isActive = rid === S.runId;
      const indicator = r.is_complete ? ' \u2713' : (r.completed_fxx && r.completed_fxx.length > 0 ? ' \u25cf' : ' \u25cb');
      html += `<div class="chip${isActive?' active':''}" data-run="${rid}">${formatMobileRunId(rid)}${indicator}</div>`;
    }
    html += '</div></div>';
  }

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
      html += `<div class="sheet-item${S.param===p.id?' active':''}" data-param="${p.id}">${p.label}</div>`;
    }
    html += `</div>
      </div>
    </div>`;
  }
  sheetContent.innerHTML = html;

  sheetContent.querySelectorAll('.chip[data-model]').forEach((el) => {
    el.addEventListener('click', () => {
      S.model = el.dataset.model;
      S.param = firstSupportedModelParam(S.model, S.param);
      ensureActiveModelGroupOpen();
      closeSheet();
      initModels();
    });
  });
  sheetContent.querySelectorAll('.chip[data-run]').forEach((el) => {
    el.addEventListener('click', () => {
      closeSheet();
      loadRun(el.dataset.run);
    });
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
      initModels();
    });
  });
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
  html += `<div class="chip${regional ? '' : ' active'}" data-sat-scope="national">National</div>`;
  html += `<div class="chip${regional ? ' active' : ''}" data-sat-scope="regional">Regional</div>`;
  html += '</div></div>';

  if (regional) {
    html += '<div class="sheet-section"><div class="sheet-section-title">Region</div><div class="sheet-chip-grid">';
    for (const [id, label] of Object.entries(SATELLITE_REGION_NAMES)) {
      html += `<div class="chip chip--grid${S.satRegion===id?' active':''}" data-sat-region="${id}">${label}</div>`;
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
    html += `<div class="chip${S.radarProduct===p.id?' active':''}" data-rprod="${p.id}">${p.label}</div>`;
  }
  html += '</div></div>';

  // Tilt selector
  const tilts = S.radarTilts || [0.5];
  html += '<div class="sheet-section"><div class="sheet-section-title">Elevation Tilt</div><div class="sheet-chips">';
  for (const t of tilts) {
    html += `<div class="chip${S.radarTilt===t?' active':''}" data-tilt="${t}">${t.toFixed(1)}\u00B0</div>`;
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
  return `<div class="sheet-item${active ? ' active' : ''}" ${attrName}="${product.id}"><span>${product.label}</span>${detail}</div>`;
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
