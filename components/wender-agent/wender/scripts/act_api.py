# @module {"id":"wa.scripts.act_api","layer":3,"owner":"ACT","deps":["wa.scripts.types","wa.scripts.predicates"],"book":"HB-WA-s2-act"}
"""Host-owned script authorization, preparation and serialized execution.

Origin: scripts_mesh/build/spec/s6_w.md. Production backend owners are supplied explicitly. The installation owner must
prove participation of every invalidator and a bounded input admission call.
No default installation owner is supplied; no backend is enabled by import.
"""
from dataclasses import dataclass as _dataclass, replace as _replace
from threading import RLock as _RLock
from functools import wraps as _wraps
from . import types as _t


# @anchor:wa.scripts.act_api._closed
def _closed(function):
    @_wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except _t.ContractError as error:
            reason = error.reason
        except Exception:
            reason = _t.Reason.UNKNOWN
        raise _t.ContractError(reason)
    return call


# @anchor:wa.scripts.act_api._need
def _need(condition, reason):
    if not condition:
        raise _t.ContractError(reason)


# @anchor:wa.scripts.act_api._true
def _true(check):
    _need(type(check) is _t.AdmissionCheck and check.result is _t.Truth.TRUE,
          check.reason if type(check) is _t.AdmissionCheck and check.reason else _t.Reason.MISSING)


# @anchor:wa.scripts.act_api._DispatchContext
@_dataclass(frozen=True, slots=True, repr=False)
class _DispatchContext:
    admission: object
    guard: object
    revision: int


# @anchor:wa.scripts.act_api._InputInterlock
class _InputInterlock:
    """Shared final admission owner; external store locks must be short lived.

    installation.check(interlock) returns AdmissionCheck only when all relevant
    mutators enter invalidate and the backend admission callback is bounded.
    It must be a trusted composition owner, not script-provided metadata.
    register's guard rechecks lease/permit, exact binding, host risk, STOP,
    scope/revisions, restart and deadline under this owner. Guard is evidence,
    never a cached permit. Production implementations of that guard remain a
    separate prerequisite. Retained contexts are bounded until explicit drain.
    """
    # @anchor:wa.scripts.act_api._InputInterlock.__init__
    def __init__(self, *, installation, max_pending):
        _need(type(max_pending) is int and max_pending > 0, _t.Reason.BOUNDS)
        self._lock = _RLock()
        self._installation = installation
        self._limit = max_pending
        self._contexts = {}
        self._revision = 0
        self._admitting = False
        _true(installation.check(self))

    # @anchor:wa.scripts.act_api._InputInterlock.register
    def register(self, admission, *, guard):
        with self._lock:
            _true(self._installation.check(self))
            _need(callable(guard), _t.Reason.TYPE)
            _need(len(self._contexts) < self._limit, _t.Reason.CAPACITY)
            _need(not any(row[0].admission is admission for row in self._contexts.values()), _t.Reason.CONFLICT)
            context = _DispatchContext(admission, guard, self._revision)
            self._contexts[id(context)] = [context, False]
            return context

    # @anchor:wa.scripts.act_api._InputInterlock.check_context
    def check_context(self, admission, context):
        with self._lock:
            if context is None:
                _need(not any(row[0].admission is admission for row in self._contexts.values()), _t.Reason.FOREIGN)
                return
            _need(type(context) is _DispatchContext, _t.Reason.FOREIGN)
            row = self._contexts.get(id(context))
            _need(row is not None and row[0] is context and context.admission is admission, _t.Reason.FOREIGN)
            _true(self._installation.check(self))

    # @anchor:wa.scripts.act_api._InputInterlock.send_once
    def send_once(self, admission, request, decision, send, *, context):
        # The RLock is this admission owner, never a store/metadata lock. It
        # covers bounded input admission only, not an unbounded completion wait.
        with self._lock:
            self.check_context(admission, context)
            row = self._contexts[id(context)]
            _need(not row[1] and not self._admitting, _t.Reason.RECONCILIATION)
            _need(context.revision == self._revision, _t.Reason.STALE)
            _need(decision is admission.decision and callable(send), _t.Reason.FOREIGN)
            # Consume before entering arbitrary guard or backend code, including
            # when it raises. A failed final check cannot be retried either.
            row[1] = True
            _true(context.guard(admission, request, decision))
            _need(context.revision == self._revision, _t.Reason.STALE)
            self._admitting = True
            try:
                return send()
            finally:
                self._admitting = False

    # @anchor:wa.scripts.act_api._InputInterlock.invalidate
    def invalidate(self, change):
        with self._lock:
            _need(callable(change) and not self._admitting, _t.Reason.CONFLICT)
            # Even a mutator that raises conservatively invalidates preparation.
            self._revision += 1
            return change()

    # @anchor:wa.scripts.act_api._InputInterlock.release
    def release(self, context, *, drained):
        with self._lock:
            _true(self._installation.check_drained(context, drained))
            self.check_context(context.admission, context)
            _need(not self._admitting, _t.Reason.CONFLICT)
            del self._contexts[id(context)]

    # @anchor:wa.scripts.act_api._InputInterlock.outstanding
    def outstanding(self):
        with self._lock:
            return tuple(row[0] for row in self._contexts.values())


