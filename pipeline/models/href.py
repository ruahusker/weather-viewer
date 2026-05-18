"""HREF (High-Resolution Ensemble Forecast) model.

NOAA's high-resolution ensemble providing probability forecasts.
Resolution: 3km over CONUS
Members: 8-10 ensemble members + mean/control
Runs: 00Z, 06Z, 12Z, 18Z
Forecast: 0-48 hours (some members to 60 hours)

Key products:
- Probability of reflectivity > 40 dBZ
- Probability of snowfall > 1"
- Ensemble mean/spread
"""

from datetime import datetime

MODEL_NAME = "href"
DISPLAY_NAME = "HREF Ensemble"
HERBIE_MODEL = "href"
HERBIE_PRODUCT = "conus"

# Run times
RUN_TIMES = [0, 6, 12, 18]

# Resolution in km
RESOLUTION_KM = 3

# Data availability delay (minutes)
AVAILABILITY_DELAY_MINUTES = 60

# Data retention
MAX_RETAINED_RUNS = 2

# Priority in scheduler
SCHEDULER_PRIORITY = 4  # Higher priority than HRRR for severe wx guidance


def get_run_fxx(run_hour: int) -> range:
    """Return forecast hour range for a given run hour."""
    if run_hour in RUN_TIMES:
        # HREF conus mean/prob guidance starts at f001 and extends to f048.
        return range(1, 49)
    return range(0)


# Parameter search strings for HREF mean product.
MEAN_PARAM_SEARCH_STRINGS = {
    # Surface parameters (from mean/probability products)
    "temp_2m": ":TMP:2 m above ground:",
    "dewpoint_2m": ":DPT:2 m above ground:",
    "ugrd_10m": ":UGRD:10 m above ground:",
    "vgrd_10m": ":VGRD:10 m above ground:",
    "mslp": ":MSLET:mean sea level:",
    "wind_gusts": ":GUST:10 m above ground:",
    
    # Precipitation (6-hourly accumulated)
    "precip": ":APCP:surface:",
    
    # Reflectivity (composite)
    "reflectivity": ":REFC:entire atmosphere:",
    
    # Convective parameters
    "cape": ":CAPE:surface:",
    "cin": ":CIN:surface:",
    
    # Helicity
    "srh_01km": ":HLCY:1000-0 m above ground:",
    "srh_03km": ":HLCY:3000-0 m above ground:",
    
    # Pressure levels
    "temp_850mb": ":TMP:850 mb:",
    "temp_700mb": ":TMP:700 mb:",
    "omega_700mb": ":VVEL:700 mb:",
    "temp_500mb": ":TMP:500 mb:",
    "temp_925mb": ":TMP:925 mb:",
    "gh_850mb": ":HGT:850 mb:",
    "gh_700mb": ":HGT:700 mb:",
    "gh_500mb": ":HGT:500 mb:",
    "gh_925mb": ":HGT:925 mb:",
    "ugrd_850mb": ":UGRD:850 mb:",
    "vgrd_850mb": ":VGRD:850 mb:",
    "ugrd_700mb": ":UGRD:700 mb:",
    "vgrd_700mb": ":VGRD:700 mb:",
    "ugrd_500mb": ":UGRD:500 mb:",
    "vgrd_500mb": ":VGRD:500 mb:",
    "ugrd_925mb": ":UGRD:925 mb:",
    "vgrd_925mb": ":VGRD:925 mb:",
    "rh_850mb": ":RH:850 mb:",
    "rh_700mb": ":RH:700 mb:",
    
    # Winter
    "snow_depth": ":SNOD:surface:",
    "snowfall_probability_1in": ":WEASD:surface:probability of >25.4:",
    "frozen_pct": ":CPOFP:surface:",
    
    # Visibility
    "visibility": ":VIS:surface:",
    
    # Cloud
    "cloud_cover": ":TCDC:entire atmosphere:",
    
    # PWAT
    "pwat": ":PWAT:entire atmosphere:",
}

# HREF probability product uses different fields and thresholds than the mean product.
PROB_PARAM_SEARCH_STRINGS = {
    "precip_probability": r":APCP:surface:.*prob >12\.7:",
    "refl_probability_40dbz": r":REFD:1000 m above ground:.*prob >40:",
    "cape_probability": r":CAPE:90-0 mb above ground:.*prob >1000:",
    # HREF prob product provides 2-5 km UH at 25/75/150, not 100.
    "uh_25km_probability": r":MXUPHL:5000-2000 m above ground:.*prob >75:",
}

PARAM_SEARCH_STRINGS = {
    **MEAN_PARAM_SEARCH_STRINGS,
    **PROB_PARAM_SEARCH_STRINGS,
}

