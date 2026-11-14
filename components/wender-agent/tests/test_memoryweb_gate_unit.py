# @module {"id":"wa.tests.memoryweb_gate_unit","layer":0,"owner":"ARCH","deps":["wa.tools.memoryweb_gate"],"book":"HB-WA-a0-arch"}
"""Synthetic offline controls; acceptance never authorizes execution."""
import ast
import contextlib
import copy
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from tools import memoryweb_gate as g

BUNDLE = json.loads((g.DOCS / 'mw.v1.schema.json').read_bytes())
DEFS = BUNDLE['$defs']


def _minimal(schema):
    if '$ref' in schema:
        return _minimal(DEFS[schema['$ref'][8:]])
    if 'const' in schema:
        return schema['const']
    if 'enum' in schema:
        return schema['enum'][0]
    if 'anyOf' in schema or 'oneOf' in schema:
        branches = schema.get('anyOf', schema.get('oneOf'))
        nulls = [s for s in branches if s.get('type') == 'null']
        return _minimal((nulls or branches)[0])
    kind = schema['type']
    if kind == 'null':
        return None
    if kind == 'boolean':
        return False
    if kind == 'integer':
        return schema.get('minimum', 0)
    if kind == 'array':
        return [_minimal(schema['items']) for _ in range(schema.get('minItems', 0))]
    if kind == 'object':
        return {k: _minimal(schema['properties'][k]) for k in schema.get('required', [])}
    if 'pattern' in schema:
        pattern = schema['pattern']
        if '{64}' in pattern:
            return 'a' * 64
        if '{4}' in pattern:
            return '2024-02-29'
        if '#[a-z' in pattern:
            return 'source.md#root'
    return 'x' * max(1, schema.get('minLength', 0))


def _fixture(root):
    value = _minimal(DEFS[root])
    if root == 'demo_request':
        value['requested_explanation_topics'] = ['intent', 'starting_state', 'variable_slots', 'ambiguous_controls', 'expected_result', 'exceptions']
    return value


def _pred():
    return dict(kind='state_signal', capability_id='synthetic-v1', bindings=[], evidence_refs=[],
                comparison='truth', snapshot_ref=None)


def _verify():
    v = _fixture('step')['verify']
    v.update(mode='state_signal', predicate=_pred(), deadline_ms=100, poll_interval_ms=10,
             consecutive_polls=2)
    return v


def _step():
    s = _fixture('step')
    s.update(verify=_verify(), locate_poll_ms=1)
    s['action'].update(kind='wait_signal')
    return s


def _explanation():
    return ''.join('## ' + h + '\nOperator scoped evidence for ' + h.lower() + '.\n\n'
                   for h in g.HEADINGS).encode()


def _accept(*args):
    return g.GateResult(0, 'PASS')


def _deny(*args):
    return g.GateResult(5, 'DENY')


