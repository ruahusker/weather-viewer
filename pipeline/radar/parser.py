"""Level II radar parser using PyART."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

CHUNK_MOMENT_MAP = {
    "reflectivity": "REF",
    "velocity": "VEL",
    "spectrum_width": "SW",
    "differential_reflectivity": "ZDR",
    "cross_correlation_ratio": "RHO",
    "differential_phase": "PHI",
}

_CHUNK_TS_RE = re.compile(r"(\d{8})_(\d{6})")


@dataclass
class SweepData:
    """Extracted data for one sweep (one tilt of one field)."""
    data: np.ndarray          # 2D (n_az, n_gates), may be masked
    azimuth: np.ndarray       # 1D azimuths in degrees
    tilt_angle: float
    max_range_km: float
    n_gates: int
    cc_data: Optional[np.ndarray] = None  # for clutter filtering


@dataclass
class VolumeData:
    """Parsed Level II volume scan."""
    site_lat: float
    site_lon: float
    tilts: list[float]
    fields: list[str]
    timestamp: Optional[datetime]
    raw_path: Path
    fixed_angles: np.ndarray
    field_sweeps: dict[str, list[Optional[SweepData]]] = field(default_factory=dict)
    sweep_starts: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.int32))
    sweep_ends: np.ndarray = field(default_factory=lambda: np.array([], dtype=np.int32))
    backend: str = "pyart"
    radar: object | None = None


_COMPACT_FIELD_NAMES = {
    "reflectivity",
    "velocity",
    "spectrum_width",
    "differential_reflectivity",
    "cross_correlation_ratio",
    "differential_phase",
    "specific_differential_phase",
}


def _masked_to_float32(data) -> np.ndarray:
    """Convert a Py-ART field slice to compact float32 with NaN fill."""
    if hasattr(data, "filled"):
        return np.asarray(data.filled(np.nan), dtype=np.float32)
    return np.asarray(data, dtype=np.float32)


def _build_compact_pyart_sweeps(radar, fixed_angles: np.ndarray) -> dict[str, list[Optional[SweepData]]]:
    """Extract only the per-sweep arrays needed after parse and drop the live Radar object."""
    sweep_starts = radar.sweep_start_ray_index["data"]
    sweep_ends = radar.sweep_end_ray_index["data"]
    nsweeps = int(radar.nsweeps)
    max_range_km = round(float(radar.range["data"][-1] / 1000.0), 1)
    azimuth_by_sweep = [
        np.asarray(radar.azimuth["data"][sweep_starts[i]:sweep_ends[i] + 1], dtype=np.float32)
        for i in range(nsweeps)
    ]

    field_sweeps: dict[str, list[Optional[SweepData]]] = {}
    for field_name in radar.fields.keys():
        if field_name not in _COMPACT_FIELD_NAMES:
            continue
        per_sweep: list[Optional[SweepData]] = []
        field_data = radar.fields[field_name]["data"]
        for sweep_idx in range(nsweeps):
            s_start = sweep_starts[sweep_idx]
            s_end = sweep_ends[sweep_idx] + 1
            data = _masked_to_float32(field_data[s_start:s_end])
            if data.ndim != 2:
                per_sweep.append(None)
                continue
            valid_count = int(np.count_nonzero(np.isfinite(data)))
            if valid_count <= 50:
                per_sweep.append(None)
                continue
            per_sweep.append(SweepData(
                data=data,
                azimuth=azimuth_by_sweep[sweep_idx],
                tilt_angle=round(float(fixed_angles[sweep_idx]), 1),
                max_range_km=max_range_km,
                n_gates=data.shape[1],
                cc_data=None,
            ))
        field_sweeps[field_name] = per_sweep

    cc_sweeps = field_sweeps.get("cross_correlation_ratio")
    if cc_sweeps is not None:
        for field_name, sweeps in field_sweeps.items():
            if field_name == "cross_correlation_ratio":
                continue
            for sweep_idx, sweep in enumerate(sweeps):
                if sweep is None or sweep_idx >= len(cc_sweeps):
                    continue
                cc_sweep = cc_sweeps[sweep_idx]
                if cc_sweep is None or cc_sweep.data.shape != sweep.data.shape:
                    continue
                sweep.cc_data = cc_sweep.data

    return field_sweeps


def parse_volume(path: Path) -> Optional[VolumeData]:
    """Parse a Level II volume scan file with PyART.

    Args:
        path: Path to .ar2v file

    Returns:
        VolumeData or None if parsing fails
    """
    if path.suffix == ".chunkbin":
        return _parse_chunk_volume(path)

    try:
        import pyart
        # Set memory limit to prevent OOM on large files (8GB)
        # This helps prevent the process from being killed
        radar = pyart.io.read_nexrad_archive(str(path))
    except MemoryError:
        logger.error(f"Out of memory reading {path.name}")
        return None
    except Exception as e:
        logger.error(f"PyART failed to read {path.name}: {e}")
        return None

    site_lat = round(float(radar.latitude['data'][0]), 4)
    site_lon = round(float(radar.longitude['data'][0]), 4)
    fixed_angles = np.array(radar.fixed_angle['data'], dtype=np.float32)
    sweep_starts = np.asarray(radar.sweep_start_ray_index['data'], dtype=np.int32)
    sweep_ends = np.asarray(radar.sweep_end_ray_index['data'], dtype=np.int32)
    field_sweeps = _build_compact_pyart_sweeps(radar, fixed_angles)

    # Parse timestamp from filename: Level2_KTLX_20260310_1541.ar2v
    timestamp = None
    try:
        parts = path.stem.split('_')
        if len(parts) >= 4:
            dt_str = f"{parts[2]}_{parts[3]}"
            timestamp = datetime.strptime(dt_str, "%Y%m%d_%H%M").replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        pass

    return VolumeData(
        site_lat=site_lat,
        site_lon=site_lon,
        tilts=[round(float(a), 1) for a in fixed_angles],
        fields=sorted(field_sweeps.keys()),
        timestamp=timestamp,
        raw_path=path,
        sweep_starts=sweep_starts,
        sweep_ends=sweep_ends,
        fixed_angles=fixed_angles,
        field_sweeps=field_sweeps,
    )


def _parse_chunk_timestamp(path: Path) -> Optional[datetime]:
    match = _CHUNK_TS_RE.search(path.stem)
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)}_{match.group(2)}",
            "%Y%m%d_%H%M%S",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_chunk_volume(path: Path) -> Optional[VolumeData]:
    """Parse a concatenated chunk file with MetPy."""
    try:
        from metpy.io import Level2File
    except Exception as e:
        logger.error(f"MetPy unavailable for chunk parsing {path.name}: {e}")
        return None

    try:
        level2 = Level2File(str(path))
    except Exception as e:
        logger.error(f"MetPy failed to read {path.name}: {e}")
        return None

    site_lat = None
    site_lon = None
    tilts = []
    field_sweeps: dict[str, list[Optional[SweepData]]] = {}

    for sweep in level2.sweeps:
        if not sweep:
            continue
        angles = [float(r.header.el_angle) for r in sweep if getattr(r, "header", None) is not None]
        if angles:
            tilts.append(round(float(np.nanmedian(np.array(angles, dtype=np.float32))), 1))
        for radial in sweep:
            vol_consts = getattr(radial, "vol_consts", None)
            if vol_consts is not None and site_lat is None and site_lon is None:
                site_lat = round(float(vol_consts.lat), 4)
                site_lon = round(float(vol_consts.lon), 4)

    if site_lat is None or site_lon is None:
        logger.warning(f"Chunk volume missing site location {path.name}")
        return None

    for field_name in CHUNK_MOMENT_MAP.keys():
        sweeps: list[Optional[SweepData]] = []
        for sweep in level2.sweeps:
            if not sweep:
                sweeps.append(None)
                continue
            arrays = _moment_arrays_from_metpy_sweep(sweep, _moment_name(field_name))
            if arrays is None:
                sweeps.append(None)
                continue
            data, azimuth, tilt_angle, max_range_km, cc_data = arrays
            valid_count = int(np.sum(~np.isnan(data)))
            if valid_count <= 50:
                sweeps.append(None)
                continue
            sweeps.append(SweepData(
                data=data,
                azimuth=azimuth,
                tilt_angle=tilt_angle,
                max_range_km=max_range_km,
                n_gates=data.shape[1],
                cc_data=cc_data,
            ))
        if any(s is not None for s in sweeps):
            field_sweeps[field_name] = sweeps

    fixed_angles = np.array(tilts, dtype=np.float32) if tilts else np.array([], dtype=np.float32)
    empty = np.array([], dtype=np.int32)

    return VolumeData(
        site_lat=site_lat,
        site_lon=site_lon,
        tilts=tilts,
        fields=sorted(field_sweeps.keys()),
        timestamp=_parse_chunk_timestamp(path),
        raw_path=path,
        sweep_starts=empty,
        sweep_ends=empty,
        fixed_angles=fixed_angles,
        field_sweeps=field_sweeps,
        backend="metpy",
    )


def _moment_name(field_name: str) -> str:
    return CHUNK_MOMENT_MAP.get(field_name, field_name)


def _radial_moment(radial, moment_name: str):
    return radial.moments.get(moment_name) or radial.moments.get(moment_name.encode())


def _moment_arrays_from_metpy_sweep(sweep: list, moment_name: str) -> Optional[tuple[np.ndarray, np.ndarray, float, int, Optional[np.ndarray]]]:
    """Build padded azimuth/data arrays from a MetPy sweep."""
    max_gates = 0
    first_gate = 0.0
    gate_width = 1.0
    found = False
    rows: list[Optional[np.ndarray]] = []
    azimuth = []
    sweep_angles = []

    for radial in sweep:
        azimuth.append(float(radial.header.az_angle))
        sweep_angles.append(float(radial.header.el_angle))
        moment = _radial_moment(radial, moment_name)
        if moment is None:
            rows.append(None)
            continue

        hdr, values = moment
        arr = np.asarray(values, dtype=np.float32)
        rows.append(arr)
        max_gates = max(max_gates, arr.shape[0])
        first_gate = float(hdr.first_gate)
        gate_width = float(hdr.gate_width)
        found = True

    if not found or max_gates <= 0:
        return None

    data = np.full((len(rows), max_gates), np.nan, dtype=np.float32)
    for i, arr in enumerate(rows):
        if arr is None:
            continue
        data[i, :arr.shape[0]] = arr

    max_range_km = first_gate + gate_width * max_gates

    cc_data = None
    if moment_name != "RHO":
        cc_rows = []
        cc_found = False
        cc_max_gates = 0
        for radial in sweep:
            moment = _radial_moment(radial, "RHO")
            if moment is None:
                cc_rows.append(None)
                continue
            _, values = moment
            arr = np.asarray(values, dtype=np.float32)
            cc_rows.append(arr)
            cc_max_gates = max(cc_max_gates, arr.shape[0])
            cc_found = True
        if cc_found and cc_max_gates == max_gates:
            cc_data = np.full((len(cc_rows), cc_max_gates), np.nan, dtype=np.float32)
            for i, arr in enumerate(cc_rows):
                if arr is None:
                    continue
                cc_data[i, :arr.shape[0]] = arr

    return (
        data,
        np.array(azimuth, dtype=np.float32),
        round(float(np.nanmedian(np.array(sweep_angles, dtype=np.float32))), 1),
        round(float(max_range_km), 1),
        cc_data,
    )


def _extract_metpy_sweep(volume: VolumeData, field_name: str,
                         target_tilt: float = 0.5,
                         prefer_long_range: bool = True) -> Optional[SweepData]:
    """Extract a sweep from a chunk-backed MetPy volume."""
    candidates = []
    for sweep in volume.field_sweeps.get(field_name, []):
        if sweep is None:
            continue
        if abs(float(sweep.tilt_angle) - target_tilt) > 0.3:
            continue
        valid_count = int(np.count_nonzero(np.isfinite(sweep.data)))
        candidates.append({
            "data": sweep.data,
            "azimuth": sweep.azimuth,
            "tilt_angle": sweep.tilt_angle,
            "max_range_km": sweep.max_range_km,
            "cc_data": sweep.cc_data,
            "n_gates": sweep.n_gates,
            "valid_count": valid_count,
            "angle": sweep.tilt_angle,
        })

    if not candidates:
        for sweep in volume.field_sweeps.get(field_name, []):
            if sweep is None:
                continue
            valid_count = int(np.count_nonzero(np.isfinite(sweep.data)))
            if valid_count > 100:
                return SweepData(
                    data=sweep.data,
                    azimuth=sweep.azimuth,
                    tilt_angle=sweep.tilt_angle,
                    max_range_km=sweep.max_range_km,
                    n_gates=sweep.n_gates,
                    cc_data=sweep.cc_data,
                )
        return None

    if prefer_long_range:
        candidates.sort(key=lambda c: (-c["n_gates"], -c["valid_count"]))
    else:
        candidates.sort(key=lambda c: (-c["valid_count"], c["n_gates"]))

    best = candidates[0]
    return SweepData(
        data=best["data"],
        azimuth=best["azimuth"],
        tilt_angle=best["tilt_angle"],
        max_range_km=best["max_range_km"],
        n_gates=best["n_gates"],
        cc_data=best["cc_data"],
    )


def find_best_sweep(volume: VolumeData, field_name: str,
                    target_tilt: float = 0.5,
                    prefer_long_range: bool = True) -> Optional[int]:
    """Find the best sweep index for a given field.

    Handles Level II split-cut VCPs where the lowest tilt has separate
    surveillance (long-range reflectivity) and Doppler (velocity) cuts.
    """
    sweeps = volume.field_sweeps.get(field_name, [])
    if not sweeps:
        return None

    candidates = []
    for i, sweep in enumerate(sweeps):
        if sweep is None:
            continue
        if abs(float(sweep.tilt_angle) - target_tilt) > 0.3:
            continue
        valid_count = int(np.count_nonzero(np.isfinite(sweep.data)))
        n_gates = sweep.n_gates

        candidates.append({
            'sweep_idx': i,
            'angle': float(sweep.tilt_angle),
            'n_gates': n_gates,
            'valid_count': valid_count,
        })

    if not candidates:
        # Fall back: any sweep with valid data
        for i, sweep in enumerate(sweeps):
            if sweep is None:
                continue
            valid_count = int(np.count_nonzero(np.isfinite(sweep.data)))
            if valid_count > 100:
                return i
        return None

    if prefer_long_range:
        candidates.sort(key=lambda c: (-c['n_gates'], -c['valid_count']))
    else:
        candidates.sort(key=lambda c: (-c['valid_count'], c['n_gates']))

    best = candidates[0]
    return best['sweep_idx'] if best['valid_count'] > 50 else None


def extract_all_sweeps(volume: VolumeData, field_name: str) -> list[SweepData]:
    """Extract all sweeps containing a given field, ordered by tilt."""
    sweeps = [sweep for sweep in volume.field_sweeps.get(field_name, []) if sweep is not None]
    sweeps.sort(key=lambda s: s.tilt_angle)
    return sweeps


def extract_sweep(volume: VolumeData, field_name: str,
                  target_tilt: float = 0.5,
                  prefer_long_range: bool = True) -> Optional[SweepData]:
    """Extract a single sweep from a parsed volume.

    Args:
        volume: Parsed VolumeData
        field_name: PyART field name (e.g., 'reflectivity')
        target_tilt: Desired elevation angle
        prefer_long_range: Prefer surveillance cut (True for reflectivity)

    Returns:
        SweepData or None
    """
    if volume.backend == "metpy":
        return _extract_metpy_sweep(
            volume,
            field_name,
            target_tilt=target_tilt,
            prefer_long_range=prefer_long_range,
        )

    sweep_idx = find_best_sweep(volume, field_name, target_tilt, prefer_long_range)
    if sweep_idx is None:
        return None
    sweeps = volume.field_sweeps.get(field_name, [])
    if sweep_idx >= len(sweeps):
        return None
    return sweeps[sweep_idx]
