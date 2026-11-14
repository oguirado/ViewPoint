'Adversarial claim-pinning tests for text boundary extraction (WP1-04).\nClaim: wender.perceive.text.extract(raw_nodes, region_start=..., region_end=..., trailers=...)\nbounds extraction strictly between the last matching start anchor (heading "ChatGPT dijo:")\nand end anchor (button "Copiar mensaje"), excluding earlier user turns and stripping site-profile\nUI chrome trailers ("ChatGPT puede cometer errores...", "Última respuesta", "Respuesta completada"),\nwhile returning "" with extraction_status="clean" on empty/whitespace-only answers without raising.\n'
import unittest
def _make_node(nid, role, name='', child_ids=None):
 return {'nodeId': str(nid), 'role': {'type': 'role', 'value': role}, 'name': {'type': 'computedString', 'value': name}, 'childIds': [str(c) for c in child_ids or []], 'ignored': False}
def make_multi_turn_fixture():
 return [_make_node(1, 'RootWebArea', 'Doc', [2]), _make_node(2, 'main', '', [10, 20, 30]), _make_node(10, 'group', 'User turn', [11, 12]), _make_node(11, 'StaticText', 'User prompt'), _make_node(12, 'button', 'Copiar mensaje'), _make_node(20, 'group', 'Assistant turn', [21, 22, 23]), _make_node(21, 'heading', 'ChatGPT dijo:'), _make_node(22, 'StaticText', 'Final recommendation'), _make_node(23, 'button', 'Copiar mensaje'), _make_node(30, 'group', 'Footer', [31, 32, 33]), _make_node(31, 'StaticText', 'ChatGPT puede cometer errores. Comprueba la información importante.'), _make_node(32, 'StaticText', 'Última respuesta'), _make_node(33, 'StaticText', 'Respuesta completada')]
def make_whitespace_answer_fixture():
 return [_make_node(1, 'RootWebArea', 'Doc', [2]), _make_node(2, 'main', '', [10, 20, 30]), _make_node(10, 'group', 'User turn', [11, 12]), _make_node(11, 'StaticText', 'User prompt'), _make_node(12, 'button', 'Copiar mensaje'), _make_node(20, 'group', 'Assistant turn', [21, 22, 23]), _make_node(21, 'heading', 'ChatGPT dijo:'), _make_node(22, 'StaticText', ' '), _make_node(23, 'button', 'Copiar mensaje'), _make_node(30, 'group', 'Footer', [31, 32, 33]), _make_node(31, 'StaticText', 'ChatGPT puede cometer errores. Comprueba la información importante.'), _make_node(32, 'StaticText', 'Última respuesta'), _make_node(33, 'StaticText', 'Respuesta completada')]
REGION_START = {'role': 'heading', 'name': 'ChatGPT dijo:'}
REGION_END = {'role': 'button', 'name': 'Copiar mensaje'}
TRAILERS = ('ChatGPT puede cometer errores. Comprueba la información importante.', 'Última respuesta', 'Respuesta completada')
class TestTextBoundaryHealthyControl(unittest.TestCase):
 def test_fixture_topology_and_integrity(self):
  fixture1 = make_multi_turn_fixture(); self.assertEqual(len(fixture1), 13); start_nodes = [n for n in fixture1 if n['role']['value'] == REGION_START['role'] and n['name']['value'] == REGION_START['name']]
  self.assertEqual(len(start_nodes), 1); end_nodes = [n for n in fixture1 if n['role']['value'] == REGION_END['role'] and n['name']['value'] == REGION_END['name']]
  self.assertEqual(len(end_nodes), 2)
 def test_whitespace_fixture_topology(self):
  fixture2 = make_whitespace_answer_fixture(); ws_node = [n for n in fixture2 if n['nodeId'] == '22'][0]; self.assertEqual(ws_node['name']['value'], ' ')
class TestTextBoundaryPinning(unittest.TestCase):
 def test_extract_text_multi_turn_boundary_wp1_04(self):
  from wender.perceive.text import extract; raw_nodes = make_multi_turn_fixture(); result = extract(raw_nodes, region_start=REGION_START, region_end=REGION_END, trailers=TRAILERS)
  self.assertEqual(result, {'text': 'Final recommendation', 'extraction_status': 'clean'}); self.assertNotIn('User prompt', result['text']); self.assertNotIn('ChatGPT puede cometer errores', result['text'])
  self.assertNotIn('Última respuesta', result['text']); self.assertNotIn('Respuesta completada', result['text'])
 def test_extract_text_empty_whitespace_answer_wp1_04(self):
  from wender.perceive.text import extract; raw_nodes = make_whitespace_answer_fixture(); result = extract(raw_nodes, region_start=REGION_START, region_end=REGION_END, trailers=TRAILERS)
  self.assertEqual(result, {'text': '', 'extraction_status': 'clean'})
 def test_extract_text_rejects_page_wide_shared_group_boundary(self):
  from wender.perceive.text import extract; page_wide_group = [_make_node(1, 'RootWebArea', 'Doc', [2]), _make_node(2, 'group', 'Conversation', [11, 12, 21, 22, 23]), _make_node(11, 'StaticText', 'User prompt'), _make_node(12, 'button', 'Copiar mensaje'), _make_node(21, 'heading', 'ChatGPT dijo:'), _make_node(22, 'StaticText', 'Final recommendation'), _make_node(23, 'button', 'Copiar mensaje')]
  result = extract(page_wide_group, region_start=REGION_START, region_end=REGION_END, trailers=TRAILERS); self.assertEqual(result, {'text': '', 'extraction_status': 'ambiguous_boundary'})
if __name__ == '__main__':
 unittest.main()
