/**
 * WX Viewer — shared UI state
 * Single mutable state object shared by all feature modules and, during
 * the migration, by legacy app.js (exposed as window.state by main.js).
 */

export const state = {
  // Data source: 'models' or 'satellite'
  dataSource: 'home',
  
  // Model state
  model: 'hrrr',
  runId: null,
  parameter: 'temp_2m',
  currentFxx: 0,
  runMeta: null,
  availableFxx: [],
  availableByParam: {},
  overlayBounds: null,
  overlayLayer: null,
  chaseReflectivityLayer: null,
  soundingProbeEnabled: false,
  soundingPoint: null,
  soundingData: null,
  preloadedImages: {},
  satPreloadedImages: {},
  radarPreloadedImages: {},
  homeRadarPreloadedImages: {},
  isPlaying: false,
  playSpeedMs: 500,
  playSpeed: 2,
  playTimer: null,
  
  // Satellite state
  satellite: 'goes19',
  satProduct: 'ir',
  satRegion: '',
  satScope: 'national',
  satelliteBounds: null,
  satOverlayLayer: null,
  satMetadata: null,
  satFrames: [],       // [{timestamp, png_path, bounds}, ...] oldest→newest
  satFrameIdx: 0,      // current index into satFrames
  satRequestId: 0,
  
  // Radar state (Level II)
  radarSite: 'KOAX',    // Default to Omaha (Great Plains)
  radarProduct: 'REF',  // Default to Reflectivity
  radarTilt: 0.5,       // Default to lowest tilt
  radarLoopLength: 0,   // Minutes of radar history to show (0 = all)
  radarOverlayLayer: null,
  radarBounds: null,
  radarFrameIdx: 0,
  radarFrames: [],      // For animation when multiple times available
  radarRequestId: 0,
  
  // Shared overlays and warning stream
  severeWarnings: null,
  severeWarningTimer: null,
  alertsLayer: null,
  spcLayer: null,
  spcHatchLayer: null,
  spotterLayer: null,
  overlayRefreshTimer: null,
  showWatchesWarnings: false,
  showMcd: false,
  mcdLayer: null,
  showSpotters: false,
  spcProduct: 'none',
  spotterFilter: 'moving_90',

  // Home dashboard
  radarCompositeLayer: null,
  radarCompositeEnabled: false,
  homeRadarFrames: [],
  homeRadarFrameIdx: 0,
  homeRadarRequestId: 0,
  homeWarningTickerTimer: null,
  homeSeenWarningIds: new Set(),
  homeSidePanelTimer: null,
  homeChaserTimer: null,
  homeRainViewerTimer: null,
  chaserItems: [],
  // Multi-SPC layers: { product_id: L.geoJSON layer }
  homeSpcLayers: {},
  homeSpcDay: 'day1',
  homeSpcView: 'overview',
  homeMcdLayer: null,
  homeMcdEnabled: false,
  featuredChaserLayer: null,
  featuredChaserTimer: null,
  homeSiteCanvasLayer: null, // single composited imageOverlay for zoomed-in sites
  homeSiteCanvasUrl: null,   // current blob URL (for cleanup)
  homeSiteLayersActive: false,
  homeSiteLayersTimer: null,
  homeLayerPanelCollapsed: false,
  homeSidePanelCollapsed: false,
  homeSpcRequestSeq: 0,
  // Alert type filter: set of enabled display_event strings
  homeAlertTypes: new Set([
    'Tornado Warning', 'Tornado Emergency', 'Severe Thunderstorm Warning', 'Flash Flood Warning',
  ]),
};
