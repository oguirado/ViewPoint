"""Adversarial regressions: actual disk-backed replay, never asset-quality proof."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import test_f1u4_bench as base

b = base.b


def refresh(case, provider, evidence):
    decision = provider['decision']
    for key, source in (('configuration_sha256', 'factory'), ('provider_sha256', 'manifest'), ('policy_sha256', 'policy')):
        decision[key] = b.core.sha256_json(provider[source])
    evidence['record']['provider_sha256'] = b.core.sha256_json(provider['manifest'])
    for name in b.GATES:
        base.rewrite_gate(evidence, name, lambda g: g.update(provider_sha256=evidence['record']['provider_sha256']))
    measurement = json.loads(evidence['files']['measurement.json'])
    measurement['provider_sha256'] = evidence['record']['provider_sha256']
    evidence['files']['measurement.json'] = base.data(measurement)
    evidence['record']['measurement_ref']['sha256'] = base.digest(base.data(measurement))


class RepairTests(unittest.TestCase):
    def disk(self, mutate=None):
        case, provider, evidence = base.fixture()
        if mutate:
            mutate(case, provider, evidence)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = base.disk_plan(root, case, provider, evidence)
            target = root/'out'/'results.json'
            target.parent.mkdir()
            target.write_bytes(b'sentinel')
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                rc = b.run_bench(plan, ('synthetic',), target.parent, timeout_s=10)
            self.assertEqual(target.read_bytes(), b'sentinel')
            return rc, json.loads(stream.getvalue())

    def test_F_A_provider_contract(self):
        def mutate(c, p, e):
            del p['manifest']['schema']; del p['manifest']['licence']
            refresh(c, p, e)
        self.assertEqual(self.disk(mutate)[0], 2)

    def test_F_A_decision_contract(self):
        def mutate(c, p, e):
            del p['decision']['monetisable']; del p['decision']['evaluated_dependencies']
        self.assertEqual(self.disk(mutate)[0], 2)

    def test_F_A_jurisdiction_membership(self):
        def mutate(c, p, e):
            p['factory']['jurisdiction'] = p['decision']['jurisdiction'] = 'XX'
            p['policy']['countries'] = []; p['policy']['groups'] = {}
            refresh(c, p, e)
        self.assertEqual(self.disk(mutate)[0], 2)

    def test_F_B_replay_cannot_publish_asset_pass(self):
        rc, report = self.disk()
        self.assertEqual(rc, 3)
        self.assertEqual(report['verdict'], 'CANNOT_SEE')
        self.assertEqual(report['rows'][0]['reasons'], ['upstream_verification_unavailable'])
        self.assertEqual(report['rows'][0]['replay_rc'], 0)

    def test_F_C_host_label_has_no_attribution(self):
        def mutate(c, p, e):
            record = json.loads(e['files']['measurement.json'])
            record['scope'] = 'host:MemTotal-MemAvailable'
            e['files']['measurement.json'] = base.data(record)
            e['record']['measurement_ref']['sha256'] = base.digest(base.data(record))
        rc, report = self.disk(mutate)
        self.assertEqual(rc, 3)
        row = report['rows'][0]
        self.assertIn('measurement_unattributed', row['reasons'])
        self.assertIsNone(row['memory_peak_bytes'])
        self.assertIsNone(row['wall_ms'])

    def test_F_D_disabled_slot(self):
        def mutate(c, p, e):
            p['factory']['slots']['generate']['provider'] = 'none'
            refresh(c, p, e)
        rc, report = self.disk(mutate)
        self.assertEqual(rc, 2)
        self.assertIn('configured_provider_mismatch', report['rows'][0]['reasons'])

    def test_F_D_wrong_profile(self):
        def mutate(c, p, e):
            p['factory']['gates']['vqa3d_profile'] = 'other-profile'
            refresh(c, p, e)
        rc, report = self.disk(mutate)
        self.assertEqual(rc, 2)
        self.assertIn('configured_profile_mismatch', report['rows'][0]['reasons'])

    def test_F_E_incomplete_does_not_mask_fail(self):
        def mutate(c, p, e):
            base.rewrite_gate(e, 'input', lambda g: g['checks'][0].update(id='other'))
            base.rewrite_gate(e, 'appearance', lambda g: g.update(rc=5, verdict='FAIL', checks=[{'id':'appearance-check','rc':5,'verdict':'FAIL'}]))
        rc, report = self.disk(mutate)
        self.assertEqual(rc, 5)
        self.assertEqual(report['rows'][0]['rc'], 5)
        self.assertEqual([g['gate'] for g in report['rows'][0]['gate_results']], list(b.GATES))

    def test_F_E_incomplete_does_not_mask_error(self):
        def mutate(c, p, e):
            base.rewrite_gate(e, 'input', lambda g: g['checks'][0].update(id='other'))
            base.rewrite_gate(e, 'vqa3d', lambda g: g.update(verdict='INVALID'))
        self.assertEqual(self.disk(mutate)[0], 2)

    def test_F_E_missing_gate_file_does_not_mask_fail(self):
        # Rehash the chain first, then delete the input bytes.
        def mutation(c, p, e):
            base.rewrite_gate(e, 'appearance', lambda g: g.update(rc=5, verdict='FAIL', checks=[{'id':'appearance-check','rc':5,'verdict':'FAIL'}]))
            e['files'].pop('input.json')
        self.assertEqual(self.disk(mutation)[0], 5)

    def test_F_E_missing_coverage_does_not_mask_malformed_json(self):
        def mutate(c, p, e):
            base.rewrite_gate(e, 'input', lambda g: g['checks'][0].update(id='other'))
            ref = e['record']['gate_receipts'][-1]
            e['files'][ref['path']] = b'{bad'
            ref['sha256'] = base.digest(b'{bad')
        self.assertEqual(self.disk(mutate)[0], 2)

    def test_F_C_owned_label_without_observation_is_absent(self):
        def mutate(c, p, e):
            measurement = json.loads(e['files']['measurement.json'])
            del measurement['execution']; del measurement['observation_ref']
            e['files']['measurement.json'] = base.data(measurement)
            e['record']['measurement_ref']['sha256'] = base.digest(base.data(measurement))
        rc, report = self.disk(mutate)
        self.assertEqual(rc, 3)
        self.assertIsNone(report['rows'][0]['memory_peak_bytes'])
        self.assertIn('measurement_unattributed', report['rows'][0]['reasons'])

    def test_F_C_observation_owner_is_bound(self):
        def mutate(c, p, e):
            observation = json.loads(e['files']['observation.json'])
            observation['execution']['id'] = 'unrelated-run'
            e['files']['observation.json'] = base.data(observation)
            measurement = json.loads(e['files']['measurement.json'])
            measurement['observation_ref']['sha256'] = base.digest(base.data(observation))
            e['files']['measurement.json'] = base.data(measurement)
            e['record']['measurement_ref']['sha256'] = base.digest(base.data(measurement))
        rc, report = self.disk(mutate)
        self.assertEqual(rc, 3)
        self.assertIsNone(report['rows'][0]['memory_peak_bytes'])

    def test_F_E_missing_coverage_within_failed_gate(self):
        def mutate(c, p, e):
            base.rewrite_gate(e, 'input', lambda g: g.update(rc=5, verdict='FAIL', checks=[{'id':'different-check','rc':5,'verdict':'FAIL'}]))
        self.assertEqual(self.disk(mutate)[0], 5)

    def test_F_H_malformed_provider_identity_has_valid_error_row(self):
        case, provider, evidence = base.fixture()
        provider['manifest']['id'] = False
        row = b.evaluate_case(case, provider, evidence)
        self.assertEqual(row['rc'], 2)
        self.assertEqual(row['provider_id'], '')
        b.validate_record(row, 'results/result')

    def test_F_F_missing_record_is_cannot_see(self):
        case, provider, evidence = base.fixture()
        evidence['record'] = None
        row = b.evaluate_case(case, provider, evidence)
        self.assertEqual(row['rc'], 3)
        self.assertEqual(row['verdict'], 'CANNOT_SEE')
        self.assertEqual(row['reasons'], ['missing_receipt'])

    def test_F_F_missing_gate_file_is_cannot_see(self):
        def mutate(c, p, e):
            e['files'].pop('input.json')
        # disk_plan writes only provided files; loader must retain other gates.
        rc, report = self.disk(mutate)
        self.assertEqual(rc, 3)
        self.assertIn('missing_receipt', report['rows'][0]['reasons'])

    def test_F_G_operator_ruling_already_applied(self):
        root = base.ROOT/'bench/references'
        self.assertFalse((root/'ref1_gemdroid_front.png').exists())
        expected = {'source_sheet_6.png','ref2_freestyler_front_crop.png','ref3_speaker_front_crop.png'}
        manifest = json.loads((root/'manifest.json').read_bytes())
        self.assertEqual({r['path'] for r in manifest['references']}, expected)
        for ref in manifest['references']:
            self.assertEqual(base.digest((root/ref['path']).read_bytes()), ref['sha256'])
            self.assertIn('publication', ref['provenance'])

    def test_F_H_float_returns_error_row(self):
        case, provider, evidence = base.fixture()
        case['parameters']['resolution'] = 1.5
        row = b.evaluate_case(case, provider, evidence)
        self.assertEqual(row['rc'], 2)
        self.assertEqual(row['verdict'], 'ERROR')
        self.assertIsNone(row['case_sha256'])
        b.validate_record(row, 'results/result')

    def test_F_H_nonobjects_return_error_rows(self):
        for part in ('case', 'provider', 'evidence'):
            for invalid in (None, [], 'bad'):
                with self.subTest(part=part, invalid=invalid):
                    args = dict(zip(('case','provider','evidence'), base.fixture()))
                    args[part] = invalid
                    row = b.evaluate_case(**args)
                    self.assertEqual(row['rc'], 2)
                    self.assertEqual(row['verdict'], 'ERROR')
                    b.validate_record(row, 'results/result')
