"""Radar grid API endpoint for client-side value lookup."""

import struct
import gzip
import numpy as np
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import Response

from .config import RADAR_CACHE_DIR, RADAR_FIELD_CONFIG
from .derived import extract_radar_product
from .parser import parse_volume


def get_radar_grid_binary(site: str, product: str, tilt: float = 0.5) -> Response:
    """Return radar sweep data as compact binary for client-side querying."""
    
    # Resolve product
    product_upper = product.upper()
    field_name = None
    short = product_upper
    for fname, cfg in RADAR_FIELD_CONFIG.items():
        if cfg["short"] == product_upper:
            field_name = fname
            short = cfg["short"]
            break
    if field_name is None:
        field_name = product.lower()
        if field_name not in RADAR_FIELD_CONFIG:
            raise HTTPException(status_code=404, detail=f"Unknown radar product: {product}")
        short = RADAR_FIELD_CONFIG[field_name]["short"]
    
    # Find latest volume
    cache_dir = RADAR_CACHE_DIR / site
    if not cache_dir.exists():
        raise HTTPException(status_code=404, detail=f"No radar cache for site {site}")
    
    raw_files = sorted(cache_dir.glob("*.ar2v"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not raw_files:
        raise HTTPException(status_code=404, detail=f"No radar volume files for {site}")
    
    # Parse volume
    volume = parse_volume(raw_files[0])
    if volume is None:
        raise HTTPException(status_code=500, detail="Failed to parse radar volume")
    
    # Find closest tilt
    if not volume.tilts:
        raise HTTPException(status_code=404, detail="No tilts in volume")

    tilts = np.array(volume.tilts)
    actual_tilt = float(tilts[int(np.argmin(np.abs(tilts - tilt)))])

    prefer_lr = RADAR_FIELD_CONFIG[field_name].get("prefer_long_range", False)
    sweep = extract_radar_product(
        volume,
        field_name,
        target_tilt=actual_tilt,
        prefer_long_range=prefer_lr,
    )
    if sweep is None:
        raise HTTPException(status_code=404, detail=f"No {field_name} data at tilt {tilt}")

    n_gates = sweep.n_gates
    if n_gates <= 0:
        raise HTTPException(status_code=404, detail=f"Invalid {field_name} gate count for {site}")
    gate_spacing_km = float(sweep.max_range_km) / float(max(n_gates, 1))
    max_range_km = float(sweep.max_range_km)
    
    cfg = RADAR_FIELD_CONFIG[field_name]
    _, vmin, vmax = cfg["lut_fn"]()
    
    data = np.asarray(sweep.data.data if hasattr(sweep.data, 'data') else sweep.data, dtype=np.float32)
    if cfg.get("unit_scale"):
        data = data * float(cfg["unit_scale"])
    azimuths_arr = np.asarray(sweep.azimuth, dtype=np.float32)
    
    n_az = len(azimuths_arr)
    
    # Build header: 28 bytes
    header = struct.pack(
        '<ffHHffff',
        float(volume.site_lat),
        float(volume.site_lon),
        n_az,
        n_gates,
        gate_spacing_km,
        max_range_km,
        vmin,
        vmax
    )
    
    full_payload = header + azimuths_arr.tobytes() + data.tobytes()
    compressed = gzip.compress(full_payload, compresslevel=4)
    
    return Response(
        content=compressed,
        media_type="application/octet-stream",
        headers={
            "Content-Encoding": "gzip",
            "X-Radar-Site": site,
            "X-Radar-Product": short,
            "X-Radar-Tilt": str(actual_tilt),
            "X-Grid-Shape": f"{n_az},{n_gates}",
            "Cache-Control": "public, max-age=60"
        }
    )
