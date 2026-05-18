"""Overlay renderer — produces PNG overlays for Leaflet map display.

Renders HRRR data as PNG images positioned by lat/lon bounds.
Images are rendered in Web Mercator Y-space so they align perfectly with
Leaflet's basemap tiles. A USA border mask (Natural Earth 50m) blacks out
everything outside the country.
"""
import logging
from datetime import datetime
from pathlib import Path

import cartopy.io.shapereader as shpreader
import matplotlib
import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.path as mpath
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap
from scipy.interpolate import griddata
from scipy.ndimage import minimum_filter, maximum_filter
from shapely.geometry import box as shapely_box

from .config import COLOR_SCALES, RENDER_DPI

matplotlib.use("Agg")

logger = logging.getLogger(__name__)

# Background color for mask / figure
MASK_COLOR = (0, 0, 0, 0)

# ---------------------------------------------------------------------------
# Mercator helpers
# ---------------------------------------------------------------------------

def _lat_to_merc(lat):
    """Convert latitude (degrees) to Web Mercator Y (degrees)."""
    lat_rad = np.radians(np.clip(lat, -85, 85))
    return np.degrees(np.log(np.tan(np.pi / 4 + lat_rad / 2)))


# ---------------------------------------------------------------------------
# USA border mask — rendered in Mercator Y-space
# ---------------------------------------------------------------------------
_USA_MASK_PATH = None  # cached matplotlib compound Path (in Mercator coords)


def _build_usa_mask_path():
    """Build a matplotlib Path in Mercator Y-space: domain rectangle with USA holes.

    Uses Natural Earth 50m admin_0_countries data via cartopy.
    All latitude values are converted to Mercator Y so the mask aligns
    with Leaflet's Web Mercator tile layer.
    """
    global _USA_MASK_PATH
    if _USA_MASK_PATH is not None:
        return _USA_MASK_PATH

    shpfile = shpreader.natural_earth(
        resolution="50m", category="cultural", name="admin_0_countries"
    )
    reader = shpreader.Reader(shpfile)

    usa_geom = None
    for rec in reader.records():
        if rec.attributes.get("ADMIN") == "United States of America":
            usa_geom = rec.geometry
            break

    if usa_geom is None:
        logger.error("Could not find USA geometry in Natural Earth data")
        return None

    # Clip to HRRR domain
    domain = shapely_box(HRRR_WEST, HRRR_SOUTH, HRRR_EAST, HRRR_NORTH)
    usa_clipped = usa_geom.intersection(domain)

    # Outer rectangle in Mercator Y-space (CCW winding)
    margin = 2.0
    s_m = _lat_to_merc(HRRR_SOUTH) - margin
    n_m = _lat_to_merc(HRRR_NORTH) + margin
    w = HRRR_WEST - margin
    e = HRRR_EAST + margin

    rect_verts = [(w, s_m), (e, s_m), (e, n_m), (w, n_m), (w, s_m)]
    rect_codes = [mpath.Path.MOVETO] + [mpath.Path.LINETO] * 3 + [mpath.Path.CLOSEPOLY]

    all_verts = list(rect_verts)
    all_codes = list(rect_codes)

    # Extract polygon exteriors as holes (already CW winding from Natural Earth)
    polys = list(usa_clipped.geoms) if hasattr(usa_clipped, "geoms") else [usa_clipped]

    for poly in polys:
        coords = list(poly.exterior.coords)
        # Convert lat to Mercator Y
        merc_coords = [(lon, _lat_to_merc(lat)) for lon, lat in coords]
        hole_codes = (
            [mpath.Path.MOVETO]
            + [mpath.Path.LINETO] * (len(merc_coords) - 2)
            + [mpath.Path.CLOSEPOLY]
        )
        all_verts.extend(merc_coords)
        all_codes.extend(hole_codes)

    _USA_MASK_PATH = mpath.Path(all_verts, all_codes)
    logger.info("Built USA border mask (Mercator) with %d polygon holes", len(polys))
    return _USA_MASK_PATH


