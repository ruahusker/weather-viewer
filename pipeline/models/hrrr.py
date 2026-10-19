"""HRRR model constants and interface contract."""

MODEL_NAME = "hrrr"
SOUNDING_LEVELS = (1000, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400, 350, 300, 250, 200)
SOUNDING_DEWPOINT_LEVELS = (1000, 925, 900, 850, 800, 750)

# fxx ranges per run hour
def get_run_fxx(run_hour: int) -> range:
    """Return forecast hour range for the given run hour."""
    if run_hour in (0, 6, 12, 18):
        return range(0, 49)
    return range(0, 19)


def is_48h_run(run_hour: int) -> bool:
    return run_hour in (0, 6, 12, 18)


# Herbie .idx regex search strings for partial GRIB download.
# HLCY (SRH) is in the sfc product on HRRR (not prs).
PARAM_SEARCH_STRINGS = {
    "temp_2m":      ":TMP:2 m above ground:",
    "dewpoint_2m":  ":DPT:2 m above ground:",
    "ugrd_10m":     ":UGRD:10 m above ground:",
    "vgrd_10m":     ":VGRD:10 m above ground:",
    "mslp":         ":MSLMA:mean sea level:",
    "precip":       ":APCP:surface:",
    "reflectivity": ":REFC:entire atmosphere:",
    # Categorical precipitation type (instantaneous only; GFS also has 0-6h averages).
    "crain":        ":CRAIN:surface:(?:anl|[0-9]+ hour fcst):",
    "csnow":        ":CSNOW:surface:(?:anl|[0-9]+ hour fcst):",
    "cicep":        ":CICEP:surface:(?:anl|[0-9]+ hour fcst):",
    "cfrzr":        ":CFRZR:surface:(?:anl|[0-9]+ hour fcst):",
    "cape":         ":CAPE:surface:",
    "cin":          ":CIN:surface:",
    "wind_gusts":   ":GUST:surface:",
    "ushear_06km":  ":VUCSH:0-6000 m above ground:",
    "vshear_06km":  ":VVCSH:0-6000 m above ground:",
    "ushear_01km":  ":VUCSH:0-1000 m above ground:",
    "vshear_01km":  ":VVCSH:0-1000 m above ground:",
    "lifted_index": ":4LFTX:180-0 mb above ground:",
    "visibility":   ":VIS:surface:",
    "snow_depth":   ":SNOD:surface:",
    "frozen_pct":   ":CPOFP:surface:",
    "frzlvl":       ":HGT:0C isotherm:",
    "temp_850mb":   ":TMP:850 mb:",
    "vil":          ":VIL:entire atmosphere:",
    "uh_25km":      ":MXUPHL:5000-2000 m above ground:",
    "srh_01km":     ":HLCY:1000-0 m above ground:",
    "srh_03km":     ":HLCY:3000-0 m above ground:",
    # Storm chase params
    "mlcape":       ":CAPE:90-0 mb above ground:",
    "mlcin":        ":CIN:90-0 mb above ground:",
    "mucape":       ":CAPE:180-0 mb above ground:",
    "cape_03km":    ":CAPE:0-3000 m above ground:",
    "lcl":          ":HGT:level of adiabatic condensation from sfc:",
    "pwat":         ":PWAT:entire atmosphere",
    "uh_02km":      ":MXUPHL:2000-0 m above ground:",
    "max_wind":     ":WIND:10 m above ground:",
    # Components of the hourly max 10 m wind: direction for Max 10m Wind barbs.
    "maxuw_10m":    ":MAXUW:10 m above ground:",
    "maxvw_10m":    ":MAXVW:10 m above ground:",
    "efhl":         ":EFHL:surface:",
    "cloud_cover":  ":TCDC:entire atmosphere:",
    "lightning":    ":LTNG:entire atmosphere:",
    "mucin":        ":CIN:180-0 mb above ground:",
    # Pressure levels — full suite (all in HRRR sfc product)
    "temp_700mb":   ":TMP:700 mb:",
    "omega_700mb":  ":VVEL:700 mb:",
    "temp_500mb":   ":TMP:500 mb:",
    "vort_500mb":   ":ABSV:500 mb:",
    "temp_925mb":   ":TMP:925 mb:",
    "temp_300mb":   ":TMP:300 mb:",
    "temp_250mb":   ":TMP:250 mb:",
    "temp_200mb":   ":TMP:200 mb:",
    "dewpoint_850mb": ":DPT:850 mb:",
    "dewpoint_925mb": ":DPT:925 mb:",
    "rh_700mb":     ":RH:700 mb:",
    "rh_850mb":     ":RH:850 mb:",
    "rh_500mb":     ":RH:500 mb:",
    "gh_700mb":     ":HGT:700 mb:",
    "gh_850mb":     ":HGT:850 mb:",
    "gh_925mb":     ":HGT:925 mb:",
    "gh_300mb":     ":HGT:300 mb:",
    "gh_250mb":     ":HGT:250 mb:",
    "gh_200mb":     ":HGT:200 mb:",
    "ugrd_850mb":   ":UGRD:850 mb:",
    "vgrd_850mb":   ":VGRD:850 mb:",
    "ugrd_925mb":   ":UGRD:925 mb:",
    "vgrd_925mb":   ":VGRD:925 mb:",
    "ugrd_700mb":   ":UGRD:700 mb:",
    "vgrd_700mb":   ":VGRD:700 mb:",
    "ugrd_500mb":   ":UGRD:500 mb:",
    "vgrd_500mb":   ":VGRD:500 mb:",
    "ugrd_300mb":   ":UGRD:300 mb:",
    "vgrd_300mb":   ":VGRD:300 mb:",
    "ugrd_250mb":   ":UGRD:250 mb:",
    "vgrd_250mb":   ":VGRD:250 mb:",
    "ugrd_200mb":   ":UGRD:200 mb:",
    "vgrd_200mb":   ":VGRD:200 mb:",
    # Storm chasing extras
    "echo_tops":    ":RETOP:cloud top:",
    "hail":         ":HAIL:entire atmosphere:",
    "pbl_height":   ":HPBL:surface:",
    "weasd":        ":WEASD:surface:(?:anl|[0-9]+ hour fcst):",  # snowpack SWE, not the accumulations
    "uh_03km":      ":MXUPHL:3000-0 m above ground:",
}

