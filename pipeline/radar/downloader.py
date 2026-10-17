"""Level II radar downloader with direct S3 primary and THREDDS fallback."""

import logging
import re
import time
import threading
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import boto3
from boto3.s3.transfer import TransferConfig
import botocore
import requests
from botocore.client import Config
from siphon.radarserver import get_radarserver_datasets, RadarServer

from .config import RADAR_CACHE_DIR, NEXRAD_SITES, MAX_VOLUME_AGE_HOURS

logger = logging.getLogger(__name__)

PRIMARY_BUCKET = "unidata-nexrad-level2"
CHUNK_BUCKET = "unidata-nexrad-level2-chunks"
MIN_VALID_SIZE_BYTES = 500_000
MIN_CHUNK_FILES = 15
TARGET_CHUNK_FILES = 15
CHUNK_PREFER_GRACE = timedelta(minutes=2)
S3_FALLBACK_COOLDOWN = timedelta(seconds=90)
_CHUNK_KEY_RE = re.compile(r"(?P<site>[A-Z0-9]{4})/(?P<channel>\d+)/(?P<stamp>\d{8}-\d{6})-(?P<seq>\d{3})-(?P<kind>[A-Z])$")
_PATH_TS_RE = re.compile(r"(\d{8})_(\d{4,6})")

# Shared RadarServer singleton
_l2_server: Optional[RadarServer] = None
_s3_bucket = None
_chunk_bucket = None
_tls = threading.local()
_s3_cooldowns: dict[str, tuple[datetime, str]] = {}
_s3_cooldown_lock = threading.Lock()
_s3_client_lock = threading.Lock()
_s3_client = None
_TRANSFER_CONFIG = TransferConfig(use_threads=False, max_concurrency=1)


class _S3Bucket:
    """Thread-safe client adapter with one transfer per download worker."""

    def __init__(self, name: str) -> None:
        global _s3_client
        with _s3_client_lock:
            if _s3_client is None:
                _s3_client = boto3.session.Session().client(
                    "s3", region_name="us-east-1",
                    config=Config(signature_version=botocore.UNSIGNED,
                                  max_pool_connections=32, connect_timeout=15,
                                  read_timeout=60, retries={"max_attempts": 2},
                                  user_agent_extra="weather-viewer"),
                )
        self.client = _s3_client
        self.name = name
        self.objects = self

    def filter(self, **kwargs):
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.name, **kwargs):
            for obj in page.get("Contents", []):
                yield SimpleNamespace(key=obj["Key"])

    def download_file(self, key: str, path: str) -> None:
        self.client.download_file(self.name, key, path, Config=_TRANSFER_CONFIG)


def get_l2_server() -> RadarServer:
    """Get or create the shared Level II RadarServer connection."""
    global _l2_server
    if _l2_server is None:
        ds = get_radarserver_datasets('https://thredds.ucar.edu/thredds/')
        l2_ds = ds['NEXRAD Level II Radar from IDD']
        url = l2_ds.follow().catalog_url
        _l2_server = RadarServer(url)
        logger.info(f"Connected to Level II THREDDS ({len(_l2_server.stations)} stations)")
    return _l2_server


def get_s3_bucket():
    """Get or create the shared unsigned S3 bucket handle."""
    global _s3_bucket
    if _s3_bucket is None:
        _s3_bucket = _S3Bucket(PRIMARY_BUCKET)
        logger.info("Connected to Level II S3 bucket (%s)", PRIMARY_BUCKET)
    return _s3_bucket


def get_chunk_bucket():
    """Get or create the shared unsigned chunk-bucket handle."""
    global _chunk_bucket
    if _chunk_bucket is None:
        _chunk_bucket = _S3Bucket(CHUNK_BUCKET)
        logger.info("Connected to chunk S3 bucket (%s)", CHUNK_BUCKET)
    return _chunk_bucket


