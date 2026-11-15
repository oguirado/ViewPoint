"""Host-only DRIVE gates; all mutation fixtures stay in SB1 scratch."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

from blender_visual_qa.drive import journal, launch, png

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "build" / "room_SB1"
SCRATCH.mkdir(parents=True, exist_ok=True)


def bpy_common():
    spec = importlib.util.spec_from_file_location("drive_common_test", ROOT / "bpy_side" / "drive_common.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.j = journal.load(ROOT / "examples" / "cube3.journal.json")

    def test_sparse_frame_indices(self):
        self.assertEqual(journal.frame_list(journal.validate(self.j)), [1, 5, 9])
        self.assertNotEqual(journal.seed_for(self.j, 5), journal.seed_for(self.j, 2))
        self.j["seed_mode"] = "fixed"
        self.assertEqual(journal.seed_for(self.j, 1), journal.seed_for(self.j, 9))

    def test_seed_golden_and_bpy_twin(self):
        triples = [(0,0,0),(0,1,0),(0,1,1),(0,17,1),(0,17,5),(0,17,9),(1,0,0),(1,1,1),(16777215,1048575,1048575),(7,31,99)]
        golden = [0,1371419539,363264740,904720292,360431674,14156008,1824779127,598127744,943046336,1022607159]
        common = bpy_common()
        for t, expected in zip(triples, golden):
            self.assertEqual(journal.cycles_seed(*t), expected)
            self.assertEqual(common.cycles_seed(*t), expected)

    def test_invalid_journals_fail_closed(self):
        mutations = [lambda j: j.update(schema="bvqa.journal.v1"),
                     lambda j: j.update(seed=1048576), lambda j: j.update(seed=True),
                     lambda j: j.update(seed=1.5), lambda j: j.update(frames={"list":[1,1]}),
                     lambda j: j.update(frames={"list":[5,1]}), lambda j: j.update(frames={"list":[]}),
                     lambda j: j.update(ops=[{"op":"eval","python":"bad"}]),
                     lambda j: j.update(ops=[{"op":"hide","obj":"x","frame":1,"render":1}]),
                     lambda j: j.update(ops=[{"op":"obj_key","obj":"x","frame":1,"loc_um":[0,0],"interp":"LINEAR"}]),
                     lambda j: j.update(ops=[{"op":"still","count":31}]),
                     lambda j: j.update(animated=[{"frame":1,"name":"x","kind":"unknown"}])]
        for mutate in mutations:
            j = copy.deepcopy(self.j)
            mutate(j)
            with self.subTest(j=j), self.assertRaises(journal.DriveError):
                journal.validate(j)

    def test_frame_budget_refuses_before_range_allocation(self):
        self.j["frames"] = {"start":0,"end":1048575,"step":1}
        with self.assertRaises(journal.DriveError) as cm:
            journal.validate(self.j)
        self.assertEqual((cm.exception.rc, cm.exception.code), (6, "E_BUDGET"))

    def test_canonical_and_duplicate_keys(self):
        self.assertEqual(journal.canonical({"b":2,"a":1}), b'{"a":1,"b":2}\n')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as d:
            p = Path(d)/"x.json"
            for text in ('{"a":1,"a":2}', '{"x":NaN}'):
                p.write_text(text)
                with self.assertRaises(journal.DriveError):
                    journal.load(p)


class PNGTests(unittest.TestCase):
    def test_all_filters_and_canonical_bytes(self):
        w,h = 4,3
        pixels = bytes((i*37+3) & 255 for i in range(w*h*3))
        reference = png.encode(w,h,pixels)
        for f in range(5):
            rows=[]
            for y in range(h):
                row=pixels[y*w*3:(y+1)*w*3]
                prev=pixels[(y-1)*w*3:y*w*3] if y else bytes(w*3)
                encoded=bytearray()
                for i,value in enumerate(row):
                    a=row[i-3] if i>=3 else 0
                    b=prev[i]; c=prev[i-3] if i>=3 else 0
                    p=a+b-c
                    candidates=(a,b,c)
                    pred=min(enumerate(candidates),key=lambda q:(abs(p-q[1]),q[0]))[1]
                    encoded.append((value-(0,a,b,(a+b)//2,pred)[f]) & 255)
                rows.append(bytes([f])+encoded)
            data=png.PNG_SIG+png.chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+png.chunk(b'tEXt',b'volatile\0metadata')+png.chunk(b'IDAT',zlib.compress(b''.join(rows)))+png.chunk(b'IEND',b'')
            self.assertEqual(png.decode(data),(w,h,pixels))
            self.assertEqual(png.encode(*png.decode(data)),reference)
        self.assertNotIn(b'tEXt',reference)

    def test_corrupt_inputs_refused(self):
        data=png.encode(1,1,b'abc')
        invalid=[b'bad', data[:-1], data+b'extra', data[:25]+bytes([data[25]^1])+data[26:],
                 png.PNG_SIG+png.chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,6,0,0,0))+png.chunk(b'IDAT',zlib.compress(b'\0abcd'))+png.chunk(b'IEND',b'')]
        for item in invalid:
            with self.subTest(item=item),self.assertRaises(journal.DriveError) as cm:
                png.decode(item)
            self.assertEqual(cm.exception.code,'E_DECODE')


class LaunchTests(unittest.TestCase):
    def test_declared_environment_and_normalised_digest(self):
        with mock.patch.dict(os.environ, {"EVIL":"leak", "DISPLAY":":999"}):
            a=launch.declared_env('/tmp/aa',1); b=launch.declared_env('/tmp/bb',1)
        self.assertEqual(set(a),{'BLENDER_USER_SCRIPTS','HOME','LC_ALL','OMP_NUM_THREADS','PATH','PYTHONNOUSERSITE','TZ','XDG_CACHE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME'})
        self.assertEqual(list(a),sorted(a))
        self.assertEqual(launch.env_sha256(a,'/tmp/aa'),launch.env_sha256(b,'/tmp/bb'))
        self.assertEqual(launch.env_sha256(a,'/tmp/aa'),journal.sha(''.join(k+'='+v.replace('/tmp/aa','@RUN')+'\n' for k,v in a.items()).encode()))

    def test_actual_process_environ_matches_declared_list(self):
        env=launch.declared_env(SCRATCH,1)
        with launch._owned_process([sys.executable,'-c','import os,time; os.write(1,b\"ready\\n\"); time.sleep(20)'],env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as child:
            import select
            self.assertTrue(select.select([child.stdout],[],[],5)[0])
            self.assertEqual(child.stdout.readline(),b'ready\n')
            actual=(Path('/proc')/str(child.pid)/'environ').read_bytes().split(b'\0')
            actual=[line for line in actual if line]
            expected=[(k+'='+v).encode() for k,v in env.items()]
            self.assertEqual(actual,expected)

    def test_argv_mask_every_bit_no_launch(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as d:
            scratch=Path(d)/'scratch';scratch.mkdir()
            blend=scratch/'x.blend';blend.touch()
            nominal=['/usr/bin/blender','-b','--factory-startup','--python-exit-code','9',str(blend),'--python','drive.py','--','--out',d]
            self.assertEqual(launch._validate_render_argv(nominal,d),0)
            variants=[nominal+['-a'],nominal+['--enable-autoexec'],[x for x in nominal if x!='--factory-startup'],[x for x in nominal if x!='--python-exit-code'],[x if x!=str(blend) else '/tmp/original.blend' for x in nominal],nominal+['--addons','cycles'],nominal+['-E','CYCLES']]
            with mock.patch.object(subprocess,'Popen') as popen:
                for argv in variants:
                    with self.assertRaises(journal.DriveError) as cm:
                        launch._validate_render_argv(argv,d)
                    self.assertEqual((cm.exception.rc,cm.exception.reason),(5,'argv_forbidden'))
                popen.assert_not_called()

    def test_owned_process_always_requests_new_session(self):
        with mock.patch.object(subprocess,'Popen') as popen,mock.patch.object(launch,'_kill_group'):
            with launch._owned_process(['stub'],env={'PATH':'/usr/bin:/bin'}):
                pass
            popen.assert_called_once_with(['stub'],start_new_session=True,env={'PATH':'/usr/bin:/bin'})

    def test_owned_session_kills_process_group(self):
        proc = None
        with launch._owned_process([sys.executable,'-c','import time; time.sleep(20)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) as child:
            proc=child
            self.assertEqual(os.getpgid(child.pid),child.pid)
        self.assertIsNotNone(proc.returncode)
        with self.assertRaises(ProcessLookupError):
            os.killpg(proc.pid,0)

    def test_signal_cleanup(self):
        with self.assertRaises(journal.DriveError):
            with launch._owned_process([sys.executable,'-c','import time; time.sleep(20)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) as child:
                pgid=child.pid
                os.kill(os.getpid(),signal.SIGTERM)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid,0)

    def test_usage_stage_json_and_no_rc_one(self):
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc=launch.main([])
        self.assertEqual(rc,2)
        self.assertEqual(json.loads(buf.getvalue())['code'],'E_USAGE')

    def test_refusal_preserves_existing_output(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as d:
            d=Path(d);src=d/'src.blend';src.write_bytes(b'source');out=d/'run';out.mkdir()
            sentinel=out/'sentinel';sentinel.write_bytes(b'unchanged')
            buf=io.StringIO()
            with contextlib.redirect_stdout(buf),mock.patch.object(subprocess,'Popen') as popen:
                rc=launch.main(['--blend',str(src),'--journal',str(ROOT/'examples/cube3.journal.json'),'--out',str(out),'--profile','forbidden'])
            self.assertEqual(rc,2)
            self.assertEqual(sentinel.read_bytes(),b'unchanged')
            self.assertEqual(src.read_bytes(),b'source')
            popen.assert_not_called()
            self.assertFalse(out.with_name('run.tmp').exists())

    def test_golden_mismatch_stale_vs_first_divergence(self):
        fixture={'blend_sha256':'a','journal_sha256':'b','profile_sha256':'c','machine':'x','backend_fp':'d','blender':{'version':'v','build_hash':'e','binary_sha256':'f'},'frames':[{'render_index':1,'sha256':'a','seed':3},{'render_index':5,'sha256':'b','seed':4}]}
        with tempfile.TemporaryDirectory(dir=SCRATCH) as d:
            d=Path(d);(d/'render').mkdir()
            for row,pixels in zip(fixture['frames'],(b'abc',b'def')):
                data=png.encode(1,1,pixels)
                row['sha256']=journal.sha(pixels);row['file_sha256']=journal.sha(data)
                (d/'render'/('frame_%06d.png'%row['render_index'])).write_bytes(data)
            journal.write(d/'capture.receipt.json',fixture);journal.write(d/'golden.json',fixture)
            self.assertEqual(launch.check(d,d/'golden.json')['rc'],0)
            for field in ('machine','backend_fp','profile_sha256'):
                changed=copy.deepcopy(fixture);changed[field]='different';journal.write(d/'golden.json',changed)
                with self.assertRaises(journal.DriveError) as cm:launch.check(d,d/'golden.json')
                self.assertEqual(cm.exception.rc,4)
            changed=copy.deepcopy(fixture);changed['frames'][1]['sha256']='wrong';journal.write(d/'golden.json',changed)
            with self.assertRaises(journal.DriveError) as cm:launch.check(d,d/'golden.json')
            self.assertEqual((cm.exception.rc,cm.exception.reason),(5,'first_div_ri:5'))


if __name__=='__main__':
    unittest.main()
