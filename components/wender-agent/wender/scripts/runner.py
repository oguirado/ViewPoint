# @module {"id":"wa.scripts.runner","layer":3,"owner":"LEARN","deps":["wa.scripts.types","wa.scripts.compiler","wa.scripts.act_api"],"book":"HB-WA-s4-learn"}
"""Finite, artifact-pinned interpreter over trusted host-owned runtime ports.

Origin: scripts_mesh/build/spec/s6_w.md. Runtime registration authenticates the
state, exact pairing lifetime, budgets, source and restart barrier. No port is
obtained from script metadata. Missing production owners keep SCRIPT closed.
"""
from dataclasses import dataclass as _dataclass, field as _field
from typing import Protocol as _Protocol
from threading import RLock as _RLock
from . import types as _t, compiler as _compiler, act_api as _act


# @anchor:wa.scripts.runner._PredicateContext
class _PredicateContext(_Protocol):
    # @anchor:wa.scripts.runner._PredicateContext.context
    def context(self, predicate, observation): ...


# @anchor:wa.scripts.runner._GoalOwner
class _GoalOwner(_Protocol):
    # @anchor:wa.scripts.runner._GoalOwner.evaluate
    def evaluate(self, terminal_goal, *, observation, scope, authority, deadline_ns): ...


# @anchor:wa.scripts.runner._SealOwner
class _SealOwner(_Protocol):
    # @anchor:wa.scripts.runner._SealOwner.finish
    def finish(self, outcome, *, ledger, deadline_ns):
        """Return an actual EpisodeSeal, or explicit no-input AdmissionCheck."""
        ...


# @anchor:wa.scripts.runner.RunnerState
@_dataclass(slots=True, repr=False)
class RunnerState:
    plan: object
    lease: object
    episode_id: str
    scope: object
    authority: object
    deadline_ns: int
    runtime: object
    observation: object
    holder: object
    holder_contract: object
    restart_evidence: object
    cursor: int = 0
    pending_effect: object = None
    checkpoint: object = _field(default_factory=_act._unknown)
    input_disposition: object = _t.InputDisposition.NOT_SENT
    owned_resources: list = _field(default_factory=list)
    outcome: object = None
    stopped: bool = False
    failure: object = None
    verified_effects: int = 0
    acquisitions_remaining: int | None = None
    owner_drained: object = _field(default_factory=_act._unknown)
    lock: object = _field(default_factory=_RLock)

    # @anchor:wa.scripts.runner.RunnerState.__post_init__
    def __post_init__(self):
        if self.acquisitions_remaining is None:
            self.acquisitions_remaining = self.plan.limits.max_acquisitions
        _act._need(type(self.acquisitions_remaining) is int and self.acquisitions_remaining > 0, _t.Reason.BOUNDS)


# @anchor:wa.scripts.runner._check
def _check(state):
    _act._need(type(state) is RunnerState and state.outcome is None, _t.Reason.CONFLICT)
    state.runtime.act_api._check(state)
    _act._true(state.runtime.check(state))


# @anchor:wa.scripts.runner._merge
def _merge(state, disposition):
    order = (_t.InputDisposition.NOT_SENT, _t.InputDisposition.APPLIED, _t.InputDisposition.INPUT_UNCERTAIN)
    state.input_disposition = max((state.input_disposition, disposition), key=order.index)


# @anchor:wa.scripts.runner._goal
def _goal(state):
    result = state.runtime.goal_owner.evaluate(state.plan.terminal_goal,
        observation=state.observation, scope=state.scope, authority=state.authority,
        deadline_ns=state.deadline_ns)
    _act._need(type(result) is _t.PredicateResult, _t.Reason.TYPE)
    return result


# @anchor:wa.scripts.runner._outcome
def _outcome(state, status, goal, failure):
    return _t.RuntimeOutcome(state.episode_id, state.plan.identity, state.scope,
        status, state.input_disposition, state.checkpoint, goal, failure,
        state.input_disposition is _t.InputDisposition.INPUT_UNCERTAIN,
        (), _t.Reason.MISSING, state.owner_drained)


