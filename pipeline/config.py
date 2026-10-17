"""Central configuration for the weather-viewer pipeline."""
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Custom colormaps — registered at import time so both renderer and web app
# pick them up automatically.
# ---------------------------------------------------------------------------
def _register_custom_cmaps():
    try:
        import matplotlib.cm as _cm
        from matplotlib.colors import LinearSegmentedColormap as _LSC
        from pipeline.palettes import register_mpl_cmap as _register_palette_cmap

        # wx_temp: operational-standard temperature ramp (see pipeline/palettes.py).
        _register_palette_cmap("temp", "wx_temp")

        # wx_temp_upper: same operational ramp for pressure-level temps,
        # versioned separately because pre-v3 PNGs were rendered with RdBu_r.
        _register_palette_cmap("temp_upper", "wx_temp_upper")

        # wx_cin: blue inhibition ramp (weak cap light → strong cap violet).
        _register_palette_cmap("cin", "wx_cin")

        # wx_wind: surface wind — clean green→yellow→orange→red→purple ramp.
        # Subtle tint at low speeds, vivid at high speeds for immediate readability.
        _wx_wind = _LSC.from_list("wx_wind", [
            (0.000, "#e8f5e9"),  # calm      faintest green tint
            (0.120, "#a5d6a7"),  # 5 mph     light green
            (0.250, "#66bb6a"),  # 10 mph    green
            (0.380, "#43a047"),  # 15 mph    medium green
            (0.500, "#c0ca33"),  # 20 mph    lime/chartreuse
            (0.600, "#fdd835"),  # 25 mph    golden yellow
            (0.700, "#fb8c00"),  # 30 mph    orange
            (0.800, "#e53935"),  # 40 mph    red
            (0.900, "#b71c1c"),  # 50 mph    dark red
            (1.000, "#7b1fa2"),  # 60+ mph   deep purple
        ])
        try:
            import matplotlib
            matplotlib.colormaps.register(_wx_wind, name="wx_wind", force=True)
        except Exception:
            _cm.register_cmap(name="wx_wind", cmap=_wx_wind)

        # wx_wind_aloft: pressure-level isotach ramp.
        # Smooth blue→teal→green→yellow→orange→red→magenta progression.
        # Avoids the harsh cyan-to-yellow jump for cleaner map readability.
        _wx_wind_aloft = _LSC.from_list("wx_wind_aloft", [
            (0.000, "#c6dbef"),  # pale steel blue — light winds
            (0.100, "#6baed6"),  # medium blue
            (0.200, "#2171b5"),  # strong blue
            (0.300, "#08519c"),  # deep navy blue
            (0.400, "#238b45"),  # forest green — moderate flow
            (0.500, "#74c476"),  # spring green
            (0.600, "#f7dc6f"),  # warm gold
            (0.700, "#f5a623"),  # deep orange
            (0.800, "#e74c3c"),  # vivid red — strong flow
            (0.900, "#c0392b"),  # dark red
            (0.950, "#8e44ad"),  # rich purple — jet core
            (1.000, "#d2b4de"),  # light mauve — extreme
        ])
        try:
            import matplotlib
            matplotlib.colormaps.register(_wx_wind_aloft, name="wx_wind_aloft", force=True)
        except Exception:
            _cm.register_cmap(name="wx_wind_aloft", cmap=_wx_wind_aloft)

        # wx_omega: 700 mb vertical velocity.
        # Negative omega/ascent uses cool purple-blue shades; near-zero is
        # intentionally light; positive omega/subsidence uses warm amber-brown.
        _wx_omega = _LSC.from_list("wx_omega", [
            (0.000, "#4c1d95"),  # -8 Pa/s strong ascent
            (0.155, "#7e22ce"),  # -6
            (0.308, "#2563eb"),  # -4
            (0.462, "#38bdf8"),  # -2
            (0.577, "#e0f2fe"),  # -0.5
            (0.615, "#f8fafc"),  #  0
            (0.692, "#fef3c7"),  #  1
            (0.769, "#f59e0b"),  #  2
            (0.923, "#c2410c"),  #  4
            (1.000, "#7c2d12"),  #  5 Pa/s strong subsidence
        ])
        try:
            import matplotlib
            matplotlib.colormaps.register(_wx_omega, name="wx_omega", force=True)
        except Exception:
            _cm.register_cmap(name="wx_omega", cmap=_wx_omega)

        # wx_cloud: grayscale cloud cover ramp that avoids pure white so
        # dense cloud fields remain readable on the basemap.
        _wx_cloud = _LSC.from_list("wx_cloud", [
            (0.000, "#161616"),  # 0%     very dark gray
            (0.180, "#2b2b2b"),  # 20%    dark gray
            (0.380, "#4a4a4a"),  # 40%    charcoal
            (0.580, "#717171"),  # 60%    mid gray
            (0.780, "#9a9a9a"),  # 80%    soft gray
            (1.000, "#c6c6c6"),  # 100%   light gray, not white
        ])
        try:
            import matplotlib
            matplotlib.colormaps.register(_wx_cloud, name="wx_cloud", force=True)
        except Exception:
            _cm.register_cmap(name="wx_cloud", cmap=_wx_cloud)
        # wx_dewpoint: moisture ramp — dry tan/brown through green into deep blue.
        # Intuitively reads as "dry → moist → tropical" at a glance.
        _wx_dewpoint = _LSC.from_list("wx_dewpoint", [
            (0.000, "#8c6b3e"),  # -20F  dry brown
            (0.100, "#b8956a"),  # -10F  tan
            (0.200, "#d4c09a"),  #   0F  pale sand
            (0.300, "#c5cfa3"),  #  10F  sage
            (0.400, "#8fbc6a"),  #  20F  soft green
            (0.500, "#4caf50"),  #  30F  green
            (0.600, "#2e7d32"),  #  40F  dark green
            (0.700, "#26a69a"),  #  50F  teal
            (0.800, "#0288d1"),  #  60F  blue
            (0.900, "#1565c0"),  #  65F  deep blue
            (1.000, "#4a148c"),  #  75F  deep purple — tropical
        ])
        try:
            matplotlib.colormaps.register(_wx_dewpoint, name="wx_dewpoint", force=True)
        except Exception:
            _cm.register_cmap(name="wx_dewpoint", cmap=_wx_dewpoint)

        # wx_heights: clean, subtle continuous ramp for geopotential heights.
        # Designed to be a quiet background field that doesn't compete with
        # overlaid wind barbs or contours. Low=cool tones, high=warm tones.
        _wx_heights = _LSC.from_list("wx_heights", [
            (0.000, "#2c3e7a"),  # deep blue — low heights (troughs)
            (0.200, "#4a7fb5"),  # medium blue
            (0.350, "#7bafd4"),  # sky blue
            (0.500, "#b8d4e3"),  # pale blue-gray — neutral
            (0.650, "#e8d5a3"),  # warm tan
            (0.800, "#d4915e"),  # copper
            (0.900, "#b85636"),  # terra cotta — high heights (ridges)
            (1.000, "#8b2d1a"),  # deep rust
        ])
        try:
            matplotlib.colormaps.register(_wx_heights, name="wx_heights", force=True)
        except Exception:
            _cm.register_cmap(name="wx_heights", cmap=_wx_heights)

        # wx_shear: severity ramp for wind shear — quiet at low values,
        # increasingly vivid as shear becomes operationally significant.
        _wx_shear = _LSC.from_list("wx_shear", [
            (0.000, "#d4e6f1"),  # faint blue — negligible shear
            (0.200, "#85c1e9"),  # light blue
            (0.400, "#27ae60"),  # green — moderate
            (0.550, "#f1c40f"),  # yellow — elevated
            (0.700, "#e67e22"),  # orange — significant
            (0.850, "#e74c3c"),  # red — extreme
            (1.000, "#7d1a6e"),  # deep magenta — off the charts
        ])
        try:
            matplotlib.colormaps.register(_wx_shear, name="wx_shear", force=True)
        except Exception:
            _cm.register_cmap(name="wx_shear", cmap=_wx_shear)

        # wx_snow: cool white-blue-purple ramp for snow depth/accumulation.
        # Reads intuitively as "light dusting → heavy accumulation."
        _wx_snow = _LSC.from_list("wx_snow", [
            (0.000, "#e8f0f8"),  # near-white blue — trace
            (0.150, "#b3d4f0"),  # pale blue
            (0.300, "#7bb3e0"),  # sky blue
            (0.450, "#4a90d9"),  # medium blue
            (0.600, "#3366c4"),  # strong blue
            (0.750, "#5e35b1"),  # violet — heavy
            (0.900, "#7b1fa2"),  # purple
            (1.000, "#4a0072"),  # deep purple — extreme
        ])
        try:
            matplotlib.colormaps.register(_wx_snow, name="wx_snow", force=True)
        except Exception:
            _cm.register_cmap(name="wx_snow", cmap=_wx_snow)

        # wx_rh: relative humidity ramp — dry warm tones to moist cool tones.
        _wx_rh = _LSC.from_list("wx_rh", [
            (0.000, "#8d6e3f"),  # dry brown — 0%
            (0.200, "#c4a265"),  # tan
            (0.400, "#e8d5a3"),  # pale sand
            (0.500, "#a8d5a2"),  # soft green — 50%
            (0.650, "#4caf50"),  # green
            (0.800, "#2196f3"),  # blue — 80%
            (0.900, "#1565c0"),  # deep blue — saturated
            (1.000, "#0d47a1"),  # navy — 100%
        ])
        try:
            matplotlib.colormaps.register(_wx_rh, name="wx_rh", force=True)
        except Exception:
            _cm.register_cmap(name="wx_rh", cmap=_wx_rh)

        # wx_cape: instability ramp — muted at low values, vivid at extreme.
        # Yellow→orange→red→magenta reads as escalating danger.
        _wx_cape = _LSC.from_list("wx_cape", [
            (0.000, "#fff9c4"),  # pale yellow — weak instability
            (0.150, "#fff176"),  # yellow
            (0.300, "#ffb74d"),  # light orange
            (0.450, "#ff9800"),  # orange — moderate
            (0.600, "#f44336"),  # red — strong
            (0.750, "#c62828"),  # dark red — extreme
            (0.900, "#880e4f"),  # deep magenta
            (1.000, "#4a0072"),  # purple — catastrophic
        ])
        try:
            matplotlib.colormaps.register(_wx_cape, name="wx_cape", force=True)
        except Exception:
            _cm.register_cmap(name="wx_cape", cmap=_wx_cape)

        # wx_visibility: low visibility pops immediately, while high visibility
        # fades into a cool pale tone instead of disappearing entirely.
        _wx_visibility = _LSC.from_list("wx_visibility", [
            (0.000, "#4b0016"),  # near zero — dense fog / blizzard
            (0.120, "#8c1d18"),  # very poor
            (0.250, "#d94801"),  # poor
            (0.420, "#f16913"),  # reduced
            (0.560, "#fdcc8a"),  # marginal
            (0.700, "#d9f0a3"),  # fair
            (0.840, "#78c679"),  # good
            (0.930, "#67a9cf"),  # very good
            (1.000, "#dbe9f6"),  # unrestricted — pale blue-gray
        ])
        try:
            matplotlib.colormaps.register(_wx_visibility, name="wx_visibility", force=True)
        except Exception:
            _cm.register_cmap(name="wx_visibility", cmap=_wx_visibility)

        # wx_probability: clean 0-100% ramp for ensemble probabilities.
        # Cool to warm reads intuitively as "unlikely → certain."
        _wx_probability = _LSC.from_list("wx_probability", [
            (0.000, "#e3f2fd"),  # near-white blue — negligible
            (0.200, "#90caf9"),  # light blue
            (0.400, "#4caf50"),  # green — moderate
            (0.600, "#fdd835"),  # yellow — likely
            (0.800, "#ff6f00"),  # deep orange — very likely
            (0.900, "#d32f2f"),  # red — near certain
            (1.000, "#b71c1c"),  # dark red — certain
        ])
        try:
            matplotlib.colormaps.register(_wx_probability, name="wx_probability", force=True)
        except Exception:
            _cm.register_cmap(name="wx_probability", cmap=_wx_probability)

        # wx_enhanced_ir: Enhanced IR colormap for satellite brightness temperature.
        # Maps IR temperature range -110°C (pos 0) to +50°C (pos 1) with color
        # enhancement at cold cloud-top temperatures for convective analysis.
        # wx_enhanced_ir: conventional rainbow IR enhancement (palettes.py).
        _register_palette_cmap("ir_enhanced", "wx_enhanced_ir")

        # wx_wv: conventional water-vapor enhancement (palettes.py).
        _register_palette_cmap("wv", "wx_wv")

        # wx_cti: Chase Target Index — vivid escalating threat ramp.
        _wx_cti = _LSC.from_list("wx_cti", [
            (0.000, "#1a1a2e"),  # near-invisible dark blue-gray
            (0.080, "#16537e"),  # dark teal — marginal
            (0.180, "#2e8b57"),  # sea green — slight
            (0.300, "#48c946"),  # vivid green — moderate
            (0.400, "#a8e034"),  # chartreuse — enhanced
            (0.500, "#fdd835"),  # golden yellow — significant
            (0.620, "#ff9800"),  # orange — high
            (0.740, "#f44336"),  # red — extreme
            (0.860, "#d500f9"),  # magenta — violent
            (1.000, "#6a1b9a"),  # deep purple — historic
        ])
        try:
            matplotlib.colormaps.register(_wx_cti, name="wx_cti", force=True)
        except Exception:
            _cm.register_cmap(name="wx_cti", cmap=_wx_cti)

        # wx_msi: Mesocyclone Strength — navy→teal→green→gold→crimson→magenta
        _wx_msi = _LSC.from_list("wx_msi", [
            (0.000, "#0d1b2a"),
            (0.120, "#1b3a5c"),
            (0.250, "#1a7a6d"),
            (0.400, "#2db84b"),
            (0.550, "#c9b71e"),
            (0.700, "#e8751a"),
            (0.850, "#c62828"),
            (1.000, "#ad1457"),
        ])
        try:
            matplotlib.colormaps.register(_wx_msi, name="wx_msi", force=True)
        except Exception:
            _cm.register_cmap(name="wx_msi", cmap=_wx_msi)

        # wx_smi: Storm Mode — diverging: purple (linear) → gray → green (discrete)
        _wx_smi = _LSC.from_list("wx_smi", [
            (0.000, "#4a148c"),   # deep purple — strongly linear
            (0.150, "#7b1fa2"),   # purple
            (0.300, "#9575cd"),   # lavender — linear-leaning
            (0.450, "#90a4ae"),   # blue-gray — ambiguous
            (0.550, "#a5d6a7"),   # pale green — discrete-leaning
            (0.700, "#43a047"),   # green — discrete supercells
            (0.850, "#f9a825"),   # gold — strongly discrete
            (1.000, "#ff6f00"),   # deep orange — extreme discrete
        ])
        try:
            matplotlib.colormaps.register(_wx_smi, name="wx_smi", force=True)
        except Exception:
            _cm.register_cmap(name="wx_smi", cmap=_wx_smi)

        # wx_ship: Sig Hail — green→yellow→orange→red→white-hot
        _wx_ship = _LSC.from_list("wx_ship", [
            (0.000, "#1b3a1b"),
            (0.150, "#2e7d32"),
            (0.300, "#8bc34a"),
            (0.450, "#fdd835"),
            (0.600, "#ff9800"),
            (0.750, "#e53935"),
            (0.900, "#b71c1c"),
            (1.000, "#f8bbd0"),   # pale pink — extreme
        ])
        try:
            matplotlib.colormaps.register(_wx_ship, name="wx_ship", force=True)
        except Exception:
            _cm.register_cmap(name="wx_ship", cmap=_wx_ship)

        # wx_dcp: Derecho — steel blue→teal→green→gold→orange→crimson
        _wx_dcp = _LSC.from_list("wx_dcp", [
            (0.000, "#1a2740"),
            (0.150, "#37474f"),
            (0.300, "#00695c"),
            (0.450, "#388e3c"),
            (0.600, "#c0ca33"),
            (0.750, "#ff8f00"),
            (0.900, "#c62828"),
            (1.000, "#880e4f"),
        ])
        try:
            matplotlib.colormaps.register(_wx_dcp, name="wx_dcp", force=True)
        except Exception:
            _cm.register_cmap(name="wx_dcp", cmap=_wx_dcp)

        # wx_citi: CI Timing — cool blue→cyan→green→yellow→orange→red
        _wx_citi = _LSC.from_list("wx_citi", [
            (0.000, "#0d1b2a"),
            (0.150, "#1565c0"),
            (0.300, "#00acc1"),
            (0.450, "#43a047"),
            (0.600, "#c0ca33"),
            (0.750, "#fdd835"),
            (0.900, "#ff6f00"),
            (1.000, "#d32f2f"),
        ])
        try:
            matplotlib.colormaps.register(_wx_citi, name="wx_citi", force=True)
        except Exception:
            _cm.register_cmap(name="wx_citi", cmap=_wx_citi)

        # wx_cvi: Chase Visibility — brown→tan→green→cyan
        _wx_cvi = _LSC.from_list("wx_cvi", [
            (0.000, "#3e2723"),   # dark brown — can't see anything
            (0.200, "#795548"),   # brown
            (0.400, "#a1887f"),   # tan
            (0.550, "#aed581"),   # pale green
            (0.700, "#4caf50"),   # green — good
            (0.850, "#26c6da"),   # cyan — excellent
            (1.000, "#e0f7fa"),   # bright cyan-white — perfect
        ])
        try:
            matplotlib.colormaps.register(_wx_cvi, name="wx_cvi", force=True)
        except Exception:
            _cm.register_cmap(name="wx_cvi", cmap=_wx_cvi)

    except Exception:
        pass  # matplotlib not available at config import time

