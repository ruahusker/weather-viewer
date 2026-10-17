"""Satellite imagery renderer - converts GOES ABI data to map overlays.

Renders GOES satellite imagery as transparent PNG overlays that can be
displayed on the Leaflet map, similar to weather model overlays.
"""

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Optional

import cartopy.crs as ccrs
import cartopy.io.shapereader as shpreader
import matplotlib
matplotlib.use('Agg')
import matplotlib.path as mpath
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from matplotlib.colors import LinearSegmentedColormap
from pyproj import Transformer
from PIL import Image, ImageFilter
from scipy.ndimage import distance_transform_edt, zoom
from scipy.spatial import cKDTree

from .goes_downloader import GOESDownloader, GOESFile

# ---------------------------------------------------------------------------
# Module-level caches — persist across render calls
# ---------------------------------------------------------------------------
# (sat_lon_r, sat_height_i, sweep) -> pyproj.Transformer
_transformer_cache: dict = {}
# (data.shape, sat_lon_r, target_w, target_h) -> (flat_indices np.int32, valid_mask bool)
_regrid_index_cache: dict = {}
_REGRID_LOCK = threading.RLock()


def _serialize_regrid(function):
    """Avoid concurrent cold geometry builds and eviction during regridding."""
    @wraps(function)
    def locked(*args, **kwargs):
        with _REGRID_LOCK:
            return function(*args, **kwargs)
    return locked
# (target_w, target_h) -> bool ndarray (True = inside USA)
_sat_usa_mask_cache: dict = {}

_TRANSFORMER_CACHE_MAX = 10
# Must comfortably exceed the number of live (product shape x region) combos,
# otherwise FIFO eviction forces multi-second KDTree rebuilds every cycle.
_REGRID_INDEX_CACHE_MAX = max(16, int(os.getenv("SATELLITE_REGRID_CACHE_MAX", "48")))
_USA_MASK_CACHE_MAX = 5
_EDGE_RAMP_CACHE_MAX = 8

# Regrid indices are also persisted here so a cache miss (eviction, memory
# trim, restart) costs a ~10ms disk load instead of a 2-3s KDTree rebuild.
_REGRID_DISK_CACHE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "data" / "regrid_cache" / "satellite"
)


def _prune_cache(mapping: dict, max_entries: int) -> None:
    while len(mapping) > max_entries:
        try:
            mapping.pop(next(iter(mapping)))
        except StopIteration:
            break


def _regrid_disk_path(regrid_key: tuple) -> Path:
    digest = hashlib.sha1(repr(regrid_key).encode()).hexdigest()[:20]
    return _REGRID_DISK_CACHE_DIR / f"{digest}.npz"


def _regrid_cache_get(regrid_key: tuple):
    """Look up regrid indices: memory first, then the on-disk spill cache."""
    cached = _regrid_index_cache.get(regrid_key)
    if cached is not None:
        return cached
    path = _regrid_disk_path(regrid_key)
    try:
        if path.exists():
            with np.load(path) as npz:
                cached = (npz["flat_indices"], npz["valid_matches"])
            _regrid_index_cache[regrid_key] = cached
            _prune_cache(_regrid_index_cache, _REGRID_INDEX_CACHE_MAX)
            return cached
    except Exception:
        path.unlink(missing_ok=True)
    return None


def _regrid_cache_put(regrid_key: tuple, flat_indices, valid_matches) -> None:
    _regrid_index_cache[regrid_key] = (flat_indices, valid_matches)
    _prune_cache(_regrid_index_cache, _REGRID_INDEX_CACHE_MAX)
    path = _regrid_disk_path(regrid_key)
    try:
        _REGRID_DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.stem}.{os.getpid()}.{threading.get_ident()}.npz")
        np.savez(tmp, flat_indices=flat_indices, valid_matches=valid_matches)
        tmp.replace(path)
    except Exception as exc:
        logger.debug("Regrid disk cache write failed: %s", exc)


def clear_satellite_caches(include_regrid: bool = False):
    """Release satellite renderer caches to free memory.

    The regrid index cache is kept by default: it is small relative to the
    image buffers that actually drive RSS, and losing it forces expensive
    KDTree rebuilds (entries are disk-backed, but routine memory trims should
    not flush hot entries).
    """
    _transformer_cache.clear()
    _sat_usa_mask_cache.clear()
    _edge_ramp_cache.clear()
    if include_regrid:
        _regrid_index_cache.clear()


# Optional observer wired up by the scheduler's FrameIndex so every frame
# meta write keeps the in-memory index (and manifests) current.
_frame_observer = None


def set_frame_observer(callback) -> None:
    global _frame_observer
    _frame_observer = callback


def write_frame_meta(meta_path: Path, meta: dict) -> None:
    """Write a frame meta JSON and notify the frame index, if registered."""
    meta_path.write_text(json.dumps(meta, indent=2))
    if _frame_observer is not None:
        try:
            _frame_observer(meta_path, meta)
        except Exception:
            logger.debug("Frame observer failed for %s", meta_path.name, exc_info=True)

logger = logging.getLogger(__name__)

# USA boundary constants
USA_WEST = -125.0
USA_EAST = -66.0
USA_SOUTH = 23.0
USA_NORTH = 50.0

# Domain bounds - cropped slightly on the north edge so composited
# full-disk products do not expose a bare Leaflet strip above the imagery.
DATA_SOUTH, DATA_NORTH = 15.0, 52.0
DATA_WEST, DATA_EAST = -145.0, -50.0

# Mercator conversion (same as model renderer)
def _lat_to_merc(lat):
    lat_rad = np.radians(np.clip(lat, -85, 85))
    return np.degrees(np.log(np.tan(np.pi / 4 + lat_rad / 2)))

MERC_SOUTH = float(_lat_to_merc(DATA_SOUTH))
MERC_NORTH = float(_lat_to_merc(DATA_NORTH))

# Geographic edge ramp settings — latitude/longitude limits for clean masking.
# Data fades to transparent between the *_fade and *_clip values.
_DEFAULT_EDGE_RAMP = {
    "north_fade": 49.5,
    "north_clip": 52.0,
    "south_fade": 17.0,
    "south_clip": 15.0,
    "west_fade": -142.0,
    "west_clip": -145.0,
    "east_fade": -53.0,
    "east_clip": -50.0,
}

# Cache: (target_w, target_h) -> float32 array (0..1)
_edge_ramp_cache: dict = {}

def _build_edge_ramp(
    target_w: int,
    target_h: int,
    ramp_cfg: dict | None = None,
    bounds: dict | None = None,
) -> np.ndarray:
    """Build a 2D alpha ramp (0-1) that fades data at geographic edges.

    Uses smoothstep for a natural dissolve.  The ramp is computed in
    lat/lon space and cached by output dimensions.
    """
    cfg = {**_DEFAULT_EDGE_RAMP, **(ramp_cfg or {})}
    ramp_bounds = bounds or {
        "west": DATA_WEST,
        "east": DATA_EAST,
        "south": DATA_SOUTH,
        "north": DATA_NORTH,
    }
    merc_south = float(_lat_to_merc(ramp_bounds["south"]))
    merc_north = float(_lat_to_merc(ramp_bounds["north"]))
    key = (
        target_w,
        target_h,
        ramp_bounds["west"], ramp_bounds["east"],
        ramp_bounds["south"], ramp_bounds["north"],
        cfg["north_fade"], cfg["north_clip"],
        cfg["south_fade"], cfg["south_clip"],
        cfg["west_fade"], cfg["west_clip"],
        cfg["east_fade"], cfg["east_clip"],
    )
    if key in _edge_ramp_cache:
        return _edge_ramp_cache[key]

    def _smoothstep(t):
        t = np.clip(t, 0, 1)
        return t * t * (3.0 - 2.0 * t)

    # Latitude array (Mercator → geographic)
    merc_y = np.linspace(merc_north, merc_south, target_h)
    lat = np.degrees(2 * np.arctan(np.exp(np.radians(merc_y))) - np.pi / 2)
    # Longitude array
    lon = np.linspace(ramp_bounds["west"], ramp_bounds["east"], target_w)

    # North ramp: 1 at _FADE, 0 at _CLIP (lat increasing northward)
    north_t = (lat - cfg["north_fade"]) / (cfg["north_clip"] - cfg["north_fade"])
    north_ramp = 1.0 - _smoothstep(north_t)  # 1 below fade, 0 above clip

    # South ramp: 1 at _FADE, 0 at _CLIP (lat decreasing southward)
    south_t = (cfg["south_fade"] - lat) / (cfg["south_fade"] - cfg["south_clip"])
    south_ramp = 1.0 - _smoothstep(south_t)

    # Combine N/S into a column vector
    lat_ramp = (north_ramp * south_ramp).astype(np.float32)  # (H,)

    # West ramp
    west_t = (cfg["west_fade"] - lon) / (cfg["west_fade"] - cfg["west_clip"])
    west_ramp = 1.0 - _smoothstep(west_t)

    # East ramp
    east_t = (lon - cfg["east_fade"]) / (cfg["east_clip"] - cfg["east_fade"])
    east_ramp = 1.0 - _smoothstep(east_t)

    lon_ramp = (west_ramp * east_ramp).astype(np.float32)  # (W,)

    # Outer product → 2D ramp
    ramp_2d = lat_ramp[:, np.newaxis] * lon_ramp[np.newaxis, :]

    _edge_ramp_cache[key] = ramp_2d
    _prune_cache(_edge_ramp_cache, _EDGE_RAMP_CACHE_MAX)
    return ramp_2d


def _merc_to_lat(merc_y: float) -> float:
    """Convert Web Mercator Y back to latitude in degrees."""
    return float(np.degrees(2 * np.arctan(np.exp(np.radians(merc_y))) - np.pi / 2))


def _build_usa_path():
    """Load USA border as a matplotlib Path for point-in-polygon testing."""
    try:
        shpfile = shpreader.natural_earth("50m", "cultural", "admin_0_countries")
        reader = shpreader.Reader(shpfile)
        for rec in reader.records():
            if rec.attributes.get("ADMIN") == "United States of America":
                usa_geom = rec.geometry
                all_verts = []
                all_codes = []
                polys = list(usa_geom.geoms) if hasattr(usa_geom, "geoms") else [usa_geom]
                for poly in polys:
                    coords = list(poly.exterior.coords)
                    all_verts.extend(coords)
                    all_codes.extend(
                        [mpath.Path.MOVETO]
                        + [mpath.Path.LINETO] * (len(coords) - 2)
                        + [mpath.Path.CLOSEPOLY])
                logger.info("Loaded USA border: %d polygons", len(polys))
                return mpath.Path(all_verts, all_codes)
    except Exception as exc:
        logger.warning("Failed to load USA border: %s", exc)
    return None

# Load once at module level
_USA_PATH = _build_usa_path()

logger = logging.getLogger(__name__)


# GOES ABI Band definitions
ABI_BANDS = {
    1: {"name": "Blue", "wavelength": 0.47, "type": "visible"},
    2: {"name": "Red", "wavelength": 0.64, "type": "visible"},
    3: {"name": "Veggie", "wavelength": 0.86, "type": "near_ir"},
    4: {"name": "Cirrus", "wavelength": 1.37, "type": "near_ir"},
    5: {"name": "Snow/Ice", "wavelength": 1.6, "type": "near_ir"},
    6: {"name": "Cloud Particle", "wavelength": 2.2, "type": "near_ir"},
    7: {"name": "Shortwave IR", "wavelength": 3.9, "type": "infrared"},
    8: {"name": "Upper-level Water Vapor", "wavelength": 6.2, "type": "water_vapor"},
    9: {"name": "Mid-level Water Vapor", "wavelength": 6.9, "type": "water_vapor"},
    10: {"name": "Low-level Water Vapor", "wavelength": 7.3, "type": "water_vapor"},
    11: {"name": "Cloud Top", "wavelength": 8.4, "type": "infrared"},
    12: {"name": "Ozone", "wavelength": 9.6, "type": "infrared"},
    13: {"name": "Clean IR", "wavelength": 10.3, "type": "infrared"},
    14: {"name": "Longwave IR", "wavelength": 11.2, "type": "infrared"},
    15: {"name": "Dirty IR", "wavelength": 12.3, "type": "infrared"},
    16: {"name": "CO2", "wavelength": 13.3, "type": "infrared"},
}

