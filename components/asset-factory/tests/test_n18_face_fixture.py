"""The FACE fixture uses disjoint fit and reference observations."""
import unittest
from fractions import Fraction

from asset_factory import domains
from tools.face.fit import camera_projection, fit_axes, project_errors
from tools.face.evaluation import image_errors, require_reference, require_separation
from tools.input.evidence import InputIssue


def camera(rows):
    return {"rotation": [[domains.pair(v) for v in row] for row in rows],
            "translation_um": [[0, 1], [0, 1], [500000, 1]],
            "intrinsics": [[120, 1], [120, 1], [40, 1], [40, 1]]}


FRONT = camera([(1, 0, 0), (0, 0, -1), (0, 1, 0)])
PROFILE = camera([(0, 1, 0), (0, 0, -1), (-1, 0, 0)])


class FitTests(unittest.TestCase):
    def setUp(self):
        self.points = [[10000, 2000, -20000], [25000, -11000, 45000], [15000, -9000, -18000]]
        self.scales = (Fraction(5, 4), Fraction(3, 2), Fraction(4, 5))
        self.truth = [[int(v*s) for v, s in zip(p, self.scales)] for p in self.points]
        self.views = {}
        for name, cam in (("front", FRONT), ("profile", PROFILE)):
            self.views[name] = {"camera": cam, "landmarks": [
                {"vertex": i, "xy_millipx": [domains.pair(v) for v in domains.pinhole(cam)(self.truth[i])]}
                for i in (0, 1)]}

    def test_registered_observations_recover_nonidentity_scales(self):
        self.assertEqual(fit_axes(self.points, self.views), self.scales)

    def test_missing_profile(self):
        del self.views["profile"]
        with self.assertRaisesRegex(InputIssue, "profile"):
            fit_axes(self.points, self.views)

    def test_singular_fit(self):
        with self.assertRaisesRegex(InputIssue, "rank"):
            fit_axes([[0, 0, 0]] * 3, self.views)

    def test_heldout_requires_profile(self):
        with self.assertRaisesRegex(InputIssue, "profile"):
            project_errors(self.truth, {"front": self.views["front"]}, set())

    def test_reused_landmark_refused(self):
        with self.assertRaisesRegex(InputIssue, "held_out"):
            project_errors(self.truth, self.views, {0})

    def test_shifted_mouth_landmark(self):
        self.truth[0][2] -= 8000
        self.assertGreater(project_errors(self.truth, self.views, set())["front"], 1000)

    def test_candidate_derived_observation_refused(self):
        with self.assertRaisesRegex(InputIssue, "reference_origin"):
            require_reference("fit_result", "a"*64, "a"*64, {"b"*64})


class CameraSeparationTests(unittest.TestCase):
    def setUp(self):
        self.fit = camera([(1, 0, 0), (0, 1, 0), (0, 0, 1)])
        self.fit["translation_um"] = [[0, 1], [0, 1], [0, 1]]
        self.designated = [camera_projection(self.fit)]

    def tilted(self, numerator, denominator):
        return {"rotation_quaternion_xyzw": [[0, 1], [numerator, denominator], [0, 1], [1, 1]],
                "intrinsics": self.fit["intrinsics"], "translation_um": self.fit["translation_um"]}

    def test_same_axis_translations_are_refused(self):
        for offset in ((9999, 0, 0), (10000, 0, 0), (0, 0, 50000)):
            with self.subTest(offset=offset):
                moved = {**self.fit, "translation_um": [[axis, 1] for axis in offset]}
                with self.assertRaisesRegex(InputIssue, "held_out_camera_reused") as caught:
                    require_separation(moved, self.designated)
                self.assertEqual(caught.exception.rc, 5)

    def test_axis_cosine_just_above_limit_is_refused(self):
        held = self.tilted(86, 983)
        cosine = camera_projection(held)[0][2][2]
        self.assertGreater(cosine, Fraction(984807, 1000000))
        with self.assertRaisesRegex(InputIssue, "held_out_camera_reused") as caught:
            require_separation(held, self.designated)
        self.assertEqual(caught.exception.rc, 5)

    def test_axis_cosine_just_below_limit_is_accepted(self):
        held = self.tilted(7, 80)
        cosine = camera_projection(held)[0][2][2]
        self.assertLess(cosine, Fraction(984807, 1000000))
        require_separation(held, self.designated)

    def test_small_rotations_are_refused(self):
        for denominator in (50, 40):
            with self.subTest(denominator=denominator):
                with self.assertRaisesRegex(InputIssue, "held_out_camera_reused"):
                    require_separation(self.tilted(1, denominator), self.designated)

    def test_separation_is_required_from_every_fit_camera(self):
        held = self.tilted(7, 80)
        # The second fit camera has the same axis as the holdout at a different centre.
        matched = {**held, "translation_um": [[0, 1], [0, 1], [50000, 1]]}
        with self.assertRaisesRegex(InputIssue, "held_out_camera_reused"):
            require_separation(held, [*self.designated, camera_projection(matched)])


class ImageTests(unittest.TestCase):
    def test_protected_tile_independent_of_rest_of_raster(self):
        original = bytes([127,127,127]) * 256
        changed = bytearray(original)
        changed[0:3] = bytes([131,131,131])
        metrics = image_errors(original, bytes(changed), [16,16], [0,0,4,4], 1)
        self.assertEqual(metrics["worst_tile_error_upper_ppm"], 15687)
        self.assertLess(metrics["max_channel_error_upper_ppm"], 20000)
        self.assertFalse(metrics["within_targets"])

    def test_identical_independently_rendered_bytes_allowed(self):
        pixels = bytes([0, 27, 44]) * 4
        self.assertTrue(image_errors(pixels, pixels, [2,2], [0,0,2,2], 2)["within_targets"])
