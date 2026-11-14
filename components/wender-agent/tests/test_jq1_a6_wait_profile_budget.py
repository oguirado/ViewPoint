"Adversarial claim-pinning tests for wender.perceive.wait.wait_done (WP1-02).\nClaim: wender.perceive.wait.wait_done is purely data-driven via a site-profile\nrule dictionary (switching stop button name from 'Detener' to 'Stop' or quiet\nsnapshot count from 2 to 3 flips the verdict without code changes) and enforces\na deterministic max_polls budget with an injectable clock/sleep callable.\n"
import time
import unittest
from wender.perceive import ax
class SnapshotSequenceReader():
 def __init__(self, snapshots):
  self.snapshots = list(snapshots); self.call_count = 0
 def __call__(self):
  if self.call_count < len(self.snapshots):
   snap = self.snapshots[self.call_count]
  else:
   snap = self.snapshots[-1] if self.snapshots else []
  self.call_count += 1; return snap
class FakeClock():
 def __init__(self):
  self.sleep_calls = []; self.elapsed_time = 0.0
 def sleep(self, seconds):
  self.sleep_calls.append(seconds); self.elapsed_time += seconds
def _make_ax_nodes(button_name=None, message_id=1):
 raw = [{'nodeId': '1', 'role': {'value': 'RootWebArea'}, 'name': {'value': 'ChatGPT'}}, {'nodeId': '2', 'role': {'value': 'heading'}, 'name': {'value': 'ChatGPT dijo:'}}, {'nodeId': '3', 'role': {'value': 'button'}, 'name': {'value': 'Copiar mensaje %d' % message_id}}]
 if button_name:
  raw.append({'nodeId': '4', 'role': {'value': 'button'}, 'name': {'value': button_name}})
 return ax.prune(raw)
def build_4_snapshot_sequence(button_name=None):
 s1 = _make_ax_nodes(button_name=button_name, message_id=1); s2 = _make_ax_nodes(button_name=button_name, message_id=2); s3 = _make_ax_nodes(button_name=button_name, message_id=2)
 s4 = _make_ax_nodes(button_name=button_name, message_id=2); return [s1, s2, s3, s4]
class TestWaitProfileBudget(unittest.TestCase):
 def test_healthy_control_fixture_recipe_sha_sequence(self):
  seq = build_4_snapshot_sequence(button_name='Detener'); sha1 = ax.snapshot_sha(seq[0]); sha2 = ax.snapshot_sha(seq[1]); sha3 = ax.snapshot_sha(seq[2])
  sha4 = ax.snapshot_sha(seq[3]); self.assertNotEqual(sha1, sha2); self.assertEqual(sha2, sha3); self.assertEqual(sha3, sha4)
 def test_healthy_control_profile_rules_contract(self):
  profile_a = {'quiet_snapshots': 2, 'absent_roles_names': [('button', 'Detener')], 'present_roles_names': [('button', 'Copiar mensaje 2')], 'after_last_anchor': ('heading', 'ChatGPT dijo:')}
  profile_b = {'quiet_snapshots': 3, 'absent_roles_names': [('button', 'Stop')], 'present_roles_names': [('button', 'Copiar mensaje 2')], 'after_last_anchor': ('heading', 'ChatGPT dijo:')}
  self.assertEqual(profile_a['quiet_snapshots'], 2); self.assertEqual(profile_b['quiet_snapshots'], 3); fake_clock = FakeClock(); fake_clock.sleep(0.1)
  self.assertEqual(fake_clock.sleep_calls, [0.1]); self.assertEqual(fake_clock.elapsed_time, 0.1)
 def test_profile_a_completes_on_poll_3(self):
  from wender.perceive.wait import wait_done; seq = build_4_snapshot_sequence(button_name=None); reader = SnapshotSequenceReader(seq); fake_clock = FakeClock()
  rules = {'quiet_snapshots': 2, 'absent_roles_names': [('button', 'Detener')], 'present_roles_names': [('button', 'Copiar mensaje 2')], 'after_last_anchor': None}
  t0 = time.monotonic(); res = wait_done(reader, rules, max_polls=10, sleep=fake_clock.sleep); vm_duration = time.monotonic() - t0; self.assertEqual(res, {'done': True, 'polls': 3, 'completion_status': 'confirmed_by_adapter'})
  self.assertLess(vm_duration, 1.0)
 def test_profile_b_blocks_when_stop_button_present(self):
  from wender.perceive.wait import wait_done; seq = build_4_snapshot_sequence(button_name='Stop'); reader = SnapshotSequenceReader(seq); fake_clock = FakeClock()
  rules = {'quiet_snapshots': 3, 'absent_roles_names': [('button', 'Stop')], 'present_roles_names': [('button', 'Copiar mensaje 2')], 'after_last_anchor': None}
  res = wait_done(reader, rules, max_polls=4, sleep=fake_clock.sleep); self.assertEqual(res, {'done': False, 'polls': 4, 'completion_status': 'timeout'})
 def test_profile_b_completes_on_poll_4_when_stop_button_absent(self):
  from wender.perceive.wait import wait_done; seq = build_4_snapshot_sequence(button_name=None); reader = SnapshotSequenceReader(seq); fake_clock = FakeClock()
  rules = {'quiet_snapshots': 3, 'absent_roles_names': [('button', 'Stop')], 'present_roles_names': [('button', 'Copiar mensaje 2')], 'after_last_anchor': None}
  res = wait_done(reader, rules, max_polls=10, sleep=fake_clock.sleep); self.assertEqual(res, {'done': True, 'polls': 4, 'completion_status': 'confirmed_by_adapter'})
 def test_never_settling_sequence_terminates_at_max_polls_4(self):
  from wender.perceive.wait import wait_done; never_settling = [_make_ax_nodes(button_name=None, message_id=i) for i in range(1, 10)]; reader = SnapshotSequenceReader(never_settling)
  fake_clock = FakeClock(); rules = {'quiet_snapshots': 2, 'absent_roles_names': [('button', 'Detener')], 'present_roles_names': [('heading', 'ChatGPT dijo:')], 'after_last_anchor': None}
  t0 = time.monotonic(); res = wait_done(reader, rules, max_polls=4, sleep=fake_clock.sleep); vm_duration = time.monotonic() - t0; self.assertEqual(res, {'done': False, 'polls': 4, 'completion_status': 'timeout'})
  self.assertLess(vm_duration, 1.0)
 def test_empty_present_selectors_returns_stable_unconfirmed(self):
  from wender.perceive.wait import wait_done; seq = build_4_snapshot_sequence(button_name=None); reader = SnapshotSequenceReader(seq); fake_clock = FakeClock()
  rules = {'quiet_snapshots': 2, 'absent_roles_names': [('button', 'Detener')], 'present_roles_names': [], 'after_last_anchor': None}; res = wait_done(reader, rules, max_polls=10, sleep=fake_clock.sleep)
  self.assertEqual(res, {'done': False, 'polls': 3, 'completion_status': 'stable_unconfirmed'})
if __name__ == '__main__':
 unittest.main()
