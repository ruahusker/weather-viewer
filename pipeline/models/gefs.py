"""GEFS (Global Ensemble Forecast System) — Long-range ensemble guidance.

GEFS provides ensemble forecasts out to 35 days (operational) or 16 days (reforecast).
- 30-31 ensemble members + control
- 0.25° (~25km) resolution
- Runs: 00Z, 06Z, 12Z, 18Z
- Forecast: product-dependent. The 0.25 degree "select parameters" product
  used here (``pgrb2s.0p25`` / Herbie ``atmos.25``) is only available from
  FH000 and FH003-240.

Key products:
- Ensemble mean/spread for all variables
- Probability of exceedance (e.g., > 1" precip in 24h)
- Spaghetti plots (500mb height contours)
- Credible intervals

Use cases:
- Extended range forecasting (beyond day 7-10)
- Uncertainty quantification
- Probability of significant events
- Pattern recognition (NAO, PNA indices)
"""

from datetime import datetime

MODEL_NAME = "gefs"
DISPLAY_NAME = "GEFS Ensemble"
HERBIE_MODEL = "gefs"
HERBIE_PRODUCT = "atmos"  # Atmospheric products

# GEFS runs 4x daily
RUN_TIMES = [0, 6, 12, 18]

# Resolution in degrees (approx 25km at mid-latitudes)
RESOLUTION_DEG = 0.25

# Data availability delay
AVAILABILITY_DELAY_MINUTES = 90  # Ensembles take longer

# Data retention
MAX_RETAINED_RUNS = 2

# Priority
SCHEDULER_PRIORITY = 6  # Lower priority than deterministic models


def get_run_fxx(run_hour: int) -> range:
    """Return forecast hour range for GEFS.

    This project uses the GEFS "select parameters" 0.25 degree product
    (``gec00.tCCz.pgrb2s.0p25.fxxx``), not the longer 0.5 degree ``pgrb2a``
    product. Per the official NCEP inventory, this product is only published
    for FH000 and FH003-240 for all cycles.
    """
    if run_hour in RUN_TIMES:
        return range(0, 241, 3)
    return range(0)


# Parameter search strings for GEFS
# Note: GEFS provides both control and mean/spread products
PARAM_SEARCH_STRINGS = {
    # Surface — ensemble mean
    "temp_2m": ":TMP:2 m above ground:",
    "dewpoint_2m": ":DPT:2 m above ground:",
    "ugrd_10m": ":UGRD:10 m above ground:",
    "vgrd_10m": ":VGRD:10 m above ground:",
    "mslp": ":MSLMA:mean sea level:",
    "psfc": ":PRES:surface:",
    "wind_gusts": ":GUST:surface:",
    
    # Precipitation (6-hour accumulations)
    "precip_6h": ":APCP:surface:6 hour acc:",
    "precip_probability_6h_05in": ":APCP:surface:6 hour acc:probability of >12.7:",
    "precip_probability_6h_1in": ":APCP:surface:6 hour acc:probability of >25.4:",
    
    # Snowfall
    "snowfall_6h": ":WEASD:surface:6 hour acc:",
    "snowfall_probability_6h_1in": ":WEASD:surface:6 hour acc:probability of >25.4:",
    
    # Pressure levels — temperatures
    "temp_925mb": ":TMP:925 mb:",
    "temp_850mb": ":TMP:850 mb:",
    "temp_700mb": ":TMP:700 mb:",
    "omega_700mb": ":VVEL:700 mb:",
    "temp_500mb": ":TMP:500 mb:",
    "temp_300mb": ":TMP:300 mb:",
    "temp_250mb": ":TMP:250 mb:",
    "temp_200mb": ":TMP:200 mb:",
    
    # Heights — critical for pattern recognition
    "gh_1000mb": ":HGT:1000 mb:",
    "gh_925mb": ":HGT:925 mb:",
    "gh_850mb": ":HGT:850 mb:",
    "gh_700mb": ":HGT:700 mb:",
    "gh_500mb": ":HGT:500 mb:",
    "gh_300mb": ":HGT:300 mb:",
    "gh_250mb": ":HGT:250 mb:",
    "gh_200mb": ":HGT:200 mb:",
    
    # Winds at pressure levels
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
    "ugrd_200mb": ":UGRD:200 mb:",
    "vgrd_200mb": ":VGRD:200 mb:",
    
    # Humidity
    "rh_850mb": ":RH:850 mb:",
    "rh_700mb": ":RH:700 mb:",
    "rh_500mb": ":RH:500 mb:",
    
    # PWAT
    "pwat": ":PWAT:entire atmosphere:",
    "pwat_probability_1in": ":PWAT:entire atmosphere:probability of >25.4:",
    
    # Cloud cover
    "cloud_cover": ":TCDC:entire atmosphere:",
    "cloud_cover_low": ":LCDC:low cloud layer:",
    "cloud_cover_mid": ":MCDC:middle cloud layer:",
    "cloud_cover_high": ":HCDC:high cloud layer:",
    
    # 500mb height spread (for uncertainty)
    "gh_500mb_spread": ":HGT:500 mb:ensemble spread:",
    "gh_500mb_anomaly": ":HGT:500 mb:ensemble mean anomaly:",
    
    # CAPE (often limited in GEFS)
    "cape": ":CAPE:surface:",
    "cape_probability_1000": ":CAPE:surface:probability of >1000:",
    
    # Lifted index
    "lifted_index": ":LFTX:surface:",
}

