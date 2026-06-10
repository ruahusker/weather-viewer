"""Versioned color palette registry — single source of truth for weather
data color schemes shared by the model renderer, radar color tables, the
satellite renderer, and the web API.

Why versions: rendered PNGs persist on disk (6-run retention for models,
hours of satellite/radar frames) and /api/value-png reverse-maps PNG colors
back to data values. When a palette changes, PNGs rendered with the old
palette must keep resolving against the old table until they age out. Each
model run records ``palette_version`` in run_meta.json; the web API selects
the matching reverse-LUT.

Version 1: the legacy custom ramps (pre-revamp).
Version 2: industry-standard palettes — NWS WSR-88D reflectivity, COD/
Pivotal-style temperature and QPF, conventional rainbow IR enhancement and
water-vapor curves.
"""

from __future__ import annotations

CURRENT_PALETTE_VERSION = 2

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# Discrete palettes: {"bounds": [...], "colors": [hex, ...]}  (len(colors) ==
# len(bounds) - 1; values map to the bin between consecutive bounds).
# Linear palettes:   {"stops": [(position 0..1, hex), ...], "vmin": x, "vmax": y}

PALETTES: dict[tuple[str, int], dict] = {}


def _register(name: str, version: int, definition: dict) -> None:
    PALETTES[(name, version)] = definition


def get_palette(name: str, version: int | None = None) -> dict | None:
    """Return a palette definition, falling back to the newest version <= requested."""
    if version is None:
        version = CURRENT_PALETTE_VERSION
    for v in range(version, 0, -1):
        definition = PALETTES.get((name, v))
        if definition is not None:
            return definition
    return None


# ---------------------------------------------------------------------------
# Reflectivity — composite/base reflectivity in dBZ
# ---------------------------------------------------------------------------
# v2 is the standard NWS WSR-88D table (identical to the Level II radar
# products) so model-simulated and observed reflectivity read the same.

_register("reflectivity", 1, {
    "bounds": [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75],
    "colors": [
        "#66d9ff", "#0099cc", "#00cc00", "#009900", "#ffff00", "#ffcc00",
        "#ff9900", "#ff0000", "#cc0000", "#990000", "#ff00ff", "#cc00cc",
        "#990099", "#ffffff",
    ],
})

_register("reflectivity", 2, {
    "bounds": [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80],
    "colors": [
        "#00ecec", "#01a0f6", "#006ec8", "#00ff00", "#00c800", "#009000",
        "#ffff00", "#e7c000", "#ff9000", "#ff0000", "#d60000", "#c00000",
        "#ff00ff", "#9955c9", "#ffffff",
    ],
})

# ---------------------------------------------------------------------------
# 2m / pressure-level temperature (deg F)
# ---------------------------------------------------------------------------

_register("temp", 1, {
    "vmin": -30, "vmax": 120,
    "stops": [
        (0.000, "#4b0082"), (0.067, "#273cbe"), (0.133, "#1f63ff"),
        (0.200, "#2f8fff"), (0.267, "#59bcff"), (0.333, "#88e3ff"),
        (0.400, "#b8fff7"), (0.467, "#8ce99a"), (0.533, "#4fd36d"),
        (0.600, "#9bd93c"), (0.667, "#e4e84a"), (0.733, "#ffd24a"),
        (0.800, "#ffad33"), (0.867, "#ff7a1a"), (0.933, "#e63b19"),
        (1.000, "#b30000"),
    ],
})

# v2: operational-style ramp (Pivotal/COD family): purples below 0F, blues
# to freezing, greens 40s-50s, yellow 60s-70s, orange 80s, red 90s-100s,
# magenta for extreme heat. Positions anchored every 10F from -30 to 120.
_register("temp", 2, {
    "vmin": -30, "vmax": 120,
    "stops": [
        (0.000, "#e8d0ff"),  # -30F pale lavender
        (0.067, "#b266e0"),  # -20F purple
        (0.133, "#7a2ed6"),  # -10F deep purple
        (0.200, "#3b28c8"),  #   0F indigo
        (0.267, "#2257e0"),  #  10F blue
        (0.333, "#3585e8"),  #  20F medium blue
        (0.400, "#62b8f2"),  #  30F light blue
        (0.413, "#86d9e8"),  #  32F freezing line cyan
        (0.467, "#4fc77c"),  #  40F light green
        (0.533, "#22a04e"),  #  50F green
        (0.600, "#9fcf30"),  #  60F yellow-green
        (0.667, "#ffe600"),  #  70F yellow
        (0.733, "#ffb000"),  #  80F orange
        (0.800, "#ff6600"),  #  90F deep orange
        (0.867, "#e60000"),  # 100F red
        (0.933, "#9e0000"),  # 110F dark red
        (1.000, "#ff00c8"),  # 120F magenta (extreme heat)
    ],
})

# ---------------------------------------------------------------------------
# QPF — accumulated precipitation (inches)
# ---------------------------------------------------------------------------

