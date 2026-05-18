#!/usr/bin/env python3
"""Bake WX Viewer boundary overlays onto a rendered model PNG."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path("/home/main/weather-viewer")
STATIC_DIR = ROOT / "web" / "static"


def mercator_lat(lat: float) -> float:
    clipped = max(-85.0, min(85.0, float(lat)))
    rad = clipped * 3.141592653589793 / 180.0
    return __import__("math").log(__import__("math").tan(__import__("math").pi / 4 + rad / 2))


def load_geojson(name: str) -> dict:
    return json.loads((STATIC_DIR / name).read_text())


def project_point(coord: list[float], bounds: dict[str, float], width: int, height: int) -> tuple[float, float]:
    west = float(bounds["west"])
    east = float(bounds["east"])
    south = float(bounds["south"])
    north = float(bounds["north"])
    merc_south = mercator_lat(south)
    merc_north = mercator_lat(north)
    lon, lat = float(coord[0]), float(coord[1])
    x = ((lon - west) / (east - west)) * width
    y = ((merc_north - mercator_lat(lat)) / (merc_north - merc_south)) * height
    return x, y


def geometry_parts(geometry: dict) -> list[list[list[float]]]:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "LineString":
        return [coords]
    if gtype == "MultiLineString":
        return list(coords)
    if gtype == "Polygon":
        return list(coords)
    if gtype == "MultiPolygon":
        return [ring for poly in coords for ring in poly]
    return []


def draw_geojson_line(draw: ImageDraw.ImageDraw, geojson: dict, bounds: dict[str, float], size: tuple[int, int],
                      color: tuple[int, int, int, int], width_px: int) -> None:
    img_w, img_h = size
    for feature in geojson.get("features", []):
        for part in geometry_parts(feature.get("geometry", {})):
            if len(part) < 2:
                continue
            pts = [project_point(coord, bounds, img_w, img_h) for coord in part]
            draw.line(pts, fill=color, width=width_px, joint="curve")


def draw_geojson_fill(draw: ImageDraw.ImageDraw, geojson: dict, bounds: dict[str, float], size: tuple[int, int],
                      fill: tuple[int, int, int, int]) -> None:
    img_w, img_h = size
    for feature in geojson.get("features", []):
        geom = feature.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        polys = [coords] if gtype == "Polygon" else coords if gtype == "MultiPolygon" else []
        for poly in polys:
            if not poly:
                continue
            outer = poly[0]
            if len(outer) < 3:
                continue
            pts = [project_point(coord, bounds, img_w, img_h) for coord in outer]
            draw.polygon(pts, fill=fill)


def main() -> None:
    run_id = "2026031216"
    src = ROOT / "data" / "rendered" / "hrrr" / run_id / "temp_2m" / "f00.png"
    bounds = json.loads((ROOT / "data" / "rendered" / "hrrr" / run_id / "bounds.json").read_text())
    out = ROOT / "web" / "static" / "export" / "hrrr_latest_temp_2m_f00_sharpest_frost3.png"

    img = Image.open(src).convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")
    size = img.size

    draw_geojson_line(draw, load_geojson("boundaries_roads.json"), bounds, size, (51, 51, 51, 153), 1)
    draw_geojson_fill(draw, load_geojson("boundaries_lakes.json"), bounds, size, (74, 144, 184, 128))
    draw_geojson_line(draw, load_geojson("boundaries_counties.json"), bounds, size, (68, 68, 68, 178), 1)
    draw_geojson_line(draw, load_geojson("boundaries_states.json"), bounds, size, (51, 51, 51, 217), 1)
    draw_geojson_line(draw, load_geojson("boundaries_countries.json"), bounds, size, (68, 68, 68, 230), 2)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, compress_level=1)
    print(out)


if __name__ == "__main__":
    main()
