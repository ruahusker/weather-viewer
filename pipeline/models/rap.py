"""RAP 13km CONUS model constants and interface contract.

RAP (Rapid Refresh) provides CONUS coverage at 13km resolution, runs every hour,
and is the direct parent model of HRRR. Good for:
- Hourly synoptic + mesoscale analysis between HRRR runs
- Pattern recognition at slightly coarser resolution
- Longer extended outlooks (51h on main cycles vs HRRR's 48h)

Herbie model/product: rap / awp130pgrb  (CONUS 13km pressure-level product)
Grid: Lambert Conformal (same projection family as HRRR, different resolution)
MSLP: uses MSLMA (same as HRRR, not PRMSL like GFS/NAM)
"""

MODEL_NAME = "rap"


def get_run_fxx(run_hour: int) -> list[int]:
    """RAP runs every hour. AWS only carries fxx 0-21 for all cycles."""
    return list(range(0, 22))


def is_extended_run(run_hour: int) -> bool:
    return run_hour in (0, 6, 12, 18)


# Herbie .idx search strings for partial GRIB download
PARAM_SEARCH_STRINGS = {
    # -- Surface fields --
    "temp_2m":      ":TMP:2 m above ground:",
    "dewpoint_2m":  ":DPT:2 m above ground:",
    "ugrd_10m":     ":UGRD:10 m above ground:",
    "vgrd_10m":     ":VGRD:10 m above ground:",
    "mslp":         ":MSLMA:mean sea level:",
    "precip":       ":APCP:surface:",
    "wind_gusts":   ":GUST:surface:",
    "visibility":   ":VIS:surface:",
    "snow_depth":   ":SNOD:surface:",
    "cloud_cover":  ":TCDC:entire atmosphere:",

    # -- Instability --
    "cape":         ":CAPE:surface:",
    "cin":          ":CIN:surface:",
    "mucape":       ":CAPE:180-0 mb above ground:",
    "mucin":        ":CIN:180-0 mb above ground:",
    "mlcape":       ":CAPE:90-0 mb above ground:",
    "mlcin":        ":CIN:90-0 mb above ground:",
    "lifted_index": ":4LFTX:180-0 mb above ground:",
    "pwat":         ":PWAT:entire atmosphere",

    # -- Wind Shear (0-6km only; no 0-1km bulk shear in pgrb product) --
    "ushear_06km":  ":VUCSH:0-6000 m above ground:",
    "vshear_06km":  ":VVCSH:0-6000 m above ground:",

    # -- Helicity --
    "srh_01km":     ":HLCY:1000-0 m above ground:",
    "srh_03km":     ":HLCY:3000-0 m above ground:",

    # -- Reflectivity --
    "reflectivity": ":REFC:entire atmosphere:",
    # Categorical precipitation type (instantaneous only; GFS also has 0-6h averages).
    "crain":        ":CRAIN:surface:(?:anl|[0-9]+ hour fcst):",
    "csnow":        ":CSNOW:surface:(?:anl|[0-9]+ hour fcst):",
    "cicep":        ":CICEP:surface:(?:anl|[0-9]+ hour fcst):",
    "cfrzr":        ":CFRZR:surface:(?:anl|[0-9]+ hour fcst):",

    # -- Pressure levels --
    "temp_850mb":   ":TMP:850 mb:",
    "temp_500mb":   ":TMP:500 mb:",
    "gh_500mb":     ":HGT:500 mb:",
    "gh_850mb":     ":HGT:850 mb:",
    "gh_925mb":     ":HGT:925 mb:",
    "gh_700mb":     ":HGT:700 mb:",
    "gh_300mb":     ":HGT:300 mb:",
    "gh_250mb":     ":HGT:250 mb:",
    "ugrd_850mb":   ":UGRD:850 mb:",
    "vgrd_850mb":   ":VGRD:850 mb:",
    "rh_700mb":     ":RH:700 mb:",
    "rh_850mb":     ":RH:850 mb:",
    "temp_700mb":   ":TMP:700 mb:",
    "omega_700mb":  ":VVEL:700 mb:",
    "temp_925mb":   ":TMP:925 mb:",
    "dewpoint_850mb": ":DPT:850 mb:",
    "lcl":          ":HGT:level of adiabatic condensation from sfc:",
    "cape_03km":    ":CAPE:0-3000 m above ground:",
    "frzlvl":       ":HGT:0C isotherm:",
    "frozen_pct":   ":CPOFP:surface:",
    "echo_tops":    ":RETOP:entire atmosphere:",
    "pbl_height":   ":HPBL:surface:",
    "weasd":        ":WEASD:surface:",
    "ugrd_500mb":   ":UGRD:500 mb:",
    "vgrd_500mb":   ":VGRD:500 mb:",
    "ugrd_300mb":   ":UGRD:300 mb:",
    "vgrd_300mb":   ":VGRD:300 mb:",
    "ugrd_250mb":   ":UGRD:250 mb:",
    "vgrd_250mb":   ":VGRD:250 mb:",
    "lightning":    ":LTNG:surface:",
    "vort_500mb":   ":ABSV:500 mb:",
}

PRS_SEARCH_STRINGS = {}

