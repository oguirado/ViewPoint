# @module {"id":"wa.scripts.types","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}
"""Shared S0 shapes; construction never grants authority.

Origin: scripts_mesh/build/spec/s0_w.md and SCRIPTS_DESIGN_v2.md.
Host input fence → short store-owner transaction.
Store transactions never acquire the fence, actuate, compile, hash, replay or
call a model. Final admission and invalidation share the host serialization
boundary. Every port is a structural contract, never an implementation.

Digest inputs exclude their own pins/certificates. Lifecycle and telemetry are
outside execution identity. Canonical serialization belongs to S1. Private
records have no generic export operation; repr hides handles and receipt data.
Host attestation, registries, privacy approval and authenticated restart evidence
are external prerequisites. Equal records are not interchangeable capabilities.
B owns the following exchange records; owner implementations supply the private
port type parameters below. No local record or backend import substitutes for them:
* CandidateRef: exact logical/revision/artifact identity, scope and lifecycle
  revision; retrieval conveys no authority (B resolver/store boundary).
* ScriptLease (B script_store): owner-issued episode/process-incarnation and
  retained-owner tokens; retention is distinct from current step eligibility.
* CatalogueSnapshot (B catalogue): canonically sorted immutable metadata,
  generation, full digest and nonwrapping deny sequence; pinned context cannot
  replace the current host eligibility check.
* RunEvent (B script_ledger): schema/process/run/episode/action/event identity,
  ordered index, artifact, source, dispositions, approved observations and closed
  reasons; telemetry has provenance or explicit absence, never private payloads.
* EpisodeSeal (B script_ledger): expected event count, verified terminal goal,
  explicit completeness, chain head, policy/compiler/verifier/environment/suite
  digests, producer provenance and independent support; integrity alone is not
  authenticity and incomplete tails supply no support.
* DreamCertificate (B dream_replay): owner-verified candidate/support identity,
  replay coverage through the claimed terminal/window, scope and current
  policy/environment/verifier compatibility; missing/conflicting support is
  UNKNOWN, and construction or a certificate string grants no promotion.

Numeric policy fields have explicit units and no defaults; values are trusted
configuration, not measurements or constructor-selected design targets.
"""
from dataclasses import dataclass as _dataclass, field as _field, fields as _fields, MISSING as _MISSING
from enum import Enum as _Enum
from types import UnionType as _UnionType
from typing import Protocol as _Protocol, TypeVar as _TypeVar, get_args as _get_args, get_origin as _get_origin, get_type_hints as _get_type_hints
from unicodedata import normalize as _normalize


# @anchor:wa.scripts.types.Reason
class Reason(_Enum):
    SCHEMA = 'schema'
    TYPE = 'type'
    BOUNDS = 'bounds'
    MISSING = 'missing_evidence'
    CONFLICT = 'conflict'
    STALE = 'stale_authority'
    FOREIGN = 'foreign_handle'
    CAPACITY = 'capacity'
    CANCELLED = 'cancellation'
    DEADLINE = 'deadline'
    RECONCILIATION = 'reconciliation'
    PRIVACY = 'privacy'
    UNKNOWN = 'unknown'


# @anchor:wa.scripts.types.ContractError
class ContractError(ValueError):
    """Closed reason only: never embeds a caller's exception or private value."""
    def __init__(self, reason):
        if type(reason) is not Reason:
            reason = Reason.TYPE
        self.reason = reason
        super().__init__(reason.value)


# @anchor:wa.scripts.types.Truth
class Truth(_Enum):
    TRUE = 'TRUE'
    FALSE = 'FALSE'
    UNKNOWN = 'UNKNOWN'

    def __bool__(self):
        raise ContractError(Reason.MISSING if self is Truth.UNKNOWN else Reason.TYPE)


def _require(condition, reason=Reason.SCHEMA):
    if not condition:
        raise ContractError(reason)


def _matches(value, annotation):
    origin, args = _get_origin(annotation), _get_args(annotation)
    if origin is _UnionType:
        return any(_matches(value, arg) for arg in args)
    if origin is tuple:
        if type(value) is not tuple:
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches(item, args[0]) for item in value)
        return len(value) == len(args) and all(_matches(v, a) for v, a in zip(value, args))
    return type(value) is annotation


def _scalar_check(value, *, allow_signed=False):
    if type(value) is str:
        invalid_unicode = False
        try:
            value.encode('utf-8', errors='strict')
        except UnicodeError:
            invalid_unicode = True
        # Leave the handler before raising: even suppressed context retains input.
        _require(not invalid_unicode, Reason.TYPE)
        _require(bool(value), Reason.MISSING)
    elif type(value) is int:
        _require(allow_signed or value >= 0, Reason.BOUNDS)
    elif type(value) is tuple:
        for item in value:
            _scalar_check(item, allow_signed=allow_signed)


class _RecordMeta(type):
    def __call__(cls, *args, **kwargs):
        # Reject signatures without creating a native exception with private keys.
        fields = tuple(field for field in _fields(cls) if field.init)
        names = tuple(field.name for field in fields)
        _require(len(args) <= len(names), Reason.SCHEMA)
        supplied = set(names[:len(args)])
        _require(not supplied.intersection(kwargs) and set(kwargs) <= set(names), Reason.SCHEMA)
        supplied.update(kwargs)
        _require(all(field.name in supplied or field.default is not _MISSING
                     or field.default_factory is not _MISSING for field in fields), Reason.SCHEMA)
        return super().__call__(*args, **kwargs)


class _Record(metaclass=_RecordMeta):
    __slots__ = ()

    def __post_init__(self):
        annotations = _get_type_hints(type(self))
        for field in _fields(self):
            value = getattr(self, field.name)
            _require(_matches(value, annotations[field.name]), Reason.TYPE)
            signed = (type(self) in (Operand, MetadataEntry) and field.name == 'value') or (
                type(self) is ArgSpec and self.kind is ValueKind.INTEGER
                and field.name in ('minimum', 'maximum'))
            _scalar_check(value, allow_signed=signed)
        self._validate()

    def _validate(self):
        pass


def _unique(values):
    _require(len(values) == len(set(values)), Reason.SCHEMA)


def _unique_names(values):
    _unique(tuple(_normalize('NFKC', name).casefold() for name in values))


def _named(values):
    names = tuple(item.name for item in values)
    _unique_names(names)
    _require(names == tuple(sorted(names)))


def _present(value, reason):
    _require((value is None) == (reason is not None), Reason.MISSING)


