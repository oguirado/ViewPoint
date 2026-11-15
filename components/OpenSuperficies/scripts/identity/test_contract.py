#!/usr/bin/env python3
"""Offline configuration and approval negatives; no browser, Docker or X server."""
import ast
import copy
import hashlib
import importlib.util
import io
import os
import socket
import threading
import json
from pathlib import Path
import re
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'docker/identity/entrypoint.sh').read_text().split("<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
module = ast.parse(source)
# Evaluate configuration code alone: no imports, main call, X, browser or Docker.
functions = ast.Module(body=[node for node in module.body if
                            isinstance(node, (ast.ClassDef, ast.FunctionDef)) and
                            node.name in ('Refused', 'settings', 'require_view_mount')], type_ignores=[])
ns = {'os': os, 'json': json, 're': re, 'urlsplit': urlsplit}
exec(compile(functions, 'entrypoint-config', 'exec'), ns)
spec = importlib.util.spec_from_file_location('identity_build', ROOT / 'scripts/identity/print_build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class Contract(unittest.TestCase):
    def env(self):
        return {'SRF_WINDOWS': '1', 'SRF_TILES': json.dumps([
            {'id': 'window-1', 'x': 0, 'y': 0, 'w': 1280, 'h': 900}]),
            'SRF_EGRESS_PROXY': 'http://srf-egress-proxy:3128'}

    def test_valid(self):
        tiles, proxy = ns['settings'](self.env())
        self.assertEqual(tiles[0]['w'], 1280)
        self.assertEqual(proxy, 'http://srf-egress-proxy:3128')

    def test_config_refusals(self):
        for key, values in {
                'SRF_WINDOWS': ['0', '17', 'x', '2'],
                'SRF_TILES': ['null', '{}', '[]', '[{}]', 'bad'],
                'SRF_EGRESS_PROXY': ['', 'socks5://proxy:3128', 'http://user:pass@proxy:3128',
                                     'http://proxy:3128/private', 'http://proxy', 'http://proxy:99999']}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    env = self.env()
                    env[key] = value
                    with self.assertRaises(ns['Refused']) as caught:
                        ns['settings'](env)
                    self.assertEqual(caught.exception.code, 2)

    def test_geometry_refusals(self):
        for key, value in [('x', -1), ('w', 1281), ('h', 901), ('w', True), ('id', 'a b')]:
            env = self.env()
            tiles = json.loads(env['SRF_TILES'])
            tiles[0][key] = value
            env['SRF_TILES'] = json.dumps(tiles)
            with self.assertRaises(ns['Refused']):
                ns['settings'](env)
        env = self.env()
        tiles = json.loads(env['SRF_TILES'])
        tiles.append(dict(tiles[0], id='window-2'))
        env.update(SRF_WINDOWS='2', SRF_TILES=json.dumps(tiles))
        with self.assertRaises(ns['Refused']):
            ns['settings'](env)

    def test_xpra_mode(self):
        # Execute configuration assignments only, using shell builtins. No server.
        config = (ROOT / 'docker/identity/xpra-layer.sh').read_text().split('PIDFILE=', 1)[0]
        config += '\nprintf "%s %s %s" "$DISPLAY_NUM" "$XPRA_SOCKET" "$XPRA_READONLY"\n'
        for login, expected in [('', 'yes'), ('0', 'yes'), ('1', 'no'), ('yes', 'yes')]:
            result = subprocess.run(['/bin/bash', '-c', config],
                                    env={'SRF_LOGIN': login, 'XPRA_READONLY': 'no',
                                         'XPRA_SOCKET': '/unsafe/socket', 'DISPLAY_NUM': '1'},
                                    capture_output=True, text=True, timeout=2, check=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '100 /srfview/xpra.sock ' + expected)

    def test_view_mount_preflight(self):
        from unittest.mock import mock_open
        for mounted, directory, symlink, writable, accepted in [
                (True, True, False, True, True),
                (False, True, False, True, False),
                (True, False, False, True, False),
                (True, True, True, True, False),
                (True, True, False, False, False)]:
            mountinfo = '25 20 0:30 / /srfview rw - ext4 /dev/test rw\n' if mounted else ''
            with self.subTest(mounted=mounted, directory=directory, symlink=symlink, writable=writable), \
                    patch('builtins.open', mock_open(read_data=mountinfo)), \
                    patch.object(os.path, 'isdir', return_value=directory), \
                    patch.object(os.path, 'islink', return_value=symlink), \
                    patch.object(os, 'access', return_value=writable):
                if accepted:
                    ns['require_view_mount']()
                    self.check_wrapper_mount(True)
                else:
                    with self.assertRaises(ns['Refused']) as caught:
                        ns['require_view_mount']()
                    self.assertEqual(caught.exception.reason, 'view_mount_not_writable')
                    self.assertEqual(caught.exception.code, 4)
                    self.check_wrapper_mount(False)
        with patch('builtins.open', side_effect=OSError):
            with self.assertRaises(ns['Refused']):
                ns['require_view_mount']()

    def check_wrapper_mount(self, accepted):
        xpra = (ROOT / 'docker/identity/xpra-layer.sh').read_text()
        probe = xpra.split("<<'VIEW_MOUNT'\n", 1)[1].split('\nVIEW_MOUNT', 1)[0]
        with patch('sys.stderr', new=io.StringIO()):
            if accepted:
                exec(compile(probe, 'wrapper-mount', 'exec'), {})
            else:
                with self.assertRaises(SystemExit) as caught:
                    exec(compile(probe, 'wrapper-mount', 'exec'), {})
                self.assertEqual(caught.exception.code, 4)

    def test_xpra_http_probe(self):
        # Run the production probe against a fake local Unix endpoint only.
        xpra = (ROOT / 'docker/identity/xpra-layer.sh').read_text()
        probe = xpra.split("<<'VIEW_HTTP'\n", 1)[1].split('\nVIEW_HTTP', 1)[0]
        scratch = ROOT / 'build/room_IMAGE'
        scratch.mkdir(parents=True, exist_ok=True)
        for response, expected in [(b'HTTP/1.1 200 OK\r\n', 0),
                                   (b'HTTP/1.0 200 OK\r\n', 0),
                                   (b'HTTP/1.1 404 Missing\r\n', 3),
                                   (b'garbage 200\r\n', 3),
                                   (b'HTTP/1.1 200', 3), (b'', 3), (None, 3)]:
            with self.subTest(response=response), tempfile.TemporaryDirectory(dir=scratch) as temp:
                path = str(Path(temp) / 'x.sock')
                requests, failures = [], []
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(path)
                    server.listen(1)
                    server.settimeout(3)
                    def serve():
                        try:
                            conn, _ = server.accept()
                            with conn:
                                conn.settimeout(2)
                                request = bytearray()
                                while len(request) < 4096 and not request.endswith(b'\r\n\r\n'):
                                    part = conn.recv(1)
                                    if not part:
                                        break
                                    request.extend(part)
                                requests.append(bytes(request))
                                if response is None:
                                    conn.recv(1)  # Probe deadline must close this idle socket.
                                    return
                                # Deliberately fragment the status line.
                                for byte in response:
                                    conn.sendall(bytes([byte]))
                        except OSError as exc:
                            failures.append(str(exc))
                    thread = threading.Thread(target=serve, daemon=True)
                    thread.start()
                    result = subprocess.run(['python3', '-I', '-', path], input=probe,
                                            text=True, capture_output=True, timeout=3)
                    thread.join(timeout=4)
                    self.assertFalse(thread.is_alive())
                    self.assertFalse(failures)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertEqual(requests, [b'GET / HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n'])
        missing = subprocess.run(['python3', '-I', '-', str(scratch / 'absent.sock')],
                                 input=probe, text=True, capture_output=True, timeout=3)
        self.assertEqual(missing.returncode, 3)

    def test_duplicate_approval(self):
        with self.assertRaises(build.Refused):
            build.parse('{"image":"a","image":"b"}')

    def test_approval_hash_and_unknown_fields(self):
        sha = 'a' * 64  # Synthetic fixture, never a production approval.
        value = {'schema': 'os.cu.builder-approval/v1', 'mode': 'per_build',
                 'image': build.IMAGE, 'arch': 'aarch64', 'recipe_sha256': sha,
                 'approval_id': 'b2-0000', 'operator_note': 'offline fixture'}
        build.approved(value, sha)
        for key, replacement in [('recipe_sha256', 'b' * 64), ('image', 'os-worker-base'),
                                 ('mode', 'permanent'), ('extra', 'ignored')]:
            bad = dict(value)
            bad[key] = replacement
            with self.assertRaises(build.Refused):
                build.approved(bad, sha)

    def test_absent_approval_rc3(self):
        with patch.object(build.os, 'open', side_effect=FileNotFoundError), \
                patch('sys.stderr', new=io.StringIO()) as output:
            self.assertEqual(build.main(), 3)
            self.assertIn('operator_approval_unavailable', output.getvalue())

    def test_recipe_and_tamper(self):
        recipe = json.loads((ROOT / 'docker/identity/recipe.json').read_text())
        argv = build.command(ROOT, recipe)
        self.assertEqual(argv[:2], ['docker', 'build'])
        self.assertIn('--pull=false', argv)
        self.assertIn('--network=none', argv)
        for key, bad in [('base_image_id', 'sha256:' + 'a' * 64),
                         ('chromium_debian_version', '155')]:
            fixture = copy.deepcopy(recipe)
            fixture[key] = bad
            with self.assertRaises(build.Refused):
                build.command(ROOT, fixture)
        recipe['files']['docker/identity/Dockerfile'] = 'b' * 64
        with self.assertRaises(build.Refused):
            build.command(ROOT, recipe)

    def test_local_base_inspection(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=build.BASE_ID + '\n'))
        build.verify_base(runner)
        self.assertEqual(runner.call_args.args[0],
                         ['docker', 'image', 'inspect', '--format', '{{.Id}}', build.BASE])
        for rc, output in [(0, 'sha256:' + 'b' * 64), (3, build.BASE_ID), (0, '')]:
            runner.return_value = SimpleNamespace(returncode=rc, stdout=output)
            with self.assertRaises(build.Refused):
                build.verify_base(runner)

    def test_print_requires_matching_base(self):
        raw = (ROOT / 'docker/identity/recipe.json').read_bytes()
        approval = {'schema': 'os.cu.builder-approval/v1', 'mode': 'per_build',
                    'image': build.IMAGE, 'arch': 'aarch64',
                    'recipe_sha256': hashlib.sha256(raw).hexdigest(),
                    'approval_id': 'b2-0000', 'operator_note': 'offline fixture'}
        with patch.object(build, 'read_approval', return_value=approval), \
                patch.object(build, 'verify_base', side_effect=build.Refused('local_base_image_id_mismatch')), \
                patch('sys.stdout', new=io.StringIO()) as output, \
                patch('sys.stderr', new=io.StringIO()):
            self.assertEqual(build.main(), 3)
            self.assertEqual(output.getvalue(), '')


if __name__ == '__main__':
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Contract))
    sys.exit(0 if result.wasSuccessful() else 5)
