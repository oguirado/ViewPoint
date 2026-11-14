import copy
import importlib
import json
import unittest
from unittest.mock import Mock, call, patch
from wender.perceive import ax
from wender.perceive.stopstate import IncompleteTree, _tree, detect
from wender.perceive.text import extract
from wender.perceive.wait import wait_done
class StopRequested(RuntimeError):
 def __init__(self, result):
  self.result = copy.deepcopy(result); super().__init__('STOP: ' + result['reason'])
class GuardedDriverReference():
 def __init__(self, cdp, driver, *, url='', frame_urls=(), headers=None):
  self.cdp, self.driver = (cdp, driver); self.metadata = {'url': url, 'frame_urls': frame_urls, 'headers': headers}; self.stopped = None
 def _check(self):
  if self.stopped is not None:
   raise StopRequested(self.stopped)
 def observe(self):
  self._check(); self.cdp.send('Accessibility.enable', session_id='fixture')
  try:
   raw = self.cdp.send('Accessibility.getFullAXTree', session_id='fixture')['nodes']
  finally:
   self.cdp.send('Accessibility.disable', session_id='fixture')
  result = detect(raw, **self.metadata)
  if result['is_stop']:
   self.stopped = result
   self._check()
  _tree(raw); return self.driver.observe()
 def candidate_actions(self, state):
  self._check(); return self.driver.candidate_actions(state)
 def act(self, action):
  self._check(); return self.driver.act(action)
 def reset(self):
  self._check(); return self.driver.reset()
ASSISTANT = {'role': 'heading', 'name': 'ChatGPT dijo:'}
COPY = {'role': 'button', 'name': 'Copiar mensaje'}
STOP = {'role': 'button', 'name': 'Detener'}
TRAILERS = ('ChatGPT puede cometer errores', 'Última respuesta', 'Respuesta completada')
CHATGPT_RULES = {'absent_roles_names': [STOP], 'after_last_anchor': ASSISTANT, 'present_roles_names': [COPY], 'quiet_snapshots': 2}
def node(nid, role, name='', children=(), *, ignored=False, hidden=False):
 result = {'nodeId': nid, 'role': {'value': role}, 'name': {'value': name}, 'childIds': list(children), 'ignored': ignored}
 if hidden:
  result['properties'] = [{'name': 'hidden', 'value': {'value': True}}]
 return result
def chat_snapshot(body='Hello world', *, assistant_copy=True, streaming=False, later_user=False, wrapped=True):
 children = ['anchor', 'body'] + (['assistant-copy'] if assistant_copy else [])
 if streaming:
  children.append('stop')
 raw = [node('root', 'RootWebArea', children=['user', 'assistant'] + (['later-user'] if later_user else [])), node('user', 'article', children=['user-text', 'user-copy']), node('user-text', 'StaticText', 'USER QUESTION'), node('user-copy', 'button', 'Copiar mensaje'), node('assistant', 'article' if wrapped else 'group', children=children), node('anchor', 'heading', 'ChatGPT dijo:'), node('body', 'StaticText', body)]
 if assistant_copy:
  raw.append(node('assistant-copy', 'button', 'Copiar mensaje'))
 if streaming:
  raw.append(node('stop', 'button', 'Detener'))
 if later_user:
  raw += [node('later-user', 'article', children=['later-text', 'later-copy']), node('later-text', 'StaticText', 'LATER USER'), node('later-copy', 'button', 'Copiar mensaje')]
 return raw
