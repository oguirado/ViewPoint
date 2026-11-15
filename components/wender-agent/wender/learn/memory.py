# @module {"id":"wa.learn.memory","layer":2,"owner":"LEARN","deps":["wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Versioned, bounded executor memory. Observations remain in caller-owned RAM."""
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Protocol as _Protocol
from wender.scripts import types as _context_types

SUPPORTED_SCHEMAS = frozenset(('wender.executor_memory/v1', 'wender.executor_memory/v2'))
MAX_LESSONS = 3  # [TARGET]
MAX_LESSON_CHARS = 400  # [TARGET]
MAX_LESSONS_TOKENS = 120  # [TARGET]
MAX_PREFIX_TOKENS = 2000  # [TARGET]
MIN_TRUSTED_REPRODUCTIONS = 2  # [TARGET]
DEFAULT_HORIZON_RUNS = 200  # [TARGET]
ALLOWED_REGIONS = frozenset(('navigation', 'sidebar', 'main', 'composer', 'menu', 'dialog', 'header', 'nav', 'footer'))
ALLOWED_CONTAINER_RULES = frozenset(('first', 'last', 'any', 'model submenu', 'LAST answer container, after last Investigado'))
NEGATED_INVARIANT_RE = re.compile(r'^(?:Never|Do not|No|Require|Abstain when|Stop on)\b')
FORBIDDEN_MEMORY_KEYS = frozenset(('task', 'query', 'raw_query', 'question', 'answer', 'harvested', 'harvested_answer', 'ax', 'elements', 'cookies', 'token', 'url', 'title', 'prompt'))
RAW_LEAK_PATTERNS = re.compile(r'(?:\barxiv\+\d+\b|GPT-6\.1\s+Sol|\bAnswer with\b|\bQuestion\b|\bPRIVATE HISTORY\b|https?://)', re.I)
_BLOCKED = re.compile(r'\b(?:upgrade|mejorar|plan|billing|facturaci[oó]n|try pro|subscribe|suscribirse|account|cuenta|settings|ajustes|configuraci[oó]n|logout|log out|cerrar sesi[oó]n|captcha)\b', re.I)
_ADMISSION = ('verified_reproductions', 'alignment_violations', 'last_verified_epoch')


# @anchor:wa.learn.memory.MemoryRejected
class MemoryRejected(ValueError):
    """Schema, privacy, digest, budget or admission refusal."""


def _require_exact_int(value, name, *, min_value=0):
    if type(value) is not int or value < min_value:
        raise MemoryRejected(f'{name} must be an exact int >= {min_value}')
    return value


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise MemoryRejected('duplicate JSON key')
        out[key] = value
    return out


def _reject_constant(value):
    raise MemoryRejected('non-finite JSON constant')


def _leaks(text, probes):
    for probe in probes:
        if isinstance(probe, str) and len(probe.strip()) >= 4:
            probe = probe.strip().lower()
            if probe in text.lower() or any(probe[i:i + 12] in text.lower() for i in range(len(probe) - 11)):
                return True
    return False


def _scan_no_raw_payload(obj, *, forbidden_substrings=(), path='root'):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str) or key.lower() in FORBIDDEN_MEMORY_KEYS:
                raise MemoryRejected('forbidden raw payload key in memory')
            if RAW_LEAK_PATTERNS.search(key) or _leaks(key, forbidden_substrings):
                raise MemoryRejected('raw payload in memory key')
            _scan_no_raw_payload(value, forbidden_substrings=forbidden_substrings, path=f'{path}.{key}')
    elif isinstance(obj, list):
        for value in obj:
            _scan_no_raw_payload(value, forbidden_substrings=forbidden_substrings, path=path + '[]')
    elif isinstance(obj, str):
        # This fixed v1 workflow description is protocol prose, not task text.
        protocol_expected = path.endswith('.expected') and obj == 'composer contains question'
        if '\r' in obj or (RAW_LEAK_PATTERNS.search(obj) and not protocol_expected) or _leaks(obj, forbidden_substrings):
            raise MemoryRejected('raw payload in memory')
    elif obj is not None and type(obj) not in (int, bool):
        raise MemoryRejected('memory requires integer JSON wire values')


