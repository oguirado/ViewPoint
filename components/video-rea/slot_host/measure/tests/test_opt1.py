# SPDX-License-Identifier: AGPL-3.0-only
"""Freeze Medicion carrier bytes and count the redundant work on each path."""

from collections import Counter
from hashlib import sha256
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

from slot_host.measure import harness, host
from slot_host.measure.evidence import Store
from slot_host.measure.seal import DEFAULT_MANIFEST, FixtureError
from slot_host.projections import schema
from video_rea.expose.wire import encode


EMPTY_INDEX_SHA256 = '71e4f4c7ece22f8828d9127223237daf2ac4e353b9f380e8be048978144aa36d'


class Opt1Tests(TestCase):
    def fixture(self, face, transport, case):
        captured = {}
        invoke, commit = harness.Session.invoke, Store.commit

        def capture_reply(session, carrier):
            reply = invoke(session, carrier)
            captured['reply'] = sha256(encode(reply)).hexdigest()
            return reply

        def capture_evidence(store):
            index = commit(store)
            captured['index'] = sha256(index.read_bytes()).hexdigest()
            captured['objects'] = {
                key: sha256((store.root / row['sha256']).read_bytes()).hexdigest()
                for key, row in store.index.items()
            }
            return index

        with (patch.object(harness, 'uuid4', return_value=UUID(int=1)),
              patch.object(harness.Session, 'invoke', capture_reply),
              patch.object(Store, 'commit', capture_evidence)):
            report = harness.run(DEFAULT_MANIFEST, face, case, (transport,))
        sample, = report['runs']
        return sample, captured

    def test_mcp_inspect_reuses_canonical_enum_values(self):
        validate = harness.tools._validate_static_manifest
        encodings = []

        def observe_validation(manifest):
            with patch.object(schema, 'jcs_bytes', wraps=schema.jcs_bytes) as encode_item:
                validate(manifest)
            encodings.append(encode_item.call_count)

        with patch.object(harness.tools, '_validate_static_manifest', observe_validation):
            for case, code, expected in (
                ('nominal', 'OK', '95a2e6927852c2bdf5ddebb8968a553cde6a710d196035f09af356a75039ae80'),
                ('negative', 'SCHEMA_INVALID', '474d81e7c69c438f14b5ea598cadf423755ea05802dcc356a9ba4408cfdfd9d1'),
            ):
                sample, captured = self.fixture('slot.inspect', 'mcp', case)
                self.assertEqual((sample['code'], sample['launches']), (code, 0))
                self.assertEqual(captured, {'reply': expected, 'index': EMPTY_INDEX_SHA256, 'objects': {}})
        self.assertEqual(len(encodings), 1)
        self.assertLessEqual(encodings[0], 388)

    def test_json_reconstruction_needs_no_fixture_executable(self):
        stage = harness.Runtime.__init__
        with patch.object(harness.Runtime, '__init__', autospec=True, side_effect=stage) as staged:
            for case, expected in (
                ('nominal', '8011b4f4aa8852f6ad25d83193f0bfb71e099429cb7c75825856f34624287276'),
                ('negative', '44e861ad2045901a23e0daea398fc15c2607857fcf7dfdd42d98a8147223cf1a'),
            ):
                sample, captured = self.fixture('reconstruction.obligations', 'json', case)
                self.assertEqual((sample['code'], sample['terminal_state'], sample['launches']),
                                 ('OK', 'fixture_settled', 0))
                self.assertEqual(captured, {'reply': expected, 'index': EMPTY_INDEX_SHA256, 'objects': {}})

            invoke = harness.Session.invoke

            def leave_unexpected_file(session, transport):
                reply = invoke(session, transport)
                (session.host.store.root.parent / 'unexpected').touch()
                return reply

            with (patch.object(harness.Session, 'invoke', leave_unexpected_file),
                  self.assertRaisesRegex(FixtureError, 'Fixture processes, scratch, decode or lease did not settle')):
                harness.run(DEFAULT_MANIFEST, 'reconstruction.obligations', 'nominal', ('json',))
        staged.assert_not_called()

    def test_cli_comparison_reuses_verified_digests_and_authorizes_each_paged_object_once(self):
        readback, authorize = harness.readback, Store._authorized_object
        consumer_checks = []

        def observe_readback(*args):
            checked = Counter()

            def record(store, key, request_id, attempt_id):
                checked[key] += 1
                return authorize(store, key, request_id, attempt_id)

            with patch.object(Store, '_authorized_object', record):
                result = readback(*args)
            consumer_checks.append(dict(checked))
            return result

        baseline = {
            'nominal': {
                'reply': '44b3287a62d53dec9c16602a225c17c54f7285b9bc3911c424b71631503570b0',
                'index': '133509823d3820cbcf2d11fdeb1abe5dd467f0115859f51bf485e5173ce9dbfb',
                'objects': {
                    'report': '30cfff6ad5da02002428bcb1b09bc3cb6729499b5b37f896e2a113df0d750142',
                    'comparison-receipt': '43c101bd857884b1b70b65e3a8ed40e75c4e76fec2507fe31015e79e4bfc2e65',
                },
            },
            'negative': {
                'reply': '045840e1f67d2b62b453bfe7d803e4c3db6bc88459ab52f02f7a7066e81dc6c8',
                'index': '4b49a7d6c4aee7122adafc58ae30320c8ab080f27e9a7d674e46dac5a3409435',
                'objects': {
                    'report': 'f1e260766e9d0645b766450cea4bbdcdef0e8fb28100e542b7c68bf928b35007',
                    'comparison-receipt': '2c097e696234aeebf8c61c34267076deb1fe4b8f29d8ed7b7ac0d9309f1b439c',
                },
            },
        }
        with (patch.object(harness, 'readback', observe_readback),
              patch.object(host, 'sha', wraps=host.sha) as producer_digest):
            for case, verdict in (('nominal', 'match'), ('negative', 'mismatch')):
                sample, captured = self.fixture('behaviour.compare', 'cli', case)
                self.assertEqual((sample['code'], sample['result']['verdict']['result']), ('OK', verdict))
                self.assertEqual(sample['readback']['pages'], 5)
                self.assertEqual(captured, baseline[case])
        self.assertEqual(consumer_checks, [{'report': 1, 'comparison-receipt': 1}] * 2)
        self.assertEqual(producer_digest.call_count, 2)