# @anchor:wa.scripts.runner.start
@_act._closed
def start(plan, *, lease, session, admission, inputs, registry, runtime):
    accepted = _compiler.verify_pairing(plan, session=session, admission=admission,
                                       inputs=inputs, registry=registry)
    _act._need(accepted is plan, _t.Reason.FOREIGN)
    deadline = min(runtime.deadline_ns, plan.limits.absolute_deadline_ns,
                   lease.absolute_deadline_ns, runtime.holder.absolute_deadline_ns,
                   runtime.holder_contract.absolute_deadline_ns)
    _act._need(type(deadline) is int and deadline > runtime.clock(), _t.Reason.DEADLINE)
    state = RunnerState(plan, lease, runtime.episode_id, runtime.scope, runtime.authority,
        deadline, runtime, runtime.observation, runtime.holder, runtime.holder_contract,
        runtime.restart_evidence)
    try:
        _act._true(runtime.register(state))
        _check(state)
        _act._need(state.scope == plan.scope and state.episode_id == inputs._episode,
                   _t.Reason.FOREIGN)
        entry = _act._predicate(runtime.contexts, plan.entry, state.observation, state.scope)
        _act._passed(entry)
        state.checkpoint = entry
        # A no-input path requires the same independent registered terminal goal.
        if _goal(state).value is _t.Truth.TRUE:
            finish(state)
        return state
    except _t.ContractError as error:
        abort(state, error.reason)
        return state
    except Exception:
        abort(state, _t.Reason.UNKNOWN)
        return state


# @anchor:wa.scripts.runner.interpret
def interpret(state):
    _check(state)
    _act._need(type(state.cursor) is int and 0 <= state.cursor < len(state.plan.nodes), _t.Reason.BOUNDS)
    node = state.plan.nodes[state.cursor]
    _act._need(type(node) is _t.StepPlan, _t.Reason.TYPE)
    if node.kind in (_t.ActionKind.CLICK, _t.ActionKind.TYPE, _t.ActionKind.KEY):
        _act._need(node.effect is not None, _t.Reason.MISSING)
        return _t.StepIntent(state.plan.identity, node.step_id, state.cursor,
            node.effect.action, node.effect.selector, node.effect.arguments,
            node.effect.postcondition, state.scope, state.authority)
    _act._need(node.kind in (_t.ActionKind.FIND, _t.ActionKind.WAIT_UNTIL,
        _t.ActionKind.ASSERT_TAG, _t.ActionKind.HARVEST, _t.NodeKind.TERMINAL), _t.Reason.TYPE)
    return node


# @anchor:wa.scripts.runner.advance
def advance(state):
    with state.lock:
        if state.outcome is not None:
            return state.outcome
        try:
            interpreted = interpret(state)
            node = state.plan.nodes[state.cursor]
            if type(interpreted) is _t.StepIntent:
                prepared = state.pending_effect
                if prepared is None:
                    prepared = state.runtime.act_api.prepare_effect(state, node)
                result = state.runtime.act_api.dispatch_effect(state, prepared)
                _merge(state, result.input_disposition)
                state.checkpoint = result.checkpoint
                _act._passed(result.checkpoint)
                _act._need(not state.runtime.act_api.service_stopped and prepared.closed,
                           _t.Reason.RECONCILIATION)
                # Observation records are retained only by their explicit host owner.
                _act._true(state.runtime.retain_observation(state, prepared.post))
                state.observation = prepared.post
            elif node.kind is _t.ActionKind.FIND:
                # Symbolic query association stays private; it grants no actuator authority.
                _act._true(state.runtime.find_owner.resolve(node.find, state=state))
            elif node.kind is _t.ActionKind.WAIT_UNTIL:
                _act._need(node.wait.budget.acquisition_attempts <= state.acquisitions_remaining, _t.Reason.BOUNDS)
                result = state.runtime.frame_wait.wait_until(node.wait.predicate, state.scope,
                    node.wait.budget, state.deadline_ns, state.holder)
                state.acquisitions_remaining -= state.runtime.frame_wait.attempt_count
                _act._need(type(result) is _t.AcquisitionResult and result.observation is not None,
                    result.reason if type(result) is _t.AcquisitionResult and result.reason else _t.Reason.MISSING)
                state.owned_resources.append(result.owner_token)
                _act._true(state.runtime.retain_observation(state, result.observation))
                state.observation = result.observation
            elif node.kind is _t.ActionKind.ASSERT_TAG:
                state.checkpoint = _act._predicate(state.runtime.contexts,
                    node.assertion.predicate, state.observation, state.scope)
                _act._passed(state.checkpoint)
                _act._true(state.runtime.audit_read(state, node, state.checkpoint))
            elif node.kind is _t.ActionKind.HARVEST:
                # Owner validates exact query/answer/turn, authorization and byte bound.
                private = state.runtime.harvest_owner.acquire(node.harvest, state=state)
                _act._need(type(private) is _t.PrivateRef, _t.Reason.PRIVACY)
                _act._true(state.runtime.results.deliver(private, state.episode_id, state.scope,
                    _t.Purpose.HARVEST, state.runtime.result_consumer))
            elif node.kind is _t.NodeKind.TERMINAL:
                _act._need(node.terminal is state.plan.terminal_goal, _t.Reason.FOREIGN)
                return finish(state)
            _check(state)
            state.cursor += 1
            return state
        except _t.ContractError as error:
            return abort(state, error.reason)
        except Exception:
            return abort(state, _t.Reason.UNKNOWN)


