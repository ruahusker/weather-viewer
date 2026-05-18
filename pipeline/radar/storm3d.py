"""On-demand 3D storm extraction from Level II volumes."""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .parser import extract_all_sweeps, parse_volume

EARTH_RADIUS_KM = 6371.0
GRID_SPACING_KM = 1.0
GRID_RADIUS_KM = 48.0
MAX_HEIGHT_KM = 18.0
MIN_RENDER_DBZ = 20.0
MIN_COMPONENT_DBZ = 35.0
SELECTION_RADIUS_KM = 22.0
MAX_RETURN_VOXELS = 18000
TEXTURE_XY_SIZE = 96
TEXTURE_Z_SIZE = 64

# Enhanced multi-channel mode constants
ENH_GRID_SPACING_KM = 0.5
ENH_GRID_RADIUS_KM = 25.0       # 50km x 50km bounding box
ENH_MAX_HEIGHT_KM = 20.0
ENH_TEXTURE_XY_SIZE = 100
ENH_TEXTURE_Z_SIZE = 64
ENH_MIN_RENDER_DBZ = 5.0        # Lower floor to capture cloud shell
ENH_VEL_RANGE = (-50.0, 50.0)   # m/s
ENH_ZDR_RANGE = (-2.0, 6.0)     # dB
ENH_CC_MET_THRESHOLD = 0.85


@dataclass
class StormVolumeResult:
    """Compact storm volume ready for JSON serialization."""

    payload: dict


def _latlon_to_xy_km(site_lat: float, site_lon: float, lat: float, lon: float) -> tuple[float, float]:
    lat0 = np.radians(site_lat)
    dlat = np.radians(lat - site_lat)
    dlon = np.radians(lon - site_lon)
    y_km = EARTH_RADIUS_KM * dlat
    x_km = EARTH_RADIUS_KM * np.cos(lat0) * dlon
    return float(x_km), float(y_km)


def _xy_to_latlon(site_lat: float, site_lon: float, x_km: float, y_km: float) -> tuple[float, float]:
    lat = site_lat + np.degrees(y_km / EARTH_RADIUS_KM)
    lon = site_lon + np.degrees(x_km / (EARTH_RADIUS_KM * np.cos(np.radians(site_lat))))
    return float(lat), float(lon)


def _beam_height_km(range_km: np.ndarray, elevation_deg: float) -> np.ndarray:
    elev = np.radians(elevation_deg)
    return range_km * np.sin(elev) + (range_km ** 2) / (2.0 * EARTH_RADIUS_KM)


def _grid_shape() -> tuple[int, int, int]:
    xy_bins = int(round((GRID_RADIUS_KM * 2.0) / GRID_SPACING_KM))
    z_bins = int(round(MAX_HEIGHT_KM / GRID_SPACING_KM))
    return z_bins, xy_bins, xy_bins


def _accumulate_reflectivity_grid(volume, click_x_km: float, click_y_km: float) -> np.ndarray:
    grid = np.full(_grid_shape(), -np.inf, dtype=np.float32)
    sweeps = extract_all_sweeps(volume, "reflectivity")
    if not sweeps:
        return grid

    x_min = click_x_km - GRID_RADIUS_KM
    y_min = click_y_km - GRID_RADIUS_KM
    nx = grid.shape[2]
    ny = grid.shape[1]
    nz = grid.shape[0]

    for sweep in sweeps:
        if sweep.n_gates <= 0:
            continue
        data = sweep.data.filled(np.nan) if hasattr(sweep.data, "filled") else np.asarray(sweep.data, dtype=np.float32)
        if data.ndim != 2:
            continue

        gate_step_km = float(sweep.max_range_km) / float(sweep.n_gates)
        ranges = (np.arange(sweep.n_gates, dtype=np.float32) + 0.5) * gate_step_km
        az_rad = np.radians(np.asarray(sweep.azimuth, dtype=np.float32))
        cos_el = np.cos(np.radians(sweep.tilt_angle))
        ground_range = ranges * cos_el
        z_vals = _beam_height_km(ranges, sweep.tilt_angle).astype(np.float32)

        sin_az = np.sin(az_rad)[:, None]
        cos_az = np.cos(az_rad)[:, None]
        x = sin_az * ground_range[None, :]
        y = cos_az * ground_range[None, :]

        valid = np.isfinite(data) & (data >= MIN_RENDER_DBZ)
        if not np.any(valid):
            continue

        x = x[valid]
        y = y[valid]
        z = np.broadcast_to(z_vals[None, :], data.shape)[valid]
        refl = data[valid]

        in_window = (
            (x >= x_min) & (x < x_min + 2.0 * GRID_RADIUS_KM) &
            (y >= y_min) & (y < y_min + 2.0 * GRID_RADIUS_KM) &
            (z >= 0.0) & (z < MAX_HEIGHT_KM)
        )
        if not np.any(in_window):
            continue

        x = x[in_window]
        y = y[in_window]
        z = z[in_window]
        refl = refl[in_window]

        xi = np.floor((x - x_min) / GRID_SPACING_KM).astype(np.int32)
        yi = np.floor((y - y_min) / GRID_SPACING_KM).astype(np.int32)
        zi = np.floor(z / GRID_SPACING_KM).astype(np.int32)

        keep = (
            (xi >= 0) & (xi < nx) &
            (yi >= 0) & (yi < ny) &
            (zi >= 0) & (zi < nz)
        )
        if not np.any(keep):
            continue

        flat_idx = zi[keep] * (ny * nx) + yi[keep] * nx + xi[keep]
        np.maximum.at(grid.ravel(), flat_idx, refl[keep].astype(np.float32))

    return grid


