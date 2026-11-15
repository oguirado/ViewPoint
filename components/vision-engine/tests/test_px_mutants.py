"""PX mutant suite (SPEC_INT §3 X1-X5 on synth): each vqa.px-mutant/v1 spec in tests/px_mutants/*.json is run through
S0-S2 and must be detected in the expected class and region; controls (GPU jitter, clean) must stay silent."""
import hashlib
import json
import time
import unittest

import numpy as np

from tests.px_mutants import driver as D
from visioncore import canon, ids
from vadapt.synth import render_scenario

SPECS = D.load_specs()
DETECT = [k for k, v in SPECS.items() if not v["expect"].get("control")]
CONTROLS = [k for k, v in SPECS.items() if v["expect"].get("control")]
_CACHE = {}


def ev(name):
    if name not in _CACHE:
        _CACHE[name] = D.evaluate(SPECS[name])
    return _CACHE[name]


class TestSpecs(unittest.TestCase):
    def test_inventory(self):
        for need in ("flicker", "frozen_subregion", "small_overlap", "slow_drift", "scroll", "gpu_jitter_control"):
            self.assertIn(need, SPECS)
        self.assertEqual(len(SPECS), 11)

    def test_schema_fields(self):
        for k, s in SPECS.items():
            self.assertEqual(s["schema"], "vqa.px-mutant/v1")
            self.assertEqual(s["id"], k)
            self.assertIn("expect", s)

    def test_spec_rect_matches_runtime_truth(self):
        for k in DETECT:
            s = SPECS[k]
            if not s.get("rect") or k == "scroll":
                continue
            _, truth = D.build(s)
            self.assertEqual(list(truth["rect"]), s["rect"], k)

    def test_truth_fields_use_ids(self):
        for k in DETECT:
            truth = ev(k)["truth"]
            for f in truth["fields"][:20]:
                self.assertTrue(D.FIELD.match(f), f)
            if truth.get("rect"):
                want = []
                for fr in truth["frames"]:
                    want += ids.field_ids_of_rect(tuple(truth["rect"]), 256, 160, fr)
                self.assertEqual(truth["fields"], want, k)


class TestDetection(unittest.TestCase):
    """X3 on synth: the expected class lands on the truth tiles, nothing of that family fires outside."""

    def _check(self, name):
        m, ex = ev(name), SPECS[name]["expect"]
        self.assertGreaterEqual(m["hits"], ex["min_hits"], name)
        if ex["min_hits"]:
            self.assertIsNotNone(m["first_hit"])
            self.assertLessEqual(m["first_hit"], ex["first_hit_max"], name)
        self.assertLessEqual(m["outside"], ex.get("outside_max", 0), name)

    def test_flicker(self):
        self._check("flicker")
        # periodic blink must not be reported as plain CHANGED after warm-up
        m = ev("flicker")
        self.assertEqual(int((m["cls"][3:] == D.CLS.index("CHANGED")).sum()), 0)

    def test_frozen_subregion(self):
        self._check("frozen_subregion")

    def test_small_overlap(self):
        self._check("small_overlap")

    def test_slow_drift(self):
        self._check("slow_drift")

    def test_toggle_paste_shift_scroll_rect(self):
        for n in ("toggle_rect", "paste_rect", "shift_rect", "scroll_rect"):
            self._check(n)

    def test_scroll_global_motion(self):
        m, ex = ev("scroll"), SPECS["scroll"]["expect"]
        for f in range(1, m["frames"]):
            self.assertEqual(m["gmv"][f], ex["global_mv"], f)
        cls = m["cls"][1:]
        dirty = int((cls >= 2).sum())
        moved = int((cls == D.CLS.index("MOVED")).sum())
        self.assertGreaterEqual(moved * 1000, ex["moved_share_permille_min"] * dirty)

    def test_every_detectable_spec_has_hits(self):
        for k in DETECT:
            if SPECS[k]["expect"]["min_hits"]:
                self.assertGreater(ev(k)["hits"], 0, k)


