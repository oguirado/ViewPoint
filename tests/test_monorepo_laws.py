"""Real commit-graph negative controls for the accepted import boundary."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import check_laws as laws


class OwnedHistoryTests(unittest.TestCase):
    def test_forbidden_new_commit_must_fail_and_unrelated_refs_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', laws.NAME)
            git('config', 'user.email', laws.EMAIL)
            git('config', 'commit.gpgsign', 'false')
            git('commit', '--allow-empty', '-qm', 'accepted legacy import')
            base = git('rev-parse', 'HEAD')
            git('tag', 'v0.2.6')
            baseline = {'schema': 'viewpoint.laws-baseline.v1',
                        'release_tag': 'v0.2.6', 'release': base,
                        'component_pins': dict.fromkeys(
                            ('wender-agent', 'vision-engine', 'opensuperficies'), base),
                        'accepted_import_merges': [base, base, base]}
            (root / 'LAWS_BASELINE.json').write_text(json.dumps(baseline))
            git('checkout', '-qb', 'unrelated')
            git('commit', '--allow-empty', '-qm', 'invalid unrelated message')
            git('checkout', '-q', '-')
            with patch.object(laws, 'git', side_effect=git):
                self.assertEqual(laws.check_history(root=root, owned=True), [])
                git('commit', '--allow-empty', '-qm', 'ci: add owned verification')
                self.assertEqual(laws.check_history(root=root, owned=True), [])
                message = 'ci: add forbidden witness\n\n' + 'Generated ' + 'with tooling'
                git('commit', '--allow-empty', '-qm', message)
                bad = git('rev-parse', 'HEAD')
                errors = laws.check_history(root=root, owned=True)
                self.assertTrue(any(error.startswith(bad + ':') for error in errors), errors)
            baseline['release'] = 'f' * 40
            (root / 'LAWS_BASELINE.json').write_text(json.dumps(baseline))
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                laws.owned_revisions(root)
            (root / 'LAWS_BASELINE.json').unlink()
            with self.assertRaises(FileNotFoundError):
                laws.owned_revisions(root)
