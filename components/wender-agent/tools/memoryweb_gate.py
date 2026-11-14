# @module {"id":"wa.tools.memoryweb_gate","layer":0,"owner":"ARCH","deps":[],"book":"HB-WA-a0-arch"}
"""Offline MemoryWeb gate. Corpus bytes never supply executable owners.

Contract origin: docs/spec/MEMORYWEB_PROTOCOL.md
Trusted callbacks attest their named evidence only; they do not authorize effects.
Trusted evidence_load supplies a normalized, pinned evidence view. evidence_check
attests its binding to producer receipts, frozen inventory and authorization;
local gates independently recompute counts, reward, regression and margins.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from dataclasses import dataclass, replace
from datetime import date
from typing import Callable

DOCS = Path(__file__).resolve().parents[1] / 'docs' / 'spec'
PROTOCOL_PIN = '8001646c443a7c54a8251730a60b340359ed49c007ed478d7810f3610516b478'
SCHEMA_PIN = 'b85fd5742f768e1a9cb5478ebe628fac7f7a9f7cf34e111c8fd1ab3dc6a95025'
ROOTS = frozenset('site node pattern workflow step demo demo_request demo_steps run publication_receipt caps tags mounts overrides capstone detail'.split())
LIMITS = {'demo': 32768, 'demo_steps': 65536, 'workflow': 65536, 'site': 8192,
          'publication_receipt': 65536}
MAX_FILE = 65536
MAX_FILES = 4096
MAX_BYTES = 16777216
KEYWORDS = frozenset('$schema $id description $defs $ref type const enum anyOf oneOf required properties additionalProperties items minItems maxItems uniqueItems minLength maxLength minimum maximum pattern'.split())
TYPES = {'null': type(None), 'boolean': bool, 'integer': int, 'string': str,
         'array': list, 'object': dict}
HEADINGS = ('Intent', 'Starting state', 'Variable slots', 'Ambiguous controls',
            'Expected result', 'Exceptions')


# @anchor:wa.tools.memoryweb_gate.GateResult
@dataclass(frozen=True)
class GateResult:
    rc: int
    category: str


# @anchor:wa.tools.memoryweb_gate.NativeDisposition
@dataclass(frozen=True)
class NativeDisposition:
    disposition: str
    rc: int | None
    component: str
    operation: str
    native_rc: int
    native_reason: str


# @anchor:wa.tools.memoryweb_gate.ValidationContext
@dataclass(frozen=True)
class ValidationContext:
    """Trusted offline owners, never populated from corpus configuration.

    evidence_load(kind, artifact, related) returns a normalized dict. The separate
    evidence_check receives (kind, artifact, (view, *related)) and must verify its
    exact receipt pins, frozen definitions, authorization and independent review.
    No producer codec is assumed. journal_read_budget is an explicit byte budget,
    not a retention rule. Missing evidence or budget cannot pass a dependent gate.
    """
    root: Path
    as_of: date | None = None
    capability_check: Callable | None = None
    evidence_check: Callable | None = None
    review_check: Callable | None = None
    chain_check: Callable | None = None
    evidence_load: Callable | None = None
    journal_read_budget: int | None = None


class _Refusal(ValueError):
    def __init__(self, category, rc=5):
        self.category = category
        self.rc = rc
        super().__init__(category)


def _require(test, category, rc=5):
    if not test:
        raise _Refusal(category, rc)


def _boundary(function):
    def guarded(*args, **kwargs):
        try:
            function(*args, **kwargs)
            return GateResult(0, 'PASS')
        except _Refusal as exc:
            return GateResult(exc.rc, exc.category)
        except (RecursionError, MemoryError):
            return GateResult(5, 'CAPACITY')
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeError, re.error):
            return GateResult(2, 'SCHEMA')
        except OSError as exc:
            return GateResult(5, 'CONTAINMENT' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else 'IO')
    return guarded


# @anchor:wa.tools.memoryweb_gate.strict_load
def strict_load(raw: bytes) -> object:
    def reject(_):
        raise _Refusal('JSON_NUMBER', 2)

    def pairs(items):
        out = {}
        for key, value in items:
            _require(key not in out, 'JSON_DUPLICATE_KEY', 2)
            out[key] = value
        return out

    value = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=pairs,
                       parse_float=reject, parse_constant=reject)

    def walk(obj, depth=0):
        _require(depth <= 64, 'CAPACITY')
        if type(obj) is str:
            _require(not any(0xD800 <= ord(c) <= 0xDFFF for c in obj), 'JSON_UNICODE', 2)
        elif type(obj) is dict:
            for k, v in obj.items():
                walk(k, depth + 1)
                walk(v, depth + 1)
        elif type(obj) is list:
            for v in obj:
                walk(v, depth + 1)
    walk(value)
    return value


# @anchor:wa.tools.memoryweb_gate.canon
def canon(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


# @anchor:wa.tools.memoryweb_gate.compile_bundle
def compile_bundle(bundle: dict) -> dict:
    _require(type(bundle) is dict and type(bundle.get('$defs')) is dict,
             'SCHEMA_BUNDLE', 2)
    defs = bundle['$defs']

    def scan(s, depth=0):
        _require(depth <= 64, 'CAPACITY')
        _require(type(s) is dict and not set(s) - KEYWORDS, 'SCHEMA_KEYWORD', 2)
        for k in ('$schema', '$id', 'description', 'pattern'):
            if k in s:
                _require(type(s[k]) is str, 'SCHEMA_OPERAND', 2)
        if '$ref' in s:
            ref = s['$ref']
            _require(set(s) == {'$ref'} and type(ref) is str and
                     ref.startswith('#/$defs/') and ref[8:] in defs, 'SCHEMA_REF', 2)
        if 'type' in s:
            _require(type(s['type']) is str and s['type'] in TYPES, 'SCHEMA_TYPE', 2)
        if 'pattern' in s:
            re.compile(s['pattern'])
        for k in ('minimum', 'maximum', 'minItems', 'maxItems', 'minLength', 'maxLength'):
            if k in s:
                _require(type(s[k]) is int and (k in ('minimum', 'maximum') or s[k] >= 0),
                         'SCHEMA_OPERAND', 2)
        for lo, hi in (('minimum', 'maximum'), ('minItems', 'maxItems'), ('minLength', 'maxLength')):
            if lo in s and hi in s:
                _require(s[lo] <= s[hi], 'SCHEMA_OPERAND', 2)
        for k in ('additionalProperties', 'uniqueItems'):
            if k in s:
                _require(type(s[k]) is bool, 'SCHEMA_OPERAND', 2)
        if 'required' in s:
            v = s['required']
            _require(type(v) is list and all(type(x) is str for x in v) and
                     len(v) == len(set(v)), 'SCHEMA_OPERAND', 2)
        if 'enum' in s:
            v = s['enum']
            _require(type(v) is list and bool(v) and len({canon(x) for x in v}) == len(v),
                     'SCHEMA_OPERAND', 2)
        for k in ('$defs', 'properties'):
            if k in s:
                _require(type(s[k]) is dict and all(type(x) is str for x in s[k]),
                         'SCHEMA_OPERAND', 2)
                for child in s[k].values():
                    scan(child, depth + 1)
        for k in ('anyOf', 'oneOf'):
            if k in s:
                _require(type(s[k]) is list and bool(s[k]), 'SCHEMA_OPERAND', 2)
                for child in s[k]:
                    scan(child, depth + 1)
        if 'items' in s:
            scan(s['items'], depth + 1)
    scan(bundle)
    return defs


# @anchor:wa.tools.memoryweb_gate.conforms
def conforms(value, schema, defs, depth=0) -> bool:
    _require(depth <= 64, 'CAPACITY')
    if '$defs' in schema and not any(k in schema for k in ('type', '$ref', 'const', 'enum', 'anyOf', 'oneOf')):
        return False
    def test(v, s):
        return conforms(v, s, defs, depth + 1)
    if '$ref' in schema:
        return test(value, defs[schema['$ref'][8:]])
    for key in ('anyOf', 'oneOf'):
        if key in schema:
            results = [test(value, s) for s in schema[key]]
            if (not any(results)) if key == 'anyOf' else (sum(results) != 1):
                return False
    if 'type' in schema and type(value) is not TYPES[schema['type']]:
        return False
    if 'const' in schema and canon(value) != canon(schema['const']):
        return False
    if 'enum' in schema and canon(value) not in [canon(x) for x in schema['enum']]:
        return False
    if type(value) is int:
        if 'minimum' in schema and value < schema['minimum']:
            return False
        if 'maximum' in schema and value > schema['maximum']:
            return False
    if type(value) is str:
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', sys.maxsize):
            return False
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            return False
    if type(value) is list:
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', sys.maxsize):
            return False
        if schema.get('uniqueItems') and len({canon(x) for x in value}) != len(value):
            return False
        if 'items' in schema and not all(test(x, schema['items']) for x in value):
            return False
    if type(value) is dict:
        props = schema.get('properties', {})
        if not set(schema.get('required', [])) <= value.keys():
            return False
        if schema.get('additionalProperties') is False and value.keys() - props.keys():
            return False
        if not all(test(value[k], props[k]) for k in value.keys() & props.keys()):
            return False
    return True


def _lines(raw):
    raw.decode('utf-8', errors='strict')
    _require(b'\r' not in raw, 'LINE_ENDINGS', 3)
    fence = None
    offset = 0
    for line in raw.splitlines(keepends=True):
        text = line.decode('utf-8').rstrip('\n')
        m = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', text)
        outside = fence is None
        if m:
            marker, rest = m.groups()
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                fence = None
            outside = False
        yield offset, text, outside
        offset += len(line)


def _anchors(raw):
    anchors, headings, pending = {}, [], None
    for offset, text, outside in _lines(raw):
        if not outside:
            _require(pending is None, 'ANCHOR_UNATTACHED', 3)
            continue
        m = re.fullmatch(r'<a id="([a-z0-9_-]+)"></a>', text)
        if m:
            name = m[1]
            _require(pending is None and name not in anchors, 'ANCHOR_DUPLICATE', 3)
            pending = (name, offset)
            continue
        h = re.match(r'^ {0,3}(#{1,6})(?:[ \t]+|$)', text)
        if h:
            headings.append((offset, len(h[1]), pending))
            if pending:
                anchors[pending[0]] = (pending[1], len(h[1]), len(headings) - 1)
            pending = None
        elif text.strip():
            _require(pending is None, 'ANCHOR_UNATTACHED', 3)
    _require(pending is None, 'ANCHOR_UNATTACHED', 3)
    return anchors, headings


# @anchor:wa.tools.memoryweb_gate.extract_span
def extract_span(markdown: bytes, anchor_id: str) -> bytes:
    anchors, headings = _anchors(markdown)
    _require(anchor_id in anchors, 'ANCHOR_MISSING', 3)
    start, level, index = anchors[anchor_id]
    end = len(markdown)
    for offset, next_level, attached in headings[index + 1:]:
        if next_level <= level:
            end = attached[1] if attached else offset
            break
    return markdown[start:end]


def _read(path, cap, *, owner=None):
    # Traverse every component with descriptor-relative O_NOFOLLOW, including the owner.
    path = Path(path).absolute()
    cap = min(cap, {'MEMORY.md': 8192, 'EXPLANATION.md': 16384}.get(path.name, cap))
    if owner is not None:
        owner = Path(owner).absolute()
        _require(path.is_relative_to(owner), 'CONTAINMENT')
    _require('..' not in path.parts, 'CONTAINMENT')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            _require(stat.S_ISREG(os.fstat(fd).st_mode), 'FILE_TYPE')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(cap + 1)
            _require(len(raw) <= cap, 'CAPACITY')
            _require(b'\x00' not in raw and not raw.startswith((b'PK\x03\x04', b'SQLite format 3', b'%PDF-')), 'TEXT')
            raw.decode('utf-8', errors='strict')
            return raw
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _Refusal('CONTAINMENT') from None
        raise
    finally:
        os.close(descriptor)


def _reference(owner, ref):
    _require(type(ref) is str and ref and not Path(ref).is_absolute() and
             not any(p in ('..', '.') for p in ref.split('/')) and
             not re.search(r'[:\\?#]', ref), 'CONTAINMENT')
    return Path(owner) / ref


# @anchor:wa.tools.memoryweb_gate.check_explanation
@_boundary
def check_explanation(path: Path) -> GateResult:
    raw = _read(path, 16384)
    sections, current = [], None
    for _, text, outside in _lines(raw):
        if not outside:
            continue
        if re.match(r'^##(?:\s|$)', text):
            _require(text in ['## ' + h for h in HEADINGS], 'EXPLANATION', 2)
            current = []
            sections.append((text[3:], current))
        elif current is not None and text.strip() and not text.strip().startswith(('Describe ', 'Name caller-supplied ', 'Explain confusing ')) and not re.fullmatch(r'\s*(?:<!--.*-->|[<\[{].*[>\]}]|TODO|TBD|\.\.\.)\s*', text):
            current.append(text.strip())
    _require(tuple(k for k, _ in sections) == HEADINGS and all(v for _, v in sections),
             'EXPLANATION', 2)


# @anchor:wa.tools.memoryweb_gate.entry_check
@_boundary
def entry_check(entry_path: Path, protocol_path: Path) -> GateResult:
    entry = _read(entry_path, 600)
    protocol = _read(protocol_path, 1048576)
    _require(hashlib.sha256(protocol).hexdigest() == PROTOCOL_PIN, 'PROTOCOL_PIN', 3)
    _require(PROTOCOL_PIN.encode() in entry, 'ENTRY_PIN', 3)
    anchors, headings = _anchors(protocol)
    _require(all(attached for _, level, attached in headings if level >= 2), 'ANCHOR_MISSING', 3)
    refs = re.findall(r'\]\(([^)]+)#([a-z0-9_-]+)\)', entry.decode())
    _require(bool(refs), 'ENTRY_TARGET', 3)
    for file, fragment in refs:
        target = _reference(Path(entry_path).absolute().parent, file)
        _require(target.absolute() == Path(protocol_path).absolute() and fragment in anchors,
                 'ENTRY_TARGET', 3)
        extract_span(protocol, fragment)


def _owner_check(callback, args, missing):
    _require(callable(callback), missing)
    try:
        result = callback(*args)
    except Exception:
        raise _Refusal('EVIDENCE_OWNER') from None
    _require(type(result) is GateResult and type(result.rc) is int and result.rc == 0 and result.category == 'PASS',
             'EVIDENCE_DENIED')


def _objects(value):
    if type(value) is dict:
        yield value
        for child in value.values():
            yield from _objects(child)
    elif type(value) is list:
        for child in value:
            yield from _objects(child)


def _box(value, maximum=None):
    _require(value[0] <= value[2] and value[1] <= value[3], 'GEOMETRY')
    if maximum:
        _require(0 <= value[0] <= value[2] <= maximum[0] and
                 0 <= value[1] <= value[3] <= maximum[1], 'GEOMETRY')


def _semantic_tree(value, ctx, related):
    for obj in _objects(value):
        for key, val in obj.items():
            if key in ('last_verified', 'recorded', 'recorded_date', 'expires') and val is not None:
                try:
                    date.fromisoformat(val)
                except (ValueError, TypeError):
                    raise _Refusal('DATE', 2) from None
            if key in ('box_px', 'crop_box_px', 'change_box_px', 'expected_change_box_px') and val is not None:
                _box(val)
            if key == 'rel_box_bps':
                _box(val, (10000, 10000))
            if key == 'mask_boxes_px':
                for box in val:
                    _box(box)
            if key == 'domains':
                for hostname in val:
                    _require(all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                 for label in hostname.split('.')), 'HOSTNAME', 2)
        if obj.get('rung') == 'T1':
            refs = obj['required_anchor_refs']
            _require(len(refs) == len(set(refs)) and obj['anchor_ref'] in refs, 'TARGET_FRAME')
            _require(len(refs) != 1 or obj['consistency_tolerance_px'] == 0, 'TARGET_TOLERANCE')
            _require((obj['frame_kind'] == 'viewport') == (obj['relative_xy_bps'] is not None), 'TARGET_FRAME')
        if 'tags' in obj:
            _require(len(obj['tags']) == len(set(obj['tags'])), 'TAGS')
        if set(('name', 'type', 'value')) <= obj.keys():
            types = {'integer': int, 'boolean': bool, 'string': str, 'integer_box': list}
            _require(type(obj['value']) is types[obj['type']], 'BINDING', 2)
            if obj['type'] == 'integer_box':
                _require(len(obj['value']) == 4 and all(type(x) is int for x in obj['value']), 'BINDING', 2)
                _box(obj['value'])
        if 'required_capabilities' in obj:
            _owner_check(ctx.capability_check, (obj, None), 'CAPABILITY_MISSING')
        if 'capability_id' in obj:
            _owner_check(ctx.capability_check, (obj, obj if 'comparison' in obj else None),
                         'CAPABILITY_MISSING')
        if 'provenance' in obj:
            p = obj['provenance']
            source = _reference(ctx.root, p['source_ref'].split('#')[0])
            raw = _read(source, MAX_FILE, owner=ctx.root)
            if p['source'] == 'human':
                _require(check_explanation(source).rc == 0, 'PROVENANCE')
            else:
                _require(p.get('receipt_sha256') is not None and
                         hashlib.sha256(raw).hexdigest() == p['receipt_sha256'], 'PROVENANCE_PIN', 3)
            digest = p.get('quote_sha256')
            if digest:
                if '#' in p['source_ref']:
                    raw = extract_span(raw, p['source_ref'].split('#')[1])
                _require(hashlib.sha256(raw).hexdigest() == digest, 'PROVENANCE_PIN', 3)
            _owner_check(ctx.evidence_check, ('provenance', obj, related), 'EVIDENCE_MISSING')
        if 'expires' in obj and obj['status'] == 'active':
            _require(ctx.as_of is not None, 'AS_OF_MISSING')
            _require(obj['expires'] is None or date.fromisoformat(obj['expires']) >= ctx.as_of,
                     'EXPIRED')


def _verifier(v, ctx, related, depth=0, parent=None):
    _require(depth <= 1, 'VERIFIER_DEPTH')
    _require(v['poll_interval_ms'] * v['consecutive_polls'] <= v['deadline_ms'], 'POLLING')
    if parent:
        _require(all(v[k] == parent[k] for k in ('predicate', 'deadline_ms', 'busy_predicate')),
                 'VERIFIER_EQUIVALENCE')
    if v['mode'] in ('state_signal', 'side_effect'):
        _require(v['predicate'] is not None, 'VERIFIER_PREDICATE')
    else:
        _require(v['ram_evidence_ref'] is not None, 'RAM_EVIDENCE_MISSING')
        _owner_check(ctx.evidence_check, ('ram', v, related), 'RAM_EVIDENCE_MISSING')
    for child in v['fallback_verifiers']:
        _verifier(child, ctx, related, depth + 1, v)


def _steps(steps, slots, ctx, related):
    captures = {}
    _require([s['seq'] for s in steps] == list(range(1, len(steps) + 1)), 'STEP_SEQUENCE')
    for s in steps:
        _require(not s['protected'] or s['when'] is None, 'PROTECTED')
        _require(s['locate_wait_ms'] == 0 or s['locate_poll_ms'] <= s['locate_wait_ms'], 'POLLING')
        for c in s['capture_before']:
            p = c['observe']
            _require(c['name'] not in captures and p['comparison'] == 'truth' and
                     p['snapshot_ref'] is None, 'CAPTURE')
            captures[c['name']] = p
        for p in _objects(s):
            if 'comparison' in p:
                ref = p['snapshot_ref']
                if p['comparison'] == 'truth':
                    _require(ref is None, 'SNAPSHOT')
                else:
                    _require(ref in captures, 'SNAPSHOT')
                    old = captures[ref]
                    _require(all(old[k] == p[k] for k in ('kind', 'capability_id', 'bindings')),
                             'SNAPSHOT_COMPATIBILITY')
        a = s['action']
        kind, target = a['kind'], s['target']
        focus = any(p['kind'] == 'state_signal' for p in s['preconditions'])
        grounded = lambda t: t is not None and t.get('rung') in ('T0', 'T1', 'T2')
        if kind in ('click', 'right_click', 'double_click', 'drag'):
            _require(grounded(target) and all(grounded(t) for t in s['fallback_targets']), 'ACTION_TARGET')
        if kind in ('paste', 'key') and target is None:
            _require(focus, 'ACTION_FOCUS')
            _owner_check(ctx.evidence_check, ('focus', s, related), 'FOCUS_EVIDENCE_MISSING')
        if kind == 'paste':
            _require(a['text_slot'] in slots, 'ACTION_SLOT')
        if kind == 'key':
            _require(a['key_name'] is not None, 'ACTION_KEY')
        if kind == 'drag':
            displacement = a['dx_px'] is not None and a['dy_px'] is not None
            _require((grounded(a['destination']) and a['dx_px'] is None and a['dy_px'] is None) or
                     (a['destination'] is None and displacement), 'ACTION_DRAG')
            _owner_check(ctx.evidence_check, ('drag_start', s, related), 'EVIDENCE_MISSING')
        if kind == 'scroll':
            _require(a['dx_px'] is not None and a['dy_px'] is not None, 'ACTION_SCROLL')
        relevant = {'button': ('click', 'right_click', 'double_click', 'drag'),
                    'text_slot': ('paste',), 'key_name': ('key',),
                    'dx_px': ('drag', 'scroll'), 'dy_px': ('drag', 'scroll'), 'destination': ('drag',)}
        _require(all(a[k] is None for k, kinds in relevant.items() if kind not in kinds), 'ACTION_FIELDS')
        _verifier(s['verify'], ctx, related)
        if s['repeat'] is not None or s['when'] is not None or s['verify']['busy_predicate'] is not None:
            _owner_check(ctx.evidence_check, ('ordering', s, related), 'EVIDENCE_MISSING')
    for slot in slots.values():
        _require(len(set(slot['chip_clear_steps'])) == len(slot['chip_clear_steps']), 'CHIP_ORDER')
        for seq in slot['chip_clear_steps']:
            _require(1 <= seq <= len(steps) and steps[seq - 1]['action']['kind'] != 'paste', 'CHIP_ORDER')
        pastes = [s['seq'] for s in steps if s['action']['text_slot'] == slot['slot_id']]
        _require(all(c < p for c in slot['chip_clear_steps'] for p in pastes), 'CHIP_ORDER')
        positions = []
        for seq in slot['chip_clear_steps']:
            step = steps[seq - 1]
            target = step['target']
            _require(step['action']['kind'] == 'click' and target is not None and
                     target.get('rung') in ('T0', 'T1', 'T2') and
                     step['preconditions'] and step['verify']['predicate'] is not None,
                     'CHIP_GROUNDING')
            view = _evidence('chip_geometry', step, ctx, related)
            _require(type(view.get('center_x_px')) is int and view.get('guard_pass') is True and
                     view.get('postcondition_pass') is True, 'CHIP_GROUNDING')
            _require(type(view.get('center_y_px')) is int and type(view.get('chip_id')) is str, 'CHIP_GROUNDING')
            positions.append((-view['center_x_px'], view['center_y_px'], view['chip_id']))
        _require(all(a < b for a, b in zip(positions, positions[1:])), 'CHIP_ORDER')


def _evidence(kind, value, ctx, related):
    _require(callable(ctx.evidence_load), 'EVIDENCE_MISSING')
    try:
        view = ctx.evidence_load(kind, value, related)
    except Exception:
        raise _Refusal('EVIDENCE_OWNER') from None
    _require(type(view) is dict, 'EVIDENCE_MISSING')
    _owner_check(ctx.evidence_check, (kind, value, (view, *related)), 'EVIDENCE_MISSING')
    return view


def _distinct(values, category):
    _require(type(values) is list and all(type(v) is str for v in values) and
             len(values) == len(set(values)), category)
    return set(values)


def _integer(value, category, minimum=0):
    _require(type(value) is int and value >= minimum, category)
    return value


def _run_inventory(value, ctx, related):
    view = _evidence('run_inventory', value, ctx, related)
    _require(view.get('workflow_sha256') == value['workflow_sha256'], 'RUN_PIN', 3)
    steps = view.get('steps')
    _require(type(steps) is list and len(steps) == value['steps_total'], 'COUNTS_INVENTORY')
    seqs = [s.get('seq') for s in steps]
    _require(all(type(n) is int for n in seqs) and sorted(seqs) == list(range(1, len(steps) + 1)),
             'COUNTS_INVENTORY')
    groups = {'verified': 'steps_verified', 'satisfied_skip': 'steps_skipped_satisfied',
              'condition_skip': 'steps_skipped_condition', 'failed': 'steps_failed',
              'not_reached': 'steps_not_reached'}
    for step in steps:
        disposition = step.get('disposition')
        _require(disposition in groups, 'COUNTS_INVENTORY')
        if disposition in ('verified', 'satisfied_skip'):
            _require(step.get('predicate_pass') is True, 'COUNTS_EVIDENCE')
        if disposition == 'condition_skip':
            _require(step.get('optional') is True and step.get('guard_false') is True,
                     'COUNTS_EVIDENCE')
    for disposition, field in groups.items():
        _require(sum(s['disposition'] == disposition for s in steps) == value[field], 'COUNTS')
    failed = [s['seq'] for s in steps if s['disposition'] == 'failed']
    _require(value['failed_step_seq'] == (min(failed) if failed else None), 'COUNTS')
    segments = view.get('required_segments')
    _require(type(segments) is dict, 'REPRODUCTION_EVIDENCE')
    by_seq = {s['seq']: s for s in steps}
    verified = 0
    for segment, members in segments.items():
        _require(type(segment) is str and type(members) is list and members and
                 all(type(n) is int and n in by_seq for n in members) and
                 len(members) == len(set(members)), 'REPRODUCTION_EVIDENCE')
        _require(all(by_seq[n]['disposition'] != 'condition_skip' for n in members), 'REPRODUCTION_EVIDENCE')
        verified += all(by_seq[n]['disposition'] in ('verified', 'satisfied_skip') for n in members)
    metric = verified * 1000000 // len(segments) if segments else None
    _require(value['reproduced_ppm'] == metric, 'REPRODUCTION')
    _require(value['rc'] != 0 or view.get('final_task_pass') is True, 'FINAL_ORACLE')


def _lifecycle(value, ctx, related):
    view = _evidence('lifecycle', value, ctx, related)
    _require(view.get('killed_mutant_ids') is not None and
             _distinct(view['killed_mutant_ids'], 'MUTANT_EVIDENCE') == set(value['oracle']['planted_mutant_ids']),
             'MUTANT_EVIDENCE')
    _require(view.get('protected_pass') is True and view.get('supported') is True, 'ADMISSION_SUPPORT')
    stats = value['stats']
    if value['admission_basis'] == 'operator_demo':
        runs = view.get('admission_runs')
        _require(type(runs) is list and len(runs) >= 2, 'DEMO_ADMISSION')
        ids = [r.get('id') for r in runs]
        _distinct(ids, 'DEMO_ADMISSION')
        _require(all(r.get('verified') is True and r.get('independent') is True and
                     r.get('reproduced_ppm') == 1000000 for r in runs), 'DEMO_ADMISSION')
        if view.get('task_scope') == 'project_mutating':
            _require(any(r.get('operator_demo') is True for r in runs) and
                     any(r.get('supervised') is True and r.get('authorized') is True and
                         not r.get('operator_demo') for r in runs), 'DEMO_ADMISSION')
        else:
            _require(view.get('task_scope') in ('read_only', 'resettable'), 'DEMO_ADMISSION')
    else:
        episodes = view.get('episodes')
        _require(type(episodes) is list and len(episodes) >= 3, 'DREAM_SUPPORT')
        _distinct([e.get('id') for e in episodes], 'DREAM_SUPPORT')
        rewards, savings = [], []
        for e in episodes:
            _require(e.get('closed') is True and e.get('supported') is True, 'DREAM_SUPPORT')
            base = _integer(e.get('tokens_base'), 'DREAM_DENOMINATOR', 1)
            cand = _integer(e.get('tokens_cand'), 'DREAM_EVIDENCE')
            pb, pc = e.get('pass_base'), e.get('pass_cand')
            _require(type(pb) is int and pb in (0, 1) and type(pc) is int and pc in (0, 1), 'DREAM_EVIDENCE')
            rewards.append(1000 * (pc - pb) - (cand - base) // 10)
            savings.append((base - cand) * 10000 // base)
        _require(min(rewards) >= 0 and min(savings) >= 1500, 'DREAM_REWARD')
        _require(stats['min_r_e'] == min(rewards) and stats['saved_tokens_bps'] == min(savings), 'DREAM_STATS')
    profile = value['evaluation_profile']
    initial = profile['kind'] == 'initial_task_v1'
    _require((value['baseline_id'] is None) == initial and
             (profile['baseline_workflow_sha256'] is None) == initial, 'BASELINE')
    if value['status'] != 'promoted':
        return
    cohort, trials = view.get('cohort'), view.get('trials')
    _require(type(cohort) is dict and type(trials) is list, 'COHORT_MISSING')
    _require(hashlib.sha256(canon(cohort).encode()).hexdigest() == profile['cohort_sha256'] and
             view.get('profile_sha256') == hashlib.sha256(canon(profile).encode()).hexdigest(), 'COHORT_PIN', 3)
    tasks = _distinct(cohort.get('task_ids'), 'COHORT_TASKS')
    _require(len(tasks) >= 3 and cohort.get('fixture_ids') == profile['fixture_ids'], 'COHORT_TASKS')
    fixtures = _distinct(profile['fixture_ids'], 'COHORT_FIXTURES')
    _require(_distinct([t.get('task_id') for t in trials], 'COHORT_TASKS') == tasks, 'COHORT_TASKS')
    if not initial:
        _require(view.get('baseline_eligible') is True and
                 view.get('baseline_workflow_sha256') == profile['baseline_workflow_sha256'] and
                 view.get('baseline_id') == value['baseline_id'], 'BASELINE')
    gain = 0
    for t in trials:
        _require(all(t.get(k) is True for k in ('authorized', 'live', 'supported', 'required_pass',
                                               'reviewed', 'semantic_verification')), 'COHORT_SUPPORT')
        candidate = t.get('candidate')
        _require(type(candidate) is dict and type(candidate.get('task_pass')) is int and
                 candidate['task_pass'] == 1 and type(candidate.get('fixtures')) is dict and
                 set(candidate['fixtures']) == fixtures and
                 all(type(v) is bool for v in candidate['fixtures'].values()), 'COHORT_CASES')
        _require(t.get('regression') is False, 'REGRESSION')
        if initial:
            _require(all(candidate['fixtures'].values()), 'COHORT_CASES')
            gain += 10000
        else:
            baseline = t.get('baseline')
            _require(type(baseline) is dict and type(baseline.get('task_pass')) is int and
                     baseline['task_pass'] in (0, 1) and type(baseline.get('fixtures')) is dict and
                     set(baseline['fixtures']) == fixtures and
                     all(type(v) is bool for v in baseline['fixtures'].values()), 'COHORT_CASES')
            _require(all(not baseline['fixtures'][f] or candidate['fixtures'][f] for f in fixtures) and
                     candidate['task_pass'] >= baseline['task_pass'], 'REGRESSION')
            ct = (candidate['task_pass'], sum(candidate['fixtures'].values()))
            bt = (baseline['task_pass'], sum(baseline['fixtures'].values()))
            if ct == bt:
                ct += (-_integer(candidate.get('tokens_used'), 'COHORT_TOKENS'),)
                bt += (-_integer(baseline.get('tokens_used'), 'COHORT_TOKENS'),)
            gain += (ct > bt) - (ct < bt)
    mass, bound = len(tasks), 9500 if initial else 0
    _require((stats['margin_gain'], stats['margin_mass'], stats['margin_bound']) == (gain, mass, bound), 'MARGIN_STATS')
    _require(gain > mass * bound, 'MARGIN')
    _require(stats['online_shadow_runs'] == mass and stats['shadow_regressions'] == 0, 'COHORT_STATS')


def _script(w, path, defs, ctx):
    ref, pin = w['script_ref'], w['script_sha256']
    _require((ref is None) == (pin is None), 'SCRIPT_PAIR')
    if ref is None:
        return
    _require(re.fullmatch(r'scripts/[A-Za-z0-9_-]+\.py', ref) is not None, 'SCRIPT_PATH')
    raw = _read(_reference(ctx.root, ref), 32768, owner=ctx.root)
    _require(raw.endswith(b'\n'), 'SCRIPT_HEADER')
    _require(hashlib.sha256(raw).hexdigest() == pin, 'SCRIPT_PIN', 3)
    lines = raw.splitlines()
    if lines and lines[0].startswith(b'#!'):
        lines = lines[1:]
    prefix = b'# MW-CAPS: '
    _require(lines and lines[0].startswith(prefix) and
             sum(x.startswith(prefix) for x in lines) == 1, 'SCRIPT_HEADER')
    payload = lines[0][len(prefix):]
    caps = strict_load(payload)
    _require(payload == canon(caps).encode() and conforms(caps, defs['caps'], defs), 'SCRIPT_CAPS', 2)
    _owner_check(ctx.evidence_check, ('script', w, (caps,)), 'EVIDENCE_MISSING')


def _public_scan(value, *, digest_fields=frozenset()):
    forbidden = re.compile(
        r'(?i)(?:password|passwd|secret|token|cookie|bearer|session|capsule|api[_-]?key|'
        r'data:|base64|traceback|https?://|file://|(?<![a-z0-9])/[a-z0-9_.~-]+/|'
        r'[A-Z]:\\|[\w.+-]+@[\w.-]+|[a-f0-9]{32,}|[A-Za-z0-9+/]{80,}={0,2})')
    if type(value) is dict:
        for k, v in value.items():
            if k not in digest_fields:
                _public_scan(k)
                _public_scan(v)
    elif type(value) is list:
        for v in value:
            _public_scan(v)
    elif type(value) is str:
        _require(forbidden.search(value) is None, 'PRIVACY')
        _require(not re.search(r'(?i)(?:pixels?|screenshots?|image[_-]?bytes)\s*[:=]\s*\[', value), 'PRIVACY')


def _publication(value, raw, ctx, related, defs):
    # Field exemptions apply only at the closed publication_receipt root.
    _public_scan(value, digest_fields={'workflow_sha256', 'evaluation_profile_sha256',
                                      'prev_receipt_sha256'})
    profiles, cohorts = [], []
    for ref in value['public_evidence_refs']:
        content = _read(_reference(ctx.root, ref), MAX_FILE, owner=ctx.root)
        if ref.endswith('.json'):
            document, kind = _public_document(content, defs)
            (profiles if kind == 'profile' else cohorts).append(document)
        else:
            _public_scan(content.decode('utf-8'))
        _owner_check(ctx.review_check, (content, 'public_evidence'), 'REVIEW_MISSING')
    if profiles:
        _require(len(profiles) == 1 and hashlib.sha256(canon(profiles[0]).encode()).hexdigest() ==
                 value['evaluation_profile_sha256'], 'PUBLIC_PROFILE_PIN', 3)
        _require(len(cohorts) == 1 and hashlib.sha256(canon(cohorts[0]).encode()).hexdigest() ==
                 profiles[0]['cohort_sha256'] and value['cohort_task_id'] in cohorts[0]['task_ids'] and
                 profiles[0]['fixture_ids'] == cohorts[0]['fixture_ids'], 'PUBLIC_COHORT_PIN', 3)
    _require((value['evaluation_profile_sha256'] is None) == (value['cohort_task_id'] is None),
             'PUBLICATION_COHORT')
    if value['cohort_task_id'] is not None:
        _require(value['mode'] == 'shadow', 'PUBLICATION_COHORT')
        matches = [r for r in related if r.get('schema') == 'mw.publication_receipt.v1' and
                   all(r.get(k) == value[k] for k in ('workflow_id', 'evaluation_profile_sha256', 'cohort_task_id'))]
        _require(len(matches) == 1, 'PUBLICATION_COHORT')
        _owner_check(ctx.evidence_check, ('public_cohort', value, related), 'EVIDENCE_MISSING')
    _owner_check(ctx.review_check, (raw, 'publication'), 'REVIEW_MISSING')


def _inferred_root(path):
    parts = Path(path).absolute().parts
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] in ('sites', 'patterns') and i + 2 < len(parts):
            return Path(*parts[:i])
    return Path(path).absolute().parent


def _graph(records, ctx):
    values = [v for v, _, _ in records]
    nodes = {(v['owner_kind'], v['owner_id'], v['id']): (v, raw)
             for v, raw, _ in records if v['schema'] == 'mw.node.v1'}
    owners = {(v['schema'][3:-3], v['id']) for v in values
              if v['schema'] in ('mw.site.v1', 'mw.pattern.v1')}
    workflows = {(v['site_id'], v['id']): (v, raw) for v, raw, _ in records
                 if v['schema'] == 'mw.workflow.v1'}
    demos = {(v['site_id'], v['demo_id']) for v in values if v['schema'] == 'mw.demo.v1'}
    registries = [v for v in values if v['schema'] == 'mw.tags.v1']
    _require(len(registries) <= 1, 'TAGS')
    facets = registries[0]['facets'] if registries else {}
    for tokens in facets.values():
        _distinct(tokens, 'TAGS')
    known = set().union(*map(set, facets.values())) if facets else set()
    for v in values:
        kind = v['schema'][3:-3]
        if kind == 'node':
            _require((v['owner_kind'], v['owner_id']) in owners, 'OWNER_MISSING')
            tags = set(v['tags'])
            if v['status'] == 'active':
                _require(tags <= known, 'TAGS')
                needed = {'domain'}
                if v['kind'] in ('workflow', 'control', 'particularity', 'state_model', 'rule', 'lesson'):
                    needed.add('action')
                if v['kind'] == 'control':
                    needed.add('ui-control')
                if v['kind'] == 'lesson':
                    needed.add('failure-mode')
                _require(all(tags & set(facets.get(f, [])) for f in needed), 'TAG_FACETS')
            target = v['pointer_target']
            _require((v['kind'] == 'pointer') == (target is not None), 'POINTER')
            if target is not None:
                key = (target['owner_kind'], target['owner_id'], target['node_id'])
                _require(key in nodes and key != (v['owner_kind'], v['owner_id'], v['id']), 'POINTER')
                _require(nodes[key][0]['source_sha256'] == target['source_sha256'], 'POINTER_PIN', 3)
        if kind == 'site':
            _require(all(('pattern', p) in owners for p in v['patterns']), 'PATTERN_MISSING')
        if kind in ('workflow', 'demo'):
            _require(('site', v['site_id']) in owners, 'OWNER_MISSING')
        if kind in ('workflow', 'demo_steps'):
            for obj in _objects(v):
                if obj.get('rung') == 'T0':
                    _require((v['site_id'], obj['demo_id']) in demos, 'DEMO_MISSING')
        if kind in ('run', 'publication_receipt'):
            target = workflows.get((v['site_id'], v['workflow_id']))
            # Public-only corpus projections may contain no private workflow payload.
            if kind == 'run' or target is not None:
                _require(target is not None, 'WORKFLOW_MISSING')
                _require(hashlib.sha256(target[1]).hexdigest() == v['workflow_sha256'], 'WORKFLOW_PIN', 3)
        if kind == 'workflow':
            fallback = v['fallback']
            _require((fallback['kind'] == 'workflow') == (fallback['workflow_id'] is not None), 'FALLBACK')
            if fallback['workflow_id'] is not None:
                _require((v['site_id'], fallback['workflow_id']) in workflows and
                         fallback['workflow_id'] != v['id'], 'FALLBACK')
            if v['baseline_id'] is not None:
                target = workflows.get((v['site_id'], v['baseline_id']))
                _require(target is not None and target[0]['status'] == 'promoted', 'BASELINE')
                _require(hashlib.sha256(target[1]).hexdigest() ==
                         v['evaluation_profile']['baseline_workflow_sha256'], 'BASELINE_PIN', 3)
        if kind == 'detail':
            target = nodes.get((v['owner_kind'], v['owner_id'], v['node_id']))
            _require(target is not None, 'DETAIL_NODE')
            _require(hashlib.sha256(target[1]).hexdigest() == v['node_sha256'] and
                     target[0]['anchor'] == v['anchor'] and
                     target[0]['source_sha256'] == v['source_sha256'], 'DETAIL_PIN', 3)
        if kind == 'capstone':
            expected = {key[2] for key in nodes if key[:2] == (v['owner_kind'], v['owner_id'])}
            _require(_distinct(v['node_ids'], 'CAPSTONE') == expected, 'CAPSTONE')
            sites = {site['id'] for site in values if site['schema'] == 'mw.site.v1' and
                     v['owner_kind'] == 'pattern' and v['owner_id'] in site['patterns']}
            _require(_distinct(v['defined_in'], 'CAPSTONE') == sites, 'CAPSTONE')
            _distinct(v['support_receipt_refs'], 'CAPSTONE')
            view = _evidence('capstone_counts', v, ctx, tuple(values))
            receipts = view.get('receipts')
            _require(type(receipts) is list and
                     _distinct([r.get('ref') for r in receipts], 'CAPSTONE') == set(v['support_receipt_refs']), 'CAPSTONE')
            for receipt in receipts:
                _require(type(receipt.get('falsified')) is bool, 'CAPSTONE')
                owner = Path(ctx.root) / ('sites' if v['owner_kind'] == 'site' else 'patterns') / v['owner_id']
                raw = _read(_reference(owner, receipt['ref']), MAX_FILE, owner=owner)
                _require(hashlib.sha256(raw).hexdigest() == receipt.get('sha256'), 'CAPSTONE_PIN', 3)
            _require(sum(r['falsified'] for r in receipts) == v['falsifications'], 'CAPSTONE')


def _public_document(content, defs):
    value = strict_load(content)
    _require(content == (canon(value) + '\n').encode(), 'CANONICAL', 2)
    if conforms(value, defs['evaluation_profile'], defs):
        # Only these closed schema properties carry digest exemptions.
        _public_scan(value, digest_fields={'cohort_sha256', 'baseline_workflow_sha256'})
        return value, 'profile'
    _require(type(value) is dict and set(value) == {'task_ids', 'fixture_ids'}, 'PUBLIC_EVIDENCE', 2)
    for field in ('task_ids', 'fixture_ids'):
        _require(bool(_distinct(value[field], 'PUBLICATION_COHORT')) and
                 all(conforms(v, defs['value_3'], defs) for v in value[field]), 'PUBLIC_EVIDENCE', 2)
    _public_scan(value)
    return value, 'cohort'


def _owner_root(path, ctx):
    relative = path.relative_to(Path(ctx.root).absolute())
    if len(relative.parts) >= 3 and relative.parts[0] in ('sites', 'patterns'):
        return Path(ctx.root).absolute().joinpath(*relative.parts[:2])
    return Path(ctx.root).absolute()


def _artifact_root(value, path, ctx):
    if value.get('schema') in ('mw.detail.v1', 'mw.capstone.v1') and path.relative_to(ctx.root).parts[0] == 'catalogue':
        return Path(ctx.root) / ('sites' if value['owner_kind'] == 'site' else 'patterns') / value['owner_id']
    return _owner_root(path, ctx)


def _identity(value, path, root):
    kind = value['schema'][3:-3]
    identity = value.get('id') or (value.get('demo_id') if kind == 'demo' else None)
    if identity:
        expected = path.parent.name if kind in ('site', 'demo', 'pattern') else path.stem
        _require(identity == expected, 'IDENTITY')
    relative = path.relative_to(root).parts
    if 'owner_id' in value and 'owner_kind' in value:
        folder = 'sites' if value['owner_kind'] == 'site' else 'patterns'
        _require(tuple(relative[:2]) == (folder, value['owner_id']) or
                 (kind in ('detail', 'capstone') and relative[0] == 'catalogue'), 'IDENTITY')
    if kind == 'demo_steps' and 'demos' in relative:
        i = relative.index('demos')
        _require(len(relative) > i + 2 and value['demo_id'] == relative[i + 1], 'IDENTITY')
    if 'site_id' in value and relative and relative[0] == 'sites':
        _require(len(relative) > 2 and relative[1] == value['site_id'], 'IDENTITY')


def _artifact(value, raw, path, defs, ctx, related):
    _require(type(value) is dict and type(value.get('schema')) is str, 'ROOT', 2)
    kind = value['schema'][3:-3]
    _require(kind in ROOTS and value.get('schema') == 'mw.' + kind + '.v1', 'ROOT', 2)
    _require(len(raw) <= LIMITS.get(kind, MAX_FILE), 'CAPACITY')
    _require(conforms(value, defs[kind], defs), 'SCHEMA', 2)
    _identity(value, path, Path(ctx.root).absolute())
    ctx = replace(ctx, root=_artifact_root(value, path, ctx))
    _semantic_tree(value, ctx, related)
    if kind == 'step':
        _steps([value], {}, ctx, related)
    if kind == 'workflow':
        slots = {s['slot_id']: s for s in value['text_slots']}
        _require(len(slots) == len(value['text_slots']), 'DUPLICATE_ID')
        _steps(value['steps'], slots, ctx, related)
        oracle = value['oracle']
        ids = oracle['planted_mutant_ids']
        _require(len(ids) == len(set(ids)) and oracle['mutants_total'] == len(ids) and
                 oracle['mutants_killed'] <= len(ids), 'MUTANTS')
        if value['status'] in ('shadow', 'promoted'):
            _require(len(ids) >= 3 and oracle['mutants_killed'] == len(ids) and
                     bool(oracle['receipt_refs']), 'ADMISSION')
            _lifecycle(value, ctx, related)
        elif any(v is not None for v in value['stats'].values()):
            _owner_check(ctx.evidence_check, ('statistics', value, related), 'EVIDENCE_MISSING')
        profile = value['evaluation_profile']
        if profile['kind'] == 'initial_task_v1':
            _require(value['baseline_id'] is None and profile['baseline_workflow_sha256'] is None,
                     'BASELINE')
        else:
            _require(value['baseline_id'] is not None and profile['baseline_workflow_sha256'] is not None,
                     'BASELINE')
        _script(value, path, defs, ctx)
    if kind == 'demo_steps':
        _require([s['seq'] for s in value['steps']] == list(range(1, len(value['steps']) + 1)),
                 'STEP_SEQUENCE')
    if kind == 'demo':
        for ref, pin in ((value['steps_file'], value['steps_sha256']),
                         (value['explanation']['file'], value['explanation']['sha256'])):
            source = _reference(path.parent, ref)
            content = _read(source, 16384 if ref == 'EXPLANATION.md' else 65536, owner=ctx.root)
            _require(hashlib.sha256(content).hexdigest() == pin, 'DEMO_PIN', 3)
            if ref == 'EXPLANATION.md':
                _require(check_explanation(source).rc == 0, 'EXPLANATION', 2)
            else:
                steps = strict_load(content)
                _require(content == (canon(steps) + '\n').encode(), 'CANONICAL', 2)
                _require(conforms(steps, defs['demo_steps'], defs) and
                         steps['site_id'] == value['site_id'] and steps['demo_id'] == value['demo_id'],
                         'DEMO_STEPS', 2)
    if kind == 'demo_request':
        lexical = {'no_query_term', 'no_candidate', 'below_tau', 'low_term_match', 'low_idf_cov', 'flat_peak'}
        _require(value['trigger_reason'] not in lexical or
                 value['router_reason'] == value['trigger_reason'], 'REQUEST_LINK')
        _owner_check(ctx.evidence_check, ('request', value, related), 'EVIDENCE_MISSING')
    if kind == 'publication_receipt':
        _publication(value, raw, ctx, related, defs)
    if kind == 'run':
        counts = [value[k] for k in ('steps_verified', 'steps_skipped_satisfied',
                  'steps_skipped_condition', 'steps_failed', 'steps_not_reached')]
        _require(sum(counts) == value['steps_total'], 'COUNTS')
        _require(value['rc'] != 0 or (counts[3:] == [0, 0] and value['failed_step_seq'] is None), 'COUNTS')
        _run_inventory(value, ctx, related)
    if kind in ('mounts', 'overrides', 'capstone', 'detail', 'pattern'):
        _owner_check(ctx.evidence_check, (kind, value, related), 'EVIDENCE_MISSING')
    if kind in ('node', 'detail', 'pattern'):
        reference = value['behaviour_anchor'] if kind == 'pattern' else value['anchor']
        _require(reference.count('#') == 1, 'ANCHOR_MISSING', 3)
        ref, anchor = reference.split('#')
        content = _read(_reference(ctx.root, ref), MAX_FILE, owner=ctx.root)
        span = extract_span(content, anchor)
        if kind != 'pattern':
            _require(hashlib.sha256(span).hexdigest() == value['source_sha256'], 'SPAN_PIN', 3)


def _parse(raw):
    value = strict_load(raw)
    _require(raw == (canon(value) + '\n').encode('utf-8'), 'CANONICAL', 2)
    return value


def _load(path, ctx):
    named_caps = {'site.json': 8192, 'manifest.json': 32768, 'steps.json': 65536,
                  'receipts.jsonl': 65536}
    budget = ctx.journal_read_budget
    if path.suffix == '.jsonl' and budget is not None:
        _require(type(budget) is int and budget > 0, 'JOURNAL_BUDGET_MISSING')
    raw = _read(path, budget if path.suffix == '.jsonl' and budget is not None else named_caps.get(path.name, MAX_FILE), owner=ctx.root)
    if path.suffix == '.jsonl':
        _require(raw.endswith(b'\n'), 'TORN', 3)
        raws = tuple(raw.splitlines(keepends=True))
        values = tuple(_parse(line) for line in raws)
        _require(bool(values), 'JOURNAL', 2)
        first_kind = values[0].get('schema')
        if first_kind == 'mw.run.v1':
            _require(type(budget) is int and budget > 0, 'JOURNAL_BUDGET_MISSING')
            _require(len(raw) <= budget, 'CAPACITY')
        if first_kind == 'mw.publication_receipt.v1':
            _require(len(raw) <= 65536, 'CAPACITY')
        _require(first_kind in ('mw.run.v1', 'mw.publication_receipt.v1', 'mw.capstone.v1'), 'JOURNAL', 2)
        for seq, value in enumerate(values, 1):
            _require(value.get('schema') == first_kind and value.get('seq') == seq, 'SEQUENCE', 3)
            _require((seq != 1 or value.get('prev_receipt_sha256') is None) and
                     (seq == 1 or value.get('prev_receipt_sha256') is not None), 'PREDECESSOR', 3)
        # The trusted owner supplies the installed domain/framing rule, not plain SHA guessing.
        _owner_check(ctx.chain_check, (values, raws), 'CHAIN_MISSING')
        return list(zip(values, raws))
    _require(path.suffix == '.json', 'UNCLASSIFIED', 2)
    return [(_parse(raw), raw)]


def _lineage(records):
    nodes = {}
    for value in records:
        if value.get('schema') == 'mw.node.v1':
            key = (value['owner_kind'], value['owner_id'], value['id'])
            _require(key not in nodes, 'DUPLICATE_ID')
            nodes[key] = value
    successors = {k: [] for k in nodes}
    for key, value in nodes.items():
        for old in value['supersedes']:
            predecessor = key[:2] + (old,)
            _require(predecessor in nodes, 'LINEAGE_MISSING')
            successors[predecessor].append(key)
    visiting, done = set(), set()
    def visit(key):
        _require(key not in visiting, 'LINEAGE_CYCLE')
        if key in done:
            return
        visiting.add(key)
        for successor in successors[key]:
            visit(successor)
        visiting.remove(key)
        done.add(key)
    for key in nodes:
        visit(key)
    # Each connected lineage component has at most one active terminal node.
    seen = set()
    for key in nodes:
        if key in seen:
            continue
        pending, component = [key], set()
        while pending:
            k = pending.pop()
            if k in component:
                continue
            component.add(k)
            pending += successors[k] + [k[:2] + (old,) for old in nodes[k]['supersedes']]
        seen |= component
        active = [k for k in component if nodes[k]['status'] == 'active']
        _require(len(active) <= 1 and all(not successors[k] for k in active), 'LINEAGE_TAIL')


# @anchor:wa.tools.memoryweb_gate.validate_file
@_boundary
def validate_file(path: Path, bundle: dict, *, context=None) -> GateResult:
    path = Path(path).absolute()
    ctx = context or ValidationContext(_inferred_root(path))
    defs = compile_bundle(bundle)
    records = _load(path, ctx)
    related = tuple(v for v, _ in records)
    for value, raw in records:
        _artifact(value, raw, path, defs, ctx, related)
    _lineage(related)


# @anchor:wa.tools.memoryweb_gate.validate_directory
@_boundary
def validate_directory(path: Path, bundle: dict, *, context=None) -> GateResult:
    path = Path(path).absolute()
    ctx = context or ValidationContext(path)
    defs = compile_bundle(bundle)
    files, total, records = [], 0, []
    # An explicit stack also bounds hostile directory depth and empty-directory fan-out.
    pending, directories = [(path, 0)], 0
    while pending:
        directory, depth = pending.pop()
        directories += 1
        _require(depth <= 64 and directories <= MAX_FILES, 'CAPACITY')
        _require(not directory.is_symlink(), 'CONTAINMENT')
        with os.scandir(directory) as entries:
            for entry in entries:
                _require(not entry.is_symlink(), 'CONTAINMENT')
                child = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append((child, depth + 1))
                else:
                    files.append(child)
                    _require(len(files) <= MAX_FILES, 'CAPACITY')
                    total += entry.stat(follow_symlinks=False).st_size
                    _require(total <= MAX_BYTES, 'CAPACITY')
    _require(bool(files), 'EMPTY')
    auxiliary = {}
    for file in sorted(files):
        if file.suffix == '.json':
            candidate = _parse(_read(file, MAX_FILE, owner=path))
            if type(candidate) is dict and 'schema' not in candidate:
                auxiliary[file] = candidate
                continue
        if file.suffix in ('.json', '.jsonl'):
            records.extend((v, raw, file) for v, raw in _load(file, ctx))
        elif file.name == 'EXPLANATION.md':
            result = check_explanation(file)
            _require(result.rc == 0, result.category, result.rc)
        else:
            # Referenced sources are inert; unreferenced/unclassified bytes are refused.
            _require(file.suffix in ('.md', '.py', '.sh'), 'UNCLASSIFIED', 2)
    related = tuple(v for v, _, _ in records)
    _require(bool(related), 'EMPTY')
    identities = set()
    tasks = set()
    for value, raw, file in records:
        _require(type(value) is dict, 'SCHEMA', 2)
        key = (value.get('schema'), value.get('owner_id', value.get('site_id')),
               value.get('id', value.get('demo_id', value.get('run_id'))))
        if key[2] is not None and value.get('schema') != 'mw.demo_steps.v1':
            _require(key not in identities, 'DUPLICATE_ID')
            identities.add(key)
        if value.get('schema') == 'mw.publication_receipt.v1' and value.get('cohort_task_id'):
            task = (value['workflow_id'], value['evaluation_profile_sha256'], value['cohort_task_id'])
            _require(task not in tasks, 'PUBLICATION_COHORT')
            tasks.add(task)
    for value in related:
        kind = value.get('schema', '')
        _require(type(kind) is str and kind.startswith('mw.') and kind.endswith('.v1') and
                 kind[3:-3] in ROOTS, 'ROOT', 2)
        _require(conforms(value, defs[kind[3:-3]], defs), 'SCHEMA', 2)
    _lineage(related)
    _graph(records, ctx)
    for value, raw, file in records:
        _artifact(value, raw, file, defs, ctx, related)
    referenced = set()
    for value, _, source in records:
        base = _artifact_root(value, source, ctx)
        for obj in _objects(value):
            for key in ('source_ref', 'anchor', 'behaviour_anchor', 'script_ref', 'steps_file',
                        'public_evidence_refs', 'evidence_refs', 'receipt_refs', 'support_receipt_refs',
                        'authorization_receipt_ref', 'file'):
                refs = obj.get(key)
                if type(refs) is str:
                    refs = [refs]
                if type(refs) is list:
                    for ref in refs:
                        reference_base = source.parent if value['schema'] == 'mw.demo.v1' and key in ('file', 'steps_file') else base
                        target = _reference(reference_base, ref.split('#')[0])
                        _read(target, 32768 if target.suffix in ('.py', '.sh') else MAX_FILE, owner=base)
                        referenced.add(target)
    for file in auxiliary:
        _require(file in referenced, 'ROOT', 2)
        _public_document(_read(file, MAX_FILE, owner=path), defs)
    for file in files:
        if file.suffix not in ('.json', '.jsonl'):
            _require(file in referenced, 'UNCLASSIFIED', 2)
            _read(file, 32768 if file.suffix != '.md' else MAX_FILE, owner=path)


# @anchor:wa.tools.memoryweb_gate.translate_native
def translate_native(component: str, operation: str, code: int, reason: str, *,
                     observed_human_boundary: bool = False,
                     retries_exhausted: bool = False) -> NativeDisposition:
    rc, disposition = 5, 'stop'
    if observed_human_boundary is True:
        rc, disposition = 4, 'handoff'
    elif type(code) is int:
        if component == 'vision-engine' and operation == 'observe':
            expected = {0: 'OK', 2: 'usage', 3: 'reject', 4: 'stale', 5: 'gate', 6: 'budget'}
            if expected.get(code) == reason:
                rc = {0: None, 2: 2, 3: 5, 4: 3, 5: 5, 6: 5}[code]
                disposition = 'observe' if code == 0 else 'stop'
        elif component == 'wender-agent' and operation == 'reseal-check' and code == 4 and reason == 'stale':
            rc = 3
        elif component == 'producer' and operation == 'read' and code in (4, 6) and reason in ('render', 'navigation'):
            if not retries_exhausted:
                rc, disposition = None, 'retry_read'
    return NativeDisposition(disposition, rc, component, operation, code, reason)


# @anchor:wa.tools.memoryweb_gate.main
def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    result = GateResult(2, 'USAGE')
    try:
        if args == ['entry-check']:
            result = entry_check(DOCS / 'MEMORYWEB_ENTRY.md', DOCS / 'MEMORYWEB_PROTOCOL.md')
        elif len(args) == 2 and args[0] == 'check-explanation':
            result = check_explanation(Path(args[1]))
        elif len(args) == 2 and args[0] == 'validate':
            raw = _read(DOCS / 'mw.v1.schema.json', 1048576)
            _require(hashlib.sha256(raw).hexdigest() == SCHEMA_PIN, 'SCHEMA_PIN', 3)
            bundle = strict_load(raw)
            path = Path(args[1])
            result = (validate_directory if path.is_dir() else validate_file)(path, bundle)
    except _Refusal as exc:
        result = GateResult(exc.rc, exc.category)
    except (OSError, ValueError, TypeError, RecursionError, MemoryError):
        result = GateResult(5, 'DEPENDENCY')
    print(canon({'rc': result.rc, 'category': result.category}))
    return result.rc


if __name__ == '__main__':
    sys.exit(main())
