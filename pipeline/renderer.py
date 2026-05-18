"""Overlay renderer — produces data-only PNG overlays for Leaflet.

V3: Native-resolution rendering. Geographic features (boundaries, roads,
    mask, labels) are handled by Leaflet tile layers instead of being
    baked into the PNG. Output is RGBA with transparency outside the
    data domain.

Backup of V2 (pre-rasterized overlay): renderer_v2_overlay.py
"""
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

import cartopy.io.shapereader as shpreader
import matplotlib
import matplotlib.colors as mcolors
import matplotlib.path as mpath
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap
from PIL import Image, ImageFilter
from PIL import ImageDraw
from scipy.ndimage import (
    distance_transform_edt,
    gaussian_filter,
    maximum_filter,
    minimum_filter,
)
from scipy.spatial import cKDTree

from .config import COLOR_SCALES, RENDER_DPI

# ---------------------------------------------------------------------------
# Disk-persisted regrid cache directory
# ---------------------------------------------------------------------------
_REGRID_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "regrid_cache"
_REGRID_CACHE_DIR.mkdir(parents=True, exist_ok=True)

matplotlib.use("Agg")

logger = logging.getLogger(__name__)
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
_REGRID_LOCK_WAIT_SECONDS = 120

_PRESSURE_WIND_PARAMS = {
    "wind_925mb",
    "wind_850mb",
    "wind_700mb",
    "wind_500mb",
    "wind_300mb",
    "wind_250mb",
    "wind_200mb",
}

_HEIGHT_CONTOUR_CONFIG = {
    # Heights are contoured in meters.
    # 250/500 mb: every 60 m
    # 700/850 mb: every 30 m
    "gh_925mb": {"interval": 15, "label_every": 30, "smooth_sigma": 0.7},
    "gh_250mb": {"interval": 60, "label_every": 60, "smooth_sigma": 0.8, "label_fontsize": 6.2},
    "gh_300mb": {"interval": 60, "label_every": 120, "smooth_sigma": 0.8, "label_fontsize": 6.0},
    "gh_200mb": {"interval": 120, "label_every": 240, "smooth_sigma": 0.9, "label_fontsize": 5.8},
    "gh_500mb": {"interval": 60, "label_every": 60, "smooth_sigma": 0.8, "label_fontsize": 6.1},
    "gh_700mb": {"interval": 30, "label_every": 30, "smooth_sigma": 0.7},
    "gh_850mb": {"interval": 30, "label_every": 30, "smooth_sigma": 0.7},
}

_HEIGHT_CONTOUR_PARAMS = set(_HEIGHT_CONTOUR_CONFIG)

_SCALAR_HEIGHT_OVERLAYS = {
    "temp_925mb": "gh_925mb",
    "rh_925mb": "gh_925mb",
    "wind_925mb": "gh_925mb",
    "temp_850mb": "gh_850mb",
    "dewpoint_850mb": "gh_850mb",
    "rh_850mb": "gh_850mb",
    "wind_850mb": "gh_850mb",
    "temp_700mb": "gh_700mb",
    "rh_700mb": "gh_700mb",
    "omega_700mb": "gh_700mb",
    "wind_700mb": "gh_700mb",
    "temp_500mb": "gh_500mb",
    "vort_500mb": "gh_500mb",
    "wind_500mb": "gh_500mb",
    "wind_300mb": "gh_300mb",
    "wind_250mb": "gh_250mb",
    "wind_200mb": "gh_200mb",
}

# ---------------------------------------------------------------------------
# Mercator helpers
# ---------------------------------------------------------------------------

def _lat_to_merc(lat):
    """Convert latitude (degrees) to Web Mercator Y (degrees)."""
    lat_rad = np.radians(np.clip(lat, -85, 85))
    return np.degrees(np.log(np.tan(np.pi / 4 + lat_rad / 2)))


# ---------------------------------------------------------------------------
# Domain bounds — CONUS extent (matches HRRR coverage)
# Leaflet EPSG:3857 stretches ImageOverlay linearly in Mercator pixel space,
# so the pixel grid must use Mercator Y-spacing for correct alignment.
# ---------------------------------------------------------------------------

DATA_SOUTH, DATA_NORTH = 15.0, 58.0
DATA_WEST, DATA_EAST = -145.0, -50.0

_FIXED_CONUS_BOUNDS_MODELS = {
    "hrrr",
    "rap",
    "nam",
    "nam12km",
    "nam3km",
    "gfs",
    "gefs",
    "gdps",
    "ecmwf",
    "ifs",
    "rtma",
    "href",
    "rrfs",
}

_NO_OUTSIDE_DIFFUSION_MODELS = {
    "href",
}

MERC_SOUTH = float(_lat_to_merc(DATA_SOUTH))
MERC_NORTH = float(_lat_to_merc(DATA_NORTH))

# Minimum output width for matplotlib params (contours/barbs need decent res)
_MPL_MIN_WIDTH = 1800

_SPARSE_OVERLAY_ALPHA = {
}

# Params that should skip outside-domain color diffusion — they render
# opaque data on a black backing layer so edge bleed looks wrong.
_NO_DIFFUSION_PARAMS = {
    "reflectivity", "precip",
    "snow_depth", "snowfall", "snowfall_6h", "weasd",
    "kuchera_6hr", "kuchera_12hr", "snow10_12hr",
    "lightning", "echo_tops", "hail",
    "uh_02km", "uh_03km", "uh_25km", "uh_25km_swath", "uh_03km_swath",
    "cti", "msi", "ship", "dcp", "citi", "cvi", "llmt", "ascent", "hp_risk",
    "refl_probability_40dbz", "uh_25km_probability",
    "precip_probability", "precip_probability_6h_05in", "precip_probability_6h_1in",
    "visibility_probability",
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "wind_speed",
    "wind_925mb", "wind_850mb", "wind_700mb", "wind_500mb",
    "wind_300mb", "wind_250mb", "wind_200mb",
}


def _path_from_geometry(geom, label):
    """Convert polygon geometry into a matplotlib Path."""
    try:
        all_verts = []
        all_codes = []
        polys = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
        for poly in polys:
            coords = list(poly.exterior.coords)
            if len(coords) < 3:
                continue
            all_verts.extend(coords)
            all_codes.extend(
                [mpath.Path.MOVETO]
                + [mpath.Path.LINETO] * (len(coords) - 2)
                + [mpath.Path.CLOSEPOLY]
            )
        if all_verts:
            logger.info("Loaded %s: %d polygons", label, len(polys))
            return mpath.Path(all_verts, all_codes)
    except Exception as exc:
        logger.warning("Failed to load %s: %s", label, exc)
    return None


def _build_usa_path():
    """Load USA border as a matplotlib Path for point-in-polygon testing."""
    try:
        shpfile = shpreader.natural_earth("50m", "cultural", "admin_0_countries")
        reader = shpreader.Reader(shpfile)
        for rec in reader.records():
            if rec.attributes.get("ADMIN") == "United States of America":
                return _path_from_geometry(rec.geometry, "USA border")
    except Exception as exc:
        logger.warning("Failed to load USA border: %s", exc)
    return None


_BOUNDARY_GEOJSON_CACHE: dict[str, dict] = {}


def _load_boundary_geojson(name: str) -> dict | None:
    """Load a cached boundary GeoJSON file from web/static."""
    if name in _BOUNDARY_GEOJSON_CACHE:
        return _BOUNDARY_GEOJSON_CACHE[name]
    path = _STATIC_DIR / name
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        logger.warning("Failed to load boundary asset %s: %s", name, exc)
        return None
    _BOUNDARY_GEOJSON_CACHE[name] = data
    return data


def _project_boundary_point(coord: list[float], bounds: tuple[float, float, float, float], width: int, height: int) -> tuple[float, float]:
    """Project a lon/lat point onto the rendered image in Mercator Y-space."""
    south, north, west, east = bounds
    merc_south = float(_lat_to_merc(south))
    merc_north = float(_lat_to_merc(north))
    lon, lat = float(coord[0]), float(coord[1])
    x = ((lon - west) / max(east - west, 1e-6)) * width
    y = ((merc_north - float(_lat_to_merc(lat))) / max(merc_north - merc_south, 1e-6)) * height
    return x, y


