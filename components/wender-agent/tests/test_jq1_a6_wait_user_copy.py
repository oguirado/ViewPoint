'Adversarial claim-pinning tests for wender.perceive.wait.wait_done (WP1-02).'
import unittest
from unittest.mock import MagicMock
def create_mock_snapshot(nodes):
 return [{'role': role, 'name': name} for role, name in nodes]
class TestJq1A6WaitUserCopy(unittest.TestCase):
 def test_healthy_control_fixture_sequence(self):
  snapshots = [[('heading', 'ChatGPT dijo:'), ('button', 'Detener')], [('heading', 'ChatGPT dijo:'), ('button', 'Copiar mensaje')]]; self.assertEqual(len(snapshots), 2)
  has_stop = any((role == 'button' and name == 'Detener' for role, name in snapshots[0])); self.assertTrue(has_stop); has_anchor = any((role == 'heading' and name == 'ChatGPT dijo:' for role, name in snapshots[1]))
  has_copy_after = any((role == 'button' and name == 'Copiar mensaje' for role, name in snapshots[1])); self.assertTrue(has_anchor and has_copy_after)
 def test_user_turn_copy_button_trap_no_assistant_heading(self):
  from wender.perceive.wait import wait_done; snapshots = [[('paragraph', 'User prompt text'), ('button', 'Copiar mensaje')], [('paragraph', 'User prompt text'), ('button', 'Copiar mensaje')], [('paragraph', 'User prompt text'), ('button', 'Copiar mensaje')]]
  mock_reader = MagicMock(side_effect=[create_mock_snapshot(s) for s in snapshots]); rules = {'absent_roles_names': [('button', 'Detener')], 'after_last_anchor': ('heading', 'ChatGPT dijo:'), 'present_roles_names': [('button', 'Copiar mensaje')], 'quiet_snapshots': 2, 'max_polls': 3, 'sleep': lambda seconds: None}
  res = wait_done(mock_reader, **rules); self.assertEqual(res, {'done': False, 'polls': 3, 'completion_status': 'timeout'})
 def test_user_turn_copy_button_followed_by_streaming_assistant(self):
  from wender.perceive.wait import wait_done; snapshots = [[('paragraph', 'User prompt text'), ('button', 'Copiar mensaje'), ('heading', 'ChatGPT dijo:'), ('paragraph', 'Partial response text...')], [('paragraph', 'User prompt text'), ('button', 'Copiar mensaje'), ('heading', 'ChatGPT dijo:'), ('paragraph', 'More partial text...')], [('paragraph', 'User prompt text'), ('button', 'Copiar mensaje'), ('heading', 'ChatGPT dijo:'), ('paragraph', 'Still streaming text...')]]
  mock_reader = MagicMock(side_effect=[create_mock_snapshot(s) for s in snapshots]); rules = {'absent_roles_names': [('button', 'Detener')], 'after_last_anchor': ('heading', 'ChatGPT dijo:'), 'present_roles_names': [('button', 'Copiar mensaje')], 'quiet_snapshots': 2, 'max_polls': 3, 'sleep': lambda seconds: None}
  res = wait_done(mock_reader, **rules); self.assertEqual(res, {'done': False, 'polls': 3, 'completion_status': 'timeout'})
 def test_complete_turn_nodes_with_active_stop_button(self):
  from wender.perceive.wait import wait_done; snapshots = [[('heading', 'ChatGPT dijo:'), ('paragraph', 'Complete response text'), ('button', 'Copiar mensaje'), ('button', 'Detener')], [('heading', 'ChatGPT dijo:'), ('paragraph', 'Complete response text'), ('button', 'Copiar mensaje'), ('button', 'Detener')], [('heading', 'ChatGPT dijo:'), ('paragraph', 'Complete response text'), ('button', 'Copiar mensaje'), ('button', 'Detener')]]
  mock_reader = MagicMock(side_effect=[create_mock_snapshot(s) for s in snapshots]); rules = {'absent_roles_names': [('button', 'Detener')], 'after_last_anchor': ('heading', 'ChatGPT dijo:'), 'present_roles_names': [('button', 'Copiar mensaje')], 'quiet_snapshots': 2, 'max_polls': 3, 'sleep': lambda seconds: None}
  res = wait_done(mock_reader, **rules); self.assertEqual(res, {'done': False, 'polls': 3, 'completion_status': 'timeout'})
 def test_legacy_quiet_threshold_one_cannot_complete_before_canonical_threshold(self):
  from wender.perceive.wait import wait_done; snap = [{'nodeId': '1', 'role': {'value': 'RootWebArea'}, 'name': {'value': 'ChatGPT'}, 'childIds': ['2']}, {'nodeId': '2', 'role': {'value': 'article'}, 'name': {'value': ''}, 'childIds': ['3', '4']}, {'nodeId': '3', 'role': {'value': 'heading'}, 'name': {'value': 'ChatGPT dijo:'}, 'childIds': []}, {'nodeId': '4', 'role': {'value': 'button'}, 'name': {'value': 'Copiar mensaje'}, 'childIds': []}]
  one_poll = wait_done(lambda: snap, absent_roles_names=[('button', 'Detener')], after_last_anchor=('heading', 'ChatGPT dijo:'), present_roles_names=[('button', 'Copiar mensaje')], quiet_snapshots=1, max_polls=1, sleep=lambda seconds: None)
  self.assertEqual(one_poll, {'done': False, 'polls': 1, 'completion_status': 'timeout'}); two_polls = wait_done(lambda: snap, absent_roles_names=[('button', 'Detener')], after_last_anchor=('heading', 'ChatGPT dijo:'), present_roles_names=[('button', 'Copiar mensaje')], quiet_snapshots=1, max_polls=2, sleep=lambda seconds: None)
  self.assertEqual(two_polls, {'done': True, 'polls': 2, 'completion_status': 'confirmed_by_adapter'})
if __name__ == '__main__':
 unittest.main()
