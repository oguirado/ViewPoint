# @module {"id":"wa.learn.script_policy","layer":2,"owner":"LEARN","deps":[],"book":"HB-WA-s4-learn"}
"""Host-injected routing policy. Passive records never confer authority.

Origin: scripts_mesh/build/spec/s9_w.md. No production policy defaults.
"""
from dataclasses import dataclass as _dataclass
from fractions import Fraction as _Fraction
from threading import RLock as _RLock


def _need(value, code):
    if value is not True:
        raise ValueError(code)


class _Immutable:
    __slots__ = ()

    def __post_init__(self):
        def check(value):
            if isinstance(value, (dict, list, set, bytearray, memoryview)):
                raise TypeError('MUTABLE_RECORD')
            if type(value) is tuple:
                for item in value:
                    check(item)
        for name in self.__dataclass_fields__:
            check(getattr(self, name))


# @anchor:wa.learn.script_policy.CalibrationArtifact
@_dataclass(frozen=True, slots=True, repr=False)
class CalibrationArtifact(_Immutable):
    identity: str
    model: str
    tokenizer: str
    serving: tuple
    split: str
    sample_refs: tuple
    fitting_refs: tuple
    parameters: tuple
    thresholds: tuple
    policy_revision: str
    environment_revision: str
    registration: object


# @anchor:wa.learn.script_policy.validate_calibration_artifact
def validate_calibration_artifact(artifact, *, owner, current, min_samples,
                                  max_error, min_coverage):
    """The constructor-injected owner authenticates exact retained records."""
    if type(artifact) is not CalibrationArtifact or owner is None:
        return False
    a = artifact
    if (not a.identity or not a.split or a.registration is None
            or type(min_samples) is not int or min_samples <= 0
            or type(max_error) is not _Fraction or type(min_coverage) is not _Fraction
            or not 0 <= max_error <= 1 or not 0 < min_coverage <= 1):
        return False
    if any(type(x) is not tuple for x in (a.serving, a.sample_refs, a.fitting_refs,
                                          a.parameters, a.thresholds)):
        return False
    if len(a.sample_refs) < min_samples or len(set(a.sample_refs)) != len(a.sample_refs):
        return False
    if not a.fitting_refs or set(a.sample_refs) & set(a.fitting_refs):
        return False
    if current != (a.model, a.tokenizer, a.serving, a.split,
                   a.policy_revision, a.environment_revision):
        return False
    try:
        parameters, thresholds = dict(a.parameters), dict(a.thresholds)
        if (any(type(parameters.get(k)) is not int for k in
                ('t_star_num', 't_star_den', 'q_hat_promille'))
                or parameters['t_star_num'] <= 0 or parameters['t_star_den'] <= 0
                or not 0 <= parameters['q_hat_promille'] <= 1000):
            return False
        if (type(thresholds.get('error')) is not _Fraction
                or type(thresholds.get('coverage')) is not _Fraction
                or not 0 <= thresholds['error'] <= max_error
                or not min_coverage <= thresholds['coverage'] <= 1):
            return False
        if any(type(thresholds.get(k)) is not int or not floor <= thresholds[k] <= 1000
               for k, floor in (('tau_default_promille', 800), ('tau_destructive_promille', 950),
                                ('margin_promille', 150))):
            return False
        return owner.check(a, current=current, min_samples=min_samples,
                           max_error=max_error, min_coverage=min_coverage) is True
    except Exception:
        return False


# @anchor:wa.learn.script_policy.RouteEligibility
@_dataclass(frozen=True, slots=True, repr=False)
class RouteEligibility(_Immutable):
    lane: str
    eligible: bool | None
    reason: str | None
    scope: object
    authority: object
    observation: object
    evidence: tuple


