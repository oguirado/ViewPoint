"""Adversarial-review regressions; all assets, services and peaks are synthetic."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import struct
import threading
import unittest
from unittest.mock import patch

import test_f1u2_adapter as original

a = original.a


def indexed_glb(*, indices=(0, 1, 2), component=5123, stride=None):
    doc = json.loads(original.glb()[20:20 + struct.unpack_from('<I', original.glb(), 12)[0]])
    fmt, size = {5121: ('B', 1), 5123: ('H', 2), 5125: ('I', 4)}[component]
    step = stride or size
    binary = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
    binary += b''.join(struct.pack('<' + fmt, n) + b'\0' * (step - size) for n in indices)
    doc['buffers'][0]['byteLength'] = len(binary)
    view = {'buffer': 0, 'byteOffset': 36, 'byteLength': len(binary) - 36}
    if stride:
        view['byteStride'] = stride
    doc['bufferViews'].append(view)
    doc['accessors'].append({'bufferView': 1, 'componentType': component, 'count': 3, 'type': 'SCALAR'})
    doc['meshes'][0]['primitives'][0]['indices'] = 1
    return original.glb(document=doc, binary=binary)


class RepairTests(unittest.IsolatedAsyncioTestCase):
    setUp = original.AdapterTests.setUp
    call = original.AdapterTests.call
    assert_rc = original.AdapterTests.assert_rc
    snapshot = original.AdapterTests.snapshot
    sentinel = original.AdapterTests.sentinel

    async def test_f01_unproven_export_dependencies_cover_generate(self):
        provider = json.loads((original.ROOT / 'providers/trellis2/provider.json').read_bytes())
        for dep in provider['licence']['deps']:
            if dep['name'] in ('nvdiffrast', 'nvdiffrec'):
                self.assertIn('generate', dep['used_by'])
        self.assertFalse(provider['runnable'])
        self.assertIn('geometry-only', ' '.join(provider['known_issues']))
        self.assertIn('DINOv3', ' '.join(provider['known_issues']))

    async def test_f02_response_evidence_is_persisted_and_hash_bound(self):
        result = await self.call(original.FakeService(peak=12345))
        self.assert_rc(result, 0)
        evidence_refs = [ref for ref in result['receipts'] if ref['path'] != 'request.json']
        self.assertEqual(len(evidence_refs), 1, 'request echo alone is not service evidence')
        ref = evidence_refs[0]
        evidence_path = self.output / ref['path']
        self.assertEqual(a.core.sha256_file(evidence_path), ref['sha256'])
        evidence = json.loads(evidence_path.read_bytes())
        self.assertEqual(evidence['request'], self.request)
        self.assertEqual(evidence['provider_sha256'], a.core.sha256_json(self.provider))
        self.assertEqual(evidence['output_sha256'], original.digest(original.glb()))
        self.assertIs(evidence['geometry_only'], True)
        self.assertIs(evidence['raw_preserved'], True)
        self.assertEqual(evidence['service_peak_rss_bytes'], 12345)
        self.assertNotIn('verdict', evidence)

    async def test_f03_foreign_lock_has_explicit_reason_and_no_service_call(self):
        lock = self.parent / '.generation.lock'
        lock.write_bytes(b'foreign owner')
        service = original.FakeService()
        result = await self.call(service)
        self.assert_rc(result, 2)  # Preserve the pre-existing rc oracle.
        self.assertEqual(result['absence_reasons']['outputs'], 'publication_locked')
        self.assertEqual(service.calls, 0)
        self.assertEqual(lock.read_bytes(), b'foreign owner')

    async def test_f03_required_keys_are_contract_errors_not_crashes(self):
        for payload, field in ((self.provider, 'licence'), (self.provider, 'territory_excluded'),
                               (self.decision, 'monetisable'), (self.factory, 'memory'),
                               (self.policy, 'countries')):
            value = payload.pop(field)
            try:
                result = await self.call()
                self.assert_rc(result, 2)
                self.assertEqual(result['absence_reasons']['outputs'], 'missing_required')
            finally:
                payload[field] = value

    async def test_f04_commit_rename_failure_cleans_stage_and_lock(self):
        before = self.sentinel()
        commit = getattr(a, '_commit_generation', a.os.replace)
        owner = a if hasattr(a, '_commit_generation') else a.os
        commit_name = '_commit_generation' if owner is a else 'replace'
        commit_calls = []

        def fail_commit(src, dest):
            if Path(dest) == self.output:
                commit_calls.append((Path(src), Path(dest)))
                self.assertTrue((Path(src) / 'result.json').is_file())
                raise OSError('injected commit rename failure')
            return commit(src, dest)

        with patch.object(owner, commit_name, fail_commit):
            result = await self.call()
        self.assert_rc(result, 2)
        self.assertEqual(len(commit_calls), 1)
        self.assertFalse(commit_calls[0][0].exists())
        self.assertFalse((self.parent / '.generation.lock').exists())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(p.name for p in self.parent.iterdir()), ['caller.glb', 'caller.json'])

    async def test_f05_indexed_positions_and_stride_have_positive_coverage(self):
        for component in (5121, 5123, 5125):
            for stride in (None, 4):
                with self.subTest(component=component, stride=stride):
                    a.validate_glb(indexed_glb(component=component, stride=stride))

    async def test_f05_out_of_range_index_has_exact_refusal(self):
        result = await self.call(original.FakeService(body=indexed_glb(indices=(0, 1, 3))))
        self.assert_rc(result, 2)
        self.assertEqual(result['absence_reasons']['outputs'], 'glb_index_bounds')

    async def test_f05_invalid_index_accessor_has_exact_refusal(self):
        body = indexed_glb()
        doc = json.loads(body[20:20 + struct.unpack_from('<I', body, 12)[0]])
        doc['meshes'][0]['primitives'][0]['indices'] = True
        result = await self.call(original.FakeService(body=original.glb(document=doc,
                               binary=struct.pack('<9f3H', 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 2))))
        self.assert_rc(result, 2)
        self.assertEqual(result['absence_reasons']['outputs'], 'glb_indices_invalid')

    async def test_f05_stride_alignment_has_exact_refusal(self):
        doc = json.loads(original.glb()[20:20 + struct.unpack_from('<I', original.glb(), 12)[0]])
        binary = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0) + b'\0' * 8
        doc['buffers'][0]['byteLength'] = len(binary)
        doc['bufferViews'][0].update(byteLength=len(binary), byteStride=13)
        result = await self.call(original.FakeService(body=original.glb(document=doc, binary=binary)))
        self.assert_rc(result, 2)
        self.assertEqual(result['absence_reasons']['outputs'], 'glb_accessor_bounds')

    async def test_f06_provider_peak_requires_measurement_reference(self):
        self.provider['runtime']['ram_peak_gb_measured'] = 24
        self.decision['provider_sha256'] = a.core.sha256_json(self.provider)
        service = original.FakeService()
        result = await self.call(service)
        self.assert_rc(result, 2)
        self.assertEqual(result['absence_reasons']['outputs'], 'peak_measurement_invalid')
        self.assertEqual(service.calls, 0)

    async def test_f07_cancellation_drains_inner_task(self):
        entered, drained, event = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Service(original.FakeService):
            async def generate(inner, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.01)
                    drained.set()

        job = asyncio.create_task(self.call(Service(), cancel_event=event))
        await asyncio.wait_for(entered.wait(), 1)
        event.set()
        result = await job
        self.assert_rc(result, 3)
        self.assertTrue(drained.is_set(), 'transport finally must finish before return')

    async def test_f07_refusal_closes_response_stream(self):
        closed = asyncio.Event()

        class Service(original.FakeService):
            async def generate(inner, *args, **kwargs):
                response = await super().generate(*args, **kwargs)

                async def chunks():
                    try:
                        yield b''
                    finally:
                        closed.set()

                response.chunks = chunks()
                return response

        result = await self.call(Service())
        self.assert_rc(result, 2)
        self.assertTrue(closed.is_set(), 'stream must close before refusal returns')

    async def test_f07_structured_cancellation_clears_task_counter(self):
        service = original.FakeService(delay=1)
        job = asyncio.create_task(self.call(service))
        await asyncio.wait_for(service.started.wait(), 1)
        job.cancel()
        self.assert_rc(await job, 3)
        self.assertEqual(job.cancelling(), 0)

    async def test_f08_validation_keeps_event_loop_responsive(self):
        entered, released = threading.Event(), threading.Event()
        validator = a.validate_glb
        heartbeat_seen = []

        def blocking_validator(*args, **kwargs):
            entered.set()
            heartbeat_seen.append(released.wait(0.2))
            validator(*args, **kwargs)

        async def heartbeat():
            while not entered.is_set():
                await asyncio.sleep(0)
            released.set()

        ticker = asyncio.create_task(heartbeat())
        try:
            with patch.object(a, 'validate_glb', blocking_validator):
                self.assert_rc(await self.call(), 0)
            self.assertEqual(heartbeat_seen, [True], 'validation blocked scheduler heartbeat')
        finally:
            released.set()
            await ticker

    async def test_f09_unparseable_identity_raises_library_contract_error(self):
        self.request['request_id'] = None
        with self.assertRaises(a.core.ContractError):
            await a.generate(self.image, seed=17, pipeline_type='1024', output_dir=self.output,
                             request=self.request, provider=self.provider, decision=self.decision,
                             factory=self.factory, policy=self.policy)

    async def test_f10_destination_lock_precedes_inference(self):
        first = original.FakeService(delay=0.05)
        job = asyncio.create_task(self.call(first))
        await asyncio.wait_for(first.started.wait(), 1)
        second = original.FakeService()
        try:
            result = await self.call(second)
            self.assert_rc(result, 2)
            self.assertEqual(result['absence_reasons']['outputs'], 'publication_locked')
            self.assertEqual(second.calls, 0)
        finally:
            await job

    async def test_f10_concurrent_empty_directory_is_never_replaced(self):
        commit = getattr(a, '_commit_generation', a.os.replace)
        owner = a if hasattr(a, '_commit_generation') else a.os
        name = '_commit_generation' if owner is a else 'replace'
        caller_inode = []

        def race(src, dest):
            if Path(dest) == self.output:
                self.output.mkdir()
                caller_inode.append(self.output.stat().st_ino)
            return commit(src, dest)

        with patch.object(owner, name, race):
            result = await self.call()
        self.assert_rc(result, 5, 'output_exists')
        self.assertEqual(self.output.stat().st_ino, caller_inode[0])
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse((self.parent / '.generation.lock').exists())
        self.assertEqual(sorted(p.name for p in self.parent.iterdir()), ['generation'])

    async def test_f10_publication_fsyncs_parent_directory(self):
        sync = a.os.fsync
        parent_inode = self.parent.stat().st_ino
        parent_syncs = []

        def observe(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino == parent_inode:
                parent_syncs.append(fd)
            return sync(fd)

        with patch.object(a.os, 'fsync', observe):
            self.assert_rc(await self.call(), 0)
        self.assertTrue(parent_syncs, 'commit must sync its containing directory')

    async def test_f10_missing_atomic_no_replace_support_refuses(self):
        before = self.sentinel()
        with patch.object(a.ctypes, 'CDLL', return_value=object()):
            result = await self.call()
        self.assert_rc(result, 3, 'publication_noreplace_unavailable')
        self.assertEqual(self.snapshot(), before)

    async def test_f11_unreferenced_service_peak_is_not_a_measured_metric(self):
        class Service(original.FakeService):
            async def generate(inner, *args, **kwargs):
                response = await super().generate(*args, **kwargs)
                response.peak_measurement_ref = None
                return response

        result = await self.call(Service(peak=12345))
        self.assert_rc(result, 0)
        self.assertIsNone(result['metrics']['peak_rss_bytes'])
        self.assertEqual(result['absence_reasons']['peak_rss_bytes'], 'service_self_reported')


class IndividualMutationTests(unittest.TestCase):
    def test_f10_overwriting_commit_mutant_goes_red(self):
        result = unittest.TestResult()
        with patch.object(a, '_commit_generation', a.os.replace):
            RepairTests('test_f10_concurrent_empty_directory_is_never_replaced').run(result)
        self.assertGreater(len(result.failures), 0)
        self.assertEqual(result.errors, [])
        print('NEGATIVE_CONTROL overwriting_commit new_oracle=FAIL errors=0')

    def test_f05_index_bounds_rule_mutant_goes_red(self):
        require = a._require
        cases = [('glb_index_bounds', 'test_f05_out_of_range_index_has_exact_refusal'),
                 ('glb_indices_invalid', 'test_f05_invalid_index_accessor_has_exact_refusal'),
                 ('glb_accessor_bounds', 'test_f05_stride_alignment_has_exact_refusal')]
        for rule, test in cases:
            def bypass(condition, rc, code):
                if code != rule:
                    require(condition, rc, code)
            result = unittest.TestResult()
            with self.subTest(rule=rule), patch.object(a, '_require', bypass):
                RepairTests(test).run(result)
            self.assertGreater(len(result.failures), 0)
            self.assertEqual(result.errors, [])
            print('NEGATIVE_CONTROL', rule, 'new_oracle=FAIL', 'errors=0')
