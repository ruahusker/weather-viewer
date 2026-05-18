# Weather Viewer (WX Viewer) — Agent Documentation

This file contains essential information for AI coding agents working on the weather-viewer project.

## Project Overview

WX Viewer is a real-time weather visualization platform that downloads, processes, and displays meteorological data from multiple weather models and observational sources. It renders interactive map overlays viewable through a web browser.

**Supported Data Sources:**
- **Weather Models**: HRRR (primary), NAM 3km/12km, RAP, GFS, ECMWF IFS, GDPS (Canadian), AIFS, RTMA
- **Satellite**: GOES-19 East (ABI bands, GLM lightning, derived products)
- **Radar**: NEXRAD Level III from ~140 US radar sites

**Live URL**: https://www.vibeprojects.us/wx/

## Technology Stack

| Component | Technology |
|-----------|------------|
| Backend | Python 3.12+ with FastAPI |
| Pipeline | Herbie-data for GRIB downloads, eccodes for parsing |
| Rendering | matplotlib, cartopy, PIL, numpy, scipy |
| Radar Processing | arm-pyart, siphon, metpy |
| Frontend | Vanilla JavaScript + Leaflet.js |
| Web Server | nginx (reverse proxy) + uvicorn (ASGI) |
| Process Management | systemd |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              WEB LAYER                                  │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────────────┐  │
│  │  index.html │    │   app.js    │    │       dashboard.html        │  │
│  │  (Leaflet)  │◄───│  (UI/Map)   │◄───│     (monitor/status)        │  │
│  └─────────────┘    └─────────────┘    └─────────────────────────────┘  │
│         ▲                                              ▲                │
│         │                                              │                │
│         └──────────────────────────────────────────────┘                │
│                         nginx (port 80)                                 │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                            API LAYER (FastAPI)                          │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────────────────┐ │
│  │ /api/runs      │  │ /api/available │  │ /api/value/{model}/{run}   │ │
│  │ /api/status    │  │ /api/colorscale│  │ /api/alerts (NWS)          │ │
│  └────────────────┘  └────────────────┘  └────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         PIPELINE LAYER                                  │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                unified_scheduler.py (main daemon)                │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────┐   │   │
│  │  │  HRRR    │  │  NAM     │  │  ECMWF   │  │ Other Models   │   │   │
│  │  │ downloader│  │ downloader│  │ downloader│  │ (GFS, RAP, etc)│   │   │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └───────┬────────┘   │   │
│  │       └─────────────┴─────────────┴────────────────┘              │   │
│  │                           │                                       │   │
│  │                    ┌──────▼──────┐                                │   │
│  │                    │  processor  │  (eccodes GRIB → arrays)       │   │
│  │                    └──────┬──────┘                                │   │
│  │                           │                                       │   │
│  │                    ┌──────▼──────┐                                │   │
│  │                    │  renderer   │  (PNG overlays)                │   │
│  │                    └─────────────┘                                │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  radar/scheduler.py  (NEXRAD Level III polling)                  │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                           DATA STORAGE                                  │
│  ~/weather-viewer/data/                                                 │
│  ├── grib_cache/          # Downloaded GRIB2 files (rotating retention) │
│  ├── rendered/            # Generated PNG overlays                      │
│  │   ├── hrrr/                                                           │
│  │   ├── nam3km/                                                         │
│  │   ├── satellite/                                                      │
│  │   └── radar/                                                          │
│  ├── satellite_cache/     # GOES imagery                                │
│  ├── radar_cache/         # NEXRAD Level III data                       │
│  └── logs/                # Application logs                            │
└─────────────────────────────────────────────────────────────────────────┘
```

## Directory Structure

```
weather-viewer/
├── pipeline/                    # Core processing pipeline
│   ├── unified_scheduler.py     # Main scheduler (replaces old per-model schedulers)
│   ├── config.py                # Central configuration, color scales, satellite config
│   ├── processor.py             # GRIB2 extraction using eccodes (v2, ~45x faster)
│   ├── renderer.py              # PNG overlay generation (v3, native resolution)
│   ├── cleanup.py               # Data retention and disk pressure management
│   ├── downloader.py            # Legacy download utilities
│   ├── models/                  # Per-model parameter definitions
│   │   ├── hrrr.py              # HRRR model constants
│   │   ├── nam.py, nam3km.py    # NAM variants
│   │   ├── gfs.py, ecmwf.py     # Global models
│   │   ├── rap.py, rtma.py      # Rapid refresh/analysis
│   │   ├── gdps.py              # Canadian model (special handling)
│   │   └── aifs.py              # ECMWF AIFS
│   └── radar/                   # NEXRAD Level III processing
│       ├── scheduler.py
│       ├── config.py            # ~140 radar site definitions
│       ├── downloader.py
│       └── renderer.py
├── web/                         # FastAPI web application
│   ├── app.py                   # API endpoints, NWS alerts, dashboard data
│   └── static/                  # Frontend assets
│       ├── index.html           # Main SPA (models + satellite + radar)
│       ├── dashboard.html       # Pipeline monitor dashboard (/wx/monitor)
│       ├── js/app.js            # Main application logic
│       └── css/style.css
├── nginx/weather.conf           # nginx site configuration
├── systemd/                     # systemd service definitions
│   ├── weather-pipeline.service # Pipeline scheduler daemon
│   └── weather-web.service      # FastAPI/uvicorn daemon
├── requirements.txt             # Python dependencies
└── setup.sh                     # One-shot Ubuntu setup script
```

## Key Configuration

All important configuration is in `pipeline/config.py`:

- `BASE_DIR`: `/home/main/weather-viewer`
- `DATA_DIR`: `{BASE_DIR}/data`
- `RENDERED_DIR`: `{DATA_DIR}/rendered`
- `GRIB_CACHE_DIR`: `{DATA_DIR}/grib_cache`
- `COLOR_SCALES`: 70+ parameter color map definitions
- `SATELLITE_CONFIG`: GOES-19 product definitions
- `RETENTION`: Data retention policies (6 full runs, 2 GRIB caches)
- `RENDER_WORKERS`: 1 (memory-constrained, griddata is hungry)
- `RENDER_DPI`: 200

## Running the Application

### Development

```bash
# Activate virtual environment
source .venv/bin/activate

