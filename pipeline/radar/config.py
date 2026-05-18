"""Radar configuration — Level II NEXRAD sites and products."""

from pathlib import Path

# Import FIELD_CONFIG from color_tables for hover value lookup support
from .color_tables import FIELD_CONFIG as RADAR_FIELD_CONFIG

# Radar data directories
RADAR_CACHE_DIR = Path("/home/main/weather-viewer/data/radar_cache/level2")
RADAR_RENDERED_DIR = Path("/home/main/weather-viewer/data/rendered/radar")

# Ensure directories exist
RADAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
RADAR_RENDERED_DIR.mkdir(parents=True, exist_ok=True)

# Retention
RAW_RETENTION_HOURS = 2       # keep .ar2v files for on-demand tilt rendering
RENDERED_RETENTION_HOURS = 2  # keep pre-rendered PNGs
ONDEMAND_RETENTION_MINUTES = 30

# Rendering
OUTPUT_SIZE = 2400            # px diameter of rendered images
CC_CLUTTER_THRESHOLD = 0.3   # mask gates with CC below this
PNG_OPTIMIZE = False         # faster write path for high-frequency radar renders

# Scheduler - active path is scheduler_pipeline with S3-first ingest
POLL_INTERVAL_SECONDS = 2     # shorter scheduler cadence for lower ingest latency
DOWNLOAD_WORKERS = 12         # more source checks/downloads in parallel
RENDER_WORKERS = 4            # processes for PyART parse + render
RADAR_WORKER_MAX_TASKS = 25   # keep workers warmer so renderer caches persist longer
RENDER_TIMEOUT_SECONDS = 180  # per-site render timeout — increased for CPUQuota=50% throttling
MAX_VOLUME_AGE_HOURS = 2      # S3/THREDDS lookback window
CLEANUP_INTERVAL_SECONDS = 300  # avoid heavy full-dir scans every minute
CLEANUP_IN_PROCESS = False      # cleanup is handled by the hourly cron job

# Download failure blacklisting — skip sites that fail repeatedly
SITE_FAILURE_BLACKLIST_THRESHOLD = 10   # consecutive failures before blacklisting
SITE_BLACKLIST_COOLDOWN_SECONDS = 300   # re-try blacklisted sites after 5 minutes

# Fast-mode product subset — render only these high-value products per cycle.
# Set to None to render all products from FIELD_CONFIG.
FAST_RENDER_PRODUCTS = [
    "reflectivity",
    "velocity",
    "differential_reflectivity",
    "cross_correlation_ratio",
    "specific_differential_phase",
    "storm_relative_velocity",
    "composite_reflectivity",
    "echo_tops",
    "vertically_integrated_liquid",
    "hail_probability",
    "rainfall_rate",
]

# Processing strategy used by scheduler_pipeline:
# priority-driven polling with low-latency rendering of newer data first
OLDEST_FIRST_RENDERING = True