# GRIB short names for eccodes
GRIB_SHORTNAMES = {
    "temp_2m": "2t",
    "dewpoint_2m": "2d",
    "ugrd_10m": "10u",
    "vgrd_10m": "10v",
    "mslp": "mslet",
    "wind_gusts": "gust",
    "precip": "tp",
    "precip_probability": "tp",
    "reflectivity": "refc",
    "refl_probability_40dbz": "refd",
    "cape": "cape",
    "cin": "cin",
    "cape_probability": "cape",
    "srh_01km": "hlcy",
    "srh_03km": "hlcy",
    "uh_25km_probability": "unknown",
    "temp_850mb": "t",
    "temp_700mb": "t",
    "omega_700mb": "w",
    "temp_500mb": "t",
    "temp_925mb": "t",
    "gh_850mb": "gh",
    "gh_700mb": "gh",
    "gh_500mb": "gh",
    "gh_925mb": "gh",
    "ugrd_850mb": "u",
    "vgrd_850mb": "v",
    "ugrd_700mb": "u",
    "vgrd_700mb": "v",
    "ugrd_500mb": "u",
    "vgrd_500mb": "v",
    "ugrd_925mb": "u",
    "vgrd_925mb": "v",
    "rh_850mb": "r",
    "rh_700mb": "r",
    "snow_depth": "sde",
    "frozen_pct": "cpofp",
    "visibility": "vis",
    "cloud_cover": "tcc",
    "pwat": "pwat",
}

# Level types for filtering
LEVEL_TYPES = {
    "temp_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "dewpoint_2m": {"typeOfLevel": "heightAboveGround", "level": 2},
    "ugrd_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "vgrd_10m": {"typeOfLevel": "heightAboveGround", "level": 10},
    "mslp": {"typeOfLevel": "meanSea"},
    "wind_gusts": {"typeOfLevel": "heightAboveGround", "level": 10},
    "precip": {"typeOfLevel": "surface"},
    "precip_probability": {
        "typeOfLevel": "surface",
        "probabilityType": 1,
        "productDefinitionTemplateNumber": 9,
        "scaledValueOfUpperLimit": 12700,
        "scaleFactorOfUpperLimit": 3,
    },
    "reflectivity": {"typeOfLevel": "atmosphere"},
    "refl_probability_40dbz": {
        "typeOfLevel": "heightAboveGround",
        "level": 1000,
        "probabilityType": 1,
        "productDefinitionTemplateNumber": 5,
        "scaledValueOfUpperLimit": 40000,
        "scaleFactorOfUpperLimit": 3,
    },
    "cape": {"typeOfLevel": "surface", "level": 0},
    "cin": {"typeOfLevel": "surface", "level": 0},
    "cape_probability": {
        "typeOfLevel": "pressureFromGroundLayer",
        "topLevel": 9000,
        "bottomLevel": 0,
        "probabilityType": 1,
        "productDefinitionTemplateNumber": 5,
        "scaledValueOfUpperLimit": 1000000,
        "scaleFactorOfUpperLimit": 3,
    },
    "srh_01km": {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 1000},
    "srh_03km": {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 3000},
    "uh_25km_probability": {
        "typeOfLevel": "heightAboveGroundLayer",
        "topLevel": 5000,
        "bottomLevel": 2000,
        "probabilityType": 1,
        "productDefinitionTemplateNumber": 5,
        "scaledValueOfUpperLimit": 75000,
        "scaleFactorOfUpperLimit": 3,
    },
    "temp_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "omega_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "temp_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "temp_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "gh_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "gh_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "gh_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "ugrd_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "vgrd_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "ugrd_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "vgrd_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "ugrd_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "vgrd_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "ugrd_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "vgrd_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "rh_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "rh_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "snow_depth": {"typeOfLevel": "surface"},
    "frozen_pct": {"typeOfLevel": "surface"},
    "visibility": {"typeOfLevel": "surface"},
    "cloud_cover": {"typeOfLevel": "atmosphere"},
    "pwat": {"typeOfLevel": "atmosphereSingleLayer"},
}

# Data sources (NOMADS typically has HREF)
SOURCES_PRIORITY = ["nomads", "aws"]

# Derived parameters (wind speed from u/v components)
DERIVED_PARAMS = {
    "wind_speed": ("ugrd_10m", "vgrd_10m"),
    "wind_850mb": ("ugrd_850mb", "vgrd_850mb"),
    "wind_700mb": ("ugrd_700mb", "vgrd_700mb"),
    "wind_500mb": ("ugrd_500mb", "vgrd_500mb"),
    "wind_925mb": ("ugrd_925mb", "vgrd_925mb"),
}

# No composites for HREF (probability products are handled as regular params)
COMPOSITE_PARAMS = {}

# HREF probability thresholds for special rendering
PROBABILITY_PARAMS = {
    "refl_probability_40dbz": {"threshold": 40, "unit": "dBZ", "levels": [10, 30, 50, 70]},
    "cape_probability": {"threshold": 1000, "unit": "J/kg", "levels": [10, 30, 50, 70]},
    "uh_25km_probability": {"threshold": 75, "unit": "m²/s²", "levels": [10, 30, 50, 70]},
}
