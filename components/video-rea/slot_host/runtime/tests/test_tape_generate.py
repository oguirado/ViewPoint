# SPDX-License-Identifier: AGPL-3.0-only
"""Offline projection, tape custody and independent replay for tape.generate."""

import copy
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from slot_host.projections import PublicationError, SlotProjector, control_envelope, generate, verify_publication
from slot_host.projections.tests.test_projections import load_registry, minimal_manifest
from slot_host.runtime import tools
from slot_host.runtime.replay_lineage import ReplayLineageError, resolve_replay_lineage
from slot_host.runtime.tests._tool_gate import assert_tool_gate

_TOOL = 'tape.generate'
_ROUTE = 'host.tape.generate'
_MCP = 'rea_tape_generate'
_SCHEMA = 'urn:test:transition-input'
_WORKLOAD = 'fixture:transition-suite'
_CONTEXT = {'caller': 'agent', 'agent_instance': 'tape-fixture'}
_POLICY = {'perception_ceiling': 'none'}
_CLI = ['slot', 'mini', _TOOL, '--args', '<json>']
_COMPONENT = Path(__file__).resolve().parents[3]
_REPLAY = importlib.import_module('slot_host.adapters.rust-anything.fixture_observers').record_replay


def _encode(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _object(properties, required=None):
    return {'type': 'object', 'properties': properties, 'additionalProperties': False,
            'required': list(properties if required is None else required)}


class FixtureHost:
    def __init__(self):
        self.schemas = {_SCHEMA: {'$id': _SCHEMA, **_object({
            'event_id': {'const': 'press-1'}, 'action': {'type': 'string', 'enum': ['press', 'release']},
        })}}
        self.tapes = {}
        self.calls = []
        self.capture = None
        self.readback = lambda artifacts: artifacts

    def resolve_operation_schema(self, ref, *, request_id, max_bytes):
        self.calls.append(('schema', ref, request_id))
        return _encode(self.schemas[ref]) if ref in self.schemas else None

    def resolve_test_capture(self, ref, *, request_id, max_bytes):
        self.calls.append(('capture', ref, request_id))
        return _encode(self.capture) if ref == _WORKLOAD and self.capture is not None else None

    def retain_generated_tape(self, request_id, artifacts):
        self.calls.append(('retain', request_id))
        self.tapes.update(artifacts)
        return self.readback(artifacts)


class TapeGenerateTests(unittest.TestCase):
    def setUp(self):
        self.host = FixtureHost()
        self.registry = load_registry()
        for name in ('req', 'reply'):
            path = _COMPONENT.parent / 'vp-direct' / 'schemas' / f'vp.control.{name}_v1.schema.json'
            schema = json.loads(path.read_bytes())
            self.registry[schema['$id']] = schema
        self.registry.update(copy.deepcopy(tools.ARGUMENT_SCHEMAS))
        self.registry.update(copy.deepcopy(tools.RESULT_SCHEMAS))
        self.registry['host_mini_tools'] = tools.MINI_TOOLS
        self.manifest = minimal_manifest(registry=self.registry)
        self.bundle = self.publish()
        self.projector = self.make_projector()

    def publish(self, manifest=None, **options):
        return generate(manifest or self.manifest, policy=_POLICY, registry=self.registry, **options)

    def make_projector(self, *, dispatch=True):
        return SlotProjector(self.manifest, self.bundle, policy=_POLICY, registry=self.registry,
                             dispatch=self.dispatch if dispatch else None, context=_CONTEXT,
                             authenticated_context=_CONTEXT)

    def dispatch(self, envelope):
        return tools.handle_tape_control(self.projector.prepare(envelope, authenticated_context=_CONTEXT),
                                         host=self.host)

    def args(self, request_id='fixture-tape-001', **overrides):
        payload = {'operation_schema_ref': _SCHEMA, 'mode': 'synthetic', 'generation_budget': 1,
                   'seed': 0, **overrides}
        return {'request_id': request_id, 'payload': payload}

    def run_tape(self, args=None):
        reply = self.projector.invoke(_ROUTE, self.args() if args is None else args)
        self.assertEqual((reply['code'], reply['ok']), ('OK', True), reply)
        outcome = reply['result']['payload']
        tape = self.host.tapes[outcome['tape_ref']]
        log = self.host.tapes[outcome['generation_log_ref']]
        for ref, kind, data in ((outcome['tape_ref'], 'tape', tape),
                                (outcome['generation_log_ref'], 'log', log)):
            self.assertEqual(ref, f'fixture:tape.generate:{kind}:sha256:' + hashlib.sha256(data).hexdigest())
        return outcome, tape, json.loads(log)

    def test_tool_gate_four_faces_share_the_control_request(self):
        row = next((entry for entry in self.bundle['router_rows'] if entry['id'] == _ROUTE), None)
        self.assertIsNotNone(row, 'the router must publish the shared tape.generate operation')
        self.assertEqual((row['manifest_digest'], row['role'], row['required_effects']),
                         (None, None, []))
        self.assertEqual(row['perception']['required_level'], 'none')
        self.assertEqual(row['cli_argv_shape'], _CLI)
        self.assertEqual(row['mcp_name'], _MCP)
        self.assertEqual(tools.ROUTER_ENTRIES[_ROUTE]['cli_argv_shape'], _CLI)
        self.assertIn(_MCP, tools.MCP_TOOLS)
        self.assertIn(_MCP, {descriptor['name'] for descriptor in self.projector.list_tools()})
        other = minimal_manifest('another-slot', registry=self.registry)
        self.assertIn(row, self.publish(other, existing_rows=[row])['router_rows'])

        arguments = self.args()
        cli_command = _CLI[:-2]
        with patch.object(tools, 'run_bound', side_effect=AssertionError('unexpected external execution')) as launch:
            reply = assert_tool_gate(self, self.projector, _TOOL, arguments, cli_command=cli_command, code='OK')
            self.assertEqual(tools.dispatch(_TOOL, arguments, host=self.host)['result'], reply['result']['payload'])

            calls, tapes = list(self.host.calls), self.host.tapes.copy()
            assert_tool_gate(self, self.projector, _TOOL, self.args(generation_budget=0),
                             cli_command=cli_command, code='SCHEMA_INVALID')
            self.assertEqual(self.host.calls, calls)
            assert_tool_gate(self, self.projector, _TOOL, self.args(operation_schema_ref='urn:test:absent'),
                             cli_command=cli_command, code='AVAILABILITY_PARKED')
            self.assertEqual(self.host.tapes, tapes)
            launch.assert_not_called()

    def test_tool_gate_refuses_collisions_missing_faces_and_schema_drift(self):
        self.assertIn(_ROUTE, self.bundle['cli_shapes'])
        for field, key in (('mcp_tools', _MCP), ('cli_shapes', _ROUTE), ('json_contracts', _ROUTE),
                           ('router_rows', _ROUTE)):
            with self.subTest(field=field):
                broken = copy.deepcopy(self.bundle)
                if field == 'router_rows':
                    broken[field] = [row for row in broken[field] if row['id'] != key]
                else:
                    del broken[field][key]
                with self.assertRaises(PublicationError) as error:
                    verify_publication(broken, self.manifest, policy=_POLICY, registry=self.registry)
                self.assertEqual(error.exception.code, 'MISSING_PROJECTION')

        row = next(row for row in self.bundle['router_rows'] if row['id'] == _ROUTE)
        collisions = [
            {'id': 'host.other', 'mcp_name': row['mcp_name']},
            {'id': 'host.other', 'cli_argv_shape': row['cli_argv_shape']},
            {'id': 'host.other', 'input_schema_ref': row['input_schema_ref']},
            {'id': _ROUTE, 'mcp_name': 'rea_other'},
        ]
        for collision in collisions:
            with self.subTest(collision=collision):
                with self.assertRaises(PublicationError) as error:
                    self.publish(existing_rows=[collision])
                self.assertEqual(error.exception.code, 'COLLISION')

        ref = row['json_contract_ref']['result_schema_ref']
        schema = self.registry.pop(ref)
        with self.assertRaises(PublicationError) as error:
            self.publish()
        self.assertEqual(error.exception.code, 'MISSING_PROJECTION')
        self.registry[ref] = {**schema, 'title': 'changed result contract'}
        with self.assertRaises(PublicationError) as error:
            self.publish()
        self.assertEqual(error.exception.code, 'SCHEMA_INVALID')

    def test_synthetic_same_input_repeats_and_changed_seed_is_consumed(self):
        nominal, first, log = self.run_tape()
        repeated = self.run_tape(self.args('fixture-tape-repeat'))
        self.assertEqual((nominal, first, log), repeated)
        self.assertEqual(log['operation_schema_sha256'], hashlib.sha256(_encode(self.host.schemas[_SCHEMA])).hexdigest())
        self.assertEqual(log['origin_by_case'], {'case-1': {'mode': 'synthetic'}})
        changed, second, changed_log = self.run_tape(self.args('fixture-tape-other', seed=1))
        self.assertNotEqual(nominal['tape_ref'], changed['tape_ref'])
        self.assertEqual(changed_log['request']['seed'], 1)

        # Precomputed transitions from the accepted press oracle, plus a
        # host-authored release control. Only the consumed tape varies.
        case_path = _COMPONENT / 'contracts/oracle/cases/outside-record-replay-compare/nominal.json'
        fixture = json.loads(case_path.read_bytes())['fixture']
        fixture['original']['transition_returns']['release'] = 'off'
        fixture['candidate']['transition_returns']['release'] = 'on'
        host = SimpleNamespace(manifest=self.manifest)
        for raw, action, original, oracle_code, verdict in (
                (first, 'press', 'on', 'ORACLE_PASS', 'match'),
                (second, 'release', 'off', 'ORACLE_BEHAVIOUR_MISMATCH', 'mismatch')):
            with self.subTest(action=action):
                tape = json.loads(raw)
                self.assertEqual(tape, [{'event_id': 'press-1', 'action': action}])
                case = {**fixture, 'input_tape': tape, 'host_oracle': {**fixture['host_oracle'], 'return_value': original}}
                code, observation = _REPLAY(case, host)
                self.assertEqual((code, observation['verdict'], observation['original_return'],
                                  observation['candidate_return']), (oracle_code, verdict, original, 'on'))
                self.assertEqual(observation['activated_event_ids'], ['press-1'])
                if verdict == 'mismatch':
                    self.assertEqual(observation['first_divergence'], {'event_id': 'press-1'})

        parent = 'fixture:parent'
        accepted_digest = hashlib.sha256(first).hexdigest()
        retained = {(parent, accepted_digest): {'parent_episode_id': parent, 'input_tape_sha256': accepted_digest}}
        resolver = lambda episode, tape_sha: retained.get((episode, tape_sha))
        self.assertEqual(resolve_replay_lineage({'parent_episode_id': parent, 'tape_sha256': accepted_digest},
                                               tape_resolver=resolver)['tape_sha256'], accepted_digest)
        with self.assertRaises(ReplayLineageError):
            resolve_replay_lineage({'parent_episode_id': parent, 'tape_sha256': hashlib.sha256(second).hexdigest()},
                                   tape_resolver=resolver)

    def test_synthetic_cases_obey_types_boundaries_salt_and_budget(self):
        schema_ref = 'urn:test:typed-edge-input'
        self.host.schemas[schema_ref] = {'$id': schema_ref, **_object({
            'index': {'type': 'integer', 'minimum': -3, 'maximum': 9},
            'name': {'type': 'string', 'minLength': 0, 'maxLength': 3},
            'flags': {'type': 'array', 'minItems': 0, 'maxItems': 2, 'items': {'type': 'boolean'}},
            'optional': {'type': 'null'},
        }, ('index', 'name', 'flags'))}
        args = self.args(operation_schema_ref=schema_ref, generation_budget=24)
        result, raw, _ = self.run_tape(args)
        cases = json.loads(raw)
        self.assertEqual(len(cases), 24)
        self.assertEqual(len(set(map(_encode, cases))), 24)
        self.assertEqual(len(result['origin_by_case']), 24)
        self.assertEqual({min(row['index'] for row in cases), max(row['index'] for row in cases)}, {-3, 9})
        self.assertEqual({len(row['flags']) for row in cases}, {0, 1, 2})
        self.assertEqual({len(row['name']) for row in cases}, {0, 1, 3})
        self.assertEqual({'optional' in row for row in cases}, {False, True})
        for case in cases:
            self.assertIs(type(case['index']), int)
            self.assertIs(type(case['name']), str)
            self.assertTrue(all(type(flag) is bool for flag in case['flags']))
            self.assertIsNone(case.get('optional'))
        salted = self.args(operation_schema_ref=schema_ref, generation_budget=24, salt='new-edges')
        self.assertNotEqual(self.run_tape(salted)[1], raw)
        self.assertEqual(self.run_tape(salted), self.run_tape(salted))

    def test_from_tests_uses_authorized_capture_order_and_provenance(self):
        press = {'event_id': 'press-1', 'action': 'press'}
        release = {'event_id': 'press-1', 'action': 'release'}
        self.host.capture = {'schema': 'rea.fixture_test_capture/v1', 'workload_ref': _WORKLOAD,
                             'operation_schema_ref': _SCHEMA,
                             'operation_schema_sha256': hashlib.sha256(_encode(self.host.schemas[_SCHEMA])).hexdigest(),
                             'calls': [{'export': 'button', 'arguments': row} for row in (release, release, press)],
                             'skipped_exports': ['unsupported']}
        args = self.args(mode='from-tests', admitted_test_workload_ref=_WORKLOAD, generation_budget=2, seed=42)
        result, tape, log = self.run_tape(args)
        self.assertEqual(json.loads(tape), [release, press])
        self.assertEqual(result['skipped_exports'], ['unsupported'])
        self.assertEqual(result['origin_by_case'], {
            'case-1': {'mode': 'from-tests', 'export': 'button', 'capture_index': 1},
            'case-2': {'mode': 'from-tests', 'export': 'button', 'capture_index': 3},
        })
        self.assertEqual(log['capture_sha256'], hashlib.sha256(_encode(self.host.capture)).hexdigest())
        self.assertEqual(log['tape_ref'], result['tape_ref'])
        self.assertEqual(log['request'], args['payload'])
        self.assertEqual(self.run_tape(args), (result, tape, log))
        args['payload']['generation_budget'] = 1
        self.assertEqual(json.loads(self.run_tape(args)[1]), [release])
        self.host.capture['calls'][-1]['arguments'] = {'event_id': 'press-1', 'action': 42}
        self.assertEqual(self.projector.invoke(_ROUTE, args)['code'], 'SCHEMA_INVALID')
        self.host.capture['operation_schema_sha256'] = 'a' * 64
        self.assertEqual(self.projector.invoke(_ROUTE, args)['code'], 'AVAILABILITY_PARKED')

    def test_closed_requests_and_preview_have_no_fixture_effects(self):
        args = self.args()
        preview = self.projector.invoke(_ROUTE, {**args, 'dry_run': True})
        self.assertEqual((preview['code'], preview['result'].get('kind')), ('OK', 'dry_run'))
        self.assertEqual(self.host.calls, [])
        for changes in ({'generation_budget': 0}, {'generation_budget': 65}, {'seed': True}, {'mode': 'from-tests'},
                        {'admitted_test_workload_ref': _WORKLOAD}, {'command': ['no-execution']},
                        {'operation_schema_ref': '/non-opaque-reference'}):
            with self.subTest(changes=changes):
                reply = self.projector.invoke(_ROUTE, self.args(**changes))
                self.assertEqual((reply['code'], reply['result']), ('SCHEMA_INVALID', {}))
        self.assertEqual(self.host.calls, [])

    def test_missing_host_sources_or_verified_readback_do_not_publish_refs(self):
        args = self.args()
        envelope = control_envelope(self.manifest, _ROUTE, args, context=_CONTEXT)
        parked = self.make_projector(dispatch=False).handle_envelope(envelope)
        # The configured fixture path must be actually callable before the
        # absence of a dispatcher can serve as a negative control.
        self.run_tape(args)
        self.assertEqual((parked['code'], parked['result']), ('AVAILABILITY_PARKED', {}))
        for invalid in (self.args(operation_schema_ref='urn:test:absent'),
                        self.args(mode='from-tests', admitted_test_workload_ref=_WORKLOAD)):
            reply = self.projector.invoke(_ROUTE, invalid)
            self.assertEqual((reply['code'], reply['result']), ('AVAILABILITY_PARKED', {}))
        with patch.object(self.host, 'resolve_operation_schema', return_value=b' ' * 16385):
            self.assertEqual(self.projector.invoke(_ROUTE, args)['code'], 'AVAILABILITY_PARKED')
        schema = self.host.schemas[_SCHEMA]
        schema['properties']['action']['pattern'] = 'press'
        self.assertEqual(self.projector.invoke(_ROUTE, args)['code'], 'SCHEMA_INVALID')
        del schema['properties']['action']['pattern']
        for readback in (lambda artifacts: {}, lambda artifacts: {ref: value + b' ' for ref, value in artifacts.items()}):
            self.host.readback = readback
            reply = self.projector.invoke(_ROUTE, args)
            self.assertEqual((reply['code'], reply['result']), ('AVAILABILITY_PARKED', {}))
        self.host = None
        self.assertEqual(self.projector.invoke(_ROUTE, args)['code'], 'AVAILABILITY_PARKED')


if __name__ == '__main__':
    unittest.main()
