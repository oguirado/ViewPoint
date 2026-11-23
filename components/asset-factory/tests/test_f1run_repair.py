"""Adversarial TRELLIS regressions. Synthetic dependencies never qualify CUDA."""
import asyncio
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from asset_factory import core, proc
from providers.trellis2 import adapter, runtime
from providers.trellis2.build import build
import test_f1u2_adapter as fixtures


def local_fixture(root):
    names = ('sparse_structure_flow_model', 'sparse_structure_decoder',
             'shape_slat_decoder', 'shape_slat_flow_model_512')
    config = {'args': {'models': {name: name for name in names}}}
    (root / 'pipeline.json').write_text(json.dumps(config))
    for name in names:
        (root / (name + '.json')).write_text('{}')
        (root / (name + '.safetensors')).write_bytes(b'pinned fixture')
    for name in ('config.json', 'preprocessor_config.json'):
        (root / name).write_text('{}')
    (root / 'model.safetensors').write_bytes(b'dino fixture')
    provider = json.loads((Path(runtime.__file__).parent / 'provider.json').read_text())
    files = [{'path': p.name, 'sha256': core.sha256_file(p)} for p in root.iterdir() if p.is_file()]
    for weight in provider['weights']:
        weight['files'] = deepcopy(files)
    manifest = root / 'provider.json'
    manifest.write_text(core.canonical_json(provider))
    return manifest, provider


class WeightTests(unittest.TestCase):
    def test_f1_bare_prefix_symlink_cannot_redirect_authenticated_suffixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'model'; root.mkdir(); manifest, _ = local_fixture(root)
            (root / 'shape_slat_decoder').symlink_to(root.parent / 'outside')
            with patch.object(runtime, 'PROVIDER_PATH', manifest, create=True):
                with self.assertRaises(adapter.Refusal) as caught:
                    runtime.local_plan(root, root, '512')
            self.assertEqual((caught.exception.rc, caught.exception.code), (2, 'weight_path_escape'))

    def test_f1_changed_checkpoint_refuses_before_model_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manifest, _ = local_fixture(root)
            with patch.object(runtime, 'PROVIDER_PATH', manifest, create=True):
                runtime.local_plan(root, root, '512')
                (root / 'shape_slat_decoder.safetensors').write_bytes(b'replaced')
                with self.assertRaises(adapter.Refusal) as caught:
                    runtime.local_plan(root, root, '512')
            self.assertEqual((caught.exception.rc, caught.exception.code), (4, 'weight_stale'))

    def test_f1_pipeline_symlink_escape_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'model'; root.mkdir(); manifest, _ = local_fixture(root)
            outside = root.parent / 'outside.json'; outside.write_bytes((root / 'pipeline.json').read_bytes())
            (root / 'pipeline.json').unlink(); (root / 'pipeline.json').symlink_to(outside)
            with patch.object(runtime, 'PROVIDER_PATH', manifest, create=True):
                with self.assertRaises(adapter.Refusal) as caught:
                    runtime.local_plan(root, root, '512')
            self.assertEqual((caught.exception.rc, caught.exception.code), (2, 'weight_path_escape'))

    def test_f1_duplicate_config_refuses_even_with_matching_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manifest, provider = local_fixture(root)
            path = root / 'pipeline.json'
            path.write_text('{"args":null,' + path.read_text()[1:])
            for weight in provider['weights']:
                for item in weight['files']:
                    if item['path'] == path.name: item['sha256'] = core.sha256_file(path)
            manifest.write_text(core.canonical_json(provider))
            with patch.object(runtime, 'PROVIDER_PATH', manifest, create=True):
                with self.assertRaises(adapter.Refusal) as caught:
                    runtime.local_plan(root, root, '512')
            self.assertEqual((caught.exception.rc, caught.exception.code), (2, 'weight_config_invalid'))

    def test_f1_dino_symlink_escape_refuses_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'model'; root.mkdir(); manifest, _ = local_fixture(root)
            outside = root.parent / 'outside'; outside.write_bytes(b'dino fixture')
            (root / 'model.safetensors').unlink(); (root / 'model.safetensors').symlink_to(outside)
            with patch.object(runtime, 'PROVIDER_PATH', manifest, create=True):
                with self.assertRaises(adapter.Refusal) as caught:
                    runtime.load_pipeline(root, root, root, '512')
            self.assertEqual((caught.exception.rc, caught.exception.code), (2, 'weight_path_escape'))


