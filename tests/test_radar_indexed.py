"""Indexed radar PNGs preserve exact colors, alpha, and value queries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from pipeline.radar import renderer
from pipeline.radar.color_tables import FIELD_CONFIG, RADAR_PALETTE_VERSION


class IndexedRadarTests(unittest.TestCase):
    def test_exact_palette_and_transparency_round_trip_for_every_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            for field,cfg in FIELD_CONFIG.items():
                with self.subTest(field=field):
                    lut,_,_=cfg['lut_fn']()
                    indices=np.resize(np.arange(16,220,dtype=np.uint8),(32,32))
                    valid=np.ones(indices.shape,dtype=bool)
                    valid[::3]=False
                    expected=lut[indices]
                    expected[~valid]=0
                    image=renderer._indexed_radar_image(indices.copy(),lut,valid)
                    self.assertIsNotNone(image)
                    self.assertEqual(image.mode,'P')
                    path=Path(tmp)/(field+'.png')
                    renderer._atomic_write_png(image,path)
                    with Image.open(path) as result:
                        np.testing.assert_array_equal(np.asarray(result.convert('RGBA')),expected)

    def test_all_256_opaque_colors_plus_missing_pixels_falls_back_exactly(self):
        lut=np.zeros((256,4),dtype=np.uint8)
        lut[:,0]=np.arange(256)
        lut[:,1]=80
        lut[:,3]=255
        data=np.arange(257,dtype=np.float32).reshape(1,257)
        valid=np.ones(data.shape,dtype=bool);valid[0,-1]=False
        az_idx=np.zeros(data.shape,dtype=np.uint16)
        gates=np.minimum(np.arange(257),255).reshape(data.shape)
        with patch.object(renderer,'build_polar_lookup',return_value=(az_idx,gates,valid)):
            a=renderer.render_sweep_to_png(data,np.array([0]),lut,0,255,None,indexed=False)
            b=renderer.render_sweep_to_png(data,np.array([0]),lut,0,255,None,indexed=True)
        self.assertEqual(b.mode,'RGBA')
        np.testing.assert_array_equal(np.asarray(a),np.asarray(b))

    def test_existing_transparent_entry_preserves_all_other_palette_entries(self):
        lut=np.zeros((256,4),dtype=np.uint8)
        lut[:,0]=np.arange(256)
        lut[:,3]=255
        lut[0]=0
        indices=np.resize(np.arange(256,dtype=np.uint8),(32,32))
        valid=np.ones(indices.shape,dtype=bool);valid[0]=False
        expected=lut[indices];expected[~valid]=0
        image=renderer._indexed_radar_image(indices.copy(),lut,valid)
        np.testing.assert_array_equal(np.asarray(image.convert('RGBA')),expected)

    def test_missing_and_nonfinite_data_and_clutter_match_rgba(self):
        for field,cfg in FIELD_CONFIG.items():
            lut,vmin,vmax=cfg['lut_fn']()
            rng=np.random.default_rng(4)
            data=rng.uniform(vmin-1,vmax+1,(8,20)).astype(np.float32)
            data[0,:]=np.nan;data[1,0]=np.inf;data[2,0]=-np.inf
            cc=np.full(data.shape,.9,dtype=np.float32);cc[3]=.1
            with np.errstate(invalid='ignore'):
                a=renderer.render_sweep_to_png(data,np.arange(8)*45,lut,vmin,vmax,cfg['mask_below'],output_size=64,cc_data=cc)
                b=renderer.render_sweep_to_png(data,np.arange(8)*45,lut,vmin,vmax,cfg['mask_below'],output_size=64,cc_data=cc,indexed=True)
            np.testing.assert_array_equal(np.asarray(a),np.asarray(b.convert('RGBA')),err_msg=field)

    def test_hover_value_is_identical_for_indexed_and_rgba_images(self):
        from fastapi.testclient import TestClient
        from web import app as api
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'png').mkdir();(root/'meta').mkdir()
            lut,_,_=FIELD_CONFIG['reflectivity']['lut_fn']()
            image=renderer._indexed_radar_image(np.full((10,10),160,dtype=np.uint8),lut,np.ones((10,10),dtype=bool))
            for timestamp,encoded in [('20261014_140000',image),('20261014_140001',image.convert('RGBA'))]:
                stem=f'KOAX_REF_0.5_{timestamp}'
                encoded.save(root/'png'/(stem+'.png'))
                (root/'meta'/(stem+'.json')).write_text(json.dumps({'site':'KOAX','field':'reflectivity','short':'REF','tilt':.5,'timestamp':timestamp,'palette_version':RADAR_PALETTE_VERSION,'bounds':{'south':30,'north':40,'west':-100,'east':-90,'center_lat':35,'center_lon':-95,'max_range_km':460},'png_path':f'radar/png/{stem}.png'}))
            with patch.object(api,'RADAR_RENDERED_DIR',root),TestClient(api.app) as client:
                a=client.get('/api/value/radar/KOAX/REF?lat=35&lon=-95&frame=20261014_140000')
                b=client.get('/api/value/radar/KOAX/REF?lat=35&lon=-95&frame=20261014_140001')
            self.assertEqual(a.status_code,200)
            self.assertEqual(b.status_code,200)
            self.assertEqual(a.json(),b.json())

if __name__=='__main__':unittest.main()
