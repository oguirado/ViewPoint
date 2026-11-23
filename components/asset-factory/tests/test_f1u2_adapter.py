"""Synthetic, isolated service tests; no model/container/GPU is invoked."""
import asyncio
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

# python -I omits cwd; explicitly add only this component's known root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from providers.trellis2 import adapter as a


def digest(data):
    return hashlib.sha256(data).hexdigest()


def glb(*, document=None, binary=None):
    if binary is None:
        binary = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
    if document is None:
        document = {'asset': {'version': '2.0'}, 'buffers': [{'byteLength': len(binary)}],
                    'bufferViews': [{'buffer': 0, 'byteLength': len(binary)}],
                    'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3, 'type': 'VEC3',
                                   'min': [0, 0, 0], 'max': [1, 1, 0]}],
                    'meshes': [{'primitives': [{'attributes': {'POSITION': 0}}]}],
                    'nodes': [{'mesh': 0}], 'scenes': [{'nodes': [0]}], 'scene': 0}
    payload = json.dumps(document, separators=(',', ':')).encode()
    payload += b' ' * (-len(payload) % 4)
    binary += b'\0' * (-len(binary) % 4)
    return (struct.pack('<4sII', b'glTF', 2, 28 + len(payload) + len(binary))
            + struct.pack('<I4s', len(payload), b'JSON') + payload
            + struct.pack('<I4s', len(binary), b'BIN\0') + binary)


