# @module {"id":"wa.scripts.frame_wait","layer":3,"owner":"ACT","deps":["wa.scripts.types","wa.scripts.predicates","wa.act.proto1_settle_clock"],"book":"HB-WA-s2-act"}
"""Bounded semantic waits over injected, supervised committed-observation owners.

Origin: scripts_mesh/build/spec/s6_w.md. Composition must authenticate the
scheduler/reader cancellation boundary and Holder/HolderContract association.
No production reader or containment proof is inferred from these interfaces.
"""
from dataclasses import dataclass as _dataclass
from threading import Lock as _Lock
from typing import Protocol as _Protocol
from . import types as _t, predicates as _predicates


# @anchor:wa.scripts.frame_wait._need
def _need(condition, reason):
    if not condition:
        raise _t.ContractError(reason)


# @anchor:wa.scripts.frame_wait._true
def _true(check):
    _need(type(check) is _t.AdmissionCheck and check.result is _t.Truth.TRUE,
          check.reason if type(check) is _t.AdmissionCheck and check.reason else _t.Reason.MISSING)


# @anchor:wa.scripts.frame_wait._refusal
def _refusal(reason):
    return _t.AcquisitionResult(None, reason,
        _t.AcquisitionDisposition.CANCELLED if reason is _t.Reason.CANCELLED else _t.AcquisitionDisposition.REFUSED,
        None, None, _t.MissingReason.INCOMPLETE, _t.InputDisposition.NOT_SENT)


# @anchor:wa.scripts.frame_wait._WaitState
@_dataclass(slots=True, repr=False)
class _WaitState:
    deadline: int
    attempts: int = 0
    accepted: int = 0
    previous: object = None
    latest_identity: str = ''
    accepted_identity: str = ''
    next_acquisition_ns: int = 0
    cancellation_registration: object = None
    settled_evidence: object = None
    current: object = None


# @anchor:wa.scripts.frame_wait.due_events
def due_events(events, previous_tick, current_tick, *, cursor):
    """Return (due tuple, next cursor) over an immutable sorted tick sequence."""
    _need(type(events) is tuple and all(type(tick) is int and tick >= 0 for tick in events), _t.Reason.TYPE)
    _need(all(a <= b for a, b in zip(events, events[1:])), _t.Reason.SCHEMA)
    _need(all(type(value) is int for value in (previous_tick, current_tick, cursor)), _t.Reason.TYPE)
    _need(0 <= previous_tick <= current_tick and 0 <= cursor <= len(events), _t.Reason.BOUNDS)
    if previous_tick == current_tick:
        return (), cursor
    start = cursor
    while start < len(events) and events[start] < previous_tick:
        start += 1
    end = start
    while end < len(events) and events[end] < current_tick:
        end += 1
    return events[start:end], end


