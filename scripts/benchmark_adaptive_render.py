"""Replay pinned hours as a backlog or timed arrivals at a fixed render budget."""
import argparse
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import hashlib
import json
import logging
import multiprocessing as mp
import os
from pathlib import Path
import resource
import sys
import time


def initialize(root, output):
    global scheduler
    sys.path.insert(0, root)
    from pipeline import unified_scheduler as scheduler
    logging.basicConfig(level=logging.WARNING)
    scheduler.MODEL_TASK_METRICS = True
    scheduler._init_render_worker()
    scheduler.WORKER_STATUS_DIR = Path(output)/"status"
    scheduler.WORKER_STATUS_DIR.mkdir(exist_ok=True)


def warm():
    time.sleep(.2)
    return os.getpid()


def render_job(fixture, output, index, processes):
    fixture, target = Path(fixture), Path(output)/f"job{index:02d}"
    target.mkdir()
    params, extras = scheduler._compute_render_params(scheduler.MODEL_MAP["hrrr"])
    result = scheduler._render_model_batch(
        [{"fxx": 47, "grib_path_str": str(fixture/"f47.grib2"),
          "prev_grib_str": str(fixture/"f46.grib2"),
          "lookback_strs": {"35": str(fixture/"f35.grib2"), "41": str(fixture/"f41.grib2")}}],
        str(target), datetime(2026,10,14,12).isoformat(), params, extras, "hrrr",
        "benchmark", processes, "matched_replay",
    )[0]
    actual = sorted(p.parent.name for p in target.glob("*/f47.png"))
    if len(actual) != 70:
        raise RuntimeError(f"Expected 70 maps, got {len(actual)}")
    return {"index": index, "processes": processes, "products": actual, "metrics": result[2]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("backlog", "stream", "single"), required=True)
    parser.add_argument("--max-burst", type=int, default=4)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--cpu-budget", type=int, default=8)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root))
    from pipeline.resource_budget import render_processes_for_backlog, service_cgroup
    args.output.mkdir(parents=True)
    fixture = str(args.fixture.resolve())
    output = str(args.output.resolve())
    arrivals = ([0]*args.jobs if args.mode != "stream" else [15*(i//2) for i in range(args.jobs)])
    workers = min(args.jobs, 8)
    cgroup = service_cgroup()
    before_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    initialized = time.monotonic()
    with ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"),
                             initializer=initialize, initargs=(str(args.root), output)) as pool:
        warmed = [pool.submit(warm) for _ in range(workers)]
        for future in warmed:
            future.result()
        started = time.monotonic()
        pending, active, results = list(range(args.jobs)), {}, []
        dispatch = []
        while pending or active:
            elapsed = time.monotonic()-started
            ready = [i for i in pending if arrivals[i] <= elapsed]
            free_slots = workers-len(active)
            for index in ready:
                if free_slots <= 0:
                    break
                used = sum(processes for processes, _ in active.values())
                if used >= args.cpu_budget:
                    break
                processes = (args.max_burst if args.mode == "single" else
                             render_processes_for_backlog(len(ready), free_slots, used,
                                                          args.cpu_budget, args.max_burst))
                processes = min(processes, args.cpu_budget-used)
                future = pool.submit(render_job, fixture, output, index, processes)
                now = time.monotonic()-started
                active[future] = (processes, now)
                dispatch.append({"index": index, "processes": processes, "at_s": round(now,3)})
                pending.remove(index)
                free_slots -= 1
                ready = [i for i in ready if i != index]
            if active:
                done, _ = wait(active, timeout=.2, return_when=FIRST_COMPLETED)
                for future in done:
                    processes, submitted = active.pop(future)
                    result = future.result()
                    result["completed_at_s"] = round(time.monotonic()-started,3)
                    result["latency_s"] = round(time.monotonic()-started-arrivals[result["index"]],3)
                    results.append(result)
            else:
                time.sleep(.1)
        elapsed = time.monotonic()-started
    after_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
    report = {"mode": args.mode, "max_burst": args.max_burst, "cpu_budget": args.cpu_budget,
              "jobs": args.jobs, "elapsed_s": round(elapsed,3),
              "initialization_s": round(started-initialized,3),
              "cpu_s": round(sum(r["metrics"]["cpu_seconds"] for r in results),3),
              "cpu_with_initialization_s": round(after_cpu.ru_utime+after_cpu.ru_stime-before_cpu.ru_utime-before_cpu.ru_stime,3),
              "dispatch": dispatch, "results": sorted(results,key=lambda r:r["index"]),
              "completed_at": datetime.now(timezone.utc).isoformat(),
              "input_sha256": {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in args.fixture.glob("f*.grib2")}}
    if cgroup:
        report["peak_mb"] = round(int((cgroup/"memory.peak").read_text())/1024**2,1)
        report["swap_mb"] = round(int((cgroup/"memory.swap.current").read_text())/1024**2,1)
        report["memory_events"] = (cgroup/"memory.events").read_text()
    (args.output/"benchmark.json").write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in ("results","input_sha256")}),flush=True)


if __name__ == "__main__":
    main()
