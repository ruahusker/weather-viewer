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
│       ├── mobile.html          # Phone SPA (served when UA is a phone, or ?view=mobile / wx_view cookie)
│       ├── dashboard.html       # Pipeline monitor dashboard (/wx/monitor)
│       ├── js/app.js            # Main application logic (desktop)
│       ├── js/mobile.js         # Phone application logic
│       ├── css/style.css        # Single stylesheet (theme-refresh.css was folded into its end)
│       └── vendor/              # Self-hosted third-party assets (leaflet, html2canvas, three)
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
- Model render capacity: eight forecast-hour slots (pools of 3/3/2), independent hours first; up to two render children for overlapping hours and four for a lone hour when resources permit
- Radar render capacity: four processes; satellite: three product threads, two active render contexts
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

Automated resource and regression checks:

```bash
.venv/bin/python -m unittest tests.test_performance tests.test_regressions -q
node tests/frontend.test.cjs
```

Also verify live behavior manually:

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

- Three ProcessPoolExecutors with 3/3/2 parent workers; family preferences and idle-slot borrowing
- Independent forecast hours have priority; split CPU capacity is shared across ready hours, with four children reserved for a lone hour and up to two for overlapping hours
- FIFO processing by run time
- Download ahead of render for throughput
- Resume capability: skips existing PNGs on restart
- Dispatch at forecast-hour boundaries; waiting models gain priority after two minutes
- Cgroup memory, host memory, CPU, pressure, and swap growth govern dispatch

### Memory Management

- Up to eight model jobs and ten rendering CPUs; dynamic CPU admission subtracts other host work; 20 GiB working-memory admission, 22/24 GiB service high/maximum, and a 4 GiB host reserve
- Scientific-library threads capped at one per worker
- Per-model proportional-memory peaks persist in data/render_memory_metrics_v2.json, keyed by render-process count; keep the reservation until completion metrics are recorded
- Model projection/rotation arrays are read-only memory maps shared across workers, with a 256 MiB mapping budget per process; smoothed height grids: 128 MiB within a forecast hour, shared with forked children and cleared at hour completion; radar lookup cache: 192 MiB per process; shared satellite array cache: 192 MiB
- Splits use measured per-map CPU costs from data/render_costs.json; cold profiles use render-type estimates. Radar memory high/maximum is 10/12 GiB to reduce reclaim while host memory is available.
- Radar PNGs use exact indexed colors and per-entry alpha when a transparent palette entry is available; full-color RGBA fallback preserves frames needing all 256 colors plus missing data; PNG compression level 1
- API: two workers with two compute slots shared across them; 3D jobs consume both slots
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
| Slow rendering | Memory pressure | Check `/api/status` resources and service cgroup limits; tune admission estimates |
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

See [docs/performance.md](docs/performance.md) for current worker assignments,
CPU and memory budgets, benchmark results, observation files, and configuration recovery.

## Satellite Configuration

GOES-19 East is the primary source. Products defined in `SATELLITE_CONFIG`:

- **Domains**: F (Full Disk), C (CONUS), M1/M2 (Mesoscale, 1km/1min)
- **Products**: IR (Band 13), WV (Band 9), Vis (Band 2), GLM (lightning)
- **Retention**: 24 hours default, 6 hours for mesoscale
- **Update frequency**: 5-10 minutes for most products, 1 minute for mesoscale

## Project holds and shared monitor (2026-10-14)

The user requested all other Vibe projects be put on hold for weather processing.
Bitcoin Core/dashboard, Nexus, Options Desk, Poly, IB Gateway/quote feeds,
OpenClaw and its project timers, and all three running Docker workloads are
stopped. Keep them held until the user requests resumption. Persistent unit
guards and container restart policies enforce the hold. The protected manifest,
verification, and restore tool are in
`data/performance/vibe-project-hold-20261014/`; do not run the restore tool as
part of routine weather maintenance. Weather, management, and OS services stay
available. Partial releases (recorded under `partial_releases` in the manifest):
`bitcoind.service` and the `public-pool` container (2026-10-14), and the Nexus
terminal, `nexus-terminal.service` (2026-10-18, user request; only its guard was
removed). Everything else remains held.

Both hosts use identical `web/static/dashboard.html` at `/wx/monitor`. It includes
all 106 radar cards (including KTWX), radar refresh and logs, night status,
shared satellite-thread accounting, disk categories, and responsive radar cards.
Use root `/api/...` paths, which also work on the Mac without nginx prefix
rewriting. Browser evidence is in
`data/performance/mac-dashboard-parity-20261014/`.

The 10:43 CDT repeated matched-input test had medians of 19.405 s on Mac and
28.329 s on Vibe for one HRRR hour, 70 maps, two render children. This is
per-hour latency with normal weather work active, not whole-release throughput
or an isolated hardware comparison. See `docs/mac-vibe-comparison-20261014.md`.

## Adaptive burst and shared geometry update (11:30 CDT)

The verified policy uses four children for an eligible lone forecast hour and
up to two when hours overlap, retaining independent-hour priority. Shared
read-only geometry and per-hour height preparation preserve exact output.
The completion bookkeeping bug that mixed split and ordinary memory peaks is
fixed; v2 histories were reconstructed from actual task metrics. Detailed
benchmarks, deployed settings, checks, and rollback instructions are in
`docs/adaptive-four-20261014.md`. Keep the other projects held.

## v4 interface (2026-10-17)

The viewer, phone page and `/wx/monitor` default to the v4 look; `?ui=classic`
switches back (remembered in localStorage `wx_ui`), `?ui=v4` returns. An inline
script in each page's `<head>` sets `<html data-ui="v4">`, and every v4 rule is
scoped to that attribute, so the classic styles stay intact underneath.

- `css/theme-v4.css` + `js/ui-v4.js` (desktop): floating bar/panels/time dock,
  model chips driving `#modelSelect`, run card over `#runSelect`, point readout
  (fed by app.js's `wx:point-query` event and `/api/meteogram`), radar alerts panel
- `css/mobile-v4.css` + `js/mobile-ui-v4.js` (phone): source pills, bottom dock
- `css/monitor-v4.css` (monitor): `dashboard.html` must stay identical on the Mac host
- Fonts: self-hosted Geist / Geist Mono in `static/fonts/` (SIL OFL)
- Reference screenshots of every main page: `docs/ui-v4-thumbnails.png`
