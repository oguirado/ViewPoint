# SPDX-License-Identifier: AGPL-3.0-only
"""Acceptance tests for the rust-anything discovery package and offline report."""

from importlib import import_module
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from slot_host.projections import generate, manifest_digest
from slot_host.projections.schema import digest, jcs_bytes, require, standalone
from slot_host.runtime.live_park import LIVE_EXECUTION_PARKED
from slot_host.runtime.receipts import evidence_accepted, receipt_digest
from video_rea.expose.wire import loads


PACKAGE = Path(__file__).resolve().parents[1]
COMPONENT = PACKAGE.parents[2]
MANIFEST_PATH = PACKAGE / 'rust-anything.slot.json'
GATE_PATH = PACKAGE / 'adapter_gate.py'
BANK = COMPONENT / 'contracts/oracle'
PREFIX = 'ext.rust-anything'
UPSTREAM_URL = 'https://github.com/erkamyaman/rust-anything'
UPSTREAM_COMMIT = 'b30206b1af11c58706303dfe20a3bd2d819c1e68'
UPSTREAM_SOURCE = f'{UPSTREAM_URL}/blob/{UPSTREAM_COMMIT}'
LICENCE_REFS = [f'{UPSTREAM_SOURCE}/LICENSE#L1-L21', f'{UPSTREAM_SOURCE}/Cargo.toml#L6']
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


class RustAnythingAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kit = None
        if GATE_PATH.is_file():
            cls.kit = import_module('slot_host.adapters.rust-anything.adapter_gate').OfflineKit()
        catalogue = COMPONENT / 'contracts/manifest'
        manifest_path = MANIFEST_PATH if MANIFEST_PATH.is_file() else catalogue / 'examples/rust-anything.slot.json'
        cls.manifest = loads(manifest_path.read_bytes())
        cls.schema = loads((catalogue / 'rea.slot_manifest_v2.schema.json').read_bytes())

    def test_manifest_validates_with_explicit_pending_pins_and_full_kit(self):
        registry = {self.schema['$id']: self.schema}
        require(self.manifest, standalone(self.schema, registry), 'Slot manifest')
        self.assertEqual(self.manifest['status'], 'not_installed')
        self.assertEqual(self.manifest['upstream']['licence_id'], 'MIT')
        self.assertEqual(self.manifest['upstream']['version'], '3.3.0')
        self.assertEqual(self.manifest['upstream']['source_url'], 'https://github.com/erkamyaman/rust-anything')
        self.assertIs(self.manifest['upstream']['user_installed'], True)
        self.assertEqual(self.manifest['transport']['executable_binding'], 'user_selected')
        self.assertIsNone(self.manifest['provider_ref']['implementation_sha256'])
        self.assertIsNone(self.manifest['provider_ref']['provider_sha256'])
        self.assertIsNone(self.manifest['capabilities']['adapter_identity']['adapter_sha256'])
        self.assertTrue(all(row['execution_profile'] == 'offline_fixture'
                            and row['request_digest'] is None and row['result_digest'] is None
                            for row in self.manifest['contract_refs']))
        conformance = self.manifest['conformance']
        self.assertCountEqual(conformance['required_checks'], CODES)
        self.assertCountEqual(conformance['fixture_refs'], [f'fixture:host/oracle-bank/{key}@1' for key in CODES])
        self.assertCountEqual(conformance['negative_control_refs'], [f'negctl:host/oracle-bank/{key}@1' for key in CODES])

    def test_manifest_pins_both_upstream_licence_coordinates(self):
        self.assertEqual(self.manifest['upstream']['licence_evidence_refs'], LICENCE_REFS)

    def test_gate_rejects_missing_or_substituted_licence_pins(self):
        kit_type = import_module('slot_host.adapters.rust-anything.adapter_gate').OfflineKit
        manifest = loads(MANIFEST_PATH.read_bytes())
        with TemporaryDirectory(prefix='rust-anything-pin-') as directory:
            package = Path(directory)
            (package / 'fixtures.json').write_bytes((PACKAGE / 'fixtures.json').read_bytes())
            manifest_path = package / MANIFEST_PATH.name
            manifest['upstream']['licence_evidence_refs'] = LICENCE_REFS
            manifest_path.write_bytes(jcs_bytes(manifest))
            self.assertEqual(kit_type(package).manifest, manifest)

            invalid_refs = {
                'missing Cargo evidence': LICENCE_REFS[:1],
                'missing LICENSE evidence': LICENCE_REFS[1:],
                'mutable LICENSE ref': [LICENCE_REFS[0].replace(UPSTREAM_COMMIT, 'main'), LICENCE_REFS[1]],
                'different Cargo commit': [LICENCE_REFS[0], LICENCE_REFS[1].replace(UPSTREAM_COMMIT, '0' * 40)],
            }
            for change, refs in invalid_refs.items():
                with self.subTest(change=change):
                    manifest['upstream']['licence_evidence_refs'] = refs
                    manifest_path.write_bytes(jcs_bytes(manifest))
                    with self.assertRaisesRegex(ValueError, 'upstream licence evidence'):
                        kit_type(package)

    def test_upstream_documentation_links_to_reviewed_commit(self):
        document = (PACKAGE / 'UPSTREAM.md').read_text(encoding='utf-8')
        links = re.findall(r'\[[^\]\n]+\]\(([^)\n]+)\)', document)
        upstream_links = [link for link in links if not link.startswith('../../../contracts/')]
        self.assertTrue(upstream_links)
        for link in upstream_links:
            with self.subTest(link=link):
                self.assertRegex(link, rf'\A{re.escape(UPSTREAM_SOURCE)}/[^#\s]+#L[1-9]\d*(?:-L[1-9]\d*)?\Z')
        self.assertIn(f'`{UPSTREAM_COMMIT}`', document)

    def test_projections_repeat_and_publish_all_offline_case_routes(self):
        if self.kit is None:
            bundle = generate(self.manifest, registry={self.schema['$id']: self.schema})
        else:
            bundle = self.kit.compile()
            self.assertEqual(jcs_bytes(bundle), jcs_bytes(self.kit.compile()))
        names = {'admission', 'record_replay', 'perception', 'freshness', 'timing', 'custody',
                 'transport', 'independence', 'lifecycle'}
        cases = {f'{PREFIX}.conform.{name}{suffix}' for name in names for suffix in ('', '_negative')}
        self.assertEqual({row['id'] for row in bundle['router_rows'] if row['id'].startswith(f'{PREFIX}.conform.')},
                         cases)
        for route in cases | {f'{PREFIX}.{op}' for op in ('record', 'replay', 'compare')}:
            self.assertIn(route, bundle['cli_shapes'])
            self.assertIn(route, bundle['json_contracts'])
            self.assertEqual(bundle['route_availability'][route], 'not_installed')
        self.assertFalse(bundle['unresolved_conformance_refs'])
        self.assertIn('rea_slot_describe__rust_anything', bundle['mcp_tools'])
        self.assertIn('rea_slot_inspect', bundle['mcp_tools'])
        self.assertNotIn('rea_ext_rust_anything__record', bundle['mcp_tools'])
        self.assertEqual(jcs_bytes(bundle) + b'\n', (PACKAGE / 'projections.json').read_bytes())

    def test_gate_retains_only_host_fixture_receipts_and_parks_external_execution(self):
        self.assertIsNotNone(self.kit, 'No adapter-specific offline conformance gate is present')
        with patch('subprocess.Popen', side_effect=AssertionError('offline gate spawned a process')):
            report = self.kit.gate()
        self.assertTrue(report['passed'])
        self.assertEqual(report['execution_profile'], 'offline_fixture')
        self.assertEqual(report['live_status'], LIVE_EXECUTION_PARKED)
        self.assertIsNone(report['installation_ref'])
        self.assertIsNone(report['executable_path'])
        self.assertEqual(report['manifest_digest'], manifest_digest(self.manifest))
        self.assertEqual(report['projection_sha256'], digest(self.kit.bundle))
        self.assertEqual(jcs_bytes(report) + b'\n', (PACKAGE / 'offline-receipts.json').read_bytes())

    def _assert_case(self, key):
        self.assertIsNotNone(self.kit, f'Adapter has no offline run for {key}')
        pair = self.kit.run(key)
        self.assertEqual(set(pair), {'nominal', 'negative'})
        for polarity, code in (('nominal', 'ORACLE_PASS'), ('negative', CODES[key])):
            with self.subTest(polarity=polarity):
                row = pair[polarity]
                self.assertEqual(row['code'], code)
                self.assertEqual(row['outcome'], 'pass' if polarity == 'nominal' else 'fail')
                self.assertTrue(row['passed'])
                self.assertTrue(row['assertions'])
                self.assertTrue(all(row['assertions'].values()))
                case = loads((BANK / 'cases' / key / f'{polarity}.json').read_bytes())
                self.assertEqual(row['case_sha256'], digest(case))
                self.assertEqual(row['fixture_catalog_sha256'], self.kit.catalog_sha256)
                receipt = row['receipt']
                self.assertEqual(row['receipt_sha256'], receipt_digest(receipt))
                self.assertEqual(row['record_sha256'], digest({k: v for k, v in row.items() if k != 'record_sha256'}))
                self.assertTrue(receipt['attempt']['opened_before_dispatch'])
                self.assertFalse(receipt['observation']['producer']['process_bound'])
                self.assertEqual(receipt['observation']['producer']['id'], 'rust-anything.adapter_gate')
                self.assertFalse(receipt['shared_receipt_adapter']['live_use_permitted'])
                self.assertNotIn('slot_result', receipt['attempt'])
                self.assertFalse(evidence_accepted({'receipt': receipt, 'custody': {}}))
        self.assertNotEqual(pair['nominal']['fixture_sha256'], pair['negative']['fixture_sha256'])

    def test_admit_and_project(self):
        self._assert_case('admit-and-project')

    def test_outside_record_replay_compare(self):
        self._assert_case('outside-record-replay-compare')

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
