"""Disk retention enforcement for rendered images and GRIB cache."""
import logging
import os
import re
import shutil
from pathlib import Path

import psutil

from .config import (
    DISK_PRESSURE_THRESHOLD_GB,
    GRIB_CACHE_DIR,
    PARTIAL_PARAMS,
    RENDERED_DIR,
    RETENTION,
)

logger = logging.getLogger(__name__)

_RUN_HOUR_RE = re.compile(r"\.t(\d{2})z\.")
_FULL_RUN_RE = re.compile(r"(\d{10})0000-")
_RRFS_RUN_RE = re.compile(r"rrfs_(\d{8})_(\d{2})_")
# GDPS/model-prefixed pattern: gdps_YYYYMMDD_HH_fXXX.grib2
_MODEL_DATE_HOUR_RE = re.compile(r"[a-z]+_(\d{8})_(\d{2})_f\d+")


def _extract_grib_run_id(path: Path) -> str | None:
    """Infer run id (YYYYMMDDHH) from a cached GRIB/IDX/TMP file path."""
    date_part = path.parent.name
    name = path.name

    match = _FULL_RUN_RE.search(name)
    if match:
        return match.group(1)

    match = _RRFS_RUN_RE.search(name)
    if match:
        return f"{match.group(1)}{match.group(2)}"

    match = _RUN_HOUR_RE.search(name)
    if match and len(date_part) == 8 and date_part.isdigit():
        return f"{date_part}{match.group(1)}"

    match = _MODEL_DATE_HOUR_RE.search(name)
    if match:
        return f"{match.group(1)}{match.group(2)}"

    # Fallback: use parent directory date if it looks like YYYYMMDD
    if len(date_part) == 8 and date_part.isdigit():
        return f"{date_part}00"

    return None


def _cleanup_grib_cache_files(model_grib: Path, keep_runs: int) -> tuple[int, int, int]:
    """Prune cached GRIB files by actual run id instead of only date directory."""
    files_by_run: dict[str, list[Path]] = {}
    orphan_files: list[Path] = []

    for path in model_grib.rglob("*"):
        if not path.is_file():
            continue
        run_id = _extract_grib_run_id(path)
        if run_id is None:
            orphan_files.append(path)
            continue
        files_by_run.setdefault(run_id, []).append(path)

    if not files_by_run and not orphan_files:
        return 0, 0, 0

    keep_ids = set(sorted(files_by_run.keys(), reverse=True)[:keep_runs])
    bytes_freed = 0
    deleted_files = 0
    deleted_dirs = 0

    for run_id, paths in files_by_run.items():
        if run_id in keep_ids:
            continue
        for path in paths:
            try:
                bytes_freed += path.stat().st_size
                path.unlink(missing_ok=True)
                deleted_files += 1
            except OSError:
                continue
        logger.info("GRIB cache: pruned cached files for run %s (%d files)", run_id, len(paths))

    for path in orphan_files:
        if path.suffix != ".tmp":
            continue
        try:
            bytes_freed += path.stat().st_size
            path.unlink(missing_ok=True)
            deleted_files += 1
        except OSError:
            continue

    for directory in sorted((d for d in model_grib.rglob("*") if d.is_dir()), reverse=True):
        try:
            next(directory.iterdir())
        except StopIteration:
            directory.rmdir()
            deleted_dirs += 1
        except OSError:
            continue

    return bytes_freed, deleted_files, deleted_dirs

# Memory pressure thresholds (percent of total RAM, env-overridable so they
# can be tuned on a deployed box without a code change)
def _env_pct(name: str, default: float) -> float:
    try:
        return min(100.0, max(50.0, float(os.getenv(name, default))))
    except (TypeError, ValueError):
        return default


MEMORY_PRESSURE_THRESHOLD_PCT = _env_pct("MEMORY_PRESSURE_THRESHOLD_PCT", 85.0)  # warning + cache clearing
MEMORY_LOWPRI_SHED_THRESHOLD_PCT = _env_pct("MEMORY_LOWPRI_SHED_THRESHOLD_PCT", 90.0)  # shed lowpri workers one at a time
MEMORY_LOWPRI_KILL_ALL_PCT = _env_pct("MEMORY_LOWPRI_KILL_ALL_PCT", 96.0)  # kill all lowpri workers
MEMORY_CRITICAL_THRESHOLD_PCT = _env_pct("MEMORY_CRITICAL_THRESHOLD_PCT", 98.0)  # aggressive cleanup / shutdown


def _sorted_run_dirs(base: Path) -> list[Path]:
    """Return run directories sorted newest-first (by directory name)."""
    if not base.exists():
        return []
    dirs = [d for d in base.iterdir() if d.is_dir()]
    return sorted(dirs, key=lambda d: d.name, reverse=True)


