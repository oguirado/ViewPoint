"""Lossless static GLB inspection and document partition (no Blender imports).

Public: inspect_glb(source, *, roles), separate_glb(source, output_dir, *, roles,
normalize, timeout_s, metadata=None, required_gates=(), required_lod_levels=None),
verify_bundle(output_dir). Missing requirements refuse; () explicitly declares no LODs.

roles maps decimal glTF node indices to visual|collision|occluder. Every mesh
node must be covered, including inactive nodes. Names are advisory only.
metadata is a source-bound dict with exactly source_sha256, roles_sha256, lods,
and gate_refs. LOD entries are {level,path,sha256}; gate references are
{path,sha256,subject_sha256}. Paths are relative to source.parent. A referenced
gate must use a schema in GATE_SCHEMAS, with matching rc/status/verdict and
source/subject hashes. Evidence is copied into the bundle and rehashed on reads.
Unknown sibling formats refuse; no sibling API is assumed.

Only fresh destination directories are accepted. The original binary buffer,
UVs, embedded images and PBR records are preserved byte-for-byte; the partition
removes opposite-role mesh bindings and prunes the mesh table, retaining empty
ancestor nodes. Unreferenced binary bytes are retained, so this is not a data
redaction tool. No basis conversion, implicit scaling or normalization occurs.
Skins, animation, morph targets, extensions and external resources refuse.
Inline closed record contracts are validated before atomic publication.
"""
import copy
import json
import math
from pathlib import Path
import shutil
import struct
import tempfile
import time

_monotonic = time.monotonic

from tools.blender.helpers_contracts import (
    GlbError, STATUS, VERDICT, bound_path, canonical, closed, digest, integer, json_digest,
    number, parse_json, publish_directory, read_bytes, record, sha, sync_directory, write_synced,
    core, validate_record,
)
from tools.blender.glb_schemas import SEPARATION_SCHEMA, RECEIPT_SCHEMA

JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
SEPARATION_FIELDS = tuple(SEPARATION_SCHEMA['properties'])
RECEIPT_FIELDS = tuple(RECEIPT_SCHEMA['properties'])


def _ref(value, array):
    integer(value)
    if value >= len(array):
        raise GlbError('invalid_reference')
    return value


