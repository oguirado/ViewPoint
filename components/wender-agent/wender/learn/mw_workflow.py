# @module {"id":"wa.learn.mw_workflow","layer":4,"owner":"LEARN","deps":["wa.tools.memoryweb_gate","wa.learn.demo_replay","wa.learn.demonstrations","wa.learn.script_live","wa.scripts.types","wa.perceive.srf_identity","wa.learn.mw_guidance"],"book":"HB-WA-s4-learn"}
"""Declarative, owner-bound workflow execution; corpus content grants no authority.

Origin: MEMORYWEB_PROTOCOL.md and MWP3. All ports are trusted Python composition,
not plugin names from JSON. There is deliberately no production owner factory.
Unimplemented transports/verifiers refuse before input. Pixel ownership is RAM-only.
"""
from dataclasses import dataclass, field, fields, replace
import hashlib
from pathlib import Path
from types import MappingProxyType

from tools import memoryweb_gate as gate
from wender.learn.demo_replay import locate, verify_change
from wender.learn.demonstrations import validate_text_slot
from wender.learn.script_live import LegacyDispatchOwner
from wender.scripts.types import AdmissionCheck, Truth, Reason
from wender.perceive.srf_identity import SrfIdentitySurface
from wender.learn import mw_guidance as guidance


# @anchor:wa.learn.mw_workflow.WorkflowLimits
@dataclass(frozen=True)
class WorkflowLimits:
    workflow_bytes: int
    steps: int
    snapshots: int
    acquisitions: int
    retained_bytes: int
    peak_bytes: int
    event_bytes: int
    event_count: int
    journal_bytes: int
    slot_bytes: int
    aggregate_slot_bytes: int
    cancel_ns: int
    drain_ns: int
    no_progress_ns: int
    deadline_ns: int


# @anchor:wa.learn.mw_workflow.ObservationValue
@dataclass(frozen=True)
class ObservationValue:
    scope: object
    authority: object
    publication: str
    capture_start_ns: int
    capture_end_ns: int
    capability_id: str
    capability_revision: str
    output_kind: str
    unit: str
    value: object
    complete: bool
    missing_reason: object


# @anchor:wa.learn.mw_workflow.CapturedSnapshot
@dataclass(frozen=True)
class CapturedSnapshot:
    name: str
    observation: ObservationValue
    retained_bytes: int


# @anchor:wa.learn.mw_workflow.GroundedTarget
@dataclass(frozen=True)
class GroundedTarget:
    point: tuple
    destination: object
    observation: ObservationValue
    scope: object
    authority: object
    applicability: AdmissionCheck
    support_owner: object
    expires_ns: int
    translation: tuple


# @anchor:wa.learn.mw_workflow.PinnedArtifact
@dataclass(frozen=True)
class PinnedArtifact:
    path: Path
    sha256: str
    immutable: AdmissionCheck


# @anchor:wa.learn.mw_workflow.PrivateSlot
@dataclass(frozen=True)
class PrivateSlot:
    """Ephemeral host memory, never serialized or included in exception messages."""
    value: str = field(repr=False)
    initial_length: int
    scope: object
    authority: object
    transport: str
    admission: AdmissionCheck


# @anchor:wa.learn.mw_workflow.RunResult
@dataclass(frozen=True)
class RunResult:
    rc: int
    verdict_code: str
    summary: object = None
    diagnostic: str | None = None


# @anchor:wa.learn.mw_workflow.NativeReadFailure
@dataclass
class NativeReadFailure(Exception):
    component: str
    operation: str
    code: int
    reason: str
    observed_human_boundary: bool = False


# @anchor:wa.learn.mw_workflow.IdentityPlan
@dataclass(frozen=True)
class IdentityPlan:
    operation: str
    payload: object
    admission: AdmissionCheck


def _identity_payload(surface, plan, action, target):
    """Validate and defensively copy a plan without crossing the input boundary."""
    _need(isinstance(surface, SrfIdentitySurface) and type(plan) is IdentityPlan and
          _admitted(plan.admission) and type(plan.payload) is dict, 'OUT_OF_SUPPORT')
    operation, payload = plan.operation, dict(plan.payload)
    _need(operation in ('click', 'key', 'scroll', 'drag') and operation in surface.mapping, 'OUT_OF_SUPPORT')
    expected = {'click': 'click', 'right_click': 'click', 'key': 'key', 'scroll': 'scroll', 'drag': 'drag'}
    _need(expected.get(action['kind']) == operation, 'OUT_OF_SUPPORT')
    allowed = {'click': {'point', 'button'}, 'key': {'keysym'}, 'scroll': {'point', 'dir', 'modifier'}, 'drag': {'span', 'button'}}
    _need(not set(payload) - allowed[operation], 'OUT_OF_SUPPORT')
    if operation == 'click':
        _need(target is not None and payload.get('point') == ','.join(map(str, target.point)) and
              type(payload.get('button')) is int and payload['button'] in (1, 2, 3), 'OUT_OF_SUPPORT')
        if action['kind'] == 'right_click':
            _need(payload['button'] == 3, 'OUT_OF_SUPPORT')
    if operation == 'key':
        _need(payload.get('keysym') == action['key_name'], 'OUT_OF_SUPPORT')
    if operation in ('scroll', 'drag'):
        _need(type(target) is GroundedTarget, 'OUT_OF_SUPPORT')
        start = ','.join(map(str, target.point))
        if operation == 'scroll':
            _need(payload.get('point') == start, 'OUT_OF_SUPPORT')
        else:
            if target.destination is not None:
                endpoint = target.destination
            else:
                _need(type(action['dx_px']) is int and type(action['dy_px']) is int, 'OUT_OF_SUPPORT')
                endpoint = (target.point[0] + action['dx_px'], target.point[1] + action['dy_px'])
            _need(payload.get('span') == start + ':' + ','.join(map(str, endpoint)), 'OUT_OF_SUPPORT')
    _need(all(type(v) in (str, int) for v in payload.values()), 'OUT_OF_SUPPORT')
    return IdentityPlan(operation, payload, plan.admission)


def _identity_dispatch(surface, prepared, action):
    result = surface._call(prepared.operation, **prepared.payload)
    receipt = surface._validated_input(result, action['kind'])
    return receipt['input_mono_ns']


def _identity_send(surface, plan, action, target):
    return _identity_dispatch(surface, _identity_payload(surface, plan, action, target), action)


# @anchor:wa.learn.mw_workflow.IdentityTransport
@dataclass(frozen=True)
class IdentityTransport:
    """Explicit existing attachment and registered action planner; no text argv.

    planner(action,target) returns IdentityPlan and attests current frame identity,
    authority, key policy, endpoint bounds and exact native displacement semantics
    under the caller's LegacyDispatchOwner lock. There is no private-pipe method;
    private slots therefore cannot use the installed identity CLI's type argv.
    """
    surface: SrfIdentitySurface
    planner: object

    # @anchor:wa.learn.mw_workflow.IdentityTransport.send
    def send(self, action, target):
        return _identity_send(self.surface, self.planner(action, target), action, target)


# @anchor:wa.learn.mw_workflow.RunOwners
@dataclass(frozen=True)
class RunOwners:
    """Explicit ports; each authorization returns exact AdmissionCheck(TRUE, None).

    artifact.pin(path) is a context manager yielding PinnedArtifact; check(pin)
    attests custody throughout the lease. authorization.check(phase, workflow,
    scope, authority) checks lease, notice, go-ahead, effects and restart; inventory
    returns the frozen required-segment mapping. observations.acquire(predicate,
    deadline) and validate(value) attest fresh scoped publications. capabilities
    supports(workflow), target(target, state, deadline), validate_target(...),
    verifier(spec), equivalent(a,b), changed(predicate,baseline,current), focus(...),
    action(action,target) and final(workflow) attest installed exact contracts.
    resources.reserve(kind, bytes), cancel(timeout), drain(timeout) retain ownership
    until verified release. journal exposes read, digest, authenticate, recover,
    reserve, append, stage, validate, select; stage writes inactive metadata only.
    journal.context() supplies authenticated real-gate inventory callbacks.
    ram.t0(target, instance, state, deadline) returns frame, descriptor, publication,
    custody admission; ram.change(spec,state,deadline) returns before, after,
    expected and custody admission. transport.send(action,target) never retries.
    slots.read(slot,state,max_bytes) returns PrivateSlot from RAM/private pipe.
    slots.admit(slot,state) attests current chip grounding/order, empty-editor,
    focus, scoped history policy, residue recovery and owned clear support.
    slots.prepare(slot,payload,state), clear(slot), cleared(slot) are required for
    owned clipboard transport. slots.verify(slot,state,length,initial) attests
    actual accepted content. slot_length_bindings selects independent integer
    observations; the registered owner must define each unit as Python Unicode
    code-point count. This trusted invocation binding is not workflow content.
    transport.send_private(action,target,payload) must use an inherited private
    pipe, never text argv/environment/files. The identity CLI has no such port.
    """
    artifact: object
    validation_context: gate.ValidationContext
    authorization: object
    capabilities: object
    observations: object
    ram: object
    transport: object
    slots: object
    resources: object
    journal: object
    dispatch: LegacyDispatchOwner
    surface: object
    replay_policy: object
    clock: object
    sleep: object
    limits: WorkflowLimits
    scope: object
    authority: object
    run_id: str
    mode: str
    output: Path
    slot_length_bindings: tuple = ()
    guidance: object | None = None


