"""Unified pipeline scheduler — single process, all models, one render pool.

Key design:
  - Background DownloadManager fetches data for ALL models continuously
  - Priority waterfall: render workers focus on highest-priority model first,
    overflow to lower priorities only when higher has no ready work
  - Workers finish current forecast hour, then reassign by priority
  - Resume: skips fxx where all expected PNGs already exist on disk

Run:  python3 -m pipeline.unified_scheduler
"""

from __future__ import annotations

import calendar
import ctypes
import fcntl
import gc
import hashlib
import json
import logging
import logging.handlers
import multiprocessing as mp
import os
import queue
import re
import resource
import shutil
import sys
import threading
import time
import warnings
import numpy as np
from concurrent.futures import CancelledError, ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from herbie import Herbie

from .cleanup import (
    check_memory_pressure,
    check_memory_critical,
    clear_all_caches,
    MEMORY_LOWPRI_SHED_THRESHOLD_PCT,
    MEMORY_LOWPRI_KILL_ALL_PCT,
)
from .config import (
    GRIB_CACHE_DIR,
    LOG_DIR,
    POLL_INTERVAL_SECONDS,
    RENDERED_DIR,
)
from .palettes import CURRENT_PALETTE_VERSION
from .processor import SNOW_PRODUCTS
from .swath import update_running_max, finalize_swath
from .resource_budget import RenderAdmission, process_group_pss, render_processes_for_backlog
from .source_circuit import SOURCE_CIRCUIT
from .render_cost import RenderCosts, balanced_chunks

# Height fields used as hidden contour dependencies for combo products.
_HEIGHT_OVERLAY_DEPENDENCIES = {
    "temp_925mb": "gh_925mb",
    "rh_925mb": "gh_925mb",
    "wind_925mb": "gh_925mb",
    "temp_850mb": "gh_850mb",
    "dewpoint_850mb": "gh_850mb",
    "rh_850mb": "gh_850mb",
    "wind_850mb": "gh_850mb",
    "temp_700mb": "gh_700mb",
    "rh_700mb": "gh_700mb",
    "omega_700mb": "gh_700mb",
    "wind_700mb": "gh_700mb",
    "temp_500mb": "gh_500mb",
    "rh_500mb": "gh_500mb",
    "vort_500mb": "gh_500mb",
    "wind_500mb": "gh_500mb",
    "temp_300mb": "gh_300mb",
    "wind_300mb": "gh_300mb",
    "temp_250mb": "gh_250mb",
    "wind_250mb": "gh_250mb",
    "temp_200mb": "gh_200mb",
    "wind_200mb": "gh_200mb",
}

_REQUESTS_TIMEOUT_PATCH_LOCK = threading.Lock()
_REQUESTS_TIMEOUT_PATCHED = False

# Model constants
from .models.hrrr import (
    COMPOSITE_PARAMS as HRRR_COMPOSITES,
    DERIVED_PARAMS as HRRR_DERIVED,
    PARAM_SEARCH_STRINGS as HRRR_SEARCH,
    PRS_SEARCH_STRINGS as HRRR_PRS,
    SOURCES_PRIORITY as HRRR_SOURCES,
    get_run_fxx as _hrrr_fxx,
)
from .models.nam3km import (
    COMPOSITE_PARAMS as NAM3KM_COMPOSITES,
    DERIVED_PARAMS as NAM3KM_DERIVED,
    HERBIE_MODEL as NAM3KM_HERBIE_MODEL,
    HERBIE_PRODUCT as NAM3KM_HERBIE_PRODUCT,
    PARAM_SEARCH_STRINGS as NAM3KM_SEARCH,
    SOURCES_PRIORITY as NAM3KM_SOURCES,
    get_run_fxx as _nam3km_fxx,
)
from .models.nam import (
    DERIVED_PARAMS as NAM_DERIVED,
    PARAM_SEARCH_STRINGS as NAM_SEARCH,
    SOURCES_PRIORITY as NAM_SOURCES,
    get_run_fxx as _nam_fxx,
)
from .models.ecmwf import (
    DERIVED_PARAMS as IFS_DERIVED,
    PARAM_SEARCH_STRINGS as IFS_SEARCH,
    SOURCES_PRIORITY as IFS_SOURCES,
    get_run_fxx as _ifs_fxx,
)
from .models.gfs import (
    COMPOSITE_PARAMS as GFS_COMPOSITES,
    DERIVED_PARAMS as GFS_DERIVED,
    PARAM_SEARCH_STRINGS as GFS_SEARCH,
    PRS_SEARCH_STRINGS as GFS_PRS,
    SOURCES_PRIORITY as GFS_SOURCES,
    get_run_fxx as _gfs_fxx,
)

from .models.rap import (
    DERIVED_PARAMS as RAP_DERIVED,
    PARAM_SEARCH_STRINGS as RAP_SEARCH,
    SOURCES_PRIORITY as RAP_SOURCES,
    get_run_fxx as _rap_fxx,
)
from .models.gdps import (
    DERIVED_PARAMS as GDPS_DERIVED,
    PARAM_SEARCH_STRINGS as GDPS_SEARCH,
    SOURCES_PRIORITY as GDPS_SOURCES,
    get_run_fxx as _gdps_fxx,
)


def _ensure_requests_timeout_patch() -> None:
    """Install a default requests timeout once for Herbie HTTP calls."""
    global _REQUESTS_TIMEOUT_PATCHED
    if _REQUESTS_TIMEOUT_PATCHED:
        return

    with _REQUESTS_TIMEOUT_PATCH_LOCK:
        if _REQUESTS_TIMEOUT_PATCHED:
            return

        import requests

        original_send = requests.adapters.HTTPAdapter.send

        def _timeout_send(self_adapter, request, **kwargs):
            kwargs.setdefault(
                "timeout",
                (SOURCE_PROBE_TIMEOUT_SECONDS, DOWNLOAD_TIMEOUT),
            )
            return original_send(self_adapter, request, **kwargs)

        requests.adapters.HTTPAdapter.send = _timeout_send
        _REQUESTS_TIMEOUT_PATCHED = True
