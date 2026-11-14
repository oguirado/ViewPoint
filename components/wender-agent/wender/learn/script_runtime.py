# @module {"id":"wa.learn.script_runtime","layer":3,"owner":"LEARN","deps":["wa.learn.script_integration","wa.learn.script_policy","wa.learn.context_router","wa.learn.script_generator","wa.learn.script_store","wa.learn.script_ledger","wa.scripts.compiler","wa.scripts.types","wa.scripts.header","wa.scripts.frame_wait","wa.scripts.act_api","wa.scripts.runner"],"book":"HB-WA-s4-learn"}
"""S11A owner coordination and bounded episode records.
Origin: scripts_mesh/build/spec/s11a_w.md.
Host services are explicitly injected; construction does not confer authority.
Safe drained suspensions preserve fallback eligibility without being relabeled
as successful completion or ordinary resolver misses.
"""
from dataclasses import dataclass as _dataclass
import json as _json
from threading import RLock as _RLock

_STAGES = ('observe', 'select', 'act', 'verify', 'frame_wait', 'model')
_RESIDENT = ('before_release', 'after_release')
_PROVENANCE = frozenset(('owner_monotonic', 'owner_accounting'))
_REASONS = frozenset(('instrumentation_unavailable', 'accounting_unavailable',
                      'terminal_evidence_unavailable', 'provider_counter_unavailable'))


def _need(value, code):
    if value is not True:
        raise ValueError(code)


def _quantity(value):
    _need(type(value) is int and value >= 0, 'METRIC_QUANTITY')


def _code(value, allowed):
    _need(type(value) is str and value in allowed, 'METRIC_PUBLIC_CODE')


def _pairs(value, keys):
    _need(type(value) is tuple and len(value) == len(keys), 'METRIC_FIELDS')
    _need(all(type(p) is tuple and len(p) == 2 and type(p[0]) is str
              for p in value), 'METRIC_FIELDS')
    result = dict(value)
    _need(set(result) == set(keys), 'METRIC_FIELDS')
    return result


# @anchor:wa.learn.script_runtime.StageMeasurement
@_dataclass(frozen=True, slots=True, repr=False)
class StageMeasurement:
    duration_ns: int | None
    provenance: str | None
    reason: str | None
    censored: bool

    # @anchor:wa.learn.script_runtime.StageMeasurement.as_dict
    def as_dict(self, *, unit='ns'):
        """Serialize resident bytes using value/unit, never duration_ns."""
        _need(unit in ('ns', 'bytes'), 'METRIC_UNIT')
        _need(type(self.censored) is bool, 'METRIC_CENSORED')
        if self.duration_ns is None:
            _code(self.reason, _REASONS)
            _need(self.provenance is None, 'METRIC_UNAVAILABLE_PROVENANCE')
        else:
            _quantity(self.duration_ns)
            _code(self.provenance, _PROVENANCE)
            _need(self.provenance == ('owner_monotonic' if unit == 'ns'
                                     else 'owner_accounting'), 'METRIC_PROVENANCE')
            _need(self.reason is None, 'METRIC_AVAILABLE_REASON')
        return {'value': self.duration_ns, 'unit': unit,
                'provenance': self.provenance, 'reason': self.reason,
                'censored': self.censored}


# @anchor:wa.learn.script_runtime.EpisodeMetrics
@_dataclass(frozen=True, slots=True, repr=False)
class EpisodeMetrics:
    success: bool | None
    muse_calls: int | None
    script_hits: int
    script_suspends: int
    latency_ns: tuple
    resident_script_bytes: tuple
    cleanup_complete: bool
    missing_reasons: tuple

    # @anchor:wa.learn.script_runtime.EpisodeMetrics.to_json
    def to_json(self, *, max_record_bytes, terminal_owner=None, terminal_evidence=None):
        """Validate a closed, bounded schema without serializing host evidence.

        terminal_owner is a trusted constructor dependency, not model content.
        Successful serialization is not a runtime-readiness certificate.
        """
        _quantity(max_record_bytes)
        _need(max_record_bytes > 0, 'METRIC_RECORD_BOUND')
        _need(self.success is None or type(self.success) is bool, 'METRIC_SUCCESS')
        _need(type(self.cleanup_complete) is bool, 'METRIC_CLEANUP')
        _quantity(self.script_hits)
        _quantity(self.script_suspends)
        if self.muse_calls is not None:
            _quantity(self.muse_calls)
        stages = _pairs(self.latency_ns, _STAGES)
        resident = _pairs(self.resident_script_bytes, _RESIDENT)
        required = []
        if self.success is None:
            required.append('success')
        else:
            _need(terminal_evidence is not None and terminal_owner is not None,
                  'METRIC_TERMINAL_EVIDENCE')
            _need(terminal_owner.check_terminal_metric(terminal_evidence, self.success) is True,
                  'METRIC_TERMINAL_EVIDENCE')
        if self.muse_calls is None:
            required.append('muse_calls')
        for name, measurement in tuple(stages.items()) + tuple(resident.items()):
            _need(type(measurement) is StageMeasurement, 'METRIC_MEASUREMENT')
            if measurement.duration_ns is None:
                prefix = 'latency_ns.' if name in _STAGES else 'resident_script_bytes.'
                required.append(prefix + name)
        missing = _pairs(self.missing_reasons, required)
        for key, reason in missing.items():
            _code(reason, _REASONS)
            if key.startswith('latency_ns.'):
                _need(reason == stages[key.split('.')[1]].reason, 'METRIC_MISSING_REASON')
            elif key.startswith('resident_script_bytes.'):
                _need(reason == resident[key.split('.')[1]].reason, 'METRIC_MISSING_REASON')
            else:
                _need(reason == ('terminal_evidence_unavailable' if key == 'success'
                                 else 'provider_counter_unavailable'), 'METRIC_MISSING_REASON')
        row = {'schema': 'wender.scripts.episode/v1', 'success': self.success,
               'muse_calls': self.muse_calls, 'script_hits': self.script_hits,
               'script_suspends': self.script_suspends,
               'latency_ns': {key: stages[key].as_dict() for key in _STAGES},
               'resident_script_bytes': {key: resident[key].as_dict(unit='bytes')
                                         for key in _RESIDENT},
               'cleanup_complete': self.cleanup_complete, 'missing_reasons': missing}
        result = _json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n'
        _need(len(result.encode('utf-8')) <= max_record_bytes, 'METRIC_RECORD_BOUND')
        return result


# @anchor:wa.learn.script_runtime.MuseAttemptCounter
class MuseAttemptCounter:
    """Count actual transport entries, including exceptions, with constant storage.

    Wrap the actual provider invocation, not budget charging or model proposals.
    This utility does not supply S8's generation transport or live readiness.
    """

    # @anchor:wa.learn.script_runtime.MuseAttemptCounter.__init__
    def __init__(self):
        self._attempts = 0
        self._lock = _RLock()

    # @anchor:wa.learn.script_runtime.MuseAttemptCounter.invoke
    def invoke(self, provider, *args, **kwargs):
        _need(callable(provider), 'PROVIDER_UNAVAILABLE')
        with self._lock:
            self._attempts += 1
        return provider(*args, **kwargs)

    # @anchor:wa.learn.script_runtime.MuseAttemptCounter.attempts
    def attempts(self):
        with self._lock:
            return self._attempts



