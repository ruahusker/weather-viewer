# Sharp wind symbols on dark maps — October 17, 2026

Wind overlays now have pale blue-white barbs (`#edf6ff`, 0.55 pt) with a dark
keyline (`#101b29`, 0.65 pt extra width, 95% opacity). Symbol length, sampling
density, model grids, wind rotation, speed thresholds, and units are preserved.
Surface shading remains mph; surface barb components are converted to knots.

The renderer publishes a self-contained `fXX.wind.svg` alongside the existing
PNG. Speed shading is embedded at the existing grid resolution; barbs, flags,
isotachs, height contours and labels remain vector paths. Leaflet's existing
image layer displays and preloads the SVG. Desktop and phone prefer it only
when `/api/available/{model}/{run}/vectors` reports that frame. Older frames
continue to use PNGs. PNGs also remain available for point-value queries.
Both files are published atomically and retained/removed with the model run.

Wind animations no longer apply the whole-image drop-shadow stack, which
softened the thin symbols and required extra browser compositing. Other
products retain their existing presentation. Assets and the phone service
worker have new cache versions. The shared nginx route compresses only the new
wind SVGs and varies the response by `Accept-Encoding`; PNG and radar handling
is preserved. nginx configuration validation passed before reloading it.

## Validation and cost

50 backend performance/regression tests and 31 frontend checks passed. Coverage
includes surface knots, transparency outside the grid, self-contained SVG fill,
vector symbol paths, atomic sidecars, availability and PNG fallback. Live browser
checks passed for HRRR surface wind on desktop and phone, regional zoom,
canvas export, and removal of playback shadow filters, with no JavaScript errors.
The public URL serves the SVG as `image/svg+xml` with gzip content encoding.

Evidence is in `data/performance/wind-barbs-20261017/`, including screenshots,
browser results, and matched-input benchmark metrics. The warm-cache benchmark
used the same cached HRRR 19Z f00 GRIB in fresh, single-library-thread processes.
It renders two representative maps, not a complete forecast hour or release.

| Parameter | Before CPU | Final CPU | PNG dimensions before/final |
| --- | ---: | ---: | --- |
| Surface wind | 1.004 s | 2.100 s | 2520 × 1392 |
| 850 mb wind with heights | 1.799 s | 2.400 s | 2610 × 1441 |

Peak process RSS was 935.9 MiB before and 945.9 MiB after in these representative
two-map runs. The additional vector export costs roughly 0.6–1.1 CPU seconds
per sampled map and adds a sidecar to retained wind products. It avoids the
fourfold pixel count and roughly threefold PNG size measured in the rejected
2× raster experiment. The benchmark uses bounds derived from its input; live
HRRR rendering and the refresh tool preserve the existing fixed CONUS bounds.
Normal render cost/admission measurements continue to include the new export.

The latest complete HRRR run, `2026101719`, was refreshed from cached data with
one low-priority process: 19 surface-wind frames and 95 upper-air wind frames
(850/700/500/300/250 mb), 114 total. No downloads or scheduler metadata changes
were needed. The public surface-wind f00 SVG transferred 2,115,870 bytes with
gzip, compared with its 2,897,956-byte uncompressed size.
`scripts/refresh_hrrr_wind.py` provides the same targeted maintenance operation.
The API and model service were restarted to load the change. Worker counts,
resource limits, radar, satellite, monitor and other-project hold settings were
not changed by this task.

## Recovery

To select the PNG fallback for wind while keeping pressure vectors, omit wind
parameters from `get_available_vectors` in `web/app.py` and restart weather-web.
For full recovery, revert this task's renderer style/export and wind animation
CSS changes, bump asset versions, and restart weather-pipeline. Existing PNGs
remain compatible throughout; no run metadata or bounds need rebuilding.
