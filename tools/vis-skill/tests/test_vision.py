"""Rename regressions. Run only through campaign g1/safe_run.sh."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(os.environ.get('VISION_TEST_SOURCE', Path(__file__).resolve().parents[1])).resolve()


def snapshot(root):
    return {str(p.relative_to(root)): ('link', os.readlink(p)) if p.is_symlink()
            else ('dir',) if p.is_dir() else ('file', p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*')}


class InstallFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='vision-install-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        for name in ('vis', 'vis.json', 'install.sh', 'skill_copies.py'):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, self.repo / name)
        (self.repo / 'vision').symlink_to('vis')
        for name in ('skill', 'dist'):
            shutil.copytree(ROOT / name, self.repo / name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home))
        for key in ('VISION_INSTALL_OK', 'VIS_INSTALL_OK'):
            self.env.pop(key, None)
        self.targets = [self.repo / 'dist' / host / 'vision' for host in ('claude', 'codex')]
        self.targets += [self.home / name / 'skills/vision' for name in ('.claude', '.agents')]

    def install(self, *args, authorized=True, env=None):
        e = dict(self.env)
        if authorized:
            e['VISION_INSTALL_OK'] = '1'
        if env:
            e.update(env)
        return subprocess.run([str(self.repo / 'install.sh'), *args], env=e,
                              capture_output=True, text=True, timeout=60)


class InstallerTest(InstallFixture):
    def test_dry_run_no_writes(self):
        (self.targets[0] / 'references').mkdir(parents=True, exist_ok=True)
        orphan = self.targets[0] / 'references/orphan'
        orphan.write_text('old')
        before = snapshot(self.base)
        r = self.install('--dry-run', authorized=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(snapshot(self.base), before)
        self.assertIn('DELETE ' + str(orphan), r.stdout)
        self.assertIn('WRITE ' + str(self.targets[-1] / 'SKILL.md'), r.stdout)

    def test_variable_guard(self):
        before = snapshot(self.base)
        r = self.install('--apply', authorized=False)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(snapshot(self.base), before)

    def test_apply_idempotent_and_legacy_variable(self):
        r = self.install('--apply', authorized=False, env={'VIS_INSTALL_OK': '1'})
        self.assertEqual(r.returncode, 0, r.stderr)
        for dest in self.targets:
            text = (dest / 'SKILL.md').read_text()
            self.assertIn('name: vision\n', text)
            self.assertIn(str(self.repo / 'vision'), text)
            self.assertNotIn('@SOURCE_SHA@', text)
        before = snapshot(self.base)
        r = self.install('--apply')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(snapshot(self.base), before)
        self.assertNotIn('WRITE ', r.stdout)

    def test_orphan_scope(self):
        for dest in self.targets:
            (dest / 'obsolete/nested').mkdir(parents=True, exist_ok=True)
            (dest / 'obsolete/nested/file').write_text('orphan')
            (dest.parent / 'outside').write_text('keep')
        r = self.install('--apply')
        self.assertEqual(r.returncode, 0, r.stderr)
        for dest in self.targets:
            self.assertFalse((dest / 'obsolete').exists())
            self.assertTrue((dest.parent / 'outside').is_file(), 'cleanup escaped the vision directory')
            self.assertEqual((dest.parent / 'outside').read_text(), 'keep')

    def test_legacy_untouched(self):
        legacy = [p.parent / 'vis' for p in self.targets]
        for p in legacy:
            p.mkdir(parents=True, exist_ok=True)
            (p / 'sentinel').write_bytes(b'legacy\x00copy')
        before = [snapshot(p) for p in legacy]
        r = self.install('--apply')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([snapshot(p) for p in legacy], before)

    def test_inside_checkout_guard(self):
        home = self.repo / 'fake-home'
        home.mkdir()
        before = snapshot(self.base)
        r = self.install('--apply', env={'HOME': str(home)})
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(snapshot(self.base), before)

    def test_linked_target_guard(self):
        for linked in (self.targets[-1], self.home / '.agents', self.targets[0]):
            with self.subTest(linked=linked):
                outside = self.base / 'outside'
                outside.mkdir(exist_ok=True)
                linked.parent.mkdir(parents=True, exist_ok=True)
                if linked.exists():
                    shutil.rmtree(linked)
                linked.symlink_to(outside, target_is_directory=True)
                before = snapshot(self.base)
                for mode in ('--dry-run', '--apply'):
                    r = self.install(mode)
                    self.assertEqual(r.returncode, 5, r.stderr)
                    self.assertEqual(snapshot(self.base), before)
                linked.unlink()



class DoctorTest(InstallFixture):
    def doctor(self):
        r = subprocess.run([sys.executable, '-I', str(self.repo / 'vision'), 'doctor', '--skill'],
                           env=self.env, capture_output=True, text=True, timeout=90)
        return r, json.loads(r.stdout)

    def test_installed_copies_and_legacy_warning(self):
        r = self.install('--apply')
        self.assertEqual(r.returncode, 0, r.stderr)
        legacy = self.home / '.agents/skills/vis'
        legacy.mkdir()
        (legacy / 'unchanged').write_text('legacy sentinel')
        before = snapshot(legacy)
        r, d = self.doctor()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(d['drift'])
        self.assertEqual(len(d['skill_copies']), 4)
        self.assertIn(str(legacy), d['legacy_copies'])
        self.assertIn('legacy_skill_copy_present', d['warnings'])
        self.assertEqual(snapshot(legacy), before)

    def test_every_copy_detects_changed_missing_orphan(self):
        self.assertEqual(self.install('--apply').returncode, 0)
        for dest in self.targets:
            for kind in ('changed', 'missing', 'orphan'):
                with self.subTest(dest=dest, kind=kind):
                    p = dest / ('references/surfaces.md' if kind != 'orphan' else 'orphan.md')
                    if kind == 'missing':
                        p.unlink()
                    else:
                        p.write_text('drift without touching the template SHA')
                    r, d = self.doctor()
                    self.assertEqual(r.returncode, 3, r.stdout)
                    self.assertTrue(d['drift'])
                    copy = next(c for c in d['skill_copies'] if c['path'] == str(dest))
                    self.assertIn(str(p.relative_to(dest)), copy[kind])
                    self.assertEqual(self.install('--apply').returncode, 0)
        r, d = self.doctor()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(d['drift'])


class DocsTest(unittest.TestCase):
    def test_rename_and_install_in_public_docs(self):
        for name in ('README.md', 'AGENTS.md', 'docs/CONTRACT.md',
                     'docs/RELEASE_NOTES.md', 'docs/ORCHESTRATOR_GUIDE.md'):
            with self.subTest(name=name):
                p = ROOT / name
                self.assertTrue(p.is_file(), name)
                text = p.read_text()
                self.assertIn('vision (formerly vis)', text)
                self.assertIn('VISION_INSTALL_OK=1 ./install.sh --apply', text)
        text = (ROOT / 'README.md').read_text()
        for value in ('vis executable', '/dev/shm/vqa-cu/vis/',
                      '~/.local/state/vis/receipts/', 'VIS_*', 'vis.json', 'schema ids'):
            self.assertIn(value, text)


class SkillTest(unittest.TestCase):
    def test_skill_identity_and_compatibility(self):
        text = (ROOT / 'skill/SKILL.md.tmpl').read_text()
        self.assertIn('name: vision\n', text)
        description = text.split('description: ', 1)[1].splitlines()[0]
        self.assertIn('vision', description)
        self.assertEqual(description.count('(formerly vis)'), 1)
        self.assertIn('`vision` below means `python3 -I @VISION_PATH@`', text)
        self.assertNotIn('require bc/vis-core', text)
        for ref in (ROOT / 'skill/references').glob('*.md'):
            if ref.name != 'worker_block.md':
                self.assertNotRegex(ref.read_text(), r'(?<![/\w.-])vis(?![/\w.-])')
        for name in ('vis executable', '/dev/shm/vqa-cu/vis/',
                     '~/.local/state/vis/receipts/', 'VIS_*', 'vis.json', 'schema ids'):
            self.assertIn(name, text)
        import hashlib
        self.assertEqual(hashlib.sha256((ROOT / 'skill/references/worker_block.md').read_bytes()).hexdigest(),
                         'b67cf713525e7250e88f36c68ee13a68d550292677d77bada88c677375745aaa')


class VendoredEngineDoctorTest(unittest.TestCase):
    # Negative control: a fresh HOME with plain directories (not git clones)
    # as the engine roots must be reported, never crashed on (rc 9).
    def _plain_engine_dirs(self, base):
        wender = base / 'plain-wender'
        ve = base / 'plain-ve'
        wender.mkdir()
        ve.mkdir()
        (wender / 'vendored-only.txt').write_text('vendored copy, not a git checkout')
        return wender, ve

    def test_doctor_reports_vendored_engines_without_crashing(self):
        with tempfile.TemporaryDirectory(prefix='vision-vendored-home-') as tmp:
            base = Path(tmp)
            home = base / 'home'
            home.mkdir()
            wender, ve = self._plain_engine_dirs(base)
            env = dict(os.environ, HOME=str(home),
                       VIS_WENDER_REPO=str(wender), VIS_VE_REPO=str(ve))
            for name in ('vis', 'vision'):
                with self.subTest(entrypoint=name):
                    r = subprocess.run([sys.executable, '-I', str(ROOT / name), 'doctor'],
                                       env=env, capture_output=True, text=True, timeout=90)
                    self.assertNotEqual(r.returncode, 9, r.stdout)
                    self.assertNotIn('ReadError', r.stdout)
                    self.assertNotIn('Traceback', r.stdout + r.stderr)
                    self.assertNotIn('Traceback', r.stderr)
                    d = json.loads(r.stdout)
                    self.assertNotEqual(d.get('code'), 'E_INTERNAL')
                    self.assertIn('tools', d)
                    self.assertIn('engines', d)
                    finding = str(d.get('engines_unavailable') or d.get('engines', {}).get('reason', '')) + str(d.get('engines', {}))
                    self.assertIn('engine archive unavailable', finding)
                    self.assertIn('VIS_WENDER_REPO', finding)
                    self.assertIn('VIS_VE_REPO', finding)
                    self.assertIn('git clones', finding)
                    self.assertIs(d.get('capabilities', {}).get('engines_available'), False)

    def test_engines_fails_with_clear_message_not_traceback(self):
        import importlib.machinery
        import importlib.util
        with tempfile.TemporaryDirectory(prefix='vision-vendored-eng-') as tmp:
            base = Path(tmp)
            home = base / 'home'
            home.mkdir()
            wender, ve = self._plain_engine_dirs(base)
            loader = importlib.machinery.SourceFileLoader('vis_vendored_test', str(ROOT / 'vis'))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            env = dict(HOME=str(home), VIS_WENDER_REPO=str(wender), VIS_VE_REPO=str(ve))
            with unittest.mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaises(mod.Failure) as caught:
                    mod.engines()
            exc = caught.exception
            self.assertEqual(exc.rc, 7)
            self.assertIn('engine archive unavailable', exc.reason)
            self.assertIn('VIS_WENDER_REPO', exc.reason)
            self.assertIn('VIS_VE_REPO', exc.reason)
            self.assertIn('git clones', exc.reason)
            self.assertNotIn('ReadError', exc.reason)


class AliasTest(unittest.TestCase):
    def test_isolated_entrypoints_match(self):
        with tempfile.TemporaryDirectory(prefix='vision-home-') as tmp:
            env = dict(os.environ, HOME=tmp)
            for args in [('doctor',), ('doctor', '--skill'), ('start',),
                         ('look', '--unknown'), ('act',), ('stop', '--unknown')]:
                with self.subTest(args=args):
                    calls = [subprocess.run([sys.executable, '-I', str(ROOT / name), *args],
                                            env=env, capture_output=True, timeout=90)
                             for name in ('vis', 'vision')]
                    self.assertEqual((calls[0].returncode, calls[0].stdout),
                                     (calls[1].returncode, calls[1].stdout))
                    self.assertIn(calls[0].returncode, (0, 3) if args[0] == 'doctor' else (2,))


if __name__ == '__main__':
    unittest.main()
