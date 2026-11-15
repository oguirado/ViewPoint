# @module {"id":"wa.perceive.tab_manager","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.session_registry","wa.perceive.cdp_port"],"book":"HB-WA-s1-perceive"}
"""Origin-validated CDP tab discovery with generation-safe target ownership.

A TabManager adopts acknowledged resources into the canonical SessionRegistry.
It deliberately does not own the browser or use a repository-local Handle type.
"""
from __future__ import annotations

from contextlib import nullcontext
import threading
import time

from .cdp_pipe import CdpError, CdpPipe, WriterFence, _callable_writer_guard
from .cdp_port import CdpTabError, _canonical_https_url, _matches_site_prefix
from .session_registry import (
    Handle, Receipt, SessionRegistry, RegistryError, StaleHandleError,
)


class _ScopedTransport:
    """Registry cleanup adapter: no connection-wide close, no unfenced send."""

    def __init__(self, manager):
        self._manager = manager

    def send(self, method, params=None, session_id=None, timeout=None):
        return self._manager._send(method, params, session_id, timeout)

    def detach_session(self, session_id):
        result = self.send("Target.detachFromTarget", {"sessionId": session_id}, timeout=5)
        cdp = self._manager.cdp
        attached = getattr(cdp, "_attached", None)
        if isinstance(attached, list):
            cdp._attached = [sid for sid in attached if sid != session_id]
        return result

    def close_target(self, target_id):
        return self.send("Target.closeTarget", {"targetId": target_id}, timeout=5)


