"""Learn CPU cost per map and balance forecast-hour splits."""
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import tempfile

LOGGER = logging.getLogger(__name__)


def balanced_chunks(params, count, costs):
    """Assign expensive maps first, retaining original order within each chunk."""
    count = max(1, min(count, len(params)))
    chunks, totals = [[] for _ in range(count)], [0.0] * count
    order = {name: index for index, name in enumerate(params)}
    for name in sorted(params, key=lambda name: (-costs.get(name, 1.0), order[name])):
        index = min(range(count), key=lambda i: (totals[i], len(chunks[i]), i))
        chunks[index].append(name)
        totals[index] += costs.get(name, 1.0)
    return [sorted(chunk, key=order.get) for chunk in chunks if chunk]


class RenderCosts:
    """Small locked profile shared by the persistent forecast-hour workers."""
    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
            return {model: {name: float(cost) for name, cost in values.items()
                            if math.isfinite(float(cost)) and float(cost) > 0}
                    for model, values in data.items()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def estimates(self, model, params):
        from .config import COLOR_SCALES
        learned = self._read().get(model, {})
        def initial(name):
            cfg = COLOR_SCALES.get(name, {})
            if name.startswith("wind_"):
                return 2.0
            if name in {"mslp", "psfc"} or name.startswith("gh_"):
                return .8
            if cfg.get("contours") or cfg.get("render") == "contour" or name.endswith("mb"):
                return .7
            return .15
        return {name: learned.get(name, initial(name)) for name in params}

    def update(self, model, measured):
        measured = {name: float(cost) for name, cost in measured.items()
                    if math.isfinite(float(cost)) and float(cost) > 0}
        if not measured:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                data = self._read()
                previous = data.setdefault(model, {})
                for name, cost in measured.items():
                    previous[name] = .7 * previous[name] + .3 * cost if name in previous else cost
                with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent,
                                                 prefix=".render-costs-", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(data, handle, allow_nan=False)
                temporary.chmod(0o644)
                os.replace(temporary, self.path)
        except OSError as exc:
            LOGGER.warning("Could not retain render costs: %s", exc)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
