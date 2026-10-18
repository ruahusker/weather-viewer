"""Surface precipitation type for radar, from the HRRR model.

The model pipeline publishes HRRR's precip-type codes per forecast hour as
``rendered/hrrr/<run>/ptype_grid/fXX.npz`` (see OverlayRenderer.build_ptype_grid).
Radar scans are colored with the newest HRRR run that covers the scan time.
Codes: 0 rain, 1 snow, 2 sleet, 3 freezing rain, 255 outside the model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import math
from pathlib import Path

import numpy as np

from pipeline.config import RENDERED_DIR

logger = logging.getLogger(__name__)

# Newest HRRR runs to try (HRRR posts roughly 1-1.5 h after its run hour) and
# the longest forecast lead accepted for coloring a scan.
_MAX_RUNS_BACK = 6
_MAX_LEAD_HOURS = 18
_EARTH_KM_PER_DEG = 111.195
_cache: dict[Path, tuple[float, np.ndarray, tuple[float, float, float, float]]] = {}


def _merc(lat_deg):
    return np.log(np.tan(np.pi / 4 + np.radians(lat_deg) / 2))


def find_grid(scan_time: datetime | None, root: Path = RENDERED_DIR) -> Path | None:
    """Return the HRRR precip-type grid closest in time from the newest run."""
    if scan_time is None:
        return None
    if scan_time.tzinfo is None:
        scan_time = scan_time.replace(tzinfo=timezone.utc)
    base = scan_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for runs_back in range(_MAX_RUNS_BACK + 1):
        run = base - timedelta(hours=runs_back)
        lead = round((scan_time - run).total_seconds() / 3600.0)
        if not 0 <= lead <= _MAX_LEAD_HOURS:
            continue
        path = root / "hrrr" / run.strftime("%Y%m%d%H") / "ptype_grid" / f"f{lead:02d}.npz"
        if path.exists():
            return path
    return None


def load_grid(path: Path):
    """Load (codes, bounds) with a small mtime-checked cache."""
    mtime = path.stat().st_mtime
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1], cached[2]
    with np.load(path) as data:
        codes = np.asarray(data["codes"], dtype=np.uint8)
        bounds = tuple(float(v) for v in data["bounds"])
    _cache[path] = (mtime, codes, bounds)
    while len(_cache) > 3:
        _cache.pop(next(iter(_cache)))
    return codes, bounds


def gate_types(site_lat: float, site_lon: float, azimuth_deg: np.ndarray, n_gates: int,
               max_range_km: float, tilt_deg: float, codes: np.ndarray,
               bounds: tuple[float, float, float, float]) -> np.ndarray:
    """Precip-type code for every gate of a sweep (n_az x n_gates, float with NaN).

    Gate positions use a flat-earth projection from the radar, which is well
    within one model grid cell over a radar's ~230 km range.
    """
    south, north, west, east = bounds
    height, width = codes.shape
    ranges = (np.arange(n_gates, dtype=np.float64) + 0.5) * (max_range_km / n_gates)
    ground = ranges * math.cos(math.radians(tilt_deg))
    az = np.radians(np.asarray(azimuth_deg, dtype=np.float64))[:, None]
    lat = site_lat + (ground[None, :] * np.cos(az)) / _EARTH_KM_PER_DEG
    lon = site_lon + (ground[None, :] * np.sin(az)) / (
        _EARTH_KM_PER_DEG * math.cos(math.radians(site_lat)))
    x = np.floor((lon - west) / (east - west) * width).astype(np.int64)
    y = np.floor((_merc(north) - _merc(lat)) / (_merc(north) - _merc(south)) * height).astype(np.int64)
    inside = (x >= 0) & (x < width) & (y >= 0) & (y < height)
    result = np.full(lat.shape, np.nan, dtype=np.float32)
    picked = codes[np.clip(y, 0, height - 1), np.clip(x, 0, width - 1)].astype(np.float32)
    picked[picked == 255] = np.nan
    result[inside] = picked[inside]
    return result
