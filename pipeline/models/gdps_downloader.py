"""Download GDPS fields from the current ECCC MSC Datamart feed.

Files use dated directories and the MSC_GDPS naming scheme documented at
https://eccc-msc.github.io/open-data/msc-data/nwp_gdps/readme_gdps-datamart_en/.
"""

import logging
import shutil
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from ..config import GRIB_CACHE_DIR, MAX_DOWNLOAD_THREADS

logger = logging.getLogger(__name__)

# Base URL for GDPS data
BASE_URL = "https://dd.weather.gc.ca"

# Map our internal parameter names to GDPS CMC filename components
# Format: (VAR, LEVEL_TYPE, LEVEL)
# _build_url translates these GRIB abbreviations into current MSC file names.
GDPS_PARAM_MAP = {
    # Surface / 2m
    "temp_2m":       ("TMP",   "TGL", 2),
    "dewpoint_2m":   ("DPT",   "TGL", 2),
    "rh_2m":         ("RH",    "TGL", 2),
    "mslp":          ("PRMSL", "MSL", 0),
    "wind_u_10m":    ("UGRD",  "TGL", 10),
    "wind_v_10m":    ("VGRD",  "TGL", 10),
    "wind_gust_10m": ("GUST",  "TGL", 10),
    "cape_surface":  ("CAPE",  "SFC", 0),
    "cin_surface":   ("CIN",   "SFC", 0),
    "snow_depth":    ("SNOD",  "SFC", 0),
    "cloud_cover":   ("TCDC",  "SFC", 0),
    "pbl_height":    ("HPBL",  "SFC", 0),
    "precip":        ("APCP",  "SFC", 0),  # Accumulated precipitation

    # 925mb
    "temp_925mb":    ("TMP",  "ISBL", 925),
    "rh_925mb":      ("RH",   "ISBL", 925),
    "gh_925mb":      ("HGT",  "ISBL", 925),
    "wind_u_925mb":  ("UGRD", "ISBL", 925),
    "wind_v_925mb":  ("VGRD", "ISBL", 925),

    # 850mb
    "temp_850mb":    ("TMP",  "ISBL", 850),
    "rh_850mb":      ("RH",   "ISBL", 850),
    "gh_850mb":      ("HGT",  "ISBL", 850),
    "wind_u_850mb":  ("UGRD", "ISBL", 850),
    "wind_v_850mb":  ("VGRD", "ISBL", 850),
    "vort_850mb":    ("ABSV", "ISBL", 850),

    # 700mb
    "omega_700mb":   ("VVEL", "ISBL", 700),
    "temp_700mb":    ("TMP",  "ISBL", 700),
    "rh_700mb":      ("RH",   "ISBL", 700),
    "gh_700mb":      ("HGT",  "ISBL", 700),
    "wind_u_700mb":  ("UGRD", "ISBL", 700),
    "wind_v_700mb":  ("VGRD", "ISBL", 700),

    # 500mb
    "temp_500mb":    ("TMP",  "ISBL", 500),
    "gh_500mb":      ("HGT",  "ISBL", 500),
    "wind_u_500mb":  ("UGRD", "ISBL", 500),
    "wind_v_500mb":  ("VGRD", "ISBL", 500),
    "rh_500mb":      ("RH",   "ISBL", 500),
    "vort_500mb":    ("ABSV", "ISBL", 500),

    # 300mb
    "temp_300mb":    ("TMP",  "ISBL", 300),
    "gh_300mb":      ("HGT",  "ISBL", 300),
    "wind_u_300mb":  ("UGRD", "ISBL", 300),
    "wind_v_300mb":  ("VGRD", "ISBL", 300),

    # 250mb
    "temp_250mb":    ("TMP",  "ISBL", 250),
    "gh_250mb":      ("HGT",  "ISBL", 250),
    "wind_u_250mb":  ("UGRD", "ISBL", 250),
    "wind_v_250mb":  ("VGRD", "ISBL", 250),

    # 200mb
    "temp_200mb":    ("TMP",  "ISBL", 200),
    "gh_200mb":      ("HGT",  "ISBL", 200),
    "wind_u_200mb":  ("UGRD", "ISBL", 200),
    "wind_v_200mb":  ("VGRD", "ISBL", 200),
}


_VARIABLE_NAMES = {
    "TMP": "AirTemp", "DPT": "DewPoint", "RH": "RelativeHumidity",
    "PRMSL": "Pressure", "UGRD": "WindU", "VGRD": "WindV",
    "GUST": "WindGust", "CAPE": "CAPE", "CIN": "CIN",
    "SNOD": "SnowDepth", "TCDC": "TotalCloudCover",
    "HPBL": "PlanetaryBoundaryLayerHeight", "APCP": "Precip-Accum",
    "HGT": "GeopotentialHeight", "ABSV": "AbsoluteVorticity",
    "VVEL": "VerticalVelocity",
}


def _build_url(run_dt: datetime, fxx: int, var: str, level_type: str, level: int) -> str:
    """Build a dated MSC GDPS URL, including zero-padded pressure levels."""
    level_name = {
        "TGL": f"AGL-{level}m", "ISBL": f"IsbL-{level:04d}",
        "MSL": "MSL", "SFC": "Sfc",
    }[level_type]
    filename = (f"{run_dt:%Y%m%dT%H}Z_MSC_GDPS_{_VARIABLE_NAMES[var]}_"
                f"{level_name}_LatLon0.15_PT{fxx:03d}H.grib2")
    return (f"{BASE_URL}/{run_dt:%Y%m%d}/WXO-DD/model_gdps/15km/"
            f"{run_dt:%H}/{fxx:03d}/{filename}")


