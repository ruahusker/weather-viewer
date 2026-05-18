#!/usr/bin/env python3
"""
Generate a transparent overlay PNG with state/country boundaries
for use as a CSS overlay on top of model thumbnails.

Domain: lat 21-53, lon -135 to -60  (same as V3 renderer data bounds)
Output: web/static/thumbnail_overlay.png
"""
import json
import numpy as np
from PIL import Image, ImageDraw
from pathlib import Path

# --- Config ---
W, H = 1800, 990   # Match HRRR native res
LAT_MIN, LAT_MAX = 21.0, 53.0
LON_MIN, LON_MAX = -135.0, -60.0

STATES_PATH   = Path(__file__).parent.parent / "web/static/boundaries_states.json"
COUNTRIES_PATH= Path(__file__).parent.parent / "web/static/boundaries_countries.json"
OUT_PATH      = Path(__file__).parent.parent / "web/static/thumbnail_overlay.png"

STATE_COLOR   = (200, 200, 200, 200)   # RGBA — light grey, mostly opaque
COUNTRY_COLOR = (220, 220, 220, 230)   # slightly brighter for international borders


def ll_to_px(lon, lat):
    """Map lon/lat to pixel coords (origin = top-left)."""
    x = (lon - LON_MIN) / (LON_MAX - LON_MIN) * W
    y = (1.0 - (lat - LAT_MIN) / (LAT_MAX - LAT_MIN)) * H
    return x, y


def draw_geojson(draw: ImageDraw.ImageDraw, geojson: dict, color):
    for feature in geojson.get("features", []):
        geom = feature.get("geometry") or {}
        gtype = geom.get("type")
        coords_list = geom.get("coordinates", [])

        if gtype == "LineString":
            rings = [coords_list]
        elif gtype == "MultiLineString":
            rings = coords_list
        elif gtype == "Polygon":
            rings = coords_list          # list of rings; first is outer
        elif gtype == "MultiPolygon":
            rings = [ring for poly in coords_list for ring in poly]
        else:
            continue

        for ring in rings:
            if len(ring) < 2:
                continue
            pts = []
            for pt in ring:
                lon, lat = pt[0], pt[1]
                if lon < LON_MIN - 2 or lon > LON_MAX + 2:
                    continue
                if lat < LAT_MIN - 2 or lat > LAT_MAX + 2:
                    continue
                pts.append(ll_to_px(lon, lat))
            if len(pts) < 2:
                continue
            draw.line(pts, fill=color, width=1)


def main():
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    with open(COUNTRIES_PATH) as f:
        countries = json.load(f)
    with open(STATES_PATH) as f:
        states = json.load(f)

    draw_geojson(draw, countries, COUNTRY_COLOR)
    draw_geojson(draw, states, STATE_COLOR)

    img.save(OUT_PATH, optimize=True)
    print(f"Saved {OUT_PATH}  ({W}x{H})")


if __name__ == "__main__":
    main()
