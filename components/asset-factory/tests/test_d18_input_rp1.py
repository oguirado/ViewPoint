"""Discriminating controls for D18 INPUT repair round 1."""
import hashlib
from fractions import Fraction
from pathlib import Path
import unittest

from asset_factory import domains
from gates import input_gate
import test_d18_input_contract as fixtures
from tools.input.evidence import bytes_digest
from tools.input.frames import DecodedVideo, Frame


def fixture(cls):
    instance = cls()
    instance.setUp()
    return instance


class DomainsRepair(unittest.TestCase):
    def test_quaternion_rotation_and_legacy_exact_matrix(self):
        camera = {"rotation": [[[3, 5], [-4, 5], [0, 1]],
                               [[4, 5], [3, 5], [0, 1]],
                               [[0, 1], [0, 1], [1, 1]]],
                  "translation_um": [[0, 1], [0, 1], [1, 1]], "intrinsics": [[1, 1]] * 4}
        with self.assertRaisesRegex(ValueError, "invalid_quaternion"):
            domains.pinhole({k: v for k, v in camera.items() if k != "rotation"}
                            | {"rotation_quaternion_xyzw": [[0, 1]] * 4})
        parameterized = {k: v for k, v in camera.items() if k != "rotation"}
        parameterized["rotation_quaternion_xyzw"] = [[0, 1], [0, 1], [1, 2], [1, 1]]
        self.assertEqual(domains.pinhole(parameterized)([1, 0, 0]), domains.pinhole(camera)([1, 0, 0]))
        self.assertEqual(domains.camera_center(parameterized), (0, 0, -1))

        parameterized["rotation_quaternion_xyzw"] = [[0, 1], [0, 1], [267949, 1000000], [1, 1]]
        x, y = domains.pinhole(parameterized)([1, 0, 0])
        cosine, sine = (x - 1000) / 1000, (y - 1000) / 1000
        self.assertEqual(cosine*cosine + sine*sine, 1)
        self.assertLess(abs(sine - Fraction(1, 2)), Fraction(1, 1000000))
        with self.assertRaisesRegex(ValueError, "invalid_camera"):
            domains.pinhole(camera | {"rotation_quaternion_xyzw": [[0, 1]] * 4})

    def test_compose_crop_inverse_and_legacy_anisotropic_map(self):
        first = domains.crop_transform({"rect": [1, 2, 5, 6], "size": [8, 2]}, [10, 10])
        self.assertEqual(first, [[2, 1], [1, 2], [-2, 1], [-1, 1]])
        second = domains.crop_transform({"rect": [2, 0, 6, 2], "size": [3, 7]}, [8, 2])
        full = domains.compose_crop(first, second)
        self.assertEqual(full, [[3, 2], [7, 4], [-3, 1], [-7, 2]])
        self.assertEqual(domains.compose_crop(full, domains.invert_crop(full)),
                         [[1, 1], [1, 1], [0, 1], [0, 1]])
        with self.assertRaises(ValueError):
            domains.invert_crop([[0, 1], [1, 1], [0, 1], [0, 1]])

    def test_legacy_gate_source_identity(self):
        self.assertEqual(hashlib.sha256(Path(input_gate.__file__).read_bytes()).hexdigest(),
                         "3a93e7a42b062dcd119e77603582e90cd58ae006926cdd038020d3ab868af75b")


class ViewsRepair(unittest.TestCase):
    def test_selected_views_share_pose(self):
        views = fixture(fixtures.Views)
        views.row("back")["pose_digest"] = bytes_digest(b"other capture pose")
        self.assertEqual(views.run_gate()["reason_codes"], ["selected_view_pose_mismatch"])

    def test_side_sign_uses_observed_landmark_order(self):
        views = fixture(fixtures.Views)
        left = views.row("left")
        left["landmarks"] = [
            {"id": "left_ear", "world_um": [1, 0, 0],
             "pixel_millipx": [4000, 4000], "uncertainty_millipx": 100},
            {"id": "nose_tip", "world_um": [0, -1, 0],
             "pixel_millipx": [4500, 4000], "uncertainty_millipx": 100},
        ]
        self.assertEqual(views.run_gate()["reason_codes"], ["side_landmark_mirror_sign"])

    def test_all_decoded_pts_and_indices_are_checked(self):
        views = fixture(fixtures.Views)
        fr = views.video.frames
        for bad, code in ((Frame(len(fr), (0, 1), fr[0].rgb), "decoded_pts_not_increasing"),
                          (Frame(len(fr) + 1, (2, 1), fr[0].rgb), "decoded_indices_not_contiguous")):
            with self.subTest(code=code):
                views.video = DecodedVideo(views.source, 0, 8, 8, (*fr, bad))
                self.assertEqual(views.run_gate()["reason_codes"], [code])

    def test_registered_crop_rejects_anisotropy(self):
        views = fixture(fixtures.Views)
        views.row("front")["crop"]["size"] = [8, 4]
        self.assertEqual(views.run_gate()["reason_codes"], ["anisotropic_registered_crop"])