from dataclasses import replace as _replace
from wender.learn import script_integration as _integration
from wender.learn import script_policy as _policy
from wender.learn import context_router as _context
from wender.learn import script_generator as _generation
from wender.scripts import compiler as _compiler, types as _types, runner as _runner


# @anchor:wa.learn.script_runtime.RuntimeConfiguration
@_dataclass(frozen=True, slots=True, repr=False)
class RuntimeConfiguration:
    """Trusted constructor data. Bounds have no implicit production defaults."""
    mode: str
    policies: tuple
    model_identity: object
    absolute_deadline_ns: int
    max_record_bytes: int
    max_registrations: int

    # @anchor:wa.learn.script_runtime.RuntimeConfiguration.__post_init__
    def __post_init__(self):
        _need(self.mode in ('offline', 'live'), 'RUNTIME_MODE')
        _need(type(self.policies) is tuple and all(type(p) is tuple and len(p) == 2
              and type(p[0]) is str and p[1] is not None for p in self.policies), 'RUNTIME_POLICIES')
        _need(len(dict(self.policies)) == len(self.policies), 'RUNTIME_POLICIES')
        for bound in (self.absolute_deadline_ns, self.max_record_bytes, self.max_registrations):
            _need(type(bound) is int and bound > 0, 'RUNTIME_BOUND')
        _need(self.model_identity is not None, 'RUNTIME_MODEL')


# @anchor:wa.learn.script_runtime.RuntimeServices
@_dataclass(frozen=True, slots=True, repr=False)
class RuntimeServices:
    """Registered trusted ports, never discovered from an observation or source.

    integration implements the S9 owner checks; observation implements S9's
    observation host. actions supplies the shared final input fence. cleanup
    drains exact identities, not owner counts. All readiness remains independent
    of construction and is checked by integration.check_services.
    """
    integration: object
    observation: object
    acquisition: object
    actions: object
    budget: object
    cleanup: object
    context: object
    generation: object
    accounting: object
    telemetry: object
    owned: tuple


# @anchor:wa.learn.script_runtime.ArtifactCustody
@_dataclass(frozen=True, slots=True, eq=False, repr=False)
class ArtifactCustody:
    plan: object
    source_session: object
    admission: object
    inputs: object
    registry: object
    scope: object
    authority: object
    cleanup_registration: object


# @anchor:wa.learn.script_runtime.FallbackEvidence
@_dataclass(frozen=True, slots=True, eq=False, repr=False)
class FallbackEvidence:
    episode: str
    scope: object
    observation: object
    failure_reason: object
    input_disposition: object
    checkpoint: object
    drain_evidence: object
    fresh_grant_evidence: object


# @anchor:wa.learn.script_runtime.ObservationHost
class ObservationHost:
    """Retain exact S4/S0 association supplied by a registered acquisition host."""
    # @anchor:wa.learn.script_runtime.ObservationHost.__init__
    def __init__(self, owner, *, max_registrations):
        _need(owner is not None, 'OBSERVATION_OWNER')
        _need(type(max_registrations) is int and max_registrations > 0, 'OBSERVATION_BOUND')
        self._owner, self._limit = owner, max_registrations
        self._allocations, self._evidence = {}, {}

    # @anchor:wa.learn.script_runtime.ObservationHost.reserve_observation
    def reserve_observation(self, acquisition, policy, resources):
        _need(len(self._allocations) < self._limit, 'OBSERVATION_BOUND')
        allocation = self._owner.reserve_observation(acquisition, policy, resources)
        _need(allocation is not None and id(allocation) not in self._allocations, 'OBSERVATION_ALLOCATION')
        self._allocations[id(allocation)] = (allocation, acquisition)
        return allocation

    # @anchor:wa.learn.script_runtime.ObservationHost.check_acquisition
    def check_acquisition(self, acquisition, scope, authority, allocation):
        pair = self._allocations.get(id(allocation))
        return (pair is not None and pair[0] is allocation and pair[1] is acquisition
                and self._owner.check_acquisition(acquisition, scope, authority, allocation) is True)

    # @anchor:wa.learn.script_runtime.ObservationHost.observation_arguments
    def observation_arguments(self, acquisition, scope, authority, allocation):
        _need(self.check_acquisition(acquisition, scope, authority, allocation), 'ACQUISITION')
        return self._owner.observation_arguments(acquisition, scope, authority, allocation)

    # @anchor:wa.learn.script_runtime.ObservationHost.register_observation
    def register_observation(self, acquisition, bundle, scope, authority, allocation):
        _need(self.check_acquisition(acquisition, scope, authority, allocation), 'ACQUISITION')
        evidence = self._owner.register_observation(acquisition, bundle, scope, authority, allocation)
        _need(type(evidence) is _integration.IntegrationEvidence and evidence.bundle is bundle
              and evidence.allocation is allocation and self._owner.check_evidence(evidence) is True,
              'OBSERVATION_REGISTRATION')
        self._evidence[id(evidence)] = evidence
        return evidence

    # @anchor:wa.learn.script_runtime.ObservationHost.check_evidence
    def check_evidence(self, evidence):
        return (self._evidence.get(id(evidence)) is evidence
                and self._owner.check_evidence(evidence) is True)

    # @anchor:wa.learn.script_runtime.ObservationHost.release_observation
    def release_observation(self, allocation):
        pair = self._allocations.get(id(allocation))
        _need(pair is not None and pair[0] is allocation, 'OBSERVATION_ALLOCATION')
        _need(self._owner.release_observation(allocation) is True, 'OBSERVATION_DRAIN')
        del self._allocations[id(allocation)]
        self._evidence = {k: v for k, v in self._evidence.items() if v.allocation is not allocation}
        return True

    # @anchor:wa.learn.script_runtime.ObservationHost.close
    def close(self):
        failed = False
        for allocation, _ in tuple(self._allocations.values()):
            try:
                self.release_observation(allocation)
            except Exception:
                failed = True
        return not failed and not self._allocations


