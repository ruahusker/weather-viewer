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
# The pgrb2s.0p25 control-member file (gec00) carries surface fields only.
# Pressure levels come from the 0.5 degree pgrb2ap5 product (UPPER_AIR_*
# below) and are appended to the same GRIB. Neither product has cloud layers,
# lifted index, 6 h snowfall, 500 mb anomaly or exceedance probabilities.
_SIX_HOUR_WINDOWS = "|".join(f"{hour - 6}-{hour}" for hour in range(6, 241, 6))

# Fields in the 0.25 degree pgrb2s file (the scheduler's primary download)
SURFACE_SEARCH_STRINGS = {
    # Surface (control member)
    "temp_2m": ":TMP:2 m above ground:",
    "dewpoint_2m": ":DPT:2 m above ground:",
    "ugrd_10m": ":UGRD:10 m above ground:",
    "vgrd_10m": ":VGRD:10 m above ground:",
    "mslp": ":PRMSL:mean sea level:",
    "psfc": ":PRES:surface:",
    "wind_gusts": ":GUST:surface:",

    # APCP buckets reset every 6 h; hours 3, 9, 15... hold 3 h buckets and are skipped
    "precip_6h": f":APCP:surface:(?:{_SIX_HOUR_WINDOWS}) hour acc fcst:",

    "pwat": ":PWAT:entire atmosphere",
    "cloud_cover": ":TCDC:entire atmosphere:",
    "cape": ":CAPE:surface:",
}

# 0.5 degree pgrb2ap5 product (Herbie "atmos.5"), appended to each forecast
# hour's surface subset. Member "c00" keeps the pressure levels on the same
# control run as the surface fields; "avg" would use the ensemble mean.
UPPER_AIR_PRODUCT = "atmos.5"
UPPER_AIR_MEMBER = "c00"

# pgrb2ap5 has TMP/RH on 1000-200 mb except 300 mb, HGT/UGRD/VGRD on every
# level, and VVEL only at 850 mb.
_UPPER_AIR_LEVELS = (925, 850, 700, 500, 300, 250, 200)
UPPER_AIR_SEARCH_STRINGS = {
    **{f"temp_{lev}mb": f":TMP:{lev} mb:" for lev in _UPPER_AIR_LEVELS if lev != 300},
    **{f"gh_{lev}mb": f":HGT:{lev} mb:" for lev in _UPPER_AIR_LEVELS},
    **{f"ugrd_{lev}mb": f":UGRD:{lev} mb:" for lev in _UPPER_AIR_LEVELS},
    **{f"vgrd_{lev}mb": f":VGRD:{lev} mb:" for lev in _UPPER_AIR_LEVELS},
    **{f"rh_{lev}mb": f":RH:{lev} mb:" for lev in (850, 700, 500)},
}

# Ensemble spread (gespr), appended after the member fields so gh_500mb
# still matches the member's height first.
SPREAD_MEMBER = "spr"
SPREAD_SEARCH_STRINGS = {
    "gh_500mb_spread": ":HGT:500 mb:",
}

# Every field the GEFS download provides, across all three files
PARAM_SEARCH_STRINGS = {
    **SURFACE_SEARCH_STRINGS,
    **UPPER_AIR_SEARCH_STRINGS,
    **SPREAD_SEARCH_STRINGS,
}

# GRIB short names
GRIB_SHORTNAMES = {
    "temp_2m": "2t",
    "dewpoint_2m": "2d",
    "ugrd_10m": "10u",
    "vgrd_10m": "10v",
    "mslp": "prmsl",
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
    # derivedForecast 2 = ensemble standard deviation (gespr)
    "gh_500mb_spread": {"typeOfLevel": "isobaricInhPa", "level": 500, "derivedForecast": 2},
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
