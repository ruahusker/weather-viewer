"""
GDPS (Global Deterministic Prediction System) - Environment Canada
Resolution: ~15 km (0.15° lat/lon)
Runs: 00Z, 12Z
Forecast: 0-240 hours (10 days)
Grid: Regular lat/lon, 2400x1201 points

Data source: https://dd.weather.gc.ca/model_gem_global/15km/
Alternative: https://dd.meteo.gc.ca/today/model_gem_global/15km/
"""

from datetime import datetime, timezone

MODEL_NAME = "gdps"
DISPLAY_NAME = "GDPS"
HERBIE_MODEL = "gdps"
HERBIE_PRODUCT = "latlon15x15"

# Run times - GDPS runs at 00Z and 12Z
RUN_TIMES = [0, 12]

# Resolution in degrees
RESOLUTION = 0.15

# Grid dimensions
GRID_WIDTH = 2400  # longitude points
GRID_HEIGHT = 1201  # latitude points

# Data availability delay (minutes) - GDPS typically available ~30-45 min after run time
AVAILABILITY_DELAY_MINUTES = 45

# Data retention - keep 2 runs
MAX_RETAINED_RUNS = 2

# Whether this model has snow data
HAS_SNOW = True

# Priority in scheduler (lower = higher priority)
SCHEDULER_PRIORITY = 5


def get_run_fxx(run_hour: int) -> range:
    """Return forecast hour range for a given run hour."""
    if run_hour == 0:
        # 00Z GDPS publishes the full 10-day 3-hourly range.
        return range(0, 241, 3)
    if run_hour == 12:
        # 12Z GDPS on the current Environment Canada lat/lon feed ends at f114.
        return range(0, 115, 3)
    return range(0)


def get_model_url(run_date: datetime, run_hour: int, fxx: int) -> str:
    """
    Return the URL pattern for GDPS data.
    Used for direct download if Herbie fails.
    """
    # Format: CMC_glb_PARAM_LEVEL_YYYYMMDDHH_Phhh.grib2
    base_url = "https://dd.weather.gc.ca/model_gem_global/15km/grib2/lat_lon"
    url = f"{base_url}/{run_hour:02d}/{fxx:03d}"
    return url


# Parameter search strings for GDPS GRIB2 files
# GDPS uses specific naming conventions:
# - TMP = Temperature
# - DPT = Dew point temperature
# - DEPR = Dew point depression
# - UGRD = U wind component
# - VGRD = V wind component
# - HGT = Geopotential height
# - RH = Relative humidity
# - ABSV = Absolute vorticity
# - PRMSL = Pressure reduced to MSL
# - PRES = Pressure
# - CAPE = Convective Available Potential Energy
# - CIN = Convective Inhibition
# - GUST = Wind gust
# - ACPCP = Accumulated precipitation
# - APCP = Accumulated precipitation (alternate)
# - WEASNOD = Snow depth
# - SNOD = Snow depth (alternate)
# - WTMP = Water temperature
# - ICEC = Ice concentration

