# @module {"id":"wa.tests.disambig","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.disambig","wa.perceive.srf_identity"],"book":"HB-WA-s1-perceive"}
"""W3 predicates, defective fixtures and positive controls; no live producer."""
import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
import time
import unittest

from wender.perceive.disambig import (
    TurnOwnership, answer_container_for_turn, canon_name, capture_turn_boundary,
    container_ctx_for, container_vs_leaf, disambiguate, format_label,
    verify_harvested_text, wl_signature,
)
from wender.perceive.srf_identity import ax_options, last_answer_container


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _element(name='Copiar', ordinal=1, cid=None, **kw):
    row = dict(id=f'producer-{ordinal}', role='button', name=name, region='main',
               container={'id': cid or f'a{ordinal}', 'ordinal': ordinal},
               box=[300, ordinal*30, 80, 20], actionable=True, states={'enabled': True})
    row.update(kw)
    return row


def _completed(ordinal=1, terminal='Copiar'):
    return [_element('Investigado 2s', ordinal), _element(terminal, ordinal)]


def _snapshot(rows):
    return {'elements': rows}


def _rich(rows):
    return ax_options(_snapshot(copy.deepcopy(rows)), rich_options=True)[0]


def _owned():
    return capture_turn_boundary(_snapshot(_completed()), turn_index=2,
                                 task_text='What is the capital of Australia?',
                                 prev_answer_sha256=_sha('Canberra is the capital of Australia.'))