for _level in SOUNDING_LEVELS:
    PARAM_SEARCH_STRINGS.setdefault(f"temp_{_level}mb", f":TMP:{_level} mb:")
    PARAM_SEARCH_STRINGS.setdefault(f"gh_{_level}mb", f":HGT:{_level} mb:")
    PARAM_SEARCH_STRINGS.setdefault(f"ugrd_{_level}mb", f":UGRD:{_level} mb:")
    PARAM_SEARCH_STRINGS.setdefault(f"vgrd_{_level}mb", f":VGRD:{_level} mb:")
    PARAM_SEARCH_STRINGS.setdefault(f"rh_{_level}mb", f":RH:{_level} mb:")
for _level in SOUNDING_DEWPOINT_LEVELS:
    PARAM_SEARCH_STRINGS.setdefault(f"dewpoint_{_level}mb", f":DPT:{_level} mb:")

# Some upper-air fields are more reliably available from the HRRR `prs`
# product than from `sfc`. Append them so soundings and combined products
# get a deeper, more complete profile.
PRS_SEARCH_STRINGS = {
    "omega_700mb": ":VVEL:700 mb:",
    "vort_500mb": ":ABSV:500 mb:",
}

for _level in SOUNDING_LEVELS:
    PRS_SEARCH_STRINGS.setdefault(f"temp_{_level}mb", f":TMP:{_level} mb:")
    PRS_SEARCH_STRINGS.setdefault(f"gh_{_level}mb", f":HGT:{_level} mb:")
    PRS_SEARCH_STRINGS.setdefault(f"rh_{_level}mb", f":RH:{_level} mb:")