# GRIB short names
GRIB_SHORTNAMES = {
    "temp_2m": "2t",
    "dewpoint_2m": "2d",
    "ugrd_10m": "10u",
    "vgrd_10m": "10v",
    "mslp": "mslma",
    "psfc": "sp",
    "wind_gusts": "gust",
    "precip_6h": "tp",
    "precip_probability_6h_05in": "tp",
    "precip_probability_6h_1in": "tp",
    "snowfall_6h": "sf",
    "snowfall_probability_6h_1in": "sf",
    "temp_925mb": "t",
    "temp_850mb": "t",
    "temp_700mb": "t",
    "omega_700mb": "w",
    "temp_500mb": "t",
    "temp_300mb": "t",
    "temp_250mb": "t",
    "temp_200mb": "t",
    "gh_1000mb": "gh",
    "gh_925mb": "gh",
    "gh_850mb": "gh",
    "gh_700mb": "gh",
    "gh_500mb": "gh",
    "gh_300mb": "gh",
    "gh_250mb": "gh",
    "gh_200mb": "gh",
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
    "ugrd_200mb": "u",
    "vgrd_200mb": "v",
    "rh_850mb": "r",
    "rh_700mb": "r",
    "rh_500mb": "r",
    "pwat": "pwat",
    "pwat_probability_1in": "pwat",
    "cloud_cover": "tcc",
    "cloud_cover_low": "lcc",
    "cloud_cover_mid": "mcc",
    "cloud_cover_high": "hcc",
    "gh_500mb_spread": "gh",
    "gh_500mb_anomaly": "gh",
    "cape": "cape",
    "cape_probability_1000": "cape",
    "lifted_index": "lftx",
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
    "precip_6h": {"typeOfLevel": "surface"},
    "precip_probability_6h_05in": {"typeOfLevel": "surface"},
    "precip_probability_6h_1in": {"typeOfLevel": "surface"},
    "snowfall_6h": {"typeOfLevel": "surface"},
    "snowfall_probability_6h_1in": {"typeOfLevel": "surface"},
    "temp_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "temp_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "omega_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "temp_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "temp_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "temp_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "temp_200mb": {"typeOfLevel": "isobaricInhPa", "level": 200},
    "gh_1000mb": {"typeOfLevel": "isobaricInhPa", "level": 1000},
    "gh_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "gh_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "gh_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "gh_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_300mb": {"typeOfLevel": "isobaricInhPa", "level": 300},
    "gh_250mb": {"typeOfLevel": "isobaricInhPa", "level": 250},
    "gh_200mb": {"typeOfLevel": "isobaricInhPa", "level": 200},
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
    "ugrd_200mb": {"typeOfLevel": "isobaricInhPa", "level": 200},
    "vgrd_200mb": {"typeOfLevel": "isobaricInhPa", "level": 200},
    "rh_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "rh_700mb": {"typeOfLevel": "isobaricInhPa", "level": 700},
    "rh_500mb": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "pwat": {"typeOfLevel": "atmosphereSingleLayer"},
    "pwat_probability_1in": {"typeOfLevel": "atmosphereSingleLayer"},
    "cloud_cover": {"typeOfLevel": "atmosphere"},
    "cloud_cover_low": {"typeOfLevel": "lowCloudLayer"},
    "cloud_cover_mid": {"typeOfLevel": "middleCloudLayer"},
    "cloud_cover_high": {"typeOfLevel": "highCloudLayer"},
    "gh_500mb_spread": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_500mb_anomaly": {"typeOfLevel": "isobaricInhPa", "level": 500},
    "cape": {"typeOfLevel": "surface", "level": 0},
    "cape_probability_1000": {"typeOfLevel": "surface", "level": 0},
    "lifted_index": {"typeOfLevel": "surface"},
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
    "wind_200mb": ("ugrd_200mb", "vgrd_200mb"),
}

COMPOSITE_PARAMS = {}

# GEFS-specific probability parameters
PROBABILITY_PARAMS = {
    "precip_probability_6h_05in": {"threshold": 12.7, "unit": "mm/6h", "levels": [10, 30, 50, 70, 90]},
    "precip_probability_6h_1in": {"threshold": 25.4, "unit": "mm/6h", "levels": [10, 30, 50, 70, 90]},
    "snowfall_probability_6h_1in": {"threshold": 25.4, "unit": "mm/6h", "levels": [10, 30, 50, 70]},
    "pwat_probability_1in": {"threshold": 25.4, "unit": "mm", "levels": [10, 30, 50, 70]},
    "cape_probability_1000": {"threshold": 1000, "unit": "J/kg", "levels": [10, 30, 50, 70]},
}
