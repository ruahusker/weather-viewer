"""Satellite scheduler — FIFO queue-based continuous processing.

Architecture similar to radar scheduler:
- Producer threads continuously check/download new satellite files
- Consumer threads continuously render from queue
- Queue is sorted by timestamp (newest first for satellite - opposite of radar)
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from queue import PriorityQueue
from typing import Optional

from .config import SATELLITE_CONFIG, SATELLITE_CACHE_DIR, SATELLITE_RENDERED_DIR
from .goes_downloader import GOESDownloader
from .satellite_renderer import SatelliteRenderer

logger = logging.getLogger(__name__)

STATE_FILE = Path("/home/main/weather-viewer/data/satellite_fifo_state.json")

# Configuration
DOWNLOAD_WORKERS = 3          # threads for S3 downloads
RENDER_WORKERS = 3            # processes for rendering (satellite is CPU-bound)
MAX_FILES_PER_BATCH = 2       # files per product per cycle
SATELLITE_LOOKBACK_HOURS = 2  # only look at recent data


class SatelliteJob:
    """Job for satellite processing queue."""
    def __init__(self, source_id: str, product_type: str, goes_file, timestamp: float):
        self.source_id = source_id
        self.product_type = product_type
        self.goes_file = goes_file
        self.timestamp = timestamp  # for queue sorting
        
    def __lt__(self, other):
        # Newest first (higher timestamp = higher priority)
        return self.timestamp > other.timestamp


def _download_job(source_id: str, product_type: str, goes_file) -> Optional[SatelliteJob]:
    """Download a single satellite file. Returns job for rendering."""
    try:
        downloader = GOESDownloader(satellite=source_id, product="ABI-L2-MCMIP", domain="C")
        output_dir = SATELLITE_CACHE_DIR / source_id / "mcmip"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        path = downloader.download_file(goes_file, output_dir)
        if path:
            ts = goes_file.timestamp.timestamp()
            return SatelliteJob(source_id, product_type, goes_file, ts)
    except Exception as e:
        logger.warning(f"Download failed for {source_id}/{product_type}: {e}")
    return None


def _render_job(job: SatelliteJob) -> bool:
    """Render a satellite job. Runs in process pool."""
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
    
    try:
        from .satellite_renderer import SatelliteRenderer
        from pathlib import Path
        
        renderer = SatelliteRenderer()
        source_id = job.source_id
        product_type = job.product_type
        goes_file = job.goes_file
        
        # Determine render method based on product type
        output_dir = SATELLITE_CACHE_DIR / source_id / "mcmip"
        file_path = output_dir / goes_file.filename
        
        # Simple ABI product rendering (core products)
        if product_type in ["ir", "wv", "vis", "sw_ir"]:
            result = renderer.render_abi_product(file_path, product_type, 
                filename=f"{source_id}_{product_type}_{Path(goes_file.filename).stem}.png")
        else:
            # Skip complex products for now
            return False
            
        if result:
            logger.info(f"[{source_id}-{product_type}] Rendered: {result.png_path.name}")
            return True
    except Exception as e:
        logger.error(f"Render failed for {job.source_id}/{job.product_type}: {e}")
    return False


class SatelliteFifoScheduler:
    """FIFO satellite scheduler - similar to radar architecture."""
    
    def __init__(self):
        self.enabled = True
        self.state = self._load_state()
        self.render_queue = PriorityQueue()
        self.queue_counter = 0
        self.in_progress = set()
        self.in_progress_lock = threading.Lock()
        
        # Downloaders and renderer
        self.goes_downloaders = {}
        self.sat_renderer = None
        
        SATELLITE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        SATELLITE_RENDERED_DIR.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"SatelliteFifoScheduler: {DOWNLOAD_WORKERS} downloaders, {RENDER_WORKERS} renderers")
        
    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except Exception:
                pass
        return {"last_file": {}}
    
    def _save_state(self):
        try:
            STATE_FILE.write_text(json.dumps(self.state, indent=2))
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
    
    def _get_downloader(self, source_id: str):
        """Get or create downloader for source."""
        if source_id not in self.goes_downloaders:
            self.goes_downloaders[source_id] = GOESDownloader(
                satellite=source_id, 
                product="ABI-L2-MCMIP", 
                domain="C"
            )
        return self.goes_downloaders[source_id]
    
    def _list_new_files(self, source_id: str, product_type: str) -> list:
        """List new files that haven't been processed yet."""
        try:
            downloader = self._get_downloader(source_id)
            files = downloader.list_available(hours=SATELLITE_LOOKBACK_HOURS, max_files=50)
            
            if not files:
                return []
            
            # Check which files are already rendered
            png_dir = SATELLITE_RENDERED_DIR / "png"
            new_files = []
            for f in files:
                png_name = f"{source_id}_{product_type}_{Path(f.filename).stem}.png"
                if not (png_dir / png_name).exists():
                    new_files.append(f)
            
            # Sort by timestamp (newest first)
            new_files.sort(key=lambda x: x.timestamp, reverse=True)
            return new_files[:MAX_FILES_PER_BATCH]
            
        except Exception as e:
            logger.warning(f"Error listing files for {source_id}/{product_type}: {e}")
            return []
    
    def producer(self, pool: ThreadPoolExecutor):
        """Continuously check for new satellite data and queue it."""
        logger.info("Satellite producer started")
        
        # Core products only for efficiency
        core_products = ["ir", "wv", "vis", "sw_ir"]
        
        while self.enabled:
            try:
                for source_id in ["goes19"]:  # Can extend for GOES-16
                    for product_type in core_products:
                        # Check for new files
                        new_files = self._list_new_files(source_id, product_type)
                        
                        for goes_file in new_files:
                            # Check if already in progress
                            file_key = f"{source_id}_{product_type}_{goes_file.filename}"
                            with self.in_progress_lock:
                                if file_key in self.in_progress:
                                    continue
                                self.in_progress.add(file_key)
                            
                            # Submit download
                            future = pool.submit(_download_job, source_id, product_type, goes_file)
                            try:
                                job = future.result(timeout=120)
                                if job:
                                    self.queue_counter += 1
                                    self.render_queue.put((job.timestamp, self.queue_counter, job))
                            except Exception as e:
                                logger.warning(f"Download error: {e}")
                                with self.in_progress_lock:
                                    self.in_progress.discard(file_key)
                
                # Brief pause between checks
                time.sleep(10)
                
            except Exception as e:
                logger.error(f"Producer error: {e}")
                time.sleep(5)
    
    def consumer(self, pool: ProcessPoolExecutor):
        """Continuously render from queue."""
        logger.info("Satellite consumer started")
        
        while self.enabled:
            try:
                # Get job from queue (blocking with timeout)
                try:
                    ts, counter, job = self.render_queue.get(timeout=10)
                except:
                    continue
                
                # Submit to process pool
                future = pool.submit(_render_job, job)
                
                try:
                    success = future.result(timeout=90)
                    if success:
                        self.state["last_file"][f"{job.source_id}_{job.product_type}"] = \
                            datetime.now(timezone.utc).isoformat()
                except Exception as e:
                    logger.error(f"Render error: {e}")
                finally:
                    file_key = f"{job.source_id}_{job.product_type}_{job.goes_file.filename}"
                    with self.in_progress_lock:
                        self.in_progress.discard(file_key)
                    self.render_queue.task_done()
                    
            except Exception as e:
                logger.error(f"Consumer error: {e}")
    
    def run_continuous(self):
        """Run continuous satellite pipeline."""
        logger.info("Starting satellite FIFO pipeline")
        
        # Thread pool for downloads (I/O bound)
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as download_pool:
            # Process pool for renders (CPU bound)
            with ProcessPoolExecutor(max_workers=RENDER_WORKERS) as render_pool:
                
                # Start producer thread
                producer_thread = threading.Thread(target=self.producer, args=(download_pool,))
                producer_thread.daemon = True
                producer_thread.start()
                
                # Start consumer threads
                consumer_threads = []
                for i in range(RENDER_WORKERS):
                    t = threading.Thread(target=self.consumer, args=(render_pool,))
                    t.daemon = True
                    t.start()
                    consumer_threads.append(t)
                
                # Main loop
                try:
                    while self.enabled:
                        time.sleep(60)
                        self._save_state()
                        
                        qsize = self.render_queue.qsize()
                        if qsize > 0:
                            logger.info(f"Satellite queue: {qsize} pending, {len(self.in_progress)} rendering")
                            
                except KeyboardInterrupt:
                    logger.info("Shutting down satellite scheduler...")
                    self.enabled = False


def main():
    """Entry point."""
    import sys
    from logging.handlers import RotatingFileHandler
    
    sys.stdout.reconfigure(line_buffering=True)
    
    # Log to both stdout and file
    log_dir = Path("/home/main/weather-viewer/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    
    handlers = [
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(log_dir / "satellite.log", maxBytes=10*1024*1024, backupCount=3)
    ]
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
        handlers=handlers
    )
    
    scheduler = SatelliteFifoScheduler()
    scheduler.run_continuous()


if __name__ == "__main__":
    main()
