"""Synthetic GLB witnesses and executable rule mutants; no third-party assets."""
import copy
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock
import zlib

# -I ignores PYTHONPATH: add only the trusted component root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.blender import glb_io as io
from tools.blender import glb_capture as capture
from tools.blender.helpers_contracts import canonical, digest, json_digest, GlbError, record, parse_json


def fixture(helper=True, role='collision'):
    binary, views, accessors = bytearray(), [], []
    def blob(data):
        binary.extend(b'\0' * (-len(binary) % 4))
        offset = len(binary)
        binary.extend(data)
        views.append({'buffer': 0, 'byteOffset': offset, 'byteLength': len(data)})
        return len(views)-1
    def acc(values, fmt, component, kind, count, **extra):
        view = blob(struct.pack('<' + fmt, *values))
        accessors.append({'bufferView': view, 'componentType': component, 'type': kind,
                          'count': count, **extra})
        return len(accessors)-1
    points = [(-1,-.5,-.5), (1,-.5,-.5), (1,.5,-.5), (-1,.5,-.5),
              (-1,-.5,.5), (1,-.5,.5), (1,.5,.5), (-1,.5,.5)]
    positions = acc([x for p in points for x in p], '24f', 5126, 'VEC3', 8,
                    min=[-1,-.5,-.5], max=[1,.5,.5])
    uv = acc([v for i in range(8) for v in (i % 2, (i//2) % 2)], '16f', 5126, 'VEC2', 8)
    indices = [0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,
               1,2,6,1,6,5,2,3,7,2,7,6,3,0,4,3,4,7]
    tris1 = acc(indices[:18], '18H', 5123, 'SCALAR', 18)
    tris2 = acc(indices[18:], '18H', 5123, 'SCALAR', 18)
    tris = acc(indices, '36H', 5123, 'SCALAR', 36)
    def chunk(kind, content):
        return struct.pack('>I', len(content)) + kind + content + struct.pack('>I', zlib.crc32(kind+content))
    image = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>2I5B', 2,2,8,2,0,0,0))
    image += chunk(b'IDAT', zlib.compress(b'\0\xff\x40\x10\xff\x40\x10' * 2)) + chunk(b'IEND', b'')
    image_view = blob(image)
    mesh = {'primitives': [{'attributes': {'POSITION': positions, 'TEXCOORD_0': uv},
                            'indices': tris1, 'material': 0},
                           {'attributes': {'POSITION': positions, 'TEXCOORD_0': uv},
                            'indices': tris2, 'material': 2}]}
    nodes = [{'name': 'transformed-root', 'translation': [1.25,-0.5,0.125],
              'scale': [0.75,1.2,1], 'children': [1,2]},
             {'name': 'legitimate_col', 'mesh': 0},
             {'name': 'instance', 'mesh': 0, 'translation': [0,0,1.4], 'scale': [.5,.5,.5]}]
    roles, meshes = {'1': 'visual', '2': 'visual'}, [mesh]
    if helper:
        nodes[0]['children'].append(3)
        suffix = '_col' if role == 'collision' else '-occ'
        nodes.append({'name': 'enclosing' + suffix, 'mesh': 1, 'scale': [4,8,8]})
        roles['3'] = role
        meshes.append({'primitives': [{'attributes': {'POSITION': positions}, 'indices': tris, 'material': 1}]})
    doc = {'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': [0]}],
           'nodes': nodes, 'meshes': meshes, 'accessors': accessors, 'bufferViews': views,
           'materials': [{'name': 'red-textured', 'pbrMetallicRoughness': {
               'baseColorTexture': {'index': 0}, 'metallicFactor': 0, 'roughnessFactor': 0.8}},
               {'name': 'opaque-white', 'pbrMetallicRoughness': {'baseColorFactor': [1,1,1,1], 'metallicFactor': 0}},
               {'name': 'other-material', 'pbrMetallicRoughness': {'baseColorFactor': [0.8,0.1,0.02,1], 'metallicFactor': 0}}],
           'textures': [{'source': 0}], 'images': [{'bufferView': image_view, 'mimeType': 'image/png'}],
           'buffers': [{'byteLength': len(binary)}]}
    binary.extend(b'\0' * (-len(binary) % 4))
    return doc, bytes(binary), roles


def gate_payload(rc=0, **fields):
    from tools.blender.helpers_contracts import STATUS, VERDICT
    return {'schema': 'af.mesh-regression/v1', 'rc': rc, 'status': STATUS.get(rc, 'ERROR'),
            'verdict': VERDICT.get(rc, 'ERROR'), **fields}


class GLBFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root/'source.glb', self.root/'result'
        self.doc, self.binary, self.roles = fixture()
        self.save()

    def save(self):
        self.data = io.encode_glb(self.doc, self.binary)
        self.source.write_bytes(self.data)
        self.metadata = {'source_sha256': digest(self.data), 'roles_sha256': json_digest(self.roles),
                         'lods': [], 'gate_refs': []}
        gate = self.root/'gate.json'
        gate.write_bytes(canonical(gate_payload(input_sha256=digest(self.data))))
        self.metadata['gate_refs'] = [{'path': gate.name, 'sha256': digest(gate.read_bytes()),
                                      'subject_sha256': digest(self.data)}]

    def separate(self, **kwargs):
        opts = {'roles': self.roles, 'normalize': False, 'timeout_s': 20, 'metadata': self.metadata,
                'required_gates': ('mesh-regression',), 'required_lod_levels': ()}
        opts.update(kwargs)
        return io.separate_glb(self.source, self.target, **opts)

    def assertRefusal(self, result, rc):
        self.assertEqual(result['rc'], rc, result)
        self.assertNotEqual(result['rc'], 1)
        self.assertEqual(self.source.read_bytes(), self.data)
        self.assertFalse(self.target.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['gate.json', 'source.glb'])

class GLBTests(GLBFixture, unittest.TestCase):
    def test_u4_identity_keeps_raw_bytes(self):
        self.doc, self.binary, self.roles = fixture(False)
        self.save()
        result = self.separate()
        self.assertEqual(result['rc'], 0, result)
        self.assertEqual((self.target/'visual.glb').read_bytes(), self.data)
        self.assertFalse((self.target/'helpers.glb').exists())
        self.assertEqual(io.verify_bundle(self.target)['rc'], 0)
        self.assertEqual(json.loads((self.target/'receipt.json').read_text())['schema'], 'af.glb-receipt/v1')
        self.assertEqual(result['normalization'], {'applied': False, 'basis': 'gltf-native'})
        self.assertEqual(result['appearance']['rc'], 3)
        self.assertNotIn('verdict', result)

    def test_u4_separates_declared_helpers(self):
        result = self.separate()
        self.assertEqual(result['rc'], 0, result)
        visual, vb = io.decode_glb((self.target/'visual.glb').read_bytes())
        helpers, hb = io.decode_glb((self.target/'helpers.glb').read_bytes())
        self.assertEqual({i for i,n in enumerate(visual['nodes']) if 'mesh' in n}, {1,2})
        self.assertEqual({i for i,n in enumerate(helpers['nodes']) if 'mesh' in n}, {3})
        self.assertEqual(visual['nodes'][0], self.doc['nodes'][0])
        self.assertEqual(vb, self.binary); self.assertEqual(hb, self.binary)
        inventory = io.inspect_glb(self.source, roles=self.roles)
        self.assertEqual(inventory['rc'], 0)
        self.assertTrue(inventory['features']['instancing'])
        helper_world = inventory['nodes'][3]['world_matrix_hex']
        self.assertEqual(float.fromhex(helper_world[0]), 3.0)
        self.assertEqual(float.fromhex(helper_world[3]), 1.25)
        self.assertEqual(float.fromhex(helper_world[7]), -0.5)
        self.assertEqual(io.verify_bundle(self.target)['rc'], 0)

    def test_u4_roundtrip_preserves_scene_and_pbr(self):
        self.assertEqual(self.separate()['rc'], 0)
        out, binary = io.decode_glb((self.target/'visual.glb').read_bytes())
        for key in ('materials', 'textures', 'images', 'bufferViews', 'accessors', 'scenes'):
            self.assertEqual(out[key], self.doc[key])
        self.assertEqual(out['meshes'][out['nodes'][1]['mesh']], self.doc['meshes'][0])
        self.assertEqual(out['nodes'][1]['mesh'], out['nodes'][2]['mesh'])
        image = self.doc['bufferViews'][self.doc['images'][0]['bufferView']]
        offset, length = image['byteOffset'], image['byteLength']
        self.assertEqual(binary[offset:offset+length], self.binary[offset:offset+length])
        for mutation in ('material', 'uv', 'transform', 'binary', 'mesh'):
            bad, buf = copy.deepcopy(out), bytearray(binary)
            if mutation == 'material': bad['materials'][0]['pbrMetallicRoughness'].pop('baseColorTexture')
            if mutation == 'uv': bad['meshes'][0]['primitives'][0]['attributes'].pop('TEXCOORD_0')
            if mutation == 'transform': bad['nodes'][0]['translation'][0] += 1
            if mutation == 'binary': buf[0] ^= 1
            if mutation == 'mesh': bad['nodes'][1].pop('mesh')
            self.assertEqual(io.verify_partition(self.doc, self.binary, io.encode_glb(bad, bytes(buf)),
                             io.role_table(self.doc, self.roles), visual=True)['rc'], 5, mutation)

    def test_u4_suffix_is_not_role_authority(self):
        self.doc['nodes'][3]['name'] = 'no-helper-suffix'; self.save()
        result = self.separate()
        self.assertEqual(result['rc'], 0, result)
        out, _ = io.decode_glb((self.target/'visual.glb').read_bytes())
        self.assertIn('mesh', out['nodes'][1]); self.assertNotIn('mesh', out['nodes'][3])
        self.assertTrue(result['nodes'][1]['suffix_hint']); self.assertFalse(result['nodes'][3]['suffix_hint'])
        self.assertEqual(io.inspect_glb(self.source, roles={**self.roles, 1: 'collision'})['rc'], 2)

    def test_role_coverage_and_invalid_types_refuse(self):
        for roles in ({'1': 'visual'}, {**self.roles, '99': 'visual'},
                      {**self.roles, '01': 'visual'}, {True: 'visual'}, {**self.roles, '3': 'maybe'}):
            with self.subTest(roles=roles): self.assertRefusal(self.separate(roles=roles), 2)

    def test_u4_unsupported_skin_and_external_texture_refuse(self):
        for key, value in [('skins', [{'joints': [1]}]), ('animations', [{}]),
                           ('extensionsUsed', ['KHR_draco_mesh_compression'])]:
            self.doc[key] = value; self.save(); self.assertRefusal(self.separate(), 2); del self.doc[key]
        self.doc['images'][0]['uri'] = '../secret.png'; self.save()
        self.assertRefusal(self.separate(), 2)

    def test_u4_missing_or_stale_sidecar_refuses(self):
        self.assertRefusal(self.separate(metadata=None), 3)
        self.assertRefusal(self.separate(metadata={**self.metadata, 'source_sha256': '0'*64}), 4)
        self.assertRefusal(self.separate(metadata={**self.metadata, 'roles_sha256': '0'*64}), 4)
        self.assertRefusal(self.separate(normalize=True), 2)
        self.assertRefusal(self.separate(normalize=0), 2)

    def test_u4_missing_lod_is_not_renumbered(self):
        lod = self.root/'lod7.glb'
        lod_doc = copy.deepcopy(self.doc); lod_doc['nodes'][1]['name'] = 'distinct-lod'
        lod_data = io.encode_glb(lod_doc, self.binary); lod.write_bytes(lod_data)
        self.assertNotEqual(digest(lod_data), digest(self.data))
        self.metadata['lods'] = [{'level': 7, 'path': lod.name, 'sha256': digest(lod_data)}]
        self.assertEqual(self.separate()['rc'], 3)
        gate = self.root/'regression.json'
        gate.write_bytes(canonical(gate_payload(input_sha256=digest(self.data), candidate_sha256=digest(lod_data))))
        self.metadata['gate_refs'] = [{'path': gate.name, 'sha256': digest(gate.read_bytes()),
                                      'subject_sha256': digest(lod_data)}]
        result = self.separate(required_lod_levels=(7,)); self.assertEqual(result['rc'], 0, result)
        self.assertEqual(result['lods'][0]['level'], 7)
        __import__('shutil').rmtree(self.target); lod.unlink(); gate.unlink()
        self.assertRefusal(self.separate(), 5)

    def test_bound_upstream_gate_and_paths(self):
        gate = self.root/'gate.json'
        gate.write_bytes(canonical(gate_payload(input_sha256=digest(self.data))))
        ref = {'path': gate.name, 'sha256': digest(gate.read_bytes()), 'subject_sha256': digest(self.data)}
        self.metadata['gate_refs'] = [ref]
        self.assertEqual(self.separate()['rc'], 0)
        __import__('shutil').rmtree(self.target)
        for rc in (2,3,4,5,6):
            gate.write_bytes(canonical(gate_payload(rc, input_sha256=digest(self.data))))
            ref['sha256'] = digest(gate.read_bytes())
            result = self.separate(); self.assertEqual(result['rc'], rc, result)
            self.assertFalse(self.target.exists())
        gate.write_bytes(canonical(gate_payload(1, input_sha256=digest(self.data)))); ref['sha256'] = digest(gate.read_bytes())
        self.assertEqual(self.separate()['rc'], 2)
        gate.write_bytes(canonical(gate_payload(input_sha256=digest(self.data)))); ref['sha256'] = digest(gate.read_bytes())
        ref['path'] = '../escape'; self.assertEqual(self.separate()['rc'], 2)
        ref['path'] = 'linked'; (self.root/'linked').symlink_to(gate)
        self.assertEqual(self.separate()['rc'], 2)

    def test_u4_refusal_preserves_outputs(self):
        self.target.mkdir(); sentinel = self.target/'visual.glb'; sentinel.write_bytes(b'CALLER SENTINEL')
        self.assertEqual(self.separate()['rc'], 2)
        self.assertEqual(sentinel.read_bytes(), b'CALLER SENTINEL')
        self.assertFalse((self.target/'receipt.json').exists())
        sentinel.unlink(); self.target.rmdir()
        with mock.patch.object(io, 'verify_partition', return_value=record(5, 'planted_failure')):
            self.assertRefusal(self.separate(), 5)
        with mock.patch.object(io, 'publish_directory', side_effect=OSError('publication fault')):
            self.assertRefusal(self.separate(), 2)

    def test_stale_source_and_budget_preserve_bytes(self):
        ticks = iter([0])
        with mock.patch.object(io, '_monotonic', side_effect=lambda: next(ticks, 99999)): self.assertRefusal(self.separate(), 6)
        self.assertRefusal(self.separate(timeout_s=True), 2)
        original = io.read_bytes
        count = 0
        def changed(path):
            nonlocal count
            if Path(path) == self.source:
                count += 1
                if count > 1: return self.data + b'changed'
            return original(path)
        with mock.patch.object(io, 'read_bytes', side_effect=changed): self.assertRefusal(self.separate(), 4)

    def test_receipt_and_artifact_tampering_are_stale(self):
        self.assertEqual(self.separate()['rc'], 0)
        visual = self.target/'visual.glb'; visual.write_bytes(visual.read_bytes() + b'wrong')
        self.assertEqual(io.verify_bundle(self.target)['rc'], 4)
        visual.unlink(); self.assertEqual(io.verify_bundle(self.target)['rc'], 3)
        path = self.target/'receipt.json'; receipt = json.loads(path.read_text())
        receipt['report']['path'] = '../source.glb'; path.write_bytes(canonical(receipt))
        self.assertEqual(io.verify_bundle(self.target)['rc'], 2)

    def test_malformed_glb_and_graphs_refuse(self):
        for bad in (b'', self.data[:10], b'BAD!' + self.data[4:], self.data + b'extra'):
            self.source.write_bytes(bad); self.assertEqual(io.inspect_glb(self.source, roles=self.roles)['rc'], 2)
        for mutation in ('cycle', 'multiple_parent', 'buffer', 'bool_mesh', 'skin', 'transform'):
            doc = copy.deepcopy(self.doc)
            if mutation == 'cycle': doc['nodes'][1]['children'] = [0]
            if mutation == 'multiple_parent': doc['nodes'][1]['children'] = [2]
            if mutation == 'buffer': doc['bufferViews'][0]['byteLength'] = 999999
            if mutation == 'bool_mesh': doc['nodes'][1]['mesh'] = True
            if mutation == 'skin': doc['nodes'][1]['skin'] = 0
            if mutation == 'transform': doc['nodes'][0]['matrix'] = [0]*16
            self.source.write_bytes(io.encode_glb(doc, self.binary))
            self.assertEqual(io.inspect_glb(self.source, roles=self.roles)['rc'], 2, mutation)

    def test_duplicate_json_and_float_receipts_refuse(self):
        with self.assertRaises(GlbError): parse_json(b'{"a":1,"a":2}')
        with self.assertRaises(GlbError): canonical({'metric': 0.5})
        self.assertEqual(io.inspect_glb(self.source, roles={**self.roles, 1: 'visual'})['rc'], 2)

    def test_cross_role_instances_and_reflections_survive(self):
        self.doc['nodes'][3]['mesh'] = 0
        self.doc['nodes'][0]['scale'][0] = -0.75
        self.save()
        result = self.separate()
        self.assertEqual(result['rc'], 0, result)
        visual, _ = io.decode_glb((self.target/'visual.glb').read_bytes())
        helper, _ = io.decode_glb((self.target/'helpers.glb').read_bytes())
        self.assertEqual(len(visual['meshes']), 1)
        self.assertEqual(len(helper['meshes']), 1)
        self.assertEqual(visual['nodes'][0]['scale'][0], -0.75)
        self.assertEqual(visual['meshes'][0], helper['meshes'][0])

    def test_atomic_commit_does_not_replace_racing_empty_directory(self):
        original = io.publish_directory
        inode = []
        def race(stage, target):
            target.mkdir()
            inode.append(target.stat().st_ino)
            return original(stage, target)
        with mock.patch.object(io, 'publish_directory', side_effect=race):
            result = self.separate()
        self.assertEqual(result['rc'], 2, result)
        self.assertEqual(self.target.stat().st_ino, inode[0])
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['gate.json', 'result', 'source.glb'])

    def test_concurrent_commit_has_one_owner(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)
        original = io.publish_directory
        def together(stage, target):
            barrier.wait(timeout=5)
            return original(stage, target)
        with mock.patch.object(io, 'publish_directory', side_effect=together):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.separate(), range(2)))
        self.assertEqual(sorted(r['rc'] for r in results), [0, 2])
        self.assertEqual(io.verify_bundle(self.target)['rc'], 0)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['gate.json', 'result', 'source.glb'])

    def test_malformed_names_and_resource_budget_refuse(self):
        for name in ([], {}, None, 42, True):
            self.doc['nodes'][1]['name'] = name; self.save()
            self.assertRefusal(self.separate(), 2)
        self.doc['nodes'][1]['name'] = 'visual'; self.save()
        import tools.blender.helpers_contracts as contracts
        with mock.patch.object(contracts, 'MAX_BYTES', len(self.data)-1):
            self.assertRefusal(self.separate(), 6)

    def test_stale_gate_cannot_be_overridden(self):
        gate = self.root/'gate.json'
        gate.write_bytes(canonical(gate_payload(input_sha256='0'*64)))
        ref = {'path': gate.name, 'sha256': digest(gate.read_bytes()), 'subject_sha256': digest(self.data)}
        self.metadata['gate_refs'] = [ref]
        self.assertEqual(self.separate()['rc'], 4)
        gate.write_bytes(canonical(gate_payload(status='FAIL', input_sha256=digest(self.data))))
        ref['sha256'] = digest(gate.read_bytes())
        self.assertEqual(self.separate()['rc'], 2)
        gate.write_bytes(canonical(gate_payload(5, input_sha256=digest(self.data))))
        ref['sha256'] = digest(gate.read_bytes())
        self.assertEqual(self.separate()['rc'], 5)
        ref['sha256'] = '0'*64
        self.assertEqual(self.separate()['rc'], 4)

    def test_u4_missing_capture_cannot_pass(self):
        args = {'source_sha256': digest(self.data), 'candidate_sha256': digest(self.data)}
        self.assertEqual(capture.evaluate_captures(None, **args)['rc'], 3)
        captures = {'schema': 'af.glb-capture-frames/v1', **args, 'helpers_declared': True,
                    'profile': copy.deepcopy(capture.CAPTURE_PROFILE), 'views': []}
        self.assertEqual(capture.evaluate_captures(captures, **args)['rc'], 3)
        captures['views'] = [{'view': i, 'raw_visual_rgb_sha256': 'a'*64, 'candidate_rgb_sha256': 'a'*64,
                             'diagnostic_rgb_sha256': 'b'*64, 'background_rgb_sha256': 'd'*64,
                             'pixel_count': 4096} for i in range(capture.CAPTURE_PROFILE['views'])]
        self.assertEqual(capture.evaluate_captures(captures, **args)['rc'], 0)
        for invalid in (False, 0.0):
            bad = copy.deepcopy(captures); bad['profile']['seed'] = invalid
            self.assertEqual(capture.evaluate_captures(bad, **args)['rc'], 2)
        captures['views'][1]['candidate_rgb_sha256'] = 'c'*64
        self.assertEqual(capture.evaluate_captures(captures, **args)['rc'], 5)
        captures['candidate_sha256'] = '0'*64
        self.assertEqual(capture.evaluate_captures(captures, **args)['rc'], 4)

    def test_u4_timeout_reaps_owned_processes(self):
        code = ('import os,time,pathlib; pathlib.Path("parent.pid").write_text(str(os.getpid())); child=os.fork(); '
                'pathlib.Path("child.pid").write_text(str(child)) if child else None; time.sleep(60)')
        owned = []
        original = capture.subprocess.Popen
        def launch(*args, **kwargs):
            proc = original(*args, **kwargs)
            owned.append(proc)
            return proc
        def cleanup():
            # A killed-rule mutant deliberately breaks production cleanup.
            # The witness must still reap that exact owned group and its pipes.
            for proc in owned:
                try: os.killpg(proc.pid, 9)
                except ProcessLookupError: pass
                proc.wait(timeout=2)
                if proc.stderr: proc.stderr.close()
        self.addCleanup(cleanup)
        started = time.monotonic()
        with mock.patch.object(capture.subprocess, 'Popen', side_effect=launch):
            with self.assertRaises(GlbError) as error:
                capture.run_owned([sys.executable, '-I', '-c', code], timeout_s=1, cwd=self.root)
        self.assertEqual(error.exception.rc, 6)
        self.assertLess(time.monotonic() - started, 5)
        child = int((self.root/'child.pid').read_text())
        stat = Path('/proc')/str(child)/'stat'
        deadline = time.monotonic() + 2
        while True:
            try: state = stat.read_text().split()[2]
            except FileNotFoundError: state = 'gone'
            if state in ('gone', 'Z'): break
            self.assertLess(time.monotonic(), deadline, 'owned grandchild survived')
            time.sleep(0.01)
        self.assertIn(state, ('gone', 'Z'))
        self.assertFalse(self.target.exists())

    def test_atomic_publish_unavailable_refuses_without_fallback(self):
        import tools.blender.helpers_contracts as contracts
        with mock.patch.object(contracts.ctypes, 'CDLL', return_value=object()):
            self.assertRefusal(self.separate(), 3)

    def test_closed_primitive_rejects_unknown_fields(self):
        from tools.blender.helpers_contracts import closed
        with self.assertRaises(GlbError): closed({'extra': 1}, ())

    def test_closed_schemas_and_records(self):
        from tools.blender.glb_schemas import SEPARATION_SCHEMA, RECEIPT_SCHEMA, CAPTURE_SCHEMA
        for schema, name in ((SEPARATION_SCHEMA, 'glb-separation'),
                             (RECEIPT_SCHEMA, 'glb-receipt'), (CAPTURE_SCHEMA, 'glb-capture')):
            self.assertEqual(schema['$id'], 'urn:af:' + name + ':v1')
            self.assertEqual(schema['properties']['schema']['const'], 'af.' + name + '/v1')
            self.assertIs(schema['additionalProperties'], False)
        report = self.separate(); self.assertEqual(report['rc'], 0)
        io.validate_separation(report)
        for bad in ({**report, 'extra': 1}, {**report, 'rc': False},
                    {**report, 'appearance': {'rc': 0, 'verdict': 'PASS'}},
                    {**report, 'normalization': {'applied': True}}):
            with self.assertRaises(GlbError): io.validate_separation(bad)


