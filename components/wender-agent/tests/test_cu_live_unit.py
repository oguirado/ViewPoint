# @module {"id":"wa.tests.cu_live_unit","layer":2,"owner":"ARCH","deps":["wa.tools.cu_live"],"book":"HB-WA-s4-learn"}
"""Lifecycle fault injection; no real capsule, model, or image writes."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import cu_live


class TestLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        (self.app / 'holdings-h2l').touch()
        self.run_dir = self.root / 'episode'
        self.container = 'srf-ng-hcap-hc-012345abcdef-1'
        self.slot = dict(container=self.container, slot=1, run_id='hc-012345abcdef',
                         display=':100', width=1280, height=900, lane_socket='/run/lane',
                         frames_dir=str(self.root / 'frames'), ready_ms=1,
                         ready_line='HOLDINGS_READY 1001 0')
        self.terminal = dict(stop_reason='done', terminal_observation='I read the first item: hello',
                             final_frame=dict(path=str(self.root / 'final.png'),
                                              sha256_rgb24='a' * 64))
        self.argv = ['--app', str(self.app), '--scenario', 'inbox-driven', '--seed', '1001',
                     '--goal', 'open the inbox and read the first item',
                     '--ve-root', str(self.root), '--muse-workspace', str(self.root),
                     '--run-dir', str(self.run_dir)]

    def invoke(self, *, response=None, start_code=0, start_error=None,
               loop_error=None, stop_error=None, stop_code=0, terminal=None, loop_code=0):
        def transport(argv, **kwargs):
            self.assertFalse(kwargs['check'])
            if argv[1] == 'start':
                if start_error:
                    raise start_error
                payload = json.dumps([self.slot]) if response is None else response
                return subprocess.CompletedProcess(argv, start_code, stdout=payload)
            if stop_error:
                raise stop_error
            return subprocess.CompletedProcess(argv, stop_code, stdout='{}')
        def loop(argv, **kwargs):
            self.assertIn('srf:' + self.container, argv)
            self.assertNotIn('--mask', argv)
            if loop_error:
                raise loop_error
            print(json.dumps(self.terminal if terminal is None else terminal))
            return loop_code
        with patch.object(cu_live.subprocess, 'run', side_effect=transport) as calls, \
             patch.object(cu_live, 'loop_main', side_effect=loop) as loops, \
             contextlib.redirect_stdout(io.StringIO()):
            code = cu_live.main(self.argv)
        summary = json.loads((self.run_dir / 'summary.json').read_text())
        for name in ('start.json', 'terminal.json', 'cleanup.json'):
            self.assertTrue((self.run_dir / name).is_file())
        return code, summary, calls, loops

    def test_valid_path_preserves_evidence_without_claiming_goal(self):
        code, summary, calls, loops = self.invoke()
        self.assertEqual(loops.call_args.args[0][loops.call_args.args[0].index('--muse-timeout') + 1], '240')
        self.assertEqual(code, 0)
        self.assertEqual(summary['terminal_summary'], self.terminal)
        self.assertEqual(summary['requested']['seed'], 1001)
        self.assertEqual(summary['requested']['scenario'], 'inbox-driven')
        self.assertEqual(summary['requested']['goal'], self.argv[7])
        self.assertEqual(summary['effective']['max_ms'], 300000)
        self.assertTrue(summary['transport_completed'])
        self.assertTrue(summary['model_claimed_done'])
        self.assertEqual(summary['goal_evidence'], 'not_reached')
        self.assertTrue(summary['cleanup']['ok'])
        self.assertEqual(calls.call_args.args[0][1:], ['stop', '--container', self.container])

    def test_lean_defaults_without_workspace_argument(self):
        index = self.argv.index('--muse-workspace')
        del self.argv[index:index + 2]
        code, _, _, loops = self.invoke()
        self.assertEqual(code, 0)
        command = loops.call_args.args[0]
        self.assertEqual(command[command.index('--effort') + 1], 'auto')
        self.assertEqual(command[command.index('--images') + 1], 'marks')
        self.assertNotIn('--muse-workspace', command)

    def test_full_images_and_fixed_effort_forwarded(self):
        self.argv += ['--images', 'full', '--effort', 'xhigh']
        code, _, _, loops = self.invoke()
        self.assertEqual(code, 0)
        command = loops.call_args.args[0]
        self.assertEqual(command[command.index('--effort') + 1], 'xhigh')
        self.assertEqual(command[command.index('--images') + 1], 'full')

    def test_muse_timeout_forwarded_to_loop(self):
        self.argv += ['--muse-timeout', '175.5']
        code, summary, _, loops = self.invoke()
        self.assertEqual(code, 0)
        command = loops.call_args.args[0]
        self.assertEqual(command[command.index('--muse-timeout') + 1], '175.5')
        self.assertEqual(summary['effective']['muse_timeout'], 175.5)

    def test_non_success_start_cleans_recovered_identity(self):
        code, summary, calls, loops = self.invoke(start_code=4)
        self.assertNotEqual(code, 0)
        self.assertEqual(summary['primary_outcome']['stage'], 'start')
        self.assertTrue(summary['cleanup']['ok'])
        self.assertEqual(calls.call_count, 2)
        loops.assert_not_called()

    def test_malformed_array_cleans_all_recoverable_entries(self):
        other = 'srf-ng-hcap-hc-abcdef012345-2'
        code, summary, calls, loops = self.invoke(response=json.dumps([
            {'container': self.container}, {'container': other}, {'container': '--unrelated'}]))
        self.assertNotEqual(code, 0)
        self.assertEqual(calls.call_count, 3)
        self.assertEqual(summary['cleanup']['containers'], [self.container, other])
        self.assertEqual(summary['cleanup']['ownership'], 'unknown')
        self.assertFalse(summary['cleanup']['ok'])
        self.assertTrue(summary['cleanup']['recovery_required'])
        loops.assert_not_called()

    def test_incomplete_metadata_still_cleans_known_identity(self):
        code, summary, calls, loops = self.invoke(response=json.dumps([
            {'container': self.container}]))
        self.assertNotEqual(code, 0)
        self.assertEqual(summary['primary_outcome']['stage'], 'start')
        self.assertEqual(summary['cleanup']['ownership'], 'known')
        self.assertTrue(summary['cleanup']['ok'])
        self.assertEqual(calls.call_args.args[0][1:], ['stop', '--container', self.container])
        loops.assert_not_called()

    def test_unparseable_start_does_not_stop_unrelated_containers(self):
        code, summary, calls, loops = self.invoke(response='broken {container: arbitrary}')
        self.assertNotEqual(code, 0)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(summary['cleanup']['ownership'], 'unknown')
        self.assertFalse(summary['cleanup']['ok'])
        loops.assert_not_called()

    def test_timeout_unknown_even_with_recovered_container(self):
        for stdout in ('', json.dumps([self.slot])):
            with self.subTest(stdout=stdout):
                self.run_dir = self.root / ('timeout' + str(len(stdout)))
                self.argv[-1] = str(self.run_dir)
                code, summary, calls, loops = self.invoke(start_error=subprocess.TimeoutExpired(
                    'start', 120, output=stdout.encode()))
                self.assertNotEqual(code, 0)
                self.assertEqual(summary['primary_outcome']['error_class'], 'TimeoutExpired')
                self.assertEqual(summary['cleanup']['ownership'], 'unknown')
                self.assertFalse(summary['cleanup']['ok'])
                self.assertEqual(calls.call_count, 2 if stdout else 1)
                loops.assert_not_called()

    def test_seed_mismatch_stops_before_loop(self):
        self.slot['ready_line'] = 'HOLDINGS_READY 7 0'
        code, summary, _, loops = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertTrue(summary['cleanup']['ok'])
        loops.assert_not_called()

    def test_loop_and_cleanup_failure_keep_primary_failure(self):
        code, summary, _, _ = self.invoke(loop_error=ValueError('primary'),
                                          stop_error=OSError('secondary'))
        self.assertNotEqual(code, 0)
        self.assertEqual(summary['primary_outcome']['error_class'], 'ValueError')
        self.assertEqual(summary['primary_outcome']['stage'], 'loop')
        self.assertEqual(summary['cleanup']['results'][0]['error_class'], 'OSError')
        self.assertFalse(summary['cleanup']['ok'])

    def test_terminal_evidence_survives_exception_after_stdout(self):
        def failed_loop(argv):
            print(json.dumps(self.terminal))
            raise RuntimeError('failure after terminal output')
        with patch.object(cu_live.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, stdout=json.dumps([self.slot])),
                subprocess.CompletedProcess([], 0, stdout='{}')]) as calls, \
             patch.object(cu_live, 'loop_main', side_effect=failed_loop), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(cu_live.main(self.argv), 0)
        summary = json.loads((self.run_dir / 'summary.json').read_text())
        self.assertEqual(summary['terminal_summary'], self.terminal)
        self.assertEqual(summary['primary_outcome']['error_class'], 'RuntimeError')
        self.assertTrue(summary['model_claimed_done'])
        self.assertFalse(summary['transport_completed'])
        self.assertEqual(summary['goal_evidence'], 'not_reached')
        self.assertTrue(summary['cleanup']['ok'])
        self.assertEqual(calls.call_args.args[0][1:], ['stop', '--container', self.container])

    def test_stop_failure_cannot_pass(self):
        code, summary, _, _ = self.invoke(stop_code=4)
        self.assertNotEqual(code, 0)
        self.assertEqual(summary['primary_outcome']['exit_code'], 0)
        self.assertFalse(summary['cleanup']['ok'])
        self.assertTrue(summary['cleanup']['recovery_required'])

    def test_immediate_done_is_unverified(self):
        _, summary, _, _ = self.invoke(terminal={'stop_reason': 'done'})
        self.assertTrue(summary['model_claimed_done'])
        self.assertEqual(summary['goal_evidence'], 'not_reached')
        self.assertNotIn('goal_completed', summary)

    def test_budget_stop_cannot_pass_despite_zero_loop_exit(self):
        code, summary, _, _ = self.invoke(terminal={'stop_reason': 'max_ms'})
        self.assertNotEqual(code, 0)
        self.assertFalse(summary['transport_completed'])
        self.assertFalse(summary['model_claimed_done'])

    def task_args(self):
        task = dict(task_id='inbox-test', goal='Open inbox', scenario='inbox-driven',
                    seed=1001, max_actions=8, success_predicate='route == "inbox"')
        tasks = self.root / 'tasks.jsonl'
        tasks.write_text(json.dumps(task) + '\n')
        self.argv = ['--app', str(self.app), '--task', 'inbox-test',
                     '--tasks-file', str(tasks), '--ve-root', str(self.root),
                     '--muse-workspace', str(self.root), '--run-dir', str(self.run_dir)]
        return task

    def test_task_defaults_prompt_hook_and_verified_summary(self):
        task = self.task_args()
        terminal = dict(stop_reason='goal_reached', goal_verified=True, goal_evidence='verified')
        code, summary, calls, loops = self.invoke(terminal=terminal)
        self.assertEqual(code, 0)
        command = loops.call_args.args[0]
        self.assertEqual(command[command.index('--max-steps') + 1], '8')
        self.assertEqual(command[command.index('--goal') + 1],
                         'Open inbox\nsuccess means: (route is inbox)')
        hook = loops.call_args.kwargs['goal_check']
        self.assertTrue(hook({'lane': {'route': 'inbox'}}))
        self.assertFalse(hook({'lane': {'route': 'people'}}))
        self.assertIsNone(hook({'lane': {}}))
        self.assertEqual(summary['effective']['scenario'], task['scenario'])
        self.assertEqual(summary['effective']['seed'], task['seed'])
        self.assertEqual(summary['effective']['goal'], task['goal'])
        self.assertEqual(summary['goal_evidence'], 'verified')
        self.assertTrue(summary['goal_verified'])
        self.assertTrue(summary['transport_completed'])
        self.assertFalse(summary['model_claimed_done'])
        self.assertIn('inbox-driven', calls.call_args_list[0].args[0])

    def test_task_explicit_overrides_and_false_summary(self):
        self.task_args()
        self.argv += ['--goal', 'Custom goal', '--seed', '7', '--scenario', 'custom',
                      '--max-steps', '3']
        self.slot['ready_line'] = 'HOLDINGS_READY 7 0'
        code, summary, calls, loops = self.invoke(terminal=dict(stop_reason='done', goal_verified=False))
        self.assertEqual(code, 0)
        command = loops.call_args.args[0]
        self.assertEqual(command[command.index('--goal') + 1],
                         'Custom goal\nsuccess means: (route is inbox)')
        self.assertEqual(command[command.index('--max-steps') + 1], '3')
        self.assertEqual(summary['effective']['scenario'], 'custom')
        self.assertEqual(summary['effective']['seed'], 7)
        self.assertEqual(summary['goal_evidence'], 'not_reached')
        self.assertFalse(summary['goal_verified'])
        self.assertIn('custom', calls.call_args_list[0].args[0])

    def test_invalid_task_fails_before_launch(self):
        self.task_args()
        self.argv[self.argv.index('--task') + 1] = 'missing-task'
        code, summary, calls, loops = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(summary['primary_outcome']['stage'], 'preflight')
        self.assertEqual(summary['goal_evidence'], 'not_reached')
        calls.assert_not_called()
        loops.assert_not_called()

    def test_reused_directory_preserves_previous_episode(self):
        self.invoke()
        previous = (self.run_dir / 'summary.json').read_bytes()
        with patch.object(cu_live.subprocess, 'run') as calls, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(cu_live.main(self.argv), 0)
        calls.assert_not_called()
        self.assertEqual((self.run_dir / 'summary.json').read_bytes(), previous)

    def test_missing_resolution_never_launches(self):
        self.argv[1] = str(self.root / 'missing')
        code, summary, calls, loops = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertEqual(summary['primary_outcome']['stage'], 'preflight')
        calls.assert_not_called()
        loops.assert_not_called()


    def test_no_progress_flag_forwarded(self):
        self.argv += ['--no-progress-steps', '5']
        code, summary, calls, loops = self.invoke()
        command = loops.call_args.args[0]
        self.assertEqual(command[command.index('--no-progress-steps') + 1], '5')

    def test_predicate_distance_and_holdings_hint_wired(self):
        task = self.task_args()
        task['task_id'] = 'hb_tf_a-07-v1'
        (self.root / 'tasks.jsonl').write_text(json.dumps(task) + '\n')
        self.argv[self.argv.index('--task') + 1] = task['task_id']
        code, summary, calls, loops = self.invoke()
        command = loops.call_args.args[0]
        goal = command[command.index('--goal') + 1]
        self.assertIn('Task-family hint:', goal)
        self.assertIn("opening a message is 'Mark read'", goal)
        self.assertIn('only do it when the goal asks to acknowledge/confirm/clear/handle', goal)
        distance = loops.call_args.kwargs['goal_distance']
        self.assertEqual(distance({'lane': {'route': 'home'}}), 1)
        self.assertEqual(distance({'lane': {'route': 'inbox'}}), 0)
        self.assertIsNone(distance({'lane': {}}))


if __name__ == '__main__':
    unittest.main()