@dataclass(frozen=True)
class _SlotLengthBinding:
    """Trusted observation selector whose registered unit counts Unicode code points."""
    slot_id: str
    predicate: dict
    capability_revision: str
    unit: str


def _descriptor_bound(value, budget, depth=0):
    _need(depth < 16 and budget > 0, 'OUT_OF_SUPPORT')
    if value is None or type(value) in (bool, int):
        _need(type(value) is not int or value.bit_length() <= 64, 'OUT_OF_SUPPORT')
        return 24
    if type(value) is str:
        _need(len(value) <= budget // 6, 'OUT_OF_SUPPORT')
        return 6 * len(value) + 2
    _need(type(value) in (dict, list) and len(value) <= budget // 2, 'OUT_OF_SUPPORT')
    used = 2
    for k in value:
        if type(value) is dict:
            _need(type(k) is str, 'OUT_OF_SUPPORT')
            used += _descriptor_bound(k, budget - used, depth + 1) + 1
            item = value[k]
        else:
            item = k
        used += _descriptor_bound(item, budget - used, depth + 1) + 1
        _need(used <= budget, 'OUT_OF_SUPPORT')
    return used


def _length_predicate_shape(predicate):
    if not all(type(ref) is str and 0 < len(ref) <= 400 for ref in predicate['evidence_refs']):
        return False
    for binding in predicate['bindings']:
        if type(binding) is not dict or set(binding) != {'name', 'type', 'value'}:
            return False
        name, kind, value = binding['name'], binding['type'], binding['value']
        if (type(name) is not str or not 0 < len(name) <= 63 or
                name[0] not in 'abcdefghijklmnopqrstuvwxyz0123456789' or
                any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in name)):
            return False
        integer = lambda v: type(v) is int and -2147483648 <= v <= 2147483647
        if not ((kind == 'integer' and integer(value)) or
                (kind == 'boolean' and type(value) is bool) or
                (kind == 'string' and type(value) is str and 0 < len(value) <= 400) or
                (kind == 'integer_box' and type(value) is list and len(value) == 4 and all(integer(v) for v in value))):
            return False
    return True


def _length_bindings(state, owners):
    bindings = owners.slot_length_bindings
    slots = state.workflow['text_slots']
    _need(type(bindings) is tuple and len(bindings) <= len(slots), 'OUT_OF_SUPPORT')
    for slot in slots:
        matches = [b for b in bindings if type(b) is _SlotLengthBinding and b.slot_id == slot['slot_id']]
        _need(len(matches) == 1, 'OUT_OF_SUPPORT')
        b = matches[0]
        p = b.predicate
        _need(type(p) is dict and set(p) == {'kind', 'capability_id', 'bindings', 'evidence_refs', 'comparison', 'snapshot_ref'} and
              p.get('comparison') == 'truth' and p.get('snapshot_ref') is None, 'OUT_OF_SUPPORT')
        _need(all(type(v) is str and 0 < len(v) <= owners.limits.workflow_bytes for v in
                  (b.slot_id, b.capability_revision, b.unit, p.get('capability_id'))) and
              p.get('kind') in ('state_signal', 'side_effect', 'mark_label', 'region_change', 'capability', 'authorized_scope') and
              type(p.get('bindings')) is list and type(p.get('evidence_refs')) is list, 'OUT_OF_SUPPORT')
        descriptor = dict(slot_id=b.slot_id, predicate=p, revision=b.capability_revision, unit=b.unit)
        bound = _descriptor_bound(descriptor, owners.limits.workflow_bytes)
        _need(len(p['capability_id']) <= 80 and _length_predicate_shape(p), 'OUT_OF_SUPPORT')
        _need(state.retained + bound <= owners.limits.retained_bytes, 'OUT_OF_SUPPORT')
        _reserve(owners, 'length_descriptor', bound * 3)
        raw = gate.canon(descriptor).encode()
        _need(len(raw) <= owners.limits.workflow_bytes, 'OUT_OF_SUPPORT')
        state.retained += len(raw)
        state.length_bindings[b.slot_id] = _SlotLengthBinding(b.slot_id, gate.strict_load(gate.canon(p).encode()), b.capability_revision, b.unit)


# @anchor:wa.learn.mw_workflow.RunState
@dataclass
class RunState:
    raw: bytes
    workflow_sha256: str
    workflow: dict
    scope: object
    authority: object
    required_segments: object
    snapshots: dict = field(default_factory=dict)
    classifications: dict = field(default_factory=dict)
    current_step: object = None
    acquisitions: int = 0
    retained: int = 0
    journal_seq: int = 0
    journal_prev: object = None
    journal_bytes: int = 0
    uncertain: bool = False
    last_progress_ns: int = 0
    busy_transition: bool = False
    journal: object = None
    pin: object = None
    slot_bytes: int = 0
    native_results: list = field(default_factory=list)
    last_input_ns: int | None = None
    completed_steps: dict = field(default_factory=dict)
    journal_ready: bool = False
    resume_blocked: bool = False
    length_bindings: dict = field(default_factory=dict)
    chip_keys: dict = field(default_factory=dict)
    guidance_refs: list = field(default_factory=list)
    guidance_pairs: set = field(default_factory=set)
    guidance_frames: list = field(default_factory=list)
    guidance_events: int = 0
    guidance_reperceptions: int = 0
    guidance_bytes: int = 0


# @anchor:wa.learn.mw_workflow.StepState
@dataclass
class StepState:
    seq: int
    run: RunState
    deadline_ns: int
    locate_deadline_ns: int
    verify_deadline_ns: int
    iteration: int = 0
    phase: str = 'precondition'
    busy_seen: bool = False
    consecutive: int = 0
    input_ns: int | None = None
    dispatch_disposition: str = 'NOT_SENT'
    classification: str = 'not_reached'
    baseline: object = None
    target: object = None
    start_ns: int | None = None
    guidance_start_ns: int | None = None
    guidance_baseline: object = None
    guidance_quiet_start: int | None = None
    guidance_quiet_boundary: int | None = None
    guidance_poll_ns: int | None = None


# @anchor:wa.learn.mw_workflow.LocationFailure
class LocationFailure(ValueError):
    """Closed primary-location refusal; no private diagnostic details."""
    # @anchor:wa.learn.mw_workflow.LocationFailure.__init__
    def __init__(self, code):
        if type(code) is not str or code not in frozenset((
                'LOW_CONFIDENCE', 'AMBIGUOUS_TARGET', 'LAYOUT_INVARIANT')):
            raise ValueError('OUT_OF_SUPPORT') from None
        self.code = code
        super().__init__(code)


class _Stop(Exception):
    def __init__(self, code, rc=5, diagnostic=None):
        self.code, self.rc, self.diagnostic = code, rc, diagnostic or code


def _need(ok, code='OUT_OF_SUPPORT', rc=5):
    if not ok:
        raise _Stop(code, rc)


def _admitted(value):
    return type(value) is AdmissionCheck and value.result is Truth.TRUE and value.reason is None


def _passed(value):
    return type(value) is gate.GateResult and type(value.rc) is int and value.rc == 0 and value.category == 'PASS'


def _time(owners, deadline=None):
    now = owners.clock()
    _need(type(now) is int and now >= 0, 'OUT_OF_SUPPORT')
    _need(now < min(owners.limits.deadline_ns, deadline if deadline is not None else owners.limits.deadline_ns), 'DEADLINE')
    return now


def _pause(owners, ns, deadline):
    now = _time(owners, deadline)
    owners.sleep(min(ns, deadline - now, owners.limits.deadline_ns - now))
    _time(owners, deadline)


def _valid_observation(value, owners):
    if type(value) is not ObservationValue:
        return False
    now = owners.clock()
    validation = owners.observations.validate(value)
    _need(not (type(validation) is AdmissionCheck and validation.reason in
               (Reason.PRIVACY, Reason.SCHEMA, Reason.TYPE, Reason.BOUNDS)), 'OUT_OF_SUPPORT')
    return (value.scope == owners.scope and value.authority == owners.authority and
            value.complete is True and value.missing_reason is None and
            type(value.capture_start_ns) is int and type(value.capture_end_ns) is int and
            0 <= value.capture_start_ns <= value.capture_end_ns <= now and
            type(value.publication) is str and bool(value.publication) and
            type(value.capability_revision) is str and bool(value.capability_revision) and
            _admitted(validation))


def _compatible(a, b):
    return all(getattr(a, k) == getattr(b, k) for k in
               ('scope', 'authority', 'capability_id', 'capability_revision', 'output_kind', 'unit'))


# @anchor:wa.learn.mw_workflow.evaluate_predicate
def evaluate_predicate(predicate, *, observation, snapshots, owners):
    """Three-valued comparisons: missing, stale and ill-typed evidence is UNKNOWN."""
    if not _valid_observation(observation, owners) or observation.capability_id != predicate['capability_id']:
        return Truth.UNKNOWN
    value, comparison = observation.value, predicate['comparison']
    if comparison == 'truth':
        return (Truth.TRUE if value else Truth.FALSE) if type(value) is bool and observation.output_kind == 'boolean' else Truth.UNKNOWN
    baseline = snapshots.get(predicate['snapshot_ref'])
    if type(baseline) is not CapturedSnapshot or not _compatible(baseline.observation, observation):
        return Truth.UNKNOWN
    old = baseline.observation
    if observation.capture_start_ns <= old.capture_end_ns:
        return Truth.UNKNOWN
    if comparison == 'delta_gt' and observation.output_kind == 'integer' and type(value) is int and type(old.value) is int:
        answer = value > old.value
    elif comparison == 'set_grew' and observation.output_kind == 'set' and type(value) is frozenset and type(old.value) is frozenset:
        answer = bool(value - old.value)
    elif comparison == 'changed' and observation.output_kind == 'ram':
        result = owners.capabilities.changed(predicate, old, observation)
        return result.result if type(result) is AdmissionCheck else Truth.UNKNOWN
    else:
        return Truth.UNKNOWN
    return Truth.TRUE if answer else Truth.FALSE


def _reserve(owners, kind, amount):
    _need(type(amount) is int and amount > 0 and _admitted(owners.resources.reserve(kind, amount)), 'OUT_OF_SUPPORT')


def _observe(predicate, state, owners, deadline):
    for attempt in range(4):
        _time(owners, deadline)
        _need(state.acquisitions < owners.limits.acquisitions, 'OUT_OF_SUPPORT')
        state.acquisitions += 1
        _reserve(owners, 'acquisition_peak', owners.limits.peak_bytes)
        try:
            value = owners.observations.acquire(predicate, deadline)
        except NativeReadFailure as exc:
            disposition = gate.translate_native(exc.component, exc.operation, exc.code, exc.reason,
                                               observed_human_boundary=exc.observed_human_boundary,
                                               retries_exhausted=attempt == 3)
            registered = ((exc.component == 'producer' and exc.operation == 'read' and exc.code in (4, 6) and exc.reason in ('render', 'navigation')) or
                          (exc.component == 'vision-engine' and exc.operation == 'observe' and {0: 'OK', 2: 'usage', 3: 'reject', 4: 'stale', 5: 'gate', 6: 'budget'}.get(exc.code) == exc.reason))
            if registered:
                _need(len(state.native_results) < owners.limits.event_count, 'OUT_OF_SUPPORT')
                state.native_results.append(dict(component=exc.component, operation=exc.operation, rc=exc.code, reason=exc.reason))
            rc = disposition.rc if disposition.rc is not None else 5
            _need(disposition.disposition == 'retry_read', {2: 'USAGE_SCHEMA', 3: 'SEAL_MISMATCH', 4: 'HUMAN_HANDOFF'}.get(rc, 'OUT_OF_SUPPORT'), rc)
            _pause(owners, (100, 250, 500)[attempt] * 1_000_000, deadline)
            continue
        _time(owners, deadline)
        return value


def _predicate(predicate, state, owners, deadline, *, after=None):
    value = _observe(predicate, state, owners, deadline)
    if after is not None and (type(value) is not ObservationValue or value.capture_start_ns <= after):
        return Truth.UNKNOWN
    return evaluate_predicate(predicate, observation=value, snapshots=state.snapshots, owners=owners)


def _authorize(phase, state, owners):
    _time(owners)
    _need(not state.uncertain, 'INPUT_UNCERTAIN')
    _need(_admitted(owners.artifact.check(state.pin)), 'SEAL_MISMATCH', 3)
    _need(_admitted(owners.authorization.check(phase, state.workflow, state.scope, state.authority)), 'OUT_OF_SUPPORT')
    _time(owners)


def _grounded(value, target, state, owners, deadline):
    return (type(value) is GroundedTarget and type(value.point) is tuple and len(value.point) == 2 and
            all(type(n) is int and n >= 0 for n in value.point) and
            value.scope == owners.scope and value.authority == owners.authority and
            type(value.expires_ns) is int and owners.clock() < min(value.expires_ns, deadline) and
            _valid_observation(value.observation, owners) and
            type(state.start_ns) is int and value.observation.capture_start_ns >= state.start_ns and
            (state.run.last_input_ns is None or value.observation.capture_start_ns > state.run.last_input_ns) and
            _admitted(value.applicability) and
            value.support_owner is owners.capabilities and
            _admitted(owners.capabilities.validate_target(target, value, state)))


# @anchor:wa.learn.mw_workflow.locate_target
def locate_target(target, *, step_state, owners, deadline_ns):
    """Relocate through registered owners; T3 never supplies input coordinates."""
    _time(owners, deadline_ns)
    _need(type(target) is dict and target.get('rung') in ('T0', 'T1', 'T2'), 'OUT_OF_SUPPORT')
    state = step_state.run
    _need(state.acquisitions < owners.limits.acquisitions, 'OUT_OF_SUPPORT')
    state.acquisitions += 1
    _reserve(owners, 'target_peak', owners.limits.peak_bytes)
    if target['rung'] == 'T0':
        _need(target['match_mode'] == 'pixel_ncc' and owners.replay_policy is not None, 'OUT_OF_SUPPORT')
        _need(owners.replay_policy.limits.memory_bytes <= owners.limits.peak_bytes and
              owners.replay_policy.limits.retained_bytes <= owners.limits.retained_bytes, 'OUT_OF_SUPPORT')
        candidates = []
        for order, instance in enumerate(target['instances']):
            _time(owners, deadline_ns)
            frame, descriptor, publication, custody = owners.ram.t0(target, instance, step_state, deadline_ns)
            _need(_admitted(custody) and _valid_observation(publication, owners), 'OUT_OF_SUPPORT')
            result = locate(frame, descriptor, policy=owners.replay_policy, deadline_ns=deadline_ns, clock=owners.clock)
            _time(owners, deadline_ns)
            if result.failure_code is None and result.point is not None and result.score is not None and result.score >= target['min_confidence_bps'] * 100:
                candidates.append((result.score, -order, result, publication))
        _need(bool(candidates), 'LOW_CONFIDENCE')
        candidates.sort(key=lambda row: row[:2], reverse=True)
        best = candidates[0]
        _need(not any(c[0] == best[0] and c[2].point != best[2].point for c in candidates[1:]), 'LOW_CONFIDENCE')
        value = GroundedTarget(tuple(best[2].point), None, best[3], owners.scope, owners.authority,
                               AdmissionCheck(Truth.TRUE, None), owners.capabilities, deadline_ns, tuple(best[2].translation))
    else:
        if target['rung'] == 'T2':
            _need(target['locale'] is not None, 'OUT_OF_SUPPORT')
        try:
            value = owners.capabilities.target(target, step_state, deadline_ns)
        except LocationFailure as exc:
            raise _Stop('LOW_CONFIDENCE', diagnostic=exc.code) from None
    _need(_grounded(value, target, step_state, owners, deadline_ns), 'LOW_CONFIDENCE')
    return value


def _verifier(spec, step, state, owners):
    _need(_admitted(owners.capabilities.verifier(spec)), 'OUT_OF_SUPPORT')
    predicate = spec['predicate']
    if spec['mode'] in ('state_signal', 'side_effect'):
        _need(predicate is not None, 'OUT_OF_SUPPORT')
        return _predicate(predicate, state.run, owners, state.verify_deadline_ns, after=state.input_ns)
    # The legacy unmasked implementation is visual evidence only. Independent
    # semantic postconditions and the final oracle still have to pass.
    if spec['mode'] != 'after_crop' or spec['mask_boxes_px']:
        _need(callable(getattr(owners.capabilities, 'observe_verifier', None)) and predicate is not None, 'OUT_OF_SUPPORT')
        _need(state.run.acquisitions < owners.limits.acquisitions, 'OUT_OF_SUPPORT')
        state.run.acquisitions += 1
        _reserve(owners, 'verifier_peak', owners.limits.peak_bytes)
        value = owners.capabilities.observe_verifier(spec, state, state.verify_deadline_ns)
        _time(owners, state.verify_deadline_ns)
        if state.input_ns is not None and (type(value) is not ObservationValue or value.capture_start_ns <= state.input_ns):
            return Truth.UNKNOWN
        return evaluate_predicate(predicate, observation=value, snapshots=state.run.snapshots, owners=owners)
    _need(owners.replay_policy is not None and
          owners.replay_policy.limits.memory_bytes <= owners.limits.peak_bytes and
          owners.replay_policy.limits.retained_bytes <= owners.limits.retained_bytes, 'OUT_OF_SUPPORT')
    before, after, expected, custody = owners.ram.change(spec, state, state.verify_deadline_ns)
    _need(_admitted(custody), 'OUT_OF_SUPPORT')
    _time(owners, state.verify_deadline_ns)
    result = verify_change(before, after, expected, translation=state.target.translation if state.target is not None else (0, 0), policy=owners.replay_policy)
    _time(owners, state.verify_deadline_ns)
    if result.verified is not True:
        return Truth.FALSE
    _need(predicate is not None, 'OUT_OF_SUPPORT')
    return _predicate(predicate, state.run, owners, state.verify_deadline_ns, after=state.input_ns)


_GUIDANCE_RECHECK = object()


def _guidance_release(state, owners, frame):
    if not any(frame is held for held in state.guidance_frames):
        return
    _need(_admitted(owners.guidance.release(frame, deadline_ns=owners.clock() + owners.limits.drain_ns)))
    state.guidance_frames[:] = [held for held in state.guidance_frames if held is not frame]
    state.retained -= owners.guidance.policy.frame_retained_bytes
    if state.current_step is not None and state.current_step.guidance_baseline is frame:
        state.current_step.guidance_baseline = None


def _guidance_release_all(state, owners):
    failed = False
    for frame in tuple(state.guidance_frames):
        try:
            _guidance_release(state, owners, frame)
        except Exception:
            failed = True
    _need(not failed)


def _condition_skip(step, ss, owners):
    state = ss.run
    if step['when'] is not None:
        truth = _predicate(step['when'], state, owners, ss.deadline_ns)
        _need(truth is not Truth.UNKNOWN, 'LOW_CONFIDENCE')
        if truth is Truth.FALSE:
            _need(not step['protected'] and not any(ss.seq in members for members in state.required_segments.values()), 'OUT_OF_SUPPORT')
            _classify(state, ss, owners, 'condition_skip')
            return True
    return False


def _satisfied(step, ss, owners):
    if step['already_satisfied'] is None:
        return False
    truth = _predicate(step['already_satisfied'], ss.run, owners, ss.deadline_ns)
    _need(truth is not Truth.UNKNOWN, 'LOW_CONFIDENCE')
    if truth is Truth.TRUE:
        _need(_admitted(owners.capabilities.satisfaction(step, ss)), 'LOW_CONFIDENCE')
        return True
    return False


def _guidance_poll(ss, owners, deadline, *, force=False):
    owner, state = owners.guidance, ss.run
    if owner is None:
        return False
    _need(not state.uncertain and ss.dispatch_disposition != 'INPUT_UNCERTAIN', 'INPUT_UNCERTAIN')
    now = _time(owners, deadline)
    if not force and ss.guidance_poll_ns is not None and now - ss.guidance_poll_ns < owner.policy.poll_ns:
        return False
    _need(now - state.last_progress_ns < 3 * owners.limits.no_progress_ns, 'STUCK')
    policy = owner.policy
    _need(state.acquisitions < owners.limits.acquisitions)
    _need(state.retained + policy.frame_retained_bytes <= owners.limits.retained_bytes)
    # Charge both peak and retained allocations before the frame producer.
    _reserve(owners, 'guidance_peak', policy.frame_peak_bytes)
    _reserve(owners, 'guidance_retained', policy.frame_retained_bytes)
    state.acquisitions += 1
    frame = owner.capture(ss, deadline_ns=deadline)
    # Track even malformed producer results so ownership is drained on refusal.
    state.guidance_frames.append(frame)
    state.retained += policy.frame_retained_bytes
    _time(owners, deadline)
    _need(_admitted(guidance.validate_frame(frame, ss, owner)) and frame.surface is owners.surface and
          frame.capture_end_ns <= owners.clock())
    ss.guidance_poll_ns = owners.clock()
    before = ss.guidance_baseline
    if before is None:
        ss.guidance_baseline = frame
        return False
    _need(_admitted(guidance.validate_frame(before, ss, owner)))
    _reserve(owners, 'guidance_diff_peak', policy.frame_peak_bytes)
    regions = guidance.changed_regions(before.frame, frame.frame, adapter=owner.adapter)
    _time(owners, deadline)
    expected = owner.expected_regions(ss) if ss.input_ns is not None else None
    decision = guidance.classify_guidance(before, frame, step_state=ss, policy=policy, regions=regions,
        expected_regions=expected, quiet_start_ns=ss.guidance_quiet_start,
        quiet_boundary_ns=ss.guidance_quiet_boundary)
    _need(decision.disposition != 'refused')
    _need(_admitted(guidance.validate_frame(frame, ss, owner)))
    if decision.disposition == 'quiet':
        if ss.guidance_quiet_start is None:
            ss.guidance_quiet_start = (before.capture_start_ns if ss.input_ns is None or
                before.capture_start_ns > ss.input_ns else frame.capture_start_ns)
        ss.guidance_quiet_boundary = frame.capture_end_ns
    else:
        ss.guidance_quiet_start = ss.guidance_quiet_boundary = None
    changed = decision.disposition == 'guidance'
    if changed:
        pair = (before.sequence, frame.sequence)
        _need(pair not in state.guidance_pairs)
        _need(state.guidance_reperceptions < policy.max_reperceptions and
              state.guidance_events + len(decision.evidence) <= policy.max_events)
        # Keep room for delivery and terminal/failure evidence after acceptance.
        count = len(decision.evidence)
        reserve_count = count + 3 + len(state.workflow['steps'])
        _need(state.journal_seq + reserve_count <= owners.limits.event_count and
              state.journal_bytes + reserve_count * owners.limits.event_bytes <= owners.limits.journal_bytes)
        _need(_admitted(owners.journal.reserve(reserve_count * owners.limits.event_bytes, reserve_count)))
        prepared = []
        for evidence in decision.evidence:
            demo = owner.mini_demo(evidence, ss, deadline_ns=deadline)
            _time(owners, deadline)
            evidence = replace(evidence, demo_ref=demo)
            payload = dict(time_ns=evidence.time_ns, box_px=list(evidence.box_px), classification=evidence.classification,
                before_sequence=evidence.before_sequence, after_sequence=evidence.after_sequence, demo_ref=evidence.demo_ref)
            _need(guidance.validate_payload(payload))
            ref = guidance.evidence_reference(evidence, '0' * 64)
            size = len(gate.canon(payload).encode()) + len(ref.encode())
            _need(state.guidance_bytes + size <= policy.metadata_bytes)
            _need(state.retained + size <= owners.limits.retained_bytes)
            _reserve(owners, 'guidance_metadata', size)
            state.guidance_bytes += size
            state.retained += size
            prepared.append((evidence, payload))
        _need(_admitted(guidance.validate_frame(frame, ss, owner)))
        _authorize('guidance', state, owners)
        state.guidance_pairs.add(pair)
        state.guidance_reperceptions += 1
        frozen = (ss.deadline_ns, ss.locate_deadline_ns, ss.verify_deadline_ns,
                  ss.input_ns, ss.busy_seen, state.last_progress_ns, ss.iteration)
        snapshots = dict(state.snapshots)
        refreshed = owner.reperceive(frame, ss, deadline_ns=deadline)
        unchanged = frozen == (ss.deadline_ns, ss.locate_deadline_ns, ss.verify_deadline_ns,
                               ss.input_ns, ss.busy_seen, state.last_progress_ns, ss.iteration) and snapshots == state.snapshots
        if not unchanged:
            (ss.deadline_ns, ss.locate_deadline_ns, ss.verify_deadline_ns,
             ss.input_ns, ss.busy_seen, state.last_progress_ns, ss.iteration) = frozen
            state.snapshots.clear()
            state.snapshots.update(snapshots)
        _need(unchanged and _admitted(refreshed))
        _time(owners, deadline)
        _need(_admitted(guidance.validate_frame(frame, ss, owner)))
        ss.target = None
        ss.consecutive = 0
        # Do not touch input_ns, busy_seen, snapshots, progress or deadlines.
        for evidence, payload in prepared:
            digest = _event(state, owners, 'operator_guidance', ss.seq, 'terminal', payload)
            state.guidance_refs.append(guidance.evidence_reference(evidence, digest))
            state.guidance_events += 1
    _guidance_release(state, owners, before)
    ss.guidance_baseline = frame
    return changed


# @anchor:wa.learn.mw_workflow.verify_step
def verify_step(step, *, state, owners):
    """Causal busy-then-stable verification under one frozen deadline."""
    spec = step['verify']
    alternatives = [spec] + spec['fallback_verifiers']
    for child in spec['fallback_verifiers']:
        _need(not child['fallback_verifiers'] and all(child[k] == spec[k] for k in ('busy_predicate', 'deadline_ms')) and
              (child['predicate'] or {}).get('snapshot_ref') == (spec['predicate'] or {}).get('snapshot_ref') and
              _admitted(owners.capabilities.equivalent(spec, child)), 'OUT_OF_SUPPORT')
    while True:
        now = _time(owners, state.verify_deadline_ns)
        if now - state.run.last_progress_ns >= 3 * owners.limits.no_progress_ns:
            raise _Stop('STUCK')
        _guidance_poll(state, owners, state.verify_deadline_ns)
        busy = spec['busy_predicate']
        if busy is not None and not state.busy_seen:
            seen = _predicate(busy, state.run, owners, state.verify_deadline_ns, after=state.input_ns)
            if seen is Truth.TRUE:
                state.busy_seen = True
                # A busy transition is progress only once per reached step.
                if not state.run.busy_transition:
                    state.run.last_progress_ns = owners.clock()
                    state.run.busy_transition = True
                state.consecutive = 0
                _pause(owners, spec['poll_interval_ms'] * 1_000_000, state.verify_deadline_ns)
                continue
            else:
                state.consecutive = 0
                _pause(owners, spec['poll_interval_ms'] * 1_000_000, state.verify_deadline_ns)
                continue
        elif busy is not None:
            # Busy cannot remain asserted while a generic quiet predicate claims
            # completion. UNKNOWN resets stability as well.
            clear = _predicate(busy, state.run, owners, state.verify_deadline_ns, after=state.input_ns)
            if clear is not Truth.FALSE:
                state.consecutive = 0
                _pause(owners, spec['poll_interval_ms'] * 1_000_000, state.verify_deadline_ns)
                continue
        passed = False
        for alternative in alternatives:
            _time(owners, state.verify_deadline_ns)
            truth = _verifier(alternative, step, state, owners)
            if truth is Truth.TRUE:
                passed = True
                break
        state.consecutive = state.consecutive + 1 if passed else 0
        if state.consecutive >= spec['consecutive_polls']:
            state.run.last_progress_ns = owners.clock()
            return AdmissionCheck(Truth.TRUE, None)
        _pause(owners, spec['poll_interval_ms'] * 1_000_000, state.verify_deadline_ns)


def _capture(step, state, owners):
    for capture in step['capture_before']:
        name = capture['name']
        _need(name not in state.snapshots and len(state.snapshots) < owners.limits.snapshots, 'OUT_OF_SUPPORT')
        amount = owners.limits.retained_bytes // owners.limits.snapshots
        _reserve(owners, 'retained_snapshot', amount)
        value = _observe(capture['observe'], state, owners, owners.limits.deadline_ns)
        _need(_valid_observation(value, owners), 'OUT_OF_SUPPORT')
        _need(type(value.value) in (bool, int, frozenset) or value.output_kind == 'ram', 'OUT_OF_SUPPORT')
        # RAM references are immutable and custody-validated by the observation owner.
        _need(state.retained + amount <= owners.limits.retained_bytes, 'OUT_OF_SUPPORT')
        state.retained += amount
        state.snapshots[name] = CapturedSnapshot(name, value, amount)


def _event(state, owners, kind, seq, phase, payload=None):
    _time(owners)
    event = dict(seq=state.journal_seq + 1, prev=state.journal_prev, kind=kind, step_seq=seq, phase=phase)
    if kind == 'operator_guidance':
        _need(phase == 'terminal' and not state.uncertain and guidance.validate_payload(payload))
        event['guidance'] = payload
    else:
        _need(payload is None)
    digest = owners.journal.digest(gate.canon(event).encode(), state.journal_prev)
    _need(type(digest) is str and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest), 'TORN', 3)
    raw = (gate.canon(dict(event, digest=digest)) + '\n').encode()
    _need(len(raw) <= owners.limits.event_bytes and state.journal_seq < owners.limits.event_count and
          state.journal_bytes + len(raw) <= owners.limits.journal_bytes, 'OUT_OF_SUPPORT')
    _need(_admitted(owners.journal.append(raw)), 'INPUT_UNCERTAIN' if state.uncertain else 'TORN', 3)
    state.journal_seq += 1
    state.journal_prev = digest
    state.journal_bytes += len(raw)
    return digest


