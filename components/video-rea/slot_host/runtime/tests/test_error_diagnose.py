# SPDX-License-Identifier: AGPL-3.0-only
"""Offline diagnostics through the registered projector and pinned carriers."""

import copy
import json
import signal
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.projections import PublicationError, SlotProjector, generate, verify_publication
from slot_host.projections.schema import digest
from slot_host.projections.tests.test_projections import load_registry, minimal_manifest
from slot_host.runtime import tools
from slot_host.runtime.tests._tool_gate import assert_tool_gate


TOOL = 'error.diagnose'
ROUTE = 'host.error.diagnose'
MCP = 'rea_error_diagnose'
ATTEMPT = {'operation': 'compare', 'attempt_ref': 'attempt:diagnostic-fixture'}
CONTEXT = {'caller': 'agent', 'agent_instance': 'diagnostic-fixture'}


def failure(source, code, **extra):
    return {'source': source, 'code': code, 'evidence_ref': 'evidence:failure', **extra}


class ErrorDiagnoseTests(unittest.TestCase):
    def setUp(self):
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
        self.bundle = self.generate()
        self.projector = SlotProjector(self.manifest, self.bundle, policy=self.policy, registry=self.registry,
                                       dispatch=self.dispatch, context=CONTEXT, authenticated_context=CONTEXT)

    def generate(self, manifest=None, **options):
        return generate(self.manifest if manifest is None else manifest, policy=self.policy,
                        registry=self.registry, **options)

    def dispatch(self, envelope):
        normalized = self.projector.prepare(envelope, authenticated_context=CONTEXT)
        return tools.handle_diagnostic_control(normalized)

    def args(self, **evidence):
        return {'request_id': 'fixture-diagnosis', 'payload': dict(ATTEMPT, **evidence)}

    def diagnose(self, **evidence):
        reply = self.projector.invoke(ROUTE, self.args(**evidence))
        self.assertEqual(reply['code'], 'OK', reply)
        return reply['result']['payload']

    def test_held_back_incomplete_provider_receipt(self):
        # Redacted known failure from the held-back swarm receipt. E_TARGET
        # alone does not establish that the target ran or caused the error.
        result = self.diagnose(provider_error=failure('provider', 'E_TARGET',
                                                       cause='claude_api_incomplete_response'),
                               exit_record={'evidence_ref': 'evidence:launcher-exit', 'rc': 8})
        self.assertEqual((result['code'], result['category'], result['retryable']),
                         ('PROVIDER_INCOMPLETE_RESPONSE', 'provider_failure', None))
        self.assertIsNone(result['slot_result_ref'])
        self.assertEqual(result['evidence_refs'], ['evidence:failure', 'evidence:launcher-exit'])
        self.assertEqual(self.diagnose(provider_error=failure('provider', 'E_TARGET'))['code'], 'UNKNOWN')

    def test_typed_refusal_adapter_failure_and_target_observation(self):
        cases = [
            (failure('control', 'PERCEPTION_DENIED'), 'PERCEPTION_DENIED', 'control_refusal', None),
            (failure('provider', 'internal_error', cause='clean_room'),
             'PROVIDER_REFUSED', 'provider_refusal', None),
            (failure('adapter', 'exchange_invalid'), 'exchange_invalid', 'adapter_failure', None),
            (failure('target', 'E_INPUT', slot_result_ref='result:target'), 'E_INPUT', 'target_error', 'result:target'),
        ]
        for evidence, code, category, reference in cases:
            with self.subTest(code=code):
                result = self.diagnose(provider_error=evidence)
                self.assertEqual((result['code'], result['category'], result['slot_result_ref']),
                                 (code, category, reference))
                self.assertIs(result['retryable'], False)

    def test_crash_requires_exit_evidence_and_uncertainty_blocks_retry(self):
        exit_record = {'rc': -signal.SIGSEGV, 'timed_out': False, 'overflow': False,
                       'evidence_ref': 'evidence:observed-exit'}
        crash = self.diagnose(exit_record=exit_record)
        self.assertEqual((crash['code'], crash['category'], crash['retryable']),
                         ('PROCESS_CRASH', 'process_crash', False))
        self.assertEqual(self.diagnose(exit_record=dict(exit_record, rc=-signal.SIGKILL))['code'], 'UNKNOWN')
        self.assertEqual(self.diagnose(exit_record=dict(exit_record, rc=128 + signal.SIGSEGV))['code'], 'UNKNOWN')
        self.assertEqual(self.diagnose(exit_record={'rc': -signal.SIGSEGV,
                                                    'evidence_ref': 'evidence:unqualified'})['code'], 'UNKNOWN')
        interrupted = self.diagnose(exit_record=dict(exit_record, timed_out=True))
        self.assertEqual((interrupted['code'], interrupted['retryable'], interrupted['recovery']),
                         ('UNCERTAIN_OUTCOME', False, 'reconcile_prior_attempt'))

    def test_insufficient_and_conflicting_evidence_are_explicit(self):
        empty = self.diagnose()
        self.assertEqual((empty['code'], empty['retryable'], empty['unknowns']),
                         ('UNKNOWN', None, ['missing_failure_evidence']))
        target = failure('target', 'E_INPUT', slot_result_ref='result:target')
        self.assertIsNone(self.diagnose(provider_error=failure('target', 'E_INPUT'))['slot_result_ref'])
        result = self.diagnose(provider_error=target, exit_record={
            'rc': -signal.SIGSEGV, 'timed_out': False, 'overflow': False, 'evidence_ref': 'evidence:exit',
        })
        self.assertEqual((result['code'], result['category'], result['retryable'], result['slot_result_ref']),
                         ('AMBIGUOUS', 'ambiguous', False, None))
        self.assertIn('conflicting_diagnostics', result['unknowns'])

    def test_target_result_requires_a_compatible_paired_exit(self):
        target = failure('target', 'E_INPUT', slot_result_ref='result:target')
        observed = {'evidence_ref': 'evidence:exit'}
        for rc in (2, 3, 4, 5, 6):
            with self.subTest(paired_rc=rc):
                result = self.diagnose(provider_error=target, exit_record=dict(observed, rc=rc, control_outcome='OK'))
                self.assertEqual((result['code'], result['category'], result['slot_result_ref']),
                                 ('E_INPUT', 'target_error', 'result:target'))
        for facts in (
            {'rc': -signal.SIGKILL}, {'rc': -signal.SIGSEGV}, {'rc': 128 + signal.SIGSEGV},
            {'rc': 0}, {'rc': 1}, {'rc': 2.0}, {'rc': None}, {},
        ):
            with self.subTest(exit_facts=facts):
                result = self.diagnose(provider_error=target, exit_record=dict(observed, **facts))
                self.assertEqual((result['code'], result['category'], result['retryable'], result['slot_result_ref']),
                                 ('AMBIGUOUS', 'ambiguous', False, None))
                self.assertEqual(result['evidence_refs'], ['evidence:failure', 'evidence:exit'])
                self.assertIn('conflicting_diagnostics', result['unknowns'])

    def test_unrecognized_control_outcome_with_zero_exit_is_explicit(self):
        result = self.diagnose(exit_record={'evidence_ref': 'evidence:exit', 'rc': 0,
                                            'control_outcome': 'FUTURE_HOLD'})
        self.assertEqual((result['code'], result['category'], result['retryable'], result['slot_result_ref']),
                         ('UNKNOWN', 'unknown', None, None))
        self.assertEqual(result['evidence_refs'], ['evidence:exit'])
        self.assertEqual(result['unknowns'], ['unrecognized_control_outcome'])

    def test_unrecognized_control_outcome_conflicts_with_target_observation(self):
        target = failure('target', 'E_INPUT', slot_result_ref='result:target')
        for rc in (0, 2):
            with self.subTest(rc=rc):
                result = self.diagnose(provider_error=target, exit_record={
                    'evidence_ref': 'evidence:exit', 'rc': rc, 'control_outcome': 'FUTURE_HOLD',
                })
                self.assertEqual((result['code'], result['category'], result['retryable'], result['slot_result_ref']),
                                 ('AMBIGUOUS', 'ambiguous', False, None))
                self.assertIn('unrecognized_control_outcome', result['unknowns'])
                self.assertIn('conflicting_diagnostics', result['unknowns'])

    def test_unrecognized_reported_failure_from_a_conflicting_plane_is_ambiguous(self):
        cases = (
            (failure('control', 'FUTURE_HOLD'),
             {'rc': -signal.SIGSEGV, 'timed_out': False, 'overflow': False}),
            (failure('provider', 'FUTURE_PROVIDER_FAILURE'), {'rc': 0, 'control_outcome': 'HOLD'}),
        )
        for reported, facts in cases:
            with self.subTest(source=reported['source']):
                result = self.diagnose(provider_error=reported, exit_record={'evidence_ref': 'evidence:exit', **facts})
                self.assertEqual((result['code'], result['category'], result['retryable'], result['slot_result_ref']),
                                 ('AMBIGUOUS', 'ambiguous', False, None))
                self.assertEqual(result['evidence_refs'], ['evidence:failure', 'evidence:exit'])
                self.assertEqual(result['unknowns'], ['unrecognized_reported_failure', 'conflicting_diagnostics'])

    def test_unrecognized_control_outcome_conflicts_with_timeout_and_preserves_recovery(self):
        result = self.diagnose(exit_record={'evidence_ref': 'evidence:exit', 'timed_out': True,
                                            'control_outcome': 'FUTURE_HOLD'})
        self.assertEqual((result['code'], result['retryable'], result['slot_result_ref'], result['recovery']),
                         ('AMBIGUOUS', False, None, 'reconcile_prior_attempt'))
        self.assertEqual(result['unknowns'], ['unrecognized_control_outcome', 'conflicting_diagnostics'])

    def test_tool_gate_shared_four_face_parity_and_parked_execution(self):
        args = self.args(provider_error=failure('adapter', 'exchange_invalid'))
        row = next((row for row in self.bundle['router_rows'] if row['id'] == ROUTE), None)
        self.assertIsNotNone(row, 'the router must publish the host operation')
        self.assertEqual(row['required_effects'], [])
        self.assertEqual(row['perception']['required_level'], 'none')
        self.assertIsNone(row['manifest_digest'])
        second = self.generate(minimal_manifest('other-slot', registry=self.registry), existing_rows=[row])
        self.assertIn(row, second['router_rows'])
        self.assertIn(MCP, {tool['name'] for tool in self.projector.list_tools()})
        cli_command = ('slot', 'mini', TOOL)
        with patch.object(tools, 'run_bound', side_effect=AssertionError('unexpected execution')) as run:
            assert_tool_gate(self, self.projector, TOOL, args, cli_command=cli_command, code='OK')
            parked = SlotProjector(self.manifest, self.bundle, policy=self.policy, registry=self.registry,
                                   context=CONTEXT, authenticated_context=CONTEXT)
            assert_tool_gate(self, parked, TOOL, args, cli_command=cli_command, code='AVAILABILITY_PARKED')
            preview = self.projector.invoke(ROUTE, dict(args, dry_run=True))
            self.assertEqual(preview['result']['kind'], 'dry_run')
            invalid = {
                'boolean_exit_code': self.args(exit_record={'rc': True}),
                'unredacted_output': self.args(provider_error={
                    **failure('adapter', 'exchange_invalid'), 'raw_output': 'unredacted text',
                }),
            }
            for case, arguments in invalid.items():
                with self.subTest(case=case):
                    assert_tool_gate(self, self.projector, TOOL, arguments,
                                     cli_command=cli_command, code='SCHEMA_INVALID')
            run.assert_not_called()

    def test_publication_rejects_each_missing_face_and_collision(self):
        for face, key in (('router_rows', ROUTE), ('cli_shapes', ROUTE),
                          ('mcp_tools', MCP), ('json_contracts', ROUTE)):
            with self.subTest(face=face):
                bundle = copy.deepcopy(self.bundle)
                if face == 'router_rows':
                    bundle[face] = [row for row in bundle[face] if row['id'] != key]
                else:
                    bundle[face].pop(key, None)
                with self.assertRaises(PublicationError) as raised:
                    verify_publication(bundle, self.manifest, policy=self.policy, registry=self.registry)
                self.assertEqual(raised.exception.code, 'MISSING_PROJECTION')
        with self.assertRaises(PublicationError) as raised:
            self.generate(existing_rows=[{'id': 'host.other', 'mcp_name': MCP}])
        self.assertEqual(raised.exception.code, 'COLLISION')
        del self.registry[tools.MINI_TOOLS[TOOL]['result_schema_ref']]
        with self.assertRaises(PublicationError) as raised:
            self.generate()
        self.assertEqual(raised.exception.code, 'MISSING_PROJECTION')

    def test_publication_rejects_each_tampered_diagnostic_schema(self):
        descriptor = tools.MINI_TOOLS[TOOL]
        for key, change in (
            ('argument_schema_ref', {'description': 'Changed without updating the pin.'}),
            ('result_schema_ref', {'additionalProperties': True}),
        ):
            with self.subTest(schema=key):
                ref = descriptor[key]
                original = self.registry[ref]
                with patch.dict(self.registry, {ref: dict(original, **change)}):
                    with self.assertRaises(PublicationError) as raised:
                        self.generate()
                    self.assertEqual(raised.exception.code, 'SCHEMA_INVALID')
                self.assertEqual(descriptor[key + '_sha256'], digest(original))

    def test_publication_rejects_missing_or_null_registered_mini_tool_pins(self):
        for name in ('effect.isolate', 'error.diagnose', 'journal.verify', 'reconstruction.obligations', 'tape.generate'):
            for key in ('argument_schema_ref_sha256', 'result_schema_ref_sha256'):
                for present in (False, True):
                    with self.subTest(tool=name, pin=key, null_pin=present):
                        descriptors = copy.deepcopy(tools.MINI_TOOLS)
                        if present:
                            descriptors[name][key] = None
                        else:
                            descriptors[name].pop(key, None)
                        with self.assertRaises(PublicationError) as raised:
                            self.generate(host_mini_tools=descriptors)
                        self.assertEqual(raised.exception.code, 'SCHEMA_INVALID')
