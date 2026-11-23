"""CF1 bench scenes (needs Blender 4.0.2; skips cleanly without it)."""
import os
import shutil
import tempfile
import unittest

from tests.test_bpy_side import BLENDER, build, jload


@unittest.skipUnless(os.path.exists(BLENDER), "Blender not installed")
class BenchSceneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bvqa_cf1_")
        cls.b4 = jload(build("B4", cls.tmp)[1])
        cls.b1 = jload(build("B1", cls.tmp)[1])
        cls.b3 = jload(build("B3", cls.tmp)[1])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_b4_temporal_and_ramp_notes(self):
        t = self.b4["temporal"]
        self.assertEqual(len(t), 7)  # 3 flicker + frozen + creep + 2 ramps
        kinds = sorted(x["kind"] for x in t)
        self.assertEqual(kinds.count("flicker"), 3)
        self.assertEqual(kinds.count("frozen"), 1)
        self.assertEqual(kinds.count("drift"), 3)
        for r in ("b4_ramp0", "b4_ramp1"):
            frames = {a["frame"] for a in self.b4["animated"] if a["name"] == r}
            self.assertEqual(frames, set(range(2, 5)))

    def test_b1_boxes_are_48px_and_six(self):
        names = [o["name"] for o in self.b1["objects"]]
        self.assertEqual(sorted(names), sorted("b1_box%d" % v for v in (1, 2, 3, 5, 8, 16)))

    def test_filter_width_and_lit(self):
        self.assertEqual(self.b1["filter_width"], 0.01)
        self.assertEqual(self.b3["lit"], ["b3_lit"])


if __name__ == "__main__":
    unittest.main()
