"""Pinned subtree admission retains byte, mode and untracked-payload refusals."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from metaviewer_agents.perception import deps_lock


class SubtreeTests(unittest.TestCase):
    def test_subtree_pin_and_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args],
                                               text=True, stderr=subprocess.PIPE).strip()
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('config', 'commit.gpgsign', 'false')
            (repo / 'payload.py').write_text('VALUE = 1\n')
            git('add', 'payload.py')
            git('commit', '-qm', 'pin')
            pin = git('rev-parse', 'HEAD')
            component = repo / 'components/wender-agent'
            component.mkdir(parents=True)
            (repo / 'payload.py').rename(component / 'payload.py')
            git('add', '-A')
            git('commit', '-qm', 'import')
            with patch.object(deps_lock, '_LOCK_PATH', repo / 'deps.lock'):
                check = lambda: deps_lock._verify_monorepo_subtree(component, pin, 'wender-agent')
                self.assertTrue(check())
                (component / 'payload.py').write_text('VALUE = 2\n')
                with self.assertRaises(deps_lock.DependencyLockError):
                    check()
                (component / 'payload.py').write_text('VALUE = 1\n')
                (component / 'extra.py').write_text('pass\n')
                with self.assertRaises(deps_lock.DependencyLockError):
                    check()
                (component / 'extra.py').unlink()
                git('update-index', '--assume-unchanged', 'components/wender-agent/payload.py')
                with self.assertRaises(deps_lock.DependencyLockError):
                    check()
                git('update-index', '--no-assume-unchanged', 'components/wender-agent/payload.py')
                (component / 'payload.py').chmod(0o755)
                with self.assertRaises(deps_lock.DependencyLockError):
                    check()
                (component / 'payload.py').chmod(0o644)
                git('add', 'components/wender-agent/payload.py')
                git('commit', '--allow-empty', '-qm', 'next')
                self.assertTrue(check())
