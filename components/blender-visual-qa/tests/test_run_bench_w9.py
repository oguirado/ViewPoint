import unittest
import tempfile
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from blender_visual_qa.truth import run_bench as rb


class SeedTests(unittest.TestCase):
    def test_seed_sets(self):
        self.assertEqual(rb.parse_seeds('fit:0-1,gate:10-11'), {'fit':[0,1], 'gate':[10,11]})
        for bad in ('fit:0-1,gate:1-2', 'fit:0-1', 'fit:-1,gate:2', 'fit:2-1,gate:3', 'fit:0,fit:1,gate:3'):
            with self.subTest(bad=bad), self.assertRaises(rb.BenchError): rb.parse_seeds(bad)

    def test_golden_and_additive_mutant(self):
        triples = [(0,0,0),(0,1,0),(0,1,1),(0,17,1),(0,17,5),(0,17,9),(1,0,0),(1,1,1),
                   (16777215,1048575,1048575),(7,31,99)]
        golden = [0,1371419539,363264740,904720292,360431674,14156008,1824779127,598127744,
                  943046336,1022607159]
        for coords, want in zip(triples,golden):
            self.assertEqual(rb.cycles_seed(*coords),want)
        self.assertEqual(rb.beauty_seed('B1',0,0,'per_frame'),1824779127)
        self.assertNotEqual(rb.beauty_seed('B1',0,1,'per_frame'),rb.beauty_seed('B1',1,0,'per_frame'))
        self.assertEqual(rb.beauty_seed('B1',2,3,'fixed'),rb.beauty_seed('B1',2,0,'fixed'))

    def test_seedadd_negative_is_rejected_by_collision_gate(self):
        with patch.object(rb,'cycles_seed',side_effect=lambda b,k,ri:1000*b+k+ri):
            with self.assertRaises(rb.BenchError) as ctx:
                rb.check_seed_collisions(['B1'],[0,1],8,per_frame={'B1'})
        self.assertEqual(ctx.exception.code,'E_TRUTH_ARGS')
        self.assertIn('collision',str(ctx.exception))

    def test_seed_expression_uses_canonical_function(self):
        expr=rb.seed_expression('B1',7,'per_frame',4)
        self.assertIn('cycles_seed',expr)
        self.assertNotIn('+sc.frame_current',expr)
        scene=SimpleNamespace(cycles=SimpleNamespace(),frame_current=1)
        handlers=[]
        bpy=SimpleNamespace(context=SimpleNamespace(scene=scene),app=SimpleNamespace(handlers=SimpleNamespace(render_pre=handlers)))
        with patch.dict(sys.modules,{'bpy':bpy}), patch.object(sys,'path',list(sys.path)):
            exec(expr,{})
            self.assertEqual(scene.cycles.seed,rb.cycles_seed(rb.BENCH_NO['B1'],7,0))
            handlers[0](scene)
            self.assertEqual(scene.cycles.seed,rb.cycles_seed(rb.BENCH_NO['B1'],7,1))

    def test_host_job_limit(self):
        with self.assertRaises(rb.BenchError):
            rb.validate(rb.parse_args(['--out','unused','--jobs','4','--cpu-plan','w9']))
        for cpus in rb.JOB_CPUS:
            self.assertNotIn('5-9',cpus)

    def test_engine_errors_grid_translated(self):
        from visioncore.errors import VqaError
        with patch.object(rb,'vision_analyze',side_effect=VqaError('E_VC_GRID_MISMATCH',{})):
            with self.assertRaises(rb.BenchError) as ctx:
                rb.analyze_bench('B1','unused',4,{})
        self.assertEqual(ctx.exception.code,'E_STALE')
        self.assertIn('grid',str(ctx.exception))


class ProcessTests(unittest.TestCase):
    def test_timeout_reaps_owned_process_group(self):
        rb._STOP.clear()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(rb.BenchError) as ctx:
                rb.run_blender([sys.executable, '-c', 'import time;time.sleep(10)'], d,
                               Path(d)/'process.log', timeout=.1)
        self.assertEqual(ctx.exception.code,'E_TIMEOUT')
        self.assertEqual(rb._ACTIVE,set())


