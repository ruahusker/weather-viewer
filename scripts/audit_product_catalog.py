#!/usr/bin/env python3
"""Audit WX Viewer product catalogs across pipeline config and frontend UI."""
from __future__ import annotations

import argparse
import ast
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INDEX_HTML = ROOT / "web" / "static" / "index.html"
SCHEDULER = ROOT / "pipeline" / "unified_scheduler.py"
RADAR_CONFIG = ROOT / "pipeline" / "radar" / "config.py"


def _attrs(raw: str) -> dict[str, str]:
    return dict(re.findall(r'([\w:-]+)="([^"]*)"', raw))


def _button_attrs(source: str) -> list[dict[str, str]]:
    return [_attrs(m.group(1)) for m in re.finditer(r"<button\b([^>]*)>", source)]


def _frontend_catalog() -> dict[str, Any]:
    source = INDEX_HTML.read_text()
    model_params: dict[str, set[str]] = {}
    sat_products: set[str] = set()
    radar_products: set[str] = set()

    for attrs in _button_attrs(source):
        if param := attrs.get("data-param"):
            models = {
                item.strip()
                for item in attrs.get("data-models", "").split(",")
                if item.strip()
            }
            model_params.setdefault(param, set()).update(models)
        if product := attrs.get("data-sat-product"):
            sat_products.add(product)
        if product := attrs.get("data-radar-product"):
            radar_products.add(product)

    return {
        "model_params": model_params,
        "sat_products": sat_products,
        "radar_products": radar_products,
    }


