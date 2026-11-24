# SPDX-License-Identifier: AGPL-3.0-only
"""Verify the Craft Anything declaration and every offline conformance contrast."""

from importlib import import_module, util
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from slot_host.projections import generate, manifest_digest
from slot_host.projections.schema import digest, jcs_bytes, require, standalone
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED
from slot_host.runtime.receipts import evidence_accepted, receipt_digest
from video_rea.expose.wire import loads


SLOT = 'craft-anything'
PACKAGE = Path(__file__).resolve().parents[1]
CONTRACTS = PACKAGE.parents[2] / 'contracts'
MANIFEST = CONTRACTS / 'manifest'
EXAMPLE = MANIFEST / 'examples/craft-anything.slot.json'
GATE = 'slot_host.adapters.craft-anything.adapter_gate'
CASES = {
    'admit-and-project': ('admission', 'ORACLE_LIVE_PROFILE_NOT_ADMITTED'),
    'outside-record-replay-compare': ('record_replay', 'ORACLE_BEHAVIOUR_MISMATCH'),
    'perception-and-pool': ('perception', 'ORACLE_VISION_LEASE_TIMEOUT'),
    'freshness-and-value-semantics': ('freshness', 'ORACLE_ORIGINAL_STALE'),
    'event-and-presentation-time': ('timing', 'ORACLE_EVENT_TIME_MISMATCH'),
    'custody-and-wrong-binding': ('custody', 'ORACLE_PRODUCER_BINDING_MISMATCH'),
    'transport-parity-and-retry': ('transport', 'ORACLE_REQUEST_ID_CONFLICT'),
    'oracle-independence-and-coverage': ('independence', 'ORACLE_EXPECTATION_NOT_INDEPENDENT'),
    'update-and-remove': ('lifecycle', 'ORACLE_REPLACEMENT_NOT_QUALIFIED'),
}


