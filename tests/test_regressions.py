"""Weather API, pipeline dependency and rendered-map regression checks."""
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image
from fastapi.testclient import TestClient

from web import app as api
from pipeline.renderer import OverlayRenderer, _SCALAR_HEIGHT_OVERLAYS
from pipeline.radar.renderer import Level2Renderer
from pipeline.radar.color_tables import FIELD_CONFIG, RADAR_PALETTE_VERSION, get_radar_lut
from scripts.audit_product_catalog import _scheduler_catalog


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'meta').mkdir()
        (self.root / 'png').mkdir()
        self.patch = patch.object(api, 'RADAR_RENDERED_DIR', self.root)
        self.patch.start()
        self.bounds = {'south': 30, 'north': 40, 'west': -100, 'east': -90,
                       'center_lat': 35, 'center_lon': -95, 'max_range_km': 460}
        lut, _, _ = get_radar_lut('reflectivity', 1)
        for timestamp, tilt, index in [('20260930_120000', .48, 90), ('20260930_120000', 1.5, 200),
                                        ('20260930_120500', .52, 140)]:
            stem = f'KOAX_REF_{tilt}_{timestamp}'
            Image.new('RGBA', (10, 10), tuple(map(int, lut[index]))).save(self.root / 'png' / f'{stem}.png')
            (self.root / 'meta' / f'{stem}.json').write_text(json.dumps({
                'timestamp': timestamp, 'tilt': tilt, 'bounds': self.bounds,
                'png_path': f'radar/png/{stem}.png', 'field': 'reflectivity',
            }))
        (self.root / 'meta' / 'KOAX_REF_broken.json').write_text('{')

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()
        self.client.close()

    def test_radar_loop_does_not_mix_elevations(self):
        frames = self.client.get('/api/radar/frames/KOAX/REF').json()
        self.assertEqual([x['tilt'] for x in frames], [.48, .52])
        upper = self.client.get('/api/radar/frames/KOAX/REF?tilt=1.5').json()
        self.assertEqual([x['tilt'] for x in upper], [1.5])
        self.assertEqual(self.client.get('/api/radar/frames/KOAX/REF?tilt=-1').status_code, 422)

    def test_radar_value_matches_displayed_frame_and_tilt(self):
        url = '/api/value/radar/KOAX/REF?lat=35&lon=-95'
        first = self.client.get(url + '&frame=20260930_120000').json()['value']
        latest = self.client.get(url).json()['value']
        upper = self.client.get(url + '&tilt=1.5&frame=20260930_120000').json()['value']
        self.assertLess(first, latest)
        self.assertLess(latest, upper)
        self.assertEqual(self.client.get(url + '&frame=20200101_000000').status_code, 404)

    def test_radar_values_and_legends_match_both_archived_and_new_palettes(self):
        for field, product, parameter in [('reflectivity', 'REF', 'radar_reflectivity'),
                                          ('velocity', 'VEL', 'radar_velocity')]:
            for version in range(1, RADAR_PALETTE_VERSION + 1):
                with self.subTest(field=field, version=version):
                    lut, vmin, vmax = get_radar_lut(field, version)
                    timestamp = f'20260930_13000{version}'
                    stem = f'KOAX_{product}_0.5_{timestamp}'
                    Image.new('RGBA', (10, 10), tuple(map(int, lut[220]))).save(self.root / 'png' / f'{stem}.png')
                    (self.root / 'meta' / f'{stem}.json').write_text(json.dumps({
                        'timestamp': timestamp, 'tilt': .5, 'bounds': self.bounds,
                        'png_path': f'radar/png/{stem}.png', 'field': field,
                        **({'palette_version': version} if version > 1 else {}),
                    }))
                    value = self.client.get(f'/api/value/radar/KOAX/{product}?lat=35&lon=-95&frame={timestamp}').json()
                    self.assertAlmostEqual(value['value'], vmin + 220 / 255 * (vmax - vmin), delta=.15)
                    frames = self.client.get(f'/api/radar/frames/KOAX/{product}').json()
                    self.assertEqual(next(f for f in frames if f['timestamp'] == timestamp)['palette_version'], version)
                    legend = self.client.get(f'/api/legend/{parameter}?radar_palette_version={version}').json()
                    self.assertEqual((legend['vmin'], legend['vmax']), (vmin, vmax))
                    self.assertEqual(len(legend['stops']), 256)
                    r, g, b, alpha = lut[220]
                    self.assertEqual(legend['stops'][220]['color'], f'rgba({r},{g},{b},{alpha/255:.3f})')
        self.assertEqual(self.client.get('/api/legend/radar_reflectivity?radar_palette_version=999').status_code, 422)

    def test_radar_weak_echoes_are_translucent_and_missing_data_stays_transparent(self):
        from pipeline.radar.renderer import render_sweep_to_png
        for field in ('reflectivity', 'velocity'):
            lut, vmin, vmax = get_radar_lut(field)
            old_lut, _, _ = get_radar_lut(field, 2)
            solid_lut, _, _ = get_radar_lut(field, 3)
            np.testing.assert_array_equal(lut, old_lut)
            np.testing.assert_array_equal(lut[:, :3], solid_lut[:, :3])
            self.assertTrue(np.all(solid_lut[:, 3] == 255))
            self.assertTrue(np.any(lut[:, 3] < 255))
            # Alternate data and missing rays to exercise real gate boundaries.
            data = np.full((8, 10), 10.0, dtype=np.float32)
            data[::2] = np.nan
            with np.errstate(invalid='ignore'):
                png = render_sweep_to_png(data, np.arange(8) * 45.0, lut, vmin, vmax,
                                          FIELD_CONFIG[field]['mask_below'], output_size=64)
            pixels = np.asarray(png)
            expected = lut[int((10.0 - vmin) / (vmax - vmin) * 255)]
            self.assertGreater(expected[3], 0)
            self.assertLess(expected[3], 255)
            self.assertEqual(set(np.unique(pixels[:, :, 3])), {0, expected[3]})
            self.assertTrue(np.all(pixels[pixels[:, :, 3] > 0] == expected))

    def test_errors_are_not_cached_as_immutable_assets(self):
        response = self.client.get('/rendered/not-a-weather-file.png')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_legends_explain_lines_and_units(self):
        wind = self.client.get('/api/legend/wind_500mb').json()
        self.assertEqual(wind['annotation'], 'Height contours (m) · Wind barbs (kt)')
        pressure = self.client.get('/api/legend/mslp').json()
        self.assertTrue(pressure['contour_only'])
        self.assertIn('mb', pressure['annotation'])

    def test_vector_availability_keeps_raster_only_frames_as_fallback(self):
        run = self.root / 'nam3km' / '2026100812'
        (run / 'mslp').mkdir(parents=True)
        for name in ('f00.contours.json', 'f02.contours.json', 'f01.png', '.f03.contours.tmp'):
            (run / 'mslp' / name).write_text('{}')
        with patch.object(api, 'RENDERED_DIR', self.root):
            response = self.client.get('/api/available/nam3km/2026100812/vectors')
            self.assertEqual(response.json(), {'mslp': [0, 2]})
            self.assertEqual(self.client.get('/api/available/nam3km/invalid/vectors').status_code, 400)

    def test_explicit_view_and_shared_source_routing(self):
        response = self.client.get('/?view=mobile&source=models')
        self.assertIn('mobile.js?', response.text)
        self.assertEqual(response.cookies['wx_view'], 'mobile')
        response = self.client.get('/?view=desktop&source=models')
        self.assertIn('js/app.js?', response.text)
        self.assertIn('mobile.js?', self.client.get('/mobile.html').text)
        self.client.cookies.clear()
        response = self.client.get('/?desktop=1', headers={'User-Agent': 'iPhone Mobile'})
        self.assertIn('js/app.js?', response.text)

    def test_monitor_marks_old_completed_runs_as_stale(self):
        run = self.root / 'gdps' / '2026052100'
        run.mkdir(parents=True)
        (run / 'run_meta.json').write_text(json.dumps({'forecast_hours': [0], 'completed_fxx': [0], 'is_complete': True}))
        with patch.object(api, 'RENDERED_DIR', self.root), \
             patch.object(api, '_MODEL_NAMES', ['gdps']), \
             patch.object(api, '_MODEL_STATUS_CACHE', {'data': None, 'ts': 0}), \
             patch.object(api, '_reported_completed_fxx', return_value=[0]), \
             patch.object(api, '_read_pipeline_state', return_value={}):
            status = api._get_model_status()[0]
        self.assertTrue(status['data_stale'])
        self.assertGreater(status['run_age_hours'], 24)


