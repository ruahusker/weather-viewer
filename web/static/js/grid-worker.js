/**
 * Grid Worker - Client-side value lookup for Models and Radar
 * Handles binary grid data and performs fast interpolation
 */

const gridCache = new Map();

// Message handler
self.onmessage = async (e) => {
  const { type, model, runId, fxx, param, site, product, tilt, lat, lon, cacheKey } = e.data;
  
  try {
    switch (type) {
      case 'LOAD_MODEL_GRID':
        await loadModelGrid(model, runId, fxx, param, cacheKey);
        break;
      case 'LOAD_RADAR_GRID':
        await loadRadarGrid(site, product, tilt, cacheKey);
        break;
      case 'QUERY_VALUE':
        queryCachedValue(lat, lon, cacheKey);
        break;
      case 'CLEAR_CACHE':
        gridCache.clear();
        self.postMessage({ type: 'CACHE_CLEARED' });
        break;
    }
  } catch (err) {
    self.postMessage({ type: 'ERROR', error: err.message, cacheKey });
  }
};

// Decompress gzip if needed
async function decompress(buffer) {
  // Check if already decompressed (first 2 bytes of gzip are 0x1f 0x8b)
  const view = new DataView(buffer);
  if (view.getUint16(0) !== 0x1f8b) {
    return buffer; // Not gzip
  }
  
  // Use DecompressionStream
  const stream = new Response(buffer).body.pipeThrough(new DecompressionStream('gzip'));
  return await new Response(stream).arrayBuffer();
}

// Load model grid from API
async function loadModelGrid(model, runId, fxx, param, cacheKey) {
  const resp = await fetch(`/api/grid/${model}/${runId}/${fxx}/${param}`);
  
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
  }
  
  const buffer = await resp.arrayBuffer();
  const decompressed = await decompress(buffer);
  const grid = decodeModelGrid(decompressed);
  
  gridCache.set(cacheKey, { type: 'model', ...grid });
  self.postMessage({ type: 'GRID_LOADED', cacheKey, shape: grid.shape, sizeMB: (decompressed.byteLength / 1024 / 1024).toFixed(2) });
}

// Load radar grid from API
async function loadRadarGrid(site, product, tilt, cacheKey) {
  const resp = await fetch(`/api/radar/grid/${site}/${product}?tilt=${tilt}`);
  
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
  }
  
  const buffer = await resp.arrayBuffer();
  const decompressed = await decompress(buffer);
  const grid = decodeRadarGrid(decompressed);
  
  gridCache.set(cacheKey, { type: 'radar', ...grid });
  self.postMessage({ type: 'GRID_LOADED', cacheKey, shape: [grid.nAz, grid.nGates], sizeMB: (decompressed.byteLength / 1024 / 1024).toFixed(2) });
}

// Decode model grid binary format
function decodeModelGrid(buffer) {
  const view = new DataView(buffer);
  const ni = view.getUint32(0, true);
  const nj = view.getUint32(4, true);
  const latMin = view.getFloat32(8, true);
  const latMax = view.getFloat32(12, true);
  const lonMin = view.getFloat32(16, true);
  const lonMax = view.getFloat32(20, true);
  const valMin = view.getFloat32(24, true);
  const valMax = view.getFloat32(28, true);
  
  const data = new Float32Array(buffer, 32); // offset after 32-byte header
  
  return {
    data,
    shape: [nj, ni],
    bounds: { latMin, latMax, lonMin, lonMax, valMin, valMax }
  };
}

// Decode radar grid binary format
function decodeRadarGrid(buffer) {
  const view = new DataView(buffer);
  const siteLat = view.getFloat32(0, true);
  const siteLon = view.getFloat32(4, true);
  const nAz = view.getUint16(8, true);
  const nGates = view.getUint16(10, true);
  const gateSpacingKm = view.getFloat32(12, true);
  const maxRangeKm = view.getFloat32(16, true);
  const vmin = view.getFloat32(20, true);
  const vmax = view.getFloat32(24, true);
  
  const azOffset = 28;
  const dataOffset = azOffset + nAz * 4;
  
  const azimuths = new Float32Array(buffer, azOffset, nAz);
  const data = new Float32Array(buffer, dataOffset, nAz * nGates);
  
  return {
    siteLat, siteLon,
    nAz, nGates,
    gateSpacingKm, maxRangeKm,
    vmin, vmax,
    azimuths, data
  };
}

