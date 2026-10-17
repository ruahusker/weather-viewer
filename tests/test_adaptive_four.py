"""Larger bursts preserve maps, share exact geometry, and respect resources."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from pipeline import renderer, resource_budget as budget, unified_scheduler as scheduler
from pipeline.render_cost import RenderCosts, balanced_chunks
from pipeline.render_geometry import shared_arrays


class AdaptiveFourTests(unittest.TestCase):
    def test_ready_hours_have_priority_and_spare_cores_allow_four(self):
        choose = budget.render_processes_for_backlog
        self.assertEqual(choose(8, 8, 0, 8, 4), 1)
        self.assertEqual(choose(2, 8, 0, 8, 4), 2)
        self.assertEqual(choose(1, 7, 4, 8, 4), 2)
        self.assertEqual(choose(1, 8, 0, 8, 4), 4)
        self.assertEqual(choose(2, 8, 6, 8, 4), 1)
        self.assertEqual(choose(1, 8, 7, 8, 4), 1)
        self.assertEqual(choose(2, 8, 4, 8, 4), 2)
        self.assertEqual(choose(3, 8, 0, 10, 4), 2)
        self.assertEqual(choose(2, 7, 4, 10, 4), 2)

    def test_larger_splits_reserve_more_memory_and_have_separate_history(self):
        admission = budget.RenderAdmission()
        self.assertGreater(admission.estimate("hrrr", 4), admission.estimate("hrrr", 2))
        admission.job_processes[("hrrr", 1)] = 4
        admission.release(("hrrr", 1), {"peak_group_pss_mb": 12000, "baseline_pss_mb": 200})
        self.assertGreater(admission.estimate("hrrr", 4), 12000 * budget.MIB)
        self.assertLess(admission.estimate("hrrr", 2), 6000 * budget.MIB)
        admission.snapshot = budget.MemorySnapshot(available=30000 * budget.MIB, limit=30000 * budget.MIB)
        admission.cpu_budget = lambda: 8
        self.assertTrue(admission.reserve(("gfs", 0), render_processes=4))
        self.assertTrue(admission.reserve(("gfs", 1), render_processes=4))
        self.assertFalse(admission.reserve(("gfs", 2)))

    def test_measured_costs_balance_expensive_maps_and_survive_concurrent_updates(self):
        params = ["a", "b", "c", "d", "e", "f"]
        costs = {"a": 10, "b": 1, "c": 9, "d": 1, "e": 1, "f": 1}
        chunks = balanced_chunks(params, 2, costs)
        self.assertEqual(sorted(sum(chunks, [])), params)
        self.assertEqual([sum(costs[p] for p in chunk) for chunk in chunks], [12, 11])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "costs.json"
            with ThreadPoolExecutor(4) as pool:
                list(pool.map(lambda i: RenderCosts(path).update("hrrr", {str(i): i + 1}), range(8)))
            self.assertEqual(len(json.loads(path.read_text())["hrrr"]), 8)
            profile = RenderCosts(path)
            profile.update("hrrr", {"a": 10})
            self.assertEqual(profile.estimates("hrrr", ["a"])["a"], 10)
            path.write_text("broken")
            self.assertGreater(profile.estimates("hrrr", ["a"])["a"], 0)

    def test_completed_split_metrics_keep_process_count_after_reservation_release(self):
        admission = budget.RenderAdmission()
        admission.job_processes[("hrrr", 1)] = 4
        admission.release(("hrrr", 1))
        admission.release(("hrrr", 1), {"peak_group_pss_mb": 12000, "baseline_pss_mb": 200,
                                     "burst_subworkers": 4})
        self.assertEqual(list(admission.samples["hrrr"]), [])
        self.assertEqual(list(admission.samples["hrrr:4"]), [11800])

    def test_geometry_is_read_only_shared_and_recovers_from_invalid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            arrays = {"lon": np.arange(12, dtype=np.float32).reshape(3, 4)}
            first, hit = shared_arrays(tmp, ("grid", 1), (3, 4), ("lon",), lambda: arrays)
            self.assertFalse(hit)
            self.assertIsInstance(first["lon"], np.memmap)
            with self.assertRaises(ValueError):
                first["lon"][0, 0] = -1
            with patch("pipeline.render_geometry.np.save") as save:
                second, hit = shared_arrays(tmp, ("grid", 1), (3, 4), ("lon",),
                                            lambda: self.fail("warm geometry was recomputed"))
                self.assertTrue(hit)
                save.assert_not_called()
            np.testing.assert_array_equal(first["lon"], second["lon"])
            next(Path(tmp).glob("*.npy")).write_bytes(b"partial")
            recovered, hit = shared_arrays(tmp, ("grid", 1), (3, 4), ("lon",), lambda: arrays)
            self.assertFalse(hit)
            np.testing.assert_array_equal(recovered["lon"], arrays["lon"])

    def test_shared_projection_and_rotation_equal_uncached_formulas(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(renderer, "_REGRID_CACHE_DIR", Path(tmp)):
            first = renderer.OverlayRenderer()
            grid = first._get_target_grid((30, 45, -110, -90), 200, 150)
            lon, lat = np.meshgrid(np.linspace(-110, -90, 200, dtype=np.float32),
                                   np.linspace(45, 30, 150, dtype=np.float32))
            np.testing.assert_array_equal(grid["lon"], lon)
            np.testing.assert_array_equal(grid["merc_lat"], renderer._lat_to_merc(grid["lat"]))
            u = np.full((150, 200), 12, dtype=np.float32)
            v = u * 2
            rotated = first._rotate_to_earth_relative(lon, lat, grid, u, v)
            angle = np.sin(np.radians(38.5)) * np.radians(grid["lon"] + 97.5)
            np.testing.assert_array_equal(rotated[0], u * np.cos(angle) + v * np.sin(angle))
            second = renderer.OverlayRenderer()
            warm = second._get_target_grid((30, 45, -110, -90), 200, 150)
            actual = second._rotate_to_earth_relative(lon, lat, warm, u, v)
            np.testing.assert_array_equal(actual[0], rotated[0])
            self.assertEqual(second.geometry_cache_misses, 0)
            self.assertGreater(second.geometry_cache_hits, 0)

    def test_four_children_preserve_height_dependencies_and_release_hour_fields(self):
        lon, lat = np.meshgrid(np.linspace(-110, -90, 16), np.linspace(30, 45, 12))
        y, x = np.indices(lon.shape)
        def fields():
            def field(values):
                return {"data": np.asarray(values, dtype=np.float32), "lon": lon, "lat": lat}
            return {"temp_500mb": field(-20+x+y), "rh_500mb": field(40+x+y),
                    "wind_500mb": field(35+x+y), "u500": field(np.full(lon.shape, 25)),
                    "v500": field(np.full(lon.shape, 15)), "gh_500mb": field(5300+25*x+10*y)}
        from pipeline.processor import DERIVED_PARAMS
        u, v = DERIVED_PARAMS["wind_500mb"]
        def actual_fields():
            data = fields()
            data[u], data[v] = data.pop("u500"), data.pop("v500")
            return data
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("cache", "expected", "actual"):
                (root/name).mkdir()
            render = renderer.OverlayRenderer()
            state = {"renderer": render, "render_costs": RenderCosts(root/"costs.json")}
            with patch.object(renderer, "_REGRID_CACHE_DIR", root/"cache"), \
                 patch.object(scheduler, "_worker_state", state), \
                 patch.object(scheduler, "BURST_RENDER_MIN_PARAMS", 1):
                expected = render.render_all_params(actual_fields(), root/"expected", 0, model_name="hrrr")
                actual = scheduler._render_with_burst_subworkers(actual_fields(), root/"actual", 0,
                         datetime(2026, 10, 14, 12), ["temp_500mb", "rh_500mb", "wind_500mb"], "hrrr", 4)
                self.assertEqual(set(actual), set(expected))
                self.assertFalse(render._height_grid_cache)
                self.assertNotIn("processed", scheduler._burst_render_state)
                for name in expected:
                    with Image.open(root/"expected"/name/"f00.png") as a, Image.open(root/"actual"/name/"f00.png") as b:
                        np.testing.assert_array_equal(np.asarray(a), np.asarray(b), err_msg=name)
                # Fresh-hour heights must differ and must never reuse prior values.
                fresh = actual_fields()
                fresh["gh_500mb"]["data"] += 100
                render.prepare_shared_heights(fresh, [["temp_500mb"], ["rh_500mb"]], "hrrr")
                self.assertTrue(render._height_grid_cache)
                render.clear_hour_cache()
                self.assertFalse(render._height_grid_cache)


if __name__ == "__main__":
    unittest.main()
