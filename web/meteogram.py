"""Multi-model point forecasts (meteogram plume) sampled from rendered overlays.

Rendered PNGs are retained for every run (GRIBs only for two), so a point
series is read back from them through the same palette reverse-mapping as
``/api/value-png``. Decoding a full-resolution PNG per forecast hour is too
slow to repeat per click, so each frame is decoded once into a downsampled
float16 value grid under ``data/meteogram_cache``; point lookups then
memory-map those grids and touch one page per frame.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from pipeline.config import COLOR_SCALES, DATA_DIR, RENDERED_DIR

logger = logging.getLogger(__name__)

# Bump GRID_VERSION when decoding changes so stale grids are never read.
GRID_VERSION = 1
CACHE_DIR = DATA_DIR / "meteogram_cache" / f"v{GRID_VERSION}"
MAX_GRID_DIM = 600          # HRRR/NAM 3km (1799 px wide) -> stride 3 (~9 km)
CACHE_RUNS_PER_MODEL = 2

# Plume order: convection-allowing first, then regional, then global.
MODELS: dict[str, str] = {
    "hrrr": "HRRR",
    "rrfs": "RRFS",
    "nam3km": "NAM 3km",
    "rap": "RAP",
    "nam12km": "NAM 12km",
    "href": "HREF Mean",
    "gfs": "GFS",
    "gefs": "GEFS Mean",
}
# ECMWF and GDPS are left out until their overlays are fixed: as of
# 2026-10-16 every retained run of both renders a longitude-shifted field
# (Asia drawn inside the CONUS bounds), so sampled values are wrong.

# wind_speed is omitted: its overlay draws barbs over an alpha-ramped fill,
# which cannot be reverse-mapped to a value reliably.
PARAMS = ("temp_2m", "dewpoint_2m", "wind_gusts", "cloud_cover", "precip")

# Transparent pixels inside the domain mean "below the lowest drawn bin" for
# these parameters (renderer set_under); elsewhere they mean no data.
CLEAR_VALUE = {"precip": 0.0}

# Models whose ``precip`` frames are per-forecast-hour amounts (the
# scheduler differences consecutive accumulations). Other models render the
# raw GRIB accumulation, whose window varies by model and forecast hour.
HOURLY_PRECIP_MODELS = {"hrrr", "rap"}

_CLEAR = np.float16(-np.inf)
RgbResolver = Callable[[tuple, str, float, float, str, int], "float | None"]


def _run_dir(model: str, run_id: str) -> Path:
    return RENDERED_DIR / model / run_id


def _frames(param_dir: Path) -> dict[int, Path]:
    """Hourly frame PNGs keyed by forecast hour (subhourly files skipped)."""
    frames: dict[int, Path] = {}
    try:
        entries = os.scandir(param_dir)
    except OSError:
        return frames
    with entries:
        for entry in entries:
            name = entry.name
            if name.startswith("f") and name.endswith(".png") and name[1:-4].isdigit():
                frames[int(name[1:-4])] = Path(entry.path)
    return frames


def _mercator(lat: float) -> float:
    return math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def _grid_name(fxx: int, width: int, height: int, stride: int) -> str:
    return f"f{fxx:03d}_{width}x{height}s{stride}.npy"


def _parse_grid_name(name: str) -> tuple[int, int, int, int] | None:
    try:
        stem, dims = name[:-4].split("_", 1)
        size, stride = dims.split("s")
        width, height = size.split("x")
        return int(stem[1:]), int(width), int(height), int(stride)
    except ValueError:
        return None


def _cached_grids(cache_dir: Path) -> dict[int, tuple[Path, int, int, int]]:
    grids: dict[int, tuple[Path, int, int, int]] = {}
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return grids
    for name in names:
        if not name.endswith(".npy"):
            continue
        parsed = _parse_grid_name(name)
        if parsed is not None:
            fxx, width, height, stride = parsed
            grids[fxx] = (cache_dir / name, width, height, stride)
    return grids


def build_grid(
    png_path: Path, param: str, palette_version: int, rgb_to_value: RgbResolver,
    memo: dict[int, float] | None = None,
) -> tuple[np.ndarray, int, int, int] | None:
    """Decode one overlay PNG into a downsampled float16 value grid.

    ``memo`` maps packed RGBA to value; frames of one parameter and palette
    share most colors, so reusing it skips nearly all palette searches.
    """
    from PIL import Image

    scale = COLOR_SCALES.get(param) or {}
    cmap_name = scale.get("cmap")
    if cmap_name is None:
        return None
    vmin, vmax = scale.get("vmin", 0), scale.get("vmax", 1)
    with Image.open(png_path) as img:
        rgba = np.asarray(img.convert("RGBA"))
    height, width = rgba.shape[:2]
    stride = max(1, math.ceil(max(width, height) / MAX_GRID_DIM))
    offset = stride // 2
    sub = rgba[offset::stride, offset::stride]
    packed = (
        sub[..., 0].astype(np.uint32) << 24 | sub[..., 1].astype(np.uint32) << 16
        | sub[..., 2].astype(np.uint32) << 8 | sub[..., 3].astype(np.uint32)
    )
    colors, inverse = np.unique(packed, return_inverse=True)
    memo = {} if memo is None else memo
    values = np.empty(colors.shape[0], dtype=np.float32)
    for i, color in enumerate(colors.tolist()):
        value = memo.get(color)
        if value is None:
            if (color & 0xFF) < 128:
                value = -np.inf
            else:
                rgb = ((color >> 24) & 0xFF, (color >> 16) & 0xFF, (color >> 8) & 0xFF)
                resolved = rgb_to_value(rgb, cmap_name, vmin, vmax, param, palette_version)
                value = np.nan if resolved is None else resolved
            memo[color] = value
        values[i] = value
    grid = values[inverse.reshape(sub.shape[:2])].astype(np.float16)
    return grid, width, height, stride


def _write_grid(cache_dir: Path, fxx: int, built: tuple[np.ndarray, int, int, int]) -> None:
    grid, width, height, stride = built
    cache_dir.mkdir(parents=True, exist_ok=True)
    for stale in cache_dir.glob(f"f{fxx:03d}_*.npy"):
        stale.unlink(missing_ok=True)
    target = cache_dir / _grid_name(fxx, width, height, stride)
    temporary = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    with temporary.open("wb") as handle:
        np.save(handle, grid)
    temporary.replace(target)


def _missing_frames(model: str, run_id: str, params: list[str]) -> list[tuple[str, int, Path]]:
    missing = []
    run_dir = _run_dir(model, run_id)
    for param in params:
        cached = _cached_grids(CACHE_DIR / model / run_id / param)
        for fxx, png in _frames(run_dir / param).items():
            entry = cached.get(fxx)
            try:
                if entry is None or entry[0].stat().st_mtime < png.stat().st_mtime:
                    missing.append((param, fxx, png))
            except OSError:
                missing.append((param, fxx, png))
    return missing


def run_params(model: str, run_id: str) -> list[str]:
    run_dir = _run_dir(model, run_id)
    return [p for p in PARAMS if (run_dir / p).is_dir() and COLOR_SCALES.get(p, {}).get("cmap")]


def needs_build(model: str, run_id: str) -> bool:
    return bool(_missing_frames(model, run_id, run_params(model, run_id)))


def build_run(
    model: str, run_id: str, palette_version: int, rgb_to_value: RgbResolver,
) -> int:
    """Build every missing grid for a run; returns the number built."""
    built_count = 0
    memos: dict[str, dict[int, float]] = {}
    for param, fxx, png in _missing_frames(model, run_id, run_params(model, run_id)):
        try:
            built = build_grid(png, param, palette_version, rgb_to_value, memos.setdefault(param, {}))
        except Exception as exc:  # A frame being rewritten mid-read is retried later.
            logger.warning("meteogram grid %s/%s/%s/f%02d failed: %s", model, run_id, param, fxx, exc)
            continue
        if built is not None:
            _write_grid(CACHE_DIR / model / run_id / param, fxx, built)
            built_count += 1
    _prune_model_cache(model)
    return built_count


def _prune_model_cache(model: str) -> None:
    """Keep cached grids only for the newest runs that still have overlays."""
    model_dir = CACHE_DIR / model
    try:
        runs = sorted((p for p in model_dir.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        return
    for index, run_path in enumerate(runs):
        if index >= CACHE_RUNS_PER_MODEL or not _run_dir(model, run_path.name).is_dir():
            shutil.rmtree(run_path, ignore_errors=True)


def _bounds(model: str, run_id: str) -> dict | None:
    try:
        return json.loads((_run_dir(model, run_id) / "bounds.json").read_text())
    except (OSError, ValueError):
        return None


def in_domain(model: str, run_id: str, lat: float, lon: float) -> bool:
    bounds = _bounds(model, run_id)
    return bool(bounds) and bounds["south"] <= lat <= bounds["north"] and bounds["west"] <= lon <= bounds["east"]


def sample_run(model: str, run_id: str, lat: float, lon: float, init_time: str | None) -> dict:
    """Read the cached grids for one run at a point (caller checks the domain)."""
    bounds = _bounds(model, run_id)
    x_frac = (lon - bounds["west"]) / (bounds["east"] - bounds["west"])
    merc_north, merc_south = _mercator(bounds["north"]), _mercator(bounds["south"])
    y_frac = (merc_north - _mercator(lat)) / (merc_north - merc_south)

    series: dict[str, list] = {}
    units: dict[str, str] = {}
    for param in run_params(model, run_id):
        points = []
        for fxx, (path, width, height, stride) in sorted(_cached_grids(CACHE_DIR / model / run_id / param).items()):
            try:
                grid = np.load(path, mmap_mode="r")
            except (OSError, ValueError):
                continue
            px = min(max(int(x_frac * width), 0), width - 1)
            py = min(max(int(y_frac * height), 0), height - 1)
            offset = stride // 2
            col = min(max(round((px - offset) / stride), 0), grid.shape[1] - 1)
            row = min(max(round((py - offset) / stride), 0), grid.shape[0] - 1)
            value = float(grid[row, col])
            if value == -math.inf:
                value = CLEAR_VALUE.get(param)
            elif math.isnan(value):
                value = None
            points.append([fxx, None if value is None else round(value, 2)])
        if any(v is not None for _, v in points):
            series[param] = points
            units[param] = COLOR_SCALES.get(param, {}).get("unit", "")

    return {
        "model": model,
        "label": MODELS.get(model, model.upper()),
        "run_id": run_id,
        "init_time": init_time or _init_from_run_id(run_id),
        "lat": round(lat, 4),
        "lon": round(lon, 4),
        "series": series,
        "units": units,
        "precip_hourly": model in HOURLY_PRECIP_MODELS,
    }


def _init_from_run_id(run_id: str) -> str:
    return datetime.strptime(run_id, "%Y%m%d%H").replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def select_run(runs: list[dict], max_age_hours: float = 36.0) -> dict | None:
    """Newest complete run, else the newest run with the most rendered hours.

    Runs older than ``max_age_hours`` are ignored so a stalled model does not
    plot a stale forecast beside current ones.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    fresh = []
    for run in runs:
        try:
            init = datetime.strptime(run["run_id"], "%Y%m%d%H").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if init >= cutoff:
            fresh.append(run)
    fresh.sort(key=lambda r: r["run_id"], reverse=True)
    for run in fresh:
        if run.get("is_complete"):
            return run
    return max(fresh, key=lambda r: len(r.get("completed_fxx") or []), default=None)