# Unified MCMIP product configuration
MCMIP_PRODUCT_CONFIG = {
    "vis_blue": {"band": 1,  "type": "refl", "cmap": "gray",           "vmin": 0,    "vmax": 60, "gamma": 0.5, "tone_lift": 0.08},
    "ir":       {"band": 13, "type": "bt",   "cmap": "wx_enhanced_ir", "vmin": -110, "vmax": 50, "gamma": 1.0},
    "ir_native":{"band": 13, "type": "bt",   "cmap": "wx_enhanced_ir", "vmin": -110, "vmax": 50, "gamma": 1.0},
    "ir_14":    {"band": 14, "type": "bt",   "cmap": "wx_enhanced_ir", "vmin": -110, "vmax": 50, "gamma": 1.0},
    "cloud_top":{"band": 11, "type": "bt",   "cmap": "wx_enhanced_ir", "vmin": -110, "vmax": 50, "gamma": 1.0},
    "ozone":    {"band": 12, "type": "bt",   "cmap": "Greys",          "vmin": -110, "vmax": 50, "gamma": 1.0},
    "ir_dirty": {"band": 15, "type": "bt",   "cmap": "wx_enhanced_ir", "vmin": -110, "vmax": 50, "gamma": 1.0},
    "co2":      {"band": 16, "type": "bt",   "cmap": "wx_enhanced_ir", "vmin": -110, "vmax": 50, "gamma": 1.0},
    "wv":       {"band": 9,  "type": "bt",   "cmap": "wx_wv",          "vmin": -80,  "vmax": -20, "gamma": 1.0},
    "vis":      {"band": 2,  "type": "refl", "cmap": "gray",           "vmin": 0,    "vmax": 60, "gamma": 0.5, "tone_lift": 0.08},
    "vis_1km":  {"band": 2,  "type": "refl", "cmap": "gray",           "vmin": 0,    "vmax": 75, "gamma": 0.65, "tone_lift": 0.12},
    "cloud_particle": {"band": 6, "type": "refl", "cmap": "gray",      "vmin": 0,    "vmax": 75, "gamma": 0.5, "tone_lift": 0.08},
    "sw_ir":    {
        "band": 7, "type": "bt", "cmap": "wx_enhanced_ir", "vmin": -50, "vmax": 80, "gamma": 1.0,
        "edge_ramp": {
            "north_fade": 51.2, "north_clip": 52.0,
            "south_fade": 15.6, "south_clip": 15.0,
            "west_fade": -144.0, "west_clip": -145.0,
            "east_fade": -51.0, "east_clip": -50.0,
            "visible_crop_threshold": 0.18,
        },
    },
    "upper_wv": {"band": 8,  "type": "bt",   "cmap": "wx_wv",          "vmin": -80,  "vmax": -20, "gamma": 1.0},
    "lower_wv": {"band": 10, "type": "bt",   "cmap": "wx_wv",          "vmin": -60,  "vmax": 20, "gamma": 1.0},
    "veggie":   {"band": 3,  "type": "refl", "cmap": "gray",           "vmin": 0,    "vmax": 75, "gamma": 0.5, "tone_lift": 0.08},
    "snow_ice": {"band": 5,  "type": "refl", "cmap": "gray",           "vmin": 0,    "vmax": 75, "gamma": 0.5, "tone_lift": 0.08},
    "cirrus":   {"band": 4,  "type": "refl", "cmap": "gray",           "vmin": 0,    "vmax": 75, "gamma": 0.5, "tone_lift": 0.08},
}

# L2 product configuration
L2_PRODUCT_CONFIG = {
    "fire": {
        "var": "Temp",
        "cmap": "hot",
        "vmin": 300,
        "vmax": 600,
        "celsius": False,
        "extend_masked": False,
    },
    "pwat": {
        "var": "TPW",
        "cmap": "Blues",
        "vmin": 0,
        "vmax": 70,
        "celsius": False,
        "extend_masked": True,
    },
    "sst": {
        "var": "SST",
        "cmap": "RdYlBu_r",
        "vmin": -2,
        "vmax": 35,
        "celsius": True,
        "max_dqf": 1,
        "extend_masked": True,
    },
    "rain_rate": {
        "var": "RRQPE",
        "cmap": "YlGnBu",
        "vmin": 0,
        "vmax": 50,
        "celsius": False,
        "max_dqf": 2,
        "extend_masked": True,
    },
    "stability": {
        "var": "LI",
        "cmap": "RdBu",
        "vmin": -10,
        "vmax": 10,
        "celsius": False,
        "extend_masked": True,
    },
}

# Color scales for different products
SATELLITE_COLOR_SCALES = {
    "ir": {
        "cmap": "Greys",
        "vmin": -110,        # Cold cloud tops (°C)
        "vmax": 50,          # Warm ground (°C)
        "label": "Brightness Temperature (°C)",
    },
    "wv": {
        "cmap": "Greys",
        "vmin": -80,
        "vmax": 50,
        "label": "Water Vapor BT (°C)",
    },
    "vis": {
        "vmin": 0,
        "vmax": 100,
        "label": "Reflectance (%)",
    },
}


@dataclass
class RenderedSatellite:
    """Result of rendering a satellite image."""
    png_path: Path
    bounds: dict  # {north, south, east, west}
    timestamp: str
    satellite: str
    product: str
    band: Optional[int] = None