# @anchor:wa.scripts.types.freeze_entries
def freeze_entries(entries):
    """Explicit ingress copy of passive named metadata, never declassification.

    Accepts entry pairs rather than dicts so duplicate names cannot disappear.
    Host approval is required before metadata becomes reusable/public.
    """
    # Fixed representation ceilings [TARGET]: depth 8, 1024 nodes, 256 KiB.
    # Count across the complete ingress, including nested mappings and keys.
    count, size = 0, 0

    def charge(value, depth):
        nonlocal count, size
        count += 1
        _require(depth <= 8 and count <= 1024, Reason.BOUNDS)
        if type(value) is str:
            _require(len(value) <= 262144 - size, Reason.BOUNDS)
            _scalar_check(value)
            size += len(value.encode('utf-8'))
        elif type(value) is int:
            _require(value.bit_length() <= 2097152, Reason.BOUNDS)
            size += max(1, (value.bit_length() + 7) // 8)
        else:
            size += 1
        _require(size <= 262144, Reason.BOUNDS)

    def pairs(entries, depth):
        result = []
        for entry in entries:
            _require(type(entry) in (tuple, list) and len(entry) == 2)
            name, value = entry
            _require(type(name) is str, Reason.TYPE)
            charge(name, depth)
            result.append(MetadataEntry(name, freeze(value, depth)))
        result = tuple(sorted(result, key=lambda item: item.name))
        _named(result)
        return result

    def freeze(value, depth):
        charge(value, depth)
        if type(value) in (str, int, bool, type(None)):
            return value
        if type(value) in (tuple, list):
            return tuple(freeze(item, depth + 1) for item in value)
        if type(value) is dict:
            return pairs(value.items(), depth + 1)
        raise ContractError(Reason.TYPE)
    return pairs(entries, 0)


# @anchor:wa.scripts.types.ValueKind
class ValueKind(_Enum):
    TEXT = 'TEXT'
    INTEGER = 'INTEGER'
    BOOLEAN = 'BOOLEAN'
    KEY = 'KEY'


# @anchor:wa.scripts.types.Sensitivity
class Sensitivity(_Enum):
    PUBLIC = 'PUBLIC'
    PRIVATE = 'PRIVATE'


# @anchor:wa.scripts.types.MissingReason
class MissingReason(_Enum):
    ABSENT = 'absent'
    UNSUPPORTED = 'unsupported'
    INCOMPLETE = 'incomplete'
    CONFLICTING = 'conflicting'
    STALE = 'stale'
    UNAVAILABLE_MEASUREMENT = 'unavailable_measurement'
    INAPPLICABLE_EVIDENCE = 'inapplicable_evidence'


# @anchor:wa.scripts.types.ExportDestination
class ExportDestination(_Enum):
    REUSABLE_ARTIFACT = 'reusable_artifact'
    CATALOGUE_CARD = 'catalogue_card'
    PUBLIC_RECEIPT = 'public_receipt'
    GENERATION_BRIEF = 'generation_brief'
    FROZEN_PREFIX = 'frozen_prefix'
    MODEL_PAYLOAD = 'model_payload'


# @anchor:wa.scripts.types.Purpose
class Purpose(_Enum):
    INPUT = 'INPUT'
    HARVEST = 'HARVEST'
    QUERY = 'QUERY'


# @anchor:wa.scripts.types.ActionKind
class ActionKind(_Enum):
    FIND = 'find'
    CLICK = 'click'
    TYPE = 'type'
    KEY = 'key'
    WAIT_UNTIL = 'wait_until'
    ASSERT_TAG = 'assert_tag'
    HARVEST = 'harvest'


# @anchor:wa.scripts.types.PredicateOperator
class PredicateOperator(_Enum):
    ALL = 'all'
    ANY = 'any'
    NOT = 'not'
    EXISTS_UNIQUE = 'exists_unique'
    STATE_FLAG = 'state_flag'
    FOCUSED = 'focused'
    VALUE_LEN_DELTA = 'value_len_delta'
    SCOPED_TERMINAL = 'scoped_terminal'
    DECLARED_TRANSITION = 'declared_transition'
    TASK_GOAL = 'task_goal'


# @anchor:wa.scripts.types.NodeKind
class NodeKind(_Enum):
    EFFECT = 'EFFECT'
    PREDICATE = 'PREDICATE'
    RESULT = 'RESULT'
    TERMINAL = 'TERMINAL'


# @anchor:wa.scripts.types.InputDisposition
class InputDisposition(_Enum):
    NOT_SENT = 'not_sent'
    APPLIED = 'applied'
    INPUT_UNCERTAIN = 'input_uncertain'


# @anchor:wa.scripts.types.EventState
class EventState(_Enum):
    NOT_SENT = 'not_sent'
    ADMITTED = 'admitted'
    APPLIED = 'applied'
    INPUT_UNCERTAIN = 'input_uncertain'
    VERIFIED = 'verified'
    REFUSED = 'refused'
    SUSPENDED = 'suspended'
    CANCELLED = 'cancelled'


# @anchor:wa.scripts.types.Lifecycle
class Lifecycle(_Enum):
    DRAFT = 'draft'
    SHADOW = 'shadow'
    PROMOTED = 'promoted'
    SUSPENDED = 'suspended'
    QUARANTINED = 'quarantined'
    RETIRED = 'retired'


# @anchor:wa.scripts.types.FailureClass
class FailureClass(_Enum):
    EPISODE_CANCELLATION = 'EPISODE_CANCELLATION'
    EPISODE_DEADLINE = 'EPISODE_DEADLINE'
    BACKEND_OUTAGE = 'BACKEND_OUTAGE'
    AUDIT_OUTAGE = 'AUDIT_OUTAGE'
    INCOMPATIBLE_ENVIRONMENT = 'INCOMPATIBLE_ENVIRONMENT'
    INCOMPATIBLE_SCOPE = 'INCOMPATIBLE_SCOPE'
    ARTIFACT_DEFECT = 'ARTIFACT_DEFECT'
    UNCLASSIFIED = 'UNCLASSIFIED'


# @anchor:wa.scripts.types.MatchLevel
class MatchLevel(_Enum):
    L0_EXACT = 'L0_EXACT'
    L1_STABLE = 'L1_STABLE'
    L2_NAME = 'L2_NAME'
    L3_VARIANT = 'L3_VARIANT'
    L4_UNIQUE_ROLE = 'L4_UNIQUE_ROLE'


# @anchor:wa.scripts.types.Lane
class Lane(_Enum):
    SCRIPT = 'SCRIPT'
    STRUCTURAL = 'STRUCTURAL'
    TEXT = 'TEXT'
    VISUAL = 'VISUAL'
    MUSE = 'MUSE'


# @anchor:wa.scripts.types.EvidenceSource
class EvidenceSource(_Enum):
    AX = 'AX'
    HOST = 'HOST'
    COMMITTED_PUBLICATION = 'COMMITTED_PUBLICATION'


# @anchor:wa.scripts.types.Ownership
class Ownership(_Enum):
    RETAINED = 'RETAINED'
    TRANSIENT = 'TRANSIENT'


# @anchor:wa.scripts.types.TerminalStatus
class TerminalStatus(_Enum):
    COMPLETED = 'COMPLETED'
    REFUSED = 'REFUSED'
    SUSPENDED = 'SUSPENDED'
    CANCELLED = 'CANCELLED'
    INPUT_UNCERTAIN = 'INPUT_UNCERTAIN'


def _metadata(value):
    if type(value) in (str, int, bool, type(None)):
        _scalar_check(value, allow_signed=True)
    elif type(value) is tuple:
        if value and all(type(item) is MetadataEntry for item in value):
            _named(value)
        else:
            for item in value:
                _metadata(item)
    else:
        raise ContractError(Reason.TYPE)


def _semantic(predicate):
    if predicate.operator in (PredicateOperator.DECLARED_TRANSITION, PredicateOperator.VALUE_LEN_DELTA, PredicateOperator.SCOPED_TERMINAL, PredicateOperator.TASK_GOAL):
        return True
    if predicate.operator is PredicateOperator.ALL:
        return any(_semantic(child) for child in predicate.children)
    if predicate.operator is PredicateOperator.ANY:
        return all(_semantic(child) for child in predicate.children)
    return False


# @anchor:wa.scripts.types.MetadataEntry
@_dataclass(frozen=True, slots=True)
class MetadataEntry(_Record):
    name: str = _field(repr=False)
    value: str | int | bool | None | tuple = _field(repr=False)

    def _validate(self):
        _metadata(self.value)


# @anchor:wa.scripts.types.PrivateRef
@_dataclass(frozen=True, slots=True)
class PrivateRef(_Record):
    owner: str
    incarnation: str
    token: str = _field(repr=False)


# @anchor:wa.scripts.types.MissingRef
@_dataclass(frozen=True, slots=True)
class MissingRef(_Record):
    value: str | None = _field(repr=False)
    reason: Reason | None

    def _validate(self):
        _present(self.value, self.reason)


# @anchor:wa.scripts.types.ScopeIdentity
@_dataclass(frozen=True, slots=True)
class ScopeIdentity(_Record):
    principal: str = _field(repr=False)
    tenant: str = _field(repr=False)
    browser_profile_incarnation: str = _field(repr=False)
    top_level_origin: str = _field(repr=False)
    target_frame_origin: str = _field(repr=False)
    frame_ancestry: tuple[str, ...] = _field(repr=False)
    scope_revision: int
    attestation: PrivateRef | None = _field(repr=False)
    attestation_reason: Reason | None

    def _validate(self):
        _present(self.attestation, self.attestation_reason)


# @anchor:wa.scripts.types.AuthorityRevision
@_dataclass(frozen=True, slots=True)
class AuthorityRevision(_Record):
    policy: int
    capability_set: int
    schema: int
    calibration_certificate: int | None
    calibration_reason: Reason | None
    scope: int
    environment_compatibility: int

    def _validate(self):
        _present(self.calibration_certificate, self.calibration_reason)


# @anchor:wa.scripts.types.ArgSpec
@_dataclass(frozen=True, slots=True)
class ArgSpec(_Record):
    name: str
    kind: ValueKind
    required: bool
    minimum: int
    maximum: int
    sensitivity: Sensitivity
    purpose: Purpose
    capability_contract: str
    destination_contract: str
    normalization_abi: str

    def _validate(self):
        _require(self.minimum <= self.maximum, Reason.BOUNDS)


# @anchor:wa.scripts.types.ABISet
@_dataclass(frozen=True, slots=True)
class ABISet(_Record):
    dsl: str
    compiler: str
    act: str
    predicate: str
    schema: str


# @anchor:wa.scripts.types.PlanDigest
@_dataclass(frozen=True, slots=True)
class PlanDigest(_Record):
    algorithm: str
    full_digest: str

    def _validate(self):
        _require(self.algorithm == 'sha256' and len(self.full_digest) == 64 and all(c in '0123456789abcdef' for c in self.full_digest))


# @anchor:wa.scripts.types.MetadataDigest
@_dataclass(frozen=True, slots=True)
class MetadataDigest(_Record):
    algorithm: str
    full_digest: str

    def _validate(self):
        _require(self.algorithm == 'sha256' and len(self.full_digest) == 64 and all(c in '0123456789abcdef' for c in self.full_digest))


# @anchor:wa.scripts.types.ArtifactDigest
@_dataclass(frozen=True, slots=True)
class ArtifactDigest(_Record):
    algorithm: str
    full_digest: str

    def _validate(self):
        _require(self.algorithm == 'sha256' and len(self.full_digest) == 64 and all(c in '0123456789abcdef' for c in self.full_digest))


# @anchor:wa.scripts.types.ArtifactIdentity
@_dataclass(frozen=True, slots=True)
class ArtifactIdentity(_Record):
    plan: PlanDigest
    metadata: MetadataDigest
    artifact: ArtifactDigest
    logical_id: str
    revision: int
    scope: ScopeIdentity
    signature_schema: str
    structural_screen_digest: PlanDigest
    goal_contract: str
    source_digest: PlanDigest
    abis: ABISet


# @anchor:wa.scripts.types.PlanLimits
@_dataclass(frozen=True, slots=True)
class PlanLimits(_Record):
    max_nodes: int
    canonical_bytes: int
    absolute_deadline_ns: int
    max_acquisitions: int
    episode_input_bytes: int
    episode_result_bytes: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self)), Reason.BOUNDS)
        _require(self.max_nodes <= 64 and self.canonical_bytes <= 262144
                 and self.max_acquisitions <= 64, Reason.BOUNDS)  # [TARGET]


