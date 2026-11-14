# @module {"id":"tests.test_jq1_a9_google_cookie_modal_modo_ia","layer":3,"owner":"TESTS","deps":["wa.learn.chrome_driver","wa.learn.replay","wa.perceive.ax","wa.state.schema"],"book":"HB-WA-s4-learn"}
'Integration tests for jq1-a9 Google cookie consent modal dismissal and Modo IA replay admission.'
import html.parser
import json
import os
import unittest
from wender.learn import chrome_driver, replay
from wender.perceive import ax
from wender.remember import skills
from wender.state import schema
class FixtureHTMLParser(html.parser.HTMLParser):
 def __init__(self):
  super().__init__(); self.elements = []
 def handle_starttag(self, tag, attrs):
  d = dict(attrs); role = d.get('role') or ({'button': 'button', 'a': 'link', 'iframe': 'iframe'}.get(tag) if tag != 'input' else 'textbox' if d.get('type') == 'text' else None)
  name = d.get('aria-label') or d.get('placeholder')
  if role or name:
   self.elements.append({'tag': tag, 'role': role, 'name': name, 'id': d.get('id')})
def _build_raw_node(nid, role, name='', kids=None, ignored=False, props=None, backend=None):
 n = {'nodeId': str(nid), 'role': {'type': 'role', 'value': role}, 'name': {'type': 'computedString', 'value': name}, 'childIds': [str(k) for k in kids or []], 'ignored': ignored}
 if props:
  n['properties'] = [{'name': k, 'value': {'type': 'value', 'value': v}} for k, v in props.items()]
 if backend is not None:
  n['backendDOMNodeId'] = backend
 return n
def _build_states_for_viewport(is_wide=True):
 w = 1600 if is_wide else 780; vp = {'w': w, 'h': 1000, 'scroll_x': 0, 'scroll_y': 0, 'doc_w': w, 'doc_h': 1000, 'dpr_ppm': 1000000}; mk = lambda p, r, b: schema.build_state(p, kind='cdp', url='http://localhost/google', title='Búsqueda de Google - Replica', ax_sha256=ax.snapshot_sha(p, True), ax_stable_sha256=ax.snapshot_sha(p, False), prune={'name': ax.PRUNE_NAME, 'version': ax.PRUNE_VERSION}, raw_count=len(r), boxes=b, viewport=vp, actionable_roles=ax.ACTIONABLE_ROLES)
 raw_s0 = [_build_raw_node(1, 'RootWebArea', 'Búsqueda de Google - Replica', [2, 10]), _build_raw_node(2, 'dialog', 'Antes de ir a la Búsqueda de Google', [3, 4], backend=100), _build_raw_node(3, 'heading', 'Antes de ir a la Búsqueda de Google'), _build_raw_node(4, 'button', 'Rechazar todo', backend=101), _build_raw_node(10, 'search', 'Buscar', [11, 12] if is_wide else [11]), _build_raw_node(11, 'button', 'Búsqueda por imágenes', backend=200)] + ([_build_raw_node(12, 'link', 'Modo IA', backend=201)] if is_wide else [])
 p0 = ax.prune(raw_s0); b0 = {p0[0]['id']: [0, 0, w, 1000], p0[1]['id']: [400, 100, 500, 400], p0[2]['id']: [420, 120, 460, 40], p0[3]['id']: [420, 200, 120, 40], p0[4]['id']: [0, 0, w, 200], p0[5]['id']: [1120, 280, 40, 40]}
 if is_wide:
  b0[p0[6]['id']] = [1170, 280, 88, 40]
 raw_s1 = [_build_raw_node(1, 'RootWebArea', 'Búsqueda de Google - Replica', [10]), _build_raw_node(10, 'search', 'Buscar', [11, 12] if is_wide else [11]), _build_raw_node(11, 'button', 'Búsqueda por imágenes', backend=200)] + ([_build_raw_node(12, 'link', 'Modo IA', backend=201)] if is_wide else [])
 p1 = ax.prune(raw_s1); b1 = {p1[0]['id']: [0, 0, w, 1000], p1[1]['id']: [0, 0, w, 200], p1[2]['id']: [1120, 280, 40, 40]}
 if is_wide:
  b1[p1[3]['id']] = [1170, 280, 88, 40]
 raw_s2 = [_build_raw_node(1, 'RootWebArea', 'Búsqueda de Google - Replica', [10, 20]), _build_raw_node(10, 'search', 'Buscar', [11, 12] if is_wide else [11]), _build_raw_node(11, 'button', 'Búsqueda por imágenes', backend=200), _build_raw_node(20, 'region', 'Modo IA Container', [21, 22]), _build_raw_node(21, 'heading', '¿Qué tienes en mente?', backend=300), _build_raw_node(22, 'combobox', '¿Qué tienes en mente?', backend=301)]
 if is_wide:
  raw_s2[1]['childIds'].append('12')
  raw_s2.insert(3, _build_raw_node(12, 'link', 'Modo IA', backend=201))
 p2 = ax.prune(raw_s2); b2 = {p2[0]['id']: [0, 0, w, 1000], p2[1]['id']: [0, 0, w, 200], p2[2]['id']: [1120, 280, 40, 40]}; idx = 3
 if is_wide:
  b2[p2[idx]['id']] = [1170, 280, 88, 40]
  idx += 1
 b2[p2[idx]['id']], b2[p2[idx + 1]['id']], b2[p2[idx + 2]['id']] = ([200, 300, 800, 400], [220, 320, 400, 40], [220, 380, 600, 50]); return (mk(p0, raw_s0, b0), mk(p1, raw_s1, b1), mk(p2, raw_s2, b2), p0, p1, p2, raw_s0, raw_s1, raw_s2)
