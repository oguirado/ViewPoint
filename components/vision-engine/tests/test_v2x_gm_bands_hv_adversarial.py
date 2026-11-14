"""Safety regressions for the bands estimator; all fixtures stay in RAM."""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from eval.v2x import gm_bands_hv as gm


def objects(two=False):
    rng = np.random.default_rng(912)
    prev = np.full((480, 640), 40, dtype=np.uint8)
    cur = prev.copy()
    for y, x, dx, dy in ([(64, 64, 12, 0), (280, 380, 0, -10)] if two
                          else [(144, 160, 8, 8)]):
        block = rng.integers(0, 256, (96, 128), dtype=np.uint8)
        prev[y:y+96, x:x+128] = block
        cur[y+dy:y+dy+96, x+dx:x+dx+128] = block
    return prev, cur


class TestBandsAdversarial(unittest.TestCase):
    def test_flat_background_never_claims_static_tiles(self):
        prev, cur = objects()
        mask = gm.consensus_tiles(prev, cur, 16, 8, 8)
        changed = (prev != cur).reshape(30, 16, 40, 16).any(axis=(1, 3))
        self.assertFalse(np.any(mask & ~changed))
        self.assertTrue(mask.any())
        result = gm.estimate_global_motion(prev, cur, 100)
        self.assertIsNotNone(result)
        self.assertEqual(result[:2], (8, 8))

    def test_two_independent_motions_are_refused(self):
        rng = np.random.default_rng(0)
        prev = np.full((240, 320), 40, dtype=np.uint8)
        cur = prev.copy()
        for y, x, dx, dy in ((32, 32, 12, 0), (144, 200, 0, -10)):
            block = rng.integers(0, 256, (48, 64), dtype=np.uint8)
            prev[y:y+48, x:x+64] = block
            cur[y+dy:y+dy+48, x+dx:x+dx+64] = block
        self.assertIsNone(gm.estimate_global_motion(prev, cur, 100))

    def test_large_scrolls_never_return_wrong_vector(self):
        rng = np.random.default_rng(3000)
        prev = rng.integers(0, 256, (1088, 1920), dtype=np.uint8)
        for dy in (-600, 600, -830, 830, -816, 816):
            with self.subTest(dy=dy):
                cur = rng.integers(0, 256, prev.shape, dtype=np.uint8)
                a, b = max(0, dy), min(1088, 1088 + dy)
                cur[a:b] = prev[a-dy:b-dy]
                result = gm.estimate_global_motion(prev, cur, 1919)
                if result is not None:
                    self.assertEqual(result[:2], (0, dy))
                    self.assertIs(type(result[2]), int)

    def test_joint_verifications_are_bounded(self):
        rng = np.random.default_rng(11)
        prev = rng.integers(0, 256, (120, 160), dtype=np.uint8)
        cur = np.roll(prev, 13, axis=0)
        absolute = np.abs
        areas = []
        def counted_abs(array):
            if array.ndim == 2:
                areas.append(array.size)
            return absolute(array)
        with patch.object(gm.np, 'abs', side_effect=counted_abs):
            gm.estimate_global_motion(prev, cur, 100)
        self.assertLessEqual(sum(areas), 9 * prev.size)
        # Joint verification is factored so the expensive calls are observable.
        with patch.object(gm, '_joint_sad', wraps=gm._joint_sad) as sad:
            gm.estimate_global_motion(prev, cur, 100)
        self.assertLessEqual(sad.call_count, 9)
        self.assertGreater(sad.call_count, 0)

    def test_projection_smoothing_has_no_padding(self):
        prev, cur = objects()
        original = np.convolve
        modes = []
        def convolve(a, b, mode):
            modes.append(mode)
            return original(a, b, mode=mode)
        with patch.object(gm.np, 'convolve', side_effect=convolve):
            gm.estimate_global_motion(prev, cur, 100)
        self.assertTrue(modes)
        self.assertEqual(set(modes), {'valid'})

    def test_integer_only_source(self):
        tree = ast.parse(Path(gm.__file__).read_text())
        for node in ast.walk(tree):
            self.assertFalse(isinstance(node, ast.Constant) and isinstance(node.value, float))
            self.assertFalse(isinstance(node, ast.Div))

    def test_ops_are_isolated_between_threads(self):
        gm.reset_ops()
        prev, cur = objects()
        with ThreadPoolExecutor(max_workers=1) as pool:
            count = pool.submit(lambda: (gm.consensus_tiles(prev, cur, 16, 8, 8), gm.OPS)[1]).result()
        self.assertGreater(count, 0)
        self.assertEqual(gm.OPS, 0)


if __name__ == '__main__':
    unittest.main()
