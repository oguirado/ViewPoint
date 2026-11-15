'Tests for jq1-a9-gemini-model-menuitemradio.'
import html.parser
import json
import pathlib
import unittest
from wender.act.cdp_executor import run_ax_action
from wender.learn.explore import Driver
from wender.learn.replay import replay_and_admit
from wender.perceive import ax
from wender.remember import skills
from wender.state import schema
class GeminiModelPickerHTMLParser(html.parser.HTMLParser):
 def __init__(self):
  super().__init__(); self.button_found = self.menu_found = self.iframe_found = False; self.button_name = self.menu_name = self.iframe_srcdoc = ''; self.button_expanded = None
  self.menuitems, self._tag_stack = ([], [])
 def handle_starttag(self, tag, attrs):
  d = dict(attrs); self._tag_stack.append({'tag': tag, 'attrs': d, 'text': ''}); role = d.get('role')
  if role == 'button':
   self.button_found, self.button_expanded = (True, d.get('aria-expanded'))
  elif role == 'menu':
   self.menu_found, self.menu_name = (True, d.get('aria-label', ''))
  elif tag == 'iframe':
   self.iframe_found, self.iframe_srcdoc = (True, d.get('srcdoc', ''))
 def handle_data(self, data):
  if self._tag_stack:
   self._tag_stack[-1]['text'] += data
 def handle_endtag(self, tag):
  if not self._tag_stack:
   return
  e = self._tag_stack.pop(); role, text = (e['attrs'].get('role'), e['text'].strip())
  if role == 'button' and text:
   self.button_name = text
  elif role == 'menuitemradio':
   self.menuitems.append({'name': text, 'checked': e['attrs'].get('aria-checked')})
def _mn(nid, role, name, kids=None, prop=None, bid=None):
 d = {'nodeId': str(nid), 'role': {'type': 'role', 'value': role}, 'name': {'type': 'name', 'value': name}}
 if kids is not None:
  d['childIds'] = [str(k) for k in kids]
 if prop is not None:
  d['properties'] = [{'name': k, 'value': {'type': 'boolean', 'value': v}} for k, v in prop.items()]
 if bid is not None:
  d['backendDOMNodeId'] = bid
 return d
def _build_raw_tree_s0():
 return [_mn(1, 'RootWebArea', 'Gemini Replica', ['2', '3']), _mn(2, 'button', 'Selector de modelo', prop={'expanded': False}, bid=101), _mn(3, 'article', 'Challenge Frame', bid=102)]
def _build_raw_tree_s1():
 return [_mn(1, 'RootWebArea', 'Gemini Replica', ['2', '4', '3']), _mn(2, 'button', 'Selector de modelo', prop={'expanded': True}, bid=101), _mn(4, 'menu', 'Modelos', ['5', '6', '7'], bid=103), _mn(5, 'menuitemradio', '2.0 Flash', prop={'checked': True}, bid=104), _mn(6, 'menuitemradio', '2.0 Pro', prop={'checked': False}, bid=105), _mn(7, 'menuitemradio', 'Deep Research', prop={'checked': False}, bid=106), _mn(3, 'article', 'Challenge Frame', bid=102)]
def _build_raw_tree_s2():
 return [_mn(1, 'RootWebArea', 'Gemini Replica', ['2', '4', '3']), _mn(2, 'button', 'Selector de modelo', prop={'expanded': True}, bid=101), _mn(4, 'menu', 'Modelos', ['5', '6', '7'], bid=103), _mn(5, 'menuitemradio', '2.0 Flash', prop={'checked': False}, bid=104), _mn(6, 'menuitemradio', '2.0 Pro', prop={'checked': False}, bid=105), _mn(7, 'menuitemradio', 'Deep Research', prop={'checked': True}, bid=106), _mn(3, 'article', 'Challenge Frame', bid=102)]
