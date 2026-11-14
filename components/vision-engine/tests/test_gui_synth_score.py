"""P1 W3 scorer: hand-built fixture with known confusion/precision/recall/ppm/mv exactness; legacy view == score_w3; refusals (M6, M7)."""
import collections
import unittest
from fractions import Fraction as F

import numpy as np

F_, N_ = 4, 3


def ev(eid, f0, f1, tiles, ideal, v1, mv=None, scene="B", basis="HYP", **kw):
    d = {"scene_id": scene, "event_id": eid, "f0": f0, "f1": f1, "tiles": tiles, "ideal": ideal, "v1_expected": v1,
         "v1_basis": basis, "mv": mv, "op": "t"}
    d.update(kw)
    return d


def scene_b():
    """B: MOVED event f0=1,f1=3 (half-open) on tiles 0,1 with mv (0,4); 12 tile-frames; row 3 of the confusion is non-symmetric."""
    cls = np.zeros((F_, N_), np.uint8)
    dx = np.zeros((F_, N_), np.int64)
    dy = np.zeros((F_, N_), np.int64)
    cls[1, 0], dy[1, 0] = 3, 4
    cls[1, 1], dy[1, 1] = 3, 5      # MOVED but wrong mv
    cls[2, 0] = 2                   # CHANGED (miss)
    cls[2, 1], dy[2, 1] = 3, 4
    cls[3, 2] = 4                   # spurious FLICKER on an unlabelled tile-frame
    return cls, dx, dy, [ev(0, 1, 3, [0, 1], 3, 2, mv=[0, 4])]


def score_b():
    from eval.gui_synth import score
    cls, dx, dy, evs = scene_b()
    return score.score(cls, dx, dy, evs, scene={"scene_id": "B", "kind": "scroll_small", "n": F_}, engine_ms=40, generator_ms=8)


class TestScore(unittest.TestCase):
    def test_confusion_ideal_rows_true_cols_pred(self):
        r = score_b()
        c = r["confusion_ideal"]
        self.assertEqual(c[3][3], 3)          # MOVED->MOVED
        self.assertEqual(c[3][2], 1)          # MOVED->CHANGED
        self.assertEqual(c[0][0], 7)
        self.assertEqual(c[0][4], 1)          # UNCHANGED->FLICKER
        self.assertEqual(sum(map(sum, c)), F_ * N_)
        self.assertNotEqual(c[3][2], c[2][3])  # M6: transposition changes this cell
        self.assertEqual(r["confusion"], c)

    def test_confusion_vs_v1_expected(self):
        c = score_b()["confusion_v1_expected"]
        self.assertEqual(c[2][3], 3)
        self.assertEqual(c[2][2], 1)

    def test_precision_recall_exact_fractions(self):
        pc = score_b()["per_class"]
        self.assertEqual(pc["MOVED"]["recall"]["fraction"], "3/4")
        self.assertEqual(pc["MOVED"]["recall"]["permille"], 750)
        self.assertEqual(pc["MOVED"]["precision"]["fraction"], "1/1")
        self.assertEqual(pc["FLICKER"]["precision"]["fraction"], "0/1")
        self.assertEqual(pc["UNCHANGED"]["precision"]["fraction"], "1/1")

    def test_mv_exactness(self):
        r = score_b()
        self.assertEqual(r["mv_correct"], 2)
        self.assertEqual(r["mv_total"], 4)
        self.assertEqual(r["metrics"]["mv_exact"], "1/2")

    def test_fp_ppm_over_tile_frames_on_static(self):
        from eval.gui_synth import score
        cls = np.zeros((F_, N_), np.uint8)
        cls[2, 1] = 2
        z = np.zeros_like(cls, dtype=np.int64)
        r = score.score(cls, z, z, [], scene={"scene_id": "A", "kind": "static_clean", "n": F_})
        self.assertEqual(r["fp_count"], 1)
        self.assertEqual(r["fp_tile_frames"], 12)
        self.assertEqual(r["fp_ppm"], 83333)               # 10^6/12, not 10^6/4 (M7)
        self.assertEqual(r["fp_ppm_exact"], "250000/3")
        r2 = score.score(cls, z, z, [], scene={"scene_id": "A2", "kind": "scroll_small", "n": F_})
        self.assertEqual(r2["fp_tile_frames"], 0)           # FP ppm only on static_clean + aa_jitter

    def test_timings_separate(self):
        r = score_b()
        self.assertEqual(r["ms_per_frame"], 10)
        self.assertEqual(r["generator_ms_per_frame"], 2)

    def test_legacy_view_equals_score_w3(self):
        """Replay of score_w3.py's per-event window rule (first 8 frames from f0, rect tiles) on a longer fixture."""
        from eval.gui_synth import score
        names = score.NAMES
        EXP = {"CHANGED": {2}, "MOVED": {3}, "FLICKER": {4}, "CHANGED_REGION": {2, 3}, "NOT_CHANGED": {0, 1}}
        rng = np.random.RandomState(5)
        cls = rng.randint(0, 5, size=(20, 6)).astype(np.uint8)
        z = np.zeros(cls.shape, np.int64)
        events = [ev(0, 2, 6, [0, 1, 2], 2, 2, scene="L", expected_kind="CHANGED_REGION"),
                  ev(1, 10, 14, [3, 4], 4, 4, scene="L", expected_kind="FLICKER")]
        r = score.score(cls, z, z, events, scene={"scene_id": "L", "kind": "typing", "n": 20})
        for e, row in zip(events, r["legacy"]):
            win = cls[e["f0"]:min(20, e["f0"] + 8)][:, e["tiles"]]
            h = collections.Counter(int(x) for x in win.ravel())
            tot = sum(h.values()) or 1
            ok = sum(v for k, v in h.items() if k in EXP[e["expected_kind"]])
            anychg = sum(v for k, v in h.items() if k >= 2)
            self.assertEqual(row["hit_frac"], round(ok / tot, 3))
            self.assertEqual(row["any_change_frac"], round(anychg / tot, 3))
            self.assertEqual(row["detected"], anychg > 0)
            self.assertEqual(row["hist"], {names[k]: v for k, v in sorted(h.items())})

    def test_three_scene_aggregate(self):
        from eval.gui_synth import score
        a = score_b()
        z = np.zeros((F_, N_), np.uint8)
        zz = np.zeros((F_, N_), np.int64)
        s1 = score.score(z, zz, zz, [], scene={"scene_id": "A", "kind": "static_clean", "n": F_})
        s2 = score.score(z, zz, zz, [], scene={"scene_id": "C", "kind": "aa_jitter", "n": F_})
        agg = score.aggregate([a, s1, s2])
        self.assertEqual(sum(map(sum, agg["confusion_ideal"])), 3 * F_ * N_)
        self.assertEqual(agg["confusion_ideal"][3][3], 3)
        self.assertEqual(set(agg["scene_units"]), {"A", "B", "C"})


