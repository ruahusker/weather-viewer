"""NAM 12km CONUS model constants and interface contract.

NAM (North American Mesoscale) provides continental-scale coverage at ~12km resolution,
runs 4x daily (00/06/12/18Z), and forecasts out to 84 hours (3.5 days).

The regular NAM has many storm-chasing parameters similar to NAM 3km but with
coarser resolution. Good for pattern recognition and backup when NAM 3km is delayed.

Herbie model/product: nam / awphys
Grid: Lambert Conformal (same as NAM 3km, HRRR)
"""

MODEL_NAME = "nam12km"


def get_run_fxx(run_hour: int) -> list[int]:
    """NAM runs at 00Z, 06Z, 12Z, 18Z; fxx 0-36 hourly, then 39-84 every 3h."""
    if run_hour in (0, 6, 12, 18):
        return list(range(0, 37)) + list(range(39, 85, 3))
    return []


def is_extended_run(run_hour: int) -> bool:
    """All NAM runs go to 84h."""
    return run_hour in (0, 6, 12, 18)


# Herbie .idx search strings for partial GRIB download
PARAM_SEARCH_STRINGS = {
    # -- Surface fields --
    "temp_2m":      ":TMP:2 m above ground:",
    "dewpoint_2m":  ":DPT:2 m above ground:",
    "ugrd_10m":     ":UGRD:10 m above ground:",
    "vgrd_10m":     ":VGRD:10 m above ground:",
    "mslp":         ":PRMSL:mean sea level:",
    "precip":       ":APCP:surface:",
    "wind_gusts":   ":GUST:surface:",
    "visibility":   ":VIS:surface:",
    "snow_depth":   ":SNOD:surface:",
    "cloud_cover":  ":TCDC:entire atmosphere",
    
    # -- Instability --
    "cape":         ":CAPE:surface:",
    "cin":          ":CIN:surface:",
    "mucape":       ":CAPE:180-0 mb above ground:",
    "mucin":        ":CIN:180-0 mb above ground:",
    "mlcape":       ":CAPE:90-0 mb above ground:",
    "mlcin":        ":CIN:90-0 mb above ground:",
    "lifted_index": ":4LFTX:180-0 mb above ground:",
    "pwat":         ":PWAT:entire atmosphere",
    
    # -- Wind Shear --
    # NAM has VUCSH/VVCSH at specific levels - only 0-6000m available
    "ushear_06km":  ":VUCSH:0-6000 m above ground:",
    "vshear_06km":  ":VVCSH:0-6000 m above ground:",
    
    # -- Helicity --
    # NAM only has 0-3km SRH, not 0-1km
    "srh_03km":     ":HLCY:3000-0 m above ground:",
    
    # -- Reflectivity --
    "reflectivity": ":REFC:entire atmosphere",
    "vil":          ":VIL:entire atmosphere",
    
    # -- Pressure levels --
    "temp_850mb":   ":TMP:850 mb:",
    "temp_700mb":   ":TMP:700 mb:",
    "omega_700mb":  ":VVEL:700 mb:",
    "temp_500mb":   ":TMP:500 mb:",
    "gh_500mb":     ":HGT:500 mb:",
    "gh_850mb":     ":HGT:850 mb:",
    "gh_925mb":     ":HGT:925 mb:",
    "gh_700mb":     ":HGT:700 mb:",
    "rh_700mb":     ":RH:700 mb:",
    "rh_850mb":     ":RH:850 mb:",
    "ugrd_850mb":   ":UGRD:850 mb:",
    "vgrd_850mb":   ":VGRD:850 mb:",

    # -- Winter --
    "frzlvl":       ":HGT:0C isotherm:",

    # -- Wind shear 0-1km --
    "ushear_01km":  ":VUCSH:0-1000 m above ground:",
    "vshear_01km":  ":VVCSH:0-1000 m above ground:",

    # -- Helicity 0-1km --
    "srh_01km":     ":HLCY:1000-0 m above ground:",

    # -- Storm chase extras --
    "lcl":          ":HGT:level of adiabatic condensation from sfc:",
    "cape_03km":    ":CAPE:0-3000 m above ground:",

    # -- Additional convective / storm fields --
    "echo_tops":    ":RETOP:entire atmosphere:",
    "lightning":    ":LTNG:surface:",
    "vort_500mb":   ":ABSV:500 mb:",
    "temp_925mb":   ":TMP:925 mb:",
}

