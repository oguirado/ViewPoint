"""Resealed adversarial evidence must not acquire a bridge PASS."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_f0u5_bridge import EXPECT, PROFILE, ROOT, SOURCE, bridge, encoded, fixture, seal


def absent_observation():
    return {"obs_id": None, "frame_sha256": None, "state_sha256": None,
            "capture_available": False, "camera_source": "none", "camera_name": ""}


class ReviewRegressionTests(unittest.TestCase):
    def check_evidence(self, trace, rc, reason=None):
        # Seal both layers independently of the implementation under test.
        trace = seal(trace)
        raw = encoded(trace)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_bytes(raw)
            child = subprocess.run(
                [sys.executable, "-B", "-m", "gates.vqa3d_bridge",
                 "--receipt", str(path), "--source-sha256", SOURCE,
                 "--profile", PROFILE["id"], "--profile-sha256", PROFILE["sha256"],
                 "--required-check", "geometry", "--required-check", "uv"],
                cwd=ROOT, capture_output=True, timeout=10)
            results = {"evaluate_trace": bridge.evaluate_trace(trace, **EXPECT),
                       "read_receipt": bridge.read_receipt(path, **EXPECT),
                       "CLI": json.loads(child.stdout)}
            for entry, result in results.items():
                with self.subTest(entry=entry):
                    self.assertEqual(result["rc"], rc, result)
                    self.assertEqual(result["verdict"], "PASS" if rc == 0 else "ERROR")
                    self.assertEqual(result["reason_codes"], [] if reason is None else [reason])
            self.assertEqual(child.returncode, rc, child.stderr)
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(results["read_receipt"]["receipt_file_sha256"],
                             hashlib.sha256(raw).hexdigest())

    def test_pass_with_verified_false_refuses(self):
        trace = fixture()
        trace["receipts"][0]["verified"] = False
        self.check_evidence(trace, 2, "receipt_verified_mismatch")

    def test_pass_with_explicit_timeout_error_refuses(self):
        trace = fixture()
        trace["receipts"][0]["error"] = "TimeoutError: evaluator timed out"
        self.check_evidence(trace, 2, "receipt_error_mismatch")

    def test_after_frame_disagrees_refuses(self):
        trace = fixture()
        trace["receipts"][0]["after_frame"]["state_sha256"] = "e" * 64
        self.check_evidence(trace, 2, "after_frame_mismatch")

    def test_state_change_disagrees_refuses(self):
        trace = fixture()
        trace["receipts"][0]["state_sha256_change"] = True
        self.check_evidence(trace, 2, "state_change_mismatch")

    def test_state_comparison_disagrees_refuses(self):
        trace = fixture()
        trace["receipts"][0]["state_comparison_available"] = False
        self.check_evidence(trace, 2, "state_comparison_mismatch")

    def test_nonpass_verification_and_error_contradictions_refuse(self):
        from test_f0u5_bridge import change_verdict
        for verdict in ("FAIL", "ERROR", "CANNOT_SEE", "SKIP", "UNCLEAR"):
            trace = change_verdict(fixture(), verdict)
            # Construct coherent producer metadata before mutating one field.
            trace["receipts"][0].update(verified=False, error="synthetic only")
            for field, value, reason in (
                    ("verified", True, "receipt_verified_mismatch"),
                    ("error", None, "receipt_error_mismatch"),
                    ("error", "contradictory reason", "receipt_error_mismatch")):
                with self.subTest(verdict=verdict, field=field, value=value):
                    candidate = copy.deepcopy(trace)
                    candidate["receipts"][0][field] = value
                    self.check_evidence(candidate, 2, reason)

    def test_advisory_metadata_contradictions_refuse(self):
        from test_f0u5_bridge import change_verdict
        for field, value, reason in (
                ("verified", False, "receipt_verified_mismatch"),
                ("error", "unexpected error", "receipt_error_mismatch")):
            with self.subTest(field=field):
                trace = change_verdict(fixture(), "ADVISORY")
                trace["receipts"][0][field] = value
                self.check_evidence(trace, 2, reason)

    def test_changed_state_reported_unchanged_refuses(self):
        trace = fixture()
        row = trace["receipts"][0]
        row["after"]["state_sha256"] = "e" * 64
        row["after_frame"] = copy.deepcopy(row["after"])
        self.check_evidence(trace, 2, "state_change_mismatch")

    def test_absent_observation_claimed_comparable_refuses(self):
        trace = fixture()
        trace["receipts"][0]["before"] = absent_observation()
        self.check_evidence(trace, 2, "state_comparison_mismatch")

    def test_unavailable_comparison_claimed_changed_refuses(self):
        trace = fixture()
        row = trace["receipts"][0]
        row.update(before=absent_observation(), state_comparison_available=False,
                   state_sha256_change=True)
        self.check_evidence(trace, 2, "state_change_mismatch")

    def test_absent_observation_with_state_identity_refuses(self):
        trace = fixture()
        row = trace["receipts"][0]
        row.update(before=absent_observation(), state_comparison_available=False)
        row["before"]["state_sha256"] = "d" * 64
        self.check_evidence(trace, 2, "observation_reference_mismatch")

    def test_present_observation_without_state_identity_refuses(self):
        trace = fixture()
        trace["receipts"][0]["before"]["state_sha256"] = None
        self.check_evidence(trace, 2, "observation_reference_mismatch")

    def test_pixel_capture_is_optional_for_state_comparison(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                trace = fixture()
                for row in trace["receipts"]:
                    for key in ("before", "after"):
                        row[key].update(frame_sha256=None, capture_available=False,
                                        camera_source="none", camera_name="")
                    if changed:
                        row["after"]["state_sha256"] = "e" * 64
                    row["after_frame"] = copy.deepcopy(row["after"])
                    row["state_sha256_change"] = changed
                self.check_evidence(trace, 0)

    def test_producer_shaped_absent_observations_pass(self):
        for absent in (("before",), ("after",), ("before", "after")):
            with self.subTest(absent=absent):
                trace = fixture()
                for row in trace["receipts"]:
                    for key in absent:
                        row[key] = absent_observation()
                    row.update(after_frame=copy.deepcopy(row["after"]),
                               state_comparison_available=False, state_sha256_change=False)
                self.check_evidence(trace, 0)