def enforce_retention(
    rendered_dir: Path = RENDERED_DIR,
    grib_dir: Path = GRIB_CACHE_DIR,
    model: str = "hrrr",
    rendered_model: str | None = None,
    grib_model: str | None = None,
) -> dict:
    """
    Apply retention policy and return a summary dict with bytes_freed and counts.

    Policy:
      - Runs [0, full_rendered): keep all images
      - Runs [full_rendered, full_rendered+partial_rendered): keep only PARTIAL_PARAMS
      - Runs [full_rendered+partial_rendered, ...): delete entire run directory
    """
    full_keep = RETENTION["full_rendered"]
    partial_keep = RETENTION["partial_rendered"]

    model_rendered = rendered_dir / (rendered_model or model)
    model_grib = grib_dir / (grib_model or model)

    run_dirs = _sorted_run_dirs(model_rendered)
    bytes_freed = 0
    deleted_files = 0
    deleted_dirs = 0

    for i, run_dir in enumerate(run_dirs):
        if i < full_keep:
            # Keep everything
            continue

        if i < full_keep + partial_keep:
            # Keep only PARTIAL_PARAMS; delete other param subdirs
            for param_dir in run_dir.iterdir():
                if not param_dir.is_dir():
                    continue
                if param_dir.name not in PARTIAL_PARAMS:
                    sz = sum(f.stat().st_size for f in param_dir.rglob("*") if f.is_file())
                    cnt = sum(1 for f in param_dir.rglob("*") if f.is_file())
                    shutil.rmtree(param_dir)
                    bytes_freed += sz
                    deleted_files += cnt
                    logger.info("Retention: removed %s (%d files, %.1fMB)", param_dir, cnt, sz / 1e6)
        else:
            # Delete the entire run directory
            sz = sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())
            cnt = sum(1 for f in run_dir.rglob("*") if f.is_file())
            shutil.rmtree(run_dir)
            bytes_freed += sz
            deleted_files += cnt
            deleted_dirs += 1
            logger.info("Retention: removed run %s (%d files, %.1fMB)", run_dir.name, cnt, sz / 1e6)

    grib_keep = RETENTION["grib_cache"]
    if model_grib.exists():
        grib_bytes, grib_files, grib_dirs = _cleanup_grib_cache_files(model_grib, grib_keep)
        bytes_freed += grib_bytes
        deleted_files += grib_files
        deleted_dirs += grib_dirs

    return {
        "bytes_freed": bytes_freed,
        "deleted_files": deleted_files,
        "deleted_dirs": deleted_dirs,
    }


def check_disk_pressure(threshold_gb: float = DISK_PRESSURE_THRESHOLD_GB) -> bool:
    """Return True if free space on /data is below threshold_gb."""
    try:
        stat = shutil.disk_usage(str(RENDERED_DIR.parent))
        free_gb = stat.free / 1e9
        if free_gb < threshold_gb:
            logger.warning("Disk pressure: %.1fGB free (threshold %.1fGB)", free_gb, threshold_gb)
            return True
    except Exception as exc:
        logger.error("Disk check failed: %s", exc)
    return False


def aggressive_cleanup(rendered_dir: Path = RENDERED_DIR, model: str = "hrrr") -> dict:
    """Delete all but the most recent full run. Called when disk pressure detected."""
    model_rendered = rendered_dir / model
    run_dirs = _sorted_run_dirs(model_rendered)
    bytes_freed = 0
    deleted_files = 0

    for run_dir in run_dirs[1:]:  # keep only index 0 (newest)
        sz = sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())
        cnt = sum(1 for f in run_dir.rglob("*") if f.is_file())
        shutil.rmtree(run_dir)
        bytes_freed += sz
        deleted_files += cnt
        logger.warning("Aggressive cleanup: removed %s", run_dir.name)

    return {"bytes_freed": bytes_freed, "deleted_files": deleted_files}


def check_memory_pressure(threshold_pct: float = MEMORY_PRESSURE_THRESHOLD_PCT) -> tuple[bool, float]:
    """Check if system memory usage is above threshold.
    
    Returns:
        (is_under_pressure, current_usage_percent)
    """
    try:
        mem = psutil.virtual_memory()
        if mem.percent >= threshold_pct:
            logger.warning("Memory pressure: %.1f%% used (threshold %.1f%%), available: %.1fGB",
                         mem.percent, threshold_pct, mem.available / 1e9)
            return True, mem.percent
        return False, mem.percent
    except Exception as exc:
        logger.error("Memory check failed: %s", exc)
        return False, 0.0


def check_memory_critical(threshold_pct: float = MEMORY_CRITICAL_THRESHOLD_PCT) -> tuple[bool, float]:
    """Check if system memory is critically high.
    
    Returns:
        (is_critical, current_usage_percent)
    """
    try:
        mem = psutil.virtual_memory()
        if mem.percent >= threshold_pct:
            logger.error("CRITICAL: Memory usage %.1f%% (threshold %.1f%%), available: %.1fGB",
                        mem.percent, threshold_pct, mem.available / 1e9)
            return True, mem.percent
        return False, mem.percent
    except Exception as exc:
        logger.error("Memory check failed: %s", exc)
        return False, 0.0


def clear_all_caches():
    """Clear module-level caches across the pipeline to free memory."""
    freed = []
    try:
        from .processor import _latlon_cache
        n = len(_latlon_cache)
        _latlon_cache.clear()
        if n: freed.append(f"latlon({n})")
    except Exception:
        pass
    try:
        from .renderer import _cmap_norm_cache
        n = len(_cmap_norm_cache)
        _cmap_norm_cache.clear()
        if n: freed.append(f"cmap({n})")
    except Exception:
        pass
    try:
        from .satellite.satellite_renderer import clear_satellite_caches
        clear_satellite_caches()
        freed.append("satellite")
    except Exception:
        pass
    if freed:
        logger.info("Cleared caches: %s", ", ".join(freed))
    import gc
    gc.collect()


def get_memory_stats() -> dict:
    """Get current memory statistics."""
    try:
        mem = psutil.virtual_memory()
        return {
            "total_gb": mem.total / 1e9,
            "available_gb": mem.available / 1e9,
            "used_gb": mem.used / 1e9,
            "percent": mem.percent,
            "pressure": mem.percent >= MEMORY_PRESSURE_THRESHOLD_PCT,
            "critical": mem.percent >= MEMORY_CRITICAL_THRESHOLD_PCT,
        }
    except Exception as exc:
        logger.error("Failed to get memory stats: %s", exc)
        return {}
