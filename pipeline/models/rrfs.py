"""RRFS (Rapid Refresh Forecast System) — Next-gen convection-allowing model.

RRFS is NOAA's next-generation rapid refresh system, replacing HRRR.
- 3km resolution over CONUS (same as HRRR)
- Hourly updates with improved data assimilation
- Ensemble capability (coming online)
- Native microphysics with better precip representation

Current status (2024-2025): Parallel runs, becoming operational
Resolution: 3km
Runs: Every hour (analysis + forecast)
Forecast: Currently 0-24 hours (expanding to 60+ hours)

Key improvements over HRRR:
- Better data assimilation (ensemble Kalman filter)
- Improved microphysics
- Better handling of terrain
- Future ensemble capability
"""

from datetime import datetime

MODEL_NAME = "rrfs"
DISPLAY_NAME = "RRFS (Next-Gen)"
HERBIE_MODEL = "rrfs"
HERBIE_PRODUCT = "conus"  # Or 'native' for full resolution

# RRFS runs every hour
RUN_TIMES = list(range(24))

# Resolution in km
RESOLUTION_KM = 3

# Data availability delay
AVAILABILITY_DELAY_MINUTES = 45

# Data retention
MAX_RETAINED_RUNS = 3

# Priority — similar to HRRR
SCHEDULER_PRIORITY = 5


def get_run_fxx(run_hour: int) -> range:
    """Return forecast hour range for RRFS.
    
    Currently 0-24 hours for most runs, some to 60 hours.
    Planning for longer forecasts as system matures.
    """
    if run_hour in RUN_TIMES:
        # Full hourly output to 24 hours
        # May expand to 60+ hours for certain runs
        return range(0, 25)
    return range(0)


# Parameter search strings for RRFS
# Very similar to HRRR since it's the successor
PARAM_SEARCH_STRINGS = {
    # Surface
    "temp_2m": ":TMP:2 m above ground:",
    "dewpoint_2m": ":DPT:2 m above ground:",
    "ugrd_10m": ":UGRD:10 m above ground:",
    "vgrd_10m": ":VGRD:10 m above ground:",
    "mslp": ":MSLET:mean sea level:",
    "psfc": ":PRES:surface:",
    "wind_gusts": ":GUST:surface:",
    
    # Precipitation
    "precip": ":APCP:surface:",
    "precip_rate": ":PRATE:surface:",
    
    # Radar reflectivity
    "reflectivity": ":REFC:entire atmosphere",
    "refl_1km": ":REFD:1000 m above ground:",
    
    # Convective parameters
    "cape": ":CAPE:surface:",
    "cin": ":CIN:surface:",
    "cape_ml": ":CAPE:90-0 mb above ground:",
    "cin_ml": ":CIN:90-0 mb above ground:",
    
    # Helicity / updrafts
    "srh_01km": ":HLCY:1000-0 m above ground:",
    "srh_03km": ":HLCY:3000-0 m above ground:",
    "uh_25km": ":UPHL:5000-2000 m above ground:",
    # No 0-2 km updraft helicity in the ops feed (MXUPHL 0-3 km / 2-5 km only)
    
    # Shear
    "ushear_01km": ":VUCSH:0-1000 m above ground:",
    "vshear_01km": ":VVCSH:0-1000 m above ground:",
    "ushear_06km": ":VUCSH:0-6000 m above ground:",
    "vshear_06km": ":VVCSH:0-6000 m above ground:",
    
    # Pressure levels
    "temp_925mb": ":TMP:925 mb:",
    "temp_850mb": ":TMP:850 mb:",
    "temp_700mb": ":TMP:700 mb:",
    "omega_700mb": ":VVEL:700 mb:",
    "temp_500mb": ":TMP:500 mb:",
    "temp_300mb": ":TMP:300 mb:",
    "temp_250mb": ":TMP:250 mb:",
    "gh_925mb": ":HGT:925 mb:",
    "gh_850mb": ":HGT:850 mb:",
    "gh_700mb": ":HGT:700 mb:",
    "gh_500mb": ":HGT:500 mb:",
    "gh_300mb": ":HGT:300 mb:",
    "gh_250mb": ":HGT:250 mb:",
    "ugrd_925mb": ":UGRD:925 mb:",
    "vgrd_925mb": ":VGRD:925 mb:",
    "ugrd_850mb": ":UGRD:850 mb:",
    "vgrd_850mb": ":VGRD:850 mb:",
    "ugrd_700mb": ":UGRD:700 mb:",
    "vgrd_700mb": ":VGRD:700 mb:",
    "ugrd_500mb": ":UGRD:500 mb:",
    "vgrd_500mb": ":VGRD:500 mb:",
    "ugrd_300mb": ":UGRD:300 mb:",
    "vgrd_300mb": ":VGRD:300 mb:",
    "ugrd_250mb": ":UGRD:250 mb:",
    "vgrd_250mb": ":VGRD:250 mb:",
    "rh_850mb": ":RH:850 mb:",
    "rh_700mb": ":RH:700 mb:",
    "rh_500mb": ":RH:500 mb:",
    
    # Vorticity
    "vort_500mb": ":ABSV:500 mb:",
    
    # Cloud / visibility
    "cloud_cover": r":TCDC:entire atmosphere \(considered as a single layer\):[0-9]+ hour fcst:",
    "visibility": ":VIS:surface:",
    "ceil": ":HGT:cloud ceiling:",
    
    # Moisture
    "pwat": ":PWAT:entire atmosphere",
    
    # Winter
    "snow_depth": ":SNOD:surface:",
    "weasd": ":WEASD:surface:(?:anl|[0-9]+ hour fcst):",  # Snowpack water equivalent
    "frozen_pct": ":CPOFP:surface:",
    "frzlvl": ":HGT:0C isotherm:",
    
    # Storm chasing
    "vil": ":VIL:entire atmosphere",
    "echo_tops": ":RETOP:entire atmosphere",
    "pbl_height": ":HPBL:surface:",
    "lcl": ":HGT:level of adiabatic condensation from sfc:",
    "lfc": ":HGT:level of free convection:",
    
    # Lightning
    "lightning": ":LTNG:entire atmosphere:",
    
    # Max/min
    "max_wind": ":WIND:10 m above ground:[0-9]+-[0-9]+ hour max fcst:",
    "max_temp_1h": ":TMAX:2 m above ground:",
    "min_temp_1h": ":TMIN:2 m above ground:",
    
    # Soil
    "soil_temp_0_10cm": ":TMP:0-0.1 m below ground:",
    "soil_moisture_0_10cm": ":SOILW:0-0.1 m below ground:",
}

