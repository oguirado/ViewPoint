import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image, ImageDraw

from asset_factory import core
from providers.birefnet.qualification.adapter import evaluate, frozen_input
from tools.input.evidence import InputIssue
from tools.input.matte import _png


class MattePilotTests(unittest.TestCase):
    def setUp(self):
        self.original = Image.new("RGB", (16, 16), (200, 160, 120))
        self.mask = Image.new("L", self.original.size)
        ImageDraw.Draw(self.mask).rectangle((3, 3, 12, 12), fill=255)
        self.master = self.original.convert("RGBA")
        alpha = self.mask.copy()
        alpha.putpixel((6, 5), 128)
        self.master.putalpha(alpha)
        self.regions = {"extremities": [[6, 5, 7, 6]], "fine_structure": [[7, 5, 8, 6]],
                        "holes": [], "border": []}

    def check(self, submitted=None):
        return evaluate(self.original, self.master, self.mask, self.regions, 128, [1, 1],
                        submitted=submitted)

    def change_alpha(self, coordinate, alpha):
        self.master.putpixel(coordinate, (*self.original.getpixel(coordinate), alpha))

    def test_once_premultiplied_positive(self):
        derived, result = self.check()
        self.assertEqual(result["reason_codes"], [], result)
        self.assertEqual(result["boundary_band"]["low_alpha_outside_pixels"], 0)
        self.assertEqual(Image.open(io.BytesIO(derived)).getpixel((2, 1)), (100, 80, 60))
        self.assertEqual(self.check(derived)[1], result)

    def test_double_premultiplication_is_refused(self):
        derived, _ = self.check()
        image = Image.open(io.BytesIO(derived))
        image.putpixel((2, 1), (50, 40, 30))
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        self.assertEqual(self.check(stream.getvalue())[1]["reason_codes"], ["submitted_derivative_mismatch"])

    def test_erased_finger_is_refused(self):
        self.change_alpha((6, 5), 0)
        result = self.check()[1]
        self.assertEqual(result["source_rc"], 5)
        self.assertEqual(result["reason_codes"], ["protected_extremity_missing"])

    def test_erased_hair_is_refused(self):
        self.change_alpha((7, 5), 0)
        self.assertEqual(self.check()[1]["reason_codes"], ["protected_fine_structure_missing"])

    def test_low_alpha_halo_is_refused(self):
        self.change_alpha((2, 5), 1)
        result = self.check()[1]
        self.assertEqual(result["reason_codes"], ["low_alpha_outside_reference_mask"])
        self.assertEqual(result["boundary_band"]["low_alpha_outside_pixels"], 1)
        self.assertEqual(result["metrics"]["boundary_iou"], [[1, 1], [1, 1]])

    def test_stale_reference_is_refused(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mask.png"
            self.mask.save(path)
            with self.assertRaisesRegex(ValueError, "frozen_input_digest_mismatch"):
                frozen_input({"path": str(path), "sha256": "0" * 64})

    def test_truncated_png_does_not_yield_an_observation(self):
        encoded = io.BytesIO()
        self.mask.save(encoded, format="PNG")
        with self.assertRaises(InputIssue) as ctx:
            _png(encoded.getvalue()[:40], "L", 1024, 256)
        self.assertEqual((ctx.exception.rc, ctx.exception.code), (2, "invalid_matte_png"))

    def test_result_schema_matches_evaluator(self):
        schema_path = Path("providers/birefnet/qualification/result.schema.json")
        schema = core.load_json(schema_path, max_bytes=8192)
        payload = {
            "schema": "af.birefnet-cpu-pilot-result/v1", "scope": "diagnostic-retained-still",
            "reference": "ref2", "freeze_sha256": "0" * 64, "inference_ns": 1, "evaluation_ns": 1,
            "assessment": self.check()[1], "derivative_available": True,
            "digests": dict.fromkeys(("original.png", "straight.png", "diagnostic-rgb.png"), "0" * 64),
        }
        core.validate(payload, schema)
        del payload["assessment"]["source_rc"]
        with self.assertRaises(core.ContractError):
            core.validate(payload, schema)


if __name__ == "__main__":
    unittest.main()
