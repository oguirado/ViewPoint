"""Frozen F0-U1 consumer oracles; no downstream runtime or models required."""
import copy
import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # Explicit component path also works with python -I.
SLOTS = ('generate', 'refine', 'topology', 'uv_texture', 'parts', 'rig', 'motion', 'render')
# Declared W3 expansion; keep the F0 fixture slots and their identities frozen.
NEW_SCHEMA_IDS = {'cad_code': 'cad-code', 'collision': 'collision', 'physics': 'physics',
                  'procedural': 'procedural', 'scene_compose': 'scene-compose',
                  'denoise': 'denoise', 'texture': 'texture'}
INPUTS = {'generate': ['image'], 'refine': ['coarse_glb', 'reference_image'],
          'topology': ['raw_glb'], 'uv_texture': ['glb', 'reference_image'],
          'parts': ['glb'], 'rig': ['glb'], 'motion': ['rig_glb', 'video'],
          'render': ['blend_scene']}
OUTPUTS = {'generate': ['mesh_glb'], 'refine': ['refined_glb'],
           'topology': ['topology_glb'], 'uv_texture': ['textured_glb', 'albedo', 'normal', 'orm'],
           'parts': ['parts_glb', 'part_map'], 'rig': ['rigged_glb', 'skeleton'],
           'motion': ['action_bvh'], 'render': ['frame_set', 'render_receipt']}
H = 'a' * 64


def artifact(role):
    if role in ('prompt', 'text'):
        return {'role': role, 'text': 'synthetic reference'}
    return {'role': role, 'path': 'synthetic.bin', 'media_type': 'application/octet-stream', 'sha256': H}


def request(slot='generate'):
    return {'schema': f'af.{slot}/v1', 'kind': 'request', 'slot': slot,
            'request_id': 'synthetic-request', 'provider_id': 'synthetic-tool',
            'provider_revision': 'a' * 40, 'inputs': [artifact(r) for r in INPUTS[slot]],
            'params': {'seed': 42, 'quality': 'draft', 'enabled': True},
            'context': {'jurisdiction': 'ES', 'commercial_use': False,
                        'policy_sha256': H, 'toolchain_sha256': H, 'timeout_s': 10}}


def result(slot='generate'):
    p = request(slot)
    for k in ('inputs', 'params', 'context'):
        del p[k]
    p.update(kind='result', rc=0, status='OK', outputs=[artifact(r) for r in OUTPUTS[slot]],
             receipts=[{'schema': 'af.synthetic/v1', 'path': 'synthetic.json', 'sha256': H}],
             metrics={'wall_ms': None, 'peak_rss_bytes': None},
             absence_reasons={'wall_ms': 'not measured', 'peak_rss_bytes': 'not measured'})
    return p


def provider(kind='model', runnable=True):
    p = {'schema': 'af.provider/v1', 'id': 'synthetic', 'slot': 'generate',
         'kind': kind, 'runnable': runnable, 'revision': 'b' * 40,
         'licence': {'code': 'MIT', 'weights': 'MIT', 'deps': []},
         'territory_excluded': [], 'commercial': True, 'gated': False,
         'weights': [{'repo': 'synthetic/model', 'revision': 'c' * 40,
                      'files': [{'path': 'synthetic.weights', 'sha256': H}]}] if kind == 'model' else [],
         'runtime': {'container': 'build/Dockerfile', 'ram_peak_gb_measured': None,
                     'ram_peak_measurement_ref': None}, 'known_issues': []}
    if not runnable:
        p.update(id='hunyuan3d21', blocked_reason='licence_territory', revision=None,
                 weights=[], commercial=None, gated=None, territory_excluded=['EU', 'UK', 'KR'])
    return p


