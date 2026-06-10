"""FastAPI web backend for the weather model viewer."""
import asyncio
import atexit
import json
import math
import re
import subprocess
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pipeline.config import (
    DATA_DIR, GRIB_CACHE_DIR, RENDERED_DIR, COLOR_SCALES, SUBHOURLY_CONFIG,
    SATELLITE_RENDERED_DIR, SATELLITE_CACHE_DIR, SATELLITE_CONFIG
)

# Import config to trigger colormap registration (wx_temp, etc.)
import pipeline.config

from pipeline.radar.config import RADAR_RENDERED_DIR, RADAR_CACHE_DIR, RADAR_CONFIG, NEXRAD_SITES, LEVEL2_PRODUCTS
from pipeline.radar.color_tables import FIELD_CONFIG as RADAR_FIELD_CONFIG
from pipeline.radar.grid_api import get_radar_grid_binary
from pipeline.models.grid_api import get_model_grid_binary

app = FastAPI(title="Weather Model Viewer")

# ─── Compression ────────────────────────────────────────────────
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ─── Cache headers middleware ───────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

class CacheHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        p = request.url.path
        # Versioned static assets (JS/CSS with ?v= query string) — cache long
        if p.startswith("/static/") and request.url.query:
            ext = p.rsplit(".", 1)[-1] if "." in p else ""
            if ext in ("js", "css", "json"):
                response.headers["Cache-Control"] = "public, max-age=2592000"  # 30 days
        # Static GeoJSON/images without version — cache 1 day
        elif p.startswith("/static/") and p.endswith(".json"):
            response.headers["Cache-Control"] = "public, max-age=86400"
        # Rendered model/radar/satellite images — immutable once created
        elif p.startswith(("/rendered/", "/satellite/", "/radar/")):
            response.headers["Cache-Control"] = "public, max-age=86400"
        # API responses — short cache
        elif p.startswith("/api/"):
            if "/colorscales" in p or "/config" in p:
                response.headers["Cache-Control"] = "public, max-age=86400"
            elif "/runs" in p or "/available/" in p or "/bounds/" in p:
                response.headers["Cache-Control"] = "public, max-age=60"
            elif "/alerts" in p or "/surface/" in p:
                response.headers["Cache-Control"] = "public, max-age=60"
            elif "/sounding/" in p:
                response.headers["Cache-Control"] = "public, max-age=300"
        return response

app.add_middleware(CacheHeaderMiddleware)

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Mount rendered images directories
app.mount("/rendered", StaticFiles(directory=str(RENDERED_DIR)), name="rendered")
app.mount("/satellite", StaticFiles(directory=str(SATELLITE_RENDERED_DIR)), name="satellite")
app.mount("/radar", StaticFiles(directory=str(RADAR_RENDERED_DIR)), name="radar")

PIPELINE_STATE_FILE = DATA_DIR / "pipeline_state.json"
_RADAR_3D_CACHE: dict[str, dict] = {}
HOME_RENDERED_DIR = RENDERED_DIR / "home"
_CMAP_CACHE_MAX = 64
_ZONE_GEOM_CACHE_MAX = 256
_SPC_CACHE_MAX = 32
_SPC_TEXT_CACHE_MAX = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prune_cache(mapping: dict, max_entries: int) -> None:
    """Bound insertion-ordered caches used by the web process."""
    while len(mapping) > max_entries:
        try:
            mapping.pop(next(iter(mapping)))
        except StopIteration:
            break

def _disk_completed_fxx(run_dir: Path, parameters: list[str]) -> list[int]:
    """Determine which forecast hours are fully rendered on disk.
    
    Only considers parameters that actually exist on disk. Parameters missing
    from the GRIB subset (e.g., NAM visibility, VIL) are skipped.
    """
    if not parameters:
        return []
    # An fxx is complete if every EXISTING parameter has its PNG
    fxx_sets = []
    found_any = False
    for param in parameters:
        param_dir = run_dir / param
        if not param_dir.is_dir():
            continue  # Skip params not in GRIB subset
        pngs = {
            int(f.stem[1:])
            for f in param_dir.glob("f*.png")
            if f.stem.startswith("f") and f.stem[1:].isdigit()
        }
        if pngs:
            found_any = True
            fxx_sets.append(pngs)
    if not found_any:
        return []
    return sorted(set.intersection(*fxx_sets))


def _reported_completed_fxx(run_dir: Path, meta: dict) -> list[int]:
    """Return best-known completed forecast hours for display/reporting."""
    forecast_hours = {int(fxx) for fxx in meta.get("forecast_hours", [])}
    parameters = meta.get("parameters", [])
    meta_completed = {int(fxx) for fxx in meta.get("completed_fxx", [])}
    disk_completed = set(_disk_completed_fxx(run_dir, parameters))
    completed = meta_completed | disk_completed
    if forecast_hours:
        completed &= forecast_hours
    return sorted(completed)


# model -> (expires_at, runs list). Incomplete runs change as PNGs land, so
# the whole model answer gets a short TTL; completed runs are immutable and
# their (expensive) per-param disk scans are cached permanently below.
_SCAN_RUNS_TTL = 15.0
_scan_runs_cache: dict[str, tuple[float, list[dict]]] = {}
# (model, run_id) -> summary dict for runs that were complete when scanned.
_complete_run_cache: dict[tuple[str, str], dict] = {}
_COMPLETE_RUN_CACHE_MAX = 256


def _scan_runs(model: str = "hrrr") -> list[dict]:
    cached = _scan_runs_cache.get(model)
    if cached is not None and time.time() < cached[0]:
        return cached[1]

    model_dir = RENDERED_DIR / model
    if not model_dir.exists():
        return []
    runs = []
    for run_dir in sorted(model_dir.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue

        complete_summary = _complete_run_cache.get((model, run_dir.name))
        if complete_summary is not None:
            runs.append(complete_summary)
            continue

        meta_path = run_dir / "run_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                forecast_hours = set(meta.get("forecast_hours", []))
                parameters = meta.get("parameters", [])
                completed_fxx = _reported_completed_fxx(run_dir, meta)
                is_complete = meta.get("is_complete", False) or bool(
                    forecast_hours and set(completed_fxx) >= forecast_hours
                )
                summary = {
                    "run_id": run_dir.name,
                    "model": model,
                    "init_time": meta.get("init_time"),
                    "is_complete": is_complete,
                    "completed_fxx": completed_fxx,
                    "forecast_hours": list(forecast_hours),
                    "parameters": parameters,
                }
                runs.append(summary)
                if is_complete:
                    _complete_run_cache[(model, run_dir.name)] = summary
                    _prune_cache(_complete_run_cache, _COMPLETE_RUN_CACHE_MAX)
            except Exception:
                runs.append({
                    "run_id": run_dir.name,
                    "model": model,
                    "init_time": None,
                    "is_complete": False,
                    "completed_fxx": [],
                    "forecast_hours": [],
                    "parameters": [],
                })
    _scan_runs_cache[model] = (time.time() + _SCAN_RUNS_TTL, runs)
    return runs


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/radar-3d")
async def radar_3d_view():
    return FileResponse(str(_STATIC_DIR / "radar3d.html"))


@app.get("/wx/radar-3d")
async def radar_3d_view_prefixed():
    return FileResponse(str(_STATIC_DIR / "radar3d.html"))


@app.get("/api/status")
async def get_status():
    if PIPELINE_STATE_FILE.exists():
        try:
            return JSONResponse(json.loads(PIPELINE_STATE_FILE.read_text()))
        except Exception:
            pass
    return JSONResponse({"status": "unknown"})


@app.get("/api/runs")
async def list_runs(model: str = "hrrr"):
    return JSONResponse(_scan_runs(model))


@app.get("/api/runs/{model}/{run_id}")
async def get_run(model: str, run_id: str):
    meta_path = RENDERED_DIR / model / run_id / "run_meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        return JSONResponse(json.loads(meta_path.read_text()))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/bounds/{model}/{run_id}")
async def get_bounds(model: str, run_id: str):
    """Return the lat/lon bounds for the overlay images."""
    bounds_path = RENDERED_DIR / model / run_id / "bounds.json"
    if not bounds_path.exists():
        raise HTTPException(status_code=404, detail="Bounds not found")
    try:
        return JSONResponse(json.loads(bounds_path.read_text()))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# (model, run_id) -> (expires_at, payload). Runs marked complete get a long
# expiry (their PNG set no longer changes); active runs a short one.
_AVAILABLE_TTL_ACTIVE = 10.0
_AVAILABLE_TTL_COMPLETE = 3600.0
_available_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_AVAILABLE_CACHE_MAX = 256


@app.get("/api/available/{model}/{run_id}")
async def get_available(model: str, run_id: str):
    """Scan disk for rendered overlay PNGs per parameter.

    Returns {param_name: [fxx_int, ...], ...}
    For subhourly params: {param_name: [fxx_str, ...], ...} where fxx_str can be '01', '01_15min', etc.
    """
    cache_key = (model, run_id)
    cached = _available_cache.get(cache_key)
    if cached is not None and time.time() < cached[0]:
        return JSONResponse(cached[1])

    run_dir = RENDERED_DIR / model / run_id
    result = {}
    if run_dir.exists():
        for param_dir in run_dir.iterdir():
            if param_dir.is_dir() and param_dir.name not in (".", ".."):
                # Check if this is a subhourly param (contains subh in name)
                is_subh = "_subh" in param_dir.name
                
                frames = []
                for f in param_dir.glob("f*.png"):
                    if not f.stem.startswith("f"):
                        continue
                    fxx_part = f.stem[1:]  # Remove 'f' prefix
                    
                    if is_subh:
                        # For subhourly, keep as string (e.g., '01_15min')
                        frames.append(fxx_part)
                    else:
                        # For hourly, convert to int
                        if fxx_part.isdigit():
                            frames.append(int(fxx_part))
                
                if frames:
                    result[param_dir.name] = sorted(frames)

    is_complete = False
    try:
        meta_path = run_dir / "run_meta.json"
        if meta_path.exists():
            is_complete = bool(json.loads(meta_path.read_text()).get("is_complete", False))
    except Exception:
        pass
    ttl = _AVAILABLE_TTL_COMPLETE if is_complete else _AVAILABLE_TTL_ACTIVE
    _available_cache[cache_key] = (time.time() + ttl, result)
    _prune_cache(_available_cache, _AVAILABLE_CACHE_MAX)
    return JSONResponse(result)


@app.get("/api/colorscales")
async def get_colorscales():
    """Return color scale metadata for the frontend colorbar."""
    return JSONResponse({
        k: {"vmin": v["vmin"], "vmax": v["vmax"], "unit": v["unit"], "label": v["label"]}
        for k, v in COLOR_SCALES.items()
    })


@app.get("/api/config")
async def get_config():
    """Return pipeline configuration for frontend."""
    return JSONResponse({
        "subhourly": {
            "enabled": SUBHOURLY_CONFIG.get("enabled", True),
            "params": SUBHOURLY_CONFIG.get("params", ["reflectivity", "temp_2m"]),
        }
    })


# ---------------------------------------------------------------------------
# Log tail endpoint
# ---------------------------------------------------------------------------

# Log files - pipeline (models+satellite) and radar
LOG_DIR = DATA_DIR.parent / "logs"
PIPELINE_LOG = LOG_DIR / "pipeline.log"
RADAR_LOG = LOG_DIR / "radar.log"

LOG_TRANSLATIONS = [
    (r'Run (\d{10}): downloading fxx=(\d+)', r'Downloading forecast hour \2 for run \1'),
    (r'Run (\d{10}): fxx=(\d+) downloaded in ([\d.]+)s', r'Downloaded hour \2 in \3 seconds'),
    (r'Downloaded sfc fxx=(\d+) .+', r'Downloaded surface data for hour \1'),
    (r'fxx=(\d+) not yet available', r'Hour \1 not yet available from server'),
    (r'Run (\d{10}): rendering fxx=(\d+)', r'Rendering maps for hour \2'),
    (r'Run (\d{10}): fxx=(\d+) rendered (\d+) params .+ (\d+)% done', r'Hour \2: \3 params rendered (\4%)'),
    (r'Found (\d+) unprocessed run\(s\)', r'Found \1 new forecast runs to process'),
    (r'Run (\d{10}): complete', r'Run \1 complete'),
    (r'Pipeline scheduler started', r'Pipeline started'),
    (r'Resuming in-progress run (\d{10})', r'Resuming run \1'),
    (r'Could not load precip_type .+', r'Precip type not available'),
    (r'Skipping precip_type .+', r'Precip type not in dataset'),
]


def translate_log_line(line: str) -> tuple[str, str]:
    match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),?\d* (\w+) ([\w.]+): (.+)$', line)
    if match:
        timestamp, level, module, message = match.groups()
    else:
        timestamp, level, message = None, 'INFO', line

    translated = message
    for pattern, replacement in LOG_TRANSLATIONS:
        new_msg = re.sub(pattern, replacement, message)
        if new_msg != message:
            translated = new_msg
            break

    return line, translated


def _recent_radar_log_lines(max_lines: int = 500) -> list[str]:
    """Read recent radar logs, preferring the systemd journal."""
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "weather-radar.service", "-n", str(max_lines), "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode == 0:
            lines = [line.rstrip("\n") for line in proc.stdout.splitlines() if line.strip()]
            if lines:
                return lines
    except Exception:
        pass

    try:
        if RADAR_LOG.exists():
            with open(RADAR_LOG) as f:
                return [line.rstrip("\n") for line in f.readlines()[-max_lines:] if line.strip()]
    except Exception:
        pass

    return []


@app.get("/api/logs")
async def get_logs(lines: int = 50):
    """Get recent log entries from pipeline and radar (merged, sorted by time)."""
    all_entries = []
    
    # Read from both log files
    for log_file, source in [(PIPELINE_LOG, "pipeline"), (RADAR_LOG, "radar")]:
        if not log_file.exists():
            continue
        try:
            with open(log_file, "r") as f:
                file_lines = f.readlines()
                for line in file_lines:
                    line = line.rstrip('\n')
                    if not line.strip():
                        continue
                    original, plain = translate_log_line(line)
                    if 'precip type' in plain.lower():
                        continue
                    ts_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                    if not ts_match:
                        continue  # skip traceback/continuation lines
                    all_entries.append({
                        "original": original,
                        "plain": f"[{source.upper()}] {plain}",
                        "timestamp": ts_match.group(1),
                        "source": source,
                        "_ts": ts_match.group(1)
                    })
        except Exception as exc:
            logger.warning(f"Error reading {log_file}: {exc}")
    
    # Sort by timestamp and take last N lines
    all_entries.sort(key=lambda x: x["_ts"])
    entries = all_entries[-lines:]
    
    # Remove internal sort key
    for entry in entries:
        del entry["_ts"]
    
    return JSONResponse({"entries": entries})


@app.get("/api/logs/download")
async def download_logs(source: str = "pipeline"):
    """Download a log file (pipeline or radar)."""
    log_file = PIPELINE_LOG if source == "pipeline" else RADAR_LOG if source == "radar" else None
    if not log_file or not log_file.exists():
        raise HTTPException(status_code=404, detail=f"Log file {source} not found")
    from fastapi.responses import FileResponse
    return FileResponse(
        path=str(log_file),
        media_type="text/plain",
        filename=log_file.name
    )


# ---------------------------------------------------------------------------
# Value query endpoint for hover
# ---------------------------------------------------------------------------

# Map model name → grib cache subdirectory
_MODEL_GRIB_SUBDIR = {
    "hrrr":    "hrrr",
    "rtma":    "rtma",
    "nam3km":  "nam",
    "nam12km": "nam",
    "rap":     "rap",
    "ecmwf":   "ifs",
    "gfs":     "gfs",
    "gdps":    "gdps",
}

# Derived params: (u_component, v_component) → magnitude
_VALUE_DERIVED = {
    "wind_speed":  ("ugrd_10m",   "vgrd_10m"),
    "shear_06km":  ("ushear_06km","vshear_06km"),
    "shear_01km":  ("ushear_01km","vshear_01km"),
    "wind_850mb":  ("ugrd_850mb", "vgrd_850mb"),
    "wind_500mb":  ("ugrd_500mb", "vgrd_500mb"),
    "wind_300mb":  ("ugrd_300mb", "vgrd_300mb"),
}


def _find_grib_for_fxx(model: str, run_id: str, fxx: int):
    """Return the cached GRIB file path for a given model/run/fxx, or None."""
    subdir = _MODEL_GRIB_SUBDIR.get(model)
    if not subdir:
        return None
    date_str = run_id[:8]
    hour_str  = run_id[8:10]
    cache_dir = GRIB_CACHE_DIR / subdir / date_str
    if not cache_dir.exists():
        return None

    if model == "hrrr":
        pat = f"hrrr.t{hour_str}z.wrfsfcf{fxx:02d}.grib2"
    elif model == "nam3km":
        pat = f"nam.t{hour_str}z.conusnest.hiresf{fxx:02d}.tm00.grib2"
    elif model == "nam12km":
        pat = f"nam.t{hour_str}z.awphys{fxx:02d}.tm00.grib2"
    elif model == "rap":
        pat = f"rap.t{hour_str}z.awp130pgrbf{fxx:02d}.grib2"
    elif model == "ecmwf":
        pat = f"{date_str}{hour_str}0000-{fxx}h-oper-fc.grib2"
    elif model == "gfs":
        pat = f"gfs.t{hour_str}z.pgrb2.0p25.f{fxx:03d}.grib2"
    elif model == "gdps":
        pat = f"CMC_glb_*_latlon.15x.15_{date_str}{hour_str}_P{fxx:03d}.grib2"
    elif model == "rtma":
        pat = f"rtma2p5.t{hour_str}z.2dvaranl_ndfd.grb2_wexp"
    else:
        return None

    matches = list(cache_dir.glob(f"subset_*__{pat}"))
    return matches[0] if matches else None


def _nearest_point(field, lat: float, lon: float) -> float:
    """Return the field value at the nearest grid point to (lat, lon)."""
    import numpy as np
    lats, lons = field.lat, field.lon
    lon_n = lon + 360.0 if (lons.max() > 180 and lon < 0) else lon
    vals = field.values
    while vals.ndim > 2 and vals.shape[0] <= 4:
        vals = vals[0]
    if lats.ndim == 2:
        dist = (lats - lat) ** 2 + (lons - lon_n) ** 2
        idx = tuple(int(i) for i in np.unravel_index(np.argmin(dist), dist.shape))
        return float(vals[idx])
    else:
        li = int(np.argmin(np.abs(lats - lat)))
        lj = int(np.argmin(np.abs(lons - lon_n)))
        return float(vals[li, lj])


def _wind_direction_from_uv(u: float, v: float) -> float:
    """Return meteorological wind direction in degrees."""
    direction = (270.0 - math.degrees(math.atan2(v, u))) % 360.0
    return float(direction)


def _dewpoint_from_temp_rh(temp_c: float, rh: float) -> float | None:
    """Estimate dewpoint in Celsius from temperature and RH."""
    if not math.isfinite(temp_c) or not math.isfinite(rh):
        return None
    rh = max(1e-3, min(100.0, rh))
    a = 17.625
    b = 243.04
    gamma = math.log(rh / 100.0) + (a * temp_c) / (b + temp_c)
    return float((b * gamma) / (a - gamma))


def _coerce_float(value) -> float | None:
    try:
        value = float(value)
    except Exception:
        return None
    return value if math.isfinite(value) else None


def _format_wind_uv(u: float | None, v: float | None) -> dict | None:
    if u is None or v is None:
        return None
    try:
        from sharppy.sharptab import utils as sharp_utils
        direction, speed = sharp_utils.comp2vec(u, v)
        return {
            "u_kt": round(float(u), 1),
            "v_kt": round(float(v), 1),
            "dir_deg": round(float(direction), 0),
            "speed_kt": round(float(speed), 1),
        }
    except Exception:
        speed = math.hypot(u, v)
        return {
            "u_kt": round(float(u), 1),
            "v_kt": round(float(v), 1),
            "dir_deg": round(_wind_direction_from_uv(u, v), 0),
            "speed_kt": round(float(speed), 1),
        }


