"""Regression checks for resource limits, shared work, and unchanged map data."""

import ast
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import xarray as xr

from pipeline.resource_budget import MIB, MemorySnapshot, RenderAdmission
from pipeline.source_circuit import SourceCircuit


class AdmissionTests(unittest.TestCase):
    def admission(self):
        result = RenderAdmission()
        result.snapshot = MemorySnapshot(current=2 * 1024 * MIB, limit=8 * 1024 * MIB,
                                         available=12 * 1024 * MIB)
        return result

    def test_pending_jobs_reserve_memory_before_workers_start(self):
        admission = self.admission()
        self.assertTrue(admission.reserve(("rrfs", 1)))
        self.assertFalse(admission.reserve(("rrfs", 2)))
        self.assertTrue(admission.reserve(("gefs", 1)))
        admission.release(("rrfs", 1))
        self.assertTrue(admission.reserve(("rrfs", 2)))

    def test_live_allocations_are_not_counted_twice(self):
        admission = self.admission()
        self.assertTrue(admission.reserve(("hrrr", 1)))
        admission.snapshot.current = 5 * 1024 * MIB
        admission.workers[("hrrr", 1)] = {"allocated": 2 * 1024 * MIB}
        self.assertTrue(admission.reserve(("gfs", 1)))

    def test_reclaimable_file_cache_does_not_permanently_stall_idle_workers(self):
        admission = self.admission()
        admission.snapshot.current = 5500 * MIB
        admission.snapshot.reclaimable_file = 3000 * MIB
        self.assertTrue(admission.reserve(("hrrr", 1)))

    def test_local_pressure_blocks_work_when_host_has_free_memory(self):
        admission = self.admission()
        admission.snapshot.pressure = 4
        self.assertFalse(admission.reserve(("gefs", 1)))
        admission.snapshot.pressure = 0
        admission.snapshot.swap_growth = 32 * MIB
        self.assertFalse(admission.reserve(("gefs", 1)))

    def test_cold_worker_peaks_survive_restarts_and_partial_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            admission = RenderAdmission(metrics_path=path)
            admission.release(("rap", 1), {"baseline_rss_mb": 170, "peak_rss_mb": 2870})
            restored = RenderAdmission(metrics_path=path)
            self.assertGreaterEqual(restored.estimate("rap"), 3240 * MIB)
            for fxx in range(64):
                restored.release(("rap", fxx), {"baseline_rss_mb": 400, "peak_rss_mb": 500})
            self.assertGreaterEqual(restored.estimate("rap"), 2400 * MIB)

    def test_cold_rap_jobs_cannot_fill_all_six_slots_before_measurement(self):
        admission = self.admission()
        accepted = [fxx for fxx in range(6) if admission.reserve(("rap", fxx))]
        self.assertEqual(accepted, [0, 1])

    def test_family_preferences_borrowing_and_aging(self):
        tree = ast.parse(Path("pipeline/unified_scheduler.py").read_text())
        wanted = {"MODEL_PRIORITY_ORDER", "POOL_MODEL_GROUPS"}
        namespace = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets):
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<dispatch>", "exec"), namespace)
        namespace["MODEL_PRIORITY_RANK"] = {name: i for i, name in enumerate(namespace["MODEL_PRIORITY_ORDER"])}
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_balanced_dispatch_plan")
        namespace["Callable"] = object
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<dispatch>", "exec"), namespace)
        plan = namespace[function.name]({"hrrr": [0, 1, 2, 3], "rap": [0]},
                                       {"affinity_a": 2, "affinity_b": 2, "affinity_c": 2}, {}, 100,
                                       lambda *args: True)
        self.assertEqual(len(plan), 5)
        self.assertEqual(plan[:2], [("affinity_a", "hrrr", 0), ("affinity_a", "hrrr", 1)])
        self.assertIn(("affinity_b", "rap", 0), plan)
        aged = namespace[function.name]({"hrrr": [0], "gdps": [3]}, {"affinity_a": 1},
                                       {"gdps": 0, "hrrr": 199}, 200, lambda *args: True)
        self.assertEqual(aged, [("affinity_a", "gdps", 3)])

    def test_unpublished_hours_do_not_trip_source_circuit(self):
        circuit = SourceCircuit()
        self.assertFalse(circuit.failed("rap", "azure", FileNotFoundError()))
        self.assertTrue(circuit.available("rap", "azure"))
        self.assertTrue(circuit.failed("rap", "azure", KeyError("href")))
        self.assertFalse(circuit.available("rap", "azure"))
        self.assertTrue(circuit.available("rap", "aws"))


