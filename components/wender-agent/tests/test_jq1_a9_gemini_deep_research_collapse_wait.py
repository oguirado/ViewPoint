'Tests for Gemini Deep Research collapse and wait_done stability (WP1-02).'
import html.parser
import os
import unittest
from wender.perceive import ax
class GeminiFixtureHTMLParser(html.parser.HTMLParser):
 def __init__(self):
  super().__init__(); self.roles_found, self.labels_found, self.has_inert_iframe = ([], [], False)
 def handle_starttag(self, tag, attrs):
  d = dict(attrs)
  if 'role' in d:
   self.roles_found.append(d['role'])
  if 'aria-label' in d:
   self.labels_found.append(d['aria-label'])
  if tag == 'iframe' and 'srcdoc' in d and ('http://' not in d.get('src', '')):
   self.has_inert_iframe = True
def _rn(nid, role, name, kids=(), props=None):
 d = {'nodeId': str(nid), 'role': {'type': 'role', 'value': role}, 'name': {'type': 'computed', 'value': name}, 'ignored': False, 'childIds': [str(k) for k in kids]}
 if props is not None:
  d['properties'] = [{'name': k, 'value': {'value': v}} for k, v in props.items()]
 return d
def build_raw_ax_snapshot(state):
 root = _rn(1, 'RootWebArea', 'Gemini Replica', ['2'])
 if state == 0:
  return [root, _rn(2, 'main', 'Gemini Interface', ['3']), _rn(3, 'region', 'Deep Research', ['4', '5'], {'expanded': True}), _rn(4, 'progressbar', 'Investigando', [], {'busy': True}), _rn(5, 'StaticText', 'Paso 1: Análisis inicial')]
 if state == 1:
  return [root, _rn(2, 'main', 'Gemini Interface', ['3']), _rn(3, 'region', 'Deep Research', ['4', '5', '6', '7'], {'expanded': True}), _rn(4, 'progressbar', 'Investigando', [], {'busy': True}), _rn(5, 'StaticText', 'Paso 1: Análisis inicial'), _rn(6, 'StaticText', 'Paso 2: Búsqueda avanzada'), _rn(7, 'StaticText', 'Paso 3: Síntesis de resultados')]
 if state in (2, 3, 4):
  return [root, _rn(2, 'main', 'Gemini Interface', ['3', '8']), _rn(3, 'region', 'Deep Research', [], {'expanded': False}), _rn(8, 'article', 'Informe')]
 if state == 'FAKE_PERMISSIVE_DONE':
  return [root, _rn(2, 'main', 'Gemini Interface', ['3', '8']), _rn(3, 'region', 'Deep Research', [], {'expanded': True}), _rn(8, 'article', 'Informe')]
 raise ValueError(f'Unknown state: {state}')
class FakeCDP():
 def __init__(self, states=None):
  self.states = list(states) if states is not None else [0, 1, 2, 3, 4]; self.current_idx = 0; self.enabled = False
 def send(self, method, params=None, session_id=None):
  if method == 'Accessibility.enable':
   self.enabled = True
   return {}
  if method == 'Accessibility.disable':
   self.enabled = False
   return {}
  if method == 'Accessibility.getFullAXTree':
   if not self.enabled:
    raise RuntimeError('Accessibility not enabled')
   return {'nodes': build_raw_ax_snapshot(self.states[min(self.current_idx, len(self.states) - 1)])}
  raise NotImplementedError(f'Unhandled CDP method: {method}')
 def advance(self):
  if self.current_idx < len(self.states) - 1:
   self.current_idx += 1
class LocalWaitAdapter():
 @staticmethod
 def check_done_stability(cdp, session_id=None, required_consecutive_equal_hashes=3, sleep_func=None):
  raw = ax.raw_tree(cdp, session_id=session_id)
  pruned = ax.prune(raw)
  sha = ax.snapshot_sha(pruned)
  roles = {n['role'] for n in pruned}
  if 'progressbar' in roles:
   return (False, sha)
  reg = next((n for n in pruned if n['role'] == 'region' and n['name'] == 'Deep Research'), None)
  if reg and dict(reg['attrs']).get('expanded') == 'true':
   return (False, sha)
  if 'article' not in roles:
   return (False, sha)
  return (True, sha)