# @anchor:wa.scripts.act_api._WireEffect
@_dataclass(frozen=True, slots=True, repr=False)
class _WireEffect:
    body: object
    adapter_input: object
    observation: object
    transport: object
    frame_evidence: object
    action_token: _t.PrivateRef
    callback: object


# @anchor:wa.scripts.act_api._AppliedEffect
@_dataclass(frozen=True, slots=True, repr=False)
class _AppliedEffect:
    adapter: object
    profile: object
    config: object
    signal_reader: object


# @anchor:wa.scripts.act_api._PreparedEffect
@_dataclass(slots=True, repr=False)
class _PreparedEffect:
    plan: object
    node: object
    lease: object
    binding: object
    intent: object
    deadline_ns: int
    permit: object = None
    reservation: object = None
    grant: object = None
    wire: object = None
    admission: object = None
    context: object = None
    entered: bool = False
    custody_entered: bool = False
    consumed: bool = False
    disposition: object = _t.InputDisposition.NOT_SENT
    result: object = None
    post: object = None
    post_token: object = None
    closed: bool = False
    holder_contract: object = None
    send_deadline_ns: int | None = None


# Owner ports are local composition contracts, never deserialized capabilities.
from typing import Protocol as _Protocol
from . import predicates as _predicates


# @anchor:wa.scripts.act_api._BindingOwner
class _BindingOwner(_Protocol):
    # @anchor:wa.scripts.act_api._BindingOwner.bind
    def bind(self, selector, *, plan, step_id, observation, scope, authority, deadline_ns): ...


# @anchor:wa.scripts.act_api._RiskOwner
class _RiskOwner(_Protocol):
    # @anchor:wa.scripts.act_api._RiskOwner.check
    def check(self, intent, binding, *, episode_id, scope, authority, deadline_ns): ...


# @anchor:wa.scripts.act_api._ApplicationOwner
class _ApplicationOwner(_Protocol):
    # @anchor:wa.scripts.act_api._ApplicationOwner.confirm
    def confirm(self, action_token, *, admission, completion, pre, scope, authority, deadline_ns): ...

    # @anchor:wa.scripts.act_api._ApplicationOwner.check_post
    def check_post(self, evidence, post, *, action_token, pre, scope, deadline_ns):
        """Attest causal sequence/tick/epoch, surface and any trusted rebinding."""
        ...


# @anchor:wa.scripts.act_api._WireOwner
class _WireOwner(_Protocol):
    # @anchor:wa.scripts.act_api._WireOwner.prepare
    def prepare(self, intent, binding, *, episode_id, deadline_ns): ...

    # @anchor:wa.scripts.act_api._WireOwner.actuator
    def actuator(self, intent, binding):
        """Return the registered custody consumer for opaque input capabilities."""
        ...

    # @anchor:wa.scripts.act_api._WireOwner.accept_input
    def accept_input(self, private_ref, *, intent, actuator): ...


# @anchor:wa.scripts.act_api._ExecutionPolicy
@_dataclass(frozen=True, slots=True, repr=False)
class _ExecutionPolicy:
    """All ceilings and stage allocations supplied by trusted composition."""
    max_pending: int
    max_callbacks: int
    max_queue: int
    retained_bytes: int
    terminal_bytes: int
    refusal_bytes: int
    acquisition_ns: int
    send_ns: int
    settle_ns: int
    seal_ns: int
    cleanup_ns: int

    # @anchor:wa.scripts.act_api._ExecutionPolicy.__post_init__
    def __post_init__(self):
        from dataclasses import fields
        for field in fields(self):
            value = getattr(self, field.name)
            _need(type(value) is int and value > 0, _t.Reason.BOUNDS)


