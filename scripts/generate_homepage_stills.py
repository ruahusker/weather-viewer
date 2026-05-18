#!/usr/bin/env python3
"""Generate homepage stills with the live site compositor via headless Chromium."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from PIL import Image


BASE_URL = "http://127.0.0.1:8000"
OUTPUT_DIR = Path("/home/main/weather-viewer/data/rendered/home")
WINDOW_SIZE = "1160,720"
VIRTUAL_TIME_BUDGET_MS = "18000"
MAP_BG_OUTPUT = Path("/home/main/weather-viewer/web/static/map_background.png")


def fetch_json(path: str) -> dict:
    with urlopen(f"{BASE_URL}{path}", timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def chromium_binary() -> str:
    for candidate in ("chromium-browser", "chromium"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise RuntimeError("Chromium binary not found")


def trim_bottom_capture_band(image_path: Path) -> None:
    """Remove solid dark footer bands sometimes left by headless capture.

    Leaflet/Chromium occasionally leaves an unrendered strip at the bottom of
    the screenshot. Trim it if it is a deep, nearly uniform band and resize the
    image back to the original dimensions so downstream thumbnail layout stays
    unchanged.
    """
    with Image.open(image_path) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        max_scan_rows = min(160, height // 3)
        trim_rows = 0

        for y in range(height - 1, height - max_scan_rows - 1, -1):
            row = [rgb.getpixel((x, y)) for x in range(0, width, 4)]
            dark_pixels = sum(1 for r, g, b in row if r < 18 and g < 18 and b < 24)
            if row and (dark_pixels / len(row)) >= 0.995:
                trim_rows += 1
                continue
            break

        if trim_rows < 24:
            return

        cropped = rgb.crop((0, 0, width, height - trim_rows))
        restored = cropped.resize((width, height), Image.Resampling.LANCZOS)
        restored.save(image_path)


def render_capture(url: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    browser = chromium_binary()
    with tempfile.TemporaryDirectory(prefix="wx-home-stills-") as user_data_dir:
        cmd = [
            browser,
            "--headless",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={user_data_dir}",
            f"--window-size={WINDOW_SIZE}",
            f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
            f"--screenshot={output_path}",
            url,
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
    trim_bottom_capture_band(output_path)


def write_meta(name: str, payload: dict) -> None:
    meta_path = OUTPUT_DIR / f"{name}.json"
    payload = dict(payload)
    payload["image"] = f"{name}.png"
    payload["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    meta_path.write_text(json.dumps(payload, indent=2))


def build_model_job(item: dict) -> tuple[str, dict] | None:
    if not item:
        return None
    params = {
        "capture": "1",
        "source": "models",
        "model": item.get("model", "hrrr"),
        "run": item.get("run_id", ""),
        "param": item.get("parameter", "temp_2m"),
    }
    url = f"{BASE_URL}/?{urlencode(params)}"
    meta = {
        "kind": "model",
        "title": item.get("label") or "Lead model map",
        "subtitle": f"Run {item.get('run_id', '--')} · {(item.get('parameter') or '').replace('_', ' ')}",
        "timestamp": item.get("run_id"),
        "model": item.get("model"),
        "run_id": item.get("run_id"),
        "parameter": item.get("parameter"),
        "bounds": item.get("bounds"),
    }
    return url, meta


def build_radar_job(item: dict) -> tuple[str, dict] | None:
    if not item:
        return None
    params = {
        "capture": "1",
        "source": "radar",
        "site": item.get("site", "KTLX"),
        "product": "REF",
        "tilt": "0.5",
    }
    url = f"{BASE_URL}/?{urlencode(params)}"
    headline = item.get("headline") or item.get("event") or item.get("name") or item.get("site")
    meta = {
        "kind": "radar",
        "title": headline,
        "subtitle": item.get("name") or item.get("site"),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "site": item.get("site"),
        "product": "REF",
        "tilt": 0.5,
    }
    return url, meta


def build_satellite_job() -> tuple[str, dict] | None:
    latest = fetch_json("/api/satellite/latest/goes19/ir")
    params = {
        "capture": "1",
        "source": "satellite",
        "satellite": "goes19",
        "sat_product": "ir",
    }
    url = f"{BASE_URL}/?{urlencode(params)}"
    meta = {
        "kind": "satellite",
        "title": "GOES-19 Infrared",
        "subtitle": latest.get("timestamp"),
        "timestamp": latest.get("timestamp"),
        "satellite": "goes19",
        "product": "ir",
        "bounds": latest.get("bounds"),
    }
    return url, meta


def render_named_job(name: str, job: tuple[str, dict] | None, failures: list[str], required: bool = True) -> None:
    if not job:
        if required:
            failures.append(f"{name}: no source data")
        else:
            print(f"skipped {name}: no source data")
        return
    url, meta = job
    output_path = OUTPUT_DIR / f"{name}.png"
    try:
        render_capture(url, output_path)
        write_meta(name, meta)
        print(f"generated {name}: {output_path}")
    except Exception as exc:  # pragma: no cover - operational path
        failures.append(f"{name}: {exc}")


def render_map_background() -> None:
    """Render a static CONUS map background (basemap + all overlays, no data layer).

    Only regenerated if the file is missing or older than 24 hours.
    """
    import time
    if MAP_BG_OUTPUT.exists() and MAP_BG_OUTPUT.stat().st_size > 10_000:
        age_seconds = time.time() - MAP_BG_OUTPUT.stat().st_mtime
        if age_seconds < 86400:
            print("map_background.png is recent, skipping regeneration")
            return

    url = f"{BASE_URL}/?capture=1&background_only=1"
    try:
        render_capture(url, MAP_BG_OUTPUT)
        print(f"generated map_background: {MAP_BG_OUTPUT}")
    except Exception as exc:
        print(f"warning: map_background generation failed: {exc}")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    render_map_background()
    home_data = fetch_json("/api/home/overview")
    failures: list[str] = []

    model_items = home_data.get("model_quicklooks") or []
    warned_sites = home_data.get("warned_sites") or []
    radar_sites = warned_sites or (home_data.get("focus_sites") or [])

    render_named_job("model_latest", build_model_job(model_items[0] if model_items else None), failures)
    render_named_job("radar_latest", build_radar_job(radar_sites[0] if radar_sites else None), failures, required=False)
    render_named_job("satellite_latest", build_satellite_job(), failures)

    for idx, item in enumerate(model_items[:3], start=1):
        render_named_job(f"model_quicklook_{idx}", build_model_job(item), failures)

    for idx, item in enumerate(radar_sites[:3], start=1):
        render_named_job(f"radar_quicklook_{idx}", build_radar_job(item), failures)

    if failures:
        print("failures:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
