"""Bound retries of structurally broken upstream download sources."""

import threading
import time


class SourceCircuit:
    """Temporarily stop source probes after an invalid upstream response."""

    def __init__(self, cooldown_seconds: int = 300) -> None:
        self.cooldown = cooldown_seconds
        self._until: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def available(self, model: str, source: str) -> bool:
        with self._lock:
            key = (model, source)
            until = self._until.get(key, 0)
            if until <= time.monotonic():
                self._until.pop(key, None)
                return True
            return False

    def failed(self, model: str, source: str, error: Exception) -> bool:
        # Missing data for a future hour is normal and must not disable a
        # source. Only the known malformed signer response opens this circuit.
        if source != "azure" or not isinstance(error, KeyError) or error.args != ("href",):
            return False
        with self._lock:
            self._until[(model, source)] = time.monotonic() + self.cooldown
        return True


SOURCE_CIRCUIT = SourceCircuit()