def _apply_usa_mask(ax):
    """Draw mask patch over everything outside the USA border (Mercator space).
    Returns the patch artist for visibility toggling."""
    mask_path = _build_usa_mask_path()
    if mask_path is None:
        return None
    patch = mpatches.PathPatch(
        mask_path,
        facecolor=MASK_COLOR,
        edgecolor="none",
        linewidth=0,
        zorder=50,
    )
    ax.add_patch(patch)
    return patch


# ---------------------------------------------------------------------------
# Great Lakes — blue fill on top of data, below mask
# ---------------------------------------------------------------------------
_GREAT_LAKES_PATHS = None

GREAT_LAKE_NAMES = {"Lake Superior", "Lake Michigan", "Lake Huron", "Lake Erie", "Lake Ontario"}
LAKE_COLOR = "#4a90b8"


def _build_great_lakes_paths():
    """Load Great Lakes polygons from Natural Earth, converted to Mercator Y."""
    global _GREAT_LAKES_PATHS
    if _GREAT_LAKES_PATHS is not None:
        return _GREAT_LAKES_PATHS

    shpfile = shpreader.natural_earth(
        resolution="50m", category="physical", name="lakes"
    )
    reader = shpreader.Reader(shpfile)

    paths = []
    for rec in reader.records():
        name = rec.attributes.get("name", "")
        if name in GREAT_LAKE_NAMES:
            geom = rec.geometry
            polys = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
            for poly in polys:
                coords = list(poly.exterior.coords)
                merc_coords = [(lon, float(_lat_to_merc(lat))) for lon, lat in coords]
                codes = (
                    [mpath.Path.MOVETO]
                    + [mpath.Path.LINETO] * (len(merc_coords) - 2)
                    + [mpath.Path.CLOSEPOLY]
                )
                paths.append(mpath.Path(merc_coords, codes))

    _GREAT_LAKES_PATHS = paths
    logger.info("Built %d Great Lakes polygon paths", len(paths))
    return _GREAT_LAKES_PATHS


def _apply_great_lakes(ax):
    """Draw Great Lakes as blue fills above data, below mask."""
    for lake_path in _build_great_lakes_paths():
        patch = mpatches.PathPatch(
            lake_path,
            facecolor=LAKE_COLOR,
            edgecolor="none",
            linewidth=0,
            zorder=45,  # above data, below mask (50)
        )
        ax.add_patch(patch)


# ---------------------------------------------------------------------------
# State boundaries + major roads — drawn on top of data, clipped by mask
# ---------------------------------------------------------------------------
_STATE_LINES = None
_ROAD_LINES = None


def _build_state_lines():
    """Load US state boundary lines from Natural Earth, in Mercator Y."""
    global _STATE_LINES
    if _STATE_LINES is not None:
        return _STATE_LINES

    from shapely.geometry import box as sbox
    domain = sbox(HRRR_WEST, HRRR_SOUTH, HRRR_EAST, HRRR_NORTH)

    shpfile = shpreader.natural_earth(
        resolution="50m", category="cultural", name="admin_1_states_provinces_lines"
    )
    reader = shpreader.Reader(shpfile)

    lines = []
    for rec in reader.records():
        geom = rec.geometry
        try:
            clipped = geom.intersection(domain)
        except Exception:
            continue
        if clipped.is_empty:
            continue

        parts = list(clipped.geoms) if hasattr(clipped, "geoms") else [clipped]
        for part in parts:
            if hasattr(part, "coords"):
                coords = list(part.coords)
            elif hasattr(part, "exterior"):
                coords = list(part.exterior.coords)
            else:
                continue
            merc = [(lon, float(_lat_to_merc(lat))) for lon, lat in coords]
            lines.append(merc)

    _STATE_LINES = lines
    logger.info("Built %d state boundary line segments", len(lines))
    return _STATE_LINES


