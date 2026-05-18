"""
RRFS (Rapid Refresh Forecast System) direct S3 downloader.

Herbie's RRFS template is outdated — the S3 bucket structure changed from
``rrfs_a.{date}`` to ``rrfsens.{date}`` and filenames now include the member
identifier and ``3km`` resolution tag.

Actual S3 path (as of 2026-03):
  s3://noaa-rrfs-pds/rrfs_a/rrfsens.{YYYYMMDD}/{HH}/m001/
      rrfs.t{HH}z.m001.prslev.3km.f{FFF}.conus.grib2
      rrfs.t{HH}z.m001.prslev.3km.f{FFF}.conus.grib2.idx

This module downloads subsets via HTTP range requests using the .idx file,
identical to the approach Herbie uses internally.
"""

import logging
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError

from ..config import GRIB_CACHE_DIR

logger = logging.getLogger(__name__)

# Public HTTPS endpoint for the S3 bucket (no auth needed)
BASE_URL = "https://noaa-rrfs-pds.s3.amazonaws.com"

# Default ensemble member (m001 is always present; no "control" directory)
DEFAULT_MEMBER = "m001"

# Available run hours
RUN_HOURS = [0, 6, 12, 18]


def _build_url(run_dt: datetime, fxx: int, member: str = DEFAULT_MEMBER) -> str:
    """Build the HTTPS URL for an RRFS CONUS prslev GRIB file."""
    date_str = run_dt.strftime("%Y%m%d")
    hour_str = run_dt.strftime("%H")
    filename = f"rrfs.t{hour_str}z.{member}.prslev.3km.f{fxx:03d}.conus.grib2"
    return f"{BASE_URL}/rrfs_a/rrfsens.{date_str}/{hour_str}/{member}/{filename}"


def _build_idx_url(run_dt: datetime, fxx: int, member: str = DEFAULT_MEMBER) -> str:
    return _build_url(run_dt, fxx, member) + ".idx"


def check_availability(run_dt: datetime, fxx: int = 0) -> bool:
    """Check if RRFS data exists for a given run/fxx via HEAD request on idx."""
    url = _build_idx_url(run_dt, fxx)
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def _parse_idx(idx_text: str) -> list[tuple[int, int, str]]:
    """Parse a wgrib2-style .idx file into (byte_start, byte_end, line) tuples.

    byte_end is -1 for the last record (meaning "to end of file").
    """
    lines = [l.strip() for l in idx_text.strip().splitlines() if l.strip()]
    records = []
    for i, line in enumerate(lines):
        parts = line.split(":")
        if len(parts) < 3:
            continue
        byte_start = int(parts[1])
        records.append((byte_start, line))

    result = []
    for i, (start, line) in enumerate(records):
        if i + 1 < len(records):
            end = records[i + 1][0] - 1
        else:
            end = -1  # last record
        result.append((start, end, line))
    return result


def _match_records(records: list[tuple[int, int, str]],
                   patterns: list[str]) -> list[tuple[int, int]]:
    """Return (start, end) byte ranges for idx records matching any pattern."""
    ranges = []
    for start, end, line in records:
        for pat in patterns:
            if pat in line:
                ranges.append((start, end))
                break
    return ranges


def _download_ranges(grib_url: str, ranges: list[tuple[int, int]],
                     output_path: Path, timeout: int = 120) -> bool:
    """Download specific byte ranges from a GRIB file and write to output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "wb") as f:
        for start, end in ranges:
            if end == -1:
                range_header = f"bytes={start}-"
            else:
                range_header = f"bytes={start}-{end}"
            req = urllib.request.Request(grib_url)
            req.add_header("Range", range_header)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    f.write(resp.read())
            except Exception as exc:
                logger.warning("[RRFS] Range download failed %s: %s", range_header, exc)
                return False
    return output_path.exists() and output_path.stat().st_size > 0


def download_forecast_hour(run_dt: datetime, fxx: int,
                           search_patterns: list[str],
                           member: str = DEFAULT_MEMBER) -> Path | None:
    """Download an RRFS subset for one forecast hour using idx-based ranging.

    search_patterns: list of wgrib2 search strings (e.g. ":TMP:2 m above ground:")
    Returns local Path to the subset GRIB, or None on failure.
    """
    date_str = run_dt.strftime("%Y%m%d")
    hour_str = run_dt.strftime("%H")

    output_dir = GRIB_CACHE_DIR / "rrfs" / date_str
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"rrfs_{date_str}_{hour_str}_f{fxx:03d}.grib2"

    # Skip if already downloaded
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    # Fetch the idx file
    idx_url = _build_idx_url(run_dt, fxx, member)
    try:
        with urllib.request.urlopen(idx_url, timeout=15) as resp:
            idx_text = resp.read().decode("utf-8")
    except Exception as exc:
        logger.debug("[RRFS] idx not available for %s fxx=%03d: %s", hour_str, fxx, exc)
        return None

    records = _parse_idx(idx_text)
    if not records:
        logger.warning("[RRFS] Empty idx for fxx=%03d", fxx)
        return None

    ranges = _match_records(records, search_patterns)
    if not ranges:
        logger.warning("[RRFS] No matching records in idx for fxx=%03d", fxx)
        return None

    grib_url = _build_url(run_dt, fxx, member)
    logger.info("[RRFS] Downloading fxx=%03d (%d fields, %d ranges)",
                fxx, len(search_patterns), len(ranges))

    if _download_ranges(grib_url, ranges, output_path):
        logger.info("[RRFS] Downloaded fxx=%03d → %s", fxx, output_path.name)
        return output_path

    # Clean up partial file
    output_path.unlink(missing_ok=True)
    return None
