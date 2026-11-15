"""Source-neutral, in-RAM, content-addressed corpus manifest (J10).

Inputs carry independently authored generation intent and declared pixel hashes;
this module never infers ground truth from GM/FL predictions. There is no I/O.
"""
from dataclasses import dataclass
import hashlib
import json
import re

import numpy as np
from eval.gui_synth.rng import SplitMix64

SCHEMA = 'holdings-manifest-v1'
_KINDS = frozenset(('cdp_tab', 'x11_window', 'xvfb_screen', 'capsule', 'godot_window'))
_LABELS = frozenset(('static', 'localized_change', 'integer_translation',
                     'periodic_flicker', 'real_motion_veto', 'sparse_support',
                     'mixed_history', 'skipped_frame', 'resize_epoch',
                     'caller_mutation', 'plugin_exception', 'eviction'))
_IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z')
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+\-]{0,95}\Z')
_MAX_PIXELS = 8_000_000


class ManifestError(ValueError):
    """A provenance, schema, layout, identity or completeness invariant failed."""


def _record(value, fields, context):
    if type(value) is not dict or set(value) != set(fields):
        raise ManifestError(context + ': expected exactly ' + ', '.join(sorted(fields)))
    if not all(type(k) is str for k in value):
        raise ManifestError(context + ': non-string record key')
    return value


def _seq(value, context):
    if type(value) not in (list, tuple):
        raise ManifestError(context + ': expected list/tuple')
    return value


def _int(value, context, *, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ManifestError(context + ': expected bounded integer (bool forbidden)')
    return value


def _id(value, context):
    if type(value) is not str or not _IDENT.fullmatch(value):
        raise ManifestError(context + ': invalid bounded identifier')
    return value


def _digest(value, context):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ManifestError(context + ': expected lowercase SHA-256')
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False)


def _no_float(value):
    if type(value) is float:
        raise ManifestError('floating point metadata forbidden')
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ManifestError('non-string metadata key')
            _no_float(item)
    elif type(value) in (list, tuple):
        for item in value:
            _no_float(item)
    elif value is not None and type(value) not in (str, int, bool):
        raise ManifestError('unsupported metadata type')


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def parse_metadata(text):
    """Parse metadata without JSON duplicate-key, floats or exotic-value loopholes."""
    if type(text) is not str:
        raise ManifestError('JSON must be text')
    try:
        data = json.loads(text, object_pairs_hook=_unique_pairs,
                          parse_float=lambda _: (_ for _ in ()).throw(ManifestError('float forbidden')),
                          parse_constant=lambda _: (_ for _ in ()).throw(ManifestError('constant forbidden')))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ManifestError('invalid JSON') from exc
    _no_float(data)
    return data


def pixel_declaration_sha256(width, height):
    """Hash a closed, architecture-neutral HxWx3 uint8 row-major layout."""
    _int(width, 'width', minimum=1)
    _int(height, 'height', minimum=1)
    if width * height > _MAX_PIXELS:
        raise ManifestError('frame exceeds in-RAM pixel bound')
    return hashlib.sha256(_json({'channels': 3, 'dtype': 'uint8', 'height': height,
                                 'order': 'C', 'width': width}).encode('ascii')).hexdigest()


def _handle(value):
    _record(value, ('kind', 'canonical_id', 'generation'), 'handle')
    if type(value['kind']) is not str or value['kind'] not in _KINDS:
        raise ManifestError('unknown handle kind')
    return {'kind': value['kind'], 'canonical_id': _id(value['canonical_id'], 'canonical_id'),
            'generation': _int(value['generation'], 'generation')}


def _environment(value):
    _record(value, ('python', 'numpy', 'platform', 'dependency_digest'), 'environment')
    out = {}
    for name in ('python', 'numpy', 'platform'):
        v = value[name]
        if type(v) is not str or not _VERSION.fullmatch(v):
            raise ManifestError('missing/invalid environment dependency ' + name)
        out[name] = v
    out['dependency_digest'] = _digest(value['dependency_digest'], 'dependency_digest')
    return out


def derive_case_seed(master_seed, *, handle, case_id, seed_tag,
                     generator_sha256, environment):
    """Lane/case-local SplitMix64 seed; order and unrelated lanes do not matter."""
    _int(master_seed, 'master_seed', maximum=(1 << 64) - 1)
    _int(seed_tag, 'seed_tag', maximum=(1 << 64) - 1)
    h = _handle(handle)
    ident = {'handle': h, 'case_id': _id(case_id, 'case_id'),
             'seed_tag': seed_tag, 'generator_sha256': _digest(generator_sha256, 'generator_sha256'),
             'environment': _environment(environment)}
    mixed = int.from_bytes(hashlib.sha256(_json(ident).encode('ascii')).digest()[:8], 'big')
    return SplitMix64(master_seed ^ mixed).u64()


