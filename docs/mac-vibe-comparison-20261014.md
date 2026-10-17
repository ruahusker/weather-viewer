# Mac versus Vibe — October 14, 2026

## Result with Vibe's other projects held

| Same HRRR forecast hour, 70 maps, two render children | Mac | Vibe |
|---|---:|---:|
| Trial 1 | 19.284 s | 28.527 s |
| Trial 2 | 19.405 s | 28.329 s |
| Trial 3 | 19.575 s | 27.481 s |
| **Median elapsed** | **19.405 s** | **28.329 s** |
| Median process CPU time | 35.000 s | 49.816 s |

Mac took **31.5% less elapsed time** for this workload. Vibe took 46.0% longer.
Tests ran concurrently at 10:43–10:44 CDT. This measures forecast-hour latency;
it does not establish which host completes an entire release first with its
different production worker count.

The earlier 10:28–10:29 CDT comparison, before pausing the other Vibe projects,
had medians of 19.106 s on Mac and 27.520 s on Vibe. Pausing those projects did
not produce a measurable speed improvement in this repeat. The tests ran at
different times with normal, varying weather work, so a roughly 3% difference
between Vibe medians cannot isolate the effect of the pause.

## What this says about the remaining speed difference

The gap remains after the unrelated projects are stopped, with identical input
files, product names, and render-process count. The jobs used roughly 1.8 CPU
seconds per elapsed second on both hosts, and Vibe had over 20 GiB of available
RAM around each trial. This points to computation speed in the deployed
rendering stack rather than a shortage of memory or the paused projects being
the main cause of the per-hour gap.

Mac is an M1 Pro with ten cores and 16 GiB RAM. Vibe exposes twelve EPYC 9645
virtual CPUs under KVM with about 31.3 GiB RAM. A vCPU count alone does not
predict the speed of one rendering process. Libraries and rendering code also
differ. These measurements do not separate CPU architecture, virtualization,
library versions, and implementation effects.

The existing Vibe batch benchmark favors independent hours when work is queued:
eight independent jobs took 70.396 s with the retained caches, versus 75.824 s
for four jobs split across two children each. Those runs used eight rendering
CPUs and pinned inputs. The production scheduler therefore fills independent
hours first and splits only when CPU capacity is spare. Radar CPU savings feed
the adaptive model budget; model admission remains subject to memory and host
pressure.

## Method and limits

- HRRR run 2026101412, forecast hour 47, with previous hour 46 and snow lookbacks
  35/41. The four input files total 400,696,388 bytes; SHA-256 hashes match.
- Three repetitions, two render children, one scientific-library thread per
  process, low process priority, the same 70 output product names in every trial.
- Timing includes extraction, derivation, and PNG rendering. Initial imports,
  fixture hashing, and downloads are excluded. Inputs were already cached.
- Normal weather processing remained active, including the same 106 radar
  stations. GFS 12Z was arriving on both hosts during the repeat; background
  workload was not frozen or identical. All other then-running Vibe project
  services, project timers, and Docker containers were stopped by 10:36 CDT.
- This is an **as-deployed software and hardware comparison**, not an isolated
  hardware benchmark. Mac uses NumPy 2.5.4, SciPy 1.18.1, Matplotlib 3.11.2,
  Pillow 12.3.0, eccodes 2.49.0. Vibe uses 2.4.4, 1.17.1, 3.10.9, 12.2.0,
  and 2.47.0, respectively.
- All 70 product names and image dimensions matched in the first comparison.
  Only four decoded images were exactly identical across hosts; contour/font
  rasterization and numerical color rounding differ. This is not evidence of
  cross-host pixel equivalence. Same-host sequential-versus-split height tests
  and same-host indexed-versus-RGBA radar checks passed exact pixel comparisons.

## Operational changes completed

- Mac: retained standalone height maps in the split renderer; backfilled all six
  height levels for the 19 hours of HRRR 14Z.
- Mac: all 106 radar stations, metadata/priorities matching Vibe; exact indexed
  PNG encoding with RGBA fallback, compression level 1.
- Both dashboards: identical template, all 106 visible station cards including
  Topeka, Radar log tab, status refresh, and radar cards that fit phone screens.
  Chromium checked the actual pages and APIs at desktop and phone widths,
  matching station sets and HTML hashes, collapse, log switching, and refresh
  without browser errors.
- Vibe: other projects remain held until resumption is requested. Manifest and
  restore script are stored at
  `/home/main/weather-viewer/data/performance/vibe-project-hold-20261014/`.
  Bitcoin Core was stopped through systemd. Project restart guards and disabled
  container restart policies prevent routine automatic restarts. Weather, SSH,
  nginx, and operating-system services remain active.

## Evidence

- `compare_hrrr.py`: repeatable workload runner.
- `mac-projects-held/report.json`, `vibe-projects-held-report.json`: final trials.
- `mac/report.json`, `report.json`: initial trials.
- `image-audit.json`, `pixel-comparison.json`: first-trial dimensions and pixels.
- `../mac-height-20261014/`: height change, tests, deployment, backfill logs.
- `../mac-radar-parity-20261014/`: radar change, 42 Mac backend checks, live API,
  image encoding, hover verification, and original source files.
- `../mac-dashboard-parity-20261014/browser-check.json`: final browser results
  and associated desktop/phone screenshots. Vibe's required backend checks
  passed 42 tests and its frontend checks passed 30 tests after deployment.

No automatic project resumption was scheduled.
