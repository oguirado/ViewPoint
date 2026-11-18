"""Sequential mutation proof: mutate a disposable component copy only.

Run under the same bounded memory scope as unittest. No assertion is edited.
Every mutation must cause an assertion failure in its named test, not an import
or syntax error. Results are data written to the caller's explicit report path.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[3]
SCHEDULER = ROOT / "tools/scheduler/scheduler.py"


def case(name, test, *changes, file=SCHEDULER):
    return name, "test_f0u3_scheduler." + test, file, changes


CASES = [
    case("decimal_gib", "AdmissionTests.test_build_request_gib_boundary_and_identity",
         ("ceiling * 2**30", "ceiling * 10**9")),
    case("ignore_authorization", "AdmissionTests.test_identity_evidence_required_and_licence_denial_is_metadata",
         ('def _binding(request):\n', 'def _binding(request):\n    return\n')),
    case("ignore_stage_hash", "AdmissionTests.test_revision_policy_receipt_binding",
         ('if core.sha256_json(stage) != request["stage_request_sha256"]:', 'if False:'),
         ('if any(request[key] != expected[key] for key in expected):',
          'if any(request[key] != expected[key] for key in expected if key != "stage_request_sha256"):')),
    case("ignore_provider_hash", "AdmissionTests.test_revision_policy_receipt_binding",
         ('if core.sha256_json(provider) != request["provider_sha256"]:', 'if False:')),
    case("ignore_decision_hash", "LifecycleTests.test_stale_decision_never_starts",
         ('if core.sha256_json(decision) != request["decision_sha256"]:', 'if False:')),
    case("ignore_policy_config_binding", "AdmissionTests.test_revision_policy_receipt_binding",
         ('if decision.get(key) != expected:', 'if False:')),
    case("ignore_request_rebuild", "AdmissionTests.test_revision_policy_receipt_binding",
         ('if any(request[key] != expected[key] for key in expected):', 'if False:')),
    case("null_peak_becomes_zero", "AdmissionTests.test_missing_peak_or_memavailable_cannot_see",
         ('    memory = request["memory"]\n    missing = []',
          '    memory = dict(request["memory"])\n    if memory["measured_peak_bytes"] is None:\n        memory["measured_peak_bytes"] = 0\n    missing = []'),
         ('if memory["measured_peak_bytes"] is None or memory["measured_peak_bytes"] == 0:',
          'if memory["measured_peak_bytes"] is None:'),
         ('    if peak is None:\n        raise _Refusal(3, "peak_unmeasured")', '    if peak is None:\n        peak = 0')),
    case("max_instead_of_min_headroom", "AdmissionTests.test_admission_boundary_bytes",
         ('headroom = min(observation["host_mem_available_bytes"], observation["cgroup_headroom_bytes"])',
          'headroom = max(observation["host_mem_available_bytes"], observation["cgroup_headroom_bytes"])')),
    case("off_by_one_headroom", "AdmissionTests.test_admission_boundary_bytes",
         ('headroom - memory["measured_peak_bytes"] < memory["min_free_bytes"]:',
          'headroom - memory["measured_peak_bytes"] < memory["min_free_bytes"] - 1:')),
    case("ignore_resident", "LifecycleTests.test_refusal_never_starts",
         ('if observation["residents"] != []:', 'if False:')),
    case("ignore_effective_limits", "AdmissionTests.test_effective_ceiling_verified",
         ('def _limits(request, observation):\n', 'def _limits(request, observation):\n    return\n')),
    case("ignore_admission_result", "LifecycleTests.test_refusal_never_starts",
         ('if decision["rc"] != 0:', 'if False:')),
    case("skip_stop", "LifecycleTests.test_serial_switch_releases_before_start",
         ('stopped = await _call(backend.stop, lease["handle"], deadline_ns=deadline, lease=lease)', 'stopped = None')),
    case("noop_release_pass", "LifecycleTests.test_gate_rejects_noop_cleanup_mutant",
         ('if observation["residents"] != []:', 'if False:'),
         (' or payload["release_observation"]["residents"] != []', '')),
    case("success_only_cleanup", "LifecycleTests.test_exception_cancel_timeout_cleanup",
         ('        if lease is not None:\n            cleanup, cancelled', '        if lease is not None and operation_rc == 0:\n            cleanup, cancelled')),
    case("remove_cancellation_shield", "LifecycleTests.test_cancel_during_cleanup_is_shielded",
         ('await asyncio.shield(task)', 'await task')),
    case("ignore_pending_task", "LifecycleTests.test_late_cancel_resistant_work_keeps_lease",
         ('    await _settle_pending(lease, deadline)', '    pass  # mutant: ignore pending work')),
    case("ignore_lease_identity", "LifecycleTests.test_replaced_lease_preserved",
         ('if (not stat.S_ISREG(current.st_mode) or current.st_ino != original.st_ino or',
          'if False and (not stat.S_ISREG(current.st_mode) or current.st_ino != original.st_ino or')),
    case("ignore_fresh_sample", "LifecycleTests.test_observation_sample_must_be_fresh",
         ('if not before <= stamp <= time.monotonic_ns():', 'if False:')),
    case("ignore_runtime_limit", "LifecycleTests.test_runtime_ceiling_exceeded",
         ('if peak > request["memory"]["ceiling_bytes"]:', 'if False:')),
    case("ignore_running_limits", "LifecycleTests.test_readiness_and_release_require_evidence",
         ('        _limits(request, current)', '        pass  # mutant: omit running limits')),
    case("ignore_deadline", "LifecycleTests.test_exception_cancel_timeout_cleanup",
         ('remaining = (deadline_ns - time.monotonic_ns()) / 10**9', 'remaining = 10.0'),
         ('if time.monotonic_ns() > deadline_ns:', 'if False:')),
    case("publish_refusals", "CLITests.test_refusal_preserves_output_bytes",
         ('if result["rc"] == 0:', 'if True:')),
    case("skip_cli_authorization", "CLITests.test_cli_validates_authorization_before_backend",
         ('                _binding(request)\n', '                pass\n')),
    # The integrated kernel and local validator enforce the same invariant.
    # Remove both guards in scratch so the original assertion must detect it.
    case("allow_unknown_property", "AdmissionTests.test_closed_integer_only_schema",
         ('core.validate(payload, schema)', 'pass  # mutant: shared shape guard'),
         ('if child is False:', 'if False:')),
    case("allow_unpinned_revision", "AdmissionTests.test_closed_integer_only_schema",
         ('if request["provider_revision"] in ("main", "master", "latest", "HEAD"):', 'if False:')),
    case("allow_relative_lease", "AdmissionTests.test_closed_integer_only_schema",
         ('if not Path(request["lease_path"]).is_absolute():', 'if False:')),
    case("allow_multiple_residents_policy", "AdmissionTests.test_closed_integer_only_schema",
         ('core.validate(payload, schema)', 'pass  # mutant: shared shape guard'),
         ('if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):', 'if False:')),
    case("missing_absence_reason", "AdmissionTests.test_closed_integer_only_schema",
         ('            _text(observation["absence_reasons"].get(key), "absence_reasons/" + key)', '            pass')),
]


def review_case(name, test, *changes):
    return name, "test_f0u3_review." + test, SCHEDULER, changes


CASES += [
    review_case("ignore_blocked_provider", "ReviewLifecycleTests.test_blocked_provider_never_starts",
                ('if provider.get("runnable") is not True:', 'if False:')),
    review_case("ignore_contradictory_decision", "ReviewLifecycleTests.test_contradictory_decision_never_starts",
                ('or decision.get("verdict") != core.VERDICT[rc] or decision.get("allowed") is not (rc == 0)',
                 'or False')),
    *[review_case("ignore_decision_" + key, "ReviewLifecycleTests.test_wrong_decision_identity_never_starts",
                  (before, 'False')) for key, before in (
        ("provider", 'decision.get("provider_id") != request["provider_id"]'),
        ("operation", 'decision.get("operation") != request["slot"]'),
        ("jurisdiction", 'decision.get("jurisdiction") != request["context"].get("jurisdiction")'),
        ("commercial", 'decision.get("commercial_use") is not request["context"].get("commercial_use")'))],
    review_case("ignore_running_inventory", "ReviewLifecycleTests.test_post_start_inventory_mismatch_is_error",
                ('if residents != [{"handle": handle, "provider_id": request["provider_id"],\n'
                 '                          "provider_revision": request["provider_revision"]}]:', 'if False:')),
    review_case("ignore_stop_return_contract", "ReviewLifecycleTests.test_release_contract_errors_are_errors_and_fence",
                ('if stopped is not None:', 'if False:')),
    review_case("ignore_start_handle_contract", "ReviewLifecycleTests.test_nonstring_handle_is_error_and_fences",
                ('    if not isinstance(handle, str) or not handle:\n'
                 '        raise core.ContractError("backend_contract")\n    lease["handle"] = handle',
                 '    handle = str(handle)\n    lease["handle"] = handle')),
    review_case("late_start_loses_handle", "ReviewLifecycleTests.test_late_start_handle_is_stopped_and_persisted",
                ('    if capture_start:\n        _capture_start(lease, result)', '    pass'),
                ('                _capture_start(lease, task.result())', '                pass')),
    review_case("cancelled_start_loses_handle", "ReviewLifecycleTests.test_cancelled_start_returns_owned_handle_during_cleanup",
                ('                _capture_start(lease, task.result())', '                pass')),
    review_case("skip_admission_unwind", "ReviewLifecycleTests.test_cancelled_admission_observer_unwinds_before_unlink",
                ('    await _settle_pending(lease, deadline)', '    pass')),
    review_case("zero_peak_admits", "ReviewHostTests.test_zero_peak_cannot_prove_admission",
                ('if memory["measured_peak_bytes"] is None or memory["measured_peak_bytes"] == 0:',
                 'if memory["measured_peak_bytes"] is None:')),
    review_case("collapse_cleanup_contract_error", "ReviewLifecycleTests.test_release_contract_errors_are_errors_and_fence",
                ('return dict(_result(2, "backend_contract", exc.code), observation=observation)',
                 'return dict(_result(3, "release_unproven"), observation=observation)')),
    review_case("discard_admission_reasons", "ReviewLifecycleTests.test_all_admission_absences_survive_receipt",
                ('            reasons.extend(decision["reason_codes"])', '            pass')),
    review_case("forget_full_sample_history", "ReviewLifecycleTests.test_full_lifecycle_sample_ids_cannot_replay",
                ('observation["sample_id"] in seen', 'observation["sample_id"] == previous["sample_id"] if previous else False')),
    case("remove_exclusive_create", "LifecycleTests.test_shared_lease_excludes_second_process",
         ('os.O_CREAT | os.O_EXCL | os.O_WRONLY', 'os.O_CREAT | os.O_WRONLY')),
    review_case("skip_receipt_preflight", "ReviewHostTests.test_cli_aliases_and_missing_parent_refuse_before_start",
                ('            raise core.ContractError("request_invalid", "receipt_alias")', '            pass')),
]


def _main():
    if len(sys.argv) != 2:
        return 2
    destination = Path(sys.argv[1])
    original = SCHEDULER.read_bytes()
    results = []
    failure = None
    rc = 0
    bootstrap = ("import sys,unittest;sys.path.insert(0,sys.argv[1]);"
                 "unittest.main(module=None,argv=['unittest',sys.argv[2]])")
    try:
        with tempfile.TemporaryDirectory(prefix="af-f0u3-mutants-") as temporary:
            scratch = Path(temporary) / "component"
            shutil.copytree(ROOT, scratch, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name, test, path, changes in CASES:
                target = scratch / path.relative_to(ROOT)
                baseline = target.read_bytes()
                modified = baseline.decode("utf-8")
                for before, after in changes:
                    count = modified.count(before)
                    expected = 2 if before in (
                        'if observation["residents"] != []:',
                        '    await _settle_pending(lease, deadline)') else 1
                    if count != expected:
                        raise ValueError(name + ": mutation anchor count " + str(count))
                    modified = modified.replace(before, after)
                compile(modified, str(target), "exec")
                try:
                    target.write_text(modified, encoding="utf-8")
                    child = subprocess.run([sys.executable, "-I", "-B", "-c", bootstrap,
                                            str(scratch / "tests"), test],
                                           cwd=scratch, capture_output=True, text=True, timeout=15,
                                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), check=False)
                    output = child.stdout + child.stderr
                    killed = child.returncode == 1 and "FAIL:" in output and "AssertionError" in output
                    results.append({"mutant": name, "test": test, "rc": child.returncode, "killed": killed,
                                    "source": str(path.relative_to(ROOT)),
                                    "original_sha256": hashlib.sha256(baseline).hexdigest(),
                                    "mutant_sha256": hashlib.sha256(modified.encode("utf-8")).hexdigest(),
                                    "tail": output.splitlines()[-12:]})
                    print(json.dumps({"mutant": name, "rc": child.returncode, "killed": killed}), flush=True)
                finally:
                    target.write_bytes(baseline)
                if not killed:
                    rc = 5
                    failure = name + ": no assertion killed this mutant"
                    break
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        rc, failure = 2, str(exc)
    live_unchanged = SCHEDULER.read_bytes() == original
    if not live_unchanged:
        rc, failure = 5, "live source changed during scratch mutation proof"
    payload = {"rc": rc, "mutants": results, "expected": len(CASES),
               "killed": sum(row["killed"] for row in results),
               "restored": live_unchanged, "live_unchanged": live_unchanged, "failure": failure}
    try:
        fd, staged = tempfile.mkstemp(prefix=".af-mutation-", dir=destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, destination)
        finally:
            if os.path.exists(staged):
                os.unlink(staged)
    except OSError:
        return 2
    return rc


def main():
    try:
        return _main()
    except (OSError, ValueError, RuntimeError, SyntaxError, subprocess.TimeoutExpired):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
