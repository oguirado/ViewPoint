# SPDX-License-Identifier: AGPL-3.0-only
"""Reconstruction closure through the published, offline control route."""

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.projections import PublicationError, SlotProjector, generate, verify_publication
from slot_host.projections.schema import digest
from slot_host.projections.tests.test_projections import load_registry, minimal_manifest
from slot_host.runtime import tools
from slot_host.runtime.tests._tool_gate import assert_tool_gate

TOOL = 'reconstruction.obligations'
ROUTE = 'host.reconstruction.obligations'
MCP = 'rea_reconstruction_obligations'
REQUEST_ID = 'fixture-reconstruction-request'
CONTEXT = {'caller': 'agent', 'agent_instance': 'reconstruction-fixture'}
SUBJECT = {'boundary_id': 'fixture:boundary', 'evidence_bundle_ref': 'fixture:evidence-bundle'}
REVIEW = 'review.identity'
PROCESS = 'process.packaged'
PROCESS_CASES = ['positive', 'negative', 'cancellation', 'teardown']


def fixture():
    request = {**SUBJECT, 'reviewed_obligations': [], 'bindings': [], 'contradictions': []}
    retained = {'schema': 'rea.reconstruction_obligation_evidence/v1', **SUBJECT,
                'originals': [], 'fixtures': [], 'verifiers': [], 'contradictions': []}
    for obligation, cases, original_authority, proof_authority, owner in (
        (REVIEW, ['positive'], 'static', 'unit', '1' * 64),
        (PROCESS, PROCESS_CASES, 'process', 'packaged-process', '2' * 64),
    ):
        revision = {'obligation_id': obligation, 'obligation_version': 1}
        request['reviewed_obligations'].append({
            **revision, 'required_case_kinds': list(cases), 'required_original_authority': original_authority,
            'required_fixture_authority': proof_authority, 'required_verifier_authority': proof_authority,
        })
        proof = dict(revision, authority=proof_authority, owner_sha256=owner, outcome='pass')
        verifier = ({'verifier_id': 'verifier.reviewed', 'claim_id': 'claim.reviewed'} if obligation == REVIEW
                    else {'verifier_id': 'verifier.packaged', 'claim_id': 'claim.process'})
        result_ref = f'evidence:{obligation}:verifier'
        retained['verifiers'].append(dict(proof, **verifier, evidence_ref=result_ref))
        binding = {'obligation_id': obligation, 'owner_sha256': owner,
                   'original_cases': [], 'fixtures': [],
                   'verifier': dict(verifier, enumerated_obligation_ids=[obligation], result_evidence_ref=result_ref)}
        for kind in cases:
            original = {'kind': kind, 'evidence_ref': f'evidence:{obligation}:original'}
            fixture_prefix = 'fixture' if obligation == REVIEW else 'packaged'
            link = {'fixture_id': f'{fixture_prefix}.{kind}', 'case_kind': kind,
                    'evidence_ref': f'evidence:{obligation}:fixtures'}
            retained['originals'].append(dict(revision, **original, authority=original_authority))
            retained['fixtures'].append(dict(proof, **link))
            binding['original_cases'].append(original)
            binding['fixtures'].append(link)
        request['bindings'].append(binding)
    retained['reviewed_obligations'] = copy.deepcopy(request['reviewed_obligations'])
    return request, retained


class FixtureHost:
    def __init__(self, bundle):
        self.bundle = bundle
        self.calls = []

    def resolve_reconstruction_evidence(self, evidence_bundle_ref, *, boundary_id, request_id, max_bytes):
        self.calls.append((evidence_bundle_ref, boundary_id, request_id, max_bytes))
        return json.dumps(self.bundle, sort_keys=True).encode('ascii')