_register_custom_cmaps()

BASE_DIR = Path(os.environ.get("WX_BASE_DIR", Path(__file__).resolve().parent.parent)).expanduser()
DATA_DIR = BASE_DIR / "data"
GRIB_CACHE_DIR = DATA_DIR / "grib_cache"
RENDERED_DIR = DATA_DIR / "rendered"
LOG_DIR = BASE_DIR / "logs"

# Retention policy: number of complete runs to keep
RETENTION = {
    "full_rendered": int(os.environ.get("WX_FULL_RENDERED_RUNS", "6")),
    "partial_rendered": 0,  # keep only PARTIAL_PARAMS for older runs
    "grib_cache": int(os.environ.get("WX_GRIB_CACHE_RUNS", "2")),
}

# Parameters kept for partial (older) runs
PARTIAL_PARAMS = ["temp_2m", "reflectivity", "precip"]

# Subhourly reflectivity settings
SUBHOURLY_CONFIG = {
    "enabled": True,           # Enable subhourly (15-min) reflectivity downloads
    "fxx_range": range(1, 7),  # Forecast hours to get subhourly for (f01-f06)
    "params": ["reflectivity", "temp_2m"],  # Parameters to process at subhourly resolution
}

# Scheduler polling
POLL_INTERVAL_SECONDS = 30

# Parallel download threads (I/O-bound)
MAX_DOWNLOAD_THREADS = 6

# Parallel render processes (CPU-bound; one per CPU core)
# Four render workers — aligned with unified_scheduler's shared pool sizing
RENDER_WORKERS = 4

# Render output resolution — 200 DPI at 3840px wide gives crisp lines at zoom
RENDER_DPI = 200

# QPF table comes from the versioned palette registry (COD/WPC-style ramp).
from pipeline.palettes import (  # noqa: E402
    CURRENT_PALETTE_VERSION,
    get_palette as _get_palette,
)

_QPF_PALETTE = _get_palette("qpf")
QPF_BOUNDS = list(_QPF_PALETTE["bounds"])
QPF_COLORS = list(_QPF_PALETTE["colors"])

