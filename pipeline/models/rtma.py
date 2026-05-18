"""
RTMA (Real-Time Mesoscale Analysis) - NCEP High-Resolution Analysis
Resolution: 2.5 km
Coverage: CONUS
Update: Hourly (every hour)
Forecast: Analysis only (fxx=0)

Data sources:
- AWS: https://noaa-rtma-pds.s3.amazonaws.com/
- NOMADS: https://nomads.ncep.noaa.gov/pub/data/nccf/com/rtma/prod/
"""

from datetime import datetime, timezone

MODEL_NAME = "rtma"
DISPLAY_NAME = "RTMA"
HERBIE_MODEL = "rtma"
HERBIE_PRODUCT = "anl"

# Run times - RTMA is hourly
RUN_TIMES = list(range(24))

# Resolution in km
RESOLUTION_KM = 2.5

# Data availability delay (minutes) - RTMA typically available ~10-15 min after the hour
AVAILABILITY_DELAY_MINUTES = 15

# Data retention - keep 6 runs (6 hours of analyses)
MAX_RETAINED_RUNS = 6

# Whether this model has snow data
HAS_SNOW = False

# Priority in scheduler (lower = higher priority)
# RTMA is analysis, so high priority for current conditions
SCHEDULER_PRIORITY = 2


def get_run_fxx(run_hour: int) -> range:
    """Return forecast hour range for a given run hour.
    
    RTMA is analysis only - no forecast hours.
    """
    if run_hour in RUN_TIMES:
        return range(0, 1)  # Only fxx=0 (analysis)
    return range(0)


# Sources priority for Herbie
SOURCES_PRIORITY = ["aws", "nomads"]

# Parameter search strings for RTMA GRIB2 files
# RTMA uses standard NCEP naming conventions
PARAM_SEARCH_STRINGS = {
    # Surface / 2m parameters
    "temp_2m": ":TMP:2 m above ground:",
    "dewpoint_2m": ":DPT:2 m above ground:",
    "rh_2m": ":RH:2 m above ground:",
    "q_2m": ":SPFH:2 m above ground:",  # Specific humidity
    "mslp": ":MSLMA:surface:",  # MSL pressure (RTMA uses MSLMA)
    "psfc": ":PRES:surface:",  # Surface pressure (use this instead of mslp)
    
    # Wind
    "wind_u_10m": ":UGRD:10 m above ground:",
    "wind_v_10m": ":VGRD:10 m above ground:",
    "wind_speed_10m": ":WIND:10 m above ground:",  # Pre-computed wind speed
    "wind_speed": ":WIND:10 m above ground:",  # Alias for standard name
    "wind_dir_10m": ":WDIR:10 m above ground:",  # Wind direction
    "wind_gust_10m": ":GUST:10 m above ground:",
    "wind_gusts": ":GUST:10 m above ground:",  # Alias for standard name
    
    # Visibility/ceiling
    "visibility": ":VIS:surface:",
    "ceiling": ":CEIL:cloud ceiling:",
    "cloud_cover": ":TCDC:entire atmosphere",  # Total cloud cover
    
}

# GRIB shortName mappings for eccodes lookup
GRIB_SHORTNAMES = {
    "temp_2m": "2t",
    "dewpoint_2m": "2d",
    "rh_2m": "2r",
    "q_2m": "2sh",
    "mslp": "mslma",  # RTMA uses MSLMA (but file may not have it)
    "psfc": "sp",  # Surface pressure (this one exists)
    "wind_u_10m": "10u",
    "wind_v_10m": "10v",
    "wind_speed_10m": "10si",
    "wind_speed": "10si",  # Alias - RTMA has pre-computed wind speed
    "wind_dir_10m": "10wdir",
    "wind_gust_10m": "i10fg",
    "wind_gusts": "i10fg",  # Alias - instantaneous 10m gust
    "visibility": "vis",
    "ceiling": "ceil",
    "cloud_cover": "tcc",
}

# GRIB level type mappings
LEVEL_TYPES = {
    "temp_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "dewpoint_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "rh_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "q_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "mslp": {"typeOfLevel": "meanSea"},
    "psfc": {"typeOfLevel": "surface"},
    "wind_u_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "wind_v_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "wind_speed_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "wind_speed": {"typeOfLevel": "heightAboveGround", "level": 10},  # Alias
    "wind_dir_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "wind_gust_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "wind_gusts": {"typeOfLevel": "heightAboveGround", "level": 10},  # Alias
    "visibility": {"typeOfLevel": "surface"},
    "ceiling": {"typeOfLevel": "cloudCeiling"},
    "cloud_cover": {"typeOfLevel": "atmosphere"},
}

# Derived parameters
# Format: {output_param: (u_input_param, v_input_param)}
DERIVED_PARAMS = {
    "wind_speed": ("wind_u_10m", "wind_v_10m"),
}

# Rendered parameters - what we actually display
RENDERED_PARAMS = [
    "temp_2m",
    "dewpoint_2m",
    "rh_2m",
    "q_2m",
    "wind_speed",
    "wind_gusts",
    "wind_u_10m",
    "wind_v_10m",
    "wind_dir_10m",
    "mslp",
    "psfc",
    "visibility",
    "cloud_cover",
]

# Map internal param names to display names
PARAM_DISPLAY_NAMES = {
    "temp_2m": "2m Temperature",
    "dewpoint_2m": "2m Dewpoint",
    "rh_2m": "2m Relative Humidity",
    "q_2m": "2m Specific Humidity",
    "wind_speed": "10m Wind Speed",
    "wind_gusts": "10m Wind Gusts",
    "wind_u_10m": "10m U Component",
    "wind_v_10m": "10m V Component",
    "wind_dir_10m": "10m Wind Direction",
    "mslp": "MSL Pressure",
    "psfc": "Surface Pressure",
    "visibility": "Visibility",
    "cloud_cover": "Total Cloud Cover",
}
