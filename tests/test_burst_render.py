"""Split rendering preserves sequential output and recovers from child failure."""

import importlib.util
import os
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime

import numpy as np
from PIL import Image

from pipeline import renderer
from pipeline.processor import DERIVED_PARAMS
from pipeline.resource_budget import MIB, MemorySnapshot, RenderAdmission

if os.getenv("WX_BURST_CANDIDATE"):
    spec = importlib.util.spec_from_file_location("pipeline.burst_candidate", os.environ["WX_BURST_CANDIDATE"])
    scheduler = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scheduler
    spec.loader.exec_module(scheduler)
    budget_spec = importlib.util.spec_from_file_location(
        "pipeline.burst_budget_candidate", Path(os.environ["WX_BURST_CANDIDATE"]).with_name("resource_budget.py"),
    )
    budget = importlib.util.module_from_spec(budget_spec)
    sys.modules[budget_spec.name] = budget
    budget_spec.loader.exec_module(budget)
    RenderAdmission = budget.RenderAdmission
else:
    from pipeline import unified_scheduler as scheduler


def exit_without_result(*args):
    os._exit(1)


class BurstRenderTests(unittest.TestCase):
    def fields(self):
        lon, lat = np.meshgrid(np.linspace(-110, -90, 16), np.linspace(30, 45, 12))
        y, x = np.indices(lon.shape)
        def field(values):
            return {"data": np.asarray(values, dtype=np.float32), "lon": lon, "lat": lat}
        u, v = DERIVED_PARAMS["wind_500mb"]
        return {
            "temp_500mb": field(-20 + x + y),
            "gh_500mb": field(5300 + 25 * x + 10 * y),
            "wind_500mb": field(35 + x + y),
            u: field(np.full(lon.shape, 25)),
            v: field(np.full(lon.shape, 15)),
        }

    def test_split_matches_sequential_images_including_height_and_wind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sequential, split = root / "sequential", root / "split"
            sequential.mkdir()
            split.mkdir()
            (root / "cache").mkdir()
            with patch.object(renderer, "_REGRID_CACHE_DIR", root / "cache"), \
                 patch.object(scheduler, "BURST_RENDER_MIN_PARAMS", 1):
                expected = renderer.OverlayRenderer().render_all_params(
                    self.fields(), sequential, 0, model_name="hrrr",
                )
                actual = scheduler._render_with_burst_subworkers(
                    self.fields(), split, 0, datetime(2026, 10, 14, 12),
                    ["temp_500mb", "wind_500mb"], "hrrr", 2,
                )
            self.assertEqual(set(actual), set(expected))
            self.assertEqual(set(actual), {"temp_500mb", "wind_500mb", "gh_500mb"})
            for name in expected:
                with Image.open(sequential / name / "f00.png") as a, \
                     Image.open(split / name / "f00.png") as b:
                    np.testing.assert_array_equal(np.asarray(a), np.asarray(b), err_msg=name)

    def test_child_exit_falls_back_without_blocking_the_worker(self):
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(scheduler, "BURST_RENDER_MIN_PARAMS", 1), \
             patch.object(scheduler, "_burst_render_child", exit_without_result):
            result = scheduler._render_with_burst_subworkers(
                self.fields(), Path(tmp), 0, datetime(2026, 10, 14, 12),
                ["temp_500mb", "wind_500mb"], "hrrr", 2,
            )
        self.assertIsNone(result)
        self.assertLess(time.monotonic() - started, 5)
        self.assertNotIn("processed", scheduler._burst_render_state)

    def test_split_jobs_reserve_additional_memory_before_dispatch(self):
        admission = RenderAdmission()
        admission.snapshot = MemorySnapshot(current=2 * 1024 * MIB,
                                           limit=8 * 1024 * MIB,
                                           available=12 * 1024 * MIB)
        self.assertTrue(admission.reserve(("hrrr", 1), memory_multiplier=1.5))
        self.assertFalse(admission.reserve(("hrrr", 2), memory_multiplier=1.5))
        admission.release(("hrrr", 1))
        self.assertTrue(admission.reserve(("hrrr", 2)))


if __name__ == "__main__":
    unittest.main()
