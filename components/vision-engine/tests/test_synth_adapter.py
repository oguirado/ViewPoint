import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np

from visioncore import canon
from visioncore.errors import VqaError
from vadapt.synth import SCENARIOS, SynthAdapter, apply_op, render_scenario
from vadapt.synth import synthetic as S
from vadapt.synth.pngenc import encode_png
from tests.synth_gen import GOLDEN, SEED, expectation


PINNED = [81, 247, 119, 32, 71, 44, 121, 51]


class Golden(unittest.TestCase):
    def test_golden_match(self):
        for name in SCENARIOS:
            man, exp = expectation(name)
            d = GOLDEN / name
            self.assertEqual(canon.dumps(man), (d / "frames.manifest.json").read_text(), name)
            self.assertEqual(canon.dumps(exp), (d / "expect" / "synth.json").read_text(), name)
            self.assertEqual(exp["frames_sha256"] + "\n", (d / "expect" / "frames.sha256").read_text(), name)

    def test_x1_determinism_3x(self):
        for name in SCENARIOS:
            runs = [expectation(name)[1]["frames_sha256"] for _ in range(3)]
            self.assertEqual(len(set(runs)), 1, name)

    def test_seed_changes_output(self):
        a = expectation("clean_static")[1]["frames_sha256"]
        fr, _, _ = render_scenario("clean_static", SEED + 1)
        self.assertNotEqual(a, S.frames_sha256([S.frame_sha(f) for f in fr]))

    def test_noise_pinned(self):
        self.assertEqual(S.noise(1, 1, (8,)).tolist(), PINNED)

    def test_canonical_json_goldens(self):
        for p in GOLDEN.glob("*/*.json"):
            self.assertTrue(canon.is_canonical(p.read_text()), p)
        for p in GOLDEN.glob("*/expect/*.json"):
            self.assertTrue(canon.is_canonical(p.read_text()), p)


def _diff_tiles_mask(a, b):
    return np.any(a != b, axis=2)


class Semantics(unittest.TestCase):
    def sc(self, name, **kw):
        return render_scenario(name, SEED, **kw)

    def test_clean_static_identical(self):
        fr, t, _ = self.sc("clean_static")
        self.assertTrue(all(np.array_equal(fr[0], f) for f in fr))
        self.assertEqual(t["fields"], [])

    def test_gpu_jitter_bounded_and_nonzero(self):
        fr, t, _ = self.sc("gpu_jitter")
        base = S.base_scene(SEED, 256, 160)
        for f in fr:
            d = np.abs(f.astype(int) - base.astype(int))
            self.assertLessEqual(d.max(), 1)
        self.assertFalse(np.array_equal(fr[0], fr[1]))
        self.assertEqual(t["fields"], [])

    def test_flicker_only_inside_rect_on_odd_frames(self):
        fr, t, _ = self.sc("flicker")
        x, y, w, h = t["rect"]
        for i, f in enumerate(fr):
            m = _diff_tiles_mask(f, fr[0])
            if i % 2 == 0:
                self.assertFalse(m.any())
            else:
                self.assertTrue(m[y:y + h, x:x + w].any())
                m[y:y + h, x:x + w] = False
                self.assertFalse(m.any())
        self.assertEqual(len(t["fields"]), 12 * len(t["frames"]))

    def test_frozen_subregion(self):
        fr, t, _ = self.sc("frozen_subregion")
        x, y, w, h = S.FREEZE_F
        for i in range(9, len(fr)):
            self.assertTrue(np.array_equal(fr[i][y:y + h, x:x + w], fr[8][y:y + h, x:x + w]))
        self.assertFalse(np.array_equal(fr[3][y:y + h, x:x + w], fr[4][y:y + h, x:x + w]))
        self.assertFalse(np.array_equal(fr[20], fr[21]))
        tx, ty, tw, th = S.FREEZE_T  # ticker outside the frozen rect keeps changing
        self.assertFalse(np.array_equal(fr[20][ty:ty + 16, tx:tx + tw], fr[21][ty:ty + 16, tx:tx + tw]))

    def test_small_overlap(self):
        fr, t, _ = self.sc("small_overlap")
        self.assertTrue(all(np.array_equal(fr[0], f) for f in fr[:8]))
        self.assertFalse(np.array_equal(fr[7], fr[8]))
        x, y, w, h = t["rect"]
        self.assertEqual(w, 3)
        self.assertTrue(np.array_equal(fr[8][y:y + h, x:x + w], fr[23][y:y + h, x:x + w]))

    def test_slow_drift_monotone(self):
        fr, t, _ = self.sc("slow_drift")
        x, y, w, h = S.DRIFT_R
        means = [int(f[y:y + h, x:x + w].astype(int).sum()) for f in fr]
        self.assertEqual(means, sorted(means))
        self.assertLessEqual(int(np.abs(fr[1].astype(int) - fr[0].astype(int)).max()), 1)
        self.assertEqual(int(np.abs(fr[-1].astype(int) - fr[0].astype(int)).max()), 11)

    def test_scroll_global(self):
        fr, t, _ = self.sc("scroll")
        self.assertTrue(np.array_equal(fr[3], np.roll(fr[0], 12, axis=1)))
        self.assertEqual(t["content_shift_px"], [4, 0])

    def test_bad_scenario_and_size(self):
        with self.assertRaises(VqaError):
            render_scenario("nope")
        with self.assertRaises(VqaError):
            render_scenario("flicker", W=64)