class SchemaSurfaceTests(unittest.TestCase):
    def test_schema_inventory_and_identity(self):
        identities = {name: name for name in (*SLOTS, 'provider')}
        identities.update(NEW_SCHEMA_IDS)
        self.assertEqual({p.name for p in (ROOT / 'slots').glob('*.schema.json')},
                         {name + '.schema.json' for name in identities})
        for name, identity in identities.items():
            with self.subTest(name=name):
                schema = json.loads((ROOT / 'slots' / (name + '.schema.json')).read_text())
                self.assertEqual(schema['$id'], f'urn:af:{identity}:v1')
                self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')

    def test_local_refs_closed_objects_and_subset(self):
        allowed = {'$schema', '$id', '$defs', '$ref', 'type', 'const', 'enum', 'required',
                   'properties', 'additionalProperties', 'items', 'minItems', 'oneOf'}
        for path in sorted((ROOT / 'slots').glob('*.schema.json')):
            schema = json.loads(path.read_text())
            def walk(node):
                self.assertIsInstance(node, dict)
                self.assertFalse(set(node) - allowed, (path, set(node) - allowed))
                if '$ref' in node:
                    self.assertTrue(node['$ref'].startswith('#/'))
                    target = schema
                    for key in node['$ref'][2:].split('/'):
                        target = target[key.replace('~1', '/').replace('~0', '~')]
                    self.assertIsInstance(target, dict)
                if node.get('type') == 'object' and 'properties' in node:
                    self.assertIs(node['additionalProperties'], False)
                for key in ('properties', '$defs'):
                    for child in node.get(key, {}).values():
                        walk(child)
                for key in ('items', 'additionalProperties'):
                    if isinstance(node.get(key), dict):
                        walk(node[key])
                for child in node.get('oneOf', []):
                    walk(child)
            walk(schema)


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = importlib.import_module('asset_factory.core')
        cls.contracts = importlib.import_module('asset_factory.contracts')

    def reject(self, payload, slot='generate', kind='request', code=None):
        with self.assertRaises(self.core.ContractError) as cm:
            self.contracts.validate_slot(payload, slot, kind)
        self.assertEqual(cm.exception.rc, 2)
        if code is not None:
            self.assertEqual(cm.exception.code, code)

    def reject_provider(self, payload, code=None):
        with self.assertRaises(self.core.ContractError) as cm:
            self.contracts.validate_provider(payload)
        self.assertEqual(cm.exception.rc, 2)
        if code is not None:
            self.assertEqual(cm.exception.code, code)

    def test_slot_contracts_validate_each_stage(self):
        for slot in SLOTS:
            for kind, make in [('request', request), ('result', result)]:
                with self.subTest(slot=slot, kind=kind):
                    p = make(slot)
                    self.assertIsNone(self.contracts.validate_slot(p, slot, kind))
                    for key, value in [('slot', 'wrong'), ('schema', 'af.wrong/v1'),
                                       ('kind', 'wrong'), ('unknown', True)]:
                        q = copy.deepcopy(p); q[key] = value
                        self.reject(q, slot, kind)
                    for key in p:
                        q = copy.deepcopy(p); del q[key]
                        self.reject(q, slot, kind)
                    q = copy.deepcopy(p)
                    q['inputs' if kind == 'request' else 'outputs'][0]['role'] = 'wrong'
                    self.reject(q, slot, kind)
                    q = copy.deepcopy(p)
                    q['inputs' if kind == 'request' else 'outputs'][0]['extra'] = True
                    self.reject(q, slot, kind)
        self.reject(request(), 'not-a-slot', code='unknown_slot')
        self.reject(request(), kind='not-a-kind', code='unknown_kind')

    def test_all_role_multiplicities(self):
        for slot in SLOTS:
            for kind, make, key in [('request', request, 'inputs'), ('result', result, 'outputs')]:
                p = make(slot)
                for i in range(len(p[key])):
                    q = copy.deepcopy(p); q[key].pop(i)
                    self.reject(q, slot, kind)
                q = copy.deepcopy(p); q[key].append(copy.deepcopy(q[key][0]))
                self.reject(q, slot, kind)
        for slot, optional in [('generate', 'texture'), ('topology', 'normal_map')]:
            p = result(slot); p['outputs'].append(artifact(optional))
            self.contracts.validate_slot(p, slot, 'result')
        p = result('motion'); p['outputs'] = [artifact('action_glb')]
        self.contracts.validate_slot(p, 'motion', 'result')
        p['outputs'].append(artifact('action_bvh'))
        self.reject(p, 'motion', 'result')

    def test_provider_requires_licence_territory_revision(self):
        for kind, runnable in [('model', True), ('tool', True), ('model', False)]:
            p = provider(kind, runnable)
            self.assertIsNone(self.contracts.validate_provider(p))
            for field in p:
                if field == 'blocked_reason' and runnable:
                    continue
                q = copy.deepcopy(p); del q[field]
                self.reject_provider(q)
            q = copy.deepcopy(p); q['unknown'] = True
            self.reject_provider(q)
        for key in ('code', 'weights', 'deps'):
            p = provider(); del p['licence'][key]
            self.reject_provider(p)
        p = provider(); p['licence']['deps'] = [{'name': 'synthetic', 'licence': 'NC',
             'commercial': False, 'territory_excluded': [], 'gated': False, 'used_by': ['uv_texture']}]
        self.contracts.validate_provider(p)
        for field in p['licence']['deps'][0]:
            q = copy.deepcopy(p); del q['licence']['deps'][0][field]
            self.reject_provider(q)

    def test_dependency_unknown_rights_are_nullable_not_false(self):
        p = provider()
        dep = {'name': 'synthetic', 'licence': 'unknown', 'commercial': True,
               'territory_excluded': [], 'gated': False, 'used_by': ['uv_texture']}
        p['licence']['deps'] = [dep]
        for field in ('commercial', 'gated'):
            original = dep[field]
            dep[field] = None
            self.contracts.validate_provider(p)
            dep[field] = 'unknown'
            self.reject_provider(p)
            del dep[field]
            self.reject_provider(p)
            dep[field] = original

    def test_provider_rejects_unpinned_weights(self):
        self.contracts.validate_provider(provider())
        for revision in ('main', 'master', 'latest', 'HEAD', ''):
            for target in ('provider', 'weight'):
                p = provider()
                if target == 'provider': p['revision'] = revision
                else: p['weights'][0]['revision'] = revision
                self.reject_provider(p)
            p = request(); p['provider_revision'] = revision
            self.reject(p)
        p = provider(); del p['weights'][0]['files'][0]['sha256']
        self.reject_provider(p)
        for digest in ('', 'a' * 63, 'g' * 64):
            p = provider(); p['weights'][0]['files'][0]['sha256'] = digest
            self.reject_provider(p)
        for change in ('empty_weights', 'empty_files', 'tool_weights', 'missing_block_reason'):
            p = provider('tool' if change == 'tool_weights' else 'model', change != 'missing_block_reason')
            if change == 'empty_weights': p['weights'] = []
            elif change == 'empty_files': p['weights'][0]['files'] = []
            elif change == 'tool_weights': p['weights'] = provider()['weights']
            else: del p['blocked_reason']
            self.reject_provider(p)

    def test_generate_and_motion_input_exclusivity(self):
        for slot, alternatives, fixed in [('generate', ('image', 'prompt'), []),
                                           ('parts', ('glb', 'image'), []),
                                           ('motion', ('video', 'text'), ['rig_glb'])]:
            for role in alternatives:
                p = request(slot); p['inputs'] = [artifact(r) for r in fixed + [role]]
                self.contracts.validate_slot(p, slot, 'request')
            for roles in (fixed, fixed + list(alternatives)):
                p = request(slot); p['inputs'] = [artifact(r) for r in roles]
                self.reject(p, slot, code='input_exclusivity')
        p = request(); p['inputs'] = [{'role': 'prompt', 'path': 'not-text'}]
        self.reject(p)

    def test_result_requires_receipt_for_ok(self):
        for rc, status in [(0, 'OK'), (2, 'ERROR'), (3, 'INCOMPLETE'), (4, 'STALE'),
                            (5, 'FAIL'), (6, 'BUDGET_EXCEEDED')]:
            p = result(); p.update(rc=rc, status=status)
            if rc: p.update(outputs=[], receipts=[])
            self.contracts.validate_slot(p, 'generate', 'result')
            p['status'] = 'FAIL' if status != 'FAIL' else 'OK'
            self.reject(p, kind='result', code='rc_status_mismatch')
        p = result(); p['receipts'] = []
        self.reject(p, kind='result', code='ok_without_receipt')
        for rc in (1, True):
            p = result(); p['rc'] = rc
            self.reject(p, kind='result')
        for field in ('schema', 'path', 'sha256'):
            p = result(); del p['receipts'][0][field]
            self.reject(p, kind='result')

    def test_metrics_unknown_is_not_zero(self):
        self.contracts.validate_slot(result(), 'generate', 'result')
        for key in ('wall_ms', 'peak_rss_bytes'):
            for value in (0, 1):
                p = result(); p['metrics'][key] = value
                self.contracts.validate_slot(p, 'generate', 'result')
            for value in (-1, 1.5, True):
                p = result(); p['metrics'][key] = value
                self.reject(p, kind='result')
            for reason in (None, ''):
                p = result()
                if reason is None: del p['absence_reasons'][key]
                else: p['absence_reasons'][key] = reason
                self.reject(p, kind='result', code='null_metric_without_reason')
        p = provider(); p['runtime'].update(ram_peak_gb_measured=1,
                         ram_peak_measurement_ref={'path': 'peak.json', 'sha256': H})
        self.contracts.validate_provider(p)
        p['runtime']['ram_peak_measurement_ref'] = None
        self.reject_provider(p)
        for value in (-1, True, 1.5):
            p = provider(); p['runtime']['ram_peak_gb_measured'] = value
            self.reject_provider(p)

    def test_context_params_and_artifact_types(self):
        self.contracts.validate_slot(request(), 'generate', 'request')
        for key, values in [('timeout_s', [True, 0, -1]), ('commercial_use', [None, 0, 'false']),
                            ('jurisdiction', ['']), ('policy_sha256', ['bad']),
                            ('toolchain_sha256', ['bad'])]:
            for value in values:
                p = request(); p['context'][key] = value
                self.reject(p)
        for value in ([], {}, None, 1.5):
            p = request(); p['params']['custom'] = value
            self.reject(p)
        for field in ('path', 'media_type', 'sha256'):
            p = request(); del p['inputs'][0][field]
            self.reject(p)
        for field in ('request_id', 'provider_id'):
            p = request(); p[field] = ''
            self.reject(p)

    def test_schema_refs_and_canonical_roundtrip(self):
        obj = {'z': None, 'é': [True, 0], 'a': 'unicode'}
        canonical = self.core.canonical_json(obj)
        self.assertEqual(canonical, '{"a":"unicode","z":null,"é":[true,0]}\n')
        self.assertEqual(self.core.sha256_json(obj), hashlib.sha256(canonical.encode()).hexdigest())
        for invalid in ({'x': 1.0}, {'x': float('nan')}, {1: 'key'}, {'x': (1,)}):
            with self.assertRaises(self.core.ContractError): self.core.canonical_json(invalid)
        self.core.validate({'a': 1}, {'type': 'object', 'required': ['a'],
             'properties': {'a': {'$ref': '#/$defs/int'}}, 'additionalProperties': False,
             '$defs': {'int': {'type': 'integer'}}})
        for payload, schema in [(True, {'type': 'integer'}), (1, {'const': True}),
                                (1, {'enum': [True]}), ({}, {'$ref': '#/$defs/missing'}),
                                ({}, {'$ref': 'https://example.invalid/schema'}),
                                ({}, {'not': {}}), ({}, {'minimum': 0}),
                                ({}, {'properties': {'unused': {'unsupported': 1}}})]:
            with self.assertRaises(self.core.ContractError): self.core.validate(payload, schema)
        for slot in SLOTS:
            schema = self.core.load_json(ROOT / 'slots' / (slot + '.schema.json'), max_bytes=100000)
            for make in (request, result): self.core.validate(make(slot), schema)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'input.json'
            for raw in ('{"a":1,"a":2}', '{"a":1.0}', '{"a":NaN}', '{'):
                p.write_text(raw)
                with self.assertRaises(self.core.ContractError): self.core.load_json(p, max_bytes=1000)
            p.write_text('{"a":1}')
            self.assertEqual(self.core.load_json(p, max_bytes=100), {'a': 1})
            with self.assertRaises(self.core.ContractError): self.core.load_json(p, max_bytes=1)

    def test_worst_rc_non_numeric_precedence(self):
        cases = [([], 3), ([0], 0), ([0, 4, 6], 3), ([5, 6], 5), ([2, 5], 2),
                 ([1], 2), ([True], 2), ([False], 2), ([7], 2)]
        for values, expected in cases:
            self.assertEqual(self.core.worst_rc(values), expected)

    def test_refusal_preserves_output_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'caller.json'; p.write_bytes(b'caller sentinel')
            with self.assertRaises(self.core.ContractError):
                self.core.atomic_write_json(p, {'bad': 1.5})
            self.assertEqual(p.read_bytes(), b'caller sentinel')
            with mock.patch.object(self.core.os, 'replace', side_effect=OSError('planted failure')):
                with self.assertRaises((self.core.ContractError, OSError)):
                    self.core.atomic_write_json(p, {'good': 1})
            self.assertEqual(p.read_bytes(), b'caller sentinel')
            self.assertEqual(list(Path(td).iterdir()), [p])
            self.core.atomic_write_json(p, {'good': 1})
            self.assertEqual(p.read_text(), '{"good":1}\n')
            self.assertEqual(self.core.sha256_file(p), hashlib.sha256(p.read_bytes()).hexdigest())

    def test_cli_exact_rc_and_canonical_refusals(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'input.json'
            for good in (True, False):
                payload = request()
                if not good: payload['unexpected'] = True
                p.write_text(json.dumps(payload)); before = p.read_bytes()
                output = io.StringIO()
                with mock.patch('sys.stdout', output):
                    rc = self.contracts.main(['--slot', 'generate', '--kind', 'request', '--in', str(p)])
                self.assertEqual(rc, 0 if good else 2)
                self.assertEqual(p.read_bytes(), before)
                record = json.loads(output.getvalue())
                self.assertEqual(record['rc'], rc)
                self.assertEqual(output.getvalue(), self.core.canonical_json(record))
            p.write_text(json.dumps(provider()))
            output = io.StringIO()
            with mock.patch('sys.stdout', output):
                self.assertEqual(self.contracts.main(['--provider', str(p)]), 0)
            for argv in ([], ['--unknown'], ['--slot', 'generate']):
                with mock.patch('sys.stdout', io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
                    self.assertEqual(self.contracts.main(argv), 2)

    def test_gate_rejects_planted_schema_failure(self):
        schema = self.core.load_json(ROOT / 'slots' / 'provider.schema.json', max_bytes=100000)
        p = provider(); del p['licence']
        with self.assertRaises(self.core.ContractError): self.core.validate(p, schema)
        mutant = copy.deepcopy(schema); mutant['required'].remove('licence')
        self.core.validate(p, mutant)  # Planted omission now accepted: the oracle must detect it.
        self.assertNotEqual(schema['required'], mutant['required'])


if __name__ == '__main__':
    unittest.main()