# @anchor:wa.scripts.types.ScriptHeader
@_dataclass(frozen=True, slots=True)
class ScriptHeader(_Record):
    logical_id: str
    revision: int
    predecessor: MissingRef
    scope: ScopeIdentity
    site_contract: str
    goal_contract: str
    signature_contract: str
    abis: ABISet
    arguments: tuple[ArgSpec, ...]
    capabilities: tuple[str, ...]
    limits: PlanLimits
    entry: 'PredicateSpec'
    terminal_goal: str
    metadata_references: tuple[str, ...]
    supplied_pins: ArtifactIdentity | None
    pins_reason: Reason | None

    def _validate(self):
        _require(self.entry.scope == self.scope, Reason.STALE)
        _unique_names(tuple(arg.name for arg in self.arguments))
        _unique(self.capabilities)
        _present(self.supplied_pins, self.pins_reason)


# @anchor:wa.scripts.types.TargetSelector
@_dataclass(frozen=True, slots=True)
class TargetSelector(_Record):
    role: str
    canonical_label_reference: str
    label_export_receipt: PrivateRef = _field(repr=False)
    landmark: str
    container_owner: str
    turn_owner: str
    row: int | None
    column: int | None
    header_contract: MissingRef
    action: ActionKind
    allowed_match_levels: tuple[MatchLevel, ...]
    structural_identity_requirements: tuple[str, ...]

    def _validate(self):
        _require(bool(self.allowed_match_levels) and bool(self.structural_identity_requirements), Reason.MISSING)
        _unique(self.allowed_match_levels)
        _require(all(level in (MatchLevel.L0_EXACT, MatchLevel.L1_STABLE)
                     for level in self.allowed_match_levels), Reason.FOREIGN)


# @anchor:wa.scripts.types.ObservationRef
@_dataclass(frozen=True, slots=True)
class ObservationRef(_Record):
    scope: ScopeIdentity
    publication_identity: str
    producer_identity: str
    producer_generation: int | None
    producer_schema: str
    capture_start_ns: int
    capture_end_ns: int
    association: MissingRef
    completeness: MissingRef
    projection: MissingRef
    provenance: str
    generation_reason: Reason | None
    authority: AuthorityRevision

    def _validate(self):
        _require(self.authority.scope == self.scope.scope_revision, Reason.STALE)
        _present(self.producer_generation, self.generation_reason)
        _require(self.capture_start_ns <= self.capture_end_ns, Reason.BOUNDS)


# @anchor:wa.scripts.types.Fact
@_dataclass(frozen=True, slots=True)
class Fact(_Record):
    value: Truth
    source: EvidenceSource
    source_generation: int | None
    reason: Reason | None

    def _validate(self):
        _require((self.value is Truth.UNKNOWN) == (self.reason is not None), Reason.MISSING)
        _require(self.source_generation is not None or self.value is Truth.UNKNOWN, Reason.MISSING)


# @anchor:wa.scripts.types.TargetFacts
@_dataclass(frozen=True, slots=True)
class TargetFacts(_Record):
    visible: 'FactEvidence'
    enabled: 'FactEvidence'
    actionable: 'FactEvidence'
    editable: 'FactEvidence'
    focused: 'FactEvidence'
    busy: 'FactEvidence'
    blocked: 'FactEvidence'


# @anchor:wa.scripts.types.ProducerLayout
@_dataclass(frozen=True, slots=True)
class ProducerLayout(_Record):
    """Owner-attested semantic wire layout; never a presentation bit word."""
    source: EvidenceSource
    schema: str
    attestation: PrivateRef = _field(repr=False)


# @anchor:wa.scripts.types.ExecutionWitness
@_dataclass(frozen=True, slots=True)
class ExecutionWitness(_Record):
    scope: ScopeIdentity
    observation: ObservationRef
    producer_schema: str
    producer_layout: ProducerLayout
    producer_generation: int
    ownership_scope: str
    supported_actions: tuple[ActionKind, ...]
    facts: TargetFacts
    custody: PrivateRef = _field(repr=False)
    expires_ns: int
    transform_reference: MissingRef
    join_reference: MissingRef
    registry_handle: PrivateRef = _field(repr=False)

    def _validate(self):
        _require(self.scope == self.observation.scope and self.scope.attestation is not None, Reason.MISSING)
        _require(self.producer_generation == self.observation.producer_generation and self.producer_schema == self.observation.producer_schema, Reason.STALE)
        _require(self.producer_layout.schema == self.producer_schema, Reason.STALE)
        _require(bool(self.supported_actions), Reason.MISSING)
        _require(self.expires_ns >= self.observation.capture_end_ns, Reason.DEADLINE)
        for field in _fields(self.facts):
            fact = getattr(self.facts, field.name)
            _require(fact.source_generation == self.producer_generation
                     and fact.source_schema == self.producer_schema
                     and fact.source is self.producer_layout.source
                     and fact.observation == self.observation, Reason.STALE)


# @anchor:wa.scripts.types.TargetBinding
@_dataclass(frozen=True, slots=True)
class TargetBinding(_Record):
    selector: TargetSelector
    host_reference: PrivateRef = _field(repr=False)
    observation: ObservationRef
    witness: ExecutionWitness
    artifact: ArtifactIdentity
    step_id: str
    scope: ScopeIdentity
    authority: AuthorityRevision
    binder_handle: PrivateRef = _field(repr=False)

    def _validate(self):
        _require(self.scope == self.observation.scope == self.witness.scope and self.observation == self.witness.observation, Reason.STALE)
        _require(self.artifact.scope == self.scope, Reason.STALE)
        _require(self.authority == self.observation.authority, Reason.STALE)
        _require(self.authority.scope == self.scope.scope_revision, Reason.STALE)
        _require(self.selector.action in self.witness.supported_actions)


# @anchor:wa.scripts.types.Operand
@_dataclass(frozen=True, slots=True)
class Operand(_Record):
    operand_id: str
    query_id: str
    kind: ValueKind
    value: str | int | bool

    def _validate(self):
        _require(type(self.value) is {ValueKind.TEXT: str, ValueKind.INTEGER: int, ValueKind.BOOLEAN: bool, ValueKind.KEY: str}[self.kind], Reason.TYPE)


# @anchor:wa.scripts.types.PredicateSpec
@_dataclass(frozen=True, slots=True)
class PredicateSpec(_Record):
    operator: PredicateOperator
    operands: tuple[Operand, ...]
    children: tuple['PredicateSpec', ...]
    scope: ScopeIdentity
    source_requirements: tuple[EvidenceSource, ...]
    transition_reference: MissingRef

    def _validate(self):
        _unique_names(tuple(item.operand_id for item in self.operands))
        _require(bool(self.source_requirements), Reason.MISSING)
        _require(all(child.scope == self.scope for child in self.children), Reason.STALE)
        if self.operator is PredicateOperator.NOT:
            _require(len(self.children) == 1 and not self.operands)
        elif self.operator in (PredicateOperator.ALL, PredicateOperator.ANY):
            _require(bool(self.children) and not self.operands)
        else:
            _require(not self.children and len(self.operands) == 1)
            expected = {
                PredicateOperator.EXISTS_UNIQUE: ValueKind.TEXT,
                PredicateOperator.STATE_FLAG: ValueKind.TEXT,
                PredicateOperator.FOCUSED: ValueKind.TEXT,
                PredicateOperator.VALUE_LEN_DELTA: ValueKind.INTEGER,
                PredicateOperator.SCOPED_TERMINAL: ValueKind.TEXT,
                PredicateOperator.DECLARED_TRANSITION: ValueKind.TEXT,
                PredicateOperator.TASK_GOAL: ValueKind.TEXT,
            }[self.operator]
            _require(self.operands[0].kind is expected, Reason.TYPE)
            if self.operator is PredicateOperator.STATE_FLAG:
                _require(self.operands[0].value in (
                    'visible', 'enabled', 'actionable', 'editable', 'focused', 'busy', 'blocked'))
        if self.operator in (PredicateOperator.DECLARED_TRANSITION, PredicateOperator.VALUE_LEN_DELTA):
            _require(self.transition_reference.value is not None, Reason.MISSING)


# @anchor:wa.scripts.types.SlotDescriptor
@_dataclass(frozen=True, slots=True)
class SlotDescriptor(_Record):
    slot_name: str
    kind: ValueKind
    purpose: Purpose
    capability: PrivateRef = _field(repr=False)
    episode_id: str
    scope: ScopeIdentity
    destination_contract: str
    capability_contract: str
    key_vocabulary: 'HostKeyVocabulary | None'


# @anchor:wa.scripts.types.VerificationBudget
@_dataclass(frozen=True, slots=True)
class VerificationBudget(_Record):
    timeout_ms: int
    max_acquisitions: int
    absolute_deadline_ns: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self)), Reason.BOUNDS)
        _require(self.timeout_ms <= 2000 and self.max_acquisitions <= 64, Reason.BOUNDS)  # [TARGET]


