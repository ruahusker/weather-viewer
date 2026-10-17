# Weather service performance rollout — October 14, 2026

The live services now use bounded rendering, shared download/cache work, and
memory-aware model admission. Forecast parameters, radar products, overlay
resolution, and rendered-history retention remain available. A 24-hour recording
is running to measure daylight satellite load and simultaneous model releases.

## Capacity and worker assignment

The host exposes **12 KVM vCPUs**, approximately **31 GiB RAM**, and **16 GiB
swap**. These are virtual CPUs; no service is pinned to particular CPU numbers.
Linux schedules their threads across the available CPUs. A systemd CPU quota
limits a service's aggregate CPU time, not its number of processes.

| Service | Before | Deployed assignment | CPU budget | Memory high / maximum |
|---|---|---|---|---|
| Models | 4 forecast-hour slots, usually split | Up to 8 forecast-hour jobs in pools of 3/3/2; independent hours first, up to 2 children with overlapping hours and 4 for a lone hour | Dynamic budget up to 10 cores, subtracting other host work and 0.5 core headroom; weight 200 | 22 / 24 GiB; separate 20 GiB working admission |
| Radar | 3 render processes; 12 download threads | 4 render processes; 12 download threads; 1 PNG writer per renderer | No hard quota; weight 100 | 10 / 12 GiB |
| Satellite | 1 process, 3 product threads | 1 process, 3 product threads, at most 2 active dataset/render contexts | No hard quota; weight 100 | 5 / 7 GiB |
| API | 4 Uvicorn workers, up to 16 default executor threads each | 2 Uvicorn workers; 4 I/O executor threads and 2 compute threads per worker; 2 compute slots shared across workers | No hard quota; weight 400 | 4 / 5 GiB |
| Surface observations | 1 process | 1 process | 0.2 cores aggregate | Existing host limits |

Scientific-library thread counts are set to 1 in all five services. The measured
model worker thread count fell from roughly 23 to 2 (main thread plus the memory
sampler); radar workers use 2 (main thread plus PNG writer). nginx's 12 workers
are inexpensive and remain unchanged. Low-priority workers remain disabled.
Two-process model rendering was enabled at 09:08 CDT on October 14, with a
four-job ceiling and additional memory reservation. At 09:41:46 CDT, adaptive
throughput scheduling superseded that ceiling. The model service now prefers
independent forecast hours, using split rendering when additional CPU capacity
would otherwise go idle. Admission uses its own cumulative host CPU counters
so other scheduler CPU checks cannot reset its sampling interval. Memory and CPU admission can reduce either limit.

The installed overrides are `/etc/systemd/system/weather-*.service.d/95-performance.conf`.
Their repository sources are `systemd/performance/*.conf`. Existing service
users, paths, unrelated overrides, and startup settings were preserved.
The model split-render settings additionally use `97-render-burst.conf`, sourced
from `systemd/performance/weather-pipeline-burst.conf`. The current allocation
is installed last as `zz-adaptive-throughput.conf` for models, radar, and
satellite; sources are `systemd/performance/weather-*-adaptive.conf`. The `zz`
prefix also overrides radar's older `workers.conf`.

## Model scheduling and memory

Pool A prefers HRRR. Pool B prefers NAM 3 km, RAP, RTMA, HREF, and RRFS. Pool C
prefers GFS, ECMWF, NAM 12 km, GDPS, and GEFS. Idle pools borrow ready work from
other families. Waiting models gain priority after two minutes; running jobs
finish at forecast-hour boundaries.

GRIB extraction runs once per forecast hour in its parent worker. Independent
hours normally render in that process. When CPU capacity exceeds ready hours
or available parent slots, overlapping hours can use up to two forked children,
and a lone hour can use up to four. Arrays are shared through copy-on-write.
Children retain the contour and wind dependencies
they need. Standalone height maps are included, matching sequential output.
Pools recycle parent workers after 16 jobs. Downloads continue ahead of
rendering. GDPS has one shared eight-thread parameter-download pool, with at
most four outstanding parameter downloads per forecast hour.

`pipeline/resource_budget.py` admits jobs using service memory accounting,
host memory, active process-family PSS, memory pressure, swap growth, and pending-job
reservations. It subtracts reclaimable inactive file pages from working memory,
keeps a 512 MiB service margin and 4 GiB host reserve, and reserves 256 MiB for
worker startup until that worker is visible. It learns a 64-sample per-model
95th-percentile incremental peak with a 20% margin, bounded below by conservative
cold-worker estimates. Measurements persist in `data/render_memory_metrics_v2.json`,
with separate histories for each render-process count. The older v1 file is
preserved; its split peaks had been misclassified when reservations were released
before recording completion metrics. The v2 seed was reconstructed from task logs.

