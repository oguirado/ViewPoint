"""Temporarily break cache rules in isolated subprocess memory; never edit code.

Each child compiles an anchored source mutation, runs its discriminating test,
and requires an assertion failure. Import, syntax, or runtime errors are not kills.
The caller supplies the same memory scope as the ordinary unit suite.
"""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
TEST_PATH = ROOT / "tests/test_f0u4_cache.py"
spec = importlib.util.spec_from_file_location("f0u4_tests", TEST_PATH)
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
cache = tests.cache


def cases():
    values = []
    for field in ("slot", "inputs", "provider_id", "provider_revision", "provider_sha256", "params", "toolchain_sha256", "policy_sha256"):
        values.append({"id": "identity_omit_" + field, "test": "test_key_binds_full_identity", "changes": [[
            "return core.sha256_json(request)", "return core.sha256_json({k: v for k, v in request.items() if k != " + repr(field) + "})"]]})
    def add(name, test, old, new):
        values.append({"id": name, "test": test, "changes": [[old, new]]})
    add("identity_truncate_digest", "test_key_binds_full_identity", "return core.sha256_json(request)",
        'return core.sha256_json(dict(request, inputs=[dict(v, sha256=v["sha256"][:8]) if "sha256" in v else v for v in request["inputs"]]))')
    add("identity_sort_input_list", "test_key_binds_full_identity", "return core.sha256_json(request)",
        'return core.sha256_json(dict(request, inputs=sorted(request["inputs"], key=core.canonical_json)))')
    add("identity_unsorted_json", "test_key_canonical_mapping_order", "return core.sha256_json(request)",
        'return hashlib.sha256(json.dumps(request).encode()).hexdigest()')
    add("request_open_schema", "test_request_validation", '_validate(request, "cache-request")', 'pass')
    add("short_digest_allowed", "test_request_validation", '_require(isinstance(value, str) and _HASH.fullmatch(value) is not None, "invalid_digest")', 'pass')
    add("stored_fail_ignored", "test_reject_then_repeat_stays_rejected", 'if manifest["verdict"] == "FAIL":', 'if False:')
    add("conflict_accepted", "test_conflicting_publish_and_path_escape_refused", 'if core.canonical_json(existing) != core.canonical_json(manifest):', 'if False:')
    add("required_coverage_ignored", "test_missing_or_unclear_evidence_cannot_pass", 'if not required_gates or any(gate not in gates for gate in required_gates):', 'if False:')
    add("artifact_hash_ignored", "test_tampered_artifact_or_receipt_is_stale", 'if digest != artifact["sha256"]:', 'if False:')
    add("evidence_hash_ignored", "test_tampered_artifact_or_receipt_is_stale", 'if digest != ref["sha256"]:', 'if False:')
    add("subject_not_bound", "test_manifest_and_gate_bindings", '\n    if ref["subject_sha256"] not in artifact_hashes:', '\n    if False:')
    add("receipt_source_ignored", "test_pass_roundtrip_requires_bound_receipts", 'if payload["source_sha256"] != ref["subject_sha256"]:', 'if False:')
    add("unknown_family_accepted", "test_bridge_cannot_see_and_unknown_evidence", 'if payload["schema"] != "af.bridge-verdict/v1":', 'if False:')
    add("bridge_failure_ignored", "test_schema_and_evidence_negatives", '_require(rc != 0 or not payload["failed_checks"], "evidence_invalid")', 'pass')
    add("foreign_rc_one_leaked", "test_report_fail_maps_to_stage_five", 'return 5, "upstream_fail"', 'return 1, "upstream_fail"')
    add("report_pass_unverified", "test_report_pass_requires_bridge", '(3, "bridge_required")', '(0, None)')
    add("licence_not_rechecked", "test_policy_rechecked_on_lookup", '_decision(decision, request)\n', 'pass\n')
    add("malformed_decision_accepted", "test_decision_malformed_is_error", '_decision(decision, request)\n', 'pass\n')
    add("duplicate_keys_accepted", "test_duplicate_json_keys_and_float_rejected", '_require(key not in out, "duplicate_key")', 'pass')
    add("caller_asset_overwritten", "test_pass_roundtrip_requires_bound_receipts", 'if digest != expected:', 'source.write_bytes(b"mutated caller bytes")\n    if digest != expected:')
    add("staging_visible_before_flip", "test_staging_manifest_is_last", 'staging = Path(tempfile.mkdtemp(prefix=".stage-" + key + "-", dir=entry.parent))', 'staging = entry\n        staging.mkdir()')
    # Publication now has two independent visibility guards. Break both to
    # model the original rule violation; retaining either correctly prevents it.
    values[-1]["changes"].append(['return os.path.lexists(entry.parent / (".publish-" + entry.name + ".lock"))', 'return False'])
    add("second_publisher_false_success", "test_concurrent_publishers_and_lease", 'raise _Refusal(2, "publication_locked") from None', 'return _result(0, key)')
    add("budget_checks_removed", "test_resource_bounds_are_not_pass", 'if self.bytes > MAX_TOTAL_BYTES or time.monotonic() >= self.deadline:', 'if False:')
    add("traversal_allowed", "test_conflicting_publish_and_path_escape_refused", '_require(not p.is_absolute() and ".." not in p.parts and str(p) == relative, "path_escape")', 'pass')
    add("symlink_escape_allowed", "test_escaping_symlink_refused", '_require(candidate.resolve().is_relative_to(root.resolve()), "path_escape")', 'pass')
    values[-1]["changes"].append(['with _source_file(path) as source:', 'with open(path, "rb") as source:'])
    add("manifest_drift_allowed", "test_manifest_and_gate_bindings", 'if manifest["request_key"] != key or core.canonical_json(manifest["request"]) != core.canonical_json(request):', 'if False:')
    add("miss_promoted_to_pass", "test_missing_entry_is_not_acceptance", 'if manifest is None:\n            budget.check()\n            return _result(0, key, miss=True)', 'if manifest is None:\n            budget.check()\n            return _result(0, key)')
    add("crashed_publish_claims_success", "test_crash_before_manifest_not_visible", 'except _ERRORS as exc:\n        return _failure(exc, key, manifest_hash)\n    finally:', 'except _ERRORS as exc:\n        return _result(0, key)\n    finally:')
    add("orphan_entry_claims_success", "test_incomplete_entry_never_visible_or_overwritten", 'if entry.exists():\n            raise _Refusal(2, "incomplete_entry")', 'if entry.exists():\n            return _result(0, key)')
    add("derivative_inherits_receipt", "test_derivative_preserves_raw_identity", 'if payload["source_sha256"] != ref["subject_sha256"]:', 'if False:')
    add("null_source_becomes_stale", "test_null_source_and_specific_bridge_codes", 'if rc != 0:\n        return rc, "upstream_" + verdict.lower()', 'if rc != 0 and payload["source_sha256"] is not None:\n        return rc, "upstream_" + verdict.lower()')
    add("foreign_error_downgraded", "test_foreign_error_and_gate_error_dominate_fail", 'return 2, "upstream_error"', 'return 3, "bridge_required"')
    add("gate_error_downgraded", "test_foreign_error_and_gate_error_dominate_fail", 'elif ref["verdict"] == "ERROR":', 'elif False:')
    add("bool_integer_request_conflated", "test_embedded_request_bool_integer_drift", 'core.canonical_json(manifest["request"]) != core.canonical_json(request)', 'manifest["request"] != request')
    add("cleanup_failure_hidden", "test_refusal_io_and_cleanup_errors", 'if cleanup_failed:', 'if False:')
    add("duplicate_required_names_allowed", "test_file_type_and_duplicate_gate_names", '_require(len(set(required_gates)) == len(required_gates), "duplicate_gate")', 'pass')
    add("json_budget_ignored", "test_resource_bounds_are_not_pass", 'if length > MAX_JSON_BYTES:', 'if False:')
    add("float_text_coerced_to_integer", "test_duplicate_json_keys_and_float_rejected", 'parse_float=reject', 'parse_float=lambda value: int(float(value))')
    add("nan_text_coerced_to_zero", "test_duplicate_json_keys_and_float_rejected", 'parse_constant=reject', 'parse_constant=lambda value: 0')
    add("manifest_schema_ignored", "test_schema_and_evidence_negatives", '_validate(manifest, "manifest")', 'pass')
    add("copy_drift_claims_success", "test_payload_copy_drift_and_io_preserve_source", 'except _ERRORS as exc:\n        return _failure(exc, key, manifest_hash)\n    finally:', 'except _ERRORS as exc:\n        return _result(0, key)\n    finally:')
    return values


