# @module {"id":"wa.perceive.redact","layer":2,"owner":"PERCEIVE","deps":["wa.scripts.types"],"book":"HB-WA-s1-perceive","summary":"Receipt-backed public projections and region masking."}
"""S4 privacy boundary. Local records cover the spec ABI absent from S0.

Receipts are integrity records, not cryptographic host attestations. Classification
must be supplied by the trusted producer; passive page data cannot grant it.
"""
from dataclasses import dataclass as _dataclass, fields as _fields, is_dataclass as _is_dataclass
from enum import Enum as _Enum
from typing import Generic as _Generic, TypeVar as _TypeVar
import hashlib as _hashlib
import json as _json
import re as _re
import unicodedata as _ud
from urllib.parse import urlsplit as _urlsplit


class _ScopeAuthorityError(ValueError):
    pass


class _PrivacyRedactionError(ValueError):
    pass


def _immutable(value):
    if type(value) in (str, bytes, int, float, bool, type(None)) or isinstance(value, _Enum):
        return
    if type(value) in (tuple, frozenset):
        for item in value:
            _immutable(item)
        return
    if _is_dataclass(value) and value.__dataclass_params__.frozen:
        for field in _fields(value):
            _immutable(getattr(value, field.name))
        return
    raise TypeError('record requires recursively immutable values')


class _Record:
    def __post_init__(self):
        for field in _fields(self):
            _immutable(getattr(self, field.name))


class _FactStatus(str, _Enum):
    KNOWN = 'KNOWN'
    UNKNOWN = 'UNKNOWN'
    UNAVAILABLE = 'UNAVAILABLE'


_T = _TypeVar('_T')


@_dataclass(frozen=True)
class _FactValue(_Record, _Generic[_T]):
    status: _FactStatus
    value: object = None
    reason: str | None = None

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.status, _FactStatus):
            raise ValueError('invalid fact status')
        if self.status == _FactStatus.KNOWN:
            if self.value is None:
                raise ValueError('known fact requires value')
        elif self.value is not None or not isinstance(self.reason, str) or not self.reason:
            raise ValueError('unknown fact requires reason and no value')

    def __bool__(self):
        raise TypeError('FactValue has no implicit truthiness; check .is_known() and .value explicitly')

    def is_known(self):
        return self.status == _FactStatus.KNOWN


@_dataclass(frozen=True)
class _ScopeIdentity(_Record):
    principal_id: str
    tenant_id: str
    profile_incarnation: str
    top_level_origin: str
    target_frame_origin: str
    frame_ancestry: tuple[str, ...]
    scope_revision: int
    host_attested: bool = True

    def __post_init__(self):
        super().__post_init__()
        _validate_scope_identity(self)


@_dataclass(frozen=True)
class _DeclassificationReceipt(_Record):
    receipt_id: str
    source_observation_id: str
    scope_identity: _ScopeIdentity
    field_or_region: str
    transform_chain: tuple[str, ...]
    content_sha256: str
    destination: str
    purpose: str
    policy_revision: int


@_dataclass(frozen=True)
class _ObservationRef(_Record):
    observation_id: str
    scope_identity: _ScopeIdentity
    producer_gen16: int
    srf_64: int
    content_hash: str
    scope_complete: bool
    turn_index: int

    def __post_init__(self):
        super().__post_init__()
        _validate_scope_identity(self.scope_identity)
        if (not self.observation_id or type(self.producer_gen16) is not int
                or not 0 <= self.producer_gen16 <= 65535 or type(self.srf_64) is not int
                or not 0 <= self.srf_64 < 2**64 or type(self.turn_index) is not int
                or self.turn_index < 0 or type(self.scope_complete) is not bool
                or not _re.fullmatch('[0-9a-f]{64}', self.content_hash)):
            raise ValueError('invalid observation reference')


