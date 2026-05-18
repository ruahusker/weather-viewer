"""Pipeline scheduler for ECMWF IFS model.

Lightweight scheduler that downloads and renders IFS forecasts.
Runs as a separate service alongside the HRRR scheduler.
"""
import fcntl
import json
import logging
import logging.handlers
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from herbie import Herbie

from .cleanup import check_disk_pressure, aggressive_cleanup
from .config import (
    GRIB_CACHE_DIR,
    LOG_DIR,
    POLL_INTERVAL_SECONDS,
    RENDER_WORKERS,
    RENDERED_DIR,
)
from .models.ecmwf import (
    MODEL_NAME,
    PARAM_SEARCH_STRINGS,
    DERIVED_PARAMS,
    SOURCES_PRIORITY,
    get_run_fxx,
)

LOG_DIR.mkdir(parents=True, exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "pipeline_ifs.log",
    maxBytes=50 * 1024 * 1024,
    backupCount=2,
)
_handler.setFormatter(logging.Formatter("%(asctime)s [IFS] %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler, logging.StreamHandler()])

logger = logging.getLogger(__name__)

DATA_DIR = RENDERED_DIR.parent

IFS_RENDERED_PARAMS = [
    "temp_2m", "dewpoint_2m", "wind_speed", "mslp",
    "precip", "mucape", "cloud_cover", "pwat", "snowfall", "snow_depth",
    "temp_850mb", "gh_500mb", "rh_700mb",
]


# ---------------------------------------------------------------------------
# Render worker
# ---------------------------------------------------------------------------

_worker_state = {}


def _init_render_worker():
    import matplotlib
    matplotlib.use("Agg")
    from .processor import FieldProcessor
    from .renderer import OverlayRenderer
    _worker_state["renderer"] = OverlayRenderer()
    _worker_state["processor"] = FieldProcessor()


def _render_fxx_worker(grib_path_str, fxx, out_dir_str, run_dt_iso):
    """Render all IFS params for one forecast hour."""
    grib_path = Path(grib_path_str)
    out_dir = Path(out_dir_str)
    run_dt = datetime.fromisoformat(run_dt_iso)

    renderer = _worker_state["renderer"]
    processor = _worker_state["processor"]

    all_params = list(PARAM_SEARCH_STRINGS.keys()) + list(DERIVED_PARAMS.keys())

    processed = processor.process_for_render(
        grib_path, all_params, fxx,
        prev_precip_grib=None,  # IFS precip is already accumulated totals
        lookback_gribs=None,
    )

    rendered_params = renderer.render_all_params(processed, out_dir, fxx, run_dt)
    return fxx, sorted(rendered_params)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_dir(run_dt: datetime) -> Path:
    return RENDERED_DIR / MODEL_NAME / run_dt.strftime("%Y%m%d%H")


def _is_fxx_complete(out_dir: Path, fxx: int) -> bool:
    fname = f"f{fxx:02d}.png"
    for param in IFS_RENDERED_PARAMS:
        if not (out_dir / param / fname).exists():
            return False
    return True


def _write_meta(run_dt: datetime, meta: dict):
    d = _run_dir(run_dt)
    d.mkdir(parents=True, exist_ok=True, mode=0o755)
    (d / "run_meta.json").write_text(json.dumps(meta, indent=2, default=str))


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _download_fxx(run_dt: datetime, fxx: int) -> Path | None:
    """Download one IFS forecast hour via Herbie."""
    try:
        H = Herbie(
            date=run_dt.strftime("%Y-%m-%d %H:%M"),
            model="ifs",
            product="oper",
            fxx=fxx,
            save_dir=str(GRIB_CACHE_DIR),
            overwrite=False,
            verbose=False,
            priority=SOURCES_PRIORITY,
        )
        if H.grib is None:
            return None
        search = "|".join(PARAM_SEARCH_STRINGS.values())
        local_path = H.download(search)
        logger.info("Downloaded IFS fxx=%03d -> %s", fxx, local_path)
        return Path(local_path)
    except Exception as exc:
        logger.error("Failed to download IFS fxx=%03d: %s", fxx, exc)
        return None


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_run(run_dt: datetime):
    run_id = run_dt.strftime("%Y%m%d%H")
    fxx_list = list(get_run_fxx(run_dt.hour))
    if not fxx_list:
        return

    out_dir = _run_dir(run_dt)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

    todo_fxx = set(f for f in fxx_list if not _is_fxx_complete(out_dir, f))
    completed_fxx = sorted(f for f in fxx_list if f not in todo_fxx)

    if not todo_fxx:
        logger.info("IFS %s: already complete", run_id)
        return

    logger.info("IFS %s: %d fxx to render, %d done", run_id, len(todo_fxx), len(completed_fxx))

    meta = {
        "model": MODEL_NAME,
        "run_id": run_id,
        "init_time": run_dt.strftime("%Y-%m-%dT%H:00:00Z"),
        "parameters": IFS_RENDERED_PARAMS,
        "forecast_hours": fxx_list,
        "completed_fxx": sorted(completed_fxx),
        "is_complete": False,
        "last_updated": datetime.utcnow().isoformat(),
    }
    _write_meta(run_dt, meta)

    # Download all available fxx
    logger.info("IFS %s: downloading %d forecast hours", run_id, len(todo_fxx))
    grib_paths = {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as dl_pool:
        futures = {dl_pool.submit(_download_fxx, run_dt, f): f for f in sorted(todo_fxx)}
        for future in as_completed(futures):
            fxx = futures[future]
            path = future.result()
            if path:
                grib_paths[fxx] = path

    logger.info("IFS %s: downloaded %d/%d fxx", run_id, len(grib_paths), len(todo_fxx))

    # Render
    with ProcessPoolExecutor(
        max_workers=RENDER_WORKERS,
        initializer=_init_render_worker,
    ) as render_pool:
        futures = {}
        for fxx in sorted(grib_paths.keys()):
            if _is_fxx_complete(out_dir, fxx):
                if fxx not in completed_fxx:
                    completed_fxx.append(fxx)
                continue
            futures[render_pool.submit(
                _render_fxx_worker,
                str(grib_paths[fxx]), fxx, str(out_dir), run_dt.isoformat(),
            )] = fxx

        try:
            for future in as_completed(futures):
                fxx = futures[future]
                try:
                    result_fxx, rendered = future.result()
                    if _is_fxx_complete(out_dir, result_fxx):
                        if result_fxx not in completed_fxx:
                            completed_fxx.append(result_fxx)
                    meta["completed_fxx"] = sorted(completed_fxx)
                    meta["last_updated"] = datetime.utcnow().isoformat()
                    _write_meta(run_dt, meta)
                    logger.info("IFS %s: fxx=%03d rendered %d params — %.0f%% done",
                                run_id, result_fxx, len(rendered),
                                100 * len(completed_fxx) / len(fxx_list))
                except Exception as exc:
                    logger.error("IFS %s: fxx=%03d render failed: %s", run_id, fxx, exc)
        except Exception as pool_exc:
            logger.error("IFS %s: render pool failed: %s", run_id, pool_exc)
            # Save progress before exit (monitor will restart)
            meta["completed_fxx"] = sorted(completed_fxx)
            meta["last_updated"] = datetime.utcnow().isoformat()
            _write_meta(run_dt, meta)
            raise

    # Finalize
    is_complete = set(completed_fxx) >= set(fxx_list)
    meta["is_complete"] = is_complete
    meta["completed_fxx"] = sorted(completed_fxx)
    meta["last_updated"] = datetime.utcnow().isoformat()
    _write_meta(run_dt, meta)
    logger.info("IFS %s: %s (%d/%d fxx)",
                run_id, "complete" if is_complete else "partial",
                len(completed_fxx), len(fxx_list))


# ---------------------------------------------------------------------------
# Run detection
# ---------------------------------------------------------------------------

def get_latest_ifs_run() -> datetime | None:
    """IFS runs at 00Z and 12Z. Find the most recent available."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for hours_ago in range(0, 24):
        candidate = now.replace(minute=0, second=0, microsecond=0)
        candidate = datetime.utcfromtimestamp(
            int(candidate.timestamp()) - hours_ago * 3600
        )
        if candidate.hour not in (0, 12):
            continue
        # Check if already complete
        run_dir = _run_dir(candidate)
        meta_path = run_dir / "run_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("is_complete"):
                    continue
            except Exception:
                pass
        # Check availability
        try:
            H = Herbie(
                date=candidate.strftime("%Y-%m-%d %H:%M"),
                model="ifs", product="oper", fxx=0,
                verbose=False, priority=SOURCES_PRIORITY,
            )
            if H.grib is not None:
                return candidate
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_LOCK_FILE = DATA_DIR / "scheduler_ifs.lock"


def _acquire_lock():
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another IFS scheduler is already running.", file=sys.stderr)
        sys.exit(1)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd


def main():
    lock = _acquire_lock()  # noqa: F841
    logger.info("IFS pipeline scheduler started")

    while True:
        try:
            run_dt = get_latest_ifs_run()
            if run_dt:
                logger.info("Processing IFS run %s", run_dt.strftime("%Y%m%d%H"))
                process_run(run_dt)
            else:
                logger.debug("No new IFS runs; sleeping %ds", POLL_INTERVAL_SECONDS * 2)
        except KeyboardInterrupt:
            logger.info("IFS scheduler interrupted — exiting")
            break
        except Exception as exc:
            logger.error("IFS scheduler error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL_SECONDS * 2)


if __name__ == "__main__":
    main()
