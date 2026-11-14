# @module {"id":"wa.perceive.session_registry","layer":1,"owner":"PERCEIVE","deps":["wa.state.handles"],"book":"HB-WA-s1-perceive"}
"""Local, generation-safe target identities and immutable perception value records.

This module owns *metadata*, not a CDP connection or a browser lifecycle.
Callers confirm discovery/navigation before registering or advancing epochs.
Transport cleanup is target-scoped, performed after releasing the registry lock.
"""

from __future__ import annotations

from dataclasses import replace
import threading
import time


from wender.state.handles import (
    SUPPORTED_KINDS, RegistryError, StaleHandleError, AliasConflictError,
    QuarantinedTargetError, FrameValidationError, Handle, TargetRecord, FrameRef,
    Observation, Receipt, ActionRequest, ActorLease, FocusLease, StreamSpec,
    StreamToken, Ticket, ChangeResult, PerceptionResult, WindowFrames,
    WindowDescription, _integer, _name, _handle, _epoch, _sequence,
    _owned_array, _perception_handle,
)


def _alias_key(kind: str, canonical_id: str) -> tuple[str, str]:
    # Explicitly shared destination IDs identify a capsule and its child screen.
    family = "capsule_screen" if kind in ("capsule", "xvfb_screen") else kind
    return family, canonical_id