SNOW_BOUNDS = [0.1, 1, 2, 3, 4, 6, 8, 12, 18, 24, 36]
SNOW_COLORS = [
    "#eef8ff", "#cfeeff", "#9bd8ff", "#63b7ff", "#2f80ed",
    "#1455c0", "#5b3fbd", "#8e2ab8", "#c2185b", "#f7f7ff",
]

CAPE_BOUNDS = [250, 500, 1000, 1500, 2000, 3000, 4000, 5000, 6500]
CAPE_COLORS = [
    "#fff7bc", "#fee391", "#fec44f", "#fe9929", "#ec7014",
    "#cc4c02", "#993404", "#662506",
]

SRH_BOUNDS = [-500, -250, -100, 0, 50, 100, 150, 200, 300, 400, 500]
SRH_COLORS = [
    "#274c77", "#6096ba", "#d7e3ec", "#f4f1de", "#f6d365",
    "#f4a261", "#e76f51", "#d62828", "#9d0208", "#5a0002",
]

SEVERE_INDEX_BOUNDS = [0.5, 1, 2, 3, 4, 6, 8, 10]
SEVERE_INDEX_COLORS = [
    "#fff7bc", "#fee391", "#fec44f", "#fe9929", "#ec7014",
    "#cc4c02", "#7f2704",
]

