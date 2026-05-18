"""Level II radar scheduler — decoupled download + priority render dispatch.

Downloads run continuously in a background thread for ALL sites.
Rendering uses a priority waterfall: Nebraska sites first, then FIFO for
the rest.  Workers process one volume at a time so they can be reassigned
quickly when higher-priority data arrives.
"""
from __future__ import annotations

import json
import logging
import os
import warnings
import re
import gc
import time
import threading
import ctypes
from concurrent.futures import (
    FIRST_COMPLETED, Future, ThreadPoolExecutor, ProcessPoolExecutor, wait,
)
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import (
    NEXRAD_SITES, RADAR_CACHE_DIR, RADAR_RENDERED_DIR, RADAR_CONFIG,
    POLL_INTERVAL_SECONDS, DOWNLOAD_WORKERS, RENDER_WORKERS, RADAR_WORKER_MAX_TASKS,
    CLEANUP_INTERVAL_SECONDS,
    CLEANUP_IN_PROCESS,
    RENDER_TIMEOUT_SECONDS,
    SITE_FAILURE_BLACKLIST_THRESHOLD,
    SITE_BLACKLIST_COOLDOWN_SECONDS,
)
from .cleanup import cleanup_all

logger = logging.getLogger(__name__)

STATE_FILE = Path("/home/main/weather-viewer/data/radar_state.json")
WORKER_STATUS_DIR = Path("/home/main/weather-viewer/data/radar_worker_status")
_TS_RE = re.compile(r"(\d{8})_(\d{4,6})")
CHUNK_LAG_THRESHOLD_MINUTES = 8
MAX_PENDING_DOWNLOADS = max(1, DOWNLOAD_WORKERS * 2)
READY_BACKPRESSURE_SITES = max(RENDER_WORKERS * 2, 8)
READY_HARD_CAP_SITES = max(RENDER_WORKERS * 4, 16)
PRIORITY_POLL_INTERVAL_SECONDS = max(2, int(os.getenv("RADAR_PRIORITY_POLL_INTERVAL_SECONDS", "6")))
FIFO_POLL_INTERVAL_SECONDS = max(4, int(os.getenv("RADAR_FIFO_POLL_INTERVAL_SECONDS", "12")))

# Priority groups — Nebraska at priority 1, everything else at 2 (FIFO)
PRIORITY_SITES = sorted(
    [s for s, cfg in NEXRAD_SITES.items() if cfg.get("priority", 2) == 1]
)
FIFO_SITES = sorted(
    [s for s, cfg in NEXRAD_SITES.items() if cfg.get("priority", 2) != 1]
)

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None

_WORKER_RENDERER = None


# ---------------------------------------------------------------------------
# Worker functions (must be top-level for ProcessPoolExecutor pickling)
# ---------------------------------------------------------------------------

def _timestamp_from_path(path: Path) -> str:
    match = _TS_RE.search(path.stem)
    if not match:
        return "unknown"
    date_part = match.group(1)
    time_part = match.group(2)
    if len(time_part) == 4:
        time_part += "00"
    return f"{date_part}_{time_part}"