def child(case):
    source = Path(cache.__file__).read_text(encoding="utf-8")
    for old, new in case["changes"]:
        if source.count(old) != 1:
            return {"id": case["id"], "verdict": "CANNOT_SEE", "reason": "nonunique_anchor"}
        source = source.replace(old, new)
    exec(compile(source, cache.__file__, "exec"), cache.__dict__)
    stream = io.StringIO()
    suite = unittest.TestSuite([tests.CacheTests(case["test"])])
    result = unittest.TextTestRunner(stream=stream).run(suite)
    killed = bool(result.failures) and not result.errors and result.testsRun > 0
    return {"id": case["id"], "test": case["test"], "verdict": "KILLED" if killed else "CANNOT_SEE" if result.errors else "SURVIVED",
            "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "output_tail": stream.getvalue().splitlines()[-6:]}


if __name__ == "__main__":
    selected = cases()
    if len(sys.argv) == 3 and sys.argv[1] == "--case":
        print(json.dumps(child(next(c for c in selected if c["id"] == sys.argv[2])), sort_keys=True))
    else:
        results = []
        for case in selected:
            run = subprocess.run([sys.executable, "-I", "-B", __file__, "--case", case["id"]],
                capture_output=True, text=True, timeout=15, cwd=ROOT)
            if run.returncode != 0:
                results.append({"id": case["id"], "verdict": "CANNOT_SEE", "child_rc": run.returncode, "stderr_tail": run.stderr.splitlines()[-4:]})
            else:
                results.append(json.loads(run.stdout))
        ok = bool(results) and all(r["verdict"] == "KILLED" for r in results)
        print(json.dumps({"rc": 0 if ok else 5, "mutants": len(results), "killed": sum(r["verdict"] == "KILLED" for r in results), "results": results}, sort_keys=True))
        raise SystemExit(0 if ok else 5)