def _restart(state, owners):
    raw = owners.journal.read(owners.limits.journal_bytes)
    _need(type(raw) is bytes and len(raw) <= owners.limits.journal_bytes, 'TORN', 3)
    suffix = None
    if raw and not raw.endswith(b'\n'):
        end = raw.rfind(b'\n') + 1
        raw, suffix = raw[:end], raw[end:]
    outstanding = set()
    guidance_group, guidance_boxes = None, set()
    for line in raw.splitlines(keepends=True):
        _need(len(line) <= owners.limits.event_bytes and state.journal_seq < owners.limits.event_count, 'TORN', 3)
        try:
            event = gate.strict_load(line)
        except Exception:
            raise _Stop('TORN', 3) from None
        _need(type(event) is dict, 'TORN', 3)
        is_guidance = event.get('kind') == 'operator_guidance'
        keys = {'seq', 'prev', 'kind', 'step_seq', 'phase', 'digest'}
        _need(set(event) == keys | ({'guidance'} if is_guidance else set()), 'TORN', 3)
        _need(type(event['seq']) is int and event['seq'] == state.journal_seq + 1 and event['prev'] == state.journal_prev and
              line == (gate.canon(event) + '\n').encode() and _admitted(owners.journal.authenticate(event)), 'TORN', 3)
        _need(event['kind'] in ('intent', 'complete', 'verified', 'satisfied_skip', 'condition_skip', 'failed', 'operator_guidance'), 'TORN', 3)
        key = event['step_seq']
        _need(type(key) is int and 1 <= key <= len(state.workflow['steps']) and
              event['phase'] == ('delivery' if event['kind'] in ('intent', 'complete') else 'terminal'), 'TORN', 3)
        if is_guidance:
            payload = event['guidance']
            _need(not outstanding and guidance.validate_payload(payload), 'TORN', 3)
            pair = (payload['before_sequence'], payload['after_sequence'])
            # Distinct regions of one attribution are contiguous, never replayed.
            if pair != guidance_group:
                _need(pair not in state.guidance_pairs, 'TORN', 3)
                state.guidance_pairs.add(pair)
                guidance_group, guidance_boxes = pair, set()
            box = tuple(payload['box_px'])
            _need(box not in guidance_boxes, 'TORN', 3)
            guidance_boxes.add(box)
            evidence = guidance.GuidanceEvidence(key, **dict(payload, box_px=tuple(payload['box_px'])))
            state.guidance_refs.append(guidance.evidence_reference(evidence, event['digest']))
            state.resume_blocked = True
        if not is_guidance:
            guidance_group = None
        if event['kind'] == 'intent':
            _need(key not in outstanding, 'TORN', 3)
            outstanding.add(key)
        elif event['kind'] == 'complete':
            _need(key in outstanding, 'TORN', 3)
            outstanding.remove(key)
        if event['kind'] in ('verified', 'satisfied_skip', 'condition_skip'):
            state.resume_blocked = True
        state.journal_seq, state.journal_prev = event['seq'], event['digest']
        state.journal_bytes += len(line)
    if suffix is not None:
        # Only an authenticated complete prefix can seed inactive recovery. The
        # owner quarantines the suffix and validates before atomic selection;
        # this invocation always stops, and never replays effects.
        _need(_admitted(owners.journal.recover(raw, suffix)), 'TORN', 3)
        raise _Stop('TORN', 3)
    _need(not outstanding, 'INPUT_UNCERTAIN')
    state.journal_ready = True