# Color scale definitions for each rendered parameter
COLOR_SCALES = {
    # -- Surface --
    "temp_2m": {
        "cmap": "wx_temp",
        "vmin": -30,
        "vmax": 120,
        "unit": "\u00b0F",
        "label": "2m Temperature (\u00b0F)",
    },
    "dewpoint_2m": {
        "cmap": "wx_dewpoint",
        "vmin": -20,
        "vmax": 75,
        "unit": "\u00b0F",
        "label": "2m Dewpoint (\u00b0F)",
    },
    "rh_2m": {
        "cmap": "wx_rh",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "2m Relative Humidity (%)",
    },
    "wind_speed": {
        "cmap": "wx_wind",
        "vmin": 0,
        "vmax": 60,
        "unit": "mph",
        "label": "Wind (mph)",
    },
    "wind_gusts": {
        "cmap": "wx_wind",
        "vmin": 0,
        "vmax": 80,
        "unit": "mph",
        "label": "Wind Gusts (mph)",
    },
    "mslp": {
        "cmap": None,  # contour only, no fill
        "vmin": 980,
        "vmax": 1030,
        "unit": "mb",
        "label": "MSLP (mb)",
    },
    "psfc": {
        "cmap": None,  # contour only, no fill
        "vmin": 500,
        "vmax": 1050,
        "unit": "mb",
        "label": "Surface Pressure (mb)",
    },

    # -- Pressure Levels --
    "temp_850mb": {
        "cmap": "wx_temp_upper",
        "vmin": -25,
        "vmax": 95,
        "unit": "\u00b0F",
        "label": "850mb Temperature (\u00b0F)",
        "contours": {
            "levels": [32],
            "colors": "#f8fafc",
            "linewidths": 0.7,
            "alpha": 0.7,
            "smooth_sigma": 0.7,
            "labels": True,
            "label_fmt": "32F",
        },
    },
    "temp_700mb": {
        "cmap": "wx_temp_upper",
        "vmin": -45,
        "vmax": 75,
        "unit": "\u00b0F",
        "label": "700mb Temperature (\u00b0F)",
        "contours": {
            "levels": [0, 32],
            "colors": "#f8fafc",
            "linewidths": 0.55,
            "alpha": 0.55,
            "smooth_sigma": 0.7,
        },
    },
    "temp_500mb": {
        "cmap": "wx_temp_upper",
        "vmin": -70,
        "vmax": 35,
        "unit": "\u00b0F",
        "label": "500mb Temperature (\u00b0F)",
        "contours": {
            "levels": [-40, -20, 0],
            "colors": "#f8fafc",
            "linewidths": 0.45,
            "alpha": 0.45,
            "smooth_sigma": 0.7,
        },
    },
    "gh_850mb": {
        "cmap": "wx_heights",
        "vmin": 1200,
        "vmax": 1800,
        "unit": "m",
        "label": "850mb Heights (m)",
    },
    "gh_700mb": {
        "cmap": "wx_heights",
        "vmin": 2700,
        "vmax": 3300,
        "unit": "m",
        "label": "700mb Heights (m)",
    },
    "gh_250mb": {
        "cmap": "wx_heights",
        "vmin": 9600,
        "vmax": 11000,
        "unit": "m",
        "label": "250mb Heights (m)",
    },
    "gh_925mb": {
        "cmap": "wx_heights",
        "vmin": 600,
        "vmax": 900,
        "unit": "m",
        "label": "925mb Heights (m)",
    },
    "rh_850mb": {
        "cmap": "wx_rh",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "850mb RH (%)",
    },
    "omega_700mb": {
        "cmap": "wx_omega",
        "vmin": -8,
        "vmax": 5,
        "unit": "Pa/s",
        "label": "700mb Vert Vel (Pa/s, neg=ascent)",
        "boost": False,
        "contours": [
            {
                "levels": [-8, -6, -4, -2],
                "colors": "#22d3ee",
                "linewidths": 0.92,
                "alpha": 0.96,
                "smooth_sigma": 0.9,
                "halo_width": 2.6,
                "halo_alpha": 0.72,
                "stroke_width": 1.55,
                "stroke_alpha": 0.82,
                "labels": True,
                "label_levels": [-6, -4, -2],
                "label_fontsize": 5.6,
                "label_fmt": "%g",
            },
            {
                "levels": [2, 4],
                "colors": "#7c2d12",
                "linewidths": 0.86,
                "alpha": 0.92,
                "linestyles": "dashed",
                "smooth_sigma": 0.9,
                "halo_width": 2.5,
                "halo_alpha": 0.70,
                "stroke_width": 1.45,
                "stroke_alpha": 0.78,
                "labels": True,
                "label_levels": [2, 4],
                "label_fontsize": 5.5,
                "label_fmt": "%g",
            },
        ],
    },

    # -- Precipitation --
    "reflectivity": {
        "cmap": "nws_refl",
        "vmin": -10,
        "vmax": 75,
        "unit": "dBZ",
        "label": "Composite Refl (dBZ)",
    },
    "precip": {
        "cmap": "discrete",
        "bounds": QPF_BOUNDS,
        "colors": QPF_COLORS,
        "tick_vals": [0.01, 0.10, 0.25, 0.50, 1.00, 2.00, 3.00],
        "vmin": 0.01,
        "vmax": 3.0,
        "unit": "in",
        "label": "Hourly QPF (in)",
    },

    # -- Instability --
    "cape": {
        "cmap": "discrete",
        "bounds": CAPE_BOUNDS,
        "colors": CAPE_COLORS,
        "tick_vals": [250, 1000, 2000, 3000, 4000, 5000],
        "vmin": 250,
        "vmax": 6500,
        "unit": "J/kg",
        "label": "SBCAPE (J/kg)",
    },
    "cin": {
        "cmap": "wx_cin",
        "vmin": -200,
        "vmax": 0,
        "unit": "J/kg",
        "label": "SBCIN (J/kg)",
    },
    "lifted_index": {
        "cmap": "RdBu",
        "vmin": -8,
        "vmax": 8,
        "unit": "\u00b0C",
        "label": "Lifted Index (\u00b0C)",
    },
    "stp": {
        "cmap": "discrete",
        "bounds": SEVERE_INDEX_BOUNDS,
        "colors": SEVERE_INDEX_COLORS,
        "tick_vals": [0.5, 1, 2, 3, 4, 6, 8, 10],
        "vmin": 0.5,
        "vmax": 10,
        "unit": "",
        "label": "Sig Tornado Param",
    },
    "scp": {
        "cmap": "discrete",
        "bounds": [0.5, 1, 2, 4, 8, 12, 16, 20],
        "colors": SEVERE_INDEX_COLORS,
        "tick_vals": [0.5, 1, 2, 4, 8, 12, 16, 20],
        "vmin": 0.5,
        "vmax": 20,
        "unit": "",
        "label": "Supercell Composite",
    },
    "cti": {
        "cmap": "wx_cti",
        "vmin": 0,
        "vmax": 5,
        "unit": "",
        "label": "Chase Target Index",
    },
    "msi": {
        "cmap": "wx_msi",
        "vmin": 0,
        "vmax": 5,
        "unit": "",
        "label": "Mesocyclone Strength",
    },
    "smi": {
        "cmap": "wx_smi",
        "vmin": 0,
        "vmax": 1.5,
        "unit": "",
        "label": "Storm Mode Index",
    },
    "ship": {
        "cmap": "wx_ship",
        "vmin": 0,
        "vmax": 4,
        "unit": "",
        "label": "Sig Hail Parameter",
    },
    "dcp": {
        "cmap": "wx_dcp",
        "vmin": 0,
        "vmax": 4,
        "unit": "",
        "label": "Derecho Composite",
    },
    "citi": {
        "cmap": "wx_citi",
        "vmin": 0,
        "vmax": 2,
        "unit": "",
        "label": "CI Timing Index",
    },
    "cvi": {
        "cmap": "wx_cvi",
        "vmin": 0,
        "vmax": 1,
        "unit": "",
        "label": "Chase Visibility",
    },
    "llmt": {
        "cmap": "GnBu",
        "vmin": 0,
        "vmax": 3,
        "unit": "",
        "label": "Low-Level Moisture Transport",
    },
    "ascent": {
        "cmap": "PuRd",
        "vmin": 0,
        "vmax": 3,
        "unit": "",
        "label": "Forcing for Ascent",
    },
    "hp_risk": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 3,
        "unit": "",
        "label": "Storm Obscuration / HP Risk",
    },
    "vil": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 60,
        "unit": "kg/m\u00b2",
        "label": "VIL (kg/m\u00b2)",
    },
    "uh_25km": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 300,
        "unit": "m\u00b2/s\u00b2",
        "label": "2-5km Updraft Helicity",
    },

    # -- Wind Shear --
    "srh_01km": {
        "cmap": "discrete",
        "bounds": SRH_BOUNDS,
        "colors": SRH_COLORS,
        "tick_vals": [-250, 0, 100, 200, 300, 400, 500],
        "vmin": -500,
        "vmax": 500,
        "unit": "m\u00b2/s\u00b2",
        "label": "0-1km SRH (m\u00b2/s\u00b2)",
    },
    "srh_03km": {
        "cmap": "discrete",
        "bounds": SRH_BOUNDS,
        "colors": SRH_COLORS,
        "tick_vals": [-250, 0, 100, 200, 300, 400, 500],
        "vmin": -500,
        "vmax": 500,
        "unit": "m\u00b2/s\u00b2",
        "label": "0-3km SRH (m\u00b2/s\u00b2)",
    },
    "shear_06km": {
        "cmap": "wx_shear",
        "vmin": 0,
        "vmax": 80,
        "unit": "kt",
        "label": "0-6km Shear (kt)",
    },
    "shear_01km": {
        "cmap": "wx_shear",
        "vmin": 0,
        "vmax": 50,
        "unit": "kt",
        "label": "0-1km Shear (kt)",
    },
    # -- Storm Chase --
    "mlcape": {
        "cmap": "discrete",
        "bounds": CAPE_BOUNDS,
        "colors": CAPE_COLORS,
        "tick_vals": [250, 1000, 2000, 3000, 4000, 5000],
        "vmin": 250,
        "vmax": 6500,
        "unit": "J/kg",
        "label": "MLCAPE (J/kg)",
    },
    "mucape": {
        "cmap": "discrete",
        "bounds": CAPE_BOUNDS,
        "colors": CAPE_COLORS,
        "tick_vals": [250, 1000, 2000, 3000, 4000, 5000],
        "vmin": 250,
        "vmax": 6500,
        "unit": "J/kg",
        "label": "MUCAPE (J/kg)",
    },
    "mlcin": {
        "cmap": "wx_cin",
        "vmin": -200,
        "vmax": 0,
        "unit": "J/kg",
        "label": "MLCIN (J/kg)",
    },
    "mucin": {
        "cmap": "wx_cin",
        "vmin": -200,
        "vmax": 0,
        "unit": "J/kg",
        "label": "MUCIN (J/kg)",
    },
    "cape_03km": {
        "cmap": "discrete",
        "bounds": [25, 50, 100, 150, 200, 300, 500, 750],
        "colors": ["#fff7bc", "#fee391", "#fec44f", "#fe9929", "#ec7014", "#cc4c02", "#7f2704"],
        "tick_vals": [25, 100, 200, 300, 500, 750],
        "vmin": 25,
        "vmax": 750,
        "unit": "J/kg",
        "label": "0-3km CAPE (J/kg)",
    },
    # -- Wind at pressure levels --
    "wind_850mb": {
        "cmap": "wx_wind_aloft",
        "vmin": 0,
        "vmax": 80,
        "unit": "kt",
        "label": "850mb Wind (kt)",
    },
    "wind_700mb": {
        "cmap": "wx_wind_aloft",
        "vmin": 0,
        "vmax": 90,
        "unit": "kt",
        "label": "700mb Heights / Wind (kt)",
    },
    "wind_500mb": {
        "cmap": "wx_wind_aloft",
        "vmin": 0,
        "vmax": 100,
        "unit": "kt",
        "label": "500mb Wind (kt)",
    },
    "wind_300mb": {
        "cmap": "wx_wind_aloft",
        "vmin": 0,
        "vmax": 150,
        "unit": "kt",
        "label": "300mb Wind (kt)",
    },
    "lcl": {
        "cmap": "RdYlGn_r",
        "vmin": 500,
        "vmax": 10000,
        "unit": "ft",
        "label": "LCL Height (ft AGL)",
    },
    "pwat": {
        "cmap": "YlGnBu",
        "vmin": 0,
        "vmax": 2.5,
        "unit": "in",
        "label": "PWAT (in)",
    },
    "uh_02km": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 150,
        "unit": "m\u00b2/s\u00b2",
        "label": "0-2km Updraft Helicity",
    },
    "max_wind": {
        "cmap": "wx_wind",
        "vmin": 0,
        "vmax": 80,
        "unit": "mph",
        "label": "Max 10m Wind (mph)",
    },
    "efhl": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 500,
        "unit": "m\u00b2/s\u00b2",
        "label": "Eff. Helicity (m\u00b2/s\u00b2)",
    },
    "cloud_cover": {
        "cmap": "wx_cloud",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Cloud Cover (%)",
    },
    "cloud_cover_low": {
        "cmap": "wx_cloud",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Low Cloud Cover (%)",
    },
    "cloud_cover_mid": {
        "cmap": "wx_cloud",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Mid Cloud Cover (%)",
    },
    "cloud_cover_high": {
        "cmap": "wx_cloud",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "High Cloud Cover (%)",
    },
    "lightning": {
        "cmap": "inferno",
        "vmin": 0,
        "vmax": 2,
        "unit": "flashes",
        "label": "Lightning",
    },

    # -- Winter --
    "visibility": {
        "cmap": "wx_visibility",
        "vmin": 0,
        "vmax": 8,
        "unit": "mi",
        "label": "Visibility (mi)",
    },
    "snow_depth": {
        "cmap": "discrete",
        "bounds": SNOW_BOUNDS,
        "colors": SNOW_COLORS,
        "tick_vals": [0.1, 1, 2, 4, 6, 8, 12, 18, 24],
        "vmin": 0.1,
        "vmax": 36,
        "unit": "in",
        "label": "Snow Depth (in)",
    },
    "frozen_pct": {
        "cmap": "YlGnBu",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Frozen Precip (%)",
    },
    "frzlvl": {
        "cmap": None,
        "vmin": 0,
        "vmax": 16000,
        "unit": "ft",
        "label": "Freezing Level (ft)",
        "render": "contour",
        "contours": {
            "levels": [0, 1000, 2000, 3000, 4000, 5000, 6000, 7000,
                       8000, 9000, 10000, 11000, 12000, 13000, 14000, 15000, 16000],
            "label_levels": [0, 2000, 4000, 6000, 8000, 10000, 12000, 14000, 16000],
            "colors": "#f8fafc",
            "linewidths": 0.55,
            "alpha": 0.72,
            "smooth_sigma": 0.9,
            "labels": True,
            "label_fmt": "%d",
        },
    },
    "kuchera_6hr": {
        "cmap": "discrete",
        "bounds": SNOW_BOUNDS,
        "colors": SNOW_COLORS,
        "tick_vals": [0.1, 1, 2, 4, 6, 8, 12],
        "vmin": 0.1,
        "vmax": 36,
        "unit": "in",
        "label": "6hr Kuchera Snow (in)",
    },
    "kuchera_12hr": {
        "cmap": "discrete",
        "bounds": SNOW_BOUNDS,
        "colors": SNOW_COLORS,
        "tick_vals": [0.1, 1, 2, 4, 6, 8, 12, 18],
        "vmin": 0.1,
        "vmax": 36,
        "unit": "in",
        "label": "12hr Kuchera Snow (in)",
    },
    "snow10_12hr": {
        "cmap": "discrete",
        "bounds": SNOW_BOUNDS,
        "colors": SNOW_COLORS,
        "tick_vals": [0.1, 1, 2, 4, 6, 8, 12, 18],
        "vmin": 0.1,
        "vmax": 36,
        "unit": "in",
        "label": "12hr 10:1 Snow (in)",
    },

    # -- Additional pressure levels --
    "temp_925mb": {
        "cmap": "wx_temp_upper",
        "vmin": -10,
        "vmax": 110,
        "unit": "\u00b0F",
        "label": "925mb Temperature (\u00b0F)",
        "contours": {
            "levels": [32],
            "colors": "#f8fafc",
            "linewidths": 0.7,
            "alpha": 0.7,
            "smooth_sigma": 0.7,
            "labels": True,
            "label_fmt": "32F",
        },
    },
    "temp_250mb": {
        "cmap": "wx_temp_upper",
        "vmin": -85,
        "vmax": -20,
        "unit": "\u00b0F",
        "label": "250mb Temperature (\u00b0F)",
    },
    "dewpoint_850mb": {
        "cmap": "wx_dewpoint",
        "vmin": -40,
        "vmax": 75,
        "unit": "\u00b0F",
        "label": "850mb Dewpoint (\u00b0F)",
    },
    "wind_250mb": {
        "cmap": "wx_wind_aloft",
        "vmin": 0,
        "vmax": 200,
        "unit": "kt",
        "label": "250mb Wind (kt)",
    },
    # -- Convective / Storm chasing extras --
    "echo_tops": {
        "cmap": "wx_shear",
        "vmin": 20000,
        "vmax": 60000,
        "unit": "ft",
        "label": "Echo Tops (ft)",
    },
    "hail": {
        "cmap": "inferno",
        "vmin": 0,
        "vmax": 3,
        "unit": "in",
        "label": "Hail Size (in)",
    },
    "uh_03km": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 250,
        "unit": "m\u00b2/s\u00b2",
        "label": "0-3km Updraft Helicity",
    },
    "uh_25km_swath": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 300,
        "unit": "m\u00b2/s\u00b2",
        "label": "2-5km UH Swath (Run Max)",
    },
    "uh_03km_swath": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 250,
        "unit": "m\u00b2/s\u00b2",
        "label": "0-3km UH Swath (Run Max)",
    },
    "pbl_height": {
        "cmap": "viridis",
        "vmin": 0,
        "vmax": 6000,
        "unit": "ft",
        "label": "PBL Height (ft)",
    },
    "weasd": {
        "cmap": "discrete",
        "bounds": QPF_BOUNDS,
        "colors": QPF_COLORS,
        "tick_vals": [0.01, 0.10, 0.25, 0.50, 1.00, 2.00],
        "vmin": 0.01,
        "vmax": 3.0,
        "unit": "in",
        "label": "Snow Water Equiv (in)",
    },
    # -- ECMWF IFS-specific --
    "snowfall": {
        "cmap": "discrete",
        "bounds": QPF_BOUNDS,
        "colors": QPF_COLORS,
        "tick_vals": [0.01, 0.10, 0.25, 0.50, 1.00, 2.00],
        "vmin": 0.01,
        "vmax": 3.0,
        "unit": "in",
        "label": "Snowfall (in w.e.)",
    },
    "gh_500mb": {
        "cmap": "wx_heights",
        "vmin": 4800,
        "vmax": 6000,
        "unit": "m",
        "label": "500mb Heights (m)",
    },
    "gh_300mb": {
        "cmap": "wx_heights",
        "vmin": 8400,
        "vmax": 9600,
        "unit": "m",
        "label": "300mb Heights (m)",
    },
    "rh_700mb": {
        "cmap": "wx_rh",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "700mb RH (%)",
    },
    # -- HREF/GEFS Ensemble and Probability Products --
    "refl_probability_40dbz": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob Refl >40 dBZ (%)",
    },
    "cape_probability": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob CAPE >1000 J/kg (%)",
    },
    "uh_25km_probability": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob UH 2-5km >75 (%)",
    },
    "precip_probability": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob of Precip (%)",
    },
    "precip_probability_6h_05in": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob 6h Precip >0.5\" (%)",
    },
    "precip_probability_6h_1in": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob 6h Precip >1\" (%)",
    },
    "pwat_probability_1in": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob PWAT >1\" (%)",
    },
    "pop_12h": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "12h Precip Probability (%)",
    },
    "visibility_probability": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob Low Visibility (%)",
    },
    # -- Apparent Temperature --
    "apparent_temp": {
        "cmap": "wx_temp",
        "vmin": -30,
        "vmax": 120,
        "unit": "°F",
        "label": "Apparent Temperature (°F)",
    },
    "heat_index": {
        "cmap": "wx_temp",
        "vmin": 70,
        "vmax": 130,
        "unit": "°F",
        "label": "Heat Index (°F)",
    },
    "wind_chill": {
        "cmap": "wx_temp",
        "vmin": -40,
        "vmax": 50,
        "unit": "°F",
        "label": "Wind Chill (°F)",
    },
    # -- Ceiling and Visibility --
    "ceil": {
        "cmap": "YlGnBu_r",
        "vmin": 0,
        "vmax": 20000,
        "unit": "ft",
        "label": "Cloud Ceiling (ft)",
    },
    "lfc": {
        "cmap": "RdYlGn_r",
        "vmin": 0,
        "vmax": 15000,
        "unit": "ft",
        "label": "LFC Height (ft AGL)",
    },
    # -- Soil Parameters --
    "soil_temp_0_10cm": {
        "cmap": "RdBu_r",
        "vmin": -10,
        "vmax": 110,
        "unit": "°F",
        "label": "Soil Temp 0-10cm (°F)",
    },
    "soil_moisture_0_10cm": {
        "cmap": "YlGnBu",
        "vmin": 0,
        "vmax": 1,
        "unit": "m³/m³",
        "label": "Soil Moisture 0-10cm",
    },
    # -- Wave Parameters --
    "wave_height": {
        "cmap": "YlGnBu",
        "vmin": 0,
        "vmax": 30,
        "unit": "ft",
        "label": "Significant Wave Height (ft)",
    },
    "wave_period": {
        "cmap": "viridis",
        "vmin": 0,
        "vmax": 20,
        "unit": "s",
        "label": "Wave Period (s)",
    },
    # -- Additional Pressure Levels --
    "gh_1000mb": {
        "cmap": "wx_heights",
        "vmin": -500,
        "vmax": 1500,
        "unit": "m",
        "label": "1000mb Heights (m)",
    },
    "gh_200mb": {
        "cmap": "wx_heights",
        "vmin": 11400,
        "vmax": 12600,
        "unit": "m",
        "label": "200mb Heights (m)",
    },
    "gh_925mb": {
        "cmap": "wx_heights",
        "vmin": 600,
        "vmax": 900,
        "unit": "m",
        "label": "925mb Heights (m)",
    },
    "temp_200mb": {
        "cmap": "wx_temp_upper",
        "vmin": -85,
        "vmax": -25,
        "unit": "°F",
        "label": "200mb Temperature (°F)",
    },
    "wind_200mb": {
        "cmap": "wx_wind_aloft",
        "vmin": 0,
        "vmax": 150,
        "unit": "kt",
        "label": "200mb Wind (kt)",
    },
    "wind_925mb": {
        "cmap": "wx_wind_aloft",
        "vmin": 0,
        "vmax": 80,
        "unit": "kt",
        "label": "925mb Wind (kt)",
    },
    "rh_925mb": {
        "cmap": "wx_rh",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "925mb RH (%)",
    },
    "rh_500mb": {
        "cmap": "wx_rh",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "500mb RH (%)",
    },
    "vort_500mb": {
        "cmap": "YlOrRd",
        "vmin": 4,
        "vmax": 30,
        "unit": "10⁻⁵ s⁻¹",
        "label": "500mb Vorticity",
        "contours": {
            "levels": [8, 12, 16, 20, 24],
            "colors": "#f8fafc",
            "linewidths": 0.4,
            "alpha": 0.46,
            "smooth_sigma": 0.8,
        },
    },
    # -- 6-hour Accumulated Precip --
    "precip_6h": {
        "cmap": "discrete",
        "bounds": QPF_BOUNDS,
        "colors": QPF_COLORS,
        "tick_vals": [0.01, 0.10, 0.25, 0.50, 1.00, 2.00, 3.00],
        "vmin": 0.01,
        "vmax": 3.0,
        "unit": "in",
        "label": "6h Precipitation (in)",
    },
    "snowfall_6h": {
        "cmap": "discrete",
        "bounds": SNOW_BOUNDS,
        "colors": SNOW_COLORS,
        "tick_vals": [0.1, 1, 2, 4, 6, 8, 12],
        "vmin": 0.1,
        "vmax": 36,
        "unit": "in",
        "label": "6h Snowfall (in)",
    },
    # -- Max/Min Temps --
    "max_temp_1h": {
        "cmap": "wx_temp",
        "vmin": -30,
        "vmax": 120,
        "unit": "°F",
        "label": "1h Max Temperature (°F)",
    },
    "min_temp_1h": {
        "cmap": "wx_temp",
        "vmin": -30,
        "vmax": 120,
        "unit": "°F",
        "label": "1h Min Temperature (°F)",
    },
    "temp_max_6h": {
        "cmap": "wx_temp",
        "vmin": -30,
        "vmax": 120,
        "unit": "°F",
        "label": "6h Max Temperature (°F)",
    },
    "temp_min_6h": {
        "cmap": "wx_temp",
        "vmin": -30,
        "vmax": 120,
        "unit": "°F",
        "label": "6h Min Temperature (°F)",
    },
    # -- Ensemble Spread/Anomaly --
    "gh_500mb_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 1000,
        "unit": "m",
        "label": "500mb Height Spread (m)",
    },
    "gh_500mb_anomaly": {
        "cmap": "RdBu_r",
        "vmin": -300,
        "vmax": 300,
        "unit": "m",
        "label": "500mb Height Anomaly (m)",
    },
    "gh_850mb_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 600,
        "unit": "m",
        "label": "850mb Height Spread (m)",
    },
    "temp_2m_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 10,
        "unit": "K",
        "label": "2m Temp Spread (K)",
    },
    "dewpoint_2m_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 10,
        "unit": "K",
        "label": "2m Dewpoint Spread (K)",
    },
    "mslp_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 800,
        "unit": "Pa",
        "label": "MSLP Spread (Pa)",
    },
    "temp_850mb_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 8,
        "unit": "K",
        "label": "850mb Temp Spread (K)",
    },
    "wind_10m_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 10,
        "unit": "m/s",
        "label": "10m Wind Spread (m/s)",
    },
    "cape_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 1000,
        "unit": "J/kg",
        "label": "CAPE Spread (J/kg)",
    },
    "pwat_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 15,
        "unit": "mm",
        "label": "PWAT Spread (mm)",
    },
    "visibility_spread": {
        "cmap": "YlOrRd",
        "vmin": 0,
        "vmax": 10000,
        "unit": "m",
        "label": "Visibility Spread (m)",
    },
    "prob_temp_below_freezing": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob Temp Below Freezing (%)",
    },
    "prob_cape_gt_250": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob CAPE > 250 J/kg (%)",
    },
    "prob_cape_gt_1000": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob CAPE > 1000 J/kg (%)",
    },
    "prob_freezing_rain": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob Freezing Rain (%)",
    },
    "prob_snow": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob Snow (%)",
    },
    "prob_vis_below_1mi": {
        "cmap": "wx_probability",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Prob Vis Below 1 mi (%)",
    },
    "thickness_1000_500": {
        "cmap": "RdBu_r",
        "vmin": 4920,
        "vmax": 5880,
        "unit": "m",
        "label": "1000-500mb Thickness (m)",
    },
    "thickness_1000_850": {
        "cmap": "RdBu_r",
        "vmin": 1200,
        "vmax": 1600,
        "unit": "m",
        "label": "1000-850mb Thickness (m)",
    },
    "cloud_ceiling": {
        "cmap": "RdYlGn",
        "vmin": 0,
        "vmax": 5000,
        "unit": "m",
        "label": "Cloud Ceiling (m)",
    },
    "crain": {
        "cmap": "Greens",
        "vmin": 0,
        "vmax": 1,
        "unit": "",
        "label": "Categorical Rain",
    },
    "cfrzr": {
        "cmap": "Purples",
        "vmin": 0,
        "vmax": 1,
        "unit": "",
        "label": "Categorical Freezing Rain",
    },
    "cicep": {
        "cmap": "Blues",
        "vmin": 0,
        "vmax": 1,
        "unit": "",
        "label": "Categorical Ice Pellets",
    },
    "csnow": {
        "cmap": "cool",
        "vmin": 0,
        "vmax": 1,
        "unit": "",
        "label": "Categorical Snow",
    },
    "wind_10m": {
        "cmap": "wx_wind",
        "vmin": 0,
        "vmax": 40,
        "unit": "m/s",
        "label": "10m Wind Speed (m/s)",
    },
    # -- Satellite Products --
    "goes_ir_13": {
        "cmap": "wx_enhanced_ir",
        "vmin": -110,
        "vmax": 50,
        "unit": "°C",
        "label": "GOES IR Band 13 (°C)",
    },
    "goes_wv_09": {
        "cmap": "wx_wv",
        "vmin": -80,
        "vmax": -20,
        "unit": "°C",
        "label": "GOES Water Vapor (°C)",
    },
    "goes_vis_02": {
        "cmap": "gray",
        "vmin": 0,
        "vmax": 60,
        "unit": "%",
        "label": "GOES Visible Reflectance (%)",
    },
    "goes_sw_ir_07": {
        "cmap": "wx_enhanced_ir",
        "vmin": -50,
        "vmax": 80,
        "unit": "°C",
        "label": "Shortwave IR Band 7 (°C)",
    },
    "goes_upper_wv_08": {
        "cmap": "wx_wv",
        "vmin": -80,
        "vmax": -20,
        "unit": "°C",
        "label": "Upper Water Vapor (°C)",
    },
    "goes_lower_wv_10": {
        "cmap": "wx_wv",
        "vmin": -60,
        "vmax": 20,
        "unit": "°C",
        "label": "Lower Water Vapor (°C)",
    },
    "goes_snow_ice_05": {
        "cmap": "gray",
        "vmin": 0,
        "vmax": 75,
        "unit": "%",
        "label": "Snow/Ice Reflectance (%)",
    },
    "goes_cirrus_04": {
        "cmap": "gray",
        "vmin": 0,
        "vmax": 75,
        "unit": "%",
        "label": "Cirrus Reflectance (%)",
    },
    "goes_pwat": {
        "cmap": "Blues",
        "vmin": 0,
        "vmax": 70,
        "unit": "mm",
        "label": "Total Precipitable Water (mm)",
    },
    "goes_sst": {
        "cmap": "RdYlBu_r",
        "vmin": -2,
        "vmax": 35,
        "unit": "°C",
        "label": "Sea Surface Temperature (°C)",
    },
    "goes_rain_rate": {
        "cmap": "YlGnBu",
        "vmin": 0,
        "vmax": 50,
        "unit": "mm/hr",
        "label": "Rainfall Rate QPE (mm/hr)",
    },
    "goes_stability": {
        "cmap": "RdBu",
        "vmin": -10,
        "vmax": 10,
        "unit": "K",
        "label": "Lifted Index (K)",
    },
    "goes_vis_1km": {
        "cmap": "gray",
        "vmin": 0,
        "vmax": 75,
        "unit": "%",
        "label": "GOES Visible 1km Reflectance (%)",
    },
    "goes_ir_native": {
        "cmap": "wx_enhanced_ir",
        "vmin": -110,
        "vmax": 50,
        "unit": "°C",
        "label": "Clean IR Native 2km (°C)",
    },
    "goes_ir_14": {
        "cmap": "wx_enhanced_ir",
        "vmin": -110,
        "vmax": 50,
        "unit": "°C",
        "label": "Longwave IR Band 14 (°C)",
    },
    "goes_veggie": {
        "cmap": "gray",
        "vmin": 0,
        "vmax": 75,
        "unit": "%",
        "label": "Veggie Band 3 Reflectance (%)",
    },
    "goes_m1_ir_1km": {
        "cmap": "wx_enhanced_ir",
        "vmin": -110,
        "vmax": 50,
        "unit": "°C",
        "label": "M1 IR 1km (°C)",
    },
    "goes_m2_ir_1km": {
        "cmap": "wx_enhanced_ir",
        "vmin": -110,
        "vmax": 50,
        "unit": "°C",
        "label": "M2 IR 1km (°C)",
    },
    "goes_m1_vis_1km": {
        "cmap": "gray",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "M1 Visible 1km (%)",
    },
    "goes_m2_vis_1km": {
        "cmap": "gray",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "M2 Visible 1km (%)",
    },
    "goes_fire": {
        "cmap": "hot",
        "vmin": 300,
        "vmax": 600,
        "unit": "K",
        "label": "Fire Temperature (K)",
    },
    # New L1b ABI bands
    "goes_vis_blue_01": {
        "cmap": "gray",
        "vmin": 0,
        "vmax": 60,
        "unit": "%",
        "label": "Blue Visible Band 1 (0.47µm) Reflectance (%)",
    },
    "goes_cloud_particle_06": {
        "cmap": "gray",
        "vmin": 0,
        "vmax": 75,
        "unit": "%",
        "label": "Cloud Particle Band 6 (2.2µm) Reflectance (%)",
    },
    "goes_ir_dirty_15": {
        "cmap": "wx_enhanced_ir",
        "vmin": -110,
        "vmax": 50,
        "unit": "°C",
        "label": "Dirty Longwave IR Band 15 (12.3µm) (°C)",
    },
    "goes_co2_16": {
        "cmap": "wx_enhanced_ir",
        "vmin": -110,
        "vmax": 50,
        "unit": "°C",
        "label": "CO2 Absorption IR Band 16 (13.3µm) (°C)",
    },
    "goes_cloud_top_11": {
        "cmap": "wx_enhanced_ir",
        "vmin": -110,
        "vmax": 50,
        "unit": "°C",
        "label": "Cloud Top IR Band 11 (8.4µm) (°C)",
    },
    "goes_ozone_12": {
        "cmap": "Greys",
        "vmin": -110,
        "vmax": 50,
        "unit": "°C",
        "label": "Ozone IR Band 12 (9.6µm) (°C)",
    },
    
    # -- NEXRAD Level II Radar Products --
    "radar_reflectivity": {
        "cmap": "radar_ref",
        "vmin": -20,
        "vmax": 80,
        "unit": "dBZ",
        "label": "Reflectivity (dBZ)",
    },
    "radar_velocity": {
        "cmap": "radar_vel",
        "vmin": -90,
        "vmax": 90,
        "unit": "MPH",
        "label": "Radial Velocity (MPH)",
    },
    "radar_sw": {
        "cmap": "radar_sw",
        "vmin": 0,
        "vmax": 20,
        "unit": "m/s",
        "label": "Spectrum Width (m/s)",
    },
    "radar_zdr": {
        "cmap": "radar_zdr",
        "vmin": -2,
        "vmax": 6,
        "unit": "dB",
        "label": "Differential Reflectivity (dB)",
    },
    "radar_cc": {
        "cmap": "radar_cc",
        "vmin": 0.20,
        "vmax": 1.05,
        "unit": "",
        "label": "Correlation Coefficient",
    },
    "radar_phidp": {
        "cmap": "radar_phidp",
        "vmin": 0,
        "vmax": 180,
        "unit": "deg",
        "label": "Differential Phase (°)",
    },
    "radar_kdp": {
        "cmap": "radar_kdp",
        "vmin": -1,
        "vmax": 8,
        "unit": "deg/km",
        "label": "Specific Differential Phase (deg/km)",
    },
    "radar_srm": {
        "cmap": "radar_vel",
        "vmin": -90,
        "vmax": 90,
        "unit": "MPH",
        "label": "Storm-Relative Velocity (MPH)",
    },
    "radar_azs": {
        "cmap": "radar_azs",
        "vmin": 0,
        "vmax": 0.05,
        "unit": "s^-1",
        "label": "Azimuthal Shear (s^-1)",
    },
    "radar_div": {
        "cmap": "radar_div",
        "vmin": -12,
        "vmax": 12,
        "unit": "m/s per km",
        "label": "Divergence / Convergence",
    },
    "radar_cct": {
        "cmap": "radar_cct",
        "vmin": 0,
        "vmax": 1,
        "unit": "",
        "label": "CC Texture",
    },
    "radar_pht": {
        "cmap": "radar_pht",
        "vmin": 0,
        "vmax": 60,
        "unit": "deg",
        "label": "PHI Texture (deg)",
    },
    "radar_tds": {
        "cmap": "radar_tds",
        "vmin": 0,
        "vmax": 1,
        "unit": "",
        "label": "Debris Signature",
    },
    "radar_cref": {
        "cmap": "radar_ref",
        "vmin": -20,
        "vmax": 80,
        "unit": "dBZ",
        "label": "Composite Reflectivity (dBZ)",
    },
    "radar_etop": {
        "cmap": "radar_etop",
        "vmin": 0,
        "vmax": 70,
        "unit": "kft",
        "label": "Echo Tops (kft)",
    },
    "radar_vil": {
        "cmap": "radar_vil",
        "vmin": 0,
        "vmax": 80,
        "unit": "kg/m^2",
        "label": "Vertically Integrated Liquid",
    },
    "radar_vxt": {
        "cmap": "radar_vxt",
        "vmin": 0,
        "vmax": 80,
        "unit": "MPH",
        "label": "Velocity Texture (MPH)",
    },
    "radar_hail": {
        "cmap": "radar_hail",
        "vmin": 0,
        "vmax": 100,
        "unit": "%",
        "label": "Hail Probability (%)",
    },
    "radar_rain": {
        "cmap": "radar_rain",
        "vmin": 0,
        "vmax": 8,
        "unit": "in/hr",
        "label": "Rainfall Rate (in/hr)",
    },
}

