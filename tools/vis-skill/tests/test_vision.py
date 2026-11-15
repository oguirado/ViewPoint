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
    # Fresh-HOME controls for vendored (non-git) engine roots. Doctor is a
    # read-only capability manifest: it reports engines_unavailable
    # structurally (engines.available=false) and exits 0 — the same way
    # session_runtime missing is reported without failing. Security refusals
    # (rc 5) must still fail closed instead of being downgraded.
    DESKTOP_TOOLS = ('Xvfb', 'xdotool', 'dbus-run-session', 'openbox',
                     'xprop', 'ffmpeg', 'google-chrome', 'systemctl',
                     'systemd-run')

    def _plain_engine_dirs(self, base):
        wender = base / 'plain-wender'
        ve = base / 'plain-ve'
        wender.mkdir()
        ve.mkdir()
        (wender / 'vendored-only.txt').write_text('vendored copy, not a git checkout')
        return wender, ve

    def _tool_stubs(self, base):
        # Locate desktop tools via stub executables first on PATH so the
        # result never depends on the host's installed tools.
        stubs = base / 'stubs'
        stubs.mkdir()
        for tool in self.DESKTOP_TOOLS:
            p = stubs / tool
            p.write_text('#!/bin/sh\nexit 0\n')
            p.chmod(0o755)
        return stubs

    def _env(self, base, home, wender, ve):
        stubs = self._tool_stubs(base)
        return dict(os.environ, HOME=str(home),
                    VIS_WENDER_REPO=str(wender), VIS_VE_REPO=str(ve),
                    PATH=str(stubs) + os.pathsep + os.environ.get('PATH', ''))

    def _doctor(self, env, entrypoint='vis', extra=()):
        return subprocess.run([sys.executable, '-I', str(ROOT / entrypoint), 'doctor', *extra],
                              env=env, capture_output=True, text=True, timeout=90)

    def _git_repo(self, base, name, files):
        repo = base / name
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], env=dict(os.environ, HOME=str(base / 'git-home')))
        for rel, content in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                        'commit', '-qm', 'fixture'], check=True)
        sha = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                             capture_output=True, text=True, check=True).stdout.strip()
        return repo, sha

    def test_doctor_reports_vendored_engines_without_crashing(self):
        with tempfile.TemporaryDirectory(prefix='vision-vendored-home-') as tmp:
            base = Path(tmp)
            home = base / 'home'
            home.mkdir()
            wender, ve = self._plain_engine_dirs(base)
            env = self._env(base, home, wender, ve)
            for name in ('vis', 'vision'):
                with self.subTest(entrypoint=name):
                    r = self._doctor(env, name)
                    self.assertEqual(r.returncode, 0, r.stdout)
                    self.assertNotIn('Traceback', r.stdout + r.stderr)
                    self.assertNotIn('ReadError', r.stdout)
                    d = json.loads(r.stdout)
                    self.assertEqual(d['rc'], 0)
                    self.assertEqual(d['code'], 'OK')
                    self.assertIn('tools', d)
                    self.assertIn('engines', d)
                    # engines() aborts on wender, so the message names the
                    # wender root specifically; the hint names both variables.
                    self.assertIs(d['engines']['available'], False)
                    unavailable = d['engines_unavailable']
                    self.assertIn('engine archive unavailable', unavailable)
                    self.assertIn('VIS_WENDER_REPO', unavailable)
                    self.assertIn(str(wender), unavailable)
                    self.assertIn('git clones', unavailable)
                    self.assertIs(d['capabilities']['engines_available'], False)

    def test_doctor_unavailable_inside_git_repo_lacking_pin(self):
        # A plain vendored subdirectory inside a real git repo resolves
        # `git -C` to that repo, which lacks the pinned commit: the actual
        # clean-checkout failure mode.
        with tempfile.TemporaryDirectory(prefix='vision-mono-') as tmp:
            base = Path(tmp)
            home = base / 'home'
            home.mkdir()
            (base / 'git-home').mkdir()
            repo, _ = self._git_repo(base, 'mono', {'README': 'monorepo'})
            wender = repo / 'wender-vendored'
            wender.mkdir()
            (wender / 'vendored-only.txt').write_text('vendored copy inside a git repo')
            env = self._env(base, home, wender, wender)
            r = self._doctor(env)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertNotIn('Traceback', r.stdout + r.stderr)
            d = json.loads(r.stdout)
            self.assertIs(d['engines']['available'], False)
            self.assertIn('engine archive unavailable', d['engines_unavailable'])
            self.assertIn(str(wender), d['engines_unavailable'])
            self.assertIn('pinned commit', d['engines_unavailable'])
            self.assertIn('git clones', d['engines_unavailable'])
            self.assertIs(d['capabilities']['engines_available'], False)

    def test_doctor_positive_control_with_pinned_git_repos(self):
        with tempfile.TemporaryDirectory(prefix='vision-pinned-') as tmp:
            base = Path(tmp)
            home = base / 'home'
            home.mkdir()
            (base / 'git-home').mkdir()
            wender_repo, wender_sha = self._git_repo(base, 'wender', {'wender/act/hosted_guard.py': ''})
            ve_repo, ve_sha = self._git_repo(base, 've', {
                'vadapt/__init__.py': '',
                'vadapt/vserve.py': '',
                'vadapt/cu_describe.py': "print('--frame-raw')\n",
            })
            target = base / 'target'
            target.mkdir()
            (target / 'deps.lock').write_text(json.dumps({
                'dependencies': {
                    'wender-agent': {'commit': wender_sha},
                    'vision-engine': {'commit': ve_sha},
                }}))
            env = self._env(base, home, wender_repo, ve_repo)
            r = self._doctor(env, extra=('--target-repo', str(target)))
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertNotIn('Traceback', r.stdout + r.stderr)
            d = json.loads(r.stdout)
            self.assertIsNone(d['engines_unavailable'])
            self.assertIs(d['capabilities']['engines_available'], True)
            self.assertTrue(d['capabilities']['frame_raw'])
            self.assertTrue(d['capabilities']['vserve'])
            self.assertTrue(d['capabilities']['hosted_guard'])
            self.assertTrue(d['engines']['wender'].endswith('wender-' + wender_sha))
            self.assertTrue(d['engines']['ve'].endswith('ve-' + ve_sha))
            # Extracted engines are read-only; make the tree writable again
            # so TemporaryDirectory cleanup can remove it.
            for p in home.rglob('*'):
                p.chmod(0o755 if p.is_dir() else 0o644)

    def test_doctor_rc5_security_refusal_still_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix='vision-symlink-') as tmp:
            base = Path(tmp)
            home = base / 'home'
            home.mkdir()
            (base / 'git-home').mkdir()
            wender, ve = self._plain_engine_dirs(base)
            cache = home / '.cache' / 'vis'
            cache.mkdir(parents=True)
            pin = json.loads((ROOT / 'vis.json').read_text())['defaults']['wender_ref']
            (cache / ('wender-' + pin)).symlink_to(base)
            env = self._env(base, home, wender, ve)
            r = self._doctor(env)
            self.assertEqual(r.returncode, 5, r.stdout)
            self.assertNotIn('Traceback', r.stdout + r.stderr)
            d = json.loads(r.stdout)
            self.assertEqual(d['rc'], 5)
            self.assertEqual(d['code'], 'E_RULE')
            self.assertIn('symlink engine cache refused', d['reason'])

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
            self.assertIn(str(wender), exc.reason)
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
