# Weather Viewer (WX Viewer) - Project Context & Mandates

WX Viewer is a real-time weather visualization platform that downloads, processes, and displays meteorological data from multiple weather models and observational sources. It renders interactive map overlays viewable through a web browser.

## 🎯 Project Overview
- **Data Sources**: HRRR (primary), NAM 3km/12km, RAP, GFS, ECMWF IFS, GDPS, AIFS, RTMA, GOES-19 East Satellite, NEXRAD Level III Radar.
- **Tech Stack**: Python 3.12+ (FastAPI, Herbie, eccodes, matplotlib, cartopy, pyart), Vanilla JS + Leaflet.js, Nginx + Uvicorn, Systemd.
- **Live URL**: https://www.vibeprojects.us/wx/

## 🏗️ Architecture
- **Web Layer**: Vanilla JS/Leaflet frontend (`web/static/`) communicating with FastAPI.
- **API Layer**: FastAPI (`web/app.py`) serving model runs, status, and satellite/radar metadata.
- **Pipeline Layer**: `unified_scheduler.py` manages model downloads and processing.
    - **Processor**: `pipeline/processor.py` (eccodes GRIB → arrays).
    - **Renderer**: `pipeline/renderer.py` (PNG overlays).
- **Data Storage**: `data/` directory containing `grib_cache/`, `rendered/`, `satellite_cache/`, and `radar_cache/`.

## 🛠️ Development Workflows

### Running the Application
- **Environment**: Use the virtual environment in `.venv/`.
- **Pipeline**: `python -m pipeline.unified_scheduler`
- **Web Server**: `python -m uvicorn web.app:app --reload --host 127.0.0.1 --port 8000`
- **Production Services**: `weather-pipeline.service`, `weather-web.service`.

### Key Commands
- **Logs**: `tail -f logs/pipeline.log` or `journalctl -u weather-pipeline -f`.
- **Status**: `curl http://localhost/api/status`.

## 📜 Contextual Mandates & Engineering Standards

### Coding Style
- **Python**: 
    - Use type hints for function signatures.
    - Use `pathlib.Path` for all file operations.
    - Logging via `logging` module, NOT `print`.
    - Constants should be `UPPER_CASE` in config modules.
- **JavaScript**:
    - `'use strict';` at the top of files.
    - Use `const`/`let`, NOT `var`.
    - Centralized state object at the top of modules.

### Technical Integrity
- **GRIB Processing**: Use `processor.py` (direct `eccodes` usage) for performance. Avoid `cfgrib` unless explicitly required.
- **Rendering**: Native model resolution must be preserved. Use the fast path (numpy colormap + PIL) for fill params and the slow path (matplotlib) for contours (MSLP).
- **Memory Management**: `RENDER_WORKERS` is typically set to 1 due to high memory usage of `griddata`.
- **Data Retention**: Adhere to `RETENTION` policies in `pipeline/config.py` to prevent disk saturation.

### Adding New Parameters
1. Define in model file (e.g., `pipeline/models/hrrr.py`).
2. Add color scale in `pipeline/config.py`.
3. Add to `rendered_params` in `pipeline/unified_scheduler.py`.
4. Update frontend `web/static/index.html`.

## 📂 Key Directory Structure
- `pipeline/`: Core logic (scheduler, processor, renderer).
- `web/`: FastAPI application and static frontend.
- `data/`: Processed data and caches.
- `scripts/`: Maintenance and utility scripts.
- `sharppy/`: Sounding analysis library (vendored/integrated).
- `systemd/`: Service definitions.

## ⚠️ Safety & Security
- **API User**: Runs as `wx` user.
- **Secrets**: Never log or commit API keys or sensitive configurations.
- **Disk Space**: Monitor disk pressure; the pipeline triggers cleanup at <5GB free space.

## 🔍 Troubleshooting
- **No new runs**: Check disk space (`df -h`).
- **Partial parameters**: Check `PARAM_SEARCH_STRINGS` in model definitions.
- **Misaligned overlays**: Verify `bounds.json` in the run directory.
- **Rate limiting**: NOMADS (NOAA) may rate limit; the pipeline uses exponential backoff.