COLOR_SCALES.setdefault("refl_1km", {
    **COLOR_SCALES["reflectivity"],
    "label": "1km Reflectivity (dBZ)",
})
COLOR_SCALES.setdefault("cape_ml", {
    **COLOR_SCALES["mlcape"],
    "label": "MLCAPE (J/kg)",
})
COLOR_SCALES.setdefault("cin_ml", {
    **COLOR_SCALES["mlcin"],
    "label": "MLCIN (J/kg)",
})
COLOR_SCALES.setdefault("cape_surface", {
    **COLOR_SCALES["cape"],
    "label": "Surface CAPE (J/kg)",
})
COLOR_SCALES.setdefault("cin_surface", {
    **COLOR_SCALES["cin"],
    "label": "Surface CIN (J/kg)",
})
COLOR_SCALES.setdefault("wind_gust_10m", {
    "cmap": "wx_wind",
    "vmin": 0,
    "vmax": 80,
    "unit": "mph",
    "label": "10m Wind Gust (mph)",
})
COLOR_SCALES.setdefault("temp_300mb", {
    "cmap": "RdBu_r",
    "vmin": -85,
    "vmax": -15,
    "unit": "°F",
    "label": "300mb Temperature (°F)",
})
COLOR_SCALES.setdefault("snowfall_probability_6h_1in", {
    "cmap": "wx_probability",
    "vmin": 0,
    "vmax": 100,
    "unit": "%",
    "label": "Prob 6h Snow >1\" (%)",
})
COLOR_SCALES.setdefault("cape_probability_1000", {
    "cmap": "wx_probability",
    "vmin": 0,
    "vmax": 100,
    "unit": "%",
    "label": "Prob CAPE >1000 J/kg (%)",
})

