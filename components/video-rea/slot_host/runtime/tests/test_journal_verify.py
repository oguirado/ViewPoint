# SPDX-License-Identifier: AGPL-3.0-only
"""Retained journal contrasts through the authenticated offline projector."""

import copy
import hashlib
import io
import json
import struct
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from slot_host.compare._timeline import _events
from slot_host.projections import PublicationError, SlotProjector, control_envelope, generate, verify_publication
from slot_host.projections.cli import main as projected_cli
from slot_host.projections.schema import digest
from slot_host.projections.tests.test_projections import load_registry, minimal_manifest
from slot_host.runtime import tools
from slot_host.runtime.evidence_projection import _check_timeline_side
from slot_host.runtime.replay_lineage import resolve_replay_lineage
from slot_host.runtime.tests._tool_gate import assert_tool_gate

TOOL = 'journal.verify'
ROUTE = 'host.journal.verify'
MCP = 'rea_journal_verify'
CLI = ['slot', 'mini', TOOL, '--args', '<json>']
PROFILE = 'rea.input_journal/v1'
JOURNAL = 'fixture:events'
SEAL = 'fixture:seal'
CONTEXT = {'caller': 'agent', 'agent_instance': 'journal-fixture'}
REQUEST_ID = 'fixture-journal-request'
EVENTS = [
    {'event_id': 'e1', 't_ms': 10, 'kind': 'key_down', 'input_digest': 'c1' * 32},
    {'event_id': 'e2', 't_ms': 40, 'kind': 'pointer_click', 'input_digest': 'c2' * 32},
    {'event_id': 'e3', 't_ms': 250, 'kind': 'key_up', 'input_digest': 'c3' * 32},
]
IDS = tuple(event['event_id'] for event in EVENTS)


def encode(value):
    # This fixture uses only ASCII keys and safe integers.
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def fixture_fields():
    return [
        {'event': dict(event, t_ticks=event['t_ms'] * 2 + 1), 'event_time_base': {'num': 1, 'den': 2000},
         'presentation': {'pts_ms': pts, 'pts_ticks': pts * 3 + 1, 'time_base': {'num': 1, 'den': 3000}}}
        for event, pts in zip(EVENTS, (51, 112, 309))
    ]


def seal_rows(fields):
    root = '0' * 64
    lines = []
    for index, row in enumerate(fields, 1):
        material = [root.encode('ascii'), f'{index}'.encode('ascii'), root.encode('ascii'), encode(row)]
        step_hash = hashlib.sha256(
            b'rea/input-journal/step/v1' + b''.join(struct.pack('>Q', len(field)) + field for field in material)
        ).hexdigest()
        lines.append(encode(dict(row, step_index=index, parent_hash=root, step_hash=step_hash)))
        root = step_hash
    return b'\n'.join(lines) + b'\n', root


class FixtureHost:
    def __init__(self):
        self.raw, self.root = seal_rows(fixture_fields())
        self.refs = (JOURNAL, SEAL)
        self.subject = JOURNAL
        self.receipts = {}
        self.readback_override = None
        self.resolutions = []

    def resolve_journal(self, journal_ref, trusted_seal_ref, *, request_id, max_bytes):
        self.resolutions.append((journal_ref, trusted_seal_ref, request_id, max_bytes))
        if (journal_ref, trusted_seal_ref) != self.refs:
            return None
        from slot_host.runtime.journal_verify import TrustedSeal

        return self.raw, TrustedSeal(self.subject, PROFILE, self.root, IDS)

    def retain_journal_verification(self, request_id, receipt_ref, receipt_bytes):
        self.receipts[receipt_ref] = receipt_bytes
        return self.receipts[receipt_ref] if self.readback_override is None else self.readback_override


