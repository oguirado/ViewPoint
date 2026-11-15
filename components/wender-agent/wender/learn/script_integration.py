# @module {"id":"wa.learn.script_integration","layer":3,"owner":"LEARN","deps":["wa.learn.catalogue","wa.learn.resolver","wa.learn.script_policy","wa.perceive.script_observation","wa.scripts.runner","wa.scripts.types"],"book":"HB-WA-s3-remember"}
"""Composition boundary over sealed owners, never a second interpreter.

Origin: scripts_mesh/build/spec/s9_w.md. `owners` is a constructor-injected
host port, never a source-data field. Its check methods return literal True
only for retained registrations. No production implementation is supplied.
"""
from dataclasses import dataclass as _dataclass
from functools import wraps as _wraps
from wender.scripts import types as _t, runner as _runner
from wender.learn import catalogue as _catalogue, resolver as _resolver
from wender.perceive import script_observation as _observations
from .script_policy import _Immutable


class _Unavailable(ValueError):
    pass


def _closed(function):
    @_wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except _Unavailable as error:
            code = error.args[0]
        except Exception:
            code = 'INTEGRATION_UNAVAILABLE'
        raise ValueError(code) from None
    return call


def _need(value, code):
    if value is not True:
        raise _Unavailable(code)


# @anchor:wa.learn.script_integration.IntegrationEvidence
@_dataclass(frozen=True, slots=True, repr=False)
class IntegrationEvidence(_Immutable):
    scope: object
    authority: object
    observation: object
    bundle: object
    inventory: object
    blockers_clear: bool | None
    known_screen: bool | None
    binding: object
    export_approvals: tuple
    allocation: object


# @anchor:wa.learn.script_integration.ExecutionCustody
@_dataclass(frozen=True, slots=True, repr=False)
class ExecutionCustody(_Immutable):
    plan: object
    source_session: object
    admission: object
    inputs: object
    registry: object
    lease: object
    runtime: object
    cleanup_registration: object


# @anchor:wa.learn.script_integration.RestartState
@_dataclass(frozen=True, slots=True, repr=False)
class RestartState(_Immutable):
    open: bool
    scope: object
    unresolved: tuple
    reconciliation: object
    missing_reason: str | None


# @anchor:wa.learn.script_integration.IntegrationSession
@_dataclass(frozen=True, slots=True, repr=False)
class IntegrationSession(_Immutable):
    episode: str
    scope: object
    authority: object
    deadline_ns: int
    arrival_ns: int
    restart: RestartState
    custody: ExecutionCustody | None
    context_epoch: object
    consumed_actions: tuple
    unresolved_actions: tuple
    model_budget: object
    allocations: tuple


# @anchor:wa.learn.script_integration.LaneSelection
@_dataclass(frozen=True, slots=True, repr=False)
class LaneSelection(_Immutable):
    lane: str | None
    tier: int | None
    target: object
    reason: str | None
    observation: object
    authority: object
    evidence: tuple
    custody: ExecutionCustody | None = None


def _current(session, owners):
    _need(type(session) is IntegrationSession, 'SESSION_TYPE')
    _need(owners.check_session(session) is True, 'SESSION_REGISTRATION')
    _need(type(session.restart) is RestartState and session.restart.open is True
          and session.restart.scope == session.scope and not session.restart.unresolved
          and session.restart.reconciliation is not None and not session.unresolved_actions,
          'RESTART_UNCERTAIN')
    _need(owners.check_restart(session) is True, 'RESTART_REGISTRATION')
    _need(owners.stopped() is False, 'STOP')
    _need(owners.current_authority(session.scope) == session.authority, 'STALE_AUTHORITY')
    _need(type(session.deadline_ns) is int and owners.clock() < session.deadline_ns, 'DEADLINE')


def _custody_current(session, custody, owners):
    _need(type(custody) is ExecutionCustody, 'EXECUTION_CUSTODY')
    runtime, lease = custody.runtime, custody.lease
    _need(custody.plan.identity == lease.artifact and custody.plan.scope == session.scope
          and lease.scope == session.scope and lease.episode_id == session.episode
          and lease.authority_revision == session.authority
          and runtime.scope == session.scope and runtime.authority == session.authority
          and runtime.episode_id == session.episode
          and runtime.deadline_ns <= session.deadline_ns
          and lease.absolute_deadline_ns <= session.deadline_ns
          and custody.cleanup_registration is not None
          and owners.check_custody(session, custody) is True, 'EXECUTION_CUSTODY')


# @anchor:wa.learn.script_integration.observe
@_closed
def observe(*, host, acquisition, scope, authority, policy, resources):
    """Acquisition owner supplies bounded display and separate full inventory.

    Host port: reserve_observation/check_acquisition/observation_arguments/
    register_observation/release_observation. Registration binds both families.
    The reservation is retained with successful evidence, not prematurely freed.
    """
    allocation = host.reserve_observation(acquisition, policy, resources)
    try:
        _need(allocation is not None, 'OBSERVATION_RESERVATION')
        _need(type(scope) is _t.ScopeIdentity and type(authority) is _t.AuthorityRevision
              and scope.attestation is not None and scope.attestation_reason is None, 'SCOPE_ATTESTATION')
        _need(host.check_acquisition(acquisition, scope, authority, allocation) is True, 'ACQUISITION')
        args = host.observation_arguments(acquisition, scope, authority, allocation)
        required = ('observation_id', 'publication_id', 'scope_identity', 'raw_elements', 'producer_gen16',
                    'srf_64', 'turn_index', 'scope_complete', 'stream_truncated', 'turns',
                    'join_result', 'ax_matches', 'transform_meta', 'policy_revision', 'budget_port')
        _need(type(args) is dict and set(args) == set(required), 'OBSERVATION_FIELDS')
        p = args['scope_identity']
        _need((p.principal_id, p.tenant_id, p.profile_incarnation, p.top_level_origin,
               p.target_frame_origin, p.frame_ancestry, p.scope_revision) ==
              (scope.principal, scope.tenant, scope.browser_profile_incarnation, scope.top_level_origin,
               scope.target_frame_origin, scope.frame_ancestry, scope.scope_revision), 'SCOPE_MAPPING')
        bundle = _observations.build(**args)
        evidence = host.register_observation(acquisition, bundle, scope, authority, allocation)
        _need(type(evidence) is IntegrationEvidence and evidence.bundle is bundle
              and evidence.scope == scope and evidence.authority == authority
              and type(evidence.observation) is _t.ObservationRef
              and evidence.observation.scope == scope and evidence.observation.authority == authority
              and evidence.observation.publication_identity == args['publication_id']
              and evidence.allocation is allocation and evidence.inventory is not None
              and host.check_evidence(evidence) is True, 'OBSERVATION_REGISTRATION')
        return evidence
    except BaseException:
        host.release_observation(allocation)
        raise


