# SPDX-License-Identifier: AGPL-3.0-only
"""Projection tests against independently declared fixture contracts."""

import copy
import unittest
from pathlib import Path

from slot_host.projections import (
    PublicationError, control_envelope, generate, jcs_bytes, json_invoke,
    load_manifest_bytes, manifest_digest, mcp_invoke, router_invoke, verify_publication,
)
from slot_host.projections.schema import accepts, digest

REQUEST_ID = '12345678-args-test'
CONTROL_REQUEST = 'urn:vp.control.req:v1'
CONTROL_REPLY = 'urn:vp.control.reply:v1'
REQUEST = 'urn:rea.slot_request:v1'
RESULT = 'urn:rea.slot_result:v1'
MINI_ARGUMENT = 'urn:fixture.behaviour.record.args:v1'
MINI_RESULT = 'urn:fixture.behaviour.record.result:v1'
CASE_REF = 'fixture:host/oracle-bank/case_a@1'
NEGATIVE_REF = 'negctl:wrong-retained-reference'
ROOT = Path(__file__).resolve().parents[3]
EXPECTED = {
    'ext.rea': 'rea_slot_describe__rea',
    'ext.rea.record': 'rea_ext_rea__record',
    'ext.rea.replay': 'rea_ext_rea__replay',
    'ext.rea.compare': 'rea_ext_rea__compare',
    'ext.rea.conform': 'rea_conform_rea',
    'ext.rea.conform.case_a': 'rea_conform_rea__case_a',
    'host.slot.list': 'rea_slot_list',
    'host.slot.inspect': 'rea_slot_inspect',
    'host.behaviour.record': 'rea_behaviour_record',
    'host.behaviour.replay': 'rea_behaviour_replay',
    'host.behaviour.compare': 'rea_behaviour_compare',
    'host.timeline.compare': 'rea_timeline_compare',
    'host.visual.compare': 'rea_visual_compare',
    'host.evidence.inspect': 'rea_evidence_inspect',
    'host.fixture.ground_truth': 'rea_fixture_ground_truth',
}
POLICY = {'licence': 'allowed', 'perception_ceiling': 'vision', 'evidence_schema_bound': True,
          'case_availability': {'case_a': 'admitted'}, 'conformance_receipts': {
              'record': 'receipt:record/1', 'replay': 'receipt:replay/1', 'compare': 'receipt:compare/1'}}
BINDING = {'installations': ['local-1'], 'selected_ref': None, 'provider_id': 'rea', 'provider_revision': '1.0.0',
           'provider_sha256': 'a' * 64, 'implementation_sha256': 'b' * 64,
           'adapter_id': 'rea.cli.v1', 'adapter_sha256': 'c' * 64, 'probes': {'ready': 'passed'}}


def closed(properties, required=()):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(required)}


def load_registry():
    manifest_schema = load_manifest_bytes((ROOT / 'contracts/manifest/rea.slot_manifest_v2.schema.json').read_bytes())
    request = {'$id': REQUEST, '$defs': {'seed': {'type': 'integer', 'minimum': 0}},
               'properties': {'params': {'type': 'object'}}, 'allOf': []}
    for role in ('replay', 'verify'):
        params = closed({'seed': {'$ref': '#/$defs/seed'}, 'frames': {
            'type': 'array', 'minItems': 1, 'items': {'type': 'integer'},
        }})
        request['allOf'].append({'if': {'required': ['slot'], 'properties': {'slot': {'const': role}}},
                                 'then': {'properties': {'params': params}}})
    request_contract = {'$id': CONTROL_REQUEST, **closed({
        'schema': {'const': 'vp.control.req/v1'}, 'op': {'const': 'rea.slot.project'},
        'request_id': {'type': 'string'}, 'context': {'type': ['object', 'null']},
        'dry_run': {'type': 'boolean'}, 'args': closed({
            'route_id': {'type': 'string'}, 'arguments': {'type': 'object'},
        }, ('route_id', 'arguments')),
    }, ('schema', 'op', 'request_id', 'context', 'args'))}
    reply_contract = {'$id': CONTROL_REPLY, **closed({
        'schema': {'const': 'vp.control.reply/v1'}, 'op': {'const': 'rea.slot.project'},
        'request_id': {'type': 'string'}, 'ok': {'type': 'boolean'}, 'code': {'type': 'string'},
        'result': {'type': 'object'}, 'effective_request_sha256': {'type': ['string', 'null']},
        'evidence': {'type': 'array'}, 'not_verified': {'type': 'array'},
    }, ('schema', 'op', 'request_id', 'ok', 'code', 'result', 'effective_request_sha256', 'evidence', 'not_verified'))}
    resources = [manifest_schema, request, {'$id': RESULT, 'type': 'object'}, request_contract, reply_contract,
                 {'$id': MINI_ARGUMENT, **closed({'tape': {'type': 'string'}}, ('tape',))},
                 {'$id': MINI_RESULT, **closed({'retained': {'type': 'boolean'}}, ('retained',))}]
    registry = {resource['$id']: resource for resource in resources}
    registry[CASE_REF] = {'fixture': 'case_a'}
    return registry