def _estimate_peak_xy(volume) -> tuple[float, float]:
    sweeps = extract_all_sweeps(volume, "reflectivity")
    best_dbz = -np.inf
    best_xy = (0.0, 0.0)

    for sweep in sweeps:
        data = sweep.data.filled(np.nan) if hasattr(sweep.data, "filled") else np.asarray(sweep.data, dtype=np.float32)
        if data.ndim != 2 or not np.isfinite(data).any():
            continue

        flat_idx = int(np.nanargmax(data))
        dbz = float(np.ravel(data)[flat_idx])
        if not np.isfinite(dbz) or dbz < best_dbz:
            continue

        az_idx, gate_idx = np.unravel_index(flat_idx, data.shape)
        gate_step_km = float(sweep.max_range_km) / float(sweep.n_gates)
        range_km = (gate_idx + 0.5) * gate_step_km
        ground_range = range_km * np.cos(np.radians(sweep.tilt_angle))
        az_rad = np.radians(float(sweep.azimuth[az_idx]))
        best_dbz = dbz
        best_xy = (
            float(np.sin(az_rad) * ground_range),
            float(np.cos(az_rad) * ground_range),
        )

    return best_xy


def _component_mask(grid: np.ndarray, threshold_dbz: float) -> np.ndarray:
    return np.isfinite(grid) & (grid >= threshold_dbz)


def _component_neighbors(z: int, y: int, x: int, shape: tuple[int, int, int]):
    max_z, max_y, max_x = shape
    for dz, dy, dx in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        nz, ny, nx = z + dz, y + dy, x + dx
        if 0 <= nz < max_z and 0 <= ny < max_y and 0 <= nx < max_x:
            yield nz, ny, nx


def _find_selected_component(grid: np.ndarray, threshold_dbz: float,
                              grid_radius_km: float = GRID_RADIUS_KM,
                              grid_spacing_km: float = GRID_SPACING_KM,
                              selection_radius_km: float = SELECTION_RADIUS_KM) -> Optional[np.ndarray]:
    mask = _component_mask(grid, threshold_dbz)
    if not np.any(mask):
        return None

    visited = np.zeros(mask.shape, dtype=bool)
    center_xy = grid_radius_km / grid_spacing_km
    selection_radius2 = (selection_radius_km / grid_spacing_km) ** 2

    best_component = None
    best_score = None

    occupied = np.argwhere(mask)
    for z0, y0, x0 in occupied:
        if visited[z0, y0, x0]:
            continue

        queue = deque([(int(z0), int(y0), int(x0))])
        visited[z0, y0, x0] = True
        voxels: list[tuple[int, int, int]] = []
        max_dbz = -np.inf
        min_dist2 = np.inf

        while queue:
            z, y, x = queue.popleft()
            voxels.append((z, y, x))
            dbz = float(grid[z, y, x])
            if dbz > max_dbz:
                max_dbz = dbz

            dx = x + 0.5 - center_xy
            dy = y + 0.5 - center_xy
            dist2 = dx * dx + dy * dy
            if dist2 < min_dist2:
                min_dist2 = dist2

            for nz, ny, nx in _component_neighbors(z, y, x, mask.shape):
                if mask[nz, ny, nx] and not visited[nz, ny, nx]:
                    visited[nz, ny, nx] = True
                    queue.append((nz, ny, nx))

        qualifies = min_dist2 <= selection_radius2
        score = (
            0 if qualifies else 1,
            round(min_dist2, 3),
            -round(max_dbz, 3),
            -len(voxels),
        )
        if best_score is None or score < best_score:
            best_score = score
            best_component = np.array(voxels, dtype=np.int32)

    return best_component


def _find_box_component(
    grid: np.ndarray,
    threshold_dbz: float,
    box_x_km: tuple[float, float],
    box_y_km: tuple[float, float],
    click_x_km: float,
    click_y_km: float,
    grid_radius_km: float,
    grid_spacing_km: float,
) -> Optional[np.ndarray]:
    mask = _component_mask(grid, threshold_dbz)
    if not np.any(mask):
        return None

    x_min = click_x_km - grid_radius_km
    y_min = click_y_km - grid_radius_km
    bx0 = max(0, int(np.floor((min(box_x_km) - x_min) / grid_spacing_km)))
    bx1 = min(grid.shape[2], int(np.ceil((max(box_x_km) - x_min) / grid_spacing_km)))
    by0 = max(0, int(np.floor((min(box_y_km) - y_min) / grid_spacing_km)))
    by1 = min(grid.shape[1], int(np.ceil((max(box_y_km) - y_min) / grid_spacing_km)))
    box_center_x = (box_x_km[0] + box_x_km[1]) * 0.5
    box_center_y = (box_y_km[0] + box_y_km[1]) * 0.5

    visited = np.zeros(mask.shape, dtype=bool)
    best_component = None
    best_score = None

    occupied = np.argwhere(mask)
    for z0, y0, x0 in occupied:
        if visited[z0, y0, x0]:
            continue

        queue = deque([(int(z0), int(y0), int(x0))])
        visited[z0, y0, x0] = True
        voxels: list[tuple[int, int, int]] = []
        max_dbz = -np.inf
        overlap = 0
        min_dist2 = np.inf

        while queue:
            z, y, x = queue.popleft()
            voxels.append((z, y, x))
            dbz = float(grid[z, y, x])
            max_dbz = max(max_dbz, dbz)

            if by0 <= y < by1 and bx0 <= x < bx1:
                overlap += 1

            vx = x_min + (x + 0.5) * grid_spacing_km
            vy = y_min + (y + 0.5) * grid_spacing_km
            dist2 = (vx - box_center_x) ** 2 + (vy - box_center_y) ** 2
            min_dist2 = min(min_dist2, dist2)

            for nz, ny, nx in _component_neighbors(z, y, x, mask.shape):
                if mask[nz, ny, nx] and not visited[nz, ny, nx]:
                    visited[nz, ny, nx] = True
                    queue.append((nz, ny, nx))

        score = (
            0 if overlap > 0 else 1,
            -overlap,
            round(min_dist2, 3),
            -round(max_dbz, 3),
            -len(voxels),
        )
        if best_score is None or score < best_score:
            best_score = score
            best_component = np.array(voxels, dtype=np.int32)

    return best_component


