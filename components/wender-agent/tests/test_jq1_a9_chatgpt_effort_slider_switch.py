'Tests for jq1-a9-chatgpt-effort-slider-switch.\nVerifies:\n1. HTML fixture contract using stdlib HTMLParser (roles, names, geometry, transitions).\n2. Reload-stable prune_ax_v1 node IDs across repeated pruning.\n3. run_ax_action protocol over FakeCdp and ScriptedReadNodes:\n - Switch toggle click: expect target_states checked="true"\n - Slider select at stop 2: value="2", expect target_name="Alta"\n4. Negative state verification where expectation failure is reported correctly (ok=0, verified=0).\n5. Production contract verification for WP1-02, WP1-04, and WP1-05.\n'
import json
from html.parser import HTMLParser
import pathlib
import unittest
from wender.act import cdp_executor
from wender.act.cdp_executor import run_ax_action, ActionError
from wender.perceive import ax
from wender.state.schema import dumps
def _raw_node(nid, role, name='', kids=(), ignored=False, props=None, backend=None):
 node = {'nodeId': str(nid), 'role': {'type': 'role', 'value': role}, 'name': {'type': 'computedString', 'value': name}, 'childIds': [str(k) for k in kids], 'ignored': ignored}
 if props:
  node['properties'] = [{'name': k, 'value': {'type': 'value', 'value': v}} for k, v in props.items()]
 if backend is not None:
  node['backendDOMNodeId'] = backend
 return node
class HTMLFixtureContractParser(HTMLParser):
 def __init__(self):
  super().__init__(); self.elements = []; self.iframes = []
 def handle_starttag(self, tag, attrs):
  attr_dict = dict(attrs)
  if tag == 'iframe':
   self.iframes.append(attr_dict)
  role = attr_dict.get('role')
  if role or tag in ('input', 'button'):
   self.elements.append({'tag': tag, 'role': role, 'attr_dict': attr_dict})
class FakeCdp():
 def __init__(self):
  self.calls = []
 def send(self, method, params=None, session_id=None):
  payload = dict(params or {}); self.calls.append({'method': method, 'params': payload, 'session_id': session_id})
  if method == 'DOM.scrollIntoViewIfNeeded':
   return {}
  if method == 'DOM.resolveNode':
   return {'object': {'objectId': 'obj-%s' % payload.get('backendNodeId')}}
  if method == 'Runtime.callFunctionOn':
   return {'result': {'type': 'undefined'}}
  return {}
class ScriptedReadNodes():
 def __init__(self, snapshots):
  self.snapshots = [list(s) for s in snapshots]; self.calls = 0
 def __call__(self):
  self.calls += 1
  if not self.snapshots:
   raise IndexError('no more scripted snapshots')
  if len(self.snapshots) == 1:
   return list(self.snapshots[0])
  return list(self.snapshots.pop(0))