class TestRefusals(unittest.TestCase):
    def test_foreign_scene_labels_refused(self):
        from eval.gui_synth import score
        cls, dx, dy, evs = scene_b()
        evs[0]["scene_id"] = "OTHER"
        with self.assertRaises(score.ScoreError) as cm:
            score.score(cls, dx, dy, evs, scene={"scene_id": "B", "kind": "scroll_small", "n": F_})
        self.assertEqual(cm.exception.code, "E_SCORE_SCENE")
        self.assertEqual(cm.exception.rc, 5)

    def test_foreign_frame_sha_refused(self):
        from eval.gui_synth import score
        cls, dx, dy, evs = scene_b()
        evs[0]["frame_shas"] = ["a" * 64] * F_
        with self.assertRaises(score.ScoreError) as cm:
            score.score(cls, dx, dy, evs, scene={"scene_id": "B", "kind": "scroll_small", "n": F_}, frame_shas=["b" * 64] * F_)
        self.assertEqual(cm.exception.code, "E_SCORE_SCENE")

    def test_missing_scene_identity_refused(self):
        from eval.gui_synth import score
        cls, dx, dy, evs = scene_b()
        with self.assertRaises(score.ScoreError) as cm:
            score.score(cls, dx, dy, evs)
        self.assertEqual(cm.exception.code, "E_SCORE_SCENE")

    def test_prereg_sha_mismatch_refused_on_gate(self):
        from eval.gui_synth import score
        cls, dx, dy, evs = scene_b()
        with self.assertRaises(Exception) as cm:
            score.score(cls, dx, dy, evs, scene={"scene_id": "B", "kind": "scroll_small", "n": F_}, split="GATE", prereg_sha="0" * 64)
        self.assertIn(getattr(cm.exception, "code", ""), ("E_PREREG_SHA", "E_PREREG_MISSING", "E_PREREG_FORMAT"))

    def test_comparison_refuses_result_with_other_prereg(self):
        from eval.gui_synth import score, signflip
        r = score_b()
        r["prereg_sha"] = "0" * 64
        with self.assertRaises(Exception) as cm:
            signflip.compare(r, dict(r), "recall_MOVED")
        self.assertIn(getattr(cm.exception, "code", ""), ("E_PREREG_SHA", "E_PREREG_MISSING", "E_PREREG_FORMAT"))


if __name__ == "__main__":
    unittest.main()
