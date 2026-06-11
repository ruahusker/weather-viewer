"""Level II radar renderer — numpy+PIL polar-to-Cartesian."""

import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from .color_tables import FIELD_CONFIG
from .config import RADAR_RENDERED_DIR, OUTPUT_SIZE, CC_CLUTTER_THRESHOLD, PNG_OPTIMIZE, FAST_RENDER_PRODUCTS

logger = logging.getLogger(__name__)

# Geometry cache: (n_gates, half_size) → (pixel_az_flat, gate_idx, valid_mask)
# Contains the parts that depend only on grid geometry, not per-scan azimuths.
_geom_cache: dict = {}

# Full lookup cache: (n_az, n_gates, half_size, az_hash) → az_idx
# For a given site+tilt, azimuth arrays are essentially identical scan-to-scan,
# so the expensive per-pixel nearest-az search is cached after the first scan.
_full_lookup_cache: OrderedDict = OrderedDict()
_FULL_LOOKUP_CACHE_MAX_ENTRIES = 12
_AZ_HASH_ROUND_DECIMALS = 1
_GEOM_CACHE_MAX_ENTRIES = 8

# Mercator-correct lookup cache:
# (n_az, n_gates, size, az_hash, site_lat_r2, site_lon_r2, max_range_km) → (az_idx, gate_idx, valid_mask)
_merc_lookup_cache: OrderedDict = OrderedDict()
_MERC_LOOKUP_CACHE_MAX_ENTRIES = 16


def _prune_geom_cache() -> None:
    while len(_geom_cache) > _GEOM_CACHE_MAX_ENTRIES:
        try:
            _geom_cache.pop(next(iter(_geom_cache)))
        except StopIteration:
            break


