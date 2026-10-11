"""GRIB2 field extraction and unit conversion.

V2: Uses eccodes directly for single-pass GRIB scanning (~45x faster than cfgrib).
Backup of V1 (cfgrib): processor_v1_cfgrib.py
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import eccodes
import numpy as np

from .models.hrrr import COMPOSITE_PARAMS as _HRRR_COMPOSITES, DERIVED_PARAMS as _HRRR_DERIVED
from .models.nam3km import COMPOSITE_PARAMS as _NAM3KM_COMPOSITES
from .models.gfs import DERIVED_PARAMS as _GFS_DERIVED
from .models.nam import DERIVED_PARAMS as _NAM_DERIVED
from .models.nam3km import DERIVED_PARAMS as _NAM3KM_DERIVED
from .models.gdps import DERIVED_PARAMS as _GDPS_DERIVED
from .models.rtma import DERIVED_PARAMS as _RTMA_DERIVED
from .models.href import DERIVED_PARAMS as _HREF_DERIVED
from .models.rrfs import DERIVED_PARAMS as _RRFS_DERIVED
from .models.gefs import DERIVED_PARAMS as _GEFS_DERIVED

# Merge all model derived params — HRRR base, extended by others
DERIVED_PARAMS = {
    **_HRRR_DERIVED,

    **_NAM3KM_DERIVED,
    **_NAM_DERIVED,
    **_GFS_DERIVED,
    **_GDPS_DERIVED,
    **_RTMA_DERIVED,
    **_HREF_DERIVED,
    **_RRFS_DERIVED,
    **_GEFS_DERIVED,
}

# Merge all model composite params
COMPOSITE_PARAMS = {
    **_HRRR_COMPOSITES,
    **_NAM3KM_COMPOSITES,
}

from .models.hrrr import GRIB_SHORTNAMES as _HRRR_SHORTNAMES
from .models.hrrr import LEVEL_TYPES as _HRRR_LEVELS
from .models.ecmwf import GRIB_SHORTNAMES as _IFS_SHORTNAMES
from .models.ecmwf import LEVEL_TYPES as _IFS_LEVELS
from .models.gfs import GRIB_SHORTNAMES as _GFS_SHORTNAMES
from .models.gfs import LEVEL_TYPES as _GFS_LEVELS
from .models.gdps import GRIB_SHORTNAMES as _GDPS_SHORTNAMES
from .models.gdps import LEVEL_TYPES as _GDPS_LEVELS
from .models.rtma import GRIB_SHORTNAMES as _RTMA_SHORTNAMES
from .models.rtma import LEVEL_TYPES as _RTMA_LEVELS
from .models.href import GRIB_SHORTNAMES as _HREF_SHORTNAMES
from .models.href import LEVEL_TYPES as _HREF_LEVELS
from .models.rrfs import GRIB_SHORTNAMES as _RRFS_SHORTNAMES
from .models.rrfs import LEVEL_TYPES as _RRFS_LEVELS
from .models.gefs import GRIB_SHORTNAMES as _GEFS_SHORTNAMES
from .models.gefs import LEVEL_TYPES as _GEFS_LEVELS

# Build list of all model lookups for multi-model fallback
_ALL_MODEL_SHORTNAMES = [
    _HRRR_SHORTNAMES,
    _IFS_SHORTNAMES,
    _GFS_SHORTNAMES,
    _GDPS_SHORTNAMES,
    _RTMA_SHORTNAMES,
    _HREF_SHORTNAMES,
    _RRFS_SHORTNAMES,
    _GEFS_SHORTNAMES,
]
_ALL_MODEL_LEVELS = [
    _HRRR_LEVELS,
    _IFS_LEVELS,
    _GFS_LEVELS,
    _GDPS_LEVELS,
    _RTMA_LEVELS,
    _HREF_LEVELS,
    _RRFS_LEVELS,
    _GEFS_LEVELS,
]

try:
    from .models.nam3km import PARAM_SEARCH_STRINGS as _NAM_PARAMS
    _NAM_SHORTNAMES = {"mslp": "prmsl", "mucin": "cin"}
    _NAM_LEVELS = {
        "mslp":         {"typeOfLevel": "meanSea"},
        "mucin":        {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
        # NAM uses atmosphereSingleLayer where HRRR uses atmosphere/cloudTop
        "reflectivity": {"typeOfLevel": "atmosphereSingleLayer"},
        "vil":          {"typeOfLevel": "atmosphereSingleLayer"},
        "echo_tops":    {"typeOfLevel": "atmosphereSingleLayer"},
        "cloud_cover":  {"typeOfLevel": "atmosphereSingleLayer"},
        # NAM lightning is on the surface level, not atmosphere
        "lightning":    {"typeOfLevel": "surface"},
    }
    _ALL_MODEL_SHORTNAMES.append(_NAM_SHORTNAMES)
    _ALL_MODEL_LEVELS.append(_NAM_LEVELS)
except ImportError:
    pass

# Primary lookup (HRRR as base, extended by other models)
GRIB_SHORTNAMES = {**_HRRR_SHORTNAMES}
LEVEL_TYPES = {**_HRRR_LEVELS}
for sn_dict in _ALL_MODEL_SHORTNAMES[1:]:
    for k, v in sn_dict.items():
        if k not in GRIB_SHORTNAMES:
            GRIB_SHORTNAMES[k] = v
for lv_dict in _ALL_MODEL_LEVELS[1:]:
    for k, v in lv_dict.items():
        if k not in LEVEL_TYPES:
            LEVEL_TYPES[k] = v

# Standard fallback for 700mb vertical velocity when a model file does not
# define explicit eccodes mappings but the GRIB contains the field.
GRIB_SHORTNAMES.setdefault("omega_700mb", "w")
LEVEL_TYPES.setdefault("omega_700mb", {"typeOfLevel": "isobaricInhPa", "level": 700})

logger = logging.getLogger(__name__)

SPECIAL_MATCH_CRITERIA = {
    # ECCC's accumulated precipitation can have an unknown shortName in
    # eccodes, but its WMO discipline/category/number still identifies it.
    "precip": [
        {"discipline": 0, "parameterCategory": 1, "parameterNumber": 8,
         "typeOfLevel": "surface", "stepType": "accum"},
    ],
    "reflectivity": [
        {
            "shortName": "refc",
            "typeOfLevel": "atmosphereSingleLayer",
        },
    ],
    "omega_700mb": [
        {
            "shortName": "w",
            "typeOfLevel": "isobaricInhPa",
            "level": 700,
            "parameterCategory": 2,
            "parameterNumber": 8,
        },
    ],
    # Some RRFS fields come through eccodes as "unknown" but can still
    # be uniquely identified by discipline/category/number plus level keys.
    "echo_tops": [
        {
            "shortName": "unknown",
            "typeOfLevel": "cloudTop",
            "parameterCategory": 16,
            "parameterNumber": 3,
        },
    ],
    "snowfall": [
        {
            "parameterCategory": 1,
            "parameterNumber": 29,
            "typeOfLevel": "surface",
        },
    ],
    "uh_25km": [
        {
            "shortName": "unknown",
            "parameterCategory": 7,
            "parameterNumber": 15,
            "typeOfLevel": "heightAboveGroundLayer",
            "topLevel": 5000,
            "bottomLevel": 2000,
        },
    ],
}

# Enable eccodes multi-field GRIB2 support so that NCEP sub-message pairs
# (e.g. RAP U/V wind components stored in one physical GRIB2 message) are
# each returned as a separate handle by codes_grib_new_from_file.
eccodes.codes_grib_multi_support_on()

# Snow accumulation products that need lookback GRIBs
SNOW_PRODUCTS = {
    "kuchera_6hr":  {"hours": 6, "method": "kuchera"},
    "kuchera_12hr": {"hours": 12, "method": "kuchera"},
    "snow10_12hr":  {"hours": 12, "method": "10:1"},
}


@dataclass(slots=True)
class GribField:
    """Lightweight container replacing xr.DataArray for GRIB field data."""
    values: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    valid_time: datetime | None = None


def _safe_get(msgid, key, default=None):
    """Get an eccodes key value, returning default if not found."""
    try:
        return eccodes.codes_get(msgid, key)
    except (eccodes.KeyValueNotFoundError, eccodes.InternalError):
        return default


def _get_valid_time(msgid) -> datetime | None:
    """Extract valid time from GRIB message."""
    vd = _safe_get(msgid, "validityDate")
    vt = _safe_get(msgid, "validityTime", 0)
    if vd is None:
        return None
    return datetime(vd // 10000, (vd % 10000) // 100, vd % 100,
                    vt // 100, vt % 100)


_latlon_cache = {}  # {(gridType, nj, ni): (lat, lon)}
_LATLON_CACHE_MAX = 10  # Cap to prevent unbounded growth


def _extract_field(msgid) -> GribField:
    """Extract values, lat, lon, valid_time from a GRIB message.

    Caches lat/lon arrays per grid shape since all messages in a model
    share the same grid (saves ~0.45s per redundant extraction).
    """
    ni = eccodes.codes_get(msgid, "Ni")
    nj = eccodes.codes_get(msgid, "Nj")
    # Use in-place conversion if possible
    vals = eccodes.codes_get_values(msgid).reshape(nj, ni).astype(np.float32, copy=False)
    if _safe_get(msgid, "alternativeRowScanning", 0) == 1:
        if _safe_get(msgid, "jPointsAreConsecutive", 0) == 0:
            vals[1::2] = vals[1::2, ::-1]
        else:
            vals[:, 1::2] = vals[::-1, 1::2]

    grid_type = _safe_get(msgid, "gridType", "unknown")
    cache_key = (grid_type, nj, ni)

    if cache_key not in _latlon_cache:
        lats = eccodes.codes_get_array(msgid, "latitudes").reshape(nj, ni)
        lons = eccodes.codes_get_array(msgid, "longitudes").reshape(nj, ni)
        # Use float32 for coordinates — 7 decimal places is plenty for weather grids
        if grid_type == "regular_ll":
            lats = lats[:, 0].astype(np.float32, copy=False)
            lons = lons[0, :].astype(np.float32, copy=False)
        else:
            lats = lats.astype(np.float32, copy=False)
            lons = lons.astype(np.float32, copy=False)
        _latlon_cache[cache_key] = (lats, lons)
        while len(_latlon_cache) > _LATLON_CACHE_MAX:
            _latlon_cache.pop(next(iter(_latlon_cache)))

    lats, lons = _latlon_cache[cache_key]

    return GribField(
        values=vals, lat=lats, lon=lons,
        valid_time=_get_valid_time(msgid),
    )


def _kuchera_slr(t2m_c, t850_c):
    """Compute Kuchera (2003) snow-to-liquid ratio."""
    slr = np.where(
        t850_c > -0.5, 12.0,
        np.where(
            t850_c > -9.0, 12.0 + (-t850_c - 0.5) * 0.5,
            np.where(
                t850_c > -18.0, 16.25 + (-t850_c - 9.0) * 0.42,
                np.where(t850_c > -25.0, 20.0, 20.0 - (-t850_c - 25.0) * 0.5)
            )
        )
    )
    slr = np.where(t2m_c > 1.5, 0.0, slr)
    return np.clip(slr, 0.0, 25.0)


class FieldProcessor:

    def _build_match_candidates(self, params: list[str]) -> list[tuple[str, list[dict]]]:
        """Build match candidates: [(param_name, [criteria_dicts]), ...]

        Each criteria dict has eccodes key names (shortName, typeOfLevel, level, etc.)
        that must ALL match for a GRIB message to be identified as that parameter.
        """
        result = []
        for param in params:
            criteria_list = []
            has_exact_mapping = False
            # Try each model's shortName + level mappings
            for sn_dict, lv_dict in zip(_ALL_MODEL_SHORTNAMES, _ALL_MODEL_LEVELS):
                if param in sn_dict and param in lv_dict:
                    c = {"shortName": sn_dict[param]}
                    c.update(lv_dict[param])
                    if c not in criteria_list:
                        criteria_list.append(c)
                        has_exact_mapping = True
            # Merged primary lookup
            if param in GRIB_SHORTNAMES and param in LEVEL_TYPES:
                c = {"shortName": GRIB_SHORTNAMES[param]}
                c.update(LEVEL_TYPES[param])
                if c not in criteria_list:
                    criteria_list.append(c)
                    has_exact_mapping = True
            # shortName-only fallback
            if not has_exact_mapping:
                for sn_dict in _ALL_MODEL_SHORTNAMES:
                    if param in sn_dict:
                        c = {"shortName": sn_dict[param]}
                        if c not in criteria_list:
                            criteria_list.append(c)
            if criteria_list:
                for extra in SPECIAL_MATCH_CRITERIA.get(param, []):
                    if extra not in criteria_list:
                        criteria_list.append(extra)
                result.append((param, criteria_list))
            else:
                logger.warning("Unknown param %s — no shortName mapping", param)
        return result

    def _msg_matches(self, msgid, criteria: dict) -> bool:
        """Check if a GRIB message matches ALL keys in criteria."""
        for key, expected in criteria.items():
            actual = _safe_get(msgid, key)
            if actual is None or actual != expected:
                return False
        return True

    def load_field(self, grib_path: Path, param: str) -> GribField | None:
        """Load a single field from a GRIB file. Scans until found."""
        candidates = self._build_match_candidates([param])
        if not candidates:
            return None
        _, criteria_list = candidates[0]

        with open(str(grib_path), "rb") as f:
            while True:
                msgid = eccodes.codes_grib_new_from_file(f)
                if msgid is None:
                    break
                try:
                    for criteria in criteria_list:
                        if self._msg_matches(msgid, criteria):
                            return _extract_field(msgid)
                finally:
                    eccodes.codes_release(msgid)

        logger.warning("Could not load %s from %s", param, grib_path.name)
        return None

    def extract_all_params(self, grib_path: Path,
                           params: list[str]) -> dict[str, GribField]:
        """Extract multiple parameters in a single pass through the GRIB file."""
        match_list = self._build_match_candidates(params)
        result = {}
        found = set()

        with open(str(grib_path), "rb") as f:
            while True:
                msgid = eccodes.codes_grib_new_from_file(f)
                if msgid is None:
                    break
                try:
                    for param, criteria_list in match_list:
                        if param in found:
                            continue
                        for criteria in criteria_list:
                            if self._msg_matches(msgid, criteria):
                                result[param] = _extract_field(msgid)
                                found.add(param)
                                break
                finally:
                    eccodes.codes_release(msgid)
                if len(found) == len(match_list):
                    break

        for param, _ in match_list:
            if param not in found:
                logger.debug("Skipping %s — not found in %s",
                             param, grib_path.name)
        return result

    def _convert_units(self, param: str, data: np.ndarray) -> np.ndarray:
        """Apply unit conversions to raw GRIB2 data. Uses in-place ops for memory."""
        # Ensure we have a float32 array we can modify in place if it's not already
        if data.dtype != np.float32:
            data = data.astype(np.float32)

        if param in ("temp_2m", "dewpoint_2m", "apparent_temp", "max_temp_1h", "min_temp_1h") or (
            param.endswith("mb") and (param.startswith("temp_") or param.startswith("dewpoint_"))
        ):
            data -= 273.15
            data *= 1.8
            data += 32.0
            return data
        if param in ("ugrd_10m", "vgrd_10m", "wind_u_10m", "wind_v_10m"):
            data *= 2.237
            return data
        if param in ("wind_speed", "wind_speed_10m", "wind_gust_10m", "wind_gusts"):
            data *= 2.237
            return data
        if (
            param.endswith("mb") and (param.startswith("ugrd_") or param.startswith("vgrd_") or param.startswith("wind_u_") or param.startswith("wind_v_"))
        ):
            data *= 1.944  # m/s → knots
            return data
        if param in ("precip", "precip_6h"):
            data /= 25.4
            return data
        if param == "mslp":
            data /= 100.0
            return data
        if param == "psfc":
            data /= 100.0
            return data
        if param in ("ushear_06km", "vshear_06km", "ushear_01km", "vshear_01km"):
            data *= 1.944
            return data
        if param == "visibility":
            data /= 1609.344
            return data
        if param == "ceil":
            data *= 3.28084
            return data
        if param == "snow_depth":
            # In-place where where possible
            mask = data >= 9990.0
            data[mask] = np.nan
            data *= 39.3701
            return data
        if param in ("frzlvl", "lcl", "pbl_height", "echo_tops"):
            data *= 3.28084  # m → ft
            return data
        if param == "max_wind":
            data *= 2.237
            return data
        if param in ("pwat", "snowfall", "snowfall_6h", "weasd", "wave_height"):
            mask = data >= 9990.0
            data[mask] = np.nan
            data /= 25.4  # mm or kg/m² → inches
            return data
        if param.startswith("gh_") and param.endswith("mb") or param in ("gh_500mb_spread",
                     "gh_500mb_anomaly"):
            return data  # keep geopotential heights/spreads in meters
        if param == "rh_2m" or (param.startswith("rh_") and param.endswith("mb")) or param in (
                     "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"):
            np.clip(data, 0, 100, out=data)
            return data
        if param == "hail":
            data *= 39.3701  # m → inches
            return data
        if param.startswith("vort_") and param.endswith("mb"):
            data *= 1.0e5  # s^-1 -> 10^-5 s^-1
            return data
        return data

    def _load_raw_precip(self, grib_path: Path) -> np.ndarray | None:
        """Load raw accumulated precip in inches from a GRIB file."""
        field = self.load_field(grib_path, "precip")
        if field is None:
            return None
        return self._convert_units("precip", field.values)

    def _compute_snow_products(self, result, grib_path, fxx, lookback_gribs,
                               accum_precip_inches=None):
        """Compute Kuchera and 10:1 snow accumulation products.

        Args:
            accum_precip_inches: Pre-extracted total-accumulated precip array (inches).
                                 When supplied, skips the redundant GRIB re-scan.
        """
        if accum_precip_inches is not None and "_precip_meta" in result:
            current_apcp = accum_precip_inches
            meta = result["_precip_meta"]
            lat, lon, valid_time = meta["lat"], meta["lon"], meta["valid_time"]
        else:
            field = self.load_field(grib_path, "precip")
            if field is None:
                return
            raw_apcp = field.values.copy()
            while raw_apcp.ndim > 2 and raw_apcp.shape[0] <= 2:
                raw_apcp = raw_apcp[0]
            current_apcp = self._convert_units("precip", raw_apcp)
            lat, lon = field.lat, field.lon
            valid_time = field.valid_time

        t850_c = None
        if "temp_850mb" in result:
            t850_f = result["temp_850mb"]["data"]
            t850_c = (t850_f - 32.0) * 5.0 / 9.0
        elif fxx > 0:
            f850 = self.load_field(grib_path, "temp_850mb")
            if f850 is not None:
                raw = f850.values
                while raw.ndim > 2 and raw.shape[0] <= 2:
                    raw = raw[0]
                t850_c = raw - 273.15

        t2m_c = None
        if "temp_2m" in result:
            t2m_c = (result["temp_2m"]["data"] - 32.0) * 5.0 / 9.0

        for product, cfg in SNOW_PRODUCTS.items():
            hours = cfg["hours"]
            lookback_fxx = fxx - hours
            if lookback_fxx < 0:
                prev_apcp = np.zeros_like(current_apcp)
            elif lookback_fxx == 0:
                prev_apcp = np.zeros_like(current_apcp)
            elif lookback_fxx in lookback_gribs:
                prev_apcp = self._load_raw_precip(lookback_gribs[lookback_fxx])
                if prev_apcp is None:
                    continue
            else:
                continue

            qpf = np.maximum(current_apcp - prev_apcp, 0.0)
            if cfg["method"] == "kuchera":
                if t850_c is None or t2m_c is None:
                    continue
                slr = _kuchera_slr(t2m_c, t850_c)
                snow_inches = qpf * slr
            else:
                snow_inches = qpf * 10.0

            result[product] = {
                "data": snow_inches, "lat": lat, "lon": lon,
                "valid_time": valid_time,
            }

    def process_for_render(self, grib_path, params, fxx=0,
                           prev_precip_grib=None, lookback_gribs=None):
        """Extract and convert all requested parameters.

        Returns dict: {param: {"data": ndarray, "lat": ndarray, "lon": ndarray,
                                "valid_time": datetime}}
        """
        fetch_params = list(params)
        snow_requested = [p for p in fetch_params if p in SNOW_PRODUCTS]
        for p in snow_requested:
            fetch_params.remove(p)
        composite_requested = [p for p in fetch_params if p in COMPOSITE_PARAMS]
        for p in composite_requested:
            fetch_params.remove(p)
            for req in COMPOSITE_PARAMS[p]["requires"]:
                if req not in fetch_params:
                    fetch_params.append(req)

        kuchera_requested = any(
            SNOW_PRODUCTS[p]["method"] == "kuchera" for p in snow_requested
        )
        if kuchera_requested:
            if "temp_850mb" not in fetch_params:
                fetch_params.append("temp_850mb")
            if "temp_2m" not in fetch_params:
                fetch_params.append("temp_2m")

        for derived, components in DERIVED_PARAMS.items():
            if derived in fetch_params and components[1] is not None:
                fetch_params.remove(derived)
                for comp in components:
                    if comp is None:
                        continue
                    if comp not in fetch_params:
                        fetch_params.append(comp)

        raw = self.extract_all_params(grib_path, fetch_params)
        result = {}

        for param in list(raw):
            field = raw.pop(param)  # pop to free raw GribField immediately
            raw_data = field.values
            while raw_data.ndim > 2 and raw_data.shape[0] <= 2:
                raw_data = raw_data[0]
            data = self._convert_units(param, raw_data)
            result[param] = {
                "data": data, "lat": field.lat, "lon": field.lon,
                "valid_time": field.valid_time,
            }
        del raw

        # Save total-accumulated precip (inches) before converting to hourly delta,
        # so _compute_snow_products can reuse it without re-scanning the GRIB.
        accum_precip_inches = None
        if "precip" in result:
            accum_precip_inches = result["precip"]["data"].copy()
            result["_precip_meta"] = {
                "lat": result["precip"]["lat"],
                "lon": result["precip"]["lon"],
                "valid_time": result["precip"]["valid_time"],
            }

        # Hourly precip: subtract previous fxx accumulation
        if "precip" in result and prev_precip_grib is not None and fxx > 0:
            prev_field = self.load_field(prev_precip_grib, "precip")
            if prev_field is not None:
                raw_prev = prev_field.values
                while raw_prev.ndim > 2 and raw_prev.shape[0] <= 2:
                    raw_prev = raw_prev[0]
                prev_data = self._convert_units("precip", raw_prev)
                result["precip"]["data"] = np.maximum(
                    result["precip"]["data"] - prev_data, 0.0)

        # Derived: magnitude from u/v component pairs
        for derived, (u_key, v_key) in DERIVED_PARAMS.items():
            if derived in params and u_key in result and v_key in result:
                u = result[u_key]["data"]
                v = result[v_key]["data"]
                # Use in-place to avoid a third large array if possible
                mag = u**2
                mag += v**2
                np.sqrt(mag, out=mag)
                result[derived] = {
                    "data": mag,
                    "lat": result[u_key]["lat"],
                    "lon": result[u_key]["lon"],
                    "valid_time": result[u_key]["valid_time"],
                }

        # Composite indices (STP, SCP)
        if composite_requested:
            self._compute_composites(result, composite_requested)

        # Snow accumulation products — pass pre-extracted accum precip to avoid GRIB re-scan
        if snow_requested and lookback_gribs is not None:
            self._compute_snow_products(result, grib_path, fxx, lookback_gribs,
                                        accum_precip_inches=accum_precip_inches)

        # Remove internal metadata key before returning
        result.pop("_precip_meta", None)

        return result

    def _compute_composites(self, result, requested):
        """Compute composite severe weather indices from processed fields."""
        ref = None
        for key in ("cape", "cin", "mlcape", "mlcin", "srh_01km", "srh_03km"):
            if key in result:
                ref = result[key]
                break
        if ref is None:
            return

        if "stp" in requested:
            cape_key = next((k for k in ("cape", "mlcape", "mucape") if k in result), None)
            cin_key = next((k for k in ("cin", "mlcin", "mucin") if k in result), None)
            if cape_key and cin_key and all(k in result for k in ("srh_01km", "shear_06km")):
                cape = result[cape_key]["data"]
                cin = result[cin_key]["data"]
                srh = result["srh_01km"]["data"]
                shear = result["shear_06km"]["data"]
                cape_term = cape / 1500.0
                srh_term = srh / 150.0
                shear_term = shear / 40.0
                cin_term = np.clip((200.0 + cin) / 150.0, 0.0, 1.0)
                stp = np.maximum(cape_term * srh_term * shear_term * cin_term, 0.0)
                result["stp"] = {
                    "data": stp, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        if "scp" in requested:
            cape_key = next((k for k in ("cape", "mlcape", "mucape") if k in result), None)
            if cape_key and all(k in result for k in ("srh_03km", "shear_06km")):
                cape = result[cape_key]["data"]
                srh = result["srh_03km"]["data"]
                shear = result["shear_06km"]["data"]
                scp = np.maximum(
                    (cape / 1000.0) * (srh / 50.0) * (shear / 40.0), 0.0)
                result["scp"] = {
                    "data": scp, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Chase Target Index (CTI)
        # Identifies tornado/supercell environments where instability, shear,
        # low-level rotation, low cloud bases, and weak cap converge.
        # Units at this point: CAPE J/kg, CIN J/kg, SRH m²/s², shear kt, LCL ft
        if "cti" in requested:
            cape_key = "mlcape" if "mlcape" in result else "cape"
            cin_key = "mlcin" if "mlcin" in result else "cin"
            reqs = (cape_key, cin_key, "srh_01km", "shear_06km", "lcl")
            if all(k in result for k in reqs):
                cape = result[cape_key]["data"]
                cin = result[cin_key]["data"]
                srh = result["srh_01km"]["data"]
                shear = result["shear_06km"]["data"]  # knots
                lcl = result["lcl"]["data"]            # feet
                cape_term = cape / 2000.0
                srh_term = srh / 200.0
                shear_term = shear / 25.0
                lcl_term = np.clip(1.0 - lcl / 6500.0, 0.0, 1.0)  # 6500 ft
                cin_term = np.clip((200.0 + cin) / 150.0, 0.0, 1.0)
                cti = np.maximum(
                    cape_term * srh_term * shear_term * lcl_term * cin_term,
                    0.0,
                )
                result["cti"] = {
                    "data": cti, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Mesocyclone Strength Index (MSI)
        # Targets persistent rotating supercells using MUCAPE (elevated storms),
        # 0-3km SRH (deeper rotation), and 0-3km CAPE (low-level buoyancy).
        if "msi" in requested:
            if all(k in result for k in ("mucape", "mucin", "srh_03km",
                                          "shear_06km", "cape_03km")):
                mucape = result["mucape"]["data"]
                mucin = result["mucin"]["data"]
                srh3 = result["srh_03km"]["data"]
                shear = result["shear_06km"]["data"]   # knots
                cape3 = result["cape_03km"]["data"]
                cape_term = mucape / 1500.0
                srh_term = srh3 / 250.0
                shear_term = shear / 35.0
                cape3_term = cape3 / 150.0
                cin_gate = np.clip((150.0 + mucin) / 100.0, 0.0, 1.0)
                msi = np.maximum(
                    cape_term * srh_term * shear_term * cape3_term * cin_gate,
                    0.0,
                )
                result["msi"] = {
                    "data": msi, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Storm Mode Index (SMI)
        # Discriminates discrete supercells (high) vs linear/QLCS (low).
        # Based on 0-1km/0-6km shear ratio (Dial et al. 2010).
        if "smi" in requested:
            if all(k in result for k in ("mlcape", "shear_01km",
                                          "shear_06km", "lcl")):
                mlcape = result["mlcape"]["data"]
                s1 = result["shear_01km"]["data"]  # knots
                s6 = result["shear_06km"]["data"]  # knots
                lcl = result["lcl"]["data"]        # feet
                shear_ratio = s1 / np.maximum(s6, 1.0)
                discrete = np.clip((s6 - 25.0) / 30.0, 0.0, 1.0)
                linear_pen = np.clip(shear_ratio / 0.5, 0.0, 1.0)
                cape_term = np.clip(mlcape / 1500.0, 0.0, 1.5)
                lcl_bonus = np.clip(1.0 - lcl / 16400.0, 0.0, 1.0)  # 5000m
                smi = cape_term * discrete * (1.0 - 0.7 * linear_pen) * lcl_bonus
                result["smi"] = {
                    "data": smi, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Significant Hail Parameter (SHiP)
        # Combines lapse rate, freezing level sweet spot, MUCAPE, and shear.
        if "ship" in requested:
            if all(k in result for k in ("mucape", "temp_700mb", "temp_500mb",
                                          "frzlvl", "shear_06km")):
                mucape = result["mucape"]["data"]
                t700 = result["temp_700mb"]["data"]   # °F
                t500 = result["temp_500mb"]["data"]   # °F
                frzl = result["frzlvl"]["data"]       # feet
                shear = result["shear_06km"]["data"]  # knots
                # Lapse rate (°F delta → °C/km approx)
                lapse_f = t700 - t500
                lapse_c = lapse_f * 5.0 / 9.0
                lapse_term = np.clip((lapse_c - 5.5) / 2.5, 0.0, 1.5)
                # Freezing level sweet spot: 10000-14000 ft optimal for hail
                frzl_term = np.where(
                    frzl < 7000, 0.3,
                    np.where(frzl < 10000, 0.7,
                    np.where(frzl < 14000, 1.0,
                    np.where(frzl < 16000, 0.6, 0.3))))
                mucape_term = np.clip(mucape / 2000.0, 0.0, 2.0)
                shear_term = np.clip(shear / 30.0, 0.0, 1.5)
                ship = mucape_term * lapse_term * frzl_term * shear_term
                result["ship"] = {
                    "data": ship, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Derecho Composite Parameter (DCP)
        # Identifies damaging wind / bow echo environments.
        # Uses surface-700mb temp difference as downdraft potential proxy.
        if "dcp" in requested:
            if all(k in result for k in ("mucape", "temp_700mb", "temp_2m",
                                          "shear_06km", "wind_gusts")):
                mucape = result["mucape"]["data"]
                t700 = result["temp_700mb"]["data"]   # °F
                t2m = result["temp_2m"]["data"]       # °F
                shear = result["shear_06km"]["data"]  # knots
                gusts = result["wind_gusts"]["data"]  # mph
                downdraft = np.clip((t2m - t700) / 80.0, 0.0, 1.5)
                mucape_term = np.clip(mucape / 2000.0, 0.0, 2.0)
                shear_term = np.clip(shear / 30.0, 0.0, 1.5)
                gust_term = np.clip(gusts / 40.0, 0.0, 1.5)
                dcp = np.maximum(
                    mucape_term * downdraft * shear_term * (0.5 + 0.5 * gust_term),
                    0.0,
                )
                result["dcp"] = {
                    "data": dcp, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Convective Initiation Timing Index (CITI)
        # Shows where cap is weakening and storms are likely to fire.
        if "citi" in requested:
            cin_key = "mlcin" if "mlcin" in result else "cin"
            if all(k in result for k in ("mlcape", cin_key,
                                          "pbl_height", "cloud_cover", "omega_700mb")):
                mlcape = result["mlcape"]["data"]
                cin = result[cin_key]["data"]
                pbl = result["pbl_height"]["data"]       # feet
                cc = result["cloud_cover"]["data"]       # %
                omega = result["omega_700mb"]["data"]    # µb/s (negative = ascent)
                cap_weakness = np.clip(1.0 - np.abs(cin) / 100.0, 0.0, 1.0)
                pbl_forcing = np.clip((pbl - 6500.0) / 13000.0, 0.0, 1.0)
                instab = np.clip(mlcape / 1000.0, 0.0, 2.0)
                cloud_factor = np.clip(1.0 - cc / 80.0, 0.2, 1.0)
                ascent_factor = np.clip((-omega - 2.0) / 10.0, 0.0, 1.0)
                citi = instab * cap_weakness * (0.45 + 0.4 * pbl_forcing + 0.35 * ascent_factor) * cloud_factor
                result["citi"] = {
                    "data": citi, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Chase Visibility Index (CVI)
        # Assesses whether a chaser can visually see and safely chase storms.
        if "cvi" in requested:
            if all(k in result for k in ("visibility", "cloud_cover", "pbl_height")):
                vis = result["visibility"]["data"]    # miles
                cc = result["cloud_cover"]["data"]    # %
                pbl = result["pbl_height"]["data"]    # feet
                vis_term = np.clip(vis / 5.0, 0.0, 1.0)
                cloud_term = np.clip(1.0 - cc / 90.0, 0.0, 1.0)
                pbl_term = np.clip(pbl / 13000.0, 0.3, 1.0)
                cvi = vis_term * (0.6 * cloud_term + 0.4 * pbl_term)
                result["cvi"] = {
                    "data": cvi, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Low-Level Moisture Transport (LLMT)
        if "llmt" in requested:
            if all(k in result for k in ("dewpoint_850mb", "wind_850mb", "rh_850mb")):
                td850 = result["dewpoint_850mb"]["data"]   # °F
                wind850 = result["wind_850mb"]["data"]     # kt
                rh850 = result["rh_850mb"]["data"]         # %
                moisture_term = np.clip((td850 - 45.0) / 20.0, 0.0, 1.5)
                flow_term = np.clip((wind850 - 10.0) / 35.0, 0.0, 1.5)
                sat_term = np.clip((rh850 - 55.0) / 35.0, 0.0, 1.2)
                llmt = moisture_term * (0.65 * flow_term + 0.35 * sat_term)
                result["llmt"] = {
                    "data": llmt, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Forcing for Ascent
        if "ascent" in requested:
            if "omega_700mb" in result:
                omega = result["omega_700mb"]["data"]   # µb/s (negative = ascent)
                ascent_term = np.clip((-omega - 1.0) / 10.0, 0.0, 2.0)
                support = np.full_like(ascent_term, 0.75, dtype=np.float32)
                if "wind_250mb" in result:
                    jet250 = result["wind_250mb"]["data"]  # kt
                    jet_term = np.clip((jet250 - 50.0) / 80.0, 0.0, 1.5)
                    support += 0.45 * jet_term.astype(np.float32)
                ascent = ascent_term * support
                result["ascent"] = {
                    "data": ascent, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }

        # Storm Obscuration / HP Risk
        if "hp_risk" in requested:
            if all(k in result for k in ("visibility", "cloud_cover", "pbl_height", "rh_700mb", "rh_850mb")):
                vis = result["visibility"]["data"]       # miles
                cc = result["cloud_cover"]["data"]       # %
                pbl = result["pbl_height"]["data"]       # feet
                rh700 = result["rh_700mb"]["data"]       # %
                rh850 = result["rh_850mb"]["data"]       # %
                low_vis = np.clip((5.0 - vis) / 4.0, 0.0, 1.5)
                cloud_term = np.clip((cc - 35.0) / 55.0, 0.0, 1.4)
                moisture_term = (
                    0.55 * np.clip((rh700 - 60.0) / 35.0, 0.0, 1.3)
                    + 0.45 * np.clip((rh850 - 65.0) / 30.0, 0.0, 1.3)
                )
                poor_mix = np.clip((4500.0 - pbl) / 3500.0, 0.0, 1.2)
                hp_risk = (0.35 * low_vis + 0.3 * cloud_term + 0.25 * moisture_term + 0.1 * poor_mix) * 1.6
                result["hp_risk"] = {
                    "data": hp_risk, "lat": ref["lat"],
                    "lon": ref["lon"], "valid_time": ref["valid_time"],
                }