# @anchor:wa.learn.memory.control_map_digest
def control_map_digest(control_map: dict) -> str:
    if not isinstance(control_map, dict) or not control_map:
        raise MemoryRejected('control_map must be nonempty')
    return hashlib.sha256(_dump(control_map).encode('utf-8')).hexdigest()


# @anchor:wa.learn.memory.estimate_tokens
def estimate_tokens(text: str) -> int:
    """Deterministic budget proxy; not a tokenizer or a BPE upper bound."""
    if type(text) is not str:
        raise MemoryRejected('token estimate requires str')
    return max((len(text) + 3) // 4, len(text.split()))


# @anchor:wa.learn.memory.validate_lessons
def validate_lessons(lessons, *, forbidden_substrings=()) -> list[str]:
    if not isinstance(lessons, list) or len(lessons) > MAX_LESSONS:
        raise MemoryRejected('lessons count exceeds max 3')
    for lesson in lessons:
        if (type(lesson) is not str or not lesson.strip() or '\n' in lesson or '\r' in lesson
                or len(lesson) > MAX_LESSON_CHARS or not NEGATED_INVARIANT_RE.match(lesson)
                or RAW_LEAK_PATTERNS.search(lesson) or _leaks(lesson, forbidden_substrings)):
            raise MemoryRejected('invalid or private lesson')
    if sum(estimate_tokens(s) for s in lessons) > MAX_LESSONS_TOKENS:
        raise MemoryRejected('lessons exceed 120 token budget')
    return list(lessons)


# @anchor:wa.learn.memory.validate_control_map
def validate_control_map(control_map: dict) -> dict:
    if not isinstance(control_map, dict) or not control_map:
        raise MemoryRejected('control_map must be nonempty')
    for spec in control_map.values():
        if not isinstance(spec, dict) or not isinstance(spec.get('role'), str) or not spec['role']:
            raise MemoryRejected('invalid control role')
        rule = spec.get('container_rule')
        if not isinstance(rule, str) or not (rule in ALLOWED_CONTAINER_RULES or (rule.startswith('same_as:') and rule[8:])):
            raise MemoryRejected('invalid container rule')
        region = spec.get('region')
        if region is not None and (not isinstance(region, str) or not (region in ALLOWED_REGIONS or re.fullmatch(r'answer:(?:[0-9]+|last)', region))):
            raise MemoryRejected('invalid control region')
        if 'name_match' in spec:
            match = spec['name_match']
            if not isinstance(match, dict) or match.get('kind') not in ('any', 'exact', 'prefix', 'suffix') or type(match.get('text')) is not str:
                raise MemoryRejected('invalid name_match')
        if 'pattern' in spec:
            try:
                re.compile(spec['pattern'])
            except (TypeError, re.error):
                raise MemoryRejected('invalid control pattern') from None
        if 'bbox_norm' in spec:
            box = spec['bbox_norm']
            if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int or not 0 <= v <= 1_000_000 for v in box):
                raise MemoryRejected('invalid bbox_norm')
        if 'structural_fallback' in spec and type(spec['structural_fallback']) is not bool:
            raise MemoryRejected('invalid structural_fallback')
    return copy.deepcopy(control_map)


