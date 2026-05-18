"""HRRR downloader using herbie-data."""
import hashlib
import logging
import threading
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from herbie import Herbie

from .config import GRIB_CACHE_DIR, MAX_DOWNLOAD_THREADS
from .models.hrrr import PARAM_SEARCH_STRINGS, PRS_SEARCH_STRINGS, SOURCES_PRIORITY, get_run_fxx

logger = logging.getLogger(__name__)

# Parameters downloaded for every run
DOWNLOAD_PARAMS = list(PARAM_SEARCH_STRINGS.keys())

# Subhourly intervals available in HRRR (in minutes)
SUBHOURLY_INTERVALS = [0, 15, 30, 45]
_MAX_SEARCH_PARTS_PER_DOWNLOAD = 20


def format_subh_fxx(fxx: int, interval: int) -> str:
    """Format forecast hour string for subhourly product.
    
    Args:
        fxx: Base forecast hour (e.g., 1 for f01)
        interval: Minutes within the hour (0, 15, 30, 45)
    
    Returns:
        Formatted string like 'f01', 'f01_15min', etc.
    """
    if interval == 0:
        return f"{fxx:02d}"
    return f"{fxx:02d}_{interval}min"


def parse_subh_fxx(fxx_str: str) -> tuple[int, int]:
    """Parse subhourly forecast string into (fxx, interval).
    
    Args:
        fxx_str: String like 'f01', 'f01_15min', etc.
    
    Returns:
        Tuple of (base_fxx, interval_minutes)
    """
    if "_" not in fxx_str:
        return int(fxx_str), 0
    base, interval_part = fxx_str.split("_")
    interval = int(interval_part.replace("min", ""))
    return int(base), interval


def _chunk_search_patterns(patterns: list[str], max_parts: int = _MAX_SEARCH_PARTS_PER_DOWNLOAD) -> list[str]:
    clean = [p for p in patterns if p]
    if not clean:
        return []
    return ["|".join(clean[i:i + max_parts]) for i in range(0, len(clean), max_parts)]


def _combined_subset_path(base_path: Path, patterns: list[str]) -> Path:
    digest = hashlib.md5("|".join(patterns).encode("utf-8")).hexdigest()[:12]
    name = base_path.name
    if name.startswith("subset_") and "__" in name:
        _, suffix = name.split("__", 1)
        return base_path.with_name(f"subset_{digest}__{suffix}")
    return base_path.with_name(f"subset_{digest}__{name}")


