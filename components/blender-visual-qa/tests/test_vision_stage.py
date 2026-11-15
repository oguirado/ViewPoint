import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from blender_visual_qa import vision_stage as vs
from blender_visual_qa.oracles import vision


class StageTests(unittest.TestCase):
    def test_pin_and_adapter_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); wheel=d/'engine.whl'; cfg=d/'vision.json'
            def config(sha):
                cfg.write_text(json.dumps({'engine_path':str(wheel),'pin':{
                    'wheel_sha256':sha,'commit':'frozen'}}))
            with zipfile.ZipFile(wheel,'w') as z: z.writestr('vadapt/blender/from_render_dir.py','')
            sha=hashlib.sha256(wheel.read_bytes()).hexdigest(); config(sha)
            self.assertEqual(vs.pinned_wheel(cfg,'frozen')[0],wheel)
            with self.assertRaises(vs.StageError): vs.pinned_wheel(cfg,'other')
            config('0'*64)
            with self.assertRaises(vs.StageError): vs.pinned_wheel(cfg)
            with zipfile.ZipFile(wheel,'w') as z: z.writestr('vadapt/godot.py','')
            config(hashlib.sha256(wheel.read_bytes()).hexdigest())
            with self.assertRaises(vs.StageError): vs.pinned_wheel(cfg)

    def test_off_does_not_require_engine(self):
        self.assertEqual(vs.run_stage('unused',vision='off',config='missing')['status'],'SKIPPED')

    def test_shadow_sidecars_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            v=Path(d)/'vision'; v.mkdir()
            frames=b'{"frame":0}\n'
            regions=b'{"frame":0,"cls":"DRIFT","n_tiles":2}\n'
            data={'schema':'vqa.vision/v1','files':{}}
            for name,raw in [('frames.jsonl',frames),('regions.jsonl',regions)]:
                (v/name).write_bytes(raw); data['files'][name]=hashlib.sha256(raw).hexdigest()
            (v/'vision.json').write_text(json.dumps(data))
            result=vision.evaluate_vision(d,'vc_slow_drift',mode='shadow')
            self.assertEqual(result[0],vision.FAIL)
            self.assertEqual(vision.effective_verdict(result),'ADVISORY')
            (v/'regions.jsonl').write_bytes(regions+b' ')
            self.assertEqual(vision.evaluate_vision(d,'vc_slow_drift')[0],vision.CANNOT_SEE)

    def test_bool_and_invalid_profile_cannot_trigger(self):
        data={'regions':[{'frame':True,'n_tiles':True,'cls':'DRIFT'}]}
        self.assertEqual(vision.vc_slow_drift(data)[0],vision.PASS)
        with self.assertRaises(vision.VisionError): vision.vc_slow_drift(data,thresholds={'DRIFT':0})