# @anchor:wa.perceive.tests_disambig.TestDisambig
class TestDisambig(unittest.TestCase):
    # @anchor:wa.perceive.tests_disambig.test_fixture_five_duplicate_copy_buttons_missing_ordinal_of_n_and_container_ctx
    def test_fixture_five_duplicate_copy_buttons_missing_ordinal_of_n_and_container_ctx(self):
        rows = [_element(ordinal=i) for i in range(1, 6)]
        legacy, _ = ax_options(_snapshot(copy.deepcopy(rows)))
        self.assertEqual(len({(e['role'], e['name'], e['region']) for e in legacy}), 1)
        self.assertTrue(all('disambig' not in e for e in legacy))
        rich = _rich(rows)
        self._assert_group(rich)
        self.assertEqual([e['disambig']['tag'] for e in rich], [f'{i}-of-5' for i in range(1, 6)])
        self.assertTrue(all(e['disambig']['container_ctx'] for e in rich))

    def _assert_group(self, options):
        count = len(options)
        for cut in (32, 192, 10000):
            self.assertEqual(len({e['label'].encode()[:cut] for e in options}), count)
        ordered = sorted(options, key=lambda e: e['disambig']['ordinal_1k'])
        self.assertEqual([e['disambig']['ordinal_1k'] for e in ordered], list(range(1, count+1)))
        for i, e in enumerate(ordered):
            d = e['disambig']
            self.assertEqual(d['cardinality'], count)
            self.assertEqual(d['neighbor_prev'], ordered[i-1]['id'] if i else None)
            self.assertEqual(d['neighbor_next'], ordered[i+1]['id'] if i+1 < count else None)
            if count > 1:
                self.assertRegex(d['wl_sig'], r'^[0-9a-f]{8}$')

    # @anchor:wa.perceive.tests_disambig.test_fixture_followup_turn_harvests_turn1_article_before_turn2_article_renders
    def test_fixture_followup_turn_harvests_turn1_article_before_turn2_article_renders(self):
        before = _snapshot(_completed())
        owner = _owned()
        self.assertEqual(last_answer_container(before), 'a1')  # defective stateless path
        self.assertIsNone(answer_container_for_turn(before, owner))
        for busy in ('Detener', 'Investigando 3s', 'Investigando', 'Stop response', 'Generating 14s'):
            after = _snapshot(_completed() + [_element(busy, 2)])
            self.assertEqual(last_answer_container(after), 'a1')
            self.assertIsNone(answer_container_for_turn(after, owner))
        self.assertIsNone(answer_container_for_turn(_snapshot(_completed()+[_element('Copiar', 2)]), owner))
        fresh = _snapshot(_completed()+_completed(9))
        self.assertEqual(answer_container_for_turn(fresh, owner), 'a9')
        self.assertEqual(last_answer_container(fresh, ownership=owner), 'a9')

    # @anchor:wa.perceive.tests_disambig.test_fixture_sibling_fallback_walks_outside_prompt_landmark_into_sidebar_nav
    def test_fixture_sibling_fallback_walks_outside_prompt_landmark_into_sidebar_nav(self):
        rows = [_element(ordinal=1), _element(ordinal=2),
                _element('SECRET SIDEBAR TITLE', role='heading', region='navigation', box=[20, 30, 200, 20])]
        naive = next(e['name'] for e in rows if e['role'] == 'heading')
        self.assertIn('SECRET SIDEBAR', naive)
        self.assertEqual(container_ctx_for(0, rows), 'main#1')
        rich = _rich(rows)
        self.assertNotIn('SECRET SIDEBAR', json.dumps(rich))
        marker, terminal = _completed()
        terminal['region'] = 'navigation'
        snap = _snapshot([marker, terminal])
        self.assertEqual(marker['container']['id'], terminal['container']['id'])
        self.assertIsNone(last_answer_container(snap))
        self.assertIsNone(answer_container_for_turn(snap))
        rows[2].update(region='main', container=rows[0]['container'])
        self.assertEqual(container_ctx_for(0, rows), 'main#1')  # x<240 guard
        rows[2].update(box=[300, 30, 200, 20], landmark_id='sidebar')
        self.assertEqual(container_ctx_for(0, rows), 'main#1')

    # @anchor:wa.perceive.tests_disambig.test_fixture_heading_only_or_question_echo_article_accepted_as_completed_answer
    def test_fixture_heading_only_or_question_echo_article_accepted_as_completed_answer(self):
        owner = _owned()
        for text, code in [('ChatGPT dijo:\n', 'heading_only'), ('# Heading', 'heading_only'),
                           ('What is the capital of Australia?', 'question_echo')]:
            self.assertTrue(bool(text.strip()))  # naive nonempty acceptance fails predicate
            ok, digest, failure = verify_harvested_text(text, owner)
            self.assertFalse(ok)
            self.assertEqual(failure, code)
            self.assertEqual(digest, _sha(text))
        for role, name in [('heading', 'ChatGPT dijo:'), ('article', 'What is the capital of Australia?')]:
            content = _element(name, 9, role=role, author='assistant')
            snap = _snapshot([content, _element('Copiar', 9)])
            self.assertTrue(any(e['name'] == 'Copiar' for e in snap['elements']))
            self.assertIsNone(answer_container_for_turn(snap, owner))
            snap['elements'].insert(1, _element('Fresh body.', 9, role='paragraph'))
            self.assertEqual(answer_container_for_turn(snap, owner), 'a9')
        self.assertTrue(verify_harvested_text('# Heading\n\nReal body', owner)[0])

    # @anchor:wa.perceive.tests_disambig.test_fixture_stale_prev_answer_sha256_accepted_as_fresh_harvest
    def test_fixture_stale_prev_answer_sha256_accepted_as_fresh_harvest(self):
        owner = _owned()
        self.assertEqual(answer_container_for_turn(_snapshot(_completed(9)), owner), 'a9')
        for text in ('Canberra is the capital of Australia.', '  Canberra  is the capital of Australia.\n'):
            self.assertTrue(bool(text))  # fresh container plus nonempty text is insufficient
            self.assertEqual(verify_harvested_text(text, owner)[2], 'stale_prev_answer_sha256')
        self.assertEqual(verify_harvested_text('New verified Turn-2 answer with arxiv+2', owner),
                         (True, _sha('New verified Turn-2 answer with arxiv+2'), None))
        prior = 'Café answer'
        owner = capture_turn_boundary({}, prev_answer_sha256=_sha(prior))
        self.assertEqual(verify_harvested_text('Cafe\u0301  answer', owner)[2], 'stale_prev_answer_sha256')

    # @anchor:wa.perceive.tests_disambig.test_predicate_rich_labels_pairwise_distinct_and_linked
    def test_predicate_rich_labels_pairwise_distinct_and_linked(self):
        for name, count in [('Copiar', 5), ('Pause', 2), ('Save', 3)]:
            with self.subTest(name=name):
                self._assert_group(_rich([_element(name, i) for i in range(count, 0, -1)]))
        rows = [_element(name, i) for i, name in enumerate(('Step', 'pacing', 'Pause', 'recovery'), 1)]
        self.assertEqual([e['label'] for e in _rich(rows)], [e['name'] for e in rows])
        for seconds in (3, 14):
            self.assertEqual(canon_name(f'Investigando {seconds}s'), ('investigando <n>', True))
            self.assertEqual(canon_name(f'Investigado {seconds}s'), ('investigado <n>', False))
        self.assertEqual(canon_name('  CAFÉ\tSave  '), canon_name('cafe\u0301 save'))
        self.assertEqual(canon_name(None), ('', False))
        self.assertFalse(canon_name('Stopwatch')[1])
        self.assertEqual(canon_name('Stop generating 3s'), ('stop', True))
        a, b = [_element('Investigando 3s', i) for i in (1, 2)], [_element('Investigando 14s', i) for i in (1, 2)]
        self.assertEqual(disambiguate(a), disambiguate(b))

    # @anchor:wa.perceive.tests_disambig.test_positive_control_legacy_byte_identical_and_turn2_fresh_harvest
    def test_positive_control_legacy_byte_identical_and_turn2_fresh_harvest(self):
        expected = [{'id': 'e0', 'role': 'button', 'name': 'Ask', 'region': 'main',
                     'container_ordinal': 1, 'value_len': None, 'focused': None,
                     'states': {'enabled': True, 'focused': None, 'expanded': None, 'checked': None, 'selected': None}}]
        rows = [_element('Ask')]
        self.assertEqual(json.dumps(ax_options(_snapshot(rows))[0], sort_keys=True), json.dumps(expected, sort_keys=True))
        rows += [_element('PRIVATE HISTORY', region='navigation'), _element('Upgrade plan')]
        expected += [{'id': 'navigation', 'role': 'group', 'name': 'Navigation/history',
                      'region': 'navigation', 'container_ordinal': None}]
        self.assertEqual(ax_options(_snapshot(rows))[0], expected)
        rich = _rich(rows)
        self.assertEqual(rich[0]['label'], 'Ask')
        self.assertEqual(rich[0]['name'], 'Ask')
        self.assertEqual(rich[0]['disambig']['wl_sig'], None)
        self.assertEqual(rich[-1]['disambig'], None)
        self.assertEqual(rich[-1]['label'], 'Navigation/history')
        owner = _owned()
        self.assertEqual(answer_container_for_turn(_snapshot(_completed(9)), owner), 'a9')
        self.assertTrue(verify_harvested_text('Fresh answer', owner)[0])
        capped = [_element('x'*121, i) for i in range(1, 66)]
        legacy, _ = ax_options(_snapshot(capped))
        self.assertEqual(len(legacy), 60)
        self.assertTrue(all(len(e['name']) == 120 for e in legacy))
        rich = _rich(capped + [_element('PRIVATE HISTORY', region='navigation')])
        self.assertEqual(len(rich), 60)
        self.assertEqual(rich[0]['disambig']['cardinality'], 59)
        # Different raw names can collide after the frozen display-name cap.
        truncated = _rich([_element('x'*120+'a', 1), _element('x'*120+'b', 2)])
        self._assert_group(truncated)

    # @anchor:wa.perceive.tests_disambig.test_containment_prefix_and_integer_r10_controls
    def test_containment_prefix_and_integer_r10_controls(self):
        parent = _element('Message Mark read Acknowledge', role='row', anchor_id='inbox.item.msg-103', box=[300, 20, 400, 80])
        children = [_element('Mark read', anchor_id='inbox.item.msg-103.read', box=[320, 30, 60, 20]),
                    _element('Acknowledge', anchor_id='inbox.item.msg-103.ack', box=[400, 30, 60, 20])]
        rows = [parent] + children
        self.assertEqual(container_vs_leaf(rows), {0: [1, 2]})
        rich = _rich(rows)
        self.assertNotIn('Mark read', rich[0]['label'])
        self.assertNotIn('Acknowledge', rich[0]['label'])
        self.assertIn('container: select_row', rich[0]['label'])
        self.assertEqual(rich[0]['disambig']['child_actions'][0]['id'], 'e1')
        for child in children:
            child.pop('anchor_id')
        self.assertEqual(container_vs_leaf(rows), {0: [1, 2]})  # geometry-only positive
        children[0]['box'] = [1000, 1000, 60, 20]
        self.assertEqual(container_vs_leaf(rows), {0: [2]})
        children[0]['anchor_id'] = 'inbox.item.msg-103.read'
        self.assertEqual(container_vs_leaf(rows), {0: [1, 2]})  # prefix-only spec path
        children[0]['region'] = 'navigation'
        self.assertEqual(container_vs_leaf(rows), {0: [2]})
        # Exact cross-multiplication threshold: 85/100 passes, 84/100 fails.
        rows = [_element('row', role='row', box=[300, 20, 200, 20]),
                _element('act', box=[483, 20, 20, 10])]
        self.assertEqual(container_vs_leaf(rows), {0: [1]})
        rows[1]['box'][0] = 484
        self.assertEqual(container_vs_leaf(rows), {})

    # @anchor:wa.perceive.tests_disambig.test_context_ancestors_cycles_prompt_landmark_and_privacy
    def test_context_ancestors_cycles_prompt_landmark_and_privacy(self):
        rows = [_element('Copiar', id='control', parent_id='head'),
                _element('Answer controls', id='head', role='heading')]
        self.assertEqual(container_ctx_for(0, rows), 'Answer controls')
        self.assertIsNone(container_ctx_for(0, rows, prompt_landmark_id='different'))
        rows[1]['parent_id'] = 'control'
        rows[1]['role'] = 'article'
        rows[1]['name'] = 'PRIVATE ANSWER PROSE'
        self.assertEqual(container_ctx_for(0, rows), 'main#1')
        rows[1]['role'], rows[1]['name'] = 'heading', 'https://private.invalid/title'
        self.assertEqual(container_ctx_for(0, rows), 'main#1')
        before = copy.deepcopy(rows)
        disambiguate(rows)
        self.assertEqual(rows, before)

    # @anchor:wa.perceive.tests_disambig.test_grid_digits_have_structural_row_and_column_metadata
    def test_grid_digits_have_structural_row_and_column_metadata(self):
        rows = [_element('', role='table', id='table', actionable=False),
                _element('', role='row', id='row', parent_id='table', actionable=False),
                _element('Name', role='columnheader', id='h1', actionable=False),
                _element('Count', role='columnheader', id='h2', actionable=False),
                _element('Item', role='gridcell', id='key', parent_id='row', box=[300, 30, 30, 20]),
                _element('1', role='gridcell', id='digit', parent_id='row', box=[400, 30, 30, 20])]
        digit = _rich(rows)[-1]
        grid = digit['disambig']['row_col_header']
        self.assertEqual((grid['row'], grid['rows'], grid['col'], grid['cols']), (1, 1, 2, 2))
        self.assertEqual((grid['header'], grid['row_key']), ('Count', 'Item'))
        self.assertIn('row=1/1 col=2/2', digit['label'])
        self.assertNotEqual(digit['label'], '1')
        self.assertNotIn('producer-', digit['label'])

    # @anchor:wa.perceive.tests_disambig.test_wl_scope_focus_determinism_and_unicode_budget
    def test_wl_scope_focus_determinism_and_unicode_budget(self):
        rows = [_element(ordinal=i) for i in (1, 2)]
        a = disambiguate(rows)
        rows[0]['focused'], rows[0]['states']['focused'] = True, True
        self.assertEqual(a, disambiguate(rows))
        self.assertEqual(wl_signature(0, rows, {0: [('row', 1)]}), wl_signature(0, rows, {0: [('row', 1)]}))
        self.assertNotEqual(wl_signature(0, rows, {}), wl_signature(0, rows, {0: [('row', 1)]}))
        d = dict(a[0], container_ctx='界'*32)
        label = format_label({'name': 'Copiar'}, d)
        self.assertLessEqual(len(label.encode())-len('Copiar'.encode()), 48)
        with self.assertRaises(ValueError):
            format_label({'name': 'Copiar'}, d, max_suffix_bytes=1)
        with self.assertRaises(ValueError):
            disambiguate(rows, token_cap_bytes=0)
        scoped = _rich(_completed()+_completed(9)+[_element('Copiar', 10, scope='code_block'),
                                               _element('Copiar', 11, scope='user_turn')])
        copies = [e for e in scoped if e['name'] == 'Copiar']
        self.assertEqual([e['disambig']['scope'] for e in copies],
                         ['answer:1-of-2,non_last', 'after:last(button "Investigado*")', 'code_block', 'user_turn'])

    # @anchor:wa.perceive.tests_disambig.test_owned_terminals_boundaries_and_newest_incomplete_fail_closed
    def test_owned_terminals_boundaries_and_newest_incomplete_fail_closed(self):
        owner = _owned()
        for terminal in ('Copiar', 'Copy', 'Bifurcar', 'No útil', '5 fuentes'):
            rows = _completed(9, terminal)
            self.assertEqual(answer_container_for_turn(_snapshot(rows), owner), 'a9')
            rows[1]['scope'] = 'user_turn'
            self.assertIsNone(answer_container_for_turn(_snapshot(rows), owner))
            rows[1]['scope'] = 'code_block'
            self.assertIsNone(answer_container_for_turn(_snapshot(rows), owner))
        for ordinal in (None, True, '9', 1):
            rows = _completed(9)
            for e in rows:
                e['container']['ordinal'] = ordinal
            self.assertIsNone(answer_container_for_turn(_snapshot(rows), owner))
        self.assertIsNone(answer_container_for_turn(_snapshot(_completed(9)+[_element('Investigado 3s', 10)]), owner))
        self.assertIsNone(answer_container_for_turn(_snapshot(list(reversed(_completed(9)))), owner))
        self.assertIsNone(answer_container_for_turn(_snapshot(_completed(9, 'Copiar mensaje')), owner))
        rows = _completed(9)+[_element('unrelated', 11, states={'busy': True})]
        self.assertIsNone(answer_container_for_turn(_snapshot(rows), owner))
        rows[-1]['region'] = 'navigation'
        self.assertEqual(answer_container_for_turn(_snapshot(rows), owner), 'a9')

    # @anchor:wa.perceive.tests_disambig.test_terminal_inherited_scope_and_uncapped_context
    def test_terminal_inherited_scope_and_uncapped_context(self):
        for scope in ('user_turn', 'code_block'):
            parent = _element('', 9, role='group', scope=scope, id='nested', actionable=False)
            marker, terminal = _completed(9)
            terminal['parent_id'] = 'nested'
            snap = _snapshot([marker, parent, terminal])
            self.assertIsNone(answer_container_for_turn(snap, _owned()))
            rich = _rich(snap['elements'])
            self.assertEqual(rich[-1]['disambig']['scope'], scope)
            parent.pop('scope')
            self.assertEqual(answer_container_for_turn(snap, _owned()), 'a9')
        # Non-actionable parent context survives the option projection/cap.
        control = _element('Ask', parent_id='heading')
        heading = _element('Composer controls', id='heading', role='heading', actionable=False)
        rich = _rich([control, heading])
        self.assertEqual(len(rich), 1)
        self.assertEqual(rich[0]['disambig']['container_ctx'], 'Composer controls')

    # @anchor:wa.perceive.tests_disambig.test_ownership_hash_only_immutable_and_text_failures
    def test_ownership_hash_only_immutable_and_text_failures(self):
        owner = _owned()
        with self.assertRaises(ValueError):
            TurnOwnership(1, None, None, None, frozenset())
        self.assertNotIn('capital', repr(owner))
        with self.assertRaises(FrozenInstanceError):
            owner.turn_index = 3
        for bad in (0, True, '1'):
            with self.assertRaises(ValueError):
                capture_turn_boundary({}, turn_index=bad)
        with self.assertRaises(ValueError):
            capture_turn_boundary({}, prev_answer_sha256='raw answer')
        self.assertEqual(capture_turn_boundary({}).boundary_max_ordinal, None)
        self.assertEqual(capture_turn_boundary({}).boundary_containers, frozenset())
        for text in (None, '', ' \n '):
            self.assertEqual(verify_harvested_text(text, owner), (False, None, 'empty_text'))
        self.assertEqual(verify_harvested_text(' WHAT is the CAPITAL of Australia? ', owner)[2], 'question_echo')

    # @anchor:wa.perceive.tests_disambig.test_latency_p95_sixty_options
    def test_latency_p95_sixty_options(self):
        rows = [_element('Copiar', i) for i in range(60)]
        disambiguate(rows)
        timings = []
        for _ in range(100):
            start = time.perf_counter_ns()
            disambiguate(rows)
            timings.append(time.perf_counter_ns()-start)
        p95 = sorted(timings)[94]
        print(f'W3 disambiguate 60 options p95_ns={p95} samples={len(timings)} [MEASURED]')
        self.assertLessEqual(p95, 10_000_000)


if __name__ == '__main__':
    unittest.main()