def _action_arguments(action, arguments):
    """One structural action contract shared by effects and send intents."""
    _unique_names(tuple(slot.slot_name for slot in arguments))
    if action in (ActionKind.TYPE, ActionKind.KEY):
        _require(len(arguments) == 1, Reason.MISSING)
        slot = arguments[0]
        _require(slot.kind is (ValueKind.TEXT if action is ActionKind.TYPE else ValueKind.KEY), Reason.TYPE)
        if action is ActionKind.KEY:
            _require(slot.key_vocabulary is not None, Reason.MISSING)


# @anchor:wa.scripts.types.EffectSpec
@_dataclass(frozen=True, slots=True)
class EffectSpec(_Record):
    action: ActionKind
    selector: TargetSelector
    arguments: tuple[SlotDescriptor, ...]
    precondition: PredicateSpec
    postcondition: PredicateSpec
    verification: VerificationBudget

    def _validate(self):
        _require(self.action in (ActionKind.CLICK, ActionKind.TYPE, ActionKind.KEY))
        _require(self.action is self.selector.action)
        _require(self.precondition.scope == self.postcondition.scope, Reason.STALE)
        _require(_semantic(self.postcondition), Reason.MISSING)
        _require(all(slot.scope == self.precondition.scope and slot.purpose is Purpose.INPUT for slot in self.arguments), Reason.FOREIGN)
        _action_arguments(self.action, self.arguments)


# @anchor:wa.scripts.types.ResultDeclaration
@_dataclass(frozen=True, slots=True)
class ResultDeclaration(_Record):
    name: str
    kind: ValueKind
    purpose: Purpose
    destination_contract: str
    max_bytes: int

    def _validate(self):
        _require(0 < self.max_bytes <= 16384, Reason.BOUNDS)  # [TARGET]
        _require(self.purpose is Purpose.HARVEST)


# @anchor:wa.scripts.types.TerminalGoalRef
@_dataclass(frozen=True, slots=True)
class TerminalGoalRef(_Record):
    reference: str
    scope: ScopeIdentity
    host_handle: PrivateRef = _field(repr=False)


# @anchor:wa.scripts.types.QueryDeclaration
@_dataclass(frozen=True, slots=True)
class QueryDeclaration(_Record):
    name: str
    selector: TargetSelector
    scope: ScopeIdentity
    operators: tuple[PredicateOperator, ...]

    def _validate(self):
        _require(bool(self.operators), Reason.MISSING)
        _unique(self.operators)
        _require(all(op not in (PredicateOperator.ALL, PredicateOperator.ANY,
                               PredicateOperator.NOT) for op in self.operators))


# @anchor:wa.scripts.types.FindSpec
@_dataclass(frozen=True, slots=True)
class FindSpec(_Record):
    query: QueryDeclaration


# @anchor:wa.scripts.types.WaitSpec
@_dataclass(frozen=True, slots=True)
class WaitSpec(_Record):
    predicate: PredicateSpec
    budget: 'WaitPolicy'


# @anchor:wa.scripts.types.AssertSpec
@_dataclass(frozen=True, slots=True)
class AssertSpec(_Record):
    predicate: PredicateSpec
    code: Reason


# @anchor:wa.scripts.types.HarvestSpec
@_dataclass(frozen=True, slots=True)
class HarvestSpec(_Record):
    query_id: str
    result: ResultDeclaration
    authorization: 'RegistryRef' = _field(repr=False)
    scope: ScopeIdentity
    max_bytes: int

    def _validate(self):
        _require(0 < self.max_bytes <= self.result.max_bytes, Reason.BOUNDS)
        _require(self.authorization.kind == 'harvest', Reason.FOREIGN)


# @anchor:wa.scripts.types.StepPlan
@_dataclass(frozen=True, slots=True)
class StepPlan(_Record):
    step_id: str
    kind: ActionKind | NodeKind
    effect: EffectSpec | None
    predicate: PredicateSpec | None
    result: ResultDeclaration | None
    terminal: TerminalGoalRef | None
    find: FindSpec | None = None
    wait: WaitSpec | None = None
    assertion: AssertSpec | None = None
    harvest: HarvestSpec | None = None

    def _validate(self):
        # NodeKind is retained solely for the separate terminal-goal node.
        payloads = (self.effect, self.predicate, self.result, self.terminal,
                    self.find, self.wait, self.assertion, self.harvest)
        indices = {ActionKind.FIND: 4, ActionKind.CLICK: 0,
                   ActionKind.TYPE: 0, ActionKind.KEY: 0,
                   ActionKind.WAIT_UNTIL: 5, ActionKind.ASSERT_TAG: 6,
                   ActionKind.HARVEST: 7, NodeKind.TERMINAL: 3}
        _require(self.kind in indices)
        index = indices[self.kind]
        _require(all((item is not None) == (i == index)
                     for i, item in enumerate(payloads)), Reason.MISSING)
        if self.effect is not None:
            _require(self.kind is self.effect.action)


# @anchor:wa.scripts.types.ValidatedPlan
@_dataclass(frozen=True, slots=True)
class ValidatedPlan(_Record):
    nodes: tuple[StepPlan, ...]
    symbols: tuple[ResultDeclaration, ...]
    slots: tuple[ArgSpec, ...]
    entry: PredicateSpec
    terminal_goal: TerminalGoalRef
    limits: PlanLimits
    abis: ABISet
    identity: ArtifactIdentity
    scope: ScopeIdentity
    compiler_handle: PrivateRef = _field(repr=False)

    definition: 'PlanDefinition'
    canonical_bytes: bytes = _field(repr=False)
    compiler_registry: 'RegistryRef' = _field(repr=False)
    queries: tuple[QueryDeclaration, ...] = ()

    def _validate(self):
        _plan_structure(self.nodes, self.symbols, self.slots, self.entry,
                        self.terminal_goal, self.limits, self.scope, self.queries)
        _require(0 < len(self.canonical_bytes) <= self.limits.canonical_bytes, Reason.BOUNDS)
        for name in ('nodes', 'symbols', 'slots', 'entry', 'terminal_goal', 'limits', 'identity', 'queries'):
            _require(getattr(self, name) == getattr(self.definition, name), Reason.STALE)
        _require(self.scope == self.definition.header.scope == self.identity.scope, Reason.STALE)
        _require(self.abis == self.definition.header.abis == self.identity.abis, Reason.STALE)


def _plan_structure(nodes, symbols, slots, entry, terminal_goal, limits, scope, queries):
    _require(bool(nodes), Reason.MISSING)
    _require(len(nodes) <= limits.max_nodes, Reason.BOUNDS)
    _unique_names(tuple(node.step_id for node in nodes))
    _unique_names(tuple(slot.name for slot in slots))
    _unique_names(tuple(symbol.name for symbol in symbols))
    _require(entry.scope == terminal_goal.scope == scope, Reason.STALE)
    _require(nodes[-1].terminal == terminal_goal, Reason.MISSING)
    _require(sum(node.kind is NodeKind.TERMINAL for node in nodes) == 1)
    _unique_names(tuple(query.name for query in queries))
    query_table = {query.name: query for query in queries}
    result_table = {symbol.name: symbol for symbol in symbols}
    _require(all(query.scope == scope for query in queries), Reason.STALE)

    def check_predicate(predicate):
        _require(predicate.scope == scope, Reason.STALE)
        for operand in predicate.operands:
            query = query_table.get(operand.query_id)
            _require(query is not None and predicate.operator in query.operators, Reason.FOREIGN)
        for child in predicate.children:
            check_predicate(child)

    check_predicate(entry)
    for node in nodes:
        if node.result is not None:
            _require(result_table.get(node.result.name) == node.result, Reason.FOREIGN)
        if node.find is not None:
            _require(query_table.get(node.find.query.name) == node.find.query, Reason.FOREIGN)
        if node.wait is not None:
            check_predicate(node.wait.predicate)
        if node.assertion is not None:
            check_predicate(node.assertion.predicate)
        if node.harvest is not None:
            harvest = node.harvest
            _require(harvest.scope == scope, Reason.STALE)
            _require(result_table.get(harvest.result.name) == harvest.result, Reason.FOREIGN)
            _require(harvest.query_id in query_table, Reason.FOREIGN)
            _require(harvest.max_bytes <= limits.episode_result_bytes, Reason.BOUNDS)
        if node.effect is not None:
            check_predicate(node.effect.precondition)
            check_predicate(node.effect.postcondition)
            _require(node.effect.precondition.scope == scope, Reason.STALE)
            declarations = {slot.name: slot for slot in slots}
            for argument in node.effect.arguments:
                declaration = declarations.get(argument.slot_name)
                _require(declaration is not None, Reason.FOREIGN)
                _require(argument.kind is declaration.kind
                         and argument.purpose is declaration.purpose
                         and argument.destination_contract == declaration.destination_contract
                         and argument.capability_contract == declaration.capability_contract,
                         Reason.FOREIGN)
        if node.predicate is not None:
            check_predicate(node.predicate)

