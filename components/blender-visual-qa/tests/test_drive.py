"""Host-only DRIVE gates; all mutation fixtures stay in SB1 scratch."""
import contextlib
import copy
import fcntl
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

    def test_contained_environment_adds_worker_settings(self):
        run = SCRATCH.resolve()
        declared = launch.declared_env(run, 1)
        managed = launch.containment.worker_environment(declared, run)
        self.assertEqual({key: managed[key] for key in declared}, declared)
        for key in ("TMPDIR", "TMP", "TEMP"):
            self.assertEqual(managed[key], str(run / "xdg" / "tmp"))
        self.assertEqual(managed["PYTHONDONTWRITEBYTECODE"], "1")

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

    def test_kill_group_escalates_when_leader_exits_before_descendant(self):
        import itertools
        fake_proc = mock.Mock(pid=424242)
        fake_proc.wait.return_value = 0
        alive = iter([True] * 6 + [False])
        with mock.patch.object(launch.os, 'killpg') as killpg, \
                mock.patch.object(launch, '_group_alive', side_effect=lambda _pgid: next(alive, False)), \
                mock.patch.object(launch.time, 'monotonic', side_effect=itertools.count(0.0, 0.2)), \
                mock.patch.object(launch.time, 'sleep'):
            launch._kill_group(fake_proc)
        self.assertEqual(
            killpg.call_args_list,
            [mock.call(424242, signal.SIGTERM), mock.call(424242, signal.SIGKILL)],
        )

    def test_owned_session_kills_descendant_surviving_sigterm_after_leader_exit(self):
        import select
        leader_code = (
            "import signal, subprocess, sys; "
            "p = subprocess.Popen([sys.executable, '-c', "
            "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(\"ready\", flush=True); time.sleep(20)'], "
            "stdout=subprocess.PIPE); "
            "assert p.stdout.readline().strip() == b'ready'; "
            "print(p.pid, flush=True)"
        )
        pgid = None
        desc_pid = None
        with launch._owned_process([sys.executable, '-c', leader_code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as leader:
            pgid = leader.pid
            self.assertTrue(select.select([leader.stdout], [], [], 5)[0])
            desc_pid = int(leader.stdout.readline().decode().strip())
            self.assertEqual(leader.wait(timeout=5), 0)
            self.assertEqual(os.getpgid(desc_pid), pgid)
        try:
            os.waitpid(desc_pid, 0)
        except ChildProcessError:
            pass
        self.assertFalse(launch._group_alive(pgid))
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(desc_pid, 0)

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


class DriveLockTests(unittest.TestCase):
    def setUp(self):
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(scratch) / "component"
        self.root.mkdir()
        self.runtime_parent = Path(scratch) / "user"
        self.runtime = self.runtime_parent / str(os.geteuid())
        self.runtime.mkdir(mode=0o700, parents=True)
        self.state = self.runtime / "viewpoint-drive"
        self.legacy = self.root / "build" / "room_SB1" / "blender.lock"
        self.enterContext(mock.patch.object(launch, "ROOT", self.root))
        self.enterContext(mock.patch.object(
            launch, "_runtime_parent",
            side_effect=lambda stack: launch._open_lock_dir(stack, self.runtime_parent),
        ))
        self.work = self.enterContext(mock.patch.object(launch, "drive"))
        self.work.return_value = {"code": "PROBE_ENTERED_DRIVE", "rc": 0}

    def admission(self, output="out"):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            rc = launch.main(["--blend", "source", "--journal", "journal", "--out", output])
        result = json.loads(stdout.getvalue())
        return rc, result["code"], result.get("reason")

    def test_checkout_and_canonical_alias_contend_on_existing_lock(self):
        (self.root / "SOURCE_LAYOUT").write_bytes((ROOT / "SOURCE_LAYOUT").read_bytes())
        self.legacy.parent.mkdir(parents=True)
        self.legacy.parent.parent.chmod(0o775)
        self.legacy.parent.chmod(0o775)
        alias = self.root.with_name("alias")
        alias.symlink_to(self.root, target_is_directory=True)
        with self.legacy.open("a") as old_lock:
            self.legacy.chmod(0o664)
            fcntl.flock(old_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(launch, "ROOT", alias):
                self.assertEqual(self.admission("other"), (6, "E_BUDGET", "blender_busy"))
            self.work.assert_not_called()
        self.assertEqual(self.admission(), (0, "PROBE_ENTERED_DRIVE", None))
        self.assertFalse(self.state.exists())

    def test_standalone_reserves_legacy_on_writable_root_and_releases_both(self):
        def work(args):
            with self.legacy.open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.admission("other"), (6, "E_BUDGET", "blender_busy"))
            raise OSError("work failed")

        self.work.side_effect = work
        self.assertEqual(self.admission(), (2, "E_IO", "work failed"))
        with self.legacy.open("a") as old_lock:
            fcntl.flock(old_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.admission(), (6, "E_BUDGET", "blender_busy"))
        self.work.side_effect = None
        self.assertEqual(self.admission(), (0, "PROBE_ENTERED_DRIVE", None))

    def test_standalone_unsafe_runtime_never_enters_work(self):
        self.runtime.rmdir()
        self.assertEqual(self.admission()[:2], (2, "E_IO"))
        self.runtime.mkdir(mode=0o775)
        self.runtime.chmod(0o775)
        self.assertEqual(self.admission()[:2], (2, "E_IO"))
        self.runtime.chmod(0o700)
        self.state.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.admission()[:2], (2, "E_IO"))
        self.work.assert_not_called()
        self.assertFalse(self.legacy.exists())


if __name__=='__main__':
    unittest.main()
