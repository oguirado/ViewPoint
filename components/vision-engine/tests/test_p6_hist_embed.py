"""Unit tests for research prototype eval/p6_hist_embed.py.

Pins requirements:
1. Identical tiles give T = 0.
2. A small elastic warp of a texture gives T / (2 * s) < 0.2 (classified as DEFORM).
3. Replacing a tile by unrelated content gives T / (2 * s) > 0.8 (classified as OCCLUDE).
4. The paper upper bound T <= 2 * t + 2 * alpha * sum(d_m) holds on 200 seeded pairs.
5. Determinism under fixed seed.
"""

import unittest
import numpy as np

from eval.p6_hist_embed import (
    generate_partitions,
    histogram_embedding_distance,
    upper_bound,
    lower_bound,
    classify_tile_change,
)


class TestP6HistEmbed(unittest.TestCase):

    def test_identical_tiles_zero_distance(self):
        """Identical tiles give T = 0."""
        rng = np.random.default_rng(42)
        L = rng.integers(0, 256, size=256)
        L_prime = L.copy()

        T = histogram_embedding_distance(L, L_prime, alpha=0.05, K=100, seed=123)
        self.assertEqual(T, 0.0)

        label, norm_T, raw_T = classify_tile_change(L, L_prime, threshold=0.5, alpha=0.05, K=100, seed=123)
        self.assertEqual(raw_T, 0.0)
        self.assertEqual(norm_T, 0.0)
        self.assertEqual(label, "DEFORM")

    def test_small_elastic_warp_below_threshold(self):
        """A small elastic warp of a texture gives T / (2 * s) < 0.2 and is classified as DEFORM."""
        s_grid = 16
        s = s_grid * s_grid
        x, y = np.meshgrid(np.arange(s_grid), np.arange(s_grid))
        # Continuous texture
        texture = (np.sin(x / 2.0) + np.cos(y / 2.0)) * 60.0 + 128.0
        L = texture.ravel()

        # Elastic warp / small perturbation in pixel values (|u - v| <= 4)
        rng = np.random.default_rng(100)
        warp = rng.uniform(-4.0, 4.0, size=s)
        L_prime = np.clip(L + warp, 0, 255)

        label, norm_T, raw_T = classify_tile_change(L, L_prime, threshold=0.5, alpha=0.05, K=200, seed=42)
        self.assertLess(norm_T, 0.2)
        self.assertEqual(label, "DEFORM")

    def test_unrelated_replacement_above_threshold(self):
        """Replacing a tile by unrelated content gives T / (2 * s) > 0.8 and is classified as OCCLUDE."""
        s = 256
        # Original tile with low luma [0, 30]
        rng1 = np.random.default_rng(10)
        L = rng1.uniform(0.0, 30.0, size=s)

        # Replaced content with high luma [180, 220]
        rng2 = np.random.default_rng(20)
        L_prime = rng2.uniform(180.0, 220.0, size=s)

        label, norm_T, raw_T = classify_tile_change(L, L_prime, threshold=0.5, alpha=0.05, K=200, seed=42)
        self.assertGreater(norm_T, 0.8)
        self.assertEqual(label, "OCCLUDE")

    def test_paper_upper_bound_holds_on_200_seeded_pairs(self):
        """The paper upper bound T <= 2 * t + 2 * alpha * sum(d_m) holds on 200 seeded pairs."""
        s = 64
        K = 200
        alpha = 0.05
        pair_gen_rng = np.random.default_rng(999)

        for pair_idx in range(200):
            # Draw random unpaired count t in [0, 16]
            t = pair_gen_rng.integers(0, 17)
            num_paired = s - t

            L_paired = pair_gen_rng.uniform(0.0, 255.0, size=num_paired)
            paired_shifts = pair_gen_rng.uniform(-20.0, 20.0, size=num_paired)
            L_prime_paired = np.clip(L_paired + paired_shifts, 0.0, 255.0)
            actual_dists = np.abs(L_paired - L_prime_paired)

            L_unpaired = pair_gen_rng.uniform(0.0, 255.0, size=t)
            L_prime_unpaired = pair_gen_rng.uniform(0.0, 255.0, size=t)

            L = np.concatenate([L_paired, L_unpaired])
            L_prime = np.concatenate([L_prime_paired, L_prime_unpaired])

            T = histogram_embedding_distance(L, L_prime, alpha=alpha, K=K, seed=1000 + pair_idx)
            bound = upper_bound(t, actual_dists, alpha)

            # Assert T <= upper_bound (allowing small numerical margin for float)
            self.assertLessEqual(T, bound + 1e-6, f"Upper bound failed at pair {pair_idx}: T={T}, bound={bound}")

    def test_determinism_under_fixed_seed(self):
        """Results are identical given identical inputs and fixed seed."""
        rng = np.random.default_rng(77)
        L = rng.uniform(0, 255, size=100)
        L_prime = rng.uniform(0, 255, size=100)

        T1 = histogram_embedding_distance(L, L_prime, alpha=0.05, K=150, seed=888)
        T2 = histogram_embedding_distance(L, L_prime, alpha=0.05, K=150, seed=888)

        self.assertEqual(T1, T2)

        p1 = generate_partitions(5, 0.05, seed=123)
        p2 = generate_partitions(5, 0.05, seed=123)
        self.assertEqual(len(p1), len(p2))
        for arr1, arr2 in zip(p1, p2):
            np.testing.assert_array_equal(arr1, arr2)

    def test_edge_cases_and_error_handling(self):
        """Invalid inputs raise appropriate exceptions."""
        with self.assertRaises(ValueError):
            histogram_embedding_distance([1, 2, 3], [1, 2], alpha=0.05, K=10)

        with self.assertRaises(ValueError):
            generate_partitions(10, alpha=-0.01)

        with self.assertRaises(ValueError):
            generate_partitions(0, alpha=0.05)

        # Empty token lists
        T = histogram_embedding_distance([], [], alpha=0.05, K=10)
        self.assertEqual(T, 0.0)

        label, norm_T, raw_T = classify_tile_change([], [])
        self.assertEqual(label, "DEFORM")
        self.assertEqual(norm_T, 0.0)

        # Lower bound calculation sanity check
        lb = lower_bound(s=256, a=50.0, alpha=0.05)
        self.assertIsInstance(lb, float)


if __name__ == "__main__":
    unittest.main()
