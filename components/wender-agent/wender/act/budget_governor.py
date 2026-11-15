# @module {"id":"wa.act.budget_governor","layer":1,"owner":"ACT","deps":["wa.perceive.session_registry"],"book":"HB-WA-s2-act"}
"""Thread-safe admission accounting for mutating operations.

This is a budget primitive, not a scheduler, STOP fence or transport writer.
Reserve before authorization; under the *host's* send fence, commit BEFORE
possibly issuing each mutating send. Every retry requires a fresh reservation.
An uncertain send keeps its committed charge. Only a provably unsent pending
reservation may be cancelled. This module never calls external services.
"""

from dataclasses import dataclass
import secrets
import threading
import weakref

from wender.perceive.session_registry import Handle


SUPPORTED_KINDS = frozenset((
    "cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"
))


class BudgetError(RuntimeError):
    """Admission or reservation-state refusal, without a side effect."""


class ConfigurationRequired(BudgetError):
    """Required budget/deadline configuration is not supplied."""


class BudgetExhausted(BudgetError):
    """Either atomic account lacks sufficient unreserved credit."""


class DeadlineInfeasible(BudgetError):
    """The original absolute deadline cannot accommodate one service cap."""


class StaleHandle(BudgetError):
    """A later incarnation has already been observed for this identity."""


class ReservationInactive(BudgetError):
    """A reservation is not pending in the governor that issued it."""


def _nonnegative_int(value, label):
    if type(value) is not int or value < 0:
        raise ValueError("%s must be a nonnegative int" % label)
    return value


def _handle_snapshot(handle):
    """Accept structural handles but keep an immutable canonical local snapshot."""
    try:
        kind = handle.kind
        canonical_id = handle.canonical_id
        generation = handle.generation
    except AttributeError as exc:
        raise ValueError("handle requires kind, canonical_id and generation") from exc
    if not isinstance(kind, str) or kind not in SUPPORTED_KINDS:
        raise ValueError("unsupported handle kind: %r" % (kind,))
    if (not isinstance(canonical_id, str) or not canonical_id
            or canonical_id != canonical_id.strip() or "\x00" in canonical_id):
        raise ValueError("handle canonical_id must be a canonical string")
    if type(generation) is not int or generation < 1:
        raise ValueError("handle generation must be a positive int")
    return Handle(kind, canonical_id, generation)


def _resource_key(kind, canonical_id):
    # Only host-provided identical canonical_ids attest that a capsule and its
    # xvfb child refer to one destination. Never infer aliases from display names.
    if kind in ("capsule", "xvfb_screen"):
        return ("capsule_screen", canonical_id)
    return (kind, canonical_id)


def _configured_key(value):
    """Allow Handle, (kind, id), or bare canonical-id as dict budget keys."""
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("per-target budget key must not be empty")
        return ("*", value)
    if isinstance(value, tuple) and len(value) == 2:
        kind, canonical_id = value
        if not isinstance(kind, str) or kind not in SUPPORTED_KINDS:
            raise ValueError("unsupported per-target budget kind")
        if not isinstance(canonical_id, str) or not canonical_id.strip():
            raise ValueError("per-target budget canonical_id must be nonempty")
        return _resource_key(kind, canonical_id)
    h = _handle_snapshot(value)
    return _resource_key(h.kind, h.canonical_id)


@dataclass(frozen=True)
class Reservation:
    """One governor-issued, single-use, unsent budget hold."""

    handle: Handle
    cost: int
    deadline_ns: int
    nonce: bytes


@dataclass(frozen=True)
class _Pending:
    reservation: Reservation
    resource_key: tuple
    incarnation_key: tuple
    action_id: str | None


