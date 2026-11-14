"""tests/test_jq1_a1_repeat_thread_determinism_and_types.py - Thread determinism, type invariants and immutability tests for eval.v2x.gm_bands_hv."""

from concurrent.futures import ThreadPoolExecutor
import math
import unittest
import numpy as np
from eval.v2x.gm_bands_hv import estimate_global_motion, consensus_tiles


class TestRepeatThreadDeterminismAndTypes(unittest.TestCase):

    def setUp(self):
        self.max_d = 64
        self.s = 16
        self.num_workers = 4
        self.repeats_per_scene = 8

    def _build_scenes(self):
        rng = np.random.default_rng(1616)
        shapes = [(180, 320), (135, 240)]
        scenes = []

        for H, W in shapes:
            # Scene 1: Textured scroll by (0, 13)
            prev1 = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
            cur1 = np.empty_like(prev1)
            cur1[13:, :] = prev1[:-13, :]
            cur1[:13, :] = rng.integers(0, 256, size=(13, W), dtype=np.uint8)
            scenes.append(("textured_scroll_0_13", H, W, prev1, cur1))

            # Scene 2: 48-square textured block dragged (8, 8) over flat canvas
            prev2 = np.full((H, W), 128, dtype=np.uint8)
            cur2 = np.full((H, W), 128, dtype=np.uint8)
            block = rng.integers(0, 256, size=(48, 48), dtype=np.uint8)
            y0, x0 = (H - 48 - 8) // 2, (W - 48 - 8) // 2
            prev2[y0:y0+48, x0:x0+48] = block
            cur2[y0+8:y0+8+48, x0+8:x0+8+48] = block
            scenes.append(("dragged_block_8_8", H, W, prev2, cur2))

            # Scene 3: Period-16 stripes shifted by 3
            x_coords = np.arange(W)
            stripes = np.where((x_coords // 8) % 2 == 0, 200, 50).astype(np.uint8)
            prev3 = np.tile(stripes, (H, 1))
            cur3 = np.roll(prev3, shift=3, axis=1)
            scenes.append(("period16_stripes_shift3", H, W, prev3, cur3))

            # Scene 4: Identical textured frames
            prev4 = rng.integers(0, 256, size=(H, W), dtype=np.uint8)
            cur4 = prev4.copy()
            scenes.append(("identical_textured", H, W, prev4, cur4))

        scene_data = []
        for name, H, W, prev, cur in scenes:
            prev.flags.writeable = False
            cur.flags.writeable = False
            scene_data.append({
                "name": name,
                "H": H,
                "W": W,
                "prev": prev,
                "cur": cur,
                "prev_bytes": prev.tobytes(),
                "cur_bytes": cur.tobytes(),
            })
        return scene_data

    def test_repeat_thread_determinism_and_types(self):
        scenes = self._build_scenes()

        # Compute serial baselines
        baselines = {}
        for s_idx, sc in enumerate(scenes):
            est = estimate_global_motion(sc["prev"], sc["cur"], max_d=self.max_d)
            dx, dy = est[:2] if est is not None else (0, 0)
            mask = consensus_tiles(sc["prev"], sc["cur"], self.s, dx, dy)
            baselines[s_idx] = (est, mask)

        def worker_task(s_idx):
            sc = scenes[s_idx]
            est = estimate_global_motion(sc["prev"], sc["cur"], max_d=self.max_d)
            dx, dy = est[:2] if est is not None else (0, 0)
            mask = consensus_tiles(sc["prev"], sc["cur"], self.s, dx, dy)
            return s_idx, est, mask

        # Interleave scene tasks across ThreadPoolExecutor
        tasks = []
        for rep in range(self.repeats_per_scene):
            for s_idx in range(len(scenes)):
                tasks.append(s_idx)

        with ThreadPoolExecutor(max_workers=self.num_workers) as executor:
            futures = [executor.submit(worker_task, s_idx) for s_idx in tasks]
            results = [f.result() for f in futures]

        self.assertEqual(len(results), len(scenes) * self.repeats_per_scene)

        # Verify results
        for s_idx, est, mask in results:
            sc = scenes[s_idx]
            serial_est, serial_mask = baselines[s_idx]

            # 1. Unchanged input bytes & read-only flag
            self.assertEqual(sc["prev"].tobytes(), sc["prev_bytes"])
            self.assertEqual(sc["cur"].tobytes(), sc["cur_bytes"])
            self.assertFalse(sc["prev"].flags.writeable)
            self.assertFalse(sc["cur"].flags.writeable)

            # 2. Identical results to serial baselines
            self.assertEqual(est, serial_est)
            self.assertTrue(np.array_equal(mask, serial_mask))
            self.assertEqual(mask.tobytes(), serial_mask.tobytes())

            # 3. Expected bool mask shape: (ceil(H / 16), ceil(W / 16))
            expected_shape = (math.ceil(sc["H"] / 16), math.ceil(sc["W"] / 16))
            self.assertEqual(mask.shape, expected_shape)
            self.assertEqual(mask.dtype, np.bool_)

            # 4. Strict nonnegative Python int tuple elements when present
            if est is not None:
                self.assertIs(type(est), tuple)
                self.assertEqual(len(est), 3)
                for val in est:
                    self.assertIs(type(val), int)
                    self.assertNotIsInstance(val, bool)
                    self.assertGreaterEqual(val, 0)


if __name__ == "__main__":
    unittest.main()