PARAM_SEARCH_STRINGS = {
    # Surface/2m parameters
    "temp_2m": ":TMP:2 m above ground:",
    "dewpoint_2m": ":DPT:2 m above ground:",
    "dewpoint_depression_2m": ":DEPR:2 m above ground:",
    "rh_2m": ":RH:2 m above ground:",
    "mslp": ":PRMSL:mean sea level:",
    
    # Precipitation
    "precip_6hr": ":ACPCP:surface:",  # Accumulated precipitation
    "precip_total": ":APCP:surface:",  # Total precipitation
    
    # Wind
    "wind_u_10m": ":UGRD:10 m above ground:",
    "wind_v_10m": ":VGRD:10 m above ground:",
    "wind_gust_10m": ":GUST:10 m above ground:",
    
    # Convective parameters
    "cape_surface": ":CAPE:surface:",
    "cin_surface": ":CIN:surface:",
    "cape_ml": ":CAPE:180-0 mb above ground:",  # Mixed layer CAPE if available
    "cin_ml": ":CIN:180-0 mb above ground:",
    
    # Snow
    "snow_depth": ":SNOD:surface:",
    "snow_we": ":WEASNOD:surface:",  # Water equivalent snow depth
    "snow_density": ":SDEN:surface:",
    
    # Surface temperature
    "skin_temp": ":TMP:surface:",  # Skin/ground temperature
    "soil_temp_0_10cm": ":TMP:0-0.1 m below ground:",
    "soil_temp_10_40cm": ":TMP:0.1-0.4 m below ground:",
    
    # 925mb level
    "temp_925mb": ":TMP:925 mb:",
    "dewpoint_925mb": ":DPT:925 mb:",
    "rh_925mb": ":RH:925 mb:",
    "gh_925mb": ":HGT:925 mb:",
    "wind_u_925mb": ":UGRD:925 mb:",
    "wind_v_925mb": ":VGRD:925 mb:",
    
    # 850mb level
    "temp_850mb": ":TMP:850 mb:",
    "dewpoint_850mb": ":DPT:850 mb:",
    "rh_850mb": ":RH:850 mb:",
    "gh_850mb": ":HGT:850 mb:",
    "wind_u_850mb": ":UGRD:850 mb:",
    "wind_v_850mb": ":VGRD:850 mb:",
    "vort_850mb": ":ABSV:850 mb:",
    
    # 700mb level
    "temp_700mb": ":TMP:700 mb:",
    "omega_700mb": ":VVEL:700 mb:",
    "dewpoint_700mb": ":DPT:700 mb:",
    "rh_700mb": ":RH:700 mb:",
    "gh_700mb": ":HGT:700 mb:",
    "wind_u_700mb": ":UGRD:700 mb:",
    "wind_v_700mb": ":VGRD:700 mb:",
    
    # 500mb level
    "temp_500mb": ":TMP:500 mb:",
    "gh_500mb": ":HGT:500 mb:",
    "wind_u_500mb": ":UGRD:500 mb:",
    "wind_v_500mb": ":VGRD:500 mb:",
    "vort_500mb": ":ABSV:500 mb:",
    "rh_500mb": ":RH:500 mb:",
    
    # 300mb level (jet stream)
    "temp_300mb": ":TMP:300 mb:",
    "gh_300mb": ":HGT:300 mb:",
    "wind_u_300mb": ":UGRD:300 mb:",
    "wind_v_300mb": ":VGRD:300 mb:",
    "wind_speed_300mb": ":WIND:300 mb:",  # If available
    
    # 250mb level
    "temp_250mb": ":TMP:250 mb:",
    "gh_250mb": ":HGT:250 mb:",
    "wind_u_250mb": ":UGRD:250 mb:",
    "wind_v_250mb": ":VGRD:250 mb:",
    
    # 200mb level
    "temp_200mb": ":TMP:200 mb:",
    "gh_200mb": ":HGT:200 mb:",
    "wind_u_200mb": ":UGRD:200 mb:",
    "wind_v_200mb": ":VGRD:200 mb:",
}

# Mapping for Herbie to find GDPS files
# GDPS uses a specific file naming pattern
HERBIE_PRIORITY = ["aws", "nomads"]  # Try AWS first, then fall back

# Data sources for Herbie
SOURCES = ["aws", "nomads"]

# File index pattern - GDPS files are named like:
# CMC_glb_PARAM_LEVEL_YYYYMMDDHH_Phhh.grib2
# where:
# - PARAM is the parameter name (TMP, UGRD, etc.)
# - LEVEL is the level type (ISBL, SFC, etc.)
# - YYYYMMDDHH is the run datetime
# - hhh is the forecast hour (000-240)

# Sources priority for Herbie
SOURCES_PRIORITY = ["aws", "nomads"]