def _clean_watch_type(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.replace("_", " ")


def _format_sars_hail(matches) -> dict:
    quality_dates, quality_sizes, loose_count, sig_reports, sig_prob = matches or ([], [], 0, 0, 0)
    analogs = []
    for date, size in list(zip(quality_dates, quality_sizes))[:6]:
        try:
            size_text = f'{float(size):.2f}"'
        except Exception:
            size_text = str(size)
        analogs.append({
            "label": str(date),
            "match": size_text,
        })
    return {
        "loose_match_count": int(loose_count or 0),
        "significant_match_count": int(sig_reports or 0),
        "significant_probability": round(float(sig_prob or 0.0), 3),
        "analogs": analogs,
    }


def _format_sars_supercell(matches) -> dict:
    quality_soundings, quality_types, loose_count, tornadic_count, tor_prob = matches or ([], [], 0, 0, 0)
    analogs = []
    for sounding, match_type in list(zip(quality_soundings, quality_types))[:6]:
        analogs.append({
            "label": str(sounding),
            "match": str(match_type),
        })
    return {
        "loose_match_count": int(loose_count or 0),
        "tornadic_loose_match_count": int(tornadic_count or 0),
        "tornado_probability": round(float(tor_prob or 0.0), 3),
        "analogs": analogs,
    }


def _serialize_trace(pressures, temperatures) -> list[dict]:
    points = []
    try:
        iterable = zip(pressures, temperatures)
    except Exception:
        return points
    for pressure, temp_c in iterable:
        pressure_f = _coerce_float(pressure)
        temp_f = _coerce_float(temp_c)
        if pressure_f is None or temp_f is None:
            continue
        points.append({
            "pressure_hpa": round(float(pressure_f), 1),
            "temp_c": round(float(temp_f), 2),
        })
    return points


def _build_sharppy_diagnostics(run_id: str, fxx: int, levels: list[dict], surface: dict) -> dict | None:
    usable = []

    if surface:
        sfc_pres = _coerce_float(surface.get("mslp_mb")) or 1000.0
        sfc_pres = max(900.0, min(1050.0, sfc_pres))
        sfc_temp = _coerce_float(surface.get("temp_c"))
        sfc_dewp = _coerce_float(surface.get("dewpoint_c"))
        wind = surface.get("wind")
        sfc_u = _coerce_float(wind.get("u_kt")) if isinstance(wind, dict) else None
        sfc_v = _coerce_float(wind.get("v_kt")) if isinstance(wind, dict) else None
        if sfc_temp is not None and sfc_dewp is not None:
            usable.append({
                "pressure_hpa": sfc_pres,
                "height_m": 0.0,
                "temp_c": sfc_temp,
                "dewpoint_c": sfc_dewp,
                "u_kt": sfc_u or 0.0,
                "v_kt": sfc_v or 0.0,
            })

    for level in sorted(levels, key=lambda row: row["pressure_hpa"], reverse=True):
        temp_c = _coerce_float(level.get("temp_c"))
        dewpoint_c = _coerce_float(level.get("dewpoint_c"))
        height_m = _coerce_float(level.get("height_m"))
        u_kt = _coerce_float(level.get("u_kt"))
        v_kt = _coerce_float(level.get("v_kt"))
        if temp_c is None or dewpoint_c is None or height_m is None:
            continue
        usable.append({
            "pressure_hpa": float(level["pressure_hpa"]),
            "height_m": height_m,
            "temp_c": temp_c,
            "dewpoint_c": dewpoint_c,
            "u_kt": u_kt or 0.0,
            "v_kt": v_kt or 0.0,
        })

    deduped = []
    seen_pressures = set()
    for row in usable:
        pressure_key = round(row["pressure_hpa"], 1)
        if pressure_key in seen_pressures:
            continue
        seen_pressures.add(pressure_key)
        deduped.append(row)

    if len(deduped) < 4:
        return None

    try:
        import numpy as np
        from sharppy.sharptab import profile as sharp_profile
        from sharppy.sharptab import utils as sharp_utils
    except Exception:
        return None

    valid_dt = None
    if len(run_id) == 10:
        try:
            init = datetime(int(run_id[:4]), int(run_id[4:6]), int(run_id[6:8]), int(run_id[8:10]), tzinfo=timezone.utc)
            valid_dt = init
            if isinstance(fxx, int):
                from datetime import timedelta
                valid_dt = init + timedelta(hours=fxx)
        except Exception:
            valid_dt = None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        prof = sharp_profile.create_profile(
            profile="convective",
            pres=np.array([row["pressure_hpa"] for row in deduped], dtype=float),
            hght=np.array([row["height_m"] for row in deduped], dtype=float),
            tmpc=np.array([row["temp_c"] for row in deduped], dtype=float),
            dwpc=np.array([row["dewpoint_c"] for row in deduped], dtype=float),
            u=np.array([row["u_kt"] for row in deduped], dtype=float),
            v=np.array([row["v_kt"] for row in deduped], dtype=float),
            location="GRID",
            date=valid_dt or datetime.now(timezone.utc),
        )

    right_motion = _format_wind_uv(_coerce_float(prof.bunkers[0]), _coerce_float(prof.bunkers[1])) if getattr(prof, "bunkers", None) else None
    left_motion = _format_wind_uv(_coerce_float(prof.bunkers[2]), _coerce_float(prof.bunkers[3])) if getattr(prof, "bunkers", None) else None

    diagnostics = {
        "sbcape_jkg": round(float(prof.sfcpcl.bplus), 1),
        "sbcin_jkg": round(float(prof.sfcpcl.bminus), 1),
        "mlcape_jkg": round(float(prof.mlpcl.bplus), 1),
        "mlcin_jkg": round(float(prof.mlpcl.bminus), 1),
        "mucape_jkg": round(float(prof.mupcl.bplus), 1),
        "mucin_jkg": round(float(prof.mupcl.bminus), 1),
        "sblcl_m": round(float(prof.sfcpcl.lclhght), 0) if _coerce_float(prof.sfcpcl.lclhght) is not None else None,
        "mllcl_m": round(float(prof.mlpcl.lclhght), 0) if _coerce_float(prof.mlpcl.lclhght) is not None else None,
        "mllfc_m": round(float(prof.mlpcl.lfchght), 0) if _coerce_float(prof.mlpcl.lfchght) is not None else None,
        "mlel_m": round(float(prof.mlpcl.elhght), 0) if _coerce_float(prof.mlpcl.elhght) is not None else None,
        "li5_c": round(float(prof.mlpcl.li5), 1) if _coerce_float(prof.mlpcl.li5) is not None else None,
        "srh_1km_m2s2": round(float(prof.right_srh1km[0]), 1) if getattr(prof, "right_srh1km", None) else None,
        "srh_3km_m2s2": round(float(prof.right_srh3km[0]), 1) if getattr(prof, "right_srh3km", None) else None,
        "ebwd_kt": round(float(prof.ebwspd), 1) if _coerce_float(getattr(prof, "ebwspd", None)) is not None else None,
        "ship": round(float(prof.ship), 2) if _coerce_float(getattr(prof, "ship", None)) is not None else None,
        "stp_cin": round(float(prof.right_stp_cin), 2) if _coerce_float(getattr(prof, "right_stp_cin", None)) is not None else None,
        "stp_fixed": round(float(prof.right_stp_fixed), 2) if _coerce_float(getattr(prof, "right_stp_fixed", None)) is not None else None,
        "scp": round(float(prof.right_scp), 2) if _coerce_float(getattr(prof, "right_scp", None)) is not None else None,
        "dcape_jkg": round(float(prof.dcape), 1) if _coerce_float(getattr(prof, "dcape", None)) is not None else None,
        "k_index": round(float(prof.k_idx), 1) if _coerce_float(getattr(prof, "k_idx", None)) is not None else None,
        "totals_totals": round(float(prof.totals_totals), 1) if _coerce_float(getattr(prof, "totals_totals", None)) is not None else None,
        "tei": round(float(prof.tei), 1) if _coerce_float(getattr(prof, "tei", None)) is not None else None,
        "effective_inflow_base_mb": round(float(prof.ebottom), 1) if _coerce_float(getattr(prof, "ebottom", None)) is not None else None,
        "effective_inflow_top_m": round(float(prof.etopm), 0) if _coerce_float(getattr(prof, "etopm", None)) is not None else None,
        "bunkers_right": right_motion,
        "bunkers_left": left_motion,
        "watch_type": _clean_watch_type(getattr(prof, "watch_type", None)),
        "right_watch_type": _clean_watch_type(getattr(prof, "right_watch_type", None)),
        "left_watch_type": _clean_watch_type(getattr(prof, "left_watch_type", None)),
        "precip_type": _clean_watch_type(getattr(prof, "precip_type", None)),
        "sig_severe": round(float(prof.sig_severe), 0) if _coerce_float(getattr(prof, "sig_severe", None)) is not None else None,
        "wndg": round(float(prof.wndg), 2) if _coerce_float(getattr(prof, "wndg", None)) is not None else None,
        "sars_hail": _format_sars_hail(getattr(prof, "matches", None)),
        "sars_supercell": _format_sars_supercell(getattr(prof, "supercell_matches", None)),
        "sars_hail_right": _format_sars_hail(getattr(prof, "right_matches", None)),
        "sars_hail_left": _format_sars_hail(getattr(prof, "left_matches", None)),
        "sars_supercell_right": _format_sars_supercell(getattr(prof, "right_supercell_matches", None)),
        "sars_supercell_left": _format_sars_supercell(getattr(prof, "left_supercell_matches", None)),
        "profile_points": len(deduped),
    }
    diagnostics["plot_data"] = {
        "wetbulb_profile": _serialize_trace(getattr(prof, "pres", []), getattr(prof, "wetbulb", [])),
        "surface_parcel": _serialize_trace(
            getattr(getattr(prof, "sfcpcl", None), "ptrace", []),
            getattr(getattr(prof, "sfcpcl", None), "ttrace", []),
        ),
        "surface_parcel_lcl_pressure_hpa": round(float(prof.sfcpcl.lclpres), 1) if _coerce_float(getattr(prof.sfcpcl, "lclpres", None)) is not None else None,
        "surface_parcel_lfc_pressure_hpa": round(float(prof.sfcpcl.lfcpres), 1) if _coerce_float(getattr(prof.sfcpcl, "lfcpres", None)) is not None else None,
        "surface_parcel_el_pressure_hpa": round(float(prof.sfcpcl.elpres), 1) if _coerce_float(getattr(prof.sfcpcl, "elpres", None)) is not None else None,
    }
    return diagnostics


def _sample_converted_point(proc, grib_file: Path, param: str, lat: float, lon: float) -> float | None:
    """Load a GRIB field, sample nearest point, and apply configured unit conversion."""
    field = proc.load_field(grib_file, param)
    if field is None:
        return None
    raw_val = _nearest_point(field, lat, lon)
    import numpy as np
    value = float(proc._convert_units(param, np.array([raw_val], dtype=np.float32))[0])
    if not math.isfinite(value):
        return None
    return value


def _find_sounding_cache(model: str, run_id: str, fxx: int) -> Path | None:
    path = RENDERED_DIR / model / run_id / "soundings" / f"f{fxx:02d}.npz"
    return path if path.exists() else None


def _find_native_sounding_cache(model: str, run_id: str, fxx: int) -> Path | None:
    path = RENDERED_DIR / model / run_id / "soundings_native" / f"f{fxx:02d}.npz"
    return path if path.exists() else None


def _nearest_cache_value(values, lats, lons, lat: float, lon: float) -> float | None:
    import numpy as np

    lon_n = lon + 360.0 if (np.nanmax(lons) > 180 and lon < 0) else lon
    if lats.ndim == 2:
        dist = (lats - lat) ** 2 + (lons - lon_n) ** 2
        idx = tuple(int(i) for i in np.unravel_index(np.argmin(dist), dist.shape))
        value = values[idx]
    else:
        li = int(np.argmin(np.abs(lats - lat)))
        lj = int(np.argmin(np.abs(lons - lon_n)))
        value = values[li, lj]
    value_f = float(value)
    return value_f if math.isfinite(value_f) else None


_SOUNDING_LEVEL_FIELDS = {
    1000: {
        "temp": "temp_1000mb",
        "dewpoint": "dewpoint_1000mb",
        "rh": "rh_1000mb",
        "height": "gh_1000mb",
        "u": "ugrd_1000mb",
        "v": "vgrd_1000mb",
    },
    925: {
        "temp": "temp_925mb",
        "dewpoint": "dewpoint_925mb",
        "rh": "rh_925mb",
        "height": "gh_925mb",
        "u": "ugrd_925mb",
        "v": "vgrd_925mb",
    },
    900: {
        "temp": "temp_900mb",
        "dewpoint": "dewpoint_900mb",
        "rh": "rh_900mb",
        "height": "gh_900mb",
        "u": "ugrd_900mb",
        "v": "vgrd_900mb",
    },
    850: {
        "temp": "temp_850mb",
        "dewpoint": "dewpoint_850mb",
        "rh": "rh_850mb",
        "height": "gh_850mb",
        "u": "ugrd_850mb",
        "v": "vgrd_850mb",
    },
    800: {
        "temp": "temp_800mb",
        "dewpoint": "dewpoint_800mb",
        "rh": "rh_800mb",
        "height": "gh_800mb",
        "u": "ugrd_800mb",
        "v": "vgrd_800mb",
    },
    750: {
        "temp": "temp_750mb",
        "dewpoint": "dewpoint_750mb",
        "rh": "rh_750mb",
        "height": "gh_750mb",
        "u": "ugrd_750mb",
        "v": "vgrd_750mb",
    },
    700: {
        "temp": "temp_700mb",
        "dewpoint": None,
        "rh": "rh_700mb",
        "height": "gh_700mb",
        "u": "ugrd_700mb",
        "v": "vgrd_700mb",
    },
    650: {
        "temp": "temp_650mb",
        "dewpoint": None,
        "rh": "rh_650mb",
        "height": "gh_650mb",
        "u": "ugrd_650mb",
        "v": "vgrd_650mb",
    },
    600: {
        "temp": "temp_600mb",
        "dewpoint": None,
        "rh": "rh_600mb",
        "height": "gh_600mb",
        "u": "ugrd_600mb",
        "v": "vgrd_600mb",
    },
    550: {
        "temp": "temp_550mb",
        "dewpoint": None,
        "rh": "rh_550mb",
        "height": "gh_550mb",
        "u": "ugrd_550mb",
        "v": "vgrd_550mb",
    },
    500: {
        "temp": "temp_500mb",
        "dewpoint": None,
        "rh": "rh_500mb",
        "height": "gh_500mb",
        "u": "ugrd_500mb",
        "v": "vgrd_500mb",
    },
    450: {
        "temp": "temp_450mb",
        "dewpoint": None,
        "rh": "rh_450mb",
        "height": "gh_450mb",
        "u": "ugrd_450mb",
        "v": "vgrd_450mb",
    },
    400: {
        "temp": "temp_400mb",
        "dewpoint": None,
        "rh": "rh_400mb",
        "height": "gh_400mb",
        "u": "ugrd_400mb",
        "v": "vgrd_400mb",
    },
    350: {
        "temp": "temp_350mb",
        "dewpoint": None,
        "rh": "rh_350mb",
        "height": "gh_350mb",
        "u": "ugrd_350mb",
        "v": "vgrd_350mb",
    },
    300: {
        "temp": "temp_300mb",
        "dewpoint": None,
        "rh": "rh_300mb",
        "height": "gh_300mb",
        "u": "ugrd_300mb",
        "v": "vgrd_300mb",
    },
    250: {
        "temp": "temp_250mb",
        "dewpoint": None,
        "rh": "rh_250mb",
        "height": "gh_250mb",
        "u": "ugrd_250mb",
        "v": "vgrd_250mb",
    },
    200: {
        "temp": "temp_200mb",
        "dewpoint": None,
        "rh": "rh_200mb",
        "height": "gh_200mb",
        "u": "ugrd_200mb",
        "v": "vgrd_200mb",
    },
}


# Computed sounding responses are small; cache them so repeat probes of the
# same point (scrub back/forth, re-open modal) skip the npz decompression.
_sounding_response_cache: dict[tuple, dict] = {}
_SOUNDING_RESPONSE_CACHE_MAX = 256


@app.get("/api/sounding/{model}/{run_id}/{fxx}")
async def get_sounding(
    model: str,
    run_id: str,
    fxx: int,
    lat: float = Query(...),
    lon: float = Query(...),
):
    """Return a nearest-gridpoint profile for the current model subset cache."""
    from pipeline.processor import FieldProcessor

    try:
        native_cache_path = _find_native_sounding_cache(model, run_id, fxx)
        cache_path = _find_sounding_cache(model, run_id, fxx)
        source = "cache"
        levels = []
        surface = {}

        sounding_cache_key = None
        active_path = native_cache_path or cache_path
        if active_path is not None:
            try:
                sounding_cache_key = (
                    model, run_id, fxx, round(lat, 3), round(lon, 3),
                    str(active_path), active_path.stat().st_mtime_ns,
                )
            except OSError:
                sounding_cache_key = None
            cached_response = _sounding_response_cache.get(sounding_cache_key)
            if cached_response is not None:
                return JSONResponse(cached_response)

        if native_cache_path or cache_path:
            import numpy as np

            active_cache_path = native_cache_path or cache_path
            with np.load(active_cache_path) as cache:
                lats = cache["lat"]
                lons = cache["lon"]
                stride = int(cache["stride"][0]) if "stride" in cache else None
                is_native = "native" in cache or native_cache_path is not None

                for pressure_hpa, field_map in _SOUNDING_LEVEL_FIELDS.items():
                    temp_f = _nearest_cache_value(cache[field_map["temp"]], lats, lons, lat, lon) if field_map.get("temp") in cache else None
                    dewpoint_f = _nearest_cache_value(cache[field_map["dewpoint"]], lats, lons, lat, lon) if field_map.get("dewpoint") in cache else None
                    rh = _nearest_cache_value(cache[field_map["rh"]], lats, lons, lat, lon) if field_map.get("rh") in cache else None
                    height_m = _nearest_cache_value(cache[field_map["height"]], lats, lons, lat, lon) if field_map.get("height") in cache else None
                    u_kt = _nearest_cache_value(cache[field_map["u"]], lats, lons, lat, lon) if field_map.get("u") in cache else None
                    v_kt = _nearest_cache_value(cache[field_map["v"]], lats, lons, lat, lon) if field_map.get("v") in cache else None

                    temp_c = (temp_f - 32.0) * 5.0 / 9.0 if temp_f is not None else None
                    dewpoint_c = (dewpoint_f - 32.0) * 5.0 / 9.0 if dewpoint_f is not None else None
                    if dewpoint_c is None and temp_c is not None and rh is not None:
                        dewpoint_c = _dewpoint_from_temp_rh(temp_c, rh)

                    wind_speed_kt = None
                    wind_dir_deg = None
                    if u_kt is not None and v_kt is not None:
                        wind_speed_kt = float(math.hypot(u_kt, v_kt))
                        wind_dir_deg = _wind_direction_from_uv(u_kt, v_kt)

                    if all(value is None for value in (temp_c, dewpoint_c, rh, height_m, wind_speed_kt)):
                        continue

                    levels.append({
                        "pressure_hpa": pressure_hpa,
                        "temp_c": round(temp_c, 1) if temp_c is not None else None,
                        "dewpoint_c": round(dewpoint_c, 1) if dewpoint_c is not None else None,
                        "rh_pct": round(rh, 1) if rh is not None else None,
                        "height_m": round(height_m, 0) if height_m is not None else None,
                        "wind_speed_kt": round(wind_speed_kt, 1) if wind_speed_kt is not None else None,
                        "wind_dir_deg": round(wind_dir_deg, 0) if wind_dir_deg is not None else None,
                        "u_kt": round(u_kt, 1) if u_kt is not None else None,
                        "v_kt": round(v_kt, 1) if v_kt is not None else None,
                    })

                surface_temp_f = _nearest_cache_value(cache["temp_2m"], lats, lons, lat, lon) if "temp_2m" in cache else None
                surface_dewpoint_f = _nearest_cache_value(cache["dewpoint_2m"], lats, lons, lat, lon) if "dewpoint_2m" in cache else None
                surface_wind_u = _nearest_cache_value(cache["ugrd_10m"], lats, lons, lat, lon) if "ugrd_10m" in cache else None
                surface_wind_v = _nearest_cache_value(cache["vgrd_10m"], lats, lons, lat, lon) if "vgrd_10m" in cache else None
                surface_mslp = _nearest_cache_value(cache["mslp"], lats, lons, lat, lon) if "mslp" in cache else None
                if surface_temp_f is not None:
                    surface["temp_c"] = round((surface_temp_f - 32.0) * 5.0 / 9.0, 1)
                if surface_dewpoint_f is not None:
                    surface["dewpoint_c"] = round((surface_dewpoint_f - 32.0) * 5.0 / 9.0, 1)
                if surface_wind_u is not None and surface_wind_v is not None:
                    surface["wind"] = _format_wind_uv(surface_wind_u, surface_wind_v)
                if surface_mslp is not None:
                    surface["mslp_mb"] = round(surface_mslp, 1)
                if is_native:
                    source = "native_cache"
                    cache_note = "Profile uses persisted native-grid sounding storage for this frame."
                else:
                    cache_note = f"Profile uses persisted reduced-grid sounding cache (stride {stride})." if stride else "Profile uses persisted reduced-grid sounding cache."
        else:
            grib_file = _find_grib_for_fxx(model, run_id, fxx)
            if not grib_file:
                raise HTTPException(status_code=404, detail="GRIB file not found")

            proc = FieldProcessor()
            source = "grib"
            cache_note = "Profile uses live GRIB sampling because no persisted sounding cache exists for this frame yet."
            for pressure_hpa, field_map in _SOUNDING_LEVEL_FIELDS.items():
                temp_f = _sample_converted_point(proc, grib_file, field_map["temp"], lat, lon) if field_map.get("temp") else None
                dewpoint_f = _sample_converted_point(proc, grib_file, field_map["dewpoint"], lat, lon) if field_map.get("dewpoint") else None
                rh = _sample_converted_point(proc, grib_file, field_map["rh"], lat, lon) if field_map.get("rh") else None
                height_m = _sample_converted_point(proc, grib_file, field_map["height"], lat, lon) if field_map.get("height") else None
                u_kt = _sample_converted_point(proc, grib_file, field_map["u"], lat, lon) if field_map.get("u") else None
                v_kt = _sample_converted_point(proc, grib_file, field_map["v"], lat, lon) if field_map.get("v") else None

                temp_c = (temp_f - 32.0) * 5.0 / 9.0 if temp_f is not None else None
                dewpoint_c = (dewpoint_f - 32.0) * 5.0 / 9.0 if dewpoint_f is not None else None
                if dewpoint_c is None and temp_c is not None and rh is not None:
                    dewpoint_c = _dewpoint_from_temp_rh(temp_c, rh)

                wind_speed_kt = None
                wind_dir_deg = None
                if u_kt is not None and v_kt is not None:
                    wind_speed_kt = float(math.hypot(u_kt, v_kt))
                    wind_dir_deg = _wind_direction_from_uv(u_kt, v_kt)

                if all(value is None for value in (temp_c, dewpoint_c, rh, height_m, wind_speed_kt)):
                    continue

                levels.append({
                    "pressure_hpa": pressure_hpa,
                    "temp_c": round(temp_c, 1) if temp_c is not None else None,
                    "dewpoint_c": round(dewpoint_c, 1) if dewpoint_c is not None else None,
                    "rh_pct": round(rh, 1) if rh is not None else None,
                    "height_m": round(height_m, 0) if height_m is not None else None,
                    "wind_speed_kt": round(wind_speed_kt, 1) if wind_speed_kt is not None else None,
                    "wind_dir_deg": round(wind_dir_deg, 0) if wind_dir_deg is not None else None,
                    "u_kt": round(u_kt, 1) if u_kt is not None else None,
                    "v_kt": round(v_kt, 1) if v_kt is not None else None,
                })

            surface_temp_f = _sample_converted_point(proc, grib_file, "temp_2m", lat, lon)
            surface_dewpoint_f = _sample_converted_point(proc, grib_file, "dewpoint_2m", lat, lon)
            surface_wind_u = _sample_converted_point(proc, grib_file, "ugrd_10m", lat, lon)
            surface_wind_v = _sample_converted_point(proc, grib_file, "vgrd_10m", lat, lon)
            if surface_temp_f is not None:
                surface["temp_c"] = round((surface_temp_f - 32.0) * 5.0 / 9.0, 1)
            if surface_dewpoint_f is not None:
                surface["dewpoint_c"] = round((surface_dewpoint_f - 32.0) * 5.0 / 9.0, 1)
            if surface_wind_u is not None and surface_wind_v is not None:
                surface["wind"] = _format_wind_uv(surface_wind_u, surface_wind_v)
            surface_mslp = _sample_converted_point(proc, grib_file, "mslp", lat, lon)
            if surface_mslp is not None:
                surface["mslp_mb"] = round(surface_mslp, 1)

        if not levels:
            raise HTTPException(status_code=404, detail="No sounding profile fields found in cache or GRIB subset")

        profile_quality = "full" if len(levels) >= 5 else ("partial" if len(levels) >= 3 else "sparse")
        diagnostics = _build_sharppy_diagnostics(run_id, fxx, levels, surface)
        payload = {
            "model": model,
            "run_id": run_id,
            "fxx": fxx,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "levels": sorted(levels, key=lambda row: row["pressure_hpa"], reverse=True),
            "surface": surface,
            "quality": profile_quality,
            "level_count": len(levels),
            "note": cache_note,
            "source": source,
            "diagnostics": diagnostics,
        }
        if sounding_cache_key is not None:
            _sounding_response_cache[sounding_cache_key] = payload
            _prune_cache(_sounding_response_cache, _SOUNDING_RESPONSE_CACHE_MAX)
        return JSONResponse(payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/value/{model}/{run_id}/{fxx}/{parameter}")
async def get_value(
    model: str, run_id: str, fxx: int, parameter: str,
    lat: float = Query(...), lon: float = Query(...),
):
    from pipeline.processor import FieldProcessor

    grib_file = _find_grib_for_fxx(model, run_id, fxx)
    if not grib_file:
        raise HTTPException(status_code=404, detail="GRIB file not found")

    try:
        proc = FieldProcessor()

        if parameter in _VALUE_DERIVED:
            u_key, v_key = _VALUE_DERIVED[parameter]
            uf = proc.load_field(grib_file, u_key)
            vf = proc.load_field(grib_file, v_key)
            if uf is None or vf is None:
                raise HTTPException(status_code=404, detail=f"Components not found for {parameter}")
            import numpy as np
            u_val = proc._convert_units(u_key, uf.values)
            v_val = proc._convert_units(v_key, vf.values)
            while u_val.ndim > 2 and u_val.shape[0] <= 4:
                u_val = u_val[0]
            while v_val.ndim > 2 and v_val.shape[0] <= 4:
                v_val = v_val[0]
            # Find nearest point index using u field coords
            import numpy as _np
            lats, lons = uf.lat, uf.lon
            lon_n = lon + 360.0 if (lons.max() > 180 and lon < 0) else lon
            if lats.ndim == 2:
                dist = (lats - lat) ** 2 + (lons - lon_n) ** 2
                idx = tuple(int(i) for i in _np.unravel_index(_np.argmin(dist), dist.shape))
            else:
                li = int(_np.argmin(_np.abs(lats - lat)))
                lj = int(_np.argmin(_np.abs(lons - lon_n)))
                idx = (li, lj)
            u_pt = float(u_val[idx])
            v_pt = float(v_val[idx])
            value = float(_np.sqrt(u_pt ** 2 + v_pt ** 2))
        else:
            field = proc.load_field(grib_file, parameter)
            if field is None:
                raise HTTPException(status_code=404, detail=f"Parameter {parameter} not found")
            raw_val = _nearest_point(field, lat, lon)
            import numpy as np
            value = float(proc._convert_units(parameter, np.array([raw_val]))[0])

        unit = COLOR_SCALES.get(parameter, {}).get("unit", "")
        return JSONResponse({
            "value": round(value, 2),
            "unit": unit,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
        })

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# PNG-based value query endpoint (does not require GRIB files)
# ---------------------------------------------------------------------------

# Import pipeline config to register custom colormaps
from pipeline.config import COLOR_SCALES

# Cache for colormap RGB -> value lookups
_CMAP_CACHE: dict[str, dict] = {}
_MEAN_VAL_CACHE: dict[str, float] = {}


def _compute_cmap_mean_val(cmap_name: str) -> float:
    """Compute the mean pixel value after saturation enhancement for a colormap.

    This must match the renderer's approach so that contrast enhancement
    can be reversed correctly when mapping pixel colors back to data values.
    """
    if cmap_name in _MEAN_VAL_CACHE:
        return _MEAN_VAL_CACHE[cmap_name]
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        cmap = plt.get_cmap(cmap_name)
    except Exception:
        return 128.0
    n_samples = 256
    total = 0.0
    for i in range(n_samples):
        t = i / (n_samples - 1)
        r, g, b, _ = cmap(t)
        r_sat, g_sat, b_sat = _apply_saturation(r * 255, g * 255, b * 255)
        total += r_sat + g_sat + b_sat
    mean_val = total / (n_samples * 3)
    _MEAN_VAL_CACHE[cmap_name] = mean_val
    return mean_val


def _apply_saturation(r: float, g: float, b: float) -> tuple:
    """Apply saturation enhancement (factor 2.2x) - same as renderer."""
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    r = gray * (1.0 - 2.2) + r * 2.2
    g = gray * (1.0 - 2.2) + g * 2.2
    b = gray * (1.0 - 2.2) + b * 2.2
    return (r, g, b)


def _apply_contrast(r: float, g: float, b: float, mean_val: float) -> tuple:
    """Apply contrast enhancement (factor 1.2x) with given mean - same as renderer."""
    r = mean_val * (1.0 - 1.2) + r * 1.2
    g = mean_val * (1.0 - 1.2) + g * 1.2
    b = mean_val * (1.0 - 1.2) + b * 1.2
    # Clamp to valid range
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))
    return (r, g, b)


