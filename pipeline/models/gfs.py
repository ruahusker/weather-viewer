"""GFS 0.25° model constants and interface contract.

GFS provides global coverage at 0.25° resolution (~28km), runs 4x daily (00/06/12/18Z),
and forecasts out to 16 days (384 hours). This makes it valuable for:
- Long-range chase outlooks (days 3-7+)
- Pattern recognition (troughs, ridges, jet streams)
- Global context when regional models are unavailable

GFS uses a regular lat/lon grid like ECMWF IFS, so no rotation needed for wind barbs.
Precipitation is 6-hourly accumulated (not hourly like HRRR).

NOTE: GFS pgrb2.0p25 has fewer storm-chasing parameters than HRRR:
- No updraft helicity (UH)
- No 0-1km SRH (only 0-3km)
- No bulk shear components (VUCSH/VVCSH) - only VWSH at odd levels
- Some params use different GRIB naming (PRMSL not MSLMA)
"""

MODEL_NAME = "gfs"


def get_run_fxx(run_hour: int) -> list[int]:
    """GFS runs at 00Z, 06Z, 12Z, 18Z; fxx 0-120 hourly, then 123-384 every 3h."""
    if run_hour in (0, 6, 12, 18):
        return list(range(0, 121)) + list(range(123, 385, 3))
    return []


def is_extended_run(run_hour: int) -> bool:
    """All GFS runs go to 384h, but we limit to 120h."""
    return run_hour in (0, 6, 12, 18)


# Herbie .idx search strings for partial GRIB download
# GFS pgrb2.0p25 contains the primary fields we need
PARAM_SEARCH_STRINGS = {
    # -- Surface fields --
    "temp_2m":      ":TMP:2 m above ground:",
    "dewpoint_2m":  ":DPT:2 m above ground:",
    "ugrd_10m":     ":UGRD:10 m above ground:",
    "vgrd_10m":     ":VGRD:10 m above ground:",
    "mslp":         ":PRMSL:mean sea level:",  # GFS uses PRMSL, not MSLMA
    "precip":       ":APCP:surface:",
    "wind_gusts":   ":GUST:surface:",
    "visibility":   ":VIS:surface:",
    "snow_depth":   ":SNOD:surface:",
    # GFS TCDC is by pressure level, not entire atmosphere - skip for now
    
    # -- Instability (storm chase essentials) --
    "cape":         ":CAPE:surface:",
    "cin":          ":CIN:surface:",
    "mucape":       ":CAPE:180-0 mb above ground:",
    "mlcape":       ":CAPE:90-0 mb above ground:",
    "mlcin":        ":CIN:90-0 mb above ground:",
    "cape_03km":    ":CAPE:0-3000 m above ground:",
    "lifted_index": ":LFTX:surface:",  # GFS uses LFTX not 4LFTX
    "pwat":         ":PWAT:entire atmosphere",
    
    # -- Helicity --
    # GFS only has 0-3km SRH, not 0-1km
    "srh_03km":     ":HLCY:3000-0 m above ground:",
    "reflectivity": ":REFC:entire atmosphere:",
    "mucin":        ":CIN:180-0 mb above ground:",
    "frzlvl":       ":HGT:0C isotherm:",
    "pbl_height":   ":HPBL:surface:",
    "weasd":        ":WEASD:surface:",
    
    # -- Pressure levels --
    "temp_850mb":   ":TMP:850 mb:",
    "temp_700mb":   ":TMP:700 mb:",
    "omega_700mb":  ":VVEL:700 mb:",
    "temp_500mb":   ":TMP:500 mb:",
    "gh_500mb":     ":HGT:500 mb:",
    "gh_850mb":     ":HGT:850 mb:",
    "rh_700mb":     ":RH:700 mb:",
    "rh_850mb":     ":RH:850 mb:",
    "ugrd_850mb":   ":UGRD:850 mb:",
    "vgrd_850mb":   ":VGRD:850 mb:",
    "ugrd_500mb":   ":UGRD:500 mb:",
    "vgrd_500mb":   ":VGRD:500 mb:",
    "ugrd_300mb":   ":UGRD:300 mb:",
    "vgrd_300mb":   ":VGRD:300 mb:",
    "ugrd_250mb":   ":UGRD:250 mb:",
    "vgrd_250mb":   ":VGRD:250 mb:",
    "temp_925mb":   ":TMP:925 mb:",
}

