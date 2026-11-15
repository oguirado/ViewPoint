# @module {"id":"wa.tests.mw_profiles_unit","layer":4,"owner":"LEARN","deps":["wa.learn.mw_profiles","wa.tests.test_mw_handlers_unit","wa.tools.mw_run","wa.tools.mw_profile_init","wa.tests.mw_live_evidence_unit","wa.tests.mw_owners_srf_unit"],"book":"HB-WA-s4-learn"}
"""Public profile controls with synthetic private text and RAM-only transports.
All numeric fixture bounds are [TARGET]. No actual identity or secret is read.
"""
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
from dataclasses import replace
import ast
import copy
import inspect
import textwrap
import hashlib
import subprocess
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import types
import traceback
import unittest
from unittest.mock import MagicMock, patch

from tools import mw_run as cli
from tools import mw_profile_init as initializer
from tests import test_mw_live_evidence_unit as live
from tests import test_mw_owners_srf_unit as composition
from wender.learn import mw_profiles as m
from wender.learn import mw_owners_srf as owners


_REAL_NOTICE_PATH = Path.home() / '.local/state/memoryweb/notices.jsonl'
_REAL_PROBE = m._producer_evidence
_REAL_POPEN = subprocess.Popen
_REAL_LIVE_CONFIGURATION = live.m.LiveEvidenceOwner._configuration


# @anchor:wa.s4.mw_profiles._notice_state
def _notice_state(path):
    states = []
    for target in (path, Path('/dev/shm/vqa-leases'), Path('/dev/shm/vqa-cu')):
        try:
            info = target.stat()
            listing = tuple(sorted(item.name for item in target.iterdir())) if target.is_dir() else None
            states.append((True, info.st_size, info.st_mtime_ns, listing))
        except FileNotFoundError:
            states.append((False, None, None, None))
    return tuple(states)


# @anchor:wa.s4.mw_profiles._notice_guard
def _notice_guard(case, before):
    case.assertEqual(_notice_state(_REAL_NOTICE_PATH), before, 'real notice journal changed')


# @anchor:wa.s4.mw_profiles._isolated_live_setup
def _isolated_live_setup(fixture):
    """Reuse live evidence fixtures without touching the production RAM root."""
    temporary = tempfile.TemporaryDirectory
    root = temporary(dir='/dev/shm')
    fixture.addCleanup(root.cleanup)
    def directory(*args, **kwargs):
        if kwargs.get('dir') == '/dev/shm/vqa-cu':
            kwargs['dir'] = root.name
        return temporary(*args, **kwargs)
    # The frozen evidence fixture has a literal root, so bind its configuration
    # check to private fixture and provisioning roots for the fixture lifetime.
    # Every other frozen configuration check remains intact.
    import textwrap
    source = textwrap.dedent(inspect.getsource(_REAL_LIVE_CONFIGURATION))
    expected = "self.workspace.is_relative_to('/dev/shm/vqa-cu')"
    assert expected in source
    namespace = dict(vars(live.m), _profile_module=m)
    exec(compile(source.replace(expected, '(self.workspace.is_relative_to(' + repr(root.name) + ') or self.workspace.is_relative_to(_profile_module._RAM_ROOT))'),
                 '<isolated-fixture-root>', 'exec'), namespace)
    configuration_patch = patch.object(live.m.LiveEvidenceOwner, '_configuration', namespace['_configuration'])
    configuration_patch.start()
    fixture.addCleanup(configuration_patch.stop)
    with patch.object(live, 'Path', side_effect=lambda value: Path(root.name) if str(value) == '/dev/shm/vqa-cu' else Path(value)), \
         patch.object(tempfile, 'TemporaryDirectory', side_effect=directory):
        fixture.setUp()


# @anchor:wa.s4.mw_profiles.TestProfiles
class TestProfiles(unittest.TestCase):
    # @anchor:wa.s4.mw_profiles.TestProfiles.setUp
    def setUp(self):
        before = _notice_state(_REAL_NOTICE_PATH)
        self.addCleanup(_notice_guard, self, before)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.identity = self.root / 'identity.env'
        self.ack = self.root / 'ack.txt'
        self.profile = self.root / 'site.json'
        self.export = self.root / 'export'
        self.export.mkdir(mode=0o700)
        self._private(self.identity, b'SYNTHETIC_NOT_PARSED')
        self.token = b'SYNTHETIC_ACK' + bytes(range(32))
        self._private(self.ack, self.token)
        self.value = dict(capsule='synthetic', container='synthetic', window='w1',
            identity_env=str(self.identity), announce_gate='trusted_notice_v1',
            live_ack_source=dict(kind='file', path=str(self.ack)), allowed_modes=['replay', 'shadow'],
            export_dir=str(self.export), ack_sha256=hashlib.sha256(self.token).hexdigest())
        self._write()
        # Any loader/usage rejection must precede the entire composition seam.
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(Path, 'home', return_value=self.root))
        self.stack.enter_context(patch.object(m, '_LEASE_ROOT', self.root / 'leases'))
        self.stack.enter_context(patch.dict(os.environ, VQA_VE_ROOT=''))
        self.stack.enter_context(patch.object(m.shutil, 'which', return_value=None))
        ram = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(ram.cleanup)
        self.stack.enter_context(patch.object(m, '_RAM_ROOT', Path(ram.name)))
        self.builder = self.stack.enter_context(patch.object(m, 'build_live_ports', wraps=m.build_live_ports))
        self.runner = self.stack.enter_context(patch.object(m, 'run_live_workflow', wraps=m.run_live_workflow))
        # inspect.signature must see the frozen APIs even through monitoring mocks.
        self.builder.__signature__ = inspect.signature(owners.build_live_ports)
        self.runner.__signature__ = inspect.signature(live.m.run_live_workflow)
        self.spawn = self.stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('PROCESS_TRAP')))
        # Legacy composition fixtures do not negotiate a real child.
        self.probe = self.stack.enter_context(patch.object(m, '_producer_evidence', return_value=
            m._ProducerEvidence(m.BINARY, None, frozenset(), time.monotonic_ns(), None, False, m.Reason.MISSING)))

    def _private(self, path, raw):
        path.write_bytes(raw)
        path.chmod(0o600)

    def _write(self):
        self._private(self.profile, json.dumps(self.value).encode())

    def _load(self, site='site'):
        return m.load_profile(site, profile_dir=self.root)

    def _invoke(self, profile=None, mode='replay'):
        return m.make_invocation(profile or self._load(), workflow=self.root / 'workflow.json',
                                 output=self.root / 'out', mode=mode)

    # @anchor:wa.s4.mw_profiles.TestProfiles._no_workspace
    @contextmanager
    def _no_workspace(self):
        root = m._RAM_ROOT
        before = set(root.iterdir()) if root.exists() else set()
        mkdir = Path.mkdir
        attempted = []
        def monitored(path, *args, **kwargs):
            if path.parent == root:
                attempted.append(path)
            return mkdir(path, *args, **kwargs)
        with patch.object(Path, 'mkdir', monitored):
            yield
        self.assertEqual(attempted, [], 'workspace creation attempted')
        self.assertEqual(set(root.iterdir()) if root.exists() else set(), before)

    def _reject(self, call, rc=5):
        with self._no_workspace(), patch.object(m, 'SrfIdentitySurface') as surface:
            with self.assertRaises(m.ProfileError) as caught:
                call()
            surface.assert_not_called()
        self.assertFalse((self.root / 'out').exists())
        self.assertEqual(caught.exception.rc, rc)
        self.assertEqual(str(caught.exception), 'USAGE_SCHEMA' if rc == 2 else 'OUT_OF_SUPPORT')
        self.builder.assert_not_called()
        self.runner.assert_not_called()
        self.spawn.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_workspace_guard_mutant
    def test_workspace_guard_mutant(self):
        profile, invocation = self._load(), self._invoke()
        # Positive: missing installation rejects without any mkdir attempt.
        self._reject(lambda: m.run_profile(profile, invocation, bundle={}, services=None))
        @contextmanager
        def allocating(*args, **kwargs):
            workspace = m._RAM_ROOT / 'allocation-mutant'
            workspace.mkdir(mode=0o700)
            workspace.rmdir()
            raise m.ProfileError()
            yield
        with patch.object(m, '_default_profile_services', allocating):
            with self.assertRaisesRegex(AssertionError, 'workspace creation attempted'):
                self._reject(lambda: m.run_profile(profile, invocation, bundle={}, services=None))

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_lookup
    def test_lookup(self):
        for name in ('../site', '/site', 'a/b', 'site\n', '-site', '', 'a' * 65, 'SITE'):
            with self.subTest(name=name):
                self._reject(lambda: self._load(name), 2)
        self.assertEqual(self._load().site, 'site')
        for name in ('a', 'a' * 64, 'a_b-c'):
            self._private(self.root / (name + '.json'), self.profile.read_bytes())
            self.assertEqual(self._load(name).site, name)
        with patch.dict(os.environ, XDG_CONFIG_HOME='IGNORED'), patch.object(Path, 'home', return_value=self.root):
            target = self.root / '.config/memoryweb/profiles'
            target.mkdir(parents=True, mode=0o700)
            (self.root / '.config').chmod(0o700)
            (self.root / '.config/memoryweb').chmod(0o700)
            self._private(target / 'site.json', self.profile.read_bytes())
            self.assertEqual(m.load_profile('site').site, 'site')

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_permissions
    def test_permissions(self):
        for path in (self.profile, self.identity, self.ack):
            for mode in (0o620, 0o602, 0o644, 0o400, 0o1600):
                with self.subTest(path=path.name, mode=mode):
                    path.chmod(mode)
                    self._reject(self._load)
                    path.chmod(0o600)
            self.assertIs(type(self._load()), m.Profile)
        actual = os.fstat
        target_inode = None
        def foreign(fd):
            s = actual(fd)
            if s.st_ino == target_inode:
                return types.SimpleNamespace(**{k: getattr(s, k) for k in ('st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}, st_uid=os.geteuid() + 1)
            return s
        actual_stat = os.stat
        def foreign_stat(*a, **kw):
            result = actual_stat(*a, **kw)
            if result.st_ino == target_inode:
                return types.SimpleNamespace(**{k: getattr(result, k) for k in ('st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}, st_uid=os.geteuid() + 1)
            return result
        for path in (self.profile, self.identity, self.ack):
            target_inode = path.stat().st_ino
            with patch.object(m.os, 'fstat', side_effect=foreign), patch.object(m.os, 'stat', side_effect=foreign_stat):
                self._reject(self._load)
        self.root.chmod(0o777)
        self._reject(self._load)
        self.root.chmod(0o700)
        self.assertIs(type(self._load()), m.Profile)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_symlinks_and_git
    def test_symlinks_and_git(self):
        for path in (self.profile, self.identity, self.ack):
            saved = path.with_suffix('.saved')
            path.rename(saved)
            path.symlink_to(saved)
            self._reject(self._load)
            path.unlink()
            saved.rename(path)
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        self._reject(lambda: m.load_profile('site', profile_dir=link))
        for kind in ('directory', 'file', 'symlink'):
            git = self.root / '.git'
            if kind == 'directory': git.mkdir()
            elif kind == 'file': git.write_text('gitdir: synthetic')
            else: git.symlink_to(self.root / 'missing')
            self._reject(self._load)
            if kind == 'directory': git.rmdir()
            else: git.unlink()
        # Independent ancestors for identity and acknowledgement, including worktree files.
        for key in ('identity_env', 'live_ack_source'):
            tree = self.root / key
            tree.mkdir(mode=0o700)
            leaf = tree / 'private'
            self._private(leaf, b'SYNTHETIC')
            if key == 'identity_env': self.value[key] = str(leaf)
            else: self.value[key]['path'] = str(leaf)
            self._write()
            for directory in (True, False):
                git = tree / '.git'
                if directory: git.mkdir()
                else: git.write_text('gitdir: synthetic')
                self._reject(self._load)
                if directory: git.rmdir()
                else: git.unlink()
            self.assertIs(type(self._load()), m.Profile)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_missing_and_nonregular
    def test_missing_and_nonregular(self):
        for path in (self.profile, self.identity, self.ack):
            raw = path.read_bytes()
            path.unlink()
            self._reject(self._load)
            os.mkfifo(path, 0o600)
            self._reject(self._load)
            path.unlink()
            path.mkdir()
            self._reject(self._load)
            path.rmdir()
            self._private(path, raw)
        self.assertIs(type(self._load()), m.Profile)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_bounds_and_mutation
    def test_bounds_and_mutation(self):
        raw = self.profile.read_bytes()
        self._private(self.profile, raw + b' ' * (16384 - len(raw)))
        self.assertIs(type(self._load()), m.Profile)
        with self.profile.open('ab') as f: f.write(b' ')
        self._reject(self._load)
        self._private(self.profile, raw)
        p = replace(self._load(), ack_sha256=hashlib.sha256(b'a' * 4096).hexdigest())
        self._private(self.ack, b'a' * 4096)
        self.assertEqual(self._invoke(p).live_ack, p.ack_sha256)
        self._private(self.ack, b'a' * 4097)
        self._reject(lambda: self._invoke(p))
        self._private(self.ack, self.token)
        actual = os.read
        changed = False
        def mutate(fd, count):
            nonlocal changed
            result = actual(fd, count)
            if result and not changed:
                changed = True
                with self.profile.open('ab') as f: f.write(b' ')
            return result
        with patch.object(m.os, 'read', side_effect=mutate):
            self._reject(self._load)
        # An existing deep chain ensures removing the ceiling actually admits it.
        deep = self.root
        for _ in range(128 - len(self.root.parts)):
            deep = deep / 'a'
            deep.mkdir(mode=0o700)
        self._private(deep / 'site.json', raw)
        self.assertIs(type(m.load_profile('site', profile_dir=deep)), m.Profile)
        deep = deep / 'a'
        deep.mkdir(mode=0o700)
        self._private(deep / 'site.json', raw)
        self._reject(lambda: m.load_profile('site', profile_dir=deep))
        # Identity guard inspects metadata without reading identity contents.
        actual_read = os.read
        identity_inode = self.identity.stat().st_ino
        def guarded(fd, n):
            self.assertNotEqual(os.fstat(fd).st_ino, identity_inode)
            return actual_read(fd, n)
        with patch.object(m.os, 'read', side_effect=guarded):
            self.assertEqual(self._invoke().identity_env, self.identity)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_ancestry_refusal
    def test_ancestry_refusal(self):
        original = os.stat
        def unreadable(path, *a, **kw):
            if path == '.git': raise PermissionError('SYNTHETIC_CANARY')
            return original(path, *a, **kw)
        with patch.object(m.os, 'stat', side_effect=unreadable):
            self._reject(self._load)
        fstat = os.fstat
        target = self.root.stat().st_ino
        self.root.chmod(0o1777)
        def foreign_child(fd):
            st = fstat(fd)
            if st.st_ino == target:
                return types.SimpleNamespace(**{k: getattr(st, k) for k in ('st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}, st_uid=os.geteuid() + 1)
            return st
        with patch.object(m.os, 'fstat', side_effect=foreign_child):
            self._reject(self._load)
        self.root.chmod(0o700)
        self.assertIs(type(self._load()), m.Profile)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_replacement_and_read_overflow
    def test_replacement_and_read_overflow(self):
        original = os.read
        for leaf in (self.profile, self.ack, self.identity):
            changed = False
            profile = self._load()
            raw = leaf.read_bytes()
            def replace_leaf(fd, n):
                nonlocal changed
                data = original(fd, n)
                if data and not changed:
                    changed = True
                    saved = leaf.with_suffix('.replacement')
                    self._private(saved, raw)
                    saved.replace(leaf)
                return data
            with patch.object(m.os, 'read', side_effect=replace_leaf):
                self._reject(self._load if leaf == self.profile else lambda: self._invoke(profile))
        # A size increase after preflight is bounded by the sentinel read too.
        raw = self.profile.read_bytes()
        changed = False
        def grow(fd, n):
            nonlocal changed
            if not changed:
                changed = True
                with self.profile.open('ab') as f: f.write(b' ' * 16384)
            return original(fd, n)
        with patch.object(m.os, 'read', side_effect=grow):
            self._reject(self._load)
        self._private(self.profile, raw)
        self.assertIs(type(self._load()), m.Profile)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_descriptor_cleanup
    def test_descriptor_cleanup(self):
        actual_open, actual_close = os.open, os.close
        opened, closed = [], []
        def tracked_open(*a, **kw):
            fd = actual_open(*a, **kw)
            opened.append(fd)
            return fd
        def tracked_close(fd):
            closed.append(fd)
            actual_close(fd)
        try:
            with patch.object(m.os, 'open', side_effect=tracked_open), patch.object(m.os, 'close', side_effect=tracked_close):
                self.assertIs(type(self._load()), m.Profile)
                self.assertEqual(sorted(opened), sorted(closed))
                opened.clear()
                closed.clear()
                self.profile.chmod(0o644)
                self._reject(self._load)
                self.assertEqual(sorted(opened), sorted(closed))
        finally:
            # Recover descriptors held deliberately by the close-removal mutant.
            for fd in set(opened) - set(closed): actual_close(fd)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_strict_json
    def test_strict_json(self):
        raw = self.profile.read_bytes()
        for bad in (raw[:-1] + b',"capsule":"replacement"}', b'{"capsule":"a","capsule":"b"}', b'{"x":1.0}', b'{"x":NaN}',
                    b'{"x":Infinity}', b'{"x":-Infinity}', b'\xff', b'{"x":"\\ud800"}', b'{'):
            self._private(self.profile, bad)
            self._reject(self._load, 2)
        self._private(self.profile, raw)
        self.assertIs(type(self._load()), m.Profile)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_strict_decoder_public_api
    def test_strict_decoder_public_api(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":1.5}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":-Infinity}', b'{"x":"\\ud800"}', b'{"x":"\xff"}'):
            with self.subTest(raw=raw), self.assertRaises(Exception):
                m.gate.strict_load(raw)
        self.assertEqual(m.gate.strict_load(b'{"x":"valid"}'), {'x': 'valid'})

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_closed_schema
    def test_closed_schema(self):
        original = json.loads(json.dumps(self.value))
        cases = [dict(original, extra=True), {}, [], None]
        for key in original:
            cases.append({k: v for k, v in original.items() if k != key})
            cases.append(dict(original, **{key: True}))
        cases += [dict(original, live_ack_source=v) for v in ({'kind': 'file'}, {'kind': 'file', 'path': str(self.ack), 'extra': 1})]
        cases += [dict(original, allowed_modes=v) for v in ([], ['replay', 'replay'], ['unknown'], [1], 'replay')]
        for value in cases:
            self.value = value
            self._write()
            self._reject(self._load, 2)
        self.value = original
        self._write()
        self.assertIs(type(self._load()), m.Profile)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_scalars_and_kinds
    def test_scalars_and_kinds(self):
        for key in ('capsule', 'container', 'window'):
            old = self.value[key]
            for bad in ('', '-option', ' ', 'a\n', 'a\x7f', 'a' * 257, 'é' * 129):
                self.value[key] = bad
                self._write()
                self._reject(self._load, 2)
            self.value[key] = 'é' * 128
            self._write()
            self.assertIs(type(self._load()), m.Profile)
            self.value[key] = old
        for key in ('identity_env',):
            old = self.value[key]
            for bad in ('relative', '/a/../b', '/' + 'a' * 4096):
                self.value[key] = bad
                self._write()
                self._reject(self._load, 2)
            self.value[key] = old
        self.value['announce_gate'] = 'shell'
        self._write()
        self._reject(self._load)
        self.value['announce_gate'] = 'trusted_notice_v1'
        self.value['live_ack_source']['kind'] = 'env'
        self._write()
        self._reject(self._load)
        self.value['live_ack_source']['kind'] = 'file'
        self._write()
        self.assertIs(type(self._load()), m.Profile)

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_ack_and_modes
    def test_ack_and_modes(self):
        p = self._load()
        for bad in (b'', b'\n', b'\r\n'):
            self._private(self.ack, bad)
            short_pin = replace(p, ack_sha256=hashlib.sha256(bad).hexdigest())
            self._reject(lambda: self._invoke(short_pin))
        for bad in (b'a' * 31, b'X' * len(self.token), b'X' * 4097):
            self._private(self.ack, bad)
            self._reject(lambda: self._invoke(p))
        for good in (bytes(range(32)), b'X' * 4096, b'X' * 32 + b'\n'):
            self._private(self.ack, good)
            active = replace(p, ack_sha256=hashlib.sha256(good).hexdigest())
            for mode in ('replay', 'shadow'):
                self.assertEqual(self._invoke(active, mode).live_ack, active.ack_sha256)
        self._private(self.ack, self.token)
        for mode in ('promoted', 'repair', 'validate'):
            self._reject(lambda: self._invoke(replace(p, allowed_modes=(mode,)), mode))
        self._reject(lambda: self._invoke(replace(p, allowed_modes=('shadow',))))

    def _args(self):
        return [str(self.root / 'workflow.json'), '--mode', 'replay', '--output', str(self.root / 'out'), '--profile', 'site']

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_cli_conflicts_and_privacy
    def test_cli_conflicts_and_privacy(self):
        for flag in ('identity', 'container', 'window', 'identity-env', 'live-ack'):
            for value in ('', 'SYNTHETIC_CANARY'):
                with patch.object(cli, 'load_profile') as load, redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(self._args() + ['--' + flag, value]), 2)
                    load.assert_not_called()
                    self.assertNotIn('SYNTHETIC_CANARY', out.getvalue())
        for kwargs in ({'owners': object()}, {'live_ports': object()}):
            with patch.object(cli, 'load_profile') as load, redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(self._args(), **kwargs), 2)
                load.assert_not_called()
        for args in ([str(self.root / 'workflow'), '--mode', 'validate', '--profile', 'site'],
                     [str(self.root / 'workflow'), '--mode', 'replay', '--profile', 'site']):
            with patch.object(cli, 'load_profile') as load, redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 2)
                load.assert_not_called()
        for args in (self._args() + ['--SYNTHETIC_CANARY'], self._args()[:-1] + ['../SYNTHETIC_CANARY']):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(args, profile_dir=self.root), 2)
                self.assertNotIn('SYNTHETIC_CANARY', out.getvalue() + err.getvalue())
        result = unittest.TestResult()
        TestDefaultProvisioning('test_shell_default').run(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.spawn.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_schema_pin
    def test_schema_pin(self):
        with patch.object(cli.gate, 'SCHEMA_PIN', '0' * 64), patch.object(cli, 'load_profile') as load, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(self._args(), profile_dir=self.root), 3)
            load.assert_not_called()
        self.spawn.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_private_trace
    def test_private_trace(self):
        canary = 'SYNTHETIC_PRIVATE_CANARY'
        renamed = self.root / (canary + '.env')
        self.identity.rename(renamed)
        self.value.update(capsule=canary, identity_env=str(renamed))
        token = (canary * 2).encode()
        self._private(self.ack, token)
        self.value['ack_sha256'] = hashlib.sha256(token).hexdigest()
        self._write()
        profile = self._load()
        invocation = self._invoke(profile)
        def denied(*a): raise RuntimeError(canary)
        with self.assertRaises(m.ProfileError) as caught:
            m.run_profile(profile, invocation, bundle={}, services=m.ProfileServices(denied, denied))
        self.assertNotIn(canary, ''.join(traceback.format_exception(caught.exception)))
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(self._args(), profile_dir=self.root,
                profile_services=m.ProfileServices(denied, denied)), 5)
        self.assertNotIn(canary, out.getvalue() + err.getvalue())
        self.assertNotIn(canary, repr(profile))
        self.spawn.assert_not_called()
        self.builder.assert_not_called()
        self.runner.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_services_and_binding
    def test_services_and_binding(self):
        p = self._load()
        inv = self._invoke(p)
        for services in (None, {}, m.ProfileServices(None, None)):
            self._reject(lambda: m.run_profile(p, inv, bundle={}, services=services))
        services = m.ProfileServices(lambda *a: {}, lambda *a: {})
        self._reject(lambda: m.run_profile(p, replace(inv, identity='foreign'), bundle={}, services=services))
        self._reject(lambda: m.run_profile(p, inv, bundle={}, services=services))
        def explode(*a):
            raise ValueError('SYNTHETIC_CANARY')
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(self._args(), profile_dir=self.root,
                                     profile_services=m.ProfileServices(explode, explode)), 5)
            self.assertNotIn('SYNTHETIC_CANARY', out.getvalue() + err.getvalue())
        self.assertNotIn('synthetic', repr(p))
        self.assertNotIn('SYNTHETIC_ACK', repr(inv))
        self.assertNotIn('explode', repr(m.ProfileServices(explode, explode)))

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_host_installation_required
    def test_host_installation_required(self):
        # Missing installed host policy/evidence blocks before allocation; the separate
        # TestDefaultProvisioning controls exercise the configured positive path.
        profile = self._load()
        invocation = self._invoke(profile)
        with patch.object(m, '_default_profile_services', wraps=m._default_profile_services) as factory:
            self._reject(lambda: m.run_profile(profile, invocation, bundle={}, services=None))
            factory.assert_called_once_with(profile, invocation, bundle={})
            factory.reset_mock()
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(self._args(), profile_dir=self.root), 5)
            factory.assert_called_once()
            self.assertEqual(json.loads(out.getvalue()), {'rc': 5, 'verdict_code': 'OUT_OF_SUPPORT'})
            self.assertEqual(err.getvalue(), '')
        self.builder.assert_not_called()
        self.runner.assert_not_called()
        self.spawn.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_default_binding_precedes_factory
    def test_default_binding_precedes_factory(self):
        profile = self._load()
        invocation = self._invoke(profile)
        with patch.object(m, '_default_profile_services', wraps=m._default_profile_services) as factory:
            for name, value in (('identity', 'foreign'), ('container', 'foreign'),
                                ('window', 'foreign'), ('identity_env', self.root / 'foreign.env')):
                with self.subTest(field=name):
                    self._reject(lambda: m.run_profile(profile, replace(invocation, **{name: value}),
                                                      bundle={}, services=None))
                    factory.assert_not_called()
            self._reject(lambda: m.run_profile(replace(profile, announce_gate='foreign'), invocation,
                                              bundle={}, services=None))
            factory.assert_not_called()
            # Exact binding reaches the same real factory that foreign inputs cannot enter.
            self._reject(lambda: m.run_profile(profile, invocation, bundle={}, services=None))
            factory.assert_called_once_with(profile, invocation, bundle={})

    # @anchor:wa.s4.mw_profiles.TestProfiles.test_explicit_services_never_fallback
    def test_explicit_services_never_fallback(self):
        profile = self._load()
        invocation = self._invoke(profile)
        with patch.object(m, '_default_profile_services', wraps=m._default_profile_services) as factory:
            for services in ({}, m.ProfileServices(None, None),
                             m.ProfileServices(lambda *a: {}, lambda *a: {})):
                self._reject(lambda: m.run_profile(profile, invocation, bundle={}, services=services))
                factory.assert_not_called()


# @anchor:wa.s4.mw_profiles.TestProfileComposition
class TestProfileComposition(unittest.TestCase):
    # @anchor:wa.s4.mw_profiles.TestProfileComposition.setUp
    def setUp(self):
        self.live = live.TestLiveEvidence('test_real_composition_relocation')
        _isolated_live_setup(self.live)
        self.addCleanup(self.live.doCleanups)
        self.config = TestProfiles('test_lookup')
        self.config.setUp()
        self.addCleanup(self.config.doCleanups)
        self.config.stack.close()
        t, c = self.live, self.config
        c.value.update(capsule=t.f.invocation.identity, container=t.f.invocation.container, window='w1')
        c._write()
        self.profile = c._load()
        self.invocation = m.make_invocation(self.profile, workflow=t.b.path, output=t.f.invocation.output, mode='replay')
        t.f.invocation = self.invocation
        self.events = []
        self.notice_result = composition.YES
        self.notice_scope = t.ports.scope
        self.notice_authority = t.ports.authority
        self.notice_deadline = t.ports.limits.deadline_ns
        def notice(inv, *, scope, authority, now_ns, absolute_deadline_ns):
            self.events.append('notice')
            return self.notice_result if (inv == self.invocation and scope == self.notice_scope and
                authority == self.notice_authority and absolute_deadline_ns == self.notice_deadline and
                now_ns < absolute_deadline_ns) else composition.NO
        t.ports = replace(t.ports, announce_gate=notice)
        t.f.ports = t.ports
        def surface(**kwargs):
            self.events.append('surface')
            result = t.f._surface(**kwargs)
            result.frame = t._frame
            return result
        self.dependencies = t.f._dependencies()
        self.arguments = dict(locator_policy=t.p, evidence_policy=t.c, trust=t.trust, workspace=t.root,
            executable='unused', surface_factory=surface, describer=t.describer, locate_transport=t.model)
        self.services = m.ProfileServices(lambda *a: self.dependencies.copy(), lambda *a: self.arguments.copy())
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        original = owners.BoundedNativeRead.bind
        def bind(reader, inv, **kwargs):
            self.events.append('native_bind')
            original(reader, inv, **kwargs)
            return t.f.native.bind(inv, **kwargs)
        self.stack.enter_context(patch.object(owners.BoundedNativeRead, 'bind', bind))
        self.stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('PROCESS_TRAP')))
        t.b._write()

    def _run(self):
        return m.run_profile(self.profile, self.invocation, bundle=composition.base.BUNDLE, services=self.services)

    def _no_activity(self):
        self.assertFalse(self.live.f.factory_calls)
        self.assertFalse(self.live.f.native.calls)
        self.assertEqual(self.live.model.calls, 0)

    # @anchor:wa.s4.mw_profiles.TestProfileComposition.test_shell_positive
    def test_shell_positive(self):
        t = self.live
        args = [str(t.b.path), '--mode', 'replay', '--output', str(self.invocation.output), '--profile', 'site']
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args, profile_dir=self.config.root, profile_services=self.services), 0)
        self.assertEqual(self.events[:3], ['notice', 'native_bind', 'surface'])
        self.assertEqual(t.model.calls, 1)
        self.assertEqual([op for op, _ in t.f.native.calls if op != 'frame'], ['click'])
        self.assertIsNone(t.ports.actor_lock.holder)
        self.assertTrue(all(not p.exists() for p in t.model.paths))
        self.assertNotIn('SYNTHETIC_ACK', out.getvalue())

    # @anchor:wa.s4.mw_profiles.TestProfileComposition.test_shadow_positive
    def test_shadow_positive(self):
        self.invocation = m.make_invocation(self.profile, workflow=self.invocation.workflow,
            output=self.invocation.output, mode='shadow')
        self.live.f.invocation = self.invocation
        self.assertEqual(self._run().rc, 0)
        self.assertEqual(self.events[:3], ['notice', 'native_bind', 'surface'])
        self.assertIsNone(self.live.ports.actor_lock.holder)

    # @anchor:wa.s4.mw_profiles.TestProfileComposition.test_dependency_matrix
    def test_dependency_matrix(self):
        for key in tuple(self.dependencies) + ('extra',):
            previous = self.dependencies.copy()
            if key == 'extra': self.dependencies[key] = object()
            else: del self.dependencies[key]
            with self.subTest(key=key), patch.object(m, 'build_live_ports', wraps=owners.build_live_ports) as builder:
                builder.__signature__ = inspect.signature(owners.build_live_ports)
                with self.assertRaises(m.ProfileError): self._run()
                builder.assert_not_called()
            self._no_activity()
            self.dependencies = previous
        self.assertIs(type(owners.build_live_ports(self.invocation, **self.dependencies)), owners.LivePorts)
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.s4.mw_profiles.TestProfileComposition.test_notice_matrix
    def test_notice_matrix(self):
        for result in (composition.NO, owners.AdmissionCheck(owners.Truth.UNKNOWN, owners.Reason.MISSING), True):
            self.notice_result = result
            with self.assertRaises(m.ProfileError): self._run()
            self._no_activity()
        self.notice_result = composition.YES
        for attr, value in (('notice_scope', 'foreign'), ('notice_authority', 'foreign'), ('notice_deadline', 1)):
            old = getattr(self, attr)
            setattr(self, attr, value)
            with self.assertRaises(m.ProfileError): self._run()
            self._no_activity()
            setattr(self, attr, old)
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.s4.mw_profiles.TestProfileComposition.test_locator_privacy_resources
    def test_locator_privacy_resources(self):
        original = self.arguments.copy()
        for key in ('locator_policy', 'evidence_policy', 'trust', 'workspace', 'executable'):
            del self.arguments[key]
            with patch.object(m, 'run_live_workflow', wraps=live.m.run_live_workflow) as runner:
                runner.__signature__ = inspect.signature(live.m.run_live_workflow)
                with self.assertRaises(m.ProfileError): self._run()
                runner.assert_not_called()
            self._no_activity()
            self.arguments = original.copy()
        self.arguments['unexpected'] = True
        with self.assertRaises(m.ProfileError): self._run()
        self.arguments = original.copy()
        for key in ('locator_policy', 'evidence_policy', 'trust'):
            self.arguments[key] = None
            with self.assertRaises(m.ProfileError): self._run()
            self._no_activity()
            self.arguments = original.copy()
        self.dependencies['resources'] = None
        with self.assertRaises(m.ProfileError): self._run()
        self._no_activity()
        self.dependencies = self.live.f._dependencies()
        self.assertEqual(self._run().rc, 0)


    # @anchor:wa.s4.mw_profiles.TestProfileComposition.test_trusted_types_and_binding
    def test_trusted_types_and_binding(self):
        actual = self.services
        self.services = types.SimpleNamespace(dependencies=actual.dependencies, locator_arguments=actual.locator_arguments)
        with self.assertRaises(m.ProfileError): self._run()
        self._no_activity()
        self.services = actual
        self.profile = replace(self.profile, capsule='foreign')
        with self.assertRaises(m.ProfileError): self._run()
        self._no_activity()
        self.profile = replace(self.profile, capsule=self.invocation.identity)
        self.assertEqual(self._run().rc, 0)

    # @anchor:wa.s4.mw_profiles.TestProfileComposition.test_cleanup
    def test_cleanup(self):
        t = self.live
        original = self.arguments['surface_factory']
        def failure(**kwargs):
            raise ValueError('SYNTHETIC_CANARY')
        self.arguments['surface_factory'] = failure
        with self.assertRaises(m.ProfileError): self._run()
        self.assertIsNone(t.ports.actor_lock.holder)
        self.arguments['surface_factory'] = original
        t.model.generate = lambda *a: (_ for _ in ()).throw(ValueError('SYNTHETIC_CANARY'))
        self.assertNotEqual(self._run().rc, 0)
        self.assertIsNone(t.ports.actor_lock.holder)
        t.b.journal.raw = b''
        t.model.generate = live._Model.generate.__get__(t.model)
        t.model.clean = False
        with self.assertRaises(m.ProfileError): self._run()
        self.assertIsNotNone(t.ports.actor_lock.holder)
        key = (self.invocation.container, self.invocation.window)
        locator = owners._LOCATOR_CUSTODY[key]
        t.model.clean = True
        self.assertTrue(locator._drain())
        owners._LOCATOR_CUSTODY.pop(key)