class MapRenderTests(unittest.TestCase):
    def test_pressure_vectors_preserve_contour_values_and_geographic_alignment(self):
        renderer = OverlayRenderer()
        lon, lat = np.meshgrid(np.linspace(-120, -70, 80), np.linspace(25, 50, 60))
        values = 1010 + (lon + 95) * 0.4
        field = {'data': values.astype(np.float32), 'lat': lat, 'lon': lon}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(renderer, '_get_mpl_resolution', return_value=(80, 60)), \
             patch.object(renderer, '_regrid', return_value=values), \
             patch.object(renderer, '_get_target_grid', return_value={'lon': lon, 'lat': lat}):
            output = Path(tmp) / 'pressure.png'
            renderer.render_overlay('mslp', field, output)
            data = json.loads(output.with_suffix('.contours.json').read_text())
            self.assertEqual(data['type'], 'FeatureCollection')
            contour = next(f for f in data['features'] if f['properties'].get('kind') == 'isobar'
                           and f['properties']['value'] == 1008)
            self.assertTrue(contour['properties']['major'])
            coordinates = np.asarray(contour['geometry']['coordinates'][0])
            np.testing.assert_allclose(coordinates[:, 0], -100, atol=.00001)
            self.assertAlmostEqual(coordinates[:, 1].min(), 25, places=4)
            self.assertAlmostEqual(coordinates[:, 1].max(), 50, places=4)
            self.assertTrue(all(np.isfinite(np.asarray(f['geometry']['coordinates'])).all()
                                for f in data['features'] if f['geometry']['type'] == 'Point'))
            self.assertTrue(output.is_file())

    def test_every_upper_air_product_downloads_its_height_field(self):
        for model, params in _scheduler_catalog()['models'].items():
            module = importlib.import_module('pipeline.models.' + {'nam12km': 'nam'}.get(model, model))
            for param in params:
                height = _SCALAR_HEIGHT_OVERLAYS.get(param)
                if height:
                    with self.subTest(model=model, param=param):
                        self.assertIn(height, module.PARAM_SEARCH_STRINGS)
                        # NAM nest reuses the processor's shared GRIB mappings.
                        from pipeline.processor import FieldProcessor
                        candidates = FieldProcessor()._build_match_candidates([height])
                        self.assertTrue(candidates[0][1])

    def test_upper_tilt_render_does_not_replace_latest_low_tilt(self):
        sweep = SimpleNamespace(data=np.full((8, 10), 35.0), cc_data=None,
                                azimuth=np.arange(8), max_range_km=230, n_gates=10,
                                tilt_angle=.5)
        with tempfile.TemporaryDirectory() as tmp:
            renderer = Level2Renderer(output_dir=Path(tmp), output_size=32)
            with patch('pipeline.radar.renderer.render_sweep_to_png', return_value=Image.new('RGBA', (32, 32))):
                lower = renderer.render_product(sweep, 'KOAX', 'reflectivity', '20260930_120000', 35, -95)
                sweep.tilt_angle = 1.5
                renderer.render_product(sweep, 'KOAX', 'reflectivity', '20260930_120000', 35, -95, update_latest=False)
            self.assertEqual((Path(tmp) / 'meta/KOAX_REF_latest.json').resolve(), lower.meta_path)
            self.assertEqual(json.loads(lower.meta_path.read_text())['palette_version'], RADAR_PALETTE_VERSION)

    def test_pressure_extrema_get_high_and_low_labels(self):
        renderer = OverlayRenderer()
        n = 360
        y, x = np.mgrid[:n, :n]
        values = 1010 + 20 * np.exp(-((x-80)**2+(y-80)**2)/100) - 20 * np.exp(-((x-280)**2+(y-280)**2)/100)
        lon, lat = np.meshgrid(np.linspace(-120, -70, n), np.linspace(25, 50, n))
        field = {'data': values.astype(np.float32), 'lat': lat, 'lon': lon}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(renderer, '_get_mpl_resolution', return_value=(n, n)), \
             patch.object(renderer, '_regrid', return_value=values), \
             patch.object(renderer, '_get_target_grid', return_value={'lon': lon, 'lat': lat}), \
             patch.object(renderer, '_annotate_pressure_centers') as annotate:
            renderer._render_mslp(field, Path(tmp) / 'pressure.png')
        centers = {call.args[2]: call.args[1] for call in annotate.call_args_list}
        self.assertTrue(centers['H'])
        self.assertTrue(centers['L'])
        self.assertGreater(centers['H'][0][0], 1020)
        self.assertLess(centers['L'][0][0], 1000)

    def test_height_contours_have_visible_outlines_and_labels(self):
        renderer = OverlayRenderer()
        lon, lat = np.meshgrid(np.linspace(-120, -70, 80), np.linspace(25, 50, 60))
        values = 5200 + (lon + 120) * 15 + (lat-25) * 4
        field = {'data': values, 'lat': lat, 'lon': lon}
        fig, ax = renderer._make_fig_ax((25, 50, -120, -70), 800, 500)
        with patch.object(renderer, '_regrid', return_value=values), \
             patch.object(renderer, '_get_target_grid', return_value={'lon': lon, 'lat': lat}):
            renderer._draw_height_contours(ax, 'gh_500mb', field, (25, 50, -120, -70), 800, 500)
        self.assertTrue(ax.collections[0].get_path_effects())
        self.assertTrue(ax.texts)
        import matplotlib.pyplot as plt
        plt.close(fig)


