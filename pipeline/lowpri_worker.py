"""Low-priority model render worker — runs as a separate systemd service.

Watches a file-based queue for render jobs dispatched by the main pipeline
scheduler, renders them one at a time, and writes results back. The service
can supervise multiple identical worker processes via ``LOWPRI_WORKERS``.

CPU is capped via systemd CPUQuota= on the service unit.
This keeps low-priority (FIFO backfill) work from competing with
the priority waterfall workers in the main pipeline.

Run:  python3 -m pipeline.lowpri_worker
"""
from __future__ import annotations

import gc
import ctypes
import json
import logging
import logging.handlers
import multiprocessing
import os
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# Queue directories
QUEUE_DIR = Path("/home/main/weather-viewer/data/lowpri_queue")
PENDING_DIR = QUEUE_DIR / "pending"
CLAIMED_DIR = QUEUE_DIR / "claimed"
RESULTS_DIR = QUEUE_DIR / "results"

POLL_INTERVAL = max(1, int(os.getenv("LOWPRI_POLL_INTERVAL", "5")))  # seconds between queue checks
SUPERVISOR_POLL_INTERVAL = 5  # seconds between child health checks
PENDING_SCAN_LIMIT = 32
WORKER_TARGET_FILE = QUEUE_DIR / "worker_target"
CLAIMED_STALE_SECONDS = 600  # re-queue claimed jobs older than 10 minutes
WORKER_STATUS_DIR = Path("/home/main/weather-viewer/data/worker_status")

logger = logging.getLogger(__name__)

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None


def _release_memory(label: str = "") -> float:
    gc.collect()
    if _libc is not None:
        _libc.malloc_trim(0)
    rss_mb = 0
    try:
        import psutil
        rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        pass
    if label:
        logger.info("[memory] %s — RSS after release: %.0f MB", label, rss_mb)
    return rss_mb


def _write_worker_status(
    *,
    stage: str,
    detail: str | None = None,
    model_name: str = "",
    run_id: str = "",
    fxx: int | None = None,
    render_params: int | None = None,
    fetched_fields: int | None = None,
) -> None:
    """Write a low-priority worker status file consumed by the dashboard."""
    WORKER_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "worker_type": "low_priority",
        "stage": stage,
        "started": time.time(),
    }
    if model_name:
        payload["model"] = model_name
    if run_id:
        payload["run_id"] = run_id
    if fxx is not None:
        payload["fxx"] = fxx
    if detail:
        payload["detail"] = detail
    if render_params is not None:
        payload["render_params"] = render_params
    if fetched_fields is not None:
        payload["fetched_fields"] = fetched_fields
    try:
        (WORKER_STATUS_DIR / f"{os.getpid()}.json").write_text(json.dumps(payload))
    except Exception:
        pass


def _clear_worker_status() -> None:
    try:
        (WORKER_STATUS_DIR / f"{os.getpid()}.json").unlink(missing_ok=True)
    except Exception:
        pass


def _init_once():
    """Lazy one-time initialization of matplotlib + renderer + processor."""
    import matplotlib
    matplotlib.use("Agg")
    from .processor import FieldProcessor
    from .renderer import OverlayRenderer
    return OverlayRenderer(), FieldProcessor()


def _render_job(job: dict, renderer, processor) -> dict:
    """Render one forecast hour and return a result dict."""
    from .unified_scheduler import (
        _render_single_fxx_impl,
        _worker_rss_mb,
        WORKER_RSS_LIMIT_MB,
        WORKER_CACHE_CLEAR_RSS_MB,
    )

    model_name = job["model_name"]
    fxx = job["fxx"]
    run_dt = datetime.fromisoformat(job["run_dt_iso"])
    out_dir = Path(job["out_dir"])
    grib_path = Path(job["grib_path_str"])
    prev_grib = Path(job["prev_grib_str"]) if job.get("prev_grib_str") else None
    lookback_strs = job.get("lookback_strs")
    lookback_gribs = (
        {int(k): Path(v) for k, v in lookback_strs.items()}
        if lookback_strs else None
    )
    render_params = job["render_params"]
    extra_soundings = job.get("extra_soundings", [])

    # Inject renderer + processor into the module-level _worker_state
    # so _render_single_fxx_impl can find them.
    from . import unified_scheduler as _us
    _us._worker_state["renderer"] = renderer
    _us._worker_state["processor"] = processor

    done_fxx, rendered = _render_single_fxx_impl(
        grib_path, fxx, out_dir, run_dt,
        render_params, extra_soundings,
        prev_grib, lookback_gribs, model_name,
        worker_type="low_priority",
    )

    # Memory management
    rss = _worker_rss_mb()
    if rss > WORKER_CACHE_CLEAR_RSS_MB:
        logger.info("[LP %s] fxx=%02d cleanup — RSS: %.0f MB", model_name.upper(), fxx, rss)
        try:
            renderer.clear_caches()
        except Exception:
            pass
        try:
            from .processor import _latlon_cache
            _latlon_cache.clear()
        except Exception:
            pass
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
    _release_memory()

    return {
        "job_id": job["job_id"],
        "run_id": job.get("run_id", ""),
        "model_name": model_name,
        "fxx": done_fxx,
        "rendered": rendered,
    }