# Run pipeline scheduler (foreground)
python -m pipeline.unified_scheduler

# Run web server (development)
python -m uvicorn web.app:app --reload --host 127.0.0.1 --port 8000
```

### Production

```bash
# Start services
sudo systemctl start weather-pipeline weather-web

# Check status
sudo systemctl status weather-pipeline weather-web

# View logs
sudo journalctl -u weather-pipeline -f
tail -f /home/main/weather-viewer/logs/pipeline.log
```

### Full Setup (New Machine)

```bash
sudo ./setup.sh
sudo systemctl start weather-pipeline weather-web
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /api/status` | Current pipeline state |
| `GET /api/runs?model=hrrr` | List available runs for model |
| `GET /api/available/{model}/{run_id}` | Available parameters per forecast hour |
| `GET /api/bounds/{model}/{run_id}` | Lat/lon bounds for overlays |
| `GET /api/colorscales` | Color scale metadata |
| `GET /api/value/{model}/{run_id}/{fxx}/{param}?lat=X&lon=Y` | Value at point |
| `GET /api/alerts` | Active NWS alerts (GeoJSON, 60s cache) |
| `GET /api/dashboard` | Full system status for monitor |
| `GET /api/logs` | Recent pipeline log entries |
| `GET /api/satellite/latest/{sat}/{product}` | Latest satellite image metadata |
| `GET /api/satellite/frames/{sat}/{product}` | All frames for animation |
| `/rendered/` | Static PNG files (served by nginx) |
| `/satellite/` | Satellite PNG files |
| `/radar/` | Radar PNG files |

## Code Style Guidelines

### Python

- Use type hints for function signatures
- Docstrings for module-level documentation
- Use `pathlib.Path` for file paths
- Logging via `logging` module, not print
- Constants are UPPER_CASE in config modules
- Prefer explicit imports over `*` imports

Example:
```python
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

def process_run(model: str, run_dt: datetime) -> list[Path]:
    """Process a model run and return generated file paths."""
    # implementation