def _action(step, ss, owners):
    action, state = step['action'], ss.run
    kind = action['kind']
    _need(kind in ('click', 'right_click', 'double_click', 'paste', 'key', 'scroll', 'drag', 'wait_signal'), 'OUT_OF_SUPPORT')
    if kind == 'wait_signal':
        return
    if kind in ('click', 'right_click', 'double_click', 'drag'):
        _need(ss.target is not None, 'OUT_OF_SUPPORT')
    if kind == 'scroll' and ss.target is None:
        _need(state.acquisitions < owners.limits.acquisitions, 'OUT_OF_SUPPORT')
        state.acquisitions += 1
        _reserve(owners, 'target_peak', owners.limits.peak_bytes)
        ss.target = owners.capabilities.target(None, ss, ss.locate_deadline_ns)
        _need(_grounded(ss.target, None, ss, owners, ss.locate_deadline_ns), 'OUT_OF_SUPPORT')
    if kind in ('key', 'paste'):
        _need(_admitted(owners.capabilities.focus(step, ss)), 'OUT_OF_SUPPORT')
    if kind == 'drag':
        displacement = type(action['dx_px']) is int and type(action['dy_px']) is int
        _need((action['destination'] is not None) != displacement, 'OUT_OF_SUPPORT')
        if action['destination'] is not None:
            destination = locate_target(action['destination'], step_state=ss, owners=owners, deadline_ns=ss.locate_deadline_ns)
            ss.target = GroundedTarget(ss.target.point, destination.point, ss.target.observation,
                                       ss.target.scope, ss.target.authority, ss.target.applicability,
                                       ss.target.support_owner, ss.target.expires_ns, ss.target.translation)
    # Referenced cleanup steps are close actions, executed in the declared order
    # with current grounding. Their semantic verifier proves each removal.
    for slot_spec in state.workflow['text_slots']:
        refs = slot_spec['chip_clear_steps']
        if ss.seq in refs:
            pos = refs.index(ss.seq)
            _need(kind == 'click' and ss.target is not None and
                  _grounded(ss.target, step['target'], ss, owners, ss.locate_deadline_ns), 'LOW_CONFIDENCE')
            prior = [state.completed_steps.get(n) for n in refs[:pos]]
            _need(all(x is not None and x.classification == 'verified' and x.target is not None for x in prior), 'OUT_OF_SUPPORT')
            if ss.seq not in state.chip_keys:
                _reserve(owners, 'chip_identity', owners.limits.workflow_bytes)
                ctx = owners.validation_context
                _need(callable(ctx.evidence_load) and callable(ctx.evidence_check), 'OUT_OF_SUPPORT')
                view = ctx.evidence_load('chip_geometry', step, (state.workflow,))
                _need(type(view) is dict and _passed(ctx.evidence_check('chip_geometry', step, (view, state.workflow))), 'OUT_OF_SUPPORT')
                _need(type(view.get('center_x_px')) is int and type(view.get('center_y_px')) is int and
                      type(view.get('chip_id')) is str and 0 < len(view['chip_id']) <= owners.limits.workflow_bytes and
                      view.get('guard_pass') is True and view.get('postcondition_pass') is True, 'OUT_OF_SUPPORT')
                amount = 4 * len(view['chip_id']) + 32
                _need(len(state.chip_keys) < len(state.workflow['steps']) and
                      state.retained + amount <= owners.limits.retained_bytes, 'OUT_OF_SUPPORT')
                state.retained += amount
                state.chip_keys[ss.seq] = (-ss.target.point[0], ss.target.point[1], view['chip_id'])
            _need(not prior or state.chip_keys[refs[pos - 1]] < state.chip_keys[ss.seq], 'LOW_CONFIDENCE')
    _need(_admitted(owners.capabilities.action(action, ss.target)), 'OUT_OF_SUPPORT')
    if type(owners.transport) is IdentityTransport:
        _need(action['path'] == 'coordinates' and kind in ('click', 'right_click', 'key', 'scroll', 'drag') and
              owners.transport.surface is owners.surface, 'OUT_OF_SUPPORT')
    _need(type(owners.dispatch) is LegacyDispatchOwner and owners.dispatch.check_ready(), 'OUT_OF_SUPPORT')
    slot, private, payload = None, None, None
    if kind == 'paste':
        slot = next((s for s in state.workflow['text_slots'] if s['slot_id'] == action['text_slot']), None)
        _need(slot is not None and owners.slots is not None and _admitted(owners.slots.admit(slot, ss)), 'OUT_OF_SUPPORT')
        slot_deadline = min(ss.deadline_ns, owners.clock() + slot['budget_ms'] * 1_000_000)
        _need(all(state.classifications.get(n) == 'verified' and n in state.completed_steps and
                  state.completed_steps[n].target is not None for n in slot['chip_clear_steps']), 'OUT_OF_SUPPORT')
        # Focus is reacquired after all removals, before reading private input.
        _need(_admitted(owners.capabilities.focus(step, ss)), 'OUT_OF_SUPPORT')
        _reserve(owners, 'private_slot', owners.limits.slot_bytes)
        private = owners.slots.read(slot, ss, owners.limits.slot_bytes)
        _time(owners, slot_deadline)
        _need(type(private) is PrivateSlot and private.scope == owners.scope and private.authority == owners.authority and
              _admitted(private.admission) and type(private.initial_length) is int and private.initial_length >= 0 and
              private.transport in ('private_pipe', 'owned_clipboard'), 'OUT_OF_SUPPORT')
        _need(type(private.value) is str and len(private.value) <= slot['max_length'], 'USAGE_SCHEMA', 2)
        if slot['clear_first']:
            _need(private.initial_length in slot['empty_len_allowed'], 'LOW_CONFIDENCE')
        payload = validate_text_slot(private.value, expected_length=len(private.value), max_bytes=owners.limits.slot_bytes)
        _need(state.slot_bytes + len(payload) <= owners.limits.aggregate_slot_bytes, 'OUT_OF_SUPPORT')
        state.slot_bytes += len(payload)
        if private.transport == 'private_pipe':
            _need(callable(getattr(owners.transport, 'send_private', None)), 'OUT_OF_SUPPORT')
        else:
            _need(slot['transport'] == 'clipboard_paste', 'OUT_OF_SUPPORT')
    _need(state.journal_seq + 3 <= owners.limits.event_count and
          state.journal_bytes + 3 * owners.limits.event_bytes <= owners.limits.journal_bytes, 'OUT_OF_SUPPORT')
    _need(_admitted(owners.journal.reserve(3 * owners.limits.event_bytes, 3)), 'OUT_OF_SUPPORT')
    delivery_deadline = min(ss.deadline_ns, owners.clock() + action['delivery_budget_ms'] * 1_000_000,
                            slot_deadline if kind == 'paste' else ss.deadline_ns)

    def send():
        _time(owners, delivery_deadline)
        _authorize('send', state, owners)
        if kind in ('key', 'paste'):
            _need(_admitted(owners.capabilities.focus(step, ss)), 'OUT_OF_SUPPORT')
        prepared = None
        if type(owners.transport) is IdentityTransport:
            prepared = _identity_payload(owners.surface, owners.transport.planner(action, ss.target), action, ss.target)
        _time(owners, delivery_deadline)
        if ss.target is not None:
            _need(_grounded(ss.target, step['target'], ss, owners, ss.locate_deadline_ns),
                  'OUT_OF_SUPPORT' if kind == 'scroll' and step['target'] is None else 'LOW_CONFIDENCE')
        if _guidance_poll(ss, owners, min(delivery_deadline, ss.locate_deadline_ns), force=True):
            return _GUIDANCE_RECHECK
        # Guidance grants no authorization; repeat after owner callbacks.
        if owners.guidance is not None:
            _authorize('send', state, owners)
            if kind in ('key', 'paste'):
                _need(_admitted(owners.capabilities.focus(step, ss)), 'OUT_OF_SUPPORT')
            if ss.target is not None:
                _need(_grounded(ss.target, step['target'], ss, owners, ss.locate_deadline_ns), 'LOW_CONFIDENCE')
        _event(state, owners, 'intent', ss.seq, 'delivery')
        boundary_ns = owners.clock()
        state.uncertain = True
        ss.dispatch_disposition = 'INPUT_UNCERTAIN'
        if private is not None and private.transport == 'private_pipe':
            receipt = owners.transport.send_private(action, ss.target, payload)
        elif prepared is not None:
            receipt = _identity_dispatch(owners.surface, prepared, action)
        else:
            receipt = owners.transport.send(action, ss.target)
        _need(type(receipt) is int and boundary_ns <= receipt <= owners.clock(), 'INPUT_UNCERTAIN')
        ss.input_ns = receipt
        state.last_input_ns = receipt
        _time(owners, delivery_deadline)
        _event(state, owners, 'complete', ss.seq, 'delivery')
        state.uncertain = False
        ss.dispatch_disposition = 'SENT'
        ss.guidance_quiet_start = ss.guidance_quiet_boundary = None

    clipboard_owned = False
    try:
        if private is not None and private.transport == 'owned_clipboard':
            # Once preparation begins, finally owns residue recovery even if the
            # callback fails halfway through writing the owned clipboard.
            clipboard_owned = True
            _need(_admitted(owners.slots.prepare(slot, payload, ss)), 'OUT_OF_SUPPORT')
        outcome = owners.dispatch.dispatch(owners.surface, action, send)
        if outcome is _GUIDANCE_RECHECK:
            return outcome
        if slot is not None:
            binding = state.length_bindings[slot['slot_id']]
            observed = _observe(binding.predicate, state, owners,
                                min(slot_deadline, ss.verify_deadline_ns, owners.limits.deadline_ns))
            _need(_valid_observation(observed, owners) and
                  observed.capability_id == binding.predicate['capability_id'] and
                  observed.capability_revision == binding.capability_revision and observed.unit == binding.unit and
                  observed.output_kind == 'integer' and type(observed.value) is int and observed.value >= 0 and
                  observed.capture_start_ns > ss.input_ns, 'CHANGE_MISMATCH')
            allowed = {len(private.value) if c == 'exact_n' else private.initial_length + len(private.value)
                       for c in slot['post_type_len_allowed']}
            _need(observed.value in allowed, 'CHANGE_MISMATCH')
            _need(_admitted(owners.slots.verify(slot, ss, len(private.value), private.initial_length)), 'CHANGE_MISMATCH')
    except BaseException:
        if state.uncertain:
            owners.dispatch.uncertain(action)
            raise _Stop('INPUT_UNCERTAIN') from None
        raise
    finally:
        if clipboard_owned:
            try:
                clean = _admitted(owners.slots.clear(slot)) and _admitted(owners.slots.cleared(slot))
            except Exception:
                clean = False
            if not clean:
                owners.dispatch.uncertain(action)
                raise _Stop('INPUT_UNCERTAIN' if state.uncertain else 'OUT_OF_SUPPORT')
        payload, private = None, None