class FakeCdp():
 def __init__(self, raw_trees, url, title, viewport):
  self.raw_trees, self.url, self.title, self.viewport, self.step, self.calls = (list(raw_trees), url, title, viewport, 0, [])
 def evaluate(self, session_id, expression):
  self.calls.append(('evaluate', expression))
  if 'location.href' in expression:
   return json.dumps([self.url, self.title])
  if 'wenderViewport' in expression:
   return json.dumps(self.viewport)
  return '{}'
 def send(self, method, params=None, session_id=None):
  self.calls.append((method, params))
  if method == 'Accessibility.getFullAXTree':
   return {'nodes': self.raw_trees[min(self.step, len(self.raw_trees) - 1)]}
  if method == 'DOM.getDocument':
   return {'root': {'nodeId': 1}}
  if method == 'DOM.getBoxModel':
   bid = (params or {}).get('backendNodeId')
   borders = {100: [400, 100, 900, 100, 900, 500, 400, 500], 101: [420, 200, 540, 200, 540, 240, 420, 240], 200: [1120, 280, 1160, 280, 1160, 320, 1120, 320], 201: [1170, 280, 1258, 280, 1258, 320, 1170, 320], 300: [220, 320, 620, 320, 620, 360, 220, 360], 301: [220, 380, 820, 380, 820, 430, 220, 430]}
   return {'model': {'border': borders.get(bid, [0, 0, 10, 0, 10, 10, 0, 10])}}
  if method == 'DOM.resolveNode':
   return {'object': {'objectId': 'obj-1'}}
  if method == 'Runtime.callFunctionOn':
   return {'result': {'type': 'undefined'}}
  return {}
class ScriptedDriver(chrome_driver.ChromeDriver):
 def __init__(self, cdp, session_id, start_url, states):
  super().__init__(cdp, session_id, start_url); self.states, self.step_idx = (states, 0)
 def reset(self):
  self.step_idx = 0
  if self.cdp:
   self.cdp.step = 0
 def observe(self):
  return (self.states[min(self.step_idx, len(self.states) - 1)], '0123456789abcdef')
 def act(self, action):
  self.step_idx += 1
  if self.cdp:
   self.cdp.step = self.step_idx