from .models.rtma import (
    DERIVED_PARAMS as RTMA_DERIVED,
    PARAM_SEARCH_STRINGS as RTMA_SEARCH,
    SOURCES_PRIORITY as RTMA_SOURCES,
    get_run_fxx as _rtma_fxx,
)
from .models.href import (
    DERIVED_PARAMS as HREF_DERIVED,
    PARAM_SEARCH_STRINGS as HREF_SEARCH,
    MEAN_PARAM_SEARCH_STRINGS as HREF_MEAN_SEARCH,
    PROB_PARAM_SEARCH_STRINGS as HREF_PROB_SEARCH,
    SOURCES_PRIORITY as HREF_SOURCES,
    get_run_fxx as _href_fxx,
)
from .models.rrfs import (
    DERIVED_PARAMS as RRFS_DERIVED,
    PARAM_SEARCH_STRINGS as RRFS_SEARCH,
    SOURCES_PRIORITY as RRFS_SOURCES,
    get_run_fxx as _rrfs_fxx,
)
from .models.gefs import (
    DERIVED_PARAMS as GEFS_DERIVED,
    PARAM_SEARCH_STRINGS as GEFS_SEARCH,
    SOURCES_PRIORITY as GEFS_SOURCES,
    get_run_fxx as _gefs_fxx,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    LOG_DIR / "pipeline.log",
    maxBytes=50 * 1024 * 1024,
    backupCount=3,
)
_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logging.basicConfig(
    level=logging.INFO,
    handlers=[_handler, logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """Read an integer env var with a floor and safe fallback."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var with a safe fallback."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    logger.warning("Invalid %s=%r; using %s", name, raw, default)
    return default


def _env_csv_set(name: str, default: set[str]) -> set[str]:
    """Read a comma-separated env var into a normalized set."""
    raw = os.getenv(name)
    if raw is None:
        return set(default)
    values = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return values if values else set()


_MAX_SEARCH_PARTS_PER_DOWNLOAD = 20
MODEL_PRIORITY_ORDER = [
    "hrrr",
    "nam3km",
    "rap",
    "rtma",
    "href",
    "gfs",
    "ecmwf",
    "nam12km",
    "rrfs",
    "gdps",
    "gefs",
]
MODEL_PRIORITY_RANK = {
    model_name: rank for rank, model_name in enumerate(MODEL_PRIORITY_ORDER)
}
POOL_MODEL_GROUPS = {
    "affinity_a": {"hrrr"},
    "affinity_b": {"nam3km", "rap", "rtma", "href", "rrfs"},
    "affinity_c": {"gfs", "ecmwf", "nam12km", "gdps", "gefs"},
}


def _balanced_dispatch_plan(renderable_by_model: dict[str, list[int]],
                            pool_slots: dict[str, int], ready_since: dict[str, float],
                            now: float, admit: Callable) -> list[tuple[str, str, int]]:
    """Prefer each pool's model family, borrow idle capacity, and age waiting work."""
    remaining = {name: list(hours) for name, hours in renderable_by_model.items() if hours}
    plan = []
    for pool_name, slots in pool_slots.items():
        preferred = POOL_MODEL_GROUPS.get(pool_name, set())
        for _ in range(slots):
            def rank(name):
                waited = now - ready_since.get(name, now)
                return (0 if waited >= 120 else 1,
                        -waited if waited >= 120 else 0,
                        name not in preferred, MODEL_PRIORITY_RANK.get(name, 999))
            submitted = False
            for model in sorted(remaining, key=rank):
                # A memory rejection applies to the model, not to a lead time.
                # Probe one ready hour rather than rebuilding hundreds of job
                # descriptions while the service is waiting for headroom.
                for fxx in remaining[model][:1]:
                    if admit(pool_name, model, fxx):
                        remaining[model].remove(fxx)
                        ready_since[model] = now
                        plan.append((pool_name, model, fxx))
                        submitted = True
                        break
                if not remaining[model]:
                    del remaining[model]
                if submitted:
                    break
            if not submitted:
                break
    return plan


def _shared_dispatch_plan(
    renderable_by_model: dict[str, list[int]],
    slots: int,
    fairness_cursor: int,
) -> tuple[list[tuple[str, int]], int]:
    """Fill shared slots while giving one ready secondary model a turn."""
    active = [name for name in MODEL_PRIORITY_ORDER if renderable_by_model.get(name)]
    if slots <= 0 or not active:
        return [], fairness_cursor

    remaining = {name: list(renderable_by_model[name]) for name in active}
    plan: list[tuple[str, int]] = []
    if slots > 1 and len(active) > 1:
        # Reserve one slot for a model behind the highest-priority ready one.
        # Rotate that slot so a long RAP/GFS run cannot starve RTMA or others.
        for offset in range(len(MODEL_PRIORITY_ORDER)):
            rank = (fairness_cursor + offset) % len(MODEL_PRIORITY_ORDER)
            secondary = MODEL_PRIORITY_ORDER[rank]
            if secondary in remaining and secondary != active[0]:
                plan.append((secondary, remaining[secondary].pop(0)))
                fairness_cursor = (rank + 1) % len(MODEL_PRIORITY_ORDER)
                break

    for model_name in active:
        while remaining[model_name] and len(plan) < slots:
            plan.append((model_name, remaining[model_name].pop(0)))
        if len(plan) >= slots:
            break
    return plan, fairness_cursor


# ---------------------------------------------------------------------------
# Background download manager — fetches data for ALL models continuously
# ---------------------------------------------------------------------------

DOWNLOAD_THREADS_TOTAL = _env_int("PIPELINE_DOWNLOAD_THREADS", 12)
DOWNLOAD_ACTIVE_WINDOW = _env_int("PIPELINE_DOWNLOAD_ACTIVE_WINDOW", 4)
DOWNLOAD_MAX_INFLIGHT_PER_MODEL = _env_int(
    "PIPELINE_DOWNLOAD_MAX_INFLIGHT_PER_MODEL", 6
)  # lower-priority fairness cap; top model can still use all threads
FULL_MODEL_DISCOVERY_INTERVAL = _env_int("PIPELINE_FULL_DISCOVERY_INTERVAL", 5)
DOWNLOAD_SCAN_PAUSE_SECONDS = 0.1
DOWNLOAD_ACTIVE_SCAN_INTERVAL_SECONDS = _env_int("PIPELINE_DOWNLOAD_ACTIVE_SCAN_INTERVAL", 5)
DOWNLOAD_IDLE_SCAN_INTERVAL_SECONDS = _env_int("PIPELINE_DOWNLOAD_IDLE_SCAN_INTERVAL", 15)
DOWNLOAD_RETRY_BACKOFF_SECONDS = _env_int("PIPELINE_DOWNLOAD_RETRY_BACKOFF_SECONDS", 20)
DOWNLOAD_PROGRESSIVE_WINDOW_SIZE = _env_int(
    "PIPELINE_DOWNLOAD_PROGRESSIVE_WINDOW_SIZE", 7
)
DOWNLOAD_SCAN_SUMMARY_EVERY = _env_int("PIPELINE_DOWNLOAD_SCAN_SUMMARY_EVERY", 1)


class DownloadManager:
    """Continuously scans all models and downloads data as soon as available.

    Downloads run in parallel for every model regardless of render priority.
    The main loop pulls from the download state using priority waterfall.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # model_name -> (run_dt, run_id, {fxx: Path})
        self._active: dict[str, tuple[datetime, str, dict[int, Path]]] = {}
        # model_name -> set of fxx currently being downloaded
        self._inflight: dict[str, set[int]] = {}
        # (model_name, run_id, fxx) -> retry epoch after transient "not ready"/failure
        self._retry_after: dict[tuple[str, str, int], float] = {}
        self._pool = ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS_TOTAL)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan_pass = 0

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="download_manager", daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def get_downloaded(self, model_name: str) -> tuple[datetime, str, dict[int, Path]] | None:
        """Return (run_dt, run_id, {fxx: Path}) for a model, or None."""
        with self._lock:
            entry = self._active.get(model_name)
            if entry is None:
                return None
            run_dt, run_id, downloaded = entry
            return (run_dt, run_id, dict(downloaded))

    def get_all_active_runs(self) -> dict[str, tuple[datetime, str]]:
        """Return {model_name: (run_dt, run_id)} for all models with active downloads."""
        with self._lock:
            return {
                name: (run_dt, run_id)
                for name, (run_dt, run_id, _) in self._active.items()
            }

    def _run(self):
        """Background loop: scan all models, submit downloads."""
        while not self._stop.is_set():
            try:
                self._scan_pass += 1
                full_scan = (
                    FULL_MODEL_DISCOVERY_INTERVAL <= 1
                    or self._scan_pass % FULL_MODEL_DISCOVERY_INTERVAL == 0
                )
                self._scan_all(full_scan=full_scan)
            except Exception as exc:
                logger.error("DownloadManager scan error: %s", exc, exc_info=True)
            # Scan faster when idle (no active downloads) to pick up new runs quickly.
            # When active, keep scans frequent so lower-priority models can pick
            # up quickly when the top model has no immediately downloadable work.
            with self._lock:
                has_active = bool(self._active)
            self._stop.wait(
                timeout=(
                    DOWNLOAD_ACTIVE_SCAN_INTERVAL_SECONDS
                    if has_active
                    else DOWNLOAD_IDLE_SCAN_INTERVAL_SECONDS
                )
            )

    def _models_to_scan(self, *, full_scan: bool) -> list[str]:
        """Return the model scan order for this pass."""
        if full_scan:
            return list(MODEL_PRIORITY_ORDER)

        scan_names = list(MODEL_PRIORITY_ORDER[:DOWNLOAD_ACTIVE_WINDOW])
        with self._lock:
            active_names = list(self._active.keys())

        for name in active_names:
            if name not in scan_names:
                scan_names.append(name)
        return scan_names

    @staticmethod
    def _summarize_scan_model(model_name: str, status: str, detail: str | None = None) -> str:
        label = model_name.upper()
        if detail:
            return f"{label}:{status}({detail})"
        return f"{label}:{status}"

    def _scan_all(self, *, full_scan: bool):
        """One pass: detect pending work for the current scan set."""
        highest_pending_model: str | None = None
        scan_names = self._models_to_scan(full_scan=full_scan)
        scan_summary: list[str] = []
        submitted_total = 0
        for model_name in scan_names:
            if self._stop.is_set():
                break

            if DOWNLOAD_SCAN_PAUSE_SECONDS > 0:
                time.sleep(DOWNLOAD_SCAN_PAUSE_SECONDS)
            
            model = MODEL_MAP.get(model_name)
            if model is None:
                continue
            try:
                run_dt = _detect_pending(model)
                if run_dt is None:
                    with self._lock:
                        self._active.pop(model_name, None)
                        self._inflight.pop(model_name, None)
                    scan_summary.append(self._summarize_scan_model(model_name, "idle"))
                    continue

                if highest_pending_model is None:
                    highest_pending_model = model_name

                run_id = run_dt.strftime("%Y%m%d%H")

                with self._lock:
                    current = self._active.get(model_name)
                    if current is not None and current[1] != run_id:
                        logger.info("[%s] New run %s detected (was %s)",
                                    model_name.upper(), run_id, current[1])
                        self._active.pop(model_name, None)
                        self._inflight.pop(model_name, None)
                        stale_keys = [
                            key for key in self._retry_after
                            if key[0] == model_name
                        ]
                        for key in stale_keys:
                            self._retry_after.pop(key, None)

                    if model_name not in self._active:
                        self._active[model_name] = (run_dt, run_id, {})
                        self._inflight[model_name] = set()

                    downloaded = self._active[model_name][2]
                    inflight = self._inflight[model_name]

                fxx_list = list(model.get_fxx(run_dt.hour))
                out_dir = _run_dir(model_name, run_dt)
                frontier_idx = 0
                submitted_for_model = 0

                # Probe downloads in a progressive sliding window across the
                # model's forecast-hour order. This keeps attention on the next
                # unresolved block instead of racing far ahead when an early
                # hour is still unpublished.
                for idx, frontier_fxx in enumerate(fxx_list):
                    if frontier_fxx in downloaded:
                        frontier_idx = idx + 1
                        continue
                    if _is_fxx_complete(
                        out_dir, frontier_fxx, model.rendered_params, model.snow_products
                    ):
                        frontier_idx = idx + 1
                        continue
                    frontier_idx = idx
                    break
                scan_fxx_list = fxx_list[frontier_idx:frontier_idx + DOWNLOAD_PROGRESSIVE_WINDOW_SIZE]
                frontier_fxx = fxx_list[frontier_idx] if frontier_idx < len(fxx_list) else None
                stalled_reason: str | None = None

                for fxx in scan_fxx_list:
                    retry_key = (model_name, run_id, fxx)
                    with self._lock:
                        if fxx in downloaded or fxx in inflight:
                            continue
                        retry_after = self._retry_after.get(retry_key, 0)
                        if retry_after > time.time():
                            if stalled_reason is None:
                                stalled_reason = "backoff"
                            continue
                        # Per-model cap: don't let one model hog all threads
                        inflight_cap = (
                            DOWNLOAD_THREADS_TOTAL
                            if model_name == highest_pending_model
                            else DOWNLOAD_MAX_INFLIGHT_PER_MODEL
                        )
                        if len(inflight) >= inflight_cap:
                            if stalled_reason is None:
                                stalled_reason = f"cap:{len(inflight)}/{inflight_cap}"
                            continue
                        # Global cap: never queue more than the pool can run,
                        # otherwise queued downloads sit in inflight forever
                        # and block future scans from submitting new work.
                        total_inflight = sum(len(s) for s in self._inflight.values())
                        if total_inflight >= DOWNLOAD_THREADS_TOTAL:
                            stalled_reason = f"pool:{total_inflight}/{DOWNLOAD_THREADS_TOTAL}"
                            break

                    # Check local cache first
                    cached = _find_cached_grib(model, run_dt, fxx)
                    if cached is not None:
                        with self._lock:
                            self._active[model_name][2][fxx] = cached
                            self._retry_after.pop(retry_key, None)
                        continue

                    # Already rendered on disk? Skip.
                    if _is_fxx_complete(out_dir, fxx, model.rendered_params, model.snow_products):
                        continue

                    with self._lock:
                        self._inflight[model_name].add(fxx)
                    logger.info("[%s] [DL] Submitting download fxx=%02d", model_name.upper(), fxx)
                    self._pool.submit(self._download_one, model, run_dt, run_id, fxx)
                    submitted_for_model += 1
                    submitted_total += 1

                if submitted_for_model > 0:
                    scan_summary.append(
                        self._summarize_scan_model(
                            model_name, "queued", f"{run_id} +{submitted_for_model}"
                        )
                    )
                elif frontier_fxx is None:
                    scan_summary.append(
                        self._summarize_scan_model(model_name, "ready", f"{run_id} complete")
                    )
                else:
                    detail = f"{run_id} f{frontier_fxx:02d}"
                    if stalled_reason:
                        detail = f"{detail} {stalled_reason}"
                    scan_summary.append(
                        self._summarize_scan_model(model_name, "waiting", detail)
                    )

            except Exception as exc:
                logger.error("[%s] Download scan error: %s",
                             model_name.upper(), exc, exc_info=True)
                scan_summary.append(self._summarize_scan_model(model_name, "error"))

        if (
            scan_summary
            and DOWNLOAD_SCAN_SUMMARY_EVERY > 0
            and self._scan_pass % DOWNLOAD_SCAN_SUMMARY_EVERY == 0
        ):
            mode = "full" if full_scan else "active"
            logger.info(
                "[DL] Scan pass %d (%s, submitted=%d): %s",
                self._scan_pass,
                mode,
                submitted_total,
                ", ".join(scan_summary),
            )

    def _download_one(self, model: ModelConfig, run_dt: datetime, run_id: str, fxx: int):
        """Download a single fxx and register it."""
        _ensure_requests_timeout_patch()
        try:
            path = _download_fxx(model, run_dt, fxx)
            with self._lock:
                self._inflight.get(model.name, set()).discard(fxx)
                if path is not None:
                    entry = self._active.get(model.name)
                    if entry is not None and entry[1] == run_id:
                        entry[2][fxx] = path
                    self._retry_after.pop((model.name, run_id, fxx), None)
                else:
                    self._retry_after[(model.name, run_id, fxx)] = (
                        time.time() + DOWNLOAD_RETRY_BACKOFF_SECONDS
                    )
                    logger.info("[%s] [DL] fxx=%02d not yet available", model.name.upper(), fxx)
        except Exception as exc:
            logger.warning("[%s] [DL] Download failed fxx=%02d: %s",
                           model.name.upper(), fxx, exc)
            with self._lock:
                self._inflight.get(model.name, set()).discard(fxx)
                self._retry_after[(model.name, run_id, fxx)] = (
                    time.time() + DOWNLOAD_RETRY_BACKOFF_SECONDS
                )

def _chunk_search_patterns(patterns: list[str], max_parts: int = _MAX_SEARCH_PARTS_PER_DOWNLOAD) -> list[str]:
    """Split large Herbie partial-download regexes into smaller chunks."""
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


def _subset_output_path(herbie_obj: Herbie, patterns: list[str]) -> Path:
    """Return a deterministic local path for a subset download."""
    grib_source = str(herbie_obj.grib)
    basename = (
        Path(urlparse(grib_source).path).name
        if grib_source.startswith(("http://", "https://"))
        else Path(grib_source).name
    )
    base_path = (
        Path(herbie_obj.save_dir).expand()
        / herbie_obj.model
        / f"{herbie_obj.date:%Y%m%d}"
        / basename
    )
    return _combined_subset_path(base_path, patterns)


def _read_text(src: str) -> str:
    if src.startswith(("http://", "https://")):
        with urlopen(src, timeout=30) as resp:
            return resp.read().decode("utf-8")
    return Path(src).read_text()


def _parse_wgrib2_idx(idx_text: str) -> list[tuple[int, int | None, str]]:
    """Parse a wgrib2-style idx file into byte ranges with the source line."""
    rows: list[tuple[int, str]] = []
    for line in idx_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        try:
            start = int(parts[1])
        except ValueError:
            continue
        rows.append((start, line))

    records: list[tuple[int, int | None, str]] = []
    for i, (start, line) in enumerate(rows):
        end = rows[i + 1][0] - 1 if i + 1 < len(rows) else None
        records.append((start, end, line))
    return records


def _merge_byte_ranges(ranges: list[tuple[int, int | None]]) -> list[tuple[int, int | None]]:
    if not ranges:
        return []
    ranges = sorted(ranges, key=lambda item: item[0])
    merged: list[list[int | None]] = [[ranges[0][0], ranges[0][1]]]
    for start, end in ranges[1:]:
        prev_start, prev_end = merged[-1]
        if prev_end is None or start <= prev_end + 1:
            merged[-1][1] = None if (prev_end is None or end is None) else max(prev_end, end)
            continue
        merged.append([start, end])
    return [(int(start), int(end) if end is not None else None) for start, end in merged]


def _download_range_bytes(grib_src: str, start: int, end: int | None) -> bytes:
    if grib_src.startswith(("http://", "https://")):
        range_value = f"bytes={start}-{'' if end is None else end}"
        request = Request(grib_src, headers={"Range": range_value})
        with urlopen(request, timeout=60) as resp:
            return resp.read()

    with open(grib_src, "rb") as fh:
        fh.seek(start)
        if end is None:
            return fh.read()
        return fh.read(end - start + 1)


def _download_subset_via_idx(herbie_obj: Herbie, patterns: list[str]) -> Path:
    """Download a subset directly from the idx file via byte ranges."""
    if not herbie_obj.idx:
        raise ValueError("Index file not found")

    out_path = _subset_output_path(herbie_obj, patterns)
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path

    compiled = [re.compile(pattern) for pattern in patterns if pattern]
    if not compiled:
        raise ValueError("No search patterns provided")

    records = _parse_wgrib2_idx(_read_text(str(herbie_obj.idx)))
    matched_ranges = [
        (start, end)
        for start, end, line in records
        if any(regex.search(line) for regex in compiled)
    ]
    if not matched_ranges:
        raise ValueError("No matching records in idx")

    merged_ranges = _merge_byte_ranges(matched_ranges)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    grib_src = str(herbie_obj.grib)

    with open(tmp_path, "wb") as out_f:
        for start, end in merged_ranges:
            out_f.write(_download_range_bytes(grib_src, start, end))

    if tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise ValueError("Empty subset download")

    tmp_path.replace(out_path)
    return out_path


def _combine_grib_parts(base_path: Path, patterns: list[str], part_paths: list[Path]) -> Path:
    """Concatenate multiple subset GRIBs into one deterministic combined file."""
    if len(part_paths) == 1:
        return part_paths[0]

    combined_path = _combined_subset_path(base_path, patterns)
    if combined_path.exists() and combined_path.stat().st_size > 0:
        return combined_path

    with open(combined_path, "wb") as combined_f:
        for part_path in part_paths:
            with open(part_path, "rb") as part_f:
                combined_f.write(part_f.read())
    return combined_path


def _download_subset_chunked(herbie_obj: Herbie, patterns: list[str]) -> Path:
    """Download a partial GRIB subset, chunking large search lists to avoid dropped fields."""
    chunks = _chunk_search_patterns(patterns)
    if not chunks:
        raise ValueError("No search patterns provided")

    try:
        return _download_subset_via_idx(herbie_obj, patterns)
    except Exception as exc:
        logger.debug("Direct idx subset failed for %s f%02d, falling back to Herbie: %s",
                     herbie_obj.model, herbie_obj.fxx, exc)

    if len(chunks) == 1:
        return Path(herbie_obj.download(chunks[0]))

    chunk_paths: list[Path] = []
    for chunk in chunks:
        chunk_path = Path(herbie_obj.download(chunk))
        if not chunk_path.exists():
            raise FileNotFoundError(chunk_path)
        chunk_paths.append(chunk_path)

    combined_path = _combined_subset_path(chunk_paths[0], patterns)
    if combined_path.exists() and combined_path.stat().st_size > 0:
        return combined_path

    with open(combined_path, "wb") as combined_f:
        for chunk_path in chunk_paths:
            with open(chunk_path, "rb") as chunk_f:
                combined_f.write(chunk_f.read())
    return combined_path

# Suppress Herbie's verbose internal logging (index errors, "remake" warnings)
logging.getLogger("herbie").setLevel(logging.CRITICAL)
logging.getLogger("herbie.core").setLevel(logging.CRITICAL)

DATA_DIR = RENDERED_DIR.parent
PIPELINE_STATE_FILE = DATA_DIR / "pipeline_state.json"

# Snow products that only render at fxx >= their lookback period
SNOW_FXX_MIN = {p: cfg["hours"] for p, cfg in SNOW_PRODUCTS.items()}


def _pool_short_label(pool_name: str) -> str:
    if pool_name == "affinity_a":
        return "A"
    if pool_name == "affinity_b":
        return "B"
    if pool_name == "affinity_c":
        return "C"
    if pool_name == "catchall":
        return "X"
    return "S"


def _pool_slot_layout(pool_configs: dict[str, "_PoolConfig"]) -> list[str]:
    if "shared" in pool_configs:
        return ["S"] * pool_configs["shared"].worker_count

    slots: list[str] = []
    for pool_name in ("affinity_a", "affinity_b", "affinity_c", "catchall"):
        cfg = pool_configs.get(pool_name)
        if cfg is None or cfg.worker_count <= 0:
            continue
        slots.extend([_pool_short_label(pool_name)] * cfg.worker_count)
    return slots


def _pool_slot_activity(
    pool_configs: dict[str, "_PoolConfig"],
    in_flight: dict[tuple[str, int], tuple[str, object]],
) -> list[str]:
    if "shared" in pool_configs:
        entries = sorted(
            (mn.upper(), fxx) for (mn, fxx), (pool_name, _future) in in_flight.items()
            if pool_name == "shared"
        )
        values = [f"S:{mn} f{fxx:02d}" for mn, fxx in entries]
        values.extend(["S:-"] * max(0, pool_configs["shared"].worker_count - len(values)))
        return values[: pool_configs["shared"].worker_count]

    slots: list[str] = []
    for pool_name in ("affinity_a", "affinity_b", "affinity_c", "catchall"):
        cfg = pool_configs.get(pool_name)
        if cfg is None or cfg.worker_count <= 0:
            continue
        short = _pool_short_label(pool_name)
        entries = sorted(
            (mn.upper(), fxx) for (mn, fxx), (pn, _future) in in_flight.items()
            if pn == pool_name
        )
        values = [f"{short}:{mn} f{fxx:02d}" for mn, fxx in entries]
        values.extend([f"{short}:-"] * max(0, cfg.worker_count - len(values)))
        slots.extend(values[: cfg.worker_count])
    return slots


# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------

@dataclass
class ModelConfig:
    name: str
    priority: int               # 1 = highest
    herbie_model: str
    herbie_product: str
    param_search: dict[str, str]
    prs_search: dict[str, str]
    derived: dict
    composites: dict
    rendered_params: list[str]
    sources: list[str]
    get_fxx: Callable
    run_hours: list[int]
    hourly_precip: bool = False  # subtract prev fxx for hourly precip
    snow_products: bool = False  # compute Kuchera / 10:1 snow
    herbie_kwargs: dict = field(default_factory=dict)  # extra Herbie args (domain, member)
    sounding_cache: bool = False


_SOUNDING_CACHE_STRIDE = {
    "hrrr": 6,
    "nam3km": 6,
}
_FULL_SOUNDING_DIRNAME = "soundings_native"

_SOUNDING_CACHE_PARAMS = {
    "hrrr": [
        "temp_2m", "dewpoint_2m", "mslp", "ugrd_10m", "vgrd_10m",
        "temp_1000mb", "temp_925mb", "temp_900mb", "temp_850mb", "temp_800mb", "temp_750mb",
        "temp_700mb", "temp_650mb", "temp_600mb", "temp_550mb", "temp_500mb", "temp_450mb",
        "temp_400mb", "temp_350mb", "temp_300mb", "temp_250mb", "temp_200mb",
        "dewpoint_1000mb", "dewpoint_925mb", "dewpoint_900mb", "dewpoint_850mb", "dewpoint_800mb", "dewpoint_750mb",
        "rh_1000mb", "rh_925mb", "rh_900mb", "rh_850mb", "rh_800mb", "rh_750mb", "rh_700mb",
        "rh_650mb", "rh_600mb", "rh_550mb", "rh_500mb", "rh_450mb", "rh_400mb", "rh_350mb",
        "rh_300mb", "rh_250mb", "rh_200mb",
        "gh_1000mb", "gh_925mb", "gh_900mb", "gh_850mb", "gh_800mb", "gh_750mb", "gh_700mb",
        "gh_650mb", "gh_600mb", "gh_550mb", "gh_500mb", "gh_450mb", "gh_400mb", "gh_350mb",
        "gh_300mb", "gh_250mb", "gh_200mb",
        "ugrd_1000mb", "vgrd_1000mb", "ugrd_925mb", "vgrd_925mb", "ugrd_900mb", "vgrd_900mb",
        "ugrd_850mb", "vgrd_850mb", "ugrd_800mb", "vgrd_800mb", "ugrd_750mb", "vgrd_750mb",
        "ugrd_700mb", "vgrd_700mb", "ugrd_650mb", "vgrd_650mb", "ugrd_600mb", "vgrd_600mb",
        "ugrd_550mb", "vgrd_550mb", "ugrd_500mb", "vgrd_500mb", "ugrd_450mb", "vgrd_450mb",
        "ugrd_400mb", "vgrd_400mb", "ugrd_350mb", "vgrd_350mb", "ugrd_300mb", "vgrd_300mb",
        "ugrd_250mb", "vgrd_250mb", "ugrd_200mb", "vgrd_200mb",
    ],
    "nam3km": [
        "temp_2m", "dewpoint_2m", "mslp", "ugrd_10m", "vgrd_10m",
        "temp_1000mb", "temp_925mb", "temp_900mb", "temp_850mb", "temp_800mb", "temp_750mb",
        "temp_700mb", "temp_650mb", "temp_600mb", "temp_550mb", "temp_500mb", "temp_450mb",
        "temp_400mb", "temp_350mb", "temp_300mb", "temp_250mb", "temp_200mb",
        "dewpoint_1000mb", "dewpoint_925mb", "dewpoint_900mb", "dewpoint_850mb", "dewpoint_800mb", "dewpoint_750mb",
        "rh_1000mb", "rh_925mb", "rh_900mb", "rh_850mb", "rh_800mb", "rh_750mb", "rh_700mb",
        "rh_650mb", "rh_600mb", "rh_550mb", "rh_500mb", "rh_450mb", "rh_400mb", "rh_350mb",
        "rh_300mb", "rh_250mb", "rh_200mb",
        "gh_1000mb", "gh_925mb", "gh_900mb", "gh_850mb", "gh_800mb", "gh_750mb", "gh_700mb",
        "gh_650mb", "gh_600mb", "gh_550mb", "gh_500mb", "gh_450mb", "gh_400mb", "gh_350mb",
        "gh_300mb", "gh_250mb", "gh_200mb",
        "ugrd_1000mb", "vgrd_1000mb", "ugrd_925mb", "vgrd_925mb", "ugrd_900mb", "vgrd_900mb",
        "ugrd_850mb", "vgrd_850mb", "ugrd_800mb", "vgrd_800mb", "ugrd_750mb", "vgrd_750mb",
        "ugrd_700mb", "vgrd_700mb", "ugrd_650mb", "vgrd_650mb", "ugrd_600mb", "vgrd_600mb",
        "ugrd_550mb", "vgrd_550mb", "ugrd_500mb", "vgrd_500mb", "ugrd_450mb", "vgrd_450mb",
        "ugrd_400mb", "vgrd_400mb", "ugrd_350mb", "vgrd_350mb", "ugrd_300mb", "vgrd_300mb",
        "ugrd_250mb", "vgrd_250mb", "ugrd_200mb", "vgrd_200mb",
    ],
}


def _get_sounding_cache_params(model_name: str) -> list[str]:
    return list(_SOUNDING_CACHE_PARAMS.get((model_name or "").lower(), []))


def _write_native_sounding_cache(processed: dict, out_dir: Path, fxx: int, model_name: str):
    cache_params = _get_sounding_cache_params(model_name)
    if not cache_params:
        return

    available = [param for param in cache_params if param in processed]
    if not available:
        return

    first = processed[available[0]]
    payload: dict[str, np.ndarray] = {
        "lat": np.asarray(first["lat"], dtype=np.float32),
        "lon": np.asarray(first["lon"], dtype=np.float32),
        "native": np.asarray([1], dtype=np.int16),
    }

    for param in available:
        payload[param] = np.asarray(processed[param]["data"], dtype=np.float16)

    sounding_dir = out_dir / _FULL_SOUNDING_DIRNAME
    sounding_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = sounding_dir / f"f{fxx:02d}.npz.tmp"
    out_path = sounding_dir / f"f{fxx:02d}.npz"
    np.savez(tmp_path, **payload)
    Path(str(tmp_path) + ".npz").replace(out_path)


def _write_ptype_grid(renderer, processed: dict, out_dir: Path, fxx: int, model_name: str) -> None:
    """Publish HRRR surface precip-type codes for the radar Precip Type product.

    Written atomically as <run>/ptype_grid/fXX.npz; removed with the run.
    Failures are logged and never affect the model render.
    """
    if model_name != "hrrr" or "precip_type" not in processed:
        return
    previous = renderer._current_model_name
    try:
        renderer._current_model_name = model_name
        built = renderer.build_ptype_grid(processed)
        if built is None:
            return
        codes, bounds = built
        target = out_dir / "ptype_grid" / f"f{fxx:02d}.npz"
        target.parent.mkdir(parents=True, exist_ok=True)
        valid = processed["precip_type"].get("valid_time")
        temporary = target.with_name(f".{target.stem}.tmp.npz")
        np.savez_compressed(temporary, codes=codes, bounds=np.asarray(bounds, dtype=np.float64),
                            valid_time=np.asarray(str(valid or "")))
        temporary.replace(target)
    except Exception as exc:
        logger.warning("[HRRR] fxx=%02d precip-type grid not written: %s", fxx, exc)
    finally:
        renderer._current_model_name = previous


def _write_sounding_cache(processed: dict, out_dir: Path, fxx: int, model_name: str):
    cache_params = _get_sounding_cache_params(model_name)
    if not cache_params:
        return

    available = [param for param in cache_params if param in processed]
    if not available:
        return

    first = processed[available[0]]
    lat = np.asarray(first["lat"])
    lon = np.asarray(first["lon"])
    stride = _SOUNDING_CACHE_STRIDE.get((model_name or "").lower(), 6)

    payload: dict[str, np.ndarray] = {
        "lat": np.asarray(lat[::stride, ::stride] if lat.ndim == 2 else lat[::stride], dtype=np.float32),
        "lon": np.asarray(lon[::stride, ::stride] if lon.ndim == 2 else lon[::stride], dtype=np.float32),
        "stride": np.asarray([stride], dtype=np.int16),
    }

    for param in available:
        data = np.asarray(processed[param]["data"])
        if data.ndim == 2:
            data = data[::stride, ::stride]
        elif data.ndim == 1:
            data = data[::stride]
        payload[param] = np.asarray(data, dtype=np.float16)

    sounding_dir = out_dir / "soundings"
    sounding_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = sounding_dir / f"f{fxx:02d}.npz.tmp"
    out_path = sounding_dir / f"f{fxx:02d}.npz"
    np.savez(tmp_path, **payload)
    Path(str(tmp_path) + ".npz").replace(out_path)


MODELS = [
    ModelConfig(
        name="hrrr", priority=1,
        herbie_model="hrrr", herbie_product="sfc",
        param_search=HRRR_SEARCH, prs_search=HRRR_PRS,
        derived=HRRR_DERIVED, composites=HRRR_COMPOSITES,
        rendered_params=[
            "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
            "reflectivity", "precip_type", "precip", "precip_12h",
            "cape", "cin", "lifted_index", "stp", "scp", "cti",
            "msi", "smi", "ship", "dcp", "citi", "cvi", "llmt", "ascent", "hp_risk",
            "vil", "uh_25km", "uh_03km",
            "srh_01km", "srh_03km", "shear_06km", "shear_01km",
            "mlcape", "mlcin", "mucape", "mucin", "cape_03km", "lcl", "pwat",
            "uh_02km", "max_wind", "efhl", "cloud_cover", "lightning",
            "echo_tops", "hail", "pbl_height", "weasd",
            "temp_850mb", "temp_700mb", "temp_500mb", "temp_925mb",
            "dewpoint_850mb",
            "omega_700mb", "vort_500mb",
            "wind_850mb", "wind_700mb", "wind_500mb", "wind_300mb", "wind_250mb",
            "rh_850mb", "rh_700mb",
            "visibility", "snow_depth", "frozen_pct", "frzlvl",
            "kuchera_6hr", "kuchera_12hr", "snow10_12hr",
        ],
        sources=HRRR_SOURCES, get_fxx=_hrrr_fxx,
        run_hours=list(range(24)),
        hourly_precip=True, snow_products=True,
        sounding_cache=True,
    ),
    ModelConfig(
        name="rtma", priority=10,
        herbie_model="rtma", herbie_product="anl",
        param_search=RTMA_SEARCH, prs_search={},
        derived=RTMA_DERIVED, composites={},
        rendered_params=[
            "temp_2m", "dewpoint_2m", "rh_2m", "wind_speed", "wind_gusts", "psfc",
            "visibility", "cloud_cover",
        ],
        sources=RTMA_SOURCES, get_fxx=_rtma_fxx,
        run_hours=list(range(24)),
    ),
    ModelConfig(
        name="nam3km", priority=1,
        herbie_model=NAM3KM_HERBIE_MODEL, herbie_product=NAM3KM_HERBIE_PRODUCT,
        param_search=NAM3KM_SEARCH, prs_search={},
        derived=NAM3KM_DERIVED, composites=NAM3KM_COMPOSITES,
        rendered_params=[
            "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
            "reflectivity", "precip_type", "precip",
            "mucape", "mucin", "mlcape", "mlcin", "lifted_index",
            "vil", "lightning", "cloud_cover",
            "srh_01km", "srh_03km", "shear_06km", "shear_01km",
            "uh_25km", "uh_03km",
            "lcl", "visibility", "snow_depth", "frzlvl",
            "pwat", "pbl_height", "echo_tops",
            "temp_850mb", "temp_700mb", "temp_500mb", "temp_925mb",
            "dewpoint_850mb",
            "omega_700mb", "vort_500mb",
            "wind_850mb", "wind_700mb", "wind_500mb", "wind_250mb",
            "rh_850mb", "rh_700mb",
            "cti", "stp", "scp", "smi", "ship", "dcp", "citi", "cvi", "llmt", "ascent", "hp_risk",
        ],
        sources=NAM3KM_SOURCES, get_fxx=_nam3km_fxx,
        run_hours=[0, 6, 12, 18],
        sounding_cache=True,
    ),
    ModelConfig(
        name="rap", priority=1,
        herbie_model="rap", herbie_product="awp130pgrb",
        param_search=RAP_SEARCH, prs_search={},
        derived=RAP_DERIVED, composites={},
        rendered_params=[
            "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
            "reflectivity", "precip_type", "precip",
            "cape", "cin", "mucape", "mucin", "mlcape", "mlcin",
            "lifted_index", "pwat", "cape_03km", "lcl",
            "cloud_cover", "visibility", "snow_depth",
            "echo_tops", "pbl_height", "weasd",
            "srh_01km", "srh_03km", "shear_06km",
            "temp_850mb", "temp_700mb", "temp_500mb", "temp_925mb",
            "dewpoint_850mb",
            "omega_700mb", "vort_500mb", "lightning",
            "wind_850mb", "wind_500mb", "wind_300mb", "wind_250mb",
            "rh_700mb", "rh_850mb", "frzlvl", "frozen_pct",
        ],
        sources=RAP_SOURCES, get_fxx=_rap_fxx,
        run_hours=list(range(24)),
        hourly_precip=True,
    ),
    ModelConfig(
        name="nam12km", priority=10,
        herbie_model="nam", herbie_product="awphys",
        param_search=NAM_SEARCH, prs_search={},
        derived=NAM_DERIVED, composites={},
        rendered_params=[
            "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
            "reflectivity", "precip",
            "cape", "cin", "mucape", "mucin", "mlcape", "mlcin",
            "lifted_index", "pwat", "cape_03km", "lcl",
            "vil", "cloud_cover", "visibility",
            "srh_01km", "srh_03km", "shear_06km", "shear_01km",
            "echo_tops", "lightning",
            "snow_depth", "frzlvl",
            "temp_850mb", "temp_700mb", "temp_500mb", "temp_925mb",
            "omega_700mb", "vort_500mb",
            "rh_700mb", "rh_850mb", "wind_850mb",
        ],
        sources=NAM_SOURCES, get_fxx=_nam_fxx,
        run_hours=[0, 6, 12, 18],
    ),
    ModelConfig(
        name="ecmwf", priority=10,
        herbie_model="ifs", herbie_product="oper",
        param_search=IFS_SEARCH, prs_search={},
        derived=IFS_DERIVED, composites={},
        rendered_params=[
            "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
            "precip", "mucape", "cloud_cover", "pwat", "snowfall", "snow_depth",
            "temp_850mb", "temp_700mb", "temp_500mb",
            "rh_700mb", "rh_850mb",
            "wind_850mb", "wind_500mb", "wind_300mb",
        ],
        sources=IFS_SOURCES, get_fxx=_ifs_fxx,
        run_hours=[0, 12],
    ),
    ModelConfig(
        name="gfs", priority=10,
        herbie_model="gfs", herbie_product="pgrb2.0p25",
        param_search=GFS_SEARCH, prs_search=GFS_PRS,
        derived=GFS_DERIVED, composites=GFS_COMPOSITES,
        rendered_params=[
            # Surface
            "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
            "precip", "visibility",
            # Instability
            "cape", "cin", "mucape", "mucin", "mlcape", "mlcin", "cape_03km",
            "lifted_index", "pwat",
            "pbl_height", "weasd", "frzlvl",
            # Helicity (GFS only has 0-3km)
            "srh_03km",
            # Pressure levels
            "reflectivity", "precip_type",
            "temp_850mb", "temp_700mb", "temp_500mb", "temp_925mb",
            "rh_700mb", "rh_850mb",
            "wind_850mb", "wind_500mb", "wind_300mb", "wind_250mb",
            # Winter
            "snow_depth",
        ],
        sources=GFS_SOURCES, get_fxx=_gfs_fxx,
        run_hours=[0, 6, 12, 18],
    ),
    ModelConfig(
        name="gdps", priority=10,
        herbie_model="gdps", herbie_product="latlon15x15",
        param_search=GDPS_SEARCH, prs_search={},
        derived=GDPS_DERIVED, composites={},
        rendered_params=[
            # Surface - only params that exist in GDPS_PARAM_MAP
            "temp_2m", "dewpoint_2m", "mslp",
            "precip", "cloud_cover",
            "cape_surface", "cin_surface",
            # Pressure levels
            "temp_850mb", "temp_700mb", "temp_500mb",
            "rh_700mb", "rh_850mb", "omega_700mb", "vort_500mb",
            "wind_speed", "wind_gusts", "wind_850mb", "wind_700mb",
            "wind_500mb", "wind_300mb",
            # Winter
            "snow_depth",
        ],
        sources=GDPS_SOURCES, get_fxx=_gdps_fxx,
        run_hours=[0, 12],
    ),
    # HREF: High-Resolution Ensemble (probability forecasts)
    ModelConfig(
        name="href", priority=10,
        herbie_model="href", herbie_product="mean",
        herbie_kwargs={"domain": "conus"},
        param_search=HREF_SEARCH, prs_search={},
        derived=HREF_DERIVED, composites={},
        rendered_params=[
            # Surface
            "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
            # Reflectivity and probabilities
            "reflectivity", "refl_probability_40dbz",
            # Convective
            "cape", "cin", "cape_probability",
            "srh_01km", "srh_03km", "uh_25km_probability",
            # Pressure levels
            "temp_850mb", "temp_700mb", "temp_500mb",
            "wind_850mb", "wind_500mb",
            "rh_700mb", "rh_850mb",
            # Precip
            "precip", "precip_probability",
            "frozen_pct",
            # Other
            "visibility", "cloud_cover", "pwat",
        ],
        sources=HREF_SOURCES, get_fxx=_href_fxx,
        run_hours=[0, 6, 12, 18],
    ),
    # RRFS: Rapid Refresh Forecast System (next-gen, HRRR replacement)
    ModelConfig(
        name="rrfs", priority=10,
        herbie_model="rrfs", herbie_product="prslev",
        herbie_kwargs={"member": "control", "domain": "conus"},
        param_search=RRFS_SEARCH, prs_search={},
        derived=RRFS_DERIVED, composites={},
        rendered_params=[
            # Surface
            "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
            "precip", "reflectivity", "refl_1km",
            # Convective
            "cape", "cin", "cape_ml", "cin_ml",
            "srh_01km", "srh_03km", "uh_25km", "uh_02km",
            "shear_01km", "shear_06km",
            # Pressure levels
            "temp_925mb", "temp_850mb", "temp_700mb", "temp_500mb", "temp_300mb", "temp_250mb",
            "wind_925mb", "wind_850mb", "wind_700mb", "wind_500mb", "wind_300mb", "wind_250mb",
            "rh_850mb", "rh_700mb", "rh_500mb",
            "vort_500mb",
            # Other
            "cloud_cover", "visibility", "ceil", "pwat",
            "snow_depth", "weasd", "frozen_pct", "frzlvl",
            "vil", "echo_tops", "pbl_height", "lcl", "lightning",
            "max_wind", "max_temp_1h", "min_temp_1h",
        ],
        sources=RRFS_SOURCES, get_fxx=_rrfs_fxx,
        run_hours=[0, 6, 12, 18],  # S3 bucket only has 4x daily runs
    ),
    # GEFS: Global Ensemble Forecast System (extended range)
    ModelConfig(
        name="gefs", priority=10,  # Lower priority (longer runs, lower res)
        herbie_model="gefs", herbie_product="atmos.25",
        herbie_kwargs={"member": 0},
        param_search=GEFS_SEARCH, prs_search={},
        derived=GEFS_DERIVED, composites={},
        rendered_params=[
            # Surface
            "temp_2m", "dewpoint_2m", "wind_speed", "wind_gusts", "mslp",
            # Precip (6-hourly accumulations)
            "precip_6h", "precip_probability_6h_05in", "precip_probability_6h_1in",
            "snowfall_6h", "snowfall_probability_6h_1in",
            # Pressure levels — comprehensive
            "temp_925mb", "temp_850mb", "temp_700mb", "temp_500mb", "temp_300mb", "temp_250mb", "temp_200mb",
            "wind_925mb", "wind_850mb", "wind_700mb", "wind_500mb", "wind_300mb", "wind_250mb", "wind_200mb",
            "rh_850mb", "rh_700mb", "rh_500mb",
            # Ensemble-specific
            "gh_500mb_spread", "gh_500mb_anomaly",
            "pwat", "pwat_probability_1in",
            "cape", "cape_probability_1000",
            # Cloud
            "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
            "lifted_index",
        ],
        sources=GEFS_SOURCES, get_fxx=_gefs_fxx,
        run_hours=[0, 6, 12, 18],
    ),
]

_enabled_models = _env_csv_set("WX_MODELS", set())
if _enabled_models:
    MODELS = [model for model in MODELS if model.name in _enabled_models]
MODEL_MAP = {m.name: m for m in MODELS}


# Pipeline tuning
DOWNLOAD_TIMEOUT = 60  # read timeout — drop stalled connections, retry on next scan
MODEL_WORKER_MAX_TASKS = _env_int("MODEL_WORKER_MAX_TASKS", 2)  # recycle workers periodically to reclaim memory
MODEL_TASK_METRICS = _env_bool("MODEL_TASK_METRICS", False)

# Shared render pool, with additional backfill handled by the
# separate low-priority service.
SHARED_RENDER_WORKERS = _env_int("SHARED_RENDER_WORKERS", 6)
LOW_PRIORITY_WORKERS = _env_int("LOWPRI_WORKERS", 1, minimum=0)
AFFINITY_POOL_MODE = _env_bool("AFFINITY_POOL_MODE", False)
BALANCED_POOL_MODE = _env_bool("BALANCED_POOL_MODE", True)
AFFINITY_POOL_A_WORKERS = _env_int("AFFINITY_POOL_A_WORKERS", 2)
AFFINITY_POOL_B_WORKERS = _env_int("AFFINITY_POOL_B_WORKERS", 2)
AFFINITY_POOL_C_WORKERS = _env_int("AFFINITY_POOL_C_WORKERS", 2, minimum=0)
AFFINITY_CATCHALL_WORKERS = _env_int("AFFINITY_CATCHALL_WORKERS", 0, minimum=0)
AFFINITY_POOL_LEASE_FXX = _env_int("AFFINITY_POOL_LEASE_FXX", 2)
AFFINITY_POOL_IDLE_RELEASE_SECONDS = _env_int("AFFINITY_POOL_IDLE_RELEASE_SECONDS", 180)
AFFINITY_POOL_HOLD_OPEN_SECONDS = _env_int("AFFINITY_POOL_HOLD_OPEN_SECONDS", 45, minimum=0)
AFFINITY_POOL_BURST_SUBWORKERS = max(1, _env_int("AFFINITY_POOL_BURST_SUBWORKERS", 3))
TRICKLE_READY_FXX_THRESHOLD = _env_int("TRICKLE_READY_FXX_THRESHOLD", 4, minimum=1)
TRICKLE_MULTI_MODEL_THRESHOLD = _env_int("TRICKLE_MULTI_MODEL_THRESHOLD", 2, minimum=1)
TRICKLE_IDLE_RELEASE_SECONDS = _env_int("TRICKLE_IDLE_RELEASE_SECONDS", 45, minimum=0)
TRICKLE_HOLD_OPEN_SECONDS = _env_int("TRICKLE_HOLD_OPEN_SECONDS", 15, minimum=0)
TRICKLE_RESERVED_CATCHALL_SLOTS = _env_int("TRICKLE_RESERVED_CATCHALL_SLOTS", 1, minimum=0)

BURST_RENDER_ENABLED = _env_bool("BURST_RENDER_ENABLED", False)
BURST_RENDER_MAX_SUBWORKERS = max(1, _env_int("BURST_RENDER_MAX_SUBWORKERS", 2))
BURST_RENDER_MAX_CPU_PCT = float(os.getenv("BURST_RENDER_MAX_CPU_PCT", "55"))
BURST_RENDER_MAX_MEM_PCT = float(os.getenv("BURST_RENDER_MAX_MEM_PCT", "80"))
BURST_RENDER_MIN_PARAMS = max(1, _env_int("BURST_RENDER_MIN_PARAMS", 18))
BURST_RENDER_TIMEOUT_SECONDS = max(1.0, float(os.getenv("BURST_RENDER_TIMEOUT_SECONDS", "600")))
BURST_RENDER_MEMORY_FACTOR = max(1.0, float(os.getenv("BURST_RENDER_MEMORY_FACTOR", "2.25")))
BURST_RENDER_MODELS = _env_csv_set("BURST_RENDER_MODELS", set(MODEL_PRIORITY_ORDER))

WORKER_RSS_LIMIT_MB = 1400
WORKER_CACHE_CLEAR_RSS_MB = 900
EMPTY_RENDER_RETRY_LIMIT = 3
EMPTY_RENDER_RETRY_BACKOFF_SECONDS = 15
SOURCE_RACE_MODELS = _env_csv_set(
    "SOURCE_RACE_MODELS", {"hrrr", "rap", "nam3km"}
)
SOURCE_RACE_PROBE_LIMIT = _env_int("SOURCE_RACE_PROBE_LIMIT", 2)
SOURCE_RACE_MAX_THREADS = _env_int("SOURCE_RACE_MAX_THREADS", 2)
SOURCE_PROBE_TIMEOUT_SECONDS = 15  # max time for a single source probe before giving up


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

_source_race_cache: dict[tuple[str, str], list[str]] = {}
_herbie_init_cache: dict[tuple, Herbie] = {}

_HERBIE_CACHE_TTL = 90.0  # seconds — short enough to pick up newly published fxx

def _get_herbie_cached(priority: list[str], **kwargs) -> Herbie:
    """Return a cached Herbie object for the same parameters."""
    key = (tuple(priority), tuple(sorted(kwargs.items())))
    now = time.time()
    cached = _herbie_init_cache.get(key)
    if cached is not None:
        herbie_obj, cached_at = cached
        # Don't serve stale negative results — fxx may have been published since
        if herbie_obj.grib is not None or (now - cached_at) < _HERBIE_CACHE_TTL:
            return herbie_obj

    H = Herbie(priority=priority, **kwargs)
    _herbie_init_cache[key] = (H, now)
    # Simple cache size limit
    if len(_herbie_init_cache) > 200:
        _herbie_init_cache.clear()
    return H


def _download_fxx(model: ModelConfig, run_dt: datetime, fxx: int,
                   _max_retries: int = 3) -> Path | None:
    """Download one forecast hour via Herbie. Returns local path or None.

    Retries with exponential backoff on 503 (rate-limit) errors.
    """
    run_id = run_dt.strftime("%Y%m%d%H")
    fallback_sources = [s for s in model.sources if s != "nomads"] or ["aws"]
    fallback_sources = [s for s in fallback_sources if SOURCE_CIRCUIT.available(model.name, s)]
    candidate_sources = list(dict.fromkeys(model.sources + fallback_sources))
    candidate_sources = [s for s in candidate_sources if SOURCE_CIRCUIT.available(model.name, s)]

    def _build_herbie(priority: list[str]) -> Herbie:
        return _get_herbie_cached(
            priority=priority,
            date=run_dt.strftime("%Y-%m-%d %H:%M"),
            model=model.herbie_model,
            product=model.herbie_product,
            fxx=fxx,
            save_dir=str(GRIB_CACHE_DIR),
            overwrite=False,
            verbose=False,
            **model.herbie_kwargs,
        )

    def _build_source_herbie(source: str) -> Herbie:
        try:
            return _build_herbie([source])
        except Exception as exc:
            SOURCE_CIRCUIT.failed(model.name, source, exc)
            raise

    def _probe_source(source: str) -> tuple[str, Herbie | None, str | None]:
        try:
            herbie_obj = _build_source_herbie(source)
            if herbie_obj.grib is None:
                return source, None, None
            return source, herbie_obj, None
        except Exception as exc:
            return source, None, str(exc)

    def _race_source_candidates() -> list[tuple[str, Herbie]]:
        if model.name not in SOURCE_RACE_MODELS or len(candidate_sources) < 2:
            return []
            
        # Check cache for this model run
        cache_key = (model.name, run_id)
        if cache_key in _source_race_cache:
            cached_sources = _source_race_cache[cache_key]
            results = []
            for s in cached_sources:
                if s not in candidate_sources:
                    continue
                try:
                    h = _build_source_herbie(s)
                    if h.grib is not None:
                        results.append((s, h))
                except Exception:
                    continue
            if results:
                return results

        winners: list[tuple[str, Herbie]] = []
        probe_pool = ThreadPoolExecutor(
            max_workers=min(len(candidate_sources), SOURCE_RACE_MAX_THREADS)
        )
        future_map = {
            probe_pool.submit(_probe_source, source): source
            for source in candidate_sources
        }
        try:
            for future in as_completed(future_map, timeout=SOURCE_PROBE_TIMEOUT_SECONDS):
                source = future_map[future]
                try:
                    probed_source, herbie_obj, err = future.result(timeout=5)
                except TimeoutError:
                    logger.debug("[%s] fxx=%02d source probe timed out for %s",
                                 model.name.upper(), fxx, source)
                    continue
                except Exception as exc:
                    logger.debug("[%s] fxx=%02d source probe failed for %s: %s",
                                 model.name.upper(), fxx, source, exc)
                    continue
                if herbie_obj is None:
                    if err:
                        logger.debug("[%s] fxx=%02d source probe failed for %s: %s",
                                     model.name.upper(), fxx, probed_source, err)
                    continue
                winners.append((probed_source, herbie_obj))
                if len(winners) >= SOURCE_RACE_PROBE_LIMIT:
                    break
        except TimeoutError:
            logger.debug("[%s] fxx=%02d source race timed out after %ds",
                         model.name.upper(), fxx, SOURCE_PROBE_TIMEOUT_SECONDS)
        finally:
            probe_pool.shutdown(wait=False, cancel_futures=True)

        if winners:
            logger.info("[%s] fxx=%02d source race winners: %s",
                        model.name.upper(), fxx,
                        ", ".join(source for source, _ in winners))
            # Cache the winning source order for this run
            _source_race_cache[cache_key] = [s for s, _ in winners]
            # Limit cache size
            if len(_source_race_cache) > 50:
                _source_race_cache.clear()
        return winners

    def _download_from_herbie(
        herbie_obj: Herbie, priority: list[str], source_label: str | None = None,
    ) -> Path | None:
        if herbie_obj.grib is None:
            logger.debug("[%s] fxx=%02d grib=None for source=%s",
                         model.name.upper(), fxx, source_label or priority)
            return None
        search_patterns = list(model.param_search.values())
        try:
            local_path = _download_subset_chunked(herbie_obj, search_patterns)
        except Exception as idx_exc:
            _s = str(idx_exc)
            logger.debug("[%s] fxx=%02d subset download error from %s: %s",
                         model.name.upper(), fxx, source_label or priority, _s[:200])
            if "index file" not in _s and ".idx" not in _s and "404" not in _s:
                raise
            # NOMADS idx not ready yet — retry with AWS
            if priority == model.sources and priority != fallback_sources:
                logger.debug("[%s] fxx=%02d NOMADS idx missing, trying %s",
                             model.name.upper(), fxx, fallback_sources)
                fallback_herbie = _build_herbie(fallback_sources)
                if fallback_herbie.grib is None:
                    return None
                local_path = _download_subset_chunked(fallback_herbie, search_patterns)
            else:
                return None

        local_path = Path(local_path)
        if not local_path.exists() or local_path.stat().st_size == 0:
            local_path.unlink(missing_ok=True)
            logger.warning("[%s] fxx=%02d empty subset, discarding",
                           model.name.upper(), fxx)
            return None
        if source_label:
            logger.info("[%s] [DL] Downloaded fxx=%02d from %s",
                        model.name.upper(), fxx, source_label)
        else:
            logger.info("[%s] [DL] Downloaded fxx=%02d", model.name.upper(), fxx)
        return local_path

    # GDPS requires special handling - per-parameter downloads
    if model.name == "gdps":
        from .models.gdps_downloader import download_forecast_hour, GDPS_PARAM_MAP
        
        # Rendered wind products and upper-air maps need hidden components
        # and height fields. The current feed supplies them through 240 hours.
        required_params = set(model.rendered_params)
        for param in model.rendered_params:
            height = _HEIGHT_OVERLAY_DEPENDENCIES.get(param)
            if height:
                required_params.add(height)
            required_params.update(comp for comp in model.derived.get(param, ()) if comp)
        gdps_params = sorted(p for p in required_params if p in GDPS_PARAM_MAP)
        return download_forecast_hour(run_dt, fxx, gdps_params, max_workers=4)

    # RRFS requires special handling — Herbie's template is outdated,
    # download directly from S3 using idx-based subsetting.
    if model.name == "rrfs":
        from .models.rrfs_downloader import download_forecast_hour as rrfs_download
        search_patterns = list(model.param_search.values())
        return rrfs_download(run_dt, fxx, search_patterns)

    # HREF uses separate mean/prob products. Download both and concatenate
    # so the render worker sees one GRIB containing deterministic + probability fields.
    if model.name == "href":
        def _download_href_product(product: str, search_map: dict[str, str], priority: list[str]) -> Path | None:
            patterns = list(search_map.values())
            if not patterns:
                return None
            H = Herbie(
                date=run_dt.strftime("%Y-%m-%d %H:%M"),
                model=model.herbie_model,
                product=product,
                fxx=fxx,
                save_dir=str(GRIB_CACHE_DIR),
                overwrite=False,
                verbose=False,
                priority=priority,
                **model.herbie_kwargs,
            )
            if H.grib is None:
                return None
            return _download_subset_chunked(H, patterns)

        for priority in (model.sources, fallback_sources):
            try:
                part_paths = []
                mean_path = _download_href_product("mean", HREF_MEAN_SEARCH, priority)
                if mean_path is not None:
                    part_paths.append(Path(mean_path))
                prob_path = _download_href_product("prob", HREF_PROB_SEARCH, priority)
                if prob_path is not None:
                    part_paths.append(Path(prob_path))
                if not part_paths:
                    continue
                combined = _combine_grib_parts(
                    part_paths[0],
                    list(HREF_MEAN_SEARCH.values()) + list(HREF_PROB_SEARCH.values()),
                    part_paths,
                )
                logger.info("[%s] [DL] Downloaded fxx=%02d", model.name.upper(), fxx)
                return combined
            except Exception as exc:
                exc_str = str(exc)
                if priority == model.sources and any(
                    token in exc_str for token in ("index file", ".idx", "404")
                ):
                    logger.debug("[%s] fxx=%02d primary HREF source incomplete, trying %s",
                                 model.name.upper(), fxx, fallback_sources)
                    continue
                if priority == fallback_sources:
                    logger.error("[%s] [DL] Download failed fxx=%02d: %s",
                                 model.name.upper(), fxx, exc)
                    return None
        return None

    for attempt in range(1, _max_retries + 1):
        try:
            failed_sources = set()
            selected_source = None
            selected_priority = model.sources
            local_path = None
            source_herbies: dict[str, Herbie] = dict(_race_source_candidates())
            ordered_sources = [source for source, _ in source_herbies.items()]
            ordered_sources.extend(
                source for source in candidate_sources if source not in source_herbies
            )
            if not ordered_sources:
                ordered_sources = candidate_sources

            for source in ordered_sources:
                herbie_obj = source_herbies.get(source)
                if herbie_obj is None:
                    try:
                        herbie_obj = _build_source_herbie(source)
                    except Exception as exc:
                        failed_sources.add(source)
                        logger.info("[%s] fxx=%02d source init failed for %s: %s",
                                    model.name.upper(), fxx, source, exc)
                        continue
                try:
                    local_path = _download_from_herbie(herbie_obj, [source], source)
                except Exception as exc:
                    exc_str = str(exc)
                    logger.warning(
                        "[%s] fxx=%02d source %s failed, trying next source: %s",
                        model.name.upper(), fxx, source, exc_str.splitlines()[0],
                    )
                    continue
                if local_path is not None:
                    selected_source = source
                    selected_priority = [source]
                    break
                logger.info("[%s] fxx=%02d source %s returned no data",
                            model.name.upper(), fxx, source)

            viable_fallback = [source for source in fallback_sources if source not in failed_sources]
            if local_path is None and model.sources != fallback_sources and viable_fallback:
                logger.info("[%s] fxx=%02d all single-source probes unavailable, trying grouped fallback %s",
                            model.name.upper(), fxx, viable_fallback)
                try:
                    fallback_herbie = _build_herbie(viable_fallback)
                except KeyError as exc:
                    if exc.args != ("href",) or "azure" not in viable_fallback:
                        raise
                    # Herbie's Azure signer can return JSON without an href.
                    # All earlier sources were already probed; retry on the
                    # next scan instead of logging a hard download failure.
                    logger.info("[%s] fxx=%02d Azure signer unavailable; retrying next scan",
                                model.name.upper(), fxx)
                    return None
                local_path = _download_from_herbie(
                    fallback_herbie, viable_fallback,
                )
                if local_path is not None:
                    selected_source = ",".join(viable_fallback)
                    selected_priority = viable_fallback

            if local_path is None:
                return None

            # Append prs fields if needed (HRRR SRH — currently a no-op)
            if model.prs_search:
                try:
                    H_prs = Herbie(
                        date=run_dt.strftime("%Y-%m-%d %H:%M"),
                        model=model.herbie_model,
                        product="prs",
                        fxx=fxx,
                        save_dir=str(GRIB_CACHE_DIR),
                        overwrite=False,
                        verbose=False,
                        priority=selected_priority,
                    )
                    prs_path = H_prs.download("|".join(model.prs_search.values()))
                    with open(local_path, "ab") as sfc, open(prs_path, "rb") as prs:
                        sfc.write(prs.read())
                except Exception as exc:
                    logger.warning("[%s] prs append failed fxx=%02d: %s",
                                   model.name.upper(), fxx, exc)

            return Path(local_path)
        except Exception as exc:
            if "503" in str(exc) and attempt < _max_retries:
                wait = 5 * (2 ** (attempt - 1))  # 5s, 10s, 20s
                logger.warning("[%s] fxx=%02d 503 rate-limited, "
                               "retry %d/%d in %ds",
                               model.name.upper(), fxx,
                               attempt, _max_retries, wait)
                time.sleep(wait)
                continue
            exc_str = str(exc)
            is_nomads_network_error = (
                model.sources != fallback_sources
                and any(token in exc_str for token in (
                    "nomads.ncep.noaa.gov",
                    "NameResolutionError",
                    "Failed to resolve",
                    "Temporary failure in name resolution",
                    "Max retries exceeded",
                    "HTTPSConnectionPool",
                    "NewConnectionError",
                    "ConnectTimeoutError",
                    "ReadTimeout",
                ))
            )
            if is_nomads_network_error:
                logger.warning("[%s] fxx=%02d primary source failed, trying %s: %s",
                               model.name.upper(), fxx, fallback_sources, exc_str)
                try:
                    local_path = _download_from_herbie(
                        _build_herbie(fallback_sources), fallback_sources,
                    )
                    if local_path is not None:
                        return local_path
                except Exception as fallback_exc:
                    exc = fallback_exc
                    exc_str = str(fallback_exc)
            if any(s in exc_str for s in ("index file", "grib is None", "not find", ".idx")):
                logger.debug("[%s] fxx=%02d not on server: %s",
                             model.name.upper(), fxx, exc_str.splitlines()[0])
            else:
                logger.error("[%s] [DL] Download failed fxx=%02d: %s",
                             model.name.upper(), fxx, exc)
            return None


# ---------------------------------------------------------------------------
# Orphan cleanup — kill stale render workers from a previous scheduler run
# ---------------------------------------------------------------------------

def _kill_orphan_workers() -> None:
    """Find and kill render worker processes orphaned by a previous instance.

    When the scheduler is OOM-killed or hard-stopped, its
    ProcessPoolExecutor children can survive reparented to PID 1,
    silently consuming memory.  We identify them by:
      - ppid == 1 (reparented to init)
      - cmdline contains 'multiprocessing.spawn' and 'spawn_main'
      - running as the same user
      - NOT our own pid
    """
    import psutil
    my_pid = os.getpid()
    my_uid = os.getuid()
    killed = 0
    for proc in psutil.process_iter(["pid", "ppid", "uids", "cmdline"]):
        try:
            info = proc.info
            if info["ppid"] != 1:
                continue
            if info["pid"] == my_pid:
                continue
            uids = info.get("uids")
            if uids is None or uids.real != my_uid:
                continue
            cmdline = info.get("cmdline") or []
            cmdline_str = " ".join(cmdline)
            if "multiprocessing.spawn" not in cmdline_str:
                continue
            if "spawn_main" not in cmdline_str:
                continue
            logger.warning("Killing orphaned render worker pid=%d: %s",
                           info["pid"], cmdline_str[:120])
            proc.terminate()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        logger.info("Cleaned up %d orphaned render worker(s)", killed)


# ---------------------------------------------------------------------------
# Render worker — runs in a subprocess via ProcessPoolExecutor
# ---------------------------------------------------------------------------

_worker_state = {}
_burst_render_state: dict[str, object] = {}

# ---------------------------------------------------------------------------
# Memory management helpers
# ---------------------------------------------------------------------------

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None


def _release_memory(label: str = "") -> float:
    """Force Python GC and ask glibc to return free pages to the OS."""
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


def _clear_worker_caches() -> None:
    """Drop per-worker warm caches when RSS stays too high between tasks."""
    try:
        renderer = _worker_state.get("renderer")
        if renderer is not None:
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


def _worker_rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


def _system_cpu_percent() -> float:
    try:
        import psutil
        return float(psutil.cpu_percent(interval=0.0))
    except Exception:
        return 100.0


def _system_mem_percent() -> float:
    try:
        import psutil
        return float(psutil.virtual_memory().percent)
    except Exception:
        return 100.0


def _init_render_worker():
    """Called once when the render subprocess starts."""
    import matplotlib
    matplotlib.use("Agg")
    from .processor import FieldProcessor
    from .renderer import OverlayRenderer
    _worker_state["renderer"] = OverlayRenderer()
    _worker_state["processor"] = FieldProcessor()


WORKER_STATUS_DIR = DATA_DIR / "worker_status"


def _write_worker_status(
    model_name: str,
    run_id: str,
    fxx: int,
    *,
    stage: str = "starting",
    detail: str | None = None,
    render_params: int | None = None,
    fetched_fields: int | None = None,
    worker_type: str = "shared",
    burst_active: bool | None = None,
    burst_subworkers: int | None = None,
    burst_reason: str | None = None,
):
    """Write a status file for this worker subprocess."""
    WORKER_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    status = {
        "pid": pid,
        "model": model_name,
        "run_id": run_id,
        "fxx": fxx,
        "stage": stage,
        "started": time.time(),
        "worker_type": worker_type,
        "rss_mb": _worker_rss_mb(),
        "baseline_rss_mb": _worker_state.get("job_baseline_rss_mb", 0),
        "baseline_pss_mb": _worker_state.get("job_baseline_pss_mb", 0),
        "child_pids": _worker_state.get("render_child_pids", []),
    }
    if detail:
        status["detail"] = detail
    if render_params is not None:
        status["render_params"] = render_params
    if fetched_fields is not None:
        status["fetched_fields"] = fetched_fields
    if burst_active is not None:
        status["burst_active"] = bool(burst_active)
    if burst_subworkers is not None:
        status["burst_subworkers"] = int(burst_subworkers)
    if burst_reason:
        status["burst_reason"] = burst_reason
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


def _set_render_children(child_pids):
    """Expose the actual process family for proportional memory accounting."""
    _worker_state["render_child_pids"] = list(child_pids)
    path = WORKER_STATUS_DIR / f"{os.getpid()}.json"
    try:
        status = json.loads(path.read_text())
        status["child_pids"] = list(child_pids)
        path.write_text(json.dumps(status))
    except (OSError, ValueError):
        pass


def _chunk_render_params(params: list[str], chunks: int) -> list[list[str]]:
    chunked = [[] for _ in range(chunks)]
    for idx, param in enumerate(params):
        chunked[idx % chunks].append(param)
    return [chunk for chunk in chunked if chunk]


def _render_cost_profile():
    if "render_costs" not in _worker_state:
        _worker_state["render_costs"] = RenderCosts(DATA_DIR / "render_costs.json")
    return _worker_state["render_costs"]


def _expand_chunk_fields(chunk_params: list[str], processed_keys: set[str]) -> set[str]:
    from .processor import BARB_COMPONENTS as _BARB_COMPONENTS
    from .processor import DERIVED_PARAMS as _ALL_DERIVED

    needed = set(chunk_params)
    for param in chunk_params:
        contour_param = _HEIGHT_OVERLAY_DEPENDENCIES.get(param)
        if contour_param and contour_param in processed_keys:
            needed.add(contour_param)
        # Both tables can name a product (GDPS aliases wind_gusts in DERIVED).
        components = (*(_ALL_DERIVED.get(param) or ()), *(_BARB_COMPONENTS.get(param) or ()))
        if components:
            for comp in components:
                if comp is not None and comp in processed_keys:
                    needed.add(comp)
    return needed


def _burst_render_child(
    chunk_params: list[str],
    output_dir_str: str,
    fxx: int,
    model_name: str,
    result_queue,
):
    try:
        from .renderer import OverlayRenderer

        processed = _burst_render_state.get("processed")
        if not isinstance(processed, dict):
            result_queue.put({"ok": False, "params": chunk_params, "error": "missing processed state"})
            return

        needed_fields = _expand_chunk_fields(chunk_params, set(processed))
        subset_processed = {
            key: processed[key]
            for key in needed_fields
            if key in processed
        }
        renderer = _worker_state.get("renderer") or OverlayRenderer()
        rendered = renderer.render_all_params(
            subset_processed,
            Path(output_dir_str),
            fxx,
            model_name=model_name,
            selected_params=set(chunk_params),
            shared_height_cache=True,
        )
        result_queue.put({"ok": True, "params": chunk_params, "rendered": rendered,
                          "costs": renderer.last_render_costs})
    except Exception as exc:
        result_queue.put({"ok": False, "params": chunk_params, "error": str(exc)})


def _render_with_burst_subworkers(
    processed: dict,
    output_dir: Path,
    fxx: int,
    run_dt: datetime,
    render_param_names: list[str],
    model_name: str,
    subworkers: int,
) -> list[str] | None:
    if subworkers <= 1:
        return None
    from .processor import DERIVED_PARAMS

    component_fields = {
        key for pair in DERIVED_PARAMS.values() for key in pair if key is not None
    } | {"ushear_06km", "vshear_06km", "ushear_01km", "vshear_01km"}
    # Sequential rendering also emits retained contour dependencies (gh_*).
    # Include those outputs when splitting, while keeping vector components
    # as dependencies of their wind products rather than separate jobs.
    eligible = [
        param for param in dict.fromkeys([*render_param_names, *processed])
        if param in processed and param not in component_fields
    ]
    if len(eligible) < BURST_RENDER_MIN_PARAMS:
        return None
    try:
        ctx = mp.get_context("fork")
    except ValueError:
        logger.info("[%s] fxx=%02d burst disabled: fork context unavailable",
                    model_name.upper(), fxx)
        return None

    costs = _render_cost_profile()
    chunks = balanced_chunks(eligible, min(subworkers, len(eligible)),
                             costs.estimates(model_name, eligible))
    if len(chunks) <= 1:
        return None

    renderer = _worker_state.get("renderer")
    if renderer is not None:
        renderer.prepare_shared_heights(processed, chunks, model_name)

    _burst_render_state["processed"] = processed
    result_queue = ctx.Queue()
    procs = []
    logger.info("[%s] fxx=%02d burst render start: %d subworkers, %d params, chunks=%s",
                model_name.upper(), fxx, len(chunks), len(eligible),
                [len(chunk) for chunk in chunks])
    try:
        for chunk in chunks:
            proc = ctx.Process(
                target=_burst_render_child,
                args=(chunk, str(output_dir), fxx, model_name, result_queue),
                daemon=False,
            )
            proc.start()
            procs.append(proc)

        _set_render_children([proc.pid for proc in procs])

        rendered: list[str] = []
        failures: list[str] = []
        measured_costs = {}
        deadline = time.monotonic() + BURST_RENDER_TIMEOUT_SECONDS
        for _ in chunks:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failures.append("child result timeout")
                    break
                try:
                    msg = result_queue.get(timeout=min(1.0, remaining))
                    break
                except queue.Empty:
                    if any(proc.exitcode not in (None, 0) for proc in procs):
                        failures.append("render child exited without a result")
                        break
                    if all(proc.exitcode == 0 for proc in procs):
                        failures.append("missing render child result")
                        break
            if failures:
                break
            if msg.get("ok"):
                rendered.extend(msg.get("rendered", []))
                measured_costs.update(msg.get("costs", {}))
            else:
                failures.append(
                    f"{','.join(msg.get('params', []))}: {msg.get('error', 'unknown error')}"
                )

        for proc in procs:
            proc.join(timeout=1)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1)
                failures.append(f"pid {proc.pid} timeout")
            elif proc.exitcode not in (0, None):
                failures.append(f"pid {proc.pid} exit={proc.exitcode}")

        if failures:
            logger.warning("[%s] fxx=%02d burst render failed, falling back to sequential: %s",
                           model_name.upper(), fxx, "; ".join(failures[:6]))
            return None

        logger.info("[%s] fxx=%02d burst render complete: %d params via %d subworkers",
                    model_name.upper(), fxx, len(rendered), len(chunks))
        costs.update(model_name, measured_costs)
        return sorted(set(rendered))
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=1)
        _set_render_children([])
        _burst_render_state.pop("processed", None)
        if renderer is not None:
            renderer.clear_hour_cache()
        try:
            result_queue.close()
        except Exception:
            pass


def _render_single_fxx_impl(
    grib_path: Path,
    fxx: int,
    out_dir: Path,
    run_dt: datetime,
    render_param_names: list[str],
    extra_param_names: list[str],
    prev_grib: Path | None,
    lookback_gribs: dict[int, Path] | None,
    model_name: str,
    worker_type: str = "shared",
    burst_subworkers: int = 1,
    burst_reason: str | None = None,
) -> tuple[int, list[str]]:
    """Core render logic for one forecast hour. Runs in subprocess."""
    run_id = run_dt.strftime("%Y%m%d%H")
    _worker_state["job_baseline_rss_mb"] = _worker_rss_mb()
    burst_active = burst_subworkers > 1
    _write_worker_status(
        model_name,
        run_id,
        fxx,
        stage="starting",
        worker_type=worker_type,
        burst_active=burst_active,
        burst_subworkers=burst_subworkers,
        burst_reason=burst_reason,
    )

    if not grib_path.exists():
        _write_worker_status(
            model_name,
            run_id,
            fxx,
            stage="missing_grib",
            worker_type=worker_type,
            burst_active=burst_active,
            burst_subworkers=burst_subworkers,
            burst_reason=burst_reason,
        )
        _clear_worker_status()
        return fxx, []

    renderer = _worker_state["renderer"]
    processor = _worker_state["processor"]

    try:
        rss_before = _worker_rss_mb()
        _write_worker_status(
            model_name,
            run_id,
            fxx,
            stage="processing_grib",
            worker_type=worker_type,
            burst_active=burst_active,
            burst_subworkers=burst_subworkers,
            burst_reason=burst_reason,
        )
        logger.info("[%s] fxx=%02d worker start (RSS %.0f MB)",
                    model_name.upper(), fxx, rss_before)
        hidden_contour_params = [
            contour_param
            for contour_param in (
                _HEIGHT_OVERLAY_DEPENDENCIES.get(param) for param in render_param_names
            )
            if contour_param is not None
        ]
        fetch_param_names = list(
            dict.fromkeys([*render_param_names, *extra_param_names, *hidden_contour_params])
        )
        processed = processor.process_for_render(
            grib_path, fetch_param_names, fxx, prev_grib, lookback_gribs,
        )
        rss_after_process = _worker_rss_mb()
        _write_worker_status(
            model_name,
            run_id,
            fxx,
            stage="rendering",
            render_params=len(render_param_names),
            fetched_fields=len(processed),
            worker_type=worker_type,
            burst_active=burst_active,
            burst_subworkers=burst_subworkers,
            burst_reason=burst_reason,
        )
        logger.info("[%s] fxx=%02d processed fields (RSS %.0f -> %.0f MB)",
                    model_name.upper(), fxx, rss_before, rss_after_process)
        _write_sounding_cache(processed, out_dir, fxx, model_name)
        _write_ptype_grid(renderer, processed, out_dir, fxx, model_name)
        # Keep u/v components needed by wind overlays even if they came
        # in via extra_param_names (sounding cache).  Without them,
        # render_all_params cannot produce wind barb overlays.
        from .processor import DERIVED_PARAMS as _ALL_DERIVED
        _needed_for_render = set()
        for _wp, (_uk, _vk) in _ALL_DERIVED.items():
            if _wp in render_param_names and _vk is not None:
                _needed_for_render.update((_uk, _vk, _wp))
        for _param in render_param_names:
            _contour_param = _HEIGHT_OVERLAY_DEPENDENCIES.get(_param)
            if _contour_param is not None:
                _needed_for_render.add(_contour_param)
        _extra_only = set(extra_param_names) - _needed_for_render
        render_processed = {
            key: value for key, value in processed.items()
            if key not in _extra_only
        }
        del processed  # release reference early — render_processed has what it needs
        rss_before_render = _worker_rss_mb()
        logger.info("[%s] fxx=%02d renderer start (RSS %.0f MB, %d render params, %d fetched fields)",
                    model_name.upper(), fxx, rss_before_render,
                    len(render_param_names), len(render_processed))
        rendered = _render_with_burst_subworkers(
            render_processed,
            out_dir,
            fxx,
            run_dt,
            render_param_names,
            model_name,
            burst_subworkers,
        )
        if rendered is None:
            rendered = renderer.render_all_params(
                render_processed,
                out_dir,
                fxx,
                run_dt,
                model_name=model_name,
            )
            _render_cost_profile().update(model_name, renderer.last_render_costs)
        rss_after_render = _worker_rss_mb()
        _write_worker_status(
            model_name,
            run_id,
            fxx,
            stage="cleanup",
            render_params=len(render_param_names),
            fetched_fields=len(render_processed),
            detail=f"rendered={len(rendered)}",
            worker_type=worker_type,
            burst_active=burst_active,
            burst_subworkers=burst_subworkers,
            burst_reason=burst_reason,
        )
        logger.info("[%s] fxx=%02d rendered %d params (RSS %.0f -> %.0f MB)",
                    model_name.upper(), fxx, len(rendered), rss_before_render, rss_after_render)
    except FileNotFoundError:
        _write_worker_status(
            model_name,
            run_id,
            fxx,
            stage="missing_input",
            worker_type=worker_type,
            burst_active=burst_active,
            burst_subworkers=burst_subworkers,
            burst_reason=burst_reason,
        )
        _clear_worker_status()
        return fxx, []

    try:
        del render_processed
    except Exception:
        pass
    rss_after_cleanup = _release_memory(f"{model_name.upper()} fxx={fxx:02d} worker cleanup")
    if rss_after_cleanup >= WORKER_CACHE_CLEAR_RSS_MB:
        _clear_worker_caches()
        _release_memory(
            f"{model_name.upper()} fxx={fxx:02d} worker cache-drop cleanup"
        )
    _clear_worker_status()
    return fxx, sorted(rendered)


def _render_fxx(
    grib_path_str: str,
    fxx: int,
    out_dir_str: str,
    run_dt_iso: str,
    render_param_names: list[str],
    extra_param_names: list[str],
    prev_grib_str: str | None,
    lookback_strs: dict[str, str] | None,
    model_name: str = "",
    worker_type: str = "shared",
    burst_subworkers: int = 1,
    burst_reason: str | None = None,
) -> tuple[int, list[str]]:
    """Render one forecast hour. Thin wrapper for backward compat."""
    run_dt = datetime.fromisoformat(run_dt_iso)
    grib_path = Path(grib_path_str)
    out_dir = Path(out_dir_str)
    prev_grib = Path(prev_grib_str) if prev_grib_str else None
    lookback_gribs = (
        {int(k): Path(v) for k, v in lookback_strs.items()}
        if lookback_strs is not None else None
    )
    return _render_single_fxx_impl(
        grib_path, fxx, out_dir, run_dt,
        render_param_names, extra_param_names,
        prev_grib, lookback_gribs, model_name,
        worker_type=worker_type,
        burst_subworkers=burst_subworkers,
        burst_reason=burst_reason,
    )


def _render_model_batch(
    batch_items: list[dict],
    out_dir_str: str,
    run_dt_iso: str,
    render_param_names: list[str],
    extra_param_names: list[str],
    model_name: str,
    worker_type: str = "shared",
    burst_subworkers: int = 1,
    burst_reason: str | None = None,
) -> list[tuple[int, list[str]]]:
    """Render a batch of forecast hours for one model in a single worker.

    The worker stays on this model for the entire batch, keeping regrid
    caches warm.  Breaks early if RSS exceeds WORKER_RSS_LIMIT_MB.
    """
    run_dt = datetime.fromisoformat(run_dt_iso)
    out_dir = Path(out_dir_str)
    results: list[tuple[int, list[str]]] = []

    for item in batch_items:
        fxx = item["fxx"]
        grib_path = Path(item["grib_path_str"])
        prev_grib = Path(item["prev_grib_str"]) if item.get("prev_grib_str") else None
        lookback_strs = item.get("lookback_strs")
        lookback_gribs = (
            {int(k): Path(v) for k, v in lookback_strs.items()}
            if lookback_strs is not None else None
        )

        baseline = _worker_rss_mb()
        peak = [baseline]
        baseline_pss = process_group_pss(os.getpid()) / 1024**2
        _worker_state["job_baseline_pss_mb"] = baseline_pss
        _worker_state["render_child_pids"] = []
        peak_pss = [baseline_pss]
        sampling_stop = threading.Event()
        def sample_memory():
            while not sampling_stop.wait(1):
                peak[0] = max(peak[0], _worker_rss_mb())
                peak_pss[0] = max(peak_pss[0], process_group_pss(
                    os.getpid(), _worker_state.get("render_child_pids", []),
                ) / 1024**2)
        sampler = threading.Thread(target=sample_memory, daemon=True)
        sampler.start()
        started = time.monotonic()
        cpu_before = resource.getrusage(resource.RUSAGE_SELF)
        child_cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            result = _render_single_fxx_impl(
                grib_path, fxx, out_dir, run_dt,
                render_param_names, extra_param_names,
                prev_grib, lookback_gribs, model_name,
                worker_type=worker_type,
                burst_subworkers=burst_subworkers,
                burst_reason=burst_reason,
            )
        finally:
            sampling_stop.set()
            sampler.join(timeout=1)
        metrics = {"baseline_rss_mb": baseline, "peak_rss_mb": peak[0],
                   "baseline_pss_mb": baseline_pss, "peak_group_pss_mb": peak_pss[0],
                   "elapsed_seconds": round(time.monotonic() - started, 2)}
        cpu_after = resource.getrusage(resource.RUSAGE_SELF)
        child_cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        child_cpu = (child_cpu_after.ru_utime + child_cpu_after.ru_stime
                     - child_cpu_before.ru_utime - child_cpu_before.ru_stime)
        metrics["cpu_seconds"] = round(cpu_after.ru_utime + cpu_after.ru_stime
                                       - cpu_before.ru_utime - cpu_before.ru_stime
                                       + child_cpu, 2)
        metrics["child_cpu_seconds"] = round(child_cpu, 2)
        metrics["burst_subworkers"] = burst_subworkers
        metrics["rendered_params"] = len(result[1])
        # Existing schedulers may spawn a worker while a rollout is staged.
        # Enable the extended result protocol explicitly with the new service.
        results.append((*result, metrics) if MODEL_TASK_METRICS else result)
        logger.info("[%s] fxx=%02d task metrics: %s", model_name.upper(), fxx, metrics)

        # Check memory pressure and trim/clear caches
        rss = _worker_rss_mb()
        if rss > WORKER_CACHE_CLEAR_RSS_MB:
            logger.info("[%s] fxx=%02d worker cleanup — high RSS: %.0f MB",
                        model_name.upper(), fxx, rss)
            _clear_worker_caches()
            rss = _release_memory(f"{model_name.upper()} fxx={fxx:02d} worker cleanup")
        else:
            # Always trim to return free pages to OS even if not clearing caches
            _release_memory()
            rss = _worker_rss_mb()

        if rss > WORKER_RSS_LIMIT_MB:
            logger.warning("[%s] Worker RSS %.0f MB exceeds %d MB limit, ending batch early "
                           "(%d/%d done)", model_name.upper(), rss, WORKER_RSS_LIMIT_MB,
                           len(results), len(batch_items))
            break

    return results



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_dir(model_name: str, run_dt: datetime) -> Path:
    return RENDERED_DIR / model_name / run_dt.strftime("%Y%m%d%H")


_RUN_PNG_INDEX_TTL = 30.0
_FXX_COMPLETE_TTL = 30.0
_COMPLETED_RUNS_TTL = 60.0
_RECENT_COMPLETIONS_TTL = 60.0
_GRIB_CACHE_HIT_TTL = 300.0
_GRIB_CACHE_MISS_TTL = 30.0
_GRIB_DIR_SNAPSHOT_TTL = 10.0
_GRIB_DIR_SNAPSHOT_MAX = 64

_run_png_index_cache: dict[str, tuple[float, dict[str, set[str]]]] = {}
_fxx_complete_cache: dict[tuple[str, int], tuple[float, bool]] = {}
_completed_runs_cache: dict[str, tuple[float, set[str]]] = {}
_recent_completed_cache: tuple[float, set[str]] | None = None
_grib_lookup_cache: dict[tuple[str, str, int], tuple[float, Path | None]] = {}
# Per-cache-dir scandir snapshot: canonical GRIB name -> best on-disk path.
_grib_dir_snapshot_cache: dict[str, tuple[float, dict[str, Path]]] = {}


def _grib_dir_index(cache_dir: Path) -> dict[str, Path]:
    """One scandir pass over a GRIB cache dir, mapping canonical filename to
    the best matching file (direct download preferred, else largest subset).

    Replaces per-fxx glob+stat scans; refreshed every _GRIB_DIR_SNAPSHOT_TTL.
    """
    key = str(cache_dir)
    now = time.time()
    cached = _grib_dir_snapshot_cache.get(key)
    if cached is not None and (now - cached[0]) < _GRIB_DIR_SNAPSHOT_TTL:
        return cached[1]

    best: dict[str, tuple[tuple, Path]] = {}
    try:
        with os.scandir(cache_dir) as entries:
            for entry in entries:
                name = entry.name
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_size <= 0:
                    continue
                if name.startswith("subset_") and "__" in name:
                    canonical = name.split("__", 1)[1]
                    rank = (0, st.st_size, st.st_mtime)
                else:
                    canonical = name
                    rank = (1, st.st_size, st.st_mtime)  # direct beats subsets
                current = best.get(canonical)
                if current is None or rank > current[0]:
                    best[canonical] = (rank, Path(entry.path))
    except OSError:
        pass

    index = {canonical: path for canonical, (_, path) in best.items()}
    _grib_dir_snapshot_cache[key] = (now, index)
    while len(_grib_dir_snapshot_cache) > _GRIB_DIR_SNAPSHOT_MAX:
        _grib_dir_snapshot_cache.pop(next(iter(_grib_dir_snapshot_cache)))
    return index


def _invalidate_run_completion_cache(model_name: str, run_id: str | None = None, fxx: int | None = None):
    """Drop cached run-completion state after new outputs or metadata writes."""
    _completed_runs_cache.pop(model_name, None)
    global _recent_completed_cache
    _recent_completed_cache = None
    if run_id is None:
        return

    out_dir_key = str(RENDERED_DIR / model_name / run_id)
    _run_png_index_cache.pop(out_dir_key, None)
    _fxx_completion_cache.pop(out_dir_key, None)

    if fxx is not None:
        _fxx_complete_cache.pop((out_dir_key, fxx), None)
        return

    stale_keys = [key for key in _fxx_complete_cache if key[0] == out_dir_key]
    for key in stale_keys:
        _fxx_complete_cache.pop(key, None)


def _get_run_png_index(out_dir: Path, rendered_params: list[str]) -> dict[str, set[str]]:
    """Return a short-lived index of PNG files by parameter directory."""
    now = time.time()
    cache_key = str(out_dir)
    cached = _run_png_index_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    png_index: dict[str, set[str]] = {}
    if out_dir.exists():
        rendered_params_set = set(rendered_params)
        try:
            with os.scandir(out_dir) as entries:
                for entry in entries:
                    if entry.is_dir() and entry.name in rendered_params_set:
                        try:
                            with os.scandir(entry.path) as png_entries:
                                names = {
                                    pe.name for pe in png_entries
                                    if pe.is_file() and pe.name.endswith(".png")
                                }
                                if names:
                                    png_index[entry.name] = names
                        except OSError:
                            continue
        except OSError:
            pass

    _run_png_index_cache[cache_key] = (now + _RUN_PNG_INDEX_TTL, png_index)
    return png_index


_fxx_completion_cache: dict[str, tuple[float, set[int]]] = {}

def _get_completed_fxx_for_run(out_dir: Path, model: ModelConfig) -> set[int]:
    """Return the set of forecast hours already fully rendered on disk."""
    now = time.time()
    cache_key = str(out_dir)
    cached = _fxx_completion_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    fxx_list = list(model.get_fxx(int(out_dir.name[8:10])))
    png_index = _get_run_png_index(out_dir, model.rendered_params)
    
    if not png_index:
        _fxx_completion_cache[cache_key] = (now + _FXX_COMPLETE_TTL, set())
        return set()

    # Determine if this is a single-fxx model (analysis only)
    is_single_fxx = len(fxx_list) == 1 and fxx_list[0] == 0
    min_files_per_param = 1 if is_single_fxx else 3

    # Filter params that actually have enough files to be considered "active"
    active_params = [
        p for p in model.rendered_params
        if p in png_index and len(png_index[p]) >= min_files_per_param
    ]
    
    if not active_params:
        _fxx_completion_cache[cache_key] = (now + _FXX_COMPLETE_TTL, set())
        return set()

    completed = set()
    for fxx in fxx_list:
        fname = f"f{fxx:02d}.png"
        found_count = 0
        checked_count = 0
        
        for param in active_params:
            if model.snow_products:
                min_fxx = SNOW_FXX_MIN.get(param, 0)
                if fxx < min_fxx:
                    continue
            
            checked_count += 1
            if fname in png_index[param]:
                found_count += 1
        
        if checked_count > 0 and found_count >= checked_count * 0.8:
            completed.add(fxx)

    _fxx_completion_cache[cache_key] = (now + _FXX_COMPLETE_TTL, completed)
    return completed


def _is_fxx_complete(
    out_dir: Path, fxx: int,
    rendered_params: list[str], has_snow: bool,
) -> bool:
    """True if expected PNGs exist for this forecast hour."""
    # This is now a lightweight wrapper around the cached set
    model_name = out_dir.parent.name
    model = MODEL_MAP.get(model_name)
    if not model:
        return False
        
    completed_fxx = _get_completed_fxx_for_run(out_dir, model)
    return fxx in completed_fxx


def _write_meta(model_name: str, run_dt: datetime, meta: dict):
    d = _run_dir(model_name, run_dt)
    d.mkdir(parents=True, exist_ok=True, mode=0o755)
    (d / "run_meta.json").write_text(json.dumps(meta, indent=2, default=str))
    _invalidate_run_completion_cache(model_name, run_dt.strftime("%Y%m%d%H"))


# source_param -> swath_param, per model
_SWATH_PARAMS: dict[str, dict[str, str]] = {
    "hrrr":   {"uh_25km": "uh_25km_swath", "uh_03km": "uh_03km_swath"},
    "nam3km": {"uh_25km": "uh_25km_swath", "uh_03km": "uh_03km_swath"},
    "rrfs":   {"uh_25km": "uh_25km_swath"},
}


def _compute_swath_products(model_name: str, run_dir: Path) -> None:
    """Fill any remaining gaps at run completion."""
    pairs = _SWATH_PARAMS.get(model_name, {})
    for source_param, swath_param in pairs.items():
        finalize_swath(run_dir, source_param, swath_param)


def _prune_native_sounding_runs(model_name: str, keep_run_id: str):
    model_dir = RENDERED_DIR / model_name
    if not model_dir.exists():
        return
    for run_dir in model_dir.iterdir():
        if not run_dir.is_dir() or run_dir.name == keep_run_id:
            continue
        meta_path = run_dir / "run_meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if not meta.get("is_complete"):
            continue
        native_dir = run_dir / _FULL_SOUNDING_DIRNAME
        if native_dir.exists():
            shutil.rmtree(native_dir, ignore_errors=True)


def _write_state(state: dict):
    try:
        PIPELINE_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as exc:
        logger.error("Failed to write pipeline state: %s", exc)




def _finalize(
    model: ModelConfig,
    run_dt: datetime,
    fxx_list: list[int],
    completed: list[int],
    skipped: list[int] | None = None,
):
    """Write final metadata and pipeline state for a run."""
    skipped = sorted(set(skipped or []))
    run_id = run_dt.strftime("%Y%m%d%H")
    is_complete = (set(completed) | set(skipped)) >= set(fxx_list)

    # Preserve last_updated and best_effort from existing metadata.
    last_updated = datetime.now(timezone.utc).isoformat()
    best_effort = False
    existing_meta_path = _run_dir(model.name, run_dt) / "run_meta.json"
    if existing_meta_path.exists():
        try:
            prev = json.loads(existing_meta_path.read_text())
            prev_completed = set(int(f) for f in prev.get("completed_fxx", []))
            if set(completed) == prev_completed and prev.get("last_updated"):
                # No new progress — keep the original timestamp
                last_updated = prev["last_updated"]
            best_effort = bool(prev.get("best_effort"))
            if best_effort:
                is_complete = True
        except Exception:
            pass

    meta = {
        "model": model.name,
        "run_id": run_id,
        "init_time": run_dt.strftime("%Y-%m-%dT%H:00:00Z"),
        "parameters": model.rendered_params,
        "forecast_hours": fxx_list,
        "completed_fxx": sorted(completed),
        "skipped_fxx": skipped,
        "is_complete": is_complete,
        "processing_started": datetime.now(timezone.utc).isoformat(),
        "last_updated": last_updated,
        "palette_version": CURRENT_PALETTE_VERSION,
    }
    if best_effort:
        meta["best_effort"] = True
    _write_meta(model.name, run_dt, meta)
    _write_state({
        "status": "idle",
        "last_run": f"{model.name}/{run_id}",
        "model": model.name,
        "completed_fxx": sorted(completed),
        "total_fxx": len(fxx_list),
        "is_complete": is_complete,
    })
    # Track this completion to prevent re-processing after cleanup
    if is_complete:
        _add_recent_completion(model.name, run_id)
        if model.sounding_cache:
            _prune_native_sounding_runs(model.name, run_id)
        _compute_swath_products(model.name, _run_dir(model.name, run_dt))
    logger.info("[%s] Run %s: %s (%d/%d fxx)",
                model.name.upper(), run_id,
                "complete" if is_complete else "partial",
                len(completed), len(fxx_list))


# ---------------------------------------------------------------------------
# Run detection — prioritized across all models
# ---------------------------------------------------------------------------

_check_available_cache: dict[tuple[str, str], tuple[float, bool]] = {}
_CHECK_AVAILABLE_TTL_POSITIVE = 300.0  # 5 minutes
_CHECK_AVAILABLE_TTL_NEGATIVE = 60.0   # 1 minute — fast retry when waiting for new data


def _check_available(model: ModelConfig, run_dt: datetime) -> bool:
    """Check if the model's earliest forecast hour is available on the server."""
    run_id = run_dt.strftime("%Y%m%d%H")
    cache_key = (model.name, run_id)
    now = time.time()
    
    cached = _check_available_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    first_fxx_list = list(model.get_fxx(run_dt.hour))
    probe_fxx = int(first_fxx_list[0]) if first_fxx_list else 0
    
    available = False
    try:
        # GDPS requires special handling
        if model.name == "gdps":
            from .models.gdps_downloader import check_availability
            available = check_availability(run_dt, fxx=probe_fxx)
        # RRFS: bypass Herbie, check S3 directly
        elif model.name == "rrfs":
            from .models.rrfs_downloader import check_availability as rrfs_avail
            available = rrfs_avail(run_dt, fxx=probe_fxx)
        else:
            probe_candidates = [probe_fxx]
            if probe_fxx != 0:
                probe_candidates.append(0)
            if probe_fxx != 1:
                probe_candidates.append(1)

            for fxx in probe_candidates:
                try:
                    H = Herbie(
                        date=run_dt.strftime("%Y-%m-%d %H:%M"),
                        model=model.herbie_model,
                        product=model.herbie_product,
                        fxx=fxx, verbose=False,
                        priority=model.sources,
                        **model.herbie_kwargs,
                    )
                    if H.grib is None:
                        continue
                    # Also verify the idx file is accessible — without it,
                    # subset downloads will fail even though the GRIB exists.
                    try:
                        H.inventory()
                    except Exception:
                        continue
                    available = True
                    break
                except Exception:
                    continue
    except Exception:
        pass

    ttl = _CHECK_AVAILABLE_TTL_POSITIVE if available else _CHECK_AVAILABLE_TTL_NEGATIVE
    _check_available_cache[cache_key] = (now + ttl, available)
    return available


def _completed_run_ids(model_name: str) -> set[str]:
    """Return set of run_ids that are fully complete on disk."""
    now = time.time()
    cached = _completed_runs_cache.get(model_name)
    if cached is not None and cached[0] > now:
        return set(cached[1])

    completed = set()
    model_dir = RENDERED_DIR / model_name
    model = MODEL_MAP.get(model_name)
    if not model_dir.exists():
        return completed
    
    # Only scan the most recent 30 runs. Older runs are irrelevant for current
    # scheduling and will be caught by the retention cleanup anyway.
    try:
        entries = sorted(
            [e for e in os.scandir(model_dir) if e.is_dir() and e.name.isdigit()],
            key=lambda e: e.name,
            reverse=True
        )[:30]
    except OSError:
        return completed

    for entry in entries:
        rd = Path(entry.path)
        meta_path = rd / "run_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                fxx_list = _canonical_forecast_hours(model, entry.name, meta)
                meta_completed = {int(f) for f in meta.get("completed_fxx", [])}
                meta_complete = bool(fxx_list) and meta_completed >= set(fxx_list)
                if meta.get("abandoned"):
                    completed.add(entry.name)
                    continue
                if meta.get("is_complete") and meta_complete:
                    completed.add(entry.name)
                    continue
                disk_complete = bool(fxx_list) and model is not None and all(
                    _is_fxx_complete(rd, fxx, model.rendered_params, model.snow_products)
                    for fxx in fxx_list
                )
                if meta.get("is_complete") and disk_complete:
                    completed.add(entry.name)
            except Exception:
                pass
    _completed_runs_cache[model_name] = (now + _COMPLETED_RUNS_TTL, set(completed))
    return completed


def _is_run_actually_complete(model_name: str, run_id: str, fxx_list: list[int]) -> bool:
    """Check if all fxx PNGs actually exist on disk (ignores metadata).
    
    Special case: Single-fxx models (analysis only) check for 1 file per param.
    """
    out_dir = RENDERED_DIR / model_name / run_id
    if not out_dir.exists():
        return False
    
    # Get the model's rendered params to check
    model = MODEL_MAP.get(model_name)
    if not model:
        return False
    
    # Determine if this is a single-fxx model (analysis only)
    # These models only have f00.png files
    is_single_fxx = len(fxx_list) == 1 and fxx_list[0] == 0
    
    # For single-fxx models, check if f00.png exists for key params
    if is_single_fxx:
        key_params = ["temp_2m", "psfc", "wind_speed"]
        found_count = 0
        for param in key_params:
            if (out_dir / param / "f00.png").exists():
                found_count += 1
        # For single-fxx, require at least 1 param to exist
        return found_count >= 1
    
    # Check if at least 80% of fxx have PNGs (handles partial availability)
    found_count = 0
    for fxx in fxx_list:
        fname = f"f{fxx:02d}.png"
        # Check a few key params that should always exist
        for param in ["temp_2m", "mslp", "reflectivity"]:
            if (out_dir / param / fname).exists():
                found_count += 1
                break
    
    return found_count >= len(fxx_list) * 0.8


# File to track recent completions (separate from pipeline_state to avoid overwrites)
RECENT_COMPLETIONS_FILE = DATA_DIR / "recent_completions.json"


def _get_recently_completed_runs() -> set[str]:
    """Get set of run_ids that were recently completed AND still exist on disk.
    
    Returns run_ids in format "model/run_id" that completed recently
    to prevent re-processing immediately after cleanup deletes old runs.
    Only includes runs whose directories still exist on disk.
    """
    global _recent_completed_cache
    now = time.time()
    if _recent_completed_cache is not None and _recent_completed_cache[0] > now:
        return set(_recent_completed_cache[1])

    recent = set()
    try:
        if RECENT_COMPLETIONS_FILE.exists():
            data = json.loads(RECENT_COMPLETIONS_FILE.read_text())
            # Keep entries from the last 2 hours
            cutoff = now - 2 * 3600
            for entry in data.get("recent", []):
                if entry.get("timestamp", 0) > cutoff:
                    run_id_full = entry.get("run_id", "")
                    # Verify the directory still exists on disk
                    if "/" in run_id_full:
                        model_name, run_id = run_id_full.split("/", 1)
                        run_dir = RENDERED_DIR / model_name / run_id
                        if run_dir.exists():
                            recent.add(run_id_full)
                    else:
                        # Old format without model prefix - include but don't verify
                        recent.add(run_id_full)
    except Exception:
        pass
    _recent_completed_cache = (now + _RECENT_COMPLETIONS_TTL, set(recent))
    return recent


def _add_recent_completion(model_name: str, run_id: str):
    """Add a completed run to the recent completions tracking."""
    try:
        recent = []
        if RECENT_COMPLETIONS_FILE.exists():
            try:
                data = json.loads(RECENT_COMPLETIONS_FILE.read_text())
                recent = data.get("recent", [])
            except Exception:
                pass
        
        # Add new completion
        recent.append({
            "run_id": f"{model_name}/{run_id}",
            "timestamp": time.time(),
        })
        
        # Keep only last 20 entries
        recent = recent[-20:]
        
        RECENT_COMPLETIONS_FILE.write_text(json.dumps({"recent": recent}))
        _invalidate_run_completion_cache(model_name, run_id)
    except Exception:
        pass


def _detect_pending(model: ModelConfig) -> datetime | None:
    """Find the most recent incomplete or new run for this model.

    Never catches up on old runs — always picks the freshest available run.
    Falls back to an in-progress run only if there is no newer available run.
    """
    model_dir = RENDERED_DIR / model.name
    completed = _completed_run_ids(model.name)

    # Get recently completed runs to avoid re-processing after cleanup
    recent_completed = _get_recently_completed_runs()
    recent_run_id = f"{model.name}/"  # prefix for this model's runs

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Lookback: just enough to catch the most recent run cycle per model
    if model.name in ("hrrr", "rap", "rtma"):
        # Hourly models often publish late enough that a 2-hour window can
        # skip a run entirely once the clock rolls over to the next cycle.
        lookback = 3
    elif model.name in ("nam3km", "nam12km"):
        lookback = 7          # 6h-cycle models — 7h finds the latest cycle
    elif model.name == "gdps":
        # GDPS downloads separate files for each parameter and has 81 forecast
        # hours. Keep an interrupted run eligible until the following day so
        # the 13-hour cutoff cannot abandon it before a replacement is ready.
        lookback = 25
    else:
        lookback = 13         # 12h-cycle models (ecmwf, gfs)
    cutoff_ts = calendar.timegm(
        now.replace(minute=0, second=0, microsecond=0).timetuple()
    ) - lookback * 3600
    cutoff_id = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y%m%d%H")

    # Track the newest in-progress run as fallback (only used if no new run found)
    newest_in_progress: datetime | None = None

    if model_dir.exists():
        for rd in sorted(model_dir.iterdir(), reverse=True)[:6]:
            if not rd.is_dir() or rd.name in completed:
                continue
            if rd.name < cutoff_id:
                # Mark as abandoned if it's too old, so we don't keep trying to catch up
                meta_path = rd / "run_meta.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                        if not meta.get("abandoned"):
                            logger.info("[%s] Run %s too old (cutoff %s); marking abandoned",
                                        model.name.upper(), rd.name, cutoff_id)
                            meta["abandoned"] = True
                            meta["last_updated"] = datetime.now(timezone.utc).isoformat()
                            meta_path.write_text(json.dumps(meta, indent=2, default=str))
                    except Exception:
                        pass
                continue

            meta_path = rd / "run_meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("abandoned"):
                    if model.name != "gdps":
                        continue
                    logger.info("[GDPS] Resuming previously abandoned run %s within lookback", rd.name)
                    meta["abandoned"] = False
                    meta["last_updated"] = datetime.now(timezone.utc).isoformat()
                    meta_path.write_text(json.dumps(meta, indent=2, default=str))
                fxx_list = _canonical_forecast_hours(model, rd.name, meta)
                if meta.get("forecast_hours", []) != fxx_list:
                    meta["forecast_hours"] = fxx_list
                    meta["last_updated"] = datetime.now(timezone.utc).isoformat()
                    meta_path.write_text(json.dumps(meta, indent=2, default=str))

                completed_fxx_list = [int(f) for f in meta.get("completed_fxx", [])]
                skipped_fxx_list = [int(f) for f in meta.get("skipped_fxx", [])]
                meta_completed = set(completed_fxx_list)
                meta_skipped = set(skipped_fxx_list)
                meta_terminal = meta_completed | meta_skipped
                meta_complete = bool(fxx_list) and meta_terminal >= set(fxx_list)

                # Stale-run check: if a run has made significant progress
                # (>= 50%) but hasn't had any new fxx completed for hours,
                # mark it as best-effort complete.  This handles forecast
                # hours the server never publishes.  Must run BEFORE the
                # disk-completeness repair so it isn't overridden.
                _STALE_HOURS = 1
                if meta_terminal and not meta_complete and meta.get("last_updated"):
                    try:
                        last_updated = datetime.fromisoformat(
                            meta["last_updated"].replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                        stale_seconds = (now - last_updated).total_seconds()
                        pct = len(meta_terminal) / max(len(fxx_list), 1) * 100
                        if stale_seconds > _STALE_HOURS * 3600 and pct >= 50:
                            logger.info(
                                "[%s] Run %s stalled at %.0f%% for %.1fh; marking best-effort complete",
                                model.name.upper(), rd.name, pct, stale_seconds / 3600,
                            )
                            meta["is_complete"] = True
                            meta["best_effort"] = True
                            meta["last_updated"] = datetime.now(timezone.utc).isoformat()
                            meta_path.write_text(json.dumps(meta, indent=2, default=str))
                            continue
                    except Exception:
                        pass

                # If metadata says complete but the run is not actually complete on disk,
                # repair the metadata and let the scheduler resume it.
                # Skip best-effort runs — they were intentionally marked complete
                # by the stale-run detector despite missing some forecast hours.
                if meta.get("is_complete"):
                    if meta_complete or meta.get("best_effort"):
                        continue
                    disk_complete = bool(fxx_list) and all(
                        _is_fxx_complete(rd, fxx, model.rendered_params, model.snow_products)
                        for fxx in fxx_list
                    )
                    if disk_complete:
                        continue
                    logger.warning(
                        "[%s] Run %s marked complete in metadata but missing forecast hours on disk; resuming",
                        model.name.upper(), rd.name,
                    )
                    meta["is_complete"] = False
                    meta["last_updated"] = datetime.now(timezone.utc).isoformat()
                    meta_path.write_text(json.dumps(meta, indent=2, default=str))

                # Double-check: if every forecast hour is now complete on disk, mark complete.
                if meta_complete:
                    logger.info("[%s] Run %s actually complete on disk, updating metadata",
                                model.name.upper(), rd.name)
                    meta["is_complete"] = True
                    meta["last_updated"] = datetime.now(timezone.utc).isoformat()
                    meta_path.write_text(json.dumps(meta, indent=2, default=str))
                    continue

                disk_complete = bool(fxx_list) and all(
                    _is_fxx_complete(rd, fxx, model.rendered_params, model.snow_products)
                    for fxx in fxx_list
                )
                if disk_complete:
                    logger.info("[%s] Run %s actually complete on disk, updating metadata",
                                model.name.upper(), rd.name)
                    meta["is_complete"] = True
                    meta["last_updated"] = datetime.now(timezone.utc).isoformat()
                    meta_path.write_text(json.dumps(meta, indent=2, default=str))
                    continue

                # Track newest in-progress run for fallback
                rid = rd.name
                candidate_dt = datetime(
                    int(rid[:4]), int(rid[4:6]),
                    int(rid[6:8]), int(rid[8:10]),
                )
                if newest_in_progress is None or candidate_dt > newest_in_progress:
                    newest_in_progress = candidate_dt

            except Exception:
                pass

    # Scan recent hours for a new available run - ALWAYS prefer newest
    for hours_ago in range(0, lookback):
        ts = calendar.timegm(
            now.replace(minute=0, second=0, microsecond=0).timetuple()
        ) - hours_ago * 3600
        candidate = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        if candidate.hour not in model.run_hours:
            continue
        run_id = candidate.strftime("%Y%m%d%H")

        # Skip if this run was recently completed AND still exists on disk
        # This prevents re-processing after retention deletes old runs
        # But we should re-process if the directory no longer exists (was deleted by cleanup)
        if f"{recent_run_id}{run_id}" in recent_completed:
            # Recently completed — skip even if retention deleted the directory.
            # Without this, retention-delete → re-process → retention-delete loops.
            continue

        if run_id in completed:
            continue

        # Keep the newest run being processed. Skipping its directory here can
        # select an older still-available cycle and repeatedly switch runs
        # while the current cycle's downloads are in progress.
        if model_dir.exists():
            run_dir = model_dir / run_id
            if run_dir.exists():
                if newest_in_progress == candidate:
                    return candidate
                # A directory without valid in-progress metadata was handled
                # as failed above; do not immediately restart it.
                continue

        # Prefer locally cached data when present; otherwise probe remote availability.
        candidate_fxx = list(model.get_fxx(candidate.hour))
        first_candidate_fxx = int(candidate_fxx[0]) if candidate_fxx else 0
        if _find_cached_grib(model, candidate, first_candidate_fxx) is not None or _check_available(model, candidate):
            return candidate

    # No new run found - only use in-progress run as absolute last resort
    return newest_in_progress


def _find_cached_grib(model: ModelConfig, run_dt: datetime, fxx: int) -> Path | None:
    """Resolve an already-downloaded GRIB subset from local cache.

    This avoids rebuilding Herbie state and re-checking remote sources for
    prerequisite files (previous precip hour, snow lookback hours) that are
    usually already present on disk.
    """
    cache_key = (model.name, run_dt.strftime("%Y%m%d%H"), fxx)
    now = time.time()
    cached = _grib_lookup_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        # Re-resolve if cleanup deleted the cached path mid-TTL.
        if cached[1] is None or cached[1].exists():
            return cached[1]

    cache_dir = GRIB_CACHE_DIR / model.herbie_model / run_dt.strftime("%Y%m%d")
    if not cache_dir.exists():
        _grib_lookup_cache[cache_key] = (now + _GRIB_CACHE_MISS_TTL, None)
        return None

    hour = run_dt.strftime("%H")
    suffixes = []
    if model.name == "hrrr":
        suffixes.append(f"hrrr.t{hour}z.wrfsfcf{fxx:02d}.grib2")
    elif model.name == "rap":
        suffixes.append(f"rap.t{hour}z.awp130pgrbf{fxx:02d}.grib2")
    elif model.name == "gfs":
        suffixes.append(f"gfs.t{hour}z.pgrb2.0p25.f{fxx:03d}")
    elif model.name == "nam12km":
        suffixes.append(f"nam.t{hour}z.awphys{fxx:02d}.tm00.grib2")
    elif model.name == "nam3km":
        suffixes.append(f"nam.t{hour}z.conusnest.hiresf{fxx:02d}.tm00.grib2")
    elif model.name == "rtma":
        suffixes.append(f"rtma2p5.t{hour}z.2dvaranl_ndfd.grb2_wexp")
    elif model.name == "ecmwf":
        suffixes.append(f"{run_dt.strftime('%Y%m%d%H')}0000-{fxx}h-oper-fc.grib2")
    elif model.name == "href":
        suffixes.append(f"href.t{hour}z.conus.mean.f{fxx:02d}.grib2")
    elif model.name == "rrfs":
        suffixes.append(f"rrfs_{run_dt.strftime('%Y%m%d')}_{hour}_f{fxx:03d}.grib2")
    elif model.name == "gefs":
        suffixes.append(f"gec00.t{hour}z.pgrb2s.0p25.f{fxx:03d}")
    else:
        return None

    dir_index = _grib_dir_index(cache_dir)
    for suffix in suffixes:
        match = dir_index.get(suffix)
        if match is not None:
            _grib_lookup_cache[cache_key] = (now + _GRIB_CACHE_HIT_TTL, match)
            return match

    _grib_lookup_cache[cache_key] = (now + _GRIB_CACHE_MISS_TTL, None)
    return None


def _canonical_forecast_hours(model: ModelConfig, run_id: str, meta: dict | None = None) -> list[int]:
    """Return the authoritative forecast-hour list for a model run."""
    try:
        return list(model.get_fxx(int(run_id[8:10])))
    except Exception:
        if meta is not None:
            return [int(fxx) for fxx in meta.get("forecast_hours", [])]
        return []


def _latest_rendered_run_dt(model_name: str) -> datetime | None:
    """Return the most recent locally rendered run timestamp for a model."""
    model_dir = RENDERED_DIR / model_name
    if not model_dir.exists():
        return None
    latest_name = None
    for rd in model_dir.iterdir():
        if rd.is_dir() and rd.name.isdigit() and len(rd.name) == 10:
            if latest_name is None or rd.name > latest_name:
                latest_name = rd.name
    if latest_name is None:
        return None
    return datetime(
        int(latest_name[:4]), int(latest_name[4:6]),
        int(latest_name[6:8]), int(latest_name[8:10]),
    )


def _decide_burst_subworkers(
    model_name: str,
    *,
    lowpri_in_flight: int,
    lowpri_shed_active: bool,
) -> tuple[int, str]:
    if not BURST_RENDER_ENABLED:
        return 1, "disabled"
    if model_name not in BURST_RENDER_MODELS:
        return 1, "model_off"
    if lowpri_shed_active:
        return 1, "lowpri_shed"
    if _system_cpu_percent() >= BURST_RENDER_MAX_CPU_PCT:
        return 1, "cpu_busy"

    mem_pct = _system_mem_percent()
    if mem_pct >= BURST_RENDER_MAX_MEM_PCT:
        return 1, f"mem:{mem_pct:.0f}"

    return BURST_RENDER_MAX_SUBWORKERS, f"cpu:available lowpri:{lowpri_in_flight} mem:{mem_pct:.0f}"


def _ready_staleness_seconds(model: ModelConfig, run_dt: datetime) -> float:
    """How stale this model is locally; larger means it deserves a slot sooner."""
    latest_local = _latest_rendered_run_dt(model.name)
    baseline = latest_local or run_dt
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max((now - baseline).total_seconds(), 0.0)


# ---------------------------------------------------------------------------
# Priority waterfall helpers
# ---------------------------------------------------------------------------

def _compute_render_params(model: ModelConfig) -> tuple[list[str], list[str]]:
    """Return (render_params, extra_soundings) for a model."""
    render_params = list(model.rendered_params)
    for derived_param in model.derived:
        if derived_param in model.rendered_params and derived_param not in render_params:
            render_params.append(derived_param)
    for composite_param in model.composites:
        if composite_param in model.rendered_params and composite_param not in render_params:
            render_params.append(composite_param)
    if model.snow_products:
        for snow_param in SNOW_PRODUCTS:
            if snow_param in model.rendered_params and snow_param not in render_params:
                render_params.append(snow_param)
    extra_soundings: list[str] = []
    if model.sounding_cache:
        extra_soundings = [
            p for p in _get_sounding_cache_params(model.name)
            if p not in render_params
        ]
    return render_params, extra_soundings


def _build_render_item(
    model: ModelConfig, run_dt: datetime, fxx: int,
    downloaded: dict[int, Path],
) -> dict | None:
    """Build a render batch item for one forecast hour."""
    if fxx not in downloaded:
        return None

    prev_grib_str = None
    if model.hourly_precip and fxx > 0:
        prev_path = downloaded.get(fxx - 1)
        if prev_path is None:
            prev_path = _find_cached_grib(model, run_dt, fxx - 1)
        if prev_path is not None:
            prev_grib_str = str(prev_path)

    lookback_strs = None
    if model.snow_products:
        lb: dict[str, str] = {}
        for product, cfg in SNOW_PRODUCTS.items():
            lb_fxx = fxx - cfg["hours"]
            if lb_fxx > 0:
                lb_path = downloaded.get(lb_fxx)
                if lb_path is None:
                    lb_path = _find_cached_grib(model, run_dt, lb_fxx)
                if lb_path is not None:
                    lb[str(lb_fxx)] = str(lb_path)
        lookback_strs = lb

    return {
        "fxx": fxx,
        "grib_path_str": str(downloaded[fxx]),
        "prev_grib_str": prev_grib_str,
        "lookback_strs": lookback_strs,
    }


@dataclass
class _RunState:
    """Tracks render progress for one model run."""
    model: ModelConfig
    run_dt: datetime
    run_id: str
    fxx_list: list[int]
    completed: set[int]
    skipped: set[int]
    empty_render_attempts: dict[int, int]
    empty_render_retry_after: dict[int, float]
    render_params: list[str]
    extra_soundings: list[str]

    @staticmethod
    def create(model: ModelConfig, run_dt: datetime) -> "_RunState":
        """Create a RunState, loading any existing progress from disk."""
        run_id = run_dt.strftime("%Y%m%d%H")
        fxx_list = list(model.get_fxx(run_dt.hour))
        out_dir = _run_dir(model.name, run_dt)
        render_params, extra_soundings = _compute_render_params(model)

        # Load existing metadata for resume
        completed: set[int] = set()
        skipped: set[int] = set()
        meta_path = out_dir / "run_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                completed = {int(f) for f in meta.get("completed_fxx", [])}
                skipped = {int(f) for f in meta.get("skipped_fxx", [])}
            except Exception:
                pass

        # Also check what is already rendered on disk
        disk_completed = {
            f for f in fxx_list
            if _is_fxx_complete(out_dir, f, model.rendered_params, model.snow_products)
        }
        completed |= disk_completed

        return _RunState(
            model=model,
            run_dt=run_dt,
            run_id=run_id,
            fxx_list=fxx_list,
            completed=completed & set(fxx_list),
            skipped=skipped & set(fxx_list),
            empty_render_attempts={},
            empty_render_retry_after={},
            render_params=render_params,
            extra_soundings=extra_soundings,
        )

    def todo(self, in_flight_fxx: set[int]) -> list[int]:
        """Return fxx that still need rendering (in fxx_list order)."""
        terminal = self.completed | self.skipped | in_flight_fxx
        now = time.time()
        return [
            f for f in self.fxx_list
            if f not in terminal
            and now >= self.empty_render_retry_after.get(f, 0)
        ]

    def is_done(self) -> bool:
        """True when all fxx are completed or skipped (or best-effort)."""
        if (self.completed | self.skipped) >= set(self.fxx_list):
            return True
        # Check if stale-run detector marked this as best-effort
        meta_path = _run_dir(self.model.name, self.run_dt) / "run_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("best_effort") or (
                    meta.get("is_complete") and meta.get("best_effort")
                ):
                    return True
            except Exception:
                pass
        return False

    def write_meta(self):
        """Write current metadata to disk."""
        out_dir = _run_dir(self.model.name, self.run_dt)
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o755)

        # Preserve last_updated when no new progress
        last_updated = datetime.now(timezone.utc).isoformat()
        meta_path = out_dir / "run_meta.json"
        if meta_path.exists():
            try:
                prev = json.loads(meta_path.read_text())
                prev_completed = {int(f) for f in prev.get("completed_fxx", [])}
                if self.completed == prev_completed and prev.get("last_updated"):
                    last_updated = prev["last_updated"]
            except Exception:
                pass

        meta = {
            "model": self.model.name,
            "run_id": self.run_id,
            "init_time": self.run_dt.strftime("%Y-%m-%dT%H:00:00Z"),
            "parameters": self.model.rendered_params,
            "forecast_hours": self.fxx_list,
            "completed_fxx": sorted(self.completed),
            "skipped_fxx": sorted(self.skipped),
            "is_complete": False,
            "processing_started": datetime.now(timezone.utc).isoformat(),
            "last_updated": last_updated,
            "palette_version": CURRENT_PALETTE_VERSION,
        }
        _write_meta(self.model.name, self.run_dt, meta)

    def handle_result(self, fxx: int, rendered: list[str]):
        """Process a render result for one fxx."""
        out_dir = _run_dir(self.model.name, self.run_dt)
        _invalidate_run_completion_cache(self.model.name, self.run_id, fxx)
        if rendered or _is_fxx_complete(
            out_dir, fxx, self.model.rendered_params, self.model.snow_products,
        ):
            self.completed.add(fxx)
            self.empty_render_attempts.pop(fxx, None)
            self.empty_render_retry_after.pop(fxx, None)
            # Update running-max swath images for any UH source params rendered
            swath_pairs = _SWATH_PARAMS.get(self.model.name, {})
            for source_param, swath_param in swath_pairs.items():
                if source_param in rendered:
                    update_running_max(out_dir, source_param, swath_param, fxx)
        else:
            attempts = self.empty_render_attempts.get(fxx, 0) + 1
            self.empty_render_attempts[fxx] = attempts
            if attempts >= EMPTY_RENDER_RETRY_LIMIT:
                self.skipped.add(fxx)
                self.empty_render_retry_after.pop(fxx, None)
                logger.warning(
                    "[%s] %s fxx=%02d no outputs after %d attempts; skipped",
                    self.model.name.upper(), self.run_id, fxx, attempts,
                )
            else:
                self.empty_render_retry_after[fxx] = (
                    time.time() + EMPTY_RENDER_RETRY_BACKOFF_SECONDS
                )
                logger.warning(
                    "[%s] %s fxx=%02d no outputs; retry %d/%d",
                    self.model.name.upper(), self.run_id, fxx,
                    attempts, EMPTY_RENDER_RETRY_LIMIT,
                )
        self.write_meta()
        pct = 100 * len(self.completed | self.skipped) / max(len(self.fxx_list), 1)
        logger.info(
            "[%s] %s fxx=%02d rendered %d params — %.0f%%",
            self.model.name.upper(), self.run_id, fxx,
            len(rendered), pct,
        )


@dataclass
class _PoolBinding:
    model_name: str | None = None
    lease_remaining: int = 0
    last_active_ts: float = 0.0


@dataclass
class _PoolConfig:
    name: str
    worker_count: int
    burst_subworkers: int
    sticky: bool

def run_radar_scheduler_loop(stop_event: threading.Event) -> None:
    """Run radar scheduler in a background thread.

    Uses decoupled download/render architecture: background downloads for all
    sites, priority waterfall dispatch (Nebraska first, then FIFO).
    """
    try:
        from .radar.scheduler_pipeline import run as run_radar
        logger.info("Starting radar scheduler (decoupled download/render)")
        run_radar(stop_event=stop_event)
    except Exception as exc:
        logger.error("Failed to start radar scheduler: %s", exc)




# ---------------------------------------------------------------------------
# Main scheduler loop — priority waterfall dispatch
# ---------------------------------------------------------------------------

_LOCK_FILE = DATA_DIR / "scheduler_unified.lock"


def _acquire_lock():
    """Ensure only one unified scheduler runs at a time."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another unified scheduler is already running.", file=sys.stderr)
        sys.exit(1)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd


def main():
    # Suppress multiprocessing resource_tracker warnings (no timestamps, clutter logs)
    warnings.filterwarnings("ignore", module="multiprocessing.resource_tracker")
    lock = _acquire_lock()  # noqa: F841 — held for process lifetime
    affinity_pool_mode = AFFINITY_POOL_MODE
    if affinity_pool_mode:
        total_render_workers = (
            AFFINITY_POOL_A_WORKERS
            + AFFINITY_POOL_B_WORKERS
            + AFFINITY_POOL_C_WORKERS
            + AFFINITY_CATCHALL_WORKERS
        )
        logger.info(
            "Unified pipeline scheduler started — affinity pools enabled "
            "(A=%d, B=%d, C=%d, catchall=%d; total=%d)",
            AFFINITY_POOL_A_WORKERS,
            AFFINITY_POOL_B_WORKERS,
            AFFINITY_POOL_C_WORKERS,
            AFFINITY_CATCHALL_WORKERS,
            total_render_workers,
        )
        logger.info(
            "Affinity binding: min_lease_fxx=%d idle_release=%ds hold_open=%ds pool_burst=%dx catchall=1x",
            AFFINITY_POOL_LEASE_FXX,
            AFFINITY_POOL_IDLE_RELEASE_SECONDS,
            AFFINITY_POOL_HOLD_OPEN_SECONDS,
            AFFINITY_POOL_BURST_SUBWORKERS,
        )
        logger.info(
            "Trickle mode: ready_fxx<%d or multi_model<%d -> idle_release=%ds hold_open=%ds reserve_x=%d",
            TRICKLE_READY_FXX_THRESHOLD,
            TRICKLE_MULTI_MODEL_THRESHOLD,
            TRICKLE_IDLE_RELEASE_SECONDS,
            TRICKLE_HOLD_OPEN_SECONDS,
            TRICKLE_RESERVED_CATCHALL_SLOTS,
        )
    else:
        total_render_workers = SHARED_RENDER_WORKERS
        logger.info("Unified pipeline scheduler started — %d render workers, "
                    "background downloads for all models", SHARED_RENDER_WORKERS)
    logger.info(
        "Burst render mode: enabled=%s max_subworkers=%d models=%s cpu<%.0f mem<%.0f min_params=%d",
        BURST_RENDER_ENABLED,
        BURST_RENDER_MAX_SUBWORKERS,
        ",".join(sorted(BURST_RENDER_MODELS)),
        BURST_RENDER_MAX_CPU_PCT,
        BURST_RENDER_MAX_MEM_PCT,
        BURST_RENDER_MIN_PARAMS,
    )
    _write_state({"status": "starting"})

    # Clean up stale worker status files from previous run
    if WORKER_STATUS_DIR.exists():
        for f in WORKER_STATUS_DIR.glob("*.json"):
            f.unlink(missing_ok=True)

    # Kill orphaned render workers from a previous instance.  When the
    # scheduler is OOM-killed or hard-stopped, ProcessPoolExecutor children
    # can survive as reparented-to-init zombies eating memory.
    _kill_orphan_workers()

    # Radar scheduler disabled - running as separate service (weather-radar)
    radar_stop_event = threading.Event()
    logger.info("Radar scheduler disabled (using standalone weather-radar service)")

    def _new_process_pool(worker_count: int) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_init_render_worker,
            max_tasks_per_child=MODEL_WORKER_MAX_TASKS,
        )

    if affinity_pool_mode:
        pool_configs: dict[str, _PoolConfig] = {
            "affinity_a": _PoolConfig("affinity_a", AFFINITY_POOL_A_WORKERS, AFFINITY_POOL_BURST_SUBWORKERS, True),
            "affinity_b": _PoolConfig("affinity_b", AFFINITY_POOL_B_WORKERS, AFFINITY_POOL_BURST_SUBWORKERS, True),
            "affinity_c": _PoolConfig("affinity_c", AFFINITY_POOL_C_WORKERS, AFFINITY_POOL_BURST_SUBWORKERS, True),
            "catchall": _PoolConfig("catchall", AFFINITY_CATCHALL_WORKERS, 1, False),
        }
    else:
        pool_configs = {
            "shared": _PoolConfig("shared", SHARED_RENDER_WORKERS, BURST_RENDER_MAX_SUBWORKERS, False),
        }

    render_pools = {
        name: _new_process_pool(cfg.worker_count)
        for name, cfg in pool_configs.items()
        if cfg.worker_count > 0
    }
    pool_broken = {name: False for name in render_pools}
    pool_bindings = {
        name: _PoolBinding()
        for name, cfg in pool_configs.items()
        if cfg.sticky and cfg.worker_count > 0
    }

    # Low-priority lane — file-based queue consumed by weather-pipeline-lowpri service
    from .lowpri_worker import PENDING_DIR, RESULTS_DIR, QUEUE_DIR
    for _qdir in (PENDING_DIR, RESULTS_DIR):
        _qdir.mkdir(parents=True, exist_ok=True)

    def _load_existing_lowpri_jobs() -> dict[tuple[str, int], tuple[str, float]]:
        """Rebuild LP in-flight bookkeeping from on-disk queue files."""
        loaded: dict[tuple[str, int], tuple[str, float]] = {}
        for queue_dir in (PENDING_DIR, QUEUE_DIR / "claimed"):
            try:
                job_files = sorted(queue_dir.glob("*.json"))
            except Exception:
                continue
            for job_file in job_files:
                try:
                    job = json.loads(job_file.read_text())
                    model_name = str(job.get("model_name") or "").strip()
                    fxx = int(job.get("fxx"))
                    job_id = str(job.get("job_id") or job_file.stem)
                    loaded[(model_name, fxx)] = (job_id, job_file.stat().st_mtime)
                except Exception:
                    continue
        return loaded

    # Lowpri worker throttle — write target worker count to a file so the
    # lowpri supervisor can scale up/down gracefully under memory pressure.
    WORKER_TARGET_FILE = QUEUE_DIR / "worker_target"
    lowpri_max_workers = 0 if affinity_pool_mode else LOW_PRIORITY_WORKERS
    lowpri_target_workers = lowpri_max_workers
    lowpri_shed_active = False

    def _write_lowpri_target(target: int) -> None:
        nonlocal lowpri_target_workers, lowpri_shed_active
        target = max(0, min(target, lowpri_max_workers))
        if target == lowpri_target_workers:
            return
        lowpri_target_workers = target
        lowpri_shed_active = target < lowpri_max_workers
        try:
            tmp = WORKER_TARGET_FILE.with_suffix(".tmp")
            tmp.write_text(str(target))
            tmp.rename(WORKER_TARGET_FILE)
            logger.info("[LP throttle] Worker target → %d/%d", target, lowpri_max_workers)
        except Exception as exc:
            logger.warning("[LP throttle] Failed to write worker target: %s", exc)

    def _ensure_pool(pool_name: str):
        if not pool_broken.get(pool_name):
            return render_pools[pool_name]
        cfg = pool_configs[pool_name]
        logger.info("Recreating render pool %s after crash", pool_name)
        try:
            render_pools[pool_name].shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        render_pools[pool_name] = _new_process_pool(cfg.worker_count)
        pool_broken[pool_name] = False
        return render_pools[pool_name]

    # Start background download manager — downloads for ALL models in parallel
    dl_mgr = DownloadManager()
    dl_mgr.start()

    # Run tracking: {model_name: _RunState}
    run_states: dict[str, _RunState] = {}
    # Guards against finalize/retrack churn when the download manager still
    # advertises a run that is already fully complete on disk.
    completed_run_ids: dict[str, str] = {}
    # In-flight renders: {(model_name, fxx): (pool_name, Future)}
    in_flight: dict[tuple[str, int], tuple[str, object]] = {}
    admission = RenderAdmission(metrics_path=DATA_DIR / "render_memory_metrics_v2.json")
    ready_since: dict[str, float] = {}
    shared_fairness_cursor = MODEL_PRIORITY_RANK["rap"]
    low_priority_in_flight: dict[tuple[str, int], object]
    if lowpri_max_workers > 0:
        low_priority_in_flight = _load_existing_lowpri_jobs()
        if low_priority_in_flight:
            logger.info("[LP] Recovered %d queued/in-flight jobs from disk after startup",
                        len(low_priority_in_flight))
    else:
        low_priority_in_flight = {}
        logger.info("[LP] Disabled (LOWPRI_WORKERS=0); skipping LP queue recovery/dispatch")

    last_memory_check = 0.0
    last_status_log = 0.0
    MEMORY_CHECK_INTERVAL = 5
    STATUS_LOG_INTERVAL = 30
    DISPATCH_INTERVAL = 2

    while True:
        try:
            now = time.time()

            # --- Periodic memory check ---
            if now - last_memory_check > MEMORY_CHECK_INTERVAL:
                last_memory_check = now
                is_pressure, mem_pct = check_memory_pressure()
                is_critical, _ = check_memory_critical()
                if is_critical:
                    logger.error("MEMORY CRITICAL: %.1f%% — clearing caches", mem_pct)
                    clear_all_caches()
                    _release_memory("critical cleanup")
                elif is_pressure:
                    logger.warning("Memory pressure: %.1f%% — clearing caches", mem_pct)
                    clear_all_caches()
                    _release_memory("pressure cleanup")

                # --- Graduated lowpri worker shedding ---
                if mem_pct >= MEMORY_LOWPRI_KILL_ALL_PCT:
                    # Emergency: kill all lowpri workers immediately
                    _write_lowpri_target(0)
                elif mem_pct >= MEMORY_LOWPRI_SHED_THRESHOLD_PCT:
                    # Shed one worker per check cycle
                    _write_lowpri_target(lowpri_target_workers - 1)
                elif lowpri_target_workers < lowpri_max_workers and mem_pct < MEMORY_LOWPRI_SHED_THRESHOLD_PCT - 10:
                    # Ramp back up one worker per cycle when comfortably below threshold
                    _write_lowpri_target(lowpri_target_workers + 1)

            # --- Collect completed renders (all pools) ---
            done_keys = [k for k, (_, f) in in_flight.items() if f.done()]
            for key in done_keys:
                model_name, fxx = key
                pool_name, future = in_flight.pop(key)
                state = run_states.get(model_name)
                if state is None:
                    admission.release(key)
                    continue
                try:
                    batch_results = future.result(timeout=10)
                    for batch_result in batch_results:
                        done_fxx, rendered = batch_result[:2]
                        metrics = batch_result[2] if len(batch_result) > 2 else None
                        admission.release((model_name, done_fxx), metrics)
                        if getattr(future, "_wx_run_id", state.run_id) != state.run_id:
                            continue
                        state.handle_result(done_fxx, rendered)
                        binding = pool_bindings.get(pool_name)
                        if binding is not None and binding.model_name == model_name:
                            binding.last_active_ts = now
                            if binding.lease_remaining > 0:
                                binding.lease_remaining -= 1
                except BrokenProcessPool:
                    logger.error("Render pool %s crashed", pool_name)
                    pool_broken[pool_name] = True
                    stale_keys = [k for k, (pn, _) in in_flight.items() if pn == pool_name]
                    for stale_key in stale_keys:
                        in_flight.pop(stale_key, None)
                        admission.release(stale_key)
                except CancelledError:
                    logger.info("[%s] Render cancelled fxx=%02d",
                                model_name.upper(), fxx)
                except Exception as exc:
                    logger.error("[%s] Render failed fxx=%02d: %s",
                                 model_name.upper(), fxx, exc, exc_info=True)
                finally:
                    # Keep the process count until metrics are recorded above.
                    # Also release cancelled, failed, or empty batch results.
                    admission.release(key)

            # --- Expire stale low-priority in-flight entries ---
            LP_STALE_TIMEOUT = 900  # 15 minutes
            stale_lp = [
                k for k, (_, t) in low_priority_in_flight.items()
                if now - t > LP_STALE_TIMEOUT
            ]
            for k in stale_lp:
                mn, fxx = k
                logger.warning("[LP] Expiring stale in-flight entry %s fxx=%02d (no result after %ds)",
                               mn.upper(), fxx, LP_STALE_TIMEOUT)
                del low_priority_in_flight[k]

            # --- Collect completed renders (low priority file queue) ---
            for result_file in RESULTS_DIR.glob("*.json"):
                try:
                    result = json.loads(result_file.read_text())
                    result_file.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("Failed to read LP result %s: %s", result_file.name, exc)
                    result_file.unlink(missing_ok=True)
                    continue

                job_id = result.get("job_id", "")
                model_name = result.get("model_name", "")
                done_fxx = result.get("fxx", -1)
                rendered = result.get("rendered", [])

                # Remove from in-flight tracking
                lp_key = (model_name, done_fxx)
                if lp_key in low_priority_in_flight:
                    del low_priority_in_flight[lp_key]

                state = run_states.get(model_name)
                if state is None:
                    continue

                # Discard results from a previous run
                result_run_id = result.get("run_id", "")
                if result_run_id and result_run_id != state.run_id:
                    logger.info("[LP] Discarding stale result %s fxx=%02d (run %s, current %s)",
                                model_name.upper(), done_fxx, result_run_id, state.run_id)
                    continue

                if result.get("error"):
                    logger.error("[%s] LP render failed fxx=%02d: %s",
                                 model_name.upper(), done_fxx, result["error"])
                state.handle_result(done_fxx, rendered)
                logger.info("[LP] Result collected: %s fxx=%02d → %d outputs",
                            model_name.upper(), done_fxx, len(rendered))

            # --- Finalize completed runs ---
            for model_name in list(run_states):
                state = run_states[model_name]
                model_in_flight = any(mn == model_name for mn, _ in in_flight) or \
                                  any(mn == model_name for mn, _ in low_priority_in_flight)
                if state.is_done() and not model_in_flight:
                    _finalize(
                        state.model, state.run_dt, state.fxx_list,
                        sorted(state.completed), skipped=sorted(state.skipped),
                    )
                    completed_run_ids[model_name] = state.run_id
                    del run_states[model_name]
                    logger.info("[%s] Run %s finalized",
                                model_name.upper(), state.run_id)

            # --- Drop stale/redundant low-priority queue bookkeeping ---
            stale_lp_keys: list[tuple[str, int]] = []
            for (model_name, fxx), (job_id, _queued_at) in low_priority_in_flight.items():
                state = run_states.get(model_name)
                remove_job = False
                if state is None:
                    remove_job = True
                elif fxx in state.completed or fxx in state.skipped or fxx not in state.fxx_list:
                    remove_job = True

                if not remove_job:
                    continue

                for queue_dir in (PENDING_DIR, QUEUE_DIR / "claimed"):
                    try:
                        (queue_dir / f"{job_id}.json").unlink(missing_ok=True)
                    except Exception:
                        pass
                stale_lp_keys.append((model_name, fxx))

            for key in stale_lp_keys:
                model_name, fxx = key
                low_priority_in_flight.pop(key, None)
                logger.info("[LP] Dropped stale queued job %s fxx=%02d",
                            model_name.upper(), fxx)

            # --- Update run states from download manager ---
            for model_name in MODEL_PRIORITY_ORDER:
                dl_info = dl_mgr.get_downloaded(model_name)
                if dl_info is None:
                    # No pending work for this model — finalize if needed
                    if model_name in run_states:
                        model_in_flight = any(
                            mn == model_name for mn, _ in in_flight
                        ) or any(
                            mn == model_name for mn, _ in low_priority_in_flight
                        )
                        if not model_in_flight:
                            state = run_states[model_name]
                            _finalize(
                                state.model, state.run_dt, state.fxx_list,
                                sorted(state.completed),
                                skipped=sorted(state.skipped),
                            )
                            completed_run_ids[model_name] = state.run_id
                            del run_states[model_name]
                    continue

                run_dt, run_id, _ = dl_info

                if completed_run_ids.get(model_name) == run_id:
                    continue

                # Initialize or switch run state for this model
                if model_name not in run_states or run_states[model_name].run_id != run_id:
                    # Finalize old run if switching
                    if model_name in run_states:
                        old_state = run_states[model_name]
                        # Drop in-flight renders for old run (results will be
                        # silently discarded when the futures complete)
                        old_hp = [k for k in in_flight if k[0] == model_name]
                        for k in old_hp:
                            # Running old-run work still occupies a real slot.
                            if in_flight[k][1].cancel():
                                in_flight.pop(k)
                                admission.release(k)
                        old_lp = [k for k in low_priority_in_flight if k[0] == model_name]
                        for k in old_lp:
                            low_priority_in_flight.pop(k)
                        # Remove pending LP queue files for the old run
                        old_run_prefix = f"{model_name}_{old_state.run_dt.strftime('%Y%m%d%H')}_"
                        purged = 0
                        for pending_file in PENDING_DIR.glob(f"{old_run_prefix}*.json"):
                            pending_file.unlink(missing_ok=True)
                            purged += 1
                        if old_hp or old_lp or purged:
                            logger.info("[%s] Dropping %d HP + %d LP in-flight + %d queued for old run %s → switching to %s",
                                        model_name.upper(), len(old_hp), len(old_lp), purged,
                                        old_state.run_id, run_id)
                        for binding in pool_bindings.values():
                            if binding.model_name == model_name:
                                binding.model_name = None
                                binding.lease_remaining = 0
                        _finalize(
                            old_state.model, old_state.run_dt,
                            old_state.fxx_list,
                            sorted(old_state.completed),
                            skipped=sorted(old_state.skipped),
                        )
                        completed_run_ids[model_name] = old_state.run_id
                    model = MODEL_MAP[model_name]
                    run_states[model_name] = _RunState.create(model, run_dt)
                    run_states[model_name].write_meta()
                    logger.info("[%s] Tracking run %s (%d fxx, %d already done)",
                                model_name.upper(), run_id,
                                len(run_states[model_name].fxx_list),
                                len(run_states[model_name].completed))

            # --- Build per-model ready work ---
            downloaded_by_model: dict[str, dict[int, Path]] = {}
            renderable_by_model: dict[str, list[int]] = {}
            for model_name in MODEL_PRIORITY_ORDER:
                state = run_states.get(model_name)
                if state is None:
                    continue
                dl_info = dl_mgr.get_downloaded(model_name)
                if dl_info is None:
                    continue
                _, _, downloaded = dl_info
                model_in_flight_fxx = {fxx for mn, fxx in in_flight if mn == model_name}
                model_in_flight_fxx.update(
                    {fxx for mn, fxx in low_priority_in_flight if mn == model_name}
                )
                renderable = [f for f in state.todo(model_in_flight_fxx) if f in downloaded]
                if renderable:
                    downloaded_by_model[model_name] = downloaded
                    renderable_by_model[model_name] = renderable

            pool_slots = {
                name: max(
                    0,
                    cfg.worker_count - sum(1 for _k, (pn, _f) in in_flight.items() if pn == name),
                )
                for name, cfg in pool_configs.items()
                if cfg.worker_count > 0
            }

            trickle_mode = False
            admission.refresh(WORKER_STATUS_DIR)
            for model_name in list(ready_since):
                if model_name not in renderable_by_model:
                    ready_since.pop(model_name, None)
            for model_name in renderable_by_model:
                ready_since.setdefault(model_name, now)
            if affinity_pool_mode and BALANCED_POOL_MODE:
                remaining_ready = sum(len(hours) for hours in renderable_by_model.values())
                remaining_slots = sum(pool_slots.values())
                def submit_balanced(pool_name: str, model_name: str, fxx: int) -> bool:
                    nonlocal remaining_ready, remaining_slots
                    state = run_states[model_name]
                    item = _build_render_item(state.model, state.run_dt, fxx,
                                              downloaded_by_model[model_name])
                    key = (model_name, fxx)
                    if item is None:
                        return False
                    burst_subworkers, burst_reason = _decide_burst_subworkers(
                        model_name,
                        lowpri_in_flight=len(low_priority_in_flight),
                        lowpri_shed_active=lowpri_shed_active,
                    )
                    burst_subworkers = min(
                        burst_subworkers, pool_configs[pool_name].burst_subworkers,
                    )
                    planned = render_processes_for_backlog(
                        remaining_ready, remaining_slots, admission.reserved_processes(),
                        admission.cpu_budget(), burst_subworkers,
                    )
                    if planned < burst_subworkers:
                        burst_reason = "independent_forecast_hours"
                    burst_subworkers = planned
                    while not admission.reserve(key, render_processes=burst_subworkers):
                        if burst_subworkers <= 1:
                            return False
                        burst_subworkers -= 1
                        burst_reason = "burst_resource_budget"
                    out_dir = _run_dir(model_name, state.run_dt)
                    out_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
                    try:
                        future = _ensure_pool(pool_name).submit(
                            _render_model_batch, [item], str(out_dir), state.run_dt.isoformat(),
                            state.render_params, state.extra_soundings, model_name, pool_name,
                            burst_subworkers, burst_reason,
                        )
                    except BrokenProcessPool:
                        pool_broken[pool_name] = True
                        admission.release(key)
                        return False
                    future._wx_run_id = state.run_id
                    in_flight[key] = (pool_name, future)
                    remaining_ready -= 1
                    remaining_slots -= 1
                    logger.info(
                        "[pool %s] [%s] fxx=%02d dispatch mode: burst_subworkers=%d reason=%s balanced",
                        pool_name, model_name.upper(), fxx, burst_subworkers, burst_reason,
                    )
                    return True
                _balanced_dispatch_plan(renderable_by_model, pool_slots, ready_since,
                                        now, submit_balanced)
            elif affinity_pool_mode:
                sticky_pool_names = [
                    name
                    for name in ("affinity_a", "affinity_b", "affinity_c")
                    if name in pool_bindings
                ]
                active_candidates = [mn for mn in MODEL_PRIORITY_ORDER if renderable_by_model.get(mn)]
                total_ready_fxx = sum(len(renderable_by_model.get(mn, [])) for mn in active_candidates)
                multi_ready_models = sum(
                    1 for mn in active_candidates if len(renderable_by_model.get(mn, [])) >= 2
                )
                trickle_mode = (
                    total_ready_fxx < TRICKLE_READY_FXX_THRESHOLD
                    or multi_ready_models < TRICKLE_MULTI_MODEL_THRESHOLD
                )
                effective_idle_release = (
                    TRICKLE_IDLE_RELEASE_SECONDS
                    if trickle_mode
                    else AFFINITY_POOL_IDLE_RELEASE_SECONDS
                )
                effective_hold_open = (
                    TRICKLE_HOLD_OPEN_SECONDS
                    if trickle_mode
                    else AFFINITY_POOL_HOLD_OPEN_SECONDS
                )

                def _submit_render_job(
                    pool_name: str,
                    model_name: str,
                    fxx: int,
                    burst_subworkers: int,
                    burst_reason: str,
                ) -> bool:
                    if (model_name, fxx) in in_flight:
                        return False
                    state = run_states.get(model_name)
                    downloaded = downloaded_by_model.get(model_name)
                    if state is None or downloaded is None:
                        return False
                    item = _build_render_item(state.model, state.run_dt, fxx, downloaded)
                    if item is None:
                        return False
                    if not admission.reserve((model_name, fxx)):
                        return False
                    out_dir = _run_dir(model_name, state.run_dt)
                    out_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
                    try:
                        future = _ensure_pool(pool_name).submit(
                            _render_model_batch,
                            [item],
                            str(out_dir),
                            state.run_dt.isoformat(),
                            state.render_params,
                            state.extra_soundings,
                            model_name,
                            pool_name,
                            burst_subworkers,
                            burst_reason,
                        )
                        future._wx_run_id = state.run_id
                        in_flight[(model_name, fxx)] = (pool_name, future)
                        binding = pool_bindings.get(pool_name)
                        if binding is not None:
                            binding.last_active_ts = now
                        return True
                    except BrokenProcessPool:
                        pool_broken[pool_name] = True
                        admission.release((model_name, fxx))
                        return False

                def _available_renderable(model_name: str) -> list[int]:
                    return [
                        fxx for fxx in renderable_by_model.get(model_name, [])
                        if (model_name, fxx) not in in_flight
                    ]

                def _bound_worker_capacity(model_name: str) -> int:
                    total = 0
                    for other_name in sticky_pool_names:
                        binding = pool_bindings[other_name]
                        if binding.model_name == model_name:
                            total += pool_configs[other_name].worker_count
                    return total

                for pool_name in sticky_pool_names:
                    binding = pool_bindings[pool_name]
                    model_name = binding.model_name
                    pool_model_in_flight = any(mn == model_name and pn == pool_name for (mn, _), (pn, _) in in_flight.items())
                    if model_name and not renderable_by_model.get(model_name) and not pool_model_in_flight:
                        idle_for = now - binding.last_active_ts if binding.last_active_ts else 0
                        if binding.lease_remaining <= 0 or idle_for >= effective_idle_release:
                            logger.info("[pool %s] Releasing binding for %s", pool_name, model_name.upper())
                            binding.model_name = None
                            binding.lease_remaining = 0

                def _rebind_pool(pool_name: str, new_model_name: str) -> None:
                    binding = pool_bindings[pool_name]
                    if binding.model_name != new_model_name:
                        logger.info(
                            "[pool %s] Bound to %s (min lease %d fxx)",
                            pool_name,
                            new_model_name.upper(),
                            AFFINITY_POOL_LEASE_FXX,
                        )
                    binding.model_name = new_model_name
                    binding.lease_remaining = AFFINITY_POOL_LEASE_FXX
                    binding.last_active_ts = now

                for pool_name in sticky_pool_names:
                    binding = pool_bindings[pool_name]
                    if binding.model_name and renderable_by_model.get(binding.model_name):
                        if binding.lease_remaining > 0:
                            continue
                        current_rank = MODEL_PRIORITY_RANK.get(binding.model_name, 999)
                        reserved_models = {
                            b.model_name
                            for other_name, b in pool_bindings.items()
                            if other_name != pool_name and b.model_name
                        }
                        for candidate in active_candidates:
                            if candidate == binding.model_name or candidate in reserved_models:
                                continue
                            if MODEL_PRIORITY_RANK.get(candidate, 999) < current_rank:
                                _rebind_pool(pool_name, candidate)
                                break
                    elif not binding.model_name:
                        reserved_models = {
                            b.model_name
                            for other_name, b in pool_bindings.items()
                            if other_name != pool_name and b.model_name
                        }
                        bound_any = False
                        for candidate in active_candidates:
                            if candidate in reserved_models:
                                continue
                            _rebind_pool(pool_name, candidate)
                            bound_any = True
                            break
                        if not bound_any and trickle_mode:
                            for candidate in active_candidates:
                                if not _available_renderable(candidate):
                                    continue
                                current_capacity = _bound_worker_capacity(candidate)
                                if (
                                    len(_available_renderable(candidate)) > current_capacity
                                    or len(active_candidates) == 1
                                ):
                                    _rebind_pool(pool_name, candidate)
                                    break

                for pool_name in sticky_pool_names:
                    slots = pool_slots.get(pool_name, 0)
                    if slots <= 0:
                        continue
                    binding = pool_bindings[pool_name]
                    model_name = binding.model_name
                    if not model_name:
                        continue
                    state = run_states.get(model_name)
                    renderable = _available_renderable(model_name)
                    downloaded = downloaded_by_model.get(model_name)
                    if state is None or not renderable or not downloaded:
                        continue
                    remaining_slots = slots
                    for fxx in renderable:
                        if remaining_slots <= 0:
                            break
                        burst_subworkers, burst_reason = _decide_burst_subworkers(
                            model_name,
                            lowpri_in_flight=len(low_priority_in_flight),
                            lowpri_shed_active=lowpri_shed_active,
                        )
                        burst_subworkers = min(
                            burst_subworkers,
                            pool_configs[pool_name].burst_subworkers,
                        )
                        logger.info(
                            "[pool %s] [%s] fxx=%02d dispatch mode: burst_subworkers=%d reason=%s",
                            pool_name,
                            model_name.upper(),
                            fxx,
                            burst_subworkers,
                            burst_reason,
                        )
                        if _submit_render_job(
                            pool_name,
                            model_name,
                            fxx,
                            burst_subworkers,
                            burst_reason,
                        ):
                            remaining_slots -= 1
                        elif pool_broken.get(pool_name):
                            break

                    if remaining_slots <= 0 or pool_broken.get(pool_name):
                        continue

                    idle_for = now - binding.last_active_ts if binding.last_active_ts else 0
                    if (
                        binding.model_name
                        and effective_hold_open > 0
                        and idle_for < effective_hold_open
                    ):
                        continue

                    # Prefer the bound model, but do not leave the pool half idle if
                    # another ready model can use the spare slot.
                    spillover_candidates = [
                        candidate
                        for candidate in MODEL_PRIORITY_ORDER
                        if candidate != model_name and _available_renderable(candidate)
                    ]
                    for spill_model_name in spillover_candidates:
                        if remaining_slots <= 0:
                            break
                        spill_renderable = _available_renderable(spill_model_name)
                        for fxx in spill_renderable:
                            if remaining_slots <= 0:
                                break
                            burst_subworkers, burst_reason = _decide_burst_subworkers(
                                spill_model_name,
                                lowpri_in_flight=len(low_priority_in_flight),
                                lowpri_shed_active=lowpri_shed_active,
                            )
                            burst_subworkers = min(
                                burst_subworkers,
                                pool_configs[pool_name].burst_subworkers,
                            )
                            logger.info(
                                "[pool %s] [%s] fxx=%02d dispatch mode: burst_subworkers=%d reason=%s spillover",
                                pool_name,
                                spill_model_name.upper(),
                                fxx,
                                burst_subworkers,
                                burst_reason,
                            )
                            if _submit_render_job(
                                pool_name,
                                spill_model_name,
                                fxx,
                                burst_subworkers,
                                burst_reason,
                            ):
                                remaining_slots -= 1
                            elif pool_broken.get(pool_name):
                                break
                        if pool_broken.get(pool_name):
                            break

                catchall_name = "catchall"
                slots = pool_slots.get(catchall_name, 0)
                reserved_models = {
                    binding.model_name for binding in pool_bindings.values() if binding.model_name
                }
                if slots > 0 and catchall_name in render_pools:
                    dispatched_in_catchall = False
                    for model_name in MODEL_PRIORITY_ORDER:
                        if slots <= 0:
                            break
                        if model_name in reserved_models:
                            continue
                        state = run_states.get(model_name)
                        renderable = _available_renderable(model_name)
                        downloaded = downloaded_by_model.get(model_name)
                        if state is None or not renderable or not downloaded:
                            continue
                        for fxx in renderable:
                            if slots <= 0:
                                break
                            logger.info(
                                "[pool catchall] [%s] fxx=%02d dispatch mode: burst_subworkers=1 reason=catchall",
                                model_name.upper(),
                                fxx,
                            )
                            if _submit_render_job(
                                catchall_name,
                                model_name,
                                fxx,
                                1,
                                "catchall",
                            ):
                                slots -= 1
                                dispatched_in_catchall = True
                            elif pool_broken.get(catchall_name):
                                pool_broken[catchall_name] = True
                                break

                    spillover_slots = slots
                    if trickle_mode:
                        spillover_slots = max(
                            0,
                            slots - min(TRICKLE_RESERVED_CATCHALL_SLOTS, pool_configs[catchall_name].worker_count),
                        )

                    if spillover_slots > 0 and not pool_broken.get(catchall_name):
                        # If no unbound model can use catch-all, let it help the bound
                        # pools instead of sitting empty.
                        for model_name in MODEL_PRIORITY_ORDER:
                            if spillover_slots <= 0:
                                break
                            spill_renderable = _available_renderable(model_name)
                            if not spill_renderable:
                                continue
                            for fxx in spill_renderable:
                                if spillover_slots <= 0:
                                    break
                                logger.info(
                                    "[pool catchall] [%s] fxx=%02d dispatch mode: burst_subworkers=1 reason=catchall spillover",
                                    model_name.upper(),
                                    fxx,
                                )
                                if _submit_render_job(
                                    catchall_name,
                                    model_name,
                                    fxx,
                                    1,
                                    "catchall spillover",
                                ):
                                    spillover_slots -= 1
                                    dispatched_in_catchall = True
                                elif pool_broken.get(catchall_name):
                                    break
                            if pool_broken.get(catchall_name):
                                break
            else:
                idle_workers = pool_slots.get("shared", 0)
                if idle_workers > 0:
                    dispatch_plan, shared_fairness_cursor = _shared_dispatch_plan(
                        renderable_by_model, idle_workers, shared_fairness_cursor,
                    )
                    for model_name, fxx in dispatch_plan:
                        state = run_states.get(model_name)
                        downloaded = downloaded_by_model.get(model_name)
                        if state is None or not downloaded:
                            continue
                        out_dir = _run_dir(model_name, state.run_dt)
                        out_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
                        item = _build_render_item(state.model, state.run_dt, fxx, downloaded)
                        if item is None:
                            continue
                        if not admission.reserve((model_name, fxx)):
                            continue
                        burst_subworkers, burst_reason = _decide_burst_subworkers(
                            model_name,
                            lowpri_in_flight=len(low_priority_in_flight),
                            lowpri_shed_active=lowpri_shed_active,
                        )
                        logger.info(
                            "[%s] fxx=%02d dispatch mode: burst_subworkers=%d reason=%s",
                            model_name.upper(),
                            fxx,
                            burst_subworkers,
                            burst_reason,
                        )
                        try:
                            future = _ensure_pool("shared").submit(
                                _render_model_batch,
                                [item],
                                str(out_dir),
                                state.run_dt.isoformat(),
                                state.render_params,
                                state.extra_soundings,
                                model_name,
                                "shared",
                                burst_subworkers,
                                burst_reason,
                            )
                            future._wx_run_id = state.run_id
                            in_flight[(model_name, fxx)] = ("shared", future)
                        except BrokenProcessPool:
                            pool_broken["shared"] = True
                            admission.release((model_name, fxx))
                            break

            # --- Low Priority Lane: Dispatch oldest available data via file queue ---
            # LP is overflow/backfill only: shared workers get first claim.
            shared_full = len(in_flight) >= total_render_workers
            # Respect the memory-pressure throttle — don't dispatch more than target allows
            lowpri_slots = max(0, lowpri_target_workers - len(low_priority_in_flight))
            if not any(pool_broken.values()) and lowpri_slots > 0 and shared_full:
                # Prefer oldest active runs for LP backfill, regardless of
                # model priority. Priority only breaks ties among same-age runs.
                lp_states = sorted(
                    run_states.values(),
                    key=lambda s: (
                        s.run_dt,
                        MODEL_PRIORITY_RANK.get(s.model.name, 999),
                    ),
                )
                for state in lp_states:
                    if lowpri_slots <= 0:
                        break
                    dl_info = dl_mgr.get_downloaded(state.model.name)
                    if dl_info is None:
                        continue
                    _, _, downloaded = dl_info

                    # In-flight fxx in either pool
                    model_in_flight_fxx = {f for mn, f in in_flight if mn == state.model.name}
                    model_in_flight_fxx.update({f for mn, f in low_priority_in_flight if mn == state.model.name})

                    renderable = state.todo(model_in_flight_fxx)
                    renderable = [f for f in renderable if f in downloaded]

                    if renderable:
                        out_dir = _run_dir(state.model.name, state.run_dt)
                        out_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
                        for fxx in renderable[:lowpri_slots]:
                            item = _build_render_item(state.model, state.run_dt, fxx, downloaded)
                            if not item:
                                continue
                            job_id = f"{state.model.name}_{state.run_dt.strftime('%Y%m%d%H')}_f{fxx:03d}_{int(time.time()*1000)}"
                            job = {
                                "job_id": job_id,
                                "model_name": state.model.name,
                                "run_id": state.run_dt.strftime("%Y%m%d%H"),
                                "run_dt_iso": state.run_dt.isoformat(),
                                "fxx": fxx,
                                "out_dir": str(out_dir),
                                "grib_path_str": item["grib_path_str"],
                                "prev_grib_str": item.get("prev_grib_str"),
                                "lookback_strs": item.get("lookback_strs"),
                                "render_params": state.render_params,
                                "extra_soundings": state.extra_soundings,
                            }
                            tmp = PENDING_DIR / f".{job_id}.tmp"
                            tmp.write_text(json.dumps(job))
                            tmp.rename(PENDING_DIR / f"{job_id}.json")
                            low_priority_in_flight[(state.model.name, fxx)] = (job_id, now)
                            lowpri_slots -= 1
                            logger.info("[LP] Queued %s fxx=%02d (job %s)",
                                        state.model.name.upper(), fxx, job_id)
                            if lowpri_slots <= 0:
                                break

            # --- Memory pressure check between dispatches ---
            if in_flight or low_priority_in_flight:
                is_critical, mem_pct = check_memory_critical()
                if is_critical:
                    logger.error("Memory critical (%.1f%%) — pausing dispatches", mem_pct)
                    clear_all_caches()
                    time.sleep(30)

            # --- Periodic status logging ---
            if now - last_status_log > STATUS_LOG_INTERVAL:
                last_status_log = now
                total_in_flight = len(in_flight) + len(low_priority_in_flight)
                pool_layout = _pool_slot_layout(pool_configs)
                pool_activity = _pool_slot_activity(pool_configs, in_flight)
                if total_in_flight > 0:
                    models_rendering: dict[str, list[int]] = {}
                    pool_rendering: dict[str, list[str]] = {}
                    for (mn, fxx), (pool_name, _future) in in_flight.items():
                        models_rendering.setdefault(mn, []).append(fxx)
                        pool_rendering.setdefault(pool_name, []).append(f"{mn.upper()} f{fxx:02d}")
                    
                    lp_parts = []
                    for (mn, fxx) in low_priority_in_flight:
                        lp_parts.append(f"{mn.upper()} f{fxx:02d}")

                    parts = []
                    for mn in MODEL_PRIORITY_ORDER:
                        if mn in models_rendering:
                            state = run_states.get(mn)
                            rid = state.run_id if state else "?"
                            n_done = len(state.completed) if state else 0
                            n_total = len(state.fxx_list) if state else 0
                            parts.append(
                                f"[{mn.upper()}] {rid} "
                                f"({len(models_rendering[mn])}w, {n_done}/{n_total})"
                            )
                    
                    lp_str = f" [LP: {', '.join(lp_parts)}]" if lp_parts else ""
                    pool_str = ""
                    if affinity_pool_mode and pool_rendering:
                        pool_parts = []
                        for pool_name in ("affinity_a", "affinity_b", "affinity_c", "catchall"):
                            entries = pool_rendering.get(pool_name)
                            if entries:
                                pool_parts.append(f"{pool_name}={', '.join(entries[:4])}")
                        if pool_parts:
                            pool_str = f" [pools: {'; '.join(pool_parts)}]"
                    slot_str = f" [slots: {' '.join(pool_activity)}]" if pool_activity else ""
                    logger.info(
                        "Rendering %d/%d workers: %s%s",
                        total_in_flight, total_render_workers,
                        " + ".join(parts), lp_str + pool_str + slot_str,
                    )
                    # Write pipeline state for the highest-priority active model
                    for mn in MODEL_PRIORITY_ORDER:
                        if mn in models_rendering:
                            state = run_states.get(mn)
                            if state:
                                _write_state({
                                    "status": "processing",
                                    "model": mn,
                                    "current_run": state.run_id,
                                    "completed_fxx": sorted(state.completed),
                                    "total_fxx": len(state.fxx_list),
                                    "trickle_mode": trickle_mode,
                                    "resources": admission.status(),
                                    "queue_layout": pool_layout,
                                    "queue_activity": pool_activity,
                                    "queue_layout_text": " ".join(pool_layout),
                                    "queue_activity_text": " | ".join(pool_activity),
                                })
                            break
                else:
                    active = dl_mgr.get_all_active_runs()
                    if active:
                        parts = [
                            f"[{mn.upper()}] {rid}"
                            for mn, (_, rid) in active.items()
                        ]
                        _write_state({
                            "status": "downloading",
                            "active_downloads": {mn: rid for mn, (_, rid) in active.items()},
                            "resources": admission.status(),
                            "trickle_mode": trickle_mode if affinity_pool_mode else False,
                            "queue_layout": pool_layout,
                            "queue_activity": pool_activity,
                            "queue_layout_text": " ".join(pool_layout),
                            "queue_activity_text": " | ".join(pool_activity),
                        })
                        logger.info("Downloading (no renders ready): %s",
                                    " + ".join(parts))
                    else:
                        _write_state({
                            "status": "idle",
                            "waiting": True,
                            "resources": admission.status(),
                            "trickle_mode": trickle_mode if affinity_pool_mode else False,
                            "queue_layout": pool_layout,
                            "queue_activity": pool_activity,
                            "queue_layout_text": " ".join(pool_layout),
                            "queue_activity_text": " | ".join(pool_activity),
                        })
                        logger.debug("No pending work")

        except KeyboardInterrupt:
            logger.info("Scheduler interrupted — exiting")
            radar_stop_event.set()
            dl_mgr.stop()
            for pool in render_pools.values():
                try:
                    pool.shutdown(wait=False)
                except Exception:
                    pass
            break
        except Exception as exc:
            logger.error("Scheduler error: %s", exc, exc_info=True)

        time.sleep(DISPATCH_INTERVAL)


if __name__ == "__main__":
    main()
