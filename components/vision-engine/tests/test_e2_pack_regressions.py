"""W3 pack failing-first regressions; fixtures use actual captured timelines."""
import unittest
from pathlib import Path
import tempfile
import numpy as np
from visioncore import pack, ids, layout
from visioncore.desc import TILE_DTYPE
from visioncore.errors import VqaError


def gop(frames, events=(), tracks=()):
    return {'g':0, 'f0':frames[0], 'n_frames':len(frames), 'frame_ids':list(frames),
            'dirty_ppm':[1000]*len(frames), 'events':list(events), 'tracks':list(tracks), 'cycles':[], 'cut':False}


def batch(g, **kw):
    return dict(W=32,H=32,params_sha256='a'*64,gops=[g],regions={}, **kw)


class PackSealTests(unittest.TestCase):
    def test_jc4_sparse_media_and_crop_predecessor(self):
        captured = [0,5,10,15]
        tr = {'k':'k5:0000','r_rep':'r10:0000','first':5,'last':15,'cls_mask':4,
              'flags':['APPEAR','VANISH'], 'mv_mode':[0,0], 'n_tiles_max':1, 'period':0}
        g = gop(captured, tracks=[tr])
        b = batch(g)
        b['regions'] = {'r10:0000':{'id':'r10:0000','frame':10,'cls':'CHANGED',
                                  'bbox_t':[0,0,0,0], 'bbox_px':[0,0,16,16]}}
        tile_calls, frame_calls = [], []
        def tiles(f):
            self.assertIn(f, captured)
            tile_calls.append(f)
            a = np.zeros(4, dtype=TILE_DTYPE)
            a['morton'] = [0,1,2,3]
            return a
        def frames(f):
            self.assertIn(f, captured)
            frame_calls.append(f)
            return np.zeros((32,32,3),dtype=np.uint8)
        b.update(tiles=tiles, frames=frames)
        pr = pack.pack(b)
        self.assertEqual([x[1] for x in pr.pack['frame_map']], captured)
        self.assertEqual(tile_calls, captured)
        self.assertEqual(frame_calls,[5,10])
        self.assertEqual(pr.pack['crop_frames'],[5,10])

    def test_jc5_mandatory_overflow_refuses_without_writes(self):
        with tempfile.TemporaryDirectory() as d:
            guard = Path(d)/'guard'
            guard.write_bytes(b'unchanged')
            g = gop(list(range(15)), [('FLASH',ids.global_id(i)) for i in range(15)])
            with self.assertRaises(VqaError) as cm:
                pack.pack(batch(g)).write(Path(d)/'out')
            self.assertEqual((cm.exception.code,cm.exception.rc),('E_BUDGET',6))
            self.assertEqual(list(Path(d).iterdir()),[guard])
            self.assertEqual(guard.read_bytes(),b'unchanged')

    def test_jc5_exact_cap_and_dedup(self):
        ev = [('FLASH',ids.global_id(i)) for i in range(12)]*2
        pr = pack.pack(batch(gop(list(range(12)),ev)))
        self.assertEqual([x[1] for x in pr.pack['frame_map']],list(range(12)))

    def test_adv13_call_cap_after_ladder(self):
        with self.assertRaises(VqaError) as cm:
            pack.pack(batch(gop([0]),text_b=64000))
        self.assertEqual((cm.exception.code,cm.exception.rc),('E_BUDGET',6))
        self.assertLessEqual(pack.pack(batch(gop([0]),text_b=1000)).pack['est_tok'],15500)


    def test_adv13_run_and_retry_allowances(self):
        budget = pack.RunTokenBudget()
        for _ in range(4):
            budget.charge(15500)
        self.assertEqual(budget.tokens,62000)
        state = vars(budget).copy()
        with self.assertRaises(VqaError) as cm:
            budget.charge(1)
        self.assertEqual(cm.exception.rc,6)
        self.assertEqual(vars(budget),state)
        for _ in range(2):
            budget.charge(15500,retry=True)
        self.assertEqual(budget.tokens,93000)
        with self.assertRaises(VqaError):
            budget.charge(1,retry=True)

    def test_adv13_refuse_before_judge_or_cache_write(self):
        with tempfile.TemporaryDirectory() as d:
            pr = pack.pack(batch(gop([0])))
            cache = pack.ResponseCache(d)
            calls = []
            with self.assertRaises(VqaError) as cm:
                pack.judge_batch(pr, lambda req: calls.append(req) or b'{}', cache, b'x'*62000)
            self.assertEqual(cm.exception.rc,6)
            self.assertEqual(calls,[])
            self.assertEqual(list(Path(d).iterdir()),[])
            budget = pack.RunTokenBudget()
            budget.charge(15500)
            budget.charge(15500)
            budget.charge(15500)
            budget.charge(15500)
            with self.assertRaises(VqaError):
                pack.judge_batch(pr, lambda req: calls.append(req) or b'{}', cache, budget=budget)
            self.assertEqual(calls,[])
            self.assertEqual(list(Path(d).iterdir()),[])

    def test_jc5_select_overflow_refuses(self):
        with self.assertRaises(VqaError) as cm:
            pack.select_frames([gop(list(range(15)),[('FLASH',ids.global_id(i)) for i in range(15)])])
        self.assertEqual(cm.exception.rc,6)

    def test_jc4_gui_sparse_analyze_pack_verify(self):
        from vadapt.gui import GuiAdapter
        from visioncore import canon, cite, pipeline, API_VERSION
        captured = [0,5,10,15]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'screens').mkdir()
            rows = []
            for f in captured:
                name = str(f)+'.png'
                (root/'screens'/name).write_bytes(pack.png_encode(np.zeros((32,32,3),dtype=np.uint8)))
                rows.append({'file':name,'step':f})
            (root/'steps.jsonl').write_text(''.join(canon.dumps(r) for r in rows))
            run = pipeline.analyze(GuiAdapter().frames(root))
            gs = run.gops()
            self.assertEqual([f for g in gs for f in g['frame_ids']], captured)
            tile_map = dict(zip(captured,run.tiles))
            pr = pack.pack({'W':32,'H':32,'params_sha256':run.params_sha256,'gops':gs,
                            'regions':[r for rs in run.regions for r in rs], 'tiles':tile_map.__getitem__})
            self.assertEqual([x[1] for x in pr.pack['frame_map']],captured)
            v = cite.build_vision(32,32,b''.join(t.tobytes() for t in run.tiles),run.frame_rows,
                                  [r for rs in run.regions for r in rs],gs,run.params_sha256,API_VERSION)
            pr.pack['api_version'] = API_VERSION
            good = {'check_id':'legacy.text','status':'FAIL','reason_class':'static_content','pointers':['f15:t0000']}
            self.assertEqual(cite.verify_citations(v,{'results':[good]},pr.pack)['rc'],0)
            good['pointers'] = ['f1:t0000']
            self.assertEqual(cite.verify_citations(v,{'results':[good]},pr.pack)['rc'],3)


    def test_adv13_negative_text_cannot_cancel_media_cost(self):
        for bad in (-1, True, '1000'):
            with self.subTest(bad=bad):
                with self.assertRaises(VqaError) as cm:
                    pack.pack(batch(gop([0]),text_b=bad))
                self.assertEqual(cm.exception.rc,2)


    def test_adv13_dispatch_requires_ledger_cache_hits_are_free(self):
        with tempfile.TemporaryDirectory() as d:
            pr = pack.pack(batch(gop([0])))
            cache = pack.ResponseCache(d)
            calls = []
            def call(req):
                calls.append(req)
                return b'{}'
            with self.assertRaises(VqaError) as cm:
                pack.judge_batch(pr,call,cache)
            self.assertEqual(cm.exception.rc,2)
            self.assertEqual(calls,[])
            budget = pack.RunTokenBudget()
            response, receipt = pack.judge_batch(pr,call,cache,budget=budget)
            state = vars(budget).copy()
            cached, hit = pack.judge_batch(pr,call,cache,budget=budget)
            self.assertEqual(cached,response)
            self.assertEqual(hit['cache'],'hit')
            self.assertEqual(vars(budget),state)
            self.assertEqual(len(calls),1)