class RunnerPartitionTests(unittest.TestCase):
    def result(self, bench, seed):
        return {'bench':bench,'seed':seed,'grid':'test','frames':2,'cpu_ms_per_frame':0,
                'row':{'bench':bench,'seed':seed},'still_q4':[100+seed],
                'metrics':{'tl':{},'dirty_precision_ppm':None,'jitter_fp_ppm':None,
                           'mv_exact_ppm':None,'mv_err_p95_px':None,'class_precision_ppm':{}}}

    def test_scheduler_preserves_logical_arms_and_fit_only_probe(self):
        import json
        captured={}
        analyzed=[]
        def fake_run(bench, root, w, h, frames, spp, table, seed=0, analyze=True, scene_seed=None):
            r=self.result(bench,seed)
            if not analyze:
                return {'bench':bench,'seed':seed,'run_dir':'unused'}
            r['run_dir']='unused'
            return r
        def fake_analyze(bench, run, spp, table, t_wall=None, seed=0, clean=None):
            analyzed.append((seed,int(table['T0_Q4'])))
            r=self.result(bench,seed); r.pop('still_q4'); return r
        def calibrate(rows, samples, adapter, renderer, grid, source, strict=False, seeds=None, still_meta=None):
            captured.update(rows=rows,samples=samples,seeds=seeds,still_meta=still_meta)
            return None, {'T0_Q4':rb.vparams.effective(None)['T0_Q4'],'checks':[],'passed':False}
        with tempfile.TemporaryDirectory() as d, patch.object(rb,'run_one',side_effect=fake_run), patch.object(rb,'analyze_bench',side_effect=fake_analyze), patch.object(rb.calibrate,'calibrate',side_effect=calibrate):
            # Mock retains the API signature used by the split-aware integration check.
            import inspect
            rb.calibrate.calibrate.__signature__=inspect.signature(calibrate)
            rc=rb.main(['--out',d,'--benches','B6','--frames','2','--seeds','fit:0-1,gate:2-3','--jobs','3'])
            rows=[json.loads(line) for line in (Path(d)/'rows.jsonl').read_text().splitlines()]
            self.assertEqual(rc,5)
            self.assertEqual([r['seed'] for r in rows],[0,1,2,3])
            self.assertEqual(captured['samples'],[100,101])
            self.assertEqual(captured['seeds'],{'fit':[0,1],'gate':[2,3]})
            self.assertEqual(captured['still_meta']['seeds'],[0,1])  # still probe bound to FIT arms only
            gate=[c for c in analyzed if c[0] in (2,3)]
            self.assertEqual([c[0] for c in gate],[2,3])  # each GATE arm analysed exactly once
            self.assertEqual({t for _,t in analyzed},{(5*100+3)//4})  # ... at the T0 frozen from FIT

    def test_partial_population_refuses_and_removes_stale_profile(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'profile.json'; p.write_text('{}')
            with patch.object(rb,'run_one',side_effect=rb.BenchError('E_STEP','planted')), patch.object(rb.calibrate,'calibrate') as calib:
                rc=rb.main(['--out',d,'--benches','B6','--frames','2'])
            self.assertEqual(rc,3)
            self.assertFalse(p.exists())
            calib.assert_not_called()
            self.assertEqual(json.loads((Path(d)/'calib_report.json').read_text())['calibration']['refusal'],'incomplete_population')


class DeterminismTests(unittest.TestCase):
    def test_truth_and_decoded_beauty_are_both_bound(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as d:
            run=Path(d); (run/'truth').mkdir(); (run/'render').mkdir()
            np.save(run/'truth/f000001.ob.npy',np.zeros((2,2),np.uint16))
            p=run/'render/frame_0001.png'
            p.write_bytes(rb.decode.encode_png(np.zeros((2,2,3),np.uint8)))
            initial=rb.determinism_fingerprint(run)
            np.save(run/'truth/f000001.ob.npy',np.ones((2,2),np.uint16))
            self.assertNotEqual(rb.determinism_fingerprint(run),initial)
            np.save(run/'truth/f000001.ob.npy',np.zeros((2,2),np.uint16))
            p.write_bytes(rb.decode.encode_png(np.ones((2,2,3),np.uint8)))
            self.assertNotEqual(rb.determinism_fingerprint(run),initial)


class ReanalysisTests(unittest.TestCase):
    def test_fitted_threshold_does_not_overwrite_first_pass(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d); (run/'vision').mkdir(); (run/'vision/vision.json').write_text('{}')
            fake=SimpleNamespace(tiles=[1])
            table=rb.vparams.effective(None)
            with patch.object(rb.from_render_dir,'adapt'), patch.object(rb.pipeline,'ManifestSource'), patch.object(rb.pipeline,'analyze',return_value=fake) as analyze:
                rb.vision_analyze(run,table)
            output=analyze.call_args.args[3]
            self.assertNotEqual(output,run/'vision')
            self.assertEqual(output.name,rb.vparams.params_sha256(table))