Two-child jobs reserve at least 2.25 times the ordinary cold-job floor before dispatch, based
on the full-output HRRR benchmark below. The host reserve is 4 GiB. If only the
extra split reservation does not fit, the job can run sequentially. Known child PIDs are measured with proportional memory, counting shared pages
once. Their existing allocation is subtracted from remaining reservations,
avoiding the previous double reservation while children were running. Split
and ordinary peaks are learned separately. Three/four-child floors increase by
0.625 times the cold-job floor per extra child beyond two; measured peaks plus
20% margin can raise these estimates. Smaller splits are tried when a larger
split does not fit. Service cgroup memory and pressure
remain authoritative aggregate measurements. The 20 GiB working budget excludes
reclaimable inactive file cache; the higher 22/24 GiB service limits allow useful
file cache without throttling renderers prematurely. Split child failure or a
ten-minute result timeout triggers sequential fallback instead of blocking a
worker indefinitely. Task CPU totals include reaped rendering children.

The live rollout revealed cold RAP peaks near 2 GiB. Initial estimates were
raised before the final rollout; that staging interval is kept separately in
`data/performance/rollout-staging-20261014.jsonl`. Do not treat staging swap or
freshly restarted zero-swap counters as proof of steady-state behavior.

Task logs now include elapsed time, CPU time, peak RSS, baseline RSS, and parameter
count. `/api/status` exposes the admission budget and queue activity. Old-run
futures remain counted until they finish, and their results cannot mark a newer
run complete. Model PNG writes are atomic so an interrupted replacement cannot
leave a partial frame that is mistaken for completed work.

Known malformed Azure signer responses enter a five-minute per-model/source
cooldown. Ordinary unpublished forecast hours continue normal retries.

## Radar, satellite, API, and browser work

Radar still serves the configured 106 sites and all 11 background products.
Background parsing retains every reflectivity elevation needed by composite
reflectivity, echo tops, VIL, and other derived products. Other fields retain
their useful lowest tilts in detached compact arrays. On-demand tilts and 3D
requests retain access to the full raw volume. Tilt-list requests read elevation
metadata without decoding all field arrays.

Radar derived products share one prepared reflectivity stack. Geometry lookups
reuse static site data and exact compact indices, with a 256 MiB array-cache
budget per process (192 MiB after 09:41 CDT). Garbage collection and allocator trimming precede decisions
to discard reusable caches. The shared S3 client has a 32-connection pool and
avoids nested transfer threads.

Satellite jobs render newest frames first, with mesoscale products first in that
phase. Archive work follows with a 15-second launch budget; a frame already
running finishes before the next phase. Archive starting positions rotate.
The normal 30-second polling interval includes cycle time. Newest-first S3
listings stop after enough matching files are found, and shared raw downloads
and listings avoid repeated work across products.

HDF5 access is serialized only during dataset extraction. Selected arrays are
detached for rendering, with a 192 MiB shared array cache and independent copies
for jobs that modify inputs. RGB products reuse exact projection lookups.
Both model and satellite scalar colormaps produce byte RGBA directly, avoiding
large float RGBA intermediates.

Interactive radar computations use a bounded cross-process queue and shared
result cache. Two tilt/metadata jobs can run at once; a large 3D job acquires both
slots. Identical requests share computation, including requests arriving at
different API workers. Raw file identity and request parameters determine cache
keys. A disconnected client cannot release a still-running compute lease.
Shared slot metadata reserves queued decode memory before array allocation,
subtracting RSS growth already counted by the service. A 3D job holds both compute
slots but reserves one volume decode; two independent tilts reserve two decodes.
Result storage is capped at 128 MiB with a 30-minute expiration. Queue saturation
returns HTTP 503 with Retry-After rather than starting unbounded decodes.

Desktop and phone clients prefetch nine nearby animation frames: the current
frame, six ahead, and two behind, with loop wraparound. Preload bookkeeping is
bounded. Hidden tabs stop playback and periodic refresh work; playback resumes
only if it was running before the tab was hidden. Asset/service-worker versions
were updated so browsers receive the changes.

## Verification and measured results