# GRIB short names
GRIB_SHORTNAMES = {
    "temp_2m": "2t",
    "dewpoint_2m": "2d",
    "ugrd_10m": "10u",
    "vgrd_10m": "10v",
    "mslp": "mslet",
    "psfc": "sp",
    "wind_gusts": "gust",
    "precip": "tp",
    "precip_rate": "prate",
    "reflectivity": "refc",
    "refl_1km": "rare",
    "cape": "cape",
    "cin": "cin",
    "cape_ml": "cape",
    "cin_ml": "cin",
    "srh_01km": "hlcy",
    "srh_03km": "hlcy",
    "uh_25km": "unknown",
    "uh_02km": "mxuphl",
    "ushear_01km": "vucsh",
    "vshear_01km": "vvcsh",
    "ushear_06km": "vucsh",
    "vshear_06km": "vvcsh",
    "temp_925mb": "t",
    "temp_850mb": "t",
    "temp_700mb": "t",
    "omega_700mb": "w",
    "temp_500mb": "t",
    "temp_300mb": "t",
    "temp_250mb": "t",
    "gh_925mb": "gh",
    "gh_850mb": "gh",
    "gh_700mb": "gh",
    "gh_500mb": "gh",
    "gh_300mb": "gh",
    "gh_250mb": "gh",
    "ugrd_925mb": "u",
    "vgrd_925mb": "v",
    "ugrd_850mb": "u",
    "vgrd_850mb": "v",
    "ugrd_700mb": "u",
    "vgrd_700mb": "v",
    "ugrd_500mb": "u",
    "vgrd_500mb": "v",
    "ugrd_300mb": "u",
    "vgrd_300mb": "v",
    "ugrd_250mb": "u",
    "vgrd_250mb": "v",
    "rh_850mb": "r",
    "rh_700mb": "r",
    "rh_500mb": "r",
    "vort_500mb": "absv",
    "cloud_cover": "tcc",
    "visibility": "vis",
    "ceil": "gh",
    "pwat": "pwat",
    "snow_depth": "sde",
    "weasd": "sdwe",
    "frozen_pct": "cpofp",
    "frzlvl": "gh",
    "vil": "veril",
    "echo_tops": "retop",
    "pbl_height": "blh",
    "lcl": "gh",
    "lfc": "gh",
    "lightning": "ltng",
    "max_wind": "max_10si",
    "max_temp_1h": "tmax",
    "min_temp_1h": "tmin",
    "soil_temp_0_10cm": "st",
    "soil_moisture_0_10cm": "sw",
}

