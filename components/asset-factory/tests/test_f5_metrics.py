import unittest

from asset_factory import core
from tools.render import metrics
from tools.render.pipeline import HERE, Refusal


def observed_detail(candidate, reference):
    """The gate requires independent RGB evidence in addition to gradients."""
    np, _ = metrics.modules()
    result = metrics.detail(candidate, reference)
    result["rgb_mse_ppm"] = metrics.rgb_mse_ppm(
        np.repeat(candidate[:, :, None], 3, axis=2),
        np.repeat(reference[:, :, None], 3, axis=2))
    return result


class MetricTests(unittest.TestCase):
    def setUp(self):
        self.np, _ = metrics.modules()
        y, x = self.np.indices((64, 64))
        self.image = ((x * x + 13 * y + x * y) % 160 + 40).astype(self.np.int32)
        self.policy = core.load_json(HERE / "policy.json", max_bytes=16384)

    def test_static_clip_green_and_flicker_clip_red_despite_zero_noise(self):
        a = self.image
        clean = metrics.reference_delta(a, a, a, a)
        flicker = metrics.reference_delta(a - 20, a + 20, a, a)
        detail = observed_detail(a, a)
        self.assertEqual(metrics.gate(noise=0, reference_residual=clean, detail_metrics=detail,
                                     motion=None, policy=self.policy)["rc"], 0)
        verdict = metrics.gate(noise=0, reference_residual=flicker, detail_metrics=detail,
                               motion=None, policy=self.policy)
        self.assertEqual(verdict["rc"], 5)
        self.assertIn("reference_flicker_above_ceiling", verdict["failed"])

    def test_true_motion_and_light_change_cancel_against_reference(self):
        a = self.image
        b = self.np.roll(a, 4, axis=1) + 10
        self.assertEqual(metrics.reference_delta(a, b, a, b), 0)

    def test_reference_explains_lighting_that_motion_proxy_cannot(self):
        proxy = {"motion_residual_ppm": 100000, "motion_worst_tile_ppm": 200000,
                 "motion_coverage_ppm": 1000000}
        self.assertEqual(metrics.gate(noise=0, reference_residual=0,
            detail_metrics=observed_detail(self.image, self.image), motion=proxy, policy=self.policy)["rc"], 0)

    def test_static_motion_compensator_green_and_smooth_flash_red(self):
        guide = self.np.repeat(self.image[:, :, None], 3, axis=2)
        clean = metrics.motion_residual(self.image, self.image, guide, guide, factor=1, tile=8, radius=2)
        flicker = metrics.motion_residual(self.image - 20, self.image + 20, guide, guide,
                                         factor=1, tile=8, radius=2)
        self.assertEqual(clean["motion_residual_ppm"], 0)
        self.assertEqual(clean["motion_coverage_ppm"], 1000000)
        self.assertGreater(flicker["motion_residual_ppm"], self.policy["flicker_max_ppm"])
        self.assertEqual(metrics.gate(noise=0, reference_residual=None,
            detail_metrics=observed_detail(self.image, self.image), motion=flicker, policy=self.policy)["rc"], 5)

    def test_clean_translation_has_zero_compensated_residual(self):
        a = self.image
        b = self.np.roll(a, 2, axis=1)
        ga, gb = (self.np.repeat(im[:, :, None], 3, axis=2) for im in (a, b))
        result = metrics.motion_residual(a, b, ga, gb, factor=1, tile=8, radius=3, max_error=0)
        self.assertEqual(result["motion_residual_ppm"], 0)
        self.assertGreater(result["motion_coverage_ppm"], 700000)
        self.assertIn("0,-2", result["motion_offsets"])

    def test_production_downsample_retains_two_pixel_motion(self):
        y, x = self.np.indices((128, 128))
        a = ((x*x + 13*y + x*y) % 160 + 40).astype(self.np.int32)
        b = self.np.roll(a, 2, axis=1)
        ga, gb = (self.np.repeat(im[:, :, None], 3, axis=2) for im in (a, b))
        result = metrics.motion_residual(a, b, ga, gb, factor=4, tile=8, radius=4, max_error=8)
        self.assertEqual(result["motion_residual_ppm"], 0)
        self.assertGreaterEqual(result["motion_coverage_ppm"], 700000)

    def test_blur_loses_detail_and_fails_even_with_zero_noise(self):
        blurred = self.np.full_like(self.image, 120)
        detail = observed_detail(blurred, self.image)
        self.assertEqual(detail["detail_ratio_ppm"], 0)
        self.assertEqual(metrics.gate(noise=0, reference_residual=0, detail_metrics=detail,
                                     motion=None, policy=self.policy)["rc"], 5)

    def test_null_detail_is_not_zero_or_pass(self):
        flat = self.np.full_like(self.image, 120)
        self.assertIsNone(observed_detail(flat, flat)["detail_ratio_ppm"])
        self.assertEqual(metrics.gate(noise=0, reference_residual=0,
            detail_metrics=observed_detail(flat, flat), motion=None, policy=self.policy)["rc"], 3)

    def test_missing_temporal_coverage_never_passes(self):
        self.assertEqual(metrics.gate(noise=0, reference_residual=None,
            detail_metrics=observed_detail(self.image, self.image), motion=None, policy=self.policy)["rc"], 3)

    def test_integer_metrics_repeat_byte_exact(self):
        results = [core.canonical_json(observed_detail(self.image, self.image)) for _ in range(2)]
        self.assertEqual(*results)
        self.assertNotIn(".", results[0])

    def test_flicker_and_detail_exact_threshold_boundaries(self):
        d = observed_detail(self.image, self.image)
        threshold = self.policy["flicker_max_ppm"]
        self.assertEqual(metrics.gate(noise=0, reference_residual=threshold, detail_metrics=d,
                                     motion=None, policy=self.policy)["rc"], 0)
        self.assertEqual(metrics.gate(noise=0, reference_residual=threshold + 1, detail_metrics=d,
                                     motion=None, policy=self.policy)["rc"], 5)
        d["detail_ratio_ppm"] = self.policy["detail_min_ppm"]
        self.assertEqual(metrics.gate(noise=0, reference_residual=0, detail_metrics=d,
                                     motion=None, policy=self.policy)["rc"], 0)
        d["detail_ratio_ppm"] -= 1
        self.assertEqual(metrics.gate(noise=0, reference_residual=0, detail_metrics=d,
                                     motion=None, policy=self.policy)["rc"], 5)

    def test_dimension_and_float_input_refused(self):
        with self.assertRaises(Refusal):
            metrics.reference_delta(self.image, self.image[:2], self.image, self.image)
        with self.assertRaises(Refusal):
            metrics.reference_delta(self.image.astype(float), self.image, self.image, self.image)

    def test_one_pixel_residual_cannot_round_to_zero(self):
        a = self.image.copy()
        a[0, 0] += 1
        self.assertGreater(metrics.reference_delta(self.image, a, self.image, self.image), 0)


if __name__ == "__main__":
    unittest.main()