def _build_cmap_lookup(cmap_name: str, vmin: float, vmax: float, parameter: str = None) -> dict:
    """Build a lookup table mapping RGB values to data values for a colormap.

    Applies the same color enhancements as the renderer so that the lookup
    matches the actual rendered PNG colors.
    Note: "reflectivity" skips enhancement in the renderer.
    """
    try:
        import matplotlib
        matplotlib.use('Agg')  # Non-interactive backend
        import matplotlib.pyplot as plt
        cmap = plt.get_cmap(cmap_name)
    except Exception:
        return {}

    # Check if this parameter should skip color enhancement (like reflectivity)
    skip_enhance = parameter and parameter.lower() in ("reflectivity",)

    lookup = {}
    n_samples = 256

    if skip_enhance:
        # No enhancement - direct colormap sampling
        for i in range(n_samples):
            t = i / (n_samples - 1)
            r, g, b, _ = cmap(t)
            rgb_key = (int(r * 255), int(g * 255), int(b * 255))
            value = vmin + t * (vmax - vmin)
            lookup[rgb_key] = value
    else:
        # First pass: apply saturation to all samples and compute mean
        # This matches the renderer's approach (pipeline/renderer.py lines 493-503)
        sat_samples = []
        for i in range(n_samples):
            t = i / (n_samples - 1)
            r, g, b, _ = cmap(t)
            r_sat, g_sat, b_sat = _apply_saturation(r * 255, g * 255, b * 255)
            sat_samples.append((r_sat, g_sat, b_sat))

        # Compute mean of all channels after saturation (same as renderer)
        mean_val = sum(sum(c) for c in sat_samples) / (len(sat_samples) * 3)

        # Second pass: apply contrast with computed mean and build lookup
        for i, (r_sat, g_sat, b_sat) in enumerate(sat_samples):
            t = i / (n_samples - 1)
            r, g, b = _apply_contrast(r_sat, g_sat, b_sat, mean_val)
            rgb_key = (int(r), int(g), int(b))
            value = vmin + t * (vmax - vmin)
            lookup[rgb_key] = value

    return lookup

def _lookup_arrays(lookup: dict) -> tuple:
    """Return (rgb (N,3) int array, values (N,) float array) for a LUT dict."""
    import numpy as np

    keys = np.array(list(lookup.keys()), dtype=np.int32)
    vals = np.array(list(lookup.values()), dtype=np.float64)
    return keys, vals


def _nearest_lut_value(rgb: tuple, keys, vals, max_dist: float = 15000.0) -> float | None:
    """Vectorized nearest-color search in RGB space."""
    import numpy as np

    if keys.size == 0:
        return None
    dist = ((keys - np.asarray(rgb, dtype=np.int32)) ** 2).sum(axis=1)
    idx = int(np.argmin(dist))
    # Threshold is lenient because the renderer applies saturation/contrast
    # enhancements that shift colors from raw colormap output.
    return float(vals[idx]) if float(dist[idx]) < max_dist else None


