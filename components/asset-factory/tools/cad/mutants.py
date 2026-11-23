"""Demonstrate that CAD assertions detect isolated deliberately broken gates.

Only scratch copies are mutated. The normal source and tests remain unchanged.
Run under the same resource scope as the CAD test suite.
"""
import argparse
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest

from asset_factory import core
from tools.cad.contracts import ROOT, Refusal, require


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    spec = importlib.util.spec_from_file_location('cad_gate_tests', ROOT/'tests/test_w3_cad_gate.py')
    tests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tests)
    original_evaluate = tests.evaluate
    source = (ROOT/'tools/cad/gate.py').read_text()
    mutations = [
        ('ignore_bbox', "if bbox_error > policy['dimension_tolerance_um']:", 'if False:',
         'test_bbox_check_is_independently_observable'),
        ('ignore_hole', "if mass['topology']['genus'] != policy['expected_genus']:", 'if False:',
         'test_hole_and_missing_hole_red_then_green'),
        ('accept_failed_metrics', 'record(5 if failed else 0, failed,', 'record(0, failed,',
         'test_unit_swap_red_then_green'),
    ]
    receipts = []
    with tempfile.TemporaryDirectory(prefix='.cad-mutants-', dir=args.report.parent) as temporary:
        for name, old, new, check in mutations:
            require(source.count(old) == 1, 2, 'mutant_anchor_drift')
            file = Path(temporary)/(name+'.py')
            file.write_text(source.replace(old, new))
            spec = importlib.util.spec_from_file_location(name, file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            tests.evaluate = module.evaluate
            log = io.StringIO()
            result = unittest.TextTestRunner(stream=log).run(unittest.TestSuite([tests.CadGateTests(check)]))
            require(len(result.failures) == 1 and not result.errors, 5, 'mutant_survived_or_errored')
            tests.evaluate = original_evaluate
            green = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([tests.CadGateTests(check)]))
            require(green.wasSuccessful(), 5, 'restored_gate_failed')
            receipts.append({'mutant': name, 'test': check, 'mutant_rc': 5,
                             'restored_rc': 0, 'assertion_failure': log.getvalue()})
    report = {'schema': 'af.cad-mutant-proof/v1', 'rc': 0, 'status': 'OK',
              'scope': 'CAD-host-gates-only', 'controls': receipts,
              'gate_sha256': core.sha256_file(ROOT/'tools/cad/gate.py'),
              'tests_sha256': core.sha256_file(ROOT/'tests/test_w3_cad_gate.py')}
    require(not args.report.exists() and not args.report.is_symlink(), 5, 'output_exists')
    core.atomic_write_json(args.report, report)
    return core.emit(report)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Refusal as exc:
        raise SystemExit(core.emit({'rc': exc.rc, 'status': core.STATUS[exc.rc], 'reason_codes': [exc.code]}))
