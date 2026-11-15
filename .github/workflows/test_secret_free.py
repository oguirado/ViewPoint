"""Public CI inputs must remain pinned and fail closed on changed sources."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'
spec = importlib.util.spec_from_file_location('vendored_assembly', WORKFLOWS / 'vendored_assembly.py')
assembly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assembly)


class SecretFreeTests(unittest.TestCase):
    def test_no_private_access_or_checkout(self):
        for name in ('ci', 'package', 'volume', 'build-other', 'assembly', 'secrets'):
            source = (WORKFLOWS / (name + '.yml')).read_text()
            with self.subTest(workflow=name):
                self.assertNotRegex(source, r'\$\{\{\s*secrets\.')
                self.assertNotIn('KIT_REPO_TOKEN', source)
                self.assertNotIn('ASSEMBLY_READ_TOKEN', source)
                self.assertNotIn('_deps/', source)
                self.assertNotIn('token_check', source)
                self.assertNotIn('head.repo.fork', source)
        ci = (WORKFLOWS / 'ci.yml').read_text()
        self.assertEqual(ci.count('if ! python3 -B .github/workflows/vendored_assembly.py; then'), 3)
        self.assertEqual(ci.count('MV_MW_WENDER_ROOT: ${{ github.workspace }}/components/wender-agent'), 3)
        self.assertEqual(ci.count('VENDORED_INPUTS_INVALID:'), 3)
        self.assertIn("'schema': 'gate_receipt/v1'", ci)
        for name in ('ci', 'assembly'):
            self.assertIn('PYTHONDONTWRITEBYTECODE: "1"', (WORKFLOWS / (name + '.yml')).read_text())
        smoke = (WORKFLOWS / 'assembly.yml').read_text()
        self.assertIn('python3 -m unittest tests.test_assembly_smoke -v', smoke)
        self.assertIn('vendored_assembly.py --smoke --timeout 60', smoke)

    def test_each_kit_checkout_uses_validated_lock_without_token(self):
        count = 0
        for name in ('ci', 'package', 'volume', 'build-other'):
            text = (WORKFLOWS / (name + '.yml')).read_text()
            jobs = re.split(r'(?m)^  [\w-]+:\n', text.split('jobs:\n', 1)[1])[1:]
            for job in jobs:
                if 'path: kit-checkout' not in job:
                    continue
                with self.subTest(workflow=name, job=count):
                    count += 1
                    self.assertEqual(job.count('id: kit\n'), 1)
                    self.assertIn("lock = json.load(open('ui/kit.lock.json'))", job)
                    self.assertIn("assert re.fullmatch(r'[0-9a-f]{40}', lock['commit'])", job)
                    self.assertIn('repository: ${{ steps.kit.outputs.repository }}', job)
                    self.assertIn('ref: ${{ steps.kit.outputs.commit }}', job)
                    checkout = job.split('          path: kit-checkout', 1)[0].rsplit('      - ', 1)[1]
                    self.assertNotIn('token:', checkout)
                    self.assertIn('persist-credentials: false', job)
        self.assertEqual(count, 7)

    def test_kit_lock_reader_rejects_invalid_identity(self):
        source = (WORKFLOWS / 'package.yml').read_text()
        block = source.split('      - name: Read locked kit identity\n', 1)[1].split('      - ', 1)[0]
        code = '\n'.join(line[10:] for line in block.split('          python3 - <<\'PYEOF\'\n', 1)[1].split('          PYEOF', 1)[0].splitlines())
        lock = json.loads((ROOT / 'ui/kit.lock.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'ui').mkdir()
            output = root / 'output'
            for patch in ({}, {'commit': 'main'}, {'commit': ''},
                          {'repo': 'https://example.invalid/kit.git'}):
                with self.subTest(patch=patch):
                    (root / 'ui/kit.lock.json').write_text(json.dumps(dict(lock, **patch)))
                    output.unlink(missing_ok=True)
                    proc = subprocess.run(['python3', '-c', code], cwd=root,
                                          env=dict(os.environ, GITHUB_OUTPUT=str(output)),
                                          capture_output=True, text=True, timeout=10)
                    if patch:
                        self.assertNotEqual(proc.returncode, 0)
                        self.assertFalse(output.exists())
                    else:
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertEqual(output.read_text(), 'repository=oguirado/capsule-ui-kit\ncommit=' + lock['commit'] + '\n')

    def test_vendored_precondition_preserves_blocked_receipts(self):
        source = (WORKFLOWS / 'ci.yml').read_text()
        for job, gate in (('laws-contracts', 'unittest-root'), ('python', 'python-agents'),
                          ('e2e', 'e2e-wender')):
            block = source.split('  ' + job + ':\n', 1)[1]
            block = block.split('      - name: Verify vendored assembly inputs\n', 1)[1]
            code = block.split('        run: |\n', 1)[1].split('      - name:', 1)[0]
            code = '\n'.join(line[10:] for line in code.splitlines() if line.startswith('          '))
            # Only substitute the verifier invocation; execute the real receipt code.
            invocation = 'python3 -B .github/workflows/vendored_assembly.py'
            with self.subTest(job=job), tempfile.TemporaryDirectory() as tmp:
                env = dict(os.environ, RUNNER_TEMP=tmp, JOB_ID=job, PLATFORM='x64',
                           GITHUB_STEP_SUMMARY=str(Path(tmp) / 'summary'),
                           GITHUB_RUN_ID='fixture', GITHUB_SHA='fixture')
                proc = subprocess.run(['bash', '-c', code.replace(invocation, 'false')],
                                      cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                receipt_file = Path(tmp) / f'metaviewer-ci/{job}/x64/{gate}.receipt.json'
                receipt = json.loads(receipt_file.read_text())
                self.assertEqual(receipt['schema'], 'gate_receipt/v1')
                self.assertEqual(receipt['outcome'], 'BLOCKED')
                self.assertIsNone(receipt['rc'])
                self.assertIsNone(receipt['counts']['passed'])
                self.assertEqual(receipt['counts']['source'], 'unexecuted')
                self.assertIn('VENDORED_INPUTS_INVALID:', receipt['reason'])
                receipt_file.unlink()
                proc = subprocess.run(['bash', '-c', code], cwd=ROOT, env=env,
                                      capture_output=True, text=True, timeout=10)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(receipt_file.exists())

    def fixture(self, root):
        lock = json.loads((ROOT / 'deps.lock').read_text())
        for entry in lock['components']:
            path = root / entry['vendored_path']
            path.mkdir(parents=True)
            (path / 'source.txt').write_text(entry['name'] + '\n')
            entry['vendored_tree_sha256'] = assembly.deps_lock._vendored_tree_sha256(path)
        (root / 'deps.lock').write_text(json.dumps(lock))

    def test_vendored_inputs_and_mutations(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            self.fixture(root)
            pins, paths = assembly.verify(root)
            self.assertEqual(set(pins), set(assembly.assembly_smoke.REQUIRED_COMPONENTS))
            self.assertEqual(set(paths), set(pins))
            for name, path in paths.items():
                with self.subTest(component=name):
                    source = path / 'source.txt'
                    original = source.read_bytes()
                    source.write_bytes(b'tampered\n')
                    with self.assertRaises(assembly.deps_lock.DependencyLockError):
                        assembly.verify(root)
                    source.write_bytes(original)
                    for relative in ('__pycache__/cache', 'cache.pyc'):
                        extra = path / relative
                        extra.parent.mkdir(parents=True, exist_ok=True)
                        extra.write_bytes(b'extra')
                        with self.assertRaises(assembly.deps_lock.DependencyLockError):
                            assembly.verify(root)
                        extra.unlink()
                        if relative.startswith('__pycache__/'):
                            extra.parent.rmdir()
                    source.unlink()
                    with self.assertRaises(assembly.deps_lock.DependencyLockError):
                        assembly.verify(root)
                    source.write_bytes(original)
            for variable, value in (('MV_WENDER_ROOT', ''), ('VQA_VE_ROOT', '/tmp'),
                                    ('MV_WENDER_EXECUTABLE', '/tmp/override')):
                with self.subTest(variable=variable), mock.patch.dict(os.environ, {variable: value}):
                    with self.assertRaises(ValueError):
                        assembly.verify(root)

    def test_smoke_keeps_planner_and_component_coverage(self):
        smoke = assembly.assembly_smoke
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            self.fixture(root)
            (root / 'agents').mkdir()
            (root / 'daemon-adapter').mkdir()
            (root / 'contracts').mkdir()
            pins, paths = assembly.verify(root)
            with mock.patch.object(assembly, 'ROOT', root):
                self.check_smokes(pins, paths)

    def check_smokes(self, pins, paths):
        smoke = assembly.assembly_smoke
        with mock.patch.object(assembly, 'verify', wraps=assembly.verify) as verify, \
             mock.patch.object(smoke, 'smoke_vision_engine') as vision, \
             mock.patch.object(smoke, 'smoke_wender_agent') as wender, \
             mock.patch.object(smoke, 'smoke_opensuperficies') as surfaces, \
             mock.patch.object(smoke, 'aggregate_results', return_value=(True, [])) as aggregate:
            self.assertEqual(assembly.smoke(pins, paths, 60), 0)
            copied = verify.call_args_list[0].args[0]
            vision.assert_called_once_with(copied / 'components/vision-engine', pins['vision-engine'], timeout_s=60)
            wender.assert_called_once_with(copied / 'components/wender-agent', pins['wender-agent'], timeout_s=60, require_planner=True)
            surfaces.assert_called_once()
            self.assertEqual(surfaces.call_args.kwargs['timeout_s'], 60)
            self.assertEqual(aggregate.call_args.args[0], [vision.return_value, wender.return_value, surfaces.return_value])
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(verify.call_args.args[0], assembly.ROOT)
            self.assertFalse(copied.exists(), 'temporary probe sources must be removed')
            aggregate.return_value = (False, [])
            self.assertEqual(assembly.smoke(pins, paths, 60), 1)


if __name__ == '__main__':
    unittest.main()
