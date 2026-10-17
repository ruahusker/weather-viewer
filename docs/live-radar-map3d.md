# Live 3D radar map

Added 2026-10-15. Open `/wx/radar-map-3d` (also `/radar-map-3d`).
The desktop Radar sidebar and mobile tab bar link to this page.
Vibe's nginx include `nginx/wx-site-routes.conf` forwards the exact new page
path to FastAPI; its other static `/wx/` routes remain in place.

## Coverage and measurements

- Only Omaha **KOAX** and Hastings **KUEX** are enabled. All other site IDs return 404.
- Full cached `.ar2v` Level II scans, all available reflectivity elevation sweeps.
- Coverage is a 240-km radius per radar on a 240×240×40 grid, spanning 20 km vertically.
- Internal distances are metres; every displayed vertical measurement is **feet above the radar antenna**. Default vertical exaggeration is 1×; the user can increase it.
- Real gate range coordinates, azimuths and elevations, 4/3-Earth refraction, and Mercator georeferencing are used. Missing gates, wide elevation gaps and the cone of silence are left empty. Negative reflectivity is omitted; the display threshold starts at 10 dBZ.
- This is a sampled radar reflectivity volume, including possible non-precipitation echoes. It is not a cloud boundary or an inferred tornado/hail product.
- Overlap uses the nearer enabled radar. Both sites retain separate observation times. Playback pairs scans within six minutes; unmatched sites are hidden.
- The latest 12 complete archives per site form the selectable history. Complete scans are polled every 30 seconds while the page is visible and live. Observation ages above 12 minutes show DELAYED.

## Implementation and resource bounds

- `pipeline/radar/map3d.py`: reflectivity-only Py-ART decode and sampling. One uint8 per voxel: dBZ = byte×0.5 − 32; zero is missing.
- `web/radar_map3d.py`: router and persistent cache in `data/radar_map3d/`. Override only for tests with `WX_RADAR_MAP3D_CACHE`.
- Cache keys include source name, size, nanosecond mtime and builder version. Publish binary first and metadata second using atomic renames.
- Cross-worker file lock permits **one feature decode subprocess per host**. The subprocess exits after each build to free arrays. Existing weather ingestion/render services are unchanged.
- Before a new build, require 768 MiB host availability and reserve that headroom beneath the web service's cgroup memory.high working set. A bounded shared task queue and shielded requests keep disconnected clients from duplicating active builds.
- At most 24 generated frames per site; immutable gzip-compressed binary responses. A browser retains at most six decoded frames and two active GPU volumes.
- Browser uses MapLibre GL JS 5.7.3 (vendored with license) and existing Three.js 0.161.0. WebGL 2 is required. A 4×4×4 maximum hierarchy skips empty/below-threshold blocks during volume ray marching. Device pixel ratio capped at 1.5.
- Dark basemap reuses the site's CARTO public browser key. Optional satellite imagery comes from Esri. Provider attribution is retained. NWS warning polygons use the existing `/api/alerts` feed and show feed status.

## Verification

`python -m unittest tests.test_radar_map3d -q` covers geographic axes, missing elevation/range data, feet conversion, site restrictions, concurrent-client deduplication, and disconnect behavior.

Rollout artifacts and browser checks: `data/performance/live-radar3d-20261015/`.
Full source backups for each host are retained there. The Vibe install script changes only the new files, router inclusion, and entry links, preserving its separate processing optimizations.

## Recovery

Restore the three existing entry files (`web/app.py`, `web/static/index.html`,
`web/static/mobile.html`) from this rollout's host-specific before directory and
restart only the weather web service. The new modules/assets/cache can remain
unused. Vibe backups preserve relative paths under `before/`; Mac backups are
under `local-before/` (app.py/index.html/mobile.html). No radar, model, satellite,
Bitcoin or Stratum configuration is changed by this feature.
To remove the public page route too, restore the nginx include from its backup,
run `sudo nginx -t`, then reload nginx. Mac serves the route directly in FastAPI.
