"""W2/C2: JC-6 re-verify (4080x16 grid ids), X1-style determinism of vcore analyze, ADV/RCORE conformance spot checks."""
import unittest

import numpy as np

from tests.test_e1_change_pipeline import blob_sha
from tests.test_pyramid import make
from vadapt.synth import synthetic as syn
from visioncore import frame as fr, ids, layout, pipeline
from visioncore.pyramid import Pyramid


def clean_source(scenario, seed, W=256, H=160, n=16):
    frs, _, _ = syn.render_scenario(scenario, seed, W, H, n)
    return fr.MemoryFrameSource([fr.Frame(i, i * 33333, i, np.ascontiguousarray(r)) for i, r in enumerate(frs)],
                                {"width": W, "height": H})


class TestJC6WideGrid(unittest.TestCase):
    def test_grid_4080x16_ident_and_axes(self):
        s, gw, gh = layout.grid_params(4080, 16)
        self.assertEqual((s, gw, gh), (16, 255, 1))
        self.assertEqual(layout.grid_ident(4080, 16), "grid:4080x16:s16")

    def test_every_selected_node_formats_and_parses(self):
        _, _, p = make(4080, 16, {(x, 0): 100 + x % 50 for x in range(0, 255, 3)})
        self.assertEqual(p.L, 8)
        for B in (1, 2, 8, 64, 500):
            nodes = p.select(B, 0)
            # root level 8 is unnameable while ids.node_id caps at L7 (REG-REQ on bus_C2): forest of <= 4 level-7 nodes
            self.assertLessEqual(len(nodes), B if p.id_cap == p.L else max(B, 4))
            for lvl, m0, mx, cnt in nodes:
                kind, f = ids.parse_any(p.node_id(3, lvl, m0 >> (2 * lvl)))
                self.assertEqual((kind, f["level"], f["morton"]), ("node", lvl, m0))

    def test_select_one_covers_all_dirty_tiles(self):
        _, _, p = make(4080, 16, {(0, 0): 200, (254, 0): 90})
        nodes = p.select(1, 0)
        for tx in (0, 254):
            m = layout.morton(tx, 0)
            self.assertTrue(any(n[1] <= m < n[1] + 4 ** n[0] for n in nodes))


class TestX1SelfCheck(unittest.TestCase):
    def test_six_clean_captures_three_runs_identical(self):
        caps = [("clean_static", 1), ("clean_static", 2), ("clean_static", 3),
                ("gpu_jitter", 1), ("gpu_jitter", 2), ("gpu_jitter", 3)]
        for sc, seed in caps:
            with self.subTest(sc=sc, seed=seed):
                shas = {blob_sha(pipeline.analyze(clean_source(sc, seed))) for _ in range(3)}
                self.assertEqual(len(shas), 1)


if __name__ == "__main__":
    unittest.main()
