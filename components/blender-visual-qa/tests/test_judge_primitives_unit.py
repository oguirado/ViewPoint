"""Unit tests for judge package primitives in blender_visual_qa/judge/__init__.py."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from blender_visual_qa.judge import (
    CANARY_ALPHABET,
    DEFAULT_AGY_SHIM,
    DEFAULT_POLICY,
    DEFAULT_WORKER_PATH,
    DualKeyDict,
    JudgeBudgetError,
    JudgeError,
    JudgeTransportError,
    JudgeValidationError,
    _categorize_check,
    compute_canonical_sha256,
    compute_file_sha256,
    derive_canary_token,
    encode_png_rgb,
    get_default_agy_shim,
    get_default_judge_policy,
    get_default_worker_argv_prefix,
    load_judge_prompt_template,
    load_schema,
    render_canary_background,
    strict_json_loads,
)


class TestJudgePrimitivesUnit(unittest.TestCase):
    """Unit test suite for blender_visual_qa.judge primitives."""

    # 1. get_default_judge_policy
    def test_get_default_judge_policy_nominal(self) -> None:
        policy = get_default_judge_policy()
        self.assertEqual(policy, DEFAULT_POLICY)
        # Verify it returns a shallow copy, modifying it does not alter DEFAULT_POLICY
        policy["model_id"] = "custom-model"
        self.assertEqual(DEFAULT_POLICY["model_id"], "gemini-3.8-flash")

    def test_get_default_judge_policy_negative_missing_key(self) -> None:
        policy = get_default_judge_policy()
        with self.assertRaises(KeyError):
            _ = policy["non_existent_policy_key"]

    # 2. derive_canary_token
    def test_derive_canary_token_nominal(self) -> None:
        token1 = derive_canary_token("test-seed-123", length=8)
        token2 = derive_canary_token("test-seed-123", length=8)

        self.assertEqual(len(token1), 8)
        self.assertEqual(token1, token2)
        for char in token1:
            self.assertIn(char, CANARY_ALPHABET)

        # Confirm length variation
        token_len12 = derive_canary_token("test-seed-123", length=12)
        self.assertEqual(len(token_len12), 12)

    def test_derive_canary_token_negative_different_seeds(self) -> None:
        token_a = derive_canary_token("seed_a", length=8)
        token_b = derive_canary_token("seed_b", length=8)
        self.assertNotEqual(token_a, token_b)

        with self.assertRaises(AttributeError):
            derive_canary_token(12345, length=8)  # type: ignore[arg-type]

    # 3. get_default_worker_argv_prefix
    def test_get_default_worker_argv_prefix_default_and_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            prefix = get_default_worker_argv_prefix()
            self.assertEqual(prefix, [DEFAULT_WORKER_PATH])

        custom_worker = "/custom/path/to/worker"
        with patch.dict(os.environ, {"BLENDER_VISUAL_QA_GEMINI_WORKER": custom_worker}):
            prefix = get_default_worker_argv_prefix()
            self.assertEqual(prefix, [custom_worker])

    def test_get_default_worker_argv_prefix_negative_empty_env(self) -> None:
        with patch.dict(os.environ, {"BLENDER_VISUAL_QA_GEMINI_WORKER": ""}):
            prefix = get_default_worker_argv_prefix()
            self.assertEqual(prefix, [""])
            self.assertNotEqual(prefix, [DEFAULT_WORKER_PATH])

    # 4. get_default_agy_shim
    def test_get_default_agy_shim_default_and_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            shim = get_default_agy_shim()
            self.assertEqual(shim, DEFAULT_AGY_SHIM)

        custom_shim = "/custom/bin/agy"
        with patch.dict(os.environ, {"BLENDER_VISUAL_QA_AGY": custom_shim}):
            shim = get_default_agy_shim()
            self.assertEqual(shim, custom_shim)

    def test_get_default_agy_shim_negative_empty_env(self) -> None:
        with patch.dict(os.environ, {"BLENDER_VISUAL_QA_AGY": ""}):
            shim = get_default_agy_shim()
            self.assertEqual(shim, "")
            self.assertNotEqual(shim, DEFAULT_AGY_SHIM)

    # 5. JudgeError exception hierarchy
    def test_judge_error_hierarchy_and_attributes(self) -> None:
        err = JudgeError("base error", code="custom_code", stage="custom_stage")
        self.assertIsInstance(err, Exception)
        self.assertEqual(err.message, "base error")
        self.assertEqual(err.code, "custom_code")
        self.assertEqual(err.stage, "custom_stage")

        val_err = JudgeValidationError("invalid schema", code="bad_schema")
        self.assertIsInstance(val_err, JudgeError)
        self.assertEqual(val_err.message, "invalid schema")
        self.assertEqual(val_err.code, "bad_schema")
        self.assertEqual(val_err.stage, "judge")

        trans_err = JudgeTransportError("timeout", code="worker_timeout")
        self.assertIsInstance(trans_err, JudgeError)
        self.assertEqual(trans_err.code, "worker_timeout")

        budget_err = JudgeBudgetError("token limit", code="tokens_exceeded")
        self.assertIsInstance(budget_err, JudgeError)
        self.assertEqual(budget_err.code, "tokens_exceeded")

    def test_judge_error_negative_type_isolation(self) -> None:
        val_err = JudgeValidationError("validation error")
        self.assertNotIsInstance(val_err, JudgeTransportError)
        self.assertNotIsInstance(val_err, JudgeBudgetError)

    # 6. compute_canonical_sha256
    def test_compute_canonical_sha256_nominal(self) -> None:
        data1 = {"b": 2, "a": 1, "c": [3, 2, 1]}
        data2 = {"a": 1, "c": [3, 2, 1], "b": 2}

        sha1 = compute_canonical_sha256(data1)
        sha2 = compute_canonical_sha256(data2)

        self.assertEqual(sha1, sha2)
        raw_canonical = json.dumps(data1, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected_sha = hashlib.sha256(raw_canonical).hexdigest()
        self.assertEqual(sha1, expected_sha)

    def test_compute_canonical_sha256_negative(self) -> None:
        data1 = {"a": 1}
        data2 = {"a": 2}
        self.assertNotEqual(compute_canonical_sha256(data1), compute_canonical_sha256(data2))

        with self.assertRaises(TypeError):
            compute_canonical_sha256({"unsupported": object()})

    # 7. compute_file_sha256
    def test_compute_file_sha256_nominal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fpath = Path(td) / "sample.txt"
            content = b"blender visual qa canary test bytes"
            fpath.write_bytes(content)

            expected_sha = hashlib.sha256(content).hexdigest()
            calc_sha = compute_file_sha256(fpath)
            self.assertEqual(calc_sha, expected_sha)

    def test_compute_file_sha256_negative_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing_path = Path(td) / "non_existent.txt"
            with self.assertRaises(FileNotFoundError):
                compute_file_sha256(missing_path)

    # 8. strict_json_loads
    def test_strict_json_loads_nominal(self) -> None:
        json_str = '{"a": 1, "b": "hello", "c": [true, false, null]}'
        obj = strict_json_loads(json_str)
        self.assertEqual(obj, {"a": 1, "b": "hello", "c": [True, False, None]})

    def test_strict_json_loads_negative_cases(self) -> None:
        # Markdown fences
        with self.assertRaises(JudgeValidationError) as cm:
            strict_json_loads('```json\n{"a": 1}\n```')
        self.assertEqual(cm.exception.code, "output_markdown_fence")

        # Not single json object (surrounding text)
        with self.assertRaises(JudgeValidationError) as cm:
            strict_json_loads('hello {"a": 1}')
        self.assertEqual(cm.exception.code, "output_not_single_json_object")

        # Duplicate key
        with self.assertRaises(JudgeValidationError) as cm:
            strict_json_loads('{"a": 1, "a": 2}')
        self.assertEqual(cm.exception.code, "output_malformed_json")
        self.assertIn("Duplicate key", str(cm.exception))

        # Forbidden constants: NaN
        with self.assertRaises(JudgeValidationError) as cm:
            strict_json_loads('{"a": NaN}')
        self.assertEqual(cm.exception.code, "output_malformed_json")
        self.assertIn("Forbidden constant 'NaN'", str(cm.exception))

        # Forbidden constants: Infinity
        with self.assertRaises(JudgeValidationError) as cm:
            strict_json_loads('{"a": Infinity}')
        self.assertEqual(cm.exception.code, "output_malformed_json")
        self.assertIn("Forbidden constant 'Infinity'", str(cm.exception))

        # Trailing bytes after JSON object
        with self.assertRaises(JudgeValidationError) as cm:
            strict_json_loads('{"a": 1} {"b": 2}')
        self.assertEqual(cm.exception.code, "output_trailing_bytes")

        # Malformed json
        with self.assertRaises(JudgeValidationError) as cm:
            strict_json_loads('{"a": 1,}')
        self.assertEqual(cm.exception.code, "output_malformed_json")

        # Top level list instead of object
        with self.assertRaises(JudgeValidationError) as cm:
            strict_json_loads('[1, 2, 3]')
        self.assertEqual(cm.exception.code, "output_not_single_json_object")

    # 9. encode_png_rgb
    def test_encode_png_rgb_nominal_decode(self) -> None:
        width, height = 4, 3
        raw_rgb = bytes(range(width * height * 3))

        png_data = encode_png_rgb(width, height, raw_rgb)

        # Decode using in-test zlib/PNG parser
        self.assertTrue(png_data.startswith(b"\x89PNG\r\n\x1a\n"))

        idx = 8
        idat_acc = bytearray()
        ihdr_seen = False

        while idx < len(png_data):
            length, = struct.unpack(">I", png_data[idx : idx + 4])
            chunk_type = png_data[idx + 4 : idx + 8]
            chunk_data = png_data[idx + 8 : idx + 8 + length]
            crc, = struct.unpack(">I", png_data[idx + 8 + length : idx + 12 + length])

            calc_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
            self.assertEqual(crc, calc_crc, f"CRC32 mismatch for chunk {chunk_type!r}")

            if chunk_type == b"IHDR":
                w_read, h_read, depth, color, comp, filt, inter = struct.unpack(">IIBBBBB", chunk_data)
                self.assertEqual((w_read, h_read, depth, color), (width, height, 8, 2))
                ihdr_seen = True
            elif chunk_type == b"IDAT":
                idat_acc.extend(chunk_data)

            idx += 12 + length

        self.assertTrue(ihdr_seen)
        raw_scanlines = zlib.decompress(bytes(idat_acc))
        row_len = width * 3 + 1
        decoded_rgb = bytearray()
        for y in range(height):
            filter_type = raw_scanlines[y * row_len]
            self.assertEqual(filter_type, 0)
            decoded_rgb.extend(raw_scanlines[y * row_len + 1 : (y + 1) * row_len])

        self.assertEqual(bytes(decoded_rgb), raw_rgb)

    @unittest.expectedFailure
    def test_encode_png_rgb_negative_buffer_length(self) -> None:
        """exposes a bug in encode_png_rgb: buffer length mismatch is not validated."""
        width, height = 2, 2
        invalid_short_rgb = b"\x00" * 5  # requires 2*2*3 = 12 bytes
        # Currently encode_png_rgb doesn't validate pixels_rgb length, producing truncated scanlines without error.
        # It should raise JudgeValidationError or ValueError when pixels_rgb length != width * height * 3.
        png_bytes = encode_png_rgb(width, height, invalid_short_rgb)
        raw_scanlines = zlib.decompress(png_bytes[33:-12])  # IDAT content check
        if len(raw_scanlines) != height * (width * 3 + 1):
            raise JudgeValidationError("Buffer length mismatch in encode_png_rgb", code="invalid_buffer_length")

    # 10. render_canary_background
    def test_render_canary_background_nominal(self) -> None:
        w, h = 320, 96
        bg_shapes = render_canary_background(w, h, draw_shapes=True)
        self.assertEqual(len(bg_shapes), w * h * 3)
        self.assertNotEqual(bg_shapes, b"\x00" * (w * h * 3))

        bg_black = render_canary_background(w, h, draw_shapes=False)
        self.assertEqual(len(bg_black), w * h * 3)
        self.assertEqual(bg_black, b"\x00" * (w * h * 3))

    def test_render_canary_background_negative_custom_dims(self) -> None:
        w, h = 10, 10
        bg = render_canary_background(w, h, draw_shapes=False)
        self.assertEqual(len(bg), 100 * 3)
        self.assertEqual(bg, b"\x00" * 300)

    # 11. DualKeyDict
    def test_dual_key_dict_nominal(self) -> None:
        dkd = DualKeyDict({"100": "string_key_val", 200: "int_key_val"})

        # Str key lookup
        self.assertEqual(dkd["100"], "string_key_val")
        self.assertEqual(dkd[100], "string_key_val")

        # Int key lookup
        self.assertEqual(dkd[200], "int_key_val")
        self.assertEqual(dkd["200"], "int_key_val")

        # .get() lookup
        self.assertEqual(dkd.get(100), "string_key_val")
        self.assertEqual(dkd.get("200"), "int_key_val")
        self.assertEqual(dkd.get("missing", "default_val"), "default_val")

    def test_dual_key_dict_negative_missing_key(self) -> None:
        dkd = DualKeyDict({"10": "val"})
        with self.assertRaises(KeyError):
            _ = dkd["99"]
        self.assertIsNone(dkd.get(99))

    # 12. load_judge_prompt_template
    def test_load_judge_prompt_template_nominal(self) -> None:
        tpl = load_judge_prompt_template()
        self.assertIsInstance(tpl, str)
        self.assertIn("RUN_ID", tpl)
        self.assertIn("BATCH_ID", tpl)

    def test_load_judge_prompt_template_negative_missing_file(self) -> None:
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(JudgeValidationError) as cm:
                load_judge_prompt_template()
            self.assertIn("Missing judge prompt template", str(cm.exception))

    # 13. load_schema
    def test_load_schema_nominal(self) -> None:
        schema = load_schema("judge-input.schema.json")
        self.assertIsInstance(schema, dict)
        self.assertEqual(schema.get("$id"), "urn:vqa:judge-input:v1")

    def test_load_schema_negative_unknown_name(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_schema("unknown-non-existent-schema")

    # 14. _categorize_check
    def test_categorize_check_nominal(self) -> None:
        # q02: pause
        ch_q02 = {"id": "c1", "description": "Verify gameplay is paused when requested"}
        self.assertEqual(_categorize_check(ch_q02), "q02")

        # q03: card / levelup
        ch_q03 = {"id": "c2", "description": "Check levelup card overlay"}
        self.assertEqual(_categorize_check(ch_q03), "q03")

        # q04: camera / fleet / motion
        ch_q04 = {"id": "c3", "description": "Verify camera rotation motion"}
        self.assertEqual(_categorize_check(ch_q04), "q04")

        # q01: default
        ch_q01 = {"id": "c0", "description": "Standard HUD visual check"}
        self.assertEqual(_categorize_check(ch_q01), "q01")

    def test_categorize_check_negative_missing_id(self) -> None:
        ch_invalid = {"description": "Missing id check"}
        with self.assertRaises(KeyError):
            _categorize_check(ch_invalid)


if __name__ == "__main__":
    unittest.main()
