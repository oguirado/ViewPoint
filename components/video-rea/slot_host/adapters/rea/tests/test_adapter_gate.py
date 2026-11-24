# SPDX-License-Identifier: AGPL-3.0-only
"""Acceptance tests for the morluto/rea declaration and per-adapter offline gate."""

from pathlib import Path
import re
import unittest
from unittest.mock import patch

from slot_host.projections import generate, manifest_digest
from slot_host.projections.schema import digest, jcs_bytes, require, standalone
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED
from slot_host.runtime.receipts import evidence_accepted, receipt_digest
from video_rea.expose.wire import loads


PACKAGE = Path(__file__).resolve().parents[1]
CONTRACTS = PACKAGE.parents[2] / 'contracts'
MANIFEST = CONTRACTS / 'manifest'
CODES = {
    'admit-and-project': 'ORACLE_LIVE_PROFILE_NOT_ADMITTED',
    'outside-record-replay-compare': 'ORACLE_BEHAVIOUR_MISMATCH',
    'perception-and-pool': 'ORACLE_VISION_LEASE_TIMEOUT',
    'freshness-and-value-semantics': 'ORACLE_ORIGINAL_STALE',
    'event-and-presentation-time': 'ORACLE_EVENT_TIME_MISMATCH',
    'custody-and-wrong-binding': 'ORACLE_PRODUCER_BINDING_MISMATCH',
    'transport-parity-and-retry': 'ORACLE_REQUEST_ID_CONFLICT',
    'oracle-independence-and-coverage': 'ORACLE_EXPECTATION_NOT_INDEPENDENT',
    'update-and-remove': 'ORACLE_REPLACEMENT_NOT_QUALIFIED',
}
CASE_ROUTES = ('admission', 'record_replay', 'perception', 'freshness', 'timing',
               'custody', 'transport', 'independence', 'lifecycle')