@_dataclass(frozen=True)
class _TargetFacts(_Record):
    element_id: int
    role_code: _FactValue
    role_name: _FactValue
    visible: _FactValue
    enabled: _FactValue
    actionable: _FactValue
    focused: _FactValue
    value_nonempty: _FactValue
    bbox: _FactValue
    click_point: _FactValue
    mark_id: _FactValue
    backend_node_id: _FactValue
    match_level: _FactValue
    gen16: _FactValue
    landmark_role: _FactValue
    container_key: _FactValue
    public_label: _FactValue
    opaque_label_ref: str | None
    declassification_receipt: _DeclassificationReceipt | None


@_dataclass(frozen=True)
class _ExecutionWitness(_Record):
    observation_id: str
    scope_identity: _ScopeIdentity
    element_id: int
    backend_node_id: _FactValue
    mark_id: _FactValue
    gen16: int
    ax_state_word: int
    bbox: tuple[int, int, int, int]
    click_point: tuple[int, int]
    coord_space: str
    transform_epoch: int
    join_certified: bool
    match_level: str


@_dataclass(frozen=True)
class _TargetBinding(_Record):
    bound: bool
    status: str
    reason_code: str
    observation_ref: _ObservationRef
    target_facts: _TargetFacts | None
    execution_witness: _ExecutionWitness | None
    candidate_ids: tuple[int, ...]
    association_verified: bool


@_dataclass(frozen=True)
class _ImageRef(_Record):
    image_id: str
    source_observation_id: str
    parent_publication_id: str
    scope_identity: _ScopeIdentity
    variant: str
    width: int
    height: int
    approved_regions: tuple
    masked_regions: tuple
    pixel_sha256: str
    encoded_bytes: bytes
    receipt: _DeclassificationReceipt


@_dataclass(frozen=True)
class _CascadeView(_Record):
    observation_ref: _ObservationRef
    scope_identity: _ScopeIdentity
    structural_tokens: tuple[str, ...]
    role_counts: tuple
    actionable_ids: tuple[int, ...]
    public_landmarks: tuple[str, ...]
    opaque_refs: tuple[str, ...]
    serialized_bytes: int
    receipts: tuple


@_dataclass(frozen=True)
class _ImageNeedResult(_Record):
    image_needed: bool
    reason_code: str
    allowed_variants: tuple[str, ...]
    required_target_bboxes: tuple


@_dataclass(frozen=True)
class _ObservationBundle(_Record):
    observation_ref: _ObservationRef
    scope_identity: _ScopeIdentity
    publication_id: str
    turn_ownership_verified: bool
    answer_complete: _FactValue
    target_facts_table: tuple
    execution_witnesses: tuple
    cascade_view: _CascadeView
    receipts: tuple
    serialized_bytes: int


# Import the complete shared ABI atomically; never mix incompatible record families.
try:
    from wender.scripts.types import (FactStatus, FactValue, ScopeIdentity,
        DeclassificationReceipt, ObservationRef, TargetFacts, ExecutionWitness,
        TargetBinding, ImageRef, CascadeView, ImageNeedResult, ObservationBundle)
except ImportError:
    FactStatus, FactValue, ScopeIdentity = _FactStatus, _FactValue, _ScopeIdentity
    DeclassificationReceipt, ObservationRef = _DeclassificationReceipt, _ObservationRef
    TargetFacts, ExecutionWitness, TargetBinding = _TargetFacts, _ExecutionWitness, _TargetBinding
    ImageRef, CascadeView = _ImageRef, _CascadeView
    ImageNeedResult, ObservationBundle = _ImageNeedResult, _ObservationBundle


