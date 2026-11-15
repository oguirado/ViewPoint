#!/usr/bin/env python3
"""Offline Electron capsule contracts. No Docker, display, app or network starts."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
SCRATCH = ROOT / 'build/room_IMG'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def definitions():
    source = (ROOT / 'docker/program/entrypoint.sh').read_text().split("<<'PYTHON'\n")[1].rsplit('\nPYTHON', 1)[0]
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if not isinstance(node, ast.Expr)]
    scope = {}
    exec(compile(tree, '<entrypoint>', 'exec'), scope)
    return scope


class Contract(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        (self.app / 'metaviewer').write_bytes(b'fake app; never executed\n')
        (self.app / 'metaviewer').chmod(0o755)
        (self.app / 'resources').mkdir()
        (self.app / 'resources/app.asar').write_bytes(b'fake archive')

    def test_entrypoint_metadata(self):
        scope = definitions()
        config = self.root / 'app.json'
        good = dict(exe='metaviewer', args=['--nested-depth=1'], wm_class='metaviewer')
        config.write_text(json.dumps(good))
        program = scope['load_electron_app'](config, self.app)
        self.assertEqual(program, dict(argv=[str(self.app / 'metaviewer'), '--nested-depth=1',
                         '--user-data-dir=/profile/app', '--ozone-platform=x11'], wm_class='metaviewer'))
        good['args'] += ['--user-data-dir=/profile/app', '--ozone-platform=x11']
        config.write_text(json.dumps(good))
        self.assertEqual(scope['load_electron_app'](config, self.app), program)
        for change in [dict(exe='../metaviewer'), dict(exe='/bin/sh'), dict(exe='resources'),
                       dict(exe='missing'), dict(args='x'), dict(args=[3]), dict(args=['x\n']),
                       dict(args=['x\x00']), dict(wm_class=''), dict(wm_class='bad class'),
                       dict(wm_class='a' * 65), dict(extra=True),
                       dict(args=['--user-data-dir=/tmp/other']), dict(args=['--ozone-platform=wayland']),
                       dict(args=['--no-sandbox']), dict(args=['--']), dict(exe='some..app')]:
            with self.subTest(change=change):
                config.write_text(json.dumps(dict(good, **change)))
                with self.assertRaises(scope['Refused']) as caught:
                    scope['load_electron_app'](config, self.app)
                self.assertEqual(caught.exception.code, 2)
        config.write_text('{"exe":"metaviewer","exe":"metaviewer","args":[],"wm_class":"ok"}')
        with self.assertRaises(scope['Refused']):
            scope['load_electron_app'](config, self.app)
        config.write_text(json.dumps(good))
        (self.app / 'metaviewer').chmod(0o644)
        with self.assertRaises(scope['Refused']):
            scope['load_electron_app'](config, self.app)

    def test_metadata_symlink_escape_and_types(self):
        scope = definitions()
        config = self.root / 'app.json'
        good = dict(exe='metaviewer', args=[], wm_class='metaviewer')
        outside = self.root / 'outside'
        outside.write_text('fake')
        outside.chmod(0o755)
        (self.app / 'escape').symlink_to(outside)
        for value in [[], None, {}, dict(good, exe='escape'), dict(good, args=['x\r']),
                      dict(good, args=['--user-data-dir']), dict(good, args=['--ozone-platform=x11'] * 2)]:
            config.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(scope['Refused']):
                scope['load_electron_app'](config, self.app)
        config.write_text('x' * 65537)
        with self.assertRaises(scope['Refused']):
            scope['load_electron_app'](config, self.app)
        config.unlink()
        with self.assertRaises(scope['Refused']):
            scope['load_electron_app'](config, self.app)

    def test_unsafe_arg_families_refused_before_spawn(self):
        flags = ['--no-sandbox', '--disable-setuid-sandbox', '--disable-seccomp-filter-sandbox',
                 '--disable-namespace-sandbox', '--disable-gpu-sandbox', '--single-process',
                 '--no-zygote', '--in-process-gpu', '--remote-debugging-port',
                 '--remote-debugging-pipe', '--inspect', '--inspect-brk',
                 '--disable-dev-shm-usage', '--js-flags', '--disable-web-security',
                 '--allow-file-access-from-files', '--custom-sandbox-option']
        scope = definitions()
        config = self.root / 'app.json'
        loader = scope['load_electron_app']
        for flag in flags:
            for arg in [flag, flag + '=value']:
                with self.subTest(arg=arg):
                    config.write_text(json.dumps(dict(exe='metaviewer', args=[arg], wm_class='metaviewer')))
                    with mock.patch.dict(os.environ, SRF_PROGRAM='metaviewer-nested', clear=True), \
                            mock.patch.object(scope['signal'], 'signal'), \
                            mock.patch.dict(scope, load_electron_app=lambda: loader(config, self.app)), \
                            mock.patch.object(scope['subprocess'], 'Popen', side_effect=AssertionError('spawned')), \
                            mock.patch.object(scope['subprocess'], 'run', side_effect=AssertionError('runtime called')), \
                            contextlib.redirect_stderr(io.StringIO()) as errors:
                        self.assertEqual(scope['main'](), 2)
                    self.assertEqual(errors.getvalue(), 'REFUSED: invalid_app_manifest\n')
        # Only the argument name is inspected; ordinary application values remain valid.
        config.write_text(json.dumps(dict(exe='metaviewer', args=['--title=sandbox'], wm_class='metaviewer')))
        self.assertIn('--title=sandbox', loader(config, self.app)['argv'])

    def test_program_child_environment(self):
        scope = definitions()
        allowed = dict(DISPLAY=':100', HOME='/profile', XDG_RUNTIME_DIR='/run/newton-tenant',
                       XDG_CONFIG_HOME='/profile/config', LANG='C.UTF-8', LC_ALL='C.UTF-8',
                       PATH='/usr/bin', NO_AT_BRIDGE='1', GTK_USE_PORTAL='0', GIO_USE_VFS='local')
        pollution = dict(ELECTRON_RUN_AS_NODE='1', ELECTRON_DISABLE_SANDBOX='1',
                         ELECTRON_EXTRA_LAUNCH_ARGS='--no-sandbox', NODE_OPTIONS='--inspect',
                         NODE_PATH='/untrusted', CHROME_LOG_FILE='/untrusted',
                         DBUS_SESSION_BUS_ADDRESS='unix:path=/untrusted', DBUS_SYSTEM_BUS_ADDRESS='x',
                         SRF_PROGRAM='metaviewer-nested', OTHER='untrusted')
        with mock.patch.dict(os.environ, dict(allowed, **pollution), clear=True), \
                mock.patch.object(scope['subprocess'], 'Popen') as popen:
            scope['spawn_program'](['fake-app'])
        self.assertEqual(popen.call_args.kwargs['env'],
                         dict(allowed, DBUS_SESSION_BUS_ADDRESS='disabled:'))
        self.assertEqual(popen.call_args.args, (['fake-app'],))

    def test_electron_window_types_and_top_level(self):
        scope = definitions()
        normal = 'WM_STATE(WM_STATE):\n\twindow state: Normal\n'
        props = {
            '10': normal + '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_SPLASH',
            '11': normal + '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DIALOG',
            '12': normal + '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_UTILITY',
            '13': normal + '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_TOOLTIP',
            '14': normal + '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NOTIFICATION',
            '15': normal + '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL',
            '16': normal + '_NET_WM_WINDOW_TYPE:  not found.',
            '17': 'WM_STATE:  not found.\n_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL',
            '18': normal + '_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DOCK',
            '19': '',
        }
        def fake(argv, check=True):
            if argv[:2] == ['xdotool', 'search']:
                return ' '.join(props)
            if argv[0] == 'xprop':
                self.assertEqual(argv, ['xprop', '-id', argv[2], 'WM_STATE', '_NET_WM_WINDOW_TYPE'])
                return props[argv[2]]
            self.assertEqual(argv[:3], ['xdotool', 'getwindowgeometry', '--shell'])
            return 'X=0\nY=0\nWIDTH=1280\nHEIGHT=720\n'
        with mock.patch.dict(os.environ, SRF_PROGRAM='metaviewer-nested'), \
                mock.patch.dict(scope, run=fake):
            scope['PROGRAMS']['metaviewer-nested'] = dict(wm_class='metaviewer')
            self.assertEqual(scope['windows'](), [('15', (0, 0, 1280, 720)), ('16', (0, 0, 1280, 720))])
        # Notepad's existing selection path must issue no xprop queries.
        with mock.patch.dict(os.environ, SRF_PROGRAM='notepad'), mock.patch.dict(scope, run=fake):
            self.assertEqual(len(scope['windows']()), len(props))

    def test_electron_waits_for_main_window_and_refuses_ambiguity(self):
        scope = definitions()
        with mock.patch.dict(scope, windows=mock.Mock(side_effect=[[], [('21', ())]])), \
                mock.patch.object(scope['time'], 'sleep'):
            self.assertEqual(scope['await_program_window'](set()), ['21'])
        with mock.patch.dict(scope, windows=lambda: [('20', ()), ('21', ())]):
            with self.assertRaises(scope['Refused']) as caught:
                scope['await_program_window'](set())
            self.assertEqual((caught.exception.code, caught.exception.reason),
                             (4, 'ambiguous_program_window'))
            self.assertEqual(scope['await_program_window']({'20'}), ['21'])
        probe = mock.Mock(return_value=[])
        with mock.patch.dict(scope, windows=probe), mock.patch.object(scope['time'], 'sleep'):
            with self.assertRaises(scope['Refused']) as caught:
                scope['await_program_window'](set())
            self.assertEqual(caught.exception.reason, 'program_readiness_timeout')
            self.assertEqual(probe.call_count, 300)

    def test_image_probe_program_environment_offline(self):
        source = (ROOT / 'docker/program/entrypoint.sh').read_text()
        image_tree = ast.parse((ROOT / 'scripts/program-electron/test_image.py').read_text())
        assignment = next(n for n in ast.walk(image_tree) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'probe' for t in n.targets))
        probe = assignment.value.left.value % ('{}', '{}')
        compile(probe, '<image-probe>', 'exec')
        with mock.patch.dict(os.environ, XDG_RUNTIME_DIR='/run/newton-tenant', clear=True), \
                mock.patch.object(os, 'getuid', return_value=10001), \
                mock.patch.object(Path, 'read_text', return_value=source), \
                mock.patch.object(subprocess, 'Popen') as popen:
            popen.return_value.wait.return_value = 0
            exec(probe.split('expected = json.loads', 1)[0], {})
            env = popen.call_args.kwargs['env']
            self.assertEqual(env['DBUS_SESSION_BUS_ADDRESS'], 'disabled:')
            self.assertFalse(any(k.startswith(('ELECTRON_', 'NODE_', 'CHROME_')) for k in env))
            self.assertNotIn('UNLISTED', env)
            compile(popen.call_args.args[0][-1], '<child-probe>', 'exec')

    def test_electron_splash_waits_for_normal(self):
        scope = definitions()
        searches = 0
        def fake(argv, check=True):
            nonlocal searches
            if argv[:2] == ['xdotool', 'search']:
                searches += 1
                return '10' if searches == 1 else '10 11'
            if argv[0] == 'xprop':
                kind = 'SPLASH' if argv[2] == '10' else 'NORMAL'
                return 'WM_STATE(WM_STATE):\n_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_' + kind
            return 'X=0\nY=0\nWIDTH=1280\nHEIGHT=720\n'
        with mock.patch.dict(os.environ, SRF_PROGRAM='metaviewer-nested'), \
                mock.patch.dict(scope, run=fake), mock.patch.object(scope['time'], 'sleep') as sleep:
            scope['PROGRAMS']['metaviewer-nested'] = dict(wm_class='metaviewer')
            self.assertEqual(scope['await_program_window'](set()), ['11'])
            self.assertEqual(searches, 2)
            sleep.assert_called_once_with(0.1)

    def test_sandbox_environment_refused(self):
        scope = definitions()
        with mock.patch.dict(os.environ, SRF_PROGRAM='metaviewer-nested', ELECTRON_DISABLE_SANDBOX='', clear=True), \
                mock.patch.object(scope['signal'], 'signal'), \
                mock.patch.object(scope['subprocess'], 'Popen', side_effect=AssertionError('spawned')), \
                mock.patch.object(scope['subprocess'], 'run', side_effect=AssertionError('runtime called')), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(scope['main'](), 2)
            self.assertEqual(errors.getvalue(), 'REFUSED: electron_sandbox_required\n')

    def test_invalid_boot_before_spawn(self):
        scope = definitions()
        with mock.patch.dict(os.environ, SRF_PROGRAM='metaviewer-nested', clear=True), \
                mock.patch.dict(scope, load_electron_app=mock.Mock(side_effect=scope['Refused'](2, 'invalid_app_manifest'))), \
                mock.patch.object(scope['signal'], 'signal'), \
                mock.patch.object(scope['subprocess'], 'Popen', side_effect=AssertionError('spawned')), \
                mock.patch.object(scope['subprocess'], 'run', side_effect=AssertionError('runtime called')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scope['main'](), 2)
            scope['load_electron_app'].assert_called_once_with()

    def test_notepad_argv(self):
        scope = definitions()
        self.assertEqual(scope['PROGRAMS']['notepad'], dict(argv=['python3', '-I', '/usr/local/libexec/srf-notepad.py'], wm_class='srf-notepad'))
        self.assertEqual(scope['program_argv']('notepad', scope['PROGRAMS']['notepad'], 'w1'),
                         ['python3', '-I', '/usr/local/libexec/srf-notepad.py', '--tile', 'w1'])
        self.assertEqual(scope['program_argv']('metaviewer-nested', dict(argv=['app']), 'w1'), ['app'])

    def test_pin(self):
        pin = load('pin', 'scripts/program-electron/pin_app.py')
        (self.app / 'alias').symlink_to('metaviewer')
        (self.app / 'chrome-sandbox').write_bytes(b'fake sandbox')
        (self.app / 'chrome-sandbox').chmod(0o4755)
        (self.app / 'resources.pak').write_bytes(b'pak')
        manifest = pin.pin(self.app, self.root)
        stage = self.root / 'build/electron_app'
        self.assertFalse((stage / 'alias').is_symlink())
        self.assertEqual((stage / 'alias').read_bytes(), (stage / 'metaviewer').read_bytes())
        self.assertEqual(stat.S_IMODE((stage / 'chrome-sandbox').stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((stage / 'resources/app.asar').stat().st_mode), 0o444)
        self.assertEqual(manifest, (self.root / 'docker/program-electron/app.manifest').read_text())
        names = [line.split('  ', 1)[1] for line in manifest.splitlines()]
        self.assertEqual(names, sorted(names))
        for line in manifest.splitlines():
            sha, name = line.split('  ', 1)
            self.assertEqual(sha, hashlib.sha256((stage / name).read_bytes()).hexdigest())
        with self.assertRaises(pin.Refused):
            pin.pin(self.app, self.root)

    def test_pin_refusals(self):
        pin = load('pin', 'scripts/program-electron/pin_app.py')
        for kind in ['escape', 'fifo', 'setuid', 'cycle', 'newline']:
            with self.subTest(kind=kind):
                bad = self.app / 'bad'
                if kind == 'escape':
                    bad.symlink_to('/etc/passwd')
                elif kind == 'fifo':
                    os.mkfifo(bad)
                elif kind == 'cycle':
                    bad.symlink_to('.')
                elif kind == 'newline':
                    bad = self.app / 'bad\nname'
                    bad.write_text('x')
                else:
                    bad.write_text('x')
                    bad.chmod(0o4755)
                with self.assertRaises(pin.Refused):
                    pin.pin(self.app, self.root)
                self.assertFalse((self.root / 'build/electron_app').exists())
                bad.unlink()

    def fixture(self):
        pin = load('pin', 'scripts/program-electron/pin_app.py')
        pin.pin(self.app, self.root)
        recipe = json.loads((ROOT / 'docker/program-electron/recipe.json').read_text())
        for name in recipe['files']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
        recipe['files'] = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in recipe['files']}
        recipe['app_manifest_sha256'] = hashlib.sha256((self.root / 'docker/program-electron/app.manifest').read_bytes()).hexdigest()
        return recipe

    def test_builder(self):
        builder = load('builder', 'scripts/program-electron/print_build.py')
        recipe = self.fixture()
        argv = builder.command(self.root, recipe)
        self.assertEqual(argv, ['docker', 'build', '--pull=false', '--platform', 'linux/arm64', '--network=none',
                         '-f', str(self.root / 'docker/program-electron/Dockerfile'), '-t', builder.IMAGE, str(self.root)])
        stage = self.root / 'build/electron_app'
        (stage / 'resources/app.asar').chmod(0o644)
        (stage / 'resources/app.asar').write_bytes(b'tampered')
        with self.assertRaises(builder.Refused):
            builder.command(self.root, recipe)

    def test_manifest_and_recipe_refusals(self):
        builder = load('builder', 'scripts/program-electron/print_build.py')
        recipe = self.fixture()
        for change in [dict(base_image_id='wrong'), dict(image='wrong'), dict(schema='wrong'),
                       dict(package_pins={}), dict(app_manifest_sha256='wrong'), dict(files={})]:
            with self.subTest(change=change), self.assertRaises(builder.Refused):
                builder.command(self.root, dict(recipe, **change))
        stage = self.root / 'build/electron_app'
        stage.chmod(0o755)
        extra = stage / 'extra'
        extra.write_bytes(b'extra')
        extra.chmod(0o444)
        with self.assertRaises(builder.Refused):
            builder.command(self.root, recipe)
        extra.unlink()
        extra.symlink_to('metaviewer')
        with self.assertRaises(builder.Refused):
            builder.command(self.root, recipe)
        extra.unlink()
        (stage / 'metaviewer').unlink()
        with self.assertRaises(builder.Refused):
            builder.command(self.root, recipe)
        manifest = self.root / 'docker/program-electron/app.manifest'
        for text in ['0' * 64 + '  ../escape\n', '0' * 64 + '  /escape\n',
                     '0' * 64 + '  same\n' + '0' * 64 + '  same\n',
                     '0' * 64 + '  z\n' + '0' * 64 + '  a\n', 'bad\n']:
            manifest.write_text(text)
            changed = dict(recipe, app_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
            with self.subTest(text=text), self.assertRaises(builder.Refused):
                builder.command(self.root, changed)

    def test_dockerfile_contract(self):
        gtk = (ROOT / 'docker/program/Dockerfile').read_text()
        electron = (ROOT / 'docker/program-electron/Dockerfile').read_text()
        self.assertEqual(electron.split('COPY ', 1)[0], gtk.split('COPY ', 1)[0])
        self.assertEqual(electron.split('ENV ', 1)[1], gtk.split('ENV ', 1)[1])
        for line in gtk.splitlines():
            if line.startswith('COPY ') and 'srf-notepad' not in line:
                self.assertIn(line, electron.splitlines())
        copies = {line.split()[-2] for line in electron.splitlines() if line.startswith('COPY ')}
        self.assertEqual(copies, {'docker/identity/xpra-layer.sh', 'docker/program/entrypoint.sh',
                                 'build/electron_app/', 'docker/program-electron/app.json'})
        self.assertNotIn('ELECTRON_DISABLE_SANDBOX', electron)
        self.assertNotIn('apt-get', electron)
        self.assertIn('chmod 0555', electron)
        self.assertIn('chmod 0444', electron)
        self.assertLess(electron.index('/usr/local/share/srf-app/app.json'),
                        electron.index('chmod 0555 /usr/local/share/srf-app'))

    def test_printer_only_emits_approved_command(self):
        builder = load('builder', 'scripts/program-electron/print_build.py')
        recipe = self.fixture()
        recipe_path = self.root / 'docker/program-electron/recipe.json'
        recipe_path.write_text(json.dumps(recipe))
        value = dict(schema='os.cu.builder-approval/v1', mode='per_build', image=builder.IMAGE,
                     arch='aarch64', recipe_sha256=hashlib.sha256(recipe_path.read_bytes()).hexdigest(),
                     approval_id='b2-0001', operator_note='offline fixture')
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(argv, ['docker', 'image', 'inspect', '--format', '{{.Id}}', builder.BASE])
            return subprocess.CompletedProcess(argv, 0, stdout=builder.BASE_ID)

        with mock.patch.object(builder, '__file__', str(self.root / 'scripts/program-electron/print_build.py')), \
                mock.patch.object(builder, 'read_approval', return_value=value) as approval, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(builder._print_build('/unused/offline-approval', runner=runner), 0)
            approval.assert_called_once_with('/unused/offline-approval')
        import shlex
        self.assertEqual(output.getvalue(), shlex.join(builder.command(self.root, recipe)) + '\n')
        self.assertEqual(len(calls), 1)
        value['recipe_sha256'] = 'wrong'
        with mock.patch.object(builder, '__file__', str(self.root / 'scripts/program-electron/print_build.py')), \
                mock.patch.object(builder, 'read_approval', return_value=value), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder._print_build('/unused/offline-approval', runner=runner), 3)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(len(calls), 1)

    def test_recipe_and_empty_placeholder(self):
        builder = load('builder', 'scripts/program-electron/print_build.py')
        recipe = json.loads((ROOT / 'docker/program-electron/recipe.json').read_text())
        for name, sha in recipe['files'].items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), sha, name)
        self.assertEqual(hashlib.sha256((ROOT / 'docker/program-electron/app.manifest').read_bytes()).hexdigest(), recipe['app_manifest_sha256'])
        # The empty placeholder never builds; a real pin builds only from its own staged tree.
        if not (ROOT / 'docker/program-electron/app.manifest').read_bytes():
            with self.assertRaises(builder.Refused):
                builder.command(ROOT, recipe)
        elif not (ROOT / 'build/electron_app').is_dir():
            with self.assertRaisesRegex(builder.Refused, 'staged_app_missing'):
                builder.command(ROOT, recipe)
        else:
            argv = builder.command(ROOT, recipe)
            self.assertEqual(argv[:6], ['docker', 'build', '--pull=false', '--platform', 'linux/arm64', '--network=none'])
            self.assertEqual(argv[argv.index('-t') + 1], builder.IMAGE)
            self.assertEqual(argv[-1], str(ROOT))

    def test_approval_and_inspection_fakes(self):
        builder = load('builder', 'scripts/program-electron/print_build.py')
        value = dict(schema='os.cu.builder-approval/v1', mode='per_build', image=builder.IMAGE, arch='aarch64',
                     recipe_sha256='expected', approval_id='b2-0001', operator_note='test')
        builder.approved(value, 'expected')
        for change in [dict(recipe_sha256='wrong'), dict(image='other'), dict(mode='persistent')]:
            with self.assertRaises(builder.Refused):
                builder.approved(dict(value, **change), 'expected')
        builder.verify_base(lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=builder.BASE_ID))
        with self.assertRaises(builder.Refused):
            builder.verify_base(lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout='wrong'))
        with mock.patch.object(builder, 'read_approval', side_effect=builder.Refused('operator_approval_unavailable')), \
                mock.patch.object(builder.subprocess, 'run', side_effect=AssertionError('Docker called')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder.main(), 3)


if __name__ == '__main__':
    sys.exit(0 if unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Contract)).wasSuccessful() else 6)
