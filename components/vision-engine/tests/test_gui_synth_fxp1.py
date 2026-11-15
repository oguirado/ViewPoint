"""FXP1 FT: contracts added by the ADV_P1 fix wave (F1,F3,F4,F5,F6,F7,F8,F2,Q6).
Interfaces as landed by FA/FB/FC: scenes.build(..., split=), splits.PARAMETER_SETS, prereg.seal(..., reseal=, reason=) with the
SEAL_HISTORY.jsonl chain (entry_sha256/prev_entry_sha256/prev_prereg_sha256/new_prereg_sha256), signflip.compare(..., kinds=)
with param_bucket strata, n_used/n_dropped, p_scene/p_stratified."""
import gzip
import json
import os
import shutil
import tempfile
import unittest
from fractions import Fraction as F
from unittest import mock

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELD = ("GATE", "TEST")


def _build(kind, k, split, **kw):
    from eval.gui_synth import scenes
    return scenes.build(kind, k, split=split, **kw)


def _vals(kind, split, key, w=320, h=180):
    from eval.gui_synth import splits
    return {_build(kind, k, split, w=w, h=h, n=30)[key] for k in splits.seeds(split)}


# ---- F1 / F5 / F6 / F7 : generator + labels -------------------------------------------------------------------------
class TestSupportAndMotion(unittest.TestCase):
    def test_f1_caret_and_typing_have_ideal_moved_support(self):
        from eval.gui_synth import labels, scenes
        from visioncore import change as C
        from visioncore.grid import Grid
        for kind in ("caret_move", "typing"):
            tot = 0
            for k in range(6):
                sc = scenes.build(kind, k, w=320, h=180, fps=60, n=30)
                ev = labels.derive(sc, Grid(320, 180))
                tot += sum(len(e["tiles"]) for e in ev if e["ideal"] == C.C_MOVED)
            self.assertGreater(tot, 0, kind + ": support_moved must be > 0")

    def test_f1_support_moved_in_catalogue_1080p(self):
        from eval.gui_synth import labels, scenes
        from visioncore import change as C
        from visioncore.grid import Grid
        sc = scenes.build("caret_move", 0, w=1920, h=1080, fps=60, n=16)
        ev = labels.derive(sc, Grid(1920, 1080))
        self.assertTrue(any(e["ideal"] == C.C_MOVED for e in ev))

    def test_f5_motion_spread_over_several_frames(self):
        from eval.gui_synth import scenes
        for kind in ("caret_move", "typing", "window_drag"):
            sc = scenes.build(kind, 0, w=320, h=180, fps=60, n=120)
            moving = [i for i in range(1, 120) if scenes.motion(sc, i) != (0, 0)]
            self.assertGreaterEqual(len(moving), 4, kind)
            self.assertGreater(max(moving) - min(moving), 1, kind)

    def test_f6_velocity_scales_with_grid_and_keeps_sub_and_super_tile(self):
        from eval.gui_synth import splits
        from visioncore.grid import Grid
        for w, h in ((320, 180), (1920, 1080)):
            s = Grid(w, h).s
            dys = {abs(_build("scroll_small", k, "DEV", w=w, h=h, n=30)["dy"]) for k in splits.seeds("DEV")}
            dys |= {abs(_build("scroll_large", k, "DEV", w=w, h=h, n=30)["dy"]) for k in splits.seeds("DEV")}
            self.assertTrue(any(d < s for d in dys), (w, "sub-tile", s, sorted(dys)))
            self.assertTrue(any(d > s for d in dys), (w, "super-tile", s, sorted(dys)))
        a = _vals("scroll_small", "DEV", "dy", 320, 180)
        b = _vals("scroll_small", "DEV", "dy", 1920, 1080)
        self.assertNotEqual(a, b, "velocities must differ between resolutions (scaled with s)")

    def test_f7_blink_fps_varies_with_k_including_odd_period(self):
        from eval.gui_synth import splits
        sc = [_build("caret_blink", k, "DEV", w=320, h=180, n=30) for k in splits.seeds("DEV")]
        self.assertEqual({s["fps"] for s in sc} & {30, 60}, {30, 60})
        self.assertTrue(any(s["period"] % 2 for s in sc), "an odd blink period must be covered")