def answer_tree():
 raw = [node('root', 'RootWebArea', children=['old', 'user', 'latest']), node('old', 'article', children=['old-anchor', 'old-text', 'old-copy']), node('old-anchor', 'heading', 'ChatGPT dijo:'), node('old-text', 'StaticText', 'OLD ANSWER'), node('old-copy', 'button', 'Copiar mensaje'), node('user', 'article', children=['user-text', 'user-copy']), node('user-text', 'StaticText', 'USER QUESTION'), node('user-copy', 'button', 'Copiar mensaje'), node('latest', 'article', children=['anchor', 'wrapper', 'own-copy', 'outside']), node('anchor', 'heading', 'ChatGPT dijo:', children=['anchor-text']), node('anchor-text', 'StaticText', 'ChatGPT dijo:'), node('wrapper', 'generic', children=['heading', 'paragraph', 'list', 'row', 'code', 'excluded', 'trailer1', 'trailer2', 'trailer3'], ignored=True), node('heading', 'heading', 'Results', children=['heading-text']), node('heading-text', 'StaticText', 'Results'), node('paragraph', 'paragraph', children=['hello']), node('hello', 'StaticText', 'Hello world', children=['inline-hello', 'inline-world']), node('inline-hello', 'InlineTextBox', 'Hello'), node('inline-world', 'InlineTextBox', 'world'), node('list', 'list', children=['li1', 'li2']), node('li1', 'listitem', children=['yes1']), node('yes1', 'StaticText', 'Yes'), node('li2', 'listitem', children=['yes2']), node('yes2', 'StaticText', 'Yes'), node('row', 'row', children=['left', 'right']), node('left', 'StaticText', 'A'), node('right', 'StaticText', 'B'), node('code', 'code', children=['code-text']), node('code-text', 'StaticText', 'if ready:\n    emit()'), node('excluded', 'button', 'Share', children=['excluded-text']), node('excluded-text', 'StaticText', 'SHARE UI'), node('trailer1', 'StaticText', 'ChatGPT puede cometer errores'), node('trailer2', 'StaticText', 'Última respuesta'), node('trailer3', 'StaticText', 'Respuesta completada'), node('own-copy', 'button', 'Copiar mensaje', children=['copy-label']), node('copy-label', 'StaticText', 'Copiar mensaje'), node('outside', 'StaticText', 'OUTSIDE ANSWER')]
 return list(reversed(raw))
def wall_tree(role='checkbox', name="I'm not a robot", *, hidden=False):
 return [node('root', 'RootWebArea', children=['cue']), node('cue', role, name, hidden=hidden)]
class FakeTime():
 def __init__(self):
  self.ms = 0; self.sleeps = []
 def clock(self):
  return self.ms
 def sleep(self, delay_ms):
  self.sleeps.append(delay_ms); self.ms += delay_ms
