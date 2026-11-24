# SPDX-License-Identifier: AGPL-3.0-only
"""Witness admitted fixture effects and the absence of denied effects."""

import copy
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from slot_host.compare import behaviour
from slot_host.projections import PublicationError, SlotProjector, control_envelope, generate, verify_publication
from slot_host.projections.schema import digest
from slot_host.projections.tests.test_projections import load_registry, minimal_manifest
from slot_host.runtime import process_lifecycle, tools
from slot_host.runtime.tests._tool_gate import assert_tool_gate

_TOOL = 'effect.isolate'
_ROUTE = 'host.effect.isolate'
_MCP = 'rea_effect_isolate'
_CLI = ['slot', 'mini', _TOOL, '--args', '<json>']
_CONTEXT = {'caller': 'agent', 'agent_instance': 'effect-fixture'}
_POLICY = {'perception_ceiling': 'none'}
_REQUEST = {
    'run_ref': 'fixture:paired-file-writes',
    'admitted_sink_profile': 'fixture:local-file-sink',
    'initial_state_ref': 'fixture:initial-file-state',
    'reset_policy': 'fresh_per_side',
}
_COMPONENT = Path(__file__).resolve().parents[3]


class FixtureHost:
    def __init__(self):
        self.plan = {
            'schema': 'rea.effect_fixture_plan/v1', 'request': dict(_REQUEST), 'initial_value': 'seed',
            'original': [{'target': 'allowed', 'value': 'observed'}, {'target': 'prohibited', 'value': 'blocked'}],
            'candidate': [{'target': 'prohibited', 'value': 'blocked'}],
        }
        self.resolutions = []
        self.retained = {}
        self.readback = lambda artifacts: artifacts

    def resolve_effect_fixture(self, request, *, request_id, max_bytes):
        self.resolutions.append((request, request_id, max_bytes))
        return None if self.plan is None else json.dumps(self.plan, sort_keys=True).encode()

    def retain_effect_isolation(self, request_id, artifacts):
        self.retained[request_id] = dict(artifacts)
        return self.readback(artifacts)