# No separate prs product needed — GFS has all fields in pgrb2.0p25
PRS_SEARCH_STRINGS = {}

# cfgrib shortNames for eccodes extraction
# GFS uses standard WMO shortNames
GRIB_SHORTNAMES = {
    "temp_2m":      "2t",
    "dewpoint_2m":  "2d",
    "ugrd_10m":     "10u",
    "vgrd_10m":     "10v",
    "mslp":         "prmsl",  # Note: different from HRRR's mslma
    "precip":       "tp",
    "wind_gusts":   "gust",
    "visibility":   "vis",
    "snow_depth":   "sde",
    "cape":         "cape",
    "cin":          "cin",
    "mucape":       "cape",
    "mlcape":       "cape",
    "mlcin":        "cin",
    "cape_03km":    "cape",
    "lifted_index": "lftx",  # Note: different from HRRR's 4lftx
    "pwat":         "pwat",
    "srh_03km":     "hlcy",
    "reflectivity": "refc",
    "mucin":        "cin",
    "frzlvl":       "gh",
    "pbl_height":   "hpbl",
    "weasd":        "weasd",
    "ugrd_250mb":   "u",
    "vgrd_250mb":   "v",
    "temp_925mb":   "t",
    "temp_850mb":   "t",
    "temp_700mb":   "t",
    "omega_700mb":  "w",
    "temp_500mb":   "t",
    "gh_500mb":     "gh",
    "gh_850mb":     "gh",
    "rh_700mb":     "r",
    "rh_850mb":     "r",
    "ugrd_850mb":   "u",
    "vgrd_850mb":   "v",
    "ugrd_500mb":   "u",
    "vgrd_500mb":   "v",
    "ugrd_300mb":   "u",
    "vgrd_300mb":   "v",
}

# Level type filters for eccodes
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
    "cape":         {"typeOfLevel": "surface", "level": 0},
    "cin":          {"typeOfLevel": "surface", "level": 0},
    "mucape":       {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
    "mlcape":       {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 9000},
    "mlcin":        {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 9000},
    "cape_03km":    {"typeOfLevel": "heightAboveGroundLayer"},
    "lifted_index": {"typeOfLevel": "surface"},  # GFS LFTX is at surface
    "pwat":         {"typeOfLevel": "atmosphereSingleLayer"},
    "srh_03km":     {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 3000},
    "temp_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_700mb":   {"typeOfLevel": "isobaricInhPa", "level": 700},
    "omega_700mb":  {"typeOfLevel": "isobaricInhPa", "level": 700},
    "temp_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_500mb":     {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "rh_700mb":     {"typeOfLevel": "isobaricInhPa", "level": 700},
    "rh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "ugrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "vgrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "ugrd_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "vgrd_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "ugrd_300mb":   {"typeOfLevel": "isobaricInhPa", "level": 300},
    "vgrd_300mb":   {"typeOfLevel": "isobaricInhPa", "level": 300},
    "ugrd_250mb":   {"typeOfLevel": "isobaricInhPa", "level": 250},
    "vgrd_250mb":   {"typeOfLevel": "isobaricInhPa", "level": 250},
    "temp_925mb":   {"typeOfLevel": "isobaricInhPa", "level": 925},
    "reflectivity": {"typeOfLevel": "atmosphere"},
    "mucin":        {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
    "frzlvl":       {"typeOfLevel": "isothermZero"},
    "pbl_height":   {"typeOfLevel": "surface"},
    "weasd":        {"typeOfLevel": "surface"},
}

# Source priority: NOMADS first (files appear sooner), AWS fallback
SOURCES_PRIORITY = ["nomads", "aws"]

# Derived parameters computed from u/v pairs
DERIVED_PARAMS = {
    "wind_speed":   ("ugrd_10m", "vgrd_10m"),
    # GFS doesn't have standard bulk shear - skip shear_01km and shear_06km
    "wind_850mb":   ("ugrd_850mb", "vgrd_850mb"),
    "wind_500mb":   ("ugrd_500mb", "vgrd_500mb"),
    "wind_300mb":   ("ugrd_300mb", "vgrd_300mb"),
    "wind_250mb":   ("ugrd_250mb", "vgrd_250mb"),
}

# Composite indices - GFS doesn't have all ingredients for STP/SCP
# (missing 0-1km SRH, no reliable bulk shear)
COMPOSITE_PARAMS = {}
