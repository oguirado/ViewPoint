"""Unit-name authority and the component-wide static safety gate."""
import ast
import os
from pathlib import Path
import signal
import unittest
from unittest import mock
from asset_factory import proc
from systemd_fake import FakeRunner

def launch(runner=None, **kwargs):
    return proc.popen(['/usr/bin/true'], runner=runner or FakeRunner(),
        start_new_session=True, memory_limits=proc.MemoryLimits(1024**3, 2*1024**3, 0), **kwargs)

class ProcessSafety(unittest.TestCase):
    def test_invalid_targets_never_reach_manager(self):
        class Indexable:
            def __index__(self):
                raise AssertionError('must not coerce')
        for value in (True, False, 0, 1, -1, os.getpid(), os.getpgrp(),
                      123456, mock.MagicMock(), Indexable(), 'af-job-'+'a'*32+'.service'):
            for method in (proc.signal_group, proc.signal_pid):
                with self.subTest(value=repr(value), method=method.__name__):
                    with self.assertRaises(proc.SignalRefusal):
                        method(value, signal.SIGKILL)

    def test_composition_has_no_numeric_or_inherited_stop_surface(self):
        job = launch()
        self.assertNotIsInstance(job, proc.subprocess.Popen)
        for name in ('pid', 'send_signal', 'terminate', 'kill', '_popen'):
            self.assertFalse(hasattr(job, name), name)
        job.stop()

    def test_missing_manager_is_typed_before_any_job(self):
        runner = FakeRunner(available=False)
        with self.assertRaises(proc.SignalRefusal) as error:
            launch(runner)
        self.assertEqual(error.exception.code, 'systemd_user_manager_unavailable')
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.units, {})

    def test_no_session_no_spawn_and_no_retrofit(self):
        with self.assertRaises(proc.SignalRefusal) as error:
            proc.popen(['never'])
        self.assertEqual(error.exception.code, 'group_session_required')
        with self.assertRaises(proc.SignalRefusal):
            proc.register_group(mock.Mock(), start_new_session=True)

    def test_unit_validation_before_control_operations(self):
        runner = FakeRunner()
        for unit in ('other.service', 'af-job-x.service', 'af-job-'+'A'*32+'.service',
                     'af-job-'+'a'*32+'.service extra', '--all', '../af-job-'+ 'a'*32+'.service'):
            with self.assertRaises(proc.SignalRefusal):
                proc._unit_call(runner, 'stop', unit)
        self.assertEqual(runner.calls, [])

    def test_forged_handle_is_refused(self):
        with self.assertRaises(proc.SignalRefusal):
            proc.Job('af-job-'+'a'*32+'.service', FakeRunner())

    def test_inherited_handle_cannot_observe_or_stop(self):
        runner = FakeRunner()
        job = launch(runner)
        count = len(runner.calls)
        with mock.patch.object(proc.os, 'getpid', return_value=os.getpid()+100000):
            for method in (job.wait, job.stop, lambda: proc.poll_exit(job)):
                with self.assertRaises(proc.SignalRefusal) as error:
                    method()
                self.assertEqual(error.exception.code, 'group_not_owned')
        self.assertEqual(len(runner.calls), count)
        job.stop()

    @staticmethod
    def raw_signal_sites(source, path):
        sites = []
        descriptor_backend = str(path) == 'asset_factory/proc_pidns.py'
        forbidden = ('os.kill', 'cgroup.kill') + (() if descriptor_backend else ('signal.pidfd_send_signal',))
        for literal in forbidden:
            if literal in source:
                sites.append(f'{path}:{literal}')
        if not str(path).endswith('.py'):
            return sites
        tree = ast.parse(source)
        aliases = {'os': 'os', 'signal': 'signal'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in aliases:
                        aliases[alias.asname or alias.name] = alias.name
            if isinstance(node, ast.ImportFrom) and node.module in ('os', 'signal'):
                if any(a.name in ('kill', 'killpg', 'pidfd_send_signal') for a in node.names):
                    sites.append(f'{path}:signal_import')
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if aliases.get(node.value.id) == 'os' and node.attr in ('kill', 'killpg'):
                    sites.append(f'{path}:{node.lineno}:raw_signal')
                if aliases.get(node.value.id) == 'signal' and node.attr == 'pidfd_send_signal' and not descriptor_backend:
                    sites.append(f'{path}:{node.lineno}:raw_signal')
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('getattr', 'eval', 'exec'):
                if any(isinstance(n, ast.Constant) and isinstance(n.value, str) and
                       any(x in n.value for x in ('killpg', 'pidfd_send_signal', 'cgroup.kill'))
                       for n in ast.walk(node)):
                    sites.append(f'{path}:{node.lineno}:dynamic_signal')
        return sites

    def test_static_scan_rejects_raw_signals_everywhere(self):
        root = Path(proc.__file__).resolve().parents[1]
        violations = []
        for path in sorted(root.rglob('*')):
            if not path.is_file() or path == Path(__file__).resolve() or '__pycache__' in path.parts:
                continue
            try:
                source = path.read_text()
            except UnicodeError:
                continue
            violations.extend(self.raw_signal_sites(source, path.relative_to(root)))
        self.assertEqual(violations, [])

    def test_static_gate_negative_controls(self):
        for source in ('import os\nos.kill(1,9)', 'import os as x\nx.killpg(1,9)',
                       "getattr(os, 'killpg')(1,9)", 'eval("os.killpg(1,9)")',
                       'exec("os.killpg(1,9)")', "open('cgroup.kill','w')",
                       'signal.pidfd_send_signal(2,9)'):
            with self.subTest(source=source):
                self.assertTrue(self.raw_signal_sites(source, 'mutant.py'))

    def test_descriptor_delivery_is_allowed_only_in_namespace_module(self):
        source = 'import signal\nsignal.pidfd_send_signal(fd, 9)'
        self.assertEqual(self.raw_signal_sites(source, 'asset_factory/proc_pidns.py'), [])
        self.assertTrue(self.raw_signal_sites(source, 'asset_factory/proc.py'))
        self.assertTrue(self.raw_signal_sites('import os\nos.killpg(2,9)', 'asset_factory/proc_pidns.py'))

if __name__ == '__main__':
    unittest.main()
