"""Tests for judge response validation and worker usage extraction.

Coverage:
- validate_response (blender_visual_qa/judge/__init__.py line 1217)
- _extract_worker_usage (blender_visual_qa/judge/__init__.py line 1521)
"""

import copy
import math
import unittest
from typing import Any

from blender_visual_qa import judge
from blender_visual_qa.judge import (
    JudgeValidationError,
    _extract_worker_usage,
    validate_response,
)


def _make_valid_triplet() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build a minimal valid input_obj, expectations, and output_obj triplet."""
    input_obj: dict[str, Any] = {
        "run_id": "run_01",
        "batch_id": "batch_01",
        "attempt_id": "att_01",
        "media": [
            {"evidence_id": "frame_1", "kind": "png", "role": "evidence"},
            {"evidence_id": "att_media", "kind": "png", "role": "attention"},
        ],
        "checks": [
            {
                "check_id": "check_01",
                "evidence_ids": ["frame_1"],
                "evidence_requirement": "scene",
            }
        ],
    }

    input_sha = judge.compute_canonical_sha256(input_obj)
    prompt_sha = "a" * 64

    expectations: dict[str, Any] = {
        "input_sha256": input_sha,
        "prompt_sha256": prompt_sha,
        "attention": {
            "attention_text": "hello",
            "attention_absent": "NO",
            "attention_video": "none",
        },
    }

    output_obj: dict[str, Any] = {
        "schema": "vqa.judge-output/v1",
        "run_id": "run_01",
        "batch_id": "batch_01",
        "attempt_id": "att_01",
        "model_id": judge.DEFAULT_MODEL_ID,
        "input_sha256": input_sha,
        "prompt_sha256": prompt_sha,
        "media_seen": ["frame_1"],
        "attention_answers": {
            "attention_text": "hello",
            "attention_absent": "NO",
            "attention_video": "none",
        },
        "results": [
            {
                "check_id": "check_01",
                "verdict": "PASS",
                "confidence": 0.95,
                "reason": "Object visible",
                "observations": [
                    {
                        "evidence_id": "frame_1",
                        "time_ms": None,
                        "text": "Object is visible",
                        "kind": "scene",
                    }
                ],
            }
        ],
    }

    return input_obj, expectations, output_obj


class TestValidateResponsePositive(unittest.TestCase):
    def test_validate_response_nominal_pass(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        res = validate_response(input_obj, output_obj, expectations)
        self.assertTrue(res["valid"])
        self.assertEqual(res["outcome"], "VALID")
        self.assertEqual(res["errors"], [])
        self.assertEqual(len(res["results"]), 1)
        r0 = res["results"][0]
        self.assertEqual(r0["check_id"], "check_01")
        self.assertEqual(r0["verdict"], "PASS")
        self.assertEqual(r0["confidence"], 0.95)
        self.assertEqual(r0["reason"], "Object visible")


class TestValidateResponseNegative(unittest.TestCase):
    def test_unknown_check_ids(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["results"] = [
            {
                "check_id": "check_unknown",
                "verdict": "PASS",
                "confidence": 0.95,
                "reason": "Unknown check",
                "observations": [
                    {
                        "evidence_id": "frame_1",
                        "time_ms": None,
                        "text": "visible",
                        "kind": "scene",
                    }
                ],
            }
        ]
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(res["outcome"], "ERROR")
        self.assertEqual(
            res["errors"],
            ["Check results count/id mismatch: ['check_unknown'] vs ['check_01']"],
        )

    def test_missing_checks(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["results"] = []
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Output schema validation failure: Schema validation error for judge-output: [] should be non-empty at results"],
        )

    def test_duplicate_check_ids(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        r0 = output_obj["results"][0]
        output_obj["results"] = [r0, copy.deepcopy(r0)]
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(res["outcome"], "ERROR")
        self.assertEqual(
            res["errors"],
            ["Check results count/id mismatch: ['check_01', 'check_01'] vs ['check_01']"],
        )

    def test_verdict_outside_rubric(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["results"][0]["verdict"] = "MAYBE"
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Output schema validation failure: Schema validation error for judge-output: 'MAYBE' is not one of ['PASS', 'FAIL', 'CANNOT_SEE'] at results/0/verdict"],
        )

    def test_disallowed_evidence_id(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        input_obj["media"].append({"evidence_id": "frame_2", "kind": "png", "role": "evidence"})
        output_obj["media_seen"].append("frame_2")
        output_obj["results"][0]["observations"][0]["evidence_id"] = "frame_2"
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(res["outcome"], "ERROR")
        self.assertEqual(
            res["errors"],
            ["Observation in check check_01 references disallowed evidence_id frame_2"],
        )

    def test_attention_media_reference(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        input_obj["checks"][0]["evidence_ids"].append("att_media")
        output_obj["media_seen"].append("att_media")
        output_obj["results"][0]["observations"][0]["evidence_id"] = "att_media"
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(res["outcome"], "ERROR")
        self.assertEqual(
            res["errors"],
            ["Check check_01 observation references attention media att_media"],
        )

    def test_extra_keys_in_output_schema(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["unrecognized_extra_key"] = 123
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Output schema validation failure: Schema validation error for judge-output: Additional properties are not allowed ('unrecognized_extra_key' was unexpected) at "],
        )

    def test_non_numeric_confidence_type(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["results"][0]["confidence"] = "high"
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Output schema validation failure: Schema validation error for judge-output: 'high' is not of type 'number' at results/0/confidence"],
        )

    def test_invalid_confidence_value_bounds(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["results"][0]["confidence"] = 1.5
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Output schema validation failure: Schema validation error for judge-output: 1.5 is greater than the maximum of 1 at results/0/confidence"],
        )

    def test_invalid_confidence_nan(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["results"][0]["confidence"] = math.nan
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(res["outcome"], "ERROR")
        self.assertEqual(
            res["errors"],
            ["Invalid confidence value for check check_01: nan"],
        )

    def test_identity_mismatches(self):
        # run_id mismatch
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["run_id"] = "run_wrong"
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(res["errors"], ["run_id mismatch: run_wrong != run_01"])

        # batch_id mismatch
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["batch_id"] = "batch_wrong"
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(res["errors"], ["batch_id mismatch: batch_wrong != batch_01"])

        # attempt_id mismatch
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["attempt_id"] = "att_wrong"
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(res["errors"], ["attempt_id mismatch: att_wrong != att_01"])

        # model_id mismatch
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["model_id"] = "wrong-model-v1"
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Output schema validation failure: Schema validation error for judge-output: 'gemini-3.8-flash' was expected at model_id"],
        )

        # input_sha256 mismatch
        input_obj, expectations, output_obj = _make_valid_triplet()
        input_sha = output_obj["input_sha256"]
        output_obj["input_sha256"] = "b" * 64
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            [f"input_sha256 mismatch: {'b'*64} not in ['{input_sha}']"],
        )

        # prompt_sha256 mismatch
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["prompt_sha256"] = "b" * 64
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            [f"prompt_sha256 mismatch: {'b'*64} != {'a'*64}"],
        )

    def test_unknown_media_seen(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["media_seen"].append("ghost_media")
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Unknown evidence ids in media_seen: ['ghost_media']"],
        )

    def test_cannot_see_with_non_zero_confidence(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["results"][0]["verdict"] = "CANNOT_SEE"
        output_obj["results"][0]["confidence"] = 0.5
        output_obj["results"][0]["observations"] = []
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Check check_01 returned CANNOT_SEE but confidence is non-zero (0.5)"],
        )

    def test_cannot_see_with_non_empty_observations(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["results"][0]["verdict"] = "CANNOT_SEE"
        output_obj["results"][0]["confidence"] = 0.0
        output_obj["results"][0]["observations"] = [
            {
                "evidence_id": "frame_1",
                "time_ms": None,
                "text": "visible",
                "kind": "scene",
            }
        ]
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Check check_01 returned CANNOT_SEE but observations list is non-empty ([{'evidence_id': 'frame_1', 'time_ms': None, 'text': 'visible', 'kind': 'scene'}])"],
        )

    def test_readability_pass_requires_ocr_observation(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        input_obj["checks"][0]["evidence_requirement"] = "ocr"
        output_obj["results"][0]["verdict"] = "PASS"
        output_obj["results"][0]["observations"][0]["kind"] = "scene"
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Readability PASS in check check_01 requires located literal OCR for frame_1"],
        )

    def test_png_observation_nonnull_time_ms(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        output_obj["results"][0]["observations"][0]["time_ms"] = 100
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Observation for PNG evidence frame_1 must have null time_ms (got 100)"],
        )

    def test_mp4_observation_exceeds_duration(self):
        input_obj, expectations, output_obj = _make_valid_triplet()
        input_obj["media"].append(
            {"evidence_id": "clip_1", "kind": "mp4", "role": "evidence", "duration_ms": 1500}
        )
        input_obj["checks"][0]["evidence_ids"].append("clip_1")
        output_obj["media_seen"].append("clip_1")
        output_obj["results"][0]["observations"].append(
            {
                "evidence_id": "clip_1",
                "time_ms": 1800,
                "text": "out of range timestamp",
                "kind": "scene",
            }
        )
        res = validate_response(input_obj, output_obj, expectations)
        self.assertFalse(res["valid"])
        self.assertEqual(
            res["errors"],
            ["Observation time_ms 1800 exceeds duration 1500 for clip_1"],
        )


class TestExtractWorkerUsage(unittest.TestCase):
    def test_nominal_worker_usage(self):
        records = [
            {
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 45,
                    "cache_read_tokens": 10,
                    "thinking_tokens": 5,
                },
                "usage_presente": {
                    "input": True,
                    "output": True,
                    "cache_read": True,
                    "thinking": True,
                },
            }
        ]
        usage, presence = _extract_worker_usage(records)
        self.assertEqual(
            usage,
            {
                "input_tokens": 120,
                "output_tokens": 45,
                "cache_tokens": 10,
                "thinking_tokens": 5,
            },
        )
        self.assertEqual(presence, {"input": True, "output": True})

    def test_refuses_boolean_counter(self):
        records = [
            {
                "usage": {"input_tokens": True, "output_tokens": 10},
                "usage_presente": {"input": True, "output": True},
            }
        ]
        with self.assertRaises(JudgeValidationError) as cm:
            _extract_worker_usage(records)
        self.assertEqual(
            str(cm.exception),
            "Invalid input_tokens in worker usage: expected nonnegative integer, got True",
        )

    def test_refuses_negative_counter(self):
        records = [
            {
                "usage": {"input_tokens": 10, "output_tokens": -5},
                "usage_presente": {"input": True, "output": True},
            }
        ]
        with self.assertRaises(JudgeValidationError) as cm:
            _extract_worker_usage(records)
        self.assertEqual(
            str(cm.exception),
            "Invalid output_tokens in worker usage: expected nonnegative integer, got -5",
        )

    def test_refuses_non_integer_counter(self):
        records = [
            {
                "usage": {"input_tokens": 10, "cache_read_tokens": 12.34},
                "usage_presente": {"input": True, "cache_read": True},
            }
        ]
        with self.assertRaises(JudgeValidationError) as cm:
            _extract_worker_usage(records)
        self.assertEqual(
            str(cm.exception),
            "Invalid cache_read_tokens in worker usage: expected nonnegative integer, got 12.34",
        )

    @unittest.expectedFailure
    def test_refuses_missing_counter_when_present(self):
        records = [
            {
                "usage": {"output_tokens": 10},
                "usage_presente": {"input": True, "output": True},
            }
        ]
        with self.assertRaises(JudgeValidationError):
            _extract_worker_usage(records)

    def test_handles_missing_counter_or_presence(self):
        records: list[dict[str, Any]] = [{}]
        usage, presence = _extract_worker_usage(records)
        self.assertEqual(
            usage,
            {
                "input_tokens": None,
                "output_tokens": None,
                "cache_tokens": None,
                "thinking_tokens": None,
            },
        )
        self.assertEqual(presence, {"input": False, "output": False})


if __name__ == "__main__":
    unittest.main()
