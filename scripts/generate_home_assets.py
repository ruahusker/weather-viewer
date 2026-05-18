#!/usr/bin/env python3
"""Generate cached homepage assets in one pass."""

from __future__ import annotations

import subprocess
import sys


STEPS = [
    ["/home/main/weather-viewer/.venv/bin/python", "/home/main/weather-viewer/scripts/generate_radar_composite.py"],
    ["/home/main/weather-viewer/.venv/bin/python", "/home/main/weather-viewer/scripts/generate_homepage_stills.py"],
]


def main() -> int:
    for cmd in STEPS:
        print(f"running: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, timeout=480)
        except subprocess.TimeoutExpired:
            print(f"timeout: {' '.join(cmd)}")
            return 1
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