class WaitReferenceContracts(unittest.TestCase):
 def run_wait(self, snapshots, *, rules=None, max_polls=None, interval=10, timeout=None):
  timer, times = (FakeTime(), []); snapshots = copy.deepcopy(snapshots); index = 0
  def read():
   nonlocal index
   times.append(timer.ms)
   result = snapshots[min(index, len(snapshots) - 1)]
   index += 1
   return copy.deepcopy(result)
  result = wait_done(read, CHATGPT_RULES if rules is None else rules, clock=timer.clock, sleep=timer.sleep, max_polls=len(snapshots) if max_polls is None else max_polls, poll_interval_ms=interval, timeout_ms=timeout)
  return (result, times, timer.sleeps)
 def test_body_change_then_two_equal_completes_at_poll_three(self):
  a, b = (chat_snapshot('H'), chat_snapshot('Hello world')); self.assertEqual(ax.snapshot_sha(ax.prune(a)), ax.snapshot_sha(ax.prune(b))); result, times, sleeps = self.run_wait([a, b, b])
  self.assertEqual(result, {'done': True, 'polls': 3, 'completion_status': 'confirmed_by_adapter'}); self.assertEqual(times, [0, 10, 20]); self.assertEqual(sleeps, [10, 10])
 def test_three_equal_snapshots_complete_at_poll_four(self):
  rules = {**CHATGPT_RULES, 'quiet_snapshots': 3}; a, b = (chat_snapshot('H'), chat_snapshot('Hello world')); result, times, sleeps = self.run_wait([a, b, b, b], rules=rules)
  self.assertEqual(result, {'done': True, 'polls': 4, 'completion_status': 'confirmed_by_adapter'}); self.assertEqual(times, [0, 10, 20, 30]); self.assertEqual(sleeps, [10, 10, 10])
 def test_user_copy_before_anchor_never_confirms(self):
  result, times, sleeps = self.run_wait([chat_snapshot(assistant_copy=False)], max_polls=4); self.assertEqual(result, {'done': False, 'polls': 4, 'completion_status': 'timeout'})
  self.assertEqual(times, [0, 10, 20, 30]); self.assertEqual(len(sleeps), 3)
 def test_later_user_copy_is_not_the_assistant_copy(self):
  result, _, _ = self.run_wait([chat_snapshot(assistant_copy=False, later_user=True)], max_polls=3); self.assertEqual(result, {'done': False, 'polls': 3, 'completion_status': 'timeout'})
 def test_active_stop_vetoes_even_an_own_copy_and_stable_answer(self):
  result, _, _ = self.run_wait([chat_snapshot(streaming=True)], max_polls=3); self.assertEqual(result, {'done': False, 'polls': 3, 'completion_status': 'timeout'})
 def test_streaming_then_changed_then_repeat_confirms(self):
  result, _, _ = self.run_wait([chat_snapshot('H', streaming=True), chat_snapshot('Hello'), chat_snapshot('Hello')]); self.assertEqual(result['polls'], 3)
  self.assertTrue(result['done'])
 def test_latest_anchor_must_own_its_copy(self):
  raw = chat_snapshot(); raw[0]['childIds'].append('new'); raw += [node('new', 'article', children=['new-anchor', 'new-body']), node('new-anchor', 'heading', 'ChatGPT dijo:'), node('new-body', 'StaticText', 'New')]
  result, _, _ = self.run_wait([raw], max_polls=3); self.assertEqual(result['completion_status'], 'timeout')
 def test_absent_article_scope_fails_closed(self):
  result, _, _ = self.run_wait([chat_snapshot(wrapped=False)], max_polls=2); self.assertFalse(result['done']); self.assertEqual(result['completion_status'], 'timeout')
 def test_copy_inside_anchor_heading_subtree_cannot_confirm(self):
  raw = chat_snapshot(); next((n for n in raw if n['nodeId'] == 'assistant'))['childIds'].remove('assistant-copy'); next((n for n in raw if n['nodeId'] == 'anchor'))['childIds'].append('assistant-copy')
  result, _, _ = self.run_wait([raw], max_polls=3); self.assertEqual(result, {'done': False, 'polls': 3, 'completion_status': 'timeout'})
 def test_all_present_conditions_are_required(self):
  rules = {**CHATGPT_RULES, 'present_roles_names': [COPY, {'role': 'status', 'name': 'Finished'}]}; result, _, _ = self.run_wait([chat_snapshot()], rules=rules, max_polls=3)
  self.assertEqual(result['completion_status'], 'timeout')
 def test_quiet_alone_is_stable_unconfirmed_with_done_false(self):
  rules = {'quiet_snapshots': 2}; result, times, sleeps = self.run_wait([chat_snapshot(), chat_snapshot()], rules=rules); self.assertEqual(result, {'done': False, 'polls': 2, 'completion_status': 'stable_unconfirmed'})
  self.assertEqual(times, [0, 10]); self.assertEqual(sleeps, [10])
 def test_never_settling_stream_exhausts_exact_poll_budget(self):
  snapshots = [chat_snapshot(str(i)) for i in range(5)]; result, times, sleeps = self.run_wait(snapshots); self.assertEqual(result, {'done': False, 'polls': 5, 'completion_status': 'timeout'})
  self.assertEqual(times, [0, 10, 20, 30, 40]); self.assertEqual(sleeps, [10] * 4)
 def test_zero_poll_budget_does_not_acquire_or_sleep(self):
  result, times, sleeps = self.run_wait([], max_polls=0); self.assertEqual(result, {'done': False, 'polls': 0, 'completion_status': 'timeout'}); self.assertEqual((times, sleeps), ([], []))
 def test_deadline_truncates_final_sleep_and_forbids_read_at_deadline(self):
  result, times, sleeps = self.run_wait([chat_snapshot(streaming=True)], max_polls=9, timeout=25); self.assertEqual(result, {'done': False, 'polls': 3, 'completion_status': 'timeout'})
  self.assertEqual(times, [0, 10, 20]); self.assertEqual(sleeps, [10, 10, 5])
 def test_slow_read_at_deadline_is_counted_but_cannot_confirm(self):
  timer = FakeTime()
  def read():
   timer.ms += 10
   return chat_snapshot()
  result = wait_done(read, CHATGPT_RULES, clock=timer.clock, sleep=timer.sleep, max_polls=5, poll_interval_ms=0, timeout_ms=20); self.assertEqual(result, {'done': False, 'polls': 2, 'completion_status': 'timeout'})
  self.assertEqual(timer.sleeps, [0])
 def test_reader_exception_propagates_without_retry(self):
  timer, read = (FakeTime(), Mock(side_effect=OSError('fixture transport failure')))
  with self.assertRaisesRegex(OSError, 'fixture transport failure'):
   wait_done(read, CHATGPT_RULES, clock=timer.clock, sleep=timer.sleep, max_polls=3)
  read.assert_called_once_with(); self.assertEqual(timer.sleeps, [])
 def test_malformed_snapshot_resets_quiet_run(self):
  good = chat_snapshot(); bad = copy.deepcopy(good); bad[0]['childIds'].append('missing'); result, _, _ = self.run_wait([good, bad, good, good]); self.assertEqual(result['polls'], 4)
  self.assertTrue(result['done'])
 def test_flat_cdp_order_does_not_change_document_snapshot(self):
  good = chat_snapshot(); result, _, _ = self.run_wait([good, list(reversed(good))]); self.assertTrue(result['done']); self.assertEqual(result['polls'], 2)
 def test_reader_reusing_mutable_raw_list_still_resets_on_text_change(self):
  timer, raw = (FakeTime(), chat_snapshot('H')); body = next((n for n in raw if n['nodeId'] == 'body')); texts = iter(('H', 'Hello', 'Hello'))
  def read():
   body['name']['value'] = next(texts)
   return raw
  result = wait_done(read, CHATGPT_RULES, clock=timer.clock, sleep=timer.sleep, max_polls=3, poll_interval_ms=10); self.assertEqual(result, {'done': True, 'polls': 3, 'completion_status': 'confirmed_by_adapter'})
  self.assertEqual(timer.sleeps, [10, 10])
 def test_one_poll_budget_has_no_terminal_sleep(self):
  result, times, sleeps = self.run_wait([chat_snapshot()], max_polls=1); self.assertEqual(result, {'done': False, 'polls': 1, 'completion_status': 'timeout'})
  self.assertEqual((times, sleeps), ([0], []))
 def test_backward_clock_fails_loudly(self):
  sleep = Mock()
  with self.assertRaisesRegex(ValueError, 'monotonic integer'):
   wait_done(lambda: chat_snapshot(), CHATGPT_RULES, clock=Mock(side_effect=[10, 9]), sleep=sleep, max_polls=2)
  sleep.assert_not_called()
 def test_invalid_budgets_rules_and_clock_fail_loudly(self):
  timer = FakeTime()
  for kwargs in ({'max_polls': -1}, {'max_polls': True}, {'max_polls': 1, 'poll_interval_ms': -1}, {'max_polls': 1, 'timeout_ms': 0}):
   with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
    wait_done(Mock(), CHATGPT_RULES, clock=timer.clock, sleep=timer.sleep, **kwargs)
  with self.assertRaises(ValueError):
   wait_done(Mock(), {'quiet_snapshots': 1}, clock=timer.clock, sleep=timer.sleep, max_polls=2)
  with self.assertRaises(ValueError):
   wait_done(Mock(), {'unknown': True}, clock=timer.clock, sleep=timer.sleep, max_polls=2)
  with self.assertRaises(ValueError):
   wait_done(Mock(), CHATGPT_RULES, clock=lambda: 1.5, sleep=timer.sleep, max_polls=2)