# NEXRAD WSR-88D Sites - CONUS coverage
# Organized by region for smart polling
NEXRAD_SITES = {
    # Nebraska (highest priority)
    "KOAX": {"name": "Omaha", "state": "NE", "lat": 41.320, "lon": -96.370, "priority": 1},
    "KUEX": {"name": "Hastings", "state": "NE", "lat": 40.321, "lon": -98.442, "priority": 1},
    "KLNX": {"name": "North Platte", "state": "NE", "lat": 41.957, "lon": -100.576, "priority": 1},

    # Central Plains
    "KDMX": {"name": "Des Moines", "state": "IA", "lat": 41.731, "lon": -93.723, "priority": 2},
    "KDVN": {"name": "Davenport", "state": "IA", "lat": 41.612, "lon": -90.581, "priority": 2},
    "KARX": {"name": "La Crosse", "state": "WI", "lat": 43.822, "lon": -91.191, "priority": 2},
    "KEAX": {"name": "Kansas City", "state": "MO", "lat": 38.810, "lon": -94.264, "priority": 2},
    "KLSX": {"name": "St. Louis", "state": "MO", "lat": 38.699, "lon": -90.683, "priority": 2},
    "KSGF": {"name": "Springfield", "state": "MO", "lat": 37.235, "lon": -93.400, "priority": 2},
    "KDDC": {"name": "Dodge City", "state": "KS", "lat": 37.761, "lon": -99.969, "priority": 2},
    "KGLD": {"name": "Goodland", "state": "KS", "lat": 39.367, "lon": -101.700, "priority": 2},
    "KICT": {"name": "Wichita", "state": "KS", "lat": 37.655, "lon": -97.443, "priority": 2},
    "KTWX": {"name": "Topeka", "state": "KS", "lat": 38.997, "lon": -96.232, "priority": 2},
    "KFTG": {"name": "Denver", "state": "CO", "lat": 39.786, "lon": -104.546, "priority": 2},
    "KGJX": {"name": "Grand Junction", "state": "CO", "lat": 39.062, "lon": -108.214, "priority": 2},
    "KPUX": {"name": "Pueblo", "state": "CO", "lat": 38.460, "lon": -104.181, "priority": 2},
    "KABR": {"name": "Aberdeen", "state": "SD", "lat": 45.456, "lon": -98.413, "priority": 2},
    "KFSD": {"name": "Sioux Falls", "state": "SD", "lat": 43.588, "lon": -96.729, "priority": 2},
    "KUDX": {"name": "Rapid City", "state": "SD", "lat": 44.125, "lon": -102.829, "priority": 2},
    
    # Northern Plains
    "KMVX": {"name": "Fargo", "state": "ND", "lat": 47.528, "lon": -97.325, "priority": 2},
    "KMBX": {"name": "Minot", "state": "ND", "lat": 48.393, "lon": -100.864, "priority": 2},
    "KBIS": {"name": "Bismarck", "state": "ND", "lat": 46.771, "lon": -100.760, "priority": 2},
    "KBLX": {"name": "Billings", "state": "MT", "lat": 45.854, "lon": -108.607, "priority": 2},
    "KGGW": {"name": "Glasgow", "state": "MT", "lat": 48.206, "lon": -106.624, "priority": 2},
    "KMSX": {"name": "Missoula", "state": "MT", "lat": 47.041, "lon": -114.107, "priority": 2},
    "KTFX": {"name": "Great Falls", "state": "MT", "lat": 47.460, "lon": -111.384, "priority": 2},
    "KCBX": {"name": "Boise", "state": "ID", "lat": 43.490, "lon": -116.236, "priority": 2},
    
    # Southern Plains (Tornado Alley)
    "KTLX": {"name": "Oklahoma City", "state": "OK", "lat": 35.333, "lon": -97.278, "priority": 2},
    "KINX": {"name": "Tulsa", "state": "OK", "lat": 36.175, "lon": -95.564, "priority": 2},
    "KAMA": {"name": "Amarillo", "state": "TX", "lat": 35.233, "lon": -101.709, "priority": 2},
    "KFWS": {"name": "Dallas/Fort Worth", "state": "TX", "lat": 32.573, "lon": -97.303, "priority": 2},
    "KGRK": {"name": "Central Texas", "state": "TX", "lat": 30.722, "lon": -97.383, "priority": 2},
    "KHGX": {"name": "Houston", "state": "TX", "lat": 29.472, "lon": -95.079, "priority": 2},
    "KMRX": {"name": "Knoxville", "state": "TN", "lat": 36.169, "lon": -83.402, "priority": 2},
    "KSRX": {"name": "Fort Smith", "state": "AR", "lat": 35.290, "lon": -94.362, "priority": 2},
    "KLZK": {"name": "Little Rock", "state": "AR", "lat": 34.836, "lon": -92.262, "priority": 2},
    "KPOE": {"name": "Fort Polk", "state": "LA", "lat": 31.155, "lon": -92.976, "priority": 2},
    "KSHV": {"name": "Shreveport", "state": "LA", "lat": 32.451, "lon": -93.841, "priority": 2},
    "KLCH": {"name": "Lake Charles", "state": "LA", "lat": 30.125, "lon": -93.216, "priority": 2},
    "KLBB": {"name": "Lubbock", "state": "TX", "lat": 33.654, "lon": -101.814, "priority": 2},
    "KMAF": {"name": "Midland/Odessa", "state": "TX", "lat": 31.943, "lon": -102.189, "priority": 2},
    "KSJT": {"name": "San Angelo", "state": "TX", "lat": 31.371, "lon": -100.493, "priority": 2},
    "KDYX": {"name": "Dyess AFB", "state": "TX", "lat": 32.538, "lon": -99.254, "priority": 2},
    "KFDR": {"name": "Frederick", "state": "OK", "lat": 34.362, "lon": -98.976, "priority": 2},
    
    # Midwest
    "KLOT": {"name": "Chicago", "state": "IL", "lat": 41.604, "lon": -88.084, "priority": 2},
    "KILX": {"name": "Lincoln", "state": "IL", "lat": 40.151, "lon": -89.337, "priority": 2},
    "KIND": {"name": "Indianapolis", "state": "IN", "lat": 39.708, "lon": -86.280, "priority": 2},
    "KIWX": {"name": "North Webster", "state": "IN", "lat": 41.359, "lon": -85.700, "priority": 2},
    "KMKX": {"name": "Milwaukee", "state": "WI", "lat": 42.968, "lon": -88.551, "priority": 2},
    "KGRR": {"name": "Grand Rapids", "state": "MI", "lat": 42.894, "lon": -85.545, "priority": 2},
    "KDTX": {"name": "Detroit", "state": "MI", "lat": 42.700, "lon": -83.471, "priority": 2},
    "KAPX": {"name": "Gaylord", "state": "MI", "lat": 44.907, "lon": -84.720, "priority": 2},
    "KCLE": {"name": "Cleveland", "state": "OH", "lat": 41.413, "lon": -81.860, "priority": 2},
    "KILN": {"name": "Cincinnati", "state": "OH", "lat": 39.420, "lon": -83.822, "priority": 2},
    
    # Southeast
    "KBMX": {"name": "Birmingham", "state": "AL", "lat": 33.172, "lon": -86.770, "priority": 2},
    "KEOX": {"name": "Fort Rucker", "state": "AL", "lat": 31.460, "lon": -85.459, "priority": 2},
    "KMXX": {"name": "Maxwell AFB", "state": "AL", "lat": 32.537, "lon": -85.789, "priority": 2},
    "KHTX": {"name": "Huntsville", "state": "AL", "lat": 34.931, "lon": -86.084, "priority": 2},
    "KGWX": {"name": "Columbus AFB", "state": "MS", "lat": 33.897, "lon": -88.329, "priority": 2},
    "KDGX": {"name": "Jackson", "state": "MS", "lat": 32.280, "lon": -89.984, "priority": 2},
    "KHDC": {"name": "New Orleans", "state": "LA", "lat": 30.521, "lon": -90.418, "priority": 2},
    "KTBW": {"name": "Tampa", "state": "FL", "lat": 27.705, "lon": -82.402, "priority": 2},
    "KMLB": {"name": "Melbourne", "state": "FL", "lat": 28.113, "lon": -80.654, "priority": 2},
    "KAMX": {"name": "Miami", "state": "FL", "lat": 25.611, "lon": -80.413, "priority": 2},
    "KJAX": {"name": "Jacksonville", "state": "FL", "lat": 30.485, "lon": -81.702, "priority": 2},
    "KEVX": {"name": "Eglin AFB", "state": "FL", "lat": 30.564, "lon": -85.922, "priority": 2},
    "KTLH": {"name": "Tallahassee", "state": "FL", "lat": 30.398, "lon": -84.329, "priority": 2},
    "KFCX": {"name": "Roanoke", "state": "VA", "lat": 37.024, "lon": -80.274, "priority": 2},
    "KAKQ": {"name": "Norfolk", "state": "VA", "lat": 36.984, "lon": -77.008, "priority": 2},
    "KLWX": {"name": "Sterling", "state": "VA", "lat": 38.976, "lon": -77.487, "priority": 2},
    "KMHX": {"name": "Morehead City", "state": "NC", "lat": 34.776, "lon": -76.876, "priority": 2},
    "KRAX": {"name": "Raleigh", "state": "NC", "lat": 35.665, "lon": -78.490, "priority": 2},
    "KGSP": {"name": "Greer", "state": "SC", "lat": 34.884, "lon": -82.220, "priority": 2},
    "KCAE": {"name": "Columbia", "state": "SC", "lat": 33.949, "lon": -81.118, "priority": 2},
    "KCLX": {"name": "Charleston", "state": "SC", "lat": 32.655, "lon": -81.042, "priority": 2},
    "KFFC": {"name": "Atlanta", "state": "GA", "lat": 33.363, "lon": -84.566, "priority": 2},
    "KVAX": {"name": "Moody AFB", "state": "GA", "lat": 30.890, "lon": -83.002, "priority": 2},
    "KJGX": {"name": "Robins AFB", "state": "GA", "lat": 32.675, "lon": -83.351, "priority": 2},
    "KOKX": {"name": "New York City", "state": "NY", "lat": 40.865, "lon": -72.864, "priority": 2},
    "KBGM": {"name": "Binghamton", "state": "NY", "lat": 42.200, "lon": -75.983, "priority": 2},
    "KENX": {"name": "Albany", "state": "NY", "lat": 42.586, "lon": -74.064, "priority": 2},
    "KTYX": {"name": "Montague", "state": "NY", "lat": 43.756, "lon": -75.678, "priority": 2},
    "KBOX": {"name": "Boston", "state": "MA", "lat": 41.956, "lon": -71.137, "priority": 2},
    "KDOX": {"name": "Dover AFB", "state": "DE", "lat": 38.826, "lon": -75.440, "priority": 2},
    "KDIX": {"name": "Philadelphia", "state": "NJ", "lat": 39.947, "lon": -74.411, "priority": 2},
    "KPBZ": {"name": "Pittsburgh", "state": "PA", "lat": 40.532, "lon": -80.218, "priority": 2},
    "KCCX": {"name": "State College", "state": "PA", "lat": 40.923, "lon": -78.004, "priority": 2},
    
    # Southwest
    "KFSX": {"name": "Flagstaff", "state": "AZ", "lat": 34.574, "lon": -111.198, "priority": 2},
    "KIWA": {"name": "Phoenix", "state": "AZ", "lat": 33.289, "lon": -111.670, "priority": 2},
    "KEMX": {"name": "Tucson", "state": "AZ", "lat": 31.894, "lon": -110.630, "priority": 2},
    "KYUX": {"name": "Yuma", "state": "AZ", "lat": 32.495, "lon": -114.656, "priority": 2},
    "KHDX": {"name": "Holloman AFB", "state": "NM", "lat": 33.077, "lon": -106.120, "priority": 2},
    "KABX": {"name": "Albuquerque", "state": "NM", "lat": 35.150, "lon": -106.824, "priority": 2},
    "KFDX": {"name": "Cannon AFB", "state": "NM", "lat": 34.635, "lon": -103.618, "priority": 2},
    "KMUX": {"name": "San Francisco", "state": "CA", "lat": 37.155, "lon": -121.898, "priority": 2},
    "KBBX": {"name": "Beale AFB", "state": "CA", "lat": 39.496, "lon": -121.632, "priority": 2},
    "KBHX": {"name": "Eureka", "state": "CA", "lat": 40.499, "lon": -124.292, "priority": 2},
    "KVBX": {"name": "Vandenberg AFB", "state": "CA", "lat": 34.838, "lon": -120.398, "priority": 2},
    "KNKX": {"name": "San Diego", "state": "CA", "lat": 32.919, "lon": -117.042, "priority": 2},
    "KESX": {"name": "Las Vegas", "state": "NV", "lat": 35.701, "lon": -114.891, "priority": 2},
    "KRGX": {"name": "Reno", "state": "NV", "lat": 39.754, "lon": -119.462, "priority": 2},
    "KLGX": {"name": "Langley Hill", "state": "WA", "lat": 47.116, "lon": -124.106, "priority": 2},
    "KATX": {"name": "Seattle", "state": "WA", "lat": 47.681, "lon": -122.250, "priority": 2},
    "KOTX": {"name": "Spokane", "state": "WA", "lat": 47.681, "lon": -117.626, "priority": 2},
    "KRTX": {"name": "Portland", "state": "OR", "lat": 45.715, "lon": -122.965, "priority": 2},
    "KMAX": {"name": "Medford", "state": "OR", "lat": 42.081, "lon": -122.717, "priority": 2},
}

