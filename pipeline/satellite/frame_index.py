"""In-memory index of rendered satellite frames.

Replaces the glob-the-entire-meta-directory pattern that previously ran for
every product x region on every maintenance cycle (the meta dir holds tens of
thousands of JSON files, so each glob+parse pass cost seconds of CPU).

The index is built once at startup from a single directory scan, then kept
current incrementally: every frame meta write goes through ``record()`` and
every cleanup deletion goes through ``prune()``.  A periodic ``rebuild()``
self-heals any drift caused by external cleanup (cron) deleting files.

Per-key frame manifests are written under ``manifests/`` so the web API can
serve frame lists from one small file instead of globbing the meta dir.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_FILENAME_TS_RE = re.compile(r"_(\d{7})_(\d{4})(?:_|$)")


def parse_satellite_timestamp(ts_str: str) -> datetime | None:
    """Parse a satellite meta timestamp (e.g. '2026-161T13:41:00Z')."""
    if not ts_str:
        return None
    for fmt in ("%Y-%jT%H:%M:%SZ", "%Y-%jT%H:%MZ"):
        try:
            return datetime.strptime(ts_str, fmt)
        except Exception:
            continue
    try:
        ts_iso = re.sub(r"T(\d{2})-(\d{2})-(\d{2})", r"T\1:\2:\3", ts_str)
        return datetime.fromisoformat(ts_iso.replace("Z", ""))
    except Exception:
        return None


def parse_satellite_timestamp_from_filename(name: str) -> datetime | None:
    match = _FILENAME_TS_RE.search(name)
    if not match:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%Y%j%H%M")
    except ValueError:
        return None


def normalize_satellite_id(raw: str) -> str:
    """Normalize 'G19' / 'GOES-19' / 'goes19' to 'goes19' (same rules as web API)."""
    sat = str(raw or "").lower()
    if sat.startswith("goes-") and sat[5:].isdigit():
        return f"goes{sat[5:]}"
    if sat.startswith("g") and not sat.startswith("goes") and sat[1:].isdigit():
        return f"goes{sat[1:]}"
    return sat


class FrameIndex:
    """Thread-safe index of rendered frames keyed by (satellite, product, region)."""

    def __init__(self, meta_dir: Path, manifest_dir: Path, data_root: Path):
        self.meta_dir = Path(meta_dir)
        self.manifest_dir = Path(manifest_dir)
        self.data_root = Path(data_root)
        self._lock = threading.Lock()
        # key -> {meta_name: (ts_epoch, frame_payload_dict)}
        self._frames: dict[tuple[str, str, str | None], dict[str, tuple[float, dict]]] = {}
        self._dirty_keys: set[tuple[str, str, str | None]] = set()
        self.last_rebuild = 0.0

    # ------------------------------------------------------------------
    # Key/derivation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _key_from_meta(meta_name: str, meta: dict) -> tuple[str, str, str | None] | None:
        product = meta.get("product")
        if not product or not isinstance(product, str):
            return None
        satellite = normalize_satellite_id(meta.get("satellite", ""))
        if not satellite:
            # Fall back to the filename prefix (e.g. goes19_ir_...).
            satellite = meta_name.split("_", 1)[0]
        region = meta.get("region")
        if region is not None:
            region = str(region)
        return (satellite, product, region)

    @staticmethod
    def _frame_payload(meta: dict) -> dict:
        return {
            "timestamp": meta.get("timestamp", ""),
            "png_path": meta.get("png_path", ""),
            "bounds": meta.get("bounds"),
            "region": meta.get("region"),
        }

    def _frame_ts(self, meta_name: str, meta: dict) -> float | None:
        dt = parse_satellite_timestamp(str(meta.get("timestamp", "")))
        if dt is None:
            dt = parse_satellite_timestamp_from_filename(meta_name)
        if dt is None:
            return None
        # Frame timestamps are UTC but parsed naive; pin the zone so
        # .timestamp() doesn't apply the server's local-time offset.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    def manifest_path(self, satellite: str, product: str, region: str | None) -> Path:
        name = f"{satellite}_{product}" if region is None else f"{satellite}_{product}_{region}"
        return self.manifest_dir / f"{name}.json"

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record(self, meta_path: Path, meta: dict) -> None:
        """Register a freshly written frame meta. Called from render threads."""
        meta_name = Path(meta_path).name
        if meta_name.endswith("_latest.json"):
            return
        key = self._key_from_meta(meta_name, meta)
        if key is None:
            return
        ts = self._frame_ts(meta_name, meta)
        if ts is None:
            return
        with self._lock:
            self._frames.setdefault(key, {})[meta_name] = (ts, self._frame_payload(meta))
            self._dirty_keys.add(key)

    def prune(
        self,
        satellite: str,
        product: str,
        region: str | None,
        *,
        keep: int,
        max_age_seconds: float,
        now: float | None = None,
    ) -> int:
        """Delete frames beyond ``keep`` newest or older than ``max_age_seconds``.

        Unlinks both the meta JSON and the PNG, and drops index entries.
        Returns the number of frames removed.
        """
        now = now if now is not None else time.time()
        key = (satellite, product, region)
        with self._lock:
            entries = sorted(
                self._frames.get(key, {}).items(),
                key=lambda item: item[1][0],
                reverse=True,
            )
            doomed = [
                (meta_name, payload)
                for idx, (meta_name, (ts, payload)) in enumerate(entries)
                if idx >= keep or (now - ts) > max_age_seconds
            ]
            for meta_name, _ in doomed:
                self._frames[key].pop(meta_name, None)
            if doomed:
                self._dirty_keys.add(key)
        for meta_name, payload in doomed:
            (self.meta_dir / meta_name).unlink(missing_ok=True)
            png_rel = payload.get("png_path")
            if png_rel:
                (self.data_root / str(png_rel)).unlink(missing_ok=True)
        return len(doomed)

    def remove(self, satellite: str, product: str, region: str | None, meta_name: str) -> None:
        key = (satellite, product, region)
        with self._lock:
            if self._frames.get(key, {}).pop(meta_name, None) is not None:
                self._dirty_keys.add(key)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def latest_meta_name(self, satellite: str, product: str, region: str | None) -> str | None:
        key = (normalize_satellite_id(satellite), product, region)
        with self._lock:
            entries = self._frames.get(key)
            if not entries:
                return None
            return max(entries.items(), key=lambda item: item[1][0])[0]

    def frame_count(self, satellite: str, product: str, region: str | None) -> int:
        key = (normalize_satellite_id(satellite), product, region)
        with self._lock:
            return len(self._frames.get(key, {}))

    # ------------------------------------------------------------------
    # Rebuild / self-heal
    # ------------------------------------------------------------------

    def rebuild(self, purge_orphans: bool = True) -> tuple[int, int]:
        """Full rescan of the meta dir. Returns (indexed, orphans_removed).

        Orphans are metas whose PNG no longer exists, plus unparseable metas
        older than a minute (a fresh file may still be mid-write).
        """
        start = time.time()
        frames: dict[tuple[str, str, str | None], dict[str, tuple[float, dict]]] = {}
        orphans = 0
        indexed = 0
        for meta_file in self.meta_dir.glob("*.json"):
            name = meta_file.name
            if name.endswith("_latest.json"):
                continue
            try:
                meta = json.loads(meta_file.read_text())
            except Exception:
                try:
                    if (start - meta_file.stat().st_mtime) > 60 and purge_orphans:
                        meta_file.unlink(missing_ok=True)
                        orphans += 1
                except OSError:
                    pass
                continue
            key = self._key_from_meta(name, meta)
            ts = self._frame_ts(name, meta)
            if key is None or ts is None:
                continue
            png_rel = meta.get("png_path")
            if purge_orphans and png_rel and not (self.data_root / str(png_rel)).exists():
                meta_file.unlink(missing_ok=True)
                orphans += 1
                continue
            frames.setdefault(key, {})[name] = (ts, self._frame_payload(meta))
            indexed += 1
        with self._lock:
            self._frames = frames
            self._dirty_keys = set(frames.keys())
        self.last_rebuild = time.time()
        logger.info(
            "Satellite frame index rebuilt: %d frames across %d products, "
            "%d orphans removed (%.1fs)",
            indexed, len(frames), orphans, self.last_rebuild - start,
        )
        return indexed, orphans

    # ------------------------------------------------------------------
    # Manifests
    # ------------------------------------------------------------------

    def flush_manifests(self) -> int:
        """Write manifests for keys whose frames changed. Returns count written."""
        with self._lock:
            dirty = list(self._dirty_keys)
            self._dirty_keys.clear()
            snapshots = {
                key: sorted(self._frames.get(key, {}).values(), key=lambda item: item[0])
                for key in dirty
            }
        if not dirty:
            return 0
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.compile(r"^[a-z0-9_.-]+$")
        written = 0
        for key, entries in snapshots.items():
            satellite, product, region = key
            if not safe_name.match(product) or (region is not None and not safe_name.match(region)):
                continue
            payload = {
                "satellite": satellite,
                "product": product,
                "region": region,
                "updated": time.time(),
                "frames": [frame for _, frame in entries],
            }
            path = self.manifest_path(satellite, product, region)
            try:
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(payload))
                tmp.replace(path)
                written += 1
            except Exception as exc:
                logger.warning("Manifest write failed for %s: %s", path.name, exc)
        return written
