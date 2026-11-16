"""Synthetic producer-shaped traces qualify the reader, not any 3D asset."""
import contextlib
import copy
import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# unittest discovery also works under python3 -I without trusting PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gates import vqa3d_bridge as bridge

SOURCE = "a" * 64
PROFILE = {"id": "synthetic-intake", "sha256": "b" * 64}
EXPECT = {"expected_source_sha256": SOURCE, "expected_profile_id": PROFILE["id"],
          "expected_profile_sha256": PROFILE["sha256"], "required_check_ids": ("geometry", "uv")}
DOMAIN = {"PASS": 0, "ADVISORY": 0, "FAIL": 1, "ERROR": 2,
          "CANNOT_SEE": 3, "SKIP": 3, "UNCLEAR": 3}
RANK = {"PASS": 0, "ADVISORY": 1, "CANNOT_SEE": 2, "SKIP": 2,
        "UNCLEAR": 2, "FAIL": 3, "ERROR": 4}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def seal(trace, *, summary=True):
    for receipt in trace["receipts"]:
        receipt.pop("receipt_sha256", None)
        receipt["receipt_sha256"] = digest(receipt)
    if summary:
        gates = [r["final_verdict"] for r in trace["receipts"] if r["severity"] == "gate"]
        verdict = max(gates, key=RANK.get) if gates else "CANNOT_SEE"
        if verdict == "PASS" and any(r["final_verdict"] != "PASS" for r in trace["receipts"]
                                      if r["severity"] == "advisory"):
            verdict = "ADVISORY"
        trace["summary"] = {"schema": "vqa3d.profile-summary/v1", "profile": dict(PROFILE),
                            "checks": len(trace["receipts"]), "final_verdict": verdict,
                            "verdict_rc": DOMAIN[verdict]}
        actions = {r["check_id"]: r["suggested_action"] for r in trace["receipts"]
                   if r.get("suggested_action")}
        if actions:
            trace["summary"]["suggested_actions"] = actions
    trace.pop("trace_sha256", None)
    trace["trace_sha256"] = digest(trace)
    return trace


def fixture():
    observation = {"obs_id": "synthetic", "frame_sha256": "c" * 64,
                   "state_sha256": "d" * 64, "capture_available": True,
                   "camera_source": "scene", "camera_name": "camera"}
    rows = []
    for check_id in EXPECT["required_check_ids"]:
        verdicts = [{"oracle": "synthetic", "verdict": "PASS", "reason": "synthetic only"}]
        rows.append({"schema": "vqa3d.receipt/v1", "profile": dict(PROFILE),
                     "check_id": check_id, "severity": "gate", "why": "synthetic coverage",
                     "adapter": {"id": "fake", "version": "v1", "status": "available"},
                     "obligation": {"kind": "synthetic"}, "before": dict(observation),
                     "after": dict(observation), "after_frame": dict(observation),
                     "state_sha256_change": False, "state_comparison_available": True,
                     "verdicts": verdicts, "verification": {"method": "obligation", "detail": copy.deepcopy(verdicts)},
                     "final_verdict": "PASS", "verdict_rc": 0, "verified": True, "error": None})
    return seal({"schema": "vqa3d.profile-trace/v1", "profile": dict(PROFILE),
                 "provenance": {"source_sha256": SOURCE}, "receipts": rows,
                 "probe": {}, "scene_state": {}, "scenario": {}, "observations": [],
                 "events": [], "replay_runs": []})


def change_verdict(trace, verdict, index=0):
    row = trace["receipts"][index]
    row.update(final_verdict=verdict, verdict_rc=DOMAIN[verdict])
    row["verdicts"][0]["verdict"] = verdict
    row["verification"]["detail"] = copy.deepcopy(row["verdicts"])
    row["verified"] = verdict in ("PASS", "ADVISORY")
    row["error"] = None if row["verified"] else row["verdicts"][0]["reason"]
    return seal(trace)