class AdapterRepairs(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.case = fixtures.AdapterTests(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    async def test_f2_unmeasured_mode_never_calls_service(self):
        case = self.case
        case.provider = json.loads((Path(runtime.__file__).parent / 'provider.json').read_text())
        case.request['provider_revision'] = case.provider['revision']
        case.decision['provider_sha256'] = core.sha256_json(case.provider)
        service = fixtures.FakeService()
        result = await case.call(service)
        self.assertEqual(result['rc'], 3, result)
        self.assertEqual(result['absence_reasons']['outputs'], 'calibration_unavailable')
        self.assertEqual(service.calls, 0)
        self.assertFalse(case.output.exists())

    async def test_f4_invalid_scene_references_never_publish(self):
        base = fixtures.glb(); size = struct.unpack_from('<I', base, 12)[0]
        original = json.loads(base[20:20 + size])
        mutations = [lambda d: d.update(scene=999), lambda d: d['nodes'][0].update(mesh=999),
                     lambda d: d['scenes'][0].update(nodes=[999]),
                     lambda d: d['nodes'][0].update(children=[0]),
                     lambda d: d['nodes'][0].update(skin=0),
                     lambda d: d.update(materials=[{'pbrMetallicRoughness': {'baseColorTexture': {'index': 999}}}]),
                     lambda d: d['meshes'][0]['primitives'][0]['attributes'].update(NORMAL=999)]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=mutate):
                self.case.output = self.case.parent / ('scene-' + str(index))
                doc = deepcopy(original); mutate(doc)
                result = await self.case.call(fixtures.FakeService(body=fixtures.glb(document=doc)))
                self.assertEqual(result['rc'], 2, result)
                self.assertFalse(self.case.output.exists())

    async def test_f6_unverified_peak_stays_null(self):
        result = await self.case.call(fixtures.FakeService(peak=1))
        self.assertEqual(result['rc'], 0, result)
        self.assertIsNone(result['metrics']['peak_rss_bytes'])
        self.assertEqual(result['absence_reasons']['peak_rss_bytes'], 'service_self_reported')
        evidence = json.loads((self.case.output / 'service-evidence.json').read_text())
        self.assertEqual(evidence['service_peak_rss_bytes'], 1)


class RuntimeRepairs(unittest.TestCase):
    def test_f3_decode_uses_the_hashed_buffer(self):
        from PIL import Image
        # Loading an extension inside patch.dict(sys.modules) removes its
        # Python module on restoration while leaving native state initialized.
        # Keep NumPy loaded before replacing the synthetic Torch dependency.
        import numpy
        class Array:
            def __init__(self, values): self.values = values
            def detach(self): return self
            def cpu(self): return self
            def tolist(self): return self.values
        consumed = []
        class Pipeline:
            def to(self, *args): pass
            def run(self, image, **kwargs):
                consumed.append(image.getpixel((0, 0)))
                return [SimpleNamespace(vertices=Array([[0,0,0],[1,0,0],[0,1,0]]), faces=Array([[0,1,2]]))]
        fake = SimpleNamespace(__version__='synthetic', version=SimpleNamespace(cuda=None),
            cuda=SimpleNamespace(is_available=lambda: True, reset_peak_memory_stats=lambda: None,
            synchronize=lambda: None, max_memory_allocated=lambda: 0, max_memory_reserved=lambda: 0))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); image = root / 'image.png'; Image.new('RGB', (2,2), (255,0,0)).save(image)
            digest = core.sha256_file(image); real_open = Image.open
            def replace(argument, *args, **kwargs):
                Image.new('RGB', (2,2), (0,0,255)).save(image)
                return real_open(argument, *args, **kwargs)
            with patch.dict(sys.modules, {'torch': fake}), patch.object(runtime, 'load_pipeline', return_value=Pipeline()), \
                 patch.object(runtime, '_native_admission', return_value=None, create=True), \
                 patch.object(Image, 'open', side_effect=replace):
                result = runtime.execute(image_path=image, output_dir=root/'out', model_root=root,
                    dino_root=root, decoder_root=root, seed=42, pipeline_type='512')
            self.assertEqual(result['input_sha256'], digest)
            self.assertEqual(consumed, [(255,0,0)])

    def test_f8_no_permit_never_reaches_cuda(self):
        from PIL import Image
        reached = []
        fake = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: reached.append(True)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); image = root/'image.png'; Image.new('RGB', (2,2)).save(image)
            with patch.dict(sys.modules, {'torch': fake}):
                with self.assertRaises(adapter.Refusal) as caught:
                    runtime.execute(image_path=image, output_dir=root/'out', model_root=root,
                        dino_root=root, decoder_root=root, seed=42, pipeline_type='512')
            self.assertEqual((caught.exception.rc, caught.exception.code), (3, 'runtime_admission_unavailable'))
            self.assertEqual(reached, [])