# Tag scales that draw from the versioned palette registry so the web API can
# resolve PNGs rendered under an older palette (see /api/value-png).
for _scale_cfg in COLOR_SCALES.values():
    if _scale_cfg.get("cmap") == "wx_temp":
        _scale_cfg["palette"] = "temp"
    elif _scale_cfg.get("cmap") == "wx_temp_upper":
        _scale_cfg["palette"] = "temp_upper"
    elif _scale_cfg.get("cmap") == "wx_cin":
        _scale_cfg["palette"] = "cin"
    elif _scale_cfg.get("cmap") == "nws_refl":
        _scale_cfg["palette"] = "reflectivity"
    elif _scale_cfg.get("cmap") == "wx_enhanced_ir":
        _scale_cfg["palette"] = "ir_enhanced"
    elif _scale_cfg.get("cmap") == "wx_wv":
        _scale_cfg["palette"] = "wv"
    elif _scale_cfg.get("colors") == QPF_COLORS:
        _scale_cfg["palette"] = "qpf"
del _scale_cfg

_EXTRA_SOUNDING_TEMP_LEVELS = {
    1000: (-40, 110),
    900: (-40, 100),
    800: (-40, 90),
    750: (-50, 80),
    650: (-60, 70),
    600: (-70, 60),
    550: (-80, 50),
    450: (-90, 30),
    400: (-100, 20),
    350: (-110, 10),
}
for _level, (_vmin, _vmax) in _EXTRA_SOUNDING_TEMP_LEVELS.items():
    COLOR_SCALES.setdefault(
        f"temp_{_level}mb",
        {
            "cmap": "wx_temp",
            "vmin": _vmin,
            "vmax": _vmax,
            "unit": "°F",
            "label": f"{_level}mb Temperature (°F)",
        },
    )

