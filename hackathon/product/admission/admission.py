"""Multi-actor lease and region-scoped admission controller (reference).

Stdlib-only reference implementation of ``product/admission/SPEC.md``.
It extends ViewPoint's ``observe -> propose -> wait-verify`` pipeline
(``swarm/CONTRACT.md`` sections 3, 4 and 6) with:

* multi-actor identity (``actor_id``) and epoch-versioned leases;
* a bounded ring of immutable frame observations;
* region-scoped rebase: a proposal based on an older frame is admitted
  (``ADMITTED_REBASED``) only when the view and scene epochs are unchanged
  and every change published after its base frame is attributed to known
  regions that stay clear of the proposal's target region and footprint;
* one exclusive actuator, so at most one dispatch is ever in flight;
* typed verification receipts that never turn ambiguous evidence into
  success.

All state lives behind a single lock: ``propose`` and ``wait_verify`` are
linearizable, which is what the multi-peer tests in ``test_admission.py``
rely on. The controller never sleeps and never reads a clock: every call
takes ``now_ms`` from the caller, so tests are deterministic.
"""

from __future__ import annotations

import collections
import enum
import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence, Tuple

__all__ = [
    "Code",
    "Rect",
    "Lease",
    "FrameObservation",
    "Proposal",
    "PostCapture",
    "ExecutorOutcome",
    "AdmissionDecision",
    "VerificationReceipt",
    "AdmissionController",
    "DISPLAY_W",
    "DISPLAY_H",
    "GUI_TURN_CAP_MS",
    "SCRIPT_TURN_CAP_MS",
    "ALLOWED_GUI_KEYS",
]

DISPLAY_W = 1920
DISPLAY_H = 1080
GUI_TURN_CAP_MS = 60_000
SCRIPT_TURN_CAP_MS = 120_000
DEFAULT_RING_SIZE = 16
DEFAULT_MARGIN_PX = 8
STALE_DROP_LIMIT = 3
CONSECUTIVE_TIMEOUT_PARK = 2
MAX_GUI_ACTS = 8
ALLOWED_GUI_KEYS = frozenset({"KP_1", "KP_3", "KP_7", "KP_4", "KP_Add", "Home"})

Rect = Tuple[int, int, int, int]  # (x, y, w, h), w > 0 and h > 0


class Code(str, enum.Enum):
    """Every admission decision and verification verdict."""

    # admission (propose)
    ADMITTED = "ADMITTED"
    ADMITTED_REBASED = "ADMITTED_REBASED"
    # outcomes, GUI lane
    VERIFIED = "VERIFIED"
    VERIFIED_WITH_SPILL = "VERIFIED_WITH_SPILL"
    NO_VISIBLE_CHANGE = "NO_VISIBLE_CHANGE"
    INPUT_UNCERTAIN = "INPUT_UNCERTAIN"
    # outcomes, script lane
    STATE_VERIFIED = "STATE_VERIFIED"
    JOB_ERROR = "JOB_ERROR"
    OUTCOME_UNCERTAIN = "OUTCOME_UNCERTAIN"
    # freshness refusals
    STALE_FRAME = "STALE_FRAME"
    STALE_REGION = "STALE_REGION"
    STALE_VIEW = "STALE_VIEW"
    STALE_SCENE = "STALE_SCENE"
    FRAME_EVICTED = "FRAME_EVICTED"
    # authority / validation / lock refusals
    NO_LEASE = "NO_LEASE"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    ACTUATOR_BUSY = "ACTUATOR_BUSY"
    FOOTPRINT_MISSING = "FOOTPRINT_MISSING"
    CLAIM_REFUSED = "CLAIM_REFUSED"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    # timeout
    TURN_TIMEOUT = "TURN_TIMEOUT"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