def _find_peak_component(grid: np.ndarray, threshold_dbz: float) -> Optional[np.ndarray]:
    mask = _component_mask(grid, threshold_dbz)
    if not np.any(mask):
        return None

    visited = np.zeros(mask.shape, dtype=bool)
    best_component = None
    best_score = None

    occupied = np.argwhere(mask)
    for z0, y0, x0 in occupied:
        if visited[z0, y0, x0]:
            continue

        queue = deque([(int(z0), int(y0), int(x0))])
        visited[z0, y0, x0] = True
        voxels: list[tuple[int, int, int]] = []
        max_dbz = -np.inf
        dbz_sum = 0.0

        while queue:
            z, y, x = queue.popleft()
            voxels.append((z, y, x))
            dbz = float(grid[z, y, x])
            max_dbz = max(max_dbz, dbz)
            dbz_sum += dbz

            for nz, ny, nx in _component_neighbors(z, y, x, mask.shape):
                if mask[nz, ny, nx] and not visited[nz, ny, nx]:
                    visited[nz, ny, nx] = True
                    queue.append((nz, ny, nx))

        score = (
            -round(max_dbz, 3),
            -round(dbz_sum, 3),
            -len(voxels),
        )
        if best_score is None or score < best_score:
            best_score = score
            best_component = np.array(voxels, dtype=np.int32)

    return best_component


def _component_payload(
    component: np.ndarray,
    grid: np.ndarray,
    site_lat: float,
    site_lon: float,
    click_lat: float,
    click_lon: float,
    click_x_km: float,
    click_y_km: float,
) -> dict:
    x_min = click_x_km - GRID_RADIUS_KM
    y_min = click_y_km - GRID_RADIUS_KM

    z_idx = component[:, 0]
    y_idx = component[:, 1]
    x_idx = component[:, 2]
    dbz = grid[z_idx, y_idx, x_idx]

    x_km = x_min + (x_idx.astype(np.float32) + 0.5) * GRID_SPACING_KM
    y_km = y_min + (y_idx.astype(np.float32) + 0.5) * GRID_SPACING_KM
    z_km = (z_idx.astype(np.float32) + 0.5) * GRID_SPACING_KM

    weights = np.clip(dbz, MIN_COMPONENT_DBZ, None)
    weight_sum = float(np.sum(weights)) or 1.0
    centroid_x = float(np.sum(x_km * weights) / weight_sum)
    centroid_y = float(np.sum(y_km * weights) / weight_sum)
    centroid_z = float(np.sum(z_km * weights) / weight_sum)
    centroid_lat, centroid_lon = _xy_to_latlon(site_lat, site_lon, centroid_x, centroid_y)
    click_distance_km = float(np.hypot(centroid_x - click_x_km, centroid_y - click_y_km))

    order = np.argsort(dbz)[::-1]
    if order.size > MAX_RETURN_VOXELS:
        order = order[:MAX_RETURN_VOXELS]

    voxels = [
        [round(float(x_km[i] - centroid_x), 2),
         round(float(y_km[i] - centroid_y), 2),
         round(float(z_km[i]), 2),
         round(float(dbz[i]), 1)]
        for i in order
    ]

    return {
        "selection": {
            "click_lat": round(click_lat, 4),
            "click_lon": round(click_lon, 4),
            "centroid_lat": round(centroid_lat, 4),
            "centroid_lon": round(centroid_lon, 4),
            "distance_from_click_km": round(click_distance_km, 1),
        },
        "summary": {
            "voxel_count": int(component.shape[0]),
            "displayed_voxel_count": len(voxels),
            "max_dbz": round(float(np.nanmax(dbz)), 1),
            "top_km": round(float(np.nanmax(z_km)), 1),
            "base_km": round(float(np.nanmin(z_km)), 1),
            "centroid_height_km": round(centroid_z, 1),
        },
        "grid": {
            "spacing_km": GRID_SPACING_KM,
            "radius_km": GRID_RADIUS_KM,
        },
        "voxels": voxels,
        "isosurfaces": [
            {"threshold_dbz": 40.0, "color": "#33ff99", "opacity": 0.18},
            {"threshold_dbz": 50.0, "color": "#ffd166", "opacity": 0.28},
            {"threshold_dbz": 60.0, "color": "#ff5e5b", "opacity": 0.42},
        ],
    }


def _volume_texture_payload(component: np.ndarray, grid: np.ndarray) -> dict:
    z_idx = component[:, 0]
    y_idx = component[:, 1]
    x_idx = component[:, 2]
    dbz = grid[z_idx, y_idx, x_idx]

    weights = np.clip(dbz, MIN_COMPONENT_DBZ, None)
    weight_sum = float(np.sum(weights)) or 1.0
    center_x = int(round(float(np.sum(x_idx * weights) / weight_sum)))
    center_y = int(round(float(np.sum(y_idx * weights) / weight_sum)))

    half_xy = TEXTURE_XY_SIZE // 2
    x0 = max(0, min(grid.shape[2] - TEXTURE_XY_SIZE, center_x - half_xy))
    y0 = max(0, min(grid.shape[1] - TEXTURE_XY_SIZE, center_y - half_xy))
    x1 = min(grid.shape[2], x0 + TEXTURE_XY_SIZE)
    y1 = min(grid.shape[1], y0 + TEXTURE_XY_SIZE)
    z0 = 0
    z1 = grid.shape[0]

    subgrid = grid[z0:z1, y0:y1, x0:x1].copy()

    # Keep the broader storm envelope instead of only the tight core. This
    # gives the raymarch enough low/mid-dBZ body to look like a storm volume.
    subgrid[subgrid < 10.0] = -np.inf
    subgrid[~np.isfinite(subgrid)] = 0.0

    norm = np.clip((subgrid - MIN_RENDER_DBZ) / (75.0 - MIN_RENDER_DBZ), 0.0, 1.0).astype(np.float32)

    # Smooth lightly, but blend most of the original field back in so the
    # browser raymarch keeps sharper storm edges and core definition.
    padded = np.pad(norm, 1, mode="edge")
    smoothed = (
        padded[1:-1, 1:-1, 1:-1] * 4.4 +
        padded[:-2, 1:-1, 1:-1] * 0.55 +
        padded[2:, 1:-1, 1:-1] * 0.55 +
        padded[1:-1, :-2, 1:-1] * 0.7 +
        padded[1:-1, 2:, 1:-1] * 0.7 +
        padded[1:-1, 1:-1, :-2] * 0.8 +
        padded[1:-1, 1:-1, 2:] * 0.8
    ) / 8.5
    norm = np.clip(norm * 0.72 + smoothed * 0.28, 0.0, 1.0)

    # Upsample vertically so the browser raymarch has smoother echo tops and less banding.
    if norm.shape[0] != TEXTURE_Z_SIZE:
        old_z = np.arange(norm.shape[0], dtype=np.float32)
        new_z = np.linspace(0, max(norm.shape[0] - 1, 1), TEXTURE_Z_SIZE, dtype=np.float32)
        flat = norm.reshape(norm.shape[0], -1)
        interp = np.empty((TEXTURE_Z_SIZE, flat.shape[1]), dtype=np.float32)
        for idx in range(flat.shape[1]):
            interp[:, idx] = np.interp(new_z, old_z, flat[:, idx])
        norm = interp.reshape(TEXTURE_Z_SIZE, norm.shape[1], norm.shape[2])

    volume_u8 = np.round(np.clip(norm, 0.0, 1.0) * 255.0).astype(np.uint8)
    # Data3DTexture expects x-fastest, then y, then z. The grid is already
    # stored as [z, y, x], so C-order bytes are the correct layout.
    packed = volume_u8.tobytes(order="C")

    return {
        "dims": {
            "x": int(volume_u8.shape[2]),
            "y": int(volume_u8.shape[1]),
            "z": int(volume_u8.shape[0]),
        },
        "size_km": {
            "x": round(float(volume_u8.shape[2] * GRID_SPACING_KM), 2),
            "y": round(float(volume_u8.shape[1] * GRID_SPACING_KM), 2),
            "z": round(float(volume_u8.shape[0] * GRID_SPACING_KM), 2),
        },
        "dbz_range": {
            "min": MIN_RENDER_DBZ,
            "max": 75.0,
        },
        "encoding": "base64-u8",
        "data": base64.b64encode(packed).decode("ascii"),
    }