class StatefulFakeCDP():
 def __init__(self, raw_trees, initial_index=1):
  self.raw_trees, self.state_index, self.calls = (list(raw_trees), initial_index, [])
 def send(self, method, params=None, session_id=None):
  params = params or {}; self.calls.append({'method': method, 'params': params, 'session_id': session_id})
  if method == 'Accessibility.getFullAXTree':
   return {'nodes': self.raw_trees[self.state_index]}
  if method == 'DOM.resolveNode':
   return {'object': {'objectId': 'obj_%s' % params.get('backendNodeId')}}
  if method == 'Runtime.callFunctionOn':
   oid = params.get('objectId')
   if oid == 'obj_106' and self.state_index == 1:
    self.state_index = 2
   elif oid == 'obj_101' and self.state_index == 0:
    self.state_index = 1
   return {'result': {'value': True}}
  return {}
class StatefulGeminiDriver(Driver):
 def __init__(self, states):
  self.states, self.step_index = (list(states), 0)
 def reset(self):
  self.step_index = 0
 def observe(self):
  return (self.states[min(self.step_index, len(self.states) - 1)], '0123456789abcdef' * 4)
 def candidate_actions(self, state):
  return []
 def act(self, action):
  self.step_index += 1
class TestJq1A9GeminiModelMenuItemRadio(unittest.TestCase):
 def setUp(self):
  self.raw_s0, self.raw_s1, self.raw_s2 = (_build_raw_tree_s0(), _build_raw_tree_s1(), _build_raw_tree_s2()); self.pruned_s0, self.pruned_s1, self.pruned_s2 = (ax.prune(self.raw_s0), ax.prune(self.raw_s1), ax.prune(self.raw_s2))
  mk = lambda p, r: schema.build_state(p, kind='cdp', url='http://localhost/gemini', title='Gemini Replica', ax_sha256=ax.snapshot_sha(p, True), ax_stable_sha256=ax.snapshot_sha(p, False), prune={'name': 'prune_ax_v1', 'version': 1}, raw_count=len(r))
  self.st0, self.st1, self.st2 = (mk(self.pruned_s0, self.raw_s0), mk(self.pruned_s1, self.raw_s1), mk(self.pruned_s2, self.raw_s2))
 def test_fixture_html_contract(self):
  fp = pathlib.Path(__file__).parent / 'fixtures' / 'sites' / 'jq1_a9_gemini_model_menuitemradio.html'; self.assertTrue(fp.exists()); p = GeminiModelPickerHTMLParser()
  p.feed(fp.read_text(encoding='utf-8')); self.assertTrue(p.button_found and p.menu_found and p.iframe_found); self.assertEqual((p.button_name, p.button_expanded, p.menu_name), ('Selector de modelo', 'false', 'Modelos'))
  self.assertEqual(p.menuitems, [{'name': '2.0 Flash', 'checked': 'true'}, {'name': '2.0 Pro', 'checked': 'false'}, {'name': 'Deep Research', 'checked': 'false'}])
  self.assertIn('Inert Challenge Frame', p.iframe_srcdoc)
 def test_run_ax_action_deep_research_selection(self):
  cdp = StatefulFakeCDP([self.raw_s0, self.raw_s1, self.raw_s2], initial_index=1); target_id = next((n['id'] for n in self.pruned_s1 if n['name'] == 'Deep Research'))
  read_nodes = lambda: ax.prune(cdp.send('Accessibility.getFullAXTree')['nodes']); receipt = run_ax_action(cdp, 'sess_gemini', self.pruned_s1, {'op': 'click', 'target': target_id, 'expect': {'target_states': {'checked': 'true'}}}, read_nodes, sleep=lambda s: None)
  self.assertEqual((receipt['ok'], receipt['attempts'], receipt['verified'], receipt['path'], receipt['error']), (1, 1, 1, 'ax', None)); post = read_nodes()
  for name, val in (('Deep Research', 'true'), ('2.0 Flash', 'false'), ('2.0 Pro', 'false')):
   self.assertEqual(dict(next((n for n in post if n['name'] == name))['attrs']).get('checked'), val)
 def test_replay_and_admit_gemini_model_selection(self):
  btn_id = next((n['id'] for n in self.pruned_s0 if n['role'] == 'button' and n['name'] == 'Selector de modelo')); deep_id = next((n['id'] for n in self.pruned_s1 if n['role'] == 'menuitemradio' and n['name'] == 'Deep Research'))
  draft = {'id': 'gemini-model-selector-deep-research', 'intent': 'Open Gemini model menu and select Deep Research option', 'params': {}, 'steps': [{'path': 'ax', 'op': 'click', 'target': btn_id}, {'path': 'ax', 'op': 'click', 'target': deep_id}], 'checkpoints': [{'after_step': 0, 'expect': {'node_present': [{'role': 'menu', 'name': 'Modelos'}, {'role': 'menuitemradio', 'name': 'Deep Research'}]}}, {'after_step': 1, 'expect': {'node_present': [{'role': 'menuitemradio', 'name': 'Deep Research'}]}}], 'provenance': {'source': 'test:jq1_a9_gemini_model_menuitemradio'}}
  res_json = replay_and_admit(draft, StatefulGeminiDriver([self.st0, self.st1, self.st2]), verified_by='jq1-builder', runs=2); parsed = json.loads(res_json)
  self.assertTrue(parsed['evidence']['reproduced']); self.assertEqual(len(parsed['evidence']['sha256']), 64)
 def test_negative_state_verification_failures(self):
  static_cdp = StatefulFakeCDP([self.raw_s0, self.raw_s1, self.raw_s1], initial_index=1); deep_id = next((n['id'] for n in self.pruned_s1 if n['name'] == 'Deep Research'))
  receipt = run_ax_action(static_cdp, 'sess_static', self.pruned_s1, {'op': 'click', 'target': deep_id, 'expect': {'target_states': {'checked': 'true'}}}, read_nodes=lambda: ax.prune(static_cdp.send('Accessibility.getFullAXTree')['nodes']), sleep=lambda s: None)
  self.assertEqual((receipt['ok'], receipt['verified'], receipt['error']), (0, 0, 'expectation not satisfied')); btn_id = next((n['id'] for n in self.pruned_s0 if n['role'] == 'button' and n['name'] == 'Selector de modelo'))
  bad = {'id': 'gemini-invalid-checkpoint', 'intent': 'Invalid expectation test', 'params': {}, 'steps': [{'path': 'ax', 'op': 'click', 'target': btn_id}, {'path': 'ax', 'op': 'click', 'target': deep_id}], 'checkpoints': [{'after_step': 1, 'expect': {'node_present': [{'role': 'menuitemradio', 'name': 'NonExistentModelOption'}]}}], 'provenance': {'source': 'test:jq1_a9_gemini_model_menuitemradio'}}
  with self.assertRaises(skills.SkillRejected):
   replay_and_admit(bad, StatefulGeminiDriver([self.st0, self.st1, self.st2]), verified_by='jq1-builder', runs=2)
 def test_absent_module_wait_done_wp1_02(self):
  from wender.perceive.wait import wait_done; ms = 0
  def sleep(d):
   nonlocal ms
   ms += d
  rules = {'absent_roles_names': [{'role': 'progressbar', 'name': 'Investigando'}], 'present_roles_names': [{'role': 'menuitemradio', 'name': 'Deep Research'}], 'quiet_snapshots': 2}
  self.assertEqual(wait_done(lambda: self.raw_s2, rules, clock=lambda: ms, sleep=sleep, max_polls=2), {'done': True, 'polls': 2, 'completion_status': 'confirmed_by_adapter'})
 def test_absent_module_text_extract_wp1_04(self):
  from wender.perceive.text import extract; raw = [_mn(1, 'RootWebArea', 'Gemini Replica', ['2']), _mn(2, 'article', 'Informe', ['3']), _mn(3, 'StaticText', 'Deep Research ready', [])]
  self.assertEqual(extract(raw, target_role='article', target_name='Informe'), {'text': 'Deep Research ready', 'extraction_status': 'clean'})
 def test_absent_module_stopstate_detect_wp1_05(self):
  from wender.perceive.stopstate import detect; self.assertEqual(detect(self.st2), {'is_stop': False, 'reason': None, 'confidence': 0, 'evidence': []})
  self.assertEqual(detect(self.st2, url='https://www.google.com/sorry/index'), {'is_stop': True, 'reason': 'google_sorry', 'confidence': 100, 'evidence': [{'kind': 'url', 'value': 'https://www.google.com/sorry/index'}]})
if __name__ == '__main__':
 unittest.main()