def _classify(state, ss, owners, disposition):
    ss.classification = disposition
    state.classifications[ss.seq] = disposition
    state.completed_steps[ss.seq] = ss
    _event(state, owners, disposition, ss.seq, 'terminal')


def _execute(workflow, state, owners):
    for predicate in workflow['preconditions']:
        _need(_predicate(predicate, state, owners, owners.limits.deadline_ns) is Truth.TRUE, 'LOW_CONFIDENCE')
    for step in workflow['steps']:
        _guidance_release_all(state, owners)
        start = _time(owners)
        ss = StepState(step['seq'], state, owners.limits.deadline_ns,
                       min(owners.limits.deadline_ns, start + step['locate_wait_ms'] * 1_000_000),
                       min(owners.limits.deadline_ns, start + step['verify']['deadline_ms'] * 1_000_000))
        ss.start_ns = start
        ss.guidance_start_ns = start
        state.current_step = ss
        state.busy_transition = False
        _capture(step, state, owners)
        _guidance_poll(ss, owners, ss.locate_deadline_ns, force=True)
        _guidance_poll(ss, owners, ss.locate_deadline_ns, force=True)
        for predicate in step['preconditions']:
            _need(_predicate(predicate, state, owners, ss.deadline_ns) is Truth.TRUE, 'LOW_CONFIDENCE')
        if _condition_skip(step, ss, owners):
            continue
        if _satisfied(step, ss, owners):
            _classify(state, ss, owners, 'satisfied_skip')
            continue
        repeat = step['repeat']
        while True:
            if repeat is not None:
                truth = _predicate(repeat['while_predicate'], state, owners, ss.deadline_ns)
                _need(truth is not Truth.UNKNOWN, 'LOW_CONFIDENCE')
                if truth is Truth.FALSE:
                    # A zero-iteration repeat needs its postcondition too.
                    if ss.iteration == 0:
                        # No action was sent: use a current semantic proof without
                        # requiring busy transitions or a post-input crop pair.
                        zero_step = dict(step, verify=dict(step['verify'], mode='state_signal',
                                         busy_predicate=None, fallback_verifiers=[]))
                        _need(_admitted(verify_step(zero_step, state=ss, owners=owners)), 'CHANGE_MISMATCH')
                    break
                _need(ss.iteration < repeat['max_iterations'], 'STUCK')
            ss.start_ns = _time(owners)
            for predicate in step['preconditions']:
                _need(_predicate(predicate, state, owners, ss.deadline_ns) is Truth.TRUE, 'LOW_CONFIDENCE')
            _authorize('step', state, owners)
            if step['target'] is not None and step['action']['kind'] != 'wait_signal':
                while True:
                    try:
                        ss.target = locate_target(step['target'], step_state=ss, owners=owners, deadline_ns=ss.locate_deadline_ns)
                        break
                    except _Stop as exc:
                        if exc.code != 'LOW_CONFIDENCE':
                            raise
                        _pause(owners, step['locate_poll_ms'] * 1_000_000, ss.locate_deadline_ns)
            # Verification time is frozen before dispatch and never renewed by a
            # repeat, fallback, poll or target acquisition.
            if _action(step, ss, owners) is _GUIDANCE_RECHECK:
                for predicate in step['preconditions']:
                    _need(_predicate(predicate, state, owners, ss.deadline_ns) is Truth.TRUE, 'LOW_CONFIDENCE')
                if ss.input_ns is not None:
                    _need(_admitted(verify_step(step, state=ss, owners=owners)), 'CHANGE_MISMATCH')
                    continue
                if _condition_skip(step, ss, owners):
                    break
                if _satisfied(step, ss, owners):
                    _classify(state, ss, owners, 'satisfied_skip')
                    break
                continue
            ss.busy_seen, ss.consecutive = False, 0
            _need(_admitted(verify_step(step, state=ss, owners=owners)), 'CHANGE_MISMATCH')
            ss.iteration += 1
            if repeat is None:
                break
        if ss.classification == 'not_reached':
            _classify(state, ss, owners, 'verified')
        _guidance_release_all(state, owners)
    for predicate in workflow['oracle']['postconditions']:
        _need(_predicate(predicate, state, owners, owners.limits.deadline_ns) is Truth.TRUE, 'CHANGE_MISMATCH')
    _need(_admitted(owners.capabilities.final(workflow)), 'CHANGE_MISMATCH')