# @anchor:wa.tests.memoryweb_gate_unit.TestGate
class TestGate(unittest.TestCase):
    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.setUp
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = g.ValidationContext(self.root, capability_check=_accept,
                    evidence_check=_accept, review_check=_accept, chain_check=_accept)

    def _write(self, value, name='x.json'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((g.canon(value) + '\n').encode())
        return path

    def _validate(self, value, name='x.json', ctx=None):
        return g.validate_file(self._write(value, name), BUNDLE, context=ctx or self.ctx)

    def _assert(self, result, category='PASS', rc=None):
        self.assertEqual(result.category, category)
        self.assertEqual(result.rc, (0 if category == 'PASS' else 5) if rc is None else rc)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_protocol_schema_parity
    def test_protocol_schema_parity(self):
        raw = (g.DOCS / 'MEMORYWEB_PROTOCOL.md').read_bytes()
        schema = (g.DOCS / 'mw.v1.schema.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), g.PROTOCOL_PIN)
        extracted = re.search(rb'```json\n(\{\n  "\$schema".*?\n\})\n```', raw, re.S)[1] + b'\n'
        self.assertEqual(extracted, schema)
        self.assertEqual(hashlib.sha256(schema).hexdigest(), g.SCHEMA_PIN)
        self.assertNotIn('anchor_span', DEFS)
        altered = copy.deepcopy(BUNDLE)
        altered['$defs']['caps']['required'].remove('network')
        self.assertNotEqual(g.canon(altered), g.canon(BUNDLE))
        p, e = self.root / 'MEMORYWEB_PROTOCOL.md', self.root / 'MEMORYWEB_ENTRY.md'
        p.write_bytes(raw)
        e.write_bytes((g.DOCS / e.name).read_bytes())
        self._assert(g.entry_check(e, p))
        for changed in (raw + b' ', raw.replace(b'\n', b'\r\n'), raw.replace(b'<a id="protocol-index"></a>', b'')):
            p.write_bytes(changed)
            self._assert(g.entry_check(e, p), 'PROTOCOL_PIN', 3)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_entry_controls
    def test_entry_controls(self):
        p, e = self.root / 'MEMORYWEB_PROTOCOL.md', self.root / 'ENTRY.md'
        p.write_bytes((g.DOCS / p.name).read_bytes())
        entry = (g.DOCS / 'MEMORYWEB_ENTRY.md').read_bytes()
        for data, category, rc in ((entry, 'PASS', 0), (entry + b' ' * 601, 'CAPACITY', 5),
                (entry.replace(g.PROTOCOL_PIN.encode(), b'0' * 64), 'ENTRY_PIN', 3),
                (entry.replace(b'#protocol-index', b'#dead'), 'ENTRY_TARGET', 3)):
            e.write_bytes(data)
            self._assert(g.entry_check(e, p), category, rc)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_root_selection
    def test_root_selection(self):
        self.assertEqual(len(g.ROOTS), 16)
        self.assertFalse(g.conforms(_fixture('caps'), BUNDLE, DEFS))
        for root in sorted(g.ROOTS):
            with self.subTest(root=root):
                self.assertTrue(g.conforms(_fixture(root), DEFS[root], DEFS))
                bad = _fixture(root)
                del bad['schema']
                self.assertFalse(g.conforms(bad, DEFS[root], DEFS))
        self._assert(self._validate({'schema': 'mw.unknown.v1'}), 'ROOT', 2)
        self._assert(g.validate_file(self._write(BUNDLE), BUNDLE, context=self.ctx), 'ROOT', 2)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_strict_parsing
    def test_strict_parsing(self):
        good = {'integer': 3, 'unicode': 'niño 😀'}
        self.assertEqual(g.strict_load(g.canon(good).encode()), good)
        for raw in (b'{"a":1,"a":2}', b'1.0', b'NaN', b'Infinity', b'-Infinity', b'"\xff"',
                    b'"\\ud800"', b'{"\\udfff":0}'):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                g.strict_load(raw)
            path = self.root / 'invalid.json'; path.write_bytes(raw)
            category = ('JSON_DUPLICATE_KEY' if raw.startswith(b'{"a"') else
                        'JSON_UNICODE' if b'\\ud' in raw else
                        'SCHEMA' if b'\xff' in raw else 'JSON_NUMBER')
            self._assert(g.validate_file(path, BUNDLE, context=self.ctx), category, 2)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_exact_types_and_composition
    def test_exact_types_and_composition(self):
        for kind, good, bad in (('integer', 1, True), ('boolean', True, 1), ('array', [1], 'integer_box')):
            s = {'type': kind}
            self.assertTrue(g.conforms(good, s, DEFS))
            self.assertFalse(g.conforms(bad, s, DEFS))
        self.assertFalse(g.conforms(True, {'const': 1}, DEFS))
        self.assertFalse(g.conforms(True, {'enum': [1]}, DEFS))
        self.assertTrue(g.conforms(1, {'oneOf': [{'type': 'integer'}, {'type': 'string'}]}, DEFS))
        self.assertFalse(g.conforms(None, {'oneOf': [{'type': 'integer'}, {'type': 'string'}]}, DEFS))
        self.assertFalse(g.conforms(1, {'oneOf': [{'type': 'integer'}, {'const': 1}]}, DEFS))
        self.assertFalse(g.conforms([], {'anyOf': [{'type': 'integer'}, {'type': 'string'}]}, DEFS))
        self.assertFalse(g.conforms([1, 1], {'type': 'array', 'uniqueItems': True}, DEFS))
        self.assertTrue(g.conforms([1, True], {'type': 'array', 'uniqueItems': True}, DEFS))
        self.assertFalse(g.conforms(0, {'minimum': 1}, DEFS))
        self.assertFalse(g.conforms(3, {'maximum': 2}, DEFS))
        self.assertFalse(g.conforms('a', {'minLength': 2}, DEFS))
        self.assertFalse(g.conforms('abc', {'maxLength': 2}, DEFS))
        self.assertFalse(g.conforms([], {'minItems': 1}, DEFS))
        self.assertFalse(g.conforms([1, 2], {'maxItems': 1}, DEFS))

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_schema_compilation
    def test_schema_compilation(self):
        self.assertEqual(g.compile_bundle(BUNDLE), DEFS)
        mutations = [{'unknown': 1}, {'$ref': 'https://example.com/a'}, {'$ref': '#/$defs/missing'},
            {'$ref': '#/$defs/caps', 'description': 'sibling'}, {'type': 'number'}, {'type': []},
            {'pattern': '['}, {'pattern': 1}, {'oneOf': []}, {'anyOf': {}}, {'items': True},
            {'required': ['x', 'x']}, {'required': [1]}, {'properties': []},
            {'additionalProperties': {}}, {'uniqueItems': 1}, {'enum': []},
            {'minimum': True}, {'minItems': -1}, {'minLength': '1'}, {'minimum': 2, 'maximum': 1}]
        for changed in mutations:
            b = copy.deepcopy(BUNDLE)
            b['$defs']['bad'] = changed
            with self.subTest(changed=changed), self.assertRaises((ValueError, re.error)):
                g.compile_bundle(b)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_depth_capacity
    def test_depth_capacity(self):
        value = b'[' * 64 + b'0' + b']' * 64
        self.assertIsInstance(g.strict_load(value), list)
        with self.assertRaises(ValueError):
            g.strict_load(b'[' + value + b']')
        self.assertTrue(g.conforms(1, {'type': 'integer'}, DEFS, depth=64))
        with self.assertRaises(ValueError):
            g.conforms(1, {'type': 'integer'}, DEFS, depth=65)
        p = self._write(_fixture('caps'))
        p.write_bytes(b' ' * (g.MAX_FILE + 1))
        self._assert(g.validate_file(p, BUNDLE), 'CAPACITY')
        p.unlink()
        self._write(_fixture('caps'))
        self._assert(g.validate_directory(self.root, BUNDLE))
        with patch.object(g, 'MAX_FILES', 0):
            self._assert(g.validate_directory(self.root, BUNDLE), 'CAPACITY')
        with patch.object(g, 'MAX_BYTES', 1):
            self._assert(g.validate_directory(self.root, BUNDLE), 'CAPACITY')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_pattern_guards
    def test_pattern_guards(self):
        for root, value in (('value_3', 'valid-id'), ('tag', 'ui_tag'), ('value_6', 'a' * 64),
                            ('value_7', '2024-02-29'), ('hostname', 'example.com')):
            self.assertTrue(g.conforms(value, DEFS[root], DEFS))
            self.assertFalse(g.conforms(value + '\n', DEFS[root], DEFS))
        schema = DEFS['node']['properties']['anchor']
        self.assertTrue(g.conforms('source.md#root', schema, DEFS))
        self.assertFalse(g.conforms('source.md#root\n', schema, DEFS))

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_canonical_bytes
    def test_canonical_bytes(self):
        p = self._write(_fixture('caps'))
        self._assert(g.validate_file(p, BUNDLE))
        canonical = p.read_bytes()
        for raw in (canonical[:-1], json.dumps(_fixture('caps'), indent=2).encode() + b'\n',
                    json.dumps(dict(reversed(list(_fixture('caps').items())))).encode() + b'\n'):
            p.write_bytes(raw)
            self._assert(g.validate_file(p, BUNDLE), 'CANONICAL', 2)
        v = _fixture('caps'); v['extra'] = 'private'
        self._assert(self._validate(v), 'SCHEMA', 2)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_anchor_spans
    def test_anchor_spans(self):
        raw = (b'<a id="root"></a>\n# Root\nbody\n<a id="child"></a>\n\n## Child\n'
               b'```\n# ignored\n<a id="ignored"></a>\n```\n'
               b'<a id="sibling"></a>\n# Sibling\nEOF')
        self.assertEqual(g.extract_span(raw, 'root'), raw[:raw.index(b'<a id="sibling"')])
        self.assertEqual(g.extract_span(raw, 'child'), raw[raw.index(b'<a id="child"'):raw.index(b'<a id="sibling"')])
        self.assertEqual(g.extract_span(raw, 'sibling'), b'<a id="sibling"></a>\n# Sibling\nEOF')
        self.assertEqual(g.extract_span(b'<a id="x"></a>\n## X\n<a id="y"></a>\n## Y\n', 'x'), b'<a id="x"></a>\n## X\n')
        for bad in (b'<a id="x"></a>\nbody\n# X', b'<a id="x"></a>\nbody', b'<a id="x"></a>\n## X\n<a id="x"></a>\n## Y',
                    raw.replace(b'\n', b'\r\n'), b'inline <a id="x"></a>\n# X',
                    b'<a id="x"></a>\n<a id="y"></a>\n# Y'):
            category = ('LINE_ENDINGS' if b'\r' in bad else
                        'ANCHOR_MISSING' if bad.startswith(b'inline') else
                        'ANCHOR_DUPLICATE' if bad.count(b'<a id=') > 1 else 'ANCHOR_UNATTACHED')
            with self.subTest(bad=bad), self.assertRaises(g._Refusal) as caught:
                g.extract_span(bad, 'x')
            self.assertEqual(caught.exception.category, category)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_explanation
    def test_explanation(self):
        path = self.root / 'EXPLANATION.md'
        path.write_bytes(_explanation())
        self._assert(g.check_explanation(path))
        good = _explanation()
        invalid = [good.replace(b'## Intent', b'## Wrong'), good + b'## Intent\nDuplicate\n',
                   good.replace(b'## Intent', b'## Starting state', 1),
                   good.replace(b'Operator scoped evidence for intent.', b''),
                   b'```markdown\n' + good + b'```\n',
                   b''.join(('## ' + h + '\nTODO\n').encode() for h in g.HEADINGS),
                   b''.join(('## ' + h + '\nDescribe the task and why it is being performed.\n').encode() for h in g.HEADINGS)]
        for bad in invalid:
            path.write_bytes(bad)
            self._assert(g.check_explanation(path), 'EXPLANATION', 2)
        path.write_bytes(good + b' ' * 16385)
        self._assert(g.check_explanation(path), 'CAPACITY')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_action_shapes
    def test_action_shapes(self):
        s = _step()
        self._assert(self._validate(s))
        s['action'].update(kind='click')
        self._assert(self._validate(s), 'ACTION_TARGET')
        s['target'] = {'rung': 'T3', 'predicate': _pred()}
        self._assert(self._validate(s), 'ACTION_TARGET')
        s['target'] = _minimal(DEFS['t2'])
        s['target']['rel_box_bps'] = [0, 0, 100, 100]
        self._assert(self._validate(s))
        s['action']['text_slot'] = 'unrelated'
        self._assert(self._validate(s), 'ACTION_FIELDS')
        s['action'].update(text_slot=None, kind='paste')
        self._assert(self._validate(s), 'ACTION_SLOT')
        s['action'].update(kind='drag')
        self._assert(self._validate(s), 'ACTION_DRAG')
        s['action'].update(dx_px=-10, dy_px=20)
        self._assert(self._validate(s))
        s['action']['destination'] = copy.deepcopy(s['target'])
        self._assert(self._validate(s), 'ACTION_DRAG')
        s['action'].update(dx_px=None, dy_px=None)
        self._assert(self._validate(s))

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_focus_and_slots
    def test_focus_and_slots(self):
        s = _step(); s['action'].update(kind='key', key_name='ENTER')
        self._assert(self._validate(s), 'ACTION_FOCUS')
        s['preconditions'] = [_pred()]
        self._assert(self._validate(s))
        self._assert(self._validate(s, ctx=replace(self.ctx, evidence_check=None)), 'FOCUS_EVIDENCE_MISSING')
        (self.root / 'source.md').write_bytes(_explanation())
        w = _fixture('workflow'); w['steps'] = [_step()]
        w['provenance']['source_ref'] = 'source.md'
        w['text_slots'] = [_minimal(DEFS['slot'])]
        w['steps'][0]['action'].update(kind='paste', text_slot=w['text_slots'][0]['slot_id'])
        w['steps'][0]['preconditions'] = [_pred()]
        self._assert(self._validate(w))
        w['steps'][0]['action']['text_slot'] = 'missing'
        self._assert(self._validate(w), 'ACTION_SLOT')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_snapshot_and_verifier
    def test_snapshot_and_verifier(self):
        missing = _step(); del missing['verify']
        self._assert(self._validate(missing), 'SCHEMA', 2)
        s = _step()
        s['capture_before'] = [{'name': 'before', 'observe': _pred()}]
        s['verify']['predicate'].update(comparison='changed', snapshot_ref='before')
        self._assert(self._validate(s))
        s['verify']['predicate']['snapshot_ref'] = 'future'
        self._assert(self._validate(s), 'SNAPSHOT')
        s['verify']['predicate']['snapshot_ref'] = 'before'
        s['capture_before'][0]['observe']['capability_id'] = 'incompatible'
        self._assert(self._validate(s), 'SNAPSHOT_COMPATIBILITY')
        s['capture_before'][0]['observe']['capability_id'] = 'synthetic-v1'
        s['capture_before'] *= 2
        self._assert(self._validate(s), 'CAPTURE')
        s = _step(); s['verify']['consecutive_polls'] = 11
        self._assert(self._validate(s), 'POLLING')
        s = _step(); s['verify']['predicate'] = None
        self._assert(self._validate(s), 'VERIFIER_PREDICATE')
        s = _step(); s['verify']['mode'] = 'after_crop'
        self._assert(self._validate(s), 'RAM_EVIDENCE_MISSING')
        s['verify']['ram_evidence_ref'] = 'live'
        self._assert(self._validate(s))
        self._assert(self._validate(s, ctx=replace(self.ctx, evidence_check=None)), 'RAM_EVIDENCE_MISSING')
        s = _step(); fallback = copy.deepcopy(s['verify']); s['verify']['fallback_verifiers'] = [fallback]
        self._assert(self._validate(s))
        fallback['deadline_ms'] = 200
        self._assert(self._validate(s), 'VERIFIER_EQUIVALENCE')
        fallback['deadline_ms'] = 100
        fallback['fallback_verifiers'] = [_verify()]
        self._assert(self._validate(s), 'VERIFIER_DEPTH')
        s = _step(); s.update(protected=True, when=_pred())
        self._assert(self._validate(s), 'PROTECTED')
        s['when'] = None
        self._assert(self._validate(s))

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_bindings_capabilities_geometry
    def test_bindings_capabilities_geometry(self):
        s = _step()
        p = s['verify']['predicate']
        for kind, value, bad in [('integer', 1, True), ('boolean', True, 1),
                                  ('string', 'text', 1), ('integer_box', [0, 0, 10, 10], 'integer_box')]:
            p['bindings'] = [dict(name='v', type=kind, value=value)]
            self._assert(self._validate(s))
            p['bindings'][0]['value'] = bad
            self._assert(self._validate(s), 'BINDING', 2)
        p['bindings'] = []
        self._assert(self._validate(s, ctx=replace(self.ctx, capability_check=None)), 'CAPABILITY_MISSING')
        self._assert(self._validate(s, ctx=replace(self.ctx, capability_check=_deny)), 'EVIDENCE_DENIED')
        for cap in ('unknown-version', 'unknown-capability', 'unknown-parameter'):
            p['capability_id'] = cap
            owner = lambda requirement, predicate: _deny() if requirement.get('capability_id', '').startswith('unknown') else _accept()
            self._assert(self._validate(s, ctx=replace(self.ctx, capability_check=owner)), 'EVIDENCE_DENIED')
        s = _step(); s['verify']['change_box_px'] = [10, 0, 1, 5]
        self._assert(self._validate(s), 'GEOMETRY')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_owner_failure_privacy
    def test_owner_failure_privacy(self):
        def raises(*args):
            raise RuntimeError('planted-owner-secret')
        step = _step()
        self._assert(self._validate(step, ctx=replace(self.ctx, capability_check=raises)), 'EVIDENCE_OWNER')
        self._assert(self._validate(step))
        def bool_result(*args):
            return g.GateResult(False, 'PASS')
        self._assert(self._validate(step, ctx=replace(self.ctx, capability_check=bool_result)), 'EVIDENCE_DENIED')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_requests
    def test_requests(self):
        v = _fixture('demo_request')
        v['requested_explanation_topics'] = ['intent', 'starting_state', 'variable_slots', 'ambiguous_controls', 'expected_result', 'exceptions']
        v['router_reason'] = 'no_query_term'
        self._assert(self._validate(v))
        for key in ('trigger_reason', 'current_safe_state'):
            bad = copy.deepcopy(v); del bad[key]
            self._assert(self._validate(bad), 'SCHEMA', 2)
        v['router_reason'] = 'exact_id'
        self._assert(self._validate(v), 'REQUEST_LINK')
        v['router_reason'] = 'no_query_term'
        self._assert(self._validate(v, ctx=replace(self.ctx, evidence_check=None)), 'EVIDENCE_MISSING')
        self._assert(self._validate(v, ctx=replace(self.ctx, evidence_check=_deny)), 'EVIDENCE_DENIED')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_publication_privacy
    def test_publication_privacy(self):
        v = _fixture('publication_receipt')
        self._assert(self._validate(v))
        for field in ('mode', 'evaluation_profile_sha256', 'cohort_task_id'):
            bad = copy.deepcopy(v); del bad[field]
            self._assert(self._validate(bad), 'SCHEMA', 2)
        self._assert(self._validate(v, ctx=replace(self.ctx, review_check=None)), 'REVIEW_MISSING')
        self._assert(self._validate(v, ctx=replace(self.ctx, review_check=_deny)), 'EVIDENCE_DENIED')
        for secret in ('password=planted', '/home/example/private', 'data:image/png;base64,PLANTED',
                       'a' * 64, 'session-identifier', 'Traceback (most recent call last)',
                       'https://private.invalid/?token=planted'):
            bad = copy.deepcopy(v); bad['public_evidence_refs'] = [secret]
            self._assert(self._validate(bad), 'PRIVACY')
        v['cohort_task_id'] = 'public-task'
        self._assert(self._validate(v), 'PUBLICATION_COHORT')
        v.update(evaluation_profile_sha256='b' * 64, mode='shadow')
        self._assert(self._validate(v))
        self._assert(self._validate(v, ctx=replace(self.ctx, evidence_check=_deny)), 'EVIDENCE_DENIED')
        p = self._write(v); p.write_bytes(p.read_bytes() + b'\x00')
        self._assert(g.validate_file(p, BUNDLE, context=self.ctx), 'TEXT')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_public_duplicate_cohort
    def test_public_duplicate_cohort(self):
        v = _fixture('publication_receipt')
        v.update(mode='shadow', evaluation_profile_sha256='b' * 64, cohort_task_id='public-task')
        first = (g.canon(v) + '\n').encode()
        path = self.root / 'receipts.jsonl'; path.write_bytes(first)
        self._assert(g.validate_file(path, BUNDLE, context=self.ctx))
        v.update(seq=2, prev_receipt_sha256='a' * 64)
        path.write_bytes(first + (g.canon(v) + '\n').encode())
        self._assert(g.validate_file(path, BUNDLE, context=self.ctx), 'PUBLICATION_COHORT')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_journal
    def test_journal(self):
        v = _fixture('publication_receipt')
        one = (g.canon(v) + '\n').encode()
        p = self.root / 'receipts.jsonl'; p.write_bytes(one)
        self._assert(g.validate_file(p, BUNDLE, context=self.ctx))
        self._assert(g.validate_file(p, BUNDLE, context=replace(self.ctx, chain_check=None)), 'CHAIN_MISSING')
        p.write_bytes(one[:-1])
        self._assert(g.validate_file(p, BUNDLE, context=self.ctx), 'TORN', 3)
        v['seq'] = 3; v['prev_receipt_sha256'] = 'a' * 64
        p.write_bytes(one + (g.canon(v) + '\n').encode())
        self._assert(g.validate_file(p, BUNDLE, context=self.ctx), 'SEQUENCE', 3)
        v['seq'] = 2; v['prev_receipt_sha256'] = None
        p.write_bytes(one + (g.canon(v) + '\n').encode())
        self._assert(g.validate_file(p, BUNDLE, context=self.ctx), 'PREDECESSOR', 3)
        v['prev_receipt_sha256'] = 'b' * 64
        p.write_bytes(one + (g.canon(v) + '\n').encode())
        owner = lambda records, raws: _accept() if records[1]['prev_receipt_sha256'] == hashlib.sha256(raws[0]).hexdigest() else _deny()
        self._assert(g.validate_file(p, BUNDLE, context=replace(self.ctx, chain_check=owner)), 'EVIDENCE_DENIED')
        v['prev_receipt_sha256'] = hashlib.sha256(one).hexdigest()
        p.write_bytes(one + (g.canon(v) + '\n').encode())
        self._assert(g.validate_file(p, BUNDLE, context=replace(self.ctx, chain_check=owner)))

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_private_journal_budget
    def test_private_journal_budget(self):
        value = _fixture('run')
        path = self.root / 'runs.jsonl'; path.write_bytes((g.canon(value) + '\n').encode())
        self._assert(g.validate_file(path, BUNDLE, context=self.ctx), 'JOURNAL_BUDGET_MISSING')
        view = dict(workflow_sha256=value['workflow_sha256'], steps=[], required_segments={}, final_task_pass=True)
        ctx = replace(self.ctx, journal_read_budget=4096, evidence_load=lambda *a: view)
        self._assert(g.validate_file(path, BUNDLE, context=ctx))
        for budget in (0, -1, True):
            self._assert(g.validate_file(path, BUNDLE, context=replace(ctx, journal_read_budget=budget)), 'JOURNAL_BUDGET_MISSING')
        self._assert(g.validate_file(path, BUNDLE, context=replace(ctx, journal_read_budget=1)), 'CAPACITY')
        # Public receipt streams retain their explicitly prescribed byte budget.
        value = _fixture('publication_receipt')
        path.write_bytes((g.canon(value) + '\n').encode())
        self._assert(g.validate_file(path, BUNDLE, context=self.ctx))

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_counts
    def test_counts(self):
        v = _fixture('run')
        view = dict(workflow_sha256=v['workflow_sha256'], steps=[], required_segments={}, final_task_pass=True)
        ctx = replace(self.ctx, evidence_load=lambda *args: view)
        self._assert(self._validate(v, ctx=ctx))
        self._assert(self._validate(v), 'EVIDENCE_MISSING')
        v.update(steps_total=2, steps_verified=1, steps_skipped_satisfied=1, reproduced_ppm=1000000)
        view.update(steps=[dict(seq=1, disposition='verified', predicate_pass=True),
                           dict(seq=2, disposition='satisfied_skip', predicate_pass=True)],
                    required_segments={'s1': [1], 's2': [2]})
        self._assert(self._validate(v, ctx=ctx))
        for pin in ('0'*64, '1'*64, '2'*64):
            bad = {**view, 'workflow_sha256': pin}
            self._assert(self._validate(v, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'RUN_PIN', 3)
        for segments in ({'s': []}, {'s': [99]}, None):
            bad = {**view, 'required_segments': segments}
            self._assert(self._validate(v, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'REPRODUCTION_EVIDENCE')
        for bad_steps in ([], [view['steps'][0]] * 2, [dict(seq=1, disposition='verified'), view['steps'][1]]):
            bad = {**view, 'steps': bad_steps}
            self._assert(self._validate(v, ctx=replace(ctx, evidence_load=lambda *a: bad)),
                         'COUNTS_EVIDENCE' if len(bad_steps) == 2 and bad_steps[0].get('predicate_pass') is None else 'COUNTS_INVENTORY')
        for metric in (None, 0, 999999):
            self._assert(self._validate({**v, 'reproduced_ppm': metric}, ctx=ctx), 'REPRODUCTION')
        view['steps'][1].update(disposition='condition_skip', optional=True, guard_false=True)
        v.update(steps_skipped_satisfied=0, steps_skipped_condition=1, reproduced_ppm=1000000)
        view['required_segments'] = {'s1': [1]}
        self._assert(self._validate(v, ctx=ctx))
        view['steps'][1]['guard_false'] = False
        self._assert(self._validate(v, ctx=ctx), 'COUNTS_EVIDENCE')
        view['steps'][1]['guard_false'] = True
        view['final_task_pass'] = False
        self._assert(self._validate(v, ctx=ctx), 'FINAL_ORACLE')
        v.update(steps_failed=1, steps_skipped_condition=0)
        self._assert(self._validate(v, ctx=ctx), 'COUNTS')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_native_translation
    def test_native_translation(self):
        for code, reason, rc in ((0, 'OK', None), (2, 'usage', 2), (3, 'reject', 5),
                                 (4, 'stale', 3), (5, 'gate', 5), (6, 'budget', 5)):
            result = g.translate_native('vision-engine', 'observe', code, reason)
            self.assertEqual(result.rc, rc)
            self.assertEqual(result.native_reason, reason)
            self.assertNotEqual(result.disposition, 'VERIFIED')
        self.assertEqual(g.translate_native('wender-agent', 'reseal-check', 4, 'stale').rc, 3)
        for code in (4, 6):
            self.assertEqual(g.translate_native('producer', 'read', code, 'render').disposition, 'retry_read')
            self.assertEqual(g.translate_native('producer', 'read', code, 'render', retries_exhausted=True).rc, 5)
        for source, operation, reason in [('wrapper', 'STOP', 'STOP'), ('vision-engine', 'observe', 'unknown'),
                                          ('unknown', 'observe', 'OK'), ('vision-engine', 'input', 'OK')]:
            self.assertEqual(g.translate_native(source, operation, 0, reason).rc, 5)
            self.assertEqual(g.translate_native(source, operation, 0, reason, observed_human_boundary=True).rc, 4)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_cli_privacy
    def test_cli_privacy(self):
        p = self._write(_fixture('caps'))
        for args, expected in ((['validate', str(p)], 0), (['entry-check'], 0),
                               (['unknown', 'planted-secret'], 2), (['validate', str(self.root / 'absent')], 5)):
            result = subprocess.run([sys.executable, '-B', str(g.DOCS.parents[1] / 'tools/memoryweb_gate.py'), *args],
                                    cwd='/', capture_output=True, timeout=10)
            self.assertEqual(result.returncode, expected, result.stdout)
            out = json.loads(result.stdout)
            self.assertEqual(set(out), {'rc', 'category'})
            self.assertLess(len(result.stdout), 128)
            self.assertNotIn(b'planted-secret', result.stdout + result.stderr)
            self.assertNotIn(str(self.root).encode(), result.stdout + result.stderr)
        p.write_bytes(b'{"password":"planted-secret"}')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertNotEqual(g.main(['validate', str(p)]), 0)
        self.assertNotIn('planted-secret', out.getvalue())

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_containment_unclassified
    def test_containment_unclassified(self):
        p = self._write(_fixture('caps'))
        self._assert(g.validate_file(p, BUNDLE, context=self.ctx))
        outside = self.root.parent / ('not-created-' + self.root.name)
        self._assert(g.validate_file(outside, BUNDLE, context=self.ctx), 'CONTAINMENT')
        link = self.root / 'link.json'; link.symlink_to(p)
        self._assert(g.validate_file(link, BUNDLE, context=self.ctx), 'CONTAINMENT')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), 'CONTAINMENT')
        link.unlink()
        self._write({'unsupported': True}, 'unknown.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), 'ROOT', 2)
        (self.root / 'unknown.json').unlink()
        (self.root / 'payload.bin').write_bytes(b'not executed')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), 'UNCLASSIFIED', 2)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_workflow_draft_and_admission
    def test_workflow_draft_and_admission(self):
        (self.root / 'source.md').write_bytes(_explanation())
        w = _fixture('workflow')
        w['provenance']['source_ref'] = 'source.md'
        w['steps'] = [_step()]
        w['oracle']['postconditions'] = [_pred()]
        self._assert(self._validate(w))
        w['status'] = 'shadow'
        self._assert(self._validate(w), 'ADMISSION')
        w['oracle'].update(planted_mutant_ids=['m1', 'm2', 'm3'], mutants_total=3,
                           mutants_killed=3, receipt_refs=['synthetic-receipt'])
        self._assert(self._validate(w), 'EVIDENCE_MISSING')
        for ids, total, killed, category in [(['m1', 'm1', 'm2'], 3, 3, 'MUTANTS'),
                (['m1', 'm2', 'm3'], 3, 2, 'ADMISSION'), (['m1', 'm2'], 2, 2, 'ADMISSION')]:
            bad = copy.deepcopy(w)
            bad['oracle'].update(planted_mutant_ids=ids, mutants_total=total, mutants_killed=killed)
            self._assert(self._validate(bad), category)
        w['status'] = 'draft'
        w['evaluation_profile']['kind'] = 'paired_replacement_v1'
        self._assert(self._validate(w), 'BASELINE')
        w.update(baseline_id='incumbent')
        w['evaluation_profile']['baseline_workflow_sha256'] = 'a' * 64
        self._assert(self._validate(w))
        w['evaluation_profile']['kind'] = 'initial_task_v1'
        self._assert(self._validate(w), 'BASELINE')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_script_dependency
    def test_script_dependency(self):
        (self.root / 'source.md').write_bytes(_explanation())
        w = _fixture('workflow')
        w['provenance']['source_ref'] = 'source.md'
        w['steps'] = [_step()]
        w['oracle']['postconditions'] = [_pred()]
        w['script_ref'] = 'scripts/dependency.py'
        self._assert(self._validate(w), 'SCRIPT_PAIR')
        caps = g.canon(_fixture('caps')).encode()
        source = b'#!/usr/bin/python3\n# MW-CAPS: ' + caps + b'\nraise RuntimeError("inert dependency")\n'
        script = self.root / w['script_ref']; script.parent.mkdir(); script.write_bytes(source)
        w['script_sha256'] = hashlib.sha256(source).hexdigest()
        self._assert(self._validate(w))
        for content in (b'# missing caps\n', source + b'# MW-CAPS: ' + caps + b'\n',
                        source.replace(caps, caps + b' ')):
            script.write_bytes(content); w['script_sha256'] = hashlib.sha256(content).hexdigest()
            self.assertNotEqual(self._validate(w).rc, 0)
        script.write_bytes(source); w['script_sha256'] = '0' * 64
        self._assert(self._validate(w), 'SCRIPT_PIN', 3)
        for reference in ('../outside.py', '/absolute.py', 'dependency.py'):
            w['script_ref'] = reference
            self._assert(self._validate(w), 'SCRIPT_PATH')

    def _node(self, identity='x'):
        raw = b'<a id="root"></a>\n## Root\nEvidence.\n<a id="other"></a>\n## Other\nDifferent.\n'
        owner = self.root / 'sites/owner'; owner.mkdir(parents=True, exist_ok=True)
        (owner / 'source.md').write_bytes(raw)
        (owner / 'EXPLANATION.md').write_bytes(_explanation())
        n = _fixture('node')
        n.update(id=identity, owner_id='owner', owner_kind='site', anchor='source.md#root',
                 source_sha256=hashlib.sha256(g.extract_span(raw, 'root')).hexdigest())
        n['provenance']['source_ref'] = 'EXPLANATION.md'
        return n

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_dates_identity_and_span_pins
    def test_dates_identity_and_span_pins(self):
        n = self._node()
        name = 'sites/owner/nodes/x.json'
        self._assert(self._validate(n, name))
        self._assert(self._validate(n, 'sites/owner/nodes/wrong.json'), 'IDENTITY')
        for invalid in ('2023-02-29', '2024-13-01', '2024-04-31'):
            n['provenance']['recorded'] = invalid
            self._assert(self._validate(n, name), 'DATE', 2)
        n['provenance']['recorded'] = '2024-02-29'
        n['source_sha256'] = hashlib.sha256((self.root / 'sites/owner/source.md').read_bytes()).hexdigest()
        self._assert(self._validate(n, name), 'SPAN_PIN', 3)
        n['source_sha256'] = '0' * 64
        self._assert(self._validate(n, name), 'SPAN_PIN', 3)
        n = self._node(); n.update(status='active', expires='2024-02-29')
        self._assert(self._validate(n, name), 'AS_OF_MISSING')
        self._assert(self._validate(n, name, ctx=replace(self.ctx, as_of=g.date(2024, 2, 29))))
        self._assert(self._validate(n, name, ctx=replace(self.ctx, as_of=g.date(2024, 3, 1))), 'EXPIRED')
        n['anchor'] = '../source.md#root'
        self._assert(self._validate(n, name, ctx=replace(self.ctx, as_of=g.date(2024, 2, 29))), 'CONTAINMENT')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_lineage
    def test_lineage(self):
        first, second = self._node('first'), self._node('second')
        site = _fixture('site'); site['id'] = 'owner'
        site['provenance']['source_ref'] = 'EXPLANATION.md'
        self._write(site, 'sites/owner/site.json')
        second['supersedes'] = ['first']
        self._write(first, 'sites/owner/nodes/first.json')
        self._write(second, 'sites/owner/nodes/second.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx))
        for prior in (['missing'], ['second']):
            second['supersedes'] = prior
            self._write(second, 'sites/owner/nodes/second.json')
            result = g.validate_directory(self.root, BUNDLE, context=self.ctx)
            self._assert(result, 'LINEAGE_MISSING' if prior == ['missing'] else 'LINEAGE_CYCLE')
        second['supersedes'] = ['first']; second['owner_id'] = 'foreign'
        self._write(second, 'sites/owner/nodes/second.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), 'LINEAGE_MISSING')
        second['owner_id'] = 'owner'
        first['status'] = second['status'] = 'active'
        self._write(first, 'sites/owner/nodes/first.json')
        self._write(second, 'sites/owner/nodes/second.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), 'LINEAGE_TAIL')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_representation_fixtures
    def test_representation_fixtures(self):
        s = _step(); s.update(locate_wait_ms=100, locate_poll_ms=10)
        s['verify']['mask_boxes_px'] = [[0, 0, 10, 10]]
        s['when'] = _pred()
        s['repeat'] = dict(while_predicate=_pred(), max_iterations=2)
        self._assert(self._validate(s))
        s['target'] = _minimal(DEFS['t0'])
        s['target']['instances'] = [dict(seq=1, crop_box_px=[0, 0, 10, 10], crop_sha256='a' * 64),
                                     dict(seq=2, crop_box_px=[10, 10, 20, 20], crop_sha256='b' * 64)]
        self._assert(self._validate(s))
        s['verify']['mask_boxes_px'] = [[10, 0, 1, 5]]
        self._assert(self._validate(s), 'GEOMETRY')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_public_dependency_review
    def test_public_dependency_review(self):
        v = _fixture('publication_receipt')
        v['public_evidence_refs'] = ['public.md']
        public = self.root / 'public.md'; public.write_text('Reviewed public task evidence.\n')
        seen = []
        def review(payload, purpose):
            seen.append((payload, purpose))
            return _accept()
        self._assert(self._validate(v, ctx=replace(self.ctx, review_check=review)))
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx))
        self.assertIn((public.read_bytes(), 'public_evidence'), seen)
        self.assertIn(((g.canon(v) + '\n').encode(), 'publication'), seen)
        for data in (b'password=planted\n', b'/var/private/value\n', b'pixels=[1,2,3]\n'):
            public.write_bytes(data)
            self._assert(self._validate(v), 'PRIVACY')
        public.write_bytes(b'PK\x03\x04synthetic-archive')
        self._assert(self._validate(v), 'TEXT')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_artifact_byte_ceilings
    def test_artifact_byte_ceilings(self):
        for name, cap in [('site.json', 8192), ('manifest.json', 32768),
                          ('steps.json', 65536), ('workflow.json', 65536),
                          ('receipts.jsonl', 65536)]:
            path = self.root / name; path.write_bytes(b' ' * (cap + 1))
            self._assert(g.validate_file(path, BUNDLE, context=self.ctx), 'CAPACITY')
        p = self.root / 'EXPLANATION.md'
        base = _explanation(); p.write_bytes(base + b' ' * (16384 - len(base)))
        self._assert(g.check_explanation(p))
        p.write_bytes(p.read_bytes() + b' ')
        self._assert(g.check_explanation(p), 'CAPACITY')
        node = self._node()
        owner = self.root / 'sites/owner'
        source = b'<a id="root"></a>\n## Root\n'
        source += b'x' * (8192 - len(source))
        (owner / 'MEMORY.md').write_bytes(source)
        node.update(anchor='MEMORY.md#root', source_sha256=hashlib.sha256(source).hexdigest())
        self._assert(self._validate(node, 'sites/owner/nodes/x.json'))
        (owner / 'MEMORY.md').write_bytes(source + b'x')
        self._assert(self._validate(node, 'sites/owner/nodes/x.json'), 'CAPACITY')
        (owner / 'EXPLANATION.md').write_bytes(_explanation() + b'x' * 16385)
        self._assert(self._validate(node, 'sites/owner/nodes/x.json'), 'CAPACITY')
        (owner / 'EXPLANATION.md').write_bytes(_explanation())
        w = _fixture('workflow'); w['steps'] = [_step()]
        w['provenance']['source_ref'] = 'source.md'
        (self.root / 'source.md').write_bytes(_explanation())
        w['script_ref'] = 'scripts/dependency.py'
        source = b'# MW-CAPS: ' + g.canon(_fixture('caps')).encode() + b'\n#'
        source += b' ' * (32767 - len(source)) + b'\n'
        script = self.root / w['script_ref']; script.parent.mkdir(exist_ok=True)
        script.write_bytes(source); w['script_sha256'] = hashlib.sha256(source).hexdigest()
        self._assert(self._validate(w))
        script.write_bytes(source + b' ')
        self._assert(self._validate(w), 'CAPACITY')

    def _lifecycle_fixture(self, *, replacement=False, dream=False):
        (self.root / 'source.md').write_bytes(_explanation())
        w = _fixture('workflow')
        w['provenance']['source_ref'] = 'source.md'
        w.update(status='promoted', steps=[_step()])
        w['oracle'].update(postconditions=[_pred()], planted_mutant_ids=['m1', 'm2', 'm3'],
                           mutants_total=3, mutants_killed=3, receipt_refs=['receipt.md'])
        cohort = dict(task_ids=['a', 'b', 'c'], fixture_ids=['nominal', 'portable'])
        w['evaluation_profile'].update(fixture_ids=cohort['fixture_ids'],
            cohort_sha256=hashlib.sha256(g.canon(cohort).encode()).hexdigest())
        trials = [dict(task_id=t, authorized=True, live=True, supported=True, required_pass=True,
                       reviewed=True, semantic_verification=True, regression=False,
                       candidate=dict(task_pass=1, fixtures={'nominal': True, 'portable': True}, tokens_used=80),
                       baseline=dict(task_pass=1, fixtures={'nominal': True, 'portable': True}, tokens_used=100))
                  for t in cohort['task_ids']]
        view = dict(killed_mutant_ids=['m1','m2','m3'], protected_pass=True, supported=True, task_scope='read_only',
                    admission_runs=[dict(id=i, independent=True, verified=True, reproduced_ppm=1000000)
                                    for i in ('r1', 'r2')], cohort=cohort, trials=trials)
        if replacement:
            w['baseline_id'] = 'incumbent'
            w['evaluation_profile'].update(kind='paired_replacement_v1', baseline_workflow_sha256='b' * 64)
            view.update(baseline_eligible=True, baseline_workflow_sha256='b' * 64, baseline_id='incumbent')
        if dream:
            w['admission_basis'] = 'dream'
            view['episodes'] = [dict(id=i, closed=True, supported=True, tokens_base=100, tokens_cand=80,
                                     pass_base=1, pass_cand=1) for i in ('e1', 'e2', 'e3')]
            w['stats'].update(min_r_e=2, saved_tokens_bps=2000)
        w['stats'].update(margin_gain=3 if replacement else 30000, margin_mass=3,
                          margin_bound=0 if replacement else 9500,
                          online_shadow_runs=3, shadow_regressions=0)
        view['profile_sha256'] = hashlib.sha256(g.canon(w['evaluation_profile']).encode()).hexdigest()
        return w, view

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_lifecycle_cohorts
    def test_lifecycle_cohorts(self):
        for replacement in (False, True):
            w, view = self._lifecycle_fixture(replacement=replacement)
            ctx = replace(self.ctx, evidence_load=lambda *args: view)
            self._assert(self._validate(w, ctx=ctx))
            for kills in ([], ['m1','m2'], ['m1','m2','foreign']):
                bad = {**view, 'killed_mutant_ids': kills}
                self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'MUTANT_EVIDENCE')
            for key, changed, category in (
                ('task_id', 'a', 'COHORT_TASKS'), ('required_pass', False, 'COHORT_SUPPORT'),
                ('regression', True, 'REGRESSION'), ('authorized', False, 'COHORT_SUPPORT'),
                ('semantic_verification', False, 'COHORT_SUPPORT')):
                bad = copy.deepcopy(view); bad['trials'][1][key] = changed
                self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), category)
            for stat in ('margin_gain', 'margin_mass', 'margin_bound'):
                bad = copy.deepcopy(w); bad['stats'][stat] += 1
                self._assert(self._validate(bad, ctx=ctx), 'MARGIN_STATS')
            bad = copy.deepcopy(view); bad['trials'][0]['candidate']['fixtures']['portable'] = False
            self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)),
                         'REGRESSION' if replacement else 'COHORT_CASES')
            for field in ('profile_sha256',):
                bad = {**view, field: '0' * 64}
                self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'COHORT_PIN', 3)
            for key in ('online_shadow_runs', 'shadow_regressions'):
                bad = copy.deepcopy(w); bad['stats'][key] += 1
                self._assert(self._validate(bad, ctx=ctx), 'COHORT_STATS')
            bad = {**view, 'cohort': None}
            self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'COHORT_MISSING')
            if replacement:
                # All-tie evidence must retain the incumbent, even with consistent aggregates.
                bad = copy.deepcopy(view)
                for t in bad['trials']:
                    t['candidate']['tokens_used'] = 100
                tied = copy.deepcopy(w); tied['stats']['margin_gain'] = 0
                self._assert(self._validate(tied, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'MARGIN')
                for missing in (None, -1, True):
                    bad = copy.deepcopy(view); bad['trials'][0]['baseline']['tokens_used'] = missing
                    self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'COHORT_TOKENS')
                # A genuine portability improvement wins before tokens are compared.
                for trial in view['trials']:
                    trial['baseline']['fixtures']['portable'] = False
                    trial['baseline']['tokens_used'] = None
                self._assert(self._validate(w, ctx=ctx))
                view['baseline_eligible'] = False
                self._assert(self._validate(w, ctx=ctx), 'BASELINE')
        w, view = self._lifecycle_fixture()
        w['status'] = 'shadow'
        ctx = replace(self.ctx, evidence_load=lambda *a: view)
        view['task_scope'] = 'project_mutating'
        self._assert(self._validate(w, ctx=ctx), 'DEMO_ADMISSION')
        view['admission_runs'][0]['operator_demo'] = True
        view['admission_runs'][1].update(supervised=True, authorized=True)
        self._assert(self._validate(w, ctx=ctx))
        for key in ('verified', 'independent', 'authorized'):
            bad = copy.deepcopy(view); bad['admission_runs'][1][key] = False
            self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'DEMO_ADMISSION')
        view['supported'] = False
        self._assert(self._validate(w, ctx=ctx), 'ADMISSION_SUPPORT')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_dream_numeric
    def test_dream_numeric(self):
        w, view = self._lifecycle_fixture(dream=True)
        ctx = replace(self.ctx, evidence_load=lambda *a: view)
        self._assert(self._validate(w, ctx=ctx))
        for baseline in (None, 0, -1):
            bad = copy.deepcopy(view); bad['episodes'][0]['tokens_base'] = baseline
            self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'DREAM_DENOMINATOR')
        for delta in (dict(tokens_cand=90), dict(pass_cand=0), dict(tokens_cand=101)):
            bad = copy.deepcopy(view); bad['episodes'][0].update(delta)
            self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'DREAM_REWARD')
        bad = copy.deepcopy(view); bad['episodes'][0]['supported'] = False
        self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'DREAM_SUPPORT')
        bad = copy.deepcopy(view); bad['episodes'][1]['id'] = 'e1'
        self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: bad)), 'DREAM_SUPPORT')
        w['stats']['min_r_e'] = 0
        self._assert(self._validate(w, ctx=ctx), 'DREAM_STATS')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_target_frames_and_chips
    def test_target_frames_and_chips(self):
        step = _step(); target = _minimal(DEFS['t1'])
        target.update(frame_predicate=_pred(), anchor_ref='a', required_anchor_refs=['a'])
        step['target'] = target
        self._assert(self._validate(step))
        for tolerance in (1, 2):
            target['consistency_tolerance_px'] = tolerance
            self._assert(self._validate(step), 'TARGET_TOLERANCE')
        target['consistency_tolerance_px'] = 0
        for delta in (dict(frame_kind='viewport'), dict(relative_xy_bps=[1, 1]),
                      dict(required_anchor_refs=['b'])):
            bad = copy.deepcopy(step); bad['target'].update(delta)
            self._assert(self._validate(bad), 'TARGET_FRAME')
        target.update(frame_kind='viewport', relative_xy_bps=[5000, 5000])
        self._assert(self._validate(step))
        w, _ = self._lifecycle_fixture(); w.update(status='draft', stats=_minimal(DEFS['stats']))
        slot = _minimal(DEFS['slot']); slot.update(slot_id='text', chip_clear_steps=[1, 2])
        w['text_slots'] = [slot]
        w['steps'] = []
        for seq in (1, 2, 3):
            s = _step(); s.update(seq=seq, target=_minimal(DEFS['t2']), preconditions=[_pred()])
            s['action'].update(kind='click' if seq < 3 else 'paste', text_slot='text' if seq == 3 else None)
            w['steps'].append(s)
        view = dict(center_x_px=0, center_y_px=0, chip_id='chip', guard_pass=True, postcondition_pass=True)
        def load(kind, artifact, related):
            return {**view, 'center_x_px': 300 - artifact['seq'] * 100}
        ctx = replace(self.ctx, evidence_load=load)
        self._assert(self._validate(w, ctx=ctx))
        for key in ('guard_pass', 'postcondition_pass'):
            view[key] = False
            self._assert(self._validate(w, ctx=ctx), 'CHIP_GROUNDING')
            view[key] = True
        self._assert(self._validate(w, ctx=replace(ctx, evidence_load=lambda *a: view)), 'CHIP_ORDER')
        for sequences in ([2, 1], [1, 1], [3]):
            slot['chip_clear_steps'] = sequences
            self._assert(self._validate(w, ctx=ctx), 'CHIP_ORDER')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_canonical_owner_paths
    def test_canonical_owner_paths(self):
        site = _fixture('site'); site['id'] = 'owner'
        site['provenance']['source_ref'] = 'EXPLANATION.md'
        self._node()
        path = self._write(site, 'sites/owner/site.json')
        self._assert(g.validate_file(path, BUNDLE, context=self.ctx))
        path = self._write(_fixture('caps'), 'sites/owner/caps.json')
        self._assert(g.validate_file(path, BUNDLE))
        proc = subprocess.run([sys.executable, '-B', g.__file__, 'validate', str(path)],
                              capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        node = self._node(); path = self._write(node, 'sites/owner/nodes/x.json')
        # CLI has no provenance owner; its first refusal must be evidence, not identity/path.
        self._assert(g.validate_file(path, BUNDLE), 'EVIDENCE_MISSING')
        inferred = replace(self.ctx, root=g._inferred_root(path))
        self._assert(g.validate_file(path, BUNDLE, context=inferred))
        for kind in ('workflow', 'demo_steps', 'run', 'publication_receipt'):
            v = _fixture(kind); v['site_id'] = 'foreign'
            if kind == 'workflow':
                v['steps'] = [_step()]
            self._assert(self._validate(v, 'sites/owner/' + ('x.json' if kind == 'workflow' else 'record.json')), 'IDENTITY')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_graph_references
    def test_graph_references(self):
        node = self._node(); self._write(node, 'sites/owner/nodes/x.json')
        site = _fixture('site'); site['id'] = 'owner'
        site['provenance']['source_ref'] = 'EXPLANATION.md'
        self._write(site, 'sites/owner/site.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx))
        (self.root / 'sites/owner/site.json').unlink()
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), 'OWNER_MISSING')
        self._write(site, 'sites/owner/site.json')
        pointer = copy.deepcopy(node); pointer.update(id='p', kind='pointer', pointer_target=dict(
            owner_kind='site', owner_id='owner', node_id='x', source_sha256=node['source_sha256']))
        self._write(pointer, 'sites/owner/nodes/p.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx))
        for field, val, cat in [('node_id', 'absent', 'POINTER'), ('source_sha256', '0'*64, 'POINTER_PIN'),
                                ('owner_id', 'foreign', 'POINTER')]:
            bad = copy.deepcopy(pointer); bad['pointer_target'][field] = val
            self._write(bad, 'sites/owner/nodes/p.json')
            self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), cat, 3 if cat.endswith('_PIN') else 5)
        self._write(pointer, 'sites/owner/nodes/p.json')
        tags = _fixture('tags'); tags['facets'] = {'domain':['web'], 'action':['click'], 'ui-control':['button'], 'failure-mode':['miss']}
        self._write(tags, 'standards/tags.json')
        node.update(status='active', kind='control', tags=['web','click','button'])
        ctx = replace(self.ctx, as_of=g.date(2024,2,29))
        for tokens, cat in [(['web','click','button'], 'PASS'), (['web','click'], 'TAG_FACETS'),
                            (['web','button'], 'TAG_FACETS'), (['web','click','unknown'], 'TAGS'),
                            (['web','click','button','button'], 'TAGS')]:
            node['tags'] = tokens; self._write(node, 'sites/owner/nodes/x.json')
            self._assert(g.validate_directory(self.root, BUNDLE, context=ctx), cat)
        node['tags'] = ['web','click','button']; self._write(node, 'sites/owner/nodes/x.json')
        detail = dict(schema='mw.detail.v1', owner_kind='site', owner_id='owner', node_id='x',
                      node_sha256=hashlib.sha256((g.canon(node)+'\n').encode()).hexdigest(),
                      anchor=node['anchor'], source_sha256=node['source_sha256'])
        # Derived locator lives in catalogue; span resolution still uses canonical owner.
        self._write(detail, 'catalogue/details/x.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=ctx))
        for field in ('node_sha256', 'source_sha256', 'anchor'):
            bad = {**detail, field: 'source.md#other' if field == 'anchor' else '0'*64}
            self._write(bad, 'catalogue/details/x.json')
            self._assert(g.validate_directory(self.root, BUNDLE, context=ctx), 'DETAIL_PIN', 3)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_graph_workflows_and_capstones
    def test_graph_workflows_and_capstones(self):
        node = self._node(); self._write(node, 'sites/owner/nodes/x.json')
        site = _fixture('site'); site.update(id='owner')
        site['provenance']['source_ref'] = 'EXPLANATION.md'
        self._write(site, 'sites/owner/site.json')
        cap = _fixture('capstone'); cap.update(owner_kind='site', owner_id='owner', node_ids=['x'])
        cap_view = dict(receipts=[])
        ctx = replace(self.ctx, evidence_load=lambda *a: cap_view)
        self._write(cap, 'catalogue/capstone.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=ctx))
        for delta in (dict(node_ids=[]), dict(defined_in=['missing']), dict(falsifications=1)):
            self._write({**cap, **delta}, 'catalogue/capstone.json')
            self._assert(g.validate_directory(self.root, BUNDLE, context=ctx), 'CAPSTONE')
        self._write(cap, 'catalogue/capstone.json')
        w = _fixture('workflow'); w.update(site_id='owner', steps=[_step()])
        w['provenance']['source_ref'] = 'EXPLANATION.md'
        w['fallback'].update(kind='ask_operator', workflow_id=None)
        self._write(w, 'sites/owner/workflows/x.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=ctx))
        for fallback in (dict(kind='workflow',workflow_id='missing'), dict(kind='workflow',workflow_id='x'),
                         dict(kind='workflow',workflow_id=None)):
            bad = copy.deepcopy(w); bad['fallback'] = fallback
            self._write(bad, 'sites/owner/workflows/x.json')
            self._assert(g.validate_directory(self.root, BUNDLE, context=ctx), 'FALLBACK')
        self._write(w, 'sites/owner/workflows/x.json')
        site['patterns'] = ['absent']; self._write(site, 'sites/owner/site.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=ctx), 'PATTERN_MISSING')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_canonical_demo_files
    def test_canonical_demo_files(self):
        site = _fixture('site'); site['id'] = 'owner'
        site['provenance']['source_ref'] = 'demos/x/EXPLANATION.md'
        self._write(site, 'sites/owner/site.json')
        demo = _fixture('demo'); demo['site_id'] = 'owner'
        demo['provenance']['source_ref'] = 'demos/x/EXPLANATION.md'
        demo['steps_file'] = 'steps.json'
        steps = _fixture('demo_steps'); steps.update(site_id='owner', demo_id='x')
        path = self._write(steps, 'sites/owner/demos/x/steps.json')
        demo['steps_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        explanation = path.parent / 'EXPLANATION.md'; explanation.write_bytes(_explanation())
        demo['explanation']['sha256'] = hashlib.sha256(explanation.read_bytes()).hexdigest()
        self._write(demo, 'sites/owner/demos/x/manifest.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx))
        for field in ('steps_sha256',):
            for digest in ('0'*64, '1'*64, '2'*64):
                bad = {**demo, field: digest}
                self._write(bad, 'sites/owner/demos/x/manifest.json')
                self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), 'DEMO_PIN', 3)
        steps['demo_id'] = 'foreign'; self._write(steps, 'sites/owner/demos/x/steps.json')
        self._assert(g.validate_file(path, BUNDLE, context=self.ctx), 'IDENTITY')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_graph_baseline_pins
    def test_graph_baseline_pins(self):
        base, view = self._lifecycle_fixture()
        (self.root / 'source.md').unlink()
        owner = self.root / 'sites/owner'; owner.mkdir(parents=True)
        (owner / 'EXPLANATION.md').write_bytes(_explanation())
        base.update(id='incumbent', site_id='owner')
        base['provenance']['source_ref'] = 'EXPLANATION.md'
        base['fallback'].update(kind='ask_operator', workflow_id=None)
        # Oracle references are real inert files; the trusted owner binds their evidence.
        (owner / 'receipt.md').write_text('Synthetic independently pinned oracle evidence.\n')
        base_path = self._write(base, 'sites/owner/workflows/incumbent.json')
        site = _fixture('site'); site['id'] = 'owner'
        site['provenance']['source_ref'] = 'EXPLANATION.md'
        self._write(site, 'sites/owner/site.json')
        candidate = copy.deepcopy(base)
        candidate.update(id='x', status='draft', baseline_id='incumbent', stats=_minimal(DEFS['stats']))
        candidate['evaluation_profile'].update(kind='paired_replacement_v1',
            baseline_workflow_sha256=hashlib.sha256(base_path.read_bytes()).hexdigest())
        candidate['fallback'].update(kind='workflow', workflow_id='incumbent')
        path = self._write(candidate, 'sites/owner/workflows/x.json')
        ctx = replace(self.ctx, evidence_load=lambda *a: view)
        self._assert(g.validate_directory(self.root, BUNDLE, context=ctx))
        for pin in ('0'*64, '1'*64, '2'*64):
            bad = copy.deepcopy(candidate); bad['evaluation_profile']['baseline_workflow_sha256'] = pin
            self._write(bad, 'sites/owner/workflows/x.json')
            self._assert(g.validate_directory(self.root, BUNDLE, context=ctx), 'BASELINE_PIN', 3)
        self._write(candidate, 'sites/owner/workflows/x.json')
        base['status'] = 'draft'; self._write(base, 'sites/owner/workflows/incumbent.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=ctx), 'BASELINE')

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_pattern_spans
    def test_pattern_spans(self):
        owner = self.root / 'patterns/x'; owner.mkdir(parents=True)
        (owner / 'EXPLANATION.md').write_bytes(_explanation())
        source = b'<a id="root"></a>\n## Root\nBehaviour.\n'
        (owner / 'MEMORY.md').write_bytes(source)
        pattern = _fixture('pattern'); pattern['provenance']['source_ref'] = 'EXPLANATION.md'
        pattern['behaviour_anchor'] = 'MEMORY.md#root'
        self._assert(self._validate(pattern, 'patterns/x/pattern.json'))
        for raw, cat in [(source.replace(b'root',b'other'), 'ANCHOR_MISSING'),
                         (source + source, 'ANCHOR_DUPLICATE'), (source.replace(b'\n',b'\r\n'), 'LINE_ENDINGS')]:
            (owner / 'MEMORY.md').write_bytes(raw)
            self._assert(self._validate(pattern, 'patterns/x/pattern.json'), cat, 3)

    # @anchor:wa.tests.memoryweb_gate_unit.TestGate.test_structured_public_evidence
    def test_structured_public_evidence(self):
        receipt = _fixture('publication_receipt')
        cohort = dict(task_ids=['a','b','c'], fixture_ids=['nominal'])
        profile = _minimal(DEFS['evaluation_profile']); profile.update(fixture_ids=['nominal'],
            cohort_sha256=hashlib.sha256(g.canon(cohort).encode()).hexdigest())
        receipt.update(mode='shadow', cohort_task_id='a', public_evidence_refs=['profile.json', 'cohort.json'],
                       evaluation_profile_sha256=hashlib.sha256(g.canon(profile).encode()).hexdigest())
        self._write(profile, 'profile.json'); self._write(cohort, 'cohort.json')
        self._write(receipt, 'receipt.json')
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx))
        for pin in ('0'*64, '1'*64, '2'*64):
            bad = {**receipt, 'evaluation_profile_sha256': pin}; self._write(bad, 'receipt.json')
            self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), 'PUBLIC_PROFILE_PIN', 3)
        self._write(receipt, 'receipt.json')
        (self.root / 'EXPLANATION.md').write_bytes(_explanation())
        self._assert(g.validate_directory(self.root, BUNDLE, context=self.ctx), 'UNCLASSIFIED', 2)


# @anchor:wa.tests.memoryweb_gate_unit.TestControlMutants
class TestControlMutants(unittest.TestCase):
    # @anchor:wa.tests.memoryweb_gate_unit.TestControlMutants.test_check_removal
    def test_check_removal(self):
        controls = {
            'PROTOCOL_PIN': 'test_protocol_schema_parity', 'ENTRY_PIN': 'test_entry_controls',
            'ENTRY_TARGET': 'test_entry_controls', 'JSON_NUMBER': 'test_strict_parsing',
            'JSON_DUPLICATE_KEY': 'test_strict_parsing', 'JSON_UNICODE': 'test_strict_parsing',
            'SCHEMA_KEYWORD': 'test_schema_compilation', 'SCHEMA_REF': 'test_schema_compilation',
            'SCHEMA_TYPE': 'test_schema_compilation', 'SCHEMA_OPERAND': 'test_schema_compilation',
            'CAPACITY': 'test_depth_capacity', 'CANONICAL': 'test_canonical_bytes',
            'ROOT': 'test_root_selection', 'ANCHOR_UNATTACHED': 'test_anchor_spans',
            'ANCHOR_DUPLICATE': 'test_anchor_spans', 'EXPLANATION': 'test_explanation',
            'ACTION_TARGET': 'test_action_shapes', 'ACTION_FIELDS': 'test_action_shapes',
            'ACTION_SLOT': 'test_action_shapes', 'ACTION_DRAG': 'test_action_shapes',
            'ACTION_FOCUS': 'test_focus_and_slots', 'SNAPSHOT': 'test_snapshot_and_verifier',
            'CAPTURE': 'test_snapshot_and_verifier',
            'SNAPSHOT_COMPATIBILITY': 'test_snapshot_and_verifier', 'POLLING': 'test_snapshot_and_verifier',
            'VERIFIER_PREDICATE': 'test_snapshot_and_verifier',
            'RAM_EVIDENCE_MISSING': 'test_snapshot_and_verifier',
            'VERIFIER_DEPTH': 'test_snapshot_and_verifier',
            'VERIFIER_EQUIVALENCE': 'test_snapshot_and_verifier',
            'PROTECTED': 'test_snapshot_and_verifier', 'BINDING': 'test_bindings_capabilities_geometry',
            'GEOMETRY': 'test_bindings_capabilities_geometry', 'REQUEST_LINK': 'test_requests',
            'PRIVACY': 'test_public_dependency_review', 'PUBLICATION_COHORT': 'test_publication_privacy',
            'EVIDENCE_DENIED': 'test_publication_privacy', 'TEXT': 'test_publication_privacy',
            'TORN': 'test_journal', 'SEQUENCE': 'test_journal', 'PREDECESSOR': 'test_journal',
            'COUNTS': 'test_counts', 'ADMISSION': 'test_workflow_draft_and_admission',
            'MUTANTS': 'test_workflow_draft_and_admission', 'BASELINE': 'test_workflow_draft_and_admission',
            'SCRIPT_PAIR': 'test_script_dependency', 'SCRIPT_PATH': 'test_script_dependency',
            'SCRIPT_PIN': 'test_script_dependency', 'SCRIPT_HEADER': 'test_script_dependency',
            'IDENTITY': 'test_dates_identity_and_span_pins', 'DATE': 'test_dates_identity_and_span_pins',
            'SPAN_PIN': 'test_dates_identity_and_span_pins', 'AS_OF_MISSING': 'test_dates_identity_and_span_pins',
            'EXPIRED': 'test_dates_identity_and_span_pins', 'LINEAGE_MISSING': 'test_lineage',
            'LINEAGE_CYCLE': 'test_lineage', 'LINEAGE_TAIL': 'test_lineage',
        }
        controls.update({
            'SCHEMA': 'test_publication_privacy',
            'COUNTS_INVENTORY': 'test_counts', 'COUNTS_EVIDENCE': 'test_counts',
            'REPRODUCTION': 'test_counts', 'REPRODUCTION_EVIDENCE': 'test_counts',
            'RUN_PIN': 'test_counts', 'FINAL_ORACLE': 'test_counts',
            'DEMO_ADMISSION': 'test_lifecycle_cohorts', 'ADMISSION_SUPPORT': 'test_lifecycle_cohorts',
            'DREAM_SUPPORT': 'test_dream_numeric', 'DREAM_REWARD': 'test_dream_numeric',
            'MUTANT_EVIDENCE': 'test_lifecycle_cohorts',
            'DREAM_STATS': 'test_dream_numeric', 'COHORT_MISSING': 'test_lifecycle_cohorts',
            'COHORT_PIN': 'test_lifecycle_cohorts', 'COHORT_TASKS': 'test_lifecycle_cohorts',
            'COHORT_SUPPORT': 'test_lifecycle_cohorts', 'COHORT_CASES': 'test_lifecycle_cohorts',
            'COHORT_STATS': 'test_lifecycle_cohorts', 'REGRESSION': 'test_lifecycle_cohorts',
            'MARGIN_STATS': 'test_lifecycle_cohorts', 'MARGIN': 'test_lifecycle_cohorts',
            'TARGET_TOLERANCE': 'test_target_frames_and_chips', 'TARGET_FRAME': 'test_target_frames_and_chips',
            'CHIP_GROUNDING': 'test_target_frames_and_chips', 'CHIP_ORDER': 'test_target_frames_and_chips',
            'LINE_ENDINGS': 'test_anchor_spans', 'ANCHOR_MISSING': 'test_pattern_spans',
            'OWNER_MISSING': 'test_graph_references', 'POINTER': 'test_graph_references',
            'POINTER_PIN': 'test_graph_references', 'TAGS': 'test_graph_references',
            'TAG_FACETS': 'test_graph_references', 'DETAIL_PIN': 'test_graph_references',
            'CAPSTONE': 'test_graph_workflows_and_capstones', 'FALLBACK': 'test_graph_workflows_and_capstones',
            'PATTERN_MISSING': 'test_graph_workflows_and_capstones',
            'BASELINE_PIN': 'test_graph_baseline_pins', 'DEMO_PIN': 'test_canonical_demo_files',
            'PUBLIC_PROFILE_PIN': 'test_structured_public_evidence',
            'JOURNAL_BUDGET_MISSING': 'test_private_journal_budget',
            'CONTAINMENT': 'test_containment_unclassified',
        })
        source = Path(g.__file__).read_text()
        original = g
        for category, test in controls.items():
            with self.subTest(category=category):
                class Remove(ast.NodeTransformer):
                    count = 0
                    def visit_Raise(self, node):
                        call = node.exc
                        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and
                                call.func.id == '_Refusal' and call.args and
                                isinstance(call.args[0], ast.Constant) and call.args[0].value == category):
                            self.count += 1
                            return ast.copy_location(ast.Pass(), node)
                        return self.generic_visit(node)
                    def visit_Expr(self, node):
                        call = node.value
                        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and
                                call.func.id == '_require' and len(call.args) >= 2 and
                                isinstance(call.args[1], ast.Constant) and call.args[1].value == category):
                            self.count += 1
                            return ast.copy_location(ast.Pass(), node)
                        return self.generic_visit(node)
                remover = Remove()
                tree = ast.fix_missing_locations(remover.visit(ast.parse(source)))
                self.assertGreater(remover.count, 0)
                module = types.ModuleType('memoryweb_control_mutant')
                module.__file__ = original.__file__
                with patch.dict(sys.modules, {module.__name__: module}):
                    exec(compile(tree, original.__file__, 'exec'), module.__dict__)
                    result = unittest.TestResult()
                    with patch.dict(globals(), {'g': module}):
                        TestGate(test).run(result)
                    self.assertTrue(result.failures, 'SURVIVED OR ERROR-ONLY: ' + category)
        # These mutations target branch checks rather than categorical refusals.
        for before, after, test in [
            ("base = _integer(e.get('tokens_base'), 'DREAM_DENOMINATOR', 1)", "base = e.get('tokens_base')", 'test_dream_numeric'),
            ("-_integer(baseline.get('tokens_used'), 'COHORT_TOKENS')", "-baseline.get('tokens_used')", 'test_lifecycle_cohorts'),
            ("re.compile(s['pattern'])", "None", 'test_schema_compilation'),
            ("if '$defs' in schema and not any", "if False and not any", 'test_root_selection'),
            ("'MEMORY.md': 8192", "'MEMORY.md': 65536", 'test_artifact_byte_ceilings'),
            ("'EXPLANATION.md': 16384", "'EXPLANATION.md': 65536", 'test_artifact_byte_ceilings'),
            ("_require(callable(callback), missing)", "pass", 'test_bindings_capabilities_geometry'),
            ("re.fullmatch(schema['pattern'], value)", "re.match(schema['pattern'], value)", 'test_pattern_guards'),
            ("type(value) is not TYPES[schema['type']]", "not isinstance(value, TYPES[schema['type']])", 'test_exact_types_and_composition'),
            ("sum(results) != 1", "not any(results)", 'test_exact_types_and_composition'),
            ("disposition = 'observe' if code == 0 else 'stop'", "disposition = 'VERIFIED' if code == 0 else 'stop'", 'test_native_translation'),
            ("3: 5, 4: 3", "3: 3, 4: 3", 'test_native_translation'),
            ("rc, disposition = 4, 'handoff'", "rc, disposition = 5, 'stop'", 'test_native_translation')]:
            self.assertIn(before, source)
            module = types.ModuleType('memoryweb_control_mutant')
            module.__file__ = original.__file__
            with patch.dict(sys.modules, {module.__name__: module}):
                exec(compile(source.replace(before, after), original.__file__, 'exec'), module.__dict__)
                result = unittest.TestResult()
                with patch.dict(globals(), {'g': module}):
                    TestGate(test).run(result)
                self.assertFalse(result.wasSuccessful(), 'SURVIVED: ' + before)


if __name__ == '__main__':
    unittest.main()
