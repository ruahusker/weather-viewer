'use strict';

/** WX Viewer — Leaflet-based Weather Model & Satellite Viewer */

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


// BASE comes from js/core/api.js (exposed on window by js/main.js).

const URL_PARAMS = new URLSearchParams(window.location.search || '');
const CAPTURE_MODE = URL_PARAMS.get('capture') === '1';
const CAPTURE_SOURCE = (URL_PARAMS.get('source') || '').toLowerCase();
const CAPTURE_FRAME = URL_PARAMS.get('frame') || 'latest';
const BACKGROUND_ONLY = URL_PARAMS.get('background_only') === '1';
const DEBUG_SCRUBBER = URL_PARAMS.get('debug_scrubber') === '1';
const MODEL_IMAGE_VERSION = '20261008a';
const SHAREABLE_VIEWS = new Set(['home', 'models', 'satellite', 'radar', 'chase', 'surface']);
const SHAREABLE_MODELS = new Set([
  'ecmwf', 'gdps', 'gefs', 'gfs', 'href', 'hrrr', 'nam12km', 'nam3km', 'rap', 'rrfs', 'rtma',
]);
const SHAREABLE_SATELLITES = new Set(['goes18', 'goes19']);
const SHAREABLE_QUERY_KEYS = [
  'view', 'source', 'model', 'run', 'param', 'fxx', 'satellite', 'sat_product', 'sat_region',
  'site', 'product', 'tilt', 'frame', 'lat', 'lon', 'z',
];

if (CAPTURE_MODE) {
  document.documentElement.dataset.captureMode = '1';
}

// Zoom level at which the home page switches from the composite to individual site images
const HOME_RADAR_ZOOM_THRESHOLD = 6;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
// The shared state object lives in js/core/state.js (window.state via main.js).

// Featured chasers — always shown as pulsing dots on the home map
const FEATURED_CHASERS = ['Steve Blum', 'Chad Alcares', 'Mike Lachendro', 'Daniel Castro'];

let captureReadyTimer = null;
let captureReadySeq = 0;

const SPC_PRODUCT_COMPANIONS = {
  day1_torn: ['day1_torn', 'day1_sig_torn'],
  day1_hail: ['day1_hail', 'day1_sig_hail'],
  day1_wind: ['day1_wind', 'day1_sig_wind'],
  day2_torn: ['day2_torn', 'day2_sig_torn'],
  day2_hail: ['day2_hail', 'day2_sig_hail'],
  day2_wind: ['day2_wind', 'day2_sig_wind'],
  day3_prob: ['day3_prob', 'day3_sig'],
};

const HOME_SPC_VIEW_PRODUCTS = {
  day1: {
    overview: ['day1_cat'],
    tornado: SPC_PRODUCT_COMPANIONS.day1_torn,
    hail: SPC_PRODUCT_COMPANIONS.day1_hail,
    wind: SPC_PRODUCT_COMPANIONS.day1_wind,
  },
  day2: {
    overview: ['day2_cat'],
    tornado: SPC_PRODUCT_COMPANIONS.day2_torn,
    hail: SPC_PRODUCT_COMPANIONS.day2_hail,
    wind: SPC_PRODUCT_COMPANIONS.day2_wind,
  },
  day3: {
    overview: SPC_PRODUCT_COMPANIONS.day3_prob,
  },
};
let scrubberDebugPanel = null;
let scrubberDebugCopyBtn = null;
let scrubberDebugTimer = null;
let scrubberDebugObserver = null;
let scrubberDebugEvents = [];
let scrubberDebugHistoryHooked = false;
let satelliteViewerResizeObserver = null;
let activeSourceReflowTimer = null;
let _mapPointerDownPoint = null;
let _mapPointerDragged = false;
let _soundingRequestSeq = 0;
let _mapLoadingCount = 0;
let _mapLoadingTimer = null;
let _satelliteResumeAfterZoom = false;
let _modelRunsId = 0;
let _modelSelectionId = 0;
let _legendRequestId = 0;
let _chaseRequestId = 0;

function getCaptureConfig() {
  if (!CAPTURE_MODE) return null;
  const source = ['models', 'satellite', 'radar'].includes(CAPTURE_SOURCE) ? CAPTURE_SOURCE : 'models';
  return {
    source,
    frame: CAPTURE_FRAME,
    model: URL_PARAMS.get('model') || 'hrrr',
    runId: URL_PARAMS.get('run') || null,
    parameter: URL_PARAMS.get('param') || 'temp_2m',
    fxx: URL_PARAMS.get('fxx') || null,
    satellite: URL_PARAMS.get('satellite') || 'goes19',
    satProduct: URL_PARAMS.get('sat_product') || 'ir',
    satRegion: URL_PARAMS.get('sat_region') || '',
    radarSite: URL_PARAMS.get('site') || 'KTLX',
    radarProduct: URL_PARAMS.get('product') || 'REF',
    radarTilt: Number.parseFloat(URL_PARAMS.get('tilt') || '0.5'),
  };
}

function getSharedViewConfig() {
  if (CAPTURE_MODE) return null;
  const cleanId = (value) => (/^[a-z0-9_]+$/i.test(value || '') ? value : null);
  const rawView = URL_PARAMS.get('view');
  const view = (SHAREABLE_VIEWS.has(rawView) ? rawView : URL_PARAMS.get('source') || '').toLowerCase();
  const lat = Number.parseFloat(URL_PARAMS.get('lat') || '');
  const lon = Number.parseFloat(URL_PARAMS.get('lon') || '');
  const zoom = Number.parseFloat(URL_PARAMS.get('z') || '');
  const site = (URL_PARAMS.get('site') || '').toUpperCase();
  const product = (URL_PARAMS.get('product') || '').toUpperCase();
  return {
    view: SHAREABLE_VIEWS.has(view) ? view : null,
    model: SHAREABLE_MODELS.has(URL_PARAMS.get('model') || '') ? URL_PARAMS.get('model') : null,
    runId: /^\d{10}$/.test(URL_PARAMS.get('run') || '') ? URL_PARAMS.get('run') : null,
    parameter: cleanId(URL_PARAMS.get('param')),
    fxx: URL_PARAMS.has('fxx') ? URL_PARAMS.get('fxx') : null,
    satellite: SHAREABLE_SATELLITES.has(URL_PARAMS.get('satellite') || '')
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

function applyCaptureConfig(config) {
  if (!config) return;
  state.dataSource = config.source;
  if (config.source === 'models') {
    state.model = config.model;
    state.parameter = config.parameter;
    if (config.runId) state.runId = config.runId;
    if (config.fxx !== null) {
      state.currentFxx = /^\d+$/.test(config.fxx) ? Number.parseInt(config.fxx, 10) : config.fxx;
    }
  } else if (config.source === 'satellite') {
    state.satellite = config.satellite;
    state.satProduct = config.satProduct;
    state.satRegion = config.satRegion || '';
    state.satScope = state.satRegion ? 'regional' : 'national';
    ensureValidSatelliteScopeSelection();
  } else if (config.source === 'radar') {
    state.radarSite = config.radarSite;
    state.radarProduct = config.radarProduct;
    state.radarTilt = Number.isFinite(config.radarTilt) ? config.radarTilt : 0.5;
  }
}

function applySharedViewConfig(config) {
  if (!config) return;
  if (config.view) state.dataSource = config.view;
  if (config.model) state.model = config.model;
  if (config.runId) state.runId = config.runId;
  if (config.parameter) state.parameter = config.parameter;
  if (config.fxx !== null) {
    state.currentFxx = /^\d+$/.test(config.fxx) ? Number.parseInt(config.fxx, 10) : config.fxx;
  }
  if (config.satellite) state.satellite = config.satellite;
  if (config.satProduct) state.satProduct = config.satProduct;
  if (config.satRegion !== null) {
    state.satRegion = config.satRegion;
    state.satScope = config.satRegion ? 'regional' : 'national';
  }
  if (config.radarSite) state.radarSite = config.radarSite;
  if (config.radarProduct) state.radarProduct = config.radarProduct;
  if (Number.isFinite(config.radarTilt)) state.radarTilt = config.radarTilt;
}

function clearCaptureReady() {
  if (!CAPTURE_MODE || !document.body) return;
  document.body.dataset.captureReady = '0';
  if (captureReadyTimer) {
    clearTimeout(captureReadyTimer);
    captureReadyTimer = null;
  }
}

function beginMapLoading(message = 'Loading weather data…') {
  _mapLoadingCount += 1;
  if (mapLoadingText) mapLoadingText.textContent = message;
  if (_mapLoadingTimer) return;
  _mapLoadingTimer = setTimeout(() => {
    _mapLoadingTimer = null;
    if (_mapLoadingCount > 0 && mapLoadingOverlay) {
      mapLoadingOverlay.classList.add('visible');
      mapLoadingOverlay.setAttribute('aria-hidden', 'false');
    }
  }, 700);
}

function endMapLoading() {
  _mapLoadingCount = Math.max(0, _mapLoadingCount - 1);
  if (_mapLoadingCount > 0) return;
  if (_mapLoadingTimer) {
    clearTimeout(_mapLoadingTimer);
    _mapLoadingTimer = null;
  }
  if (mapLoadingOverlay) {
    mapLoadingOverlay.classList.remove('visible');
    mapLoadingOverlay.setAttribute('aria-hidden', 'true');
  }
}

function markCaptureReady(reason = '') {
  if (!CAPTURE_MODE || !document.body) return;
  const seq = ++captureReadySeq;
  if (captureReadyTimer) clearTimeout(captureReadyTimer);
  captureReadyTimer = setTimeout(() => {
    if (seq !== captureReadySeq) return;
    if (leafletMap) {
      leafletMap.invalidateSize(false);
    }
    document.body.dataset.captureReady = '1';
    document.body.dataset.captureReason = reason;
  }, 900);
}

const SHARED_VIEW_CONFIG = getSharedViewConfig();
let sharedModelSelectionPending = Boolean(
  SHARED_VIEW_CONFIG?.view === 'models'
  && (SHARED_VIEW_CONFIG.runId || SHARED_VIEW_CONFIG.fxx !== null)
);
let sharedChaseSelectionPending = Boolean(SHARED_VIEW_CONFIG?.view === 'chase');
let sharedFramePending = SHARED_VIEW_CONFIG?.frame || null;
let sharedMapViewPending = SHARED_VIEW_CONFIG?.mapView || null;

if (CAPTURE_MODE) applyCaptureConfig(getCaptureConfig());
else applySharedViewConfig(SHARED_VIEW_CONFIG);

function getStoredRadarSite() {
  if (CAPTURE_MODE) return null;
  if (URL_PARAMS.get('site')) return null;
  try {
    return localStorage.getItem('wx_viewer_radar_site');
  } catch {
    return null;
  }
}

function persistRadarSite(site) {
  if (CAPTURE_MODE || !site) return;
  try {
    localStorage.setItem('wx_viewer_radar_site', site);
  } catch {}
}

function getStoredHomeLayerPanelCollapsed() {
  if (CAPTURE_MODE) return false;
  try {
    return localStorage.getItem('wx_home_layer_panel_collapsed') === '1';
  } catch {
    return false;
  }
}

function persistHomeLayerPanelCollapsed(collapsed) {
  if (CAPTURE_MODE) return;
  try {
    localStorage.setItem('wx_home_layer_panel_collapsed', collapsed ? '1' : '0');
  } catch {}
}

function getStoredHomeSidePanelCollapsed() {
  if (CAPTURE_MODE) return false;
  try {
    return localStorage.getItem('wx_home_side_panel_collapsed') === '1';
  } catch {
    return false;
  }
}

function persistHomeSidePanelCollapsed(collapsed) {
  if (CAPTURE_MODE) return;
  try {
    localStorage.setItem('wx_home_side_panel_collapsed', collapsed ? '1' : '0');
  } catch {}
}

const storedRadarSite = getStoredRadarSite();
if (storedRadarSite) {
  state.radarSite = storedRadarSite;
}
state.homeLayerPanelCollapsed = getStoredHomeLayerPanelCollapsed();
state.homeSidePanelCollapsed = getStoredHomeSidePanelCollapsed();


let leafletMap = null;
let baseTileLayer = null;
let labelTileLayer = null;
let usaBounds = null;
let usaViewportTimer = null;
let defaultMinZoom = 0;
let boundaryRenderer = null;
let alertsRenderer = null;
let spcRenderer = null;
let spotterRenderer = null;
const MODEL_CITY_ZOOM_THRESHOLD = 6.75;
const MODEL_ROADS_ZOOM_THRESHOLD = 5.5;
const MODEL_COUNTY_ZOOM_THRESHOLD = 6.5;
const REFERENCE_COUNTY_ZOOM_THRESHOLD = 7.0;
const REFERENCE_COUNTRY_COAST_ZOOM_THRESHOLD = 3.0;

const SATELLITE_REFERENCE_CITIES = [
  ['Seattle', 47.6062, -122.3321],
  ['Portland', 45.5152, -122.6784],
  ['San Francisco', 37.7749, -122.4194],
  ['Los Angeles', 34.0522, -118.2437],
  ['San Diego', 32.7157, -117.1611],
  ['Las Vegas', 36.1699, -115.1398],
  ['Phoenix', 33.4484, -112.0740],
  ['Boise', 43.6150, -116.2023],
  ['Salt Lake City', 40.7608, -111.8910],
  ['Denver', 39.7392, -104.9903],
  ['Albuquerque', 35.0844, -106.6504],
  ['El Paso', 31.7619, -106.4850],
  ['Cheyenne', 41.1400, -104.8202],
  ['Rapid City', 44.0805, -103.2310],
  ['Bismarck', 46.8083, -100.7837],
  ['Helena', 46.5884, -112.0245],
  ['Omaha', 41.2565, -95.9345],
  ['Lincoln', 40.8136, -96.7026],
  ['Kansas City', 39.0997, -94.5786],
  ['Oklahoma City', 35.4676, -97.5164],
  ['Dallas', 32.7767, -96.7970],
  ['Houston', 29.7604, -95.3698],
  ['San Antonio', 29.4241, -98.4936],
  ['New Orleans', 29.9511, -90.0715],
  ['Minneapolis', 44.9778, -93.2650],
  ['Chicago', 41.8781, -87.6298],
  ['St. Louis', 38.6270, -90.1994],
  ['Memphis', 35.1495, -90.0490],
  ['Nashville', 36.1627, -86.7816],
  ['Atlanta', 33.7490, -84.3880],
  ['Birmingham', 33.5186, -86.8104],
  ['Miami', 25.7617, -80.1918],
  ['Tampa', 27.9506, -82.4572],
  ['Naples', 26.1420, -81.7948],
  ['Orlando', 28.5383, -81.3792],
  ['Jacksonville', 30.3322, -81.6557],
  ['Charlotte', 35.2271, -80.8431],
  ['Raleigh', 35.7796, -78.6382],
  ['Washington', 38.9072, -77.0369],
  ['New York', 40.7128, -74.0060],
  ['Philadelphia', 39.9526, -75.1652],
  ['Detroit', 42.3314, -83.0458],
  ['Cleveland', 41.4993, -81.6944],
  ['Pittsburgh', 40.4406, -79.9959],
  ['Buffalo', 42.8864, -78.8784],
  ['Boston', 42.3601, -71.0589],
  ['Manchester', 42.9956, -71.4548],
  ['Bangor', 44.8012, -68.7778],
];

const SATELLITE_REGIONAL_REFERENCE_CITIES = {
  central_plains: [
    ['Grand Junction', 39.0639, -108.5506],
    ['Fort Collins', 40.5853, -105.0844],
    ['Greeley', 40.4233, -104.7091],
    ['Boulder', 40.0150, -105.2705],
    ['Colorado Springs', 38.8339, -104.8214],
    ['Pueblo', 38.2544, -104.6091],
    ['Lamar', 38.0872, -102.6207],
    ['Sterling', 40.6255, -103.2077],
    ['Pierre', 44.3683, -100.3510],
    ['Brookings', 44.3114, -96.7984],
    ['Mitchell', 43.7094, -98.0298],
    ['Watertown', 44.8994, -97.1151],
    ['Scottsbluff', 41.8666, -103.6672],
    ['North Platte', 41.1403, -100.7601],
    ['Kearney', 40.6995, -99.0815],
    ['Grand Island', 40.9264, -98.3420],
    ['Norfolk', 41.9570, -97.4351],
    ['Columbus', 41.4303, -97.3684],
    ['Wichita', 37.6872, -97.3301],
    ['Topeka', 39.0473, -95.6752],
    ['Salina', 38.8403, -97.6114],
    ['Hays', 38.8792, -99.3268],
    ['Garden City', 37.9717, -100.8727],
    ['Dodge City', 37.7528, -100.0171],
    ['Colby', 39.3958, -101.0524],
    ['Springfield', 37.2089, -93.2923],
    ['Columbia', 38.9517, -92.3341],
    ['Jefferson City', 38.5767, -92.1735],
    ['Kirksville', 40.1948, -92.5833],
    ['Des Moines', 41.5868, -93.6250],
    ['Sioux City', 42.4997, -96.4003],
    ['Council Bluffs', 41.2619, -95.8608],
    ['Cedar Rapids', 41.9779, -91.6656],
    ['Davenport', 41.5236, -90.5776],
    ['Mason City', 43.1536, -93.2010],
  ],
  northern_plains: [
    ['Billings', 45.7833, -108.5007],
    ['Miles City', 46.4083, -105.8406],
    ['Glasgow', 48.1958, -106.6367],
    ['Sidney', 47.7164, -104.1563],
    ['Dickinson', 46.8792, -102.7896],
    ['Williston', 48.1469, -103.6179],
    ['Minot', 48.2325, -101.2963],
    ['Bismarck', 46.8083, -100.7837],
    ['Jamestown', 46.9105, -98.7084],
    ['Fargo', 46.8772, -96.7898],
    ['Grand Forks', 47.9253, -97.0329],
    ['Aberdeen', 45.4647, -98.4865],
    ['Pierre', 44.3683, -100.3510],
    ['Watertown', 44.8994, -97.1151],
    ['Brookings', 44.3114, -96.7984],
    ['Rapid City', 44.0805, -103.2310],
    ['Spearfish', 44.4908, -103.8594],
    ['Deadwood', 44.3767, -103.7296],
  ],
  upper_midwest: [
    ['Duluth', 46.7867, -92.1005],
    ['Rochester', 44.0121, -92.4802],
    ['St Cloud', 45.5579, -94.1632],
    ['Mankato', 44.1636, -93.9994],
    ['La Crosse', 43.8014, -91.2396],
    ['Eau Claire', 44.8113, -91.4985],
    ['Madison', 43.0731, -89.4012],
    ['Green Bay', 44.5133, -88.0133],
    ['Milwaukee', 43.0389, -87.9065],
    ['Rockford', 42.2711, -89.0937],
    ['Peoria', 40.6936, -89.5890],
    ['Cedar Rapids', 41.9779, -91.6656],
    ['Dubuque', 42.5006, -90.6646],
    ['Davenport', 41.5236, -90.5776],
    ['Des Moines', 41.5868, -93.6250],
    ['Waterloo', 42.4928, -92.3426],
    ['Sioux Falls', 43.5446, -96.7311],
    ['South Bend', 41.6764, -86.2520],
  ],
  southern_plains: [
    ['Amarillo', 35.2220, -101.8313],
    ['Lubbock', 33.5779, -101.8552],
    ['Midland', 31.9974, -102.0779],
    ['Odessa', 31.8457, -102.3676],
    ['San Angelo', 31.4638, -100.4370],
    ['Abilene', 32.4487, -99.7331],
    ['Wichita Falls', 33.9137, -98.4934],
    ['Lawton', 34.6036, -98.3959],
    ['Norman', 35.2226, -97.4395],
    ['Stillwater', 36.1156, -97.0584],
    ['Tulsa', 36.1540, -95.9928],
    ['Enid', 36.3956, -97.8784],
    ['Ponca City', 36.7069, -97.0856],
    ['Wichita', 37.6872, -97.3301],
    ['Hutchinson', 38.0608, -97.9298],
    ['Dodge City', 37.7528, -100.0171],
    ['Garden City', 37.9717, -100.8727],
    ['Sherman', 33.6357, -96.6089],
  ],
  southeast: [
    ['Birmingham', 33.5186, -86.8104],
    ['Montgomery', 32.3792, -86.3077],
    ['Huntsville', 34.7304, -86.5861],
    ['Mobile', 30.6954, -88.0399],
    ['Pensacola', 30.4213, -87.2169],
    ['Tallahassee', 30.4383, -84.2807],
    ['Jacksonville', 30.3322, -81.6557],
    ['Orlando', 28.5383, -81.3792],
    ['Tampa', 27.9506, -82.4572],
    ['Fort Myers', 26.6406, -81.8723],
    ['Savannah', 32.0809, -81.0912],
    ['Macon', 32.8407, -83.6324],
    ['Columbus', 32.4609, -84.9877],
    ['Atlanta', 33.7490, -84.3880],
    ['Augusta', 33.4735, -82.0105],
    ['Greenville', 34.8526, -82.3940],
    ['Columbia', 34.0007, -81.0348],
    ['Charleston', 32.7765, -79.9311],
  ],
  northeast: [
    ['Buffalo', 42.8864, -78.8784],
    ['Rochester', 43.1566, -77.6088],
    ['Syracuse', 43.0481, -76.1474],
    ['Albany', 42.6526, -73.7562],
    ['Scranton', 41.4089, -75.6624],
    ['Allentown', 40.6023, -75.4714],
    ['Harrisburg', 40.2732, -76.8867],
    ['Philadelphia', 39.9526, -75.1652],
    ['Newark', 40.7357, -74.1724],
    ['New York', 40.7128, -74.0060],
    ['Hartford', 41.7658, -72.6734],
    ['New Haven', 41.3083, -72.9279],
    ['Providence', 41.8240, -71.4128],
    ['Boston', 42.3601, -71.0589],
    ['Portland', 43.6591, -70.2568],
    ['Manchester', 42.9956, -71.4548],
    ['Burlington', 44.4759, -73.2121],
    ['Pittsburgh', 40.4406, -79.9959],
  ],
  southwest: [
    ['Las Vegas', 36.1699, -115.1398],
    ['Kingman', 35.1894, -114.0530],
    ['Flagstaff', 35.1983, -111.6513],
    ['Prescott', 34.5400, -112.4685],
    ['Phoenix', 33.4484, -112.0740],
    ['Yuma', 32.6927, -114.6277],
    ['Tucson', 32.2226, -110.9747],
    ['Roswell', 33.3943, -104.5230],
    ['Albuquerque', 35.0844, -106.6504],
    ['Santa Fe', 35.6870, -105.9378],
    ['Farmington', 36.7281, -108.2187],
    ['Durango', 37.2753, -107.8801],
    ['Grand Junction', 39.0639, -108.5506],
    ['St George', 37.0965, -113.5684],
    ['Page', 36.9147, -111.4558],
    ['El Paso', 31.7619, -106.4850],
    ['Las Cruces', 32.3199, -106.7637],
    ['Safford', 32.8339, -109.7076],
  ],
  west_coast: [
    ['Bellingham', 48.7519, -122.4787],
    ['Seattle', 47.6062, -122.3321],
    ['Tacoma', 47.2529, -122.4443],
    ['Olympia', 47.0379, -122.9007],
    ['Yakima', 46.6021, -120.5059],
    ['Spokane', 47.6588, -117.4260],
    ['Portland', 45.5152, -122.6784],
    ['Salem', 44.9429, -123.0351],
    ['Eugene', 44.0521, -123.0868],
    ['Medford', 42.3265, -122.8756],
    ['Redding', 40.5865, -122.3917],
    ['Sacramento', 38.5816, -121.4944],
    ['Santa Rosa', 38.4405, -122.7144],
    ['San Francisco', 37.7749, -122.4194],
    ['Fresno', 36.7378, -119.7871],
    ['Bakersfield', 35.3733, -119.0187],
    ['Los Angeles', 34.0522, -118.2437],
    ['San Diego', 32.7157, -117.1611],
  ],
};

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

const MODEL_REFERENCE_CITIES = (() => {
  const seen = new Set();
  const merged = [];
  const addCity = ([name, lat, lon]) => {
    const key = `${name}|${lat}|${lon}`;
    if (seen.has(key)) return;
    seen.add(key);
    merged.push([name, lat, lon]);
  };
  SATELLITE_REFERENCE_CITIES.forEach(addCity);
  Object.values(SATELLITE_REGIONAL_REFERENCE_CITIES).forEach((cities) => cities.forEach(addCity));
  return merged;
})();

const MODEL_PRESENTATION_BOUNDS = [[15.0, -145.0], [58.0, -50.0]];
// Outline (lat, lon) of the area where HRRR, NAM 3km and RRFS all have data
// (the smallest domains), traced from rendered PNG alpha on 2026-10-17 with a
// small inward margin. The default model view keeps the whole window inside it.
const MODEL_DATA_OUTLINE = [
  [47.5, -132.85], [47.89, -131.26], [48.31, -129.68], [48.66, -128.09], [49.05, -126.51], [49.36, -124.92],
  [49.67, -123.34], [49.98, -121.75], [50.21, -120.17], [50.48, -118.58], [50.72, -117.0], [50.92, -115.41],
  [51.12, -113.83], [51.28, -112.24], [51.42, -110.66], [51.58, -109.07], [51.68, -107.49], [51.78, -105.9],
  [51.88, -104.32], [51.94, -102.73], [51.97, -101.15], [52.01, -99.56], [52.04, -97.98], [52.04, -96.39],
  [52.01, -94.81], [51.97, -93.22], [51.91, -91.64], [51.84, -90.05], [51.74, -88.46], [51.65, -86.88],
  [51.51, -85.29], [51.38, -83.71], [51.22, -82.12], [51.02, -80.54], [50.82, -78.95], [50.62, -77.37],
  [50.38, -75.78], [50.11, -74.2], [49.84, -72.61], [49.57, -71.03], [49.22, -69.44], [48.91, -67.86],
  [48.52, -66.27], [48.14, -64.69], [47.75, -63.1], [47.46, -62.15], [47.36, -62.15], [45.75, -63.1],
  [42.88, -64.69], [39.66, -66.27], [36.11, -67.86], [32.22, -69.44], [27.82, -71.03], [23.0, -72.61],
  [22.61, -74.2], [22.95, -75.78], [23.29, -77.37], [23.58, -78.95], [23.87, -80.54], [24.11, -82.12],
  [24.35, -83.71], [24.55, -85.29], [24.74, -86.88], [24.88, -88.46], [25.03, -90.05], [25.12, -91.64],
  [25.17, -93.22], [25.22, -94.81], [25.26, -96.39], [25.26, -97.98], [25.26, -99.56], [25.22, -101.15],
  [25.17, -102.73], [25.07, -104.32], [24.93, -105.9], [24.79, -107.49], [24.64, -109.07], [24.45, -110.66],
  [24.21, -112.24], [23.97, -113.83], [23.72, -115.41], [23.43, -117.0], [23.09, -118.58], [22.75, -120.17],
  [22.36, -121.75], [25.93, -123.34], [30.45, -124.92], [34.56, -126.51], [38.26, -128.09], [41.59, -129.68],
  [44.6, -131.26], [47.36, -132.85],
];
// Where the clear part of the map is centred on open (central US).
const MODEL_DEFAULT_FOCUS = [38.8, -96.5];
const MODEL_SPARSE_CANVAS_PARAMS = new Set([
  'cape', 'mlcape', 'mucape', 'cape_03km',
  'cape_ml', 'cape_surface', 'cape_probability', 'cape_probability_1000',
  'cin', 'mlcin', 'mucin', 'cin_ml', 'cin_surface', 'lifted_index',
  'stp', 'scp', 'lcl', 'llmt', 'ascent', 'hp_risk',
  'srh_01km', 'srh_03km', 'shear_01km', 'shear_06km',
  'refl_probability_40dbz', 'uh_25km_probability',
  'precip_probability', 'precip_probability_6h_05in',
  'precip_probability_6h_1in', 'pwat_probability_1in',
  'snowfall_probability_6h_1in', 'visibility_probability',
  'cloud_cover', 'cloud_cover_low', 'cloud_cover_mid', 'cloud_cover_high',
  'uh_02km', 'uh_03km', 'uh_25km', 'hail', 'echo_tops', 'lightning',
]);

const SATELLITE_REGIONAL_PRODUCT_SET = new Set(['vis_1km', 'ir_native', 'ir_14', 'veggie', 'cloud_particle']);
const SATELLITE_REGION_SUPPORTED_PRODUCTS = Object.fromEntries(
  Object.keys(SATELLITE_REGION_NAMES).map((region) => [region, SATELLITE_REGIONAL_PRODUCT_SET])
);

const SATELLITE_SCOPE_DEFAULTS = {
  national: { product: 'ir', region: '' },
  regional: { product: 'vis_1km', region: 'central_plains' },
};

// Regional satellite products — one generated button list; the region is
// applied to the buttons' data-sat-region attribute by syncSatelliteRegionButtons().
const SATELLITE_REGIONAL_PRODUCTS = [
  ['vis_1km', 'Visible 1km — Band 2 (0.64µm)'],
  ['ir_native', 'Clean IR Native — Band 13 (10.3µm)'],
  ['ir_14', 'Longwave IR — Band 14 (11.2µm)'],
  ['veggie', 'Veggie — Band 3 (0.86µm)'],
  ['cloud_particle', 'Cloud Particle — Band 6 (2.2µm)'],
];

function buildSatelliteRegionalProducts() {
  const container = document.getElementById('satRegionalProducts');
  if (!container) return;
  container.innerHTML = SATELLITE_REGIONAL_PRODUCTS.map(([product, label]) =>
    `<button class="param-btn" data-sat-product="${product}" data-sat-region="">` +
    `<span class="param-icon"></span>${label}</button>`,
  ).join('');
}

function syncSatelliteRegionButtons() {
  const region = state.satRegion || SATELLITE_SCOPE_DEFAULTS.regional.region;
  document.querySelectorAll('#satRegionalProducts .param-btn').forEach((btn) => {
    btn.dataset.satRegion = region;
  });
  const sel = document.getElementById('satRegionSelect');
  if (sel && sel.value !== region) sel.value = region;
}

// Server Status cards — generated so markup stays in one place. Ids/classes
// must match what updateMiniModelStatus() queries (mini-{fill,badge,text}-<name>).
const MODEL_STATUS_CARDS = [
  ['ecmwf', 'ECMWF', 'ecmwf'],
  ['gdps', 'GDPS', 'gdps'],
  ['gfs', 'GFS', 'gfs'],
  ['hrrr', 'HRRR', 'hrrr'],
  ['href', 'HREF', 'href'],
  ['nam12km', 'NAM 12km', 'nam'],
  ['nam3km', 'NAM 3km', 'nam3km'],
  ['rap', 'RAP', 'rap'],
  ['rrfs', 'RRFS', 'rrfs'],
  ['rtma', 'RTMA', 'rtma'],
  ['gefs', 'GEFS', 'gefs'],
];

function buildModelStatusCards() {
  const container = document.getElementById('modelStatusCompact');
  if (!container) return;
  container.innerHTML = MODEL_STATUS_CARDS.map(([key, label, color]) =>
    `<div class="model-compact" id="mini-${key}">` +
    `<div class="model-compact-header">` +
    `<span class="model-compact-name ${color}">${label}</span>` +
    `<span class="model-compact-badge" id="mini-badge-${key}">IDLE</span>` +
    `</div>` +
    `<div class="model-compact-bar"><div class="model-compact-fill ${color}" id="mini-fill-${key}" style="width:0%"></div></div>` +
    `<div class="model-compact-text" id="mini-text-${key}">--</div>` +
    `</div>`,
  ).join('');
}

// Models sidebar parameter filter — hides non-matching .param-btn buttons by
// label text and collapses groups with no visible matches.
function initParamFilter() {
  const input = document.getElementById('paramFilter');
  const sidebar = document.getElementById('modelSidebar');
  if (!input || !sidebar) return;
  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    sidebar.querySelectorAll('.param-btn[data-param]').forEach((btn) => {
      btn.classList.toggle('param-filter-hidden', !!q && !btn.textContent.toLowerCase().includes(q));
    });
    sidebar.querySelectorAll('.param-group').forEach((group) => {
      const buttons = [...group.querySelectorAll('.param-btn[data-param]')];
      if (!buttons.length) return;
      const anyVisible = buttons.some((b) =>
        !b.classList.contains('param-filter-hidden') && b.style.display !== 'none');
      group.classList.toggle('param-filter-empty', !!q && !anyVisible);
      const toggle = group.querySelector(':scope > .param-group-toggle');
      if (q && anyVisible && !group.classList.contains('expanded')) {
        group.classList.add('expanded');
        group.dataset.filterExpanded = '1';
        toggle?.setAttribute('aria-expanded', 'true');
      } else if (!q && group.dataset.filterExpanded) {
        group.classList.remove('expanded');
        delete group.dataset.filterExpanded;
        toggle?.setAttribute('aria-expanded', 'false');
      }
    });
  });
}

/** Keep pinned products visible without duplicating the parameter catalog. */
function renderModelFavorites() {
  const list = document.getElementById('favoriteProducts');
  const toggle = document.getElementById('favoriteProductToggle');
  const note = document.getElementById('favoriteProductsNote');
  if (!list || !toggle || !note || !window.ViewerUI) return;
  const favorites = window.ViewerUI.favorites;
  const selected = document.querySelector(`.param-btn[data-param="${state.parameter}"]`);
  const selectedName = selected?.textContent.trim() || state.parameter;
  toggle.textContent = favorites.has(state.parameter) ? '★ Remove selected' : '☆ Save selected';
  toggle.setAttribute('aria-pressed', String(favorites.has(state.parameter)));
  toggle.setAttribute('aria-label', `${favorites.has(state.parameter) ? 'Remove' : 'Save'} ${selectedName} ${favorites.has(state.parameter) ? 'from' : 'to'} favorites`);
  toggle.title = selectedName;
  toggle.disabled = !selected;
  list.replaceChildren();
  let visibleCount = 0;
  const saved = favorites.list();
  const availabilityReady = Object.keys(state.availableByParam || {}).length > 0;
  for (const parameter of saved) {
    const original = document.querySelector(`.param-btn[data-param="${parameter}"]`);
    if (!original || !original.dataset.models?.split(',').includes(state.model)) continue;
    if (availabilityReady && !state.availableByParam[parameter]?.length) continue;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'favorite-product';
    button.dataset.favoriteParam = parameter;
    button.textContent = original.textContent.trim();
    button.classList.toggle('active', parameter === state.parameter);
    button.setAttribute('aria-pressed', String(parameter === state.parameter));
    list.appendChild(button);
    visibleCount++;
  }
  note.hidden = visibleCount > 0;
  note.textContent = saved.length ? 'Saved favorites are unavailable for this model/run.' : 'Select a product, then save it here.';
}

function handleFavoriteProductClick(event) {
  const button = event.target.closest('[data-favorite-param]');
  if (!button) return;
  document.querySelector(`.param-btn[data-param="${button.dataset.favoriteParam}"]`)?.click();
}

function toggleSelectedFavorite() {
  window.ViewerUI.favorites.toggle(state.parameter);
}

function initModelFavorites() {
  document.getElementById('favoriteProductToggle')?.addEventListener('click', toggleSelectedFavorite);
  document.getElementById('favoriteProducts')?.addEventListener('click', handleFavoriteProductClick);
  window.ViewerUI.favorites.subscribe(renderModelFavorites);
  renderModelFavorites();
}

// Plain-English tooltips for cryptic parameter buttons (applied at init).
const PARAM_TOOLTIPS = {
  pwat: 'Precipitable water — total moisture in a column of air; high values signal heavy-rain potential',
  mlcape: 'Mixed-layer CAPE — storm fuel (instability) averaged over the lowest ~1 km of the atmosphere',
  mlcin: 'Mixed-layer CIN — the warm-air "cap" that storms must break through to form',
  cape_ml: 'Mixed-layer CAPE — storm fuel (instability) averaged over the lowest ~1 km of the atmosphere',
  cin_ml: 'Mixed-layer CIN — the warm-air "cap" that storms must break through to form',
  mucape: 'Most-unstable CAPE — storm fuel of the single most buoyant air parcel near the ground',
  mucin: 'Most-unstable CIN — inhibition for the most buoyant parcel; small values mean an easy cap break',
  cape: 'Surface-based CAPE — storm fuel (instability) for air rising from the surface',
  cape_surface: 'Surface-based CAPE — storm fuel (instability) for air rising from the surface',
  cin: 'Surface-based CIN — the "cap" that inhibits surface-based storms',
  cin_surface: 'Surface-based CIN — the "cap" that inhibits surface-based storms',
  stp: 'Significant tornado parameter — composite of instability, shear, and spin; values above 1 favor strong tornadoes',
  srh_01km: 'Storm-relative helicity, 0–1 km — low-level spin available to storms; key for tornado potential',
  srh_03km: 'Storm-relative helicity, 0–3 km — low-level spin available to storms over a deeper layer',
  vil: 'Vertically integrated liquid — radar estimate of total liquid water aloft; spikes can indicate hail',
  uh_25km: 'Updraft helicity, 2–5 km — rotating-updraft (mesocyclone) tracks; highlights supercell paths',
  uh_02km: 'Updraft helicity, 0–2 km — lowest-level rotation tracks; tornado-scale spin',
  uh_03km: 'Updraft helicity, 0–3 km — low-to-mid-level rotating-updraft tracks',
  echo_tops: 'Echo tops — height of the storm top; taller storms are usually stronger',
  lifted_index: 'Lifted index — surface-parcel stability; negative values mean unstable air',
  lcl: 'Lifted condensation level — approximate cloud-base height; low values favor tornadoes',
  kuchera_6hr: '6-hour snowfall using the Kuchera ratio — adjusts totals for colder, fluffier snow',
  kuchera_12hr: '12-hour snowfall using the Kuchera ratio — adjusts totals for colder, fluffier snow',
};

const RADAR_PRODUCT_TOOLTIPS = {
  VIL: 'Vertically integrated liquid — radar estimate of total liquid water aloft; spikes can indicate hail',
};

function applyParamTooltips() {
  document.querySelectorAll('.param-btn[data-param]').forEach((btn) => {
    const tip = PARAM_TOOLTIPS[btn.dataset.param];
    if (tip && !btn.title) btn.title = tip;
  });
  document.querySelectorAll('.param-btn[data-radar-product]').forEach((btn) => {
    const tip = RADAR_PRODUCT_TOOLTIPS[btn.dataset.radarProduct];
    if (tip && !btn.title) btn.title = tip;
  });
}

// Sync aria-pressed with the .active class on toggle buttons (param/speed).
function syncAriaPressed() {
  document.querySelectorAll('.param-btn, .speed-btn').forEach((btn) => {
    btn.setAttribute('aria-pressed', btn.classList.contains('active') ? 'true' : 'false');
  });
}

function updateSatelliteScopeButtons() {
  document.querySelectorAll('[data-sat-scope]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.satScope === state.satScope);
  });
}

function updateSatelliteScopeVisibility() {
  document.querySelectorAll('[data-sat-scope-group]').forEach((group) => {
    group.classList.toggle('hidden', group.dataset.satScopeGroup !== state.satScope);
  });
}

function updateSatelliteProductButtons() {
  document.querySelectorAll('.param-btn[data-sat-product]').forEach((btn) => {
    btn.classList.toggle(
      'active',
      btn.dataset.satProduct === state.satProduct && (btn.dataset.satRegion || '') === state.satRegion,
    );
  });
  syncAriaPressed();
}

function ensureValidSatelliteRegionProduct() {
  if (!state.satRegion) return;
  const supported = SATELLITE_REGION_SUPPORTED_PRODUCTS[state.satRegion];
  if (!supported || supported.has(state.satProduct)) return;
  state.satProduct = supported.values().next().value || 'vis_1km';
}

function syncSatelliteSourceForRegion() {
  if (state.satRegion) {
    state.satellite = SATELLITE_REGION_SOURCES[state.satRegion] || 'goes19';
  } else if (state.satScope === 'national') {
    state.satellite = 'goes19';
  }
  const satSelect = document.getElementById('satelliteSelect');
  if (satSelect) {
    satSelect.value = state.satellite;
  }
}

function ensureValidSatelliteScopeSelection() {
  if (state.satScope === 'regional') {
    if (!state.satRegion) {
      state.satRegion = SATELLITE_SCOPE_DEFAULTS.regional.region;
    }
    syncSatelliteSourceForRegion();
    ensureValidSatelliteRegionProduct();
    return;
  }
  state.satRegion = '';
  syncSatelliteSourceForRegion();
}

function setSatelliteScope(scope, preserveSelection = true) {
  state.satScope = scope === 'regional' ? 'regional' : 'national';
  if (state.satScope === 'regional') {
    if (!state.satRegion) {
      state.satRegion = SATELLITE_SCOPE_DEFAULTS.regional.region;
    }
    const supported = SATELLITE_REGION_SUPPORTED_PRODUCTS[state.satRegion];
    if (!preserveSelection || !supported || !supported.has(state.satProduct)) {
      state.satProduct = SATELLITE_SCOPE_DEFAULTS.regional.product;
    }
  } else {
    state.satRegion = '';
    if (!preserveSelection && !state.satProduct) {
      state.satProduct = SATELLITE_SCOPE_DEFAULTS.national.product;
    }
  }
  ensureValidSatelliteScopeSelection();
  syncSatelliteRegionButtons();
  updateSatelliteScopeButtons();
  updateSatelliteScopeVisibility();
  updateSatelliteProductButtons();
}

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

// Source tabs
const tabHome = $('tabHome');
const tabModels = $('tabModels');
const tabSatellite = $('tabSatellite');
const tabRadar = $('tabRadar');
const tabChase = $('tabChase');
const tabSurface = $('tabSurface');

// Controls
const satelliteControls = $('satelliteControls');
const radarControls = $('radarControls');

// Sidebars
const modelSidebar = $('modelSidebar');
const satelliteSidebar = $('satelliteSidebar');
const radarSidebar = $('radarSidebar');
const surfaceSidebar = $('surfaceSidebar');
const chaseSidebar = $('chaseSidebar');
const scrubberBar = $('scrubberBar');
const mapSection = document.querySelector('.map-section');
const homeLayerPanel = $('homeLayerPanel');
const homeLayerPanelToggle = $('homeLayerPanelToggle');
const homeSidePanel = $('homeSidePanel');
const homeSidePanelToggle = $('homeSidePanelToggle');
const watchWarningToggle = $('watchWarningToggle');
const mcdToggle = $('mcdToggle');
const spotterToggle = $('spotterToggle');
const surfaceRadarToggle = $('surfaceRadarToggle');
const spotterFilterSelect = $('spotterFilterSelect');
const satelliteImageViewer = $('satelliteImageViewer');
const satelliteImageViewerImg = $('satelliteImageViewerImg');
const satelliteImageViewerOverlay = $('satelliteImageViewerOverlay');

let _viewerStateGeoJSON = null;
let _viewerCountryGeoJSON = null;
let _viewerCoastGeoJSON = null;
let _viewerLakeGeoJSON = null;
let _viewerOverlayCache = { key: null, svg: null };

// Model controls
const modelSelect = $('modelSelect');
const runSelect = $('runSelect');
const scrubber = $('scrubber');
const scrubberTicks = $('scrubberTicks');
const playBtn = $('playBtn');
const validTimeDisplay = $('validTimeDisplay');

// ── Image preload cache ──────────────────────────────────────────────────────
// Tracks load state per URL: 'pending' | 'ok' | 'error'
const _imgCache = new Map();
const _loadQueue = [];
let _loadActive = 0;
const PRELOAD_CONCURRENCY = 4;
let _productUrls = [];   // URLs belonging to the current product

function _pumpQueue() {
  if (document.hidden) return;
  while (_loadActive < PRELOAD_CONCURRENCY && _loadQueue.length) {
    const url = _loadQueue.shift();
    const s = _imgCache.get(url);
    if (s === 'ok' || s === 'error') continue;
    _loadActive++;
    if (window.PressureContours?.isUrl(url)) {
      window.PressureContours.load(url).then(
        () => { _imgCache.set(url, 'ok'); },
        () => { _imgCache.set(url, 'error'); },
      ).finally(() => { _loadActive--; _pumpQueue(); updateLoadStatus(); });
      continue;
    }
    const img = new Image();
    img.onload  = () => { _imgCache.set(url, 'ok');    _loadActive--; _pumpQueue(); updateLoadStatus(); };
    img.onerror = () => { _imgCache.set(url, 'error'); _loadActive--; _pumpQueue(); updateLoadStatus(); };
    img.src = url;
  }
}

// Queue URLs for background preloading. front=true loads them first.
function cacheFrames(urls, front = false) {
  const pending = urls.filter(u => u && !_imgCache.has(u));
  for (const u of pending) _imgCache.set(u, 'pending');
  if (front) _loadQueue.unshift(...pending);
  else _loadQueue.push(...pending);
  _pumpQueue();
}

function isCached(url) { return url ? _imgCache.get(url) === 'ok' : false; }
function isErrored(url) { return url ? _imgCache.get(url) === 'error' : false; }

// Clear pending queue when switching products (already-loaded images stay in browser cache)
function clearLoadQueue() {
  // Canceled entries must be eligible for loading when this product is revisited.
  for (const url of _loadQueue) _imgCache.delete(url);
  _loadQueue.length = 0;
  _productUrls = [];
}

/** Current frame index and total frame count for the active source. */
function currentFramePosition() {
  if (state.dataSource === 'models') {
    const idx = state.availableFxx.indexOf(state.currentFxx);
    return { idx, count: state.availableFxx.length };
  }
  if (state.dataSource === 'satellite') return { idx: state.satFrameIdx, count: state.satFrames.length };
  if (state.dataSource === 'radar') return { idx: state.radarFrameIdx, count: state.radarFrames.length };
  if (state.dataSource === 'home') return { idx: state.homeRadarFrameIdx, count: state.homeRadarFrames.length };
  return { idx: -1, count: 0 };
}

// Frame counter shows the position in the whole loop ("37/72"). The preload
// window around the current frame only adds a "loading" state, never its own
// count, so the total always reflects every frame in the loop.
function updateLoadStatus() {
  const fc = document.getElementById('frameCounter');
  if (!fc) return;
  const { idx, count } = currentFramePosition();
  fc.textContent = count > 0 && idx >= 0 ? `${idx + 1}/${count}` : '--';
  const loading = _productUrls.some((u) => _imgCache.get(u) === 'pending');
  fc.classList.toggle('loading', loading);
  fc.title = loading ? 'Loading nearby frames' : '';
}

const captureBtn = $('captureBtn');
const copyBtn = $('copyBtn');
const mapNoticeOverlay = $('mapNoticeOverlay');
const mapLoadingOverlay = $('mapLoadingOverlay');
const mapLoadingText = $('mapLoadingText');
const chaseReflectivityToggle = $('chaseReflectivityToggle');
const soundingProbeBtn = $('soundingProbeBtn');
const soundingPanel = $('soundingPanel');
const soundingPanelTitle = $('soundingPanelTitle');
const soundingPanelMeta = $('soundingPanelMeta');
const soundingPanelStatus = $('soundingPanelStatus');
const soundingChart = $('soundingChart');
const soundingHodo = $('soundingHodo');
const soundingHighlights = $('soundingHighlights');
const soundingHazards = $('soundingHazards');
const soundingSars = $('soundingSars');
const soundingSummary = $('soundingSummary');
const soundingPanelClose = $('soundingPanelClose');
const soundingPanelDownload = $('soundingPanelDownload');
const soundingPanelCopy = $('soundingPanelCopy');

// Satellite controls
const satelliteSelect = $('satelliteSelect');
const satScopeNational = $('satScopeNational');
const satScopeRegional = $('satScopeRegional');
// satProductSelect removed - use sidebar buttons instead

// Display
const hoverValue = $('hoverValue');
const fxxProgress = $('fxxProgress');

function setHoverValueHtml(html, active = false) {
  if (!hoverValue) return;
  
  // Skip update if content is the same (prevents flicker)
  const currentHtml = hoverValue.innerHTML;
  if (currentHtml === html) return;
  
  // Smooth transition: fade out slightly, update, fade in
  if (hoverValue.style.opacity !== '0.85') {
    hoverValue.style.opacity = '0.85';
  }
  
  hoverValue.innerHTML = html || '';
  hoverValue.classList.toggle('active', active);
  
  // Ensure full opacity after a brief delay for smooth transition
  requestAnimationFrame(() => {
    hoverValue.style.opacity = '1';
  });
}

function shouldUseSatelliteImageViewer() {
  return false;
}

function syncSatelliteImageViewerMode() {
  const useViewer = shouldUseSatelliteImageViewer();
  const mapEl = leafletMap?.getContainer?.();
  if (mapEl) {
    mapEl.classList.toggle('hidden', useViewer);
    mapEl.style.display = useViewer ? 'none' : '';
    mapEl.style.visibility = useViewer ? 'hidden' : '';
    mapEl.style.pointerEvents = useViewer ? 'none' : '';
    mapEl.style.position = useViewer ? 'fixed' : '';
    mapEl.style.left = useViewer ? '-200vw' : '';
    mapEl.style.top = useViewer ? '-200vh' : '';
    mapEl.style.width = useViewer ? '1px' : '';
    mapEl.style.height = useViewer ? '1px' : '';
    mapEl.style.overflow = useViewer ? 'hidden' : '';
    mapEl.setAttribute('data-band7-viewer-active', useViewer ? '1' : '0');
  }
  if (mapSection) mapSection.classList.toggle('band7-viewer-mode', useViewer);
  if (satelliteImageViewer) {
    satelliteImageViewer.classList.toggle('hidden', !useViewer);
    satelliteImageViewer.setAttribute('aria-hidden', useViewer ? 'false' : 'true');
  }
  if (!useViewer && satelliteImageViewerImg) {
    satelliteImageViewerImg.removeAttribute('src');
  }
  if (!useViewer && satelliteImageViewerOverlay) {
    satelliteImageViewerOverlay.innerHTML = '';
  }
}

function applySatelliteImageViewerLayout(frame = null) {
  if (!shouldUseSatelliteImageViewer() || !satelliteImageViewerImg) return;
  const activeFrame = frame || state.satFrames[state.satFrameIdx];
  const rect = _viewerImageRect(satelliteImageViewerImg);
  if (!rect) return;
  satelliteImageViewerImg.style.cssText = `position:absolute;left:${rect.x}px;top:${rect.y}px;width:${rect.width}px;height:${rect.height}px;object-fit:fill;`;
  if (activeFrame) {
    renderSatelliteImageViewerOverlay(activeFrame);
  }
}

function _viewerMercY(lat) {
  const latRad = (Math.max(-85, Math.min(85, lat)) * Math.PI) / 180;
  return (180 / Math.PI) * Math.log(Math.tan(Math.PI / 4 + latRad / 2));
}

function _viewerImageRect(imgEl) {
  if (!satelliteImageViewer || !imgEl?.naturalWidth || !imgEl?.naturalHeight) return null;
  const containerW = satelliteImageViewer.clientWidth;
  const containerH = satelliteImageViewer.clientHeight;
  if (!containerW || !containerH) return null;
  const imgRatio = imgEl.naturalWidth / imgEl.naturalHeight;
  const containerRatio = containerW / containerH;
  // Start with contain sizing
  let width, height;
  if (containerRatio > imgRatio) {
    height = containerH;
    width = height * imgRatio;
  } else {
    width = containerW;
    height = width / imgRatio;
  }
  // Scale up to reduce letterboxing — image is cropped to container
  const scale = 1.35;
  width *= scale;
  height *= scale;
  return {
    x: (containerW - width) / 2,
    y: (containerH - height) / 2,
    width,
    height,
  };
}

function _viewerProjectPoint(lat, lon, bounds, rect) {
  const west = bounds.west;
  const east = bounds.east;
  const southMerc = _viewerMercY(bounds.south);
  const northMerc = _viewerMercY(bounds.north);
  const yMerc = _viewerMercY(lat);
  const xNorm = (lon - west) / Math.max(1e-6, east - west);
  const yNorm = (northMerc - yMerc) / Math.max(1e-6, northMerc - southMerc);
  return [
    rect.x + xNorm * rect.width,
    rect.y + yNorm * rect.height,
  ];
}

function _viewerFeatureToPath(feature, bounds, rect) {
  const geom = feature?.geometry;
  if (!geom?.coordinates) return '';
  const paths = [];

  const pushRing = (ring) => {
    if (!Array.isArray(ring) || ring.length < 2) return;
    const coords = ring
      .filter((pair) => Array.isArray(pair) && pair.length >= 2)
      .map(([lon, lat]) => _viewerProjectPoint(lat, lon, bounds, rect));
    if (coords.length < 2) return;
    const [firstX, firstY] = coords[0];
    let segment = `M ${firstX.toFixed(1)} ${firstY.toFixed(1)}`;
    for (let i = 1; i < coords.length; i += 1) {
      const [x, y] = coords[i];
      segment += ` L ${x.toFixed(1)} ${y.toFixed(1)}`;
    }
    paths.push(segment);
  };

  if (geom.type === 'LineString') {
    pushRing(geom.coordinates);
  } else if (geom.type === 'MultiLineString' || geom.type === 'Polygon') {
    geom.coordinates.forEach(pushRing);
  } else if (geom.type === 'MultiPolygon') {
    geom.coordinates.forEach((poly) => poly.forEach(pushRing));
  }

  return paths.join(' ');
}

function renderSatelliteImageViewerOverlay(frame) {
  if (!shouldUseSatelliteImageViewer() || !satelliteImageViewerOverlay || !satelliteImageViewerImg || !frame?.bounds) {
    if (satelliteImageViewerOverlay) satelliteImageViewerOverlay.innerHTML = '';
    return;
  }
  const rect = _viewerImageRect(satelliteImageViewerImg);
  if (!rect) return;
  const bounds = frame.bounds;

  // Cache overlay SVG - only rebuild when bounds or container size change
  const cacheKey = `${bounds.south},${bounds.west},${bounds.north},${bounds.east},${satelliteImageViewer.clientWidth},${satelliteImageViewer.clientHeight}`;
  if (_viewerOverlayCache.key === cacheKey && _viewerOverlayCache.svg) {
    satelliteImageViewerOverlay.setAttribute('viewBox', `0 0 ${satelliteImageViewer.clientWidth} ${satelliteImageViewer.clientHeight}`);
    satelliteImageViewerOverlay.innerHTML = _viewerOverlayCache.svg;
    return;
  }

  satelliteImageViewerOverlay.setAttribute('viewBox', `0 0 ${satelliteImageViewer.clientWidth} ${satelliteImageViewer.clientHeight}`);

  const svgParts = [];
  // Clip all overlays to the satellite image rectangle
  // Clip to the visible portion of the satellite image (intersection of image rect and container)
    const clipX = Math.max(0, rect.x);
    const clipY = Math.max(0, rect.y);
    const clipR = Math.min(satelliteImageViewer.clientWidth, rect.x + rect.width);
    const clipB = Math.min(satelliteImageViewer.clientHeight, rect.y + rect.height);
    svgParts.push(`<defs><clipPath id="satImgClip"><rect x="${clipX.toFixed(1)}" y="${clipY.toFixed(1)}" width="${(clipR - clipX).toFixed(1)}" height="${(clipB - clipY).toFixed(1)}" /></clipPath></defs>`);
  svgParts.push('<g clip-path="url(#satImgClip)">');
  const addFeatures = (geojson, outlineWidth, lineWidth) => {
    if (!geojson?.features?.length) return;
    const paths = geojson.features
      .map((feature) => _viewerFeatureToPath(feature, bounds, rect))
      .filter(Boolean)
      .join(' ');
    if (!paths) return;
    svgParts.push(`<path class="satellite-viewer-outline" d="${paths}" stroke-width="${outlineWidth}" />`);
    svgParts.push(`<path class="satellite-viewer-line" d="${paths}" stroke-width="${lineWidth}" />`);
  };

  addFeatures(_viewerStateGeoJSON, 2.7, 1.25);
  addFeatures(_viewerCountryGeoJSON, 2.7, 1.25);
  addFeatures(_viewerCoastGeoJSON, 2.7, 1.25);

  // Lake boundaries with distinct blue tint
  if (_viewerLakeGeoJSON?.features?.length) {
    const lakePaths = _viewerLakeGeoJSON.features
      .map((feature) => _viewerFeatureToPath(feature, bounds, rect))
      .filter(Boolean)
      .join(' ');
    if (lakePaths) {
      svgParts.push(`<path class="satellite-viewer-lake-outline" d="${lakePaths}" stroke-width="2.2" />`);
      svgParts.push(`<path class="satellite-viewer-lake-line" d="${lakePaths}" stroke-width="0.9" />`);
    }
  }

  const referenceCities = state.satRegion
    ? SATELLITE_REFERENCE_CITIES.concat(SATELLITE_REGIONAL_REFERENCE_CITIES[state.satRegion] || [])
    : SATELLITE_REFERENCE_CITIES;

  for (const [name, lat, lon] of referenceCities) {
    if (lat < bounds.south || lat > bounds.north || lon < bounds.west || lon > bounds.east) continue;
    const [x, y] = _viewerProjectPoint(lat, lon, bounds, rect);
    svgParts.push(`<circle class="satellite-viewer-city-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" />`);
    svgParts.push(`<text class="satellite-viewer-city-label" x="${(x + 8).toFixed(1)}" y="${(y + 4).toFixed(1)}">${name}</text>`);
  }

    svgParts.push('</g>');
  satelliteImageViewerOverlay.innerHTML = svgParts.join('');
  _viewerOverlayCache = { key: cacheKey, svg: svgParts.join('') };
}

function formatCoord(value, digits = 2) {
  return Number.isFinite(value) ? value.toFixed(digits) : '--';
}

function _mixRgb(rgb, base = [13, 15, 20], strength = 0.72) {
  return rgb.map((channel, idx) =>
    Math.round(base[idx] * (1 - strength) + channel * strength)
  );
}

function _toneDownWashRgb(rgb, base = [13, 15, 20]) {
  const avg = Math.round((rgb[0] + rgb[1] + rgb[2]) / 3);
  const softened = rgb.map((channel) => Math.round(avg + (channel - avg) * 0.35));
  return softened.map((channel, idx) =>
    Math.max(base[idx], Math.min(150, channel))
  );
}

function _avgBucket(sum, count, fallback) {
  if (!count) return fallback.slice();
  return sum.map((value) => Math.round(value / count));
}

function resetModelBackgroundWash() {
  const mapEl = leafletMap?.getContainer?.();
  if (!mapEl) return;
  mapEl.classList.remove('model-wash');
  mapEl.style.removeProperty('--model-bg-top');
  mapEl.style.removeProperty('--model-bg-mid');
  mapEl.style.removeProperty('--model-bg-bot');
}

function resetSatelliteBackgroundWash() {
  const mapEl = leafletMap?.getContainer?.();
  if (!mapEl) return;
  mapEl.classList.remove('satellite-wash');
  mapEl.style.removeProperty('--sat-bg-top');
  mapEl.style.removeProperty('--sat-bg-mid');
  mapEl.style.removeProperty('--sat-bg-bot');
}

function applyModelBackgroundWashFromImage(img) {
  if (state.dataSource !== 'models' || !img?.complete || !img.naturalWidth) return;
  const mapEl = leafletMap?.getContainer?.();
  if (!mapEl) return;

  if (_modelWashCanvasCache.src !== img.src) {
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    _modelWashCanvasCache = { src: img.src, canvas, ctx };
  }

  const { canvas, ctx } = _modelWashCanvasCache;
  const { width, height } = canvas;
  if (!width || !height) return;

  const sample = ctx.getImageData(0, 0, width, height).data;
  const stepX = Math.max(1, Math.floor(width / 90));
  const stepY = Math.max(1, Math.floor(height / 70));
  const alphaMin = 20;
  const sums = { top: [0, 0, 0], mid: [0, 0, 0], bot: [0, 0, 0], all: [0, 0, 0] };
  const counts = { top: 0, mid: 0, bot: 0, all: 0 };

  for (let y = 0; y < height; y += stepY) {
    const band = y < height * 0.33 ? 'top' : y < height * 0.67 ? 'mid' : 'bot';
    for (let x = 0; x < width; x += stepX) {
      const idx = (y * width + x) * 4;
      const a = sample[idx + 3];
      if (a < alphaMin) continue;
      const r = sample[idx];
      const g = sample[idx + 1];
      const b = sample[idx + 2];
      const maxChannel = Math.max(r, g, b);
      const minChannel = Math.min(r, g, b);
      const chroma = maxChannel - minChannel;
      const lum = (r + g + b) / 3;
      if (chroma > 165 || lum > 245) continue;
      sums[band][0] += r; sums[band][1] += g; sums[band][2] += b;
      sums.all[0] += r; sums.all[1] += g; sums.all[2] += b;
      counts[band] += 1;
      counts.all += 1;
    }
  }

  // Require meaningful pixel count before applying/resetting wash.
  // Sparse or highly-chromatic products (echo tops, precip) produce few
  // qualifying pixels; without this threshold the wash flickers on/off
  // every frame as counts.all oscillates around 0.
  const totalSamples = Math.ceil((canvas.width / Math.max(1, Math.floor(canvas.width / 90))) *
                                  (canvas.height / Math.max(1, Math.floor(canvas.height / 70))));
  if (counts.all < totalSamples * 0.01) {
    // Too few muted pixels — leave whatever wash is already showing
    return;
  }

  const all = _avgBucket(sums.all, counts.all, [13, 15, 20]);
  const top = _toneDownWashRgb(_mixRgb(_avgBucket(sums.top, counts.top, all), [13, 15, 20], 0.58));
  const mid = _toneDownWashRgb(_mixRgb(_avgBucket(sums.mid, counts.mid, all), [13, 15, 20], 0.54));
  const bot = _toneDownWashRgb(_mixRgb(_avgBucket(sums.bot, counts.bot, all), [13, 15, 20], 0.56));

  mapEl.style.setProperty('--model-bg-top', `rgb(${top.join(', ')})`);
  mapEl.style.setProperty('--model-bg-mid', `rgb(${mid.join(', ')})`);
  mapEl.style.setProperty('--model-bg-bot', `rgb(${bot.join(', ')})`);
  mapEl.classList.add('model-wash');
}

function applySatelliteBackgroundWashFromImage(img) {
  if (state.dataSource !== 'satellite' || !img?.complete || !img.naturalWidth) return;
  const mapEl = leafletMap?.getContainer?.();
  if (!mapEl) return;

  if (_modelWashCanvasCache.src !== img.src) {
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    _modelWashCanvasCache = { src: img.src, canvas, ctx };
  }

  const { canvas, ctx } = _modelWashCanvasCache;
  const { width, height } = canvas;
  if (!width || !height) return;

  const sample = ctx.getImageData(0, 0, width, height).data;
  const stepX = Math.max(1, Math.floor(width / 90));
  const stepY = Math.max(1, Math.floor(height / 70));
  const alphaMin = 30;
  const sums = { top: [0, 0, 0], mid: [0, 0, 0], bot: [0, 0, 0], all: [0, 0, 0] };
  const counts = { top: 0, mid: 0, bot: 0, all: 0 };

  for (let y = 0; y < height; y += stepY) {
    const band = y < height * 0.33 ? 'top' : y < height * 0.67 ? 'mid' : 'bot';
    for (let x = 0; x < width; x += stepX) {
      const idx = (y * width + x) * 4;
      const a = sample[idx + 3];
      if (a < alphaMin) continue;
      const r = sample[idx];
      const g = sample[idx + 1];
      const b = sample[idx + 2];
      const maxChannel = Math.max(r, g, b);
      const minChannel = Math.min(r, g, b);
      const chroma = maxChannel - minChannel;
      const lum = (r + g + b) / 3;
      if (lum > 248) continue;
      if (chroma > 210 && lum > 150) continue;
      sums[band][0] += r; sums[band][1] += g; sums[band][2] += b;
      sums.all[0] += r; sums.all[1] += g; sums.all[2] += b;
      counts[band] += 1;
      counts.all += 1;
    }
  }

  if (!counts.all) {
    resetSatelliteBackgroundWash();
    return;
  }

  const all = _avgBucket(sums.all, counts.all, [10, 10, 20]);
  const top = _toneDownWashRgb(_mixRgb(_avgBucket(sums.top, counts.top, all), [10, 10, 20], 0.64), [10, 10, 20]);
  const mid = _toneDownWashRgb(_mixRgb(_avgBucket(sums.mid, counts.mid, all), [10, 10, 20], 0.60), [10, 10, 20]);
  const bot = _toneDownWashRgb(_mixRgb(_avgBucket(sums.bot, counts.bot, all), [10, 10, 20], 0.62), [10, 10, 20]);

  mapEl.style.setProperty('--sat-bg-top', `rgb(${top.join(', ')})`);
  mapEl.style.setProperty('--sat-bg-mid', `rgb(${mid.join(', ')})`);
  mapEl.style.setProperty('--sat-bg-bot', `rgb(${bot.join(', ')})`);
  mapEl.classList.add('satellite-wash');
}

// Debounce control for hover queries - wait for mouse to stop before querying
let _hoverDebounceTimer = null;
const HOVER_DEBOUNCE_MS = 30; // Fast response - 30ms after mouse stops
let _hoverAbortController = null;
let _lastHoverLat = null;
let _lastHoverLon = null;
let _isQueryInProgress = false;

// Client-side PNG sampling for instant value lookups
let _paramColorScaleCache = {};
let _overlayCanvasCache = { src: null, canvas: null, ctx: null };
let _modelWashCanvasCache = { src: null, canvas: null, ctx: null };

function parseLegendColor(color) {
  if (!color) return null;
  const value = String(color).trim();
  if (value.startsWith('#') && value.length === 7) {
    return [
      parseInt(value.slice(1, 3), 16),
      parseInt(value.slice(3, 5), 16),
      parseInt(value.slice(5, 7), 16),
    ];
  }
  const match = value.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
  if (!match) return null;
  return [parseInt(match[1]), parseInt(match[2]), parseInt(match[3])];
}

function paletteQueryForCurrentRun() {
  if (state.dataSource !== 'models' || !state.runPaletteVersions || state.runId == null) return '';
  const v = state.runPaletteVersions[String(state.runId)];
  return v ? `?palette_version=${v}` : '';
}

async function getValueFromOverlayImage(lat, lon, parameter) {
  const overlayImg = state.overlayLayer?.getElement?.();
  if (!overlayImg || !overlayImg.complete) return null;
  const transparentIsZeroParams = new Set(['snow_depth', 'weasd']);

  const paletteQ = paletteQueryForCurrentRun();
  const scaleCacheKey = parameter + paletteQ;
  let scaleInfo = _paramColorScaleCache[scaleCacheKey];
  if (!scaleInfo) {
    try {
      const data = await apiFetch(`${BASE}/api/legend/${parameter}${paletteQ}`);
      const lookup = {};

      if (data.color_lookup && data.color_lookup.length > 0) {
        // Use server-computed lookup (exact colormap + enhancement match)
        for (const [r, g, b, value] of data.color_lookup) {
          lookup[`${r},${g},${b}`] = value;
        }
      } else if (data.discrete && data.colors && data.bounds) {
        for (let i = 0; i < data.colors.length; i++) {
          const rgb = parseLegendColor(data.colors[i]);
          if (!rgb) continue;
          const upperIdx = Math.min(i + 1, data.bounds.length - 1);
          const value = (Number(data.bounds[i]) + Number(data.bounds[upperIdx])) / 2;
          lookup[`${rgb[0]},${rgb[1]},${rgb[2]}`] = value;
        }
      } else if (data.stops && data.stops.length > 0) {
        // Fallback for radar/reflectivity: interpolate from stops (no enhancement)
        for (let i = 0; i < 256; i++) {
          const t = i / 255;
          let stopIdx = 0;
          for (let j = 0; j < data.stops.length - 1; j++) {
            if (t >= data.stops[j].pos && t <= data.stops[j + 1].pos) {
              stopIdx = j;
              break;
            }
          }
          const stop1 = data.stops[stopIdx];
          const stop2 = data.stops[Math.min(stopIdx + 1, data.stops.length - 1)];
          const range = stop2.pos - stop1.pos;
          const localT = range > 0 ? (t - stop1.pos) / range : 0;
          const c1 = parseLegendColor(stop1.color);
          const c2 = parseLegendColor(stop2.color);
          if (!c1 || !c2) continue;
          const r = Math.round(c1[0] + (c2[0] - c1[0]) * localT);
          const g = Math.round(c1[1] + (c2[1] - c1[1]) * localT);
          const b = Math.round(c1[2] + (c2[2] - c1[2]) * localT);
          const value = data.vmin + t * (data.vmax - data.vmin);
          lookup[`${r},${g},${b}`] = value;
        }
      }
      scaleInfo = { vmin: data.vmin, vmax: data.vmax, label: data.label, unit: data.unit, lookup: lookup };
      _paramColorScaleCache[scaleCacheKey] = scaleInfo;
    } catch (e) { return null; }
  }

  const img = overlayImg;
  if (!img.naturalWidth) return null;
  const bounds = state.overlayBounds;
  if (!bounds) return null;
  const [[south, west], [north, east]] = bounds;
  if (lat < south || lat > north || lon < west || lon > east) return null;

  // Cache canvas per image src to avoid re-drawing on every mousemove
  if (_overlayCanvasCache.src !== img.src) {
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0);
    _overlayCanvasCache = { src: img.src, canvas, ctx };
  }
  const ctx = _overlayCanvasCache.ctx;

  const px = ((lon - west) / (east - west)) * img.naturalWidth;
  // Image is rendered in Mercator Y-space — convert lat to Mercator before interpolating
  const latToMerc = (d) => { const r = d * Math.PI / 180; return (Math.log(Math.tan(Math.PI / 4 + r / 2))) * 180 / Math.PI; };
  const mercNorth = latToMerc(north), mercSouth = latToMerc(south), mercLat = latToMerc(lat);
  const py = ((mercNorth - mercLat) / (mercNorth - mercSouth)) * img.naturalHeight;
  const sx = Math.max(0, Math.min(Math.floor(px), img.naturalWidth - 1));
  const sy = Math.max(0, Math.min(Math.floor(py), img.naturalHeight - 1));
  const pixel = ctx.getImageData(sx, sy, 1, 1).data;
  const alpha = pixel[3];
  if (alpha < 16) {
    if (transparentIsZeroParams.has(parameter)) {
      return { value: 0, unit: scaleInfo.unit || '', label: scaleInfo.label || parameter };
    }
    return null;
  }
  const rgbKey = `${pixel[0]},${pixel[1]},${pixel[2]}`;
  let value = scaleInfo.lookup[rgbKey];
  if (value === undefined) {
    let bestDist = Infinity, bestVal = null;
    for (const [key, val] of Object.entries(scaleInfo.lookup)) {
      const [lr, lg, lb] = key.split(',').map(Number);
      const dist = (pixel[0] - lr) ** 2 + (pixel[1] - lg) ** 2 + (pixel[2] - lb) ** 2;
      if (dist < bestDist) { bestDist = dist; bestVal = val; }
    }
    if (bestDist < 20000) value = bestVal;
  }
  if (value === undefined) return null;
  return { value, unit: scaleInfo.unit || '', label: scaleInfo.label || parameter };
}


async function handleMapValueQuery(lat, lon, source) {
  _lastHoverLat = lat;
  _lastHoverLon = lon;
  
  // Cancel any pending query
  if (_hoverAbortController) {
    _hoverAbortController.abort();
  }
  _hoverAbortController = new AbortController();
  
  // Mark query as in progress (but don't show loading state)
  _isQueryInProgress = true;
  
  if (source === 'models' && state.runId != null && state.currentFxx != null && state.parameter) {
    const result = await getValueFromOverlayImage(lat, lon, state.parameter);
    if (result) {
      const label = escapeHtml(result.label || state.parameter);
      const value = Number.isFinite(result.value) ? result.value.toFixed(1) : '--';
      const unit = escapeHtml(result.unit || '');
      setHoverValueHtml(`<span class="value">${escapeHtml(value)}${unit ? ` ${unit}` : ''}</span><br><span>${label}</span>`, true);
    }
    _isQueryInProgress = false;
    return;
  }

  if (source === 'radar' && state.radarSite && state.radarProduct) {
    try {
      const path = `${BASE}/api/value/radar/${encodeURIComponent(state.radarSite)}/${encodeURIComponent(state.radarProduct)}?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}&tilt=${encodeURIComponent(state.radarFrames[state.radarFrameIdx]?.tilt ?? state.radarTilt)}${state.radarFrames[state.radarFrameIdx]?.timestamp ? `&frame=${encodeURIComponent(state.radarFrames[state.radarFrameIdx].timestamp)}` : ''}`;
      const resp = await fetch(path, { signal: _hoverAbortController.signal });
      if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
      const data = await resp.json();
      if (data.value == null) {
        setHoverValueHtml(`<span>${escapeHtml(formatCoord(lat))}°N, ${escapeHtml(formatCoord(lon))}°W</span>`, true);
      } else {
        const unit = escapeHtml(data.unit || '');
        const label = escapeHtml(data.label || state.radarProduct);
        setHoverValueHtml(`<span class="value">${data.value.toFixed(1)}${unit ? ` ${unit}` : ''}</span><br><span>${label}</span>`, true);
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        setHoverValueHtml(`<span>${escapeHtml(formatCoord(lat))}°N, ${escapeHtml(formatCoord(lon))}°W</span>`, true);
      }
    }
    _isQueryInProgress = false;
    return;
  }

  let label = 'Location';
  if (source === 'radar') label = `${state.radarSite} ${state.radarProduct}`;
  else if (source === 'satellite') label = `${state.satellite.toUpperCase()} ${state.satProduct}`;
  else if (source === 'home') label = 'Home map';
  setHoverValueHtml(`<span>${escapeHtml(formatCoord(lat))}°N, ${escapeHtml(formatCoord(lon))}°W</span><br><span>${escapeHtml(label)}</span>`, true);
  _isQueryInProgress = false;
}

function debouncedMapHover(evt) {
  if (!evt?.latlng) return;

  const lat = evt.latlng.lat;
  const lon = evt.latlng.lng;
  const source = state.dataSource;

  if (source === 'models') {
    // Client-side sampling is instant — no debounce needed
    handleMapValueQuery(lat, lon, source);
  } else {
    // Server-side queries (radar, satellite) still debounce
    if (_hoverDebounceTimer) {
      clearTimeout(_hoverDebounceTimer);
    }
    _hoverDebounceTimer = setTimeout(() => {
      handleMapValueQuery(lat, lon, source);
    }, HOVER_DEBOUNCE_MS);
  }
}

// Keep click handler for compatibility
async function handleMapValueClick(evt) {
  if (!evt?.latlng) return;
  const meteogramProbe = window.WXMeteogram?.isEnabled?.() === true;
  if (state.dataSource === 'models' && (state.soundingProbeEnabled || meteogramProbe)) {
    if (_mapPointerDragged) return;
    if (_mapPointerDownPoint && evt.containerPoint) {
      const dx = evt.containerPoint.x - _mapPointerDownPoint.x;
      const dy = evt.containerPoint.y - _mapPointerDownPoint.y;
      if (Math.hypot(dx, dy) > 6) return;
    }
    if (meteogramProbe) {
      window.WXMeteogram.open(evt.latlng);
      return;
    }
    await loadModelSounding(evt.latlng);
    return;
  }
  // Click just triggers the same query as hover (immediate)
  if (_hoverDebounceTimer) clearTimeout(_hoverDebounceTimer);
  await handleMapValueQuery(evt.latlng.lat, evt.latlng.lng, state.dataSource);
  // The v4 point readout (ui-v4.js) listens for clicked points.
  document.dispatchEvent(new CustomEvent('wx:point-query', {
    detail: {
      lat: evt.latlng.lat,
      lng: evt.latlng.lng,
      source: state.dataSource,
      value: hoverValue?.querySelector('.value')?.textContent || '',
      label: hoverValue?.querySelector('.value ~ span')?.textContent || '',
    },
  }));
}

function updateSoundingProbeUi() {
  if (soundingProbeBtn) {
    soundingProbeBtn.classList.toggle('active', state.soundingProbeEnabled);
    soundingProbeBtn.setAttribute('aria-pressed', state.soundingProbeEnabled ? 'true' : 'false');
    soundingProbeBtn.textContent = state.soundingProbeEnabled ? 'Sounding Probe On' : 'Sounding Probe';
  }
  const mapEl = leafletMap?.getContainer?.();
  if (mapEl) {
    mapEl.classList.toggle('sounding-probe-active', state.soundingProbeEnabled && state.dataSource === 'models');
    mapEl.style.cursor = state.soundingProbeEnabled && state.dataSource === 'models' ? 'crosshair' : '';
  }
}

function setSoundingStatus(message = '', visible = false) {
  if (!soundingPanelStatus) return;
  soundingPanelStatus.textContent = message || '';
  soundingPanelStatus.classList.toggle('hidden', !visible || !message);
}

function clearSoundingChart() {
  if (soundingChart) soundingChart.innerHTML = '';
  if (soundingHodo) soundingHodo.innerHTML = '';
  if (soundingHighlights) soundingHighlights.innerHTML = '';
  if (soundingHazards) soundingHazards.innerHTML = '';
  if (soundingSars) soundingSars.innerHTML = '';
  if (soundingSummary) soundingSummary.innerHTML = '';
}

function setSoundingProbeEnabled(enabled) {
  state.soundingProbeEnabled = !!enabled;
  updateSoundingProbeUi();
  if (!enabled && soundingPanel) {
    soundingPanel.classList.add('hidden');
  }
  if (!enabled && !state.soundingData) {
    if (soundingPanelTitle) soundingPanelTitle.textContent = 'Probe inactive';
    if (soundingPanelMeta) soundingPanelMeta.textContent = 'Enable Sounding Probe, then click the map.';
    setSoundingStatus('', false);
    clearSoundingChart();
  }
}

function closeSoundingPanel(resetProbe = false) {
  state.soundingData = null;
  if (resetProbe) {
    state.soundingPoint = null;
    setSoundingProbeEnabled(false);
  }
  if (soundingPanel) soundingPanel.classList.add('hidden');
  if (soundingPanelTitle) soundingPanelTitle.textContent = 'Probe inactive';
  if (soundingPanelMeta) soundingPanelMeta.textContent = 'Enable Sounding Probe, then click the map.';
  setSoundingStatus('', false);
  clearSoundingChart();
}

function formatLatLonLabel(latlng) {
  if (!latlng) return '--';
  const lat = Math.abs(latlng.lat);
  const lon = Math.abs(latlng.lng);
  const latHem = latlng.lat >= 0 ? 'N' : 'S';
  const lonHem = latlng.lng >= 0 ? 'E' : 'W';
  return `${formatCoord(lat, 2)}°${latHem}, ${formatCoord(lon, 2)}°${lonHem}`;
}

function escapeAttr(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function formatSignedNumber(value, digits = 0) {
  if (!Number.isFinite(value)) return '--';
  const abs = Math.abs(value);
  const text = digits > 0 ? abs.toFixed(digits) : Math.round(abs).toString();
  return `${value > 0 ? '+' : value < 0 ? '-' : ''}${text}`;
}

function formatProbability(value) {
  if (!Number.isFinite(value)) return '--';
  return `${Math.round(value * 100)}%`;
}

function buildSoundingCardStat(label, value) {
  return `<div class="sounding-card-row"><div class="sounding-card-stat-label">${escapeHtml(label)}</div><div class="sounding-card-stat-value">${escapeHtml(value)}</div></div>`;
}

function buildSoundingCard(title, subtitle, stats = [], analogs = []) {
  const statHtml = stats.length ? `<div class="sounding-card-grid">${stats.join('')}</div>` : '';
  const analogHtml = analogs.length ? `<div class="sounding-analog-list">${analogs.map((analog) => `<div class="sounding-analog-item"><strong>${escapeHtml(analog.label || '--')}</strong><span>${escapeHtml(analog.match || '--')}</span></div>`).join('')}</div>` : '';
  return `<section class="sounding-card"><div class="sounding-card-title">${escapeHtml(title)}</div>${subtitle ? `<div class="sounding-card-subtitle">${escapeHtml(subtitle)}</div>` : ''}${statHtml}${analogHtml}</section>`;
}

function buildSoundingSummaryItem(label, value) {
  return `<div class="sounding-summary-item"><div class="sounding-summary-label">${escapeHtml(label)}</div><div class="sounding-summary-value">${escapeHtml(value)}</div></div>`;
}

function buildSoundingSummaryGroup(title, items = []) {
  if (!items.length) return '';
  return `<section class="sounding-summary-group"><div class="sounding-summary-group-title">${escapeHtml(title)}</div><div class="sounding-summary-rows">${items.join('')}</div></section>`;
}

function renderSoundingHighlights(data) {
  if (!soundingHighlights) return;
  const diagnostics = data.diagnostics || {};
  const surface = data.surface || {};
  const wind = surface.wind || {};
  const cards = [
    `<div class="sounding-highlight sounding-highlight--watch"><div class="sounding-highlight-label">Hazard Type</div><div class="sounding-highlight-value">${escapeHtml(diagnostics.watch_type || 'NONE')}</div></div>`,
    `<div class="sounding-highlight"><div class="sounding-highlight-label">MLCAPE / CIN</div><div class="sounding-highlight-value">${escapeHtml(`${Number.isFinite(diagnostics.mlcape_jkg) ? Math.round(diagnostics.mlcape_jkg) : '--'} / ${Number.isFinite(diagnostics.mlcin_jkg) ? Math.round(diagnostics.mlcin_jkg) : '--'}`)}</div></div>`,
    `<div class="sounding-highlight"><div class="sounding-highlight-label">0-1km SRH</div><div class="sounding-highlight-value">${escapeHtml(Number.isFinite(diagnostics.srh_1km_m2s2) ? `${Math.round(diagnostics.srh_1km_m2s2)} m2/s2` : '--')}</div></div>`,
    `<div class="sounding-highlight"><div class="sounding-highlight-label">EBWD</div><div class="sounding-highlight-value">${escapeHtml(Number.isFinite(diagnostics.ebwd_kt) ? `${Math.round(diagnostics.ebwd_kt)} kt` : '--')}</div></div>`,
    `<div class="sounding-highlight"><div class="sounding-highlight-label">Surface Wind</div><div class="sounding-highlight-value">${escapeHtml(Number.isFinite(wind.dir_deg) && Number.isFinite(wind.speed_kt) ? `${Math.round(wind.dir_deg)}/${Math.round(wind.speed_kt)} kt` : '--')}</div></div>`,
  ];
  soundingHighlights.innerHTML = cards.join('');
}

function renderSoundingHazards(data) {
  if (!soundingHazards) return;
  const diagnostics = data.diagnostics || {};
  const hazardCards = [
    buildSoundingCard(
      'Watch / Hazard Type',
      'Primary SHARPpy watch guidance from the sampled profile.',
      [
        buildSoundingCardStat('Primary', diagnostics.watch_type || 'NONE'),
        buildSoundingCardStat('Right Mover', diagnostics.right_watch_type || '--'),
        buildSoundingCardStat('Left Mover', diagnostics.left_watch_type || '--'),
        buildSoundingCardStat('Precip Type', diagnostics.precip_type || '--'),
      ],
    ),
    buildSoundingCard(
      'Severe Parameter Table',
      'Composite severe-weather and downdraft diagnostics.',
      [
        buildSoundingCardStat('STP (CIN)', Number.isFinite(diagnostics.stp_cin) ? diagnostics.stp_cin.toFixed(2) : '--'),
        buildSoundingCardStat('SCP', Number.isFinite(diagnostics.scp) ? diagnostics.scp.toFixed(2) : '--'),
        buildSoundingCardStat('SHIP', Number.isFinite(diagnostics.ship) ? diagnostics.ship.toFixed(2) : '--'),
        buildSoundingCardStat('WNDG', Number.isFinite(diagnostics.wndg) ? diagnostics.wndg.toFixed(2) : '--'),
        buildSoundingCardStat('Sig Severe', Number.isFinite(diagnostics.sig_severe) ? `${Math.round(diagnostics.sig_severe)}` : '--'),
        buildSoundingCardStat('DCAPE', Number.isFinite(diagnostics.dcape_jkg) ? `${Math.round(diagnostics.dcape_jkg)} J/kg` : '--'),
      ],
    ),
  ];
  soundingHazards.innerHTML = hazardCards.join('');
}

function renderSoundingSars(data) {
  if (!soundingSars) return;
  const diagnostics = data.diagnostics || {};
  const hail = diagnostics.sars_hail || {};
  const supercell = diagnostics.sars_supercell || {};
  const cards = [
    buildSoundingCard(
      'SARS Hail Analogs',
      'Historic hail analog matches pulled from SHARPpy/SPC SARS logic.',
      [
        buildSoundingCardStat('Sig Hail Prob', formatProbability(hail.significant_probability)),
        buildSoundingCardStat('Loose Matches', Number.isFinite(hail.loose_match_count) ? `${hail.loose_match_count}` : '--'),
        buildSoundingCardStat('Sig Reports', Number.isFinite(hail.significant_match_count) ? `${hail.significant_match_count}` : '--'),
      ],
      hail.analogs || [],
    ),
    buildSoundingCard(
      'SARS Supercell Analogs',
      'Historic supercell proximity-sounding analogs and estimated tornado hit rate.',
      [
        buildSoundingCardStat('Tornado Prob', formatProbability(supercell.tornado_probability)),
        buildSoundingCardStat('Loose Matches', Number.isFinite(supercell.loose_match_count) ? `${supercell.loose_match_count}` : '--'),
        buildSoundingCardStat('Tornadic Matches', Number.isFinite(supercell.tornadic_loose_match_count) ? `${supercell.tornadic_loose_match_count}` : '--'),
      ],
      supercell.analogs || [],
    ),
  ];
  soundingSars.innerHTML = cards.join('');
}

function renderSoundingSummary(data) {
  if (!soundingSummary) return;
  const surface = data.surface || {};
  const diagnostics = data.diagnostics || {};
  const surfaceWind = surface.wind || null;
  const thermoRows = [
    buildSoundingSummaryItem('Profile', `${data.level_count || 0} levels`),
    buildSoundingSummaryItem('Quality', data.quality || '--'),
    buildSoundingSummaryItem('Surface Temp', surface.temp_c != null ? `${surface.temp_c.toFixed(1)} C` : '--'),
    buildSoundingSummaryItem('Surface Dewpoint', surface.dewpoint_c != null ? `${surface.dewpoint_c.toFixed(1)} C` : '--'),
    buildSoundingSummaryItem('Surface Wind', surfaceWind && surfaceWind.dir_deg != null && surfaceWind.speed_kt != null ? `${surfaceWind.dir_deg}/${Math.round(surfaceWind.speed_kt)} kt` : '--'),
    buildSoundingSummaryItem('MSLP', surface.mslp_mb != null ? `${surface.mslp_mb.toFixed(1)} mb` : '--'),
    buildSoundingSummaryItem('LI (500mb)', Number.isFinite(diagnostics.li5_c) ? `${formatSignedNumber(diagnostics.li5_c, 1)} C` : '--'),
    buildSoundingSummaryItem('Totals Totals', diagnostics.totals_totals != null ? `${diagnostics.totals_totals.toFixed(1)}` : '--'),
    buildSoundingSummaryItem('K Index', diagnostics.k_index != null ? `${diagnostics.k_index.toFixed(1)}` : '--'),
    buildSoundingSummaryItem('TEI', diagnostics.tei != null ? `${diagnostics.tei.toFixed(1)}` : '--'),
  ];
  const parcelRows = [
    buildSoundingSummaryItem('MLCAPE', diagnostics.mlcape_jkg != null ? `${Math.round(diagnostics.mlcape_jkg)} J/kg` : '--'),
    buildSoundingSummaryItem('MLCIN', diagnostics.mlcin_jkg != null ? `${Math.round(diagnostics.mlcin_jkg)} J/kg` : '--'),
    buildSoundingSummaryItem('MUCAPE', diagnostics.mucape_jkg != null ? `${Math.round(diagnostics.mucape_jkg)} J/kg` : '--'),
    buildSoundingSummaryItem('ML LCL', diagnostics.mllcl_m != null ? `${Math.round(diagnostics.mllcl_m)} m` : '--'),
    buildSoundingSummaryItem('ML LFC', diagnostics.mllfc_m != null ? `${Math.round(diagnostics.mllfc_m)} m` : '--'),
    buildSoundingSummaryItem('ML EL', diagnostics.mlel_m != null ? `${Math.round(diagnostics.mlel_m)} m` : '--'),
    buildSoundingSummaryItem('DCAPE', diagnostics.dcape_jkg != null ? `${Math.round(diagnostics.dcape_jkg)} J/kg` : '--'),
  ];
  const kinematicRows = [
    buildSoundingSummaryItem('0-1km SRH', diagnostics.srh_1km_m2s2 != null ? `${Math.round(diagnostics.srh_1km_m2s2)} m2/s2` : '--'),
    buildSoundingSummaryItem('0-3km SRH', diagnostics.srh_3km_m2s2 != null ? `${Math.round(diagnostics.srh_3km_m2s2)} m2/s2` : '--'),
    buildSoundingSummaryItem('EBWD', diagnostics.ebwd_kt != null ? `${Math.round(diagnostics.ebwd_kt)} kt` : '--'),
    buildSoundingSummaryItem('Bunkers RM', diagnostics.bunkers_right && diagnostics.bunkers_right.dir_deg != null ? `${diagnostics.bunkers_right.dir_deg}/${Math.round(diagnostics.bunkers_right.speed_kt)} kt` : '--'),
    buildSoundingSummaryItem('Bunkers LM', diagnostics.bunkers_left && diagnostics.bunkers_left.dir_deg != null ? `${diagnostics.bunkers_left.dir_deg}/${Math.round(diagnostics.bunkers_left.speed_kt)} kt` : '--'),
  ];
  const compositeRows = [
    buildSoundingSummaryItem('SHIP', diagnostics.ship != null ? `${diagnostics.ship.toFixed(2)}` : '--'),
    buildSoundingSummaryItem('STP (CIN)', diagnostics.stp_cin != null ? `${diagnostics.stp_cin.toFixed(2)}` : '--'),
    buildSoundingSummaryItem('SCP', diagnostics.scp != null ? `${diagnostics.scp.toFixed(2)}` : '--'),
    buildSoundingSummaryItem('WNDG', diagnostics.wndg != null ? `${diagnostics.wndg.toFixed(2)}` : '--'),
    buildSoundingSummaryItem('Sig Severe', diagnostics.sig_severe != null ? `${Math.round(diagnostics.sig_severe)}` : '--'),
  ];
  soundingSummary.innerHTML = [
    buildSoundingSummaryGroup('Thermodynamics', thermoRows),
    buildSoundingSummaryGroup('Parcel / Buoyancy', parcelRows),
    buildSoundingSummaryGroup('Kinematics', kinematicRows),
    buildSoundingSummaryGroup('Composite Indices', compositeRows),
  ].join('');
}

function buildSoundingPath(levels, accessor, xFn, yFn) {
  const points = levels
    .filter((level) => Number.isFinite(accessor(level)))
    .map((level) => `${xFn(accessor(level), level.pressure_hpa).toFixed(1)},${yFn(level.pressure_hpa).toFixed(1)}`);
  return points.length >= 2 ? `M ${points.join(' L ')}` : '';
}

function buildThermoPath(points, xFn, yFn) {
  const pathPoints = points
    .filter((point) => Number.isFinite(point?.tempC) && Number.isFinite(point?.pressureHpa))
    .map((point) => `${xFn(point.tempC, point.pressureHpa).toFixed(1)},${yFn(point.pressureHpa).toFixed(1)}`);
  return pathPoints.length >= 2 ? `M ${pathPoints.join(' L ')}` : '';
}

const SHARPY_ZEROCNK = 273.15;
const SHARPY_ROCP = 0.28571426;

function sharppyTheta(p, tC, p2 = 1000) {
  return ((tC + SHARPY_ZEROCNK) * Math.pow((p2 / p), SHARPY_ROCP)) - SHARPY_ZEROCNK;
}

function sharppyWobf(tC) {
  const t = tC - 20;
  if (t <= 0) {
    let npol = 1 + t * (-8.841660499999999e-3 + t * (1.4714143e-4 + t * (-9.671989000000001e-7 + t * (-3.2607217e-8 + t * (-3.8598073e-10)))));
    return 15.13 / Math.pow(npol, 4);
  }
  let ppol = t * (4.9618922e-7 + t * (-6.1059365e-9 + t * (3.9401551e-11 + t * (-1.2588129e-13 + t * (1.668828e-16)))));
  ppol = 1 + t * (3.6182989e-3 + t * (-1.3603273e-5 + ppol));
  return (29.93 / Math.pow(ppol, 4)) + (0.96 * t) - 14.8;
}

function sharppySatlift(p, thetamC, conv = 0.1) {
  if (Math.abs(p - 1000) <= 0.001) return thetamC;
  const pwrp = Math.pow((p / 1000), SHARPY_ROCP);
  let eor = 999;
  let t1 = 0;
  let e1 = 0;
  let t2 = 0;
  let e2 = 0;
  let rate = 1;
  while (Math.abs(eor) > conv) {
    if (eor === 999) {
      t1 = (thetamC + SHARPY_ZEROCNK) * pwrp - SHARPY_ZEROCNK;
      e1 = sharppyWobf(t1) - sharppyWobf(thetamC);
      rate = 1;
    } else {
      rate = (t2 - t1) / (e2 - e1);
      t1 = t2;
      e1 = e2;
    }
    t2 = t1 - (e1 * rate);
    e2 = ((t2 + SHARPY_ZEROCNK) / pwrp) - SHARPY_ZEROCNK;
    e2 += sharppyWobf(t2) - sharppyWobf(e2) - thetamC;
    eor = e2 * rate;
  }
  return t2 - eor;
}

function sharppyWetlift(p, tC, p2) {
  const thetaC = sharppyTheta(p, tC, 1000);
  const thetam = thetaC - sharppyWobf(thetaC) + sharppyWobf(tC);
  return sharppySatlift(p2, thetam);
}

function buildDryAdiabatPoints(thetaC, pressureBottom, pressureTop, stepHpa = 25) {
  const points = [];
  for (let pressure = pressureBottom; pressure >= pressureTop; pressure -= stepHpa) {
    const tempC = ((thetaC + SHARPY_ZEROCNK) / Math.pow((1000 / pressure), SHARPY_ROCP)) - SHARPY_ZEROCNK;
    points.push({ pressureHpa: pressure, tempC });
  }
  return points;
}

function buildMoistAdiabatPoints(surfaceTempC, pressureBottom, pressureTop, stepHpa = 10) {
  const points = [];
  for (let pressure = pressureBottom; pressure >= pressureTop; pressure -= stepHpa) {
    points.push({ pressureHpa: pressure, tempC: sharppyWetlift(1000, surfaceTempC, pressure) });
  }
  return points;
}

function sharppyTempAtMixrat(mixingRatioGkg, pressureHpa) {
  if (!Number.isFinite(mixingRatioGkg) || !Number.isFinite(pressureHpa) || mixingRatioGkg <= 0 || pressureHpa <= 0) return null;
  const c1 = 0.0498646455;
  const c2 = 2.4082965;
  const c3 = 7.07475;
  const c4 = 38.9114;
  const c5 = 0.0915;
  const c6 = 1.2035;
  const x = Math.log10((mixingRatioGkg * pressureHpa) / (622 + mixingRatioGkg));
  return Math.pow(10, (c1 * x) + c2) - c3 + (c4 * Math.pow(Math.pow(10, c5 * x) - c6, 2)) - SHARPY_ZEROCNK;
}

function buildMixingRatioPoints(mixingRatioGkg, pressureBottom, pressureTop, stepHpa = 25) {
  const points = [];
  for (let pressure = pressureBottom; pressure >= pressureTop; pressure -= stepHpa) {
    const tempC = sharppyTempAtMixrat(mixingRatioGkg, pressure);
    if (Number.isFinite(tempC)) {
      points.push({ pressureHpa: pressure, tempC });
    }
  }
  return points;
}

function interpolateProfileTemp(points, pressureHpa) {
  if (!Array.isArray(points) || points.length < 2 || !Number.isFinite(pressureHpa)) return null;
  for (let i = 0; i < points.length - 1; i += 1) {
    const a = points[i];
    const b = points[i + 1];
    if (!Number.isFinite(a?.pressure_hpa) || !Number.isFinite(b?.pressure_hpa) || !Number.isFinite(a?.temp_c) || !Number.isFinite(b?.temp_c)) continue;
    const top = Math.min(a.pressure_hpa, b.pressure_hpa);
    const bottom = Math.max(a.pressure_hpa, b.pressure_hpa);
    if (pressureHpa < top || pressureHpa > bottom) continue;
    const denom = Math.log(b.pressure_hpa) - Math.log(a.pressure_hpa);
    if (!Number.isFinite(denom) || Math.abs(denom) < 1e-6) return a.temp_c;
    const frac = (Math.log(pressureHpa) - Math.log(a.pressure_hpa)) / denom;
    return a.temp_c + ((b.temp_c - a.temp_c) * frac);
  }
  return null;
}

function buildParcelShadePaths(parcelTrace, envTrace, pressureTop, pressureBottom, tempToX, pressureToY) {
  if (!Array.isArray(parcelTrace) || parcelTrace.length < 2 || !Array.isArray(envTrace) || envTrace.length < 2) {
    return { cape: [], cin: [] };
  }

  const results = { cape: [], cin: [] };
  const segments = [];
  for (let i = 0; i < parcelTrace.length - 1; i += 1) {
    const a = parcelTrace[i];
    const b = parcelTrace[i + 1];
    if (!Number.isFinite(a?.pressure_hpa) || !Number.isFinite(b?.pressure_hpa) || !Number.isFinite(a?.temp_c) || !Number.isFinite(b?.temp_c)) continue;
    if (a.pressure_hpa > pressureBottom || a.pressure_hpa < pressureTop || b.pressure_hpa > pressureBottom || b.pressure_hpa < pressureTop) continue;
    const envA = interpolateProfileTemp(envTrace, a.pressure_hpa);
    const envB = interpolateProfileTemp(envTrace, b.pressure_hpa);
    if (!Number.isFinite(envA) || !Number.isFinite(envB)) continue;
    segments.push({
      a: { pressure_hpa: a.pressure_hpa, parcel_temp_c: a.temp_c, env_temp_c: envA },
      b: { pressure_hpa: b.pressure_hpa, parcel_temp_c: b.temp_c, env_temp_c: envB },
    });
  }

  const pushPolygon = (bucket, points) => {
    if (!points || points.length < 3) return;
    const coords = points.map((point) => `${tempToX(point.temp_c, point.pressure_hpa).toFixed(1)},${pressureToY(point.pressure_hpa).toFixed(1)}`);
    if (coords.length >= 3) results[bucket].push(`M ${coords.join(' L ')} Z`);
  };

  for (const segment of segments) {
    const diffA = segment.a.parcel_temp_c - segment.a.env_temp_c;
    const diffB = segment.b.parcel_temp_c - segment.b.env_temp_c;
    const sameSign = (diffA >= 0 && diffB >= 0) || (diffA <= 0 && diffB <= 0);
    const bucketFor = (diff) => (diff >= 0 ? 'cape' : 'cin');

    if (sameSign) {
      pushPolygon(bucketFor(diffA), [
        { pressure_hpa: segment.a.pressure_hpa, temp_c: segment.a.parcel_temp_c },
        { pressure_hpa: segment.b.pressure_hpa, temp_c: segment.b.parcel_temp_c },
        { pressure_hpa: segment.b.pressure_hpa, temp_c: segment.b.env_temp_c },
        { pressure_hpa: segment.a.pressure_hpa, temp_c: segment.a.env_temp_c },
      ]);
      continue;
    }

    const frac = diffA / (diffA - diffB);
    if (!Number.isFinite(frac)) continue;
    const crossPressure = segment.a.pressure_hpa + ((segment.b.pressure_hpa - segment.a.pressure_hpa) * frac);
    const crossTemp = segment.a.parcel_temp_c + ((segment.b.parcel_temp_c - segment.a.parcel_temp_c) * frac);
    const crossing = { pressure_hpa: crossPressure, temp_c: crossTemp };

    pushPolygon(bucketFor(diffA), [
      { pressure_hpa: segment.a.pressure_hpa, temp_c: segment.a.parcel_temp_c },
      crossing,
      { pressure_hpa: crossPressure, temp_c: crossTemp },
      { pressure_hpa: segment.a.pressure_hpa, temp_c: segment.a.env_temp_c },
    ]);
    pushPolygon(bucketFor(diffB), [
      crossing,
      { pressure_hpa: segment.b.pressure_hpa, temp_c: segment.b.parcel_temp_c },
      { pressure_hpa: segment.b.pressure_hpa, temp_c: segment.b.env_temp_c },
      { pressure_hpa: crossPressure, temp_c: crossTemp },
    ]);
  }

  return results;
}

function normalizeSoundingTopPressure(minPressureHpa) {
  if (!Number.isFinite(minPressureHpa)) return 200;
  if (minPressureHpa <= 100) return 100;
  if (minPressureHpa <= 150) return 150;
  if (minPressureHpa <= 200) return 200;
  if (minPressureHpa <= 250) return 250;
  if (minPressureHpa <= 300) return 300;
  if (minPressureHpa <= 400) return 400;
  return 500;
}

function buildSkewTPressureLevels(pressureTop) {
  const levels = [];
  for (let pressure = 1000; pressure >= pressureTop; pressure -= 100) {
    levels.push(pressure);
  }
  return levels;
}

function buildWindBarbSvg(x, y, windDirDeg, windSpeedKt, shaftLength = 18) {
  if (!Number.isFinite(windDirDeg) || !Number.isFinite(windSpeedKt)) return '';
  // `windDirDeg` is meteorological "from" direction; the barb staff should
  // point into the wind rather than 180 degrees away from it.
  const angle = (windDirDeg * Math.PI) / 180;
  const sin = Math.sin(angle);
  const cos = Math.cos(angle);
  const xTip = x + sin * shaftLength;
  const yTip = y + cos * shaftLength;
  const parts = [
    `<line class="wind-staff" x1="${x.toFixed(1)}" y1="${y.toFixed(1)}" x2="${xTip.toFixed(1)}" y2="${yTip.toFixed(1)}"></line>`,
  ];

  let remaining = Math.max(0, Math.round(windSpeedKt / 5) * 5);
  let fullBarbs = Math.floor((remaining % 50) / 10);
  const halfBarb = (remaining % 10) >= 5;
  const pennants = Math.floor(remaining / 50);
  const nx = sin;
  const ny = cos;
  const px = cos;
  const py = -sin;
  let offset = 0;
  const spacing = 4.1;
  const fullLen = 8.2;
  const halfLen = 4.6;

  for (let i = 0; i < pennants; i += 1) {
    const baseX = xTip - nx * offset;
    const baseY = yTip - ny * offset;
    const nextOffset = offset + spacing;
    const nextX = xTip - nx * nextOffset;
    const nextY = yTip - ny * nextOffset;
    const wingX = baseX + px * fullLen;
    const wingY = baseY + py * fullLen;
    parts.push(`<polygon class="wind-pennant" points="${baseX.toFixed(1)},${baseY.toFixed(1)} ${nextX.toFixed(1)},${nextY.toFixed(1)} ${wingX.toFixed(1)},${wingY.toFixed(1)}"></polygon>`);
    offset += spacing + 1.4;
  }

  for (let i = 0; i < fullBarbs; i += 1) {
    const baseX = xTip - nx * offset;
    const baseY = yTip - ny * offset;
    const wingX = baseX + px * fullLen;
    const wingY = baseY + py * fullLen;
    parts.push(`<line class="wind-staff" x1="${baseX.toFixed(1)}" y1="${baseY.toFixed(1)}" x2="${wingX.toFixed(1)}" y2="${wingY.toFixed(1)}"></line>`);
    offset += spacing;
  }

  if (halfBarb) {
    const baseX = xTip - nx * offset;
    const baseY = yTip - ny * offset;
    const wingX = baseX + px * halfLen;
    const wingY = baseY + py * halfLen;
    parts.push(`<line class="wind-staff" x1="${baseX.toFixed(1)}" y1="${baseY.toFixed(1)}" x2="${wingX.toFixed(1)}" y2="${wingY.toFixed(1)}"></line>`);
  }

  return parts.join('');
}

function renderSoundingChart(data) {
  if (!soundingChart) return;
  const levels = Array.isArray(data?.levels) ? data.levels.filter((row) => Number.isFinite(row.pressure_hpa)) : [];
  const plotData = data?.diagnostics?.plot_data || {};
  const wetbulbProfile = Array.isArray(plotData.wetbulb_profile) ? plotData.wetbulb_profile.filter((row) => Number.isFinite(row?.pressure_hpa) && Number.isFinite(row?.temp_c)) : [];
  const parcelTrace = Array.isArray(plotData.surface_parcel) ? plotData.surface_parcel.filter((row) => Number.isFinite(row?.pressure_hpa) && Number.isFinite(row?.temp_c)) : [];
  if (!levels.length) {
    soundingChart.innerHTML = '';
    return;
  }

  const width = 420;
  const height = 430;
  const margin = { top: 18, right: 72, bottom: 28, left: 38 };
  const plotLeft = margin.left;
  const plotTop = margin.top;
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const pressureBottom = 1000;
  const availablePressures = levels.map((row) => row.pressure_hpa)
    .concat(wetbulbProfile.map((row) => row.pressure_hpa))
    .concat(parcelTrace.map((row) => row.pressure_hpa))
    .filter((value) => Number.isFinite(value));
  const pressureTop = normalizeSoundingTopPressure(Math.min(...availablePressures));
  const skewFactor = 64;
  const tempMin = -50;
  const tempMax = 50;
  const windX = width - 34;
  const yBottom = plotTop + plotHeight;

  const pressureToY = (pressure) => {
    const topLog = Math.log(pressureTop);
    const bottomLog = Math.log(pressureBottom);
    const valueLog = Math.log(Math.max(pressureTop, Math.min(pressureBottom, pressure)));
    return plotTop + ((valueLog - topLog) / (bottomLog - topLog)) * plotHeight;
  };
  const tempToX = (tempC, pressure) => {
    const baseX = plotLeft + ((tempC - tempMin) / (tempMax - tempMin)) * plotWidth;
    const y = pressureToY(pressure);
    const skewOffset = ((yBottom - y) / plotHeight) * skewFactor;
    return baseX + skewOffset;
  };

  const isobars = buildSkewTPressureLevels(pressureTop);
  const isotherms = [];
  for (let t = -40; t <= 40; t += 10) isotherms.push(t);
  const dryAdiabatStarts = [];
  for (let t = -20; t <= 200; t += 40) dryAdiabatStarts.push(t);
  const moistAdiabatStarts = [-10, 10, 30];
  const mixingRatios = [4, 8, 12, 16];

  const parts = [];
  parts.push(`<rect x="0" y="0" width="${width}" height="${height}" rx="10" ry="10" fill="transparent"></rect>`);
  parts.push(`<defs><clipPath id="soundingPlotClip"><rect x="${plotLeft}" y="${plotTop}" width="${plotWidth}" height="${plotHeight}"></rect></clipPath></defs>`);
  for (const pressure of isobars) {
    const y = pressureToY(pressure);
    parts.push(`<line class="grid-line major" x1="${plotLeft}" y1="${y.toFixed(1)}" x2="${plotLeft + plotWidth}" y2="${y.toFixed(1)}"></line>`);
    parts.push(`<text class="level-label" x="${plotLeft - 6}" y="${(y + 3).toFixed(1)}" text-anchor="end">${pressure}</text>`);
  }
  parts.push(`<g clip-path="url(#soundingPlotClip)">`);
  for (const temp of isotherms) {
    const x1 = tempToX(temp, pressureBottom);
    const x2 = tempToX(temp, pressureTop);
    parts.push(`<line class="grid-line iso-therm" x1="${x1.toFixed(1)}" y1="${yBottom.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${plotTop.toFixed(1)}"></line>`);
  }
  for (const startTemp of dryAdiabatStarts) {
    const path = buildThermoPath(
      buildDryAdiabatPoints(startTemp, pressureBottom, pressureTop),
      tempToX,
      pressureToY,
    );
    if (path) parts.push(`<path class="dry-adiabat-line" d="${path}"></path>`);
  }
  for (const startTemp of moistAdiabatStarts) {
    const path = buildThermoPath(
      buildMoistAdiabatPoints(startTemp, pressureBottom, pressureTop),
      tempToX,
      pressureToY,
    );
    if (path) parts.push(`<path class="moist-adiabat-line" d="${path}"></path>`);
  }
  for (const ratio of mixingRatios) {
    const path = buildThermoPath(
      buildMixingRatioPoints(ratio, pressureBottom, 600),
      tempToX,
      pressureToY,
    );
    if (path) parts.push(`<path class="mixing-ratio-line" d="${path}"></path>`);
  }
  const envTrace = levels
    .filter((level) => Number.isFinite(level.temp_c))
    .map((level) => ({ pressure_hpa: level.pressure_hpa, temp_c: level.temp_c }))
    .sort((a, b) => b.pressure_hpa - a.pressure_hpa);
  const shadePaths = buildParcelShadePaths(parcelTrace, envTrace, pressureTop, pressureBottom, tempToX, pressureToY);
  shadePaths.cin.forEach((path) => parts.push(`<path class="cin-fill" d="${path}"></path>`));
  shadePaths.cape.forEach((path) => parts.push(`<path class="cape-fill" d="${path}"></path>`));
  const tempPath = buildSoundingPath(levels, (level) => level.temp_c, tempToX, pressureToY);
  const dewpointPath = buildSoundingPath(levels, (level) => level.dewpoint_c, tempToX, pressureToY);
  const wetbulbPath = buildSoundingPath(wetbulbProfile, (level) => level.temp_c, tempToX, pressureToY);
  const parcelPath = buildSoundingPath(parcelTrace, (level) => level.temp_c, tempToX, pressureToY);
  if (dewpointPath) parts.push(`<path class="dewpoint-line" d="${dewpointPath}"></path>`);
  if (wetbulbPath) parts.push(`<path class="wetbulb-line" d="${wetbulbPath}"></path>`);
  if (tempPath) parts.push(`<path class="temp-line" d="${tempPath}"></path>`);
  if (parcelPath) parts.push(`<path class="parcel-line" d="${parcelPath}"></path>`);
  parts.push(`</g>`);
  for (const temp of isotherms) {
    const x1 = tempToX(temp, pressureBottom);
    parts.push(`<text class="axis-label" x="${x1.toFixed(1)}" y="${(yBottom + 14).toFixed(1)}" text-anchor="middle">${temp}</text>`);
  }
  parts.push(`<text class="axis-label" x="${plotLeft}" y="${(plotTop - 6).toFixed(1)}">mb</text>`);
  parts.push(`<text class="axis-label" x="${windX - 6}" y="${(plotTop - 6).toFixed(1)}" text-anchor="end">wind</text>`);

  for (const level of levels) {
    if (!Number.isFinite(level.wind_speed_kt) || !Number.isFinite(level.wind_dir_deg)) continue;
    const y = pressureToY(level.pressure_hpa);
    parts.push(buildWindBarbSvg(windX, y, level.wind_dir_deg, level.wind_speed_kt));
    parts.push(`<text class="wind-text" x="${(windX - 7).toFixed(1)}" y="${(y + 3).toFixed(1)}" text-anchor="end">${Math.round(level.wind_speed_kt)}</text>`);
  }

  soundingChart.innerHTML = parts.join('');
}

function renderSoundingHodograph(data) {
  if (!soundingHodo) return;
  const levels = Array.isArray(data?.levels) ? data.levels : [];
  const surfaceWind = data?.surface?.wind || null;
  const diagnostics = data?.diagnostics || {};
  const points = [];
  if (surfaceWind && Number.isFinite(surfaceWind.u_kt) && Number.isFinite(surfaceWind.v_kt)) {
    points.push({ height_m: 0, u_kt: surfaceWind.u_kt, v_kt: surfaceWind.v_kt, label: 'SFC' });
  }
  for (const level of levels) {
    if (!Number.isFinite(level?.u_kt) || !Number.isFinite(level?.v_kt) || !Number.isFinite(level?.height_m)) continue;
    points.push({ height_m: level.height_m, u_kt: level.u_kt, v_kt: level.v_kt });
  }
  points.sort((a, b) => a.height_m - b.height_m);
  if (points.length < 2) {
    soundingHodo.innerHTML = '';
    return;
  }

  const motions = [diagnostics.bunkers_right, diagnostics.bunkers_left].filter((wind) => wind && Number.isFinite(wind.u_kt) && Number.isFinite(wind.v_kt));
  const maxComponent = points.concat(motions).reduce((max, point) => Math.max(max, Math.abs(point.u_kt || 0), Math.abs(point.v_kt || 0)), 40);
  const ringMax = Math.max(25, Math.ceil(maxComponent / 20) * 20);
  const width = 240;
  const height = 240;
  const cx = width / 2;
  const cy = height / 2;
  const plotRadius = 88;
  const scale = plotRadius / ringMax;
  const project = (u, v) => ({ x: cx + (u * scale), y: cy - (v * scale) });
  const rings = [];
  for (let speed = 20; speed <= ringMax; speed += 20) rings.push(speed);

  const parts = [];
  for (const speed of rings) {
    const radius = speed * scale;
    parts.push(`<circle class="grid-circle${speed === ringMax ? ' major' : ''}" cx="${cx}" cy="${cy}" r="${radius.toFixed(1)}"></circle>`);
    parts.push(`<text class="ring-label" x="${cx + radius + 4}" y="${cy - 4}">${speed}</text>`);
  }
  parts.push(`<line class="grid-axis" x1="${cx}" y1="${cy - plotRadius}" x2="${cx}" y2="${cy + plotRadius}"></line>`);
  parts.push(`<line class="grid-axis" x1="${cx - plotRadius}" y1="${cy}" x2="${cx + plotRadius}" y2="${cy}"></line>`);
  parts.push(`<text class="axis-label" x="${cx + plotRadius - 4}" y="${cy - 6}" text-anchor="end">+U</text>`);
  parts.push(`<text class="axis-label" x="${cx + 6}" y="${cy - plotRadius + 12}">+V</text>`);

  const path = points.map((point) => {
    const p = project(point.u_kt, point.v_kt);
    return `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
  }).join(' L ');
  parts.push(`<path class="hodo-line" d="M ${path}"></path>`);

  const labelTargets = [
    { target: 0, label: 'SFC' },
    { target: 1000, label: '1 km' },
    { target: 3000, label: '3 km' },
    { target: 6000, label: '6 km' },
    { target: 9000, label: '9 km' },
  ];
  for (const point of points) {
    const p = project(point.u_kt, point.v_kt);
    parts.push(`<circle class="hodo-point" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3.2"></circle>`);
  }
  for (const target of labelTargets) {
    let nearest = null;
    let bestDelta = Infinity;
    for (const point of points) {
      const delta = Math.abs((point.height_m || 0) - target.target);
      if (delta < bestDelta) {
        bestDelta = delta;
        nearest = point;
      }
    }
    if (!nearest || bestDelta > 1750) continue;
    const p = project(nearest.u_kt, nearest.v_kt);
    parts.push(`<text class="height-label" x="${(p.x + 6).toFixed(1)}" y="${(p.y - 6).toFixed(1)}">${escapeHtml(target.label)}</text>`);
  }

  if (diagnostics.bunkers_right && Number.isFinite(diagnostics.bunkers_right.u_kt) && Number.isFinite(diagnostics.bunkers_right.v_kt)) {
    const rm = project(diagnostics.bunkers_right.u_kt, diagnostics.bunkers_right.v_kt);
    parts.push(`<circle class="storm-motion" cx="${rm.x.toFixed(1)}" cy="${rm.y.toFixed(1)}" r="4.3"></circle>`);
    parts.push(`<text class="height-label" x="${(rm.x + 7).toFixed(1)}" y="${(rm.y - 7).toFixed(1)}">RM</text>`);
  }
  if (diagnostics.bunkers_left && Number.isFinite(diagnostics.bunkers_left.u_kt) && Number.isFinite(diagnostics.bunkers_left.v_kt)) {
    const lm = project(diagnostics.bunkers_left.u_kt, diagnostics.bunkers_left.v_kt);
    parts.push(`<circle class="storm-motion" cx="${lm.x.toFixed(1)}" cy="${lm.y.toFixed(1)}" r="4.3"></circle>`);
    parts.push(`<text class="height-label" x="${(lm.x + 7).toFixed(1)}" y="${(lm.y - 7).toFixed(1)}">LM</text>`);
  }

  soundingHodo.innerHTML = parts.join('');
}

function renderSoundingPanel(data, point) {
  if (soundingPanel) soundingPanel.classList.remove('hidden');
  const label = formatLatLonLabel(point);
  const frame = parseFxx(state.currentFxx).display;
  if (soundingPanelTitle) soundingPanelTitle.textContent = label;
  if (soundingPanelMeta) soundingPanelMeta.textContent = `${state.model.toUpperCase()} ${state.runId || '--'} ${frame} · ${data.note || ''}`;
  setSoundingStatus(data.quality === 'sparse' ? 'Sparse profile. This run/frame subset is missing some standard sounding levels.' : '', data.quality === 'sparse');
  renderSoundingChart(data);
  renderSoundingHodograph(data);
  renderSoundingHighlights(data);
  renderSoundingHazards(data);
  renderSoundingSars(data);
  renderSoundingSummary(data);
}

async function loadModelSounding(latlng) {
  if (!latlng || state.dataSource !== 'models' || !state.runId) return;
  state.soundingPoint = { lat: latlng.lat, lng: latlng.lng };
  state.soundingData = null;
  const requestId = ++_soundingRequestSeq;
  if (soundingPanel) soundingPanel.classList.remove('hidden');
  if (soundingPanelTitle) soundingPanelTitle.textContent = formatLatLonLabel(latlng);
  if (soundingPanelMeta) soundingPanelMeta.textContent = `${state.model.toUpperCase()} ${state.runId || '--'} ${parseFxx(state.currentFxx).display}`;
  setSoundingStatus('Loading sounding profile…', true);
  clearSoundingChart();
  try {
    const data = await apiFetch(
      `${BASE}/api/sounding/${encodeURIComponent(state.model)}/${encodeURIComponent(state.runId)}/${encodeURIComponent(state.currentFxx)}?lat=${encodeURIComponent(latlng.lat)}&lon=${encodeURIComponent(latlng.lng)}`
    );
    if (requestId !== _soundingRequestSeq) return;
    state.soundingData = data;
    renderSoundingPanel(data, latlng);
  } catch (err) {
    if (requestId !== _soundingRequestSeq) return;
    if (soundingPanel) soundingPanel.classList.remove('hidden');
    if (soundingPanelTitle) soundingPanelTitle.textContent = formatLatLonLabel(latlng);
    if (soundingPanelMeta) soundingPanelMeta.textContent = `${state.model.toUpperCase()} ${state.runId || '--'} ${parseFxx(state.currentFxx).display}`;
    setSoundingStatus('No sounding profile was available for this model frame. The cached subset likely does not contain enough vertical fields yet.', true);
    clearSoundingChart();
  }
}

function refreshSoundingForCurrentPoint() {
  if (!state.soundingPoint || state.dataSource !== 'models' || soundingPanel?.classList.contains('hidden')) return;
  loadModelSounding({ lat: state.soundingPoint.lat, lng: state.soundingPoint.lng });
}

// ---------------------------------------------------------------------------
// Time helpers
// ---------------------------------------------------------------------------
// centralTime/parseIsoDate/formatMinutesAgo/formatMinutesLeft come

// from js/core/format.js via js/main.js.



// escapeHtml comes from js/core/format.js via js/main.js.

// assetPath comes from js/core/api.js via js/main.js.

function attachImageFallbacks(scope = document) {
  scope.querySelectorAll('img').forEach((img) => {
    if (img.dataset.fallbackBound === '1') return;
    img.dataset.fallbackBound = '1';
    img.addEventListener('error', () => {
      img.style.display = 'none';
      const card = img.closest('.home-preview-card, .home-feature-frame');
      if (card) card.classList.add('image-missing');
    });
  });
}

function buildCaptureMetadata() {
  if (state.dataSource === 'models' || state.dataSource === 'chase') {
    const chase = state.dataSource === 'chase';
    const selection = chase ? chaseState : state;
    const parameter = chase ? selection.param : selection.parameter;
    const product = chase
      ? document.querySelector(`.chase-product-btn[data-chase-param="${parameter}"] .chase-product-name`)?.textContent?.trim()
      : document.getElementById('legendLabel')?.textContent?.trim();
    return window.MapCaptureMetadata.model({
      model: selection.model, product: product || parameter,
      runId: selection.runId, fxx: selection.currentFxx,
    });
  }
  const modelText = document.getElementById('mapInfoModel')?.textContent?.trim() || '';
  const productText = document.getElementById('mapInfoProduct')?.textContent?.trim() || '';
  let timeText;
  if (state.dataSource === 'satellite') {
    // validTimeDisplay is updated on every frame scrub; mapInfoTime is only set at load
    const vt = document.getElementById('validTimeDisplay')?.textContent?.trim() || '';
    timeText = vt.replace(/^Satellite\s+/i, '')
      || document.getElementById('mapInfoTime')?.textContent?.trim()
      || '';
  } else {
    timeText = document.getElementById('mapInfoTime')?.textContent?.trim()
      || document.getElementById('validTimeDisplay')?.textContent?.trim()
      || '';
  }
  const parts = [modelText, productText, timeText].filter(Boolean);
  return parts.join(' | ');
}

function buildCaptureFilename() {
  if (state.dataSource === 'radar') {
    return `wx-viewer-radar-${state.radarSite}-${state.radarProduct}-${Date.now()}.png`;
  }
  if (state.dataSource === 'satellite') {
    return `wx-viewer-satellite-${state.satellite}-${state.satProduct}-${Date.now()}.png`;
  }
  return `wx-viewer-${state.model}-${state.parameter}-${Date.now()}.png`;
}

function stampCaptureMetadata(canvas, text, bottomOffset = 18) {
  const width = document.querySelector('.map-section')?.clientWidth || canvas.width;
  return window.MapCaptureMetadata.stamp(canvas, text, { scale: canvas.width / width, bottomOffset });
}

// Play/pause button icons (inline SVG, matching the other hand-tuned icons).
const PLAY_ICON_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
const PAUSE_ICON_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------
function showToast(message, type = 'info', timeoutMs = 5000) {
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    container.id = 'toastContainer';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  toast.className = `toast toast--${type === 'error' ? 'error' : 'info'}`;
  toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
  toast.textContent = message;
  let dismissed = false;
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    toast.classList.add('is-leaving');
    setTimeout(() => toast.remove(), 220);
  };
  toast.addEventListener('click', dismiss);
  container.appendChild(toast);
  if (timeoutMs > 0) setTimeout(dismiss, timeoutMs);
}

// html2canvas is only needed for screenshot capture; load it lazily on first use.
let html2canvasPromise = null;
function loadHtml2Canvas() {
  if (typeof window.html2canvas !== 'undefined') return Promise.resolve(window.html2canvas);
  if (!html2canvasPromise) {
    html2canvasPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/static/vendor/html2canvas/html2canvas.min.js?v=1.4.1';
      script.onload = () => resolve(window.html2canvas);
      script.onerror = () => {
        html2canvasPromise = null;
        showToast('Screenshot library failed to load', 'error');
        reject(new Error('Failed to load html2canvas'));
      };
      document.head.appendChild(script);
    });
  }
  return html2canvasPromise;
}

async function captureMapSectionCanvas({ scale, hidden = [], transparent = false, stamp = true } = {}) {
  const mapSectionEl = document.querySelector('.map-section');
  if (!mapSectionEl) throw new Error('Map section not found');
  beginMapLoading('Loading screenshot library…');
  try {
    await loadHtml2Canvas();
  } finally {
    endMapLoading();
  }
  if (state.overlayLayer?.isPressureContours) await state.overlayLayer._loading;
  const cleanupCaptureOverlay = prepareMapCaptureOverlays();
  // Loop capture renders the map in layers: hide some, or drop backgrounds.
  const restoreVisibility = hidden.filter(Boolean).map((el) => {
    const previous = el.style.visibility;
    el.style.visibility = 'hidden';
    return () => { el.style.visibility = previous; };
  });
  const clearedBackgrounds = transparent
    ? [mapSectionEl, leafletMap?.getContainer?.()].filter(Boolean).map((el) => {
      const previous = el.style.background;
      el.style.background = 'transparent';
      return () => { el.style.background = previous; };
    })
    : [];
  try {
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const metadata = buildCaptureMetadata();
    const canvas = await html2canvas(mapSectionEl, {
      useCORS: true,
      allowTaint: true,
      logging: false,
      ...(scale ? { scale } : {}),
      ...(transparent ? { backgroundColor: null } : {}),
      ignoreElements: (el) =>
        el?.id === 'scrubberBar' ||
        el?.id === 'mapLoadingOverlay' ||
        el?.classList?.contains('v4-readout') ||
        el?.classList?.contains('v4-alerts') ||
        el?.classList?.contains('leaflet-control-container') ||
        el?.classList?.contains('leaflet-control-zoom') ||
        el?.classList?.contains('leaflet-control-attribution'),
    });
    if (!stamp) return canvas;
    return stampCaptureMetadata(canvas, metadata, captureStampOffset());
  } finally {
    [...restoreVisibility, ...clearedBackgrounds].forEach((restore) => restore());
    cleanupCaptureOverlay?.();
  }
}

function captureStampOffset() {
  return state.dataSource === 'radar' ? 52 : 2;
}

function forEachLatLngPath(latlngs, visitor) {
  if (!Array.isArray(latlngs) || latlngs.length === 0) return;
  if (latlngs[0] && typeof latlngs[0].lat === 'number' && typeof latlngs[0].lng === 'number') {
    visitor(latlngs);
    return;
  }
  latlngs.forEach((part) => forEachLatLngPath(part, visitor));
}

function drawCaptureVectorLayer(ctx, layer) {
  if (!layer || !leafletMap || !leafletMap.hasLayer(layer)) return;
  if (typeof layer.eachLayer === 'function' && typeof layer.getLatLngs !== 'function') {
    layer.eachLayer((child) => drawCaptureVectorLayer(ctx, child));
    return;
  }
  const latlngs = layer.getLatLngs?.();
  if (!latlngs) return;
  const opts = layer.options || {};
  const stroke = opts.stroke !== false && !!opts.color && (opts.weight || 0) > 0;
  const fill = !!opts.fill && !!opts.fillColor && (opts.fillOpacity || 0) > 0;
  if (!stroke && !fill) return;

  forEachLatLngPath(latlngs, (path) => {
    if (!Array.isArray(path) || path.length < 2) return;
    ctx.beginPath();
    path.forEach((latlng, index) => {
      const pt = leafletMap.latLngToContainerPoint(latlng);
      if (index === 0) ctx.moveTo(pt.x, pt.y);
      else ctx.lineTo(pt.x, pt.y);
    });
    if (fill && path.length >= 3) {
      ctx.closePath();
      ctx.fillStyle = opts.fillColor;
      ctx.globalAlpha = opts.fillOpacity ?? 1;
      ctx.fill();
    }
    if (stroke) {
      ctx.strokeStyle = opts.color;
      ctx.lineWidth = opts.weight ?? 1;
      ctx.lineJoin = opts.lineJoin || 'round';
      ctx.lineCap = opts.lineCap || 'round';
      ctx.globalAlpha = opts.opacity ?? 1;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  });
}

/** Rasterize Leaflet vectors for export without clipping their SVG viewports. */
function prepareMapCaptureOverlays() {
  if (!leafletMap) return null;
  const mapEl = leafletMap.getContainer?.();
  if (!mapEl) return null;

  const width = mapEl.clientWidth;
  const height = mapEl.clientHeight;
  if (!width || !height) return null;

  const ratio = window.devicePixelRatio || 1;
  const origin = leafletMap.containerPointToLayerPoint([0, 0]);
  const cleanups = [];
  const pressureCleanup = state.overlayLayer?.isPressureContours ? state.overlayLayer.prepareCapture() : null;
  if (pressureCleanup) cleanups.push(pressureCleanup);
  const groups = [
    {
      paneName: 'modelCanvasPane',
      layers: [window._modelCanvasBackingLayer],
    },
    {
      paneName: 'boundaryPane',
      layers: [
        window._modelFrameShadowLayer,
        window._modelFrameOuterBorderLayer,
        window._modelFrameBorderLayer,
        window._countryBoundaryOutlineLayer,
        window._countryBoundaryLayer,
        window._stateBoundaryOutlineLayer,
        window._stateBoundaryLayer,
        window._coastlineBoundaryOutlineLayer,
        window._coastlineBoundaryLayer,
        window._lakesBoundaryLayer,
        window._roadsBoundaryLayer,
        window._countyBoundaryLayer,
      ],
    },
  ];

  for (const { paneName, layers } of groups) {
    const pane = leafletMap.getPane?.(paneName);
    if (!pane || !layers.some((layer) => layer && leafletMap.hasLayer(layer))) continue;

    const canvas = document.createElement('canvas');
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    canvas.style.position = 'absolute';
    // Cancel the map pane's pan offset while retaining the pane's stacking order.
    canvas.style.left = `${origin.x}px`;
    canvas.style.top = `${origin.y}px`;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.style.pointerEvents = 'none';

    const ctx = canvas.getContext('2d');
    if (!ctx) continue;
    ctx.scale(ratio, ratio);
    layers.forEach((layer) => drawCaptureVectorLayer(ctx, layer));

    const originals = [...pane.children].map((child) => [child, child.style.visibility]);
    originals.forEach(([child]) => { child.style.visibility = 'hidden'; });
    pane.appendChild(canvas);
    cleanups.push(() => {
      originals.forEach(([child, visibility]) => { child.style.visibility = visibility; });
      canvas.remove();
    });
  }

  return () => {
    cleanups.forEach((cleanup) => cleanup());
  };
}

function buildCaptureFilename() {
  const source = state.dataSource || 'map';
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  return `wxviewer_${source}_${timestamp}.png`;
}

function buildSoundingCaptureFilename() {
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const model = (state.model || 'model').toLowerCase();
  const run = state.runId || 'run';
  const frame = parseFxx(state.currentFxx).display.replace(/\s+/g, '').toLowerCase();
  const point = state.soundingPoint
    ? `${Math.abs(state.soundingPoint.lat).toFixed(2)}${state.soundingPoint.lat >= 0 ? 'n' : 's'}_${Math.abs(state.soundingPoint.lng).toFixed(2)}${state.soundingPoint.lng >= 0 ? 'e' : 'w'}`
    : 'point';
  return `wxviewer_sounding_${model}_${run}_${frame}_${point}_${timestamp}.png`;
}

async function canvasToBlob(canvas) {
  return await new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error('Failed to encode screenshot'));
    }, 'image/png');
  });
}

async function downloadMapScreenshot() {
  const canvas = await captureMapSectionCanvas();
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

// ── Loop capture (animated GIF) ──────────────────────────────────────────────
const LOOP_CAPTURE_MAX_FRAMES = 60;   // satellite archives can hold hundreds
const LOOP_CAPTURE_MAX_WIDTH = 960;   // keeps GIFs shareable
const LOOP_CAPTURE_HOLD_MS = 1200;    // pause on the last frame before repeating
let _loopCapture = null;              // { cancelled } while a capture runs

/** Frame delay for the GIF: model hours step slower than observations. */
function loopCaptureFrameDelay() {
  return (state.dataSource === 'models' || state.dataSource === 'chase') ? 450 : 140;
}

const CANVAS_BLEND_MODES = { multiply: 'multiply', overlay: 'overlay', screen: 'screen' };

/**
 * Fast loop frames: when only one weather image changes per frame, capture
 * the map below and above it once, then each frame is just below + image +
 * above + time stamp. Returns null when the view needs a full capture per
 * frame (pressure contours, Home, Chase).
 */
async function buildLoopCompositor(scale) {
  if (!['models', 'satellite', 'radar'].includes(state.dataSource)) return null;
  if (state.overlayLayer?.isPressureContours && state.dataSource === 'models') return null;
  const frameImg = getActiveOverlayImage();
  const framePane = frameImg?.closest?.('.leaflet-pane');
  const mapPane = leafletMap?.getPane?.('mapPane');
  const mapSectionEl = document.querySelector('.map-section');
  if (!frameImg || frameImg.tagName !== 'IMG' || !framePane || !mapPane || !mapSectionEl) return null;
  const zOf = (pane) => Number.parseInt(getComputedStyle(pane).zIndex, 10) || 0;
  const frameZ = zOf(framePane);
  const panes = [...mapPane.children].filter((el) => el.classList.contains('leaflet-pane'));
  const belowHidden = [
    ...panes.filter((pane) => pane === framePane || zOf(pane) >= frameZ),
    ...[...mapSectionEl.children].filter((el) => el.id !== 'map'),
  ];
  const aboveHidden = panes.filter((pane) => pane === framePane || zOf(pane) <= frameZ);
  const below = await captureMapSectionCanvas({ scale, hidden: belowHidden, stamp: false });
  const above = await captureMapSectionCanvas({ scale, hidden: aboveHidden, transparent: true, stamp: false });
  const sectionRect = mapSectionEl.getBoundingClientRect();
  const ratio = below.width / sectionRect.width;
  return function composeFrame() {
    const img = getActiveOverlayImage();
    const out = document.createElement('canvas');
    out.width = below.width;
    out.height = below.height;
    const ctx = out.getContext('2d');
    ctx.drawImage(below, 0, 0);
    if (img?.complete && img.naturalWidth) {
      const rect = img.getBoundingClientRect();
      const style = getComputedStyle(img);
      ctx.save();
      // The layer's own opacity, not a mid-transition value.
      ctx.globalAlpha = Number.parseFloat(img.style.opacity || '1');
      ctx.globalCompositeOperation = CANVAS_BLEND_MODES[style.mixBlendMode] || 'source-over';
      if (style.filter && style.filter !== 'none' && 'filter' in ctx) ctx.filter = style.filter;
      ctx.imageSmoothingEnabled = !/pixelated|crisp-edges/.test(style.imageRendering);
      ctx.drawImage(img, (rect.left - sectionRect.left) * ratio, (rect.top - sectionRect.top) * ratio,
        rect.width * ratio, rect.height * ratio);
      ctx.restore();
    }
    ctx.drawImage(above, 0, 0);
    return stampCaptureMetadata(out, buildCaptureMetadata(), captureStampOffset());
  };
}

/** Wait until the map's image overlays (and pressure contours) have loaded. */
async function waitForMapImages(timeoutMs = 10000) {
  const deadline = performance.now() + timeoutMs;
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  while (performance.now() < deadline) {
    if (state.overlayLayer?.isPressureContours) {
      try { await state.overlayLayer._loading; } catch { /* drawn as far as it got */ }
    }
    const mapEl = leafletMap?.getContainer?.();
    const images = mapEl ? [...mapEl.querySelectorAll('.leaflet-overlay-pane img, img.leaflet-image-layer')] : [];
    if (images.every((img) => img.complete)) return;
    await new Promise((resolve) => setTimeout(resolve, 80));
  }
}

function setLoopCaptureStatus(text) {
  const btn = document.getElementById('loopCaptureBtn');
  let pill = document.getElementById('loopCaptureStatus');
  if (!text) {
    pill?.remove();
    btn?.classList.remove('active');
    btn?.setAttribute('aria-label', 'Download loop as GIF');
    if (btn) btn.title = 'Download loop as GIF';
    return;
  }
  if (!pill) {
    pill = document.createElement('div');
    pill.id = 'loopCaptureStatus';
    pill.className = 'loop-capture-status';
    pill.setAttribute('role', 'status');
    document.body.appendChild(pill);
  }
  pill.textContent = text;
  btn?.classList.add('active');
  btn?.setAttribute('aria-label', 'Cancel loop capture');
  if (btn) btn.title = 'Cancel loop capture';
}

/**
 * Step through the loop, capture each frame like the screenshot does, and
 * download the frames as an animated GIF. Clicking again cancels.
 */
async function downloadLoopGif() {
  if (_loopCapture) {
    _loopCapture.cancelled = true;
    return;
  }
  const total = (parseInt(scrubber.max, 10) || 0) + 1;
  if (scrubber.disabled || total < 2) {
    showToast('This view has no loop to capture.');
    return;
  }
  const job = { cancelled: false };
  _loopCapture = job;
  const startIdx = parseInt(scrubber.value, 10) || 0;
  const wasPlaying = !!state.isPlaying;
  if (wasPlaying) pause();
  // Observations keep their newest frames; forecasts start at the first hour.
  const observed = ['satellite', 'radar', 'home'].includes(state.dataSource);
  const count = Math.min(total, LOOP_CAPTURE_MAX_FRAMES);
  const first = observed ? total - count : 0;
  const goTo = (idx) => {
    scrubber.value = String(idx);
    scrubber.dispatchEvent(new Event('input', { bubbles: true }));
  };
  try {
    setLoopCaptureStatus('Preparing loop capture…');
    const { GIFEncoder, quantize, applyPalette } = await import('/static/vendor/gifenc/gifenc.esm.js?v=1.0.3');
    const gif = GIFEncoder();
    const scratch = document.createElement('canvas');
    const ctx = scratch.getContext('2d', { willReadFrequently: true });
    const delay = loopCaptureFrameDelay();
    const sectionWidth = document.querySelector('.map-section')?.clientWidth || LOOP_CAPTURE_MAX_WIDTH;
    const scale = Math.min(window.devicePixelRatio || 1, LOOP_CAPTURE_MAX_WIDTH / sectionWidth);
    goTo(first);
    await waitForMapImages();
    const compose = await buildLoopCompositor(scale);
    for (let n = 0; n < count; n++) {
      if (job.cancelled) break;
      setLoopCaptureStatus(`Capturing loop ${n + 1}/${count} · click the loop button to cancel`);
      goTo(first + n);
      await waitForMapImages();
      const frame = compose ? compose() : await captureMapSectionCanvas({ scale });
      if (job.cancelled) break;
      const scale = Math.min(1, LOOP_CAPTURE_MAX_WIDTH / frame.width);
      scratch.width = Math.round(frame.width * scale);
      scratch.height = Math.round(frame.height * scale);
      ctx.drawImage(frame, 0, 0, scratch.width, scratch.height);
      const { data, width, height } = ctx.getImageData(0, 0, scratch.width, scratch.height);
      const palette = quantize(data, 256);
      gif.writeFrame(applyPalette(data, palette), width, height, {
        palette,
        delay: n === count - 1 ? LOOP_CAPTURE_HOLD_MS : delay,
      });
    }
    if (job.cancelled) {
      showToast('Loop capture cancelled.');
      return;
    }
    setLoopCaptureStatus('Saving GIF…');
    gif.finish();
    const blob = new Blob([gif.bytes()], { type: 'image/gif' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = buildCaptureFilename().replace(/\.png$/, '-loop.gif');
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast(`Loop saved: ${count} frames, ${(blob.size / 1048576).toFixed(1)} MB.`);
  } catch (err) {
    console.error('Loop capture failed:', err);
    showToast('Loop capture failed. Try again.', 'error');
  } finally {
    _loopCapture = null;
    setLoopCaptureStatus('');
    goTo(Math.min(startIdx, total - 1));
    if (wasPlaying && !state.isPlaying) play();
  }
}

async function copyMapScreenshot() {
  const canvas = await captureMapSectionCanvas();
  const blob = await canvasToBlob(canvas);
  if (!navigator.clipboard || typeof ClipboardItem === 'undefined') {
    throw new Error('Clipboard image copy is not supported in this browser');
  }
  await navigator.clipboard.write([
    new ClipboardItem({ 'image/png': blob }),
  ]);
}

async function captureSoundingPanelCanvas() {
  if (!soundingPanel || soundingPanel.classList.contains('hidden')) {
    throw new Error('Sounding panel is not open');
  }
  await loadHtml2Canvas();
  const rect = soundingPanel.getBoundingClientRect();
  const clone = soundingPanel.cloneNode(true);
  clone.id = 'soundingPanelCaptureClone';
  clone.classList.remove('hidden');
  clone.style.position = 'fixed';
  clone.style.left = '-10000px';
  clone.style.top = '0';
  clone.style.right = 'auto';
  clone.style.bottom = 'auto';
  clone.style.width = `${Math.ceil(rect.width)}px`;
  clone.style.maxHeight = 'none';
  clone.style.height = 'auto';
  clone.style.overflow = 'visible';
  clone.style.zIndex = '99999';
  document.body.appendChild(clone);
  try {
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    return await html2canvas(clone, {
      useCORS: true,
      allowTaint: true,
      backgroundColor: null,
      width: Math.ceil(clone.scrollWidth || rect.width),
      height: Math.ceil(clone.scrollHeight || clone.offsetHeight),
      ignoreElements: (el) =>
        el?.classList?.contains('sounding-export-btn') ||
        el?.id === 'soundingPanelClose' ||
        el?.id === 'soundingPanelDownload' ||
        el?.id === 'soundingPanelCopy',
    });
  } finally {
    clone.remove();
  }
}

async function downloadSoundingScreenshot() {
  const canvas = await captureSoundingPanelCanvas();
  const blob = await canvasToBlob(canvas);
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = buildSoundingCaptureFilename();
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function copySoundingScreenshot() {
  const canvas = await captureSoundingPanelCanvas();
  const blob = await canvasToBlob(canvas);
  if (!navigator.clipboard || typeof ClipboardItem === 'undefined') {
    throw new Error('Clipboard image copy is not supported in this browser');
  }
  await navigator.clipboard.write([
    new ClipboardItem({ 'image/png': blob }),
  ]);
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
// apiFetch comes from js/core/api.js via js/main.js.

const VIEW_STORAGE_KEY = 'wx_viewer_active_view';
let shareableUrlSyncTimer = null;

function currentShareableFrame() {
  if (state.dataSource === 'satellite') {
    return state.satFrames[state.satFrameIdx]?.timestamp || null;
  }
  if (state.dataSource === 'radar') {
    return state.radarFrames[state.radarFrameIdx]?.timestamp || null;
  }
  return null;
}

function buildShareableDesktopUrl() {
  const url = new URL(window.location.href);
  for (const key of [...SHAREABLE_QUERY_KEYS, 'desktop']) url.searchParams.delete(key);
  url.searchParams.set('view', state.dataSource || 'home');

  if (state.dataSource === 'models') {
    if (state.model) url.searchParams.set('model', state.model);
    if (state.runId) url.searchParams.set('run', state.runId);
    if (state.parameter) url.searchParams.set('param', state.parameter);
    if (state.currentFxx !== null && state.currentFxx !== undefined) {
      url.searchParams.set('fxx', String(state.currentFxx));
    }
  } else if (state.dataSource === 'chase') {
    if (chaseState.model) url.searchParams.set('model', chaseState.model);
    if (chaseState.runId) url.searchParams.set('run', chaseState.runId);
    if (chaseState.param) url.searchParams.set('param', chaseState.param);
    if (chaseState.currentFxx !== null && chaseState.currentFxx !== undefined) {
      url.searchParams.set('fxx', String(chaseState.currentFxx));
    }
  } else if (state.dataSource === 'satellite') {
    if (state.satellite) url.searchParams.set('satellite', state.satellite);
    if (state.satProduct) url.searchParams.set('sat_product', state.satProduct);
    if (state.satRegion) url.searchParams.set('sat_region', state.satRegion);
  } else if (state.dataSource === 'radar') {
    if (state.radarSite) url.searchParams.set('site', state.radarSite);
    if (state.radarProduct) url.searchParams.set('product', state.radarProduct);
    if (Number.isFinite(state.radarTilt)) url.searchParams.set('tilt', String(state.radarTilt));
  }

  const frame = currentShareableFrame();
  if (frame) url.searchParams.set('frame', frame);

  if (leafletMap && ['home', 'radar', 'surface', 'chase'].includes(state.dataSource)) {
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

function syncShareableDesktopUrl() {
  if (CAPTURE_MODE) return;
  if (shareableUrlSyncTimer) clearTimeout(shareableUrlSyncTimer);
  shareableUrlSyncTimer = setTimeout(() => {
    shareableUrlSyncTimer = null;
    const url = buildShareableDesktopUrl();
    const target = `${url.pathname}${url.search}${url.hash}`;
    const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (target !== current) history.replaceState(null, '', target);
  }, 120);
}

function restoreSharedMapView(source) {
  if (!leafletMap || !sharedMapViewPending || SHARED_VIEW_CONFIG?.view !== source) return false;
  const { lat, lon, zoom } = sharedMapViewPending;
  sharedMapViewPending = null;
  leafletMap.setView([lat, lon], zoom, { animate: false });
  return true;
}

function persistActiveView(source) {
  try {
    localStorage.setItem(VIEW_STORAGE_KEY, source);
  } catch {}
  syncShareableDesktopUrl();
}

function getStoredActiveView() {
  if (CAPTURE_MODE) return null;
  try {
    const view = localStorage.getItem(VIEW_STORAGE_KEY);
    return ['home', 'models', 'satellite', 'radar', 'chase', 'surface'].includes(view) ? view : null;
  } catch {
    return null;
  }
}

function getInitialView() {
  if (CAPTURE_MODE) {
    return getCaptureConfig()?.source || 'models';
  }
  if (SHARED_VIEW_CONFIG?.view) return SHARED_VIEW_CONFIG.view;
  const hashView = (window.location.hash || '').replace('#', '');
  if (['home', 'models', 'satellite', 'radar', 'chase', 'surface'].includes(hashView)) {
    return hashView;
  }
  return 'home';
}

function getPanelOcclusionWidth(panelEl, mapRect, side) {
  if (!panelEl || !mapRect) return 0;
  const rect = panelEl.getBoundingClientRect();
  if (!rect.width || !rect.height) return 0;
  const overlapTop = Math.max(rect.top, mapRect.top);
  const overlapBottom = Math.min(rect.bottom, mapRect.bottom);
  if (overlapBottom <= overlapTop) return 0;
  if (side === 'left') {
    return Math.max(0, Math.min(rect.right, mapRect.right) - mapRect.left);
  }
  return Math.max(0, mapRect.right - Math.max(rect.left, mapRect.left));
}

function getUsaViewportPadding() {
  const padding = {
    topLeft: L.point(16, 16),
    bottomRight: L.point(16, 16),
  };
  if (state.dataSource !== 'home' || !leafletMap) return padding;
  const dashboard = document.getElementById('homeDashboard');
  const mapEl = leafletMap.getContainer();
  if (!dashboard || dashboard.classList.contains('hidden') || !mapEl) return padding;

  const mapRect = mapEl.getBoundingClientRect();
  const leftOcclusion = getPanelOcclusionWidth(homeLayerPanel, mapRect, 'left');
  const rightOcclusion = getPanelOcclusionWidth(homeSidePanel, mapRect, 'right');
  const leftPadding = 16 + leftOcclusion + 24;
  const rightPadding = 16 + rightOcclusion + 24;
  padding.topLeft = L.point(leftPadding, 16);
  padding.bottomRight = L.point(rightPadding, 16);
  return padding;
}

function applyUsaViewport({ animate = false } = {}) {
  if (!leafletMap || !usaBounds) return;
  leafletMap.setMinZoom(defaultMinZoom);
  const padding = getUsaViewportPadding();
  leafletMap.fitBounds(usaBounds, {
    animate,
    duration: animate ? 0.6 : undefined,
    paddingTopLeft: padding.topLeft,
    paddingBottomRight: padding.bottomRight,
  });
  leafletMap.setMinZoom(leafletMap.getZoom());
}

function scheduleUsaViewportRefresh(animate = true) {
  if (!leafletMap || !usaBounds) return;
  if (usaViewportTimer) clearTimeout(usaViewportTimer);
  requestAnimationFrame(() => applyUsaViewport({ animate }));
  usaViewportTimer = setTimeout(() => {
    applyUsaViewport({ animate: false });
  }, 280);
}

function scheduleLeafletResize() {
  if (!leafletMap) return;
  requestAnimationFrame(() => {
    leafletMap.invalidateSize(false);
  });
  setTimeout(() => {
    if (leafletMap) leafletMap.invalidateSize(false);
  }, 180);
}

function scheduleActiveSourceReflow() {
  if (!leafletMap) return;
  scheduleLeafletResize();
  if (shouldUseSatelliteImageViewer()) {
    applySatelliteImageViewerLayout();
  }
  if (activeSourceReflowTimer) {
    clearTimeout(activeSourceReflowTimer);
  }
  activeSourceReflowTimer = setTimeout(() => {
    activeSourceReflowTimer = null;
    reflowActiveMapSource();
  }, 260);
}

function geometryBoundsIntersect(geometry, bbox) {
  if (!geometry || !geometry.coordinates) return false;
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;

  const walk = (coords) => {
    if (!Array.isArray(coords) || coords.length === 0) return;
    if (typeof coords[0] === 'number' && typeof coords[1] === 'number') {
      const [lon, lat] = coords;
      if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;
      minLon = Math.min(minLon, lon);
      minLat = Math.min(minLat, lat);
      maxLon = Math.max(maxLon, lon);
      maxLat = Math.max(maxLat, lat);
      return;
    }
    coords.forEach(walk);
  };

  walk(geometry.coordinates);
  if (!Number.isFinite(minLon)) return false;

  return !(
    maxLon < bbox.west ||
    minLon > bbox.east ||
    maxLat < bbox.south ||
    minLat > bbox.north
  );
}

function filterGeoJSONToBounds(geojson, bbox) {
  if (!geojson || !Array.isArray(geojson.features)) return geojson;
  return {
    ...geojson,
    features: geojson.features.filter((feature) => geometryBoundsIntersect(feature?.geometry, bbox)),
  };
}

function syncScrubberVisibility() {
  if (!scrubberBar) return;
  const visible = (state.dataSource !== 'home' && state.dataSource !== 'surface')
    || (state.radarCompositeEnabled && state.homeRadarFrames.length > 0);
  if (state.dataSource !== 'satellite' && state.playSpeed === 10) {
    setPlaySpeed(5);
  }
  scrubberBar.style.display = visible ? 'flex' : 'none';
  scrubberBar.dataset.mode = state.dataSource || 'home';
}

function syncSurfaceObsMode(source = state.dataSource) {
  if (typeof SurfaceObs === 'undefined') return;
  const showOnRadar = Boolean(surfaceRadarToggle?.checked) && source === 'radar';
  if (source === 'surface') {
    SurfaceObs.show();
    return;
  }
  if (showOnRadar) {
    SurfaceObs.setRadarOverlay(true);
    return;
  }
  SurfaceObs.hide();
}

function ensureScrubberDebug() {
  if (!DEBUG_SCRUBBER || scrubberDebugPanel) return;
  scrubberDebugPanel = document.createElement('textarea');
  scrubberDebugPanel.id = 'scrubberDebugPanel';
  scrubberDebugPanel.readOnly = true;
  scrubberDebugPanel.style.cssText = [
    'position:fixed',
    'top:44px',
    'right:8px',
    'z-index:2000',
    'max-width:420px',
    'max-height:50vh',
    'overflow:auto',
    'padding:10px 12px',
    'margin:0',
    'background:rgba(7,10,16,0.9)',
    'color:#dbe8ff',
    'border:1px solid rgba(140,180,230,0.4)',
    'border-radius:10px',
    'font:12px/1.35 monospace',
    'white-space:pre',
    'pointer-events:auto',
    'width:min(420px, calc(100vw - 16px))',
    'height:min(50vh, 420px)',
    'resize:none',
  ].join(';');
  scrubberDebugCopyBtn = document.createElement('button');
  scrubberDebugCopyBtn.type = 'button';
  scrubberDebugCopyBtn.textContent = 'Copy debug';
  scrubberDebugCopyBtn.style.cssText = [
    'position:fixed',
    'top:8px',
    'right:8px',
    'z-index:2001',
    'padding:6px 10px',
    'background:rgba(7,10,16,0.95)',
    'color:#dbe8ff',
    'border:1px solid rgba(140,180,230,0.4)',
    'border-radius:8px',
    'font:12px/1 monospace',
    'cursor:pointer',
  ].join(';');
  scrubberDebugCopyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(scrubberDebugPanel.value || '');
      scrubberDebugCopyBtn.textContent = 'Copied';
      setTimeout(() => {
        if (scrubberDebugCopyBtn) scrubberDebugCopyBtn.textContent = 'Copy debug';
      }, 1200);
    } catch {
      scrubberDebugPanel.focus();
      scrubberDebugPanel.select();
    }
  });
  document.body.appendChild(scrubberDebugCopyBtn);
  document.body.appendChild(scrubberDebugPanel);

  const mutationLog = [];
  const pushDebugEvent = (message) => {
    scrubberDebugEvents.unshift(`${new Date().toISOString().slice(11, 19)} ${message}`);
    scrubberDebugEvents = scrubberDebugEvents.slice(0, 10);
  };
  window.__wxPushDebugEvent = pushDebugEvent;
  if (!scrubberDebugHistoryHooked) {
    scrubberDebugHistoryHooked = true;
    const originalReplaceState = history.replaceState.bind(history);
    history.replaceState = function (...args) {
      pushDebugEvent(`history.replaceState -> ${String(args[2] ?? '-')}`);
      return originalReplaceState(...args);
    };
    const originalPushState = history.pushState.bind(history);
    history.pushState = function (...args) {
      pushDebugEvent(`history.pushState -> ${String(args[2] ?? '-')}`);
      return originalPushState(...args);
    };
  }
  const watched = () => [
    ['scrubber', scrubberBar],
    ['legend', document.getElementById('mapLegend')],
    ['logo', document.querySelector('.map-logo')],
    ['homeDashboard', document.getElementById('homeDashboard')],
    ['html', document.documentElement],
    ['body', document.body],
  ];
  const summarize = (label, el) => {
    if (!el) return `${label}: missing`;
    const style = window.getComputedStyle(el);
    return [
      `${label}: display=${style.display}`,
      `visibility=${style.visibility}`,
      `opacity=${style.opacity}`,
      `z=${style.zIndex}`,
      `classes=${el.className || '-'}`,
      `style="${el.getAttribute('style') || ''}"`,
    ].join(' | ');
  };
  const render = () => {
    const lines = [
      `source=${state.dataSource} hash=${window.location.hash || '-'} capture=${document.documentElement.dataset.captureMode || '0'}`,
      ...watched().map(([label, el]) => summarize(label, el)),
      'events:',
      ...(scrubberDebugEvents.length ? scrubberDebugEvents : ['(none)']),
      'mutations:',
      ...(mutationLog.length ? mutationLog : ['(none)']),
    ];
    scrubberDebugPanel.value = lines.join('\n');
  };

  scrubberDebugObserver = new MutationObserver((records) => {
    for (const record of records.slice(-10)) {
      const target = record.target;
      const id = target.id ? `#${target.id}` : target.className ? `.${String(target.className).split(' ')[0]}` : target.tagName;
      mutationLog.unshift(`${new Date().toISOString().slice(11, 19)} ${id} ${record.attributeName}="${target.getAttribute(record.attributeName) || ''}"`);
    }
    mutationLog.splice(12);
    render();
  });
  scrubberDebugObserver.observe(document.documentElement, { subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden', 'data-capture-mode'] });
  scrubberDebugObserver.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden'] });
  scrubberDebugTimer = setInterval(render, 250);
  render();
}

// ---------------------------------------------------------------------------
// Source Tab Switching
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Storm Chasing tab — derived composite maps
// ---------------------------------------------------------------------------
const chaseState = {
  model: 'hrrr',
  runId: null,
  param: 'cti',
  currentFxx: 0,
  availableFxx: [],
  overlayBounds: null,
  reflectivityEnabled: false,
  reflectivityAvailableFxx: [],
};
if (SHARED_VIEW_CONFIG?.view === 'chase') {
  if (SHARED_VIEW_CONFIG.model) chaseState.model = SHARED_VIEW_CONFIG.model;
  if (SHARED_VIEW_CONFIG.runId) chaseState.runId = SHARED_VIEW_CONFIG.runId;
  if (SHARED_VIEW_CONFIG.parameter) chaseState.param = SHARED_VIEW_CONFIG.parameter;
  if (SHARED_VIEW_CONFIG.fxx !== null) {
    chaseState.currentFxx = /^\d+$/.test(SHARED_VIEW_CONFIG.fxx)
      ? Number.parseInt(SHARED_VIEW_CONFIG.fxx, 10)
      : SHARED_VIEW_CONFIG.fxx;
  }
}
let _chaseOverlayGen = 0;
let _chaseReflectivityGen = 0;

function showMapNotice(message) {
  if (!mapNoticeOverlay) return;
  if (!message) {
    mapNoticeOverlay.textContent = '';
    mapNoticeOverlay.classList.remove('visible');
    return;
  }
  mapNoticeOverlay.textContent = message;
  mapNoticeOverlay.classList.add('visible');
}

function hideMapNotice() {
  showMapNotice('');
}

function clearChaseReflectivityLayer() {
  ++_chaseReflectivityGen;
  if (state.chaseReflectivityLayer) {
    leafletMap.removeLayer(state.chaseReflectivityLayer);
    state.chaseReflectivityLayer = null;
  }
}

function clearChaseOverlayState() {
  ++_chaseOverlayGen;
  clearChaseReflectivityLayer();
  chaseState.availableFxx = [];
  chaseState.reflectivityAvailableFxx = [];
  chaseState.currentFxx = 0;
  state.availableFxx = [];
  if (scrubber) {
    scrubber.min = 0;
    scrubber.max = 0;
    scrubber.value = 0;
  }
  renderScrubberTicks([]);
  if (state.overlayLayer) {
    leafletMap.removeLayer(state.overlayLayer);
    state.overlayLayer = null;
  }
  if (validTimeDisplay) {
    validTimeDisplay.textContent = 'No data available';
  }
}

let _chaseInitialized = false;
function initChaseTab() {
  const modelSel = document.getElementById('chaseModelSelect');
  const runSel = document.getElementById('chaseRunSelect');
  if (!modelSel || !runSel) return;

  if (!_chaseInitialized) {
    _chaseInitialized = true;

    // Wire up model selector
    modelSel.addEventListener('change', () => {
      chaseState.model = modelSel.value;
      updateChaseProductVisibility();
      loadChaseRuns();
    });

    // Wire up run selector
    runSel.addEventListener('change', () => {
      chaseState.runId = runSel.value;
      loadChaseData();
    });

    if (chaseReflectivityToggle) {
      chaseReflectivityToggle.checked = chaseState.reflectivityEnabled;
      chaseReflectivityToggle.addEventListener('change', () => {
        chaseState.reflectivityEnabled = chaseReflectivityToggle.checked;
        updateChaseReflectivityOverlay();
      });
    }

    // Wire up product buttons
    document.querySelectorAll('.chase-product-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.chase-product-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        chaseState.param = btn.dataset.chaseParam;
        loadChaseData();
      });
    });
  }

  modelSel.value = chaseState.model;
  document.querySelectorAll('.chase-product-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.chaseParam === chaseState.param);
  });
  updateChaseProductVisibility();
  loadChaseRuns();
}

function updateChaseProductVisibility() {
  const model = chaseState.model;
  let anyActive = false;
  document.querySelectorAll('.chase-product-btn').forEach(btn => {
    const models = (btn.dataset.chaseModels || '').split(',');
    const visible = models.includes(model);
    btn.style.display = visible ? '' : 'none';
    if (visible && btn.classList.contains('active')) anyActive = true;
  });
  // If active product is hidden, select first visible
  if (!anyActive) {
    const first = document.querySelector('.chase-product-btn[style=""], .chase-product-btn:not([style])');
    if (first) {
      document.querySelectorAll('.chase-product-btn').forEach(b => b.classList.remove('active'));
      first.classList.add('active');
      chaseState.param = first.dataset.chaseParam;
    }
  }
}

async function loadChaseRuns() {
  const runSel = document.getElementById('chaseRunSelect');
  if (!runSel) return;
  try {
    const resp = await fetch(BASE + '/api/runs?model=' + chaseState.model);
    const runs = await resp.json();
    runSel.innerHTML = '';
    if (runs.length === 0) {
      runSel.innerHTML = '<option value="">No runs</option>';
      return;
    }
    runs.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.run_id || r;
      const rid = opt.value;
      // Format: 2026032012 -> "03/20 12Z"
      if (rid.length >= 10) {
        const mm = rid.substring(4, 6);
        const dd = rid.substring(6, 8);
        const hh = rid.substring(8, 10);
        opt.textContent = mm + '/' + dd + ' ' + hh + 'Z';
      } else {
        opt.textContent = rid;
      }
      runSel.appendChild(opt);
    });
    const requestedRun = sharedChaseSelectionPending ? SHARED_VIEW_CONFIG?.runId : null;
    if (requestedRun && runs.some((run) => String(run.run_id || run) === String(requestedRun))) {
      runSel.value = requestedRun;
    }
    chaseState.runId = runSel.value;
    loadChaseData();
  } catch (e) {
    console.error('Chase runs fetch failed:', e);
  }
}

async function loadChaseData() {
  const requestId = ++_chaseRequestId;
  const isCurrent = () => requestId === _chaseRequestId && state.dataSource === 'chase';
  if (!chaseState.runId) return;
  beginMapLoading('Loading chase guidance…');
  const model = chaseState.model;
  const runId = chaseState.runId;
  const param = chaseState.param;

  // Invalidate any in-flight overlay loads (the swap in updateChaseOverlay handles removal)
  ++_chaseOverlayGen;
  ++_chaseReflectivityGen;

  try {
    // Get available fxx for this param
    const availResp = await fetch(BASE + '/api/available/' + model + '/' + runId);
    const avail = await availResp.json();
    if (!isCurrent()) return;
    const fxxList = avail[param] || [];
    chaseState.reflectivityAvailableFxx = (avail.reflectivity || []).slice().sort((a, b) => a - b);
    if (fxxList.length === 0) {
      clearChaseOverlayState();
      showMapNotice('No product data for this run.');
      console.warn('No chase data for', param, 'in', model, runId);
      updateLegend();
      return;
    }
    hideMapNotice();
    chaseState.availableFxx = fxxList.sort((a, b) => a - b);
    const requestedFxx = sharedChaseSelectionPending ? SHARED_VIEW_CONFIG?.fxx : null;
    const sharedFxx = requestedFxx !== null
      ? chaseState.availableFxx.find((value) => String(value) === String(requestedFxx))
      : undefined;
    chaseState.currentFxx = sharedFxx !== undefined ? sharedFxx : fxxList[0];
    sharedChaseSelectionPending = false;

    // Get bounds
    const boundsResp = await fetch(BASE + '/api/bounds/' + model + '/' + runId);
    const bounds = await boundsResp.json();
    if (!isCurrent()) return;
    chaseState.overlayBounds = bounds;

    // Populate scrubber
    populateChaseScrubber();
    updateChaseOverlay();
    updateChaseReflectivityOverlay();
  } catch (e) {
    if (!isCurrent()) return;
    clearChaseOverlayState();
    showMapNotice('Unable to load this chase product right now.');
    updateLegend();
    console.error('Chase data load failed:', e);
  } finally {
    syncShareableDesktopUrl();
    endMapLoading();
  }
}

function populateChaseScrubber() {
  const fxxList = chaseState.availableFxx;
  if (!fxxList || fxxList.length === 0) return;
  state.availableFxx = fxxList;
  state.fxxIndex = Math.max(0, fxxList.findIndex((value) => String(value) === String(chaseState.currentFxx)));
  scrubber.min = 0;
  scrubber.max = Math.max(0, fxxList.length - 1);
  scrubber.value = state.fxxIndex;
  renderScrubberTicks(fxxList);
  syncScrubberVisibility();
}

function updateChaseOverlay() {
  if (!chaseState.overlayBounds) return;
  updateChaseReflectivityOverlay();
  const b = chaseState.overlayBounds;
  const bounds = [[b.south, b.west], [b.north, b.east]];
  const fxx = chaseState.currentFxx;
  const fxxStr = String(fxx).padStart(2, '0');
  const url = BASE + '/rendered/' + chaseState.model + '/' + chaseState.runId + '/' + chaseState.param + '/f' + fxxStr + '.png';

  hideMapNotice();
  if (state.overlayLayer) {
    state.overlayLayer.setUrl(url);
    state.overlayLayer.setBounds(bounds);
  } else {
    state.overlayLayer = L.imageOverlay(url, bounds, { opacity: 1.0, interactive: false, className: modelOverlayClass(chaseState.param) });
    state.overlayLayer.addTo(leafletMap);
  }
  applyModelOverlayClass(chaseState.param);

  const overlayImg = state.overlayLayer?.getElement?.();
  if (overlayImg) {
    const gen = ++_chaseOverlayGen;
    const onLoad = () => {
      if (gen !== _chaseOverlayGen) return;
      updateLegend();
      updateChaseValidTime();
    };
    const onError = () => {
      if (gen !== _chaseOverlayGen) return;
      if (state.overlayLayer) {
        leafletMap.removeLayer(state.overlayLayer);
        state.overlayLayer = null;
      }
      showMapNotice('No values met display threshold for this frame.');
    };
    if (overlayImg.complete && overlayImg.naturalWidth > 0) {
      onLoad();
    } else {
      overlayImg.addEventListener('load', onLoad, { once: true });
      overlayImg.addEventListener('error', onError, { once: true });
    }
  }
  syncShareableDesktopUrl();
}

function updateChaseReflectivityOverlay() {
  const shouldShow = (
    state.dataSource === 'chase'
    && chaseState.reflectivityEnabled
    && chaseState.runId
    && chaseState.overlayBounds
    && chaseState.reflectivityAvailableFxx.includes(chaseState.currentFxx)
  );

  if (!shouldShow) {
    clearChaseReflectivityLayer();
    return;
  }

  const b = chaseState.overlayBounds;
  const bounds = [[b.south, b.west], [b.north, b.east]];
  const fxxStr = String(chaseState.currentFxx).padStart(2, '0');
  const url = BASE + '/rendered/' + chaseState.model + '/' + chaseState.runId + '/reflectivity/f' + fxxStr + '.png?_=' + Date.now();
  const gen = ++_chaseReflectivityGen;
  const oldLayer = state.chaseReflectivityLayer;

  const img = new Image();
  img.onload = () => {
    if (gen !== _chaseReflectivityGen) return;
    if (state.chaseReflectivityLayer) {
      leafletMap.removeLayer(state.chaseReflectivityLayer);
    }
    state.chaseReflectivityLayer = L.imageOverlay(url, bounds, {
      opacity: 1.0,
      interactive: false,
      className: 'chase-reflectivity-overlay',
      pane: 'chaseReflectivityPane',
    });
    state.chaseReflectivityLayer.addTo(leafletMap);
  };
  img.onerror = () => {
    if (gen !== _chaseReflectivityGen) return;
    if (oldLayer && state.chaseReflectivityLayer === oldLayer) {
      leafletMap.removeLayer(oldLayer);
      state.chaseReflectivityLayer = null;
    }
  };
  img.src = url;
}

function updateChaseValidTime() {
  if (!validTimeDisplay) return;
  const fxx = chaseState.currentFxx;
  const runId = chaseState.runId;
  if (!runId || runId.length < 10) return;
  const yyyy = parseInt(runId.substring(0, 4), 10);
  const mm = parseInt(runId.substring(4, 6), 10) - 1;
  const dd = parseInt(runId.substring(6, 8), 10);
  const hh = parseInt(runId.substring(8, 10), 10);
  const init = new Date(Date.UTC(yyyy, mm, dd, hh));
  const valid = new Date(init.getTime() + fxx * 3600000);
  const weekday = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][valid.getUTCDay()];
  const utcH = String(valid.getUTCHours()).padStart(2, '0');
  const fxxStr = 'F+' + String(fxx).padStart(2, '0');
  validTimeDisplay.textContent = fxxStr + '  Valid: ' + weekday + ' ' + utcH + 'z ' + (valid.getUTCMonth() + 1) + '/' + valid.getUTCDate();
}

function switchDataSource(source) {
  if (DEBUG_SCRUBBER && typeof window.__wxPushDebugEvent === 'function') {
    const stack = new Error().stack?.split('\n').slice(2, 5).map((line) => line.trim()).join(' <- ') || 'no stack';
    window.__wxPushDebugEvent(`switchDataSource(${source}) hash=${window.location.hash || '-'} ${stack}`);
  }
  pause();
  stopRadarAutoRefresh();
  ++_modelRunsId;
  ++_modelSelectionId;
  ++_legendRequestId;
  ++_chaseRequestId;
  ++state.satRequestId;
  ++state.radarRequestId;
  clearLoadQueue();
  state.dataSource = source;
  setReferenceOverlayMode(source);
  if (!CAPTURE_MODE) persistActiveView(source);
  clearCaptureReady();
  
  // Update tabs
  tabHome.classList.toggle('active', source === 'home');
  tabModels.classList.toggle('active', source === 'models');
  tabSatellite.classList.toggle('active', source === 'satellite');
  tabRadar.classList.toggle('active', source === 'radar');
  tabChase.classList.toggle('active', source === 'chase');
  tabSurface.classList.toggle('active', source === 'surface');
  document.querySelectorAll('.source-tabs [role="tab"]').forEach((t) => {
    t.setAttribute('aria-selected', String(t.dataset.source === source));
  });
  
  // Toggle control visibility
  satelliteControls.classList.toggle('hidden', source !== 'satellite');
  radarControls.classList.toggle('hidden', source !== 'radar');
  document.getElementById('radarSiteInline')?.classList.toggle('hidden', source !== 'radar');
  
  // Toggle sidebar visibility
  modelSidebar.classList.toggle('hidden', source !== 'models');
  satelliteSidebar.classList.toggle('hidden', source !== 'satellite');
  radarSidebar.classList.toggle('hidden', source !== 'radar');
  if (surfaceSidebar) surfaceSidebar.classList.toggle('hidden', source !== 'surface');
  if (chaseSidebar) chaseSidebar.classList.toggle('hidden', source !== 'chase');
  const homeDashboard = document.getElementById('homeDashboard');
  if (homeDashboard) homeDashboard.classList.toggle('hidden', source !== 'home');
  if (mapSection) mapSection.classList.toggle('hidden', false);
  syncSatelliteImageViewerMode();
  
  // Scrubber visible for both modes - satellite shows time history
  syncScrubberVisibility();

  if (mapSection) mapSection.classList.toggle('radar-mode', source === 'radar');
  if (mapSection) mapSection.classList.toggle('models-mode', source === 'models');
  if (mapSection) mapSection.classList.toggle('chase-mode', source === 'chase');
  if (mapSection) mapSection.classList.toggle('surface-mode', source === 'surface');
  if (source !== 'models' && soundingPanel) {
    soundingPanel.classList.add('hidden');
  }
  updateSoundingProbeUi();

  scheduleLeafletResize();

  const restoringSharedMap = Boolean(
    sharedMapViewPending && SHARED_VIEW_CONFIG?.view === source
  );
  if (source === 'home' && leafletMap && usaBounds && !restoringSharedMap) {
    applyUsaViewport({ animate: false });
    scheduleUsaViewportRefresh(true);
  }

  if (source !== 'chase') {
    hideMapNotice();
  }

  // Clear current overlay
  if (state.overlayLayer) {
    leafletMap.removeLayer(state.overlayLayer);
    state.overlayLayer = null;
  }
  clearChaseReflectivityLayer();
  if (state.satOverlayLayer) {
    leafletMap.removeLayer(state.satOverlayLayer);
    state.satOverlayLayer = null;
  }
  if (state.radarOverlayLayer) {
    leafletMap.removeLayer(state.radarOverlayLayer);
    state.radarOverlayLayer = null;
  }
  syncSurfaceObsMode(source);
  if (source === 'radar' || source === 'home' || source === 'chase') {
    refreshExternalOverlays();
  } else {
    clearOverlayLayer('alertsLayer');
    clearOverlayLayer('spcLayer');
    clearOverlayLayer('spotterLayer');
  if (state.featuredChaserLayer) {
    leafletMap.removeLayer(state.featuredChaserLayer);
    state.featuredChaserLayer = null;
  }
  if (state.featuredChaserTimer) {
    clearInterval(state.featuredChaserTimer);
    state.featuredChaserTimer = null;
  }
  }
  
  // Cleanup home when leaving
  if (source !== 'home') {
    cleanupHomeDashboard();
  }

  // Load appropriate data
  if (source === 'home') {
    initHomeDashboard();
  } else if (source === 'models') {
    setModelReferenceMode(true, { fitViewport: true });
    loadRuns();
    updateMapInfoForModel();
  } else if (source === 'satellite') {
    setSatelliteBasemapMode(true);
    loadSatelliteData();
    updateMapInfoForSatellite();
    refreshSatelliteStatus();
  } else if (source === 'chase') {
    setModelReferenceMode(true, { fitViewport: true });
    initChaseTab();
  } else if (source === 'radar') {
    if (state.radarTilt === 0.5) loadRadarData();
    else loadRadarTilt();
    loadRadarTilts();
    updateMapInfoForRadar();
    refreshRadarStatus();
    void refreshSevereWarnings();
    if (!restoringSharedMap) flyToRadarSite(state.radarSite);
    _radarSiteBuildPromise = buildRadarSiteMarkers().then(showRadarSiteMarkers);
  } else if (source === 'surface') {
    if (!restoringSharedMap) flyToRadarSite(state.radarSite);
  }

  if (restoringSharedMap) restoreSharedMapView(source);

  // Hide radar site markers when leaving radar view
  if (source !== 'radar') {
    hideRadarSiteMarkers();
  }

  // Update legend
  updateLegend();
}

function clearSatelliteLayers() {
  state.satFrames = [];
  state.satFrameIdx = 0;
  clearLoadQueue();
  if (satelliteImageViewerImg) {
    satelliteImageViewerImg.removeAttribute('src');
  }
  if (satelliteImageViewerOverlay) {
    satelliteImageViewerOverlay.innerHTML = '';
  }
  if (state.satOverlayLayer) {
    leafletMap.removeLayer(state.satOverlayLayer);
    state.satOverlayLayer = null;
  }
}

function setLayerVisible(layer, visible, targetMap = leafletMap) {
  if (!layer || !targetMap) return;
  if (visible) {
    if (!targetMap.hasLayer(layer)) layer.addTo(targetMap);
  } else if (targetMap.hasLayer(layer)) {
    targetMap.removeLayer(layer);
  }
}

function buildReferenceCityLayer(cities, options = {}) {
  const {
    pane = 'satelliteCityPane',
    tooltipClassName = 'sat-city-tooltip',
    radius = 3,
    dotColor = 'rgba(7, 11, 18, 0.95)',
    dotFillColor = 'rgba(248, 251, 255, 0.98)',
    dotWeight = 1,
    showMarker = true,
  } = options;

  return L.layerGroup(
    cities.flatMap(([name, lat, lon]) => {
      const dot = L.circleMarker([lat, lon], {
        pane,
        interactive: false,
        radius: showMarker ? radius : 0.1,
        weight: showMarker ? dotWeight : 0,
        color: showMarker ? dotColor : 'transparent',
        fillColor: showMarker ? dotFillColor : 'transparent',
        fillOpacity: showMarker ? 1 : 0,
      });
      dot.bindTooltip(name, {
        permanent: true,
        direction: 'right',
        offset: [8, 0],
        opacity: 1,
        className: tooltipClassName,
      });
      return [dot];
    })
  );
}

function updateModelCityLayerVisibility() {
  if (!leafletMap) return;
  setLayerVisible(window._satelliteCityLayer, false);
  setLayerVisible(window._modelCityLayer, false);
}

function updateModelDetailLayerVisibility() {
  if (!leafletMap) return;
  const isModels = state.dataSource === 'models';
  const zoom = leafletMap.getZoom();
  setLayerVisible(window._roadsBoundaryLayer, isModels && zoom >= MODEL_ROADS_ZOOM_THRESHOLD);
  const showReferenceCounties = !isModels && zoom >= REFERENCE_COUNTY_ZOOM_THRESHOLD;
  setLayerVisible(window._countyBoundaryLayer, (isModels && zoom >= MODEL_COUNTY_ZOOM_THRESHOLD) || showReferenceCounties);
}

function updateSatelliteViewport() {
  if (!leafletMap || state.dataSource !== 'satellite') return;
  const bounds = state.satelliteBounds || [[22, -130], [51, -62]];
  const [[south, west], [north, east]] = bounds;
  const latInset = Math.max(0.75, (north - south) * 0.20);
  const lonInset = Math.max(1.5, (east - west) * 0.20);
  const viewBounds = [
    [south + latInset, west + lonInset],
    [north - latInset, east - lonInset],
  ];
  leafletMap.fitBounds(viewBounds, { animate: false, padding: [0, 0] });
  leafletMap.setMaxBounds(bounds);
  applySatellitePaneClip();
}

function applySatellitePaneClip() {
  return;
}

function clearSatellitePaneClip() {
  if (!leafletMap) return;
  ['tilePane', 'labelsPane'].forEach((paneName) => {
    const pane = leafletMap.getPane(paneName);
    if (pane) pane.style.clipPath = '';
  });
}

/** Pixels of the map hidden behind floating chrome (top bar, side panel, time dock). */
function getModelViewInsets() {
  const insets = { top: 0, left: 0, right: 0, bottom: 0 };
  const mapRect = leafletMap?.getContainer?.()?.getBoundingClientRect?.();
  if (!mapRect || !mapRect.width || !mapRect.height) return insets;
  // getClientRects is empty for display:none; offsetParent would also be null for fixed elements.
  const visible = (el) => el && !el.classList.contains('hidden') && el.getClientRects().length > 0;
  const topbar = document.querySelector('.topbar');
  if (visible(topbar)) insets.top = Math.max(0, topbar.getBoundingClientRect().bottom - mapRect.top);
  if (visible(modelSidebar)) insets.left = Math.max(0, modelSidebar.getBoundingClientRect().right - mapRect.left);
  const dock = document.getElementById('scrubberBar');
  if (visible(dock)) {
    const dockRect = dock.getBoundingClientRect();
    // Only a dock spanning most of the map hides a full-width strip.
    if (dockRect.width > mapRect.width * 0.6) insets.bottom = Math.max(0, mapRect.bottom - dockRect.top);
  }
  insets.top = Math.min(insets.top, mapRect.height * 0.3);
  insets.left = Math.min(insets.left, mapRect.width * 0.5);
  insets.bottom = Math.min(insets.bottom, mapRect.height * 0.3);
  return insets;
}

/** Ray-casting test: is (lat, lon) inside MODEL_DATA_OUTLINE? */
function isInModelDataOutline(lat, lon) {
  let inside = false;
  for (let i = 0, j = MODEL_DATA_OUTLINE.length - 1; i < MODEL_DATA_OUTLINE.length; j = i++) {
    const [latI, lonI] = MODEL_DATA_OUTLINE[i];
    const [latJ, lonJ] = MODEL_DATA_OUTLINE[j];
    if ((latI > lat) !== (latJ > lat) && lon < ((lonJ - lonI) * (lat - latI)) / (latJ - latI) + lonI) inside = !inside;
  }
  return inside;
}

/**
 * Default model view: centre MODEL_DEFAULT_FOCUS in the part of the map not
 * covered by panels, zoomed out as far as possible while the whole window
 * (gutters and see-through chrome included) shows only area with data.
 */
function getModelDataView() {
  const size = leafletMap.getSize();
  const insets = getModelViewInsets();
  const crs = leafletMap.options.crs;
  const focus = L.latLng(MODEL_DEFAULT_FOCUS[0], MODEL_DEFAULT_FOCUS[1]);
  // Screen offset from the window centre to the centre of the clear area.
  const offset = L.point((insets.left - insets.right) / 2, (insets.top - insets.bottom) / 2);
  const centerAt = (zoom) => crs.pointToLatLng(crs.latLngToPoint(focus, zoom).subtract(offset), zoom);
  const fits = (zoom) => {
    const topLeft = crs.latLngToPoint(centerAt(zoom), zoom).subtract(size.divideBy(2));
    const steps = 24;
    for (let i = 0; i <= steps; i++) {
      const fx = (size.x * i) / steps;
      const fy = (size.y * i) / steps;
      for (const [x, y] of [[fx, 0], [fx, size.y], [0, fy], [size.x, fy]]) {
        const ll = crs.pointToLatLng(topLeft.add([x, y]), zoom);
        if (!isInModelDataOutline(ll.lat, ll.lng)) return false;
      }
    }
    return true;
  };
  let lo = defaultMinZoom;
  let hi = leafletMap.getMaxZoom();
  if (fits(lo)) hi = lo;
  for (let i = 0; i < 24 && hi - lo > 0.005; i++) {
    const mid = (lo + hi) / 2;
    if (fits(mid)) hi = mid; else lo = mid;
  }
  return { center: centerAt(hi), zoom: hi };
}

let _modelViewTouched = false;
let _modelViewRefitToken = 0;
let _modelViewWatching = false;

/** Note when the viewer drags, scrolls or keys the map, so refits stop. */
function watchModelViewInteraction() {
  if (_modelViewWatching) return;
  _modelViewWatching = true;
  const container = leafletMap.getContainer();
  const markTouched = () => { _modelViewTouched = true; };
  for (const type of ['pointerdown', 'wheel', 'keydown', 'touchstart']) {
    container.addEventListener(type, markTouched, { passive: true });
  }
}

function applyModelDataView() {
  const { center, zoom } = getModelDataView();
  // setView rounds to zoomSnap; allow the exact fit for this one view.
  const zoomSnap = leafletMap.options.zoomSnap;
  leafletMap.options.zoomSnap = 0;
  try {
    leafletMap.setView(center, zoom, { animate: false });
  } finally {
    leafletMap.options.zoomSnap = zoomSnap;
  }
}

function updateModelViewport() {
  if (!leafletMap || state.dataSource !== 'models') return;
  const bounds = MODEL_PRESENTATION_BOUNDS;
  applyModelDataView();
  // Panels and the map container are still settling during start-up and tab
  // switches; refit a few times unless the viewer has touched the map.
  watchModelViewInteraction();
  _modelViewTouched = false;
  const refitToken = ++_modelViewRefitToken;
  for (const delay of [0, 250, 800]) {
    setTimeout(() => {
      if (refitToken !== _modelViewRefitToken || _modelViewTouched || state.dataSource !== 'models') return;
      applyModelDataView();
    }, delay);
  }

  const [[frameSouth, frameWest], [frameNorth, frameEast]] = bounds;
  const latPad = Math.max((frameNorth - frameSouth) * 0.08, 2.0);
  const lonPad = Math.max((frameEast - frameWest) * 0.08, 4.0);
  leafletMap.setMaxBounds([
    [frameSouth - latPad, frameWest - lonPad],
    [frameNorth + latPad, frameEast + lonPad],
  ]);
}

function reflowActiveMapSource() {
  if (!leafletMap) return;
  if (state.dataSource === 'satellite') {
    if (shouldUseSatelliteImageViewer()) {
      applySatelliteImageViewerLayout();
    } else {
      applySatellitePaneClip();
    }
    return;
  }
  if (state.dataSource === 'home' && usaBounds) {
    scheduleUsaViewportRefresh(false);
  }
}

function updateModelFrame(bounds = state.overlayBounds) {
  if (!leafletMap) return;
  bounds = MODEL_PRESENTATION_BOUNDS;
  const frameBounds = L.latLngBounds(bounds);
  const isSparseCanvas = MODEL_SPARSE_CANVAS_PARAMS.has(state.parameter);
  const backingStyle = { fillColor: '#000000', fillOpacity: 1.0 };
  const shadowStyle = isSparseCanvas
    ? { color: '#02050a', weight: 5, opacity: 0.72 }
    : { color: '#04070d', weight: 4, opacity: 0.5 };
  const outerBorderStyle = isSparseCanvas
    ? { color: '#0b1220', weight: 3.4, opacity: 0.96 }
    : { color: '#0f1722', weight: 2.8, opacity: 0.88 };
  const innerBorderStyle = isSparseCanvas
    ? { color: '#ffffff', weight: 1.7, opacity: 1.0 }
    : { color: '#fbfdff', weight: 1.35, opacity: 0.98 };

  if (!window._modelCanvasBackingLayer) {
    window._modelCanvasBackingLayer = L.rectangle(frameBounds, {
      pane: 'modelCanvasPane',
      interactive: false,
      stroke: false,
      fill: true,
      fillColor: backingStyle.fillColor,
      fillOpacity: backingStyle.fillOpacity,
    });
  } else {
    window._modelCanvasBackingLayer.setBounds(frameBounds);
    window._modelCanvasBackingLayer.setStyle(backingStyle);
  }

  if (!window._modelFrameShadowLayer) {
    window._modelFrameShadowLayer = L.rectangle(frameBounds, {
      pane: 'boundaryPane',
      interactive: false,
      stroke: true,
      color: shadowStyle.color,
      weight: shadowStyle.weight,
      opacity: shadowStyle.opacity,
      fill: false,
      lineJoin: 'round',
    });
  } else {
    window._modelFrameShadowLayer.setBounds(frameBounds);
    window._modelFrameShadowLayer.setStyle(shadowStyle);
  }

  if (!window._modelFrameOuterBorderLayer) {
    window._modelFrameOuterBorderLayer = L.rectangle(frameBounds, {
      pane: 'boundaryPane',
      interactive: false,
      stroke: true,
      color: outerBorderStyle.color,
      weight: outerBorderStyle.weight,
      opacity: outerBorderStyle.opacity,
      fill: false,
      lineJoin: 'round',
    });
  } else {
    window._modelFrameOuterBorderLayer.setBounds(frameBounds);
    window._modelFrameOuterBorderLayer.setStyle(outerBorderStyle);
  }

  if (!window._modelFrameBorderLayer) {
    window._modelFrameBorderLayer = L.rectangle(frameBounds, {
      pane: 'boundaryPane',
      interactive: false,
      stroke: true,
      color: innerBorderStyle.color,
      weight: innerBorderStyle.weight,
      opacity: innerBorderStyle.opacity,
      fill: false,
      lineJoin: 'round',
    });
  } else {
    window._modelFrameBorderLayer.setBounds(frameBounds);
    window._modelFrameBorderLayer.setStyle(innerBorderStyle);
  }
}

function setReferenceOverlayMode(mode, { fitViewport = false } = {}) {
  if (!leafletMap) return;

  const baseTile = baseTileLayer?.getContainer?.();
  if (baseTile) baseTile.style.display = (mode === 'satellite') ? 'none' : '';
  const labelTile = labelTileLayer?.getContainer?.();
  if (labelTile) labelTile.style.display = (mode === 'satellite') ? 'none' : '';

  if (mode === 'satellite') {
    if (fitViewport) updateSatelliteViewport();
    resetModelBackgroundWash();
  } else if (mode === 'models') {
    resetSatelliteBackgroundWash();
    resetModelBackgroundWash();
    if (fitViewport) updateModelViewport();
  } else {
    leafletMap.setMaxBounds(null);
    leafletMap.setMinZoom(defaultMinZoom);
    resetModelBackgroundWash();
    resetSatelliteBackgroundWash();
    clearSatellitePaneClip();
  }

  const boundaryStylePresets = {
    satellite: {
      line: { color: '#f4f7fb', weight: 1.3, opacity: 0.72, lineJoin: 'round', lineCap: 'round' },
      outline: { color: '#05080d', weight: 2.7, opacity: 0.42, lineJoin: 'round', lineCap: 'round' },
    },
    models: {
      line: { color: '#243040', weight: 1.1, opacity: 0.85, lineJoin: 'round', lineCap: 'round' },
      outline: { color: '#f4f7fb', weight: 2.4, opacity: 0.48, lineJoin: 'round', lineCap: 'round' },
    },
    default: {
      line: { color: '#333', weight: 1.2, opacity: 0.85, lineJoin: 'round', lineCap: 'round' },
      outline: { color: '#05080d', weight: 2.2, opacity: 0.0, lineJoin: 'round', lineCap: 'round' },
    },
  };

  const styles = {
    satellite: {
      state: boundaryStylePresets.satellite.line,
      stateOutline: boundaryStylePresets.satellite.outline,
      country: boundaryStylePresets.satellite.line,
      countryOutline: boundaryStylePresets.satellite.outline,
      coast: boundaryStylePresets.satellite.line,
      coastOutline: boundaryStylePresets.satellite.outline,
      lakes: { color: '#9cc7e4', weight: 0.8, fillColor: '#86b7d8', fillOpacity: 0.08, opacity: 0.65 },
      showCoast: true,
      showCities: true,
      showCounties: false,
      showRoads: false,
    },
    models: {
      state: boundaryStylePresets.models.line,
      stateOutline: boundaryStylePresets.models.outline,
      country: boundaryStylePresets.models.line,
      countryOutline: boundaryStylePresets.models.outline,
      coast: boundaryStylePresets.models.line,
      coastOutline: boundaryStylePresets.models.outline,
      lakes: { color: '#4a90b8', weight: 0, fillColor: '#4a90b8', fillOpacity: 0.5, opacity: 0 },
      showCoast: true,
      showCities: false,
      showCounties: true,
      showRoads: true,
      showModelFrame: true,
    },
    default: {
      state: boundaryStylePresets.default.line,
      stateOutline: boundaryStylePresets.default.outline,
      country: boundaryStylePresets.default.line,
      countryOutline: boundaryStylePresets.default.outline,
      coast: boundaryStylePresets.default.line,
      coastOutline: boundaryStylePresets.default.outline,
      lakes: { color: '#4a90b8', weight: 0, fillColor: '#4a90b8', fillOpacity: 0.5, opacity: 0 },
      showCoast: false,
      showCities: false,
      showCounties: true,
      showRoads: true,
      showModelFrame: false,
    },
  };

  const styleSet = styles[mode] || styles.default;
  if (window._stateBoundaryLayer) {
    window._stateBoundaryLayer.setStyle(styleSet.state);
  }
  if (window._stateBoundaryOutlineLayer) {
    window._stateBoundaryOutlineLayer.setStyle(styleSet.stateOutline);
  }
  if (window._countryBoundaryLayer) {
    window._countryBoundaryLayer.setStyle(styleSet.country);
  }
  if (window._countryBoundaryOutlineLayer) {
    window._countryBoundaryOutlineLayer.setStyle(styleSet.countryOutline);
  }
  if (window._coastlineBoundaryLayer) {
    window._coastlineBoundaryLayer.setStyle(styleSet.coast);
  }
  if (window._coastlineBoundaryOutlineLayer) {
    window._coastlineBoundaryOutlineLayer.setStyle(styleSet.coastOutline);
  }
  if (window._lakesBoundaryLayer) {
    window._lakesBoundaryLayer.setStyle(styleSet.lakes);
  }
  setLayerVisible(window._coastlineBoundaryLayer, styleSet.showCoast);
  setLayerVisible(
    window._countyBoundaryLayer,
    styleSet.showCounties && leafletMap.getZoom() >= REFERENCE_COUNTY_ZOOM_THRESHOLD
  );
  setLayerVisible(window._roadsBoundaryLayer, styleSet.showRoads);
  setLayerVisible(window._satelliteCityLayer, mode === 'satellite' && styleSet.showCities);
  updateModelCityLayerVisibility();
  updateModelDetailLayerVisibility();
  if (styleSet.showModelFrame) updateModelFrame();
  setLayerVisible(window._modelCanvasBackingLayer, !!styleSet.showModelFrame);
  setLayerVisible(window._modelFrameShadowLayer, !!styleSet.showModelFrame);
  setLayerVisible(window._modelFrameOuterBorderLayer, !!styleSet.showModelFrame);
  setLayerVisible(window._modelFrameBorderLayer, !!styleSet.showModelFrame);
}

function setSatelliteBasemapMode(enabled) {
  setReferenceOverlayMode(enabled ? 'satellite' : 'default', { fitViewport: enabled });
  const mapEl = leafletMap?.getContainer?.();
  if (mapEl) mapEl.classList.toggle('satellite-canvas', enabled);
  if (!enabled) clearSatellitePaneClip();

  const overlayEl = state.satOverlayLayer?.getElement?.();
  if (overlayEl) {
    overlayEl.style.mixBlendMode = 'normal';
  }
}

function setModelReferenceMode(enabled, options = {}) {
  setReferenceOverlayMode(enabled ? 'models' : 'default', options);
}



function clearRadarLayers() {
  state.radarFrames = [];
  state.radarFrameIdx = 0;
  clearLoadQueue();
  if (state.radarOverlayLayer) {
    leafletMap.removeLayer(state.radarOverlayLayer);
    state.radarOverlayLayer = null;
  }
}

function clearModelLayers() {
  if (state.overlayLayer) {
    leafletMap.removeLayer(state.overlayLayer);
    state.overlayLayer = null;
  }
  clearChaseReflectivityLayer();
}

// ---------------------------------------------------------------------------
// Leaflet map initialization
// ---------------------------------------------------------------------------
function initMap() {
  const hrrrBounds = L.latLngBounds([21.0, -135.0], [53.0, -60.0]);
  const conusReferenceBounds = {
    west: -127.0,
    east: -65.0,
    south: 23.0,
    north: 51.5,
  };
  const regionalReferenceBounds = {
    west: -141.0,
    east: -52.0,
    south: 18.0,
    north: 58.5,
  };

  usaBounds = L.latLngBounds([23.0, -129.0], [53.0, -65.0]);
  const usaMaxBounds = L.latLngBounds([18.0, -134.5], [58.5, -59.0]);

  leafletMap = L.map('map', {
    maxZoom: 10,
    zoomControl: false,
    attributionControl: true,
    zoomSnap: 0.25,
    zoomDelta: 0.5,
    wheelPxPerZoomLevel: 40,
    maxBounds: usaMaxBounds,
    maxBoundsViscosity: 1.0
  });
  L.control.zoom({ position: 'topright' }).addTo(leafletMap);
  defaultMinZoom = leafletMap.getMinZoom();

  if (typeof SurfaceObs !== 'undefined') {
    SurfaceObs.init(leafletMap, BASE);
  }

  applyUsaViewport({ animate: false });
  leafletMap.on('mousedown', (evt) => {
    _mapPointerDragged = false;
    _mapPointerDownPoint = evt?.containerPoint ? { x: evt.containerPoint.x, y: evt.containerPoint.y } : null;
  });
  leafletMap.on('dragstart', () => {
    _mapPointerDragged = true;
  });
  leafletMap.on('dragend', () => {
    setTimeout(() => {
      _mapPointerDragged = false;
    }, 0);
  });
  leafletMap.on('mousemove', debouncedMapHover);
  leafletMap.on('click', handleMapValueClick);
  leafletMap.on('mouseout', () => {
    setHoverValueHtml('', false);
    // Cancel any pending query
    if (_hoverAbortController) {
      _hoverAbortController.abort();
      _hoverAbortController = null;
    }
    // Clear debounce timer
    if (_hoverDebounceTimer) {
      clearTimeout(_hoverDebounceTimer);
      _hoverDebounceTimer = null;
    }
  });

  // Keep satellite clipping in sync with Leaflet's internal resize handling,
  // but do not force a viewport refit here. Source-specific reflow is
  // handled separately after the resize settles.
  leafletMap.on('resize', function() {
      if (state.dataSource === 'satellite' && !shouldUseSatelliteImageViewer()) {
        applySatellitePaneClip();
      }
  });

  baseTileLayer = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png?key=cb1_2l3n_1_eb188b07acba78da03ca3c9c', {
      attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://www.openstreetmap.org/">OSM</a>',
      subdomains: 'abcd',
      maxZoom: 19,
      crossOrigin: true,
      }
  ).addTo(leafletMap);

  // RainViewer radar composite pane
  const radarCompositePane = leafletMap.createPane('radarCompositePane');
  radarCompositePane.style.zIndex = 430;
  radarCompositePane.style.pointerEvents = 'none';

  const modelCanvasPane = leafletMap.createPane('modelCanvasPane');
  modelCanvasPane.style.zIndex = 392;
  modelCanvasPane.style.pointerEvents = 'none';

  const chaseReflectivityPane = leafletMap.createPane('chaseReflectivityPane');
  chaseReflectivityPane.style.zIndex = 404;
  chaseReflectivityPane.style.pointerEvents = 'none';

  // Vector boundaries pane
  const boundaryPane = leafletMap.createPane('boundaryPane');
  boundaryPane.style.zIndex = 445;
  boundaryPane.style.pointerEvents = 'none';
  boundaryRenderer = L.svg({ pane: 'boundaryPane' });

  const spcPane = leafletMap.createPane('spcPane');
  spcPane.style.zIndex = 460;
  spcPane.style.pointerEvents = 'auto';
  spcRenderer = L.svg({ pane: 'spcPane' });

  const alertsPane = leafletMap.createPane('alertsPane');
  alertsPane.style.zIndex = 470;
  alertsPane.style.pointerEvents = 'auto';
  alertsRenderer = L.canvas({ pane: 'alertsPane' });

  const spotterPane = leafletMap.createPane('spotterPane');
  spotterPane.style.zIndex = 480;
  spotterPane.style.pointerEvents = 'auto';
  // Note: no canvas renderer for spotters - using default SVG so clicks work

  const featuredChaserPane = leafletMap.createPane('featuredChaserPane');
  featuredChaserPane.style.zIndex = 485;
  featuredChaserPane.style.pointerEvents = 'auto';

  // Radar site marker pane – clickable dots for each NEXRAD station
  const radarSitePane = leafletMap.createPane('radarSitePane');
  radarSitePane.style.zIndex = 490;
  radarSitePane.style.pointerEvents = 'auto';

    // Boundary fetches: states/lakes are visible at the default zoom but are
  // deferred until after first paint; countries/coastline are coarse low-zoom
  // context and only load once the zoom level can actually show them.
  const loadStateBoundaries = () => {
    fetch(`${BASE}/static/boundaries_states.json`)
    .then(r => r.json())
    .then(geojson => {
      const filtered = filterGeoJSONToBounds(geojson, conusReferenceBounds);
      _viewerStateGeoJSON = filtered;
      _viewerOverlayCache.key = null;
      window._stateBoundaryOutlineLayer = L.geoJSON(filtered, {
        pane: 'boundaryPane',
        renderer: boundaryRenderer,
        style: { color: '#05080d', weight: 2.2, opacity: 0.0 },
        interactive: false,
      }).addTo(leafletMap);
      window._stateBoundaryLayer = L.geoJSON(filtered, {
        pane: 'boundaryPane',
        renderer: boundaryRenderer,
        style: { color: '#333', weight: 1.2, opacity: 0.85 },
        interactive: false,
      }).addTo(leafletMap);
      if (state.dataSource === 'satellite' || state.dataSource === 'models') {
        setReferenceOverlayMode(state.dataSource);
      }
      if (shouldUseSatelliteImageViewer() && state.satFrames[state.satFrameIdx]) {
        renderSatelliteImageViewerOverlay(state.satFrames[state.satFrameIdx]);
      }
    }).catch(e => { console.warn('State boundaries failed:', e); showToast('State boundary overlay failed to load', 'error'); });
  };

  const loadLakeBoundaries = () => {
    fetch(`${BASE}/static/boundaries_lakes.json`)
    .then(r => r.json())
    .then(geojson => {
      const filtered = filterGeoJSONToBounds(geojson, conusReferenceBounds);
      _viewerLakeGeoJSON = filtered;
      _viewerOverlayCache.key = null;
      window._lakesBoundaryLayer = L.geoJSON(filtered, {
        pane: 'boundaryPane',
        renderer: boundaryRenderer,
        style: { color: '#4a90b8', weight: 0, fillColor: '#4a90b8', fillOpacity: 0.5, opacity: 0 },
        interactive: false,
      }).addTo(leafletMap);
      if (state.dataSource === 'satellite' || state.dataSource === 'models') {
        setReferenceOverlayMode(state.dataSource);
      }
      if (shouldUseSatelliteImageViewer() && state.satFrames[state.satFrameIdx]) {
        renderSatelliteImageViewerOverlay(state.satFrames[state.satFrameIdx]);
      }

    }).catch(e => { console.warn('Lakes failed:', e); showToast('Lake overlay failed to load', 'error'); });
  };

  const loadCountryCoastBoundaries = () => {
    fetch(`${BASE}/static/boundaries_countries.json`)
    .then(r => r.json())
    .then(geojson => {
      const filtered = filterGeoJSONToBounds(geojson, regionalReferenceBounds);
      _viewerCountryGeoJSON = filtered;
      _viewerOverlayCache.key = null;
      window._countryBoundaryOutlineLayer = L.geoJSON(filtered, {
        pane: 'boundaryPane',
        renderer: boundaryRenderer,
        style: { color: '#05080d', weight: 2.2, opacity: 0.0 },
        interactive: false,
      }).addTo(leafletMap);
      window._countryBoundaryLayer = L.geoJSON(filtered, {
        pane: 'boundaryPane',
        renderer: boundaryRenderer,
        style: { color: '#333', weight: 1.2, opacity: 0.85 },
        interactive: false,
      }).addTo(leafletMap);
      if (state.dataSource === 'satellite' || state.dataSource === 'models') {
        setReferenceOverlayMode(state.dataSource);
      }
      if (shouldUseSatelliteImageViewer() && state.satFrames[state.satFrameIdx]) {
        renderSatelliteImageViewerOverlay(state.satFrames[state.satFrameIdx]);
      }
    }).catch(e => { console.warn('Country boundaries failed:', e); showToast('Country boundary overlay failed to load', 'error'); });

    fetch(`${BASE}/static/boundaries_coastline.json`)
    .then(r => r.json())
    .then(geojson => {
      _viewerCoastGeoJSON = geojson;
      _viewerOverlayCache.key = null;
      window._coastlineBoundaryOutlineLayer = L.geoJSON(geojson, {
        pane: 'boundaryPane',
        renderer: boundaryRenderer,
        style: { color: '#05080d', weight: 2.2, opacity: 0.0 },
        interactive: false,
      });
      window._coastlineBoundaryLayer = L.geoJSON(geojson, {
        pane: 'boundaryPane',
        renderer: boundaryRenderer,
        style: { color: '#5a6772', weight: 1.1, opacity: 0.45 },
        interactive: false,
      });
      if (state.dataSource === 'satellite' || state.dataSource === 'models') {
        window._coastlineBoundaryOutlineLayer.addTo(leafletMap);
        window._coastlineBoundaryLayer.addTo(leafletMap);
        setReferenceOverlayMode(state.dataSource);
      }
      if (shouldUseSatelliteImageViewer() && state.satFrames[state.satFrameIdx]) {
        renderSatelliteImageViewerOverlay(state.satFrames[state.satFrameIdx]);
      }
    }).catch(e => { console.warn('Coastline failed:', e); showToast('Coastline overlay failed to load', 'error'); });
  };

  let _countryCoastLoadStarted = false;
  const lazyLoadCountryCoastBoundaries = () => {
    if (_countryCoastLoadStarted) return;
    if (leafletMap.getZoom() < REFERENCE_COUNTRY_COAST_ZOOM_THRESHOLD) return;
    _countryCoastLoadStarted = true;
    loadCountryCoastBoundaries();
  };
  leafletMap.on('zoomend', lazyLoadCountryCoastBoundaries);

  let _deferredBoundariesRan = false;
  const runDeferredBoundaries = () => {
    if (_deferredBoundariesRan) return;
    _deferredBoundariesRan = true;
    const idle = window.requestIdleCallback || ((cb) => setTimeout(cb, 0));
    idle(() => {
      loadStateBoundaries();
      loadLakeBoundaries();
      lazyLoadCountryCoastBoundaries();
    }, { timeout: 1000 });
  };
  // The initial map load may already have fired before listeners were attached.
  runDeferredBoundaries();

  // Lazy-load counties and roads — deferred until zoom level needs them
  let _countyLoadStarted = false, _roadsLoadStarted = false;
  function lazyLoadCountiesAndRoads() {
    const zoom = leafletMap.getZoom();
    if (!_roadsLoadStarted && zoom >= MODEL_ROADS_ZOOM_THRESHOLD) {
      _roadsLoadStarted = true;
      fetch(`${BASE}/static/boundaries_roads.json`)
        .then(r => r.json())
        .then(geojson => {
          const filtered = filterGeoJSONToBounds(geojson, conusReferenceBounds);
          window._roadsBoundaryLayer = L.geoJSON(filtered, {
            pane: 'boundaryPane',
            renderer: boundaryRenderer,
            style: { color: '#333', weight: 0.7, opacity: 0.6 },
            interactive: false,
          }).addTo(leafletMap);
          if (state.dataSource === 'satellite' || state.dataSource === 'models') {
            setReferenceOverlayMode(state.dataSource);
          }
        }).catch(e => console.warn('Roads failed:', e));
    }
    if (!_countyLoadStarted && zoom >= MODEL_COUNTY_ZOOM_THRESHOLD) {
      _countyLoadStarted = true;
      fetch(`${BASE}/static/boundaries_counties.json`)
        .then(r => r.json())
        .then(geojson => {
          const filtered = filterGeoJSONToBounds(geojson, conusReferenceBounds);
          window._countyBoundaryLayer = L.geoJSON(filtered, {
            pane: 'boundaryPane',
            renderer: boundaryRenderer,
            style: { color: '#444', weight: 0.6, opacity: 0.7, fill: false },
            interactive: false,
          }).addTo(leafletMap);
          if (state.dataSource === 'satellite' || state.dataSource === 'models') {
            setReferenceOverlayMode(state.dataSource);
          }
        }).catch(e => console.warn('County boundaries failed:', e));
    }
  }
  leafletMap.on('zoomend', lazyLoadCountiesAndRoads);
  lazyLoadCountiesAndRoads(); // check initial zoom level

  const labelsPane = leafletMap.createPane('labelsPane');
  labelsPane.style.zIndex = 450;
  labelsPane.style.pointerEvents = 'none';
  labelTileLayer = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/dark_only_labels/{z}/{x}/{y}{r}.png?key=cb1_2l3n_1_eb188b07acba78da03ca3c9c', {
      pane: 'labelsPane',
      subdomains: 'abcd',
      maxZoom: 19,
      crossOrigin: true,
    }
  ).addTo(leafletMap);

  const satelliteCityPane = leafletMap.createPane('satelliteCityPane');
  satelliteCityPane.style.zIndex = 452;
  satelliteCityPane.style.pointerEvents = 'none';

  window._satelliteCityLayer = buildReferenceCityLayer(SATELLITE_REFERENCE_CITIES, {
    pane: 'satelliteCityPane',
    tooltipClassName: 'sat-city-tooltip',
    showMarker: false,
  });
  const modelCityPane = leafletMap.createPane('modelCityPane');
  modelCityPane.style.zIndex = 453;
  modelCityPane.style.pointerEvents = 'none';
  window._modelCityLayer = buildReferenceCityLayer(MODEL_REFERENCE_CITIES, {
    pane: 'modelCityPane',
    tooltipClassName: 'model-city-tooltip',
    radius: 2.25,
    dotWeight: 0.9,
    dotColor: 'rgba(6, 10, 16, 0.92)',
    dotFillColor: 'rgba(244, 247, 251, 0.95)',
    showMarker: false,
  });
  if (state.dataSource === 'satellite' || state.dataSource === 'models') {
    setReferenceOverlayMode(state.dataSource);
  }

  ['move', 'zoom', 'resize'].forEach((eventName) => {
    leafletMap.on(eventName, () => {
      if (state.dataSource === 'satellite') {
        applySatellitePaneClip();
      }
      if (state.dataSource === 'radar' && state.storm3dArmed) {

      }
    });
  });
  ['moveend', 'zoomend'].forEach((eventName) => {
    leafletMap.on(eventName, () => {
      if (state.dataSource === 'radar' && state.storm3dArmed) {

      }
    });
  });

  leafletMap.on('zoomstart', () => {
    if (state.dataSource === 'satellite' && state.isPlaying) {
      _satelliteResumeAfterZoom = true;
      pause();
    } else {
      _satelliteResumeAfterZoom = false;
    }
  });

  leafletMap.on('zoomend', () => {
    if (state.dataSource === 'satellite' && _satelliteResumeAfterZoom) {
      _satelliteResumeAfterZoom = false;
      play();
    }
  });

  // Home page: switch composite ↔ individual site layers based on zoom
  leafletMap.on('zoomend', () => {
    if (state.dataSource === 'home' && state.radarCompositeEnabled) {
      updateHomeRadarMode();
    }
    updateModelCityLayerVisibility();
    updateModelDetailLayerVisibility();
  });
  leafletMap.on('moveend', updateModelCityLayerVisibility);
  leafletMap.on('moveend', updateModelDetailLayerVisibility);
  leafletMap.on('moveend', syncShareableDesktopUrl);

  window.addEventListener('resize', () => {
    scheduleActiveSourceReflow();
  });

  if (typeof ResizeObserver !== 'undefined' && !satelliteViewerResizeObserver) {
    satelliteViewerResizeObserver = new ResizeObserver(() => {
      scheduleActiveSourceReflow();
    });
    if (satelliteImageViewer) {
      satelliteViewerResizeObserver.observe(satelliteImageViewer);
    }
    if (mapSection) {
      satelliteViewerResizeObserver.observe(mapSection);
    }
  }

  const scrubberEl = document.querySelector('.scrubber-bar');
  if (scrubberEl) {
    L.DomEvent.disableScrollPropagation(scrubberEl);
    L.DomEvent.disableClickPropagation(scrubberEl);
  }
}

// ---------------------------------------------------------------------------
// Satellite Data Functions
// ---------------------------------------------------------------------------
async function loadSatelliteData() {
  const requestId = ++state.satRequestId;
  clearCaptureReady();
  beginMapLoading('Loading satellite imagery…');
  try {

    // Fetch frame history (all timestamped renders)
    let frames = [];
    const satQuery = state.satRegion ? `?region=${encodeURIComponent(state.satRegion)}` : '';
    try {
      frames = await apiFetch(`${BASE}/api/satellite/frames/${state.satellite}/${state.satProduct}${satQuery}`);
    } catch (_) {}
    if (requestId !== state.satRequestId || state.dataSource !== 'satellite') return;

    // Fetch latest as fallback / always show latest on load
    const meta = await apiFetch(`${BASE}/api/satellite/latest/${state.satellite}/${state.satProduct}${satQuery}`);
    if (requestId !== state.satRequestId || state.dataSource !== 'satellite') return;
    state.satMetadata = meta;

    if (meta && meta.bounds) {
      state.satelliteBounds = [[meta.bounds.south, meta.bounds.west], [meta.bounds.north, meta.bounds.east]];
      setSatelliteBasemapMode(true);
    }

    // Build frame list: timestamped history + latest
    if (frames.length > 0) {
      state.satFrames = frames;
    } else {
      // Only latest available — wrap it as a single frame
      state.satFrames = [{ timestamp: meta.timestamp, png_path: meta.png_path, bounds: meta.bounds }];
    }
    state.satFrameIdx = state.satFrames.length - 1; // start at newest
    if (sharedFramePending && SHARED_VIEW_CONFIG?.view === 'satellite') {
      const sharedIdx = state.satFrames.findIndex((frame) => String(frame.timestamp) === sharedFramePending);
      if (sharedIdx >= 0) state.satFrameIdx = sharedIdx;
      sharedFramePending = null;
    }

    preloadAllDesktopFrames();
    updateSatScrubber();
    showSatelliteFrame(state.satFrameIdx);


    const updateEl = document.getElementById('satLastUpdate');
    if (updateEl && meta.timestamp) {
      updateEl.textContent = `Last update: ${formatSatTime(meta.timestamp)}`;
    }
  } catch (e) {
    console.error('Failed to load satellite data:', e);
    showToast('Failed to load satellite imagery', 'error');
  } finally {
    endMapLoading();
  }
}



// =============================================================================




function parseSatTimestamp(ts) {
  // ts looks like "2026-03-09T17-01-17.6Z" (dashes instead of colons in time)
  let normalized = ts.replace(/T(\d{2})-(\d{2})-[\d.]+Z?$/, 'T$1:$2:00Z');
  // ts may also use ordinal day format: "2026-073T04:21:00Z" — JS Date can't parse this
  normalized = normalized.replace(/^(\d{4})-(\d{3})T/, (_, year, doy) => {
    const ord = new Date(Date.UTC(parseInt(year, 10), 0, parseInt(doy, 10)));
    const mm = String(ord.getUTCMonth() + 1).padStart(2, '0');
    const dd = String(ord.getUTCDate()).padStart(2, '0');
    return `${year}-${mm}-${dd}T`;
  });
  const d = new Date(normalized);
  if (isNaN(d)) return { short: ts, full: ts, scrubber: ts, hourKey: ts };
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  const dow = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getUTCDay()];
  const local = centralTime(d);
  return {
    short: `${hh}${mm}Z`,
    full: `${dow} ${hh}${mm}Z (${local})`,
    scrubber: `${hh}Z`,
    hourKey: `${d.getUTCFullYear()}-${d.getUTCMonth()}-${d.getUTCDate()}-${hh}`,
  };
}

// Keep formatSatTime for backward compat (used in satLastUpdate)
function formatSatTime(ts) {
  return parseSatTimestamp(ts).full;
}

function parseRadarTimestamp(ts) {
  if (!ts || ts.length < 15) {
    return { date: null, short: ts || '--', full: ts || '--', scrubber: ts || '--', minuteKey: ts || '--' };
  }
  const y = ts.slice(0, 4);
  const mo = ts.slice(4, 6);
  const d = ts.slice(6, 8);
  const h = ts.slice(9, 11);
  const mi = ts.slice(11, 13);
  const s = ts.slice(13, 15);
  const date = new Date(`${y}-${mo}-${d}T${h}:${mi}:${s}Z`);
  if (isNaN(date.getTime())) {
    return { date: null, short: ts, full: ts, scrubber: ts, minuteKey: ts };
  }
  const dow = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][date.getUTCDay()];
  const local = centralTime(date);
  return {
    date,
    short: `${h}${mi}Z`,
    full: `${dow} ${h}${mi}Z (${local})`,
    scrubber: `${h}:${mi}`,
    minuteKey: `${y}-${mo}-${d}-${h}-${mi}`,
  };
}

function filterRadarFramesByLoopLength(frames, loopMinutes) {
  if (!Array.isArray(frames) || frames.length === 0 || !Number.isFinite(loopMinutes) || loopMinutes <= 0) {
    return frames;
  }
  const latestParsed = parseRadarTimestamp(frames[frames.length - 1]?.timestamp);
  if (!latestParsed.date) return frames;
  const cutoff = latestParsed.date.getTime() - (loopMinutes * 60 * 1000);
  const filtered = frames.filter((frame) => {
    const parsed = parseRadarTimestamp(frame.timestamp);
    return parsed.date && parsed.date.getTime() >= cutoff;
  });
  return filtered.length > 0 ? filtered : [frames[frames.length - 1]];
}

function normalizeHomeRadarFrame(frame) {
  if (!frame) return null;
  const image = frame.image || '';
  const pngPath = frame.png_path || (image ? `home/${image}` : '');
  const bounds = frame.bounds || null;
  if (!pngPath || !bounds) return null;
  return {
    image,
    png_path: String(pngPath).replace(/^\//, '').replace(/^rendered\//, ''),
    timestamp: frame.timestamp || null,
    generated_at: frame.generated_at || null,
    bounds,
    site_count: frame.site_count ?? null,
  };
}

function buildHomeRadarFallbackFrame(meta) {
  if (!meta?.image || !meta?.bounds) return null;
  return normalizeHomeRadarFrame({
    image: meta.image,
    png_path: `home/${meta.image}`,
    timestamp: meta.timestamp || null,
    generated_at: meta.generated_at || null,
    bounds: meta.bounds,
    site_count: meta.site_count ?? null,
  });
}

function showSatelliteFrame(idx) {
  const frame = state.satFrames[idx];
  if (!frame || !frame.png_path) return;

  const bounds = state.satelliteBounds || (
    frame.bounds
      ? [[frame.bounds.south, frame.bounds.west], [frame.bounds.north, frame.bounds.east]]
      : null
  );

  const url = `${BASE}/rendered/${String(frame.png_path).replace(/^\//, '')}`;

  if (state.satOverlayLayer) {
    state.satOverlayLayer.setUrl(url);
    if (bounds) state.satOverlayLayer.setBounds(bounds);
  } else {
    state.satOverlayLayer = L.imageOverlay(url, bounds || state.satelliteBounds, {
      opacity: 1.0,
      interactive: false,
      className: 'satellite-overlay',
    }).addTo(leafletMap);
    const _el = state.satOverlayLayer?.getElement?.();
    if (_el) { _el.style.mixBlendMode = 'normal'; }
  }

  // Update time display
  if (frame.timestamp) {
    const t = parseSatTimestamp(frame.timestamp);
    validTimeDisplay.textContent = `Satellite  ${t.full}`;
  }

  const overlayImg = state.satOverlayLayer?.getElement?.();
  if (overlayImg && !overlayImg.complete) {
    overlayImg.addEventListener('load', () => markCaptureReady('satellite'), { once: true });
    overlayImg.addEventListener('error', () => markCaptureReady('satellite'), { once: true });
  } else {
    markCaptureReady('satellite');
  }
  syncShareableDesktopUrl();
  preloadAllDesktopFrames();
}

function updateSatScrubber() {
  scrubber.disabled = state.satFrames.length < 2;
  const frames = state.satFrames;
  scrubber.min = 0;
  scrubber.max = Math.max(0, frames.length - 1);
  scrubber.value = state.satFrameIdx;

  scrubberTicks.classList.add('satellite-mode');
  scrubberTicks.innerHTML = '';
  if (frames.length <= 1) return;

  const hourStarts = [];
  let lastHourKey = null;
  for (let i = 0; i < frames.length; i++) {
    const parsed = parseSatTimestamp(frames[i].timestamp);
    if (parsed.hourKey !== lastHourKey) {
      hourStarts.push(i);
      lastHourKey = parsed.hourKey;
    }
  }

  let labelIndices = hourStarts.length ? hourStarts : [0];
  const maxLabels = 5;
  if (labelIndices.length > maxLabels) {
    const reduced = [];
    for (let n = 0; n < maxLabels; n++) {
      const idx = Math.round((n * (labelIndices.length - 1)) / (maxLabels - 1));
      reduced.push(labelIndices[idx]);
    }
    labelIndices = [...new Set(reduced)];
  }

  for (const i of labelIndices) {
    const tick = document.createElement('div');
    tick.className = 'scrubber-tick';
    const label = document.createElement('span');
    label.className = 'tick-label';
    label.textContent = parseSatTimestamp(frames[i].timestamp).scrubber;
    tick.appendChild(label);
    scrubberTicks.appendChild(tick);
  }
}


function updateMapInfoForSatellite() {
  const modelEl = document.getElementById('mapInfoModel');
  const productEl = document.getElementById('mapInfoProduct');
  const timeEl = document.getElementById('mapInfoTime');
  
  const satNames = {
    'goes19': 'GOES-19 East'
  };
  
  const productNames = {
    'ir': 'Infrared (Band 13)',
    'ir_14': 'Infrared (Band 14)',
    'ir_dirty': 'Dirty IR (Band 15)',
    'co2': 'CO2 Absorption (Band 16)',
    'cloud_top': 'Cloud Top (Band 11)',
    'ozone': 'Ozone (Band 12)',
    'sw_ir': 'Shortwave IR (Band 7)',
    'upper_wv': 'Upper Water Vapor (Band 8)',
    'wv': 'Water Vapor (Band 9)',
    'lower_wv': 'Lower Water Vapor (Band 10)',
    'vis': 'Visible (Band 2)',
    'vis_blue': 'Visible Blue (Band 1)',
    'veggie': 'Veggie (Band 3)',
    'cirrus': 'Cirrus (Band 4)',
    'snow_ice': 'Snow/Ice (Band 5)',
    'cloud_particle': 'Cloud Particle (Band 6)',
    'natural_color': 'Natural Color RGB',
    'day_cloud_phase': 'Day Cloud Phase RGB',
    'sandwich': 'IR/VIS Sandwich RGB',
    'pwat': 'Precipitable Water',
    'sst': 'Sea Surface Temp',
    'rain_rate': 'Rainfall Rate',
    'stability': 'Lifted Index',
    'fire': 'Fire/Hot Spot',
    'vis_1km': 'Visible 1km',
    'ir_native': 'Clean IR Native (CMIP)',
    'm1_ir_1km': 'M1 Floater IR',
    'm2_ir_1km': 'M2 Floater IR',
    'm1_vis_1km': 'M1 Floater Visible',
    'm2_vis_1km': 'M2 Floater Visible',
  };
  if (modelEl) {
    const satLabel = satNames[state.satellite] || state.satellite;
    const regionLabel = SATELLITE_REGION_NAMES[state.satRegion || ''] || state.satRegion;
    modelEl.textContent = state.satRegion ? `${satLabel} · ${regionLabel}` : satLabel;
  }
  if (productEl) productEl.textContent = productNames[state.satProduct] || state.satProduct;
  if (timeEl) {
    if (state.satMetadata && state.satMetadata.timestamp) {
      const date = new Date(state.satMetadata.timestamp);
      timeEl.textContent = date.toUTCString();
    } else {
      timeEl.textContent = 'Valid: --';
    }
  }
}

// ---------------------------------------------------------------------------
// Radar Functions
// ---------------------------------------------------------------------------
let _radarStatusData = null;
let _radarStatusInterval = null;
let _radarAutoRefreshTimer = null;
const RADAR_AUTO_REFRESH_MS = 30_000;  // poll for new scans every 30s

async function renderCurrentRadarFrameOnDemand(requestId = state.radarRequestId) {
  const { radarSite: site, radarProduct: product, radarTilt: tilt } = state;
  const frame = await apiFetch(`${BASE}/api/radar/render/${site}/${product}/${tilt}`, { timeoutMs: 60_000 });
  if (requestId !== state.radarRequestId || state.dataSource !== 'radar') return null;
  if (frame?.png_path && Number.isFinite(frame.tilt)) {
    state.radarTilt = frame.tilt;
    const select = document.getElementById('radarTiltSelect');
    if (select) {
      if (![...select.options].some((option) => Number(option.value) === frame.tilt)) {
        const option = document.createElement('option');
        option.value = String(frame.tilt);
        option.textContent = `${frame.tilt.toFixed(1)}°`;
        select.appendChild(option);
      }
      select.value = String(frame.tilt);
    }
  }
  return frame?.png_path ? frame : null;
}

async function loadRadarData() {
  const requestId = ++state.radarRequestId;
  const { radarSite: site, radarProduct: product, radarTilt: tilt } = state;
  const isCurrent = () => requestId === state.radarRequestId && state.dataSource === 'radar';
  pause();
  stopRadarAutoRefresh();
  clearLoadQueue();
  clearRadarLayers();
  state.radarFrames = [];
  state.radarFrameIdx = 0;
  updateScrubberForRadar();
  hideMapNotice();
  clearCaptureReady();
  beginMapLoading('Loading radar frames…');
  try {
    let frames = await apiFetch(`${BASE}/api/radar/frames/${site}/${product}?tilt=${tilt}`);
    if (!isCurrent()) return;
    if (!frames?.length || tilt > 0.65) {
      const frame = await renderCurrentRadarFrameOnDemand(requestId);
      if (!isCurrent()) return;
      frames = frame ? [frame] : [];
    }
    if (!frames.length) {
      showMapNotice('No radar data available for this product and elevation.');
      return;
    }
    state.radarFrames = filterRadarFramesByLoopLength(frames, state.radarLoopLength);
    state.radarFrameIdx = state.radarFrames.length - 1;
    if (sharedFramePending && SHARED_VIEW_CONFIG?.view === 'radar') {
      const sharedIdx = state.radarFrames.findIndex((frame) => String(frame.timestamp) === sharedFramePending);
      if (sharedIdx >= 0) state.radarFrameIdx = sharedIdx;
      sharedFramePending = null;
    }
    updateScrubberForRadar();
    showRadarFrame(state.radarFrameIdx);
    preloadAllDesktopFrames();
    updateMapInfoForRadar();
    updateLegend();
    startRadarAutoRefresh();
  } catch (error) {
    if (isCurrent()) {
      validTimeDisplay.textContent = 'Radar data unavailable';
      showMapNotice('Unable to load this radar product. Select a product to retry.');
    }
  } finally {
    endMapLoading();
  }
}

function startRadarAutoRefresh() {
  stopRadarAutoRefresh();
  _radarAutoRefreshTimer = setInterval(refreshRadarFrames, RADAR_AUTO_REFRESH_MS);
}

function stopRadarAutoRefresh() {
  if (_radarAutoRefreshTimer) {
    clearInterval(_radarAutoRefreshTimer);
    _radarAutoRefreshTimer = null;
  }
}

async function refreshRadarFrames() {
  if (document.hidden) return;
  if (state.dataSource !== 'radar') { stopRadarAutoRefresh(); return; }
  const requestId = state.radarRequestId;
  try {
    const response = await fetch(`${BASE}/api/radar/frames/${state.radarSite}/${state.radarProduct}?tilt=${state.radarTilt}`);
    if (!response.ok) return;
    let frames = await response.json();
    if (state.dataSource !== 'radar' || requestId !== state.radarRequestId) return;
    if (state.radarTilt > 0.65) {
      const frame = await renderCurrentRadarFrameOnDemand(requestId);
      if (requestId !== state.radarRequestId || state.dataSource !== 'radar') return;
      frames = frame ? [frame] : [];
    }
    if (!frames || !frames.length) return;

    frames = filterRadarFramesByLoopLength(frames, state.radarLoopLength);

    // Determine if user is at or near the latest frame
    const wasAtEnd = state.radarFrameIdx >= state.radarFrames.length - 1;
    const oldCount = state.radarFrames.length;
    const oldLatest = state.radarFrames.length ? state.radarFrames[state.radarFrames.length - 1]?.timestamp : null;
    const newLatest = frames[frames.length - 1]?.timestamp;

    // No new data
    if (newLatest === oldLatest && frames.length === oldCount) return;

    const selectedTimestamp = state.radarFrames[state.radarFrameIdx]?.timestamp;
    state.radarFrames = frames;
    state.radarFrameIdx = wasAtEnd ? frames.length - 1 : Math.max(0, frames.findIndex((frame) => frame.timestamp === selectedTimestamp));

    showRadarFrame(state.radarFrameIdx);
    // Always update scrubber to reflect new frame count
    updateScrubberForRadar();
    preloadAllDesktopFrames();
    updateMapInfoForRadar();
  } catch (e) {
    console.log('Radar auto-refresh failed:', e);
    showToast('Radar auto-refresh failed', 'error');
  }
}

function updateScrubberForRadar() {
  syncScrubberVisibility();
  const frameCount = state.radarFrames.length;
  scrubber.disabled = frameCount < 2;
  document.getElementById('frameCounter').textContent = frameCount ? `${state.radarFrameIdx + 1}/${frameCount}` : '—';
  scrubberTicks.classList.remove('satellite-mode');
  scrubberTicks.innerHTML = '';
  
  scrubber.max = Math.max(0, frameCount - 1);
  scrubber.value = state.radarFrameIdx;

  if (frameCount > 1) {
    let labelIndices = [];
    const maxLabels = Math.min(6, frameCount);
    if (frameCount <= maxLabels) {
      labelIndices = Array.from({ length: frameCount }, (_, idx) => idx);
    } else {
      for (let n = 0; n < maxLabels; n += 1) {
        labelIndices.push(Math.round((n * (frameCount - 1)) / (maxLabels - 1)));
      }
      labelIndices = [...new Set(labelIndices)];
    }

    for (const idx of labelIndices) {
      const tick = document.createElement('div');
      tick.className = 'scrubber-tick';
      const label = document.createElement('span');
      label.className = 'tick-label';
      label.textContent = parseRadarTimestamp(state.radarFrames[idx]?.timestamp).scrubber;
      tick.appendChild(label);
      scrubberTicks.appendChild(tick);
    }
  }
  
  // Update frame counter
  const frameCounter = document.getElementById('frameCounter');
  if (frameCounter) {
    frameCounter.textContent = frameCount > 0 ? `${state.radarFrameIdx + 1}/${frameCount}` : '--';
  }
}

function updateMapInfoForRadar() {
  const modelEl = document.getElementById('mapInfoModel');
  const productEl = document.getElementById('mapInfoProduct');
  const timeEl = document.getElementById('mapInfoTime');
  
  const radarNames = {
    'KOAX': 'Omaha (KOAX)',
    'KTLX': 'Oklahoma City (KTLX)',
    'KDMX': 'Des Moines (KDMX)',
    'KICT': 'Wichita (KICT)',
    'KTWX': 'Topeka (KTWX)',
    'KEAX': 'Kansas City (KEAX)',
    'KFWS': 'Dallas/FW (KFWS)',
    'KAMA': 'Amarillo (KAMA)',
    'KGLD': 'Goodland (KGLD)',
    'KFTG': 'Denver (KFTG)'
  };
  
  const productNames = {
    'REF': 'Reflectivity',
    'VEL': 'Velocity',
    'SW':  'Spectrum Width',
    'ZDR': 'Diff Reflectivity',
    'CC':  'Correlation Coeff',
    'PHI': 'Diff Phase',
    'KDP': 'Specific Diff Phase',
    'SRM': 'Storm-Relative Velocity',
    'AZS': 'Azimuthal Shear',
    'DIV': 'Divergence / Convergence',
    'CCT': 'CC Texture',
    'PHT': 'PHI Texture',
    'TDS': 'Debris Signature',
    'CREF': 'Composite Reflectivity',
    'ETOP': 'Echo Tops',
    'VIL': 'Vertically Integrated Liquid',
    'VXT': 'Velocity Texture',
    'HAIL': 'Hail Probability',
    'RAIN': 'Rainfall Rate',
  };

  const tiltStr = state.radarTilt !== undefined ? ` (${state.radarTilt}°)` : '';
  if (modelEl) modelEl.textContent = radarNames[state.radarSite] || state.radarSite;
  if (productEl) productEl.textContent = (productNames[state.radarProduct] || state.radarProduct) + tiltStr;
  
  // Display timestamp from current frame if available
  if (timeEl) {
    const frame = state.radarFrames[state.radarFrameIdx];
    if (frame && frame.timestamp && frame.timestamp.length >= 15) {
      timeEl.textContent = parseRadarTimestamp(frame.timestamp).full;
    } else {
      timeEl.textContent = '--';
    }
  }
}

async function refreshRadarStatus() {
  try {
    const resp = await fetch(`${BASE}/api/radar/status`);
    if (resp.ok) {
      _radarStatusData = await resp.json();
      renderRadarStatus();
    }
  } catch (e) {
    console.log('Failed to refresh radar status:', e);
  }
}

function renderRadarStatus() {
  if (!_radarStatusData) return;
  
  const site = _radarStatusData[state.radarSite];
  if (!site) return;
  
  const product = site.products[state.radarProduct];
  if (!product) return;
  
  // Update any status indicators if they exist
  const statusEl = document.getElementById('radarStatus');
  if (statusEl) {
    const statusClass = product.status === 'live' ? 'live' : 
                       product.status === 'recent' ? 'recent' : 
                       product.status === 'stale' ? 'stale' : 'offline';
    statusEl.className = `radar-status ${statusClass}`;
    statusEl.textContent = product.status.toUpperCase();
  }
  
  // Update age display
  const ageEl = document.getElementById('radarAge');
  if (ageEl && product.age_min !== null) {
    ageEl.textContent = product.age_min < 1 
      ? '<1 min ago' 
      : `${Math.round(product.age_min)} min ago`;
  }
  
  // Update frame count
  const frameCountEl = document.getElementById('radarFrameCount');
  if (frameCountEl) {
    frameCountEl.textContent = `${product.frames} frames`;
  }
}

function clearOverlayLayer(key) {
  if (state[key]) {
    leafletMap.removeLayer(state[key]);
    state[key] = null;
  }
  if (key === 'spcLayer' && state.spcHatchLayer) {
    leafletMap.removeLayer(state.spcHatchLayer);
    state.spcHatchLayer = null;
  }
}

async function refreshSevereWarnings() {
  if (document.hidden) return;
  try {
    state.severeWarnings = await apiFetch(`${BASE}/api/alerts/severe?limit=24`);
    renderSevereWarnings();
  } catch (e) {
    console.error('Failed to refresh severe warnings:', e);
  }
}

function renderSevereWarnings() {
  const metaEl = document.getElementById('severeWarningMeta');
  const listEl = document.getElementById('severeWarningList');
  if (!metaEl || !listEl || !state.severeWarnings) return;

  const items = state.severeWarnings.items || [];
  metaEl.textContent = `${items.length} active · updated ${formatMinutesAgo(state.severeWarnings.updated)}`;

  if (!items.length) {
    listEl.innerHTML = '<div class="warning-empty">No active tornado or severe thunderstorm warnings.</div>';
    return;
  }

  listEl.innerHTML = items.map(item => {
    const tone = item.event === 'Tornado Warning' ? 'tornado' : 'severe';
    const timeText = formatMinutesLeft(item.minutes_left);
    const lat = item.centroid && Number.isFinite(item.centroid.lat) ? item.centroid.lat : '';
    const lon = item.centroid && Number.isFinite(item.centroid.lon) ? item.centroid.lon : '';
    return `<div class="warning-item ${tone}" data-warning-lat="${escapeHtml(String(lat))}" data-warning-lon="${escapeHtml(String(lon))}" role="button" tabindex="0">
      <div class="warning-item-header">
        <div class="warning-item-event">${escapeHtml(item.event)}</div>
        <div class="warning-item-time">${escapeHtml(timeText)}</div>
      </div>
      <div class="warning-item-title">${escapeHtml(item.headline || item.event)}</div>
      <div class="warning-item-area">${escapeHtml(item.area_desc || item.severity || '')}</div>
    </div>`;
  }).join('');

  listEl.querySelectorAll('.warning-item').forEach(el => {
    const activate = async () => {
      const lat = parseFloat(el.dataset.warningLat || '');
      const lon = parseFloat(el.dataset.warningLon || '');
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
      await jumpToWarningRadar(lat, lon);
    };
    el.addEventListener('click', activate);
    el.addEventListener('keydown', (evt) => {
      if (evt.key === 'Enter' || evt.key === ' ') {
        evt.preventDefault();
        activate();
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Home Dashboard
// ---------------------------------------------------------------------------

async function initHomeDashboard() {
  const dashboard = document.getElementById('homeDashboard');
  if (dashboard) dashboard.classList.remove('hidden');

  // Set home defaults
  state.showSpotters = true;
  state.radarCompositeEnabled = false;
  state.homeMcdEnabled = false;
  state.homeSpcDay = 'day1';
  state.homeSpcView = 'overview';
  state.homeAlertTypes = new Set([
    'Tornado Warning', 'Tornado Emergency', 'Severe Thunderstorm Warning', 'Flash Flood Warning',
  ]);

  // Sync UI — radar & spotters
  const hrc = document.getElementById('homeRadarComposite');
  if (hrc) hrc.checked = state.radarCompositeEnabled;
  const hst = document.getElementById('homeSpotterToggle');
  if (hst) hst.checked = true;
  const hmt = document.getElementById('homeMcdToggle');
  if (hmt) hmt.checked = false;
  applyHomeLayerPanelState();
  applyHomeSidePanelState();

  // Sync alert type checkboxes
  document.querySelectorAll('[data-alert-type]').forEach(cb => {
    cb.checked = state.homeAlertTypes.has(cb.dataset.alertType);
  });

  document.querySelectorAll('[data-spc-day]').forEach((input) => {
    input.checked = input.dataset.spcDay === state.homeSpcDay;
  });
  document.querySelectorAll('[data-spc-view]').forEach((input) => {
    input.checked = input.dataset.spcView === state.homeSpcView;
  });

  // Wire up layer controls
  wireHomeLayerControls();

  // Wire up side panel tabs
  wireHomeSidePanelTabs();

  // Init layers
  await Promise.all([initRainViewerLayer(), refreshAlertOverlay(), initWarningTicker()]);
  refreshSpotterOverlay();
  refreshFeaturedChasers();
  refreshMcdOverlay();

  // Load side panel data
  loadHomeSidePanelData();
  loadChaserPanel();

  // Auto-refresh featured chaser dots every 2 minutes
  if (state.featuredChaserTimer) clearInterval(state.featuredChaserTimer);
  state.featuredChaserTimer = setInterval(() => {
    if (document.hidden) return;
    if (['home', 'radar'].includes(state.dataSource)) refreshFeaturedChasers();
  }, 120_000);
}

function cleanupHomeDashboard() {
  // Remove site layers and composite
  clearHomeSiteLayers();
  state.homeSiteLayersActive = false;
  if (state.radarCompositeLayer) {
    leafletMap.removeLayer(state.radarCompositeLayer);
    state.radarCompositeLayer = null;
  }
  state.homeRadarFrames = [];
  state.homeRadarFrameIdx = 0;
  // Remove multi-SPC layers
  clearHomeSpcLayers();
  clearHomeMcdLayer();
  // Remove alerts layer
  clearOverlayLayer('alertsLayer');
  clearOverlayLayer('spotterLayer');
  // Stop ticker
  if (state.homeWarningTickerTimer) {
    clearInterval(state.homeWarningTickerTimer);
    state.homeWarningTickerTimer = null;
  }
  // Stop side panel timers
  if (state.homeSidePanelTimer) {
    clearInterval(state.homeSidePanelTimer);
    state.homeSidePanelTimer = null;
  }
  if (state.homeChaserTimer) {
    clearInterval(state.homeChaserTimer);
    state.homeChaserTimer = null;
  }
  if (state.homeRainViewerTimer) {
    clearInterval(state.homeRainViewerTimer);
    state.homeRainViewerTimer = null;
  }
  state.homeSeenWarningIds.clear();
  syncScrubberVisibility();
}

// --- Home page: zoom-in individual site layer switching ---

// Image cache: png_path -> HTMLImageElement (preloaded, reused across redraws)
const _homeSiteImageCache = {};
let _sitePreloadPromise = null; // resolves when all site images are in cache

// Fixed CONUS bounds — canvas always covers the same area so Leaflet never jumps
const _SITE_CANVAS_BOUNDS = { south: 23, north: 53, west: -129, east: -66 };
const _SITE_CANVAS_W = 9600;
const _SITE_CANVAS_H = 5600;

function _mercY(lat) { return Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)); }
const _mNorth = _mercY(_SITE_CANVAS_BOUNDS.north);
const _mSouth = _mercY(_SITE_CANVAS_BOUNDS.south);
function _latToY(lat) { return (_mNorth - _mercY(lat)) / (_mNorth - _mSouth) * _SITE_CANVAS_H; }
function _lonToX(lon) { return (lon - _SITE_CANVAS_BOUNDS.west) / (_SITE_CANVAS_BOUNDS.east - _SITE_CANVAS_BOUNDS.west) * _SITE_CANVAS_W; }

// Preload all site images — stores a promise so loadHomeSiteLayers can await it
function preloadHomeSiteImages() {
  _sitePreloadPromise = (async () => {
    try {
      const resp = await fetch(`${BASE}/api/radar/site_layers?_=${Date.now()}`, { cache: 'no-store' });
      if (!resp.ok) return;
      const sites = await resp.json();
      await Promise.all(sites.map(s => new Promise(resolve => {
        if (_homeSiteImageCache[s.png_path]?.complete && _homeSiteImageCache[s.png_path].naturalWidth) {
          resolve(); return;
        }
        const img = new Image();
        img.onload = () => { _homeSiteImageCache[s.png_path] = img; resolve(); };
        img.onerror = resolve;
        img.src = `${BASE}/rendered/${s.png_path}`;
        _homeSiteImageCache[s.png_path] = img;
      })));
    } catch {}
  })();
  return _sitePreloadPromise;
}

function clearHomeSiteLayers() {
  if (state.homeSiteCanvasLayer) {
    leafletMap.removeLayer(state.homeSiteCanvasLayer);
    state.homeSiteCanvasLayer = null;
  }
  if (state.homeSiteCanvasUrl) {
    URL.revokeObjectURL(state.homeSiteCanvasUrl);
    state.homeSiteCanvasUrl = null;
  }
  if (state.homeSiteLayersTimer) {
    clearInterval(state.homeSiteLayersTimer);
    state.homeSiteLayersTimer = null;
  }
}

async function loadHomeSiteLayers() {
  if (!state.radarCompositeEnabled || !leafletMap) return;

  // Wait for preload to finish — ensures all images are in cache before we draw
  if (_sitePreloadPromise) await _sitePreloadPromise;

  let sites;
  try {
    const resp = await fetch(`${BASE}/api/radar/site_layers?_=${Date.now()}`, { cache: 'no-store' });
    if (!resp.ok) return;
    sites = await resp.json();
  } catch { return; }
  if (!sites.length) return;

  const canvas = document.createElement('canvas');
  canvas.width  = _SITE_CANVAS_W;
  canvas.height = _SITE_CANVAS_H;
  const ctx = canvas.getContext('2d');

  // Use cached images where available; fetch fresh ones otherwise
  await Promise.all(sites.map(s => new Promise(resolve => {
    const cached = _homeSiteImageCache[s.png_path];
    const draw = (img) => {
      const b = s.bounds;
      const x = _lonToX(b.west),    y = _latToY(b.north);
      const w = _lonToX(b.east) - x, h = _latToY(b.south) - y;
      if (w > 0 && h > 0) ctx.drawImage(img, x, y, w, h);
      resolve();
    };
    if (cached?.complete && cached.naturalWidth) { draw(cached); return; }
    const img = new Image();
    img.onload = () => { _homeSiteImageCache[s.png_path] = img; draw(img); };
    img.onerror = resolve;
    img.src = `${BASE}/rendered/${s.png_path}`;
  })));

  const url = await new Promise(resolve =>
    canvas.toBlob(blob => resolve(URL.createObjectURL(blob)), 'image/png')
  );

  const bounds = [[_SITE_CANVAS_BOUNDS.south, _SITE_CANVAS_BOUNDS.west],
                  [_SITE_CANVAS_BOUNDS.north, _SITE_CANVAS_BOUNDS.east]];
  const oldUrl = state.homeSiteCanvasUrl;
  state.homeSiteCanvasUrl = url;

  if (state.homeSiteCanvasLayer) {
    state.homeSiteCanvasLayer.setUrl(url);
  } else {
    state.homeSiteCanvasLayer = L.imageOverlay(url, bounds, {
      pane: 'radarCompositePane',
      opacity: 0.78,
      interactive: false,
      className: 'satellite-overlay',
    }).addTo(leafletMap);
  }

  if (oldUrl) setTimeout(() => URL.revokeObjectURL(oldUrl), 2000);
}

function updateHomeRadarMode() {
  if (!leafletMap || !state.radarCompositeEnabled) return;
  const shouldUseSites = leafletMap.getZoom() > HOME_RADAR_ZOOM_THRESHOLD;

  if (shouldUseSites && !state.homeSiteLayersActive) {
    state.homeSiteLayersActive = true;
    // Keep composite visible until canvas is ready, then remove it
    loadHomeSiteLayers().then(() => {
      if (state.homeSiteLayersActive && state.radarCompositeLayer) {
        leafletMap.removeLayer(state.radarCompositeLayer);
        state.radarCompositeLayer = null;
      }
    });
    if (!state.homeSiteLayersTimer) {
      state.homeSiteLayersTimer = setInterval(() => {
    if (document.hidden) return;
        if (state.dataSource === 'home' && state.homeSiteLayersActive) loadHomeSiteLayers();
      }, 120_000);
    }
  } else if (!shouldUseSites && state.homeSiteLayersActive) {
    state.homeSiteLayersActive = false;
    showHomeRadarFrame(state.homeRadarFrameIdx); // restore composite first
    clearHomeSiteLayers();                       // then remove site canvas
  }
}

// --- Cached local national radar composite ---
// Defer loop-frame and site-image preloading until the visible frame has
// painted, so first page load isn't competing with tens of MB of background
// fetches. Loop frames load first; site PNGs (~89 images) follow.
let _homeBgPreloadTimer = null;
function scheduleHomeBackgroundPreload() {
  if (_homeBgPreloadTimer) clearTimeout(_homeBgPreloadTimer);
  let started = false;
  const start = () => {
    if (started) return;
    started = true;
    if (_homeBgPreloadTimer) { clearTimeout(_homeBgPreloadTimer); _homeBgPreloadTimer = null; }
    if (state.dataSource !== 'home' || !state.radarCompositeEnabled) return;
    preloadAllDesktopFrames();
    setTimeout(() => {
      if (state.dataSource === 'home' && state.radarCompositeEnabled) preloadHomeSiteImages();
    }, 4000);
  };
  const overlayImg = state.radarCompositeLayer?.getElement?.();
  if (overlayImg && !overlayImg.complete) {
    overlayImg.addEventListener('load', start, { once: true });
    overlayImg.addEventListener('error', start, { once: true });
    _homeBgPreloadTimer = setTimeout(start, 8000); // fallback if load never fires
  } else {
    _homeBgPreloadTimer = setTimeout(start, 500);
  }
}

async function initRainViewerLayer() {
  if (state.radarCompositeLayer) {
    leafletMap.removeLayer(state.radarCompositeLayer);
    state.radarCompositeLayer = null;
  }
  state.homeRadarFrames = [];
  state.homeRadarFrameIdx = 0;
  if (!state.radarCompositeEnabled) {
    syncScrubberVisibility();
    return;
  }

  const requestId = ++state.homeRadarRequestId;
  try {
    let frames = [];
    try {
      const framesResp = await fetch(`${BASE}/rendered/home/radar_composite_frames.json?_=${Date.now()}`, { cache: 'no-store' });
      if (framesResp.ok) {
        const framesData = await framesResp.json();
        if (Array.isArray(framesData)) {
          frames = framesData.map(normalizeHomeRadarFrame).filter(Boolean);
        }
      }
    } catch {}

    if (!frames.length) {
      const metaResp = await fetch(`${BASE}/rendered/home/radar_composite_latest.json?_=${Date.now()}`, { cache: 'no-store' });
      if (!metaResp.ok) throw new Error(`composite meta ${metaResp.status}`);
      const meta = await metaResp.json();
      const fallback = buildHomeRadarFallbackFrame(meta);
      if (!fallback) throw new Error('composite meta incomplete');
      frames = [fallback];
    }

    if (requestId !== state.homeRadarRequestId) return;
    frames = filterRadarFramesByLoopLength(frames, state.radarLoopLength);
    clearLoadQueue();
    state.homeRadarFrames = frames;
    state.homeRadarFrameIdx = Math.max(0, frames.length - 1);
    updateScrubberForHomeRadar();
    showHomeRadarFrame(state.homeRadarFrameIdx); // visible frame loads via the overlay itself
    updateMapInfoForHomeRadar();
    updateLegend();
    updateHomeRadarMode();
    scheduleHomeBackgroundPreload();
  } catch (e) {
    console.warn('Local radar composite init failed:', e);
    syncScrubberVisibility();
  }

  // Auto-refresh every 2 min so newly generated composites appear without a reload.
  if (state.homeRainViewerTimer) clearInterval(state.homeRainViewerTimer);
  state.homeRainViewerTimer = setInterval(() => {
    if (document.hidden) return;
    if (state.dataSource === 'home' && state.radarCompositeEnabled) {
      initRainViewerLayer();
    }
  }, 120_000);
}

function updateScrubberForHomeRadar() {
  syncScrubberVisibility();
  const frames = state.homeRadarFrames;
  const frameCount = frames.length;
  scrubberTicks.classList.remove('satellite-mode');
  scrubberTicks.innerHTML = '';
  scrubber.min = 0;
  scrubber.max = Math.max(0, frameCount - 1);
  scrubber.value = state.homeRadarFrameIdx;

  if (frameCount > 1) {
    let labelIndices = [];
    const maxLabels = Math.min(6, frameCount);
    if (frameCount <= maxLabels) {
      labelIndices = Array.from({ length: frameCount }, (_, idx) => idx);
    } else {
      for (let n = 0; n < maxLabels; n += 1) {
        labelIndices.push(Math.round((n * (frameCount - 1)) / (maxLabels - 1)));
      }
      labelIndices = [...new Set(labelIndices)];
    }
    for (const idx of labelIndices) {
      const tick = document.createElement('div');
      tick.className = 'scrubber-tick';
      const label = document.createElement('span');
      label.className = 'tick-label';
      label.textContent = parseRadarTimestamp(frames[idx]?.timestamp).scrubber;
      tick.appendChild(label);
      scrubberTicks.appendChild(tick);
    }
  }

  const frameCounter = document.getElementById('frameCounter');
  if (frameCounter) {
    frameCounter.textContent = frameCount > 0 ? `${state.homeRadarFrameIdx + 1}/${frameCount}` : '--';
  }
}

function showHomeRadarFrame(idx) {
  const frame = state.homeRadarFrames[idx];
  if (!frame) return;
  const bounds = [[frame.bounds.south, frame.bounds.west], [frame.bounds.north, frame.bounds.east]];
  // Stable URL matching preload cache
  const token = encodeURIComponent(frame.generated_at || frame.timestamp || "");
  const url = `${BASE}/rendered/${frame.png_path}?v=${token}`;

  if (state.radarCompositeLayer) {
    state.radarCompositeLayer.setUrl(url);
    state.radarCompositeLayer.setBounds(bounds);
  } else {
    state.radarCompositeLayer = L.imageOverlay(url, bounds, {
      pane: 'radarCompositePane',
      opacity: 0.78,
      interactive: false,
      className: 'satellite-overlay',
    }).addTo(leafletMap);
  }

  if (validTimeDisplay) {
    validTimeDisplay.textContent = frame.timestamp
      ? `Composite: ${parseRadarTimestamp(frame.timestamp).full}`
      : 'Composite: --';
  }
  const frameCounter = document.getElementById('frameCounter');
  if (frameCounter) frameCounter.textContent = `${idx + 1}/${state.homeRadarFrames.length}`;
  markCaptureReady('home_radar');
  preloadAllDesktopFrames();
}

function updateMapInfoForHomeRadar() {
  const modelEl = document.getElementById('mapInfoModel');
  const productEl = document.getElementById('mapInfoProduct');
  const timeEl = document.getElementById('mapInfoTime');
  if (modelEl) modelEl.textContent = 'CONUS Radar';
  if (productEl) productEl.textContent = 'Reflectivity Composite';
  if (timeEl) {
    const frame = state.homeRadarFrames[state.homeRadarFrameIdx];
    timeEl.textContent = frame?.timestamp ? parseRadarTimestamp(frame.timestamp).full : '--';
  }
}

// --- Warning Panel (Side Panel) ---
async function initWarningTicker() {
  await refreshWarningsPanel();
  if (state.homeWarningTickerTimer) clearInterval(state.homeWarningTickerTimer);
  state.homeWarningTickerTimer = setInterval(() => {
    if (document.hidden) return;
    if (state.dataSource === 'home') refreshWarningsPanel();
  }, 45_000);
}

function switchSidePanel(panelName) {
  // panelName: 'quicklooks', 'warnings', or 'chasers'
  document.querySelectorAll('.side-tab').forEach(t => t.classList.toggle('active', t.dataset.panel === panelName));
  document.querySelectorAll('.side-panel-pane').forEach(p => {
    const paneName = p.id.replace('sidePanel', '').toLowerCase();
    p.classList.toggle('active', paneName === panelName);
  });
}

async function refreshWarningsPanel() {
  const listEl = document.getElementById('warningList');
  const headerEl = document.getElementById('warningListHeader');
  const badgeEl = document.getElementById('warningBadge');
  if (!listEl) return;

  try {
    const data = await apiFetch(`${BASE}/api/alerts/severe?limit=30`);
    const items = data.items || [];

    // Update badge count and tab highlight
    if (badgeEl) {
      badgeEl.textContent = items.length;
      badgeEl.style.display = items.length > 0 ? 'inline-flex' : 'none';
    }
    const warningsTab = document.getElementById('warningsTab');
    if (warningsTab) {
      warningsTab.classList.toggle('has-warnings', items.length > 0);
    }

    // Auto-switch tabs based on warning state
    if (items.length > 0) {
      // Warnings exist - switch to warnings tab
      switchSidePanel('warnings');
    } else {
      // No warnings - switch back to quicklooks
      switchSidePanel('quicklooks');
    }

    // Update header
    if (headerEl) {
      const updated = data.updated ? formatMinutesAgo(data.updated) : 'just now';
      headerEl.textContent = items.length 
        ? `${items.length} active warning${items.length !== 1 ? 's' : ''} · updated ${updated}`
        : 'No active warnings';
    }

    if (!items.length) {
      listEl.innerHTML = '<div class="warning-empty">No active tornado, severe thunderstorm, or flash flood warnings.</div>';
      return;
    }

    const html = items.map(item => {
      const event = item.event || '';
      let toneClass = 'warning-card--svr';
      if (event.includes('Tornado')) toneClass = 'warning-card--tor';
      else if (event.includes('Flash Flood')) toneClass = 'warning-card--ffw';

      const key = item.headline || event + (item.area_desc || '');
      const isNew = !state.homeSeenWarningIds.has(key);
      const flashClass = isNew ? ' warning-card--new' : '';
      state.homeSeenWarningIds.add(key);

      const lat = item.centroid?.lat ?? '';
      const lon = item.centroid?.lon ?? '';
      const timeLeft = item.minutes_left !== null && item.minutes_left !== undefined
        ? formatMinutesLeft(item.minutes_left)
        : '';

      return `<div class="warning-card ${toneClass}${flashClass}" data-lat="${escapeHtml(String(lat))}" data-lon="${escapeHtml(String(lon))}">
        <div class="warning-card__event">${escapeHtml(event)}</div>
        <div class="warning-card__area">${escapeHtml(item.area_desc || item.headline || '')}</div>
        <div class="warning-card__meta">
          <span class="warning-card__time">${escapeHtml(timeLeft)}</span>
        </div>
      </div>`;
    }).join('');

    listEl.innerHTML = html;

    // Bind click handlers
    listEl.querySelectorAll('.warning-card[data-lat]').forEach(el => {
      el.addEventListener('click', async () => {
        const lat = parseFloat(el.dataset.lat);
        const lon = parseFloat(el.dataset.lon);
        if (Number.isFinite(lat) && Number.isFinite(lon)) {
          await jumpToWarningRadar(lat, lon);
        }
      });
    });
  } catch (e) {
    console.warn('Warnings panel fetch failed:', e);
    if (headerEl) headerEl.textContent = 'Failed to load warnings';
  }
}

// --- Side Panel: Quicklooks Tab ---
async function loadHomeSidePanelData() {
  const pane = document.getElementById('sidePanelQuicklooks');
  if (!pane) return;

  try {
    const data = await apiFetch(`${BASE}/api/home/overview`);
    let html = '';

    const cards = (data.quicklook_cards || []).slice(0, 4);
    for (const item of cards) {
      const kind = item.kind || 'model';
      const action = kind === 'satellite' ? 'satellite' : kind === 'radar' ? 'radar' : 'model';
      const kicker = kind === 'radar'
        ? (item.site || 'RADAR')
        : kind === 'satellite'
          ? 'SATELLITE'
          : (item.model || 'MODEL').toUpperCase();
      let meta = item.subtitle || '';
      if (kind === 'radar' && item.minutes_left != null) {
        meta = `${meta}${meta ? ' \u00b7 ' : ''}${formatMinutesLeft(item.minutes_left)}`;
      }

      html += `<div class="side-ql-item" data-action="${escapeHtml(action)}" data-site="${escapeHtml(item.site || '')}" data-lat="${escapeHtml(String(item.lat ?? ''))}" data-lon="${escapeHtml(String(item.lon ?? ''))}" data-model="${escapeHtml(item.model || '')}" data-run="${escapeHtml(item.run_id || '')}" data-param="${escapeHtml(item.parameter || '')}" data-satellite="${escapeHtml(item.satellite || 'goes19')}" data-product="${escapeHtml(item.product || 'ir')}">
        <div class="side-ql-kicker">${escapeHtml(kicker)}</div>
        <div class="side-ql-title">${escapeHtml(item.title || 'Quicklook')}</div>
        <div class="side-ql-meta">${escapeHtml(meta)}</div>
        ${item.image_path ? `<div class="side-ql-img-wrap"><img class="side-ql-img" src="${escapeHtml(assetPath(item.image_path))}" alt="${escapeHtml(item.title || 'Quicklook')}" /></div>` : ''}
      </div>`;
    }

    if (!html) html = '<div class="side-ql-meta" style="padding:12px;text-align:center;">No quicklooks available</div>';

    pane.innerHTML = html;
    attachImageFallbacks(pane);

    // Bind clicks
    pane.querySelectorAll('.side-ql-item').forEach(el => {
      el.addEventListener('click', async () => {
        const action = el.dataset.action;
        if (action === 'radar') {
          const site = el.dataset.site;
          if (site) {
            switchDataSource('radar');
            pause();
            clearRadarLayers();
            state.radarSite = site;
            persistRadarSite(site);
            state.radarTilt = 0.5;
            const sel = document.getElementById('radarSiteSelect');
            if (sel) sel.value = site;
            await flyToRadarSite(site);
            await loadRadarTilts();
            await loadRadarData();
            updateMapInfoForRadar();
          }
        } else if (action === 'model') {
          const model = el.dataset.model;
          const runId = el.dataset.run;
          const param = el.dataset.param;
          if (model && runId && param) {
            switchDataSource('models');
            pause();
            state.model = model;
            state.parameter = param;
            if (modelSelect) modelSelect.value = model;
            await loadRuns();
            if (runSelect) runSelect.value = runId;
            await selectRun(runId);
            updateParamVisibilityForModel(model);
            updateParameterButtons();
            updateMapInfoForModel();
          }
        } else if (action === 'satellite') {
          switchDataSource('satellite');
          state.satellite = el.dataset.satellite || 'goes19';
          state.satProduct = el.dataset.product || 'ir';
          const satSel = document.getElementById('satelliteSelect');
          if (satSel) satSel.value = state.satellite;
          await loadSatelliteData();
          updateMapInfoForSatellite();
        }
      });
    });
  } catch (e) {
    if (pane) pane.innerHTML = '<div class="side-ql-meta" style="padding:12px;">Failed to load quicklooks</div>';
  }

  // Auto-refresh every 60s
  if (state.homeSidePanelTimer) clearInterval(state.homeSidePanelTimer);
  state.homeSidePanelTimer = setInterval(() => {
    if (document.hidden) return;
    if (state.dataSource === 'home') loadHomeSidePanelData();
  }, 60_000);
}

// --- Side Panel: Chasers Tab ---
async function loadChaserPanel() {
  const list = document.getElementById('chaserList');
  if (list && !state.chaserItems.length) {
    list.innerHTML = '<div class="side-ql-meta" style="padding:8px;text-align:center;color:var(--text-dim);">Loading spotters...</div>';
  }
  try {
    const data = await apiFetch(`${BASE}/api/spotters/ranked?limit=2000`);
    state.chaserItems = data.items || [];
    renderChaserList(state.chaserItems);
  } catch (e) {
    console.warn('Chaser panel fetch failed:', e);
    if (list && !state.chaserItems.length) {
      list.innerHTML = '<div class="side-ql-meta" style="padding:8px;text-align:center;color:var(--text-dim);">Spotter network unavailable</div>';
    }
  }

  // Auto-refresh every 90s
  if (state.homeChaserTimer) clearInterval(state.homeChaserTimer);
  state.homeChaserTimer = setInterval(() => {
    if (document.hidden) return;
    if (state.dataSource === 'home') loadChaserPanel();
  }, 90_000);
}

function renderChaserList(items) {
  const list = document.getElementById('chaserList');
  if (!list) return;

  if (!items.length) {
    list.innerHTML = '<div class="side-ql-meta" style="padding:8px;">No active chasers</div>';
    return;
  }

  list.innerHTML = items.map((item, i) => {
    const dots = Array.from({length: 5}, (_, d) =>
      `<span class="chaser-dot${d < item.activity_score ? ' active' : ''}"></span>`
    ).join('');

    return `<div class="chaser-item" data-lat="${escapeHtml(String(item.lat ?? ''))}" data-lon="${escapeHtml(String(item.lon ?? ''))}">
      <span class="chaser-rank">${i + 1}</span>
      <div class="chaser-info">
        <div class="chaser-name">${escapeHtml(item.name)}</div>
        <div class="chaser-status">${escapeHtml(item.status || '--')}</div>
      </div>
      <div class="chaser-dots">${dots}</div>
    </div>`;
  }).join('');

  // Bind clicks
  list.querySelectorAll('.chaser-item').forEach(el => {
    el.addEventListener('click', async () => {
      const lat = parseFloat(el.dataset.lat);
      const lon = parseFloat(el.dataset.lon);
      if (Number.isFinite(lat) && Number.isFinite(lon)) {
        await jumpToChaserLocation(lat, lon);
      }
    });
  });
}

async function jumpToChaserLocation(lat, lon) {
  // Ensure site locations are loaded before finding nearest
  if (!_radarSiteLocations) {
    try {
      const resp = await fetch(`${BASE}/api/radar/sites`);
      if (resp.ok) {
        const data = await resp.json();
        _radarSiteLocations = data.sites || {};
      }
    } catch {}
  }
  const nearestSite = nearestRadarSite(lat, lon);
  if (!nearestSite) return;

  // Set state BEFORE switchDataSource so it flies to the right site immediately
  pause();
  state.radarSite = nearestSite;
  persistRadarSite(nearestSite);
  state.radarTilt = 0.5;

  const radarSiteSelect = document.getElementById('radarSiteSelect');
  if (radarSiteSelect) radarSiteSelect.value = nearestSite;
  const radarTiltSelect = document.getElementById('radarTiltSelect');
  if (radarTiltSelect) radarTiltSelect.value = '0.5';

  // Enable warnings and spotters on the radar page
  state.showWatchesWarnings = true;
  if (watchWarningToggle) watchWarningToggle.checked = true;
  
  // Enable spotters
  state.showSpotters = true;
  if (spotterToggle) spotterToggle.checked = true;

  switchDataSource('radar'); // loads data and kicks off initial flyTo

  // scheduleLeafletResize inside switchDataSource calls invalidateSize at ~180ms,
  // which cancels the in-flight flyTo. Re-fire after it settles.
  setTimeout(() => flyToRadarSite(nearestSite), 300);
}

// --- Layer Control Wiring ---
function wireHomeLayerControls() {
  if (homeLayerPanelToggle) {
    homeLayerPanelToggle.onclick = () => {
      state.homeLayerPanelCollapsed = !state.homeLayerPanelCollapsed;
      applyHomeLayerPanelState();
      persistHomeLayerPanelCollapsed(state.homeLayerPanelCollapsed);
      if (state.dataSource === 'home') scheduleUsaViewportRefresh(true);
    };
  }
  if (homeSidePanelToggle) {
    homeSidePanelToggle.onclick = () => {
      state.homeSidePanelCollapsed = !state.homeSidePanelCollapsed;
      applyHomeSidePanelState();
      persistHomeSidePanelCollapsed(state.homeSidePanelCollapsed);
      if (state.dataSource === 'home') scheduleUsaViewportRefresh(true);
    };
  }

  // Radar composite
  const hrc = document.getElementById('homeRadarComposite');
  if (hrc) {
    hrc.onchange = () => {
      state.radarCompositeEnabled = hrc.checked;
      if (state.radarCompositeEnabled) {
        initRainViewerLayer();
      } else {
        clearHomeSiteLayers();
        state.homeSiteLayersActive = false;
        if (state.radarCompositeLayer) {
          leafletMap.removeLayer(state.radarCompositeLayer);
          state.radarCompositeLayer = null;
        }
        state.homeRadarFrames = [];
        state.homeRadarFrameIdx = 0;
        syncScrubberVisibility();
      }
    };
  }

  // Spotters
  const hst = document.getElementById('homeSpotterToggle');
  if (hst) {
    hst.onchange = () => {
      state.showSpotters = hst.checked;
      refreshSpotterOverlay();
    };
  }

  const hmt = document.getElementById('homeMcdToggle');
  if (hmt) {
    hmt.onchange = () => {
      state.homeMcdEnabled = hmt.checked;
      refreshMcdOverlay();
    };
  }

  // Alert type checkboxes — each has data-alert-type attribute
  document.querySelectorAll('[data-alert-type]').forEach(cb => {
    cb.onchange = () => {
      const eventType = cb.dataset.alertType;
      if (cb.checked) {
        state.homeAlertTypes.add(eventType);
      } else {
        state.homeAlertTypes.delete(eventType);
      }
      refreshAlertOverlay();
    };
  });

  document.querySelectorAll('[data-spc-day]').forEach(input => {
    input.onchange = () => {
      if (!input.checked) return;
      state.homeSpcDay = input.dataset.spcDay || 'none';
      if (!isHomeSpcViewAvailable(state.homeSpcDay, state.homeSpcView)) {
        state.homeSpcView = 'overview';
      }
      syncHomeSpcControls();
      refreshHomeSpcLayers();
    };
  });

  document.querySelectorAll('[data-spc-view]').forEach(input => {
    input.onchange = () => {
      if (!input.checked || input.disabled) return;
      state.homeSpcView = input.dataset.spcView || 'overview';
      syncHomeSpcControls();
      refreshHomeSpcLayers();
    };
  });
}

// --- Side Panel Tab Wiring ---
function wireHomeSidePanelTabs() {
  document.querySelectorAll('.side-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.side-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const panel = tab.dataset.panel;
      document.querySelectorAll('.side-panel-pane').forEach(p => p.classList.toggle('active', p.id === `sidePanel${panel.charAt(0).toUpperCase() + panel.slice(1)}`));
    });
  });

  // Chaser search
  const search = document.getElementById('chaserSearch');
  if (search) {
    search.oninput = () => {
      const q = search.value.toLowerCase().trim();
      const filtered = q ? state.chaserItems.filter(c => c.name.toLowerCase().includes(q)) : state.chaserItems;
      renderChaserList(filtered);
    };
  }
}

function nearestRadarSite(lat, lon) {
  if (!_radarSiteLocations) return null;
  let bestSite = null;
  let bestDist = Infinity;
  for (const [siteId, info] of Object.entries(_radarSiteLocations)) {
    const siteLat = Number(info.lat);
    const siteLon = Number(info.lon);
    if (!Number.isFinite(siteLat) || !Number.isFinite(siteLon)) continue;
    const dLat = siteLat - lat;
    const dLon = siteLon - lon;
    const dist2 = dLat * dLat + dLon * dLon;
    if (dist2 < bestDist) {
      bestDist = dist2;
      bestSite = siteId;
    }
  }
  return bestSite;
}

async function jumpToWarningRadar(lat, lon) {
  // Ensure site locations are loaded before finding nearest
  if (!_radarSiteLocations) {
    try {
      const resp = await fetch(`${BASE}/api/radar/sites`);
      if (resp.ok) {
        const data = await resp.json();
        _radarSiteLocations = data.sites || {};
      }
    } catch {}
  }
  const nearestSite = nearestRadarSite(lat, lon);
  if (!nearestSite) return;

  // Set state BEFORE switchDataSource so it flies to the right site immediately
  pause();
  state.radarSite = nearestSite;
  persistRadarSite(nearestSite);
  state.radarTilt = 0.5;

  const radarSiteSelect = document.getElementById('radarSiteSelect');
  if (radarSiteSelect) radarSiteSelect.value = nearestSite;
  const radarTiltSelect = document.getElementById('radarTiltSelect');
  if (radarTiltSelect) radarTiltSelect.value = '0.5';

  // Enable warnings on the radar page
  state.showWatchesWarnings = true;
  if (watchWarningToggle) watchWarningToggle.checked = true;

  switchDataSource('radar'); // loads data and kicks off initial flyTo

  // scheduleLeafletResize inside switchDataSource calls invalidateSize at ~180ms,
  // which cancels the in-flight flyTo. Re-fire after it settles.
  setTimeout(() => flyToRadarSite(nearestSite), 300);
}

function bindOverlayPopup(layer, title, lines) {
  const html = [title, ...lines.filter(Boolean).map(escapeHtml)].join('<br>');
  layer.bindPopup(html, { className: 'alert-popup' });
}

function bindSpcPopup(layer, title, lines, discussionText, sourceUrl) {
  const headerHtml = [title, ...lines.filter(Boolean).map(escapeHtml)].join('<br>');
  const discussionHtml = discussionText
    ? `<div class="spc-popup-discussion">${escapeHtml(discussionText).replace(/\n/g, '<br>')}</div>`
    : '';
  const sourceHtml = sourceUrl
    ? `<div class="spc-popup-source"><a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">SPC source</a></div>`
    : '';
  layer.bindPopup(`${headerHtml}${discussionHtml}${sourceHtml}`, {
    className: 'alert-popup',
    maxWidth: 620,
  });
  layer.on('popupopen', () => {
    if (typeof layer.closeTooltip === 'function') layer.closeTooltip();
    if (typeof layer.unbindTooltip === 'function' && layer.__spcHoverBinding) {
      layer.unbindTooltip();
    }
    const popupEl = layer.getPopup?.()?.getElement?.();
    if (popupEl) popupEl.style.zIndex = '2500';
  });
  layer.on('popupclose', () => {
    if (!layer.__spcHoverBinding) return;
    layer.bindTooltip(layer.__spcHoverBinding.html, layer.__spcHoverBinding.options);
  });
}

function bindOverlayHover(layer, title, lines) {
  const html = [title, ...lines.filter(Boolean).map(escapeHtml)].join('<br>');
  const options = {
    className: 'spc-hover-tooltip',
    sticky: true,
    direction: 'top',
    opacity: 0.97,
  };
  layer.__spcHoverBinding = { html, options };
  layer.bindTooltip(html, options);
}

function ensureSpcPatternDefs() {
  if (document.getElementById('spc-hatch-dark')) return;
  // Create an invisible SVG in the document body to hold reusable pattern defs.
  // This ensures the pattern is available to all SVG elements on the page.
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', '0');
  svg.setAttribute('height', '0');
  svg.style.position = 'absolute';
  svg.style.pointerEvents = 'none';
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const pattern = document.createElementNS('http://www.w3.org/2000/svg', 'pattern');
  pattern.setAttribute('id', 'spc-hatch-dark');
  pattern.setAttribute('patternUnits', 'userSpaceOnUse');
  pattern.setAttribute('width', '10');
  pattern.setAttribute('height', '10');
  const hatch = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  hatch.setAttribute('d', 'M-2,2 l4,-4 M0,10 l10,-10 M8,12 l4,-4');
  hatch.setAttribute('stroke', '#000000');
  hatch.setAttribute('stroke-width', '2.5');
  hatch.setAttribute('stroke-linecap', 'square');
  pattern.appendChild(hatch);
  defs.appendChild(pattern);
  svg.appendChild(defs);
  document.body.appendChild(svg);
}

function pointInRing(point, ring) {
  const [x, y] = point;
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0];
    const yi = ring[i][1];
    const xj = ring[j][0];
    const yj = ring[j][1];
    const intersects = ((yi > y) !== (yj > y))
      && (x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-9) + xi);
    if (intersects) inside = !inside;
  }
  return inside;
}

function pointInPolygon(point, rings) {
  if (!Array.isArray(rings) || !rings.length) return false;
  if (!pointInRing(point, rings[0])) return false;
  for (let i = 1; i < rings.length; i += 1) {
    if (pointInRing(point, rings[i])) return false;
  }
  return true;
}

function buildHatchSegmentsForPolygon(rings) {
  const outer = Array.isArray(rings?.[0]) ? rings[0] : null;
  if (!outer || outer.length < 3) return [];
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const [x, y] of outer) {
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  if (!Number.isFinite(minX) || !Number.isFinite(maxX) || !Number.isFinite(minY) || !Number.isFinite(maxY)) {
    return [];
  }
  const spacing = Math.max((maxX - minX), (maxY - minY)) > 20 ? 1.2 : 0.65;
  const sampleStep = spacing / 4;
  const minIntercept = minY - maxX;
  const maxIntercept = maxY - minX;
  const segments = [];
  for (let intercept = minIntercept - spacing; intercept <= maxIntercept + spacing; intercept += spacing) {
    const xStart = Math.max(minX, minY - intercept);
    const xEnd = Math.min(maxX, maxY - intercept);
    if (xEnd <= xStart) continue;
    let active = [];
    for (let x = xStart; x <= xEnd + 1e-9; x += sampleStep) {
      const y = x + intercept;
      const point = [x, y];
      const inside = pointInPolygon(point, rings);
      if (inside) {
        active.push(point);
      } else if (active.length >= 2) {
        segments.push(active);
        active = [];
      } else {
        active = [];
      }
    }
    if (active.length >= 2) segments.push(active);
  }
  return segments;
}

function buildSpcDisplayFeatureSets(data) {
  const fills = [];
  const hatchLines = [];
  for (const feature of (data.features || [])) {
    const props = feature.properties || {};
    const style = props.style || {};
    if (!style.hatch) {
      fills.push(feature);
      continue;
    }
    // Include the significant polygon in fills so its outline renders on the map
    fills.push(feature);
    const geometry = feature.geometry || {};
    const polygons = geometry.type === 'Polygon'
      ? [geometry.coordinates]
      : geometry.type === 'MultiPolygon'
        ? geometry.coordinates
        : [];
    for (const polygon of polygons) {
      const segments = buildHatchSegmentsForPolygon(polygon);
      for (const coords of segments) {
        hatchLines.push({
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: coords },
          properties: {
            ...props,
            _spcHatchLine: true,
          },
        });
      }
    }
  }
  return { fills, hatchLines };
}

function applySpcFeaturePresentation(layer, feature) {
  const props = feature?.properties || {};
  const style = props.style || {};
  if (!style.hatch) return;
  const apply = () => {
    ensureSpcPatternDefs();
    const el = layer._path || (layer.getElement ? layer.getElement() : null);
    if (!el) return;
    el.classList.add('spc-significant-path');
    layer.options.interactive = false;
    if (typeof layer.bringToFront === 'function') layer.bringToFront();
  };
  layer.on('add', apply);
  if (layer._path) apply();
}

function formatSpcTimestamp(value) {
  if (!value) return '';
  const text = String(value);
  if (!/^\d{12}$/.test(text)) return text;
  const iso = `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}T${text.slice(8, 10)}:${text.slice(10, 12)}:00Z`;
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return text;
  return dt.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  });
}

function getSpcHoverLines(data, props) {
  const lines = [];
  if (Array.isArray(props.detail_lines)) {
    lines.push(...props.detail_lines);
  } else if (props.detail) {
    lines.push(props.detail);
  } else if (props.label) {
    lines.push(props.label);
  }
  if (props.valid) lines.push(`Valid: ${formatSpcTimestamp(props.valid)}`);
  if (props.expire) lines.push(`Expires: ${formatSpcTimestamp(props.expire)}`);
  if (props.issue) lines.push(`Issued: ${formatSpcTimestamp(props.issue)}`);
  return lines;
}

function buildSpcLegendSections(datasets) {
  const sections = [];
  for (const data of datasets) {
    const seen = new Set();
    const items = [];
    for (const feature of (data.features || [])) {
      const props = feature.properties || {};
      const style = props.style || {};
      const label = props.label || (props.significant ? 'Significant' : '');
      if (!label) continue;
      const key = `${style.fillColor || ''}|${label}|${style.hatch ? 'hatch' : 'solid'}`;
      if (seen.has(key)) continue;
      seen.add(key);
      items.push({
        color: style.fillColor || '#888',
        label,
        hatched: !!style.hatch,
      });
    }
    if (items.length) {
      sections.push({
        title: data.label || data.product || 'SPC Outlook',
        items,
      });
    }
  }
  return sections;
}

function renderSpcLegend(legendSections) {
  const legendEl = document.getElementById('spcLegend');
  if (!legendEl) return;
  if (!legendSections.length) {
    legendEl.classList.add('hidden');
    legendEl.innerHTML = '';
    return;
  }
  legendEl.innerHTML = legendSections.map((sec) => `
    <div class="spc-legend-title">${escapeHtml(sec.title)}</div>
    ${sec.items.map((it) => `
      <div class="spc-legend-item">
        <span class="spc-legend-color${it.hatched ? ' hatched' : ''}" style="background-color:${escapeHtml(it.color)}"></span>
        <span>${escapeHtml(it.label)}</span>
      </div>
    `).join('')}
  `).join('<div style="margin-top:5px"></div>') + '<div class="spc-legend-note">Hatched areas denote significant severe potential.</div>';
  legendEl.classList.remove('hidden');
}

function getSelectedHomeSpcProducts() {
  const dayProducts = HOME_SPC_VIEW_PRODUCTS[state.homeSpcDay];
  if (!dayProducts) return [];
  return dayProducts[state.homeSpcView] || dayProducts.overview || [];
}

function isHomeSpcViewAvailable(day, view) {
  if (!day || day === 'none') return view === 'overview';
  return Boolean(HOME_SPC_VIEW_PRODUCTS[day]?.[view]);
}

function syncHomeSpcControls() {
  document.querySelectorAll('[data-spc-day]').forEach((input) => {
    input.checked = input.dataset.spcDay === state.homeSpcDay;
  });
  document.querySelectorAll('[data-spc-view]').forEach((input) => {
    const view = input.dataset.spcView || 'overview';
    const option = input.closest('[data-spc-view-option]');
    const available = state.homeSpcDay !== 'none' && isHomeSpcViewAvailable(state.homeSpcDay, view);
    input.disabled = !available;
    input.checked = available && view === state.homeSpcView;
    if (option) option.classList.toggle('is-disabled', !available);
  });
}

async function refreshAlertOverlay() {
  if (document.hidden) return;
  if (!['radar', 'home'].includes(state.dataSource)) {
    clearOverlayLayer('alertsLayer');
    return;
  }

  // Radar: filter to warnings only when toggle is on
  if (state.dataSource === 'radar' && !state.showWatchesWarnings) {
    clearOverlayLayer('alertsLayer');
    return;
  }

  const RADAR_WARNING_TYPES = 'Tornado Warning,Tornado Emergency,Severe Thunderstorm Warning,Flash Flood Warning';
  let url = `${BASE}/api/alerts`;
  if (state.dataSource === 'radar') {
    url += `?types=${encodeURIComponent(RADAR_WARNING_TYPES)}`;
  } else if (state.dataSource === 'home' && state.homeAlertTypes.size > 0) {
    url += `?types=${encodeURIComponent([...state.homeAlertTypes].join(','))}`;
  } else if (state.dataSource === 'home' && state.homeAlertTypes.size === 0) {
    clearOverlayLayer('alertsLayer');
    return;
  }

  try {
    const data = await apiFetch(url);
    clearOverlayLayer('alertsLayer');
    state.alertsLayer = L.geoJSON(data, {
      pane: 'alertsPane',
      renderer: alertsRenderer || undefined,
      style: feature => {
        const style = feature.properties.style || {};
        return {
          color: style.color || '#888',
          weight: style.weight || 1,
          fillColor: style.color || '#888',
          fillOpacity: style.fill || 0.08,
        };
      },
      onEachFeature: (feature, layer) => {
        const props = feature.properties || {};
        bindOverlayPopup(layer, `<strong>${props.display_event || props.event || 'Alert'}</strong>`, [
          props.headline,
          props.area_desc,
          props.expires ? `Expires: ${props.expires}` : '',
        ]);
      },
    }).addTo(leafletMap);
  } catch (e) {
    console.error('Failed to refresh watches/warnings overlay:', e);
  }
}

async function refreshSpcOverlay() {
  if (document.hidden) return;
  if (state.dataSource !== 'home') {
    clearOverlayLayer('spcLayer');
    clearHomeSpcLayers();
    return;
  }

  refreshHomeSpcLayers();
}

function clearHomeSpcLayers(invalidate = true) {
  if (invalidate) state.homeSpcRequestSeq += 1;
  for (const [key, layer] of Object.entries(state.homeSpcLayers)) {
    if (layer) leafletMap.removeLayer(layer);
  }
  state.homeSpcLayers = {};
  const legendEl = document.getElementById('spcLegend');
  if (legendEl) { legendEl.classList.add('hidden'); legendEl.innerHTML = ''; }
}

function clearHomeMcdLayer() {
  if (state.homeMcdLayer) {
    leafletMap.removeLayer(state.homeMcdLayer);
    state.homeMcdLayer = null;
  }
}

async function refreshHomeSpcLayers() {
  if (document.hidden) return;
  const requestSeq = ++state.homeSpcRequestSeq;
  const selectedProducts = getSelectedHomeSpcProducts();

  clearHomeSpcLayers(false);
  if (!selectedProducts.length) return;

  const datasets = [];

  for (const productId of selectedProducts) {
    try {
      const data = await apiFetch(`${BASE}/api/spc/${productId}`);
      if (requestSeq !== state.homeSpcRequestSeq || state.dataSource !== 'home') return;
      const featureSets = buildSpcDisplayFeatureSets(data);
      const layer = L.geoJSON({ ...data, features: featureSets.fills }, {
        pane: 'spcPane',
        renderer: spcRenderer || undefined,
        style: feature => {
          const style = feature.properties.style || {};
          return {
            color: style.color || '#bbb',
            weight: style.weight || 2,
            fillColor: style.fillColor || '#888',
            fillOpacity: style.fillOpacity ?? 0.18,
            dashArray: style.dashArray || null,
          };
        },
        onEachFeature: (feature, lyr) => {
          const props = feature.properties || {};
          const lines = getSpcHoverLines(data, props);
          bindSpcPopup(
            lyr,
            `<strong>${data.label || 'SPC Outlook'}</strong>`,
            lines,
            props.discussion_text || data.discussion_text || '',
            props.discussion_source_url || data.discussion_source_url || ''
          );
          bindOverlayHover(lyr, `<strong>${data.label || 'SPC Outlook'}</strong>`, lines);
          applySpcFeaturePresentation(lyr, feature);
        },
      });
      if (requestSeq !== state.homeSpcRequestSeq || state.dataSource !== 'home') return;
      layer.addTo(leafletMap);
      if (layer.bringToFront) layer.bringToFront();
      state.homeSpcLayers[productId] = layer;
      datasets.push(data);
    } catch (e) {
      console.warn(`Failed to load SPC ${productId}:`, e);
    }
  }
  renderSpcLegend(buildSpcLegendSections(datasets));
}

async function refreshMcdOverlay() {
  if (document.hidden) return;
  // Home dashboard: controlled by homeMcdEnabled
  if (state.dataSource === 'home') {
    if (!state.homeMcdEnabled) { clearHomeMcdLayer(); return; }
    try {
      const data = await apiFetch(`${BASE}/api/spc/mcds?limit=25`);
      clearHomeMcdLayer();
      state.homeMcdLayer = L.geoJSON(data, {
        pane: 'spcPane',
        renderer: spcRenderer || undefined,
        style: feature => {
          const style = feature.properties?.style || {};
          return {
            color: style.color || '#ffb347',
            weight: style.weight || 2,
            fillColor: style.fillColor || '#ff9f1c',
            fillOpacity: style.fillOpacity ?? 0.08,
            dashArray: style.dashArray || '8 5',
          };
        },
        onEachFeature: (feature, layer) => {
          const props = feature.properties || {};
          bindSpcPopup(
            layer,
            `<strong>MCD ${props.number || ''}</strong>`,
            [
              props.issued ? `Issued: ${props.issued}` : '',
              props.expires ? `Expires: ${props.expires}` : '',
              Array.isArray(props.probabilities) ? props.probabilities.join(' · ') : '',
            ],
            props.discussion_text || '',
            props.discussion_source_url || ''
          );
        },
      }).addTo(leafletMap);
      if (state.homeMcdLayer?.bringToFront) state.homeMcdLayer.bringToFront();
    } catch (e) {
      console.warn('Failed to load MCD overlay:', e);
    }
    return;
  }

  // Radar/satellite: controlled by showMcd toggle
  clearOverlayLayer('mcdLayer');
  if (!state.showMcd) return;
  try {
    const data = await apiFetch(`${BASE}/api/spc/mcds?limit=25`);
    state.mcdLayer = L.geoJSON(data, {
      pane: 'spcPane',
      renderer: spcRenderer || undefined,
      style: feature => {
        const style = feature.properties?.style || {};
        return {
          color: style.color || '#ffb347',
          weight: style.weight || 2,
          fillColor: style.fillColor || '#ff9f1c',
          fillOpacity: style.fillOpacity ?? 0.08,
          dashArray: style.dashArray || '8 5',
        };
      },
      onEachFeature: (feature, layer) => {
        const props = feature.properties || {};
        bindSpcPopup(
          layer,
          `<strong>MCD ${props.number || ''}</strong>`,
          [
            props.issued ? `Issued: ${props.issued}` : '',
            props.expires ? `Expires: ${props.expires}` : '',
            Array.isArray(props.probabilities) ? props.probabilities.join(' · ') : '',
          ],
          props.discussion_text || '',
          props.discussion_source_url || ''
        );
      },
    }).addTo(leafletMap);
  } catch (e) {
    console.warn('Failed to load MCD overlay:', e);
  }
}

function applyHomeLayerPanelState() {
  if (!homeLayerPanel || !homeLayerPanelToggle) return;
  homeLayerPanel.classList.toggle('collapsed', state.homeLayerPanelCollapsed);
  homeLayerPanelToggle.setAttribute('aria-expanded', String(!state.homeLayerPanelCollapsed));
  syncHomeSpcControls();
}

function applyHomeSidePanelState() {
  if (!homeSidePanel || !homeSidePanelToggle) return;
  homeSidePanel.classList.toggle('collapsed', state.homeSidePanelCollapsed);
  homeSidePanelToggle.setAttribute('aria-expanded', String(!state.homeSidePanelCollapsed));
}

async function refreshSpotterOverlay() {
  if (document.hidden) return;
  if (!['radar', 'home'].includes(state.dataSource)) {
    clearOverlayLayer('spotterLayer');
    return;
  }
  if (!state.showSpotters) {
    clearOverlayLayer('spotterLayer');
    return;
  }

  try {
    const [mode, ageStr] = (state.spotterFilter || 'moving_90').split('_');
    const maxAgeMin = parseInt(ageStr || '90', 10);
    const movingOnly = mode === 'moving';
    const data = await apiFetch(`${BASE}/api/spotters?max_age_min=${maxAgeMin}&moving_only=${movingOnly ? 'true' : 'false'}`);
    clearOverlayLayer('spotterLayer');
    state.spotterLayer = L.geoJSON(data, {
      pane: 'spotterPane',
      interactive: true,
      pointToLayer: (feature, latlng) => L.circleMarker(latlng, {
        radius: 3,
        color: '#ff4444',
        weight: 1,
        fillColor: '#cc0000',
        fillOpacity: 0.85,
        interactive: true,
      }),
      onEachFeature: (feature, layer) => {
        const props = feature.properties || {};
        layer.bindPopup(`<div style="font-family:var(--font-data);padding:4px 2px;">
          <strong style="color:var(--text);">${escapeHtml(props.name || 'Spotter')}</strong>
          <div style="color:var(--text-mid);font-size:11px;margin-top:4px;">${escapeHtml(props.status || '')}</div>
          ${props.timestamp ? `<div style="color:var(--text-dim);font-size:10px;margin-top:2px;">Updated: ${escapeHtml(props.timestamp)}</div>` : ''}
        </div>`, { 
          className: 'spotter-popup',
          closeButton: false,
          offset: [0, -5]
        });
      },
    }).addTo(leafletMap);
  } catch (e) {
    console.error('Failed to refresh spotter overlay:', e);
  }
}

async function refreshFeaturedChasers() {
  if (document.hidden) return;
  if (!['home', 'radar'].includes(state.dataSource)) {
    if (state.featuredChaserLayer) {
      leafletMap.removeLayer(state.featuredChaserLayer);
      state.featuredChaserLayer = null;
    }
    return;
  }
  try {
    const data = await apiFetch(`${BASE}/api/spotters?max_age_min=1440&moving_only=false`);
    if (state.featuredChaserLayer) {
      leafletMap.removeLayer(state.featuredChaserLayer);
      state.featuredChaserLayer = null;
    }
    const namesLower = FEATURED_CHASERS.map(n => n.toLowerCase());
    const matched = (data.features || []).filter(f => {
      const name = (f.properties?.name || '').toLowerCase();
      return namesLower.some(n => name.includes(n));
    });
    if (!matched.length) return;
    state.featuredChaserLayer = L.geoJSON({ type: 'FeatureCollection', features: matched }, {
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
      onEachFeature: (feature, layer) => {
        const props = feature.properties || {};
        const popupHtml = '<div style="font-family:var(--font-data);padding:4px 2px;">'
          + '<strong style="color:var(--accent);">' + escapeHtml(props.name || 'Chaser') + '</strong>'
          + '<div style="color:var(--text-mid);font-size:11px;margin-top:4px;">' + escapeHtml(props.status || '') + '</div>'
          + (props.timestamp ? '<div style="color:var(--text-dim);font-size:10px;margin-top:2px;">Updated: ' + escapeHtml(props.timestamp) + '</div>' : '')
          + '</div>';
        layer.bindPopup(popupHtml, {
          className: 'spotter-popup',
          closeButton: false,
          offset: [0, -10]
        });
      },
    }).addTo(leafletMap);
  } catch (e) {
    console.error('Failed to refresh featured chasers:', e);
  }
}

async function refreshExternalOverlays() {
  if (document.hidden) return;
  await Promise.all([
    refreshAlertOverlay(),
    refreshSpcOverlay(),
    refreshSpotterOverlay(),
    refreshFeaturedChasers(),
  ]);
}

let radarLegendPaletteVersion = null;

function showRadarFrame(idx) {
  if (state.dataSource !== 'radar') return;
  const frame = state.radarFrames[idx];
  if (!frame) return;

  const paletteVersion = frame.palette_version || 1;
  if (radarLegendPaletteVersion !== paletteVersion) {
    radarLegendPaletteVersion = paletteVersion;
    updateLegend();
  }
  const oldLayer = state.radarOverlayLayer;

  // Stable URL so preloaded browser cache is reused
  const url = `${BASE}/rendered/${frame.png_path}?_=${frame.timestamp || ""}&palette=${frame.palette_version || 1}`;
  const bounds = [[frame.bounds.south, frame.bounds.west], [frame.bounds.north, frame.bounds.east]];

  const newLayer = L.imageOverlay(url, bounds, {
    pane: 'radarCompositePane',
    opacity: 1.0,
    interactive: false,
    crossOrigin: true,
    className: 'radar-overlay'
  });

  // Preload: add new layer, then remove old once loaded
  newLayer.addTo(leafletMap);
  state.radarOverlayLayer = newLayer;

  const img = newLayer.getElement();
  if (img && oldLayer) {
    img.addEventListener('load', () => {
      leafletMap.removeLayer(oldLayer);
    }, { once: true });
    // Fallback: remove old layer after 2s even if load event doesn't fire
    setTimeout(() => {
      if (leafletMap.hasLayer(oldLayer)) leafletMap.removeLayer(oldLayer);
    }, 2000);
  } else if (oldLayer) {
    leafletMap.removeLayer(oldLayer);
  }
  
  // Update time display — timestamp format is YYYYMMDD_HHMMSS
  if (frame.timestamp) {
    const timeEl = document.getElementById('validTimeDisplay');
    if (timeEl) {
      timeEl.textContent = `Scan: ${parseRadarTimestamp(frame.timestamp).full}`;
    }
  }
  
  // Update frame counter
  const frameCounter = document.getElementById('frameCounter');
  if (frameCounter) {
    frameCounter.textContent = `${idx + 1}/${state.radarFrames.length}`;
  }
  markCaptureReady('radar');
  syncShareableDesktopUrl();

  preloadAllDesktopFrames();
}

function radarStepForward() {
  if (state.radarFrames.length <= 1) return;
  const nextIdx = Math.min(state.radarFrameIdx + 1, state.radarFrames.length - 1);
  state.radarFrameIdx = nextIdx;
  scrubber.value = nextIdx;
  showRadarFrame(nextIdx);
}

function radarStepBackward() {
  if (state.radarFrames.length <= 1) return;
  const prevIdx = Math.max(state.radarFrameIdx - 1, 0);
  state.radarFrameIdx = prevIdx;
  scrubber.value = prevIdx;
  showRadarFrame(prevIdx);
}

// Fly map to a radar site
let _radarSiteLocations = null;
async function centerRadarSiteInView(site, { animate = true, zoom = 8 } = {}) {
  if (!_radarSiteLocations) {
    try {
      const resp = await fetch(`${BASE}/api/radar/sites`);
      if (resp.ok) {
        const data = await resp.json();
        _radarSiteLocations = data.sites || {};
      }
    } catch (e) {
      console.log('Failed to fetch radar site locations:', e);
    }
  }
  const info = _radarSiteLocations && _radarSiteLocations[site];
  if (info && info.lat && info.lon) {
    const targetLatLng = L.latLng(info.lat, info.lon);
    const mapSize = leafletMap.getSize();
    const isMobile = window.matchMedia('(max-width: 980px)').matches;
    const radarSidebarVisible = radarSidebar && !radarSidebar.classList.contains('hidden') && !isMobile;
    const horizontalOffset = radarSidebarVisible ? Math.round(Math.min(240, mapSize.x * 0.18)) : 0;
    const targetZoom = Number.isFinite(zoom) ? zoom : 8;
    const shiftedPoint = leafletMap.project(targetLatLng, targetZoom).subtract([horizontalOffset, 0]);
    const shiftedCenter = leafletMap.unproject(shiftedPoint, targetZoom);
    if (animate) {
      leafletMap.flyTo(shiftedCenter, targetZoom, { duration: 1.2 });
    } else {
      leafletMap.setView(shiftedCenter, targetZoom, { animate: false });
    }
  }
}

async function flyToRadarSite(site) {
  return centerRadarSiteInView(site, { animate: true, zoom: 8 });
}

// Populate the radar site <select> from the API (reuses the site-location cache).
async function populateRadarSiteSelect() {
  const sel = document.getElementById('radarSiteSelect');
  if (!sel) return;
  if (!_radarSiteLocations) {
    try {
      const resp = await fetch(`${BASE}/api/radar/sites`);
      if (resp.ok) {
        const data = await resp.json();
        _radarSiteLocations = data.sites || {};
      }
    } catch (e) {
      console.log('Failed to fetch radar site locations:', e);
    }
  }
  const sites = _radarSiteLocations;
  if (!sites || Object.keys(sites).length === 0) {
    sel.innerHTML = '<option value="" disabled>Radar sites unavailable</option>';
    return;
  }
  const entries = Object.entries(sites)
    .sort((a, b) => String(a[1].name || a[0]).localeCompare(String(b[1].name || b[0])));
  sel.innerHTML = entries.map(([siteId, info]) =>
    `<option value="${siteId}">${info.name || siteId} (${siteId})</option>`,
  ).join('');
  sel.value = state.radarSite;
}

// ---------------------------------------------------------------------------
// Radar site marker dots (clickable NEXRAD station locations)
// ---------------------------------------------------------------------------
let _radarSiteMarkerLayer = null;
let _radarSiteBuildPromise = null;

async function buildRadarSiteMarkers() {
  if (_radarSiteMarkerLayer) return; // already built
  if (!_radarSiteLocations) {
    try {
      const resp = await fetch(`${BASE}/api/radar/sites`);
      if (resp.ok) {
        const data = await resp.json();
        _radarSiteLocations = data.sites || {};
      }
    } catch (e) {
      console.log('Failed to fetch radar site locations:', e);
      return;
    }
  }
  if (!_radarSiteLocations) return;

  _radarSiteMarkerLayer = L.layerGroup();
  for (const [siteId, info] of Object.entries(_radarSiteLocations)) {
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
    marker.on('click', async () => {
      switchDataSource('radar');
      pause();
      clearRadarLayers();
      state.radarSite = siteId;
      persistRadarSite(siteId);
      state.radarTilt = 0.5;
      const sel = document.getElementById('radarSiteSelect');
      if (sel) sel.value = siteId;
      await flyToRadarSite(siteId);
      await loadRadarTilts();
      await loadRadarData();
      updateMapInfoForRadar();
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

// Load available tilts for current site and populate the dropdown
async function loadRadarTilts() {
  const site = state.radarSite;
  const tiltSelect = document.getElementById('radarTiltSelect');
  if (!tiltSelect) return;

  try {
    const resp = await fetch(`${BASE}/api/radar/tilts/${state.radarSite}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (site !== state.radarSite || state.dataSource !== 'radar') return;
    const tilts = data.tilts || [0.5];

    const currentVal = String(state.radarTilt);
    tiltSelect.innerHTML = '';
    tilts.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t;
      opt.textContent = `${t}°${t === tilts[0] ? ' (lowest)' : ''}`;
      tiltSelect.appendChild(opt);
    });
    // Restore selection if still valid, else default to lowest
    if (tilts.map(String).includes(currentVal)) {
      tiltSelect.value = currentVal;
    } else {
      tiltSelect.value = tilts[0];
      state.radarTilt = tilts[0];
    }
  } catch (e) {
    console.log('Failed to load radar tilts:', e);
    showToast('Failed to load radar tilts', 'error');
  }
}

// Render an upper tilt on demand
async function loadRadarTilt() {
  return loadRadarData();
}

// ---------------------------------------------------------------------------
// Model Data Functions (existing)
// ---------------------------------------------------------------------------
function overlayUrl(runId, param, fxx) {
  const fxxStr = typeof fxx === 'number' ? String(fxx).padStart(2, '0') : fxx;
  const vector = window.PressureContours && state.vectorAvailableByParam?.[param]?.some((frame) => String(frame) === String(fxx));
  return `${BASE}/rendered/${state.model}/${runId}/${param}/f${fxxStr}.${vector ? 'contours.json' : 'png'}?v=${MODEL_IMAGE_VERSION}`;
}

function modelOverlayParamClass(param) {
  return 'model-overlay-' + String(param || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '-');
}

function modelOverlayClass(param) {
  return `model-overlay ${modelOverlayParamClass(param)}`;
}

function applyModelOverlayClass(param) {
  if (state.overlayLayer?.isPressureContours) return;
  const img = state.overlayLayer?.getElement?.();
  if (!img) return;
  for (const cls of Array.from(img.classList)) {
    if (cls !== 'model-overlay' && cls.startsWith('model-overlay-')) {
      img.classList.remove(cls);
    }
  }
  img.classList.add('model-overlay', modelOverlayParamClass(param));
}

function showOverlay() {
  if (state.dataSource !== 'models') return;
  if (!state.overlayBounds || !state.runId) return;

  const fxx = state.currentFxx;
  const param = state.parameter;
  const existing = document.getElementById('noDataMsg');
  if (existing) existing.remove();

  const url = overlayUrl(state.runId, param, fxx);
  const vector = !!window.PressureContours?.isUrl(url);
  if (state.overlayLayer && !!state.overlayLayer.isPressureContours !== vector) {
    leafletMap.removeLayer(state.overlayLayer);
    state.overlayLayer = null;
  }

  if (state.overlayLayer) {
    state.overlayLayer.setUrl(url);
    state.overlayLayer.setBounds(state.overlayBounds);
  } else if (vector) {
    state.overlayLayer = window.PressureContours.layer(url).addTo(leafletMap);
  } else {
    state.overlayLayer = L.imageOverlay(url, state.overlayBounds, {
      opacity: 1.0,
      interactive: false,
      className: modelOverlayClass(param),
    }).addTo(leafletMap);
  }
  applyModelOverlayClass(param);
  updateModelFrame();
  markCaptureReady('model');
  syncShareableDesktopUrl();
  preloadAllDesktopFrames();
}

async function loadAvailability() {
  const requestId = _modelSelectionId;
  const model = state.model;
  const runId = state.runId;
  if (!state.runId) return;
  try {
    const [data, vectors] = await Promise.all([
      apiFetch(`${BASE}/api/available/${model}/${runId}?_=${Date.now()}`),
      apiFetch(`${BASE}/api/available/${model}/${runId}/vectors?_=${Date.now()}`).catch(() => ({})),
    ]);
    if (requestId !== _modelSelectionId || model !== state.model || runId !== state.runId || state.dataSource !== 'models') return;
    state.availableByParam = data || {};
    state.vectorAvailableByParam = vectors;
    state.availableFxx = state.availableByParam[state.parameter] || [];
  } catch (e) {
    console.error('Failed to load availability:', e);
  }
}

function clearModelDisplay() {
  pause();
  clearLoadQueue();
  if (state.overlayLayer) leafletMap.removeLayer(state.overlayLayer);
  state.overlayLayer = null;
  state.overlayBounds = null;
  state.runMeta = null;
  state.availableByParam = {};
  state.vectorAvailableByParam = {};
  state.availableFxx = [];
  updateScrubber();
  if (fxxProgress) fxxProgress.innerHTML = '';
  validTimeDisplay.textContent = 'Loading model data…';
  document.getElementById('frameCounter').textContent = '—';
}

async function loadRuns(preferredValidTime = null) {
	if (state.dataSource !== 'models') return;
  const requestId = ++_modelRunsId;
  ++_modelSelectionId;
  const model = state.model;
  const isCurrent = () => requestId === _modelRunsId && state.dataSource === 'models' && model === state.model;
  clearModelDisplay();
  hideMapNotice();
  beginMapLoading('Loading model runs…');
	try {
    const runs = await apiFetch(`${BASE}/api/runs?model=${model}`);
    if (!isCurrent()) return;
    state.runPaletteVersions = {};
    for (const r of runs) state.runPaletteVersions[String(r.run_id)] = r.palette_version || 1;
    populateRunSelect(runs);
    if (!runs.length) {
      state.runId = null;
      validTimeDisplay.textContent = 'No model runs available';
      showMapNotice('No runs available for this model yet.');
    }
    if (runs.length > 0) {
      const captureRunId = CAPTURE_MODE && state.runId ? String(state.runId) : null;
      const sharedRunId = sharedModelSelectionPending && state.runId ? String(state.runId) : null;
      const selectedRun = (captureRunId && runs.find(r => String(r.run_id) === captureRunId))
        || (sharedRunId && runs.find(r => String(r.run_id) === sharedRunId))
        || prioritizeModelRuns(runs, model).find(r => r.is_complete || r.completed_fxx?.length)
        || runs[0];
      await selectRun(selectedRun.run_id, preferredValidTime);
      if (isCurrent()) updateParamVisibilityForModel(state.model);
    }
  } catch (e) {
    console.error('Failed to load runs:', e);
    if (isCurrent()) {
      state.runId = null;
      validTimeDisplay.textContent = 'Unable to load model runs';
      showMapNotice('Unable to load model runs. Select a model to retry.');
    }
  } finally {
    endMapLoading();
  }
}

function populateRunSelect(runs) {
  runSelect.innerHTML = '';
  if (runs.length === 0) {
    runSelect.innerHTML = '<option value="">No runs</option>';
    return;
  }
  for (const run of runs) {
    const opt = document.createElement('option');
    opt.value = run.run_id;
    const status = window.ViewerUI.runStatus(run, modelRunIsOld(run.run_id, state.model));
    opt.textContent = `${formatRunId(run.run_id)} · ${status}`;
    opt.title = `${run.run_id.slice(0,4)}-${run.run_id.slice(4,6)}-${run.run_id.slice(6,8)} ${run.run_id.slice(8,10)}:00 UTC · ${status}`;
    runSelect.appendChild(opt);
  }
}

function formatRunId(runId) {
  if (runId.length === 10) {
    return `${runId.slice(4,6)}/${runId.slice(6,8)} ${runId.slice(8,10)}Z`;
  }
  return runId;
}

function getModelValidTime(runId, fxx) {
  if (!runId || fxx === null || fxx === undefined) return null;
  if (String(runId).length !== 10) return null;
  const parsed = parseFxx(fxx);
  if (!Number.isFinite(parsed.hours) || !Number.isFinite(parsed.minutes)) return null;
  const init = new Date(`${runId.slice(0,4)}-${runId.slice(4,6)}-${runId.slice(6,8)}T${runId.slice(8,10)}:00:00Z`);
  if (Number.isNaN(init.getTime())) return null;
  return new Date(init.getTime() + parsed.hours * 3600_000 + parsed.minutes * 60_000);
}

function findClosestAvailableFxx(runId, availableFxx, targetValidTime) {
  if (!targetValidTime || !(targetValidTime instanceof Date) || Number.isNaN(targetValidTime.getTime())) return null;
  if (!Array.isArray(availableFxx) || availableFxx.length === 0) return null;
  let bestFxx = null;
  let bestDiff = Infinity;
  for (const fxx of availableFxx) {
    const valid = getModelValidTime(runId, fxx);
    if (!valid) continue;
    const diff = Math.abs(valid.getTime() - targetValidTime.getTime());
    if (diff < bestDiff) {
      bestDiff = diff;
      bestFxx = fxx;
    }
  }
  return bestFxx;
}

async function selectRun(runId, preferredValidTime = null) {
  const requestId = ++_modelSelectionId;
  const model = state.model;
  const isCurrent = () => requestId === _modelSelectionId && model === state.model && state.dataSource === 'models';
  clearModelDisplay();
  hideMapNotice();
  const requestedSharedFxx = sharedModelSelectionPending ? SHARED_VIEW_CONFIG?.fxx : null;
	state.runId = runId;
  if (runSelect) runSelect.value = runId;
  clearLoadQueue();
  clearCaptureReady();
  beginMapLoading('Loading model data…');

  try {
    const [meta, availability, bounds, vectors] = await Promise.all([
      apiFetch(`${BASE}/api/runs/${model}/${runId}`),
      apiFetch(`${BASE}/api/available/${model}/${runId}`),
      apiFetch(`${BASE}/api/bounds/${model}/${runId}`),
      apiFetch(`${BASE}/api/available/${model}/${runId}/vectors`).catch(() => ({})),
    ]);
    if (!isCurrent()) return;
    state.runMeta = meta;
    state.availableByParam = availability || {};
    state.vectorAvailableByParam = vectors;
    state.overlayBounds = [[bounds.south, bounds.west], [bounds.north, bounds.east]];
    updateModelFrame();
    setModelReferenceMode(true);
    updateParamVisibilityForModel(state.model);
    state.availableFxx = state.availableByParam[state.parameter] || [];

    createFxxProgress();
    updateFxxProgress();
    updateScrubber();

    if (state.availableFxx.length > 0) {
      if (CAPTURE_MODE) {
        const captureFxx = getCaptureConfig()?.fxx;
        const matchedFxx = captureFxx !== null
          ? state.availableFxx.find((value) => String(value) === String(captureFxx))
          : undefined;
        state.currentFxx = matchedFxx !== undefined ? matchedFxx : state.availableFxx[state.availableFxx.length - 1];
      } else if (requestedSharedFxx !== null && requestedSharedFxx !== undefined) {
        const sharedFxx = state.availableFxx.find((value) => String(value) === String(requestedSharedFxx));
        state.currentFxx = sharedFxx !== undefined ? sharedFxx : state.availableFxx[0];
      } else {
        const matchedFxx = findClosestAvailableFxx(runId, state.availableFxx, preferredValidTime);
        state.currentFxx = matchedFxx !== null ? matchedFxx : state.availableFxx[0];
      }
      const currentIdx = Math.max(0, state.availableFxx.findIndex((value) => String(value) === String(state.currentFxx)));
      scrubber.value = currentIdx;
      preloadAllDesktopFrames();
      showOverlay();
      displayCurrentFrame();
    }
  } catch (e) {
    console.error('Failed to select run:', e);
    if (isCurrent()) {
      validTimeDisplay.textContent = 'Unable to load this run';
      showMapNotice('Unable to load this run. Select another run to retry.');
    }
  } finally {
    if (isCurrent()) {
      sharedModelSelectionPending = false;
      syncShareableDesktopUrl();
    }
	  endMapLoading();
  }
}

// ── Unified frame URL + smart preloading (mirrors mobile.js) ──────────────
// Returns a stable URL for any frame index in the current data source.
function desktopFrameUrl(idx) {
  if (state.dataSource === 'models') {
    const fxx = state.availableFxx[idx];
    if (fxx == null) return null;
    return overlayUrl(state.runId, state.parameter, fxx);
  }
  if (state.dataSource === 'satellite') {
    const frame = state.satFrames[idx];
    return frame?.png_path ? `${BASE}/rendered/${String(frame.png_path).replace(/^\//, '')}` : null;
  }
  if (state.dataSource === 'radar') {
    const frame = state.radarFrames[idx];
    return frame?.png_path ? `${BASE}/rendered/${frame.png_path}?_=${frame.timestamp || ''}&palette=${frame.palette_version || 1}` : null;
  }
  if (state.dataSource === 'home') {
    const frame = state.homeRadarFrames[idx];
    if (!frame) return null;
    const token = encodeURIComponent(frame.generated_at || frame.timestamp || '');
    return `${BASE}/rendered/${frame.png_path}?v=${token}`;
  }
  return null;
}

// Preload all frames for the current product with priority on the next 6.
let resumePlaybackWhenVisible = false;
function handlePageVisibility() {
  if (document.hidden) {
    resumePlaybackWhenVisible = state.isPlaying;
    pause();
  } else {
    preloadAllDesktopFrames();
    _pumpQueue();
    if (resumePlaybackWhenVisible) play();
    resumePlaybackWhenVisible = false;
  }
}
document.addEventListener('visibilitychange', handlePageVisibility);

function frameWindow(count, current) {
  if (!Number.isFinite(count) || count <= 0) return [];
  current = Number.isFinite(current) ? current : 0;
  return [...new Set([0, 1, 2, 3, 4, 5, 6, -1, -2]
    .map(offset => ((current + offset) % count + count) % count))];
}

function trimPreloadCache() {
  const keep = new Set(_productUrls);
  for (const [url, status] of _imgCache) {
    if (_imgCache.size <= 96) break;
    if (status !== 'pending' && !keep.has(url)) _imgCache.delete(url);
  }
}

function preloadAllDesktopFrames() {
  if (document.hidden) return;
  clearLoadQueue();
  let totalCount = 0;
  let currentIdx = 0;
  if (state.dataSource === 'models') {
    totalCount = state.availableFxx.length;
    currentIdx = Math.max(0, state.availableFxx.indexOf(state.currentFxx));
  } else if (state.dataSource === 'satellite') {
    totalCount = state.satFrames.length;
    currentIdx = state.satFrameIdx;
  } else if (state.dataSource === 'radar') {
    totalCount = state.radarFrames.length;
    currentIdx = state.radarFrameIdx;
  } else if (state.dataSource === 'home') {
    totalCount = state.homeRadarFrames.length;
    currentIdx = state.homeRadarFrameIdx;
  } else {
    return;
  }

  _productUrls = frameWindow(totalCount, currentIdx).map(desktopFrameUrl).filter(Boolean);
  trimPreloadCache();
  cacheFrames(_productUrls, true);
  updateLoadStatus();
}

// Returns the URL of the next frame to be shown (for playback wait-check).
function _getNextDesktopFrameUrl() {
  if (state.dataSource === 'models') {
    const fxxList = state.availableFxx;
    if (!fxxList.length) return null;
    const cur = fxxList.indexOf(state.currentFxx);
    return desktopFrameUrl(cur >= fxxList.length - 1 ? 0 : cur + 1);
  }
  if (state.dataSource === 'satellite') {
    if (!state.satFrames.length) return null;
    return desktopFrameUrl(state.satFrameIdx >= state.satFrames.length - 1 ? 0 : state.satFrameIdx + 1);
  }
  if (state.dataSource === 'radar') {
    if (!state.radarFrames.length) return null;
    return desktopFrameUrl(state.radarFrameIdx >= state.radarFrames.length - 1 ? 0 : state.radarFrameIdx + 1);
  }
  if (state.dataSource === 'home') {
    if (!state.homeRadarFrames.length) return null;
    return desktopFrameUrl(state.homeRadarFrameIdx >= state.homeRadarFrames.length - 1 ? 0 : state.homeRadarFrameIdx + 1);
  }
  return null;
}




function displayCurrentFrame() {
  showOverlay();
  updateValidTimeDisplay();
  refreshSoundingForCurrentPoint();

  if (fxxProgress) {
    fxxProgress.querySelectorAll('.fxx-box').forEach(box => {
      box.classList.toggle('current', parseInt(box.dataset.fxx) === state.currentFxx);
    });
  }
}

function parseFxx(fxx) {
  if (typeof fxx === 'number') {
    return { hours: fxx, minutes: 0, display: `F+${String(fxx).padStart(2,'0')}` };
  }
  if (fxx.includes('_')) {
    const [hours, minStr] = fxx.split('_');
    const minutes = parseInt(minStr.replace('min', ''), 10);
    return { 
      hours: parseInt(hours, 10), 
      minutes, 
      display: `F+${hours}:${String(minutes).padStart(2,'0')}` 
    };
  }
  return { hours: parseInt(fxx, 10), minutes: 0, display: `F+${fxx.padStart(2,'0')}` };
}

function updateValidTimeDisplay() {
  if (!state.runMeta) return;
  const fxx = state.currentFxx;
  const initTime = state.runMeta.init_time;
  const parsed = parseFxx(fxx);
  
  if (initTime) {
    const init = new Date(initTime);
    const valid = new Date(init.getTime() + parsed.hours * 3600_000 + parsed.minutes * 60_000);
    const weekday = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][valid.getUTCDay()];
    const month = valid.getUTCMonth() + 1;
    const day = valid.getUTCDate();
    const utcH = String(valid.getUTCHours()).padStart(2, '0');
    const utcM = valid.getUTCMinutes() > 0 ? `:${String(valid.getUTCMinutes()).padStart(2,'0')}` : '';
    validTimeDisplay.textContent = `${parsed.display}  Valid: ${weekday} ${utcH}${utcM}z ${month}/${day}`;
  } else {
    validTimeDisplay.textContent = parsed.display;
  }
  
  updateMapInfoForModel();
}

function updateMapInfoForModel() {
  const modelEl = document.getElementById('mapInfoModel');
  const productEl = document.getElementById('mapInfoProduct');
  const timeEl = document.getElementById('mapInfoTime');
  
  if (!modelEl || !productEl || !timeEl) return;
  
  const modelNames = {
    'hrrr': 'HRRR',
    'nam3km': 'NAM 3km',
    'nam12km': 'NAM 12km',
    'ecmwf': 'ECMWF',
    'rap': 'RAP',
    'gfs': 'GFS',
    'gdps': 'GDPS',
    'rtma': 'RTMA',
    'href': 'HREF Ensemble',
    'rrfs': 'RRFS',
    'gefs': 'GEFS Ensemble'
  };
  modelEl.textContent = modelNames[state.model] || state.model.toUpperCase();
  
  const legendLabel = document.getElementById('legendLabel');
  if (legendLabel && legendLabel.textContent) {
    productEl.textContent = legendLabel.textContent;
  } else {
    productEl.textContent = state.parameter;
  }
  
  if (state.runMeta && state.runMeta.init_time) {
    const fxx = state.currentFxx;
    const parsed = parseFxx(fxx);
    const init = new Date(state.runMeta.init_time);
    const valid = new Date(init.getTime() + parsed.hours * 3600_000 + parsed.minutes * 60_000);
    const utcH = String(valid.getUTCHours()).padStart(2, '0');
    const utcM = parsed.minutes > 0 ? `:${String(valid.getUTCMinutes()).padStart(2,'0')}` : '';
    const day = String(valid.getUTCDate()).padStart(2, '0');
    const month = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][valid.getUTCMonth()];
    timeEl.textContent = `Valid: ${utcH}${utcM}Z ${day} ${month}${modelRunIsOld(state.runId, state.model) ? ' · Older run' : ''}`;
  } else {
    timeEl.textContent = 'Valid: --';
  }
}

function updateScrubber() {
  scrubber.disabled = state.availableFxx.length < 2;
  if (state.dataSource !== 'models') return;
  syncScrubberVisibility();
  const fxxList = state.availableFxx;
  scrubberTicks.classList.remove('satellite-mode');
  if (fxxList.length === 0) {
    scrubber.max = 0;
    scrubberTicks.innerHTML = '';
    return;
  }
  scrubber.min = 0;
  scrubber.max = fxxList.length - 1;
  renderScrubberTicks(fxxList);
}

function getModelScrubberTickIndices(fxxList) {
  if (!Array.isArray(fxxList) || fxxList.length === 0) return [];
  if (fxxList.length <= 5) {
    return fxxList.map((_, index) => index);
  }

  const maxTicks = 5;
  const lastIndex = fxxList.length - 1;
  const indices = new Set([0, lastIndex]);

  for (let slot = 1; slot < maxTicks - 1; slot += 1) {
    const index = Math.round((slot * lastIndex) / (maxTicks - 1));
    indices.add(index);
  }

  return Array.from(indices).sort((a, b) => a - b);
}

function renderScrubberTicks(fxxList) {
  scrubberTicks.innerHTML = '';
  if (fxxList.length <= 1) return;

  const tickIndices = getModelScrubberTickIndices(fxxList);
  for (const i of tickIndices) {
    const tick = document.createElement('div');
    tick.className = 'scrubber-tick';
    tick.style.left = `${(i / (fxxList.length - 1)) * 100}%`;
    if (i === 0 || i === fxxList.length - 1) tick.classList.add('edge');
    scrubberTicks.appendChild(tick);
  }
}

function togglePlay() {
  if (state.isPlaying) {
    pause();
  } else {
    play();
  }
}

const LOOP_END_PAUSE_MULTIPLIER = 1.75;

function getActiveOverlayImage() {
  if (state.dataSource === 'home' && state.radarCompositeLayer) {
    return state.radarCompositeLayer.getElement?.() || null;
  }
  if ((state.dataSource === 'models' || state.dataSource === 'chase') && state.overlayLayer) {
    return state.overlayLayer.getElement?.() || null;
  }
  if (state.dataSource === 'satellite' && state.satOverlayLayer) {
    return state.satOverlayLayer.getElement?.() || null;
  }
  if (state.dataSource === 'radar' && state.radarOverlayLayer) {
    return state.radarOverlayLayer.getElement?.() || null;
  }
  return null;
}

let _loopPlaybackId = 0;
let _playbackImageCleanup = null;
const LOOP_END_HOLD_MS = 1000;

/** Whether the frame on screen is the last frame of the active sequence. */
function isLastPlaybackFrame() {
  if (state.dataSource === 'radar') return state.radarFrameIdx === state.radarFrames.length - 1;
  if (state.dataSource === 'satellite') return state.satFrameIdx === state.satFrames.length - 1;
  if (state.dataSource === 'home') return state.homeRadarFrameIdx === state.homeRadarFrames.length - 1;
  if (state.dataSource === 'chase') return chaseState.currentFxx === chaseState.availableFxx.at(-1);
  return state.currentFxx === state.availableFxx.at(-1);
}

function startLoopPlayback(interval, advanceFrame) {
  const playbackId = ++_loopPlaybackId;
  clearTimeout(state.playTimer);
  if (_playbackImageCleanup) _playbackImageCleanup();
  const isCurrent = () => state.isPlaying && playbackId === _loopPlaybackId;
  const scheduleNext = () => {
    if (!isCurrent()) return;
    const delay = isLastPlaybackFrame()
      ? Math.max(LOOP_END_HOLD_MS, Math.round(interval * LOOP_END_PAUSE_MULTIPLIER))
      : interval;
    const img = getActiveOverlayImage();
    const queue = () => {
      if (!isCurrent()) return;
      if (_playbackImageCleanup) _playbackImageCleanup();
      if (isCurrent()) state.playTimer = setTimeout(tick, delay);
    };
    if (img && !img.complete) {
      _playbackImageCleanup = () => {
        img.removeEventListener('load', queue);
        img.removeEventListener('error', queue);
        _playbackImageCleanup = null;
      };
      img.addEventListener('load', queue);
      img.addEventListener('error', queue);
    } else queue();
  };
  const tick = () => {
    if (!isCurrent()) return;
    const nextUrl = _getNextDesktopFrameUrl();
    if (nextUrl && _imgCache.get(nextUrl) === 'pending') {
      state.playTimer = setTimeout(tick, 50);
      return;
    }
    advanceFrame();
    scheduleNext();
  };

  state.isPlaying = true;
  leafletMap?.getContainer?.()?.classList.add('is-playing');
  playBtn.innerHTML = PAUSE_ICON_SVG;
  playBtn.title = 'Pause (Space)';
  playBtn.setAttribute('aria-label', 'Pause');
  tick();
}

function play() {
  if (state.dataSource === 'home') {
    if (state.homeRadarFrames.length <= 1) return;
    const interval = Math.max(25, Math.round(state.playSpeedMs / state.playSpeed));
    startLoopPlayback(interval, () => {
      if (state.homeRadarFrameIdx >= state.homeRadarFrames.length - 1) {
        state.homeRadarFrameIdx = 0;
        scrubber.value = 0;
        showHomeRadarFrame(0);
        return true;
      }
      state.homeRadarFrameIdx += 1;
      scrubber.value = state.homeRadarFrameIdx;
      showHomeRadarFrame(state.homeRadarFrameIdx);
      return false;
    });
    return;
  }
  if (state.dataSource === 'satellite') {
    if (state.satFrames.length <= 1) return;
    const interval = Math.max(25, Math.round(state.playSpeedMs / state.playSpeed));
    startLoopPlayback(interval, () => {
      if (state.satFrameIdx >= state.satFrames.length - 1) {
        state.satFrameIdx = 0;
        scrubber.value = 0;
        showSatelliteFrame(0);
        return true;
      }
      satStepForward();
      return false;
    });
    return;
  }
  if (state.dataSource === 'radar') {
    if (state.radarFrames.length <= 1) return;
    const interval = Math.max(25, Math.round(state.playSpeedMs / state.playSpeed));
    startLoopPlayback(interval, () => {
      if (state.radarFrameIdx >= state.radarFrames.length - 1) {
        state.radarFrameIdx = 0;
        scrubber.value = 0;
        showRadarFrame(0);
        return true;
      }
      radarStepForward();
      return false;
    });
    return;
  }
  if (state.dataSource === 'chase') {
    if (chaseState.availableFxx.length <= 1) return;
    const interval = Math.round(state.playSpeedMs / state.playSpeed);
    startLoopPlayback(interval, () => {
      const fxxList = chaseState.availableFxx;
      const currentIdx = fxxList.indexOf(chaseState.currentFxx);
      if (currentIdx >= fxxList.length - 1) {
        chaseState.currentFxx = fxxList[0];
        scrubber.value = 0;
        updateChaseOverlay();
        return true;
      }
      chaseState.currentFxx = fxxList[currentIdx + 1];
      scrubber.value = currentIdx + 1;
      updateChaseOverlay();
      return false;
    });
    return;
  }
  if (state.availableFxx.length <= 1) return;
  const interval = Math.round(state.playSpeedMs / state.playSpeed);
  startLoopPlayback(interval, () => {
    if (state.currentFxx === state.availableFxx.at(-1)) {
      state.currentFxx = state.availableFxx[0];
      scrubber.value = state.availableFxx.indexOf(state.currentFxx);
      displayCurrentFrame();
      return true;
    }
    stepForward();
    return false;
  });
}

function setPlaySpeed(speed) {
  state.playSpeed = speed;
  document.querySelectorAll('.speed-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.speed) === speed);
  });
  syncAriaPressed();
  if (state.isPlaying) {
    clearTimeout(state.playTimer);
    play();
  }
}

function pause() {
  ++_loopPlaybackId;
  if (_playbackImageCleanup) _playbackImageCleanup();
  state.isPlaying = false;
  leafletMap?.getContainer?.()?.classList.remove('is-playing');
  playBtn.innerHTML = PLAY_ICON_SVG;
  playBtn.title = 'Play (Space)';
  playBtn.setAttribute('aria-label', 'Play');
  clearTimeout(state.playTimer);
}

function satStepForward() {
  if (state.satFrameIdx < state.satFrames.length - 1) {
    state.satFrameIdx++;
    scrubber.value = state.satFrameIdx;
    showSatelliteFrame(state.satFrameIdx);
  }
}

function satStepBackward() {
  if (state.satFrameIdx > 0) {
    state.satFrameIdx--;
    scrubber.value = state.satFrameIdx;
    showSatelliteFrame(state.satFrameIdx);
  }
}

function stepForward() {
  if (state.dataSource === 'chase') {
    const fxxList = chaseState.availableFxx;
    if (fxxList.length === 0) return;
    const currentIdx = fxxList.indexOf(chaseState.currentFxx);
    if (currentIdx < fxxList.length - 1) {
      chaseState.currentFxx = fxxList[currentIdx + 1];
      scrubber.value = currentIdx + 1;
      updateChaseOverlay();
    }
    return;
  }
  const fxxList = state.availableFxx;
  if (fxxList.length === 0) return;
  const currentIdx = fxxList.indexOf(state.currentFxx);
  if (currentIdx < fxxList.length - 1) {
    state.currentFxx = fxxList[currentIdx + 1];
    scrubber.value = currentIdx + 1;
    displayCurrentFrame();
  }
}

function stepBackward() {
  if (state.dataSource === 'chase') {
    const fxxList = chaseState.availableFxx;
    if (fxxList.length === 0) return;
    const currentIdx = fxxList.indexOf(chaseState.currentFxx);
    if (currentIdx > 0) {
      chaseState.currentFxx = fxxList[currentIdx - 1];
      scrubber.value = currentIdx - 1;
      updateChaseOverlay();
    }
    return;
  }
  const fxxList = state.availableFxx;
  if (fxxList.length === 0) return;
  const currentIdx = fxxList.indexOf(state.currentFxx);
  if (currentIdx > 0) {
    state.currentFxx = fxxList[currentIdx - 1];
    scrubber.value = currentIdx - 1;
    displayCurrentFrame();
  }
}

function createFxxProgress() {
  if (!fxxProgress) return;
  fxxProgress.innerHTML = '';
  
  if (state.dataSource !== 'models') return;
  
  const forecastHours = state.runMeta?.forecast_hours || [];
  if (forecastHours.length === 0) return;

  for (const fxx of forecastHours) {
    const box = document.createElement('div');
    box.className = 'fxx-box';
    box.dataset.fxx = fxx;
    box.title = `F${String(fxx).padStart(2,'0')}`;
    const span = document.createElement('span');
    span.textContent = typeof fxx === 'number' ? String(fxx).padStart(2,'0') : fxx;
    box.appendChild(span);

    box.addEventListener('click', () => {
      state.currentFxx = fxx;
      scrubber.value = state.availableFxx.indexOf(fxx);
      displayCurrentFrame();
    });

    fxxProgress.appendChild(box);
  }
}

function updateFxxProgress() {
  if (!fxxProgress) return;
  
  if (state.dataSource !== 'models') {
    fxxProgress.innerHTML = '';
    return;
  }
  
  const completed = new Set(state.runMeta?.completed_fxx || []);
  fxxProgress.querySelectorAll('.fxx-box').forEach(box => {
    const fxx = box.dataset.fxx;
    const fxxInt = parseInt(fxx, 10);
    const isComplete = completed.has(fxx) || completed.has(fxxInt);
    box.classList.toggle('irrelevant', false);
    box.classList.toggle('complete', isComplete);
    box.classList.toggle('pending', !isComplete);
  });
}

async function pollForNewFrames() {
  if (state.dataSource !== 'models' || !state.runId) return;
  const requestId = _modelSelectionId;
  const model = state.model;
  const runId = state.runId;
  try {
    const data = await apiFetch(`${BASE}/api/available/${state.model}/${state.runId}?_=${Date.now()}`);
    if (requestId !== _modelSelectionId || model !== state.model || runId !== state.runId || state.dataSource !== 'models') return;
    const previous = new Set(state.availableFxx);
    state.availableByParam = data || {};
    state.availableFxx = state.availableByParam[state.parameter] || [];
    updateParamVisibilityForModel(state.model);
    state.availableFxx = state.availableByParam[state.parameter] || [];
    
    const current = new Set(state.availableFxx);
    if (current.size > previous.size) {
      updateFxxProgress();
      updateScrubber();
      preloadAllDesktopFrames();
    }
  } catch (e) {
    console.warn('Poll failed:', e);
  }
}

function updateParamVisibilityForModel(selectedModel) {
  if (state.dataSource !== 'models') return;

  const availabilityReady = state.runId
    && state.availableByParam
    && Object.keys(state.availableByParam).length > 0;
  let firstVisibleParam = null;
  let activeStillVisible = false;
  
  document.querySelectorAll('.param-btn[data-param]').forEach(btn => {
    const models = btn.dataset.models;
    if (!models) return;
    const modelList = models.split(',');
    const modelOk = modelList.includes(selectedModel);
    const frames = state.availableByParam?.[btn.dataset.param] || [];
    const availabilityOk = !availabilityReady || frames.length > 0;
    const visible = modelOk && availabilityOk;
    btn.style.display = visible ? 'flex' : 'none';
    btn.disabled = !visible;
    if (visible && !firstVisibleParam) firstVisibleParam = btn;
    if (visible && btn.classList.contains('active')) activeStillVisible = true;
  });

  if (availabilityReady && !activeStillVisible && firstVisibleParam) {
    document.querySelectorAll('.param-btn[data-param]').forEach(btn => btn.classList.remove('active'));
    firstVisibleParam.classList.add('active');
    syncAriaPressed();
    state.parameter = firstVisibleParam.dataset.param;
    state.availableFxx = state.availableByParam[state.parameter] || [];
    updateLegend();
  }

  document.querySelectorAll('#modelSidebar .param-group').forEach(group => {
    const visibleItems = group.querySelectorAll('.param-btn[data-param][style*="flex"]');
    group.style.display = visibleItems.length > 0 ? 'block' : 'none';
  });
  renderModelFavorites();
}

async function loadColorScales() {
  try {
    return await apiFetch(`${BASE}/api/colorscales`);
  } catch (e) {
    console.warn('Failed to load color scales:', e);
    return {};
  }
}

async function updateLegend() {
  const requestId = ++_legendRequestId;
  const legendLabel = document.getElementById('legendLabel');
  const legendCanvas = document.getElementById('legendCanvas');
  const legendTicks = document.getElementById('legendTicks');
  
  if (!legendLabel || !legendCanvas || !legendTicks) return;
  document.getElementById('legendNote').textContent = '';
  legendCanvas.hidden = false;
  legendTicks.hidden = false;
  const activeModelParamBtn = document.querySelector('.param-btn[data-param].active');
  const preferredLabel = state.dataSource === 'models'
    ? (activeModelParamBtn?.dataset.legendLabel || activeModelParamBtn?.textContent || '').trim()
    : '';

  let param, label;
  
  if (state.dataSource === 'satellite') {
    const satLegendKeys = {
      // Visible/Near-IR
      'vis':           'goes_vis_02',
      'vis_blue':      'goes_vis_blue_01',
      'veggie':        'goes_veggie',
      'cirrus':        'goes_cirrus_04',
      'snow_ice':      'goes_snow_ice_05',
      'cloud_particle': 'goes_cloud_particle_06',
      // IR bands
      'ir':            'goes_ir_13',
      'ir_14':         'goes_ir_14',
      'ir_dirty':      'goes_ir_dirty_15',
      'co2':           'goes_co2_16',
      'cloud_top':     'goes_cloud_top_11',
      'ozone':         'goes_ozone_12',
      'sw_ir':         'goes_sw_ir_07',
      // Water Vapor
      'wv':            'goes_wv_09',
      'upper_wv':      'goes_upper_wv_08',
      'lower_wv':      'goes_lower_wv_10',
      // Derived products
      'pwat':          'goes_pwat',
      'sst':           'goes_sst',
      'rain_rate':     'goes_rain_rate',
      'stability':     'goes_stability',
      'fire':          'goes_fire',
      // Detail products
      'vis_1km':       'goes_vis_1km',
      'ir_native':     'goes_ir_native',
      // Mesoscale sectors reuse the same visible/IR color scales
      'm1_ir_1km':     'goes_m1_ir_1km',
      'm2_ir_1km':     'goes_m2_ir_1km',
      'm1_vis_1km':    'goes_m1_vis_1km',
      'm2_vis_1km':    'goes_m2_vis_1km',
    };
    param = satLegendKeys[state.satProduct];
    if (!param) {
      // RGB products have no colorscale legend — clear it
      legendLabel.textContent = {
        'natural_color': 'Natural Color RGB',
        'day_cloud_phase': 'Day Cloud Phase RGB',
        'sandwich': 'IR/VIS Sandwich RGB',
      'm1_ir_1km': 'M1 Floater IR',
      'm2_ir_1km': 'M2 Floater IR',
      'm1_vis_1km': 'M1 Floater Visible',
      'm2_vis_1km': 'M2 Floater Visible',
      }[state.satProduct] || state.satProduct;
      const ctx = legendCanvas.getContext('2d');
      ctx.clearRect(0, 0, legendCanvas.width, legendCanvas.height);
      legendTicks.innerHTML = '';
      return;
    }
  } else if (state.dataSource === 'radar' || state.dataSource === 'home') {
    const radarLegendKeys = {
      'REF': 'radar_reflectivity',
      'VEL': 'radar_velocity',
      'SW':  'radar_sw',
      'ZDR': 'radar_zdr',
      'CC':  'radar_cc',
      'PHI': 'radar_phidp',
      'KDP': 'radar_kdp',
      'SRM': 'radar_srm',
      'AZS': 'radar_azs',
      'DIV': 'radar_div',
      'CCT': 'radar_cct',
      'PHT': 'radar_pht',
      'TDS': 'radar_tds',
      'CREF': 'radar_cref',
      'ETOP': 'radar_etop',
      'VIL': 'radar_vil',
      'VXT': 'radar_vxt',
      'HAIL': 'radar_hail',
      'RAIN': 'radar_rain',
    };
    const homeRadarProduct = state.dataSource === 'home' ? 'REF' : state.radarProduct;
    param = radarLegendKeys[homeRadarProduct];
    if (!param) {
      legendLabel.textContent = homeRadarProduct;
      const ctx = legendCanvas.getContext('2d');
      ctx.clearRect(0, 0, legendCanvas.width, legendCanvas.height);
      legendTicks.innerHTML = '';
      return;
    }
  } else if (state.dataSource === 'chase') {
    param = chaseState.param;
  } else {
    param = state.parameter;
  }
  
  try {
    const paletteQuery = state.dataSource === 'radar'
      ? `?radar_palette_version=${state.radarFrames[state.radarFrameIdx]?.palette_version || 1}`
      : state.dataSource === 'home' ? '?radar_palette_version=1' : paletteQueryForCurrentRun();
    const data = await apiFetch(`${BASE}/api/legend/${param}${paletteQuery}`);
    if (requestId !== _legendRequestId) return;
    const title = preferredLabel || data.label || label || param;
    legendLabel.textContent = `${title}${data.unit && !title.includes(data.unit) ? ` · ${data.unit}` : ''}`;
    document.getElementById('legendNote').textContent = data.annotation || '';
    legendCanvas.hidden = Boolean(data.contour_only);
    legendTicks.hidden = Boolean(data.contour_only);
    
    const ctx = legendCanvas.getContext('2d');
    ctx.clearRect(0, 0, legendCanvas.width, legendCanvas.height);
    
    if (data.discrete && data.colors && data.bounds && data.bounds.length >= 2) {
      const min = Number(data.bounds[0]);
      const max = Number(data.bounds[data.bounds.length - 1]);
      const range = Math.max(max - min, 1e-6);
      for (let i = 0; i < data.colors.length; i++) {
        const start = ((Number(data.bounds[i]) - min) / range) * legendCanvas.width;
        const upperIdx = Math.min(i + 1, data.bounds.length - 1);
        const end = ((Number(data.bounds[upperIdx]) - min) / range) * legendCanvas.width;
        ctx.fillStyle = data.colors[i];
        ctx.fillRect(start, 0, Math.max(end - start, 1), legendCanvas.height);
      }
    } else if (data.stops && data.stops.length > 0) {
      const gradient = ctx.createLinearGradient(0, 0, legendCanvas.width, 0);
      for (const stop of data.stops) {
        gradient.addColorStop(stop.pos, stop.color);
      }
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, legendCanvas.width, legendCanvas.height);
    }
    
    legendTicks.innerHTML = '';
    const ticks = Array.isArray(data.tick_vals) && data.tick_vals.length > 0
      ? data.tick_vals
      : null;
    if (ticks) {
      const min = Number(data.bounds?.[0] ?? data.vmin);
      const max = Number(data.bounds?.at(-1) ?? data.vmax);
      const range = Math.max(max - min, 1e-6);
      for (const rawValue of ticks) {
        const value = Number(rawValue);
        const tick = document.createElement('span');
        tick.style.left = `${Math.max(0, Math.min(1, (value - min) / range)) * 100}%`;
        tick.textContent = Math.abs(value) < 10 && !Number.isInteger(value) ? value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '') : Math.round(value);
        legendTicks.appendChild(tick);
      }
    } else if (data.vmin !== undefined && data.vmax !== undefined) {
      const steps = 5;
      for (let i = 0; i <= steps; i++) {
        const value = data.vmin + (data.vmax - data.vmin) * (i / steps);
        const tick = document.createElement('span');
        tick.style.left = `${(i / steps) * 100}%`;
        tick.textContent = Math.round(value);
        legendTicks.appendChild(tick);
      }
    }
  } catch (e) {
    if (requestId === _legendRequestId) legendLabel.textContent = preferredLabel || label || param;
  }
  
  if (state.dataSource === 'models') {
    updateMapInfoForModel();
  }
}

async function refreshModelStatus() {
  if (document.hidden) return;
  if (state.dataSource !== 'models') return;
  try {
    const resp = await fetch(`${BASE}/api/dashboard`);
    if (resp.ok) {
      const data = await resp.json();
      if (data.models) updateMiniModelStatus(data.models);
    }
  } catch (e) {}
}

function updateMiniModelStatus(models) {
  models.forEach(m => {
    const fill = document.getElementById(`mini-fill-${m.name}`);
    const badge = document.getElementById(`mini-badge-${m.name}`);
    const text = document.getElementById(`mini-text-${m.name}`);
    if (!fill || !badge || !text) return;

    const pct = m.total_fxx ? (m.completed_fxx / m.total_fxx) * 100 : 0;
    fill.style.width = `${pct}%`;

    if (m.data_stale && m.status !== 'processing') {
      badge.textContent = 'STALE';
      badge.className = 'model-compact-badge stale';
      text.textContent = `Last run ${m.last_run || '--'}`;
    } else if (m.status === 'processing') {
      badge.textContent = 'PROCESSING';
      badge.className = 'model-compact-badge processing';
      text.textContent = `${m.completed_fxx}/${m.total_fxx} (${Math.round(pct)}%)`;
    } else if (m.status === 'complete') {
      badge.textContent = 'COMPLETE';
      badge.className = 'model-compact-badge complete';
      text.textContent = `F${String(m.max_completed_fxx || 0).padStart(2,'0')} done`;
    } else {
      badge.textContent = 'IDLE';
      badge.className = 'model-compact-badge';
      text.textContent = m.last_run || '--';
    }
  });
}

// ---------------------------------------------------------------------------
// Satellite archive status
// ---------------------------------------------------------------------------

// Product display labels (short names for compact display)
const SAT_PRODUCT_LABELS = {
  ir: 'IR Band 13', wv: 'Mid WV Band 9', vis: 'Visible Band 2',
  vis_blue: 'Visible Band 1', veggie: 'Veggie Band 3',
  sw_ir: 'SW IR Band 7', upper_wv: 'Upper WV Band 8', lower_wv: 'Lower WV Band 10',
  snow_ice: 'Snow/Ice Band 5', cirrus: 'Cirrus Band 4',
  cloud_particle: 'Cloud Part Band 6',
  ir_14: 'IR Band 14', ir_dirty: 'Dirty IR Band 15',
  co2: 'CO2 Band 16', cloud_top: 'Cloud Top Band 11', ozone: 'Ozone Band 12',
  natural_color: 'Natural Color', day_cloud_phase: 'Day Cloud Phase',
  sandwich: 'Sandwich',
  pwat: 'Precip Water', sst: 'Sea Surface Temp',
  rain_rate: 'Rainfall Rate', stability: 'Lifted Index',
  fire: 'Fire/Hot Spot',
  vis_1km: 'Visible 1km', ir_native: 'IR Native CMIP',
  m1_ir_1km: 'M1 Floater IR', m2_ir_1km: 'M2 Floater IR',
  m1_vis_1km: 'M1 Floater Vis', m2_vis_1km: 'M2 Floater Vis',
};

// Product display order
const SAT_PRODUCT_ORDER = [
  'vis_1km','ir_native',
  'm1_ir_1km','m2_ir_1km','m1_vis_1km','m2_vis_1km',
  'vis','vis_blue','veggie','cirrus','snow_ice','cloud_particle',
  'ir','ir_14','ir_dirty','co2','cloud_top','ozone','sw_ir',
  'upper_wv','wv','lower_wv',
  'natural_color','sandwich','day_cloud_phase',
  'pwat','sst','rain_rate','stability','fire',
];

let _satStatusData = null;
let _satStatusTab = 'goes19';

async function refreshSatelliteStatus() {
  if (document.hidden) return;
  try {
    const data = await apiFetch(`${BASE}/api/satellite/status`);
    _satStatusData = data;
    renderSatelliteStatus();
  } catch (e) {}
}

function renderSatelliteStatus() {
  const container = document.getElementById('satProductStatus');
  if (!container || !_satStatusData) return;
  const sourceData = _satStatusData[_satStatusTab] || {};

  const rows = SAT_PRODUCT_ORDER.map(pid => {
    const info = sourceData[pid];
    if (!info) return '';
    const label = SAT_PRODUCT_LABELS[pid] || pid;
    const isFloater = ['m1_ir_1km','m2_ir_1km','m1_vis_1km','m2_vis_1km'].includes(pid);
    const badgeClass = info.status === 'live' ? 'live' : info.status === 'stale' ? 'stale' : info.status === 'old' ? 'old' : '';
    const badgeText = info.status === 'live' ? 'LIVE' : info.status === 'stale' ? 'STALE' : info.status === 'old' ? 'OLD' : 'NO DATA';
    const pct = info.pct ?? 0;
    const frameLabel = info.frames >= info.max_frames
      ? `${info.max_frames}/${info.max_frames} (full)`
      : `${info.frames}/${info.max_frames}`;
    let subtext = (isFloater && info.status === 'no_data')
      ? 'On-demand floater — no active sector'
      : `${frameLabel} frames`;
    if (info.age_min !== null && info.age_min !== undefined) {
      const age = info.age_min < 60
        ? `${Math.round(info.age_min)}m ago`
        : `${(info.age_min / 60).toFixed(1)}h ago`;
      subtext += ` · ${info.last_update || ''} (${age})`;
    }
    return `<div class="sat-compact">
      <div class="sat-compact-header">
        <span class="sat-compact-name">${label}</span>
        <span class="sat-compact-badge ${badgeClass}">${badgeText}</span>
      </div>
      <div class="sat-compact-bar"><div class="sat-compact-fill" style="width:${pct}%"></div></div>
      <div class="sat-compact-text">${subtext}</div>
    </div>`;
  }).join('');

  container.innerHTML = rows || '<div class="sat-status-loading">No data</div>';
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------
async function init() {
  const mobileLink = document.querySelector('[data-mobile-link]');
  if (mobileLink) {
    mobileLink.href = `${BASE}/mobile.html`;
    mobileLink.addEventListener('click', () => {
      const url = buildShareableDesktopUrl();
      url.pathname = `${BASE}/mobile.html`;
      mobileLink.href = url.href;
    });
  }
  window.__wxInitCount = (window.__wxInitCount || 0) + 1;
  initMap();
  ensureScrubberDebug();
  if (DEBUG_SCRUBBER && typeof window.__wxPushDebugEvent === 'function') {
    window.__wxPushDebugEvent(`init#${window.__wxInitCount} href=${window.location.href}`);
  }
  if (CAPTURE_MODE && document.body) {
    document.body.dataset.captureReady = '0';
  }
  if (modelSelect) modelSelect.value = state.model;
  if (satelliteSelect) satelliteSelect.value = state.satellite;
  buildSatelliteRegionalProducts();
  buildModelStatusCards();
  setSatelliteScope(state.satScope || (state.satRegion ? 'regional' : 'national'), true);
  const radarSiteSelectInit = document.getElementById('radarSiteSelect');
  if (radarSiteSelectInit) radarSiteSelectInit.value = state.radarSite;
  populateRadarSiteSelect();
  const radarTiltSelectInit = document.getElementById('radarTiltSelect');
  if (radarTiltSelectInit && Number.isFinite(state.radarTilt)) {
    radarTiltSelectInit.value = String(state.radarTilt);
  }
  document.querySelectorAll('.param-btn[data-radar-product]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.radarProduct === state.radarProduct);
  });
  document.querySelectorAll('.param-btn[data-param]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.param === state.parameter);
  });

  syncAriaPressed();
  initParamFilter();
  initModelFavorites();
  applyParamTooltips();

  state.severeWarningTimer = setInterval(refreshSevereWarnings, 60000);
  state.overlayRefreshTimer = setInterval(refreshExternalOverlays, 120000);
  setInterval(refreshModelStatus, 15000);
  
  // Source tab handlers
  // Keyboard navigation for the data-source tablist (ARIA tabs pattern)
  const _sourceTablist = document.querySelector('.source-tabs');
  if (_sourceTablist) {
    // ARIA tabs pattern with manual activation: arrows only move focus
    // (roving tabindex); Enter/Space activate the focused tab natively.
    const tabs = [..._sourceTablist.querySelectorAll('[role="tab"]')];
    const focusTab = (tab) => {
      tabs.forEach((t) => { t.tabIndex = -1; });
      tab.tabIndex = 0;
      tab.focus();
    };
    tabs.forEach((t) => { t.tabIndex = t.classList.contains('active') ? 0 : -1; });
    tabs.forEach((t) => t.addEventListener('click', () => {
      tabs.forEach((u) => { u.tabIndex = -1; });
      t.tabIndex = 0;
    }));
    _sourceTablist.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      const idx = tabs.indexOf(document.activeElement);
      if (idx === -1) return;
      e.preventDefault();
      focusTab(e.key === 'ArrowRight'
        ? tabs[(idx + 1) % tabs.length]
        : tabs[(idx - 1 + tabs.length) % tabs.length]);
    });
  }

  tabHome.addEventListener('click', () => {
    if (DEBUG_SCRUBBER && typeof window.__wxPushDebugEvent === 'function') window.__wxPushDebugEvent('tabHome click');
    switchDataSource('home');
  });
  tabModels.addEventListener('click', () => {
    if (DEBUG_SCRUBBER && typeof window.__wxPushDebugEvent === 'function') window.__wxPushDebugEvent('tabModels click');
    switchDataSource('models');
  });
  tabSatellite.addEventListener('click', () => {
    if (DEBUG_SCRUBBER && typeof window.__wxPushDebugEvent === 'function') window.__wxPushDebugEvent('tabSatellite click');
    switchDataSource('satellite');
  });
  tabRadar.addEventListener('click', () => {
    if (DEBUG_SCRUBBER && typeof window.__wxPushDebugEvent === 'function') window.__wxPushDebugEvent('tabRadar click');
    switchDataSource('radar');
  });
  tabChase.addEventListener('click', () => {
    if (DEBUG_SCRUBBER && typeof window.__wxPushDebugEvent === 'function') window.__wxPushDebugEvent('tabChase click');
    switchDataSource('chase');
  });
  if (tabSurface) {
    tabSurface.addEventListener('click', () => {
      if (DEBUG_SCRUBBER && typeof window.__wxPushDebugEvent === 'function') window.__wxPushDebugEvent('tabSurface click');
      switchDataSource('surface');
    });
  }

  if (DEBUG_SCRUBBER) {
    document.addEventListener('click', (evt) => {
      if (typeof window.__wxPushDebugEvent !== 'function') return;
      const target = evt.target;
      if (!(target instanceof Element)) {
        window.__wxPushDebugEvent('document click target=(non-element)');
        return;
      }
      const id = target.id ? `#${target.id}` : '';
      const cls = target.className && typeof target.className === 'string'
        ? `.${target.className.trim().split(/\s+/).slice(0, 2).join('.')}`
        : '';
      window.__wxPushDebugEvent(`document click target=${target.tagName.toLowerCase()}${id}${cls}`);
    }, true);
    window.addEventListener('hashchange', () => {
      if (typeof window.__wxPushDebugEvent === 'function') {
        window.__wxPushDebugEvent(`hashchange -> ${window.location.hash || '-'}`);
      }
    });
    window.addEventListener('popstate', () => {
      if (typeof window.__wxPushDebugEvent === 'function') {
        window.__wxPushDebugEvent(`popstate -> ${window.location.hash || '-'}`);
      }
    });
    window.addEventListener('pageshow', (evt) => {
      if (typeof window.__wxPushDebugEvent === 'function') {
        window.__wxPushDebugEvent(`pageshow persisted=${evt.persisted ? '1' : '0'} href=${window.location.href}`);
      }
    });
    window.addEventListener('pagehide', (evt) => {
      if (typeof window.__wxPushDebugEvent === 'function') {
        window.__wxPushDebugEvent(`pagehide persisted=${evt.persisted ? '1' : '0'} href=${window.location.href}`);
      }
    });
    window.addEventListener('beforeunload', () => {
      if (typeof window.__wxPushDebugEvent === 'function') {
        window.__wxPushDebugEvent(`beforeunload href=${window.location.href}`);
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (typeof window.__wxPushDebugEvent === 'function') {
        window.__wxPushDebugEvent(`visibilitychange -> ${document.visibilityState}`);
      }
    });
    window.addEventListener('focus', () => {
      if (typeof window.__wxPushDebugEvent === 'function') window.__wxPushDebugEvent('window focus');
    });
    window.addEventListener('blur', () => {
      if (typeof window.__wxPushDebugEvent === 'function') window.__wxPushDebugEvent('window blur');
    });
  }
  
  // Model controls
	modelSelect.addEventListener('change', () => {
    const preferredValidTime = getModelValidTime(state.runId, state.currentFxx);
    pause();
    clearModelLayers();
    state.model = modelSelect.value;
    syncShareableDesktopUrl();
    loadRuns(preferredValidTime);
	  updateParamVisibilityForModel(state.model);
	});
  
  runSelect.addEventListener('change', () => {
    const preferredValidTime = getModelValidTime(state.runId, state.currentFxx);
    pause();
    clearModelLayers();
    selectRun(runSelect.value, preferredValidTime);
  });

  if (soundingProbeBtn) {
    soundingProbeBtn.addEventListener('click', () => {
      setSoundingProbeEnabled(!state.soundingProbeEnabled);
      if (state.soundingProbeEnabled) {
        window.WXMeteogram?.setEnabled(false);
        if (state.soundingPoint && state.soundingData) {
          if (soundingPanel) soundingPanel.classList.remove('hidden');
          if (soundingPanelTitle) soundingPanelTitle.textContent = formatLatLonLabel(state.soundingPoint);
          if (soundingPanelMeta) soundingPanelMeta.textContent = `${state.model.toUpperCase()} ${state.runId || '--'} ${parseFxx(state.currentFxx).display}`;
        }
      } else if (!state.soundingData) {
        closeSoundingPanel(false);
      }
    });
  }

  if (soundingPanelClose) {
    soundingPanelClose.addEventListener('click', () => {
      closeSoundingPanel(true);
    });
  }

  if (soundingPanelDownload) {
    soundingPanelDownload.addEventListener('click', async () => {
      try {
        await downloadSoundingScreenshot();
      } catch (err) {
        console.error('Sounding download failed:', err);
      }
    });
  }

  if (soundingPanelCopy) {
    soundingPanelCopy.addEventListener('click', async () => {
      try {
        await copySoundingScreenshot();
      } catch (err) {
        console.error('Sounding copy failed:', err);
      }
    });
  }
  
  // Satellite controls
  satelliteSelect.addEventListener('change', () => {
    pause();
    clearSatelliteLayers();
    state.satellite = satelliteSelect.value;
    syncShareableDesktopUrl();
    loadSatelliteData();
	});

  // Scrubber controls
  scrubber.addEventListener('input', () => {
    const idx = parseInt(scrubber.value, 10);
    if (state.dataSource === 'home') {
      state.homeRadarFrameIdx = idx;
      showHomeRadarFrame(idx);
      updateMapInfoForHomeRadar();
    } else if (state.dataSource === 'satellite') {
      state.satFrameIdx = idx;
      showSatelliteFrame(idx);
    } else if (state.dataSource === 'radar') {
      state.radarFrameIdx = idx;
      showRadarFrame(idx);
    } else if (state.dataSource === 'chase') {
      if (chaseState.availableFxx[idx] !== undefined) {
        chaseState.currentFxx = chaseState.availableFxx[idx];
        updateChaseOverlay();
      }
    } else if (state.availableFxx[idx] !== undefined) {
      state.currentFxx = state.availableFxx[idx];
      displayCurrentFrame();
    }
    syncShareableDesktopUrl();
  });
  
  playBtn.addEventListener('click', togglePlay);

  document.querySelectorAll('.speed-btn').forEach(btn => {
    btn.addEventListener('click', () => setPlaySpeed(parseInt(btn.dataset.speed)));
  });
  
  // Keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' ||
        e.target.tagName === 'BUTTON' || e.target.tagName === 'TEXTAREA' ||
        e.target.isContentEditable) return;
    
    switch (e.key) {
      case ' ':
        e.preventDefault();
        togglePlay();
        break;
      case 'ArrowRight':
        e.preventDefault();
        if (state.dataSource === 'satellite') satStepForward();
        else if (state.dataSource === 'radar') radarStepForward();
        else stepForward();
        break;
      case 'ArrowLeft':
        e.preventDefault();
        if (state.dataSource === 'satellite') satStepBackward();
        else if (state.dataSource === 'radar') radarStepBackward();
        else stepBackward();
        break;
    }
  });
  
  // Parameter buttons
	document.querySelectorAll('.param-btn[data-param]').forEach(btn => {
	  btn.addEventListener('click', () => {
	    document.querySelectorAll('.param-btn[data-param]').forEach(b => b.classList.remove('active'));
	    btn.classList.add('active');
	    syncAriaPressed();
	    state.parameter = btn.dataset.param;
      renderModelFavorites();
      syncShareableDesktopUrl();
      
      if (state.dataSource === 'models') {
        const preferredValidTime = getModelValidTime(state.runId, state.currentFxx);
        const wasPlaying = state.isPlaying;
        pause();
        clearModelLayers();
  clearLoadQueue();
        loadAvailability().then(() => {
          updateParamVisibilityForModel(state.model);
          updateScrubber();
          if (state.availableFxx.length > 0) {
            const matchedFxx = findClosestAvailableFxx(state.runId, state.availableFxx, preferredValidTime);
            state.currentFxx = matchedFxx !== null ? matchedFxx : state.availableFxx[0];
            scrubber.value = Math.max(0, state.availableFxx.findIndex((value) => String(value) === String(state.currentFxx)));
            preloadAllDesktopFrames();
            showOverlay();
            displayCurrentFrame();
            if (wasPlaying) play();
          }
          updateLegend();
        });
      }
    });
  });
  
  // Satellite product buttons
  const satButtons = document.querySelectorAll('.param-btn[data-sat-product]');
  if (DEBUG_SCRUBBER) console.log(`Found ${satButtons.length} satellite product buttons`);
  satButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      if (DEBUG_SCRUBBER) console.log(`Satellite button clicked: ${btn.dataset.satProduct}`);
      const wasPlaying = state.isPlaying;
      pause();
    state.satProduct = btn.dataset.satProduct;
      state.satRegion = btn.getAttribute('data-sat-region') ?? '';
      syncShareableDesktopUrl();
      // Determine scope from the button's parent group, not just the region attribute
      const scopeGroup = btn.closest('[data-sat-scope-group]');
      const btnScope = (scopeGroup && scopeGroup.dataset.satScopeGroup === 'regional') || state.satRegion ? 'regional' : 'national';
      state.satScope = btnScope;
      // Floaters live under regional tab but don't need a region — skip region logic
      const isFloater = ['m1_ir_1km','m2_ir_1km','m1_vis_1km','m2_vis_1km'].includes(state.satProduct);
      if (!isFloater) {
        ensureValidSatelliteScopeSelection();
      }
      // For floaters, ensure we use goes19 directly
      if (isFloater) {
        state.satellite = 'goes19';
      }
      updateSatelliteScopeButtons();
      updateSatelliteScopeVisibility();
      updateSatelliteProductButtons();
      clearSatelliteLayers();
    loadSatelliteData().then(() => { if (wasPlaying) play(); });
    updateLegend();
    });
  });

  [satScopeNational, satScopeRegional].forEach((btn) => {
    if (!btn) return;
    btn.addEventListener('click', () => {
      const nextScope = btn.dataset.satScope || 'national';
      if (nextScope === state.satScope) return;
      const wasPlaying = state.isPlaying;
      pause();
      setSatelliteScope(nextScope, true);
      syncShareableDesktopUrl();
      clearSatelliteLayers();
      loadSatelliteData().then(() => { if (wasPlaying) play(); });
      updateLegend();
    });
  });

  // Regional satellite: region selector drives the generated product list
  const satRegionSelect = document.getElementById('satRegionSelect');
  if (satRegionSelect) {
    satRegionSelect.addEventListener('change', () => {
      if (satRegionSelect.value === state.satRegion) return;
      const wasPlaying = state.isPlaying;
      pause();
      state.satRegion = satRegionSelect.value;
      state.satScope = 'regional';
      syncShareableDesktopUrl();
      syncSatelliteSourceForRegion();
      ensureValidSatelliteRegionProduct();
      syncSatelliteRegionButtons();
      updateSatelliteScopeButtons();
      updateSatelliteProductButtons();
      clearSatelliteLayers();
      loadSatelliteData().then(() => { if (wasPlaying) play(); });
      updateLegend();
    });
  }
  
  // Radar controls
  const radarSiteSelect = document.getElementById('radarSiteSelect');
  if (radarSiteSelect) {
    radarSiteSelect.addEventListener('change', () => {
      const wasPlaying = state.isPlaying;
      pause();
      clearRadarLayers();
      state.radarSite = radarSiteSelect.value;
      persistRadarSite(state.radarSite);
      state.radarTilt = 0.5;
      syncShareableDesktopUrl();
      loadRadarData().then(() => { if (wasPlaying) play(); });
      loadRadarTilts();
      flyToRadarSite(state.radarSite);
    });
  }
  
  // Radar product buttons
  const radarButtons = document.querySelectorAll('.param-btn[data-radar-product]');
  if (DEBUG_SCRUBBER) console.log(`Found ${radarButtons.length} radar product buttons`);
  radarButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      if (DEBUG_SCRUBBER) console.log(`Radar button clicked: ${btn.dataset.radarProduct}`);
      const wasPlaying = state.isPlaying;
      pause();
	    document.querySelectorAll('.param-btn[data-radar-product]').forEach(b => b.classList.remove('active'));
	    btn.classList.add('active');
	    syncAriaPressed();
      state.radarProduct = btn.dataset.radarProduct;
      state.radarTilt = 0.5; // Reset to lowest tilt on product change
      syncShareableDesktopUrl();
      clearRadarLayers();
      const tiltSelect = document.getElementById('radarTiltSelect');
      if (tiltSelect) tiltSelect.value = '0.5';
      loadRadarData().then(() => { if (wasPlaying) play(); });
      updateLegend();
    });
  });

  // Radar tilt selector
  const radarTiltSelect = document.getElementById('radarTiltSelect');
  if (radarTiltSelect) {
	  radarTiltSelect.addEventListener('change', () => {
      pause();
      clearRadarLayers();
      state.radarTilt = parseFloat(radarTiltSelect.value);
      syncShareableDesktopUrl();
      if (state.radarTilt === 0.5) {
        loadRadarData();  // Pre-rendered, use normal frames API
      } else {
        loadRadarTilt();  // On-demand render
      }
      updateMapInfoForRadar();
    });
  }
  
  // Radar loop length selector
  const radarLoopLength = document.getElementById('radarLoopLength');
  if (radarLoopLength) {
    radarLoopLength.addEventListener('change', () => {
      pause();
      clearRadarLayers();
      state.radarLoopLength = parseInt(radarLoopLength.value, 10);
      loadRadarData();
    });
  }

  if (watchWarningToggle) {
    watchWarningToggle.addEventListener('change', async () => {
      state.showWatchesWarnings = watchWarningToggle.checked;
      await refreshAlertOverlay();
    });
  }


  if (mcdToggle) {
    mcdToggle.addEventListener('change', async () => {
      state.showMcd = mcdToggle.checked;
      await refreshMcdOverlay();
    });
  }
  if (spotterToggle) {
    spotterToggle.addEventListener('change', async () => {
      state.showSpotters = spotterToggle.checked;
      await refreshSpotterOverlay();
    });
  }
  if (surfaceRadarToggle) {
    surfaceRadarToggle.addEventListener('change', () => {
      syncSurfaceObsMode(state.dataSource);
    });
  }

  if (spotterFilterSelect) {
    spotterFilterSelect.addEventListener('change', async () => {
      state.spotterFilter = spotterFilterSelect.value;
      if (state.showSpotters) await refreshSpotterOverlay();
    });
  }
  // Parameter group expand/collapse toggles
  document.addEventListener('click', (event) => {
    const toggle = event.target.closest('.param-group-toggle');
    if (!toggle) return;
    const group = toggle.closest('.param-group');
    if (!group) return;
    event.preventDefault();
    event.stopPropagation();
    const expanded = toggle.getAttribute('aria-expanded') === 'true';
    toggle.setAttribute('aria-expanded', String(!expanded));
    group.classList.toggle('expanded', !expanded);
  });

  // Mobile menu
  const mobileMenuBtn = document.getElementById('mobileMenuBtn');
  const mobileBackdrop = document.getElementById('mobileBackdrop');

  if (mobileMenuBtn) {
    mobileMenuBtn.addEventListener('click', () => {
      let sidebar;
      if (state.dataSource === 'models') sidebar = document.getElementById('modelSidebar');
      else if (state.dataSource === 'radar') sidebar = document.getElementById('radarSidebar');
      else if (state.dataSource === 'satellite') sidebar = document.getElementById('satSidebar');
      else if (state.dataSource === 'surface') sidebar = document.getElementById('surfaceSidebar');
      if (!sidebar) return;
      const open = sidebar.classList.toggle('mobile-open');
      if (mobileBackdrop) mobileBackdrop.classList.toggle('active', open);
    });
  }

  if (mobileBackdrop) {
    mobileBackdrop.addEventListener('click', () => {
      document.querySelectorAll('.param-sidebar.mobile-open').forEach(s => s.classList.remove('mobile-open'));
      mobileBackdrop.classList.remove('active');
    });
  }

  if (captureBtn) {
    captureBtn.addEventListener('click', async () => {
      try {
        await downloadMapScreenshot();
      } catch (err) {
        console.error('Screenshot download failed:', err);
      }
    });
  }

  const loopCaptureBtn = document.getElementById('loopCaptureBtn');
  if (loopCaptureBtn) loopCaptureBtn.addEventListener('click', downloadLoopGif);

  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      try {
        await copyMapScreenshot();
      } catch (err) {
        console.error('Screenshot copy failed:', err);
      }
    });
  }

  if (BACKGROUND_ONLY && CAPTURE_MODE) {
    // Fit map to CONUS and mark ready after boundaries load (~3s)
    if (leafletMap) {
      leafletMap.fitBounds([[21, -135], [53, -60]], { animate: false, padding: [0, 0] });
    }
    setTimeout(() => markCaptureReady('background_only'), 3000);
    return;
  }

  switchDataSource(getInitialView());
  updateSoundingProbeUi();
  void refreshModelStatus();

}

init();
