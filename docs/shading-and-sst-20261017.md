# Shading-only diagnostics and restored SST — October 17, 2026

PBL height and 33 additional shaded model products no longer declare scalar
isolines in `pipeline/config.py`. This includes surface/mixed-layer/most-unstable
CAPE and CIN (including their model aliases), lifted index, severe-weather
indices, helicity, shear, VIL, precipitation/snow shading and visibility.
These products now use the existing native-resolution PIL fill renderer rather
than adding Matplotlib contour lines. Units, colormaps and thresholds are
unchanged. Dedicated pressure/height products and upper-air analysis contours
retain their existing behavior.

The model service was restarted and client/PWA cache versions were updated.
Current cached same-hour diagnostic fields were refreshed where source GRIBs
were available, using low-priority jobs. Some older archived maps retain their
original rendering; new forecast runs use shading only.
Archived precipitation/snow accumulations were not recomputed without their
required lookbacks. Maintenance evidence is in
`data/performance/pbl-no-isolines-20261017.json`,
`data/performance/shading-only-params-20261017.json` and
`data/performance/shading-refresh-20261017-rap-gfs.json`.

SST was failing before rendering because the shared xarray cache reinserted
GOES auxiliary band coordinates as data variables. The cache now stores
detached `Variable` objects and preserves each variable's coordinate/data role.
L2 jobs request only their primary field and DQF plus common projection metadata,
reducing unnecessary reads and memory. Mutations remain isolated between jobs.
SST keeps its quality filtering and Kelvin-to-Celsius conversion. Missing/cloudy
retrievals are no longer diffused across large gaps by the color-extension pass.

After restarting the satellite service, both GOES-19 and GOES-18 SST resumed
publishing current hourly imagery and animation frames. Live endpoints and the
desktop/phone SST views were verified. PBL, MUCAPE and MLCIN were also inspected
in the live browser. Evidence and screenshots are in
`data/performance/sst-fix-20261017/`.

52 backend checks and 31 frontend checks passed. New regression coverage verifies
auxiliary coordinate roles across cache reads, mutation isolation, SST quality
gaps and Celsius conversion. Python compilation and whitespace checks passed.