# ---- F4 : disjoint per-split parameter sets -------------------------------------------------------------------------
class TestDisjointSplitParams(unittest.TestCase):
    CASES = (("scroll_small", "dy"), ("scroll_large", "dy"), ("scroll_h", "dx"), ("caret_blink", "half_period_ms"))

    def test_dev_vs_heldout_values_disjoint(self):
        for kind, key in self.CASES:
            dev = _vals(kind, "DEV", key)
            self.assertTrue(dev, kind)
            for sp in HELD:
                held = _vals(kind, sp, key)
                self.assertTrue(held, (kind, sp))
                self.assertEqual(dev & held, set(), (kind, key, sp))

    def test_scene_id_carries_split(self):
        for sp in ("DEV",) + HELD:
            from eval.gui_synth import splits
            k = splits.seeds(sp)[0]
            self.assertIn(sp, _build("scroll_small", k, sp)["scene_id"])

    def test_prereg_lists_split_params(self):
        p = json.load(open(os.path.join(REPO, "eval/gui_synth/PREREG.json")))
        from eval.gui_synth import splits
        self.assertEqual(p.get("parameter_sets"), splits.PARAMETER_SETS)

    def test_parameter_sets_value_disjoint_across_splits(self):
        from eval.gui_synth import splits
        ps = splits.PARAMETER_SETS
        for kind in ps["DEV"]:
            for name in ps["DEV"][kind]:
                if name in ("fps", "box_divisor"):
                    continue  # structural axes, not tuned values
                sets = [set(ps[sp][kind][name]) for sp in ("DEV", "GATE", "TEST")]
                for i in range(3):
                    for j in range(i + 1, 3):
                        self.assertEqual(sets[i] & sets[j], set(), (kind, name, i, j))


# ---- F3 : seal guard + history chain ---------------------------------------------------------------------------------
def _fake_table():
    from eval.gui_synth import scenes, splits
    ids = [_build(kd, kk, "DEV", w=320, h=180, n=120)["scene_id"] for kd in scenes.KINDS for kk in splits.seeds("DEV")]
    return {sid: ["%064x" % (i + 1)] * 120 for i, sid in enumerate(ids)}


class _SealEnv:
    """Scratch copy of the real PREREG.json + SEAL_HISTORY.jsonl (the pinned original must be in the chain)."""
    def __enter__(self):
        os.makedirs(os.path.join(REPO, "build", "room_FT"), exist_ok=True)
        self.d = tempfile.mkdtemp(prefix="fxp1_seal_", dir=os.path.join(REPO, "build", "room_FT"))
        for lane in ("FA", "FC"):
            with open(os.path.join(self.d, "bus_%s.md" % lane), "w") as f:
                f.write("## %sDONE\n" % lane)
        for n in ("PREREG.json", "SEAL_HISTORY.jsonl"):
            shutil.copy(os.path.join(REPO, "eval/gui_synth", n), os.path.join(self.d, n))
        self.path = os.path.join(self.d, "PREREG.json")
        self.hist = os.path.join(self.d, "SEAL_HISTORY.jsonl")
        self.table = _fake_table()
        return self

    def __exit__(self, *a):
        shutil.rmtree(self.d, ignore_errors=True)

    def seal(self, **kw):
        from eval.gui_synth import prereg
        return prereg.seal(self.path, bus_dir=self.d, dev_table=self.table, **kw)

    def rows(self):
        return [json.loads(l) for l in open(self.hist) if l.strip()]

    def write(self, rows):
        from eval.gui_synth import prereg
        out = []
        for r in rows:  # entry hashes are recomputed so only the structural defect under test remains
            r = dict(r)
            r["entry_sha256"] = prereg._entry_sha(r)
            out.append(r)
        open(self.hist, "w").write("".join(json.dumps(r, sort_keys=True) + "\n" for r in out))


class TestSealHistory(unittest.TestCase):
    def test_seal_refused_without_reseal(self):
        from eval.gui_synth import prereg
        with _SealEnv() as e:
            before = (open(e.path, "rb").read(), open(e.hist, "rb").read())
            with self.assertRaises(prereg.PreregError) as cm:
                e.seal()
            self.assertEqual(cm.exception.rc, 5)
            with self.assertRaises(prereg.PreregError):
                e.seal(reseal=True)  # reason missing
            with self.assertRaises(prereg.PreregError):
                e.seal(reseal=True, reason="  ")
            self.assertEqual((open(e.path, "rb").read(), open(e.hist, "rb").read()), before)

    def test_reseal_appends_exactly_one_chained_line(self):
        from eval.gui_synth import prereg
        with _SealEnv() as e:
            old = e.rows()
            old_sha = prereg.sha256(e.path)
            e.seal(reseal=True, reason="because")
            rows = e.rows()
            self.assertEqual(len(rows), len(old) + 1)
            self.assertEqual(rows[:len(old)], old)  # append-only
            last = rows[-1]
            self.assertEqual(last["prev_prereg_sha256"], old_sha)
            self.assertEqual(last["prev_entry_sha256"], old[-1]["entry_sha256"])
            self.assertEqual(last["new_prereg_sha256"], prereg.sha256(e.path))
            self.assertEqual(last["reason"], "because")
            self.assertIn("sources_tree_sha256", last)
            self.assertTrue(prereg.verify(e.path))

    def test_tampered_history_fails_verify(self):
        from eval.gui_synth import prereg
        with _SealEnv() as e:
            e.seal(reseal=True, reason="r")
            good = e.rows()
            self.assertTrue(prereg.verify(e.path))
            cases = (("broken prev sha", [good[0], dict(good[1], prev_prereg_sha256="0" * 64)]),
                     ("broken entry link", [good[0], dict(good[1], prev_entry_sha256="0" * 64)]),
                     ("line skipped", [good[1]]),
                     ("last mismatches prereg", [good[0], dict(good[1], new_prereg_sha256="f" * 64)]),
                     ("original replaced", [dict(good[0], new_prereg_sha256="e" * 64), good[1]]))
            for name, rows in cases:
                e.write(rows)
                with self.assertRaises(prereg.PreregError, msg=name) as cm:
                    prereg.verify(e.path)
                self.assertEqual(cm.exception.rc, 5, name)
            raw = [json.dumps(r, sort_keys=True) for r in good]
            open(e.hist, "w").write(raw[0] + "\n" + raw[1].replace("\"r\"", "\"x\"") + "\n")  # edit without rehash
            with self.assertRaises(prereg.PreregError):
                prereg.verify(e.path)
            e.write(good)
            self.assertTrue(prereg.verify(e.path))

    def test_real_history_first_line_is_e738181_seal(self):
        rows = [json.loads(l) for l in open(os.path.join(REPO, "eval/gui_synth/SEAL_HISTORY.jsonl")) if l.strip()]
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(rows[0]["new_prereg_sha256"].startswith("c015d66c"))
        self.assertIsNone(rows[0]["prev_entry_sha256"])

    def test_real_verify_history_length_3(self):
        """Exit gate: only true after the final reseal of the wave."""
        from eval.gui_synth import prereg
        prereg.verify()
        n = sum(1 for l in open(os.path.join(REPO, "eval/gui_synth/SEAL_HISTORY.jsonl")) if l.strip())
        self.assertEqual(n, 3)  # JFXP1 third link