# Level II products — raw radar moments from volume scans
LEVEL2_PRODUCTS = {
    "reflectivity":              {"label": "Reflectivity",      "short": "REF", "unit": "dBZ"},
    "velocity":                  {"label": "Velocity",          "short": "VEL", "unit": "MPH"},
    "spectrum_width":            {"label": "Spectrum Width",    "short": "SW",  "unit": "m/s"},
    "differential_reflectivity": {"label": "Diff Reflectivity", "short": "ZDR", "unit": "dB"},
    "cross_correlation_ratio":   {"label": "Correlation Coeff", "short": "CC",  "unit": ""},
    "differential_phase":        {"label": "Diff Phase",        "short": "PHI", "unit": "deg"},
    "specific_differential_phase": {"label": "Specific Diff Phase", "short": "KDP", "unit": "deg/km"},
    "storm_relative_velocity":     {"label": "Storm-Relative Velocity", "short": "SRM", "unit": "MPH"},
    "azimuthal_shear":             {"label": "Azimuthal Shear", "short": "AZS", "unit": "s^-1"},
    "divergence":                  {"label": "Divergence / Convergence", "short": "DIV", "unit": "m/s per km"},
    "cc_texture":                  {"label": "CC Texture",      "short": "CCT", "unit": ""},
    "phi_texture":                 {"label": "PHI Texture",     "short": "PHT", "unit": "deg"},
    "debris_flag":                 {"label": "Debris Signature","short": "TDS", "unit": ""},
    "composite_reflectivity":      {"label": "Composite Reflectivity", "short": "CREF", "unit": "dBZ"},
    "echo_tops":                   {"label": "Echo Tops",       "short": "ETOP", "unit": "kft"},
    "vertically_integrated_liquid": {"label": "Vertically Integrated Liquid", "short": "VIL", "unit": "kg/m^2"},
    "velocity_texture": {"label": "Velocity Texture", "short": "VXT", "unit": "MPH"},
    "hail_probability": {"label": "Hail Probability", "short": "HAIL", "unit": "%"},
    "rainfall_rate": {"label": "Rainfall Rate", "short": "RAIN", "unit": "in/hr"},
}