class PxOps(unittest.TestCase):
    def setUp(self):
        self.fr, _, _ = render_scenario("clean_static", SEED)

    def test_ops_truth_and_effect(self):
        r = (20, 20, 40, 30)
        cases = [("toggle_rect", {"frames": [2, 4]}), ("freeze_rect", {"from_frame": 0}),
                 ("paste_rect", {"src": [100, 100], "frames": [5]}), ("shift_rect", {"dx": 3, "dy": 0, "frames": [6]}),
                 ("scroll_rect", {"dx": 2, "dy": 1, "frames": [1, 2]})]
        for op, p in cases:
            before = [f.copy() for f in self.fr]
            out, m = apply_op(self.fr, op, r, p)
            self.assertTrue(all(np.array_equal(a, b) for a, b in zip(before, self.fr)), op)  # inputs untouched
            self.assertEqual(m["schema"], "vqa.px-mutant/v1")
            self.assertEqual(m["truth"]["rect"], list(r))
            self.assertTrue(m["truth"]["fields"] and all(f.startswith("f") for f in m["truth"]["fields"]))
            changed = [i for i in range(len(out)) if not np.array_equal(out[i], self.fr[i])]
            if op == "freeze_rect":
                self.assertEqual(changed, [])  # static base: freezing a static rect is a no-op by construction
            else:
                self.assertEqual(changed, m["truth"]["frames"], op)
                for i in changed:
                    mk = _diff_tiles_mask(out[i], self.fr[i])
                    mk[20:50, 20:60] = False
                    self.assertFalse(mk.any(), op)
            self.assertTrue(canon.is_canonical(canon.dumps(m)))

    def test_freeze_effective_on_animated(self):
        fr, _, _ = render_scenario("frozen_subregion", SEED)
        out, m = apply_op(fr, "freeze_rect", (0, 128, 64, 32), {"from_frame": 4})
        self.assertTrue(np.array_equal(out[10][128:160, 0:64], fr[4][128:160, 0:64]))
        self.assertEqual(m["truth"]["frames"], list(range(5, 24)))

    def test_negative(self):
        with self.assertRaises(VqaError):
            apply_op(self.fr, "bogus", (0, 0, 8, 8))
        with self.assertRaises(VqaError):
            apply_op(self.fr, "toggle_rect", (250, 150, 20, 20))
        with self.assertRaises(VqaError):
            apply_op(self.fr, "toggle_rect", (0, 0, 0, 8))


class Adapter(unittest.TestCase):
    def test_write_run_and_read_back(self):
        with tempfile.TemporaryDirectory() as td:
            a = SynthAdapter()
            man, truth = a.write_run(td, "flicker", seed=SEED)
            raw = (Path(td) / "frames.rgb").read_bytes()
            self.assertEqual(len(raw), man["count"] * man["frame_bytes"])
            src = a.frames(td)
            self.assertEqual(len(src), man["count"])
            self.assertEqual(src.meta, man)
            fb = man["frame_bytes"]
            for fm, fr in zip(man["frames"], src):
                self.assertEqual(fr.sha256, fm["sha256"])
                self.assertEqual(fr.render_index, fm["i"])
                import hashlib
                self.assertEqual(hashlib.sha256(raw[fm["i"] * fb:(fm["i"] + 1) * fb]).hexdigest(), fm["sha256"])
            self.assertEqual(a.scope(td)["res"], "256x160")
            self.assertEqual(len(list(a.hints(td))), 2)
            self.assertEqual(len(a.still_probe(td)), 30)
            self.assertTrue(canon.is_canonical((Path(td) / "frames.manifest.json").read_text()))
            for k in ("schema", "width", "height", "channels", "pixel_format", "count", "frames", "provenance"):
                self.assertIn(k, man)
            self.assertEqual(man["provenance"]["adapter_id"], "synth")

    def test_missing_scenario_json(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(VqaError) as c:
                SynthAdapter().frames(td)
            self.assertEqual(c.exception.rc, 2)

    def test_still_probe_not_identical_but_tiny(self):
        with tempfile.TemporaryDirectory() as td:
            a = SynthAdapter()
            a.write_run(td, "clean_static", seed=SEED)
            fr = [f.rgb.astype(int) for f in a.still_probe(td)]
            self.assertFalse(np.array_equal(fr[0], fr[1]))
            self.assertLessEqual(max(int(np.abs(f - fr[0]).max()) for f in fr), 2)


class Png(unittest.TestCase):
    def test_structure_all_filters(self):
        fr, _, _ = render_scenario("clean_static", SEED, W=256, H=160, n=16)
        img = fr[0][:20, :40]
        for filt in (0, 1, 2, 3, 4, -1):
            b = encode_png(np.ascontiguousarray(img), filt)
            self.assertEqual(b[:8], b"\x89PNG\r\n\x1a\n")
            n = struct.unpack(">I", b[8:12])[0]
            self.assertEqual(b[12:16], b"IHDR")
            self.assertEqual(struct.unpack(">IIBBBBB", b[16:16 + n]), (40, 20, 8, 2, 0, 0, 0))
            i = b.index(b"IDAT")
            ln = struct.unpack(">I", b[i - 4:i])[0]
            self.assertEqual(len(zlib.decompress(b[i + 4:i + 4 + ln])), 20 * (1 + 40 * 3))

    def test_rgba(self):
        img = np.zeros((4, 4, 3), np.uint8)
        b = encode_png(img, 0, alpha=np.full((4, 4), 7, np.uint8))
        self.assertEqual(b[25], 6)


if __name__ == "__main__":
    unittest.main()
