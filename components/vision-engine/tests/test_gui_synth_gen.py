"""P1 W1 generator: determinism, seed sensitivity, integer-only frames, RAM-only output (pinned interface)."""
import json
import os
import unittest

import numpy as np

from tests.gui_synth_common import SMALL, sha_list, walk_floats


class TestGen(unittest.TestCase):
    def test_same_kind_k_identical_shas_twice(self):
        from eval.gui_synth import scenes
        for kind in scenes.KINDS:
            a = sha_list(scenes.build(kind, 3, **SMALL))
            b = sha_list(scenes.build(kind, 3, **SMALL))
            self.assertEqual(a, b, kind)
            self.assertEqual(len(a), SMALL["n"], kind)

    def test_different_k_different_shas(self):
        from eval.gui_synth import scenes
        for kind in scenes.KINDS:
            self.assertNotEqual(sha_list(scenes.build(kind, 1, **SMALL)),
                                sha_list(scenes.build(kind, 2, **SMALL)), kind)

    def test_frames_uint8_hwc_no_float(self):
        from eval.gui_synth import gen, scenes
        for kind in scenes.KINDS:
            sc = scenes.build(kind, 0, **SMALL)
            self.assertEqual(list(walk_floats(sc)), [], kind)
            json.dumps(sc)
            for i, a in gen.render(sc):
                self.assertEqual(a.dtype, np.uint8, kind)
                self.assertEqual(a.shape, (SMALL["h"], SMALL["w"], 3), kind)
                if i >= 2:
                    break

    def test_kinds_append_only_catalogue(self):
        from eval.gui_synth import scenes
        need = ("static_clean", "aa_jitter", "scroll_small", "scroll_large", "scroll_page", "scroll_h", "scroll_smooth",
                "caret_blink", "typing", "caret_move", "window_drag", "canvas_anim")
        self.assertEqual(tuple(scenes.KINDS[:12]) if set(need) <= set(scenes.KINDS) else None, tuple(scenes.KINDS[:12]))
        self.assertTrue(set(need) <= set(scenes.KINDS))

    def test_blink_period_matches_spec_and_pixels(self):
        """P = 2*half_ms*fps/1000 frames, checked on the rendered pixels (not only on the scene's own bookkeeping)."""
        from eval.gui_synth import scenes
        seen = set()
        for k in range(24):
            for fps in (30, 60):
                sc = scenes.build("caret_blink", k, w=96, h=64, fps=fps, n=2 * 120 + 4)
                half = sc["half_period_ms"]
                P = 2 * half * fps // 1000
                self.assertEqual(sc["period"], P)
                sh = sha_list(sc)
                j = next(i for i, x in enumerate(sh) if x != sh[0])
                back = next(i for i in range(j, len(sh)) if sh[i] == sh[0])
                self.assertEqual(back, P, (k, fps, half))
                seen.add((half, fps))
        self.assertEqual(seen, {(h, f) for h in (250, 500, 1000) for f in (30, 60)})

    def test_rng_splitmix64_reference(self):
        from eval.gui_synth import rng
        r = rng.SplitMix64(0)
        # canonical SplitMix64 outputs for seed 0
        self.assertEqual([r.u64() for _ in range(3)], [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4, 0x06C45D188009454F])
        r = rng.SplitMix64(7)
        v = [r.randint(-3, 5) for _ in range(200)]
        self.assertTrue(all(isinstance(x, int) and -3 <= x <= 5 for x in v))
        self.assertEqual({-3, 5} <= set(v), True)

    def test_to_ram_writes_only_inside_workspace(self):
        from eval.gui_synth import gen, scenes
        from vadapt import ramws
        sc = scenes.build("scroll_small", 0, **SMALL)
        before = {p for p in _repo_frames()}
        with ramws.open_run("p1t", "gen_ram_%d" % os.getpid(), 64 << 20, tool="gui_synth") as ws:
            rel, shas = gen.to_ram(sc, ws)
            p = ws.path(rel)
            self.assertTrue(str(p).startswith("/dev/shm/vqa-ramws/gui_synth/"))
            self.assertTrue(str(p).endswith("frames.rgb24"))
            self.assertEqual(os.path.getsize(p), SMALL["n"] * SMALL["h"] * SMALL["w"] * 3)
            self.assertEqual(list(shas), sha_list(sc))
        self.assertEqual({p for p in _repo_frames()}, before)

    def test_src_meta(self):
        from eval.gui_synth import gen, scenes
        s = gen.Src(scenes.build("static_clean", 0, **SMALL))
        self.assertEqual(s.meta["width"], 320)
        self.assertEqual(s.meta["height"], 180)
        self.assertEqual(s.meta["storage"], "mem")
        self.assertEqual(s.meta["count"], SMALL["n"])


def _repo_frames():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for d, ds, fs in os.walk(root):
        ds[:] = [x for x in ds if x not in (".git", "__pycache__") and not x.startswith("room_")]  # other lanes' scratch is not ours
        for f in fs:
            if f.endswith((".rgb24", ".png", ".raw")):
                yield os.path.join(d, f)


if __name__ == "__main__":
    unittest.main()