class DownloadTests(unittest.TestCase):
    @staticmethod
    def scheduler_download():
        """Load just the download function without starting scheduler logging."""
        import ast
        from datetime import datetime
        from unittest.mock import Mock
        tree = ast.parse(Path('pipeline/unified_scheduler.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_download_fxx')
        namespace = {'__package__': 'pipeline', 'ModelConfig': object, 'Herbie': object,
                     'datetime': datetime, 'Path': Path, 'logger': Mock(),
                     'SOURCE_RACE_MODELS': set(), '_HEIGHT_OVERLAY_DEPENDENCIES': _SCALAR_HEIGHT_OVERLAYS}
        from pipeline.source_circuit import SourceCircuit
        namespace['SOURCE_CIRCUIT'] = SourceCircuit()
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<download>', 'exec'), namespace)
        return namespace

    def test_gdps_downloads_wind_components_and_contour_heights_at_all_lead_times(self):
        from datetime import datetime
        from pipeline.models.gdps import DERIVED_PARAMS, get_run_fxx
        from pipeline.models.gdps_downloader import _build_url
        ns = self.scheduler_download()
        model = SimpleNamespace(name='gdps', sources=[], rendered_params=['wind_700mb', 'temp_850mb'], derived=DERIVED_PARAMS)
        with patch('pipeline.models.gdps_downloader.download_forecast_hour') as download:
            for hour in [0, 123, 240]:
                ns['_download_fxx'](model, datetime(2026, 9, 30, 12), hour)
                self.assertEqual(set(download.call_args.args[2]), {'wind_u_700mb', 'wind_v_700mb', 'gh_700mb', 'temp_850mb', 'gh_850mb'})
        self.assertIn(240, get_run_fxx(12))
        self.assertEqual(_build_url(datetime(2026, 9, 30, 12), 3, 'HGT', 'ISBL', 850),
                         'https://dd.weather.gc.ca/20260930/WXO-DD/model_gdps/15km/12/003/20260930T12Z_MSC_GDPS_GeopotentialHeight_IsbL-0850_LatLon0.15_PT003H.grib2')

    def test_failed_source_is_excluded_from_grouped_fallback(self):
        from datetime import datetime
        ns = self.scheduler_download()
        calls = []
        def herbie(*, priority, **kwargs):
            calls.append(priority)
            if 'azure' in priority:
                raise KeyError('href')
            return SimpleNamespace(grib=None)
        ns['_get_herbie_cached'] = herbie
        ns['GRIB_CACHE_DIR'] = Path('/tmp')
        model = SimpleNamespace(name='test', sources=['nomads', 'azure', 'aws'], herbie_model='test',
                                herbie_product='test', herbie_kwargs={})
        self.assertIsNone(ns['_download_fxx'](model, datetime(2026, 9, 30), 0, _max_retries=1))
        self.assertEqual(calls, [['nomads'], ['azure'], ['aws'], ['aws']])

    def test_gdps_rejects_truncated_download_without_replacing_good_file(self):
        import io
        from pipeline.models.gdps_downloader import _download_file
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.grib2'
            path.write_bytes(b'original')
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'GRIB' + b'0' * 30)):
                self.assertFalse(_download_file('https://example.test/field', path))
            self.assertEqual(path.read_bytes(), b'original')
            self.assertFalse(path.with_suffix('.grib2.part').exists())

    def test_unrecognized_precip_shortname_uses_exact_wmo_identification(self):
        import eccodes
        from pipeline.processor import FieldProcessor
        message = eccodes.codes_grib_new_from_samples('regular_ll_sfc_grib2')
        try:
            for key, value in {'centre': 'cwao', 'discipline': 0, 'parameterCategory': 1,
                               'parameterNumber': 8, 'stepType': 'accum'}.items():
                eccodes.codes_set(message, key, value)
            candidates = FieldProcessor()._build_match_candidates(['precip'])[0][1]
            self.assertTrue(any(FieldProcessor()._msg_matches(message, criteria) for criteria in candidates))
            eccodes.codes_set(message, 'parameterNumber', 10)
            self.assertFalse(any(FieldProcessor()._msg_matches(message, criteria) for criteria in candidates))
        finally:
            eccodes.codes_release(message)