class EffectIsolateTests(unittest.TestCase):
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

    def publish(self, manifest=None, **kwargs):
        return generate(manifest or self.manifest, policy=_POLICY, registry=self.registry, **kwargs)

    def make_projector(self, *, dispatch=True):
        return SlotProjector(self.manifest, self.bundle, policy=_POLICY, registry=self.registry,
                             context=_CONTEXT, authenticated_context=_CONTEXT,
                             dispatch=self.dispatch if dispatch else None)

    def dispatch(self, envelope):
        normalized = self.projector.prepare(envelope, authenticated_context=_CONTEXT)
        return tools.handle_effect_control(normalized, host=self.host)

    def args(self, request_id='fixture-effect-001', **changes):
        return {'request_id': request_id, 'payload': {**_REQUEST, **changes}}

    def run_effect(self, args=None):
        reply = self.projector.invoke(_ROUTE, self.args() if args is None else args)
        self.assertEqual((reply['code'], reply['ok']), ('OK', True), reply)
        return reply['result']['payload']

    def test_tool_gate_four_faces_share_one_control_request(self):
        row = next((entry for entry in self.bundle['router_rows'] if entry['id'] == _ROUTE), None)
        self.assertIsNotNone(row, 'the projector must publish the shared effect.isolate route')
        self.assertEqual((row['manifest_digest'], row['role'], row['perception']['required_level']),
                         (None, None, 'none'))
        self.assertEqual((row['cli_argv_shape'], row['mcp_name']), (_CLI, _MCP))
        self.assertEqual(tools.ROUTER_ENTRIES[_ROUTE]['cli_argv_shape'], _CLI)
        self.assertIn(_MCP, tools.MCP_TOOLS)
        self.assertIn(_MCP, {entry['name'] for entry in self.projector.list_tools()})
        other = minimal_manifest('another-slot', registry=self.registry)
        self.assertIn(row, self.publish(other, existing_rows=[row])['router_rows'])

        args = self.args()
        cli_command = _CLI[:-2]
        with patch.object(tools, 'run_bound') as launch, patch.object(process_lifecycle.subprocess, 'Popen') as spawn:
            reply = assert_tool_gate(self, self.projector, _TOOL, args, cli_command=cli_command, code='OK')
            self.assertEqual(tools.dispatch(_TOOL, args, host=self.host)['result'], reply['result']['payload'])

            resolutions, retained = list(self.host.resolutions), self.host.retained.copy()
            with patch.object(process_lifecycle, 'create_run_scratch') as create:
                assert_tool_gate(self, self.projector, _TOOL, self.args(reset_policy='reuse'),
                                 cli_command=cli_command, code='SCHEMA_INVALID')
                self.assertEqual(self.host.resolutions, resolutions)
                assert_tool_gate(self, self.projector, _TOOL, self.args(run_ref='fixture:foreign'),
                                 cli_command=cli_command, code='AVAILABILITY_PARKED')
                create.assert_not_called()
            self.assertEqual(self.host.retained, retained)
            launch.assert_not_called()
            spawn.assert_not_called()

    def test_tool_gate_rejects_collisions_missing_faces_and_schema_drift(self):
        self.assertIn(_ROUTE, self.bundle['cli_shapes'])
        for face, key in (('router_rows', _ROUTE), ('mcp_tools', _MCP),
                          ('cli_shapes', _ROUTE), ('json_contracts', _ROUTE)):
            with self.subTest(face=face):
                broken = copy.deepcopy(self.bundle)
                if face == 'router_rows':
                    broken[face] = [row for row in broken[face] if row['id'] != key]
                else:
                    del broken[face][key]
                with self.assertRaises(PublicationError) as error:
                    verify_publication(broken, self.manifest, policy=_POLICY, registry=self.registry)
                self.assertEqual(error.exception.code, 'MISSING_PROJECTION')
        row = next(row for row in self.bundle['router_rows'] if row['id'] == _ROUTE)
        for field in ('id', 'mcp_name', 'cli_argv_shape', 'input_schema_ref'):
            with self.subTest(collision=field):
                collision = {'id': 'host.other', field: row[field]}
                with self.assertRaises(PublicationError) as error:
                    self.publish(existing_rows=[collision])
                self.assertEqual(error.exception.code, 'COLLISION')
        ref = row['json_contract_ref']['result_schema_ref']
        self.registry[ref]['properties']['fixture_ref'] = {'type': 'string'}
        with self.assertRaises(PublicationError):
            self.publish()

    def test_fixture_permits_the_local_write_and_leaves_the_prohibited_path_absent(self):
        removed = []
        destroy = process_lifecycle.destroy_run_scratch

        def witness_then_destroy(path, created):
            with (path / 'state').open('rb') as stream:
                observed = stream.read(64)
            names = sorted(entry.name for entry in path.iterdir())
            outcome = destroy(path, created)
            removed.append((path, observed, names, not path.exists()))
            return outcome

        args = self.args()
        with patch.object(process_lifecycle, 'destroy_run_scratch', side_effect=witness_then_destroy):
            result = self.run_effect(args)
        self.assertEqual([(data, names, absent) for _, data, names, absent in removed],
                         [(b'observed', ['state'], True), (b'seed', ['state'], True)])
        self.assertEqual(len({path for path, *_ in removed}), 2)
        self.assertIn('network', result['unobserved_channels'])
        self.assertIn('external_execution', result['unavailable_controls'])
        artifacts = self.host.retained[args['request_id']]
        self.assertEqual(set(artifacts), {result['fixture_ref'], *result['captured_effect_refs'],
                                          *result['reset_receipt_refs']})
        for ref, raw in artifacts.items():
            self.assertEqual(ref.rsplit(':', 1)[-1], sha256(raw).hexdigest())
        fixture = json.loads(artifacts[result['fixture_ref']])
        self.assertEqual(fixture['request'], args['payload'])
        captures = [json.loads(artifacts[ref]) for ref in result['captured_effect_refs']]
        resets = [json.loads(artifacts[ref]) for ref in result['reset_receipt_refs']]
        self.assertEqual([row['side'] for row in captures], ['original', 'candidate'])
        self.assertEqual([row['side'] for row in resets], ['original', 'candidate'])
        for row in resets:
            self.assertEqual(row['initial_state_sha256'], sha256(b'seed').hexdigest())
            self.assertIs(row['scratch_removed'], True)
        original, candidate = [row['cases'][0]['observation']['payload'] for row in captures]
        denied = {'target': 'prohibited', 'decision': 'denied', 'present': False}
        self.assertEqual(original['writes'], [
            {'target': 'allowed', 'decision': 'permitted', 'present': True,
             'observed_sha256': sha256(b'observed').hexdigest()}, denied])
        self.assertEqual(candidate['writes'], [denied])
        self.assertIs(original['prohibited_present'], False)
        self.assertIs(candidate['prohibited_present'], False)

    def test_shared_comparator_consumes_retained_match_and_activated_mismatch(self):
        original_only = self.host.plan['candidate']
        self.host.plan['candidate'] = list(self.host.plan['original'])
        policy = {'id': 'effect-fixture', 'version': '1', 'sha256': digest({'check': 'structured'})}
        for expected in ('match', 'mismatch'):
            with self.subTest(expected=expected):
                args = self.args(f'fixture-effect-{expected}')
                result = self.run_effect(args)
                artifacts = self.host.retained[args['request_id']]
                original, candidate = [json.loads(artifacts[ref]) for ref in result['captured_effect_refs']]
                report = behaviour.compare(original['cases'], candidate['cases'], policy=policy,
                                           scope='fixture-local-effects', waivers=[])
                self.assertEqual(report['result'], expected)
            self.host.plan['candidate'] = original_only

    def test_preview_invalid_inputs_and_foreign_scope_leave_no_scratch(self):
        with patch.object(process_lifecycle, 'create_run_scratch') as create:
            preview = self.projector.invoke(_ROUTE, {**self.args(), 'dry_run': True})
            self.assertEqual((preview['code'], preview['result'].get('kind')), ('OK', 'dry_run'))
            self.assertEqual(self.host.resolutions, [])
            for changes in ({'run_ref': '/non-opaque/path'}, {'external_command': ['never-run']},
                            {'reset_policy': 'reuse'}):
                reply = self.projector.invoke(_ROUTE, self.args(**changes))
                self.assertEqual((reply['code'], reply['result']), ('SCHEMA_INVALID', {}))
            self.assertEqual(self.host.resolutions, [])
            for field in ('run_ref', 'admitted_sink_profile', 'initial_state_ref'):
                with self.subTest(foreign=field):
                    reply = self.projector.invoke(_ROUTE, self.args(**{field: 'fixture:foreign'}))
                    self.assertEqual((reply['code'], reply['result']), ('AVAILABILITY_PARKED', {}))
            self.assertEqual(len(self.host.resolutions), 3)
            create.assert_not_called()
        self.assertEqual(self.host.retained, {})

    def test_missing_authority_cleanup_or_readback_cannot_publish_effect_evidence(self):
        self.run_effect()
        args = self.args('fixture-effect-refusal')
        envelope = control_envelope(self.manifest, _ROUTE, args, context=_CONTEXT)
        parked = self.make_projector(dispatch=False).handle_envelope(envelope)
        self.assertEqual((parked['code'], parked['result']), ('AVAILABILITY_PARKED', {}))
        host, self.host = self.host, None
        reply = self.projector.invoke(_ROUTE, args)
        self.assertEqual((reply['code'], reply['result']), ('AVAILABILITY_PARKED', {}))
        self.host = host
        destroy = process_lifecycle.destroy_run_scratch

        def withhold_removal(path, created):
            self.assertEqual(destroy(path, created), (True, None))
            return False, None

        with patch.object(process_lifecycle, 'destroy_run_scratch', side_effect=withhold_removal):
            failed_reset = self.projector.invoke(_ROUTE, args)
        self.assertEqual((failed_reset['code'], failed_reset['result']), ('AVAILABILITY_PARKED', {}))
        self.assertNotIn(args['request_id'], self.host.retained)
        self.host.readback = lambda artifacts: {ref: raw + b' ' for ref, raw in artifacts.items()}
        failed_readback = self.projector.invoke(_ROUTE, args)
        self.assertEqual((failed_readback['code'], failed_readback['result']), ('AVAILABILITY_PARKED', {}))
