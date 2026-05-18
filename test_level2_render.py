#!/usr/bin/env python3
"""Test Level II radar renderer — download one volume scan, render all moments.

Usage:
    python3 test_level2_render.py [SITE]    (default: KTLX)
"""

import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import requests
from PIL import Image
from siphon.radarserver import get_radarserver_datasets, RadarServer

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# THREDDS Level II download
# ---------------------------------------------------------------------------
_l2_server = None

def _get_l2_server() -> RadarServer:
    global _l2_server
    if _l2_server is None:
        ds = get_radarserver_datasets('https://thredds.ucar.edu/thredds/')
        l2_ds = ds['NEXRAD Level II Radar from IDD']
        url = l2_ds.follow().catalog_url
        _l2_server = RadarServer(url)
        logger.info(f"Connected to Level II THREDDS ({len(_l2_server.stations)} stations)")
    return _l2_server


def download_latest_level2(site: str = "KTLX", save_dir: Path = None) -> Path | None:
    """Download the most recent Level II volume scan from THREDDS."""
    save_dir = save_dir or Path("data/radar_cache/level2")
    save_dir.mkdir(parents=True, exist_ok=True)

    rs = _get_l2_server()

    query = rs.query()
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=2)
    query.stations(site).time_range(start, end).variables('Reflectivity')

    if not rs.validate_query(query):
        logger.error(f"Invalid query for {site}")
        return None

    cat = rs.get_catalog(query)
    if not cat.datasets:
        logger.error(f"No Level II data for {site}")
        return None

    # Sort by name (contains timestamp) — skip the very latest (may be incomplete)
    datasets = sorted(cat.datasets.items(), key=lambda x: x[0], reverse=True)
    for idx in ([1, 0] if len(datasets) > 1 else [0]):
        name, dataset = datasets[idx]
        url = dataset.access_urls.get('HTTPServer', '')
        if not url:
            parts = name.replace('.ar2v', '').split('_')
            if len(parts) >= 3:
                date_str = parts[2]
                url = f"https://thredds.ucar.edu/thredds/fileServer/nexrad/level2/IDD/{site}/{date_str}/{name}"
        if url:
            break

    if not url:
        logger.error(f"No HTTP URL for {name}")
        return None

    filename = name
    local_path = save_dir / filename

    if local_path.exists() and local_path.stat().st_size > 500_000:
        logger.info(f"Already cached: {local_path} ({local_path.stat().st_size/1e6:.1f} MB)")
        return local_path

    logger.info(f"Downloading {name} ...")
    t0 = time.time()
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    tmp = local_path.with_suffix('.tmp')
    with open(tmp, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
    tmp.rename(local_path)
    sz = local_path.stat().st_size
    logger.info(f"Downloaded {sz/1e6:.1f} MB in {time.time()-t0:.1f}s")
    return local_path


# ---------------------------------------------------------------------------
# Color tables — 256-level professional gradients
# ---------------------------------------------------------------------------

def _lerp_colors(breakpoints: list[tuple[float, tuple]],
                 vmin: float, vmax: float, n: int = 256) -> np.ndarray:
    """Build an Nx4 RGBA uint8 LUT from breakpoint list."""
    lut = np.zeros((n, 4), dtype=np.uint8)
    for i in range(n):
        val = vmin + (vmax - vmin) * i / (n - 1)
        if val <= breakpoints[0][0]:
            lut[i] = breakpoints[0][1]
            continue
        if val >= breakpoints[-1][0]:
            lut[i] = breakpoints[-1][1]
            continue
        for j in range(len(breakpoints) - 1):
            v0, c0 = breakpoints[j]
            v1, c1 = breakpoints[j + 1]
            if v0 <= val <= v1:
                t = (val - v0) / (v1 - v0) if v1 != v0 else 0
                lut[i] = tuple(int(c0[k] + t * (c1[k] - c0[k])) for k in range(4))
                break
    return lut


def reflectivity_lut() -> tuple[np.ndarray, float, float]:
    """NWS/RadarScope reflectivity, -20 to 80 dBZ."""
    vmin, vmax = -20.0, 80.0
    bp = [
        (-20, (  0,   0,   0,   0)),
        (  4, (  0,   0,   0,   0)),   # transparent below 5 dBZ
        (  5, (  0, 236, 236, 255)),   # teal
        ( 10, (  1, 160, 246, 255)),   # light blue
        ( 15, (  0, 110, 200, 255)),   # blue
        ( 20, (  0, 255,   0, 255)),   # bright green
        ( 25, (  0, 200,   0, 255)),   # green
        ( 30, (  0, 144,   0, 255)),   # dark green
        ( 35, (255, 255,   0, 255)),   # yellow
        ( 40, (231, 192,   0, 255)),   # gold
        ( 45, (255, 144,   0, 255)),   # orange
        ( 50, (255,   0,   0, 255)),   # red
        ( 55, (214,   0,   0, 255)),   # dark red
        ( 60, (192,   0,   0, 255)),   # crimson
        ( 65, (255,   0, 255, 255)),   # magenta
        ( 70, (153,  85, 201, 255)),   # purple
        ( 75, (255, 255, 255, 255)),   # white
        ( 80, (255, 255, 255, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def velocity_lut() -> tuple[np.ndarray, float, float]:
    """RadarScope velocity: inbound (negative)=green, outbound (positive)=red."""
    vmin, vmax = -40.0, 40.0
    bp = [
        (-40, (  0, 100,  50, 255)),   # dark green (strong inbound)
        (-30, (  0, 160,   0, 255)),   # deep green
        (-20, (  0, 220,   0, 255)),   # bright green
        (-15, ( 50, 240,  50, 255)),   # light green
        (-10, (130, 255, 130, 255)),   # pale green
        ( -5, (200, 240, 200, 255)),   # very pale green
        ( -1, (180, 180, 180, 100)),   # dim gray
        (  0, (  0,   0,   0,   0)),   # zero = transparent
        (  1, (180, 180, 180, 100)),   # dim gray
        (  5, (240, 200, 200, 255)),   # very pale red
        ( 10, (255, 130, 130, 255)),   # pale red
        ( 15, (240,  50,  50, 255)),   # light red
        ( 20, (220,   0,   0, 255)),   # bright red
        ( 30, (160,   0,   0, 255)),   # deep red
        ( 40, (100,   0,  50, 255)),   # dark maroon (strong outbound)
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def spectrum_width_lut() -> tuple[np.ndarray, float, float]:
    """Spectrum width, 0 to 20 m/s."""
    vmin, vmax = 0.0, 20.0
    bp = [
        ( 0, (  0,   0,   0,   0)),
        ( 1, (  0,  40,  80, 200)),   # dark blue
        ( 3, (  0, 100, 200, 255)),   # blue
        ( 5, (  0, 200, 220, 255)),   # cyan
        ( 7, (  0, 200, 100, 255)),   # teal-green
        (10, (255, 255,   0, 255)),   # yellow
        (13, (255, 160,   0, 255)),   # orange
        (16, (255,   0,   0, 255)),   # red
        (20, (255,   0, 255, 255)),   # magenta
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def zdr_lut() -> tuple[np.ndarray, float, float]:
    """Differential reflectivity, -2 to +6 dB."""
    vmin, vmax = -2.0, 6.0
    bp = [
        (-2.0, (180,   0, 255, 255)),  # violet (negative)
        (-1.0, ( 80,   0, 200, 255)),  # dark purple
        (-0.2, (  0,  80, 180, 255)),  # blue
        ( 0.0, ( 80, 100, 120, 200)),  # steel gray
        ( 0.3, (  0, 180, 200, 255)),  # teal
        ( 1.0, (  0, 200,   0, 255)),  # green
        ( 2.0, (255, 255,   0, 255)),  # yellow
        ( 3.0, (255, 180,   0, 255)),  # orange
        ( 4.0, (255,  50,   0, 255)),  # red
        ( 5.0, (200,   0, 100, 255)),  # magenta
        ( 6.0, (255, 255, 255, 255)),  # white
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def rhohv_lut() -> tuple[np.ndarray, float, float]:
    """Correlation coefficient, 0.2 to 1.05.

    Key: high CC (>0.97) = pure rain/snow (muted).
    Low CC (<0.85) = mixed phase, debris, clutter (vivid warning colors).
    This inverted emphasis highlights the *interesting* signals.
    """
    vmin, vmax = 0.20, 1.05
    bp = [
        (0.20, (  0,   0,   0,   0)),  # transparent (noise floor)
        (0.45, (100,   0, 120, 255)),  # dark purple (non-met)
        (0.60, (180,   0, 255, 255)),  # vivid purple (debris/clutter)
        (0.70, (255,   0,   0, 255)),  # red (mixed phase / tornado debris)
        (0.80, (255, 130,   0, 255)),  # orange (melting / hail)
        (0.85, (255, 220,   0, 255)),  # yellow (mixed precip)
        (0.90, (  0, 220,   0, 255)),  # green (light rain)
        (0.93, ( 50, 200, 100, 255)),  # green-teal
        (0.95, (  0, 180, 180, 255)),  # teal (rain)
        (0.97, ( 80, 140, 200, 255)),  # steel blue (pure rain)
        (0.99, (140, 170, 220, 255)),  # light blue (clean signal)
        (1.00, (200, 210, 230, 255)),  # pale blue-gray
        (1.05, (230, 230, 240, 255)),  # near-white
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def phidp_lut() -> tuple[np.ndarray, float, float]:
    """Differential phase, 0 to 180 degrees."""
    vmin, vmax = 0.0, 180.0
    bp = [
        (  0, (120,  50, 220, 255)),   # purple
        ( 20, (  0,   0, 255, 255)),   # blue
        ( 40, (  0, 180, 255, 255)),   # cyan
        ( 60, (  0, 200, 100, 255)),   # teal
        ( 80, (  0, 220,   0, 255)),   # green
        (100, (255, 255,   0, 255)),   # yellow
        (120, (255, 180,   0, 255)),   # orange
        (140, (255,  60,   0, 255)),   # red-orange
        (160, (255,   0,   0, 255)),   # red
        (180, (200,   0, 100, 255)),   # magenta
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


# Mapping from pyart field names to our LUT builders
FIELD_CONFIG = {
    "reflectivity":             {"lut_fn": reflectivity_lut,     "label": "Reflectivity",      "unit": "dBZ",  "mask_below": 5.0,    "prefer_long_range": True},
    "velocity":                 {"lut_fn": velocity_lut,         "label": "Velocity",           "unit": "m/s",  "mask_below": None,   "prefer_long_range": False},
    "spectrum_width":           {"lut_fn": spectrum_width_lut,   "label": "Spectrum Width",     "unit": "m/s",  "mask_below": 0.5,    "prefer_long_range": False},
    "differential_reflectivity":{"lut_fn": zdr_lut,              "label": "Diff. Reflectivity", "unit": "dB",   "mask_below": None,   "prefer_long_range": False},
    "cross_correlation_ratio":  {"lut_fn": rhohv_lut,            "label": "Correlation Coeff",  "unit": "",     "mask_below": 0.2,    "prefer_long_range": False},
    "differential_phase":       {"lut_fn": phidp_lut,            "label": "Diff. Phase",        "unit": "deg",  "mask_below": None,   "prefer_long_range": False},
}


# ---------------------------------------------------------------------------
# Polar → Cartesian renderer (numpy + PIL, no matplotlib)
# ---------------------------------------------------------------------------

_lut_cache = {}

def build_polar_lookup(n_az: int, n_gates: int, half_size: int,
                       az_array: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Precompute lookup tables mapping (y, x) → (az_index, gate_index).

    Each pixel maps to the nearest azimuth ray. Gate index is linear.
    Returns (az_idx, gate_idx, valid_mask), each of shape (size, size).
    """
    cache_key = (n_az, n_gates, half_size)
    if cache_key in _lut_cache:
        # Reuse geometry if same shape (saves ~400ms)
        return _lut_cache[cache_key]

    size = 2 * half_size
    # Cartesian coords centered on radar
    y_coords = np.arange(size, dtype=np.float32) - half_size
    x_coords = np.arange(size, dtype=np.float32) - half_size
    xx, yy = np.meshgrid(x_coords, y_coords)

    # Range in pixel units
    r = np.sqrt(xx**2 + yy**2)

    # Azimuth in degrees (meteorological: 0=N, CW)
    az = (np.degrees(np.arctan2(xx, -yy)) + 360) % 360

    # Gate index (linear mapping)
    gate_idx = (r / half_size * n_gates).astype(np.int32)

    # Azimuth index — nearest-neighbor with sorted search
    sort_order = np.argsort(az_array)
    sorted_az = az_array[sort_order]
    az_flat = az.ravel()
    insert_idx = np.searchsorted(sorted_az, az_flat)
    insert_idx = np.clip(insert_idx, 0, len(sorted_az) - 1)
    prev_idx = np.clip(insert_idx - 1, 0, len(sorted_az) - 1)

    d1 = np.abs(sorted_az[insert_idx] - az_flat)
    d2 = np.abs(sorted_az[prev_idx] - az_flat)
    d1 = np.minimum(d1, 360 - d1)
    d2 = np.minimum(d2, 360 - d2)

    best = np.where(d1 <= d2, insert_idx, prev_idx)
    az_idx = sort_order[best].reshape(az.shape)

    # Valid mask: within radar range
    valid_mask = gate_idx < n_gates
    gate_idx[~valid_mask] = 0
    az_idx[~valid_mask] = 0

    result = (az_idx, gate_idx, valid_mask)
    _lut_cache[cache_key] = result
    return result


def render_sweep_to_png(data: np.ndarray,
                        az: np.ndarray,
                        lut: np.ndarray,
                        vmin: float,
                        vmax: float,
                        mask_below: float | None,
                        output_size: int = 1800,
                        cc_data: np.ndarray | None = None,
                        cc_threshold: float = 0.3) -> Image.Image:
    """Render one sweep to a transparent RGBA PIL Image.

    Args:
        data: 2D polar array (n_az, n_gates)
        az: 1D azimuth array in degrees
        lut: (256, 4) RGBA color table
        vmin, vmax: data range for LUT mapping
        mask_below: hide values at or below this threshold
        output_size: pixel diameter of output image
        cc_data: optional correlation coefficient array (same shape as data)
                 used for clutter filtering — gates with CC < cc_threshold
                 are masked out (except for CC product itself)
        cc_threshold: CC value below which to mask (default 0.3)
    """
    n_az, n_gates = data.shape
    half = output_size // 2

    t0 = time.time()
    az_idx, gate_idx, valid_mask = build_polar_lookup(n_az, n_gates, half, az)
    t_lut = time.time() - t0

    t0 = time.time()
    # Sample data at each pixel
    sampled = data[az_idx, gate_idx]

    # Build combined mask
    pixel_mask = valid_mask.copy()

    # Handle masked arrays (pyart returns these)
    if hasattr(data, 'mask'):
        sampled_mask = data.mask[az_idx, gate_idx]
        pixel_mask &= ~sampled_mask

    # Handle NaN
    nan_mask = np.isnan(sampled) if not hasattr(sampled, 'mask') else np.isnan(sampled.data)
    pixel_mask &= ~nan_mask

    # Get raw values as a plain numpy array
    raw_data = np.asarray(sampled.data if hasattr(sampled, 'data') else sampled, dtype=np.float32)

    # CC-based clutter filter (suppress non-meteorological gates)
    if cc_data is not None:
        cc_sampled = cc_data[az_idx, gate_idx]
        cc_raw = np.asarray(cc_sampled.data if hasattr(cc_sampled, 'data') else cc_sampled, dtype=np.float32)
        # Mask where CC is below threshold (likely clutter/noise)
        cc_valid = ~np.isnan(cc_raw)
        if hasattr(cc_data, 'mask'):
            cc_valid &= ~cc_data.mask[az_idx, gate_idx]
        # Only mask where CC exists and is low — don't mask where CC is missing
        clutter_mask = cc_valid & (cc_raw < cc_threshold)
        pixel_mask &= ~clutter_mask

    # Threshold mask
    if mask_below is not None:
        pixel_mask &= raw_data > mask_below

    # Normalize to 0..255 LUT index
    normalized = np.clip((raw_data - vmin) / (vmax - vmin), 0, 1)
    indices = (normalized * 255).astype(np.int32)
    indices = np.clip(indices, 0, 255)

    # Apply LUT
    rgba = lut[indices]  # shape (H, W, 4)

    # Force transparent where masked
    rgba[~pixel_mask] = [0, 0, 0, 0]

    t_render = time.time() - t0
    logger.debug(f"  LUT build: {t_lut*1000:.0f}ms, render: {t_render*1000:.0f}ms")

    return Image.fromarray(rgba, 'RGBA')


# ---------------------------------------------------------------------------
# Smart sweep selection for split-cut VCPs
# ---------------------------------------------------------------------------

def find_best_sweep(radar, field_name: str, target_tilt: float = 0.5,
                    prefer_long_range: bool = True) -> int | None:
    """Find the best sweep index for a given field.

    Level II VCPs use "split cuts" at lower tilts:
    - Surveillance (long range, ~460km): reflectivity only
    - Doppler (short range, ~230km): velocity, spectrum width, + dual-pol

    Args:
        radar: pyart Radar object
        field_name: field to look for
        target_tilt: desired elevation angle
        prefer_long_range: if True, prefer the long-range surveillance cut

    Returns:
        sweep index or None
    """
    fixed_angles = radar.fixed_angle['data']
    sweep_starts = radar.sweep_start_ray_index['data']
    sweep_ends = radar.sweep_end_ray_index['data']

    # Find all sweeps near the target tilt
    candidates = []
    for i, angle in enumerate(fixed_angles):
        if abs(float(angle) - target_tilt) < 0.3:  # within 0.3° of target
            s_start = sweep_starts[i]
            s_end = sweep_ends[i] + 1

            # Check if this sweep has the field
            sweep_data = radar.fields.get(field_name, {}).get('data')
            if sweep_data is None:
                continue

            # Check if data is not all masked in this sweep
            sd = sweep_data[s_start:s_end]
            if hasattr(sd, 'mask'):
                valid_count = int(np.sum(~sd.mask))
            else:
                valid_count = int(np.sum(~np.isnan(sd)))

            n_gates = sd.shape[1] if len(sd.shape) > 1 else 0

            candidates.append({
                'sweep_idx': i,
                'angle': float(angle),
                'n_gates': n_gates,
                'valid_count': valid_count,
            })

    if not candidates:
        # Fall back: find any sweep with this field that has data
        for i in range(radar.nsweeps):
            s_start = sweep_starts[i]
            s_end = sweep_ends[i] + 1
            sweep_data = radar.fields.get(field_name, {}).get('data')
            if sweep_data is None:
                continue
            sd = sweep_data[s_start:s_end]
            if hasattr(sd, 'mask'):
                valid_count = int(np.sum(~sd.mask))
            else:
                valid_count = int(np.sum(~np.isnan(sd)))
            if valid_count > 100:
                return i
        return None

    # Sort: prefer the one with more gates (long range) or fewer (Doppler)
    if prefer_long_range:
        candidates.sort(key=lambda c: (-c['n_gates'], -c['valid_count']))
    else:
        # For Doppler products, prefer the sweep with actual valid data
        # and shorter range (Doppler cut has more velocity data)
        candidates.sort(key=lambda c: (-c['valid_count'], c['n_gates']))

    best = candidates[0]
    if best['valid_count'] < 100:
        # Very little data — might be wrong sweep
        logger.debug(f"  {field_name}: best sweep {best['sweep_idx']} has only {best['valid_count']} valid values")

    return best['sweep_idx']


# ---------------------------------------------------------------------------
# Geo-bounds
# ---------------------------------------------------------------------------

def radar_bounds(site_lat: float, site_lon: float, max_range_km: float) -> dict:
    R = 6371.0
    delta_lat = np.degrees(max_range_km / R)
    delta_lon = delta_lat / np.cos(np.radians(site_lat))
    return {
        "north": round(site_lat + delta_lat, 4),
        "south": round(site_lat - delta_lat, 4),
        "east":  round(site_lon + delta_lon, 4),
        "west":  round(site_lon - delta_lon, 4),
        "center_lat": site_lat,
        "center_lon": site_lon,
        "max_range_km": max_range_km,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def main():
    site = sys.argv[1].upper() if len(sys.argv) > 1 else "KTLX"
    output_dir = Path(f"data/test_level2_renders/{site}")
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Download
    logger.info(f"=== Level II Test Render: {site} ===")
    raw_path = download_latest_level2(site)
    if not raw_path:
        sys.exit(1)

    # 2. Read with PyART
    logger.info(f"Reading {raw_path.name} with PyART...")
    t0 = time.time()
    import pyart
    radar = pyart.io.read_nexrad_archive(str(raw_path))
    logger.info(f"Read in {time.time()-t0:.1f}s")

    site_lat = round(float(radar.latitude['data'][0]), 4)
    site_lon = round(float(radar.longitude['data'][0]), 4)
    fixed_angles = np.array(radar.fixed_angle['data'], dtype=float)
    sweep_starts = radar.sweep_start_ray_index['data']
    sweep_ends = radar.sweep_end_ray_index['data']

    logger.info(f"  Sweeps: {radar.nsweeps}")
    logger.info(f"  Fields: {list(radar.fields.keys())}")
    logger.info(f"  Rays: {radar.nrays}, Gates: {radar.ngates}")
    logger.info(f"  Site: {site_lat}N, {site_lon}E")
    logger.info(f"  Tilts: {[f'{a:.1f}°' for a in fixed_angles]}")

    # 3. Pre-extract CC data for clutter filtering on other products
    #    CC-based filter: mask gates where CC < threshold (non-met scatterers)
    cc_by_sweep = {}
    if 'cross_correlation_ratio' in radar.fields:
        for i in range(radar.nsweeps):
            s0 = sweep_starts[i]
            s1 = sweep_ends[i] + 1
            cc_sweep = radar.fields['cross_correlation_ratio']['data'][s0:s1]
            cc_by_sweep[i] = cc_sweep

    # 4. Render each field at the lowest appropriate tilt
    results = {}
    total_t0 = time.time()

    for field_name, cfg in FIELD_CONFIG.items():
        if field_name not in radar.fields:
            logger.warning(f"  Field '{field_name}' not in volume — skipping")
            continue

        # Find the best sweep for this field
        sweep_idx = find_best_sweep(
            radar, field_name,
            target_tilt=0.5,
            prefer_long_range=cfg.get("prefer_long_range", False)
        )
        if sweep_idx is None:
            logger.warning(f"  No valid sweep found for {field_name}")
            continue

        s_start = sweep_starts[sweep_idx]
        s_end = sweep_ends[sweep_idx] + 1
        tilt_angle = fixed_angles[sweep_idx]

        sweep_data = radar.fields[field_name]['data'][s_start:s_end]
        sweep_az = radar.azimuth['data'][s_start:s_end]
        max_range_m = radar.range['data'][-1]
        max_range_km = max_range_m / 1000.0

        n_az, n_gates = sweep_data.shape

        # Count valid data
        if hasattr(sweep_data, 'mask'):
            valid_pct = 100 * np.sum(~sweep_data.mask) / sweep_data.size
        else:
            valid_pct = 100 * np.sum(~np.isnan(sweep_data)) / sweep_data.size

        logger.info(f"  {cfg['label']}: sweep {sweep_idx}, {n_az} rays × {n_gates} gates, "
                     f"tilt={tilt_angle:.1f}°, range={max_range_km:.0f}km, "
                     f"{valid_pct:.0f}% valid")

        # Convert masked array
        if hasattr(sweep_data, 'filled'):
            data_np = sweep_data.filled(np.nan)
        else:
            data_np = np.array(sweep_data, dtype=np.float32)

        lut, vmin, vmax = cfg["lut_fn"]()

        # Get CC data for clutter filtering (skip for CC product itself)
        cc_for_filter = None
        if field_name != 'cross_correlation_ratio' and sweep_idx in cc_by_sweep:
            cc_sweep = cc_by_sweep[sweep_idx]
            # Ensure shapes match (same sweep = same shape)
            if cc_sweep.shape == sweep_data.shape:
                cc_for_filter = cc_sweep

        # Render
        t0 = time.time()
        img = render_sweep_to_png(
            data=data_np,
            az=np.array(sweep_az, dtype=np.float32),
            lut=lut,
            vmin=vmin,
            vmax=vmax,
            mask_below=cfg["mask_below"],
            output_size=1800,
            cc_data=cc_for_filter,
            cc_threshold=0.3,
        )
        render_ms = (time.time() - t0) * 1000

        fname = f"{field_name}_tilt{tilt_angle:.1f}.png"
        img.save(output_dir / fname, optimize=True)
        file_size = (output_dir / fname).stat().st_size

        logger.info(f"    → {fname}: {file_size/1024:.0f} KB, {render_ms:.0f}ms")

        results[field_name] = {
            "file": fname,
            "size_kb": round(file_size / 1024, 1),
            "render_ms": round(render_ms, 1),
            "tilt": round(float(tilt_angle), 1),
            "sweep_idx": int(sweep_idx),
            "rays": n_az,
            "gates": n_gates,
            "max_range_km": round(max_range_km, 1),
            "valid_pct": round(valid_pct, 1),
        }

    total_ms = (time.time() - total_t0) * 1000
    logger.info(f"\n  Total render time: {total_ms:.0f}ms for {len(results)} products")

    # Bounds
    max_rng = max(r["max_range_km"] for r in results.values()) if results else 230
    bounds = radar_bounds(site_lat, site_lon, max_rng)

    meta = {
        "site": site,
        "lat": site_lat,
        "lon": site_lon,
        "file": raw_path.name,
        "sweeps": int(radar.nsweeps),
        "tilts": [round(float(a), 1) for a in fixed_angles],
        "bounds": bounds,
        "products": results,
        "total_render_ms": round(total_ms, 1),
    }
    meta_path = output_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2, cls=NumpyEncoder))
    logger.info(f"\nMetadata: {meta_path}")
    logger.info(f"Output:   {output_dir}/")

    print(f"\n{'Product':<30} {'Sweep':>5} {'Size':>8} {'Render':>8} {'Rays':>6} {'Gates':>6} {'Valid%':>7}")
    print("-" * 78)
    for name, r in results.items():
        print(f"{FIELD_CONFIG[name]['label']:<30} {r['sweep_idx']:>5} {r['size_kb']:>6.0f}KB "
              f"{r['render_ms']:>6.0f}ms {r['rays']:>6} {r['gates']:>6} {r['valid_pct']:>6.1f}%")


if __name__ == "__main__":
    main()
