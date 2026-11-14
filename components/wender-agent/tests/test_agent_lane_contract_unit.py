# @module {"id":"wa.tests.agent_lane_contract","layer":0,"owner":"ARCH","deps":["wa.tools.memoryweb_gate"],"book":"HB-WA-a0-arch"}
"""Offline structural contract; acceptance grants no runtime authority.

Producer: Holdings app shell src/main/agent-lane-observation.js
and Holdings app shell src/main/agent-lane-channel.js.
Fixture origins: mw contracts_src/agent_lane_bench_default.json
and mw contracts_src/agent_lane_bench_terminal.json.
Only shape is established: no digest recomputation, live pixel binding, provenance,
authorization, incarnation agreement, count arithmetic, or runtime admission.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import unittest

from tools import memoryweb_gate as gate

ROOT = Path(__file__).resolve().parents[1]
KEYWORDS = frozenset('''$schema $id description $defs $ref type const enum anyOf not
required properties additionalProperties items minItems maxItems minLength minimum pattern'''.split())
TYPES = {'null': type(None), 'integer': int, 'boolean': bool, 'string': str,
         'array': list, 'object': dict}
PINS = {
    'default': (77893, '23e88699873f04da03c7ca1eb0aed6030871139f2b5a88d9f45619c289d63aaa'),
    'terminal': (81844, '15c1ea282c552ce99cae5f318aa62940a9182211c7fffc0235045ff49e5974b9'),
}


# @anchor:wa.tests.agent_lane_contract._require
def _require(condition, category):
    if not condition:
        raise ValueError(category)


# @anchor:wa.tests.agent_lane_contract._compile_schema
def _compile_schema(schema: dict) -> dict:
    _require(type(schema) is dict, 'SCHEMA_OPERAND')
    defs = schema.get('$defs', {})

    # @anchor:wa.tests.agent_lane_contract._compile_schema.scan
    def scan(s, depth=0):
        _require(depth <= 64, 'CAPACITY')
        _require(type(s) is dict, 'SCHEMA_OPERAND')
        _require(not set(s) - KEYWORDS, 'SCHEMA_KEYWORD')
        for k in ('$schema', '$id', 'description', 'pattern'):
            if k in s:
                _require(type(s[k]) is str, 'SCHEMA_OPERAND')
        if '$ref' in s:
            ref = s['$ref']
            _require(set(s) == {'$ref'} and type(ref) is str and
                     ref.startswith('#/$defs/') and '/' not in ref[8:] and
                     ref[8:] in defs, 'SCHEMA_REF')
        if 'type' in s:
            _require(type(s['type']) is str and s['type'] in TYPES, 'SCHEMA_TYPE')
        if 'pattern' in s:
            try:
                re.compile(s['pattern'])
            except re.error:
                raise ValueError('SCHEMA_PATTERN') from None
        for k in ('minimum', 'minItems', 'maxItems', 'minLength'):
            if k in s:
                _require(type(s[k]) is int and (k == 'minimum' or s[k] >= 0), 'SCHEMA_OPERAND')
        if 'minItems' in s and 'maxItems' in s:
            _require(s['minItems'] <= s['maxItems'], 'SCHEMA_OPERAND')
        if 'required' in s:
            v = s['required']
            _require(type(v) is list and all(type(x) is str for x in v) and
                     len(v) == len(set(v)), 'SCHEMA_OPERAND')
        if 'enum' in s:
            v = s['enum']
            _require(type(v) is list and bool(v) and
                     len({gate.canon(x) for x in v}) == len(v), 'SCHEMA_OPERAND')
        for k in ('$defs', 'properties'):
            if k in s:
                _require(type(s[k]) is dict and all(type(x) is str for x in s[k]), 'SCHEMA_OPERAND')
                for child in s[k].values():
                    scan(child, depth + 1)
        if 'anyOf' in s:
            _require(type(s['anyOf']) is list and bool(s['anyOf']), 'SCHEMA_OPERAND')
            for child in s['anyOf']:
                scan(child, depth + 1)
        for k in ('items', 'not'):
            if k in s:
                scan(s[k], depth + 1)
        if 'additionalProperties' in s:
            v = s['additionalProperties']
            _require(type(v) in (bool, dict), 'SCHEMA_OPERAND')
            if type(v) is dict:
                scan(v, depth + 1)
    scan(schema)
    return defs


# @anchor:wa.tests.agent_lane_contract._conforms
def _conforms(value, schema: dict, defs: dict, depth=0) -> bool:
    _require(depth <= 64, 'CAPACITY')

    # @anchor:wa.tests.agent_lane_contract._conforms.test
    def test(v, s):
        return _conforms(v, s, defs, depth + 1)

    if '$ref' in schema:
        return test(value, defs[schema['$ref'][8:]])
    if 'anyOf' in schema and not any(test(value, s) for s in schema['anyOf']):
        return False
    if 'not' in schema and test(value, schema['not']):
        return False
    if 'type' in schema and type(value) is not TYPES[schema['type']]:
        return False
    if 'const' in schema and gate.canon(value) != gate.canon(schema['const']):
        return False
    if 'enum' in schema and gate.canon(value) not in [gate.canon(x) for x in schema['enum']]:
        return False
    if type(value) is int and 'minimum' in schema and value < schema['minimum']:
        return False
    if type(value) is str:
        if len(value) < schema.get('minLength', 0):
            return False
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            return False
    if type(value) is list:
        if len(value) < schema.get('minItems', 0):
            return False
        if 'maxItems' in schema and len(value) > schema['maxItems']:
            return False
        if 'items' in schema and not all(test(x, schema['items']) for x in value):
            return False
    if type(value) is dict:
        props = schema.get('properties', {})
        if not set(schema.get('required', [])) <= value.keys():
            return False
        extra = value.keys() - props.keys()
        additional = schema.get('additionalProperties', True)
        if additional is False and extra:
            return False
        if type(additional) is dict and not all(test(value[k], additional) for k in extra):
            return False
        if not all(test(value[k], props[k]) for k in value.keys() & props.keys()):
            return False
    return True


# @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract
class TestAgentLaneContract(unittest.TestCase):
    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.setUp
    def setUp(self):
        self.schema = gate.strict_load((ROOT / 'contracts/agent_lane.v1.schema.json').read_bytes())
        self.defs = _compile_schema(self.schema)
        self.raw = {name: (ROOT / 'tests/fixtures/agent_lane' /
                          f'agent_lane_bench_{name}.json').read_bytes() for name in PINS}
        self.fixtures = {name: gate.strict_load(raw) for name, raw in self.raw.items()}

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.check_mutation
    def check_mutation(self, path, value=None, delete=False):
        for name, baseline in self.fixtures.items():
            with self.subTest(fixture=name, path=path, value=value, delete=delete):
                self.assertTrue(_conforms(baseline, self.schema, self.defs))
                mutant = copy.deepcopy(baseline)
                target = mutant
                for key in path[:-1]:
                    target = target[key]
                if delete:
                    del target[path[-1]]
                else:
                    target[path[-1]] = value
                self.assertFalse(_conforms(mutant, self.schema, self.defs))

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_identity_and_pins
    def test_identity_and_pins(self):
        expected = {'$id': 'wender-agent/contracts/agent_lane.v1.schema.json',
                    '$schema': 'https://json-schema.org/draft/2020-12/schema'}
        for key, value in expected.items():
            self.assertEqual(self.schema[key], value)
            mutant = copy.deepcopy(self.schema)
            mutant[key] += '/wrong'
            self.assertNotEqual(mutant[key], value)
        self.assertEqual(self.schema['properties']['schema'], {'const': 'agent_lane/v1'})
        for name, raw in self.raw.items():
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), PINS[name])
            self.assertNotEqual(hashlib.sha256(raw + b' ').hexdigest(), PINS[name][1])
            self.assertTrue(_conforms(self.fixtures[name], self.schema, self.defs))
        self.check_mutation(['schema'], 'agent_lane/v2')

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_required_and_closed_objects
    def test_required_and_closed_objects(self):
        paths = [
            ([], 'schema migration target revision tick frame state state_sha256 frame_state_sha256 elements'),
            (['migration'], 'from chain'),
            (['target'], 'kind canonical_id generation epoch'),
            (['frame'], 'pixel_format width height byte_length raw_sha256 committed_revision tick frame_seq transport source_pin'),
            (['state'], 'schema source prune ax_sha256 ax_stable_sha256 viewport nodes actionable counts'),
            (['state', 'source'], 'kind url title'),
            (['state', 'prune'], 'name version'),
            (['state', 'counts'], 'raw nodes actionable'),
            (['state', 'nodes', 0], 'id role name depth parent ordinal states actionable box'),
            (['elements', 0], 'stable_id fingerprint anchor role actionable state_node_id generation epoch'),
        ]
        for path, required in paths:
            for key in required.split():
                self.check_mutation(path + [key], delete=True)
            self.check_mutation(path + ['bogus'], 1)
        self.check_mutation(['extra'], {})

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_boolean_and_boxes
    def test_boolean_and_boxes(self):
        for path in (['state', 'nodes', 0, 'actionable'], ['elements', 0, 'actionable']):
            for value in (1, 'true', None):
                self.check_mutation(path, value)
        for value in ([1, 2, 3], [1, 2, 3, 4, 5], [1.0, 2, 3, 4], [True, 2, 3, 4]):
            self.check_mutation(['state', 'nodes', 0, 'box'], value)
        for baseline in self.fixtures.values():
            for value in (True, False):
                mutant = copy.deepcopy(baseline)
                mutant['elements'][0]['actionable'] = value
                mutant['state']['nodes'][0]['actionable'] = value
                for box in (None, [1, 2, 3, 4]):
                    mutant['state']['nodes'][0]['box'] = box
                    self.assertTrue(_conforms(mutant, self.schema, self.defs))
            mutant['state']['nodes'][0]['box'] = [1.0, 2, 3, 4]
            with self.assertRaisesRegex(ValueError, '^JSON_NUMBER$'):
                gate.strict_load(json.dumps(mutant).encode())

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_digests_ids_and_anchor
    def test_digests_ids_and_anchor(self):
        paths = [(['frame', 'raw_sha256'], 64), (['state_sha256'], 64),
                 (['frame_state_sha256'], 64), (['state', 'ax_sha256'], 64),
                 (['state', 'ax_stable_sha256'], 64), (['state', 'nodes', 0, 'id'], 16),
                 (['elements', 0, 'fingerprint'], 16), (['elements', 0, 'state_node_id'], 16),
                 (['state', 'actionable', 0], 16)]
        for path, length in paths:
            for value in ('a' * (length - 1), 'A' * length, 'g' * length, 'a' * length + '\n'):
                self.check_mutation(path, value)
        for value in ('', 'a' * 16 + ':g0:e0', 'a' * 16 + ':g1:e01', 'a' * 16 + ':g1:e0\n'):
            self.check_mutation(['elements', 0, 'stable_id'], value)
        for value in ('', 'relative'):
            self.check_mutation(['elements', 0, 'anchor'], value)
        for baseline in self.fixtures.values():
            positive = copy.deepcopy(baseline)
            positive['frame']['raw_sha256'] = '0' * 64
            positive['elements'][0]['anchor'] = '/line\nbreak'
            self.assertTrue(_conforms(positive, self.schema, self.defs))

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_nullable_and_typed_maps
    def test_nullable_and_typed_maps(self):
        for path in (['state', 'nodes', 0, 'parent'], ['state', 'source', 'url'],
                     ['state', 'source', 'title']):
            self.check_mutation(path, 123)
        self.check_mutation(['state', 'nodes', 0, 'states'], {'new': 1})
        for value in (True, '1'):
            self.check_mutation(['state', 'viewport'], {'new': value})
        for baseline in self.fixtures.values():
            positive = copy.deepcopy(baseline)
            for nullable in (None, 'a' * 16):
                positive['state']['nodes'][0]['parent'] = nullable
                positive['state']['source']['url'] = nullable
                positive['state']['source']['title'] = nullable
                positive['state']['nodes'][0]['states'] = {'arbitrary': 'value'}
                for viewport in (None, {}, {'arbitrary': -1}):
                    positive['state']['viewport'] = viewport
                    for kind in ('cdp', 'atspi', 'godot', 'vision'):
                        positive['state']['source']['kind'] = kind
                        self.assertTrue(_conforms(positive, self.schema, self.defs))

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_scalars_and_arrays
    def test_scalars_and_arrays(self):
        nonnegative = [['revision'], ['tick'], ['target', 'epoch'], ['frame', 'committed_revision'],
                       ['frame', 'tick'], ['frame', 'frame_seq'], ['state', 'prune', 'version'],
                       ['state', 'nodes', 0, 'depth'], ['state', 'nodes', 0, 'ordinal'],
                       ['elements', 0, 'epoch']] + [['state', 'counts', k] for k in ('raw', 'nodes', 'actionable')]
        positive = [['target', 'generation'], ['elements', 0, 'generation']] + [['frame', k] for k in ('width', 'height', 'byte_length')]
        for path in nonnegative:
            for value in (-1, True):
                self.check_mutation(path, value)
        for path in positive:
            for value in (0, True):
                self.check_mutation(path, value)
        for path in (['target', 'canonical_id'], ['frame', 'source_pin']):
            self.check_mutation(path, '')
        for path in (['elements'], ['state', 'nodes'], ['state', 'actionable']):
            self.check_mutation(path, {})
        for path, value in ((['migration', 'from'], 0), (['migration', 'chain'], []),
                            (['target', 'kind'], 'other'), (['frame', 'pixel_format'], 'RGBA'),
                            (['frame', 'transport'], 'other'), (['state', 'schema'], 'other'),
                            (['state', 'source', 'kind'], 'other')):
            self.check_mutation(path, value)

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_t2_branches
    def test_t2_branches(self):
        for baseline in self.fixtures.values():
            positive = copy.deepcopy(baseline)
            positive.pop('t2', None)
            self.assertTrue(_conforms(positive, self.schema, self.defs))
            for cmd, args in (('advance', {'until_tick': 0}), ('pause', {}),
                              ('custom', {'anything': [None, True]})):
                positive['t2'] = {'tick': 0, 'seq': 1, 'cmd': cmd, 'args': args}
                self.assertTrue(_conforms(positive, self.schema, self.defs))
                mutant = copy.deepcopy(positive)
                mutant['t2']['extra'] = 0
                self.assertFalse(_conforms(mutant, self.schema, self.defs))
                for key in ('tick', 'seq', 'cmd', 'args'):
                    mutant = copy.deepcopy(positive)
                    del mutant['t2'][key]
                    self.assertFalse(_conforms(mutant, self.schema, self.defs))
        invalid = [dict(tick=0, seq=1, cmd=cmd, args=args) for cmd, args in
                   [('advance', {}), ('advance', {'until_tick': -1}),
                    ('advance', {'until_tick': True}), ('advance', {'until_tick': 0, 'extra': 0}),
                    ('pause', {'extra': 0}), ('pause', []), ('custom', []), ('', {})]]
        invalid.extend([None, [], {'tick': 0, 'seq': 1, 'cmd': 'advance', 'args': {'until_tick': '0'}}])
        for value in invalid:
            self.check_mutation(['t2'], value)
        for key, value in (('tick', -1), ('tick', True), ('seq', 0), ('seq', True), ('extra', 0)):
            t2 = {'tick': 0, 'seq': 1, 'cmd': 'custom', 'args': {}, key: value}
            self.check_mutation(['t2'], t2)

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_compiler_rejections
    def test_compiler_rejections(self):
        self.assertEqual(_compile_schema(self.schema), self.defs)
        bad = [None, [], True, {'title': 'unsupported'}, {'allOf': [{}]}, {'$ref': '#/$defs/missing'},
               {'$ref': '#/$defs/hex16', 'type': 'string'}, {'$ref': 1},
               {'$ref': 'https://example.invalid/schema'}, {'additionalProperties': 1},
               {'additionalProperties': {'unknown': 1}}, {'pattern': '['}, {'pattern': 1},
               {'type': 'number'}, {'type': []}, {'required': 'x'}, {'required': [1]},
               {'required': ['x', 'x']}, {'enum': []}, {'enum': [1, 1]}, {'anyOf': []},
               {'anyOf': {}}, {'not': True}, {'items': []}, {'properties': []},
               {'properties': {1: {}}}, {'$defs': []}, {'$defs': {'unused': {'unknown': 1}}},
               {'minimum': True}, {'minLength': -1}, {'minItems': -1},
               {'maxItems': -1}, {'minItems': 2, 'maxItems': 1}, {'description': 1},
               {'$id': None}, {'$schema': []}]
        for schema in bad:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                _compile_schema(schema)
        # The full supported vocabulary accepts valid operands, including unused defs.
        for schema in ({}, {'additionalProperties': True}, {'additionalProperties': False},
                       {'additionalProperties': {'type': 'string'}}, {'enum': [True, 1]},
                       {'description': 'passive'}, {'$defs': {'unused': {'type': 'null'}}}):
            _compile_schema(schema)

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_strict_ingestion
    def test_strict_ingestion(self):
        for raw in self.raw.values():
            self.assertIsInstance(gate.strict_load(raw), dict)
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":-Infinity}', b'{"x":1.0}', b'"\xff"'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                gate.strict_load(raw)

    # @anchor:wa.tests.agent_lane_contract.TestAgentLaneContract.test_checker_assertions_and_depth
    def test_checker_assertions_and_depth(self):
        controls = [({'type': 'integer'}, 1, True), ({'type': 'boolean'}, False, 0),
                    ({'const': 1}, 1, True), ({'enum': [False]}, False, 0),
                    ({'anyOf': [{'type': 'integer'}], 'minimum': 1}, 1, 0),
                    ({'anyOf': [{'type': 'integer'}]}, 1, '1'),
                    ({'not': {'enum': ['pause']}, 'minLength': 1}, 'go', 'pause'),
                    ({'not': {'enum': ['pause']}, 'minLength': 1}, 'go', ''),
                    ({'type': 'string', 'pattern': '^[0-9a-f]{16}$'}, 'a'*16, 'a'*16+'\n'),
                    ({'type': 'object', 'additionalProperties': {'type': 'string'}}, {'x': 's'}, {'x': 1}),
                    ({'type': 'array', 'items': {'type': 'integer'}}, [1], [True])]
        for schema, positive, negative in controls:
            _compile_schema(schema)
            self.assertTrue(_conforms(positive, schema, {}))
            self.assertFalse(_conforms(negative, schema, {}))
        recursive = {'loop': {'$ref': '#/$defs/loop'}}
        _compile_schema({'$defs': recursive})
        with self.assertRaisesRegex(ValueError, '^CAPACITY$'):
            _conforms(None, recursive['loop'], recursive)
        self.assertTrue(_conforms(None, {}, {}, 64))
        with self.assertRaisesRegex(ValueError, '^CAPACITY$'):
            _conforms(None, {}, {}, 65)
        schema = {}
        for _ in range(64):
            schema = {'items': schema}
        _compile_schema(schema)
        with self.assertRaisesRegex(ValueError, '^CAPACITY$'):
            _compile_schema({'items': schema})


if __name__ == '__main__':
    unittest.main()