class BuildRepairs(unittest.TestCase):
    def test_f7_racing_directory_preserves_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); target = root/'caller'
            lock = build.read_json(build.HERE/'build-inputs.json')
            lock['wheels'] = []; lock['debs'] = {'builder': [], 'runtime': []}
            real_sync = build.sync_tree; inode = []
            def race(stage):
                real_sync(stage); target.mkdir(); inode.append(target.stat().st_ino)
            # Plant the competitor after the last exists check, immediately at
            # the syscall boundary, for both the old and the repaired commit.
            real_replace = os.replace
            def raced_replace(source, destination):
                target.mkdir(); inode.append(target.stat().st_ino)
                return real_replace(source, destination)
            def raced_commit(stage, output):
                target.mkdir(); inode.append(target.stat().st_ino)
                return build.commit_context_original(stage, output)
            if hasattr(build, 'commit_context'):
                context = patch.object(build, 'commit_context', side_effect=raced_commit)
                build.commit_context_original = build.commit_context
            else:
                context = patch.object(build.os, 'replace', side_effect=raced_replace)
            with patch.object(build, 'check_inputs'), patch.object(build, 'export_sources'), context:
                with self.assertRaises(build.Refusal) as caught:
                    build.prepare(lock, sources_root=root, packages_root=root, output=target)
            self.assertEqual((caught.exception.rc, caught.exception.reason), (2, 'output_exists'))
            self.assertEqual(target.stat().st_ino, inode[0])
            self.assertEqual(list(target.iterdir()), [])

    def test_f5_supervisor_death_releases_detached_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); beat = root/'heartbeat'; marker = root/'pid'
            detached = "import os,pathlib,sys,time;pathlib.Path(sys.argv[2]).write_text(str(os.getpid()));p=pathlib.Path(sys.argv[1]);\nwhile True: p.write_text(str(time.monotonic_ns()));time.sleep(.02)"
            command = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2],sys.argv[3]],start_new_session=True);time.sleep(60)"
            original = build.af_proc.popen; killers = []
            def launch(*args, **kwargs):
                child = original(*args, **kwargs)
                def inject():
                    deadline = time.monotonic()+3
                    while not beat.exists() and time.monotonic()<deadline: time.sleep(.01)
                    if beat.exists(): proc.signal_pid(child, signal.SIGKILL)
                thread = threading.Thread(target=inject); thread.start(); killers.append(thread)
                return child
            try:
                with patch.object(build.af_proc, 'popen', side_effect=launch):
                    with self.assertRaises(build.Refusal):
                        build.run_owned([sys.executable,'-B','-c',command,detached,str(beat),str(marker)], cwd=root, timeout_s=4)
                for thread in killers: thread.join(4)
                self.assertTrue(beat.exists(), 'failure injection did not run')
                before = beat.read_bytes(); time.sleep(.15)
                self.assertEqual(beat.read_bytes(), before, 'detached descendant survived supervisor death')
            finally:
                # In the failing baseline the marker PID belongs to this outer
                # test namespace. Only signal if its exact command still owns it.
                if marker.exists():
                    pid = int(marker.read_text())
                    path = Path('/proc')/str(pid)/'cmdline'
                    self.assertFalse(path.exists(), 'owned detached descendant was not reaped')


if __name__ == '__main__': unittest.main()
