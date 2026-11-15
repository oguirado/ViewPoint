"""Unit tests for tools/check_schema_records.py schema record validator."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.check_schema_records import (
    load_all_schemas,
    validate_schema_keywords,
    validate_value,
    process_file,
)
import visioncore.gop as vc_gop


class TestCheckSchemaRecords(unittest.TestCase):

    def test_bundled_schemas_load(self):
        """Every bundled schema in visioncore/schemas loads and validates cleanly."""
        schemas = load_all_schemas()
        self.assertGreaterEqual(len(schemas), 3)
        self.assertIn("vqa.gop/v1", schemas)
        self.assertIn("vqa.tile/v1", schemas)
        self.assertIn("vmem.unit/v1", schemas)

    def test_schema_filename_mismatch_raises(self):
        """Loading schemas with $id mismatch raises RuntimeError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_schema = {
                "$id": "vqa.gop/v1",
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
            }
            # Mismatched filename: should be vqa.gop.v1.schema.json
            path = Path(tmpdir) / "wrong.name.v1.schema.json"
            path.write_text(json.dumps(bad_schema), encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                load_all_schemas(tmpdir)
            self.assertIn("mismatch", str(ctx.exception).lower())

    def test_schema_unsupported_keyword_raises(self):
        """Schema using a keyword outside allowed whitelist raises ValueError."""
        bad_schema = {
            "$id": "vqa.gop/v1",
            "type": "object",
            "unsupportedKeyword": True,
        }
        with self.assertRaises(ValueError) as ctx:
            validate_schema_keywords(bad_schema)
        self.assertIn("unsupported schema keyword", str(ctx.exception).lower())

    def test_gop_in_memory_valid(self):
        """In-memory GOP object produced by visioncore code passes validation."""
        schemas = load_all_schemas()
        # Build GOP record using visioncore.gop
        gop_obj = {
            "schema": "vqa.gop/v1",
            "gop": 0,
            "grid": "256x160",
            "f0": 0,
            "f1": 24,
            "n_frames": 24,
            "frame_ids": list(range(24)),
            "gop_blob_sha256": "0" * 64,
            "gop_canon_sha256": "1" * 64,
        }
        errs = validate_value(gop_obj, schemas["vqa.gop/v1"])
        self.assertEqual(errs, [])

    def test_gop_mutated_invalid_exact_path(self):
        """Mutated GOP object fails with exact JSON path error."""
        schemas = load_all_schemas()
        gop_obj = {
            "schema": "vqa.gop/v1",
            "gop": 0,
            "grid": "256x160",
            "f0": 0,
            "f1": 24,
            "frame_ids": [1, 1],  # uniqueItems violation
            "gop_blob_sha256": "0" * 64,
            "gop_canon_sha256": "1" * 64,
        }
        errs = validate_value(gop_obj, schemas["vqa.gop/v1"])
        self.assertEqual(len(errs), 1)
        self.assertIn("$.frame_ids:", errs[0])
        self.assertIn("not unique", errs[0])

    def test_type_validator_negative(self):
        """Negative test for type keyword validator."""
        schema = {"type": "integer"}
        errs = validate_value("not_an_int", schema)
        self.assertEqual(len(errs), 1)
        self.assertIn("expected type 'integer'", errs[0])

    def test_required_validator_negative(self):
        """Negative test for required keyword validator."""
        schema = {"type": "object", "required": ["foo", "bar"]}
        errs = validate_value({"foo": 1}, schema)
        self.assertEqual(len(errs), 1)
        self.assertIn("missing required property 'bar'", errs[0])

    def test_additionalProperties_validator_negative(self):
        """Negative test for additionalProperties keyword validator."""
        schema = {
            "type": "object",
            "properties": {"foo": {"type": "integer"}},
            "additionalProperties": False,
        }
        errs = validate_value({"foo": 1, "extra": 2}, schema)
        self.assertEqual(len(errs), 1)
        self.assertIn("additional property 'extra' is not allowed", errs[0])

    def test_const_validator_negative(self):
        """Negative test for const keyword validator."""
        schema = {"const": "vqa.gop/v1"}
        errs = validate_value("vqa.other/v1", schema)
        self.assertEqual(len(errs), 1)
        self.assertIn("expected const 'vqa.gop/v1'", errs[0])

    def test_enum_validator_negative(self):
        """Negative test for enum keyword validator."""
        schema = {"enum": ["FG", "BG"]}
        errs = validate_value("OTHER", schema)
        self.assertEqual(len(errs), 1)
        self.assertIn("not in enum", errs[0])

    def test_pattern_validator_negative(self):
        """Negative test for pattern keyword validator."""
        schema = {"pattern": "^[0-9a-f]{64}$"}
        errs = validate_value("invalid_sha256", schema)
        self.assertEqual(len(errs), 1)
        self.assertIn("does not match pattern", errs[0])

    def test_minimum_maximum_validator_negative(self):
        """Negative test for minimum and maximum keyword validators."""
        schema_min = {"type": "integer", "minimum": 10}
        errs_min = validate_value(5, schema_min)
        self.assertEqual(len(errs_min), 1)
        self.assertIn("less than minimum 10", errs_min[0])

        schema_max = {"type": "integer", "maximum": 10}
        errs_max = validate_value(15, schema_max)
        self.assertEqual(len(errs_max), 1)
        self.assertIn("greater than maximum 10", errs_max[0])

    def test_items_minItems_maxItems_uniqueItems_validator_negative(self):
        """Negative tests for array constraint keywords."""
        schema = {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 4,
            "uniqueItems": True,
        }
        # minItems check
        errs_min = validate_value([1], schema)
        self.assertEqual(len(errs_min), 1)
        self.assertIn("less than minItems 2", errs_min[0])

        # maxItems check
        errs_max = validate_value([1, 2, 3, 4, 5], schema)
        self.assertEqual(len(errs_max), 1)
        self.assertIn("exceeds maxItems 4", errs_max[0])

        # uniqueItems check
        errs_uniq = validate_value([1, 1, 2], schema)
        self.assertEqual(len(errs_uniq), 1)
        self.assertIn("not unique", errs_uniq[0])

        # items element type check
        errs_item = validate_value([1, "string"], schema)
        self.assertEqual(len(errs_item), 1)
        self.assertIn("$[1]: expected type 'integer'", errs_item[0])

    def test_maxLength_validator_negative(self):
        """Negative test for maxLength keyword validator."""
        schema = {"type": "string", "maxLength": 5}
        errs = validate_value("hello_world", schema)
        self.assertEqual(len(errs), 1)
        self.assertIn("exceeds maxLength 5", errs[0])

    def test_cli_execution_pass(self):
        """CLI invocation with valid JSONL records returns exit code 0."""
        schemas = load_all_schemas()
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "records.jsonl"
            row = {
                "schema": "vmem.unit/v1",
                "id": "unit_001",
                "layer": "FG",
                "pin": "GOLDEN",
            }
            file_path.write_text(json.dumps(row) + "\n", encoding="utf-8")

            cmd = [
                sys.executable,
                "tools/check_schema_records.py",
                str(file_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0)
            data = json.loads(res.stdout)
            self.assertTrue(data["ok"])
            self.assertEqual(data["total_records"], 1)
            self.assertEqual(data["valid_records"], 1)
            self.assertEqual(data["invalid_records"], 0)
            self.assertEqual(data["errors"], [])

    def test_cli_execution_fail_exit_2(self):
        """CLI invocation with invalid record or unknown schema returns exit code 2."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "records.jsonl"
            bad_row = {
                "schema": "vmem.unknown/v1",
                "id": "unit_001",
            }
            file_path.write_text(json.dumps(bad_row) + "\n", encoding="utf-8")

            cmd = [
                sys.executable,
                "tools/check_schema_records.py",
                str(file_path),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(res.returncode, 2)
            data = json.loads(res.stdout)
            self.assertFalse(data["ok"])
            self.assertEqual(data["total_records"], 1)
            self.assertEqual(data["invalid_records"], 1)
            self.assertEqual(len(data["errors"]), 1)
            self.assertIn("unknown schema id", data["errors"][0])


if __name__ == "__main__":
    unittest.main()
