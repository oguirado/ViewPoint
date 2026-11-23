"""W10F L: STORAGE LAW gap in the FO3 e2e mutant harness + gate CPU default.

The e2e scratch (formerly build/room_FO3_e2e_<pid> with PNG/npy/.blend during the run) lives in a vadapt.ramws workspace
(tool blender-visual-qa, campaign tests) and is released at the end, even on failure. Blender is stubbed here (fast, no
engine run): the stub writes the same frame-like files the real stages write, into the --out paths the harness hands it.
The real-Blender mid-run check is tests/test_mutants_e2e.py::test_storage_law_no_frames_under_build_mid_run.
"""
import importlib.util
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bpy_side.canon import RAMWS_ROOT  # noqa: E402
AD = ROOT / "adapters" / "example_scene"
RAM_RUNS = Path(RAMWS_ROOT) / "blender-visual-qa/tests"


def load_e2e():
    sys.path.insert(0, str(AD))
    import e2e
    return e2e


def fake_blender(fail_on=None):
    """Stub of e2e.blender: writes frame-like outputs to --out (+ receipt json); rc 1 when a script name matches fail_on."""
    def run(env_dir, args, blend=None, timeout=180):
        args = [str(a) for a in args]
        script = Path(args[args.index("--python") + 1]).name
        if fail_on and fail_on in script:
            return 1, "stub failure in " + script
        out = Path(args[args.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"BLENDER-stub")
        if "--receipt" in args:
            Path(args[args.index("--receipt") + 1]).write_text(json.dumps({"stub": True}))
        (Path(env_dir) / ("stub_%s.npy" % script)).write_bytes(b"\x93NUMPY")
        return 0, ""
    return run


def disk_frames(since):
    return load_e2e().ramws().check_disk(ROOT / "build", since=since)


class E2eRamWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.e2e = load_e2e()
        self.run_id = "w10f-%d-%d" % (os.getpid(), time.time_ns() % 10 ** 9)
        self.since = time.time_ns()

    def tearDown(self):
        shutil.rmtree(RAM_RUNS / self.run_id, ignore_errors=True)

    def test_scratch_lives_in_ramws_and_build_stays_clean_mid_run(self):
        seen = []
        hook = lambda stage, b: seen.append((stage, disk_frames(self.since), sorted(p.name for p in b.tmp.iterdir())))
        with mock.patch.object(self.e2e, "blender", fake_blender()):
            with self.e2e.Bench(run_id=self.run_id, hook=hook) as b:
                self.assertEqual(b.tmp, RAM_RUNS / self.run_id / "e2e")
                self.assertEqual(b.ws.tool, "blender-visual-qa")
                self.assertEqual(b.ws.campaign, "tests")
                blend, receipt = b.apply(b.entries["bm.flicker.16"])
                self.assertEqual(receipt, {"stub": True})
                self.assertTrue(blend.is_file() and str(blend).startswith(str(RAM_RUNS)))
        self.assertEqual([s for s, _, _ in seen], ["make_base", "apply bm.flicker.16"])
        self.assertEqual([d for _, d, _ in seen], [[], []])  # g_disk_frames clean mid-run
        self.assertIn("base.blend", seen[0][2])
        self.assertIn("bm.flicker.16.blend", seen[1][2])
        self.assertFalse((RAM_RUNS / self.run_id).exists())  # released at the end
        self.assertGreater(b.ram_receipt["ram_peak_bytes"], 0)

    def test_workspace_released_after_exception_in_a_stage(self):
        with mock.patch.object(self.e2e, "blender", fake_blender(fail_on="apply_mutant")):
            with self.assertRaises(RuntimeError):
                with self.e2e.Bench(run_id=self.run_id) as b:
                    self.assertTrue((RAM_RUNS / self.run_id / "e2e" / "base.blend").is_file())
                    b.apply(b.entries["bm.flicker.16"])
        self.assertFalse((RAM_RUNS / self.run_id).exists())

    def test_workspace_released_when_setup_fails(self):
        with mock.patch.object(self.e2e, "blender", fake_blender(fail_on="make_base")):
            with self.assertRaises(RuntimeError):
                self.e2e.Bench(run_id=self.run_id)
        self.assertFalse((RAM_RUNS / self.run_id).exists())

    def test_workspace_released_when_the_hook_raises(self):
        def hook(stage, b):
            raise KeyboardInterrupt(stage)
        with mock.patch.object(self.e2e, "blender", fake_blender()):
            with self.assertRaises(KeyboardInterrupt):
                self.e2e.Bench(run_id=self.run_id, hook=hook)
        self.assertFalse((RAM_RUNS / self.run_id).exists())

    def test_disk_hook_is_not_vacuous(self):
        """Negative control: a harness writing a frame under build/ (the pre-W10F layout) is seen by the predicate."""
        d = ROOT / "build" / "room_L" / ("neg_%d" % os.getpid())
        d.mkdir(parents=True, exist_ok=True)
        try:
            (d / "frame_0001.png").write_bytes(b"\x89PNG")
            (d / "f000001.ma.npy").write_bytes(b"\x93NUMPY")
            hits = disk_frames(self.since)
            self.assertIn("room_L/neg_%d/frame_0001.png" % os.getpid(), hits)
            self.assertIn("room_L/neg_%d/f000001.ma.npy" % os.getpid(), hits)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_mutant_harness_test_has_no_build_scratch(self):
        src = (ROOT / "tests" / "test_mutants_e2e.py").read_text()
        self.assertNotIn('SCRATCH = os.path.join(ROOT, "build"', src)
        self.assertNotIn("e2e.Bench(SCRATCH)", src)
        self.assertIn("e2e.Bench(hook=", src)


class MidRunPredicateTests(unittest.TestCase):
    """The mid-run hook of test_mutants_e2e ignores concurrent foreign scratch but still sees the pre-W10F e2e layout."""

    def test_foreign_scratch_ignored_old_e2e_layout_caught(self):
        sys.path.insert(0, str(ROOT))
        from tests import test_mutants_e2e as t
        since = time.time_ns()
        made = [ROOT / "build" / "room_O3_tests" / ("w10f_%d" % os.getpid()) / "base.blend",
                ROOT / "build" / ("room_FO3_e2e_%d" % os.getpid()) / "frame_0001.png"]
        try:
            for f in made:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"x")
            hits = t.disk_frames_since(since)
            self.assertIn("room_FO3_e2e_%d/frame_0001.png" % os.getpid(), hits)
            self.assertFalse([h for h in hits if h.startswith("room_O3_tests/")])
        finally:
            for f in made:
                shutil.rmtree(f.parent, ignore_errors=True)


