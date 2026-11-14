# @module {"id":"wa.tests.srf_redaction","layer":2,"owner":"LEARN","deps":["wa.perceive.srf_identity","wa.learn.cu_tasks"],"book":"HB-WA-s4-learn"}
"""W2 privacy, screen identity, tripwire and non-vacuous predicate gates."""
import ast
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from wender.learn.cu_tasks import web_predicate
from wender.perceive.srf_identity import (
    ax_options, blocked_control_present, canon_head, screen_flags, srf_hash, srf_state_hash,
)


def _element(name='Ask', **overrides):
    return dict(id='e1', name=name, role='button', region='main', box=[0, 0, 30, 20],
                actionable=True, visible=True) | overrides


def _ax(*elements):
    return {'elements': list(elements)}


def fixture_srf_identity_textbox_value_raw_string_retained_in_elements():
    return _ax(_element('Prompt', role='textbox', value='s3cret-prompt-text', value_len=18))


def fixture_srf_hash_matches_when_textbox_cleared_from_len5_to_len0():
    return (_ax(_element(role='textbox', value_len=5)),
            _ax(_element(role='textbox', value_len=0)))


def fixture_srf_hash_matches_when_focused_element_id_changes():
    return (_ax(_element(id='e1', focused=True)), _ax(_element(id='e2', focused=True)))


def fixture_contains_text_predicate_vacuous_pass_when_substr_absent():
    return {'kind': 'contains_text'}, _ax(_element())


def fixture_blocked_control_present_false_when_disabled_send_button_exists():
    return _ax(_element('Send', actionable=False))