def minimal_manifest(slot_id='rea', modes=None, *, registry=None):
    registry = load_registry() if registry is None else registry
    operations = []
    for operation, role in (('record', 'replay'), ('replay', 'replay'), ('compare', 'verify')):
        operations.append({
            'id': operation, 'role': role, 'input_ref': REQUEST + '#slot=' + role,
            'output_ref': RESULT + '#slot=' + role,
            'binding': {'kind': 'cli_subcommand', 'argv': [operation], 'typed_args': []},
            'required_effects': ['fs.read:target'], 'required_grants': ['fixture.read'],
            'limits': {'timeout_s': 2, 'max_output_bytes': 1024},
            'retry_semantics': {'mode': 'at_most_once', 'on_interrupt': 'UNCERTAIN_OUTCOME'},
        })
    return {
        '$comment': 'SPDX-License-Identifier: AGPL-3.0-only', 'schema': 'rea.slot_manifest/v2',
        'slot_id': slot_id, 'status': 'registered',
        'upstream': {'name': 'REA fixture', 'source_url': 'https://github.com/morluto/rea', 'version': '1.0.0',
                     'licence_id': 'MIT', 'licence_evidence_refs': ['LICENSE:1'], 'component_notices': [],
                     'user_installed': True},
        'provider_ref': {'schema': 'rea.provider/v1', 'provider_id': slot_id, 'provider_revision': '1.0.0',
                         'provider_sha256': 'a' * 64, 'implementation_sha256': 'b' * 64},
        'contract_refs': [{'role': role, 'request_schema_ref': REQUEST, 'request_digest': digest(registry[REQUEST]),
                           'result_schema_ref': RESULT, 'result_digest': digest(registry[RESULT]),
                           'execution_profile': 'offline_fixture'} for role in ('replay', 'verify')],
        'transport': {'kind': 'cli_argv', 'argv_prefix': [], 'driver_location': 'outside_capsule',
                      'executable_binding': 'user_selected', 'framing': 'none', 'cwd_policy': 'run_scratch',
                      'env_allowlist': ['PATH'], 'alternates': [{'kind': 'mcp_stdio', 'argv_prefix': [],
                                                               'framing': 'jsonrpc_stdio'}]},
        'operations': operations,
        'capabilities': {'target_kinds': ['shipped_artifact'], 'platforms': ['linux'], 'optional_features': [],
                         'prerequisite_probes': [{'id': 'ready', 'effects': [], 'argv': ['version'], 'requires_binding': True}],
                         'adapter_identity': {'adapter_id': 'rea.cli.v1', 'adapter_sha256': 'c' * 64,
                                              'provenance': 'host_authored'}},
        'perception': {'required_level': 'none', 'optional_levels': ['vision'], 'observation_sources': [
            {'id': 'none', 'level': 'none', 'producer': 'fixture', 'outside_capsule': True},
            {'id': 'vision', 'level': 'vision', 'producer': 'host', 'outside_capsule': True,
             'vision_pool': {'required': True, 'owner': 'ViewPoint_Tools', 'ticket_field': 'vision_pool_ticket'}},
        ]},
        'record_replay': {'input_codec': 'fixture', 'output_codec': 'fixture', 'state_domain': 'fixture',
                          'observable_channels': ['fixture'], 'effects': ['fs.read:target'],
                          'determinism_modes': modes if modes is not None else ['recorded_inputs'], 'freshness_inputs': []},
        'comparison': {'comparator_id': 'fixture.compare', 'version': '1', 'policy_ref': 'policy:fixture',
                       'coverage_requirements': {'fixture': 'exact'}, 'tolerance_units': 'none',
                       'waiver_policy': {'source': 'host', 'binds': ['input', 'actual', 'reason', 'scope', 'policy_version'],
                                         'stale': 'fail'}},
        'evidence': {'shared_schema_ref': 'evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending',
                     'required_rea_fields': ['slot'], 'artifact_roles': ['fixture'],
                     'retention': 'evidence_store_attempt', 'visibility': 'private'},
        'conformance': {'fixture_refs': [CASE_REF], 'negative_control_refs': [NEGATIVE_REF],
                        'supported_entry_points': ['cli', 'mcp', 'json'], 'required_checks': [
                            'admit-and-project', 'outside-record-replay-compare', 'perception-and-pool',
                            'transport-parity-and-retry']},
        'discovery': {'summary': 'Projection fixture', 'search_terms': ['fixture'],
                      'operation_descriptions': {op['id']: op['id'] for op in operations}, 'examples': []},
    }