# @anchor:wa.learn.script_runtime.ScriptRuntime
class ScriptRuntime:
    """Identity-backed S9 coordinator; the sealed runner remains the interpreter.

    Registered services are a trust boundary. No shape-only constructor success
    is used as evidence of attestation, restart reconciliation or parser custody.
    Host composition is intentionally separate from these owner operations.
    """
    # @anchor:wa.learn.script_runtime.ScriptRuntime.__init__
    def __init__(self, configuration, services, *, session):
        _need(type(configuration) is RuntimeConfiguration and type(services) is RuntimeServices,
              'RUNTIME_CONFIGURATION')
        _need(type(session) is _integration.IntegrationSession, 'SESSION_TYPE')
        self.configuration, self.services = configuration, services
        self._host = services.integration
        _need(self._host is not None and self._host.check_session(session) is True,
              'SESSION_REGISTRATION')
        _need(session.deadline_ns <= configuration.absolute_deadline_ns, 'DEADLINE')
        self.store = self._host.store
        self._session = session
        self._sessions = {id(session): session}
        self._artifacts, self._custodies, self._states = {}, {}, {}
        self._actions, self._consumed, self._uncertainties = {}, {}, {}
        self._owned, self._finished = {}, {}
        self._lock = _RLock()
        self._closed = self._fenced = self._miss = False
        self._reconciling = False
        self._hits = self._suspends = 0
        self._suspended = {}
        self._fallbacks = {}
        self._terminal = None
        self._cleanup_complete = False
        self._provider_instrumented = False
        self._model_pending = 0
        self._counter = MuseAttemptCounter()
        self._latencies = {}
        self._resident = {}
        _need(type(services.owned) is tuple, 'OWNER_INVENTORY')
        for owner in services.owned:
            self.register_owner(owner)
        if session.custody is not None:
            custody = session.custody
            _need(type(custody) is _integration.ExecutionCustody
                  and self._host.check_custody(session, custody) is True, 'EXECUTION_CUSTODY')
            self.register_artifact(ArtifactCustody(custody.plan, custody.source_session,
                custody.admission, custody.inputs, custody.registry, session.scope,
                session.authority, custody.cleanup_registration))
            self._retain(self._custodies, custody)
            self._retain(self._owned, custody)

    # @anchor:wa.learn.script_runtime.ScriptRuntime._policy
    def _policy(self, name):
        policy = dict(self.configuration.policies).get(name)
        _need(policy is not None, 'RUNTIME_POLICY_MISSING')
        return policy

    # @anchor:wa.learn.script_runtime.ScriptRuntime._retain
    def _retain(self, table, value):
        _need(value is not None and len(table) < self.configuration.max_registrations, 'RUNTIME_BOUND')
        table[id(value)] = value
        return value

    # @anchor:wa.learn.script_runtime.ScriptRuntime._timed
    def _timed(self, name, callback):
        start = self.clock()
        try:
            return callback()
        finally:
            elapsed = self.clock() - start
            _need(type(elapsed) is int and elapsed >= 0, 'CLOCK')
            self._latencies[name] = self._latencies.get(name, 0) + elapsed

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_ready
    def check_ready(self):
        try:
            session = self._session
            return (not self._reconciling and not self.stopped() and self.check_session(session) and self.check_restart(session)
                and session.restart.open is True and not session.restart.unresolved
                and session.restart.reconciliation is not None and not session.unresolved_actions
                and session.restart.scope == session.scope
                and type(session.scope) is _types.ScopeIdentity
                and session.scope.attestation is not None and session.scope.attestation_reason is None
                and type(session.authority) is _types.AuthorityRevision
                and self.current_authority(session.scope) == session.authority
                and self.clock() < session.deadline_ns
                and self._host.check_services(self.services, self.configuration) is True)
        except Exception:
            return False

    # @anchor:wa.learn.script_runtime.ScriptRuntime.select_script
    def select_script(self, before, description):
        with self._lock:
            self._miss = False
            _need(self.check_ready(), 'RESTART_UNCERTAIN')
            acquisition = self.services.acquisition(before, description)
            evidence = self._timed('observe', lambda: _integration.observe(
                host=self.services.observation, acquisition=acquisition, scope=self._session.scope,
                authority=self._session.authority, policy=self._policy('observation'),
                resources=self._host.resources))
            self._retain(self._owned, evidence)
            request = self._host.resolve_request(self._session, evidence)
            selected = self._timed('select', lambda: _integration.select_lane(self._session, evidence,
                request=request, owners=self, policy=self._policy('selection')))
            if selected.lane != 'SCRIPT':
                self._miss = True  # S9 proved closure=vacio/detail=no_match.
                return None
            renewed = _replace(self._session, custody=selected.custody)
            _need(self._host.register_session(renewed) is True, 'SESSION_REGISTRATION')
            self._retain(self._sessions, renewed)
            self._session = renewed
            self._hits += 1
            return renewed

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_script_miss
    def check_script_miss(self):
        return self._miss and self.check_ready()

    # @anchor:wa.learn.script_runtime.ScriptRuntime.advance_session
    def advance_session(self, session):
        with self._lock:
            _need(self.check_ready() and session is self._session, 'SESSION_REGISTRATION')
            return self._timed('act', lambda: _integration.advance_session(session, owners=self))

    # @anchor:wa.learn.script_runtime.ScriptRuntime.runner_state
    def runner_state(self, session):
        _need(self.check_session(session), 'SESSION_REGISTRATION')
        return self._states.get(id(session))

    # @anchor:wa.learn.script_runtime.ScriptRuntime.accept_script
    def accept_script(self, session, state):
        if self._states.get(id(session)) is not state or state is None or state.outcome is None:
            return False
        outcome = state.outcome
        accepted = (self.check_ready() and self.check_terminal(session, outcome)
            and outcome.status is _types.TerminalStatus.COMPLETED
            and outcome.task_goal.value is _types.Truth.TRUE
            and outcome.checkpoint.value is _types.Truth.TRUE
            and outcome.input_disposition is not _types.InputDisposition.INPUT_UNCERTAIN
            and outcome.reconciliation_required is False)
        if accepted:
            self._terminal = outcome
        elif id(state) not in self._suspended:
            self._retain(self._suspended, state)
            self._suspends += 1
            self._fallbacks[id(session)] = FallbackEvidence(session.episode, session.scope,
                state.observation, outcome.failure, outcome.input_disposition,
                outcome.checkpoint, outcome.owner_drained, None)
            self._fenced = (self._fenced
                or outcome.input_disposition is _types.InputDisposition.INPUT_UNCERTAIN
                or outcome.reconciliation_required is not False
                or not self.check_terminal(session, outcome))
            self._miss = False
            self._model_pending = 0
        return accepted

    # @anchor:wa.learn.script_runtime.ScriptRuntime.fallback_evidence
    def fallback_evidence(self, session):
        _need(self.check_session(session), 'SESSION_REGISTRATION')
        return self._fallbacks.get(id(session))

    # @anchor:wa.learn.script_runtime.ScriptRuntime.charge_model
    def charge_model(self):
        with self._lock:
            if not self.check_ready() or self.services.budget.charge(self._session) is not True:
                return False
            self._model_pending += 1
            return True

    # @anchor:wa.learn.script_runtime.ScriptRuntime.possible_input
    def possible_input(self, action):
        with self._lock:
            _need(self.check_ready() and id(action) not in self._consumed, 'INPUT_FENCE')
            if self._actions.get(id(action)) is not action:
                _need(self.services.actions.possible_input(action) is True, 'INPUT_REGISTRATION')
                self._retain(self._actions, action)
            return True

    # @anchor:wa.learn.script_runtime.ScriptRuntime.dispatch
    def dispatch(self, surface, action, callback):
        with self._lock:
            _need(self.check_ready() and callable(callback), 'INPUT_FENCE')
            self.possible_input(action)
            _need(id(action) not in self._consumed, 'INPUT_CONSUMED')
            self._retain(self._consumed, action)
            entered = False
            # @anchor:wa.learn.script_runtime.ScriptRuntime.dispatch.once
            def once():
                nonlocal entered
                _need(not entered and self.check_ready(), 'INPUT_FENCE')
                entered = True
                return callback()
            try:
                return self._timed('act', lambda: self.services.actions.dispatch(surface, action, once))
            except BaseException:
                if entered:
                    self.uncertain(action)
                raise

    # @anchor:wa.learn.script_runtime.ScriptRuntime.verify
    def verify(self, action, before, after):
        _need(self._consumed.get(id(action)) is action and self.check_ready(), 'INPUT_REGISTRATION')
        return self._timed('verify', lambda: self.services.actions.verify(action, before, after) is True)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.uncertain
    def uncertain(self, action):
        with self._lock:
            self._fenced = True
            self._miss = False
            self._model_pending = 0
            identity = action if action is not None else self._session
            if id(identity) not in self._uncertainties:
                self._retain(self._uncertainties, identity)
                self.services.actions.uncertain(action)
            return False

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_session
    def check_session(self, session):
        return self._sessions.get(id(session)) is session and self._host.check_session(session) is True

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_restart
    def check_restart(self, session):
        return self.check_session(session) and self._host.check_restart(session) is True

    # @anchor:wa.learn.script_runtime.ScriptRuntime.stopped
    def stopped(self):
        return self._closed or self._fenced or self._host.stopped() is not False

    # @anchor:wa.learn.script_runtime.ScriptRuntime.current_authority
    def current_authority(self, scope):
        return self._host.current_authority(scope)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.clock
    def clock(self):
        value = self._host.clock()
        _need(type(value) is int and value >= 0, 'CLOCK')
        return value

    # @anchor:wa.learn.script_runtime.ScriptRuntime.register_artifact
    def register_artifact(self, custody):
        _need(type(custody) is ArtifactCustody and not self.stopped(), 'ARTIFACT_CUSTODY')
        _need(not any(value.plan is custody.plan and value is not custody
                      for value in self._artifacts.values()), 'ARTIFACT_ALREADY_REGISTERED')
        _need(custody.scope == self._session.scope and custody.authority == self._session.authority
              and custody.cleanup_registration is not None, 'ARTIFACT_SCOPE')
        _need(_compiler.verify_pairing(custody.plan, session=custody.source_session,
            admission=custody.admission, inputs=custody.inputs, registry=custody.registry) is custody.plan,
            'COMPILER_PAIRING')
        _need(self._host.check_artifact(custody) is True, 'ARTIFACT_REGISTRATION')
        _need(len(self._artifacts) < self.configuration.max_registrations
              and len(self._owned) < self.configuration.max_registrations, 'RUNTIME_BOUND')
        self._retain(self._artifacts, custody)
        self._retain(self._owned, custody)
        return custody

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_custody
    def check_custody(self, session, custody):
        return (self.check_session(session) and self._custodies.get(id(custody)) is custody
                and self._host.check_custody(session, custody) is True)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_evidence
    def check_evidence(self, evidence):
        return (self._owned.get(id(evidence)) is evidence
                and self.services.observation.check_evidence(evidence) is True)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_budget
    def check_budget(self, session, evidence, policy):
        return self.check_ready() and self._host.check_budget(session, evidence, policy) is True

    # @anchor:wa.learn.script_runtime.ScriptRuntime.select_proposal
    def select_proposal(self, session, evidence, policy):
        _need(self.check_ready(), 'RESTART_UNCERTAIN')
        return self._host.select_proposal(session, evidence, policy)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.execution_ttl
    def execution_ttl(self, session, policy):
        return self._host.execution_ttl(session, policy)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.consumer_identity
    def consumer_identity(self, session):
        _need(self.check_session(session), 'SESSION_REGISTRATION')
        return self._host.consumer_identity(session)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.execution_custody
    def execution_custody(self, session, target, lease):
        _need(self.check_ready() and self.check_session(session), 'SESSION_REGISTRATION')
        artifacts = [v for v in self._artifacts.values() if v.plan.identity == lease.artifact]
        _need(len(artifacts) == 1, 'ARTIFACT_REGISTRATION')
        artifact = artifacts[0]
        _need(_compiler.verify_pairing(artifact.plan, session=artifact.source_session,
            admission=artifact.admission, inputs=artifact.inputs, registry=artifact.registry) is artifact.plan,
            'COMPILER_PAIRING')
        runtime = self._host.execution_custody(session, target, lease)
        _need(type(runtime) is _integration.ExecutionCustody, 'EXECUTION_CUSTODY')
        _need(runtime.plan is artifact.plan and runtime.source_session is artifact.source_session
            and runtime.admission is artifact.admission and runtime.inputs is artifact.inputs
            and runtime.registry is artifact.registry and runtime.lease is lease
            and runtime.cleanup_registration is artifact.cleanup_registration, 'COMPILER_IDENTITY')
        self._retain(self._custodies, runtime)
        self._retain(self._owned, runtime)
        return runtime

    # @anchor:wa.learn.script_runtime.ScriptRuntime.abandon_lease
    def abandon_lease(self, session, lease):
        _need(self.check_session(session), 'SESSION_REGISTRATION')
        return self._host.abandon_lease(session, lease)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.execution_guard
    def execution_guard(self, session):
        _need(self.check_session(session), 'SESSION_REGISTRATION')
        return self._lock

    # @anchor:wa.learn.script_runtime.ScriptRuntime.retain_runner
    def retain_runner(self, session, state):
        _need(self.check_session(session) and id(session) not in self._states
              and state.plan is session.custody.plan and state.lease is session.custody.lease,
              'RUNNER_IDENTITY')
        self._states[id(session)] = state
        self._retain(self._owned, state)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_terminal
    def check_terminal(self, session, outcome):
        state = self._states.get(id(session))
        return (self.check_session(session) and state is not None and state.outcome is outcome
            and type(outcome) is _types.RuntimeOutcome
            and outcome.owner_drained.value is _types.Truth.TRUE
            and self._host.check_terminal(session, outcome) is True)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.finish_integration
    def finish_integration(self, session, registration):
        _need(self.check_session(session) and session.custody.cleanup_registration is registration,
              'CLEANUP_IDENTITY')
        if self._finished.get(id(registration)) is not registration:
            _need(self._host.finish_integration(session, registration) is True, 'INTEGRATION_DRAIN')
            self._retain(self._finished, registration)
        return True

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_reconciliation
    def check_reconciliation(self, session, evidence):
        return self.check_session(session) and self._host.check_reconciliation(session, evidence) is True

    # @anchor:wa.learn.script_runtime.ScriptRuntime.renew_session
    def renew_session(self, session, evidence):
        _need(self._reconciling, 'RECONCILIATION_TRANSACTION')
        _need(self.check_reconciliation(session, evidence), 'RECONCILIATION')
        renewed = self._host.renew_session(session, evidence)
        _need(type(renewed) is _integration.IntegrationSession and renewed is not session
              and self._host.check_session(renewed) is True, 'SESSION_REGISTRATION')
        _need(renewed.scope == session.scope and renewed.episode == session.episode
              and renewed.consumed_actions == session.consumed_actions
              and not renewed.unresolved_actions and renewed.restart.open is True
              and not renewed.restart.unresolved and renewed.restart.reconciliation is not None
              and renewed.restart.scope == renewed.scope
              and self._host.check_restart(renewed) is True
              and self._host.current_authority(renewed.scope) == renewed.authority
              and self.clock() < renewed.deadline_ns <= self.configuration.absolute_deadline_ns
              and self._host.check_fresh_grant(renewed, evidence) is True, 'FRESH_GRANT')
        self._retain(self._sessions, renewed)
        self._session = renewed
        self._model_pending = 0
        self._fenced = False
        self._uncertainties.clear()
        return renewed

    # @anchor:wa.learn.script_runtime.ScriptRuntime.reconcile_uncertain
    def reconcile_uncertain(self, evidence):
        with self._lock:
            _need(not self._closed and not self._reconciling, 'RECONCILIATION_TRANSACTION')
            previous, registrations = self._session, dict(self._sessions)
            self._reconciling = True
            try:
                return _integration.reconcile_uncertain(previous, evidence=evidence, owners=self)
            except Exception:
                self._session, self._sessions = previous, registrations
                self._fenced = True
                raise
            finally:
                self._reconciling = False

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_fresh_grant
    def check_fresh_grant(self, session, evidence):
        return self.check_session(session) and self._host.check_fresh_grant(session, evidence) is True

    # @anchor:wa.learn.script_runtime.ScriptRuntime.guard_drift
    def guard_drift(self, expected, actual, *, owner, checkpoint=None, planned_transition=False):
        accepted = _policy.guard_drift(expected, actual, owner=owner,
            checkpoint=checkpoint, planned_transition=planned_transition)
        if not accepted:
            self._miss = False
            self._model_pending = 0
            state = self._states.get(id(self._session))
            if state is not None and state.outcome is None:
                _runner.abort(state, _types.Reason.STALE)
            outcome = state.outcome if state is not None else None
            self._fenced = (self._fenced or outcome is None
                or outcome.input_disposition is _types.InputDisposition.INPUT_UNCERTAIN
                or outcome.reconciliation_required is not False
                or not self.check_terminal(self._session, outcome))
        return accepted

    # @anchor:wa.learn.script_runtime.ScriptRuntime.open_context
    def open_context(self, **arguments):
        _need(self.check_ready(), 'RESTART_UNCERTAIN')
        _need(arguments.get('host') is self.services.context and arguments.get('store') is self.store,
              'CONTEXT_OWNER')
        epoch = _context.open_epoch(**arguments)
        self._retain(self._owned, epoch)
        return epoch

    # @anchor:wa.learn.script_runtime.ScriptRuntime.pack_context
    def pack_context(self, epoch, evidence, **arguments):
        _need(self.check_ready() and self._owned.get(id(epoch)) is epoch, 'CONTEXT_REGISTRATION')
        try:
            return _context.pack_suffix(epoch, evidence, **arguments)
        except Exception:
            self.close_context(epoch, reason=_types.Reason.STALE)
            raise

    # @anchor:wa.learn.script_runtime.ScriptRuntime.close_context
    def close_context(self, epoch, *, reason):
        _need(self._owned.get(id(epoch)) is epoch, 'CONTEXT_REGISTRATION')
        result = _context.close_epoch(epoch, host=self.services.context, reason=reason)
        del self._owned[id(epoch)]
        return result

    # @anchor:wa.learn.script_runtime.ScriptRuntime.generate_draft
    def generate_draft(self, *, segment_arguments, transport, policy, host):
        _need(self.check_ready(), 'RESTART_UNCERTAIN')
        _need(host is self.services.generation, 'GENERATION_OWNER')
        brief = _generation.extract_segment(**segment_arguments)
        self._retain(self._owned, brief)
        return self._timed('model', lambda: _generation.generate_draft(
            brief, transport=transport, policy=policy, host=host))

    # @anchor:wa.learn.script_runtime.ScriptRuntime.submit_draft
    def submit_draft(self, draft, **arguments):
        _need(self.check_ready() and self._owned.get(id(draft.brief)) is draft.brief,
              'DRAFT_REGISTRATION')
        return _generation.submit_draft(draft, **arguments)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.measure_resident
    def measure_resident(self, boundary):
        _need(boundary in _RESIDENT, 'ACCOUNTING_BOUNDARY')
        if self.services.accounting is None:
            value = StageMeasurement(None, None, 'accounting_unavailable', False)
        else:
            amount = self.services.accounting.resident_script_bytes(self.store)
            _quantity(amount)
            value = StageMeasurement(amount, 'owner_accounting', None, False)
        self._resident[boundary] = value
        return value

    # @anchor:wa.learn.script_runtime.ScriptRuntime.close
    def close(self):
        with self._lock:
            self._closed = True
            self._miss = False
            failed = False
            for value in tuple(self._owned.values()):
                try:
                    _need(self.services.cleanup.drain(value) is True, 'OWNER_DRAIN')
                    _need(self.services.cleanup.check_drained(value) is True, 'OWNER_DRAIN')
                    del self._owned[id(value)]
                except Exception:
                    failed = True
            self._cleanup_complete = not failed and not self._owned
            if self._cleanup_complete:
                self._artifacts.clear()
                self._custodies.clear()
                self._states.clear()
                self._sessions.clear()
                self._actions.clear()
                self._consumed.clear()
                self._uncertainties.clear()
                self._suspended.clear()
                self._model_pending = 0
                self._session = _replace(self._session, custody=None, context_epoch=None, allocations=())
            return self._cleanup_complete


    # @anchor:wa.learn.script_runtime.ScriptRuntime.register_owner
    def register_owner(self, owner):
        _need(not self._closed and self.services.cleanup.check_owned(owner) is True,
              'OWNER_REGISTRATION')
        _need(id(owner) not in self._owned, 'OWNER_DUPLICATE')
        return self._retain(self._owned, owner)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.owns
    def owns(self, owner):
        return self._owned.get(id(owner)) is owner

    # @anchor:wa.learn.script_runtime.ScriptRuntime.verified_completion
    def verified_completion(self):
        return self._terminal is not None and self.check_terminal_metric(self._terminal, True)

    # @anchor:wa.learn.script_runtime.ScriptRuntime.invoke_provider
    def invoke_provider(self, provider, *args, **kwargs):
        """Use only at an actual provider invocation boundary, never a charge."""
        with self._lock:
            _need(self.check_ready(), 'RESTART_UNCERTAIN')
            _need(callable(provider) and self._model_pending > 0, 'MODEL_BUDGET')
            self._model_pending -= 1
            self._provider_instrumented = True
        return self._timed('model', lambda: self._counter.invoke(provider, *args, **kwargs))

    # @anchor:wa.learn.script_runtime.ScriptRuntime.check_terminal_metric
    def check_terminal_metric(self, evidence, success):
        return evidence is not None and evidence is self._terminal and success is True

    # @anchor:wa.learn.script_runtime.ScriptRuntime.episode_metrics
    def episode_metrics(self):
        missing = []
        success = True if self._terminal is not None else None
        calls = self._counter.attempts() if self._provider_instrumented else None
        if success is None:
            missing.append(('success', 'terminal_evidence_unavailable'))
        if calls is None:
            missing.append(('muse_calls', 'provider_counter_unavailable'))
        latency, resident = [], []
        for name in _STAGES:
            if name in self._latencies:
                value = StageMeasurement(self._latencies[name], 'owner_monotonic', None, False)
            else:
                value = StageMeasurement(None, None, 'instrumentation_unavailable', False)
                missing.append(('latency_ns.' + name, value.reason))
            latency.append((name, value))
        for name in _RESIDENT:
            value = self._resident.get(name, StageMeasurement(None, None, 'accounting_unavailable', False))
            resident.append((name, value))
            if value.duration_ns is None:
                missing.append(('resident_script_bytes.' + name, value.reason))
        row = EpisodeMetrics(success, calls, self._hits, self._suspends, tuple(latency),
            tuple(resident), self._cleanup_complete, tuple(missing))
        return row.to_json(max_record_bytes=self.configuration.max_record_bytes,
                           terminal_owner=self, terminal_evidence=self._terminal)