class CraftAnythingAdapterTests(unittest.TestCase):
    def test_upstream_documentation_links_to_reviewed_commit(self):
        document = (PACKAGE / 'UPSTREAM.md').read_text(encoding='utf-8')
        links = re.findall(r'\[[^\]\n]+\]\(([^)\n]+)\)', document)
        self.assertEqual([link for link in links if link.startswith(('/', 'file:'))], [])
        repo = 'https://github.com/storytold/craft-anything'
        commit = '7073462fcca6088989f9127f3473b95471a758b1'
        commit_url = f'{repo}/commit/{commit}'
        self.assertIn(commit_url, links)
        upstream_links = [link for link in links if '://' in link and link != commit_url]
        self.assertTrue(upstream_links)
        source = f'{repo}/blob/{commit}/'
        for link in upstream_links:
            with self.subTest(link=link):
                self.assertRegex(link, rf'\A{re.escape(source)}[^#\s]+#L[1-9]\d*(?:-L[1-9]\d*)?\Z')

    @classmethod
    def setUpClass(cls):
        cls.module = import_module(GATE) if util.find_spec(GATE) is not None else None
        cls.kit = cls.module.OfflineKit() if cls.module is not None else None
        cls.example = loads(EXAMPLE.read_bytes())
        # The baseline offers the discovery example through the same projector.
        cls.manifest = cls.kit.manifest if cls.kit is not None else cls.example
        cls.schema = loads((MANIFEST / 'rea.slot_manifest_v2.schema.json').read_bytes())

    def test_manifest_validates_with_verified_release_and_pending_installation(self):
        for manifest in (self.example, self.manifest):
            require(manifest, standalone(self.schema, {self.schema['$id']: self.schema}), 'Slot manifest')
            upstream = manifest['upstream']
            self.assertEqual(upstream['licence_id'], 'MIT OR Apache-2.0')
            self.assertEqual(upstream['version'], '0.4.0')
            self.assertEqual(upstream['source_url'], 'https://github.com/storytold/craft-anything')
            self.assertIs(upstream['user_installed'], True)
            self.assertEqual(manifest['status'], 'not_installed')
            self.assertEqual(manifest['transport']['executable_binding'], 'user_selected')
            self.assertEqual(manifest['transport']['driver_location'], 'outside_capsule')
            self.assertEqual(manifest['transport']['kind'], 'cli_argv')
            self.assertEqual(manifest['transport']['alternates'], [
                {'kind': 'mcp_stdio', 'argv_prefix': ['mcp'], 'framing': 'jsonrpc_stdio'},
            ])
            self.assertEqual(manifest['perception']['required_level'], 'none')
            self.assertEqual(manifest['perception']['optional_levels'], [])
            provider = manifest['provider_ref']
            self.assertEqual(provider['provider_revision'], 'unpinned')
            self.assertIsNone(provider['provider_sha256'])
            self.assertIsNone(provider['implementation_sha256'])
            self.assertIsNone(manifest['capabilities']['adapter_identity']['adapter_sha256'])
            for contract in manifest['contract_refs']:
                self.assertEqual(contract['execution_profile'], 'offline_fixture')
                self.assertIsNone(contract['request_digest'])
                self.assertIsNone(contract['result_digest'])
            self.assertEqual(manifest['evidence']['shared_schema_ref'],
                             'evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending')
            self.assertCountEqual(manifest['conformance']['required_checks'], CASES)
        self.assertEqual(self.example, self.manifest)

    def test_projection_publication_is_byte_stable_and_has_all_fixture_routes(self):
        if self.kit is None:
            bundle = generate(self.manifest, policy={'perception_ceiling': 'none'},
                              registry={self.schema['$id']: self.schema})
        else:
            bundle = self.kit.compile()
            self.assertEqual(jcs_bytes(bundle), jcs_bytes(self.kit.compile()))
        case_routes = {f'ext.{SLOT}.conform.{name}{suffix}' for name, _ in CASES.values()
                       for suffix in ('', '_negative')}
        rows = {row['id']: row for row in bundle['router_rows']}
        self.assertEqual({route for route in rows if route.startswith(f'ext.{SLOT}.conform.')}, case_routes)
        for route in case_routes | {f'ext.{SLOT}.{name}' for name in ('conform', 'record', 'replay', 'compare')}:
            self.assertIn(route, bundle['cli_shapes'])
            self.assertIn(route, bundle['json_contracts'])
            self.assertTrue(rows[route]['mcp_name'])
            self.assertNotIn(rows[route]['mcp_name'], bundle['mcp_tools'])
            self.assertEqual(bundle['route_availability'][route], 'not_installed')
        self.assertFalse(bundle['unresolved_conformance_refs'])
        self.assertIn('rea_slot_inspect', bundle['mcp_tools'])
        self.assertIn('rea_slot_describe__craft_anything', bundle['mcp_tools'])
        self.assertEqual(jcs_bytes(bundle) + b'\n', (PACKAGE / 'projections.json').read_bytes())

    def test_gate_retains_offline_receipts_without_external_execution(self):
        self.assertIsNotNone(self.module, 'no Craft Anything offline gate or receipts')
        with patch('subprocess.Popen', side_effect=AssertionError('offline gate attempted to spawn')):
            report = self.module.OfflineKit().gate()
        self.assertTrue(report['passed'])
        self.assertEqual(report['slot_id'], SLOT)
        self.assertEqual(report['execution_profile'], 'offline_fixture')
        self.assertEqual(report['live_status'], LIVE_EXECUTION_PARKED)
        self.assertEqual(report['owner_pending'], ['MAIN', 'T01', 'T02', 'T03', 'T04', 'T05', 'T06'])
        self.assertIsNone(report['installation_ref'])
        self.assertIsNone(report['executable_path'])
        self.assertEqual(report['manifest_digest'], manifest_digest(self.manifest))
        self.assertEqual(report['projection_sha256'], digest(self.kit.bundle))
        self.assertEqual(set(report['cases']), CASES.keys())
        self.assertEqual(jcs_bytes(report) + b'\n', (PACKAGE / 'offline-receipts.json').read_bytes())

    def _assert_receipt(self, row):
        self.assertTrue(row['passed'])
        self.assertEqual(row['fixture_catalog_sha256'], self.kit.catalog_sha256)
        self.assertEqual(row['record_sha256'], digest({k: v for k, v in row.items() if k != 'record_sha256'}))
        receipt = row['receipt']
        self.assertEqual(row['receipt_sha256'], receipt_digest(receipt))
        self.assertEqual(receipt['slot']['tool_id'], SLOT)
        self.assertEqual(receipt['slot']['upstream_licence_id'], 'MIT OR Apache-2.0')
        self.assertEqual(receipt['slot']['manifest_sha256'], self.kit.bundle['manifest_digest'])
        self.assertTrue(receipt['attempt']['opened_before_dispatch'])
        self.assertNotIn('slot_result', receipt['attempt'])
        self.assertEqual(receipt['observation']['producer']['id'], 'craft-anything.adapter_gate')
        self.assertFalse(receipt['observation']['producer']['process_bound'])
        self.assertFalse(receipt['shared_receipt_adapter']['live_use_permitted'])
        self.assertFalse(evidence_accepted({'receipt': receipt, 'custody': {}}))

    def _assert_case(self, test_id):
        conformance = self.manifest['conformance']
        self.assertIn(f'fixture:host/oracle-bank/{test_id}@1', conformance['fixture_refs'])
        self.assertIn(f'negctl:host/oracle-bank/{test_id}@1', conformance['negative_control_refs'])
        self.assertIsNotNone(self.kit, f'no Craft Anything offline run for {test_id}')
        pair = self.kit.run(test_id)
        self.assertEqual(set(pair), {'nominal', 'negative'})
        self.assertNotEqual(pair['nominal']['fixture_sha256'], pair['negative']['fixture_sha256'])
        for polarity, code in (('nominal', 'ORACLE_PASS'), ('negative', CASES[test_id][1])):
            with self.subTest(polarity=polarity):
                row = pair[polarity]
                self.assertEqual(row['code'], code)
                self.assertEqual(row['outcome'], 'pass' if polarity == 'nominal' else 'fail')
                case = loads((CONTRACTS / 'oracle' / 'cases' / test_id / f'{polarity}.json').read_bytes())
                self.assertEqual(row['assertions'], {check['pointer']: True for check in case['expected']['assertions']})
                self.assertEqual(row['case_sha256'], digest(case))
                self._assert_receipt(row)
        return pair

    def test_admit_and_project(self):
        self._assert_case('admit-and-project')

    def test_outside_record_replay_compare(self):
        pair = self._assert_case('outside-record-replay-compare')
        tape = self.kit.cases['outside-record-replay-compare', 'nominal']['fixture']['input_tape']
        for row in pair.values():
            self.assertEqual(row['observation']['fixture_replay_lineage'], {
                'parent_episode_id': 'craft-anything-fixture-original-episode',
                'input_tape_sha256': digest(tape),
                'foreign_parent_rejected': 'craft-anything-fixture-foreign-episode',
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

    def test_retained_evidence_abstains_on_empty_cache_and_unresolved_continuation(self):
        self.assertIsNotNone(self.kit, 'no Craft Anything retained evidence fixtures')
        cases = self.kit.run_continuation()
        self.assertEqual(set(cases), {'nominal', 'empty_cache', 'unresolved_continuation'})
        self.assertEqual(len({row['fixture_sha256'] for row in cases.values()}), len(cases))
        self.assertEqual(len({row['observation']['evidence_id'] for row in cases.values()}), 1)
        for polarity, code, offsets, missing in (
            ('nominal', 'ORACLE_PASS', [0, 1], None),
            ('empty_cache', 'ORACLE_EMPTY_CACHE', [], 0),
            ('unresolved_continuation', 'ORACLE_UNRESOLVED_CONTINUATION', [0], 1),
        ):
            with self.subTest(polarity=polarity):
                row = cases[polarity]
                self.assertEqual(row['code'], code)
                self.assertEqual(row['observation']['resolved_offsets'], offsets)
                self.assertEqual(row['observation'].get('unresolved_offset'), missing)
                self.assertEqual(row['observation']['verdict'], 'match' if missing is None else 'abstain')
                self.assertEqual(row['outcome'], 'pass' if missing is None else 'fail')
                self._assert_receipt(row)