def _rgb_to_value(rgb: tuple, cmap_name: str, vmin: float, vmax: float, parameter: str = None) -> float | None:
    """Convert an RGB color to a data value using the colormap."""
    # Skip transparent/black/white pixels (likely no data or background)
    r, g, b = rgb
    if rgb == (0, 0, 0) or rgb == (255, 255, 255):
        return None

    scale = COLOR_SCALES.get(parameter or "")
    if scale and scale.get("bounds") and scale.get("colors"):
        cache_key = f"discrete_{parameter}"
        cached = _CMAP_CACHE.get(cache_key)
        if cached is None:
            lookup = {}
            bounds = list(scale["bounds"])
            for i, color in enumerate(scale["colors"]):
                if not isinstance(color, str) or not color.startswith("#") or len(color) != 7:
                    continue
                key = (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
                upper_idx = min(i + 1, len(bounds) - 1)
                lookup[key] = (float(bounds[i]) + float(bounds[upper_idx])) / 2.0
            cached = (lookup, *_lookup_arrays(lookup))
            _CMAP_CACHE[cache_key] = cached
            _prune_cache(_CMAP_CACHE, _CMAP_CACHE_MAX)
        lookup, keys, vals = cached
        if rgb in lookup:
            return lookup[rgb]
        return _nearest_lut_value(rgb, keys, vals)

    # Check if it's a grayscale pixel (for contour-only params that might have been colormapped)
    gray_tol = 5
    if abs(r - g) < gray_tol and abs(g - b) < gray_tol and abs(r - b) < gray_tol:
        # It's grayscale - treat as no data for colormapped images
        return None

    # Include parameter in cache key to handle reflectivity separately (no color enhance)
    cache_key = f"{cmap_name}_{vmin}_{vmax}_{parameter or 'default'}"
    cached = _CMAP_CACHE.get(cache_key)
    if cached is None or not isinstance(cached, tuple):
        lookup = _build_cmap_lookup(cmap_name, vmin, vmax, parameter)
        cached = (lookup, *_lookup_arrays(lookup))
        _CMAP_CACHE[cache_key] = cached
        _prune_cache(_CMAP_CACHE, _CMAP_CACHE_MAX)
    lookup, keys, vals = cached

    # Exact match
    if rgb in lookup:
        return lookup[rgb]

    return _nearest_lut_value(rgb, keys, vals)

@app.get("/api/value-png/{model}/{run_id}/{fxx}/{parameter}")
async def get_value_from_png(
    model: str, run_id: str, fxx: int, parameter: str,
    lat: float = Query(...), lon: float = Query(...),
):
    """Query parameter value by sampling the rendered PNG image.
    
    This works even when GRIB files have been purged, as long as the
    rendered PNG images still exist.
    """
    from PIL import Image
    
    # Get bounds for the model run
    bounds_path = RENDERED_DIR / model / run_id / "bounds.json"
    if not bounds_path.exists():
        raise HTTPException(status_code=404, detail="Bounds not found")
    
    try:
        bounds = json.loads(bounds_path.read_text())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read bounds: {exc}")
    
    # Check if lat/lon is within bounds
    if not (bounds["south"] <= lat <= bounds["north"] and bounds["west"] <= lon <= bounds["east"]):
        raise HTTPException(status_code=400, detail="Coordinates outside model domain")
    
    # Find the PNG file
    fxx_str = f"f{fxx:02d}"
    png_path = RENDERED_DIR / model / run_id / parameter / f"{fxx_str}.png"
    if not png_path.exists():
        raise HTTPException(status_code=404, detail="PNG image not found")
    
    # Get color scale info
    scale = COLOR_SCALES.get(parameter)
    if not scale:
        raise HTTPException(status_code=404, detail=f"Unknown parameter: {parameter}")
    
    cmap_name = scale.get("cmap")
    if cmap_name is None:
        raise HTTPException(status_code=400, detail="Parameter has no colormap (contour-only)")
    vmin = scale.get("vmin", 0)
    vmax = scale.get("vmax", 1)
    unit = scale.get("unit", "")
    label = scale.get("label", parameter)

    try:
        # Open image and sample pixel
        with Image.open(png_path) as img:
            width, height = img.size
            
            # Convert lat/lon to pixel coordinates
            # Note: PNG y-axis is inverted (0 at top)
            # Image is rendered in Web Mercator Y-space, so lat must be
            # projected to Mercator before interpolating the pixel row.
            px = int((lon - bounds["west"]) / (bounds["east"] - bounds["west"]) * width)
            merc = lambda d: math.degrees(math.log(math.tan(math.pi / 4 + math.radians(d) / 2)))
            merc_north = merc(bounds["north"])
            merc_south = merc(bounds["south"])
            merc_lat = merc(lat)
            py = int((merc_north - merc_lat) / (merc_north - merc_south) * height)
            
            # Clamp to image bounds
            px = max(0, min(px, width - 1))
            py = max(0, min(py, height - 1))
            
            # Get pixel color
            pixel = img.getpixel((px, py))
            if isinstance(pixel, int):
                # Grayscale
                rgb = (pixel, pixel, pixel)
            elif len(pixel) >= 4 and pixel[3] < 128:
                # Transparent pixel (alpha < 50%), treat as no data
                raise HTTPException(status_code=404, detail="No data at this location (transparent pixel)")
            else:
                rgb = (pixel[0], pixel[1], pixel[2])
            
            # Convert color to value (pass parameter to handle reflectivity correctly)
            value = _rgb_to_value(rgb, cmap_name, vmin, vmax, parameter)
            
            if value is None:
                raise HTTPException(status_code=404, detail="Could not determine value from pixel color")
            
            return JSONResponse({
                "value": round(value, 2),
                "unit": unit,
                "label": label,
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "source": "png",
            })
    
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Model Grid API — Client-side value lookup
# ---------------------------------------------------------------------------

@app.get("/api/grid/{model}/{run_id}/{fxx}/{parameter}")
async def get_model_grid(
    model: str, run_id: str, fxx: int, parameter: str,
):
    """Get raw model grid as binary for client-side querying."""
    return get_model_grid_binary(model, run_id, fxx, parameter)


# ---------------------------------------------------------------------------
# Legend endpoint — color scale data for the map legend overlay
# ---------------------------------------------------------------------------

_NWS_REFL_COLORS = [
    "#66d9ff","#0099cc","#00cc00","#009900","#ffff00","#ffcc00",
    "#ff9900","#ff0000","#cc0000","#990000","#ff00ff","#cc00cc",
    "#990099","#ffffff",
]
_NWS_REFL_BOUNDS = [5,10,15,20,25,30,35,40,45,50,55,60,65,70,75]


@app.get("/api/legend/{parameter}")
async def get_legend(parameter: str):
    import matplotlib.cm as cm
    scale = COLOR_SCALES.get(parameter)
    if not scale:
        raise HTTPException(status_code=404, detail="Unknown parameter")

    cmap_name = scale.get("cmap")
    vmin = scale.get("vmin", 0)
    vmax = scale.get("vmax", 1)
    label = scale.get("label", parameter)
    unit = scale.get("unit", "")
    bounds = None
    colors = None

    if scale.get("bounds") and scale.get("colors"):
        bounds = list(scale["bounds"])
        colors = list(scale["colors"])
        stops = [
            {"pos": i / max(len(colors) - 1, 1), "color": color}
            for i, color in enumerate(colors)
        ]
        discrete = True
        tick_vals = scale.get("tick_vals", bounds)
    elif cmap_name == "nws_refl":
        bounds = _NWS_REFL_BOUNDS
        colors = _NWS_REFL_COLORS
        stops = [{"pos": i / (len(_NWS_REFL_COLORS) - 1), "color": c}
                 for i, c in enumerate(_NWS_REFL_COLORS)]
        discrete = True
        tick_vals = [5, 15, 25, 35, 45, 55, 65, 75]
    elif cmap_name and cmap_name.startswith("radar_"):
        # Level II radar: generate stops from actual LUT color tables
        _radar_param_field = {
            "radar_reflectivity": "reflectivity",
            "radar_velocity": "velocity",
            "radar_sw": "spectrum_width",
            "radar_zdr": "differential_reflectivity",
            "radar_cc": "cross_correlation_ratio",
            "radar_phidp": "differential_phase",
            "radar_kdp": "specific_differential_phase",
            "radar_srm": "storm_relative_velocity",
            "radar_azs": "azimuthal_shear",
            "radar_div": "divergence",
            "radar_cct": "cc_texture",
            "radar_pht": "phi_texture",
            "radar_tds": "debris_flag",
            "radar_cref": "composite_reflectivity",
            "radar_etop": "echo_tops",
            "radar_vil": "vertically_integrated_liquid",
            "radar_vxt": "velocity_texture",
            "radar_hail": "hail_probability",
            "radar_rain": "rainfall_rate",
        }
        field_cfg = RADAR_FIELD_CONFIG.get(_radar_param_field.get(parameter, ""))
        lut_fn = field_cfg.get("lut_fn") if field_cfg else None
        if lut_fn:
            lut, lut_vmin, lut_vmax = lut_fn()
            vmin = lut_vmin
            vmax = lut_vmax
            label = field_cfg.get("label", label)
            unit = field_cfg.get("unit", unit)
            if field_cfg.get("bounds") and field_cfg.get("colors"):
                bounds = list(field_cfg["bounds"])
                colors = list(field_cfg["colors"])
                stops = [
                    {"pos": i / max(len(colors) - 1, 1), "color": color}
                    for i, color in enumerate(colors)
                ]
                discrete = True
                tick_vals = field_cfg.get("legend_ticks", bounds)
                color_lookup = None
            else:
                stops = []
                n_stops = 32
                for i in range(n_stops):
                    idx = int(i / (n_stops - 1) * 255)
                    r, g, b, a = lut[idx]
                    pos = i / (n_stops - 1)
                    stops.append({"pos": pos, "color": f"rgba({r},{g},{b},{a/255:.2f})"})
                discrete = False
                tick_vals = None
        else:
            stops = []
            discrete = False
            tick_vals = None
    elif cmap_name:
        try:
            cmap_obj = cm.get_cmap(cmap_name)
        except Exception:
            cmap_obj = cm.get_cmap("viridis")
        stops = []
        for t in [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
            r, g, b, _ = cmap_obj(t)
            stops.append({"pos": t, "color": f"rgb({int(r*255)},{int(g*255)},{int(b*255)})"})
        discrete = False
        tick_vals = None
    else:
        stops = []
        discrete = False
        tick_vals = None

    # Build full 256-entry enhanced color lookup for client-side mouseover.
    # This uses the exact same logic as _build_cmap_lookup so the frontend
    # doesn't need to approximate colormap interpolation or enhancement.
    color_lookup = None
    if discrete and bounds and colors:
        def _hex_to_rgb(color: str):
            color = color.strip()
            if color.startswith("#") and len(color) == 7:
                return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            return None

        color_lookup = []
        for i, color in enumerate(colors):
            rgb = _hex_to_rgb(color)
            if not rgb:
                continue
            upper_idx = min(i + 1, len(bounds) - 1)
            value = (float(bounds[i]) + float(bounds[upper_idx])) / 2.0
            color_lookup.append([rgb[0], rgb[1], rgb[2], round(value, 2)])
    elif cmap_name and not cmap_name.startswith("nws_") and not cmap_name.startswith("radar_"):
        cache_key = f"{cmap_name}_{vmin}_{vmax}_{parameter or 'default'}"
        cached = _CMAP_CACHE.get(cache_key)
        if cached is None or not isinstance(cached, tuple):
            lookup = _build_cmap_lookup(cmap_name, vmin, vmax, parameter)
            cached = (lookup, *_lookup_arrays(lookup))
            _CMAP_CACHE[cache_key] = cached
            _prune_cache(_CMAP_CACHE, _CMAP_CACHE_MAX)
        lookup = cached[0]
        # Convert to list of [r, g, b, value] for JSON
        color_lookup = [[r, g, b, round(v, 2)] for (r, g, b), v in lookup.items()]

    return JSONResponse({
        "label": label, "unit": unit,
        "vmin": vmin, "vmax": vmax,
        "stops": stops, "discrete": discrete,
        "tick_vals": tick_vals,
        "bounds": bounds,
        "colors": colors,
        "color_lookup": color_lookup,
    })


# ---------------------------------------------------------------------------
# NWS Alerts endpoint
# ---------------------------------------------------------------------------

_alerts_cache: dict = {"data": None, "ts": 0}
_zone_geom_cache: dict = {}  # zone_id -> geometry dict
_ALERTS_TTL = 60  # seconds
_spc_cache: dict = {}
_spc_text_cache: dict = {}
_SPC_TTL = 300
_mcd_cache: dict = {"data": None, "ts": 0}
_MCD_TTL = 300
_home_cache: dict = {"data": None, "ts": 0}
_HOME_TTL = 60
_weather_wire_cache: dict = {"data": None, "ts": 0}
_WEATHER_WIRE_TTL = 300
_spotter_cache: dict = {"data": None, "ts": 0}
_SPOTTER_TTL = 60
_surface_cache: dict = {"data": None, "ts": 0}
_SURFACE_TTL = 120  # serve cached data up to 2 min; pipeline refreshes every 5 min
_dashboard_cache: dict = {"data": None, "ts": 0}
_DASHBOARD_TTL = 30
_PROCESS_ROWS_CACHE: dict = {"data": None, "ts": 0}
_PROCESS_ROWS_TTL = 10
_MODEL_STATUS_CACHE: dict = {"data": None, "ts": 0}
_MODEL_STATUS_TTL = 30
_DASHBOARD_LOG_CACHE: dict = {"pipeline": None, "radar": None, "ts": 0}
_DASHBOARD_LOG_TTL = 30
_satellite_status_cache: dict = {"data": None, "ts": 0}
_SATELLITE_STATUS_TTL = 30
_radar_status_cache: dict = {"data": None, "ts": 0}
_RADAR_STATUS_TTL = 30
_disk_usage_cache: dict = {"data": None, "ts": 0}
_DISK_USAGE_TTL = 300

# ---------------------------------------------------------------------------
# Background CPU sampler — reads /proc every 3 s so dashboard always has
# a fresh, accurate δ-based CPU percentage for both system-wide and
# per-process metrics.  Memory (RSS) is already instantaneous via /proc.
# ---------------------------------------------------------------------------
_CPU_SAMPLE_INTERVAL = 3  # seconds

# Cgroup paths for service-level CPU + memory stats
_CGROUP_SERVICES = {
    "models": "weather-pipeline.service",
    "lowpri": "weather-pipeline-lowpri.service",
    "radar": "weather-radar.service",
    "satellite": "weather-satellite.service",
}
_CGROUP_BASE = Path("/sys/fs/cgroup/system.slice")


def _read_cgroup_cpu_quota(service_unit: str) -> int | None:
    """Read cpu.max and return quota as a percentage (e.g. 200), or None if uncapped."""
    try:
        raw = (_CGROUP_BASE / service_unit / "cpu.max").read_text().strip()
        quota_str, period_str = raw.split()
        if quota_str == "max":
            return None
        return round(int(quota_str) / int(period_str) * 100)
    except Exception:
        return None


def _read_cgroup_usage_usec(service_unit: str) -> int | None:
    """Read usage_usec from cpu.stat."""
    try:
        for line in (_CGROUP_BASE / service_unit / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
    except Exception:
        pass
    return None


def _read_cgroup_throttled(service_unit: str) -> bool:
    """Return True if the service is currently being throttled."""
    try:
        for line in (_CGROUP_BASE / service_unit / "cpu.stat").read_text().splitlines():
            if line.startswith("nr_throttled "):
                return int(line.split()[1]) > 0
    except Exception:
        pass
    return False


def _read_cgroup_memory(service_unit: str) -> int | None:
    """Read current memory usage from memory.current."""
    try:
        return int((_CGROUP_BASE / service_unit / "memory.current").read_text().strip())
    except Exception:
        return None


class _CpuSampler:
    """Lightweight daemon thread that keeps psutil CPU deltas warm."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._system_cpu: float = 0.0
        # pid → psutil.Process  (kept alive so .cpu_percent() accumulates δ)
        self._procs: dict[int, psutil.Process] = {}
        self._proc_cpu: dict[int, float] = {}  # pid → last sampled %
        # Service-level cgroup stats
        self._svc_cpu: dict[str, float] = {}  # key → cpu %
        self._svc_quota: dict[str, int | None] = {}  # key → quota % or None
        self._svc_throttled: dict[str, bool] = {}  # key → currently throttled
        self._svc_mem: dict[str, int] = {}  # key → memory bytes
        self._prev_usage: dict[str, int] = {}  # key → previous usage_usec
        self._prev_ts: float = 0.0
        self._stop = threading.Event()
        # Prime the first reading (returns 0.0, sets baseline)
        psutil.cpu_percent(interval=None, percpu=False)
        # Prime cgroup baselines
        for key, unit in _CGROUP_SERVICES.items():
            usage = _read_cgroup_usage_usec(unit)
            if usage is not None:
                self._prev_usage[key] = usage
        self._prev_ts = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cpu-sampler")
        self._thread.start()

    # -- public API (called from request handlers) -------------------------

    def system_cpu(self) -> float:
        with self._lock:
            return self._system_cpu

    def process_cpu(self, pid: int) -> float:
        """Return most-recent δ CPU% for *pid*, or 0.0 if unknown."""
        with self._lock:
            return self._proc_cpu.get(pid, 0.0)

    def service_summaries(self) -> list[dict]:
        """Return per-service CPU/memory summary for the dashboard."""
        labels = {"models": "Models", "lowpri": "Low Priority", "radar": "Radar", "satellite": "Satellite"}
        with self._lock:
            result = []
            for key in _CGROUP_SERVICES:
                result.append({
                    "name": labels.get(key, key),
                    "key": key,
                    "cpu_percent": round(self._svc_cpu.get(key, 0.0), 1),
                    "cpu_quota": self._svc_quota.get(key),
                    "throttled": self._svc_throttled.get(key, False),
                    "mem_bytes": self._svc_mem.get(key, 0),
                })
            return result

    def stop(self) -> None:
        self._stop.set()

    # -- background loop ---------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(timeout=_CPU_SAMPLE_INTERVAL):
            try:
                now = time.time()
                sys_pct = psutil.cpu_percent(interval=None, percpu=False)

                # --- per-process sampling ---
                current_pids: set[int] = set()
                for proc in psutil.process_iter(["pid", "cmdline"]):
                    try:
                        cmdline = " ".join(proc.info["cmdline"] or [])
                        if "python" not in cmdline.lower():
                            continue
                        current_pids.add(proc.info["pid"])
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

                new_cpu: dict[int, float] = {}

                # --- cgroup service sampling ---
                dt = now - self._prev_ts if self._prev_ts else _CPU_SAMPLE_INTERVAL
                new_svc_cpu: dict[str, float] = {}
                new_svc_quota: dict[str, int | None] = {}
                new_svc_throttled: dict[str, bool] = {}
                new_svc_mem: dict[str, int] = {}
                new_prev_usage: dict[str, int] = {}
                for key, unit in _CGROUP_SERVICES.items():
                    new_svc_quota[key] = _read_cgroup_cpu_quota(unit)
                    usage = _read_cgroup_usage_usec(unit)
                    if usage is not None:
                        prev = self._prev_usage.get(key)
                        if prev is not None and dt > 0:
                            delta_usec = usage - prev
                            # Convert: (delta_usec / (dt * 1_000_000)) * 100 = cpu%
                            new_svc_cpu[key] = (delta_usec / (dt * 1_000_000)) * 100
                        new_prev_usage[key] = usage
                    # Throttle detection: compare nr_throttled with previous
                    try:
                        for line in (_CGROUP_BASE / unit / "cpu.stat").read_text().splitlines():
                            if line.startswith("nr_throttled "):
                                nr = int(line.split()[1])
                                prev_nr = getattr(self, '_prev_nr_throttled', {}).get(key, 0)
                                new_svc_throttled[key] = nr > prev_nr
                                if not hasattr(self, '_next_nr_throttled'):
                                    self._next_nr_throttled = {}
                                self._next_nr_throttled[key] = nr
                                break
                    except Exception:
                        new_svc_throttled[key] = False
                    mem = _read_cgroup_memory(unit)
                    if mem is not None:
                        new_svc_mem[key] = mem

                with self._lock:
                    # Per-process
                    for pid in current_pids:
                        if pid not in self._procs:
                            try:
                                p = psutil.Process(pid)
                                p.cpu_percent(interval=None)
                                self._procs[pid] = p
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                continue
                    dead: list[int] = []
                    for pid, p in self._procs.items():
                        try:
                            new_cpu[pid] = p.cpu_percent(interval=None)
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            dead.append(pid)
                    for pid in dead:
                        del self._procs[pid]
                    self._system_cpu = sys_pct
                    self._proc_cpu = new_cpu
                    # Service-level
                    self._svc_cpu = new_svc_cpu
                    self._svc_quota = new_svc_quota
                    self._svc_throttled = new_svc_throttled
                    self._svc_mem = new_svc_mem
                    self._prev_usage = new_prev_usage
                    self._prev_ts = now
                    self._prev_nr_throttled = getattr(self, '_next_nr_throttled', {})
            except Exception:
                pass  # never crash the sampler

_cpu_sampler = _CpuSampler()
atexit.register(_cpu_sampler.stop)

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
NWS_HEADERS = {
    "User-Agent": "(wx-viewer, weather-viewer@localhost)",
    "Accept": "application/geo+json",
}

# Alert type styling for the frontend
ALERT_STYLES = {
    "Tornado Warning":              {"color": "#ff0000", "weight": 3, "fill": 0.30, "z": 100},
    "Tornado Emergency":           {"color": "#ff00ff", "weight": 4, "fill": 0.40, "z": 110},
    "Particularly Dangerous Situation":
                                    {"color": "#ff00ff", "weight": 3, "fill": 0.35, "z": 99},
    "Severe Thunderstorm Warning":  {"color": "#ffff00", "weight": 2, "fill": 0.22, "z": 90},
    "Flash Flood Warning":          {"color": "#00ff00", "weight": 2, "fill": 0.20, "z": 80},
    "Tornado Watch":                {"color": "#ff0000", "weight": 0.8, "fill": 0.035, "z": 70},
    "Severe Thunderstorm Watch":    {"color": "#ffff00", "weight": 0.8, "fill": 0.03, "z": 60},
    "Flood Warning":                {"color": "#00aa00", "weight": 1, "fill": 0.10, "z": 50},
    "Winter Storm Warning":         {"color": "#ff69b4", "weight": 2, "fill": 0.15, "z": 45},
    "Blizzard Warning":             {"color": "#ff4500", "weight": 2, "fill": 0.20, "z": 44},
    "Ice Storm Warning":            {"color": "#8b008b", "weight": 2, "fill": 0.15, "z": 43},
    "Winter Weather Advisory":      {"color": "#7b68ee", "weight": 1, "fill": 0.10, "z": 40},
    "Wind Advisory":                {"color": "#d2b48c", "weight": 1, "fill": 0.08, "z": 30},
    "Dense Fog Advisory":           {"color": "#708090", "weight": 1, "fill": 0.08, "z": 25},
    "Special Weather Statement":    {"color": "#ffe4b5", "weight": 1, "fill": 0.05, "z": 10},
}
_DEFAULT_STYLE = {"color": "#888888", "weight": 1, "fill": 0.05, "z": 5}

SPC_PRODUCTS = {
    "day1_cat": {"layer": 1, "label": "Day 1 Categorical Outlook", "fill_opacity": 0.18, "weight": 1.5, "day": "day1", "kind": "categorical", "detail": "General categorical severe-weather risk."},
    "day1_sig_torn": {"layer": 2, "label": "Day 1 Significant Tornado Outlook", "fill_opacity": 0.0, "weight": 2.0, "day": "day1", "kind": "significant", "detail": "Significant tornado area, generally indicating potential for strong tornadoes.", "hatch": True},
    "day1_torn": {"layer": 3, "label": "Day 1 Tornado Outlook", "fill_opacity": 0.16, "weight": 1.5, "day": "day1", "kind": "probability", "detail": "Probability of tornadoes within 25 miles of a point."},
    "day1_sig_hail": {"layer": 4, "label": "Day 1 Significant Hail Outlook", "fill_opacity": 0.0, "weight": 2.0, "day": "day1", "kind": "significant", "detail": "Significant hail area, generally indicating potential for 2-inch or larger hail.", "hatch": True},
    "day1_hail": {"layer": 5, "label": "Day 1 Hail Outlook", "fill_opacity": 0.16, "weight": 1.5, "day": "day1", "kind": "probability", "detail": "Probability of severe hail within 25 miles of a point."},
    "day1_sig_wind": {"layer": 6, "label": "Day 1 Significant Wind Outlook", "fill_opacity": 0.0, "weight": 2.0, "day": "day1", "kind": "significant", "detail": "Significant wind area, generally indicating potential for 75 mph or greater gusts.", "hatch": True},
    "day1_wind": {"layer": 7, "label": "Day 1 Wind Outlook", "fill_opacity": 0.16, "weight": 1.5, "day": "day1", "kind": "probability", "detail": "Probability of damaging thunderstorm winds within 25 miles of a point."},
    "day2_cat": {"layer": 9, "label": "Day 2 Categorical Outlook", "fill_opacity": 0.16, "weight": 1.5, "day": "day2", "kind": "categorical", "detail": "General categorical severe-weather risk."},
    "day2_sig_torn": {"layer": 10, "label": "Day 2 Significant Tornado Outlook", "fill_opacity": 0.0, "weight": 2.0, "day": "day2", "kind": "significant", "detail": "Significant tornado area, generally indicating potential for strong tornadoes.", "hatch": True},
    "day2_torn": {"layer": 11, "label": "Day 2 Tornado Outlook", "fill_opacity": 0.15, "weight": 1.5, "day": "day2", "kind": "probability", "detail": "Probability of tornadoes within 25 miles of a point."},
    "day2_sig_hail": {"layer": 12, "label": "Day 2 Significant Hail Outlook", "fill_opacity": 0.0, "weight": 2.0, "day": "day2", "kind": "significant", "detail": "Significant hail area, generally indicating potential for 2-inch or larger hail.", "hatch": True},
    "day2_hail": {"layer": 13, "label": "Day 2 Hail Outlook", "fill_opacity": 0.15, "weight": 1.5, "day": "day2", "kind": "probability", "detail": "Probability of severe hail within 25 miles of a point."},
    "day2_sig_wind": {"layer": 14, "label": "Day 2 Significant Wind Outlook", "fill_opacity": 0.0, "weight": 2.0, "day": "day2", "kind": "significant", "detail": "Significant wind area, generally indicating potential for 75 mph or greater gusts.", "hatch": True},
    "day2_wind": {"layer": 15, "label": "Day 2 Wind Outlook", "fill_opacity": 0.15, "weight": 1.5, "day": "day2", "kind": "probability", "detail": "Probability of damaging thunderstorm winds within 25 miles of a point."},
    "day3_cat": {"layer": 17, "label": "Day 3 Categorical Outlook", "fill_opacity": 0.14, "weight": 1.5, "day": "day3", "kind": "categorical", "detail": "General categorical severe-weather risk."},
    "day3_sig": {"layer": 18, "label": "Day 3 Significant Severe Outlook", "fill_opacity": 0.0, "weight": 2.0, "day": "day3", "kind": "significant", "detail": "Significant severe area highlighting higher-end severe potential.", "hatch": True},
    "day3_prob": {"layer": 19, "label": "Day 3 Probabilistic Outlook", "fill_opacity": 0.14, "weight": 1.5, "day": "day3", "kind": "probability", "detail": "Probability of severe weather within 25 miles of a point."},
}
SPC_BASE_URL = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer"
SPC_MCD_URL = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/spc_mesoscale_discussion/MapServer/0/query"
SPC_TEXT_URLS = {
    "day1": "https://www.spc.noaa.gov/products/outlook/day1otlk.html",
    "day2": "https://www.spc.noaa.gov/products/outlook/day2otlk.html",
    "day3": "https://www.spc.noaa.gov/products/outlook/day3otlk.html",
}
SPOTTER_NETWORK_URL = "https://www.spotternetwork.org/feeds/gr.txt"
SPC_RSS_URL = "https://www.spc.noaa.gov/products/spcrss.xml"
NWS_NEWS_URL = "https://www.weather.gov/news"

_EXCLUDED_EVENTS = {
    "Small Craft Advisory",
    "Gale Warning",
    "Brisk Wind Advisory",
    "Heavy Freezing Spray Warning",
    "High Wind Watch",
    "Rip Current Statement",
    "Red Flag Warning",
    "Gale Watch",
    "Test Message",
    "Frost Advisory",
    "Flood Watch",
    "Flood Advisory",
    "Flood Warning",
    "High Surf Advisory",
    "Air Quality Alert",
    "Dense Fog Advisory",
    "Special Weather Statement",
}

_SEVERE_EVENTS = {"Tornado Warning", "Tornado Emergency", "Severe Thunderstorm Warning", "Flash Flood Warning"}
_OVERLAY_ALERT_EVENTS = {
    "Tornado Watch",
    "Severe Thunderstorm Watch",
    "Flash Flood Warning",
    "Severe Thunderstorm Warning",
    "Tornado Warning",
}


def _alert_display_event(event: str, headline: str, description: str) -> str:
    combined = f"{headline}\n{description}".lower()
    if event == "Tornado Warning" and "tornado emergency" in combined:
        return "Tornado Emergency"
    return event


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_satellite_meta_timestamp(value: str | None) -> datetime | None:
    """Parse mixed satellite timestamp formats emitted by older/newer renderers."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    direct = _parse_iso_dt(text)
    if direct is not None:
        return direct

    normalized = re.sub(r"T(\d{2})-(\d{2})-(\d{2})(\.\d+)?Z$", r"T\1:\2:\3\4Z", text)
    direct = _parse_iso_dt(normalized)
    if direct is not None:
        return direct

    for candidate in (text, normalized):
        for fmt in ("%Y-%jT%H:%M:%SZ", "%Y-%jT%H:%MZ"):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def _load_freshest_satellite_meta(
    meta_dir: Path,
    satellite: str,
    product: str,
    region: str | None = None,
) -> dict | None:
    """Load the freshest satellite metadata for a product, ignoring stale latest pointers."""
    freshest_meta = None
    freshest_key = None

    pattern = f"{satellite}_{product}_*.json" if region is None else f"{satellite}_{product}_*_{region}.json"
    for meta_file in meta_dir.glob(pattern):
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
            continue

        meta_product = str(meta.get("product") or "")
        meta_satellite = str(meta.get("satellite") or "").lower()
        if meta_satellite.startswith("g") and meta_satellite[1:].isdigit():
            meta_satellite = f"goes{meta_satellite[1:]}"
        elif meta_satellite.startswith("goes-") and meta_satellite[5:].isdigit():
            meta_satellite = f"goes{meta_satellite[5:]}"
        if meta_product != product or meta_satellite != satellite:
            continue
        if meta.get("region") != region:
            continue

        ts = _parse_satellite_meta_timestamp(meta.get("timestamp"))
        ts_key = ts.timestamp() if ts is not None else meta_file.stat().st_mtime
        key = (ts_key, meta_file.stat().st_mtime, meta_file.name)
        if freshest_key is None or key > freshest_key:
            freshest_key = key
            freshest_meta = meta

    return freshest_meta


def _geometry_centroid(geometry: dict | None) -> dict | None:
    if not geometry:
        return None
    coords = geometry.get("coordinates")
    gtype = geometry.get("type")
    if not coords or not gtype:
        return None

    points: list[tuple[float, float]] = []

    def _collect(node):
        if not isinstance(node, list) or not node:
            return
        if isinstance(node[0], (int, float)) and len(node) >= 2:
            points.append((float(node[0]), float(node[1])))
            return
        for child in node:
            _collect(child)

    _collect(coords)
    if not points:
        return None

    lon = sum(pt[0] for pt in points) / len(points)
    lat = sum(pt[1] for pt in points) / len(points)
    return {"lat": lat, "lon": lon}


def _parse_spotter_dt(value: str) -> str | None:
    value = value.strip()
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _parse_spc_discussion_html(html_text: str) -> str:
    marker = "Forecast Discussion"
    start = html_text.find(marker)
    if start < 0:
        return ""
    text = html_text[start + len(marker):]
    end_markers = [
        "CLICK TO GET",
        "NOTE: THE NEXT DAY",
        "Top/",
    ]
    end_positions = [text.find(token) for token in end_markers if text.find(token) >= 0]
    if end_positions:
        text = text[:min(end_positions)]
    text = re.sub(r"<[^>]+>", "\n", text)
    text = unescape(text)
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned: list[str] = []
    blank_run = 0
    for line in lines:
        compact = re.sub(r"\s+", " ", line).strip()
        if not compact:
            blank_run += 1
            if cleaned and blank_run <= 1:
                cleaned.append("")
            continue
        blank_run = 0
        cleaned.append(compact)
    return "\n".join(cleaned).strip()


async def _fetch_spc_discussion(day: str | None) -> dict:
    if not day or day not in SPC_TEXT_URLS:
        return {"day": day, "text": "", "updated": None, "source_url": None}

    now = time.time()
    cached = _spc_text_cache.get(day)
    if cached and (now - cached["ts"]) < _SPC_TTL:
        return cached["data"]

    source_url = SPC_TEXT_URLS[day]
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(source_url, headers={"User-Agent": NWS_HEADERS["User-Agent"]})
            resp.raise_for_status()
            text = _parse_spc_discussion_html(resp.text)
    except Exception:
        if cached:
            return cached["data"]
        text = ""

    payload = {
        "day": day,
        "text": text,
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_url": source_url,
    }
    _spc_text_cache[day] = {"data": payload, "ts": now}
    _prune_cache(_spc_text_cache, _SPC_TEXT_CACHE_MAX)
    return payload


def _parse_rss_items(xml_text: str, source_name: str, limit: int = 6) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items

    for item in root.findall(".//item"):
        title = _strip_html(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        summary = _strip_html(item.findtext("description"))
        published = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        items.append({
            "source": source_name,
            "title": title,
            "summary": summary[:220],
            "url": link,
            "published": published,
        })
        if len(items) >= limit:
            break
    return items


def _parse_weather_news_html(html_text: str, limit: int = 6) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()

    pattern = re.compile(
        r'<a[^>]+href="(?P<href>/news/[^"]+|https://www\.weather\.gov/news/[^"]+)"[^>]*>(?P<title>.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html_text):
        href = match.group("href").strip()
        title = _strip_html(match.group("title"))
        if not title or len(title) < 12:
            continue
        url = urljoin(NWS_NEWS_URL, href)
        if url in seen:
            continue
        seen.add(url)
        items.append({
            "source": "NWS",
            "title": title,
            "summary": "",
            "url": url,
            "published": "",
        })
        if len(items) >= limit:
            break
    return items


async def _fetch_weather_wire(limit: int = 8) -> dict:
    now = time.time()
    if _weather_wire_cache["data"] is not None and (now - _weather_wire_cache["ts"]) < _WEATHER_WIRE_TTL:
        return _weather_wire_cache["data"]

    headers = {"User-Agent": NWS_HEADERS["User-Agent"]}
    spc_items: list[dict] = []
    nws_items: list[dict] = []

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            spc_resp, nws_resp = await asyncio.gather(
                client.get(SPC_RSS_URL, headers=headers),
                client.get(NWS_NEWS_URL, headers=headers),
            )
            if spc_resp.is_success:
                spc_items = _parse_rss_items(spc_resp.text, "SPC", limit=limit)
            if nws_resp.is_success:
                nws_items = _parse_weather_news_html(nws_resp.text, limit=limit)
    except Exception:
        if _weather_wire_cache["data"] is not None:
            return _weather_wire_cache["data"]

    items = (nws_items[:4] + spc_items[:4])[:limit]

    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(items),
        "items": items,
        "sources": ["NWS", "SPC"],
        "x_feed": {
            "enabled": False,
            "status": "placeholder",
            "note": "Add X API credentials/config to enable hashtag feeds such as #iawx, #okwx, and #newx.",
        },
    }
    _weather_wire_cache["data"] = payload
    _weather_wire_cache["ts"] = now
    return payload


def _safe_spotter_name(raw: str) -> str:
    raw = raw.strip().strip('"')
    return re.sub(r"\s+", " ", raw)[:80]


def _parse_spotter_feed(text: str) -> dict:
    features = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("Object:"):
            i += 1
            continue
        try:
            lat_str, lon_str = [part.strip() for part in line.split(":", 1)[1].split(",", 1)]
            lat = float(lat_str)
            lon = float(lon_str)
        except Exception:
            i += 1
            continue

        info_text = ""
        label = "Spotter"
        j = i + 1
        while j < len(lines):
            next_line = lines[j].strip()
            if next_line.startswith("Icon:") and '"' in next_line:
                info_text = next_line.split('"', 1)[1].rsplit('"', 1)[0]
            elif next_line.startswith("Text:") and '"' in next_line:
                label = next_line.split('"', 1)[1].rsplit('"', 1)[0]
            elif next_line == "End:":
                break
            j += 1

        details = [part.strip() for part in info_text.split("\\n") if part.strip()]
        if details:
            label = _safe_spotter_name(details[0])
        timestamp = _parse_spotter_dt(details[1]) if len(details) > 1 else None
        status = details[2] if len(details) > 2 else ""

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "name": label,
                "timestamp": timestamp,
                "status": status,
            },
        })
        i = j + 1

    return {
        "type": "FeatureCollection",
        "features": features,
        "count": len(features),
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


async def _fetch_alerts_data() -> dict:
    now = time.time()
    if _alerts_cache["data"] is not None and (now - _alerts_cache["ts"]) < _ALERTS_TTL:
        return _alerts_cache["data"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(NWS_ALERTS_URL, headers=NWS_HEADERS)
            resp.raise_for_status()
            geojson = resp.json()

            # Collect zone URLs that need resolving
            features_pending = []
            zone_urls_needed = set()

            for f in geojson.get("features", []):
                props = f.get("properties", {})
                event = props.get("event", "")
                if event in _EXCLUDED_EVENTS:
                    continue
                geometry = f.get("geometry")
                zones = props.get("affectedZones", [])
                if not geometry and zones:
                    for z in zones:
                        if z not in _zone_geom_cache:
                            zone_urls_needed.add(z)
                features_pending.append((f, geometry, zones))

            # Batch-resolve missing zone geometries (limit concurrency)
            import asyncio
            zone_urls_list = list(zone_urls_needed)
            # Process in batches of 20 to avoid overwhelming the API
            for i in range(0, len(zone_urls_list), 20):
                batch = zone_urls_list[i:i+20]
                await asyncio.gather(
                    *[_resolve_zone_geometry(client, url) for url in batch]
                )

    except Exception as exc:
        if _alerts_cache["data"] is not None:
            return _alerts_cache["data"]
        raise HTTPException(status_code=502, detail=f"NWS API error: {exc}")

    # Build final features with resolved geometries
    features = []
    for f, geometry, zones in features_pending:
        props = f.get("properties", {})
        event = props.get("event", "")
        headline = props.get("headline", "")
        description = (props.get("description") or "")[:500]
        display_event = _alert_display_event(event, headline, description)
        style = ALERT_STYLES.get(display_event)
        if not style:
            continue  # skip events without a defined style

        # If no inline geometry, merge zone geometries
        if not geometry and zones:
            zone_polys = []
            for z in zones:
                zg = _zone_geom_cache.get(z)
                if zg and zg.get("type") in ("Polygon", "MultiPolygon"):
                    if zg["type"] == "Polygon":
                        zone_polys.append(zg["coordinates"])
                    else:
                        zone_polys.extend(zg["coordinates"])
            if zone_polys:
                geometry = {"type": "MultiPolygon", "coordinates": zone_polys}

        if not geometry:
            continue

        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "event": event,
                "display_event": display_event,
                "headline": headline,
                "description": description,
                "area_desc": props.get("areaDesc", ""),
                "severity": props.get("severity", ""),
                "urgency": props.get("urgency", ""),
                "sender": props.get("senderName", ""),
                "sent": props.get("sent"),
                "onset": props.get("onset"),
                "expires": props.get("expires"),
                "style": style,
            },
        })

    # Sort by z-index (most important on top)
    features.sort(key=lambda f: f["properties"]["style"]["z"], reverse=True)

    result = {
        "type": "FeatureCollection",
        "features": features,
        "updated": geojson.get("updated"),
        "styles": ALERT_STYLES,
    }
    _alerts_cache["data"] = result
    _alerts_cache["ts"] = now
    return result