class TextReferenceContracts(unittest.TestCase):
 def answer(self, raw=None, trailers=TRAILERS):
  return extract(answer_tree() if raw is None else raw, ASSISTANT, COPY, excluded_roles_names=[{'role': 'button', 'name': 'Share'}], trailers=trailers)
 def test_latest_answer_hierarchy_blocks_and_inline_dedup(self):
  result = self.answer(); self.assertEqual(result, {'text': 'Results\nHello world\nYes\nYes\nA B\nif ready:\n    emit()', 'extraction_status': 'clean'})
  self.assertEqual(result['text'].splitlines().count('Yes'), 2); self.assertEqual(result['text'].count('Hello world'), 1)
  for excluded in ('USER QUESTION', 'OLD ANSWER', 'OUTSIDE ANSWER', 'SHARE UI', *TRAILERS, 'ChatGPT dijo:', 'Copiar mensaje'):
   self.assertNotIn(excluded, result['text'])
 def test_current_pruner_discards_article_answer_text(self):
  self.assertNotIn('Hello world', [n['name'] for n in ax.prune(answer_tree())]); self.assertIn('Hello world', self.answer()['text'])
 def test_distinct_same_text_nodes_survive_but_same_edge_emits_once(self):
  raw = answer_tree(); paragraph = next((n for n in raw if n['nodeId'] == 'paragraph')); paragraph['childIds'].append('hello'); result = self.answer(raw)
  self.assertEqual(result['extraction_status'], 'clean'); self.assertEqual(result['text'].count('Hello world'), 1); self.assertEqual(result['text'].splitlines().count('Yes'), 2)
 def test_whitespace_only_answer_is_empty_clean(self):
  result = self.answer(chat_snapshot(' \n\t  ')); self.assertEqual(result, {'text': '', 'extraction_status': 'clean'})
 def test_trailers_only_strip_exact_configured_suffix_blocks(self):
  result = self.answer(trailers=()); self.assertTrue(result['text'].endswith('\n'.join(TRAILERS))); raw = answer_tree(); next((n for n in raw if n['nodeId'] == 'hello'))['name']['value'] = 'Última respuesta'
  next((n for n in raw if n['nodeId'] == 'trailer3'))['name']['value'] = 'Respuesta completada is quoted here'; result = self.answer(raw); self.assertIn('Results\nÚltima respuesta\nYes', result['text'])
  self.assertTrue(result['text'].endswith('Respuesta completada is quoted here')); self.assertIn('ChatGPT puede cometer errores', result['text'])
 def test_latest_assistant_missing_own_copy_is_ambiguous(self):
  result = self.answer(chat_snapshot(assistant_copy=False, later_user=True)); self.assertEqual(result, {'text': '', 'extraction_status': 'ambiguous_boundary'})
 def test_copy_inside_anchor_heading_subtree_is_not_an_end_boundary(self):
  raw = chat_snapshot(); next((n for n in raw if n['nodeId'] == 'assistant'))['childIds'].remove('assistant-copy'); next((n for n in raw if n['nodeId'] == 'anchor'))['childIds'].append('assistant-copy')
  self.assertEqual(self.answer(raw), {'text': '', 'extraction_status': 'ambiguous_boundary'})
 def test_code_only_answer_retains_leading_indent_and_trailing_newline(self):
  raw = chat_snapshot('    emit()\n'); next((n for n in raw if n['nodeId'] == 'body'))['role']['value'] = 'code'; self.assertEqual(self.answer(raw), {'text': '    emit()\n', 'extraction_status': 'clean'})
 def test_boundary_missing_anchor_end_article_or_duplicate_end_is_ambiguous(self):
  fixtures = [chat_snapshot(assistant_copy=False), chat_snapshot(wrapped=False)]; no_anchor = chat_snapshot(); next((n for n in no_anchor if n['nodeId'] == 'anchor'))['name']['value'] = 'Other heading'
  fixtures.append(no_anchor); duplicate_end = chat_snapshot(); next((n for n in duplicate_end if n['nodeId'] == 'assistant'))['childIds'].append('copy2')
  duplicate_end.append(node('copy2', 'button', 'Copiar mensaje')); fixtures.append(duplicate_end)
  for raw in fixtures:
   with self.subTest(raw=raw):
    self.assertEqual(self.answer(raw), {'text': '', 'extraction_status': 'ambiguous_boundary'})
 def test_missing_child_duplicate_record_cycle_and_multiple_parent_are_incomplete(self):
  missing = chat_snapshot(); missing[0]['childIds'].append('missing'); duplicate = chat_snapshot(); duplicate.append(copy.deepcopy(duplicate[-1])); cycle = chat_snapshot()
  next((n for n in cycle if n['nodeId'] == 'body'))['childIds'].append('root'); shared = chat_snapshot(); next((n for n in shared if n['nodeId'] == 'user'))['childIds'].append('body')
  parent_mismatch = chat_snapshot(); next((n for n in parent_mismatch if n['nodeId'] == 'body'))['parentId'] = 'user'
  for raw in (missing, duplicate, cycle, shared, parent_mismatch):
   with self.subTest(raw=raw):
    self.assertEqual(self.answer(raw), {'text': '', 'extraction_status': 'incomplete_tree'})
 def test_heading_name_fallback_and_orphan_inline_text_have_document_order(self):
  raw = chat_snapshot(); owner = next((n for n in raw if n['nodeId'] == 'assistant')); owner['childIds'][1:1] = ['fallback', 'orphan']; raw += [node('fallback', 'heading', 'Fallback heading'), node('orphan', 'InlineTextBox', 'Standalone text')]
  self.assertEqual(self.answer(raw)['text'], 'Fallback heading\nStandalone text\nHello world')
 def test_hidden_parent_suppresses_text_ignored_wrapper_does_not(self):
  raw = answer_tree(); wrapper = next((n for n in raw if n['nodeId'] == 'wrapper')); self.assertTrue(wrapper['ignored']); self.assertIn('Hello world', self.answer(raw)['text'])
  wrapper['properties'] = [{'name': 'hidden', 'value': {'value': True}}]; self.assertEqual(self.answer(raw), {'text': '', 'extraction_status': 'clean'})
 def test_no_64_byte_name_truncation_and_no_input_mutation(self):
  raw = chat_snapshot('é' * 100 + ' END'); saved = copy.deepcopy(raw); self.assertEqual(self.answer(raw)['text'], 'é' * 100 + ' END'); self.assertEqual(raw, saved)
  self.assertEqual(self.answer(raw)['extraction_status'], 'clean')
