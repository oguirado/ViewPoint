"""Engine path on RAM evidence (law2 wheel only): raw.json -> GuiAdapter -> vcore analyze -> vmem ingest -> receipt.
Every engine touch runs in a subprocess (the pin rewrites sys.path); frames live in ramws and are released."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from vision.tests.helpers import REPO, run_py

SCRATCH = REPO / "build" / "room_E"
RUN_ROOT = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"
INGEST = str(REPO / "vision" / "ingest.py")


def cli(args, timeout=120):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(["taskset", "-c", "0,1", sys.executable, "-B", INGEST] + args, cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=timeout)


class EnginePath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from vision.tests.test_wheel_fixture import ensure_real_wheel
        ensure_real_wheel()
        SCRATCH.mkdir(parents=True, exist_ok=True)

    def test_rgb24_raw_detected_by_guiadapter(self):
        code = ("import json\nfrom vision import ingest as I, rawjson\nfrom vadapt.gui.from_screens import GuiAdapter\n"
                "import vadapt.ramws as R\nws = R.open_run('os-cu-0', 'e-t-detect-%d', 1<<26, tool='opensuperficies')\n"
                "try:\n    d = str(ws.path('cap/'))\n    rawjson.write_linear(d, I.W, I.H, I.synth_frames(5))\n"
                "    m = GuiAdapter().frames(d).meta\n    print(json.dumps([m['storage'], m['count'], m['raw_path'], m['width'], m['height']]))\n"
                "finally:\n    ws.close()\n" % os.getpid())
        p = run_py(code)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1]), ["rgb24_raw", 5, "frames.rgb24", 64, 48])

    def test_engine_frame_sha_equals_os_manifest(self):
        code = ("import hashlib, json\nfrom vision import ingest as I, rawjson\nfrom vadapt.gui.from_screens import GuiAdapter\n"
                "import vadapt.ramws as R\nfr = I.synth_frames(6)\nws = R.open_run('os-cu-0', 'e-t-sha-%d', 1<<26, tool='opensuperficies')\n"
                "try:\n    d = str(ws.path('cap/'))\n    rawjson.write_linear(d, I.W, I.H, fr)\n"
                "    eng = [f.sha256 for f in GuiAdapter().frames(d)]\n"
                "    osr = [hashlib.sha256(b).hexdigest() for _, b in sorted(fr)]\n"
                "    rows = [r['sha256'] for r in rawjson.os_manifest(fr, I.W, I.H)['frames']]\n"
                "    print(json.dumps([eng == osr, eng == rows, len(set(eng))]))\n"
                "finally:\n    ws.close()\n" % os.getpid())
        p = run_py(code)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1]), [True, True, 6])  # scrambled ring order, distinct frames

    def test_analyze_then_vmem_ingest_on_stored(self):
        store = SCRATCH / ("store_%d" % os.getpid())
        shutil.rmtree(store, ignore_errors=True)
        try:
            p = cli(["--run-id", "e-t-store-%d" % os.getpid(), "--store", str(store)])
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue(p.stdout.strip(), "empty receipt")
            rc = json.loads(p.stdout)
            self.assertEqual(rc["schema"], "os.cu.engine-receipt/v1")
            self.assertTrue(rc["engine_sha_equals_os"] and rc["released"])
            self.assertGreater(rc["regions"], 0)
            self.assertGreaterEqual(rc["gops"], 1)
            self.assertEqual(rc["ingested_gen"], 1)
            self.assertEqual(rc["storage"], "rgb24_raw")
            self.assertTrue((store / "gen" / "000001").is_dir(), os.listdir(store))
            self.assertLessEqual(len(p.stdout), 4096)
            self.assertNotRegex(p.stdout, r"/dev/shm|/home/")  # path-free receipt
            self.assertNotRegex(p.stdout, r"\d\.\d")  # integer-only
        finally:
            shutil.rmtree(store, ignore_errors=True)

    def test_ingest_imports_pin_first(self):
        src = Path(INGEST).read_text(encoding="utf-8")
        lines = src.splitlines()
        i_pin = next(i for i, l in enumerate(lines) if l.startswith("from vision import pin"))
        i_act = next(i for i, l in enumerate(lines) if l.strip() == "pin.activate()")
        eng = [i for i, l in enumerate(lines) if re.match(r"(import|from) (vadapt|visioncore|vmem)", l)]
        self.assertTrue(eng and i_pin < i_act < min(eng), (i_pin, i_act, eng))
        fake = SCRATCH / "fake_ve"
        (fake / "visioncore").mkdir(parents=True, exist_ok=True)
        (fake / "visioncore" / "__init__.py").write_text("FAKE = 1\n")
        try:  # a rogue visioncore on PYTHONPATH must lose against the wheel
            code = ("import runpy,sys\nsys.argv=['ingest.py','--run-id','e-t-pin-%d']\nimport vision.ingest as I\n"
                    "import visioncore\nprint(visioncore.__file__)\n" % os.getpid())
            p = run_py(code, pythonpath=str(fake))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("vision_engine-1.1.0-py3-none-any.whl", p.stdout)
        finally:
            shutil.rmtree(fake, ignore_errors=True)

    def test_ingest_releases_ramws(self):
        rid = "e-t-rel-%d" % os.getpid()
        p = cli(["--run-id", rid])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(json.loads(p.stdout or "{}").get("released"), p.stdout)
        self.assertFalse(os.path.exists(os.path.join(RUN_ROOT, rid)))

    def test_no_frame_on_disk_after_ingest(self):
        stamp = time.time_ns()
        store = SCRATCH / ("store_disk_%d" % os.getpid())
        try:
            p = cli(["--run-id", "e-t-disk-%d" % os.getpid(), "--store", str(store)])
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(json.loads(p.stdout or "{}").get("frames"), 6, p.stdout)
            chk = subprocess.run(["taskset", "-c", "0,1", sys.executable, "-B", str(REPO / "vision" / "ramws_shim.py"), "check-disk",
                                  str(REPO / "build"), "--since", str(stamp)], cwd=str(REPO),
                                 env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(chk.returncode, 0, chk.stdout)
            self.assertFalse((REPO / "testdata" / "cu" / "engine").exists())  # AW1-04: no engine fixtures on disk
            names = {f for _, _, fs in os.walk(store) for f in fs}
            self.assertFalse({"tiles.bin", "gops.bin", "frames.rgb24", "frames.linear.raw", "raw.json"} & names, names)
        finally:
            shutil.rmtree(store, ignore_errors=True)

    def test_ingest_refuses_sha_mismatch(self):
        rid = "e-t-shamis-%d" % os.getpid()
        code = ("import json, os\nfrom vision import ingest as I, rawjson\norig = rawjson.os_manifest\n"
                "def bad(fr, w, h):\n    m = orig(fr, w, h)\n    m['frames'][0]['sha256'] = '0' * 64\n    return m\n"
                "rawjson.os_manifest = bad\nres = None\n"
                "try:\n    I.run(%r)\nexcept I.IngestError as e:\n    res = str(e)\n"
                "print(json.dumps([res, os.path.exists(%r)]))\n" % (rid, os.path.join(RUN_ROOT, rid)))
        p = run_py(code, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1]), ["frame_sha_mismatch", False])

    def test_rawjson_refuses_dotdot_and_dup(self):
        from vision import rawjson
        base = {"width": 4, "height": 2, "stride": 12}
        for f in ("../f", "a..b", "..", "/abs", "a/b", 'a"b', "a\\b", "a\nb", "a\x00b", "a\x7fb", "", 3):
            with self.assertRaises(rawjson.RawJsonError, msg=repr(f)):
                rawjson.validate(dict(base, file=f))
        self.assertEqual(rawjson.validate(dict(base, file="frames.linear.raw"))["file"], "frames.linear.raw")
        with self.assertRaises(rawjson.RawJsonError) as cm:
            rawjson.linearise([(1, b"a"), (1, b"b")])
        self.assertEqual(str(cm.exception), "duplicate_render_index")
        with self.assertRaises(rawjson.RawJsonError) as cm:
            rawjson.validate(dict(base, file="a..b"))
        self.assertEqual(str(cm.exception), "raw_json_file")

    def test_ingest_cap_released_before_analyze(self):
        rid = "e-t-caprel-%d" % os.getpid()
        d = os.path.join(RUN_ROOT, rid)
        code = ("import json, os\nfrom vision import ingest as I\nseen = []\n"
                "def fake(*a, **k):\n    seen.append([os.path.exists(%r), os.path.exists(%r)])\n    raise I.IngestError('stop')\n"
                "I.pipeline.analyze = fake\n"
                "try:\n    I.run(%r)\nexcept I.IngestError:\n    pass\nprint(json.dumps(seen))\n"
                % (os.path.join(d, "cap"), os.path.join(d, "eng"), rid))
        p = run_py(code, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout.strip().splitlines()[-1]), [[False, True]])  # cap gone, eng still there

    def test_ingest_no_rc1(self):
        for args in (["--bogus", "1"], ["--frames", "1"], ["--frames", "x"], ["--run-id"]):
            p = cli(args)
            self.assertEqual(p.returncode, 2, (args, p.stdout))
        self.assertNotIn("sys.exit(1)", Path(INGEST).read_text(encoding="utf-8"))
        self.assertEqual(cli(["--run-id", "e-t-rc-%d" % os.getpid()]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
