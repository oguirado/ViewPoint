"""Adversarial-review regressions; synthetic evidence only."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('f1u5_repair_oracles', Path(__file__).with_name('test_f1u5_appearance.py'))
oracles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracles)
gate, registry, core = oracles.gate, oracles.registry, oracles.core


class ReviewRegressions(oracles.AppearanceTests):
    def test_f1_fabricated_coverage_cannot_pass(self):
        observations = [{'case_id': case, 'rc': 5, 'verdict': 'FAIL',
                         'substitute_for': None,
                         'bindings': dict.fromkeys(registry.BOUND_FIELDS, '0' * 64)}
                        for case in registry.ORIGINAL_CASES]
        self.assertEqual(registry.coverage(observations)['rc'], 3)

    def test_f2_provider_revision_requires_fresh_licence(self):
        self.request['provider']['revision'] = 'different-provider-revision'
        self.review = self.make_review()
        self.check(4)

    def test_f2_operation_requires_fresh_licence(self):
        self.licence['operation'] = 'uv_texture'
        self.rebind()
        self.check(4)

    def cli_probe(self):
        args, output = self.cli_files()
        output.write_bytes(b'caller bytes')
        return args, output

    def run_cli(self, args):
        return subprocess.run([sys.executable, '-B', '-m', 'gates.appearance_gate.evaluate', *args],
                              cwd=oracles.ROOT, capture_output=True, timeout=3,
                              start_new_session=True)

    def test_f3_cli_fifo_refused_without_outer_kill(self):
        args, output = self.cli_probe()
        request = self.root / 'request.json'
        request.unlink()
        os.mkfifo(request)
        try:
            proc = self.run_cli(args)
        except subprocess.TimeoutExpired:
            self.fail('CLI blocked on FIFO until the external timeout killed it')
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['rc'], 2)
        self.assertEqual(output.read_bytes(), b'caller bytes')

    def test_f4_surrogate_diagnostic_never_returns_stage_one(self):
        args, output = self.cli_probe()
        self.request['case_id'] = '\ud800'
        (self.root / 'request.json').write_text(json.dumps(self.request, ensure_ascii=True))
        proc = self.run_cli(args)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['rc'], 2)
        self.assertEqual(output.read_bytes(), b'caller bytes')

    def test_f5_nested_licence_reason_types(self):
        self.licence['reason_codes'] = [None, {'malformed': True}]
        self.rebind()
        self.check(2)

    def test_f5_advisory_score_type(self):
        self.review['min_score'] = {'not': 'an integer'}
        self.check(2)

    def evidence_registry(self):
        """Exercise intake with labelled synthetic data, never claim live coverage."""
        entries, evidence = [], []
        for case in registry.ORIGINAL_CASES:
            request = copy.deepcopy(self.request)
            request['case_id'] = case
            review = copy.deepcopy(self.review)
            review.update(case_id=case, request_sha256=core.sha256_json(request),
                          likeness_to_reference=1)
            entries.append({'id': case, 'asset_ref': request['asset'],
                            'reference_ref': request['reference'], 'source_pointer': None,
                            'provider': 'trellis2', 'revision': request['provider']['revision'],
                            'licence': 'MIT', 'provenance_state': 'VERIFIED',
                            'execution_allowed': True, 'expected_verdict': 'FAIL',
                            'observation_status': 'CANNOT_SEE', 'defect': 'synthetic contract case',
                            'substitute_for': None})
            evidence.append({'case_id': case, 'request': request, 'upstream': self.upstream,
                             'review': review})
        data = {'schema': 'af.fixtures-negative/v1', 'fixtures': entries}
        return data, evidence

    def verified_coverage(self, data, evidence, pin=None):
        return registry.coverage(evidence, registry_data=data,
                                 registry_sha256=pin or core.sha256_json(data))

    def test_f1_pinned_evidence_revalidated(self):
        data, evidence = self.evidence_registry()
        self.assertEqual(self.verified_coverage(data, evidence)['rc'], 0)
        (self.root / self.request['asset']['path']).write_bytes(b'tampered')
        self.assertEqual(self.verified_coverage(data, evidence)['rc'], 3)

    def test_f1_pinned_registry_rejects_invented_results(self):
        data, evidence = self.evidence_registry()
        for item in evidence:
            item.pop('request')
            item.update(rc=5, verdict='FAIL', bindings=dict.fromkeys(registry.BOUND_FIELDS, '0' * 64))
        self.assertEqual(self.verified_coverage(data, evidence)['rc'], 3)

    def test_f1_relabelled_substitutes_and_registry_drift(self):
        data, evidence = self.evidence_registry()
        pin = core.sha256_json(data)
        data['fixtures'][0]['substitute_for'] = 'synthetic_good'
        self.assertEqual(self.verified_coverage(data, evidence, pin)['rc'], 4)
        self.assertEqual(self.verified_coverage(data, evidence)['rc'], 3)
        data['fixtures'][0]['substitute_for'] = None
        evidence[0]['request']['case_id'] = 'synthetic_good'
        self.assertEqual(self.verified_coverage(data, evidence)['rc'], 3)

    def test_f1_blocked_originals_never_evaluated(self):
        data = core.load_json(oracles.ROOT / 'gates/fixtures_negative/registry.json',
                              max_bytes=gate.MAX_JSON_BYTES)
        _, evidence = self.evidence_registry()
        with mock.patch.object(registry, 'evaluate', side_effect=AssertionError('blocked original read')):
            result = self.verified_coverage(data, evidence)
        self.assertEqual(result['rc'], 3)
        self.assertEqual(set(result['cases'].values()), {'CANNOT_SEE'})

    def test_f1_revalidated_false_pass_and_duplicate(self):
        data, evidence = self.evidence_registry()
        evidence[0]['review']['likeness_to_reference'] = 8
        self.assertEqual(self.verified_coverage(data, evidence)['rc'], 5)
        self.assertEqual(self.verified_coverage(data, evidence + [evidence[0]])['rc'], 2)

    def test_f2_current_inputs_all_bound(self):
        for field in ('provider_manifest', 'configuration', 'territory_policy'):
            with self.subTest(field=field):
                self.licence_context[field]['changed'] = True
                self.rebind()
                self.check(4)
                self.licence_context[field].pop('changed')
        self.request.pop('licence_context')
        self.review = self.make_review()
        self.check(3)

    def test_f3_upstream_and_review_fifos(self):
        for name in ('upstream', 'review'):
            with self.subTest(name=name):
                args, output = self.cli_probe()
                path = self.root / (name + '.json')
                path.unlink()
                os.mkfifo(path)
                try:
                    proc = self.run_cli(args)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(output.read_bytes(), b'caller bytes')
                finally:
                    path.unlink()

    def test_f3_outer_deadline_covers_loading(self):
        args, output = self.cli_probe()
        with mock.patch.object(gate, 'MAX_TIMEOUT_MS', 20):
            with mock.patch.object(gate, '_load_input', side_effect=lambda path: time.sleep(1)):
                with oracles.contextlib.redirect_stdout(oracles.io.StringIO()) as stream:
                    self.assertEqual(gate.main(args), 6)
        self.assertEqual(json.loads(stream.getvalue())['rc'], 6)
        self.assertEqual(output.read_bytes(), b'caller bytes')

    def test_f4_surrogates_in_all_echoed_fields(self):
        for obj, field in ((self.request, 'policy_sha256'), (self.upstream, 'verdict'),
                           (self.review['reviewer'], 'id')):
            with self.subTest(field=field):
                old = obj[field]
                obj[field] = '\udfff'
                result = gate.evaluate(self.request, self.upstream, self.review)
                self.assertEqual(result['rc'], 2)
                core.canonical_json(result).encode('utf-8')
                obj[field] = old

    def test_f5_nested_licence_identity_types(self):
        self.licence['provider_id'] = ['trellis2']
        self.rebind()
        self.check(2)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ReviewRegressions(name) for name in ReviewRegressions.__dict__
                              if name.startswith('test_'))