# @anchor:wa.scripts.act_api._predicate
def _predicate(contexts, predicate, observation, scope):
    context = contexts.context(predicate, observation)
    _need(type(context) is dict, _t.Reason.TYPE)
    for key in ('max_nodes', 'max_evidence_rows', 'max_inventory_rows'):
        _need(type(context.get(key)) is int and context[key] > 0, _t.Reason.BOUNDS)
    evaluate = (_predicates.evaluate_transition if predicate.operator in
                (_t.PredicateOperator.DECLARED_TRANSITION, _t.PredicateOperator.VALUE_LEN_DELTA)
                else _predicates.evaluate)
    result = evaluate(predicate, scope, **context)
    _need(type(result) is _t.PredicateResult, _t.Reason.TYPE)
    return result


# @anchor:wa.scripts.act_api._passed
def _passed(result):
    _need(type(result) is _t.PredicateResult and result.value is _t.Truth.TRUE,
          result.reason if type(result) is _t.PredicateResult and result.reason else _t.Reason.CONFLICT)


# @anchor:wa.scripts.act_api._unknown
def _unknown():
    return _t.PredicateResult(_t.Truth.UNKNOWN, _t.Reason.MISSING)


# @anchor:wa.scripts.act_api._duration
def _duration():
    return _t.MeasuredValue(None, 'ns', _t.MissingRef(None, _t.Reason.MISSING), _t.Reason.MISSING)


