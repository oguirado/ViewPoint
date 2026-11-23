"""Plant fail-open bridge mutations, run frozen tests, restore exact source bytes.

Run explicitly under the unit's memory scope; never part of normal discovery.
No assertions in the test suite are changed. Each case is a separate process.
"""
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "gates" / "vqa3d_bridge.py"
ORIGINAL = SOURCE.read_bytes()
TEXT = ORIGINAL.decode("utf-8")
ANCHOR = '    profile = profile if type(profile) is dict else {}'

# A mutation promotes precisely the named refusal to PASS at the publication
# boundary. Thus it cannot merely trip some earlier malformed-input guard.
CASES = [
    ("coverage_incomplete", "test_omitted_check_refuses"),
    ("coverage_incomplete", "test_empty_coverage_refuses"),
    ("coverage_incomplete", "test_empty_receipts_refuse"),
    ("duplicate_check", "test_duplicate_checks_refuse"),
    ("evidence_incomplete", "test_skip_unclear_never_pass"),
    ("upstream_fail", "test_domain_fail_maps_to_stage_five"),
    ("upstream_error", "test_upstream_error_has_precedence"),
    ("summary_mismatch", "test_forged_summary_cannot_override_failed_check"),
    ("receipt_verdict_mismatch", "test_forged_child_cannot_override_oracle"),
    ("summary_mismatch", "test_summary_count_and_actions_are_recomputed"),
    ("evidence_incomplete", "test_advisory_and_verified_flag_do_not_promote"),
    ("coverage_incomplete", "test_advisory_and_verified_flag_do_not_promote"),
    ("upstream_fail", "test_surplus_advisory_failure_remains_visible"),
    ("profile_stale", "test_hash_source_and_profile_drift_refuse"),
    ("source_stale", "test_hash_source_and_profile_drift_refuse"),
    ("trace_sha256_stale", "test_internal_hashes_refuse_tampering"),
    ("receipt_sha256_stale", "test_internal_hashes_refuse_tampering"),
    ("receipt_sha256_stale", "test_hash_newline_convention"),
    ("profile_missing", "test_missing_bindings_cannot_see"),
    ("source_missing", "test_missing_bindings_cannot_see"),
    ("trace_sha256_missing", "test_missing_bindings_cannot_see"),
    ("receipt_sha256_missing", "test_missing_bindings_cannot_see"),
    ("unsupported_family", "test_unknown_schema_and_verdict_refuse"),
    ("unknown_verdict", "test_unknown_schema_and_verdict_refuse"),
    ("verdict_rc_mismatch", "test_rc_and_count_types_refuse"),
    ("expectation_invalid", "test_malformed_shape_and_expectations_refuse"),
    ("schema_mismatch", "test_malformed_shape_and_expectations_refuse"),
    ("coverage_invalid", "test_malformed_shape_and_expectations_refuse"),
    ("severity_invalid", "test_malformed_shape_and_expectations_refuse"),
    ("evidence_missing", "test_empty_oracle_evidence_refuses"),
    ("invalid_hash", "test_invalid_hashes_are_errors"),
    ("noncanonical_type", "test_global_float_refuses_but_boolean_metadata_passes"),
    ("unknown_verdict", "test_invalid_nested_verdict_and_shape_refuse"),
    ("schema_mismatch", "test_profile_identity_shape_refuses"),
    ("receipt_missing", "test_missing_receipt_cannot_see"),
    ("duplicate_key", "test_parser_rejects_invalid_bytes"),
    ("non_integer_number", "test_parser_rejects_invalid_bytes"),
    ("receipt_invalid", "test_parser_rejects_invalid_bytes"),
    ("receipt_empty", "test_parser_rejects_invalid_bytes"),
    ("receipt_budget_exceeded", "test_read_limit_and_nonregular_file"),
    ("not_regular_file", "test_read_limit_and_nonregular_file"),
    ("usage_error", "test_cli_isolated_and_usage_refuse"),
    ("upstream_fail", "test_canonical_output_and_input_bytes_preserved"),
]
EDITS = [
    ("newline-hash convention", "test_complete_pinned_trace_passes",
     'return hashlib.sha256(_canonical(value)).hexdigest()',
     'return hashlib.sha256(_canonical(value) + b"\\n").hexdigest()'),
    ("refusal overwrites caller bytes", "test_canonical_output_and_input_bytes_preserved",
     '        file_hash = hashlib.sha256(raw).hexdigest()',
     '        receipt_path.write_bytes(b"changed")\n        file_hash = hashlib.sha256(raw).hexdigest()'),
    ("noncanonical stdout", "test_canonical_output_and_input_bytes_preserved",
     '    return core.emit(result)',
     '    print(json.dumps(result))\n    return result["rc"]'),
    ("forbidden sibling import", "test_no_sibling_or_blender_imports",
     'MAX_RECEIPT_BYTES = 16 * 2**20',
     'if False:\n    import bpy\nMAX_RECEIPT_BYTES = 16 * 2**20'),
    ("wrong frozen size ceiling", "test_read_limit_and_nonregular_file",
     'MAX_RECEIPT_BYTES = 16 * 2**20', 'MAX_RECEIPT_BYTES = 32 * 2**20'),
]
BOOTSTRAP = ("import sys,unittest;sys.path.insert(0,sys.argv.pop(1));"
             "sys.path.insert(0,sys.argv.pop(1));unittest.main(module=None)")


def run_test(name):
    return subprocess.run([sys.executable, "-I", "-B", "-c", BOOTSTRAP,
                           str(ROOT), str(ROOT / "tests"), "test_f0u5_bridge.BridgeTests." + name],
                          cwd=ROOT, capture_output=True, timeout=30)


def main():
    if TEXT.count(ANCHOR) != 1:
        raise RuntimeError("nonunique mutation anchor")
    killed = 0
    try:
        for reason, test in CASES:
            mutation = f'    if {reason!r} in reasons:\n        rc = 0\n' + ANCHOR
            SOURCE.write_text(TEXT.replace(ANCHOR, mutation), encoding="utf-8")
            result = run_test(test)
            # Require a real assertion failure, rather than import/syntax failure.
            if result.returncode != 1 or b"FAIL:" not in result.stderr:
                print(result.stdout.decode(), result.stderr.decode())
                raise RuntimeError(f"mutant survived or did not hit an assertion: {reason}/{test}")
            killed += 1
            print(f"KILLED {reason}: {test}", flush=True)
        for label, test, before, after in EDITS:
            if TEXT.count(before) != 1:
                raise RuntimeError("nonunique mutation anchor: " + label)
            SOURCE.write_text(TEXT.replace(before, after), encoding="utf-8")
            result = run_test(test)
            if result.returncode != 1 or b"FAIL:" not in result.stderr:
                raise RuntimeError("mutant was not killed by an assertion: " + label)
            killed += 1
            print(f"KILLED {label}: {test}", flush=True)
    finally:
        SOURCE.write_bytes(ORIGINAL)
    if SOURCE.read_bytes() != ORIGINAL:
        raise RuntimeError("source restoration failed")
    print(f"MUTANTS_KILLED={killed}/{len(CASES) + len(EDITS)}")
    print("RESTORED_SHA256=" + hashlib.sha256(SOURCE.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