class StopReferenceContracts(unittest.TestCase):
 def assert_stop(self, result, reason):
  self.assertEqual(set(result), {'is_stop', 'reason', 'confidence', 'evidence'}); self.assertTrue(result['is_stop']); self.assertEqual(result['reason'], reason)
  self.assertIs(type(result['confidence']), int); self.assertGreaterEqual(result['confidence'], 1); self.assertLessEqual(result['confidence'], 100)
  self.assertTrue(result['evidence'])
  for item in result['evidence']:
   self.assertEqual(set(item), {'kind', 'value'})
   self.assertTrue(item['value'])
 def assert_clear(self, result):
  self.assertEqual(result, {'is_stop': False, 'reason': None, 'confidence': 0, 'evidence': []})
 def test_google_sorry_index_trailing_slash_query_and_base_path(self):
  for path in ('/sorry/index', '/sorry/index/', '/sorry/index?continue=private', '/sorry/', '/sorry'):
   with self.subTest(path=path):
    result = detect([], url='https://www.google.com' + path)
    self.assert_stop(result, 'google_sorry')
    self.assertNotIn('private', json.dumps(result))
 def test_non_google_host_with_sorry_url_text_and_deceptive_hosts_are_negative(self):
  for url in ('https://example.test/sorry/index?google.com=sorry', 'https://www.google.com.evil.test/sorry/index', 'https://www.google.com@evil.test/sorry/index', 'https://notgoogle.com/sorry/index', 'https://www.google.com/sorry/indexed', 'https://example.test/sorry/google.com', 'javascript:https://google.com/sorry/index'):
   with self.subTest(url=url):
    self.assert_clear(detect([], url=url))
 def test_interactive_recaptcha_v2_all_required_hosts_and_paths(self):
  for host in ('www.google.com', 'recaptcha.google.com', 'www.recaptcha.net'):
   for path in ('/recaptcha/api2/anchor', '/recaptcha/api2/bframe', '/recaptcha/enterprise/anchor'):
    with self.subTest(host=host, path=path):
     self.assert_stop(detect(wall_tree(), frame_urls=['https://' + host + path + '?size=normal']), 'recaptcha_v2')
  self.assert_stop(detect(wall_tree('dialog', 'reCAPTCHA'), frame_urls=['https://www.google.com/recaptcha/api2/bframe']), 'recaptcha_v2')
 def test_v3_invisible_background_and_scripts_do_not_stop(self):
  for raw, frames in (([], ['https://www.google.com/recaptcha/api2/anchor?size=invisible']), (wall_tree(), ['https://www.google.com/recaptcha/api2/anchor?size=invisible']), ([], ['https://www.google.com/recaptcha/api2/anchor?size=normal']), (wall_tree(hidden=True), ['https://www.google.com/recaptcha/api2/anchor']), (wall_tree(), ['https://www.google.com/recaptcha/api.js?render=v3-key']), (wall_tree(), ['https://www.recaptcha.net/recaptcha/enterprise.js']), (wall_tree(), ['https://www.google.com.evil.test/recaptcha/api2/anchor'])):
   with self.subTest(raw=raw, frames=frames):
    self.assert_clear(detect(raw, frame_urls=frames))
 def test_visible_turnstile_challenge(self):
  frames = ['https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2/av0/fixture']; raw = wall_tree('Iframe', 'Widget containing a Cloudflare security challenge')
  self.assert_stop(detect(raw, frame_urls=frames), 'turnstile_challenge'); self.assert_stop(detect(wall_tree('checkbox', 'Verify you are human'), frame_urls=frames), 'turnstile_challenge')
  self.assert_clear(detect([], frame_urls=frames)); self.assert_clear(detect(wall_tree('Iframe', 'Cloudflare security challenge', hidden=True), frame_urls=frames))
  self.assert_clear(detect(raw, frame_urls=['https://challenges.cloudflare.com/turnstile/v0/api.js'])); self.assert_clear(detect(raw, frame_urls=['https://challenges.cloudflare.com/cdn-cgi/challenge-platform/script.js']))
 def test_cloudflare_main_document_challenge_needs_html(self):
  self.assert_stop(detect([], headers={'CF-Mitigated': ' challenge ', 'Content-Type': 'Text/HTML; charset=UTF-8'}), 'cloudflare_challenge')
  for headers in ({'cf-mitigated': 'challenge'}, {'cf-mitigated': 'challenge', 'content-type': 'application/json'}, {'cf-mitigated': 'not-a-challenge', 'content-type': 'text/html'}, {'status': '429', 'content-type': 'text/html'}):
   with self.subTest(headers=headers):
    self.assert_clear(detect([], headers=headers))
 def test_unusual_traffic_wall_has_meaningful_text_evidence(self):
  phrase = 'Our systems have detected unusual traffic from your computer network.'; result = detect(wall_tree('StaticText', phrase), url='https://example.test/')
  self.assert_stop(result, 'unusual_traffic'); self.assertEqual(result['evidence'], [{'kind': 'text', 'value': phrase}]); self.assert_clear(detect(wall_tree('StaticText', 'An article about unusual traffic')))
  self.assert_clear(detect(wall_tree('StaticText', phrase, hidden=True)))
 def test_429_cookie_consent_login_and_generic_verify_are_negative(self):
  for role, name in (('dialog', 'Cookie consent'), ('button', 'Accept all cookies'), ('button', 'Log in'), ('textbox', 'Password'), ('button', 'Verify'), ('checkbox', 'Verify you are human'), ('checkbox', "I'm not a robot")):
   with self.subTest(role=role, name=name):
    self.assert_clear(detect(wall_tree(role, name), url='https://example.test/login', headers={'status': '429'}))
 def test_state_input_and_explicit_url_override_do_not_mutate_state(self):
  state = {'source': {'url': 'https://www.google.com/sorry/index?private=1'}, 'nodes': [{'id': 'r', 'parent': None, 'role': 'heading', 'name': 'Login', 'states': {}}]}
  saved = copy.deepcopy(state); self.assert_stop(detect(state), 'google_sorry'); self.assert_clear(detect(state, url='https://example.test/')); self.assertEqual(state, saved)
 def test_hidden_state_ancestor_cannot_supply_interactive_evidence(self):
  state = {'nodes': [{'id': 'p', 'parent': None, 'role': 'group', 'name': '', 'states': {'hidden': 'true'}}, {'id': 'c', 'parent': 'p', 'role': 'checkbox', 'name': "I'm not a robot", 'states': {}}]}
  self.assert_clear(detect(state, frame_urls=['https://www.google.com/recaptcha/api2/anchor']))
 def test_url_evidence_removes_credentials_query_and_fragment(self):
  result = detect([], url='https://private-user:private-secret' '@www.google.com/sorry/index?private=query#private'); self.assert_stop(result, 'google_sorry')
  self.assertEqual(result['evidence'], [{'kind': 'url', 'value': 'https://www.google.com/sorry/index'}]); self.assertNotIn('private', json.dumps(result))
 def test_malformed_raw_preserves_independent_url_and_html_challenge_evidence(self):
  for raw in ([None], [{'name': {'value': 'missing id'}}], [node('root', 'RootWebArea', children=['missing'])]):
   with self.subTest(raw=raw):
    self.assert_stop(detect(raw, url='https://www.google.com/sorry/index'), 'google_sorry')
    self.assert_stop(detect(raw, headers={'cf-mitigated': 'challenge', 'content-type': 'text/html'}), 'cloudflare_challenge')
    self.assert_clear(detect(raw))
