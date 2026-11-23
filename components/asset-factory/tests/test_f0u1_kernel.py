"""Independent kernel boundary oracles, including refusal and resource bounds."""
import contextlib
import copy
import hashlib
import io
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asset_factory import core, contracts
from test_f0u1_contracts import artifact, provider, request, result


class KernelTests(unittest.TestCase):
    def rejects(self, value, schema, code=None):
        with self.assertRaises(core.ContractError) as caught:
            core.validate(value, schema)
        self.assertEqual(caught.exception.rc, 2)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_error_and_frozen_constants(self):
        self.assertEqual(core.RC, (0, 2, 3, 4, 5, 6))
        self.assertEqual(core.STATUS, {0:'OK', 2:'ERROR', 3:'INCOMPLETE', 4:'STALE',
                                      5:'FAIL', 6:'BUDGET_EXCEEDED'})
        self.assertEqual(core.VERDICT, {0:'PASS', 2:'ERROR', 3:'CANNOT_SEE', 4:'CANNOT_SEE',
                                       5:'FAIL', 6:'CANNOT_SEE'})
        error = core.ContractError('missing_required', '$/a')
        self.assertEqual(error.code, 'missing_required')
        self.assertEqual(error.path, '$/a')
        self.assertEqual(error.as_dict(), {'rc':2, 'status':'ERROR',
                                          'reason_codes':['missing_required'], 'path':'$/a'})
        self.rejects({}, {'required':['a']}, 'missing_required')
        self.rejects({'extra':0}, {'additionalProperties':False}, 'unknown_property')

    def test_type_const_enum_and_union_identity(self):
        samples = {'null':None, 'boolean':False, 'integer':0, 'string':'', 'array':[], 'object':{}}
        for kind, value in samples.items():
            core.validate(value, {'type':kind})
            core.validate(value, {'type':[kind, 'string'] if kind != 'string' else ['string']})
            for other, candidate in samples.items():
                if other != kind:
                    self.rejects(candidate, {'type':kind})
        for value in (None, True, 1, 'x', [False, 0], {'a':[True, 0]}):
            core.validate(value, {'const':value})
            core.validate(value, {'enum':[value]})
        for left, right in ((True,1), (False,0), ([True],[1]), ({'a':False},{'a':0}), ('1',1)):
            self.rejects(left, {'const':right})
            self.rejects(left, {'enum':[right]})
        core.validate(True, {'oneOf':[{'type':'boolean'}, {'type':'integer'}]})
        self.rejects(0, {'oneOf':[{'type':'integer'}, {'const':0}]})
        self.rejects(None, {'oneOf':[{'type':'boolean'}, {'type':'integer'}]})

    def test_object_and_array_assertions(self):
        schema = {'type':'object', 'required':['a'], 'properties':{'a':{'type':'integer'}},
                  'additionalProperties':False}
        core.validate({'a':1}, schema)
        for value in ({}, {'a':'1'}, {'a':1, 'b':0}):
            self.rejects(value, schema)
        core.validate({'extra':True}, {'additionalProperties':{'type':'boolean'}})
        self.rejects({'extra':1}, {'additionalProperties':{'type':'boolean'}})
        core.validate([0, 1], {'type':'array', 'minItems':2, 'items':{'type':'integer'}})
        self.rejects([0], {'minItems':2})
        self.rejects([0, False], {'items':{'type':'integer'}})
        # Object/array keywords do not silently assert the type of a scalar.
        core.validate(0, {'required':['x'], 'minItems':1})

    def test_schema_preflight_covers_unvisited_branches(self):
        core.validate({}, {'$schema':'https://json-schema.org/draft/2020-12/schema',
                           '$id':'urn:af:synthetic:v1'})
        bad = [True, [], {'unsupported':0}, {'type':'number'}, {'type':[]}, {'type':['integer','integer']},
               {'type':True}, {'properties':[]}, {'$defs':[]}, {'required':'a'},
               {'required':[0]}, {'required':['a','a']}, {'enum':[]}, {'enum':[True, True]},
               {'enum':'x'}, {'minItems':True}, {'minItems':-1}, {'additionalProperties':0},
               {'items':[]}, {'oneOf':[]}, {'oneOf':{}}, {'$schema':0}, {'$id':''},
               {'properties':{'unused':{'not':{}}}}, {'$defs':{'unused':{'not':{}}}},
               {'oneOf':[{}, {'properties':{'unused':{'unsupported':True}}}]}]
        for schema in bad:
            with self.subTest(schema=schema):
                self.rejects({}, schema)
        # bool and integer constants are distinct enum members.
        core.validate(1, {'enum':[True, 1]})

    def test_local_refs_siblings_and_recursive_bounds(self):
        schema = {'$defs':{'a/b~c':{'type':'integer'}}, '$ref':'#/$defs/a~1b~0c', 'const':1}
        core.validate(1, schema)
        self.rejects(2, schema)
        for ref in ('https://example.invalid/s', 'other.json', '#/$defs/missing', '#/~2', '#/const',
                    '#/oneOf/01', '#/oneOf/9'):
            self.rejects(0, {'$ref':ref, 'const':0, 'oneOf':[{}]})
        self.rejects(0, {'$ref':'#'})
        recursive = {'$defs':{'node':{'oneOf':[{'type':'null'}, {'type':'object',
                       'required':['next'], 'properties':{'next':{'$ref':'#/$defs/node'}},
                       'additionalProperties':False}]}}, '$ref':'#/$defs/node'}
        core.validate({'next':{'next':None}}, recursive)
        self.rejects({'next':1}, recursive)
        chain = {'$ref':'#/$defs/n0', '$defs':{}}
        for i in range(200):
            chain['$defs']['n'+str(i)] = {'$ref':'#/$defs/n'+str(i+1)}
        chain['$defs']['n200'] = {'type':'integer'}
        self.rejects(0, chain)
        deep = None
        for _ in range(200): deep = [deep]
        with self.assertRaises(core.ContractError): core.canonical_json(deep)

    def test_canonical_types_cycles_and_exact_digests(self):
        self.assertEqual(core.canonical_json({'z':False, 'é':0}), '{"z":false,"é":0}\n')
        self.assertNotEqual(core.sha256_json(True), core.sha256_json(1))
        self.assertEqual(core.sha256_json({'b':1,'a':0}), core.sha256_json({'a':0,'b':1}))
        cycle = []; cycle.append(cycle)
        for bad in (1.0, float('inf'), float('nan'), {1:0}, (), b'0', {0}, cycle, '\ud800'):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(core.ContractError): core.canonical_json(bad)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data'
            path.write_bytes(b'x' * (1024 * 1024 + 7))
            self.assertEqual(core.sha256_file(path), hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaises(core.ContractError): core.sha256_file(path / 'missing')

    def test_strict_load_nested_duplicates_constants_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.json'
            raw = b'{"zero":0,"flag":false,"unknown":null}'
            path.write_bytes(raw)
            self.assertEqual(core.load_json(path, max_bytes=len(raw)), json.loads(raw))
            with self.assertRaises(core.ContractError): core.load_json(path, max_bytes=len(raw)-1)
            path.write_bytes(b'0')
            for limit in (0, -1, True, '100'):
                with self.assertRaises(core.ContractError): core.load_json(path, max_bytes=limit)
            for raw in (b'{"a":{"x":0,"x":1}}', b'1e0', b'0.0', b'Infinity', b'-Infinity',
                        b'NaN', b'{}{}', b'\xff'):
                path.write_bytes(raw)
                with self.assertRaises(core.ContractError): core.load_json(path, max_bytes=1024)
            with self.assertRaises(core.ContractError): core.load_json(path / 'missing', max_bytes=100)

    def test_aggregation_all_pairs_and_order(self):
        def expected(values):
            if 2 in values: return 2
            if 5 in values: return 5
            if not values or set(values) & {3,4,6}: return 3
            return 0
        for pair in itertools.product(core.RC, repeat=2):
            self.assertEqual(core.worst_rc(iter(pair)), expected(pair))
        for bad in (True, False, 1, -1, 7, 0.0, '0', None, []):
            self.assertEqual(core.worst_rc([0, bad]), 2)

    def test_atomic_publication_order_and_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'caller.json'
            target.write_bytes(b'caller sentinel')
            replace, fsync = core.os.replace, core.os.fsync
            events = []
            def synced(fd):
                self.assertEqual(target.read_bytes(), b'caller sentinel')
                events.append('fsync'); return fsync(fd)
            def replaced(source, destination):
                self.assertEqual(Path(source).parent, target.parent)
                self.assertNotEqual(Path(source), target)
                self.assertEqual(Path(source).read_bytes(), b'{"a":0}\n')
                self.assertEqual(target.read_bytes(), b'caller sentinel')
                events.append('replace'); return replace(source, destination)
            with mock.patch.object(core.os, 'fsync', side_effect=synced), \
                 mock.patch.object(core.os, 'replace', side_effect=replaced):
                core.atomic_write_json(target, {'a':0})
            self.assertEqual(events, ['fsync', 'replace'])
            self.assertEqual(target.read_bytes(), b'{"a":0}\n')
            for operation in ('fsync', 'replace'):
                target.write_bytes(b'caller sentinel')
                with mock.patch.object(core.os, operation, side_effect=OSError('planted')):
                    with self.assertRaises(core.ContractError): core.atomic_write_json(target, {'a':0})
                self.assertEqual(target.read_bytes(), b'caller sentinel')
                self.assertEqual(list(target.parent.iterdir()), [target])
            self.rejects({'x':True}, {'properties':{'x':{'type':'integer'}}})

    def test_emit_and_cli_parse_refusals(self):
        for rc in core.RC:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(core.emit({'rc':rc, 'status':core.STATUS[rc]}), rc)
            self.assertEqual(output.getvalue(), core.canonical_json({'rc':rc, 'status':core.STATUS[rc]}))
        for value in ({}, {'rc':1}, {'rc':True}, {'rc':0.0}, [], {'rc':0,'bad':1.5}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                with self.assertRaises(core.ContractError): core.emit(value)
            self.assertEqual(output.getvalue(), '')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'in.json'
            for raw in ('{', '{"x":0,"x":1}', 'true', '1.0'):
                path.write_text(raw); before = path.read_bytes()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(contracts.main(['--provider', str(path)]), 2)
                record = json.loads(output.getvalue())
                self.assertEqual(record['rc'], 2)
                self.assertEqual(record['status'], 'ERROR')
                self.assertEqual(path.read_bytes(), before)
            with contextlib.redirect_stdout(io.StringIO()):
                path.write_text(json.dumps(provider()))
                self.assertEqual(contracts.main(['--provider',str(path), '--slot','generate']), 2)

    def test_schema_branch_rules_directly(self):
        provider_schema = core.load_json(ROOT/'slots'/'provider.schema.json', max_bytes=100000)
        slot_schema = core.load_json(ROOT/'slots'/'generate.schema.json', max_bytes=100000)
        core.validate(provider(runnable=False), provider_schema)
        core.validate(result(), slot_schema)
        blocked = provider(runnable=False); del blocked['blocked_reason']
        self.rejects(blocked, provider_schema)
        missing_receipt_identity = result(); del missing_receipt_identity['receipts'][0]['schema']
        self.rejects(missing_receipt_identity, slot_schema)

    def test_cli_module_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'in.json'
            # -I intentionally requires explicit import bootstrapping for this uninstalled component.
            script = ('import runpy,sys;sys.path.insert(0,sys.argv.pop(1));'
                      'runpy.run_module("asset_factory.contracts",run_name="__main__")')
            for value, rc in ((provider(),0), ({},2)):
                path.write_text(json.dumps(value)); before = path.read_bytes()
                for entrypoint in ([sys.executable, '-I', '-B', '-c', script, str(ROOT)],
                                   [sys.executable, '-B', '-m', 'asset_factory.contracts']):
                    process = subprocess.run([*entrypoint, '--provider', str(path)],
                                             cwd=ROOT, capture_output=True, timeout=10)
                    self.assertEqual(process.returncode, rc, process.stderr)
                    record = json.loads(process.stdout)
                    self.assertEqual(record['rc'], rc)
                    self.assertEqual(process.stdout.decode(), core.canonical_json(record))
                    self.assertEqual(process.stderr, b'')
                    self.assertEqual(path.read_bytes(), before)


class ContractBoundaryTests(unittest.TestCase):
    def test_selector_binding_and_scalar_params(self):
        for slot in core.SLOTS:
            for kind, make in (('request',request), ('result',result)):
                payload = make(slot)
                contracts.validate_slot(payload, slot, kind)
                with self.assertRaises(core.ContractError):
                    contracts.validate_slot(payload, slot, 'result' if kind == 'request' else 'request')
        for value in ('text', 0, True, False):
            payload = request(); payload['params']['custom'] = value
            contracts.validate_slot(payload, 'generate', 'request')
        for value in ([], {}, None, 1.0):
            payload = request(); payload['params']['custom'] = value
            with self.assertRaises(core.ContractError): contracts.validate_slot(payload, 'generate', 'request')

    def test_declared_measurement_does_not_authenticate_evidence(self):
        for kind in ('model','tool'):
            for runnable in (True, False):
                payload = provider(kind, runnable)
                contracts.validate_provider(payload)
        for field in ('commercial','gated'):
            payload = provider(); payload[field] = None
            # Rights are evaluated by U2; unknown rights remain representable here.
            contracts.validate_provider(payload)
        payload = provider(); payload['runtime'].update(ram_peak_gb_measured=0,
                            ram_peak_measurement_ref={'path':'absent-proof.json','sha256':'a'*64})
        contracts.validate_provider(payload)
        for key, value in (('path',''), ('sha256','bad')):
            mutant = copy.deepcopy(payload); mutant['runtime']['ram_peak_measurement_ref'][key] = value
            with self.assertRaises(core.ContractError): contracts.validate_provider(mutant)

    def test_strings_digests_and_partial_refusal_roles(self):
        for field in ('path','media_type','sha256'):
            payload = request(); payload['inputs'][0][field] = ''
            with self.assertRaises(core.ContractError): contracts.validate_slot(payload,'generate','request')
        payload = request(); payload['inputs'] = [artifact('prompt')]
        contracts.validate_slot(payload, 'generate', 'request')
        payload['inputs'][0]['text'] = ' '
        with self.assertRaises(core.ContractError): contracts.validate_slot(payload,'generate','request')
        payload = result('rig'); payload.update(rc=3,status='INCOMPLETE',receipts=[])
        payload['outputs'].pop()
        contracts.validate_slot(payload, 'rig', 'result')
        payload['outputs'].append(copy.deepcopy(payload['outputs'][0]))
        with self.assertRaises(core.ContractError): contracts.validate_slot(payload,'rig','result')
        payload = provider(); payload['blocked_reason'] = ' '
        payload['runnable'] = False
        with self.assertRaises(core.ContractError): contracts.validate_provider(payload)
