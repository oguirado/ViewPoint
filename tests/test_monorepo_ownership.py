"""Ownership changes must preserve root refusals and delegate only declared trees."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.ownership import root_owned_paths
from tools import router_index, route_docs
from test_kill_guard import _fixture_git_index, _first_party, kill_by_name_hits
from test_net_scope import tracked_network_hits
import test_hygiene as hygiene

ROOT = Path(__file__).resolve().parents[1]


class OwnershipTests(unittest.TestCase):
    def fixture(self, root, sources):
        manifest = (ROOT / 'OWNERSHIP.json').read_text()
        _fixture_git_index(root, {'OWNERSHIP.json': manifest, **sources})

    def test_root_violation_must_fail_and_component_is_delegated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = '# p' + 'kill worker\nargv = ' + repr(['n' + 'et', 'do' + 'wn']) + '\n'
            self.fixture(root, {'owned.py': bad,
                                'components/wender-agent/foreign.py': bad})
            self.assertEqual(kill_by_name_hits(root), ['owned.py:1'])
            self.assertEqual(tracked_network_hits(root), ['owned.py:2'])
            self.assertEqual(list(_first_party(root, '.py')), [root / 'owned.py'])
            self.assertNotIn('components/wender-agent/foreign.py', router_index.tracked_paths(root))
            self.assertNotIn('components/wender-agent/foreign.py', route_docs.tracked(root))
            privacy = hygiene.PrivacySweepTests()
            with patch('test_hygiene.REPO_ROOT', root):
                paths = privacy._tracked_files()
                self.assertEqual(privacy._sweep(paths, b'pk' + b'ill'), [str(root / 'owned.py')])
            # The same violating bytes survive only across the component boundary.
            (root / 'owned.py').write_text('pass\n')
            self.assertEqual(kill_by_name_hits(root), [])
            self.assertEqual(tracked_network_hits(root), [])

    def test_component_document_does_not_enter_root_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, {'owned.md': '# Owned\n',
                                'components/vision-engine/foreign.md': '# Foreign\n'})
            self.assertEqual(route_docs.markdown_files(root), ['owned.md'])

    def test_missing_invalid_and_unknown_ownership_must_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = 'components/wender-agent/source.py'
            with self.assertRaises(ValueError):
                root_owned_paths(root, [path])
            for data in ({}, {'schema': 'viewpoint.ownership.v1', 'root': 'ViewPoint',
                             'components': {'tests/': 'wender-agent'}}):
                (root / 'OWNERSHIP.json').write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    root_owned_paths(root, [path])
            self.fixture(root, {})
            with self.assertRaisesRegex(ValueError, 'undeclared component'):
                root_owned_paths(root, ['components/new/source.py'])

    def test_root_symlink_refusal_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, {'owned.py': 'pass\n'})
            (root / 'owned.py').unlink()
            (root / 'owned.py').symlink_to('OWNERSHIP.json')
            with self.assertRaisesRegex(router_index.RouterError, 'symlink refused'):
                router_index._safe_read(root, 'owned.py')
