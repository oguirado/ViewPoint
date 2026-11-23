"""FO3 / JB3-1: every bm.* row executed end to end (apply -> SD checks -> tiny CPU render -> TRUTH -> vision -> PX oracles)
and fed to harness.qualify_mutant. 160x90, 4 frames, 2 spp, serial, taskset -c 15-19. Skips cleanly without /usr/bin/blender
or the vision engine. Judge checks are not executed (wave B5): kill status is on the sd|px part of expected_check_ids.

KNOWN_OPEN pins the mutants the executed oracles do NOT kill today (reported, never hidden): the set must match exactly,
so a fix or a regression both force an update here.

STORAGE LAW (W10F L): the scratch is the Bench RAM workspace (vadapt.ramws, tool blender-visual-qa, campaign tests),
released in tearDownClass and on any setUpClass failure; a stage hook checks build/ mid-run like g_disk_frames does.
"""
import os
import re
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from bpy_side.canon import RAMWS_ROOT  # noqa: E402
AD = os.path.join(ROOT, "adapters", "example_scene")
BLENDER = "/usr/bin/blender"
CONFIG = os.path.join(ROOT, "config", "vision.json")


def pinned_engine_ok():
    sys.path.insert(0, ROOT)
    from blender_visual_qa import vision_stage
    try:
        vision_stage.pinned_wheel(CONFIG)
        return True
    except (OSError, ValueError, KeyError):
        return False


# Scratch of OTHER suites/lanes that may run concurrently (not this harness): tests/test_mutants_blender.py writes
# build/room_O3_tests (its own STORAGE LAW gap, reported), room lanes use build/room_<LANE>/.
FOREIGN_SCRATCH = re.compile(r"^(room_O3_tests|room_[A-Z][A-Za-z0-9]*)/")


def disk_frames_since(since_ns):
    """The g_disk_frames predicate: frame-like files written under build/ since since_ns (allow-listed dirs excluded),
    minus FOREIGN_SCRATCH of concurrent suites. The pre-W10F layout build/room_FO3_e2e_<pid>/ is still caught."""
    sys.path.insert(0, AD)
    import e2e
    hits = e2e.ramws().check_disk(os.path.join(ROOT, "build"), since=since_ns)
    return [h for h in hits if not FOREIGN_SCRATCH.match(h)]


# drift.15: 500 um/frame is 1/80 px at 160x90, vc_slow_drift cannot see it. freeze.14: vision FROZEN needs prior motion.
# zfight.13: bp.zfight.pixel compares two frames of a static deterministic render, so changed_ppm is 0 by construction.
KNOWN_OPEN = {"bm.drift.15", "bm.freeze_anim.14", "bm.zfight.13"}
EQUIVALENT = {"bm.neg_scale.18", "bm.step_hold.22"}


@unittest.skipUnless(os.path.exists(BLENDER), "blender missing")
@unittest.skipUnless(pinned_engine_ok(), "pinned vision engine wheel missing")
class EndToEndKillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, AD)
        import e2e
        cls.e2e = e2e
        cls.since = time.time_ns()
        cls.mid_run = []  # (stage, frame-like files under build/) seen by the hook
        cls.bench = e2e.Bench(hook=lambda stage, b: cls.mid_run.append((stage, disk_frames_since(cls.since))))
        try:
            cls.clean = cls.bench.clean()
            cls.results = {}
            for mid in sorted(cls.bench.entries):
                cls.results[mid], _ = cls.bench.qualify(mid)
        except BaseException:
            cls.bench.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.bench.close()

    def test_storage_law_no_frames_under_build_mid_run(self):
        self.assertGreater(len(self.mid_run), 10)
        self.assertEqual([m for m in self.mid_run if m[1]], [])
        self.assertTrue(str(self.bench.tmp).startswith(f"{RAMWS_ROOT}/blender-visual-qa/tests/"))

    def test_clean_controls_pass_with_evidence(self):
        self.assertEqual((self.clean["verdict"], self.clean["rc"]), ("PASS", 0))
        ids = {f["check_id"] for f in self.clean["findings"]}
        for c in ("frame.magenta", "frame.black", "frame.uniform", "frame.frozen", "bp.magenta.attrib", "bp.subject.visible",
                  "bp.zfight.pixel", "vc_flicker", "vc_frozen_subregion", "vc_slow_drift", "bo.mesh.non_manifold"):
            self.assertIn(c, ids)
        self.assertNotIn("CANNOT_SEE", {f["verdict"] for f in self.clean["findings"]})

    def test_flicker_is_killed_and_step_hold_is_equivalent_ok(self):
        self.assertEqual(self.results["bm.flicker.16"]["status"], "KILLED")
        self.assertEqual(self.results["bm.step_hold.22"]["status"], "EQUIVALENT_OK")
        self.assertEqual(self.results["bm.neg_scale.18"]["status"], "EQUIVALENT_OK")

    def test_every_non_equivalent_mutant_is_killed_except_declared_open(self):
        survivors = {m for m, r in self.results.items() if m not in EQUIVALENT and r["status"] != "KILLED"}
        self.assertEqual(survivors, KNOWN_OPEN, {m: self.results[m] for m in survivors ^ KNOWN_OPEN})
        for m in KNOWN_OPEN:
            self.assertEqual(self.results[m]["status"], "SURVIVED", m)  # never INVALID: evidence was produced
        self.assertEqual(sum(r["status"] == "KILLED" for r in self.results.values()), 17)

    def test_px_expectations_were_executed_not_synthesised(self):
        for mid in ("bm.missing_texture.05", "bm.camera_yaw.10", "bm.kill_lights.12", "bm.hide_render.17"):
            e = self.bench.entries[mid]
            self.assertTrue(e["expected_px"], mid)
        _, rep = self.bench.qualify("bm.missing_texture.05")
        attrib = [f for f in rep["findings"] if f["check_id"] == "bp.magenta.attrib"]
        self.assertEqual({f["verdict"] for f in attrib}, {"FAIL"})
        self.assertTrue(all("TexMat" in f["reason"] for f in attrib))

    def test_missing_judge_rows_keep_the_full_union_surviving(self):
        from blender_visual_qa.oracles import harness
        e = self.bench.entries["bm.missing_texture.05"]
        blend, receipt = self.bench.apply(e)
        rep, _ = self.bench.observe(blend, "judgeless", baseline=self.bench.clean_sd)
        r = harness.qualify_mutant(e, self.clean, rep, receipt)
        self.assertEqual((r["status"], r["reason"]), ("SURVIVED", "expected_check_not_failing"))


if __name__ == "__main__":
    unittest.main()


class PinnedEngineTests(unittest.TestCase):
    """BFX9d E: the e2e vision stage must run the hash-pinned wheel, never the live vision-engine source tree (whose
    uncommitted AMEND_NR SIG_DEAD_Q4 mask turned bm.step_hold.22 into EQUIVALENT_FP via one noise DRIFT tile)."""

    def test_e2e_vision_runs_the_pinned_wheel_only(self):
        from unittest import mock
        sys.path.insert(0, AD)
        import e2e
        calls = []
        with mock.patch.object(e2e.vision_stage, "run_stage",
                               side_effect=lambda run, **kw: calls.append((run, kw)) or {"status": "COMPLETE"}):
            e2e.Bench.vision(None, "RUN")
        self.assertEqual(calls, [("RUN", {"vision": "on"})])
        with mock.patch.object(e2e.vision_stage, "run_stage", return_value={"status": "INCOMPLETE"}):
            with self.assertRaises(RuntimeError):
                e2e.Bench.vision(None, "RUN")