class Level2Downloader:
    """Download Level II volume scans with direct S3 primary and THREDDS fallback."""

    def __init__(self):
        self.bucket = get_s3_bucket()
        self.chunk_bucket = None
        self.rs: Optional[RadarServer] = None

    def _session(self) -> requests.Session:
        """Thread-local HTTP session for connection reuse."""
        sess = getattr(_tls, "session", None)
        if sess is None:
            sess = requests.Session()
            _tls.session = sess
        return sess

    def _get_rs(self) -> RadarServer:
        if self.rs is None:
            self.rs = get_l2_server()
        return self.rs

    @staticmethod
    def _get_s3_cooldown(site: str) -> tuple[Optional[datetime], Optional[str]]:
        with _s3_cooldown_lock:
            until_reason = _s3_cooldowns.get(site)
        if until_reason is None:
            return None, None
        until, reason = until_reason
        if until <= datetime.now(timezone.utc):
            with _s3_cooldown_lock:
                _s3_cooldowns.pop(site, None)
            return None, None
        return until, reason

    @staticmethod
    def _set_s3_cooldown(site: str, reason: str) -> None:
        until = datetime.now(timezone.utc) + S3_FALLBACK_COOLDOWN
        with _s3_cooldown_lock:
            _s3_cooldowns[site] = (until, reason)

    @staticmethod
    def _clear_s3_cooldown(site: str) -> None:
        with _s3_cooldown_lock:
            _s3_cooldowns.pop(site, None)

    @staticmethod
    def _s3_prefixes(site: str, now: datetime) -> list[str]:
        prefixes = []
        for day_offset in range(MAX_VOLUME_AGE_HOURS // 24 + 2):
            dt = now - timedelta(days=day_offset)
            prefixes.append(f"{dt:%Y/%m/%d}/{site}/")
        return prefixes

    @staticmethod
    def _dt_from_s3_key(key: str) -> Optional[datetime]:
        try:
            name = key.rsplit("/", maxsplit=1)[-1]
            return datetime.strptime(name[4:19], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _local_name_from_s3(site: str, key: str) -> Optional[str]:
        dt = Level2Downloader._dt_from_s3_key(key)
        if dt is None:
            return None
        return f"Level2_{site}_{dt:%Y%m%d_%H%M}.ar2v"

    def _download_s3_latest(self, site: str, cache_dir: Path) -> Optional[Path]:
        now = datetime.now(timezone.utc)
        newest_obj = None
        newest_dt = None

        for prefix in self._s3_prefixes(site, now):
            for obj in self.bucket.objects.filter(Prefix=prefix):
                key = obj.key
                if key.endswith("MDM"):
                    continue
                obj_dt = self._dt_from_s3_key(key)
                if obj_dt is None:
                    continue
                age = now - obj_dt
                if age < timedelta(0) or age > timedelta(hours=MAX_VOLUME_AGE_HOURS):
                    continue
                if newest_dt is None or obj_dt > newest_dt:
                    newest_obj = obj
                    newest_dt = obj_dt

        if newest_obj is None or newest_dt is None:
            logger.debug("%s: no recent Level II objects in S3", site)
            return None

        local_name = self._local_name_from_s3(site, newest_obj.key)
        if local_name is None:
            logger.debug("%s: could not normalize S3 filename %s", site, newest_obj.key)
            return None

        local_path = cache_dir / local_name
        if local_path.exists() and local_path.stat().st_size > MIN_VALID_SIZE_BYTES:
            return local_path

        tmp = local_path.with_suffix(".tmp")
        self.bucket.download_file(newest_obj.key, str(tmp))

        size = tmp.stat().st_size
        if size < MIN_VALID_SIZE_BYTES:
            tmp.unlink(missing_ok=True)
            logger.warning("%s: S3 file too small (%d bytes)", site, size)
            return None

        tmp.rename(local_path)
        logger.info(
            "%s: downloaded %s from S3 (%.1f MB)",
            site,
            local_name,
            local_path.stat().st_size / 1e6,
        )
        return local_path

    @staticmethod
    def _chunk_prefixes(site: str, now: datetime) -> list[str]:
        prefixes = []
        for day_offset in range(MAX_VOLUME_AGE_HOURS // 24 + 2):
            dt = now - timedelta(days=day_offset)
            day = dt.strftime("%Y%m%d-")
            prefixes.append(f"{site}/1/{day}")
            prefixes.append(f"{site}/2/{day}")
        return prefixes

    @staticmethod
    def _parse_chunk_key(key: str) -> Optional[dict]:
        match = _CHUNK_KEY_RE.match(key)
        if not match:
            return None
        try:
            stamp_dt = datetime.strptime(match.group("stamp"), "%Y%m%d-%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
        return {
            "site": match.group("site"),
            "channel": match.group("channel"),
            "stamp": match.group("stamp"),
            "dt": stamp_dt,
            "seq": int(match.group("seq")),
            "kind": match.group("kind"),
        }

    def _latest_chunk_candidate(self, site: str):
        """Find the newest viable chunk volume for a site."""
        now = datetime.now(timezone.utc)
        groups: dict[tuple[str, str], list] = {}
        if self.chunk_bucket is None:
            self.chunk_bucket = get_chunk_bucket()

        for prefix in self._chunk_prefixes(site, now):
            for obj in self.chunk_bucket.objects.filter(Prefix=prefix):
                parsed = self._parse_chunk_key(obj.key)
                if parsed is None:
                    continue
                age = now - parsed["dt"]
                if age < timedelta(0) or age > timedelta(hours=MAX_VOLUME_AGE_HOURS):
                    continue
                groups.setdefault((parsed["channel"], parsed["stamp"]), []).append((parsed, obj))

        if not groups:
            return None

        selected = None
        for (_, stamp), items in groups.items():
            dt = items[0][0]["dt"]
            count = len(items)
            if selected is None:
                selected = (dt, count, stamp, items)
                continue
            best_dt, best_count, _, _ = selected
            if dt > best_dt or (dt == best_dt and count > best_count):
                selected = (dt, count, stamp, items)

        return selected

    def _download_latest_chunks(self, site: str, cache_dir: Path,
                                min_dt: Optional[datetime] = None) -> Optional[Path]:
        """Assemble the earliest useful low-tilt chunk set for a site."""
        selected = self._latest_chunk_candidate(site)
        if selected is None:
            return None

        dt, count, stamp, items = selected
        if count < MIN_CHUNK_FILES:
            logger.debug("%s: latest chunk volume %s has only %d chunks", site, stamp, count)
            return None
        if min_dt is not None and dt < (min_dt - CHUNK_PREFER_GRACE):
            logger.debug(
                "%s: chunk volume %s is too far behind full-volume candidate",
                site,
                stamp,
            )
            return None

        local_name = f"Level2Chunk_{site}_{dt:%Y%m%d}_{dt:%H%M%S}.chunkbin"
        local_path = cache_dir / local_name
        if local_path.exists() and local_path.stat().st_size > MIN_VALID_SIZE_BYTES:
            return local_path

        tmp_dir = cache_dir / f".{local_name}.parts"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        selected_items = sorted(items, key=lambda item: item[0]["seq"])[:TARGET_CHUNK_FILES]
        tmp_path = local_path.with_suffix(".tmp")

        with open(tmp_path, "wb") as out:
            for parsed, obj in selected_items:
                part_path = tmp_dir / f"{parsed['seq']:03d}-{parsed['kind']}"
                self.chunk_bucket.download_file(obj.key, str(part_path))
                out.write(part_path.read_bytes())

        size = tmp_path.stat().st_size
        if size < MIN_VALID_SIZE_BYTES:
            tmp_path.unlink(missing_ok=True)
            logger.warning("%s: chunk file too small (%d bytes)", site, size)
            return None

        tmp_path.rename(local_path)
        logger.info(
            "%s: assembled %d chunks for %s from chunk S3 (%.1f MB)",
            site,
            len(selected_items),
            local_name,
            local_path.stat().st_size / 1e6,
        )
        return local_path

    @staticmethod
    def _datetime_from_local_path(path: Optional[Path]) -> Optional[datetime]:
        if path is None:
            return None
        match = _PATH_TS_RE.search(path.stem)
        if not match:
            return None
        time_part = match.group(2)
        fmt = "%Y%m%d_%H%M%S" if len(time_part) == 6 else "%Y%m%d_%H%M"
        try:
            return datetime.strptime(
                f"{match.group(1)}_{time_part}",
                fmt,
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _prefer_full_volume(full_path: Optional[Path], chunk_path: Optional[Path]) -> Optional[Path]:
        if full_path is None:
            return chunk_path
        if chunk_path is None:
            return full_path

        full_dt = Level2Downloader._datetime_from_local_path(full_path)
        chunk_dt = Level2Downloader._datetime_from_local_path(chunk_path)
        if full_dt is None or chunk_dt is None:
            return full_path

        if chunk_dt >= (full_dt - CHUNK_PREFER_GRACE):
            return chunk_path

        return full_path

    def _download_thredds_latest(self, site: str, cache_dir: Path) -> Optional[Path]:
        rs = self._get_rs()

        # Query THREDDS for recent scans
        query = rs.query()
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=MAX_VOLUME_AGE_HOURS)
        query.stations(site).time_range(start, end).variables("Reflectivity")

        if not rs.validate_query(query):
            logger.debug(f"Invalid query for {site}")
            return None

        cat = rs.get_catalog(query)
        if not cat.datasets:
            logger.debug(f"No Level II data for {site}")
            return None

        url = ""
        local_name = ""

        # Sort newest first and prefer the newest object. THREDDS sometimes surfaces
        # incomplete entries, so fall back to the next item if the first one fails later.
        datasets = sorted(cat.datasets.items(), key=lambda x: x[0], reverse=True)
        for name, dataset in datasets[:3]:
            url = dataset.access_urls.get("HTTPServer", "")
            if not url:
                parts = name.replace(".ar2v", "").split("_")
                if len(parts) >= 3:
                    date_str = parts[2]
                    url = (
                        "https://thredds.ucar.edu/thredds/fileServer/"
                        f"nexrad/level2/IDD/{site}/{date_str}/{name}"
                    )
            if not url:
                continue
            local_name = name
            break

        if not url or not local_name:
            logger.debug(f"No HTTP URL for {site}")
            return None

        local_path = cache_dir / local_name
        if local_path.exists() and local_path.stat().st_size > MIN_VALID_SIZE_BYTES:
            return local_path

        resp = self._session().get(url, timeout=60, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "").lower()
        if "html" in content_type or "text" in content_type:
            logger.warning(f"{site}: received HTML instead of binary")
            return None

        tmp = local_path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

        size = tmp.stat().st_size
        if size < MIN_VALID_SIZE_BYTES:
            tmp.unlink(missing_ok=True)
            logger.warning(f"{site}: THREDDS file too small ({size} bytes)")
            return None

        tmp.rename(local_path)
        logger.info(
            "%s: downloaded %s from THREDDS (%.1f MB)",
            site,
            local_name,
            local_path.stat().st_size / 1e6,
        )
        return local_path

    def download_latest(self, site: str, allow_chunks: bool = True) -> Optional[Path]:
        """Download the most recent Level II volume scan for a site.

        Args:
            site: 4-letter NEXRAD site code (e.g., 'KTLX')

        Returns:
            Path to downloaded .ar2v file, or None if unavailable
        """
        if site not in NEXRAD_SITES:
            logger.warning(f"Unknown site: {site}")
            return None

        cache_dir = RADAR_CACHE_DIR / site
        cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            cooldown_until, cooldown_reason = self._get_s3_cooldown(site)
            try:
                if cooldown_until is None:
                    local_path = self._download_s3_latest(site, cache_dir)
                    if allow_chunks:
                        full_dt = self._datetime_from_local_path(local_path)
                        chunk_path = self._download_latest_chunks(site, cache_dir, min_dt=full_dt)
                        preferred = self._prefer_full_volume(local_path, chunk_path)
                        if preferred is not None:
                            self._clear_s3_cooldown(site)
                            return preferred
                    elif local_path is not None:
                        self._clear_s3_cooldown(site)
                        return local_path
                    self._set_s3_cooldown(site, "no_recent_s3_volume")
                else:
                    logger.debug(
                        "%s: skipping S3 primary for %.0fs after %s",
                        site,
                        max((cooldown_until - datetime.now(timezone.utc)).total_seconds(), 0.0),
                        cooldown_reason,
                    )
            except Exception as s3_error:
                self._set_s3_cooldown(site, type(s3_error).__name__)
                logger.warning("%s: S3 primary failed: %s", site, s3_error)

            logger.warning("%s: S3 primary unavailable, falling back to THREDDS", site)
            time.sleep(0.1)
            return self._download_thredds_latest(site, cache_dir)
        except requests.exceptions.HTTPError as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 403:
                logger.warning(f"{site}: rate limited (403)")
                time.sleep(3)
            else:
                logger.warning(f"{site}: HTTP error: {e}")
            return None
        except Exception as e:
            logger.warning(f"{site}: download failed: {e}")
            return None

    def list_cached_volumes(self, site: str) -> list[Path]:
        """List cached volume scan files for a site, newest first."""
        cache_dir = RADAR_CACHE_DIR / site
        if not cache_dir.exists():
            return []
        files = sorted(cache_dir.glob("*.ar2v"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files