def decode_glb(data, *, checkpoint=lambda: None):
    try:
        if len(data) < 20:
            raise GlbError('malformed_glb')
        magic, version, total = struct.unpack_from('<4sII', data)
        if magic != b'glTF' or version != 2 or total != len(data):
            raise GlbError('malformed_glb')
        offset, chunks = 12, []
        while offset < total:
            checkpoint()
            if offset + 8 > total:
                raise GlbError('malformed_chunk')
            size, kind = struct.unpack_from('<II', data, offset)
            offset += 8
            if size % 4 or size == 0 or offset + size > total:
                raise GlbError('malformed_chunk')
            chunks.append((kind, data[offset:offset + size]))
            offset += size
        if [k for k, _ in chunks] not in ([JSON_CHUNK], [JSON_CHUNK, BIN_CHUNK]):
            raise GlbError('unsupported_chunks')
        doc = parse_json(chunks[0][1])
        binary = chunks[1][1] if len(chunks) == 2 else b''
        checkpoint()
        validate_document(doc, binary, checkpoint=checkpoint)
        return doc, binary
    except (struct.error, TypeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
        raise GlbError('malformed_glb') from exc


def encode_glb(doc, binary):
    # glTF is a float-bearing external format; never use factory canonical() here.
    data = json.dumps(doc, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')
    data += b' ' * (-len(data) % 4)
    chunks = struct.pack('<II', len(data), JSON_CHUNK) + data
    if binary:
        chunks += struct.pack('<II', len(binary), BIN_CHUNK) + binary
    return struct.pack('<4sII', b'glTF', 2, 12 + len(chunks)) + chunks


def validate_document(doc, binary, *, checkpoint=lambda: None):
    if not isinstance(doc, dict) or not isinstance(doc.get('asset'), dict) or doc['asset'].get('version') != '2.0':
        raise GlbError('unsupported_glb_version')
    for key in ('nodes', 'meshes', 'scenes', 'buffers', 'bufferViews', 'accessors',
                'images', 'textures', 'samplers', 'materials', 'skins', 'animations', 'cameras'):
        if key in doc and (not isinstance(doc[key], list) or
                           any(not isinstance(x, dict) for x in doc[key])):
            raise GlbError('invalid_table')
    if doc.get('skins') or doc.get('animations') or doc.get('cameras'):
        raise GlbError('unsupported_dynamic_or_camera')
    def features(obj):
        checkpoint()
        if isinstance(obj, dict):
            if 'extensions' in obj or obj.get('extensionsUsed') or obj.get('extensionsRequired'):
                raise GlbError('unsupported_extension')
            if 'uri' in obj:
                raise GlbError('external_resource')
            for v in obj.values():
                features(v)
        elif isinstance(obj, list):
            for v in obj:
                features(v)
        elif isinstance(obj, float):
            number(obj)
    features(doc)
    nodes, meshes = doc.get('nodes', []), doc.get('meshes', [])
    if not nodes or not meshes or not doc.get('scenes'):
        raise GlbError('empty_scene', 3)
    if len(nodes) > 4096:  # DESIGN_CHOICE bounded graph work, no inferred default.
        raise GlbError('node_budget', 6)
    parent = {}
    for ni, node in enumerate(nodes):
        checkpoint()
        if 'name' in node and not isinstance(node['name'], str):
            raise GlbError('invalid_node_name')
        if 'skin' in node or 'weights' in node or 'camera' in node:
            raise GlbError('unsupported_dynamic_or_camera')
        if 'mesh' in node:
            _ref(node['mesh'], meshes)
        _local_matrix(node)
        if not isinstance(node.get('children', []), list):
            raise GlbError('invalid_children')
        for child in node.get('children', []):
            _ref(child, nodes)
            if child in parent:
                raise GlbError('ambiguous_parent')
            parent[child] = ni
    # Iterative ancestor walk avoids a recursion limit dependent graph acceptance.
    for ni in range(len(nodes)):
        checkpoint()
        seen, cursor = set(), ni
        while cursor in parent:
            checkpoint()
            if cursor in seen:
                raise GlbError('node_cycle')
            seen.add(cursor)
            cursor = parent[cursor]
    for scene in doc['scenes']:
        roots = scene.get('nodes', [])
        if not isinstance(roots, list) or len(set(roots)) != len(roots):
            raise GlbError('invalid_scene_roots')
        for root in roots:
            _ref(root, nodes)
            if root in parent:
                raise GlbError('nonroot_scene_node')
    _ref(doc.get('scene', 0), doc['scenes'])
    buffers = doc.get('buffers', [])
    if len(buffers) != 1 or 'uri' in buffers[0]:
        raise GlbError('unsupported_buffer')
    size = integer(buffers[0].get('byteLength'))
    if not size or size > len(binary) or len(binary) - size > 3:
        raise GlbError('buffer_bounds')
    views, accessors = doc.get('bufferViews', []), doc.get('accessors', [])
    for view in views:
        checkpoint()
        if view.get('buffer') != 0 or type(view.get('buffer')) is not int:
            raise GlbError('invalid_buffer')
        start, length = integer(view.get('byteOffset', 0)), integer(view.get('byteLength'), 1)
        if start + length > size:
            raise GlbError('buffer_bounds')
        if 'byteStride' in view:
            stride = integer(view['byteStride'], 4)
            if stride > 252 or stride % 4:
                raise GlbError('invalid_stride')
    counts = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}
    widths = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
    for acc in accessors:
        checkpoint()
        if 'sparse' in acc:
            raise GlbError('unsupported_sparse')
        view = views[_ref(acc.get('bufferView'), views)]
        if type(acc.get('componentType')) is not int or acc['componentType'] not in widths or acc.get('type') not in counts:
            raise GlbError('invalid_accessor')
        width = widths[acc['componentType']] * counts[acc['type']]
        count, start = integer(acc.get('count'), 1), integer(acc.get('byteOffset', 0))
        stride = view.get('byteStride', width)
        if stride < width or start + (count - 1) * stride + width > view['byteLength']:
            raise GlbError('accessor_bounds')
    for mesh in meshes:
        checkpoint()
        if 'weights' in mesh or not isinstance(mesh.get('primitives'), list) or not mesh['primitives']:
            raise GlbError('invalid_mesh')
        for prim in mesh['primitives']:
            if not isinstance(prim, dict) or 'targets' in prim:
                raise GlbError('unsupported_morph')
            attrs = prim.get('attributes')
            if not isinstance(attrs, dict) or 'POSITION' not in attrs:
                raise GlbError('invalid_attributes')
            for index in attrs.values():
                _ref(index, accessors)
            if prim.get('mode', 4) != 4 or type(prim.get('mode', 4)) is not int:
                raise GlbError('unsupported_primitive')
            if 'indices' in prim:
                _ref(prim['indices'], accessors)
            if 'material' in prim:
                _ref(prim['material'], doc.get('materials', []))
    for image in doc.get('images', []):
        _ref(image.get('bufferView'), views)
        if image.get('mimeType') not in ('image/png', 'image/jpeg'):
            raise GlbError('unsupported_image')
    for texture in doc.get('textures', []):
        _ref(texture.get('source'), doc.get('images', []))
        if 'sampler' in texture:
            _ref(texture['sampler'], doc.get('samplers', []))
    def texture_refs(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.endswith('Texture'):
                    if not isinstance(v, dict):
                        raise GlbError('invalid_texture')
                    _ref(v.get('index'), doc.get('textures', []))
                texture_refs(v)
        elif isinstance(obj, list):
            for v in obj:
                texture_refs(v)
    texture_refs(doc.get('materials', []))


def _local_matrix(node):
    def vector(key, fallback):
        values = node.get(key, fallback)
        if not isinstance(values, list) or len(values) != len(fallback):
            raise GlbError('invalid_transform')
        return [number(v) for v in values]
    if 'matrix' in node:
        if any(k in node for k in ('translation', 'rotation', 'scale')):
            raise GlbError('ambiguous_transform')
        flat = vector('matrix', [0] * 16)
        if flat[3:16:4] != [0, 0, 0, 1]:
            raise GlbError('nonaffine_transform')
        return [[flat[c * 4 + r] for c in range(4)] for r in range(4)]
    tx, ty, tz = vector('translation', [0, 0, 0])
    x, y, z, w = vector('rotation', [0, 0, 0, 1])
    if abs(x*x + y*y + z*z + w*w - 1) > 0.00001:
        raise GlbError('invalid_quaternion')
    scales = vector('scale', [1, 1, 1])
    rotation = [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]]
    return [[rotation[r][c] * scales[c] for c in range(3)] + [[tx, ty, tz][r]]
            for r in range(3)] + [[0, 0, 0, 1]]


def role_table(doc, roles):
    if not isinstance(roles, dict):
        raise GlbError('invalid_roles')
    converted = {}
    for key, role in roles.items():
        if type(key) is int and key >= 0:
            index = key
        elif isinstance(key, str) and key.isascii() and key.isdecimal() and str(int(key)) == key:
            index = int(key)
        else:
            raise GlbError('ambiguous_role_key')
        if index in converted or role not in ('visual', 'collision', 'occluder'):
            raise GlbError('ambiguous_roles')
        converted[index] = role
    expected = {i for i, n in enumerate(doc['nodes']) if 'mesh' in n}
    if set(converted) != expected:
        raise GlbError('role_coverage')
    if 'visual' not in converted.values():
        raise GlbError('empty_visual', 3)
    return converted


def node_inventory(doc, roles):
    nodes = doc['nodes']
    parents = {child: i for i, node in enumerate(nodes) for child in node.get('children', [])}
    matrices = {}
    for index in range(len(nodes)):
        chain, cursor = [], index
        while cursor not in matrices:
            chain.append(cursor)
            if cursor not in parents:
                break
            cursor = parents[cursor]
        for current in reversed(chain):
            local = _local_matrix(nodes[current])
            if current in parents:
                parent = matrices[parents[current]]
                local = [[sum(parent[r][k] * local[k][c] for k in range(4))
                          for c in range(4)] for r in range(4)]
            if any(not math.isfinite(v) for row in local for v in row):
                raise GlbError('transform_overflow')
            matrices[current] = local
    return [{'index': i, 'name': n.get('name', ''), 'role': roles.get(i),
             'world_matrix_hex': [float(v).hex() for row in matrices[i] for v in row],
             'suffix_hint': any(n.get('name', '').endswith(s) for s in
                                ('_col', '-col', '-convcol', '-occ', '_collision'))}
            for i, n in enumerate(nodes)]


def inspect_glb(source, *, roles):
    try:
        data = read_bytes(source)
        doc, binary = decode_glb(data)
        table = role_table(doc, roles)
        return record(0, source_sha256=digest(data), nodes=node_inventory(doc, table),
                      features={'hierarchy': True, 'instancing': len(table) > len({n['mesh'] for n in doc['nodes'] if 'mesh' in n}),
                                'materials': len(doc.get('materials', [])), 'images': len(doc.get('images', [])),
                                'skins': 0, 'animations': 0}, binary_sha256=digest(binary))
    except GlbError as exc:
        return record(exc.rc, exc.code)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return record(2, 'io_or_schema')


def _partition(doc, table, visual):
    selected = {index for index, role in table.items() if (role == 'visual') == visual}
    result = copy.deepcopy(doc)
    old_meshes = sorted({doc['nodes'][i]['mesh'] for i in selected})
    remap = {old: new for new, old in enumerate(old_meshes)}
    result['meshes'] = [copy.deepcopy(doc['meshes'][i]) for i in old_meshes]
    for index, node in enumerate(result['nodes']):
        if 'mesh' in node:
            if index in selected:
                node['mesh'] = remap[node['mesh']]
            else:
                del node['mesh']
    return result


def _visual_product(doc, binary, data, table):
    return data if all(r == 'visual' for r in table.values()) else encode_glb(_partition(doc, table, True), binary)


def verify_partition(source_doc, source_binary, candidate, table, *, visual):
    """Independent semantic delta gate: no resource/transform/material may change."""
    try:
        out, binary = decode_glb(candidate)
        if binary != source_binary:
            raise GlbError('resource_changed', 5)
        for key in set(source_doc) | set(out):
            if key not in ('nodes', 'meshes') and source_doc.get(key) != out.get(key):
                raise GlbError('feature_loss', 5)
        if len(source_doc['nodes']) != len(out['nodes']):
            raise GlbError('hierarchy_loss', 5)
        expected_meshes = set()
        for i, (before, after) in enumerate(zip(source_doc['nodes'], out['nodes'])):
            if {k: v for k, v in before.items() if k != 'mesh'} != {k: v for k, v in after.items() if k != 'mesh'}:
                raise GlbError('transform_or_node_loss', 5)
            selected = i in table and ((table[i] == 'visual') == visual)
            if selected:
                if 'mesh' not in after or source_doc['meshes'][before['mesh']] != out['meshes'][after['mesh']]:
                    raise GlbError('mesh_or_material_loss', 5)
                expected_meshes.add(after['mesh'])
            elif 'mesh' in after:
                raise GlbError('helper_leak', 5)
        if expected_meshes != set(range(len(out['meshes']))):
            raise GlbError('unexpected_mesh', 5)
        return record(0)
    except GlbError as exc:
        return record(5, exc.code)
    except (ValueError, TypeError, KeyError, IndexError):
        return record(5, 'invalid_candidate')


MAX_EVIDENCE_FILES = 64  # DESIGN_CHOICE bounded work; not an observed workload.
MAX_EVIDENCE_BYTES = 128 * 2**20
GATE_SCHEMAS = {'af.mesh-regression/v1': 'mesh-regression', 'af.glb-capture/v1': 'glb-capture'}


def _requirements(required_gates, required_lod_levels):
    if not required_gates or required_lod_levels is None:
        raise GlbError('required_coverage_missing', 3)
    if not isinstance(required_gates, (tuple, list)) or any(not isinstance(x, str) for x in required_gates):
        raise GlbError('required_coverage_invalid')
    if not isinstance(required_lod_levels, (tuple, list)):
        raise GlbError('required_coverage_invalid')
    for level in required_lod_levels: integer(level)
    if len(set(required_gates)) != len(required_gates) or len(set(required_lod_levels)) != len(required_lod_levels):
        raise GlbError('required_coverage_invalid')


def _check_evidence(root, source_sha256, lods, gate_refs, required_gates, required_lod_levels, checkpoint=lambda: None):
    _requirements(required_gates, required_lod_levels)
    if not isinstance(lods, list) or not isinstance(gate_refs, list):
        raise GlbError('metadata_invalid')
    if len(lods) + len(gate_refs) > MAX_EVIDENCE_FILES:
        raise GlbError('evidence_budget', 6)
    total = 0
    def read(path):
        nonlocal total
        checkpoint()
        content = read_bytes(path)
        total += len(content)
        if total > MAX_EVIDENCE_BYTES: raise GlbError('evidence_budget', 6)
        checkpoint()
        return content
    levels, lod_digests = set(), set()
    for entry in lods:
        closed(entry, ('level', 'path', 'sha256'))
        level = integer(entry['level'])
        if level in levels: raise GlbError('duplicate_lod')
        levels.add(level)
        path = bound_path(root, entry['path'])
        if not path.is_file(): raise GlbError('required_lod_missing', 5)
        if digest(read(path)) != sha(entry['sha256']): raise GlbError('lod_stale', 4)
        lod_digests.add(entry['sha256'])
    if not set(required_lod_levels) <= levels: raise GlbError('required_lod_uncovered', 3)
    proven_lods, gate_paths, covered, rcs = set(), set(), set(), []
    normalized = []
    for entry in gate_refs:
        try:
            if set(entry) == {'path', 'sha256', 'subject_sha256'}:
                closed(entry, ('path', 'sha256', 'subject_sha256'))
            else:
                closed(entry, ('gate', 'schema', 'path', 'sha256', 'subject_sha256', 'verdict'))
            if entry['path'] in gate_paths: raise GlbError('duplicate_gate')
            gate_paths.add(entry['path'])
            path = bound_path(root, entry['path'])
            if not path.is_file(): raise GlbError('gate_missing', 3)
            content = read(path)
            subject = sha(entry['subject_sha256'])
            if digest(content) != sha(entry['sha256']) or subject not in lod_digests | {source_sha256}:
                raise GlbError('gate_stale', 4)
            gate = parse_json(content)
            if not isinstance(gate, dict) or type(gate.get('rc')) is not int or gate['rc'] not in STATUS:
                raise GlbError('unsupported_gate_contract')
            canonical(gate)
            schema = gate.get('schema')
            if schema not in GATE_SCHEMAS: raise GlbError('unsupported_gate_contract')
            if gate.get('status') != STATUS[gate['rc']] or gate.get('verdict') != VERDICT[gate['rc']]:
                raise GlbError('upstream_rc_status_or_verdict_mismatch')
            if schema == 'af.glb-capture/v1':
                from tools.blender.glb_schemas import CAPTURE_SCHEMA
                validate_record(gate, CAPTURE_SCHEMA)
                if gate.get('subject_sha256') != subject: raise GlbError('gate_subject_stale', 4)
            elif gate.get('input_sha256') != source_sha256:
                raise GlbError('gate_subject_stale', 4)
            if subject in lod_digests:
                if gate.get('input_sha256') != source_sha256 or gate.get('candidate_sha256') != subject:
                    raise GlbError('lod_regression_unbound', 4)
                if gate['rc'] == 0: proven_lods.add(subject)
            elif subject not in [gate.get(k) for k in ('input_sha256', 'source_sha256', 'subject_sha256')]:
                raise GlbError('gate_subject_stale', 4)
            name = GATE_SCHEMAS[schema]
            if 'gate' in entry and (entry['gate'] != name or entry['schema'] != schema or entry['verdict'] != gate['verdict']):
                raise GlbError('gate_binding_stale', 4)
            covered.add(name)
            normalized.append({**entry, 'gate': name, 'schema': schema, 'verdict': gate['verdict']})
            rcs.append(gate['rc'])
        except GlbError as exc:
            rcs.append(exc.rc)
    # Preserve a single upstream status, aggregate mixed statuses by F0 precedence.
    if any(rcs):
        rc = rcs[0] if len(set(rcs)) == 1 else core.worst_rc(rcs)
        raise GlbError('upstream_refused', rc)
    if not set(required_gates) <= covered or proven_lods != lod_digests:
        raise GlbError('required_gate_uncovered', 3)
    return normalized


def _metadata(source, data, table, metadata, *, required_gates=(), required_lod_levels=None, checkpoint=lambda: None):
    if metadata is None: raise GlbError('metadata_missing', 3)
    closed(metadata, ('source_sha256', 'roles_sha256', 'lods', 'gate_refs'))
    if sha(metadata['source_sha256']) != digest(data) or sha(metadata['roles_sha256']) != json_digest({str(k): v for k, v in table.items()}):
        raise GlbError('metadata_stale', 4)
    gates = _check_evidence(source.parent, digest(data), metadata['lods'], metadata['gate_refs'],
                            required_gates, required_lod_levels, checkpoint)
    return {**copy.deepcopy(metadata), 'gate_refs': gates}


def validate_separation(report):
    validate_record(report, SEPARATION_SCHEMA)
    closed(report, SEPARATION_FIELDS)
    if report['schema'] != 'af.glb-separation/v1' or type(report['rc']) is not int or report['rc'] != 0 or report['status'] != 'OK':
        raise GlbError('separation_invalid')
    sha(report['source_sha256']); sha(report['roles_sha256'])
    if report['normalization'] != {'applied': False, 'basis': 'gltf-native'}:
        raise GlbError('normalization_invalid')
    if report['appearance'] != {'rc': 3, 'verdict': 'CANNOT_SEE', 'reason_codes': ['captures_not_evaluated']}:
        raise GlbError('appearance_overclaim')
    if report['reason_codes'] != [] or report['retained_unreferenced_buffer'] is not True:
        raise GlbError('separation_invalid')
    if not isinstance(report['nodes'], list) or not report['nodes']:
        raise GlbError('separation_invalid')
    for node in report['nodes']:
        closed(node, ('index', 'name', 'role', 'world_matrix_hex', 'suffix_hint'))
        integer(node['index'])
        if not isinstance(node['name'], str) or node['role'] not in (None, 'visual', 'collision', 'occluder') or type(node['suffix_hint']) is not bool:
            raise GlbError('separation_invalid')
        if not isinstance(node['world_matrix_hex'], list) or len(node['world_matrix_hex']) != 16:
            raise GlbError('separation_invalid')
        for value in node['world_matrix_hex']:
            if not isinstance(value, str) or not math.isfinite(float.fromhex(value)):
                raise GlbError('separation_invalid')
    for artifact in report['artifacts']:
        closed(artifact, ('path', 'sha256'))
        sha(artifact['sha256'])
    _requirements(report['required_gates'], report['required_lod_levels'])
    for lod in report['lods']:
        integer(lod['level']); sha(lod['sha256'])
    for gate in report['gate_refs']:
        sha(gate['sha256']); sha(gate['subject_sha256'])
        if gate['schema'] not in GATE_SCHEMAS or gate['gate'] != GATE_SCHEMAS[gate['schema']]:
            raise GlbError('unsupported_gate_contract')
    canonical(report)


def _tool_refs():
    return [{'path': p.name, 'sha256': digest(read_bytes(p))} for p in
            (Path(__file__), Path(__file__).with_name('helpers_contracts.py'),
             Path(__file__).with_name('glb_schemas.py'), Path(core.__file__))]


def _validate_receipt(receipt):
    validate_record(receipt, RECEIPT_SCHEMA)
    closed(receipt, RECEIPT_FIELDS)
    if receipt['schema'] != 'af.glb-receipt/v1':
        raise GlbError('receipt_invalid')
    sha(receipt['source_sha256']); sha(receipt['roles_sha256'])
    closed(receipt['report'], ('path', 'sha256'))
    sha(receipt['report']['sha256'])
    for key in ('artifacts', 'tool_files'):
        if not isinstance(receipt[key], list) or not receipt[key]:
            raise GlbError('receipt_invalid')
        for entry in receipt[key]:
            closed(entry, ('path', 'sha256'))
            sha(entry['sha256'])
    canonical(receipt)


def verify_bundle(output_dir):
    try:
        root = Path(output_dir)
        if root.is_symlink():
            raise GlbError('path_escape')
        receipt = parse_json(read_bytes(bound_path(root, 'receipt.json')))
        _validate_receipt(receipt)
        if receipt['tool_files'] != _tool_refs():
            raise GlbError('tool_stale', 4)
        report_bytes = read_bytes(bound_path(root, receipt['report']['path']))
        if digest(report_bytes) != receipt['report']['sha256']:
            raise GlbError('report_stale', 4)
        report = parse_json(report_bytes)
        validate_separation(report)
        if report['artifacts'] != receipt['artifacts'] or any(report[k] != receipt[k] for k in ('source_sha256', 'roles_sha256')):
            raise GlbError('binding_stale', 4)
        names = [a['path'] for a in receipt['artifacts']]
        if len(set(names)) != len(names) or set(names) not in ({'visual.glb'}, {'visual.glb', 'helpers.glb'}):
            raise GlbError('artifact_set_invalid')
        for artifact in receipt['artifacts']:
            content = read_bytes(bound_path(root, artifact['path']))
            if digest(content) != artifact['sha256']:
                raise GlbError('artifact_stale', 4)
            decode_glb(content)
        _check_evidence(root, report['source_sha256'], report['lods'], report['gate_refs'],
                        report['required_gates'], report['required_lod_levels'])
        return record(0)
    except GlbError as exc:
        return record(exc.rc, exc.code)
    except FileNotFoundError:
        return record(3, 'receipt_or_artifact_missing')
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
        return record(2, 'bundle_invalid')


def separate_glb(source, output_dir, *, roles, normalize, timeout_s, metadata=None,
                 required_gates=(), required_lod_levels=None):
    stage = None
    try:
        if type(normalize) is not bool or normalize:
            raise GlbError('normalization_unsupported')
        integer(timeout_s, 1)
        deadline = _monotonic() + timeout_s
        def budget():
            if _monotonic() >= deadline:
                raise GlbError('deadline_exceeded', 6)
        source, target = Path(source), Path(output_dir)
        if target.exists() or target.is_symlink():
            raise GlbError('destination_exists')
        if not target.parent.is_dir() or target.parent.resolve() != target.parent.absolute():
            raise GlbError('destination_parent_invalid')
        data = read_bytes(source)
        doc, binary = decode_glb(data, checkpoint=budget)
        table = role_table(doc, roles)
        metadata = _metadata(source, data, table, metadata, required_gates=required_gates,
                             required_lod_levels=required_lod_levels, checkpoint=budget)
        budget()
        visual = _visual_product(doc, binary, data, table)
        products = {'visual.glb': visual}
        if any(r != 'visual' for r in table.values()):
            products['helpers.glb'] = encode_glb(_partition(doc, table, False), binary)
        for name, content in products.items():
            check = verify_partition(doc, binary, content, table, visual=(name == 'visual.glb'))
            if check['rc']:
                raise GlbError(check['reason_codes'][0], 5)
        budget()
        artifacts = [{'path': name, 'sha256': digest(content)} for name, content in sorted(products.items())]
        original_metadata = copy.deepcopy(metadata)
        # Snapshot all upstream bytes; later verification has no external dependency.
        evidence = {}
        for entry in metadata['lods'] + metadata['gate_refs']:
            budget()
            content = read_bytes(bound_path(source.parent, entry['path']))
            if digest(content) != entry['sha256']: raise GlbError('evidence_stale', 4)
            name = 'evidence/' + entry['sha256'] + ('.glb' if 'level' in entry else '.json')
            evidence[name] = content
            entry['path'] = name
        report = record(0, schema='af.glb-separation/v1', source_sha256=digest(data),
                        roles_sha256=metadata['roles_sha256'], nodes=node_inventory(doc, table),
                        normalization={'applied': False, 'basis': 'gltf-native'},
                        lods=metadata['lods'], gate_refs=metadata['gate_refs'], artifacts=artifacts,
                        required_gates=list(required_gates), required_lod_levels=list(required_lod_levels),
                        appearance={'rc': 3, 'verdict': 'CANNOT_SEE', 'reason_codes': ['captures_not_evaluated']},
                        retained_unreferenced_buffer=True)
        validate_separation(report)
        stage = Path(tempfile.mkdtemp(prefix='.af-glb-', dir=target.parent))
        (stage / 'evidence').mkdir()
        for name, content in {**products, **evidence}.items():
            write_synced(stage / name, content)
        sync_directory(stage / 'evidence')
        report_data = canonical(report)
        write_synced(stage / 'separation.json', report_data)
        receipt = {'schema': 'af.glb-receipt/v1', 'source_sha256': digest(data),
                   'roles_sha256': metadata['roles_sha256'],
                   'report': {'path': 'separation.json', 'sha256': digest(report_data)},
                   'artifacts': artifacts, 'tool_files': _tool_refs()}
        _validate_receipt(receipt)
        write_synced(stage / 'receipt.json', canonical(receipt))
        check = verify_bundle(stage)
        if check['rc']:
            raise GlbError(check['reason_codes'][0], check['rc'])
        # Rebind mutable source/metadata files immediately before publication.
        if read_bytes(source) != data:
            raise GlbError('source_stale', 4)
        _metadata(source, data, table, original_metadata, required_gates=required_gates,
                  required_lod_levels=required_lod_levels, checkpoint=budget)
        budget()
        sync_directory(stage)
        publish_directory(stage, target)
        stage = None
        return report
    except GlbError as exc:
        return record(exc.rc, exc.code)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return record(2, 'io_or_schema')
    finally:
        if stage is not None:
            shutil.rmtree(stage)