def _boundary_geometry_parts(geometry: dict) -> list[list[list[float]]]:
    """Flatten common GeoJSON geometry types into coordinate parts."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "LineString":
        return [coords]
    if gtype == "MultiLineString":
        return list(coords)
    if gtype == "Polygon":
        return list(coords)
    if gtype == "MultiPolygon":
        return [ring for poly in coords for ring in poly]
    return []


# Load once at module level
_USA_PATH = _build_usa_path()


# ---------------------------------------------------------------------------
# NWS Composite Reflectivity colormap
# ---------------------------------------------------------------------------

_NWS_REFL_BOUNDS = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75]
_NWS_REFL_COLORS = [
    "#66d9ff", "#0099cc", "#00cc00", "#009900", "#ffff00", "#ffcc00",
    "#ff9900", "#ff0000", "#cc0000", "#990000", "#ff00ff", "#cc00cc",
    "#990099", "#ffffff",
]
_NWS_REFL_CMAP = ListedColormap(_NWS_REFL_COLORS)
_NWS_REFL_NORM = BoundaryNorm(_NWS_REFL_BOUNDS, _NWS_REFL_CMAP.N)

# Params that need matplotlib (vector drawing: contours, wind analysis)
_MPL_PARAMS = {"mslp", "psfc", "wind_speed"}


# Module-level colormap/norm cache — shared across all OverlayRenderer instances.
# Safe because each entry is a fully-configured (cmap_copy, norm) pair that is
# never mutated after insertion.
_cmap_norm_cache: dict = {}
_CMAP_CACHE_MAX = 60  # More than enough for all params

_REGRID_CACHE_MAX = 10  # Cap per-instance regrid caches
_TARGET_GRID_CACHE_MAX = 5


class OverlayRenderer:
    """Renders weather data as RGBA PNG overlays at native model resolution.

    Fill params use numpy colormap + PIL (fast path).
    MSLP and wind use matplotlib (vector drawing needed).
    Geographic features (boundaries, roads, labels) handled by Leaflet.
    """

    def __init__(self):
        # Regrid caches: keyed by (source_shape, bounds, target_w, target_h)
        self._regrid_cache = {}
        # Target grids: keyed by (bounds, target_w, target_h) -> {lon, lat, masks}
        self._target_grids = {}
        # EDT cache: keyed by (grid_h, grid_w, nan_mask_hash) -> {distances, outside_mask}
        self._edt_cache = {}
        self._current_model_name = ""
        self._last_render_bounds: tuple[float, float, float, float] | None = None

    def clear_caches(self):
        """Release cached regrid indices and target grids."""
        self._regrid_cache.clear()
        self._target_grids.clear()
        self._edt_cache.clear()

    def _draw_boundary_lines(self, image: Image.Image, bounds: tuple[float, float, float, float]) -> Image.Image:
        """Overlay state and coastline lines onto a rendered RGBA image."""
        draw = ImageDraw.Draw(image, "RGBA")
        size = image.size
        layers = [
            ("boundaries_states.json", (56, 56, 56, 180), 1),
            ("boundaries_coastline.json", (5, 8, 13, 110), 3),
            ("boundaries_coastline.json", (243, 246, 251, 195), 1),
        ]
        for asset_name, color, width_px in layers:
            geojson = _load_boundary_geojson(asset_name)
            if not geojson:
                continue
            for feature in geojson.get("features", []):
                for part in _boundary_geometry_parts(feature.get("geometry", {})):
                    if len(part) < 2:
                        continue
                    pts = [_project_boundary_point(coord, bounds, *size) for coord in part]
                    draw.line(pts, fill=color, width=width_px, joint="curve")
        return image

    # ------------------------------------------------------------------
    # Target grid management
    # ------------------------------------------------------------------

    def _get_target_grid(self, bounds, w, h):
        """Get or create target grid arrays for given output dimensions.

        Uses Mercator Y-spacing to match Leaflet's EPSG:3857 projection,
        which stretches ImageOverlay linearly in Mercator pixel space.
        """
        south, north, west, east = bounds
        merc_south = float(_lat_to_merc(south))
        merc_north = float(_lat_to_merc(north))
        key = (bounds, w, h)
        if key not in self._target_grids:
            lon_1d = np.linspace(west, east, w, dtype=np.float32)
            # Mercator Y-spacing: linear in Mercator, convert back to geographic
            merc_1d = np.linspace(merc_north, merc_south, h, dtype=np.float32)
            lat_1d = np.degrees(2 * np.arctan(np.exp(np.radians(merc_1d))) - np.pi / 2).astype(np.float32)
            target_lon, target_lat = np.meshgrid(lon_1d, lat_1d)

            # USA border mask — True for pixels OUTSIDE the USA
            usa_outside = np.ones((h, w), dtype=bool)
            if _USA_PATH is not None:
                pts = np.column_stack([target_lon.ravel(), target_lat.ravel()])
                inside = _USA_PATH.contains_points(pts).reshape(h, w)
                usa_outside = ~inside
                logger.info("USA mask for %dx%d: %d/%d pixels inside",
                            w, h, inside.sum(), inside.size)

            self._target_grids[key] = {
                'lon': target_lon,
                'lat': target_lat,
                'usa_outside': usa_outside,
            }
            # Cap target grid cache size
            while len(self._target_grids) > 5:
                self._target_grids.pop(next(iter(self._target_grids)))

        return self._target_grids[key]

    def _estimate_resolution(self, lon, lat, data, bounds):
        """Estimate output dimensions matching native data resolution."""
        south, north, west, east = bounds
        merc_w = east - west
        merc_h = float(_lat_to_merc(north) - _lat_to_merc(south))

        if lon.ndim == 1:
            # Regular grid (GFS, ECMWF)
            dlon = abs(float(lon[1] - lon[0])) if len(lon) > 1 else 0.25
            w = int(merc_w / dlon)
            h = int(w * merc_h / merc_w)
        else:
            # Irregular grid (HRRR Lambert Conformal)
            data_w = data.shape[-1]
            w = data_w
            h = int(data_w * merc_h / merc_w)

        w = max(200, min(w, 2400))
        h = max(150, min(h, 1600))
        return w, h

    # ------------------------------------------------------------------
    # Regridding (cached KDTree for irregular, nearest-neighbor for regular)
    # ------------------------------------------------------------------

    def _regrid(self, lon, lat, data, bounds, target_w, target_h):
        """Regrid data to target dimensions in Web Mercator space."""
        while data.ndim > 2 and data.shape[0] <= 2:
            data = data[0]

        if lon.ndim == 1 and lat.ndim == 1:
            return self._regrid_regular(lon, lat, data, bounds, target_w, target_h)
        return self._regrid_irregular(lon, lat, data, bounds, target_w, target_h)

    def _regrid_irregular(self, lon, lat, data, bounds, target_w, target_h):
        """Regrid from 2D irregular grid using cached nearest-neighbor map."""
        if lon.shape != data.shape:
            try:
                lon = np.broadcast_to(lon, data.shape)
                lat = np.broadcast_to(lat, data.shape)
            except ValueError:
                logger.error("Cannot broadcast lat/lon to data shape, skipping")
                return np.full((target_h, target_w), np.nan, dtype=np.float32)

        cache_key = (data.shape, bounds, target_w, target_h)
        if cache_key not in self._regrid_cache:
            self._build_regrid_map(lon, lat, bounds, cache_key, target_w, target_h)

        cached = self._regrid_cache[cache_key]
        indices = cached['indices']
        dist_mask = cached['dist_mask']
        result = data.ravel()[indices].reshape(target_h, target_w)
        result[dist_mask] = np.nan
        return result

    @staticmethod
    def _regrid_disk_key(*parts) -> str:
        """Deterministic filename for a regrid cache entry."""
        raw = "_".join(str(p) for p in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _np_scalar(value, cast=None):
        """Extract a Python scalar from either a NumPy scalar or 1-element array."""
        scalar = np.asarray(value).reshape(-1)[0].item()
        return cast(scalar) if cast is not None else scalar

    def _load_regrid_from_disk(self, cache_key):
        """Try to load a regrid map from disk cache. Returns True on success."""
        disk_key = self._regrid_disk_key(*cache_key)
        path = _REGRID_CACHE_DIR / f"{disk_key}.npz"
        if not path.exists():
            return False
        try:
            data = np.load(path)
            self._regrid_cache[cache_key] = {
                'indices': data['indices'],
                'dist_mask': data['dist_mask'],
            }
            logger.info("Loaded regrid cache from disk: %s (%dx%d)",
                        cache_key[0], cache_key[2], cache_key[3])
            return True
        except Exception as exc:
            logger.warning("Failed to load regrid cache %s: %s", path, exc)
            path.unlink(missing_ok=True)
            return False

    def _save_regrid_to_disk(self, cache_key, indices, dist_mask):
        """Persist a regrid map to disk for reuse across process restarts."""
        disk_key = self._regrid_disk_key(*cache_key)
        path = _REGRID_CACHE_DIR / f"{disk_key}.npz"
        try:
            with tempfile.NamedTemporaryFile(
                dir=_REGRID_CACHE_DIR,
                prefix=f"{disk_key}_",
                suffix=".npz",
                delete=False,
            ) as tmp_f:
                tmp_path = Path(tmp_f.name)
            np.savez(tmp_path, indices=indices, dist_mask=dist_mask)
            tmp_path.replace(path)
            logger.info("Saved regrid cache to disk: %s", path.name)
        except Exception as exc:
            logger.warning("Failed to save regrid cache: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _wait_for_regular_cache(self, cache_key, timeout_s=_REGRID_LOCK_WAIT_SECONDS):
        """Wait for another worker to finish building a regular cache."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._load_regular_from_disk(cache_key):
                return True
            time.sleep(0.25)
        return False

    def _build_regrid_map(self, lon, lat, bounds, cache_key, target_w, target_h):
        """Build nearest-neighbor index map from source grid to target grid.

        Checks disk cache first to avoid expensive KDTree rebuild.
        """
        # Try disk cache first
        if self._load_regrid_from_disk(cache_key):
            return

        logger.info("Building regrid map for %s -> %dx%d (one-time, will persist to disk)",
                    cache_key[0], target_w, target_h)
        # Use float32 for KDTree construction to save memory
        lon_flat = np.where(lon > 180, lon - 360, lon).astype(np.float32).ravel()
        lat_flat = lat.astype(np.float32).ravel()
        source_pts = np.column_stack([lon_flat, lat_flat])

        tg = self._get_target_grid(bounds, target_w, target_h)
        target_pts = np.column_stack([
            tg['lon'].ravel().astype(np.float32), 
            tg['lat'].ravel().astype(np.float32)
        ])

        tree = cKDTree(source_pts)
        dists, indices = tree.query(target_pts)

        # Estimate grid spacing from source data (~0.03° for HRRR)
        # Mask pixels farther than 3x grid spacing — outside data domain
        grid_spacing = np.median(np.diff(lon_flat[:100]))
        max_dist = abs(grid_spacing) * 3
        dist_mask = dists.reshape(target_h, target_w) > max_dist
        logger.info("  Grid spacing ~%.3f°, max dist %.3f°, masked %d/%d pixels",
                    grid_spacing, max_dist, dist_mask.sum(), dist_mask.size)

        indices_i32 = indices.astype(np.int32)
        self._regrid_cache[cache_key] = {
            'indices': indices_i32,
            'dist_mask': dist_mask,
        }

        # Persist to disk
        self._save_regrid_to_disk(cache_key, indices_i32, dist_mask)
        
        # Aggressive cleanup of large locals
        del tree, source_pts, target_pts, dists, indices, lon_flat, lat_flat
        import gc
        gc.collect()

    def _load_regular_from_disk(self, cache_key):
        """Try to load a regular regrid cache from disk. Returns True on success."""
        disk_key = self._regrid_disk_key(*cache_key)
        path = _REGRID_CACHE_DIR / f"reg_{disk_key}.npz"
        if not path.exists():
            return False
        try:
            data = np.load(path)
            self._regrid_cache[cache_key] = {
                'lon_idx': data['lon_idx'],
                'lat_idx': data['lat_idx'],
                'lat_descending': self._np_scalar(data['lat_descending'], bool),
                'lon_wrap': self._np_scalar(data['lon_wrap'], bool),
                'wrap_idx': self._np_scalar(data['wrap_idx'], int),
            }
            logger.info("Loaded regular regrid cache from disk: %s", path.name)
            return True
        except Exception as exc:
            logger.warning("Failed to load regular regrid cache %s: %s", path, exc)
            path.unlink(missing_ok=True)
            return False

    def _build_regular_cache(self, lon_1d, lat_1d, bounds, cache_key, target_w, target_h):
        """Build and cache nearest-neighbor index map for regular lat/lon grid.

        Checks disk cache first.
        """
        if self._load_regular_from_disk(cache_key):
            return

        disk_key = self._regrid_disk_key(*cache_key)
        lock_path = _REGRID_CACHE_DIR / f"reg_{disk_key}.lock"
        lock_fd = None
        try:
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._wait_for_regular_cache(cache_key):
                    return
                try:
                    lock_path.unlink(missing_ok=True)
                    lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    if self._wait_for_regular_cache(cache_key):
                        return
                    logger.warning("Proceeding without regular-cache lock for %s", cache_key)

            if lock_fd is not None and self._load_regular_from_disk(cache_key):
                return

            logger.info("Building regular regrid cache for %s -> %dx%d (one-time, will persist to disk)",
                        cache_key, target_w, target_h)

            tg = self._get_target_grid(bounds, target_w, target_h)

            # Convert lon to -180..180 if needed
            lon_1d = np.where(lon_1d > 180, lon_1d - 360, lon_1d)

            lat_descending = lat_1d[0] > lat_1d[-1]
            lon_wrap = lon_1d[0] > lon_1d[-1]
            wrap_idx = 0

            # Make lat ascending for searchsorted
            if lat_descending:
                lat_1d = lat_1d[::-1]
            if lon_wrap:
                wrap_idx = np.argmin(lon_1d)
                lon_1d = np.roll(lon_1d, -wrap_idx)

            lon_idx_left = np.searchsorted(lon_1d, tg['lon'], side='right') - 1
            lat_idx_left = np.searchsorted(lat_1d, tg['lat'], side='right') - 1

            lon_idx_left = np.clip(lon_idx_left, 0, len(lon_1d) - 2)
            lat_idx_left = np.clip(lat_idx_left, 0, len(lat_1d) - 2)

            lon_idx_right = lon_idx_left + 1
            lat_idx_right = lat_idx_left + 1

            lon_left_dist = np.abs(tg['lon'] - lon_1d[lon_idx_left])
            lon_right_dist = np.abs(lon_1d[lon_idx_right] - tg['lon'])
            lat_left_dist = np.abs(tg['lat'] - lat_1d[lat_idx_left])
            lat_right_dist = np.abs(lat_1d[lat_idx_right] - tg['lat'])

            lon_idx = np.where(lon_right_dist < lon_left_dist, lon_idx_right, lon_idx_left)
            lat_idx = np.where(lat_right_dist < lat_left_dist, lat_idx_right, lat_idx_left)

            lon_idx_i32 = lon_idx.astype(np.int32)
            lat_idx_i32 = lat_idx.astype(np.int32)

            self._regrid_cache[cache_key] = {
                'lon_idx': lon_idx_i32,
                'lat_idx': lat_idx_i32,
                'lat_descending': lat_descending,
                'lon_wrap': lon_wrap,
                'wrap_idx': wrap_idx,
            }

            # Persist to disk
            path = _REGRID_CACHE_DIR / f"reg_{disk_key}.npz"
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=_REGRID_CACHE_DIR,
                    prefix=f"reg_{disk_key}_",
                    suffix=".npz",
                    delete=False,
                ) as tmp_f:
                    tmp_path = Path(tmp_f.name)
                np.savez(
                    tmp_path,
                    lon_idx=lon_idx_i32,
                    lat_idx=lat_idx_i32,
                    lat_descending=np.array(lat_descending, dtype=np.bool_),
                    lon_wrap=np.array(lon_wrap, dtype=np.bool_),
                    wrap_idx=np.array(wrap_idx, dtype=np.int32),
                )
                tmp_path.replace(path)
                logger.info("Saved regular regrid cache to disk: %s", path.name)
            except Exception as exc:
                logger.warning("Failed to save regular regrid cache: %s", exc)
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
        finally:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except Exception:
                    pass
                try:
                    lock_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _regrid_regular(self, lon_1d, lat_1d, data, bounds, target_w, target_h):
        """Regrid from regular lat/lon grid using nearest-neighbor sampling."""
        cache_key = (len(lon_1d), len(lat_1d), bounds, target_w, target_h)

        if cache_key not in self._regrid_cache:
            self._build_regular_cache(lon_1d, lat_1d, bounds, cache_key,
                                      target_w, target_h)
            # Evict oldest if cache too large
            while len(self._regrid_cache) > _REGRID_CACHE_MAX:
                self._regrid_cache.pop(next(iter(self._regrid_cache)))

        c = self._regrid_cache[cache_key]

        if c['lat_descending']:
            data = data[::-1, :] if data.ndim > 1 else data
        if c['lon_wrap']:
            data = np.roll(data, -c['wrap_idx'], axis=1) if data.ndim > 1 else data

        result = data[c['lat_idx'], c['lon_idx']]
        return result.astype(np.float32)

    def _extend_outside_domain(
        self,
        rgba,
        outside_mask,
        fade_start=0.02,
        fade_end=0.15,
        apply_alpha_fade=True,
        cached_edt=None,
    ):
        """Extend data into void pixels with diffused fill and smooth alpha fade.

        Replaces raw nearest-neighbor stretching with a polished approach:
        1. NN fill for base colors
        2. Iterative blur diffusion — blur+replace void pixels multiple passes
           to completely eliminate NN streak artifacts
        3. Optional alpha gradient fade at the boundary edge

        If cached_edt is provided, reuses pre-computed distance arrays.
        """
        if not np.any(outside_mask) or np.all(outside_mask):
            return rgba

        h, w = rgba.shape[:2]

        # Step 1: Fill voids with nearest-neighbor colors
        # We always need nearest_idx for color filling (param-dependent),
        # but reuse cached distances for the alpha fade.
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

        # Step 3: Alpha fade — opaque near data, fading to transparent at edge.
        # Use a wide, smooth fade so the domain boundary is invisible.
        fade_start_px = max_dist * fade_start
        fade_end_px = max_dist * fade_end
        fade_range = max(fade_end_px - fade_start_px, 1.0)

        # Ease-out curve (quadratic) for a softer, more natural falloff
        t = np.clip(
            (distances[outside_mask] - fade_start_px) / fade_range,
            0, 1)
        void_alpha = (1.0 - t * t) * 255.0

        alpha = rgba[:, :, 3].astype(np.float32)
        alpha[outside_mask] = void_alpha
        rgba[:, :, 3] = alpha.astype(np.uint8)

        return rgba

    # ------------------------------------------------------------------
    # Colormap + norm
    # ------------------------------------------------------------------

    @staticmethod
    def _boost_cmap_lut(cmap, param: str):
        """Pre-bake saturation (1.35x) and contrast (1.08x) into colormap LUT.

        This eliminates per-pixel float math during rendering. The LUT is
        only 256 entries so the cost is negligible at cache-build time.
        """
        _SKIP_BOOST = {
            "reflectivity", "cloud_cover", "cloud_cover_low",
            "cloud_cover_mid", "cloud_cover_high",
        }
        if param in _SKIP_BOOST:
            return cmap

        # Sample 256-entry LUT as float RGBA
        lut = cmap(np.linspace(0, 1, 256))  # (256, 4)
        rgb = lut[:, :3].copy() * 255.0
        alpha = lut[:, 3:4].copy()

        # Saturation boost 1.35x
        gray = (0.299 * rgb[:, 0] + 0.587 * rgb[:, 1] + 0.114 * rgb[:, 2])[:, np.newaxis]
        rgb = gray * (1.0 - 1.35) + rgb * 1.35

        # Contrast boost 1.08x
        mean_val = float(rgb.mean())
        rgb = mean_val * (1.0 - 1.08) + rgb * 1.08

        np.clip(rgb, 0, 255, out=rgb)
        rgb /= 255.0

        boosted_lut = np.concatenate([rgb, alpha], axis=1)
        boosted = ListedColormap(boosted_lut, name=f"{cmap.name}_boosted")

        # Preserve under/over/bad colors from original
        boosted.set_under(cmap.get_under())
        boosted.set_over(cmap.get_over())
        boosted.set_bad(cmap.get_bad())
        return boosted

    def _make_cmap_norm(self, param: str):
        if param in _cmap_norm_cache:
            return _cmap_norm_cache[param]

        cfg = COLOR_SCALES.get(param, {})
        cmap_name = cfg.get("cmap")
        vmin = cfg.get("vmin", 0)
        vmax = cfg.get("vmax", 100)

        if cfg.get("bounds") and cfg.get("colors"):
            colors = list(cfg["colors"])
            bounds = list(cfg["bounds"])
            cmap = ListedColormap(colors)
            cmap.set_under((0, 0, 0, 0))
            cmap.set_bad((0, 0, 0, 0))
            cmap.set_over(colors[-1])
            result = (cmap, BoundaryNorm(bounds, cmap.N, clip=False))
            _cmap_norm_cache[param] = result
            return result

        if cmap_name == "nws_refl":
            cmap = _NWS_REFL_CMAP.copy()
            cmap.set_under((0, 0, 0, 0))
            cmap.set_bad((0, 0, 0, 0))
            result = (cmap, _NWS_REFL_NORM)
            _cmap_norm_cache[param] = result
            return result

        cmap = plt.get_cmap(cmap_name).copy() if cmap_name else None
        if cmap:
            # Check for reverse colormap option
            if cfg.get("reverse"):
                cmap = cmap.reversed()
            _UNDER_PARAMS = {
                "precip": 0.01, "cape": 250, "mlcape": 250, "mucape": 250,
                "cape_03km": 100,
                "srh_01km": 25, "srh_03km": 25,
                "shear_06km": 5, "shear_01km": 5,
                "kuchera_6hr": 0.1, "kuchera_12hr": 0.1, "snow10_12hr": 0.1,
                "stp": 0.5, "scp": 0.5, "cti": 0.3, "msi": 0.3,
                "ship": 0.3, "dcp": 0.3, "citi": 0.2, "smi": 0.05,
                "vil": 1, "uh_25km": 25,
                "lightning": 0.01,
            }
            if param in _UNDER_PARAMS:
                cmap.set_under((0, 0, 0, 0))
                vmin = _UNDER_PARAMS[param]
            cmap.set_bad((0, 0, 0, 0))
            # Pre-bake saturation/contrast into the colormap LUT
            if cfg.get("boost", True):
                cmap = self._boost_cmap_lut(cmap, param)
        norm = mcolors.Normalize(vmin=vmin, vmax=vmax) if cmap else None
        result = (cmap, norm)
        _cmap_norm_cache[param] = result
        return result

    @staticmethod
    def _prepare_scalar_data(param, data):
        """Apply display-domain filtering before fill or contour rendering."""
        if param in {"cin", "mlcin", "mucin"}:
            return np.clip(data, -300.0, 0.0)
        if param in {"cape", "mlcape", "mucape", "cape_03km"}:
            return np.maximum(data, 0.0)
        if param in {"uh_02km", "uh_03km", "uh_25km"}:
            return np.where(data >= 25.0, data, np.nan)
        if param == "vort_500mb":
            return np.where(data >= 4.0, data, np.nan)
        if param == "reflectivity":
            return np.where(data >= 10.0, data, np.nan)
        if param in {"srh_01km", "srh_03km"}:
            return np.clip(data, -500.0, 500.0)
        if param in {"shear_06km", "shear_01km"}:
            return np.maximum(data, 0.0)
        if param == "lifted_index":
            return np.clip(data, -12.0, 12.0)
        if param == "lcl":
            return np.maximum(data, 0.0)
        if param in {"stp", "scp"}:
            return np.maximum(data, 0.0)
        if param == "efhl":
            return np.where(data >= 25.0, data, np.nan)
        if param == "max_wind":
            return np.maximum(data, 0.0)
        if param == "hail":
            return np.where(data >= 0.25, data, np.nan)
        if param == "echo_tops":
            return np.where(data >= 20000.0, data, np.nan)
        if param == "pbl_height":
            return np.maximum(data, 0.0)
        if param in {
            "snow_depth", "snowfall", "snowfall_6h", "weasd",
            "kuchera_6hr", "kuchera_12hr", "snow10_12hr",
        }:
            return np.where(data >= 0.1, data, np.nan)
        if param == "visibility":
            return np.clip(data, 0.0, 10.0)
        if param == "frozen_pct":
            return np.clip(data, 0.0, 100.0)
        if param == "frzlvl":
            return np.maximum(data, 0.0)
        return data

    # ------------------------------------------------------------------
    # Fast PIL rendering for fill params
    # ------------------------------------------------------------------

    def _render_fill(self, param, data_dict, output_path):
        """Render a fill param: colormap at native res -> RGBA PNG."""
        fill_result = self._build_fill_rgba(param, data_dict)
        if not fill_result:
            return False
        rgba, bounds = fill_result
        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        self._last_render_bounds = bounds
        image = Image.fromarray(rgba, 'RGBA')
        image.save(str(output_path), compress_level=1)
        return True

    def _build_fill_rgba(self, param, data_dict):
        """Return an RGBA array and bounds for a scalar fill overlay."""
        data = data_dict["data"]
        lat = data_dict["lat"]
        lon = data_dict["lon"]
        bounds = self.get_data_bounds(lat, lon)

        data = self._prepare_scalar_data(param, data)

        cmap, norm = self._make_cmap_norm(param)
        if cmap is None:
            return False

        target_w, target_h = self._estimate_resolution(lon, lat, data, bounds)
        grid_data = self._regrid(lon, lat, data, bounds, target_w, target_h)

        # Apply colormap -> RGBA uint8
        normalized = norm(grid_data)
        rgba = (cmap(normalized) * 255).astype(np.uint8)

        # Ensure NaN/masked pixels are fully transparent
        nan_mask = np.isnan(grid_data)
        rgba[nan_mask] = 0
        # Most non-rectilinear grids benefit from outside-domain diffusion.
        # LCL is a special case: borrowing edge colors can smear red/high-LCL
        # values into the void, so fill the outside with the low-end palette
        # color instead of diffusing boundary colors.
        overlay_alpha_cfg = _SPARSE_OVERLAY_ALPHA.get(param)

        if np.ndim(lon) == 2 and np.any(nan_mask):
            # Cache EDT results per grid shape — the nan_mask is
            # identical for every param on the same model grid.
            edt_key = (nan_mask.shape, nan_mask.tobytes().__hash__())
            if edt_key not in self._edt_cache:
                outside_edt = distance_transform_edt(nan_mask)
                valid_edt = distance_transform_edt(~nan_mask).astype(np.float32)
                self._edt_cache[edt_key] = {
                    'outside_edt': outside_edt,
                    'valid_edt': valid_edt,
                }
                # Keep cache bounded
                while len(self._edt_cache) > 5:
                    self._edt_cache.pop(next(iter(self._edt_cache)))
            cached_edt = self._edt_cache[edt_key]

            if param == "lcl":
                low_rgb = (np.array(cmap(0.0)[:3]) * 255).astype(np.uint8)
                rgba[nan_mask, :3] = low_rgb
                rgba[nan_mask, 3] = 255
                inner_edge = (~nan_mask) & (cached_edt['valid_edt'] <= 1.5)
                rgba[inner_edge, :3] = low_rgb
            elif overlay_alpha_cfg:
                # Sparse overlays handle their own alpha — but still
                # feather the inner edge so the data boundary doesn't
                # show as a hard line where values are near the floor.
                pass
            elif param in _NO_DIFFUSION_PARAMS:
                # These params render opaque data on a black backing —
                # skip diffusion so no edge colors bleed into the outside.
                pass
            elif self._current_model_name in _NO_OUTSIDE_DIFFUSION_MODELS:
                # Some ensemble products, especially HREF, can end up with a
                # dominant boundary color that turns the outside-domain fill
                # into a solid slab. Keep the outside fully transparent there.
                edge_dist = cached_edt['valid_edt']
                feather_px = max(6, int(min(rgba.shape[:2]) * 0.02))
                edge_fade = np.clip(edge_dist / feather_px, 0, 1)
                rgba[:, :, 3] = (rgba[:, :, 3].astype(np.float32) * edge_fade).astype(np.uint8)
            else:
                # Diffuse boundary colors AND fade them out so the
                # model domain edge blends invisibly into the basemap.
                rgba = self._extend_outside_domain(
                    rgba,
                    nan_mask,
                    fade_start=0.0,
                    fade_end=0.40,
                    apply_alpha_fade=True,
                    cached_edt=cached_edt,
                )
                # Feather the inner edge of valid data too — prevents
                # a hard alpha cliff at the domain boundary.
                edge_dist = cached_edt['valid_edt']
                feather_px = max(6, int(min(rgba.shape[:2]) * 0.02))
                edge_fade = np.clip(edge_dist / feather_px, 0, 1)
                rgba[:, :, 3] = (rgba[:, :, 3].astype(np.float32) * edge_fade).astype(np.uint8)

        if overlay_alpha_cfg:
            cfg_vmax = float(COLOR_SCALES.get(param, {}).get("vmax", 100.0))
            overlay_alpha = np.clip(
                (np.nan_to_num(grid_data, nan=0.0) - overlay_alpha_cfg["floor"]) /
                max(cfg_vmax - overlay_alpha_cfg["floor"], 1.0),
                0,
                1,
            )
            overlay_alpha = np.where(
                nan_mask,
                0,
                (
                    overlay_alpha_cfg["min_alpha"]
                    + overlay_alpha
                    * (overlay_alpha_cfg["max_alpha"] - overlay_alpha_cfg["min_alpha"])
                ).astype(np.uint8),
            )
            rgba[:, :, 3] = overlay_alpha

        if False and param in _PRESSURE_WIND_PARAMS:
            rgba[:, :, 3] = (rgba[:, :, 3].astype(np.float32) * 0.62).astype(np.uint8)

        # Saturation/contrast boost is now pre-baked into the colormap LUT
        # (see _boost_cmap_lut) — no per-pixel math needed here.

        return rgba, bounds

    # ------------------------------------------------------------------
    # Matplotlib rendering for MSLP and wind (vector drawing needed)
    # ------------------------------------------------------------------

    def _make_fig_ax(self, bounds, target_w, target_h):
        """Create a figure and axes in Mercator Y-space with transparent bg."""
        south, north, west, east = bounds
        self._last_render_bounds = bounds
        merc_south = float(_lat_to_merc(south))
        merc_north = float(_lat_to_merc(north))
        fig_w = target_w / RENDER_DPI
        fig_h = target_h / RENDER_DPI

        fig = plt.figure(figsize=(fig_w, fig_h), dpi=RENDER_DPI)
        fig.patch.set_alpha(0)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.patch.set_alpha(0)
        ax.set_xlim(west, east)
        ax.set_ylim(merc_south, merc_north)
        return fig, ax

    def _mpl_to_pil(self, fig, output_path):
        """Render matplotlib figure to RGBA PNG."""
        fig.canvas.draw()
        w_px, h_px = fig.canvas.get_width_height()
        buf = (np.frombuffer(fig.canvas.buffer_rgba(), dtype=np.uint8)
               .reshape(h_px, w_px, 4).copy())
        plt.close(fig)

        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        image = Image.fromarray(buf, 'RGBA')
        image.save(str(output_path), compress_level=1)
        return True

    def _get_mpl_resolution(self, lon, lat, data, bounds):
        """Get resolution for matplotlib params (minimum _MPL_MIN_WIDTH)."""
        w, h = self._estimate_resolution(lon, lat, data, bounds)
        if w < _MPL_MIN_WIDTH:
            scale = _MPL_MIN_WIDTH / w
            w = _MPL_MIN_WIDTH
            h = int(h * scale)
        return w, h

    def _render_mslp(self, data_dict, output_path, param="mslp"):
        """Contour-only pressure field using matplotlib on transparent background."""
        data = data_dict["data"]
        lat = data_dict["lat"]
        lon = data_dict["lon"]
        lon_n = np.where(lon > 180, lon - 360, lon)
        bounds = self.get_data_bounds(lat, lon)

        target_w, target_h = self._get_mpl_resolution(lon, lat, data, bounds)
        fig, ax = self._make_fig_ax(bounds, target_w, target_h)

        tg = self._get_target_grid(bounds, target_w, target_h)
        lon_grid = tg["lon"]
        lat_merc_grid = _lat_to_merc(tg["lat"])
        gridded = self._regrid(lon_n, lat, data.astype(np.float32), bounds, target_w, target_h)

        # Scale smoothing for coarse grids (same logic as height contours)
        mslp_sigma = 0.8
        if lon.ndim == 1 and len(lon) > 1:
            dlon = abs(float(lon[1] - lon[0]))
            native_cols = max(1, int((bounds[3] - bounds[2]) / dlon))
            upsample = target_w / native_cols
            if upsample > 2.0:
                mslp_sigma = 0.8 * upsample * 0.55
        contour_data = gaussian_filter(gridded, sigma=mslp_sigma)

        if param == "psfc":
            thin_levels = np.arange(500, 1064, 4)
            bold_levels = np.arange(500, 1064, 20)
            thin_alpha = 0.32
            bold_alpha = 0.68
        else:
            thin_levels = np.arange(900, 1060, 2)
            bold_levels = np.arange(900, 1060, 4)
            thin_alpha = 0.4
            bold_alpha = 0.75

        # Thin contours show pressure structure without overpowering the map.
        ax.contour(
            lon_grid,
            lat_merc_grid,
            contour_data,
            levels=thin_levels,
            colors="white",
            linewidths=0.15,
            alpha=thin_alpha,
        )
        cs_bold = ax.contour(
            lon_grid,
            lat_merc_grid,
            contour_data,
            levels=bold_levels,
            colors="white",
            linewidths=0.7,
            alpha=bold_alpha,
        )
        ax.clabel(
            cs_bold,
            levels=bold_levels,
            inline=True,
            fontsize=5.5,
            fmt="%d",
            colors="white",
        )

        if param == "psfc":
            return self._mpl_to_pil(fig, output_path)

        # --- H/L pressure center detection on gaussian-filtered data (handles NaN) ---
        # Inner window ~50km, outer window ~150km (synoptic-scale)
        inner, outer = 15, 45
        if contour_data.shape[0] < outer or contour_data.shape[1] < outer:
            inner, outer = 7, 21

        local_min = minimum_filter(contour_data, size=inner)
        local_max = maximum_filter(contour_data, size=inner)
        outer_min = minimum_filter(contour_data, size=outer)
        outer_max = maximum_filter(contour_data, size=outer)

        atol = 0.1  # hPa tolerance for floating-point equality
        is_low = np.isclose(contour_data, local_min, atol=atol)
        is_high = np.isclose(contour_data, local_max, atol=atol)
        low_prom = local_min - outer_min
        high_prom = outer_max - local_max

        min_prom = 5.0  # minimum 5mb prominence for significant systems
        low_mask = is_low & (low_prom >= min_prom)
        high_mask = is_high & (high_prom >= min_prom)

        # Suppression radius ~300km (~150 grid points for 2km HRRR grid)
        suppression_radius = 150
        low_centers, high_centers = [], []
        kept = np.full(contour_data.shape, False, dtype=bool)

        for cur_mask, centers_list, sign in [(low_mask, low_centers, -1), (high_mask, high_centers, 1)]:
            ys, xs = np.where(cur_mask)
            if len(xs) == 0:
                continue
            # Defensive: ensure indices are within bounds
            valid = (ys >= 0) & (ys < contour_data.shape[0]) & (xs >= 0) & (xs < contour_data.shape[1])
            ys, xs = ys[valid], xs[valid]
            vals = contour_data[cur_mask][valid]
            order = np.argsort(vals) if sign < 0 else np.argsort(vals)[::-1]
            for oi in order:
                y, x = int(ys[oi]), int(xs[oi])
                if kept[y, x]:
                    continue
                kept[y, x] = True
                y1 = max(0, y - suppression_radius)
                y2 = min(contour_data.shape[0], y + suppression_radius)
                x1 = max(0, x - suppression_radius)
                x2 = min(contour_data.shape[1], x + suppression_radius)
                kept[y1:y2, x1:x2] = True
                px = lon_grid[y, x]
                py = lat_merc_grid[y, x]
                centers_list.append((contour_data[y, x], px, py))

        self._annotate_pressure_centers(ax, low_centers, "L", "#ff6666")
        self._annotate_pressure_centers(ax, high_centers, "H", "#66ff66")

        return self._mpl_to_pil(fig, output_path)

    def _draw_height_contours(
        self,
        ax,
        contour_param,
        contour_dict,
        bounds,
        target_w,
        target_h,
        *,
        zorder=5,
        alpha_scale=1.0,
    ):
        """Draw geopotential height contours for upper-air overlays."""
        cfg = _HEIGHT_CONTOUR_CONFIG.get(contour_param)
        if not cfg:
            return

        data = contour_dict["data"]
        lat = contour_dict["lat"]
        lon = contour_dict["lon"]
        lon_n = np.where(lon > 180, lon - 360, lon)
        tg = self._get_target_grid(bounds, target_w, target_h)
        lon_grid = tg["lon"]
        lat_merc_grid = _lat_to_merc(tg["lat"])
        gridded = self._regrid(lon_n, lat, data.astype(np.float32), bounds, target_w, target_h)

        # Scale smoothing sigma for coarse grids (GFS 0.25° vs HRRR 3km).
        # Estimate native grid columns across domain; if target_w is much
        # larger than that, the NN regrid created stair-steps that need
        # heavier smoothing.
        base_sigma = cfg.get("smooth_sigma", 0.8)
        if lon.ndim == 1 and len(lon) > 1:
            south, north, west, east = bounds
            dlon = abs(float(lon[1] - lon[0]))
            native_cols = max(1, int((east - west) / dlon))
            upsample = target_w / native_cols
            if upsample > 2.0:
                base_sigma = base_sigma * upsample * 0.55
        contour_data = gaussian_filter(gridded, sigma=base_sigma)

        scale_cfg = COLOR_SCALES.get(contour_param, {})
        vmin = float(scale_cfg.get("vmin", np.nanmin(contour_data)))
        vmax = float(scale_cfg.get("vmax", np.nanmax(contour_data)))
        interval = float(cfg["interval"])
        label_every = float(cfg["label_every"])
        start = np.floor(vmin / interval) * interval
        end = np.ceil(vmax / interval) * interval + interval
        levels = np.arange(start, end, interval)
        label_levels = np.arange(np.floor(vmin / label_every) * label_every, end, label_every)
        stroke = [pe.withStroke(linewidth=1.2, foreground=(0, 0, 0, 0.72))]

        cs = ax.contour(
            lon_grid,
            lat_merc_grid,
            contour_data,
            levels=levels,
            colors="#f4f7fb",
            linewidths=0.65,
            alpha=0.9 * alpha_scale,
            zorder=zorder,
        )
        for coll in getattr(cs, "collections", []):
            coll.set_path_effects(stroke)

        label_levels = [float(level) for level in label_levels if np.any(np.isclose(levels, level))]
        if label_levels:
            label_texts = ax.clabel(
                cs,
                levels=label_levels,
                inline=True,
                fontsize=cfg.get("label_fontsize", 5.5),
                inline_spacing=2,
                fmt="%d",
                colors="#f4f7fb",
                zorder=zorder + 0.1,
            )
            for txt in label_texts:
                txt.set_path_effects(stroke)

    @staticmethod
    def _contour_label_format(levels, fmt):
        if callable(fmt):
            return fmt
        if isinstance(fmt, str):
            if "%" in fmt:
                return fmt
            return {float(level): fmt for level in levels}
        return "%g"

    def _draw_scalar_contours(self, ax, param, data_dict, bounds, target_w, target_h, *, zorder=3):
        """Draw product-specific isolines declared in COLOR_SCALES."""
        cfg = COLOR_SCALES.get(param, {})
        contour_cfg = cfg.get("contours")
        if not contour_cfg:
            return

        contour_cfgs = contour_cfg if isinstance(contour_cfg, list) else [contour_cfg]
        data = self._prepare_scalar_data(param, data_dict["data"]).astype(np.float32)
        lat = data_dict["lat"]
        lon = data_dict["lon"]
        lon_n = np.where(lon > 180, lon - 360, lon)
        tg = self._get_target_grid(bounds, target_w, target_h)
        lon_grid = tg["lon"]
        lat_merc_grid = _lat_to_merc(tg["lat"])
        gridded = self._regrid(lon_n, lat, data, bounds, target_w, target_h)

        valid = gridded[np.isfinite(gridded)]
        if valid.size == 0:
            return
        vmin = float(valid.min())
        vmax = float(valid.max())

        for item in contour_cfgs:
            if not isinstance(item, dict):
                continue
            levels = item.get("levels")
            if levels is None:
                interval = item.get("interval")
                if not interval:
                    continue
                start = np.floor(vmin / interval) * interval
                end = np.ceil(vmax / interval) * interval + interval
                levels = np.arange(start, end, interval)
            levels = [float(level) for level in levels if vmin <= float(level) <= vmax]
            if not levels:
                continue

            sigma = float(item.get("smooth_sigma", 0.0))
            contour_data = self._smooth_field(gridded, sigma) if sigma > 0 else gridded
            try:
                cs = ax.contour(
                    lon_grid,
                    lat_merc_grid,
                    contour_data,
                    levels=levels,
                    colors=item.get("colors", "#f8fafc"),
                    linewidths=item.get("linewidths", 0.4),
                    alpha=item.get("alpha", 0.45),
                    linestyles=item.get("linestyles", "solid"),
                    zorder=zorder,
                )
            except Exception as exc:
                logger.debug("%s scalar contour failed: %s", param, exc)
                continue

            stroke_width = float(item.get("stroke_width", 1.0))
            stroke_alpha = float(item.get("stroke_alpha", 0.55))
            if stroke_width > 0:
                stroke = [pe.withStroke(linewidth=stroke_width, foreground=(0, 0, 0, stroke_alpha))]
                for coll in getattr(cs, "collections", []):
                    coll.set_path_effects(stroke)
            else:
                stroke = []

            if item.get("labels"):
                label_levels = item.get("label_levels", levels)
                label_levels = [float(level) for level in label_levels if float(level) in levels]
                if label_levels:
                    texts = ax.clabel(
                        cs,
                        levels=label_levels,
                        inline=True,
                        inline_spacing=2,
                        fontsize=item.get("label_fontsize", 5.0),
                        fmt=self._contour_label_format(label_levels, item.get("label_fmt", "%g")),
                        colors=item.get("label_color", item.get("colors", "#f8fafc")),
                        zorder=zorder + 0.1,
                    )
                    for txt in texts:
                        if stroke:
                            txt.set_path_effects(stroke)

    def _render_scalar_contour_only(self, param, data_dict, output_path):
        """Render a scalar product as isolines only."""
        data = data_dict["data"]
        lat = data_dict["lat"]
        lon = data_dict["lon"]
        bounds = self.get_data_bounds(lat, lon)
        target_w, target_h = self._get_mpl_resolution(lon, lat, data, bounds)
        fig, ax = self._make_fig_ax(bounds, target_w, target_h)
        self._draw_scalar_contours(ax, param, data_dict, bounds, target_w, target_h, zorder=3)
        return self._mpl_to_pil(fig, output_path)

    def _render_fill_with_scalar_contours(self, param, data_dict, output_path):
        """Render a scalar fill with its own isolines overlaid."""
        fill_result = self._build_fill_rgba(param, data_dict)
        if not fill_result:
            return False
        rgba, bounds = fill_result
        fill_h, fill_w = rgba.shape[:2]
        target_w, target_h = self._get_mpl_resolution(
            data_dict["lon"], data_dict["lat"], data_dict["data"], bounds,
        )
        target_w = max(fill_w, target_w)
        target_h = max(fill_h, target_h)
        south, north, west, east = bounds
        merc_south = float(_lat_to_merc(south))
        merc_north = float(_lat_to_merc(north))
        fig, ax = self._make_fig_ax(bounds, target_w, target_h)
        ax.imshow(
            rgba,
            extent=(west, east, merc_south, merc_north),
            origin="upper",
            interpolation="bilinear",
            zorder=1,
        )
        self._draw_scalar_contours(ax, param, data_dict, bounds, target_w, target_h, zorder=3)
        return self._mpl_to_pil(fig, output_path)

    def _render_height_field(self, param, data_dict, output_path):
        """Render contour-only geopotential height products."""
        data = data_dict["data"]
        lat = data_dict["lat"]
        lon = data_dict["lon"]
        bounds = self.get_data_bounds(lat, lon)
        target_w, target_h = self._get_mpl_resolution(lon, lat, data, bounds)
        fig, ax = self._make_fig_ax(bounds, target_w, target_h)
        self._draw_height_contours(ax, param, data_dict, bounds, target_w, target_h, zorder=3)
        return self._mpl_to_pil(fig, output_path)

    def _render_fill_with_height_contours(self, param, data_dict, contour_param, contour_dict, output_path):
        """Render a scalar fill product with matching geopotential height contours."""
        fill_result = self._build_fill_rgba(param, data_dict)
        if not fill_result:
            return False
        rgba, bounds = fill_result
        fill_h, fill_w = rgba.shape[:2]
        # Use at least _MPL_MIN_WIDTH so contour lines are crisp on coarse grids
        contour_w, contour_h = self._get_mpl_resolution(
            contour_dict["lon"], contour_dict["lat"],
            contour_dict["data"], bounds,
        )
        target_w = max(fill_w, contour_w)
        target_h = max(fill_h, contour_h)
        south, north, west, east = bounds
        merc_south = float(_lat_to_merc(south))
        merc_north = float(_lat_to_merc(north))
        fig, ax = self._make_fig_ax(bounds, target_w, target_h)
        ax.imshow(
            rgba,
            extent=(west, east, merc_south, merc_north),
            origin="upper",
            interpolation="bilinear",
            zorder=1,
        )
        self._draw_scalar_contours(ax, param, data_dict, bounds, target_w, target_h, zorder=3)
        self._draw_height_contours(ax, contour_param, contour_dict, bounds, target_w, target_h, zorder=4)
        return self._mpl_to_pil(fig, output_path)

    def _annotate_pressure_centers(self, ax, centers, symbol, color):
        """Draw High/Low markers with pressure values beneath them."""
        if not centers:
            return
        y_offset = (ax.get_ylim()[1] - ax.get_ylim()[0]) * 0.022
        stroke = [pe.withStroke(linewidth=2.4, foreground=(0, 0, 0, 0.78))]
        for pressure, x, y in centers:
            ax.text(
                x,
                y,
                symbol,
                ha="center",
                va="center",
                fontsize=16,
                fontweight="bold",
                color=color,
                path_effects=stroke,
                zorder=10,
            )
            ax.text(
                x,
                y - y_offset,
                f"{int(round(pressure))}",
                ha="center",
                va="top",
                fontsize=7,
                fontweight="bold",
                color="white",
                path_effects=stroke,
                zorder=10,
            )

    def _rotate_to_earth_relative(self, lon, lat, tg, grid_u, grid_v):
        """Rotate Lambert grid-relative winds approximately to earth-relative."""
        if np.ndim(lon) == 2 and np.ndim(lat) == 2:
            lon_grid = tg["lon"]
            lon0 = -97.5
            parallel_sin = np.sin(np.radians(38.5))
            angle = parallel_sin * np.radians(lon_grid - lon0)
            u_earth = grid_u * np.cos(angle) + grid_v * np.sin(angle)
            v_earth = -grid_u * np.sin(angle) + grid_v * np.cos(angle)
            return u_earth, v_earth
        return grid_u, grid_v

    def _get_wind_style(self, param):
        """Return rendering thresholds and intervals for wind products.

        Design philosophy:
        - Minimal contour clutter — only major isotachs at operationally
          relevant thresholds, no minor contours to compete with barbs.
        - Barbs are the primary directional element; fill shows magnitude.
        - Alpha ramps from transparent at threshold to semi-opaque at
          the upper range, so strong features pop and weak areas recede.
        - Barb density is tuned per level: fewer barbs at upper levels
          where flow is smoother; more at surface where detail matters.
        """
        if param == "wind_speed":
            return {
                "fill_floor": 5.0,
                "barb_floor": 10.0 / 1.15078,  # mph -> kt
                "contours": [],                 # surface: fill + barbs only
                "major_contours": [],
                "target_cols": 48,              # dense grid for surface detail
                "resolution_scale": 1.4,
                "barb_length": 4.2,
                "smooth_sigma": 1.0,
                "fill_gamma": 0.85,             # slight lift to mid-range
                "alpha_gamma": 0.9,
                "alpha_min": 0,                 # invisible below threshold
                "alpha_max": 200,               # semi-opaque at vmax
                "barb_color": "#11161d",
                "barb_lw": 0.24,
                "barb_outline_color": "white",
                "barb_outline_lw": 0.04,
                "barb_outline_alpha": 0.22,
            }
        if param in {"wind_925mb", "wind_850mb", "wind_700mb"}:
            if param == "wind_700mb":
                return {
                    "fill_floor": 10.0,
                    "barb_floor": 15.0,
                    "contours": [],
                    "major_contours": [20, 30, 50, 70],
                    "target_cols": 46,
                    "resolution_scale": 1.45,
                    "barb_length": 4.25,
                    "smooth_sigma": 1.0,
                    "fill_gamma": 0.9,
                    "alpha_gamma": 1.0,
                    "alpha_min": 0,
                    "alpha_max": 215,
                    "barb_color": "#11161d",
                    "barb_lw": 0.24,
                    "barb_outline_color": "white",
                    "barb_outline_lw": 0.04,
                    "barb_outline_alpha": 0.22,
                    "major_color": "#ffffff",
                    "major_width": 0.7,
                    "major_alpha": 0.45,
                    "major_stroke": 0.15,
                }
            return {
                "fill_floor": 15.0 if param == "wind_850mb" else 20.0,
                "barb_floor": 20.0 if param == "wind_850mb" else 25.0,
                "contours": [],                 # clean: no minor isotachs
                "major_contours": [30, 50, 70], # key operational thresholds
                "target_cols": 52 if param == "wind_850mb" else 42,
                "resolution_scale": 1.45,
                "barb_length": 4.25,
                "smooth_sigma": 1.0,
                "fill_gamma": 0.9,
                "alpha_gamma": 1.1,
                "alpha_min": 0,
                "alpha_max": 215,
                "barb_color": "#11161d",
                "barb_lw": 0.24,
                "barb_outline_color": "white",
                "barb_outline_lw": 0.04,
                "barb_outline_alpha": 0.22,
                "major_color": "#ffffff",
                "major_width": 0.7,
                "major_alpha": 0.45,
                "major_stroke": 0.15,
            }
        if param == "wind_500mb":
            return {
                "fill_floor": 30.0,
                "barb_floor": 35.0,
                "contours": [],
                "major_contours": [30, 50, 70, 90],
                "target_cols": 38,
                "resolution_scale": 1.5,
                "barb_length": 4.3,
                "smooth_sigma": 0.9,
                "fill_gamma": 0.9,
                "alpha_gamma": 1.1,
                "alpha_min": 0,
                "alpha_max": 220,
                "barb_color": "#10151c",
                "barb_lw": 0.23,
                "barb_outline_color": "white",
                "barb_outline_lw": 0.035,
                "barb_outline_alpha": 0.2,
                "major_color": "#ffffff",
                "major_width": 0.7,
                "major_alpha": 0.45,
                "major_stroke": 0.15,
            }
        if param in {"wind_300mb", "wind_250mb", "wind_200mb"}:
            return {
                "fill_floor": 50.0 if param == "wind_250mb" else 35.0,
                "barb_floor": 55.0 if param == "wind_250mb" else 40.0,
                "contours": [],
                "major_contours": [] if param == "wind_300mb" else [50, 70, 90, 110, 130, 150],
                "target_cols": 34,
                "resolution_scale": 1.55,
                "barb_length": 4.4,
                "smooth_sigma": 0.8,
                "fill_gamma": 0.85,
                "alpha_gamma": 1.15,
                "alpha_min": 0,
                "alpha_max": 225,
                "barb_color": "#0f141b",
                "barb_lw": 0.22,
                "barb_outline_color": "white",
                "barb_outline_lw": 0.03,
                "barb_outline_alpha": 0.18,
                "major_color": "#ffffff",
                "major_width": 0.8,
                "major_alpha": 0.5,
                "major_stroke": 0.18,
            }
        # Fallback
        return {
            "fill_floor": 25.0,
            "barb_floor": 30.0,
            "contours": [],
            "major_contours": [50, 75, 100, 125, 150],
            "target_cols": 34,
            "barb_length": 3.8,
            "smooth_sigma": 0.8,
            "fill_gamma": 0.85,
            "alpha_gamma": 1.15,
            "alpha_min": 0,
            "alpha_max": 225,
            "barb_color": "#333333",
            "barb_lw": 0.35,
            "barb_outline_color": "white",
            "barb_outline_lw": 0.10,
            "major_color": "#ffffff",
            "major_width": 0.8,
            "major_alpha": 0.5,
            "major_stroke": 0.18,
        }

    def _smooth_field(self, data, sigma):
        """Apply NaN-aware gaussian smoothing for display-only wind fields."""
        if sigma <= 0:
            return data
        valid = np.isfinite(data)
        if not np.any(valid):
            return data
        filled = np.where(valid, data, 0.0).astype(np.float32)
        weights = valid.astype(np.float32)
        smooth_data = gaussian_filter(filled, sigma=sigma, mode="nearest")
        smooth_weights = gaussian_filter(weights, sigma=sigma, mode="nearest")
        result = np.divide(
            smooth_data,
            smooth_weights,
            out=np.full_like(smooth_data, np.nan),
            where=smooth_weights > 1e-3,
        )
        result[~valid & (smooth_weights < 0.05)] = np.nan
        return result

    def _render_wind_overlay(self, param, speed_dict, u_dict, v_dict, output_path, contour_param=None, contour_dict=None):
        """Render a professional-style wind analysis overlay."""
        speed = speed_dict["data"]
        u = u_dict["data"]
        v = v_dict["data"]
        lat = speed_dict["lat"]
        lon = speed_dict["lon"]
        lon_n = np.where(lon > 180, lon - 360, lon)
        bounds = self.get_data_bounds(lat, lon)
        south, north, west, east = bounds
        merc_south = float(_lat_to_merc(south))
        merc_north = float(_lat_to_merc(north))

        style = self._get_wind_style(param)
        target_w, target_h = self._get_mpl_resolution(lon, lat, speed, bounds)
        resolution_scale = float(style.get("resolution_scale", 1.0))
        if resolution_scale != 1.0:
            target_w = int(round(target_w * resolution_scale))
            target_h = int(round(target_h * resolution_scale))
        fig, ax = self._make_fig_ax(bounds, target_w, target_h)

        grid_speed = self._regrid(lon_n, lat, speed, bounds, target_w, target_h)
        grid_u = self._regrid(lon_n, lat, u, bounds, target_w, target_h)
        grid_v = self._regrid(lon_n, lat, v, bounds, target_w, target_h)
        tg = self._get_target_grid(bounds, target_w, target_h)

        cfg = COLOR_SCALES.get(param, COLOR_SCALES["wind_speed"])
        grid_speed = self._smooth_field(grid_speed, style["smooth_sigma"])
        grid_u = self._smooth_field(grid_u, style["smooth_sigma"])
        grid_v = self._smooth_field(grid_v, style["smooth_sigma"])
        cmap = plt.get_cmap(cfg["cmap"]).copy()
        cmap.set_under((0, 0, 0, 0))
        cmap.set_bad((0, 0, 0, 0))
        norm = mcolors.PowerNorm(
            gamma=style["fill_gamma"],
            vmin=cfg["vmin"],
            vmax=cfg["vmax"],
        )
        valid_domain_mask = np.isfinite(grid_speed)
        shaded = np.where(grid_speed >= style["fill_floor"], grid_speed, np.nan)
        normalized = norm(shaded)
        rgba = (cmap(normalized) * 255).astype(np.uint8)
        nan_mask = np.isnan(shaded)
        rgba[nan_mask] = 0
        alpha = np.clip(
            (np.nan_to_num(grid_speed, nan=0.0) - style["fill_floor"]) /
            max(cfg["vmax"] - style["fill_floor"], 1.0),
            0, 1,
        )
        if style["alpha_gamma"] != 1.0:
            alpha = np.power(alpha, style["alpha_gamma"])
        base_alpha = np.where(
            nan_mask,
            0.0,
            style["alpha_min"] +
            alpha * (style["alpha_max"] - style["alpha_min"]),
        )

        # Feather the inner edge of the data domain so fill doesn't end
        # abruptly where the model grid stops.
        if np.ndim(lon) == 2 and np.any(~valid_domain_mask):
            edge_dist = distance_transform_edt(valid_domain_mask).astype(np.float32)
            feather_px = max(8, int(min(rgba.shape[:2]) * 0.025))
            edge_fade = np.clip(edge_dist / feather_px, 0, 1)
            base_alpha = base_alpha * edge_fade

        rgba[:, :, 3] = np.clip(base_alpha, 0, 255).astype(np.uint8)

        if np.ndim(lon) == 2:
            rgba = self._extend_outside_domain(
                rgba,
                ~valid_domain_mask,
                fade_start=0.0,
                fade_end=0.45,
                apply_alpha_fade=True,
            )

        ax.imshow(
            rgba,
            extent=(west, east, merc_south, merc_north),
            origin="upper",
            interpolation="bilinear",
            zorder=1,
        )

        if contour_param and contour_dict:
            self._draw_height_contours(ax, contour_param, contour_dict, bounds, target_w, target_h, zorder=1.7, alpha_scale=0.9)

        lon_grid = tg["lon"]
        lat_merc_grid = _lat_to_merc(tg["lat"])

        # -- Isotach contours (major only — no minor clutter) ---------------
        major_levels = [
            float(level) for level in style["major_contours"]
            if cfg["vmin"] < level < cfg["vmax"]
        ]
        if major_levels:
            m_color = style.get("major_color", "#ffffff")
            m_width = style.get("major_width", 0.7)
            m_alpha = style.get("major_alpha", 0.45)
            m_stroke = style.get("major_stroke", 0.15)
            cs_major = ax.contour(
                lon_grid, lat_merc_grid, grid_speed,
                levels=major_levels,
                colors=m_color,
                linewidths=m_width,
                alpha=m_alpha,
                zorder=2,
            )
            for coll in getattr(cs_major, "collections", []):
                coll.set_path_effects([
                    pe.Stroke(
                        linewidth=m_width + 0.5,
                        foreground=(0, 0, 0, m_stroke),
                    ),
                    pe.Normal(),
                ])

        # -- Wind barbs with outline for contrast on any background --------
        u_earth, v_earth = self._rotate_to_earth_relative(lon, lat, tg, grid_u, grid_v)

        target_cols = style.get("target_cols", 24)
        skip = max(1, grid_speed.shape[1] // target_cols)

        slc = (slice(None, None, skip), slice(None, None, skip))
        b_lon = lon_grid[slc]
        b_lat = lat_merc_grid[slc]
        b_u = u_earth[slc]
        b_v = v_earth[slc]
        b_spd = grid_speed[slc]

        valid = (b_spd >= style["barb_floor"]) & np.isfinite(b_u) & np.isfinite(b_v)
        if np.any(valid):
            barb_color = style.get("barb_color", "#222222")
            barb_lw = style.get("barb_lw", 0.45)
            outline_color = style.get("barb_outline_color", "white")
            outline_lw = style.get("barb_outline_lw", 0.15)
            outline_alpha = style.get("barb_outline_alpha", 0.5)

            # Very light keyline for contrast without producing a fuzzy double-stroke.
            ax.barbs(
                b_lon[valid], b_lat[valid], b_u[valid], b_v[valid],
                length=style["barb_length"],
                linewidth=barb_lw + outline_lw,
                color=outline_color,
                alpha=outline_alpha,
                zorder=3,
            )
            # Crisp primary barb on top.
            ax.barbs(
                b_lon[valid], b_lat[valid], b_u[valid], b_v[valid],
                length=style["barb_length"],
                linewidth=barb_lw,
                color=barb_color,
                zorder=4,
            )

        return self._mpl_to_pil(fig, output_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render_overlay(self, param, data_dict, output_path):
        """Render a single parameter as an RGBA PNG overlay."""
        if param in ("mslp", "psfc"):
            return self._render_mslp(data_dict, output_path, param=param)
        if param in _HEIGHT_CONTOUR_PARAMS:
            return self._render_height_field(param, data_dict, output_path)
        scale_cfg = COLOR_SCALES.get(param, {})
        if scale_cfg.get("render") == "contour":
            return self._render_scalar_contour_only(param, data_dict, output_path)
        if scale_cfg.get("contours"):
            return self._render_fill_with_scalar_contours(param, data_dict, output_path)
        return self._render_fill(param, data_dict, output_path)

    def get_data_bounds(self, lat, lon):
        """Return image bounds for Leaflet ImageOverlay."""
        if self._current_model_name in _FIXED_CONUS_BOUNDS_MODELS:
            return DATA_SOUTH, DATA_NORTH, DATA_WEST, DATA_EAST
        lon_n = np.where(lon > 180, lon - 360, lon)
        south = float(np.nanmin(lat))
        north = float(np.nanmax(lat))
        west = float(np.nanmin(lon_n))
        east = float(np.nanmax(lon_n))
        return south, north, west, east

    def render_all_params(self, processed, output_dir, fxx,
                          run_time=None, model_name: str = "",
                          selected_params: set[str] | None = None):
        """Render all available parameters as overlays.

        Returns list of successfully rendered param names.
        """
        rendered = []
        selected = set(selected_params) if selected_params is not None else None
        fxx_str = f"f{fxx:02d}"
        self._current_model_name = (model_name or "").lower()

        from .processor import DERIVED_PARAMS as ALL_DERIVED

        wind_vector_params = {
            param for param, (u_key, v_key) in ALL_DERIVED.items()
            if param.startswith("wind_") and v_key is not None
        }
        vector_component_params = {
            key for pair in ALL_DERIVED.values() for key in pair if key is not None
        }
        _SKIP_PARAMS = {
            *wind_vector_params,
            *vector_component_params,
            "ushear_06km", "vshear_06km", "ushear_01km", "vshear_01km",
        }

        # Save bounds metadata on first render (before param loop so
        # it is written even if a later render step raises an exception)
        bounds_file = output_dir / "bounds.json"
        if processed:
            import json
            first = next(iter(processed.values()))
            s, n, w, e = self.get_data_bounds(first["lat"], first["lon"])
            bounds_file.write_text(json.dumps({
                "south": s, "north": n, "west": w, "east": e,
            }))

        # Build set of params still needed by later render phases (wind
        # overlays, derived scalars) so we can free everything else early.
        _needed_later = set()
        for _wp, (_uk, _vk) in ALL_DERIVED.items():
            if _vk is not None and {_wp, _uk, _vk} <= set(processed):
                _needed_later.update((_wp, _uk, _vk))
                _cp = _SCALAR_HEIGHT_OVERLAYS.get(_wp)
                if _cp and _cp in processed:
                    _needed_later.add(_cp)
            elif not _wp.startswith("wind_") and _wp in processed:
                _needed_later.add(_wp)
        # Height contour params used by scalar fills are also needed later
        for _p in list(processed):
            _cp = _SCALAR_HEIGHT_OVERLAYS.get(_p)
            if _cp and _cp in processed:
                _needed_later.add(_cp)

        for param in list(processed):
            field = processed[param]
            if selected is not None and param not in selected:
                continue
            if param in _SKIP_PARAMS:
                if param not in _needed_later:
                    del processed[param]
                continue
            out_path = output_dir / param / f"{fxx_str}.png"
            if out_path.exists():
                rendered.append(param)
                if param not in _needed_later:
                    del processed[param]
                continue
            contour_param = _SCALAR_HEIGHT_OVERLAYS.get(param)
            if contour_param and contour_param in processed:
                ok = self._render_fill_with_height_contours(
                    param,
                    field,
                    contour_param,
                    processed[contour_param],
                    out_path,
                )
            else:
                ok = self.render_overlay(param, field, out_path)
            if ok:
                rendered.append(param)
            if param not in _needed_later:
                del processed[param]

        for wind_param, (u_key, v_key) in ALL_DERIVED.items():
            if not wind_param.startswith("wind_") or v_key is None:
                continue
            if selected is not None and wind_param not in selected:
                continue
            if {wind_param, u_key, v_key} <= set(processed):
                out_path = output_dir / wind_param / f"{fxx_str}.png"
                if not out_path.exists():
                    try:
                        contour_param = _SCALAR_HEIGHT_OVERLAYS.get(wind_param)
                        ok = self._render_wind_overlay(
                            wind_param,
                            processed[wind_param],
                            processed[u_key],
                            processed[v_key],
                            out_path,
                            contour_param=contour_param,
                            contour_dict=processed.get(contour_param) if contour_param else None,
                        )
                        if ok:
                            rendered.append(wind_param)
                    except Exception as exc:
                        logger.warning("%s wind overlay failed: %s", wind_param, exc)
                else:
                    rendered.append(wind_param)
            # Free wind components after use
            for _k in (wind_param, u_key, v_key):
                processed.pop(_k, None)

        # Derived scalar magnitudes (shear, etc.) that still use fill rendering.
        for derived, (u_key, v_key) in ALL_DERIVED.items():
            if derived.startswith("wind_"):
                continue
            if selected is not None and derived not in selected:
                continue
            if derived in processed:
                out_path = output_dir / derived / f"{fxx_str}.png"
                if not out_path.exists():
                    ok = self.render_overlay(
                        derived, processed[derived], out_path)
                    if ok:
                        rendered.append(derived)
                else:
                    rendered.append(derived)
                del processed[derived]

        self._current_model_name = ""
        return rendered