# @anchor:wa.scripts.types.StepIntent
@_dataclass(frozen=True, slots=True)
class StepIntent(_Record):
    artifact: ArtifactIdentity
    step_id: str
    order: int
    action: ActionKind
    selector: TargetSelector
    arguments: tuple[SlotDescriptor, ...]
    expected_transition: PredicateSpec
    scope: ScopeIdentity
    authority: AuthorityRevision

    def _validate(self):
        _require(self.action in (ActionKind.CLICK, ActionKind.TYPE, ActionKind.KEY))
        _require(self.action is self.selector.action)
        _action_arguments(self.action, self.arguments)
        _require(self.artifact.scope == self.scope, Reason.STALE)
        _require(self.expected_transition.scope == self.scope and self.authority.scope == self.scope.scope_revision, Reason.STALE)
        _require(all(slot.scope == self.scope and slot.purpose is Purpose.INPUT for slot in self.arguments), Reason.FOREIGN)


# @anchor:wa.scripts.types.MeasuredValue
@_dataclass(frozen=True, slots=True)
class MeasuredValue(_Record):
    value: int | None
    unit: str
    provenance: MissingRef
    reason: Reason | None

    def _validate(self):
        _present(self.value, self.reason)
        _require((self.value is None) == (self.provenance.value is None), Reason.MISSING)


# @anchor:wa.scripts.types.PredicateResult
@_dataclass(frozen=True, slots=True)
class PredicateResult(_Record):
    value: Truth
    reason: Reason | None

    def _validate(self):
        _require((self.value is Truth.UNKNOWN) == (self.reason is not None), Reason.MISSING)


# @anchor:wa.scripts.types.StepResult
@_dataclass(frozen=True, slots=True)
class StepResult(_Record):
    pre: ObservationRef | None
    post: ObservationRef | None
    observation_reason: Reason | None
    action_token: PrivateRef | None = _field(repr=False)
    input_disposition: InputDisposition
    predicate: PredicateResult
    checkpoint: PredicateResult
    duration: MeasuredValue
    usage: tuple[MeasuredValue, ...]
    usage_reason: Reason | None

    def _validate(self):
        _require((self.pre is None or self.post is None) == (self.observation_reason is not None), Reason.MISSING)
        _require(self.input_disposition is InputDisposition.NOT_SENT or self.action_token is not None, Reason.MISSING)
        _require(bool(self.usage) == (self.usage_reason is None), Reason.MISSING)
        if self.pre is not None and self.post is not None:
            _require(self.pre.scope == self.post.scope, Reason.STALE)


# @anchor:wa.scripts.types.RuntimeOutcome
@_dataclass(frozen=True, slots=True)
class RuntimeOutcome(_Record):
    episode_id: str
    artifact: ArtifactIdentity
    scope: ScopeIdentity
    status: TerminalStatus
    input_disposition: InputDisposition
    checkpoint: PredicateResult
    task_goal: PredicateResult
    failure: FailureClass | None
    reconciliation_required: bool
    telemetry: tuple[MeasuredValue, ...]
    telemetry_reason: Reason | None
    owner_drained: PredicateResult

    def _validate(self):
        _require(self.artifact.scope == self.scope, Reason.STALE)
        _require(bool(self.telemetry) == (self.telemetry_reason is None), Reason.MISSING)
        if self.status is TerminalStatus.COMPLETED:
            _require(self.task_goal.value is Truth.TRUE and self.checkpoint.value is Truth.TRUE
                     and self.owner_drained.value is Truth.TRUE and self.failure is None)
            _require(self.input_disposition is not InputDisposition.INPUT_UNCERTAIN,
                     Reason.RECONCILIATION)
        if self.input_disposition is InputDisposition.INPUT_UNCERTAIN:
            _require(self.reconciliation_required, Reason.RECONCILIATION)


# @anchor:wa.scripts.types.ExportReceipt
@_dataclass(frozen=True, slots=True)
class ExportReceipt(_Record):
    owner_reference: PrivateRef = _field(repr=False)
    source_observation: ObservationRef = _field(repr=False)
    scope: ScopeIdentity
    approved_field: str | None = _field(repr=False)
    pixel_region: tuple[int, int, int, int] | None = _field(repr=False)
    transform_chain: tuple[str, ...] = _field(repr=False)
    content_identity: str = _field(repr=False)
    destination: ExportDestination
    purpose: Purpose
    privacy_policy_revision: int

    def _validate(self):
        _require((self.approved_field is None) != (self.pixel_region is None))
        _require(self.scope == self.source_observation.scope, Reason.STALE)
        if self.pixel_region is not None:
            _require(self.pixel_region[2] > 0 and self.pixel_region[3] > 0, Reason.BOUNDS)


# @anchor:wa.scripts.types.Reservation
@_dataclass(frozen=True, slots=True)
class Reservation(_Record):
    owner: PrivateRef = _field(repr=False)
    scope: ScopeIdentity
    principal: str = _field(repr=False)
    operation: str
    ownership: Ownership
    ownership_reference: PrivateRef = _field(repr=False)
    peak_bytes: int
    absolute_deadline_ns: int
    policy_revision: int

    def _validate(self):
        _require(self.principal == self.scope.principal, Reason.FOREIGN)
        _require(self.peak_bytes > 0 and self.absolute_deadline_ns > 0, Reason.BOUNDS)


# @anchor:wa.scripts.types.CancellationPolicy
@_dataclass(frozen=True, slots=True)
class CancellationPolicy(_Record):
    acknowledgement_deadline_ns: int
    escalation_reference: str
    join_reference: str
    supervisor_boundary: str
    escalation_deadline_ns: int
    join_deadline_ns: int
    consumer_holder_ceiling: int
    consumer_byte_ceiling: int

    def _validate(self):
        _require(0 < self.acknowledgement_deadline_ns <= self.escalation_deadline_ns
                 <= self.join_deadline_ns, Reason.BOUNDS)
        _require(self.consumer_holder_ceiling > 0 and self.consumer_byte_ceiling > 0, Reason.BOUNDS)


# @anchor:wa.scripts.types.Holder
@_dataclass(frozen=True, slots=True)
class Holder(_Record):
    consumer_identity: str
    purpose: Purpose
    scope: ScopeIdentity
    retained_bytes: int
    holder_token: PrivateRef = _field(repr=False)
    absolute_deadline_ns: int
    cancellation_endpoint: PrivateRef = _field(repr=False)
    policy: CancellationPolicy

    def _validate(self):
        _require(self.absolute_deadline_ns > 0, Reason.BOUNDS)


# @anchor:wa.scripts.types.CancellationProof
@_dataclass(frozen=True, slots=True)
class CancellationProof(_Record):
    holder_token: PrivateRef = _field(repr=False)
    acknowledged: bool
    drained: bool
    supervisor_terminated: bool
    joined: bool
    evidence: PrivateRef | None = _field(repr=False)
    reason: Reason | None

    def _validate(self):
        complete = (self.acknowledged and self.drained) or (self.supervisor_terminated and self.joined)
        _require(complete == (self.evidence is not None), Reason.MISSING)
        _require(complete == (self.reason is None), Reason.MISSING)


# @anchor:wa.scripts.types.AcquisitionDisposition
class AcquisitionDisposition(_Enum):
    ACQUIRED = 'acquired'
    REFUSED = 'refused'
    CANCELLED = 'cancelled'


# @anchor:wa.scripts.types.AcquisitionResult
@_dataclass(frozen=True, slots=True)
class AcquisitionResult(_Record):
    observation: ObservationRef | None
    reason: Reason | None
    disposition: 'AcquisitionDisposition'
    ownership: Ownership | None
    owner_token: PrivateRef | None = _field(repr=False)
    ownership_reason: MissingReason | None
    input_disposition: InputDisposition

    def _validate(self):
        _present(self.observation, self.reason)
        _require(self.input_disposition is InputDisposition.NOT_SENT)
        _present(self.ownership, self.ownership_reason)
        _require((self.owner_token is None) == (self.ownership is None), Reason.MISSING)
        _require((self.disposition is AcquisitionDisposition.ACQUIRED)
                 == (self.observation is not None))
        if self.observation is not None:
            _require(self.ownership is not None, Reason.MISSING)


# @anchor:wa.scripts.types.AdmissionCheck
@_dataclass(frozen=True, slots=True)
class AdmissionCheck(_Record):
    result: Truth
    reason: Reason | None

    def _validate(self):
        _require(self.result is not Truth.UNKNOWN or self.reason is not None, Reason.MISSING)
        _require(self.result is not Truth.TRUE or self.reason is None)


# @anchor:wa.scripts.types.HostKeyVocabulary
@_dataclass(frozen=True, slots=True)
class HostKeyVocabulary(_Record):
    version: str
    symbols: tuple[str, ...]
    attestation: PrivateRef = _field(repr=False)

    def _validate(self):
        _require(bool(self.symbols), Reason.MISSING)
        _unique_names(self.symbols)


# @anchor:wa.scripts.types.ResourcePolicy
@_dataclass(frozen=True, slots=True)
class ResourcePolicy(_Record):
    host_bytes: int
    process_bytes: int
    emergency_reserve_bytes: int
    cross_episode_operations: int
    rate_window_ns: int
    image_bytes: int
    image_tokens: int
    cancellation: CancellationPolicy
    policy_revision: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if f.name != "cancellation"), Reason.BOUNDS)
        _require(self.emergency_reserve_bytes < self.host_bytes and self.process_bytes <= self.host_bytes, Reason.BOUNDS)