# cfgrib shortNames for eccodes extraction
GRIB_SHORTNAMES = {
    "temp_2m":      "2t",
    "dewpoint_2m":  "2d",
    "ugrd_10m":     "10u",
    "vgrd_10m":     "10v",
    "mslp":         "mslma",
    "precip":       "tp",
    "wind_gusts":   "gust",
    "visibility":   "vis",
    "snow_depth":   "sde",
    "cloud_cover":  "tcc",
    "cape":         "cape",
    "cin":          "cin",
    "mucape":       "cape",
    "mucin":        "cin",
    "mlcape":       "cape",
    "mlcin":        "cin",
    "lifted_index": "4lftx",
    "pwat":         "pwat",
    "ushear_06km":  "vucsh",
    "vshear_06km":  "vvcsh",
    "srh_01km":     "hlcy",
    "srh_03km":     "hlcy",
    "reflectivity": "refc",
    "temp_850mb":   "t",
    "temp_500mb":   "t",
    "gh_500mb":     "gh",
    "gh_850mb":     "gh",
    "gh_925mb":     "gh",
    "gh_700mb":     "gh",
    "gh_300mb":     "gh",
    "gh_250mb":     "gh",
    "ugrd_850mb":   "u",
    "vgrd_850mb":   "v",
    "rh_700mb":     "r",
    "rh_850mb":     "r",
    "temp_700mb":   "t",
    "omega_700mb":  "w",
    "temp_925mb":   "t",
    "dewpoint_850mb": "dpt",
    "lcl":          "gh",
    "cape_03km":    "cape",
    "frzlvl":       "gh",
    "frozen_pct":   "cpofp",
    "echo_tops":    "retop",
    "pbl_height":   "hpbl",
    "weasd":        "weasd",
    "ugrd_500mb":   "u",
    "vgrd_500mb":   "v",
    "ugrd_300mb":   "u",
    "vgrd_300mb":   "v",
    "ugrd_250mb":   "u",
    "vgrd_250mb":   "v",
    "lightning":    "ltng",
    "vort_500mb":   "absv",
}

LEVEL_TYPES = {
    "temp_2m":      {"typeOfLevel": "heightAboveGround", "level": 2},
    "dewpoint_2m":  {"typeOfLevel": "heightAboveGround", "level": 2},
    "ugrd_10m":     {"typeOfLevel": "heightAboveGround", "level": 10},
    "vgrd_10m":     {"typeOfLevel": "heightAboveGround", "level": 10},
    "mslp":         {"typeOfLevel": "meanSea"},
    "precip":       {"typeOfLevel": "surface"},
    "wind_gusts":   {"typeOfLevel": "surface"},
    "visibility":   {"typeOfLevel": "surface"},
    "snow_depth":   {"typeOfLevel": "surface"},
    "cloud_cover":  {"typeOfLevel": "atmosphereSingleLayer"},
    "cape":         {"typeOfLevel": "surface", "level": 0},
    "cin":          {"typeOfLevel": "surface", "level": 0},
    "mucape":       {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
    "mucin":        {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
    "mlcape":       {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 9000},
    "mlcin":        {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 9000},
    "lifted_index": {"typeOfLevel": "pressureFromGroundLayer"},
    "pwat":         {"typeOfLevel": "atmosphereSingleLayer"},
    "ushear_06km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0},
    "vshear_06km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0},
    "srh_01km":     {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 1000},
    "srh_03km":     {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 3000},
    "reflectivity": {"typeOfLevel": "atmosphereSingleLayer"},
    "temp_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_500mb":     {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "gh_925mb":     {"typeOfLevel": "isobaricInhPa", "level": 925},
    "gh_700mb":     {"typeOfLevel": "isobaricInhPa", "level": 700},
    "gh_300mb":     {"typeOfLevel": "isobaricInhPa", "level": 300},
    "gh_250mb":     {"typeOfLevel": "isobaricInhPa", "level": 250},
    "ugrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "vgrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "rh_700mb":     {"typeOfLevel": "isobaricInhPa", "level": 700},
    "rh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_700mb":   {"typeOfLevel": "isobaricInhPa", "level": 700},
    "omega_700mb":  {"typeOfLevel": "isobaricInhPa", "level": 700},
    "temp_925mb":   {"typeOfLevel": "isobaricInhPa", "level": 925},
    "dewpoint_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "lcl":          {"typeOfLevel": "adiabaticCondensation"},
    "cape_03km":    {"typeOfLevel": "heightAboveGroundLayer"},
    "frzlvl":       {"typeOfLevel": "isothermZero"},
    "frozen_pct":   {"typeOfLevel": "surface"},
    "echo_tops":    {"typeOfLevel": "atmosphere"},
    "pbl_height":   {"typeOfLevel": "surface"},
    "weasd":        {"typeOfLevel": "surface"},
    "ugrd_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "vgrd_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "ugrd_300mb":   {"typeOfLevel": "isobaricInhPa", "level": 300},
    "vgrd_300mb":   {"typeOfLevel": "isobaricInhPa", "level": 300},
    "ugrd_250mb":   {"typeOfLevel": "isobaricInhPa", "level": 250},
    "vgrd_250mb":   {"typeOfLevel": "isobaricInhPa", "level": 250},
    "lightning":    {"typeOfLevel": "surface"},
    "vort_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
}

SOURCES_PRIORITY = ["nomads", "aws", "google", "azure"]

DERIVED_PARAMS = {
    "wind_speed":   ("ugrd_10m", "vgrd_10m"),
    "shear_06km":   ("ushear_06km", "vshear_06km"),
    "wind_850mb":   ("ugrd_850mb", "vgrd_850mb"),
    "wind_500mb":   ("ugrd_500mb", "vgrd_500mb"),
    "wind_300mb":   ("ugrd_300mb", "vgrd_300mb"),
    "wind_250mb":   ("ugrd_250mb", "vgrd_250mb"),
}

COMPOSITE_PARAMS = {}
