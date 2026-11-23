"""W9 S2: labels read T0 + hysteresis band from the profile; grid errors are E_STALE reason grid (IMPL-GAP-1/2/11)."""
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blender_visual_qa.truth import labels as L  # noqa: E402
from tests.test_labels import W, H, frame  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "truth", "labels_sha.json")


def make_run(d, grid=True, anim=False):
    g = L.vgrid.Grid(W, H)
    tr = os.path.join(d, "truth")
    os.makedirs(tr)
    frs = [frame((16, 16, 64, 48)), frame((16, 16, 64, 48))]
    with open(os.path.join(tr, "frames.jsonl"), "w") as fh:
        for i, f in enumerate(frs, 1):
            for k in ("ob", "mv", "z"):
                np.save(os.path.join(tr, "f%06d.%s.npy" % (i, k)), f[k])
            row = {"render_index": i, "width": W, "height": H, "motion_blur": False}
            if grid:
                row["grid"] = g.ident
            fh.write(json.dumps(row) + "\n")
    with open(os.path.join(d, "scene.manifest.json"), "w") as mf:
        json.dump({"objects": [{"name": "box", "pass_index": 1}]}, mf)
    if anim:
        with open(os.path.join(d, "journal.json"), "w") as jf:
            json.dump({"animated": [{"frame": 2, "kind": "material", "name": "box"}]}, jf)
    return g


class ThresholdTests(unittest.TestCase):
    def test_default_band(self):
        self.assertEqual(L.thresholds({}), (35, 28))
        self.assertEqual(L.thresholds({"T0_Q4": 100}), (110, 90))
        self.assertEqual(L.thresholds({"T0_Q4": 131}), (144, 117))

    def test_free_keys_ignored(self):
        self.assertEqual(L.thresholds({"T_enter": 64, "T_exit": 48}), (35, 28))

    def test_profile_t0_changes_labels(self):  # bt.t0hi.01 shape: a 4x T0 changes SHADE_STRONG
        def strong(t0):
            with tempfile.TemporaryDirectory() as d:
                g = make_run(d, anim=True)
                resid = np.full((2, g.N), 70, np.int64)
                L.run(d, {"T0_Q4": t0}, os.path.join(d, "o"), resid)
                with open(os.path.join(d, "o", "tiles_truth.bin"), "rb") as fh:
                    _, arrs = L.parse_truth_tiles(fh.read(), g)
                return int((arrs[1]["tl"] == L.TL_ID["SHADE_STRONG"]).sum())
        self.assertGreater(strong(32), 0)      # 2*35 = 70 <= r
        self.assertEqual(strong(128), 0)       # 2*140 > r


class GridErrorTests(unittest.TestCase):
    def test_label_frame_wrong_shape_is_stale_grid(self):
        g = L.vgrid.Grid(W, H)
        f = frame((16, 16, 64, 48))
        bad = {k: v[:32] for k, v in f.items()}
        with self.assertRaises(L.VqaError) as c:
            L.label_frame(f, bad, g)
        self.assertEqual((c.exception.code, c.exception.rc), ("E_STALE", 4))
        self.assertEqual(c.exception.detail["reason"], "grid")

    def test_run_wrong_grid_ident(self):
        with tempfile.TemporaryDirectory() as d:
            make_run(d)
            p = os.path.join(d, "truth", "frames.jsonl")
            rows = [json.loads(x) for x in pathlib.Path(p).read_text().splitlines()]
            rows[0]["grid"] = "grid:1x1:s16"
            pathlib.Path(p).write_text("".join(json.dumps(r) + "\n" for r in rows))
            with self.assertRaises(L.VqaError) as c:
                L.run(d, {}, os.path.join(d, "o"))
            self.assertEqual((c.exception.code, c.exception.rc, c.exception.detail["reason"]), ("E_STALE", 4, "grid"))

    def test_run_null_grid_is_schema(self):
        with tempfile.TemporaryDirectory() as d:
            make_run(d, grid=False)
            with self.assertRaises(L.VqaError) as c:
                L.run(d, {}, os.path.join(d, "o"))
            self.assertEqual((c.exception.code, c.exception.rc), ("E_SCHEMA", 2))
            self.assertFalse(os.path.exists(os.path.join(d, "o")))

    def test_parse_truth_tiles_stale_grid(self):
        g = L.vgrid.Grid(W, H)
        blob = L.pack_truth_tiles(g, [L.static_frame(g)])
        with self.assertRaises(L.VqaError) as c:
            L.parse_truth_tiles(blob, L.vgrid.Grid(W * 2, H))
        self.assertEqual((c.exception.code, c.exception.detail["reason"]), ("E_STALE", "grid"))


class GoldenTests(unittest.TestCase):
    def test_labels_three_runs_identical_and_golden(self):
        shas = []
        for _ in range(3):
            with tempfile.TemporaryDirectory() as d:
                make_run(d, anim=True)
                shas.append(L.run(d, {"T0_Q4": 32}, os.path.join(d, "o"))["sha256"])
        self.assertEqual(len(set(shas)), 1)
        with open(GOLDEN) as fh:
            self.assertEqual(shas[0], json.load(fh)["labels_sha256"])


if __name__ == "__main__":
    unittest.main()