class TestJq1A9GoogleCookieModalModoIA(unittest.TestCase):
 def setUp(self):
  self.fixture_path = os.path.join(os.path.dirname(__file__), 'fixtures', 'sites', 'jq1_a9_google_cookie_modal_modo_ia.html')
 def test_fixture_html_contract(self):
  p = FixtureHTMLParser()
  with open(self.fixture_path, 'r', encoding='utf-8') as f:
   p.feed(f.read())
  roles = [e['role'] for e in p.elements if e['role']]; names = [e['name'] for e in p.elements if e['name']]
  for r in ('dialog', 'button', 'link', 'combobox'):
   self.assertIn(r, roles)
  for n in ('Antes de ir a la Búsqueda de Google', '¿Qué tienes en mente?'):
   self.assertIn(n, names)
 def test_replay_and_admit_1600x1000_viewport(self):
  st0, st1, st2, _, _, _, raw0, raw1, raw2 = _build_states_for_viewport(is_wide=True); reject_id = [n['id'] for n in st0['nodes'] if n['role'] == 'button' and n['name'] == 'Rechazar todo'][0]
  modo_id = [n['id'] for n in st1['nodes'] if n['role'] == 'link' and n['name'] == 'Modo IA'][0]; draft = {'id': 'jq1-a9-google-cookie-modal-modo-ia', 'intent': 'Dismiss cookie modal and click Modo IA', 'params': {}, 'steps': [{'path': 'ax', 'op': 'click', 'target': reject_id}, {'path': 'ax', 'op': 'click', 'target': modo_id}], 'checkpoints': [{'after_step': 0, 'expect': {'node_absent': {'role': 'dialog', 'name': 'Antes de ir a la Búsqueda de Google'}}}, {'after_step': 1, 'expect': {'node_present': {'role': 'combobox', 'name': '¿Qué tienes en mente?'}}}], 'provenance': {'source': 'test:jq1-a9'}}
  driver = ScriptedDriver(FakeCdp([raw0, raw1, raw2], 'http://localhost/google', 'Búsqueda de Google - Replica', st0['viewport']), 'sid-1', 'http://localhost/google', [st0, st1, st2])
  res_data = json.loads(replay.replay_and_admit(draft, driver, verified_by='jq1-a9', runs=2)); self.assertTrue(res_data['evidence']['reproduced'])
  self.assertEqual((res_data['evidence']['verified_by'], res_data['evidence']['runs'], len(res_data['evidence']['sha256'])), ('jq1-a9', 2, 64))
 def test_replay_rejected_at_780x1000_narrow_viewport(self):
  st0, st1, _, _, _, _, raw0, raw1, _ = _build_states_for_viewport(is_wide=False); reject_id = [n['id'] for n in st0['nodes'] if n['role'] == 'button' and n['name'] == 'Rechazar todo'][0]
  draft = {'id': 'jq1-a9-google-cookie-modal-modo-ia-narrow', 'intent': 'Dismiss cookie modal and click Modo IA', 'params': {}, 'steps': [{'path': 'ax', 'op': 'click', 'target': reject_id}, {'path': 'ax', 'op': 'click', 'target': '0000000000000099'}], 'checkpoints': [{'after_step': 0, 'expect': {'node_absent': {'role': 'dialog', 'name': 'Antes de ir a la Búsqueda de Google'}}}, {'after_step': 1, 'expect': {'node_present': {'role': 'combobox', 'name': '¿Qué tienes en mente?'}}}], 'provenance': {'source': 'test:jq1-a9'}}
  driver = ScriptedDriver(FakeCdp([raw0, raw1, raw1], 'http://localhost/google', 'Búsqueda de Google - Replica', st0['viewport']), 'sid-narrow', 'http://localhost/google', [st0, st1, st1])
  with self.assertRaises(skills.SkillRejected) as cm:
   replay.replay_and_admit(draft, driver, verified_by='jq1-a9', runs=2)
  self.assertIn('failing checkpoint', str(cm.exception))
 def test_negative_state_checkpoint_failure(self):
  st0, _, _, _, _, _, raw0, _, _ = _build_states_for_viewport(is_wide=True); reject_id = [n['id'] for n in st0['nodes'] if n['role'] == 'button' and n['name'] == 'Rechazar todo'][0]
  draft = {'id': 'jq1-a9-negative-test', 'intent': 'Check persistent modal failure', 'params': {}, 'steps': [{'path': 'ax', 'op': 'click', 'target': reject_id}], 'checkpoints': [{'after_step': 0, 'expect': {'node_absent': {'role': 'dialog', 'name': 'Antes de ir a la Búsqueda de Google'}}}], 'provenance': {'source': 'test:jq1-a9'}}
  driver = ScriptedDriver(FakeCdp([raw0, raw0], 'http://localhost/google', 'Búsqueda de Google - Replica', st0['viewport']), 'sid-neg', 'http://localhost/google', [st0, st0])
  with self.assertRaises(skills.SkillRejected) as cm:
   replay.replay_and_admit(draft, driver, verified_by='jq1-a9', runs=2)
  self.assertIn('failing checkpoint', str(cm.exception))
 def test_absent_production_modules(self):
  from wender.perceive.stopstate import detect; from wender.perceive.text import extract; from wender.perceive.wait import wait_done; st0, _, _, _, _, _, raw0, _, raw2 = _build_states_for_viewport(is_wide=True)
  self.assertEqual(detect(st0), {'is_stop': False, 'reason': None, 'confidence': 0, 'evidence': []}); self.assertEqual(detect(raw0, url='https://www.google.com/sorry/index'), {'is_stop': True, 'reason': 'google_sorry', 'confidence': 100, 'evidence': [{'kind': 'url', 'value': 'https://www.google.com/sorry/index'}]})
  ms = 0
  def sleep(d):
   nonlocal ms
   ms += d
  wait_res = wait_done(lambda: raw2, {'absent_roles_names': [{'role': 'dialog', 'name': 'Antes de ir a la Búsqueda de Google'}], 'present_roles_names': [{'role': 'combobox', 'name': '¿Qué tienes en mente?'}], 'quiet_snapshots': 2}, clock=lambda: ms, sleep=sleep, max_polls=2)
  self.assertEqual(wait_res, {'done': True, 'polls': 2, 'completion_status': 'confirmed_by_adapter'}); self.assertEqual(extract(raw2), {'text': '¿Qué tienes en mente?', 'extraction_status': 'clean'})
if __name__ == '__main__':
 unittest.main()
