"""ECMWF AIFS model constants and interface contract.

AIFS (Artificial Intelligence Integrated Forecasting System) is ECMWF's ML-based
global model. Key advantages over IFS:
- Data published to public S3 ~4-6 hours EARLIER than IFS oper
- Runs 00Z and 12Z, fxx 0-360 every 6 hours (15-day forecast)
- Available within ~1 hour of model init vs 6-8h for IFS

Limitations vs IFS:
- No CAPE, CIN, SRH, or convective parameters (AI model doesn't output them)
- Coarser 6-hourly time steps (vs 3-hourly for IFS)
- PWAT from total column water (tcw) rather than water vapour only (tcwv)
- No geopotential height (stores geopotential z in m2/s2, needs /g conversion)

Herbie model/product: aifs / oper
Grid: Regular lat/lon 0.25 degrees
"""

MODEL_NAME = "aifs"


def get_run_fxx(run_hour: int) -> list[int]:
    """AIFS runs at 00Z and 12Z, fxx 0-360 every 6 hours."""
    if run_hour in (0, 12):
        return list(range(0, 361, 6))
    return []


def is_extended_run(run_hour: int) -> bool:
    return run_hour in (0, 12)


# Herbie .idx search strings for partial GRIB download
# AIFS index format: :param:levtype:domain:expver:class:type:stream:
PARAM_SEARCH_STRINGS = {
    # -- Surface fields --
    "temp_2m":      ":2t:sfc:",
    "dewpoint_2m":  ":2d:sfc:",
    "ugrd_10m":     ":10u:sfc:",
    "vgrd_10m":     ":10v:sfc:",
    "mslp":         ":msl:sfc:",
    "precip":       ":tp:sfc:",
    "cloud_cover":  ":tcc:sfc:",
    "snowfall":     ":sf:sfc:",
    "pwat":         ":tcw:sfc:",   # total column water ~= PWAT

    # -- Pressure levels --
    "temp_850mb":   ":t:850:pl:",
    "temp_500mb":   ":t:500:pl:",
}

PRS_SEARCH_STRINGS = {}

# cfgrib shortNames for eccodes extraction
GRIB_SHORTNAMES = {
    "temp_2m":      "2t",
    "dewpoint_2m":  "2d",
    "ugrd_10m":     "10u",
    "vgrd_10m":     "10v",
    "mslp":         "msl",
    "precip":       "tp",
    "cloud_cover":  "tcc",
    "snowfall":     "sf",
    "pwat":         "tcw",
    "temp_850mb":   "t",
    "temp_500mb":   "t",
}

LEVEL_TYPES = {
    "temp_2m":      {"typeOfLevel": "heightAboveGround", "level": 2},
    "dewpoint_2m":  {"typeOfLevel": "heightAboveGround", "level": 2},
    "ugrd_10m":     {"typeOfLevel": "heightAboveGround", "level": 10},
    "vgrd_10m":     {"typeOfLevel": "heightAboveGround", "level": 10},
    "mslp":         {"typeOfLevel": "meanSea"},
    "precip":       {"typeOfLevel": "surface"},
    "cloud_cover":  {"typeOfLevel": "entireAtmosphere"},
    "snowfall":     {"typeOfLevel": "surface"},
    "pwat":         {"typeOfLevel": "entireAtmosphere"},
    "temp_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
}

SOURCES_PRIORITY = ["aws"]

DERIVED_PARAMS = {
    "wind_speed": ("ugrd_10m", "vgrd_10m"),
}

COMPOSITE_PARAMS = {}
