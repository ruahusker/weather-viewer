"""Refresh existing HRRR wind frames using cached GRIBs, one hour at a time.

No downloads or scheduler metadata changes. Run with scientific-library threads
limited to one and a low CPU priority while the normal weather services run.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.config import GRIB_CACHE_DIR, RENDERED_DIR
from pipeline.processor import DERIVED_PARAMS, FieldProcessor
from pipeline.renderer import OverlayRenderer, _SCALAR_HEIGHT_OVERLAYS

logger = logging.getLogger(__name__)


def refresh(run_id: str, parameters: list[str], limit: int | None = None) -> int:
    """Publish new wind imagery only for already available forecast hours."""
    if not re.fullmatch(r"\d{10}", run_id):
        raise ValueError("Expected a YYYYMMDDHH run")
    if not parameters or any(not p.startswith("wind_") or p not in DERIVED_PARAMS for p in parameters):
        raise ValueError("Expected supported wind parameters")
    run_dir = RENDERED_DIR / "hrrr" / run_id
    bounds = json.loads((run_dir / "bounds.json").read_text())
    requested = list(dict.fromkeys(parameters + [
        _SCALAR_HEIGHT_OVERLAYS[p] for p in parameters if p in _SCALAR_HEIGHT_OVERLAYS
    ]))
    cache = GRIB_CACHE_DIR / "hrrr" / run_id[:8]
    hours = sorted({int(p.stem[1:]) for parameter in parameters
                    for p in (run_dir / parameter).glob("f*.png") if p.stem[1:].isdigit()})
    processor = FieldProcessor()
    renderer = OverlayRenderer()
    renderer._current_model_name = "hrrr"
    count = 0
    for hour in hours[:limit] if limit is not None else hours:
        paths = list(cache.glob(f"*hrrr.t{run_id[-2:]}z.wrfsfcf{hour:02d}.grib2"))
        paths += list(cache.glob(f"*hrrr.t{run_id[-2:]}z.wrfprsf{hour:02d}.grib2"))
        fields = {}
        for path in paths:
            extracted = processor.process_for_render(path, requested, fxx=hour)
            for name, field in extracted.items():
                fields.setdefault(name, field)
            if set(requested) <= set(fields):
                break
        renderer.clear_hour_cache()
        for parameter in parameters:
            u_key, v_key = DERIVED_PARAMS[parameter]
            if not {parameter, u_key, v_key} <= set(fields):
                logger.warning("f%02d: missing fields for %s", hour, parameter)
                continue
            field = fields[parameter]
            actual = renderer.get_data_bounds(field["lat"], field["lon"])
            expected = tuple(bounds[key] for key in ("south", "north", "west", "east"))
            if actual != expected:
                raise ValueError(f"Refusing to change bounds: {actual} != {expected}")
            output = run_dir / parameter / f"f{hour:02d}.png"
            if not output.exists():
                continue
            height = _SCALAR_HEIGHT_OVERLAYS.get(parameter)
            renderer._render_wind_overlay(parameter, field, fields[u_key], fields[v_key],
                                          output, contour_param=height,
                                          contour_dict=fields.get(height))
            count += 1
            logger.info("Refreshed HRRR %s f%02d %s", run_id, hour, parameter)
        del fields
    logger.info("Published %d refreshed wind frames", count)
    return count


def main() -> None:
    """Select a cached run and wind products to refresh."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--params", nargs="+", default=["wind_speed"])
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    refresh(args.run_id, args.params, args.limit)


if __name__ == "__main__":
    main()