def build_storm_volume(site: str, raw_path: Path, click_lat: float, click_lon: float) -> Optional[StormVolumeResult]:
    volume = parse_volume(raw_path)
    if volume is None:
        return None

    click_x_km, click_y_km = _latlon_to_xy_km(volume.site_lat, volume.site_lon, click_lat, click_lon)
    grid = _accumulate_reflectivity_grid(volume, click_x_km, click_y_km)
    component = None
    selected_threshold = None
    for threshold in (MIN_COMPONENT_DBZ, 30.0, 25.0, MIN_RENDER_DBZ):
        component = _find_selected_component(grid, threshold)
        if component is not None and component.size:
            selected_threshold = threshold
            break
    if component is None or component.size == 0:
        return None

    timestamp = volume.timestamp.strftime("%Y%m%d_%H%M%S") if volume.timestamp else "unknown"
    payload = {
        "site": site,
        "timestamp": timestamp,
        "source_file": raw_path.name,
        "selection_threshold_dbz": selected_threshold,
        "site_location": {
            "lat": volume.site_lat,
            "lon": volume.site_lon,
        },
    }
    payload.update(
        _component_payload(
            component,
            grid,
            volume.site_lat,
            volume.site_lon,
            click_lat,
            click_lon,
            click_x_km,
            click_y_km,
        )
    )
    return StormVolumeResult(payload=payload)


def build_volume_scene(
    site: str,
    raw_path: Path,
    click_lat: float | None = None,
    click_lon: float | None = None,
) -> Optional[StormVolumeResult]:
    volume = parse_volume(raw_path)
    if volume is None:
        return None

    if click_lat is None or click_lon is None:
        auto_selected = True
        click_x_km, click_y_km = _estimate_peak_xy(volume)
        click_lat, click_lon = _xy_to_latlon(volume.site_lat, volume.site_lon, click_x_km, click_y_km)
        component_finder = _find_peak_component
    else:
        auto_selected = False
        click_x_km, click_y_km = _latlon_to_xy_km(volume.site_lat, volume.site_lon, click_lat, click_lon)
        component_finder = _find_selected_component

    grid = _accumulate_reflectivity_grid(volume, click_x_km, click_y_km)
    component = None
    selected_threshold = None
    for threshold in (MIN_COMPONENT_DBZ, 30.0, 25.0, MIN_RENDER_DBZ):
        component = component_finder(grid, threshold)
        if component is not None and component.size:
            selected_threshold = threshold
            break
    if component is None or component.size == 0:
        return None

    timestamp = volume.timestamp.strftime("%Y%m%d_%H%M%S") if volume.timestamp else "unknown"
    unique_tilts = sorted({round(float(t), 1) for t in volume.tilts})

    payload = {
        "site": site,
        "timestamp": timestamp,
        "source_file": raw_path.name,
        "selection_threshold_dbz": selected_threshold,
        "selection_mode": "peak" if auto_selected else "click",
        "site_location": {
            "lat": volume.site_lat,
            "lon": volume.site_lon,
        },
        "volume_meta": {
            "sweep_count": len(volume.tilts),
            "unique_tilt_count": len(unique_tilts),
            "tilts": unique_tilts,
            "grid_spacing_km": GRID_SPACING_KM,
            "grid_radius_km": GRID_RADIUS_KM,
            "max_height_km": MAX_HEIGHT_KM,
        },
    }
    payload.update(
        _component_payload(
            component,
            grid,
            volume.site_lat,
            volume.site_lon,
            click_lat,
            click_lon,
            click_x_km,
            click_y_km,
        )
    )
    payload["volume_texture"] = _volume_texture_payload(component, grid)
    return StormVolumeResult(payload=payload)


# ---------------------------------------------------------------------------
# Enhanced multi-channel volume (reflectivity + velocity + ZDR + CC)
# ---------------------------------------------------------------------------

def _enh_grid_shape() -> tuple[int, int, int]:
    xy_bins = int(round((ENH_GRID_RADIUS_KM * 2.0) / ENH_GRID_SPACING_KM))
    z_bins = int(round(ENH_MAX_HEIGHT_KM / ENH_GRID_SPACING_KM))
    return z_bins, xy_bins, xy_bins