def _frame(value, case_id, width, height):
    keys = {'frame_seq', 'rgb', 'pixel_decl_sha256', 'raw_sha256'}
    optional = {'normalized_rgb', 'normalized_sha256'}
    if type(value) is not dict or not keys <= set(value) or set(value) - keys - optional:
        raise ManifestError('frame: invalid closed record')
    seq = _int(value['frame_seq'], 'frame_seq')
    declared_layout = _digest(value['pixel_decl_sha256'], 'pixel_decl_sha256')
    if declared_layout != pixel_declaration_sha256(width, height):
        raise ManifestError('pixel declaration does not match geometry')
    raw = _digest(value['raw_sha256'], 'raw_sha256')
    rgb = value['rgb']
    if (type(rgb) is not np.ndarray or rgb.dtype != np.uint8 or
            rgb.shape != (height, width, 3)):
        raise ManifestError('rgb must be a HxWx3 uint8 ndarray')
    # Own data before calculating the digest: caller views and plugin mutation
    # cannot become part of the retained manifest (no frame is retained at all).
    owned = np.array(rgb, copy=True, order='C')
    owned.flags.writeable = False
    if hashlib.sha256(owned.tobytes(order='C')).hexdigest() != raw:
        raise ManifestError('raw frame digest mismatch')
    out = {'case_id': case_id, 'frame_seq': seq,
           'pixel_decl_sha256': declared_layout, 'raw_sha256': raw}
    if ('normalized_rgb' in value) != ('normalized_sha256' in value):
        raise ManifestError('normalized frame and hash must be supplied together')
    if 'normalized_rgb' in value:
        norm = value['normalized_rgb']
        if (type(norm) is not np.ndarray or norm.dtype != np.uint8 or
                norm.shape != (height, width, 3)):
            raise ManifestError('normalized frame has invalid layout')
        claimed = _digest(value['normalized_sha256'], 'normalized_sha256')
        safe = np.array(norm, copy=True, order='C')
        if hashlib.sha256(safe.tobytes(order='C')).hexdigest() != claimed:
            raise ManifestError('normalized frame digest mismatch')
        out['normalized_sha256'] = claimed
    return out


@dataclass(frozen=True)
class Manifest:
    """Immutable canonical JSON and its seal; metadata() returns a detached copy."""
    canonical_json: str
    sha256: str

    def metadata(self):
        return parse_metadata(self.canonical_json)