class BudgetGovernor:
    """One atomic budget ledger, shared across all target admissions.

    remaining excludes both pending and committed charges. commit does
    not refund or charge a second time: it changes *held* into *spent*. cancel
    frees only pending holds, once. No callback or other metadata lock is taken
    while holding this governor's private lock.

    The host must provide trusted handles, service estimates, STOP/ownership
    fencing, deadline checks at dispatch, and quarantine on uncertain outcomes.
    The governor does not claim those missing integrations are implemented.
    """

    def __init__(self, global_budget: int, per_target_budget: int | dict,
                 *, service_cap_ns: int | None = None,
                 clock=None, telemetry=None):
        self._global_limit = _nonnegative_int(global_budget, "global_budget")
        if type(per_target_budget) is int:
            self._default_target_limit = _nonnegative_int(
                per_target_budget, "per_target_budget")
            self._configured_limits = None
        elif isinstance(per_target_budget, dict):
            self._default_target_limit = None
            limits = {}
            for raw_key, raw_limit in per_target_budget.items():
                key = _configured_key(raw_key)
                limit = _nonnegative_int(raw_limit, "per-target budget")
                if key in limits and limits[key] != limit:
                    raise ValueError("conflicting budgets for canonical target %r" % (key,))
                limits[key] = limit
            self._configured_limits = limits
        else:
            raise ValueError("per_target_budget must be a nonnegative int or dict")
        if service_cap_ns is not None:
            if type(service_cap_ns) is not int or service_cap_ns <= 0:
                raise ValueError("service_cap_ns must be a positive int or None")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable or None")
        # A concrete list is an append-only, non-callback event sink while the
        # accounting mutex is held. No untrusted provider code runs in that lock.
        if telemetry is not None and type(telemetry) is not list:
            raise ValueError("telemetry must be a list or None")
        self._service_cap_ns = service_cap_ns
        self._clock = clock
        self._telemetry = telemetry
        self._lock = threading.Lock()
        self._remaining_global = self._global_limit
        self._remaining_targets = {}
        self._spent_global = 0
        self._spent_targets = {}
        self._active = {}
        # Weak references preserve token *identity* for idempotent late cancel
        # without retaining all historical reservations indefinitely.
        self._issued = weakref.WeakValueDictionary()
        self._latest_generation = {}

    def _limit_for(self, resource_key):
        if self._configured_limits is None:
            return self._default_target_limit
        if resource_key in self._configured_limits:
            return self._configured_limits[resource_key]
        wildcard_key = ("*", resource_key[1])
        if wildcard_key in self._configured_limits:
            return self._configured_limits[wildcard_key]
        raise ConfigurationRequired("CONFIG_REQUIRED: missing budget for %r" % (resource_key,))

    def _refund_locked(self, pending):
        cost = pending.reservation.cost
        self._remaining_global += cost
        key = pending.resource_key
        self._remaining_targets[key] += cost

    def _event_locked(self, event: str, pending: _Pending,
                      consumed: bool | None = None) -> None:
        """Record only action-bound transitions atomically with their ledger."""
        if pending.action_id is not None:
            handle = pending.reservation.handle
            self._telemetry.append({
                "event": event, "action_id": pending.action_id,
                "handle": (handle.kind, handle.canonical_id, handle.generation),
                "cost": pending.reservation.cost, "consumed": consumed,
            })

    def _spend_locked(self, pending: _Pending) -> None:
        cost = pending.reservation.cost
        self._spent_global += cost
        key = pending.resource_key
        self._spent_targets[key] = self._spent_targets.get(key, 0) + cost

    def reserve(self, handle: Handle, cost: int, *, deadline_ns: int,
                now_ns: int | None = None,
                action_id: str | None = None) -> Reservation:
        """Atomically hold global and canonical-target credits or refuse both.

        The deadline is absolute and never extended for a retry. now_ns is
        injected by the scheduler/host clock; no time or transport I/O here.
        """
        h = _handle_snapshot(handle)
        if type(cost) is not int or cost <= 0:
            raise ValueError("cost must be a positive int")
        if action_id is not None:
            if (not isinstance(action_id, str) or not action_id
                    or action_id != action_id.strip() or "\x00" in action_id):
                raise ValueError("action_id must be a nonempty canonical string")
            if self._telemetry is None:
                raise ConfigurationRequired("CONFIG_REQUIRED: telemetry is absent")
            if self._clock is None:
                raise ConfigurationRequired("CONFIG_REQUIRED: clock is absent")
        if now_ns is None:
            # Preserve the legacy default of zero. Scheduler calls require the
            # injected monotonic clock, not a synthetic timestamp.
            now_ns = self._clock() if action_id is not None else 0
        _nonnegative_int(deadline_ns, "deadline_ns")
        _nonnegative_int(now_ns, "now_ns")
        if self._service_cap_ns is None:
            raise ConfigurationRequired("CONFIG_REQUIRED: service_cap_ns is absent")
        if deadline_ns - now_ns < self._service_cap_ns:
            raise DeadlineInfeasible("DEADLINE_INFEASIBLE: cannot fit service cap")

        key = _resource_key(h.kind, h.canonical_id)
        incarnation_key = (h.kind, h.canonical_id)
        # CSPRNG is invoked outside the budget metadata lock.
        nonce = secrets.token_bytes(16)
        with self._lock:
            limit = self._limit_for(key)
            old_generation = self._latest_generation.get(incarnation_key)
            if old_generation is not None and h.generation < old_generation:
                raise StaleHandle("STALE_HANDLE: superseded incarnation")
            if nonce in self._issued:
                raise BudgetError("RESERVATION_COLLISION: nonce has been issued")

            # Plan retirement without changing any account. A refused
            # admission must not retire old holds, publish a generation or
            # initialize a target's remaining balance.
            superseded = ()
            if old_generation is None or h.generation > old_generation:
                superseded = tuple(
                    (old_nonce, pending)
                    for old_nonce, pending in self._active.items()
                    if (pending.incarnation_key == incarnation_key
                        and pending.reservation.handle.generation < h.generation)
                )
            refundable = sum(p.reservation.cost for _, p in superseded)
            remaining_target = self._remaining_targets.get(key, limit)
            if self._remaining_global + refundable < cost:
                raise BudgetExhausted("BUDGET_EXHAUSTED: global credits")
            if remaining_target + refundable < cost:
                raise BudgetExhausted("BUDGET_EXHAUSTED: per-target credits")

            # Both ledgers, incarnation fence and token issuance move under
            # the same lock only after all admission refusals have passed.
            self._remaining_targets.setdefault(key, limit)
            for old_nonce, pending in superseded:
                self._event_locked("invalidate", pending, consumed=False)
                del self._active[old_nonce]
                self._refund_locked(pending)
            if old_generation is None or h.generation > old_generation:
                self._latest_generation[incarnation_key] = h.generation

            reservation = Reservation(h, cost, deadline_ns, nonce)
            pending = _Pending(reservation, key, incarnation_key, action_id)
            self._remaining_global -= cost
            self._remaining_targets[key] -= cost
            self._active[nonce] = pending
            self._issued[nonce] = reservation
            self._event_locked("reserve", pending)
            return reservation

    def commit(self, reservation: Reservation) -> None:
        """Irrevocably consume a pending hold before one possible mutating send.

        Call while the host owns the STOP/dispatch fence. Neither an exception
        nor an UNKNOWN reply authorizes refund or automatic retry.
        """
        if not isinstance(reservation, Reservation):
            raise ValueError("expected a Reservation")
        with self._lock:
            pending = self._active.get(reservation.nonce)
            if pending is None or pending.reservation is not reservation:
                raise ReservationInactive("RESERVATION_INACTIVE: not a pending issued token")
            self._event_locked("commit", pending, consumed=True)
            del self._active[reservation.nonce]
            self._spend_locked(pending)

    def settle(self, reservation: Reservation, *, consumed: bool) -> None:
        """Settle once: spend possibly-sent work, refund proven-unsent work.

        consumed=False is the caller's positive no-send assertion; callers
        must use the same host writer fence as WindowScheduler. An UNKNOWN or
        unacknowledged send is not evidence for release. After commit or a
        first settlement this method always refuses a repeated settlement.
        """
        if not isinstance(reservation, Reservation):
            raise ValueError("expected a Reservation")
        if type(consumed) is not bool:
            raise ValueError("consumed must be a bool")
        with self._lock:
            pending = self._active.get(reservation.nonce)
            if pending is None or pending.reservation is not reservation:
                raise ReservationInactive(
                    "RESERVATION_INACTIVE: not a pending issued token")
            self._event_locked("settle", pending, consumed=consumed)
            del self._active[reservation.nonce]
            if consumed:
                self._spend_locked(pending)
            else:
                self._refund_locked(pending)

    def cancel(self, reservation: Reservation) -> None:
        """Release a demonstrably unsent hold once; duplicate calls are no-ops.

        A committed send can never be refunded, even if its reply was lost.
        """
        if not isinstance(reservation, Reservation):
            raise ValueError("expected a Reservation")
        with self._lock:
            # Only an originally issued object gets idempotent cancellation.
            # A copied nonce or token issued by another governor is foreign
            # even after its apparent nonce has left our live table.
            if (type(reservation.nonce) is not bytes
                    or self._issued.get(reservation.nonce) is not reservation):
                raise ReservationInactive("RESERVATION_INACTIVE: foreign token identity")
            pending = self._active.get(reservation.nonce)
            if pending is None:
                return
            self._event_locked("cancel", pending, consumed=False)
            del self._active[reservation.nonce]
            self._refund_locked(pending)

    release = cancel

    @property
    def remaining_global(self) -> int:
        with self._lock:
            return self._remaining_global

    @property
    def spent_global(self) -> int:
        with self._lock:
            return self._spent_global

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._active)

    def remaining_for(self, handle: Handle) -> int:
        h = _handle_snapshot(handle)
        key = _resource_key(h.kind, h.canonical_id)
        with self._lock:
            return self._remaining_targets.get(key, self._limit_for(key))

    def spent_for(self, handle: Handle) -> int:
        h = _handle_snapshot(handle)
        key = _resource_key(h.kind, h.canonical_id)
        with self._lock:
            return self._spent_targets.get(key, 0)