class HRRRDownloader:
    def __init__(self, run_dt: datetime, max_workers: int = MAX_DOWNLOAD_THREADS):
        self.run_dt = run_dt
        self.max_workers = max_workers
        self._date_str = run_dt.strftime("%Y-%m-%d %H:%M")

    def _make_herbie(self, fxx: int, product: str = "sfc") -> Herbie:
        return Herbie(
            date=self._date_str,
            model="hrrr",
            product=product,
            fxx=fxx,
            save_dir=str(GRIB_CACHE_DIR),
            overwrite=False,
            verbose=False,
            priority=SOURCES_PRIORITY,
        )

    def check_availability(self, fxx: int) -> bool:
        """Return True if this forecast hour is available on the remote source."""
        try:
            H = self._make_herbie(fxx)
            return H.grib is not None
        except Exception:
            return False

    def build_combined_searchstring(self, params: list[str]) -> str:
        """Join individual idx search strings with | for a single partial download."""
        parts = [PARAM_SEARCH_STRINGS[p] for p in params if p in PARAM_SEARCH_STRINGS]
        return "|".join(parts)

    def _download_chunked_subset(self, herbie_obj: Herbie, params: list[str]) -> Path:
        parts = [PARAM_SEARCH_STRINGS[p] for p in params if p in PARAM_SEARCH_STRINGS]
        chunks = _chunk_search_patterns(parts)
        if not chunks:
            raise ValueError("No search patterns provided")
        if len(chunks) == 1:
            return Path(herbie_obj.download(chunks[0]))

        chunk_paths = [Path(herbie_obj.download(chunk)) for chunk in chunks]
        combined_path = _combined_subset_path(chunk_paths[0], parts)
        if combined_path.exists() and combined_path.stat().st_size > 0:
            return combined_path

        with open(combined_path, "wb") as combined_f:
            for chunk_path in chunk_paths:
                with open(chunk_path, "rb") as chunk_f:
                    combined_f.write(chunk_f.read())
        return combined_path

    def download_forecast_hour(self, fxx: int, params: list[str] | None = None) -> Path | None:
        """Download a single sfc forecast hour; returns local path or None on failure."""
        if params is None:
            params = DOWNLOAD_PARAMS
        try:
            H = self._make_herbie(fxx)
            if H.grib is None:
                logger.debug("fxx=%02d not yet available", fxx)
                return None
            local_path = self._download_chunked_subset(H, params)
            logger.info("Downloaded sfc fxx=%02d → %s", fxx, local_path)

            # Also download SRH from prs product and append to the sfc GRIB
            if PRS_SEARCH_STRINGS:
                try:
                    prs_search = "|".join(PRS_SEARCH_STRINGS.values())
                    H_prs = self._make_herbie(fxx, product="prs")
                    prs_path = H_prs.download(prs_search)
                    # Append prs data to sfc file
                    with open(local_path, "ab") as sfc_f, open(prs_path, "rb") as prs_f:
                        sfc_f.write(prs_f.read())
                    logger.info("Appended prs fields (SRH) to fxx=%02d", fxx)
                except Exception as exc:
                    logger.warning("Could not download prs fields for fxx=%02d: %s", fxx, exc)

            return Path(local_path)
        except Exception as exc:
            logger.error("Failed to download sfc fxx=%02d: %s", fxx, exc)
            return None

    def download_subh_hour(self, fxx: int) -> Path | None:
        """Download subhourly product for a forecast hour; returns local path or None."""
        try:
            H = self._make_herbie(fxx, product="subh")
            if H.grib is None:
                logger.debug("subh fxx=%02d not yet available", fxx)
                return None
            # Subhourly: grab temperature and reflectivity at minimum
            search = ":TMP:2 m above ground:|:REFC:entire atmosphere:"
            local_path = H.download(search)
            logger.info("Downloaded subh fxx=%02d → %s", fxx, local_path)
            return Path(local_path)
        except Exception as exc:
            logger.error("Failed to download subh fxx=%02d: %s", fxx, exc)
            return None

    def stream_available_hours(self, callback, stop_event: threading.Event | None = None):
        """
        Poll each forecast hour in order. Calls callback(fxx, grib_path) as each
        becomes available. Blocks until all forecast hours for this run are downloaded.
        """
        fxx_list = list(get_run_fxx(self.run_dt.hour))
        remaining = list(fxx_list)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {}
            submitted = set()

            # Submit the first batch
            for fxx in fxx_list[:self.max_workers * 2]:
                if fxx not in submitted:
                    futures[pool.submit(self._download_with_retry, fxx)] = fxx
                    submitted.add(fxx)

            completed_set = set()

            while remaining:
                if stop_event and stop_event.is_set():
                    logger.info("Download stream cancelled by stop event")
                    break

                # Submit more as slots open
                for fxx in fxx_list:
                    if fxx not in submitted and len(futures) < self.max_workers * 2:
                        futures[pool.submit(self._download_with_retry, fxx)] = fxx
                        submitted.add(fxx)

                done = {f for f in futures if f.done()}
                for future in done:
                    fxx = futures.pop(future)
                    grib_path = future.result()
                    if grib_path is not None:
                        completed_set.add(fxx)
                        remaining = [f for f in remaining if f != fxx]
                        try:
                            callback(fxx, grib_path)
                        except Exception as exc:
                            logger.error("Callback error for fxx=%02d: %s", fxx, exc)
                    else:
                        # Not available yet — requeue after a short wait
                        logger.debug("fxx=%02d not ready, will retry", fxx)
                        import time
                        time.sleep(30)
                        if fxx not in submitted or fxx in completed_set:
                            pass
                        else:
                            futures[pool.submit(self._download_with_retry, fxx)] = fxx

                if done:
                    continue

                import time
                time.sleep(5)

    def _download_with_retry(self, fxx: int, max_retries: int = 5) -> Path | None:
        """Download with exponential back-off until the file is available."""
        import time
        delay = 30
        for attempt in range(max_retries):
            path = self.download_forecast_hour(fxx)
            if path is not None:
                return path
            if attempt < max_retries - 1:
                logger.debug("fxx=%02d attempt %d failed, waiting %ds", fxx, attempt + 1, delay)
                time.sleep(delay)
                delay = min(delay * 2, 300)
        logger.warning("fxx=%02d gave up after %d attempts", fxx, max_retries)
        return None

    def download_all_parallel(self, params: list[str] | None = None, fxx_list: list[int] | None = None) -> dict[int, Path]:
        """
        Download forecast hours in parallel immediately (non-streaming).
        Returns dict mapping fxx → local path for successful downloads.
        """
        if fxx_list is None:
            fxx_list = list(get_run_fxx(self.run_dt.hour))
        results: dict[int, Path] = {}

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_fxx = {
                pool.submit(self.download_forecast_hour, fxx, params): fxx
                for fxx in fxx_list
            }
            for future in as_completed(future_to_fxx):
                fxx = future_to_fxx[future]
                path = future.result()
                if path is not None:
                    results[fxx] = path
        return results

    # -------------------------------------------------------------------------
    # Subhourly methods - 15-minute reflectivity updates
    # -------------------------------------------------------------------------

    def _make_herbie_subh(self, fxx: int) -> Herbie:
        """Create Herbie instance for subhourly product."""
        return Herbie(
            date=self._date_str,
            model="hrrr",
            product="subh",
            fxx=fxx,
            save_dir=str(GRIB_CACHE_DIR),
            overwrite=False,
            verbose=False,
            priority=SOURCES_PRIORITY,
        )

    def download_subhourly(self, fxx: int, interval: int) -> Path | None:
        """Download subhourly data for a specific forecast hour and interval.
        
        Args:
            fxx: Base forecast hour
            interval: Minutes within hour (0, 15, 30, 45)
        
        Returns:
            Path to downloaded GRIB file or None if failed
        """
        # Subhourly only available for fxx >= 1 (f00 is analysis, no subh)
        if fxx < 1:
            return None
            
        try:
            H = self._make_herbie_subh(fxx)
            if H.grib is None:
                logger.debug("subh fxx=%02d interval=%02d not available", fxx, interval)
                return None
            
            # Build search string for reflectivity + 2m temp
            search = ":REFC:entire atmosphere:|:TMP:2 m above ground:"
            local_path = H.download(search)
            
            # Rename to include interval info
            src_path = Path(local_path)
            interval_str = format_subh_fxx(fxx, interval)
            dst_name = src_path.name.replace(
                f"wrfsfcf{fxx:02d}", f"wrfsfcf{interval_str}"
            )
            dst_path = src_path.parent / dst_name
            
            if dst_path != src_path:
                src_path.rename(dst_path)
            
            logger.info("Downloaded subh fxx=%02d interval=%02d → %s", fxx, interval, dst_path.name)
            return dst_path
            
        except Exception as exc:
            logger.error("Failed to download subh fxx=%02d interval=%02d: %s", fxx, interval, exc)
            return None

    def download_all_subhourly_parallel(self, fxx_list: list[int] | None = None) -> dict[str, Path]:
        """Download all subhourly intervals for given forecast hours.
        
        Returns:
            Dict mapping formatted fxx strings (e.g., '01_15min') to paths
        """
        if fxx_list is None:
            # Default: get subhourly for first 6 hours of run
            fxx_list = list(range(1, 7))
        
        results: dict[str, Path] = {}
        
        # Build list of (fxx, interval) tuples to download
        tasks = [(fxx, interval) for fxx in fxx_list for interval in SUBHOURLY_INTERVALS]
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_task = {
                pool.submit(self.download_subhourly, fxx, interval): (fxx, interval)
                for fxx, interval in tasks
            }
            for future in as_completed(future_to_task):
                fxx, interval = future_to_task[future]
                path = future.result()
                if path is not None:
                    key = format_subh_fxx(fxx, interval)
                    results[key] = path
        
        logger.info("Downloaded %d subhourly files", len(results))
        return results

    def check_subh_availability(self, fxx: int) -> bool:
        """Check if subhourly product is available for this forecast hour."""
        try:
            H = self._make_herbie_subh(fxx)
            return H.grib is not None
        except Exception:
            return False