def _validate_scope_identity(scope):
    if not isinstance(scope, _ScopeIdentity) and not isinstance(scope, globals().get('ScopeIdentity', _ScopeIdentity)):
        raise _ScopeAuthorityError('host-attested scope required')
    if (scope.host_attested is not True or type(scope.scope_revision) is not int
            or scope.scope_revision < 0 or not all(type(v) is str and v.strip() for v in
            (scope.principal_id, scope.tenant_id, scope.profile_incarnation))):
        raise _ScopeAuthorityError('unverified scope')
    origins = (scope.top_level_origin, scope.target_frame_origin) + scope.frame_ancestry
    if (not scope.frame_ancestry or scope.frame_ancestry[0] != scope.top_level_origin
            or scope.frame_ancestry[-1] != scope.target_frame_origin):
        raise _ScopeAuthorityError('inconsistent frame ancestry')
    for origin in origins:
        if not isinstance(origin, str):
            raise _ScopeAuthorityError('invalid origin')
        parsed = _urlsplit(origin)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query
                or parsed.fragment or origin != origin.lower() or '?' in origin or '#' in origin):
            raise _ScopeAuthorityError('canonical origin required')
        try:
            port = parsed.port
        except ValueError as exc:
            raise _ScopeAuthorityError('invalid port') from exc
        if port == {'http': 80, 'https': 443}[parsed.scheme]:
            raise _ScopeAuthorityError('noncanonical default port')


def _reserve(port, size, stage):
    if port is not None and port.reserve(size, stage=stage) is False:
        raise _PrivacyRedactionError('BUDGET_RESERVATION_REJECTED')


def _normalize_pinned_utf8_nfc_casefold(text):
    text = text.encode('utf-8', 'strict').decode('utf-8', 'strict')
    text = _ud.normalize('NFC', _ud.normalize('NFC', text).casefold())
    # Preserve whitespace boundaries while stripping other ASCII controls.
    text = ''.join(' ' if c in '\t\n\r\v\f' else c for c in text)
    text = _re.sub('[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', '', text)
    return ' '.join(text.split())


_SECRET_OR_INJECTION_PATTERNS = _re.compile(
    r'sk-|ghp_|AIza|eyJ|bearer\s|[\w.+-]+@[\w.-]+\.[a-z]{2,}|'
    r'\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]?){13,19}\b|https?://|'
    r'token=|code=|session=|cookie|/home/|[a-z]:\\|'
    r'ignore\s+previous\s+instructions|<untrusted_content>|system\s+prompt:', _re.I)
_TRANSFORMS = ('utf8_strict', 'nfc', 'casefold', 'nfc', 'ws_collapse', 'sanitize_text')
_DESTINATIONS = frozenset(('cascade', 'planner', 'audit', 'publication'))
_PURPOSES = frozenset(('script_match', 'step_binding', 'escalation_image', 'audit_summary', 'harvest_selector'))


def _digest(data):
    return _hashlib.sha256(data).hexdigest()


def _receipt(data, scope, observation, field, transforms, destination, purpose, revision):
    if (not observation or not field or destination not in _DESTINATIONS
            or purpose not in _PURPOSES or type(revision) is not int or revision < 1):
        raise _PrivacyRedactionError('invalid receipt context')
    digest = _digest(data)
    return DeclassificationReceipt(digest[:16], observation, scope, field, transforms,
                                   digest, destination, purpose, revision)