# @anchor:wa.scripts.types.PerformanceObjectives
@_dataclass(frozen=True, slots=True)
class PerformanceObjectives(_Record):
    tail_latency_ns: int
    tail_quantile_ppm: int

    def _validate(self):
        _require(self.tail_latency_ns > 0 and 0 < self.tail_quantile_ppm <= 1000000, Reason.BOUNDS)


# @anchor:wa.scripts.types.SourcePolicy
@_dataclass(frozen=True, slots=True)
class SourcePolicy(_Record):
    unicode_scalars: int
    utf8_bytes: int
    header_lines: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if type(getattr(self, f.name)) is int), Reason.BOUNDS)
        # Design ceilings [TARGET]; trusted callers must supply every value.
        _require(self.unicode_scalars <= 16384 and self.utf8_bytes <= 65536 and self.header_lines <= 64, Reason.BOUNDS)


# @anchor:wa.scripts.types.ParserPolicy
@_dataclass(frozen=True, slots=True)
class ParserPolicy(_Record):
    ast_nodes: int
    ast_depth: int
    predicate_nodes: int
    timeout_ms: int
    incremental_memory_bytes: int
    supervisor_reference: str

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if type(getattr(self, f.name)) is int), Reason.BOUNDS)
        # Design ceilings [TARGET]; trusted callers must supply every value.
        _require(self.ast_nodes <= 1024 and self.ast_depth <= 8 and self.predicate_nodes <= 32 and self.timeout_ms <= 1000 and self.incremental_memory_bytes <= 134217728, Reason.BOUNDS)


# @anchor:wa.scripts.types.SlotPolicy
@_dataclass(frozen=True, slots=True)
class SlotPolicy(_Record):
    slots: int
    input_slot_bytes: int
    harvest_result_bytes: int
    episode_input_bytes: int
    episode_result_bytes: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if type(getattr(self, f.name)) is int), Reason.BOUNDS)
        # Design ceilings [TARGET]; trusted callers must supply every value.
        _require(self.slots <= 32 and self.input_slot_bytes <= 4096 and self.harvest_result_bytes <= 16384, Reason.BOUNDS)


# @anchor:wa.scripts.types.StorePolicy
@_dataclass(frozen=True, slots=True)
class StorePolicy(_Record):
    artifact_bytes: int
    resident_artifacts: int
    catalogue_bytes: int
    pinned_generations: int
    static_meta_tokens: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if type(getattr(self, f.name)) is int), Reason.BOUNDS)
        # Design ceilings [TARGET]; trusted callers must supply every value.
        _require(self.artifact_bytes <= 16777216 and self.resident_artifacts <= 64 and self.catalogue_bytes <= 4194304 and self.pinned_generations <= 4 and self.static_meta_tokens <= 800, Reason.BOUNDS)


# @anchor:wa.scripts.types.ReplayPolicy
@_dataclass(frozen=True, slots=True)
class ReplayPolicy(_Record):
    support_bytes: int
    screening_jobs: int
    event_bytes: int
    collector_spools: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if type(getattr(self, f.name)) is int), Reason.BOUNDS)
        # Design ceilings [TARGET]; trusted callers must supply every value.
        _require(self.support_bytes <= 33554432 and self.screening_jobs <= 1 and self.event_bytes <= 8388608 and self.collector_spools <= 16, Reason.BOUNDS)


# @anchor:wa.scripts.types.ResolverPolicy
@_dataclass(frozen=True, slots=True)
class ResolverPolicy(_Record):
    distinct_terms: int
    candidates: int
    idle_grace_ns: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if type(getattr(self, f.name)) is int), Reason.BOUNDS)
        # Design ceilings [TARGET]; trusted callers must supply every value.
        _require(self.distinct_terms <= 16 and self.candidates <= 64 and self.idle_grace_ns <= 30000000000, Reason.BOUNDS)


# @anchor:wa.scripts.types.WaitPolicy
@_dataclass(frozen=True, slots=True)
class WaitPolicy(_Record):
    effect_verification_ms: int
    semantic_wait_ms: int
    acquisition_attempts: int
    absolute_deadline_ns: int
    quiet_window_ms: int
    settle_deadline_ms: int
    acquisition_spacing_ms: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if type(getattr(self, f.name)) is int), Reason.BOUNDS)
        # Design ceilings [TARGET]; trusted callers must supply every value.
        _require(self.effect_verification_ms <= 2000 and self.semantic_wait_ms <= 30000 and self.acquisition_attempts <= 64 and self.settle_deadline_ms <= 2000, Reason.BOUNDS)
        _require(self.quiet_window_ms >= 50 and self.acquisition_spacing_ms >= 20
                 and self.quiet_window_ms <= self.settle_deadline_ms, Reason.BOUNDS)  # [TARGET]


# @anchor:wa.scripts.types.ContextPolicy
@_dataclass(frozen=True, slots=True)
class ContextPolicy(_Record):
    z1_tokens: int
    z2_tokens: int
    z3_tokens: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if type(getattr(self, f.name)) is int), Reason.BOUNDS)
        # Design ceilings [TARGET]; trusted callers must supply every value.
        _require(self.z1_tokens <= 1024 and self.z2_tokens <= 2000 and self.z3_tokens <= 1200, Reason.BOUNDS)


# @anchor:wa.scripts.types.DisplayPolicy
@_dataclass(frozen=True, slots=True)
class DisplayPolicy(_Record):
    scanned_cards: int
    selected_cards: int
    prompt_rows: int
    recency_candidates: int
    recent_receipts: int
    step_tokens: int
    step_bytes: int
    failure_tokens: int
    failure_bytes: int
    resolver_tokens: int

    def _validate(self):
        _require(all(getattr(self, f.name) > 0 for f in _fields(self) if type(getattr(self, f.name)) is int), Reason.BOUNDS)
        # Design ceilings [TARGET]; trusted callers must supply every value.
        _require(self.scanned_cards <= 64 and self.selected_cards <= 8 and self.prompt_rows <= 30 and self.recency_candidates <= 15 and self.recent_receipts <= 3 and self.step_tokens <= 120 and self.step_bytes <= 4096 and self.failure_tokens <= 40 and self.failure_bytes <= 512 and self.resolver_tokens <= 300, Reason.BOUNDS)
        _require(self.selected_cards <= self.scanned_cards, Reason.BOUNDS)


# @anchor:wa.scripts.types.ContextRequest
@_dataclass(frozen=True, slots=True)
class ContextRequest(_Record):
    prefix_identity: MetadataDigest
    scope: ScopeIdentity
    current_authority: AuthorityRevision
    absolute_deadline_ns: int
    restart_evidence: MissingRef

    def _validate(self):
        _require(self.current_authority.scope == self.scope.scope_revision, Reason.STALE)
        _require(self.absolute_deadline_ns > 0, Reason.BOUNDS)


# @anchor:wa.scripts.types.FrameWaitPort
class FrameWaitPort(_Protocol):
    # @anchor:wa.scripts.types.FrameWaitPort.next_observation
    def next_observation(self, after_identity: str, absolute_deadline_ns: int,
                         cancel: Holder) -> AcquisitionResult:
        """Acquire a coherent committed observation or explicit refusal."""
        ...

    # @anchor:wa.scripts.types.FrameWaitPort.wait_until
    def wait_until(self, predicate: PredicateSpec, scope: ScopeIdentity,
                   budget: WaitPolicy, absolute_deadline_ns: int,
                   cancel: Holder) -> AcquisitionResult:
        ...


# @anchor:wa.scripts.types.ValidationRegistryPort
class ValidationRegistryPort(_Protocol):
    # @anchor:wa.scripts.types.ValidationRegistryPort.check
    def check(self, plan: ValidatedPlan, compiler: PrivateRef, identity: ArtifactIdentity,
              scope: ScopeIdentity, abis: ABISet) -> AdmissionCheck:
        """Exact retained object, owner incarnation, canonical bytes and ABI check."""
        ...


# @anchor:wa.scripts.types.BindingRegistryPort
class BindingRegistryPort(_Protocol):
    # @anchor:wa.scripts.types.BindingRegistryPort.check
    def check(self, binding: TargetBinding, observation: ObservationRef,
              scope: ScopeIdentity, authority: AuthorityRevision,
              owner: PrivateRef, generation: int) -> AdmissionCheck:
        """Check exact binding and witness custody against current publication."""
        ...


# @anchor:wa.scripts.types.AuthorityCheckPort
class AuthorityCheckPort(_Protocol):
    # @anchor:wa.scripts.types.AuthorityCheckPort.check_final
    def check_final(self, intent: StepIntent, scope: ScopeIdentity,
                    authority: AuthorityRevision, eligible: bool, stopped: bool,
                    now_ns: int, absolute_deadline_ns: int,
                    restart_evidence: MissingRef) -> AdmissionCheck:
        """Only under the shared final fence; a result is never a send permit."""
        ...


# @anchor:wa.scripts.types.SlotCapabilityPort
class SlotCapabilityPort(_Protocol):
    # @anchor:wa.scripts.types.SlotCapabilityPort.consume_input
    def consume_input(self, slot: SlotDescriptor, episode_id: str,
                      scope: ScopeIdentity, actuator: PrivateRef) -> PrivateRef:
        """Owner resolves opaque payload only inside trusted actuation."""
        ...