class WindRenderTests(unittest.TestCase):
    def test_wind_svg_keeps_symbols_vector_and_fill_self_contained(self):
        import xml.etree.ElementTree as ET
        renderer = OverlayRenderer()
        lon, lat = np.meshgrid(np.linspace(-110, -85, 16), np.linspace(30, 45, 12))
        speed = np.full(lon.shape, 23.0156, dtype=np.float32)
        speed[:2] = np.nan
        field = {'data': speed, 'lat': lat, 'lon': lon}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(renderer, '_get_mpl_resolution', return_value=(160, 120)), \
             patch.object(renderer, '_get_wind_layout', return_value={**renderer._get_wind_layout('wind_speed'), 'resolution_scale': 1}):
            output = Path(tmp) / 'f00.png'
            renderer._render_wind_overlay('wind_speed', field, field, field, output)
            with Image.open(output) as image:
                self.assertEqual(image.size, (160, 120))
            svg = ET.parse(output.with_suffix('.wind.svg')).getroot()
            ns = {'svg': 'http://www.w3.org/2000/svg'}
            images = svg.findall('.//svg:image', ns)
            self.assertEqual(len(images), 1)
            self.assertTrue(images[0].get('{http://www.w3.org/1999/xlink}href').startswith('data:image/png;base64,'))
            symbols = svg.findall('.//svg:g[@id="Barbs_2"]//svg:path', ns)
            self.assertTrue(symbols, 'Wind symbols must remain vector paths')
            self.assertIn('#edf6ff', ET.tostring(svg, encoding='unicode'))
            self.assertFalse(list(Path(tmp).glob('*.tmp')))

    def test_vector_availability_includes_only_published_wind_frames(self):
        with tempfile.TemporaryDirectory() as tmp, TestClient(api.app) as client:
            run = Path(tmp) / 'hrrr' / '2026101719'
            (run / 'wind_speed').mkdir(parents=True)
            (run / 'wind_speed' / 'f00.png').touch()
            (run / 'wind_speed' / 'f01.png').touch()
            (run / 'wind_speed' / 'f01.wind.svg').touch()
            (run / 'wind_speed' / '.f02.wind.svg.tmp').touch()
            (run / 'wind_speed' / 'fbad.wind.svg').touch()
            (run / 'mslp').mkdir()
            (run / 'mslp' / 'f00.contours.json').write_text('{}')
            with patch.object(api, 'RENDERED_DIR', Path(tmp)):
                response = client.get('/api/available/hrrr/2026101719/vectors')
            self.assertEqual(response.json(), {'mslp': [0], 'wind_speed': [1]})

    def test_wind_outside_domain_is_transparent_and_surface_barbs_use_knots(self):
        renderer = OverlayRenderer()
        n = 80
        lon, lat = np.meshgrid(np.linspace(-120, -70, n), np.linspace(25, 50, n))
        values = np.full((n, n), 23.0156)
        values[:15] = np.nan
        field = {'data': values, 'lat': lat, 'lon': lon}
        fig, ax = renderer._make_fig_ax((25, 50, -120, -70), n, n)
        with patch.object(renderer, '_get_mpl_resolution', return_value=(n, n)), \
             patch.object(renderer, '_get_wind_style', return_value={**renderer._get_wind_style('wind_speed'), 'resolution_scale': 1}), \
             patch.object(renderer, '_make_fig_ax', return_value=(fig, ax)), \
             patch.object(renderer, '_regrid', return_value=values), \
             patch.object(renderer, '_get_target_grid', return_value={'lon': lon, 'lat': lat}), \
             patch.object(renderer, '_rotate_to_earth_relative', side_effect=lambda lon, lat, tg, u, v: (u, v)) as rotate, \
             patch.object(renderer, '_write_wind_svg'), \
             patch.object(renderer, '_mpl_to_pil', return_value=True):
            renderer._render_wind_overlay('wind_speed', field, field, field, Path('/tmp/unused.png'))
        self.assertTrue(np.all(ax.images[0].get_array()[:10, :, 3] == 0))
        self.assertAlmostEqual(float(rotate.call_args.args[3][40, 40]), 20, places=4)
        import matplotlib.pyplot as plt
        plt.close(fig)

    def test_gust_barbs_point_along_10m_wind_at_gust_speed(self):
        renderer = OverlayRenderer()
        lat, lon = np.meshgrid(np.linspace(30, 40, 4), np.linspace(-100, -90, 4), indexing='ij')
        gust = np.full((4, 4), 50.0)
        u = np.full((4, 4), 18.0)
        v = np.full((4, 4), 24.0)          # sustained 30 mph from the SW
        u[0, 0] = v[0, 0] = 0.2            # near calm: no direction, no barb
        field = lambda data: {'data': data, 'lat': lat, 'lon': lon}
        with patch.object(renderer, '_render_wind_overlay', return_value=True) as wind:
            self.assertTrue(renderer._render_barb_product(
                'wind_gusts', field(gust), field(u), field(v), Path('/tmp/unused.png')))
        _, speed, bu, bv, _ = wind.call_args.args
        self.assertIs(speed['data'], gust)
        np.testing.assert_allclose(np.hypot(bu['data'], bv['data'])[1:, 1:], 50.0, rtol=1e-5)
        np.testing.assert_allclose((bu['data'] / bv['data'])[1:, 1:], 0.75, rtol=1e-5)
        self.assertTrue(np.isnan(bu['data'][0, 0]) and np.isnan(bv['data'][0, 0]))

    def test_max_wind_barbs_use_their_own_components_and_fall_back_to_shading(self):
        renderer = OverlayRenderer()
        lat, lon = np.meshgrid(np.linspace(30, 40, 4), np.linspace(-100, -90, 4), indexing='ij')
        f = lambda data: {'data': data, 'lat': lat, 'lon': lon}
        speed, u, v = f(np.full((4, 4), 40.0)), f(np.full((4, 4), 24.0)), f(np.full((4, 4), 32.0))
        with patch.object(renderer, '_render_wind_overlay', return_value=True) as wind:
            renderer._render_barb_product('max_wind', speed, u, v, Path('/tmp/unused.png'))
        self.assertIs(wind.call_args.args[2], u)
        with patch.object(renderer, '_render_wind_overlay', side_effect=RuntimeError('boom')), \
             patch.object(renderer, 'render_overlay', return_value=True) as shading:
            self.assertTrue(renderer._render_barb_product('max_wind', speed, u, v, Path('/tmp/unused.png')))
        shading.assert_called_once()

    def test_barb_products_publish_wind_svg_and_split_workers_get_components(self):
        from pipeline.unified_scheduler import _expand_chunk_fields
        keys = {'wind_gusts', 'ugrd_10m', 'vgrd_10m', 'max_wind', 'maxuw_10m', 'maxvw_10m', 'temp_2m'}
        self.assertEqual(_expand_chunk_fields(['wind_gusts'], keys), {'wind_gusts', 'ugrd_10m', 'vgrd_10m'})
        self.assertEqual(_expand_chunk_fields(['max_wind'], keys), {'max_wind', 'maxuw_10m', 'maxvw_10m'})
        with tempfile.TemporaryDirectory() as tmp, TestClient(api.app) as client:
            run = Path(tmp) / 'hrrr' / '2026101719'
            for param in ('wind_gusts', 'max_wind'):
                (run / param).mkdir(parents=True)
                (run / param / 'f03.png').touch()
                (run / param / 'f03.wind.svg').touch()
            with patch.object(api, 'RENDERED_DIR', Path(tmp)):
                response = client.get('/api/available/hrrr/2026101719/vectors')
            self.assertEqual(response.json(), {'wind_gusts': [3], 'max_wind': [3]})