# @anchor:wa.scripts.act_api.ActAPI
class ActAPI:
    """Script execution over explicitly injected, host-authenticated owners.

    The control owner authenticates exact runner/plan registration, stage and
    aggregate ceilings, restart evidence and current permit eligibility. It
    must participate in the interlock installation proof. The request owner
    includes overlapping observations, private slots, buffers and callbacks in
    the aggregate resource request. No production owner is inferred here.
    """
    # @anchor:wa.scripts.act_api.ActAPI.__init__
    def __init__(self, *, store, ledger, binding_owner, binding_registry,
                 contexts, risk, authority, wire, application, interlock,
                 slots, frame_wait, clock, cancellation, reservations,
                 action_policy, control, requests, resource_policy, policy):
        _need(type(policy) is _ExecutionPolicy and type(resource_policy) is _t.ResourcePolicy,
              _t.Reason.TYPE)
        _need(type(interlock) is _InputInterlock and callable(clock), _t.Reason.TYPE)
        _need(action_policy.script_interlock is interlock, _t.Reason.FOREIGN)
        self.store, self.ledger = store, ledger
        self.binding_owner, self.binding_registry = binding_owner, binding_registry
        self.contexts, self.risk, self.authority = contexts, risk, authority
        self.wire, self.application, self.interlock = wire, application, interlock
        self.slots, self.frame_wait, self.clock = slots, frame_wait, clock
        self.cancellation, self.reservations = cancellation, reservations
        self.action_policy, self.control, self.requests = action_policy, control, requests
        self.resource_policy, self.policy = resource_policy, policy
        self._prepared = {}
        self._actions = set()
        self._lock = _RLock()
        self.service_stopped = False

    # @anchor:wa.scripts.act_api.ActAPI._check
    def _check(self, state, intent=None, permit=None):
        _need(not self.service_stopped and not state.stopped, _t.Reason.RECONCILIATION)
        now = self.clock()
        _need(type(now) is int and now < state.deadline_ns, _t.Reason.DEADLINE)
        _need(state.plan.scope == state.scope == state.lease.scope
              and state.authority == state.lease.authority_revision
              and state.authority.scope == state.scope.scope_revision, _t.Reason.FOREIGN)
        _true(self.control.check(state, intent=intent, permit=permit, policy=self.policy))
        _true(self.store.check_lease(state.lease, state.plan.identity, state.scope,
                                    state.episode_id, state.lease.process_incarnation,
                                    state.lease.retained_owner))
        if intent is not None:
            _true(self.authority.check_final(intent, state.scope, state.authority,
                True, state.stopped, now, state.deadline_ns, state.restart_evidence))

    # @anchor:wa.scripts.act_api.ActAPI._audit
    def _audit(self, prepared, event_state, verification, reason=None):
        event = self.ledger.event(prepared.reservation, state=event_state,
            disposition=prepared.disposition, verification=verification,
            observations=tuple(o for o in (prepared.binding.observation, prepared.post) if o is not None),
            reason=reason, usage=_t.MeasuredValue(None, 'count', _t.MissingRef(None, _t.Reason.MISSING), _t.Reason.MISSING),
            cost=_t.MeasuredValue(None, 'cost', _t.MissingRef(None, _t.Reason.MISSING), _t.Reason.MISSING),
            exit_code=0 if event_state is _t.EventState.VERIFIED else None,
            score_q16=65536 if event_state is _t.EventState.VERIFIED else None)
        _true(self.ledger.append(prepared.reservation, event))

    # @anchor:wa.scripts.act_api.ActAPI._result
    def _result(self, prepared, predicate, checkpoint, reason):
        return _t.StepResult(prepared.binding.observation, prepared.post,
            reason if prepared.post is None else None,
            prepared.wire.action_token if prepared.wire is not None else None,
            prepared.disposition, predicate, checkpoint, _duration(), (), _t.Reason.MISSING)

    # @anchor:wa.scripts.act_api.ActAPI._drain
    def _drain(self, state, prepared):
        # No refund from elapsed time or an empty count: exact supervised proof.
        proof = self.cancellation.request(prepared.holder_contract)
        _need(type(prepared.holder_contract) is _t.HolderContract, _t.Reason.TYPE)
        _need(type(proof) is _t.CancellationResult, _t.Reason.TYPE)
        _true(self.cancellation.verify(prepared.holder_contract, proof))
        _need(proof.drained and (proof.acknowledged or (proof.terminated and proof.joined)),
              _t.Reason.RECONCILIATION)
        if prepared.post_token is not None:
            _true(self.frame_wait.release_observation(prepared.post_token))
            prepared.post_token = None
        if prepared.context is not None:
            self.interlock.release(prepared.context, drained=proof)
            prepared.context = None
        if prepared.grant is not None:
            _true(self.reservations.release_unused(prepared.grant, prepared.grant.charged_bytes, proof))
            prepared.grant = None
        if prepared.reservation is not None:
            refund = self.ledger.release_unused(prepared.reservation)
            _need(type(refund) is int and refund >= 0, _t.Reason.TYPE)
        prepared.closed = True
        self._prepared.pop(id(prepared), None)

    # @anchor:wa.scripts.act_api.ActAPI._cancel_effect
    def _cancel_effect(self, state, prepared, reason):
        _need(self._prepared.get(id(prepared)) is prepared, _t.Reason.FOREIGN)
        errors = []
        if prepared.result is None:
            prepared.consumed = True
            prepared.result = self._result(prepared, _unknown(),
                _t.PredicateResult(_t.Truth.UNKNOWN, reason), reason)
            if prepared.reservation is not None:
                try:
                    self._audit(prepared, _t.EventState.REFUSED, _t.Truth.UNKNOWN, reason)
                except Exception:
                    errors.append(_t.Reason.RECONCILIATION)
            if prepared.permit is not None:
                try:
                    _true(self.store.end_step(prepared.permit, prepared.result, state.scope, state.authority))
                except Exception:
                    errors.append(_t.Reason.RECONCILIATION)
        try:
            if prepared.grant is None and prepared.reservation is None:
                prepared.closed = True
                self._prepared.pop(id(prepared), None)
            else:
                self._drain(state, prepared)
        except Exception:
            errors.append(_t.Reason.RECONCILIATION)
        if errors:
            self.service_stopped = True
            raise _t.ContractError(_t.Reason.RECONCILIATION)
        state.pending_effect = None

    # @anchor:wa.scripts.act_api.ActAPI.authorize_target
    def authorize_target(self, state, node):
        return authorize_target(self, state, node)

    # @anchor:wa.scripts.act_api.ActAPI.prepare_effect
    def prepare_effect(self, state, node):
        return prepare_effect(self, state, node)

    # @anchor:wa.scripts.act_api.ActAPI.dispatch_effect
    def dispatch_effect(self, state, prepared):
        return dispatch_effect(self, state, prepared)

    # @anchor:wa.scripts.act_api.ActAPI.outstanding
    def outstanding(self):
        return tuple(self._prepared.values())