def _scheduler_catalog() -> dict[str, Any]:
    tree = ast.parse(SCHEDULER.read_text())
    models: dict[str, set[str]] = {}
    swaths: dict[str, dict[str, str]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ModelConfig":
            name = None
            params: list[str] = []
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = str(kw.value.value)
                elif kw.arg == "rendered_params" and isinstance(kw.value, ast.List):
                    params = [
                        str(item.value)
                        for item in kw.value.elts
                        if isinstance(item, ast.Constant)
                    ]
            if name:
                models[name] = set(params)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_SWATH_PARAMS":
                    swaths = ast.literal_eval(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_SWATH_PARAMS"
        ):
            swaths = ast.literal_eval(node.value)

    generated = {model: set(params) for model, params in models.items()}
    for model, pairs in swaths.items():
        generated.setdefault(model, set()).update(pairs.values())

    return {"models": models, "generated": generated, "swaths": swaths}


def _disk_catalog() -> dict[str, set[str]]:
    rendered = ROOT / "data" / "rendered"
    result: dict[str, set[str]] = {}
    if not rendered.exists():
        return result
    for model_dir in rendered.iterdir():
        if not model_dir.is_dir() or model_dir.name in {"home", "radar", "satellite"}:
            continue
        params: set[str] = set()
        for run_dir in model_dir.iterdir():
            if not run_dir.is_dir():
                continue
            for param_dir in run_dir.iterdir():
                if param_dir.is_dir() and any(param_dir.glob("f*.png")):
                    params.add(param_dir.name)
        if params:
            result[model_dir.name] = params
    return result


def _radar_shorts() -> set[str]:
    tree = ast.parse(RADAR_CONFIG.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "LEVEL2_PRODUCTS" for t in node.targets):
            continue
        products = ast.literal_eval(node.value)
        return {
            str(cfg.get("short", "")).upper()
            for cfg in products.values()
            if cfg.get("short")
        }
    return set()


def _is_internal_model_param(param: str) -> bool:
    return (
        param.startswith("wind_u_")
        or param.startswith("wind_v_")
        or (param.startswith("gh_") and param.endswith("mb"))
        or param == "wind_gust_10m"
    )


def build_report(include_disk: bool = False) -> dict[str, Any]:
    from pipeline.config import COLOR_SCALES, SATELLITE_CONFIG

    frontend = _frontend_catalog()
    scheduler = _scheduler_catalog()

    generated_by_model: dict[str, set[str]] = scheduler["generated"]
    generated_model_params = set().union(*generated_by_model.values()) if generated_by_model else set()
    ui_model_params = set(frontend["model_params"])
    color_params = set(COLOR_SCALES)

    radar_shorts = _radar_shorts()
    satellite_products = set(SATELLITE_CONFIG.get("products", {}))
    composite_products = set(SATELLITE_CONFIG.get("compositing", {}).get("products", []))

    hidden_generated = sorted(
        p for p in generated_model_params - ui_model_params if not _is_internal_model_param(p)
    )
    report: dict[str, Any] = {
        "summary": {
            "frontend_model_params": len(ui_model_params),
            "generated_model_params": len(generated_model_params),
            "frontend_satellite_products": len(frontend["sat_products"]),
            "configured_satellite_products": len(satellite_products),
            "frontend_radar_products": len(frontend["radar_products"]),
            "configured_radar_products": len(radar_shorts),
        },
        "issues": {
            "model_ui_not_generated": sorted(ui_model_params - generated_model_params),
            "model_ui_missing_colorscale": sorted(ui_model_params - color_params),
            "satellite_ui_missing_config": sorted(frontend["sat_products"] - satellite_products),
            "satellite_composite_missing_config": sorted(composite_products - satellite_products),
            "radar_ui_missing_config": sorted(frontend["radar_products"] - radar_shorts),
        },
        "opportunities": {
            "generated_model_params_not_in_ui": hidden_generated,
            "configured_satellite_not_in_ui": sorted(satellite_products - frontend["sat_products"]),
            "configured_radar_not_in_ui": sorted(radar_shorts - frontend["radar_products"]),
        },
        "models": {
            model: sorted(params)
            for model, params in sorted(generated_by_model.items())
        },
    }

    if include_disk:
        disk = _disk_catalog()
        report["disk"] = {
            "models": {model: sorted(params) for model, params in sorted(disk.items())},
            "rendered_not_in_ui": sorted(
                p
                for p in (set().union(*disk.values()) - ui_model_params if disk else set())
                if not _is_internal_model_param(p)
            ),
        }

    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = ["# WX Viewer Product Catalog Audit", ""]
    lines.append("## Summary")
    for key, value in report["summary"].items():
        lines.append(f"- {key.replace('_', ' ')}: {value}")

    lines.append("")
    lines.append("## Blocking Issues")
    issue_count = 0
    for key, values in report["issues"].items():
        issue_count += len(values)
        label = key.replace("_", " ")
        rendered = ", ".join(f"`{html.escape(v)}`" for v in values) if values else "none"
        lines.append(f"- {label}: {rendered}")

    lines.append("")
    lines.append("## Useful Opportunities")
    for key, values in report["opportunities"].items():
        label = key.replace("_", " ")
        rendered = ", ".join(f"`{html.escape(v)}`" for v in values[:40]) if values else "none"
        suffix = f" (+{len(values) - 40} more)" if len(values) > 40 else ""
        lines.append(f"- {label}: {rendered}{suffix}")

    if "disk" in report:
        lines.append("")
        lines.append("## Disk Scan")
        disk_models = report["disk"].get("models", {})
        lines.append(f"- rendered model directories: {len(disk_models)}")
        missing = report["disk"].get("rendered_not_in_ui", [])
        rendered = ", ".join(f"`{html.escape(v)}`" for v in missing[:40]) if missing else "none"
        suffix = f" (+{len(missing) - 40} more)" if len(missing) > 40 else ""
        lines.append(f"- rendered params not in UI: {rendered}{suffix}")

    lines.append("")
    lines.append(f"Issue count: {issue_count}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    parser.add_argument("--disk", action="store_true", help="also scan data/rendered")
    parser.add_argument(
        "--fail-on-issues",
        action="store_true",
        help="exit non-zero when frontend/config mismatches are found",
    )
    args = parser.parse_args()

    report = build_report(include_disk=args.disk)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(markdown_report(report))

    issue_count = sum(len(values) for values in report["issues"].values())
    return 1 if args.fail_on_issues and issue_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