class FakeService:
    def __init__(self, *, body=None, delay=0, piece=0, echo=None, response_rc=0,
                 provider_hash=None, output_hash=None, geometry_only=True,
                 raw_preserved=True, peak=None, quiescent=True, bad_ack=False, cancel_delay=0):
        self.body = glb() if body is None else body
        self.delay, self.piece, self.echo = delay, piece, echo
        self.response_rc, self.provider_hash, self.output_hash = response_rc, provider_hash, output_hash
        self.geometry_only, self.raw_preserved, self.peak = geometry_only, raw_preserved, peak
        self.quiescent, self.bad_ack, self.cancel_delay = quiescent, bad_ack, cancel_delay
        self.calls, self.cancels, self.busy = 0, 0, False
        self.started = asyncio.Event()
        self.request = None
        self.provider = None

    async def generate(self, image, request, *, deadline_ns):
        self.calls += 1
        self.busy = True
        self.started.set()
        self.request = deepcopy(request)
        echoed = deepcopy(request)
        if self.echo:
            self.echo(echoed)
        async def stream():
            size = self.piece or max(1, len(self.body))
            for offset in range(0, len(self.body), size):
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield self.body[offset:offset + size]
            self.busy = False
        return a.Response(echoed, self.provider_hash or a.core.sha256_json(self.provider),
                          self.output_hash or digest(self.body), stream(), self.geometry_only,
                          self.raw_preserved, self.peak, self.response_rc,
                          {'path': 'synthetic/peak-observation.json', 'sha256': 'e' * 64}
                          if type(self.peak) is int and self.peak > 0 else None)

    async def cancel(self, request, *, deadline_ns):
        self.cancels += 1
        if self.cancel_delay:
            await asyncio.sleep(self.cancel_delay)
        if self.quiescent:
            self.busy = False
        return {'request_id': request['request_id'], 'request_sha256': 'f' * 64 if self.bad_ack
                else a.core.sha256_json(request), 'quiescent': self.quiescent}


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.output = self.parent / 'generation'
        self.image = b'\x89PNG\r\n\x1a\nsynthetic image gate fixture'
        self.provider = {'schema': 'af.provider/v1', 'id': 'trellis2', 'slot': 'generate',
                         'kind': 'model', 'runnable': True, 'revision': 'a' * 40,
                         'licence': {'code': 'MIT', 'weights': 'MIT', 'deps': []},
                         'territory_excluded': [], 'commercial': True, 'gated': False,
                         'weights': [{'repo': 'synthetic/model', 'revision': 'b' * 40,
                                      'files': [{'path': 'synthetic.bin', 'sha256': 'c' * 64}]}],
                         'runtime': {'container': 'build/Dockerfile', 'ram_peak_gb_measured': None,
                                     'ram_peak_measurement_ref': None}, 'known_issues': []}
        self.factory = {'schema': 'af.factory-config/v1', 'jurisdiction': 'ES',
                        'commercial_use': False, 'models_path': 'synthetic/models',
                        'memory': {'max_resident_models': 1, 'ceiling_gb_default': 40,
                                   'min_free_gb_before_start': 20},
                        'slots': {slot: {'provider': 'trellis2' if slot == 'generate' else 'none'}
                                  for slot in a.core.SLOTS},
                        'gates': {'appearance': {'floor': 4, 'target': 7}, 'vqa3d_profile': 'asset-intake'}}
        self.policy = {'schema': 'af.territory-groups/v1', 'groups': {'EU': ['ES'], 'UK': ['GB'], 'KR': ['KR']},
                       'countries': ['ES', 'GB', 'KR']}
        self.request = {'schema': 'af.generate/v1', 'kind': 'request', 'slot': 'generate',
                        'request_id': 'synthetic-generation', 'provider_id': 'trellis2',
                        'provider_revision': 'a' * 40,
                        'inputs': [{'role': 'image', 'path': 'synthetic/input.png', 'media_type': 'image/png',
                                    'sha256': digest(self.image)}],
                        'params': {'seed': 17, 'pipeline_type': '1024'},
                        'context': {'jurisdiction': 'ES', 'commercial_use': False,
                                    'policy_sha256': a.core.sha256_json(self.policy),
                                    'toolchain_sha256': 'd' * 64, 'timeout_s': 1800}}
        self.decision = {'schema': 'af.licence-decision/v1', 'rc': 0, 'verdict': 'PASS', 'allowed': True,
                         'provider_id': 'trellis2', 'operation': 'generate', 'jurisdiction': 'ES',
                         'commercial_use': False, 'monetisable': True, 'reason_codes': [],
                         'evaluated_dependencies': [], 'configuration_sha256': a.core.sha256_json(self.factory),
                         'provider_sha256': a.core.sha256_json(self.provider),
                         'policy_sha256': a.core.sha256_json(self.policy)}

    async def call(self, service=None, **kwargs):
        service = service if service is not None else FakeService()
        service.provider = self.provider
        try:
            return await a.generate(self.image, seed=17, pipeline_type='1024', output_dir=self.output,
                                    request=self.request, provider=self.provider, decision=self.decision,
                                    factory=self.factory, policy=self.policy, service=service, **kwargs)
        except a.core.ContractError as exc:
            # Only the CLI/caller boundary serializes unparseable identity errors.
            return exc.as_dict()

    def assert_rc(self, result, rc, code=None):
        self.assertEqual(result['rc'], rc, result)
        self.assertEqual(result['status'], a.core.STATUS[rc])
        self.assertNotEqual(result['rc'], 1)
        self.assertNotIn('verdict', result)
        if rc:
            self.assertEqual(result['outputs'], [])
            self.assertEqual(result['receipts'], [])
            self.assertIsInstance(result['absence_reasons']['outputs'], str)
            self.assertNotEqual(result['absence_reasons']['outputs'], 'adapter_error', result)
            if code is not None:
                self.assertEqual(result['absence_reasons']['outputs'], code)

    def snapshot(self):
        return {str(p.relative_to(self.parent)): p.read_bytes()
                for p in self.parent.rglob('*') if p.is_file()}

    def sentinel(self):
        (self.parent / 'caller.glb').write_bytes(b'existing raw bytes')
        (self.parent / 'caller.json').write_bytes(b'existing receipt bytes')
        return self.snapshot()

    async def test_generate_valid_glb_binds_effective_parameters(self):
        service = FakeService(peak=12345)
        result = await self.call(service)
        self.assert_rc(result, 0)
        self.assertEqual(service.request['params'], {'seed': 17, 'pipeline_type': '1024'})
        self.assertEqual((self.output / 'raw.glb').read_bytes(), glb())
        self.assertEqual((self.output / 'result.json').read_text(), a.core.canonical_json(result))
        request_bytes = (self.output / 'request.json').read_bytes()
        self.assertEqual(digest(request_bytes), result['receipts'][0]['sha256'])
        self.assertEqual(json.loads(request_bytes), self.request)
        self.assertEqual(digest(glb()), result['outputs'][0]['sha256'])
        self.assertEqual(json.loads((self.output / 'service-evidence.json').read_text())['service_peak_rss_bytes'], 12345)
        self.assertIsNone(result['metrics']['peak_rss_bytes'])
        self.assertEqual(result['absence_reasons']['peak_rss_bytes'], 'service_self_reported')
        self.assertEqual(service.cancels, 0)

    async def test_parameter_echo_drift_refuses(self):
        before = self.sentinel()
        result = await self.call(FakeService(echo=lambda req: req['params'].update(seed=42)))
        self.assert_rc(result, 4, 'effective_request_stale')
        self.assertEqual(self.snapshot(), before)

    async def test_other_bound_echo_drift_refuses(self):
        for change in (lambda req: req['context'].update(toolchain_sha256='e' * 64),
                       lambda req: req.update(request_id='other'),
                       lambda req: req['inputs'][0].update(sha256='e' * 64)):
            with self.subTest(change=change):
                self.assert_rc(await self.call(FakeService(echo=change)), 4, 'effective_request_stale')

    async def test_malformed_response_preserves_caller_bytes(self):
        before = self.sentinel()
        bodies = (b'', b'<html>error</html>', b'glTF', glb()[:-1], glb() + b'extra',
                  glb()[:12] + struct.pack('<I', 2**32 - 4) + glb()[16:])
        codes = ('glb_invalid', 'glb_invalid', 'glb_invalid', 'glb_header_invalid',
                 'glb_header_invalid', 'glb_chunk_invalid')
        for body, code in zip(bodies, codes, strict=True):
            with self.subTest(length=len(body)):
                self.assert_rc(await self.call(FakeService(body=body)), 2, code)
                self.assertEqual(self.snapshot(), before)

    async def test_glb_mesh_and_buffer_bounds(self):
        a.validate_glb(glb())
        doc = json.loads(glb()[20:20 + struct.unpack_from('<I', glb(), 12)[0]])
        cases = []
        for mutate in (lambda d: d['bufferViews'][0].update(byteLength=1000),
                       lambda d: d['accessors'][0].update(count=100),
                       lambda d: d.update(meshes=[]),
                       lambda d: d['buffers'][0].update(uri='external.bin'),
                       lambda d: d['meshes'][0]['primitives'][0]['attributes'].update(POSITION=True),
                       lambda d: d.update(extensionsRequired=['unavailable'])):
            candidate = deepcopy(doc)
            mutate(candidate)
            cases.append(glb(document=candidate))
        codes = ('glb_view_invalid', 'glb_accessor_bounds', 'glb_mesh_missing',
                 'glb_buffer_invalid', 'glb_positions_invalid', 'glb_extensions_unsupported')
        for candidate, code in zip(cases, codes, strict=True):
            with self.subTest(sha=digest(candidate)):
                self.assert_rc(await self.call(FakeService(body=candidate)), 2, code)

    async def test_nonfinite_position_refuses(self):
        before = self.sentinel()
        data = glb(binary=struct.pack('<9f', float('nan'), 0, 0, 1, 0, 0, 0, 1, 0))
        self.assert_rc(await self.call(FakeService(body=data)), 2, 'glb_nonfinite_position')
        self.assertEqual(self.snapshot(), before)

    async def test_required_legal_and_decision_fields(self):
        for payload, field in ((self.provider, 'licence'), (self.provider, 'territory_excluded'),
                               (self.decision, 'monetisable'), (self.factory, 'memory'),
                               (self.policy, 'countries')):
            with self.subTest(field=field):
                original = payload.pop(field)
                service = FakeService()
                self.assert_rc(await self.call(service), 2, 'missing_required')
                self.assertEqual(service.calls, 0)
                payload[field] = original

    async def test_total_deadline_covers_streaming_response(self):
        self.request['context']['timeout_s'] = 1
        before = self.sentinel()
        result = await self.call(FakeService(delay=0.05, piece=max(1, len(glb()) // 24)), timeout_s=1)
        self.assert_rc(result, 6, 'deadline_exceeded')
        self.assertEqual(self.snapshot(), before)
        self.assert_rc(await self.call(timeout_s=1), 0)

    async def test_cancel_waits_for_server_quiescence(self):
        service = FakeService(delay=0.05, piece=4096, quiescent=False)
        event = asyncio.Event()
        before = self.sentinel()
        job = asyncio.create_task(self.call(service, cancel_event=event))
        await service.started.wait()
        event.set()
        result = await job
        self.assert_rc(result, 3, 'cancelled')
        self.assertTrue(service.busy)
        self.assertIn('service_quiescence', result['absence_reasons'])
        self.assertEqual(service.cancels, 1)
        self.assertEqual(self.snapshot(), before)
        # No adapter API claims scheduler lease release, even on an acknowledged cancel.
        service = FakeService(delay=0.05, piece=4096, quiescent=True)
        event = asyncio.Event()
        job = asyncio.create_task(self.call(service, cancel_event=event))
        await service.started.wait()
        event.set()
        result = await job
        self.assert_rc(result, 3, 'cancelled')
        self.assertFalse(service.busy)
        self.assertNotIn('service_quiescence', result['absence_reasons'])
        self.assertFalse(self.output.exists())

    async def test_cancel_ack_is_generation_bound(self):
        service = FakeService(delay=0.05, piece=4096, bad_ack=True)
        event = asyncio.Event()
        job = asyncio.create_task(self.call(service, cancel_event=event))
        await service.started.wait()
        event.set()
        result = await job
        self.assert_rc(result, 3, 'cancelled')
        self.assertIn('service_quiescence', result['absence_reasons'])

    async def test_task_cancel_and_cleanup_deadline(self):
        service = FakeService(delay=0.05, piece=4096, cancel_delay=10)
        job = asyncio.create_task(self.call(service))
        await service.started.wait()
        job.cancel()
        result = await job
        self.assert_rc(result, 3, 'cancelled')
        self.assertIn('service_quiescence', result['absence_reasons'])
        self.assertFalse(self.output.exists())

    async def test_pre_cancel_never_calls_service(self):
        event = asyncio.Event()
        event.set()
        service = FakeService()
        self.assert_rc(await self.call(service, cancel_event=event), 3, 'cancelled')
        self.assertEqual(service.calls, 0)

    async def test_tampered_receipt_or_glb_is_stale(self):
        before = self.sentinel()
        for service, code in ((FakeService(provider_hash='f' * 64), 'service_provider_stale'),
                              (FakeService(output_hash='f' * 64), 'output_stale')):
            self.assert_rc(await self.call(service), 4, code)
            self.assertEqual(self.snapshot(), before)
        self.decision['provider_sha256'] = 'f' * 64
        service = FakeService()
        self.assert_rc(await self.call(service), 4, 'decision_stale')
        self.assertEqual(service.calls, 0)

    async def test_current_config_and_policy_bindings(self):
        for key in ('configuration_sha256', 'policy_sha256'):
            old = self.decision[key]
            self.decision[key] = 'f' * 64
            service = FakeService()
            self.assert_rc(await self.call(service), 4, 'decision_stale')
            self.assertEqual(service.calls, 0)
            self.decision[key] = old

    async def test_licence_refusal_never_calls_service(self):
        self.decision.update(rc=5, verdict='FAIL', allowed=False)
        service = FakeService()
        self.assert_rc(await self.call(service), 5, 'licence_refused')
        self.assertEqual(service.calls, 0)
        self.assertFalse(self.output.exists())

    async def test_service_status_boundary_and_foreign_rc_one(self):
        before = self.sentinel()
        for rc in (2, 3, 4, 5, 6):
            self.assert_rc(await self.call(FakeService(response_rc=rc)), rc, 'service_refused')
            self.assertEqual(self.snapshot(), before)
        # F0 only declares foreign-1 translation in U5/U6; adapter must reject it.
        self.assert_rc(await self.call(FakeService(response_rc=1)), 2, 'service_protocol')
        self.assert_rc(await self.call(FakeService(response_rc=True)), 2, 'service_protocol')

    async def test_raw_generation_has_no_visual_verdict(self):
        result = await self.call()
        self.assert_rc(result, 0)
        self.assertNotIn('PASS', a.core.canonical_json(result))
        self.assertEqual(result['receipts'][0]['schema'], 'af.generate/v1')
        self.assertFalse(any(ref['schema'].startswith('vqa') for ref in result['receipts']))
        # Raw generation remains ungraded; U5 owns missing/SKIP/UNCLEAR trace grading.

    async def test_missing_service_peak_stays_null_and_shipped_peak_is_bound(self):
        result = await self.call()
        self.assertIsNone(result['metrics']['peak_rss_bytes'])
        self.assertEqual(result['absence_reasons']['peak_rss_bytes'], 'service_peak_unmeasured')
        manifest = json.loads((ROOT / 'providers/trellis2/provider.json').read_bytes())
        reference = manifest['runtime']['ram_peak_measurement_ref']
        measurement_path = ROOT / 'providers/trellis2' / reference['path']
        self.assertEqual(a.core.sha256_file(measurement_path), reference['sha256'])
        measurement = a.core.load_json(measurement_path, max_bytes=100000)
        self.assertEqual(manifest['runtime']['ram_peak_gb_measured'],
                         (measurement['peak_cgroup_bytes'] + 2**30 - 1) // 2**30)
        self.assertGreater(manifest['runtime']['ram_peak_gb_measured'], 0)
        self.assertTrue(manifest['runnable'])
        self.assertTrue(manifest['weights'])
        self.assertIn('af44b45f2e35a493886929c6d786e563ec68364d', manifest['revision'])
        self.assertEqual((ROOT / 'providers/trellis2/provider.json').read_text(), a.core.canonical_json(manifest))

    async def test_invalid_telemetry_refuses(self):
        for peak in (True, -1, 0, 24.0, '24'):
            self.assert_rc(await self.call(FakeService(peak=peak)), 2, 'telemetry_invalid')
        self.assert_rc(await self.call(FakeService(peak=1024)), 0)

    async def test_geometry_only_raw_capability(self):
        before = self.sentinel()
        for service in (FakeService(geometry_only=False), FakeService(raw_preserved=False)):
            self.assert_rc(await self.call(service), 5, 'capability_refused')
            self.assertEqual(self.snapshot(), before)

    async def test_invalid_input_params_pin_and_context_never_call_service(self):
        cases = [(lambda: setattr(self, 'image', b''), 2, 'image_invalid'),
                 (lambda: setattr(self, 'image', 'base64'), 2, 'image_invalid'),
                 (lambda: self.request['params'].update(seed=True), 2, 'params_invalid'),
                 (lambda: self.request['context'].update(commercial_use=None), 2, 'context_invalid'),
                 (lambda: self.provider.update(revision='main'), 2, 'unpinned_revision'),
                 (lambda: self.request['inputs'][0].update(sha256='f' * 64), 4, 'input_stale'),
                 (lambda: self.provider.update(runnable=False), 5, 'provider_blocked'),
                 (lambda: self.provider.update(weights=[]), 3, 'weights_inventory_missing')]
        for change, expected, code in cases:
            with self.subTest(expected=expected, change=change):
                image, request, provider = self.image, deepcopy(self.request), deepcopy(self.provider)
                change()
                service = FakeService()
                self.assert_rc(await self.call(service), expected, code)
                self.assertEqual(service.calls, 0)
                self.image, self.request, self.provider = image, request, provider

    async def test_timeout_and_protocol_arguments_refuse(self):
        for timeout in (0, True, -1, 1801, 1.5):
            service = FakeService()
            self.assert_rc(await self.call(service, timeout_s=timeout), 2, 'timeout_invalid')
            self.assertEqual(service.calls, 0)

    async def test_unavailable_service_cannot_see(self):
        result = await a.generate(self.image, seed=17, pipeline_type='1024', output_dir=self.output,
                                  request=self.request, provider=self.provider, decision=self.decision,
                                  factory=self.factory, policy=self.policy)
        self.assert_rc(result, 3, 'service_transport_absent')
        self.assertFalse(self.output.exists())

    async def test_existing_output_symlink_and_lock_preserve_bytes(self):
        self.output.mkdir()
        (self.output / 'raw.glb').write_bytes(b'old raw')
        before = self.snapshot()
        service = FakeService()
        self.assert_rc(await self.call(service), 5, 'output_exists')
        self.assertEqual(service.calls, 0)
        self.assertEqual(self.snapshot(), before)
        (self.output / 'raw.glb').unlink()
        self.output.rmdir()
        self.output.symlink_to(self.parent / 'other', target_is_directory=True)
        self.assert_rc(await self.call(), 2, 'path_escape')
        self.output.unlink()
        lock = self.parent / '.generation.lock'
        lock.write_bytes(b'another owner')
        self.assert_rc(await self.call(), 2, 'publication_locked')
        self.assertEqual(lock.read_bytes(), b'another owner')
        lock.unlink()
        self.assert_rc(await self.call(), 0)

    async def test_publication_failures_leave_no_committed_generation(self):
        before = self.sentinel()
        original = a.core.atomic_write_json
        def fail_result(path, value):
            if path.name == 'result.json':
                raise OSError('injected before commit')
            original(path, value)
        for target, replacement in (('core.atomic_write_json', fail_result),
                                     ('os.replace', lambda *args: (_ for _ in ()).throw(OSError('rename')))):
            with self.subTest(target=target), patch.object(a.core, 'atomic_write_json', fail_result) if target.startswith('core') else patch.object(a.os, 'replace', replacement):
                self.assert_rc(await self.call(), 2, 'publication_io')
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.output.exists())

    async def test_staged_corruption_is_stale(self):
        original = a._sync_file
        def corrupt(path, data):
            original(path, data[:-1])
        with patch.object(a, '_sync_file', corrupt):
            self.assert_rc(await self.call(), 4, 'staged_bytes_stale')
        self.assertFalse(self.output.exists())

    async def test_staged_request_and_result_corruption_is_stale(self):
        before = self.sentinel()
        original = a.core.atomic_write_json
        for name in ('request.json', 'result.json'):
            def corrupt(path, obj):
                original(path, obj)
                if path.name == name:
                    path.write_bytes(b'{}\n')
            with self.subTest(name=name), patch.object(a.core, 'atomic_write_json', corrupt):
                self.assert_rc(await self.call(), 4, 'staged_bytes_stale')
                self.assertFalse(self.output.exists())
                self.assertEqual(self.snapshot(), before)

    async def test_malformed_identity_uses_kernel_error(self):
        self.request['request_id'] = None
        result = await self.call()
        self.assertEqual(result['rc'], 2)
        self.assertNotIn('schema', result)
        self.assertIn('reason_codes', result)
        self.assertFalse(self.output.exists())

    async def test_output_budget_refuses(self):
        before = self.sentinel()
        with patch.object(a, 'MAX_GLB_BYTES', len(glb()) - 1):
            self.assert_rc(await self.call(), 6, 'output_budget_exceeded')
        self.assertEqual(self.snapshot(), before)


class MutationTests(unittest.TestCase):
    def test_negative_control_rejects_weakened_validator(self):
        # Each original oracle is rerun against a temporarily broken rule.
        # Only assertion failures count as killed mutants; errors do not.
        initial_rules = {name: getattr(a, name) for name in ('_wait', '_check_cancel', '_check_deadline')}
        def bypass(*codes):
            original = a._require
            def require(condition, rc, code):
                if code not in codes:
                    original(condition, rc, code)
            return patch.object(a, '_require', require)
        @contextmanager
        def replacements(**kwargs):
            with ExitStack() as stack:
                for name, replacement in kwargs.items():
                    stack.enter_context(patch.object(a, name, replacement))
                yield stack
        original_result = a._result
        def fake_peak(*args, **kwargs):
            result = original_result(*args, **kwargs)
            result['metrics']['peak_rss_bytes'] = 24
            return result
        def fake_approval(*args, **kwargs):
            result = original_result(*args, **kwargs)
            result['verdict'] = 'PASS'
            return result
        def torn_publish(target, data, *args):
            target.mkdir()
            (target / 'raw.glb').write_bytes(data)
            raise OSError('injected torn publication')
        mutations = [
            ('GLB validator bypass', 'test_malformed_response_preserves_caller_bytes',
             patch.object(a, 'validate_glb', lambda data, **kwargs: None)),
            ('nonfinite position bypass', 'test_nonfinite_position_refuses',
              bypass('glb_nonfinite_position')),
            ('GLB buffer-view guard bypass', 'test_glb_mesh_and_buffer_bounds',
             bypass('glb_view_invalid')),
            ('effective request bypass', 'test_parameter_echo_drift_refuses',
             bypass('effective_request_stale')),
            ('toolchain echo bypass', 'test_other_bound_echo_drift_refuses',
             bypass('effective_request_stale')),
            ('licence validation bypass', 'test_licence_refusal_never_calls_service',
             bypass('licence_refused')),
            ('absolute deadline removed', 'test_total_deadline_covers_streaming_response',
             replacements(_check_deadline=lambda *args: None, _wait=unbounded_wait)),
            ('cancellation ignored', 'test_cancel_waits_for_server_quiescence',
             replacements(_check_cancel=lambda *args: None, _wait=unbounded_wait)),
            ('quiescence forged', 'test_cancel_waits_for_server_quiescence',
             patch.object(a, '_cancel_inference', async_true)),
            ('ack binding ignored', 'test_cancel_ack_is_generation_bound',
             patch.object(a, '_cancel_inference', async_true)),
            ('output binding bypass', 'test_tampered_receipt_or_glb_is_stale',
             bypass('service_provider_stale', 'output_stale', 'decision_stale')),
            ('current policy bypass', 'test_current_config_and_policy_bindings',
             bypass('decision_stale')),
            ('service rc ignored', 'test_service_status_boundary_and_foreign_rc_one',
             patch.object(a, '_validate_response', lambda *args: None)),
             ('false appearance approval', 'test_raw_generation_has_no_visual_verdict',
             patch.object(a, '_result', fake_approval)),
             ('unmeasured peak replaced', 'test_missing_service_peak_stays_null_and_shipped_peak_is_bound',
             patch.object(a, '_result', fake_peak)),
            ('invalid telemetry allowed', 'test_invalid_telemetry_refuses',
             bypass('telemetry_invalid')),
            ('geometry raw capabilities ignored', 'test_geometry_only_raw_capability',
             bypass('capability_refused')),
            ('input validation bypass', 'test_invalid_input_params_pin_and_context_never_call_service',
             patch.object(a, '_validate_call', lambda *args: None)),
            ('timeout usage validation bypass', 'test_timeout_and_protocol_arguments_refuse',
             patch.object(a, '_validate_call', lambda *args: None)),
            ('partial publication allowed', 'test_publication_failures_leave_no_committed_generation',
             patch.object(a, '_publish', torn_publish)),
            ('required schema keys ignored', 'test_required_legal_and_decision_fields',
             patch.object(a.core, 'validate', lambda *args: None)),
            ('pre-cancellation ignored', 'test_pre_cancel_never_calls_service',
             patch.object(a, '_check_cancel', lambda *args: None)),
            ('cleanup timeout falsely acknowledged', 'test_task_cancel_and_cleanup_deadline',
             patch.object(a, '_cancel_inference', async_true)),
            ('output destination gate removed', 'test_existing_output_symlink_and_lock_preserve_bytes',
             patch.object(a, '_destination', lambda path: Path(path))),
            ('missing transport semantics removed', 'test_unavailable_service_cannot_see',
             bypass('service_transport_absent')),
            ('request and result staging bypass', 'test_staged_request_and_result_corruption_is_stale',
             bypass('staged_bytes_stale')),
            ('staged digest bypass', 'test_staged_corruption_is_stale',
             bypass('staged_bytes_stale')),
            ('output budget bypass', 'test_output_budget_refuses',
             bypass('output_budget_exceeded')),
        ]
        for name, test_name, mutation in mutations:
            with self.subTest(mutation=name), mutation:
                result = unittest.TestResult()
                AdapterTests(test_name).run(result)
                self.assertGreater(len(result.failures), 0, name)
                self.assertEqual(result.errors, [], name)
                print('NEGATIVE_CONTROL', name, 'original_oracle=FAIL', 'failures=' + str(len(result.failures)), 'errors=0')
        for name, rule in initial_rules.items():
            self.assertIs(getattr(a, name), rule, 'mutant leaked: ' + name)


async def async_true(*args):
    return True


async def unbounded_wait(awaitable, *args):
    return await awaitable


if __name__ == '__main__':
    unittest.main()