# GRIB shortName mappings for eccodes lookup
# Maps our internal parameter names to GRIB2 shortNames
GRIB_SHORTNAMES = {
    "temp_2m": "2t",
    "dewpoint_2m": "2d",
    "dewpoint_depression_2m": "2dp",
    "rh_2m": "2r",
    "mslp": "prmsl",
    "precip_6hr": "acpcp",
    "precip_total": "tp",
    "wind_u_10m": "10u",
    "wind_v_10m": "10v",
    "wind_gust_10m": "gust",
    "cape_surface": "cape",
    "cin_surface": "cin",
    "cape_ml": "cape",
    "cin_ml": "cin",
    "snow_depth": "snod",
    "snow_we": "sdwe",
    "snow_density": "sndc",
    "skin_temp": "skt",
    "soil_temp_0_10cm": "st",
    "soil_temp_10_40cm": "st",
    "temp_925mb": "t",
    "dewpoint_925mb": "dpt",
    "rh_925mb": "r",
    "gh_925mb": "gh",
    "wind_u_925mb": "u",
    "wind_v_925mb": "v",
    "temp_850mb": "t",
    "dewpoint_850mb": "dpt",
    "rh_850mb": "r",
    "gh_850mb": "gh",
    "wind_u_850mb": "u",
    "wind_v_850mb": "v",
    "vort_850mb": "absv",
    "temp_700mb": "t",
    "omega_700mb": "w",
    "dewpoint_700mb": "dpt",
    "rh_700mb": "r",
    "gh_700mb": "gh",
    "wind_u_700mb": "u",
    "wind_v_700mb": "v",
    "temp_500mb": "t",
    "gh_500mb": "gh",
    "wind_u_500mb": "u",
    "wind_v_500mb": "v",
    "vort_500mb": "absv",
    "rh_500mb": "r",
    "temp_300mb": "t",
    "gh_300mb": "gh",
    "wind_u_300mb": "u",
    "wind_v_300mb": "v",
    "temp_250mb": "t",
    "gh_250mb": "gh",
    "wind_u_250mb": "u",
    "wind_v_250mb": "v",
    "temp_200mb": "t",
    "gh_200mb": "gh",
    "wind_u_200mb": "u",
    "wind_v_200mb": "v",
}

# GRIB level type mappings
# Maps parameter names to level type filters for eccodes
LEVEL_TYPES = {
    "temp_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "dewpoint_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "dewpoint_depression_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "rh_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "mslp": {"typeOfLevel": "meanSea"},
    "precip_6hr": {"typeOfLevel": "surface"},
    "precip_total": {"typeOfLevel": "surface"},
    "wind_u_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "wind_v_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "wind_gust_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "cape_surface": {"typeOfLevel": "surface"},
    "cin_surface": {"typeOfLevel": "surface"},
    "cape_ml": {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
    "cin_ml": {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
    "snow_depth": {"typeOfLevel": "surface"},
    "snow_we": {"typeOfLevel": "surface"},
    "snow_density": {"typeOfLevel": "surface"},
    "skin_temp": {"typeOfLevel": "surface"},
    "soil_temp_0_10cm": {"typeOfLevel": "depthBelowLandLayer", "topLevel": 0, "bottomLevel": 10},
    "soil_temp_10_40cm": {"typeOfLevel": "depthBelowLandLayer", "topLevel": 10, "bottomLevel": 40},
    "temp_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "dewpoint_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "rh_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "gh_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "wind_u_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "wind_v_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "temp_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "dewpoint_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "rh_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "gh_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "wind_u_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "wind_v_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "vort_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "omega_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "dewpoint_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "rh_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "gh_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "wind_u_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "wind_v_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "temp_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "wind_u_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "wind_v_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "vort_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "rh_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "temp_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "gh_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "wind_u_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "wind_v_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "temp_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "gh_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "wind_u_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "wind_v_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "temp_200mb": {"typeOfLevel": "isobaricInhPa", "level": 200},
    "gh_200mb": {"typeOfLevel": "isobaricInhPa", "level": 200},
    "wind_u_200mb": {"typeOfLevel": "isobaricInhPa", "level": 200},
    "wind_v_200mb": {"typeOfLevel": "isobaricInhPa", "level": 200},
}

# Derived parameters calculated from base parameters
# Format: {output_param: (u_input_param, v_input_param)}
# These are used to compute wind speed from u/v components
DERIVED_PARAMS = {
    "wind_speed": ("wind_u_10m", "wind_v_10m"),
    "wind_gusts": ("wind_gust_10m", None),  # Already scalar, just mapping
    "wind_850mb": ("wind_u_850mb", "wind_v_850mb"),
    "wind_700mb": ("wind_u_700mb", "wind_v_700mb"),
    "wind_500mb": ("wind_u_500mb", "wind_v_500mb"),
    "wind_300mb": ("wind_u_300mb", "wind_v_300mb"),
}


def format_gdps_filename(param: str, level: str, run_date: datetime, run_hour: int, fxx: int) -> str:
    """
    Format a GDPS filename according to ECCC conventions.
    
    Example: CMC_glb_TMP_ISBL_850_latlon.15x.15_2024030700_P003.grib2
    """
    date_str = run_date.strftime("%Y%m%d") + f"{run_hour:02d}"
    return f"CMC_glb_{param}_{level}_latlon.15x.15_{date_str}_P{fxx:03d}.grib2"