# @anchor:wa.learn.script_policy.eligible_routes
def eligible_routes(routes, *, scope, authority, observation, owner):
    """Keep deterministic proofs separate from inference calibration."""
    order = ('SCRIPT', 'STRUCTURAL', 'TEXT', 'VISUAL', 'MUSE')
    accepted = {}
    for route in routes:
        _need(type(route) is RouteEligibility and route.lane in order, 'ROUTE_SCHEMA')
        _need(route.lane not in accepted, 'DUPLICATE_ROUTE')
        accepted[route.lane] = route
    result = []
    for lane in order:
        route = accepted.get(lane)
        if (route is None or route.eligible is not True or not route.evidence
                or (route.scope, route.authority, route.observation) != (scope, authority, observation)):
            continue
        try:
            permitted = owner.check(route, inference=lane in ('TEXT', 'VISUAL', 'MUSE'))
        except Exception:
            permitted = False
        if permitted is True:
            result.append(route)
    return tuple(result)


# @anchor:wa.learn.script_policy.CostModel
@_dataclass(frozen=True, slots=True, repr=False)
class CostModel(_Immutable):
    metric: tuple
    convention: str
    costs: tuple
    probabilities: tuple
    denominator: int | None
    provenance: str | None
    missing_reason: str | None


# @anchor:wa.learn.script_policy.expected_cost
def expected_cost(model):
    _need(type(model) is CostModel, 'COST_SCHEMA')
    if (model.denominator is None or model.provenance is None
            or any(v is None for v in model.costs + model.probabilities)):
        _need(bool(model.missing_reason), 'COST_MISSING_REASON')
        return None
    _need(type(model.denominator) is int and model.denominator > 0
          and bool(model.provenance) and bool(model.metric) and bool(model.costs), 'COST_EVIDENCE')
    _need(all(type(v) is _Fraction and v >= 0 for v in model.costs), 'COST_UNIT')
    _need(all(type(p) is _Fraction and 0 <= p <= 1 for p in model.probabilities), 'COST_PROBABILITY')
    if model.convention == 'conditional':
        _need(len(model.probabilities) == len(model.costs) - 1, 'COST_SHAPE')
        result = model.costs[-1]
        for cost, probability in reversed(tuple(zip(model.costs[:-1], model.probabilities))):
            result = cost + probability * result
        return result
    _need(model.convention == 'unconditional' and len(model.probabilities) == len(model.costs), 'COST_SHAPE')
    _need(model.probabilities[0] == 1 and all(a >= b for a, b in
          zip(model.probabilities, model.probabilities[1:])), 'COST_REACH')
    return sum((c * p for c, p in zip(model.costs, model.probabilities)), _Fraction())


# @anchor:wa.learn.script_policy.guard_drift
def guard_drift(expected, actual, *, owner, checkpoint=None, planned_transition=False):
    """Owner checks exact binding/facts/lease; similarity is never a substitute."""
    required = ('selector', 'ownership', 'observation', 'publication', 'generation',
                'geometry', 'facts', 'lease', 'scope', 'authority')
    if any(k not in expected or k not in actual or expected[k] is None or actual[k] is None
           for k in required):
        return False
    try:
        if planned_transition:
            if checkpoint is None or owner.checkpoint(checkpoint, expected, actual) is not True:
                return False
        elif any(expected[k] != actual[k] for k in required):
            return False
        return owner.check(actual) is True
    except Exception:
        return False


# @anchor:wa.learn.script_policy.AdmissionPolicy
@_dataclass(frozen=True, slots=True, repr=False)
class AdmissionPolicy(_Immutable):
    aggregate_bytes: int
    safety_reserve_bytes: int
    concurrency: int
    queue_slots: int
    queue_bytes: int
    host_attempts: int
    principal_attempts: int
    rate_window_ns: int
    stage_allocations: tuple
    maximum_deadline_ns: int
    cancellation_ns: int
    safety_response_ns: int