from functools import wraps as _wraps


# @anchor:wa.learn.script_runtime._transport_boundary
def _transport_boundary(function):
    # @anchor:wa.learn.script_runtime._transport_boundary.bounded
    @_wraps(function)
    def bounded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            pass
        raise ValueError('GENERATION_UNAVAILABLE') from None
    return bounded


import os as _os
import selectors as _selectors
import signal as _signal
import subprocess as _subprocess


# @anchor:wa.learn.script_runtime.MuseGenerationTransport
class MuseGenerationTransport:
    """Bounded subprocess transport with a trusted command/decoder adapter.

    command(schema, association) supplies reviewed argv for the installed Muse
    version. decode(raw, association) extracts its source-schema response. Neither
    port is selected by generated source. stderr is drained and discarded.
    The transport owns its process group from spawn through join, including
    descendants keeping a pipe open after the leader exits.
    """
    # @anchor:wa.learn.script_runtime.MuseGenerationTransport.__init__
    def __init__(self, *, command, decode, clock, cancelled, counter,
                 poll_ns, join_ns, wire_bytes):
        _need(all(callable(v) for v in (command, decode, clock, cancelled)), 'TRANSPORT_PORTS')
        _need(type(counter) is MuseAttemptCounter, 'TRANSPORT_COUNTER')
        for bound in (poll_ns, join_ns, wire_bytes):
            _need(type(bound) is int and bound > 0, 'TRANSPORT_BOUND')
        self._command, self._decode = command, decode
        self._clock, self._cancelled, self._counter = clock, cancelled, counter
        self._poll, self._join, self._wire = poll_ns, join_ns, wire_bytes
        self._lock = _RLock()
        self._process = None
        self._drained = True

    # @anchor:wa.learn.script_runtime.MuseGenerationTransport._eligible
    def _eligible(self, association, policy, host, response=None):
        _need(self._cancelled() is False, 'GENERATION_CANCELLED')
        now = self._clock()
        _need(type(now) is int and now < policy.absolute_deadline_ns, 'GENERATION_DEADLINE')
        accepted = host.check(association, policy, response)
        _need(type(accepted) is _types.AdmissionCheck and accepted.result is _types.Truth.TRUE,
              'GENERATION_ASSOCIATION')

    # @anchor:wa.learn.script_runtime.MuseGenerationTransport._stop
    def _stop(self, process):
        # SIGKILL bounds even a child ignoring termination or retaining stdout.
        # Guard: a non-int (mocked) or <= 1 pid makes killpg act on every process (kill(-1)); never signal our own group.
        if type(process.pid) is int and process.pid > 1 and process.pid != _os.getpgid(0):
            try:
                _os.killpg(process.pid, _signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=self._join / 1_000_000_000)
        self._drained = process.poll() is not None

    # @anchor:wa.learn.script_runtime.MuseGenerationTransport.exchange
    @_transport_boundary
    def exchange(self, payload, *, schema, association, policy, host):
        with self._lock:
            _need(type(policy) is _generation.GenerationPolicy, 'GENERATION_POLICY')
            _need(type(payload) is bytes and type(schema) is bytes and bool(schema), 'GENERATION_REQUEST')
            _need(type(policy.request_bytes) is int and policy.request_bytes > 0
                and len(payload) + len(schema) <= policy.request_bytes
                and type(policy.response_bytes) is int and policy.response_bytes > 0,
                'GENERATION_BOUND')
            self._eligible(association, policy, host)
            argv = self._command(schema, association)
            _need(type(argv) is tuple and bool(argv)
                and all(type(v) is str and bool(v) for v in argv), 'GENERATION_COMMAND')
            process = None
            selector = _selectors.DefaultSelector()
            raw = bytearray()
            try:
                self._eligible(association, policy, host)
                process = self._counter.invoke(_subprocess.Popen, argv,
                    stdin=_subprocess.PIPE, stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
                    start_new_session=True)
                self._process, self._drained = process, False
                for pipe, events in ((process.stdin, _selectors.EVENT_WRITE),
                                     (process.stdout, _selectors.EVENT_READ),
                                     (process.stderr, _selectors.EVENT_READ)):
                    _os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, events)
                offset, collected = 0, 0
                while selector.get_map():
                    self._eligible(association, policy, host)
                    timeout_ns = min(self._poll, policy.absolute_deadline_ns - self._clock())
                    for key, event in selector.select(max(0, timeout_ns) / 1_000_000_000):
                        pipe = key.fileobj
                        if pipe is process.stdin:
                            if offset < len(payload):
                                try:
                                    offset += _os.write(pipe.fileno(), payload[offset:offset + 4096])
                                except BlockingIOError:
                                    continue
                            if offset == len(payload):
                                selector.unregister(pipe)
                                pipe.close()
                        else:
                            try:
                                data = _os.read(pipe.fileno(), min(4096, self._wire - collected + 1))
                            except BlockingIOError:
                                continue
                            if not data:
                                selector.unregister(pipe)
                                pipe.close()
                                continue
                            collected += len(data)
                            _need(collected <= self._wire, 'GENERATION_WIRE_BOUND')
                            if pipe is process.stdout:
                                raw.extend(data)
                                _need(len(raw) <= self._wire, 'GENERATION_WIRE_BOUND')
                remaining = policy.absolute_deadline_ns - self._clock()
                _need(remaining > 0, 'GENERATION_DEADLINE')
                process.wait(timeout=min(self._join, remaining) / 1_000_000_000)
                _need(process.returncode == 0, 'GENERATION_PROVIDER_FAILED')
                self._eligible(association, policy, host)
                response = self._decode(bytes(raw), association)
                _need(type(response) is bytes and len(response) <= policy.response_bytes,
                      'GENERATION_RESPONSE_BOUND')
                self._eligible(association, policy, host, response)
                return response
            except Exception:
                # A public code must not leak argv, source, stderr or raw errors.
                raise ValueError('GENERATION_UNAVAILABLE') from None
            finally:
                selector.close()
                if process is not None:
                    try:
                        self._stop(process)
                    finally:
                        for pipe in (process.stdin, process.stdout, process.stderr):
                            if not pipe.closed:
                                pipe.close()
                        if self._drained:
                            self._process = None

    # @anchor:wa.learn.script_runtime.MuseGenerationTransport.close
    def close(self):
        with self._lock:
            if self._process is not None:
                try:
                    self._stop(self._process)
                except Exception:
                    return False
                if self._drained:
                    self._process = None
            return self._drained