**65 backend tests and 30 frontend tests pass** after the latest adaptive rollout. Python compilation, JavaScript
parsing, systemd unit verification, and whitespace checks pass. Live health,
model availability, satellite, radar, tilt metadata, and on-demand tilt endpoints
respond successfully. A repeated live on-demand tilt request took about 4 ms
after its initial 4.3-second computation. A live 3D radar scene took 4.6 seconds;
the identical request reached the other API worker and reused the shared result
in 19 ms. Its detailed check is in `data/performance/live-3d-check-20261014.json`.

Fresh-process benchmarks used identical cached inputs and compared decoded
images. The satellite benchmark uses synthetic daylight bands on a real CONUS
satellite grid because the rollout occurred overnight. These are representative
renderer comparisons, not measurements of an entire busy forecast release.

| Workload | CPU seconds before → after | CPU reduction | Peak RSS before → after |
|---|---|---|---|
| HRRR, 6 representative products | 7.527 → 5.916 | 21.4% | 865.6 → 866.1 MiB |
| Radar, 11 products | 11.207 → 8.268 | 26.2% | 1429.0 → 931.5 MiB (34.8% lower) |
| Satellite, 3 RGB products | 15.758 → 11.325 | 28.1% | 892.8 → 873.2 MiB |

All **20 distinct product images** matched pixel for pixel. Radar's 11 latest-image
aliases also matched. The HRRR sample establishes a CPU improvement; it does not
establish lower peak process memory. A live complete HRRR forecast hour took 59
seconds after the initial deployment, compared with an earlier 111.5-second
median, but those live jobs were not controlled identical workloads.

Detailed benchmark results are in `data/performance/render-benchmarks-20261014.json`.
The 30-second live resource check is in `data/performance/live-check-20261014.json`.

### Two-process model rendering

The later split-render rollout used the same pinned HRRR 12Z forecast-hour 47
input, previous precipitation hour, and snow lookbacks in separate output
directories. Each variant ran in a fresh service with a two-core CPU ceiling.
All **70 distinct PNG products matched pixel for pixel**, including six height
maps previously omitted by the Mac's split-render selection.

| Measurement | Sequential | Two render processes |
|---|---:|---:|
| Elapsed time | 59.345 s | 40.614 s |
| CPU time including children | 66.62 s | 72.61 s |
| Peak process-tree proportional memory | 2111.6 MiB | 4575.4 MiB |

This sample reduced elapsed time by 31.6%, using more CPU and memory. It does
not establish an equivalent speedup for a whole model cycle under every load.
The deployment keeps balanced pool borrowing, source cooldowns, atomic PNG
writes, admission control, and scientific-library thread limits. The model
service alone restarted; the other websites and Bitcoin Core remained active.

The source/configuration backups, benchmark inputs, decoded-image comparison,
and deployment timestamp are in `data/performance/burst-two-process-20261014/`.
The 24-hour recording spans configuration changes at 09:08 and 09:41 CDT.
Interpret each interval separately. To restore the previous allocation, remove
only the three `zz-adaptive-throughput.conf` files, reload systemd, and restart
models, radar, and satellite. This restores the earlier 97/95 settings. To also
disable split rendering, remove the model's `97-render-burst.conf`.

### Aggregate throughput and bounded memory caches

Eight identical complete HRRR forecast hours ran in isolated services with the
same eight-core ceiling, while radar, satellite, websites, and Bitcoin remained
active. The model service paused briefly with an automatic resume timer and an
exit trap. These are single batch comparisons, not a complete release benchmark.

| Variant | Elapsed batch | Task CPU | Peak cgroup memory |
|---|---:|---:|---:|
| 8 independent hours | 71.910 s | 475.72 s | 16373.7 MiB |
| 4 hours, each split into 2 children | 75.824 s | 481.23 s | 18793.9 MiB |
| 8 independent hours with numeric caches | 70.396 s | 487.06 s | 16929.4 MiB |

Independent hours completed about 5.2% sooner than always splitting, with about
2.4 GiB less peak memory. The cache batch was 2.1% quicker than the uncached
independent batch, but consumed 2.4% more task CPU in this trial (including new
proportional-memory sampling). The small cache gain is not an established
steady-state speedup. Transient swap occurred in both eight-worker batches
(systemd peak approximately 1.1 GiB before and 0.8 GiB with caches); this is why
production admission retains host headroom and swap-growth guards instead of
forcing eight active jobs regardless of host load.

All **1,680 PNG images across 24 jobs matched**. Each job produced 70 distinct
products. Existing pressure-vector and wind regression tests also pass.