class PrecipAccumulationTests(unittest.TestCase):
    def test_12h_precip_is_accumulation_since_12_hours_earlier(self):
        from pipeline.processor import FieldProcessor, SNOW_PRODUCTS
        self.assertEqual(SNOW_PRODUCTS['precip_12h'], {'hours': 12, 'method': 'liquid'})
        proc = FieldProcessor()
        lat = lon = np.zeros((2, 2))
        result = {'_precip_meta': {'lat': lat, 'lon': lon, 'valid_time': None}}
        current = np.array([[3.0, 1.0], [0.5, 0.0]])
        earlier = np.array([[1.0, 1.0], [0.6, 0.0]])
        with patch.object(proc, '_load_raw_precip', return_value=earlier) as load, \
             patch.object(proc, 'load_field', return_value=None):
            proc._compute_snow_products(result, Path('f18.grib2'), 18, {6: Path('f06.grib2')},
                                        accum_precip_inches=current)
        load.assert_called_with(Path('f06.grib2'))
        np.testing.assert_allclose(result['precip_12h']['data'], [[2.0, 0.0], [0.0, 0.0]])
        self.assertEqual(api._RENDER_UNDER_VMIN['precip_12h'], 0.01)


class PrecipTypeTests(unittest.TestCase):
    def test_encoding_priority_light_precip_and_virga(self):
        from pipeline.processor import encode_precip_type
        refl = np.array([[30.0, 30.0, 2.0, 40.0, 30.0]])
        rain = np.array([[1, 1, 1, 0, 0]])
        snow = np.array([[0, 1, 0, 0, 0]])
        sleet = np.array([[0, 0, 0, 0, 0]])
        fzra = np.array([[0, 0, 0, 1, 0]])
        out = encode_precip_type(refl, rain, snow, sleet, fzra)
        # rain 30 dBZ -> step 5; rain+snow -> snow; 2 dBZ rain -> lightest rain step;
        # freezing rain 40 dBZ -> 36 + 7; reflectivity without a type -> hidden
        np.testing.assert_allclose(out[0, :4], [5.5, 17.5, 0.5, 43.5])
        self.assertTrue(np.isnan(out[0, 4]))

    def test_legend_labels_and_hover_text(self):
        with TestClient(api.app) as client:
            data = client.get('/api/legend/precip_type').json()
        self.assertEqual(data['tick_labels'], ['Rain', 'Snow', 'Sleet', 'Freezing Rain'])
        self.assertEqual(len(data['colors']), 48)
        self.assertEqual(len({tuple(c[:3]) for c in data['color_lookup']}), 48)
        cats = data['categories']
        self.assertEqual(api._describe_category_value(cats, 17.5, 'dBZ'), 'Snow · 30–35 dBZ')
        self.assertEqual(api._describe_category_value(cats, 0.5, 'dBZ'), 'Rain · ≤10 dBZ')
        self.assertEqual(api._describe_category_value(cats, 47.9, ''), 'Freezing Rain · 60–65')
        self.assertIsNone(api._describe_category_value(None, 5.0))

    def test_radar_uses_newest_hrrr_grid_and_maps_gates(self):
        from datetime import datetime, timezone
        from pipeline.radar import ptype
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Snow north of 40N, rain south of it, on the grid's Mercator rows.
            merc = lambda lat: np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
            rows = (np.arange(100) + 0.5) / 100
            row_lat = np.degrees(2 * np.arctan(np.exp(merc(50) - rows * (merc(50) - merc(30)))) - np.pi / 2)
            codes = np.repeat((row_lat > 40.0)[:, None], 100, axis=1).astype(np.uint8)
            for run, lead in (('2026121810', 2), ('2026121811', 1)):
                path = root / 'hrrr' / run / 'ptype_grid' / f'f{lead:02d}.npz'
                path.parent.mkdir(parents=True)
                np.savez_compressed(path, codes=codes, bounds=np.array([30.0, 50.0, -110.0, -90.0]))
            scan = datetime(2026, 12, 18, 12, 10, tzinfo=timezone.utc)
            found = ptype.find_grid(scan, root)
            self.assertEqual(found.parent.parent.name, '2026121811')
            grid, bounds = ptype.load_grid(found)
            kinds = ptype.gate_types(40.0, -100.0, np.array([0.0, 180.0]), 10, 200.0, 0.5, grid, bounds)
            self.assertTrue(np.all(kinds[0, 2:] == 1))   # due north of a mid-grid radar: snow
            self.assertTrue(np.all(kinds[1, 2:] == 0))   # due south: rain
            self.assertIsNone(ptype.find_grid(datetime(2026, 12, 19, 12, 0, tzinfo=timezone.utc), root))

