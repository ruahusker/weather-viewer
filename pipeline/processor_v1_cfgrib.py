"""GRIB2 field extraction and unit conversion using cfgrib/xarray."""
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import xarray as xr

from .models.hrrr import COMPOSITE_PARAMS, DERIVED_PARAMS
from .models.hrrr import GRIB_SHORTNAMES as _HRRR_SHORTNAMES
from .models.hrrr import LEVEL_TYPES as _HRRR_LEVELS
from .models.ecmwf import GRIB_SHORTNAMES as _IFS_SHORTNAMES
from .models.ecmwf import LEVEL_TYPES as _IFS_LEVELS

# Build list of all model lookups for multi-model fallback
_ALL_MODEL_SHORTNAMES = [_HRRR_SHORTNAMES, _IFS_SHORTNAMES]
_ALL_MODEL_LEVELS = [_HRRR_LEVELS, _IFS_LEVELS]

try:
    from .models.nam3km import PARAM_SEARCH_STRINGS as _NAM_PARAMS
    # NAM 3km uses same shortNames as HRRR for most params,
    # but MSLP is 'prmsl' not 'mslma'
    _NAM_SHORTNAMES = {
        "mslp": "prmsl",
        "mucin": "cin",
    }
    _NAM_LEVELS = {
        "mslp": {"typeOfLevel": "meanSea"},
        "mucin": {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
    }
    _ALL_MODEL_SHORTNAMES.append(_NAM_SHORTNAMES)
    _ALL_MODEL_LEVELS.append(_NAM_LEVELS)
except ImportError:
    pass

# Primary lookup (HRRR as base)
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

logger = logging.getLogger(__name__)

# Snow accumulation products that need lookback GRIBs
SNOW_PRODUCTS = {
    "kuchera_6hr":  {"hours": 6, "method": "kuchera"},
    "kuchera_12hr": {"hours": 12, "method": "kuchera"},
    "snow10_12hr":  {"hours": 12, "method": "10:1"},
}


def _filter_keys(param: str) -> dict:
    """Build cfgrib filter_by_keys for the given parameter."""
    keys = {"shortName": GRIB_SHORTNAMES[param]}
    keys.update(LEVEL_TYPES[param])
    return keys


def _kuchera_slr(t2m_c, t850_c):
    """Compute Kuchera (2003) snow-to-liquid ratio.

    t2m_c:  2m temperature in Celsius
    t850_c: 850mb temperature in Celsius
    Returns: SLR array (0 where no snow expected)
    """
    # Base SLR from 850mb temperature
    slr = np.where(
        t850_c > -0.5, 12.0,
        np.where(
            t850_c > -9.0,  12.0 + (-t850_c - 0.5) * 0.5,
            np.where(
                t850_c > -18.0, 16.25 + (-t850_c - 9.0) * 0.42,
                np.where(
                    t850_c > -25.0, 20.0,
                    20.0 - (-t850_c - 25.0) * 0.5
                )
            )
        )
    )
    # No snow if surface temp too warm (> ~1.5 C)
    slr = np.where(t2m_c > 1.5, 0.0, slr)
    return np.clip(slr, 0.0, 25.0)


class FieldProcessor:
    def load_field(self, grib_path: Path, param: str) -> xr.DataArray | None:
        """Open a single field from a GRIB2 file using cfgrib.

        Tries the primary filter keys first, then falls back to shortName-only,
        then tries alternate model shortNames (IFS vs HRRR).
        """
        # Collect all possible shortNames and level filters for this param
        candidates = []
        # Full filter (shortName + level) from each model
        for sn_dict, lv_dict in zip(_ALL_MODEL_SHORTNAMES, _ALL_MODEL_LEVELS):
            if param in sn_dict and param in lv_dict:
                fk = {"shortName": sn_dict[param], **lv_dict[param]}
                if fk not in candidates:
                    candidates.append(fk)
        # Also primary merged lookup
        if param in GRIB_SHORTNAMES and param in LEVEL_TYPES:
            fk = {"shortName": GRIB_SHORTNAMES[param], **LEVEL_TYPES[param]}
            if fk not in candidates:
                candidates.append(fk)
        # shortName-only fallback from each model
        for sn_dict in _ALL_MODEL_SHORTNAMES:
            if param in sn_dict:
                sn_only = {"shortName": sn_dict[param]}
                if sn_only not in candidates:
                    candidates.append(sn_only)

        if not candidates:
            logger.warning("Unknown param %s — no shortName mapping", param)
            return None

        for fkeys in candidates:
            try:
                ds = xr.open_dataset(
                    str(grib_path),
                    engine="cfgrib",
                    backend_kwargs={"filter_by_keys": fkeys},
                    indexpath=None,
                )
                vlist = list(ds.data_vars)
                if vlist:
                    da = ds[vlist[0]].load()
                    ds.close()
                    return da
                ds.close()
            except Exception:
                continue

        logger.warning("Could not load %s from %s", param, grib_path.name)
        return None

    def extract_all_params(self, grib_path: Path, params: list[str]) -> dict[str, xr.DataArray]:
        """Extract multiple parameters from a GRIB2 file."""
        result = {}
        for param in params:
            da = self.load_field(grib_path, param)
            if da is not None:
                result[param] = da
            else:
                logger.warning("Skipping %s — not found in %s", param, grib_path.name)
        return result

    def _get_latlon(self, da: xr.DataArray) -> tuple[np.ndarray, np.ndarray]:
        """Extract 2D lat/lon arrays from a DataArray (HRRR is Lambert Conformal)."""
        if "latitude" in da.coords:
            lat = da.coords["latitude"].values
            lon = da.coords["longitude"].values
        elif "lat" in da.coords:
            lat = da.coords["lat"].values
            lon = da.coords["lon"].values
        else:
            raise ValueError("No lat/lon coordinates found in DataArray")
        return lat, lon

    def _get_valid_time(self, da: xr.DataArray) -> datetime | None:
        """Extract valid_time as a Python datetime."""
        if "valid_time" in da.coords:
            vt = da.coords["valid_time"].values
            return datetime.utcfromtimestamp(int(vt) // 1_000_000_000)
        if "time" in da.coords:
            t = da.coords["time"].values
            return datetime.utcfromtimestamp(int(t) // 1_000_000_000)
        return None

    def _convert_units(self, param: str, data: np.ndarray) -> np.ndarray:
        """Apply unit conversions to raw GRIB2 data."""
        if param in ("temp_2m", "dewpoint_2m"):
            # Kelvin -> F
            return (data - 273.15) * 9.0 / 5.0 + 32.0
        if param in ("ugrd_10m", "vgrd_10m"):
            # m/s -> mph
            return data * 2.237
        if param == "precip":
            # kg/m2 -> inches  (1 kg/m2 = 1 mm; 1 in = 25.4 mm)
            return data / 25.4
        if param == "mslp":
            # Pa -> mb
            return data / 100.0
        if param == "wind_gusts":
            # m/s -> mph
            return data * 2.237
        if param in ("ushear_06km", "vshear_06km", "ushear_01km", "vshear_01km"):
            # 1/s shear components -> knots (m/s * 1.944)
            return data * 1.944
        if param == "visibility":
            # m -> miles
            return data / 1609.344
        if param == "snow_depth":
            # m -> inches
            return data * 39.3701
        if param == "frzlvl":
            # m -> feet
            return data * 3.28084
        if param == "max_wind":
            # m/s -> mph
            return data * 2.237
        if param == "lcl":
            # gpm -> feet AGL
            return data * 3.28084
        if param == "pwat":
            # kg/m2 (mm) -> inches
            return data / 25.4
        if param == "snowfall":
            # m of water equivalent -> inches
            return data * 39.3701
        if param == "gh_500mb":
            # gpm -> decameters
            return data / 10.0
        if param == "rh_700mb":
            # already in %, but clamp
            return np.clip(data, 0, 100)
        # cape, cin, mlcape, mlcin, mucape, cape_03km, reflectivity, srh_*,
        # lifted_index, frozen_pct, temp_850mb (kept in K), efhl, uh_*,
        # cloud_cover, lightning — no conversion
        return data

    def _load_raw_precip(self, grib_path: Path) -> np.ndarray | None:
        """Load raw accumulated precip in inches from a GRIB file."""
        da = self.load_field(grib_path, "precip")
        if da is None:
            return None
        return self._convert_units("precip", da.values.astype(np.float32))

    def _compute_snow_products(
        self,
        result: dict,
        grib_path: Path,
        fxx: int,
        lookback_gribs: dict[int, Path],
    ):
        """Compute Kuchera and 10:1 snow accumulation products."""
        # Load current raw (accumulated) precip directly from GRIB
        # (result["precip"] is already differenced to hourly)
        da_precip = self.load_field(grib_path, "precip")
        if da_precip is None:
            return
        raw_apcp = da_precip.values.astype(np.float32)
        while raw_apcp.ndim > 2 and raw_apcp.shape[0] <= 2:
            raw_apcp = raw_apcp[0]
        current_apcp = self._convert_units("precip", raw_apcp)
        lat, lon = self._get_latlon(da_precip)
        valid_time = self._get_valid_time(da_precip)

        # Load 850mb temp in Celsius for Kuchera
        t850_c = None
        if "temp_850mb" in result:
            # temp_850mb is in Kelvin (no conversion applied)
            t850_c = result["temp_850mb"]["data"] - 273.15
        elif fxx > 0:
            da_t850 = self.load_field(grib_path, "temp_850mb")
            if da_t850 is not None:
                raw_t850 = da_t850.values.astype(np.float32)
                while raw_t850.ndim > 2 and raw_t850.shape[0] <= 2:
                    raw_t850 = raw_t850[0]
                t850_c = raw_t850 - 273.15

        # 2m temp in Celsius for Kuchera surface check
        t2m_c = None
        if "temp_2m" in result:
            # temp_2m is in Fahrenheit — convert back
            t2m_c = (result["temp_2m"]["data"] - 32.0) * 5.0 / 9.0

        for product, cfg in SNOW_PRODUCTS.items():
            hours = cfg["hours"]
            lookback_fxx = fxx - hours

            if lookback_fxx < 0:
                continue

            # Get lookback accumulated precip
            if lookback_fxx == 0:
                prev_apcp = np.zeros_like(current_apcp)
            elif lookback_fxx in lookback_gribs:
                prev_apcp = self._load_raw_precip(lookback_gribs[lookback_fxx])
                if prev_apcp is None:
                    continue
            else:
                continue

            # N-hour QPF in inches
            qpf = np.maximum(current_apcp - prev_apcp, 0.0)

            if cfg["method"] == "kuchera":
                if t850_c is None or t2m_c is None:
                    continue
                slr = _kuchera_slr(t2m_c, t850_c)
                snow_inches = qpf * slr
            else:
                # 10:1 ratio
                snow_inches = qpf * 10.0

            result[product] = {
                "data": snow_inches,
                "lat": lat,
                "lon": lon,
                "valid_time": valid_time,
            }

    def process_for_render(
        self,
        grib_path: Path,
        params: list[str],
        fxx: int = 0,
        prev_precip_grib: Path | None = None,
        lookback_gribs: dict[int, Path] | None = None,
    ) -> dict:
        """
        Extract and convert all requested parameters.

        Returns dict: {param: {"data": ndarray, "lat": ndarray, "lon": ndarray,
                                "valid_time": datetime}}

        For precip: subtracts previous fxx accumulation to give hourly amounts.
        For derived params (wind_speed, shear_*): computed from component fields.
        For snow products: uses lookback GRIBs for N-hour accumulations.
        """
        # Expand derived / composite / snow params into their component fields
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

        # Ensure temp_850mb + temp_2m are fetched if Kuchera is requested
        kuchera_requested = any(
            SNOW_PRODUCTS[p]["method"] == "kuchera" for p in snow_requested
        )
        if kuchera_requested:
            if "temp_850mb" not in fetch_params:
                fetch_params.append("temp_850mb")
            if "temp_2m" not in fetch_params:
                fetch_params.append("temp_2m")

        for derived, components in DERIVED_PARAMS.items():
            if derived in fetch_params:
                fetch_params.remove(derived)
                for comp in components:
                    if comp not in fetch_params:
                        fetch_params.append(comp)

        raw = self.extract_all_params(grib_path, fetch_params)
        result = {}

        for param, da in raw.items():
            lat, lon = self._get_latlon(da)
            valid_time = self._get_valid_time(da)
            raw_data = da.values.astype(np.float32)
            # cfgrib sometimes returns an extra leading dim (e.g. 2 levels
            # matched); take the first slice so shape matches lat/lon.
            while raw_data.ndim > 2 and raw_data.shape[0] <= 2:
                raw_data = raw_data[0]
            data = self._convert_units(param, raw_data)
            result[param] = {"data": data, "lat": lat, "lon": lon, "valid_time": valid_time}

        # Hourly precip: subtract previous run's accumulated value
        if "precip" in result and prev_precip_grib is not None and fxx > 0:
            prev_raw = self.extract_all_params(prev_precip_grib, ["precip"])
            if "precip" in prev_raw:
                raw_prev = prev_raw["precip"].values.astype(np.float32)
                while raw_prev.ndim > 2 and raw_prev.shape[0] <= 2:
                    raw_prev = raw_prev[0]
                prev_data = self._convert_units("precip", raw_prev)
                result["precip"]["data"] = np.maximum(result["precip"]["data"] - prev_data, 0.0)

        # Derived: magnitude from u/v component pairs
        for derived, (u_key, v_key) in DERIVED_PARAMS.items():
            if derived in params and u_key in result and v_key in result:
                u = result[u_key]["data"]
                v = result[v_key]["data"]
                magnitude = np.sqrt(u**2 + v**2)
                result[derived] = {
                    "data": magnitude,
                    "lat": result[u_key]["lat"],
                    "lon": result[u_key]["lon"],
                    "valid_time": result[u_key]["valid_time"],
                }

        # Composite indices (STP, SCP)
        if composite_requested:
            self._compute_composites(result, composite_requested)

        # Snow accumulation products
        if snow_requested and lookback_gribs is not None:
            self._compute_snow_products(result, grib_path, fxx, lookback_gribs)

        return result

    def _compute_composites(self, result: dict, requested: list[str]):
        """Compute composite severe weather indices from processed fields."""
        ref = None
        for key in ("cape", "cin", "srh_01km", "srh_03km"):
            if key in result:
                ref = result[key]
                break
        if ref is None:
            return

        if "stp" in requested:
            if all(k in result for k in ("cape", "cin", "srh_01km", "shear_06km")):
                cape = result["cape"]["data"]
                cin = result["cin"]["data"]  # negative values
                srh = result["srh_01km"]["data"]
                shear = result["shear_06km"]["data"]

                # Simplified STP (Thompson et al. 2003/2012, without LCL term)
                cape_term = cape / 1500.0
                srh_term = srh / 150.0
                shear_term = shear / 40.0
                cin_term = np.clip((200.0 + cin) / 150.0, 0.0, 1.0)

                stp = cape_term * srh_term * shear_term * cin_term
                stp = np.maximum(stp, 0.0)

                result["stp"] = {
                    "data": stp,
                    "lat": ref["lat"],
                    "lon": ref["lon"],
                    "valid_time": ref["valid_time"],
                }

        if "scp" in requested:
            if all(k in result for k in ("cape", "srh_03km", "shear_06km")):
                cape = result["cape"]["data"]
                srh = result["srh_03km"]["data"]
                shear = result["shear_06km"]["data"]

                # Supercell Composite Parameter (Thompson et al. 2003)
                cape_term = cape / 1000.0
                srh_term = srh / 50.0
                shear_term = shear / 40.0

                scp = cape_term * srh_term * shear_term
                scp = np.maximum(scp, 0.0)

                result["scp"] = {
                    "data": scp,
                    "lat": ref["lat"],
                    "lon": ref["lon"],
                    "valid_time": ref["valid_time"],
                }