The model renderer now retains up to 128 MiB of target projection and wind
rotation geometry per worker. A separate 128 MiB LRU cache reuses regridded,
smoothed height arrays between temperature, humidity, wind, and height overlays
within a forecast hour; source identities prevent mixing fields. It clears at
hour completion and before the next hour. Full raw forecast arrays are not
retained across hours. Regrid indices remain shared memory-mapped files, and
Linux's file cache retains recently read input/output pages without duplicate
Python copies. Radar and satellite keep their existing bounded geometry/array
caches. Existing scientific thread limits and worker recycling still apply.

Artifacts, source/configuration backups, tests, decoded-image comparisons, and
short rollout samples are in `data/performance/throughput-20261014/`. The new
allocation was installed at **09:41:46 CDT on October 14**. Independent CPU sampling was deployed at 2026-10-14T14:50:53.377826+00:00. Actual full-release
performance and daylight load remain subjects of the existing 24-hour recorder.

### Radar indexed PNG rollout

At **2026-10-14T15:04:51.646836+00:00**, the radar and API services restarted with an exact indexed
PNG path for background and on-demand rendering. Pixels use existing palette
entries; missing data uses an otherwise unused transparent entry, including
per-entry alpha for weak echoes. If all 256 colors are needed and no transparent
black entry exists, the image uses the existing RGBA representation. There is
no color approximation. Radar palette version remains 4 because colors and
scientific-value interpretation are unchanged. Writes remain atomic, and the
single PNG writer retains its bounded queue.

Pinned complete volumes from KOAX, KHGX, and KAMX were parsed and rendered in
fresh processes under the same two-core ceiling. Existing weather services and
websites continued running. Each volume produced all 11 configured products.

| Three-volume batch | RGBA, PNG level 1 | Indexed with RGBA fallback, level 1 |
|---|---:|---:|
| Parsing and rendering CPU | 24.725 s | 14.196 s (42.6% lower) |
| Elapsed batch | 21.991 s | 13.513 s (38.6% lower) |
| PNG bytes | 12,304,911 | 5,806,589 (52.8% lower) |
| Peak process RSS | 1461.0 MiB | 1510.9 MiB |

Thirty of 33 images used indexed PNGs; the three ZDR products required RGBA.
The sample does not establish lower whole-process peak RSS. A queued 2400x2400
indexed image holds approximately 5.5 MiB of pixels versus 22 MiB for RGBA;
parser/derived-field allocations still determine much of the process peak.

Levels 3 and 6 were also compared. Across all three compression variants,
**99 decoded PNG images matched exactly**, and metadata matched. Level 6 made
files 64.4% smaller than the original RGBA files. Repeated isolated encoding
trials showed its additional CPU cost: the 30 indexed images took roughly
0.92–0.98 CPU seconds at level 6 versus 0.42–0.46 at level 1. The deployment uses
level 1 to prioritize processing latency. Full-pipeline timings contain normal
host-load variation and cannot distinguish tiny encoder differences by themselves.

Geometry caches remain bounded at 192 MiB per radar worker. The smaller PNGs
also allow more frames to fit in the existing OS file cache and reduce browser
transfer size. Timestamped PNGs already use browser cache headers; latest
aliases require revalidation. Browser-decoded RGBA memory is a separate cost.

Five new tests cover exact colors/alpha, palette exhaustion fallback, missing
and nonfinite values, clutter filtering, and unchanged hover values. All
**58 backend tests and 30 frontend tests passed**. The live check confirms a
new indexed PNG is publicly served, its hover value is numeric, and latest
cache behavior remains correct. Artifacts, pinned scans, source backups,
compression trials, tests, and deployment/live checks are under
`data/performance/radar-indexed-20261014/`.

The existing 24-hour recording spans this additional change and service restart;
compare intervals separately. Recover this change by comparing and restoring
`before/pipeline/radar/renderer.py` from that artifact directory, then restarting
radar and API services. Preserve any subsequent edits during recovery.

The bounded systemd unit `wx-performance-observation.service` records resource
and freshness samples every minute for 24 hours, using at most 64 MiB memory
and 5% of one CPU. It records CPU usage, throttle periods, memory, swap, memory
pressure, OOM events, KOAX scan age, East IR age, and both mesoscale IR ages.
It writes `data/performance/rollout-20261014.jsonl`, then automatically produces
`data/performance/rollout-20261014.summary.json`. That summary will include
per-model CPU/time/peak-RSS distributions from the task logs. Daylight and
simultaneous-release results remain pending until the observation finishes. The final
checks found no weather-service OOM events. Some idle swap remained (about
481 MiB in models and 27 MiB in satellite); the recording will measure whether
that stays stable or grows during active work.