# @anchor:wa.scripts.act_api.authorize_target
@_closed
def authorize_target(api, state, node):
    api._check(state)
    _need(type(node) is _t.StepPlan and 0 <= state.cursor < len(state.plan.nodes)
          and state.plan.nodes[state.cursor] is node and node.effect is not None, _t.Reason.FOREIGN)
    effect = node.effect
    binding = api.binding_owner.bind(effect.selector, plan=state.plan, step_id=node.step_id,
        observation=state.observation, scope=state.scope, authority=state.authority,
        deadline_ns=state.deadline_ns)
    _need(type(binding) is _t.TargetBinding, _t.Reason.TYPE)
    _need(binding.artifact == state.plan.identity and binding.step_id == node.step_id
          and binding.selector is effect.selector and binding.observation is state.observation
          and binding.scope == state.scope and binding.authority == state.authority, _t.Reason.FOREIGN)
    _true(api.binding_registry.check(binding, state.observation, state.scope,
                                    state.authority, api.binding_owner, state.observation.producer_generation))
    binding.__post_init__()
    witness = binding.witness
    witness.__post_init__()
    witness.producer_layout.__post_init__()
    _need(witness.expires_ns > api.clock() and effect.action in witness.supported_actions, _t.Reason.STALE)
    _need(witness.producer_layout.source in (_t.EvidenceSource.AX, _t.EvidenceSource.HOST), _t.Reason.FOREIGN)
    required = [('visible', _t.Truth.TRUE), ('enabled', _t.Truth.TRUE),
                ('actionable', _t.Truth.TRUE), ('busy', _t.Truth.FALSE), ('blocked', _t.Truth.FALSE)]
    if effect.action is _t.ActionKind.TYPE:
        required += [('editable', _t.Truth.TRUE), ('focused', _t.Truth.TRUE)]
    for name, expected in required:
        fact = getattr(witness.facts, name)
        fact.__post_init__()
        _need(type(fact) is _t.FactEvidence and fact.value is expected
              and fact.observation is state.observation, _t.Reason.CONFLICT)
    return binding


# @anchor:wa.scripts.act_api.prepare_effect
@_closed
def prepare_effect(api, state, node):
    with api._lock:
        _need(len(api._prepared) < min(api.policy.max_pending, api.policy.max_callbacks, api.policy.max_queue) and state.pending_effect is None, _t.Reason.CAPACITY)
        action_key = (state.episode_id, state.plan.identity, state.cursor)
        _need(action_key not in api._actions, _t.Reason.RECONCILIATION)
        _need(len(api._actions) < api.policy.max_queue, _t.Reason.CAPACITY)
        binding = authorize_target(api, state, node)
        effect = node.effect
        intent = _t.StepIntent(state.plan.identity, node.step_id, state.cursor, effect.action,
            effect.selector, effect.arguments, effect.postcondition, state.scope, state.authority)
        deadline = min(state.deadline_ns, effect.verification.absolute_deadline_ns,
                       api.clock() + effect.verification.timeout_ms * 1000000)
        _need(api.clock() < deadline, _t.Reason.DEADLINE)
        api._check(state, intent)
        _true(api.risk.check(intent, binding, episode_id=state.episode_id, scope=state.scope,
                             authority=state.authority, deadline_ns=deadline))
        _passed(_predicate(api.contexts, effect.precondition, binding.observation, state.scope))
        prepared = _PreparedEffect(state.plan, node, state.lease, binding, intent, deadline,
            send_deadline_ns=min(deadline, api.clock() + api.policy.send_ns))
        api._prepared[id(prepared)] = prepared
        api._actions.add(action_key)
        state.pending_effect = prepared
        try:
            request = api.requests.request(intent, binding, deadline_ns=deadline, policy=api.policy)
            _need(type(request) is _t.ReservationRequest and request.scope == state.scope
                  and request.absolute_deadline_ns <= deadline, _t.Reason.FOREIGN)
            _need(request.peak_bytes <= api.policy.retained_bytes, _t.Reason.CAPACITY)
            outcome = api.reservations.reserve(request, api.resource_policy)
            _need(type(outcome) is _t.ReservationOutcome and outcome.grant is not None,
                  outcome.reason if type(outcome) is _t.ReservationOutcome and outcome.reason else _t.Reason.CAPACITY)
            # Retain every acquired grant before validating its returned bounds.
            # A malformed/oversized grant still needs authenticated cleanup.
            prepared.grant = outcome.grant
            _need(outcome.grant.request is request and outcome.grant.charged_bytes <= api.policy.retained_bytes
                  and outcome.grant.absolute_deadline_ns <= deadline, _t.Reason.CAPACITY)
            prepared.holder_contract = api.requests.holder(intent, outcome.grant, deadline_ns=deadline)
            _need(type(prepared.holder_contract) is _t.HolderContract
                  and prepared.holder_contract is not state.holder_contract, _t.Reason.FOREIGN)
            prepared.reservation = api.ledger.reserve(intent, api.policy.terminal_bytes,
                api.policy.refusal_bytes, state.scope, state.authority)
            prepared.permit = api.store.begin_step(state.lease, intent, state.scope, state.authority)
            prepared.wire = api.wire.prepare(intent, binding, episode_id=state.episode_id, deadline_ns=deadline)
            _need(type(prepared.wire) is _WireEffect and type(prepared.wire.action_token) is _t.PrivateRef
                  and callable(prepared.wire.callback), _t.Reason.TYPE)
            w = prepared.wire
            prepared.admission = api.action_policy.admit(w.body, w.adapter_input,
                observation=w.observation, transport=w.transport, frame_evidence=w.frame_evidence)
            def guard(admission, request, decision):
                api._check(state, intent, prepared.permit)
                _need(api.clock() < prepared.send_deadline_ns, _t.Reason.DEADLINE)
                _need(authorize_target(api, state, node) is binding, _t.Reason.STALE)
                _true(api.risk.check(intent, binding, episode_id=state.episode_id, scope=state.scope,
                                    authority=state.authority, deadline_ns=deadline))
                _passed(_predicate(api.contexts, effect.precondition, binding.observation, state.scope))
                return _t.AdmissionCheck(_t.Truth.TRUE, None)
            prepared.context = api.interlock.register(prepared.admission, guard=guard)
            return prepared
        except _t.ContractError as error:
            failure = error.reason
        except Exception:
            failure = _t.Reason.UNKNOWN
        # Retain exact partial preparation for supervised abort. Raise outside
        # handlers so private provider exceptions cannot escape via __context__.
        state.stopped = True
        raise _t.ContractError(failure)