PRS_SEARCH_STRINGS = {}

# cfgrib shortNames for eccodes extraction
GRIB_SHORTNAMES = {
    "temp_2m":      "2t",
    "dewpoint_2m":  "2d",
    "ugrd_10m":     "10u",
    "vgrd_10m":     "10v",
    "mslp":         "prmsl",
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
    "srh_03km":     "hlcy",
    "reflectivity": "refc",
    "vil":          "veril",
    "temp_850mb":   "t",
    "temp_700mb":   "t",
    "omega_700mb":  "w",
    "temp_500mb":   "t",
    "gh_500mb":     "gh",
    "gh_850mb":     "gh",
    "gh_925mb":     "gh",
    "gh_700mb":     "gh",
    "rh_700mb":     "r",
    "rh_850mb":     "r",
    "ugrd_850mb":   "u",
    "vgrd_850mb":   "v",
    "frzlvl":       "gh",
    "ushear_01km":  "vucsh",
    "vshear_01km":  "vvcsh",
    "srh_01km":     "hlcy",
    "lcl":          "gh",
    "cape_03km":    "cape",
    "echo_tops":    "retop",
    "lightning":    "ltng",
    "vort_500mb":   "absv",
    "temp_925mb":   "t",
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
    "ushear_06km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 6000},
    "vshear_06km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 6000},
    "srh_03km":     {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 3000},
    "reflectivity": {"typeOfLevel": "atmosphereSingleLayer"},
    "vil":          {"typeOfLevel": "atmosphereSingleLayer"},
    "temp_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "temp_700mb":   {"typeOfLevel": "isobaricInhPa", "level": 700},
    "omega_700mb":  {"typeOfLevel": "isobaricInhPa", "level": 700},
    "temp_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_500mb":     {"typeOfLevel": "isobaricInhPa", "level": 500},
    "gh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "gh_925mb":     {"typeOfLevel": "isobaricInhPa", "level": 925},
    "gh_700mb":     {"typeOfLevel": "isobaricInhPa", "level": 700},
    "rh_700mb":     {"typeOfLevel": "isobaricInhPa", "level": 700},
    "rh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "ugrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "vgrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "frzlvl":       {"typeOfLevel": "isothermZero"},
    "ushear_01km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 1000},
    "vshear_01km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 1000},
    "srh_01km":     {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 1000},
    "lcl":          {"typeOfLevel": "adiabaticCondensation"},
    "cape_03km":    {"typeOfLevel": "heightAboveGroundLayer"},
    "echo_tops":    {"typeOfLevel": "atmosphereSingleLayer"},
    "lightning":    {"typeOfLevel": "surface"},
    "vort_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "temp_925mb":   {"typeOfLevel": "isobaricInhPa", "level": 925},
}

SOURCES_PRIORITY = ["nomads", "aws"]

# Derived parameters computed from u/v pairs
DERIVED_PARAMS = {
    "wind_speed":   ("ugrd_10m", "vgrd_10m"),
    "shear_06km":   ("ushear_06km", "vshear_06km"),
    "shear_01km":   ("ushear_01km", "vshear_01km"),
    "wind_850mb":   ("ugrd_850mb", "vgrd_850mb"),
}

# NAM doesn't have all ingredients for STP/SCP (no 0-1km SRH, no LCL, no UH)
COMPOSITE_PARAMS = {}
