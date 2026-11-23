"""Opt-in Blender/engine gates against bounded, serial CPU scratch captures.

BVQA_DRIVE_BLENDER_TESTS=1 python3 -m unittest tests.test_drive_blender -v
"""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from blender_visual_qa.drive import containment, journal, launch, png

ROOT=Path(__file__).resolve().parents[1]
SCRATCH=ROOT/'build'/'room_SB1'


@unittest.skipUnless(os.environ.get('BVQA_DRIVE_BLENDER_TESTS')=='1','opt-in CPU Blender capture')
class BlenderDriveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        SCRATCH.mkdir(parents=True, exist_ok=True)
        cls.temp=tempfile.TemporaryDirectory(prefix='integration_',dir=SCRATCH)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root=Path(cls.temp.name)
        cls.ram = cls.enterClassContext(containment.workspace())
        cls.enterClassContext(patch.dict(os.environ, VQA_RAMWS_ROOT=str(cls.ram)))
        cls.source=cls.root/'cube3.blend'
        cls.runs=[]
        import zipfile
        cfg=journal.load(ROOT/'config'/'vision.json')
        wheel=Path(cfg['engine_path'])
        if not wheel.is_absolute():wheel=ROOT/wheel
        if journal.file_sha(wheel)!=cfg['pin']['wheel_sha256']:
            raise AssertionError('pinned engine wheel hash differs')
        cls.engine=cls.root/'engine_pin'
        with zipfile.ZipFile(wheel) as archive:
            if 'vadapt/blender/from_render_dir.py' not in archive.namelist():
                raise AssertionError('pinned wheel lacks Blender adapter')
            archive.extractall(cls.engine)
        env=launch.declared_env(cls.root,1)
        (cls.root/'xdg'/'empty').mkdir(parents=True)
        with open(cls.root/'example.log','wb') as log:
            with launch._owned_process(['/usr/bin/taskset','-c','15-19','/usr/bin/blender','-b','--factory-startup','--disable-autoexec','-noaudio','--python-exit-code','9','-t','1','--python',str(ROOT/'bpy_side'/'scene_example.py'),'--','--out',str(cls.source)],env=env,stdout=log,stderr=subprocess.STDOUT) as p:
                if p.wait(timeout=120)!=0:raise AssertionError('example builder failed')
        import shutil
        shutil.copyfile(ROOT/'examples'/'cube3.blend',cls.source)
        cls.source_sha=journal.file_sha(cls.source)
        cls.source_mtime=cls.source.stat().st_mtime_ns
        for index,threads in enumerate((1,8,1)):
            out=cls.root/('run'+str(index))
            args=type('Args',(),dict(blend=str(cls.source),out=str(out),journal=str(ROOT/'examples'/'cube3.journal.json'),profile='cycles-cpu-det',threads=threads,timeout_s=120,still=None,keep_raw=True,strict=True,mutant=None))()
            launch.drive(args)
            cls.runs.append(out)
        cls.receipts=[journal.load(p/'capture.receipt.json') for p in cls.runs]
        # Preserve a small measured result in the lane scratch, not a fabricated external golden.
        journal.write(SCRATCH/'integration.summary.json',{'code':'OK','rc':0,'frames_digest':cls.receipts[0]['frames_digest'],'frames':cls.receipts[0]['frames'],'blender':cls.receipts[0]['blender'],'runs':3,'threads':[1,8,1]})

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_gd1_repeat_threads_and_sparse_seed_coordinates(self):
        import platform
        first=self.receipts[0]
        golden=ROOT/'examples'/'golden'/'cube3'/('cycles-cpu-det.'+platform.machine()+'.json')
        if not golden.is_file():self.fail('golden AUSENTE for this machine')
        for run in self.runs:
            self.assertEqual(launch.check(run,golden)['rc'],0)
        for row in self.receipts[1:]:
            self.assertEqual(first['frames_digest'],row['frames_digest'])
            self.assertEqual(first['frames'],row['frames'])
        self.assertEqual([r['render_index'] for r in first['frames']],[1,5,9])
        self.assertEqual([r['seed'] for r in first['frames']],[904720292,360431674,14156008])

    def test_gd2_canonical_png(self):
        for run,receipt in zip(self.runs,self.receipts):
            for frame in receipt['frames']:
                name='frame_%06d.png'%frame['render_index']
                raw=png.decode((run/'render'/'raw'/name).read_bytes())
                data=(run/'render'/name).read_bytes()
                self.assertEqual(png.decode(data),raw)
                self.assertEqual(data,png.encode(*raw))
                self.assertEqual(journal.sha(raw[2]),frame['sha256'])

    def test_gd3_adapter_unchanged_and_still_probe(self):
        sys.path.insert(0,str(self.engine))
        from vadapt.blender.from_render_dir import adapt, BlenderAdapter
        for run,receipt in zip(self.runs,self.receipts):
            man=adapt(run/'render',run/'vision_in')
            self.assertEqual([r['render_index'] for r in man['frames']],[1,5,9])
            self.assertEqual([r['sha256'] for r in man['frames']],[r['sha256'] for r in receipt['frames']])
            self.assertEqual(man['count'],3)
            still=BlenderAdapter().still_probe(run/'render')
            self.assertEqual(still.meta['count'],2)
            self.assertTrue((run/'vision_in'/'region_hints.jsonl').is_file())
            self.assertEqual(man['provenance']['capture_receipt_sha256'],None)

    def test_gd4_source_isolation(self):
        self.assertEqual(journal.file_sha(self.source),self.source_sha)
        self.assertEqual(self.source.stat().st_mtime_ns,self.source_mtime)
        self.assertEqual(set(p.name for p in self.root.glob('*.blend')),{'cube3.blend'})

    def test_gd5_manifest_and_all_four_schemas(self):
        import jsonschema
        for run in self.runs:
            for name,path in [('journal','journal.json'),('render-profile','profile.json'),('scene','scene.manifest.json'),('capture-receipt','capture.receipt.json')]:
                schema=journal.load(ROOT/'blender_visual_qa'/'drive'/'schemas'/(name+'.v1.schema.json'))
                jsonschema.Draft202012Validator(schema).validate(journal.load(run/path))
            manifest=journal.load(run/'scene.manifest.json')
            for key in ('objects','materials'):
                self.assertEqual([r['pass_index'] for r in manifest[key]],list(range(1,len(manifest[key])+1)))
            hidden=[r for r in manifest['objects'] if r['hide_render']]
            self.assertEqual(len(hidden),2)
            self.assertIn('CURVE',[r['type'] for r in hidden])
            self.assertEqual(len({r['hint_name'] for r in manifest['objects']}),len(manifest['objects']))
            for row in manifest['objects']:
                self.assertRegex(row['hint_name'],r'^[a-z0-9_]{1,40}$')
            self.assertEqual(manifest['warnings'],[])
            receipt=journal.load(run/'capture.receipt.json')
            # Adaptation adds vision_in after this accounting, so count only original artifacts.
            total=sum(p.stat().st_size for p in run.rglob('*') if p.is_file() and 'vision_in' not in p.relative_to(run).parts)
            self.assertEqual(receipt['artifact_bytes'],total)

    def test_gd6_sigterm_during_s3_reaps_blender(self):
        import time
        j=journal.load(ROOT/'examples'/'cube3.journal.json')
        j['frames']={'start':1,'end':600,'step':1}
        journal.write(self.root/'interrupt.journal.json',j)
        out=self.root/'interrupted'
        cmd=[sys.executable,'-m','blender_visual_qa.drive.launch','--blend',str(self.source),
             '--journal',str(self.root/'interrupt.journal.json'),'--out',str(out),'--threads','1']
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        with launch._owned_process(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=ROOT) as child:
            pgid=None
            for _ in range(500):
                if child.poll() is not None:
                    self.fail('capture exited before interrupt')
                if any(log.stat().st_size for log in self.ram.glob('drive-*/logs/render.log')):
                    children=(Path('/proc')/str(child.pid)/'task'/str(child.pid)/'children').read_text().split()
                    if children:
                        pgid=int(children[0])
                        def emergency_reap():
                            try:os.killpg(pgid,signal.SIGKILL)
                            except ProcessLookupError:pass
                        self.addCleanup(emergency_reap)
                        break
                time.sleep(0.02)
            self.assertIsNotNone(pgid,'render subprocess not observed')
            os.kill(child.pid,signal.SIGTERM)
            stdout,stderr=child.communicate(timeout=15)
            self.assertEqual(child.returncode,2,(stdout,stderr))
            self.assertEqual(json.loads(stdout)['code'],'E_DRIVE')
        self.assertFalse(launch._group_alive(pgid))
        self.assertFalse(out.exists())
        self.assertFalse(list(self.ram.iterdir()))
        self.assertFalse(out.with_name(out.name+'.tmp').exists())

    def test_gd7_invalid_profile_leaves_destination_absent(self):
        args=type('Args',(),dict(blend=str(self.source),out=str(self.root/'refused'),journal=str(ROOT/'examples'/'cube3.journal.json'),profile='invalid',threads=1,timeout_s=120,still=None,keep_raw=True,strict=True,mutant=None))()
        with self.assertRaises(journal.DriveError) as cm:launch.drive(args)
        self.assertEqual((cm.exception.rc,cm.exception.code),(2,'E_PROFILE'))
        self.assertFalse((self.root/'refused').exists())
        self.assertFalse((self.root/'refused.tmp').exists())


if __name__=='__main__':
    unittest.main()