class StopIntegrationReferenceContracts(unittest.TestCase):
 def setup_guard(self, raw, **metadata):
  cdp, driver = (Mock(), Mock())
  def send(method, params=None, session_id=None):
   if method == 'Accessibility.getFullAXTree':
    return {'nodes': copy.deepcopy(raw)}
   if method in {'Accessibility.enable', 'Accessibility.disable'}:
    return {}
   raise AssertionError('forbidden CDP call: ' + method)
  cdp.send.side_effect = send; cdp.evaluate.side_effect = AssertionError('page JavaScript is forbidden'); cdp.navigate.side_effect = AssertionError('navigation/evasion is forbidden')
  def downstream_observe():
   from wender.perceive import browser
   return (browser.page_state(cdp, 'fixture'), None)
  driver.observe.side_effect = downstream_observe; return (GuardedDriverReference(cdp, driver, **metadata), cdp, driver)
 def test_stop_latches_before_existing_observe_and_prevents_all_followup_work(self):
  cases = [([], {'url': 'https://www.google.com/sorry/index'}), (wall_tree(), {'frame_urls': ['https://www.google.com/recaptcha/api2/anchor']}), (wall_tree('Iframe', 'Cloudflare security challenge'), {'frame_urls': ['https://challenges.cloudflare.com/cdn-cgi/challenge-platform/widget']}), ([], {'headers': {'cf-mitigated': 'challenge', 'content-type': 'text/html'}}), (wall_tree('StaticText', 'Our systems have detected unusual traffic from your computer network.'), {})]
  for raw, metadata in cases:
   with self.subTest(metadata=metadata):
    guard, cdp, driver = self.setup_guard(raw, **metadata)
    with patch('wender.perceive.browser.page_state') as page_state, patch('builtins.open', side_effect=AssertionError('no file writes or reads')):
     with self.assertRaises(StopRequested) as caught:
      guard.observe()
     self.assertTrue(caught.exception.result['is_stop'])
     self.assertTrue(caught.exception.result['evidence'])
     for attempt in (guard.observe, guard.observe, guard.reset, lambda: guard.candidate_actions({}), lambda: guard.act({'op': 'click', 'target': 'cue'})):
      with self.assertRaises(StopRequested):
       attempt()
     page_state.assert_not_called()
    self.assertEqual(cdp.send.call_args_list, [call('Accessibility.enable', session_id='fixture'), call('Accessibility.getFullAXTree', session_id='fixture'), call('Accessibility.disable', session_id='fixture')])
    cdp.evaluate.assert_not_called()
    cdp.navigate.assert_not_called()
    self.assertEqual(driver.mock_calls, [])
 def test_negative_control_allows_exactly_one_downstream_observation(self):
  guard, cdp, driver = self.setup_guard(wall_tree('button', 'Verify'), url='https://example.test/login'); expected = ({'nodes': []}, None)
  with patch('wender.perceive.browser.page_state', return_value=expected[0]) as page_state:
   self.assertEqual(guard.observe(), expected)
   page_state.assert_called_once_with(cdp, 'fixture')
  driver.observe.assert_called_once_with(); self.assertIsNone(guard.stopped); cdp.evaluate.assert_not_called()
 def test_raw_acquisition_failure_is_not_retried(self):
  guard, cdp, driver = self.setup_guard([])
  def fail(method, params=None, session_id=None):
   if method == 'Accessibility.getFullAXTree':
    raise OSError('fixture CDP failure')
   return {}
  cdp.send.side_effect = fail
  with self.assertRaisesRegex(OSError, 'fixture CDP failure'):
   guard.observe()
  self.assertEqual(sum((c.args[0] == 'Accessibility.getFullAXTree' for c in cdp.send.call_args_list)), 1); driver.observe.assert_not_called(); cdp.evaluate.assert_not_called()
 def test_incomplete_ax_without_wall_evidence_abstains_before_downstream_work(self):
  guard, cdp, driver = self.setup_guard([node('root', 'RootWebArea', children=['missing'])])
  with self.assertRaises(IncompleteTree):
   guard.observe()
  driver.observe.assert_not_called(); cdp.evaluate.assert_not_called(); self.assertEqual(sum((c.args[0] == 'Accessibility.getFullAXTree' for c in cdp.send.call_args_list)), 1)
