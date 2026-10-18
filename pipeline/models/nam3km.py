"""NAM 3km CONUS Nest model constants."""

MODEL_NAME = "nam3km"
HERBIE_MODEL = "nam"
HERBIE_PRODUCT = "conusnest.hiresf"
SOUNDING_LEVELS = (1000, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500, 450, 400, 350, 300, 250, 200)
SOUNDING_DEWPOINT_LEVELS = (1000, 925, 900, 850, 800, 750)


def get_run_fxx(run_hour: int) -> range:
    """NAM 3km runs at 00/06/12/18Z, fxx 0-60 hourly."""
    if run_hour in (0, 6, 12, 18):
        return range(0, 61)
    return range(0)


# Herbie .idx search strings — same format as HRRR
PARAM_SEARCH_STRINGS = {
    "temp_2m":      ":TMP:2 m above ground:",
    "dewpoint_2m":  ":DPT:2 m above ground:",
    "ugrd_10m":     ":UGRD:10 m above ground:",
    "vgrd_10m":     ":VGRD:10 m above ground:",
    "mslp":         ":PRMSL:mean sea level:",
    "precip":       ":APCP:surface:",
    "reflectivity": ":REFC:entire atmosphere (considered as a single layer):",
    # Categorical precipitation type (instantaneous only; GFS also has 0-6h averages).
    "crain":        ":CRAIN:surface:(?:anl|[0-9]+ hour fcst):",
    "csnow":        ":CSNOW:surface:(?:anl|[0-9]+ hour fcst):",
    "cicep":        ":CICEP:surface:(?:anl|[0-9]+ hour fcst):",
    "cfrzr":        ":CFRZR:surface:(?:anl|[0-9]+ hour fcst):",
    "mucape":       ":CAPE:180-0 mb above ground:",
    "mucin":        ":CIN:180-0 mb above ground:",
    "mlcape":       ":CAPE:90-0 mb above ground:",
    "mlcin":        ":CIN:90-0 mb above ground:",
    "wind_gusts":   ":GUST:surface:",
    "visibility":   ":VIS:surface:1 hour fcst:",
    "vil":          ":VIL:entire atmosphere",
    "lifted_index": ":4LFTX:180-0 mb above ground:",
    "frzlvl":       ":HGT:0C isotherm:",
    "snow_depth":   ":SNOD:surface:",
    "frozen_pct":   ":CPOFP:surface:",
    # Snowpack SWE (instantaneous); the file also has a bucketed WEASD accumulation
    "weasd":        ":WEASD:surface:(?:anl|[0-9]+ hour fcst):",
    "cloud_cover":  ":TCDC:entire atmosphere",
    "lightning":    ":LTNG:surface:",
    "srh_01km":     ":HLCY:1000-0 m above ground:",
    "srh_03km":     ":HLCY:3000-0 m above ground:",
    "uh_25km":      ":MXUPHL:5000-2000 m above ground:",
    "uh_03km":      ":MXUPHL:3000-0 m above ground:",
    "ushear_06km":  ":VUCSH:0-6000 m above ground:",
    "vshear_06km":  ":VVCSH:0-6000 m above ground:",
    "ushear_01km":  ":VUCSH:0-1000 m above ground:",
    "vshear_01km":  ":VVCSH:0-1000 m above ground:",
    "lcl":          ":HGT:level of adiabatic condensation from sfc:",
    # Additional fields for storm chasing composites
    "pwat":         ":PWAT:entire atmosphere",
    "pbl_height":   ":HPBL:surface:",
    "temp_850mb":   ":TMP:850 mb:",
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
    "gh_700mb":     ":HGT:700 mb:",
    "gh_500mb":     ":HGT:500 mb:",
    "gh_925mb":     ":HGT:925 mb:",
    "gh_850mb":     ":HGT:850 mb:",
    "gh_300mb":     ":HGT:300 mb:",
    "gh_250mb":     ":HGT:250 mb:",
    "gh_200mb":     ":HGT:200 mb:",
    "ugrd_925mb":   ":UGRD:925 mb:",
    "vgrd_925mb":   ":VGRD:925 mb:",
    "ugrd_850mb":   ":UGRD:850 mb:",
    "vgrd_850mb":   ":VGRD:850 mb:",
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
    "rh_700mb":     ":RH:700 mb:",
    "rh_850mb":     ":RH:850 mb:",
    "rh_500mb":     ":RH:500 mb:",
    "echo_tops":    ":RETOP:entire atmosphere",
}

for _level in SOUNDING_LEVELS:
    PARAM_SEARCH_STRINGS.setdefault(f"temp_{_level}mb", f":TMP:{_level} mb:")
    PARAM_SEARCH_STRINGS.setdefault(f"gh_{_level}mb", f":HGT:{_level} mb:")
    PARAM_SEARCH_STRINGS.setdefault(f"ugrd_{_level}mb", f":UGRD:{_level} mb:")
    PARAM_SEARCH_STRINGS.setdefault(f"vgrd_{_level}mb", f":VGRD:{_level} mb:")
    PARAM_SEARCH_STRINGS.setdefault(f"rh_{_level}mb", f":RH:{_level} mb:")
for _level in SOUNDING_DEWPOINT_LEVELS:
    PARAM_SEARCH_STRINGS.setdefault(f"dewpoint_{_level}mb", f":DPT:{_level} mb:")

PRS_SEARCH_STRINGS = {}

SOURCES_PRIORITY = ["nomads", "aws"]

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
    "wind_200mb":   ("ugrd_200mb", "vgrd_200mb"),
}

COMPOSITE_PARAMS = {
    "cti": {
        "requires": ["mlcape", "mlcin", "srh_01km", "shear_06km", "lcl"],
        "label": "Chase Target Index",
    },
    "stp": {
        "requires": ["mlcape", "mlcin", "srh_01km", "shear_06km"],
        "label": "Sig Tornado Param",
    },
    "scp": {
        "requires": ["mucape", "srh_03km", "shear_06km"],
        "label": "Supercell Composite",
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
        "requires": ["omega_700mb"],
        "label": "Forcing for Ascent",
    },
    "hp_risk": {
        "requires": ["visibility", "cloud_cover", "pbl_height", "rh_700mb", "rh_850mb"],
        "label": "Storm Obscuration / HP Risk",
    },
}