# @anchor:wa.perceive.redact.sanitize_text
def sanitize_text(raw_text, *, scope_identity, source_observation_id, field_name,
                  classification='unknown', destination='cascade', purpose='script_match',
                  policy_revision=1, budget_port=None):
    _validate_scope_identity(scope_identity)
    if raw_text is None:
        return FactValue(FactStatus.UNKNOWN, reason='MISSING_TEXT'), None, None
    if type(raw_text) is not str:
        raise TypeError('text required')
    if len(raw_text) > 4096:
        return FactValue(FactStatus.UNAVAILABLE, reason='BUDGET_PREALLOC_EXCEEDED'), None, None
    # Reserve worst-case UTF-8 bytes before even encoding the input.
    _reserve(budget_port, len(raw_text) * 4, 'redact.sanitize_text')
    try:
        data = raw_text.encode('utf-8', 'strict')
    except UnicodeError:
        return FactValue(FactStatus.UNAVAILABLE, reason='INVALID_UTF8'), None, None
    if len(data) > 4096:
        return FactValue(FactStatus.UNAVAILABLE, reason='BUDGET_PREALLOC_EXCEEDED'), None, None
    text = _normalize_pinned_utf8_nfc_casefold(raw_text)
    opaque = 'opaque_ref:' + purpose + ':' + _digest(data)[:16]
    if _SECRET_OR_INJECTION_PATTERNS.search(text):
        return FactValue(FactStatus.UNAVAILABLE, reason='REDACTED_SENSITIVE_OR_INJECTION'), opaque, None
    if classification not in ('public_ui_constant', 'schema_enum', 'declassified_public'):
        return FactValue(FactStatus.UNKNOWN, reason='NON_PUBLIC_CLASS:' + classification), opaque, None
    if not text or len(text.encode('utf-8')) > 128:
        return FactValue(FactStatus.UNKNOWN, reason='PUBLIC_LABEL_BOUND'), opaque, None
    receipt = _receipt(text.encode(), scope_identity, source_observation_id, field_name,
                       _TRANSFORMS, destination, purpose, policy_revision)
    return FactValue(FactStatus.KNOWN, text), None, receipt


def _plain(value):
    if isinstance(value, _Enum):
        return value.value
    if _is_dataclass(value):
        # Positional closed-schema encoding avoids repeating field names.
        return [_plain(getattr(value, f.name)) for f in _fields(value)]
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return value.hex()
    return value


