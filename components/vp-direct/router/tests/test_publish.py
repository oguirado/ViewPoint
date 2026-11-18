from copy import deepcopy
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import ValidationError


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from control.core.control import CapabilityRegistry, canonical_bytes, digest
from router import publish, router


class IndexPublicationTest(unittest.TestCase):
    def setUp(self):
        self.registry = CapabilityRegistry()
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_publish(self, *args):
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        stderr = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = publish.main(["--catalog-only", *args])
        return rc, stdout.buffer.getvalue(), stderr.getvalue()

    def test_final_registry_rebuild_is_byte_identical(self):
        first, first_pins = publish.rebuild_inputs(self.registry)
        reordered = CapabilityRegistry(reversed(self.registry.entries()))
        second, second_pins = publish.rebuild_inputs(reordered)
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        self.assertEqual(first_pins, second_pins)
        self.assertEqual(first["registry_sha256"], digest(self.registry.entries()))
        self.assertEqual(first["entries"], router._index_entries(self.registry.entries()))
        self.assertEqual((first["model_facts_sha256"], first["registry_sha256"]),
                         (digest([]), json.loads(publish.MCP_MANIFEST.read_bytes())["registry_sha256"]))

    def test_missing_held_out_emits_only_open_receipt(self):
        with patch.object(router, "build_index", side_effect=AssertionError("scoring was called")):
            rc, raw, err = self.run_publish()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(raw, Path(publish.__file__).with_name("index-rebuild.json").read_bytes())
        receipt = json.loads(raw)
        self.assertEqual(receipt["calibration"], "open")
        self.assertNotIn("index_sha256", receipt)
        with self.assertRaises(ValidationError):
            router.validate_contract(router.INDEX, receipt)

    def test_exposure_drift_refuses_even_with_preserved_registry_digest(self):
        for name, rows in (("MCP_MANIFEST", "tools"), ("CLI_MANIFEST", "verbs")):
            with self.subTest(manifest=name):
                data = json.loads(getattr(publish, name).read_bytes())
                data[rows].pop()
                drifted = self.root / "drifted.json"
                drifted.write_bytes(canonical_bytes(data))
                with patch.object(publish, name, drifted):
                    rc, raw, err = self.run_publish()
                self.assertEqual((rc, raw), (1, b""))
                self.assertIn("Exposure manifest differs from C1", err)

    def test_review_gate_rejects_golden_and_disguised_development_prompt(self):
        known = publish._GOLDEN.read_bytes()
        one_case = deepcopy(json.loads(known)["entries"][0])
        one_case["id"] = "new-id"
        one_case["request"] = one_case["request"].upper() + "!?"
        copy = self.root / "relabelled.json"
        copied = canonical_bytes({"schema": "vp.router_golden/v1", "entries": [one_case]})
        copy.write_bytes(copied)
        with patch.object(router, "build_index", side_effect=AssertionError("scoring was called")):
            for path, raw, error in ((publish._GOLDEN, known, "development golden set"),
                                     (copy, copied, "overlap")):
                with self.subTest(path=path.name):
                    rc, output, err = self.run_publish(
                        "--held-out", str(path), "--held-out-sha256", sha256(raw).hexdigest(),
                        "--manifest-generation", "1", "--score-min", "600",
                    )
                    self.assertEqual((rc, output), (1, b""))
                    self.assertIn(error, err)

    def test_reviewed_digest_mismatch_is_refused_before_scoring(self):
        changed = self.root / "changed.json"
        changed.write_bytes(b"{}\n")
        with patch.object(router, "build_index", side_effect=AssertionError("scoring was called")):
            rc, raw, err = self.run_publish(
                "--held-out", str(changed), "--held-out-sha256", sha256(b"[]\n").hexdigest(),
                "--manifest-generation", "1", "--score-min", "600",
            )
        self.assertEqual((rc, raw), (1, b""))
        self.assertIn("differ from their reviewed digest", err)

    def test_builder_rechecks_the_pin_after_held_out_review(self):
        selected = self.root / "selected.json"
        raw = canonical_bytes({"schema": "vp.router_golden/v1", "entries": [
            {"request": "synthetic negative control for a changed review pin"},
        ]})
        selected.write_bytes(raw)
        review = publish.reviewed_judgments

        def change_file_after_review(path, expected_digest):
            path = review(path, expected_digest)
            path.write_bytes(b"{}\n")
            return path

        with patch.object(publish, "reviewed_judgments", side_effect=change_file_after_review):
            with patch.object(router, "_rank", side_effect=AssertionError("scoring was called")):
                rc, output, err = self.run_publish(
                    "--held-out", str(selected), "--held-out-sha256", sha256(raw).hexdigest(),
                    "--manifest-generation", "1", "--score-min", "600",
                )
        self.assertEqual((rc, output), (1, b""))
        self.assertIn("differ from their reviewed digest", err)

    def test_mutant_digest_only_manifest_gate_accepts_a_missing_tool(self):
        data = json.loads(publish.MCP_MANIFEST.read_bytes())
        data["tools"].pop()
        drifted = self.root / "mcp.json"
        drifted.write_bytes(canonical_bytes(data))
        with patch.object(publish, "MCP_MANIFEST", drifted):
            with self.assertRaisesRegex(ValueError, "Exposure manifest differs"):
                publish.rebuild_inputs(self.registry)
            # A digest-only check accepts the changed projection. The real gate compares all bytes.
            self.assertEqual(data["registry_sha256"], digest(self.registry.entries()))