class RenderMemoryTests(unittest.TestCase):
    def test_radar_tilt_metadata_does_not_decode_field_arrays(self):
        from pipeline.radar.parser import read_volume_tilts
        from unittest.mock import Mock
        archive = Mock(return_value=SimpleNamespace(fixed_angle={"data": [.5, .5, 1.5, 2.5]}))
        pyart = SimpleNamespace(io=SimpleNamespace(read_nexrad_archive=archive))
        with patch.dict("sys.modules", {"pyart": pyart}):
            self.assertEqual(read_volume_tilts(Path("scan.ar2v")), [.5, 1.5, 2.5])
        archive.assert_called_once_with("scan.ar2v", delay_field_loading=True)

    def test_interrupted_model_frame_does_not_replace_a_complete_png(self):
        from pipeline.renderer import _atomic_write_png
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.png"
            path.write_bytes(b"previous complete frame")
            def interrupted(output, **kwargs):
                output.write(b"partial replacement")
                raise OSError("interrupted encode")
            image = SimpleNamespace(save=interrupted)
            with self.assertRaises(OSError):
                _atomic_write_png(image, path)
            self.assertEqual(path.read_bytes(), b"previous complete frame")
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_background_radar_keeps_vertical_reflectivity_and_owns_compact_fields(self):
        from pipeline.radar.parser import _build_compact_pyart_sweeps
        def radar(mask_low=False):
            data = np.ma.array(np.ones((12, 30), dtype=np.float32), mask=False)
            if mask_low:
                data[:4] = np.ma.masked
            return SimpleNamespace(
                nsweeps=3, sweep_start_ray_index={"data": np.array([0, 4, 8])},
                sweep_end_ray_index={"data": np.array([3, 7, 11])},
                range={"data": np.linspace(0, 100000, 30)},
                azimuth={"data": np.tile(np.arange(4) * 90, 3)},
                fields={"reflectivity": {"data": data.copy()}, "velocity": {"data": data.copy()}},
            )
        original = radar()
        fields = _build_compact_pyart_sweeps(original, np.array([.5, 1.5, 2.5]), True)
        self.assertEqual(sum(s is not None for s in fields["reflectivity"]), 3)
        self.assertEqual(sum(s is not None for s in fields["velocity"]), 1)
        self.assertTrue(fields["velocity"][0].data.flags.owndata)
        self.assertEqual(original.fields, {})
        fallback = _build_compact_pyart_sweeps(radar(True), np.array([.5, 1.5, 2.5]), True)
        self.assertIsNotNone(fallback["velocity"][1])
        full = _build_compact_pyart_sweeps(radar(), np.array([.5, 1.5, 2.5]))
        self.assertEqual(sum(s is not None for s in full["velocity"]), 3)

    def test_byte_colors_preserve_all_scalar_palette_values(self):
        from pipeline.config import COLOR_SCALES
        from pipeline.renderer import OverlayRenderer
        renderer = OverlayRenderer()
        checked = 0
        for parameter, config in COLOR_SCALES.items():
            if "cmap" not in config or parameter.startswith("radar_"):
                continue
            cmap, norm = renderer._make_cmap_norm(parameter)
            if cmap is None:
                continue
            low, high = config.get("vmin", 0), config.get("vmax", 100)
            values = np.r_[np.linspace(low - 1, high + 1, 1024), np.nan]
            normalized = norm(values)
            np.testing.assert_array_equal(cmap(normalized, bytes=True),
                                          (cmap(normalized) * 255).astype(np.uint8), err_msg=parameter)
            checked += 1
        self.assertGreater(checked, 50)

    def test_radar_cache_budget_and_compact_indices(self):
        from pipeline.radar import renderer
        renderer.clear_renderer_caches()
        azimuth = np.arange(360, dtype=np.float32)
        with patch.object(renderer, "_LOOKUP_CACHE_BYTES", 128 * 1024):
            for latitude in np.linspace(30, 45, 12):
                bounds = renderer.radar_bounds(latitude, -95, 230)
                indices, gates, valid = renderer.build_polar_lookup_mercator(
                    360, 920, 128, azimuth, latitude, -95, 230,
                    bounds["north"], bounds["south"], bounds["west"], bounds["east"],
                )
                self.assertEqual(indices.dtype, np.uint16)
                self.assertEqual(gates.dtype, np.uint16)
                self.assertTrue(np.all(indices[valid] < 360))
                self.assertTrue(np.all(gates[valid] < 920))
                self.assertLessEqual(renderer.renderer_cache_bytes(), 128 * 1024)
        renderer.clear_renderer_caches()

    def test_reflectivity_column_is_shared_only_within_one_volume(self):
        from pipeline.radar import derived
        from pipeline.radar.parser import SweepData, VolumeData
        def volume(value):
            sweeps = [SweepData(np.full((4, 40), value, dtype=np.float32),
                                np.arange(4, dtype=np.float32) * 90, angle, 100, 40)
                      for angle in (.5, 1.5)]
            return VolumeData(35, -95, [.5, 1.5], ["reflectivity"], None, Path("scan"),
                              np.array([.5, 1.5]), field_sweeps={"reflectivity": sweeps})
        first = volume(40)
        with patch.object(derived, "_resample_to_reference", wraps=derived._resample_to_reference) as resample:
            composite = derived._derive_composite_reflectivity(first)
            derived._derive_echo_tops(first)
            derived._derive_vil(first)
            self.assertEqual(resample.call_count, 2)
            np.testing.assert_array_equal(composite.data, 40)
            second = derived._derive_composite_reflectivity(volume(20))
            self.assertEqual(resample.call_count, 4)
            np.testing.assert_array_equal(second.data, 20)