def _worker_count() -> int:
    """Return the configured worker count for this service."""
    raw = os.getenv("LOWPRI_WORKERS", "1").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Invalid LOWPRI_WORKERS=%r; defaulting to 1", raw)
        return 1


def _requeue_stale_claimed_jobs(max_age_seconds: float = 0) -> None:
    """Move abandoned claimed jobs back to pending.

    Args:
        max_age_seconds: Only re-queue files older than this many seconds.
            Use 0 (default) to re-queue all claimed jobs (e.g. at startup).
    """
    now = time.time()
    for claimed_file in sorted(CLAIMED_DIR.glob("*.json")):
        age = 0.0
        if max_age_seconds > 0:
            try:
                age = now - claimed_file.stat().st_mtime
            except FileNotFoundError:
                continue
            if age < max_age_seconds:
                continue
        pending_file = PENDING_DIR / claimed_file.name
        if pending_file.exists():
            claimed_file.unlink(missing_ok=True)
            continue
        try:
            claimed_file.rename(pending_file)
            logger.info("Re-queued stale claimed job %s (age %.0fs)", claimed_file.name, age)
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Failed to re-queue stale claimed job %s: %s", claimed_file.name, exc)


_last_empty_queue_mtime: int | None = None


def _oldest_pending_job() -> Path | None:
    """Find the oldest pending job without sorting the full queue every poll."""
    global _last_empty_queue_mtime
    # Fast path: if the queue dir hasn't changed since a scan that found
    # nothing, skip the directory listing entirely (one stat per poll).
    try:
        dir_mtime = os.stat(PENDING_DIR).st_mtime_ns
    except FileNotFoundError:
        return None
    if _last_empty_queue_mtime is not None and dir_mtime == _last_empty_queue_mtime:
        return None

    oldest: Path | None = None
    oldest_name = ""
    scanned = 0
    try:
        with os.scandir(PENDING_DIR) as entries:
            for entry in entries:
                if not entry.is_file() or not entry.name.endswith(".json"):
                    continue
                scanned += 1
                if oldest is None or entry.name < oldest_name:
                    oldest = Path(entry.path)
                    oldest_name = entry.name
                if scanned >= PENDING_SCAN_LIMIT and oldest is not None:
                    break
    except FileNotFoundError:
        return None
    _last_empty_queue_mtime = dir_mtime if oldest is None else None
    return oldest


def run(worker_label: str | None = None):
    """Main loop: poll for pending jobs, render, write results."""
    for d in (PENDING_DIR, CLAIMED_DIR, RESULTS_DIR):
        d.mkdir(parents=True, exist_ok=True)

    label = worker_label or f"pid-{os.getpid()}"
    logger.info("Low-priority render worker started (%s, pid %d)", label, os.getpid())

    renderer = None
    processor = None
    last_stale_check = 0.0
    idle_status_written = False
    _write_worker_status(stage="idle", detail=f"{label} waiting for queue")
    idle_status_written = True

    while True:
        try:
            # Periodically re-queue stale claimed jobs abandoned by crashed workers
            now = time.monotonic()
            if now - last_stale_check > CLAIMED_STALE_SECONDS / 2:
                last_stale_check = now
                _requeue_stale_claimed_jobs(max_age_seconds=CLAIMED_STALE_SECONDS)

            # Find oldest pending job (FIFO by filename which encodes timestamp)
            job_file = _oldest_pending_job()
            if job_file is None:
                if not idle_status_written:
                    _write_worker_status(stage="idle", detail=f"{label} waiting for queue")
                    idle_status_written = True
                time.sleep(POLL_INTERVAL + random.uniform(0, 0.25))
                continue
            claimed_file = CLAIMED_DIR / job_file.name

            # Claim the job by moving it (atomic on same filesystem)
            try:
                job_file.rename(claimed_file)
            except FileNotFoundError:
                # Another worker grabbed it (future-proofing for multiple workers)
                continue

            try:
                job = json.loads(claimed_file.read_text())
            except Exception as exc:
                logger.error("Failed to parse job %s: %s", claimed_file.name, exc)
                claimed_file.unlink(missing_ok=True)
                _write_worker_status(stage="idle", detail=f"{label} waiting for queue")
                idle_status_written = True
                continue

            idle_status_written = False
            _write_worker_status(
                stage="claimed_job",
                detail=f"{label} claimed {claimed_file.name}",
                model_name=job["model_name"],
                run_id=job.get("run_id", ""),
                fxx=job.get("fxx"),
                render_params=len(job.get("render_params", [])),
            )
            logger.info(
                "[LP %s] Rendering %s %s fxx=%02d",
                label,
                job["model_name"].upper(),
                job.get("run_id", "?"),
                job["fxx"],
            )

            # Lazy init on first job
            if renderer is None:
                renderer, processor = _init_once()

            try:
                result = _render_job(job, renderer, processor)
            except Exception as exc:
                logger.error(
                    "[LP %s] Render failed %s fxx=%02d: %s",
                    label,
                    job["model_name"].upper(), job["fxx"], exc, exc_info=True,
                )
                result = {
                    "job_id": job["job_id"],
                    "run_id": job.get("run_id", ""),
                    "model_name": job["model_name"],
                    "fxx": job["fxx"],
                    "rendered": [],
                    "error": str(exc),
                }

            # Write result
            result_file = RESULTS_DIR / f"{job['job_id']}.json"
            result_file.write_text(json.dumps(result))

            # Clean up claimed file
            claimed_file.unlink(missing_ok=True)
            _write_worker_status(stage="idle", detail=f"{label} waiting for queue")
            idle_status_written = True

            logger.info(
                "[LP %s] Done %s fxx=%02d → %d outputs",
                label,
                job["model_name"].upper(), job["fxx"], len(result.get("rendered", [])),
            )

        except Exception as exc:
            logger.error("Low-priority worker loop error (%s): %s", label, exc, exc_info=True)
            _write_worker_status(stage="error", detail=f"{label}: {exc}")
            idle_status_written = False
            time.sleep(5)


