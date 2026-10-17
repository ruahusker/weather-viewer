"""Admission control for forecast work under host and service memory limits."""

from collections import defaultdict, deque
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import time

import psutil

MIB = 1024 ** 2
LOGGER = logging.getLogger(__name__)
INITIAL_JOB_MB = {
    "hrrr": 2300, "nam3km": 2300, "rrfs": 3000,
    "rap": 2400, "rtma": 1500, "href": 1500,
    "gfs": 1600, "ecmwf": 1800, "nam12km": 1600,
    "gdps": 2200, "gefs": 600,
}


def host_cpu_counters() -> tuple[float, float]:
    """Return total and idle CPU time without shared cpu_percent sampling state."""
    counters = psutil.cpu_times()
    total = sum(counters) - getattr(counters, "guest", 0) - getattr(counters, "guest_nice", 0)
    idle = counters.idle + getattr(counters, "iowait", 0)
    return total, idle


def process_group_pss(pid: int, child_pids=()) -> int:
    """Count shared pages once across a known render process family.

    Child PIDs come from the worker, avoiding a host-wide process scan in
    every memory sample. RSS is a fallback on systems without proportional RSS.
    """
    total = 0
    for process_id in {pid, *child_pids}:
        try:
            process = psutil.Process(int(process_id))
            memory = process.memory_full_info()
            total += getattr(memory, "pss", memory.rss)
        except (psutil.Error, OSError, ValueError):
            continue
    return total


def render_processes_for_backlog(ready_jobs: int, free_slots: int,
                                 active_processes: int, cpu_budget: int,
                                 maximum: int = 2) -> int:
    """Cover independent forecast hours before splitting one across CPUs."""
    new_jobs = min(ready_jobs, free_slots)
    available = max(0, cpu_budget - active_processes)
    # Replays favor four children for an isolated hour. With overlapping hours,
    # retain the more CPU-efficient one/two-child policy to avoid tail delays.
    if active_processes or new_jobs > 1:
        maximum = min(maximum, 2)
    # Share spare capacity across ready hours; giving all extra cores to the
    # first hour can leave the next hour sequential and delay the whole batch.
    return max(1, min(maximum, math.ceil(available / max(1, new_jobs))))