class MissingProductionPrimitiveContracts(unittest.TestCase):
 def test_missing_primitive_imports_do_not_hide_transitive_import_failures(self):
  for module_name, primitive in (('wender.perceive.wait', 'wait_done'), ('wender.perceive.text', 'extract'), ('wender.perceive.stopstate', 'detect')):
   with self.subTest(module=module_name):
    try:
     module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
     self.assertEqual(error.name, module_name)
    else:
     self.assertTrue(callable(getattr(module, primitive, None)))
 def test_missing_production_wait_done_confirms_latest_own_copy(self):
  from wender.perceive.wait import wait_done as production_wait_done; timer = FakeTime(); result = production_wait_done(lambda: chat_snapshot(), CHATGPT_RULES, clock=timer.clock, sleep=timer.sleep, max_polls=2)
  self.assertEqual(result, {'done': True, 'polls': 2, 'completion_status': 'confirmed_by_adapter'})
 def test_missing_production_extract_keeps_latest_answer_once(self):
  from wender.perceive.text import extract as production_extract; result = production_extract(answer_tree(), ASSISTANT, COPY, excluded_roles_names=[{'role': 'button', 'name': 'Share'}], trailers=TRAILERS)
  self.assertEqual(result, {'text': 'Results\nHello world\nYes\nYes\nA B\nif ready:\n    emit()', 'extraction_status': 'clean'})
 def test_missing_production_detect_stops_on_google_sorry(self):
  from wender.perceive.stopstate import detect as production_detect; self.assertEqual(production_detect([], url='https://www.google.com/sorry/index', frame_urls=(), headers={}), {'is_stop': True, 'reason': 'google_sorry', 'confidence': 100, 'evidence': [{'kind': 'url', 'value': 'https://www.google.com/sorry/index'}]})
if __name__ == '__main__':
 unittest.main()
