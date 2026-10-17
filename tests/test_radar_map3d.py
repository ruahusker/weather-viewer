"""Geography, sampling gaps, and routing constraints of the regional 3D map."""

from types import SimpleNamespace
import asyncio
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import numpy as np

from pipeline.radar import map3d
from web.radar_map3d import checked_site
from web import radar_map3d as routes
from fastapi import HTTPException


def uniform_radar(value=40, maximum_range=300_000):
    azimuth = np.arange(0, 360, .5)
    ranges = np.arange(125, maximum_range, 250)
    data = np.ma.array(np.full((len(azimuth), len(ranges)), value, dtype=np.float32))
    return SimpleNamespace(latitude={'data': [41.32]}, longitude={'data': [-96.37]},
        altitude={'data': [384]}, range={'data': ranges}, azimuth={'data': azimuth},
        fixed_angle={'data': [.5]}, fields={'reflectivity': {'data': data}},
        sweep_start_ray_index={'data': [0]}, sweep_end_ray_index={'data': [len(azimuth) - 1]})


class MapVolumeTests(unittest.TestCase):
    def test_geographic_axes_and_distance(self):
        distance, bearing = map3d.grid_geometry(41.32, -96.37)
        mid = map3d.NX // 2
        self.assertLess(distance[mid, mid], 1500)
        self.assertLess(abs(bearing[mid, -1] - 90), 5)
        self.assertLess(abs(bearing[-1, mid] - 180), 5)
        self.assertLess(abs(bearing[mid, 0] - 270), 5)
        self.assertTrue(bearing[0, mid] < 5 or bearing[0, mid] > 355)

    def test_single_low_tilt_does_not_fill_unsampled_upper_atmosphere(self):
        with patch.multiple(map3d, NX=24, NY=24):
            grid, meta = map3d.sample_volume(uniform_radar())
        self.assertGreater(np.count_nonzero(grid), 0)
        self.assertEqual(np.count_nonzero(grid[20:]), 0)
        self.assertEqual(np.unique(grid[grid > 0]).tolist(), [144])
        self.assertEqual(meta['height_reference'], 'above radar antenna')
        self.assertAlmostEqual(meta['vertical_spacing_ft'], 500 / .3048)

    def test_short_range_is_not_extrapolated_into_distant_voxels(self):
        with patch.multiple(map3d, NX=24, NY=24):
            grid, _ = map3d.sample_volume(uniform_radar(maximum_range=5000))
        self.assertEqual(np.count_nonzero(grid), 0)

    def test_negative_echoes_and_masked_gates_stay_transparent(self):
        with patch.multiple(map3d, NX=24, NY=24):
            grid, meta = map3d.sample_volume(uniform_radar(value=-10))
        self.assertEqual(np.count_nonzero(grid), 0)
        self.assertIsNone(meta['echo_top_ft'])
        radar = uniform_radar()
        radar.fields['reflectivity']['data'][:] = np.ma.masked
        with self.assertRaises(ValueError):
            map3d.sample_volume(radar)

    def test_only_requested_radar_sites_are_enabled(self):
        self.assertEqual(checked_site('KOAX'), 'KOAX')
        self.assertEqual(checked_site('KUEX'), 'KUEX')
        for site in ['KTLX', '../KOAX', 'koax']:
            with self.assertRaises(HTTPException) as error:
                checked_site(site)
            self.assertEqual(error.exception.status_code, 404)


class SharedJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_viewers_share_one_decode(self):
        calls = []
        def decode(*args):
            calls.append(args)
            time.sleep(.04)
            return {'key': 'shared'}
        with patch.object(routes, 'ensure_volume', side_effect=decode):
            results = await asyncio.gather(*(routes.shared_volume('KOAX', Path('scan'), 'same') for _ in range(12)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(r == {'key': 'shared'} for r in results))

    async def test_disconnected_viewer_does_not_cancel_shared_decode(self):
        calls = []
        def decode(*args):
            calls.append(args)
            time.sleep(.06)
            return {'key': 'finished'}
        with patch.object(routes, 'ensure_volume', side_effect=decode):
            first = asyncio.create_task(routes.shared_volume('KUEX', Path('scan'), 'cancel'))
            await asyncio.sleep(.01)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            result = await routes.shared_volume('KUEX', Path('scan'), 'cancel')
        self.assertEqual(result, {'key': 'finished'})
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