# @anchor:wa.learn.memory.resolve_control_candidates
def resolve_control_candidates(control: dict, options: list[dict], targets: dict[str, dict]) -> set[str]:
    validate_control_map({'control': control})
    option_ids = {o['id'] for o in options if isinstance(o, dict) and isinstance(o.get('id'), str)}
    rows = [(key, e) for key, e in targets.items() if key in option_ids
            and e.get('role') == control['role'] and ('region' not in control or e.get('region') == control['region'])
            and not _BLOCKED.search(str(e.get('name', ''))) and e.get('visible', True) is True
            and e.get('actionable', True) is True and e.get('enabled', True) is True
            and e.get('disabled', False) is False and (e.get('states') or {}).get('enabled', True) is not False
            and (e.get('states') or {}).get('disabled', False) is not True
            and (e.get('states') or {}).get('hidden', False) is not True]
    def named(e):
        name = e.get('name', '')
        match = control.get('name_match')
        if match:
            kind, text = match['kind'], match['text']
            return kind == 'any' or (kind == 'exact' and name == text) or (kind == 'prefix' and name.startswith(text)) or (kind == 'suffix' and name.endswith(text))
        return re.search(control.get('pattern', '.*'), name, re.I) is not None
    matched = [(key, e) for key, e in rows if named(e)]
    rule = control['container_rule']
    if not matched:
        if control.get('structural_fallback', True) is not True or 'region' not in control or rule not in ('first', 'last', 'any'):
            return set()
        matched = rows
    if rule not in ('first', 'last', 'any'):
        # Rich container rules need positive ownership evidence, never a default.
        if rule.startswith('same_as:'):
            reference = targets.get(rule[8:])
            if not reference or not reference.get('container'):
                return set()
            matched = [(k, e) for k, e in matched if e.get('container') == reference['container']]
        else:
            matched = [(k, e) for k, e in matched if e.get('container_rule') == rule]
    if control.get('bbox_norm') and matched:
        distances = [(sum(abs(a - b) for a, b in zip(control['bbox_norm'], e['bbox_norm'])), k, e)
                     for k, e in matched if isinstance(e.get('bbox_norm'), list) and len(e['bbox_norm']) == 4
                     and all(type(v) is int for v in e['bbox_norm'])]
        if not distances:
            return set()
        best = min(d[0] for d in distances)
        matched = [(k, e) for distance, k, e in distances if distance == best]
    if rule == 'first':
        matched = matched[:1]
    elif rule == 'last':
        matched = matched[-1:]
    return {key for key, _ in matched}


# @anchor:wa.learn.memory.is_trusted
def is_trusted(meta: dict) -> bool:
    if not isinstance(meta, dict):
        return False
    try:
        repro, violations, epoch = (_require_exact_int(meta.get(k), k) for k in _ADMISSION)
    except MemoryRejected:
        return False
    return repro >= MIN_TRUSTED_REPRODUCTIONS and violations == 0 and epoch > 0


# @anchor:wa.learn.memory.validate_memory
def validate_memory(memory: dict, *, cycle_version: int = 1, forbidden_substrings=()) -> dict:
    if not isinstance(memory, dict) or memory.get('schema') not in SUPPORTED_SCHEMAS:
        raise MemoryRejected('unsupported memory schema')
    _require_exact_int(cycle_version, 'cycle_version', min_value=1)
    _scan_no_raw_payload(memory, forbidden_substrings=forbidden_substrings)
    out = copy.deepcopy(memory)
    if type(out.get('site')) is not str or not out['site']:
        raise MemoryRejected('invalid site')
    validate_control_map(out.get('control_map'))
    wf = out.get('workflow')
    if (not isinstance(wf, dict) or wf.get('kind') != 'workflow' or not isinstance(wf.get('id'), str)
            or not wf['id'] or not isinstance(wf.get('steps'), list) or not wf['steps']):
        raise MemoryRejected('invalid workflow')
    for step in wf['steps']:
        if not isinstance(step, dict) or type(step.get('intent')) is not str or not step['intent']:
            raise MemoryRejected('invalid workflow intent')
    validate_lessons(out.get('lessons'), forbidden_substrings=forbidden_substrings)
    v2 = out['schema'].endswith('/v2') or 'control_map_digest' in out
    horizon = _require_exact_int(out.get('horizon_runs', DEFAULT_HORIZON_RUNS), 'horizon_runs', min_value=1)
    if v2:
        if _require_exact_int(out.get('schema_version'), 'schema_version', min_value=1) != 2:
            raise MemoryRejected('invalid schema_version')
        if cycle_version < _require_exact_int(out.get('min_cycle_version'), 'min_cycle_version', min_value=1):
            raise MemoryRejected('cycle version too old')
        if any(not spec.get('region') for spec in out['control_map'].values()):
            raise MemoryRejected('v2 controls require region')
        if out.get('control_map_digest') != control_map_digest(out['control_map']):
            raise MemoryRejected('control_map_digest mismatch')
    for key in _ADMISSION:
        value = _require_exact_int(wf.get(key, out.get(key, 0)), key)
        if key in out and _require_exact_int(out[key], key) != value:
            raise MemoryRejected('inconsistent admission metadata')
        out[key] = wf[key] = value
    if out['verified_reproductions'] > horizon:
        raise MemoryRejected('reproduction horizon exceeded')
    for meta in (out, wf):
        if meta.get('status') not in ('draft', 'trusted'):
            raise MemoryRejected('invalid status')
        if meta['status'] == 'trusted' and not is_trusted(meta):
            raise MemoryRejected('workflow marked trusted without satisfying admission gate')
    wire = _dump(out)
    if max(estimate_tokens(wire), (len(wire.encode('utf-8')) + 3) // 4) > MAX_PREFIX_TOKENS:
        raise MemoryRejected('memory prefix exceeds budget')
    return out


# @anchor:wa.learn.memory.load_memory
def load_memory(path, *, cycle_version: int = 1, forbidden_substrings=()) -> dict:
    try:
        with Path(path).open(encoding='utf-8', newline='') as handle:
            text = handle.read()
        if '\r' in text:
            raise MemoryRejected('carriage return in memory')
        return validate_memory(json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant),
                               cycle_version=cycle_version, forbidden_substrings=forbidden_substrings)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MemoryRejected('memory load refused') from exc