def _sweep_to_cartesian(sweep, click_x_km: float, click_y_km: float,
                        spacing: float, radius: float, max_height: float,
                        min_dbz: float | None = None):
    """Convert a single sweep into flat arrays of (xi, yi, zi, data, mask).

    Returns None if the sweep has no usable gates in the window.
    """
    if sweep.n_gates <= 0:
        return None
    data = sweep.data.filled(np.nan) if hasattr(sweep.data, "filled") else np.asarray(sweep.data, dtype=np.float32)
    if data.ndim != 2:
        return None

    gate_step_km = float(sweep.max_range_km) / float(sweep.n_gates)
    ranges = (np.arange(sweep.n_gates, dtype=np.float32) + 0.5) * gate_step_km
    az_rad = np.radians(np.asarray(sweep.azimuth, dtype=np.float32))
    cos_el = np.cos(np.radians(sweep.tilt_angle))
    ground_range = ranges * cos_el
    z_vals = _beam_height_km(ranges, sweep.tilt_angle).astype(np.float32)

    sin_az = np.sin(az_rad)[:, None]
    cos_az = np.cos(az_rad)[:, None]
    x = sin_az * ground_range[None, :]
    y = cos_az * ground_range[None, :]

    if min_dbz is not None:
        valid = np.isfinite(data) & (data >= min_dbz)
    else:
        valid = np.isfinite(data)
    if not np.any(valid):
        return None

    x = x[valid]
    y = y[valid]
    z = np.broadcast_to(z_vals[None, :], data.shape)[valid]
    vals = data[valid]

    x_min = click_x_km - radius
    y_min = click_y_km - radius
    in_window = (
        (x >= x_min) & (x < x_min + 2.0 * radius) &
        (y >= y_min) & (y < y_min + 2.0 * radius) &
        (z >= 0.0) & (z < max_height)
    )
    if not np.any(in_window):
        return None

    x = x[in_window]
    y = y[in_window]
    z = z[in_window]
    vals = vals[in_window]

    xy_bins = int(round((radius * 2.0) / spacing))
    z_bins = int(round(max_height / spacing))

    xi = np.floor((x - x_min) / spacing).astype(np.int32)
    yi = np.floor((y - y_min) / spacing).astype(np.int32)
    zi = np.floor(z / spacing).astype(np.int32)

    keep = (
        (xi >= 0) & (xi < xy_bins) &
        (yi >= 0) & (yi < xy_bins) &
        (zi >= 0) & (zi < z_bins)
    )
    if not np.any(keep):
        return None

    return xi[keep], yi[keep], zi[keep], vals[keep]


def _accumulate_multichannel_grid(volume, click_x_km: float, click_y_km: float) -> np.ndarray:
    """Build a (nz, ny, nx, 4) float32 grid: [reflectivity, velocity, ZDR, CC]."""
    shape = _enh_grid_shape()
    nz, ny, nx = shape
    grid = np.full((nz, ny, nx, 4), np.nan, dtype=np.float32)

    # Reflectivity needs a separate contiguous array for np.maximum.at
    # (slicing channel 0 from a 4D array produces a non-contiguous view
    # whose .ravel() returns a copy, silently discarding writes).
    refl_grid = np.full((nz, ny, nx), -np.inf, dtype=np.float32)

    spacing = ENH_GRID_SPACING_KM
    radius = ENH_GRID_RADIUS_KM
    max_h = ENH_MAX_HEIGHT_KM

    # Channel 0: Reflectivity (max compositing)
    refl_sweeps = extract_all_sweeps(volume, "reflectivity")
    for sweep in refl_sweeps:
        result = _sweep_to_cartesian(sweep, click_x_km, click_y_km,
                                     spacing, radius, max_h, min_dbz=ENH_MIN_RENDER_DBZ)
        if result is None:
            continue
        xi, yi, zi, vals = result
        flat_idx = zi * (ny * nx) + yi * nx + xi
        np.maximum.at(refl_grid.ravel(), flat_idx, vals.astype(np.float32))

    # Copy reflectivity back into the multichannel grid
    grid[:, :, :, 0] = refl_grid

    # Channel 1: Velocity (last-write from lowest tilt first — iterate low→high)
    vel_sweeps = extract_all_sweeps(volume, "velocity")
    if vel_sweeps:
        sorted_vel = sorted(vel_sweeps, key=lambda s: s.tilt_angle)
        for sweep in sorted_vel:
            result = _sweep_to_cartesian(sweep, click_x_km, click_y_km,
                                         spacing, radius, max_h, min_dbz=None)
            if result is None:
                continue
            xi, yi, zi, vals = result
            grid[zi, yi, xi, 1] = vals.astype(np.float32)

    # Channel 2: ZDR (store value co-located with reflectivity sweeps)
    zdr_sweeps = extract_all_sweeps(volume, "differential_reflectivity")
    if zdr_sweeps:
        for sweep in zdr_sweeps:
            result = _sweep_to_cartesian(sweep, click_x_km, click_y_km,
                                         spacing, radius, max_h, min_dbz=None)
            if result is None:
                continue
            xi, yi, zi, vals = result
            # Use last-write — higher tilts overwrite lower, but reflectivity
            # max-composite determines which voxels are "visible"
            grid[zi, yi, xi, 2] = vals.astype(np.float32)

    # Channel 3: CC (correlation coefficient)
    cc_sweeps = extract_all_sweeps(volume, "cross_correlation_ratio")
    if cc_sweeps:
        for sweep in cc_sweeps:
            result = _sweep_to_cartesian(sweep, click_x_km, click_y_km,
                                         spacing, radius, max_h, min_dbz=None)
            if result is None:
                continue
            xi, yi, zi, vals = result
            grid[zi, yi, xi, 3] = vals.astype(np.float32)

    return grid


def _smooth_channel_3d(arr: np.ndarray) -> np.ndarray:
    """Light 3D box-blur blended with original (72/28 split)."""
    padded = np.pad(arr, 1, mode="edge")
    smoothed = (
        padded[1:-1, 1:-1, 1:-1] * 4.4 +
        padded[:-2, 1:-1, 1:-1] * 0.55 +
        padded[2:, 1:-1, 1:-1] * 0.55 +
        padded[1:-1, :-2, 1:-1] * 0.7 +
        padded[1:-1, 2:, 1:-1] * 0.7 +
        padded[1:-1, 1:-1, :-2] * 0.8 +
        padded[1:-1, 1:-1, 2:] * 0.8
    ) / 8.5
    return np.clip(arr * 0.72 + smoothed * 0.28, 0.0, 1.0)