# ---- F8 : .json.gz capture tables ------------------------------------------------------------------------------------
class TestCaptureGz(unittest.TestCase):
    def test_json_gz_read(self):
        from eval.gui_synth import run
        os.makedirs(os.path.join(REPO, "build", "room_FT"), exist_ok=True)
        with tempfile.TemporaryDirectory(dir=os.path.join(REPO, "build", "room_FT")) as d:
            with gzip.open(os.path.join(d, "r.json.gz"), "wt") as f:
                json.dump({"capture_shas": {"GATE": {"s": ["ab"]}}}, f)
            with open(os.path.join(d, "p.json"), "w") as f:
                json.dump({"capture_shas": {"DEV": {"s": ["cd"]}}}, f)
            t = run._capture_tables(d)
            self.assertEqual(set(t), {"GATE", "DEV"})


# ---- F2 / Q6 : compare with kinds, undefined metrics, stratification -------------------------------------------------
def _res(units):
    return {"prereg_sha": "x", "scene_units": units}


def _cmp(a, b, metric="m", **kw):
    from eval.gui_synth import prereg, signflip
    with mock.patch.object(prereg, "verify", return_value={}), mock.patch.object(prereg, "sha256", return_value="x"):
        return signflip.compare(_res(a), _res(b), metric, **kw)


def _u(kind, v, param=0):
    return {"kind": kind, "param_bucket": {"p": param}, "metrics": {"m": v}}


class TestCompare(unittest.TestCase):
    def _pair(self):
        a = {"d%d" % i: _u("D", 0 if i % 2 else 1, i % 2) for i in range(6)}
        a.update({"u%d" % i: _u("U", None) for i in range(3)})
        b = {k: dict(v, metrics={"m": (None if v["metrics"]["m"] is None else v["metrics"]["m"] + 1)}) for k, v in a.items()}
        return a, b

    def test_kinds_filter_and_n_used(self):
        a, b = self._pair()
        r = _cmp(a, b, kinds=["D"])
        self.assertEqual((r["n_used"], r["n_dropped"]), (6, 0))

    def test_undefined_in_both_dropped(self):
        a, b = self._pair()
        r = _cmp(a, b)
        self.assertEqual((r["n_used"], r["n_dropped"]), (6, 3))

    def test_undefined_in_one_is_error(self):
        from eval.gui_synth.score import ScoreError
        a, b = self._pair()
        b["u0"]["metrics"]["m"] = 1
        with self.assertRaises(ScoreError) as cm:
            _cmp(a, b)
        self.assertIn("E_SCORE_METRIC", str(cm.exception) + str(getattr(cm.exception, "code", "")))

    def test_stratified_p_not_inflated_by_correlated_scenes(self):
        a = {"s%d" % i: _u("K", 0, i // 10) for i in range(20)}  # 2 strata of 10 scenes
        b = {k: _u("K", 1, v["param_bucket"]["p"]) for k, v in a.items()}
        r = _cmp(a, b)
        self.assertEqual(r["p_scene"], "1/524288")  # 2/2**20
        self.assertEqual(r["p_stratified"], "1/2")  # 2 strata -> +all / -all of 4 sign patterns
        self.assertIn("no claim", r["claim"])  # the claim follows the stratified p


if __name__ == "__main__":
    unittest.main()