def service_cgroup() -> Path | None:
    """Locate this service's cgroup, including systems without systemd."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                path = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
                if (path / "memory.current").exists():
                    return path
    except OSError:
        pass
    return None


@dataclass
class MemorySnapshot:
    current: int = 0
    reclaimable_file: int = 0
    limit: int | None = None
    available: int = 0
    swap: int = 0
    swap_growth: int = 0
    pressure: float = 0.0
    cpu_percent: float = 0.0
    model_cpu_cores: float = 0.0


class RenderAdmission:
    """Reserve each job's remaining peak allocation before dispatching it.

    Once a worker starts, subtract its process family's proportional memory
    from its reservation. This avoids both unaccounted queued work and counting
    the same active allocation twice.
    """

    def __init__(self, cgroup: Path | None = None, metrics_path: Path | None = None) -> None:
        self.cgroup = cgroup if cgroup is not None else service_cgroup()
        self.snapshot = MemorySnapshot()
        self.jobs: dict[tuple[str, int], int] = {}
        self.job_processes: dict[tuple[str, int], int] = {}
        self.workers: dict[tuple[str, int], dict] = {}
        self.samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=64))
        self.metrics_path = metrics_path
        if metrics_path is not None:
            try:
                for model, values in json.loads(metrics_path.read_text()).items():
                    self.samples[model].extend(float(value) for value in values[-64:]
                                              if math.isfinite(float(value)) and float(value) >= 0)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self._last_refresh = 0.0
        self._last_cpu_usage = None
        self._host_cpu_counters = host_cpu_counters()
        self.logical_cpus = psutil.cpu_count() or 1
        self.max_cpu_cores = min(self.logical_cpus, float(os.getenv("MODEL_MAX_CPU_CORES", "10")))
        self.cpu_reserve = float(os.getenv("MODEL_CPU_RESERVE_CORES", "0.5"))
        self.split_memory_factor = max(1.0, float(os.getenv("BURST_RENDER_MEMORY_FACTOR", "2.25")))
        self.working_limit = int(os.getenv("MODEL_WORKING_MEMORY_MB", "0")) * MIB
        self.margin = int(os.getenv("MODEL_MEMORY_MARGIN_MB", "512")) * MIB
        self.host_reserve = int(os.getenv("MODEL_HOST_RESERVE_MB", "2048")) * MIB
        self.startup_reserve = 256 * MIB

    def refresh(self, status_dir: Path, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_refresh < 3:
            return
        previous_swap = self.snapshot.swap
        total, idle = host_cpu_counters()
        previous_total, previous_idle = self._host_cpu_counters
        delta_total = max(0, total - previous_total)
        delta_idle = max(0, idle - previous_idle)
        host_cpu = (max(0.0, min(100.0, 100 * (1 - delta_idle / delta_total)))
                    if delta_total else self.snapshot.cpu_percent)
        self._host_cpu_counters = (total, idle)
        snapshot = MemorySnapshot(available=psutil.virtual_memory().available,
                                  cpu_percent=host_cpu)
        if self.cgroup is not None:
            try:
                snapshot.current = int((self.cgroup / "memory.current").read_text())
                stats = {line.split()[0]: int(line.split()[1])
                         for line in (self.cgroup / "memory.stat").read_text().splitlines()}
                snapshot.reclaimable_file = stats.get("inactive_file", 0)
                limits = [(self.cgroup / name).read_text().strip()
                          for name in ("memory.high", "memory.max")]
                finite = [int(limit) for limit in limits if limit != "max"]
                snapshot.limit = min(finite) if finite else None
                snapshot.swap = int((self.cgroup / "memory.swap.current").read_text())
                snapshot.swap_growth = max(0, snapshot.swap - previous_swap) if self._last_refresh else 0
                for line in (self.cgroup / "memory.pressure").read_text().splitlines():
                    if line.startswith("full "):
                        snapshot.pressure = float(line.split("avg10=", 1)[1].split()[0])
            except (OSError, ValueError):
                # Host memory remains a useful fallback on non-cgroup hosts.
                snapshot.limit = None
            try:
                cpu_stats = dict(line.split() for line in (self.cgroup / "cpu.stat").read_text().splitlines())
                cpu_usage = int(cpu_stats["usage_usec"])
                if self._last_cpu_usage is not None and self._last_refresh:
                    snapshot.model_cpu_cores = max(0, cpu_usage - self._last_cpu_usage) / 1e6 / max(.001, now-self._last_refresh)
                self._last_cpu_usage = cpu_usage
            except (OSError, ValueError, KeyError):
                pass
        if self.working_limit:
            snapshot.limit = min(snapshot.limit, self.working_limit) if snapshot.limit else self.working_limit
        self.snapshot = snapshot
        self._last_refresh = now
        workers = {}
        for path in status_dir.glob("*.json"):
            try:
                status = json.loads(path.read_text())
                key = (status["model"], int(status["fxx"]))
                if key in self.jobs:
                    if "baseline_pss_mb" in status:
                        current = process_group_pss(int(status["pid"]), status.get("child_pids", []))
                        baseline = float(status["baseline_pss_mb"]) * MIB
                    else:
                        current = psutil.Process(int(status["pid"])).memory_info().rss
                        baseline = float(status.get("baseline_rss_mb", 0)) * MIB
                    workers[key] = {"allocated": max(0, current - baseline)}
            except (OSError, ValueError, KeyError, psutil.Error):
                continue
        self.workers = workers

    def estimate(self, model: str, render_processes: int = 1) -> int:
        sample_key = model if render_processes == 1 else f"{model}:{render_processes}"
        samples = sorted(self.samples[sample_key])
        # Additional children share extracted fields, but allocate their own
        # render buffers. Preserve the established two-child floor and reserve
        # more for larger splits, learning each process count independently.
        factor = (self.split_memory_factor + max(0, render_processes - 2) * .625
                  if render_processes > 1 else 1)
        floor = INITIAL_JOB_MB.get(model, 2400) * factor
        if samples:
            p95 = samples[min(len(samples) - 1, int(len(samples) * .95))]
            # A partial resume or a warm worker must not erase the budget
            # needed by a fresh worker after periodic recycling.
            return int(max(floor, p95 * 1.2) * MIB)
        return floor * MIB

    def reserved_bytes(self) -> int:
        return sum(max(0, size - self.workers.get(key, {}).get("allocated", 0)
                       - (self.startup_reserve if key in self.workers else 0))
                   for key, size in self.jobs.items())

    def cpu_budget(self) -> int:
        other_cores = max(0, self.snapshot.cpu_percent * self.logical_cpus / 100
                          - self.snapshot.model_cpu_cores)
        return max(1, int(min(self.max_cpu_cores, self.logical_cpus-other_cores-self.cpu_reserve)))

    def reserved_processes(self) -> int:
        return sum(self.job_processes.get(key, 1) for key in self.jobs)

    def reserve(self, key: tuple[str, int], *, memory_multiplier: float = 1.0,
                render_processes: int = 1) -> bool:
        if key in self.jobs:
            return False
        snapshot = self.snapshot
        size = int(self.estimate(key[0], render_processes) * max(1.0, memory_multiplier)) + self.startup_reserve
        remaining = self.reserved_bytes() + size
        if snapshot.available - self.host_reserve < remaining:
            return False
        working = max(0, snapshot.current - snapshot.reclaimable_file)
        if snapshot.limit is not None and working + remaining + self.margin > snapshot.limit:
            return False
        if snapshot.pressure >= 3 or snapshot.swap_growth >= 16 * MIB:
            return False
        if self.reserved_processes() + render_processes > self.cpu_budget():
            return False
        self.jobs[key] = size
        self.job_processes[key] = render_processes
        return True

    def release(self, key: tuple[str, int], metrics: dict | None = None) -> None:
        self.jobs.pop(key, None)
        render_processes = self.job_processes.pop(key, 1)
        self.workers.pop(key, None)
        if metrics:
            render_processes = max(1, int(metrics.get("burst_subworkers", render_processes)))
            if "peak_group_pss_mb" in metrics:
                incremental = max(0, metrics["peak_group_pss_mb"] - metrics["baseline_pss_mb"])
            else:
                incremental = max(0, metrics["peak_rss_mb"] - metrics["baseline_rss_mb"])
            sample_key = key[0] if render_processes == 1 else f"{key[0]}:{render_processes}"
            self.samples[sample_key].append(incremental)
            if self.metrics_path is not None:
                try:
                    self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.metrics_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps({model: list(values)
                                                     for model, values in self.samples.items()}))
                    temporary.replace(self.metrics_path)
                except OSError as exc:
                    LOGGER.warning("Could not retain render memory measurements: %s", exc)

    def status(self) -> dict:
        snapshot = self.snapshot
        return {
            "memory_mb": round(snapshot.current / MIB),
            "working_memory_mb": round(max(0, snapshot.current - snapshot.reclaimable_file) / MIB),
            "memory_budget_mb": round(snapshot.limit / MIB) if snapshot.limit else None,
            "reserved_mb": round(self.reserved_bytes() / MIB),
            "swap_mb": round(snapshot.swap / MIB),
            "swap_growth_mb": round(snapshot.swap_growth / MIB),
            "memory_pressure_percent": snapshot.pressure,
            "host_cpu_percent": snapshot.cpu_percent,
            "model_cpu_cores": round(snapshot.model_cpu_cores, 2),
            "render_cpu_budget": self.cpu_budget(),
            "reserved_render_processes": self.reserved_processes(),
            "active_forecast_jobs": len(self.jobs),
        }