def _fill_xy_holes(arr: np.ndarray, *, min_support: float = 0.02, min_neighbors: int = 4, passes: int = 2) -> np.ndarray:
    """Fill small empty XY cells that sit inside occupied storm structure."""
    out = arr.copy()
    if out.shape[1] < 3 or out.shape[2] < 3:
        return out

    for _ in range(max(1, passes)):
        padded = np.pad(out, ((0, 0), (1, 1), (1, 1)), mode="edge")
        neighbors = [
            padded[:, :-2, :-2],
            padded[:, :-2, 1:-1],
            padded[:, :-2, 2:],
            padded[:, 1:-1, :-2],
            padded[:, 1:-1, 2:],
            padded[:, 2:, :-2],
            padded[:, 2:, 1:-1],
            padded[:, 2:, 2:],
        ]
        support_mask = [(n > min_support).astype(np.float32) for n in neighbors]
        support_count = np.sum(support_mask, axis=0)
        support_sum = np.sum([np.where(n > min_support, n, 0.0) for n in neighbors], axis=0)
        support_mean = np.divide(
            support_sum,
            np.maximum(support_count, 1.0),
            out=np.zeros_like(out),
            where=support_count > 0,
        )
        holes = (out <= min_support) & (support_count >= float(min_neighbors))
        if not np.any(holes):
            break
        out[holes] = support_mean[holes] * 0.9
    return np.clip(out, 0.0, 1.0)


def _fill_body_voids_3d(
    arr: np.ndarray,
    *,
    min_support: float = 0.02,
    min_neighbors: int = 5,
    passes: int = 2,
) -> np.ndarray:
    """Fill sparse interior 3D holes only when surrounded by echo on multiple faces."""
    out = arr.copy()
    if min(out.shape) < 3:
        return out

    for _ in range(max(1, passes)):
        padded = np.pad(out, 1, mode="edge")
        neighbors = [
            padded[:-2, 1:-1, 1:-1],
            padded[2:, 1:-1, 1:-1],
            padded[1:-1, :-2, 1:-1],
            padded[1:-1, 2:, 1:-1],
            padded[1:-1, 1:-1, :-2],
            padded[1:-1, 1:-1, 2:],
        ]
        support_mask = [(n > min_support).astype(np.float32) for n in neighbors]
        support_count = np.sum(support_mask, axis=0)
        support_sum = np.sum([np.where(n > min_support, n, 0.0) for n in neighbors], axis=0)
        support_mean = np.divide(
            support_sum,
            np.maximum(support_count, 1.0),
            out=np.zeros_like(out),
            where=support_count > 0,
        )
        holes = (out <= min_support) & (support_count >= float(min_neighbors))
        if not np.any(holes):
            break
        out[holes] = support_mean[holes] * 0.92
    return np.clip(out, 0.0, 1.0)


def _fill_vertical_echo_gaps(arr: np.ndarray, *, min_support: float = 0.02, max_gap: int = 6) -> np.ndarray:
    """Interpolate short interior gaps between occupied vertical slices."""
    out = arr.copy()
    nz, ny, nx = out.shape
    if nz < 3:
        return out

    for y in range(ny):
        for x in range(nx):
            col = out[:, y, x]
            occupied = np.flatnonzero(col > min_support)
            if occupied.size < 2:
                continue
            for lower, upper in zip(occupied[:-1], occupied[1:]):
                gap = int(upper - lower - 1)
                if gap <= 0 or gap > max_gap:
                    continue
                start = float(col[lower])
                end = float(col[upper])
                if start <= 0.0 or end <= 0.0:
                    continue
                interp = np.linspace(start, end, gap + 2, dtype=np.float32)[1:-1]
                existing = col[lower + 1:upper]
                col[lower + 1:upper] = np.maximum(existing, interp)
    return out


def _smooth_channel_z(arr: np.ndarray, *, passes: int = 1) -> np.ndarray:
    """Blend neighboring vertical layers to reduce visible sweep slabs."""
    out = arr.copy()
    for _ in range(max(1, passes)):
        padded = np.pad(out, ((1, 1), (0, 0), (0, 0)), mode="edge")
        out = np.clip(
            padded[1:-1] * 0.58 +
            padded[:-2] * 0.21 +
            padded[2:] * 0.21,
            0.0,
            1.0,
        )
    return out


def _upsample_z(arr: np.ndarray, target_z: int) -> np.ndarray:
    """Linear interpolation along axis-0 to target_z slices."""
    if arr.shape[0] == target_z:
        return arr
    old_z = np.arange(arr.shape[0], dtype=np.float32)
    new_z = np.linspace(0, max(arr.shape[0] - 1, 1), target_z, dtype=np.float32)
    flat = arr.reshape(arr.shape[0], -1)
    interp = np.empty((target_z, flat.shape[1]), dtype=np.float32)
    for idx in range(flat.shape[1]):
        interp[:, idx] = np.interp(new_z, old_z, flat[:, idx])
    return interp.reshape(target_z, arr.shape[1], arr.shape[2])