if __name__ == '__main__':
    unittest.main()


class MeteogramTests(unittest.TestCase):
    """Point forecasts read values back from rendered overlays."""

    def test_png_reverse_lookup_matches_renderer_colormaps(self):
        renderer = OverlayRenderer.__new__(OverlayRenderer)
        for param, scale in api.COLOR_SCALES.items():
            cmap_name = scale.get('cmap')
            # Radar and satellite scales are painted by their own renderers.
            if param.startswith(('radar_', 'goes_')):
                continue
            if not cmap_name or cmap_name in ('discrete', 'nws_refl') or scale.get('bounds'):
                continue
            with self.subTest(param=param):
                cmap, norm = renderer._make_cmap_norm(param)
                painted = cmap(np.arange(256), bytes=True)[:, :3]
                lookup = api._build_cmap_lookup(cmap_name, scale['vmin'], scale['vmax'], param)
                self.assertEqual(set(lookup), {tuple(int(c) for c in rgb) for rgb in painted})
                # Values must invert the renderer's normalization.
                index = 200
                expected = norm.inverse((index + 0.5) / 256.0)
                value = lookup[tuple(int(c) for c in painted[index])]
                step = (norm.vmax - norm.vmin) / 256.0
                self.assertAlmostEqual(value, float(expected), delta=step * 2)

    def test_grid_cache_round_trips_rendered_values(self):
        from web import meteogram

        renderer = OverlayRenderer.__new__(OverlayRenderer)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / 'rendered' / 'hrrr' / '2026101600'
            bounds = {'south': 30.0, 'north': 40.0, 'west': -100.0, 'east': -90.0}
            run_dir.mkdir(parents=True)
            (run_dir / 'bounds.json').write_text(json.dumps(bounds))
            # 1500 px wide forces a stride; left half 50 °F, right half 70 °F.
            data = np.full((900, 1500), 50.0, dtype=np.float32)
            data[:, 750:] = 70.0
            cmap, norm = renderer._make_cmap_norm('temp_2m')
            (run_dir / 'temp_2m').mkdir()
            for fxx in (0, 1):
                Image.fromarray(cmap(norm(data + fxx), bytes=True), 'RGBA').save(run_dir / 'temp_2m' / f'f{fxx:02d}.png')
            # A transparent precip frame means "below the lowest bin" (0 in).
            (run_dir / 'precip').mkdir()
            Image.new('RGBA', (1500, 900), (0, 0, 0, 0)).save(run_dir / 'precip' / 'f01.png')

            with patch.object(meteogram, 'RENDERED_DIR', root / 'rendered'), \
                    patch.object(meteogram, 'CACHE_DIR', root / 'cache'):
                self.assertTrue(meteogram.needs_build('hrrr', '2026101600'))
                built = meteogram.build_run('hrrr', '2026101600', 3, api._rgb_to_value)
                self.assertEqual(built, 3)
                self.assertFalse(meteogram.needs_build('hrrr', '2026101600'))
                west = meteogram.sample_run('hrrr', '2026101600', 35.0, -97.5, None)
                east = meteogram.sample_run('hrrr', '2026101600', 35.0, -92.5, None)
                self.assertFalse(meteogram.in_domain('hrrr', '2026101600', 45.0, -95.0))

        self.assertEqual(west['init_time'], '2026-10-16T00:00:00Z')
        temps = dict(west['series']['temp_2m'])
        self.assertAlmostEqual(temps[0], 50.0, delta=0.5)
        self.assertAlmostEqual(temps[1], 51.0, delta=0.5)
        self.assertAlmostEqual(dict(east['series']['temp_2m'])[0], 70.0, delta=0.5)
        self.assertEqual(west['series']['precip'], [[1, 0.0]])

    def test_run_selection_prefers_newest_complete_recent_run(self):
        from datetime import datetime, timedelta, timezone
        from web import meteogram

        now = datetime.now(timezone.utc)
        run_id = lambda hours: (now - timedelta(hours=hours)).strftime('%Y%m%d%H')
        runs = [
            {'run_id': run_id(1), 'is_complete': False, 'completed_fxx': [0, 1]},
            {'run_id': run_id(2), 'is_complete': True, 'completed_fxx': list(range(19))},
            {'run_id': run_id(60), 'is_complete': True, 'completed_fxx': list(range(49))},
        ]
        self.assertEqual(meteogram.select_run(runs)['run_id'], run_id(2))
        self.assertEqual(meteogram.select_run(runs[:1])['run_id'], run_id(1))
        self.assertIsNone(meteogram.select_run(runs[2:]))


