"""Offline geometry execution for operator-approved native calibration.

This module runs in the built image with the component and weights mounted
read-only. It does not download, remove backgrounds, texture, repair, normalize
or declare an asset PASS. The normal adapter still owns factory publication.
"""
from __future__ import annotations
import argparse
import contextlib
import ctypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import resource
import struct
import sys
import tempfile
import time

from asset_factory import core
from .adapter import Refusal, MAX_GLB_BYTES, validate_glb, _commit_generation

PROVIDER_PATH = Path(__file__).with_name('provider.json')
MAX_CONFIG_BYTES = 2**20  # DESIGN_CHOICE [TARGET], model JSON only.


def _need(condition, rc, code):
    if not condition:
        raise Refusal(rc, code)


def runtime_versions(torch):
    # PyTorch's TorchVersion subclasses str; the factory deliberately accepts
    # only exact JSON scalar types. Normalize at the external metadata boundary.
    return {'torch_version': str(torch.__version__),
            'cuda_version': None if torch.version.cuda is None else str(torch.version.cuda)}


def _weight_file(root, relative):
    root = Path(root).resolve()
    _need(isinstance(relative, str) and bool(relative) and not Path(relative).is_absolute()
          and '..' not in Path(relative).parts, 2, 'weight_path_invalid')
    path = (root / relative).resolve()
    _need(path.is_relative_to(root), 2, 'weight_path_escape')
    _need(path.is_file(), 3, 'local_weight_missing')
    return path


def _inventory(provider, repo):
    rows = [row for row in provider['weights'] if row['repo'] == repo]
    _need(len(rows) == 1, 3, 'weight_inventory_missing')
    entries = rows[0]['files']
    result = {row['path']: row['sha256'] for row in entries}
    _need(len(entries) == len(result), 2, 'weight_inventory_invalid')
    return result


def _authenticate(root, relative, inventory, *, configuration=False):
    path = _weight_file(root, relative)
    _need(relative in inventory, 4, 'weight_unregistered')
    if configuration:
        with path.open('rb') as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        _need(len(data) <= MAX_CONFIG_BYTES, 6, 'weight_config_budget')
        digest = hashlib.sha256(data).hexdigest()
    else:
        data = None
        digest = core.sha256_file(path)
    _need(digest == inventory[relative], 4, 'weight_stale')
    if not configuration:
        return path
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result: raise ValueError('duplicate key')
            result[key] = value
        return result
    def invalid(_): raise ValueError('nonfinite number')
    try:
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
        _need(isinstance(result, dict), 2, 'weight_config_invalid')
        return result
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refusal(2, 'weight_config_invalid') from exc


def _prefix(root, relative, inventory):
    # Check both paths before hashing either, so escape/missing evidence is
    # diagnosed without loading any checkpoint or importing model code.
    _need(not (Path(root) / relative).is_symlink(), 2, 'weight_path_escape')
    for suffix in ('.json', '.safetensors'):
        _weight_file(root, relative + suffix)
    _authenticate(root, relative + '.json', inventory, configuration=True)
    _authenticate(root, relative + '.safetensors', inventory)
    return str((Path(root) / relative).resolve())


def local_plan(model_root, decoder_root, pipeline_type):
    """Resolve every selected checkpoint before importing/loading any model."""
    _need(pipeline_type in ('512','1024','1024_cascade','1536_cascade'), 2, 'pipeline_type_invalid')
    provider = core.load_json(PROVIDER_PATH, max_bytes=MAX_CONFIG_BYTES)
    model_inventory = _inventory(provider, 'microsoft/TRELLIS.2-4B')
    decoder_inventory = _inventory(provider, 'microsoft/TRELLIS-image-large')
    config = _authenticate(model_root, 'pipeline.json', model_inventory, configuration=True)['args']
    names = ['sparse_structure_flow_model','sparse_structure_decoder','shape_slat_decoder']
    if pipeline_type == '512' or pipeline_type.endswith('_cascade'):
        names.append('shape_slat_flow_model_512')
    if pipeline_type != '512':
        names.append('shape_slat_flow_model_1024')
    plan = {}
    for name in names:
        reference = config['models'][name]
        if name == 'sparse_structure_decoder' and reference.startswith('microsoft/TRELLIS-image-large/'):
            plan[name] = _prefix(decoder_root, reference.removeprefix('microsoft/TRELLIS-image-large/'), decoder_inventory)
        else:
            plan[name] = _prefix(model_root, reference, model_inventory)
    return config, plan