# @anchor:wa.scripts.act_api.dispatch_effect
@_closed
def dispatch_effect(api, state, prepared):
    with api._lock:
        _need(type(prepared) is _PreparedEffect and prepared.plan is state.plan
              and prepared.lease is state.lease and prepared.node is state.plan.nodes[state.cursor], _t.Reason.FOREIGN)
        if prepared.result is not None:
            return prepared.result
        _need(api._prepared.get(id(prepared)) is prepared and not prepared.consumed, _t.Reason.RECONCILIATION)
        prepared.consumed = True
        predicate = checkpoint = _unknown()
        reason = _t.Reason.MISSING
        try:
            api._check(state, prepared.intent, prepared.permit)
            api._audit(prepared, _t.EventState.ADMITTED, _t.Truth.UNKNOWN)
            completion = []
            def physical(request, decision):
                prepared.entered = True
                prepared.disposition = _t.InputDisposition.INPUT_UNCERTAIN
                # This callback executes under the shared final admission owner.
                actuator = api.wire.actuator(prepared.intent, prepared.binding)
                for slot in prepared.intent.arguments:
                    _need(slot.episode_id == state.episode_id and slot.scope == state.scope,
                          _t.Reason.PRIVACY)
                    private = api.slots.consume_input(slot, state.episode_id, state.scope, actuator)
                    _need(type(private) is _t.PrivateRef, _t.Reason.PRIVACY)
                    _true(api.wire.accept_input(private, intent=prepared.intent, actuator=actuator))
                value = prepared.wire.callback(request, decision)
                completion.append(value)
                return value
            # W8U may quarantine custody before the SCRIPT guard reaches the
            # physical callback. Without an authenticated unsent reconciliation,
            # entering that lower owner remains conservatively possible input.
            prepared.custody_entered = True
            prepared.disposition = _t.InputDisposition.INPUT_UNCERTAIN
            response = api.action_policy.dispatch(prepared.admission, physical, script_context=prepared.context)
            _need(response['status'] == 'admitted' and len(completion) == 1, _t.Reason.RECONCILIATION)
            _need(api.clock() < prepared.send_deadline_ns, _t.Reason.DEADLINE)
            evidence = api.application.confirm(prepared.wire.action_token, admission=prepared.admission,
                completion=completion[0], pre=prepared.binding.observation, scope=state.scope,
                authority=state.authority, deadline_ns=prepared.deadline_ns)
            _need(type(evidence) is _AppliedEffect and evidence.adapter.outcome == 'APPLIED', _t.Reason.MISSING)
            prepared.disposition = _t.InputDisposition.APPLIED
            api._audit(prepared, _t.EventState.APPLIED, _t.Truth.UNKNOWN)
            _need(type(state.acquisitions_remaining) is int and state.acquisitions_remaining > 0, _t.Reason.BOUNDS)
            state.acquisitions_remaining -= 1
            acquired = api.frame_wait.next_observation(prepared.binding.observation.publication_identity,
                min(prepared.deadline_ns, api.clock() + api.policy.acquisition_ns), state.holder)
            _need(type(acquired) is _t.AcquisitionResult and acquired.observation is not None,
                  acquired.reason if type(acquired) is _t.AcquisitionResult and acquired.reason else _t.Reason.MISSING)
            prepared.post, prepared.post_token = acquired.observation, acquired.owner_token
            pre, post = prepared.binding.observation, prepared.post
            _need(post.scope == state.scope and post.publication_identity != pre.publication_identity
                  and post.capture_end_ns >= pre.capture_end_ns and post.authority == state.authority,
                  _t.Reason.STALE)
            _true(api.application.check_post(evidence, post, action_token=prepared.wire.action_token,
                pre=pre, scope=state.scope, deadline_ns=prepared.deadline_ns))
            predicate = _predicate(api.contexts, prepared.node.effect.postcondition, post, state.scope)
            _passed(predicate)
            _need(evidence.config.deadline_ns <= prepared.deadline_ns
                  and evidence.config.max_polls + 1 <= prepared.node.effect.verification.max_acquisitions,
                  _t.Reason.BOUNDS)
            _need(evidence.config.max_polls <= state.acquisitions_remaining, _t.Reason.BOUNDS)
            state.acquisitions_remaining -= evidence.config.max_polls
            settle_config = _replace(evidence.config, deadline_ns=min(evidence.config.deadline_ns,
                prepared.deadline_ns, api.clock() + api.policy.settle_ns))
            api.frame_wait.settle_effect(evidence.adapter, evidence.profile, settle_config,
                evidence.signal_reader, cancel=state.holder, scope=state.scope)
            api._check(state, prepared.intent, prepared.permit)
            _need(api.clock() < prepared.deadline_ns, _t.Reason.DEADLINE)
            api._audit(prepared, _t.EventState.VERIFIED, _t.Truth.TRUE)
            checkpoint = _t.PredicateResult(_t.Truth.TRUE, None)
            reason = None
        except _t.ContractError as error:
            reason = error.reason
        except Exception:
            reason = _t.Reason.UNKNOWN
        if checkpoint.value is not _t.Truth.TRUE:
            checkpoint = _t.PredicateResult(_t.Truth.UNKNOWN, reason or _t.Reason.MISSING)
        result = api._result(prepared, predicate, checkpoint, reason or _t.Reason.MISSING)
        prepared.result = result
        if checkpoint.value is not _t.Truth.TRUE:
            state.stopped = True
            try:
                event_state = (_t.EventState.INPUT_UNCERTAIN if prepared.disposition is _t.InputDisposition.INPUT_UNCERTAIN
                               else _t.EventState.SUSPENDED if prepared.disposition is _t.InputDisposition.APPLIED
                               else _t.EventState.REFUSED)
                api._audit(prepared, event_state, _t.Truth.UNKNOWN, reason)
            except Exception:
                api.service_stopped = True
        try:
            if checkpoint.value is _t.Truth.TRUE:
                _true(api.control.retain_post(state, prepared.post, prepared.post_token))
            _true(api.store.end_step(prepared.permit, result, state.scope, state.authority))
            api._drain(state, prepared)
            state.pending_effect = None
        except Exception:
            api.service_stopped = state.stopped = True
            # A verified semantic result cannot advance without cleanup acknowledgement.
            if checkpoint.value is _t.Truth.TRUE:
                result = api._result(prepared, predicate, _unknown(), _t.Reason.RECONCILIATION)
                prepared.result = result
        order = (_t.InputDisposition.NOT_SENT, _t.InputDisposition.APPLIED, _t.InputDisposition.INPUT_UNCERTAIN)
        state.input_disposition = max((state.input_disposition, result.input_disposition), key=order.index)
        state.checkpoint = result.checkpoint
        if result.checkpoint.value is _t.Truth.TRUE:
            state.verified_effects += 1
            state.observation = prepared.post
        return result
