'Test Perplexity submenu diagonal corridor trap verification failure.'
from html.parser import HTMLParser
import pathlib
import unittest
from wender.act.cdp_executor import ActionError, run_ax_action
from wender.perceive import ax
def _raw_node(nid, role, name='', kids=(), ignored=False, props=None, backend=None):
 node = {'nodeId': str(nid), 'role': {'type': 'role', 'value': role}, 'name': {'type': 'computedString', 'value': name}, 'childIds': [str(k) for k in kids], 'ignored': ignored}
 if props:
  node['properties'] = [{'name': k, 'value': {'type': 'value', 'value': v}} for k, v in props.items()]
 if backend is not None:
  node['backendDOMNodeId'] = backend
 return node
def _build_perplexity_raw_trees():
 initial_raw = [_raw_node(1, 'RootWebArea', 'App', [2]), _raw_node(2, 'main', 'Workspace', [1000]), _raw_node(1000, 'menu', 'Main Menu', [10, 20]), _raw_node(10, 'menuitem', 'Focus', kids=[30], props={'expanded': True, 'haspopup': 'true'}, backend=101), _raw_node(20, 'menuitem', 'Attach', props={}, backend=102), _raw_node(30, 'menu', 'Focus Options', kids=[40], backend=103), _raw_node(40, 'menuitem', 'Writing', props={'checked': False}, backend=104)]
 collapsed_raw = [_raw_node(1, 'RootWebArea', 'App', [2]), _raw_node(2, 'main', 'Workspace', [1000]), _raw_node(1000, 'menu', 'Main Menu', [10, 20]), _raw_node(10, 'menuitem', 'Focus', props={'expanded': False, 'haspopup': 'true'}, backend=101), _raw_node(20, 'menuitem', 'Attach', props={}, backend=102)]
 return (initial_raw, collapsed_raw)
class PerplexityHTMLFixtureParser(HTMLParser):
 def __init__(self):
  super().__init__(); self.menuitems = []; self.menus = []; self.iframes = []
 def handle_starttag(self, tag, attrs):
  attr_dict = dict(attrs); role = attr_dict.get('role')
  if role == 'menuitem':
   self.menuitems.append(attr_dict)
  elif role == 'menu':
   self.menus.append(attr_dict)
  elif tag == 'iframe':
   self.iframes.append(attr_dict)
class DiagonalCorridorFakeCdp():
 def __init__(self, on_waypoint=None):
  self.calls = []; self.on_waypoint = on_waypoint
 def send(self, method, params=None, session_id=None):
  payload = dict(params or {}); self.calls.append({'method': method, 'params': payload, 'session_id': session_id})
  if method == 'Input.dispatchMouseEvent':
   x, y = (payload.get('x'), payload.get('y'))
   if (x, y) == (180, 250) or (x is not None and y is not None and (x < 220) and (232 <= y <= 264)):
    if self.on_waypoint:
     self.on_waypoint()
  if method == 'DOM.getBoxModel':
   bid = payload.get('backendNodeId')
   boxes = {101: [100, 200, 120, 32], 102: [100, 232, 120, 32], 103: [220, 200, 160, 140], 104: [220, 296, 160, 32]}
   box = boxes.get(bid, [0, 0, 10, 10])
   x, y, w, h = box
   return {'model': {'border': [x, y, x + w, y, x + w, y + h, x, y + h]}}
  if method == 'DOM.resolveNode':
   return {'object': {'objectId': 'obj-%s' % payload.get('backendNodeId')}}
  if method == 'Runtime.callFunctionOn':
   return {'result': {'type': 'undefined'}}
  return {}
class StatefulReadNodes():
 def __init__(self, initial_raw, collapsed_raw, trigger_on_call=False):
  self.initial_nodes = ax.prune(initial_raw); self.collapsed_nodes = ax.prune(collapsed_raw); self.collapsed = False; self.calls = 0; self.trigger_on_call = trigger_on_call
 def collapse(self):
  self.collapsed = True
 def __call__(self):
  self.calls += 1
  if self.trigger_on_call and self.calls >= 1:
   self.collapsed = True
  if self.collapsed:
   return list(self.collapsed_nodes)
  return list(self.initial_nodes)
