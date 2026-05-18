"""Pipeline scheduler — daemon that detects new HRRR runs and processes them.

Architecture:
  - Downloads: ThreadPoolExecutor (I/O-bound, 4 threads)
  - Renders:   ProcessPoolExecutor (CPU-bound, 2 processes)
  - Each render worker creates its own MapRenderer/FieldProcessor
  - Resume: skips fxx where all expected PNGs already exist on disk
  - Metadata updated after each fxx completes all regions
"""
import calendar
import fcntl
import json
import logging
import logging.handlers
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .cleanup import aggressive_cleanup, check_disk_pressure, enforce_retention
from .config import (
    GRIB_CACHE_DIR,
    LOG_DIR,
    MAX_DOWNLOAD_THREADS,
    POLL_INTERVAL_SECONDS,
    RENDER_WORKERS,
    RENDERED_DIR,
    SUBHOURLY_CONFIG,
)
from .downloader import HRRRDownloader
from .models.hrrr import MODEL_NAME, PARAM_SEARCH_STRINGS, get_run_fxx

LOG_DIR.mkdir(parents=True, exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "pipeline.log",
    maxBytes=50 * 1024 * 1024,
    backupCount=3,
)
_handler.setFormatter(logging.Formatter("%(asctime)s [HRRR] %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler, logging.StreamHandler()])

logger = logging.getLogger(__name__)

DATA_DIR = RENDERED_DIR.parent
PIPELINE_STATE_FILE = DATA_DIR / "pipeline_state.json"

# Parameters the renderer produces (excluding intermediate component fields)
RENDERED_PARAMS = [
    "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
    "reflectivity", "precip",
    "cape", "cin", "lifted_index", "stp", "scp", "vil", "uh_25km",
    "srh_01km", "srh_03km", "shear_06km", "shear_01km",
    "mlcape", "mlcin", "mucape", "cape_03km", "lcl", "pwat",
    "uh_02km", "max_wind", "efhl", "cloud_cover", "lightning",
    "visibility", "snow_depth", "frozen_pct", "frzlvl",
    "kuchera_6hr", "kuchera_12hr", "snow10_12hr",
]

# Subhourly reflectivity params (15-minute updates)
SUBHOURLY_ENABLED = SUBHOURLY_CONFIG.get("enabled", True)
SUBHOURLY_PARAMS = SUBHOURLY_CONFIG.get("params", ["reflectivity", "temp_2m"])
SUBHOURLY_FXX_RANGE = SUBHOURLY_CONFIG.get("fxx_range", range(1, 7))


# ---------------------------------------------------------------------------
# Render worker — runs in a subprocess via ProcessPoolExecutor
# ---------------------------------------------------------------------------

_worker_state = {}


def _init_render_worker():
    """Called once when each render worker process starts."""
    import matplotlib
    matplotlib.use("Agg")

    from .processor import FieldProcessor
    from .renderer import OverlayRenderer

    _worker_state["renderer"] = OverlayRenderer()
    _worker_state["processor"] = FieldProcessor()


def _find_grib(grib_dir, hour_str, target_fxx, subh: bool = False):
    """Find a GRIB file for a specific forecast hour.
    
    Args:
        grib_dir: Directory containing GRIB files
        hour_str: Run hour string (e.g., '06')
        target_fxx: Forecast hour or formatted subhourly string
        subh: If True, target_fxx is a formatted subhourly string like '01_15min'
    """
    if subh:
        pattern = f"*hrrr.t{hour_str}z.wrfsfcf{target_fxx}.grib2"
    else:
        pattern = f"*hrrr.t{hour_str}z.wrfsfcf{target_fxx:02d}.grib2"
    matches = list(grib_dir.glob(pattern))
    return matches[0] if matches else None


def _render_fxx_worker(grib_path_str, fxx, out_dir_str, run_dt_iso):
    """Render all params as overlays for one forecast hour.

    Runs in a separate process.  Returns (fxx, [rendered_param_names]).
    """
    from .config import GRIB_CACHE_DIR
    from .models.hrrr import PARAM_SEARCH_STRINGS
    from .processor import SNOW_PRODUCTS

    grib_path = Path(grib_path_str)
    out_dir = Path(out_dir_str)
    run_dt = datetime.fromisoformat(run_dt_iso)

    renderer = _worker_state["renderer"]
    processor = _worker_state["processor"]

    grib_dir = GRIB_CACHE_DIR / "hrrr" / run_dt.strftime("%Y%m%d")
    hour_str = f"{run_dt.hour:02d}"

    # Find previous GRIB for hourly precip subtraction
    prev_grib = _find_grib(grib_dir, hour_str, fxx - 1) if fxx > 0 else None

    # Find lookback GRIBs for snow accumulation products
    lookback_gribs = {}
    for product, cfg in SNOW_PRODUCTS.items():
        lb_fxx = fxx - cfg["hours"]
        if lb_fxx > 0 and lb_fxx not in lookback_gribs:
            grib = _find_grib(grib_dir, hour_str, lb_fxx)
            if grib:
                lookback_gribs[lb_fxx] = grib

    from .models.hrrr import COMPOSITE_PARAMS
    all_params = (
        list(PARAM_SEARCH_STRINGS.keys())
        + ["wind_speed", "shear_06km", "shear_01km"]
        + list(COMPOSITE_PARAMS.keys())
        + list(SNOW_PRODUCTS.keys())
    )
    processed = processor.process_for_render(
        grib_path, all_params, fxx, prev_grib, lookback_gribs,
    )

    # Single render pass — no regions
    rendered_params = renderer.render_all_params(processed, out_dir, fxx, run_dt)

    return fxx, sorted(rendered_params)


def _render_subh_worker(grib_path_str, fxx_str, out_dir_str, run_dt_iso):
    """Render subhourly reflectivity and temp overlays.
    
    Similar to _render_fxx_worker but for subhourly data.
    fxx_str is formatted like '01', '01_15min', etc.
    """
    from .config import GRIB_CACHE_DIR
    from .models.hrrr import PARAM_SEARCH_STRINGS

    grib_path = Path(grib_path_str)
    out_dir = Path(out_dir_str)
    run_dt = datetime.fromisoformat(run_dt_iso)

    renderer = _worker_state["renderer"]
    processor = _worker_state["processor"]

    # Only process reflectivity and temp for subhourly
    subh_params = [p for p in SUBHOURLY_PARAMS if p in PARAM_SEARCH_STRINGS]
    
    processed = processor.process_for_render(
        grib_path, subh_params, fxx=0, prev_precip_grib=None, lookback_gribs={},
    )

    # Render to subhourly subfolder
    rendered = []
    for param in subh_params:
        if param not in processed:
            continue
        subh_dir = out_dir / f"{param}_subh"
        out_path = subh_dir / f"f{fxx_str}.png"
        if out_path.exists():
            rendered.append(param)
            continue
        ok = renderer.render_overlay(param, processed[param], out_path)
        if ok:
            rendered.append(param)

    return fxx_str, sorted(rendered)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_dir(run_dt: datetime) -> Path:
    return RENDERED_DIR / MODEL_NAME / run_dt.strftime("%Y%m%d%H")


# Snow products that only exist for certain fxx ranges (need lookback data)
SNOW_PRODUCTS_FXX_MIN = {
    "kuchera_6hr": 6,
    "kuchera_12hr": 12,
    "snow10_12hr": 12,
}


def _is_fxx_complete(out_dir: Path, fxx: int) -> bool:
    """True if all expected overlay PNGs exist for this forecast hour."""
    fname = f"f{fxx:02d}.png"
    for param in RENDERED_PARAMS:
        # Snow products only exist for fxx >= their lookback period
        min_fxx = SNOW_PRODUCTS_FXX_MIN.get(param, 0)
        if fxx < min_fxx:
            continue  # Skip check for snow products that don't exist at this fxx
        if not (out_dir / param / fname).exists():
            return False
    return True


def _is_subh_complete(out_dir: Path, fxx_str: str) -> bool:
    """True if all expected subhourly overlay PNGs exist."""
    for param in SUBHOURLY_PARAMS:
        subh_dir = out_dir / f"{param}_subh"
        if not (subh_dir / f"f{fxx_str}.png").exists():
            return False
    return True


def _write_meta(run_dt: datetime, meta: dict):
    d = _run_dir(run_dt)
    d.mkdir(parents=True, exist_ok=True, mode=0o755)
    (d / "run_meta.json").write_text(json.dumps(meta, indent=2, default=str))


def _write_pipeline_state(state: dict):
    try:
        PIPELINE_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as exc:
        logger.error("Failed to write pipeline state: %s", exc)


def _finalize_run(run_dt: datetime, fxx_list: list[int], completed_fxx: list[int],
                  subh_complete: list[str] | None = None):
    run_id = run_dt.strftime("%Y%m%d%H")
    is_complete = set(completed_fxx) >= set(fxx_list)
    meta = {
        "model": MODEL_NAME,
        "run_id": run_id,
        "init_time": run_dt.strftime("%Y-%m-%dT%H:00:00Z"),
        "parameters": RENDERED_PARAMS,
        "subhourly_params": SUBHOURLY_PARAMS,
        "forecast_hours": fxx_list,
        "completed_fxx": sorted(completed_fxx),
        "is_complete": is_complete,
        "last_updated": datetime.utcnow().isoformat(),
    }
    if subh_complete:
        meta["completed_subh"] = sorted(subh_complete)
    _write_meta(run_dt, meta)
    _write_pipeline_state({
        "status": "idle",
        "last_run": run_id,
        "model": MODEL_NAME,
        "completed_fxx": sorted(completed_fxx),
        "total_fxx": len(fxx_list),
        "is_complete": is_complete,
    })
    logger.info("Run %s: %s (%d/%d fxx)",
                run_id, "complete" if is_complete else "partial",
                len(completed_fxx), len(fxx_list))


# ---------------------------------------------------------------------------
# Run detection
# ---------------------------------------------------------------------------

class RunDetector:
    def get_latest_run(self) -> datetime | None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        candidate_hour = now.hour - 1 if now.minute < 35 else now.hour
        if candidate_hour < 0:
            candidate_hour = 23
        run_dt = now.replace(hour=candidate_hour, minute=0, second=0, microsecond=0)
        dl = HRRRDownloader(run_dt)
        if dl.check_availability(fxx=0):
            return run_dt
        return None

    def completed_run_ids(self) -> set[str]:
        completed = set()
        model_dir = RENDERED_DIR / MODEL_NAME
        if not model_dir.exists():
            return completed
        for run_dir in model_dir.iterdir():
            if not run_dir.is_dir():
                continue
            meta_file = run_dir / "run_meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text())
                    if meta.get("is_complete"):
                        completed.add(run_dir.name)
                except Exception:
                    pass
        return completed

    def get_unprocessed_runs(self) -> list[datetime]:
        completed = self.completed_run_ids()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        candidate = now.replace(minute=0, second=0, microsecond=0)

        # First: finish any in-progress run (has some fxx rendered but isn't complete)
        model_dir = RENDERED_DIR / MODEL_NAME
        if model_dir.exists():
            for run_dir in sorted(model_dir.iterdir(), reverse=True):
                if not run_dir.is_dir() or run_dir.name in completed:
                    continue
                meta_file = run_dir / "run_meta.json"
                if not meta_file.exists():
                    continue
                try:
                    meta = json.loads(meta_file.read_text())
                    if meta.get("completed_fxx") and not meta.get("is_complete"):
                        # This run was started but not finished — prioritize it
                        rid = run_dir.name
                        run_dt = datetime(
                            int(rid[:4]), int(rid[4:6]), int(rid[6:8]), int(rid[8:10])
                        )
                        logger.info("Resuming in-progress run %s", rid)
                        return [run_dt]
                except Exception:
                    pass

        # Then: find newest available run that hasn't been started
        for hours_ago in range(0, 7):
            ts = calendar.timegm(candidate.timetuple()) - hours_ago * 3600
            run_dt = datetime.utcfromtimestamp(ts)
            run_id = run_dt.strftime("%Y%m%d%H")
            if run_id in completed:
                continue
            dl = HRRRDownloader(run_dt)
            if dl.check_availability(fxx=0):
                return [run_dt]

        return []


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_run(run_dt: datetime):
    """Download, process, and render all forecast hours for a HRRR run."""
    run_id = run_dt.strftime("%Y%m%d%H")
    fxx_list = list(get_run_fxx(run_dt.hour))
    out_dir = _run_dir(run_dt)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

    # Resume: figure out what is already done on disk
    todo_fxx = set(f for f in fxx_list if not _is_fxx_complete(out_dir, f))
    completed_fxx = sorted(f for f in fxx_list if f not in todo_fxx)

    if not todo_fxx:
        logger.info("Run %s: already complete, skipping", run_id)
        _finalize_run(run_dt, fxx_list, completed_fxx)
        return

    logger.info("Run %s: %d fxx to render, %d already done",
                run_id, len(todo_fxx), len(completed_fxx))

    meta = {
        "model": MODEL_NAME,
        "run_id": run_id,
        "init_time": run_dt.strftime("%Y-%m-%dT%H:00:00Z"),
        "parameters": RENDERED_PARAMS,
        "forecast_hours": fxx_list,
        "completed_fxx": sorted(completed_fxx),
        "is_complete": False,
        "processing_started": datetime.utcnow().isoformat(),
        "last_updated": datetime.utcnow().isoformat(),
    }
    _write_meta(run_dt, meta)
    _write_pipeline_state({
        "status": "processing",
        "current_run": run_id,
        "model": MODEL_NAME,
        "started": meta["processing_started"],
        "completed_fxx": sorted(completed_fxx),
        "total_fxx": len(fxx_list),
    })

    downloader = HRRRDownloader(run_dt)
    out_dir_str = str(out_dir)
    run_dt_iso = run_dt.isoformat()

    def _do_render_batch(fxx_to_render, grib_paths, pool):
        """Submit fxx to the render pool and collect results, updating metadata."""
        futures = {}
        for fxx in fxx_to_render:
            if _is_fxx_complete(out_dir, fxx):
                if fxx not in completed_fxx:
                    completed_fxx.append(fxx)
                continue
            if fxx not in grib_paths:
                continue
            futures[pool.submit(
                _render_fxx_worker,
                str(grib_paths[fxx]), fxx, out_dir_str, run_dt_iso,
            )] = fxx

        for future in as_completed(futures):
            fxx = futures[future]
            try:
                result_fxx, rendered_params = future.result()

                # Only mark complete if ALL expected PNGs exist
                if _is_fxx_complete(out_dir, result_fxx):
                    if result_fxx not in completed_fxx:
                        completed_fxx.append(result_fxx)

                meta["completed_fxx"] = sorted(completed_fxx)
                meta["last_updated"] = datetime.utcnow().isoformat()
                _write_meta(run_dt, meta)
                _write_pipeline_state({
                    "status": "processing",
                    "current_run": run_id,
                    "model": MODEL_NAME,
                    "started": meta["processing_started"],
                    "completed_fxx": sorted(completed_fxx),
                    "total_fxx": len(fxx_list),
                })

                logger.info(
                    "Run %s: fxx=%02d rendered %d params — %.0f%% done",
                    run_id, result_fxx, len(rendered_params),
                    100 * len(completed_fxx) / len(fxx_list),
                )

                if check_disk_pressure():
                    aggressive_cleanup()

            except Exception as exc:
                logger.error("Run %s: fxx=%02d render failed: %s",
                             run_id, fxx, exc, exc_info=True)

    # Create the process pool once and reuse for all batches.
    # Worker processes are forked BEFORE any download threads start,
    # avoiding fork-from-multithreaded issues.
    with ProcessPoolExecutor(
        max_workers=RENDER_WORKERS,
        initializer=_init_render_worker,
    ) as render_pool:

        # Phase 1: download all available fxx in parallel
        logger.info("Run %s: downloading %d forecast hours", run_id, len(fxx_list))
        t0 = time.time()
        grib_paths = downloader.download_all_parallel()
        logger.info("Run %s: downloaded %d/%d fxx in %.1fs",
                     run_id, len(grib_paths), len(fxx_list), time.time() - t0)

        # Phase 2: render what we have
        renderable = sorted(f for f in todo_fxx if f in grib_paths)
        if renderable:
            logger.info("Run %s: submitting %d fxx for parallel rendering", run_id, len(renderable))
            try:
                _do_render_batch(renderable, grib_paths, render_pool)
            except Exception as pool_exc:
                logger.error("Run %s: render pool failed: %s", run_id, pool_exc)
                # Save progress and exit - monitor will restart
                _finalize_run(run_dt, fxx_list, sorted(set(completed_fxx)))
                raise  # Re-raise to exit main loop

        # Phase 3: poll for any missing fxx (data not yet on server)
        missing = sorted(f for f in todo_fxx if f not in grib_paths and f not in completed_fxx)
        poll_count = 0
        while missing and poll_count < 20:
            logger.info("Run %s: %d fxx not yet available, polling (%d/20): %s",
                        run_id, len(missing), poll_count + 1, missing)
            time.sleep(30)
            poll_count += 1

            new_paths = downloader.download_all_parallel(fxx_list=missing)
            if new_paths:
                grib_paths.update(new_paths)
                logger.info("Run %s: downloaded %d more fxx", run_id, len(new_paths))
                try:
                    _do_render_batch(sorted(new_paths.keys()), grib_paths, render_pool)
                except Exception as pool_exc:
                    logger.error("Run %s: render pool failed during poll: %s", run_id, pool_exc)
                    _finalize_run(run_dt, fxx_list, sorted(set(completed_fxx)))
                    raise

            missing = sorted(f for f in todo_fxx if f not in grib_paths and f not in completed_fxx)

    # Phase 4: Subhourly reflectivity (15-minute updates)
    # Only process subhourly if enabled and we're not in disk pressure
    completed_subh = []
    if SUBHOURLY_ENABLED and not check_disk_pressure():
        completed_subh = _process_subhourly(run_dt, downloader, render_pool, out_dir_str, run_dt_iso)
    elif not SUBHOURLY_ENABLED:
        logger.info("Run %s: subhourly disabled in config", run_id)
    else:
        logger.info("Run %s: skipping subhourly due to disk pressure", run_id)
    
    # Done - finalize with both hourly and subhourly completion
    _finalize_run(run_dt, fxx_list, sorted(set(completed_fxx)), completed_subh)
    
    enforce_retention()
    if check_disk_pressure():
        aggressive_cleanup()


def _process_subhourly(run_dt: datetime, downloader: HRRRDownloader, 
                       render_pool, out_dir_str: str, run_dt_iso: str) -> list[str]:
    """Download and render subhourly reflectivity data.
    
    Returns:
        List of completed subhourly frame identifiers (e.g., ['01', '01_15min', ...])
    """
    from .downloader import format_subh_fxx, SUBHOURLY_INTERVALS
    
    run_id = run_dt.strftime("%Y%m%d%H")
    out_dir = Path(out_dir_str)
    
    # Determine which subhourly frames we need
    subh_fxx_list = list(SUBHOURLY_FXX_RANGE)
    todo_subh = []
    
    for fxx in subh_fxx_list:
        for interval in SUBHOURLY_INTERVALS:
            fxx_str = format_subh_fxx(fxx, interval)
            if not _is_subh_complete(out_dir, fxx_str):
                todo_subh.append((fxx, interval, fxx_str))
    
    if not todo_subh:
        logger.info("Run %s: subhourly already complete", run_id)
        return []
    
    logger.info("Run %s: downloading %d subhourly frames", run_id, len(todo_subh))
    
    # Download subhourly data
    subh_paths = downloader.download_all_subhourly_parallel(
        fxx_list=list(set(fxx for fxx, _, _ in todo_subh))
    )
    
    if not subh_paths:
        logger.warning("Run %s: no subhourly data available", run_id)
        return []
    
    # Render subhourly frames
    futures = {}
    for fxx, interval, fxx_str in todo_subh:
        if fxx_str not in subh_paths:
            continue
        futures[render_pool.submit(
            _render_subh_worker,
            str(subh_paths[fxx_str]), fxx_str, out_dir_str, run_dt_iso,
        )] = fxx_str
    
    completed_subh = []
    for future in as_completed(futures):
        fxx_str = futures[future]
        try:
            result_fxx_str, rendered_params = future.result()
            if _is_subh_complete(out_dir, result_fxx_str):
                completed_subh.append(result_fxx_str)
                logger.debug("Run %s: subh fxx=%s rendered %d params", 
                           run_id, result_fxx_str, len(rendered_params))
        except Exception as exc:
            logger.error("Run %s: subh fxx=%s render failed: %s", run_id, fxx_str, exc)
    
    logger.info("Run %s: completed %d/%d subhourly frames", 
                run_id, len(completed_subh), len(todo_subh))
    return completed_subh


# ---------------------------------------------------------------------------
# Main scheduler loop
# ---------------------------------------------------------------------------

class PipelineScheduler:
    def __init__(self):
        self.detector = RunDetector()

    def run(self):
        logger.info("Pipeline scheduler started")
        _write_pipeline_state({"status": "starting"})

        while True:
            try:
                unprocessed = self.detector.get_unprocessed_runs()
                if unprocessed:
                    logger.info("Found %d unprocessed run(s)", len(unprocessed))
                    # Process the newest incomplete run first
                    for run_dt in sorted(unprocessed, reverse=True)[:1]:
                        process_run(run_dt)
                else:
                    logger.debug("No new runs; sleeping %ds", POLL_INTERVAL_SECONDS)
            except KeyboardInterrupt:
                logger.info("Scheduler interrupted — exiting")
                break
            except Exception as exc:
                logger.error("Scheduler error: %s", exc, exc_info=True)

            time.sleep(POLL_INTERVAL_SECONDS)


_LOCK_FILE = DATA_DIR / "scheduler.lock"


def _acquire_lock():
    """Ensure only one scheduler instance runs at a time."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another scheduler instance is already running. Exiting.", file=sys.stderr)
        sys.exit(1)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd  # must keep reference so the lock isn't released


def main():
    lock = _acquire_lock()  # noqa: F841 — held for process lifetime
    scheduler = PipelineScheduler()
    scheduler.run()


if __name__ == "__main__":
    main()