# @anchor:wa.learn.script_integration.select_lane
@_closed
def select_lane(session, evidence, *, request, owners, policy):
    _current(session, owners)
    _need(type(evidence) is IntegrationEvidence and owners.check_evidence(evidence) is True,
          'EVIDENCE_REGISTRATION')
    _need((evidence.scope, evidence.authority) == (session.scope, session.authority)
          and evidence.blockers_clear is True and evidence.known_screen is True
          and evidence.inventory is not None and evidence.binding is not None, 'SAFETY_EVIDENCE')
    _need(owners.check_budget(session, evidence, policy) is True, 'ROUTE_BUDGET')
    _need(type(request) is _resolver.ResolveRequest and request.scope == session.scope
          and request.episode_id == session.episode and request.authority_revision == session.authority
          and request.shadow_mode is False, 'REQUEST_SCOPE')
    snap = _catalogue.snapshot(owners.store, session.scope)
    resolved = _resolver.resolve(owners.store, request, catalogue_snapshot=snap)
    if resolved.closure == 'vacio':
        _need(resolved.detail == 'no_match', 'SCRIPT_NOT_ORDINARY_MISS')
        # Pure host proposal only; final input authorization is still mandatory.
        selection = owners.select_proposal(session, evidence, policy)
        _need(type(selection) is LaneSelection and selection.lane in
              ('STRUCTURAL', 'TEXT', 'VISUAL', 'MUSE') and selection.custody is None
              and selection.authority == session.authority
              and selection.observation == evidence.observation, 'PROPOSAL_SCOPE')
        return selection
    ttl = owners.execution_ttl(session, policy)
    _need(type(ttl) is int and 0 < ttl <= session.deadline_ns - owners.clock(), 'LEASE_TTL')
    lease = owners.store.acquire(resolved.candidate, scope=session.scope,
        authority_revision=session.authority, episode_id=session.episode,
        consumer_kind='dispatch', consumer_identity=owners.consumer_identity(session),
        purpose=_t.Purpose.QUERY, ttl_ns=ttl)
    try:
        custody = owners.execution_custody(session, resolved.candidate, lease)
        _need(type(custody) is ExecutionCustody and custody.lease is lease, 'EXECUTION_CUSTODY')
        _custody_current(session, custody, owners)
        _need(custody.runtime.observation is evidence.observation, 'OBSERVATION_CUSTODY')
        return LaneSelection('SCRIPT', 0, resolved.candidate, None, evidence.observation,
                             session.authority, (evidence.binding,), custody)
    except BaseException:
        # Owner performs cancellation/acknowledged release; do not forge drain.
        owners.abandon_lease(session, lease)
        raise


# @anchor:wa.learn.script_integration.advance_session
@_closed
def advance_session(session, *, owners):
    """`finish_integration` must idempotently drain its registered exact owners.

    Runner owns its own seal and lease releases; none are repeated here.
    """
    _current(session, owners)
    custody = session.custody
    _custody_current(session, custody, owners)
    # Owner serializes this public operation across threads. It retains exact
    # state identity and forbids starting another runner for consumed custody.
    with owners.execution_guard(session):
        state = owners.runner_state(session)
        if state is None:
            state = _runner.start(custody.plan, lease=custody.lease,
                session=custody.source_session, admission=custody.admission,
                inputs=custody.inputs, registry=custody.registry, runtime=custody.runtime)
            owners.retain_runner(session, state)
        _need(state.plan is custody.plan and state.lease is custody.lease, 'RUNNER_IDENTITY')
        if state.outcome is None:
            _runner.advance(state)
        if state.outcome is not None:
            _need(owners.clock() < session.deadline_ns, 'DEADLINE')
            _need(owners.check_terminal(session, state.outcome) is True, 'TERMINAL_NOT_DRAINED')
            owners.finish_integration(session, custody.cleanup_registration)
        return state


# @anchor:wa.learn.script_integration.reconcile_uncertain
@_closed
def reconcile_uncertain(session, *, evidence, owners):
    _need(type(session) is IntegrationSession and owners.check_session(session) is True,
          'SESSION_REGISTRATION')
    _need(owners.check_reconciliation(session, evidence) is True, 'RECONCILIATION')
    renewed = owners.renew_session(session, evidence)
    _need(type(renewed) is IntegrationSession and renewed is not session
          and renewed.scope == session.scope and renewed.episode == session.episode
          and renewed.consumed_actions == session.consumed_actions, 'RECONCILIATION_SCOPE')
    _current(renewed, owners)
    _need(owners.check_fresh_grant(renewed, evidence) is True, 'FRESH_GRANT')
    return renewed
