"""Explicit adversarial controls for repair regressions; no production bypass."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))
import test_f1u4_repair as tests

MUTANTS = [
    ('F-A', 'validate_bundle(bundle, schema=_schema, require=_require, slots=core.SLOTS)',
     'pass', 'test_F_A_provider_contract'),
    ('F-B', "if rc == 0:\n        # No schema-specific", "if False:\n        # No schema-specific",
     'test_F_B_replay_cannot_publish_asset_pass'),
    ('F-C', "record['scope'] == 'owned-provider-cgroup'", 'True',
     'test_F_C_host_label_has_no_attribution'),
    ('F-D-provider', "provider['factory']['slots'][case['slot']]['provider'] == manifest['id']", 'True',
     'test_F_D_disabled_slot'),
    ('F-D-profile', "provider['factory']['gates']['vqa3d_profile'] == profile['id']", 'True',
     'test_F_D_wrong_profile'),
    ('F-E', 'except Refusal as exc:\n                gate_rc, gate_reason = exc.rc, exc.reason',
     'except Refusal as exc:\n                raise', 'test_F_E_incomplete_does_not_mask_fail'),
    ('F-F-record', "_require(rec is not None, 3, 'missing_receipt')",
     "_require(rec is not None, 5, 'missing_receipt')", 'test_F_F_missing_record_is_cannot_see'),
    ('F-F-file', "_require(path.is_file(), 3, 'missing_receipt')",
     "_require(path.is_file(), 5, 'missing_receipt')", 'test_F_F_missing_gate_file_is_cannot_see'),
    ('F-H', "_require(all(isinstance(v, dict) for v in (case, provider, evidence)), 2, 'schema_mismatch')",
     "if not all(isinstance(v, dict) for v in (case, provider, evidence)):\n            return row",
     'test_F_H_nonobjects_return_error_rows'),
]


def prove():
    source_path = ROOT/'bench/run_bench.py'
    before = source_path.read_bytes()
    source = before.decode()
    original = tests.b
    results = []
    for name, anchor, replacement, oracle in MUTANTS:
        if source.count(anchor) != 1:
            raise RuntimeError('mutation anchor is not unique: '+name)
        module = types.ModuleType('repair_mutant')
        module.__file__ = str(source_path)
        exec(compile(source.replace(anchor, replacement), str(source_path), 'exec'), module.__dict__)
        tests.b = tests.base.b = module
        stream = io.StringIO()
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = unittest.TextTestRunner(stream=stream, verbosity=0).run(tests.RepairTests(oracle))
        finally:
            tests.b = tests.base.b = original
        results.append({'mutant':name,'test':oracle,'killed':bool(result.failures) and not result.errors,
                        'failures':len(result.failures),'errors':len(result.errors)})
    unchanged = source_path.read_bytes() == before
    report = {'rc':0 if unchanged and all(r['killed'] for r in results) else 5,
              'source_unchanged':unchanged, 'source_sha256':hashlib.sha256(before).hexdigest(),
              'mutants':results}
    print(json.dumps(report, sort_keys=True))
    return report['rc']


if __name__ == '__main__':
    raise SystemExit(prove())
