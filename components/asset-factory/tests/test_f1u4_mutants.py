"""Run explicitly with python3 -I tests/test_f1u4_mutants.py.

Mutants execute temporary in-memory copies of the actual module source. The
unaltered tests run against each mutant; assertions must fail, with no test
errors. Production source bytes remain untouched. This is a gate on tests,
not evidence of real-provider quality.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import subprocess
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec = importlib.util.spec_from_file_location('bench_tests', ROOT/'tests/test_f1u4_bench.py')
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
original = tests.b
source_path = ROOT/'bench/run_bench.py'

# Each change bypasses a real enforcement point, with a named red oracle.
MUTANTS = [
    ('content-hash', "_require(_digest(data) == ref['sha256'], 4, 'evidence_stale')", "_require(True, 4, 'evidence_stale')", 'test_bench_receipt_identity_and_chain'),
    ('record-binding', "_require(rec[key] == value, 4, key + '_stale')", "_require(True, 4, key + '_stale')", 'test_bench_receipt_identity_and_chain'),
    ('gate-binding', "_require(record[key] == value, 4, key + '_stale')", "_require(True, 4, key + '_stale')", 'test_bench_gate_binding_is_rechecked'),
    ('receipt-chain', "_require(record['prev_receipt_sha256'] == previous, 4, 'receipt_chain_stale')", "_require(True, 4, 'receipt_chain_stale')", 'test_bench_receipt_identity_and_chain'),
    ('profile-pin', "_require(record['profile'] == (profile if name == 'vqa3d' else None), 4, 'profile_stale')", "_require(True, 4, 'profile_stale')", 'test_bench_profile_is_required_and_pinned'),
    ('coverage', "_require(set(required).issubset(ids), 3, 'required_check_missing')", "_require(True, 3, 'required_check_missing')", 'test_bench_required_coverage'),
    ('summary', "_require(record['rc'] == core.worst_rc(check['rc'] for check in checks), 2, 'gate_summary_mismatch')", "_require(True, 2, 'gate_summary_mismatch')", 'test_bench_summary_cannot_override_failed_check'),
    ('gate-verdict', "gate_rc = _gate(gate, name, bindings, policy['required_checks'][name], previous, profile)", "gate_rc = 0", 'test_bench_all_required_gates_block'),
    ('fixture-allowlist', "_require(reference['redistribution_allowed'] is True, 5, 'fixture_not_allowed')", "_require(True, 5, 'fixture_not_allowed')", 'test_bench_declarations_and_fixture_publication_authority'),
    ('decision-binding', "_require(decision.get(key) == core.sha256_json(item), 4, 'decision_stale')", "_require(True, 4, 'decision_stale')", 'test_bench_decision_replay'),
    ('measurement', "if ref is None:\n            codes.append(3)", "if ref is None:\n            codes.append(0)", 'test_bench_unproven_measurement_is_null'),
    ('attribution', "record['scope'] == 'owned-provider-cgroup'", 'True', 'test_bench_measurement_tamper_and_attribution'),
    ('byte-preservation', "if report['rc'] == 0:", 'if True:', 'test_bench_refusal_preserves_outputs'),
    ('deadline', 'time.monotonic() < deadline', 'True', 'test_bench_timeout_preserves_outputs'),
    ('duplicate-json', "_require(key not in result, 2, 'duplicate_key')", "_require(True, 2, 'duplicate_key')", 'test_bench_parser_rejects_duplicates_and_nonintegers'),
    ('closed-schema', "elif additional is False:", "elif False:", 'test_bench_canonical_and_closed_schema'),
    ('missing-receipt', "        _require(rec is not None, 3, 'missing_receipt')", "        if rec is None:\n            row.update(rc=0,status='OK',verdict='PASS')\n            return row", 'test_bench_missing_receipt_is_cannot_see'),
    ('matrix-denominator', "            evidence = dict(evidences.get((case['case_id'], provider['manifest']['id']), {}))", "            if (case['case_id'],provider['manifest']['id']) not in evidences:\n                continue\n            evidence = dict(evidences.get((case['case_id'], provider['manifest']['id']), {}))", 'test_bench_complete_matrix_same_policy'),
    ('missing-gate', "_require(ref is not None, 3, 'missing_receipt')", "if ref is None:\n                    continue", 'test_bench_missing_required_gate'),
    ('duplicate-gate', "_require(len({r['gate'] for r in gates}) == len(gates), 2, 'duplicate_gate')", "_require(True, 2, 'duplicate_gate')", 'test_bench_duplicate_gate'),
    ('duplicate-check', "_require(len(ids) == len(set(ids)), 2, 'duplicate_check')", "_require(True, 2, 'duplicate_check')", 'test_bench_bool_rc_and_duplicate_checks'),
    ('unpinned-provider', "revision not in ('main','master','latest','HEAD')", 'True', 'test_bench_blocked_provider_and_revision'),
    ('blocked-provider', "_require(manifest.get('runnable') is True and manifest.get('id') != 'hunyuan3d21', 5, 'provider_blocked')", "_require(True, 5, 'provider_blocked')", 'test_bench_blocked_provider_and_revision'),
    ('output-hash', "_require(_digest(data) == ref['sha256'], 4, 'evidence_stale')", "_require(True, 4, 'evidence_stale')", 'test_bench_measurement_tamper_and_attribution'),
    ('symlink-boundary', "_require(resolved.is_relative_to(base.resolve()), 2, 'path_escape')", "_require(True, 2, 'path_escape')", 'test_bench_symlink_and_budget'),
    ('byte-budget', "_require(budget[0] <= MAX_TOTAL_BYTES, 6, 'byte_budget_exceeded')", "_require(True, 6, 'byte_budget_exceeded')", 'test_bench_symlink_and_budget'),
    ('json-depth', "_require(depth <= 128, 6, 'json_depth_budget')", "_require(True, 6, 'json_depth_budget')", 'test_bench_json_depth_is_bounded'),
]

def prove():
    source = source_path.read_text()
    before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    results=[]
    for name, anchor, replacement, oracle in MUTANTS:
        count=source.count(anchor)
        if count != 1 and name != 'deadline':
            raise RuntimeError('non-unique mutation anchor: '+name)
        module=types.ModuleType('bench_mutant_'+name.replace('-','_'))
        module.__file__=str(source_path)
        exec(compile(source.replace(anchor,replacement),str(source_path),'exec'),module.__dict__)
        tests.b=module
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            result=unittest.TextTestRunner(stream=stream,verbosity=0).run(tests.BenchTests(oracle))
        tests.b=original
        killed=bool(result.failures) and not result.errors
        results.append({'mutant':name,'oracle':oracle,'killed':killed,
                        'failures':len(result.failures),'errors':len(result.errors)})
    # The CLI test starts another interpreter. This control must mutate the file
    # that child actually imports, then restore exactly the original bytes.
    original_bytes=source_path.read_bytes()
    anchor="        return core.emit(report)"
    replacement="        core.emit(report)\n        return 0"
    if source.count(anchor)!=1:
        raise RuntimeError('non-unique process-status anchor')
    try:
        source_path.write_text(source.replace(anchor,replacement))
        result=subprocess.run([sys.executable,'-I','-B',str(ROOT/'tests/test_f1u4_bench.py'),
                               'BenchTests.test_bench_failure_reaches_process_status'],
                              capture_output=True,timeout=30)
        killed=result.returncode==1 and b'FAIL:' in result.stderr and b'ERROR:' not in result.stderr
        results.append({'mutant':'process-exit','oracle':'test_bench_failure_reaches_process_status',
                        'killed':killed,'failures':int(killed),'errors':0 if killed else 1})
    finally:
        source_path.write_bytes(original_bytes)
    unchanged=hashlib.sha256(source_path.read_bytes()).hexdigest()==before
    report={'rc':0 if unchanged and all(r['killed'] for r in results) else 5,
            'source_sha256':before,'source_unchanged':unchanged,'mutants':results}
    print(json.dumps(report,sort_keys=True,separators=(',',':')))
    return report['rc']

if __name__=='__main__':
    raise SystemExit(prove())