# @anchor:wa.learn.mw_workflow._summary
def _summary(state, owners, *, rc=0, verdict_code='VERIFIED'):
    missing = object()
    usage_hook = getattr(owners.capabilities, '_locate_usage', missing)
    model_calls, tokens_used = None, None
    if usage_hook is not missing:
        _need(callable(usage_hook))
        usage = usage_hook(state)
        if usage is not None:
            _need(type(usage) is tuple and len(usage) == 2)
            model_calls, tokens_used = usage
            _need(type(model_calls) is int and model_calls >= 0)
            _need(tokens_used is None or (type(tokens_used) is int and tokens_used >= 0))
    total = len(state.workflow['steps'])
    classifications = state.classifications
    segments = state.required_segments
    good = sum(all(classifications[n] in ('verified', 'satisfied_skip') for n in members) for members in segments.values())
    summary = dict(schema='mw.run.v1', seq=1, run_id=owners.run_id, site_id=state.workflow['site_id'],
                   workflow_id=state.workflow['id'], workflow_sha256=state.workflow_sha256, mode=owners.mode,
                   visibility='local', rc=rc, verdict_code=verdict_code, steps_total=total,
                   steps_verified=sum(v == 'verified' for v in classifications.values()),
                   steps_skipped_satisfied=sum(v == 'satisfied_skip' for v in classifications.values()),
                   steps_skipped_condition=sum(v == 'condition_skip' for v in classifications.values()),
                   steps_failed=sum(v == 'failed' for v in classifications.values()),
                   steps_not_reached=sum(v == 'not_reached' for v in classifications.values()), reproduced_ppm=good * 1_000_000 // len(segments) if segments else None,
                   duration_ms=None, model_calls=model_calls, tokens_used=tokens_used,
                   failed_step_seq=min((n for n, v in classifications.items() if v == 'failed'), default=None), baseline_run_id=None,
                   pre_state_sha256=None, post_state_sha256=None, evidence_refs=list(state.guidance_refs), prev_receipt_sha256=None,
                   evaluation=None, native_results=list(state.native_results))
    inventory = dict(workflow_sha256=state.workflow_sha256,
                     steps=[dict(seq=s['seq'], disposition=classifications[s['seq']],
                                 predicate_pass=classifications[s['seq']] in ('verified', 'satisfied_skip'),
                                 optional=not s['protected'], guard_false=classifications[s['seq']] == 'condition_skip')
                            for s in state.workflow['steps']],
                     required_segments={k: list(v) for k, v in segments.items()}, final_task_pass=rc == 0)
    return summary, inventory


