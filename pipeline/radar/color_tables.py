"""Professional color tables for Level II radar products.

256-level smooth gradient LUTs matching RadarScope/GR2 conventions.
Each function returns (lut, vmin, vmax) where lut is (256, 4) uint8 RGBA.
"""

import numpy as np


def _lerp_colors(breakpoints: list[tuple[float, tuple]],
                 vmin: float, vmax: float, n: int = 256) -> np.ndarray:
    """Build an Nx4 RGBA uint8 LUT from breakpoint list."""
    lut = np.zeros((n, 4), dtype=np.uint8)
    for i in range(n):
        val = vmin + (vmax - vmin) * i / (n - 1)
        if val <= breakpoints[0][0]:
            lut[i] = breakpoints[0][1]
            continue
        if val >= breakpoints[-1][0]:
            lut[i] = breakpoints[-1][1]
            continue
        for j in range(len(breakpoints) - 1):
            v0, c0 = breakpoints[j]
            v1, c1 = breakpoints[j + 1]
            if v0 <= val <= v1:
                t = (val - v0) / (v1 - v0) if v1 != v0 else 0
                lut[i] = tuple(int(c0[k] + t * (c1[k] - c0[k])) for k in range(4))
                break
    return lut


# NWS WSR-88D reflectivity — sourced from the shared versioned palette
# registry so radar and model reflectivity stay identical.
from pipeline.palettes import reflectivity_table as _reflectivity_table

REFLECTIVITY_BOUNDS, REFLECTIVITY_HEX = _reflectivity_table()


def _hex_to_rgba(hex_color: str) -> tuple[int, int, int, int]:
    return (
        int(hex_color[1:3], 16),
        int(hex_color[3:5], 16),
        int(hex_color[5:7], 16),
        255,
    )


REFLECTIVITY_COLORS = [_hex_to_rgba(c) for c in REFLECTIVITY_HEX]


