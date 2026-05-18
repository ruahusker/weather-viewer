"""Level II radar scheduler — continuously processes all sites."""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed, BrokenExecutor as BrokenProcessPool
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import (
    NEXRAD_SITES, RADAR_CACHE_DIR, RADAR_RENDERED_DIR, RADAR_CONFIG,
    POLL_INTERVAL_SECONDS, DOWNLOAD_WORKERS, RENDER_WORKERS, CLEANUP_IN_PROCESS,
)
from .cleanup import cleanup_all

logger = logging.getLogger(__name__)

STATE_FILE = Path("/home/main/weather-viewer/data/radar_state.json")


# ---------------------------------------------------------------------------
# Worker functions (must be top-level for ProcessPoolExecutor pickling)
# ---------------------------------------------------------------------------

def _download_site(site: str) -> tuple[str, Optional[Path]]:
    """Download latest volume for one site. Runs in ThreadPool."""
    from .downloader import Level2Downloader
    try:
        dl = Level2Downloader()
        path = dl.download_latest(site)
        return (site, path)
    except Exception as e:
        logger.warning(f"{site}: download error: {e}")
        return (site, None)


def _parse_and_render(site: str, raw_path: str, timestamp: str) -> tuple[str, int]:
    """Parse volume + render all lowest-tilt products. Runs in ProcessPool."""
    import logging
    from .parser import parse_volume
    from .renderer import Level2Renderer
    
    # Set up basic logging in subprocess
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
        results = renderer.render_lowest_tilt(volume, site, timestamp)
        logger.info(f"{site}: rendered {len(results)} products from {path.name}")
        return (site, len(results))
    except Exception as e:
        logger.error(f"{site}: render failed: {e}")
        return (site, 0)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Level2Scheduler:
    """Scheduler for Level II radar data - continuous processing mode."""

    def __init__(self):
        self.enabled = RADAR_CONFIG.get("enabled", True)
        self.state = self._load_state()

        # Track last downloaded filename per site to detect new data
        if "last_file" not in self.state:
            self.state["last_file"] = {}

        # Track which sites need rendering
        if "pending_render" not in self.state:
            self.state["pending_render"] = {}

        # Get all sites sorted by priority
        self.sites = sorted(
            list(NEXRAD_SITES.keys()),
            key=lambda c: NEXRAD_SITES[c].get("priority", 3)
        )

        RADAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        RADAR_RENDERED_DIR.mkdir(parents=True, exist_ok=True)

        logger.info(f"Level2Scheduler: {len(self.sites)} CONUS sites (continuous mode)")

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

    def _is_site_stale(self, site: str) -> bool:
        """Check if site has stale rendered data (> 60 min old)."""
        try:
            latest_png = RADAR_RENDERED_DIR / "png" / f"{site}_REF_latest.png"
            if latest_png.exists():
                age_min = (time.time() - latest_png.stat().st_mtime) / 60
                return age_min > 60
        except Exception:
            pass
        return False

    def _get_site_priority(self, site: str) -> int:
        """Get render priority for site (lower = higher priority)."""
        # Stale sites get priority 0, others get their config priority
        if self._is_site_stale(site):
            return 0
        return NEXRAD_SITES.get(site, {}).get("priority", 3)

    def download_all_sites(self) -> dict:
        """Download latest data for all sites. Returns dict of site->path for new data."""
        new_downloads = {}
        logger.info(f"=== Download phase: {len(self.sites)} sites ===")
        start = time.time()

        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            futures = {pool.submit(_download_site, site): site for site in self.sites}
            for future in as_completed(futures):
                site = futures[future]
                try:
                    _, path = future.result(timeout=60)
                    if path is None:
                        continue
                    # Check if this is new data
                    fname = path.name
                    if fname != self.state["last_file"].get(site):
                        new_downloads[site] = path
                        self.state["last_file"][site] = fname
                        # Add to pending render queue
                        self.state["pending_render"][site] = str(path)
                except Exception as e:
                    logger.warning(f"{site}: download future error: {e}")

        elapsed = time.time() - start
        logger.info(f"Downloads: {len(new_downloads)} new / {len(self.sites)} total ({elapsed:.1f}s)")
        return new_downloads

    def render_pending_sites(self) -> int:
        """Render all pending sites. Processes until complete. Returns count rendered."""
        pending = self.state.get("pending_render", {})
        if not pending:
            return 0

        # Convert to list and sort by priority (stale first)
        items = [(site, path) for site, path in pending.items()]
        items.sort(key=lambda x: (self._get_site_priority(x[0]), x[0]))

        stale_count = sum(1 for site, _ in items if self._is_site_stale(site))
        logger.info(f"=== Render phase: {len(items)} sites (including {stale_count} stale) ===")
        
        render_start = time.time()
        rendered_count = 0
        completed_sites = []

        try:
            with ProcessPoolExecutor(max_workers=RENDER_WORKERS,
                                     max_tasks_per_child=20) as pool:
                # Submit all render tasks
                futures = {}
                for site, path_str in items:
                    path = Path(path_str)
                    # Extract timestamp from filename
                    try:
                        parts = path.stem.split('_')
                        ts = f"{parts[2]}_{parts[3]}00" if len(parts) >= 4 else "unknown"
                    except (IndexError, ValueError):
                        ts = "unknown"

                    f = pool.submit(_parse_and_render, site, str(path), ts)
                    futures[f] = site

                # Wait for all to complete (no timeout - process everything)
                for future in as_completed(futures):
                    site = futures[future]
                    try:
                        _, count = future.result(timeout=60)
                        rendered_count += count
                        completed_sites.append(site)
                        if count > 0:
                            self.state[f"{site}_last_render"] = \
                                datetime.now(timezone.utc).isoformat()
                    except Exception as e:
                        logger.error(f"{site}: render error: {e}")

                # Remove completed sites from pending
                for site in completed_sites:
                    self.state["pending_render"].pop(site, None)

                logger.info(f"Render complete: {len(completed_sites)}/{len(items)} sites, "
                           f"{rendered_count} products ({time.time() - render_start:.1f}s)")

        except BrokenProcessPool:
            logger.error("ProcessPool crashed, will recreate next cycle")

        return len(completed_sites)

    def run_cycle(self) -> bool:
        """Run one full cycle: download all, render all pending."""
        if not self.enabled:
            return False

        cycle_start = time.time()
        
        # Phase 1: Download all sites
        new_downloads = self.download_all_sites()
        
        # Phase 2: Render ALL pending sites (including any leftovers from previous cycle)
        rendered = self.render_pending_sites()
        
        # Phase 3: Cleanup old data
        if CLEANUP_IN_PROCESS:
            try:
                cleanup_all()
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

        self._save_state()
        
        total_elapsed = time.time() - cycle_start
        logger.info(f"=== Cycle complete: {len(new_downloads)} downloads, {rendered} renders, "
                   f"{total_elapsed:.1f}s ===")

        # Return True if we did any work
        return len(new_downloads) > 0 or rendered > 0

    def run_continuous(self):
        """Run scheduler continuously - processes all sites, then immediately restarts."""
        logger.info(f"Starting Level II radar scheduler (continuous mode, "
                    f"{RENDER_WORKERS} workers, {len(self.sites)} sites)")

        while True:
            try:
                had_work = self.run_cycle()
                
                # If no work was done, pause briefly before checking again
                if not had_work:
                    logger.debug("No new data, pausing 30s before next check")
                    time.sleep(30)
                # Otherwise loop immediately to process next batch
                
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
                time.sleep(30)


def main():
    """Entry point for running radar scheduler standalone."""
    import sys
    # Force unbuffered stdout so logs reach journald immediately
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

    scheduler = Level2Scheduler()

    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        scheduler.run_cycle()
    else:
        scheduler.run_continuous()


if __name__ == "__main__":
    main()