class Fixture(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry()
        self.manifest = minimal_manifest(registry=self.registry)
        self.policy = copy.deepcopy(POLICY)
        self.binding = copy.deepcopy(BINDING)
        self.cases = [{'id': 'case_a', 'ref': CASE_REF, 'digest': digest(self.registry[CASE_REF]),
                       'summary': 'Case A', 'effects': [],
                       'perception': {'required_level': 'vision', 'optional_levels': []}}]
        self.minis = {'behaviour.record': {
            'argument_schema_ref': MINI_ARGUMENT, 'result_schema_ref': MINI_RESULT,
            'argument_schema': self.registry[MINI_ARGUMENT], 'result_schema': self.registry[MINI_RESULT],
            'perception': {'required_level': 'none', 'optional_levels': []},
        }}
        self.bundle = self.generate()

    def sources(self, **extra):
        return {**self.host(), 'host_cases': self.cases, 'host_mini_tools': self.minis, **extra}

    def host(self, **extra):
        return {'binding': self.binding, 'policy': self.policy, 'registry': self.registry, **extra}

    def generate(self, manifest=None, **extra):
        return generate(self.manifest if manifest is None else manifest, **self.sources(**extra))

    def verify(self, bundle, **extra):
        return verify_publication(bundle, self.manifest, **self.sources(**extra))

    def route(self, name, bundle=None):
        return next(row for row in (bundle if bundle is not None else self.bundle)['router_rows'] if row['id'] == name)

    def assert_code(self, code, callback, *args, **kwargs):
        with self.assertRaises(PublicationError) as raised:
            callback(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def invoke(self, route='ext.rea.record', args=None, **overrides):
        return router_invoke(self.manifest, self.bundle, route, args if args is not None else self.args(),
                             **self.host(**overrides))

    def args(self, **extra):
        return {'request_id': REQUEST_ID, **extra}


class ProjectionTests(Fixture):
    def test_publication_exhausts_independent_inventory_and_rejects_missing_face(self):
        self.verify(self.bundle)
        self.assertEqual({row['id']: row['mcp_name'] for row in self.bundle['router_rows']}, EXPECTED)
        for route, name in EXPECTED.items():
            with self.subTest(route=route):
                row = self.route(route)
                self.assertEqual(self.bundle['cli_shapes'][route], row['cli_argv_shape'])
                self.assertEqual(self.bundle['json_contracts'][route]['route_id'], route)
                self.assertEqual(row['output_schema_ref'], CONTROL_REPLY)
                if route in ('ext.rea', 'host.slot.list', 'host.slot.inspect') or row['availability'] == 'admitted':
                    self.assertIn(name, self.bundle['mcp_tools'])
                    self.assertIn(row['input_schema_ref'], self.bundle['schemas'])
                else:
                    self.assertNotIn(name, self.bundle['mcp_tools'])
        bad = copy.deepcopy(self.bundle)
        del bad['mcp_tools']['rea_ext_rea__record']
        self.assert_code('MISSING_PROJECTION', self.verify, bad)

    def test_parity_all_admitted_rows_byte_identical(self):
        arguments = {
            'ext.rea': self.args(op_id='record'),
            'ext.rea.record': self.args(), 'ext.rea.replay': self.args(), 'ext.rea.compare': self.args(),
            'ext.rea.conform.case_a': self.args(dry_run=True), 'host.slot.list': self.args(),
            'host.slot.inspect': self.args(slot_id='rea'), 'host.behaviour.record': self.args(payload={'tape': 'fixture'}),
        }
        self.assertEqual(set(arguments), {row['id'] for row in self.bundle['router_rows'] if row['availability'] == 'admitted'})
        for route, args in arguments.items():
            with self.subTest(route=route):
                expected = self.invoke(route, args)
                observed = mcp_invoke(self.manifest, self.bundle, EXPECTED[route], args, **self.host())
                self.assertEqual(jcs_bytes(observed), jcs_bytes(expected))
                envelope = control_envelope(self.manifest, route, args, context={})
                self.assertEqual(json_invoke(self.manifest, self.bundle, envelope, **self.host()), expected)

    def test_standalone_schema_preserves_role_reference_constraints(self):
        row = self.route('ext.rea.record')
        schema = self.bundle['mcp_tools'][row['mcp_name']]['inputSchema']
        self.assertIn('$defs', schema)
        self.assertTrue(schema['properties']['params']['$ref'].startswith('#/'))
        self.assertTrue(accepts(self.args(params={'seed': 7}), schema))
        self.assertFalse(accepts(self.args(params={'seed': -1}), schema))
        missing = copy.deepcopy(self.registry)
        missing[REQUEST]['allOf'][0]['then']['properties']['params']['properties']['seed']['$ref'] = 'urn:missing:params'
        manifest = minimal_manifest(registry=missing)
        self.assert_code('MISSING_PROJECTION', self.generate, manifest, registry=missing)

    def test_p1_pin_mismatch_refuses_publication(self):
        for name in ('request_digest', 'result_digest'):
            with self.subTest(name=name):
                changed = copy.deepcopy(self.manifest)
                changed['contract_refs'][0][name] = 'f' * 64
                self.assert_code('SCHEMA_INVALID', self.generate, changed)

    def test_p2_mini_tool_verified_schema_and_parity(self):
        args = self.args(payload={'tape': 'fixture'})
        direct = self.invoke('host.behaviour.record', args)
        self.assertEqual(direct, mcp_invoke(self.manifest, self.bundle, 'rea_behaviour_record', args, **self.host()))
        envelope = control_envelope(self.manifest, 'host.behaviour.record', args, context={})
        self.assertEqual(direct, json_invoke(self.manifest, self.bundle, envelope, **self.host()))
        self.assertIsNone(direct['manifest_digest'])

    def test_publication_is_byte_stable_and_manifest_binds_comment(self):
        self.assertEqual(jcs_bytes(self.bundle), jcs_bytes(self.generate(copy.deepcopy(self.manifest))))
        changed = dict(self.manifest, **{'$comment': 'SPDX-License-Identifier: MIT'})
        self.assertNotEqual(manifest_digest(self.manifest), manifest_digest(changed))
        self.assert_code('SCHEMA_INVALID', load_manifest_bytes, b'{"a":1,"a":2}')

    def test_cli_mini_command(self):
        from slot_host.projections import cli_invoke

        args = self.args(payload={'tape': 'fixture'})
        parked = self.generate(host_mini_tools={})
        self.assert_code('AVAILABILITY_PARKED', cli_invoke, self.manifest, parked,
                         ['slot', 'mini', 'behaviour.record', '--args', '<json>'], args, **self.host())