class JournalVerifyTests(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry()
        schemas = Path(__file__).resolve().parents[4] / 'vp-direct' / 'schemas'
        for name in ('req', 'reply'):
            schema = json.loads((schemas / f'vp.control.{name}_v1.schema.json').read_bytes())
            self.registry[schema['$id']] = schema
        self.registry.update(copy.deepcopy(tools.ARGUMENT_SCHEMAS))
        self.registry.update(copy.deepcopy(tools.RESULT_SCHEMAS))
        self.registry['host_mini_tools'] = tools.MINI_TOOLS
        self.policy = {'perception_ceiling': 'none'}
        self.manifest = minimal_manifest(registry=self.registry)
        self.bundle = self.generate()
        self.host = FixtureHost()
        self.projector = self.make_projector()

    def generate(self, manifest=None, **options):
        return generate(self.manifest if manifest is None else manifest, policy=self.policy,
                        registry=self.registry, **options)

    def make_projector(self, *, dispatch=True):
        return SlotProjector(self.manifest, self.bundle, policy=self.policy, registry=self.registry,
                             dispatch=self.dispatch if dispatch else None, context=CONTEXT,
                             authenticated_context=CONTEXT)

    def dispatch(self, envelope):
        normalized = self.projector.prepare(envelope, authenticated_context=CONTEXT)
        return tools.handle_journal_control(normalized, host=self.host)

    def args(self, **changes):
        return {'request_id': REQUEST_ID, 'payload': {
            'journal_ref': JOURNAL, 'trusted_seal_ref': SEAL, 'grammar_profile': PROFILE, **changes,
        }}

    def verify(self, **changes):
        reply = self.projector.invoke(ROUTE, self.args(**changes))
        self.assertEqual(reply['code'], 'OK', reply)
        result = reply['result']['payload']
        receipt = self.host.receipts[result['receipt_ref']]
        self.assertEqual(result['receipt_ref'], 'fixture:journal.verify:sha256:' + hashlib.sha256(receipt).hexdigest())
        self.assertEqual(json.loads(receipt)['result'], {k: v for k, v in result.items() if k != 'receipt_ref'})
        return result

    def assert_finding(self, result, structure, seal, completeness, steps=(), evidence=()):
        self.assertEqual(result['structure_verdict'], structure)
        self.assertEqual(result['seal_verdict'], seal)
        self.assertEqual(result['completeness'], completeness)
        self.assertEqual(result['violation_steps'], list(steps))
        self.assertEqual(result['affected_evidence_ids'], list(evidence))

    def test_rehashed_timeline_cannot_hide_the_missing_sealed_event(self):
        tape = '7a' * 32
        parent = 'episode:parent'
        policy = SimpleNamespace(integer_milliseconds=True, kinds={event['kind'] for event in EVENTS})
        for events in (EVENTS, EVENTS[::2]):
            timeline = {'clock_source': 'pts', 'origin': 'episode_start', 'events': events,
                        'canonicalization': 'rea.timeline_digest/v1',
                        'timeline_sha256': hashlib.sha256(encode(events)).hexdigest()}
            _check_timeline_side(timeline, 'fixture')
            _events(SimpleNamespace(record=timeline), policy)
            lineage = resolve_replay_lineage(
                {'parent_episode_id': parent, 'tape_sha256': tape},
                tape_resolver=lambda episode, sha: {'parent_episode_id': episode, 'input_tape_sha256': sha,
                                                    'journal': encode(events)})
            self.assertEqual(lineage['tape_sha256'], tape)

        nominal = self.verify()
        self.assert_finding(nominal, 'valid', 'bound', 'complete')
        original_lines = self.host.raw.splitlines()
        self.host.raw = b'\n'.join(original_lines[::2]) + b'\n'
        self.assert_finding(self.verify(), 'invalid', 'mismatch', 'incomplete', (2,), ('e2',))
        self.host.raw, _ = seal_rows(fixture_fields()[::2])
        changed = self.verify()
        self.assert_finding(changed, 'invalid', 'mismatch', 'incomplete', (2,), ('e2',))
        self.assertNotEqual(nominal['receipt_ref'], changed['receipt_ref'])
        receipt = json.loads(self.host.receipts[changed['receipt_ref']])
        self.assertEqual(receipt['trusted_root_sha256'], self.host.root)
        self.assertNotEqual(receipt['computed_root_sha256'], self.host.root)

    def test_event_time_and_pts_are_distinct_sealed_fields(self):
        self.assert_finding(self.verify(), 'valid', 'bound', 'complete')
        alternate = fixture_fields()
        alternate[1]['presentation'].update(pts_ms=113, pts_ticks=340)
        self.assertEqual(alternate[1]['event']['t_ms'], 40)
        self.host.raw, _ = seal_rows(alternate)
        self.assert_finding(self.verify(), 'valid', 'mismatch', 'complete')
        invalid = fixture_fields()
        invalid[1]['event']['t_ms'] = invalid[1]['presentation']['pts_ms']
        self.host.raw, _ = seal_rows(invalid)
        self.assert_finding(self.verify(), 'invalid', 'unverified', 'unknown', (2,))
        invalid = fixture_fields()
        invalid[1]['presentation']['pts_ticks'] = invalid[1]['event']['t_ticks']
        self.host.raw, _ = seal_rows(invalid)
        self.assert_finding(self.verify(), 'invalid', 'unverified', 'unknown', (2,))

    def test_swapped_events_are_reported_with_an_unexpected_event(self):
        fields = fixture_fields()
        extra = copy.deepcopy(fields[-1])
        extra['event'].update(event_id='e_extra', input_digest='c4' * 32)
        fields[1]['event']['event_id'], fields[2]['event']['event_id'] = 'e3', 'e2'
        self.host.raw, _ = seal_rows([*fields, extra])

        for policy, steps, evidence in (
            ('fail-closed', (2,), ('e2',)),
            ('record-and-continue', (2, 3, 4), ('e2', 'e3')),
        ):
            with self.subTest(policy=policy):
                self.assert_finding(self.verify(diagnostic_policy=policy),
                                    'invalid', 'mismatch', 'complete', steps, evidence)

    def test_swapped_events_are_reported_with_a_missing_event(self):
        fields = fixture_fields()[:2]
        fields[0]['event']['event_id'], fields[1]['event']['event_id'] = 'e3', 'e1'
        self.host.raw, _ = seal_rows(fields)

        for policy, steps, evidence in (
            ('fail-closed', (1,), ('e1',)),
            ('record-and-continue', (1, 2), ('e1', 'e2', 'e3')),
        ):
            with self.subTest(policy=policy):
                self.assert_finding(self.verify(diagnostic_policy=policy),
                                    'invalid', 'mismatch', 'incomplete', steps, evidence)

    def test_bad_chain_and_bad_grammar_never_bind(self):
        lines = self.host.raw.splitlines()
        changed_hash = dict(json.loads(lines[1]), step_hash='a' * 64)
        self.host.raw = b'\n'.join((lines[0], encode(changed_hash), lines[2]))
        self.assert_finding(self.verify(), 'invalid', 'mismatch', 'complete', (2,), ('e2',))
        malformed_records = (b'{"step_index":2,' + lines[1][1:], lines[1].replace(b'"t_ms":40', b'"t_ms":40.0'),
                             lines[1][:-1] + b',"undocumented":true}', b'\xff', b'')
        for malformed in malformed_records:
            with self.subTest(malformed=malformed[:24]):
                self.host.raw = b'\n'.join((lines[0], malformed, b'{}'))
                self.assert_finding(self.verify(), 'invalid', 'unverified', 'unknown', (2,))
                self.assert_finding(self.verify(diagnostic_policy='record-and-continue'),
                                    'invalid', 'unverified', 'unknown', (2, 3))
        self.host.raw = b''
        self.assert_finding(self.verify(), 'invalid', 'mismatch', 'incomplete', (1,), ('e1',))

    def test_tool_gate_four_faces_share_one_control_request(self):
        row = next((row for row in self.bundle['router_rows'] if row['id'] == ROUTE), None)
        self.assertIsNotNone(row, 'the router must publish the host operation')
        self.assertEqual(row['cli_argv_shape'], CLI)
        self.assertEqual(self.bundle['cli_shapes'][ROUTE], CLI)
        self.assertEqual((row['manifest_digest'], row['required_effects'], row['perception']['required_level']),
                         (None, [], 'none'))
        self.assertEqual(tools.ROUTER_ENTRIES[ROUTE]['cli_argv_shape'], row['cli_argv_shape'])
        self.assertIn(MCP, tools.MCP_TOOLS)
        second = self.generate(minimal_manifest('other-slot', registry=self.registry), existing_rows=[row])
        self.assertIn(row, second['router_rows'])
        self.assertIn(MCP, {tool['name'] for tool in self.projector.list_tools()})
        args = self.args()
        cli_command = CLI[:-2]
        with patch.object(tools, 'run_bound', side_effect=AssertionError('unexpected execution')) as run:
            nominal = assert_tool_gate(self, self.projector, TOOL, args, cli_command=cli_command, code='OK')
            complete = nominal['result']['payload']
            self.assert_finding(complete, 'valid', 'bound', 'complete')
            self.assertEqual(tools.dispatch(TOOL, args, host=self.host)['result'], complete)

            self.host.raw, _ = seal_rows(fixture_fields()[::2])
            finding = assert_tool_gate(self, self.projector, TOOL, args, cli_command=cli_command, code='OK')
            incomplete = finding['result']['payload']
            self.assert_finding(incomplete, 'invalid', 'mismatch', 'incomplete', (2,), ('e2',))
            self.assertNotEqual(incomplete['receipt_ref'], complete['receipt_ref'])

            receipts = self.host.receipts.copy()
            assert_tool_gate(self, self.projector, TOOL, self.args(grammar_profile='unregistered'),
                             cli_command=cli_command, code='SCHEMA_INVALID')
            assert_tool_gate(self, self.projector, TOOL, self.args(journal_ref='fixture:absent'),
                             cli_command=cli_command, code='AVAILABILITY_PARKED')
            self.assertEqual(self.host.receipts, receipts)
            run.assert_not_called()

    def test_cli_rejects_the_unpublished_journal_alias_without_dispatch(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch.object(
            self.projector, 'invoke', wraps=self.projector.invoke
        ) as invoke:
            rc = projected_cli(['journal', 'verify', '--args', json.dumps(self.args())], projector=self.projector)
        self.assertEqual(rc, 14)
        invoke.assert_not_called()

    def test_publication_reserves_the_canonical_journal_cli_shape(self):
        collision = {'id': 'host.other', 'cli_argv_shape': CLI}
        for publish in (
            lambda: self.generate(existing_rows=[collision]),
            lambda: verify_publication(self.bundle, self.manifest, policy=self.policy,
                                       registry=self.registry, existing_rows=[collision]),
        ):
            with self.assertRaises(PublicationError) as raised:
                publish()
            self.assertEqual(raised.exception.code, 'COLLISION')

    def test_publication_checks_journal_schema_pins_even_with_an_unresolved_sibling(self):
        descriptor = tools.MINI_TOOLS[TOOL]
        for key in ('argument_schema_ref', 'result_schema_ref'):
            ref = descriptor[key]
            self.assertEqual(descriptor[key + '_sha256'], digest(self.registry[ref]))
            tampered = dict(self.registry[ref], description='Changed without updating the pin.')
            with patch.dict(self.registry, {ref: tampered}):
                with self.assertRaises(PublicationError) as raised:
                    self.generate()
                self.assertEqual(raised.exception.code, 'SCHEMA_INVALID')
                with self.assertRaises(PublicationError) as raised:
                    verify_publication(self.bundle, self.manifest, policy=self.policy, registry=self.registry)
                self.assertEqual(raised.exception.code, 'SCHEMA_INVALID')

        ref = descriptor['result_schema_ref']
        tampered = dict(self.registry[ref], description='Changed without updating the pin.')
        with patch.dict(self.registry, {ref: tampered}):
            del self.registry[descriptor['argument_schema_ref']]
            with self.assertRaises(PublicationError) as raised:
                self.generate()
            self.assertEqual(raised.exception.code, 'SCHEMA_INVALID')

    def test_unresolved_authority_and_readback_park_and_preview_does_not_resolve(self):
        envelope = control_envelope(self.manifest, ROUTE, self.args(), context=CONTEXT)
        self.assertEqual(self.make_projector(dispatch=False).handle_envelope(envelope)['code'], 'AVAILABILITY_PARKED')
        preview = self.projector.invoke(ROUTE, dict(self.args(), dry_run=True))
        self.assertEqual(preview['result']['kind'], 'dry_run')
        self.assertEqual(self.host.resolutions, [])
        self.assertEqual(self.host.receipts, {})
        for invalid in ({'trusted_seal_ref': '/absolute'}, {'trusted_seal': {}},
                        {'grammar_profile': 'unregistered'}, {'diagnostic_policy': 'ignore'}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.projector.invoke(ROUTE, self.args(**invalid))['code'], 'SCHEMA_INVALID')
        self.assertEqual(self.host.resolutions, [])
        for options in ({'trusted_seal_ref': 'fixture:absent'}, {'journal_ref': 'fixture:absent'}):
            reply = self.projector.invoke(ROUTE, self.args(**options))
            self.assertEqual((reply['code'], reply['result']), ('AVAILABILITY_PARKED', {}))
        fixture = self.host
        self.host = None
        self.assertEqual(self.projector.handle_envelope(envelope)['code'], 'AVAILABILITY_PARKED')
        self.host = fixture
        fixture.subject = 'fixture:foreign'
        self.assertEqual(self.projector.handle_envelope(envelope)['code'], 'AVAILABILITY_PARKED')
        fixture.subject = JOURNAL
        self.assertEqual(fixture.receipts, {})
        fixture.readback_override = b'foreign receipt'
        reply = self.projector.handle_envelope(envelope)
        self.assertEqual((reply['code'], reply['result']), ('AVAILABILITY_PARKED', {}))
        fixture.readback_override = None
        max_bytes = fixture.resolutions[-1][3]
        self.assertGreater(max_bytes, len(fixture.raw))
        fixture.raw = b'x' * (max_bytes + 1)
        self.assertEqual(self.projector.handle_envelope(envelope)['code'], 'AVAILABILITY_PARKED')

    def test_publication_rejects_missing_faces_and_collisions(self):
        self.assertIn(ROUTE, self.bundle['json_contracts'])
        for face, key in (('router_rows', ROUTE), ('cli_shapes', ROUTE),
                          ('mcp_tools', MCP), ('json_contracts', ROUTE)):
            with self.subTest(face=face):
                bundle = copy.deepcopy(self.bundle)
                if face == 'router_rows':
                    bundle[face] = [row for row in bundle[face] if row['id'] != key]
                else:
                    del bundle[face][key]
                with self.assertRaises(PublicationError) as raised:
                    verify_publication(bundle, self.manifest, policy=self.policy, registry=self.registry)
                self.assertEqual(raised.exception.code, 'MISSING_PROJECTION')
        collisions = ({'id': ROUTE}, {'id': 'host.other', 'mcp_name': MCP},
                      {'id': 'host.other', 'cli_argv_shape': self.bundle['cli_shapes'][ROUTE]})
        for row in collisions:
            with self.subTest(row=row), self.assertRaises(PublicationError) as raised:
                self.generate(existing_rows=[row])
            self.assertEqual(raised.exception.code, 'COLLISION')
        del self.registry[tools.MINI_TOOLS[TOOL]['result_schema_ref']]
        with self.assertRaises(PublicationError) as raised:
            self.generate()
        self.assertEqual(raised.exception.code, 'MISSING_PROJECTION')
