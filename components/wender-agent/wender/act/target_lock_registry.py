# @module {"id":"wa.act.target_lock_registry","layer":2,"owner":"ACT","deps":["wa.perceive.session_registry"],"book":"HB-WA-s2-act"}
"""Nonblocking canonical TARGET actuation leases; not a dispatch integration.

All instances backed by the *same* session registry share a process-local slot
map. Acquire never executes transport. Sent/unknown actions remain fenced until
a caller establishes positive quiescence via reconcile_quarantine(..., True).
"""
from dataclasses import dataclass
import secrets
import threading
import time

try:
    from wender.perceive.session_registry import ActorLease, Handle
except ModuleNotFoundError as exc:
    # The upstream provider is not merged yet. Do not hide internal import errors.
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


class TargetBusy(RuntimeError):
    """Canonical destination already belongs to an unsent or sent actor."""


class TargetLockRefused(ValueError):
    """Invalid, stale, expired, forged, or non-quiescent lease transition."""


class PrerequisiteAbsent(TargetLockRefused):
    """Registry cannot establish the target's current identity."""


_SUPPORTED_KINDS = frozenset({
    "cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"
})


@dataclass
class _Claim:
    registry: object
    resource: str
    handle: object
    lease: object
    # The issued object is deliberately retained for identity checks, but it is
    # not the authority for its own fields: frozen dataclasses can still be
    # changed with object.__setattr__.  Keep the mint-time values separately.
    identity: tuple
    nonce: bytes
    owner: str
    generation: int
    expires_ns: int
    inflight: bool = False
    quarantined: bool = False


# Slot scope is a registry identity plus a canonical actuation destination.
# Global storage deliberately retains a stuck (quarantined) claim if its
# TargetLockRegistry wrapper is dropped, never releasing an active command.
_SLOT_GUARD = threading.RLock()
_SLOTS = {}
_NONCES = {}
_LEASES = {}


def _integer(value, field):
    if type(value) is not int or value < 0:
        raise TargetLockRefused("%s must be a nonnegative integer" % field)
    return value


def _identity(handle):
    """Return structural identity; do not require cross-repository class identity."""
    kind = getattr(handle, "kind", None)
    canonical_id = getattr(handle, "canonical_id", None)
    generation = getattr(handle, "generation", None)
    if kind not in _SUPPORTED_KINDS:
        raise TargetLockRefused("unsupported handle kind")
    if not isinstance(canonical_id, str) or not canonical_id.strip():
        raise TargetLockRefused("missing canonical target identifier")
    _integer(generation, "handle generation")
    return (kind, canonical_id, generation)


