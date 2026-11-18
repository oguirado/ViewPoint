import copy
from pathlib import Path
import unittest

from asset_factory import core
from tools.qualification.manifest import JSON_LIMIT, Refusal, check_legacy_subject


ROOT = Path(__file__).resolve().parents[1]
VERDICTS = {
    "birefnet": "CANNOT_SEE",
    "trellis2": "CANNOT_SEE",
    "unirig": "CANNOT_SEE",
    "mocapanything_v2": "CANNOT_SEE",
    "partcrafter": "FAIL",
    "realesrgan": "CANNOT_SEE",
    "triposg": "FAIL",
    "hi3d": "CANNOT_SEE",
}


def read_record(path):
    record = core.load_json(path, max_bytes=JSON_LIMIT)
    if path.read_bytes() != core.canonical_json(record).encode():
        raise AssertionError(f"Noncanonical record: {path}")
    return record


class LicenceRightsTests(unittest.TestCase):
    def test_each_wire_record_binds_its_exact_subject_and_inventory(self):
        for provider, verdict in VERDICTS.items():
            with self.subTest(provider=provider):
                directory = ROOT / "providers" / provider / "rights"
                assessment = read_record(directory / "assessment.json")
                self.assertEqual(assessment["provider"], provider)
                self.assertTrue(assessment["subjects"])
                for item in assessment["subjects"]:
                    wire_path = directory / item["rights_file"]
                    record = read_record(wire_path)
                    subject = item["subject"]
                    self.assertEqual(core.sha256_json(record), item["rights_sha256"])
                    self.assertTrue(subject["weights"])
                    self.assertEqual(record, {
                        "schema": "af.qualification-rights/v1",
                        "provider": provider,
                        "jurisdiction": "EU",
                        "commercial_use": None,
                        "verdict": verdict,
                        "subject_sha256": core.sha256_json(subject),
                    })

    def test_historical_metadata_ignores_verdict_but_preserves_exact_subject(self):
        for provider in ("birefnet", "trellis2"):
            directory = ROOT / "providers" / provider / "rights"
            for item in read_record(directory / "assessment.json")["subjects"]:
                with self.subTest(provider=provider, subject=item["name"]):
                    candidate = copy.deepcopy(item["subject"])
                    rights = read_record(directory / item["rights_file"])
                    check_legacy_subject(candidate, rights)
                    candidate["weights"][0]["sha256"] = "0" * 64
                    with self.assertRaises(Refusal) as stale:
                        check_legacy_subject(candidate, rights)
                    self.assertEqual((stale.exception.rc, stale.exception.code),
                                     (4, "rights_subject_mismatch"))

    def test_stock_noncommercial_paths_fail_and_alternatives_stay_unproven(self):
        for provider in ("partcrafter", "triposg"):
            directory = ROOT / "providers" / provider / "rights"
            assessment = read_record(directory / "assessment.json")
            components = {item["name"]: item for item in assessment["components"]}
            self.assertEqual(components["briaai/RMBG-1.4"]["disposition"], "FAIL")
            self.assertTrue(components["briaai/RMBG-1.4"]["non_commercial"])
            self.assertEqual(components["BiRefNet foreground alternative"]["disposition"],
                             "CANNOT_SEE")

    def test_trellis_keeps_conditioner_and_decoder_rights_separate(self):
        assessment = read_record(ROOT / "providers/trellis2/rights/assessment.json")
        components = {item["name"]: item for item in assessment["components"]}
        dino = components["DINOv3 ViT-L/16"]
        self.assertEqual(dino["weights_rights"]["licence"], "DINOv3 License")
        self.assertTrue(dino["weights_rights"]["commercial_allowed"])
        self.assertFalse(dino["eu_excluded"])
        self.assertTrue(dino["pins"])
        self.assertEqual(components["TRELLIS-image-large decoder"]["disposition"],
                         "CANNOT_SEE")

    def test_missing_licence_holds_and_excluded_coarse_path_fails(self):
        real = read_record(ROOT / "providers/realesrgan/rights/assessment.json")
        checkpoint = real["components"][0]
        self.assertEqual(checkpoint["disposition"], "CANNOT_SEE")
        self.assertEqual(checkpoint["weights_rights"]["licence"], "AUSENTE")
        self.assertEqual(checkpoint["evidence"], [])
        hi = read_record(ROOT / "providers/hi3d/rights/assessment.json")
        coarse = next(item for item in hi["components"] if item["name"] == "Hunyuan coarse path")
        self.assertTrue(coarse["eu_excluded"])
        self.assertEqual(coarse["disposition"], "FAIL")


if __name__ == "__main__":
    unittest.main()
