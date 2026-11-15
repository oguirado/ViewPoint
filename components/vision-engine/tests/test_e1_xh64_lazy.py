"""E1 lazy xh64 (SPEC_INT I2, T-I2): hash only cls >= 2 tiles and every tile of a keyframe; UNCHANGED/JITTER copy the
previous xh64. Mutant: skip without copy (zeros) -> killed. Golden: tests/golden/<scenario>/expect/xh64_lazy.json
(regen with VQA_REGEN=1). Perf is logged only (VQA_PERF_LOG=1)."""
import os
import time
import unittest
from pathlib import Path

import numpy as np

from visioncore import canon, change, desc
from visioncore.grid import Grid
from vadapt.synth import synthetic as S

GOLDEN = Path(__file__).resolve().parent / "golden"
W, H, N = 256, 160, 24


def _run(name, seed, lazy, skip_copy=False):
    fr, _, _ = S.render_scenario(name, seed, W, H, N)
    g = Grid(W, H)
    shapes = (g.s, g.gw, g.gh)
    state = pp = pt = px = None
    outs = []
    for f in fr:
        p = g.pad(f)
        y = desc.luma_of_padded(p)
        tiles, eq = desc.describe_padded(p, g, pp, px if not lazy else None, None, pt, y_plane=y, lazy_xh=lazy)
        if state is None:
            state = change.ClassState.init(tiles)
            out, state, _ = change.classify_first(state, tiles, y_cur=y)
        else:
            out, state, _ = change.classify_tiles(state, tiles, eq, y_cur=y, grid=shapes, params=None)
        out = out.copy()
        if lazy:
            desc.fill_xh64(out, p, g, None if skip_copy else px)
            if skip_copy and px is not None:  # mutant: lazy skip without copy
                m = out["cls"] < 2
                out["xh64"][m] = 0
        outs.append(out)
        pp, pt, px = p, tiles, out["xh64"]
    return g, fr, outs


class TestLazyXh64(unittest.TestCase):
    def test_dirty_exact_clean_copied_all_scenarios(self):
        for name in S.SCENARIOS:
            seed = canon.loads((GOLDEN / name / "expect" / "synth.json").read_text())["seed"]
            g, fr, outs = _run(name, seed, True)
            for k, o in enumerate(outs):
                p = g.pad(fr[k])
                for r in range(g.N):
                    ty, tx = int(g.ty[r]), int(g.tx[r])
                    true = desc.xh64_of(p[ty * g.s:(ty + 1) * g.s, tx * g.s:(tx + 1) * g.s])
                    cur = int(o["xh64"][r])
                    if k == 0 or o["cls"][r] >= 2:
                        self.assertEqual(cur, true, (name, k, r, "hashed"))
                    else:                    # T-I2: cls 0/1 copy the previous xh64 ...
                        self.assertEqual(cur, int(outs[k - 1]["xh64"][r]), (name, k, r, "copy"))
                        pb = g.pad(fr[k - 1])[ty * g.s:(ty + 1) * g.s, tx * g.s:(tx + 1) * g.s]
                        if (o["cls"][r] == 0 and desc.xh64_of(pb) == int(outs[k - 1]["xh64"][r])
                                and np.array_equal(p[ty * g.s:(ty + 1) * g.s, tx * g.s:(tx + 1) * g.s], pb)):
                            self.assertEqual(cur, true, (name, k, r, "byte-equal UNCHANGED is exact"))

    def test_mutant_skip_without_copy_killed(self):
        g, fr, outs = _run("gpu_jitter", 1, True, skip_copy=True)
        bad = any(int(o["xh64"][r]) != int(outs[k - 1]["xh64"][r])
                  for k, o in enumerate(outs) if k for r in range(g.N) if o["cls"][r] < 2)
        self.assertTrue(bad)  # the invariant check above would fail on this mutant

    def test_only_jitter_differs_from_eager(self):
        for name in S.SCENARIOS:
            g, _, lz = _run(name, 1, True)
            _, _, eg = _run(name, 1, False)
            for k, (a, b) in enumerate(zip(lz, eg)):
                diff = a["xh64"] != b["xh64"]
                self.assertTrue((a["cls"][diff] <= 1).all(), (name, k))   # dirty tiles never differ
                self.assertTrue(k > 0 or not diff.any())                   # keyframe 0 fully hashed
                for f in a.dtype.names:
                    if f != "xh64":
                        self.assertTrue((a[f] == b[f]).all(), (name, k, f))

    def test_golden(self):
        regen = os.environ.get("VQA_REGEN") == "1"
        for name in S.SCENARIOS:
            exp = GOLDEN / name / "expect"
            seed = canon.loads((exp / "synth.json").read_text())["seed"]
            _, _, outs = _run(name, seed, True)
            doc = {"schema": "vqa.synth-xh64/v1", "scenario": name, "W": W, "H": H, "n": N, "seed": seed,
                   "tiles_sha256": [canon.sha256_obj(o.tobytes().hex()) for o in outs]}
            if regen:
                canon.write_atomic(exp / "xh64_lazy.json", canon.dumps(doc).encode())
            self.assertEqual(canon.loads((exp / "xh64_lazy.json").read_text()), doc, name)

    def test_perf_log(self):
        if os.environ.get("VQA_PERF_LOG") != "1":
            self.skipTest("log only")
        for lazy in (False, True):
            t = time.perf_counter()
            _run("gpu_jitter", 1, lazy)
            print("lazy" if lazy else "eager", round((time.perf_counter() - t) * 1000), "ms")


if __name__ == "__main__":
    unittest.main()