# @anchor:wa.learn.script_runtime.check_offline_construction
def check_offline_construction(factory):
    """Validate a supplied offline graph, then drain its exact inventory.

    No fallback factory imports test fixtures or grants production authority.
    An absent factory is reported separately from absent live capabilities.
    """
    from wender.scripts.header import SourceAdmission
    from wender.learn.script_store import ScriptStore
    from wender.learn.script_ledger import LedgerOwner
    from wender.scripts.act_api import ActAPI
    from wender.scripts.frame_wait import FrameWait
    required = {'admission': SourceAdmission, 'compiler': _compiler.CompilerRegistry,
        'store': ScriptStore, 'ledger': LedgerOwner, 'act_api': ActAPI,
        'wait': FrameWait, 'observation': ObservationHost,
        'context': ContextAdapter, 'generation': MuseGenerationTransport,
        'runtime': ScriptRuntime}
    runtime, complete, drained, constructed = None, False, False, []
    try:
        _need(callable(factory), 'OFFLINE_FACTORY_MISSING')
        runtime = factory(mode='offline')
        _need(type(runtime) is ScriptRuntime and runtime.configuration.mode == 'offline', 'OFFLINE_MODE')
        inventory = runtime.services.integration.construction_inventory()
        _need(type(inventory) is tuple and len(inventory) == len(required), 'OFFLINE_INVENTORY')
        entries = dict(inventory)
        _need(set(entries) == set(required) and entries['runtime'] is runtime, 'OFFLINE_INVENTORY')
        for name, kind in required.items():
            value = entries[name]
            _need(type(value) is kind and runtime.services.integration.check_construction_owner(name, value) is True,
                  'OFFLINE_OWNER_REGISTRATION')
            if value is not runtime:
                _need(runtime.owns(value), 'OFFLINE_OWNER_CLEANUP')
            constructed.append(name)
        complete = True
    except Exception:
        complete = False
    finally:
        if type(runtime) is ScriptRuntime:
            drained = runtime.close()
    return {'scripts': 'on', 'check': 'offline_construction',
        'construction_complete': complete, 'cleanup_complete': drained,
        'constructed_owners': constructed,
        'missing_capabilities': ['attested_live_services', 'trusted_live_policies']}