def _timestamp_to_dt(timestamp: Optional[str]) -> Optional[datetime]:
    if not timestamp or timestamp == "unknown":
        return None
    try:
        return datetime.strptime(timestamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _download_site(site: str, allow_chunks: bool, downloader=None) -> tuple[str, Optional[Path], Optional[str]]:
    """Download latest volume for one site. Runs in ThreadPool."""
    from .downloader import Level2Downloader
    try:
        dl = downloader or Level2Downloader()
        path = dl.download_latest(site, allow_chunks=allow_chunks)
        if path:
            ts = _timestamp_from_path(path)
            return (site, path, ts)
        return (site, None, None)
    except Exception as e:
        logger.warning(f"{site}: download error: {e}")
        return (site, None, None)


def _write_worker_status(site: str, timestamp: str, raw_path: str):
    """Write a status file for this radar render worker subprocess."""
    WORKER_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    raw_name = Path(raw_path).name
    source = "chunk" if raw_name.startswith("Level2Chunk_") else "full"
    status = {
        "pid": pid,
        "site": site,
        "timestamp": timestamp,
        "raw_path": raw_name,
        "source": source,
        "started": time.time(),
    }
    try:
        (WORKER_STATUS_DIR / f"{pid}.json").write_text(json.dumps(status))
    except Exception:
        pass


def _clear_worker_status():
    """Clear this worker's status file when done."""
    pid = os.getpid()
    try:
        (WORKER_STATUS_DIR / f"{pid}.json").unlink(missing_ok=True)
    except Exception:
        pass


def _worker_rss_mb() -> float:
    """Return current worker RSS in MiB."""
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as fh:
            parts = fh.read().strip().split()
        if len(parts) < 2:
            return 0.0
        rss_pages = int(parts[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        return rss_pages * page_size / (1024 * 1024)
    except Exception:
        return 0.0


# CPU limiting for radar is handled at the OS level via systemd CPUQuota=
# in weather-radar.service. No Python-level cgroup manipulation needed.


def _trim_worker_memory() -> None:
    """Best-effort release of Python/native free memory back to the OS."""
    from .renderer import clear_renderer_caches
    rss = _worker_rss_mb()
    # If RSS is high (over 1000 MiB), clear global caches to contain creep
    if rss > 1000:
        clear_renderer_caches()
    gc.collect()
    if _libc is not None:
        try:
            _libc.malloc_trim(0)
        except Exception:
            pass


def _init_render_worker() -> None:
    """Warm one persistent renderer per radar worker process."""
    global _WORKER_RENDERER
    import logging
    from .renderer import Level2Renderer

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _WORKER_RENDERER = Level2Renderer()
    logger.info(
        "Radar render worker %d initialized persistent renderer (RSS %.0f MiB)",
        os.getpid(),
        _worker_rss_mb(),
    )


def _parse_and_render(site: str, raw_path: str, timestamp: str) -> tuple[str, int]:
    """Parse volume + render all lowest-tilt products. Runs in ProcessPool."""
    global _WORKER_RENDERER
    import logging
    from .parser import parse_volume
    from .renderer import Level2Renderer

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    logger = logging.getLogger(__name__)

    # CPU limiting handled by systemd CPUQuota= on weather-radar.service

    volume = None
    results = None
    path = Path(raw_path)
    rss_before = _worker_rss_mb()

    try:
        _write_worker_status(site, timestamp, raw_path)
        volume = parse_volume(path)
        if volume is None:
            logger.warning(f"{site}: failed to parse volume {path.name}")
            return (site, 0)
        rss_after_parse = _worker_rss_mb()
        logger.info(
            "%s: parsed %s (RSS %.0f -> %.0f MiB)",
            site, path.name, rss_before, rss_after_parse,
        )

        if _WORKER_RENDERER is None:
            _WORKER_RENDERER = Level2Renderer()
            logger.info(
                "%s: recreated persistent renderer inside worker %d",
                site, os.getpid(),
            )
        rss_after_renderer = _worker_rss_mb()
        logger.info(
            "%s: renderer ready for %s (RSS %.0f MiB)",
            site, path.name, rss_after_renderer,
        )
        results = _WORKER_RENDERER.render_lowest_tilt(volume, site, timestamp)
        rss_after_render = _worker_rss_mb()
        logger.info(
            "%s: rendered %d products from %s (RSS %.0f -> %.0f MiB)",
            site, len(results), path.name, rss_before, rss_after_render,
        )
        return (site, len(results))
    except Exception as e:
        logger.error(f"{site}: render failed: {e}")
        return (site, 0)
    finally:
        try:
            del results
        except Exception:
            pass
        try:
            del volume
        except Exception:
            pass
        _trim_worker_memory()
        rss_after_cleanup = _worker_rss_mb()
        logger.info(
            "%s: worker cleanup complete after %s (RSS %.0f MiB)",
            site, path.name, rss_after_cleanup,
        )
        _clear_worker_status()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.error("Failed to save state: %s", e)


def _ensure_state_keys(state: dict):
    for key in ("last_file", "last_check", "last_data_ts"):
        if key not in state:
            state[key] = {}


# ---------------------------------------------------------------------------
# Download manager — background thread fetches new volumes for all sites
# ---------------------------------------------------------------------------

class RadarDownloadManager:
    """Continuously scans all sites and downloads new volumes.

    Downloaded volumes are placed in a *ready* dict. The main render loop
    pops entries from there for dispatch.
    """

    def __init__(self, state: dict):
        self.state = state
        self._ready: dict[str, tuple[Path, str]] = {}  # site -> (path, timestamp)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)
        self._downloader = None
        # Failure tracking for blacklisting
        self._fail_count: dict[str, int] = {}          # site -> consecutive failures
        self._blacklisted: dict[str, float] = {}       # site -> blacklisted_at timestamp

    def start(self):
        t = threading.Thread(target=self._run, name="radar_downloader", daemon=True)
        t.start()

    def stop(self):
        self._stop.set()
        self._pool.shutdown(wait=False)

    def pop_ready(self, site: str) -> tuple[Path, str] | None:
        """Take downloaded data for *site*. Returns ``(path, timestamp)`` or ``None``."""
        with self._lock:
            return self._ready.pop(site, None)

    def ready_sites(self) -> list[str]:
        """Return ready sites ranked for fastest catch-up."""
        with self._lock:
            ready_items = list(self._ready.items())

        now = time.time()

        def _ready_sort_key(item: tuple[str, tuple[Path, str]]) -> tuple[float, float, int, str]:
            site, (_path, ready_ts) = item
            ready_dt = _timestamp_to_dt(ready_ts)
            rendered_dt = _timestamp_to_dt(self.state["last_data_ts"].get(site))
            if ready_dt is None:
                ready_epoch = 0.0
            else:
                ready_epoch = ready_dt.timestamp()

            if ready_dt is None:
                lag_seconds = float("inf") if rendered_dt is None else 0.0
            elif rendered_dt is None:
                lag_seconds = float("inf")
            else:
                lag_seconds = max(0.0, ready_epoch - rendered_dt.timestamp())

            wait_age = max(0.0, now - ready_epoch) if ready_epoch else float("inf")
            priority = int(NEXRAD_SITES[site].get("priority", 2))
            # Bigger backlog gap first, then oldest waiting ready volume, then site priority.
            return (-lag_seconds, -wait_age, priority, site)

        ready_items.sort(key=_ready_sort_key)
        return [site for site, _data in ready_items]

    def requeue(self, site: str, path: Path, timestamp: str):
        """Put a site back in the ready queue (e.g. after a pool crash)."""
        with self._lock:
            # Only requeue if there isn't already newer data waiting
            if site not in self._ready:
                self._ready[site] = (path, timestamp)
                logger.info("%s: re-queued for render after pool crash", site)

    # -- internal ------------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            try:
                self._scan_round()
            except Exception as e:
                logger.error("Download scan error: %s", e)
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def _scan_round(self):
        if self._downloader is None:
            from .downloader import Level2Downloader
            self._downloader = Level2Downloader()

        now = time.time()
        with self._lock:
            ready_backlog = len(self._ready)

        # Unblacklist sites whose cooldown has expired
        expired = [s for s, t in self._blacklisted.items()
                   if now - t >= SITE_BLACKLIST_COOLDOWN_SECONDS]
        for s in expired:
            logger.info("%s: blacklist cooldown expired, re-enabling downloads", s)
            del self._blacklisted[s]
            self._fail_count.pop(s, None)

        due: list[str] = []
        for site in NEXRAD_SITES:
            if site in self._blacklisted:
                continue
            # When render backlog is already high, keep feeding priority sites
            # but stop polling the FIFO fleet until the queue drains.
            if (
                ready_backlog >= READY_BACKPRESSURE_SITES
                and NEXRAD_SITES[site].get("priority", 2) != 1
            ):
                continue
            last_check = self.state["last_check"].get(site, 0)
            priority = NEXRAD_SITES[site].get("priority", 2)
            interval = (
                PRIORITY_POLL_INTERVAL_SECONDS
                if priority == 1
                else FIFO_POLL_INTERVAL_SECONDS
            )
            if now - last_check >= interval:
                due.append(site)

        if not due:
            return

        # Prefer checking sites that are farthest behind first.
        def _sort_key(site: str) -> tuple[float, int, str]:
            ts = self.state["last_data_ts"].get(site)
            dt = _timestamp_to_dt(ts)
            # No data yet = oldest possible
            age = (now - dt.timestamp()) if dt else float("inf")
            priority = int(NEXRAD_SITES[site].get("priority", 2))
            return (-age, priority, site)

        due.sort(key=_sort_key)

        # Under severe backlog, limit each scan round to the most important due
        # sites so the scheduler spends less CPU on downloader coordination.
        if ready_backlog >= READY_HARD_CAP_SITES:
            due = due[:max(DOWNLOAD_WORKERS, len(PRIORITY_SITES))]

        # Process ALL due sites — keep the thread pool full until done.
        due_iter = iter(due)
        active: dict[Future, str] = {}

        # Seed the pool
        for site in due_iter:
            self.state["last_check"][site] = time.time()
            allow_chunks = self._should_try_chunks(site)
            fut = self._pool.submit(_download_site, site, allow_chunks, self._downloader)
            active[fut] = site
            if len(active) >= MAX_PENDING_DOWNLOADS:
                break

        # As each finishes, submit the next due site
        while active:
            done, _ = wait(active, timeout=10, return_when=FIRST_COMPLETED)
            if not done:
                break
            for fut in done:
                site = active.pop(fut)
                self._handle_download_result(fut, site)

                # Backfill from remaining due sites
                next_site = next(due_iter, None)
                if next_site is not None:
                    self.state["last_check"][next_site] = time.time()
                    allow_chunks = self._should_try_chunks(next_site)
                    nfut = self._pool.submit(
                        _download_site, next_site, allow_chunks, self._downloader,
                    )
                    active[nfut] = next_site

    def _handle_download_result(self, fut: Future, site: str):
        try:
            _, path, ts = fut.result()
            if path is None:
                # Track consecutive failures for blacklisting
                self._fail_count[site] = self._fail_count.get(site, 0) + 1
                if self._fail_count[site] >= SITE_FAILURE_BLACKLIST_THRESHOLD:
                    self._blacklisted[site] = time.time()
                    logger.warning(
                        "%s: blacklisted after %d consecutive failures (cooldown %ds)",
                        site, self._fail_count[site], SITE_BLACKLIST_COOLDOWN_SECONDS,
                    )
                return
            # Success — reset failure counter
            self._fail_count.pop(site, None)
            fname = path.name
            if fname == self.state["last_file"].get(site):
                return
            self.state["last_file"][site] = fname
            with self._lock:
                self._ready[site] = (path, ts)
            logger.debug("%s: downloaded %s", site, fname)
        except Exception as e:
            self._fail_count[site] = self._fail_count.get(site, 0) + 1
            if self._fail_count[site] >= SITE_FAILURE_BLACKLIST_THRESHOLD:
                self._blacklisted[site] = time.time()
                logger.warning(
                    "%s: blacklisted after %d consecutive download errors (cooldown %ds)",
                    site, self._fail_count[site], SITE_BLACKLIST_COOLDOWN_SECONDS,
                )
            else:
                logger.warning("%s: download error: %s", site, e)

    def _should_try_chunks(self, site: str) -> bool:
        ts = self.state["last_data_ts"].get(site)
        dt = _timestamp_to_dt(ts)
        if dt is None:
            return True
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age >= CHUNK_LAG_THRESHOLD_MINUTES * 60


# ---------------------------------------------------------------------------
# Main scheduler loop — priority waterfall dispatch
# ---------------------------------------------------------------------------

def run(stop_event: threading.Event | None = None):
    """Run the radar scheduler.

    * Downloads happen continuously in a background thread for **all** sites.
    * Rendering uses ranked ready-site dispatch so the most behind sites catch
      up first without forcing a rigid waterfall.
    * Each worker processes one volume at a time so it can be reassigned
      quickly when higher-priority data appears.

    Parameters
    ----------
    stop_event : threading.Event, optional
        When set, the loop exits gracefully.  If *None*, runs until
        KeyboardInterrupt.
    """
    from concurrent.futures.process import BrokenProcessPool

    if not RADAR_CONFIG.get("enabled", True):
        logger.info("Radar scheduler disabled in config")
        return

    state = _load_state()
    _ensure_state_keys(state)

    RADAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RADAR_RENDERED_DIR.mkdir(parents=True, exist_ok=True)

    # Clean up stale worker status files from previous runs
    if WORKER_STATUS_DIR.exists():
        for f in WORKER_STATUS_DIR.glob("*.json"):
            f.unlink(missing_ok=True)

    dm = RadarDownloadManager(state)
    dm.start()

    pool = ProcessPoolExecutor(
        max_workers=RENDER_WORKERS,
        initializer=_init_render_worker,
        max_tasks_per_child=RADAR_WORKER_MAX_TASKS,
    )
    # site -> (future, timestamp, submit_time, raw_path)
    in_flight: dict[str, tuple[Future, str, float, str]] = {}
    pool_is_broken = False

    last_state_save = time.time()
    last_cleanup = 0.0
    last_status_log = time.time()

    logger.info(
        "Radar scheduler started: %d priority sites (NE), %d FIFO sites, "
        "%d download threads, %d render workers",
        len(PRIORITY_SITES), len(FIFO_SITES), DOWNLOAD_WORKERS, RENDER_WORKERS,
    )

    def _should_stop() -> bool:
        return stop_event is not None and stop_event.is_set()

    try:
        while not _should_stop():

            # ── 1. Collect completed renders + enforce timeouts ────────
            now = time.time()
            done_sites: list[str] = []
            for site, (fut, ts, submit_t, raw_path) in list(in_flight.items()):
                elapsed = now - submit_t
                if fut.done():
                    try:
                        _, count = fut.result()
                        if count > 0:
                            state["last_data_ts"][site] = ts
                            state[f"{site}_last_render"] = datetime.now(timezone.utc).isoformat()
                    except Exception as e:
                        logger.error("%s: render error: %s", site, e)
                        # Re-queue so the site gets another chance
                        dm.requeue(site, Path(raw_path), ts)
                    done_sites.append(site)
                elif elapsed > RENDER_TIMEOUT_SECONDS:
                    # Timed out — cancel and re-queue
                    logger.warning(
                        "%s: render timed out after %.0fs, cancelling and re-queuing",
                        site, elapsed,
                    )
                    fut.cancel()
                    dm.requeue(site, Path(raw_path), ts)
                    done_sites.append(site)
            for site in done_sites:
                del in_flight[site]

            # ── 2. Ranked ready-site dispatch ─────────────────────────
            idle_workers = RENDER_WORKERS - len(in_flight)
            if idle_workers > 0:
                ready = dm.ready_sites()

                def _submit_site(site: str) -> bool:
                    """Submit a site for rendering. Returns True on success."""
                    nonlocal pool, pool_is_broken
                    data = dm.pop_ready(site)
                    if not data:
                        return False
                    path, ts = data
                    try:
                        fut = pool.submit(_parse_and_render, site, str(path), ts)
                        in_flight[site] = (fut, ts, time.time(), str(path))
                        return True
                    except BrokenProcessPool:
                        # Pool died — flag it, requeue the data
                        pool_is_broken = True
                        dm.requeue(site, path, ts)
                        return False

                for site in ready:
                    if idle_workers <= 0 or pool_is_broken:
                        break
                    if site in in_flight:
                        continue
                    if _submit_site(site):
                        idle_workers -= 1

            # ── 3. Pool crash recovery ────────────────────────────────
            if pool_is_broken:
                logger.warning(
                    "Render pool broken — recreating (%d in-flight renders will be re-queued)",
                    len(in_flight),
                )
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                # Re-queue all in-flight work so it's not lost
                for site, (fut, ts, _, raw_path) in in_flight.items():
                    dm.requeue(site, Path(raw_path), ts)
                in_flight.clear()
                pool = ProcessPoolExecutor(
                    max_workers=RENDER_WORKERS,
                    initializer=_init_render_worker,
                    max_tasks_per_child=RADAR_WORKER_MAX_TASKS,
                )
                pool_is_broken = False
                logger.info("Render pool recreated, %d sites re-queued", len(dm.ready_sites()))

            # ── 4. Periodic housekeeping ──────────────────────────────

            if now - last_state_save >= 60:
                _save_state(state)
                last_state_save = now

            if CLEANUP_IN_PROCESS and now - last_cleanup >= CLEANUP_INTERVAL_SECONDS:
                try:
                    cleanup_all()
                    last_cleanup = now
                except Exception as e:
                    logger.error("Cleanup error: %s", e)

            if now - last_status_log >= 60:
                rendering = len(in_flight)
                queued = len(dm.ready_sites())
                blacklisted = len(dm._blacklisted)
                parts = [f"{rendering} rendering", f"{queued} queued"]
                if blacklisted:
                    parts.append(f"{blacklisted} blacklisted")
                logger.info("Radar status: %s", ", ".join(parts))
                last_status_log = now

            time.sleep(2)

    except KeyboardInterrupt:
        logger.info("Radar scheduler interrupted")
    finally:
        dm.stop()
        # Let running renders finish (up to 15s), cancel anything pending.
        logger.info("Waiting for in-flight renders to finish...")
        remaining = {s: f for s, (f, _, _, _) in in_flight.items() if not f.done()}
        if remaining:
            wait(remaining.values(), timeout=15)
        pool.shutdown(wait=False, cancel_futures=True)
        _save_state(state)
        logger.info("Radar scheduler stopped")


# ---------------------------------------------------------------------------
# Backward-compatible class wrapper for unified_scheduler integration
# ---------------------------------------------------------------------------

class Level2PipelineScheduler:
    """Thin wrapper so existing callers can use ``Level2PipelineScheduler().run_continuous()``."""

    def __init__(self):
        self.enabled = RADAR_CONFIG.get("enabled", True)

    def run_continuous(self):
        stop = threading.Event()
        if not self.enabled:
            return
        run(stop_event=stop)


def main():
    """Entry point for running radar scheduler standalone."""
    import sys
    # Suppress multiprocessing resource_tracker warnings (no timestamps, clutter logs)
    warnings.filterwarnings("ignore", module="multiprocessing.resource_tracker")
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    run()


if __name__ == "__main__":
    main()
