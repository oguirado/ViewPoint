"""[TARGET, unit decision] Hosted owner tests require pinned vendored engines."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.ci.test_ci_counts import _load_workflow_doc
from tools.assembly_smoke import parse_deps_lock

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / '.github/workflows/ci.yml'
ASSEMBLY = ROOT / '.github/workflows/assembly.yml'
VENDORED_PATHS = {
    'vision-engine': 'components/vision-engine',
    'wender-agent': 'components/wender-agent',
    'opensuperficies': 'components/OpenSuperficies',
}
CONSUMERS = {
    'laws-contracts': ('Run laws and contracts gates', 'unittest-root'),
    'python': ('Run Python agent gates', 'python-agents'),
    'e2e': ('Run E2E gate under xvfb', 'e2e-wender'),
}
VERIFIER_NAME = 'Verify vendored assembly inputs'
VERIFIER_COMMAND = 'python3 -B .github/workflows/vendored_assembly.py'


def steps(text, job='python'):
    return _load_workflow_doc(text)['jobs'][job]['steps']


def validate_wender(text):
    for forbidden in ('ASSEMBLY_READ_TOKEN', 'KIT_REPO_TOKEN', 'MV_WENDER_ROOT',
                      '_deps/wender-agent'):
        assert forbidden not in text, 'private checkout or legacy planner root restored'
    for job, (suite_name, gate) in CONSUMERS.items():
        items = steps(text, job)
        verifiers = [s for s in items if s.get('name') == VERIFIER_NAME]
        suites = [s for s in items if s.get('name') == suite_name]
        pins_steps = [s for s in items if s.get('id') == 'pins']
        assert len(verifiers) == len(suites) == len(pins_steps) == 1, 'missing or duplicate owner preparation'
        verify, suite, pins = verifiers[0], suites[0], pins_steps[0]
        assert items.index(verify) < items.index(pins) < items.index(suite), 'verify inputs before consumers'
        assert all('if' not in s and not s.get('continue-on-error')
                   for s in (verify, pins, suite)), 'owner preparation must be unconditional and fail closed'
        assert verify['env']['JOB_ID'] == job
        assert verify['env']['PLATFORM'] == ('x64' if job == 'laws-contracts' else '${{ matrix.platform }}')
        failure_branch = (
            r'^if ! ' + re.escape(VERIFIER_COMMAND) + r'; then\n'
            r'  blocked ' + re.escape(gate)
            + r' "VENDORED_INPUTS_INVALID: deps.lock content verification failed" [^\n]+\n'
            r'  exit 2\nfi$'
        )
        assert re.search(failure_branch, verify['run'], re.M), 'vendored failure must block the owner gate'
        assert '--lock deps.lock' in pins['run'] and '--print-pins' in pins['run']
        assert '--github-output "${GITHUB_OUTPUT}"' in pins['run']
        assert suite['env'].get('MV_MW_WENDER_ROOT') == (
            '${{ github.workspace }}/components/wender-agent'
        ), 'owner tests must consume the verified vendored Wender root'


@contextlib.contextmanager
def vendored_snapshot():
    """Copy shipped inputs so negative controls never modify the locked trees."""
    with tempfile.TemporaryDirectory(prefix='wender Música ') as td:
        root = Path(td)
        shutil.copy2(ROOT / 'deps.lock', root / 'deps.lock')
        for relative in VENDORED_PATHS.values():
            shutil.copytree(ROOT / relative, root / relative, symlinks=True)
        yield root


class WenderAgentsWorkflowTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'wender_vendored_assembly', ROOT / '.github/workflows/vendored_assembly.py')
        self.verifier = importlib.util.module_from_spec(spec)
        # The standalone verifier sets its import path and bytecode policy.
        with mock.patch.object(sys, 'path', sys.path.copy()), mock.patch.object(sys, 'dont_write_bytecode', True):
            spec.loader.exec_module(self.verifier)

    def verify(self, root):
        # Keep developer component overrides out of these isolated input checks.
        with mock.patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()):
            for variable in ('MV_WENDER_ROOT', 'VQA_VE_ROOT',
                             'MV_WENDER_EXECUTABLE', 'MV_VISION_EXECUTABLE'):
                os.environ.pop(variable, None)
            return self.verifier.verify(root)

    def test_pinned_vendored_trees_with_mutations(self):
        text = CI.read_text()
        validate_wender(text)
        for old, new in (
            ('MV_MW_WENDER_ROOT: ${{ github.workspace }}/components/wender-agent',
             'MV_MW_WENDER_ROOT: ${{ github.workspace }}/_deps/wender-agent'),
            ('MV_MW_WENDER_ROOT:', 'MV_WENDER_ROOT:'),
            ('--lock deps.lock', '--lock other.lock'),
            (VERIFIER_COMMAND, 'true'),
            ('          exit 2\n          fi\n      - name: Parse deps.lock',
             '          exit 0\n          fi\n      - name: Parse deps.lock'),
            ('      - name: ' + VERIFIER_NAME + '\n',
             '      - name: ' + VERIFIER_NAME + '\n        if: false\n'),
            ('      - name: ' + VERIFIER_NAME + '\n',
             '      - name: ' + VERIFIER_NAME + '\n        continue-on-error: true\n'),
        ):
            with self.subTest(workflow_mutation=old):
                self.assertIn(old, text)
                with self.assertRaises(AssertionError):
                    validate_wender(text.replace(old, new))

        with vendored_snapshot() as root:
            lock = root / 'deps.lock'
            original = lock.read_text()
            pins, paths = self.verify(root)
            self.assertEqual(paths, {name: root / path for name, path in VENDORED_PATHS.items()})
            expected_pins = parse_deps_lock(ROOT / 'deps.lock')
            self.assertEqual({name: vars(pin) for name, pin in pins.items()},
                             {name: vars(pin) for name, pin in expected_pins.items()})
            for index, entry in enumerate(json.loads(original)['components']):
                self.assertEqual(entry['vendored_path'], VENDORED_PATHS[entry['name']])
                self.assertRegex(entry['vendored_tree_sha256'], r'^[0-9a-f]{64}\Z')
                for field, value in (
                    ('vendored_path', 'components/other'),
                    ('vendored_path', None),
                    ('vendored_tree_sha256', '0' * 64),
                    ('vendored_tree_sha256', 'invalid'),
                    ('vendored_tree_sha256', None),
                    ('commit', 'main'),
                ):
                    with self.subTest(component=entry['name'], field=field, value=value):
                        mutated = json.loads(original)
                        if value is None:
                            del mutated['components'][index][field]
                        else:
                            mutated['components'][index][field] = value
                        try:
                            lock.write_text(json.dumps(mutated))
                            with self.assertRaises((ValueError, self.verifier.deps_lock.DependencyLockError)):
                                self.verify(root)
                        finally:
                            lock.write_text(original)
            self.verify(root)

    def test_pin_parser_matches_assembly_and_lock(self):
        workflow = steps(CI.read_text())
        pins = next(s for s in workflow if s.get('id') == 'pins')
        assembly = _load_workflow_doc(ASSEMBLY.read_text())
        assembly_pins = next(s for s in assembly['jobs']['pinned-assembly']['steps'] if s.get('id') == 'pins')
        self.assertEqual(pins['run'], assembly_pins['run'])
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / 'outputs'
            result = subprocess.run(['bash', '-c', pins['run']], cwd=ROOT,
                                    env=dict(os.environ, GITHUB_OUTPUT=str(output)),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            pin = parse_deps_lock(ROOT / 'deps.lock')['wender-agent']
            self.assertEqual(values['wender_agent_repo'], pin.repo)
            self.assertEqual(values['wender_agent_commit'], pin.commit)

    def test_secret_free_verification_precedes_agents_and_e2e(self):
        text = CI.read_text()
        validate_wender(text)
        for token in ('ASSEMBLY_READ_TOKEN', 'KIT_REPO_TOKEN'):
            with self.subTest(reference=token):
                self.assertNotIn(token, text)
                mutated = text.replace('          JOB_ID: python\n',
                                       '          ' + token + ': ${{ secrets.' + token + ' }}\n'
                                       '          JOB_ID: python\n', 1)
                self.assertNotEqual(mutated, text)
                with self.assertRaises(AssertionError):
                    validate_wender(mutated)
        for job in CONSUMERS:
            with self.subTest(reordered_job=job):
                block = re.search(r'^  ' + re.escape(job) + r':\n.*?(?=^  [\w-]+:|\Z)',
                                  text, re.M | re.S).group()
                start = block.index('      - name: ' + VERIFIER_NAME + '\n')
                end = block.index('      - name:', start + len('      - name:'))
                verifier = block[start:end]
                reordered = block[:start] + block[end:] + verifier
                with self.assertRaises(AssertionError):
                    validate_wender(text.replace(block, reordered, 1))

    def test_invalid_vendored_inputs_emit_blocked_receipt(self):
        script = next(s['run'] for s in steps(CI.read_text())
                      if s.get('name') == VERIFIER_NAME)
        for invalid in (True, False):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory(prefix='wender Música ') as td:
                root = Path(td)
                env = dict(os.environ, RUNNER_TEMP=td, JOB_ID='python', PLATFORM='x64',
                           GITHUB_RUN_ID='fixture', GITHUB_SHA='fixture',
                           GITHUB_STEP_SUMMARY=str(root / 'summary'),
                           PYTHONDONTWRITEBYTECODE='1')
                for variable in ('MV_WENDER_ROOT', 'VQA_VE_ROOT',
                                 'MV_WENDER_EXECUTABLE', 'MV_VISION_EXECUTABLE'):
                    env.pop(variable, None)
                if invalid:
                    env['MV_WENDER_EXECUTABLE'] = 'invalid-vendored-fixture'
                result = subprocess.run(['bash', '-c', script], cwd=ROOT, env=env,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 2 if invalid else 0,
                                 result.stdout + result.stderr)
                receipt_path = root / 'metaviewer-ci/python/x64/python-agents.receipt.json'
                if invalid:
                    self.assertIn('compiled override is not vendored mode', result.stderr)
                    receipt = json.loads(receipt_path.read_text())
                    self.assertEqual(receipt['outcome'], 'BLOCKED')
                    self.assertIsNone(receipt['rc'])
                    self.assertIn('VENDORED_INPUTS_INVALID', receipt['reason'])
                    self.assertIsNone(receipt['counts']['passed'])
                else:
                    self.assertFalse(receipt_path.exists())
                    self.assertEqual(list(root.rglob('*.receipt.json')), [])

    def test_tree_verifier_rejects_mismatching_vendored_file(self):
        with vendored_snapshot() as root:
            self.verify(root)
            for name, relative in VENDORED_PATHS.items():
                with self.subTest(component=name):
                    source = root / relative / 'README.md'
                    original = source.read_bytes()
                    try:
                        source.write_bytes(original + b'\nVendored mutation negative control.\n')
                        with self.assertRaisesRegex(self.verifier.deps_lock.DependencyLockError,
                                                    re.escape(name) + r' vendored tree sha256 .*expected') as raised:
                            self.verify(root)
                        self.assertEqual(raised.exception.code, 'dirty_tree')
                    finally:
                        source.write_bytes(original)
                    self.verify(root)


if __name__ == '__main__':
    unittest.main()