class TestControls(unittest.TestCase):
    """X4 on synth: zero false dirty tiles on clean and GPU-jitter controls."""

    def test_gpu_jitter_never_dirty(self):
        m = ev("gpu_jitter_control")
        self.assertEqual(m["dirty_total"], 0)
        self.assertGreater(int((m["cls"] == D.CLS.index("JITTER")).sum()), 0)  # jitter is seen, but classed JITTER

    def test_clean_all_unchanged(self):
        m = ev("clean_control")
        self.assertEqual(int((m["cls"] != 0).sum()), 0)

    def test_seed_sweep_fp(self):
        for seed in (1, 2, 3, 4, 5):
            for name in ("clean_static", "gpu_jitter"):
                fr, _, _ = render_scenario(name, seed)
                g, cls, _, _ = D.classify_frames(fr)
                self.assertEqual(int((cls >= 2).sum()), 0, (name, seed))

    def test_mutant_is_not_a_control(self):
        # negative: a real mutant must NOT satisfy the control predicate
        self.assertGreater(ev("flicker")["dirty_total"], 0)
        self.assertGreater(ev("toggle_rect")["dirty_total"], 0)

    def test_wrong_class_expectation_fails(self):
        # negative: asking for the wrong class yields zero hits (guards against a vacuous evaluator)
        s = json.loads(json.dumps(SPECS["flicker"]))
        s["expect"]["classes"] = ["FROZEN"]
        self.assertEqual(D.evaluate(s)["hits"], 0)

    def test_wrong_rect_misses_truth_zone(self):
        s = json.loads(json.dumps(SPECS["toggle_rect"]))
        frames, truth = D.build(s)
        s["rect"] = [160, 8, 48, 32]  # claimed zone is not where the mutation is
        m = D.evaluate(s, frames, truth)
        self.assertGreater(m["outside"], 0)


class TestRegions(unittest.TestCase):
    """S4 view of the same mutants; skipped while visioncore.regions is absent."""

    def setUp(self):
        try:
            from visioncore import regions
        except ImportError:
            self.skipTest("visioncore.regions missing")
        self.regions = regions

    def _regs(self, name, frame):
        m = ev(name)
        return self.regions.regions_of(m["tiles"][frame], m["grid"].W, m["grid"].H, frame), m

    def test_flicker_region_covers_target(self):
        regs, m = self._regs("flicker", 5)
        fl = [r for r in regs if r["cls"] == "FLICKER"]
        self.assertEqual(len(fl), 1)
        x, y, w, h = fl[0]["bbox_px"]
        self.assertEqual([x, y, w, h], SPECS["flicker"]["rect"])

    def test_frozen_region(self):
        regs, m = self._regs("frozen_subregion", 20)
        fr = [r for r in regs if r["cls"] == "FROZEN"]
        self.assertTrue(fr)
        sx, sy, sw, sh = SPECS["frozen_subregion"]["rect"]
        for r in fr:
            x, y, w, h = r["bbox_px"]
            self.assertTrue(x >= sx - 16 and y >= sy - 16 and x + w <= sx + sw + 16 and y + h <= sy + sh + 16)

    def test_scroll_region_is_moved_group(self):
        regs, m = self._regs("scroll", 10)
        self.assertTrue(any(r["group"] == "MOV" and r["mv"] == [4, 0] for r in regs))

    def test_control_has_no_regions(self):
        regs, m = self._regs("gpu_jitter_control", 10)
        self.assertEqual(regs, [])


class TestDeterminismAndCost(unittest.TestCase):
    def _digest(self, name):
        fr, _ = D.build(SPECS[name])
        g, cls, gmv, tiles = D.classify_frames(fr)
        return hashlib.sha256(b"".join(t.tobytes() for t in tiles)).hexdigest()

    def test_x1_three_repeats_identical(self):
        for n in ("flicker", "scroll"):
            self.assertEqual(len({self._digest(n) for _ in range(3)}), 1, n)

    def test_x2_clean_vs_clean_changed_rate(self):
        # same scene rendered twice (different process-independent seeds equal) -> 0 CHANGED
        a, _, _ = render_scenario("clean_static", 7)
        b, _, _ = render_scenario("gpu_jitter", 7)
        g, cls, _, _ = D.classify_frames(a[:12] + b[:12])
        self.assertEqual(int((cls >= 2).sum()), 0)

    def test_x5_cost_bounded(self):
        fr, _ = D.build(SPECS["flicker"])
        t0 = time.perf_counter()
        D.classify_frames(fr)
        per = (time.perf_counter() - t0) / len(fr)
        self.assertLess(per, 0.5)  # 256x160 synthetic; generous CI bound, real G-E4 measured at 720p elsewhere


if __name__ == "__main__":
    unittest.main()