_register("qpf", 1, {
    "bounds": [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00],
    "colors": [
        "#e7f5ff", "#bde0fe", "#73bdf2", "#37a0d8", "#168aad",
        "#35a853", "#f5d742", "#f28e2b", "#d73027",
    ],
})

# v2: COD/WPC-style green ramp into yellow/orange/red with magenta extremes.
_register("qpf", 2, {
    "bounds": [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00],
    "colors": [
        "#b4f0b4", "#69d069", "#1eb01e", "#0c7c0c", "#ffff00",
        "#ffc400", "#ff7300", "#e60000", "#ff00ff",
    ],
})

# ---------------------------------------------------------------------------
# Satellite IR enhancement (brightness temperature, deg C; cold -> position 0)
# ---------------------------------------------------------------------------

_register("ir_enhanced", 1, {
    "vmin": -110, "vmax": 50,
    "stops": [
        (0.000, "#080810"), (0.190, "#2020a0"), (0.250, "#c020c0"),
        (0.310, "#d02020"), (0.375, "#e07020"), (0.440, "#d0d020"),
        (0.500, "#30a030"), (0.560, "#40c0c0"), (0.625, "#c8e8f0"),
        (0.690, "#dddddd"), (0.750, "#999999"), (0.810, "#555555"),
        (1.000, "#000000"),
    ],
})

# v2: conventional rainbow IR enhancement — grayscale through warm scene
# temps, color enhancement kicking in below -30C with the familiar
# cyan/blue -> green -> yellow -> red -> magenta -> white cold-top ramp.
# Positions are anchored on temperature: pos = (T - (-110)) / 160.
_register("ir_enhanced", 2, {
    "vmin": -110, "vmax": 50,
    "stops": [
        (0.000, "#ffffff"),  # -110C white (overshooting tops)
        (0.125, "#ff8cff"),  #  -90C pink
        (0.188, "#cc00cc"),  #  -80C magenta
        (0.250, "#ff0000"),  #  -70C red
        (0.313, "#ff9000"),  #  -60C orange
        (0.375, "#ffff00"),  #  -50C yellow
        (0.438, "#00c800"),  #  -40C green
        (0.480, "#01a0f6"),  #  -33C blue
        (0.498, "#00ecec"),  #  -30.3C cyan (enhancement floor)
        (0.500, "#e8e8e8"),  #  -30C light gray (grayscale begins)
        (0.625, "#b0b0b0"),  #  -10C
        (0.688, "#909090"),  #    0C
        (0.813, "#505050"),  #  +20C
        (1.000, "#0a0a0a"),  #  +50C near-black (hot ground)
    ],
})

# ---------------------------------------------------------------------------
# Water vapor (brightness temperature; cold/moist -> position 0)
# ---------------------------------------------------------------------------

_register("wv", 1, {
    "vmin": -80, "vmax": -20,
    "stops": [
        (0.000, "#ffffff"), (0.200, "#e0e0e0"), (0.350, "#208080"),
        (0.500, "#20802a"), (0.650, "#5a7a20"), (0.800, "#6b3a12"),
        (1.000, "#3d1c02"),
    ],
})

# v2: conventional WV enhancement — deep blue/purple coldest tops, blue ->
# green moist plumes, white transition, tan -> dark brown dry slots.
_register("wv", 2, {
    "vmin": -80, "vmax": -20,
    "stops": [
        (0.000, "#5a009e"),  # coldest, deepest moisture
        (0.120, "#1d4ed8"),  # blue
        (0.280, "#0e9e4a"),  # green
        (0.430, "#7fcf8f"),  # pale green
        (0.550, "#ececec"),  # white transition
        (0.700, "#d2b48c"),  # tan
        (0.850, "#8a5a2b"),  # brown
        (1.000, "#3d1c02"),  # driest, dark brown
    ],
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_mpl_cmap(name: str, version: int | None = None, cmap_name: str | None = None):
    """Build (without registering) a matplotlib colormap for a linear palette."""
    from matplotlib.colors import LinearSegmentedColormap

    definition = get_palette(name, version)
    if definition is None or "stops" not in definition:
        return None
    return LinearSegmentedColormap.from_list(cmap_name or name, definition["stops"])


def register_mpl_cmap(name: str, mpl_name: str, version: int | None = None) -> bool:
    """Register a linear palette as a matplotlib colormap under mpl_name."""
    import matplotlib

    cmap = build_mpl_cmap(name, version, cmap_name=mpl_name)
    if cmap is None:
        return False
    matplotlib.colormaps.register(cmap, name=mpl_name, force=True)
    return True


def reflectivity_table(version: int | None = None) -> tuple[list, list[str]]:
    """Return (bounds, hex colors) for the reflectivity palette."""
    definition = get_palette("reflectivity", version)
    return list(definition["bounds"]), list(definition["colors"])