for _level in (1000, 900, 800, 750):
    COLOR_SCALES.setdefault(
        f"dewpoint_{_level}mb",
        {
            "cmap": "wx_dewpoint",
            "vmin": -30,
            "vmax": 80,
            "unit": "°F",
            "label": f"{_level}mb Dewpoint (°F)",
        },
    )

_EXTRA_SOUNDING_HEIGHT_RANGES = {
    900: (600, 1400),
    800: (1200, 2400),
    750: (1800, 3000),
    650: (2600, 4200),
    600: (3400, 4700),
    550: (4200, 5600),
    450: (5200, 7200),
    400: (6200, 8200),
    350: (7600, 9300),
}
for _level, (_vmin, _vmax) in _EXTRA_SOUNDING_HEIGHT_RANGES.items():
    COLOR_SCALES.setdefault(
        f"gh_{_level}mb",
        {
            "cmap": "wx_heights",
            "vmin": _vmin,
            "vmax": _vmax,
            "unit": "m",
            "label": f"{_level}mb Heights (m)",
        },
    )

for _level in (1000, 900, 800, 750, 650, 600, 550, 450, 400, 350):
    COLOR_SCALES.setdefault(
        f"rh_{_level}mb",
        {
            "cmap": "wx_rh",
            "vmin": 0,
            "vmax": 100,
            "unit": "%",
            "label": f"{_level}mb RH (%)",
        },
    )

# Disk pressure threshold — trigger aggressive cleanup below this
DISK_PRESSURE_THRESHOLD_GB = float(os.environ.get("WX_DISK_PRESSURE_GB", "5"))