def _encoded(value):
    # Intern repeated scope records within a projection; no authority fields are dropped.
    scopes = []
    def encode(item):
        if isinstance(item, ScopeIdentity):
            if item not in scopes:
                scopes.append(item)
            return ['scope', scopes.index(item)]
        if isinstance(item, _Enum):
            return item.value
        if _is_dataclass(item):
            return [encode(getattr(item, f.name)) for f in _fields(item)]
        if isinstance(item, (tuple, list)):
            return [encode(v) for v in item]
        if isinstance(item, dict):
            return {k: encode(v) for k, v in item.items()}
        if isinstance(item, bytes):
            return item.hex()
        return item
    data = encode(value)
    envelope = {'scopes': [_plain(s) for s in scopes], 'payload': data}
    return _json.dumps(envelope, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _receipt_valid(receipt, scope):
    return (isinstance(receipt, DeclassificationReceipt) and receipt.scope_identity == scope
            and type(receipt.policy_revision) is int and receipt.policy_revision >= 1
            and receipt.destination in _DESTINATIONS and receipt.purpose in _PURPOSES
            and bool(receipt.source_observation_id) and bool(receipt.field_or_region)
            and bool(receipt.transform_chain)
            and _re.fullmatch('[0-9a-f]{64}', receipt.content_sha256))


# @anchor:wa.perceive.redact.validate_public_payload
def validate_public_payload(payload, *, receipts, expected_scope, max_bytes=16384):
    _validate_scope_identity(expected_scope)
    if any(not _receipt_valid(r, expected_scope) for r in receipts):
        raise _PrivacyRedactionError('INVALID_RECEIPT')

    def walk(value, path=''):
        if isinstance(value, ScopeIdentity):
            if value != expected_scope:
                raise _ScopeAuthorityError('SCOPE_MISMATCH')
            return
        if isinstance(value, str):
            if _re.fullmatch(r'opaque_ref:[a-z_]+:[0-9a-f]{16}', value):
                return
            if (_SECRET_OR_INJECTION_PATTERNS.search(value) or not any(
                    r.content_sha256 == _digest(value.encode()) and r.field_or_region == path
                    for r in receipts)):
                raise _PrivacyRedactionError('UNRECEIPTED_OR_SENSITIVE_TEXT')
            return
        if type(value) in (int, bool, type(None)):
            return
        if isinstance(value, dict):
            if set(value) - {'labels', 'roles', 'landmarks', 'opaque_refs', 'actionable_ids'}:
                raise _PrivacyRedactionError('RAW_OR_UNKNOWN_FIELDS')
            for key, child in value.items():
                walk(child, key)
            return
        if isinstance(value, (tuple, list)):
            for i, child in enumerate(value):
                walk(child, f'{path}:{i}')
            return
        raise _PrivacyRedactionError('RAW_RECORD_FORBIDDEN')
    walk(payload)
    if len(_encoded(payload)) > max_bytes:
        raise _PrivacyRedactionError('PAYLOAD_BOUND')
    return True


# @anchor:wa.perceive.redact.project_observation
def project_observation(bundle_or_payload, *, destination, purpose, expected_scope,
                        policy_revision=1, budget_port=None):
    _validate_scope_identity(expected_scope)
    if destination not in _DESTINATIONS or purpose not in _PURPOSES:
        raise _PrivacyRedactionError('invalid destination or purpose')
    limit = 2048 if destination == 'cascade' else 16384
    _reserve(budget_port, limit * 4, 'redact.project_observation')
    if not isinstance(bundle_or_payload, ObservationBundle):
        raise _PrivacyRedactionError('observation bundle required')
    bundle = bundle_or_payload
    if bundle.scope_identity != expected_scope or bundle.observation_ref.scope_identity != expected_scope:
        raise _ScopeAuthorityError('SCOPE_MISMATCH')
    payload, receipts = _project_fields(bundle.target_facts_table, bundle.observation_ref,
                                       destination, purpose, policy_revision)
    validate_public_payload(payload, receipts=receipts, expected_scope=expected_scope, max_bytes=limit)
    if len(_encoded((payload, receipts))) > limit:
        raise _PrivacyRedactionError('PROJECTION_BOUND')
    return payload, receipts


def _project_fields(facts, ref, destination, purpose, revision):
    labels, roles, landmarks, opaque, receipts = [], [], [], [], []
    for f in facts:
        if f.public_label.is_known():
            old = f.declassification_receipt
            if (not _receipt_valid(old, ref.scope_identity)
                    or old.source_observation_id != ref.observation_id
                    or old.field_or_region != f'element:{f.element_id}:label'
                    or old.content_sha256 != _digest(f.public_label.value.encode())
                    or old.transform_chain != _TRANSFORMS):
                raise _PrivacyRedactionError('LABEL_PROVENANCE_MISMATCH')
            labels.append(f.public_label.value)
        elif f.opaque_label_ref:
            opaque.append(f.opaque_label_ref)
        for fact, out in ((f.role_name, roles), (f.landmark_role, landmarks)):
            if fact.is_known():
                if fact.value not in _SCHEMA_ROLES:
                    raise _PrivacyRedactionError('INVALID_SCHEMA_ENUM')
                out.append(fact.value)
    payload = {'labels': tuple(labels), 'roles': tuple(roles), 'landmarks': tuple(landmarks),
               'opaque_refs': tuple(opaque), 'actionable_ids': tuple(f.element_id for f in facts
                if all(x.is_known() and x.value is True for x in (f.visible, f.enabled, f.actionable)))}
    for field in ('labels', 'roles', 'landmarks'):
        for i, text in enumerate(payload[field]):
            receipts.append(_receipt(text.encode(), ref.scope_identity, ref.observation_id,
                f'{field}:{i}', _TRANSFORMS, destination, purpose, revision))
    return payload, tuple(receipts)


_SCHEMA_ROLES = frozenset(('button', 'link', 'textbox', 'checkbox', 'radio', 'switch',
    'combobox', 'menuitem', 'tab', 'main', 'navigation', 'form', 'dialog', 'toolbar',
    'menu', 'search', 'heading', 'row', 'group', 'article', 'listitem', 'text', 'unknown'))


def _inside(x, y, box):
    return box[0] <= x < box[0] + box[2] and box[1] <= y < box[1] + box[3]


# @anchor:wa.perceive.redact.project_image
def project_image(raw_pixels, *, width, height, channels=3, variant='clean', approved_regions,
                  private_regions=(), required_target_bboxes=(), som_marks=(),
                  source_observation_id, parent_publication_id, scope_identity,
                  destination='planner', purpose='escalation_image', policy_revision=1,
                  max_bytes=262144, budget_port=None):
    _validate_scope_identity(scope_identity)
    if not parent_publication_id or not source_observation_id:
        raise _PrivacyRedactionError('MISSING_IMAGE_PROVENANCE')
    if (any(type(n) is not int or n <= 0 for n in (width, height, channels))
            or channels not in (1, 3, 4) or variant not in ('clean', 'som_overlay')
            or type(raw_pixels) is not bytes or len(raw_pixels) != width * height * channels
            or len(raw_pixels) > min(max_bytes, 262144)):
        raise _PrivacyRedactionError('IMAGE_BOUND')
    for box in (*approved_regions, *private_regions, *required_target_bboxes):
        if (len(box) != 4 or any(type(n) is not int for n in box) or box[0] < 0 or box[1] < 0
                or box[2] <= 0 or box[3] <= 0 or box[0]+box[2] > width or box[1]+box[3] > height):
            raise _PrivacyRedactionError('INVALID_REGION')
    def allowed(x, y):
        return any(_inside(x, y, b) for b in approved_regions) and not any(
            _inside(x, y, b) for b in private_regions)
    for box in required_target_bboxes:
        if any(not allowed(x, y) for y in range(box[1], box[1]+box[3])
               for x in range(box[0], box[0]+box[2])):
            raise _PrivacyRedactionError('MASKING_REMOVED_REQUIRED_TARGET_EVIDENCE')
    _reserve(budget_port, len(raw_pixels) * 4, 'redact.project_image')
    pixels = bytearray(len(raw_pixels))
    for y in range(height):
        for x in range(width):
            if allowed(x, y):
                start = (y * width + x) * channels
                pixels[start:start+channels] = raw_pixels[start:start+channels]
    if variant == 'som_overlay':
        digits = ('111101101101111', '010110010010111', '111001111100111',
                  '111001111001111', '101101111001001', '111100111001111',
                  '111100111101111', '111001001001001', '111101111101111', '111101111001111')
        for mark_id, box in som_marks:
            if type(mark_id) is not int or mark_id < 0 or len(box) != 4:
                raise _PrivacyRedactionError('INVALID_MARK')
            for y in range(max(0, box[1]), min(height, box[1]+box[3])):
                for x in range(max(0, box[0]), min(width, box[0]+box[2])):
                    if allowed(x, y) and (x in (box[0], box[0]+box[2]-1) or y in (box[1], box[1]+box[3]-1)):
                        start = (y * width + x) * channels
                        pixels[start:start+channels] = bytes([128 + mark_id % 128]) * channels
            # Render the decimal mark ID using a fixed 3-by-5 numeric glyph.
            for offset, char in enumerate(str(mark_id)):
                glyph = digits[int(char)]
                for gy in range(5):
                    for gx in range(3):
                        x, y = box[0] + 1 + offset * 4 + gx, box[1] + 1 + gy
                        if (glyph[gy*3+gx] == '1' and 0 <= x < width and 0 <= y < height
                                and _inside(x, y, box) and allowed(x, y)):
                            start = (y*width+x)*channels
                            pixels[start:start+channels] = bytes([255]) * channels
    data = bytes(pixels)
    receipt = _receipt(data, scope_identity, source_observation_id,
        f'image_bbox:0,0,{width},{height}', ('region_allowlist_mask', 'private_region_zero', f'variant:{variant}'),
        destination, purpose, policy_revision)
    return ImageRef(receipt.receipt_id, source_observation_id, parent_publication_id,
        scope_identity, variant, width, height, tuple(approved_regions), tuple(private_regions),
        receipt.content_sha256, data, receipt)