# @anchor:wa.learn.script_runtime.ContextAdapter
class ContextAdapter:
    """Explicit S8 construction/epoch owner; unavailable ports cannot open it."""
    # @anchor:wa.learn.script_runtime.ContextAdapter.__init__
    def __init__(self, *, arguments, max_epochs):
        _need(type(arguments) is dict and type(max_epochs) is int and max_epochs > 0, 'CONTEXT_CONFIGURATION')
        self._arguments = dict(arguments)
        self._epochs, self._limit = {}, max_epochs
        self._closed = False

    # @anchor:wa.learn.script_runtime.ContextAdapter.open
    def open(self):
        _need(not self._closed and len(self._epochs) < self._limit, 'CONTEXT_BOUND')
        required = {'host', 'store', 'memory', 'policy', 'display_policy', 'tokenizer',
                    'export_adapter', 'configuration', 'resources'}
        _need(set(self._arguments) == required, 'CONTEXT_PORTS_UNAVAILABLE')
        epoch = _context.open_epoch(**self._arguments)
        self._epochs[id(epoch)] = epoch
        return epoch

    # @anchor:wa.learn.script_runtime.ContextAdapter.pack
    def pack(self, epoch, evidence, **arguments):
        _need(not self._closed and self._epochs.get(id(epoch)) is epoch, 'CONTEXT_REGISTRATION')
        return _context.pack_suffix(epoch, evidence, **arguments)

    # @anchor:wa.learn.script_runtime.ContextAdapter.close
    def close(self):
        self._closed = True
        for epoch in tuple(self._epochs.values()):
            _context.close_epoch(epoch, host=self._arguments['host'], reason=_types.Reason.CANCELLED)
            del self._epochs[id(epoch)]
        return not self._epochs