def load_pipeline(model_root, dino_root, decoder_root, pipeline_type):
    config, plan = local_plan(model_root, decoder_root, pipeline_type)
    dino_root = Path(dino_root).resolve()
    provider = core.load_json(PROVIDER_PATH, max_bytes=MAX_CONFIG_BYTES)
    inventory = _inventory(provider, 'camenduru/dinov3-vitl16-pretrain-lvd1689m')
    for name in ('config.json', 'preprocessor_config.json', 'model.safetensors'):
        _authenticate(dino_root, name, inventory, configuration=name.endswith('.json'))
    # Imports and constructors are deliberately after local-input admission.
    from trellis2 import models
    from trellis2.pipelines import samplers
    from trellis2.modules.image_feature_extractor import DinoV3FeatureExtractor
    from trellis2.pipelines.trellis2_image_to_3d import Trellis2GeometryPipeline
    selected = {name: models.from_pretrained(prefix) for name,prefix in plan.items()}
    kwargs = {'models':selected, 'image_cond_model':DinoV3FeatureExtractor(str(dino_root)),
              'rembg_model':None, 'low_vram':True, 'default_pipeline_type':pipeline_type,
              'shape_slat_normalization':config['shape_slat_normalization']}
    for kind in ('sparse_structure','shape_slat'):
        spec = config[kind+'_sampler']
        kwargs[kind+'_sampler'] = getattr(samplers,spec['name'])(**spec['args'])
        kwargs[kind+'_sampler_params'] = dict(spec['params'])
    # Do not call the inherited textured loader: it instantiates briaai/RMBG-2.0
    # even for a shape-only request, and would require an unapproved download.
    return Trellis2GeometryPipeline(**kwargs)


