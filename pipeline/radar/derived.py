"""Derived Level II radar products built from raw moments and full volumes."""

from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter

from .parser import SweepData, VolumeData, extract_all_sweeps, extract_sweep

DEFAULT_STORM_MOTION_DIR_DEG = 240.0  # meteorological "from" direction
DEFAULT_STORM_MOTION_KT = 30.0
EARTH_RADIUS_KM = 6371.0
REFLECTIVITY_THRESHOLD_DBZ = 18.0

RAW_LEVEL2_FIELDS = {
    "reflectivity",
    "velocity",
    "spectrum_width",
    "differential_reflectivity",
    "cross_correlation_ratio",
    "differential_phase",
}


def _to_float(data: np.ndarray) -> np.ndarray:
    if hasattr(data, "filled"):
        return np.asarray(data.filled(np.nan), dtype=np.float32)
    return np.asarray(data, dtype=np.float32)


def _range_grid_km(n_gates: int, max_range_km: float) -> np.ndarray:
    if n_gates <= 0:
        return np.array([], dtype=np.float32)
    gate_km = max_range_km / max(n_gates, 1)
    return (np.arange(n_gates, dtype=np.float32) + 0.5) * gate_km


def _nearest_azimuth_indices(source_az: np.ndarray, target_az: np.ndarray) -> np.ndarray:
    src = np.mod(np.asarray(source_az, dtype=np.float32), 360.0)
    tgt = np.mod(np.asarray(target_az, dtype=np.float32), 360.0)
    delta = np.abs(src[:, None] - tgt[None, :])
    delta = np.minimum(delta, 360.0 - delta)
    return np.argmin(delta, axis=0).astype(np.int32)


def _resample_to_reference(
    sweep: SweepData,
    ref_azimuth: np.ndarray,
    ref_ranges_km: np.ndarray,
) -> np.ndarray:
    """Resample a sweep onto the reference lowest-tilt azimuth/range grid."""
    data = _to_float(sweep.data)
    if data.size == 0:
        return np.full((len(ref_azimuth), len(ref_ranges_km)), np.nan, dtype=np.float32)

    source_ranges_km = _range_grid_km(sweep.n_gates, sweep.max_range_km)
    az_idx = _nearest_azimuth_indices(sweep.azimuth, ref_azimuth)
    out = np.full((len(ref_azimuth), len(ref_ranges_km)), np.nan, dtype=np.float32)

    for out_row, src_row_idx in enumerate(az_idx):
        row = data[src_row_idx]
        valid = np.isfinite(row)
        if np.count_nonzero(valid) < 2:
            continue
        out[out_row] = np.interp(
            ref_ranges_km,
            source_ranges_km[valid],
            row[valid],
            left=np.nan,
            right=np.nan,
        )
        out[out_row, ref_ranges_km > sweep.max_range_km] = np.nan

    return out


