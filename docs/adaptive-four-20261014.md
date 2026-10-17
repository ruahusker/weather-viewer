# Vibe processing improvements — October 14, 2026

Deployed at **11:30:51 CDT**. Mac's processing settings were preserved.

## Deployed changes

- Eight forecast-hour slots retain a dynamic model CPU budget of up to ten
  cores. Independent hours have priority. Overlapping hours use up to two render
  children; an eligible lone hour can use four when CPU and memory permit.
- Spare split capacity is distributed across ready hours. A broad 4× policy
  was rejected after its arriving-data replay failed to improve completion time.
- Splits balance measured CPU cost per map. Persistent workers share an atomic,
  locked profile at `data/render_costs.json`, with initial estimates for cold
  model/product combinations.
- Projection coordinates and wind rotation arrays are exact read-only NumPy
  memory maps, shared through the OS file cache. The per-worker mapping budget is
  256 MiB. Keys include geometry and NumPy version. Cache failures fall back to
  computing the arrays.
- Height grids used by multiple children are prepared once before fork, with
  a 128 MiB bound, and released at hour completion. They are never reused for a
  different forecast hour.
- Fixed completion bookkeeping: reservations now retain their process count
  until memory metrics are recorded. Previously, split peaks were incorrectly
  learned as single-process peaks, inflating independent-job reservations.
  Correct histories were reconstructed from task logs into
  `data/render_memory_metrics_v2.json`; the old file was preserved.
- Memory reservations increase with burst size and learn separate peaks for
  each process count. A smaller split is tried if the requested split cannot fit.
- Model working admission increased from 18 to **20 GiB**, with service
  high/maximum limits of **22/24 GiB**. The host reserve remains 4 GiB.
- Radar high/maximum limits increased from 8/10 to **10/12 GiB**. The active
  cgroup accepted the change during daemon reload; radar was not restarted.
  Satellite remains at 5/7 GiB because it had no new high-limit events in the
  final live observation.

## Controlled replay results

All HRRR cases used the same four pinned GRIB files, the same 70 distinct maps,
an eight-core CPU ceiling, and one scientific-library thread per process.
Normal radar, satellite, and web services stayed active. Model dispatch was
drained and paused during each replay phase, with recovery timers, and resumed
after every phase. Initial process imports and input hashing are outside the
reported elapsed interval.

| Workload | Baseline | Selected policy | Change |
|---|---:|---:|---:|
| Eight hours ready together | 63.775 s | 61.391 s | 3.7% less elapsed time |
| Four hours arriving in pairs at 0/15 seconds | 48.424 s | 45.655 s | 5.7% less elapsed time |
| Lone hour, candidate 2 children versus 4 | 28.927 s | 18.727–18.908 s | About 35% less elapsed time |

The earlier arriving-data baseline was 50.474 seconds; the repeated baseline
above is used for the comparison. These are representative replays, not a
guarantee of identical gains across model types or whole operational releases.
They exclude waiting for upstream publication and downloads.

For the eight-hour batch, task CPU fell from 458.28 to 448.78 seconds.
For the final arriving-data replay, it fell from 231.37 to 216.98 seconds,
about **6.2% less CPU time**. Peak replay-cgroup memory was 17,299 → 16,417 MiB
for the backlog and 14,632 → 13,051 MiB for arriving data.

Four children traded more resources for lower lone-hour latency: 56.69–57.43
CPU seconds versus 52.83, and roughly 5.2–5.3 GiB peak replay-cgroup memory
versus 4.4 GiB. This supports keeping 4× for isolated work.

All replay units completed with **zero swap and zero OOM kills**. File-backed
cache pages can be charged outside a later replay cgroup, so cgroup memory
figures do not measure total host memory savings.

### Policies tested and rejected

The first broad 4× allocator gave three cores to one ready hour and one core to
another. That replay took 68.051 seconds. Sharing spare cores fairly reduced it
to 50.424 seconds, which still did not improve the repeated baseline. The
deployed policy limits overlapping work to one/two rendering processes and
keeps four for a lone hour; its arriving-data replay took 45.655 seconds.

## Correctness and live verification

- **65 backend tests and 30 frontend tests passed**, both in staging and against
  deployed source. Production-source backend tests used an isolated data root.
- **2,730 HRRR output images** across all replay cases matched the saved
  production source pixel for pixel. Contour vector sidecars also matched.
- Pinned GFS and GDPS checks compared the old two-child path with the candidate
  four-child path: **35 GFS maps and 22 GDPS maps** matched exactly, including
  their pressure-vector sidecars.
- During a 146-second live observation, models reached **seven concurrent
  forecast jobs** and about **7.2 cores**. At least 19,914 MiB remained available.
  All four observed weather services had no new OOM kills, memory-high events,
  or swap growth.
- All 106 radar station records remained present; 56 stations advanced their
  reflectivity timestamps. Median reported reflectivity age moved from 10.5
  to 10.1 minutes. This short sample does not establish every station's cadence.
- Model restart resets its service counters; this live interval is a startup
  check. The existing 24-hour recorder continues across the deployment.
- Other Vibe projects, including Bitcoin Core and Docker workloads, remain held.
  Both monitor dashboards retain their matching template and 106 station cards.

## Evidence and recovery

Local source snapshots and this report:
`/Users/steve/weather-viewer/data/performance/vibe-adaptive4-20261014/`.

Vibe artifacts:
`/home/main/weather-viewer/data/performance/adaptive-four-20261014/`.

Key files:

- `benchmarks/*/benchmark.json`: timings, input hashes, dispatch decisions,
  per-job metrics, memory events, and actual product names.
- `image-parity.json`, `regular-image-parity.json`: exact output comparisons.
- `production-tests.log`, `production-frontend.log`: deployed-source checks.
- `deployment.json`: installed source hashes, limits, PID, and timestamp.
- `live-observation.jsonl`, `live-summary.json`: short live validation.
- `installed-before/`: original source and installed model/radar overrides.
- `before/`, `candidate/`: full relevant source snapshots.

For rollback, drain active model jobs, restore the three existing pipeline
files and the model/radar adaptive configuration files from `installed-before`,
restore its two installed overrides, reload systemd, and restart the model
service. Restore radar's active memory limits to 8/10 GiB if needed. The new
modules and v2 data can remain unused. Keep all other project holds in place.
No broad checkout or repository reset is needed.