class BridgeTests(unittest.TestCase):
    def check(self, trace, rc, **expect):
        result = bridge.evaluate_trace(trace, **(EXPECT | expect))
        self.assertEqual(result["rc"], rc, result)
        self.assertEqual(result["verdict"], {0: "PASS", 2: "ERROR", 3: "CANNOT_SEE",
                                            4: "CANNOT_SEE", 5: "FAIL", 6: "CANNOT_SEE"}[rc])
        self.assertEqual(result["schema"], "af.bridge-verdict/v1")
        self.assertEqual(set(result), {"schema", "rc", "verdict", "reason_codes", "source_sha256",
                                      "profile", "receipt_file_sha256", "trace_sha256", "failed_checks"})
        return result

    def test_complete_pinned_trace_passes(self):
        trace = fixture()
        result = self.check(trace, 0)
        self.assertEqual(result["source_sha256"], SOURCE)
        self.assertEqual(result["profile"], PROFILE)
        self.assertEqual(result["trace_sha256"], trace["trace_sha256"])
        self.assertIsNone(result["receipt_file_sha256"])
        self.assertEqual(result["failed_checks"], [])

    def test_omitted_check_refuses(self):
        trace = fixture()
        trace["receipts"].pop()
        result = self.check(seal(trace), 3)
        self.assertEqual(result["failed_checks"][-1]["check_id"], "uv")

    def test_empty_coverage_refuses(self):
        self.check(fixture(), 3, required_check_ids=())

    def test_empty_receipts_refuse(self):
        trace = fixture()
        trace["receipts"] = []
        self.check(seal(trace), 3)

    def test_duplicate_checks_refuse(self):
        trace = fixture()
        trace["receipts"].append(copy.deepcopy(trace["receipts"][0]))
        self.check(seal(trace), 2)
        self.check(fixture(), 2, required_check_ids=("geometry", "geometry"))

    def test_skip_unclear_never_pass(self):
        for verdict in ("SKIP", "UNCLEAR", "CANNOT_SEE"):
            with self.subTest(verdict=verdict):
                self.check(change_verdict(fixture(), verdict), 3)

    def test_domain_fail_maps_to_stage_five(self):
        result = self.check(change_verdict(fixture(), "FAIL"), 5)
        self.assertEqual(result["failed_checks"][0]["verdict_rc"], 1)

    def test_upstream_error_has_precedence(self):
        trace = change_verdict(fixture(), "FAIL")
        self.check(change_verdict(trace, "ERROR", 1), 2)

    def test_forged_summary_cannot_override_failed_check(self):
        trace = change_verdict(fixture(), "FAIL")
        trace["summary"].update(final_verdict="PASS", verdict_rc=0)
        result = self.check(seal(trace, summary=False), 2)
        self.assertIn("summary_mismatch", result["reason_codes"])

    def test_forged_child_cannot_override_oracle(self):
        trace = change_verdict(fixture(), "FAIL")
        trace["receipts"][0].update(final_verdict="PASS", verdict_rc=0)
        self.check(seal(trace), 2)

    def test_summary_count_and_actions_are_recomputed(self):
        trace = fixture()
        trace["receipts"][0]["suggested_action"] = {"op": "review"}
        self.check(seal(trace), 0)
        trace["summary"]["checks"] += 1
        self.check(seal(trace, summary=False), 2)
        trace["summary"]["checks"] -= 1
        trace["summary"]["suggested_actions"] = {}
        self.check(seal(trace, summary=False), 2)

    def test_advisory_and_verified_flag_do_not_promote(self):
        trace = change_verdict(fixture(), "ADVISORY")
        self.assertIs(trace["receipts"][0]["verified"], True)
        self.check(trace, 3)
        trace = fixture()
        trace["receipts"][0]["severity"] = "advisory"
        self.check(seal(trace), 3)

    def test_surplus_advisory_failure_remains_visible(self):
        trace = fixture()
        extra = copy.deepcopy(trace["receipts"][0])
        extra.update(check_id="extra", severity="advisory")
        trace["receipts"].append(extra)
        for verdict, rc in (("PASS", 0), ("ADVISORY", 3), ("FAIL", 5), ("ERROR", 2)):
            self.check(change_verdict(trace, verdict, 2), rc)

    def test_hash_source_and_profile_drift_refuse(self):
        for location in ("trace", "summary", "receipt"):
            trace = fixture()
            target = {"trace": trace, "summary": trace["summary"], "receipt": trace["receipts"][0]}[location]
            target["profile"]["sha256"] = "e" * 64
            self.check(seal(trace, summary=False), 4)
        trace = fixture()
        trace["provenance"]["source_sha256"] = "e" * 64
        self.check(seal(trace), 4)
        self.check(fixture(), 4, expected_profile_id="wrong-profile")

    def test_internal_hashes_refuse_tampering(self):
        trace = fixture()
        trace["probe"]["changed"] = True
        self.check(trace, 4)
        trace = fixture()
        trace["receipts"][0]["why"] = "tampered"
        trace.pop("trace_sha256")
        trace["trace_sha256"] = digest(trace)
        self.check(trace, 4)

    def test_hash_newline_convention(self):
        trace = fixture()
        row = trace["receipts"][0]
        payload = {k: v for k, v in row.items() if k != "receipt_sha256"}
        row["receipt_sha256"] = hashlib.sha256(encoded(payload) + b"\n").hexdigest()
        trace.pop("trace_sha256")
        trace["trace_sha256"] = digest(trace)
        self.check(trace, 4)
        self.assertEqual(bridge._digest({"unicode": "é"}), digest({"unicode": "é"}))

    def test_missing_bindings_cannot_see(self):
        for target, key in (("trace", "profile"), ("trace", "trace_sha256"),
                            ("receipt", "receipt_sha256"), ("summary", "profile")):
            trace = fixture()
            row = {"trace": trace, "receipt": trace["receipts"][0], "summary": trace["summary"]}[target]
            row.pop(key)
            self.check(trace, 3)
        trace = fixture()
        trace["provenance"]["source_sha256"] = None
        self.check(seal(trace), 3)

    def test_invalid_hashes_are_errors(self):
        trace = fixture()
        trace["provenance"]["source_sha256"] = 17
        result = self.check(seal(trace), 2)
        self.assertIsNone(result["source_sha256"])
        trace = fixture()
        trace["trace_sha256"] = "BAD"
        result = self.check(trace, 2)
        self.assertIsNone(result["trace_sha256"])

    def test_global_float_refuses_but_boolean_metadata_passes(self):
        trace = fixture()
        trace["probe"]["value"] = True
        self.check(seal(trace), 0)
        trace["probe"]["value"] = 0.25
        self.check(seal(trace), 2)

    def test_invalid_nested_verdict_and_shape_refuse(self):
        trace = fixture()
        trace["receipts"][0]["verdicts"][0]["verdict"] = "UNRECOGNISED"
        trace["receipts"][0]["verification"]["detail"] = copy.deepcopy(trace["receipts"][0]["verdicts"])
        self.check(seal(trace), 2)
        trace = fixture()
        trace["receipts"][0]["wall_ms"] = True
        self.check(seal(trace), 2)

    def test_profile_identity_shape_refuses(self):
        for profile in (None, [], {"id": PROFILE["id"]}, PROFILE | {"extra": True}):
            trace = fixture()
            trace["profile"] = profile
            self.check(seal(trace), 3 if profile is None else 2)

    def test_unknown_schema_and_verdict_refuse(self):
        for target in ("trace", "summary", "receipt"):
            for schema in ("vqa.receipt/v1", "bvqa.asset-receipt/v1", "vqa3d.profile-trace/v1-extra"):
                trace = fixture()
                row = {"trace": trace, "summary": trace["summary"], "receipt": trace["receipts"][0]}[target]
                row["schema"] = schema
                self.check(seal(trace, summary=False), 2)
        trace = fixture()
        trace["receipts"][0]["final_verdict"] = "MAYBE"
        self.check(seal(trace, summary=False), 2)

    def test_rc_and_count_types_refuse(self):
        for value in (True, False, 0.0, 7):
            for target in ("summary", "receipt"):
                trace = fixture()
                row = trace["summary"] if target == "summary" else trace["receipts"][0]
                row["verdict_rc"] = value
                self.check(seal(trace, summary=False), 2)
        trace = fixture()
        trace["summary"]["checks"] = True
        self.check(seal(trace, summary=False), 2)

    def test_malformed_shape_and_expectations_refuse(self):
        for value in (None, [], True, "trace"):
            self.check(value, 2)
        for key, value in (("expected_source_sha256", "bad"), ("expected_profile_id", ""),
                           ("expected_profile_sha256", None), ("required_check_ids", ["geometry"])):
            self.check(fixture(), 2, **{key: value})
        for key in ("adapter", "before", "after", "after_frame", "verified", "verification", "why"):
            trace = fixture()
            trace["receipts"][0].pop(key)
            self.check(seal(trace), 2)
        trace = fixture()
        trace["receipts"][0]["severity"] = "optional"
        self.check(seal(trace), 2)

    def test_empty_oracle_evidence_refuses(self):
        trace = fixture()
        trace["receipts"][0]["verdicts"] = []
        trace["receipts"][0]["verification"]["detail"] = []
        self.check(seal(trace), 3)

    def read(self, path):
        return bridge.read_receipt(path, **EXPECT)

    def test_missing_receipt_cannot_see(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            self.assertEqual(self.read(path)["rc"], 3)
            path.write_bytes(encoded(fixture()))
            self.assertEqual(self.read(path)["rc"], 0)

    def test_parser_rejects_invalid_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            for raw in (b"{", b"\xff", b'{"x":1,"x":2}', b'{"x":0.5}', b'{"x":NaN}',
                        b'{"x":Infinity}', b"[" * 2000 + b"]" * 2000):
                path.write_bytes(raw)
                result = self.read(path)
                self.assertEqual(result["rc"], 2, result)
                self.assertEqual(result["receipt_file_sha256"], hashlib.sha256(raw).hexdigest())
            for raw in (b"", b" \n"):
                path.write_bytes(raw)
                self.assertEqual(self.read(path)["rc"], 3)

    def test_read_limit_and_nonregular_file(self):
        self.assertEqual(bridge.MAX_RECEIPT_BYTES, 16 * 2**20)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            raw = encoded(fixture())
            path.write_bytes(raw)
            with mock.patch.object(bridge, "MAX_RECEIPT_BYTES", len(raw)):
                self.assertEqual(self.read(path)["rc"], 0)
            with mock.patch.object(bridge, "MAX_RECEIPT_BYTES", len(raw) - 1):
                result = self.read(path)
                self.assertEqual(result["rc"], 6)
                self.assertIsNone(result["receipt_file_sha256"])
            self.assertEqual(self.read(Path(directory))["rc"], 2)
            import os
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            self.assertEqual(self.read(fifo)["rc"], 2)

    def test_canonical_output_and_input_bytes_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            for trace in (fixture(), change_verdict(fixture(), "FAIL")):
                raw = json.dumps(trace, indent=2).encode()
                path.write_bytes(raw)
                first, second = self.read(path), self.read(path)
                self.assertEqual(first["rc"], 0 if trace["summary"]["final_verdict"] == "PASS" else 5)
                self.assertEqual(first, second)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(first["receipt_file_sha256"], hashlib.sha256(raw).hexdigest())
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    rc = bridge.main(["--receipt", str(path), "--source-sha256", SOURCE,
                                      "--profile", PROFILE["id"], "--profile-sha256", PROFILE["sha256"],
                                      "--required-check", "geometry", "--required-check", "uv"])
                self.assertEqual(rc, first["rc"])
                self.assertEqual(output.getvalue().encode(), encoded(first) + b"\n")
                self.assertEqual(path.read_bytes(), raw)

    def test_cli_isolated_and_usage_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_bytes(encoded(fixture()))
            bootstrap = "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('gates.vqa3d_bridge',run_name='__main__')"
            argv = [sys.executable, "-I", "-B", "-c", bootstrap, str(ROOT)]
            good = ["--receipt", str(path), "--source-sha256", SOURCE, "--profile", PROFILE["id"],
                    "--profile-sha256", PROFILE["sha256"], "--required-check", "geometry", "--required-check", "uv"]
            for args, rc in ((good, 0), (good[:-4], 3), ([], 2), (["--bad"], 2)):
                result = subprocess.run(argv + args, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, rc, result.stderr)
                self.assertEqual(json.loads(result.stdout)["rc"], rc)
                self.assertEqual(result.stderr, b"")

    def test_no_sibling_or_blender_imports(self):
        import ast
        tree = ast.parse(Path(bridge.__file__).read_text())
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        self.assertFalse(set(imports) & {"bpy", "vqa3d", "blender_visual_qa", "bmesh", "mathutils", "gpu"})


if __name__ == "__main__":
    unittest.main()