def _download_file(url: str, output_path: Path, timeout: int = 60) -> bool:
    """Download atomically; never promote an error page or truncated GRIB."""
    partial = output_path.with_suffix(output_path.suffix + ".part")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=timeout) as response, partial.open("wb") as target:
            shutil.copyfileobj(response, target)
        with partial.open("rb") as source:
            if source.read(4) != b"GRIB" or partial.stat().st_size < 16:
                raise ValueError("Not a GRIB file")
            source.seek(-4, 2)
            if source.read(4) != b"7777":
                raise ValueError("Truncated GRIB file")
        partial.replace(output_path)
        return True
    except Exception as exc:
        logger.debug("Download failed: %s - %s", url, exc)
        partial.unlink(missing_ok=True)
        return False


def check_availability(run_dt: datetime, fxx: int = 0) -> bool:
    """Check if GDPS data is available for a given run/fxx.

    Tries to access 2m temperature as a proxy.
    """
    url = _build_url(run_dt, fxx, "TMP", "TGL", 2)
    try:
        req = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status == 200
    except Exception:
        return False


def download_single_param(run_dt: datetime, fxx: int, param: str,
                          output_dir: Path, max_retries: int = 3) -> Path | None:
    """Download a single GDPS parameter.
    
    Returns the local path or None on failure.
    """
    if param not in GDPS_PARAM_MAP:
        logger.debug("[GDPS] Unknown parameter: %s", param)
        return None

    var, level_type, level = GDPS_PARAM_MAP[param]
    url = _build_url(run_dt, fxx, var, level_type, level)
    
    output_path = output_dir / f"{run_dt:%Y%m%d%H}_{param}_{fxx:03d}.grib2"
    
    for attempt in range(max_retries):
        if _download_file(url, output_path):
            return output_path
        import time
        time.sleep(2 ** attempt)
    
    return None


def download_forecast_hour(run_dt: datetime, fxx: int, params: list[str],
                           max_workers: int = MAX_DOWNLOAD_THREADS) -> Path | None:
    """Download all parameters for a single forecast hour and combine into one GRIB.
    
    Returns the path to the combined file, or None if no parameters downloaded.
    """
    date_str = run_dt.strftime("%Y%m%d")
    hour_str = run_dt.strftime("%H")
    
    # Output combined file path
    output_dir = GRIB_CACHE_DIR / "gdps" / date_str
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"gdps_{date_str}_{hour_str}_f{fxx:03d}.grib2"
    
    # If already exists and not empty, skip
    if output_path.exists() and output_path.stat().st_size > 1000:
        return output_path
    
    params = list(dict.fromkeys(p for p in params if p in GDPS_PARAM_MAP))
    if not params:
        return None

    # Download all parameters in parallel
    downloaded_files = []
    
    with ThreadPoolExecutor(max_workers=min(max_workers, len(params))) as executor:
        future_to_param = {
            executor.submit(download_single_param, run_dt, fxx, param, output_dir): param
            for param in params if param in GDPS_PARAM_MAP
        }
        
        for future in as_completed(future_to_param):
            param = future_to_param[future]
            try:
                path = future.result()
                if path:
                    downloaded_files.append(path)
                else:
                    logger.debug("[GDPS] Failed to download %s fxx=%03d", param, fxx)
            except Exception as exc:
                logger.warning("[GDPS] Exception downloading %s: %s", param, exc)
    
    if not downloaded_files:
        logger.warning("[GDPS] No parameters downloaded for fxx=%03d", fxx)
        return None
    
    # Combine all downloaded GRIB files into one
    try:
        partial = output_path.with_suffix(".grib2.part")
        with partial.open("wb") as outfile:
            for file_path in downloaded_files:
                try:
                    with open(file_path, "rb") as infile:
                        outfile.write(infile.read())
                except Exception as exc:
                    logger.warning("[GDPS] Error reading %s: %s", file_path, exc)
        
        partial.replace(output_path)

        # Clean up individual files
        for file_path in downloaded_files:
            try:
                file_path.unlink()
            except Exception:
                pass
        
        logger.info("[GDPS] Combined fxx=%03d with %d params → %s", 
                   fxx, len(downloaded_files), output_path)
        return output_path
        
    except Exception as exc:
        logger.error("[GDPS] Failed to combine files for fxx=%03d: %s", fxx, exc)
        return None


class GDPSDownloader:
    """GDPS-specific downloader that handles per-parameter file structure."""
    
    def __init__(self, run_dt: datetime, max_workers: int = MAX_DOWNLOAD_THREADS):
        self.run_dt = run_dt
        self.max_workers = max_workers
    
    def check_availability(self, fxx: int) -> bool:
        """Check if this forecast hour is available."""
        return check_availability(self.run_dt, fxx)
    
    def download_forecast_hour(self, fxx: int, params: list[str] | None = None) -> Path | None:
        """Download and combine all parameters for one forecast hour."""
        if params is None:
            # Default to commonly available params
            params = [
                "temp_2m", "dewpoint_2m", "mslp",
                "wind_u_10m", "wind_v_10m", "wind_gust_10m",
                "cape_surface", "cin_surface",
                "temp_850mb", "gh_850mb", "wind_u_850mb", "wind_v_850mb",
                "temp_500mb", "gh_500mb",
            ]
        return download_forecast_hour(self.run_dt, fxx, params, self.max_workers)
