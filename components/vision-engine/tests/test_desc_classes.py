"""Golden S1/S2 class tests (RPERF-2): every synthetic scenario goes through describe -> classify and is checked
against ground truth, frozen descriptor shas and per-frame class histograms (tests/golden/<scenario>/expect/).
Regenerate the goldens with VQA_REGEN=1 (writes only when set)."""
import os
import unittest
from pathlib import Path

import numpy as np

from vadapt.synth import synthetic as S
from visioncore import canon, change, desc, ids
from visioncore.grid import Grid
from visioncore.params import PARAMS

GOLDEN = Path(__file__).resolve().parent / "golden"
STABLE = ("clean_static", "gpu_jitter", "flicker", "small_overlap", "frozen_subregion")  # histograms pinned; drift/scroll depend on S2 tuning (FCORE)


def run(name, W=256, H=160, n=24, seed=1, params=None):
    fr, truth, _ = S.render_scenario(name, seed, W, H, n)
    g = Grid(W, H)
    shapes = (g.s, g.gw, g.gh)
    state = pp = pt = None
    cls, shas = [], []
    for f in fr:
        p = g.pad(f)
        tiles, eq = desc.describe_padded(p, g, pp, None, None, pt)
        y = change.luma_padded(f, g.s, g.gw, g.gh)
        if state is None:
            state = change.ClassState.init(tiles)
            out, state, _ = change.classify_first(state, tiles, y_cur=y)
        else:
            out, state, _ = change.classify_tiles(state, tiles, eq, y_cur=y, grid=shapes, params=params)
        cls.append(out["cls"].copy())
        shas.append(canon.sha256_obj(tiles.tobytes().hex()))
        pp, pt = p, tiles
    return g, truth, cls, shas


def hist(cls):
    return [np.bincount(c, minlength=7).tolist() for c in cls]


def truth_cells(g, truth):
    """-> [(frame, rank)] for the ground-truth fields."""
    cells = []
    for fid in truth["fields"]:
        d = ids.parse_any(fid)[1]
        cells.append((d["frame"], int(np.searchsorted(g.mortons, d["morton"]))))
    return cells


def recall(g, truth, cls, want):
    cells = truth_cells(g, truth)
    return sum(1 for f, r in cells if cls[f][r] == want) / max(1, len(cells))


class TestGoldenFiles(unittest.TestCase):
    def test_golden_descs_and_histograms(self):
        regen = os.environ.get("VQA_REGEN") == "1"
        for name in S.SCENARIOS:
            exp = GOLDEN / name / "expect"
            seed = canon.loads((exp / "synth.json").read_text())["seed"]
            g, truth, cls, shas = run(name, seed=seed)
            doc = {"schema": "vqa.synth-classes/v1", "scenario": name, "W": 256, "H": 160, "n": 24, "seed": seed,
                   "tiles_sha256": shas, "class_hist": hist(cls)}
            if regen:
                canon.write_atomic(exp / "classes.json", canon.dumps(doc).encode())
            stored = canon.loads((exp / "classes.json").read_text())
            self.assertEqual(stored["tiles_sha256"], shas, name + " S1 descriptors")
            if name in STABLE:
                self.assertEqual(stored["class_hist"], doc["class_hist"], name + " S2 histogram")


class TestTruth(unittest.TestCase):
    def test_controls_have_no_dirty_tiles(self):
        for name in ("clean_static", "gpu_jitter"):
            _, _, cls, _ = run(name)
            self.assertEqual(sum(int((c >= 2).sum()) for c in cls), 0, name)

    def test_flicker(self):
        g, truth, cls, _ = run("flicker")
        self.assertGreaterEqual(recall(g, truth, cls, 4), 0.9)

    def test_slow_drift_every_truth_tile_drifts_once(self):
        g, truth, cls, _ = run("slow_drift")
        ranks = {r for _, r in truth_cells(g, truth)}
        for r in ranks:
            self.assertTrue(any(c[r] == 6 for c in cls), r)

    def test_scroll_mostly_moved(self):
        _, _, cls, _ = run("scroll")
        self.assertGreaterEqual(sum(int((c == 3).sum()) for c in cls[1:]) * 10 ** 6 // (23 * 160), 800000)


def _active_cov_ppm(name="frozen_subregion"):
    fr, _, _ = S.render_scenario(name, 1, 256, 160, 24)
    g = Grid(256, 160)
    ch = np.zeros(g.N, bool)
    for a, b in zip(fr[10:], fr[9:-1]):
        d = (g.pad(a) != g.pad(b)).reshape(g.gh, g.s, g.gw, g.s, 3).any(axis=(1, 3, 4))
        ch |= d.reshape(-1)[g.flat]
    return int(ch.sum()) * 10 ** 6 // g.N


class TestFrozen(unittest.TestCase):
    def test_committed_scenario_reaches_frozen(self):
        cov = _active_cov_ppm()
        if cov < PARAMS["FROZEN_COV_PPM"]:
            self.skipTest("RPERF-2: synthetic.BAND gives %d ppm < FROZEN_COV_PPM (owner vadapt/synth)" % cov)
        g, truth, cls, _ = run("frozen_subregion")
        late = [(f, r) for f, r in truth_cells(g, truth) if f >= 16]
        self.assertTrue(all(cls[f][r] == 5 for f, r in late))

    def test_enlarged_band_freezes_every_truth_tile(self):
        old = S.BAND
        S.BAND = (0, 96, 256, 64)
        try:
            self.assertGreaterEqual(_active_cov_ppm(), 350000)
            g, truth, cls, _ = run("frozen_subregion")
            late = [(f, r) for f, r in truth_cells(g, truth) if f >= 16]
            self.assertTrue(late and all(cls[f][r] == 5 for f, r in late))
            # mutant: coverage gate that can never open must remove every FROZEN tile
            _, _, cls2, _ = run("frozen_subregion", params=dict(PARAMS, FROZEN_COV_PPM=10 ** 6))
            self.assertEqual(sum(int((c == 5).sum()) for c in cls2), 0)
        finally:
            S.BAND = old


if __name__ == "__main__":
    unittest.main()
