# @module {"id":"wa.perceive.harvest_broker","layer":1,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}
"""HarvestBroker lifecycle coordinator over SessionRegistry and TabManager.

Coordinates targets with injected transports, capsule aliases, bounded per-target
command lanes, ownership-aware teardown, crash fencing, and STOP tombstones.
Guarantees that no registry metadata mutex spans transport I/O, waits, callbacks,
or executor admission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore


# ---------------------------------------------------------------------------
# Canonical frozen value dataclasses
# Exact positional fields and types as specified in contract.
# ---------------------------------------------------------------------------

from wender.perceive.session_registry import (
    Handle, TargetRecord, FrameRef, Observation, Receipt, ActionRequest,
    ActorLease, FocusLease, StreamSpec, StreamToken, Ticket, ChangeResult,
    PerceptionResult, WindowFrames, WindowDescription, SessionRegistry,
)

# ---------------------------------------------------------------------------
# Specific Exceptions
# ---------------------------------------------------------------------------

class BrokerError(Exception):
    """Base exception for HarvestBroker errors."""
    pass


class StoppedError(BrokerError):
    """Raised when an operation is attempted after stop() or on a stopped target."""
    pass


class FencedLaneError(BrokerError):
    """Raised when a command is attempted on a fenced or overflowed command lane."""
    pass


class AmbiguousTargetError(BrokerError):
    """Raised when discovery matches multiple conflicting targets."""
    pass


class InvalidOriginError(BrokerError):
    """Raised when target origin fails normalization or validation."""
    pass


# ---------------------------------------------------------------------------
# Internal Target State Tracker
# ---------------------------------------------------------------------------

class _TargetState:
    """Mutable per-incarnation state; lane fields are protected by lane_lock."""

    def __init__(self, handle: Handle, transport: object, owned: bool,
                 epoch: int = 0, session_id: Optional[str] = None,
                 attachment_owned: bool = False, lane_limit: int = 32):
        self.handle = handle
        self.transport = transport
        self.owned = owned
        self.epoch = epoch
        self.session_id = session_id
        self.attachment_owned = attachment_owned
        self.closed = False
        self.stopped = False
        self.fenced = False
        self.lane_limit = lane_limit
        self.lane_lock = threading.Lock()
        self.inflight_count = 0
        self.active_waiters: Set[str] = set()
        self.tombstoned_request_ids: Set[str] = set()
        self.latest_request_id: Optional[str] = None
        self.request_incarnations: Dict[str, int] = {}
        self.pending_replies: Dict[str, Any] = {}
        self.ingress_events: List[dict] = []
        self.cleanup_pending = False
        self.cleanup_started = False
        self.cleanup_done = False
        self.cleanup_error: Optional[str] = None

    def to_record(self) -> TargetRecord:
        return TargetRecord(self.handle, self.transport, self.owned, self.epoch,
                            self.session_id, self.attachment_owned, self.closed)

# ---------------------------------------------------------------------------
# HarvestBroker Implementation
# ---------------------------------------------------------------------------

class HarvestBroker:
    """Complete HarvestBroker lifecycle coordinator over SessionRegistry and TabManager.

    Supported target kinds:
      - cdp_tab
      - x11_window
      - xvfb_screen
      - capsule
      - godot_window
    """

    SUPPORTED_KINDS = frozenset({
        "cdp_tab",
        "x11_window",
        "xvfb_screen",
        "capsule",
        "godot_window",
    })

    def __init__(
        self,
        registry: Any = None,
        tab_manager: Any = None,
        *,
        writer_fence: Any = None,
        lane_capacity: int = 32,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.registry = SessionRegistry() if registry is None else registry
        self.tab_manager = tab_manager
        self.writer_fence = writer_fence
        if type(lane_capacity) is not int or lane_capacity < 1:
            raise ValueError("lane_capacity must be a positive integer")
        self.lane_capacity = lane_capacity
        self.clock = clock or time.monotonic

        # Global metadata locks follow the lock order:
        # fence -> registry -> budget -> target metadata
        self._fence_lock = threading.RLock()
        self._registry_lock = threading.RLock()

        # Broker state
        self._stopped = False
        self._next_generation = 1
        self._action_counter = 1

        # Canonical tracking:
        # Map canonical_id -> target state
        self._targets_by_canonical: Dict[str, _TargetState] = {}
        # Map handle -> target state
        self._targets_by_handle: Dict[Handle, _TargetState] = {}
        # Map alias keys to canonical_id (e.g. ("capsule", alias_id) -> canonical_id)
        self._aliases: Dict[Tuple[str, str], str] = {}
        # Deferred cleanup transport tasks (due to STOP)
        self._deferred_cleanups: List[Tuple[Handle, object, bool, bool, Optional[str]]] = []
        # Tombstones for STOP
        self._stop_tombstones: Set[str] = set()

    # -----------------------------------------------------------------------
    # Canonical Resolution & Aliasing (INV-M1-1)
    # -----------------------------------------------------------------------

    def _resolve_canonical(self, kind: str, canonical_id: str) -> str:
        """Resolve aliased actuation surfaces (capsule and child screen/godot_window)

        to the same canonical resource when addressing the same destination.
        """
        # Strip or normalize capsule alias prefixes
        if (kind, canonical_id) in self._aliases:
            return self._aliases[(kind, canonical_id)]

        # Check if canonical_id is formatted as a capsule referencing a screen
        # e.g. "capsule:screen-1" or "xvfb_screen:screen-1"
        if ":" in canonical_id:
            parts = canonical_id.split(":", 1)
            prefix, dest = parts[0], parts[1]
            if prefix in ("capsule", "xvfb_screen", "godot_window", "x11_window"):
                return dest

        return canonical_id

    def bind_alias(self, alias_kind: str, alias_id: str, canonical_id: str) -> None:
        """Explicitly bind an alias actuation surface to a canonical destination resource."""
        with self._registry_lock:
            self._aliases[(alias_kind, alias_id)] = canonical_id

    # -----------------------------------------------------------------------
    # Target Registration (INV-M1-1, INV-M1-3)
    # -----------------------------------------------------------------------

    def register(self, kind: str, canonical_id: str, transport: object, *,
                 owned: bool = True, session_id: Optional[str] = None,
                 attachment_owned: bool = True, successor_handoff: Optional[Tuple[Handle, int, str]] = None) -> Handle:
        """Strict producer registration and identity/epoch reconciliation."""
        if kind not in self.SUPPORTED_KINDS:
            raise ValueError(f"Unsupported target kind: {kind!r}")
        if type(attachment_owned) is not bool:
            raise ValueError("attachment_owned must be bool")
        if session_id is None:
            # A default True argument does not assert ownership of an absent attachment.
            attachment_owned = False
        with self._registry_lock:
            if self._stopped:
                raise StoppedError("Cannot register on stopped HarvestBroker")
            canonical = self._resolve_canonical(kind, canonical_id)
            existing = self._validate_successor_handoff(self._targets_by_canonical.get(canonical), successor_handoff)
            if existing is not None:
                if existing.closed or existing.stopped:
                    if not existing.cleanup_done:
                        raise StoppedError("Prior incarnation is awaiting cleanup")
                else:
                    if (existing.transport is not transport or existing.owned != owned or
                            existing.session_id != session_id or
                            existing.attachment_owned != attachment_owned):
                        raise AmbiguousTargetError("Conflicting canonical target registration")
                    if kind != existing.handle.kind:
                        self.registry.bind_alias(kind, canonical, existing.handle)
                    return existing.handle
            # No compatibility fallback: the actual registry requires kind/id/transport.
            handle = self.registry.register(kind, canonical, transport, owned=owned)
            record = self.registry.resolve(handle)
            if session_id is not None:
                record = self.registry.bind_attachment(handle, session_id,
                                                       owned=attachment_owned)
            handle = Handle(record.handle.kind, record.handle.canonical_id,
                            record.handle.generation)
            if (handle.canonical_id != canonical or record.transport is not transport or
                    record.owned != owned or record.closed):
                raise BrokerError("Registry returned an incompatible target identity")
            state = _TargetState(handle, record.transport, record.owned,
                                 epoch=record.epoch, session_id=record.session_id,
                                 attachment_owned=record.attachment_owned,
                                 lane_limit=self.lane_capacity)
            self._targets_by_canonical[canonical] = state
            self._targets_by_handle[handle] = state
            if successor_handoff is not None: self._stop_tombstones.discard(canonical)
            return handle

    # -----------------------------------------------------------------------
    # Navigation / Epoch Transition (INV-M1-1)
    # -----------------------------------------------------------------------

    def _require_live(self, handle: Handle) -> _TargetState:
        """Caller holds _registry_lock; no transport I/O occurs here."""
        if self._stopped:
            raise StoppedError("HarvestBroker is stopped; commands are rejected.")
        state = self._targets_by_handle.get(handle)
        if state is None:
            raise ValueError(f"Target handle not registered: {handle}")
        if state.stopped or state.closed or state.handle.canonical_id in self._stop_tombstones:
            raise StoppedError(f"Target {handle} is retired")
        if state.fenced:
            raise FencedLaneError(f"Target {handle} lane is fenced")
        return state

    def advance_epoch(self, handle: Handle) -> int:
        with self._registry_lock:
            state = self._require_live(handle)
            with state.lane_lock:
                epoch = self.registry.advance_epoch(handle)
                state.epoch = epoch
                return epoch

    def _before_transport_send(self, handle: Handle, method: str) -> None:
        """Undecorated deterministic test seam before the final STOP check."""

    @staticmethod
    def _send(transport: object, method: str, params: dict,
              session_id: Optional[str]) -> Any:
        sender = getattr(transport, "send", None)
        if callable(sender):
            if session_id is None:
                return sender(method, params)
            return sender(method, params, session_id=session_id)
        command = getattr(transport, "command", None)
        if callable(command):
            return command(method, params)
        if callable(transport):
            return transport(method, params)
        raise AttributeError("Transport has no send/command callable")

    # -----------------------------------------------------------------------
    # Command Dispatch (INV-M1-2, Lock Ordering)
    # -----------------------------------------------------------------------

    def command(self, handle: Handle, method: str,
                params: Optional[dict] = None) -> dict:
        """Reserve, fence before transmission, reconcile reply, release exactly once."""
        params = {} if params is None else dict(params)
        with self._registry_lock:
            state = self._require_live(handle)
            with state.lane_lock:
                if state.inflight_count >= state.lane_limit:
                    state.fenced = True
                    raise FencedLaneError("Bounded target lane capacity exceeded")
                state.inflight_count += 1
                epoch = state.epoch
        request_id = None
        finalize = False
        try:
            # STOP/epoch may advance while preparation is paused.
            self._before_transport_send(handle, method)
            with self._registry_lock:
                if self._require_live(handle) is not state:
                    raise StoppedError("Target incarnation was replaced")
                with state.lane_lock:
                    if epoch != state.epoch:
                        raise FencedLaneError("Epoch advanced before transmission")
                    request_id = f"req-{handle.generation}-{self._action_counter}"
                    self._action_counter += 1
                    state.latest_request_id = request_id
                    state.active_waiters.add(request_id)
                    state.request_incarnations[request_id] = epoch
            # No metadata lock crosses the external transport call.
            reply = None
            transport_error = None
            try:
                reply = self._send(state.transport, method, params, state.session_id)
            except Exception as exc:
                transport_error = exc
            with self._registry_lock:
                with state.lane_lock:
                    timed_out = request_id in state.tombstoned_request_ids
                    retired = self._stopped or state.stopped or state.closed or state.fenced
                    epoch_changed = epoch != state.epoch
            if retired or timed_out:
                return {"status": "discarded_late_reply", "request_id": request_id}
            if epoch_changed:
                return {"status": "discarded_epoch_mismatch", "request_id": request_id}
            if transport_error is not None:
                raise transport_error
            if isinstance(reply, dict):
                return {**reply, "broker_request_id": request_id}
            return {"result": reply, "broker_request_id": request_id}
        finally:
            with self._registry_lock:
                with state.lane_lock:
                    if state.inflight_count <= 0:
                        raise AssertionError("command reservation released twice")
                    state.inflight_count -= 1
                    if request_id is not None:
                        state.active_waiters.discard(request_id)
                        state.request_incarnations.pop(request_id, None)
                        state.tombstoned_request_ids.discard(request_id)
                    if (state.closed and state.cleanup_pending and
                            not state.cleanup_started and state.inflight_count == 0):
                        state.cleanup_started = True
                        finalize = True
            if finalize:
                self._perform_cleanup(state)

    # -----------------------------------------------------------------------
    # Ingress Event Routing (INV-M1-2)
    # -----------------------------------------------------------------------

    def route_ingress_event(self, handle: Handle, event: dict) -> None:
        with self._registry_lock:
            state = self._require_live(handle)
            with state.lane_lock:
                if len(state.ingress_events) >= state.lane_limit:
                    state.fenced = True
                    raise FencedLaneError("Ingress lane overflow")
                state.ingress_events.append(dict(event))

    def mark_timed_out_request(self, handle: Handle, request_id: str) -> None:
        with self._registry_lock:
            state = self._targets_by_handle.get(handle)
            if state is None:
                return
            with state.lane_lock:
                if request_id in state.active_waiters:
                    state.tombstoned_request_ids.add(request_id)
                    state.active_waiters.discard(request_id)

    # -----------------------------------------------------------------------
    # Ownership-Aware Teardown and STOP (Gate G M1 5, INV-M1-3)
    # -----------------------------------------------------------------------

    def _retire(self, state: _TargetState, *, stop: bool) -> None:
        """Caller holds broker lock. Registry STOP tombstones before cleanup."""
        if stop:
            state.stopped = True
            self._stop_tombstones.add(state.handle.canonical_id)
        if not state.cleanup_pending and not state.cleanup_done:
            self.registry.stop(state.handle)  # never performs transport I/O
            state.cleanup_pending = True
            self._deferred_cleanups.append((state.handle, state.transport, state.owned,
                                            state.attachment_owned, state.session_id))

    def stop(self, handle: Optional[Handle] = None) -> None:
        with self._registry_lock:
            if handle is None:
                self._stopped = True
                candidates = list(self._targets_by_handle.values())
            else:
                state = self._targets_by_handle.get(handle)
                candidates = [] if state is None else [state]
            for state in candidates:
                if not state.stopped and not state.cleanup_done:
                    self._retire(state, stop=True)

    @staticmethod
    def _is_observed(transport: object, method: str, value: str) -> bool:
        """Explicit injected terminal-state probe, never tombstone membership."""
        probe = getattr(transport, method, None)
        return callable(probe) and probe(value) is True

    def _cleanup_observed(self, state: _TargetState) -> bool:
        transport = state.transport
        if state.attachment_owned and state.session_id:
            if not self._is_observed(transport, "is_session_detached", state.session_id):
                return False
        if state.owned:
            return self._is_observed(transport, "is_terminated", state.handle.canonical_id)
        return (state.attachment_owned and state.session_id is not None) or self._is_observed(
            transport, "is_terminated", state.handle.canonical_id)

    def reconcile_cleanup(self, handle: Handle) -> bool:
        """Acknowledge deferred cleanup only after externally observed completion."""
        with self._registry_lock:
            state = self._targets_by_handle.get(handle)
            if state is None:
                raise ValueError("Unknown handle")
            if state.cleanup_done:
                return True
            if not state.cleanup_pending or state.inflight_count:
                return False
        # Transport probes can callback/block: no broker lock is held.
        if not self._cleanup_observed(state):
            return False
        with self._registry_lock:
            if state.inflight_count or not state.cleanup_pending:
                return False
            self.registry.acknowledge_cleanup(handle)
            state.cleanup_pending = False
            state.cleanup_done = True
            self._deferred_cleanups = [entry for entry in self._deferred_cleanups
                                       if entry[0] != handle]
            return True

    def _perform_cleanup(self, state: _TargetState) -> Receipt:
        """Close owned resources off-lock; absence of proof keeps quarantine."""
        handle, transport = state.handle, state.transport
        problems = []
        with self._registry_lock:
            fenced = self._stopped or state.stopped
        if fenced:
            return Receipt("close", handle, "cleanup_pending", "STOP forbids cleanup I/O")
        if state.attachment_owned and state.session_id:
            try:
                detacher = getattr(transport, "detach_session", None)
                if not callable(detacher):
                    detacher = getattr(transport, "detach", None)
                if not callable(detacher):
                    raise BrokerError("target-scoped session detacher unavailable")
                detacher(state.session_id)
            except Exception as exc:
                problems.append(f"detach: {type(exc).__name__}: {exc}")
        if state.owned:
            try:
                closer = getattr(transport, "close_target", None)
                sender = getattr(transport, "send", None)
                if callable(closer):
                    closer(handle.canonical_id)
                elif handle.kind == "cdp_tab" and callable(sender):
                    sender("Target.closeTarget", {"targetId": handle.canonical_id})
                else:
                    # Never close a whole multiplexed connection with live siblings.
                    close = getattr(transport, "close", None)
                    with self._registry_lock:
                        shared = any(other is not state and other.transport is transport and
                                     not other.closed and not other.stopped
                                     for other in self._targets_by_handle.values())
                    if not callable(close) or callable(sender) or shared:
                        raise BrokerError("safe target-scoped closer unavailable")
                    close()
            except Exception as exc:
                problems.append(f"close: {type(exc).__name__}: {exc}")
        if problems:
            state.cleanup_error = "; ".join(problems)
        if not problems and self.reconcile_cleanup(handle):
            return Receipt("close", handle, "closed", None)
        return Receipt("close", handle, "cleanup_pending",
                       state.cleanup_error or "remote termination or detach not observed")

    def close(self, handle: Handle) -> Receipt:
        with self._registry_lock:
            state = self._targets_by_handle.get(handle)
            action_id = f"close-{handle.generation}-{self._action_counter}"
            self._action_counter += 1
            if state is None:
                return Receipt(action_id, handle, "ignored", "Unknown handle")
            if state.cleanup_done:
                return Receipt(action_id, handle, "closed", "Already closed")
            if state.closed:
                return Receipt(action_id, handle, "cleanup_pending", "Prior close pending")
            state.closed = True
            self._retire(state, stop=False)
            with state.lane_lock:
                if state.inflight_count:
                    return Receipt(action_id, handle, "cleanup_pending", "inflight lane is draining")
                state.cleanup_started = True
        outcome = self._perform_cleanup(state)
        return Receipt(action_id, handle, outcome.status, outcome.reason)

    # -----------------------------------------------------------------------
    # Discovery (INV-M1-3)
    # -----------------------------------------------------------------------

    def discover_or_create(
        self,
        origin: str,
        factory_fn: Optional[Callable[[], Tuple[str, object]]] = None,
        *,
        kind: str = "cdp_tab",
        owned: bool = True,
    ) -> Handle:
        """Discovery validates normalized origin, refuses ambiguous matches,

        and creates only on miss (INV-M1-3).
        """
        # Validate normalized origin
        norm_origin = self._normalize_origin(origin)

        with self._registry_lock:
            # Check for existing matches in registered targets
            matches = []
            for h, state in self._targets_by_handle.items():
                if not state.closed:
                    # Match by origin prefix or canonical id
                    if state.handle.canonical_id == norm_origin or norm_origin in state.handle.canonical_id:
                        matches.append(state)

            if len(matches) > 1:
                # Refuses ambiguous matches!
                raise AmbiguousTargetError(
                    f"Ambiguous target match for origin {origin!r}: {len(matches)} targets matched."
                )

            if len(matches) == 1:
                return matches[0].handle

        # Create only on miss
        if factory_fn is None:
            raise ValueError(f"No existing target for origin {norm_origin} and no factory provided.")

        new_id, new_transport = factory_fn()
        return self.register(kind, new_id, new_transport, owned=owned)

    @staticmethod
    def _normalize_origin(origin: str) -> str:
        """Validate and normalize origin to canonical scheme://host(:port)."""
        if not origin or not isinstance(origin, str):
            raise InvalidOriginError(f"Invalid origin: {origin!r}")

        s = origin.strip().lower()
        if not (s.startswith("https://") or s.startswith("http://")):
            raise InvalidOriginError(f"Origin must have http or https scheme: {origin!r}")

        # Basic parse
        parts = s.split("://", 1)
        scheme = parts[0]
        rest = parts[1].split("/", 1)[0]
        if not rest:
            raise InvalidOriginError(f"Origin host missing in {origin!r}")

        return f"{scheme}://{rest}"

    # -----------------------------------------------------------------------
    # Inspection Helpers
    # -----------------------------------------------------------------------

    def get_target_record(self, handle: Handle) -> Optional[TargetRecord]:
        """Return TargetRecord for handle."""
        with self._registry_lock:
            state = self._targets_by_handle.get(handle)
            return state.to_record() if state else None

    @property
    def is_stopped(self) -> bool:
        with self._registry_lock:
            return self._stopped

    @property
    def deferred_cleanup_count(self) -> int:
        with self._registry_lock:
            return len(self._deferred_cleanups)


    def _validate_successor_handoff(self, existing, declaration):
        """Explicit activation: (predecessor handle, generation, closer receipt id).

        Ordinary registration preserves canonical STOP and cannot send. A
        declared successor may clear it only after cleanup/join reconciliation
        has retained the predecessor's completed closure receipt. Caller holds
        the registry lock; this validation performs no transport I/O.
        """
        if declaration is None:
            return existing
        if existing is None or not existing.stopped or not existing.cleanup_done:
            raise StoppedError("Successor handoff requires completed stopped predecessor")
        receipt = getattr(existing, "closure_receipt", None)
        expected = (existing.handle, existing.handle.generation,
                    getattr(receipt, "receipt_id", None))
        if receipt is None or declaration != expected:
            raise StoppedError("Successor handoff does not reference the closure receipt")
        return existing

    # @anchor:wa.perceive.harvest_broker.reconcile_acknowledged_cleanup
    def reconcile_acknowledged_cleanup(self, handle: Handle, closure_receipt=None) -> bool:
        """Mirror registry acknowledgement by a separate trusted cleanup owner.

        Metadata only, including after STOP. The external owner must complete
        teardown and verify its join before acknowledging registry quarantine.
        A still-pending tombstone is never accepted as completion evidence.
        """
        with self._registry_lock:
            state = self._targets_by_handle.get(handle)
            if state is None:
                raise ValueError("Unknown handle")
            retired, _, pending = self.registry.tombstone(handle)
            if (retired.handle != handle or not retired.closed or pending or
                    not state.stopped or state.inflight_count):
                return False
            if closure_receipt is not None:
                if (closure_receipt.handle != handle or
                        closure_receipt.generation != handle.generation or
                        closure_receipt.status != "closed" or not closure_receipt.receipt_id):
                    return False
                state.closure_receipt = closure_receipt
            state.closed = True
            state.cleanup_pending = False
            state.cleanup_done = True
            self._deferred_cleanups = [entry for entry in self._deferred_cleanups
                                       if entry[0] != handle]
            return True

__all__ = [
    "HarvestBroker",
    "Handle",
    "TargetRecord",
    "FrameRef",
    "Observation",
    "Receipt",
    "ActionRequest",
    "ActorLease",
    "FocusLease",
    "StreamSpec",
    "StreamToken",
    "Ticket",
    "ChangeResult",
    "PerceptionResult",
    "WindowFrames",
    "WindowDescription",
    "BrokerError",
    "StoppedError",
    "FencedLaneError",
    "AmbiguousTargetError",
    "InvalidOriginError",
]