def _record_failure(state, owners, bundle, rc, code):
    if state is None or not state.journal_ready or state.uncertain or code in ('TORN', 'INPUT_UNCERTAIN', 'SEAL_MISMATCH'):
        return None
    try:
        _guidance_release_all(state, owners)
    except Exception:
        code, rc = 'OUT_OF_SUPPORT', 5
    ss = state.current_step
    if ss is not None and ss.classification == 'not_reached':
        # Assignment precedes I/O; failed persistence cannot erase a failure.
        ss.classification = 'failed'
        state.classifications[ss.seq] = 'failed'
        state.completed_steps[ss.seq] = ss
        try:
            _need(_admitted(owners.journal.reserve(owners.limits.event_bytes, 1)))
            _event(state, owners, 'failed', ss.seq, 'terminal')
        except Exception:
            return None
    # Failure receipts use the same authenticated, inactive-generation path.
    # Uncertain input and invalid journals remain fenced for recovery.
    try:
        _time(owners)
        _need(_admitted(owners.resources.cancel(owners.limits.cancel_ns)) and
              _admitted(owners.resources.drain(owners.limits.drain_ns)))
        _authorize('commit', state, owners)
        summary, inventory = _summary(state, owners, rc=rc, verdict_code=code)
        staged = owners.journal.stage(summary, inventory, owners.output)
        _need(_passed(gate.validate_file(Path(staged), bundle, context=owners.journal.context())))
        _need(_admitted(owners.journal.validate(staged, inventory)))
        _time(owners)
        _need(_admitted(owners.journal.select(staged)))
        return summary
    except Exception:
        return None


