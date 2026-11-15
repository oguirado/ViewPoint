'Tests for Gemini Deep Research opaque article pruning and text extraction (WP1-04).'
from html.parser import HTMLParser
import importlib
import os
import unittest
from wender.perceive import ax
def _raw_node(nid, role, name='', kids=(), ignored=False, props=None, backend=None):
 node = {'nodeId': str(nid), 'role': {'type': 'role', 'value': role}, 'name': {'type': 'computedString', 'value': name}, 'childIds': [str(k) for k in kids], 'ignored': ignored}
 if props:
  node['properties'] = [{'name': k, 'value': {'type': 'value', 'value': v}} for k, v in props.items()]
 if backend is not None:
  node['backendDOMNodeId'] = backend
 return node
def build_gemini_raw_ax_tree():
 return [_raw_node(1, 'region', 'Deep Research', kids=[2, 5], backend=10), _raw_node(2, 'progressbar', 'Analizando 12 sitios', kids=[3], backend=20), _raw_node(3, 'StaticText', 'Paso 1...', kids=[], backend=30), _raw_node(5, 'article', 'Informe', kids=[6, 7, 9, 11], backend=50), _raw_node(6, 'heading', 'Conclusión', kids=[], backend=60), _raw_node(7, 'paragraph', '', kids=[8], backend=70), _raw_node(8, 'StaticText', 'Hallazgo principal.', kids=[], backend=80), _raw_node(9, 'code', '', kids=[10], backend=90), _raw_node(10, 'StaticText', 'x = 42', kids=[], backend=100), _raw_node(11, 'button', 'Copiar', kids=[], backend=110)]
class GeminiFixtureParser(HTMLParser):
 def __init__(self):
  super().__init__(); self.elements = []
 def handle_starttag(self, tag, attrs):
  attr_dict = dict(attrs); role = attr_dict.get('role') or tag; label = attr_dict.get('aria-label', ''); self.elements.append({'tag': tag, 'role': role, 'label': label, 'attrs': attr_dict})
class FakeCDPAdapter():
 def __init__(self, raw_nodes=None):
  self.raw_nodes = raw_nodes if raw_nodes is not None else build_gemini_raw_ax_tree(); self.calls = []
 def send(self, method, params=None, session_id=None):
  payload = dict(params or {}); self.calls.append({'method': method, 'params': payload, 'session_id': session_id})
  if method == 'Accessibility.enable':
   return {}
  if method == 'Accessibility.getFullAXTree':
   return {'nodes': self.raw_nodes}
  if method == 'Accessibility.disable':
   return {}
  if method == 'DOM.resolveNode':
   return {'object': {'objectId': 'obj-%s' % payload.get('backendNodeId')}}
  return {}
class TestGeminiOpaqueArticleExtract(unittest.TestCase):
 def setUp(self):
  self.fixture_path = os.path.join(os.path.dirname(__file__), 'fixtures', 'sites', 'jq1_a9_gemini_opaque_article_extract.html')
 def test_fixture_contract_with_html_parser(self):
  self.assertTrue(os.path.exists(self.fixture_path), 'Fixture file must exist')
  with open(self.fixture_path, 'r', encoding='utf-8') as f:
   content = f.read()
  parser = GeminiFixtureParser(); parser.feed(content); roles_found = [e['role'] for e in parser.elements]; labels_found = [e['label'] for e in parser.elements if e['label']]
  self.assertIn('region', roles_found); self.assertIn('progressbar', roles_found); self.assertIn('article', roles_found); self.assertIn('Deep Research', labels_found)
  self.assertIn('Analizando 12 sitios', labels_found); self.assertIn('Informe', labels_found)
 def test_ax_prune_opaque_article_behavior(self):
  raw_nodes = build_gemini_raw_ax_tree(); pruned = ax.prune(raw_nodes); roles = [n['role'] for n in pruned]; names = [n['name'] for n in pruned]; self.assertIn('region', roles)
  self.assertIn('progressbar', roles); self.assertIn('article', roles); self.assertIn('Deep Research', names); self.assertIn('Analizando 12 sitios', names)
  self.assertIn('Informe', names); self.assertNotIn('Conclusión', names); self.assertNotIn('Copiar', names); non_opaque_raw = [_raw_node(1, 'region', 'Deep Research', kids=[5]), _raw_node(5, 'group', 'Informe', kids=[6, 11]), _raw_node(6, 'heading', 'Conclusión', kids=[]), _raw_node(11, 'button', 'Copiar', kids=[])]
  non_opaque_pruned = ax.prune(non_opaque_raw); non_opaque_names = [n['name'] for n in non_opaque_pruned]; self.assertIn('Conclusión', non_opaque_names)
  self.assertIn('Copiar', non_opaque_names)
 def test_ax_raw_tree_integration_with_fake_cdp(self):
  cdp = FakeCDPAdapter(); nodes = ax.raw_tree(cdp, 'session-1'); pruned = ax.prune(nodes); self.assertEqual(len(pruned), 3); self.assertEqual([n['role'] for n in pruned], ['region', 'progressbar', 'article'])
 def test_wp1_04_text_extract_gemini_article_report(self):
  from wender.perceive.text import extract; raw_nodes = build_gemini_raw_ax_tree(); extracted = extract(raw_nodes, target_role='article', target_name='Informe')
  self.assertEqual(extracted, {'text': 'Conclusión\nHallazgo principal.\nx = 42', 'extraction_status': 'clean'}); self.assertIn('Conclusión', extracted['text'])
  self.assertIn('Hallazgo principal.', extracted['text']); self.assertIn('x = 42', extracted['text']); self.assertNotIn('Analizando 12 sitios', extracted['text'])
  self.assertNotIn('Paso 1...', extracted['text'])
if __name__ == '__main__':
 unittest.main()