# @anchor:wa.s4.mw_profiles.TestDefaultProvisioning
class TestDefaultProvisioning(unittest.TestCase):
    """Composition only: synthetic host evidence, no production backend claims.

    No ProfileServices, default factory or dependency dictionary is replaced.
    The real builder is exercised; the runner boundary is explicitly a spy.
    """
    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.setUp
    def setUp(self):
        self.c = TestProfiles('test_lookup')
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()
        self.t = live.TestLiveEvidence('test_current_capture_grounding_and_lifetime')
        self.addCleanup(self.t.doCleanups)
        _isolated_live_setup(self.t)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.ram = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.ram.cleanup)
        self.root = Path(self.ram.name)
        self.stack.enter_context(patch.object(m, '_RAM_ROOT', self.root))
        self.profile = self.c._load()
        self.invocation = self.c._invoke(self.profile)
        self.c._private(self.invocation.workflow, b'{}\n')
        self.notice_ok, self.lease_ok, self.join_ok, self.cancel_ok = True, True, True, True
        self.records, self.surfaces = [], []
        now = time.monotonic_ns()
        self.policy = m._HostConfiguration(
            limits=replace(self.t.ports.limits, deadline_ns=now + 60_000_000_000),
            native_policy=self.t.ports.native_read.policy,
            locator_policy=replace(self.t.p, absolute_deadline_ns=now + 50_000_000_000),
            evidence_policy=self.t.c, resource_capacity=4_194_304, executable='/synthetic/model',
            capability_revision='synthetic-rev', supported_locales=('en',), destination=object(),
            owners=self._owners, notice=lambda **k: composition.YES if self.notice_ok else composition.NO,
            lease=lambda **k: composition.YES if self.lease_ok else composition.NO,
            export_check=self._approved_evidence, custody_check=self._approved_evidence,
            support_check=self._approved_evidence,
            cancel=lambda timeout: composition.YES if self.cancel_ok else composition.NO,
            drain=lambda timeout: composition.YES if self.join_ok else composition.NO)
        self.stack.enter_context(m.profile_host(self._configuration))
        self.c.runner.side_effect = self._runner
        self.stack.enter_context(patch.object(m, 'SrfIdentitySurface', side_effect=self._surface))
        self.action = lambda ports, args: None
        self.deferred = []
        self.approved_binding = None
        self.approved_files = {}

    def _configuration(self, profile, invocation, *, bundle):
        return self.policy

    def _owners(self, **kwargs):
        self.records.append(kwargs)
        b = self.t.b
        return m._HostOwners(b.authorization, self.t.ports.capabilities, b.observations,
                            self.t.ports.validation_context)

    def _surface(self, **kwargs):
        self.surfaces.append(kwargs)
        return object()

    def _approved_evidence(self, **kwargs):
        binding = kwargs.get('binding', getattr(kwargs.get('observation'), 'value', None))
        ok = binding is self.approved_binding and binding is not None
        for key in ('frame_path', 'marks_path'):
            if key in kwargs:
                path = kwargs[key]
                ok = ok and self.approved_files.get(path) == (path.stat().st_ino, path.read_bytes())
        return composition.YES if ok else composition.NO

    def _runner(self, invocation, **arguments):
        ports = arguments.pop('ports')
        arguments.pop('bundle')
        self.ports, self.arguments = ports, arguments
        self.assertEqual(ports.announce_gate(invocation, scope=ports.scope, authority=ports.authority,
            now_ns=ports.clock(), absolute_deadline_ns=ports.limits.deadline_ns), composition.YES)
        try:
            self.action(ports, arguments)
        except AssertionError as error:
            self.deferred.append(str(error))
        finally:
            for path in self.approved_files:
                if path.exists(): path.unlink()
        return types.SimpleNamespace(result=composition.w.RunResult(4, 'HUMAN_HANDOFF'))

    def _run(self):
        result = m.run_profile(self.profile, self.invocation, bundle=composition.base.BUNDLE, services=None)
        self.assertEqual(self.deferred, [])
        return result

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning._synthetic_default_capacity
    @contextmanager
    def _synthetic_default_capacity(self):
        # Synthetic capacities/owners only; the actual default constructor runs.
        token = m._HOST_CONFIGURATION.set(None)
        policy = replace(self.policy,
            native_policy=replace(self.policy.native_policy, publications=8),
            limits=replace(self.policy.limits, acquisitions=8))
        try:
            with patch.object(m, '_default_capacity_configuration', return_value=policy), \
                 patch.object(m.shutil, 'which', return_value='/synthetic/muse'):
                yield
        finally:
            m._HOST_CONFIGURATION.reset(token)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning._without_provider
    @contextmanager
    def _without_provider(self):
        token = m._HOST_CONFIGURATION.set(None)
        try:
            yield
        finally:
            m._HOST_CONFIGURATION.reset(token)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning._default_control
    def _default_control(self):
        with self._without_provider(), patch.object(m.shutil, 'which', return_value='/synthetic/tool'), \
             patch.dict(os.environ, VQA_VE_ROOT=str(self.c.root)), redirect_stdout(io.StringIO()):
            self.assertEqual(self._run().rc, 4)
        self.c.spawn.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_bound_authorization
    def test_bound_authorization(self):
        workflow = copy.deepcopy(self.t.b.workflow)
        self.c._private(self.invocation.workflow, (m.gate.canon(workflow) + '\n').encode())
        def activity(ports, args):
            auth = ports.authorization
            expected = {'step_' + str(step['seq']): (step['seq'],) for step in workflow['steps']}
            self.assertEqual(auth.inventory(workflow), expected)
            reparsed = m.gate.strict_load((m.gate.canon(workflow) + '\n').encode())
            self.assertEqual(auth.inventory(reparsed), expected)
            self.assertEqual(auth.check('send', reparsed, ports.scope, ports.authority), composition.YES)
            inventory = auth.inventory(workflow)
            inventory.clear()
            self.assertEqual(auth.inventory(workflow), expected)
            for phase in ('admission', 'step', 'send', 'commit'):
                self.assertEqual(auth.check(phase, workflow, ports.scope, ports.authority), composition.YES)
            for phase, value, scope, authority in (
                ('send', workflow, object(), ports.authority),
                ('send', workflow, ports.scope, object()),
                ('guidance', workflow, ports.scope, ports.authority),
                ('unknown', workflow, ports.scope, ports.authority)):
                self.assertEqual(auth.check(phase, value, scope, authority),
                                 m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            record = args['surface_factory'].__self__
            original = record.profile
            for invalid in (replace(original, capsule='foreign'), replace(original, allowed_modes=())):
                record.profile = invalid
                self.assertEqual(auth.check('send', workflow, ports.scope, ports.authority),
                                 m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            record.profile = original
            record.active = False
            self.assertEqual(auth.check('send', workflow, ports.scope, ports.authority),
                             m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            record.active = True
            with patch.object(record.config.lease.__self__, 'fd', None):
                self.assertEqual(auth.check('send', workflow, ports.scope, ports.authority),
                                 m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            config = record.config
            record.config = replace(config, limits=replace(config.limits, deadline_ns=1))
            self.assertEqual(auth.check('send', workflow, ports.scope, ports.authority),
                             m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            record.config = config
            self.assertEqual(auth.check('send', workflow, ports.scope, ports.authority), composition.YES)
        self.action = activity
        self._default_control()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_bound_inventory
    def test_bound_inventory(self):
        workflow = copy.deepcopy(self.t.b.workflow)
        def activity(ports, args):
            for steps in ([], [dict(workflow['steps'][0], seq=2)],
                          [dict(workflow['steps'][0], protected='yes')]):
                invalid = dict(workflow, steps=steps)
                self.c._private(self.invocation.workflow, (m.gate.canon(invalid) + '\n').encode())
                with self.assertRaises(m.ProfileError) as caught:
                    ports.authorization.inventory(invalid)
                self.assertEqual(caught.exception.category, 'OUT_OF_SUPPORT')
            self.c._private(self.invocation.workflow, (m.gate.canon(workflow) + '\n').encode())
            self.assertTrue(ports.authorization.inventory(workflow))
        self.action = activity
        self._default_control()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_download_export_admission
    def test_download_export_admission(self):
        workflow = copy.deepcopy(self.t.b.workflow)
        self.c._private(self.invocation.workflow, (m.gate.canon(workflow) + '\n').encode())
        evidence = m._ProducerEvidence(m.BINARY, 'a' * 64, frozenset(('downloads',)),
                                      time.monotonic_ns(), 128, True, None)
        def activity(ports, args):
            record = args['surface_factory'].__self__
            export = args['trust'].export_check
            ports.authorization.inventory(workflow)
            request = dict(invocation=self.invocation, scope=ports.scope, authority=ports.authority,
                deadline_ns=ports.limits.deadline_ns, operation='downloads', destination=self.profile.export_dir)
            self.assertEqual(export(**request), composition.YES)
            for change in (dict(scope=object()), dict(authority=object()), dict(invocation=replace(self.invocation)),
                           dict(deadline_ns=1), dict(deadline_ns=True), dict(operation='frame'),
                           dict(destination=self.c.root), dict(destination=str(self.profile.export_dir))):
                self.assertEqual(export(**dict(request, **change)),
                                 m.AdmissionCheck(m.Truth.FALSE, m.Reason.PRIVACY))
            alias = self.c.root / 'download-alias'
            alias.symlink_to(self.profile.export_dir, target_is_directory=True)
            self.assertEqual(export(**dict(request, destination=alias)),
                             m.AdmissionCheck(m.Truth.FALSE, m.Reason.PRIVACY))
            for mode in (0o720, 0o702, 0o777):
                self.profile.export_dir.chmod(mode)
                try:
                    self.assertEqual(export(**request),
                                     m.AdmissionCheck(m.Truth.FALSE, m.Reason.PRIVACY))
                finally:
                    self.profile.export_dir.chmod(0o700)
            real_stat = os.fstat
            inode = self.profile.export_dir.stat().st_ino
            def foreign(fd):
                info = real_stat(fd)
                if info.st_ino != inode:
                    return info
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            with patch.object(m.os, 'fstat', side_effect=foreign):
                self.assertEqual(export(**request),
                                 m.AdmissionCheck(m.Truth.FALSE, m.Reason.PRIVACY))
            caps = record.owners.capabilities
            for invalid in (replace(evidence, operations=frozenset()), replace(evidence, valid=False),
                            replace(evidence, digest=None), replace(evidence, output_bytes=65537),
                            replace(evidence, observed_ns=ports.limits.deadline_ns)):
                caps.producer = invalid
                self.assertEqual(export(**request),
                                 m.AdmissionCheck(m.Truth.FALSE, m.Reason.PRIVACY))
            caps.producer = evidence
            auth = record.owners.authorization
            registered = auth.workflow
            auth.workflow = None
            self.assertEqual(export(**request),
                             m.AdmissionCheck(m.Truth.FALSE, m.Reason.PRIVACY))
            auth.workflow = registered
            self.assertEqual(export(**request), composition.YES)
            self.assertEqual(args['trust'].export_check(**request), composition.YES)
            caps.producer = replace(evidence, operations=frozenset(('paste', 'type-stdin', 'scroll', 'drag', 'downloads')))
            self.assertEqual(caps.supports(workflow), m.AdmissionCheck(m.Truth.FALSE, m.Reason.MISSING))
            self.c._private(self.c.ack, b'X' * len(self.c.token))
            self.assertEqual(export(**request),
                             m.AdmissionCheck(m.Truth.FALSE, m.Reason.PRIVACY))
        self.action = activity
        with patch.object(m, '_producer_evidence', return_value=evidence) as probe:
            self._default_control()
        probe.assert_called_once()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_bound_workflow_pin
    def test_bound_workflow_pin(self):
        workflow = copy.deepcopy(self.t.b.workflow)
        self.c._private(self.invocation.workflow, (m.gate.canon(workflow) + '\n').encode())
        def activity(ports, args):
            auth = ports.authorization
            auth.inventory(workflow)
            self.assertEqual(auth.check('send', workflow, ports.scope, ports.authority), composition.YES)
            self.c._private(self.invocation.workflow, (m.gate.canon(dict(workflow, id='replaced')) + '\n').encode())
            self.assertEqual(auth.check('send', workflow, ports.scope, ports.authority),
                             m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            with self.assertRaises(m.ProfileError):
                auth.inventory(workflow)
        self.action = activity
        self._default_control()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_disabled_ai_locate
    def test_disabled_ai_locate(self):
        def activity(ports, args):
            record = args['surface_factory'].__self__
            self.assertFalse(record.config.ai_locate)
            self.assertEqual(args['executable'], '')
            transport = args['locate_transport']
            self.assertIsInstance(transport, m._DisabledLocateTransport)
            with self.assertRaises(m.ProfileError) as caught:
                transport.generate({}, object(), record.config.locator_policy,
                                   record.config.limits.deadline_ns, ports.clock)
            self.assertEqual(caught.exception.reason, 'AI locate disabled')
            self.assertEqual(transport.cancel_and_drain(record.config.locator_policy, ports.clock), composition.YES)
        self.action = activity
        with self._without_provider(), \
             patch.object(m.shutil, 'which', side_effect=lambda name: None if name == 'muse' else '/synthetic/tool'), \
             patch.dict(os.environ, VQA_VE_ROOT=str(self.c.root)), redirect_stdout(io.StringIO()):
            try:
                self.assertEqual(self._run().rc, 4)
            except m.ProfileError as error:
                self.fail('disabled locate must reach the runner: ' + error.category)
        self.c.builder.assert_called_once()
        self.c.runner.assert_called_once()
        self.c.spawn.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_workflow_ai_locate_configuration
    def test_workflow_ai_locate_configuration(self):
        for step, expected in (({'target': None}, False),
                               ({'target': {'rung': 'T1'}}, False),
                               ({'target': {'rung': 'T2'}}, True),
                               ({'target': None, 'fallback_targets': [{'rung': 'T2'}]}, True)):
            self.c._private(self.invocation.workflow, (m.gate.canon({'steps': [step]}) + '\n').encode())
            with self.subTest(step=step), self._without_provider(), \
                 patch.dict(os.environ, VQA_VE_ROOT=str(self.c.root)), \
                 patch.object(m.shutil, 'which', side_effect=lambda name: None if name == 'muse' else '/synthetic/tool'):
                if expected:
                    with self.assertRaises(m.ProfileError) as caught:
                        self._run()
                    self.assertEqual(caught.exception.reason, 'muse executable missing for AI locate')
                    self.c.builder.assert_not_called()
                    self.c.runner.assert_not_called()
                else:
                    self.assertEqual(self._run().rc, 4)
                    record = self.arguments['surface_factory'].__self__
                    self.assertFalse(record.config.ai_locate)
                    self.c.builder.assert_called_once()
                    self.c.runner.assert_called_once()
                self.c.builder.reset_mock()
                self.c.runner.reset_mock()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_ai_locate_exact_bool
    def test_ai_locate_exact_bool(self):
        for invalid in (0, 1, None, 'false'):
            self.policy = replace(self.policy, ai_locate=invalid)
            with self.assertRaises(m.ProfileError) as caught:
                self._run()
            self.assertEqual(caught.exception.category, 'OUT_OF_SUPPORT')
        self.c.builder.assert_not_called()
        self.c.runner.assert_not_called()
        self.c.spawn.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_without_provider
    def test_default_without_provider(self):
        def activity(ports, args):
            config = args['surface_factory'].__self__.config
            self.assertEqual(config.evidence_policy.ve_process_bytes, 1024 * 1024 * 1024)
            self.assertEqual(config.limits.peak_bytes, 2048 * 1024 * 1024)
            self.assertEqual(config.resource_capacity, config.limits.peak_bytes)
            workspace = args['workspace']
            self.assertEqual(workspace.name, ports.run_id)
            self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o700)
            args['surface_factory'](identity=self.invocation.identity, container=self.invocation.container,
                window=self.invocation.window, identity_env=self.invocation.identity_env,
                timeout=1, transport=None, trace=None)
        self.action = activity
        with self._without_provider(), patch.object(m.shutil, 'which', return_value='/synthetic/tool'), \
             patch.dict(os.environ, VQA_VE_ROOT=str(self.c.root)), \
             patch.object(Path, 'home', return_value=self.c.root), \
             patch.object(m, '_LEASE_ROOT', self.c.root / 'leases'), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self._run().rc, 4)
        self.c.builder.assert_called_once()
        self.c.runner.assert_called_once()
        self.assertEqual(len(self.surfaces), 1)
        self.assertFalse(tuple(self.root.iterdir()))
        notice = self.c.root / '.local/state/memoryweb/notices.jsonl'
        self.assertEqual([json.loads(row)['phase'] for row in notice.read_text().splitlines()], ['start', 'end'])
        self.assertIn('"phase":"start"', out.getvalue())

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_support_preflight
    def test_default_support_preflight(self):
        self.c._private(self.invocation.workflow, b'{"steps":[{"target":{"rung":"T2"}}]}\n')
        for missing in ('root_unset', 'root_missing', 'tesseract', 'muse'):
            env = {} if missing == 'root_unset' else {'VQA_VE_ROOT': str(self.c.root / 'absent' if missing == 'root_missing' else self.c.root)}
            def which(name):
                return None if name == missing else '/synthetic/' + name
            with self.subTest(missing=missing), self._without_provider(), \
                 patch.dict(os.environ, env, clear=True), patch.object(m.shutil, 'which', side_effect=which):
                with self.assertRaises(m.ProfileError) as caught:
                    self._run()
                expected = ('VQA_VE_ROOT must name an existing directory' if missing.startswith('root_')
                    else 'tesseract executable missing' if missing == 'tesseract'
                    else 'muse executable missing for AI locate')
                self.assertEqual(caught.exception.reason, expected)
            self.c.builder.assert_not_called()
            self.c.runner.assert_not_called()
            self.assertFalse(tuple(self.root.iterdir()))
        self.test_default_without_provider()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_export_custody_support
    def test_default_export_custody_support(self):
        def activity(ports, args):
            record = args['surface_factory'].__self__
            workflow = copy.deepcopy(self.t.b.workflow)
            self.c._private(self.invocation.workflow, (m.gate.canon(workflow) + '\n').encode())
            ports.authorization.inventory(workflow)
            record.owners.capabilities.producer = m._ProducerEvidence(m.BINARY, 'a' * 64,
                frozenset(('downloads',)), ports.clock(), 128, True, None)
            self.assertEqual(args['trust'].export_check(invocation=self.invocation,
                scope=ports.scope, authority=ports.authority, deadline_ns=ports.limits.deadline_ns,
                operation='downloads', destination=self.profile.export_dir), composition.YES)
            config = record.config
            self.assertEqual(config.export_check(), composition.YES)
            self.export_path = self.profile.export_dir
            for mode in (0o720, 0o702):
                self.export_path.chmod(mode)
                try:
                    self.assertNotEqual(config.export_check(), composition.YES)
                finally:
                    self.export_path.chmod(0o700)
            trust = args['trust']
            binding = types.SimpleNamespace(scope=ports.scope, authority=ports.authority)
            observation = types.SimpleNamespace(scope=ports.scope, authority=ports.authority,
                value=binding, complete=True, missing_reason=None)
            frame, marks = record.workspace / 'frame', record.workspace / 'marks'
            for path in (frame, marks):
                path.write_bytes(b'SYNTHETIC_PIXELS')
                path.chmod(0o600)
            custody = dict(scope=ports.scope, authority=ports.authority, binding=binding,
                frame_path=frame, marks_path=marks, deadline_ns=ports.limits.deadline_ns)
            disclosure = dict(scope=ports.scope, authority=ports.authority, observation=observation,
                description=object(), semantic_target='synthetic', frame_path=frame, marks_path=marks,
                deadline_ns=ports.limits.deadline_ns)
            support = dict(target={}, observation=observation, description=object(), mark=object(),
                point=(0, 0), step=object(), scope=ports.scope, authority=ports.authority,
                destination=trust.destination, deadline_ns=ports.limits.deadline_ns)
            try:
                self.assertEqual(trust.custody_check(**custody), composition.YES)
                with patch.object(config.custody_check.__self__, 'lease', return_value=composition.NO):
                    self.assertNotEqual(trust.custody_check(**custody), composition.YES)
                record.workspace.chmod(0o755)
                try:
                    self.assertNotEqual(trust.custody_check(**custody), composition.YES)
                finally:
                    record.workspace.chmod(0o700)
                # The configured image limit, rather than a hardcoded ceiling.
                with frame.open('r+b') as stream:
                    stream.truncate(config.locator_policy.image_bytes + 1)
                self.assertNotEqual(trust.custody_check(**custody), composition.YES)
                frame.write_bytes(b'SYNTHETIC_PIXELS')
                # Installed tools cannot supply runtime grounding/disclosure evidence.
                self.assertNotEqual(trust.support_check(**support), composition.YES)
                self.assertNotEqual(trust.export_check(**disclosure), composition.YES)
                host = config.support_check.__self__
                with patch.object(m.shutil, 'which', side_effect=lambda name: None if name == 'muse' else '/synthetic/tool'):
                    host.preflight(ai_locate=False)
                    with self.assertRaises(m.ProfileError): host.preflight(ai_locate=True)
            finally:
                frame.unlink()
                marks.unlink()
        self.action = activity
        with self._without_provider(), patch.object(m.shutil, 'which', return_value='/synthetic/tool'), \
             patch.dict(os.environ, VQA_VE_ROOT=str(self.c.root)), \
             patch.object(Path, 'home', return_value=self.c.root), \
             patch.object(m, '_LEASE_ROOT', self.c.root / 'leases'), redirect_stdout(io.StringIO()):
            self.assertEqual(self._run().rc, 4)
        self.assertFalse(tuple(self.root.iterdir()))

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning._observation_control
    def _observation_control(self, exercise):
        workflow = copy.deepcopy(self.t.b.workflow)
        self.c._private(self.invocation.workflow, (m.gate.canon(workflow) + '\n').encode())
        def activity(ports, args):
            r = args['surface_factory'].__self__
            owner = r.owners.observations
            self.assertIsInstance(owner, m._ProfileObservations)
            self.assertIs(owner.ports, ports)
            self.assertIs(owner.describer, args['describer'])
            self.assertIn(owner.describer.drain, r.resources._joins)
            self.assertIn(owner.drain, r.resources._joins)
            ports.authorization.inventory(workflow)
            source = r.workspace / 'synthetic-source'
            source.write_bytes(live._png())
            source.chmod(0o600)
            self.approved_files[source] = (source.stat().st_ino, source.read_bytes())
            seq = [0]
            def frame():
                seq[0] += 1
                return dict(path=str(source), seq=seq[0], width=10, height=10,
                            window=dict(id=self.invocation.window, w=10, h=10))
            surface = types.SimpleNamespace(window=self.invocation.window, frame=MagicMock(side_effect=frame))
            with patch.object(m, 'SrfIdentitySurface', return_value=surface):
                actual = args['surface_factory'](identity=self.invocation.identity,
                    container=self.invocation.container, window=self.invocation.window,
                    identity_env=self.invocation.identity_env, timeout=30, transport=object(), trace=None)
            self.assertIs(owner.surface, actual)
            owner.handlers['fixture'] = ('boolean', 'truth', lambda d: d.marks[0].ocr_text == 'Go')
            describer = live._Describer()
            with patch.object(owner.describer, 'describe', side_effect=describer.describe) as describe:
                exercise(r, owner, surface, describer, describe, workflow)
            self.observation_owner = owner
        self.action = activity
        self._default_control()
        self.assertTrue(self.observation_owner.closed)
        self.assertEqual(self.observation_owner.paths, {})
        self.assertFalse(tuple(self.root.iterdir()))

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_production_semantic_workflow
    def test_production_semantic_workflow(self):
        from tests.test_mw_handlers_unit import _predicate
        real_probe = _REAL_PROBE
        # Only the producer child, surface, recognizer and time are synthetic.
        # Capability support, observation ownership, final oracle and runner are real.
        for variant in ('positive', 'split_lines', 'never_busy'):
            with self.subTest(variant=variant):
                self.invocation = m.make_invocation(self.profile, workflow=self.c.root / ('variant-' + variant.replace('_', '-') + '.json'), output=self.c.root / ('out-' + variant), mode='replay')
                workflow = copy.deepcopy(self.t.b.workflow)
                workflow['id'] = self.invocation.workflow.stem
                present = _predicate(phrase='hello world')
                busy = _predicate(phrase='Busy')
                absent = _predicate('mw.ocr.text_absent.v1', phrase='Busy')
                stable = _predicate('mw.frame.stable.v1', k=3)
                workflow['engine_compat'] = [dict(engine='wender-agent', version_constraint='==1.0.0',
                                                        required_capabilities=sorted(m._SIGNAL_IDS))]
                workflow['preconditions'] = []
                workflow['trigger_sig']['select_when'] = []
                workflow['trigger_sig']['reject_when'] = []
                step = workflow['steps'][0]
                step.update(target=None, fallback_targets=[], already_satisfied=None, preconditions=[],
                            capture_before=[], when=None, repeat=None, protected=False)
                step['action'].update(kind='wait_signal', path='coordinates', button=None,
                                      destination=None, text_slot=None, key_name=None, dx_px=None, dy_px=None)
                step['verify'].update(mode='state_signal', predicate=absent, busy_predicate=busy,
                                      fallback_verifiers=[], consecutive_polls=1, deadline_ms=40,
                                      poll_interval_ms=1, recorded_gap_ms=0, ram_evidence_ref=None,
                                      mask_boxes_px=[], change_box_px=None)
                second = copy.deepcopy(step)
                second.update(seq=2)
                second['verify'].update(predicate=stable, busy_predicate=None, deadline_ms=4000, poll_interval_ms=1000)
                workflow['steps'] = [step, second]
                workflow['oracle'].update(postconditions=[present, absent], planted_mutant_ids=[],
                                           mutants_total=0, mutants_killed=0)
                defs = composition.base.BUNDLE['$defs']
                for candidate in workflow['steps']:
                    self.assertTrue(m.gate.conforms(candidate['verify']['predicate'], defs['predicate'], defs))
                    self.assertEqual([key for key, value in candidate['verify'].items() if not m.gate.conforms(value, defs['verify']['properties'][key], defs)], [])
                    self.assertEqual([key for key, value in candidate.items() if not m.gate.conforms(value, defs['step']['properties'][key], defs)], [])
                self.assertEqual([key for key, value in workflow.items() if not m.gate.conforms(value, defs['workflow']['properties'][key], defs)], [])
                self.c._private(self.invocation.workflow, (m.gate.canon(workflow) + '\n').encode())
                self.c._private(self.c.root / 'EXPLANATION.md', (self.t.b.root / 'EXPLANATION.md').read_bytes())
                ticks = [time.monotonic_ns()]
                def clock():
                    ticks[0] += 10_000
                    return ticks[0]
                def sleep(seconds):
                    ticks[0] += int(seconds * 1_000_000_000)
                source = [None]
                sequence = [0]
                def frame():
                    sequence[0] += 1
                    return dict(path=str(source[0]), seq=sequence[0], width=10, height=10,
                                window=dict(id=self.invocation.window, w=10, h=10))
                surface = types.SimpleNamespace(window=self.invocation.window, frame=frame,
                                                start=lambda: None, stop=lambda: None)
                def surface_factory(**kwargs):
                    record = self.c.runner.call_args.kwargs['surface_factory'].__self__
                    source[0] = record.workspace / 'synthetic-source'
                    source[0].write_bytes(live._png())
                    source[0].chmod(0o600)
                    # Provisioning owns only publications; the synthetic source
                    # models the surface-owned RAM capture and its stop cleanup.
                    surface.stop = lambda: source[0].unlink(missing_ok=True)
                    return surface
                recognizer = live._Describer()
                def change(d):
                    if sequence[0] == 1 and variant != 'never_busy':
                        marks = (m.Mark(1, 0, 0, 2, 2, 'text', 1.0, 'Busy', 900, None, None),)
                    else:
                        y = 5 if variant == 'split_lines' else 0
                        marks = (m.Mark(1, 0, 0, 2, 2, 'text', 1.0, 'hello', 900, None, None),
                                 m.Mark(2, 4, y, 2, 2, 'text', 1.0, 'world', 900, None, None))
                    return replace(d, marks=marks)
                recognizer.change = change
                output = b'srf-identity-surface capabilities v1\n--paste --type-stdin --scroll --drag downloads\n'
                def spawn(argv, **kwargs):
                    self.assertEqual(argv, [m.BINARY, 'capabilities'])
                    return _REAL_POPEN([sys.executable, '-c', 'import os; os.write(1, ' + repr(output) + ')'], **kwargs)
                self.c.runner.side_effect = None
                validations = []
                validate = m.gate.validate_file
                def checked(*args, **kwargs):
                    value = validate(*args, **kwargs)
                    validations.append(value)
                    return value
                with patch.object(m.gate, 'validate_file', side_effect=checked), self._without_provider(), patch.object(m, '_producer_evidence', side_effect=real_probe), \
                     patch.object(m.subprocess, 'Popen', side_effect=spawn), \
                     patch.object(m, 'SrfIdentitySurface', side_effect=surface_factory), \
                     patch.object(m.LiveOCRDescriber, 'describe', side_effect=recognizer.describe), \
                     patch.object(m.time, 'monotonic_ns', side_effect=clock), patch.object(m.time, 'sleep', side_effect=sleep), \
                     patch.object(m.shutil, 'which', return_value='/synthetic/tool'), \
                     patch.dict(os.environ, VQA_VE_ROOT=str(self.c.root)), redirect_stdout(io.StringIO()):
                    result = m.run_profile(self.profile, self.invocation, bundle=composition.base.BUNDLE, services=None)
                if variant == 'positive':
                    self.assertEqual(result.verdict_code, 'VERIFIED', (result, validations))
                else:
                    self.assertNotEqual(result.rc, 0, result)
                self.assertFalse(tuple(self.root.iterdir()))

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_signal_dispatch_and_oracle
    def test_signal_dispatch_and_oracle(self):
        from tests.test_mw_handlers_unit import _predicate
        predicate = _predicate(phrase='Go')
        workflow = self.t.b.workflow
        workflow['oracle']['postconditions'] = [predicate]
        def exercise(r, owner, surface, describer, describe, workflow):
            caps = r.owners.capabilities
            caps.producer = m._ProducerEvidence(m.BINARY, 'a' * 64, frozenset(('drag',)),
                                               time.monotonic_ns(), 128, True, None)
            verifier = dict(mode='state_signal', predicate=predicate)
            self.assertEqual(caps.verifier(verifier), composition.YES)
            self.assertEqual(caps.action(dict(kind='wait_signal'), None), composition.YES)
            for invalid in (dict(mode='side_effect', predicate=predicate),
                            dict(mode='state_signal', predicate=dict(predicate, kind='unknown')),
                            dict(mode='state_signal', predicate=dict(predicate, capability_id='unknown'))):
                self.assertNotEqual(caps.verifier(invalid), composition.YES)
            self.assertEqual(caps.equivalent(verifier, copy.deepcopy(verifier)), composition.YES)
            self.assertNotEqual(caps.equivalent(verifier, dict(verifier, predicate=_predicate(phrase='other'))), composition.YES)
            engine = dict(engine='wender-agent', version_constraint='==1.0.0', required_capabilities=sorted(m._SIGNAL_IDS))
            self.assertEqual(caps.capability_check(engine, None), m.gate.GateResult(0, 'PASS'))
            for invalid in (dict(engine, version_constraint='*'), dict(engine, version_constraint=None),
                            dict(engine, engine='vision-engine'), dict(engine, required_capabilities=['unknown'])):
                self.assertNotEqual(caps.capability_check(invalid, None).rc, 0)
            self.assertNotEqual(caps.final(workflow), composition.YES)
            reserve = r.resources.reserve
            before_capture = surface.frame.call_count
            def admission(kind, amount):
                return composition.NO if kind == 'signal_metadata' else reserve(kind, amount)
            with patch.object(r.resources, 'reserve', side_effect=admission):
                with self.assertRaises(m.ProfileError):
                    owner.acquire(predicate, r.config.limits.deadline_ns)
            self.assertEqual(surface.frame.call_count, before_capture)
            value = owner.acquire(predicate, r.config.limits.deadline_ns)
            self.assertTrue(value.value)
            self.assertEqual(caps.final(workflow), composition.YES)
            self.assertNotEqual(caps.final(dict(workflow, id='foreign')), composition.YES)
            self.assertNotEqual(caps.final(dict(workflow, oracle=dict(workflow['oracle'],
                postconditions=[_predicate(phrase='different')]))), composition.YES)
            identity = owner.predicates[value.publication]
            owner.predicates[value.publication] = m.gate.canon(_predicate(phrase='other'))
            self.assertNotEqual(caps.final(workflow), composition.YES)
            owner.predicates[value.publication] = identity
            self.assertNotEqual(owner.validate(replace(value)), composition.YES)
            for invalid in (replace(value, scope=object()), replace(value, value=False)):
                owner.registered[value.publication] = invalid
                self.assertNotEqual(caps.final(workflow), composition.YES)
            owner.registered[value.publication] = value
            self.assertEqual(caps.final(workflow), composition.YES)
            owner.expiries[value.publication] = 1
            self.assertNotEqual(caps.final(workflow), composition.YES)
            owner.expiries[value.publication] = r.config.limits.deadline_ns
            bad = dict(predicate, kind='unknown')
            before = surface.frame.call_count
            with self.assertRaises(m.SignalRefused): owner.acquire(bad, r.config.limits.deadline_ns)
            self.assertEqual(surface.frame.call_count, before)
            describer.change = lambda d: replace(d, marks=())
            absent = owner.acquire(_predicate('mw.ocr.text_absent.v1', phrase='missing'), r.config.limits.deadline_ns)
            self.assertFalse(absent.complete)
            describer.change = lambda d: d
            absent = owner.acquire(_predicate('mw.ocr.text_absent.v1', phrase='missing'), r.config.limits.deadline_ns)
            self.assertTrue(absent.complete and absent.value)
        self._observation_control(exercise)
        self.assertEqual(self.observation_owner.predicates, {})
        self.assertIsNone(self.observation_owner.signals.streak)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_capped_signal_observations
    def test_capped_signal_observations(self):
        from tests.test_mw_handlers_unit import _predicate
        def exercise(r, owner, surface, describer, describe, workflow):
            deadline = r.config.limits.deadline_ns
            describer.change = lambda d: replace(d, mark_cap_reached=True)
            present = owner.acquire(_predicate(phrase='Go'), deadline)
            self.assertTrue(present.complete and present.value)
            for cid in ('mw.ocr.text_absent.v1', 'mw.ocr.text_present.v1'):
                value = owner.acquire(_predicate(cid, phrase='missing'), deadline)
                self.assertFalse(value.complete)
                self.assertIsNone(value.value)
                self.assertEqual(value.missing_reason, m.Reason.MISSING)
            value = owner.acquire(_predicate('mw.ocr.text_absent.v1', phrase='Go'), deadline)
            self.assertFalse(value.complete)
            stable = owner.acquire(_predicate('mw.frame.stable.v1', k=2, stable_ms=1), deadline)
            self.assertTrue(stable.complete)
            legacy = owner.acquire(dict(capability_id='fixture'), deadline)
            self.assertFalse(legacy.complete)
            describer.change = lambda d: replace(d, mark_cap_reached=1)
            self.assertFalse(owner.acquire(_predicate(phrase='Go'), deadline).complete)
            describer.change = lambda d: d
            value = owner.acquire(_predicate('mw.ocr.text_absent.v1', phrase='missing'), deadline)
            self.assertTrue(value.complete and value.value)
        self._observation_control(exercise)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_regional_signal_observations
    def test_regional_signal_observations(self):
        from PIL import Image
        from tests.test_mw_handlers_unit import _predicate
        def exercise(r, owner, surface, describer, describe, workflow):
            source = r.workspace / 'synthetic-source'
            image = Image.new('RGB', (10, 10))
            image.putpixel((4, 4), (255, 0, 0))
            image.save(source, format='PNG')
            self.approved_files[source] = (source.stat().st_ino, source.read_bytes())
            full_digest = hashlib.sha256(image.tobytes()).hexdigest()
            deadline = r.config.limits.deadline_ns
            expected_size = [(5, 5)]
            corrupt = [None]
            def recognize(frame, *, marks_out, ocr_marks, deadline_ns):
                with Image.open(frame) as crop:
                    self.assertEqual(crop.size, expected_size[0])
                    pixels = crop.convert('RGB')
                    found = (255, 0, 0) in pixels.getdata()
                    digest = hashlib.sha256(pixels.tobytes()).hexdigest()
                marks_out.write_bytes(live._png())
                mark = m.Mark(1, 1, 1, 1, 1, 'text', 1.0,
                              'Go' if found else 'other', 900, None, None)
                result = m.DescribeResult('fixture', digest, *expected_size[0], None, (), (), 0,
                                          (mark,), str(marks_out), True)
                if corrupt[0] == 'dimensions': result = replace(result, width=10)
                if corrupt[0] == 'digest': result = replace(result, frame_sha256='f' * 64)
                if corrupt[0] == 'bounds': result = replace(result, marks=(replace(mark, x=result.width),))
                return result
            describe.side_effect = recognize
            predicate = _predicate(phrase='Go', region_px=[3, 3, 8, 8])
            evaluate = owner.signals.evaluate
            with patch.object(owner.signals, 'evaluate', wraps=evaluate) as evaluated:
                value = owner.acquire(predicate, deadline)
                self.assertTrue(value.complete and value.value)
                projection = evaluated.call_args.args[1]
                self.assertEqual((projection.width, projection.height), (10, 10))
                self.assertEqual(projection.frame_sha256, full_digest)
                self.assertEqual((projection.marks[0].x, projection.marks[0].y), (4, 4))
                self.assertFalse(projection.ocr_complete)
            for defect in ('dimensions', 'digest', 'bounds'):
                corrupt[0] = defect
                with patch.object(owner.signals, 'evaluate', wraps=evaluate) as evaluated:
                    self.assertFalse(owner.acquire(predicate, deadline).complete, defect)
                    evaluated.assert_not_called()
            corrupt[0] = None
            expected_size[0] = (3, 3)
            outside = owner.acquire(_predicate(phrase='Go', region_px=[0, 0, 3, 3]), deadline)
            self.assertFalse(outside.complete and outside.value)
            expected_size[0] = (5, 5)
            calls = describe.call_count
            invalid = owner.acquire(_predicate(phrase='Go', region_px=[3, 3, 11, 8]), deadline)
            self.assertFalse(invalid.complete)
            self.assertEqual(describe.call_count, calls)
            original = surface.frame.side_effect
            def wrong_dimensions():
                frame = original()
                return dict(frame, width=11, window=dict(id=r.invocation.window, w=11, h=10))
            surface.frame.side_effect = wrong_dimensions
            self.assertFalse(owner.acquire(predicate, deadline).complete)
            self.assertEqual(describe.call_count, calls)
        self._observation_control(exercise)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_empty_regional_absence
    def test_empty_regional_absence(self):
        from tests.test_mw_handlers_unit import _predicate
        def exercise(r, owner, surface, describer, describe, workflow):
            deadline = r.config.limits.deadline_ns
            for capability, expected in (('mw.ocr.text_absent.v1', True),
                                         ('mw.ocr.text_present.v1', False)):
                predicate = _predicate(capability, phrase='missing', region_px=[0, 0, 10, 10])
                with self.subTest(capability=capability):
                    describer.change = lambda d: replace(d, marks=(), mark_cap_reached=False)
                    value = owner.acquire(predicate, deadline)
                    self.assertTrue(value.complete)
                    self.assertIs(value.value, expected)
                    self.assertEqual(owner.validate(value), composition.YES)
                    describer.change = lambda d: replace(d, marks=(), mark_cap_reached=True)
                    value = owner.acquire(predicate, deadline)
                    self.assertFalse(value.complete)
                    self.assertIsNone(value.value)
                    self.assertNotEqual(owner.validate(value), composition.YES)
        self._observation_control(exercise)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_trusted_native_action_admission
    def test_trusted_native_action_admission(self):
        from tests.test_mw_handlers_unit import _predicate
        workflow = self.t.b.workflow
        workflow['engine_compat'] = [dict(engine='wender-agent', version_constraint='==1.0.0',
                                          required_capabilities=sorted(m._SIGNAL_IDS))]
        workflow['oracle']['postconditions'] = [_predicate(phrase='Go')]
        workflow['trigger_sig'].update(select_when=[], reject_when=[])
        workflow['preconditions'] = []
        step = workflow['steps'][0]
        step.update(target=None, fallback_targets=[], preconditions=[], already_satisfied=None,
                    capture_before=[], when=None, repeat=None)
        step['action']['kind'] = 'paste'
        step['verify'].update(mode='state_signal', predicate=_predicate(phrase='Go'),
                              busy_predicate=None, fallback_verifiers=[])
        def exercise(r, owner, surface, describer, describe, workflow):
            caps = r.owners.capabilities
            evidence = m._ProducerEvidence(m.BINARY, 'a' * 64,
                frozenset(('paste', 'type-stdin', 'scroll')), time.monotonic_ns(), 128, True, None)
            caps.producer = evidence
            callback = MagicMock(return_value=composition.YES)
            caps.handlers['action', 'paste'] = ('paste', callback, ('private_paste',))
            action = dict(kind='paste')
            self.assertNotEqual(caps.action(action, None), composition.YES)
            original = r.ports
            r.ports = replace(original, private_paste=object(), slots=object())
            owner.ports = r.ports
            try:
                self.assertNotEqual(caps.supports(workflow), composition.YES)
                caps.handlers['focus', None] = ('type-stdin', callback, ('private_paste',))
                self.assertEqual(caps.supports(workflow), composition.YES)
                self.assertEqual(caps.action(action, None), composition.YES)
                callback.assert_called_once_with(action, None)
                callback.reset_mock()
                for operations in (frozenset(('paste',)), frozenset(('type-stdin',))):
                    caps.producer = replace(evidence, operations=operations)
                    self.assertNotEqual(caps.action(action, None), composition.YES)
                caps.producer = evidence
                with patch.object(caps.authorization, 'check', return_value=composition.NO):
                    self.assertNotEqual(caps.action(action, None), composition.YES)
                callback.assert_not_called()
                caps.handlers['action', 'scroll'] = ('scroll', callback, ())
                self.assertNotEqual(caps.action(dict(kind='scroll'), None), composition.YES)
                value = owner.acquire({'capability_id': 'fixture'}, r.config.limits.deadline_ns)
                target = m.GroundedTarget((1, 1), None, value, r.scope, r.authority,
                                          composition.YES, caps, r.config.limits.deadline_ns, (0, 0))
                caps.targets[id(target)] = (target, m.gate.canon(None), None)
                self.assertNotEqual(caps.action(dict(kind='scroll'), target), composition.YES)
                caps.plan = MagicMock()
                self.assertEqual(caps.action(dict(kind='scroll'), target), composition.YES)
                caps.producer = replace(evidence, operations=frozenset(('paste', 'type-stdin')))
                self.assertNotEqual(caps.action(dict(kind='scroll'), target), composition.YES)
                caps.producer = evidence
                with patch.object(caps.authorization, 'check', return_value=composition.NO):
                    self.assertNotEqual(caps.action(dict(kind='scroll'), target), composition.YES)
            finally:
                r.ports = owner.ports = original
        self._observation_control(exercise)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_peak_admission
    def test_peak_admission(self):
        resource = m._Resources(types.SimpleNamespace(resource_capacity=100,
                              limits=types.SimpleNamespace(peak_bytes=100)))
        self.assertEqual(resource.reserve('metadata', 10), composition.YES)
        for kind in ('acquisition_peak', 'target_peak', 'verifier_peak', 'terminal_evidence'):
            self.assertEqual(resource.reserve(kind, 100), composition.YES)
            self.assertNotEqual(resource.reserve(kind, 101), composition.YES)
            self.assertNotEqual(resource.reserve(kind, True), composition.YES)
        self.assertEqual(resource._used, 10)
        self.assertNotEqual(resource.reserve('metadata', 91), composition.YES)
        resource._cancelled = True
        self.assertNotEqual(resource.reserve('acquisition_peak', 100), composition.YES)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_observation_ownership
    def test_default_observation_ownership(self):
        def exercise(r, owner, surface, describer, describe, workflow):
            value = owner.acquire({'capability_id': 'fixture'}, r.config.limits.deadline_ns)
            self.assertIs(type(value), m.ObservationValue)
            self.assertTrue(value.value)
            self.assertEqual(owner.validate(value), composition.YES)
            self.assertEqual(owner.validate(replace(value)), m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            self.assertEqual(owner.validate(replace(value, publication='foreign')),
                             m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            for change in (dict(scope=object()), dict(authority=object()), dict(complete=False),
                           dict(missing_reason=m.Reason.MISSING), dict(capability_revision='foreign'),
                           dict(capability_id='unsupported'), dict(capture_start_ns=value.capture_end_ns + 1)):
                invalid = replace(value, **change)
                owner.registered[value.publication] = invalid
                self.assertEqual(owner.validate(invalid), m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            owner.registered[value.publication] = value
            owner.expiries[value.publication] = 1
            self.assertEqual(owner.validate(value), m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
            owner.expiries[value.publication] = r.config.limits.deadline_ns
            self.assertEqual(owner.validate(value), composition.YES)
            describe.assert_called_once()
            self.assertTrue(describe.call_args.kwargs['ocr_marks'])
            self.published_value = value
        self._observation_control(exercise)
        self.assertNotEqual(self.observation_owner.validate(self.published_value), composition.YES)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_observation_backend
    def test_default_observation_backend(self):
        changes = (
            ('window', lambda f: dict(f, window=dict(id='foreign', w=10, h=10))),
            ('dimensions', lambda f: dict(f, width=11)),
            ('sequence', lambda f: dict(f, seq=-1)),
            ('repeat_sequence', lambda f: dict(f, seq=0)),
            ('ocr_failure', None), ('mark_cap', None), ('missingness', None),
            ('ocr_dimensions', None), ('rgb', None), ('reservation', None),
            ('publication_capacity', None), ('interval', None), ('expiry', None), ('unsupported', None),
            ('surface', None), ('metadata', None),
        )
        for label, change in changes:
            with self.subTest(fixture=label):
                def exercise(r, owner, surface, describer, describe, workflow):
                    predicate = {'capability_id': 'fixture'}
                    deadline = r.config.limits.deadline_ns
                    positive = owner.acquire(predicate, deadline)
                    self.assertEqual(owner.validate(positive), composition.YES)
                    before = surface.frame.call_count
                    if change:
                        original = surface.frame.side_effect
                        surface.frame.side_effect = lambda: change(original())
                    if label == 'ocr_failure':
                        describe.side_effect = ValueError('synthetic OCR refusal')
                    if label == 'mark_cap':
                        describer.change = lambda d: replace(d, mark_cap_reached=True)
                    if label == 'missingness':
                        describer.change = lambda d: replace(d, marks=(replace(d.marks[0], missing_reason='unavailable'),))
                    if label == 'ocr_dimensions':
                        describer.change = lambda d: replace(d, width=11)
                    if label == 'rgb':
                        describer.change = lambda d: replace(d, frame_sha256='0' * 64)
                    if label == 'surface': surface.window = 'foreign'
                    if label == 'metadata':
                        describer.change = lambda d: replace(d, marks=(replace(d.marks[0],
                            ocr_text='x' * r.config.evidence_policy.metadata_bytes),))
                    if label == 'interval':
                        original = surface.frame.side_effect
                        stamp = [owner.ports.clock()]
                        owner.ports = replace(owner.ports, clock=lambda: stamp[0])
                        def expired():
                            result = original()
                            stamp[0] -= 1
                            return result
                        surface.frame.side_effect = expired
                    if label == 'expiry':
                        stamp = [owner.ports.clock()]
                        owner.ports = replace(owner.ports, clock=lambda: stamp[0])
                        original_describe = describe.side_effect
                        def expire_ocr(*args, **kwargs):
                            result = original_describe(*args, **kwargs)
                            stamp[0] = deadline
                            return result
                        describe.side_effect = expire_ocr
                    if label in ('reservation', 'publication_capacity', 'unsupported'):
                        if label == 'reservation': owner.retained = owner.ports.limits.retained_bytes
                        if label == 'publication_capacity':
                            owner.ports = replace(owner.ports,
                                limits=replace(owner.ports.limits, acquisitions=len(owner.registered)))
                        if label == 'unsupported': predicate = {'capability_id': 'unknown'}
                        with self.assertRaises(m.ProfileError) as caught:
                            owner.acquire(predicate, deadline)
                        self.assertEqual(caught.exception.category, 'OUT_OF_SUPPORT')
                        self.assertEqual(surface.frame.call_count, before)
                    else:
                        invalid = owner.acquire(predicate, deadline)
                        self.assertIs(type(invalid), m.ObservationValue)
                        self.assertFalse(invalid.complete)
                        self.assertEqual(invalid.missing_reason, m.Reason.MISSING)
                        self.assertNotEqual(owner.validate(invalid), composition.YES)
                        if label == 'mark_cap':
                            self.assertIs(owner.evidence[invalid.publication][0], True)
                        if label == 'missingness':
                            self.assertIs(owner.evidence[invalid.publication][1], True)
                self._observation_control(exercise)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_capability_handlers
    def test_default_capability_handlers(self):
        def exercise(r, owner, surface, describer, describe, workflow):
            caps = r.owners.capabilities
            caps.producer = m._ProducerEvidence(m.BINARY, 'a' * 64, frozenset(('drag',)),
                time.monotonic_ns(), 128, True, None)
            spec = {'capability_id': 'fixture'}
            value = owner.acquire(spec, r.config.limits.deadline_ns)
            self.assertEqual(owner.validate(value), composition.YES)
            # An independently acquired semantic publication is required by final().
            wfkey = workflow.get('capability_id', workflow.get('id'))
            calls = [
                ('supports', wfkey, lambda: caps.supports(workflow)),
                ('focus', 'fixture', lambda: caps.focus(spec, object())),
                ('action', 'fixture', lambda: caps.action(spec, None)),
                ('verifier', 'fixture', lambda: caps.verifier(spec)),
                ('equivalent', 'fixture', lambda: caps.equivalent(spec, {})),
                ('satisfaction', 'fixture', lambda: caps.satisfaction(spec, object())),
                ('changed', 'fixture', lambda: caps.changed(spec, value, value)),
                ('final', wfkey, lambda: caps.final(workflow)),
            ]
            for method, key, call in calls:
                self.assertEqual(call(), m.AdmissionCheck(m.Truth.FALSE, m.Reason.MISSING))
                caps.handlers[method, key] = ('drag', lambda *args: composition.YES, ('native_read',))
                if method == 'final':
                    self.assertEqual(call(), m.AdmissionCheck(m.Truth.FALSE, m.Reason.MISSING))
                    caps.handlers[method, key] = ('drag', lambda *args: value, ('native_read',))
                self.assertEqual(call(), composition.YES)
                evidence = caps.producer
                caps.producer = replace(evidence, operations=frozenset())
                self.assertEqual(call(), m.AdmissionCheck(m.Truth.FALSE, m.Reason.MISSING))
                caps.producer = evidence
                handler = caps.handlers.pop((method, key))
                self.assertEqual(call(), m.AdmissionCheck(m.Truth.FALSE, m.Reason.MISSING))
                caps.handlers[method, key] = handler
                for operation in ('paste', 'scroll', 'type-stdin'):
                    caps.producer = replace(evidence, operations=frozenset((operation,)))
                    caps.handlers[method, key] = (operation, handler[1], ())
                    self.assertEqual(call(), m.AdmissionCheck(m.Truth.FALSE, m.Reason.MISSING))
                caps.producer = evidence
                caps.handlers[method, key] = handler
            caps.handlers['verifier', 'fixture'] = ('drag', lambda *args: composition.YES, ('ram',))
            self.assertEqual(caps.verifier(spec), m.AdmissionCheck(m.Truth.FALSE, m.Reason.MISSING))
            caps.handlers['verifier', 'fixture'] = ('drag', lambda *args: composition.YES, ())
            r.active = False
            self.assertEqual(caps.verifier(spec), m.AdmissionCheck(m.Truth.FALSE, m.Reason.MISSING))
            r.active = True
            self.assertEqual(caps.verifier(spec), composition.YES)
            # Ordinary targets retain exact object identity; copied targets refuse.
            state = object()
            grounded = m.GroundedTarget((1, 1), r.config.destination, value, r.scope, r.authority,
                composition.YES, caps, r.config.limits.deadline_ns, (0, 0))
            caps.handlers['target', 'fixture'] = ('drag', lambda *args: grounded, ('native_read',))
            caps.handlers['validate_target', 'fixture'] = ('drag', lambda *args: composition.YES, ())
            self.assertIs(caps.target(spec, state, r.config.limits.deadline_ns), grounded)
            self.assertEqual(caps.validate_target(spec, grounded, state), composition.YES)
            self.assertEqual(caps.validate_target(spec, replace(grounded), state),
                             m.AdmissionCheck(m.Truth.FALSE, m.Reason.MISSING))
        self._observation_control(exercise)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_download_observations
    def test_default_download_observations(self):
        def exercise(r, owner, surface, describer, describe, workflow):
            caps = r.owners.capabilities
            caps.producer = m._ProducerEvidence(m.BINARY, 'a' * 64, frozenset(('downloads',)),
                time.monotonic_ns(), 128, True, None)
            predicate = {'capability_id': 'fixture-downloads'}
            owner.downloads['fixture-downloads'] = ('identities', None)
            native = owner.ports.native_read
            rows = [dict(name='synthetic-A', bytes=1, mtime=1)]
            with patch.object(native, 'read', return_value=rows) as read:
                with self.assertRaises(m.ProfileError): owner.acquire(predicate, r.config.limits.deadline_ns)
                read.assert_not_called()
                owner.downloads['fixture-downloads'] = ('identities', lambda *args: composition.YES)
                first = owner.acquire(predicate, r.config.limits.deadline_ns)
                self.assertEqual(owner.validate(first), composition.YES)
                comparison = dict(predicate, comparison='set_grew', snapshot_ref='before')
                snapshots = {'before': composition.w.CapturedSnapshot('before', first, 256)}
                consumer = types.SimpleNamespace(scope=r.scope, authority=r.authority,
                    clock=owner.ports.clock, observations=owner, capabilities=caps)
                read.assert_called_with('downloads', {}, r.config.limits.deadline_ns)
                read.return_value = [dict(rows[0], bytes=20, mtime=2)]
                rewritten = owner.acquire(predicate, r.config.limits.deadline_ns)
                self.assertEqual(first.value, rewritten.value)
                self.assertIs(composition.w.evaluate_predicate(comparison, observation=rewritten,
                    snapshots=snapshots, owners=consumer), m.Truth.FALSE)
                read.return_value = rows + [dict(name='synthetic-B', bytes=1, mtime=1)]
                grown = owner.acquire(predicate, r.config.limits.deadline_ns)
                self.assertTrue(grown.value > first.value)
                self.assertIs(composition.w.evaluate_predicate(comparison, observation=grown,
                    snapshots=snapshots, owners=consumer), m.Truth.TRUE)
                self.assertNotEqual(owner.validate(replace(grown)), composition.YES)
                downstream = owner.download_owners['fixture-downloads']
                downstream._gate = lambda *args: composition.NO
                with self.assertRaisesRegex(ValueError, 'OUT_OF_SUPPORT'):
                    owner.acquire(predicate, r.config.limits.deadline_ns)
                downstream._gate = lambda *args: composition.YES
                for invalid, label in (([dict(name=str(i), bytes=1, mtime=1) for i in range(257)], 'rows'),
                    ([dict(name='x' * 1500 + str(i), bytes=1, mtime=1) for i in range(200)], 'bytes')):
                    read.return_value = invalid
                    # Independently exercise the profile inventory gates through acquire().
                    with patch.object(owners.DownloadsCodec, 'decode', return_value=frozenset()), \
                         self.assertRaises(m.ProfileError) as caught:
                            owner.acquire(predicate, r.config.limits.deadline_ns)
                    self.assertEqual(caught.exception.category, 'OUT_OF_SUPPORT', label)
                read.reset_mock()
                caps.producer = replace(caps.producer, operations=frozenset())
                with self.assertRaises(m.ProfileError): owner.acquire(predicate, r.config.limits.deadline_ns)
                read.assert_not_called()
        self._observation_control(exercise)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_real_live_runner
    def test_default_real_live_runner(self):
        self.c.runner.side_effect = None
        self.t.b.path.write_bytes((m.gate.canon(self.t.b.workflow) + '\n').encode())
        self.t.b.path.chmod(0o600)
        self.invocation = m.make_invocation(self.profile, workflow=self.t.b.path,
            output=self.invocation.output, mode=self.invocation.mode)
        with self._without_provider(), patch.object(m.shutil, 'which', return_value='/synthetic/tool'), \
             patch.dict(os.environ, VQA_VE_ROOT=str(self.c.root)), \
             patch.object(Path, 'home', return_value=self.c.root), \
             patch.object(m, '_LEASE_ROOT', self.c.root / 'leases'), \
             patch.object(m, 'SrfIdentitySurface', side_effect=composition._Surface) as surface, \
             patch.object(live.m, 'LiveOCRDescriber', return_value=self.t.describer), \
             patch.object(live.m, 'MuseLocateTransport', return_value=self.t.model), redirect_stdout(io.StringIO()):
            result = m.run_profile(self.profile, self.invocation, bundle=composition.base.BUNDLE, services=None)
        # Unsupported semantic evidence refuses, after real live capacity admission.
        surface.assert_called_once()
        self.assertNotEqual(result.rc, 0)
        self.c.runner.assert_called_once()
        self.assertFalse(tuple(self.root.iterdir()))
        notice = self.c.root / '.local/state/memoryweb/notices.jsonl'
        self.assertEqual([json.loads(row)['phase'] for row in notice.read_text().splitlines()], ['start', 'end'])

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_duration_values
    def test_default_duration_values(self):
        expected = dict(run_ns=1_800_000_000_000, step_ns=60_000_000_000,
            generation_wait_ns=420_000_000_000, locate_calls=8,
            locate_call_ns=30_000_000_000, ocr_control_ns=10_000_000_000,
            drain_ns=10_000_000_000)
        def activity(ports, args):
            config = args['surface_factory'].__self__.config
            self.assertEqual(vars(config.durations), expected)
            self.assertEqual(ports.limits.deadline_ns, 123_000 + expected['run_ns'])
            self.assertEqual(args['locator_policy'].absolute_deadline_ns, ports.limits.deadline_ns)
            self.assertEqual(args['locator_policy'].max_calls, expected['locate_calls'])
            self.assertEqual(args['locator_policy'].call_ns, expected['locate_call_ns'])
            self.assertEqual(ports.limits.drain_ns, expected['drain_ns'])
            self.assertEqual(ports.native_read.policy.join_ns, expected['drain_ns'])
        self.action = activity
        with self._synthetic_default_capacity(), patch.object(m.time, 'monotonic_ns', return_value=123_000):
            self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_clock_relative_deadlines
    def test_clock_relative_deadlines(self):
        deadlines = []
        for start in (123_000, 456_000):
            with self._synthetic_default_capacity(), patch.object(m.time, 'monotonic_ns', return_value=start):
                self.assertEqual(self._run().rc, 4)
                deadlines.append((self.ports.limits.deadline_ns,
                                  self.arguments['locator_policy'].absolute_deadline_ns))
        self.assertEqual(tuple(b-a for a,b in zip(*deadlines)), (333_000, 333_000))
        self.policy = replace(self.policy, locator_policy=replace(self.policy.locator_policy,
                              absolute_deadline_ns=1))
        with self.assertRaises(m.ProfileError): self._run()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_clock_type_before_arithmetic
    def test_clock_type_before_arithmetic(self):
        invalid = MagicMock()
        with patch.object(m.time, 'monotonic_ns', return_value=invalid):
            with self.assertRaises(m.ProfileError): self._run()
        invalid.__rsub__.assert_not_called()
        self.c.builder.assert_not_called()
        self.c.runner.assert_not_called()
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_executable_and_override
    def test_default_executable_and_override(self):
        with self._synthetic_default_capacity(), patch.object(m.shutil, 'which', return_value=None):
            with self.assertRaises(m.ProfileError): self._run()
        self.c.builder.assert_not_called()
        with m.profile_host(lambda *a, **k: None), patch.object(m, '_trusted_default_configuration') as default:
            with self.assertRaises(m.ProfileError): self._run()
            default.assert_not_called()
        with self._synthetic_default_capacity(): self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_composition
    def test_default_composition(self):
        self.assertEqual(self._run().rc, 4)
        self.c.builder.assert_called_once()
        self.c.runner.assert_called_once()
        self.assertEqual(set(self.c.builder.call_args.kwargs),
            set(inspect.signature(owners.build_live_ports).parameters) - {'invocation'})
        self.assertFalse(self.records[-1]['workspace'].exists())
        self.assertIsNone(self.ports.actor_lock.holder)
        self.c.spawn.assert_not_called()
        first = self.ports
        self.assertEqual(self._run().rc, 4)
        self.assertIsNot(first.scope, self.ports.scope)
        self.assertIsNot(first.authority, self.ports.authority)
        self.assertNotEqual(first.run_id, self.ports.run_id)
        self.assertNotIn('SYNTHETIC_ACK', repr(self.policy))

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_lifetime_and_wait
    def test_lifetime_and_wait(self):
        def activity(ports, args):
            self.record = args['surface_factory'].__self__
            self.assertNotIn('SYNTHETIC_ACK', repr(self.record))
            self.assertIs(type(ports.clock()), int)
            with patch.object(m.time, 'sleep') as sleep:
                ports.sleep(1)
                sleep.assert_called_once()
                for duration in (True, 0, -1, self.policy.limits.deadline_ns):
                    with self.assertRaises(m.ProfileError): ports.sleep(duration)
            with self.assertRaises(m.ProfileError):
                self.record._dependencies(self.profile, replace(self.invocation))
        self.action = activity
        self.assertEqual(self._run().rc, 4)
        with self.assertRaises(m.ProfileError): self.ports.sleep(1)
        with self.assertRaises(m.ProfileError): self.record._dependencies(self.profile, self.invocation)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_default_error_privacy
    def test_default_error_privacy(self):
        def failure(*args, **kwargs):
            raise ValueError('SYNTHETIC_ACK PRIVATE_SLOT_CANARY')
        self.policy = replace(self.policy, owners=failure)
        with self.assertRaises(m.ProfileError) as caught: self._run()
        self.assertNotIn('SYNTHETIC_ACK', ''.join(traceback.format_exception(caught.exception)))
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(self.c._args(), profile_dir=self.c.root), 5)
        self.assertEqual(json.loads(out.getvalue()), {'rc': 5, 'verdict_code': 'OUT_OF_SUPPORT'})
        self.assertNotIn('PRIVATE_SLOT_CANARY', out.getvalue() + err.getvalue())
        self.assertFalse(tuple(self.root.iterdir()))
        self.c.builder.assert_not_called()
        self.c.runner.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_complete_dependencies
    def test_complete_dependencies(self):
        original = m._Provisioning._dependencies
        required = set(inspect.signature(owners.build_live_ports).parameters) - {'invocation'}
        for key in sorted(required) + ['unknown']:
            def changed(record, profile, invocation):
                value = original(record, profile, invocation)
                if key == 'unknown': value[key] = object()
                else: value.pop(key)
                return value
            with self.subTest(key=key), patch.object(m._Provisioning, '_dependencies', changed):
                with self.assertRaises(m.ProfileError): self._run()
                self.c.builder.assert_not_called()
                self.c.runner.assert_not_called()
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_shell_default
    def test_shell_default(self):
        with self._synthetic_default_capacity(), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            result = cli.main(self.c._args(), profile_dir=self.c.root)
        self.assertEqual(result, 4)
        self.assertEqual(json.loads(out.getvalue()), {'rc': 4, 'verdict_code': 'HUMAN_HANDOFF'})
        self.assertEqual(err.getvalue(), '')
        self.c.runner.assert_called_once()
        self.c.spawn.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_surface_mapping
    def test_surface_mapping(self):
        def activity(ports, args):
            kwargs = dict(identity=self.invocation.identity, container=self.invocation.container,
                window=self.invocation.window, identity_env=self.invocation.identity_env,
                timeout=0.1, transport=object(), trace=None)
            for key in ('container', 'window', 'identity', 'identity_env', 'trace'):
                wrong = dict(kwargs, **{key: 'foreign'})
                with self.assertRaises(m.ProfileError): args['surface_factory'](**wrong)
            args['surface_factory'](**kwargs)
            self.assertEqual(self.surfaces, [dict(kwargs, ram_dir=str(args['workspace']))])
            self.assertTrue(args['workspace'].is_relative_to(m._RAM_ROOT))
            self.assertEqual(args['workspace'].stat().st_mode & 0o777, 0o700)
        self.action = activity
        with patch.dict(os.environ, VQA_VE_ROOT='/synthetic/ve'):
            self.assertEqual(self._run().rc, 4)
        self.assertEqual(self.arguments['ve_root'], '/synthetic/ve')

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_announcement
    def test_announcement(self):
        def activity(ports, args):
            kw = dict(scope=ports.scope, authority=ports.authority, now_ns=ports.clock(),
                      absolute_deadline_ns=ports.limits.deadline_ns)
            notice = ports.announce_gate
            for key, value in (('scope', object()), ('authority', object()),
                               ('absolute_deadline_ns', ports.clock() - 1)):
                self.assertNotEqual(notice(self.invocation, **dict(kw, **{key: value})), composition.YES)
            self.assertNotEqual(notice(replace(self.invocation), **kw), composition.YES)
            original = self.policy
            record = args['surface_factory'].__self__
            malformed = copy.copy(composition.YES)
            object.__setattr__(malformed, 'reason', m.Reason.STALE)
            for key in ('notice', 'lease'):
                for invalid in (True, m.AdmissionCheck(m.Truth.UNKNOWN, m.Reason.UNKNOWN), malformed):
                    record.config = replace(original, **{key: lambda **kw: invalid})
                    self.assertNotEqual(notice(self.invocation, **kw), composition.YES)
            record.config = original
            for flag in ('notice_ok', 'lease_ok'):
                setattr(self, flag, False)
                self.assertNotEqual(notice(self.invocation, **kw), composition.YES)
                setattr(self, flag, True)
            self.assertEqual(notice(self.invocation, **kw), composition.YES)
            for text in (b'CHANGED', b'', b'X' * 4097, self.c.token):
                self.c._private(self.c.ack, text)
                self.assertNotEqual(notice(self.invocation, **kw), composition.YES)

        self.action = activity
        self.assertEqual(self._run().rc, 4)
        # No surface construction was needed to establish these predicates.
        self.assertFalse(self.surfaces)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_host_policy
    def test_host_policy(self):
        original = self.policy
        cases = [replace(original, limits=None), replace(original, resource_capacity=True),
            replace(original, resource_capacity=original.limits.peak_bytes - 1),
            replace(original, locator_policy=replace(original.locator_policy, poll_ns=True)),
            replace(original, locator_policy=replace(original.locator_policy, call_ns=99_999_999, poll_ns=100_000_000)),
            replace(original, locator_policy=replace(original.locator_policy, peak_bytes=1)),
            replace(original, limits=replace(original.limits, peak_bytes=1)),
            replace(original, locator_policy=replace(original.locator_policy, absolute_deadline_ns=1)),
            replace(original, limits=replace(original.limits, deadline_ns=1)),
            replace(original, locator_policy=replace(original.locator_policy, retained_bytes=1)),
            replace(original, limits=replace(original.limits, retained_bytes=1)),
            replace(original, locator_policy=replace(original.locator_policy, request_bytes=256 * 1024)),
            replace(original, evidence_policy=replace(original.evidence_policy,
                decode_limits=replace(original.evidence_policy.decode_limits, png_bytes=original.locator_policy.image_bytes + 1))),
            replace(original, native_policy=replace(original.native_policy, publications=1)),
            replace(original, locator_policy=replace(original.locator_policy,
                absolute_deadline_ns=original.limits.deadline_ns + 1)),
            replace(original, limits=replace(original.limits, deadline_ns=time.monotonic_ns() + 1_000_000_000),
                locator_policy=replace(original.locator_policy,
                    absolute_deadline_ns=time.monotonic_ns() + 500_000_000, call_ns=2_000_000_000))]
        # Missing and boolean fields in every nested policy must reach the public
        # boundary as malformed exact instances, not fail fixture construction.
        for key in ('limits', 'native_policy', 'locator_policy', 'evidence_policy'):
            value = getattr(original, key)
            for field in m.fields(value):
                if field.name == 'decode_limits': continue
                for invalid in (True, None, 0, -1):
                    changed = copy.copy(value)
                    object.__setattr__(changed, field.name, invalid)
                    cases.append(replace(original, **{key: changed}))
        for field in m.fields(original.evidence_policy.decode_limits):
            for invalid in (True, None, 0, -1):
                changed = copy.copy(original.evidence_policy.decode_limits)
                object.__setattr__(changed, field.name, invalid)
                cases.append(replace(original, evidence_policy=replace(original.evidence_policy, decode_limits=changed)))
        for config in cases:
            self.policy = config
            with self.subTest(config=config), self.assertRaises(m.ProfileError): self._run()
            self.c.builder.assert_not_called()
            self.c.runner.assert_not_called()
            self.c.spawn.assert_not_called()
            self.assertEqual(self.records, [])
        self.policy = original
        with patch.dict(m.gate.LIMITS, workflow=original.limits.workflow_bytes - 1):
            with self.assertRaises(m.ProfileError): self._run()
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning._capacity_baseline
    def _capacity_baseline(self, key, name, ceiling, now):
        # Raise dependent allowances, preserving every aggregate predicate. This
        # is a synthetic per-field boundary control, not a deployment vector.
        config = replace(self.policy,
            limits=replace(self.policy.limits, deadline_ns=now + 1_800_000_000_000),
            locator_policy=replace(self.policy.locator_policy, absolute_deadline_ns=now + 1_800_000_000_000))
        if key == 'decode_limits':
            config = replace(config, evidence_policy=replace(config.evidence_policy,
                decode_limits=replace(config.evidence_policy.decode_limits, **{name: ceiling})))
        elif key == 'resource_capacity':
            config = replace(config, resource_capacity=ceiling)
        else:
            config = replace(config, **{key: replace(getattr(config, key), **{name: ceiling})})
        w, n, p, c = config.limits, config.native_policy, config.locator_policy, config.evidence_policy
        p = replace(p, image_bytes=max(p.image_bytes, c.decode_limits.png_bytes),
                    call_ns=max(p.call_ns, p.poll_ns))
        p = replace(p, retained_bytes=max(p.retained_bytes, 2 * p.image_bytes + c.metadata_bytes))
        p = replace(p, peak_bytes=max(p.peak_bytes, 2 * p.image_bytes + p.request_bytes +
                    3 * p.response_bytes + p.stderr_bytes + p.retained_bytes))
        p = replace(p, max_calls=min(p.max_calls, (64 * 1024 * 1024) // p.retained_bytes))
        w = replace(w, acquisitions=max(w.acquisitions, p.max_calls),
                    retained_bytes=max(w.retained_bytes, p.max_calls * p.retained_bytes),
                    peak_bytes=max(w.peak_bytes, p.peak_bytes + 8 * p.image_bytes + c.decode_limits.memory_bytes +
                        c.ve_process_bytes + c.ve_stdout_bytes + c.ve_stderr_bytes + c.metadata_bytes +
                        (p.max_calls - 1) * p.retained_bytes))
        return replace(config, limits=w, locator_policy=p,
                       native_policy=replace(n, publications=max(n.publications, p.max_calls)),
                       resource_capacity=max(config.resource_capacity, w.peak_bytes))

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_capacity_field_sweep
    def test_capacity_field_sweep(self):
        original = self.policy
        caps = m._capacity_ceilings()
        # The approved image boundary is an independent contract, not the default.
        caps = replace(caps, locator_policy=replace(caps.locator_policy, image_bytes=8 * 1024 * 1024),
            evidence_policy=replace(caps.evidence_policy,
                decode_limits=replace(caps.evidence_policy.decode_limits, png_bytes=8 * 1024 * 1024)))
        now = time.monotonic_ns()
        need = m._need
        failures = []
        def checked(ok, name, rc=5):
            if not ok:
                failures.append(name)
            return need(ok, name, rc)
        def reject(config, label):
            self.policy = config
            failures.clear()
            with patch.object(m, '_need', checked), patch.object(m.time, 'monotonic_ns', return_value=now), \
                 self.c._no_workspace():
                with self.assertRaises(m.ProfileError): self._run()
                self.c.builder.assert_not_called()
            self.c.runner.assert_not_called()
            self.assertFalse(self.surfaces)
            self.assertFalse(self.records)
            # An aggregate/deadline rejection cannot stand in for a missing cap.
            self.assertEqual(failures[:1], [label])
        def baseline(key, name, ceiling):
            self.policy = original
            config = self._capacity_baseline(key, name, ceiling, now)
            self.policy = config
            with patch.object(m.time, 'monotonic_ns', return_value=now):
                self.assertEqual(self._run().rc, 4)
            self.c.builder.reset_mock()
            self.c.runner.reset_mock()
            self.records.clear()
            return config
        for key in ('limits', 'native_policy', 'locator_policy', 'evidence_policy', 'durations'):
            base, cap = getattr(original, key), getattr(caps, key)
            for field in m.fields(base):
                if field.name == 'decode_limits':
                    continue
                ceiling = getattr(cap, field.name)
                if field.name in ('deadline_ns', 'absolute_deadline_ns'):
                    ceiling = now + caps.durations.run_ns
                control = baseline(key, field.name, ceiling)
                label = 'capacity_' + ('duration_' if key == 'durations' else '') + field.name
                for invalid in (0, -1, True, None, ceiling + 1):
                    value = copy.copy(getattr(control, key))
                    object.__setattr__(value, field.name, invalid)
                    with self.subTest(policy=key, field=field.name, invalid=invalid):
                        reject(replace(control, **{key: value}),
                               label if invalid == ceiling + 1 else 'host_policy_integers')
        for field in m.fields(original.evidence_policy.decode_limits):
            ceiling = getattr(caps.evidence_policy.decode_limits, field.name)
            control = baseline('decode_limits', field.name, ceiling)
            for invalid in (0, -1, True, None, ceiling + 1):
                value = copy.copy(control.evidence_policy.decode_limits)
                object.__setattr__(value, field.name, invalid)
                with self.subTest(policy='decode_limits', field=field.name, invalid=invalid):
                    reject(replace(control, evidence_policy=replace(control.evidence_policy, decode_limits=value)),
                           'capacity_' + field.name if invalid == ceiling + 1 else 'host_policy_integers')
        control = baseline('resource_capacity', None, caps.resource_capacity)
        for invalid in (0, -1, True, None, caps.resource_capacity + 1):
            reject(replace(control, resource_capacity=invalid),
                   'capacity_resources' if invalid == caps.resource_capacity + 1 else 'host_policy_integers')
        self.c.builder.assert_not_called()
        self.c.runner.assert_not_called()
        self.assertFalse(self.records)
        self.policy = original
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_capacity_contradiction
    def test_capacity_contradiction(self):
        # Independently exercise both unchanged aggregate gates within field caps.
        original = self.policy
        policy = original.locator_policy
        evidence = original.evidence_policy
        limits = replace(original.limits, retained_bytes=1)
        self.policy = replace(original, limits=limits, locator_policy=policy, evidence_policy=evidence)
        with self.c._no_workspace():
            with self.assertRaises(m.ProfileError):
                self._run()
        self.c.builder.assert_not_called()
        self.c.runner.assert_not_called()
        self.assertFalse(self.records)
        # Exercise the unchanged live runner's own capacity gate, independently.
        with patch.object(live.m, 'open_srf_owners_with_locator') as native:
            try:
                live.m.run_live_workflow(self.invocation, bundle=composition.base.BUNDLE,
                    ports=replace(self.t.ports, limits=limits), locator_policy=policy,
                    evidence_policy=evidence, trust=self.t.trust, workspace=self.t.root,
                    executable='unused', describer=self.t.describer, locate_transport=self.t.model)
            except (ValueError, TypeError, AttributeError) as error:
                self.assertEqual(str(error), 'LIVE_EVIDENCE:budgets')
            else:
                self.fail('Contradictory vector admitted')
            native.assert_not_called()
        # Preserve the superseded conductor's explicit multi-field contradiction.
        policy = replace(original.locator_policy, max_calls=8, image_bytes=8 * 1024 * 1024,
            peak_bytes=128 * 1024 * 1024, retained_bytes=16 * 1024 * 1024 + 65536)
        evidence = replace(original.evidence_policy, ve_process_bytes=4096 * 1024 * 1024,
            decode_limits=replace(original.evidence_policy.decode_limits, png_bytes=8 * 1024 * 1024))
        limits = replace(original.limits, retained_bytes=64 * 1024 * 1024, peak_bytes=512 * 1024 * 1024)
        self.policy = replace(original, limits=limits, locator_policy=policy, evidence_policy=evidence)
        with self.c._no_workspace(), self.assertRaises(m.ProfileError):
            self._run()
        with patch.object(live.m, 'open_srf_owners_with_locator') as native:
            with self.assertRaisesRegex(ValueError, 'LIVE_EVIDENCE:budgets'):
                live.m.run_live_workflow(self.invocation, bundle=composition.base.BUNDLE,
                    ports=replace(self.t.ports, limits=limits), locator_policy=policy,
                    evidence_policy=evidence, trust=self.t.trust, workspace=self.t.root,
                    executable='unused', describer=self.t.describer, locate_transport=self.t.model)
            native.assert_not_called()
        # The same profile API admits a jointly valid synthetic host policy.
        self.policy = original
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_cleanup_preserves_failure
    def test_cleanup_preserves_failure(self):
        calls = []
        def broken(**kwargs):
            self.records.append(kwargs)
            raise m.ProfileError(2)
        self.policy = replace(self.policy, owners=broken,
            cancel=lambda timeout: calls.append('cancel') or composition.NO,
            drain=lambda timeout: calls.append('drain') or composition.YES)
        with self.assertRaises(m.ProfileError) as caught:
            self._run()
        self.assertEqual(caught.exception.rc, 2)
        self.assertEqual(calls, ['cancel', 'drain'])
        retained = self.records[-1]['workspace']
        self.assertTrue(retained.exists())
        retained.rmdir()  # Synthetic recovery after the assertions only.

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_workspace
    def test_workspace(self):
        # All mutations are restricted to a private RAM fixture root.
        self.root.chmod(0o777)
        with self.assertRaises(m.ProfileError): self._run()
        # An owned shared root is allowed; the invocation child stays private.
        self.root.chmod(0o775)
        self.assertEqual(self._run().rc, 4)
        self.c.builder.reset_mock()
        self.c.runner.reset_mock()
        self.root.chmod(0o700)
        actual = m.os.stat
        def foreign(path, *args, **kwargs):
            st = actual(path, *args, **kwargs)
            if Path(path) == self.root:
                return types.SimpleNamespace(**{key: getattr(st, key) for key in ('st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}, st_uid=st.st_uid + 1)
            return st
        with patch.object(m.os, 'stat', side_effect=foreign), self.assertRaises(m.ProfileError): self._run()
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with patch.object(m, '_RAM_ROOT', link), self.assertRaises(m.ProfileError): self._run()
        mount_open = open
        def persistent(path, *args, **kwargs):
            if str(path) == '/proc/self/mountinfo':
                return io.StringIO('1 0 0:1 / / rw - ext4 fixture rw\n')
            return mount_open(path, *args, **kwargs)
        with patch('builtins.open', side_effect=persistent), self.assertRaises(m.ProfileError): self._run()
        self.c.builder.assert_not_called()
        self.c.runner.assert_not_called()
        actual_mkdir = Path.mkdir
        children = []
        def unsafe_child(path, *a, **kw):
            actual_mkdir(path, *a, **kw)
            if path.parent == self.root:
                path.chmod(0o755)
                children.append(path)
        try:
            with patch.object(Path, 'mkdir', unsafe_child):
                with self.assertRaises(m.ProfileError): self._run()
            self.c.builder.assert_not_called()
        finally:
            for child in children:
                child.chmod(0o700)
                child.rmdir()
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_resources_and_cleanup
    def test_resources_and_cleanup(self):
        def activity(ports, args):
            resource = ports.resources
            cap = self.policy.resource_capacity
            self.assertEqual(resource.reserve('synthetic', cap), composition.YES)
            for amount in (1, True, -1):
                self.assertNotEqual(resource.reserve('synthetic', amount), composition.YES)
            self.assertEqual(resource.cancel(self.policy.limits.cancel_ns), composition.YES)
            self.join_ok = False
            self.assertNotEqual(resource.drain(self.policy.limits.drain_ns), composition.YES)
            self.assertEqual(resource._used, cap)
            self.join_ok = True
            self.assertEqual(resource.drain(self.policy.limits.drain_ns), composition.YES)
            self.assertEqual(resource._used, 0)
        self.action = activity
        self.assertEqual(self._run().rc, 4)
        self.action = lambda ports, args: setattr(self, 'join_ok', False)
        with self.assertRaises(m.ProfileError): self._run()
        retained = self.records[-1]['workspace']
        self.assertTrue(retained.exists())
        # Explicit fixture recovery; failed production drain never erases custody.
        self.join_ok = True
        self.assertEqual(self.ports.resources.drain(self.policy.limits.drain_ns), composition.YES)
        retained.rmdir()

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_artifact
    def test_artifact(self):
        def activity(ports, args):
            owner = ports.artifact
            with owner.pin(self.invocation.workflow) as pin:
                self.assertEqual(owner.check(pin), composition.YES)
                self.assertNotEqual(owner.check(replace(pin)), composition.YES)
            with owner.pin(self.invocation.workflow) as pin:
                self.c._private(self.invocation.workflow, b'{"changed":true}\n')
                self.changed_artifact = owner.check(pin)
                self.assertEqual(self.changed_artifact, m.AdmissionCheck(m.Truth.FALSE, m.Reason.STALE))
        self.action = activity
        self.assertEqual(self._run().rc, 4)
        self.assertNotEqual(self.changed_artifact, composition.YES)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_evidence_privacy
    def test_evidence_privacy(self):
        def activity(ports, args):
            binding = types.SimpleNamespace(scope=ports.scope, authority=ports.authority)
            self.approved_binding = binding
            observation = types.SimpleNamespace(scope=ports.scope, authority=ports.authority,
                value=binding, complete=True, missing_reason=None)
            frame, marks = args['workspace'] / 'frame', args['workspace'] / 'marks'
            for path in (frame, marks):
                path.write_bytes(b'SYNTHETIC_PIXELS')
                path.chmod(0o600)
                self.approved_files[path] = (path.stat().st_ino, path.read_bytes())
            kw = dict(scope=ports.scope, authority=ports.authority, observation=observation,
                description=object(), semantic_target='synthetic', frame_path=frame, marks_path=marks,
                deadline_ns=ports.limits.deadline_ns)
            trust = args['trust']
            self.assertEqual(trust.export_check(**kw), composition.YES)
            for key, value in (('scope', object()), ('authority', object()), ('deadline_ns', 1)):
                self.assertNotEqual(trust.export_check(**dict(kw, **{key: value})), composition.YES)
            persistent = self.c.root / 'persistent-frame'
            persistent.write_bytes(b'SYNTHETIC_PIXELS')
            self.assertNotEqual(trust.export_check(**dict(kw, frame_path=persistent)), composition.YES)
            for key, value in (('complete', False), ('missing_reason', m.Reason.MISSING)):
                bad = types.SimpleNamespace(**dict(vars(observation), **{key: value}))
                self.assertNotEqual(trust.export_check(**dict(kw, observation=bad)), composition.YES)
            frame.write_bytes(b'CHANGED')
            self.assertNotEqual(trust.export_check(**kw), composition.YES)
            frame.write_bytes(b'SYNTHETIC_PIXELS')
            self.assertEqual(trust.custody_check(scope=ports.scope, authority=ports.authority, binding=binding,
                frame_path=frame, marks_path=marks, deadline_ns=ports.limits.deadline_ns), composition.YES)
            support = dict(target={}, observation=observation, description=object(), mark=object(), point=(0, 0),
                step=object(), scope=ports.scope, authority=ports.authority,
                destination=trust.destination, deadline_ns=ports.limits.deadline_ns)
            self.assertEqual(trust.support_check(**support), composition.YES)
            self.assertNotEqual(trust.support_check(**dict(support, destination=object())), composition.YES)
            self.approved_binding = None
            self.assertNotEqual(trust.export_check(**kw), composition.YES)
            frame.unlink()
            marks.unlink()
        self.action = activity
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_host_binding
    def test_host_binding(self):
        with self.assertRaises(m.ProfileError):
            with m.profile_host(None): pass
        with m.profile_host(self._configuration):
            self.assertEqual(self._run().rc, 4)
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_host_callbacks
    def test_host_callbacks(self):
        original = self.policy
        cases = [('executable', 'relative'), ('capability_revision', ''),
                 ('supported_locales', ()), ('supported_locales', ('',)),
                 ('supported_locales', ['en'])]
        cases += [(key, None) for key in ('owners', 'notice', 'lease', 'export_check',
                                         'custody_check', 'support_check', 'cancel', 'drain')]
        for key, value in cases:
            self.policy = replace(original, **{key: value})
            with self.subTest(field=key), self.assertRaises(m.ProfileError): self._run()
            self.c.builder.assert_not_called()
        self.policy = original
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_owner_type
    def test_owner_type(self):
        original = self.policy
        # Structurally usable foreign type; removing the type gate reaches runner.
        foreign = type('ForeignOwners', (m._HostOwners,), {})
        self.policy = replace(original, owners=lambda **kw: foreign(**vars(self._owners(**kw))))
        with self.assertRaises(m.ProfileError): self._run()
        self.c.builder.assert_not_called()
        self.policy = original
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_artifact_binding_and_canonical
    def test_artifact_binding_and_canonical(self):
        def activity(ports, args):
            foreign = self.c.root / 'foreign.json'
            self.c._private(foreign, b'{}\n')
            with self.assertRaises(m.ProfileError):
                with ports.artifact.pin(foreign): pass
            self.c._private(self.invocation.workflow, b'{ }\n')
            with self.assertRaises(m.ProfileError):
                with ports.artifact.pin(self.invocation.workflow): pass
            self.c._private(self.invocation.workflow, b'{}\n')
            with ports.artifact.pin(self.invocation.workflow) as pin:
                self.assertEqual(ports.artifact.check(pin), composition.YES)
        self.action = activity
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_ports_binding
    def test_ports_binding(self):
        def activity(ports, args):
            record = args['surface_factory'].__self__
            for field in ('scope', 'authority'):
                with self.assertRaises(m.ProfileError):
                    record._locator_arguments(self.profile, self.invocation,
                                              replace(ports, **{field: object()}))
        self.action = activity
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_cancel_bounds
    def test_cancel_bounds(self):
        def activity(ports, args):
            for timeout in (True, 0, -1, self.policy.limits.cancel_ns + 1):
                self.assertNotEqual(ports.resources.cancel(timeout), composition.YES)
            self.assertEqual(ports.resources.cancel(self.policy.limits.cancel_ns), composition.YES)
            original = ports.resources._config
            def explode(timeout): raise ValueError('PRIVATE_CANARY')
            ports.resources._config = replace(original, cancel=explode)
            self.assertEqual(ports.resources.cancel(1), m.AdmissionCheck(m.Truth.FALSE, m.Reason.UNKNOWN))
            ports.resources._config = original
        self.action = activity
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_journal
    def test_journal(self):
        def activity(ports, args):
            journal = ports.journal
            workflow = copy.deepcopy(self.t.b.workflow)
            workflow['steps'] = [composition.base._step()]
            self.c._private(self.invocation.workflow, (m.gate.canon(workflow) + '\n').encode())
            self.assertEqual(journal.read(ports.limits.journal_bytes), b'')
            initial = dict(seq=1, prev=None, kind='verified', step_seq=1, phase='terminal')
            initial['digest'] = journal.digest(m.gate.canon(initial).encode(), None)
            initial_raw = (m.gate.canon(initial) + '\n').encode()
            self.assertNotEqual(journal.append(initial_raw), composition.YES)
            self.assertNotEqual(journal.reserve(True, 1), composition.YES)
            self.assertEqual(journal.reserve(ports.limits.event_bytes * 4, 4), composition.YES)
            event = dict(seq=1, prev=None, kind='verified', step_seq=1, phase='terminal')
            payload = m.gate.canon(event).encode()
            digest = journal.digest(payload, None)
            self.assertEqual(digest, m.hashlib.sha256(b'MW_PROFILE_JOURNAL_V1\0' + b'\0' +
                len(payload).to_bytes(8, 'big') + payload).hexdigest())
            predecessor = '0' * 64
            self.assertEqual(journal.digest(payload, predecessor), m.hashlib.sha256(
                b'MW_PROFILE_JOURNAL_V1\0' + b'\1' + bytes.fromhex(predecessor) +
                len(payload).to_bytes(8, 'big') + payload).hexdigest())
            event['digest'] = digest
            raw = (m.gate.canon(event) + '\n').encode()
            self.assertNotEqual(journal.append(raw[:-1]), composition.YES)
            wrong = dict(event, seq=2)
            wrong['digest'] = journal.digest(m.gate.canon({k: v for k, v in wrong.items() if k != 'digest'}).encode(), None)
            self.assertNotEqual(journal.append((m.gate.canon(wrong) + '\n').encode()), composition.YES)
            with patch.object(journal, '_write', side_effect=OSError('SYNTHETIC_IO')):
                self.assertNotEqual(journal.append(raw), composition.YES)
            self.assertEqual(journal.read(ports.limits.journal_bytes), b'')
            private = dict(event, text='PRIVATE_SLOT_CANARY')
            private['digest'] = journal.digest(m.gate.canon({k: v for k, v in private.items() if k != 'digest'}).encode(), None)
            self.assertNotEqual(journal.append((m.gate.canon(private) + '\n').encode()), composition.YES)
            self.assertEqual(journal.append(raw), composition.YES)
            self.assertEqual(journal.authenticate(event), composition.YES)
            self.assertNotEqual(journal.authenticate(dict(event, digest='f' * 64)), composition.YES)
            self.assertNotEqual(journal.digest(b'{}', None), journal.digest(b'{}', '0' * 64))
            self.assertEqual(journal.read(ports.limits.journal_bytes), raw)
            summary = composition.base._fixture('run')
            pin = m.hashlib.sha256(self.invocation.workflow.read_bytes()).hexdigest()
            summary.update(run_id=ports.run_id, site_id=workflow['site_id'], workflow_id=workflow['id'],
                workflow_sha256=pin, mode=self.invocation.mode, visibility='local', rc=0,
                verdict_code='VERIFIED', steps_total=1, steps_verified=1, steps_skipped_satisfied=0,
                steps_skipped_condition=0, steps_failed=0, steps_not_reached=0, reproduced_ppm=1000000,
                failed_step_seq=None, model_calls=0)
            inventory = dict(workflow_sha256=pin, steps=[dict(seq=1, disposition='verified',
                predicate_pass=True, optional=not workflow['steps'][0]['protected'], guard_false=False)],
                required_segments={'task': [1]}, final_task_pass=True)
            forged = copy.deepcopy(inventory)
            forged['steps'][0]['predicate_pass'] = False
            with self.assertRaises(m.ProfileError): journal.stage(summary, forged, self.invocation.output)
            with patch.object(journal, '_write', side_effect=OSError('SYNTHETIC_IO')):
                with self.assertRaises(OSError): journal.stage(summary, inventory, self.invocation.output)
            self.assertFalse(self.invocation.output.exists())
            record = args['surface_factory'].__self__
            trusted = record.owners
            for field in ('authorization', 'validation_context'):
                if field == 'authorization':
                    changed = types.SimpleNamespace(inventory=trusted.authorization.inventory,
                        check=lambda *a: composition.NO)
                else:
                    changed = replace(trusted.validation_context,
                        evidence_check=lambda *a: m.gate.GateResult(5, 'MISSING'))
                record.owners = replace(trusted, **{field: changed})
                try:
                    with self.assertRaises(m.ProfileError): journal.stage(summary, inventory, self.invocation.output)
                finally:
                    record.owners = trusted
            previous = b'previous-committed-generation\n'
            self.c._private(self.invocation.output, previous)
            staged = journal.stage(summary, inventory, self.invocation.output)
            self.assertEqual(self.invocation.output.read_bytes(), previous)
            invalid = journal.stage(dict(summary, verdict_code='NOT_A_VERDICT'), inventory, self.invocation.output)
            self.assertNotEqual(journal.validate(invalid, inventory), composition.YES)
            self.assertEqual(journal.validate(staged, inventory), composition.YES)
            self.assertNotEqual(journal.validate(staged, forged), composition.YES)
            ctx = journal.context()
            loaded = ctx.evidence_load('run_inventory', summary, ())
            loaded['steps'][0]['predicate_pass'] = False
            self.assertEqual(ctx.evidence_load('run_inventory', summary, ()), inventory)
            self.assertNotEqual(ctx.evidence_check('run_inventory', summary, (loaded,)).rc, 0)
            self.assertEqual(ctx.evidence_check('run_inventory', summary, (inventory,)).rc, 0)
            self.c._private(staged, b'{}\n')
            self.assertNotEqual(journal.select(staged), composition.YES)
            self.assertEqual(self.invocation.output.read_bytes(), previous)
            self.c._private(staged, (m.gate.canon(summary) + '\n').encode())
            with patch.object(m.os, 'replace', side_effect=OSError('PRIVATE_CANARY')):
                self.assertNotEqual(journal.select(staged), composition.YES)
            self.assertEqual(self.invocation.output.read_bytes(), previous)
            self.assertEqual(journal.select(staged), composition.YES)
            self.assertEqual(self.invocation.output.read_bytes(), (m.gate.canon(summary) + '\n').encode())
            self.assertNotEqual(journal.select(staged), composition.YES)
            # Persisted MAC authenticates across a fresh owner; forged raw bytes do not.
            macfile = journal._store / (digest + '.mac')
            mac = macfile.read_bytes()
            self.c._private(macfile, b'0' * 64)
            self.assertNotEqual(journal.authenticate(event), composition.YES)
            self.c._private(macfile, mac)
            suffix = b'{"torn":true}'
            self.c._private(journal._store / 'events.jsonl', raw + suffix)
            self.assertNotEqual(journal.reserve(1, 1), composition.YES)
            self.assertNotEqual(journal.recover(raw, b'foreign'), composition.YES)
            self.assertEqual(journal.recover(raw, suffix), composition.YES)
            self.assertEqual(journal.read(ports.limits.journal_bytes), raw)
            self.assertEqual([p.read_bytes() for p in journal._store.glob('torn-*')], [suffix])
            self.assertEqual(len(tuple(journal._store.glob('recovery-*.json'))), 1)
            journal.close()
            self.assertEqual(journal.authenticate(event), composition.YES)
        self.action = activity
        self.assertEqual(self._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestDefaultProvisioning.test_unmocked_live_composition
    def test_unmocked_live_composition(self):
        self.c.runner.side_effect = None  # wraps the unchanged real runner
        self.stack.enter_context(patch.object(live.m, 'LiveOCRDescriber', return_value=self.t.describer))
        self.stack.enter_context(patch.object(live.m, 'MuseLocateTransport', return_value=self.t.model))
        b = self.t.b
        b.workflow['steps'] = [composition.base._step()]
        b._write()
        b.path.chmod(0o600)
        self.invocation = m.make_invocation(self.profile, workflow=b.path,
            output=self.invocation.output, mode=self.invocation.mode)
        original_policy = self.policy
        values = {}
        fail = False
        mutate = False
        self.observed = []
        def trusted_owners(**kw):
            self.records.append(kw)
            def acquire(predicate, deadline):
                stamp = m.time.monotonic_ns()
                if mutate:
                    self.c._private(b.path, (m.gate.canon(dict(b.workflow, id='changed')) + '\n').encode())
                value = composition.w.ObservationValue(kw['scope'], kw['authority'], m.uuid.uuid4().hex,
                    stamp, stamp, predicate['capability_id'], 'v1', 'boolean', 'count',
                    not fail, True, None)
                values[value.publication] = value
                self.observed.append(value)
                return value
            observations = types.SimpleNamespace(acquire=acquire,
                validate=lambda value: composition.YES if values.get(value.publication) is value else composition.NO)
            def evidence(kind, summary, related):
                if kind != 'run_inventory':
                    return b.ctx.evidence_check(kind, summary, related)
                # Independent registered final producer observation, not caller assertion.
                approved = summary['rc'] != 0 or any(v.capability_id == 'final' and v.value is True
                                                     for v in values.values())
                return m.gate.GateResult(0, 'PASS') if approved else m.gate.GateResult(5, 'MISSING')
            return m._HostOwners(b.authorization, self.t.ports.capabilities, observations,
                                 replace(b.ctx, evidence_check=evidence))
        self.policy = replace(original_policy, owners=trusted_owners, resource_capacity=16_777_216)
        def surface(**kw):
            self.surfaces.append(kw)
            return composition._Surface(**kw)
        # No real producer/model calls: the workflow waits for registered evidence.
        # Surface startup/close and the real live runner and journal remain intact.
        with patch.object(m, 'SrfIdentitySurface', side_effect=surface):
            result = m.run_profile(self.profile, self.invocation, bundle=composition.base.BUNDLE, services=None)
        self.assertEqual(result.rc, 0, result)
        self.assertIsNotNone(result.summary)
        self.assertEqual(result.summary['visibility'], 'local')
        self.assertTrue(any(v.capability_id == 'final' for v in self.observed))
        self.assertEqual(m.gate.strict_load(self.invocation.output.read_bytes()), result.summary)
        self.assertTrue(self.surfaces)
        self.assertFalse(Path(self.surfaces[-1]['ram_dir']).exists())
        # Negative producer evidence must not yield a verified receipt.
        fail = True
        self.invocation = m.make_invocation(self.profile, workflow=self.invocation.workflow,
            output=self.c.root / 'negative-out', mode=self.invocation.mode)
        with patch.object(m, 'SrfIdentitySurface', side_effect=surface):
            result = m.run_profile(self.profile, self.invocation, bundle=composition.base.BUNDLE, services=None)
        self.assertNotEqual(result.rc, 0)
        fail, mutate = False, True
        self.invocation = m.make_invocation(self.profile, workflow=self.invocation.workflow,
            output=self.c.root / 'mutated-out', mode=self.invocation.mode)
        with patch.object(m, 'SrfIdentitySurface', side_effect=surface):
            result = m.run_profile(self.profile, self.invocation, bundle=composition.base.BUNDLE, services=None)
        self.assertEqual(result.rc, 3, result)
        self.assertEqual(result.verdict_code, 'SEAL_MISMATCH')
        self.c.spawn.assert_not_called()


# @anchor:wa.s4.mw_profiles.TestHostCustody
class TestHostCustody(unittest.TestCase):
    setUp = TestDefaultProvisioning.setUp
    _configuration = TestDefaultProvisioning._configuration
    _owners = TestDefaultProvisioning._owners
    _surface = TestDefaultProvisioning._surface
    _approved_evidence = TestDefaultProvisioning._approved_evidence
    _runner = TestDefaultProvisioning._runner
    _run = TestDefaultProvisioning._run
    # @anchor:wa.s4.mw_profiles.TestHostCustody.test_cross_process_lease
    def test_cross_process_lease(self):
        root = self.c.root / 'leases'
        root.mkdir(mode=0o700)
        path = root / (self.invocation.container + '.' + self.invocation.window + '.lock')
        # Restore the real process boundary only for this fixed synthetic child.
        import multiprocessing
        parent, child = multiprocessing.Pipe()
        def hold():
            with path.open('w') as stream:
                path.chmod(0o600)
                m.fcntl.flock(stream.fileno(), m.fcntl.LOCK_EX)
                child.send('held')
                child.recv()
        process = multiprocessing.get_context('fork').Process(target=hold)
        process.start()
        try:
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(), 'held')
            host = m._DefaultHost(self.profile, self.invocation)
            self.policy = replace(self.policy, lease=host.lease, notice=host.notice)
            with patch.object(m, '_LEASE_ROOT', root), patch.object(Path, 'home', return_value=self.c.root), \
                 self.c._no_workspace():
                with self.assertRaises(m.ProfileError): self._run()
                self.c.builder.assert_not_called()
            self.c.runner.assert_not_called()
            self.assertFalse(self.surfaces)
            self.assertFalse(self.records)
        finally:
            parent.send('release')
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
            parent.close()
            child.close()
        self.assertEqual(process.exitcode, 0)
        with patch.object(m, '_LEASE_ROOT', root), patch.object(Path, 'home', return_value=self.c.root), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(self._run().rc, 4)
        self.assertIsNone(host.fd)
        rows = [json.loads(line) for line in
                (self.c.root / '.local/state/memoryweb/notices.jsonl').read_text().splitlines()]
        self.assertEqual([row['phase'] for row in rows], ['start', 'end'])
        self.assertTrue(path.exists())

    # @anchor:wa.s4.mw_profiles.TestHostCustody.test_notice_durability
    def test_notice_durability(self):
        host = m._DefaultHost(self.profile, self.invocation)
        self.policy = replace(self.policy, lease=host.lease, notice=host.notice)
        directory = self.c.root / '.local/state/memoryweb'
        for path in (directory.parent.parent, directory.parent, directory):
            path.mkdir(mode=0o700)
        journal = directory / 'notices.jsonl'
        self.c._private(journal, b'{"torn":true}')
        with patch.object(m, '_LEASE_ROOT', self.c.root / 'leases'), \
             patch.object(Path, 'home', return_value=self.c.root), redirect_stdout(io.StringIO()):
            # Runner spy asserts notice success; error is preserved in its deferred list.
            with self.assertRaises(m.ProfileError): self._run()
        self.assertEqual(journal.read_bytes(), b'{"torn":true}')
        self.assertFalse(host.started)
        self.assertIsNone(host.fd)
        self.c._private(journal, b'')
        with patch.object(m, '_LEASE_ROOT', self.c.root / 'leases'), \
             patch.object(Path, 'home', return_value=self.c.root), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(self._run().rc, 4)
        self.assertNotIn('SYNTHETIC_ACK', output.getvalue())
        self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
        self.assertEqual([json.loads(line)['phase'] for line in journal.read_text().splitlines()], ['start', 'end'])


    # @anchor:wa.s4.mw_profiles.TestHostCustody.test_end_notice_failure_releases_drained_lease
    def test_end_notice_failure_releases_drained_lease(self):
        host = m._DefaultHost(self.profile, self.invocation)
        self.policy = replace(self.policy, lease=host.lease, notice=host.notice)
        append = host.append_notice
        def failing_end(phase):
            if phase == 'end': raise OSError('synthetic notice failure')
            return append(phase)
        with patch.object(m, '_LEASE_ROOT', self.c.root / 'leases'), \
             patch.object(Path, 'home', return_value=self.c.root), \
             patch.object(host, 'append_notice', side_effect=failing_end), redirect_stdout(io.StringIO()):
            with self.assertRaises(m.ProfileError): self._run()
        self.assertIsNone(host.fd)
        self.assertFalse(list(self.root.iterdir()))

    # @anchor:wa.s4.mw_profiles.TestHostCustody.test_notice_append_failure
    def test_notice_append_failure(self):
        host = m._DefaultHost(self.profile, self.invocation)
        self.policy = replace(self.policy, lease=host.lease, notice=host.notice)
        write = m.os.write
        for failure in (0, OSError('synthetic append failure')):
            def broken(fd, raw):
                if b'"phase":"start"' in raw and b'"run_id"' in raw:
                    if isinstance(failure, OSError):
                        raise failure
                    return failure
                return write(fd, raw)
            with patch.object(m, '_LEASE_ROOT', self.c.root / 'leases'), \
                 patch.object(Path, 'home', return_value=self.c.root), \
                 patch.object(m.os, 'write', side_effect=broken), redirect_stdout(io.StringIO()):
                with self.assertRaises(m.ProfileError): self._run()
            self.assertFalse(host.started)
            self.assertIsNone(host.fd)
        with patch.object(m, '_LEASE_ROOT', self.c.root / 'leases'), \
             patch.object(Path, 'home', return_value=self.c.root), redirect_stdout(io.StringIO()):
            self.assertEqual(self._run().rc, 4)
        journal = self.c.root / '.local/state/memoryweb/notices.jsonl'
        self.assertEqual([json.loads(line)['phase'] for line in journal.read_text().splitlines()], ['start', 'end'])

    # @anchor:wa.s4.mw_profiles.TestHostCustody.test_custody_mutants
    def test_custody_mutants(self):
        nominal = unittest.TestResult()
        TestHostCustody('test_cross_process_lease').run(nominal)
        self.assertTrue(nominal.wasSuccessful(), (nominal.failures, nominal.errors))
        original = m.fcntl.flock
        def without_nonblocking(fd, operation):
            if not operation & m.fcntl.LOCK_NB:
                return original(fd, operation)
        result = unittest.TestResult()
        with patch.object(m.fcntl, 'flock', side_effect=without_nonblocking):
            TestHostCustody('test_cross_process_lease').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)
        original_need = m._need
        def without_torn(ok, name, rc=5):
            if name != 'notice_torn': original_need(ok, name, rc)
        result = unittest.TestResult()
        with patch.object(m, '_need', side_effect=without_torn):
            TestHostCustody('test_notice_durability').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)
        result = unittest.TestResult()
        def without_append(ok, name, rc=5):
            if name != 'notice_append': original_need(ok, name, rc)
        with patch.object(m, '_need', side_effect=without_append):
            TestHostCustody('test_notice_append_failure').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)
        import textwrap
        source = textwrap.dedent(inspect.getsource(m._DefaultHost.append_notice))
        source = source.replace('os.write(fd, raw) == len(raw)', 'len(raw) == len(raw)')
        namespace = dict(vars(m))
        exec(compile(source, '<notice-write-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(m._DefaultHost, 'append_notice', namespace['append_notice']):
            TestHostCustody('test_notice_durability').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)


# @anchor:wa.s4.mw_profiles.TestProfileMutants
class TestProfileMutants(unittest.TestCase):
    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_corrected_default_capacity_mutants
    def test_corrected_default_capacity_mutants(self):
        source = inspect.getsource(m._capacity_targets)
        for old, new in (('ve_process_bytes=1024 * mib', 've_process_bytes=512 * mib'),
                         ('retained_bytes=64 * mib, peak_bytes=2048 * mib', 'retained_bytes=64 * mib, peak_bytes=1024 * mib'),
                         ('resource_capacity=2048 * mib', 'resource_capacity=1024 * mib')):
            self.assertIn(old, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(old, new), '<corrected-capacity-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(field=old), patch.object(m, '_capacity_targets', namespace['_capacity_targets']):
                TestDefaultProvisioning('test_default_without_provider').run(result)
            self.assertTrue(result.failures or result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_image_ceiling_mutant
    def test_image_ceiling_mutant(self):
        source = inspect.getsource(m._capacity_ceilings)
        self.assertIn('image_bytes = 8 * 1024 * 1024', source)
        namespace = dict(vars(m))
        exec(compile(source.replace('image_bytes = 8 * 1024 * 1024',
            'image_bytes = 3 * 1024 * 1024'), '<image-ceiling-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(m, '_capacity_ceilings', namespace['_capacity_ceilings']):
            TestDefaultProvisioning('test_capacity_field_sweep').run(result)
        self.assertTrue(result.failures or result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_default_preflight_mutants
    def test_default_preflight_mutants(self):
        import textwrap
        source = textwrap.dedent(inspect.getsource(m._DefaultHost.preflight))
        for expression in ("not root or not Path(root).is_dir()", "not shutil.which('tesseract')", "ai_locate and not shutil.which('muse')"):
            namespace = dict(vars(m))
            exec(compile(source.replace(expression, 'False'), '<support-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(expression=expression), patch.object(m._DefaultHost, 'preflight', namespace['preflight']):
                TestDefaultProvisioning('test_default_support_preflight').run(result)
            self.assertTrue(result.failures or result.errors)
        for method in ('export_check', 'custody_check', 'support_check'):
            result = unittest.TestResult()
            with self.subTest(method=method), patch.object(m._DefaultHost, method, lambda *a, **k: composition.YES):
                TestDefaultProvisioning('test_default_export_custody_support').run(result)
            self.assertTrue(result.failures or result.errors, method)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_host_bound_predicate_mutants
    def test_host_bound_predicate_mutants(self):
        source = inspect.getsource(m._host_policy)
        # retained_bytes <= peak_bytes is implied by the positive sum bound;
        # its isolated removal is equivalent, so it is not a killable mutant.
        predicates = (
            'w.workflow_bytes <= gate.LIMITS["workflow"]',
            'p.poll_ns <= p.call_ns',
            '2 * p.image_bytes + p.request_bytes + 3 * p.response_bytes + p.stderr_bytes + p.retained_bytes <= p.peak_bytes',
            '2 * p.image_bytes + c.metadata_bytes <= p.retained_bytes',
            'p.max_calls * p.retained_bytes <= w.retained_bytes',
            'p.peak_bytes + 8 * p.image_bytes + c.decode_limits.memory_bytes + c.ve_process_bytes +\n          c.ve_stdout_bytes + c.ve_stderr_bytes + c.metadata_bytes + (p.max_calls - 1) * p.retained_bytes <= w.peak_bytes',
            'c.decode_limits.png_bytes <= p.image_bytes',
            'p.max_calls <= min(w.acquisitions, n.publications)',
            'w.peak_bytes <= config.resource_capacity')
        for predicate in predicates:
            self.assertIn(predicate, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(predicate, 'True'), '<host-bound-mutant>', 'exec'), namespace)
            mutant = types.FunctionType(namespace['_host_policy'].__code__, vars(m))
            result = unittest.TestResult()
            with self.subTest(predicate=predicate), patch.object(m, '_host_policy', mutant):
                TestDefaultProvisioning('test_host_policy').run(result)
            self.assertTrue(result.failures, (predicate, result.errors))
            self.assertFalse(result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_capacity_bounds_mutant
    def test_capacity_bounds_mutant(self):
        nominal = unittest.TestResult()
        TestDefaultProvisioning('test_capacity_field_sweep').run(nominal)
        self.assertTrue(nominal.wasSuccessful(), (nominal.failures, nominal.errors))
        source = inspect.getsource(m._capacity_bounds)
        caps = m._capacity_ceilings()
        comparisons = []
        for key in ('limits', 'native_policy', 'locator_policy', 'evidence_policy'):
            for field in m.fields(getattr(caps, key)):
                if field.name == 'decode_limits':
                    for nested in m.fields(caps.evidence_policy.decode_limits):
                        comparisons.append((key + '.decode_limits.' + nested.name,
                            'getattr(obj.decode_limits, nested.name) <= getattr(cap.decode_limits, nested.name)',
                            'nested.name == ' + repr(nested.name)))
                elif field.name in ('deadline_ns', 'absolute_deadline_ns'):
                    comparisons.append((key + '.' + field.name,
                        'getattr(obj, field.name) - now <= reference.durations.run_ns',
                        'key == ' + repr(key) + ' and field.name == ' + repr(field.name)))
                else:
                    comparisons.append((key + '.' + field.name,
                        'getattr(obj, field.name) <= getattr(cap, field.name)',
                        'key == ' + repr(key) + ' and field.name == ' + repr(field.name)))
        for field in m.fields(caps.durations):
            comparisons.append(('durations.' + field.name,
                'getattr(config.durations, field.name) <= getattr(reference.durations, field.name)',
                'field.name == ' + repr(field.name)))
        comparisons.append(('resource_capacity', 'config.resource_capacity <= reference.resource_capacity', 'True'))
        for label, expression, selected in comparisons:
            self.assertIn(expression, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(expression, '(' + selected + ') or (' + expression + ')'),
                         '<capacity-field-mutant>', 'exec'), namespace)
            mutant = types.FunctionType(namespace['_capacity_bounds'].__code__, vars(m))
            result = unittest.TestResult()
            with self.subTest(field=label), patch.object(m, '_capacity_bounds', mutant):
                TestDefaultProvisioning('test_capacity_field_sweep').run(result)
            self.assertTrue(result.failures, (label, result.errors))
            self.assertFalse(result.errors, (label, result.errors))

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_clock_mutants
    def test_clock_mutants(self):
        cases = (
            ('_trusted_default_configuration', 'deadline_ns=start_ns + durations.run_ns',
             'deadline_ns=durations.run_ns', 'test_clock_relative_deadlines'),
            ('_host_policy', "    _need(type(now) is int and now > 0, 'host_clock')\n",
             '', 'test_clock_type_before_arithmetic'))
        for name, old, new, test in cases:
            source = inspect.getsource(getattr(m, name))
            self.assertIn(old, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(old, new), '<host-clock-mutant>', 'exec'), namespace)
            mutant = types.FunctionType(namespace[name].__code__, vars(m))
            result = unittest.TestResult()
            with self.subTest(test=test), patch.object(m, name, mutant):
                TestDefaultProvisioning(test).run(result)
            self.assertTrue(result.failures, result.errors)
            self.assertFalse(result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_defaulted_field_mutant
    def test_defaulted_field_mutant(self):
        source = inspect.getsource(m._host_policy)
        old = "if f.name != 'decode_limits'"
        self.assertIn(old, source)
        namespace = dict(vars(m))
        exec(compile(source.replace(old,
            "if f.name not in ('decode_limits', 'ocr_tasks_max')"), '<ocr-field-mutant>', 'exec'), namespace)
        mutant = types.FunctionType(namespace['_host_policy'].__code__, vars(m))
        result = unittest.TestResult()
        with patch.object(m, '_host_policy', mutant):
            TestDefaultProvisioning('test_host_policy').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_duration_mutants
    def test_duration_mutants(self):
        nominal = m._HostDurations()
        for field in m.fields(nominal):
            for value in (0, getattr(nominal, field.name) // 2):
                result = unittest.TestResult()
                with self.subTest(field=field.name, value=value), patch.object(m, '_HostDurations',
                        return_value=replace(nominal, **{field.name: value})):
                    TestDefaultProvisioning('test_default_duration_values').run(result)
                self.assertTrue(result.failures or result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_default_selection_mutants
    def test_default_selection_mutants(self):
        result = unittest.TestResult()
        with patch.object(m, '_default_capacity_configuration', side_effect=m.ProfileError()):
            TestDefaultProvisioning('test_default_without_provider').run(result)
        self.assertTrue(result.failures or result.errors)

        source = inspect.getsource(m.run_profile)
        rows = (
            ('if services is None:', 'if False:', 'test_shell_default'),
            ('if services is None:', 'if True:', 'test_explicit_services_never_fallback'),
            ("profile.announce_gate == 'trusted_notice_v1', 'binding')",
             "profile.announce_gate == 'trusted_notice_v1' or True, 'binding')",
             'test_default_binding_precedes_factory'),
        )
        for old, new, test in rows:
            self.assertIn(old, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(old, new), '<default-profile-mutant>', 'exec'), namespace)
            mutant = types.FunctionType(namespace['run_profile'].__code__, vars(m))
            result = unittest.TestResult()
            with self.subTest(test=test), patch.object(m, 'run_profile', mutant), patch.object(cli, 'run_profile', mutant):
                case = TestDefaultProvisioning if test == 'test_shell_default' else TestProfiles
                case(test).run(result)
            self.assertTrue(result.failures, (test, result.errors))
            self.assertFalse(result.errors, (test, result.errors))

        source = inspect.getsource(cli.main)
        self.assertIn('return result.rc', source)
        namespace = dict(vars(cli))
        exec(compile(source.replace('return result.rc', 'return 0'), '<profile-rc-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(cli, 'main', namespace['main']):
            TestDefaultProvisioning('test_shell_default').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors, result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_provisioning_gate_removal
    def test_provisioning_gate_removal(self):
        rows = {
            'notice_evidence': 'test_announcement',
            'notice_lease': 'test_announcement', 'notice_binding': 'test_announcement',
            'host_policy_integers': 'test_host_policy', 'host_policy_bounds': 'test_host_policy',
            'host_policy_deadline': 'test_host_policy', 'surface_binding': 'test_surface_mapping',
            'evidence_binding': 'test_evidence_privacy', 'evidence_destination': 'test_evidence_privacy',
            'host_installed': 'test_host_binding', 'host_callbacks': 'test_host_callbacks', 'host_owners': 'test_owner_type',
            'artifact_binding': 'test_artifact_binding_and_canonical',
            'artifact_canonical': 'test_artifact_binding_and_canonical',
            'ports_binding': 'test_ports_binding', 'evidence_observation': 'test_evidence_privacy',
            'journal_capacity': 'test_journal', 'journal_append': 'test_journal',
            'journal_chain': 'test_journal', 'journal_digest': 'test_journal',
            'journal_authentication': 'test_journal', 'journal_recovery': 'test_journal',
            'journal_inventory': 'test_journal', 'journal_stage': 'test_journal',
            'journal_authority': 'test_journal', 'journal_evidence': 'test_journal',
            'journal_privacy': 'test_journal', 'journal_validate': 'test_journal',
            'journal_selection': 'test_journal',
            'artifact_mutation': 'test_artifact', 'provisioning_cleanup': 'test_resources_and_cleanup',
            'sleep_deadline': 'test_lifetime_and_wait', 'provisioning_binding': 'test_lifetime_and_wait',
        }
        original = m._need
        for label, test in rows.items():
            def bypass(ok, name, rc=5):
                if name != label: original(ok, name, rc)
            result = unittest.TestResult()
            with self.subTest(gate=label), patch.object(m, '_need', bypass):
                TestDefaultProvisioning(test).run(result)
            self.assertTrue(result.failures, (label, result.errors))
            self.assertFalse(result.errors, (label, result.errors))
        # Structural mutants for complete gates with upstream defensive checks.
        cases = (
            (m, '_workspace_root_check', lambda root: None, 'test_workspace'),
            (m._Resources, 'reserve', lambda *a: composition.YES, 'test_resources_and_cleanup'),
            (m._Resources, 'drain', lambda *a: composition.YES, 'test_resources_and_cleanup'),
        )
        for owner, name, mutant, test in cases:
            result = unittest.TestResult()
            with self.subTest(gate=name), patch.object(owner, name, mutant):
                TestDefaultProvisioning(test).run(result)
            self.assertTrue(result.failures, (name, result.errors))
            self.assertFalse(result.errors, (name, result.errors))
        # Explicit RAM binding removal must change the public surface predicate.
        source = inspect.getsource(m._Provisioning._surface)
        import textwrap
        source = textwrap.dedent(source).replace(', ram_dir=str(self.workspace)', '')
        namespace = dict(vars(m))
        exec(compile(source, '<surface-ram-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(m._Provisioning, '_surface', namespace['_surface']):
            TestDefaultProvisioning('test_surface_mapping').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors, result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_journal_and_cancel_mutants
    def test_journal_and_cancel_mutants(self):
        cases = (
            (m._Journal, 'authenticate', lambda *a: composition.YES, 'test_journal'),
            (m._Journal, 'recover', lambda *a: composition.YES, 'test_journal'),
            (m._Journal, 'validate', lambda *a: composition.YES, 'test_journal'),
            (m._Journal, 'select', lambda *a: composition.YES, 'test_journal'),
            (m._Resources, 'cancel', lambda owner, *a: (setattr(owner, '_cancelled', True), composition.YES)[1], 'test_cancel_bounds'),
        )
        for owner, name, mutant, test in cases:
            result = unittest.TestResult()
            with self.subTest(mutant=name), patch.object(owner, name, mutant):
                TestDefaultProvisioning(test).run(result)
            self.assertTrue(result.failures, (name, result.errors))
            self.assertFalse(result.errors, (name, result.errors))
        original = m._Journal.context
        def untrusted(owner):
            return replace(original(owner), evidence_check=lambda *a: m.gate.GateResult(0, 'PASS'))
        result = unittest.TestResult()
        with patch.object(m._Journal, 'context', untrusted):
            TestDefaultProvisioning('test_journal').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors, result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_production_framing_mutants
    def test_production_framing_mutants(self):
        import textwrap
        source = textwrap.dedent(inspect.getsource(m._Journal.digest))
        for old in ("b'MW_PROFILE_JOURNAL_V1\\0'", "len(raw).to_bytes(8, 'big')",
                    "(b'\\0' if prev is None else b'\\1' + bytes.fromhex(prev))"):
            self.assertIn(old, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(old, "b''"), '<journal-framing-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(framing=old), patch.object(m._Journal, 'digest', namespace['digest']):
                TestDefaultProvisioning('test_journal').run(result)
            self.assertTrue(result.failures, result.errors)
            self.assertFalse(result.errors, result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_removal_controls
    def test_removal_controls(self):
        cases = {
            'site': 'test_lookup', 'file_mode': 'test_permissions', 'file_owner': 'test_permissions',
            'directory_mode': 'test_permissions', 'directory_owner': 'test_ancestry_refusal', 'git_tree': 'test_symlinks_and_git',
            'regular': 'test_missing_and_nonregular', 'read_size': 'test_bounds_and_mutation',
            'mutation': 'test_bounds_and_mutation', 'components': 'test_bounds_and_mutation',
            'string_type': 'test_closed_schema', 'root_schema': 'test_closed_schema', 'source_schema': 'test_closed_schema',
            'modes_type': 'test_closed_schema', 'modes_schema': 'test_closed_schema',
            'scalar': 'test_scalars_and_kinds', 'coordinate': 'test_scalars_and_kinds',
            'absolute_path': 'test_scalars_and_kinds', 'gate_kind': 'test_scalars_and_kinds',
            'source_kind': 'test_scalars_and_kinds', 'ack_empty': 'test_ack_and_modes',
            'mode': 'test_ack_and_modes', 'arguments': 'test_services_and_binding',
        }
        original = m._need
        for label, name in cases.items():
            def bypass(ok, gate, rc=5):
                removed = {'read_size', 'read_overflow'} if label == 'read_size' else ({'directory_owner', 'mutation'} if label == 'directory_owner' else {label})
                if gate not in removed: original(ok, gate, rc)
            with self.subTest(mutant=label), patch.object(m, '_need', bypass):
                result = unittest.TestResult()
                TestProfiles(name).run(result)
                self.assertFalse(result.wasSuccessful(), label)
                self.assertEqual(result.testsRun, 1)

        nominal = unittest.TestResult()
        TestProfileComposition('test_trusted_types_and_binding').run(nominal)
        self.assertTrue(nominal.wasSuccessful(), (nominal.failures, nominal.errors))
        for label in ('services', 'binding'):
            def bypass(ok, gate, rc=5):
                if gate != label: original(ok, gate, rc)
            with self.subTest(mutant=label), patch.object(m, '_need', bypass):
                result = unittest.TestResult()
                TestProfileComposition('test_trusted_types_and_binding').run(result)
                self.assertTrue(result.failures, (label, result.errors))
                self.assertFalse(result.errors, (label, result.errors))
        # Native no-follow removal must admit a symlink and kill the public predicate.
        actual = os.open
        def follow(path, flags, *a, **kw):
            return actual(path, flags & ~os.O_NOFOLLOW, *a, **kw)
        actual_stat = os.stat
        def followed_stat(*a, **kw):
            kw['follow_symlinks'] = True
            return actual_stat(*a, **kw)
        with patch.object(m.os, 'open', side_effect=follow), patch.object(m.os, 'stat', side_effect=followed_stat):
            result = unittest.TestResult()
            TestProfiles('test_symlinks_and_git').run(result)
            self.assertFalse(result.wasSuccessful())

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_parser_and_cli_mutants
    def test_parser_and_cli_mutants(self):
        # Removing strict parsing must let a duplicate-bearing otherwise valid object through.
        result = unittest.TestResult()
        with patch.object(m.gate, 'strict_load', side_effect=json.loads):
            TestProfiles('test_strict_json').run(result)
        self.assertFalse(result.wasSuccessful())
        source = inspect.getsource(cli.main)
        old = 'if args.profile is not None and ('
        self.assertIn(old, source)
        namespace = dict(vars(cli))
        exec(compile(source.replace(old, 'if False and ('), '<profile-cli-mutant>', 'exec'), namespace)
        mutant = types.FunctionType(namespace['main'].__code__, vars(cli), argdefs=cli.main.__defaults__)
        mutant.__kwdefaults__ = cli.main.__kwdefaults__
        result = unittest.TestResult()
        with patch.object(cli, 'main', mutant):
            TestProfiles('test_cli_conflicts_and_privacy').run(result)
        self.assertFalse(result.wasSuccessful())
        old = 'if hashlib.sha256(schema).hexdigest() != gate.SCHEMA_PIN:'
        namespace = dict(vars(cli))
        self.assertIn(old, source)
        exec(compile(source.replace(old, 'if False:'), '<profile-seal-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(cli, 'main', namespace['main']):
            TestProfiles('test_schema_pin').run(result)
        self.assertTrue(result.failures, result.errors)

        source = inspect.getsource(m.run_profile)
        old = 'except Exception:\n        raise ProfileError() from None'
        self.assertIn(old, source)
        namespace = dict(vars(m))
        exec(compile(source.replace(old, 'except Exception:\n        raise'), '<profile-private-error-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(m, 'run_profile', namespace['run_profile']):
            TestProfiles('test_private_trace').run(result)
        self.assertFalse(result.wasSuccessful())

        source = inspect.getsource(m._guard)
        rows = (
            ('os.close(fd)', 'pass', 'test_descriptor_cleanup'),
            ('except (OSError, ValueError, TypeError, UnicodeError):\n        raise ProfileError() from None',
             'except (OSError, ValueError, TypeError, UnicodeError):\n        raise', 'test_ancestry_refusal'),
        )
        for old, new, test in rows:
            self.assertIn(old, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(old, new), '<profile-descriptor-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with patch.object(m, '_guard', namespace['_guard']): TestProfiles(test).run(result)
            self.assertFalse(result.wasSuccessful())

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_inherited_runtime_mutants
    def test_inherited_runtime_mutants(self):
        rows = (
            ('observation_scope', 'test_observation_scope'),
            ('target_scope', 'test_target_scope'),
            ('authorization', 'test_send_fence'),
            ('resource', 'test_peak_reservation'),
            ('cleanup', 'test_cleanup'),
            ('restart', 'test_restart'),
            ('missingness', 'test_missingness'),
        )
        for label, test in rows:
            old, new = composition.base.MUTANTS[label]
            module = composition.w
            tree = ast.parse(Path(module.__file__).read_text())
            matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and
                       old in ast.get_source_segment(Path(module.__file__).read_text(), node)]
            self.assertTrue(matches, label)
            result = unittest.TestResult()
            with self.subTest(mutant=label), ExitStack() as stack:
                for node in matches:
                    source = ast.get_source_segment(Path(module.__file__).read_text(), node).replace(old, new)
                    namespace = dict(vars(module))
                    exec(compile(source, '<inherited-profile-mutant>', 'exec'), namespace)
                    stack.enter_context(patch.object(module, node.name, namespace[node.name]))
                composition.base.TestWorkflow(test).run(result)
            self.assertTrue(result.failures, (label, result.errors))

    # @anchor:wa.s4.mw_profiles.TestProfileMutants.test_strict_decoder_mutants
    def test_strict_decoder_mutants(self):
        source = inspect.getsource(m.gate.strict_load)
        rows = (
            ("_require(key not in out, 'JSON_DUPLICATE_KEY', 2)", 'pass'),
            ('parse_float=reject', 'parse_float=float'),
            ('parse_constant=reject', 'parse_constant=float'),
            ("errors='strict'", "errors='replace'"),
            ("_require(not any(0xD800 <= ord(c) <= 0xDFFF for c in obj), 'JSON_UNICODE', 2)", 'pass'),
        )
        for old, new in rows:
            self.assertIn(old, source)
            namespace = dict(vars(m.gate))
            exec(compile(source.replace(old, new), '<strict-profile-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(mutant=old), patch.object(m.gate, 'strict_load', namespace['strict_load']):
                TestProfiles('test_strict_decoder_public_api').run(result)
            self.assertTrue(result.failures, result.errors)


# @anchor:wa.s4.mw_profiles.TestWave29Profiles
class TestWave29Profiles(unittest.TestCase):
    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.setUp
    def setUp(self):
        self.c = TestProfiles('test_lookup')
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_digest_and_export_schema
    def test_digest_and_export_schema(self):
        c = self.c
        original = copy.deepcopy(c.value)
        for key, invalids in (
            ('ack_sha256', (True, 1, [], {}, '', 'A' * 64, 'a' * 63, 'a' * 64 + '\n')),
            ('export_dir', (None, True, 'relative', '/tmp/../export', '/tmp/export\n'))):
            for invalid in invalids:
                c.value = dict(original, **{key: invalid})
                c._write()
                c._reject(c._load, 2)
        c.value = original
        c._write()
        self.assertEqual(c._invoke().live_ack, original['ack_sha256'])

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_independent_pin
    def test_independent_pin(self):
        c = self.c
        p = c._load()
        c._private(c.ack, b'X' * len(c.token))
        c._reject(lambda: c._invoke(p))
        c._private(c.ack, c.token)
        invocation = c._invoke(p)
        self.assertEqual(invocation.live_ack, hashlib.sha256(c.token).hexdigest())
        self.assertNotIn('SYNTHETIC_ACK', repr(invocation))

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_inactive_legacy
    def test_inactive_legacy(self):
        c = self.c
        original = copy.deepcopy(c.value)
        c.ack.unlink()
        c.value['ack_sha256'] = None
        c._write()
        profile = c._load()
        self.assertIsNone(profile.ack_sha256)
        c._reject(lambda: c._invoke(profile))
        del c.value['ack_sha256'], c.value['export_dir']
        c._write()
        legacy = c._load()
        self.assertIsNone(legacy.ack_sha256)
        self.assertIsNone(legacy.export_dir)
        c._reject(lambda: c._invoke(legacy))
        c._private(c.ack, c.token)
        c.value = original
        c._write()
        self.assertEqual(c._invoke().live_ack, original['ack_sha256'])

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_export_directory
    def test_export_directory(self):
        c = self.c
        c.value['export_dir'] = str(c.root / 'missing')
        c._write()
        c._reject(c._load)
        c.value['export_dir'] = str(c.identity)
        c._write()
        c._reject(c._load)
        alias = c.root / 'export-link'
        alias.symlink_to(c.export, target_is_directory=True)
        c.value['export_dir'] = str(alias)
        c._write()
        c._reject(c._load)
        c.value['export_dir'] = str(c.export)
        c._write()
        for mode in (0o720, 0o702, 0o777):
            c.export.chmod(mode)
            c._reject(c._load)
        c.export.chmod(0o700)
        original = os.fstat
        inode = c.export.stat().st_ino
        def foreign(fd):
            meta = original(fd)
            if meta.st_ino == inode:
                return types.SimpleNamespace(**{key: getattr(meta, key) for key in
                    ('st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')},
                    st_uid=os.geteuid() + 1)
            return meta
        actual_stat = os.stat
        def foreign_stat(*args, **kwargs):
            meta = actual_stat(*args, **kwargs)
            if meta.st_ino == inode:
                return types.SimpleNamespace(**{key: getattr(meta, key) for key in
                    ('st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')},
                    st_uid=os.geteuid() + 1)
            return meta
        with patch.object(m.os, 'fstat', side_effect=foreign), patch.object(m.os, 'stat', side_effect=foreign_stat):
            c._reject(c._load)
        self.assertEqual(c._invoke().identity, c.value['capsule'])
        # A once valid export directory is rechecked before workspace allocation.
        p, inv = c._load(), c._invoke()
        c.export.chmod(0o702)
        with self.c._no_workspace():
            c._reject(lambda: m.run_profile(p, inv, bundle={}, services=None))

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_replacement_before_provisioning
    def test_replacement_before_provisioning(self):
        c = self.c
        p, inv = c._load(), c._invoke()
        # Even replacing the token with identical bytes revokes the invocation.
        replacement = c.root / 'replacement'
        c._private(replacement, c.token)
        replacement.replace(c.ack)
        with self.c._no_workspace(), \
             patch.object(m, '_default_profile_services', wraps=m._default_profile_services) as factory:
            c._reject(lambda: m.run_profile(p, inv, bundle={}, services=None))
            factory.assert_not_called()
        self.assertEqual(c._invoke(p).live_ack, p.ack_sha256)

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_invocation_digest_binding
    def test_invocation_digest_binding(self):
        c = self.c
        p, inv = c._load(), c._invoke()
        with patch.object(m, '_default_profile_services', wraps=m._default_profile_services) as factory:
            object.__setattr__(inv, 'live_ack', '0' * 64)
            c._reject(lambda: m.run_profile(p, inv, bundle={}, services=None))
            factory.assert_not_called()
            object.__setattr__(inv, 'live_ack', p.ack_sha256)
            c._reject(lambda: m.run_profile(p, inv, bundle={}, services=None))
            factory.assert_called_once()

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_authorization_revalidation
    def test_authorization_revalidation(self):
        fixture = TestDefaultProvisioning('test_default_composition')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        calls = []
        delegate = types.SimpleNamespace(inventory=lambda workflow: calls.append('inventory') or 'inventory',
            check=lambda *args: calls.append(args[0]) or composition.YES)
        fixture.policy = replace(fixture.policy, owners=lambda **kwargs: replace(
            fixture._owners(**kwargs), authorization=delegate))
        def action(ports, args):
            auth = ports.authorization
            self.assertEqual(auth.inventory({}), 'inventory')
            for phase in ('send', 'commit'):
                self.assertEqual(auth.check(phase, {}, ports.scope, ports.authority), composition.YES)
            self.assertEqual(calls, ['inventory', 'send', 'commit'])
            fixture.c._private(fixture.c.ack, b'X' * len(fixture.c.token))
            for phase in ('send', 'commit'):
                self.assertNotEqual(auth.check(phase, {}, ports.scope, ports.authority), composition.YES)
            with self.assertRaises(m.ProfileError):
                auth.inventory({})
            self.assertEqual(calls, ['inventory', 'send', 'commit'])
        fixture.action = action
        self.assertEqual(fixture._run().rc, 4)

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_initializer_contract_cli
    def test_initializer_contract_cli(self):
        c = self.c
        args = ['--site', 'contract', '--container', 'synthetic', '--window', 'w1',
                '--export-dir', str(c.export), '--capsule', 'synthetic', '--identity-env', str(c.identity)]
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(initializer.main(args, profile_dir=c.root), 0)
        profile = m.load_profile('contract', profile_dir=c.root)
        self.assertEqual(profile.capsule, 'synthetic')
        self.assertEqual(profile.identity_env, c.identity)
        token = c.root / 'contract.ack'
        self.assertFalse(token.exists())
        self.assertIn(f'head -c 32 /dev/urandom > {token}; chmod 600 {token}', out.getvalue())
        self.assertIn('--site contract --activate', out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(initializer.main(args, profile_dir=c.root), 5)

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_initializer_lifecycle
    def test_initializer_lifecycle(self):
        c = self.c
        args = ['--site', 'new', '--container', 'synthetic', '--window', 'w1',
            '--export-dir', str(c.export), '--capsule', 'synthetic', '--identity-env', str(c.identity)]
        mask = os.umask(0o777)
        try:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(initializer.main(args, profile_dir=c.root), 0)
        finally:
            os.umask(mask)
        self.assertIn('Inactive', output.getvalue())
        target, token = c.root / 'new.json', c.root / 'new.ack'
        self.assertFalse(token.exists())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        raw = target.read_bytes()
        self.assertEqual(raw, (m.gate.canon(json.loads(raw)) + '\n').encode())
        inactive = m.load_profile('new', profile_dir=c.root)
        c._reject(lambda: c._invoke(inactive))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(initializer.main(args, profile_dir=c.root), 5)
            self.assertEqual(initializer.main(['--site', 'new', '--activate'], profile_dir=c.root), 5)
        self.assertEqual(target.read_bytes(), raw)
        self.assertFalse(token.exists())
        c._private(token, c.token)
        before = token.stat()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(initializer.main(['--site', 'new', '--activate'], profile_dir=c.root), 0)
            self.assertEqual(initializer.main(args, profile_dir=c.root), 5)
            self.assertEqual(initializer.main(['--site', 'new', '--activate'], profile_dir=c.root), 5)
        active = m.load_profile('new', profile_dir=c.root)
        self.assertEqual(active.ack_sha256, hashlib.sha256(c.token).hexdigest())
        self.assertEqual(c._invoke(active).live_ack, hashlib.sha256(c.token).hexdigest())
        self.assertEqual(m._metadata(token.stat()), m._metadata(before))
        self.assertEqual(token.read_bytes(), c.token)
        self.assertEqual(list(c.root.glob('.activate-*')), [])

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_initializer_defaults
    def test_initializer_defaults(self):
        c = self.c
        args = ['--site', 'defaults', '--container', 'synthetic', '--window', 'w1',
                '--export-dir', str(c.export)]
        with redirect_stdout(io.StringIO()), patch.object(initializer, 'IDENTITY_ENV', str(c.root / 'absent')):
            self.assertEqual(initializer.main(args, profile_dir=c.root), 5)
        self.assertFalse((c.root / 'defaults.json').exists())
        with redirect_stdout(io.StringIO()) as output, patch.object(initializer, 'IDENTITY_ENV', str(c.identity)):
            self.assertEqual(initializer.main(args, profile_dir=c.root), 0)
        profile = m.load_profile('defaults', profile_dir=c.root)
        self.assertEqual(profile.capsule, profile.container)
        self.assertEqual(profile.identity_env, c.identity)
        self.assertIn('--activate', output.getvalue())
        token = c.root / 'defaults.ack'
        self.assertFalse(token.exists())
        c._private(token, c.token)  # Operator fixture, outside initializer execution.
        before = m._metadata(token.stat())
        with redirect_stdout(io.StringIO()), patch.object(initializer, 'IDENTITY_ENV', str(c.root / 'absent')):
            self.assertEqual(initializer.main(['--site', 'defaults', '--activate'], profile_dir=c.root), 0)
        self.assertEqual(m._metadata(token.stat()), before)
        self.assertEqual(m.load_profile('defaults', profile_dir=c.root).ack_sha256, hashlib.sha256(c.token).hexdigest())

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_notice_snapshot
    def test_notice_snapshot(self):
        info = types.SimpleNamespace(st_size=12, st_mtime_ns=34)
        with patch.object(Path, 'stat', return_value=info), \
             patch.object(Path, 'is_dir', side_effect=lambda: False) as is_directory, \
             patch.object(Path, 'iterdir', side_effect=((Path('z'), Path('a')), (Path('lease'),))):
            is_directory.side_effect = [False, True, True]
            state = _notice_state(_REAL_NOTICE_PATH)
        self.assertEqual(state, ((True, 12, 34, None), (True, 12, 34, ('a', 'z')), (True, 12, 34, ('lease',))))
        with patch.object(Path, 'stat', side_effect=FileNotFoundError):
            self.assertEqual(_notice_state(_REAL_NOTICE_PATH), ((False, None, None, None),) * 3)

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_notice_guard
    def test_notice_guard(self):
        absent = (False, None, None)
        existing = (True, 12, 34)
        for before in (absent, existing):
            with patch(__name__ + '._notice_state', return_value=before):
                _notice_guard(self, before)
            for after in ((True, 13, 34), (True, 12, 35), (not before[0], None, None)):
                with patch(__name__ + '._notice_state', return_value=after):
                    with self.assertRaises(AssertionError):
                        _notice_guard(self, before)
        journal = (True, 12, 34, None)
        directory = (True, 40, 56, ('existing',))
        missing = (False, None, None, None)
        before = (journal, directory, directory)
        with patch(__name__ + '._notice_state', return_value=before):
            _notice_guard(self, before)
        for index in (1, 2):
            for changed in (missing, (True, 40, 57, ('existing',)),
                            (True, 40, 56, ('existing', 'unexpected'))):
                after = list(before)
                after[index] = changed
                with patch(__name__ + '._notice_state', return_value=tuple(after)):
                    with self.assertRaises(AssertionError):
                        _notice_guard(self, before)
        # Manual nested setup must still run its registered guard after failure.
        failed = TestProfiles('test_lookup')
        with patch(__name__ + '._notice_state', return_value=existing), \
             patch(__name__ + '._notice_guard') as guard, \
             patch.object(tempfile, 'TemporaryDirectory', side_effect=RuntimeError('synthetic setup failure')):
            with self.assertRaisesRegex(RuntimeError, '^synthetic setup failure$'):
                failed.setUp()
            failed.doCleanups()
        guard.assert_called_once_with(failed, existing)

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_initializer_guards
    def test_initializer_guards(self):
        c = self.c
        args = ['--site', 'new', '--container', 'synthetic', '--window', 'w1',
            '--export-dir', str(c.export), '--capsule', 'synthetic', '--identity-env', str(c.identity)]
        alias = c.root / 'alias'
        alias.symlink_to(c.root, target_is_directory=True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(initializer.main(args, profile_dir=alias), 5)
            for key in ('--container', '--window', '--export-dir'):
                invalid = list(args)
                index = invalid.index(key)
                del invalid[index:index + 2]
                self.assertEqual(initializer.main(invalid, profile_dir=c.root), 2)
            self.assertEqual(initializer.main(args + ['--unknown', 'SYNTHETIC_CANARY'], profile_dir=c.root), 2)
            c.root.chmod(0o750)
            self.assertEqual(initializer.main(args, profile_dir=c.root), 5)
            c.root.chmod(0o700)
            self.assertEqual(initializer.main(args, profile_dir=c.root), 0)
        self.assertFalse((c.root / 'new.ack').exists())
        self.assertIsNone(m.load_profile('new', profile_dir=c.root).ack_sha256)

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_initializer_activation_race
    def test_initializer_activation_race(self):
        c = self.c
        args = ['--site', 'new', '--container', 'synthetic', '--window', 'w1',
            '--export-dir', str(c.export), '--capsule', 'synthetic', '--identity-env', str(c.identity)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(initializer.main(args, profile_dir=c.root), 0)
        target, token = c.root / 'new.json', c.root / 'new.ack'
        initial = target.read_bytes()
        c._private(token, c.token)
        write = initializer._write
        def race(fd, value):
            write(fd, value)
            c._private(target, initial + b' ')
        with patch.object(initializer, '_write', side_effect=race), redirect_stdout(io.StringIO()):
            self.assertEqual(initializer.main(['--site', 'new', '--activate'], profile_dir=c.root), 5)
        self.assertEqual(target.read_bytes(), initial + b' ')
        self.assertFalse(list(c.root.glob('.activate-*')))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(initializer.main(['--site', 'new', '--activate'], profile_dir=c.root), 0)
        self.assertEqual(m.load_profile('new', profile_dir=c.root).ack_sha256,
                         hashlib.sha256(c.token).hexdigest())

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_capacity_and_cleanup_mutants
    def test_capacity_and_cleanup_mutants(self):
        for module, label in ((m, 'host_policy_bounds'), (live.m, 'budgets')):
            original = module._need
            def bypass(ok, gate, *args):
                if gate != label:
                    original(ok, gate, *args)
            result = unittest.TestResult()
            with self.subTest(gate=label), patch.object(module, '_need', bypass):
                TestDefaultProvisioning('test_capacity_contradiction').run(result)
            self.assertTrue(result.failures, (label, result.errors))
            self.assertFalse(result.errors, (label, result.errors))
        source = inspect.getsource(m._default_profile_services)
        namespace = dict(vars(m))
        exec(compile(source.replace('and not primary_failure', ''), '<cleanup-primary-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(m, '_default_profile_services', namespace['_default_profile_services']):
            TestDefaultProvisioning('test_cleanup_preserves_failure').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_observation_and_download_predicates
    def test_observation_and_download_predicates(self):
        fixture = composition.TestComposition('test_factory_arguments')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        outcomes = []
        # Assertions follow the public context so its native-error translator
        # cannot convert a discriminating assertion into an incidental error.
        with fixture._open() as public:
            predicate = composition.base._predicate
            value = public.observations.acquire(predicate('signal'), fixture.b.limits.deadline_ns)
            outcomes.append(owners._yes(public.observations.validate(value)))
            outcomes.append(not owners._yes(public.observations.validate(replace(value))))
            for name, bad in (('foreign', replace(value, scope='foreign')),
                              ('incomplete', replace(value, complete=False))):
                fixture.b.observations.values[name] = [replace(bad, capability_id=name, publication=name)]
                try:
                    public.observations.acquire(predicate(name), fixture.b.limits.deadline_ns)
                except ValueError:
                    outcomes.append(True)
                else:
                    outcomes.append(False)
            fixture.b.observations.denied = True
            try:
                public.observations.acquire(predicate('stale'), fixture.b.limits.deadline_ns)
            except ValueError:
                outcomes.append(True)
            else:
                outcomes.append(False)
            fixture.b.observations.denied = False
        self.assertEqual(outcomes, [True] * 5)
        rows = [dict(name='synthetic', bytes=10, mtime=1)]
        fixture.native.read = lambda *args: list(rows)
        downloads = owners.DownloadsObservations(fixture.invocation, ports=fixture.ports,
            capability_id='downloads', revision='v1', unit='identities', finished_gate=lambda *args: composition.YES)
        complete = downloads.acquire(predicate('downloads'), fixture.b.limits.deadline_ns)
        self.assertEqual(downloads.validate(complete), composition.YES)
        rows.append(dict(name='growing', bytes=1, mtime=2))
        downloads._gate = lambda *args: composition.NO
        with self.assertRaises(ValueError):
            downloads.acquire(predicate('downloads'), fixture.b.limits.deadline_ns)

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_upstream_public_gate_mutants
    def test_upstream_public_gate_mutants(self):
        import textwrap
        rows = (
            (owners._Observations, '_valid',
             'value.scope == p.scope and value.authority == p.authority', 'True',
             'test_observation_registration'),
            (owners._Observations, '_valid', 'value.complete is True', 'True',
             'test_observation_registration'),
            (owners._Observations, '_valid', '_yes(p.observations.validate(value))', 'True',
             'test_observation_registration'),
            (owners._Observations, 'validate', 'self._retained.get(value.publication) is value', 'True',
             'test_observation_registration'),
            (owners.DownloadsObservations, 'acquire', '_admit(self._gate(self._invocation, rows))', 'pass',
             'test_download_observations'),
        )
        for cls, method, old, new, name in rows:
            nominal = unittest.TestResult()
            TestWave29Profiles('test_observation_and_download_predicates').run(nominal)
            self.assertTrue(nominal.wasSuccessful(), (nominal.failures, nominal.errors))
            source = textwrap.dedent(inspect.getsource(getattr(cls, method)))
            self.assertIn(old, source)
            namespace = dict(vars(owners))
            exec(compile(source.replace(old, new), '<upstream-public-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(predicate=old), patch.object(cls, method, namespace[method]):
                TestWave29Profiles('test_observation_and_download_predicates').run(result)
            self.assertTrue(result.failures, (old, result.errors))
            self.assertFalse(result.errors, (old, result.errors))

    # @anchor:wa.s4.mw_profiles.TestWave29Profiles.test_new_predicate_mutants
    def test_new_predicate_mutants(self):
        rows = (
            ('digest_schema', 'test_digest_and_export_schema'),
            ('token_pin', 'test_independent_pin'),
            ('token_binding', 'test_replacement_before_provisioning'),
            ('notice_ack', 'test_invocation_digest_binding'),
            ('export_owner', 'test_export_directory'),
            ('export_mode', 'test_export_directory'),
            ('init_directory', 'test_initializer_guards'),
            ('init_inactive', 'test_initializer_lifecycle'),
            ('init_profile_binding', 'test_initializer_activation_race'),
        )
        original = m._need
        for label, name in rows:
            def bypass(ok, gate, rc=5):
                if gate != label:
                    original(ok, gate, rc)
            result = unittest.TestResult()
            with self.subTest(gate=label), patch.object(m, '_need', bypass):
                TestWave29Profiles(name).run(result)
            self.assertTrue(result.failures, (label, result.errors))
            self.assertFalse(result.errors, (label, result.errors))
        source = inspect.getsource(initializer.main)
        rows = (
            ("args.capsule = args.container if args.capsule is None else args.capsule",
             "pass", 'test_initializer_defaults'),
            ("args.identity_env = IDENTITY_ENV if args.identity_env is None else args.identity_env",
             "pass", 'test_initializer_defaults'),
            ('os.O_CREAT | os.O_EXCL', 'os.O_CREAT', 'test_initializer_lifecycle'),
            ("value['ack_sha256'] = digest", "value['ack_sha256'] = None", 'test_initializer_lifecycle'),
            ("print('Inactive profile created.",
             "os.close(os.open(args.site + '.ack', os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=parent))\n                print('Inactive profile created.",
             'test_initializer_lifecycle'),
        )
        for old, new, name in rows:
            self.assertIn(old, source)
            namespace = dict(vars(initializer))
            exec(compile(source.replace(old, new), '<initializer-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(initializer=old), patch.object(initializer, 'main', namespace['main']):
                TestWave29Profiles(name).run(result)
            self.assertTrue(result.failures, (old, result.errors))
            self.assertFalse(result.errors, (old, result.errors))
        # Mutate the public wrapper, rather than calling a private token helper.
        for method in ('check', 'inventory'):
            source = inspect.getsource(getattr(m._PinnedAuthorization, method))
            import textwrap
            source = textwrap.dedent(source).replace('_validate_token(self._profile, self._invocation)', 'pass')
            namespace = dict(vars(m))
            exec(compile(source, '<authorization-pin-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with patch.object(m._PinnedAuthorization, method, namespace[method]):
                TestWave29Profiles('test_authorization_revalidation').run(result)
            self.assertTrue(result.failures, (method, result.errors))
            self.assertFalse(result.errors, (method, result.errors))


# @anchor:wa.s4.mw_profiles.TestProducerProbe
class TestProducerProbe(unittest.TestCase):
    # @anchor:wa.s4.mw_profiles.TestProducerProbe.test_bounded_probe
    def test_bounded_probe(self):
        # This synthetic contract is not a claimed installed producer interface.
        contract = m._ProducerProbeContract(('fixture-usage',), 'fixture-usage')
        output = b'fixture-usage\n--paste --type-stdin --scroll --drag downloads\n'
        native = m.NativeReadPolicy(1024, 1024, 256, 4096, 1, 200_000_000, 200_000_000, 64)
        fixtures = (
            ('positive', 'import os; os.write(1, ' + repr(output) + ')', True, None),
            ('overflow', "import os; os.write(1, b'x' * 65537)", False, m.Reason.CAPACITY),
            ('timeout', 'import time; time.sleep(2)', False, m.Reason.DEADLINE),
            ('status', 'import os; os.write(1, ' + repr(output) + '); raise SystemExit(3)', False, m.Reason.MISSING),
            ('malformed', 'import os; os.write(1, ' + repr(output + b'\xff') + ')', False, m.Reason.MISSING),
            ('lookalike', "print('fixture-usage\\n--paste-more downloads-extra')", False, m.Reason.MISSING),
            ('unknown', "print('unknown')", False, m.Reason.MISSING),
            ('unknown_token', "print('fixture-usage\\n--scroll unknown')", False, m.Reason.MISSING),
            ('stderr', 'import os; os.write(1, ' + repr(output) + '); os.write(2, b"x")', False, m.Reason.MISSING),
            ('stderr_only', 'import os; os.write(2, ' + repr(output) + ')', False, m.Reason.MISSING),
            ('missing_lf', 'import os; os.write(1, ' + repr(output[:-1]) + ')', False, m.Reason.MISSING),
            ('crlf', 'import os; os.write(1, ' + repr(output.replace(b'\n', b'\r\n')) + ')', False, m.Reason.MISSING),
            ('empty_tokens', "print('fixture-usage\\n')", False, m.Reason.MISSING),
            ('subset', "print('fixture-usage\\n--scroll')", True, None),
            ('version', "print('fixture-usage v2\\n--paste')", False, m.Reason.MISSING),
            ('control', "print('fixture-usage\\n--paste\\t--scroll')", False, m.Reason.MISSING),
            ('extra_line', 'import os; os.write(1, ' + repr(output + b'\n') + ')', False, m.Reason.MISSING),
            ('duplicate', "print('fixture-usage\\n--paste --paste')", False, m.Reason.MISSING),
        )
        for label, script, valid, reason in fixtures:
            with self.subTest(fixture=label):
                resource = types.SimpleNamespace(reserve=MagicMock(return_value=composition.YES))
                record = types.SimpleNamespace(resources=resource,
                    config=types.SimpleNamespace(native_policy=native,
                        limits=types.SimpleNamespace(deadline_ns=time.monotonic_ns() + 3_000_000_000)))
                children = []
                def spawn(argv, **kwargs):
                    self.assertEqual(argv, [m.BINARY, 'fixture-usage'])
                    resource.reserve.assert_called_once_with('producer_probe', 64 * 65536)
                    self.assertFalse(kwargs['shell'])
                    child = _REAL_POPEN([sys.executable, '-c', script], **kwargs)
                    children.append(child)
                    return child
                with patch.object(m.subprocess, 'Popen', side_effect=spawn) as factory:
                    started = time.monotonic()
                    result = m._producer_evidence(time.monotonic_ns, record=record, contract=contract)
                    elapsed = time.monotonic() - started
                factory.assert_called_once()
                self.assertLess(elapsed, 0.75, 'probe did not honor timeout/join bounds')
                self.assertEqual(result.valid, valid)
                self.assertEqual(result.reason, reason)
                self.assertEqual(len(children), 1, 'probe must reserve before spawning its child')
                self.assertIsNotNone(children[0].returncode)
                self.assertTrue(children[0].stdout.closed and children[0].stderr.closed)
                if valid:
                    expected = b'fixture-usage\n--scroll\n' if label == 'subset' else output
                    operations = ('scroll',) if label == 'subset' else ('paste', 'type-stdin', 'scroll', 'drag', 'downloads')
                    self.assertEqual(result.operations, frozenset(operations))
                    self.assertEqual(result.digest, hashlib.sha256(expected).hexdigest())
                    self.assertEqual(result.output_bytes, len(expected))
                else:
                    self.assertFalse(result.operations)
                    self.assertIsNone(result.digest)
        with patch.object(m.subprocess, 'Popen') as factory:
            self.assertFalse(m._producer_evidence(time.monotonic_ns, record=record).valid)
            factory.assert_not_called()
            resource.reserve.return_value = composition.NO
            self.assertFalse(m._producer_evidence(time.monotonic_ns, record=record, contract=contract).valid)
            factory.assert_not_called()

    # @anchor:wa.s4.mw_profiles.TestProducerProbe.test_unproven_join
    def test_unproven_join(self):
        policy = m.NativeReadPolicy(1024, 1024, 256, 4096, 1, 200_000_000, 200_000_000, 64)
        record = types.SimpleNamespace(resources=types.SimpleNamespace(reserve=lambda *args: composition.YES),
            config=types.SimpleNamespace(native_policy=policy,
            limits=types.SimpleNamespace(deadline_ns=time.monotonic_ns() + 3_000_000_000)))
        for unproven in (False, True):
            children = []
            def spawn(argv, **kwargs):
                child = _REAL_POPEN([sys.executable, '-c',
                    "print('srf-identity-surface capabilities v1\\n--scroll')"], **kwargs)
                children.append(child)
                wait = child.wait
                def joined(*args, **kwargs):
                    result = wait(*args, **kwargs)
                    if unproven:
                        raise subprocess.TimeoutExpired('synthetic', 0)
                    return result
                child.wait = joined
                return child
            with patch.object(m.subprocess, 'Popen', side_effect=spawn):
                if unproven:
                    with self.assertRaises(m.ProfileError):
                        m._producer_evidence(time.monotonic_ns, record=record, contract=m._PRODUCTION_PROBE)
                else:
                    self.assertTrue(m._producer_evidence(time.monotonic_ns, record=record,
                                                         contract=m._PRODUCTION_PROBE).valid)
            self.assertTrue(children[0].stdout.closed and children[0].stderr.closed)
            self.assertIsNotNone(children[0].returncode)


# @anchor:wa.s4.mw_profiles.TestMWP9Controls
class TestMWP9Controls(unittest.TestCase):
    # @anchor:wa.s4.mw_profiles.TestMWP9Controls.test_semantic_mutants
    def test_semantic_mutants(self):
        import textwrap
        rows = [
            (m._ProfileObservations, 'invalidate', 'self.predicates.clear()', 'pass',
             'test_signal_dispatch_and_oracle'),
            (m._ProfileObservations, 'acquire', 'type(d.mark_cap_reached) is bool if signal is not None else d.mark_cap_reached is False',
             'True if signal is not None else d.mark_cap_reached is False', 'test_capped_signal_observations'),
            (m._ProfileObservations, 'acquire', 'describe_rgb = rgb[y0:y1, x0:x1]', 'describe_rgb = rgb',
             'test_regional_signal_observations'),
            (m._ProfileObservations, 'acquire', 'x=mark.x + x0, y=mark.y + y0', 'x=mark.x, y=mark.y',
             'test_regional_signal_observations'),
            (m._ProfileObservations, 'acquire', "0 <= x0 < x1 <= frame['width']", 'True',
             'test_regional_signal_observations'),
            (m._ProfileObservations, 'acquire', "(rgb.shape[1], rgb.shape[0]) == (frame['width'], frame['height'])", 'True',
             'test_regional_signal_observations'),
            (m._ProfileObservations, 'acquire', 'hashlib.sha256(describe_rgb.tobytes()).hexdigest() == d.frame_sha256', 'True',
             'test_regional_signal_observations'),
            (m._ProfileObservations, 'acquire', '(d.width, d.height) == (describe_rgb.shape[1], describe_rgb.shape[0])', 'True',
             'test_regional_signal_observations'),
            (m._ProfileObservations, 'acquire', 'mark.x + mark.w <= d.width', 'True',
             'test_regional_signal_observations'),
            (m._ProfileObservations, 'acquire', 'not d.mark_cap_reached)', 'True)',
             'test_capped_signal_observations'),
            (m._ProfileObservations, 'acquire', 'type(d.mark_cap_reached) is bool if signal is not None else d.mark_cap_reached is False',
             'd.mark_cap_reached is False', 'test_capped_signal_observations'),
            (m._ProfileCapabilities, '_production', "self._signal(spec.get('predicate')) == self._signal(args[0].get('predicate'))",
             'True', 'test_signal_dispatch_and_oracle'),
            (m._ProfileObservations, 'acquire', "_yes(p.resources.reserve('signal_metadata',\n                  8 * r.config.evidence_policy.metadata_bytes + 1024))",
             'True', 'test_signal_dispatch_and_oracle'),
            (m._ProfileCapabilities, '_production', "spec.get('mode') == 'state_signal'", 'True',
             'test_signal_dispatch_and_oracle'),
            (m._ProfileCapabilities, 'capability_check', "obj.get('version_constraint') == '==' + _ADAPTER_VERSION",
             'True', 'test_signal_dispatch_and_oracle'),
            (m._ProfileCapabilities, '_production', 'gate.canon(spec) == gate.canon(self.authorization.workflow)',
             'True', 'test_signal_dispatch_and_oracle'),
            (m._ProfileCapabilities, '_production', 'observations.predicates.get(publication) == identity',
             'True', 'test_signal_dispatch_and_oracle'),
            (m._ProfileCapabilities, '_invoke', "operation != 'paste' or _yes(self.operation('type-stdin'))",
             'True', 'test_trusted_native_action_admission'),
            (m._ProfileCapabilities, '_invoke', "_yes(a.check('step', a.workflow, r.scope, r.authority))",
             'True', 'test_trusted_native_action_admission'),
            (m._ProfileCapabilities, '_invoke', "_yes(self.operation(operation))", 'True',
             'test_trusted_native_action_admission'),
            (m._DefaultHost, 'owners', 'contract=_PRODUCTION_PROBE', 'contract=None',
             'test_production_semantic_workflow'),
            (m._Resources, 'reserve', 'amount <= self._config.limits.peak_bytes', 'True', 'test_peak_admission'),
        ]
        for cls, method, old, new, control in rows:
            with self.subTest(gate=old):
                source = textwrap.dedent(inspect.getsource(getattr(cls, method)))
                self.assertIn(old, source)
                namespace = dict(vars(m))
                exec(compile(source.replace(old, new), '<mwp9-control-mutant>', 'exec'), namespace)
                result = unittest.TestResult()
                with patch.object(cls, method, namespace[method]):
                    TestDefaultProvisioning(control).run(result)
                self.assertTrue(result.failures, (old, result.errors))
                self.assertFalse(result.errors, (old, result.errors))

    # @anchor:wa.s4.mw_profiles.TestMWP9Controls.test_runner_busy_mutant
    def test_runner_busy_mutant(self):
        import textwrap
        source = textwrap.dedent(inspect.getsource(composition.w.verify_step))
        old = 'if busy is not None and not state.busy_seen:'
        self.assertIn(old, source)
        namespace = dict(vars(composition.w))
        exec(compile(source.replace(old, 'if False:'), '<runner-busy-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(composition.w, 'verify_step', namespace['verify_step']):
            TestDefaultProvisioning('test_production_semantic_workflow').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)

    # @anchor:wa.s4.mw_profiles.TestMWP9Controls.test_frozen_binding_names
    def test_frozen_binding_names(self):
        from tests.test_mw_handlers_unit import _predicate
        definitions = composition.base.BUNDLE['$defs']
        admitted = _predicate('mw.frame.stable.v1', k=3)
        self.assertTrue(m.gate.conforms(admitted, definitions['predicate'], definitions))
        for predicate in (_predicate('mw.frame.stable.v1', stable_ms=2),
                          _predicate(phrase='Go', region_px=[0, 0, 10, 10])):
            self.assertTrue(m.gate.conforms(predicate, definitions['predicate'], definitions))
            self.assertIsNotNone(m.parse_signal(predicate, metadata_bytes=4096, string_bytes=4096))
            for bad in (dict(predicate, capability_id='other'),
                        dict(predicate, bindings=[dict(predicate['bindings'][-1], type='string')]),
                        dict(predicate, bindings=[dict(predicate['bindings'][-1], value=True)])):
                self.assertFalse(m.gate.conforms(bad, definitions['predicate'], definitions))

    # @anchor:wa.s4.mw_profiles.TestMWP9Controls.test_round2_mutants
    def test_round2_mutants(self):
        source = textwrap.dedent(inspect.getsource(m._ProfileObservations.acquire))
        for old, new in (('signal.region_px is not None)))', 'False)))'),
                         ('signal.region_px is not None)))',
                          "(signal.region_px is not None and signal.capability_id == 'mw.ocr.text_absent.v1'))))"),
                         ('not d.mark_cap_reached)', 'bool(d.marks) and not d.mark_cap_reached)'),
                         ('not d.mark_cap_reached)', 'True)')):
            self.assertIn(old, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(old, new), '<round2-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with patch.object(m._ProfileObservations, 'acquire', namespace['acquire']):
                TestDefaultProvisioning('test_empty_regional_absence').run(result)
            self.assertTrue(result.failures, (old, result.errors))
            self.assertFalse(result.errors)
        definitions = copy.deepcopy(composition.base.BUNDLE['$defs'])
        definitions['predicate'] = definitions['predicate']['anyOf'][0]
        result = unittest.TestResult()
        with patch.dict(composition.base.BUNDLE, {'$defs': definitions}):
            TestMWP9Controls('test_frozen_binding_names').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)
        for gate in ('capability_id', 'type', 'value'):
            definitions = copy.deepcopy(composition.base.BUNDLE['$defs'])
            extended = definitions['predicate']['anyOf'][1]
            if gate == 'capability_id':
                extended['properties']['capability_id'] = {}
            else:
                for branch in extended['properties']['bindings']['items']['anyOf'][1:]:
                    branch['properties'][gate] = {}
            result = unittest.TestResult()
            with patch.dict(composition.base.BUNDLE, {'$defs': definitions}):
                TestMWP9Controls('test_frozen_binding_names').run(result)
            self.assertTrue(result.failures, (gate, result.errors))
            self.assertFalse(result.errors)

    # @anchor:wa.s4.mw_profiles.TestMWP9Controls.test_cleanup_mutant
    def test_cleanup_mutant(self):
        source = inspect.getsource(m._producer_evidence)
        old = 'raise ProfileError(reason=Reason.DEADLINE) from None'
        self.assertIn(old, source)
        namespace = dict(vars(m))
        exec(compile(source.replace(old, 'pass'), '<probe-join-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(m, '_producer_evidence', namespace['_producer_evidence']):
            TestProducerProbe('test_unproven_join').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)


# @anchor:wa.s4.mw_profiles.TestMWP8IControls
class TestMWP8IControls(unittest.TestCase):
    # @anchor:wa.s4.mw_profiles.TestMWP8IControls.test_remaining_spec_mutants
    def test_remaining_spec_mutants(self):
        import textwrap
        rows = (
            (m._Provisioning, '_export',
             "if kwargs.get('operation') == 'downloads'", "return AdmissionCheck(Truth.TRUE, None)\n    if kwargs.get('operation') == 'downloads'",
             TestDefaultProvisioning, 'test_default_export_custody_support'),
            (owners.DownloadsCodec, 'decode',
             'return frozenset(self._names[n] for n in names)',
             "return frozenset(hashlib.sha256(str(row).encode()).hexdigest() for row in rows)",
             TestDefaultProvisioning, 'test_default_download_observations'),
            (initializer, 'main', 'if args.activate:',
             "args.capsule = args.container if args.capsule is None else args.capsule\n            args.identity_env = IDENTITY_ENV if args.identity_env is None else args.identity_env\n            if args.activate:",
             TestWave29Profiles, 'test_initializer_defaults'),
        )
        for owner, method, old, new, case, name in rows:
            source = textwrap.dedent(inspect.getsource(getattr(owner, method)))
            self.assertIn(old, source)
            module = initializer if owner is initializer else owners if owner is owners.DownloadsCodec else m
            namespace = dict(vars(module))
            exec(compile(source.replace(old, new), '<remaining-spec-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(predicate=old), patch.object(owner, method, namespace[method]):
                case(name).run(result)
            self.assertTrue(result.failures, (old, result.errors))
            self.assertFalse(result.errors, (old, result.errors))

    # @anchor:wa.s4.mw_profiles.TestMWP8IControls.test_new_owner_gate_mutants
    def test_new_owner_gate_mutants(self):
        original = m._need
        rows = [(gate, 'test_default_observation_backend') for gate in (
            'observation_surface', 'observation_sequence', 'observation_dimensions', 'observation_interval',
            'observation_ocr', 'observation_missingness', 'observation_metadata', 'observation_rgb',
            'observation_reservation', 'observation_publications', 'observation_expiry')]
        rows += [(gate, 'test_default_capability_handlers') for gate in (
            'capability_authorization', 'capability_operation', 'capability_owners', 'capability_transport')]
        rows += [(gate, 'test_default_download_observations') for gate in (
            'downloads_admission', 'downloads_rows', 'downloads_bytes')]
        for label, name in rows:
            def removed(ok, actual, rc=5):
                if actual != label:
                    return original(ok, actual, rc)
            result = unittest.TestResult()
            with self.subTest(gate=label), patch.object(m, '_need', side_effect=removed):
                TestDefaultProvisioning(name).run(result)
            self.assertTrue(result.failures, (label, result.errors))
            self.assertFalse(result.errors, (label, result.errors))

    # @anchor:wa.s4.mw_profiles.TestMWP8IControls.test_publication_and_handler_mutants
    def test_publication_and_handler_mutants(self):
        import textwrap
        rows = [
            (m._ProfileObservations, 'validate', 'self.registered.get(value.publication) is value', 'True'),
            (m._ProfileObservations, 'validate', 'value.scope is r.scope', 'True'),
            (m._ProfileObservations, 'validate', 'value.authority is r.authority', 'True'),
            (m._ProfileObservations, 'validate', 'value.capability_revision == r.config.capability_revision', 'True'),
            (m._ProfileObservations, 'validate', 'value.complete is True', 'True'),
            (m._ProfileObservations, 'validate', 'value.missing_reason is None', 'True'),
            (m._ProfileObservations, 'validate', 'min(self.expiries.get(value.publication, 0), p.limits.deadline_ns)',
             'p.limits.deadline_ns'),
            (m._ProfileCapabilities, '_invoke', 'self.handlers.get((method, key))', 'None'),
            (m._ProfileCapabilities, 'final', "type(value) is ObservationValue and self.observations is not None and\n             _yes(self.observations.validate(value)) and value.output_kind == 'boolean' and value.value is True", 'True'),
        ]
        for cls, method, old, new in rows:
            source = textwrap.dedent(inspect.getsource(getattr(cls, method)))
            self.assertIn(old, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(old, new), '<owner-control-mutant>', 'exec'), namespace)
            name = ('test_default_observation_ownership' if cls is m._ProfileObservations else
                    'test_default_capability_handlers')
            result = unittest.TestResult()
            with self.subTest(predicate=old), patch.object(cls, method, namespace[method]):
                TestDefaultProvisioning(name).run(result)
            self.assertTrue(result.failures, (old, result.errors))
            self.assertFalse(result.errors, (old, result.errors))

    # @anchor:wa.s4.mw_profiles.TestMWP8IControls.test_probe_mutants
    def test_probe_mutants(self):
        rows = [
            ('used + len(chunk) > 65536', 'False'),
            ('and not stderr_seen', ''),
            ("len(lines) == 3 and lines[2] == ''", 'len(lines) >= 2'),
            ("lines[0] == contract.header", 'True'),
            ('len(tokens) == len(set(tokens))', 'True'),
            ('bool(tokens)', 'True'),
            ('all(32 <= ord(c) < 127 for c in lines[1])', 'True'),
            ('clock() >= deadline', 'False'),
            ("child.returncode == 0 and clock() < deadline", 'True'),
            ("errors='strict'", "errors='ignore'"),
            ('tokens = lines[1].split()', "tokens = [next((v for v in ('--paste', '--type-stdin', '--scroll', '--drag', 'downloads') if t.startswith(v)), t) for t in lines[1].split()]"),
            ("_yes(record.resources.reserve('producer_probe', 64 * 65536))", 'True'),
        ]
        source = inspect.getsource(m._producer_evidence)
        for old, new in rows:
            self.assertIn(old, source)
            namespace = dict(vars(m))
            exec(compile(source.replace(old, new), '<probe-control-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(predicate=old), patch.object(m, '_producer_evidence', namespace['_producer_evidence']):
                TestProducerProbe('test_bounded_probe').run(result)
            self.assertTrue(result.failures, (old, result.errors))
            self.assertFalse(result.errors, (old, result.errors))

    # @anchor:wa.s4.mw_profiles.TestMWP8IControls.test_control_mutants
    def test_control_mutants(self):
        original = m._need
        rows = (
            ('authorization_active', 'test_bound_authorization'),
            ('authorization_mode', 'test_bound_authorization'),
            ('authorization_profile', 'test_bound_authorization'),
            ('authorization_lease', 'test_bound_authorization'),
            ('authorization_scope', 'test_bound_authorization'),
            ('authorization_phase', 'test_bound_authorization'),
            ('authorization_pin', 'test_bound_workflow_pin'),
            ('authorization_segments', 'test_bound_inventory'),
            ('downloads_binding', 'test_download_export_admission'),
            ('downloads_destination', 'test_download_export_admission'),
            ('downloads_probe', 'test_download_export_admission'),
            ('downloads_authorization', 'test_download_export_admission'),
            ('host_ai_locate', 'test_ai_locate_exact_bool'),
        )
        for label, name in rows:
            def removed(ok, actual, rc=5):
                if actual != label:
                    return original(ok, actual, rc)
            result = unittest.TestResult()
            with self.subTest(gate=label), patch.object(m, '_need', side_effect=removed):
                TestDefaultProvisioning(name).run(result)
            self.assertTrue(result.failures, (label, result.errors))
            self.assertFalse(result.errors, (label, result.errors))

    # @anchor:wa.s4.mw_profiles.TestMWP8IControls.test_notice_state_mutants
    def test_notice_state_mutants(self):
        import textwrap
        rows = (
            (_notice_state, "path, Path('/dev/shm/vqa-leases'), Path('/dev/shm/vqa-cu')",
             "path, Path('/dev/shm/vqa-cu')", 'test_notice_snapshot'),
            (_notice_state, "path, Path('/dev/shm/vqa-leases'), Path('/dev/shm/vqa-cu')",
             "path, Path('/dev/shm/vqa-leases')", 'test_notice_snapshot'),
            (_notice_state, 'info.st_mtime_ns, listing', 'None, listing', 'test_notice_snapshot'),
            (_notice_state, 'info.st_mtime_ns, listing', 'info.st_mtime_ns, None', 'test_notice_snapshot'),
            (_notice_guard, "case.assertEqual(_notice_state(_REAL_NOTICE_PATH), before, 'real notice journal changed')",
             'pass', 'test_notice_guard'),
        )
        for function, old, new, name in rows:
            source = textwrap.dedent(inspect.getsource(function))
            self.assertIn(old, source)
            namespace = dict(globals())
            exec(compile(source.replace(old, new), '<notice-root-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(predicate=old, replacement=new), \
                 patch(__name__ + '.' + function.__name__, namespace[function.__name__]):
                TestWave29Profiles(name).run(result)
            self.assertTrue(result.failures, (old, result.errors))
            self.assertFalse(result.errors, (old, result.errors))

    # @anchor:wa.s4.mw_profiles.TestMWP8IControls.test_preflight_transport_guard_mutants
    def test_preflight_transport_guard_mutants(self):
        import textwrap
        rows = (
            (m, m._capacity_targets, 'ai_locate=ai_locate)',
             'ai_locate=True)', TestDefaultProvisioning, 'test_disabled_ai_locate'),
            (m, m._capacity_targets, "target.get('rung') == 'T2'",
             'False', TestDefaultProvisioning, 'test_workflow_ai_locate_configuration'),
            (None, _notice_state, "(path, Path('/dev/shm/vqa-leases'), Path('/dev/shm/vqa-cu'))",
             '(path,)', TestWave29Profiles, 'test_notice_snapshot'),
            (m, m._trusted_default_configuration, 'host.preflight(ai_locate=config.ai_locate)',
             'host.preflight(ai_locate=True)', TestDefaultProvisioning, 'test_disabled_ai_locate'),
            (m, m._Provisioning._locator_arguments, "'locate_transport': _DisabledLocateTransport()",
             "'locate_transport': None", TestDefaultProvisioning, 'test_disabled_ai_locate'),
            (m, m._DisabledLocateTransport.generate, "raise ProfileError(reason='AI locate disabled')",
             'return None', TestDefaultProvisioning, 'test_disabled_ai_locate'),
            (None, _notice_guard, "case.assertEqual(_notice_state(_REAL_NOTICE_PATH), before, 'real notice journal changed')",
             'pass', TestWave29Profiles, 'test_notice_guard'),
        )
        for module, function, old, new, case, name in rows:
            source = textwrap.dedent(inspect.getsource(function))
            self.assertIn(old, source)
            namespace = dict(globals() if module is None else vars(module))
            exec(compile(source.replace(old, new), '<mwp8i-control-mutant>', 'exec'), namespace)
            owner = (m._Provisioning if function is m._Provisioning._locator_arguments else
                     m._DisabledLocateTransport if function is m._DisabledLocateTransport.generate else module)
            target = (__name__ + '.' + function.__name__) if owner is None else None
            replacement = namespace[function.__name__]
            mutant_patch = patch(target, replacement) if target else patch.object(owner, function.__name__, replacement)
            result = unittest.TestResult()
            with self.subTest(gate=function.__qualname__), mutant_patch:
                case(name).run(result)
            self.assertTrue(result.failures, (function.__qualname__, result.errors))
            self.assertFalse(result.errors, (function.__qualname__, result.errors))


# @anchor:wa.s4.mw_profiles.TestProfileScope
class TestProfileScope(unittest.TestCase):
    def _inventory(self):
        result = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=normal'],
                                capture_output=True, text=True, check=True,
                                cwd=Path(__file__).resolve().parents[1])
        allowed = {'docs/spec/MEMORYWEB_ENTRY.md', 'docs/spec/mw.v1.schema.json', 'docs/spec/MEMORYWEB_PROTOCOL.md', 'tools/memoryweb_gate.py', 'wender/learn/mw_profiles.py', 'wender/learn/mw_handlers.py', 'tests/test_mw_handlers_unit.py', 'tests/test_mw_profiles_unit.py'}
        self.assertTrue({line[3:] for line in result.stdout.splitlines()} <= allowed)

    # @anchor:wa.s4.mw_profiles.TestProfileScope.test_frozen_inventory
    def test_frozen_inventory(self):
        self._inventory()
        with patch.object(subprocess, 'run', return_value=types.SimpleNamespace(stdout=' M wender/scripts/types.py\n')):
            with self.assertRaises(AssertionError): self._inventory()

    # @anchor:wa.s4.mw_profiles.TestProfileScope.test_inventory_mutant
    def test_inventory_mutant(self):
        result = unittest.TestResult()
        with patch.object(TestProfileScope, '_inventory', lambda self: None):
            TestProfileScope('test_frozen_inventory').run(result)
        self.assertTrue(result.failures, result.errors)

    # @anchor:wa.s4.mw_profiles.TestProfileScope.test_cli_failure_schema_mutant
    def test_cli_failure_schema_mutant(self):
        source = inspect.getsource(cli.main)
        old = "print(gate.canon({'rc': exc.rc, 'verdict_code': exc.category}))"
        self.assertIn(old, source)
        namespace = dict(vars(cli))
        exec(compile(source.replace(old,
            "print(gate.canon({'rc': exc.rc, 'verdict_code': exc.category, 'reason': exc.reason}))"),
            '<cli-failure-schema-mutant>', 'exec'), namespace)
        result = unittest.TestResult()
        with patch.object(cli, 'main', namespace['main']):
            TestProfiles('test_host_installation_required').run(result)
        self.assertTrue(result.failures, result.errors)
        self.assertFalse(result.errors)
