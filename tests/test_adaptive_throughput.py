"""Resource admission and caches preserve work while using idle capacity."""
from collections import namedtuple
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from pipeline import resource_budget as budget
from pipeline import renderer


class AdaptiveAdmissionTests(unittest.TestCase):
    def test_independent_hours_get_first_use_of_cores(self):
        choose = budget.render_processes_for_backlog
        self.assertEqual(choose(20, 8, 0, 8), 1)
        self.assertEqual(choose(2, 8, 0, 8), 2)
        self.assertEqual(choose(2, 2, 6, 8), 1)
        self.assertEqual(choose(1, 1, 6, 8), 2)
        self.assertEqual(choose(1, 1, 7, 8), 1)

    def test_pss_only_visits_known_unique_processes(self):
        with patch.object(budget.psutil, 'Process') as process:
            process.return_value.memory_full_info.return_value = SimpleNamespace(pss=100, rss=200)
            self.assertEqual(budget.process_group_pss(10, [11, 11]), 200)
            self.assertEqual(process.call_count, 2)

    def test_cpu_budget_accounts_for_existing_model_work(self):
        admission = budget.RenderAdmission()
        admission.logical_cpus = 12
        admission.max_cpu_cores = 10
        admission.cpu_reserve = .5
        admission.snapshot = budget.MemorySnapshot(cpu_percent=90, model_cpu_cores=8)
        self.assertEqual(admission.cpu_budget(), 8)
        admission.snapshot.model_cpu_cores = 0
        self.assertEqual(admission.cpu_budget(), 1)

    def test_host_cpu_sampling_is_independent_of_other_scheduler_checks(self):
        Times = namedtuple("Times", "user system idle iowait guest guest_nice")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(budget.psutil, 'cpu_times', side_effect=[
                 Times(100,100,800,0,0,0), Times(120,110,860,10,0,0),
             ]), patch.object(budget.psutil, 'cpu_percent', return_value=1) as shared:
            admission = budget.RenderAdmission(cgroup=Path(tmp))
            admission.refresh(Path(tmp), force=True)
            self.assertAlmostEqual(admission.snapshot.cpu_percent,30)
            shared.assert_not_called()

    def test_children_already_in_cgroup_are_not_reserved_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            admission = budget.RenderAdmission()
            admission.jobs[('hrrr', 1)] = 5000 * budget.MIB
            Path(tmp, '10.json').write_text(json.dumps({
                'pid': 10, 'child_pids': [11, 12], 'model': 'hrrr', 'fxx': 1,
                'baseline_pss_mb': 200,
            }))
            with patch.object(budget, 'process_group_pss', return_value=4000 * budget.MIB):
                admission.refresh(Path(tmp), force=True)
            self.assertEqual(admission.reserved_bytes(), (5000-3800-256)*budget.MIB)

    def test_split_peaks_do_not_inflate_single_process_estimates(self):
        admission = budget.RenderAdmission()
        admission.job_processes[('hrrr', 1)] = 2
        admission.release(('hrrr', 1), {'peak_group_pss_mb': 6000, 'baseline_pss_mb': 200})
        self.assertEqual(admission.estimate('hrrr'), 2300*budget.MIB)
        self.assertGreater(admission.estimate('hrrr', 2), 6000*budget.MIB)


class RendererCacheTests(unittest.TestCase):
    def fields(self, offset=0):
        lon, lat = np.meshgrid(np.linspace(-110, -90, 16), np.linspace(30, 45, 12))
        return {'lon': lon, 'lat': lat, 'data': np.arange(192, dtype=np.float32).reshape(12,16)+offset}

    def test_cached_height_is_exact_and_bounded_and_does_not_reuse_other_fields(self):
        render = renderer.OverlayRenderer()
        render._height_grid_cache_limit = 200 * 150 * 4
        first, second = self.fields(), self.fields(100)
        bounds=(30,45,-110,-90)
        cfg={'smooth_sigma': .8}
        with patch.object(render, '_regrid', side_effect=lambda lon,lat,data,b,w,h: np.resize(data,(h,w))) as regrid:
            a=render._height_contour_grid('gh_500mb',first,bounds,200,150,cfg)
            b=render._height_contour_grid('gh_500mb',first,bounds,200,150,cfg)
            self.assertIs(a,b)
            self.assertEqual(regrid.call_count,1)
            c=render._height_contour_grid('gh_500mb',second,bounds,200,150,cfg)
            np.testing.assert_allclose(c-a,100,atol=.0001)
            self.assertLessEqual(render._height_grid_cache_bytes,render._height_grid_cache_limit)
            render._height_grid_cache_limit=0
            render.clear_caches()
            uncached=render._height_contour_grid('gh_500mb',first,bounds,200,150,cfg)
            np.testing.assert_array_equal(a,uncached)
            self.assertEqual(render._height_grid_cache_bytes,0)
        render._height_grid_cache['stale'] = (a,first)
        render._height_grid_cache_bytes = a.nbytes
        with tempfile.TemporaryDirectory() as tmp:
            render.render_all_params({},Path(tmp),1)
        self.assertFalse(render._height_grid_cache)
        self.assertEqual(render._height_grid_cache_bytes,0)

    def test_projection_and_rotation_cache_preserves_calculations(self):
        render=renderer.OverlayRenderer()
        grid=render._get_target_grid((30,45,-110,-90),200,150)
        np.testing.assert_array_equal(grid['merc_lat'],renderer._lat_to_merc(grid['lat']))
        field=self.fields()
        u=np.ones((150,200),dtype=np.float32)*12
        v=u*2
        angle=np.sin(np.radians(38.5))*np.radians(grid['lon']+97.5)
        actual=render._rotate_to_earth_relative(field['lon'],field['lat'],grid,u,v)
        np.testing.assert_array_equal(actual[0],u*np.cos(angle)+v*np.sin(angle))
        np.testing.assert_array_equal(actual[1],-u*np.sin(angle)+v*np.cos(angle))
        self.assertIs(render._get_target_grid((30,45,-110,-90),200,150),grid)

if __name__=='__main__':
    unittest.main()