def encode_glb(vertices, faces):
    """Encode unmodified dense triangles; factory validation remains mandatory."""
    _need(len(vertices)>0 and len(faces)>0, 3, 'geometry_empty')
    _need(12*(len(vertices)+len(faces)) < MAX_GLB_BYTES, 6, 'output_budget_exceeded')
    positions=bytearray(); indices=bytearray()
    minimum=[math.inf]*3; maximum=[-math.inf]*3
    for vertex in vertices:
        _need(len(vertex)==3, 2, 'geometry_invalid')
        native=tuple(float(v) for v in vertex)
        # Same rigid basis as upstream o_voxel/postprocess.py:313 (MIT).
        # This is export convention, never geometry simplification/normalization.
        point=(native[0], native[2], -native[1])
        _need(all(math.isfinite(v) for v in point), 2, 'geometry_nonfinite')
        positions.extend(struct.pack('<3f',*point))
        for axis,v in enumerate(point):
            minimum[axis]=min(minimum[axis],v); maximum[axis]=max(maximum[axis],v)
    for face in faces:
        _need(len(face)==3 and all(type(i) is int and 0<=i<len(vertices) for i in face), 2, 'geometry_indices_invalid')
        indices.extend(struct.pack('<3I',*face))
    binary=positions+indices
    document={'asset':{'version':'2.0','generator':'asset-factory TRELLIS.2 geometry-only'},
              'scene':0,'scenes':[{'nodes':[0]}],'nodes':[{'mesh':0,'name':'raw_geometry'}],
              'meshes':[{'name':'raw_geometry','primitives':[{'attributes':{'POSITION':0},'indices':1,'mode':4}]}],
              'buffers':[{'byteLength':len(binary)}],
              'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':len(positions),'target':34962},
                             {'buffer':0,'byteOffset':len(positions),'byteLength':len(indices),'target':34963}],
              'accessors':[{'bufferView':0,'componentType':5126,'count':len(vertices),'type':'VEC3','min':minimum,'max':maximum},
                           {'bufferView':1,'componentType':5125,'count':3*len(faces),'type':'SCALAR'}]}
    encoded=json.dumps(document,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    encoded+=b' '*((-len(encoded))%4)
    data=struct.pack('<4sII',b'glTF',2,12+8+len(encoded)+8+len(binary))
    data+=struct.pack('<I4s',len(encoded),b'JSON')+encoded+struct.pack('<I4s',len(binary),b'BIN\0')+binary
    validate_glb(data)
    return data


def _native_admission(permit_path=None, **workload):
    from .admission import verify
    return verify(permit_path, provider_path=PROVIDER_PATH, **workload)


def execute(*, image_path, output_dir, model_root, dino_root, decoder_root, seed, pipeline_type, permit_path=None):
    target=Path(output_dir).absolute()
    _need(not target.exists() and not target.is_symlink(),5,'output_exists')
    _need(target.parent.is_dir(),2,'output_parent_missing')
    permit = _native_admission(permit_path, image_path=image_path, seed=seed, pipeline_type=pipeline_type)
    with Path(image_path).open('rb') as stream:
        image_data=stream.read(32 * 2**20 + 1)
    _need(0 < len(image_data) <= 32 * 2**20, 6, 'image_budget_exceeded')
    if permit is not None:
        _need(hashlib.sha256(image_data).hexdigest() == permit['input_sha256'], 4, 'runtime_input_stale')
    from PIL import Image
    with Image.open(io.BytesIO(image_data)) as source:
        _need(source.format in ('PNG', 'JPEG') and source.width * source.height <= 4096**2,
              6, 'image_budget_exceeded')
        source.load(); image=source.convert('RGB')
    started=time.monotonic_ns()
    import torch
    _need(torch.cuda.is_available(),3,'gpu_unavailable')
    torch.cuda.reset_peak_memory_stats()
    pipeline=load_pipeline(model_root,dino_root,decoder_root,pipeline_type)
    pipeline.to('cuda')
    meshes=pipeline.run(image, seed=seed, pipeline_type=pipeline_type, preprocess_image=False)
    torch.cuda.synchronize()
    _need(len(meshes)==1,2,'geometry_count_invalid')
    mesh=meshes[0]
    vertices=mesh.vertices.detach().cpu().tolist(); faces=mesh.faces.detach().cpu().tolist()
    raw=encode_glb(vertices,faces)
    for name in ('nvdiffrast','nvdiffrec'):
        _need(not any(key==name or key.startswith(name+'.') for key in sys.modules),5,'restricted_import')
    memory_peak=Path('/sys/fs/cgroup/memory.peak')
    measurement={'rc':0,'status':'OK','seed':seed,'pipeline_type':pipeline_type,
                 'geometry_only':True,'raw_preserved':True,'preprocess_image':False,
                 'input_sha256':hashlib.sha256(image_data).hexdigest(),
                 'output_sha256':hashlib.sha256(raw).hexdigest(),'vertices':len(vertices),'triangles':len(faces),
                 'wall_ms':(time.monotonic_ns()-started)//1000000,
                 'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                 'cgroup_peak_bytes':int(memory_peak.read_text()) if memory_peak.is_file() else None,
                 'cuda_max_memory_allocated_bytes':torch.cuda.max_memory_allocated(),
                 'cuda_max_memory_reserved_bytes':torch.cuda.max_memory_reserved(),
                 **runtime_versions(torch),
                 'coordinate_convention':'native xyz -> glTF (x,z,-y); no scale normalization',
                 'appearance_verdict':'CANNOT_SEE','input_gate_verdict':'CANNOT_SEE',
                 'absence_reasons':{'input_gate':'production calibration policy absent',
                                    'appearance':'not evaluated by geometry execution'}}
    if permit is not None:
        measurement['native_permit_sha256'] = core.sha256_json(permit)
        measurement['runtime_approval_sha256'] = permit['approval_sha256']
    with tempfile.TemporaryDirectory(prefix='.native-',dir=target.parent) as temporary:
        stage=Path(temporary)/'published'; stage.mkdir()
        import numpy as np
        np.savez(stage/'model-native.npz', vertices=np.asarray(vertices,dtype=np.float32),
                 faces=np.asarray(faces,dtype=np.int32))
        measurement['model_native']={'path':'model-native.npz',
                                    'sha256':core.sha256_file(stage/'model-native.npz')}
        with (stage/'raw.glb').open('wb') as stream:
            stream.write(raw);stream.flush()
            import os
            os.fsync(stream.fileno())
        core.atomic_write_json(stage/'runtime-measurement.json',measurement)
        _commit_generation(stage,target)
    return measurement


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('image','out','model-root','dino-root','decoder-root'):
        parser.add_argument('--'+key,required=True,type=Path)
    parser.add_argument('--seed',required=True,type=int)
    parser.add_argument('--pipeline-type',required=True,choices=('512','1024','1024_cascade','1536_cascade'))
    parser.add_argument('--permit',type=Path)
    try:
        args=parser.parse_args(argv)
        # Framework Python and native/C diagnostics both belong on stderr.
        # Preserve stdout as the single canonical stage record for CLI callers.
        sys.stdout.flush()
        libc=ctypes.CDLL(None)
        libc.fflush(None)
        original_stdout=os.dup(1)
        try:
            os.dup2(2,1)
            with contextlib.redirect_stdout(sys.stderr):
                result=execute(image_path=args.image,output_dir=args.out,model_root=args.model_root,dino_root=args.dino_root,
                               decoder_root=args.decoder_root,seed=args.seed,pipeline_type=args.pipeline_type,permit_path=args.permit)
        finally:
            # Native stdio may buffer even newline-terminated messages when
            # stdout is a pipe. Flush before restoring the original descriptor.
            libc.fflush(None)
            os.dup2(original_stdout,1)
            os.close(original_stdout)
    except Refusal as exc:
        result={'rc':exc.rc,'status':core.STATUS[exc.rc],'reason_codes':[exc.code]}
    except SystemExit as exc:
        if exc.code==0:return 0
        result={'rc':2,'status':'ERROR','reason_codes':['usage_invalid']}
    except (OSError,ValueError,KeyError,TypeError,OverflowError) as exc:
        print(type(exc).__name__+': '+str(exc),file=sys.stderr)
        result={'rc':2,'status':'ERROR','reason_codes':['runtime_input_invalid']}
    except Exception as exc:
        print(type(exc).__name__+': '+str(exc),file=sys.stderr)
        result={'rc':5,'status':'FAIL','reason_codes':['native_execution_failed']}
    print(core.canonical_json(result),end='')
    return result['rc']


if __name__=='__main__':raise SystemExit(main())
