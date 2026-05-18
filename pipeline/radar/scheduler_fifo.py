"""Level II radar scheduler — FIFO by timestamp, 2 downloaders + 4 renderers.

Architecture:
- 2 workers continuously check/download new volumes
- 4 workers continuously render from queue
- Queue is sorted by timestamp (oldest first) for chronological consistency
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from queue import PriorityQueue
from typing import Optional

from .config import (
    NEXRAD_SITES, RADAR_CACHE_DIR, RADAR_RENDERED_DIR, RADAR_CONFIG,
    POLL_INTERVAL_SECONDS, DOWNLOAD_WORKERS, RENDER_WORKERS,
    RENDER_TIMEOUT_SECONDS, CLEANUP_IN_PROCESS,
)
from .cleanup import cleanup_all

logger = logging.getLogger(__name__)

STATE_FILE = Path("/home/main/weather-viewer/data/radar_state.json")
_TS_RE = re.compile(r"(\d{8})_(\d{4,6})")


# ---------------------------------------------------------------------------
# Worker functions (top-level for pickling)
# ---------------------------------------------------------------------------

def _timestamp_seconds_from_path(path: Path) -> float:
    match = _TS_RE.search(path.stem)
    if not match:
        return time.time()
    date_part = match.group(1)
    time_part = match.group(2)
    fmt = "%Y%m%d_%H%M%S" if len(time_part) == 6 else "%Y%m%d_%H%M"
    dt = datetime.strptime(f"{date_part}_{time_part}", fmt)
    return dt.timestamp()

def _download_site(site: str) -> tuple[str, Optional[Path], Optional[float]]:
    """Download latest volume for one site. Returns (site, path, timestamp_seconds)."""
    from .downloader import Level2Downloader
    try:
        dl = Level2Downloader()
        path = dl.download_latest(site)
        if path:
            ts_seconds = _timestamp_seconds_from_path(path)
            return (site, path, ts_seconds)
        return (site, None, None)
    except Exception as e:
        logger.warning(f"{site}: download error: {e}")
        return (site, None, None)


def _parse_and_render(site: str, raw_path: str, timestamp_str: str) -> tuple[str, int]:
    """Parse volume + render all lowest-tilt products."""
    import logging
    from .parser import parse_volume
    from .renderer import Level2Renderer
    
    logging.basicConfig(level=logging.INFO, 
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    logger = logging.getLogger(__name__)

    path = Path(raw_path)
    volume = parse_volume(path)
    if volume is None:
        logger.warning(f"{site}: failed to parse volume {path.name}")
        return (site, 0)

    try:
        renderer = Level2Renderer()
        results = renderer.render_lowest_tilt(volume, site, timestamp_str)
        logger.info(f"{site}: rendered {len(results)} products from {path.name}")
        return (site, len(results))
    except Exception as e:
        logger.error(f"{site}: render failed: {e}")
        return (site, 0)


# ---------------------------------------------------------------------------
# FIFO Scheduler - Oldest Timestamp First
# ---------------------------------------------------------------------------

class FifoRadarScheduler:
    """FIFO-by-timestamp scheduler: 2 downloaders, 4 renderers.
    
    Key insight: Process data in chronological order (oldest first) rather than
    by site priority. This ensures consistent temporal coverage across all sites.
    """

    def __init__(self):
        self.enabled = RADAR_CONFIG.get("enabled", True)
        self.state = self._load_state()
        
        if "last_file" not in self.state:
            self.state["last_file"] = {}
        if "last_check" not in self.state:
            self.state["last_check"] = {}

        # FIFO queue: (timestamp_seconds, insertion_order, site, path, timestamp_str)
        # Lower timestamp = older data = process first
        self.render_queue = PriorityQueue()
        self.queue_counter = 0  # For stable sorting when timestamps equal
        
        # Track sites currently being processed
        self.in_progress = set()
        self.in_progress_lock = threading.Lock()
        
        # All sites
        self.sites = list(NEXRAD_SITES.keys())

        RADAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        RADAR_RENDERED_DIR.mkdir(parents=True, exist_ok=True)

        logger.info(f"FifoRadarScheduler: {len(self.sites)} sites, {DOWNLOAD_WORKERS} downloaders, {RENDER_WORKERS} renderers")
        logger.info("Strategy: Oldest timestamp first (chronological processing)")

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_state(self):
        try:
            STATE_FILE.write_text(json.dumps(self.state, indent=2))
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _should_check_site(self, site: str) -> bool:
        """Check if enough time has passed since last check."""
        last_check = self.state["last_check"].get(site, 0)
        return (time.time() - last_check) > 30  # 30 second minimum

    def _get_timestamp_str(self, path: Path) -> str:
        """Extract timestamp string from filename."""
        try:
            parts = path.stem.split('_')
            return f"{parts[2]}_{parts[3]}00" if len(parts) >= 4 else "unknown"
        except (IndexError, ValueError):
            return "unknown"

    def producer(self, pool: ThreadPoolExecutor):
        """Continuously check sites and queue new data (oldest timestamp first)."""
        logger.info("Producer started - 2 download workers checking sites")
        
        while self.enabled:
            try:
                # Find sites that need checking
                sites_to_check = [s for s in self.sites if self._should_check_site(s)]
                
                if not sites_to_check:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue
                
                # Update last check times
                now = time.time()
                for site in sites_to_check:
                    self.state["last_check"][site] = now
                
                # Download in parallel (2 workers)
                futures = {pool.submit(_download_site, site): site for site in sites_to_check}
                
                for future in as_completed(futures):
                    site = futures[future]
                    try:
                        _, path, ts_seconds = future.result(timeout=60)
                        if path is None:
                            continue
                        
                        # Check if new data
                        fname = path.name
                        if fname == self.state["last_file"].get(site):
                            continue
                        
                        self.state["last_file"][site] = fname
                        
                        # Check if already in progress
                        with self.in_progress_lock:
                            if site in self.in_progress:
                                continue
                            self.in_progress.add(site)
                        
                        # Add to FIFO queue sorted by timestamp (oldest first)
                        ts_str = self._get_timestamp_str(path)
                        self.queue_counter += 1
                        # Priority: ts_seconds (lower = older = process first)
                        self.render_queue.put((ts_seconds, self.queue_counter, site, str(path), ts_str))
                        
                        age_min = (time.time() - ts_seconds) / 60 if ts_seconds else 0
                        logger.debug(f"{site}: queued (age={age_min:.1f}min, ts={ts_str})")
                        
                    except Exception as e:
                        logger.warning(f"{site}: producer error: {e}")
                
            except Exception as e:
                logger.error(f"Producer error: {e}")
                time.sleep(5)

    def consumer(self, pool: ProcessPoolExecutor):
        """Continuously render from queue (oldest timestamp first)."""
        logger.info("Consumer started - rendering oldest data first")
        
        while self.enabled:
            try:
                # Get oldest item from queue (blocking with timeout)
                try:
                    ts_seconds, _, site, path_str, ts_str = self.render_queue.get(timeout=10)
                except:
                    continue  # Queue empty
                
                age_min = (time.time() - ts_seconds) / 60 if ts_seconds else 0
                logger.debug(f"{site}: rendering (age={age_min:.1f}min)")
                
                # Submit to process pool
                future = pool.submit(_parse_and_render, site, path_str, ts_str)
                
                try:
                    _, count = future.result(timeout=RENDER_TIMEOUT_SECONDS)
                    if count > 0:
                        self.state[f"{site}_last_render"] = datetime.now(timezone.utc).isoformat()
                except Exception as e:
                    logger.error(f"{site}: render error: {e}")
                finally:
                    with self.in_progress_lock:
                        self.in_progress.discard(site)
                    self.render_queue.task_done()
                
            except Exception as e:
                logger.error(f"Consumer error: {e}")

    def run_continuous(self):
        """Run continuous pipeline: 2 downloaders + 4 renderers."""
        logger.info(f"Starting FIFO radar pipeline: {DOWNLOAD_WORKERS} downloaders, {RENDER_WORKERS} renderers")
        
        # Thread pool for downloads (I/O bound) - 2 workers
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as download_pool:
            # Process pool for renders (CPU bound) - 4 workers
            with ProcessPoolExecutor(max_workers=RENDER_WORKERS) as render_pool:
                
                # Start producer thread (downloads)
                producer_thread = threading.Thread(target=self.producer, args=(download_pool,))
                producer_thread.daemon = True
                producer_thread.start()
                
                # Start consumer threads (4 render workers)
                consumer_threads = []
                for i in range(RENDER_WORKERS):
                    t = threading.Thread(target=self.consumer, args=(render_pool,))
                    t.daemon = True
                    t.start()
                    consumer_threads.append(t)
                
                # Main monitoring loop
                try:
                    while self.enabled:
                        time.sleep(60)
                        self._save_state()
                        
                        # Cleanup old data
                        if CLEANUP_IN_PROCESS:
                            try:
                                cleanup_all()
                            except Exception as e:
                                logger.error(f"Cleanup error: {e}")
                        
                        # Log status
                        qsize = self.render_queue.qsize()
                        if qsize > 0:
                            logger.info(f"Queue: {qsize} pending, {len(self.in_progress)} rendering")
                        
                except KeyboardInterrupt:
                    logger.info("Shutting down...")
                    self.enabled = False


def main():
    """Entry point."""
    import sys
    from logging.handlers import RotatingFileHandler
    
    sys.stdout.reconfigure(line_buffering=True)
    
    # Log to both stdout (journald) and file (for dashboard)
    log_dir = Path("/home/main/weather-viewer/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "radar.log"
    
    handlers = [
        logging.StreamHandler(sys.stdout),  # journald
        RotatingFileHandler(               # file for dashboard
            log_file, 
            maxBytes=10*1024*1024,  # 10MB rotate
            backupCount=3
        )
    ]
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
        handlers=handlers
    )
    
    logger.info(f"Radar FIFO scheduler logging to {log_file}")
    
    scheduler = FifoRadarScheduler()
    scheduler.run_continuous()


if __name__ == "__main__":
    main()
