"""ECMWF IFS model constants and interface contract."""

MODEL_NAME = "ecmwf"


def get_run_fxx(run_hour: int) -> range:
    """IFS runs at 00Z and 12Z, fxx 0-144 every 3 hours."""
    if run_hour in (0, 12):
        return range(0, 145, 3)
    return range(0)


def is_extended_run(run_hour: int) -> bool:
    return run_hour in (0, 12)


# Herbie .idx search strings for partial GRIB download
PARAM_SEARCH_STRINGS = {
    "temp_2m":      ":2t:sfc:",
    "dewpoint_2m":  ":2d:sfc:",
    "ugrd_10m":     ":10u:sfc:",
    "vgrd_10m":     ":10v:sfc:",
    "mslp":         ":msl:sfc:",
    "precip":       ":tp:sfc:",
    "wind_gusts":   ":10fg:sfc:",
    "mucape":       ":mucape:sfc:",
    "cloud_cover":  ":tcc:sfc:",
    "pwat":         ":tcwv:sfc:",
    "snowfall":     ":sf:sfc:",
    "snow_depth":   ":sd:sfc:",
    "temp_850mb":   ":t:850:pl:",
    "temp_700mb":   ":t:700:pl:",
    "omega_700mb":  ":w:700:pl:",
    "temp_500mb":   ":t:500:pl:",
    "gh_500mb":     ":gh:500:pl:",
    "gh_850mb":     ":gh:850:pl:",
    "gh_300mb":     ":gh:300:pl:",
    "rh_700mb":     ":r:700:pl:",
    "rh_850mb":     ":r:850:pl:",
    "ugrd_850mb":   ":u:850:pl:",
    "vgrd_850mb":   ":v:850:pl:",
    "ugrd_500mb":   ":u:500:pl:",
    "vgrd_500mb":   ":v:500:pl:",
    "ugrd_300mb":   ":u:300:pl:",
    "vgrd_300mb":   ":v:300:pl:",
}

PRS_SEARCH_STRINGS = {}

# cfgrib shortNames
GRIB_SHORTNAMES = {
    "temp_2m":      "2t",
    "dewpoint_2m":  "2d",
    "ugrd_10m":     "10u",
    "vgrd_10m":     "10v",
    "mslp":         "msl",
    "precip":       "tp",
    "wind_gusts":   "10fg",
    "mucape":       "mucape",
    "cloud_cover":  "tcc",
    "pwat":         "tcwv",
    "snowfall":     "sf",
    "snow_depth":   "sd",
    "temp_850mb":   "t",
    "temp_700mb":   "t",
    "omega_700mb":  "w",
    "temp_500mb":   "t",
    "gh_500mb":     "gh",
    "gh_850mb":     "gh",
    "gh_300mb":     "gh",
    "rh_700mb":     "r",
    "rh_850mb":     "r",
    "ugrd_850mb":   "u",
    "vgrd_850mb":   "v",
    "ugrd_500mb":   "u",
    "vgrd_500mb":   "v",
    "ugrd_300mb":   "u",
    "vgrd_300mb":   "v",
}

LEVEL_TYPES = {
    "temp_2m":      {"typeOfLevel": "heightAboveGround"},
    "dewpoint_2m":  {"typeOfLevel": "heightAboveGround"},
    "ugrd_10m":     {"typeOfLevel": "heightAboveGround"},
    "vgrd_10m":     {"typeOfLevel": "heightAboveGround"},
    "mslp":         {"typeOfLevel": "meanSea"},
    "precip":       {"typeOfLevel": "surface"},
    "wind_gusts":   {"typeOfLevel": "heightAboveGround"},
    "mucape":       {"typeOfLevel": "mostUnstableParcel"},
    "cloud_cover":  {"typeOfLevel": "entireAtmosphere"},
    "pwat":         {"typeOfLevel": "entireAtmosphere"},
    "snowfall":     {"typeOfLevel": "surface"},
    "snow_depth":   {"typeOfLevel": "surface"},
    "temp_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_700mb":   {"typeOfLevel": "isobaricInhPa", "level": 700},
    "omega_700mb":  {"typeOfLevel": "isobaricInhPa", "level": 700},
    "temp_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_500mb":     {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "gh_300mb":     {"typeOfLevel": "isobaricInhPa", "level": 300},
    "rh_700mb":     {"typeOfLevel": "isobaricInhPa", "level": 700},
    "rh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "ugrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "vgrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "ugrd_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "vgrd_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "ugrd_300mb":   {"typeOfLevel": "isobaricInhPa", "level": 300},
    "vgrd_300mb":   {"typeOfLevel": "isobaricInhPa", "level": 300},
}

SOURCES_PRIORITY = ["aws"]

# Derived (computed from u/v components)
DERIVED_PARAMS = {
    "wind_speed":   ("ugrd_10m", "vgrd_10m"),
    "wind_850mb":   ("ugrd_850mb", "vgrd_850mb"),
    "wind_500mb":   ("ugrd_500mb", "vgrd_500mb"),
    "wind_300mb":   ("ugrd_300mb", "vgrd_300mb"),
}

COMPOSITE_PARAMS = {}