```

### JavaScript

- `'use strict';` at top of files
- Use `const`/`let`, no `var`
- JSDoc-style comments for functions
- Event listeners use named functions for clarity
- State object centralized at top of module

## Adding a New Weather Parameter

1. **Add to model definition** (e.g., `pipeline/models/hrrr.py`):
   - `PARAM_SEARCH_STRINGS`: Herbie .idx search string
   - `GRIB_SHORTNAMES`: eccodes shortName
   - `LEVEL_TYPES`: typeOfLevel and level info

2. **Add color scale** in `pipeline/config.py`:
   ```python
   "new_param": {
       "cmap": "viridis",
       "vmin": 0,
       "vmax": 100,
       "unit": "units",
       "label": "Human Readable Label",
   },
   ```

3. **Add to model config** in `pipeline/unified_scheduler.py`:
   ```python
   rendered_params=[..., "new_param"],
   ```

4. **Add to frontend** in `web/static/index.html`:
   - Add button in appropriate param-group
   - Use `data-param="new_param"` attribute

## Testing

There is no formal test suite. Testing is manual:

1. **Pipeline test**: Check logs for successful download/render cycle
   ```bash
   tail -f logs/pipeline.log | grep -E "(Downloaded|rendered|complete)"
   ```

2. **API test**: 
   ```bash
   curl http://localhost/api/status
   curl http://localhost/api/runs?model=hrrr
   ```

3. **Frontend test**: Open browser, verify:
   - Map loads with CartoDB dark tiles
   - Model runs populate dropdown
   - Overlays display with correct colors
   - Scrubber animates forecast hours
   - Hover values show correct units

## Important Implementation Details

### GRIB Processing (processor.py)

- Uses eccodes directly (not cfgrib) for ~45x speedup
- `extract_all_params()` does single-pass extraction
- `GribField` dataclass replaces xr.DataArray
- Caches lat/lon arrays per grid shape

### Rendering (renderer.py)

- Native model resolution preserved
- Web Mercator Y-spacing for Leaflet alignment
- Fast path: numpy colormap + PIL for fill params
- Slow path: matplotlib only for MSLP contours
- USA border mask clips data outside CONUS

### Scheduler (unified_scheduler.py)

- One ProcessPoolExecutor per model (1 worker each)
- FIFO processing by run time
- Download ahead of render for throughput
- Resume capability: skips existing PNGs on restart
- Preemption: yields to higher-priority models

### Memory Management

- `RENDER_WORKERS = 1` (single render process)
- GRIB cache limited to 2 runs
- Aggressive cleanup triggers at <5GB free disk
- Process pools recreated periodically to prevent memory growth

## Security Considerations

- API runs as `wx` user (not root)
- nginx handles static files, proxies API to uvicorn
- No authentication on API (designed for public access)
- NWS alerts use 60-second cache to avoid rate limits
- File paths are sanitized via `pathlib.Path`

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| No new runs processing | Disk full | Check `df -h`, cleanup runs manually |
| Partial parameters | GRIB field not in model | Check `PARAM_SEARCH_STRINGS` |
| Misaligned overlay | Bounds mismatch | Check `bounds.json` in run directory |
| Slow rendering | Memory pressure | Reduce `RENDER_WORKERS` to 1 |
| 503 errors in logs | Rate limited by NOMADS | Automatic retry with exponential backoff |
| Pipeline not starting | Port/file lock | Check `lsof` for stale Python processes |

## Data Retention Policy

Configuration in `pipeline/config.py`:

```python
RETENTION = {
    "full_rendered": 6,      # Keep all params for 6 recent runs
    "partial_rendered": 0,   # No partial retention (delete old runs)
    "grib_cache": 2,         # Keep GRIB files for 2 runs
}
PARTIAL_PARAMS = ["temp_2m", "reflectivity", "precip"]  # Unused with partial=0
```

Cleanup runs automatically when disk pressure detected.

## Satellite Configuration

GOES-19 East is the primary source. Products defined in `SATELLITE_CONFIG`:

- **Domains**: F (Full Disk), C (CONUS), M1/M2 (Mesoscale, 1km/1min)
- **Products**: IR (Band 13), WV (Band 9), Vis (Band 2), GLM (lightning)
- **Retention**: 24 hours default, 6 hours for mesoscale
- **Update frequency**: 5-10 minutes for most products, 1 minute for mesoscale