def reflectivity_lut() -> tuple[np.ndarray, float, float]:
    """NWS reflectivity palette with smooth interpolation, -20 to 80 dBZ."""
    vmin, vmax = -20.0, 80.0
    bp = [(float(b), c) for b, c in zip(REFLECTIVITY_BOUNDS[:-1], REFLECTIVITY_COLORS)]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def velocity_lut() -> tuple[np.ndarray, float, float]:
    """RadarScope velocity: inbound (negative)=green, outbound (positive)=red."""
    # All values in MPH (converted from m/s: * 2.237)
    vmin, vmax = -89.5, 89.5  # -40 to 40 m/s converted to MPH
    bp = [
        (-89.5, (  0, 100,  50, 255)),   # dark green (strong inbound)
        (-67.1, (  0, 160,   0, 255)),   # deep green
        (-44.7, (  0, 220,   0, 255)),   # bright green
        (-33.6, ( 50, 240,  50, 255)),   # light green
        (-22.4, (130, 255, 130, 255)),   # pale green
        (-11.2, (200, 240, 200, 255)),   # very pale green
        ( -2.2, (180, 180, 180, 100)),   # dim gray
        (  0.0, (  0,   0,   0,   0)),   # zero = transparent
        (  2.2, (180, 180, 180, 100)),   # dim gray
        ( 11.2, (240, 200, 200, 255)),   # very pale red
        ( 22.4, (255, 130, 130, 255)),   # pale red
        ( 33.6, (240,  50,  50, 255)),   # light red
        ( 44.7, (220,   0,   0, 255)),   # bright red
        ( 67.1, (160,   0,   0, 255)),   # deep red
        ( 89.5, (100,   0,  50, 255)),   # dark maroon (strong outbound)
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def spectrum_width_lut() -> tuple[np.ndarray, float, float]:
    """Spectrum width, 0 to 20 m/s."""
    vmin, vmax = 0.0, 20.0
    bp = [
        ( 0, (  0,   0,   0,   0)),
        ( 1, (  0,  40,  80, 200)),   # dark blue
        ( 3, (  0, 100, 200, 255)),   # blue
        ( 5, (  0, 200, 220, 255)),   # cyan
        ( 7, (  0, 200, 100, 255)),   # teal-green
        (10, (255, 255,   0, 255)),   # yellow
        (13, (255, 160,   0, 255)),   # orange
        (16, (255,   0,   0, 255)),   # red
        (20, (255,   0, 255, 255)),   # magenta
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def zdr_lut() -> tuple[np.ndarray, float, float]:
    """Differential reflectivity, -2 to +6 dB."""
    vmin, vmax = -2.0, 6.0
    bp = [
        (-2.0, (180,   0, 255, 255)),  # violet (negative)
        (-1.0, ( 80,   0, 200, 255)),  # dark purple
        (-0.2, (  0,  80, 180, 255)),  # blue
        ( 0.0, ( 80, 100, 120, 200)),  # steel gray
        ( 0.3, (  0, 180, 200, 255)),  # teal
        ( 1.0, (  0, 200,   0, 255)),  # green
        ( 2.0, (255, 255,   0, 255)),  # yellow
        ( 3.0, (255, 180,   0, 255)),  # orange
        ( 4.0, (255,  50,   0, 255)),  # red
        ( 5.0, (200,   0, 100, 255)),  # magenta
        ( 6.0, (255, 255, 255, 255)),  # white
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def rhohv_lut() -> tuple[np.ndarray, float, float]:
    """Correlation coefficient, 0.2 to 1.05.

    Inverted emphasis: high CC (>0.97) = pure rain/snow (muted).
    Low CC (<0.85) = mixed phase, debris, clutter (vivid warning colors).
    """
    vmin, vmax = 0.20, 1.05
    bp = [
        (0.20, (  0,   0,   0,   0)),  # transparent (noise floor)
        (0.45, (100,   0, 120, 255)),  # dark purple (non-met)
        (0.60, (180,   0, 255, 255)),  # vivid purple (debris/clutter)
        (0.70, (255,   0,   0, 255)),  # red (mixed phase / tornado debris)
        (0.80, (255, 130,   0, 255)),  # orange (melting / hail)
        (0.85, (255, 220,   0, 255)),  # yellow (mixed precip)
        (0.90, (  0, 220,   0, 255)),  # green (light rain)
        (0.93, ( 50, 200, 100, 255)),  # green-teal
        (0.95, (  0, 180, 180, 255)),  # teal (rain)
        (0.97, ( 80, 140, 200, 255)),  # steel blue (pure rain)
        (0.99, (140, 170, 220, 255)),  # light blue (clean signal)
        (1.00, (200, 210, 230, 255)),  # pale blue-gray
        (1.05, (230, 230, 240, 255)),  # near-white
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def phidp_lut() -> tuple[np.ndarray, float, float]:
    """Differential phase, 0 to 180 degrees."""
    vmin, vmax = 0.0, 180.0
    bp = [
        (  0, (120,  50, 220, 255)),   # purple
        ( 20, (  0,   0, 255, 255)),   # blue
        ( 40, (  0, 180, 255, 255)),   # cyan
        ( 60, (  0, 200, 100, 255)),   # teal
        ( 80, (  0, 220,   0, 255)),   # green
        (100, (255, 255,   0, 255)),   # yellow
        (120, (255, 180,   0, 255)),   # orange
        (140, (255,  60,   0, 255)),   # red-orange
        (160, (255,   0,   0, 255)),   # red
        (180, (200,   0, 100, 255)),   # magenta
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def kdp_lut() -> tuple[np.ndarray, float, float]:
    """Specific differential phase, -1 to 8 deg/km."""
    vmin, vmax = -1.0, 8.0
    bp = [
        (-1.0, ( 40,  40, 120, 160)),
        ( 0.0, (  0,   0,   0,   0)),
        ( 0.2, (  0, 140, 255, 255)),
        ( 0.8, (  0, 220, 180, 255)),
        ( 1.5, ( 80, 220,   0, 255)),
        ( 2.5, (255, 240,   0, 255)),
        ( 4.0, (255, 150,   0, 255)),
        ( 6.0, (255,  40,   0, 255)),
        ( 8.0, (255, 255, 255, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def shear_lut() -> tuple[np.ndarray, float, float]:
    """Azimuthal shear, 0 to 0.05 s^-1."""
    vmin, vmax = 0.0, 0.05
    bp = [
        (0.00, (  0,   0,   0,   0)),
        (0.003, ( 80, 120, 255, 180)),
        (0.008, (  0, 220, 180, 255)),
        (0.015, (255, 255,   0, 255)),
        (0.025, (255, 120,   0, 255)),
        (0.035, (255,   0,   0, 255)),
        (0.050, (255, 255, 255, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def divergence_lut() -> tuple[np.ndarray, float, float]:
    """Signed convergence/divergence, -12 to 12 m/s per km."""
    vmin, vmax = -12.0, 12.0
    bp = [
        (-12.0, (  0, 120,   0, 255)),
        ( -8.0, (  0, 200,   0, 255)),
        ( -4.0, (130, 255, 130, 255)),
        ( -1.0, (220, 220, 220, 120)),
        (  0.0, (  0,   0,   0,   0)),
        (  1.0, (220, 220, 220, 120)),
        (  4.0, (255, 160, 160, 255)),
        (  8.0, (220,  30,  30, 255)),
        ( 12.0, (120,   0,   0, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def texture_lut() -> tuple[np.ndarray, float, float]:
    """Generic texture product, 0 to 1 normalized."""
    vmin, vmax = 0.0, 1.0
    bp = [
        (0.00, (  0,   0,   0,   0)),
        (0.10, ( 40, 100, 200, 180)),
        (0.25, (  0, 180, 255, 255)),
        (0.45, (  0, 220, 140, 255)),
        (0.65, (255, 220,   0, 255)),
        (0.85, (255, 100,   0, 255)),
        (1.00, (255, 255, 255, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def phi_texture_lut() -> tuple[np.ndarray, float, float]:
    """Differential phase texture, 0 to 60 degrees."""
    vmin, vmax = 0.0, 60.0
    bp = [
        ( 0.0, (  0,   0,   0,   0)),
        ( 4.0, ( 70, 110, 220, 180)),
        (10.0, (  0, 200, 255, 255)),
        (20.0, (  0, 220, 120, 255)),
        (30.0, (255, 220,   0, 255)),
        (45.0, (255, 100,   0, 255)),
        (60.0, (255, 255, 255, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def debris_lut() -> tuple[np.ndarray, float, float]:
    """Debris confidence score, 0 to 1."""
    vmin, vmax = 0.0, 1.0
    bp = [
        (0.00, (  0,   0,   0,   0)),
        (0.20, (255, 255,   0, 200)),
        (0.40, (255, 170,   0, 255)),
        (0.60, (255,  60,   0, 255)),
        (0.80, (255,   0, 120, 255)),
        (1.00, (255, 255, 255, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def echo_tops_lut() -> tuple[np.ndarray, float, float]:
    """Echo tops, 0 to 70 kft."""
    vmin, vmax = 0.0, 70.0
    bp = [
        ( 0.0, (  0,   0,   0,   0)),
        (10.0, (  0, 140, 255, 200)),
        (20.0, (  0, 220, 180, 255)),
        (30.0, (120, 220,   0, 255)),
        (40.0, (255, 220,   0, 255)),
        (50.0, (255, 130,   0, 255)),
        (60.0, (255,   0,   0, 255)),
        (70.0, (255, 255, 255, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def vil_lut() -> tuple[np.ndarray, float, float]:
    """Vertically integrated liquid, 0 to 80 kg/m^2."""
    vmin, vmax = 0.0, 80.0
    bp = [
        ( 0.0, (  0,   0,   0,   0)),
        ( 5.0, (  0, 140, 255, 180)),
        (15.0, (  0, 220, 200, 255)),
        (25.0, (120, 220,   0, 255)),
        (40.0, (255, 220,   0, 255)),
        (55.0, (255, 140,   0, 255)),
        (70.0, (255,   0,   0, 255)),
        (80.0, (255, 255, 255, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def velocity_texture_lut() -> tuple[np.ndarray, float, float]:
    """Velocity texture, 0 to 80 MPH."""
    vmin, vmax = 0.0, 80.0
    bp = [
        ( 0.0, (  0,   0,   0,   0)),
        (10.0, (  0, 100, 255, 180)),
        (20.0, (  0, 220, 180, 255)),
        (30.0, (120, 220,   0, 255)),
        (45.0, (255, 220,   0, 255)),
        (60.0, (255, 120,   0, 255)),
        (80.0, (255, 255, 255, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def hail_probability_lut() -> tuple[np.ndarray, float, float]:
    """Hail probability, 0% to 100%."""
    vmin, vmax = 0.0, 100.0
    bp = [
        ( 0.0, (  0,   0,   0,   0)),
        (10.0, (  0, 180,   0, 180)),
        (25.0, (255, 255,   0, 200)),
        (50.0, (255, 170,   0, 255)),
        (75.0, (255,  80,   0, 255)),
        (100.0, (255,   0,   0, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


def rainfall_rate_lut() -> tuple[np.ndarray, float, float]:
    """Rainfall rate, 0 to 8 inches/hr."""
    vmin, vmax = 0.0, 8.0
    bp = [
        ( 0.0, (  0,   0,   0,   0)),
        (0.1, (  0, 140, 255, 180)),
        (0.5, (  0, 220, 100, 255)),
        (1.0, (  0, 255,   0, 255)),
        (2.0, (255, 255,   0, 255)),
        (4.0, (255, 170,   0, 255)),
        (6.0, (255,  80,   0, 255)),
        (7.0, (255,   0,   0, 255)),
        (8.0, (180,   0, 180, 255)),
    ]
    return _lerp_colors(bp, vmin, vmax), vmin, vmax


# Field name → LUT builder + rendering metadata
FIELD_CONFIG = {
    "reflectivity": {
        "lut_fn": reflectivity_lut,
        "label": "Reflectivity",
        "short": "REF",
        "unit": "dBZ",
        "mask_below": 5.0,
        "prefer_long_range": True,
        "bounds": REFLECTIVITY_BOUNDS,
        "colors": REFLECTIVITY_HEX,
        "legend_ticks": [5, 15, 25, 35, 45, 55, 65, 75],
    },
    "velocity": {
        "lut_fn": velocity_lut,
        "label": "Velocity",
        "short": "VEL",
        "unit": "MPH",
        "mask_below": None,
        "prefer_long_range": False,
        "unit_scale": 2.23694,
    },
    "spectrum_width": {
        "lut_fn": spectrum_width_lut,
        "label": "Spectrum Width",
        "short": "SW",
        "unit": "m/s",
        "mask_below": 0.5,
        "prefer_long_range": False,
    },
    "differential_reflectivity": {
        "lut_fn": zdr_lut,
        "label": "Diff Reflectivity",
        "short": "ZDR",
        "unit": "dB",
        "mask_below": None,
        "prefer_long_range": False,
    },
    "cross_correlation_ratio": {
        "lut_fn": rhohv_lut,
        "label": "Correlation Coeff",
        "short": "CC",
        "unit": "",
        "mask_below": 0.2,
        "prefer_long_range": False,
    },
    "differential_phase": {
        "lut_fn": phidp_lut,
        "label": "Diff Phase",
        "short": "PHI",
        "unit": "deg",
        "mask_below": None,
        "prefer_long_range": False,
    },
    "specific_differential_phase": {
        "lut_fn": kdp_lut,
        "label": "Specific Diff Phase",
        "short": "KDP",
        "unit": "deg/km",
        "mask_below": 0.05,
        "prefer_long_range": False,
    },
    "storm_relative_velocity": {
        "lut_fn": velocity_lut,
        "label": "Storm-Relative Velocity",
        "short": "SRM",
        "unit": "MPH",
        "mask_below": None,
        "prefer_long_range": False,
        "unit_scale": 2.23694,
    },
    "azimuthal_shear": {
        "lut_fn": shear_lut,
        "label": "Azimuthal Shear",
        "short": "AZS",
        "unit": "s^-1",
        "mask_below": 0.002,
        "prefer_long_range": False,
    },
    "divergence": {
        "lut_fn": divergence_lut,
        "label": "Divergence / Convergence",
        "short": "DIV",
        "unit": "m/s per km",
        "mask_below": None,
        "prefer_long_range": False,
    },
    "cc_texture": {
        "lut_fn": texture_lut,
        "label": "CC Texture",
        "short": "CCT",
        "unit": "",
        "mask_below": 0.02,
        "prefer_long_range": False,
    },
    "phi_texture": {
        "lut_fn": phi_texture_lut,
        "label": "PHI Texture",
        "short": "PHT",
        "unit": "deg",
        "mask_below": 1.0,
        "prefer_long_range": False,
    },
    "debris_flag": {
        "lut_fn": debris_lut,
        "label": "Debris Signature",
        "short": "TDS",
        "unit": "",
        "mask_below": 0.1,
        "prefer_long_range": False,
    },
    "composite_reflectivity": {
        "lut_fn": reflectivity_lut,
        "label": "Composite Reflectivity",
        "short": "CREF",
        "unit": "dBZ",
        "mask_below": 5.0,
        "prefer_long_range": True,
        "bounds": REFLECTIVITY_BOUNDS,
        "colors": REFLECTIVITY_HEX,
        "legend_ticks": [5, 15, 25, 35, 45, 55, 65, 75],
    },
    "echo_tops": {
        "lut_fn": echo_tops_lut,
        "label": "Echo Tops",
        "short": "ETOP",
        "unit": "kft",
        "mask_below": 1.0,
        "prefer_long_range": True,
    },
    "vertically_integrated_liquid": {
        "lut_fn": vil_lut,
        "label": "Vertically Integrated Liquid",
        "short": "VIL",
        "unit": "kg/m^2",
        "mask_below": 1.0,
        "prefer_long_range": True,
    },
    "velocity_texture": {
        "lut_fn": velocity_texture_lut,
        "label": "Velocity Texture",
        "short": "VXT",
        "unit": "MPH",
        "mask_below": 1.0,
        "prefer_long_range": False,
    },
    "hail_probability": {
        "lut_fn": hail_probability_lut,
        "label": "Hail Probability",
        "short": "HAIL",
        "unit": "%",
        "mask_below": 1.0,
        "prefer_long_range": True,
    },
    "rainfall_rate": {
        "lut_fn": rainfall_rate_lut,
        "label": "Rainfall Rate",
        "short": "RAIN",
        "unit": "in/hr",
        "mask_below": 0.5,
        "prefer_long_range": False,
    },
}