# @anchor:wa.perceive.tab_manager.TabManager
class TabManager:
    """Manage only tabs adopted on this client; each generation is non-reusable.

    ensure_tab(url_prefix, open_url, *, selected_target=None) returns Handle.
    close(handle) returns canonical Receipt. stop(handle) is local and non-I/O.
    Provide the SAME WriterFence to CDP and scheduler to fence all writes.
    """

    def __init__(self, cdp, registry: SessionRegistry, *, clock=None, writer_fence=None):
        if not isinstance(registry, SessionRegistry):
            raise TypeError("registry must be a SessionRegistry")
        if clock is not None and not callable(clock):
            raise TypeError("clock must return monotonic nanoseconds")
        if not callable(getattr(cdp, "send", None)):
            raise TypeError("cdp requires send(method, params, session_id, timeout)")
        self.cdp = cdp
        self.registry = registry
        self.clock = time.monotonic_ns if clock is None else clock
        self.writer_fence = writer_fence if writer_fence is not None else getattr(cdp, "writer_fence", None)
        if writer_fence is not None and isinstance(cdp, CdpPipe):
            existing = getattr(cdp, "writer_fence", None)
            if existing is not None and existing is not writer_fence:
                raise ValueError("CDP and TabManager writer fences differ")
            cdp.writer_fence = writer_fence
        self._transport = _ScopedTransport(self)
        self._admission = threading.Lock()
        self._inflight = False

    def _writer_guard(self):
        fence = self.writer_fence
        if fence is None:
            return nullcontext()
        writing = getattr(fence, "writing", None)
        if callable(writing):
            return writing()
        if hasattr(fence, "__enter__") and hasattr(fence, "__exit__"):
            return fence
        if callable(fence):
            return _callable_writer_guard(fence)
        raise TypeError("unsupported writer fence")

    def _send(self, method, params=None, session_id=None, timeout=None):
        # Real CDP clients fence in CdpPipe.send and must not take an external
        # non-reentrant lock twice. A typed fake gets the equivalent outer guard.
        if isinstance(self.cdp, CdpPipe) and getattr(self.cdp, "writer_fence", None) is self.writer_fence:
            return self.cdp.send(method, params=params, session_id=session_id, timeout=timeout)
        with self._writer_guard():
            return self.cdp.send(method, params=params, session_id=session_id, timeout=timeout)

    def _navigate(self, session_id, url):
        navigate = getattr(self.cdp, "navigate", None)
        if callable(navigate):
            # CdpPipe.navigate delegates each write to the guarded send.
            # Typed test transports supply an in-memory navigate implementation.
            if isinstance(self.cdp, CdpPipe):
                return navigate(session_id, url)
            with self._writer_guard():
                return navigate(session_id, url)
        return self._send("Page.navigate", {"url": url}, session_id=session_id)

    def _discover(self, url_prefix, open_url, selected_target):
        prefix_origin, prefix = _canonical_https_url(url_prefix)
        route_origin, _ = _canonical_https_url(open_url)
        if prefix_origin != route_origin:
            raise CdpTabError("requested route is outside the configured site origin")
        if selected_target is not None and (
                not isinstance(selected_target, str) or not selected_target.strip()):
            raise CdpTabError("explicit target must be a nonempty targetId")
        # Input/origin validation must precede the first CDP dispatch.
        payload = self._send("Target.getTargets")
        if not isinstance(payload, dict) or not isinstance(payload.get("targetInfos"), list):
            raise CdpTabError("Target.getTargets returned invalid targetInfos")
        targets = payload["targetInfos"]
        matched = [
            t for t in targets
            if isinstance(t, dict)
            and t.get("type") == "page"
            and isinstance(t.get("targetId"), str)
            and bool(t.get("targetId"))
            and _matches_site_prefix(t.get("url", ""), url_prefix, prefix_origin, prefix)
        ]
        if selected_target is not None:
            matched = [t for t in matched if t["targetId"] == selected_target]
            if len(matched) != 1:
                raise CdpTabError("explicit target is not a matching page")
        elif len(matched) > 1:
            raise CdpTabError("AMBIGUOUS_TARGET: explicit selection required")
        if matched:
            return matched[0]["targetId"], False, targets
        created = self._send("Target.createTarget", {"url": open_url})
        target_id = created.get("targetId") if isinstance(created, dict) else None
        if (not isinstance(target_id, str) or not target_id or
                any(isinstance(t, dict) and t.get("targetId") == target_id for t in targets)):
            raise CdpTabError("creation outcome lacks a fresh target identity")
        return target_id, True, targets

    def ensure_tab(self, url_prefix, open_url, *, selected_target=None) -> Handle:
        # Busy detection holds metadata only for the flag; the lock is released
        # before discovery, attachment and navigation transport I/O.
        if not self._admission.acquire(blocking=False):
            raise CdpTabError("BUSY: tab discovery already in progress")
        try:
            if self._inflight:
                raise CdpTabError("BUSY: tab discovery already in progress")
            self._inflight = True
        finally:
            self._admission.release()
        try:
            target_id, target_owned, _ = self._discover(url_prefix, open_url, selected_target)
            newly_registered = False
            try:
                handle = self.registry.handle_for("cdp_tab", target_id)
                record = self.registry.resolve(handle)
                if record.transport is not self._transport:
                    raise CdpTabError("target already managed by a different transport")
                if record.session_id is None:
                    raise CdpTabError("target attachment is unresolved; explicit review required")
            except StaleHandleError:
                handle = self.registry.register(
                    "cdp_tab", target_id, self._transport, owned=target_owned)
                newly_registered = True
                record = self.registry.resolve(handle)
            try:
                if record.session_id is None:
                    reply = self._send(
                        "Target.attachToTarget", {"targetId": target_id, "flatten": True})
                    sid = reply.get("sessionId") if isinstance(reply, dict) else None
                    if not isinstance(sid, str) or not sid:
                        raise CdpTabError("attachment has no acknowledged sessionId")
                    # Reject reusing caller or worker sessions before any cleanup
                    # can accidentally detach another target's attachment.
                    if sid in getattr(self.cdp, "_caller_sessions", ()):
                        raise CdpTabError("attachment collides with caller session")
                    if sid in getattr(self.cdp, "_attached", ()):
                        raise CdpTabError("attachment collides with an owned session")
                    self.registry.bind_attachment(handle, sid, owned=True)
                    attached = getattr(self.cdp, "_attached", None)
                    if isinstance(attached, list):
                        attached.append(sid)
                    record = self.registry.resolve(handle)
                    self._send("Page.enable", session_id=sid)
                # Resolve just before navigation to catch prior local STOP.
                self.registry.resolve(handle)
                self._navigate(record.session_id, open_url)
                self.registry.advance_epoch(handle)
                return handle
            except Exception:
                if newly_registered:
                    # Tombstone metadata first; registry cleanup dispatches only
                    # acknowledged target/session ownership via _ScopedTransport.
                    self.registry.close(handle)
                else:
                    # A failed reuse cannot leave an active generation trusted.
                    self.registry.stop(handle)
                raise
        finally:
            with self._admission:
                self._inflight = False

    def close(self, handle: Handle) -> Receipt:
        """Idempotently retire a generation and clean only adopted resources."""
        return self.registry.close(handle)

    def stop(self, handle: Handle) -> Receipt:
        """Local STOP: fence just this attachment and defer all CDP cleanup."""
        try:
            record = self.registry.resolve(handle)
        except StaleHandleError:
            return self.registry.stop(handle)
        fence_session = getattr(self.cdp, "fence_session", None)
        if record.session_id is not None and callable(fence_session):
            fence_session(record.session_id, reason="STOP")
        return self.registry.stop(handle)