# @anchor:wa.learn.memory.record_reproduction
def record_reproduction(memory: dict, summary: dict, *, epoch: int = 1, violations: int | None = None) -> dict:
    out = validate_memory(memory)
    if not isinstance(summary, dict):
        return out
    probes = tuple(summary.get(k) for k in ('task', 'query', 'harvested', 'answer'))
    _scan_no_raw_payload(out, forbidden_substrings=probes)
    _require_exact_int(epoch, 'epoch')
    cycle_violations = _require_exact_int(summary.get('alignment_violations', 0) if violations is None else violations, 'alignment_violations')
    verified = summary.get('goal_verified') is True
    completed = summary.get('completed', [])
    canonical = {s['intent'] for s in out['workflow']['steps']}
    if verified and (not isinstance(completed, list) or any(type(s) is not str or s not in canonical for s in completed)):
        raise MemoryRejected('noncanonical completed intents')
    if not verified and not cycle_violations:
        return out
    for meta in (out, out['workflow']):
        meta['alignment_violations'] += cycle_violations
        if verified:
            meta['verified_reproductions'] += 1
            meta['last_verified_epoch'] = epoch
            meta['last_verified_intents'] = list(completed)
        meta['status'] = 'trusted' if is_trusted(meta) else 'draft'
    return validate_memory(out, forbidden_substrings=probes)


# @anchor:wa.learn.memory.build_planner_memory_view
def build_planner_memory_view(memory: dict) -> dict:
    out = validate_memory(memory)
    # Admission telemetry never enters the cached planner prefix.
    for meta in (out, out['workflow']):
        for key in (*_ADMISSION, 'last_verified_intents', 'status'):
            meta.pop(key, None)
    return out


# @anchor:wa.learn.memory.canonical_dump
def canonical_dump(memory: dict) -> str:
    return _dump(validate_memory(memory))


# @anchor:wa.learn.memory.ContextTokenizer
class ContextTokenizer(_Protocol):
    """Configured provider tokenizer, including the complete supplied wrapper."""
    identity: str

    # @anchor:wa.learn.memory.ContextTokenizer.count
    def count(self, payload: bytes) -> int:
        ...


# @anchor:wa.learn.memory.ContextExportAdapter
class ContextExportAdapter(_Protocol):
    """Host adapter authenticates sealed receipts; payloads are always passive.

    Approval covers every key/value/region of these exact derived bytes for the
    supplied scope, destination, purpose and transformation chain. Implementors
    must validate retained ExportReceiptPort custody, not match copied fields.
    """

    purpose: _context_types.Purpose

    # @anchor:wa.learn.memory.ContextExportAdapter.approve
    def approve(self, payload: bytes, *, scope: _context_types.ScopeIdentity,
                destination: _context_types.ExportDestination,
                purpose: _context_types.Purpose,
                transform_chain: tuple[str, ...]) -> _context_types.AdmissionCheck:
        ...


def _context_approve(adapter, payload, scope, chain):
    check = adapter.approve(payload, scope=scope,
        destination=_context_types.ExportDestination.FROZEN_PREFIX,
        purpose=adapter.purpose, transform_chain=chain)
    if (type(check) is not _context_types.AdmissionCheck or
            check.result is not _context_types.Truth.TRUE):
        raise MemoryRejected('PRIVACY')