def _volume_texture_payload_enhanced(component: np.ndarray, grid: np.ndarray) -> dict:
    """Pack 4-channel grid into RGBA Uint8 texture for WebGL."""
    z_idx = component[:, 0]
    y_idx = component[:, 1]
    x_idx = component[:, 2]
    refl = grid[z_idx, y_idx, x_idx, 0]

    weights = np.clip(refl, MIN_COMPONENT_DBZ, None)
    weight_sum = float(np.sum(weights)) or 1.0
    center_x = int(round(float(np.sum(x_idx * weights) / weight_sum)))
    center_y = int(round(float(np.sum(y_idx * weights) / weight_sum)))

    half_xy = ENH_TEXTURE_XY_SIZE // 2
    x0 = max(0, min(grid.shape[2] - ENH_TEXTURE_XY_SIZE, center_x - half_xy))
    y0 = max(0, min(grid.shape[1] - ENH_TEXTURE_XY_SIZE, center_y - half_xy))
    x1 = min(grid.shape[2], x0 + ENH_TEXTURE_XY_SIZE)
    y1 = min(grid.shape[1], y0 + ENH_TEXTURE_XY_SIZE)
    z0 = 0
    z1 = grid.shape[0]

    sub = grid[z0:z1, y0:y1, x0:x1, :].copy()

    # --- Channel R: Reflectivity (0–75 dBZ → 0–255) ---
    refl_ch = sub[:, :, :, 0].copy()
    refl_ch[refl_ch < ENH_MIN_RENDER_DBZ] = -np.inf
    refl_ch[~np.isfinite(refl_ch)] = 0.0
    refl_norm = np.clip((refl_ch - ENH_MIN_RENDER_DBZ) / (75.0 - ENH_MIN_RENDER_DBZ), 0.0, 1.0).astype(np.float32)
    refl_norm = _fill_xy_holes(refl_norm, min_support=0.025, min_neighbors=4, passes=3)
    refl_norm = _fill_body_voids_3d(refl_norm, min_support=0.025, min_neighbors=5, passes=2)
    refl_norm = _smooth_channel_3d(refl_norm)
    refl_norm = _fill_xy_holes(refl_norm, min_support=0.025, min_neighbors=5, passes=2)
    refl_norm = _fill_vertical_echo_gaps(refl_norm, min_support=0.025, max_gap=10)
    refl_norm = _smooth_channel_z(refl_norm, passes=2)
    refl_norm = _fill_body_voids_3d(refl_norm, min_support=0.025, min_neighbors=5, passes=1)
    refl_norm = _upsample_z(refl_norm, ENH_TEXTURE_Z_SIZE)

    # --- Channel G: Velocity (-50 to +50 m/s → 0–255, 128=zero) ---
    vel_ch = sub[:, :, :, 1].copy()
    vel_ch[~np.isfinite(vel_ch)] = 0.0
    vel_lo, vel_hi = ENH_VEL_RANGE
    vel_norm = np.clip((vel_ch - vel_lo) / (vel_hi - vel_lo), 0.0, 1.0).astype(np.float32)
    vel_norm = _fill_xy_holes(vel_norm, min_support=0.001, min_neighbors=4, passes=1)
    vel_norm = _fill_vertical_echo_gaps(vel_norm, min_support=0.001, max_gap=6)
    vel_norm = _smooth_channel_z(vel_norm, passes=1)
    vel_norm = _upsample_z(vel_norm, ENH_TEXTURE_Z_SIZE)

    # --- Channel B: ZDR (-2.0 to +6.0 dB → 0–255) ---
    zdr_ch = sub[:, :, :, 2].copy()
    zdr_ch[~np.isfinite(zdr_ch)] = 2.0  # neutral ZDR fill
    zdr_lo, zdr_hi = ENH_ZDR_RANGE
    zdr_norm = np.clip((zdr_ch - zdr_lo) / (zdr_hi - zdr_lo), 0.0, 1.0).astype(np.float32)
    zdr_norm = _fill_xy_holes(zdr_norm, min_support=0.001, min_neighbors=4, passes=1)
    zdr_norm = _fill_vertical_echo_gaps(zdr_norm, min_support=0.001, max_gap=6)
    zdr_norm = _smooth_channel_z(zdr_norm, passes=1)
    zdr_norm = _upsample_z(zdr_norm, ENH_TEXTURE_Z_SIZE)

    # --- Channel A: CC quality mask (CC ≥ 0.85 → 128–255; < 0.85 → 0–127) ---
    cc_ch = sub[:, :, :, 3].copy()
    has_cc = np.isfinite(cc_ch).any()
    if has_cc:
        cc_ch[~np.isfinite(cc_ch)] = 1.0  # assume met where no CC data
        cc_norm = np.zeros_like(cc_ch, dtype=np.float32)
        met_mask = cc_ch >= ENH_CC_MET_THRESHOLD
        cc_norm[met_mask] = 0.5 + 0.5 * np.clip(
            (cc_ch[met_mask] - ENH_CC_MET_THRESHOLD) / (1.0 - ENH_CC_MET_THRESHOLD), 0.0, 1.0
        )
        cc_norm[~met_mask] = 0.5 * np.clip(cc_ch[~met_mask] / ENH_CC_MET_THRESHOLD, 0.0, 1.0)
    else:
        cc_norm = np.ones_like(cc_ch, dtype=np.float32)  # no CC → assume all met
    cc_norm = _fill_vertical_echo_gaps(cc_norm, min_support=0.001, max_gap=6)
    cc_norm = _smooth_channel_z(cc_norm, passes=1)
    cc_norm = _upsample_z(cc_norm, ENH_TEXTURE_Z_SIZE)

    # Pack RGBA interleaved: (z, y, x, 4) → bytes
    rgba = np.stack([
        np.round(np.clip(refl_norm, 0.0, 1.0) * 255.0).astype(np.uint8),
        np.round(np.clip(vel_norm, 0.0, 1.0) * 255.0).astype(np.uint8),
        np.round(np.clip(zdr_norm, 0.0, 1.0) * 255.0).astype(np.uint8),
        np.round(np.clip(cc_norm, 0.0, 1.0) * 255.0).astype(np.uint8),
    ], axis=-1)  # shape: (z, y, x, 4)

    packed = rgba.tobytes(order="C")

    return {
        "dims": {
            "x": int(rgba.shape[2]),
            "y": int(rgba.shape[1]),
            "z": int(rgba.shape[0]),
        },
        "size_km": {
            "x": round(float(rgba.shape[2] * ENH_GRID_SPACING_KM), 2),
            "y": round(float(rgba.shape[1] * ENH_GRID_SPACING_KM), 2),
            "z": round(ENH_MAX_HEIGHT_KM, 2),
        },
        "channels": {
            "r": {"field": "reflectivity", "unit": "dBZ", "min": ENH_MIN_RENDER_DBZ, "max": 75.0},
            "g": {"field": "velocity", "unit": "m/s", "min": ENH_VEL_RANGE[0], "max": ENH_VEL_RANGE[1]},
            "b": {"field": "differential_reflectivity", "unit": "dB", "min": ENH_ZDR_RANGE[0], "max": ENH_ZDR_RANGE[1]},
            "a": {"field": "cross_correlation_ratio", "note": "quality mask; >=0.5 is meteorological"},
        },
        "has_velocity": True,  # patched by caller with actual check
        "has_dual_pol": bool(has_cc),
        "encoding": "base64-rgba-u8",
        "data": base64.b64encode(packed).decode("ascii"),
    }