class TestPerplexityDiagonalCorridorTrap(unittest.TestCase):
 def setUp(self):
  self.waits = []
 def fake_sleep(self, seconds):
  self.waits.append(seconds)
 def test_fixture_html_contract(self):
  fixture_path = pathlib.Path(__file__).parent / 'fixtures' / 'sites' / 'jq1_a9_perplexity_diagonal_corridor_trap.html'; content = fixture_path.read_text(encoding='utf-8')
  parser = PerplexityHTMLFixtureParser(); parser.feed(content); item_ids = [m.get('id') for m in parser.menuitems]; self.assertIn('focus-item', item_ids)
  self.assertIn('attach-item', item_ids); self.assertIn('writing-item', item_ids); focus_item = next((m for m in parser.menuitems if m.get('id') == 'focus-item'))
  self.assertEqual(focus_item.get('aria-expanded'), 'true'); writing_item = next((m for m in parser.menuitems if m.get('id') == 'writing-item')); self.assertEqual(writing_item.get('aria-checked'), 'false')
  self.assertTrue(len(parser.iframes) >= 1); self.assertIn('srcdoc', parser.iframes[0])
 def test_perplexity_diagonal_corridor_trap_vanished_target_verification(self):
  initial_raw, collapsed_raw = _build_perplexity_raw_trees(); initial_nodes = ax.prune(initial_raw); writing_node = next((n for n in initial_nodes if n['name'] == 'Writing'))
  reader = StatefulReadNodes(initial_raw, collapsed_raw); cdp = DiagonalCorridorFakeCdp(on_waypoint=reader.collapse); cdp.send('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': 180, 'y': 250})
  with self.assertRaisesRegex(ActionError, "fresh AX target tie or identity downgrade"):
   run_ax_action(cdp, 'sid-diagonal-corridor-trap', initial_nodes, {'op': 'click', 'target': writing_node['id'], 'expect': {'target_states': {'checked': 'true'}}, 'irreversible': False}, reader, retries=3, wait_ms=50, sleep=self.fake_sleep, allow_coordinates=True)
 def test_absent_wait_done_wp1_02(self):
  from wender.perceive.wait import wait_done; initial_raw, _ = _build_perplexity_raw_trees(); ms = 0
  def clock():
   return ms
  def sleep(delay_ms):
   nonlocal ms
   ms += delay_ms
  receipt = wait_done(lambda: initial_raw, {'absent_roles_names': [{'role': 'button', 'name': 'Stop'}], 'present_roles_names': [{'role': 'menuitem', 'name': 'Writing'}], 'quiet_snapshots': 2}, clock=clock, sleep=sleep, max_polls=2)
  self.assertEqual(receipt, {'done': True, 'polls': 2, 'completion_status': 'confirmed_by_adapter'})
 def test_absent_text_extract_wp1_04(self):
  from wender.perceive.text import extract; raw_article = [_raw_node(1, 'RootWebArea', 'Perplexity', [2]), _raw_node(2, 'article', 'Answer', [3, 4, 5]), _raw_node(3, 'heading', 'Perplexity Answered'), _raw_node(4, 'StaticText', 'Verified citation body'), _raw_node(5, 'button', 'Copy')]
  receipt = extract(raw_article, {'role': 'heading', 'name': 'Perplexity Answered'}, {'role': 'button', 'name': 'Copy'}); self.assertEqual(receipt, {'text': 'Verified citation body', 'extraction_status': 'clean'})
 def test_absent_stopstate_detect_wp1_05(self):
  from wender.perceive.stopstate import detect; initial_raw, _ = _build_perplexity_raw_trees(); self.assertEqual(detect(initial_raw, url='https://example.test/perplexity'), {'is_stop': False, 'reason': None, 'confidence': 0, 'evidence': []})
  self.assertEqual(detect(initial_raw, headers={'cf-mitigated': 'challenge', 'content-type': 'text/html; charset=utf-8'}), {'is_stop': True, 'reason': 'cloudflare_challenge', 'confidence': 100, 'evidence': [{'kind': 'header', 'value': 'cf-mitigated: challenge'}, {'kind': 'header', 'value': 'content-type: text/html'}]})
if __name__ == '__main__':
 unittest.main()