class GLBRenderTests(GLBFixture, unittest.TestCase):
    def test_u4_real_cpu_visual_roundtrip(self):
        self.assertEqual(self.separate()['rc'], 0)
        result = capture.compare_visuals(self.source, self.target/'visual.glb', roles=self.roles, timeout_s=90)
        self.assertEqual(result['rc'], 0, result)
        self.assertTrue(any(v['raw_visual_rgb_sha256'] != v['diagnostic_rgb_sha256'] for v in result['captures']['views']))
        # Use actual measured helper-enabled RGB as the leaked candidate.
        # This independently exercises the render oracle after the structural veto.
        leaked = copy.deepcopy(result['captures'])
        leaked['candidate_sha256'] = digest(self.data)
        for view in leaked['views']: view['candidate_rgb_sha256'] = view['diagnostic_rgb_sha256']
        self.assertEqual(capture.evaluate_captures(leaked, source_sha256=digest(self.data),
                                                 candidate_sha256=digest(self.data))['rc'], 5)

    def test_u4_opaque_col_blocks_visual_fails(self):
        result = capture.compare_visuals(self.source, self.source, roles=self.roles, timeout_s=90)
        self.assertEqual(result['rc'], 5, result)

    def test_u4_opaque_occ_blocks_visual_fails(self):
        self.doc, self.binary, self.roles = fixture(True, 'occluder'); self.save()
        self.assertEqual(self.separate()['rc'], 0)
        good = capture.compare_visuals(self.source, self.target/'visual.glb', roles=self.roles, timeout_s=90)
        self.assertEqual(good['rc'], 0, good)
        bad = capture.compare_visuals(self.source, self.source, roles=self.roles, timeout_s=90)
        self.assertEqual(bad['rc'], 5, bad)


def load_tests(loader, tests, pattern):
    suite = loader.loadTestsFromTestCase(GLBTests)
    for name in ('test_u4_real_cpu_visual_roundtrip', 'test_u4_opaque_col_blocks_visual_fails', 'test_u4_opaque_occ_blocks_visual_fails'):
        suite.addTest(GLBRenderTests(name))
    return suite


if __name__ == '__main__': unittest.main()