def _build_road_lines():
    """Load US major highways from Natural Earth 10m, in Mercator Y."""
    global _ROAD_LINES
    if _ROAD_LINES is not None:
        return _ROAD_LINES

    from shapely.geometry import box as sbox
    domain = sbox(HRRR_WEST, HRRR_SOUTH, HRRR_EAST, HRRR_NORTH)

    shpfile = shpreader.natural_earth(
        resolution="10m", category="cultural", name="roads"
    )
    reader = shpreader.Reader(shpfile)

    lines = []
    for rec in reader.records():
        if rec.attributes.get("sov_a3") != "USA":
            continue
        rtype = rec.attributes.get("type", "")
        if rtype not in ("Major Highway", "Beltway"):
            continue

        geom = rec.geometry
        try:
            clipped = geom.intersection(domain)
        except Exception:
            continue
        if clipped.is_empty:
            continue

        parts = list(clipped.geoms) if hasattr(clipped, "geoms") else [clipped]
        for part in parts:
            if hasattr(part, "coords"):
                coords = list(part.coords)
            else:
                continue
            merc = [(lon, float(_lat_to_merc(lat))) for lon, lat in coords]
            lines.append(merc)

    _ROAD_LINES = lines
    logger.info("Built %d major road line segments", len(lines))
    return _ROAD_LINES


def _apply_boundaries_and_roads(ax):
    """Draw state boundaries and major highways above data, below mask."""
    from matplotlib.collections import LineCollection

    # State boundaries — black
    state_lines = _build_state_lines()
    if state_lines:
        lc = LineCollection(
            state_lines,
            colors=[(0, 0, 0, 0.8)],
            linewidths=1.0,
            zorder=46,
        )
        ax.add_collection(lc)

    # Major highways — dark gray
    road_lines = _build_road_lines()
    if road_lines:
        lc = LineCollection(
            road_lines,
            colors=[(0, 0, 0, 0.5)],
            linewidths=0.5,
            zorder=47,
        )
        ax.add_collection(lc)


# ---------------------------------------------------------------------------
# NWS Composite Reflectivity colormap
# ---------------------------------------------------------------------------
_NWS_REFL_BOUNDS = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75]
_NWS_REFL_COLORS = [
    "#66d9ff",  #   5 to 10  cyan
    "#0099cc",  #  10 to 15  blue-cyan
    "#00cc00",  #  15 to 20  green
    "#009900",  #  20 to 25  dark green
    "#ffff00",  #  25 to 30  yellow
    "#ffcc00",  #  30 to 35  amber
    "#ff9900",  #  35 to 40  orange
    "#ff0000",  #  40 to 45  red
    "#cc0000",  #  45 to 50  dark red
    "#990000",  #  50 to 55  very dark red
    "#ff00ff",  #  55 to 60  magenta
    "#cc00cc",  #  60 to 65  dark magenta
    "#990099",  #  65 to 70  purple-magenta
    "#ffffff",  #  70 to 75  white (extreme)
]
_NWS_REFL_CMAP = ListedColormap(_NWS_REFL_COLORS)
_NWS_REFL_NORM = BoundaryNorm(_NWS_REFL_BOUNDS, _NWS_REFL_CMAP.N)


# Overlay image size — width in pixels at RENDER_DPI
OVERLAY_WIDTH = 1920

# Full HRRR domain bounds (lat/lon) — where actual data lives
HRRR_SOUTH = 21.0
HRRR_NORTH = 53.0
HRRR_WEST = -135.0
HRRR_EAST = -60.0

# Extended overlay bounds — extra margin so the opaque mask covers any
# visible map area beyond the HRRR data (eliminates edge stripe).
OVERLAY_SOUTH = 17.0
OVERLAY_NORTH = 55.0
OVERLAY_WEST = -138.0
OVERLAY_EAST = -57.0

# Mercator Y equivalents for the extended overlay bounds
MERC_SOUTH = float(_lat_to_merc(OVERLAY_SOUTH))
MERC_NORTH = float(_lat_to_merc(OVERLAY_NORTH))