class TargetLockRegistry:
    """Process-local, shared, fail-closed TARGET lock for a session registry.

    ``registry`` must provide get(handle), resolve(handle), lookup(handle) or
    get_record(handle) with an authoritative current TargetRecord-like value.
    A record must expose .handle and .closed. If capsule and child window have
    different handle ids, ``registry.canonical_resource(handle)`` (or
    record.actuation_resource) must map both to the same nonempty resource id.
    In the absence of an alias resolver, record.handle.canonical_id is used.
    """

    def __init__(self, registry, *, clock=None):
        if registry is None:
            raise PrerequisiteAbsent("session registry required")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self.registry = registry
        self._clock = clock if clock is not None else time.monotonic_ns

    def _now(self):
        return _integer(self._clock(), "monotonic clock")

    def _record(self, handle):
        for name in ("get", "resolve", "lookup", "get_record"):
            method = getattr(self.registry, name, None)
            if callable(method):
                try:
                    record = method(handle)
                except (KeyError, LookupError) as exc:
                    raise PrerequisiteAbsent("target has no current registry record") from exc
                if record is None:
                    raise PrerequisiteAbsent("target has no current registry record")
                if bool(getattr(record, "closed", False)):
                    raise PrerequisiteAbsent("target is closed")
                current = getattr(record, "handle", None)
                if current is None:
                    raise PrerequisiteAbsent("registry record lacks a handle")
                _identity(current)
                if current.generation != handle.generation:
                    raise TargetLockRefused("stale handle generation")
                return record
        raise PrerequisiteAbsent("session registry lacks a lookup interface")

    def _resource(self, handle, record):
        resolver = getattr(self.registry, "canonical_resource", None)
        if callable(resolver):
            resource = resolver(handle)
        elif hasattr(record, "actuation_resource"):
            resource = record.actuation_resource
        else:
            resource = record.handle.canonical_id
        if hasattr(resource, "canonical_id"):
            resource = resource.canonical_id
        if not isinstance(resource, str) or not resource.strip():
            raise PrerequisiteAbsent("canonical actuation resource is absent")
        return resource

    def acquire(self, handle, owner, *, expires_ns, now_ns=None):
        """Acquire immediately or raise TargetBusy; expiry never reaps slots."""
        kind, canonical_id, generation = _identity(handle)
        # Normalize structural handles and string subclasses before taking the
        # metadata lock.  User properties/conversions must never run under it.
        handle = Handle(str.__str__(str(kind)), str.__str__(canonical_id), generation)
        if not isinstance(owner, str) or not owner.strip():
            raise TargetLockRefused("owner must be a nonempty string")
        owner = str.__str__(owner)
        expiration = _integer(expires_ns, "expires_ns")
        now = self._now() if now_ns is None else _integer(now_ns, "now_ns")
        if expiration <= now:
            raise TargetLockRefused("lease must expire strictly after now")
        record = self._record(handle)  # no global mutex around external registry callbacks
        resource = str.__str__(self._resource(handle, record))
        slot = (id(self.registry), resource)
        while True:
            # Constructors and entropy generation are outside the metadata
            # lock too; a rare nonce collision simply retries the mint.
            nonce = secrets.token_bytes(24)
            if type(nonce) is not bytes:
                raise TargetLockRefused("missing or invalid issued nonce")
            lease = ActorLease(handle, owner, handle.generation, nonce, expiration)
            claim = _Claim(self.registry, resource, handle, lease,
                           _identity(lease.handle), nonce, lease.owner,
                           lease.generation, expiration)
            with _SLOT_GUARD:  # metadata only, never provider code or transport
                if slot in _SLOTS:
                    raise TargetBusy("target %r already owned or quarantined" % resource)
                if nonce in _NONCES:
                    continue
                _SLOTS[slot] = claim
                _NONCES[nonce] = claim
                _LEASES[id(lease)] = claim
                return lease

    def _minted_claim(self, lease):
        """Under _SLOT_GUARD, validate minted identity without authorizing time."""
        # Index by object identity before reading any attribute.  An arbitrary
        # object with a nonce property must not execute code under this lock.
        claim = _LEASES.get(id(lease))
        if (claim is None or lease is not claim.lease
                or type(lease) is not ActorLease
                or claim.registry is not self.registry):
            raise TargetLockRefused("forged, consumed or foreign lease")
        nonce = getattr(lease, "nonce", None)
        if type(nonce) is not bytes:
            raise TargetLockRefused("missing or invalid issued nonce")
        handle = getattr(lease, "handle", None)
        if (type(handle) is not Handle
                or type(getattr(handle, "kind", None)) is not str
                or type(getattr(handle, "canonical_id", None)) is not str
                or type(getattr(handle, "generation", None)) is not int
                or type(getattr(lease, "owner", None)) is not str
                or type(getattr(lease, "generation", None)) is not int
                or type(getattr(lease, "expires_ns", None)) is not int):
            raise TargetLockRefused("forged, consumed or foreign lease")
        if (_NONCES.get(nonce) is not claim
                or _SLOTS.get((id(claim.registry), claim.resource)) is not claim
                or nonce != claim.nonce
                or lease.owner != claim.owner
                or lease.generation != claim.generation
                or lease.expires_ns != claim.expires_ns
                or (handle.kind, handle.canonical_id, handle.generation) != claim.identity):
            raise TargetLockRefused("forged, consumed or foreign lease")
        return claim

    def _live_claim(self, lease, now_ns):
        """Called only under _SLOT_GUARD; validate identity and live expiration."""
        claim = self._minted_claim(lease)
        if now_ns >= claim.expires_ns:
            raise TargetLockRefused("expired lease is fenced, not released")
        return claim

    def owns(self, lease, *, now_ns) -> bool:
        """Validate a genuine, current nonce without releasing or renewing it.

        Registry and alias providers run outside the metadata lock.  Rechecking
        the claim afterwards closes a release/reacquire or quarantine race
        during those callbacks.  Inflight ownership is retained until positive
        quiescence, but expired or quarantined claims never authorize a send.
        """
        try:
            now = _integer(now_ns, "now_ns")
            with _SLOT_GUARD:
                claim = self._live_claim(lease, now)
                if claim.quarantined:
                    return False
                identity = claim.identity
                resource = claim.resource
            record = self._record(lease.handle)
            if (_identity(record.handle) != identity
                    or self._resource(lease.handle, record) != resource):
                return False
            with _SLOT_GUARD:
                current = self._live_claim(lease, now)
                return current is claim and not current.quarantined
        except (ValueError, LookupError, TypeError, AttributeError):
            return False

    @staticmethod
    def _discard(claim):
        _SLOTS.pop((id(claim.registry), claim.resource))
        _NONCES.pop(claim.nonce)
        _LEASES.pop(id(claim.lease))

    def release(self, lease):
        """Only a *live unsent* lease can be released with its issued nonce."""
        now = self._now()
        with _SLOT_GUARD:
            claim = self._live_claim(lease, now)
            if claim.inflight or claim.quarantined:
                raise TargetLockRefused("sent or ambiguous action requires acknowledged quiescence")
            self._discard(claim)

    def mark_inflight(self, lease):
        """Fence a possible dispatch before sending; no automatic expiry release."""
        now = self._now()
        with _SLOT_GUARD:
            claim = self._live_claim(lease, now)
            if claim.inflight or claim.quarantined:
                raise TargetLockRefused("lease already marked as sent")
            claim.inflight = True

    def reconcile_quarantine(self, handle, *, acknowledged: bool, expected_lease=None):
        """Only positive backend evidence of quiescence clears a sent claim.

        Adapters retaining custody must pass ``expected_lease`` to bind evidence
        to that exact mint, preventing stale acknowledgments from clearing a
        later lease on the same handle.  Identity-bound reconciliation permits
        expired or quarantined claims; it does not authorize a new send.
        """
        kind, canonical_id, generation = _identity(handle)
        identity = (str.__str__(str(kind)), str.__str__(canonical_id), generation)
        if type(acknowledged) is not bool:
            raise TypeError("acknowledged must be bool")
        with _SLOT_GUARD:
            candidates = [c for c in _SLOTS.values()
                          if c.registry is self.registry and c.identity == identity]
            if len(candidates) != 1:
                raise TargetLockRefused("no exact-generation claim for acknowledgment")
            claim = candidates[0]
            if expected_lease is not None and self._minted_claim(expected_lease) is not claim:
                raise TargetLockRefused("forged, consumed or foreign lease")
            if not claim.inflight:
                raise TargetLockRefused("unsent lease must use nonce-fenced release")
            if acknowledged:
                self._discard(claim)
            else:
                claim.quarantined = True
