"""Unit tests for eval.p5_hoeffding_gate (Hoeffding noise gate prototype)."""

import unittest
import numpy as np

from eval.p5_hoeffding_gate import (
    hoeffding_eps,
    noise_floor_q4,
    tile_is_noise,
)


class TestP5HoeffdingGate(unittest.TestCase):
    """Test suite pinning the claims for problem P5 Hoeffding gate prototype."""

    def test_noise_floor_q4_index_and_mu(self):
        """Test noise_floor_q4 computes exact mean and p99.9 index element."""
        # For n = 1000, idx = (999 * 999) // 1000 = 998 (0-indexed 998th element out of 1000)
        samples = np.arange(1000, dtype=float)
        mu, R = noise_floor_q4(samples)
        self.assertAlmostEqual(mu, 499.5)
        self.assertEqual(R, 998.0)

        # Edge case: empty input
        mu_e, R_e = noise_floor_q4([])
        self.assertEqual(mu_e, 0.0)
        self.assertEqual(R_e, 0.0)

    def test_hoeffding_eps_monotonicity(self):
        """Test claim 3: eps decreases monotonically in b."""
        for alpha in [0.05, 0.01, 0.001]:
            b_vals = np.array([16, 32, 64, 128, 256, 512, 1024, 2048, 4096])
            eps_vals = hoeffding_eps(alpha, b_vals)
            # Check strictly decreasing
            diffs = np.diff(eps_vals)
            self.assertTrue(
                np.all(diffs < 0),
                f"eps not monotonically decreasing for alpha={alpha}: {eps_vals}",
            )

    def test_hoeffding_eps_validation(self):
        """Test hoeffding_eps raises ValueError on invalid inputs."""
        with self.assertRaises(ValueError):
            hoeffding_eps(0.0, 256)
        with self.assertRaises(ValueError):
            hoeffding_eps(1.0, 256)
        with self.assertRaises(ValueError):
            hoeffding_eps(-0.1, 256)
        with self.assertRaises(ValueError):
            hoeffding_eps(0.01, 0)
        with self.assertRaises(ValueError):
            hoeffding_eps(0.01, -10)

    def test_integer_and_float_paths_agree(self):
        """Test claim 4: integer and float paths agree on tile_is_noise decision."""
        rng = np.random.default_rng(42)
        b = 256
        mu = 2.0
        R = 4.0
        alpha = 0.01
        res_sums = rng.integers(0, 2000, size=500)

        mask_flt = tile_is_noise(res_sums, b, mu, R, alpha, use_integer=False)
        mask_int = tile_is_noise(res_sums, b, mu, R, alpha, use_integer=True)

        np.testing.assert_array_equal(mask_flt, mask_int)

    def test_r_zero_edge_case(self):
        """Test tile_is_noise handles R=0 safely in both float and int paths."""
        res_sums = np.array([0, 1, 5, 0])
        b = 256
        mu = 0.0
        R = 0.0
        alpha = 0.01

        mask_flt = tile_is_noise(res_sums, b, mu, R, alpha, use_integer=False)
        mask_int = tile_is_noise(res_sums, b, mu, R, alpha, use_integer=True)

        expected = np.array([True, False, False, True])
        np.testing.assert_array_equal(mask_flt, expected)
        np.testing.assert_array_equal(mask_int, expected)

    def test_claims_seeds_0_to_9(self):
        """Test claims 1 and 2:

        - Claim 1: With uniform bounded noise from numpy seeds 0..9 on 10000 tiles of
          256 pixels, empirical false alarm of tile_is_noise inverted is at most alpha
          for alpha 0.01 and 0.001.
        - Claim 2: A 6-level step change on a tile is never classed noise.
        """
        b = 256
        n_tiles = 10000

        for seed in range(10):
            # Seed reproducible generator in RAM
            rng = np.random.default_rng(seed)

            # Generate zero-vector SAD samples on static tiles for noise floor estimation
            # Uniform noise in [0, 4] per pixel
            calib_px = rng.uniform(0, 4, size=(1000, b))
            mu, R = noise_floor_q4(calib_px)

            # Test tiles under noise
            test_px = rng.uniform(0, 4, size=(n_tiles, b))
            residual_sums = test_px.sum(axis=1)

            for alpha in [0.01, 0.001]:
                # Evaluate float and int paths
                is_noise_flt = tile_is_noise(
                    residual_sums, b, mu, R, alpha, use_integer=False
                )
                is_noise_int = tile_is_noise(
                    residual_sums, b, mu, R, alpha, use_integer=True
                )

                # Claim 4 re-verified
                np.testing.assert_array_equal(
                    is_noise_flt,
                    is_noise_int,
                    err_msg=f"Float/int mismatch for seed {seed}, alpha {alpha}",
                )

                # Claim 1: Empirical false alarm rate (~tile_is_noise) is <= alpha
                fa_rate = np.mean(~is_noise_flt)
                self.assertLessEqual(
                    fa_rate,
                    alpha,
                    f"Seed {seed}, alpha {alpha}: false alarm rate {fa_rate} > {alpha}",
                )

                # Claim 2: A 6-level step change on a tile is never classed noise
                step_residual_sums = (test_px + 6.0).sum(axis=1)
                step_is_noise = tile_is_noise(
                    step_residual_sums, b, mu, R, alpha, use_integer=False
                )
                self.assertFalse(
                    np.any(step_is_noise),
                    f"Seed {seed}, alpha {alpha}: Step change tile was classed as noise",
                )


if __name__ == "__main__":
    unittest.main()