async def _fetch_spc_geojson(product_id: str) -> dict:
    if product_id not in SPC_PRODUCTS:
        raise HTTPException(status_code=404, detail="Unknown SPC product")

    now = time.time()
    cached = _spc_cache.get(product_id)
    if cached and (now - cached["ts"]) < _SPC_TTL:
        return cached["data"]

    config = SPC_PRODUCTS[product_id]
    discussion = await _fetch_spc_discussion(config.get("day"))
    query_url = (
        f"{SPC_BASE_URL}/{config['layer']}/query"
        "?where=1%3D1&outFields=*&returnGeometry=true&f=geojson"
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(query_url, headers={"User-Agent": NWS_HEADERS["User-Agent"]})
            resp.raise_for_status()
            geojson = resp.json()
    except Exception as exc:
        if cached:
            return cached["data"]
        raise HTTPException(status_code=502, detail=f"SPC API error: {exc}")

    features = []
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        fill = props.get("fill") or "#888888"
        stroke = props.get("stroke") or "#bbbbbb"
        label = props.get("label2") or props.get("label") or config["label"]
        significant = bool(config.get("hatch"))
        detail_lines = [label]
        if config.get("detail"):
            detail_lines.append(config["detail"])
        if significant:
            detail_lines.append("Hatched area denotes significant severe potential.")
        features.append({
            "type": "Feature",
            "geometry": feature.get("geometry"),
            "properties": {
                "label": label,
                "valid": props.get("valid"),
                "expire": props.get("expire"),
                "issue": props.get("issue"),
                "day": config.get("day"),
                "kind": config.get("kind"),
                "significant": significant,
                "detail": config.get("detail"),
                "detail_lines": detail_lines,
                "discussion_text": discussion.get("text") or "",
                "discussion_source_url": discussion.get("source_url"),
                "style": {
                    "color": stroke,
                    "fillColor": fill,
                    "fillOpacity": config["fill_opacity"],
                    "weight": config["weight"],
                    "dashArray": "6 4" if significant else None,
                    "hatch": significant,
                    "hatchColor": stroke or "#111111",
                },
            },
        })

    result = {
        "type": "FeatureCollection",
        "product": product_id,
        "label": config["label"],
        "day": config.get("day"),
        "kind": config.get("kind"),
        "significant": bool(config.get("hatch")),
        "discussion_text": discussion.get("text") or "",
        "discussion_source_url": discussion.get("source_url"),
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "features": features,
    }
    _spc_cache[product_id] = {"data": result, "ts": now}
    _prune_cache(_spc_cache, _SPC_CACHE_MAX)
    return result


async def _fetch_mcd_data(limit: int = 12) -> dict:
    now = time.time()
    cached = _mcd_cache.get("data")
    if cached is not None and (now - _mcd_cache["ts"]) < _MCD_TTL:
        return {
            **cached,
            "items": cached.get("items", [])[:limit],
            "count": min(limit, cached.get("count", 0)),
        }

    query_url = f"{SPC_MCD_URL}?where=1%3D1&outFields=*&returnGeometry=true&f=geojson"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(query_url, headers={"User-Agent": NWS_HEADERS["User-Agent"]})
            resp.raise_for_status()
            geojson = resp.json()
    except Exception as exc:
        if cached is not None:
            return {
                **cached,
                "items": cached.get("items", [])[:limit],
                "count": min(limit, cached.get("count", 0)),
            }
        raise HTTPException(status_code=502, detail=f"SPC MCD API error: {exc}")

    def _as_int(value):
        try:
            return int(str(value).strip())
        except Exception:
            return None

    items = []
    features = []
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        centroid = _geometry_centroid(feature.get("geometry"))
        num = (
            _as_int(props.get("mdNumber"))
            or _as_int(props.get("mdnum"))
            or _as_int(props.get("number"))
            or _as_int(props.get("objectid"))
        )
        discussion = props.get("discussion") or props.get("name") or props.get("title") or "Mesoscale Discussion"
        summary = props.get("summary") or props.get("desc") or props.get("description") or ""
        issued = props.get("issue") or props.get("issuance") or props.get("issued")
        expires = props.get("expire") or props.get("expires")
        probs = []
        for key, label in (
            ("ww_prob", "Watch Prob"),
            ("torn_prob", "Tornado"),
            ("hail_prob", "Hail"),
            ("wind_prob", "Wind"),
        ):
            if props.get(key) not in (None, ""):
                probs.append(f"{label} {props.get(key)}")
        items.append({
            "number": num,
            "title": discussion,
            "summary": summary[:240],
            "issued": issued,
            "expires": expires,
            "probabilities": probs,
            "centroid": centroid,
        })
        features.append({
            "type": "Feature",
            "geometry": feature.get("geometry"),
            "properties": {
                "number": num,
                "title": discussion,
                "summary": summary[:240],
                "issued": issued,
                "expires": expires,
                "probabilities": probs,
                "centroid": centroid,
                "style": {
                    "color": "#ffb347",
                    "fillColor": "#ff9f1c",
                    "fillOpacity": 0.08,
                    "weight": 2.0,
                    "dashArray": "8 5",
                },
            },
        })

    items.sort(key=lambda item: (
        item["number"] is None,
        -(item["number"] or 0),
        item["title"],
    ))
    result = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "count": len(items),
        "items": items,
        "features": features,
    }
    _mcd_cache["data"] = result
    _mcd_cache["ts"] = now
    return {
        **result,
        "items": items[:limit],
        "count": min(limit, len(items)),
    }


async def _resolve_zone_geometry(client: httpx.AsyncClient, zone_url: str) -> dict | None:
    """Fetch a zone's geometry from the NWS API, with caching."""
    if zone_url in _zone_geom_cache:
        return _zone_geom_cache[zone_url]
    try:
        resp = await client.get(zone_url, headers=NWS_HEADERS)
        if resp.status_code == 200:
            data = resp.json()
            geom = data.get("geometry")
            _zone_geom_cache[zone_url] = geom
            _prune_cache(_zone_geom_cache, _ZONE_GEOM_CACHE_MAX)
            return geom
    except Exception:
        pass
    _zone_geom_cache[zone_url] = None
    _prune_cache(_zone_geom_cache, _ZONE_GEOM_CACHE_MAX)
    return None


@app.get("/api/alerts")
async def get_alerts(types: str = Query(default="", description="Comma-separated event types to include (empty=all)")):
    """Fetch active NWS alerts, cached for 60s. Resolves zone geometries.

    Optional `types` param filters by display_event, e.g. types=Tornado+Warning,Severe+Thunderstorm+Watch
    """
    data = await _fetch_alerts_data()
    if not types:
        return JSONResponse(data)
    allowed = {t.strip() for t in types.split(",") if t.strip()}
    if not allowed:
        return JSONResponse(data)
    filtered_features = [
        f for f in data.get("features", [])
        if f.get("properties", {}).get("display_event", "") in allowed
    ]
    return JSONResponse({**data, "features": filtered_features})


@app.get("/api/alerts/severe")
async def get_severe_alerts(limit: int = Query(default=20, ge=1, le=100)):
    data = await _fetch_alerts_data()
    items = []
    now = datetime.now(timezone.utc)
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        event = props.get("display_event") or props.get("event", "")
        if event not in _SEVERE_EVENTS:
            continue
        expires = _parse_iso_dt(props.get("expires"))
        age_minutes = None
        if expires is not None:
            age_minutes = max(0, int((expires - now).total_seconds() // 60))
        centroid = _geometry_centroid(feature.get("geometry"))
        items.append({
            "event": event,
            "headline": props.get("headline") or props.get("area_desc") or event,
            "area_desc": props.get("area_desc", ""),
            "severity": props.get("severity", ""),
            "sent": props.get("sent"),
            "expires": props.get("expires"),
            "minutes_left": age_minutes,
            "color": props.get("style", {}).get("color", "#888888"),
            "centroid": centroid,
        })

    items.sort(key=lambda item: (
        0 if item["event"] == "Tornado Warning" else 1,
        item["minutes_left"] if item["minutes_left"] is not None else 999999,
        item["headline"],
    ))
    return JSONResponse({
        "updated": data.get("updated"),
        "count": len(items),
        "items": items[:limit],
    })


@app.get("/api/spc/{product_id}")
async def get_spc_product(product_id: str):
    return JSONResponse(await _fetch_spc_geojson(product_id))


@app.get("/api/spc/mcds")
async def get_spc_mcds(limit: int = Query(default=12, ge=1, le=50)):
    return JSONResponse(await _fetch_mcd_data(limit=limit))


@app.get("/api/spotters")
async def get_spotters(max_age_min: int = Query(default=90, ge=1, le=1440), moving_only: bool = False):
    now = time.time()
    if _spotter_cache["data"] is not None and (now - _spotter_cache["ts"]) < _SPOTTER_TTL:
        result = _spotter_cache["data"]
    else:
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(SPOTTER_NETWORK_URL, headers={"User-Agent": NWS_HEADERS["User-Agent"]})
                resp.raise_for_status()
                result = _parse_spotter_feed(resp.text)
        except Exception as exc:
            if _spotter_cache["data"] is not None:
                result = _spotter_cache["data"]
            else:
                raise HTTPException(status_code=502, detail=f"Spotter Network feed error: {exc}")

        _spotter_cache["data"] = result
        _spotter_cache["ts"] = now

    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_min * 60)
    filtered_features = []
    for feature in result.get("features", []):
        props = feature.get("properties", {})
        ts = _parse_iso_dt(props.get("timestamp"))
        if ts is None or ts.timestamp() < cutoff:
            continue
        status = (props.get("status") or "").upper()
        if moving_only and ("HEADING:" not in status and "MOVING" not in status):
            continue
        filtered_features.append(feature)

    filtered = {
        **result,
        "features": filtered_features,
        "count": len(filtered_features),
        "filters": {"max_age_min": max_age_min, "moving_only": moving_only},
    }
    return JSONResponse(filtered)


@app.get("/api/spotters/ranked")
async def get_spotters_ranked(limit: int = Query(default=500, ge=1, le=2000)):
    """Return spotters ranked by activity score for the chasers panel."""
    now_ts = time.time()
    if _spotter_cache["data"] is not None and (now_ts - _spotter_cache["ts"]) < _SPOTTER_TTL:
        result = _spotter_cache["data"]
    else:
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(SPOTTER_NETWORK_URL, headers={"User-Agent": NWS_HEADERS["User-Agent"]})
                resp.raise_for_status()
                result = _parse_spotter_feed(resp.text)
        except Exception as exc:
            if _spotter_cache["data"] is not None:
                result = _spotter_cache["data"]
            else:
                raise HTTPException(status_code=502, detail=f"Spotter Network feed error: {exc}")
        _spotter_cache["data"] = result
        _spotter_cache["ts"] = now_ts

    now = datetime.now(timezone.utc)
    items = []
    for feature in result.get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [None, None])
        status = (props.get("status") or "").upper()
        ts = _parse_iso_dt(props.get("timestamp"))

        score = 0
        if "MOVING" in status:
            score += 2
        if "HEADING:" in status:
            score += 1
        if ts:
            age_min = (now - ts).total_seconds() / 60
            if age_min < 30:
                score += 2
            elif age_min < 60:
                score += 1

        items.append({
            "name": props.get("name", "Spotter"),
            "status": props.get("status", ""),
            "timestamp": props.get("timestamp"),
            "lat": coords[1],
            "lon": coords[0],
            "activity_score": score,
        })

    items.sort(key=lambda x: (-x["activity_score"], x["name"]))
    return JSONResponse({
        "updated": result.get("updated"),
        "count": len(items[:limit]),
        "items": items[:limit],
    })


# ---------------------------------------------------------------------------
# Surface observations API
# ---------------------------------------------------------------------------

_SURFACE_OBS_FILE = DATA_DIR / "surface_obs.json"
_SURFACE_HISTORY_DIR = DATA_DIR / "surface_history"


@app.get("/api/surface/current")
async def get_surface_obs():
    """Return current IEM Mesonet surface observations (file-backed, TTL cached)."""
    now = time.time()
    if _surface_cache["data"] is not None and (now - _surface_cache["ts"]) < _SURFACE_TTL:
        return JSONResponse(_surface_cache["data"])

    try:
        raw = _SURFACE_OBS_FILE.read_text()
        data = json.loads(raw)
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Surface observations not yet available")
    except Exception as exc:
        if _surface_cache["data"] is not None:
            return JSONResponse(_surface_cache["data"])
        raise HTTPException(status_code=503, detail=f"Surface obs read error: {exc}")

    _surface_cache["data"] = data
    _surface_cache["ts"] = now
    return JSONResponse(data)


@app.get("/api/surface/history")
async def get_surface_obs_history(hours: int = 3):
    """Return recent surface observation frame metadata for scrubber playback."""
    hours = max(1, min(hours, 6))
    cutoff = int(time.time()) - (hours * 3600)
    frames: list[dict] = []

    if not _SURFACE_HISTORY_DIR.exists():
        return JSONResponse({"frames": frames})

    for path in sorted(_SURFACE_HISTORY_DIR.glob("*.json")):
        try:
            ts = int(path.stem)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        frames.append({"generated": ts})

    return JSONResponse({"frames": frames})


@app.get("/api/surface/frame/{generated}")
async def get_surface_obs_frame(generated: int):
    """Return one historical surface observation frame."""
    path = _SURFACE_HISTORY_DIR / f"{generated}.json"
    try:
        raw = path.read_text()
        data = json.loads(raw)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Surface observation frame not found")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Surface observation frame read error: {exc}")
    return JSONResponse(data)


