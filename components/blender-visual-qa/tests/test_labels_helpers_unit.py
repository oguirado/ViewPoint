import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blender_visual_qa.truth import labels as L  # noqa: E402

W, H = 96, 64


def frame(box=None, ob=1, z_mm=2000, mv=(0, 0)):
    """Synthetic truth frame: bg ob 0 (z=BG), a box (x0,y0,x1,y1) with ob id, uniform mv in px."""
    f = {
        "ob": np.zeros((H, W), np.uint16),
        "z": np.full((H, W), L.BG_Z, np.uint32),
        "mv": np.zeros((H, W, 2), np.int16),
    }
    if box:
        x0, y0, x1, y1 = box
        f["ob"][y0:y1, x0:x1] = ob
        f["z"][y0:y1, x0:x1] = z_mm
        f["mv"][y0:y1, x0:x1] = (mv[0] * 16, mv[1] * 16)
    return f


class LabelsHelpersUnitTest(unittest.TestCase):
    # --- load_frame ---
    def test_load_frame_success_and_optional_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ri = 1
            ob = np.zeros((H, W), dtype=np.uint16)
            mv = np.zeros((H, W, 2), dtype=np.int16)
            z = np.full((H, W), L.BG_Z, dtype=np.uint32)
            ma = np.ones((H, W), dtype=np.uint16)
            n = np.zeros((H, W, 3), dtype=np.float32)
            uv = np.zeros((H, W, 2), dtype=np.float32)

            np.save(os.path.join(tmpdir, "f%06d.ob.npy" % ri), ob)
            np.save(os.path.join(tmpdir, "f%06d.mv.npy" % ri), mv)
            np.save(os.path.join(tmpdir, "f%06d.z.npy" % ri), z)
            np.save(os.path.join(tmpdir, "f%06d.ma.npy" % ri), ma)
            np.save(os.path.join(tmpdir, "f%06d.n.npy" % ri), n)
            np.save(os.path.join(tmpdir, "f%06d.uv.npy" % ri), uv)

            loaded = L.load_frame(tmpdir, ri)
            self.assertEqual(set(loaded.keys()), {"ob", "mv", "z", "ma", "n", "uv"})
            np.testing.assert_array_equal(loaded["ob"], ob)
            np.testing.assert_array_equal(loaded["mv"], mv)
            np.testing.assert_array_equal(loaded["z"], z)
            np.testing.assert_array_equal(loaded["ma"], ma)
            np.testing.assert_array_equal(loaded["n"], n)
            np.testing.assert_array_equal(loaded["uv"], uv)

    def test_load_frame_missing_required_key_raises_e_io(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ri = 1
            ob = np.zeros((H, W), dtype=np.uint16)
            mv = np.zeros((H, W, 2), dtype=np.int16)
            # z missing

            np.save(os.path.join(tmpdir, "f%06d.ob.npy" % ri), ob)
            np.save(os.path.join(tmpdir, "f%06d.mv.npy" % ri), mv)

            with self.assertRaises(L.VqaError) as cm:
                L.load_frame(tmpdir, ri)
            self.assertEqual(cm.exception.code, "E_IO")
            self.assertEqual(cm.exception.rc, 2)

    # --- round_px ---
    def test_round_px(self):
        # 1 px = 16 units in q4. round half away from zero.
        # +8 -> +1, -8 -> -1, +7 -> 0, -7 -> 0
        inputs = np.array([0, 7, 8, 15, 16, 24, -7, -8, -15, -16, -24], dtype=np.int16)
        expected = np.array([0, 0, 1, 1, 1, 2, 0, -1, -1, -1, -2], dtype=np.int32)
        res = L.round_px(inputs)
        np.testing.assert_array_equal(res, expected)

    # --- pixel_predicates ---
    def test_pixel_predicates_anim(self):
        prev = frame((10, 10, 30, 30), ob=1)
        cur = frame((10, 10, 30, 30), ob=1)
        cur["ob"][10:30, 10:20] = 2  # left half ob=2, right half ob=1

        preds = L.pixel_predicates(prev, cur, anim_ob={2})
        self.assertTrue(preds["anim"][10:30, 10:20].all())
        self.assertFalse(preds["anim"][10:30, 20:30].any())

        preds_all = L.pixel_predicates(prev, cur, anim_all=True)
        self.assertTrue(preds_all["anim"].all())

    # --- animated_at ---
    def test_animated_at(self):
        journal = {
            "animated": [
                {"frame": 2, "kind": "material", "name": "cube"},
                {"frame": 2, "kind": "visibility", "name": "sphere"},
                {"frame": 2, "kind": "light", "name": "lamp"},
                {"frame": 2, "kind": "camera", "name": "cam"},
                {"frame": 3, "kind": "material", "name": "cube"},
                {"frame": 2, "kind": "unknown", "name": "cube"},
            ]
        }
        n2i = {"cube": 1, "sphere": 2}
        obs, light, cam = L.animated_at(journal, 2, n2i)
        self.assertEqual(obs, {1, 2})
        self.assertTrue(light)
        self.assertTrue(cam)

        obs_f3, light_f3, cam_f3 = L.animated_at(journal, 3, n2i)
        self.assertEqual(obs_f3, {1})
        self.assertFalse(light_f3)
        self.assertFalse(cam_f3)

    # --- frames_sha16 ---
    def test_frames_sha16(self):
        data = b'{"render_index": 1, "width": 96, "height": 64}\n'
        sha16 = L.frames_sha16(data)
        self.assertIsInstance(sha16, bytes)
        self.assertEqual(len(sha16), 16)
        self.assertEqual(sha16, L.frames_sha16(data))

    # --- canon_line ---
    def test_canon_line(self):
        obj = {"b": 2, "a": 1, "c": [3, 2]}
        line = L.canon_line(obj)
        self.assertEqual(line, '{"a":1,"b":2,"c":[3,2]}\n')
        self.assertTrue(line.endswith("\n"))

    # --- thresholds ---
    def test_thresholds(self):
        # Default T0_Q4 = 32, HYST_DEFAULT_PERMILLE = 100
        # band = 100 * 1000 = 100000
        # enter = 32 * (1000000 + 100000) // 1000000 = 32 * 1100000 // 1000000 = 35
        # exit = 32 * (1000000 - 100000) // 1000000 = 32 * 900000 // 1000000 = 28
        t_enter, t_exit = L.thresholds({})
        self.assertEqual((t_enter, t_exit), (35, 28))

        t_enter_custom, t_exit_custom = L.thresholds({"T0_Q4": 64})
        self.assertEqual((t_enter_custom, t_exit_custom), (70, 57))

    # --- ring_from_filter ---
    def test_ring_from_filter(self):
        self.assertEqual(L.ring_from_filter(None), L.EDGE_RING)
        self.assertEqual(L.ring_from_filter(1.0), 1)
        self.assertEqual(L.ring_from_filter(1.5), 2)
        self.assertEqual(L.ring_from_filter(2.0), 2)
        self.assertEqual(L.ring_from_filter(0.2), 1)  # max(1, ...)


if __name__ == "__main__":
    unittest.main()
