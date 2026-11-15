"""P1 W1 labels: self_check clean on all KINDS; negative controls (a) class, (b) mv, (c) dropped tile, (d) float refusal; M10 order."""
import copy
import unittest

import numpy as np

from tests.gui_synth_common import SMALL


def _setup(kind, k, w=320, h=180, n=24, fps=60):
    from eval.gui_synth import gen, labels, scenes
    from visioncore.grid import Grid
    sc = scenes.build(kind, k, w=w, h=h, fps=fps, n=n)
    g = Grid(w, h)
    frames = [a.copy() for _, a in gen.render(sc)]
    return sc, g, frames, labels.derive(sc, g)


def _codes(res):
    return {c for c, _ in res}


class TestSelfCheckClean(unittest.TestCase):
    def test_all_kinds_two_seeds_small(self):
        from eval.gui_synth import labels, scenes
        for kind in scenes.KINDS:
            for k in (0, 1):
                sc, g, frames, ev = _setup(kind, k)
                self.assertEqual(labels.self_check(sc, frames, ev), [], "%s k=%d" % (kind, k))

    def test_all_kinds_one_seed_1080p(self):
        from eval.gui_synth import labels, scenes
        for kind in scenes.KINDS:
            sc, g, frames, ev = _setup(kind, 0, 1920, 1080, 10)
            self.assertEqual(labels.self_check(sc, frames, ev), [], kind)

    def test_event_schema(self):
        sc, g, frames, ev = _setup("scroll_small", 0)
        self.assertTrue(ev)
        for e in ev:
            for key in ("scene_id", "event_id", "f0", "f1", "tiles", "ideal", "v1_expected", "v1_basis", "mv", "op"):
                self.assertIn(key, e)
            self.assertTrue(all(0 <= t < g.N for t in e["tiles"]))
            self.assertLessEqual(e["f0"], e["f1"])

    def test_morton_tile_indices(self):
        """M10: tile indices are Morton ranks (grid.order[ty,tx]), not raster ty*gw+tx."""
        from visioncore.grid import Grid
        sc, g, frames, ev = _setup("canvas_anim", 0)
        s = g.s
        changed = np.zeros(g.N, bool)
        for a, b in zip(frames, frames[1:]):
            d = (a != b).any(axis=2)
            for ty in range(g.gh):
                for tx in range(g.gw):
                    if d[ty * s:(ty + 1) * s, tx * s:(tx + 1) * s].any():
                        changed[g.order[ty, tx]] = True
        labelled = {t for e in ev for t in e["tiles"]}
        self.assertTrue({int(i) for i in np.nonzero(changed)[0]} <= labelled)


class TestNegativeControls(unittest.TestCase):
    def test_a_class_flip(self):
        from eval.gui_synth import labels
        from visioncore import change as C
        sc, g, frames, ev = _setup("window_drag", 0)
        i = next(j for j, e in enumerate(ev) if e["ideal"] == C.C_MOVED)
        bad = copy.deepcopy(ev)
        bad[i]["ideal"] = C.C_CHANGED
        self.assertIn("E_LABEL_CLASS", _codes(labels.self_check(sc, frames, bad)))

    def test_b_mv_off_by_one(self):
        from eval.gui_synth import labels
        from visioncore import change as C
        sc, g, frames, ev = _setup("scroll_small", 0)
        i = next(j for j, e in enumerate(ev) if e["ideal"] == C.C_MOVED and e["mv"])
        bad = copy.deepcopy(ev)
        bad[i]["mv"] = [bad[i]["mv"][0], bad[i]["mv"][1] + 1]
        self.assertIn("E_LABEL_MV", _codes(labels.self_check(sc, frames, bad)))

    def test_c_dropped_tile_is_gap(self):
        from eval.gui_synth import labels
        from visioncore import change as C
        sc, g, frames, ev = _setup("canvas_anim", 0)
        i = next(j for j, e in enumerate(ev) if e["ideal"] == C.C_CHANGED and len(e["tiles"]) > 1)
        bad = copy.deepcopy(ev)
        bad[i]["tiles"] = bad[i]["tiles"][1:]
        self.assertIn("E_LABEL_GAP", _codes(labels.self_check(sc, frames, bad)))

    def test_extra_tile_is_tile_error(self):
        from eval.gui_synth import labels
        sc, g, frames, ev = _setup("static_clean", 0)
        bad = copy.deepcopy(ev) or [{"scene_id": sc.get("scene_id"), "event_id": 0, "f0": 1, "f1": 2, "tiles": [], "ideal": 2,
                                     "v1_expected": 2, "v1_basis": "HYP", "mv": None, "op": "x"}]
        bad[0]["tiles"] = list(bad[0]["tiles"]) + [0]
        bad[0]["ideal"] = 2
        self.assertTrue(_codes(labels.self_check(sc, frames, bad)) & {"E_LABEL_TILE", "E_LABEL_CLASS"})

    def test_d_float_easing_refused(self):
        from eval.gui_synth import gen, scenes
        sc = scenes.build("scroll_smooth", 0, **SMALL)
        bad = copy.deepcopy(sc)

        def poison(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("easing", "ease", "table") and isinstance(v, list) and v:
                        o[k] = [float(x) for x in v]
                        return True
                    if poison(v):
                        return True
            elif isinstance(o, list):
                return any(poison(v) for v in o)
            return False
        self.assertTrue(poison(bad), "scroll_smooth scene must carry an integer easing table")
        with self.assertRaises(Exception) as cm:
            list(gen.render(bad))
        self.assertIn("E_GEN_FLOAT", str(getattr(cm.exception, "code", "")) + str(cm.exception))


if __name__ == "__main__":
    unittest.main()