def build_volume_scene_enhanced(
    site: str,
    raw_path: Path,
    volume=None,
    click_lat: float | None = None,
    click_lon: float | None = None,
    box_lat_min: float | None = None,
    box_lon_min: float | None = None,
    box_lat_max: float | None = None,
    box_lon_max: float | None = None,
) -> Optional[StormVolumeResult]:
    """Build an enhanced multi-channel 3D volume for the inline storm profiler."""
    if volume is None:
        volume = parse_volume(raw_path)
    if volume is None:
        return None

    if click_lat is None or click_lon is None:
        auto_selected = True
        click_x_km, click_y_km = _estimate_peak_xy(volume)
        click_lat, click_lon = _xy_to_latlon(volume.site_lat, volume.site_lon, click_x_km, click_y_km)
    else:
        auto_selected = False
        click_x_km, click_y_km = _latlon_to_xy_km(volume.site_lat, volume.site_lon, click_lat, click_lon)

    # Build 4-channel grid
    mcgrid = _accumulate_multichannel_grid(volume, click_x_km, click_y_km)

    # Component detection on reflectivity channel only
    refl_grid = mcgrid[:, :, :, 0].copy()

    # Use enhanced grid params for component finding
    enh_kwargs = dict(grid_radius_km=ENH_GRID_RADIUS_KM,
                      grid_spacing_km=ENH_GRID_SPACING_KM,
                      selection_radius_km=ENH_GRID_RADIUS_KM * 0.8)

    component = None
    selected_threshold = None
    has_box = all(v is not None for v in (box_lat_min, box_lon_min, box_lat_max, box_lon_max))
    box_x_km = None
    box_y_km = None
    if has_box:
        x0, y0 = _latlon_to_xy_km(volume.site_lat, volume.site_lon, float(box_lat_min), float(box_lon_min))
        x1, y1 = _latlon_to_xy_km(volume.site_lat, volume.site_lon, float(box_lat_max), float(box_lon_max))
        box_x_km = (min(x0, x1), max(x0, x1))
        box_y_km = (min(y0, y1), max(y0, y1))
    for threshold in (MIN_COMPONENT_DBZ, 30.0, 25.0, ENH_MIN_RENDER_DBZ):
        if auto_selected:
            component = _find_peak_component(refl_grid, threshold)
        elif has_box and box_x_km is not None and box_y_km is not None:
            component = _find_box_component(
                refl_grid,
                threshold,
                box_x_km,
                box_y_km,
                click_x_km,
                click_y_km,
                ENH_GRID_RADIUS_KM,
                ENH_GRID_SPACING_KM,
            )
        else:
            component = _find_selected_component(refl_grid, threshold, **enh_kwargs)
        if component is not None and component.size:
            selected_threshold = threshold
            break
    if component is None or component.size == 0:
        return None

    timestamp = volume.timestamp.strftime("%Y%m%d_%H%M%S") if volume.timestamp else "unknown"
    unique_tilts = sorted({round(float(t), 1) for t in volume.tilts})

    # Build component summary using reflectivity channel
    z_idx = component[:, 0]
    y_idx = component[:, 1]
    x_idx = component[:, 2]
    dbz = refl_grid[z_idx, y_idx, x_idx]
    x_min = click_x_km - ENH_GRID_RADIUS_KM
    y_min = click_y_km - ENH_GRID_RADIUS_KM
    z_km = (z_idx.astype(np.float32) + 0.5) * ENH_GRID_SPACING_KM

    payload = {
        "site": site,
        "timestamp": timestamp,
        "source_file": raw_path.name,
        "selection_threshold_dbz": selected_threshold,
        "selection_mode": "peak" if auto_selected else ("box" if has_box else "click"),
        "mode": "enhanced",
        "site_location": {
            "lat": volume.site_lat,
            "lon": volume.site_lon,
        },
        "volume_meta": {
            "sweep_count": len(volume.tilts),
            "unique_tilt_count": len(unique_tilts),
            "tilts": unique_tilts,
            "grid_spacing_km": ENH_GRID_SPACING_KM,
            "grid_radius_km": ENH_GRID_RADIUS_KM,
            "max_height_km": ENH_MAX_HEIGHT_KM,
        },
        "summary": {
            "voxel_count": int(component.shape[0]),
            "max_dbz": round(float(np.nanmax(dbz)), 1),
            "top_km": round(float(np.nanmax(z_km)), 1),
            "base_km": round(float(np.nanmin(z_km)), 1),
        },
        "grid": {
            "spacing_km": ENH_GRID_SPACING_KM,
            "radius_km": ENH_GRID_RADIUS_KM,
        },
        "selection": {
            "click_lat": round(click_lat, 4),
            "click_lon": round(click_lon, 4),
            "click_x_km": round(click_x_km, 2),
            "click_y_km": round(click_y_km, 2),
        },
    }
    if has_box and box_x_km is not None and box_y_km is not None:
        box_center_x = (box_x_km[0] + box_x_km[1]) * 0.5
        box_center_y = (box_y_km[0] + box_y_km[1]) * 0.5
        payload["selection"]["box"] = {
            "lat_min": round(float(box_lat_min), 4),
            "lon_min": round(float(box_lon_min), 4),
            "lat_max": round(float(box_lat_max), 4),
            "lon_max": round(float(box_lon_max), 4),
            "width_km": round(box_x_km[1] - box_x_km[0], 2),
            "height_km": round(box_y_km[1] - box_y_km[0], 2),
            "center_x_km": round(box_center_x, 2),
            "center_y_km": round(box_center_y, 2),
        }

    # Store the volume reference so the texture builder can check field availability
    _enh_volume_ref = volume
    payload["volume_texture"] = _volume_texture_payload_enhanced(component, mcgrid)
    # Patch has_velocity accurately
    payload["volume_texture"]["has_velocity"] = bool(extract_all_sweeps(volume, "velocity"))

    return StormVolumeResult(payload=payload)