def _static_context_projection(view):
    # Each object position has its own closed schema. Dynamic control names are
    # retained only under control_map and subsequently require export approval.
    schemas = {
        'root': {'schema', 'site', 'source', 'control_map', 'workflow', 'lessons',
                 'schema_version', 'min_cycle_version', 'locale', 'horizon_runs',
                 'control_map_digest'},
        'workflow': {'kind', 'id', 'trigger_sig', 'steps', 'anchor_signatures'},
        'trigger_sig': {'site', 'intent'},
        'control': {'role', 'pattern', 'region', 'container_rule', 'name_match',
                    'structural_fallback'},
        'name_match': {'kind', 'text'},
        'step': {'intent', 'expected', 'action', 'postcheck'},
        'action': {'kind', 'control', 'keysym'},
        'postcheck': {'kind', 'role', 'region', 'pattern', 'rules'},
        'anchor': {'role', 'landmark', 'required'},
    }

    def visit(value, position):
        if isinstance(value, dict):
            if position != 'control_map' and position not in schemas:
                raise MemoryRejected('SCHEMA')
            if position != 'control_map' and set(value) - schemas[position]:
                raise MemoryRejected('SCHEMA')
            for key, child in value.items():
                next_position = ('control' if position == 'control_map' else {
                    'control_map': 'control_map', 'workflow': 'workflow',
                    'trigger_sig': 'trigger_sig', 'steps': 'step',
                    'action': 'action', 'postcheck': 'postcheck',
                    'rules': 'postcheck', 'name_match': 'name_match',
                    'anchor_signatures': 'anchor',
                }.get(key))
                if isinstance(child, (dict, list)):
                    visit(child, next_position)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    if position is None:
                        raise MemoryRejected('SCHEMA')
                    visit(child, position)

    visit(view, 'root')
    # Legacy digests are not exported: approval applies to structural content,
    # never to a reusable digest derived from potentially private legacy prose.
    view.pop('control_map_digest', None)
    return view


# @anchor:wa.learn.memory.freeze_context_memory
def freeze_context_memory(memory, *, scope, cycle_version,
                          forbidden_substrings, tokenizer,
                          max_tokens, export_adapter):
    """Return approved immutable, LF-terminated, wrapped passive JSON bytes.

    Caller must reserve validation/copy/tokenization peak before entry. Legacy
    telemetry is removed by the existing planner projection; unexpected dynamic
    state and unknown nested fields refuse rather than disappearing silently.
    """
    reason = None
    try:
        if (type(scope) is not _context_types.ScopeIdentity or
                scope.attestation is None or scope.attestation_reason is not None):
            raise MemoryRejected('FOREIGN')
        if (type(max_tokens) is not int or max_tokens <= 0 or
                type(tokenizer.identity) is not str or not tokenizer.identity):
            raise MemoryRejected('BOUNDS')
        validated = validate_memory(memory, cycle_version=cycle_version,
                                    forbidden_substrings=forbidden_substrings)
        view = _static_context_projection(build_planner_memory_view(validated))
        # Approve individual strings (including dynamic keys), then composition.
        def approve_strings(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    approve_strings(key)
                    approve_strings(child)
            elif isinstance(value, list):
                for child in value:
                    approve_strings(child)
            elif type(value) is str:
                _context_approve(export_adapter, value.encode('utf-8'), scope,
                                 ('memory-static-string',))
        approve_strings(view)
        payload = (_dump({'role': 'passive', 'memory': view}) + '\n').encode('utf-8')
        _context_approve(export_adapter, payload, scope, ('memory-static-json-lf',))
        count = tokenizer.count(payload)
        if (type(count) is not int or count <= 0 or
                tokenizer.count(payload) != count or count > max_tokens):
            raise MemoryRejected('BOUNDS')
        return payload
    except MemoryRejected as exc:
        privacy_refusals = {'forbidden raw payload key in memory',
                            'raw payload in memory key', 'raw payload in memory',
                            'invalid or private lesson'}
        reason = ('PRIVACY' if str(exc) in privacy_refusals else
                  str(exc) if str(exc) in {'FOREIGN', 'BOUNDS', 'PRIVACY', 'SCHEMA'} else 'SCHEMA')
    except Exception:
        reason = 'UNKNOWN'
    # Do not retain original exceptions, canaries, adapter diagnostics or memory.
    raise MemoryRejected(reason)