class MatteRepair(unittest.TestCase):
    def test_selection_and_full_transform_chain(self):
        matte = fixture(fixtures.Mattes)
        result = matte.admit(matte.payload())
        self.assertEqual(result["source_rc"], 0, result)
        self.assertEqual(result["registration"]["selection_digest"], matte.intent["selection_digest"])
        chain = result["generator"]["transforms"]
        self.assertEqual(chain["frame_to_generator"], [[1, 1], [1, 1], [-3, 1], [-3, 1]])
        self.assertEqual(chain["generator_to_frame"], [[1, 1], [1, 1], [3, 1], [3, 1]])

    def test_selected_frame_bytes_and_crop_cannot_be_substituted(self):
        matte = fixture(fixtures.Mattes)
        matte.intent["view"] = "face"
        self.assertEqual(matte.admit(matte.payload())["reason_codes"], ["matte_crop_transform_drift"])
        matte.intent["view"] = "front"
        changed = list(matte.rgb)
        changed[0] = 1
        matte.rgb[:] = changed
        matte.original = fixtures.png("RGB", matte.size, changed)
        matte.intent["original_digest"] = bytes_digest(matte.original)
        self.assertEqual(matte.admit(matte.payload())["reason_codes"], ["matte_original_pixels_drift"])

    def test_boundary_band_subthreshold_leak_and_fine_structure(self):
        matte = fixture(fixtures.Mattes)
        self.assertEqual(matte.admit(matte.payload())["metrics"].get("boundary_iou"), [[1, 1], [1, 1]])
        matte.intent["support_min"] = 128
        matte.alpha[10] = 1
        self.assertEqual(matte.admit(matte.payload())["reason_codes"], ["low_alpha_outside_reference_mask"])
        matte.alpha[10] = 0
        matte.alpha[17] = 128
        self.assertEqual(matte.admit(matte.payload())["reason_codes"], ["alpha_outside_reference_mask"])
        matte.alpha[17] = 0
        matte.alpha[4 * 8 + 4] = 0
        matte.intent["regions"]["fine_structure"] = [[4, 4, 5, 5]]
        self.assertEqual(matte.admit(matte.payload())["reason_codes"], ["protected_fine_structure_missing"])
        matte.alpha[4 * 8 + 4] = 255
        self.assertEqual(matte.admit(matte.payload())["reason_codes"],
                         ["protected_fine_structure_lost_in_generator_crop"])
        matte.intent["regions"]["fine_structure"] = []
        matte.alpha[2 * 8 + 2] = 0
        self.assertEqual(matte.admit(matte.payload())["reason_codes"], ["boundary_iou_below_intent"])


class RawRepair(unittest.TestCase):
    def test_degenerate_budget_and_forbidden_overlap_are_distinct(self):
        raw = fixture(fixtures.RawIntent)
        raw.intent["max_degenerate_faces"] = 1
        points = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (-1, 0, 0), (-0.5, 0, 0)]
        accepted = raw.gate(points, [0, 1, 2, 0, 1, 1])
        self.assertEqual(accepted["source_rc"], 0, accepted)
        self.assertEqual(accepted["metrics"]["degenerate_faces"], [[1, 1], [1, 1]])
        uncertain = raw.gate(points, [0, 1, 2, 0, 3, 4])
        self.assertEqual((uncertain["source_rc"], uncertain["reason_codes"]),
                         (3, ["degenerate_forbidden_overlap_unresolved"]))
        raw.intent["max_degenerate_faces"] = 0
        exhausted = raw.gate(points, [0, 1, 2, 0, 1, 1])
        self.assertEqual((exhausted["source_rc"], exhausted["reason_codes"]),
                         (6, ["degenerate_face_budget_exceeded"]))
        del raw.intent["max_degenerate_faces"]
        self.assertEqual(raw.gate(points, [0, 1, 2])["source_rc"], 0)
        self.assertEqual(raw.gate(points, [0, 1, 2, 0, 1, 1])["reason_codes"],
                         ["degenerate_face_budget_missing"])