# cfgrib filter_by_keys: shortName (GRIB2 shortName or paramId)
GRIB_SHORTNAMES = {
    "temp_2m":      "2t",
    "dewpoint_2m":  "2d",
    "ugrd_10m":     "10u",
    "vgrd_10m":     "10v",
    "mslp":         "mslma",
    "precip":       "tp",
    "reflectivity": "refc",
    "crain":        "crain",
    "csnow":        "csnow",
    "cicep":        "cicep",
    "cfrzr":        "cfrzr",
    "cape":         "cape",
    "cin":          "cin",
    "wind_gusts":   "gust",
    "ushear_06km":  "vucsh",
    "vshear_06km":  "vvcsh",
    "ushear_01km":  "vucsh",
    "vshear_01km":  "vvcsh",
    "lifted_index": "4lftx",
    "visibility":   "vis",
    "snow_depth":   "sde",
    "frozen_pct":   "cpofp",
    "frzlvl":       "gh",
    "temp_850mb":   "t",
    "vil":          "veril",
    "uh_25km":      "unknown",
    "srh_01km":     "hlcy",
    "srh_03km":     "hlcy",
    "mlcape":       "cape",
    "mlcin":        "cin",
    "mucape":       "cape",
    "cape_03km":    "cape",
    "lcl":          "gh",
    "pwat":         "pwat",
    "uh_02km":      "unknown",
    "max_wind":     "max_10si",
    # eccodes has no names for NCEP's MAXUW/MAXVW; LEVEL_TYPES pins them by number.
    "maxuw_10m":    "unknown",
    "maxvw_10m":    "unknown",
    "efhl":         "unknown",
    "cloud_cover":  "tcc",
    "lightning":    "ltng",
    "mucin":        "cin",
    "temp_700mb":   "t",
    "omega_700mb":  "w",
    "temp_500mb":   "t",
    "vort_500mb":   "absv",
    "temp_925mb":   "t",
    "temp_300mb":   "t",
    "temp_250mb":   "t",
    "temp_200mb":   "t",
    "dewpoint_850mb": "dpt",
    "dewpoint_925mb": "dpt",
    "gh_700mb":     "gh",
    "gh_850mb":     "gh",
    "gh_925mb":     "gh",
    "gh_300mb":     "gh",
    "gh_250mb":     "gh",
    "gh_200mb":     "gh",
    "ugrd_850mb":   "u",
    "vgrd_850mb":   "v",
    "ugrd_925mb":   "u",
    "vgrd_925mb":   "v",
    "ugrd_700mb":   "u",
    "vgrd_700mb":   "v",
    "ugrd_500mb":   "u",
    "vgrd_500mb":   "v",
    "ugrd_300mb":   "u",
    "vgrd_300mb":   "v",
    "ugrd_250mb":   "u",
    "vgrd_250mb":   "v",
    "ugrd_200mb":   "u",
    "vgrd_200mb":   "v",
    "echo_tops":    "retop",
    "hail":         "hail",
    "pbl_height":   "hpbl",
    "weasd":        "sdwe",
    "uh_03km":      "unknown",
    # IFS-specific (shared lookup)
    "snowfall":     "sf",
    "gh_500mb":     "gh",
    "rh_700mb":     "r",
    "mucin":        "cin",
}

for _level in SOUNDING_LEVELS:
    GRIB_SHORTNAMES.setdefault(f"temp_{_level}mb", "t")
    GRIB_SHORTNAMES.setdefault(f"gh_{_level}mb", "gh")
    GRIB_SHORTNAMES.setdefault(f"ugrd_{_level}mb", "u")
    GRIB_SHORTNAMES.setdefault(f"vgrd_{_level}mb", "v")
    GRIB_SHORTNAMES.setdefault(f"rh_{_level}mb", "r")
for _level in SOUNDING_DEWPOINT_LEVELS:
    GRIB_SHORTNAMES.setdefault(f"dewpoint_{_level}mb", "dpt")