def _az_hash(az_array: np.ndarray) -> bytes:
    """Cheap fingerprint of an azimuth array (every ~20th value)."""
    step = max(1, len(az_array) // 20)
    sample = np.asarray(az_array[::step], dtype=np.float32)
    if _AZ_HASH_ROUND_DECIMALS >= 0:
        sample = np.round(sample, decimals=_AZ_HASH_ROUND_DECIMALS)
    return sample.tobytes()


def _cache_lookup_az_idx(full_key):
    cached = _full_lookup_cache.get(full_key)
    if cached is None:
        return None
    _full_lookup_cache.move_to_end(full_key)
    return cached


def _cache_store_az_idx(full_key, az_idx: np.ndarray) -> None:
    _full_lookup_cache[full_key] = az_idx
    _full_lookup_cache.move_to_end(full_key)
    while len(_full_lookup_cache) > _FULL_LOOKUP_CACHE_MAX_ENTRIES:
        _full_lookup_cache.popitem(last=False)


def clear_renderer_caches():
    """Clear all global geometry and lookup caches to reclaim memory."""
    _geom_cache.clear()
    _full_lookup_cache.clear()
    _merc_lookup_cache.clear()
    logger.info("Radar renderer caches cleared")


def build_polar_lookup(n_az: int, n_gates: int, half_size: int,
                       az_array: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Precompute (y,x) → (az_index, gate_index) lookup tables.

    Two-level cache:
    1. _geom_cache: pixel angles + gate indices (depend only on n_gates, half_size).
    2. _full_lookup_cache: full az_idx (adds per-scan azimuth matching), keyed on
       (n_az, n_gates, half_size, az_fingerprint).  For a given site+tilt the
       azimuth array is nearly identical every scan, so this hit rate is ~100%.
    """
    full_key = (n_az, n_gates, half_size, _az_hash(az_array))
    geom_key = (n_gates, half_size)
    if geom_key in _geom_cache:
        pixel_az_flat, gate_idx, valid_mask = _geom_cache[geom_key]
    else:
        size = 2 * half_size
        y_coords = np.arange(size, dtype=np.float32) - half_size
        x_coords = np.arange(size, dtype=np.float32) - half_size
        xx, yy = np.meshgrid(x_coords, y_coords)

        r = np.sqrt(xx**2 + yy**2)
        pixel_az = (np.degrees(np.arctan2(xx, -yy)) + 360) % 360
        pixel_az_flat = pixel_az.ravel()

        gate_idx = (r / half_size * n_gates).astype(np.int32)
        valid_mask = gate_idx < n_gates
        gate_idx[~valid_mask] = 0

        _geom_cache[geom_key] = (pixel_az_flat, gate_idx, valid_mask)
        _prune_geom_cache()

    cached_az_idx = _cache_lookup_az_idx(full_key)
    if cached_az_idx is not None:
        return cached_az_idx, gate_idx, valid_mask

    # Nearest-neighbor azimuth matching
    sort_order = np.argsort(az_array)
    sorted_az = az_array[sort_order]
    insert_idx = np.searchsorted(sorted_az, pixel_az_flat)
    insert_idx = np.clip(insert_idx, 0, len(sorted_az) - 1)
    prev_idx = np.clip(insert_idx - 1, 0, len(sorted_az) - 1)

    d1 = np.abs(sorted_az[insert_idx] - pixel_az_flat)
    d2 = np.abs(sorted_az[prev_idx] - pixel_az_flat)
    d1 = np.minimum(d1, 360 - d1)
    d2 = np.minimum(d2, 360 - d2)

    best = np.where(d1 <= d2, insert_idx, prev_idx)
    az_idx = sort_order[best].reshape(gate_idx.shape)
    az_idx[~valid_mask] = 0

    _cache_store_az_idx(full_key, az_idx)
    return az_idx, gate_idx, valid_mask


def build_polar_lookup_mercator(
    n_az: int, n_gates: int, size: int, az_array: np.ndarray,
    site_lat: float, site_lon: float, max_range_km: float,
    north: float, south: float, west: float, east: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Precompute (row,col) → (az_index, gate_index) for a Mercator-correct image.

    Leaflet's imageOverlay maps pixels linearly in Mercator Y (not in degrees of
    latitude), so naive equirectangular rendering causes a systematic northward
    displacement of echoes.  This function maps each pixel to the correct
    geographic position by inverting the Mercator transform, then converts to
    radar polar coordinates.
    """
    site_lat_r2 = round(site_lat, 2)
    site_lon_r2 = round(site_lon, 2)
    merc_key = (n_az, n_gates, size, _az_hash(az_array), site_lat_r2, site_lon_r2, max_range_km)

    cached = _merc_lookup_cache.get(merc_key)
    if cached is not None:
        _merc_lookup_cache.move_to_end(merc_key)
        return cached

    # Mercator Y at the north and south edges of the bounding box.
    # Leaflet maps row 0 → north, row size-1 → south, linearly in Mercator Y.
    merc_y_north = np.log(np.tan(np.pi / 4 + np.radians(north) / 2))
    merc_y_south = np.log(np.tan(np.pi / 4 + np.radians(south) / 2))

    rows = np.arange(size, dtype=np.float64)
    cols = np.arange(size, dtype=np.float64)

    # Inverse Mercator: each row → latitude
    merc_y_row = merc_y_north + (rows / size) * (merc_y_south - merc_y_north)
    lat_row = np.degrees(2.0 * np.arctan(np.exp(merc_y_row)) - np.pi / 2)  # (size,)

    # Each column → longitude (linear in degrees, same as Mercator X)
    lon_col = west + (cols / size) * (east - west)  # (size,)

    R = 6371.0
    # North-south distance from site for each row (km), shape (size, 1)
    dy_km = ((lat_row - site_lat) * (np.pi / 180.0) * R).reshape(size, 1)
    # East-west distance from site for each col (km), shape (1, size)
    # Use site latitude for the cos correction (flat-Earth approximation is fine at this scale)
    dx_km = ((lon_col - site_lon) * (np.pi / 180.0) * R * np.cos(np.radians(site_lat))).reshape(1, size)

    r = np.sqrt(dx_km ** 2 + dy_km ** 2).astype(np.float32)  # (size, size)
    pixel_az = ((np.degrees(np.arctan2(dx_km, dy_km)) + 360.0) % 360.0).astype(np.float32)
    pixel_az_flat = pixel_az.ravel()

    gate_idx = (r / max_range_km * n_gates).astype(np.int32)
    valid_mask = gate_idx < n_gates
    gate_idx[~valid_mask] = 0

    # Nearest-neighbor azimuth matching (same logic as build_polar_lookup)
    sort_order = np.argsort(az_array)
    sorted_az = az_array[sort_order]
    insert_idx = np.searchsorted(sorted_az, pixel_az_flat)
    insert_idx = np.clip(insert_idx, 0, len(sorted_az) - 1)
    prev_idx = np.clip(insert_idx - 1, 0, len(sorted_az) - 1)

    d1 = np.abs(sorted_az[insert_idx] - pixel_az_flat)
    d2 = np.abs(sorted_az[prev_idx] - pixel_az_flat)
    d1 = np.minimum(d1, 360.0 - d1)
    d2 = np.minimum(d2, 360.0 - d2)

    best = np.where(d1 <= d2, insert_idx, prev_idx)
    az_idx = sort_order[best].reshape(gate_idx.shape)
    az_idx[~valid_mask] = 0

    result = (az_idx, gate_idx, valid_mask)
    _merc_lookup_cache[merc_key] = result
    _merc_lookup_cache.move_to_end(merc_key)
    while len(_merc_lookup_cache) > _MERC_LOOKUP_CACHE_MAX_ENTRIES:
        _merc_lookup_cache.popitem(last=False)

    return result


def render_sweep_to_png(data: np.ndarray,
                        az: np.ndarray,
                        lut: np.ndarray,
                        vmin: float,
                        vmax: float,
                        mask_below: Optional[float],
                        output_size: int = 1800,
                        cc_data: Optional[np.ndarray] = None,
                        cc_threshold: float = 0.3,
                        bounds: Optional[dict] = None) -> Image.Image:
    """Render one sweep to a transparent RGBA PIL Image.

    When ``bounds`` is provided (a dict with north/south/east/west/max_range_km/
    center_lat/center_lon keys as returned by ``radar_bounds``), pixels are mapped
    to geographic coordinates using Mercator-correct interpolation so that the
    resulting image displays without positional offset in Leaflet's Web Mercator map.
    """
    n_az, n_gates = data.shape

    if bounds is not None:
        az_idx, gate_idx, valid_mask = build_polar_lookup_mercator(
            n_az, n_gates, output_size, az,
            site_lat=bounds["center_lat"],
            site_lon=bounds["center_lon"],
            max_range_km=bounds["max_range_km"],
            north=bounds["north"],
            south=bounds["south"],
            west=bounds["west"],
            east=bounds["east"],
        )
    else:
        half = output_size // 2
        az_idx, gate_idx, valid_mask = build_polar_lookup(n_az, n_gates, half, az)

    sampled = data[az_idx, gate_idx]
    pixel_mask = valid_mask.copy()

    if hasattr(data, 'mask'):
        pixel_mask &= ~data.mask[az_idx, gate_idx]

    nan_mask = np.isnan(sampled) if not hasattr(sampled, 'mask') else np.isnan(sampled.data)
    pixel_mask &= ~nan_mask

    raw_data = np.asarray(sampled.data if hasattr(sampled, 'data') else sampled, dtype=np.float32)

    # CC-based clutter filter
    if cc_data is not None:
        cc_sampled = cc_data[az_idx, gate_idx]
        cc_raw = np.asarray(cc_sampled.data if hasattr(cc_sampled, 'data') else cc_sampled, dtype=np.float32)
        cc_valid = ~np.isnan(cc_raw)
        if hasattr(cc_data, 'mask'):
            cc_valid &= ~cc_data.mask[az_idx, gate_idx]
        clutter = cc_valid & (cc_raw < cc_threshold)
        pixel_mask &= ~clutter

    if mask_below is not None:
        pixel_mask &= raw_data > mask_below

    normalized = np.clip((raw_data - vmin) / (vmax - vmin), 0, 1)
    indices = (normalized * 255).astype(np.int32)
    indices = np.clip(indices, 0, 255)

    rgba = lut[indices]
    rgba[~pixel_mask] = [0, 0, 0, 0]

    return Image.fromarray(rgba, 'RGBA')


def radar_bounds(site_lat: float, site_lon: float, max_range_km: float) -> dict:
    """Calculate lat/lon bounds for a radar coverage circle."""
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


class _NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


@dataclass
class RenderedRadar:
    """Result of rendering one radar product."""
    png_path: Path
    meta_path: Path
    site: str
    field: str
    tilt: float
    timestamp: str
    bounds: dict


class Level2Renderer:
    """Render Level II radar sweeps to transparent PNG overlays."""

    def __init__(self, output_dir: Path = None, output_size: int = None):
        self.output_dir = output_dir or RADAR_RENDERED_DIR
        self.output_size = output_size or OUTPUT_SIZE
        self.png_dir = self.output_dir / "png"
        self.meta_dir = self.output_dir / "meta"
        self.png_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)

    def render_product(self, sweep, site: str, field: str,
                       timestamp: str, site_lat: float, site_lon: float,
                       is_cc_product: bool = False) -> Optional[RenderedRadar]:
        """Render one product/tilt to PNG + metadata JSON.

        Args:
            sweep: SweepData from parser
            site: 4-letter site code
            field: PyART field name
            timestamp: YYYYMMDD_HHMMSS string
            site_lat, site_lon: radar location
            is_cc_product: if True, skip CC clutter filter (rendering CC itself)
        """
        cfg = FIELD_CONFIG.get(field)
        if cfg is None:
            logger.warning(f"No color config for field '{field}'")
            return None

        lut, vmin, vmax = cfg["lut_fn"]()

        # Convert masked array to float32 with NaN
        data = sweep.data
        if hasattr(data, 'filled'):
            data_np = data.filled(np.nan)
        else:
            data_np = np.array(data, dtype=np.float32)
        data_np = np.asarray(data_np, dtype=np.float32)
        if cfg.get("unit_scale"):
            data_np = data_np * float(cfg["unit_scale"])

        cc_for_filter = None if is_cc_product else sweep.cc_data

        bounds = radar_bounds(site_lat, site_lon, sweep.max_range_km)

        try:
            img = render_sweep_to_png(
                data=data_np,
                az=sweep.azimuth,
                lut=lut,
                vmin=vmin,
                vmax=vmax,
                mask_below=cfg["mask_below"],
                output_size=self.output_size,
                cc_data=cc_for_filter,
                cc_threshold=CC_CLUTTER_THRESHOLD,
                bounds=bounds,
            )
        except Exception as e:
            logger.error(f"Render failed for {site}/{field}: {e}")
            return None

        # File paths
        tilt_str = f"{sweep.tilt_angle:.1f}"
        fname = f"{site}_{cfg['short']}_{tilt_str}_{timestamp}.png"
        png_path = self.png_dir / fname
        meta_path = self.meta_dir / fname.replace(".png", ".json")

        img.save(png_path, optimize=PNG_OPTIMIZE)
        meta = {
            "site": site,
            "field": field,
            "short": cfg["short"],
            "label": cfg["label"],
            "unit": cfg["unit"],
            "tilt": sweep.tilt_angle,
            "timestamp": timestamp,
            "bounds": bounds,
            "png_path": f"radar/png/{fname}",
            "rays": int(sweep.data.shape[0]),
            "gates": sweep.n_gates,
            "max_range_km": sweep.max_range_km,
        }
        meta_path.write_text(json.dumps(meta, indent=2, cls=_NumpyEncoder))

        # Update "latest" symlinks
        latest_png = self.png_dir / f"{site}_{cfg['short']}_latest.png"
        latest_meta = self.meta_dir / f"{site}_{cfg['short']}_latest.json"
        latest_png.unlink(missing_ok=True)
        latest_meta.unlink(missing_ok=True)
        try:
            latest_png.symlink_to(fname)
            latest_meta.symlink_to(fname.replace(".png", ".json"))
        except OSError:
            pass

        return RenderedRadar(
            png_path=png_path,
            meta_path=meta_path,
            site=site,
            field=field,
            tilt=sweep.tilt_angle,
            timestamp=timestamp,
            bounds=bounds,
        )

    def render_lowest_tilt(self, volume, site: str,
                           timestamp: str) -> list[RenderedRadar]:
        """Render all configured radar products at the lowest tilt / volume level.

        Args:
            volume: VolumeData from parser
            site: 4-letter site code
            timestamp: YYYYMMDD_HHMMSS string

        Returns:
            List of RenderedRadar results
        """
        from .derived import extract_radar_product

        results = []
        # Filter to fast-mode subset if configured
        fields = FIELD_CONFIG
        if FAST_RENDER_PRODUCTS is not None:
            fields = {k: v for k, v in FIELD_CONFIG.items() if k in FAST_RENDER_PRODUCTS}
        for field_name, cfg in fields.items():
            prefer_lr = cfg.get("prefer_long_range", False)
            sweep = extract_radar_product(
                volume,
                field_name,
                target_tilt=0.5,
                prefer_long_range=prefer_lr,
            )
            if sweep is None:
                logger.debug(f"{site}: no data for {field_name} at 0.5°")
                continue

            is_cc = (field_name == 'cross_correlation_ratio')
            rendered = self.render_product(
                sweep=sweep,
                site=site,
                field=field_name,
                timestamp=timestamp,
                site_lat=volume.site_lat,
                site_lon=volume.site_lon,
                is_cc_product=is_cc,
            )
            if rendered:
                results.append(rendered)

        return results

    def render_on_demand(self, raw_path: Path, site: str,
                         field: str, tilt: float) -> Optional[RenderedRadar]:
        """Parse a raw file and render a specific field/tilt on demand."""
        from .derived import extract_radar_product
        from .parser import parse_volume

        volume = parse_volume(raw_path)
        if volume is None:
            return None

        cfg = FIELD_CONFIG.get(field)
        if cfg is None:
            return None

        prefer_lr = cfg.get("prefer_long_range", False)
        sweep = extract_radar_product(
            volume,
            field,
            target_tilt=tilt,
            prefer_long_range=prefer_lr,
        )
        if sweep is None:
            return None

        timestamp = volume.timestamp.strftime("%Y%m%d_%H%M%S") if volume.timestamp else "unknown"
        is_cc = (field == 'cross_correlation_ratio')

        return self.render_product(
            sweep=sweep,
            site=site,
            field=field,
            timestamp=timestamp,
            site_lat=volume.site_lat,
            site_lon=volume.site_lon,
            is_cc_product=is_cc,
        )