# Radar configuration
RADAR_CONFIG = {
    "enabled": True,
    "default_product": "reflectivity",
    "sites": NEXRAD_SITES,
    "products": LEVEL2_PRODUCTS,
}

# Regional groupings for smart polling
RADAR_REGIONS = {
    "central_plains": ["KOAX", "KUEX", "KLNX", "KDMX", "KDVN", "KARX", "KEAX", "KLSX",
                       "KSGF", "KDDC", "KGLD", "KICT", "KTWX", "KFTG", "KPUX", "KABR", "KFSD", "KUDX"],
    "tornado_alley": ["KTLX", "KINX", "KAMA", "KFWS", "KGRK", "KHGX", "KDDC", "KICT", "KGLD",
                       "KLBB", "KFDR", "KDYX", "KMAF", "KSJT"],
    "midwest": ["KLOT", "KILX", "KIND", "KMKX", "KGRR", "KDTX", "KCLE", "KILN", "KDMX"],
    "southeast": ["KBMX", "KEOX", "KMXX", "KHTX", "KGWX", "KDGX", "KHDC", "KTLH", "KFFC"],
    "northeast": ["KOKX", "KBOX", "KDIX", "KPBZ", "KBGM", "KENX", "KLWX", "KAKQ"],
    "southwest": ["KFSX", "KIWA", "KEMX", "KHDX", "KABX", "KESX", "KRGX"],
    "west_coast": ["KMUX", "KBBX", "KBHX", "KNKX", "KVBX", "KATX", "KOTX", "KRTX"],
    "conus": list(NEXRAD_SITES.keys()),
}
