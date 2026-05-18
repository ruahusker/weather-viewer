# WX Viewer

WX Viewer is a real-time weather visualization site for model guidance, GOES satellite imagery, NEXRAD radar, surface observations, and NWS alerts. It renders PNG overlays from meteorological data and serves them through a FastAPI backend and Leaflet frontend.

## Main Components

- `pipeline/` - download, process, and render weather model, satellite, radar, and surface data.
- `web/` - FastAPI API plus the static Leaflet viewer.
- `nginx/` - production reverse proxy configuration.
- `systemd/` - service definitions for the web app and data pipelines.
- `scripts/` - operational and analysis utilities.
- `sharppy/` - vendored SHARPpy components used by the site.

Generated data is intentionally not tracked. The production instance writes large GRIB, satellite, radar, log, cache, and rendered image files under `data/`, `logs/`, and related runtime paths.

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn web.app:app --reload --host 127.0.0.1 --port 8000
```

Run the unified pipeline in a separate shell when local data generation is needed:

```bash
source .venv/bin/activate
python -m pipeline.unified_scheduler
```

## Production Services

The production deployment uses nginx, uvicorn, and systemd:

```bash
sudo systemctl start weather-web
sudo systemctl start weather-pipeline
sudo systemctl start weather-satellite
sudo systemctl start weather-radar
```

Runtime status and data availability are exposed through the FastAPI endpoints in `web/app.py`.