class ReconstructionObligationTests(unittest.TestCase):
    def setUp(self):
        self.payload, retained = fixture()
        self.host = FixtureHost(retained)
        self.registry = load_registry()
        schemas = Path(__file__).resolve().parents[4] / 'vp-direct' / 'schemas'
        for kind in ('req', 'reply'):
            schema = json.loads((schemas / f'vp.control.{kind}_v1.schema.json').read_bytes())
            self.registry[schema['$id']] = schema
        self.registry.update(copy.deepcopy(tools.ARGUMENT_SCHEMAS))
        self.registry.update(copy.deepcopy(tools.RESULT_SCHEMAS))
        self.registry['host_mini_tools'] = tools.MINI_TOOLS
        self.policy = {'perception_ceiling': 'none'}
        self.manifest = minimal_manifest(registry=self.registry)
        self.bundle = self.generate(self.manifest)
        self.projector = self.make_projector(self.manifest, self.bundle)

    def generate(self, manifest, **options):
        return generate(manifest, policy=self.policy, registry=self.registry, **options)

    def make_projector(self, manifest, bundle, *, dispatched=True):
        def dispatch(envelope):
            normalized = projector.prepare(envelope, authenticated_context=CONTEXT)
            return tools.handle_reconstruction_control(normalized, host=self.host)

        projector = SlotProjector(manifest, bundle, policy=self.policy, registry=self.registry,
                                  dispatch=dispatch if dispatched else None, context=CONTEXT,
                                  authenticated_context=CONTEXT)
        return projector

    def args(self, payload=None, **options):
        return {'request_id': REQUEST_ID, 'payload': self.payload if payload is None else payload, **options}

    def evaluate(self, payload=None):
        reply = self.projector.invoke(ROUTE, self.args(payload))
        self.assertEqual(reply['code'], 'OK', reply)
        return reply['result']['payload']

    def test_required_verifier_evidence_link_moves_only_its_obligation_to_outstanding(self):
        complete = self.evaluate()
        self.assertEqual(complete['summary'], {
            'status': 'complete', 'closed': 2, 'open': 0, 'failed': 0, 'unknown': 0,
            'outstanding_obligation_ids': [],
        })
        self.assertEqual(self.evaluate(), complete)
        expected_digest = digest({
            'schema': 'rea.reconstruction_obligation_ledger/v1',
            'evidence_bundle_sha256': hashlib.sha256(json.dumps(self.host.bundle, sort_keys=True).encode('ascii')).hexdigest(),
            'request': self.payload,
            'result': {key: value for key, value in complete.items() if key != 'closure_digest'},
        })
        self.assertEqual(complete['closure_digest'], expected_digest)

        incomplete_input = copy.deepcopy(self.payload)
        del incomplete_input['bindings'][0]['verifier']['result_evidence_ref']
        incomplete = self.evaluate(incomplete_input)
        self.assertEqual(incomplete['summary'], {
            'status': 'incomplete', 'closed': 1, 'open': 1, 'failed': 0, 'unknown': 0,
            'outstanding_obligation_ids': [REVIEW],
        })
        self.assertEqual(incomplete['obligations'][0], complete['obligations'][0])
        self.assertEqual(incomplete['obligations'][1], {
            'id': REVIEW, 'status': 'open', 'required_case_kinds': ['positive'],
            'authority_gaps': ['verifier:missing_or_inapplicable_evidence'], 'unknowns': [],
        })
        self.assertNotEqual(incomplete['closure_digest'], complete['closure_digest'])

    def test_process_closure_requires_every_case_and_applicable_proof(self):
        self.assertEqual(self.evaluate()['obligations'][0]['required_case_kinds'], PROCESS_CASES)
        nominal_bundle = self.host.bundle
        cases = (
            ('originals', {'kind': 'cancellation'}, 'authority', 'runtime',
             'original:cancellation:insufficient_authority'),
            ('fixtures', {'case_kind': 'teardown'}, 'owner_sha256', '9' * 64,
             'fixture:teardown:missing_or_inapplicable_evidence'),
            ('verifiers', {}, 'obligation_version', 2, 'verifier:missing_or_inapplicable_evidence'),
            ('verifiers', {}, 'authority', 'unit', 'verifier:insufficient_authority'),
            ('verifiers', {}, 'claim_id', 'claim.foreign', 'verifier:missing_or_inapplicable_evidence'),
        )
        for table, selector, key, value, gap in cases:
            with self.subTest(table=table, key=key):
                self.host.bundle = copy.deepcopy(nominal_bundle)
                record = next(row for row in self.host.bundle[table] if row['obligation_id'] == PROCESS
                              and all(row[field] == expected for field, expected in selector.items()))
                record[key] = value
                report = self.evaluate()
                self.assertEqual(report['summary']['outstanding_obligation_ids'], [PROCESS])
                self.assertEqual(report['obligations'][0]['status'], 'open')
                self.assertTrue(any(reason.startswith(gap + ':') for reason in
                                    report['obligations'][0]['authority_gaps']), report)
        self.host.bundle = nominal_bundle
        request = copy.deepcopy(self.payload)
        request['bindings'][1]['fixtures'].pop()
        self.assertEqual(self.evaluate(request)['obligations'][0]['authority_gaps'],
                         ['fixture:teardown:missing_case'])
        request = copy.deepcopy(self.payload)
        request['bindings'][1]['verifier']['enumerated_obligation_ids'] = [REVIEW]
        self.assertEqual(self.evaluate(request)['obligations'][0]['authority_gaps'],
                         ['verifier:does_not_enumerate_obligation'])
        request = copy.deepcopy(self.payload)
        request['bindings'].append(dict(request['bindings'][1], owner_sha256='9' * 64))
        self.assertEqual(self.evaluate(request)['obligations'][0]['authority_gaps'],
                         ['ambiguous_owner_binding'])

    def test_failed_proof_and_admitted_contradiction_cannot_be_omitted_to_close(self):
        nominal_bundle = self.host.bundle
        for table, link in (
            ('verifiers', {'evidence_ref': f'evidence:{REVIEW}:verifier-attempt-2'}),
            ('fixtures', {'evidence_ref': f'evidence:{REVIEW}:fixture-fail', 'fixture_id': 'fixture.fail'}),
        ):
            with self.subTest(table=table):
                self.host.bundle = copy.deepcopy(nominal_bundle)
                records = self.host.bundle[table]
                failed_proof = dict(records[0], **link, outcome='fail')
                records.extend((
                    dict(failed_proof, obligation_version=2),
                    dict(failed_proof, owner_sha256='9' * 64),
                ))
                self.assertEqual(self.evaluate()['summary']['status'], 'complete')

                records.append(failed_proof)
                ledger = self.evaluate()
                self.assertEqual(ledger['obligations'][1]['status'], 'failed')
                self.assertEqual(ledger['obligations'][1]['authority_gaps'], ['admitted_failed_proof'])
                self.assertEqual(ledger['summary']['status'], 'incomplete')
                self.assertEqual(ledger['summary']['outstanding_obligation_ids'], [REVIEW])

        self.host.bundle['contradictions'].append({
            'obligation_id': PROCESS, 'obligation_version': 1, 'evidence_ref': 'evidence:conflicting-control',
        })
        ledger = self.evaluate()
        self.assertEqual(ledger['summary']['failed'], 2)
        self.assertEqual(ledger['summary']['status'], 'incomplete')
        self.assertEqual(ledger['obligations'][0]['authority_gaps'], ['admitted_contradiction'])

    def test_lower_authority_failure_cannot_be_masked_by_cited_pass(self):
        nominal_bundle = self.host.bundle
        for table, label in (('verifiers', 'verifier'), ('fixtures', 'fixture:positive')):
            with self.subTest(table=table):
                self.host.bundle = copy.deepcopy(nominal_bundle)
                records = self.host.bundle[table]
                passing = next(record for record in records if record['obligation_id'] == PROCESS)
                records.append(dict(passing, authority='unit', outcome='fail'))
                ledger = self.evaluate()
                self.assertEqual(ledger['obligations'][0]['status'], 'failed')
                self.assertEqual(ledger['obligations'][0]['authority_gaps'], [
                    'admitted_failed_proof', f"{label}:failed:{passing['evidence_ref']}",
                ])
                self.assertEqual(ledger['summary']['status'], 'incomplete')
                self.assertEqual(ledger['summary']['outstanding_obligation_ids'], [PROCESS])

    def test_many_admitted_failures_use_one_bounded_diagnostic(self):
        records = self.host.bundle['verifiers']
        passing = records[0]
        records.extend(
            dict(passing, evidence_ref=f'evidence:{REVIEW}:verifier-attempt-{attempt}', outcome='fail')
            for attempt in range(len(records), 512)
        )
        ledger = self.evaluate()
        self.assertEqual(ledger['obligations'][1]['status'], 'failed')
        self.assertEqual(ledger['obligations'][1]['authority_gaps'], ['admitted_failed_proof'])
        self.assertEqual(ledger['summary']['status'], 'incomplete')

    def test_unknown_cyclic_and_empty_reviews_cannot_report_complete(self):
        self.payload['reviewed_obligations'][0]['residual_unknown_ids'] = ['unknown:missing-observation']
        self.payload['reviewed_obligations'][1]['dependency_obligation_ids'] = [PROCESS]
        self.host.bundle['reviewed_obligations'] = copy.deepcopy(self.payload['reviewed_obligations'])
        report = self.evaluate()
        self.assertEqual(report['summary']['status'], 'incomplete')
        self.assertEqual([row['status'] for row in report['obligations']], ['open', 'unknown'])
        self.assertEqual(report['obligations'][0]['authority_gaps'], [f'dependency:{PROCESS}'])
        self.assertEqual(report['obligations'][1]['unknowns'], ['residual_unknown:unknown:missing-observation'])
        for key in ('reviewed_obligations', 'originals', 'verifiers', 'fixtures'):
            self.host.bundle[key] = []
        report = self.evaluate({**SUBJECT, 'reviewed_obligations': [], 'bindings': [], 'contradictions': []})
        self.assertEqual(report['obligations'], [])
        self.assertEqual(report['summary']['status'], 'unknown')

    def test_admission_prevents_foreign_evidence_and_review_inventory_downgrades(self):
        self.assertEqual(self.evaluate()['summary']['status'], 'complete')
        self.assertEqual(self.host.calls[-1][:3],
                         (SUBJECT['evidence_bundle_ref'], SUBJECT['boundary_id'], REQUEST_ID))
        reduced = copy.deepcopy(self.payload)
        reduced['reviewed_obligations'].pop()
        reduced['bindings'].pop()
        self.assertEqual(self.projector.invoke(ROUTE, self.args(reduced))['code'], 'SCHEMA_INVALID')
        injected = dict(self.payload, evidence=self.host.bundle)
        self.assertEqual(self.projector.invoke(ROUTE, self.args(injected))['code'], 'SCHEMA_INVALID')
        with patch.object(self.host, 'resolve_reconstruction_evidence', return_value=None) as resolve:
            response = self.projector.invoke(ROUTE, self.args())
            self.assertEqual((response['code'], response['result']), ('AVAILABILITY_PARKED', {}))
            preview = self.projector.invoke(ROUTE, self.args(dry_run=True))
            self.assertEqual(preview['result']['kind'], 'dry_run')
            resolve.assert_called_once()
        self.host = None
        self.assertEqual(self.projector.invoke(ROUTE, self.args())['code'], 'AVAILABILITY_PARKED')
        _, retained = fixture()
        self.host = FixtureHost(dict(retained, boundary_id='fixture:foreign'))
        self.assertEqual(self.projector.invoke(ROUTE, self.args())['code'], 'AVAILABILITY_PARKED')

    def test_tool_gate_shared_route_mcp_cli_and_json_carrier(self):
        row = next((item for item in self.bundle['router_rows'] if item['id'] == ROUTE), None)
        self.assertIsNotNone(row, 'The router must publish the host operation.')
        self.assertIsNone(row['manifest_digest'])
        self.assertEqual(row['required_effects'], [])
        self.assertEqual(row['perception']['required_level'], 'none')
        other_manifest = minimal_manifest('other-slot', registry=self.registry)
        other = self.generate(other_manifest, existing_rows=[row])
        self.assertIn(row, other['router_rows'])
        self.assertIn(MCP, {tool['name'] for tool in self.projector.list_tools()})
        self.assertEqual(row['cli_argv_shape'], ['slot', 'mini', TOOL, '--args', '<json>'])

        cli_command = ('slot', 'mini', TOOL)
        with patch.object(tools, 'run_bound', side_effect=AssertionError('unexpected launch')) as run:
            args = self.args()
            expected = assert_tool_gate(self, self.projector, TOOL, args, cli_command=cli_command, code='OK')
            self.assertEqual(self.make_projector(other_manifest, other).invoke(ROUTE, args), expected)
            self.assertEqual(tools.dispatch(TOOL, args, context=CONTEXT, host=self.host)['result'],
                             expected['result']['payload'])

            reduced = copy.deepcopy(self.payload)
            reduced['reviewed_obligations'].pop()
            reduced['bindings'].pop()
            for case, invalid in (
                ('reduced_inventory', self.args(reduced)),
                ('unknown_payload_key', self.args(dict(self.payload, evidence=self.host.bundle))),
            ):
                with self.subTest(case=case):
                    assert_tool_gate(self, self.projector, TOOL, invalid, cli_command=cli_command,
                                     code='SCHEMA_INVALID')
                    with self.assertRaises(PublicationError) as refusal:
                        tools.dispatch(TOOL, invalid, context=CONTEXT, host=self.host)
                    self.assertEqual(refusal.exception.code, 'SCHEMA_INVALID')

            parked = self.make_projector(self.manifest, self.bundle, dispatched=False)
            calls = list(self.host.calls)
            assert_tool_gate(self, parked, TOOL, args, cli_command=cli_command, code='AVAILABILITY_PARKED')
            self.assertEqual(self.host.calls, calls)

            _, retained = fixture()
            unresolved_host = FixtureHost(retained)
            with patch.object(unresolved_host, 'resolve_reconstruction_evidence', return_value=None):
                for case, host in (
                    ('unresolved_evidence', unresolved_host),
                    ('missing_host', None),
                    ('foreign_boundary', FixtureHost(dict(retained, boundary_id='fixture:foreign'))),
                ):
                    with self.subTest(case=case):
                        self.host = host
                        assert_tool_gate(self, self.projector, TOOL, args, cli_command=cli_command,
                                         code='AVAILABILITY_PARKED')
                        with self.assertRaises(ValueError) as refusal:
                            tools.dispatch(TOOL, args, context=CONTEXT, host=self.host)
                        self.assertEqual(refusal.exception.code, 'AVAILABILITY_PARKED')
            run.assert_not_called()

    def test_publication_fails_on_missing_faces_resources_and_collision(self):
        for face, key in (('router_rows', ROUTE), ('mcp_tools', MCP),
                          ('cli_shapes', ROUTE), ('json_contracts', ROUTE)):
            with self.subTest(face=face):
                publication = copy.deepcopy(self.bundle)
                if face == 'router_rows':
                    self.assertIn(key, {row['id'] for row in publication[face]})
                    publication[face] = [row for row in publication[face] if row['id'] != key]
                else:
                    self.assertIn(key, publication[face])
                    del publication[face][key]
                with self.assertRaises(PublicationError) as raised:
                    verify_publication(publication, self.manifest, policy=self.policy, registry=self.registry)
                self.assertEqual(raised.exception.code, 'MISSING_PROJECTION')
        with self.assertRaises(PublicationError) as raised:
            self.generate(self.manifest, existing_rows=[{'id': 'host.other', 'mcp_name': MCP}])
        self.assertEqual(raised.exception.code, 'COLLISION')
        del self.registry[tools.MINI_TOOLS[TOOL]['result_schema_ref']]
        with self.assertRaises(PublicationError) as raised:
            self.generate(self.manifest)
        self.assertEqual(raised.exception.code, 'MISSING_PROJECTION')
