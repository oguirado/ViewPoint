import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from bpy_side import decode_exr as dx
from blender_visual_qa.truth import decode_worker as dw
from tests.test_exr_none import fixture


class DecodeWorkerTests(unittest.TestCase):
    def test_quantisers_and_publication_match_reference(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            paths={}
            for key in ('ob','ma','mv','z','n','uv'):
                p=d/(key+'.exr'); fixture(p); paths[key]=str(p)
            row={'schema':'bvqa.truth-pending/v1','exr_codec':'NONE','frame':1,'frame_line':{'schema':'bvqa.truth-frame/v1','render_index':1,
                 'width':2,'height':2,'grid':'fixture'},'passes':paths,'valid_ob':[20,21],'valid_ma':[20,21]}
            pending=d/'pending.jsonl'; pending.write_text(json.dumps(row)+'\n')
            # The same RGBA samples passed through the original Blender quantisers.
            refs={}
            for key,p in paths.items():
                rgba=dw.exr_none.load_rgba(p)
                refs[key]=(dx.quant_index(rgba,[20,21],key) if key in ('ob','ma') else
                           {'mv':dx.quant_motion,'z':dx.quant_depth,'n':dx.quant_normal,'uv':dx.quant_uv}[key](rgba))
            refdir=d/'reference'; refdir.mkdir()
            shas=dx.write_frame(refs,str(refdir),1)
            dw.consume(pending,d/'truth',pin=False)
            record=json.loads((d/'truth/frames.jsonl').read_text())
            self.assertEqual(record['files'],shas)
            self.assertEqual(record['grid'],'fixture')
            self.assertFalse(pending.exists())
            self.assertTrue(all(not Path(p).exists() for p in paths.values()))

    def test_bad_wave_preserves_sources_and_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); p=d/'pending.jsonl'
            p.write_text('')
            with self.assertRaises(ValueError): dw.consume(p,d/'truth',pin=False)
            self.assertFalse((d/'truth/frames.jsonl').exists())
            self.assertTrue(p.exists())
            p.write_text(json.dumps({'frame':1})+'\n'+json.dumps({'frame':1})+'\n')
            with self.assertRaises(ValueError): dw.consume(p,d/'truth',pin=False)

    def test_decode_affinity_excludes_reserved_cores(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'pending.jsonl'; p.write_text('')
            with patch.object(dw.os,'sched_getaffinity',return_value={5,6}), patch.object(dw.os,'sched_setaffinity') as setaff:
                with self.assertRaisesRegex(ValueError,'E_AFFINITY'): dw.consume(p,Path(d)/'truth')
                setaff.assert_not_called()
