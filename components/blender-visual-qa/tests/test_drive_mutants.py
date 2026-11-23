"""Bounded planted DRIVE negatives. A nominal capture must pass before mutation.

BVQA_DRIVE_MUTANTS=1 python3 -m unittest tests.test_drive_mutants -v
Each mutation is applied only to an isolated SB1 scratch source copy.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from blender_visual_qa.drive import journal, launch

ROOT=Path(__file__).resolve().parents[1]
SCRATCH=ROOT/'build'/'room_SB1'


@unittest.skipUnless(os.environ.get('BVQA_DRIVE_MUTANTS')=='1','opt-in planted capture mutants')
class DriveMutantTests(unittest.TestCase):
    def test_all_planted_negatives(self):
        import platform
        golden=ROOT/'examples'/'golden'/'cube3'/('cycles-cpu-det.'+platform.machine()+'.json')
        expected=journal.load(golden)
        records=[]
        cases=[
            ('dm.seed_anim.01','bpy_side/drive_scene.py','c.use_animated_seed = False','c.use_animated_seed = True',(2,'E_PROFILE','animated_seed')),
            ('dm.denoise.02','bpy_side/drive_scene.py','c.use_denoising = False','c.use_denoising = True',(2,'E_PROFILE','denoise')),
            ('dm.dither.03','bpy_side/drive_scene.py','r.dither_intensity = 0.0','r.dither_intensity = 1.0',(2,'E_PROFILE','dither')),
            ('dm.agx.04','bpy_side/drive_scene.py','vs.view_transform, vs.look = p["view_transform"], p["look"]','vs.view_transform, vs.look = "AgX", p["look"]',(2,'E_PROFILE','view_transform')),
            ('dm.stamp.05','blender_visual_qa/drive/finish.py','target.write_bytes(encode(width, height, rgb))','target.write_bytes(raw.read_bytes())','canonical'),
            ('dm.filter.06','blender_visual_qa/drive/png.py','b"\\0" + rgb','b"\\1" + rgb','pixels'),
            ('dm.frame_off1.07','bpy_side/drive_scene.py','scene.frame_set(ri)','scene.frame_set(ri+1)','pixels'),
            ('dm.seed_ri.08','bpy_side/drive_scene.py','0 if j["seed_mode"] == "fixed" else ri','0 if j["seed_mode"] == "fixed" else frames.index(ri)',(5,'E_GATE','seed_receipt')),
            ('dm.orig_write.09','blender_visual_qa/drive/launch.py','shutil.copyfile(src, scratch)','shutil.copyfile(src, scratch)\n    scratch = src',(5,'E_GATE','argv_forbidden')),
            ('dm.passidx.10','bpy_side/drive_common.py','key=lambda o: o.name)','key=lambda o: o.name, reverse=True)','indices'),
            ('dm.hidden_skip.11','bpy_side/drive_common.py','if o.type in GEOMETRY)','if o.type in GEOMETRY and not o.hide_render)','indices'),
            ('dm.rawname.12','bpy_side/drive_common.py','return slug + "_" + ("%08x" % obj.pass_index)','return obj.name','names'),
            ('dm.zrange.13','bpy_side/drive_scene.py','row["z"] = min(rank, 32767)','row["z"] = round(candidates[rank][0]*1000)','zrank'),
            ('dm.env_leak.14','blender_visual_qa/drive/launch.py','return dict(sorted({"BLENDER_USER_SCRIPTS"','return dict(sorted({**os.environ, "BLENDER_USER_SCRIPTS"','unit_env'),
            ('dm.autoexec.15','blender_visual_qa/drive/launch.py','"--disable-autoexec"','"--enable-autoexec"',(5,'E_GATE','argv_forbidden')),
            ('dm.eevee_ctx.16','blender_visual_qa/drive/launch.py','if actual[field] != expected[field]:','if field != "backend_fp" and actual[field] != expected[field]:','unit_stale'),
            ('dm.orphan.17','blender_visual_qa/drive/launch.py','start_new_session=True','start_new_session=False','unit_session'),
        ]
        SCRATCH.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='mutants_',dir=SCRATCH) as temp:
            temp=Path(temp)
            for ident,path,before,after,killer in [('nominal','','','','nominal')]+cases:
                with self.subTest(mutant=ident):
                    sandbox=temp/ident
                    (sandbox/'blender_visual_qa').mkdir(parents=True)
                    (sandbox/'blender_visual_qa'/'__init__.py').write_text('')
                    shutil.copytree(ROOT/'blender_visual_qa'/'drive',sandbox/'blender_visual_qa'/'drive',ignore=shutil.ignore_patterns('__pycache__'))
                    (sandbox/'bpy_side').mkdir()
                    for name in ('canon.py','drive_common.py','drive_scene.py','drive_manifest.py'):
                        shutil.copyfile(ROOT/'bpy_side'/name,sandbox/'bpy_side'/name)
                    (sandbox/'tests').mkdir();(sandbox/'tests'/'__init__.py').write_text('')
                    shutil.copyfile(ROOT/'tests'/'test_drive.py',sandbox/'tests'/'test_drive.py')
                    shutil.copytree(ROOT/'examples',sandbox/'examples')
                    if path:
                        target=sandbox/path
                        text=target.read_text()
                        self.assertEqual(text.count(before),1,'mutation anchor must be unique: '+ident)
                        target.write_text(text.replace(before,after))
                    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
                    if str(killer).startswith('unit_'):
                        test={'unit_env':'test_declared_environment_and_normalised_digest','unit_stale':'test_golden_mismatch_stale_vs_first_divergence','unit_session':'test_owned_process_always_requests_new_session'}[killer]
                        cmd=[sys.executable,'-m','unittest','tests.test_drive.LaunchTests.'+test]
                    else:
                        cmd=[sys.executable,'-m','blender_visual_qa.drive.launch','--blend',str(ROOT/'examples'/'cube3.blend'),'--journal',str(ROOT/'examples'/'cube3.journal.json'),'--out',str(sandbox/'run'),'--threads','1']
                    result=subprocess.run(cmd,cwd=sandbox,env=env,capture_output=True,text=True,timeout=120)
                    (SCRATCH/(ident+'.stdout.log')).write_text(result.stdout[-12000:])
                    (SCRATCH/(ident+'.stderr.log')).write_text(result.stderr[-12000:])
                    if str(killer).startswith('unit_'):
                        self.assertEqual(result.returncode,1,result.stderr[-2000:])
                        self.assertIn('FAIL:',result.stderr)
                        self.assertNotIn('ERROR:',result.stderr)
                        reason=killer
                    else:
                        row=json.loads(result.stdout)
                        if isinstance(killer,tuple):
                            self.assertEqual((result.returncode,row['code'],row.get('reason')),killer,result.stdout)
                            reason=row['reason']
                        else:
                            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                            receipt=journal.load(sandbox/'run'/'capture.receipt.json')
                            if killer=='nominal':
                                self.assertEqual(launch.check(sandbox/'run',golden)['rc'],0)
                                reason='nominal_pass'
                            elif killer=='canonical':
                                self.assertNotEqual([r['file_sha256'] for r in receipt['frames']],[r['file_sha256'] for r in expected['frames']])
                                reason='G-D2'
                            elif killer=='pixels':
                                with self.assertRaises(journal.DriveError) as cm:launch.check(sandbox/'run',golden)
                                self.assertEqual((cm.exception.rc,cm.exception.code),(5,'E_DETERMINISM'))
                                reason='G-D1'
                            elif killer=='indices':
                                man=journal.load(sandbox/'run'/'scene.manifest.json')
                                actual={r['name']:r['pass_index'] for r in man['objects']}
                                self.assertNotEqual(actual,{'A Hidden':1,'B Hidden Curve':2,'Cube.001 Big':3})
                                reason='G-D5'
                            elif killer=='names':
                                import re
                                rows=[json.loads(line) for line in (sandbox/'run'/'render'/'bounds.jsonl').read_text().splitlines()]
                                self.assertTrue(rows)
                                self.assertTrue(any(not re.fullmatch('[a-z0-9_]{1,40}',r['name']) for r in rows))
                                reason='T-D-NAMES'
                            elif killer=='zrank':
                                rows=[json.loads(line) for line in (sandbox/'run'/'render'/'bounds.jsonl').read_text().splitlines()]
                                self.assertTrue(rows)
                                self.assertTrue(any(r['z']!=0 for r in rows)) # fixture has one visible object
                                reason='G-D3-rank'
                    records.append({'id':ident,'killed':ident!='nominal','oracle':reason,'rc':result.returncode})
        journal.write(SCRATCH/'mutants.summary.json',{'code':'OK','rc':0,'records':records})
        self.assertEqual(len(records),18)


if __name__=='__main__':
    unittest.main()