class GridWarmer:
    """Background builder for the newest run of each model.

    One API worker wins a non-blocking file lock and warms grids at idle CPU
    priority so the first click on a fresh run is served from cache.
    """

    def __init__(self, scan_runs: Callable[[str], list[dict]],
                 palette_version: Callable[[str, str], int], rgb_to_value: RgbResolver,
                 interval: float = 90.0) -> None:
        self._scan_runs = scan_runs
        self._palette_version = palette_version
        self._rgb_to_value = rgb_to_value
        self._interval = interval
        self._lock_handle = None

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="meteogram-warmer").start()

    def _acquire(self) -> bool:
        import fcntl

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        handle = (CACHE_DIR / "warmer.lock").open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        self._lock_handle = handle
        return True

    def _run(self) -> None:
        time.sleep(30)  # Let the API workers finish starting first.
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except (AttributeError, OSError):
            pass
        while self._lock_handle is None and not self._acquire():
            time.sleep(self._interval * 4)
        while True:
            for model in MODELS:
                try:
                    run = select_run(self._scan_runs(model))
                    if run is not None:
                        run_id = run["run_id"]
                        build_run(model, run_id, self._palette_version(model, run_id), self._rgb_to_value)
                except Exception:
                    logger.exception("meteogram warm failed for %s", model)
            time.sleep(self._interval)