class ReadoptionWaveTests(unittest.TestCase):
    """A-REQ (lane A): g_migration accepts re-adoption receipts of wave W9 or W10F, exactly 3 keys, shas bound."""

    def test_readoption_receipt_shapes(self):
        spec = importlib.util.spec_from_file_location("w10f_gates_ro", ROOT / "tools" / "gates.py")
        gates = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gates)
        r = lambda rev: {"path": "x", "migration_sha256": "a" * 64, "sha256": "b" * 64, "readoption": rev}
        ok = {"old_sha256": "a" * 64, "new_sha256": "b" * 64}
        self.assertTrue(gates.readoption_ok(r(dict(ok, wave="W9"))))
        self.assertTrue(gates.readoption_ok(r(dict(ok, wave="W10F"))))
        self.assertFalse(gates.readoption_ok(r(dict(ok, wave="W11"))))
        self.assertFalse(gates.readoption_ok(r(dict(ok, wave="W10F", extra=1))))
        self.assertFalse(gates.readoption_ok(r(dict(ok, new_sha256="c" * 64, wave="W10F"))))
        self.assertFalse(gates.readoption_ok(r(dict(ok, old_sha256="c" * 64, wave="W10F"))))
        self.assertFalse(gates.readoption_ok({"path": "x", "migration_sha256": "a" * 64, "sha256": "b" * 64}))


class _Stop(Exception):
    pass


class GateCpuDefaultTests(unittest.TestCase):
    def test_smoke_default_cpu_cap_is_15_19(self):
        spec = importlib.util.spec_from_file_location("w10f_gates", ROOT / "tools" / "gates.py")
        gates = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gates)
        seen = {}

        def run(cmd, **kw):
            seen.update(kw["env"])
            raise _Stop()
        base = ROOT / "build" / "gate_disk" / str(os.getpid())
        existed = base.exists()
        env = {k: v for k, v in os.environ.items() if k != "BVQA_CPU_CAP"}
        try:
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(gates.subprocess, "run", run):
                with self.assertRaises(_Stop):
                    gates.smoke(ROOT)
            self.assertEqual(seen["BVQA_CPU_CAP"], "15-19")
            with mock.patch.dict(os.environ, {"BVQA_CPU_CAP": "16-17"}), mock.patch.object(gates.subprocess, "run", run):
                with self.assertRaises(_Stop):
                    gates.smoke(ROOT)
            self.assertEqual(seen["BVQA_CPU_CAP"], "16-17")  # env override still wins
        finally:
            if not existed:
                shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
