"""Record weather service resource deltas and published-frame ages as JSONL."""

import argparse
import ast
from collections import defaultdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import statistics
import time

LOGGER = logging.getLogger(__name__)
SERVICES = ("weather-pipeline", "weather-radar", "weather-satellite", "weather-web")


def summarize(path: Path) -> dict:
    """Write a compact result after the observation period finishes."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    metrics = defaultdict(lambda: defaultdict(list))
    ages = defaultdict(list)
    for row in rows:
        for service, data in row["services"].items():
            for key in ("cpu_cores", "memory_mb", "swap_mb", "throttled_periods_delta",
                        "memory_high_events_delta", "oom_kills_delta"):
                if key in data:
                    metrics[service][key].append(data[key])
        for name, value in row.get("scan_age_minutes", {}).items():
            ages[name].append(value)
    def distribution(values: list) -> dict:
        values = sorted(values)
        return {"samples": len(values), "mean": round(statistics.mean(values), 2),
                "median": round(statistics.median(values), 2),
                "p95": round(values[min(len(values) - 1, int(len(values) * .95))], 2),
                "max": round(max(values), 2)}
    summary = {"started": rows[0]["timestamp"], "finished": rows[-1]["timestamp"],
               "samples": len(rows), "services": {}, "scan_age_minutes": {}}
    for service, fields in metrics.items():
        summary["services"][service] = {
            key: (sum(values) if key.endswith("_delta") else distribution(values))
            for key, values in fields.items()
        }
    summary["scan_age_minutes"] = {name: distribution(values) for name, values in ages.items()}
    root = Path(__file__).resolve().parents[1]
    task_metrics = defaultdict(lambda: defaultdict(list))
    cutoff = datetime.fromisoformat(rows[0]["timestamp"]).astimezone().replace(tzinfo=None)
    try:
        with (root / "logs/pipeline.log").open() as log:
            for line in log:
                match = re.search(r"\[([A-Z0-9]+)\] fxx=\d+ task metrics: (.+)", line)
                if match is None:
                    continue
                try:
                    if datetime.fromisoformat(line[:19]) < cutoff:
                        continue
                    values = ast.literal_eval(match[2])
                    for key in ("cpu_seconds", "elapsed_seconds", "peak_rss_mb", "rendered_params"):
                        if key in values:
                            task_metrics[match[1]][key].append(values[key])
                except (ValueError, SyntaxError, TypeError):
                    continue
    except OSError:
        pass
    summary["model_tasks"] = {model: {key: distribution(values) for key, values in fields.items()}
                              for model, fields in task_metrics.items()}
    path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def service_snapshot(service: str) -> dict:
    root = Path("/sys/fs/cgroup/system.slice") / f"{service}.service"
    def counters(name: str) -> dict:
        return {line.split()[0]: int(line.split()[1]) for line in (root / name).read_text().splitlines()}
    cpu = counters("cpu.stat")
    memory = counters("memory.events")
    return {
        "cpu_usage_us": cpu["usage_usec"], "throttled_periods": cpu.get("nr_throttled", 0),
        "memory_high_events": memory.get("high", 0), "oom_kills": memory.get("oom_kill", 0),
        "memory_mb": round(int((root / "memory.current").read_text()) / 1024 ** 2, 1),
        "swap_mb": round(int((root / "memory.swap.current").read_text()) / 1024 ** 2, 1),
        "memory_pressure": (root / "memory.pressure").read_text().strip(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=86400)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    previous = {}
    previous_time = time.monotonic()
    deadline = previous_time + args.seconds
    while True:
        now = time.monotonic()
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), "services": {}}
        for service in SERVICES:
            try:
                snapshot = service_snapshot(service)
                old = previous.get(service)
                if old:
                    snapshot["cpu_cores"] = round(max(0, snapshot["cpu_usage_us"] - old["cpu_usage_us"]) / 1e6 / max(.001, now - previous_time), 3)
                    for key in ("throttled_periods", "memory_high_events", "oom_kills"):
                        snapshot[f"{key}_delta"] = max(0, snapshot[key] - old[key])
                row["services"][service] = snapshot
                previous[service] = snapshot
            except (OSError, ValueError, KeyError) as exc:
                row["services"][service] = {"error": str(exc)}
        root = Path(__file__).resolve().parents[1] / "data"
        row["scan_age_minutes"] = {}
        latest = {
            "radar_koax": root / "rendered/radar/meta/KOAX_REF_latest.json",
            "satellite_ir": root / "rendered/satellite/meta/goes19_ir_latest.json",
            "satellite_m1": root / "rendered/satellite/meta/goes19_m1_ir_1km_latest.json",
            "satellite_m2": root / "rendered/satellite/meta/goes19_m2_ir_1km_latest.json",
        }
        for label, path in latest.items():
            try:
                timestamp = json.loads(path.read_text())["timestamp"]
                observed = None
                for fmt in ("%Y%m%d_%H%M%S", "%Y-%jT%H:%M:%SZ", "%Y-%jT%H:%MZ",
                            "%Y-%m-%dT%H-%M-%S.%fZ"):
                    try:
                        observed = datetime.strptime(timestamp, fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
                if observed is None:
                    observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                row["scan_age_minutes"][label] = round((datetime.now(timezone.utc) - observed).total_seconds() / 60, 2)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        try:
            state = json.loads((root / "pipeline_state.json").read_text())
            row["models"] = {key: state.get(key) for key in ("status", "model", "current_run", "resources", "queue_activity")}
        except (OSError, ValueError):
            pass
        with args.output.open("a") as output:
            output.write(json.dumps(row) + "\n")
        previous_time = now
        if now >= deadline:
            break
        time.sleep(min(max(1, args.interval), deadline - now))
    summarize(args.output)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