class OverlayRenderer:
    """Renders HRRR parameters as PNG overlays in Web Mercator Y-space."""

    # High-res regrid (fits in ~8GB RAM with 1 render worker)
    REGRID_NY = 875
    REGRID_NX = 1500

    def __init__(self):
        self._bounds_cache = None
        # Target grid: x = longitude (linear), y = Mercator Y (linear)
        # Uses extended overlay bounds so the image covers the full Leaflet view.
        merc_y = np.linspace(MERC_SOUTH, MERC_NORTH, self.REGRID_NY)
        lon_1d = np.linspace(OVERLAY_WEST, OVERLAY_EAST, self.REGRID_NX)
        self._grid_lon, self._grid_merc_y = np.meshgrid(lon_1d, merc_y)
        # Corresponding lat grid (for interpolation source lookup)
        self._grid_lat = np.degrees(
            2 * np.arctan(np.exp(np.radians(self._grid_merc_y))) - np.pi / 2
        )

    def _regrid(self, lon, lat, data):
        """Regrid data onto a regular Mercator grid.

        Handles both 2D grids (HRRR LCC) and 1D lat/lon arrays (IFS, GFS).
        """
        grid_lon = self._grid_lon
        grid_merc_y = self._grid_merc_y
        grid_lat = self._grid_lat

        # Handle 1D lat/lon (regular lat-lon grids like IFS/GFS)
        if lon.ndim == 1 and lat.ndim == 1:
            lon, lat = np.meshgrid(lon, lat)

        # Ensure all inputs have the same shape before flattening
        # Extra leading dim from cfgrib — take first slice
        while data.ndim > lon.ndim and data.shape[0] <= 2:
            data = data[0]
        if lon.shape != lat.shape or lon.shape != data.shape:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                "Shape mismatch in _regrid: lon=%s, lat=%s, data=%s",
                lon.shape, lat.shape, data.shape
            )
            try:
                lon = np.broadcast_to(lon, data.shape)
                lat = np.broadcast_to(lat, data.shape)
            except ValueError:
                logger.error("Cannot broadcast lat/lon to data shape, skipping")
                return grid_lon, grid_merc_y, np.full_like(grid_lon, np.nan)

        lon_flat = np.where(lon > 180, lon - 360, lon).ravel()
        lat_flat = lat.ravel()
        data_flat = data.ravel()

        valid = np.isfinite(data_flat)
        if not valid.any():
            return grid_lon, grid_merc_y, np.full_like(grid_lon, np.nan)

        result = griddata(
            (lon_flat[valid], lat_flat[valid]),
            data_flat[valid],
            (grid_lon, grid_lat),
            method="nearest",
        )

        # Mask out areas outside the actual HRRR domain so extended
        # overlay margins show the background color, not extrapolated data.
        # Slight inward margin (0.5°) accounts for LCC grid curvature at edges.
        outside = (
            (grid_lat < HRRR_SOUTH + 0.5)
            | (grid_lat > HRRR_NORTH - 0.5)
            | (grid_lon < HRRR_WEST + 0.5)
            | (grid_lon > HRRR_EAST + 0.5)
        )
        result[outside] = np.nan

        return grid_lon, grid_merc_y, result

    def _make_fig_ax(self):
        """Create a figure and axes in Mercator Y-space."""
        merc_w = OVERLAY_EAST - OVERLAY_WEST
        merc_h = MERC_NORTH - MERC_SOUTH
        fig_w = OVERLAY_WIDTH / RENDER_DPI
        fig_h = fig_w * (merc_h / merc_w)

        fig = plt.figure(figsize=(fig_w, fig_h), dpi=RENDER_DPI)
        fig.patch.set_facecolor((0, 0, 0, 0))
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.set_xlim(OVERLAY_WEST, OVERLAY_EAST)
        ax.set_ylim(MERC_SOUTH, MERC_NORTH)
        return fig, ax

    def _save_fig(self, fig, output_path):
        """Save figure as opaque PNG (single pass, fast)."""
        try:
            fig.savefig(
                str(output_path), dpi=RENDER_DPI,
                pad_inches=0, transparent=True,
            )
            return True
        except Exception as exc:
            logger.error("Failed to save overlay %s: %s", output_path, exc)
            return False
        finally:
            plt.close(fig)

    def _make_cmap_norm(self, param: str):
        cfg = COLOR_SCALES.get(param, {})
        cmap_name = cfg.get("cmap")
        vmin = cfg.get("vmin", 0)
        vmax = cfg.get("vmax", 100)

        if cmap_name == "nws_refl":
            cmap = _NWS_REFL_CMAP.copy()
            cmap.set_under((0, 0, 0, 0))
            cmap.set_bad((0, 0, 0, 0))
            return cmap, _NWS_REFL_NORM

        cmap = plt.get_cmap(cmap_name).copy() if cmap_name else None
        if cmap:
            # Check for reverse colormap option
            if cfg.get("reverse"):
                cmap = cmap.reversed()
            # Transparent background for sparse/threshold params
            if param == "precip":
                cmap.set_under((0, 0, 0, 0))
                vmin = 0.01
            elif param == "cape":
                cmap.set_under((0, 0, 0, 0))
                vmin = 10
            elif param in ("srh_01km", "srh_03km"):
                cmap.set_under((0, 0, 0, 0))
                vmin = 25
            elif param in ("shear_06km", "shear_01km"):
                cmap.set_under((0, 0, 0, 0))
                vmin = 5
            elif param in ("kuchera_6hr", "kuchera_12hr", "snow10_12hr"):
                cmap.set_under((0, 0, 0, 0))
                vmin = 0.1
            elif param == "stp":
                cmap.set_under((0, 0, 0, 0))
                vmin = 0.5
            elif param == "scp":
                cmap.set_under((0, 0, 0, 0))
                vmin = 0.5
            elif param == "vil":
                cmap.set_under((0, 0, 0, 0))
                vmin = 1
            elif param == "uh_25km":
                cmap.set_under((0, 0, 0, 0))
                vmin = 25
        norm = mcolors.Normalize(vmin=vmin, vmax=vmax) if cmap else None
        return cmap, norm

    def get_data_bounds(self, lat, lon):
        """Return extended overlay bounds for Leaflet ImageOverlay."""
        return OVERLAY_SOUTH, OVERLAY_NORTH, OVERLAY_WEST, OVERLAY_EAST

    def render_overlay(self, param: str, data_dict: dict, output_path: Path) -> bool:
        """Render a single parameter as a PNG overlay. Returns True on success."""
        data = data_dict["data"]
        lat = data_dict["lat"]
        lon = data_dict["lon"]

        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)

        cmap, norm = self._make_cmap_norm(param)

        if param == "mslp":
            lon_n = np.where(lon > 180, lon - 360, lon)
            return self._render_mslp_overlay(lon_n, lat, data, output_path)

        if cmap is None:
            return False

        is_refl = param == "reflectivity"
        grid_lon, grid_merc_y, grid_data = self._regrid(lon, lat, data)

        fig, ax = self._make_fig_ax()

        if is_refl:
            ax.set_facecolor('#000000')

        ax.pcolormesh(
            grid_lon, grid_merc_y, grid_data, cmap=cmap, norm=norm,
            shading="nearest" if is_refl else "gouraud", rasterized=True,
        )

        _apply_great_lakes(ax)
        _apply_usa_mask(ax)
        return self._save_fig(fig, output_path)

    def render_wind_overlay(
        self, data_dict_u: dict, data_dict_v: dict, output_path: Path,
    ) -> bool:
        """Render wind speed fill + streamlines as overlay."""
        u = data_dict_u["data"]
        v = data_dict_v["data"]
        lat = data_dict_u["lat"]
        lon = data_dict_u["lon"]
        lon_n = np.where(lon > 180, lon - 360, lon)

        speed = np.sqrt(u**2 + v**2)
        cfg = COLOR_SCALES["wind_speed"]
        cmap = plt.get_cmap(cfg["cmap"]).copy()
        norm = mcolors.Normalize(vmin=cfg["vmin"], vmax=cfg["vmax"])

        output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)

        grid_lon, grid_merc_y, grid_speed = self._regrid(lon, lat, speed)

        fig, ax = self._make_fig_ax()

        ax.pcolormesh(
            grid_lon, grid_merc_y, grid_speed, cmap=cmap, norm=norm,
            shading="gouraud", rasterized=True,
        )

        # Rotate Lambert grid-relative winds approximately to earth-relative
        LON_0 = -97.5
        PARALLEL_SIN = np.sin(np.radians(38.5))
        angle = PARALLEL_SIN * np.radians(lon_n - LON_0)
        u_earth = u * np.cos(angle) + v * np.sin(angle)
        v_earth = -u * np.sin(angle) + v * np.cos(angle)

        # Create coordinate grids for streamplot
        lat_merc = _lat_to_merc(lat)
        
        # Mask out invalid data
        mask = np.isfinite(u_earth) & np.isfinite(v_earth) & (speed >= 3.0)
        u_masked = np.where(mask, u_earth, np.nan)
        v_masked = np.where(mask, v_earth, np.nan)
        
        # Draw streamlines colored by wind speed
        ax.streamplot(
            lon_n, lat_merc, u_masked, v_masked,
            color=speed,
            cmap=cmap,
            norm=norm,
            density=1.5,
            linewidth=1.0,
            arrowsize=1.0,
            arrowstyle="->",
            minlength=0.3,
        )

        _apply_great_lakes(ax)
        _apply_usa_mask(ax)
        return self._save_fig(fig, output_path)

    def _render_mslp_overlay(self, lon, lat, data, output_path):
        """Contour-only MSLP as overlay."""
        fig, ax = self._make_fig_ax()

        # Convert contour lat coords to Mercator Y
        lat_merc = _lat_to_merc(lat)

        # Draw all contours (2mb interval) - thinner lines
        levels_all = np.arange(900, 1060, 2)
        ax.contour(
            lon, lat_merc, data, levels=levels_all,
            colors="white", linewidths=0.5, alpha=0.9,
        )

        # Draw bold contours (4mb interval) - thicker lines
        levels_bold = np.arange(900, 1060, 4)
        cs = ax.contour(
            lon, lat_merc, data, levels=levels_bold,
            colors="white", linewidths=1.5, alpha=0.9,
        )
        ax.clabel(
            cs,
            levels=levels_bold,
            inline=True,
            fontsize=7,
            fmt="%d",
            colors="white",
        )

        # --- H/L pressure center detection ---
        # Inner window for local extrema, outer window for prominence baseline
        inner, outer = 5, 15

        # Pad if data is small
        if data.shape[0] < outer or data.shape[1] < outer:
            inner, outer = 3, 7

        local_min = minimum_filter(data, size=inner)
        local_max = maximum_filter(data, size=inner)

        # Prominence: how much the center stands out from surroundings
        outer_min = minimum_filter(data, size=outer)
        outer_max = maximum_filter(data, size=outer)

        # Use np.isclose for floating-point safe equality check
        atol = 0.05  # hPa tolerance
        is_low = np.isclose(data, local_min, atol=atol)
        # Prominence for lows: local min - outer minimum
        low_prominence = local_min - outer_min

        is_high = np.isclose(data, local_max, atol=atol)
        # Prominence for highs: outer maximum - local max
        high_prominence = outer_max - local_max

        # Minimum prominence threshold (2mb - synoptic-scale systems)
        min_prom = 2.0

        # DEBUG print
        import sys
        print(
            f"[MSLP DEBUG] min={data.min():.1f} max={data.max():.1f} "
            f"shape={data.shape}  is_low={int(np.sum(is_low))} is_high={int(np.sum(is_high))}  "
            f"low_prom_max={low_prominence.max():.2f} high_prom_max={high_prominence.max():.2f}",
            file=sys.stderr,
        )

        low_mask = is_low & (low_prominence >= min_prom)
        high_mask = is_high & (high_prominence >= min_prom)
        print(f"[MSLP DEBUG] after threshold: lows={int(np.sum(low_mask))} highs={int(np.sum(high_mask))}", file=sys.stderr)

        # Suppress weaker H/L near stronger ones (within 10 grid points ~100km)
        suppression_radius = 10

        # For lows: keep only the strongest in each suppression zone
        ys, xs = np.where(low_mask)
        if len(xs) > 0:
            vals = data[low_mask]
            sort_idx = np.argsort(vals)  # lowest first
            used = np.zeros_like(data, dtype=bool)
            for i in sort_idx:
                y, x = ys[i], xs[i]
                if not used[y, x]:
                    # Mark neighbors as suppressed
                    dy = np.abs(np.arange(data.shape[0])[:, None] - y)
                    dx = np.abs(np.arange(data.shape[1])[None, :] - x)
                    suppressed = (dy < suppression_radius) & (dx < suppression_radius)
                    used |= suppressed
                    # Clear suppressed points from low_mask
                    low_mask[suppressed] = False
                    # Re-set this point
                    low_mask[y, x] = True

        # For highs: keep only the strongest in each suppression zone
        ys_h, xs_h = np.where(high_mask)
        if len(xs_h) > 0:
            vals_h = data[high_mask]
            sort_idx_h = np.argsort(vals_h)[::-1]  # highest first
            used_h = np.zeros_like(data, dtype=bool)
            for i in sort_idx_h:
                y, x = ys_h[i], xs_h[i]
                if not used_h[y, x]:
                    dy = np.abs(np.arange(data.shape[0])[:, None] - y)
                    dx = np.abs(np.arange(data.shape[1])[None, :] - x)
                    suppressed_h = (dy < suppression_radius) & (dx < suppression_radius)
                    used_h |= suppressed_h
                    high_mask[suppressed_h] = False
                    high_mask[y, x] = True

        # Plot L markers
        for y, x in zip(*np.where(low_mask)):
            plon, plat = lon[x], lat[y]
            pylon, pylat = _lat_to_merc(plat)
            ax.plot(
                plon, pylat, "w^", ms=8, mew=1.2, mec="black", zorder=10,
            )
            ax.text(
                plon - 0.3, pylat - 0.05,
                "L", fontsize=9, fontweight="bold", color="white",
                ha="center", va="top", zorder=11,
                fontfamily="DejaVu Sans",
            )

        # Plot H markers
        for y, x in zip(*np.where(high_mask)):
            plon, plat = lon[x], lat[y]
            pylon, pylat = _lat_to_merc(plat)
            ax.plot(
                plon, pylat, "ws", ms=8, mew=1.2, mec="black", zorder=10,
            )
            ax.text(
                plon - 0.3, pylat - 0.05,
                "H", fontsize=9, fontweight="bold", color="white",
                ha="center", va="top", zorder=11,
                fontfamily="DejaVu Sans",
            )

        _apply_great_lakes(ax)
        _apply_usa_mask(ax)
        return self._save_fig(fig, output_path)

    def render_all_params(
        self,
        processed: dict,
        output_dir: Path,
        fxx: int,
        run_time: datetime | None = None,
    ) -> list[str]:
        """Render all available parameters as overlays.

        Returns list of successfully rendered param names.
        """
        rendered = []
        fxx_str = f"f{fxx:02d}"

        # Component fields that are only used to derive other params
        _SKIP_PARAMS = {"ugrd_10m", "vgrd_10m", "ushear_06km", "vshear_06km", "ushear_01km", "vshear_01km"}

        for param, field in processed.items():
            if param in _SKIP_PARAMS:
                continue

            out_path = output_dir / param / f"{fxx_str}.png"
            if out_path.exists():
                rendered.append(param)
                continue

            ok = self.render_overlay(param, field, out_path)
            if ok:
                rendered.append(param)

        # Wind speed with barbs
        if "ugrd_10m" in processed and "vgrd_10m" in processed:
            out_path = output_dir / "wind_speed" / f"{fxx_str}.png"
            if not out_path.exists():
                ok = self.render_wind_overlay(
                    processed["ugrd_10m"], processed["vgrd_10m"], out_path,
                )
                if ok:
                    rendered.append("wind_speed")
            else:
                rendered.append("wind_speed")

        # Derived shear magnitudes
        from .models.hrrr import DERIVED_PARAMS
        for derived, (u_key, v_key) in DERIVED_PARAMS.items():
            if derived == "wind_speed":
                continue  # handled above
            if derived in processed:
                out_path = output_dir / derived / f"{fxx_str}.png"
                if not out_path.exists():
                    ok = self.render_overlay(derived, processed[derived], out_path)
                    if ok:
                        rendered.append(derived)
                else:
                    rendered.append(derived)

        # Save bounds metadata on first render
        bounds_file = output_dir / "bounds.json"
        if not bounds_file.exists() and processed:
            import json
            first = next(iter(processed.values()))
            s, n, w, e = self.get_data_bounds(first["lat"], first["lon"])
            bounds_file.write_text(json.dumps({
                "south": s, "north": n, "west": w, "east": e,
            }))

        return rendered