def _nan_box_filter(data: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    filled = np.nan_to_num(data, nan=0.0)
    weights = np.isfinite(data).astype(np.float32)
    smooth = uniform_filter(filled, size=size, mode="nearest")
    weight_smooth = uniform_filter(weights, size=size, mode="nearest")
    out = smooth / np.maximum(weight_smooth, 1e-6)
    out[weight_smooth < 1e-3] = np.nan
    return out.astype(np.float32)


def _texture(data: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    mean = _nan_box_filter(data, size)
    mean_sq = _nan_box_filter(data ** 2, size)
    variance = np.maximum(mean_sq - mean ** 2, 0.0)
    return np.sqrt(variance).astype(np.float32)


def _beam_height_km(range_km: np.ndarray, tilt_deg: float) -> np.ndarray:
    elev = np.radians(tilt_deg)
    k_radius = (4.0 / 3.0) * EARTH_RADIUS_KM
    return range_km * np.sin(elev) + (range_km ** 2) / (2.0 * k_radius)


def _make_sweep(data: np.ndarray, ref: SweepData) -> SweepData:
    return SweepData(
        data=np.asarray(data, dtype=np.float32),
        azimuth=np.asarray(ref.azimuth, dtype=np.float32),
        tilt_angle=ref.tilt_angle,
        max_range_km=ref.max_range_km,
        n_gates=ref.n_gates,
        cc_data=None,
    )


def _derive_kdp(volume: VolumeData, target_tilt: float) -> SweepData | None:
    phi = extract_sweep(volume, "differential_phase", target_tilt=target_tilt)
    if phi is None:
        return None
    data = _to_float(phi.data)
    gate_km = phi.max_range_km / max(phi.n_gates, 1)
    smooth = _nan_box_filter(data, size=(3, 9))
    kdp = 0.5 * np.gradient(smooth, gate_km, axis=1)
    if phi.cc_data is not None:
        cc = _to_float(phi.cc_data)
        kdp[cc < 0.8] = np.nan
    kdp = np.clip(kdp, -1.0, 8.0)
    return _make_sweep(kdp, phi)


def _derive_srm(volume: VolumeData, target_tilt: float) -> SweepData | None:
    vel = extract_sweep(volume, "velocity", target_tilt=target_tilt, prefer_long_range=False)
    if vel is None:
        return None
    data = _to_float(vel.data)
    speed_ms = DEFAULT_STORM_MOTION_KT * 0.514444
    toward_deg = (DEFAULT_STORM_MOTION_DIR_DEG + 180.0) % 360.0
    toward_rad = np.radians(toward_deg)
    storm_u = speed_ms * np.sin(toward_rad)
    storm_v = speed_ms * np.cos(toward_rad)
    az_rad = np.radians(vel.azimuth)[:, None]
    radial_component = storm_u * np.sin(az_rad) + storm_v * np.cos(az_rad)
    srm = data - radial_component
    return _make_sweep(srm, vel)


def _derive_azimuthal_shear(volume: VolumeData, target_tilt: float) -> SweepData | None:
    vel = extract_sweep(volume, "velocity", target_tilt=target_tilt, prefer_long_range=False)
    if vel is None:
        return None
    data = _to_float(vel.data)
    az_rad = np.unwrap(np.radians(vel.azimuth.astype(np.float32)))
    dtheta = np.gradient(az_rad).astype(np.float32)
    range_km = _range_grid_km(vel.n_gates, vel.max_range_km)
    arc_m = np.maximum(np.abs(dtheta)[:, None] * range_km[None, :] * 1000.0, 250.0)
    dv = np.gradient(data, axis=0)
    shear = np.abs(dv / arc_m)
    shear[~np.isfinite(data)] = np.nan
    return _make_sweep(np.clip(shear, 0.0, 0.05), vel)


def _derive_divergence(volume: VolumeData, target_tilt: float) -> SweepData | None:
    vel = extract_sweep(volume, "velocity", target_tilt=target_tilt, prefer_long_range=False)
    if vel is None:
        return None
    data = _to_float(vel.data)
    gate_km = vel.max_range_km / max(vel.n_gates, 1)
    divergence = np.gradient(data, gate_km, axis=1)
    divergence[~np.isfinite(data)] = np.nan
    return _make_sweep(np.clip(divergence, -12.0, 12.0), vel)


def _derive_cc_texture(volume: VolumeData, target_tilt: float) -> SweepData | None:
    cc = extract_sweep(volume, "cross_correlation_ratio", target_tilt=target_tilt)
    if cc is None:
        return None
    texture = _texture(_to_float(cc.data), size=(5, 7))
    return _make_sweep(np.clip(texture, 0.0, 0.25), cc)


def _derive_phi_texture(volume: VolumeData, target_tilt: float) -> SweepData | None:
    phi = extract_sweep(volume, "differential_phase", target_tilt=target_tilt)
    if phi is None:
        return None
    texture = _texture(_to_float(phi.data), size=(5, 7))
    return _make_sweep(np.clip(texture, 0.0, 60.0), phi)


def _derive_debris_flag(volume: VolumeData, target_tilt: float) -> SweepData | None:
    refl = extract_sweep(volume, "reflectivity", target_tilt=target_tilt, prefer_long_range=True)
    cc = extract_sweep(volume, "cross_correlation_ratio", target_tilt=target_tilt)
    zdr = extract_sweep(volume, "differential_reflectivity", target_tilt=target_tilt)
    shear = _derive_azimuthal_shear(volume, target_tilt)
    if refl is None or cc is None or shear is None:
        return None

    ref_ranges_km = _range_grid_km(refl.n_gates, refl.max_range_km)
    refl_data = _to_float(refl.data)
    cc_data = _resample_to_reference(cc, refl.azimuth, ref_ranges_km)
    shear_data = _resample_to_reference(shear, refl.azimuth, ref_ranges_km)
    if zdr is not None:
        zdr_data = _resample_to_reference(zdr, refl.azimuth, ref_ranges_km)
    else:
        zdr_data = np.full_like(refl_data, np.nan)

    score = np.zeros_like(refl_data, dtype=np.float32)
    score += np.clip((refl_data - 35.0) / 20.0, 0.0, 1.0) * 0.35
    score += np.clip((0.95 - cc_data) / 0.15, 0.0, 1.0) * 0.40
    score += np.clip((shear_data - 0.006) / 0.02, 0.0, 1.0) * 0.20
    if zdr is not None:
        zdr_term = 1.0 - np.clip(np.abs(zdr_data - 0.5) / 3.5, 0.0, 1.0)
        score += zdr_term * 0.05
    debris = np.clip(score, 0.0, 1.0)
    debris[(refl_data < 20.0) | (cc_data > 0.98)] = 0.0
    debris[~np.isfinite(refl_data) | ~np.isfinite(cc_data)] = np.nan
    return _make_sweep(debris, refl)


def _derive_volume_reflectivity_products(volume: VolumeData) -> tuple[SweepData, list[np.ndarray], np.ndarray] | tuple[None, None, None]:
    ref = extract_sweep(volume, "reflectivity", target_tilt=0.5, prefer_long_range=True)
    sweeps = extract_all_sweeps(volume, "reflectivity")
    if ref is None or not sweeps:
        return None, None, None

    ref_ranges_km = _range_grid_km(ref.n_gates, ref.max_range_km)
    resampled = []
    heights = []
    for sweep in sweeps:
        field = _resample_to_reference(sweep, ref.azimuth, ref_ranges_km)
        if np.isfinite(field).sum() <= 50:
            continue
        resampled.append(field)
        heights.append(_beam_height_km(ref_ranges_km, sweep.tilt_angle))

    if not resampled:
        return None, None, None
    return ref, resampled, np.asarray(heights, dtype=np.float32)


def _derive_composite_reflectivity(volume: VolumeData) -> SweepData | None:
    ref, refl_stack, _ = _derive_volume_reflectivity_products(volume)
    if ref is None:
        return None
    composite = np.nanmax(np.stack(refl_stack, axis=0), axis=0)
    return _make_sweep(composite, ref)


def _derive_echo_tops(volume: VolumeData) -> SweepData | None:
    ref, refl_stack, heights = _derive_volume_reflectivity_products(volume)
    if ref is None:
        return None

    stack = np.stack(refl_stack, axis=0)
    height_grid = np.broadcast_to(heights[:, None, :], stack.shape)
    topped = np.where(stack >= REFLECTIVITY_THRESHOLD_DBZ, height_grid, np.nan)
    echo_tops_kft = np.nanmax(topped, axis=0) * 3.28084
    echo_tops_kft[~np.isfinite(np.nanmax(stack, axis=0))] = np.nan
    return _make_sweep(np.clip(echo_tops_kft, 0.0, 70.0), ref)


def _derive_vil(volume: VolumeData) -> SweepData | None:
    ref, refl_stack, heights = _derive_volume_reflectivity_products(volume)
    if ref is None or len(refl_stack) < 1:
        return None

    # If only 1 sweep, approximate VIL as a simple column
    if len(refl_stack) == 1:
        # Single tilt - use a simplified VIL estimate
        stack = refl_stack[0]
        z_linear = 10.0 ** (np.clip(stack, 0.0, 75.0) / 10.0)
        # Approximate as 5km deep column
        liquid = 3.44e-6 * np.power(z_linear, 4.0 / 7.0) * 5000.0
        liquid[stack < REFLECTIVITY_THRESHOLD_DBZ] = 0.0
        return _make_sweep(np.clip(liquid, 0.0, 80.0), ref)

    # Multiple tilts - use full vertical integration
    stack = np.stack(refl_stack, axis=0)
    height_grid_m = np.broadcast_to(heights[:, None, :] * 1000.0, stack.shape)
    z_linear = 10.0 ** (np.clip(stack, 0.0, 75.0) / 10.0)
    liquid = 3.44e-6 * np.power(z_linear, 4.0 / 7.0)
    liquid[stack < REFLECTIVITY_THRESHOLD_DBZ] = 0.0
    vil = np.trapezoid(liquid, x=height_grid_m, axis=0)
    vil[~np.isfinite(np.nanmax(stack, axis=0))] = np.nan
    return _make_sweep(np.clip(vil, 0.0, 80.0), ref)


def _derive_velocity_texture(volume: VolumeData, target_tilt: float) -> SweepData | None:
    """Compute velocity texture - variance of velocity over a local window."""
    vel = extract_sweep(volume, "velocity", target_tilt=target_tilt, prefer_long_range=False)
    if vel is None:
        return None
    data = _to_float(vel.data)
    texture = _texture(data, size=(5, 7))
    # Scale to approximately 0-80 MPH range for texture
    texture_scaled = texture * 40.0
    return _make_sweep(np.clip(texture_scaled, 0.0, 80.0), vel)


def _derive_hail_probability(volume: VolumeData, target_tilt: float) -> SweepData | None:
    """Compute hail probability from reflectivity, ZDR, and KDP."""
    refl = extract_sweep(volume, "reflectivity", target_tilt=target_tilt, prefer_long_range=True)
    zdr = extract_sweep(volume, "differential_reflectivity", target_tilt=target_tilt)
    kdp = extract_sweep(volume, "specific_differential_phase", target_tilt=target_tilt)

    if refl is None:
        return None

    ref_data = _to_float(refl.data)
    ref_ranges_km = _range_grid_km(refl.n_gates, refl.max_range_km)

    # Resample ZDR and KDP to reference grid
    if zdr is not None:
        zdr_data = _resample_to_reference(zdr, refl.azimuth, ref_ranges_km)
    else:
        zdr_data = np.full_like(ref_data, np.nan)

    if kdp is not None:
        kdp_data = _resample_to_reference(kdp, refl.azimuth, ref_ranges_km)
    else:
        # Try deriving KDP from differential phase
        derived_kdp = _derive_kdp(volume, target_tilt)
        if derived_kdp is not None:
            kdp_data = _resample_to_reference(derived_kdp, refl.azimuth, ref_ranges_km)
        else:
            kdp_data = np.full_like(ref_data, np.nan)

    # Hail probability algorithm - enhanced with stronger reflectivity signal
    # Probability increases with reflectivity (severe storms typically 50+ dBZ)
    # and with dual-pol signatures (high ZDR, high KDP suggest large hail)
    prob = np.zeros_like(ref_data, dtype=np.float32)

    # Core: reflectivity contribution (30-65 dBZ range) - scaled to give higher probabilities
    # At 50 dBZ: ~40%, 55 dBZ: ~65%, 60 dBZ: ~85%, 65 dBZ: 100%
    refl_contrib = np.clip((ref_data - 25.0) / 40.0, 0.0, 1.0)
    prob += refl_contrib * 0.55

    # ZDR contribution (positive ZDR suggests hail, especially 2-5 dBZ)
    zdr_valid = np.isfinite(zdr_data)
    if np.any(zdr_valid):
        zdr_contrib = np.clip((zdr_data - 0.5) / 3.5, 0.0, 1.0)
        zdr_contrib[~zdr_valid] = 0.0
        prob += zdr_contrib * 0.25

    # KDP contribution (high KDP suggests large hail)
    kdp_valid = np.isfinite(kdp_data)
    if np.any(kdp_valid):
        kdp_contrib = np.clip(kdp_data / 2.5, 0.0, 1.0)
        kdp_contrib[~kdp_valid] = 0.0
        prob += kdp_contrib * 0.20

    # Suppress where reflectivity is too low (less likely to have hail)
    prob[ref_data < 25.0] = 0.0
    prob[~np.isfinite(ref_data)] = np.nan

    return _make_sweep(np.clip(prob * 100.0, 0.0, 100.0), refl)


def _derive_rainfall_rate(volume: VolumeData, target_tilt: float) -> SweepData | None:
    """Compute rainfall rate from KDP or reflectivity using Marshall-Palmer relationship."""
    # First try KDP-based rainfall (more accurate)
    kdp = extract_sweep(volume, "specific_differential_phase", target_tilt=target_tilt)
    if kdp is None:
        # Try to derive KDP first
        kdp = _derive_kdp(volume, target_tilt)

    # Also get reflectivity for fallback
    refl = extract_sweep(volume, "reflectivity", target_tilt=target_tilt, prefer_long_range=True)

    if kdp is None and refl is None:
        return None

    # If we have KDP, use it (preferred method)
    if kdp is not None:
        kdp_data = _to_float(kdp.data)
        # Rainfall rate R = 44.0 * KDP^0.822 (mm/hr) for KDP in deg/km
        rate_kdp = np.where(kdp_data > 0.1, 44.0 * np.power(np.clip(kdp_data, 0.1, 6.0), 0.822), 0.0)
        rate_kdp[~np.isfinite(kdp_data)] = np.nan
        # Convert mm/hr to inches/hr
        rate_kdp = rate_kdp * 0.03937
        return _make_sweep(np.clip(rate_kdp, 0.0, 8.0), kdp)

    # Fallback: use reflectivity-based rainfall (Marshall-Palmer: Z = 200 * R^1.6)
    # So R = (Z/200)^(1/1.6)
    ref_data = _to_float(refl.data)
    # Apply smoothing first to reduce noise
    ref_smooth = _nan_box_filter(ref_data, size=(3, 5))
    # Convert dBZ to linear Z, then to rainfall rate
    z_linear = np.power(10.0, ref_smooth / 10.0)
    # Use higher threshold (20 dBZ) to reduce noise
    rate_refl = np.where(ref_smooth > 20.0, np.power(z_linear / 200.0, 1.0 / 1.6), 0.0)
    rate_refl[~np.isfinite(ref_smooth)] = np.nan
    # Scale to be more visible and convert to inches/hr
    rate_refl = rate_refl * 10.0 * 0.03937

    return _make_sweep(np.clip(rate_refl, 0.0, 8.0), refl)


DERIVED_PRODUCT_FUNCS = {
    "specific_differential_phase": lambda volume, tilt: _derive_kdp(volume, tilt),
    "storm_relative_velocity": lambda volume, tilt: _derive_srm(volume, tilt),
    "azimuthal_shear": lambda volume, tilt: _derive_azimuthal_shear(volume, tilt),
    "divergence": lambda volume, tilt: _derive_divergence(volume, tilt),
    "cc_texture": lambda volume, tilt: _derive_cc_texture(volume, tilt),
    "phi_texture": lambda volume, tilt: _derive_phi_texture(volume, tilt),
    "debris_flag": lambda volume, tilt: _derive_debris_flag(volume, tilt),
    "composite_reflectivity": lambda volume, tilt: _derive_composite_reflectivity(volume),
    "echo_tops": lambda volume, tilt: _derive_echo_tops(volume),
    "vertically_integrated_liquid": lambda volume, tilt: _derive_vil(volume),
    "velocity_texture": lambda volume, tilt: _derive_velocity_texture(volume, tilt),
    "hail_probability": lambda volume, tilt: _derive_hail_probability(volume, tilt),
    "rainfall_rate": lambda volume, tilt: _derive_rainfall_rate(volume, tilt),
}


def extract_radar_product(
    volume: VolumeData,
    field_name: str,
    target_tilt: float = 0.5,
    prefer_long_range: bool = True,
) -> SweepData | None:
    """Return either a raw sweep or a derived radar product."""
    if field_name in RAW_LEVEL2_FIELDS:
        return extract_sweep(
            volume,
            field_name,
            target_tilt=target_tilt,
            prefer_long_range=prefer_long_range,
        )
    func = DERIVED_PRODUCT_FUNCS.get(field_name)
    if func is None:
        return None
    return func(volume, target_tilt)