// Query cached grid
function queryCachedValue(lat, lon, cacheKey) {
  const cached = gridCache.get(cacheKey);
  if (!cached) {
    self.postMessage({ type: 'CACHE_MISS', cacheKey, lat, lon });
    return;
  }
  
  let value, unit, label;
  
  if (cached.type === 'model') {
    value = interpolateModel(cached, lat, lon);
    // Get units from param name (simplified)
    unit = getModelUnit(cacheKey);
    label = getModelLabel(cacheKey);
  } else {
    value = interpolateRadar(cached, lat, lon);
    unit = getRadarUnit(cached.vmin, cached.vmax);
    label = 'Radar Reflectivity';
  }
  
  self.postMessage({ type: 'VALUE', value, unit, label, cacheKey, lat, lon });
}

// Fast nearest-neighbor interpolation for model grid
function interpolateModel(grid, lat, lon) {
  const { data, shape, bounds } = grid;
  const [nj, ni] = shape;
  
  // Convert lat/lon to grid indices
  const i = (lon - bounds.lonMin) / (bounds.lonMax - bounds.lonMin) * (ni - 1);
  const j = (bounds.latMax - lat) / (bounds.latMax - bounds.latMin) * (nj - 1);
  
  // Nearest neighbor (faster than bilinear)
  const ci = Math.max(0, Math.min(ni - 1, Math.round(i)));
  const cj = Math.max(0, Math.min(nj - 1, Math.round(j)));
  
  return data[cj * ni + ci];
}

// Radar polar coordinate lookup
function interpolateRadar(grid, lat, lon) {
  const { siteLat, siteLon, azimuths, data, nGates, gateSpacingKm } = grid;
  
  // Haversine distance and bearing from site to target
  const R = 6371.0;
  const toRad = Math.PI / 180;
  const lat1 = siteLat * toRad;
  const lat2 = lat * toRad;
  const dLat = (lat - siteLat) * toRad;
  const dLon = (lon - siteLon) * toRad;
  
  const a = Math.sin(dLat/2)**2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon/2)**2;
  const rangeKm = 2 * R * Math.asin(Math.sqrt(a));
  
  if (rangeKm > grid.maxRangeKm) {
    return null; // Outside radar range
  }
  
  // Bearing calculation
  const x = Math.sin(dLon) * Math.cos(lat2);
  const y = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  const bearing = (Math.atan2(x, y) * 180 / Math.PI + 360) % 360;
  
  // Find nearest azimuth using binary search for speed
  const azIdx = binarySearchClosest(azimuths, bearing);
  const gateIdx = Math.floor(rangeKm / gateSpacingKm);
  
  if (gateIdx < 0 || gateIdx >= nGates) {
    return null;
  }
  
  const value = data[azIdx * nGates + gateIdx];
  return isNaN(value) ? null : value;
}

// Binary search for closest value in sorted array
function binarySearchClosest(arr, target) {
  let left = 0;
  let right = arr.length - 1;
  
  // Handle wrap-around for azimuths (0-360)
  const normalizedTarget = ((target % 360) + 360) % 360;
  
  while (left < right) {
    const mid = Math.floor((left + right) / 2);
    if (arr[mid] < normalizedTarget) {
      left = mid + 1;
    } else {
      right = mid;
    }
  }
  
  // Check which is closer: left or left-1
  if (left > 0) {
    const diff1 = Math.abs(arr[left] - normalizedTarget);
    const diff2 = Math.abs(arr[left - 1] - normalizedTarget);
    const wrapDiff2 = Math.min(diff2, 360 - diff2);
    const wrapDiff1 = Math.min(diff1, 360 - diff1);
    if (wrapDiff2 < wrapDiff1) {
      left--;
    }
  }
  
  return left;
}

// Extract unit from cache key
function getModelUnit(cacheKey) {
  const param = cacheKey.split(':').pop();
  const units = {
    'temp_2m': '°F',
    'dewpoint_2m': '°F',
    'wind_u_10m': 'kts',
    'wind_v_10m': 'kts',
    'precip': 'in',
    'reflectivity': 'dBZ',
    'cape': 'J/kg',
    'cin': 'J/kg',
  };
  return units[param] || '';
}

// Extract label from cache key
function getModelLabel(cacheKey) {
  const param = cacheKey.split(':').pop();
  const labels = {
    'temp_2m': 'Temperature',
    'dewpoint_2m': 'Dewpoint',
    'wind_u_10m': 'U Wind',
    'wind_v_10m': 'V Wind',
    'precip': 'Precipitation',
    'reflectivity': 'Reflectivity',
    'cape': 'CAPE',
    'cin': 'CIN',
  };
  return labels[param] || param;
}

// Get radar unit from value range
function getRadarUnit(vmin, vmax) {
  if (vmin >= -30 && vmax <= 80) return 'dBZ';
  if (vmin >= -30 && vmax <= 30) return 'kts';
  return '';
}