# Level types
LEVEL_TYPES = {
    "temp_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "dewpoint_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "ugrd_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "vgrd_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "mslp": {"typeOfLevel": "meanSea"},
    "psfc": {"typeOfLevel": "surface"},
    "wind_gusts": {"typeOfLevel": "surface"},
    "precip": {"typeOfLevel": "surface"},
    "precip_rate": {"typeOfLevel": "surface"},
    "reflectivity": {"typeOfLevel": "atmosphere"},
    "refl_1km": {"typeOfLevel": "heightAboveGround", "level": 1000},
    "cape": {"typeOfLevel": "surface", "level": 0},
    "cin": {"typeOfLevel": "surface", "level": 0},
    "cape_ml": {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 9000},
    "cin_ml": {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 9000},
    "srh_01km": {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 1000},
    "srh_03km": {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 3000},
    "uh_25km": {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 5000, "bottomLevel": 2000},
    "uh_02km": {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 2000, "bottomLevel": 0},
    "ushear_01km": {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 1000},
    "vshear_01km": {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 1000},
    "ushear_06km": {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 6000},
    "vshear_06km": {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 6000},
    "temp_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "temp_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "omega_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "temp_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "temp_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "temp_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "gh_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "gh_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "gh_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "gh_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "gh_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "ugrd_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "vgrd_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "ugrd_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "vgrd_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "ugrd_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "vgrd_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "ugrd_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "vgrd_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "ugrd_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "vgrd_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "ugrd_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "vgrd_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "rh_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "rh_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "rh_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "vort_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "cloud_cover": {"typeOfLevel": "atmosphere"},
    "visibility": {"typeOfLevel": "surface"},
    "ceil": {"typeOfLevel": "cloudCeiling"},
    "pwat": {"typeOfLevel": "atmosphereSingleLayer"},
    "snow_depth": {"typeOfLevel": "surface"},
    "weasd": {"typeOfLevel": "surface", "stepType": "instant"},
    "frozen_pct": {"typeOfLevel": "surface"},
    "frzlvl": {"typeOfLevel": "isothermZero"},
    "vil": {"typeOfLevel": "atmosphere"},
    "echo_tops": {"typeOfLevel": "cloudTop"},
    "pbl_height": {"typeOfLevel": "surface"},
    "lcl": {"typeOfLevel": "adiabaticCondensation"},
    "lfc": {"typeOfLevel": "unknown"},
    "lightning": {"typeOfLevel": "surface"},
    "max_wind": {"typeOfLevel": "heightAboveGround", "level": 10},
    "max_temp_1h": {"typeOfLevel": "heightAboveGround", "level": 2},
    "min_temp_1h": {"typeOfLevel": "heightAboveGround", "level": 2},
    "soil_temp_0_10cm": {"typeOfLevel": "depthBelowLandLayer", "topLevel": 0, "bottomLevel": 0.1},
    "soil_moisture_0_10cm": {"typeOfLevel": "depthBelowLandLayer", "topLevel": 0, "bottomLevel": 0.1},
}

# Data sources
SOURCES_PRIORITY = ["nomads", "aws"]

# Derived parameters
DERIVED_PARAMS = {
    "wind_speed": ("ugrd_10m", "vgrd_10m"),
    "wind_925mb": ("ugrd_925mb", "vgrd_925mb"),
    "wind_850mb": ("ugrd_850mb", "vgrd_850mb"),
    "wind_700mb": ("ugrd_700mb", "vgrd_700mb"),
    "wind_500mb": ("ugrd_500mb", "vgrd_500mb"),
    "wind_300mb": ("ugrd_300mb", "vgrd_300mb"),
    "wind_250mb": ("ugrd_250mb", "vgrd_250mb"),
    "shear_01km": ("ushear_01km", "vshear_01km"),
    "shear_06km": ("ushear_06km", "vshear_06km"),
}

COMPOSITE_PARAMS = {}