# @anchor:wa.scripts.runner.abort
def abort(state, reason):
    with state.lock:
        if state.outcome is not None:
            return state.outcome
        state.stopped = True
        state.owner_drained = _act._unknown()
        prepared = state.pending_effect
        if prepared is not None:
            _merge(state, prepared.disposition)
        try:
            state.runtime.act_api.interlock.invalidate(lambda: None)
            proof = state.runtime.cancellation.request(state.holder_contract)
            _act._need(type(proof) is _t.CancellationResult, _t.Reason.TYPE)
            _act._true(state.runtime.cancellation.verify(state.holder_contract, proof))
            _act._need(proof.drained and (proof.acknowledged or (proof.terminated and proof.joined)),
                       _t.Reason.RECONCILIATION)
            if prepared is not None and not prepared.closed:
                state.runtime.act_api._cancel_effect(state, prepared, reason)
            _act._true(state.runtime.drain(state, proof=proof))
            _act._need(any(token is state.lease.holder_token for token in proof.released_tokens), _t.Reason.FOREIGN)
            _act._true(state.runtime.store.complete_holder_cancellation(state.lease, proof))
            state.owner_drained = _t.PredicateResult(_t.Truth.TRUE, None)
        except Exception:
            state.runtime.act_api.service_stopped = True
        classification = None
        try:
            classification = state.runtime.classifier.classify(state, reason=reason)
            _act._need(type(classification) is _t.FailureClassification
                and classification.scope == state.scope and classification.episode_id == state.episode_id,
                _t.Reason.FOREIGN)
            if reason is _t.Reason.DEADLINE:
                _act._need(classification.cause is _t.FailureClass.EPISODE_DEADLINE, _t.Reason.CONFLICT)
            if reason is _t.Reason.CANCELLED:
                _act._need(classification.cause is _t.FailureClass.EPISODE_CANCELLATION, _t.Reason.CONFLICT)
            _act._true(state.runtime.classifier.acknowledge(classification, state.plan.identity))
            state.failure = classification.cause
        except Exception:
            state.failure = _t.FailureClass.UNCLASSIFIED
            state.runtime.act_api.service_stopped = True
        status = (_t.TerminalStatus.INPUT_UNCERTAIN if state.input_disposition is _t.InputDisposition.INPUT_UNCERTAIN
            else _t.TerminalStatus.CANCELLED if reason is _t.Reason.CANCELLED else _t.TerminalStatus.REFUSED)
        state.outcome = _outcome(state, status, _act._unknown(), state.failure)
        return state.outcome


# @anchor:wa.scripts.runner.finish
def finish(state):
    with state.lock:
        if state.outcome is not None:
            return state.outcome
        try:
            _check(state)
            _act._need(state.pending_effect is None and not state.runtime.act_api.outstanding(), _t.Reason.RECONCILIATION)
            _act._passed(state.checkpoint)
            _act._need(state.input_disposition is not _t.InputDisposition.INPUT_UNCERTAIN, _t.Reason.RECONCILIATION)
            goal = _goal(state)
            _act._passed(goal)
            _act._true(state.runtime.drain(state, proof=None))
            state.owner_drained = _t.PredicateResult(_t.Truth.TRUE, None)
            candidate = _outcome(state, _t.TerminalStatus.COMPLETED, goal, None)
            ack = state.runtime.seal_owner.finish(candidate, ledger=state.runtime.ledger,
                deadline_ns=min(state.deadline_ns, state.runtime.clock() + state.runtime.act_api.policy.seal_ns))
            if state.verified_effects:
                _act._true(state.runtime.ledger.seal_episode(ack))
                _act._true(state.runtime.ledger.acknowledge_seal(ack, state.scope))
            else:
                # Explicit trusted no-input acknowledgement; no effect support event.
                _act._true(ack)
            _check(state)
            _act._true(state.runtime.store.release(state.lease, state.lease.holder_token))
            state.stopped = True
            state.outcome = candidate
            return candidate
        except _t.ContractError as error:
            return abort(state, error.reason)
        except Exception:
            return abort(state, _t.Reason.UNKNOWN)