@app.get("/api/surface/status")
async def get_surface_status():
    """Return summary statistics for the current surface observation dataset."""
    now = time.time()
    data = _surface_cache.get("data")
    if data is None:
        try:
            raw = _SURFACE_OBS_FILE.read_text()
            data = json.loads(raw)
        except FileNotFoundError:
            raise HTTPException(status_code=503, detail="Surface observations not yet available")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Surface obs read error: {exc}")

    stations = data.get("stations", [])
    generated = data.get("generated", 0)

    ages: list[float] = []
    for stn in stations:
        valid = stn.get("valid")
        if not valid:
            continue
        try:
            valid_clean = valid.replace("Z", "+00:00")
            dt = datetime.fromisoformat(valid_clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_min = (now - dt.timestamp()) / 60.0
            if 0 <= age_min < 360:  # ignore obviously stale/bad timestamps
                ages.append(age_min)
        except Exception:
            pass

    reporting = len(ages)
    total = len(stations)
    avg_age_min = round(sum(ages) / len(ages), 1) if ages else None
    generated_age_min = round((now - generated) / 60.0, 1) if generated else None

    return JSONResponse({
        "total": total,
        "reporting": reporting,
        "generated": generated,
        "generated_age_min": generated_age_min,
        "avg_age_min": avg_age_min,
    })


# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------

LOG_DIR = DATA_DIR.parent / "logs"
_MODEL_NAMES = ["ecmwf", "gefs", "gfs", "gdps", "href", "hrrr", "nam12km", "nam3km", "rap", "rrfs", "rtma"]


def _du(path: Path) -> str:
    """Return human-readable size of a directory."""
    if not path.exists():
        return "0 MB"
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    if total < 1024**3:
        return f"{total / 1024**2:.0f} MB"
    return f"{total / 1024**3:.1f} GB"


def _scan_file_sizes(path: Path, skip_prefixes: tuple[Path, ...] = ()) -> int:
    """Sum file sizes under a directory, optionally excluding path prefixes."""
    if not path.exists():
        return 0

    total = 0
    for file_path in path.rglob("*"):
        if not file_path.is_file():
            continue
        if skip_prefixes and any(str(file_path).startswith(str(prefix)) for prefix in skip_prefixes):
            continue
        try:
            total += file_path.stat().st_size
        except OSError:
            continue
    return total


def _fmt_bytes(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    return f"{n / 1024**2:.0f} MB"


def _get_disk_info() -> dict[str, str]:
    now = time.time()
    if _disk_usage_cache["data"] is not None and (now - _disk_usage_cache["ts"]) < _DISK_USAGE_TTL:
        return _disk_usage_cache["data"]

    model_rendered_size = _scan_file_sizes(
        RENDERED_DIR,
        skip_prefixes=(SATELLITE_RENDERED_DIR, RADAR_RENDERED_DIR),
    )
    sat_rendered_size = _scan_file_sizes(SATELLITE_RENDERED_DIR)
    radar_rendered_size = _scan_file_sizes(RADAR_RENDERED_DIR)
    grib_cache_size = _scan_file_sizes(GRIB_CACHE_DIR)
    sat_cache_size = _scan_file_sizes(SATELLITE_CACHE_DIR)
    radar_cache_size = _scan_file_sizes(RADAR_CACHE_DIR)
    logs_size = _scan_file_sizes(LOG_DIR)
    total_size = (
        model_rendered_size
        + sat_rendered_size
        + radar_rendered_size
        + grib_cache_size
        + sat_cache_size
        + radar_cache_size
        + logs_size
    )

    data = {
        "model_rendered": _fmt_bytes(model_rendered_size),
        "satellite_rendered": _fmt_bytes(sat_rendered_size),
        "radar_rendered": _fmt_bytes(radar_rendered_size),
        "grib_cache": _fmt_bytes(grib_cache_size),
        "satellite_cache": _fmt_bytes(sat_cache_size),
        "radar_cache": _fmt_bytes(radar_cache_size),
        "logs": _fmt_bytes(logs_size),
        "tracked_total": _fmt_bytes(total_size),
    }
    _disk_usage_cache["data"] = data
    _disk_usage_cache["ts"] = now
    return data


def _find_pipeline_processes() -> tuple[psutil.Process | None, list[psutil.Process]]:
    """Find the scheduler main process and its worker children."""
    main_proc = None
    workers = []
    for proc in psutil.process_iter(["pid", "cmdline", "ppid"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "unified_scheduler" in cmdline:
                if main_proc is None or proc.info["ppid"] == 1:
                    # First match or direct child of init = main process
                    pass
                workers.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Separate main from workers: main is the one whose ppid is 1 or systemd
    if workers:
        # Sort by PID — main scheduler is typically the lowest PID
        workers.sort(key=lambda p: p.pid)
        main_proc = workers.pop(0)

    return main_proc, workers


def _read_status_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_pipeline_state() -> dict | None:
    """Return the live unified scheduler state, if available."""
    return _read_status_file(PIPELINE_STATE_FILE)


def _queue_pool_label(worker_type: str | None) -> str | None:
    mapping = {
        "affinity_a": "A",
        "affinity_b": "B",
        "affinity_c": "C",
        "catchall": "X",
        "shared": "S",
    }
    return mapping.get(worker_type or "")


def _queue_task_label(task: str | None) -> str:
    if not task or task == "--":
        return "-"
    match = re.search(r"([A-Z0-9]+)\s+\d{10}\s+F(\d+)", task.upper())
    if match:
        return f"{match.group(1)} f{int(match.group(2)):02d}"
    compact = re.sub(r"\s+", " ", task).strip()
    return compact[:28] if len(compact) > 28 else compact


def _pipeline_queue_snapshot(process_rows: list[dict]) -> dict | None:
    worker_rows = [
        row for row in process_rows
        if row.get("service") == "pipeline"
        and row.get("role") == "worker"
        and row.get("worker_type") != "low_priority"
    ]
    if not worker_rows:
        return None

    grouped: dict[str, list[dict]] = {}
    for row in worker_rows:
        label = _queue_pool_label(row.get("worker_type"))
        if not label:
            continue
        grouped.setdefault(label, []).append(row)

    if not grouped:
        return None

    ordered_labels = [label for label in ("A", "B", "C", "X", "S") if label in grouped]
    layout: list[str] = []
    activity: list[str] = []
    active_count = 0
    for label in ordered_labels:
        rows = grouped[label]
        layout.extend([label] * len(rows))
        for row in rows:
            task_label = _queue_task_label(row.get("task"))
            if task_label != "-":
                active_count += 1
            activity.append(f"{label}:{task_label}")

    return {
        "status": "processing" if active_count else "idle",
        "queue_layout": layout,
        "queue_activity": activity,
        "queue_layout_text": " ".join(layout),
        "queue_activity_text": " | ".join(activity),
    }


def _proc_metrics(proc: psutil.Process, now: float) -> dict | None:
    try:
        cpu_pct = _cpu_sampler.process_cpu(proc.pid)
        return {
            "pid": proc.pid,
            "cpu_percent": round(cpu_pct, 1),
            "rss": proc.memory_info().rss,
            "uptime": now - proc.create_time(),
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _find_main_process(cmd_fragment: str) -> psutil.Process | None:
    matches: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
        try:
            cmd_parts = proc.info["cmdline"] or []
            cmdline = " ".join(cmd_parts)
            if cmd_fragment in cmdline:
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not matches:
        return None

    def _match_rank(proc: psutil.Process) -> tuple[int, int, int]:
        try:
            cmd_parts = proc.cmdline()
            cmdline = " ".join(cmd_parts)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cmd_parts = []
            cmdline = ""
        is_python_module = f"-m {cmd_fragment}" in cmdline
        exe_name = Path(cmd_parts[0]).name if cmd_parts else ""
        is_python_exec = exe_name.startswith("python")
        is_init_child = proc.ppid() == 1
        return (
            0 if is_python_exec and is_python_module else 1,
            0 if is_python_module else 1,
            0 if is_init_child else 1,
            proc.pid,
        )

    matches.sort(key=_match_rank)
    return matches[0]


def _child_processes(parent: psutil.Process) -> list[psutil.Process]:
    try:
        children = parent.children(recursive=False)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    result = []
    for proc in children:
        try:
            cmdline = " ".join(proc.cmdline())
            if "resource_tracker import main" in cmdline:
                continue
            result.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    result.sort(key=lambda p: p.pid)
    return result


def _descendant_processes(parent: psutil.Process) -> list[psutil.Process]:
    """Return all non-tracker descendants for subtree CPU/RSS accounting."""
    try:
        descendants = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    result = []
    for proc in descendants:
        try:
            cmdline = " ".join(proc.cmdline())
            if "resource_tracker import main" in cmdline:
                continue
            result.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    result.sort(key=lambda p: p.pid)
    return result


def _model_task_text(status: dict | None) -> tuple[str, str, str | None]:
    if not status:
        return ("Idle", "--", None)
    model = (status.get("model") or "").upper()
    run_id = status.get("run_id") or "unknown"
    fxx = status.get("fxx")
    stage = (status.get("stage") or "rendering").replace("_", " ").title()
    worker_type = status.get("worker_type", "shared")

    task = f"{model} {run_id}"
    if fxx is not None:
        task += f" F{int(fxx):02d}"
    detail_parts = []
    if status.get("render_params") is not None:
        detail_parts.append(f"{int(status['render_params'])} params")
    if status.get("fetched_fields") is not None:
        detail_parts.append(f"{int(status['fetched_fields'])} fields")
    if status.get("detail"):
        detail_parts.append(str(status["detail"]))
    if detail_parts:
        task += f" ({', '.join(detail_parts)})"

    if worker_type == "low_priority":
        stage = f"[LP] {stage}"

    return (stage, task, worker_type)


def _radar_task_text(status: dict | None) -> tuple[str, str]:
    if not status:
        return ("Idle", "--")
    site = status.get("site") or "----"
    timestamp = status.get("timestamp") or "unknown"
    source = (status.get("source") or "full").upper()
    return ("Rendering", f"{site} {timestamp} {source}")


def _satellite_task_text(status: dict | None) -> tuple[str, str]:
    if not status:
        return ("Idle", "Waiting for next satellite cycle")
    source_id = (status.get("source_id") or "goes").upper()
    product_type = (status.get("product_type") or "product").replace("_", "-")
    band = status.get("band")
    stage = (status.get("stage") or "running").capitalize()
    if band:
        return (stage, f"{source_id} {product_type} {band}")
    return (stage, f"{source_id} {product_type}")


def _satellite_worker_name(status: dict | None, idx: int) -> str:
    if not status:
        return f"Satellite Worker #{idx}"
    source_id = (status.get("source_id") or "goes").upper()
    product_type = (status.get("product_type") or "product").replace("_", "-")
    return f"Satellite Render: {source_id} {product_type}"


def _build_process_rows() -> list[dict]:
    """Build a service-aware process inventory for the dashboard."""
    now = time.time()
    cached = _PROCESS_ROWS_CACHE["data"]
    if cached is not None and (now - _PROCESS_ROWS_CACHE["ts"]) < _PROCESS_ROWS_TTL:
        return cached

    now = time.time()
    rows: list[dict] = []
    model_status_dir = DATA_DIR / "worker_status"
    radar_status_dir = DATA_DIR / "radar_worker_status"
    satellite_status_dir = DATA_DIR / "satellite_worker_status"
    satellite_status_file = DATA_DIR / "satellite_worker_status.json"

    pipeline_main = _find_main_process("pipeline.unified_scheduler")
    if pipeline_main:
        metrics = _proc_metrics(pipeline_main, now)
        if metrics:
            active_models = len(list(model_status_dir.glob("*.json"))) if model_status_dir.exists() else 0
            rows.append({
                **metrics,
                "name": "Model Scheduler",
                "service": "pipeline",
                "role": "scheduler",
                "status": "Scheduling" if metrics["cpu_percent"] > 1 else "Idle",
                "status_class": "active" if metrics["cpu_percent"] > 1 else "idle",
                "task": f"Dispatching {active_models} active model renders" if active_models else "Managing model queue",
            })
        for idx, proc in enumerate(_child_processes(pipeline_main), start=1):
            metrics = _proc_metrics(proc, now)
            if not metrics:
                continue
            status = _read_status_file(model_status_dir / f"{proc.pid}.json")
            if status:
                try:
                    child_cpu = 0.0
                    child_rss = 0
                    for child in _descendant_processes(proc):
                        child_metrics = _proc_metrics(child, now)
                        if not child_metrics:
                            continue
                        child_cpu += child_metrics["cpu_percent"]
                        child_rss += child_metrics["rss"]
                    metrics["cpu_percent"] = round(metrics["cpu_percent"] + child_cpu, 1)
                    metrics["rss"] += child_rss
                except Exception:
                    pass
            state, task, worker_type = _model_task_text(status)
            model_label = (status.get("model") or "").upper() if status else ""
            
            if worker_type == "low_priority":
                worker_name = f"Low Priority Render: {model_label}" if model_label else "Low Priority Worker"
            else:
                worker_name = f"Model Render: {model_label}" if model_label else f"Model Worker #{idx}"
                
            rows.append({
                **metrics,
                "name": worker_name,
                "service": "pipeline",
                "role": "worker",
                "status": state,
                "status_class": "low-priority" if worker_type == "low_priority" else ("active" if status else ("processing" if metrics["cpu_percent"] > 50 else "idle")),
                "task": task,
                "worker_type": worker_type or "shared",
                "queue_label": _queue_pool_label(worker_type),
                "burst_active": bool(status.get("burst_active")) if status else False,
                "burst_subworkers": int(status.get("burst_subworkers") or 1) if status else 1,
                "burst_reason": str(status.get("burst_reason") or "") if status else "",
            })

    lowpri_main = _find_main_process("pipeline.lowpri_worker")
    if lowpri_main:
        for idx, proc in enumerate(_child_processes(lowpri_main), start=1):
            metrics = _proc_metrics(proc, now)
            if not metrics:
                continue
            status = _read_status_file(model_status_dir / f"{proc.pid}.json")
            state, task, worker_type = _model_task_text(status)
            model_label = (status.get("model") or "").upper() if status else ""
            worker_name = f"LP Render: {model_label}" if model_label else f"Low Priority Worker #{idx}"
            rows.append({
                **metrics,
                "name": worker_name,
                "service": "pipeline",
                "role": "worker",
                "status": state,
                "status_class": "low-priority",
                "task": task,
                "worker_type": worker_type or "low_priority",
                "queue_label": _queue_pool_label(worker_type),
                "burst_active": bool(status.get("burst_active")) if status else False,
                "burst_subworkers": int(status.get("burst_subworkers") or 1) if status else 1,
                "burst_reason": str(status.get("burst_reason") or "") if status else "",
            })

    satellite_main = _find_main_process("pipeline.satellite.scheduler")
    if satellite_main:
        metrics = _proc_metrics(satellite_main, now)
        if metrics:
            sat_status = _read_status_file(satellite_status_file)
            sat_state, sat_task = _satellite_task_text(sat_status)
            sat_status_class = "active" if sat_status else ("processing" if metrics["cpu_percent"] > 10 else "idle")
            active_workers = len(list(satellite_status_dir.glob("*.json"))) if satellite_status_dir.exists() else 0
            rows.append({
                **metrics,
                "name": "Satellite Scheduler",
                "service": "satellite",
                "role": "scheduler",
                "status": sat_state,
                "status_class": sat_status_class,
                "task": f"Dispatching {active_workers} satellite renders" if active_workers else sat_task,
            })
        worker_statuses: list[dict] = []
        if satellite_status_dir.exists():
            for status_file in sorted(satellite_status_dir.glob("*.json")):
                status = _read_status_file(status_file)
                if status:
                    worker_statuses.append(status)
        active_count = len(worker_statuses)
        per_worker_cpu = round(metrics["cpu_percent"] / active_count, 1) if metrics and active_count else 0.0
        for idx, status in enumerate(worker_statuses, start=1):
            state, task = _satellite_task_text(status)
            rows.append({
                "pid": status.get("thread_ident") or satellite_main.pid,
                "cpu_percent": per_worker_cpu,
                "rss": metrics["rss"] if metrics else 0,
                "uptime": metrics["uptime"] if metrics else 0,
                "name": _satellite_worker_name(status, idx),
                "service": "satellite",
                "role": "worker",
                "status": state,
                "status_class": "active",
                "task": task,
            })

    radar_main = _find_main_process("pipeline.radar.scheduler_")
    if radar_main:
        metrics = _proc_metrics(radar_main, now)
        if metrics:
            active_renders = len(list(radar_status_dir.glob("*.json"))) if radar_status_dir.exists() else 0
            rows.append({
                **metrics,
                "name": "Radar Scheduler",
                "service": "radar",
                "role": "scheduler",
                "status": "Polling" if metrics["cpu_percent"] > 1 else "Idle",
                "status_class": "active" if metrics["cpu_percent"] > 1 else "idle",
                "task": f"Dispatching {active_renders} radar renders" if active_renders else "Polling radar sites",
            })
        for idx, proc in enumerate(_child_processes(radar_main), start=1):
            metrics = _proc_metrics(proc, now)
            if not metrics:
                continue
            status = _read_status_file(radar_status_dir / f"{proc.pid}.json")
            state, task = _radar_task_text(status)
            site_label = (status.get("site") or "").upper() if status else ""
            worker_name = f"Radar Render: {site_label}" if site_label else f"Radar Worker #{idx}"
            rows.append({
                **metrics,
                "name": worker_name,
                "service": "radar",
                "role": "worker",
                "status": state,
                "status_class": "active" if status else ("processing" if metrics["cpu_percent"] > 50 else "idle"),
                "task": task,
            })

    web_main = _find_main_process("uvicorn web.app:app")
    if web_main:
        metrics = _proc_metrics(web_main, now)
        if metrics:
            rows.append({
                **metrics,
                "name": "Web API",
                "service": "web",
                "role": "server",
                "status": "Serving",
                "status_class": "active" if metrics["cpu_percent"] > 0.2 else "idle",
                "task": "Serving /wx/monitor and API routes",
            })

    _PROCESS_ROWS_CACHE["data"] = rows
    _PROCESS_ROWS_CACHE["ts"] = now
    return rows


def _get_model_status() -> list[dict]:
    """Build per-model status from run_meta.json files on disk."""
    now = time.time()
    cached = _MODEL_STATUS_CACHE["data"]
    if cached is not None and (now - _MODEL_STATUS_CACHE["ts"]) < _MODEL_STATUS_TTL:
        return cached

    result = []
    for name in _MODEL_NAMES:
        model_dir = RENDERED_DIR / name
        info = {"name": name, "status": "idle", "current_run": None,
                "last_run": None, "completed_fxx": 0, "total_fxx": 0}

        if not model_dir.exists():
            result.append(info)
            continue

        # Find most recent run directory
        run_dirs = sorted(
            [d for d in model_dir.iterdir() if d.is_dir()], reverse=True
        )
        for rd in run_dirs:
            meta_path = rd / "run_meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                forecast_hours = meta.get("forecast_hours", [])
                completed_fxx = _reported_completed_fxx(rd, meta)
                total = len(forecast_hours)
                completed = len(completed_fxx)
                is_complete = meta.get("is_complete", False) or bool(
                    forecast_hours and set(completed_fxx) >= set(forecast_hours)
                )

                if info["current_run"] is None:
                    info["current_run"] = rd.name

                if is_complete or completed > 0:
                    # First run with actual data — use for display
                    info["last_run"] = rd.name
                    info["completed_fxx"] = completed
                    info["total_fxx"] = total
                    info["max_fxx"] = max(forecast_hours) if forecast_hours else 0
                    info["max_completed_fxx"] = max(completed_fxx) if completed_fxx else 0
                    if is_complete:
                        info["status"] = "complete"
                    elif completed > 0:
                        info["status"] = "processing"
                    break
            except Exception:
                continue

        result.append(info)

    pipeline_state = _read_pipeline_state()
    if pipeline_state and pipeline_state.get("status") == "processing":
        active_model = pipeline_state.get("model")
        active_run = pipeline_state.get("current_run")
        completed_fxx = pipeline_state.get("completed_fxx", [])
        total_fxx = int(pipeline_state.get("total_fxx", 0) or 0)
        for info in result:
            if info["name"] != active_model:
                continue
            info["status"] = "processing"
            if active_run:
                info["current_run"] = active_run
                info["last_run"] = active_run
            info["completed_fxx"] = len(completed_fxx)
            if total_fxx:
                info["total_fxx"] = total_fxx
                info["max_fxx"] = max(total_fxx - 1, 0)
            info["max_completed_fxx"] = max(completed_fxx) if completed_fxx else 0
            break

    _MODEL_STATUS_CACHE["data"] = result
    _MODEL_STATUS_CACHE["ts"] = now
    return result


def _dashboard_log_entries() -> tuple[list[dict], list[dict]]:
    now = time.time()
    if (
        _DASHBOARD_LOG_CACHE["pipeline"] is not None
        and _DASHBOARD_LOG_CACHE["radar"] is not None
        and (now - _DASHBOARD_LOG_CACHE["ts"]) < _DASHBOARD_LOG_TTL
    ):
        return _DASHBOARD_LOG_CACHE["pipeline"], _DASHBOARD_LOG_CACHE["radar"]

    log_entries: list[dict] = []
    log_file = LOG_DIR / "pipeline.log"
    if log_file.exists():
        try:
            with open(log_file) as f:
                lines = f.readlines()
                for line in lines[-500:]:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    original, plain = translate_log_line(line)
                    ts_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                    log_entries.append({
                        "original": original,
                        "plain": plain,
                        "timestamp": ts_match.group(1) if ts_match else None,
                    })
        except Exception:
            pass

    radar_log_entries: list[dict] = []
    try:
        for line in _recent_radar_log_lines(500):
            original, plain = translate_log_line(line)
            ts_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
            radar_log_entries.append({
                "original": original,
                "plain": plain,
                "timestamp": ts_match.group(1) if ts_match else None,
            })
    except Exception:
        pass

    _DASHBOARD_LOG_CACHE["pipeline"] = log_entries
    _DASHBOARD_LOG_CACHE["radar"] = radar_log_entries
    _DASHBOARD_LOG_CACHE["ts"] = now
    return log_entries, radar_log_entries


def _event_weight(event: str) -> float:
    return {
        "Tornado Warning": 8.0,
        "Tornado Emergency": 10.0,
        "Severe Thunderstorm Warning": 4.5,
        "Flash Flood Warning": 2.5,
        "Tornado Watch": 3.0,
        "Severe Thunderstorm Watch": 2.0,
    }.get(event, 1.0)


def _collect_watch_warning_items(alert_data: dict, limit: int = 24) -> list[dict]:
    items = []
    now = datetime.now(timezone.utc)
    for feature in alert_data.get("features", []):
        props = feature.get("properties", {})
        event = props.get("display_event") or props.get("event", "")
        if "Watch" not in event and "Warning" not in event and event != "Tornado Emergency":
            continue
        expires = _parse_iso_dt(props.get("expires"))
        minutes_left = None
        if expires is not None:
            minutes_left = max(0, int((expires - now).total_seconds() // 60))
        items.append({
            "event": event,
            "headline": props.get("headline") or props.get("area_desc") or event,
            "area_desc": props.get("area_desc", ""),
            "expires": props.get("expires"),
            "minutes_left": minutes_left,
            "color": props.get("style", {}).get("color", "#888888"),
            "centroid": _geometry_centroid(feature.get("geometry")),
        })
    items.sort(key=lambda item: (
        -_event_weight(item["event"]),
        item["minutes_left"] if item["minutes_left"] is not None else 999999,
        item["headline"],
    ))
    return items[:limit]


def _summarize_outlook(spc_geojson: dict) -> list[dict]:
    counts: dict[str, int] = {}
    for feature in spc_geojson.get("features", []):
        label = feature.get("properties", {}).get("label") or "Area"
        counts[label] = counts.get(label, 0) + 1
    return [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _build_focus_sites(alert_items: list[dict], watch_items: list[dict], spotter_data: dict, limit: int = 8) -> list[dict]:
    scored = []
    spotter_features = spotter_data.get("features", [])[:300]
    alert_points = [item for item in (alert_items + watch_items) if item.get("centroid")]
    for site_id, info in NEXRAD_SITES.items():
        lat = info.get("lat")
        lon = info.get("lon")
        if lat is None or lon is None:
            continue
        score = 0.0
        nearby = []
        for item in alert_points:
            centroid = item["centroid"]
            d2 = (lat - centroid["lat"]) ** 2 + (lon - centroid["lon"]) ** 2
            if d2 > 9.0:
                continue
            weight = _event_weight(item["event"]) / max(0.35, d2 + 0.25)
            score += weight
            nearby.append(item["event"])
        spotter_count = 0
        for feature in spotter_features:
            coords = (feature.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            d2 = (lat - coords[1]) ** 2 + (lon - coords[0]) ** 2
            if d2 <= 4.0:
                score += 0.45 / max(0.2, d2 + 0.1)
                spotter_count += 1
        if score <= 0:
            continue
        scored.append({
            "site": site_id,
            "name": info.get("name", site_id),
            "lat": lat,
            "lon": lon,
            "score": round(score, 2),
            "nearby_events": sorted(set(nearby))[:3],
            "spotters": spotter_count,
        })
    scored.sort(key=lambda item: (-item["score"], item["site"]))
    return scored[:limit]


def _build_warned_radar_sites(alert_items: list[dict], limit: int = 4) -> list[dict]:
    clusters: list[dict] = []
    cluster_radius2 = 1.9

    ordered_items = sorted(
        [item for item in alert_items if item.get("centroid")],
        key=lambda item: (
            -_event_weight(item.get("event", "")),
            item.get("minutes_left") if item.get("minutes_left") is not None else 999999,
        ),
    )

    for item in ordered_items:
        centroid = item["centroid"]
        matched = None
        for cluster in clusters:
            d2 = (cluster["lat"] - centroid["lat"]) ** 2 + (cluster["lon"] - centroid["lon"]) ** 2
            if d2 <= cluster_radius2:
                matched = cluster
                break
        if matched is None:
            clusters.append({
                "lat": centroid["lat"],
                "lon": centroid["lon"],
                "items": [item],
            })
        else:
            matched["items"].append(item)
            count = len(matched["items"])
            matched["lat"] = ((matched["lat"] * (count - 1)) + centroid["lat"]) / count
            matched["lon"] = ((matched["lon"] * (count - 1)) + centroid["lon"]) / count

    clusters.sort(key=lambda cluster: (
        -max(_event_weight(item.get("event", "")) for item in cluster["items"]),
        -len(cluster["items"]),
        min(item.get("minutes_left") if item.get("minutes_left") is not None else 999999 for item in cluster["items"]),
    ))

    warned_sites: list[dict] = []
    seen_sites: set[str] = set()

    for cluster in clusters:
        lat = cluster["lat"]
        lon = cluster["lon"]
        best_item = sorted(
            cluster["items"],
            key=lambda item: (
                -_event_weight(item.get("event", "")),
                item.get("minutes_left") if item.get("minutes_left") is not None else 999999,
            ),
        )[0]

        candidate_sites = []
        for site_id, info in NEXRAD_SITES.items():
            site_lat = info.get("lat")
            site_lon = info.get("lon")
            if site_lat is None or site_lon is None:
                continue
            d2 = (site_lat - lat) ** 2 + (site_lon - lon) ** 2
            if d2 <= 16.0:
                candidate_sites.append((d2, site_id, info))
        candidate_sites.sort(key=lambda item: item[0])

        chosen = None
        for _, site_id, info in candidate_sites:
            if site_id not in seen_sites:
                chosen = (site_id, info)
                break
        if chosen is None and candidate_sites:
            _, site_id, info = candidate_sites[0]
            chosen = (site_id, info)
        if chosen is None:
            continue

        site_id, info = chosen
        seen_sites.add(site_id)
        preview = _focus_site_preview(site_id)
        warned_sites.append({
            "site": site_id,
            "name": info.get("name", site_id),
            "lat": info.get("lat"),
            "lon": info.get("lon"),
            "event": best_item.get("event"),
            "headline": best_item.get("headline") or best_item.get("area_desc") or best_item.get("event") or "",
            "area_desc": best_item.get("area_desc") or "",
            "minutes_left": best_item.get("minutes_left"),
            "cluster_count": len(cluster["items"]),
            "preview": preview,
        })
        if len(warned_sites) >= limit:
            break

    return warned_sites


def _read_json_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _generated_home_still(kind: str) -> dict | None:
    meta = _read_json_file(HOME_RENDERED_DIR / f"{kind}_latest.json")
    if not meta:
        return None
    image_name = meta.get("image") or f"{kind}_latest.png"
    image_path = f"rendered/home/{image_name}"
    payload = {
        "title": meta.get("title") or f"{kind.title()} still",
        "subtitle": meta.get("subtitle") or meta.get("timestamp"),
        "image_path": image_path,
        "timestamp": meta.get("timestamp"),
        "kind": meta.get("kind") or kind,
    }
    for key in ("model", "run_id", "parameter", "bounds", "satellite", "product", "site", "tilt"):
        if key in meta:
            payload[key] = meta.get(key)
    return payload


def _generated_home_asset(name: str) -> dict | None:
    meta = _read_json_file(HOME_RENDERED_DIR / f"{name}.json")
    if not meta:
        return None
    image_name = meta.get("image") or f"{name}.png"
    payload = {
        "title": meta.get("title") or name.replace("_", " ").title(),
        "subtitle": meta.get("subtitle") or meta.get("timestamp"),
        "image_path": f"rendered/home/{image_name}",
        "timestamp": meta.get("timestamp"),
        "kind": meta.get("kind"),
    }
    for key in ("model", "run_id", "parameter", "bounds", "satellite", "product", "site", "tilt"):
        if key in meta:
            payload[key] = meta.get(key)
    return payload


def _latest_satellite_preview(source_id: str = "goes19", product_id: str = "ir") -> dict | None:
    meta_path = SATELLITE_RENDERED_DIR / "meta" / f"{source_id}_{product_id}_latest.json"
    meta = _read_json_file(meta_path)
    if not meta:
        return None
    png_path = str(meta.get("png_path") or f"satellite/png/{source_id}_{product_id}_latest.png").lstrip("/")
    return {
        "title": f"{source_id.upper()} {product_id.replace('_', ' ').title()}",
        "subtitle": meta.get("timestamp"),
        "image_path": f"rendered/{png_path}" if not png_path.startswith("rendered/") else png_path,
        "satellite": source_id,
        "product": product_id,
        "timestamp": meta.get("timestamp"),
        "bounds": meta.get("bounds"),
    }


def _focus_site_preview(site_id: str) -> dict | None:
    generated = _generated_home_still("radar")
    if generated and generated.get("site") == site_id:
        return {
            "image_path": generated.get("image_path"),
            "timestamp": generated.get("timestamp"),
            "product": generated.get("product") or "REF",
            "tilt": generated.get("tilt", 0.5),
        }
    for idx in range(1, 4):
        generated = _generated_home_asset(f"radar_quicklook_{idx}")
        if generated and generated.get("site") == site_id:
            return {
                "image_path": generated.get("image_path"),
                "timestamp": generated.get("timestamp"),
                "product": generated.get("product") or "REF",
                "tilt": generated.get("tilt", 0.5),
            }
    meta_path = RADAR_RENDERED_DIR / "meta" / f"{site_id}_REF_latest.json"
    meta = _read_json_file(meta_path)
    if not meta:
        return None
    return {
        "image_path": f"rendered/radar/png/{site_id}_REF_latest.png",
        "timestamp": meta.get("timestamp"),
        "product": meta.get("short") or "REF",
        "tilt": meta.get("tilt", 0.5),
    }


def _latest_model_quicklooks(limit: int = 3) -> list[dict]:
    recent_path = DATA_DIR / "recent_completions.json"
    recent = _read_json_file(recent_path) or {}
    items = recent.get("recent", [])
    preferred_models = {"hrrr", "nam3km", "rap", "ecmwf", "gfs", "gdps"}
    preferred_params = (
        "reflectivity",
        "cref",
        "vil",
        "etop",
        "mucape",
        "srh_0_3km",
        "uh_2_5km",
        "helicity",
        "precip",
    )
    seen: set[str] = set()
    quicklooks: list[dict] = []

    for item in reversed(items):
        run_ref = item.get("run_id") or ""
        if "/" not in run_ref:
            continue
        model, run_id = run_ref.split("/", 1)
        if model not in preferred_models:
            continue
        key = f"{model}/{run_id}"
        if key in seen:
            continue
        seen.add(key)
        run_dir = RENDERED_DIR / model / run_id
        if not run_dir.exists():
            continue

        chosen_param = None
        chosen_file = None
        for param in preferred_params:
            param_dir = run_dir / param
            if not param_dir.is_dir():
                continue
            files = sorted(param_dir.glob("f*.png"))
            if not files:
                continue
            chosen_param = param
            chosen_file = files[min(len(files) - 1, 1)]
            break

        if not chosen_file or not chosen_param:
            continue

        bounds = _read_json_file(run_dir / "bounds.json")

        quicklooks.append({
            "model": model,
            "run_id": run_id,
            "parameter": chosen_param,
            "image_path": f"rendered/{chosen_file.relative_to(RENDERED_DIR)}",
            "label": f"{model.upper()} {chosen_param.replace('_', ' ')}",
            "bounds": bounds,
        })
        generated = _generated_home_asset(f"model_quicklook_{len(quicklooks)}")
        if (
            generated
            and generated.get("model") == model
            and generated.get("run_id") == run_id
            and generated.get("parameter") == chosen_param
        ):
            quicklooks[-1]["image_path"] = generated.get("image_path", quicklooks[-1]["image_path"])
            quicklooks[-1]["timestamp"] = generated.get("timestamp")
        if len(quicklooks) >= limit:
            break

    return quicklooks


def _featured_home_map() -> dict | None:
    generated = _generated_home_still("model")
    if generated:
        return generated

    quicklooks = _latest_model_quicklooks(limit=1)
    if quicklooks:
        item = quicklooks[0]
        return {
            "title": item.get("label", "Model quicklook"),
            "subtitle": f"Run {item.get('run_id', '--')} · {item.get('parameter', '').replace('_', ' ')}",
            "image_path": item.get("image_path"),
            "kind": "model",
            "model": item.get("model"),
            "run_id": item.get("run_id"),
            "parameter": item.get("parameter"),
            "timestamp": item.get("run_id"),
            "bounds": item.get("bounds"),
        }

    fallback = _latest_satellite_preview("goes19", "ir")
    if fallback:
        return {
            **fallback,
            "kind": "satellite",
        }
    return None


def _home_quicklook_cards(warned_sites: list[dict], focus_sites: list[dict], model_quicklooks: list[dict]) -> list[dict]:
    cards: list[dict] = []
    radar_cards = warned_sites or focus_sites

    def add_card(card: dict | None) -> None:
        if not card:
            return
        key = json.dumps({
            "kind": card.get("kind"),
            "site": card.get("site"),
            "model": card.get("model"),
            "run_id": card.get("run_id"),
            "parameter": card.get("parameter"),
            "satellite": card.get("satellite"),
            "product": card.get("product"),
        }, sort_keys=True)
        if any(existing.get("_dedupe") == key for existing in cards):
            return
        entry = dict(card)
        entry["_dedupe"] = key
        cards.append(entry)

    if radar_cards:
        top = radar_cards[0]
        add_card({
            "kind": "radar",
            "title": top.get("name") or top.get("event") or top.get("site"),
            "subtitle": top.get("event") or "Radar focus site",
            "image_path": (top.get("preview") or {}).get("image_path"),
            "site": top.get("site"),
            "lat": top.get("lat"),
            "lon": top.get("lon"),
            "event": top.get("event"),
            "minutes_left": top.get("minutes_left"),
        })

    satellite = _generated_home_still("satellite") or _latest_satellite_preview("goes19", "ir")
    if satellite:
        add_card({
            **satellite,
            "kind": "satellite",
        })

    if model_quicklooks:
        first_model = model_quicklooks[0]
        add_card({
            "kind": "model",
            "title": first_model.get("label", "Model quicklook"),
            "subtitle": f"Run {first_model.get('run_id', '--')} · {first_model.get('parameter', '').replace('_', ' ')}",
            "image_path": first_model.get("image_path"),
            "model": first_model.get("model"),
            "run_id": first_model.get("run_id"),
            "parameter": first_model.get("parameter"),
            "bounds": first_model.get("bounds"),
            "timestamp": first_model.get("timestamp") or first_model.get("run_id"),
        })

    extra_candidates: list[dict] = []
    if len(radar_cards) > 1:
        extra = radar_cards[1]
        extra_candidates.append({
            "kind": "radar",
            "title": extra.get("name") or extra.get("event") or extra.get("site"),
            "subtitle": extra.get("event") or "Additional radar focus",
            "image_path": (extra.get("preview") or {}).get("image_path"),
            "site": extra.get("site"),
            "lat": extra.get("lat"),
            "lon": extra.get("lon"),
            "event": extra.get("event"),
            "minutes_left": extra.get("minutes_left"),
        })
    if len(model_quicklooks) > 1:
        extra_model = model_quicklooks[1]
        extra_candidates.append({
            "kind": "model",
            "title": extra_model.get("label", "Model quicklook"),
            "subtitle": f"Run {extra_model.get('run_id', '--')} · {extra_model.get('parameter', '').replace('_', ' ')}",
            "image_path": extra_model.get("image_path"),
            "model": extra_model.get("model"),
            "run_id": extra_model.get("run_id"),
            "parameter": extra_model.get("parameter"),
            "bounds": extra_model.get("bounds"),
            "timestamp": extra_model.get("timestamp") or extra_model.get("run_id"),
        })
    for candidate in extra_candidates:
        add_card(candidate)
        if len(cards) >= 4:
            break

    for card in cards:
        card.pop("_dedupe", None)
    return cards[:4]


@app.get("/api/dashboard")
async def get_dashboard():
    now = time.time()
    if _dashboard_cache["data"] is not None and (now - _dashboard_cache["ts"]) < _DASHBOARD_TTL:
        return JSONResponse(_dashboard_cache["data"])

    # System resources — CPU% comes from background sampler (fresh 3s δ)
    cpu_pct = _cpu_sampler.system_cpu()
    cpu_freq = psutil.cpu_freq()
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    system = {
        "cpu_percent": cpu_pct,
        "cpu_count": psutil.cpu_count(),
        "cpu_freq": round(cpu_freq.current) if cpu_freq else 0,
        "mem_percent": mem.percent,
        "mem_used": mem.used,
        "mem_total": mem.total,
        "disk_percent": disk.percent,
        "disk_used": disk.used,
        "disk_total": disk.total,
    }

    processes = _build_process_rows()

    # Model status
    models = _get_model_status()

    disk_info = _get_disk_info()
    pipeline_state = _read_pipeline_state() or {}
    queue_snapshot = _pipeline_queue_snapshot(processes)
    if queue_snapshot:
        for key, value in queue_snapshot.items():
            if pipeline_state.get(key) in (None, "", [], {}):
                pipeline_state[key] = value
    if not pipeline_state:
        pipeline_state = None

    log_entries, radar_log_entries = _dashboard_log_entries()

    payload = {
        "system": system,
        "processes": processes,
        "workers": [],
        "scheduler": None,
        "pipeline_state": pipeline_state,
        "models": models,
        "disk": disk_info,
        "log_entries": log_entries,
        "radar_log_entries": radar_log_entries,
        "service_summary": _cpu_sampler.service_summaries(),
    }
    _dashboard_cache["data"] = payload
    _dashboard_cache["ts"] = now
    return JSONResponse(payload)


@app.get("/api/home/overview")
async def get_home_overview():
    now = time.time()
    if _home_cache["data"] is not None and (now - _home_cache["ts"]) < _HOME_TTL:
        return JSONResponse(_home_cache["data"])

    alerts_data = await _fetch_alerts_data()
    severe = await get_severe_alerts(limit=12)
    severe_data = json.loads(severe.body)
    watch_warning_items = _collect_watch_warning_items(alerts_data, limit=24)

    try:
        mcd_data = await _fetch_mcd_data(limit=10)
    except Exception:
        mcd_data = {"updated": None, "count": 0, "items": []}

    try:
        outlook_geojson = await _fetch_spc_geojson("day1_cat")
        outlook_summary = _summarize_outlook(outlook_geojson)
    except Exception:
        outlook_geojson = {"updated": None, "features": []}
        outlook_summary = []

    try:
        spotter_resp = await get_spotters(max_age_min=180, moving_only=False)
        spotter_data = json.loads(spotter_resp.body)
    except Exception:
        spotter_data = {"updated": None, "count": 0, "features": []}

    try:
        weather_wire = await _fetch_weather_wire(limit=8)
    except Exception:
        weather_wire = {
            "updated": None,
            "count": 0,
            "items": [],
            "sources": ["SPC", "NWS"],
            "x_feed": {
                "enabled": False,
                "status": "placeholder",
                "note": "Weather wire unavailable.",
            },
        }

    focus_sites = _build_focus_sites(severe_data.get("items", []), watch_warning_items, spotter_data)
    for site in focus_sites:
        preview = _focus_site_preview(site["site"])
        if preview:
            site["preview"] = preview
    warned_sites = _build_warned_radar_sites(severe_data.get("items", []), limit=3)
    watches = [item for item in watch_warning_items if "Watch" in item["event"]]
    warnings = [item for item in watch_warning_items if "Warning" in item["event"] or item["event"] == "Tornado Emergency"]
    spotter_features = spotter_data.get("features", [])
    recent_spotters = []
    for feature in spotter_features[:16]:
        props = feature.get("properties", {})
        coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]
        recent_spotters.append({
            "name": props.get("name", "Spotter"),
            "status": props.get("status", ""),
            "timestamp": props.get("timestamp"),
            "lat": coords[1],
            "lon": coords[0],
        })

    model_quicklooks = _latest_model_quicklooks(limit=3)
    quicklook_cards = _home_quicklook_cards(warned_sites, focus_sites, model_quicklooks)

    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "summary": {
            "severe_warnings": severe_data.get("count", 0),
            "watches_warnings": len(watch_warning_items),
            "mcds": mcd_data.get("count", 0),
            "spotters": spotter_data.get("count", 0),
        },
        "severe": severe_data,
        "watches_warnings": {
            "updated": alerts_data.get("updated"),
            "count": len(watch_warning_items),
            "items": watch_warning_items,
            "watch_count": len(watches),
            "warning_count": len(warnings),
        },
        "mcds": mcd_data,
        "outlook": {
            "updated": outlook_geojson.get("updated"),
            "summary": outlook_summary[:8],
            "feature_count": len(outlook_geojson.get("features", [])),
        },
        "spotters": {
            "updated": spotter_data.get("updated"),
            "count": spotter_data.get("count", 0),
            "items": recent_spotters,
        },
        "featured_map": _featured_home_map(),
        "model_quicklooks": model_quicklooks,
        "quicklook_cards": quicklook_cards,
        "focus_sites": focus_sites,
        "warned_sites": warned_sites,
        "weather_wire": weather_wire,
    }
    _home_cache["data"] = payload
    _home_cache["ts"] = now
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Dashboard page route
# ---------------------------------------------------------------------------

@app.get("/wx/monitor")
async def dashboard_page():
    from fastapi.responses import FileResponse
    dash = _STATIC_DIR / "dashboard.html"
    if dash.exists():
        return FileResponse(str(dash), headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
        })
    raise HTTPException(status_code=404, detail="Dashboard not found")


# ---------------------------------------------------------------------------
# Satellite Data API
# ---------------------------------------------------------------------------

from pipeline.config import SATELLITE_RENDERED_DIR, DATA_DIR


def _satellite_loop_hours(product: str) -> int:
    products_cfg = SATELLITE_CONFIG.get("products", {})
    product_cfg = products_cfg.get(product, {})
    default_hours = int(SATELLITE_CONFIG.get("render", {}).get("loop_hours", 3))
    return int(product_cfg.get("loop_hours", default_hours))

@app.get("/api/satellite/latest/{satellite}/{product}")
async def get_latest_satellite(
    satellite: str,
    product: str,
    region: str | None = Query(default=None),
):
    """Get the latest satellite image metadata.
    
    Args:
        satellite: 'goes19' or 'goes18'
        product: 'ir', 'wv', 'vis', etc.
    """
    try:
        meta_dir = SATELLITE_RENDERED_DIR / "meta"

        # Fast path: newest entry in the scheduler-maintained manifest.
        meta = None
        manifest_frames = _satellite_manifest_frames(
            satellite, product, region,
            cutoff_dt=datetime.now(timezone.utc) - timedelta(hours=_satellite_loop_hours(product)),
        )
        if manifest_frames:
            frame = manifest_frames[-1]
            meta = {"satellite": satellite, "product": product, **frame}
            if frame.get("region") is None:
                meta.pop("region", None)
        if meta is None:
            meta = _load_freshest_satellite_meta(meta_dir, satellite, product, region=region)
        if meta is None:
            # Try to get bounds file
            bounds_file = SATELLITE_RENDERED_DIR / f"{satellite}_{product}_bounds.json"
            if bounds_file.exists():
                bounds = json.loads(bounds_file.read_text())
                # Look for PNG files
                png_dir = SATELLITE_RENDERED_DIR / "png"
                pattern = f"{satellite}_{product}_*.png" if region is None else f"{satellite}_{product}_*_{region}.png"
                png_files = sorted(png_dir.glob(pattern), reverse=True)
                if png_files:
                    payload = {
                        "satellite": satellite,
                        "product": product,
                        "bounds": bounds,
                        "png_path": str(png_files[0].relative_to(DATA_DIR.parent)),
                        "timestamp": datetime.fromtimestamp(png_files[0].stat().st_mtime).isoformat(),
                        "filename": png_files[0].name,
                    }
                    if region:
                        payload["region"] = region
                    return JSONResponse(payload)
            raise HTTPException(status_code=404, detail="No satellite data available")

        return JSONResponse(meta)
        
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _satellite_manifest_frames(
    satellite: str,
    product: str,
    region: str | None,
    cutoff_dt: datetime,
) -> list[dict] | None:
    """Read frames from the scheduler-maintained per-product manifest.

    Returns None when the manifest doesn't exist (caller falls back to the
    legacy meta-directory glob).
    """
    name = f"{satellite}_{product}" if region is None else f"{satellite}_{product}_{region}"
    manifest_path = SATELLITE_RENDERED_DIR / "manifests" / f"{name}.json"
    try:
        payload = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        return None
    frames = []
    for frame in payload.get("frames", []):
        ts = _parse_satellite_meta_timestamp(frame.get("timestamp"))
        if ts is not None and ts < cutoff_dt:
            continue
        frames.append(frame)
    return frames


@app.get("/api/satellite/frames/{satellite}/{product}")
async def get_satellite_frames(
    satellite: str,
    product: str,
    region: str | None = Query(default=None),
):
    """Get all available satellite frames sorted oldest→newest."""
    meta_dir = SATELLITE_RENDERED_DIR / "meta"
    loop_hours = _satellite_loop_hours(product)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=loop_hours)

    manifest_frames = _satellite_manifest_frames(satellite, product, region, cutoff_dt)
    if manifest_frames is not None:
        return JSONResponse(manifest_frames)

    frames = []
    pattern = f"{satellite}_{product}_*.json" if region is None else f"{satellite}_{product}_*_{region}.json"
    for f in meta_dir.glob(pattern):
        if f.stem.endswith("_latest"):
            continue
        try:
            meta = json.loads(f.read_text())
            ts = _parse_satellite_meta_timestamp(meta.get("timestamp"))
            if ts is not None and ts < cutoff_dt:
                continue
            meta_product = str(meta.get("product") or "")
            meta_satellite = str(meta.get("satellite") or "").lower()
            if meta_satellite.startswith("g") and meta_satellite[1:].isdigit():
                meta_satellite = f"goes{meta_satellite[1:]}"
            elif meta_satellite.startswith("goes-") and meta_satellite[5:].isdigit():
                meta_satellite = f"goes{meta_satellite[5:]}"
            if meta_product != product or meta_satellite != satellite:
                continue
            if meta.get("region") != region:
                continue
            frames.append({
                "timestamp": meta.get("timestamp", ""),
                "png_path": meta.get("png_path", ""),
                "bounds": meta.get("bounds"),
                "region": meta.get("region"),
            })
        except Exception:
            pass
    frames.sort(key=lambda x: x["timestamp"])
    return JSONResponse(frames)


@app.get("/api/satellite/status")
async def get_satellite_status():
    """Get per-product archive status (frame count + freshness) for all satellite sources."""
    now = time.time()
    if _satellite_status_cache["data"] is not None and (now - _satellite_status_cache["ts"]) < _SATELLITE_STATUS_TTL:
        return JSONResponse(_satellite_status_cache["data"])

    meta_dir = SATELLITE_RENDERED_DIR / "meta"
    products_cfg = SATELLITE_CONFIG.get("products", {})
    sources      = list(SATELLITE_CONFIG.get("sources", {}).keys())
    now_ts = datetime.now(timezone.utc).timestamp()

    result: dict = {source_id: {} for source_id in sources}
    regional_result: dict[str, dict] = {}
    frame_counts: dict[tuple[str, str, str | None], int] = {}
    latest_meta: dict[tuple[str, str, str | None], dict] = {}
    latest_keys: dict[tuple[str, str, str | None], tuple[float, float, str]] = {}
    regions_cfg = SATELLITE_CONFIG.get("regions", {})


    for meta_file in meta_dir.glob("*.json"):
        stem = meta_file.stem
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
            continue

        source_raw = str(meta.get("satellite") or "")
        product_id = str(meta.get("product") or "")
        region_id = meta.get("region")
        region_id = str(region_id) if region_id else None
        if not product_id:
            continue

        source_id = source_raw.lower()
        if source_id.startswith("g") and source_id[1:].isdigit():
            source_id = f"goes{source_id[1:]}"
        if source_id not in result:
            continue

        count_key = (source_id, product_id, region_id)
        dt = _parse_satellite_meta_timestamp(meta.get("timestamp"))
        if not stem.endswith("_latest"):
            loop_hours = _satellite_loop_hours(product_id)
            cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=loop_hours)
            if dt is not None and dt >= cutoff_dt:
                frame_counts[count_key] = frame_counts.get(count_key, 0) + 1
        key = ((dt.timestamp() if dt is not None else meta_file.stat().st_mtime), meta_file.stat().st_mtime, meta_file.name)
        meta_key = (source_id, product_id, region_id)
        if meta_key not in latest_keys or key > latest_keys[meta_key]:
            latest_keys[meta_key] = key
            latest_meta[meta_key] = meta

    for source_id in sources:
        for product_id, prod_cfg in products_cfg.items():
            loop_hours = _satellite_loop_hours(product_id)
            meta = latest_meta.get((source_id, product_id, None), {})
            age_min: float | None = None
            last_update: str | None = None
            ts_str = meta.get("timestamp", "")
            if ts_str:
                try:
                    dt = _parse_satellite_meta_timestamp(ts_str)
                    if dt is not None:
                        age_min = (now_ts - dt.timestamp()) / 60
                        last_update = dt.strftime("%H:%MZ")
                except Exception:
                    pass

            update_freq = int(prod_cfg.get("update_freq_minutes", 10))
            if product_id == "sst":
                update_freq = max(update_freq, 60)
            live_threshold_min = max(30, update_freq * 2 + 15)
            stale_threshold_min = max(180, update_freq * 4 + 30)

            if age_min is None:
                status_str = "no_data"
            elif age_min < live_threshold_min:
                status_str = "live"
            elif age_min < stale_threshold_min:
                status_str = "stale"
            else:
                status_str = "old"

            # Expected frames for this product given the configured loop window.
            # update_freq_minutes drives how many frames exist in loop_hours.
            max_frames = max(1, (loop_hours * 60) // update_freq)

            frames = frame_counts.get((source_id, product_id, None), 0)
            result[source_id][product_id] = {
                "frames": frames,
                "max_frames": max_frames,
                "pct": min(100, round(frames / max_frames * 100)) if max_frames else 0,
                "age_min": round(age_min, 1) if age_min is not None else None,
                "status": status_str,
                "last_update": last_update,
                "name": prod_cfg.get("name", product_id),
            }

    for region_id, region_cfg in regions_cfg.items():
        if not region_cfg.get("enabled", True):
            continue
        region_source = str(region_cfg.get("source") or "goes19").lower()
        region_products = {}
        for product_id in region_cfg.get("products", []):
            loop_hours = _satellite_loop_hours(product_id)
            prod_cfg = products_cfg.get(product_id, {})
            meta = latest_meta.get((region_source, product_id, region_id), {})
            age_min: float | None = None
            last_update: str | None = None
            ts_str = meta.get("timestamp", "")
            if ts_str:
                try:
                    dt = _parse_satellite_meta_timestamp(ts_str)
                    if dt is not None:
                        age_min = (now_ts - dt.timestamp()) / 60
                        last_update = dt.strftime("%H:%MZ")
                except Exception:
                    pass

            update_freq = int(prod_cfg.get("update_freq_minutes", 10))
            live_threshold_min = max(30, update_freq * 2 + 15)
            stale_threshold_min = max(180, update_freq * 4 + 30)

            if age_min is None:
                status_str = "no_data"
            elif age_min < live_threshold_min:
                status_str = "live"
            elif age_min < stale_threshold_min:
                status_str = "stale"
            else:
                status_str = "old"

            max_frames = max(1, (loop_hours * 60) // max(update_freq, 1))
            frames = frame_counts.get((region_source, product_id, region_id), 0)
            region_products[product_id] = {
                "frames": frames,
                "max_frames": max_frames,
                "pct": min(100, round(frames / max_frames * 100)) if max_frames else 0,
                "age_min": round(age_min, 1) if age_min is not None else None,
                "status": status_str,
                "last_update": last_update,
                "name": prod_cfg.get("name", product_id),
                "source": region_source,
            }

        regional_result[region_id] = {
            "name": region_cfg.get("name", region_id),
            "source": region_source,
            "products": region_products,
        }

    result["regional"] = regional_result

    _satellite_status_cache["data"] = result
    _satellite_status_cache["ts"] = now
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Radar API — Level II
# ---------------------------------------------------------------------------

def _resolve_radar_short(product: str) -> tuple[str, str]:
    """Resolve a product short code (REF, VEL, etc.) to (field_name, short_code).

    Accepts either short codes or full field names.
    """
    product = product.upper()
    for field_name, cfg in RADAR_FIELD_CONFIG.items():
        if cfg["short"] == product:
            return field_name, cfg["short"]
    # Try as direct field name
    product_lower = product.lower()
    if product_lower in RADAR_FIELD_CONFIG:
        return product_lower, RADAR_FIELD_CONFIG[product_lower]["short"]
    return product.lower(), product


@app.get("/api/value/radar/{site}/{product}")
async def get_radar_value(
    site: str, product: str,
    lat: float = Query(...), lon: float = Query(...),
    tilt: float = Query(0.5),
):
    """Sample the data value at a lat/lon from the rendered radar PNG."""
    import math
    import numpy as np
    from PIL import Image

    field_name, short = _resolve_radar_short(product)
    if field_name not in RADAR_FIELD_CONFIG:
        raise HTTPException(status_code=404, detail=f"Unknown radar product: {product}")

    meta_file = RADAR_RENDERED_DIR / "meta" / f"{site}_{short}_latest.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail=f"No radar data for {site}/{short}")
    meta = json.loads(meta_file.read_text())

    bounds = meta.get("bounds", {})
    site_lat = bounds.get("center_lat")
    site_lon = bounds.get("center_lon")
    max_range_km = bounds.get("max_range_km")
    if not (site_lat and site_lon and max_range_km):
        raise HTTPException(status_code=500, detail="Incomplete radar metadata")

    # Haversine range check from site to click
    lat1r = math.radians(site_lat)
    lat2r = math.radians(lat)
    dlon = math.radians(lon - site_lon)
    dlat = math.radians(lat - site_lat)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    range_km = 2 * 6371.0 * math.asin(math.sqrt(a))
    if range_km > max_range_km:
        return JSONResponse({"value": None, "unit": "", "label": "Outside radar range"})

    png_path = RADAR_RENDERED_DIR / "png" / f"{site}_{short}_latest.png"
    if not png_path.exists():
        raise HTTPException(status_code=404, detail="Radar PNG not found")

    try:
        img = Image.open(png_path).convert("RGBA")
        width, height = img.size
        west = float(bounds["west"])
        east = float(bounds["east"])
        north = float(bounds["north"])
        south = float(bounds["south"])
        merc = lambda d: math.log(math.tan(math.pi / 4 + math.radians(d) / 2))
        merc_north = merc(north)
        merc_south = merc(south)
        merc_lat = merc(lat)
        col = int((lon - west) / max(east - west, 1e-9) * width)
        row = int((merc_north - merc_lat) / max(merc_north - merc_south, 1e-9) * height)
        col = max(0, min(width - 1, col))
        row = max(0, min(height - 1, row))
        pixel = np.array(img)[row, col]  # RGBA uint8
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read radar PNG: {e}")

    if pixel[3] == 0:
        return JSONResponse({"value": None, "unit": "", "label": "No data at this location"})

    # Reverse-map pixel RGB to value via nearest LUT entry
    field_cfg = RADAR_FIELD_CONFIG[field_name]
    lut, vmin, vmax = field_cfg["lut_fn"]()
    rgb_pixel = pixel[:3].astype(float)
    rgb_lut = lut[:, :3].astype(float)
    dists = np.sum((rgb_lut - rgb_pixel) ** 2, axis=1)
    dists[lut[:, 3] == 0] = 1e9  # ignore transparent LUT entries
    best_idx = int(np.argmin(dists))
    value = vmin + (best_idx / 255.0) * (vmax - vmin)

    unit = field_cfg.get("unit", "")
    label = field_cfg.get("label", short)
    return JSONResponse({
        "value": round(value, 1),
        "unit": unit,
        "label": label,
        "lat": round(lat, 4),
        "lon": round(lon, 4),
    })


@app.get("/api/radar/site_layers")
async def get_radar_site_layers(product: str = "REF", max_age_min: int = 15):
    """Return latest PNG metadata for all sites, for home-page zoom-in overlay."""
    from datetime import datetime, timezone
    _, short = _resolve_radar_short(product)
    meta_dir = RADAR_RENDERED_DIR / "meta"
    now = datetime.now(timezone.utc)
    results = []
    for meta_file in sorted(meta_dir.glob(f"*_{short}_latest.json")):
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
            continue
        bounds = meta.get("bounds")
        png_path = meta.get("png_path")
        timestamp = meta.get("timestamp")
        if not bounds or not png_path or not timestamp:
            continue
        try:
            ts = datetime.strptime(timestamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            if (now - ts).total_seconds() / 60.0 > max_age_min:
                continue
        except Exception:
            continue
        results.append({
            "site": meta.get("site"),
            "timestamp": timestamp,
            "bounds": bounds,
            "png_path": png_path,
        })
    return results


@app.get("/api/radar/latest/{site}/{product}")
async def get_latest_radar(site: str, product: str):
    """Get the latest radar image metadata for a site and product."""
    _, short = _resolve_radar_short(product)
    meta_file = RADAR_RENDERED_DIR / "meta" / f"{site}_{short}_latest.json"
    if not meta_file.exists():
        raise HTTPException(status_code=404, detail=f"No radar data for {site}/{short}")
    try:
        return JSONResponse(json.loads(meta_file.read_text()))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/radar/frames/{site}/{product}")
async def get_radar_frames(site: str, product: str):
    """Get all available radar frames for a site and product, oldest→newest."""
    _, short = _resolve_radar_short(product)
    frames = []
    meta_dir = RADAR_RENDERED_DIR / "meta"
    for f in meta_dir.glob(f"{site}_{short}_*.json"):
        if f.stem.endswith("_latest"):
            continue
        try:
            meta = json.loads(f.read_text())
            frames.append({
                "timestamp": meta.get("timestamp", ""),
                "png_path": meta.get("png_path", ""),
                "bounds": meta.get("bounds"),
                "tilt": meta.get("tilt", 0.5),
                "site": site,
                "product": short,
                "field": meta.get("field", ""),
            })
        except Exception:
            pass
    frames.sort(key=lambda x: x["timestamp"])
    return JSONResponse(frames)


@app.get("/api/radar/tilts/{site}")
async def get_radar_tilts(site: str):
    """Get available tilts for a site from the most recent raw volume."""
    import asyncio

    cache_dir = RADAR_CACHE_DIR / site
    if not cache_dir.exists():
        return JSONResponse({"site": site, "tilts": [0.5]})

    raw_files = sorted(cache_dir.glob("*.ar2v"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not raw_files:
        return JSONResponse({"site": site, "tilts": [0.5]})

    raw_path = raw_files[0]

    def _extract_tilts():
        from pipeline.radar.parser import parse_volume
        volume = parse_volume(raw_path)
        if volume is None:
            return [0.5]
        return sorted({round(float(tilt), 1) for tilt in volume.tilts}) or [0.5]

    loop = asyncio.get_event_loop()
    tilts = await loop.run_in_executor(None, _extract_tilts)
    return JSONResponse({"site": site, "tilts": tilts})


@app.get("/api/radar/grid/{site}/{product}")
async def get_radar_grid(
    site: str, product: str,
    tilt: float = Query(0.5),
):
    """Get raw radar sweep data as binary for client-side querying."""
    return get_radar_grid_binary(site, product, tilt)


@app.get("/api/radar/render/{site}/{product}/{tilt}")
async def render_radar_tilt(site: str, product: str, tilt: float):
    """On-demand render of a specific field/tilt from cached raw data."""
    import asyncio
    field_name, short = _resolve_radar_short(product)

    # Find most recent raw file for this site
    cache_dir = RADAR_CACHE_DIR / site
    if not cache_dir.exists():
        raise HTTPException(status_code=404, detail=f"No cached data for {site}")

    raw_files = sorted(cache_dir.glob("*.ar2v"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not raw_files:
        raise HTTPException(status_code=404, detail=f"No raw data for {site}")

    raw_path = raw_files[0]

    # Run CPU-heavy render in executor to avoid blocking event loop
    loop = asyncio.get_event_loop()

    def _do_render():
        from pipeline.radar.renderer import Level2Renderer
        renderer = Level2Renderer()
        return renderer.render_on_demand(raw_path, site, field_name, tilt)

    result = await loop.run_in_executor(None, _do_render)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Could not render {site}/{product} at {tilt}°")

    meta = json.loads(result.meta_path.read_text())
    return JSONResponse(meta)


@app.get("/api/radar/storm3d/{site}")
async def get_radar_storm_3d(
    site: str,
    lat: float = Query(...),
    lon: float = Query(...),
):
    """Build a storm-centered 3D voxel payload from the latest raw Level II volume."""
    import asyncio

    cache_dir = RADAR_CACHE_DIR / site
    if not cache_dir.exists():
        raise HTTPException(status_code=404, detail=f"No cached data for {site}")

    raw_files = sorted(
        list(cache_dir.glob("*.ar2v")) + list(cache_dir.glob("*.chunkbin")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not raw_files:
        raise HTTPException(status_code=404, detail=f"No raw data for {site}")

    raw_path = raw_files[0]
    loop = asyncio.get_event_loop()

    def _build():
        from pipeline.radar.storm3d import build_storm_volume

        return build_storm_volume(site=site, raw_path=raw_path, click_lat=lat, click_lon=lon)

    result = await loop.run_in_executor(None, _build)
    if result is None:
        raise HTTPException(status_code=404, detail="No storm object found near that click")

    return JSONResponse(result.payload)


@app.get("/api/radar/volume3d/{site}")
async def get_radar_volume_3d(
    site: str,
    lat: float | None = Query(None),
    lon: float | None = Query(None),
    box_lat_min: float | None = Query(None),
    box_lon_min: float | None = Query(None),
    box_lat_max: float | None = Query(None),
    box_lon_max: float | None = Query(None),
    mode: str = Query("standard"),
):
    """Build a dedicated 3D radar payload from the latest raw Level II volume.

    Without a click location, the strongest storm component in the latest scan
    is selected automatically.  Pass mode=enhanced for multi-channel RGBA volume.
    """
    import asyncio

    cache_dir = RADAR_CACHE_DIR / site
    if not cache_dir.exists():
        raise HTTPException(status_code=404, detail=f"No cached data for {site}")

    raw_files = sorted(
        list(cache_dir.glob("*.ar2v")) + list(cache_dir.glob("*.chunkbin")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not raw_files:
        raise HTTPException(status_code=404, detail=f"No raw data for {site}")

    raw_path = raw_files[0]
    stat = raw_path.stat()
    click_key = (
        None if lat is None else round(float(lat), 4),
        None if lon is None else round(float(lon), 4),
    )
    box_key = (
        None if box_lat_min is None else round(float(box_lat_min), 4),
        None if box_lon_min is None else round(float(box_lon_min), 4),
        None if box_lat_max is None else round(float(box_lat_max), 4),
        None if box_lon_max is None else round(float(box_lon_max), 4),
    )
    cache_key = f"{site}:{raw_path.name}:{int(stat.st_mtime)}:{click_key[0]}:{click_key[1]}:{box_key[0]}:{box_key[1]}:{box_key[2]}:{box_key[3]}:{mode}"
    cached = _RADAR_3D_CACHE.get(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    loop = asyncio.get_event_loop()

    def _build():
        if mode == "enhanced":
            from pipeline.radar.storm3d import build_volume_scene_enhanced
            return build_volume_scene_enhanced(
                site=site,
                raw_path=raw_path,
                click_lat=lat,
                click_lon=lon,
                box_lat_min=box_lat_min,
                box_lon_min=box_lon_min,
                box_lat_max=box_lat_max,
                box_lon_max=box_lon_max,
            )
        else:
            from pipeline.radar.storm3d import build_volume_scene
            return build_volume_scene(
                site=site,
                raw_path=raw_path,
                click_lat=lat,
                click_lon=lon,
            )

    result = await loop.run_in_executor(None, _build)
    if result is None:
        raise HTTPException(status_code=404, detail="No volumetric storm scene available for that scan")

    _RADAR_3D_CACHE.clear()
    _RADAR_3D_CACHE[cache_key] = result.payload
    return JSONResponse(result.payload)


@app.get("/wx/api/radar/volume3d/{site}")
async def get_radar_volume_3d_prefixed(
    site: str,
    lat: float | None = Query(None),
    lon: float | None = Query(None),
    mode: str = Query("standard"),
):
    return await get_radar_volume_3d(site=site, lat=lat, lon=lon, mode=mode)


@app.get("/api/radar/status")
async def get_radar_status():
    """Get status for all radar sites."""
    now = time.time()
    if _radar_status_cache["data"] is not None and (now - _radar_status_cache["ts"]) < _RADAR_STATUS_TTL:
        return JSONResponse(_radar_status_cache["data"])

    from datetime import datetime as _dt, timezone as _timezone

    now_ts = _dt.now(_timezone.utc).timestamp()
    result: dict = {
        site_id: {
            "name": site_info.get("name", site_id),
            "lat": site_info.get("lat"),
            "lon": site_info.get("lon"),
            "priority": site_info.get("priority", 3),
            "products": {},
        }
        for site_id, site_info in NEXRAD_SITES.items()
    }

    latest_meta: dict[tuple[str, str], dict] = {}
    meta_dir = RADAR_RENDERED_DIR / "meta"
    for meta_file in meta_dir.glob("*_latest.json"):
        parts = meta_file.stem.split("_")
        if len(parts) < 3:
            continue
        site_id, short = parts[0], parts[1]
        try:
            latest_meta[(site_id, short)] = json.loads(meta_file.read_text())
        except Exception:
            continue

    for field_name, cfg in RADAR_FIELD_CONFIG.items():
        short = cfg["short"]
        for site_id in NEXRAD_SITES:
            meta = latest_meta.get((site_id, short), {})
            age_min = None
            last_update = None
            ts_str = meta.get("timestamp", "")
            if len(ts_str) >= 15:
                try:
                    dt = _dt.strptime(ts_str[:15], "%Y%m%d_%H%M%S").replace(tzinfo=_timezone.utc)
                    age_min = (now_ts - dt.timestamp()) / 60
                    last_update = f"{ts_str[9:11]}:{ts_str[11:13]}Z"
                except Exception:
                    pass

            if age_min is None:
                status_str = "no_data"
            elif age_min < 15:
                status_str = "live"
            elif age_min < 60:
                status_str = "recent"
            else:
                status_str = "stale"

            result[site_id]["products"][short] = {
                "field": field_name,
                "label": cfg["label"],
                "status": status_str,
                "age_min": round(age_min, 1) if age_min is not None else None,
                "last_update": last_update,
            }

    _radar_status_cache["data"] = result
    _radar_status_cache["ts"] = now
    return JSONResponse(result)


@app.get("/api/radar/sites")
async def get_radar_sites():
    """Get list of all available radar sites and products."""
    return JSONResponse({
        "sites": {
            site_id: {
                "name": info.get("name", site_id),
                "lat": info.get("lat"),
                "lon": info.get("lon"),
                "priority": info.get("priority", 3),
            }
            for site_id, info in NEXRAD_SITES.items()
        },
        "products": {
            cfg["short"]: {
                "field": field_name,
                "label": cfg["label"],
                "unit": cfg["unit"],
            }
            for field_name, cfg in RADAR_FIELD_CONFIG.items()
        }
    })


# ---------------------------------------------------------------------------
# SPA fallback
# ---------------------------------------------------------------------------

@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    index = _STATIC_DIR / "index.html"
    if index.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(index))
    raise HTTPException(status_code=404, detail="Not found")
