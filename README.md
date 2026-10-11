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

## Monitor Dashboard

The production monitor at `/wx/monitor` is included in this repository. The dashboard page lives at `web/static/dashboard.html`, with a redirect stub at `web/static/monitor/index.html`. Runtime data for the dashboard is served by FastAPI through `/api/dashboard`, `/api/logs`, and related status endpoints in `web/app.py`; nginx maps `/wx/monitor` to the dashboard HTML in `nginx/weather.conf`.

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

## Regression Checks

Run the API, downloader, map renderer, browser-state, and service-worker checks:

```bash
.venv/bin/python -m unittest discover -s tests -v
node tests/frontend.test.cjs
.venv/bin/python scripts/audit_product_catalog.py --disk --fail-on-issues
```

For browser checks, start an isolated API with `python -m uvicorn web.app:app --host 127.0.0.1 --port 8011`, then run `node tests/viewer.browser.cjs`. This requires Playwright and Chromium. `WX_TEST_URL`, `WX_PLAYWRIGHT_PATH`, and `WX_CHROMIUM` can override the API URL, Playwright package location, and browser executable. The browser suite uses delayed API fixtures to exercise tab/model races, radar tilts, empty data, and narrow phone layouts without depending on changing weather.

When serving the site under `/wx/`, include `nginx/wx-site-routes.conf` inside the existing nginx server block. It routes the landing page through FastAPI for device selection and prevents caching mutable run metadata and latest-image pointers. Validate with `nginx -t` before reloading nginx.