# @anchor:wa.scripts.types.ResultCapabilityPort
class ResultCapabilityPort(_Protocol):
    # @anchor:wa.scripts.types.ResultCapabilityPort.deliver
    def deliver(self, result: PrivateRef, episode_id: str, scope: ScopeIdentity,
                purpose: Purpose, consumer: PrivateRef) -> AdmissionCheck:
        ...


# @anchor:wa.scripts.types.ExportReceiptPort
class ExportReceiptPort(_Protocol):
    # @anchor:wa.scripts.types.ExportReceiptPort.check
    def check(self, receipt: ExportReceipt, source: ObservationRef,
              transform_chain: tuple[str, ...], content_identity: str,
              destination: 'ExportDestination', purpose: Purpose,
              privacy_policy_revision: int) -> AdmissionCheck:
        """Owner registration and full source/destination provenance required."""
        ...


# @anchor:wa.scripts.types.ReservationPort
class ReservationPort(_Protocol):
    # @anchor:wa.scripts.types.ReservationPort.reserve
    def reserve(self, request: 'ReservationRequest', policy: ResourcePolicy) -> 'ReservationOutcome':
        """Return an owner-minted retained grant or explicit closed refusal."""
        ...

    # @anchor:wa.scripts.types.ReservationPort.grow
    def grow(self, grant: 'ReservationGrant', request: 'ReservationRequest',
             policy: ResourcePolicy) -> 'ReservationOutcome':
        """Check exact retained grant, scope, owners and accounting revision.

        Success replaces the grant under its exact reservation token; refusal
        preserves the prior charge and lifetime.
        """
        ...

    # @anchor:wa.scripts.types.ReservationPort.release_unused
    def release_unused(self, grant: 'ReservationGrant', unused_bytes: int,
                       proof: 'CancellationResult') -> AdmissionCheck:
        """Verify retained grant identity and authenticated exact-token evidence.

        proof.released_tokens must identify grant.reservation by object identity;
        timeout or equal reconstructed tokens cannot release ownership.
        Evidence must bind the exact originating cancellation request, holder,
        scope and grant; cross-holder or superseded requests are refused.
        Deduplicate successful releases by holder, cancellation request identity,
        grant and amount, independently of completion proof object identity.
        Retries with fresh authenticated evidence return the original result
        without refunding twice; a changed amount for that request/grant or
        reconstructed grants/tokens are refused.
        Performed work stays charged.
        """
        ...


# @anchor:wa.scripts.types.CancellationPort
class CancellationPort(_Protocol):
    # @anchor:wa.scripts.types.CancellationPort.request
    def request(self, holder: 'HolderContract') -> 'CancellationResult':
        ...

    # @anchor:wa.scripts.types.CancellationPort.verify
    def verify(self, holder: 'HolderContract', proof: 'CancellationResult') -> AdmissionCheck:
        """No timeout release: retain ownership until drain or termination/join."""
        ...


# @anchor:wa.scripts.types.record_from_entries
def record_from_entries(record_type, entries):
    """Strict named ingress: preserves duplicates and gives closed key errors.

    Does not grant authority, coerce numbers, freeze nested mutable values or
    declassify private values. Direct constructors and this adapter both use
    bounded closed reasons for malformed call signatures.
    """
    _require(isinstance(record_type, type) and record_type in globals().values()
             and record_type is not _Record and issubclass(record_type, _Record), Reason.TYPE)
    fields = tuple(field for field in _fields(record_type) if field.init)
    allowed = {field.name for field in fields}
    required = {field.name for field in fields if field.default is _MISSING
                and field.default_factory is _MISSING}
    max_key_length = max(map(len, allowed), default=0)
    names, values = set(), {}
    for count, entry in enumerate(entries, start=1):
        _require(count <= len(allowed), Reason.SCHEMA)
        _require(type(entry) in (tuple, list) and len(entry) == 2)
        name, value = entry
        _require(type(name) is str, Reason.TYPE)
        # Schema-derived bounds precede Unicode encoding/normalization/storage.
        _require(len(name) <= max_key_length, Reason.SCHEMA)
        _require(name in allowed, Reason.SCHEMA)
        normalized = _normalize('NFKC', name).casefold()
        _require(normalized not in names and name not in values)
        names.add(normalized)
        values[name] = value
    _require(required <= set(values))
    return record_type(**values)


_CandidateRef = _TypeVar('_CandidateRef', contravariant=True)
_ScriptLease = _TypeVar('_ScriptLease', contravariant=True)
_CatalogueSnapshot = _TypeVar('_CatalogueSnapshot', contravariant=True)
_RunEvent = _TypeVar('_RunEvent', contravariant=True)
_EpisodeSeal = _TypeVar('_EpisodeSeal', contravariant=True)
_DreamCertificate = _TypeVar('_DreamCertificate', contravariant=True)


# @anchor:wa.scripts.types.OwnerExchangePort
class OwnerExchangePort(_Protocol[_CandidateRef, _ScriptLease, _CatalogueSnapshot,
                                  _RunEvent, _EpisodeSeal, _DreamCertificate]):
    """B injects its authoritative types and retained-object owner checks.

    A successful check supplies evidence only, never an input/send permit.
    Implementations enforce the exchange invariants documented above.
    """
    # @anchor:wa.scripts.types.OwnerExchangePort.check_candidate
    def check_candidate(self, candidate: _CandidateRef, artifact: ArtifactIdentity,
                        scope: ScopeIdentity, lifecycle_revision: int) -> AdmissionCheck:
        ...

    # @anchor:wa.scripts.types.OwnerExchangePort.check_lease
    def check_lease(self, lease: _ScriptLease, artifact: ArtifactIdentity,
                    scope: ScopeIdentity, episode_id: str,
                    process_incarnation: str, retained_owner: PrivateRef) -> AdmissionCheck:
        ...

    # @anchor:wa.scripts.types.OwnerExchangePort.check_snapshot
    def check_snapshot(self, snapshot: _CatalogueSnapshot, scope: ScopeIdentity,
                       generation: int, digest: MetadataDigest,
                       current_deny_sequence: int) -> AdmissionCheck:
        ...

    # @anchor:wa.scripts.types.OwnerExchangePort.check_event
    def check_event(self, event: _RunEvent, artifact: ArtifactIdentity,
                    scope: ScopeIdentity, episode_id: str, event_index: int) -> AdmissionCheck:
        ...

    # @anchor:wa.scripts.types.OwnerExchangePort.check_seal
    def check_seal(self, seal: _EpisodeSeal, artifact: ArtifactIdentity,
                   scope: ScopeIdentity, episode_id: str,
                   terminal_goal: TerminalGoalRef, authority: AuthorityRevision) -> AdmissionCheck:
        ...

    # @anchor:wa.scripts.types.OwnerExchangePort.check_certificate
    def check_certificate(self, certificate: _DreamCertificate, artifact: ArtifactIdentity,
                          scope: ScopeIdentity, authority: AuthorityRevision,
                          support_identity: PrivateRef) -> AdmissionCheck:
        ...


# Spec names share the original frozen vocabulary, without parallel wire enums.
# @anchor:wa.scripts.types.TruthValue
TruthValue = Truth
# @anchor:wa.scripts.types.PredicateOp
PredicateOp = PredicateOperator
# @anchor:wa.scripts.types.StepKind
StepKind = ActionKind


# @anchor:wa.scripts.types.truth_not
def truth_not(value: TruthValue) -> TruthValue:
    _require(type(value) is Truth, Reason.TYPE)
    return {Truth.TRUE: Truth.FALSE, Truth.FALSE: Truth.TRUE, Truth.UNKNOWN: Truth.UNKNOWN}[value]


# @anchor:wa.scripts.types.truth_all
def truth_all(values: tuple[TruthValue, ...]) -> TruthValue:
    _require(type(values) is tuple and all(type(v) is Truth for v in values), Reason.TYPE)
    _require(bool(values), Reason.MISSING)
    return Truth.FALSE if Truth.FALSE in values else Truth.UNKNOWN if Truth.UNKNOWN in values else Truth.TRUE


# @anchor:wa.scripts.types.truth_any
def truth_any(values: tuple[TruthValue, ...]) -> TruthValue:
    _require(type(values) is tuple and all(type(v) is Truth for v in values), Reason.TYPE)
    _require(bool(values), Reason.MISSING)
    return Truth.TRUE if Truth.TRUE in values else Truth.UNKNOWN if Truth.UNKNOWN in values else Truth.FALSE


# @anchor:wa.scripts.types.RegistryRef
@_dataclass(frozen=True, slots=True, repr=False)
class RegistryRef(_Record):
    kind: str
    owner_incarnation: str
    generation: int
    handle_identity: PrivateRef = _field(repr=False)


