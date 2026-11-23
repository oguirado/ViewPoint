"""Offline loader/export regressions; these tests do not claim CUDA proof."""
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from providers.trellis2 import adapter, runtime


@contextmanager
def pinned_fixture(root):
    provider = json.loads(runtime.PROVIDER_PATH.read_text())
    files = [{'path': str(path.relative_to(root)), 'sha256': adapter.core.sha256_file(path)}
             for path in root.rglob('*') if path.is_file()]
    for weight in provider['weights']:
        weight['files'] = files
    manifest = root / 'fixture-provider.json'
    manifest.write_text(adapter.core.canonical_json(provider))
    with patch.object(runtime, 'PROVIDER_PATH', manifest):
        yield


class RuntimeTests(unittest.TestCase):
    def test_cli_stdout_is_only_canonical_json_with_python_and_native_diagnostics(self):
        import subprocess
        import sys
        code = """import ctypes,os,sys
from providers.trellis2 import runtime

def noisy(**kwargs):
    os.write(1,b'native diagnostic\\n')
    ctypes.CDLL(None).printf(b'buffered native diagnostic')
    print('framework diagnostic')
    return {'rc':0,'status':'OK'}
runtime.execute=noisy
raise SystemExit(runtime.main(['--image','unused','--out','unused','--model-root','unused','--dino-root','unused','--decoder-root','unused','--seed','42','--pipeline-type','512']))
"""
        child=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr)
        self.assertEqual(child.stdout,'{"rc":0,"status":"OK"}\n')
        self.assertIn('native diagnostic',child.stderr)
        self.assertIn('framework diagnostic',child.stderr)
        self.assertIn('buffered native diagnostic',child.stderr)

    def test_framework_version_subclasses_are_normalized_at_metadata_boundary(self):
        from types import SimpleNamespace
        from asset_factory import core
        class FrameworkVersion(str): pass
        fake=SimpleNamespace(__version__=FrameworkVersion('2.9.1+cu129'),version=SimpleNamespace(cuda='12.9'))
        record=runtime.runtime_versions(fake)
        self.assertIs(type(record['torch_version']),str)
        self.assertEqual(record['torch_version'],'2.9.1+cu129')
        core.canonical_json(record)

    def test_export_is_real_dense_indexed_geometry(self):
        data = runtime.encode_glb([(0.,0.,0.), (1.,0.,0.), (0.,1.,0.)], [(0,1,2)])
        adapter.validate_glb(data)
        self.assertEqual(data[:4], b'glTF')

    def test_export_applies_the_source_exporter_gltf_basis(self):
        import struct
        data=runtime.encode_glb([(0.,0.,0.),(1.,0.,0.),(0.,0.,1.)],[(0,1,2)])
        length=struct.unpack_from('<I',data,12)[0]
        document=json.loads(data[20:20+length])
        # Source: TRELLIS.2/o-voxel/o_voxel/postprocess.py:313 (MIT).
        self.assertEqual(document['accessors'][0]['max'],[1.,1.,0.])

    def test_export_refuses_empty_nonfinite_and_invalid_indices(self):
        for vertices, faces in [([], []), ([(float('nan'),0.,0.)]*3, [(0,1,2)]),
                                ([(0.,0.,0.)]*3, [(0,1,3)])]:
            with self.subTest(vertices=vertices, faces=faces):
                with self.assertRaises(adapter.Refusal):
                    runtime.encode_glb(vertices, faces)

    def test_existing_caller_directory_is_preserved_before_any_model_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); target=root/'caller';target.mkdir()
            sentinel=target/'raw.glb';sentinel.write_bytes(b'caller bytes')
            with self.assertRaises(adapter.Refusal) as caught:
                runtime.execute(image_path=root/'missing.png',output_dir=target,
                                model_root=root,dino_root=root,decoder_root=root,
                                seed=42,pipeline_type='512')
            self.assertEqual((caught.exception.rc,caught.exception.code),(5,'output_exists'))
            self.assertEqual(sentinel.read_bytes(),b'caller bytes')

    def test_local_loader_never_falls_back_to_a_remote_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'pipeline.json').write_text(json.dumps({'args':{'models':{
                'sparse_structure_flow_model':'ckpts/flow',
                'sparse_structure_decoder':'microsoft/TRELLIS-image-large/ckpts/decoder',
                'shape_slat_decoder':'ckpts/shape',
                'shape_slat_flow_model_512':'ckpts/shape_flow',
                'tex_slat_decoder':'ckpts/texture'}}}))
            with pinned_fixture(root), self.assertRaises(adapter.Refusal) as caught:
                runtime.local_plan(root, root, '512')
            self.assertEqual((caught.exception.rc,caught.exception.code), (3,'local_weight_missing'))

    def test_local_plan_selects_only_shape_and_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'ckpts').mkdir()
            names=['sparse_structure_flow_model','sparse_structure_decoder','shape_slat_decoder','shape_slat_flow_model_512']
            config={'args':{'models':{key:'ckpts/'+key for key in names}}}
            config['args']['models']['tex_slat_decoder']='remote/forbidden'
            for key in names:
                (root/'ckpts'/f'{key}.json').write_text('{}')
                (root/'ckpts'/f'{key}.safetensors').write_bytes(b'fixture')
            (root/'pipeline.json').write_text(json.dumps(config))
            with pinned_fixture(root):
                _,plan=runtime.local_plan(root,root,'512')
            self.assertEqual(set(plan),set(names))
            config['args']['models']['shape_slat_decoder']='../../outside'
            (root/'pipeline.json').write_text(json.dumps(config))
            with pinned_fixture(root), self.assertRaises(adapter.Refusal) as caught:
                runtime.local_plan(root,root,'512')
            self.assertEqual(caught.exception.rc,2)


if __name__=='__main__': unittest.main()
