"""FX round 1 (JW9-4): labels emit the normative labels/frames_meta.jsonl side table {frame, cov_ppm, cam_motion}."""
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blender_visual_qa.truth import labels as L  # noqa: E402
from tests.test_labels_w9 import make_run  # noqa: E402


class FramesMetaTests(unittest.TestCase):
    def test_frame_meta_counts_covered_tiles_in_ppm(self):
        tl = np.zeros(200, np.uint8)
        tl[:5] = 3
        tl[5:7] = 10  # FROZEN_T tiles are not coverage (FLICKER_T/DRIFT_T are: tests.test_labels_fx2)
        flags = np.zeros(200, np.uint8)
        self.assertEqual(L.frame_meta(7, {"tl": tl, "flags": flags}), {"frame": 7, "cov_ppm": 25000, "cam_motion": 0})
        flags[3] = 4
        self.assertEqual(L.frame_meta(7, {"tl": tl, "flags": flags})["cam_motion"], 1)

    def test_run_writes_frames_meta_aligned_to_captured_frames(self):
        with tempfile.TemporaryDirectory() as d:
            make_run(d)
            out = os.path.join(d, "o")
            res = L.run(d, {}, out)
            lines = [json.loads(x) for x in open(os.path.join(out, "frames_meta.jsonl")).read().splitlines()]
        self.assertEqual([m["frame"] for m in lines], res["frame_ids"])
        self.assertEqual(res["frame_ids"], [1, 2])
        self.assertTrue(all(set(m) == {"frame", "cov_ppm", "cam_motion"} for m in lines))

    def test_frames_meta_is_deterministic(self):
        blobs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as d:
                make_run(d)
                L.run(d, {}, os.path.join(d, "o"))
                blobs.append(open(os.path.join(d, "o", "frames_meta.jsonl"), "rb").read())
        self.assertEqual(blobs[0], blobs[1])


if __name__ == "__main__":
    unittest.main()
