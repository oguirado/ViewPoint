"""Synthetic CPU-bound fixtures; none of these qualify real model quality."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image
from asset_factory import core
from asset_factory.contracts import validate_provider
from gates import input_gate
from providers.birefnet import adapter as birefnet
from providers.realesrgan import adapter as realesrgan
from providers.birefnet import offline, worker


ROOT = Path(__file__).resolve().parents[1]


def image_bytes(*, alpha=False, side=16):
    image = Image.new('RGBA', (side, side), (28, 44, 66, 255))
    for y in range(side):
        for x in range(side):
            image.putpixel((x, y), (x*13 % 256, y*11 % 256, 100,
                                   255 if not alpha or (5 <= x < 11 and 5 <= y < 11) else 0))
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def chosen_policy():
    policy = {'schema': 'af.input-policy/v1', 'min_side_px': 16, 'max_encoded_bytes': 65536,
              'max_decoded_pixels': 4096, 'alpha_background_max': 0, 'alpha_foreground_min': 255,
              'min_foreground_ppm': 100000, 'max_foreground_ppm': 700000,
              'max_border_foreground_ppm': 0, 'max_small_component_ppm': 0,
              'max_soft_alpha_ppm': 100000, 'small_component_max_pixels': 2,
              'max_components': 1, 'connectivity': 8, 'ppm_rounding': 'floor', 'calibration_ref': None}
    calibration = core.canonical_json({'schema': 'af.input-calibration/v1', 'rc': 0, 'verdict': 'PASS',
        'policy_sha256': core.sha256_json({k: v for k, v in policy.items() if k != 'calibration_ref'})}).encode()
    policy['calibration_ref'] = {'path': 'synthetic-only.json', 'sha256': offline.digest(calibration)}
    return policy, calibration


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = image_bytes()
        self.candidate = image_bytes(alpha=True)

    def fixture(self, name='birefnet'):
        recipe = copy.deepcopy(offline.recipe_for(name))
        model_root = self.root / name
        model_root.mkdir(exist_ok=True)
        for item in recipe['files']:
            data = b'authenticated synthetic fixture'
            if item['path'] == 'config.json':
                data = b'{"bb_pretrained":false}'
            (model_root / item['path']).write_bytes(data)
            item['bytes'] = len(data)
            item['sha256'] = offline.digest(data)
        recipe['revision'] = 'local-sha256:' + core.sha256_json(recipe['files'])
        recipe['rights_status'] = 'PASS'
        for package in recipe['packages']:
            package['version'] = 'synthetic-only'
        provider = copy.deepcopy(offline.provider_for(name))
        provider.pop('blocked_reason')
        provider.update(runnable=True, revision=recipe['revision'], commercial=True, gated=False)
        provider['licence'] = {'code': 'SYNTHETIC', 'weights': 'SYNTHETIC', 'deps': []}
        provider['weights'] = [{'repo': 'synthetic-only', 'revision': recipe['revision'],
            'files': [{k: f[k] for k in ('path', 'sha256')} for f in recipe['files']
                      if f['path'].endswith(('.safetensors', '.pth'))]}]
        memory_path = self.root / (name + '-memory.json')
        core.atomic_write_json(memory_path, {'schema': 'af.offline-image-peak/v1',
                                             'weights_sha256': core.sha256_json(provider['weights']),
                                             'recipe_sha256': core.sha256_json(recipe),
                                             'code_sha256': offline.code_identity(name),
                                             'peak_rss_bytes': 1024})
        provider['runtime']['ram_peak_gb_measured'] = 1
        provider['runtime']['ram_peak_measurement_ref'] = {'path': str(memory_path), 'sha256': core.sha256_file(memory_path)}
        factory = {'schema': 'af.factory-config/v1', 'jurisdiction': 'ES', 'commercial_use': False,
            'models_path': 'synthetic-only', 'memory': {'max_resident_models': 1,
            'ceiling_gb_default': 8, 'min_free_gb_before_start': 0},
            'slots': {slot: {'provider': name if slot == 'generate' else 'none'} for slot in core.SLOTS},
            'gates': {'appearance': {'floor': 4, 'target': 7}, 'vqa3d_profile': 'synthetic-only'}}
        policy = core.load_json(ROOT / 'config/territory-groups.v1.json', max_bytes=offline.MAX_JSON)
        gate_policy, calibration = chosen_policy()
        args = dict(factory=factory, licence_policy=policy, gate_policy=gate_policy,
                    calibration_bytes=calibration, lease_path=self.root / 'shared.lease',
                    timeout_s=2, recipe=recipe, provider=provider)
        return model_root, args

    def observed_packages(self, recipe):
        return [{**p, 'available': True, 'installed_version': p['version']} for p in recipe['packages']]

    def fake_worker(self, name, source, root, recipe, scratch, timeout):
        candidate = self.candidate
        if name == 'realesrgan':
            original = offline.decode(source, recipe)
            image = original.resize((64, 64), Image.Resampling.NEAREST)
            stream = io.BytesIO()
            image.save(stream, format='PNG')
            candidate = stream.getvalue()
        return candidate, {'peak_rss_bytes': 1024}

    def run_prepare(self, name='birefnet', mutate=None):
        model_root, args = self.fixture(name)
        if mutate:
            mutate(model_root, args)
        function = birefnet.prepare if name == 'birefnet' else realesrgan.prepare
        with mock.patch.object(offline, 'package_observation', side_effect=self.observed_packages), \
             mock.patch.object(offline, 'run_worker', side_effect=self.fake_worker) as inference:
            result = function(self.source if name == 'birefnet' else self.candidate,
                              model_root, self.root / 'output', **args)
        return result, args, inference

    def test_shipped_manifests_and_recipes_validate_and_stay_blocked(self):
        for name in ('birefnet', 'realesrgan'):
            with self.subTest(name=name):
                provider = offline.provider_for(name)
                validate_provider(provider)
                offline.validate_recipe(offline.recipe_for(name), name)
                self.assertFalse(provider['runnable'])
                self.assertIsNone(provider['runtime']['ram_peak_gb_measured'])

    def test_local_pin_probe_positive_and_changed_weight_red_then_green(self):
        root, args = self.fixture()
        with mock.patch.object(offline, 'package_observation', side_effect=self.observed_packages):
            self.assertEqual(offline.probe('birefnet', self.source, root, recipe=args['recipe'])['rc'], 0)
            path = root / 'model.safetensors'
            saved = path.read_bytes()
            path.write_bytes(b'X' + saved[1:])
            red = offline.probe('birefnet', self.source, root, recipe=args['recipe'])
            self.assertEqual((red['rc'], red['reason_codes']), (4, ['model_bytes_stale']))
            path.write_bytes(saved)
            self.assertEqual(offline.probe('birefnet', self.source, root, recipe=args['recipe'])['rc'], 0)

    def test_missing_dependency_is_cannot_see(self):
        root, args = self.fixture()
        observed = self.observed_packages(args['recipe'])
        observed[0]['available'] = False
        with mock.patch.object(offline, 'package_observation', return_value=observed):
            result = offline.probe('birefnet', self.source, root, recipe=args['recipe'])
        self.assertEqual(result['rc'], 3)
        self.assertEqual(result['quality_verdict'], 'CANNOT_SEE')

    def test_version_drift_is_stale(self):
        root, args = self.fixture()
        observed = self.observed_packages(args['recipe'])
        observed[0]['installed_version'] = 'changed'
        with mock.patch.object(offline, 'package_observation', return_value=observed):
            self.assertEqual(offline.probe('birefnet', self.source, root, recipe=args['recipe'])['rc'], 4)

    def test_escape_symlink_never_authenticates(self):
        root, args = self.fixture()
        file = root / 'model.safetensors'
        outside = self.root / 'outside.safetensors'
        outside.write_bytes(file.read_bytes())
        file.unlink()
        file.symlink_to(outside)
        self.assertEqual(offline.probe('birefnet', self.source, root, recipe=args['recipe'])['rc'], 2)

    def test_remote_or_missing_model_root_is_refused(self):
        root, args = self.fixture()
        result = offline.probe('birefnet', self.source, 'https://invalid.example/weights', recipe=args['recipe'])
        self.assertEqual(result['rc'], 3)

    def test_pretrained_backbone_flag_refused(self):
        root, args = self.fixture()
        recipe = args['recipe']
        data = b'{"bb_pretrained":true}'
        (root / 'config.json').write_bytes(data)
        item = next(f for f in recipe['files'] if f['path'] == 'config.json')
        item.update(bytes=len(data), sha256=offline.digest(data))
        recipe['revision'] = 'local-sha256:' + core.sha256_json(recipe['files'])
        result = offline.probe('birefnet', self.source, root, recipe=recipe)
        self.assertEqual((result['rc'], result['reason_codes']), (5, ['backbone_download_refused']))

    def test_recipe_rejects_float_boolean_budget_and_unknown_key(self):
        for mutation in ({'timeout_s': True}, {'max_pixels': 1.5}, {'extra': 1}):
            with self.subTest(mutation=mutation):
                recipe = {**offline.recipe_for('birefnet'), **mutation}
                self.assertEqual(offline.probe('birefnet', self.source, self.root, recipe=recipe)['rc'], 2)

    def test_recipe_rejects_package_module_substitution(self):
        recipe = copy.deepcopy(offline.recipe_for('birefnet'))
        recipe['packages'][0]['module'] = 'os'
        self.assertEqual(offline.probe('birefnet', self.source, self.root, recipe=recipe)['rc'], 2)

    def test_corrupt_input_and_budget_before_model_load(self):
        recipe = offline.recipe_for('birefnet')
        for data, rc in ((b'broken PNG', 2), (b'x' * (recipe['max_input_bytes'] + 1), 6)):
            with self.subTest(rc=rc), mock.patch.object(offline, 'verify_local') as verify:
                self.assertEqual(offline.probe('birefnet', data, self.root)['rc'], rc)
                verify.assert_not_called()

    def test_positive_preparation_binds_existing_input_gate(self):
        result, args, inference = self.run_prepare()
        self.assertEqual(result['rc'], 0, result)
        self.assertEqual(result['gate']['verdict'], 'PASS')
        self.assertEqual(result['quality_verdict'], 'CANNOT_SEE')
        self.assertEqual(result['preparation']['schema'], 'af.reference-preparation/v1')
        self.assertEqual(result['preparation']['candidate_sha256'], offline.digest(self.candidate))
        stored = self.root / 'output/receipt.json'
        self.assertEqual(stored.read_text(), core.canonical_json(result))
        self.assertEqual((self.root / 'output/candidate.png').read_bytes(), self.candidate)
        self.assertFalse(args['lease_path'].exists())
        inference.assert_called_once()

    def test_upscale_positive_preserves_alpha_and_measures_x4(self):
        result, args, inference = self.run_prepare('realesrgan')
        self.assertEqual(result['rc'], 0, result)
        self.assertIsNone(result['preparation'])
        self.assertEqual(result['metrics']['output_width'], 64)
        self.assertEqual(result['metrics']['scale'], 4)
        self.assertEqual(result['gate']['verdict'], 'PASS')

    def test_blocked_licence_never_loads_model(self):
        def change(root, args):
            args['provider']['runnable'] = False
            args['provider']['blocked_reason'] = 'synthetic refusal'
        result, args, inference = self.run_prepare(mutate=change)
        self.assertEqual((result['rc'], result['reason_codes']), (5, ['licence_refused']))
        inference.assert_not_called()
        self.assertFalse((self.root / 'output').exists())

    def test_commercial_use_is_required_no_default(self):
        def change(root, args):
            del args['factory']['commercial_use']
        result, args, inference = self.run_prepare(mutate=change)
        self.assertEqual(result['rc'], 2)
        inference.assert_not_called()

    def test_missing_calibration_never_loads_model(self):
        def change(root, args):
            args['gate_policy']['calibration_ref'] = None
        result, args, inference = self.run_prepare(mutate=change)
        self.assertEqual((result['rc'], result['reason_codes']), (3, ['policy_uncalibrated']))
        inference.assert_not_called()

    def test_unmeasured_peak_never_loads_model(self):
        def change(root, args):
            args['provider']['runtime'].update(ram_peak_gb_measured=None, ram_peak_measurement_ref=None)
        result, args, inference = self.run_prepare(mutate=change)
        self.assertEqual(result['rc'], 3)
        inference.assert_not_called()

    def test_peak_measurement_drift_never_loads_model(self):
        def change(root, args):
            path = Path(args['provider']['runtime']['ram_peak_measurement_ref']['path'])
            value = core.load_json(path, max_bytes=offline.MAX_JSON)
            value['recipe_sha256'] = 'a' * 64
            core.atomic_write_json(path, value)
            args['provider']['runtime']['ram_peak_measurement_ref']['sha256'] = core.sha256_file(path)
        result, args, inference = self.run_prepare(mutate=change)
        self.assertEqual((result['rc'], result['reason_codes']), (4, ['peak_measurement_stale']))
        inference.assert_not_called()

    def test_memory_floor_and_unknown_observation_refuse(self):
        factory = {'memory': {'max_resident_models': 1, 'ceiling_gb_default': 8, 'min_free_gb_before_start': 20}}
        recipe = offline.recipe_for('birefnet')
        original_read = Path.read_text
        for text, code in (('MemAvailable: 1048576 kB\n', 'headroom_below_floor'),
                           ('MemTotal: 1048576 kB\n', 'memavailable_unknown')):
            def read(path, *args, **kwargs):
                return text if path == Path('/proc/meminfo') else original_read(path, *args, **kwargs)
            with self.subTest(code=code), mock.patch.object(Path, 'read_text', read):
                with self.assertRaises(offline.Issue) as raised:
                    offline.admit_memory(factory, 2**30, recipe)
                self.assertEqual(raised.exception.code, code)

    def test_worker_timeout_preserves_target_and_releases_lease(self):
        root, args = self.fixture()
        with mock.patch.object(offline, 'package_observation', side_effect=self.observed_packages), \
             mock.patch.object(offline, 'run_worker', side_effect=offline.Issue(6, 'deadline_exceeded')):
            result = birefnet.prepare(self.source, root, self.root / 'output', **args)
        self.assertEqual((result['rc'], result['reason_codes']), (6, ['deadline_exceeded']))
        self.assertFalse(args['lease_path'].exists())
        self.assertFalse((self.root / 'output').exists())

    def test_stale_worker_output_hash_refuses_publication(self):
        root, args = self.fixture()
        scratch = self.root / 'scratch'
        scratch.mkdir()
        (scratch / 'candidate.png').write_bytes(self.candidate)
        core.atomic_write_json(scratch / 'worker.json', {'rc': 0, 'source_sha256': offline.digest(self.source),
            'candidate_sha256': 'a'*64, 'recipe_sha256': core.sha256_json(args['recipe']), 'peak_rss_bytes': 1024})
        with mock.patch.object(offline, 'supervised'):
            with self.assertRaises(offline.Issue) as raised:
                offline.run_worker('birefnet', self.source, root, args['recipe'], scratch, 2)
        self.assertEqual(raised.exception.code, 'worker_evidence_stale')

    def test_result_contract_rejects_undeclared_fields(self):
        with self.assertRaises(core.ContractError):
            offline.record('birefnet', 0, undeclared=True)

    def test_held_lease_never_loads_and_preserves_owner(self):
        def change(root, args):
            args['lease_path'].write_bytes(b'previous owner')
        result, args, inference = self.run_prepare(mutate=change)
        self.assertEqual((result['rc'], result['reason_codes']), (5, ['lease_held']))
        self.assertEqual(args['lease_path'].read_bytes(), b'previous owner')
        inference.assert_not_called()

    def test_existing_output_preserved(self):
        target = self.root / 'output'
        target.mkdir()
        (target / 'candidate.png').write_bytes(b'caller bytes')
        result, args, inference = self.run_prepare()
        self.assertEqual((result['rc'], result['reason_codes']), (5, ['output_exists']))
        self.assertEqual((target / 'candidate.png').read_bytes(), b'caller bytes')
        inference.assert_not_called()

    def test_wrong_size_or_rgb_and_constant_masks_fail(self):
        recipe = offline.recipe_for('birefnet')
        original = offline.decode(self.source, recipe)
        candidate = offline.decode(self.candidate, recipe)
        for altered, code in ((candidate.resize((8, 8)), 'output_shape_or_channels'),
                              (Image.new('RGBA', (16, 16), (0, 0, 0, 0)), 'source_rgb_changed')):
            with self.subTest(code=code), self.assertRaises(offline.Issue) as raised:
                offline.measurements(original, altered, 'birefnet', 1)
            self.assertEqual(raised.exception.code, code)
        for alpha in (0, 127, 255):
            candidate.putalpha(alpha)
            with self.subTest(alpha=alpha), self.assertRaises(offline.Issue) as raised:
                offline.measurements(original, candidate, 'birefnet', 1)
            self.assertEqual(raised.exception.code, 'constant_alpha')

    def test_upscale_wrong_shape_flat_rgb_and_changed_alpha_fail(self):
        recipe = offline.recipe_for('realesrgan')
        original = offline.decode(self.candidate, recipe)
        candidate = original.resize((64, 64), Image.Resampling.NEAREST)
        bad_alpha = candidate.copy()
        bad_alpha.putalpha(255)
        flat = Image.new('RGBA', (64, 64), (100, 100, 100, 255))
        flat.putalpha(candidate.getchannel('A'))
        for altered, code in ((original, 'output_shape_or_channels'), (bad_alpha, 'alpha_changed'), (flat, 'flat_upscale')):
            with self.subTest(code=code), self.assertRaises(offline.Issue) as raised:
                offline.measurements(original, altered, 'realesrgan', 4)
            self.assertEqual(raised.exception.code, code)

    def test_input_gate_opaque_mutant_red_and_good_green(self):
        self.candidate = self.source
        red, args, inference = self.run_prepare()
        self.assertEqual((red['rc'], red['reason_codes']), (5, ['constant_alpha']))
        self.assertFalse((self.root / 'output').exists())
        self.assertFalse(args['lease_path'].exists())
        self.candidate = image_bytes(alpha=True)
        green, args, inference = self.run_prepare()
        self.assertEqual(green['rc'], 0, green)

    def test_input_gate_border_failure_blocks_publication(self):
        image = offline.decode(self.source, offline.recipe_for('birefnet'))
        alpha = Image.new('L', image.size, 0)
        for y in range(5, 11):
            for x in range(6):
                alpha.putpixel((x, y), 255)
        image.putalpha(alpha)
        stream = io.BytesIO()
        image.save(stream, format='PNG')
        self.candidate = stream.getvalue()
        result, args, inference = self.run_prepare()
        self.assertEqual(result['rc'], 5, result)
        self.assertEqual(result['gate']['reason_codes'], ['border_leakage'])
        self.assertFalse((self.root / 'output').exists())
        self.assertFalse(args['lease_path'].exists())

    def test_detector_catches_mutant_that_disables_alpha_check(self):
        original = offline.decode(self.source, offline.recipe_for('birefnet'))
        class NegativeControl(unittest.TestCase):
            def runTest(inner):
                with inner.assertRaises(offline.Issue):
                    offline.measurements(original, original, 'birefnet', 1)
        nominal = unittest.TestResult()
        NegativeControl().run(nominal)
        self.assertTrue(nominal.wasSuccessful())
        with mock.patch.object(offline, 'measurements', return_value={}):
            red = unittest.TestResult()
            NegativeControl().run(red)
        self.assertEqual(len(red.failures), 1)
        green = unittest.TestResult()
        NegativeControl().run(green)
        self.assertTrue(green.wasSuccessful())

    def test_network_is_refused_without_opening_connection(self):
        for event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'urllib.Request'):
            with self.subTest(event=event), self.assertRaises(offline.Issue) as raised:
                worker.deny_network(event, ())
            self.assertEqual(raised.exception.rc, 5)

    def test_publication_io_failure_removes_only_owned_new_directory(self):
        target = self.root / 'fresh'
        with mock.patch.object(core, 'atomic_write_json', side_effect=OSError('planted failure')):
            with self.assertRaises(OSError):
                offline.publish(target, self.candidate, {'candidate_sha256': offline.digest(self.candidate)})
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_supervisor_timeout_kills_owned_group(self):
        with self.assertRaises(offline.Issue) as raised:
            offline.supervised([sys.executable, '-c', 'import time; time.sleep(30)'],
                               env={'PATH': os.defpath}, timeout_s=0.05, cwd=self.root)
        self.assertEqual((raised.exception.rc, raised.exception.code), (6, 'deadline_exceeded'))

    def test_supervisor_foreign_rc_one_is_error(self):
        with self.assertRaises(offline.Issue) as raised:
            offline.supervised([sys.executable, '-c', 'raise SystemExit(1)'],
                               env={'PATH': os.defpath}, timeout_s=2, cwd=self.root)
        self.assertEqual((raised.exception.rc, raised.exception.code), (2, 'worker_contract'))

    def test_cli_usage_and_corrupt_input_preserve_caller_bytes(self):
        target = self.root / 'caller.png'
        target.write_bytes(b'caller-owned')
        source = self.root / 'corrupt.png'
        source.write_bytes(b'corrupt')
        for module in ('providers.birefnet.adapter', 'providers.realesrgan.adapter'):
            result = subprocess.run([sys.executable, '-B', '-m', module, '--source', str(source),
                '--model-root', str(self.root), '--output', str(target)], cwd=ROOT,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(ROOT)},
                capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout)['rc'], 2)
            self.assertEqual(target.read_bytes(), b'caller-owned')


if __name__ == '__main__':
    unittest.main()