class SatelliteRenderer:
    """Render GOES satellite data as map overlays."""
    
    def __init__(self, output_dir: Path, dpi: int = 150):
        """Initialize renderer.
        
        Args:
            output_dir: Directory to save rendered PNGs
            dpi: Resolution for output images
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.dpi = dpi
        
        # Create subdirectories
        self.png_dir = self.output_dir / "png"
        self.meta_dir = self.output_dir / "meta"
        self.png_dir.mkdir(exist_ok=True)
        self.meta_dir.mkdir(exist_ok=True)
        
        logger.info(f"SatelliteRenderer initialized: output={output_dir}")

    def _extend_outside_domain_rgba(
        self,
        rgba: np.ndarray,
        outside_mask: np.ndarray,
        fade_start: float = 0.08,
        fade_end: float = 0.70,
        apply_alpha_fade: bool = True,
    ) -> np.ndarray:
        """Extend data into void pixels with diffused fill and smooth alpha fade.

        Replaces raw nearest-neighbor stretching with a polished approach:
        1. NN fill for base colors
        2. Iterative blur diffusion — blur+replace void pixels multiple passes
           to completely eliminate NN streak artifacts
        3. Alpha gradient fade at the boundary edge
        """
        if not np.any(outside_mask) or np.all(outside_mask):
            return rgba

        h, w = rgba.shape[:2]

        # Step 1: Fill voids with nearest-neighbor colors
        distances, nearest_idx = distance_transform_edt(
            outside_mask, return_indices=True)
        nearest_y = nearest_idx[0][outside_mask]
        nearest_x = nearest_idx[1][outside_mask]
        rgba[outside_mask, :3] = rgba[nearest_y, nearest_x, :3]
        rgba[outside_mask, 3] = 255

        max_dist = float(distances[outside_mask].max())
        if max_dist < 1:
            return rgba

        # Step 2: Iterative blur diffusion on void region only.
        # Each pass blurs the full image but only writes back to void pixels,
        # keeping valid data sharp while progressively smoothing NN streaks.
        blur_radius = max(16, int(max_dist * 0.12))
        n_passes = 3
        rgb = rgba[:, :, :3].copy()

        for _ in range(n_passes):
            blurred = np.array(
                Image.fromarray(rgb, 'RGB').filter(
                    ImageFilter.GaussianBlur(radius=blur_radius)))
            rgb[outside_mask] = blurred[outside_mask]

        rgba[:, :, :3] = rgb

        if not apply_alpha_fade:
            return rgba

        # Step 3: Alpha fade — opaque near data, fading to transparent at edges.
        # Quadratic ease-out for a soft, natural falloff instead of linear.
        fade_start_px = max_dist * fade_start
        fade_end_px = max_dist * fade_end
        fade_range = max(fade_end_px - fade_start_px, 1.0)

        t = np.clip(
            (distances[outside_mask] - fade_start_px) / fade_range,
            0, 1)
        void_alpha = (1.0 - t * t) * 255.0

        alpha = rgba[:, :, 3].astype(np.float32)
        alpha[outside_mask] = void_alpha
        rgba[:, :, 3] = alpha.astype(np.uint8)

        return rgba

    def _display_range(
        self,
        data: np.ndarray,
        vmin: float | None,
        vmax: float | None,
        cmap: str | object,
    ) -> tuple[float, float]:
        """Tighten grayscale BT ranges so IR/WV imagery has stronger contrast."""
        if vmin is None:
            vmin = float(np.nanmin(data))
        if vmax is None:
            vmax = float(np.nanmax(data))

        cmap_name = cmap if isinstance(cmap, str) else ""
        if cmap_name == "Greys":
            finite = data[np.isfinite(data)]
            if finite.size >= 100:
                p_lo, p_hi = np.nanpercentile(finite, [1.5, 98.5])
                vmin = max(float(vmin), float(p_lo))
                vmax = min(float(vmax), float(p_hi))
                if vmax <= vmin:
                    vmin = float(np.nanmin(finite))
                    vmax = float(np.nanmax(finite))
        return float(vmin), float(vmax)
    
    def _get_bounds(self, dataset: xr.Dataset) -> dict:
        """Extract geographic bounds from GOES dataset.
        
        GOES uses a geostationary projection. We need to convert
        the projection coordinates to lat/lon.
        """
        try:
            # Try to get from dataset attributes
            if 'geospatial_lat_lon_extent' in dataset.attrs:
                # Parse from attribute string
                attr = dataset.attrs['geospatial_lat_lon_extent']
                # Format: "geospatial_westbound_longitude=-135.0 ..."
                bounds = {}
                for key, val in re.findall(r'(\w+)=([-\d.]+)', attr):
                    bounds[key] = float(val)
                return {
                    'west': bounds.get('geospatial_westbound_longitude', -180),
                    'east': bounds.get('geospatial_eastbound_longitude', 180),
                    'south': bounds.get('geospatial_southbound_latitude', -90),
                    'north': bounds.get('geospatial_northbound_latitude', 90),
                }
            
            # Fallback: use standard GOES domains
            return self._get_standard_bounds(dataset)
            
        except Exception as e:
            logger.warning(f"Could not extract bounds: {e}, using defaults")
            return {'west': -135, 'east': -60, 'south': 20, 'north': 55}
    
    def _get_standard_bounds(self, dataset: xr.Dataset) -> dict:
        """Get standard bounds based on GOES domain."""
        # Check for domain indicator in filename or attributes
        try:
            scene_id = dataset.attrs.get('scene_id', 'FULL_DISK')
            
            if 'CONUS' in scene_id or 'C' in str(scene_id):
                # CONUS domain
                return {'west': -135, 'east': -65, 'south': 22, 'north': 50}
            elif 'MESOSCALE' in scene_id:
                # Mesoscale - approximate
                return {'west': -105, 'east': -85, 'south': 30, 'north': 45}
            else:
                # Full disk - limit to visible area
                return {'west': -160, 'east': -30, 'south': -60, 'north': 60}
        except:
            return {'west': -135, 'east': -60, 'south': 20, 'north': 55}
    
    def render_ir_band(
        self,
        dataset: xr.Dataset,
        band: int = 13,
        filename: str = None
    ) -> RenderedSatellite:
        """Render an infrared band as grayscale overlay with Web Mercator reprojection.
        
        Args:
            dataset: GOES ABI dataset
            band: Band number (default 13 = Clean IR)
            filename: Output filename (optional)
        
        Returns:
            RenderedSatellite with paths and metadata
        """
        try:
            # Get IR brightness temperature
            bt = self._get_brightness_temp(dataset, band)
            if bt is None:
                raise ValueError(f"Could not extract band {band} data")
            
            # Convert to Celsius
            bt_c = bt - 273.15
            
            # Generate filename
            if filename is None:
                timestamp = self._get_timestamp(dataset)
                filename = f"goes_ir_band{band}_{timestamp}.png"
            
            png_path = self.png_dir / filename
            
            # Render with Web Mercator reprojection
            bounds = self.render_to_mercator(
                bt_c.values,
                dataset,
                png_path,
                cmap='Greys_r',
                vmin=-110,
                vmax=50
            )
            
            # Save metadata
            meta = {
                'satellite': dataset.attrs.get('platform_ID', 'GOES'),
                'product': f'IR Band {band}',
                'band': band,
                'timestamp': self._get_timestamp(dataset),
                'bounds': bounds,
                'png_path': str(png_path.relative_to(self.output_dir.parent)),
            }
            
            meta_path = self.meta_dir / filename.replace('.png', '.json')
            write_frame_meta(meta_path, meta)
            
            return RenderedSatellite(
                png_path=png_path,
                bounds=bounds,
                timestamp=meta['timestamp'],
                satellite=meta['satellite'],
                product=meta['product'],
                band=band
            )
            
        except Exception as e:
            logger.error(f"Failed to render IR band {band}: {e}")
            raise
    
    def render_wv_band(
        self,
        dataset: xr.Dataset,
        band: int = 9,
        filename: str = None
    ) -> RenderedSatellite:
        """Render a water vapor band as grayscale overlay with Web Mercator reprojection.
        
        Args:
            dataset: GOES ABI dataset
            band: Band number (default 9 = Mid-level WV)
            filename: Output filename (optional)
        
        Returns:
            RenderedSatellite with paths and metadata
        """
        try:
            # Get WV brightness temperature
            bt = self._get_brightness_temp(dataset, band)
            if bt is None:
                raise ValueError(f"Could not extract band {band} data")
            
            # Convert to Celsius
            bt_c = bt - 273.15
            
            # Generate filename
            if filename is None:
                timestamp = self._get_timestamp(dataset)
                filename = f"goes_wv_band{band}_{timestamp}.png"
            
            png_path = self.png_dir / filename
            
            # Render with Web Mercator reprojection
            bounds = self.render_to_mercator(
                bt_c.values,
                dataset,
                png_path,
                cmap='Greys_r',
                vmin=-80,
                vmax=-20
            )
            
            # Save metadata
            meta = {
                'satellite': dataset.attrs.get('platform_ID', 'GOES'),
                'product': f'WV Band {band}',
                'band': band,
                'timestamp': self._get_timestamp(dataset),
                'bounds': bounds,
                'png_path': str(png_path.relative_to(self.output_dir.parent)),
            }
            
            meta_path = self.meta_dir / filename.replace('.png', '.json')
            write_frame_meta(meta_path, meta)
            
            return RenderedSatellite(
                png_path=png_path,
                bounds=bounds,
                timestamp=meta['timestamp'],
                satellite=meta['satellite'],
                product=meta['product'],
                band=band
            )
            
        except Exception as e:
            logger.error(f"Failed to render WV band {band}: {e}")
            raise
    
    def render_vis_band(
        self,
        dataset: xr.Dataset,
        band: int = 2,
        filename: str = None
    ) -> RenderedSatellite:
        """Render a visible band as grayscale overlay with Web Mercator reprojection.
        
        Args:
            dataset: GOES ABI dataset
            band: Band number (default 2 = Red visible)
            filename: Output filename (optional)
        
        Returns:
            RenderedSatellite with paths and metadata
        """
        try:
            # Get visible reflectance (0-1 range, not temperature!)
            refl = self._get_brightness_temp(dataset, band)
            if refl is None:
                raise ValueError(f"Could not extract band {band} data")
            
            # Convert to percentage (0-100)
            refl_pct = refl * 100
            
            # Generate filename
            if filename is None:
                timestamp = self._get_timestamp(dataset)
                filename = f"goes_vis_band{band}_{timestamp}.png"
            
            png_path = self.png_dir / filename
            
            # Render with Web Mercator reprojection
            # Use grayscale colormap with 0-100% range
            bounds = self.render_to_mercator(
                refl_pct.values,
                dataset,
                png_path,
                cmap='Greys_r',  # Reversed grayscale: dark=low reflectance, bright=high
                vmin=0,
                vmax=100
            )
            
            # Save metadata
            meta = {
                'satellite': dataset.attrs.get('platform_ID', 'GOES'),
                'product': f'VIS Band {band}',
                'band': band,
                'timestamp': self._get_timestamp(dataset),
                'bounds': bounds,
                'png_path': str(png_path.relative_to(self.output_dir.parent)),
            }
            
            meta_path = self.meta_dir / filename.replace('.png', '.json')
            write_frame_meta(meta_path, meta)
            
            return RenderedSatellite(
                png_path=png_path,
                bounds=bounds,
                timestamp=meta['timestamp'],
                satellite=meta['satellite'],
                product=meta['product'],
                band=band
            )
            
        except Exception as e:
            logger.error(f"Failed to render VIS band {band}: {e}")
            raise
    
    def _get_brightness_temp(self, dataset: xr.Dataset, band: int) -> Optional[xr.DataArray]:
        """Extract brightness temperature for IR band."""
        try:
            # Try common variable names
            var_names = [
                f'CMI_C{band:02d}',  # Multi-channel
                f'Rad{band}',         # Single channel
                f'C{band:02d}',       # Short form
            ]
            
            for var in var_names:
                if var in dataset:
                    return dataset[var]
            
            # Fallback: look for any variable with band number
            for var in dataset.data_vars:
                if str(band) in var and 'CMI' in var:
                    return dataset[var]
            
            return None
        except Exception as e:
            logger.error(f"Error extracting band {band}: {e}")
            return None
    
    def _get_band_data(self, dataset: xr.Dataset, band: int) -> Optional[xr.DataArray]:
        """Get reflectance/radiance data for a band."""
        return self._get_brightness_temp(dataset, band)
    
    def _get_timestamp(self, dataset: xr.Dataset) -> str:
        """Extract timestamp from dataset."""
        try:
            # Try various attribute names
            for attr in ['time_coverage_start', 'start_date_time', 'date_created']:
                if attr in dataset.attrs:
                    return dataset.attrs[attr].replace(':', '-')
            
            return datetime.now().strftime('%Y%m%d_%H%M%S')
        except:
            return datetime.now().strftime('%Y%m%d_%H%M%S')
    
    def _normalize(self, data: np.ndarray, vmin: float = None, vmax: float = None) -> np.ndarray:
        """Normalize data to 0-1 range."""
        if vmin is None:
            vmin = np.nanpercentile(data, 1)
        if vmax is None:
            vmax = np.nanpercentile(data, 99)
        
        normalized = (data - vmin) / (vmax - vmin)
        return np.clip(normalized, 0, 1)
    
    @_serialize_regrid
    def render_to_mercator(
        self,
        data: np.ndarray,
        dataset: xr.Dataset,
        output_path: Path,
        cmap: str = 'Greys_r',
        vmin: float = None,
        vmax: float = None,
        gamma: float = 1.0,
        tone_lift: float = 0.0,
        edge_ramp_cfg: dict | None = None,
        crop_bounds: dict | None = None,
    ):
        """Render GOES data to Web Mercator projection for proper map alignment.

        GOES uses a geostationary projection; Leaflet uses Web Mercator (EPSG:3857).
        Reprojection geometry (KDTree query results) is cached per (data.shape,
        sat_lon, target_w, target_h) so the expensive build only happens once per
        satellite/product/domain combination.
        """
        # Get projection info from dataset
        proj_attrs = dataset['goes_imager_projection'].attrs
        sat_lon = proj_attrs.get('longitude_of_projection_origin', -137.0)
        sat_height = proj_attrs.get('perspective_point_height', 35786023.0)
        sweep = proj_attrs.get('sweep_angle_axis', 'x')

        base_bounds = {
            "west": DATA_WEST,
            "east": DATA_EAST,
            "south": DATA_SOUTH,
            "north": DATA_NORTH,
        }
        render_bounds = self._resolve_render_bounds(base_bounds, crop_bounds)
        target_w = 1400 if crop_bounds else 1200
        merc_south = _lat_to_merc(render_bounds["south"])
        merc_north = _lat_to_merc(render_bounds["north"])
        target_h = int(
            target_w * (merc_north - merc_south) / (render_bounds["east"] - render_bounds["west"])
        )
        target_h = max(target_h, 1)

        sat_lon_r = round(sat_lon, 1)
        regrid_key = (
            data.shape,
            sat_lon_r,
            target_w,
            target_h,
            round(render_bounds["west"], 3),
            round(render_bounds["east"], 3),
            round(render_bounds["south"], 3),
            round(render_bounds["north"], 3),
        )

        if _regrid_cache_get(regrid_key) is None:
            # --- One-time setup per product/satellite ---
            # Build/reuse pyproj Transformer
            tx_key = (sat_lon_r, int(sat_height), sweep)
            if tx_key not in _transformer_cache:
                proj_string = (
                    f'+proj=geos +h={sat_height} +lon_0={sat_lon}'
                    f' +sweep={sweep} +ellps=WGS84 +units=m +no_defs'
                )
                _transformer_cache[tx_key] = Transformer.from_crs(
                    proj_string, 'EPSG:4326', always_xy=True
                )
                _prune_cache(_transformer_cache, _TRANSFORMER_CACHE_MAX)
                logger.info("Built GOES transformer for lon=%.1f h=%d sweep=%s",
                            sat_lon, int(sat_height), sweep)
            transformer = _transformer_cache[tx_key]

            # Geostationary → lat/lon for entire source grid
            x_rad = dataset['x'].values
            y_rad = dataset['y'].values
            x_m = x_rad * sat_height
            y_m = y_rad * sat_height
            x_grid, y_grid = np.meshgrid(x_m, y_m)
            lon_src, lat_src = transformer.transform(x_grid, y_grid)

            # Replace off-limb (inf/nan) coords so they exceed distance threshold
            lon_src_safe = np.where(np.isfinite(lon_src), lon_src, 1e9)
            lat_src_safe = np.where(np.isfinite(lat_src), lat_src, 1e9)
            src_coords = np.column_stack([lon_src_safe.ravel(), lat_src_safe.ravel()])

            lon_target = np.linspace(render_bounds["west"], render_bounds["east"], target_w)
            merc_target = np.linspace(merc_north, merc_south, target_h)
            lat_target = np.degrees(
                2 * np.arctan(np.exp(np.radians(merc_target))) - np.pi / 2
            )
            lon_grid_target, lat_grid_target = np.meshgrid(lon_target, lat_target)
            target_coords = np.column_stack(
                [lon_grid_target.ravel(), lat_grid_target.ravel()]
            )

            # KDTree query — only once; result reused for all subsequent renders
            tree = cKDTree(src_coords)
            distances, flat_indices = tree.query(
                target_coords, k=1, distance_upper_bound=0.15
            )
            valid_matches = distances < np.inf

            _regrid_cache_put(regrid_key, flat_indices.astype(np.int32), valid_matches)
            logger.info(
                "Built satellite regrid cache shape=%s → %dx%d (%d/%d valid)",
                data.shape, target_w, target_h,
                int(valid_matches.sum()), target_w * target_h,
            )

        flat_indices, valid_matches = _regrid_cache_get(regrid_key)

        # Apply cached mapping — just an array index op, ~1ms
        data_flat = data.ravel().astype(np.float32)
        data_reprojected = np.full(target_h * target_w, np.nan, dtype=np.float32)
        data_reprojected[valid_matches] = data_flat[flat_indices[valid_matches]]
        data_reprojected = data_reprojected.reshape((target_h, target_w))
        if crop_bounds:
            cropped = data_reprojected
            bounds = render_bounds
        else:
            valid_mask = np.isfinite(data_reprojected)
            row_slice, col_slice, bounds = self._crop_mercator_extent(valid_mask, target_w, target_h)
            cropped = data_reprojected[row_slice, col_slice]
            cropped, bounds = self._crop_to_visible_footprint(cropped, bounds, edge_ramp_cfg=edge_ramp_cfg)

        self._render_reprojected(
            cropped,
            None,
            output_path,
            cmap,
            vmin,
            vmax,
            gamma=gamma,
            tone_lift=tone_lift,
            edge_ramp_cfg=edge_ramp_cfg,
            bounds=bounds,
        )

        return bounds

    def _create_usa_mask_from_shapefile(self, width: int, height: int) -> np.ndarray:
        """Create USA mask using high-quality natural_earth shapefile (cached).

        Result is cached per (width, height) so the expensive point-in-polygon
        test runs only once per output resolution.
        """
        key = (width, height)
        if key in _sat_usa_mask_cache:
            return _sat_usa_mask_cache[key]

        lon_1d = np.linspace(DATA_WEST, DATA_EAST, width)
        merc_1d = np.linspace(MERC_NORTH, MERC_SOUTH, height)
        lat_1d = np.degrees(2 * np.arctan(np.exp(np.radians(merc_1d))) - np.pi / 2)
        lon_grid, lat_grid = np.meshgrid(lon_1d, lat_1d)

        if _USA_PATH is not None:
            pts = np.column_stack([lon_grid.ravel(), lat_grid.ravel()])
            inside = _USA_PATH.contains_points(pts).reshape(height, width)
            logger.info("USA mask (natural_earth) %dx%d: %d/%d inside (%.1f%%)",
                        width, height, inside.sum(), inside.size,
                        inside.sum() / inside.size * 100)
            _sat_usa_mask_cache[key] = inside
            _prune_cache(_sat_usa_mask_cache, _USA_MASK_CACHE_MAX)
            return inside

        logger.warning("USA shapefile not available, returning full mask")
        mask = np.ones((height, width), dtype=bool)
        _sat_usa_mask_cache[key] = mask
        _prune_cache(_sat_usa_mask_cache, _USA_MASK_CACHE_MAX)
        return mask

    def _crop_mercator_extent(
        self,
        valid_mask: np.ndarray,
        width: int,
        height: int,
        pad_pixels: int = 2,
        bounds: dict | None = None,
    ) -> tuple[slice, slice, dict]:
        """Return cropped row/column slices and matching geographic bounds.

        The Mercator grid is uniformly spaced in longitude and Mercator Y, so
        bounds are derived from pixel edges rather than pixel centers.
        """
        if valid_mask.shape != (height, width):
            raise ValueError(
                f"Valid mask shape {valid_mask.shape} does not match {height}x{width}"
            )

        base_bounds = bounds or {
            "west": DATA_WEST,
            "east": DATA_EAST,
            "south": DATA_SOUTH,
            "north": DATA_NORTH,
        }

        rows, cols = np.where(valid_mask)
        if rows.size == 0 or cols.size == 0:
            return (
                slice(0, height),
                slice(0, width),
                base_bounds,
            )

        row_start = max(int(rows.min()) - pad_pixels, 0)
        row_stop = min(int(rows.max()) + pad_pixels + 1, height)
        col_start = max(int(cols.min()) - pad_pixels, 0)
        col_stop = min(int(cols.max()) + pad_pixels + 1, width)

        lon_edges = np.linspace(base_bounds["west"], base_bounds["east"], width + 1, dtype=np.float64)
        merc_edges = np.linspace(
            _lat_to_merc(base_bounds["north"]),
            _lat_to_merc(base_bounds["south"]),
            height + 1,
            dtype=np.float64,
        )

        bounds = {
            "west": float(lon_edges[col_start]),
            "east": float(lon_edges[col_stop]),
            "south": _merc_to_lat(float(merc_edges[row_stop])),
            "north": _merc_to_lat(float(merc_edges[row_start])),
        }
        return slice(row_start, row_stop), slice(col_start, col_stop), bounds

    def _crop_array_to_bounds(
        self,
        data: np.ndarray,
        base_bounds: dict,
        crop_bounds: dict | None,
    ) -> tuple[np.ndarray, dict]:
        """Crop a Mercator-aligned array to a requested geographic box."""
        if not crop_bounds:
            return data, base_bounds

        west = max(float(base_bounds["west"]), float(crop_bounds["west"]))
        east = min(float(base_bounds["east"]), float(crop_bounds["east"]))
        south = max(float(base_bounds["south"]), float(crop_bounds["south"]))
        north = min(float(base_bounds["north"]), float(crop_bounds["north"]))

        if east <= west or north <= south:
            return data, base_bounds

        h, w = data.shape[:2]
        lon_edges = np.linspace(base_bounds["west"], base_bounds["east"], w + 1, dtype=np.float64)
        merc_edges = np.linspace(
            _lat_to_merc(base_bounds["north"]),
            _lat_to_merc(base_bounds["south"]),
            h + 1,
            dtype=np.float64,
        )

        col_start = int(np.searchsorted(lon_edges, west, side="right") - 1)
        col_stop = int(np.searchsorted(lon_edges, east, side="left"))
        merc_north = _lat_to_merc(north)
        merc_south = _lat_to_merc(south)
        row_start = int(np.searchsorted(-merc_edges, -merc_north, side="right") - 1)
        row_stop = int(np.searchsorted(-merc_edges, -merc_south, side="left"))

        col_start = max(0, min(col_start, w - 1))
        col_stop = max(col_start + 1, min(col_stop, w))
        row_start = max(0, min(row_start, h - 1))
        row_stop = max(row_start + 1, min(row_stop, h))

        cropped = data[row_start:row_stop, col_start:col_stop]
        bounds = {
            "west": float(lon_edges[col_start]),
            "east": float(lon_edges[col_stop]),
            "south": _merc_to_lat(float(merc_edges[row_stop])),
            "north": _merc_to_lat(float(merc_edges[row_start])),
        }
        return cropped, bounds

    @staticmethod
    def _resolve_render_bounds(base_bounds: dict, crop_bounds: dict | None) -> dict:
        """Resolve render bounds clipped to the valid base domain."""
        if not crop_bounds:
            return {
                "west": float(base_bounds["west"]),
                "east": float(base_bounds["east"]),
                "south": float(base_bounds["south"]),
                "north": float(base_bounds["north"]),
            }

        west = max(float(base_bounds["west"]), float(crop_bounds["west"]))
        east = min(float(base_bounds["east"]), float(crop_bounds["east"]))
        south = max(float(base_bounds["south"]), float(crop_bounds["south"]))
        north = min(float(base_bounds["north"]), float(crop_bounds["north"]))
        if east <= west or north <= south:
            return {
                "west": float(base_bounds["west"]),
                "east": float(base_bounds["east"]),
                "south": float(base_bounds["south"]),
                "north": float(base_bounds["north"]),
            }
        return {"west": west, "east": east, "south": south, "north": north}
    
    def _render_reprojected(
        self,
        data: np.ndarray,
        usa_mask: np.ndarray,
        output_path: Path,
        cmap: str = 'Greys_r',
        vmin: float = None,
        vmax: float = None,
        extend_masked: bool = True,
        gamma: float = 1.0,
        tone_lift: float = 0.0,
        edge_ramp_cfg: dict | None = None,
        bounds: dict | None = None,
    ):
        """Render already-reprojected data as RGBA PNG with USA mask.

        Uses PIL directly (same approach as model renderer) for pixel-precise
        alpha control. Outside-USA pixels are set to NaN before colormapping
        so they become fully transparent (0,0,0,0).
        """
        from PIL import Image
        import matplotlib.colors as mcolors

        vmin, vmax = self._display_range(data, vmin, vmax, cmap)

        # Set invalid pixels to NaN so they become transparent.
        # USA mask intentionally disabled for full-coverage satellite output.
        out = data.copy().astype(np.float32)
        # out[~usa_mask] = np.nan  # outside USA → transparent

        # Normalize and apply colormap → RGBA uint8
        norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
        cmap_obj = plt.get_cmap(cmap) if isinstance(cmap, str) else cmap
        cmap_obj = cmap_obj.copy()
        cmap_obj.set_bad((0, 0, 0, 0))  # NaN → fully transparent

        # Apply gamma correction if specified
        normalized = norm(out)
        if gamma != 1.0 and gamma is not None:
            normalized = np.power(np.clip(normalized, 0, 1), gamma)
        if tone_lift and tone_lift > 0:
            normalized = np.clip(normalized, 0, 1)
            # Lift mid/high reflectance clouds without blowing out the
            # brightest tops or flattening low-contrast surface detail.
            lift_mask = np.clip((normalized - 0.18) / 0.82, 0, 1)
            normalized = np.clip(
                normalized + tone_lift * np.sqrt(lift_mask) * (1.0 - normalized),
                0,
                1,
            )
        rgba = cmap_obj(normalized, bytes=True)
        nan_mask = np.isnan(out)

        # For products that should read as full fields, softly extend colors
        # into masked retrieval gaps before compositing over the dark canvas.
        if extend_masked and np.any(nan_mask) and not np.all(nan_mask):
            rgba = self._extend_outside_domain_rgba(
                rgba,
                nan_mask.copy(),
                fade_start=0.03,
                fade_end=0.45,
                apply_alpha_fade=False,
            )
            nan_mask = np.zeros_like(nan_mask, dtype=bool)

        # Geographic edge ramp: fade data at map boundaries
        h, w = rgba.shape[:2]
        edge_ramp = _build_edge_ramp(w, h, edge_ramp_cfg, bounds=bounds)
        alpha_f = rgba[:, :, 3].astype(np.float32)
        alpha_f[nan_mask] = 0
        alpha_f *= edge_ramp
        # Composite: blend satellite data over dark background (#0a0a14)
        # so the image is fully opaque — satellite IS the basemap.
        bg = np.array([10, 10, 20], dtype=np.uint8)
        alpha_norm = (alpha_f / 255.0).astype(np.float32)
        for c in range(3):
            rgba[:, :, c] = (
                rgba[:, :, c].astype(np.float32) * alpha_norm
                + bg[c] * (1.0 - alpha_norm)
            ).astype(np.uint8)
        rgba[:, :, 3] = 255  # fully opaque everywhere

        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        Image.fromarray(rgba, 'RGBA').save(str(output_path), compress_level=1)

        logger.debug("Rendered to Mercator with USA mask: %s", output_path)

    def _crop_to_visible_footprint(
        self,
        data: np.ndarray,
        bounds: dict,
        edge_ramp_cfg: dict | None = None,
    ) -> tuple[np.ndarray, dict]:
        """Trim away rows/cols that only exist in the soft edge-fade zone."""
        threshold = None
        if edge_ramp_cfg:
            threshold = edge_ramp_cfg.get("visible_crop_threshold")
        if threshold is None:
            return data, bounds

        h, w = data.shape[:2]
        visible_mask = np.isfinite(data)
        if not np.any(visible_mask):
            return data, bounds

        edge_ramp = _build_edge_ramp(w, h, edge_ramp_cfg, bounds=bounds)
        visible_mask &= edge_ramp > float(threshold)
        if not np.any(visible_mask):
            return data, bounds

        row_slice, col_slice, cropped_bounds = self._crop_mercator_extent(
            visible_mask,
            w,
            h,
            pad_pixels=0,
            bounds=bounds,
        )
        return data[row_slice, col_slice], cropped_bounds
    
    def _create_usa_mask(self, bounds: dict, width: int, height: int) -> np.ndarray:
        """Create a boolean mask for USA coverage using rasterio features."""
        from matplotlib.path import Path as MplPath
        
        # Define USA boundary polygon (simplified CONUS)
        # These are approximate lon/lat coordinates for US mainland borders
        usa_coords = np.array([
            # West Coast
            [-124.7, 48.4], [-124.5, 46.3], [-123.9, 45.5], [-123.9, 42.0],
            [-124.4, 41.9], [-124.2, 40.0], [-122.8, 37.8], [-119.0, 32.5],
            [-117.1, 32.5],  # CA/Mexico border
            # South border
            [-109.0, 31.3], [-108.2, 31.3], [-106.5, 31.8], [-103.0, 28.9],
            [-100.0, 28.9], [-97.0, 25.9], [-97.4, 25.8],  # TX/Mexico
            # Gulf Coast  
            [-97.0, 26.0], [-96.5, 28.0], [-94.0, 29.5], [-90.0, 28.8],
            [-88.0, 30.2], [-85.0, 29.9], [-82.8, 27.6], [-80.0, 25.1],
            [-80.0, 24.5], [-82.5, 24.5], [-83.0, 27.0],  # FL
            # East Coast
            [-80.2, 32.0], [-80.0, 32.5], [-78.8, 33.8], [-77.0, 34.7],
            [-75.5, 35.2], [-75.5, 37.0], [-74.5, 40.2], [-73.9, 40.8],
            [-72.0, 41.3], [-70.0, 41.2], [-69.9, 43.7], [-70.8, 43.0],
            # North border
            [-70.0, 45.3], [-67.0, 47.4], [-67.9, 47.9], [-69.0, 47.5],
            [-71.0, 45.0], [-72.0, 45.0], [-74.7, 45.0], [-75.4, 44.9],
            [-79.6, 43.6], [-82.4, 42.9], [-83.0, 46.5], [-84.7, 46.8],
            [-87.0, 47.5], [-89.0, 48.0], [-89.6, 48.0],
            [-94.7, 49.0], [-95.2, 49.4], [-96.8, 49.0],
            [-123.3, 49.0],  # WA/Canada
            # Close polygon
            [-124.7, 48.4]
        ])
        
        # Create meshgrid of lat/lon
        lons = np.linspace(bounds['lon_min'], bounds['lon_max'], width)
        lats = np.linspace(bounds['lat_max'], bounds['lat_min'], height)  # Flip for image coords
        lon_grid, lat_grid = np.meshgrid(lons, lats)
        
        # Create path and test points
        path = MplPath(usa_coords)
        points = np.column_stack([lon_grid.ravel(), lat_grid.ravel()])
        mask = path.contains_points(points).reshape((height, width))
        
        return mask

    # ------------------------------------------------------------------
    # GOES East + West compositing
    # ------------------------------------------------------------------

    @_serialize_regrid
    def reproject_to_array(
        self,
        data: np.ndarray,
        dataset: xr.Dataset,
        target_w: int = 1200,
    ) -> tuple:
        """Reproject GOES data to Web Mercator grid, returning raw array.

        Returns (data_reprojected, target_w, target_h) where
        data_reprojected is float32 with NaN for no-data pixels.
        """
        proj_attrs = dataset['goes_imager_projection'].attrs
        sat_lon = proj_attrs.get('longitude_of_projection_origin', -137.0)
        sat_height = proj_attrs.get('perspective_point_height', 35786023.0)
        sweep = proj_attrs.get('sweep_angle_axis', 'x')

        target_h = int(target_w * (MERC_NORTH - MERC_SOUTH) / (DATA_EAST - DATA_WEST))
        sat_lon_r = round(sat_lon, 1)
        regrid_key = (data.shape, sat_lon_r, target_w, target_h)

        if _regrid_cache_get(regrid_key) is None:
            tx_key = (sat_lon_r, int(sat_height), sweep)
            if tx_key not in _transformer_cache:
                proj_string = (
                    f'+proj=geos +h={sat_height} +lon_0={sat_lon}'
                    f' +sweep={sweep} +ellps=WGS84 +units=m +no_defs'
                )
                _transformer_cache[tx_key] = Transformer.from_crs(
                    proj_string, 'EPSG:4326', always_xy=True
                )
                _prune_cache(_transformer_cache, _TRANSFORMER_CACHE_MAX)
                logger.info("Built GOES transformer for lon=%.1f", sat_lon)
            transformer = _transformer_cache[tx_key]

            x_rad = dataset['x'].values
            y_rad = dataset['y'].values
            x_m = x_rad * sat_height
            y_m = y_rad * sat_height
            x_grid, y_grid = np.meshgrid(x_m, y_m)
            lon_src, lat_src = transformer.transform(x_grid, y_grid)

            lon_src_safe = np.where(np.isfinite(lon_src), lon_src, 1e9)
            lat_src_safe = np.where(np.isfinite(lat_src), lat_src, 1e9)
            src_coords = np.column_stack([lon_src_safe.ravel(), lat_src_safe.ravel()])

            lon_target = np.linspace(DATA_WEST, DATA_EAST, target_w)
            merc_target = np.linspace(MERC_NORTH, MERC_SOUTH, target_h)
            lat_target = np.degrees(
                2 * np.arctan(np.exp(np.radians(merc_target))) - np.pi / 2
            )
            lon_grid_target, lat_grid_target = np.meshgrid(lon_target, lat_target)
            target_coords = np.column_stack(
                [lon_grid_target.ravel(), lat_grid_target.ravel()]
            )

            tree = cKDTree(src_coords)
            distances, flat_indices = tree.query(
                target_coords, k=1, distance_upper_bound=0.15
            )
            valid_matches = distances < np.inf

            _regrid_cache_put(regrid_key, flat_indices.astype(np.int32), valid_matches)
            logger.info(
                "Built composite regrid cache shape=%s sat=%.1f → %dx%d (%d valid)",
                data.shape, sat_lon, target_w, target_h,
                int(valid_matches.sum()),
            )

        flat_indices, valid_matches = _regrid_cache_get(regrid_key)

        data_flat = data.ravel().astype(np.float32)
        data_reprojected = np.full(target_h * target_w, np.nan, dtype=np.float32)
        data_reprojected[valid_matches] = data_flat[flat_indices[valid_matches]]
        data_reprojected = data_reprojected.reshape((target_h, target_w))

        return data_reprojected, target_w, target_h

    def composite_blend(
        self,
        west: np.ndarray,
        east: np.ndarray,
        target_w: int,
        blend_west_lon: float = -113.0,
        blend_east_lon: float = -100.0,
    ) -> np.ndarray:
        """Blend two reprojected arrays with longitude-weighted crossfade.

        West of blend_west_lon: west only.
        East of blend_east_lon: east only.
        In between: linear crossfade weighted by longitude.
        Where one source has NaN: use the other at full weight.
        """
        lon_1d = np.linspace(DATA_WEST, DATA_EAST, target_w)
        # weight_east: 0 at blend_west_lon, 1 at blend_east_lon
        w_east = np.clip(
            (lon_1d - blend_west_lon) / (blend_east_lon - blend_west_lon),
            0.0, 1.0,
        ).astype(np.float32)
        w_west = 1.0 - w_east
        # Broadcast to 2D
        we = w_east[np.newaxis, :]   # (1, W)
        ww = w_west[np.newaxis, :]

        w_valid = np.isfinite(west)
        e_valid = np.isfinite(east)

        # Start with NaN, fill in order of priority
        out = np.full_like(west, np.nan)
        # Where only one source exists, use it at full weight
        out[w_valid & ~e_valid] = west[w_valid & ~e_valid]
        out[e_valid & ~w_valid] = east[e_valid & ~w_valid]
        # Where both exist, blend by longitude weight
        both = w_valid & e_valid
        if np.any(both):
            # Broadcast weight arrays to full 2D
            ww_2d = np.broadcast_to(ww, west.shape)
            we_2d = np.broadcast_to(we, west.shape)
            out[both] = ww_2d[both] * west[both] + we_2d[both] * east[both]

        return out

    def render_composite(
        self,
        west_data: np.ndarray,
        west_ds: xr.Dataset,
        east_data: np.ndarray,
        east_ds: xr.Dataset,
        output_path: Path,
        cmap: str = 'Greys_r',
        vmin: float = None,
        vmax: float = None,
        blend_west_lon: float = -113.0,
        blend_east_lon: float = -100.0,
        target_w: int = 1200,
        gamma: float = 1.0,
        tone_lift: float = 0.0,
        edge_ramp_cfg: dict | None = None,
        crop_bounds: dict | None = None,
    ) -> dict:
        """Reproject both satellites, blend, and render to PNG."""
        west_reproj, tw, th = self.reproject_to_array(west_data, west_ds, target_w)
        east_reproj, _, _ = self.reproject_to_array(east_data, east_ds, target_w)
        blended = self.composite_blend(
            west_reproj, east_reproj, tw, blend_west_lon, blend_east_lon,
        )
        if crop_bounds:
            cropped, bounds = self._crop_array_to_bounds(
                blended,
                {
                    "west": DATA_WEST,
                    "east": DATA_EAST,
                    "south": DATA_SOUTH,
                    "north": DATA_NORTH,
                },
                crop_bounds,
            )
        else:
            valid_mask = np.isfinite(blended)
            row_slice, col_slice, bounds = self._crop_mercator_extent(valid_mask, tw, th)
            cropped = blended[row_slice, col_slice]
            cropped, bounds = self._crop_to_visible_footprint(cropped, bounds, edge_ramp_cfg=edge_ramp_cfg)
        self._render_reprojected(
            cropped,
            None,
            output_path,
            cmap,
            vmin,
            vmax,
            gamma=gamma,
            tone_lift=tone_lift,
            edge_ramp_cfg=edge_ramp_cfg,
            bounds=bounds,
        )
        return bounds

    def render_composite_rgb(
        self,
        west_rgb: np.ndarray,
        west_ds: xr.Dataset,
        east_rgb: np.ndarray,
        east_ds: xr.Dataset,
        output_path: Path,
        blend_west_lon: float = -113.0,
        blend_east_lon: float = -100.0,
        target_w: int = 1200,
    ) -> dict:
        """Reproject both satellites' RGB, blend, and render to PNG."""
        # Reproject each channel independently
        channels_blended = []
        for ch in range(3):
            w_reproj, tw, th = self.reproject_to_array(
                west_rgb[:, :, ch], west_ds, target_w)
            e_reproj, _, _ = self.reproject_to_array(
                east_rgb[:, :, ch], east_ds, target_w)
            blended_ch = self.composite_blend(
                w_reproj, e_reproj, tw, blend_west_lon, blend_east_lon,
            )
            channels_blended.append(blended_ch)

        rgb_out = np.stack(channels_blended, axis=-1)  # (H, W, 3)
        no_data = ~np.isfinite(rgb_out).all(axis=-1)
        row_slice, col_slice, bounds = self._crop_mercator_extent(~no_data, tw, th)
        rgb_out = rgb_out[row_slice, col_slice, :]
        no_data = no_data[row_slice, col_slice]
        rgb_out = np.nan_to_num(rgb_out, nan=0.0)

        th, tw = rgb_out.shape[:2]

        # Build RGBA
        rgba = np.zeros((th, tw, 4), dtype=np.uint8)
        rgba[:, :, :3] = (np.clip(rgb_out, 0, 1) * 255).astype(np.uint8)
        alpha_f = np.where(no_data, 0.0, 255.0).astype(np.float32)

        # Geographic edge ramp + dark background composite
        edge_ramp = _build_edge_ramp(tw, th)
        alpha_f *= edge_ramp
        bg = np.array([10, 10, 20], dtype=np.uint8)
        alpha_norm = (alpha_f / 255.0).astype(np.float32)
        for c in range(3):
            rgba[:, :, c] = (
                rgba[:, :, c].astype(np.float32) * alpha_norm
                + bg[c] * (1.0 - alpha_norm)
            ).astype(np.uint8)
        rgba[:, :, 3] = 255

        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        Image.fromarray(rgba, 'RGBA').save(str(output_path), compress_level=1)

        return bounds

    def render_abi_product(
        self,
        dataset: xr.Dataset,
        product_type: str,
        filename: str = None,
        region_name: str | None = None,
        region_bounds: dict | None = None,
    ) -> "RenderedSatellite":
        """Unified MCMIP renderer for single-band ABI products.

        Looks up product_type in MCMIP_PRODUCT_CONFIG, extracts the
        appropriate CMI variable, converts units, and reprojects to Mercator.
        """
        cfg = MCMIP_PRODUCT_CONFIG.get(product_type)
        if cfg is None:
            raise ValueError(f"Unknown MCMIP product type: {product_type}")

        band = cfg["band"]
        var_name = f"CMI_C{band:02d}"

        raw = self._get_brightness_temp(dataset, band)
        if raw is None:
            raise ValueError(f"Could not extract {var_name} from dataset")

        if cfg["type"] == "bt":
            data = raw.values.astype(np.float32) - 273.15
        else:  # refl
            data = raw.values.astype(np.float32) * 100.0

        if filename is None:
            timestamp = self._get_timestamp(dataset)
            filename = f"goes_{product_type}_band{band}_{timestamp}.png"

        png_path = self.png_dir / filename

        bounds = self.render_to_mercator(
            data,
            dataset,
            png_path,
            cmap=cfg["cmap"],
            vmin=cfg["vmin"],
            vmax=cfg["vmax"],
            gamma=cfg.get("gamma", 1.0),
            tone_lift=cfg.get("tone_lift", 0.0),
            edge_ramp_cfg=cfg.get("edge_ramp"),
            crop_bounds=region_bounds,
        )

        meta = {
            "satellite": dataset.attrs.get("platform_ID", "GOES"),
            "product": product_type,
            "band": band,
            "timestamp": self._get_timestamp(dataset),
            "bounds": bounds,
            "png_path": str(png_path.relative_to(self.output_dir.parent)),
        }
        if region_name:
            meta["region"] = region_name
        meta_path = self.meta_dir / filename.replace(".png", ".json")
        write_frame_meta(meta_path, meta)

        return RenderedSatellite(
            png_path=png_path,
            bounds=bounds,
            timestamp=meta["timestamp"],
            satellite=meta["satellite"],
            product=product_type,
            band=band,
        )

    def render_natural_color(
        self,
        dataset: xr.Dataset,
        filename: str = None
    ) -> "RenderedSatellite":
        """Render natural color (true color) RGB composite.

        R = Band 2 (Red), G = veggie-corrected (0.45*B2 + 0.1*B3 + 0.45*B1),
        B = Band 1 (Blue).  Gamma 0.5 applied, no colorscale legend.
        """
        red = self._get_brightness_temp(dataset, 2)
        green = self._get_brightness_temp(dataset, 3)
        blue = self._get_brightness_temp(dataset, 1)

        if any(x is None for x in [red, green, blue]):
            raise ValueError("Missing required bands (1, 2, 3) for natural color")

        r = np.clip(red.values.astype(np.float32), 0, 1)
        g = np.clip(green.values.astype(np.float32), 0, 1)
        b = np.clip(blue.values.astype(np.float32), 0, 1)

        g_corrected = np.clip(0.45 * r + 0.1 * g + 0.45 * b, 0, 1)

        rgb = np.stack([r, g_corrected, b], axis=-1)
        rgb = np.power(np.clip(rgb, 0, 1), 0.5)

        if filename is None:
            timestamp = self._get_timestamp(dataset)
            filename = f"goes_natural_color_{timestamp}.png"

        png_path = self.png_dir / filename
        bounds = self.render_rgb_mercator(rgb, dataset, png_path)

        meta = {
            "satellite": dataset.attrs.get("platform_ID", "GOES"),
            "product": "natural_color",
            "timestamp": self._get_timestamp(dataset),
            "bounds": bounds,
            "png_path": str(png_path.relative_to(self.output_dir.parent)),
        }
        meta_path = self.meta_dir / filename.replace(".png", ".json")
        write_frame_meta(meta_path, meta)

        return RenderedSatellite(
            png_path=png_path,
            bounds=bounds,
            timestamp=meta["timestamp"],
            satellite=meta["satellite"],
            product="natural_color",
            band=None,
        )

    def render_day_cloud_phase(
        self,
        dataset: xr.Dataset,
        filename: str = None
    ) -> "RenderedSatellite":
        """Render Day Cloud Phase RGB composite using the NOAA/STAR recipe.

        R = Band 13 BT, 7.5 to -53.5 C
        G = Band 2 reflectance, 0 to 78%
        B = Band 5 reflectance, 1 to 59%
        """
        b2 = self._get_brightness_temp(dataset, 2)
        b5 = self._get_brightness_temp(dataset, 5)
        b13 = self._get_brightness_temp(dataset, 13)

        if any(x is None for x in [b2, b5, b13]):
            raise ValueError("Missing required bands (2, 5, 13) for Day Cloud Phase")

        rgb = self._build_day_cloud_phase_rgb(
            b2.values.astype(np.float32),
            b5.values.astype(np.float32),
            b13.values.astype(np.float32),
        )

        if filename is None:
            timestamp = self._get_timestamp(dataset)
            filename = f"goes_day_cloud_phase_{timestamp}.png"

        png_path = self.png_dir / filename
        bounds = self.render_rgb_mercator(rgb, dataset, png_path)

        meta = {
            "satellite": dataset.attrs.get("platform_ID", "GOES"),
            "product": "day_cloud_phase",
            "timestamp": self._get_timestamp(dataset),
            "bounds": bounds,
            "png_path": str(png_path.relative_to(self.output_dir.parent)),
        }
        meta_path = self.meta_dir / filename.replace(".png", ".json")
        write_frame_meta(meta_path, meta)

        return RenderedSatellite(
            png_path=png_path,
            bounds=bounds,
            timestamp=meta["timestamp"],
            satellite=meta["satellite"],
            product="day_cloud_phase",
            band=None,
        )

    def render_sandwich(
        self,
        dataset: xr.Dataset,
        filename: str = None
    ) -> "RenderedSatellite":
        """Render a daytime VIS/IR Sandwich product.

        The visible channel carries cloud-top texture while enhanced Band 13
        color is blended onto cold cloud tops.
        """
        b2 = self._get_brightness_temp(dataset, 2)
        b13 = self._get_brightness_temp(dataset, 13)

        if b2 is None or b13 is None:
            raise ValueError("Missing required bands (2, 13) for Sandwich RGB")

        rgb = self._build_sandwich_rgb(
            b2.values.astype(np.float32),
            b13.values.astype(np.float32),
        )

        if filename is None:
            timestamp = self._get_timestamp(dataset)
            filename = f"goes_sandwich_{timestamp}.png"

        png_path = self.png_dir / filename
        bounds = self.render_rgb_mercator(rgb, dataset, png_path)

        meta = {
            "satellite": dataset.attrs.get("platform_ID", "GOES"),
            "product": "sandwich",
            "timestamp": self._get_timestamp(dataset),
            "bounds": bounds,
            "png_path": str(png_path.relative_to(self.output_dir.parent)),
        }
        meta_path = self.meta_dir / filename.replace(".png", ".json")
        write_frame_meta(meta_path, meta)

        return RenderedSatellite(
            png_path=png_path,
            bounds=bounds,
            timestamp=meta["timestamp"],
            satellite=meta["satellite"],
            product="sandwich",
            band=None,
        )

    @staticmethod
    def _scale_channel(data: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
        scaled = (data - vmin) / (vmax - vmin)
        return np.clip(scaled, 0, 1).astype(np.float32)

    def _build_day_cloud_phase_rgb(
        self,
        band2_reflectance: np.ndarray,
        band5_reflectance: np.ndarray,
        band13_bt_kelvin: np.ndarray,
    ) -> np.ndarray:
        bt13_c = band13_bt_kelvin - 273.15
        r = self._scale_channel(7.5 - bt13_c, 0.0, 61.0)
        g = self._scale_channel(band2_reflectance, 0.0, 0.78)
        b = self._scale_channel(band5_reflectance, 0.01, 0.59)
        return np.stack([r, g, b], axis=-1)

    def _build_sandwich_rgb(
        self,
        band2_reflectance: np.ndarray,
        band13_bt_kelvin: np.ndarray,
    ) -> np.ndarray:
        vis = self._scale_channel(band2_reflectance, 0.0, 0.85)
        vis = np.power(vis, 0.55)
        base = np.stack([vis, vis, vis], axis=-1)

        bt13_c = band13_bt_kelvin - 273.15
        try:
            cmap_obj = plt.get_cmap("wx_enhanced_ir")
        except Exception:
            cmap_obj = plt.get_cmap("turbo")
        ir_norm = self._scale_channel(bt13_c, -90.0, -25.0)
        enhanced = np.asarray(cmap_obj(ir_norm)[..., :3], dtype=np.float32)

        cold_alpha = self._scale_channel(-35.0 - bt13_c, 0.0, 35.0)
        cold_alpha = np.power(cold_alpha, 0.7) * 0.82
        cold_alpha = cold_alpha[..., np.newaxis]
        return np.clip(base * (1.0 - cold_alpha) + enhanced * cold_alpha, 0, 1)

    @_serialize_regrid
    def render_rgb_mercator(
        self,
        rgb: np.ndarray,
        dataset: xr.Dataset,
        output_path: Path,
    ) -> dict:
        """Reproject a 3-channel (H, W, 3) RGB array from GOES geostationary
        projection to Web Mercator and save as an RGBA PNG with USA mask.

        Uses the same KD-tree approach as render_to_mercator but operates on
        3-channel data.  Pixels outside the USA boundary get alpha=0.

        Returns bounds dict.
        """
        from scipy.spatial import cKDTree
        from pyproj import Transformer
        from PIL import Image

        # Get projection info from dataset
        try:
            proj_attrs = dataset["goes_imager_projection"].attrs
            sat_lon = proj_attrs.get("longitude_of_projection_origin", -75.2)
            sat_height = proj_attrs.get("perspective_point_height", 35786023.0)
            sweep = proj_attrs.get("sweep_angle_axis", "x")
        except Exception:
            sat_lon = -75.2
            sat_height = 35786023.0
            sweep = "x"

        _DATA_SOUTH, _DATA_NORTH = 15.0, 52.0
        _DATA_WEST, _DATA_EAST = -145.0, -50.0
        _MERC_SOUTH = float(_lat_to_merc(_DATA_SOUTH))
        _MERC_NORTH = float(_lat_to_merc(_DATA_NORTH))
        target_w = 1200
        target_h = int(target_w * (_MERC_NORTH - _MERC_SOUTH) / (_DATA_EAST - _DATA_WEST))
        valid_src = np.all(np.isfinite(rgb), axis=-1).ravel()
        if valid_src.sum() < 100:
            blank = np.zeros((target_h, target_w, 4), dtype=np.uint8)
            output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            Image.fromarray(blank, "RGBA").save(str(output_path), compress_level=1)
            return {"west": _DATA_WEST, "east": _DATA_EAST,
                    "south": _DATA_SOUTH, "north": _DATA_NORTH}
        x_rad, y_rad = dataset["x"].values, dataset["y"].values
        geometry_digest = hashlib.sha1(x_rad.tobytes() + y_rad.tobytes()).hexdigest()
        validity_digest = hashlib.sha1(np.packbits(valid_src).tobytes()).hexdigest()
        key = ("rgb", rgb.shape[:2], sat_lon, sat_height, sweep, target_w, target_h,
               geometry_digest, validity_digest)
        mapping = _regrid_cache_get(key)
        if mapping is None:
            x_grid, y_grid = np.meshgrid(x_rad * sat_height, y_rad * sat_height)
            transformer = Transformer.from_crs(
                f"+proj=geos +h={sat_height} +lon_0={sat_lon} +sweep={sweep} +ellps=WGS84 +units=m +no_defs",
                "EPSG:4326", always_xy=True,
            )
            lon_src, lat_src = transformer.transform(x_grid, y_grid)
            src_coords = np.column_stack([lon_src.ravel()[valid_src], lat_src.ravel()[valid_src]])
            lon_target = np.linspace(_DATA_WEST, _DATA_EAST, target_w)
            merc_target = np.linspace(_MERC_NORTH, _MERC_SOUTH, target_h)
            lat_target = np.degrees(2 * np.arctan(np.exp(np.radians(merc_target))) - np.pi / 2)
            lon_grid_t, lat_grid_t = np.meshgrid(lon_target, lat_target)
            tree = cKDTree(src_coords)
            distances, indices = tree.query(np.column_stack([lon_grid_t.ravel(), lat_grid_t.ravel()]),
                                            k=1, distance_upper_bound=0.15)
            valid_matches = distances < np.inf
            flat_indices = np.zeros(target_h * target_w, dtype=np.int32)
            source_indices = np.flatnonzero(valid_src)
            flat_indices[valid_matches] = source_indices[indices[valid_matches]]
            _regrid_cache_put(key, flat_indices, valid_matches)
            mapping = flat_indices, valid_matches
        flat_indices, valid_matches = mapping
        rgb_out = np.zeros((target_h * target_w, 3), dtype=np.float32)
        rgb_out[valid_matches] = rgb.reshape(-1, 3)[flat_indices[valid_matches]]
        rgb_out = rgb_out.reshape((target_h, target_w, 3))

        # No-data mask (pixels that had no nearby source)
        no_data = (~valid_matches).reshape((target_h, target_w))

        # USA mask (kept for easy re-enable; currently disabled)
        usa_mask = self._create_usa_mask_from_shapefile(target_w, target_h)

        # Build RGBA
        rgba = np.zeros((target_h, target_w, 4), dtype=np.uint8)
        rgba[:, :, :3] = (np.clip(rgb_out, 0, 1) * 255).astype(np.uint8)
        # alpha = np.where(usa_mask & ~no_data, 255, 0).astype(np.uint8)
        alpha_f = np.where(~no_data, 255.0, 0.0).astype(np.float32)

        # Geographic edge ramp + dark background composite
        edge_ramp = _build_edge_ramp(target_w, target_h)
        alpha_f *= edge_ramp
        bg = np.array([10, 10, 20], dtype=np.uint8)
        alpha_norm = (alpha_f / 255.0).astype(np.float32)
        for c in range(3):
            rgba[:, :, c] = (
                rgba[:, :, c].astype(np.float32) * alpha_norm
                + bg[c] * (1.0 - alpha_norm)
            ).astype(np.uint8)
        rgba[:, :, 3] = 255

        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        Image.fromarray(rgba, "RGBA").save(str(output_path), compress_level=1)
        logger.debug("Rendered RGB to Mercator: %s", output_path)

        return {
            "west": _DATA_WEST,
            "east": _DATA_EAST,
            "south": _DATA_SOUTH,
            "north": _DATA_NORTH,
        }

    def render_cmip_product(
        self,
        dataset: xr.Dataset,
        product_type: str,
        band_num: int,
        filename: str = None,
        region_name: str | None = None,
        region_bounds: dict | None = None,
    ) -> "RenderedSatellite":
        """Render CMIP single-band product at native resolution (1km for Band 2).
        
        CMIP provides higher resolution than MCMIP:
        - Band 2 (Visible): 1km native (vs 2km in MCMIP)
        - Bands 1,3-6: 1km native
        - Bands 7-16: 2km native
        """
        # Single-band CMIP files typically expose `CMI`, while some multi-band or
        # alternate exports use band-specific names.
        candidates = [f"CMI_C{band_num:02d}", f"C{band_num:02d}", "CMI"]
        var_name = next((name for name in candidates if name in dataset), None)
        if var_name is None:
            raise ValueError(f"Band {band_num} variable not found in CMIP dataset")

        raw = dataset[var_name]

        scale_overrides = {
            "vis": {"cmap": "gray", "vmin": 0, "vmax": 60},
            "vis_1km": {"cmap": "gray", "vmin": 0, "vmax": 75, "gamma": 0.65, "tone_lift": 0.12},
            "ir_native": {"cmap": "wx_enhanced_ir", "vmin": -110, "vmax": 50, "gamma": 1.0},
            "veggie": {"cmap": "gray", "vmin": 0, "vmax": 75},
        }
        scale_cfg = scale_overrides.get(product_type)

        # CMIP provides corrected reflectance for visible/NIR and BT in Kelvin for IR.
        if band_num in [7, 8, 9, 10, 11, 12, 13, 14, 15, 16]:
            data = raw.values.astype(np.float32) - 273.15
            cmap = scale_cfg["cmap"] if scale_cfg else "Greys"
            vmin = scale_cfg["vmin"] if scale_cfg else -110
            vmax = scale_cfg["vmax"] if scale_cfg else 50
        else:
            # Corrected reflectance should be a unitless factor near 0..1.
            # Clamp extreme decode artifacts before converting to percent so
            # native visible products do not wash out to a nearly solid frame.
            data = np.clip(raw.values.astype(np.float32), 0.0, 1.10) * 100.0
            cmap = scale_cfg["cmap"] if scale_cfg else "gray"
            vmin = scale_cfg["vmin"] if scale_cfg else 0
            vmax = scale_cfg["vmax"] if scale_cfg else 100
        
        if filename is None:
            timestamp = self._get_timestamp(dataset)
            filename = f"{product_type}_band{band_num}_{timestamp}.png"
        
        png_path = self.png_dir / filename
        
        native_1km = product_type == "vis_1km" and band_num == 2
        source_shape = tuple(int(v) for v in data.shape)
        target_width = None
        if native_1km:
            source_width = int(data.shape[1])
            if region_bounds:
                render_bounds = self._resolve_render_bounds(
                    {
                        "west": DATA_WEST,
                        "east": DATA_EAST,
                        "south": DATA_SOUTH,
                        "north": DATA_NORTH,
                    },
                    region_bounds,
                )
                mid_lat = (render_bounds["south"] + render_bounds["north"]) * 0.5
                lon_width = max(render_bounds["east"] - render_bounds["west"], 0.1)
                width_km = lon_width * 111.32 * max(float(np.cos(np.radians(mid_lat))), 0.25)
                if source_width >= 1800:
                    target_width = int(np.clip(round(width_km), 1800, source_width))
                else:
                    target_width = max(source_width, 1)
            else:
                target_width = min(source_width, 2800)

        gamma = scale_cfg.get("gamma", 1.0) if scale_cfg else 1.0
        tone_lift = scale_cfg.get("tone_lift", 0.0) if scale_cfg else 0.0
        bounds = self.render_to_mercator_cmip(
            data,
            dataset,
            png_path,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            gamma=gamma,
            tone_lift=tone_lift,
            crop_bounds=region_bounds,
            target_w=target_width,
        )
        
        meta = {
            "satellite": dataset.attrs.get("platform_ID", "GOES"),
            "product": product_type,
            "band": band_num,
            "resolution": "1km native" if native_1km else "native CMIP",
            "source_product": "ABI-L2-CMIP",
            "native_resolution": native_1km,
            "source_shape": list(source_shape),
            "target_width": target_width,
            "timestamp": self._get_timestamp(dataset),
            "bounds": bounds,
            "png_path": str(png_path.relative_to(self.output_dir.parent)),
        }
        if region_name:
            meta["region"] = region_name
        meta_path = self.meta_dir / filename.replace(".png", ".json")
        write_frame_meta(meta_path, meta)
        
        return RenderedSatellite(
            png_path=png_path,
            bounds=bounds,
            timestamp=meta["timestamp"],
            satellite=meta["satellite"],
            product=product_type,
            band=band_num,
        )

    def render_mesoscale_product(
        self,
        dataset: xr.Dataset,
        product_type: str,
        band_num: int,
        filename: str = None,
    ) -> "RenderedSatellite":
        """Render mesoscale sector (M1/M2) as standalone image.
        
        Key differences from CONUS rendering:
        - NO USA mask - show full sector even if over ocean/Canada/Mexico
        - Native 1km resolution for all bands
        - 1-minute update cadence
        - Small geographic extent (~1000x1000 km)
        """
        import matplotlib.pyplot as plt
        from PIL import Image
        import matplotlib.colors as mcolors
        
        candidates = [f"CMI_C{band_num:02d}", f"C{band_num:02d}", "CMI"]
        var_name = next((name for name in candidates if name in dataset), None)
        if var_name is None:
            raise ValueError(f"Band {band_num} variable not found in dataset")
        
        raw = dataset[var_name]
        
        # Convert units
        if band_num == 2:  # Visible
            data = raw.values.astype(np.float32) * 100.0
            cmap = 'Greys_r'
            vmin, vmax = 0, 100
        elif band_num in [7, 11, 12, 13, 14, 15, 16]:  # IR bands
            data = raw.values.astype(np.float32) - 273.15
            cmap = 'Greys'
            vmin, vmax = -110, 50
        else:  # Other bands
            data = raw.values.astype(np.float32) * 100.0
            cmap = 'gray'
            vmin, vmax = 0, 100
        
        if filename is None:
            timestamp = self._get_timestamp(dataset)
            filename = f"{product_type}_{timestamp}.png"
        
        png_path = self.png_dir / filename
        
        # Get actual bounds from dataset
        try:
            from pyproj import Transformer
            proj_attrs = dataset["goes_imager_projection"].attrs
            sat_lon = proj_attrs.get("longitude_of_projection_origin", -75.2)
            sat_height = proj_attrs.get("perspective_point_height", 35786023.0)
            sweep = proj_attrs.get("sweep_angle_axis", "x")
            
            x_rad = dataset["x"].values
            y_rad = dataset["y"].values
            x_m = x_rad * sat_height
            y_m = y_rad * sat_height
            x_grid, y_grid = np.meshgrid(x_m, y_m)
            
            sweep_param = f"+sweep={sweep}"
            proj_string = (
                f"+proj=geos +h={sat_height} +lon_0={sat_lon} "
                f"{sweep_param} +ellps=WGS84 +units=m +no_defs"
            )
            transformer = Transformer.from_crs(proj_string, "EPSG:4326", always_xy=True)
            lon_grid, lat_grid = transformer.transform(x_grid, y_grid)
            
            bounds = {
                "west": float(lon_grid.min()),
                "east": float(lon_grid.max()),
                "south": float(lat_grid.min()),
                "north": float(lat_grid.max()),
            }
        except:
            bounds = {"west": -100, "east": -80, "south": 30, "north": 45}
        
        # Render WITHOUT USA mask - full sector view
        h, w = data.shape
        
        # Create RGBA array
        norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
        cmap_obj = plt.get_cmap(cmap) if isinstance(cmap, str) else cmap
        cmap_obj = cmap_obj.copy()
        cmap_obj.set_bad((0, 0, 0, 0))
        
        # Mask NaN values for transparency
        valid_mask = ~np.isnan(data)
        data_filled = np.where(valid_mask, data, vmin)
        
        rgba = cmap_obj(norm(data_filled), bytes=True)
        rgba[~valid_mask, 3] = 0  # Transparent for NaN

        # Clean inner-edge feather — no fill outside domain
        if np.any(~valid_mask):
            edge_dist = distance_transform_edt(valid_mask).astype(np.float32)
            feather_px = max(12, int(min(h, w) * 0.06))
            edge_fade = np.clip(edge_dist / feather_px, 0, 1)
            edge_fade = edge_fade * edge_fade * (3.0 - 2.0 * edge_fade)
            rgba[:, :, 3] = (rgba[:, :, 3].astype(np.float32) * edge_fade).astype(np.uint8)
        
        png_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        Image.fromarray(rgba, 'RGBA').save(str(png_path), compress_level=1)
        
        meta = {
            "satellite": dataset.attrs.get("platform_ID", "GOES"),
            "product": product_type,
            "band": band_num,
            "resolution": "1km mesoscale",
            "mesoscale": True,
            "timestamp": self._get_timestamp(dataset),
            "bounds": bounds,
            "png_path": str(png_path.relative_to(self.output_dir.parent)),
        }
        meta_path = self.meta_dir / filename.replace(".png", ".json")
        write_frame_meta(meta_path, meta)
        
        return RenderedSatellite(
            png_path=png_path,
            bounds=bounds,
            timestamp=meta["timestamp"],
            satellite=meta["satellite"],
            product=product_type,
            band=band_num,
        )

    @_serialize_regrid
    def render_to_mercator_cmip(
        self,
        data: np.ndarray,
        dataset: xr.Dataset,
        output_path: Path,
        cmap: str = 'Greys_r',
        vmin: float = None,
        vmax: float = None,
        gamma: float = 1.0,
        tone_lift: float = 0.0,
        crop_bounds: dict | None = None,
        target_w: int | None = None,
    ) -> dict:
        """Render CMIP data to Web Mercator at product-specific resolution.

        Uses the same cached geostationary-to-Mercator regrid map as the
        standard satellite path, just at a larger target size.
        """
        from PIL import Image

        try:
            proj_attrs = dataset["goes_imager_projection"].attrs
            sat_lon = proj_attrs.get("longitude_of_projection_origin", -75.2)
            sat_height = proj_attrs.get("perspective_point_height", 35786023.0)
            sweep = proj_attrs.get("sweep_angle_axis", "x")
        except Exception:
            sat_lon = -75.2
            sat_height = 35786023.0
            sweep = "x"

        base_bounds = {
            "west": DATA_WEST,
            "east": DATA_EAST,
            "south": DATA_SOUTH,
            "north": DATA_NORTH,
        }
        render_bounds = self._resolve_render_bounds(base_bounds, crop_bounds)
        if target_w is None:
            target_w = 2800 if crop_bounds else 2400
        target_w = max(int(target_w), 1)
        merc_south = _lat_to_merc(render_bounds["south"])
        merc_north = _lat_to_merc(render_bounds["north"])
        target_h = int(
            target_w * (merc_north - merc_south) / (render_bounds["east"] - render_bounds["west"])
        )
        target_h = max(target_h, 1)

        sat_lon_r = round(sat_lon, 1)
        regrid_key = (
            data.shape,
            sat_lon_r,
            target_w,
            target_h,
            round(render_bounds["west"], 3),
            round(render_bounds["east"], 3),
            round(render_bounds["south"], 3),
            round(render_bounds["north"], 3),
        )

        if _regrid_cache_get(regrid_key) is None:
            tx_key = ("ll_to_geos", sat_lon_r, int(sat_height), sweep)
            if tx_key not in _transformer_cache:
                proj_string = (
                    f"+proj=geos +h={sat_height} +lon_0={sat_lon}"
                    f" +sweep={sweep} +ellps=WGS84 +units=m +no_defs"
                )
                _transformer_cache[tx_key] = Transformer.from_crs(
                    "EPSG:4326", proj_string, always_xy=True
                )
                _prune_cache(_transformer_cache, _TRANSFORMER_CACHE_MAX)
                logger.info("Built inverse GOES transformer for lon=%.1f h=%d sweep=%s",
                            sat_lon, int(sat_height), sweep)
            transformer = _transformer_cache[tx_key]

            try:
                x_rad = np.asarray(dataset["x"].values, dtype=np.float64)
                y_rad = np.asarray(dataset["y"].values, dtype=np.float64)
            except Exception:
                logger.warning("CMIP dataset missing x/y coordinates")
                return render_bounds
            if y_rad.size != data.shape[0] or x_rad.size != data.shape[1]:
                logger.warning(
                    "CMIP x/y coordinate shape mismatch data=%s x=%d y=%d",
                    data.shape, x_rad.size, y_rad.size,
                )
                return render_bounds

            lon_target = np.linspace(render_bounds["west"], render_bounds["east"], target_w)
            merc_target = np.linspace(merc_north, merc_south, target_h)
            lat_target = np.degrees(
                2 * np.arctan(np.exp(np.radians(merc_target))) - np.pi / 2
            )
            lon_grid_target, lat_grid_target = np.meshgrid(lon_target, lat_target)

            x_target_m, y_target_m = transformer.transform(lon_grid_target, lat_grid_target)
            x_target = x_target_m / sat_height
            y_target = y_target_m / sat_height

            def nearest_axis_indices(axis: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
                axis_work = axis
                reversed_axis = bool(axis_work[0] > axis_work[-1])
                if reversed_axis:
                    axis_work = axis_work[::-1]
                valid = (
                    np.isfinite(values)
                    & (values >= axis_work[0])
                    & (values <= axis_work[-1])
                )
                idx_hi = np.searchsorted(axis_work, values, side="left")
                idx_hi = np.clip(idx_hi, 0, axis_work.size - 1)
                idx_lo = np.clip(idx_hi - 1, 0, axis_work.size - 1)
                choose_hi = (
                    np.abs(axis_work[idx_hi] - values)
                    < np.abs(values - axis_work[idx_lo])
                )
                idx = np.where(choose_hi, idx_hi, idx_lo)
                if reversed_axis:
                    idx = axis_work.size - 1 - idx
                return idx.astype(np.int32), valid

            col_idx, valid_x = nearest_axis_indices(x_rad, x_target)
            row_idx, valid_y = nearest_axis_indices(y_rad, y_target)
            valid_matches = valid_x & valid_y
            flat_indices = (row_idx.astype(np.int64) * data.shape[1] + col_idx.astype(np.int64))
            _regrid_cache_put(regrid_key, flat_indices.astype(np.int32), valid_matches)
            logger.info(
                "Built CMIP regrid cache shape=%s → %dx%d (%d/%d valid)",
                data.shape, target_w, target_h,
                int(valid_matches.sum()), target_w * target_h,
            )

        flat_indices, valid_matches = _regrid_cache_get(regrid_key)

        data_flat = data.ravel().astype(np.float32)
        finite_mask = np.isfinite(data_flat)
        if int(finite_mask.sum()) < 100:
            logger.warning("Not enough valid CMIP data points")
            blank = np.zeros((target_h, target_w, 4), dtype=np.uint8)
            output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            Image.fromarray(blank, "RGBA").save(str(output_path), compress_level=1)
            return render_bounds

        data_reprojected = np.full(target_h * target_w, np.nan, dtype=np.float32)
        valid_matches_flat = valid_matches.ravel()
        flat_indices_flat = flat_indices.ravel()
        valid_output = valid_matches_flat.copy()
        valid_output[valid_matches_flat] = finite_mask[flat_indices_flat[valid_matches_flat]]
        data_reprojected[valid_output] = data_flat[flat_indices_flat[valid_output]]
        data_reprojected = data_reprojected.reshape((target_h, target_w))

        if crop_bounds:
            render_data = data_reprojected
            bounds = render_bounds
        else:
            render_data = data_reprojected
            bounds = base_bounds

        self._render_reprojected(
            render_data,
            None,
            output_path,
            cmap,
            vmin,
            vmax,
            gamma=gamma,
            tone_lift=tone_lift,
            bounds=bounds,
        )

        logger.debug("Rendered CMIP to Mercator (%dpx): %s", target_w, output_path)
        
        return bounds

    def render_l2_product(
        self,
        dataset: xr.Dataset,
        product_type: str,
        filename: str = None,
    ) -> "RenderedSatellite":
        """Render a generic GOES L2 full-disk product.

        Extracts the primary variable as defined in L2_PRODUCT_CONFIG,
        optionally converts K→°C, then reprojects to Mercator via the
        standard KD-tree pipeline.

        For the 'fire' product, only pixels with Temp > 350 K are shown;
        background / non-fire values are NaN and become transparent.
        """
        cfg = L2_PRODUCT_CONFIG.get(product_type)
        if cfg is None:
            raise ValueError(f"Unknown L2 product type: {product_type}")

        var_name = cfg["var"]
        if var_name not in dataset:
            raise ValueError(f"Variable '{var_name}' not found in dataset for {product_type}")

        raw = dataset[var_name].values.astype(np.float32)
        raw = self._mask_l2_quality(raw, dataset, cfg)

        if cfg.get("celsius", False):
            raw = raw - 273.15

        # For fire: mask background (non-fire) pixels
        if product_type == "fire":
            threshold = 350.0  # K; keep only fire-detected pixels
            # If celsius conversion was done (it isn't for fire), adjust threshold
            raw[raw < threshold] = np.nan

        min_visible = cfg.get("min_visible")
        if min_visible is not None:
            raw[raw < float(min_visible)] = np.nan

        # L2 Full Disk products use a geostationary projection but often lack
        # goes_imager_projection — we handle that gracefully in render_to_mercator
        # via its try/except which falls back to GOES-19 defaults.
        if filename is None:
            timestamp = self._get_timestamp(dataset)
            filename = f"goes_{product_type}_{timestamp}.png"

        png_path = self.png_dir / filename

        bounds = self._render_l2_to_mercator(
            raw,
            dataset,
            png_path,
            cmap=cfg["cmap"],
            vmin=cfg["vmin"],
            vmax=cfg["vmax"],
            extend_masked=cfg.get("extend_masked", True),
        )

        meta = {
            "satellite": dataset.attrs.get("platform_ID", "GOES"),
            "product": product_type,
            "timestamp": self._get_timestamp(dataset),
            "bounds": bounds,
            "png_path": str(png_path.relative_to(self.output_dir.parent)),
        }
        meta_path = self.meta_dir / filename.replace(".png", ".json")
        write_frame_meta(meta_path, meta)

        return RenderedSatellite(
            png_path=png_path,
            bounds=bounds,
            timestamp=meta["timestamp"],
            satellite=meta["satellite"],
            product=product_type,
            band=None,
        )

    def _render_l2_to_mercator(
        self,
        data: np.ndarray,
        dataset: xr.Dataset,
        output_path: Path,
        cmap: str = "Greys_r",
        vmin: float = None,
        vmax: float = None,
        extend_masked: bool = False,
        gamma: float = 1.0,
    ) -> dict:
        """Reproject an L2 full-disk data array to Web Mercator.

        L2 products may not have goes_imager_projection in the same way as
        MCMIP; we fall back to GOES-19 geostationary defaults when needed.
        """
        from scipy.spatial import cKDTree
        from pyproj import Transformer

        # Try standard projection variable; fall back to GOES-19 defaults
        try:
            proj_attrs = dataset["goes_imager_projection"].attrs
            sat_lon = proj_attrs.get("longitude_of_projection_origin", -75.2)
            sat_height = proj_attrs.get("perspective_point_height", 35786023.0)
            sweep = proj_attrs.get("sweep_angle_axis", "x")
        except Exception:
            sat_lon = -75.2
            sat_height = 35786023.0
            sweep = "x"

        try:
            x_rad = dataset["x"].values
            y_rad = dataset["y"].values
        except Exception:
            logger.warning("L2 dataset missing x/y coordinates; skipping render")
            return {"west": DATA_WEST, "east": DATA_EAST,
                    "south": DATA_SOUTH, "north": DATA_NORTH}

        x_m = x_rad * sat_height
        y_m = y_rad * sat_height
        x_grid, y_grid = np.meshgrid(x_m, y_m)

        sweep_param = f"+sweep={sweep}"
        proj_string = (
            f"+proj=geos +h={sat_height} +lon_0={sat_lon} "
            f"{sweep_param} +ellps=WGS84 +units=m +no_defs"
        )
        transformer = Transformer.from_crs(proj_string, "EPSG:4326", always_xy=True)
        lon_src, lat_src = transformer.transform(x_grid, y_grid)

        _DATA_SOUTH, _DATA_NORTH = 15.0, 52.0
        _DATA_WEST, _DATA_EAST = -145.0, -50.0

        def _lat_to_merc(lat):
            lat_rad = np.radians(np.clip(lat, -85, 85))
            return np.degrees(np.log(np.tan(np.pi / 4 + lat_rad / 2)))

        _MERC_SOUTH = float(_lat_to_merc(_DATA_SOUTH))
        _MERC_NORTH = float(_lat_to_merc(_DATA_NORTH))

        target_w = 1200
        target_h = int(target_w * (_MERC_NORTH - _MERC_SOUTH) / (_DATA_EAST - _DATA_WEST))

        lon_target = np.linspace(_DATA_WEST, _DATA_EAST, target_w)
        merc_target = np.linspace(_MERC_NORTH, _MERC_SOUTH, target_h)
        lat_target = np.degrees(2 * np.arctan(np.exp(np.radians(merc_target))) - np.pi / 2)
        lon_grid_t, lat_grid_t = np.meshgrid(lon_target, lat_target)

        valid_mask = ~np.isnan(data)
        valid_points = valid_mask.ravel()
        if valid_points.sum() == 0:
            logger.warning("Not enough valid data points for L2 reprojection (%s)", output_path.name)
            # Produce a blank transparent image so the pipeline continues
            from PIL import Image
            blank = np.zeros((target_h, target_w, 4), dtype=np.uint8)
            output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            Image.fromarray(blank, "RGBA").save(str(output_path), compress_level=1)
            return {"west": _DATA_WEST, "east": _DATA_EAST,
                    "south": _DATA_SOUTH, "north": _DATA_NORTH}

        src_coords = np.column_stack([
            lon_src.ravel()[valid_points],
            lat_src.ravel()[valid_points],
        ])
        src_values = data.ravel()[valid_points]

        tree = cKDTree(src_coords)
        target_coords = np.column_stack([lon_grid_t.ravel(), lat_grid_t.ravel()])
        distances, indices = tree.query(target_coords, k=1, distance_upper_bound=0.15)

        data_reprojected = np.full(target_h * target_w, np.nan)
        valid_matches = distances < np.inf
        data_reprojected[valid_matches] = src_values[indices[valid_matches]]
        data_reprojected = data_reprojected.reshape((target_h, target_w))

        usa_mask = self._create_usa_mask_from_shapefile(target_w, target_h)
        self._render_reprojected(
            data_reprojected,
            usa_mask,
            output_path,
            cmap,
            vmin,
            vmax,
            extend_masked=extend_masked,
            gamma=gamma,
        )

        return {
            "west": _DATA_WEST,
            "east": _DATA_EAST,
            "south": _DATA_SOUTH,
            "north": _DATA_NORTH,
        }

    def _mask_l2_quality(
        self,
        data: np.ndarray,
        dataset: xr.Dataset,
        cfg: dict,
    ) -> np.ndarray:
        """Mask invalid L2 retrieval pixels using dataset quality flags."""
        masked = data.copy()

        max_dqf = cfg.get("max_dqf")
        dqf = dataset.get("DQF")
        if max_dqf is not None and dqf is not None:
            masked[np.asarray(dqf.values) > int(max_dqf)] = np.nan

        return masked

class LightningOverlayRenderer:
    """Render GLM lightning data as point overlays."""

    BUCKET_MINUTES = 5  # accumulate strikes into 5-minute buckets (aligns with radar frames)

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir = self.output_dir / "glm_archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _floor_to_bucket(dt: datetime, minutes: int = 5) -> datetime:
        """Floor a datetime to the nearest N-minute bucket."""
        floored_min = (dt.minute // minutes) * minutes
        return dt.replace(minute=floored_min, second=0, microsecond=0)

    @staticmethod
    def _bucket_key(dt: datetime) -> str:
        """Return bucket filename key: YYYYMMDDHHNN."""
        return dt.strftime("%Y%m%d%H%M")

    def _extract_strikes(self, dataset_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
        """Open a GLM NetCDF and return (lats, lons, energies, iso_timestamp)."""
        with xr.open_dataset(dataset_path) as ds:
            flash_lat = ds.get("flash_lat") if ds.get("flash_lat") is not None else ds.get("latitude")
            flash_lon = ds.get("flash_lon") if ds.get("flash_lon") is not None else ds.get("longitude")
            flash_energy = ds.get("flash_energy") if ds.get("flash_energy") is not None else ds.get("energy")

            if flash_lat is None or flash_lon is None:
                raise ValueError("GLM dataset missing flash_lat/flash_lon")

            lat_vals = np.asarray(flash_lat.values, dtype=np.float32)
            lon_vals = np.asarray(flash_lon.values, dtype=np.float32)
            valid = np.isfinite(lat_vals) & np.isfinite(lon_vals)

            if flash_energy is not None:
                energy_vals = np.asarray(flash_energy.values, dtype=np.float32)
                valid &= np.isfinite(energy_vals)
            else:
                energy_vals = np.ones_like(lat_vals)

            ts_raw = (
                ds.attrs.get("time_coverage_start")
                or ds.attrs.get("start_date_time")
                or ds.attrs.get("date_created")
            )
            timestamp = str(ts_raw).replace(":", "-") if ts_raw else datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ")

        return lat_vals[valid], lon_vals[valid], energy_vals[valid], timestamp

    def _make_features(self, lats, lons, energies) -> list:
        return [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                "properties": {"energy": float(energy)},
            }
            for lat, lon, energy in zip(lats, lons, energies)
        ]

    def render(self, dataset_path: str | Path, source_id: str = "goes19") -> dict | None:
        """Render a GLM NetCDF into latest GeoJSON + 5-min bucket archive file."""
        dataset_path = Path(dataset_path)
        if not dataset_path.exists():
            raise FileNotFoundError(f"GLM dataset not found: {dataset_path}")

        try:
            lats, lons, energies, timestamp = self._extract_strikes(dataset_path)
            features = self._make_features(lats, lons, energies)

            geojson_data = {
                "type": "FeatureCollection",
                "features": features,
                "count": len(features),
                "satellite": source_id.upper(),
                "timestamp": timestamp,
            }

            # Write "latest" file (always overwrite)
            latest_path = self.output_dir / f"lightning_{source_id}_glm_latest.json"
            latest_path.write_text(json.dumps(geojson_data))

            meta = {
                "satellite": source_id.upper(),
                "product": "glm",
                "timestamp": timestamp,
                "geojson_path": str(latest_path),
                "count": len(features),
            }
            (self.output_dir / f"{source_id}_glm_latest.json").write_text(json.dumps(meta, indent=2))

            # Accumulate into 5-min bucket archive file
            self._accumulate_bucket(source_id, timestamp, features)

            return meta
        except Exception:
            logger.exception("Failed to render GLM lightning from %s", dataset_path)
            raise

    def _accumulate_bucket(self, source_id: str, timestamp: str, new_features: list):
        """Append new strikes to the appropriate 5-min bucket file."""
        try:
            # Parse timestamp (format: "2026-03-14T01-30-00.0Z")
            normalized = timestamp.replace("T", " ").rstrip("Z")
            # Replace dashes-in-time back to colons: "2026-03-14 01-30-00.0" -> "2026-03-14 01:30:00"
            parts = normalized.split(" ")
            if len(parts) == 2:
                time_part = parts[1].replace("-", ":", 2).split(".")[0]
                dt = datetime.strptime(f"{parts[0]} {time_part}", "%Y-%m-%d %H:%M:%S")
            else:
                dt = datetime.utcnow()

            bucket_dt = self._floor_to_bucket(dt, self.BUCKET_MINUTES)
            bucket_key = self._bucket_key(bucket_dt)
            bucket_end = bucket_dt + timedelta(minutes=self.BUCKET_MINUTES)
            bucket_file = self.archive_dir / f"{source_id}_glm_{bucket_key}.json"

            if bucket_file.exists():
                existing = json.loads(bucket_file.read_text())
                all_features = existing.get("features", []) + new_features
            else:
                all_features = new_features

            bucket_data = {
                "type": "FeatureCollection",
                "features": all_features,
                "count": len(all_features),
                "satellite": source_id.upper(),
                "bucket_start": bucket_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bucket_end": bucket_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ts": bucket_key,
            }
            bucket_file.write_text(json.dumps(bucket_data))
        except Exception:
            logger.exception("Failed to write GLM bucket file")

    def cleanup_archive(self, keep_hours: int = 3):
        """Remove GLM bucket files older than keep_hours."""
        cutoff = datetime.utcnow().timestamp() - keep_hours * 3600
        removed = 0
        for f in self.archive_dir.glob("*_glm_*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.info("GLM archive cleanup: removed %d old bucket files", removed)

    def render_png_overlay(
        self,
        lightning_data: dict,
        bounds: dict,
        width: int = 2000,
        height: int = 1500
    ) -> np.ndarray:
        """Render lightning as a transparent PNG overlay.
        
        Creates a heatmap-style overlay that can be composited on the map.
        """
        # Create empty canvas
        canvas = np.zeros((height, width, 4), dtype=np.uint8)
        
        if lightning_data['count'] == 0:
            return canvas
        
        # Map lat/lon to pixel coordinates
        lons = lightning_data['lon']
        lats = lightning_data['lat']
        energies = lightning_data['energy']
        
        lon_range = bounds['east'] - bounds['west']
        lat_range = bounds['north'] - bounds['south']
        
        x = ((lons - bounds['west']) / lon_range * width).astype(int)
        y = ((bounds['north'] - lats) / lat_range * height).astype(int)
        
        # Clip to bounds
        valid = (x >= 0) & (x < width) & (y >= 0) & (y < height)
        x, y, energies = x[valid], y[valid], energies[valid]
        
        # Draw flashes as bright yellow/orange points with glow
        for xi, yi, energy in zip(x, y, energies):
            intensity = min(255, int(100 + energy / 100))
            
            # Draw point (with bounds checking)
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    py, px = yi + dy, xi + dx
                    if 0 <= py < height and 0 <= px < width:
                        dist = np.sqrt(dx**2 + dy**2)
                        if dist < 1:
                            # Core - bright white/yellow
                            canvas[py, px] = [255, 255, 200, 255]
                        elif dist < 2.5:
                            # Glow - yellow/orange
                            alpha = int(200 * (1 - dist/2.5))
                            canvas[py, px] = [255, 200, 100, alpha]
        
        return canvas


# (re and datetime imported at top of file)