class ReaAdapterTests(unittest.TestCase):
    def test_upstream_documentation_links_to_reviewed_commit(self):
        document = (PACKAGE / 'UPSTREAM.md').read_text(encoding='utf-8')
        links = re.findall(r'\[[^\]\n]+\]\(([^)\n]+)\)', document)
        self.assertEqual([link for link in links if link.startswith(('/', 'file:'))], [])
        upstream_links = [link for link in links if '://' in link]
        self.assertTrue(upstream_links)
        source = 'https://github.com/morluto/rea/blob/160d821ac9a0310afcb8d1fc427862dd6508add5/'
        for link in upstream_links:
            with self.subTest(link=link):
                self.assertRegex(link, rf'\A{re.escape(source)}[^#\s]+(?:#L[1-9]\d*(?:-L[1-9]\d*)?)?\Z')

    @classmethod
    def setUpClass(cls):
        cls.gate_module = None
        cls.kit = None
        if (PACKAGE / 'adapter_gate.py').is_file():
            from slot_host.adapters.rea import adapter_gate

            cls.gate_module = adapter_gate
            cls.kit = adapter_gate.OfflineKit()
        # The pre-step checkout already has a discovery example. Use it to
        # check whether the old manifest and projector can meet this gate.
        path = PACKAGE / 'rea.slot.json'
        cls.manifest = loads((path if path.is_file() else MANIFEST / 'examples/rea.slot.json').read_bytes())
        cls.schema = loads((MANIFEST / 'rea.slot_manifest_v2.schema.json').read_bytes())

    def test_manifest_validates_with_verified_release_and_pending_installation(self):
        require(self.manifest, standalone(self.schema, {self.schema['$id']: self.schema}), 'Slot manifest')
        upstream = self.manifest['upstream']
        self.assertEqual(upstream['version'], '6.8.0')
        self.assertEqual(upstream['name'], 'morluto/rea')
        self.assertEqual(upstream['source_url'], 'https://github.com/morluto/rea')
        self.assertEqual(upstream['licence_id'], 'MIT')
        self.assertEqual(len(upstream['licence_evidence_refs']), 1)
        self.assertTrue(upstream['licence_evidence_refs'][0].endswith('/LICENSE#L1-L3'))
        self.assertEqual(len(upstream['component_notices']), 1)
        self.assertTrue(upstream['component_notices'][0].endswith('/THIRD_PARTY_NOTICES.md'))
        self.assertIs(upstream['user_installed'], True)
        self.assertEqual(self.manifest['status'], 'not_installed')
        transport = self.manifest['transport']
        self.assertEqual((transport['kind'], transport['framing'], transport['argv_prefix']),
                         ('mcp_stdio', 'jsonrpc_stdio', ['mcp']))
        self.assertEqual(transport['executable_binding'], 'user_selected')
        self.assertEqual(transport['driver_location'], 'outside_capsule')
        provider = self.manifest['provider_ref']
        self.assertEqual((provider['schema'], provider['provider_id'], provider['provider_revision']),
                         ('rea.provider/v1', 'rea', 'unpinned'))
        self.assertIsNone(provider['provider_sha256'])
        self.assertIsNone(provider['implementation_sha256'])
        self.assertIsNone(self.manifest['capabilities']['adapter_identity']['adapter_sha256'])
        for contract in self.manifest['contract_refs']:
            self.assertEqual(contract['execution_profile'], 'offline_fixture')
            self.assertIsNone(contract['request_digest'])
            self.assertIsNone(contract['result_digest'])
        bindings = {op['id']: op['binding'] for op in self.manifest['operations']}
        self.assertEqual(bindings, {
            'record': {'kind': 'mcp_tool', 'tool': 'export_evidence_bundle'},
            'replay': {'kind': 'host_composition', 'mini_tool': 'behaviour.replay', 'native_call': None},
            'compare': {'kind': 'mcp_tool', 'tool': 'compare_artifacts'},
        })
        self.assertEqual(self.manifest['evidence']['shared_schema_ref'], 'evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending')
        conf = self.manifest['conformance']
        self.assertCountEqual(conf['required_checks'], CODES)
        self.assertCountEqual(conf['fixture_refs'], [f'fixture:host/oracle-bank/{case}@1' for case in CODES])
        self.assertCountEqual(conf['negative_control_refs'], [f'negctl:host/oracle-bank/{case}@1' for case in CODES])

    def test_projection_publication_is_byte_stable_and_has_all_offline_routes(self):
        if self.kit is None:
            bundle = generate(self.manifest, policy={'perception_ceiling': 'none'},
                              registry={self.schema['$id']: self.schema})
        else:
            bundle = self.kit.compile()
            self.assertEqual(jcs_bytes(bundle), jcs_bytes(self.kit.compile()))
        case_routes = {f'ext.rea.conform.{name}{suffix}' for name in CASE_ROUTES for suffix in ('', '_negative')}
        rows = {row['id']: row for row in bundle['router_rows']}
        self.assertEqual({route for route in rows if route.startswith('ext.rea.conform.')}, case_routes)
        for route in case_routes | {'ext.rea.conform', 'ext.rea.record', 'ext.rea.replay', 'ext.rea.compare'}:
            self.assertIn(route, bundle['cli_shapes'])
            self.assertIn(route, bundle['json_contracts'])
            self.assertIn('mcp_name', rows[route])
            self.assertNotIn(rows[route]['mcp_name'], bundle['mcp_tools'])
            self.assertEqual(bundle['route_availability'][route], 'not_installed')
        self.assertFalse(bundle['unresolved_conformance_refs'])
        self.assertIn('rea_slot_inspect', bundle['mcp_tools'])
        self.assertIn('rea_slot_describe__rea', bundle['mcp_tools'])
        self.assertEqual(jcs_bytes(bundle) + b'\n', (PACKAGE / 'projections.json').read_bytes())

    def test_gate_retains_offline_receipts_and_never_spawns(self):
        self.assertIsNotNone(self.gate_module, 'missing morluto/rea adapter gate')
        with patch('subprocess.Popen', side_effect=AssertionError('offline qualification spawned a process')):
            report = self.gate_module.OfflineKit().gate()
        self.assertTrue(report['passed'])
        self.assertEqual(report['slot_id'], 'rea')
        self.assertEqual(report['execution_profile'], 'offline_fixture')
        self.assertEqual(report['live_status'], LIVE_EXECUTION_PARKED)
        self.assertEqual(report['owner_pending'], ['MAIN', 'T01', 'T02', 'T03', 'T04', 'T05', 'T06'])
        self.assertIsNone(report['installation_ref'])
        self.assertIsNone(report['executable_path'])
        self.assertEqual(report['manifest_digest'], manifest_digest(self.manifest))
        self.assertEqual(report['projection_sha256'], digest(self.kit.bundle))
        self.assertEqual(set(report['cases']), CODES.keys())
        self.assertEqual(jcs_bytes(report) + b'\n', (PACKAGE / 'offline-receipts.json').read_bytes())

    def _assert_case(self, test_id):
        self.assertIsNotNone(self.kit, f'no morluto/rea conformance run for {test_id}')
        pair = self.kit.run(test_id)
        self.assertEqual(set(pair), {'nominal', 'negative'})
        self.assertNotEqual(pair['nominal']['fixture_sha256'], pair['negative']['fixture_sha256'])
        for polarity, code in (('nominal', 'ORACLE_PASS'), ('negative', CODES[test_id])):
            with self.subTest(polarity=polarity):
                row = pair[polarity]
                self.assertEqual(row['code'], code)
                self.assertEqual(row['outcome'], 'pass' if polarity == 'nominal' else 'fail')
                self.assertTrue(row['passed'])
                case = loads((CONTRACTS / 'oracle' / 'cases' / test_id / f'{polarity}.json').read_bytes())
                self.assertEqual(row['assertions'], {a['pointer']: True for a in case['expected']['assertions']})
                self.assertEqual(row['case_sha256'], digest(case))
                self.assertEqual(row['fixture_catalog_sha256'], self.kit.catalog_sha256)
                self.assertEqual(row['record_sha256'], digest({k: v for k, v in row.items() if k != 'record_sha256'}))
                receipt = row['receipt']
                self.assertEqual(row['receipt_sha256'], receipt_digest(receipt))
                self.assertEqual(receipt['slot']['tool_id'], 'rea')
                self.assertEqual(receipt['slot']['manifest_sha256'], self.kit.bundle['manifest_digest'])
                self.assertTrue(receipt['attempt']['opened_before_dispatch'])
                self.assertNotIn('slot_result', receipt['attempt'])
                self.assertEqual(receipt['observation']['producer']['id'], 'rea.adapter_gate')
                self.assertFalse(receipt['observation']['producer']['process_bound'])
                self.assertFalse(receipt['shared_receipt_adapter']['live_use_permitted'])
                self.assertFalse(evidence_accepted({'receipt': receipt, 'custody': {}}))
        return pair

    def test_admit_and_project(self):
        self._assert_case('admit-and-project')

    def test_outside_record_replay_compare(self):
        pair = self._assert_case('outside-record-replay-compare')
        case = self.kit.cases['outside-record-replay-compare', 'nominal']
        for row in pair.values():
            self.assertEqual(row['observation']['fixture_replay_lineage'], {
                'parent_episode_id': 'rea-fixture-original-episode',
                'input_tape_sha256': digest(case['fixture']['input_tape']),
                'foreign_parent_rejected': 'rea-fixture-foreign-episode',
            })

    def test_perception_and_pool(self):
        self._assert_case('perception-and-pool')

    def test_freshness_and_value_semantics(self):
        self._assert_case('freshness-and-value-semantics')

    def test_event_and_presentation_time(self):
        self._assert_case('event-and-presentation-time')

    def test_custody_and_wrong_binding(self):
        self._assert_case('custody-and-wrong-binding')

    def test_transport_parity_and_retry(self):
        self._assert_case('transport-parity-and-retry')

    def test_oracle_independence_and_coverage(self):
        self._assert_case('oracle-independence-and-coverage')

    def test_update_and_remove(self):
        self._assert_case('update-and-remove')