# @anchor:wa.perceive.session_registry.SessionRegistry
class SessionRegistry:
    """In-process metadata registry with epoch and generation fencing.

    A registered handle stays valid only until close/stop. No method here sends a
    navigation command; callers must advance_epoch only on *validated* success.
    Reconciliation of uncertain remote state is explicit, never an implicit retry.
    """

    def __init__(self, *, clock=None) -> None:
        self._clock = time.monotonic_ns if clock is None else clock
        if not callable(self._clock):
            raise RegistryError("clock must be callable")
        self._lock = threading.Lock()
        self._generation = 0
        self._active: dict[Handle, TargetRecord] = {}
        self._aliases: dict[tuple[str, str], Handle] = {}
        self._tombstones: dict[Handle, TargetRecord] = {}
        self._closed_ns: dict[Handle, int] = {}
        self._quarantine: set[tuple[str, str]] = set()
        self._pending: dict[Handle, tuple[tuple[str, str], ...]] = {}
        self._last_frame_seq: dict[tuple[Handle, int], int] = {}
        self._latest_frame_digest: dict[tuple[Handle, int], str] = {}

    def register(self, kind: str, canonical_id: str, transport: object, *, owned: bool) -> Handle:
        if kind not in SUPPORTED_KINDS:
            raise RegistryError(f"unsupported target kind: {kind!r}")
        _name(canonical_id, "canonical_id")
        if type(owned) is not bool:
            raise RegistryError("owned must be bool")
        key = _alias_key(kind, canonical_id)
        with self._lock:
            if key in self._quarantine:
                raise QuarantinedTargetError("destination has unresolved cleanup/STOP")
            old = self._aliases.get(key)
            if old is not None:
                record = self._active.get(old)
                if record is None:
                    raise StaleHandleError("alias refers to a closed target")
                if record.owned != owned:
                    raise AliasConflictError("duplicate registration cannot change ownership")
                # Kind-family aliases must also preserve the original transport.
                # A caller's matching string never authorizes a transport switch.
                if record.transport is not transport:
                    raise AliasConflictError("active destination already bound to another transport")
                return old
            self._generation += 1  # Python integers are unbounded, never masked/wrapped.
            h = Handle(kind, canonical_id, self._generation)
            self._active[h] = TargetRecord(h, transport, owned, 0, None, False, False)
            self._aliases[key] = h
            return h

    def bind_alias(self, kind: str, canonical_id: str, handle: Handle) -> Handle:
        """Declare a second trusted address for the same destination, without I/O."""
        if kind not in SUPPORTED_KINDS:
            raise RegistryError(f"unsupported target kind: {kind!r}")
        _name(canonical_id, "canonical_id")
        key, h = _alias_key(kind, canonical_id), _handle(handle)
        with self._lock:
            self._require_active(h)
            if key in self._quarantine:
                raise QuarantinedTargetError("destination has unresolved cleanup/STOP")
            incumbent = self._aliases.get(key)
            if incumbent is not None and incumbent != h:
                raise AliasConflictError("alias belongs to another live destination")
            self._aliases[key] = h
            return h

    def handle_for(self, kind: str, canonical_id: str) -> Handle:
        if kind not in SUPPORTED_KINDS:
            raise RegistryError(f"unsupported target kind: {kind!r}")
        _name(canonical_id, "canonical_id")
        with self._lock:
            h = self._aliases.get(_alias_key(kind, canonical_id))
            if h is None or h not in self._active:
                raise StaleHandleError("no active target at alias")
            return h

    def _require_active(self, h: Handle) -> TargetRecord:
        # Caller holds the registry lock. Exact tuple identity includes generation.
        record = self._active.get(h)
        if record is None:
            raise StaleHandleError(f"stale or closed handle: {h!r}")
        return record

    def resolve(self, handle: Handle) -> TargetRecord:
        h = _handle(handle)
        with self._lock:
            return self._require_active(h)

    def get(self, handle: Handle) -> TargetRecord | None:
        """Scheduler view: active record or None, with strict handle ingress.

        A closed incarnation is not scheduler-live. No alias, transport or
        external callback is evaluated; the complete triple selects the record.
        """
        try:
            h = _handle(handle)
        except RegistryError as exc:
            raise RegistryError(f"SessionRegistry.get invalid handle: {exc}") from exc
        with self._lock:
            record = self._active.get(h)
            return record if record is not None and not record.closed else None

    def canonical_resource(self, handle: Handle) -> str:
        """Authenticated scheduler lock key for a *live* registry incarnation.

        Bind aliases only with bind_alias and a live handle. Neither a caller
        string nor a tombstoned generation can select shared-resource authority.
        """
        try:
            h = _handle(handle)
        except RegistryError as exc:
            raise RegistryError(
                f"SessionRegistry.canonical_resource invalid handle: {exc}"
            ) from exc
        with self._lock:
            record = self._require_active(h)
            if record.closed:
                raise StaleHandleError("closed record cannot be a scheduler resource")
            key = _alias_key(record.handle.kind, record.handle.canonical_id)
            if self._aliases.get(key) != h:
                raise StaleHandleError("primary resource alias is not authenticated")
            return f"{key[0]}:{key[1]}"

    def bind_attachment(self, handle: Handle, session_id: str, *, owned: bool) -> TargetRecord:
        h = _handle(handle)
        _name(session_id, "session_id")
        if type(owned) is not bool:
            raise RegistryError("owned must be bool")
        with self._lock:
            record = self._require_active(h)
            if record.session_id is not None and (record.session_id, record.attachment_owned) != (session_id, owned):
                raise AliasConflictError("attachment already bound to a different session/owner")
            for other in self._active.values():
                if other.handle != h and other.session_id == session_id and other.transport is record.transport:
                    raise AliasConflictError("attachment session belongs to another target")
            record = replace(record, session_id=session_id, attachment_owned=owned)
            self._active[h] = record
            return record

    def advance_epoch(self, handle: Handle) -> int:
        """A caller invokes this *after* a validated navigation/geometry change."""
        h = _handle(handle)
        with self._lock:
            record = self._require_active(h)
            next_epoch = record.epoch + 1
            self._active[h] = replace(record, epoch=next_epoch)
            return next_epoch

    def admit_frame(self, handle: Handle, frame_seq: int, rgb: object, *, epoch: int | None = None) -> FrameRef:
        h = _handle(handle)
        _sequence(frame_seq)
        with self._lock:
            active_epoch = self._require_active(h).epoch
        target_epoch = active_epoch if epoch is None else _epoch(epoch)
        if target_epoch != active_epoch:
            raise FrameValidationError("stream epoch is stale")
        # Work on the potentially large image without holding the metadata lock.
        frame = FrameRef.from_rgb(h, target_epoch, frame_seq, rgb)
        with self._lock:
            if self._require_active(h).epoch != target_epoch:
                raise FrameValidationError("stream epoch changed during frame admission")
            key = (h, target_epoch)
            if frame_seq <= self._last_frame_seq.get(key, -1):
                raise FrameValidationError("frame sequence repeated or regressed")
            self._last_frame_seq[key] = frame_seq
            self._latest_frame_digest[key] = frame.digest
        return frame

    def admit_observation(self, handle: Handle, frame: FrameRef, *, state: dict, dpr_ppm: int) -> Observation:
        h = _handle(handle)
        with self._lock:
            epoch = self._require_active(h).epoch
            latest = self._last_frame_seq.get((h, epoch), -1)
            digest = self._latest_frame_digest.get((h, epoch))
        if (frame.handle != h or frame.epoch != epoch or
                frame.frame_seq != latest or frame.digest != digest or latest < 0):
            raise FrameValidationError("unregistered or stale frame provenance")
        return Observation(h, epoch, frame.frame_seq, dpr_ppm, state, frame)

    def _retire(self, h: Handle, *, stop: bool) -> tuple[TargetRecord | None, tuple[tuple[str, str], ...]]:
        """Caller holds the lock. Tombstone before any external effect."""
        record = self._active.pop(h, None)
        if record is None:
            return None, ()
        tombstone = replace(record, closed=True)
        self._tombstones[h] = tombstone
        keys = tuple(key for key, bound in self._aliases.items() if bound == h)
        for key in keys:
            del self._aliases[key]
            self._quarantine.add(key)
        self._pending[h] = keys
        return tombstone, keys

    def _finish_cleanup(self, h: Handle) -> None:
        with self._lock:
            for key in self._pending.pop(h, ()):
                self._quarantine.discard(key)

    def _close(self, handle: Handle, *, stop: bool) -> Receipt:
        h = _handle(handle)
        closed_ns = self._clock()  # injected callbacks must never run under lock
        with self._lock:
            record, keys = self._retire(h, stop=stop)
            if record is None:
                if h in self._tombstones:
                    return Receipt("stop" if stop else "close", h, "already_closed", None)
                return Receipt("stop" if stop else "close", h, "stale", "no matching live generation")
            self._closed_ns[h] = closed_ns
        if stop:
            # Preserve pending cleanup and quarantine; do NOT send anything after STOP.
            return Receipt("stop", h, "stopped", "transport cleanup deferred")
        failures = []
        transport = record.transport
        # Never call connection-wide transport.close() if a CDP-style send exists.
        if record.attachment_owned and record.session_id:
            detach = getattr(transport, "detach_session", None)
            if callable(detach):
                try:
                    detach(record.session_id)
                except Exception as exc:
                    failures.append(f"detach: {type(exc).__name__}: {exc}")
            else:
                failures.append("detach: target-scoped detacher unavailable")
        if record.owned:
            close_target = getattr(transport, "close_target", None)
            sender = getattr(transport, "send", None)
            close = getattr(transport, "close", None)
            try:
                if callable(close_target):
                    close_target(h.canonical_id)
                elif h.kind == "cdp_tab" and callable(sender):
                    sender("Target.closeTarget", {"targetId": h.canonical_id})
                elif callable(close) and not callable(sender):
                    close()
                else:
                    failures.append("close_target: target-scoped closer unavailable")
            except Exception as exc:
                failures.append(f"close_target: {type(exc).__name__}: {exc}")
        if failures:
            return Receipt("close", h, "cleanup_pending", "; ".join(failures))
        self._finish_cleanup(h)
        return Receipt("close", h, "closed", None)

    def close(self, handle: Handle) -> Receipt:
        """Retire generation and clean only acknowledged owned resources once."""
        return self._close(handle, stop=False)

    def stop(self, handle: Handle) -> Receipt:
        """Fence locally and keep a quarantine tombstone; perform zero transport I/O."""
        return self._close(handle, stop=True)

    def acknowledge_cleanup(self, handle: Handle) -> None:
        """Explicit operator reconciliation of deferred remote cleanup (no I/O)."""
        h = _handle(handle)
        with self._lock:
            if h not in self._tombstones:
                raise StaleHandleError("no tombstone to reconcile")
            for key in self._pending.pop(h, ()):
                self._quarantine.discard(key)

    def tombstone(self, handle: Handle) -> tuple[TargetRecord, int, bool]:
        """Return closed record, monotonic time in ns and pending-cleanup status."""
        h = _handle(handle)
        with self._lock:
            if h not in self._tombstones:
                raise StaleHandleError("no tombstone for handle")
            return self._tombstones[h], self._closed_ns[h], h in self._pending
