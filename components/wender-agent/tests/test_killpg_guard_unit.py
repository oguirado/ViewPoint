import ast
import os
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class TestKillpgGuard(unittest.TestCase):
    def test_every_killpg_is_guarded(self):
        # Each killpg must sit under a guard that proves an int pid > 1 and not our own group (incident W7).
        missing = []
        for path in sorted((ROOT / 'wender').rglob('*.py')):
            source = path.read_text(encoding='utf-8')
            for func in ast.walk(ast.parse(source)):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = ast.get_source_segment(source, func) or ''
                for node in ast.walk(func):
                    if isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'killpg':
                        if not ('> 1' in body and 'getpgid(0)' in body):
                            missing.append('%s:%d' % (path.relative_to(ROOT), node.lineno))
        self.assertEqual(missing, [])

    def test_mocked_pid_never_signals(self):
        # Must FAIL fixture: a MagicMock pid coerces to 1; the package audit hook refuses kill(-1) equivalents.
        for call in (lambda: os.killpg(mock.MagicMock(), 9), lambda: os.killpg(1, 9), lambda: os.kill(-1, 9),
                     lambda: os.kill(0, 9), lambda: os.killpg(os.getpgid(0), 9)):
            with self.assertRaises(PermissionError):
                call()


if __name__ == '__main__':
    unittest.main()