def build_manifest(cases, *, environment, lanes, generator_source,
                   generator_sha256, algorithm_id, master_seed):
    """Validate a closed corpus and seal canonical metadata without storing pixels.

    Each case has case_id, seed_tag, geometry, faults, expected (intent),
    expected_frame_count, expected_frame_seqs, frames. Lanes bind a typed handle,
    epoch_plan (case_id, epoch, frame_seqs), and exact case_ids. Sequences can
    contain explicit gaps. Results never include ndarray values or callbacks.
    """
    env = _environment(environment)
    digest = _digest(generator_sha256, 'generator_sha256')
    if type(generator_source) is not bytes or not generator_source:
        raise ManifestError('generator_source must be nonempty exact bytes')
    if hashlib.sha256(generator_source).hexdigest() != digest:
        raise ManifestError('generator source digest does not match supplied bytes')
    algorithm = _id(algorithm_id, 'algorithm_id')
    _int(master_seed, 'master_seed', maximum=(1 << 64) - 1)
    case_data, frame_data = [], []
    ids = set()
    for item in _seq(cases, 'cases'):
        _record(item, ('case_id', 'seed_tag', 'geometry', 'faults', 'expected',
                       'expected_frame_count', 'expected_frame_seqs', 'frames'), 'case')
        cid = _id(item['case_id'], 'case_id')
        if cid in ids:
            raise ManifestError('duplicate case identity')
        ids.add(cid)
        seed_tag = _int(item['seed_tag'], 'seed_tag', maximum=(1 << 64) - 1)
        geometry = _record(item['geometry'], ('width', 'height'), 'geometry')
        width = _int(geometry['width'], 'width', minimum=1)
        height = _int(geometry['height'], 'height', minimum=1)
        pixel_declaration_sha256(width, height)
        faults = _seq(item['faults'], 'faults')
        if len(set(map(str, faults))) != len(faults) or any(type(x) is not str or x not in _LABELS for x in faults):
            raise ManifestError('duplicate or unsupported fault label')
        exp = _record(item['expected'], ('label', 'dx', 'dy', 'gm_ops', 'fl_ops'), 'expected')
        if type(exp['label']) is not str or exp['label'] not in _LABELS:
            raise ManifestError('expected label must be authored generation intent')
        if exp['label'] not in faults:
            raise ManifestError('expected label must belong to declared fault intent')
        out_exp = {'label': exp['label']}
        for name in ('dx', 'dy'):
            v = exp[name]
            out_exp[name] = None if v is None else _int(v, name, minimum=-8192, maximum=8192)
        for name in ('gm_ops', 'fl_ops'):
            v = exp[name]
            out_exp[name] = None if v is None else _int(v, name)
        seqs = [_int(x, 'expected_frame_seq') for x in _seq(item['expected_frame_seqs'], 'expected_frame_seqs')]
        declared_count = _int(item['expected_frame_count'], 'expected_frame_count', minimum=1)
        if len(seqs) != declared_count or len(set(seqs)) != len(seqs):
            raise ManifestError('incomplete or duplicate declared frame sequence')
        frames = [_frame(x, cid, width, height) for x in _seq(item['frames'], 'frames')]
        observed_seqs = [f['frame_seq'] for f in frames]
        if len(frames) != declared_count or sorted(observed_seqs) != sorted(seqs):
            raise ManifestError('observed frames do not match declared sequence/count')
        frame_data.extend(sorted(frames, key=lambda f: f['frame_seq']))
        case_data.append({'case_id': cid, 'seed_tag': seed_tag,
                          'geometry': {'width': width, 'height': height},
                          'faults': sorted(faults), 'expected': out_exp,
                          'expected_frame_count': declared_count,
                          'expected_frame_seqs': sorted(seqs)})
    if not case_data:
        raise ManifestError('empty generated corpus')
    lane_data, claimed = [], set()
    handles = set()
    for lane in _seq(lanes, 'lanes'):
        _record(lane, ('handle', 'epoch_plan', 'case_ids'), 'lane')
        h = _handle(lane['handle'])
        identity = (h['kind'], h['canonical_id'], h['generation'])
        if identity in handles:
            raise ManifestError('duplicate lane identity')
        handles.add(identity)
        cids = [_id(x, 'lane case_id') for x in _seq(lane['case_ids'], 'case_ids')]
        if not cids or len(cids) != len(set(cids)) or any(cid in claimed for cid in cids):
            raise ManifestError('case belongs to multiple lanes / duplicate lane case')
        if not set(cids) <= ids:
            raise ManifestError('lane references an unknown case')
        claimed.update(cids)
        plan = []
        for entry in _seq(lane['epoch_plan'], 'epoch_plan'):
            _record(entry, ('case_id', 'epoch', 'frame_seqs'), 'epoch_plan entry')
            cid = _id(entry['case_id'], 'plan case_id')
            seqs = [_int(s, 'plan frame_seq') for s in _seq(entry['frame_seqs'], 'plan frame_seqs')]
            if len(seqs) != len(set(seqs)):
                raise ManifestError('repeated frame in epoch plan')
            plan.append({'case_id': cid, 'epoch': _int(entry['epoch'], 'epoch'),
                         'frame_seqs': sorted(seqs)})
        if sorted(p['case_id'] for p in plan) != sorted(cids):
            raise ManifestError('epoch plan does not bind each case exactly once')
        by_id = {c['case_id']: c for c in case_data}
        if any(p['frame_seqs'] != by_id[p['case_id']]['expected_frame_seqs'] for p in plan):
            raise ManifestError('epoch plan sequence does not match case declaration')
        plan.sort(key=lambda p: (p['epoch'], p['case_id']))
        # Epoch monotonicity and stream resets are a caller's replay concern;
        # do not infer a passage of time from shuffled case descriptions.
        lane_data.append({'handle': h, 'epoch_plan': plan, 'case_ids': sorted(cids)})
    if claimed != ids:
        raise ManifestError('missing lane binding for a generated case')
    doc = {'schema': SCHEMA, 'generator_sha256': digest,
           'algorithm_id': algorithm, 'master_seed': master_seed,
           'environment': env,
           'lanes': sorted(lane_data, key=lambda l: (l['handle']['kind'],
                                                   l['handle']['canonical_id'],
                                                   l['handle']['generation'])),
           'cases': sorted(case_data, key=lambda c: c['case_id']),
           'frames': sorted(frame_data, key=lambda f: (f['case_id'], f['frame_seq']))}
    _no_float(doc)
    canonical = _json(doc)
    return Manifest(canonical, hashlib.sha256(canonical.encode('ascii')).hexdigest())


def verify_manifest(text, seal):
    """Verify exact canonical encoding, duplicated keys and content-addressed seal."""
    _digest(seal, 'manifest seal')
    doc = parse_metadata(text)
    if type(doc) is not dict or set(doc) != {'schema', 'generator_sha256', 'algorithm_id',
                                          'master_seed', 'environment', 'lanes', 'cases', 'frames'}:
        raise ManifestError('invalid manifest fields')
    if doc['schema'] != SCHEMA or _json(doc) != text:
        raise ManifestError('noncanonical manifest')
    _digest(doc['generator_sha256'], 'generator_sha256')
    _environment(doc['environment'])
    _int(doc['master_seed'], 'master_seed', maximum=(1 << 64)-1)
    if hashlib.sha256(text.encode('ascii')).hexdigest() != seal:
        raise ManifestError('manifest seal mismatch')
    return True