def _configure_logging(worker_name: str) -> None:
    log_dir = Path("/home/main/weather-viewer/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_dir / f"{worker_name}.log", maxBytes=10 * 1024 * 1024, backupCount=3,
        ),
    ]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def _child_main(worker_index: int) -> None:
    sys.stdout.reconfigure(line_buffering=True)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    worker_name = f"lowpri_worker_{worker_index}"
    _configure_logging(worker_name)
    try:
        run(worker_label=f"w{worker_index}")
    finally:
        _clear_worker_status()


def _read_worker_target(max_workers: int) -> int:
    """Read the desired worker count from the throttle file.

    Returns *max_workers* when the file is missing or unreadable so that
    the supervisor defaults to full capacity when the unified scheduler
    has not (yet) written a target.
    """
    try:
        raw = WORKER_TARGET_FILE.read_text().strip()
        target = int(raw)
        return max(0, min(target, max_workers))
    except (FileNotFoundError, ValueError, OSError):
        return max_workers


def _supervise_workers(worker_count: int) -> None:
    logger.info("Low-priority worker supervisor starting %d worker processes", worker_count)
    stop = False
    children: dict[int, multiprocessing.Process] = {}

    def _handle_signal(signum, _frame):
        nonlocal stop
        logger.info("Low-priority supervisor received signal %s", signum)
        stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not stop:
        target = _read_worker_target(worker_count)
        alive_indices = sorted(idx for idx, p in children.items() if p.is_alive())
        alive_count = len(alive_indices)

        if alive_count > target:
            # Scale down: terminate highest-indexed worker (one per cycle)
            victim = alive_indices[-1]
            proc = children[victim]
            logger.info(
                "[LP throttle] Scaling down %d→%d: stopping w%d (pid %d)",
                alive_count, target, victim, proc.pid,
            )
            proc.terminate()
            # Don't join here — let the next cycle reap it

        # Reap any exited workers
        for worker_index in list(children):
            proc = children[worker_index]
            if not proc.is_alive():
                proc.join(timeout=0)
                if proc.exitcode is not None:
                    # Only log as unexpected if we didn't ask it to stop
                    if worker_index <= target:
                        logger.warning(
                            "Low-priority worker w%d exited with code %s; will restart",
                            worker_index, proc.exitcode,
                        )
                del children[worker_index]

        # Scale up: spawn workers up to target (one per cycle)
        if alive_count < target:
            for worker_index in range(1, worker_count + 1):
                if worker_index in children:
                    continue
                new_proc = multiprocessing.Process(
                    target=_child_main,
                    args=(worker_index,),
                    name=f"lowpri_worker_{worker_index}",
                )
                new_proc.start()
                children[worker_index] = new_proc
                logger.info(
                    "[LP throttle] Scaling up %d→%d: started w%d (pid %d)",
                    alive_count, target, worker_index, new_proc.pid,
                )
                break  # One per cycle

        time.sleep(SUPERVISOR_POLL_INTERVAL)

    for worker_index, proc in children.items():
        if proc.is_alive():
            logger.info("Stopping low-priority worker w%d (pid %d)", worker_index, proc.pid)
            proc.terminate()
    for worker_index, proc in children.items():
        proc.join(timeout=10)
        if proc.is_alive():
            logger.warning(
                "Low-priority worker w%d (pid %d) did not exit after SIGTERM; killing",
                worker_index, proc.pid,
            )
            proc.kill()
            proc.join(timeout=5)


def main():
    sys.stdout.reconfigure(line_buffering=True)
    _configure_logging("lowpri_worker")
    for d in (PENDING_DIR, CLAIMED_DIR, RESULTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    _requeue_stale_claimed_jobs()
    worker_count = _worker_count()
    if worker_count == 1:
        logger.info("Low-priority service configured for 1 worker")
        run(worker_label="w1")
        return
    _supervise_workers(worker_count)


if __name__ == "__main__":
    main()
