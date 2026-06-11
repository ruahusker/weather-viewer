#!/usr/bin/env python3
"""Run weather-viewer data cleanup from cron."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path("/home/main/weather-viewer")
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from pipeline.cleanup import enforce_retention
from pipeline.radar.cleanup import cleanup_all as cleanup_radar


LOG = logging.getLogger("cleanup_weather_data")
LOG_DIR = BASE_DIR / "logs"
HOME_RENDERED_DIR = BASE_DIR / "data" / "rendered" / "home"
CLEANUP_TARGETS = [
    {"model": "hrrr"},
    {"model": "href"},
    {"model": "rtma"},
    {"model": "nam3km"},
    {"model": "rap"},
    {"model": "nam12km", "grib_model": "nam"},
    {"model": "ecmwf"},
    {"model": "gfs"},
    {"model": "gefs"},
    {"model": "gdps"},
    {"model": "rrfs"},
    {"model": "aifs"},
    {"model": "ifs"},
]

HOME_STATIC_KEEP = {
    "model_latest.png",
    "model_latest.json",
    "model_quicklook_1.png",
    "model_quicklook_1.json",
    "model_quicklook_2.png",
    "model_quicklook_2.json",
    "model_quicklook_3.png",
    "model_quicklook_3.json",
    "radar_latest.png",
    "radar_latest.json",
    "radar_quicklook_1.png",
    "radar_quicklook_1.json",
    "radar_quicklook_2.png",
    "radar_quicklook_2.json",
    "radar_quicklook_3.png",
    "radar_quicklook_3.json",
    "satellite_latest.png",
    "satellite_latest.json",
    "radar_composite_latest.webp",
    "radar_composite_latest.json",
    "radar_composite_frames.json",
}


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler(
                LOG_DIR / "cleanup.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=3,
            ),
            logging.StreamHandler(),
        ],
    )


def run_model_cleanup() -> None:
    for target in CLEANUP_TARGETS:
        model = target["model"]
        summary = enforce_retention(
            model=model,
            rendered_model=target.get("rendered_model"),
            grib_model=target.get("grib_model"),
        )
        if summary["bytes_freed"] > 0 or summary["deleted_files"] > 0 or summary["deleted_dirs"] > 0:
            LOG.info(
                "[%s] freed %.1f GB, deleted %d files, removed %d dirs",
                model,
                summary["bytes_freed"] / 1e9,
                summary["deleted_files"],
                summary["deleted_dirs"],
            )


def run_satellite_cleanup() -> None:
    try:
        from pipeline.satellite.scheduler import SatelliteScheduler

        scheduler = SatelliteScheduler()
        scheduler.cleanup_old_data()
    except Exception:
        LOG.exception("Satellite cleanup skipped")


def run_home_cleanup() -> None:
    if not HOME_RENDERED_DIR.exists():
        return

    keep_names = set(HOME_STATIC_KEEP)
    frames_path = HOME_RENDERED_DIR / "radar_composite_frames.json"
    try:
        frames = json.loads(frames_path.read_text()) if frames_path.exists() else []
    except Exception:
        frames = []

    if isinstance(frames, list):
        for item in frames:
            if not isinstance(item, dict):
                continue
            image_name = item.get("image")
            if not image_name:
                continue
            keep_names.add(image_name)
            keep_names.add(f"{Path(image_name).stem}.json")

    cutoff_ts = (datetime.now(timezone.utc) - timedelta(hours=36)).timestamp()
    removed = 0
    freed_bytes = 0

    for path in HOME_RENDERED_DIR.iterdir():
        if not path.is_file():
            continue
        if path.name in keep_names:
            continue
        try:
            stale = path.stat().st_mtime < cutoff_ts
            orphaned_composite = path.name.startswith("radar_composite_")
            if stale or orphaned_composite:
                freed_bytes += path.stat().st_size
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue

    if removed:
        LOG.info(
            "[home] removed %d stale files, freed %.1f MB",
            removed,
            freed_bytes / 1e6,
        )


def purge_native_sounding_cache() -> None:
    """Remove oversized native-grid sounding caches now that reduced caches are used."""
    removed_dirs = 0
    freed_bytes = 0

    for model in ("hrrr", "nam3km"):
        model_dir = BASE_DIR / "data" / "rendered" / model
        if not model_dir.exists():
            continue
        for run_dir in model_dir.iterdir():
            if not run_dir.is_dir():
                continue
            native_dir = run_dir / "soundings_native"
            if not native_dir.is_dir():
                continue
            try:
                freed_bytes += sum(f.stat().st_size for f in native_dir.rglob("*") if f.is_file())
            except OSError:
                pass
            for child in sorted(native_dir.rglob("*"), reverse=True):
                try:
                    if child.is_file():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        child.rmdir()
                except OSError:
                    continue
            try:
                native_dir.rmdir()
                removed_dirs += 1
            except OSError:
                continue

    if removed_dirs:
        LOG.info(
            "[soundings_native] removed %d dirs, freed %.1f GB",
            removed_dirs,
            freed_bytes / 1e9,
        )


def cleanup_floater_cache() -> None:
    """Delete all mesoscale floater raw files older than 5 minutes."""
    meso_dir = BASE_DIR / "data" / "satellite_cache" / "goes19" / "mesoscale"
    if not meso_dir.exists():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - 300
    removed = 0
    freed = 0
    for subdir in meso_dir.iterdir():
        if not subdir.is_dir():
            continue
        for f in subdir.iterdir():
            if not f.is_file() or not f.suffix == ".nc":
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    freed += f.stat().st_size
                    f.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    if removed:
        LOG.info(
            "[floater] removed %d raw files older than 5min, freed %.1f MB",
            removed,
            freed / 1e6,
        )


def cleanup_stale_worker_status() -> int:
    """Remove worker status files whose PID is no longer running."""
    removed = 0
    for status_dir in [
        BASE_DIR / "data" / "radar_worker_status",
        BASE_DIR / "data" / "worker_status",
    ]:
        if not status_dir.is_dir():
            continue
        for f in status_dir.glob("*.json"):
            try:
                pid = int(f.stem)
                os.kill(pid, 0)
            except (ValueError, ProcessLookupError):
                f.unlink(missing_ok=True)
                removed += 1
            except PermissionError:
                pass
    return removed


# Disk usage thresholds for deleted-but-open file recovery
_DISK_RECLAIM_THRESHOLD_PCT = 80  # restart services if disk still above 80% after cleanup
_RECLAIM_SERVICES = [
    "weather-pipeline",
    "weather-radar",
    "weather-satellite",
    "weather-web",
]


def _get_disk_usage_pct() -> float:
    """Return disk usage percentage for the data partition."""
    try:
        stat = shutil.disk_usage(str(BASE_DIR / "data"))
        return (stat.used / stat.total) * 100
    except Exception:
        return 0.0


def _reclaim_deleted_open_files() -> None:
    """Restart services to release deleted-but-open file handles.

    When cleanup deletes files that long-running processes still have open,
    Linux keeps the disk space allocated until those processes close the
    handles.  A graceful service restart releases all handles immediately.
    """
    usage_pct = _get_disk_usage_pct()
    if usage_pct < _DISK_RECLAIM_THRESHOLD_PCT:
        return

    LOG.warning(
        "Disk at %.1f%% after cleanup (threshold %d%%) — "
        "restarting services to reclaim deleted-but-open file handles",
        usage_pct,
        _DISK_RECLAIM_THRESHOLD_PCT,
    )

    for svc in _RECLAIM_SERVICES:
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", svc],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                LOG.info("Restarted %s to reclaim file handles", svc)
            else:
                LOG.warning("Failed to restart %s: %s", svc, result.stderr.strip())
        except Exception as exc:
            LOG.warning("Could not restart %s: %s", svc, exc)

    new_pct = _get_disk_usage_pct()
    LOG.info(
        "Disk usage after service restarts: %.1f%% (was %.1f%%)",
        new_pct,
        usage_pct,
    )


def main() -> int:
    configure_logging()
    LOG.info("Starting cron cleanup")
    run_model_cleanup()
    radar_removed = cleanup_radar()
    LOG.info("Radar cleanup removed %d files", radar_removed)
    cleanup_floater_cache()
    run_home_cleanup()
    purge_native_sounding_cache()
    run_satellite_cleanup()
    stale_removed = cleanup_stale_worker_status()
    if stale_removed:
        LOG.info("Removed %d stale worker status files", stale_removed)
    _reclaim_deleted_open_files()
    LOG.info("Cron cleanup complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
