#!/usr/bin/env python3
"""Summarize pipeline throughput, queueing, and burst activity from logs."""

from __future__ import annotations

import argparse
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median


TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")
MAIN_QUEUE_RE = re.compile(r"\[(?P<model>[A-Z0-9]+)\] fxx=(?P<fxx>\d+).*dispatch mode: burst_subworkers=(?P<burst>\d+)")
MAIN_DONE_RE = re.compile(r"\[(?P<model>[A-Z0-9]+)\]\s+\d{10}\s+fxx=(?P<fxx>\d+)\s+rendered\s+(?P<count>\d+)\s+params")
LP_QUEUE_RE = re.compile(r"\[LP\] Queued (?P<model>[A-Z0-9]+) fxx=(?P<fxx>\d+)")
LP_DONE_RE = re.compile(r"\[LP (?P<worker>[^\]]+)\] Done (?P<model>[A-Z0-9]+) fxx=(?P<fxx>\d+) → (?P<count>\d+) outputs")
DL_SCAN_RE = re.compile(r"\[DL\] Scan pass \d+ \((?P<mode>[^,]+), submitted=(?P<submitted>\d+)\): (?P<summary>.*)")


@dataclass(frozen=True)
class JobKey:
    lane: str
    model: str
    fxx: int


def parse_ts(line: str) -> datetime | None:
    match = TS_RE.match(line)
    if not match:
        return None
    return datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S")


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((p / 100.0) * len(ordered)) - 1))
    return ordered[idx]


def fmt_seconds(value: float) -> str:
    if value < 60:
        return f"{value:.1f}s"
    return f"{value / 60:.1f}m"


def summarize(main_log: Path, lowpri_logs: list[Path]) -> str:
    queued_at: dict[JobKey, datetime] = {}
    lane_counts = Counter()
    model_done = Counter()
    burst_counts = Counter()
    burst_subworkers = Counter()
    main_latencies: list[float] = []
    lp_latencies: list[float] = []
    per_model_latency: dict[str, list[float]] = defaultdict(list)
    dl_scan_submitted: list[int] = []
    dl_scan_modes = Counter()

    def process_line(line: str, lane: str) -> None:
        ts = parse_ts(line)
        if ts is None:
            return

        if lane == "main":
            m = MAIN_QUEUE_RE.search(line)
            if m:
                key = JobKey("main", m.group("model"), int(m.group("fxx")))
                queued_at[key] = ts
                burst = int(m.group("burst"))
                burst_subworkers[burst] += 1
                if burst > 1:
                    burst_counts[m.group("model")] += 1
                return

            m = MAIN_DONE_RE.search(line)
            if m:
                model = m.group("model")
                fxx = int(m.group("fxx"))
                key = JobKey("main", model, fxx)
                lane_counts["main_done"] += 1
                model_done[model] += 1
                if key in queued_at:
                    dt = (ts - queued_at.pop(key)).total_seconds()
                    main_latencies.append(dt)
                    per_model_latency[model].append(dt)
                return

            m = DL_SCAN_RE.search(line)
            if m:
                dl_scan_modes[m.group("mode")] += 1
                dl_scan_submitted.append(int(m.group("submitted")))
                return

        if lane == "lowpri":
            m = LP_QUEUE_RE.search(line)
            if m:
                key = JobKey("lowpri", m.group("model"), int(m.group("fxx")))
                queued_at[key] = ts
                return

            m = LP_DONE_RE.search(line)
            if m:
                model = m.group("model")
                fxx = int(m.group("fxx"))
                key = JobKey("lowpri", model, fxx)
                lane_counts["lowpri_done"] += 1
                model_done[f"{model}_LP"] += 1
                if key in queued_at:
                    dt = (ts - queued_at.pop(key)).total_seconds()
                    lp_latencies.append(dt)
                    per_model_latency[f"{model}_LP"].append(dt)
                return

    for line in main_log.read_text(errors="replace").splitlines():
        process_line(line, "main")

    for log_path in lowpri_logs:
        for line in log_path.read_text(errors="replace").splitlines():
            process_line(line, "lowpri")

    lines = []
    lines.append("Throughput Summary")
    lines.append(f"Main done: {lane_counts['main_done']}")
    lines.append(f"Low-priority done: {lane_counts['lowpri_done']}")

    if main_latencies:
        lines.append(
            "Main queue->done latency: "
            f"median={fmt_seconds(median(main_latencies))} "
            f"p95={fmt_seconds(pct(main_latencies, 95))}"
        )
    if lp_latencies:
        lines.append(
            "Low-priority queue->done latency: "
            f"median={fmt_seconds(median(lp_latencies))} "
            f"p95={fmt_seconds(pct(lp_latencies, 95))}"
        )

    if burst_subworkers:
        burst_parts = [f"x{sub}={count}" for sub, count in sorted(burst_subworkers.items())]
        lines.append(f"Dispatch burst distribution: {', '.join(burst_parts)}")
    if burst_counts:
        lines.append(
            "Burst activations by model: "
            + ", ".join(f"{model}={count}" for model, count in sorted(burst_counts.items()))
        )

    if dl_scan_submitted:
        lines.append(
            "Download scans: "
            f"count={len(dl_scan_submitted)} "
            f"avg_submitted={sum(dl_scan_submitted) / len(dl_scan_submitted):.2f} "
            f"max_submitted={max(dl_scan_submitted)} "
            f"modes={dict(sorted(dl_scan_modes.items()))}"
        )

    if model_done:
        lines.append("Completed fxx by model:")
        for model, count in sorted(model_done.items()):
            suffix = ""
            latencies = per_model_latency.get(model)
            if latencies:
                suffix = f" median={fmt_seconds(median(latencies))}"
            lines.append(f"  {model}: {count}{suffix}")

    outstanding = sorted(queued_at.items(), key=lambda item: (item[0].lane, item[0].model, item[0].fxx))
    if outstanding:
        lines.append("Queued without matching completion in analyzed logs:")
        for key, ts in outstanding[:20]:
            lines.append(f"  {key.lane} {key.model} f{key.fxx:02d} queued {ts.isoformat(sep=' ')}")
        if len(outstanding) > 20:
            lines.append(f"  ... {len(outstanding) - 20} more")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-log", default="logs/pipeline.log", type=Path)
    parser.add_argument(
        "--lowpri-log",
        action="append",
        default=[],
        type=Path,
        help="Low-priority log file(s). Defaults to logs/lowpri_worker.log if present.",
    )
    args = parser.parse_args()

    lowpri_logs = list(args.lowpri_log)
    default_lowpri = Path("logs/lowpri_worker.log")
    if not lowpri_logs and default_lowpri.exists():
        lowpri_logs = [default_lowpri]

    if not args.main_log.exists():
        raise SystemExit(f"Missing main log: {args.main_log}")
    for log_path in lowpri_logs:
        if not log_path.exists():
            raise SystemExit(f"Missing low-priority log: {log_path}")

    print(summarize(args.main_log, lowpri_logs))


if __name__ == "__main__":
    main()