ADMIT_CODES = frozenset({Code.ADMITTED, Code.ADMITTED_REBASED})
SUCCESS_VERDICTS = frozenset(
    {Code.VERIFIED, Code.VERIFIED_WITH_SPILL, Code.STATE_VERIFIED}
)
UNCERTAIN_VERDICTS = frozenset(
    {Code.INPUT_UNCERTAIN, Code.OUTCOME_UNCERTAIN, Code.TURN_TIMEOUT}
)


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def valid_rect(r: object) -> bool:
    """True for an on-display ``(x, y, w, h)`` of ints with positive size."""
    if not isinstance(r, (tuple, list)) or len(r) != 4:
        return False
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in r):
        return False
    x, y, w, h = r
    return (
        w > 0 and h > 0 and x >= 0 and y >= 0
        and x + w <= DISPLAY_W and y + h <= DISPLAY_H
    )


def _norm(r: Sequence[int]) -> Rect:
    return (int(r[0]), int(r[1]), int(r[2]), int(r[3]))


def inflate(r: Rect, margin: int) -> Rect:
    """Grow ``r`` by ``margin`` px on every side (not clipped: conservative)."""
    x, y, w, h = r
    return (x - margin, y - margin, w + 2 * margin, h + 2 * margin)


def intersects(a: Rect, b: Rect) -> bool:
    """Open-interior overlap test on half-open boxes [x, x+w) x [y, y+h)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def contains(outer: Rect, inner: Rect) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ix + iw <= ox + ow and iy + ih <= oy + oh


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Lease:
    lease_id: str
    actor_id: str
    lease_epoch: int
    ttl_ms: int
    issued_ts_ms: int
    expires_ts_ms: int
    allow_gui: bool
    write_prefixes: Tuple[str, ...]
    allowed_regions: Tuple[Rect, ...]


@dataclass(frozen=True)
class FrameObservation:
    """Immutable published frame.

    ``changed_regions`` lists the regions that changed relative to the
    previous frame. ``None`` means the extent of the change is unknown
    (unattributed foreign change, reconcile capture, surface restart):
    such a frame can never be rebased across.
    """

    frame_seq: int
    view_epoch: int
    scene_epoch: int
    changed_regions: Optional[Tuple[Rect, ...]]
    sha256: str
    ts_ms: int
    attributed_to: Optional[str] = None


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    actor_id: str
    lane: str  # "gui" | "script"
    lease_id: str
    lease_epoch: int
    based_on_frame: int
    view_epoch: int
    scene_epoch: int
    based_on_sha256: Optional[str] = None
    target_region: Optional[Sequence[int]] = None
    footprint: Optional[Sequence[int]] = None
    write_set: Tuple[str, ...] = ()
    keys: Optional[Tuple[str, ...]] = None
    allow_rebase: bool = True
    # gui: "view_change" (viewport navigation, bumps view_epoch when verified)
    #      or "input" (region-local input, no epoch bump); script: "state_job".
    # Empty selects the lane default ("view_change" / "state_job").
    action_class: str = ""


@dataclass(frozen=True)
class PostCapture:
    """What the capture after the dispatch saw (input to wait_verify)."""

    changed_regions: Optional[Tuple[Rect, ...]]
    content_sha256: Optional[str] = None


@dataclass(frozen=True)
class ExecutorOutcome:
    """Raw executor evidence.

    GUI lane: ``rc`` of the actuator (0 changed, 3 no visible change).
    Script lane: bridge receipt ``status`` ("OK" | "ERROR") correlated by
    ``event_id``. ``elapsed_ms`` is the executor-measured duration.
    """

    rc: Optional[int] = None
    status: Optional[str] = None
    event_id: Optional[str] = None
    elapsed_ms: Optional[int] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class AdmissionDecision:
    code: Code
    proposal_id: str
    actor_id: str
    head_frame: int
    dispatch_token: Optional[str] = None
    rebased: bool = False
    drops: int = 0
    parked: bool = False
    reason: str = ""

    @property
    def admitted(self) -> bool:
        return self.code in ADMIT_CODES


@dataclass(frozen=True)
class VerificationReceipt:
    verdict: Code
    proposal_id: Optional[str]
    actor_id: Optional[str]
    dispatch_token: str
    dispatch_capture_seq: Optional[int]
    verify_frame_ref: Optional[int]
    rebased: bool = False
    own_regions: Tuple[Rect, ...] = ()
    spill_regions: Tuple[Rect, ...] = ()
    view_epoch: int = 0
    scene_epoch: int = 0
    reason: str = ""


@dataclass
class _Dispatch:
    token: str
    proposal: Proposal
    lease_epoch: int
    dispatch_capture_seq: int
    dispatched_ms: int
    deadline_ms: int
    rebased: bool


# --------------------------------------------------------------------------
# controller
# --------------------------------------------------------------------------

class AdmissionController:
    """Single authority for leases, frames and the exclusive actuator."""

    def __init__(
        self,
        *,
        now_ms: int = 0,
        ring_size: int = DEFAULT_RING_SIZE,
        margin_px: int = DEFAULT_MARGIN_PX,
        gui_cap_ms: int = GUI_TURN_CAP_MS,
        script_cap_ms: int = SCRIPT_TURN_CAP_MS,
    ) -> None:
        if ring_size < 2:
            raise ValueError("ring_size must be >= 2")
        if margin_px < 0:
            raise ValueError("margin_px must be >= 0")
        self._lock = threading.Lock()
        self._ring_size = ring_size
        self._margin = margin_px
        self._gui_cap = gui_cap_ms
        self._script_cap = script_cap_ms
        self._frames: "collections.OrderedDict[int, FrameObservation]" = (
            collections.OrderedDict()
        )
        self._head = 0
        self._view_epoch = 0
        self._scene_epoch = 0
        self._leases: dict[str, Lease] = {}          # actor -> current lease
        self._lease_state: dict[str, str] = {}       # lease_id -> active|revoked|expired|superseded
        self._lease_epochs: dict[str, int] = {}      # actor -> last epoch issued
        self._lease_owner: dict[str, str] = {}       # lease_id -> actor
        self._lease_seq = 0
        self._consumed: set[str] = set()             # proposal ids dispatched or parked
        self._drops: dict[str, int] = {}
        self._inflight: Optional[_Dispatch] = None
        self._closed: dict[str, VerificationReceipt] = {}
        self._reconcile = False
        self._consecutive_timeouts: dict[str, int] = {}
        self._last_observed: dict[str, int] = {}
        self._started = False
        self._publish(now_ms, changed_regions=None, attributed_to=None)

    # -- frames -----------------------------------------------------------

    def _publish(
        self,
        now_ms: int,
        changed_regions: Optional[Iterable[Sequence[int]]],
        attributed_to: Optional[str],
        content_sha256: Optional[str] = None,
    ) -> FrameObservation:
        seq = self._head + 1 if self._started else 0
        self._started = True
        regions = (
            None if changed_regions is None
            else tuple(_norm(r) for r in changed_regions)
        )
        body = json.dumps(
            [seq, self._view_epoch, self._scene_epoch,
             None if regions is None else [list(r) for r in regions],
             content_sha256, attributed_to],
            separators=(",", ":"),
        ).encode()
        frame = FrameObservation(
            frame_seq=seq,
            view_epoch=self._view_epoch,
            scene_epoch=self._scene_epoch,
            changed_regions=regions,
            sha256=hashlib.sha256(body).hexdigest(),
            ts_ms=now_ms,
            attributed_to=attributed_to,
        )
        self._frames[seq] = frame
        self._head = seq
        while len(self._frames) > self._ring_size:
            self._frames.popitem(last=False)
        return frame

    def capture(
        self,
        now_ms: int,
        changed_regions: Optional[Iterable[Sequence[int]]],
        *,
        view_changed: bool = False,
        scene_changed: bool = False,
        attributed_to: Optional[str] = None,
        content_sha256: Optional[str] = None,
    ) -> FrameObservation:
        """Publish a frame for a change that did not go through admission.

        ``changed_regions=None`` models a foreign change of unknown extent.
        """
        with self._lock:
            if changed_regions is not None:
                for r in changed_regions:
                    if not valid_rect(r):
                        raise ValueError(f"invalid changed region {r!r}")
            if view_changed:
                self._view_epoch += 1
            if scene_changed:
                self._scene_epoch += 1
            return self._publish(now_ms, changed_regions, attributed_to,
                                 content_sha256)

    def surface_restart(self, now_ms: int) -> FrameObservation:
        """New surface incarnation: evicts every frame, bumps both epochs."""
        with self._lock:
            self._frames.clear()
            self._view_epoch += 1
            self._scene_epoch += 1
            frame = self._publish(now_ms, None, None)
            return frame

    def observe(self, actor_id: str, now_ms: int) -> FrameObservation:
        """Return the latest immutable frame (read-only; no lease needed)."""
        with self._lock:
            frame = self._frames[self._head]
            self._last_observed[actor_id] = frame.frame_seq
            return frame

    @property
    def head_frame(self) -> int:
        with self._lock:
            return self._head

    @property
    def epochs(self) -> Tuple[int, int]:
        with self._lock:
            return self._view_epoch, self._scene_epoch

    @property
    def actuator_busy(self) -> bool:
        with self._lock:
            return self._inflight is not None or self._reconcile

    @property
    def in_reconcile(self) -> bool:
        with self._lock:
            return self._reconcile

    def frame(self, seq: int) -> Optional[FrameObservation]:
        with self._lock:
            return self._frames.get(seq)

    # -- leases -----------------------------------------------------------

    def issue_lease(
        self,
        actor_id: str,
        now_ms: int,
        *,
        ttl_ms: int,
        allow_gui: bool = False,
        write_prefixes: Iterable[str] = (),
        allowed_regions: Optional[Iterable[Sequence[int]]] = None,
    ) -> Lease:
        """Grant (or renew) ``actor_id``'s lease with a strictly higher epoch."""
        if not isinstance(actor_id, str) or not actor_id:
            raise ValueError("actor_id must be a non-empty string")
        if not isinstance(ttl_ms, int) or ttl_ms <= 0:
            raise ValueError("ttl_ms must be a positive int")
        prefixes = tuple(write_prefixes)
        if any(not isinstance(p, str) or not p for p in prefixes):
            raise ValueError("write_prefixes must be non-empty strings")
        if allowed_regions is None:
            regions: Tuple[Rect, ...] = ((0, 0, DISPLAY_W, DISPLAY_H),)
        else:
            regions = tuple(_norm(r) for r in allowed_regions)
            if any(not valid_rect(r) for r in regions):
                raise ValueError("allowed_regions must be valid on-display rects")
        with self._lock:
            prev = self._leases.get(actor_id)
            if prev is not None and self._lease_state.get(prev.lease_id) == "active":
                self._lease_state[prev.lease_id] = "superseded"
            epoch = self._lease_epochs.get(actor_id, 0) + 1
            self._lease_epochs[actor_id] = epoch
            self._lease_seq += 1
            lease = Lease(
                lease_id=f"lease-{actor_id}-{self._lease_seq:04d}",
                actor_id=actor_id,
                lease_epoch=epoch,
                ttl_ms=ttl_ms,
                issued_ts_ms=now_ms,
                expires_ts_ms=now_ms + ttl_ms,
                allow_gui=bool(allow_gui),
                write_prefixes=prefixes,
                allowed_regions=regions,
            )
            self._leases[actor_id] = lease
            self._lease_state[lease.lease_id] = "active"
            self._lease_owner[lease.lease_id] = actor_id
            self._consecutive_timeouts[actor_id] = 0
            return lease

    def revoke_lease(self, actor_id: str) -> bool:
        with self._lock:
            return self._revoke_locked(actor_id)

    def _revoke_locked(self, actor_id: str) -> bool:
        lease = self._leases.pop(actor_id, None)
        if lease is None:
            return False
        self._lease_state[lease.lease_id] = "revoked"
        return True

    def expire_leases(self, now_ms: int) -> Tuple[str, ...]:
        """Mark every lease with ``now_ms >= expires_ts_ms`` expired."""
        expired = []
        with self._lock:
            for actor_id, lease in list(self._leases.items()):
                if now_ms >= lease.expires_ts_ms:
                    self._lease_state[lease.lease_id] = "expired"
                    expired.append(lease.lease_id)
        return tuple(expired)

    def lease_of(self, actor_id: str) -> Optional[Lease]:
        with self._lock:
            return self._leases.get(actor_id)

    def _check_lease(self, p: Proposal, now_ms: int) -> Tuple[Optional[Code], str, Optional[Lease]]:
        lease = self._leases.get(p.actor_id)
        state = self._lease_state.get(p.lease_id)
        if state is not None and self._lease_owner.get(p.lease_id) != p.actor_id:
            return Code.NO_LEASE, f"lease {p.lease_id} belongs to another actor", None
        if lease is None:
            if state in ("expired", "superseded"):
                return Code.LEASE_EXPIRED, f"lease {p.lease_id} is {state}", None
            return Code.NO_LEASE, f"actor {p.actor_id} holds no lease", None
        if p.lease_id != lease.lease_id:
            if state is None:
                return Code.NO_LEASE, f"unknown lease_id {p.lease_id!r}", None
            if state == "revoked":
                return Code.NO_LEASE, f"lease {p.lease_id} was revoked", None
            return Code.LEASE_EXPIRED, f"lease {p.lease_id} is {state}", None
        if state == "revoked":  # pragma: no cover - revoked leases are popped
            return Code.NO_LEASE, "lease revoked", None
        if p.lease_epoch != lease.lease_epoch:
            return (Code.LEASE_EXPIRED,
                    f"lease_epoch {p.lease_epoch} != active {lease.lease_epoch}", None)
        if state == "expired" or now_ms >= lease.expires_ts_ms:
            self._lease_state[lease.lease_id] = "expired"
            return Code.LEASE_EXPIRED, f"lease expired at {lease.expires_ts_ms}", None
        return None, "", lease

    # -- admission --------------------------------------------------------

    def propose(self, proposal: Proposal, now_ms: int) -> AdmissionDecision:
        """Atomically admit or refuse one proposal (see SPEC.md section 5)."""
        with self._lock:
            return self._propose_locked(proposal, now_ms)

    def _refuse(self, p: Proposal, code: Code, reason: str, *,
                drops: int = 0, parked: bool = False) -> AdmissionDecision:
        return AdmissionDecision(code=code, proposal_id=p.proposal_id,
                                 actor_id=p.actor_id, head_frame=self._head,
                                 drops=drops, parked=parked, reason=reason)

    def _propose_locked(self, p: Proposal, now_ms: int) -> AdmissionDecision:
        # 1. idempotency: a dispatched or parked id never reaches the actuator again
        if not isinstance(p.proposal_id, str) or not p.proposal_id:
            return self._refuse(p, Code.CLAIM_REFUSED, "proposal_id must be a non-empty string")
        if p.proposal_id in self._consumed:
            return self._refuse(p, Code.DUPLICATE_EVENT,
                                f"proposal_id {p.proposal_id!r} already dispatched or parked")
        # 2. authority
        code, reason, lease = self._check_lease(p, now_ms)
        if code is not None:
            return self._refuse(p, code, reason)
        assert lease is not None
        # 3. claim validation
        code, reason = self._check_claim(p, lease)
        if code is not None:
            return self._refuse(p, code, reason)
        # 4. exclusive actuator
        if self._reconcile:
            return self._refuse(p, Code.ACTUATOR_BUSY, "controller is in RECONCILE")
        if self._inflight is not None:
            return self._refuse(p, Code.ACTUATOR_BUSY,
                                f"dispatch {self._inflight.proposal.proposal_id} in flight")
        # 5. freshness (GUI lane only; script lane is guarded by disjoint claims)
        rebased = False
        if p.lane == "gui":
            code, reason, rebased = self._check_freshness(p)
            if code is not None:
                if code == Code.STALE_FRAME:
                    drops = self._drops.get(p.proposal_id, 0) + 1
                    self._drops[p.proposal_id] = drops
                    parked = drops >= STALE_DROP_LIMIT
                    if parked:
                        self._consumed.add(p.proposal_id)
                    return self._refuse(p, code, reason, drops=drops, parked=parked)
                return self._refuse(p, code, reason)
        # 6. admit and take the actuator
        token = secrets.token_hex(16)
        cap = self._gui_cap if p.lane == "gui" else self._script_cap
        self._inflight = _Dispatch(
            token=token, proposal=p, lease_epoch=lease.lease_epoch,
            dispatch_capture_seq=self._head, dispatched_ms=now_ms,
            deadline_ms=now_ms + cap, rebased=rebased,
        )
        self._consumed.add(p.proposal_id)
        return AdmissionDecision(
            code=Code.ADMITTED_REBASED if rebased else Code.ADMITTED,
            proposal_id=p.proposal_id, actor_id=p.actor_id,
            head_frame=self._head, dispatch_token=token, rebased=rebased,
            drops=self._drops.get(p.proposal_id, 0),
            reason=("rebased from frame %d to %d" % (p.based_on_frame, self._head))
            if rebased else "",
        )

    def _check_claim(self, p: Proposal, lease: Lease) -> Tuple[Optional[Code], str]:
        if p.lane == "script":
            if p.action_class not in ("", "state_job"):
                return Code.CLAIM_REFUSED, f"action_class {p.action_class!r} invalid for script lane"
            ws = tuple(p.write_set or ())
            if not ws:
                return Code.CLAIM_REFUSED, "script lane requires a non-empty write_set"
            for w in ws:
                if not isinstance(w, str) or not any(w.startswith(pre) for pre in lease.write_prefixes):
                    return Code.CLAIM_REFUSED, f"write_set entry {w!r} outside write_prefixes"
            return None, ""
        if p.lane != "gui":
            return Code.CLAIM_REFUSED, f"unknown lane {p.lane!r}"
        if not lease.allow_gui:
            return Code.CLAIM_REFUSED, "lease does not allow the GUI lane"
        if p.action_class not in ("", "view_change", "input"):
            return Code.CLAIM_REFUSED, f"action_class {p.action_class!r} invalid for GUI lane"
        if p.footprint is None or not valid_rect(p.footprint):
            return Code.FOOTPRINT_MISSING, f"invalid or missing footprint {p.footprint!r}"
        if p.target_region is not None and not valid_rect(p.target_region):
            return Code.CLAIM_REFUSED, f"invalid target_region {p.target_region!r}"
        keys = p.keys
        if keys is not None:
            if not (1 <= len(keys) <= MAX_GUI_ACTS):
                return Code.CLAIM_REFUSED, "keys must hold 1..8 acts"
            bad = [k for k in keys if k not in ALLOWED_GUI_KEYS]
            if bad:
                return Code.CLAIM_REFUSED, f"disallowed keys {bad!r}"
        for name, rect in (("footprint", p.footprint), ("target_region", p.target_region)):
            if rect is None:
                continue
            r = _norm(rect)
            if not any(contains(a, r) for a in lease.allowed_regions):
                return Code.CLAIM_REFUSED, f"{name} {r} outside allowed_regions"
        return None, ""

    def _check_freshness(self, p: Proposal) -> Tuple[Optional[Code], str, bool]:
        b = p.based_on_frame
        if not isinstance(b, int) or isinstance(b, bool) or b > self._head or b < 0:
            return Code.STALE_FRAME, f"based_on_frame {b!r} was never published", False
        base = self._frames.get(b)
        if base is None:
            return Code.FRAME_EVICTED, f"frame {b} evicted from ring", False
        if p.based_on_sha256 is not None and p.based_on_sha256 != base.sha256:
            return Code.STALE_FRAME, f"sha256 does not bind frame {b}", False
        if p.view_epoch != base.view_epoch or p.scene_epoch != base.scene_epoch:
            return Code.STALE_FRAME, f"declared epochs do not match frame {b}", False
        head = self._frames[self._head]
        if b == self._head:
            return None, "", False
        # b < head: region-scoped rebase decision (SPEC.md invariant R)
        if base.view_epoch != head.view_epoch:
            return Code.STALE_VIEW, (
                f"view_epoch {base.view_epoch} -> {head.view_epoch} since frame {b}"), False
        if base.scene_epoch != head.scene_epoch:
            return Code.STALE_SCENE, (
                f"scene_epoch {base.scene_epoch} -> {head.scene_epoch} since frame {b}"), False
        if not p.allow_rebase:
            return Code.STALE_FRAME, f"frame {b} < head {self._head}, rebase not permitted", False
        if p.target_region is None:
            return Code.STALE_FRAME, f"frame {b} < head {self._head}, no target_region", False
        guards = (inflate(_norm(p.target_region), self._margin),
                  inflate(_norm(p.footprint), self._margin))  # type: ignore[arg-type]
        for seq in range(b + 1, self._head + 1):
            f = self._frames[seq]
            if f.changed_regions is None:
                return Code.STALE_FRAME, f"frame {seq} carries an unattributed change", False
            for r in f.changed_regions:
                if any(intersects(g, r) for g in guards):
                    return Code.STALE_REGION, (
                        f"frame {seq} changed {r} within margin of the proposal"), False
        return None, "", True

    # -- verification -----------------------------------------------------

    def wait_verify(
        self,
        dispatch_token: str,
        post_frame_obs: Optional[PostCapture],
        executor_outcome: Optional[ExecutorOutcome],
        now_ms: int,
    ) -> VerificationReceipt:
        """Close the in-flight dispatch with a typed verdict (SPEC.md section 6)."""
        with self._lock:
            closed = self._closed.get(dispatch_token)
            if closed is not None:
                return VerificationReceipt(
                    verdict=Code.DUPLICATE_EVENT, proposal_id=closed.proposal_id,
                    actor_id=closed.actor_id, dispatch_token=dispatch_token,
                    dispatch_capture_seq=closed.dispatch_capture_seq,
                    verify_frame_ref=closed.verify_frame_ref,
                    view_epoch=self._view_epoch, scene_epoch=self._scene_epoch,
                    reason=f"dispatch already closed as {closed.verdict.value}")
            d = self._inflight
            if d is None or not isinstance(dispatch_token, str) or not secrets.compare_digest(
                    d.token.encode(), dispatch_token.encode()):
                return VerificationReceipt(
                    verdict=Code.CLAIM_REFUSED, proposal_id=None, actor_id=None,
                    dispatch_token=str(dispatch_token), dispatch_capture_seq=None,
                    verify_frame_ref=None, view_epoch=self._view_epoch,
                    scene_epoch=self._scene_epoch,
                    reason="dispatch_token does not name the in-flight dispatch")
            verdict, reason, own, spill = self._classify(d, post_frame_obs,
                                                         executor_outcome, now_ms)
            actor = d.proposal.actor_id
            verify_ref: Optional[int] = None
            if verdict in UNCERTAIN_VERDICTS:
                self._reconcile = True
            else:
                if (verdict in (Code.VERIFIED, Code.VERIFIED_WITH_SPILL)
                        and d.proposal.action_class != "input"):
                    self._view_epoch += 1
                elif verdict == Code.STATE_VERIFIED:
                    self._scene_epoch += 1
                assert post_frame_obs is not None
                regions = post_frame_obs.changed_regions
                if verdict == Code.JOB_ERROR:
                    regions = None  # failed job: extent of any change unknown
                frame = self._publish(now_ms, regions, actor,
                                      post_frame_obs.content_sha256)
                verify_ref = frame.frame_seq
            if verdict == Code.TURN_TIMEOUT:
                n = self._consecutive_timeouts.get(actor, 0) + 1
                self._consecutive_timeouts[actor] = n
                if n >= CONSECUTIVE_TIMEOUT_PARK:
                    self._revoke_locked(actor)
                    reason += f"; {n} consecutive timeouts: actor parked (lease revoked)"
            else:
                self._consecutive_timeouts[actor] = 0
            receipt = VerificationReceipt(
                verdict=verdict, proposal_id=d.proposal.proposal_id, actor_id=actor,
                dispatch_token=d.token, dispatch_capture_seq=d.dispatch_capture_seq,
                verify_frame_ref=verify_ref, rebased=d.rebased,
                own_regions=own, spill_regions=spill,
                view_epoch=self._view_epoch, scene_epoch=self._scene_epoch,
                reason=reason)
            self._closed[d.token] = receipt
            self._inflight = None
            return receipt

    def _classify(self, d: _Dispatch, post: Optional[PostCapture],
                  out: Optional[ExecutorOutcome], now_ms: int
                  ) -> Tuple[Code, str, Tuple[Rect, ...], Tuple[Rect, ...]]:
        p = d.proposal
        cap = d.deadline_ms - d.dispatched_ms
        if now_ms > d.deadline_ms or (
                out is not None and isinstance(out.elapsed_ms, int) and out.elapsed_ms > cap):
            return Code.TURN_TIMEOUT, f"exceeded {cap} ms turn cap", (), ()
        if p.lane == "script":
            if out is None or out.event_id != p.proposal_id:
                return Code.OUTCOME_UNCERTAIN, "missing or uncorrelated bridge receipt", (), ()
            if post is None:
                return Code.OUTCOME_UNCERTAIN, "no post-dispatch capture", (), ()
            if out.status == "OK" and out.error is None:
                return Code.STATE_VERIFIED, "", (), ()
            if out.status == "ERROR":
                return Code.JOB_ERROR, out.error or "bridge reported ERROR", (), ()
            return Code.OUTCOME_UNCERTAIN, f"malformed receipt status {out.status!r}", (), ()
        # GUI lane
        if out is None or not isinstance(out.rc, int) or isinstance(out.rc, bool):
            return Code.INPUT_UNCERTAIN, "missing actuator exit code", (), ()
        if post is None or post.changed_regions is None:
            return Code.INPUT_UNCERTAIN, "post-dispatch change extent unknown", (), ()
        regions = tuple(_norm(r) for r in post.changed_regions)
        if any(not valid_rect(r) for r in regions):
            return Code.INPUT_UNCERTAIN, "post-dispatch capture has invalid regions", (), ()
        if out.rc == 3:
            if regions:
                return Code.INPUT_UNCERTAIN, "rc 3 contradicted by visible change", (), ()
            return Code.NO_VISIBLE_CHANGE, "", (), ()
        if out.rc != 0:
            return Code.INPUT_UNCERTAIN, f"ambiguous actuator rc {out.rc}", (), ()
        if not regions:
            return Code.INPUT_UNCERTAIN, "rc 0 contradicted by zero change", (), ()
        fp = _norm(p.footprint)  # type: ignore[arg-type]
        own = tuple(r for r in regions if contains(fp, r))
        spill = tuple(r for r in regions if not contains(fp, r))
        if spill:
            return Code.VERIFIED_WITH_SPILL, f"{len(spill)} region(s) outside footprint", own, spill
        return Code.VERIFIED, "", own, ()

    def resolve_reconcile(self, now_ms: int) -> FrameObservation:
        """Leave RECONCILE with a full re-observation: both epochs advance and
        the published frame has unknown extent, so nothing rebases across it."""
        with self._lock:
            if not self._reconcile:
                raise RuntimeError("controller is not in RECONCILE")
            self._view_epoch += 1
            self._scene_epoch += 1
            self._reconcile = False
            return self._publish(now_ms, None, None)
