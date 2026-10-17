"""Satellite data scheduler - CURRENT DATA ONLY, no backlog.

Fetches only the SINGLE most recent image for each product type.
No back-rendering, no backlog processing - just stay current.
"""

import ctypes
import gc
import json
import logging
import os
import re
import threading
import time


# CPU limiting for satellite is handled at the OS level via systemd CPUQuota=
# in weather-satellite.service. No Python-level cgroup manipulation needed.

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import xarray as xr
import psutil

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None


def _release_memory():
    """Force GC and return free pages to the OS."""
    gc.collect()
    if _libc is not None:
        _libc.malloc_trim(0)

# HDF5/NetCDF4 is not thread-safe: concurrent xr.open_dataset() calls from
# multiple threads cause HDF errors or segfaults.  Serialize all file-open +
# data-extraction operations with this lock; downloads remain fully parallel.
from .dataset_cache import load_dataset, clear_dataset_cache

from pipeline.config import (
    COLOR_SCALES, DATA_DIR, SATELLITE_CACHE_DIR, SATELLITE_RENDERED_DIR, SATELLITE_CONFIG
)
from .goes_downloader import GOESDownloader
from .satellite_renderer import (
    SatelliteRenderer,
    clear_satellite_caches,
    set_frame_observer,
    write_frame_meta,
)
from .frame_index import FrameIndex

logger = logging.getLogger(__name__)
SATELLITE_STATUS_FILE = DATA_DIR / "satellite_worker_status.json"
SATELLITE_WORKER_STATUS_DIR = DATA_DIR / "satellite_worker_status"
SATELLITE_CACHE_CLEAR_RSS_MB = max(
    512, int(os.getenv("SATELLITE_CACHE_CLEAR_RSS_MB", "1200"))
)
SATELLITE_META_REFRESH_CACHE_MAX = max(
    32, int(os.getenv("SATELLITE_META_REFRESH_CACHE_MAX", "256"))
)

_PRODUCT_LABELS = {
    "ir": "IR", "wv": "Mid-WV", "vis": "Visible", "sw_ir": "SW-IR",
    "upper_wv": "Upper-WV", "lower_wv": "Lower-WV",
    "vis_blue": "Blue-Vis", "snow_ice": "Snow/Ice", "cirrus": "Cirrus",
    "cloud_particle": "Cloud-Part",
    "natural_color": "Nat-Color", "day_cloud_phase": "Cld-Phase",
    "sandwich": "Sandwich",
    "vis_1km": "Vis-1km", "ir_native": "IR-Native",
    "ir_14": "IR-14", "veggie": "Veggie",
    "m1_ir_1km": "M1-IR", "m2_ir_1km": "M2-IR",
    "m1_vis_1km": "M1-Vis", "m2_vis_1km": "M2-Vis",
    "fire": "Fire", "pwat": "PWAT", "sst": "SST",
    "rain_rate": "Rain", "stability": "Stability",
}

_SATELLITE_SCALE_KEYS = {
    "vis": "goes_vis_02",
    "vis_blue": "goes_vis_blue_01",
    "veggie": "goes_veggie",
    "cirrus": "goes_cirrus_04",
    "snow_ice": "goes_snow_ice_05",
    "cloud_particle": "goes_cloud_particle_06",
    "sw_ir": "goes_sw_ir_07",
    "upper_wv": "goes_upper_wv_08",
    "wv": "goes_wv_09",
    "lower_wv": "goes_lower_wv_10",
    "cloud_top": "goes_cloud_top_11",
    "ozone": "goes_ozone_12",
    "ir": "goes_ir_13",
    "ir_14": "goes_ir_14",
    "ir_dirty": "goes_ir_dirty_15",
    "co2": "goes_co2_16",
}

def _sat_tag(source_id: str, product_type: str) -> str:
    label = _PRODUCT_LABELS.get(product_type, product_type.replace('_', '-').upper())
    sat_short = source_id.replace('goes', 'G')
    return f"[{sat_short}-{label}]"


