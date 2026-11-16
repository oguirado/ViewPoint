"""Closed factory record schemas; custom semantic validators live in glb_io/capture.

Kept as Python data to remain inside the F2-U4 glb*.py ownership surface.
No external schema library is required; these documents are consumer contracts.
"""
DRAFT = 'https://json-schema.org/draft/2020-12/schema'
SHA = {'type': 'string'}  # Full digest syntax is checked by the semantic validator.
STRING = {'type': 'string'}
INTEGER = {'type': 'integer'}


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


def array(item):
    return {'type': 'array', 'items': item}


def schema(name, properties):
    return {'$schema': DRAFT, '$id': 'urn:af:' + name + ':v1',
            **obj({'schema': {'const': 'af.' + name + '/v1'}, **properties})}


FILE_REF = obj({'path': STRING, 'sha256': SHA})
GATE_REF = obj({'gate': STRING, 'schema': STRING, 'path': STRING, 'sha256': SHA,
                'subject_sha256': SHA, 'verdict': {'const': 'PASS'}})
LOD = obj({'level': INTEGER, 'path': STRING, 'sha256': SHA})
NODE = obj({'index': INTEGER, 'name': STRING,
            'role': {'enum': ['visual', 'collision', 'occluder', None]},
            'world_matrix_hex': {**array(STRING), 'minItems': 16},
            'suffix_hint': {'type': 'boolean'}})

SEPARATION_SCHEMA = schema('glb-separation', {
    'rc': {'const': 0}, 'status': {'const': 'OK'}, 'reason_codes': {'const': []},
    'source_sha256': SHA, 'roles_sha256': SHA, 'nodes': {**array(NODE), 'minItems': 1},
    'normalization': {'const': {'applied': False, 'basis': 'gltf-native'}},
    'lods': array(LOD), 'gate_refs': array(GATE_REF),
    'required_gates': {**array(STRING), 'minItems': 1}, 'required_lod_levels': array(INTEGER),
    'artifacts': {**array(FILE_REF), 'minItems': 1},
    'appearance': {'const': {'rc': 3, 'verdict': 'CANNOT_SEE', 'reason_codes': ['captures_not_evaluated']}},
    'retained_unreferenced_buffer': {'const': True},
})
RECEIPT_SCHEMA = schema('glb-receipt', {
    'source_sha256': SHA, 'roles_sha256': SHA, 'report': FILE_REF,
    'artifacts': {**array(FILE_REF), 'minItems': 1},
    'tool_files': {**array(FILE_REF), 'minItems': 1},
})
CAPTURE_FRAMES = obj({
    'schema': {'const': 'af.glb-capture-frames/v1'},
    'source_sha256': SHA, 'candidate_sha256': SHA,
    'helpers_declared': {'type': 'boolean'},
    'profile': obj({'engine': STRING, 'device': STRING, 'resolution': INTEGER,
                    'samples': INTEGER, 'seed': INTEGER, 'views': INTEGER,
                    'comparison': STRING, 'color': STRING}),
    'views': {**array(obj({'view': INTEGER, 'raw_visual_rgb_sha256': SHA,
                          'candidate_rgb_sha256': SHA, 'diagnostic_rgb_sha256': SHA,
                          'background_rgb_sha256': SHA, 'pixel_count': INTEGER})), 'minItems': 1},
})
NULL_SHA = {'oneOf': [SHA, {'type': 'null'}]}
CAPTURE_SCHEMA = schema('glb-capture', {
    'rc': {'enum': [0, 2, 3, 4, 5, 6]}, 'status': STRING,
    'verdict': {'enum': ['PASS', 'ERROR', 'CANNOT_SEE', 'FAIL']},
    'reason_codes': array(STRING), 'source_sha256': NULL_SHA, 'candidate_sha256': NULL_SHA,
    'diagnostics': {'oneOf': [STRING, {'type': 'null'}]},
    'subject_sha256': NULL_SHA, 'receipt_sha256': NULL_SHA, 'tool_sha256': NULL_SHA,
    'renderer': {'oneOf': [obj({'path': STRING, 'sha256': SHA, 'version': STRING}), {'type': 'null'}]},
    'captures': {'oneOf': [CAPTURE_FRAMES, {'type': 'null'}]},
})