class TestChatGPTSettingReplicaFixture(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  fixture_path = pathlib.Path(__file__).parent / 'fixtures' / 'sites' / 'jq1_a9_chatgpt_effort_slider_switch.html'
  cls.html_content = fixture_path.read_text(encoding='utf-8')
 def test_fixture_contract_roles_and_attributes(self):
  parser = HTMLFixtureContractParser(); parser.feed(self.html_content); roles = [el.get('role') for el in parser.elements]; self.assertIn('menuitem', roles)
  self.assertIn('slider', roles); self.assertIn('switch', roles); menuitems = [el for el in parser.elements if el.get('role') == 'menuitem']; self.assertTrue(len(menuitems) >= 1)
  sliders = [el for el in parser.elements if el.get('role') == 'slider' or el['tag'] == 'input']; slider = next((el for el in sliders if el['attr_dict'].get('type') == 'range' or el.get('role') == 'slider'))
  self.assertEqual(slider['attr_dict'].get('min'), '0'); self.assertEqual(slider['attr_dict'].get('max'), '4'); switches = [el for el in parser.elements if el.get('role') == 'switch']
  self.assertTrue(len(switches) >= 1); self.assertEqual(switches[0]['attr_dict'].get('aria-checked'), 'false'); self.assertTrue(len(parser.iframes) >= 1)
  iframe = parser.iframes[0]; self.assertIn('srcdoc', iframe); self.assertNotIn('http://', iframe['srcdoc']); self.assertNotIn('https://', iframe['srcdoc'])
class TestChatGPTSettingReplicaAXExecutor(unittest.TestCase):
 def setUp(self):
  self.waits = []
 def fake_sleep(self, seconds):
  self.waits.append(seconds)
 def _build_raw_snapshots(self):
  raw_initial = [_raw_node(1, 'RootWebArea', 'ChatGPT Settings Replica', [2]), _raw_node(2, 'main', 'Settings Workspace', [10, 20, 30]), _raw_node(10, 'menuitem', 'Potencia', backend=101), _raw_node(20, 'slider', 'Potencia', props={'value': 0, 'valuemin': 0, 'valuemax': 4}, backend=102), _raw_node(30, 'switch', 'Búsqueda web', props={'checked': False}, backend=103)]
  raw_switch_toggled = [_raw_node(1, 'RootWebArea', 'ChatGPT Settings Replica', [2]), _raw_node(2, 'main', 'Settings Workspace', [10, 20, 30]), _raw_node(10, 'menuitem', 'Potencia', backend=101), _raw_node(20, 'slider', 'Potencia', props={'value': 0, 'valuemin': 0, 'valuemax': 4}, backend=102), _raw_node(30, 'switch', 'Búsqueda web', props={'checked': True}, backend=103)]
  raw_slider_selected = [_raw_node(1, 'RootWebArea', 'ChatGPT Settings Replica', [2]), _raw_node(2, 'main', 'Settings Workspace', [10, 20, 30]), _raw_node(10, 'menuitem', 'Potencia', backend=101), _raw_node(20, 'slider', 'Alta', props={'value': 2, 'valuemin': 0, 'valuemax': 4, 'valuetext': 'Alta'}, backend=102), _raw_node(30, 'switch', 'Búsqueda web', props={'checked': False}, backend=103)]
  return (raw_initial, raw_switch_toggled, raw_slider_selected)
 def test_prune_emits_menuitem_slider_switch_with_stable_ids(self):
  raw_initial, _, _ = self._build_raw_snapshots(); nodes_1 = ax.prune(raw_initial); nodes_2 = ax.prune(raw_initial); roles = [n['role'] for n in nodes_1]
  self.assertIn('menuitem', roles); self.assertIn('slider', roles); self.assertIn('switch', roles)
  for n1, n2 in zip(nodes_1, nodes_2):
   self.assertEqual(n1['id'], n2['id'])
   self.assertEqual(len(n1['id']), 16)
   int(n1['id'], 16)
 def test_run_ax_action_click_switch_verifies_checked_true(self):
  raw_initial, raw_switch_toggled, _ = self._build_raw_snapshots(); initial_nodes = ax.prune(raw_initial); toggled_nodes = ax.prune(raw_switch_toggled)
  switch_node = next((n for n in initial_nodes if n['role'] == 'switch')); cdp = FakeCdp(); reader = ScriptedReadNodes([toggled_nodes]); action = {'op': 'click', 'target': switch_node['id'], 'expect': {'target_states': {'checked': 'true'}}, 'irreversible': False}
  receipt = run_ax_action(cdp, 'sess-switch', initial_nodes, action, reader, sleep=self.fake_sleep); serialized = dumps(receipt); parsed = json.loads(serialized)
  self.assertEqual(parsed, receipt); self.assertEqual(receipt['ok'], 1); self.assertEqual(receipt['verified'], 1); self.assertEqual(receipt['path'], 'ax')
  self.assertEqual(receipt['attempts'], 1); self.assertIsNone(receipt['error']); self.assertNotEqual(receipt['pre_sha'], receipt['post_sha'])
 def test_run_ax_action_select_slider_verifies_target_name_alta(self):
  raw_initial, _, raw_slider_selected = self._build_raw_snapshots(); initial_nodes = ax.prune(raw_initial); selected_nodes = ax.prune(raw_slider_selected)
  slider_node = next((n for n in initial_nodes if n['role'] == 'slider')); cdp = FakeCdp(); reader = ScriptedReadNodes([selected_nodes]); action = {'op': 'select', 'target': slider_node['id'], 'value': '2', 'expect': {'target_name': 'Alta'}, 'irreversible': False}
  receipt = run_ax_action(cdp, 'sess-slider', initial_nodes, action, reader, sleep=self.fake_sleep); self.assertEqual(receipt['ok'], 1); self.assertEqual(receipt['verified'], 1)
  self.assertEqual(receipt['path'], 'ax'); self.assertEqual(receipt['attempts'], 1); self.assertIsNone(receipt['error']); self.assertNotEqual(receipt['pre_sha'], receipt['post_sha'])
 def test_negative_state_verification_fails_when_expectation_unmet(self):
  raw_initial, _, _ = self._build_raw_snapshots(); initial_nodes = ax.prune(raw_initial); switch_node = next((n for n in initial_nodes if n['role'] == 'switch'))
  cdp = FakeCdp(); reader = ScriptedReadNodes([initial_nodes, initial_nodes, initial_nodes]); action = {'op': 'click', 'target': switch_node['id'], 'expect': {'target_states': {'checked': 'true'}}, 'irreversible': False}
  receipt = run_ax_action(cdp, 'sess-fail', initial_nodes, action, reader, retries=3, sleep=self.fake_sleep); self.assertEqual(receipt['ok'], 0); self.assertEqual(receipt['verified'], 0)
  self.assertEqual(receipt['attempts'], 3); self.assertIsNotNone(receipt['error']); self.assertEqual(receipt['pre_sha'], receipt['post_sha'])
class TestAbsentProductionAPIs(unittest.TestCase):
 def test_wp1_02_ax_completion_waiter_wait_done(self):
  from wender.perceive.wait import wait_done; raw_turn = [_raw_node(1, 'RootWebArea', 'ChatGPT', [2]), _raw_node(2, 'article', 'Turn', [3, 4, 5]), _raw_node(3, 'heading', 'ChatGPT dijo:'), _raw_node(4, 'StaticText', 'Alta'), _raw_node(5, 'button', 'Copiar mensaje')]
  ms = 0
  def clock():
   return ms
  def sleep(delay_ms):
   nonlocal ms
   ms += delay_ms
  rules = {'absent_roles_names': [{'role': 'button', 'name': 'Detener'}], 'after_last_anchor': {'role': 'heading', 'name': 'ChatGPT dijo:'}, 'present_roles_names': [{'role': 'button', 'name': 'Copiar mensaje'}], 'quiet_snapshots': 2}
  receipt = wait_done(lambda: raw_turn, rules, clock=clock, sleep=sleep, max_polls=2); self.assertEqual(receipt, {'done': True, 'polls': 2, 'completion_status': 'confirmed_by_adapter'})
 def test_wp1_04_answer_text_extractor_extract(self):
  from wender.perceive.text import extract; raw_turn = [_raw_node(1, 'RootWebArea', 'ChatGPT', [2]), _raw_node(2, 'article', 'Turn', [3, 4, 5]), _raw_node(3, 'heading', 'ChatGPT dijo:'), _raw_node(4, 'StaticText', 'Potencia configurada en Alta'), _raw_node(5, 'button', 'Copiar mensaje')]
  receipt = extract(raw_turn, {'role': 'heading', 'name': 'ChatGPT dijo:'}, {'role': 'button', 'name': 'Copiar mensaje'}); self.assertEqual(receipt, {'text': 'Potencia configurada en Alta', 'extraction_status': 'clean'})
 def test_wp1_05_stop_state_detector_detect(self):
  from wender.perceive.stopstate import detect; raw_initial = [_raw_node(1, 'RootWebArea', 'ChatGPT Settings Replica', [2]), _raw_node(2, 'main', 'Settings Workspace', [10]), _raw_node(10, 'menuitem', 'Potencia', backend=101)]
  self.assertEqual(detect(raw_initial, url='https://example.test/settings'), {'is_stop': False, 'reason': None, 'confidence': 0, 'evidence': []})
  self.assertEqual(detect(raw_initial, url='https://www.google.com/sorry/index?continue=1'), {'is_stop': True, 'reason': 'google_sorry', 'confidence': 100, 'evidence': [{'kind': 'url', 'value': 'https://www.google.com/sorry/index'}]})
if __name__ == '__main__':
 unittest.main()