class TestGeminiDeepResearchCollapseWait(unittest.TestCase):
 def test_fixture_html_contract_via_parser(self):
  fp = os.path.join(os.path.dirname(__file__), 'fixtures', 'sites', 'jq1_a9_gemini_deep_research_collapse_wait.html'); self.assertTrue(os.path.exists(fp))
  parser = GeminiFixtureHTMLParser()
  with open(fp, 'r', encoding='utf-8') as f:
   parser.feed(f.read())
  for r in ('main', 'region', 'progressbar'):
   self.assertIn(r, parser.roles_found)
  for lbl in ('Deep Research', 'Investigando'):
   self.assertIn(lbl, parser.labels_found)
  self.assertTrue(parser.has_inert_iframe)
 def test_ax_snapshot_sha_transitions_and_stability(self):
  shas = [ax.snapshot_sha(ax.prune(build_raw_ax_snapshot(i))) for i in range(5)]; self.assertEqual(shas[0], shas[1]); self.assertNotEqual(shas[0], shas[2])
  self.assertEqual(shas[2], shas[3]); self.assertEqual(shas[3], shas[4])
 def test_overly_permissive_predicate_negative_state(self):
  p_s2 = ax.prune(build_raw_ax_snapshot(2)); p_fake = ax.prune(build_raw_ax_snapshot('FAKE_PERMISSIVE_DONE')); perm = lambda p: 'progressbar' not in {n['role'] for n in p} and 'article' in {n['role'] for n in p}
  self.assertTrue(perm(p_s2) and perm(p_fake)); self.assertNotEqual(ax.snapshot_sha(p_s2), ax.snapshot_sha(p_fake))
 def test_local_adapter_wait_stability(self):
  cdp = FakeCDP([0, 1, 2, 3, 4]); cdp.send('Accessibility.enable'); self.assertFalse(LocalWaitAdapter.check_done_stability(cdp)[0]); cdp.advance(); self.assertFalse(LocalWaitAdapter.check_done_stability(cdp)[0])
  cdp.advance(); d2, s2 = LocalWaitAdapter.check_done_stability(cdp); cdp.advance(); d3, s3 = LocalWaitAdapter.check_done_stability(cdp); cdp.advance()
  d4, s4 = LocalWaitAdapter.check_done_stability(cdp); self.assertTrue(d2 and d3 and d4); self.assertEqual(s2, s3); self.assertEqual(s3, s4)
 def test_wp1_02_gemini_deep_research_wait_done(self):
  from wender.perceive.wait import wait_done
  def make_reader(c):
   def read():
    c.send('Accessibility.enable', session_id='gemini')
    try:
     nodes = c.send('Accessibility.getFullAXTree', session_id='gemini')['nodes']
    finally:
     c.send('Accessibility.disable', session_id='gemini')
    c.advance()
    return nodes
   return read
  rules = {'panel_collapsed': ('region', 'Deep Research', 'expanded', 'false'), 'absent_roles_names': [{'role': 'progressbar', 'name': 'Investigando'}], 'present_roles_names': [{'role': 'article', 'name': 'Informe'}], 'quiet_snapshots': 3}
  untouched = FakeCDP([0, 1, 2, 3, 4])
  with self.assertRaises(ValueError):
   wait_done(make_reader(untouched), {'panel_collapsed': ('region', 'Deep Research')}, clock=lambda: 0, sleep=lambda ms: None, max_polls=5)
  self.assertEqual(untouched.current_idx, 0); self.assertFalse(untouched.enabled); ms = 0; clock = lambda: ms
  def sleep(d):
   nonlocal ms
   ms += d
  res = wait_done(make_reader(FakeCDP([0, 1, 2, 3, 4])), rules, clock=clock, sleep=sleep, max_polls=5, poll_interval_ms=10); self.assertEqual(res, {'done': True, 'polls': 5, 'completion_status': 'confirmed_by_adapter'})
  res_exp = wait_done(make_reader(FakeCDP(['FAKE_PERMISSIVE_DONE'])), rules, clock=clock, sleep=sleep, max_polls=3, poll_interval_ms=10); self.assertEqual(res_exp, {'done': False, 'polls': 3, 'completion_status': 'timeout'})
  res_prog = wait_done(make_reader(FakeCDP([0])), rules, clock=clock, sleep=sleep, max_polls=3, poll_interval_ms=10); self.assertEqual(res_prog, {'done': False, 'polls': 3, 'completion_status': 'timeout'})
if __name__ == '__main__':
 unittest.main()