# @anchor:wa.learn.script_runtime._OfflineGraph
class _OfflineGraph:
    """Explicit closed synthetic ports: no authority, parser job, input or model.

    Numeric policies below are synthetic construction limits [TARGET], never
    live defaults. No method upgrades this object to a production owner.
    """
    # @anchor:wa.learn.script_runtime._OfflineGraph.__init__
    def __init__(self):
        self.inventory = {}
        self.drained = {}
        self.session = None
        self.closed = False

    # @anchor:wa.learn.script_runtime._OfflineGraph.deny
    def deny(self, *args, **kwargs):
        return _types.AdmissionCheck(_types.Truth.FALSE, _types.Reason.MISSING)

    # @anchor:wa.learn.script_runtime._OfflineGraph.unavailable
    def unavailable(self, *args, **kwargs):
        raise ValueError('OFFLINE_SERVICE_UNAVAILABLE')

    # @anchor:wa.learn.script_runtime._OfflineGraph.now_ns
    def now_ns(self):
        return 1

    # @anchor:wa.learn.script_runtime._OfflineGraph.clock
    def clock(self):
        return self.now_ns()

    # @anchor:wa.learn.script_runtime._OfflineGraph.check_session
    def check_session(self, session):
        return self.session is session

    # @anchor:wa.learn.script_runtime._OfflineGraph.check_services
    def check_services(self, *args):
        return False

    # @anchor:wa.learn.script_runtime._OfflineGraph.stopped
    def stopped(self):
        return self.closed

    # @anchor:wa.learn.script_runtime._OfflineGraph.check_restart
    def check_restart(self, session):
        return False

    # @anchor:wa.learn.script_runtime._OfflineGraph.current_authority
    def current_authority(self, scope):
        return self.authority

    # @anchor:wa.learn.script_runtime._OfflineGraph.check_owned
    def check_owned(self, owner):
        return any(value is owner for value in self.inventory.values())

    # @anchor:wa.learn.script_runtime._OfflineGraph.construction_inventory
    def construction_inventory(self):
        return tuple(self.inventory.items())

    # @anchor:wa.learn.script_runtime._OfflineGraph.check_construction_owner
    def check_construction_owner(self, name, owner):
        return self.inventory.get(name) is owner

    # @anchor:wa.learn.script_runtime._OfflineGraph.drain
    def drain(self, owner):
        # Close the root first so no dependent port can begin new work.
        self.closed = True
        if self.drained.get(id(owner)) is owner:
            return True
        name = next((key for key, value in self.inventory.items() if value is owner), None)
        _need(name is not None and name != 'runtime', 'OFFLINE_OWNER_IDENTITY')
        if name in ('context', 'observation', 'generation'):
            _need(owner.close() is True, 'OFFLINE_DRAIN')
        elif name == 'admission':
            _need(owner.outstanding() == (), 'OFFLINE_DRAIN')
        elif name == 'compiler':
            _need(owner.outstanding() == ((), ()), 'OFFLINE_DRAIN')
        elif name in ('act_api', 'wait'):
            _need(owner.outstanding() == (), 'OFFLINE_DRAIN')
        elif name == 'ledger':
            snapshot = owner.snapshot()
            _need(snapshot[0] == (), 'OFFLINE_DRAIN')
            proof = object()
            result = owner.retire(generation=snapshot[3], proof=proof,
                check_drain=lambda producer, value: _types.AdmissionCheck(
                    _types.Truth.TRUE if value is proof and self.closed else _types.Truth.FALSE, None))
            _need(result.result is _types.Truth.TRUE, 'OFFLINE_DRAIN')
        # The empty store never issued a reservation, lease or snapshot: its
        # reservation/export/attempt ports below have refused since creation.
        self.drained[id(owner)] = owner
        return True

    # @anchor:wa.learn.script_runtime._OfflineGraph.check_drained
    def check_drained(self, owner):
        return self.drained.get(id(owner)) is owner and self.closed