def _parse_satellite_timestamp(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    for fmt in ("%Y-%jT%H:%M:%SZ", "%Y-%jT%H:%MZ"):
        try:
            return datetime.strptime(ts_str, fmt)
        except Exception:
            continue
    try:
        ts_iso = re.sub(r'T(\d{2})-(\d{2})-(\d{2})', r'T\1:\2:\3', ts_str)
        return datetime.fromisoformat(ts_iso.replace("Z", ""))
    except Exception:
        return None


_FILENAME_TS_RE = re.compile(r"_(\d{7})_(\d{4})(?:_|$)")


def _parse_satellite_timestamp_from_filename(name: str) -> datetime | None:
    match = _FILENAME_TS_RE.search(name)
    if not match:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%Y%j%H%M")
    except ValueError:
        return None


class SatelliteScheduler:
    """Satellite scheduler - CURRENT DATA ONLY."""

    def __init__(self, config: dict = None):
        self.config = config or SATELLITE_CONFIG
        self.enabled = self.config.get("enabled", True)
        render_cfg = self.config.get("render", {})
        self.loop_hours = int(render_cfg.get("loop_hours", 3))
        self.cache_retention_hours = int(render_cfg.get("cache_retention_hours", self.loop_hours))
        self.loop_frames_per_band = int(render_cfg.get("loop_frames_per_band", 36))
        self.max_workers = max(1, int(render_cfg.get("max_workers", 2)))
        self.job_batch_size = max(
            self.max_workers,
            int(render_cfg.get("job_batch_size", self.max_workers * 2)),
        )
        self.latest_frames_per_cycle = max(1, int(render_cfg.get("latest_frames_per_cycle", 1)))
        self.archive_frames_per_cycle = max(
            self.latest_frames_per_cycle,
            min(
                self.loop_frames_per_band,
                int(render_cfg.get("archive_frames_per_cycle", 12)),
            ),
        )
        self.job_timeout_seconds = max(
            120, int(render_cfg.get("job_timeout_seconds", 240))
        )
        self.archive_refresh_interval_seconds = max(
            30, int(render_cfg.get("archive_refresh_interval_seconds", 300))
        )
        self.cleanup_in_process = bool(render_cfg.get("cleanup_in_process", False))
        self.cleanup_interval_seconds = max(
            60, int(render_cfg.get("cleanup_interval_seconds", 900))
        )
        self.state_file = DATA_DIR / "satellite_state.json"
        self.state = self._load_state()
        self._configured_loop_frames_per_band = self.loop_frames_per_band
        self._cycle_do_maintenance = False
        self._last_archive_refresh = 0.0
        self._archive_deadline = None
        self._archive_cursors = {}
        self._archive_jobs_attempted = 0
        self.archive_budget_seconds = max(1, int(render_cfg.get("archive_budget_seconds", 15)))
        self._last_cleanup = 0.0
        
        self.goes_downloaders = {}
        self.goes_cmip_downloaders = {}
        self.l2_downloaders = {}
        self.sat_renderer = SatelliteRenderer(output_dir=SATELLITE_RENDERED_DIR)
        # png_path in frame metas is relative to RENDERED_DIR (data/rendered/).
        self.frame_index = FrameIndex(
            meta_dir=self.sat_renderer.meta_dir,
            manifest_dir=SATELLITE_RENDERED_DIR / "manifests",
            data_root=SATELLITE_RENDERED_DIR.parent,
        )
        self.frame_index.rebuild()
        self.frame_index.flush_manifests()
        set_frame_observer(self.frame_index.record)
        self._index_rebuild_interval = max(
            300, int(render_cfg.get("frame_index_rebuild_seconds", 1800))
        )
        self._status_lock = threading.Lock()
        self._latest_meta_refresh_times: dict[tuple[str, str, str | None], float] = {}
        self._latest_meta_refresh_ttl = max(
            5, int(render_cfg.get("latest_meta_refresh_ttl_seconds", 15))
        )
        
        SATELLITE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        SATELLITE_RENDERED_DIR.mkdir(parents=True, exist_ok=True)
        SATELLITE_WORKER_STATUS_DIR.mkdir(parents=True, exist_ok=True)
        for status_file in SATELLITE_WORKER_STATUS_DIR.glob("*.json"):
            status_file.unlink(missing_ok=True)
        SATELLITE_STATUS_FILE.unlink(missing_ok=True)
        
        logger.info("SatelliteScheduler initialized (CURRENT DATA ONLY mode)")

    @staticmethod
    def _current_worker_key() -> str:
        thread = threading.current_thread()
        ident = threading.get_ident()
        return f"{thread.name}_{ident}"

    def _configured_regions_for_product(
        self,
        product_type: str,
        source_id: str | None = None,
    ) -> list[tuple[str, dict]]:
        """Return enabled regional render targets for a product."""
        regions = []
        for region_name, cfg in self.config.get("regions", {}).items():
            if not cfg.get("enabled", True):
                continue
            if product_type not in cfg.get("products", []):
                continue
            region_source = str(cfg.get("source") or "goes19")
            if source_id is not None and region_source != source_id:
                continue
            regions.append((
                region_name,
                {
                    "source": region_source,
                    "west": float(cfg["west"]),
                    "east": float(cfg["east"]),
                    "south": float(cfg["south"]),
                    "north": float(cfg["north"]),
                },
            ))
        return regions

    @staticmethod
    def _regional_filename(filename: str, region_name: str) -> str:
        stem, dot, suffix = filename.rpartition(".")
        if not dot:
            return f"{filename}_{region_name}"
        return f"{stem}_{region_name}.{suffix}"

    def _missing_regional_renders(
        self,
        product_type: str,
        ts_filename: str,
        source_id: str | None = None,
    ) -> list[tuple[str, dict]]:
        """Return configured regional outputs that are still missing for a frame."""
        missing = []
        for region_name, region_bounds in self._configured_regions_for_product(product_type, source_id=source_id):
            regional_filename = self._regional_filename(ts_filename, region_name)
            png_path = self.sat_renderer.png_dir / regional_filename
            meta_path = self.sat_renderer.meta_dir / regional_filename.replace(".png", ".json")
            if not png_path.exists() or not meta_path.exists():
                missing.append((region_name, region_bounds))
        return missing

    def _cmip_render_is_current(self, product_type: str, filename: str) -> bool:
        """Return true when an existing render was produced from native CMIP."""
        png_path = self.sat_renderer.png_dir / filename
        meta_path = self.sat_renderer.meta_dir / filename.replace(".png", ".json")
        if not png_path.exists() or not meta_path.exists():
            return False
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            return False
        if meta.get("source_product") != "ABI-L2-CMIP":
            return False
        if product_type == "vis_1km":
            return meta.get("native_resolution") is True
        return True

    def _missing_cmip_regional_renders(
        self,
        product_type: str,
        ts_filename: str,
        source_id: str | None = None,
    ) -> list[tuple[str, dict]]:
        """Return regional outputs missing a current native-CMIP render."""
        missing = []
        for region_name, region_bounds in self._configured_regions_for_product(product_type, source_id=source_id):
            regional_filename = self._regional_filename(ts_filename, region_name)
            if not self._cmip_render_is_current(product_type, regional_filename):
                missing.append((region_name, region_bounds))
        return missing

    @staticmethod
    def _regional_target_width(product_type: str) -> int:
        """Use a much denser target grid for regional crops so they stay sharp."""
        if product_type in {"vis_1km", "ir_native"}:
            return 12000
        if product_type in {"ir_14", "veggie", "cloud_particle"}:
            return 9000
        return 6000

    @staticmethod
    def _shared_composite_cache_dir(source_id: str) -> Path:
        """Return the shared raw MCMIP cache used by all composite products."""
        return SATELLITE_CACHE_DIR / source_id / "comp_mcmip"

    def _reset_downloader_caches(self):
        """Force fresh S3 client state on the next cycle after a stalled job."""
        self.goes_downloaders.clear()
        self.goes_cmip_downloaders.clear()
        self.l2_downloaders.clear()
        logger.warning("Reset satellite downloader caches after stalled job(s)")

    def _prune_latest_meta_cache(self) -> None:
        while len(self._latest_meta_refresh_times) > SATELLITE_META_REFRESH_CACHE_MAX:
            try:
                self._latest_meta_refresh_times.pop(next(iter(self._latest_meta_refresh_times)))
            except StopIteration:
                break

    def _trim_memory_if_needed(self, reason: str) -> None:
        rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        if rss_mb < SATELLITE_CACHE_CLEAR_RSS_MB:
            return
        # Regrid entries are disk-backed (npz spill), so clearing them costs a
        # ~10ms reload instead of a multi-second KDTree rebuild.
        clear_satellite_caches(include_regrid=True)
        clear_dataset_cache()
        self._prune_latest_meta_cache()
        _release_memory()
        logger.info(
            "Satellite memory trim after %s (RSS %.0f MB, threshold %d MB)",
            reason,
            rss_mb,
            SATELLITE_CACHE_CLEAR_RSS_MB,
        )

    def _write_status(self, status: dict | None):
        """Publish current satellite loop activity for the dashboard (thread-safe)."""
        try:
            with self._status_lock:
                worker_file = SATELLITE_WORKER_STATUS_DIR / f"{self._current_worker_key()}.json"
                if not status:
                    worker_file.unlink(missing_ok=True)
                    return
                payload = {
                    "pid": os.getpid(),
                    "thread_name": threading.current_thread().name,
                    "thread_ident": threading.get_ident(),
                    "updated": time.time(),
                    **status,
                }
                worker_file.write_text(json.dumps(payload))
                SATELLITE_STATUS_FILE.write_text(json.dumps(payload))
        except Exception:
            pass

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                pass
        return {"last_download": {}, "latest_file": {}}

    def save_state(self):
        try:
            self.state_file.write_text(json.dumps(self.state, indent=2))
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def get_downloader(self, sat_num: int, product: str = "ABI-L1b-Rad", domain: str = "C"):
        key = f"{sat_num}_{product}_{domain}"
        if key not in self.goes_downloaders:
            self.goes_downloaders[key] = GOESDownloader(
                satellite=sat_num, product=product, domain=domain
            )
        return self.goes_downloaders[key]

    def process_product(self, source_id: str, product_type: str, render_func) -> bool:
        """Process ONE product - get only the SINGLE most recent file."""
        try:
            self._write_status({
                "source_id": source_id,
                "product_type": product_type,
                "stage": "checking",
                "task": f"{source_id} {product_type} latest",
            })
            # Extract satellite number (goes19 -> 19) as integer
            sat_num = int(source_id.replace('goes', ''))
            downloader = self.get_downloader(sat_num)
            output_dir = SATELLITE_CACHE_DIR / source_id / "mcmip"
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Get ONLY the most recent file (1 hour lookback, max 1 file)
            files = downloader.list_available(hours=6, max_files=6)
            if not files:
                return False
            
            goes_file = files[0]  # Most recent only
            
            # Check if we already have the latest (using timestamp from file)
            png_dir = self.sat_renderer.png_dir
            # Extract timestamp from filename to check if newer than current latest
            parts = goes_file.filename.split('_')
            s_part = parts[3]  # s20260700106177
            file_ts = f"{s_part[1:5]}-{s_part[5:8]} {s_part[8:10]}:{s_part[10:12]}"
            
            # Check existing latest file
            latest_png = png_dir / f"{source_id}_{product_type}_latest.png"
            if latest_png.exists():
                # Compare timestamps from our state
                last_ts = self.state.get(f"{source_id}_{product_type}_timestamp", "")
                if last_ts == file_ts:
                    return False  # Already rendered this one
                
            # Store timestamp for next check
            self.state[f"{source_id}_{product_type}_timestamp"] = file_ts
            
            # Download and render
            self._write_status({
                "source_id": source_id,
                "product_type": product_type,
                "stage": "downloading",
                "task": f"{source_id} {product_type} download",
                "filename": goes_file.filename,
            })
            path = downloader.download_file(goes_file, output_dir)
            if path:
                self._write_status({
                    "source_id": source_id,
                    "product_type": product_type,
                    "stage": "rendering",
                    "task": f"{source_id} {product_type} render",
                    "filename": Path(path).name,
                })
                try:
                    with load_dataset(path) as ds:
                        result = render_func(ds)
                    if result:
                        logger.info(f"{_sat_tag(source_id, product_type)} Rendered: {result.png_path.name}")
                        self.state["last_download"][f"{source_id}_{product_type}"] = \
                            datetime.now(timezone.utc).isoformat()
                        self._write_status(None)
                        return True
                except Exception as open_err:
                    logger.warning("Corrupt cache file, deleting for re-download: %s (%s)",
                                   Path(path).name, open_err)
                    try:
                        Path(path).unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception as e:
            logger.warning(f"{_sat_tag(source_id, product_type)} Error: {e}")
        self._write_status(None)
        return False

    def process_l1b_band(self, source_id: str, product_type: str, band: str, filename: str,
                          prefetched_files: list = None) -> bool:
        """Process ONE L1b band - get multiple files for animation.

        Args:
            prefetched_files: Pre-fetched file list from a shared S3 listing.
                              When supplied, skips the per-band S3 list_available call.
        """
        try:
            self._write_status({
                "source_id": source_id,
                "product_type": product_type,
                "band": band,
                "stage": "checking",
                "task": f"{source_id} {product_type} {band}",
            })
            # Extract satellite number (goes19 -> 19) as integer
            sat_num = int(source_id.replace('goes', ''))
            downloader = self.get_downloader(sat_num, product="ABI-L1b-Rad", domain="C")
            output_dir = SATELLITE_CACHE_DIR / source_id / "l1b" / band
            output_dir.mkdir(parents=True, exist_ok=True)

            if prefetched_files is not None:
                files = prefetched_files
            else:
                # Fallback: fetch listing independently (legacy / single-band use)
                candidate_max_files = max(self.loop_frames_per_band * 20, 400)
                files = downloader.list_available(
                    hours=self.loop_hours,
                    max_files=candidate_max_files,
                )
            if not files:
                return False

            # Filter for specific band
            band_files = [f for f in files if f"_{band}_" in f.filename or f"-M6{band}_" in f.filename]
            if not band_files:
                return False
            
            png_dir = self.sat_renderer.png_dir
            meta_dir = self.sat_renderer.meta_dir
            rendered_count = 0
            
            # Process up to loop_frames_per_band most recent files for animation.
            for goes_file in band_files[:self.loop_frames_per_band]:
                if self._archive_deadline is not None and time.monotonic() >= self._archive_deadline:
                    break
                try:
                    # Extract timestamp from filename
                    parts = goes_file.filename.split('_')
                    s_part = parts[3]  # s20260700106177
                    year = s_part[1:5]
                    doy = s_part[5:8]
                    hour = s_part[8:10]
                    minute = s_part[10:12]
                    file_ts = f"{year}-{doy} {hour}:{minute}"
                    iso_ts = f"{year}-{doy}T{hour}:{minute}:00Z"
                    
                    # Create timestamped filename
                    ts_filename = f"{source_id}_{product_type}_{year}{doy}_{hour}{minute}.png"
                    ts_png_path = png_dir / ts_filename
                    
                    # Skip if already rendered
                    if ts_png_path.exists():
                        continue
                    
                    # Download
                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "band": band,
                        "stage": "downloading",
                        "task": f"{source_id} {product_type} {band} download",
                        "filename": goes_file.filename,
                    })
                    path = downloader.download_file(goes_file, output_dir)
                    if not path:
                        continue
                    
                    # Render with timestamped filename
                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "band": band,
                        "stage": "rendering",
                        "task": f"{source_id} {product_type} {band} render",
                        "filename": Path(path).name,
                    })
                    try:
                        with load_dataset(path, variables=("Rad", "DQF")) as ds:
                            result = self.render_l1b_band(ds, product_type, band, ts_filename, iso_ts)
                        if result:
                            rendered_count += 1
                            self._refresh_latest_meta(source_id, product_type)
                    except Exception as open_err:
                        # HDF/NetCDF error usually means a partial or corrupt download.
                        # Delete the file so it gets re-fetched next cycle.
                        logger.warning("Corrupt cache file, deleting for re-download: %s (%s)",
                                       Path(path).name, open_err)
                        try:
                            Path(path).unlink(missing_ok=True)
                        except OSError:
                            pass
                except Exception as e:
                    logger.warning(f"Error processing {goes_file.filename}: {e}")
                    continue
            
            # Also update the "latest" file
            if band_files and rendered_count > 0:
                try:
                    goes_file = band_files[0]  # Most recent
                    parts = goes_file.filename.split('_')
                    s_part = parts[3]
                    file_ts = f"{s_part[1:5]}-{s_part[5:8]} {s_part[8:10]}:{s_part[10:12]}"
                    self.state[f"{source_id}_{product_type}_{band}_timestamp"] = file_ts
                    self.state["last_download"][f"{source_id}_{product_type}"] = \
                        datetime.now(timezone.utc).isoformat()
                except:
                    pass

            self._post_process_product(source_id, product_type, rendered_count)
            
            if rendered_count > 0:
                logger.info(f"{_sat_tag(source_id, product_type)} Rendered {rendered_count} frames")
                self._write_status(None)
                return True
        except Exception as e:
            logger.warning(f"{_sat_tag(source_id, product_type)}-{band} Error: {e}")
        self._write_status(None)
        return False
    
    def _cleanup_old_frames(
        self,
        source_id: str,
        product_type: str,
        keep: int = 30,
        region: str | None = None,
    ):
        """Remove stale PNG/meta files outside the active loop window."""
        try:
            self.frame_index.prune(
                source_id,
                product_type,
                region,
                keep=keep,
                max_age_seconds=max(self.loop_hours, 1) * 3600,
            )
        except Exception as e:
            logger.warning(f"Cleanup error: {e}")

    def _refresh_latest_meta(
        self,
        source_id: str,
        product_type: str,
        region: str | None = None,
        *,
        force: bool = False,
    ):
        """Refresh the product latest JSON from the newest exact-product frame."""
        try:
            cache_key = (source_id, product_type, region)
            now = time.time()
            if not force:
                last_refresh = self._latest_meta_refresh_times.get(cache_key, 0.0)
                if (now - last_refresh) < self._latest_meta_refresh_ttl:
                    return

            meta_name = self.frame_index.latest_meta_name(source_id, product_type, region)
            if meta_name is None:
                return
            meta_path = self.sat_renderer.meta_dir / meta_name
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                # Frame was deleted (or corrupted) behind the index's back.
                self.frame_index.remove(source_id, product_type, region, meta_name)
                return

            latest_name = (
                f"{source_id}_{product_type}_latest.json"
                if region is None
                else f"{source_id}_{product_type}_{region}_latest.json"
            )
            latest_meta_path = self.sat_renderer.meta_dir / latest_name
            latest_meta_path.write_text(json.dumps(meta, indent=2))
            self._latest_meta_refresh_times[cache_key] = now
            self._prune_latest_meta_cache()
        except Exception as e:
            logger.warning(f"Latest-meta refresh error for {source_id} {product_type}: {e}")

    def _post_process_product(self, source_id: str, product_type: str, rendered_count: int) -> None:
        """Keep latest metadata fresh every cycle; defer heavy cleanup to maintenance cycles."""
        if rendered_count > 0 or self._cycle_do_maintenance:
            self._refresh_latest_meta(
                source_id, product_type, force=self._cycle_do_maintenance,
            )
            for region_name, _ in self._configured_regions_for_product(product_type):
                self._refresh_latest_meta(
                    source_id, product_type, region=region_name,
                    force=self._cycle_do_maintenance,
                )

        if self._cycle_do_maintenance:
            self._cleanup_old_frames(
                source_id,
                product_type,
                keep=max(self._configured_loop_frames_per_band, 30),
            )
            self._refresh_latest_meta(source_id, product_type, force=True)
            for region_name, _ in self._configured_regions_for_product(product_type):
                self._cleanup_old_frames(
                    source_id,
                    product_type,
                    keep=max(self._configured_loop_frames_per_band, 30),
                    region=region_name,
                )
                self._refresh_latest_meta(
                    source_id, product_type, region=region_name, force=True,
                )

        # Keep web-facing frame manifests current (cheap: dirty keys only).
        self.frame_index.flush_manifests()

    def cleanup_old_data(self):
        """Prune old rendered and cached satellite files."""
        keep = max(self.loop_frames_per_band, 30)
        max_age_seconds = max(self.cache_retention_hours, 2) * 3600
        rendered_max_age_seconds = max(self.loop_hours, 1) * 3600
        now = time.time()

        try:
            for source_id in self.config.get("sources", {}).keys():
                for product_type in self.config.get("products", {}).keys():
                    self._cleanup_old_frames(source_id, product_type, keep=keep)
                    self._refresh_latest_meta(source_id, product_type, force=True)
                    for region_name, _ in self._configured_regions_for_product(product_type):
                        self._cleanup_old_frames(source_id, product_type, keep=keep, region=region_name)
                        self._refresh_latest_meta(
                            source_id, product_type, region=region_name, force=True,
                        )

            for cache_file in SATELLITE_CACHE_DIR.rglob("*"):
                if not cache_file.is_file():
                    continue
                try:
                    if (now - cache_file.stat().st_mtime) > max_age_seconds:
                        cache_file.unlink(missing_ok=True)
                except OSError:
                    continue

            for render_dir in (self.sat_renderer.png_dir, self.sat_renderer.meta_dir):
                for rendered_file in render_dir.glob("*"):
                    if not rendered_file.is_file():
                        continue
                    if rendered_file.stem.endswith("_latest"):
                        continue
                    frame_dt = _parse_satellite_timestamp_from_filename(rendered_file.name)
                    frame_ts = frame_dt.timestamp() if frame_dt is not None else rendered_file.stat().st_mtime
                    try:
                        if (now - frame_ts) > rendered_max_age_seconds:
                            rendered_file.unlink(missing_ok=True)
                    except OSError:
                        continue

            # The sweep above deletes frames directly; resync the index.
            self.frame_index.rebuild()
            self.frame_index.flush_manifests()
            logger.info("Satellite cleanup complete")
        except Exception as exc:
            logger.warning(f"Satellite cleanup error: {exc}")
    
    def render_l1b_band(self, ds, product_type: str, band: str, filename: str, timestamp_str: str = None):
        """Render L1b raw radiance data to PNG with proper Web Mercator reprojection."""
        try:
            from .satellite_renderer import RenderedSatellite
            
            # Get radiance data - L1b uses 'Rad' variable
            if 'Rad' not in ds:
                return None
            
            rad = ds['Rad']
            data = rad.values.astype(np.float32)
            
            # Get dimensions
            if len(data.shape) == 3:
                data = data[0] if data.shape[0] == 1 else data

            scale_key = _SATELLITE_SCALE_KEYS.get(product_type)
            scale_cfg = COLOR_SCALES.get(scale_key, {})
            cmap = scale_cfg.get("cmap", "gray")
            vmin = scale_cfg.get("vmin", 0)
            vmax = scale_cfg.get("vmax", 100)

            if band in ["C01", "C02", "C03", "C04", "C05", "C06"]:
                display_data = self._l1b_reflectance_percent(ds, rad, data)
            elif band in ["C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14", "C15", "C16"]:
                display_data = self._radiance_to_bt_celsius(ds, rad, data)
            else:
                return None

            if display_data is None or not np.isfinite(display_data).any():
                return None
            
            # Use proper Web Mercator reprojection
            png_path = self.sat_renderer.png_dir / filename
            bounds = self.sat_renderer.render_to_mercator(
                display_data.astype(np.float32),
                ds,
                png_path,
                cmap=cmap,
                vmin=vmin,
                vmax=vmax
            )
            
            # Use provided timestamp or generate current one
            if timestamp_str is None:
                timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
            
            result = RenderedSatellite(
                png_path=png_path,
                bounds=bounds,
                timestamp=timestamp_str,
                satellite="goes19",
                product=product_type
            )
            
            # Write timestamped meta file for API animation
            try:
                meta = {
                    "satellite": "G19",
                    "product": product_type,
                    "timestamp": timestamp_str,
                    "bounds": bounds,
                    "png_path": f"satellite/png/{filename}"
                }
                # Create timestamped meta filename
                meta_filename = filename.replace('.png', '.json')
                meta_path = self.sat_renderer.meta_dir / meta_filename
                write_frame_meta(meta_path, meta)
                
                # Also update the "latest" meta file
                latest_meta_path = self.sat_renderer.meta_dir / f"goes19_{product_type}_latest.json"
                latest_meta_path.write_text(json.dumps(meta, indent=2))
            except Exception as e:
                logger.warning(f"Failed to write meta file: {e}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error rendering L1b {band}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    @staticmethod
    def _l1b_reflectance_percent(ds: xr.Dataset, rad: xr.DataArray, data: np.ndarray) -> np.ndarray | None:
        """Convert reflective ABI L1b radiance to reflectance percentage."""
        kappa0 = None
        if "kappa0" in ds:
            try:
                kappa0 = float(np.asarray(ds["kappa0"].values).squeeze())
            except Exception:
                kappa0 = None
        if kappa0 is None:
            try:
                kappa0 = float(rad.attrs.get("kappa0"))
            except Exception:
                kappa0 = None
        if kappa0 is None or kappa0 <= 0:
            return None

        refl = np.full(data.shape, np.nan, dtype=np.float32)
        valid = data > 0
        refl[valid] = np.clip(data[valid] * kappa0 * 100.0, 0.0, 100.0)
        return refl

    @staticmethod
    def _radiance_to_bt_celsius(ds: xr.Dataset, rad: xr.DataArray, data: np.ndarray) -> np.ndarray | None:
        """Convert emissive ABI L1b radiance to brightness temperature in Celsius."""
        def _get_scalar(name: str) -> float:
            if name in ds:
                try:
                    return float(np.asarray(ds[name].values).squeeze())
                except Exception:
                    pass
            try:
                return float(rad.attrs.get(name, 0) or 0)
            except Exception:
                return 0.0

        fk1 = _get_scalar("planck_fk1")
        fk2 = _get_scalar("planck_fk2")
        bc1 = _get_scalar("planck_bc1")
        bc2 = _get_scalar("planck_bc2")
        if fk1 <= 0 or fk2 <= 0 or bc2 == 0:
            return None

        bt = np.full(data.shape, np.nan, dtype=np.float32)
        valid = data > 0
        with np.errstate(divide="ignore", invalid="ignore"):
            bt_kelvin = (fk2 / np.log((fk1 / data[valid]) + 1.0) - bc1) / bc2
        bt[valid] = bt_kelvin.astype(np.float32) - 273.15
        return bt
    

    def process_l2_product(self, source_id: str, product_type: str, product_code: str,
                            domain: str = "F", prefetched_files: list = None,
                            frame_limit: int | None = None) -> bool:
        """Process an L2 derived product (PWAT, SST, fire, rain_rate, stability).

        Works like process_l1b_band but uses render_l2_product from the renderer.
        """
        try:
            self._write_status({
                "source_id": source_id,
                "product_type": product_type,
                "stage": "checking",
                "task": f"{source_id} {product_type}",
            })
            sat_num = int(source_id.replace("goes", ""))
            dl_key = f"{sat_num}_{product_code}_{domain}"
            if dl_key not in self.l2_downloaders:
                self.l2_downloaders[dl_key] = GOESDownloader(
                    satellite=sat_num, product=product_code, domain=domain
                )
            downloader = self.l2_downloaders[dl_key]
            output_dir = self._shared_composite_cache_dir(source_id)
            output_dir.mkdir(parents=True, exist_ok=True)

            if prefetched_files is not None:
                files = prefetched_files
            else:
                files = downloader.list_available(
                    hours=self.loop_hours,
                    max_files=max(self.loop_frames_per_band * 5, 100),
                )
            if not files:
                return False

            png_dir = self.sat_renderer.png_dir
            rendered_count = 0

            max_frames = max(1, min(frame_limit or self.loop_frames_per_band, self.loop_frames_per_band))
            for goes_file in files[:max_frames]:
                if self._archive_deadline is not None and time.monotonic() >= self._archive_deadline:
                    break
                try:
                    parts = goes_file.filename.split("_")
                    s_part = parts[3]
                    year, doy = s_part[1:5], s_part[5:8]
                    hour, minute = s_part[8:10], s_part[10:12]
                    iso_ts = f"{year}-{doy}T{hour}:{minute}:00Z"
                    ts_filename = f"{source_id}_{product_type}_{year}{doy}_{hour}{minute}.png"
                    base_exists = (png_dir / ts_filename).exists()
                    missing_regions = self._missing_regional_renders(
                        product_type,
                        ts_filename,
                        source_id=source_id,
                    )

                    if base_exists and not missing_regions:
                        continue

                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "stage": "downloading",
                        "task": f"{source_id} {product_type} download",
                        "filename": goes_file.filename,
                    })
                    path = downloader.download_file(goes_file, output_dir)
                    if not path:
                        continue

                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "stage": "rendering",
                        "task": f"{source_id} {product_type} render",
                        "filename": Path(path).name,
                    })
                    with load_dataset(path) as ds:
                        result = self.sat_renderer.render_l2_product(
                            ds, product_type, filename=ts_filename
                        )
                    if result:
                        rendered_count += 1
                        self._refresh_latest_meta(source_id, product_type)
                except Exception as e:
                    logger.warning(f"Error processing {product_type} {goes_file.filename}: {e}")

            self._post_process_product(source_id, product_type, rendered_count)

            if rendered_count > 0:
                self.state["last_download"][f"{source_id}_{product_type}"] = \
                    datetime.now(timezone.utc).isoformat()
                logger.info(f"{_sat_tag(source_id, product_type)} Rendered {rendered_count} frames")
                self._write_status(None)
                return True
        except Exception as e:
            logger.warning(f"{_sat_tag(source_id, product_type)} Error: {e}")
        self._write_status(None)
        return False

    def process_cmip_band(self, source_id: str, product_type: str, band_num: int,
                           domain: str = "C", prefetched_files: list = None,
                           regions_only: bool = False) -> bool:
        """Process a native CMIP single-band product such as true Band 2 1km visible."""
        try:
            self._write_status({
                "source_id": source_id,
                "product_type": product_type,
                "stage": "checking",
                "task": f"{source_id} {product_type}",
            })
            sat_num = int(source_id.replace("goes", ""))
            dl_key = f"cmip_{sat_num}_{domain}"
            if dl_key not in self.goes_cmip_downloaders:
                self.goes_cmip_downloaders[dl_key] = GOESDownloader(
                    satellite=sat_num, product="ABI-L2-CMIP", domain=domain
                )
            downloader = self.goes_cmip_downloaders[dl_key]
            # Keep per-product cache dirs so parallel CMIP jobs for the same band
            # (e.g. `vis` and `vis_1km`) do not race on the same staging file.
            output_dir = SATELLITE_CACHE_DIR / source_id / "cmip" / f"C{band_num:02d}"
            output_dir.mkdir(parents=True, exist_ok=True)

            if prefetched_files is not None:
                files = prefetched_files
            else:
                files = downloader.list_available(
                    hours=self.loop_hours,
                    max_files=max(self.loop_frames_per_band * 20, 400),
                )
            if not files:
                return False

            band_tag = f"C{band_num:02d}"
            band_files = [f for f in files if f"_{band_tag}_" in f.filename or f"-M6{band_tag}_" in f.filename]
            if not band_files:
                return False

            rendered_count = 0

            for goes_file in band_files[:self.loop_frames_per_band]:
                if self._archive_deadline is not None and time.monotonic() >= self._archive_deadline:
                    break
                try:
                    parts = goes_file.filename.split("_")
                    s_part = parts[3]
                    year, doy = s_part[1:5], s_part[5:8]
                    hour, minute = s_part[8:10], s_part[10:12]
                    ts_filename = f"{source_id}_{product_type}_{year}{doy}_{hour}{minute}.png"
                    base_current = self._cmip_render_is_current(product_type, ts_filename)
                    missing_regions = self._missing_cmip_regional_renders(
                        product_type,
                        ts_filename,
                        source_id=source_id,
                    )

                    if regions_only:
                        if not missing_regions:
                            continue
                    elif base_current and not missing_regions:
                        continue

                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "stage": "downloading",
                        "task": f"{source_id} {product_type} download",
                        "filename": goes_file.filename,
                    })
                    path = downloader.download_file(goes_file, output_dir)
                    if not path:
                        continue

                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "stage": "rendering",
                        "task": f"{source_id} {product_type} render",
                    })
                    with load_dataset(path, variables=("CMI", "DQF", f"CMI_C{band_num:02d}", f"C{band_num:02d}")) as ds:
                        did_render = False
                        if not regions_only and not base_current:
                            result = self.sat_renderer.render_cmip_product(
                                ds, product_type, band_num, filename=ts_filename
                            )
                            did_render = result is not None

                        for region_name, region_bounds in missing_regions:
                            regional_filename = self._regional_filename(ts_filename, region_name)
                            result = self.sat_renderer.render_cmip_product(
                                ds,
                                product_type,
                                band_num,
                                filename=regional_filename,
                                region_name=region_name,
                                region_bounds=region_bounds,
                            )
                            did_render = did_render or result is not None

                    if did_render:
                        rendered_count += 1
                        self._refresh_latest_meta(source_id, product_type)
                except Exception as e:
                    logger.warning(f"Error processing {product_type} {goes_file.filename}: {e}")

            self._post_process_product(source_id, product_type, rendered_count)

            if rendered_count > 0:
                self.state["last_download"][f"{source_id}_{product_type}"] = \
                    datetime.now(timezone.utc).isoformat()
                logger.info(f"{_sat_tag(source_id, product_type)} Rendered {rendered_count} frames")
                self._write_status(None)
                return True
        except Exception as e:
            logger.warning(f"{_sat_tag(source_id, product_type)} Error: {e}")
        self._write_status(None)
        return False

    def process_mcmip_single_band(
        self,
        source_id: str,
        product_type: str,
        band_num: int,
        prefetched_files: list | None = None,
        high_res: bool = False,
        regions_only: bool = False,
    ) -> bool:
        """Render a single-band product from the consolidated MCMIP stream.

        NOAA's per-band L1b/CMIP feeds can lag or disappear by band while the
        matching MCMIP file is still current. Use MCMIP as the durable source
        for products that only need CMI values, optionally via the higher-res
        CMIP-style renderer for native IR products.
        """
        try:
            self._write_status({
                "source_id": source_id,
                "product_type": product_type,
                "stage": "checking",
                "task": f"{source_id} {product_type}",
            })
            sat_num = int(source_id.replace("goes", ""))
            dl_key = f"mcmip_{sat_num}"
            if dl_key not in self.goes_downloaders:
                self.goes_downloaders[dl_key] = GOESDownloader(
                    satellite=sat_num, product="ABI-L2-MCMIP", domain="C"
                )
            downloader = self.goes_downloaders[dl_key]
            output_dir = self._shared_composite_cache_dir(source_id)
            output_dir.mkdir(parents=True, exist_ok=True)

            if prefetched_files is not None:
                files = prefetched_files
            else:
                files = downloader.list_available(
                    hours=self.loop_hours,
                    max_files=max(self.loop_frames_per_band * 5, 100),
                )
            if not files:
                return False

            png_dir = self.sat_renderer.png_dir
            rendered_count = 0

            for goes_file in files[:self.loop_frames_per_band]:
                if self._archive_deadline is not None and time.monotonic() >= self._archive_deadline:
                    break
                try:
                    parts = goes_file.filename.split("_")
                    s_part = parts[3]
                    year, doy = s_part[1:5], s_part[5:8]
                    hour, minute = s_part[8:10], s_part[10:12]
                    iso_ts = f"{year}-{doy}T{hour}:{minute}:00Z"
                    ts_filename = f"{source_id}_{product_type}_{year}{doy}_{hour}{minute}.png"
                    base_exists = (png_dir / ts_filename).exists()
                    missing_regions = self._missing_regional_renders(product_type, ts_filename)

                    if regions_only:
                        if not missing_regions:
                            continue
                    elif base_exists and not missing_regions:
                        continue

                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "stage": "downloading",
                        "task": f"{source_id} {product_type} download",
                        "filename": goes_file.filename,
                    })
                    path = downloader.download_file(goes_file, output_dir)
                    if not path:
                        continue

                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "stage": "rendering",
                        "task": f"{source_id} {product_type} render",
                        "filename": Path(path).name,
                    })
                    with load_dataset(path, variables=("CMI", f"CMI_C{band_num:02d}", f"C{band_num:02d}", f"Rad{band_num}")) as ds:
                        did_render = False
                        if high_res:
                            result = None
                            if not regions_only and not base_exists:
                                result = self.sat_renderer.render_cmip_product(
                                    ds, product_type, band_num, filename=ts_filename
                                )
                                did_render = True
                        else:
                            result = None
                            if not regions_only and not base_exists:
                                result = self.sat_renderer.render_abi_product(
                                    ds, product_type, filename=ts_filename
                                )
                                did_render = True

                        for region_name, region_bounds in missing_regions:
                            regional_filename = self._regional_filename(ts_filename, region_name)
                            if high_res:
                                self.sat_renderer.render_cmip_product(
                                    ds,
                                    product_type,
                                    band_num,
                                    filename=regional_filename,
                                    region_name=region_name,
                                    region_bounds=region_bounds,
                                )
                                did_render = True
                            else:
                                self.sat_renderer.render_abi_product(
                                    ds,
                                    product_type,
                                    filename=regional_filename,
                                    region_name=region_name,
                                    region_bounds=region_bounds,
                                )
                                did_render = True

                    if did_render:
                        rendered_count += 1
                        self._refresh_latest_meta(source_id, product_type)
                except Exception as e:
                    logger.warning(f"Error processing {product_type} {goes_file.filename}: {e}")

            self._post_process_product(source_id, product_type, rendered_count)

            if rendered_count > 0:
                self.state["last_download"][f"{source_id}_{product_type}"] = \
                    datetime.now(timezone.utc).isoformat()
                logger.info(f"{_sat_tag(source_id, product_type)} Rendered {rendered_count} frames")
                self._write_status(None)
                return True
        except Exception as e:
            logger.warning(f"{_sat_tag(source_id, product_type)} Error: {e}")
        self._write_status(None)
        return False

    def process_rgb_composite(self, source_id: str, product_type: str, bands: list,
                               prefetched_files: list = None) -> bool:
        """Process an RGB composite (natural_color, day_cloud_phase, sandwich).

        Downloads MCMIP files (which contain all bands) and renders multi-band RGB.
        """
        try:
            self._write_status({
                "source_id": source_id,
                "product_type": product_type,
                "stage": "checking",
                "task": f"{source_id} {product_type}",
            })
            sat_num = int(source_id.replace("goes", ""))
            dl_key = f"mcmip_{sat_num}"
            if dl_key not in self.goes_downloaders:
                self.goes_downloaders[dl_key] = GOESDownloader(
                    satellite=sat_num, product="ABI-L2-MCMIP", domain="C"
                )
            downloader = self.goes_downloaders[dl_key]
            # Keep per-product cache dirs so parallel RGB jobs do not race on the
            # same temporary file names while downloading identical MCMIP files.
            output_dir = self._shared_composite_cache_dir(source_id)
            output_dir.mkdir(parents=True, exist_ok=True)

            if prefetched_files is not None:
                files = prefetched_files
            else:
                files = downloader.list_available(
                    hours=self.loop_hours,
                    max_files=max(self.loop_frames_per_band * 5, 100),
                )
            if not files:
                return False

            png_dir = self.sat_renderer.png_dir
            rendered_count = 0

            for goes_file in files[:self.loop_frames_per_band]:
                if self._archive_deadline is not None and time.monotonic() >= self._archive_deadline:
                    break
                try:
                    parts = goes_file.filename.split("_")
                    s_part = parts[3]
                    year, doy = s_part[1:5], s_part[5:8]
                    hour, minute = s_part[8:10], s_part[10:12]
                    iso_ts = f"{year}-{doy}T{hour}:{minute}:00Z"
                    ts_filename = f"{source_id}_{product_type}_{year}{doy}_{hour}{minute}.png"

                    if (png_dir / ts_filename).exists():
                        continue

                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "stage": "downloading",
                        "task": f"{source_id} {product_type} download",
                        "filename": goes_file.filename,
                    })
                    path = downloader.download_file(goes_file, output_dir)
                    if not path:
                        continue

                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "stage": "rendering",
                        "task": f"{source_id} {product_type} render",
                    })
                    with load_dataset(path, variables=("CMI_C01", "CMI_C02", "CMI_C03", "CMI_C05", "CMI_C13")) as ds:
                        if product_type == "natural_color":
                            self.sat_renderer.render_natural_color(ds, filename=ts_filename)
                        elif product_type == "day_cloud_phase":
                            self.sat_renderer.render_day_cloud_phase(ds, filename=ts_filename)
                        elif product_type == "sandwich":
                            self.sat_renderer.render_sandwich(ds, filename=ts_filename)
                        else:
                            raise ValueError(f"Unsupported RGB composite: {product_type}")
                        rendered_count += 1
                        self._refresh_latest_meta(source_id, product_type)
                except Exception as e:
                    logger.warning(f"Error processing {product_type} {goes_file.filename}: {e}")

            self._post_process_product(source_id, product_type, rendered_count)

            if rendered_count > 0:
                self.state["last_download"][f"{source_id}_{product_type}"] = \
                    datetime.now(timezone.utc).isoformat()
                logger.info(f"{_sat_tag(source_id, product_type)} Rendered {rendered_count} frames")
                self._write_status(None)
                return True
        except Exception as e:
            logger.warning(f"{_sat_tag(source_id, product_type)} Error: {e}")
        self._write_status(None)
        return False

    def process_mesoscale_band(self, source_id: str, product_type: str, band: str,
                                domain: str = "M1", prefetched_files: list = None) -> bool:
        """Process a mesoscale sector band (M1/M2 IR or Visible at 1-min updates)."""
        try:
            self._write_status({
                "source_id": source_id,
                "product_type": product_type,
                "stage": "checking",
                "task": f"{source_id} {product_type}",
            })
            sat_num = int(source_id.replace("goes", ""))
            downloader = self.get_downloader(sat_num, product="ABI-L2-MCMIP", domain=domain)
            output_dir = SATELLITE_CACHE_DIR / source_id / "mesoscale" / f"{domain}_{band}"
            output_dir.mkdir(parents=True, exist_ok=True)

            if prefetched_files is not None:
                files = prefetched_files
            else:
                retention_hours = self.config.get("products", {}).get(product_type, {}).get("retention_hours", 6)
                files = downloader.list_available(
                    hours=min(retention_hours, self.loop_hours),
                    max_files=max(self.loop_frames_per_band * 20, 400),
                )
            if not files:
                return False

            png_dir = self.sat_renderer.png_dir
            rendered_count = 0
            band_num = int(str(band).replace("C", ""))

            for goes_file in files[:self.loop_frames_per_band]:
                if self._archive_deadline is not None and time.monotonic() >= self._archive_deadline:
                    break
                try:
                    parts = goes_file.filename.split("_")
                    s_part = parts[3]
                    year, doy = s_part[1:5], s_part[5:8]
                    hour, minute = s_part[8:10], s_part[10:12]
                    iso_ts = f"{year}-{doy}T{hour}:{minute}:00Z"
                    ts_filename = f"{source_id}_{product_type}_{year}{doy}_{hour}{minute}.png"

                    if (png_dir / ts_filename).exists():
                        continue

                    path = downloader.download_file(goes_file, output_dir)
                    if not path:
                        continue

                    self._write_status({
                        "source_id": source_id,
                        "product_type": product_type,
                        "band": band,
                        "stage": "rendering",
                        "task": f"{source_id} {product_type} render",
                    })
                    with load_dataset(path, variables=("CMI", "DQF", f"CMI_C{band_num:02d}", f"C{band_num:02d}")) as ds:
                        result = self.sat_renderer.render_mesoscale_product(
                            ds, product_type, band_num, filename=ts_filename
                        )
                    if result:
                        rendered_count += 1
                        self._refresh_latest_meta(source_id, product_type)
                except Exception as e:
                    logger.warning(f"Error processing {product_type} {goes_file.filename}: {e}")

            self._post_process_product(source_id, product_type, rendered_count)

            if rendered_count > 0:
                self.state["last_download"][f"{source_id}_{product_type}"] = \
                    datetime.now(timezone.utc).isoformat()
                logger.info(f"{_sat_tag(source_id, product_type)} Rendered {rendered_count} frames")
                self._write_status(None)
                return True
        except Exception as e:
            logger.warning(f"{_sat_tag(source_id, product_type)} Error: {e}")
        self._write_status(None)
        return False

    # ------------------------------------------------------------------
    # Composite processing: GOES-18 (West) + GOES-19 (East)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_goes_timestamp(goes_file) -> tuple:
        """Extract (year, doy, hour, minute) from a GOES filename."""
        parts = goes_file.filename.split("_")
        s_part = parts[3]  # e.g. s20260780106177
        return s_part[1:5], s_part[5:8], s_part[8:10], s_part[10:12]

    @staticmethod
    def _match_frames_by_time(
        west_files: list,
        east_files: list,
        tolerance_minutes: int = 8,
        max_pairs: int = 36,
    ) -> list:
        """Pair GOES-West and GOES-East files by scan start time.

        Returns list of (west_file, east_file, year, doy, hour, minute) tuples,
        newest first.
        """
        def _to_minutes(parts):
            return int(parts[0]) * 525960 + int(parts[1]) * 1440 + int(parts[2]) * 60 + int(parts[3])

        east_by_time = {}
        for f in east_files:
            parts = f.filename.split("_")
            s = parts[3]
            key = _to_minutes((s[1:5], s[5:8], s[8:10], s[10:12]))
            east_by_time[key] = f

        pairs = []
        for wf in west_files:
            parts = wf.filename.split("_")
            s = parts[3]
            w_parts = (s[1:5], s[5:8], s[8:10], s[10:12])
            w_min = _to_minutes(w_parts)
            # Find closest east file within tolerance
            best_ef = None
            best_delta = tolerance_minutes + 1
            for e_min, ef in east_by_time.items():
                delta = abs(w_min - e_min)
                if delta < best_delta:
                    best_delta = delta
                    best_ef = ef
            if best_ef is not None and best_delta <= tolerance_minutes:
                pairs.append((wf, best_ef, w_parts[0], w_parts[1], w_parts[2], w_parts[3]))
                if len(pairs) >= max_pairs:
                    break
        return pairs

    def process_composite_mcmip_single_band(
        self,
        product_type: str,
        band_num: int,
        west_files: list,
        east_files: list,
        high_res: bool = False,
        render_regions: bool = True,
    ) -> bool:
        """Composite a single-band product from GOES-West + GOES-East MCMIP."""
        comp_cfg = self.config.get("compositing", {})
        output_source_id = comp_cfg.get("output_source_id", "goes19")
        blend_west = comp_cfg.get("blend_west_lon", -113.0)
        blend_east = comp_cfg.get("blend_east_lon", -100.0)
        tolerance = comp_cfg.get("time_tolerance_minutes", 8)
        west_source = comp_cfg.get("west_source", "goes18")
        east_source = comp_cfg.get("east_source", "goes19")

        try:
            self._write_status({
                "source_id": output_source_id,
                "product_type": product_type,
                "stage": "matching",
                "task": f"composite {product_type}",
            })

            pairs = self._match_frames_by_time(
                west_files, east_files,
                tolerance_minutes=tolerance,
                max_pairs=self.loop_frames_per_band,
            )
            if not pairs:
                logger.warning("[composite] No time-matched pairs for %s", product_type)
                self._write_status(None)
                return False

            cfg = None
            try:
                from .satellite_renderer import MCMIP_PRODUCT_CONFIG
                cfg = MCMIP_PRODUCT_CONFIG.get(product_type)
            except ImportError:
                pass

            png_dir = self.sat_renderer.png_dir
            rendered_count = 0
            west_cache_dir = self._shared_composite_cache_dir(west_source)
            east_cache_dir = self._shared_composite_cache_dir(east_source)
            west_cache_dir.mkdir(parents=True, exist_ok=True)
            east_cache_dir.mkdir(parents=True, exist_ok=True)

            west_sat_num = int(west_source.replace("goes", ""))
            east_sat_num = int(east_source.replace("goes", ""))
            w_dl_key = f"mcmip_{west_sat_num}"
            e_dl_key = f"mcmip_{east_sat_num}"
            if w_dl_key not in self.goes_downloaders:
                self.goes_downloaders[w_dl_key] = GOESDownloader(
                    satellite=west_sat_num, product="ABI-L2-MCMIP", domain="C"
                )
            if e_dl_key not in self.goes_downloaders:
                self.goes_downloaders[e_dl_key] = GOESDownloader(
                    satellite=east_sat_num, product="ABI-L2-MCMIP", domain="C"
                )
            w_downloader = self.goes_downloaders[w_dl_key]
            e_downloader = self.goes_downloaders[e_dl_key]

            for wf, ef, year, doy, hour, minute in pairs:
                if self._archive_deadline is not None and time.monotonic() >= self._archive_deadline:
                    break
                try:
                    iso_ts = f"{year}-{doy}T{hour}:{minute}:00Z"
                    ts_filename = f"{output_source_id}_{product_type}_{year}{doy}_{hour}{minute}.png"
                    base_exists = (png_dir / ts_filename).exists()
                    missing_regions = (
                        self._missing_regional_renders(product_type, ts_filename)
                        if render_regions else []
                    )

                    if base_exists and not missing_regions:
                        continue

                    self._write_status({
                        "source_id": output_source_id,
                        "product_type": product_type,
                        "stage": "downloading",
                        "task": f"composite {product_type} download",
                    })
                    w_path = w_downloader.download_file(wf, west_cache_dir)
                    e_path = e_downloader.download_file(ef, east_cache_dir)
                    if not w_path or not e_path:
                        continue

                    self._write_status({
                        "source_id": output_source_id,
                        "product_type": product_type,
                        "stage": "compositing",
                        "task": f"composite {product_type} render",
                    })

                    with load_dataset(w_path, variables=(f"CMI_C{band_num:02d}",)) as w_ds, load_dataset(e_path, variables=(f"CMI_C{band_num:02d}",)) as e_ds:
                        var_name = f"CMI_C{band_num:02d}"
                        w_raw = w_ds.get(var_name)
                        e_raw = e_ds.get(var_name)
                        if w_raw is None or e_raw is None:
                            continue

                        if cfg and cfg["type"] == "bt":
                            w_data = w_raw.values.astype(np.float32) - 273.15
                            e_data = e_raw.values.astype(np.float32) - 273.15
                        else:
                            w_data = w_raw.values.astype(np.float32) * 100.0
                            e_data = e_raw.values.astype(np.float32) * 100.0

                        cmap_name = cfg["cmap"] if cfg else "Greys_r"
                        vmin = cfg["vmin"] if cfg else -110
                        vmax = cfg["vmax"] if cfg else 50
                        gamma = cfg.get("gamma", 1.0) if cfg else 1.0
                        tone_lift = cfg.get("tone_lift", 0.0) if cfg else 0.0

                        bounds = None
                        did_render = False
                        if not base_exists:
                            output_path = png_dir / ts_filename
                            bounds = self.sat_renderer.render_composite(
                                w_data, w_ds, e_data, e_ds,
                                output_path,
                                cmap=cmap_name, vmin=vmin, vmax=vmax,
                                blend_west_lon=blend_west,
                                blend_east_lon=blend_east,
                                gamma=gamma,
                                tone_lift=tone_lift,
                                edge_ramp_cfg=cfg.get("edge_ramp") if cfg else None,
                            )
                            did_render = True

                        for region_name, region_bounds in missing_regions:
                            regional_filename = self._regional_filename(ts_filename, region_name)
                            regional_target_w = self._regional_target_width(product_type)
                            regional_bounds = self.sat_renderer.render_composite(
                                w_data,
                                w_ds,
                                e_data,
                                e_ds,
                                png_dir / regional_filename,
                                cmap=cmap_name,
                                vmin=vmin,
                                vmax=vmax,
                                blend_west_lon=blend_west,
                                blend_east_lon=blend_east,
                                target_w=regional_target_w,
                                gamma=gamma,
                                tone_lift=tone_lift,
                                edge_ramp_cfg=cfg.get("edge_ramp") if cfg else None,
                                crop_bounds=region_bounds,
                            )
                            regional_meta = {
                                "satellite": "G19",
                                "product": product_type,
                                "timestamp": iso_ts,
                                "bounds": regional_bounds,
                                "png_path": f"satellite/png/{regional_filename}",
                                "composite": True,
                                "region": region_name,
                            }
                            regional_meta_path = self.sat_renderer.meta_dir / regional_filename.replace(".png", ".json")
                            write_frame_meta(regional_meta_path, regional_meta)
                            did_render = True

                    if bounds:
                        # Use output satellite ID (G19) so API filters match
                        meta = {
                            "satellite": "G19",
                            "product": product_type,
                            "timestamp": iso_ts,
                            "bounds": bounds,
                            "png_path": f"satellite/png/{ts_filename}",
                            "composite": True,
                        }
                        meta_path = self.sat_renderer.meta_dir / ts_filename.replace(".png", ".json")
                        write_frame_meta(meta_path, meta)
                    if did_render:
                        rendered_count += 1
                        self._refresh_latest_meta(output_source_id, product_type)
                except Exception as e:
                    logger.warning("[composite] Error processing %s frame: %s", product_type, e)

            self._post_process_product(output_source_id, product_type, rendered_count)

            if rendered_count > 0:
                self.state["last_download"][f"{output_source_id}_{product_type}"] = \
                    datetime.now(timezone.utc).isoformat()
                logger.info("[G18+G19-%s] Composited %d frames",
                            _PRODUCT_LABELS.get(product_type, product_type), rendered_count)
                self._write_status(None)
                return True
        except Exception as e:
            logger.warning("[composite] %s error: %s", product_type, e)
        self._write_status(None)
        return False

    def process_composite_rgb(
        self,
        product_type: str,
        west_files: list,
        east_files: list,
    ) -> bool:
        """Composite an RGB product from two satellites."""
        comp_cfg = self.config.get("compositing", {})
        output_source_id = comp_cfg.get("output_source_id", "goes19")
        blend_west = comp_cfg.get("blend_west_lon", -113.0)
        blend_east = comp_cfg.get("blend_east_lon", -100.0)
        tolerance = comp_cfg.get("time_tolerance_minutes", 8)
        west_source = comp_cfg.get("west_source", "goes18")
        east_source = comp_cfg.get("east_source", "goes19")

        try:
            self._write_status({
                "source_id": output_source_id,
                "product_type": product_type,
                "stage": "matching",
                "task": f"composite {product_type}",
            })

            pairs = self._match_frames_by_time(
                west_files, east_files,
                tolerance_minutes=tolerance,
                max_pairs=self.loop_frames_per_band,
            )
            if not pairs:
                logger.warning("[composite] No time-matched pairs for %s", product_type)
                self._write_status(None)
                return False

            png_dir = self.sat_renderer.png_dir
            rendered_count = 0
            west_cache_dir = self._shared_composite_cache_dir(west_source)
            east_cache_dir = self._shared_composite_cache_dir(east_source)
            west_cache_dir.mkdir(parents=True, exist_ok=True)
            east_cache_dir.mkdir(parents=True, exist_ok=True)

            west_sat_num = int(west_source.replace("goes", ""))
            east_sat_num = int(east_source.replace("goes", ""))
            w_dl_key = f"mcmip_{west_sat_num}"
            e_dl_key = f"mcmip_{east_sat_num}"
            if w_dl_key not in self.goes_downloaders:
                self.goes_downloaders[w_dl_key] = GOESDownloader(
                    satellite=west_sat_num, product="ABI-L2-MCMIP", domain="C"
                )
            if e_dl_key not in self.goes_downloaders:
                self.goes_downloaders[e_dl_key] = GOESDownloader(
                    satellite=east_sat_num, product="ABI-L2-MCMIP", domain="C"
                )
            w_downloader = self.goes_downloaders[w_dl_key]
            e_downloader = self.goes_downloaders[e_dl_key]

            for wf, ef, year, doy, hour, minute in pairs:
                if self._archive_deadline is not None and time.monotonic() >= self._archive_deadline:
                    break
                try:
                    iso_ts = f"{year}-{doy}T{hour}:{minute}:00Z"
                    ts_filename = f"{output_source_id}_{product_type}_{year}{doy}_{hour}{minute}.png"

                    if (png_dir / ts_filename).exists():
                        continue

                    self._write_status({
                        "source_id": output_source_id,
                        "product_type": product_type,
                        "stage": "downloading",
                        "task": f"composite {product_type} download",
                    })
                    w_path = w_downloader.download_file(wf, west_cache_dir)
                    e_path = e_downloader.download_file(ef, east_cache_dir)
                    if not w_path or not e_path:
                        continue

                    self._write_status({
                        "source_id": output_source_id,
                        "product_type": product_type,
                        "stage": "compositing",
                        "task": f"composite {product_type} render",
                    })

                    with load_dataset(w_path, variables=("CMI_C01", "CMI_C02", "CMI_C03", "CMI_C05", "CMI_C13")) as w_ds, load_dataset(e_path, variables=("CMI_C01", "CMI_C02", "CMI_C03", "CMI_C05", "CMI_C13")) as e_ds:
                        if product_type == "natural_color":
                            w_rgb = self._extract_natural_color_rgb(w_ds)
                            e_rgb = self._extract_natural_color_rgb(e_ds)
                        elif product_type == "day_cloud_phase":
                            w_rgb = self._extract_day_cloud_phase_rgb(w_ds)
                            e_rgb = self._extract_day_cloud_phase_rgb(e_ds)
                        elif product_type == "sandwich":
                            w_rgb = self._extract_sandwich_rgb(w_ds)
                            e_rgb = self._extract_sandwich_rgb(e_ds)
                        else:
                            continue

                        if w_rgb is None or e_rgb is None:
                            continue

                        output_path = png_dir / ts_filename
                        bounds = self.sat_renderer.render_composite_rgb(
                            w_rgb, w_ds, e_rgb, e_ds,
                            output_path,
                            blend_west_lon=blend_west,
                            blend_east_lon=blend_east,
                        )

                    if bounds:
                        meta = {
                            "satellite": "G19",
                            "product": product_type,
                            "timestamp": iso_ts,
                            "bounds": bounds,
                            "png_path": f"satellite/png/{ts_filename}",
                            "composite": True,
                        }
                        meta_path = self.sat_renderer.meta_dir / ts_filename.replace(".png", ".json")
                        write_frame_meta(meta_path, meta)
                        rendered_count += 1
                        self._refresh_latest_meta(output_source_id, product_type)
                except Exception as e:
                    logger.warning("[composite] Error processing %s RGB frame: %s", product_type, e)

            self._post_process_product(output_source_id, product_type, rendered_count)

            if rendered_count > 0:
                self.state["last_download"][f"{output_source_id}_{product_type}"] = \
                    datetime.now(timezone.utc).isoformat()
                logger.info("[G18+G19-%s] Composited %d frames",
                            _PRODUCT_LABELS.get(product_type, product_type), rendered_count)
                self._write_status(None)
                return True
        except Exception as e:
            logger.warning("[composite] %s RGB error: %s", product_type, e)
        self._write_status(None)
        return False

    def _extract_natural_color_rgb(self, ds: xr.Dataset) -> np.ndarray | None:
        """Extract natural color RGB array from MCMIP dataset (H, W, 3), 0-1 float."""
        try:
            red = self.sat_renderer._get_brightness_temp(ds, 2)
            green = self.sat_renderer._get_brightness_temp(ds, 3)
            blue = self.sat_renderer._get_brightness_temp(ds, 1)
            if any(x is None for x in [red, green, blue]):
                return None
            r = np.clip(red.values.astype(np.float32), 0, 1)
            g = np.clip(green.values.astype(np.float32), 0, 1)
            b = np.clip(blue.values.astype(np.float32), 0, 1)
            g_corrected = np.clip(0.45 * r + 0.1 * g + 0.45 * b, 0, 1)
            rgb = np.stack([r, g_corrected, b], axis=-1)
            rgb = np.power(np.clip(rgb, 0, 1), 0.5)
            return rgb
        except Exception as e:
            logger.warning("Failed to extract natural color RGB: %s", e)
            return None

    def _extract_day_cloud_phase_rgb(self, ds: xr.Dataset) -> np.ndarray | None:
        """Extract day cloud phase RGB array from MCMIP dataset (H, W, 3), 0-1 float."""
        try:
            b2 = self.sat_renderer._get_brightness_temp(ds, 2)
            b5 = self.sat_renderer._get_brightness_temp(ds, 5)
            b13 = self.sat_renderer._get_brightness_temp(ds, 13)
            if any(x is None for x in [b2, b5, b13]):
                return None
            return self.sat_renderer._build_day_cloud_phase_rgb(
                b2.values.astype(np.float32),
                b5.values.astype(np.float32),
                b13.values.astype(np.float32),
            )
        except Exception as e:
            logger.warning("Failed to extract day cloud phase RGB: %s", e)
            return None

    def _extract_sandwich_rgb(self, ds: xr.Dataset) -> np.ndarray | None:
        """Extract VIS/IR Sandwich RGB array from MCMIP dataset."""
        try:
            b2 = self.sat_renderer._get_brightness_temp(ds, 2)
            b13 = self.sat_renderer._get_brightness_temp(ds, 13)
            if b2 is None or b13 is None:
                return None
            return self.sat_renderer._build_sandwich_rgb(
                b2.values.astype(np.float32),
                b13.values.astype(np.float32),
            )
        except Exception as e:
            logger.warning("Failed to extract sandwich RGB: %s", e)
            return None

    def run_cycle(self):
        """Run one cycle - get CURRENT data only, no backlog.

        Optimisations vs original:
        - Single shared S3 listing per source (16× fewer round-trips).
        - Shared listings reused across product families where possible.
        - Product families rendered in parallel via ThreadPoolExecutor.
        """
        if not self.enabled:
            return

        cycle_start = time.time()
        now = cycle_start
        archive_mode = (
            (now - self._last_archive_refresh) >= self.archive_refresh_interval_seconds
        )
        frame_limit = (
            self.archive_frames_per_cycle if archive_mode else self.latest_frames_per_cycle
        )
        self.loop_frames_per_band = frame_limit
        self._cycle_do_maintenance = archive_mode
        logger.info(
            "=== Satellite cycle start (%s, frame_limit=%d) ===",
            "archive" if archive_mode else "latest",
            frame_limit,
        )

        total_rendered = 0

        # Visible/reflectance products are only useful during daylight.
        # Skip them outside 10z–02z to save processing cycles overnight.
        _VIS_PRODUCTS = {
            "vis", "vis_blue", "vis_1km", "veggie", "snow_ice", "cirrus",
            "cloud_particle", "natural_color", "day_cloud_phase",
            "sandwich", "m1_vis_1km", "m2_vis_1km",
        }
        utc_hour = datetime.now(timezone.utc).hour
        _vis_enabled = (utc_hour >= 10 or utc_hour < 2)
        if not _vis_enabled:
            logger.debug("Visible products disabled (UTC hour %02d outside 10z-02z)", utc_hour)

        # All 16 ABI bands in priority order (key products first)
        mcmip_single_band_products = [
            ("vis_blue", 1, False),
            ("ir", 13, False),
            ("ir_14", 14, False),
            ("cloud_top", 11, False),
            ("ozone", 12, False),
            ("ir_dirty", 15, False),
            ("co2", 16, False),
            ("wv", 9, False),
            ("vis", 2, False),
            ("cloud_particle", 6, False),
            ("sw_ir", 7, False),
            ("upper_wv", 8, False),
            ("lower_wv", 10, False),
            ("veggie", 3, False),
            ("snow_ice", 5, False),
            ("cirrus", 4, False),
            ("ir_native", 13, True),
        ]
        core_bands = [
            ("vis_blue",      "C01", "goes19_vis_blue_latest.png"),
            ("cloud_particle","C06", "goes19_cloud_particle_latest.png"),
            ("cloud_top",     "C11", "goes19_cloud_top_latest.png"),
            ("ozone",         "C12", "goes19_ozone_latest.png"),
            ("ir_dirty",      "C15", "goes19_ir_dirty_latest.png"),
            ("co2",           "C16", "goes19_co2_latest.png"),
        ]
        rgb_products = [
            ("natural_color", [1, 2, 3]),
            ("day_cloud_phase", [2, 5, 13]),
            ("sandwich", [2, 13]),
        ]
        derived_products = [
            ("fire", "ABI-L2-FDC", "F"),
            ("pwat", "ABI-L2-TPW", "F"),
            ("sst", "ABI-L2-SST", "F"),
            ("rain_rate", "ABI-L2-RRQPE", "F"),
            ("stability", "ABI-L2-DSI", "F"),
        ]
        cmip_products = [
            ("vis_1km", 2, "C"),
        ]
        mesoscale_products = [
            ("m1_ir_1km", "C13", "M1"),
            ("m2_ir_1km", "C13", "M2"),
            ("m1_vis_1km", "C02", "M1"),
            ("m2_vis_1km", "C02", "M2"),
        ]

        if not _vis_enabled:
            mcmip_single_band_products = [
                p for p in mcmip_single_band_products if p[0] not in _VIS_PRODUCTS
            ]
            core_bands = [
                p for p in core_bands if p[0] not in _VIS_PRODUCTS
            ]
            rgb_products = [
                p for p in rgb_products if p[0] not in _VIS_PRODUCTS
            ]
            cmip_products = [
                p for p in cmip_products if p[0] not in _VIS_PRODUCTS
            ]
            mesoscale_products = [
                p for p in mesoscale_products if p[0] not in _VIS_PRODUCTS
            ]

        # Compositing configuration
        comp_cfg = self.config.get("compositing", {})
        compositing_enabled = comp_cfg.get("enabled", False)
        comp_products = set(comp_cfg.get("products", []))
        comp_output_id = comp_cfg.get("output_source_id", "goes19")
        comp_west_source = comp_cfg.get("west_source", "goes18")
        comp_east_source = comp_cfg.get("east_source", "goes19")
        region_source_ids = list(dict.fromkeys(
            str(cfg.get("source") or comp_output_id)
            for cfg in self.config.get("regions", {}).values()
            if cfg.get("enabled", True)
        ))

        # When compositing, only iterate the output satellite for single-source
        # products; composite products are handled separately.
        if compositing_enabled:
            single_source_ids = list(dict.fromkeys([comp_output_id, *region_source_ids]))
        else:
            single_source_ids = list(self.config["sources"].keys())

        try:
            # ---- Composite jobs: fetch MCMIP listings from both satellites ----
            shared_west_mcmip = []
            shared_east_mcmip = []
            if compositing_enabled:
                west_sat_num = int(comp_west_source.replace("goes", ""))
                east_sat_num = int(comp_east_source.replace("goes", ""))
                w_mcmip_key = f"mcmip_{west_sat_num}"
                e_mcmip_key = f"mcmip_{east_sat_num}"
                if w_mcmip_key not in self.goes_downloaders:
                    self.goes_downloaders[w_mcmip_key] = GOESDownloader(
                        satellite=west_sat_num, product="ABI-L2-MCMIP", domain="C"
                    )
                if e_mcmip_key not in self.goes_downloaders:
                    self.goes_downloaders[e_mcmip_key] = GOESDownloader(
                        satellite=east_sat_num, product="ABI-L2-MCMIP", domain="C"
                    )
                logger.info("[composite] Fetching MCMIP listings for G%d + G%d...",
                            west_sat_num, east_sat_num)
                try:
                    shared_west_mcmip = self.goes_downloaders[w_mcmip_key].list_available(
                        hours=self.loop_hours,
                        max_files=max(self.archive_frames_per_cycle * 2, 24),
                    )
                except Exception as exc:
                    logger.warning("[composite] West MCMIP listing failed: %s", exc)
                try:
                    shared_east_mcmip = self.goes_downloaders[e_mcmip_key].list_available(
                        hours=self.loop_hours,
                        max_files=max(self.archive_frames_per_cycle * 2, 24),
                    )
                except Exception as exc:
                    logger.warning("[composite] East MCMIP listing failed: %s", exc)

                if shared_west_mcmip and shared_east_mcmip:
                    logger.info("[composite] West: %d files, East: %d files",
                                len(shared_west_mcmip), len(shared_east_mcmip))
                else:
                    logger.warning("[composite] Missing listings, composite products will fall back to single-satellite")

            for source_id in single_source_ids:
                sat_num = int(source_id.replace('goes', ''))
                l1b_downloader = self.get_downloader(sat_num, product="ABI-L1b-Rad", domain="C")
                mcmip_key = f"mcmip_{sat_num}"
                if mcmip_key not in self.goes_downloaders:
                    self.goes_downloaders[mcmip_key] = GOESDownloader(
                        satellite=sat_num, product="ABI-L2-MCMIP", domain="C"
                    )
                mcmip_downloader = self.goes_downloaders[mcmip_key]
                cmip_key = f"cmip_{sat_num}_C"
                if cmip_key not in self.goes_cmip_downloaders:
                    self.goes_cmip_downloaders[cmip_key] = GOESDownloader(
                        satellite=sat_num, product="ABI-L2-CMIP", domain="C"
                    )
                cmip_downloader = self.goes_cmip_downloaders[cmip_key]

                logger.info("[%s] Fetching shared satellite listings...", source_id)
                try:
                    l1b_min_files = 16 * self.archive_frames_per_cycle * 2
                    shared_l1b_files = l1b_downloader.list_available(
                        hours=self.loop_hours,
                        max_files=max(self.loop_frames_per_band * 20, l1b_min_files),
                    )
                except Exception as exc:
                    logger.warning("[%s] L1b listing failed: %s", source_id, exc)
                    shared_l1b_files = []

                try:
                    shared_mcmip_files = (
                        shared_east_mcmip if source_id == comp_east_source else
                        shared_west_mcmip if source_id == comp_west_source else []
                    ) if compositing_enabled else []
                    if not shared_mcmip_files:
                        shared_mcmip_files = mcmip_downloader.list_available(
                            hours=self.loop_hours,
                            max_files=max(self.archive_frames_per_cycle * 2, 24),
                        )
                except Exception as exc:
                    logger.warning("[%s] MCMIP listing failed: %s", source_id, exc)
                    shared_mcmip_files = []

                try:
                    cmip_min_files = 16 * self.archive_frames_per_cycle * 2
                    shared_cmip_files = cmip_downloader.list_available(
                        hours=self.loop_hours,
                        max_files=max(self.loop_frames_per_band * 20, cmip_min_files),
                    )
                except Exception as exc:
                    logger.warning("[%s] CMIP listing failed: %s", source_id, exc)
                    shared_cmip_files = []

                # Prefetch mesoscale domain listings
                meso_max_files = max(self.archive_frames_per_cycle * 2, 24)
                meso_hours = min(
                    self.config.get("products", {}).get("m1_ir_1km", {}).get("retention_hours", 6),
                    self.loop_hours,
                )
                try:
                    m1_downloader = self.get_downloader(sat_num, product="ABI-L2-MCMIP", domain="M1")
                    shared_m1_files = m1_downloader.list_available(hours=meso_hours, max_files=meso_max_files)
                except Exception as exc:
                    logger.warning("[%s] M1 listing failed: %s", source_id, exc)
                    shared_m1_files = []
                try:
                    m2_downloader = self.get_downloader(sat_num, product="ABI-L2-MCMIP", domain="M2")
                    shared_m2_files = m2_downloader.list_available(hours=meso_hours, max_files=meso_max_files)
                except Exception as exc:
                    logger.warning("[%s] M2 listing failed: %s", source_id, exc)
                    shared_m2_files = []

                if not shared_l1b_files and not shared_mcmip_files and not shared_cmip_files:
                    logger.warning("[%s] No shared satellite listings available", source_id)

                is_auxiliary_regional_source = compositing_enabled and source_id != comp_output_id
                jobs = []

                regional_products = {
                    product_type for product_type, _, _ in mcmip_single_band_products
                    if self._configured_regions_for_product(product_type)
                }

                can_composite = (compositing_enabled and shared_west_mcmip and shared_east_mcmip)
                for product_type, band_num, high_res in mcmip_single_band_products:
                    region_products_for_source = self._configured_regions_for_product(
                        product_type,
                        source_id=source_id,
                    )

                    if can_composite and product_type in comp_products and source_id == comp_output_id:
                        jobs.append((
                            f"comp_{product_type}",
                            self.process_composite_mcmip_single_band,
                            (
                                product_type,
                                band_num,
                                shared_west_mcmip,
                                shared_east_mcmip,
                                high_res,
                                False,
                            ),
                        ))
                        if region_products_for_source:
                            jobs.append((
                                f"{product_type}_regional_{source_id}",
                                self.process_mcmip_single_band,
                                (
                                    source_id,
                                    product_type,
                                    band_num,
                                    shared_mcmip_files or None,
                                    high_res,
                                    True,
                                ),
                            ))
                    elif can_composite and product_type in comp_products:
                        if region_products_for_source:
                            jobs.append((
                                f"{product_type}_regional_{source_id}",
                                self.process_mcmip_single_band,
                                (
                                    source_id,
                                    product_type,
                                    band_num,
                                    shared_mcmip_files or None,
                                    high_res,
                                    True,
                                ),
                            ))
                    else:
                        jobs.append((
                            product_type,
                            self.process_mcmip_single_band,
                            (
                                source_id,
                                product_type,
                                band_num,
                                shared_mcmip_files or None,
                                high_res,
                                False,
                            ),
                        ))

                for product_type, band_num, domain in cmip_products:
                    region_products_for_source = self._configured_regions_for_product(
                        product_type,
                        source_id=source_id,
                    )
                    if is_auxiliary_regional_source and not region_products_for_source:
                        continue
                    job_name = (
                        f"{product_type}_regional_{source_id}"
                        if is_auxiliary_regional_source
                        else product_type
                    )
                    jobs.append((
                        job_name,
                        self.process_cmip_band,
                        (
                            source_id,
                            product_type,
                            band_num,
                            domain,
                            shared_cmip_files or None,
                            is_auxiliary_regional_source,
                        ),
                    ))

                # RGB composites
                if not is_auxiliary_regional_source:
                    for product_type, bands in rgb_products:
                        if can_composite and product_type in comp_products:
                            jobs.append((
                                f"comp_{product_type}",
                                self.process_composite_rgb,
                                (product_type, shared_west_mcmip, shared_east_mcmip),
                            ))
                        else:
                            jobs.append((
                                product_type,
                                self.process_rgb_composite,
                                (source_id, product_type, bands, shared_mcmip_files or None),
                            ))

                    jobs.extend(
                        (
                            product_type,
                            self.process_mesoscale_band,
                            (source_id, product_type, band, domain,
                             shared_m1_files if domain == "M1" else shared_m2_files),
                        )
                        for product_type, band, domain in mesoscale_products
                    )
                    effective_core_bands = core_bands
                    if compositing_enabled:
                        effective_core_bands = [
                            item for item in core_bands if item[0] not in comp_products
                        ]

                    if shared_l1b_files:
                        jobs.extend(
                            (
                                product_type,
                                self.process_l1b_band,
                                (source_id, product_type, band, filename, shared_l1b_files),
                            )
                            for product_type, band, filename in effective_core_bands
                        )
                    else:
                        jobs.extend(
                            (
                                product_type,
                                self.process_l1b_band,
                                (source_id, product_type, band, filename),
                            )
                            for product_type, band, filename in effective_core_bands
                        )
                l2_frame_limit = max(
                    1,
                    min(
                        self.loop_frames_per_band,
                        int(self.config.get("render", {}).get(
                            "l2_frames_per_cycle",
                            self.latest_frames_per_cycle,
                        )),
                    ),
                )
                jobs.extend(
                    (
                        product_type,
                        self.process_l2_product,
                        (source_id, product_type, product_code, domain, None, l2_frame_limit),
                    )
                    for product_type, product_code, domain in derived_products
                )
                logger.info("[%s] Dispatching %d satellite jobs (%d composite)",
                            source_id, len(jobs),
                            sum(1 for j in jobs if j[0].startswith("comp_")))
                jobs.sort(key=lambda job: self._job_priority(job[0]))
                total_rendered += self._run_job_phase(source_id, jobs, 1, archive=False)
                if archive_mode:
                    cursor = self._archive_cursors.get(source_id, 0) % max(1, len(jobs))
                    archive_jobs = jobs[cursor:] + jobs[:cursor]
                    total_rendered += self._run_job_phase(
                        source_id, archive_jobs, self.archive_frames_per_cycle, archive=True,
                    )
                    self._archive_cursors[source_id] = (cursor + self._archive_jobs_attempted) % max(1, len(jobs))

        finally:
            self.loop_frames_per_band = self._configured_loop_frames_per_band
            self._cycle_do_maintenance = False
            self._archive_deadline = None

        if archive_mode:
            self._last_archive_refresh = cycle_start

        if self.cleanup_in_process and (now - self._last_cleanup) >= self.cleanup_interval_seconds:
            self.cleanup_old_data()
            self._last_cleanup = time.time()

        # Self-heal: external cleanup (cron) can delete frames behind the
        # index's back; resync with a full rescan on a slow cadence.
        if (time.time() - self.frame_index.last_rebuild) >= self._index_rebuild_interval:
            try:
                self.frame_index.rebuild()
            except Exception as exc:
                logger.warning("Frame index rebuild failed: %s", exc)

        self.frame_index.flush_manifests()
        _release_memory()
        self._trim_memory_if_needed("cycle")
        self.save_state()
        elapsed = time.time() - cycle_start
        logger.info("=== Satellite cycle complete: %d renders, %.1fs ===",
                    total_rendered, elapsed)

    @staticmethod
    def _job_priority(name: str) -> int:
        if name.startswith(("m1_", "m2_")):
            return 0
        product = name.removeprefix("comp_").split("_regional_")[0]
        return 1 if product in {"ir", "ir_native", "wv", "vis", "vis_1km"} else 2

    def _run_job_phase(self, source_id: str, jobs: list, frames: int, archive: bool) -> int:
        """Publish latest frames first; give archive work a bounded time slice."""
        self.loop_frames_per_band = frames
        self._cycle_do_maintenance = archive
        self._archive_deadline = time.monotonic() + self.archive_budget_seconds if archive else None
        self._archive_jobs_attempted = 0
        rendered = 0
        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        try:
            for start in range(0, len(jobs), self.job_batch_size):
                if self._archive_deadline is not None and time.monotonic() >= self._archive_deadline:
                    break
                batch = jobs[start:start + self.job_batch_size]
                self._archive_jobs_attempted += len(batch)
                futures = {pool.submit(fn, *args): name for name, fn, args in batch}
                done, pending = wait(futures, timeout=self.job_timeout_seconds)
                for future in done:
                    try:
                        rendered += bool(future.result())
                    except Exception as exc:
                        logger.warning("[%s] %s raised: %s", source_id, futures[future], exc)
                if pending:
                    logger.error("[%s] Satellite phase timed out waiting on: %s", source_id,
                                 ", ".join(sorted(futures[f] for f in pending)))
                    for future in pending:
                        future.cancel()
                    break
        finally:
            # Running threads cannot be canceled. Drain before changing frame
            # limits or starting another phase so old jobs never accumulate.
            pool.shutdown(wait=True, cancel_futures=True)
            self._archive_deadline = None
            _release_memory()
            self._trim_memory_if_needed(f"{source_id} phase")
        return rendered

    def run_continuous(self, interval_seconds: int = 30):
        """Run scheduler continuously - 30 second cycles for fresh data."""
        logger.info(f"Starting satellite scheduler (CURRENT DATA ONLY, {interval_seconds}s interval)")
        
        while True:
            try:
                started = time.monotonic()
                self.run_cycle()
                time.sleep(max(1, interval_seconds - (time.monotonic() - started)))
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                time.sleep(10)


def main():
    """Entry point."""
    import sys
    import logging.handlers
    
    sys.stdout.reconfigure(line_buffering=True)
    
    log_dir = DATA_DIR.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_dir / "satellite.log", maxBytes=10*1024*1024, backupCount=3
        )
    ]
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
        handlers=handlers
    )
    
    scheduler = SatelliteScheduler()
    scheduler.run_continuous(interval_seconds=30)


if __name__ == "__main__":
    main()
