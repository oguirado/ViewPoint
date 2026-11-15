# @module {"id":"wa.act.focus_policy","layer":2,"owner":"ACT","deps":["wa.perceive.session_registry","wa.act.target_lock_registry"],"book":"HB-WA-s2-act"}
"""Fail-closed, target-scoped focus grants for externally issued actor leases.

This module never focuses a window or issues a lease.  The registry and lock
provider are mandatory authorities; no permissive fallback is installed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading
import time
from typing import Protocol

try:
    from wender.perceive.session_registry import ActorLease, FocusLease, Handle, Observation
except ModuleNotFoundError as exc:
    if exc.name != "wender.perceive.session_registry":
        raise

    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: object

try:
    from wender.act.target_lock_registry import TargetLockRegistry
except ModuleNotFoundError as exc:
    if exc.name != "wender.act.target_lock_registry":
        raise

    class TargetLockRegistry(Protocol):
        def owns(self, lease: ActorLease, *, now_ns: int) -> bool: ...


class FocusRefused(ValueError):
    """No live, matching and explicitly focused TARGET authority."""


class FocusClass(str, Enum):
    BACKGROUND_SAFE = "background_safe"
    SESSION_SCOPED = "session_scoped"
    FOCUS_REQUIRED = "focus_required"
    REFUSE = "refuse"


def classify(path: str, op: str) -> FocusClass:
    """Classify without escalating unknown operations to background-safe."""
    if not isinstance(path, str) or not isinstance(op, str):
        return FocusClass.REFUSE
    if path == "api" and op in ("call", "invoke", "read"):
        return FocusClass.BACKGROUND_SAFE
    if path in ("ax", "dom") and op in ("click", "focus", "type", "select"):
        # SESSION_SCOPED describes a *candidate*; only positive backend evidence
        # can waive focus.  The CDP bridge below deliberately requires focus.
        return FocusClass.SESSION_SCOPED
    if path in ("ax", "dom", "coordinates") and op in (
            "press", "insert_text", "hover", "lpath_click"):
        return FocusClass.FOCUS_REQUIRED
    if path == "coordinates" and op == "click":
        return FocusClass.FOCUS_REQUIRED
    return FocusClass.REFUSE


def _handle_key(handle):
    if (getattr(handle, "kind", None) not in
            ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")
            or not isinstance(getattr(handle, "canonical_id", None), str)
            or not handle.canonical_id
            or not isinstance(getattr(handle, "generation", None), int)
            or isinstance(handle.generation, bool) or handle.generation < 0):
        raise FocusRefused("invalid canonical target handle")
    return (handle.kind, handle.canonical_id, handle.generation)


def _same_handle(a, b):
    try:
        return _handle_key(a) == _handle_key(b)
    except FocusRefused:
        return False


def _valid_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


class FocusPolicy:
    """Trusted external focus events plus one-use, nonce-bound focus grants.

    Required structural providers:
      registry.get(handle) -> TargetRecord with handle, epoch, closed;
      locks.owns(lease, *, now_ns) -> bool, validating minted nonce and owner.
    Neither provider may be replaced with an optimistic stub in production.
    """

    def __init__(self, registry, locks, *, clock=None):
        if registry is None or not callable(getattr(registry, "get", None)):
            raise FocusRefused("session registry.get prerequisite AUSENTE")
        if locks is None or not callable(getattr(locks, "owns", None)):
            raise FocusRefused("target lock ownership prerequisite AUSENTE")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be a monotonic nanosecond callable")
        self.registry = registry
        self.locks = locks
        self.clock = time.monotonic_ns if clock is None else clock
        self._mutex = threading.RLock()
        self._focus = {}
        self._modal = {}
        self._grants = {}
        self._spent = set()

    classify = staticmethod(classify)

    def _record(self, handle):
        _handle_key(handle)
        record = self.registry.get(handle)
        if (record is None or getattr(record, "closed", True)
                or not _same_handle(getattr(record, "handle", None), handle)
                or not _valid_integer(getattr(record, "epoch", None))):
            raise FocusRefused("unknown, closed or superseded target")
        return record

    def _check_lease(self, lease, now_ns):
        if not _valid_integer(now_ns):
            raise FocusRefused("invalid monotonic time")
        handle = getattr(lease, "handle", None)
        key = _handle_key(handle)
        if (not isinstance(getattr(lease, "owner", None), str) or not lease.owner
                or not _valid_integer(getattr(lease, "generation", None))
                or lease.generation != key[2]
                or not isinstance(getattr(lease, "nonce", None), bytes)
                or not lease.nonce
                or not _valid_integer(getattr(lease, "expires_ns", None))
                or now_ns >= lease.expires_ns):
            raise FocusRefused("malformed, foreign or expired actor lease")
        record = self._record(handle)
        if self.locks.owns(lease, now_ns=now_ns) is not True:
            raise FocusRefused("actor lease nonce is not current/owned")
        return record

    def set_focus(self, handle, *, domain: str, epoch: int) -> None:
        if not isinstance(domain, str) or not domain:
            raise FocusRefused("invalid input domain")
        record = self._record(handle)
        if not _valid_integer(epoch) or record.epoch != epoch:
            raise FocusRefused("stale focus geometry epoch")
        with self._mutex:
            modal = self._modal.get(domain)
            if modal is not None and not _same_handle(modal, handle):
                raise FocusRefused("modal owner blocks focus")
            self._focus[domain] = (_handle_key(handle), epoch)
            self._invalidate_domain(domain)

    def set_modal(self, handle: Handle | None, *, domain: str) -> None:
        if not isinstance(domain, str) or not domain:
            raise FocusRefused("invalid input domain")
        if handle is not None:
            self._record(handle)
        with self._mutex:
            self._modal[domain] = handle
            self._invalidate_domain(domain)

    def _invalidate_domain(self, domain):
        for key in tuple(self._grants):
            if key[1] == domain:
                del self._grants[key]

    def grant(self, lease, *, domain: str, now_ns=None) -> FocusLease:
        if not isinstance(domain, str) or not domain:
            raise FocusRefused("invalid input domain")
        now = self.clock() if now_ns is None else now_ns
        record = self._check_lease(lease, now)
        with self._mutex:
            if lease.nonce in self._spent:
                raise FocusRefused("consumed actor lease")
            if self._focus.get(domain) != (_handle_key(lease.handle), record.epoch):
                raise FocusRefused("no current explicit focus on target and epoch")
            modal = self._modal.get(domain)
            if modal is not None and not _same_handle(modal, lease.handle):
                raise FocusRefused("modal owned by another target")
            token = FocusLease(lease, domain, record.epoch)
            self._grants[(lease.nonce, domain)] = token
            return token

    def validate(self, lease, focus, observation, *, domain: str, now_ns=None) -> None:
        now = self.clock() if now_ns is None else now_ns
        record = self._check_lease(lease, now)
        if (not isinstance(domain, str) or not domain
                or not _same_handle(getattr(observation, "handle", None), lease.handle)
                or getattr(observation, "epoch", None) != record.epoch
                or not _same_handle(getattr(getattr(focus, "lease", None), "handle", None), lease.handle)
                or getattr(focus, "lease", None) != lease
                or getattr(focus, "domain", None) != domain
                or getattr(focus, "epoch", None) != record.epoch):
            raise FocusRefused("focus/observation/lease target or epoch mismatch")
        with self._mutex:
            if (lease.nonce in self._spent
                    or self._grants.get((lease.nonce, domain)) is not focus
                    or self._focus.get(domain) != (_handle_key(lease.handle), record.epoch)):
                raise FocusRefused("focus grant forged, retired or superseded")
            modal = self._modal.get(domain)
            if modal is not None and not _same_handle(modal, lease.handle):
                raise FocusRefused("modal owner changed")

    def consume(self, focus) -> None:
        """Single-use authorization immediately before the first side effect."""
        lease = getattr(focus, "lease", None)
        domain = getattr(focus, "domain", None)
        nonce = getattr(lease, "nonce", None)
        with self._mutex:
            if nonce in self._spent or self._grants.get((nonce, domain)) is not focus:
                raise FocusRefused("focus grant was already consumed")
            self._spent.add(nonce)
            self._grants.pop((nonce, domain))
