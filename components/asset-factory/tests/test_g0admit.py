"""Admission controls use synthetic authority only; never import a model."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from asset_factory import core, proc
from providers.trellis2 import runtime
from providers.trellis2.adapter import Refusal
from tools.scheduler import scheduler
from test_f0u3_scheduler import inputs, observation

try:
    from providers.trellis2 import admission as a
except ImportError:
    a = None


class SchedulerScope(unittest.TestCase):
    def test_measured_512_request_keeps_authenticated_peak(self):
        stage, _, factory, decision = inputs()
        provider = core.load_json(ROOT/'providers/trellis2/provider.json', max_bytes=2**20)
        stage.update(provider_id=provider['id'], provider_revision=provider['revision'],
                     params={'seed':42, 'pipeline_type':'512'})
        factory['slots']['generate']['provider'] = provider['id']
        factory['memory'].update(ceiling_gb_default=48, min_free_gb_before_start=20)
        decision.update(provider_id=provider['id'], provider_sha256=core.sha256_json(provider),
                        configuration_sha256=core.sha256_json(factory))
        request = scheduler.build_request(stage, provider, factory, decision, lease_path=Path('/tmp/synthetic-lease'))
        self.assertEqual(request['memory']['measured_peak_bytes'], 15*2**30)
        seen = observation(host_mem_available_bytes=64*2**30, cgroup_headroom_bytes=64*2**30,
                           effective_ceiling_bytes=48*2**30)
        self.assertEqual(scheduler.admit(request, seen)['rc'], 0)

    def test_other_workloads_never_inherit_512_peak(self):
        for params in ({'seed': 42, 'pipeline_type': '1536_cascade'},
                       {'seed': 42, 'pipeline_type': '1024'},
                       {'seed': 42, 'pipeline_type': '512', 'resolution': 1536},
                       {'seed': 42}, {'seed': 42, 'pipeline_type': '512', 'steps': 100}):
            with self.subTest(params=params):
                stage, provider, factory, decision = inputs()
                provider['id'] = stage['provider_id'] = 'trellis2'
                factory['slots']['generate']['provider'] = 'trellis2'
                stage['params'] = params
                decision.update(provider_id='trellis2', provider_sha256=core.sha256_json(provider),
                                configuration_sha256=core.sha256_json(factory))
                request = scheduler.build_request(stage, provider, factory, decision, lease_path=Path('/tmp/synthetic-lease'))
                self.assertIsNone(request['memory']['measured_peak_bytes'])
                self.assertEqual(scheduler.admit(request, observation())['rc'], 3)


class NativePermit(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(a, 'native permit verifier missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / 'authority'; self.auth.mkdir()
        self.provider_path = self.root / 'provider.json'
        self.image = self.root / 'reference.png'; self.image.write_bytes(b'synthetic image bytes')
        self.provider = core.load_json(ROOT/'providers/trellis2/provider.json', max_bytes=2**20)
        source = ROOT/'providers/trellis2/build/measurements/gb10-512.json'
        dest = self.root/'build/measurements/gb10-512.json'; dest.parent.mkdir(parents=True)
        dest.write_bytes(source.read_bytes())
        core.atomic_write_json(self.provider_path, self.provider)
        self.approval_path = self.root/'approval.json'
        self.patches = [patch.object(a, 'AUTHORITY_DIR', self.auth),
                        patch.object(a, 'APPROVAL_PATH', self.approval_path),
                        patch.object(a, 'mem_available', return_value=128*2**30),
                        patch.object(a, '_readonly', return_value=None),
                        patch.object(a, '_container_id', return_value='a'*64)]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.binding = a.bindings(self.provider_path)
        self.approval = dict(self.binding, schema='af.trellis-runtime-approval/v1', approved=True,
            approval_id='synthetic-test-only', approved_by='test fixture', expires_unix_s=int(time.time())+120,
            pipeline_type='512', timeout_s=60, ceiling_bytes=48*2**30, min_free_bytes=20*2**30,
            input_sha256=core.sha256_file(self.image), seeds=[42], commercial_use=True,
            calibration_basis='historical GB10 512 low_vram geometry-only measurement')
        core.atomic_write_json(self.approval_path, self.approval)
        self.approval_path.chmod(0o600)
        self.lease = a.Lease(self.root/'model.lock'); self.lease.__enter__()
        self.addCleanup(lambda: self.lease.__exit__(None, None, None))
        receipt = scheduler.native_admission(self.provider, {'seed':42, 'pipeline_type':'512'},
            lease=self.lease, ceiling_bytes=48*2**30, min_free_bytes=20*2**30,
            image_id=self.binding['image_id'], provider_path=self.provider_path)
        self.assertEqual(receipt['rc'], 0, receipt)
        self.permit = a.make_permit(self.approval, core.sha256_file(self.approval_path), self.binding,
            seed=42, pipeline_type='512', input_sha256=core.sha256_file(self.image),
            container_id='a'*64, receipt=receipt)
        self.path = self.auth/'permit.json'; core.atomic_write_json(self.path, self.permit)
        self.path.chmod(0o600)

    def authority(self, **changes):
        permit = deepcopy(self.permit); permit.update(changes)
        server = a.Authority(self.auth, permit, self.lease, lambda pid: pid == os.getpid())
        server.__enter__(); self.addCleanup(lambda: server.__exit__(None, None, None))
        return server

    def verify(self):
        return a.verify(self.path, image_path=self.image, seed=42, pipeline_type='512', provider_path=self.provider_path)

    def refusal(self, code, rc=None):
        with self.assertRaises(Refusal) as caught: self.verify()
        self.assertEqual(caught.exception.code, code)
        if rc is not None: self.assertEqual(caught.exception.rc, rc)

    def test_valid_permit_and_live_lease(self):
        self.authority()
        self.assertEqual(self.verify()['schema'], 'af.trellis-native-permit/v1')

    def test_second_use_is_refused(self):
        self.authority(); self.verify()
        self.refusal('permit_replayed', 5)

    def test_wrong_peer_is_refused(self):
        server = a.Authority(self.auth, self.permit, self.lease, lambda pid: False)
        server.__enter__(); self.addCleanup(lambda: server.__exit__(None, None, None))
        self.refusal('runtime_peer_unproven', 5)

    def test_writable_authority_is_refused(self):
        with patch.object(a, '_readonly', side_effect=Refusal(5, 'authority_mount_writable')):
            self.refusal('authority_mount_writable', 5)

    def test_live_host_memory_drop_is_refused(self):
        self.authority()
        with patch.object(a, 'mem_available', return_value=20*2**30):
            self.refusal('headroom_below_floor', 5)

    def test_lease_inode_replacement_is_refused(self):
        self.authority()
        self.lease.path.unlink(); self.lease.path.touch()
        self.refusal('lease_not_held', 5)

    def test_unproven_cleanup_fences_next_launch(self):
        self.lease.arm(); self.lease.__exit__(None, None, None)
        with self.assertRaises(Refusal) as caught:
            with a.Lease(self.lease.path): pass
        self.assertEqual(caught.exception.code, 'lease_release_unproven')

    def test_verified_release_removes_persistent_fence(self):
        self.lease.arm(); self.lease.release_verified()
        self.assertFalse(self.lease.fence_path.exists())

    def test_mode_is_checked_even_with_matching_authority(self):
        self.authority()
        with self.assertRaises(Refusal) as caught:
            a.verify(self.path, image_path=self.image, seed=42, pipeline_type='1024', provider_path=self.provider_path)
        self.assertEqual(caught.exception.code, 'runtime_operation_unapproved')

    def test_image_replacement_is_refused(self):
        self.authority(); self.image.write_bytes(b'replaced')
        self.refusal('runtime_input_stale', 4)

    def test_runtime_checks_permit_before_decoding(self):
        self.authority()
        with patch.object(runtime, 'PROVIDER_PATH', self.provider_path), \
             patch.object(runtime, 'load_pipeline', side_effect=AssertionError('model work reached')):
            # The fixture deliberately is not an image. Reaching its decoder
            # demonstrates the live permit gate admitted it before model work.
            from PIL import UnidentifiedImageError
            with self.assertRaises(UnidentifiedImageError):
                runtime.execute(image_path=self.image, output_dir=self.root/'out', model_root=self.root,
                    dino_root=self.root, decoder_root=self.root, seed=42, pipeline_type='512', permit_path=self.path)

    def test_native_admission_must_see_lease_and_headroom(self):
        self.lease.__exit__(None, None, None)
        receipt = scheduler.native_admission(self.provider, {'seed':42, 'pipeline_type':'512'},
            lease=self.lease, ceiling_bytes=48*2**30, min_free_bytes=20*2**30,
            image_id=self.binding['image_id'], provider_path=self.provider_path)
        self.assertEqual(receipt['reason_codes'], ['lease_not_held'])

    def test_native_calibration_accepts_only_measured_workload(self):
        for mode in ('1024', '1024_cascade', '1536_cascade'):
            self.assertEqual(a.calibrated_peak(self.provider, {'seed':42, 'pipeline_type':mode},
                             provider_path=self.provider_path), (None, None))
        peak, ref = a.calibrated_peak(self.provider, {'seed':42, 'pipeline_type':'512'}, provider_path=self.provider_path)
        self.assertEqual(peak, 15)
        self.assertEqual(ref['sha256'], self.binding['calibration_sha256'])

    def test_no_permit_before_any_image_or_model_work(self):
        with patch.object(Path, 'open', side_effect=AssertionError('image opened')):
            with self.assertRaises(Refusal) as caught:
                runtime.execute(image_path=self.image, output_dir=self.root/'output', model_root=self.root,
                    dino_root=self.root, decoder_root=self.root, seed=42, pipeline_type='512')
        self.assertEqual(caught.exception.code, 'runtime_admission_unavailable')
        self.assertFalse((self.root/'output').exists())

    def test_permit_other_image_mode_weights_and_tampering(self):
        self.authority()
        for key, value in (('image_id','sha256:'+'b'*64), ('pipeline_type','1024'),
                           ('weights_inventory_sha256','c'*64), ('seed',43), ('timeout_s',1),
                           ('untrusted_extra',True)):
            with self.subTest(key=key):
                changed = deepcopy(self.permit); changed[key] = value
                core.atomic_write_json(self.path, changed)
                self.refusal('permit_tampered', 4)

    def test_matching_authority_still_cannot_change_approved_binding(self):
        self.permit['image_id'] = 'sha256:'+'f'*64
        core.atomic_write_json(self.path, self.permit); self.authority()
        self.refusal('permit_binding_stale', 4)

    def test_stale_calibration(self):
        self.authority()
        (self.root/'build/measurements/gb10-512.json').write_text('{}\n')
        self.refusal('calibration_stale', 4)

    def test_no_live_authority(self):
        self.refusal('runtime_authority_unavailable', 3)

    def test_lease_not_held(self):
        self.authority(); self.lease.__exit__(None, None, None)
        self.refusal('lease_not_held', 5)

    def test_expired_approval(self):
        self.approval['expires_unix_s'] = 1; core.atomic_write_json(self.approval_path, self.approval)
        self.authority(); self.refusal('runtime_approval_expired', 4)

    def test_template_cannot_authorize(self):
        self.approval['approved'] = False; core.atomic_write_json(self.approval_path, self.approval)
        self.authority(); self.refusal('runtime_approval_unavailable', 3)

    def test_replayed_admission(self):
        self.permit['scheduler']['admitted_monotonic_ns'] = 1
        core.atomic_write_json(self.path, self.permit); self.authority()
        self.refusal('admission_stale', 4)

    def test_r44_mocked_popen_never_signals(self):
        with patch('subprocess.Popen', return_value=MagicMock()), \
             patch.object(os, 'kill') as kill, patch.object(os, 'killpg') as killpg:
            with self.assertRaises(proc.SignalRefusal):
                proc.popen(['/usr/bin/true'], start_new_session=True)
        kill.assert_not_called(); killpg.assert_not_called()


class HostIssuer(unittest.TestCase):
    def test_missing_approval_never_calls_docker(self):
        from providers.trellis2 import admission_runner as runner
        with tempfile.TemporaryDirectory() as tmp, patch.object(a, 'APPROVAL_PATH', Path('/nonexistent-synthetic-approval')), \
             patch.object(runner, 'docker') as docker:
            with self.assertRaises(Refusal) as caught:
                runner.run(image_path=Path(tmp)/'missing', output_root=Path(tmp)/'out', model_root=tmp,
                           dino_root=tmp, decoder_root=tmp, seed=42, pipeline_type='512')
            self.assertEqual((caught.exception.rc, caught.exception.code), (3, 'admission_evidence_missing'))
            self.assertFalse((Path(tmp)/'out').exists())
        docker.assert_not_called()

    def test_guarded_runner_mocked_popen_never_signals(self):
        from providers.trellis2 import admission_runner as runner
        from providers.trellis2.build.build import Refusal as BuildRefusal
        with patch('subprocess.Popen', return_value=MagicMock()), \
             patch.object(os, 'kill') as kill, patch.object(os, 'killpg') as killpg:
            with self.assertRaises(BuildRefusal) as caught:
                runner.docker('ps', '-q')
            self.assertEqual((caught.exception.rc, caught.exception.reason), (3, 'signal_target_not_integer'))
        kill.assert_not_called(); killpg.assert_not_called()

    def test_peer_identity_binds_actual_container_image_and_limits(self):
        from providers.trellis2 import admission_runner as runner
        image = 'sha256:'+'b'*64
        value = {'Image':image, 'Name':'/af-synthetic', 'State':{'Pid':os.getpid(), 'Running':True},
                 'HostConfig':{'NetworkMode':'none', 'Memory':48*2**30, 'MemorySwap':48*2**30}}
        with patch.object(runner, 'inspect_container', return_value=value):
            self.assertTrue(runner._peer_owned(os.getpid(), 'a'*64, image, 48*2**30))
            value['Image'] = 'sha256:'+'c'*64
            with self.assertRaises(Refusal) as caught:
                runner._peer_owned(os.getpid(), 'a'*64, image, 48*2**30)
            self.assertEqual(caught.exception.code, 'runtime_container_unproven')


if __name__ == '__main__': unittest.main()