## Reproduction and configuration recovery

```bash
.venv/bin/python -m unittest tests.test_performance tests.test_regressions -q
node tests/frontend.test.cjs
sudo systemctl status wx-performance-observation
```

`scripts/benchmark_weather_render.py` compares current rendering with a saved
source snapshot using cached files and isolated output directories.
`scripts/sample_weather_performance.py` can record another bounded observation.

`scripts/apply_performance_services.sh` installs the reviewed overrides, saves
previous configuration, reloads systemd, and restarts the five weather services.
The original installed configuration is saved under
`data/performance/service-backup-20261014T061110Z`. The original source snapshot,
including edits already present before this task, is under
`data/performance/source-before-20261014`.

To recover the previous service allocation, restore the saved `95-performance.conf`
for each service if one exists in that backup; otherwise remove only the newly
installed `95-performance.conf`. Run `systemctl daemon-reload` and restart the
weather services. Compare source files against the saved snapshot before any
source rollback so later work is preserved.

## Shared Mac dashboard, project hold, and comparison — 2026-10-14

At 10:36 CDT, all then-running non-weather project services, associated timers,
and Docker containers were stopped at the user's request, including Bitcoin
Core, trading feeds, and OpenClaw. Unit condition guards prevent automatic
restarts; running containers now have restart policy `no`. OS, management,
nginx, and weather services remain available. No automatic resumption is
scheduled. Keep the hold until the user requests restoration.

Protected operational record:
`data/performance/vibe-project-hold-20261014/manifest.json`.
Restoration tool (run as main only when resumption is requested):
`data/performance/vibe-project-hold-20261014/restore.py`. It removes only the
recorded guards, restores original Docker policies, and starts only previously
active services/timers and containers.

Mac now polls the same 106 station definitions and uses the same exact indexed
PNG optimization. Both monitor dashboards have identical HTML, radar refresh,
Radar logs, all 106 station cards including Topeka, night status, shared
satellite-thread accounting, and the additional disk categories. Radar cards
fit desktop and phone viewports. Live Chromium checks against both hosts passed
with no browser errors; evidence and prior dashboard bytes are under
`data/performance/mac-dashboard-parity-20261014/`.

The matched-input HRRR test was repeated after the hold: Mac median 19.405 s,
Vibe 28.329 s, 70 maps and two render children. Mac took 31.5% less elapsed time.
The earlier comparison was 19.106 s versus 27.520 s. The project hold did not
yield a measurable per-hour speed gain in this repeat; normal weather work
varied between tests. This does not compare whole-release throughput or isolate
hardware from library and implementation differences. Method, raw timings,
version differences, and image-comparison limits are documented in
`docs/mac-vibe-comparison-20261014.md`.

## Adaptive bursts and shared geometry — 11:30 CDT update

The latest rollout balances splits using measured map CPU costs, shares exact
projection and rotation arrays through read-only memory maps, and prepares
repeated height grids before fork. Height data remains confined to one forecast
hour. The per-worker geometry mapping budget is 256 MiB; height grids remain
bounded at 128 MiB.

The completion reservation is retained until metrics are recorded. The old
release order had mixed split peaks into single-process histories, inflating
reservations. Correct per-process-count histories were rebuilt from task logs
into `data/render_memory_metrics_v2.json`.

The selected policy reserves 4× for an eligible lone hour and uses up to 2× for
overlapping hours, with independent hours first. A lone pinned HRRR hour took
18.7–18.9 seconds with four children versus 28.9 seconds with two. The selected
arriving-data replay took 45.655 seconds versus 48.424 for its repeated baseline;
the eight-hour backlog took 61.391 versus 63.775 seconds. Broad 4× policies were
rejected because they failed to improve the arriving-data batch.

Model working admission is 20 GiB with 22/24 GiB high/maximum limits and the
existing 4 GiB host reserve. Radar is 10/12 GiB; it accepted the raised limits
without a restart. Satellite retains 5/7 GiB. The final short live check observed
no new memory-high events, OOM kills, or swap growth.

65 backend tests and 30 frontend tests passed against deployed source.
2,730 HRRR images and 57 global-model pairs, plus contour sidecars, matched
exactly. Full measurements and recovery instructions:
`docs/adaptive-four-20261014.md`. Artifacts:
`data/performance/adaptive-four-20261014/`.