LEVEL_TYPES = {
    "temp_2m":      {"typeOfLevel": "heightAboveGround", "level": 2},
    "dewpoint_2m":  {"typeOfLevel": "heightAboveGround", "level": 2},
    "ugrd_10m":     {"typeOfLevel": "heightAboveGround", "level": 10},
    "vgrd_10m":     {"typeOfLevel": "heightAboveGround", "level": 10},
    "mslp":         {"typeOfLevel": "meanSea"},
    "precip":       {"typeOfLevel": "surface"},
    "reflectivity": {"typeOfLevel": "atmosphere"},
    "crain":        {"typeOfLevel": "surface", "stepType": "instant"},
    "csnow":        {"typeOfLevel": "surface", "stepType": "instant"},
    "cicep":        {"typeOfLevel": "surface", "stepType": "instant"},
    "cfrzr":        {"typeOfLevel": "surface", "stepType": "instant"},
    "cape":         {"typeOfLevel": "surface", "level": 0},
    "cin":          {"typeOfLevel": "surface", "level": 0},
    "wind_gusts":   {"typeOfLevel": "surface"},
    # Shear: level=0, distinguished by bottomLevel
    "ushear_06km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 6000},
    "vshear_06km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 6000},
    "ushear_01km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 1000},
    "vshear_01km":  {"typeOfLevel": "heightAboveGroundLayer", "level": 0, "bottomLevel": 1000},
    "lifted_index": {"typeOfLevel": "pressureFromGroundLayer"},
    "visibility":   {"typeOfLevel": "surface"},
    "snow_depth":   {"typeOfLevel": "surface"},
    "frozen_pct":   {"typeOfLevel": "surface"},
    "frzlvl":       {"typeOfLevel": "isothermZero"},
    "temp_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "vil":          {"typeOfLevel": "atmosphere"},
    "uh_25km":      {
        "typeOfLevel": "heightAboveGroundLayer",
        "topLevel": 5000,
        "bottomLevel": 2000,
        "stepType": "max",
    },
    # SRH: sfc product, heightAboveGroundLayer filtered by topLevel
    "srh_01km":     {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 1000},
    "srh_03km":     {"typeOfLevel": "heightAboveGroundLayer", "topLevel": 3000},
    # Storm chase params
    "mlcape":       {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 9000},
    "mlcin":        {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 9000},
    "mucape":       {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
    "cape_03km":    {"typeOfLevel": "heightAboveGroundLayer"},
    "lcl":          {"typeOfLevel": "adiabaticCondensation"},
    "pwat":         {"typeOfLevel": "atmosphereSingleLayer"},
    "uh_02km":      {
        "typeOfLevel": "heightAboveGroundLayer",
        "topLevel": 2000,
        "bottomLevel": 0,
        "stepType": "max",
    },
    "max_wind":     {"typeOfLevel": "heightAboveGround"},
    "maxuw_10m":    {"typeOfLevel": "heightAboveGround", "level": 10, "stepType": "max",
                     "discipline": 0, "parameterCategory": 2, "parameterNumber": 222},
    "maxvw_10m":    {"typeOfLevel": "heightAboveGround", "level": 10, "stepType": "max",
                     "discipline": 0, "parameterCategory": 2, "parameterNumber": 223},
    "efhl":         {"typeOfLevel": "surface", "stepType": "instant"},
    "cloud_cover":  {"typeOfLevel": "atmosphere"},
    "lightning":    {"typeOfLevel": "atmosphere"},
    "mucin":        {"typeOfLevel": "pressureFromGroundLayer", "topLevel": 18000},
    "temp_700mb":   {"typeOfLevel": "isobaricInhPa", "level": 700},
    "omega_700mb":  {"typeOfLevel": "isobaricInhPa", "level": 700},
    "temp_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "vort_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "temp_925mb":   {"typeOfLevel": "isobaricInhPa", "level": 925},
    "temp_300mb":   {"typeOfLevel": "isobaricInhPa", "level": 300},
    "temp_250mb":   {"typeOfLevel": "isobaricInhPa", "level": 250},
    "temp_200mb":   {"typeOfLevel": "isobaricInhPa", "level": 200},
    "dewpoint_850mb": {"typeOfLevel": "isobaricInhPa", "level": 850},
    "dewpoint_925mb": {"typeOfLevel": "isobaricInhPa", "level": 925},
    "gh_700mb":     {"typeOfLevel": "isobaricInhPa", "level": 700},
    "gh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "gh_925mb":     {"typeOfLevel": "isobaricInhPa", "level": 925},
    "gh_300mb":     {"typeOfLevel": "isobaricInhPa", "level": 300},
    "gh_250mb":     {"typeOfLevel": "isobaricInhPa", "level": 250},
    "gh_200mb":     {"typeOfLevel": "isobaricInhPa", "level": 200},
    "ugrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "vgrd_850mb":   {"typeOfLevel": "isobaricInhPa", "level": 850},
    "ugrd_925mb":   {"typeOfLevel": "isobaricInhPa", "level": 925},
    "vgrd_925mb":   {"typeOfLevel": "isobaricInhPa", "level": 925},
    "ugrd_700mb":   {"typeOfLevel": "isobaricInhPa", "level": 700},
    "vgrd_700mb":   {"typeOfLevel": "isobaricInhPa", "level": 700},
    "ugrd_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "vgrd_500mb":   {"typeOfLevel": "isobaricInhPa", "level": 500},
    "ugrd_300mb":   {"typeOfLevel": "isobaricInhPa", "level": 300},
    "vgrd_300mb":   {"typeOfLevel": "isobaricInhPa", "level": 300},
    "ugrd_250mb":   {"typeOfLevel": "isobaricInhPa", "level": 250},
    "vgrd_250mb":   {"typeOfLevel": "isobaricInhPa", "level": 250},
    "ugrd_200mb":   {"typeOfLevel": "isobaricInhPa", "level": 200},
    "vgrd_200mb":   {"typeOfLevel": "isobaricInhPa", "level": 200},
    "echo_tops":    {"typeOfLevel": "cloudTop"},
    "hail":         {"typeOfLevel": "atmosphere"},
    "pbl_height":   {"typeOfLevel": "surface"},
    "weasd":        {"typeOfLevel": "surface", "stepType": "instant"},
    "uh_03km":      {
        "typeOfLevel": "heightAboveGroundLayer",
        "topLevel": 3000,
        "bottomLevel": 0,
        "stepType": "max",
    },
    # IFS-specific (shared lookup)
    "snowfall":     {"typeOfLevel": "surface"},
    "gh_500mb":     {"typeOfLevel": "isobaricInhPa", "level": 500},
    "rh_700mb":     {"typeOfLevel": "isobaricInhPa", "level": 700},
    "rh_850mb":     {"typeOfLevel": "isobaricInhPa", "level": 850},
    "rh_500mb":     {"typeOfLevel": "isobaricInhPa", "level": 500},
}

for _level in SOUNDING_LEVELS:
    LEVEL_TYPES.setdefault(f"temp_{_level}mb", {"typeOfLevel": "isobaricInhPa", "level": _level})
    LEVEL_TYPES.setdefault(f"gh_{_level}mb", {"typeOfLevel": "isobaricInhPa", "level": _level})
    LEVEL_TYPES.setdefault(f"ugrd_{_level}mb", {"typeOfLevel": "isobaricInhPa", "level": _level})
    LEVEL_TYPES.setdefault(f"vgrd_{_level}mb", {"typeOfLevel": "isobaricInhPa", "level": _level})
    LEVEL_TYPES.setdefault(f"rh_{_level}mb", {"typeOfLevel": "isobaricInhPa", "level": _level})
for _level in SOUNDING_DEWPOINT_LEVELS:
    LEVEL_TYPES.setdefault(f"dewpoint_{_level}mb", {"typeOfLevel": "isobaricInhPa", "level": _level})

SOURCES_PRIORITY = ["nomads", "aws", "google", "azure", "pando", "pando2"]

# Parameters rendered as a derived product (not directly a GRIB field)
DERIVED_PARAMS = {
    "wind_speed":   ("ugrd_10m", "vgrd_10m"),
    "shear_06km":   ("ushear_06km", "vshear_06km"),
    "shear_01km":   ("ushear_01km", "vshear_01km"),
    "wind_925mb":   ("ugrd_925mb", "vgrd_925mb"),
    "wind_850mb":   ("ugrd_850mb", "vgrd_850mb"),
    "wind_700mb":   ("ugrd_700mb", "vgrd_700mb"),
    "wind_500mb":   ("ugrd_500mb", "vgrd_500mb"),
    "wind_300mb":   ("ugrd_300mb", "vgrd_300mb"),
    "wind_250mb":   ("ugrd_250mb", "vgrd_250mb"),
}

# Composite indices derived from multiple already-processed fields
COMPOSITE_PARAMS = {
    # Composite reflectivity colored by categorical surface precip type.
    # Shared by every model that publishes CRAIN/CSNOW/CICEP/CFRZR.
    "precip_type": {
        "requires": ["reflectivity", "crain", "csnow", "cicep", "cfrzr"],
        "label": "Precip Type",
    },
    "stp": {
        "requires": ["cape", "cin", "srh_01km", "shear_06km"],
        "label": "Sig Tornado Param",
    },
    "scp": {
        "requires": ["cape", "srh_03km", "shear_06km"],
        "label": "Supercell Composite",
    },
    "cti": {
        "requires": ["mlcape", "mlcin", "srh_01km", "shear_06km", "lcl"],
        "label": "Chase Target Index",
    },
    "msi": {
        "requires": ["mucape", "mucin", "srh_03km", "shear_06km", "cape_03km"],
        "label": "Mesocyclone Strength",
    },
    "smi": {
        "requires": ["mlcape", "shear_01km", "shear_06km", "lcl"],
        "label": "Storm Mode Index",
    },
    "ship": {
        "requires": ["mucape", "temp_700mb", "temp_500mb", "frzlvl", "shear_06km"],
        "label": "Sig Hail Parameter",
    },
    "dcp": {
        "requires": ["mucape", "temp_700mb", "temp_2m", "shear_06km", "wind_gusts"],
        "label": "Derecho Composite",
    },
    "citi": {
        "requires": ["mlcape", "mlcin", "pbl_height", "cloud_cover", "omega_700mb"],
        "label": "CI Timing Index",
    },
    "cvi": {
        "requires": ["visibility", "cloud_cover", "pbl_height"],
        "label": "Chase Visibility",
    },
    "llmt": {
        "requires": ["dewpoint_850mb", "wind_850mb", "rh_850mb"],
        "label": "Low-Level Moisture Transport",
    },
    "ascent": {
        "requires": ["omega_700mb", "wind_250mb"],
        "label": "Forcing for Ascent",
    },
    "hp_risk": {
        "requires": ["visibility", "cloud_cover", "pbl_height", "rh_700mb", "rh_850mb"],
        "label": "Storm Obscuration / HP Risk",
    },
}
