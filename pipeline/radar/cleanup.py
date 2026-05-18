"""Radar data retention and cleanup."""

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .config import (
    RADAR_CACHE_DIR, RADAR_RENDERED_DIR,
    RAW_RETENTION_HOURS, RENDERED_RETENTION_HOURS, ONDEMAND_RETENTION_MINUTES,
)

logger = logging.getLogger(__name__)


def cleanup_raw_files():
    """Delete raw .ar2v and chunk files older than RAW_RETENTION_HOURS."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RAW_RETENTION_HOURS)
    cutoff_ts = cutoff.timestamp()
    removed = 0

    for ar2v in RADAR_CACHE_DIR.rglob("*.ar2v"):
        try:
            if ar2v.stat().st_mtime < cutoff_ts:
                ar2v.unlink()
                removed += 1
        except OSError:
            pass

    for chunkbin in RADAR_CACHE_DIR.rglob("*.chunkbin"):
        try:
            if chunkbin.stat().st_mtime < cutoff_ts:
                chunkbin.unlink()
                removed += 1
        except OSError:
            pass

    # Also clean up any stale .tmp files (failed downloads)
    for tmp in RADAR_CACHE_DIR.rglob("*.tmp"):
        try:
            if tmp.stat().st_mtime < cutoff_ts:
                tmp.unlink()
        except OSError:
            pass

    for parts_dir in RADAR_CACHE_DIR.rglob("*.parts"):
        try:
            if parts_dir.is_dir() and parts_dir.stat().st_mtime < cutoff_ts:
                for child in parts_dir.iterdir():
                    child.unlink(missing_ok=True)
                parts_dir.rmdir()
        except OSError:
            pass

    if removed:
        logger.info(f"Cleaned {removed} raw Level II files")
    return removed


def cleanup_rendered_files():
    """Delete rendered PNGs and metadata older than RENDERED_RETENTION_HOURS."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RENDERED_RETENTION_HOURS)
    cutoff_ts = cutoff.timestamp()
    removed = 0

    png_dir = RADAR_RENDERED_DIR / "png"
    meta_dir = RADAR_RENDERED_DIR / "meta"

    for d in [png_dir, meta_dir]:
        if not d.exists():
            continue
        for f in d.iterdir():
            # Skip symlinks (latest pointers)
            if f.is_symlink():
                # Remove broken symlinks
                if not f.resolve().exists():
                    f.unlink(missing_ok=True)
                continue
            try:
                if f.stat().st_mtime < cutoff_ts:
                    f.unlink()
                    removed += 1
            except OSError:
                pass

    if removed:
        logger.info(f"Cleaned {removed} rendered radar files")
    return removed


def cleanup_all():
    """Run all cleanup tasks."""
    raw = cleanup_raw_files()
    rendered = cleanup_rendered_files()
    return raw + rendered