# @anchor:wa.learn.script_policy.AdmissionGovernor
class AdmissionGovernor:
    """One host-owned instance across episodes; bounded principal table.

    No user callback runs under this lock. Reservations are identity-bound;
    ambiguous drain preserves capacity rather than refunding it.
    """
    # @anchor:wa.learn.script_policy.AdmissionGovernor.__init__
    def __init__(self, policy, *, clock, telemetry, drain_owner):
        _need(type(policy) is AdmissionPolicy, 'ADMISSION_POLICY')
        values = tuple(getattr(policy, name) for name in policy.__dataclass_fields__
                       if name != 'stage_allocations')
        _need(all(type(v) is int and v > 0 for v in values), 'ADMISSION_LIMIT_MISSING')
        _need(policy.safety_reserve_bytes < policy.aggregate_bytes
              and type(policy.stage_allocations) is tuple and bool(policy.stage_allocations)
              and all(type(v) is int and v > 0 for _, v in policy.stage_allocations), 'ADMISSION_RESERVE')
        self.policy, self._clock = policy, clock
        self._telemetry, self._drain_owner = telemetry, drain_owner
        self._lock, self._rows = _RLock(), {}
        self._window, self._attempts, self._principals = clock(), 0, {}
        self._charged = 0

    # @anchor:wa.learn.script_policy.AdmissionGovernor.charge
    def charge(self, principal):
        """Charge even invalid/rejected drafts before parser/model work."""
        with self._lock:
            now = self._clock()
            _need(type(now) is int and now >= self._window, 'ADMISSION_CLOCK')
            if now - self._window >= self.policy.rate_window_ns:
                self._window, self._attempts, self._principals = now, 0, {}
            self._attempts += 1
            _need(self._attempts <= self.policy.host_attempts, 'HOST_RATE')
            _need(type(principal) is str and bool(principal), 'PRINCIPAL')
            count = self._principals.get(principal, 0) + 1
            self._principals[principal] = count
            _need(count <= self.policy.principal_attempts, 'PRINCIPAL_RATE')

    # @anchor:wa.learn.script_policy.AdmissionGovernor.reserve
    def reserve(self, owner, *, peak_bytes, queued_bytes, deadline_ns):
        measured = self._telemetry()
        _need(type(measured) is int and measured >= 0, 'AGGREGATE_TELEMETRY')
        _need(type(peak_bytes) is int and peak_bytes > 0 and type(queued_bytes) is int
              and queued_bytes >= 0, 'AGGREGATE_PEAK')
        with self._lock:
            now = self._clock()
            _need(type(deadline_ns) is int and now < deadline_ns
                  and deadline_ns - now <= self.policy.maximum_deadline_ns, 'ADMISSION_DEADLINE')
            _need(id(owner) not in self._rows, 'DUPLICATE_OWNER')
            _need(len(self._rows) < min(self.policy.concurrency, self.policy.queue_slots)
                  and sum(r[2] for r in self._rows.values()) + queued_bytes <= self.policy.queue_bytes,
                  'ADMISSION_QUEUE')
            _need(max(measured, self._charged) + peak_bytes <=
                  self.policy.aggregate_bytes - self.policy.safety_reserve_bytes, 'AGGREGATE_CAPACITY')
            token = object()
            self._rows[id(owner)] = (owner, peak_bytes, queued_bytes, token)
            self._charged += peak_bytes
            return token

    # @anchor:wa.learn.script_policy.AdmissionGovernor.grow
    def grow(self, owner, token, *, extra_bytes):
        measured = self._telemetry()
        _need(type(measured) is int and measured >= 0, 'AGGREGATE_TELEMETRY')
        with self._lock:
            row = self._rows.get(id(owner))
            _need(row is not None and row[0] is owner and row[3] is token, 'RESERVATION_OWNER')
            _need(type(extra_bytes) is int and extra_bytes > 0, 'AGGREGATE_PEAK')
            _need(max(measured, self._charged) + extra_bytes <=
                  self.policy.aggregate_bytes - self.policy.safety_reserve_bytes, 'AGGREGATE_CAPACITY')
            self._rows[id(owner)] = (owner, row[1] + extra_bytes, row[2], token)
            self._charged += extra_bytes

    # @anchor:wa.learn.script_policy.AdmissionGovernor.release
    def release(self, owner, token, *, proof):
        _need(self._drain_owner.check(owner, proof) is True, 'OWNER_NOT_DRAINED')
        with self._lock:
            row = self._rows.get(id(owner))
            _need(row is not None and row[0] is owner and row[3] is token, 'RESERVATION_OWNER')
            self._charged -= row[1]
            del self._rows[id(owner)]

    # @anchor:wa.learn.script_policy.AdmissionGovernor.outstanding
    def outstanding(self):
        with self._lock:
            return tuple((r[0], r[1]) for r in self._rows.values())