# ---------------------------------------------------------------------------
# Satellite Configuration
# ---------------------------------------------------------------------------
SATELLITE_CONFIG = {
    "enabled": True,
    "sources": {
        "goes18": {
            "name": "GOES-18 West",
            "satellite": 18,
            "coverage": "Pacific / Western Americas",
            "location": "137.0°W",
            "domains": ["F", "C", "M1", "M2"],
            # Other GOES-18 bands feed the GOES-19 blend or regional views.
            "standalone_products": ["pwat", "sst", "rain_rate", "stability", "fire"],
        },
        "goes19": {
            "name": "GOES-19 East",
            "satellite": 19,
            "coverage": "Americas / Atlantic",
            "location": "75.2°W",
            "domains": ["F", "C", "M1", "M2"],
        },
    },
    "compositing": {
        "enabled": True,
        "output_source_id": "goes19",   # output filenames keep goes19_ prefix
        "west_source": "goes18",
        "east_source": "goes19",
        "blend_west_lon": -113.0,       # west edge of crossfade zone
        "blend_east_lon": -100.0,       # east edge of crossfade zone
        "time_tolerance_minutes": 8,    # max scan-time delta for matching
        "products": [
            "ir", "wv", "vis", "vis_blue", "sw_ir", "upper_wv", "lower_wv",
            "snow_ice", "cirrus", "veggie", "cloud_particle", "ir_14",
            "cloud_top", "ozone", "ir_dirty", "co2",
            "natural_color", "day_cloud_phase", "sandwich",
            "ir_native",
        ],
    },
    "products": {
        "ir": {
            "name": "Infrared (Band 13)",
            "product_code": "ABI-L2-MCMIP",
            "band": 13,
            "description": "Clean Longwave IR - Day/Night Clouds",
            "update_freq_minutes": 10,
            "loop_hours": 24,
            "loop_frames_per_band": 144,
        },
        "wv": {
            "name": "Water Vapor (Band 9)",
            "product_code": "ABI-L2-MCMIP",
            "band": 9,
            "description": "Mid-level Water Vapor",
            "update_freq_minutes": 10,
        },
        "vis": {
            "name": "Visible (Band 2)",
            "product_code": "ABI-L2-MCMIP",
            "band": 2,
            "description": "Red Visible - Daytime Only",
            "update_freq_minutes": 10,
            "loop_hours": 24,
        },
        "vis_blue": {
            "name": "Visible Blue (Band 1)",
            "product_code": "ABI-L2-MCMIP",
            "band": 1,
            "description": "Blue visible band - aerosol and haze detail",
            "update_freq_minutes": 10,
            "loop_hours": 24,
        },
        "sw_ir": {
            "name": "Shortwave IR (Band 7)",
            "product_code": "ABI-L2-MCMIP",
            "band": 7,
            "description": "Fire/hot spot, fog detection - 3.9µm",
            "update_freq_minutes": 10,
            "loop_hours": 24,
            "loop_frames_per_band": 144,
        },
        "upper_wv": {
            "name": "Upper Water Vapor (Band 8)",
            "product_code": "ABI-L2-MCMIP",
            "band": 8,
            "description": "Upper troposphere moisture - 6.2µm",
            "update_freq_minutes": 10,
        },
        "lower_wv": {
            "name": "Lower Water Vapor (Band 10)",
            "product_code": "ABI-L2-MCMIP",
            "band": 10,
            "description": "Lower troposphere moisture - 7.3µm",
            "update_freq_minutes": 10,
        },
        "snow_ice": {
            "name": "Snow/Ice (Band 5)",
            "product_code": "ABI-L2-MCMIP",
            "band": 5,
            "description": "Snow vs cloud discrimination - 1.6µm",
            "update_freq_minutes": 10,
        },
        "cirrus": {
            "name": "Cirrus (Band 4)",
            "product_code": "ABI-L2-MCMIP",
            "band": 4,
            "description": "Thin high cloud detection - 1.37µm",
            "update_freq_minutes": 10,
        },
        "cloud_particle": {
            "name": "Cloud Particle (Band 6)",
            "product_code": "ABI-L2-MCMIP",
            "band": 6,
            "description": "Cloud particle size / phase discrimination - 2.2µm",
            "update_freq_minutes": 10,
        },
        "natural_color": {
            "name": "Natural Color RGB",
            "product_code": "ABI-L2-MCMIP",
            "bands": [1, 2, 3],
            "rgb": True,
            "description": "True-color daytime composite",
            "update_freq_minutes": 10,
        },
        "day_cloud_phase": {
            "name": "Day Cloud Phase RGB",
            "product_code": "ABI-L2-MCMIP",
            "bands": [2, 5, 13],
            "rgb": True,
            "description": "Ice vs water vs clear sky",
            "update_freq_minutes": 10,
        },
        "sandwich": {
            "name": "IR/VIS Sandwich RGB",
            "product_code": "ABI-L2-MCMIP",
            "bands": [2, 13],
            "rgb": True,
            "description": "Visible texture blended with enhanced IR cloud-top temperature",
            "update_freq_minutes": 10,
        },
        "fire": {
            "name": "Fire/Hot Spot",
            "product_code": "ABI-L2-FDC",
            "domain": "F",
            "description": "Active fire detections",
            "update_freq_minutes": 10,
        },
        "pwat": {
            "name": "Total Precipitable Water",
            "product_code": "ABI-L2-TPW",
            "domain": "F",
            "description": "Satellite-derived PWAT (mm)",
            "update_freq_minutes": 30,
        },
        "sst": {
            "name": "Sea Surface Temperature",
            "product_code": "ABI-L2-SST",
            "domain": "F",
            "description": "SST (°C)",
            "update_freq_minutes": 30,
        },
        "rain_rate": {
            "name": "Rainfall Rate QPE",
            "product_code": "ABI-L2-RRQPE",
            "domain": "F",
            "description": "Satellite-derived precip rate (mm/hr)",
            "update_freq_minutes": 10,
        },
        "stability": {
            "name": "Lifted Index",
            "product_code": "ABI-L2-DSI",
            "domain": "F",
            "description": "Derived stability indices - Lifted Index",
            "update_freq_minutes": 30,
        },
        "vis_1km": {
            "name": "Visible 1km (Band 2 CMIP)",
            "product_code": "ABI-L2-CMIP",
            "band": 2,
            "domain": "C",
            "description": "Native-resolution CMIP Band 2 visible reflectance",
            "update_freq_minutes": 5,
            "loop_hours": 24,
        },
        "ir_native": {
            "name": "Clean IR Native (Band 13 CMIP)",
            "product_code": "ABI-L2-CMIP",
            "band": 13,
            "domain": "C",
            "description": "Native 2km Clean IR (vs re-gridded MCMIP)",
            "update_freq_minutes": 5,
            "loop_hours": 24,
            "loop_frames_per_band": 288,
        },
        "ir_14": {
            "name": "Longwave IR (Band 14)",
            "product_code": "ABI-L2-CMIP",
            "band": 14,
            "domain": "C",
            "description": "Longwave IR 11.2µm - companion to Band 13 for microphysics",
            "update_freq_minutes": 5,
            "loop_hours": 24,
            "loop_frames_per_band": 288,
        },
        "cloud_top": {
            "name": "Cloud Top IR (Band 11)",
            "product_code": "ABI-L1b-Rad",
            "band": 11,
            "domain": "C",
            "description": "8.4µm cloud-top and low-level moisture detail",
            "update_freq_minutes": 5,
            "loop_hours": 24,
        },
        "ozone": {
            "name": "Ozone IR (Band 12)",
            "product_code": "ABI-L1b-Rad",
            "band": 12,
            "domain": "C",
            "description": "9.6µm ozone-sensitive upper-level signal",
            "update_freq_minutes": 5,
            "loop_hours": 24,
        },
        "ir_dirty": {
            "name": "Dirty Longwave IR (Band 15)",
            "product_code": "ABI-L1b-Rad",
            "band": 15,
            "domain": "C",
            "description": "12.3µm dirty longwave IR for cloud-top and low-level moisture contrast",
            "update_freq_minutes": 5,
            "loop_hours": 24,
        },
        "co2": {
            "name": "CO2 Absorption IR (Band 16)",
            "product_code": "ABI-L1b-Rad",
            "band": 16,
            "domain": "C",
            "description": "13.3µm CO2 absorption band for high cloud and upper-tropospheric structure",
            "update_freq_minutes": 5,
            "loop_hours": 24,
        },
        "veggie": {
            "name": "Veggie (Band 3, 1km)",
            "product_code": "ABI-L2-CMIP",
            "band": 3,
            "domain": "C",
            "description": "Veggie 0.86µm - vegetation health, snow mask at 1km",
            "update_freq_minutes": 5,
        },
        # Mesoscale sectors - 1km resolution, 1-minute updates
        "m1_ir_1km": {
            "name": "M1 Floater IR (1km)",
            "product_code": "ABI-L2-MCMIP",
            "domain": "M1",
            "band": 13,
            "description": "Mesoscale sector 1 - Clean IR at 1km, 1-min updates (repositionable)",
            "update_freq_minutes": 1,
            "mesoscale": True,
            "retention_hours": 24,
        },
        "m2_ir_1km": {
            "name": "M2 Floater IR (1km)",
            "product_code": "ABI-L2-MCMIP",
            "domain": "M2",
            "band": 13,
            "description": "Mesoscale sector 2 - Clean IR at 1km, 1-min updates (repositionable)",
            "update_freq_minutes": 1,
            "mesoscale": True,
            "retention_hours": 24,
        },
        "m1_vis_1km": {
            "name": "M1 Floater Visible (1km)",
            "product_code": "ABI-L2-MCMIP",
            "domain": "M1",
            "band": 2,
            "description": "Mesoscale sector 1 - Visible at 1km, 1-min updates (daytime only)",
            "update_freq_minutes": 1,
            "mesoscale": True,
            "retention_hours": 24,
            "loop_hours": 24,
        },
        "m2_vis_1km": {
            "name": "M2 Floater Visible (1km)",
            "product_code": "ABI-L2-MCMIP",
            "domain": "M2",
            "band": 2,
            "description": "Mesoscale sector 2 - Visible at 1km, 1-min updates (daytime only)",
            "update_freq_minutes": 1,
            "mesoscale": True,
            "retention_hours": 24,
            "loop_hours": 24,
        },
    },
    "regions": {
        "central_plains": {
            "name": "Central Plains",
            "enabled": True,
            "source": "goes19",
            "west": -109.8,
            "east": -88.4,
            "south": 35.9,
            "north": 46.4,
            "products": [
                "vis_1km",
                "ir_native",
                "ir_14",
                "veggie",
                "cloud_particle",
            ],
        },
        "northern_plains": {
            "name": "Northern Plains",
            "enabled": True,
            "source": "goes19",
            "west": -111.6,
            "east": -90.4,
            "south": 39.6,
            "north": 49.5,
            "products": [
                "vis_1km",
                "ir_native",
                "ir_14",
                "veggie",
                "cloud_particle",
            ],
        },
        "upper_midwest": {
            "name": "Upper Midwest",
            "enabled": True,
            "source": "goes19",
            "west": -100.4,
            "east": -79.2,
            "south": 39.1,
            "north": 49.4,
            "products": [
                "vis_1km",
                "ir_native",
                "ir_14",
                "veggie",
                "cloud_particle",
            ],
        },
        "southern_plains": {
            "name": "Southern Plains",
            "enabled": True,
            "source": "goes19",
            "west": -110.0,
            "east": -88.8,
            "south": 27.4,
            "north": 37.8,
            "products": [
                "vis_1km",
                "ir_native",
                "ir_14",
                "veggie",
                "cloud_particle",
            ],
        },
        "southeast": {
            "name": "Southeast",
            "enabled": True,
            "source": "goes19",
            "west": -93.4,
            "east": -72.2,
            "south": 24.5,
            "north": 35.7,
            "products": [
                "vis_1km",
                "ir_native",
                "ir_14",
                "veggie",
                "cloud_particle",
            ],
        },
        "northeast": {
            "name": "Northeast",
            "enabled": True,
            "source": "goes19",
            "west": -84.6,
            "east": -64.4,
            "south": 37.3,
            "north": 47.7,
            "products": [
                "vis_1km",
                "ir_native",
                "ir_14",
                "veggie",
                "cloud_particle",
            ],
        },
        "southwest": {
            "name": "Southwest",
            "enabled": True,
            "source": "goes18",
            "west": -120.9,
            "east": -99.7,
            "south": 31.1,
            "north": 41.5,
            "products": [
                "vis_1km",
                "ir_native",
                "ir_14",
                "veggie",
                "cloud_particle",
            ],
        },
        "west_coast": {
            "name": "West Coast",
            "enabled": True,
            "source": "goes18",
            "west": -128.6,
            "east": -107.4,
            "south": 35.3,
            "north": 45.7,
            "products": [
                "vis_1km",
                "ir_native",
                "ir_14",
                "veggie",
                "cloud_particle",
            ],
        },
    },
    "download": {
        "domain": "C",  # CONUS for ABI
        "retention_hours": 24,  # Keep 24 hours of satellite data
        "parallel_downloads": 2,
    },
    "render": {
        "dpi": 150,
        "output_format": "png",
        # 24-hour loop depth retained on disk. 10-min products → 144 frames,
        # 5-min → 288, 1-min mesoscale → 1440.
        "loop_hours": 24,
        "cache_retention_hours": 2,  # Raw .nc files deleted after 2 hours (renders happen immediately)
        "loop_frames_per_band": 720,
        # Render a few latest frames every cycle; archive cycles backfill the rest
        # of the 12-hour window in bounded chunks.
        "latest_frames_per_cycle": 3,
        # Archive mode catches up in bounded chunks instead of trying to render the
        # entire 12-hour backlog in one pass.
        "archive_frames_per_cycle": 12,
        # Fire archive more often but each cycle has far fewer new frames to catch up.
        "archive_refresh_interval_seconds": 90,
        "archive_budget_seconds": 15,
        # Cleanup is handled by the cron job (scripts/cleanup_weather_data.py, hourly).
        "cleanup_in_process": False,
        # Cleanup is handled by the cron job (scripts/cleanup_weather_data.py, hourly).
        # Keep this high so the in-scheduler cleanup stays effectively disabled.
        "cleanup_interval_seconds": 86400,
        # Keep concurrency below the point where large archive cycles start
        # stalling and timing out under mixed composite/regional load.
        "max_workers": 3,
        # Dispatch satellite work in bounded waves instead of queueing the
        # entire cycle at once; this reduces long stalls and makes timeouts
        # easier to recover from.
        "job_batch_size": 6,
    },
}

_satellite_workers = os.environ.get("WX_SATELLITE_WORKERS")
if _satellite_workers is not None:
    SATELLITE_CONFIG["render"]["max_workers"] = max(1, int(_satellite_workers))
    SATELLITE_CONFIG["render"]["job_batch_size"] = max(2, int(_satellite_workers) * 2)

_satellite_loop_hours = os.environ.get("WX_SATELLITE_LOOP_HOURS")
if _satellite_loop_hours is not None:
    _loop_hours = max(1, int(_satellite_loop_hours))
    SATELLITE_CONFIG["render"]["loop_hours"] = _loop_hours
    for _product in SATELLITE_CONFIG["products"].values():
        _product["loop_hours"] = _loop_hours
        if "retention_hours" in _product:
            _product["retention_hours"] = min(_product["retention_hours"], _loop_hours)

# Satellite data directories
SATELLITE_CACHE_DIR = DATA_DIR / "satellite_cache"
SATELLITE_RENDERED_DIR = RENDERED_DIR / "satellite"
