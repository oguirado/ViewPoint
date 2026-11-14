#!/usr/bin/env python3
"""Offline program image contracts; never starts Docker or a display."""
import ast
import contextlib
import io
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
SCRATCH = ROOT / 'build/room_IMG'
SCRATCH.mkdir(parents=True, exist_ok=True)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entrypoint_definitions():
    source = (ROOT / 'docker/program/entrypoint.sh').read_text().split(
        "<<'PYTHON'\n", 1)[1].rsplit('\nPYTHON', 1)[0]
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if not isinstance(node, ast.Expr)]
    scope = {}
    exec(compile(tree, '<entrypoint-definitions>', 'exec'), scope)
    return tree, scope


class Contract(unittest.TestCase):
    def test_openbox_border(self):
        tree, _ = entrypoint_definitions()
        configs = [node.args[0].value for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == 'write_text' and isinstance(node.args[0], ast.Constant)]
        self.assertEqual(len(configs), 1)
        from xml.etree import ElementTree
        config = ElementTree.fromstring(configs[0])
        ns = {'ob': 'http://openbox.org/3.4/rc'}
        self.assertEqual(config.findtext('ob:theme/ob:keepBorder', namespaces=ns), 'no')
        self.assertLess([child.tag for child in config].index('{%s}theme' % ns['ob']),
                        [child.tag for child in config].index('{%s}applications' % ns['ob']))

    def test_await_value_budget(self):
        tree, scope = entrypoint_definitions()
        wait = scope['await_value']
        with mock.patch.object(scope['time'], 'sleep') as sleep:
            for budget in (3, 100, 300):
                probe = mock.Mock(return_value=False)
                sleep.reset_mock()
                with self.assertRaises(scope['Refused']) as caught:
                    wait(probe, budget)
                self.assertEqual(caught.exception.code, 4)
                self.assertEqual(caught.exception.reason, 'program_readiness_timeout')
                self.assertEqual(probe.call_count, budget)
                self.assertEqual(sleep.call_args_list, [mock.call(0.1)] * budget)
            probe = mock.Mock(side_effect=[False] * 299 + ['ready'])
            sleep.reset_mock()
            self.assertEqual(wait(probe, 300), 'ready')
            self.assertEqual(probe.call_count, 300)
            self.assertEqual(sleep.call_count, 299)
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        waits = sorted((node for node in ast.walk(main) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == 'await_value'),
                       key=lambda node: node.lineno)
        self.assertEqual([node.args[1].value for node in waits], [100, 100, 300])
        selection = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                         and node.name == 'await_program_window')
        budget = [node.args[1].value for node in ast.walk(selection) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name) and node.func.id == 'await_value']
        self.assertEqual(budget, [300])

    def test_recipe(self):
        builder = load('builder', 'scripts/program/print_build.py')
        recipe = json.loads((ROOT / 'docker/program/recipe.json').read_text())
        argv = builder.command(ROOT, recipe)
        self.assertEqual(argv, ['docker', 'build', '--pull=false', '--platform', 'linux/arm64',
                               '--network=none', '-f', str(ROOT / 'docker/program/Dockerfile'),
                               '-t', builder.IMAGE, str(ROOT)])
        copied = set()
        for line in (ROOT / 'docker/program/Dockerfile').read_text().splitlines():
            if line.startswith('COPY '):
                copied.add(line.split()[-2])
        self.assertEqual(copied, {'docker/identity/xpra-layer.sh', 'docker/program/entrypoint.sh',
                                 'docker/program/apps/srf-notepad.py'})
        self.assertTrue(copied <= recipe['files'].keys())
        for path, sha in recipe['files'].items():
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), sha, path)
        bad = dict(recipe, files=dict(recipe['files']))
        bad['files']['docker/program/entrypoint.sh'] = '0' * 64
        with self.assertRaises(builder.Refused):
            builder.command(ROOT, bad)

    def test_absolute_build_paths(self):
        builder = load('builder', 'scripts/program/print_build.py')
        recipe = json.loads((ROOT / 'docker/program/recipe.json').read_text())
        # Isolate command formatting from the pending recipe reseal.
        recipe['files'] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                           for name in recipe['files']}
        argv = builder.command(ROOT, recipe)
        self.assertEqual(argv[argv.index('-f') + 1], str(ROOT / 'docker/program/Dockerfile'))
        self.assertEqual(argv[-1], str(ROOT))

    def test_refusals_before_spawn(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            os.symlink(sys.executable, Path(tmp) / 'python3')
            env = {'PATH': tmp, 'SRF_PROGRAM': 'unknown'}
            for args in [['invalid'], []]:
                result = subprocess.run(['/bin/sh', str(ROOT / 'docker/program/entrypoint.sh')] + args,
                                        env=env, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(b'REFUSED:', result.stderr)
                self.assertNotIn(b'Traceback', result.stderr)

    def test_entrypoint_settings(self):
        # Load definitions only: no entrypoint, display or subprocess is executed.
        _, scope = entrypoint_definitions()
        tile = dict(id='w1', x=0, y=0, w=400, h=300)
        settings = scope['settings']
        env = dict(SRF_WINDOWS='1', SRF_TILES=json.dumps([tile]))
        self.assertEqual(settings(env), [tile])
        for changed in [dict(w=1281), dict(h=901), dict(x=-1), dict(w=True), dict(extra=1), dict(id='')]:
            bad = dict(env, SRF_TILES=json.dumps([dict(tile, **changed)]))
            with self.subTest(changed=changed), self.assertRaises(scope['Refused']):
                settings(bad)
        for tiles in [[tile, tile], [], [dict(tile, w=0)]]:
            with self.assertRaises(scope['Refused']):
                settings(dict(SRF_WINDOWS=str(len(tiles)), SRF_TILES=json.dumps(tiles)))

    def test_paths(self):
        app = load('notepad', 'docker/program/apps/srf-notepad.py')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            root = Path(tmp)
            for name in ['', '/tmp/x', '..', '../x', 'a/../b', 'a/b', '.', 'x\x00y']:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    app.check_name(name)
            (root / 'ok').write_text('hello')
            self.assertEqual(app.read_document('ok', root), 'hello')
            app.write_document('new', 'saved', root)
            self.assertEqual((root / 'new').read_text(), 'saved')
            (root / 'link').symlink_to(root / 'ok')
            (root / 'dir').mkdir()
            os.mkfifo(root / 'fifo')
            (root / 'large').write_bytes(b'x' * (app.MAX_BYTES + 1))
            for name in ['link', 'dir', 'fifo', 'large']:
                for operation in [lambda: app.read_document(name, root),
                                  lambda: app.write_document(name, 'x', root)]:
                    with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                        operation()
            with self.assertRaises(ValueError):
                app.write_document('ok', 'x' * (app.MAX_BYTES + 1), root)
            self.assertEqual((root / 'ok').read_text(), 'hello')
        tree = ast.parse((ROOT / 'docker/program/apps/srf-notepad.py').read_text())
        forbidden = {'subprocess', 'socket', 'urllib', 'requests', 'http'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertTrue(forbidden.isdisjoint(a.name.split('.')[0] for a in node.names))

    def test_approval(self):
        builder = load('builder', 'scripts/program/print_build.py')
        value = dict(schema='os.cu.builder-approval/v1', mode='per_build', image=builder.IMAGE,
                     arch='aarch64', recipe_sha256='wrong', approval_id='b2-0001', operator_note='test')
        with self.assertRaises(builder.Refused):
            builder.approved(value, 'expected')
        value['recipe_sha256'] = 'expected'
        builder.approved(value, 'expected')
        with self.assertRaises(builder.Refused):
            builder.verify_base(lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout='wrong'))
        builder.verify_base(lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=builder.BASE_ID))
        # Read a real approval file through the internal printer. Only host ownership
        # metadata and Docker image inspection are faked; no privileged fixture needed.
        value['recipe_sha256'] = hashlib.sha256((ROOT / 'docker/program/recipe.json').read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            approval = Path(tmp) / 'approval.json'
            approval.write_text(json.dumps(value))
            approval.chmod(0o644)
            fields = list(approval.stat())
            fields[4:6] = [0, 0]
            root_stat = os.stat_result(fields)
            calls = []

            def image_runner(argv, **kwargs):
                calls.append(argv)
                self.assertEqual(argv, ['docker', 'image', 'inspect', '--format', '{{.Id}}', builder.BASE])
                return subprocess.CompletedProcess(argv, 0, stdout=builder.BASE_ID)

            output = io.StringIO()
            with mock.patch.object(builder.os, 'fstat', return_value=root_stat), contextlib.redirect_stdout(output):
                self.assertEqual(builder._print_build(approval, runner=image_runner), 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(output.getvalue(),
                             'docker build --pull=false --platform linux/arm64 --network=none '
                             '-f ' + str(ROOT / 'docker/program/Dockerfile') + ' -t ' + builder.IMAGE + ' ' + str(ROOT) + '\n')
            # A mismatched real file must refuse before consulting Docker.
            value['recipe_sha256'] = 'wrong'
            approval.write_text(json.dumps(value))
            errors = io.StringIO()
            with mock.patch.object(builder.os, 'fstat', return_value=root_stat), contextlib.redirect_stderr(errors):
                self.assertEqual(builder._print_build(approval, runner=image_runner), 3)
            self.assertEqual(len(calls), 1)
            self.assertEqual(errors.getvalue(), 'REFUSED: approval_does_not_match_recipe\n')

    def test_production_ignores_approval_environment(self):
        builder = load('builder', 'scripts/program/print_build.py')
        # Intercept open to avoid reading any operator approval or running Docker.
        with mock.patch.dict(os.environ, SRF_PROGRAM_TEST_APPROVAL='/unused/test-approval.json'), \
                mock.patch.object(builder.os, 'open', side_effect=OSError) as opened, \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.main(), 3)
        self.assertEqual(opened.call_args.args[0], builder.APPROVAL)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Contract)
    sys.exit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 6)
