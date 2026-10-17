"""Georeferenced reflectivity volumes for the live Omaha/Hastings 3D map.

Grid order is (height, south, east), with voxel centres in a local Mercator
box. Sample real gate ranges and elevation angles; leave unsampled space empty.
Heights are above the radar antenna, with 4/3-Earth beam propagation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path

import numpy as np

VERSION = "v1"
SITES = ("KOAX", "KUEX")
RADIUS_M = 240_000
NX = NY = 240
NZ = 40
HEIGHT_M = 20_000
EARTH_M = 6_371_000.0
MERCATOR_M = 6_378_137.0
FEET_PER_M = 1 / 0.3048


def grid_geometry(lat: float, lon: float) -> tuple[np.ndarray, np.ndarray]:
    """Return ground distance and bearing at georeferenced voxel centres."""
    lat0, lon0 = np.radians([lat, lon])
    scale = 1 / np.cos(lat0)
    offsets = (np.arange(NX, dtype=np.float64) + .5) / NX * (2 * RADIUS_M) - RADIUS_M
    gx, gy = np.meshgrid(offsets * scale, offsets * scale)
    target_lon = lon0 + gx / MERCATOR_M
    northing = MERCATOR_M * np.log(np.tan(np.pi / 4 + lat0 / 2)) - gy
    target_lat = 2 * np.arctan(np.exp(northing / MERCATOR_M)) - np.pi / 2
    dlon, dlat = target_lon - lon0, target_lat - lat0
    a = np.sin(dlat / 2) ** 2 + np.cos(lat0) * np.cos(target_lat) * np.sin(dlon / 2) ** 2
    distance = 2 * EARTH_M * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    bearing = np.degrees(np.arctan2(np.sin(dlon) * np.cos(target_lat),
                                  np.cos(lat0) * np.sin(target_lat)
                                  - np.sin(lat0) * np.cos(target_lat) * np.cos(dlon))) % 360
    return distance, bearing


def sample_volume(radar) -> tuple[np.ndarray, dict]:
    """Map reflectivity into a bounded grid, preserving missing observations."""
    lat, lon = float(radar.latitude['data'][0]), float(radar.longitude['data'][0])
    distance, bearing = grid_geometry(lat, lon)
    re = EARTH_M * 4 / 3
    height = (np.arange(NZ, dtype=np.float64) + .5) * HEIGHT_M / NZ
    gamma = distance / re
    horizontal = (re + height[:, None, None]) * np.sin(gamma)
    vertical = (re + height[:, None, None]) * np.cos(gamma) - re
    slant = np.hypot(horizontal, vertical)
    elevation = np.degrees(np.arctan2(vertical, horizontal))
    field = radar.fields.get('reflectivity')
    if field is None:
        raise ValueError('This scan has no reflectivity observations')
    values = np.ma.asarray(field['data']).filled(np.nan)
    angles = np.asarray(radar.fixed_angle['data'], dtype=float)
    starts, ends = radar.sweep_start_ray_index['data'], radar.sweep_end_ray_index['data']
    # Split cuts can repeat the same tilt; choose the sweep with actual REF data.
    selected = {}
    for i, angle in enumerate(angles):
        if not np.isfinite(angle):
            continue
        count = np.count_nonzero(np.isfinite(values[starts[i]:ends[i] + 1]))
        if count and count > selected.get(round(float(angle), 1), (-1, 0))[1]:
            selected[round(float(angle), 1)] = (i, count)
    if not selected:
        raise ValueError('This scan has no usable reflectivity sweeps')
    tilts = np.array(sorted(selected))
    upper = np.searchsorted(tilts, elevation).clip(0, len(tilts) - 1)
    lower = np.maximum(0, upper - 1)
    nearest = np.where(abs(elevation - tilts[lower]) <= abs(elevation - tilts[upper]), lower, upper)
    ranges = np.asarray(radar.range['data'], dtype=float)
    if len(ranges) < 2 or not np.all(np.diff(ranges) > 0):
        raise ValueError('Invalid radar gate range coordinates')
    gate_upper = np.searchsorted(ranges, slant).clip(0, len(ranges) - 1)
    gate_lower = np.maximum(0, gate_upper - 1)
    gates = np.where(abs(slant - ranges[gate_lower]) <= abs(slant - ranges[gate_upper]), gate_lower, gate_upper)
    valid_range = (slant >= ranges[0] - (ranges[1] - ranges[0]) / 2) & (slant <= ranges[-1])
    # A one-degree beam is sampled only within 0.65 degrees of its centre.
    # Do not fill the cone of silence or bridge wide elevation gaps.
    observed = valid_range & (distance[None] <= RADIUS_M) & (abs(elevation - tilts[nearest]) <= .65)
    grid = np.zeros((NZ, NY, NX), dtype=np.uint8)
    for index, angle in enumerate(tilts):
        sweep, _ = selected[float(angle)]
        start, end = int(starts[sweep]), int(ends[sweep]) + 1
        az = np.asarray(radar.azimuth['data'][start:end], dtype=float) % 360
        order = np.argsort(az)
        sorted_az = az[order]
        if not len(sorted_az):
            continue
        insertion = np.searchsorted(sorted_az, bearing)
        left, right = (insertion - 1) % len(az), insertion % len(az)
        delta_left = abs((bearing - sorted_az[left] + 180) % 360 - 180)
        delta_right = abs((bearing - sorted_az[right] + 180) % 360 - 180)
        az_index = order[np.where(delta_left <= delta_right, left, right)]
        mask = observed & (nearest == index) & (np.minimum(delta_left, delta_right)[None] <= 1.2)
        z, y, x = np.nonzero(mask)
        dbz = values[start + az_index[y, x], gates[z, y, x]]
        valid = np.isfinite(dbz) & (dbz >= 0) & (dbz <= 95)
        grid[z[valid], y[valid], x[valid]] = np.rint((dbz[valid] + 32) * 2).astype(np.uint8)
    occupied = grid > 0
    top_indices = np.nonzero(occupied)[0]
    metadata = {
        'site_lat': lat, 'site_lon': lon,
        'base_altitude_m': float(radar.altitude['data'][0]),
        'dimensions': [NX, NY, NZ], 'radius_m': RADIUS_M, 'height_m': HEIGHT_M,
        'height_reference': 'above radar antenna',
        'encoding': {'scale': .5, 'offset': -32, 'missing': 0},
        'tilts': tilts.tolist(), 'occupied_voxels': int(occupied.sum()),
        'max_dbz': float(grid.max() * .5 - 32) if occupied.any() else None,
        'echo_top_ft': round(float(height[top_indices.max()]) * FEET_PER_M) if len(top_indices) else None,
        'horizontal_spacing_m': 2 * RADIUS_M / NX, 'vertical_spacing_ft': HEIGHT_M / NZ * FEET_PER_M,
    }
    return grid, metadata


def build(raw_path: Path, output: Path, site: str, key: str) -> dict:
    """Decode once in an expendable process, then publish binary and manifest."""
    import pyart
    radar = pyart.io.read_nexrad_archive(str(raw_path), include_fields=['reflectivity'], station=site)
    grid, metadata = sample_volume(radar)
    try:
        stamp = pyart.util.datetime_from_radar(radar)
        scan_time = datetime(stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute,
                             stamp.second, tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError):
        from .parser import _parse_chunk_timestamp
        dt = _parse_chunk_timestamp(raw_path)
        if dt is None:
            raise ValueError('Scan has no valid observation time')
        scan_time = dt.isoformat()
    output.mkdir(parents=True, exist_ok=True)
    from .color_tables import get_radar_lut
    palette, palette_min, palette_max = get_radar_lut('reflectivity')
    metadata.update(version=VERSION, site=site, key=key, scan=raw_path.name,
                    scan_time=scan_time, built_at=datetime.now(timezone.utc).isoformat(),
                    byte_length=grid.nbytes, palette=palette.tolist(),
                    palette_min=palette_min, palette_max=palette_max)
    binary = gzip.compress(grid.tobytes(order='C'), compresslevel=1, mtime=0)
    metadata['compressed_bytes'] = len(binary)
    for name, content in [(f'{key}.bin.gz', binary), (f'{key}.json', json.dumps(metadata).encode())]:
        temp = output / f'.{name}.{os.getpid()}.tmp'
        temp.write_bytes(content)
        temp.replace(output / name)
    return metadata


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('raw', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('site', choices=SITES)
    parser.add_argument('key')
    args = parser.parse_args()
    build(args.raw, args.output, args.site, args.key)