class DashboardWorkerStatusTests(unittest.TestCase):
    def test_busy_worker_without_status_file_is_not_idle(self):
        watcher = api._WorkerTaskWatcher()
        with patch.object(api, '_worker_task_watcher', watcher):
            busy = api._idle_worker_fields(4242, 80.0)
            idle = api._idle_worker_fields(4242, 2.0)
        self.assertEqual((busy['status'], busy['status_class']), ('Busy', 'processing'))
        self.assertEqual((idle['status'], idle['status_class'], idle['task']), ('Idle', 'idle', '--'))

    def test_watcher_keeps_short_radar_task_after_status_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(api, 'DATA_DIR', Path(tmp)):
            status_dir = Path(tmp) / 'radar_worker_status'
            status_dir.mkdir()
            status_path = status_dir / '4242.json'
            status_path.write_text(json.dumps({'site': 'KOAX', 'timestamp': '20261017_1600', 'source': 'full'}))
            watcher = api._WorkerTaskWatcher()
            watcher._current = watcher._scan()
            status_path.unlink()
            with patch.object(api, '_worker_task_watcher', watcher), \
                    patch.object(watcher, '_scan', return_value={}):
                watcher._active_until = float('inf')
                with patch.object(api.time, 'sleep', side_effect=[None, StopIteration]):
                    with self.assertRaises(StopIteration):
                        watcher._run()
                fields = api._idle_worker_fields(4242, 1.0)
        self.assertEqual(fields['status'], 'Idle')
        self.assertTrue(fields['task'].startswith('Last: KOAX 20261017_1600 FULL'), fields['task'])

    def test_dashboard_live_sections_refresh_inside_cached_payload(self):
        cached = {'processes': [], 'models': ['cached'], 'log_entries': [], 'system': {}}
        with patch.dict(api._dashboard_cache, {'data': cached, 'ts': 0, 'live_ts': 0}), \
                patch.object(api, '_live_dashboard_sections', return_value={'processes': [{'pid': 1}]}):
            payload = api._refresh_dashboard_live()
        self.assertEqual(payload['processes'], [{'pid': 1}])
        self.assertEqual(payload['models'], ['cached'])
