'Adversarial claim-pinning tests for text subtree extraction (wender.perceive.text.extract).'
import unittest
from wender.perceive import ax
def _n(nid, role, name='', kids=(), ignored=False, props=None, backend=None):
 d = {'nodeId': str(nid), 'role': {'type': 'role', 'value': role}, 'name': {'type': 'computedString', 'value': name}, 'childIds': [str(k) for k in kids], 'ignored': ignored}
 if props:
  d['properties'] = [{'name': k, 'value': {'type': 'x', 'value': v}} for k, v in props.items()]
 if backend is not None:
  d['backendDOMNodeId'] = backend
 return d
def make_text_subtree_fixture():
 return [_n(1, 'article', 'Turn Article', kids=[2]), _n(2, 'generic', '', kids=[3, 6, 10, 12], ignored=True), _n(3, 'heading', '', kids=[4], props={'level': 2}), _n(4, 'StaticText', 'Results', kids=[5]), _n(5, 'InlineTextBox', 'Results', kids=[]), _n(6, 'paragraph', '', kids=[7]), _n(7, 'StaticText', 'Hello world', kids=[8, 9]), _n(8, 'InlineTextBox', 'Hello ', kids=[]), _n(9, 'InlineTextBox', 'world', kids=[]), _n(10, 'listitem', '', kids=[11]), _n(11, 'StaticText', 'Yes', kids=[]), _n(12, 'listitem', '', kids=[13]), _n(13, 'StaticText', 'Yes', kids=[])]
class TestJq1A6TextSubtreeControl(unittest.TestCase):
 def test_fixture_structure_and_prune_behavior(self):
  nodes = make_text_subtree_fixture(); self.assertEqual(len(nodes), 13); pruned = ax.prune(nodes); self.assertTrue(len(pruned) > 0); self.assertEqual(pruned[0]['role'], 'article')
class TestJq1A6TextSubtreeExtract(unittest.TestCase):
 def test_extract_reconstructs_ax_hierarchy_and_deduplicates_inlinetextboxes(self):
  from wender.perceive.text import extract; raw_nodes = make_text_subtree_fixture(); res = extract(raw_nodes); self.assertEqual(res, {'text': 'Results\nHello world\nYes\nYes', 'extraction_status': 'clean'})
  self.assertIn('Results', res['text']); self.assertIn('Hello world', res['text']); self.assertNotIn('Hello worldHello world', res['text']); self.assertEqual(res['text'].count('Yes'), 2)
 def test_extract_validates_disconnected_components_in_forest(self):
  from wender.perceive.text import extract; raw_nodes = make_text_subtree_fixture(); raw_nodes += [_n(90, 'generic', '', kids=[91]), _n(91, 'generic', '', kids=[90])]
  self.assertEqual(extract(raw_nodes), {'text': '', 'extraction_status': 'incomplete_tree'})
if __name__ == '__main__':
 unittest.main()
