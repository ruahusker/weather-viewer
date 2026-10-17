"""Compare a saved renderer with the current code using the same cached input.

Run each variant in a fresh process; compare decoded PNGs in the output tree.
This preserves live output and measures CPU time and process peak RSS.
"""

import argparse
import importlib.util
import json
import logging
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("radar", "model", "satellite"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--model", default="hrrr")
    parser.add_argument("--daylight-fixture", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.monotonic()
    if args.kind == "radar":
        from pipeline.radar.parser import parse_volume
        if args.baseline:
            load_module("pipeline.radar.derived", args.baseline / "pipeline/radar/derived.py")
            module = load_module("pipeline.radar.benchmark_renderer", args.baseline / "pipeline/radar/renderer.py")
        else:
            from pipeline.radar import renderer as module
        renderer = module.Level2Renderer(output_dir=args.output)
        volume = parse_volume(args.input, lowest_tilt_only=not bool(args.baseline))
        if volume is None:
            raise RuntimeError("Could not parse benchmark volume")
        site = args.input.name.split("_")[1]
        results = renderer.render_lowest_tilt(volume, site, "benchmark")
        renderer.close()
        products = len(results)
    elif args.kind == "satellite":
        import numpy as np
        import xarray as xr
        from pipeline.satellite import satellite_renderer as current
        module = current
        if args.baseline:
            module = load_module("pipeline.satellite.benchmark_renderer", args.baseline / "pipeline/satellite/satellite_renderer.py")
            module._REGRID_DISK_CACHE_DIR = current._REGRID_DISK_CACHE_DIR
        renderer = module.SatelliteRenderer(output_dir=args.output)
        with xr.open_dataset(args.input) as source:
            names = [name for name in ("CMI_C01", "CMI_C02", "CMI_C03", "CMI_C05", "CMI_C13", "goes_imager_projection") if name in source]
            dataset = source[names].load()
            if args.daylight_fixture:
                # Exercise full-size daytime RGB work on the real satellite
                # geometry even when the live cache contains nighttime scans.
                thermal = dataset["CMI_C13"]
                for band, scale in ((1, .7), (2, .9), (3, .8), (5, .6)):
                    dataset[f"CMI_C{band:02d}"] = xr.DataArray(
                        np.clip((thermal.values - 190) / 120 * scale, 0, 1),
                        dims=thermal.dims, coords=thermal.coords,
                    )
            results = [renderer.render_natural_color(dataset, filename="natural_color.png"),
                       renderer.render_day_cloud_phase(dataset, filename="day_cloud_phase.png"),
                       renderer.render_sandwich(dataset, filename="sandwich.png")]
            products = sum(result is not None for result in results)
    else:
        from pipeline.processor import FieldProcessor
        from pipeline import renderer as current
        module = current
        if args.baseline:
            module = load_module("pipeline.benchmark_renderer", args.baseline / "pipeline/renderer.py")
            module._REGRID_CACHE_DIR = current._REGRID_CACHE_DIR
        parameters = ["temp_2m", "dewpoint_2m", "cape", "reflectivity", "wind_speed", "mslp"]
        fields = FieldProcessor().process_for_render(args.input, parameters)
        args.output.mkdir(parents=True, exist_ok=True)
        products = len(module.OverlayRenderer().render_all_params(fields, args.output, 0, model_name=args.model))
    after = resource.getrusage(resource.RUSAGE_SELF)
    report = {
        "kind": args.kind, "variant": "before" if args.baseline else "after",
        "input": str(args.input), "products": products,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "cpu_seconds": round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 3),
        "peak_rss_mb": round(after.ru_maxrss / 1024, 1),
        "synthetic_daylight": args.daylight_fixture,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "benchmark.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