# @anchor:wa.scripts.types.FactEvidence
@_dataclass(frozen=True, slots=True)
class FactEvidence(_Record):
    value: TruthValue
    source: EvidenceSource
    source_schema: str
    source_generation: int | None
    observation: ObservationRef = _field(repr=False)
    association: MissingRef = _field(repr=False)
    reason: MissingReason | None

    def _validate(self):
        _require((self.value is Truth.UNKNOWN) == (self.reason is not None), Reason.MISSING)
        _require(self.source_generation == self.observation.producer_generation, Reason.STALE)
        _require(self.source_schema == self.observation.producer_schema, Reason.STALE)
        if self.association.value is not None:
            _require(self.association == self.observation.association, Reason.STALE)
        if self.value is not Truth.UNKNOWN:
            _require(self.observation.association.value is not None, Reason.MISSING)
            _require(self.source_generation is not None and self.association.value is not None
                     and self.observation.completeness.value is not None, Reason.MISSING)


# @anchor:wa.scripts.types.ReservationRequest
@_dataclass(frozen=True, slots=True)
class ReservationRequest(_Record):
    owner: PrivateRef = _field(repr=False)
    consumer: PrivateRef = _field(repr=False)
    scope: ScopeIdentity
    operation: str
    peak_bytes: int
    retained_owners: tuple[PrivateRef, ...] = _field(repr=False)
    shared_owners: tuple[PrivateRef, ...] = _field(repr=False)
    accounting_revision: int
    absolute_deadline_ns: int

    def _validate(self):
        _require(self.peak_bytes > 0 and self.absolute_deadline_ns > 0, Reason.BOUNDS)
        _unique(self.retained_owners + self.shared_owners)


# @anchor:wa.scripts.types.ReservationGrant
@_dataclass(frozen=True, slots=True)
class ReservationGrant(_Record):
    request: ReservationRequest
    reservation: RegistryRef = _field(repr=False)
    charged_bytes: int
    accounting_revision: int
    issued_ns: int
    absolute_deadline_ns: int

    def _validate(self):
        _require(self.charged_bytes >= self.request.peak_bytes, Reason.BOUNDS)
        _require(self.accounting_revision == self.request.accounting_revision, Reason.STALE)
        _require(self.issued_ns < self.absolute_deadline_ns <= self.request.absolute_deadline_ns, Reason.BOUNDS)


# @anchor:wa.scripts.types.ReservationOutcome
@_dataclass(frozen=True, slots=True)
class ReservationOutcome(_Record):
    """Structural result only; the port owner authenticates grant membership."""
    grant: ReservationGrant | None = _field(repr=False)
    reason: Reason | None

    def _validate(self):
        _present(self.grant, self.reason)


# @anchor:wa.scripts.types.HolderContract
@_dataclass(frozen=True, slots=True)
class HolderContract(_Record):
    consumer_identity: str
    purpose: Purpose
    scope: ScopeIdentity
    retained_bytes: int
    absolute_deadline_ns: int
    cancellation_endpoint: RegistryRef = _field(repr=False)
    supervisor_boundary: RegistryRef = _field(repr=False)
    policy: CancellationPolicy

    def _validate(self):
        _require(0 < self.retained_bytes <= self.policy.consumer_byte_ceiling, Reason.BOUNDS)
        _require(self.absolute_deadline_ns > 0, Reason.BOUNDS)


# @anchor:wa.scripts.types.CancellationResult
@_dataclass(frozen=True, slots=True)
class CancellationResult(_Record):
    requested: bool
    acknowledged: bool
    terminated: bool
    joined: bool
    drained: bool
    released_tokens: tuple[RegistryRef, ...] = _field(repr=False)
    evidence: RegistryRef | None = _field(repr=False)
    reason: MissingReason | None

    def _validate(self):
        _require(not (self.acknowledged or self.terminated or self.joined or self.drained) or self.requested)
        _require(not self.joined or self.terminated)
        _unique(self.released_tokens)
        if self.released_tokens:
            _require(self.drained and (self.acknowledged or (self.terminated and self.joined))
                     and self.evidence is not None and self.reason is None, Reason.MISSING)
        _require((self.evidence is None) == (self.reason is not None), Reason.MISSING)


# @anchor:wa.scripts.types.FailureClassification
@_dataclass(frozen=True, slots=True)
class FailureClassification(_Record):
    episode_id: str
    scope: ScopeIdentity
    environment: str
    cause: FailureClass
    evidence: tuple[RegistryRef, ...] = _field(repr=False)
    lifecycle_revision: int

    def _validate(self):
        _require(self.cause is FailureClass.UNCLASSIFIED or bool(self.evidence), Reason.MISSING)


# @anchor:wa.scripts.types.RestartBarrierView
@_dataclass(frozen=True, slots=True)
class RestartBarrierView(_Record):
    closed: bool
    interrupted_scope: ScopeIdentity
    unresolved_actions: tuple[RegistryRef, ...] = _field(repr=False)
    reconciliation: RegistryRef | None = _field(repr=False)
    reason: MissingReason | None

    def _validate(self):
        _present(self.reconciliation, self.reason)
        if not self.closed:
            _require(not self.unresolved_actions and self.reconciliation is not None, Reason.RECONCILIATION)


# @anchor:wa.scripts.types.PlanDefinition
@_dataclass(frozen=True, slots=True)
class PlanDefinition(_Record):
    """Immutable description; no compiler registration or execution authority."""
    header: ScriptHeader
    nodes: tuple[StepPlan, ...]
    selectors: tuple[TargetSelector, ...]
    symbols: tuple[ResultDeclaration, ...]
    slots: tuple[ArgSpec, ...]
    entry: PredicateSpec
    terminal_goal: TerminalGoalRef
    limits: PlanLimits
    identity: ArtifactIdentity
    structural_screen_digest: PlanDigest
    source_digest: PlanDigest
    queries: tuple[QueryDeclaration, ...] = ()

    def _validate(self):
        _require(self.header.arguments == self.slots and self.header.limits == self.limits)
        _require(self.header.entry == self.entry, Reason.CONFLICT)
        _require(self.header.terminal_goal == self.terminal_goal.reference)
        _unique(self.selectors)
        _require(all(node.effect is None or node.effect.selector in self.selectors for node in self.nodes), Reason.FOREIGN)
        _require(self.identity.scope == self.header.scope and self.identity.abis == self.header.abis, Reason.STALE)
        _require(self.identity.logical_id == self.header.logical_id and self.identity.revision == self.header.revision)
        _require(self.identity.goal_contract == self.header.goal_contract
                 and self.identity.signature_schema == self.header.signature_contract, Reason.STALE)
        _require(self.header.supplied_pins is None or self.header.supplied_pins == self.identity, Reason.STALE)
        _require(all(query.selector in self.selectors for query in self.queries), Reason.FOREIGN)
        _require(self.identity.source_digest == self.source_digest
                 and self.identity.structural_screen_digest == self.structural_screen_digest)
        _plan_structure(self.nodes, self.symbols, self.slots, self.entry,
                        self.terminal_goal, self.limits, self.header.scope, self.queries)


# @anchor:wa.scripts.types.CanonicalPlanAssociation
@_dataclass(frozen=True, slots=True)
class CanonicalPlanAssociation(_Record):
    plan: PlanDefinition
    canonical_bytes: bytes = _field(repr=False)
    compiler: RegistryRef = _field(repr=False)

    def _validate(self):
        _require(0 < len(self.canonical_bytes) <= self.plan.limits.canonical_bytes, Reason.BOUNDS)
        # S1 checks exact canonical bytes and pins against its retained registry.


# @anchor:wa.scripts.types.ClockPort
class ClockPort(_Protocol):
    # @anchor:wa.scripts.types.ClockPort.now_ns
    def now_ns(self) -> int:
        ...


# @anchor:wa.scripts.types.CancellationStatePort
class CancellationStatePort(_Protocol):
    # @anchor:wa.scripts.types.CancellationStatePort.is_cancelled
    def is_cancelled(self, cancellation: RegistryRef, scope: ScopeIdentity) -> bool:
        ...


# @anchor:wa.scripts.types.StepAuthorityPort
class StepAuthorityPort(_Protocol[_ScriptLease]):
    """Host input fence precedes the short store check; callbacks run outside it."""
    # @anchor:wa.scripts.types.StepAuthorityPort.begin_step
    def begin_step(self, lease: _ScriptLease, intent: StepIntent,
                   scope: ScopeIdentity, authority: AuthorityRevision) -> RegistryRef:
        ...

    # @anchor:wa.scripts.types.StepAuthorityPort.end_step
    def end_step(self, permit: RegistryRef, result: StepResult,
                 scope: ScopeIdentity, authority: AuthorityRevision) -> AdmissionCheck:
        ...

    # @anchor:wa.scripts.types.StepAuthorityPort.release
    def release(self, lease: _ScriptLease, retained_token: RegistryRef) -> AdmissionCheck:
        ...


# @anchor:wa.scripts.types.EventAuthorityPort
class EventAuthorityPort(_Protocol[_RunEvent, _EpisodeSeal]):
    # @anchor:wa.scripts.types.EventAuthorityPort.reserve
    def reserve(self, intent: StepIntent, terminal_bytes: int, refusal_bytes: int,
                scope: ScopeIdentity, authority: AuthorityRevision) -> RegistryRef:
        ...

    # @anchor:wa.scripts.types.EventAuthorityPort.append
    def append(self, reservation: RegistryRef, event: _RunEvent) -> AdmissionCheck:
        ...

    # @anchor:wa.scripts.types.EventAuthorityPort.acknowledge_seal
    def acknowledge_seal(self, seal: _EpisodeSeal, scope: ScopeIdentity) -> AdmissionCheck:
        ...
