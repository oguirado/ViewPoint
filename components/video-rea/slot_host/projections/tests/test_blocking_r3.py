# SPDX-License-Identifier: AGPL-3.0-only
"""Regression tests within the projection-owned source surface."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slot_host.projections import PublicationError, SlotProjector, control_envelope, json_invoke, router_invoke
from slot_host.projections.cli import _read_bounded_file, parse_slot_command
from slot_host.projections.project import _check_collisions
from slot_host.projections.tests.test_projections import (
    CONTROL_REQUEST, MINI_ARGUMENT, MINI_RESULT, REQUEST, RESULT, Fixture, minimal_manifest,
)


class BlockingR3(Fixture):
    def test_r3_item1_delete_route_from_every_face(self):
        bad = copy.deepcopy(self.bundle)
        route = self.route('ext.rea.record')
        bad['router_rows'] = [row for row in bad['router_rows'] if row['id'] != route['id']]
        for table in ('cli_shapes', 'json_contracts', 'route_availability', 'route_installation'):
            del bad[table][route['id']]
        del bad['schemas'][route['input_schema_ref']]
        del bad['mcp_tools'][route['mcp_name']]
        del bad['resolved_params']['record']
        self.verify(self.bundle)
        self.assert_code('MISSING_PROJECTION', self.verify, bad)

    def test_publication_rejects_coordinated_default_drift(self):
        row = self.route('ext.rea.record')
        changes = {'perception_level': 'vision', 'determinism_mode': 'uncontrolled',
                   'dry_run': True, 'inputs': [{'role': 'source', 'ref': 'fixture:tape'}], 'params': {'seed': 7}}
        self.verify(self.bundle)
        for field, changed in changes.items():
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.bundle)
                for schema in (candidate['schemas'][row['input_schema_ref']],
                               candidate['mcp_tools'][row['mcp_name']]['inputSchema']):
                    schema['properties'][field]['default'] = changed
                self.assert_code('DIVERGENT_DEFAULT', self.verify, candidate)

    def test_publication_rejects_json_only_request_schema_ref_drift(self):
        candidate = copy.deepcopy(self.bundle)
        candidate['json_contracts']['ext.rea.record']['request_schema_ref'] = 'urn:rea.slot_request:v9'
        self.verify(self.bundle)
        self.assert_code('DIVERGENT_DEFAULT', self.verify, candidate)

    def test_publication_rejects_normalized_and_reserved_name_collisions(self):
        shared = [self.route('host.slot.list'), self.route('host.slot.inspect')]
        self.verify(self.bundle, existing_rows=shared)
        record = self.route('ext.rea.record')
        parked = self.route('host.behaviour.compare')
        self.assertIsNone(parked['input_schema_ref'])
        for changes in ({'mcp_name': record['mcp_name'].replace('_', '-')},
                        {'cli_argv_shape': record['cli_argv_shape']}, {'mcp_name': parked['mcp_name']}):
            with self.subTest(changes=changes):
                existing = dict(record, id='elsewhere.record', input_schema_ref=None,
                                mcp_name='elsewhere_record', cli_argv_shape=['elsewhere'], **{})
                existing.update(changes)
                self.assert_code('COLLISION', _check_collisions, self.bundle, [existing])
        candidate = copy.deepcopy(self.bundle)
        next(row for row in candidate['router_rows'] if row['id'] == parked['id'])['mcp_name'] = 'vp_rea_call'
        self.assert_code('COLLISION', self.verify, candidate)

    def test_pinned_schema_must_still_resolve_at_invocation(self):
        envelope = control_envelope(self.manifest, 'ext.rea.record', self.args(), context={})
        self.assertEqual(json_invoke(self.manifest, self.bundle, envelope, **self.host())['op_id'], 'record')
        for ref in (REQUEST, RESULT, CONTROL_REQUEST):
            with self.subTest(ref=ref):
                missing = dict(self.registry)
                del missing[ref]
                self.assert_code('SCHEMA_INVALID', json_invoke, self.manifest, self.bundle,
                                 envelope, **self.host(registry=missing))

    def test_r3_item10_result_pin_wrong_digest_schema_invalid(self):
        self.verify(self.bundle)
        registry = dict(self.registry)
        registry[RESULT] = dict(registry[RESULT], title='Different result resource')
        self.assert_code('SCHEMA_INVALID', self.generate, registry=registry)

    def test_mini_tool_inline_schema_does_not_supply_registry_pin(self):
        name = 'host.behaviour.record'
        args = self.args(payload={'tape': 'fixture'})
        self.assertEqual(self.route(name)['availability'], 'admitted')
        for ref in (MINI_ARGUMENT, MINI_RESULT):
            with self.subTest(ref=ref):
                absent = dict(self.registry)
                del absent[ref]
                parked = self.generate(registry=absent)
                self.assertEqual(self.route(name, parked)['availability'], 'parked')
                envelope = control_envelope(self.manifest, name, args, context={})
                self.assert_code('AVAILABILITY_PARKED', json_invoke, self.manifest, parked,
                                 envelope, **self.host(registry=absent))

    def test_projection_validation_rejects_empty_array_and_unknown_host_payload(self):
        self.invoke(args=self.args(params={'frames': [1]}))
        self.invoke('host.behaviour.record', self.args(payload={'tape': 'fixture'}))
        self.assert_code('SCHEMA_INVALID', self.invoke, args=self.args(params={'frames': []}))
        self.assert_code('SCHEMA_INVALID', self.invoke, 'host.behaviour.record',
                         self.args(payload={'tape': 'fixture', 'extra': True}))

    def test_invocation_refuses_manifest_and_policy_substitution(self):
        self.invoke()
        manifest = copy.deepcopy(self.manifest)
        manifest['record_replay']['determinism_modes'] = ['substituted']
        self.assert_code('SCHEMA_INVALID', router_invoke, manifest, self.bundle, 'ext.rea.record',
                         self.args(), **self.host())
        for policy in (None, dict(self.policy, licence='denied')):
            with self.subTest(policy=policy):
                self.assert_code('SCHEMA_INVALID', self.invoke, policy=policy)

    def test_invocation_refuses_implicit_and_explicit_installation_substitution(self):
        self.assertEqual(self.invoke()['installation_ref'], 'local-1')
        self.assertEqual(self.invoke(args=self.args(installation_ref='local-1'))['installation_ref'], 'local-1')
        changed = dict(self.binding, installations=['wrong-ref'])
        for args in (self.args(), self.args(installation_ref='wrong-ref')):
            with self.subTest(args=args):
                envelope = control_envelope(self.manifest, 'ext.rea.record', args, context={})
                self.assert_code('SCHEMA_INVALID', self.invoke, args=args, binding=changed)
                self.assert_code('SCHEMA_INVALID', json_invoke, self.manifest, self.bundle, envelope,
                                 **self.host(binding=changed))

    def test_invocation_requires_measured_artifact_and_adapter_identity(self):
        self.invoke()
        for name in ('provider_id', 'provider_revision', 'provider_sha256', 'implementation_sha256',
                     'adapter_id', 'adapter_sha256'):
            with self.subTest(name=name):
                absent = dict(self.binding)
                del absent[name]
                self.assert_code('SCHEMA_INVALID', self.invoke, binding=absent)
                self.assert_code('SCHEMA_INVALID', self.invoke, binding=dict(self.binding, **{name: 'wrong'}))

    def test_live_prerequisite_failure_blocks_previously_admitted_route(self):
        self.invoke()
        self.assert_code('AVAILABILITY_BLOCKED', self.invoke,
                         binding=dict(self.binding, probes={'ready': 'failed'}))
        self.assert_code('AVAILABILITY_BLOCKED', self.invoke, current_licence='denied')

    def test_discovery_uses_final_route_readiness_and_preserves_state(self):
        self.assertEqual(self.route('ext.rea')['availability'], 'admitted')
        registry = dict(self.registry)
        del registry[REQUEST]
        cases = [({'registry': registry}, 'parked'), ({'binding': {}}, 'not_installed'),
                 ({'policy': dict(self.policy, licence='denied')}, 'blocked'),
                 ({'binding': dict(self.binding, installations=['local-1', 'other'])}, 'parked'),
                 ({'registry': registry, 'policy': dict(self.policy, licence='denied')}, 'blocked')]
        for options, state in cases:
            with self.subTest(state=state):
                publication = self.generate(**options)
                self.assertEqual(self.route('ext.rea', publication)['availability'], state)
                self.assertEqual(self.route('ext.rea.record', publication)['availability'], state)

    def test_inspect_normalization_uses_target_slot_binding(self):
        beta = minimal_manifest('beta', registry=self.registry)
        binding = dict(self.binding, provider_id='beta', installations=['beta-local'])
        beta_bundle = self.generate(beta, binding=binding)
        args = self.args(slot_id='beta')
        kwargs = {'enrolled_slots': {'beta': (beta, beta_bundle, binding)}}
        self.assertEqual(self.invoke('host.slot.inspect', self.args(slot_id='rea'))['arguments']['installation_ref'], 'local-1')
        self.assertEqual(self.invoke('host.slot.inspect', args, **kwargs)['arguments']['installation_ref'], 'beta-local')
        absent = self.generate(beta, binding={})
        out = self.invoke('host.slot.inspect', args, enrolled_slots={'beta': (beta, absent, {})})
        self.assertNotIn('installation_ref', out['arguments'])
        self.assertEqual(self.route('ext.beta', absent)['availability'], 'not_installed')

    def test_conformance_uses_case_effective_perception(self):
        context = {'front': 'ViewPoint_REA'}
        seen = []

        def decision(authenticated, request):
            seen.append((authenticated, request['route_id']))
            return 'OK'

        self.invoke('ext.rea.conform.case_a', context=context, vision_admission=decision)
        self.assertEqual(seen, [(context, 'ext.rea.conform.case_a')])
        self.assert_code('PERCEPTION_DENIED', self.invoke, 'ext.rea.conform.case_a', context=context,
                         vision_admission=decision, perception_ceiling='none')
        self.assertEqual(len(seen), 1)

    def test_authoritative_vision_timeout_and_unbound_lease_are_distinct(self):
        args = self.args(perception_level='vision')
        self.assert_code('PERCEPTION_DENIED', self.invoke, args=args, context={'vision_pool_ticket': 'arbitrary'})
        self.assert_code('POOL_TIMEOUT', self.invoke, args=args, context={}, vision_admission=lambda *_: 'POOL_TIMEOUT')
        self.assert_code('PERCEPTION_DENIED', self.invoke, args=args, context={}, vision_admission=lambda *_: None)

    def test_alternate_transport_contract_and_parked_refusal(self):
        self.assertEqual(self.route('ext.rea.record')['availability'], 'admitted')
        for fields in ({'driver_location': 'inside_capsule'}, {'env_allowlist': ['PATH', 'PATH']}):
            invalid = copy.deepcopy(self.manifest)
            invalid['transport']['alternates'][0].update(fields)
            self.assert_code('SCHEMA_INVALID', self.generate, invalid)
        changed = copy.deepcopy(self.manifest)
        changed['operations'][0]['binding'] = {'kind': 'mcp_tool', 'tool': 'record'}
        parked = self.generate(changed)
        self.assertEqual(self.route('ext.rea.record', parked)['availability'], 'parked')
        self.assert_code('AVAILABILITY_PARKED', router_invoke, changed, parked, 'ext.rea.record', self.args(), **self.host())

    def test_compatibility_cli_parses_dry_run_and_refuses_unknown_flags(self):
        argv = ['slot', 'call', 'rea', 'record', '--args', '<json>', '--dry-run']
        route, args, strict = parse_slot_command(argv, args_document=self.args())
        self.assertEqual(route, 'ext.rea.record')
        self.assertTrue(args['dry_run'])
        self.assertFalse(strict)
        self.assert_code('SCHEMA_INVALID', parse_slot_command, argv[:-1] + ['--unsupported'], args_document=self.args())

    def test_adapter_without_dispatcher_has_no_successful_exchange(self):
        adapter = SlotProjector(self.manifest, self.bundle, **self.sources(), context={})
        envelope = control_envelope(self.manifest, 'ext.rea.record', self.args(), context={})
        result = adapter.handle_envelope(envelope)
        self.assertEqual(result['code'], 'AVAILABILITY_PARKED')
        self.assertFalse(result['ok'])
        self.assertEqual(result['result'], {})
        self.assertNotIn('vp_rea_call', [tool['name'] for tool in adapter.list_tools()])

    def test_adapter_rejects_unknown_envelope_field_before_forwarding(self):
        captured = []
        adapter = SlotProjector(self.manifest, self.bundle, **self.sources(), dispatch=captured.append, context={})
        envelope = control_envelope(self.manifest, 'ext.rea.record', self.args(dry_run=True), context={})
        result = adapter.handle_envelope(dict(envelope, unsupported=True))
        self.assertEqual(result['code'], 'SCHEMA_INVALID')
        self.assertEqual(captured, [])

    def test_descriptor_check_validates_replacement_after_path_selection(self):
        real_open = os.open
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'args.json'
            path.write_bytes(b'{}')
            self.assertEqual(_read_bounded_file(path), b'{}')

            def open_replacement(filename, flags):
                self.assertTrue(flags & os.O_NONBLOCK)
                path.unlink()
                os.mkfifo(path)
                return real_open(filename, flags)

            with patch('slot_host.projections.cli.os.open', side_effect=open_replacement):
                self.assert_code('SCHEMA_INVALID', _read_bounded_file, path)


if __name__ == '__main__':
    unittest.main()
