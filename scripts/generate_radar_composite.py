#!/usr/bin/env python3
"""Build a cached CONUS radar composite from existing REF latest renders."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


RADAR_META_DIR = Path("/home/main/weather-viewer/data/rendered/radar/meta")
RADAR_PNG_DIR = Path("/home/main/weather-viewer/data/rendered/radar/png")
OUTPUT_DIR = Path("/home/main/weather-viewer/data/rendered/home")
OUTPUT_PNG = OUTPUT_DIR / "radar_composite_latest.png"
OUTPUT_META = OUTPUT_DIR / "radar_composite_latest.json"
OUTPUT_FRAMES = OUTPUT_DIR / "radar_composite_frames.json"

CONUS_BOUNDS = {
    "south": 23.0,
    "west": -129.0,
    "north": 53.0,
    "east": -66.0,
}
OUTPUT_SIZE = (9600, 5600)
MAX_SITE_AGE_MIN = 15
MAX_HISTORY_FRAMES = 12


def parse_radar_timestamp(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def lon_to_x(lon: float, width: int) -> int:
    west = CONUS_BOUNDS["west"]
    east = CONUS_BOUNDS["east"]
    return round((lon - west) / (east - west) * width)


def _merc_y(lat_deg: float) -> float:
    """Web Mercator Y value (proportional to log-tan)."""
    return math.log(math.tan(math.pi / 4 + math.radians(lat_deg) / 2))


def lat_to_y(lat: float, height: int) -> int:
    """Convert latitude to pixel Y using Web Mercator (matches Leaflet's CRS)."""
    y_north = _merc_y(CONUS_BOUNDS["north"])
    y_south = _merc_y(CONUS_BOUNDS["south"])
    return round((y_north - _merc_y(lat)) / (y_north - y_south) * height)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
    path.chmod(0o644)


def atomic_save_image(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".png", dir=path.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        image.save(tmp_path, format="PNG")
        os.replace(tmp_path, path)
        path.chmod(0o644)
    finally:
        tmp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2))


def load_existing_frames() -> list[dict]:
    try:
        data = json.loads(OUTPUT_FRAMES.read_text())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    frames: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("image") and item.get("timestamp"):
            frames.append(item)
    return frames


def prune_history(keep_images: set[str], keep_meta: set[str]) -> None:
    for png_path in OUTPUT_DIR.glob("radar_composite_*.png"):
        if png_path.name == OUTPUT_PNG.name:
            continue
        if png_path.name not in keep_images:
            png_path.unlink(missing_ok=True)
    for meta_path in OUTPUT_DIR.glob("radar_composite_*.json"):
        if meta_path.name in {OUTPUT_META.name, OUTPUT_FRAMES.name}:
            continue
        if meta_path.name not in keep_meta:
            meta_path.unlink(missing_ok=True)


def should_include_site(meta: dict, now: datetime) -> bool:
    bounds = meta.get("bounds") or {}
    png_path = meta.get("png_path") or ""
    if not png_path or not bounds:
        return False
    ts = parse_radar_timestamp(meta.get("timestamp"))
    if ts is None:
        return False
    age_min = (now - ts).total_seconds() / 60.0
    if age_min > MAX_SITE_AGE_MIN:
        return False
    return True


def composite_site(base: Image.Image, meta: dict) -> bool:
    bounds = meta["bounds"]
    png_rel = str(meta["png_path"]).replace("radar/png/", "")
    png_path = RADAR_PNG_DIR / png_rel
    if not png_path.exists():
        return False

    with Image.open(png_path) as src:
        src_rgba = src.convert("RGBA")

    width, height = base.size
    left = lon_to_x(bounds["west"], width)
    right = lon_to_x(bounds["east"], width)
    top = lat_to_y(bounds["north"], height)
    bottom = lat_to_y(bounds["south"], height)

    if right <= left or bottom <= top:
        return False

    resized = src_rgba.resize((right - left, bottom - top), resample=Image.BILINEAR)
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    layer.alpha_composite(resized, (left, top))
    base.alpha_composite(layer)
    return True


def main() -> int:
    now = datetime.now(timezone.utc)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", OUTPUT_SIZE, (0, 0, 0, 0))

    used_sites: list[dict] = []
    skipped = 0
    for meta_file in sorted(RADAR_META_DIR.glob("*_REF_latest.json")):
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
            skipped += 1
            continue
        if not should_include_site(meta, now):
            skipped += 1
            continue
        if composite_site(canvas, meta):
            used_sites.append(
                {
                    "site": meta.get("site"),
                    "timestamp": meta.get("timestamp"),
                    "bounds": meta.get("bounds"),
                }
            )
        else:
            skipped += 1

    if not used_sites:
        print("no fresh radar sites available for composite")
        return 1

    latest_site_ts = max(
        (parse_radar_timestamp(site.get("timestamp")) for site in used_sites),
        default=None,
    ) or now
    timestamp = latest_site_ts.strftime("%Y%m%d_%H%M%S")
    frame_image_name = f"radar_composite_{timestamp}.png"
    frame_meta_name = f"radar_composite_{timestamp}.json"

    atomic_save_image(OUTPUT_PNG, canvas)
    payload = {
        "image": "radar_composite_latest.png",
        "timestamp": timestamp,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "bounds": CONUS_BOUNDS,
        "size": {"width": OUTPUT_SIZE[0], "height": OUTPUT_SIZE[1]},
        "site_count": len(used_sites),
        "max_site_age_min": MAX_SITE_AGE_MIN,
        "sites": used_sites,
        "skipped_count": skipped,
    }
    atomic_write_json(OUTPUT_META, payload)

    frame_payload = dict(payload)
    frame_payload["image"] = frame_image_name
    atomic_save_image(OUTPUT_DIR / frame_image_name, canvas)
    atomic_write_json(OUTPUT_DIR / frame_meta_name, frame_payload)

    frame_entry = {
        "image": frame_image_name,
        "png_path": f"home/{frame_image_name}",
        "timestamp": timestamp,
        "generated_at": payload["generated_at"],
        "bounds": CONUS_BOUNDS,
        "site_count": len(used_sites),
    }
    frame_map = {str(item.get("timestamp")): item for item in load_existing_frames()}
    frame_map[timestamp] = frame_entry
    frames = sorted(frame_map.values(), key=lambda item: str(item.get("timestamp") or ""))[-MAX_HISTORY_FRAMES:]
    atomic_write_json(OUTPUT_FRAMES, frames)

    keep_images = {item["image"] for item in frames}
    keep_meta = {f"{Path(item['image']).stem}.json" for item in frames}
    prune_history(keep_images, keep_meta)

    print(f"generated radar composite: {OUTPUT_PNG} from {len(used_sites)} sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