# @anchor:wa.learn.script_runtime.build_offline_runtime
def build_offline_runtime(*, mode):
    """Build real sealed owners with closed, explicitly synthetic dependencies.

    No live surface or credentials are used. Missing live attestation and restart
    proof deliberately keep check_ready false even after construction succeeds.
    """
    from wender.scripts.header import SourceAdmission
    from wender.learn.script_store import ScriptStore
    from wender.learn.script_ledger import LedgerOwner, LedgerPolicy
    from wender.scripts.act_api import ActAPI, _InputInterlock, _ExecutionPolicy
    from wender.scripts.frame_wait import FrameWait
    from types import SimpleNamespace
    _need(mode == 'offline', 'OFFLINE_MODE')
    root = _OfflineGraph()
    owner = _types.PrivateRef('synthetic-offline', 'synthetic-offline', 'construction')
    scope = _types.ScopeIdentity('synthetic', 'synthetic', 'synthetic',
        'https://offline.invalid', 'https://offline.invalid', ('https://offline.invalid',),
        1, None, _types.Reason.MISSING)
    authority = _types.AuthorityRevision(1, 1, 1, None, _types.Reason.MISSING, 1, 1)
    root.authority = authority
    endpoint = _types.RegistryRef('offline-endpoint', owner.incarnation, 1, owner)
    boundary = _types.RegistryRef('offline-boundary', owner.incarnation, 1, owner)
    cancellation = _types.CancellationPolicy(1, 'offline', 'offline', 'offline', 2, 3, 1, 1048576)
    resources = _types.ResourcePolicy(16777216, 8388608, 1048576, 1, 1000, 1, 1, cancellation, 1)
    source = _types.SourcePolicy(16384, 65536, 64)
    parser = _types.ParserPolicy(1024, 8, 32, 1000, 134217728, 'offline-unavailable')
    slots = _types.SlotPolicy(1, 4096, 16384, 4096, 16384)
    closed = SimpleNamespace(charge=root.deny, reserve=root.unavailable,
        release=root.deny, check=root.deny, request=root.unavailable, verify=root.deny,
        check_plan=root.deny, check_installation=root.deny, check_drained=root.deny)
    admission = SourceAdmission(scope=scope, owner=owner, source_policy=source,
        parser_policy=parser, slot_policy=slots, resource_policy=resources, clock=root,
        attempts=closed, reservations=closed, cancellation=closed,
        resolver=root.unavailable, verifier=root.unavailable, peak_bound=root.unavailable,
        telemetry=lambda: 0, result_decoder=root.unavailable, endpoint=endpoint,
        boundary=boundary, holder_factory=root.unavailable, accounting_revision=1,
        absolute_deadline_ns=1000000000)
    registry = _compiler.CompilerRegistry(compiler=owner, incarnation=owner.incarnation,
        generation=1, max_entries=1, max_bytes=262144, entry_bytes=262144, allocation=closed)
    store = ScriptStore(owner=owner, incarnation=owner.incarnation, generation=1,
        compiler=owner, validation_registry=registry,
        store_policy=_types.StorePolicy(16777216, 1, 4194304, 1, 800),
        resolver_policy=_types.ResolverPolicy(1, 1, 1000), resource_policy=resources,
        slot_policy=slots, clock=root, attempts=closed, reservations=closed,
        cancellation=closed, endpoint=endpoint, boundary=boundary,
        holder_factory=root.unavailable, telemetry=lambda: 0,
        scope_check=root.deny, screen_check=root.deny)
    root.store = store
    digest = '0' * 64  # Public synthetic identity; no private value is hashed.
    abis = _types.ABISet('offline', 'offline', 'offline', 'offline', 'offline')
    artifact = _types.ArtifactIdentity(_types.PlanDigest('sha256', digest),
        _types.MetadataDigest('sha256', digest), _types.ArtifactDigest('sha256', digest),
        'synthetic-offline', 1, scope, 'offline', _types.PlanDigest('sha256', digest),
        'offline', _types.PlanDigest('sha256', digest), abis)
    ledger = LedgerOwner(producer=owner, run_id='synthetic', episode_id='synthetic',
        artifact=artifact, authority=authority,
        policy=LedgerPolicy(8388608, 1, 262144, 32768, 4194304, 32, 128, 64, 8192, 1, 1000000000),
        clock=root.clock, check_intent=root.deny, check_event=root.deny,
        check_seal=root.deny, check_recovery=root.deny, check_reconciliation=root.deny)
    contract = _types.HolderContract('offline', _types.Purpose.QUERY, scope, 1,
        1000000000, endpoint, boundary, cancellation)
    offline_wait_boundary = SimpleNamespace(check_installation=lambda observations, scheduler, control:
        _types.AdmissionCheck(_types.Truth.TRUE if observations is closed and scheduler is closed
            and control is closed else _types.Truth.FALSE, None))
    wait = FrameWait(observations=closed, scheduler=closed, control=closed,
        cancellation=closed, holder_contract=contract, contexts=closed, quiet=closed,
        boundary=offline_wait_boundary, clock=root.clock, max_notification_bytes=1)
    interlock_registration = []
    # @anchor:wa.learn.script_runtime.build_offline_runtime.install
    def install(value):
        if type(value) is _InputInterlock and not interlock_registration and not root.closed:
            interlock_registration.append(value)
        accepted = bool(interlock_registration) and interlock_registration[0] is value and not root.closed
        return _types.AdmissionCheck(_types.Truth.TRUE if accepted else _types.Truth.FALSE, None)
    installation = SimpleNamespace(check=install, check_drained=root.deny)
    interlock = _InputInterlock(installation=installation, max_pending=1)
    action_policy = SimpleNamespace(script_interlock=interlock, dispatch=root.unavailable)
    act = ActAPI(store=store, ledger=ledger, binding_owner=closed, binding_registry=closed,
        contexts=closed, risk=closed, authority=closed, wire=closed, application=closed,
        interlock=interlock, slots=closed, frame_wait=wait, clock=root.clock,
        cancellation=closed, reservations=closed, action_policy=action_policy,
        control=closed, requests=closed, resource_policy=resources,
        policy=_ExecutionPolicy(1, 1, 1, 1024, 1024, 1024, 1, 1, 1, 1, 1))
    observation = ObservationHost(closed, max_registrations=1)
    context = ContextAdapter(arguments={}, max_epochs=1)
    generation = MuseGenerationTransport(command=root.unavailable, decode=root.unavailable,
        clock=root.clock, cancelled=lambda: True, counter=MuseAttemptCounter(),
        poll_ns=1000000, join_ns=100000000, wire_bytes=65536)
    root.inventory = dict(admission=admission, compiler=registry, store=store, ledger=ledger,
        act_api=act, wait=wait, observation=observation, context=context, generation=generation)
    session = _integration.IntegrationSession('synthetic', scope, authority, 1000000000,
        1, _integration.RestartState(False, scope, (), None, 'live_reconciliation_unavailable'),
        None, None, (), (), None, ())
    root.session = session
    configuration = RuntimeConfiguration('offline', (('observation', source), ('selection', resources)),
        'synthetic-no-model', 1000000000, 8192, 32)
    services = RuntimeServices(root, observation, root.unavailable, closed, closed,
        root, context, generation, None, None, tuple(root.inventory.values()))
    runtime = ScriptRuntime(configuration, services, session=session)
    root.inventory['runtime'] = runtime
    return runtime


# @anchor:wa.learn.script_runtime.build_runtime
def build_runtime(*, mode):
    """Construct the sealed graph; absent live grants remain explicitly closed.

    Production attestation, reconciliation and numeric policies are not supplied
    by these CLIs. Construction cannot invent them: the resulting offline graph
    is injected but check_ready rejects live work until trusted ports are supplied.
    Callers with registered live ports can use the runtime_factory composition
    port. Synthetic limits never become production configuration.
    """
    _need(mode in ('live', 'offline'), 'RUNTIME_MODE')
    return build_offline_runtime(mode='offline')
