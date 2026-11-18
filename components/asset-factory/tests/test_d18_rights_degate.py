"""Model declarations never authorize or deny a technical qualification."""
import unittest

from asset_factory import core
from tools.qualification import manifest
import test_n18_qual_forbidden as fixtures


class QualificationDeclarations(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ForbiddenComponentsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.candidate = self.fixture.candidate

    def test_unknown_and_noncommercial_qualify_without_grants(self):
        self.candidate.pop("rights")
        self.candidate["dependencies"][0].pop("licence")
        self.candidate["dependencies"][0]["name"] = "briaai/RMBG-1.4"
        self.candidate["licence_metadata"] = {
            "licence": "CC-BY-NC-4.0", "licence_url": "https://creativecommons.org/licenses/by-nc/4.0/",
            "jurisdiction": "user-declared", "commercial_use": True, "declaration": "unproven",
        }
        snapshot = core.sha256_json(self.candidate)
        manifest.check_candidate(self.candidate, manifest.Pins())
        self.assertEqual(core.sha256_json(self.candidate), snapshot)

    def test_integrity_refuses_unpinned_bytes_even_without_grants(self):
        self.candidate.pop("rights")
        self.candidate["weights"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(manifest.Refusal, "^pin_digest_mismatch$"):
            manifest.check_candidate(self.candidate, manifest.Pins())

    def test_legacy_unproven_verdict_is_informational(self):
        self.fixture.bind_rights(self.candidate, verdict="CANNOT_SEE")
        manifest.check_candidate(self.candidate, manifest.Pins())
