import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from asset_factory import core
from providers.build123d.adapter import evaluate
from providers.build123d import worker
from tools.cad.contracts import ROOT, request_validate
from tools.cad.fixtures import analytic_part
from tools.cad.process import environment, publish_new_directory, run


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'part.py'
        self.source.write_text('raise RuntimeError("must never execute on refusal")\n')
        self.provider = core.load_json(ROOT/'providers/build123d/provider.json', max_bytes=100000)
        self.policy = analytic_part()[1]
        self.factory = {'commercial_use': False, 'jurisdiction': 'ES'}
        self.licence = {'schema': 'af.territory-groups/v1', 'groups': {}, 'countries': ['ES']}
        self.request = {'schema': 'af.cad-code/v1', 'kind': 'request', 'slot': 'cad_code',
                        'request_id': 'synthetic-cad', 'provider_id': 'build123d',
                        'provider_revision': self.provider['revision'],
                        'inputs': [{'role': 'cad_source', 'path': 'part.py',
                                    'media_type': 'text/x-python', 'sha256': core.sha256_file(self.source)}],
                        'params': {'units': 'mm', 'density_kg_m3': 1000,
                                   'linear_deflection_um': 1, 'angular_deflection_mdeg': 1000,
                                   'g10_policy_sha256': core.sha256_json(self.policy)},
                        'context': {'jurisdiction': 'ES', 'commercial_use': False,
                                    'policy_sha256': core.sha256_json(self.licence),
                                    'toolchain_sha256': 'a'*64, 'timeout_s': 10}}
        self.decision = {'schema': 'af.licence-decision/v1', 'allowed': False, 'rc': 5,
                         'verdict': 'FAIL', 'provider_id': 'build123d', 'operation': 'cad_code',
                         'jurisdiction': 'ES', 'commercial_use': False,
                         'provider_sha256': core.sha256_json(self.provider),
                         'configuration_sha256': core.sha256_json(self.factory),
                         'policy_sha256': core.sha256_json(self.licence)}

    def call(self, output=None):
        return evaluate(self.request, provider=self.provider, factory=self.factory,
                        licence_policy=self.licence, decision=self.decision,
                        g10_policy=self.policy, source_root=self.root,
                        output_dir=output or self.root/'output')

    def test_blocked_runtime_never_publishes(self):
        result, gate = self.call()
        self.assertEqual(result['rc'], 3)
        self.assertEqual(gate['verdict'], 'CANNOT_SEE')
        self.assertIn('research_only_unverified_runtime', gate['reason_codes'])
        self.assertEqual(result['outputs'], [])
        self.assertFalse((self.root/'output').exists())
        core.validate(result, core.load_json(ROOT/'tools/cad/slot.schema.json', max_bytes=100000))

    def test_no_revision_default(self):
        self.request['provider_revision'] = 'main'
        result, gate = self.call()
        self.assertEqual(result['rc'], 2)
        self.assertEqual(gate['reason_codes'], ['unpinned_revision'])

    def test_registered_contract_rejects_blank_request_identity(self):
        self.request['request_id'] = '   '
        result, gate = self.call()
        self.assertEqual(result['rc'], 2)
        self.assertEqual(gate['reason_codes'], ['schema_mismatch'])
        self.assertFalse((self.root/'output').exists())

    def test_refusals_preserve_existing_caller_bytes(self):
        out = self.root/'output'
        out.mkdir()
        (out/'part.step').write_bytes(b'CALLER')
        result, _ = self.call(out)
        self.assertEqual(result['rc'], 5)
        self.assertEqual((out/'part.step').read_bytes(), b'CALLER')

    def test_source_and_policy_and_licence_drift(self):
        original = copy.deepcopy(self.request)
        for mutate in (lambda: self.request['inputs'][0].update(sha256='0'*64),
                       lambda: self.request['params'].update(g10_policy_sha256='0'*64),
                       lambda: self.request['context'].update(policy_sha256='0'*64)):
            self.request = copy.deepcopy(original)
            mutate()
            self.assertEqual(self.call()[0]['rc'], 4)

    def test_source_escape_and_symlink_escape(self):
        outside_temp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)/'outside.py'
        outside.write_text('pass\n')
        self.request['inputs'][0]['path'] = str(outside)
        self.assertEqual(self.call()[0]['rc'], 2)
        (self.root/'link.py').symlink_to(outside)
        self.request['inputs'][0]['path'] = 'link.py'
        self.assertEqual(self.call()[0]['rc'], 2)

    def test_bad_types_and_unknown_params(self):
        for key, value in [('timeout_s', True), ('commercial_use', None)]:
            saved = self.request['context'][key]
            self.request['context'][key] = value
            self.assertEqual(self.call()[0]['rc'], 2)
            self.request['context'][key] = saved
        self.request['params']['invented'] = 1
        self.assertEqual(self.call()[0]['rc'], 2)

    def test_forged_runnable_not_enough(self):
        self.provider['runnable'] = True
        self.provider.pop('blocked_reason')
        self.provider['commercial'] = True
        self.provider['gated'] = False
        self.decision.update(provider_sha256=core.sha256_json(self.provider),
                             allowed=True, rc=0, verdict='PASS')
        self.assertEqual(self.call()[0]['rc'], 3)
        self.provider['runtime'].update(ram_peak_gb_measured=1,
                                       ram_peak_measurement_ref={'path': 'measured.json', 'sha256': 'a'*64})
        self.decision['provider_sha256'] = core.sha256_json(self.provider)
        result, gate = self.call()
        self.assertEqual(result['rc'], 3)
        self.assertEqual(gate['reason_codes'], ['production_cad_qualification_incomplete'])

    def test_worker_without_runtime_returns_cannot_see(self):
        with patch.dict(sys.modules, {'build123d': None}):
            with patch('sys.stdout'):
                rc = worker.main(['--source', str(self.source), '--params', str(self.root/'params.json'),
                                  '--out', str(self.root/'out')])
        self.assertEqual(rc, 3)
        self.assertFalse((self.root/'out').exists())

    def test_owned_process_timeout(self):
        env = environment(self.root, ROOT)
        result = run([sys.executable, '-c', 'while True: pass'], cwd=self.root,
                     timeout_s=0.1, env=env)
        self.assertEqual(result['rc'], 6)
        self.assertTrue(result['timed_out'])

    def test_subprocess_environment_has_no_ambient_credentials(self):
        env = environment(self.root, ROOT)
        self.assertEqual(env['PYTHONHASHSEED'], '0')
        self.assertNotIn('HTTP_PROXY', env)
        self.assertNotIn('HF_TOKEN', env)

    def test_foreign_exit_one_translates_to_error(self):
        result = run([sys.executable, '-c', 'raise SystemExit(1)'], cwd=self.root,
                     timeout_s=1, env=environment(self.root, ROOT))
        self.assertEqual(result['rc'], 2)
        self.assertEqual(result['exit_code'], 1)

    def test_atomic_publication_never_overwrites_caller(self):
        staged, target = self.root/'stage', self.root/'target'
        staged.mkdir()
        (staged/'part.step').write_bytes(b'CANDIDATE')
        target.mkdir()
        (target/'part.step').write_bytes(b'CALLER')
        with self.assertRaises(Exception) as caught:
            publish_new_directory(staged, target)
        self.assertEqual(caught.exception.rc, 5)
        self.assertEqual((target/'part.step').read_bytes(), b'CALLER')
        publish_new_directory(staged, self.root/'fresh')
        self.assertEqual((self.root/'fresh'/'part.step').read_bytes(), b'CANDIDATE')

    def test_worker_script_exit_one_is_refused_and_preserves_bytes(self):
        self.source.write_text('raise SystemExit(1)\n')
        params = self.root/'params.json'
        core.atomic_write_json(params, {'units': 'mm'})
        out = self.root/'out'
        out.mkdir()
        (out/'part.step').write_bytes(b'CALLER')
        with patch.dict(sys.modules, {'build123d': SimpleNamespace()}):
            with patch('sys.stdout'):
                rc = worker.main(['--source', str(self.source), '--params', str(params), '--out', str(out)])
        self.assertEqual(rc, 2)
        self.assertEqual((out/'part.step').read_bytes(), b'CALLER')

    def test_worker_export_failure_preserves_bytes(self):
        self.source.write_text('class Part:\n    volume = 1\npart = Part()\n')
        params = self.root/'params.json'
        core.atomic_write_json(params, {'units': 'mm', 'linear_deflection_um': 1,
                                       'angular_deflection_mdeg': 1000})
        out = self.root/'out'
        out.mkdir()
        (out/'part.step').write_bytes(b'CALLER')
        def fail_step(shape, path, **kwargs):
            Path(path).write_text('partial')
            raise ValueError('export failed')
        with patch.dict(sys.modules, {'build123d': SimpleNamespace(export_step=fail_step)}):
            with patch('sys.stdout'):
                rc = worker.main(['--source', str(self.source), '--params', str(params), '--out', str(out)])
        self.assertEqual(rc, 2)
        self.assertEqual((out/'part.step').read_bytes(), b'CALLER')
        self.assertEqual(list(self.root.glob('.cad-*')), [])


if __name__ == '__main__':
    unittest.main()