class TestSrfRedaction(unittest.TestCase):
    def test_textbox_value_redacted_and_value_len_validated(self):
        ax = fixture_srf_identity_textbox_value_raw_string_retained_in_elements()
        mutant = copy.deepcopy(ax)  # Must FAIL: raw-copy projection retains the secret.
        opts, targets = ax_options(ax)
        predicate = lambda data: 's3cret-prompt-text' not in json.dumps(data) and 'value' not in data['elements'][0]
        self.assertFalse(predicate(mutant))
        self.assertTrue(predicate(ax))
        self.assertNotIn('s3cret-prompt-text', json.dumps([opts, targets]))
        self.assertEqual(opts[0]['value_len'], 18)
        self.assertTrue(web_predicate({'kind': 'value_nonempty', 'role': 'textbox'}, {'elements': opts}))
        for raw in ('5', -1, True, 3.5, None, 0):
            with self.subTest(raw=raw):
                options, _ = ax_options(_ax(_element(role='textbox', value_len=raw,
                                                     focused='private', states={'enabled': True, 'checked': 'private'})))
                self.assertEqual(options[0]['value_len'], 0 if type(raw) is int and raw == 0 else None)
                self.assertIsNone(options[0]['focused'])
                self.assertIsNone(options[0]['states']['checked'])
        self.assertFalse(web_predicate({'kind': 'value_nonempty'}, _ax(_element(value_len=0))))
        self.assertIsNone(ax_options(_ax(_element()))[0][0]['value_len'])
        ax = _ax(*[_element(f'private-history-{i}', role='link', region='navigation', value='s3cret-prompt-text') for i in range(50)],
                 _element('Prompt', role='textbox', value_len=0))
        options, targets = ax_options(ax)
        serialized = json.dumps([options, targets, screen_flags(ax)])
        self.assertNotIn('private-history-', serialized)
        self.assertNotIn('s3cret-prompt-text', serialized)
        self.assertEqual(screen_flags(ax)['sidebar_hidden'], 50)
        self.assertEqual(len(options), 2)
        self.assertEqual(set(options[0]), {'id', 'role', 'name', 'region', 'container_ordinal', 'value_len', 'focused', 'states'})

    def test_srf_hash_changes_when_textbox_cleared_len5_to_len0(self):
        a, b = fixture_srf_hash_matches_when_textbox_cleared_from_len5_to_len0()
        predicate = lambda hash_fn: hash_fn(a) != hash_fn(b)
        self.assertFalse(predicate(lambda ax: srf_hash(ax, include_state=False)))
        self.assertTrue(predicate(srf_hash))
        self.assertTrue(predicate(srf_state_hash))
        self.assertEqual(srf_hash(a), srf_hash(copy.deepcopy(a)))
        self.assertEqual(srf_hash(a, include_state=False), srf_hash(b, include_state=False))
        self.assertRegex(srf_hash(a), r'^[0-9a-f]{12}$')
        self.assertRegex(srf_state_hash(a), r'^[0-9a-f]{16}$')
        for field in ('enabled', 'checked', 'expanded', 'selected', 'disabled'):
            a, b = _ax(_element(states={field: False})), _ax(_element(states={field: True}))
            self.assertNotEqual(srf_state_hash(a), srf_state_hash(b))
        self.assertEqual(srf_hash(_ax(_element(value='one'))), srf_hash(_ax(_element(value='two'))))

    def test_srf_hash_changes_when_focused_element_id_changes(self):
        a, b = fixture_srf_hash_matches_when_focused_element_id_changes()
        self.assertEqual(srf_hash(a, include_state=False), srf_hash(b, include_state=False))  # state-blind mutant
        self.assertNotEqual(srf_hash(a), srf_hash(b))
        self.assertNotEqual(srf_state_hash(a), srf_state_hash(b))
        a, b = _ax(_element(focused=True)), _ax(_element(focused=False))
        self.assertNotEqual(srf_hash(a), srf_hash(b))
        self.assertEqual(srf_hash(a, include_state=False), srf_hash(b, include_state=False))
        self.assertEqual(srf_hash(a), srf_hash(copy.deepcopy(a)))
        self.assertEqual(canon_head('Investigando 3s'), canon_head('Investigando 9s'))
        self.assertEqual(srf_hash(_ax(_element('Investigando 3s'))), srf_hash(_ax(_element('Investigando 9s'))))
        # Must FAIL: a one-word head aliases distinct sign actions.
        self.assertEqual('Sign in'.split()[0], 'Sign out'.split()[0])
        self.assertNotEqual(srf_hash(_ax(_element('Sign in'))), srf_hash(_ax(_element('Sign out'))))
        a = _ax(_element('Toolbar', box=[0, 0, 10, 10]), _element('Footer', id='e2', box=[0, 128, 10, 10]))
        b = copy.deepcopy(a)
        b['elements'][0]['box'], b['elements'][1]['box'] = b['elements'][1]['box'], b['elements'][0]['box']
        # Must FAIL: an order-insensitive label multiset aliases transposed rows.
        self.assertEqual(sorted(e['name'] for e in a['elements']), sorted(e['name'] for e in b['elements']))
        self.assertNotEqual(srf_hash(a), srf_hash(b))
        self.assertNotEqual(srf_state_hash(a), srf_state_hash(b))
        b = copy.deepcopy(a)
        b['elements'][0]['box'][0] += 1
        self.assertEqual(srf_hash(a), srf_hash(b))  # coordinates order but never enter payload
        self.assertEqual(canon_head('  LO\u0301   QUE  sea '), 'ló que')
        self.assertLessEqual(len(canon_head('a' * 100)), 24)

    def test_contains_text_predicate_trichotomy_and_non_vacuous(self):
        rule, ax = fixture_contains_text_predicate_vacuous_pass_when_substr_absent()
        self.assertTrue(rule.get('substr', '') in ax['elements'][0]['name'])  # Must FAIL: vacuous mutant
        with self.assertRaises(ValueError):
            web_predicate(rule, ax)
        for substr in (None, '', '   ', 123):
            for snapshot in ({}, _ax()):
                with self.subTest(substr=substr, snapshot=snapshot), self.assertRaises(ValueError):
                    web_predicate({'kind': 'contains_text', 'substr': substr}, snapshot)
        rule = {'kind': 'contains_text', 'substr': 'L\u00d3 QUE', 'role': 'button', 'region': 'main'}
        self.assertTrue(web_predicate(rule, _ax(_element('Pregunta lo\u0301  que sea'))))
        self.assertTrue(web_predicate(rule, _ax(_element('Ask', text='LO\u0301 QUE'))))
        rule['substr'] = ' ausente '
        self.assertFalse(web_predicate(rule, _ax(_element())))
        self.assertIsNone(web_predicate(rule, _ax()))
        self.assertIsNone(web_predicate(rule, {}))
        self.assertIsNone(web_predicate(rule, _ax(_element(name=None))))
        self.assertIsNone(web_predicate(rule | {'states': {'focused': True}}, _ax(_element())))
        self.assertIsNone(web_predicate(rule | {'pattern': '^Missing'}, _ax(_element())))
        self.assertFalse(web_predicate(rule | {'substr': 'secret'}, _ax(_element(value='secret'))))
        self.assertFalse(web_predicate(rule | {'substr': 'secret'}, _ax(_element()), harvested='secret'))

    def test_blocked_control_present_on_disabled_send_and_upgrade(self):
        ax = fixture_blocked_control_present_false_when_disabled_send_button_exists()
        self.assertFalse(bool(ax_options(ax)[0]))  # Must FAIL: counting only emitted controls misses Send.
        self.assertTrue(blocked_control_present(ax))
        for name in ('Send', 'Enviar', 'Submit', 'Ask', 'Preguntar'):
            self.assertTrue(blocked_control_present(_ax(_element(name, states={'enabled': False}))))
        for name in ('Try Pro', 'Mejorar plan', 'Upgrade', 'Billing', 'Facturación', 'Subscribe', 'Suscribirse'):
            with self.subTest(name=name):
                ax = _ax(_element(name))
                self.assertTrue(blocked_control_present(ax))
                self.assertEqual(ax_options(ax), ([], {}))
                self.assertNotIn(name, json.dumps(screen_flags(ax)))
        clean = _ax(_element(), _element('Nuevo', role='link', region='navigation'))
        self.assertIs(blocked_control_present(clean), False)
        self.assertEqual(screen_flags(clean)['blocked_control_present'], 0)
        ax = _ax(_element('Send', actionable=False), _element('Upgrade'))
        self.assertEqual(screen_flags(ax)['blocked_control_present'], 2)
        target = _ax(_element('Continue', actionable=False))
        self.assertFalse(blocked_control_present(target))
        self.assertTrue(blocked_control_present(target, {'role': 'button', 'pattern': '^Continue$', 'region': 'main'}))
        self.assertFalse(blocked_control_present(target, {'role': 'link'}))

    def test_observation_hashes_shared_and_cache_invalidated(self):
        from wender.perceive import srf_identity as surface
        ax = _ax(_element('Ask', focused=False, value_len=5)) | {'frame_seq': 10}
        surface._snapshot_hashes.cache_clear()
        with patch.object(surface, 'canon_head', wraps=canon_head) as canonicalize:
            full, state = srf_hash(ax), srf_state_hash(ax)
            flags = screen_flags(ax)
            self.assertEqual((full, state), (flags['srf_hash'], flags['srf_state_hash']))
            self.assertEqual(canonicalize.call_count, 1)
            # Positive control: equivalent snapshots share computation.
            self.assertEqual(srf_hash(copy.deepcopy(ax)), full)
            self.assertEqual(canonicalize.call_count, 1)
            ax['frame_seq'] += 1
            self.assertEqual(srf_hash(ax), full)
            self.assertEqual(canonicalize.call_count, 2)
        # Must FAIL: a frame_seq-only cache returns the previous digest here.
        stale = full
        ax['elements'][0]['value_len'] = 0
        self.assertNotEqual(srf_hash(ax), stale)
        self.assertNotEqual(srf_state_hash(ax), state)
        self.assertEqual(screen_flags(ax)['srf_hash'], srf_hash(ax))
        for field, value in [('focused', True), ('id', 'changed'), ('name', 'Sign out'),
                             ('states', {'enabled': False})]:
            before = srf_hash(ax)
            ax['elements'][0][field] = value
            self.assertNotEqual(srf_hash(ax), before, field)
        # Raw input is excluded even from the RAM memoization key.
        with patch.object(surface, 'canon_head', wraps=canon_head) as canonicalize:
            ax['elements'][0]['value'] = 'private-input'
            self.assertEqual(srf_hash(ax), srf_hash(copy.deepcopy(ax)))
            self.assertEqual(canonicalize.call_count, 0)
        other = _ax(_element('Ask', value_len=7)) | {'frame_seq': ax['frame_seq']}
        self.assertNotEqual(srf_hash(other), srf_hash(ax))
        # Rule-specific flags remain fresh while hash computation is shared.
        target = _ax(_element('Continue', actionable=False)) | {'frame_seq': 99}
        self.assertFalse(blocked_control_present(target))
        self.assertTrue(blocked_control_present(target, {'role': 'button'}))
        self.assertFalse(blocked_control_present(target))

    def test_w2_hyperbook_tags_and_deps_sound(self):
        from tools.layer_check import check
        root = Path(__file__).resolve().parents[1]
        books = json.loads((root / 'hyperbooks/COMPANION_FORMAT.json').read_text())['expected_books']
        for path, book in [('wender/perceive/srf_identity.py', 'HB-WA-s1-perceive'),
                           ('wender/learn/cu_tasks.py', 'HB-WA-s4-learn')]:
            text = (root / path).read_text()
            header = json.loads(text.splitlines()[0].split('@module ', 1)[1])
            self.assertEqual(header['book'], book)
            self.assertIn(header['book'], books)
            imports = {node.module.replace('wender.', 'wa.', 1) for node in ast.walk(ast.parse(text))
                       if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('wender.')}
            self.assertEqual(set(header['deps']), imports)
            mutant = header | {'book': 'HB-WA-s2-perceive'}
            self.assertNotIn(mutant['book'], books)
            if imports:
                self.assertNotEqual(set(), imports)  # missing dependency mutant is rejected
        self.assertEqual(check(root), [])