class SatelliteCacheTests(unittest.TestCase):
    def test_sst_preserves_quality_gaps_and_converts_kelvin_to_celsius(self):
        from pipeline.satellite.satellite_renderer import SatelliteRenderer
        source = xr.Dataset({
            "SST": (("y", "x"), np.array([[300, 301], [302, 303]], dtype=np.float32)),
            "DQF": (("y", "x"), np.array([[0, 1], [2, 3]], dtype=np.int8)),
        }, attrs={"time_coverage_start": "2026-10-17T20:00:00Z", "platform_ID": "G19"})
        with tempfile.TemporaryDirectory() as tmp:
            renderer = SatelliteRenderer(output_dir=Path(tmp))
            with patch.object(renderer, "_render_l2_to_mercator", return_value={"west": -145, "east": -50, "south": 15, "north": 52}) as render:
                renderer.render_l2_product(source, "sst", filename="sst.png")
            values = render.call_args.args[0]
            np.testing.assert_allclose(values[0], [26.85, 27.85], atol=.0001)
            self.assertTrue(np.isnan(values[1]).all())
            self.assertFalse(render.call_args.kwargs["extend_masked"])

    def test_sst_auxiliary_band_coordinates_keep_their_roles_in_cache(self):
        from pipeline.satellite import dataset_cache
        dataset_cache.clear_dataset_cache()
        source = xr.Dataset(
            {"SST": (("y", "x"), np.full((4, 5), 300, dtype=np.float32)),
             "DQF": (("y", "x"), np.zeros((4, 5), dtype=np.int8)),
             "goes_imager_projection": xr.DataArray(0, attrs={"longitude_of_projection_origin": -75.2})},
            coords={"x": np.arange(5), "y": np.arange(4),
                    "SST_day_night_emissive_band_ids": ("bands", [7, 11, 13, 14]),
                    "SST_day_night_emissive_wavelengths": ("bands", [3.9, 8.4, 10.3, 11.2]),
                    "SST_night_only_emissive_band_id": 15},
            attrs={"time_coverage_start": "2026-10-17T19:00:00Z"},
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "sst.nc"
                source.to_netcdf(path, engine="scipy")
                with dataset_cache.load_dataset(path) as detached:
                    self.assertEqual(set(detached.coords), set(source.coords))
                    np.testing.assert_array_equal(detached["SST"], 300)
                    np.testing.assert_array_equal(detached["SST_day_night_emissive_band_ids"], [7, 11, 13, 14])
                    detached["SST"].values[:] = 999
                    detached["SST_day_night_emissive_band_ids"].values[:] = 0
                with dataset_cache.load_dataset(path, ("SST", "DQF", "SST_day_night_emissive_band_ids")) as again:
                    np.testing.assert_array_equal(again["SST"], 300)
                    np.testing.assert_array_equal(again["DQF"], 0)
                    np.testing.assert_array_equal(again["SST_day_night_emissive_band_ids"], [7, 11, 13, 14])
        finally:
            dataset_cache.clear_dataset_cache()

    def test_satellite_listings_fetch_newest_hours_first_and_stop_when_sufficient(self):
        from datetime import datetime, timedelta, timezone
        from pipeline.satellite.goes_downloader import GOESDownloader
        downloader = object.__new__(GOESDownloader)
        downloader.bucket, downloader.domain = "test", "C"
        downloader._product_prefix = lambda: "ABI-L2-MCMIPC"
        downloader._refresh_fs_if_stale = lambda: None
        newest = datetime(2026, 10, 14, 6, 30, tzinfo=timezone.utc)
        paths = [f"test/scan-{index}.nc" for index in range(10)]
        files = {path: SimpleNamespace(timestamp=newest-timedelta(minutes=index), domain="C")
                 for index, path in enumerate(paths)}
        downloader._parse_filename = lambda filename, path: files[path]
        listed = []
        def ls(prefix, refresh):
            listed.append(prefix)
            return paths
        downloader.fs = SimpleNamespace(invalidate_cache=lambda prefix: None, ls=ls)
        result = downloader.list_available(end_time=newest, hours=24, max_files=3)
        self.assertEqual([file.timestamp for file in result], [newest-timedelta(minutes=i) for i in range(3)])
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0].endswith("/06/"))

    def test_archive_time_slice_does_not_change_latest_frame_work(self):
        from pipeline.satellite.scheduler import SatelliteScheduler
        scheduler = object.__new__(SatelliteScheduler)
        scheduler.max_workers = 2
        scheduler.job_batch_size = 1
        scheduler.job_timeout_seconds = 120
        scheduler.archive_budget_seconds = .01
        scheduler._trim_memory_if_needed = lambda reason: None
        seen = []
        def job():
            seen.append(scheduler.loop_frames_per_band)
            time.sleep(.02)
            return True
        jobs = [(str(index), job, ()) for index in range(3)]
        self.assertEqual(scheduler._run_job_phase("test", jobs, 1, False), 3)
        self.assertEqual(seen, [1, 1, 1])
        seen.clear()
        self.assertEqual(scheduler._run_job_phase("test", jobs, 12, True), 1)
        self.assertEqual(seen, [12])
        self.assertIsNone(scheduler._archive_deadline)
        self.assertLess(scheduler._job_priority("m1_ir_1km"), scheduler._job_priority("sst"))

    def test_two_products_share_one_atomic_raw_download(self):
        from concurrent.futures import ThreadPoolExecutor
        from pipeline.satellite.goes_downloader import GOESDownloader
        downloader = object.__new__(GOESDownloader)
        downloader._refresh_fs_if_stale = lambda: None
        calls = []
        def get(remote, path):
            calls.append(remote)
            time.sleep(.02)
            Path(path).write_bytes(b"complete NetCDF fixture")
        downloader.fs = SimpleNamespace(get=get)
        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(max_workers=2) as pool:
            files = [SimpleNamespace(filename="scan.nc", s3_path="same", local_path=None) for _ in range(2)]
            futures = [pool.submit(downloader.download_file, file, Path(tmp)) for file in files]
            outputs = [future.result() for future in futures]
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(outputs[0].read_bytes(), b"complete NetCDF fixture")
            self.assertEqual(calls, ["same"])

    def test_detached_bands_reuse_reads_and_cannot_mutate_other_products(self):
        from pipeline.satellite import dataset_cache
        dataset_cache.clear_dataset_cache()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.nc"
            source = xr.Dataset({"CMI_C13": (("y", "x"), np.ones((4, 5), dtype=np.float32)),
                                 "CMI_C02": (("y", "x"), np.full((4, 5), 2, dtype=np.float32)),
                                 "goes_imager_projection": xr.DataArray(0, attrs={"longitude_of_projection_origin": -75})},
                                coords={"x": np.arange(5), "y": np.arange(4)}, attrs={"time_coverage_start": "2026-10-14T00:00:00Z"})
            source.to_netcdf(path, engine="scipy")
            with patch.object(dataset_cache.xr, "open_dataset", wraps=xr.open_dataset) as opened:
                with dataset_cache.load_dataset(path, ("CMI_C13",)) as first:
                    first["CMI_C13"].values[:] = 99
                    with dataset_cache.load_dataset(path, ("CMI_C02",)) as second:
                        np.testing.assert_array_equal(second["CMI_C02"], 2)
                with dataset_cache.load_dataset(path, ("CMI_C13",)) as again:
                    np.testing.assert_array_equal(again["CMI_C13"], 1)
                    self.assertEqual(again.attrs["time_coverage_start"], source.attrs["time_coverage_start"])
                self.assertEqual(opened.call_count, 2)
        dataset_cache.clear_dataset_cache()


class HeavyQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_job_consumes_one_decode_reservation_in_a_warm_api(self):
        from web.heavy_jobs import HeavyJobQueue
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            cgroup = directory / "cgroup"
            cgroup.mkdir()
            (cgroup / "memory.high").write_text(str(4 * 1024 * MIB))
            (cgroup / "memory.current").write_text(str(1800 * MIB))
            (cgroup / "memory.stat").write_text("inactive_file 0\n")
            queue = HeavyJobQueue(directory)
            queue._cgroup = cgroup
            result = await queue.run("large", lambda: {"volume": True}, slots=2)
            self.assertEqual(result, {"volume": True})
            await queue.close()

    async def test_two_pending_decodes_are_reserved_before_array_allocation(self):
        from web.heavy_jobs import HeavyJobQueue, QueueBusy
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            cgroup = directory / "cgroup"
            cgroup.mkdir()
            (cgroup / "memory.high").write_text(str(4 * 1024 * MIB))
            (cgroup / "memory.current").write_text(str(1800 * MIB))
            (cgroup / "memory.stat").write_text("inactive_file 0\n")
            first, second = [HeavyJobQueue(directory, wait_seconds=.05) for _ in range(2)]
            first._cgroup = second._cgroup = cgroup
            lease = await first._slot(time.monotonic() + 1, 1)
            try:
                with self.assertRaises(QueueBusy):
                    await second._slot(time.monotonic() + .05, 1)
            finally:
                for handle in lease:
                    handle.close()
                await first.close()
                await second.close()

    async def test_large_volume_job_reserves_both_shared_slots(self):
        from web.heavy_jobs import HeavyJobQueue
        with tempfile.TemporaryDirectory() as tmp:
            first, second = [HeavyJobQueue(Path(tmp)) for _ in range(2)]
            started, release, tilt_started = threading.Event(), threading.Event(), threading.Event()
            def volume():
                started.set()
                release.wait(2)
                return {"volume": True}
            request = asyncio.create_task(first.run("volume", volume, slots=2))
            try:
                while not started.is_set():
                    await asyncio.sleep(.01)
                tilt = asyncio.create_task(second.run("tilt", lambda: (tilt_started.set() or {"tilt": True})))
                await asyncio.sleep(.05)
                self.assertFalse(tilt_started.is_set())
                release.set()
                self.assertEqual(await request, {"volume": True})
                self.assertEqual(await tilt, {"tilt": True})
            finally:
                release.set()
                await first.close()
                await second.close()

    async def test_two_api_workers_share_a_compute_limit_and_identical_results(self):
        from web.heavy_jobs import HeavyJobQueue
        with tempfile.TemporaryDirectory() as tmp:
            queues = [HeavyJobQueue(Path(tmp)) for _ in range(2)]
            counts = {"active": 0, "peak": 0, "calls": 0}
            lock = threading.Lock()
            def compute():
                with lock:
                    counts["active"] += 1
                    counts["calls"] += 1
                    counts["peak"] = max(counts["peak"], counts["active"])
                time.sleep(.03)
                with lock:
                    counts["active"] -= 1
                return {"value": 42}
            try:
                values = await asyncio.gather(*(queues[i % 2].run("same", compute) for i in range(6)))
                self.assertEqual(counts["calls"], 1)
                self.assertTrue(all(value == {"value": 42} for value in values))
                await asyncio.gather(*(queues[i % 2].run(str(i), compute) for i in range(8)))
                self.assertLessEqual(counts["peak"], 2)
            finally:
                await asyncio.gather(*(queue.close() for queue in queues))

    async def test_client_cancellation_keeps_the_compute_lease_until_work_finishes(self):
        from web.heavy_jobs import HeavyJobQueue
        with tempfile.TemporaryDirectory() as tmp:
            first, second = [HeavyJobQueue(Path(tmp), workers=1) for _ in range(2)]
            started, release, next_started = threading.Event(), threading.Event(), threading.Event()
            def compute():
                started.set()
                release.wait(2)
                return {"first": True}
            request = asyncio.create_task(first.run("first", compute))
            try:
                while not started.is_set():
                    await asyncio.sleep(.01)
                request.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await request
                waiting = asyncio.create_task(second.run("second", lambda: (next_started.set() or {"second": True})))
                await asyncio.sleep(.05)
                self.assertFalse(next_started.is_set())
                release.set()
                self.assertEqual(await waiting, {"second": True})
            finally:
                release.set()
                await first.close()
                await second.close()


if __name__ == "__main__":
    unittest.main()
