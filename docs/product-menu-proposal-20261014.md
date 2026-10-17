# Proposed product cleanup — October 14, 2026

This is a proposal for a site used for everyday forecasts, storm watching and
chasing, and winter weather. The current catalog audit finds 101 distinct model
product identifiers, 30 satellite products, and 19 radar products. There are no
frontend/configuration catalog mismatches. Those counts include model-specific
aliases and specialist diagnostics; they are not the number rendered by every
model.

## Main menu

Use three plain-language collections, plus Advanced and Favorites. Aim for
roughly 20–25 main choices across the collections, with controls for interval,
parcel type, and altitude instead of a separate button for every variant.

| Collection | Main choices |
|---|---|
| Everyday | Temperature; feels-like temperature; dewpoint; wind and gusts; cloud cover; radar; precipitation type; rainfall totals; warnings |
| Storms | Radar reflectivity; velocity/storm-relative velocity; correlation coefficient; mixed-layer CAPE and CIN; low-level helicity; deep-layer shear; rotating-updraft swaths; soundings; live lightning; storm reports |
| Winter | Rain/snow/sleet/freezing-rain forecast; snowfall totals with an interval selector; snowfall probabilities; snow depth; wind chill; visibility |

Some choices belong in multiple collections but should share one product and
cached output. Keep actual observations and forecast output clearly labeled.

## Retire or replace

- Retire the standalone custom model scores: Chase Target Index (`cti`),
  Mesocyclone Strength (`msi`), Storm Mode Index (`smi`), CI Timing Index (`citi`),
  Chase Visibility (`cvi`), Low-Level Moisture Transport score (`llmt`), Forcing
  for Ascent score (`ascent`), and Storm Obscuration/HP Risk (`hp_risk`). Their
  implementations combine existing fields with local thresholds and weights.
  Preserve underlying CAPE, inhibition, shear, helicity, visibility, moisture,
  and vertical-motion fields wherever another product or sounding uses them.
- Replace the current radar Hail Probability display with an established MRMS
  hail-size estimate (MESH). The existing radar code builds its percentage from
  fixed reflectivity, ZDR, and KDP contributions; its label suggests more than
  that simple score establishes.
- Remove satellite sea-surface temperature from the normal inland-weather
  collection. Keep it available as an optional specialist product if ocean or
  tropical-weather use matters.
- Replace the standalone Day Cloud Phase menu choice with a day/night cloud
  product that uses Day Cloud Phase in daylight and Nighttime Microphysics at
  night. Keep Natural Color, IR/VIS Sandwich, infrared, water vapor, and the
  repositionable mesoscale views as readily accessible satellite choices.

## Advanced and optional rendering

- Radar spectrum width, raw differential phase, CC texture, PHI texture,
  velocity texture, and divergence/convergence.
- Raw satellite ozone and CO2 bands, blue visible, cirrus, particle-size bands,
  and individual longwave IR variants. Band data can remain inputs for useful
  composites without generating a separate display stream for each band.
- Lifted Index, effective helicity, extra CAPE/CIN parcel choices, alternate UH
  layers, boundary-layer height, and snow water equivalent. Keep these
  accessible to interested users.
- Extra upper-air maps and ensemble height spread/anomaly. Keep 850 mb flow,
  500 mb flow/heights, and one jet-level wind view accessible for forecasting;
  reserve other altitude-specific maps for Advanced.
- Use one CAPE/CIN control with parcel choices and normalized labels across
  models. Mixed-layer, surface-based, and most-unstable parcels have different
  meteorological meanings and should retain their definitions.
- Use one snowfall control with accumulation intervals. Put the 10:1
  comparison in Advanced; show Kuchera totals and existing GEFS snowfall
  probabilities prominently.

Moving a choice into Advanced only changes navigation. To reduce server work,
unused specialist maps must also stop automatic rendering, or be generated
through a bounded, cached on-demand path. Do not drop their raw input fields
without checking dependencies for retained maps and soundings.

## Additions, in suggested order

| Priority | Product | Value | Expected implementation cost |
|---|---|---|---|
| 1 | Feels-like temperature | Everyday heat index and winter wind chill, with valid-condition handling | Low: temperature, wind, and moisture already available; palette entries exist |
| 1 | Rain/snow/sleet/freezing-rain map | Makes winter precipitation changes easy to follow | Low to moderate: add model categorical fields and combine with precipitation intensity; preserve mixed categories |
| 1 | Observed storm reports | Reported hail sizes, damaging winds, tornadoes, rainfall, and winter amounts | Low relative to full-grid rendering: cached text/point feed |
| 2 | GOES total lightning overlay | Actual lightning activity over radar or satellite imagery | New ingestion and caching; use a bounded recent window |
| 2 | Day/night clouds and fog | Useful satellite imagery through sunset and overnight | Moderate: extend the current RGB path and reuse satellite bands/grid caches |
| 3 | MRMS observed rain totals and hail estimates | Regional/national rain accumulation and estimated hail size | Moderate: a new gridded source; start with a small product set and one bounded processing worker |

A side-by-side model comparison at the same valid time is also worthwhile.
It can reuse existing output images and gives a practical view of disagreement
without creating another full family of derived maps.

## Formula review before promotion

SHIP and DCP are recognized meteorological parameters. The current site code
uses simplified formulas under those names: its SHIP lacks the standard parcel
mixing-ratio term and actual height-based lapse-rate calculation; its DCP uses
temperature difference and gust proxies instead of DCAPE and mean wind. STP
and SCP also warrant comparison against their published definitions. Correct
or clearly qualify these products before presenting them as primary hazard
indicators.

Relevant primary references:

- [NWS composite-parameter training, including SHIP](https://training.weather.gov/wdtd/courses/rac/documentation/rac21-Composite-Parameters.pdf)
- [NWS DCP definition](https://training.weather.gov/wdtd/courses/rac/documentation/rac21-intro-to-meso-params.pdf)
- [NOAA HRRR/RAP precipitation-type diagnostics](https://rapidrefresh.noaa.gov/RAP_var_diagnosis.html)
- [NWS wind-chill definition](https://www.weather.gov/safety/cold-wind-chill-chart)
- [NWS heat-index calculator](https://www.weather.gov/epz/wxcalc_heatindex)
- [NOAA GLM products](https://vlab.noaa.gov/web/geostationary-lightning-mapper/products)
- [NOAA day/night cloud combination guide](https://www.star.nesdis.noaa.gov/GOES/documents/ABIQuickGuide_DayNightCloudMicroCombo.pdf)
- [NSSL MRMS product table](https://www.nssl.noaa.gov/projects/mrms/operational/tables.php)
- [NWS preliminary storm reports](https://www.weather.gov/bgm/helpLocalStormReports)

## Performance interpretation

The current learned HRRR per-map profile records approximately 58.8 CPU seconds
across 71 entries. The eight custom scores total approximately 1.49 seconds,
or 2.5% of that profile. Removing them mainly improves clarity. Upper-air wind
maps are among the costlier entries, approximately 2.6–3.1 CPU seconds each.
These are running per-map estimates, not a controlled full-run benchmark;
they exclude GRIB processing and downloads and do not directly predict wall
time under parallel rendering.

Favor the inexpensive additions first, then measure the new grid/RGB products
under active forecast, radar, and satellite load. Keep the existing resource
admission and all other project holds in place.