# @anchor:wa.scripts.frame_wait.FrameWait
class FrameWait:
    """Frozen FrameWaitPort with explicit trusted item-local composition.

    boundary.check_installation(reader, scheduler, control) proves bounded
    calls and shared work accounting. control.check(holder, contract, scope,
    deadline) authenticates association and current authority on every poll.
    contexts.context supplies the complete private evaluator keyword mapping;
    quiet.check attests all applicable read-only quiet signals. Neither an
    observation notification nor transport acknowledgement supplies evidence.
    Successful acquisition ownership transfers to the caller. Everything else
    is released here; failed release or unproved cancellation stops service.
    """
    # @anchor:wa.scripts.frame_wait.FrameWait.__init__
    def __init__(self, *, observations, scheduler, control, cancellation,
                 holder_contract, contexts, quiet, boundary, clock,
                 store=None, lease=None, settle_coordinator=None, max_notification_bytes=None):
        _need(type(holder_contract) is _t.HolderContract and callable(clock), _t.Reason.TYPE)
        self._observations, self._scheduler = observations, scheduler
        self._control, self._cancellation = control, cancellation
        self._contract, self._contexts, self._quiet = holder_contract, contexts, quiet
        self._clock, self._boundary = clock, boundary
        self._store, self._lease = store, lease
        _need((store is None) == (lease is None), _t.Reason.MISSING)
        self._settle = settle_coordinator
        self._notification_lock = _Lock()
        self._latest_identity = ''
        self._max_notification_bytes = max_notification_bytes
        self._settled_evidence = None
        self._entry = _Lock()
        self._failed_release = None
        self.service_stopped = False
        self.attempt_count = self.accepted_publication_count = 0
        _true(boundary.check_installation(observations, scheduler, control))

    # @anchor:wa.scripts.frame_wait.FrameWait._now
    def _now(self):
        now = self._clock()
        _need(type(now) is int and now >= 0, _t.Reason.TYPE)
        return now

    # @anchor:wa.scripts.frame_wait.FrameWait._check
    def _check(self, cancel, scope, deadline):
        _need(not self.service_stopped, _t.Reason.RECONCILIATION)
        _true(self._boundary.check_installation(self._observations, self._scheduler, self._control))
        _need(type(cancel) is _t.Holder and cancel.scope == scope == self._contract.scope, _t.Reason.FOREIGN)
        _need(self._now() <= deadline, _t.Reason.DEADLINE)
        _true(self._control.check(cancel, self._contract, scope, deadline))

    # @anchor:wa.scripts.frame_wait.FrameWait._release
    def _release(self, result):
        # Reader owns exact-token deduplication; never infer drain from counts.
        try:
            _true(self._observations.release(result.owner_token))
        except Exception:
            self.service_stopped = True
            self._failed_release = result
            raise _t.ContractError(_t.Reason.RECONCILIATION) from None

    # @anchor:wa.scripts.frame_wait.FrameWait._drop
    def _drop(self, state):
        result, state.current = state.current, None
        self._release(result)

    # @anchor:wa.scripts.frame_wait.FrameWait._cancel
    def _cancel(self):
        try:
            proof = self._cancellation.request(self._contract)
            _need(type(proof) is _t.CancellationResult, _t.Reason.TYPE)
            _true(self._cancellation.verify(self._contract, proof))
            _need(proof.drained and (proof.acknowledged or (proof.terminated and proof.joined)), _t.Reason.RECONCILIATION)
            if self._store is not None:
                _need(any(token is self._lease.holder_token for token in proof.released_tokens), _t.Reason.FOREIGN)
                _true(self._store.complete_holder_cancellation(self._lease, proof))
        except Exception:
            self.service_stopped = True

    # @anchor:wa.scripts.frame_wait.FrameWait._run
    def _run(self, after_identity, deadline, cancel, *, predicate=None, scope=None, budget=None):
        state = None
        entered = False
        try:
            _need(self._entry.acquire(blocking=False), _t.Reason.CONFLICT)
            entered = True
            _need(type(deadline) is int and deadline > 0 and type(after_identity) is str, _t.Reason.TYPE)
            _need(type(cancel) is _t.Holder, _t.Reason.TYPE)
            scope = cancel.scope if scope is None else scope
            deadline = min(deadline, cancel.absolute_deadline_ns, self._contract.absolute_deadline_ns)
            if budget is not None:
                _need(type(budget) is _t.WaitPolicy and type(predicate) is _t.PredicateSpec, _t.Reason.TYPE)
                deadline = min(deadline, budget.absolute_deadline_ns, self._now() + budget.semantic_wait_ms * 1000000)
            self._check(cancel, scope, deadline)
            state = _WaitState(deadline)
            attempts = 1 if budget is None else budget.acquisition_attempts
            spacing = 0 if budget is None else budget.acquisition_spacing_ms * 1000000
            last_reason = _t.Reason.MISSING
            while state.attempts < attempts:
                self._check(cancel, scope, deadline)
                if budget is not None and state.attempts:
                    remaining = attempts - state.attempts
                    duration = deadline - self._now()
                    delay = max(spacing, (duration + remaining - 1) // remaining)
                    boundary = min(deadline, self._now() + delay)
                    state.next_acquisition_ns = boundary
                    self._scheduler.wait_until_ns(boundary, cancel=cancel)
                    _need(self._now() >= boundary, _t.Reason.DEADLINE)
                    self._check(cancel, scope, deadline)
                with self._notification_lock:
                    state.latest_identity = self._latest_identity
                state.attempts += 1
                result = self._observations.acquire(after_identity, deadline_ns=deadline, cancel=cancel)
                _need(type(result) is _t.AcquisitionResult, _t.Reason.TYPE)
                state.current = result if result.observation is not None else None
                self._check(cancel, scope, deadline)
                if result.observation is None:
                    last_reason = result.reason
                    if last_reason in (_t.Reason.CANCELLED, _t.Reason.DEADLINE):
                        raise _t.ContractError(last_reason)
                    continue
                observation = result.observation
                _true(self._observations.check(observation, scope=scope, deadline_ns=deadline))
                _need(observation.scope == scope and observation.capture_end_ns <= deadline, _t.Reason.STALE)
                previous = state.previous
                if previous is not None and observation.publication_identity == previous.publication_identity:
                    _need(observation == previous, _t.Reason.CONFLICT)
                    self._drop(state)
                    continue
                if observation.publication_identity == after_identity:
                    self._drop(state)
                    continue
                if previous is not None:
                    _need((observation.producer_identity, observation.producer_generation, observation.authority) ==
                          (previous.producer_identity, previous.producer_generation, previous.authority)
                          and observation.capture_end_ns >= previous.capture_end_ns, _t.Reason.STALE)
                state.previous = observation
                state.accepted_identity = observation.publication_identity
                state.accepted += 1
                after_identity = observation.publication_identity
                if predicate is None:
                    state.current = None
                    return result
                context = self._contexts.context(predicate, observation)
                for key in ('max_nodes', 'max_evidence_rows', 'max_inventory_rows'):
                    _need(type(context.get(key)) is int and context[key] > 0, _t.Reason.BOUNDS)
                evaluation = _predicates.evaluate(predicate, scope, **context)
                _need(type(evaluation) is _t.PredicateResult, _t.Reason.TYPE)
                quiet = self._quiet.check(observation, quiet_window_ns=budget.quiet_window_ms * 1000000,
                                          deadline_ns=deadline)
                self._check(cancel, scope, deadline)
                if evaluation.value is _t.Truth.TRUE and type(quiet) is _t.AdmissionCheck and quiet.result is _t.Truth.TRUE:
                    state.current = None
                    return result
                last_reason = evaluation.reason or (quiet.reason if type(quiet) is _t.AdmissionCheck else None) or _t.Reason.MISSING
                self._drop(state)
                if self._now() == deadline:
                    break
            return _refusal(last_reason)
        except _t.ContractError as error:
            if error.reason in (_t.Reason.CANCELLED, _t.Reason.DEADLINE):
                self._cancel()
            return _refusal(error.reason)
        except Exception:
            self._cancel()
            return _refusal(_t.Reason.UNKNOWN)
        finally:
            if state is not None:
                self.attempt_count = state.attempts
                self.accepted_publication_count = state.accepted
                if state.current is not None:
                    try:
                        self._drop(state)
                    except _t.ContractError:
                        pass
            if entered:
                self._entry.release()

    # @anchor:wa.scripts.frame_wait.FrameWait.outstanding
    def outstanding(self):
        """Retained failed-release evidence; never declare drain from counters."""
        return () if self._failed_release is None else (self._failed_release.owner_token,)

    # @anchor:wa.scripts.frame_wait.FrameWait.next_observation
    def next_observation(self, after_identity: str, absolute_deadline_ns: int,
                         cancel: _t.Holder) -> _t.AcquisitionResult:
        return self._run(after_identity, absolute_deadline_ns, cancel)

    # @anchor:wa.scripts.frame_wait.FrameWait.wait_until
    def wait_until(self, predicate, scope, budget: _t.WaitPolicy,
                   absolute_deadline_ns: int, cancel: _t.Holder) -> _t.AcquisitionResult:
        return self._run('', absolute_deadline_ns, cancel, predicate=predicate, scope=scope, budget=budget)

    # @anchor:wa.scripts.frame_wait.FrameWait.notify
    def notify(self, publication_identity):
        """Coalesce publication hints without acquiring or charging an attempt."""
        _need(type(publication_identity) is str and bool(publication_identity), _t.Reason.TYPE)
        _need(type(self._max_notification_bytes) is int and self._max_notification_bytes > 0
              and len(publication_identity.encode('utf-8')) <= self._max_notification_bytes, _t.Reason.BOUNDS)
        with self._notification_lock:
            self._latest_identity = publication_identity

    # @anchor:wa.scripts.frame_wait.FrameWait.settle_effect
    def settle_effect(self, adapter, profile, config, signal_reader, *, cancel, scope):
        """Settle only separately authenticated APPLIED adapters, under authority.

        Application owner validation precedes this call. Coordinator enforces
        source, epoch, frame, causal identity and every applicable signal.
        """
        from wender.act.proto1_settle_clock import SettleCoordinator, AdapterInput, SettleConfig
        _need(type(self._settle) is SettleCoordinator and type(adapter) is AdapterInput
              and type(config) is SettleConfig, _t.Reason.MISSING)
        deadline = min(config.deadline_ns, cancel.absolute_deadline_ns, self._contract.absolute_deadline_ns)
        _need(config.deadline_ns == deadline and adapter.outcome == 'APPLIED', _t.Reason.DEADLINE)
        self._check(cancel, scope, deadline)
        _true(self._boundary.check_settle(adapter, profile, config, signal_reader))
        failed = False
        try:
            result = self._settle.settle(adapter, profile, config, signal_reader)
            self._check(cancel, scope, deadline)
        except Exception:
            failed = True
        if failed:
            self._cancel()
            raise _t.ContractError(_t.Reason.MISSING)
        self._settled_evidence = result
        return result

    # @anchor:wa.scripts.frame_wait.FrameWait.release_observation
    def release_observation(self, owner_token):
        """Release caller-transferred ownership through the exact reader owner."""
        try:
            check = self._observations.release(owner_token)
            _true(check)
            return check
        except Exception:
            self.service_stopped = True
        raise _t.ContractError(_t.Reason.RECONCILIATION)


# @anchor:wa.scripts.frame_wait._ObservationOwner
class _ObservationOwner(_Protocol):
    # @anchor:wa.scripts.frame_wait._ObservationOwner.acquire
    def acquire(self, after_identity, *, deadline_ns, cancel): ...

    # @anchor:wa.scripts.frame_wait._ObservationOwner.release
    def release(self, owner_token): ...

    # @anchor:wa.scripts.frame_wait._ObservationOwner.check
    def check(self, observation, *, scope, deadline_ns): ...


# @anchor:wa.scripts.frame_wait._WaitScheduler
class _WaitScheduler(_Protocol):
    # @anchor:wa.scripts.frame_wait._WaitScheduler.wait_until_ns
    def wait_until_ns(self, boundary_ns, *, cancel): ...
