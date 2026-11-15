"""JFXP1 JT: FLICKER strata power (>= 6 strata per primary on GATE/TEST), split-scoped claims (M15), seal history of 3."""
import json
import os
import unittest
from fractions import Fraction as F
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(REPO, "eval", "gui_synth", "SEAL_HISTORY.jsonl")


def _strata(kinds, split):
    from eval.gui_synth import scenes, splits
    out = set()
    for kind in kinds:
        for k in splits.seeds(split):
            s = scenes.build(kind, k, split=split, w=320, h=180, n=30)
            out.add((kind, json.dumps(s["param_bucket"], sort_keys=True)))
    return out


class TestStrataPower(unittest.TestCase):
    def test_primaries_have_at_least_six_strata_on_gate_and_test(self):
        prim = json.load(open(os.path.join(REPO, "eval/gui_synth/PREREG.json")))["primary"]
        self.assertEqual(len(prim), 3)
        for p in prim:
            for sp in ("GATE", "TEST"):
                n = len(_strata(p["kinds"], sp))
                self.assertGreaterEqual(n, 6, (p["id"], sp, n))

    def test_flicker_blink_ten_strata_on_gate_and_test(self):
        for sp in ("GATE", "TEST"):
            self.assertEqual(len(_strata(["caret_blink"], sp)), 10, sp)  # min two-sided p 1/512

    def test_blink_half_period_sets_are_the_judges_lists_and_disjoint_from_dev(self):
        from eval.gui_synth import splits
        ps = splits.PARAMETER_SETS
        self.assertEqual(ps["GATE"]["caret_blink"]["half_period_ms"], [300, 333, 367, 450, 750])
        self.assertEqual(ps["TEST"]["caret_blink"]["half_period_ms"], [383, 417, 467, 550, 875])
        for sp in ("GATE", "TEST"):
            self.assertEqual(ps[sp]["caret_blink"]["fps"], [30, 60])
            self.assertEqual(set(ps[sp]["caret_blink"]["half_period_ms"]) & set(ps["DEV"]["caret_blink"]["half_period_ms"]), set())
        self.assertEqual(set(ps["GATE"]["caret_blink"]["half_period_ms"]) & set(ps["TEST"]["caret_blink"]["half_period_ms"]), set())

    def test_blink_periods_keep_flicker_support(self):
        from eval.gui_synth import splits
        for sp in ("GATE", "TEST"):
            for hp in splits.PARAMETER_SETS[sp]["caret_blink"]["half_period_ms"]:
                self.assertLessEqual(2 * hp * 60 // 1000, 105, (sp, hp))


def _u(kind, v, param):
    return {"kind": kind, "param_bucket": {"p": param}, "metrics": {"m": v}}


def _cmp(a, b, **kw):
    from eval.gui_synth import prereg, signflip
    res = lambda u: {"prereg_sha": "x", "scene_units": u}
    with mock.patch.object(prereg, "verify", return_value={}), mock.patch.object(prereg, "sha256", return_value="x"):
        return signflip.compare(res(a), res(b), "m", **kw)


def _split_units():
    """DEV: 2 strata moving up (would pool into the claim); GATE: 7 strata all up; TEST: 7 strata all up."""
    a = {}
    for sp, n in (("DEV", 2), ("GATE", 7), ("TEST", 7)):
        for i in range(n):
            a["%s:K:%d:%d" % (sp, i, i)] = _u("K", 0, "%s%d" % (sp, i))
    b = {k: _u("K", 1, v["param_bucket"]["p"]) for k, v in a.items()}
    return a, b


class TestSplitScopedClaims(unittest.TestCase):
    def test_split_filter_keeps_only_prefix_units(self):
        a, b = _split_units()
        r = _cmp(a, b, split="GATE")
        self.assertEqual((r["split"], r["n_used"], r["n_strata"]), ("GATE", 7, 7))
        self.assertEqual(r["p_stratified"], "1/64")
        self.assertEqual(r["claim"], "improvement")

    def test_dev_split_never_claims(self):
        a, b = _split_units()
        r = _cmp(a, b, split="DEV")
        self.assertEqual((r["split"], r["n_used"]), ("DEV", 2))
        self.assertEqual(r["claim"], "no claim")

    def test_no_split_is_no_claim_and_records_none(self):
        a, b = _split_units()
        r = _cmp(a, b)
        self.assertIsNone(r["split"])
        self.assertEqual(r["n_used"], 16)
        self.assertEqual(r["claim"], "no claim")  # p would be 1/32768 < 1/20 but pooled DEV+GATE+TEST is not a claim

    def test_test_split_can_claim(self):
        a, b = _split_units()
        r = _cmp(a, b, split="TEST")
        self.assertEqual((r["split"], r["n_used"], r["claim"]), ("TEST", 7, "improvement"))

    def test_split_and_kinds_compose(self):
        a, b = _split_units()
        a["GATE:Z:0:0"], b["GATE:Z:0:0"] = _u("Z", 0, "z"), _u("Z", 1, "z")
        r = _cmp(a, b, kinds=["K"], split="GATE")
        self.assertEqual(r["n_used"], 7)


class TestSealHistoryThree(unittest.TestCase):
    def test_history_length_3_chained_to_bd3896cd(self):
        rows = [json.loads(l) for l in open(HIST) if l.strip()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]["new_prereg_sha256"].startswith("c015d66c"))
        self.assertTrue(rows[1]["new_prereg_sha256"].startswith("bd3896cd"))
        self.assertTrue(rows[2]["prev_prereg_sha256"].startswith("bd3896cd"))
        self.assertEqual(rows[2]["prev_entry_sha256"], rows[1]["entry_sha256"])
        self.assertEqual(rows[1]["prev_entry_sha256"], rows[0]["entry_sha256"])
        self.assertEqual(rows[2]["reason"], "JP1 fix: FLICKER strata + split-scoped claims")
        self.assertNotEqual(rows[2]["new_prereg_sha256"], rows[1]["new_prereg_sha256"])

    def test_head_equals_current_prereg_and_verifies(self):
        from eval.gui_synth import prereg
        prereg.verify()
        rows = [json.loads(l) for l in open(HIST) if l.strip()]
        self.assertEqual(rows[-1]["new_prereg_sha256"], prereg.sha256())


if __name__ == "__main__":
    unittest.main()
