'Adversarial claim-pinning tests for wender.perceive.wait.wait_done (WP1-02).'
import unittest
from wender.perceive import ax
def _n(nid, role, name='', kids=(), ignored=False, props=None, backend=None):
 d = {'nodeId': str(nid), 'role': {'type': 'role', 'value': role}, 'name': {'type': 'computedString', 'value': name}, 'childIds': [str(k) for k in kids], 'ignored': ignored}
 if props:
  d['properties'] = [{'name': k, 'value': {'type': 'x', 'value': v}} for k, v in props.items()]
 if backend is not None:
  d['backendDOMNodeId'] = backend
 return d
def make_chatgpt_snapshot(has_detener=False, has_copiar=True, heading_text='ChatGPT dijo:', msg_text='Response text'):
 kids_main = []; kids_turn = []; nodes = [_n(1, 'RootWebArea', 'ChatGPT', kids=[2])]; next_id = 3
 if has_detener:
  detener_id = next_id
  next_id += 1
  kids_main.append(detener_id)
  nodes.append(_n(detener_id, 'button', 'Detener', backend=10))
 turn_id = next_id; next_id += 1; kids_main.append(turn_id); heading_id = next_id; next_id += 1; kids_turn.append(heading_id); nodes.append(_n(heading_id, 'heading', heading_text, backend=11))
 text_id = next_id; next_id += 1; kids_turn.append(text_id); nodes.append(_n(text_id, 'note', msg_text, backend=12))
 if has_copiar:
  copiar_id = next_id
  next_id += 1
  kids_turn.append(copiar_id)
  nodes.append(_n(copiar_id, 'button', 'Copiar mensaje', backend=13))
 nodes.append(_n(turn_id, 'group', 'Assistant turn', kids=kids_turn)); nodes.append(_n(2, 'main', '', kids=kids_main)); return ax.prune(nodes)
class FakeSnapshotReader():
 def __init__(self, snapshots):
  self.snapshots = list(snapshots); self.poll_count = 0
 def __call__(self):
  if not self.snapshots:
   raise RuntimeError('FakeSnapshotReader ran out of queued snapshots')
  self.poll_count += 1
  if len(self.snapshots) > 1:
   return self.snapshots.pop(0)
  return self.snapshots[0]
class TestJq1A6WaitAnchor(unittest.TestCase):
 def test_healthy_control_fixture_construction_and_sha_stability(self):
  snap1 = make_chatgpt_snapshot(has_detener=False, has_copiar=True); snap2 = make_chatgpt_snapshot(has_detener=False, has_copiar=True); roles_and_names = [(n['role'], n['name']) for n in snap1]
  self.assertNotIn(('button', 'Detener'), roles_and_names); self.assertIn(('heading', 'ChatGPT dijo:'), roles_and_names); self.assertIn(('button', 'Copiar mensaje'), roles_and_names)
  heading_idx = next((i for i, n in enumerate(snap1) if n['role'] == 'heading' and n['name'] == 'ChatGPT dijo:')); copiar_idx = next((i for i, n in enumerate(snap1) if n['role'] == 'button' and n['name'] == 'Copiar mensaje'))
  self.assertLess(heading_idx, copiar_idx, "Heading 'ChatGPT dijo:' must precede 'Copiar mensaje' in document order"); sha1 = ax.snapshot_sha(snap1, True)
  sha2 = ax.snapshot_sha(snap2, True); self.assertEqual(sha1, sha2); reader = FakeSnapshotReader([snap1, snap2]); self.assertEqual(len(reader()), len(snap1))
  self.assertEqual(reader.poll_count, 1)
 def test_wp1_02_wait_done_succeeds_when_quiet_snapshots_and_anchor_rule_pass(self):
  from wender.perceive.wait import wait_done; snap1 = make_chatgpt_snapshot(has_detener=False, has_copiar=True); snap2 = make_chatgpt_snapshot(has_detener=False, has_copiar=True)
  self.assertEqual(ax.snapshot_sha(snap1, True), ax.snapshot_sha(snap2, True)); reader = FakeSnapshotReader([snap1, snap2]); profile = {'absent_roles_names': [('button', 'Detener')], 'after_last_anchor': {'anchor': {'role': 'heading', 'name': 'ChatGPT dijo:'}, 'target': {'role': 'button', 'name': 'Copiar mensaje'}}, 'quiet_snapshots': 2}
  res = wait_done(reader, profile, max_polls=5, sleep_fn=lambda s: None); self.assertEqual(res, {'done': True, 'polls': 2, 'completion_status': 'confirmed_by_adapter'})
 def test_wp1_02_wait_done_fails_when_detener_button_present(self):
  from wender.perceive.wait import wait_done; snap1 = make_chatgpt_snapshot(has_detener=True, has_copiar=False); snap2 = make_chatgpt_snapshot(has_detener=True, has_copiar=False)
  reader = FakeSnapshotReader([snap1, snap2]); profile = {'absent_roles_names': [('button', 'Detener')], 'after_last_anchor': {'anchor': {'role': 'heading', 'name': 'ChatGPT dijo:'}, 'target': {'role': 'button', 'name': 'Copiar mensaje'}}, 'quiet_snapshots': 2}
  res = wait_done(reader, profile, max_polls=2, sleep_fn=lambda s: None); self.assertEqual(res, {'done': False, 'polls': 2, 'completion_status': 'timeout'})
 def test_wp1_02_wait_done_fails_when_anchor_target_missing(self):
  from wender.perceive.wait import wait_done; snap1 = make_chatgpt_snapshot(has_detener=False, has_copiar=False); snap2 = make_chatgpt_snapshot(has_detener=False, has_copiar=False)
  reader = FakeSnapshotReader([snap1, snap2]); profile = {'absent_roles_names': [('button', 'Detener')], 'after_last_anchor': {'anchor': {'role': 'heading', 'name': 'ChatGPT dijo:'}, 'target': {'role': 'button', 'name': 'Copiar mensaje'}}, 'quiet_snapshots': 2}
  res = wait_done(reader, profile, max_polls=2, sleep_fn=lambda s: None); self.assertEqual(res, {'done': False, 'polls': 2, 'completion_status': 'timeout'})
 def test_wp1_02_wait_done_fails_when_snapshots_not_quiet(self):
  from wender.perceive.wait import wait_done; snap1 = make_chatgpt_snapshot(has_detener=False, has_copiar=True, msg_text='chunk 1'); snap2 = make_chatgpt_snapshot(has_detener=False, has_copiar=True, msg_text='chunk 1 and 2')
  self.assertNotEqual(ax.snapshot_sha(snap1, True), ax.snapshot_sha(snap2, True)); reader = FakeSnapshotReader([snap1, snap2]); profile = {'absent_roles_names': [('button', 'Detener')], 'after_last_anchor': {'anchor': {'role': 'heading', 'name': 'ChatGPT dijo:'}, 'target': {'role': 'button', 'name': 'Copiar mensaje'}}, 'quiet_snapshots': 2}
  res = wait_done(reader, profile, max_polls=2, sleep_fn=lambda s: None); self.assertEqual(res, {'done': False, 'polls': 2, 'completion_status': 'timeout'})
if __name__ == '__main__':
 unittest.main()
