#!/usr/bin/env python3
"""Audit rendered satellite files against the configured loop window."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/home/main/weather-viewer")
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from pipeline.config import SATELLITE_CONFIG


PNG_DIR = BASE_DIR / "data" / "rendered" / "satellite" / "png"
META_DIR = BASE_DIR / "data" / "rendered" / "satellite" / "meta"
FILENAME_TS_RE = re.compile(r"_(\d{7})_(\d{4})(?:_|\.|$)")
PRODUCT_RE = re.compile(r"^(goes\d+)_(.+)_(\d{7})_(\d{4})(?:_(.+))?$")


def parse_timestamp_from_name(name: str) -> datetime | None:
    match = FILENAME_TS_RE.search(name)
    if not match:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%Y%j%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_product_key(stem: str) -> str:
    match = PRODUCT_RE.match(stem)
    if not match:
        return "unparsed"
    source_id, product_type, _, _, region = match.groups()
    if region:
        return f"{source_id}/{product_type}/{region}"
    return f"{source_id}/{product_type}/full"


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def main() -> int:
    loop_hours = int(SATELLITE_CONFIG.get("render", {}).get("loop_hours", 12))
    cutoff = datetime.now(timezone.utc).timestamp() - (loop_hours * 3600)

    png_files = [p for p in PNG_DIR.glob("*.png") if not p.stem.endswith("_latest")]
    meta_files = [p for p in META_DIR.glob("*.json") if not p.stem.endswith("_latest")]
    meta_names = {p.stem for p in meta_files}
    png_names = {p.stem for p in png_files}

    png_in_window = 0
    png_outside = 0
    png_unknown = 0
    png_outside_bytes = 0
    product_counts: Counter[str] = Counter()
    old_product_counts: Counter[str] = Counter()
    latest_ts: defaultdict[str, datetime | None] = defaultdict(lambda: None)
    unknown_png_examples: list[str] = []

    for png in png_files:
        product_key = parse_product_key(png.stem)
        product_counts[product_key] += 1
        ts = parse_timestamp_from_name(png.name)
        if ts is None:
            png_unknown += 1
            if len(unknown_png_examples) < 8:
                unknown_png_examples.append(png.name)
            continue
        previous = latest_ts[product_key]
        if previous is None or ts > previous:
            latest_ts[product_key] = ts
        if ts.timestamp() >= cutoff:
            png_in_window += 1
        else:
            png_outside += 1
            old_product_counts[product_key] += 1
            try:
                png_outside_bytes += png.stat().st_size
            except OSError:
                pass

    meta_in_window = 0
    meta_outside = 0
    meta_unknown = 0
    unknown_meta_examples: list[str] = []
    for meta in meta_files:
        ts = parse_timestamp_from_name(meta.name)
        if ts is None:
            try:
                payload = json.loads(meta.read_text())
            except Exception:
                meta_unknown += 1
                if len(unknown_meta_examples) < 8:
                    unknown_meta_examples.append(meta.name)
                continue
            ts_text = str(payload.get("timestamp", ""))
            for fmt in ("%Y-%jT%H:%M:%SZ", "%Y-%jT%H:%MZ"):
                try:
                    ts = datetime.strptime(ts_text, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
        if ts is None:
            meta_unknown += 1
            if len(unknown_meta_examples) < 8:
                unknown_meta_examples.append(meta.name)
        elif ts.timestamp() >= cutoff:
            meta_in_window += 1
        else:
            meta_outside += 1

    orphan_png = png_names - meta_names
    orphan_meta = meta_names - png_names

    print(f"Configured loop window: {loop_hours} hours")
    print(f"Rendered PNGs: {len(png_files)} total, {png_in_window} within window, {png_outside} older, {png_unknown} unknown-ts")
    print(f"Rendered meta: {len(meta_files)} total, {meta_in_window} within window, {meta_outside} older, {meta_unknown} unknown-ts")
    print(f"Orphan PNGs without meta: {len(orphan_png)}")
    print(f"Orphan meta without PNG: {len(orphan_meta)}")
    print(f"Space tied to PNGs older than window: {format_bytes(png_outside_bytes)}")

    print("\nTop rendered product buckets:")
    for key, count in product_counts.most_common(12):
        latest = latest_ts.get(key)
        latest_text = latest.isoformat() if latest else "unknown"
        print(f"  {key}: {count} pngs, latest={latest_text}")

    if old_product_counts:
        print("\nBuckets with files older than the active window:")
        for key, count in old_product_counts.most_common(12):
            print(f"  {key}: {count} old pngs")
    else:
        print("\nBuckets with files older than the active window: none")

    if unknown_png_examples:
        print("\nSample PNGs without parseable frame timestamps:")
        for name in unknown_png_examples:
            print(f"  {name}")

    if unknown_meta_examples:
        print("\nSample meta files without parseable frame timestamps:")
        for name in unknown_meta_examples:
            print(f"  {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