# @anchor:wa.learn.mw_workflow.run_workflow
def run_workflow(path, *, bundle, owners, limits):
    """Execute only pinned canonical workflows with all trusted owners bound.

    The stuck detector bounds elapsed no-progress time. Recovery-attempt counting
    belongs to the later recovery controller.
    """
    state, cleanup_done = None, False
    try:
        _need(type(owners) is RunOwners and type(limits) is WorkflowLimits and owners.limits == limits, 'OUT_OF_SUPPORT')
        _need(all(type(getattr(limits, f.name)) is int and getattr(limits, f.name) > 0 for f in fields(limits)), 'USAGE_SCHEMA', 2)
        _need(all(getattr(owners, name) is not None for name in ('artifact', 'validation_context', 'authorization', 'capabilities',
              'observations', 'resources', 'journal', 'dispatch', 'surface', 'clock', 'sleep')), 'OUT_OF_SUPPORT')
        _need(owners.mode in ('replay', 'shadow', 'promoted', 'repair'), 'USAGE_SCHEMA', 2)
        _time(owners)
        with owners.artifact.pin(Path(path)) as pin:
            _need(type(pin) is PinnedArtifact and _admitted(pin.immutable), 'SEAL_MISMATCH', 3)
            root, source = Path(owners.validation_context.root).absolute(), Path(pin.path).absolute()
            _need(source.is_relative_to(root), 'SEAL_MISMATCH', 3)
            _need(not any(p.is_symlink() for p in (source, *source.parents)), 'SEAL_MISMATCH', 3)
            with source.open('rb') as stream:
                raw = stream.read(min(limits.workflow_bytes, 65536) + 1)
            _need(len(raw) <= min(limits.workflow_bytes, 65536), 'USAGE_SCHEMA', 2)
            _need(hashlib.sha256(raw).hexdigest() == pin.sha256 and _admitted(owners.artifact.check(pin)), 'SEAL_MISMATCH', 3)
            try:
                workflow = gate.strict_load(raw)
            except Exception:
                raise _Stop('USAGE_SCHEMA', 2) from None
            _need(raw == (gate.canon(workflow) + '\n').encode(), 'USAGE_SCHEMA', 2)
            validation = gate.validate_file(source, bundle, context=owners.validation_context)
            _need(_passed(validation), 'USAGE_SCHEMA' if getattr(validation, 'rc', 5) == 2 else 'OUT_OF_SUPPORT', getattr(validation, 'rc', 5))
            _need(workflow['schema'] == 'mw.workflow.v1' and len(workflow['steps']) <= limits.steps, 'USAGE_SCHEMA', 2)
            frozen = owners.authorization.inventory(workflow)
            _need(type(frozen) is dict and all(type(k) is str and type(v) in (tuple, list) and v and
                  len(v) == len(set(v)) and all(type(n) is int and 1 <= n <= len(workflow['steps']) for n in v) for k, v in frozen.items()), 'OUT_OF_SUPPORT')
            state = RunState(raw, pin.sha256, workflow, owners.scope, owners.authority,
                             MappingProxyType({k: tuple(v) for k, v in frozen.items()}), journal=owners.journal, pin=pin,
                             last_progress_ns=owners.clock())
            state.classifications = {s['seq']: 'not_reached' for s in workflow['steps']}
            try:
                missing = object()
                bind_hook = getattr(owners.capabilities, '_bind_locator_run', missing)
                if bind_hook is not missing:
                    _need(callable(bind_hook))
                    bind_hook(state)
                _restart(state, owners)
                _need(not state.resume_blocked, 'OUT_OF_SUPPORT')
                if owners.guidance is not None:
                    _need(_admitted(guidance.validate_owner(owners.guidance)))
                    policy = owners.guidance.policy
                    _need(policy.frame_peak_bytes <= limits.peak_bytes and
                          2 * policy.frame_retained_bytes <= limits.retained_bytes)
                _length_bindings(state, owners)
                _authorize('admission', state, owners)
                _need(_admitted(owners.capabilities.supports(workflow)), 'OUT_OF_SUPPORT')
                for step in workflow['steps']:
                    for spec in [step['verify']] + step['verify']['fallback_verifiers']:
                        _need(_admitted(owners.capabilities.verifier(spec)), 'OUT_OF_SUPPORT')
                        if spec['mode'] not in ('state_signal', 'side_effect'):
                            _need(spec['predicate'] is not None, 'OUT_OF_SUPPORT')
                            if spec['mode'] != 'after_crop' or spec['mask_boxes_px']:
                                _need(callable(getattr(owners.capabilities, 'observe_verifier', None)), 'OUT_OF_SUPPORT')
                            else:
                                _need(owners.ram is not None and owners.replay_policy is not None, 'OUT_OF_SUPPORT')
                _reserve(owners, 'terminal_evidence', limits.peak_bytes)
                _need(_admitted(owners.journal.reserve(limits.event_bytes * (len(workflow['steps']) + 1), len(workflow['steps']) + 1)), 'OUT_OF_SUPPORT')
                _need(not workflow['text_slots'] or owners.slots is not None, 'OUT_OF_SUPPORT')
                _execute(workflow, state, owners)
                _guidance_release_all(state, owners)
                _need(_admitted(owners.resources.cancel(limits.cancel_ns)) and _admitted(owners.resources.drain(limits.drain_ns)), 'OUT_OF_SUPPORT')
                cleanup_done = True
                _authorize('commit', state, owners)
                summary, inventory = _summary(state, owners)
                staged = owners.journal.stage(summary, inventory, owners.output)
                _need(_passed(gate.validate_file(Path(staged), bundle, context=owners.journal.context())), 'TORN', 3)
                _need(_admitted(owners.journal.validate(staged, inventory)), 'TORN', 3)
                _time(owners)
                _need(_admitted(owners.journal.select(staged)), 'TORN', 3)
                return RunResult(0, 'VERIFIED', summary, None)
            except _Stop as exc:
                summary = _record_failure(state, owners, bundle, exc.rc, exc.code)
                return RunResult(exc.rc, exc.code, summary, exc.diagnostic)
            except (Exception, MemoryError):
                code = 'INPUT_UNCERTAIN' if state.uncertain else 'OUT_OF_SUPPORT'
                summary = _record_failure(state, owners, bundle, 5, code)
                return RunResult(5, code, summary, 'OWNER_FAILURE')
    except _Stop as exc:
        summary = _record_failure(state, owners, bundle, exc.rc, exc.code)
        return RunResult(exc.rc, exc.code, summary, diagnostic=exc.diagnostic)
    except (Exception, MemoryError):
        code = 'INPUT_UNCERTAIN' if state is not None and state.uncertain else 'OUT_OF_SUPPORT'
        summary = _record_failure(state, owners, bundle, 5, code)
        return RunResult(5, code, summary, diagnostic='OWNER_FAILURE')
    finally:
        if state is not None and type(owners) is RunOwners:
            try:
                _guidance_release_all(state, owners)
            except Exception:
                pass
        if type(owners) is RunOwners and type(limits) is WorkflowLimits and not cleanup_done:
            # Never release allocations ourselves: failed drain remains owned.
            try:
                owners.resources.cancel(limits.cancel_ns)
                owners.resources.drain(limits.drain_ns)
            except Exception:
                pass
