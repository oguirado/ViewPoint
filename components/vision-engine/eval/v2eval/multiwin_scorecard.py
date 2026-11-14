"""Offline multi-window vision scorecard evaluator and deterministic canonical serializer.

Pools per-window integer confusion counts explicitly without abusing sealed
duplicate-scene scoring. Reports frame support, drops, exactness, per-window
isolated operation totals, seals, and deterministic canonical bytes while
excluding diagnostic wall-clock timing and host-specific fields.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from fractions import Fraction
import hashlib
import hmac
import json
import math
from typing import Any

import numpy as np

from eval.gui_synth.score import ScoreError, aggregate as _sealed_aggregate, score as _sealed_score
from eval.honesty.metrics import CLASSES, class_honesty, silent_miss_ppm

AUSENTE = "AUSENTE"

SUPPORTED_TARGET_KINDS = (
    "cdp_tab",
    "x11_window",
    "xvfb_screen",
    "capsule",
    "godot_window",
)

MANDATORY_METRIC_KEYS = (
    "cross_window_bleed",
    "safety_violations",
    "frame_exactness",
    "frame_drops",
    "operation_totals",
    "eligibility_stalls",
    "starvation",
    "terminal_completeness",
    "pooled_confusion",
)

METADATA_LOCK_ORDER = (
    "scheduler_or_fence",
    "registry",
    "budget",
    "target_actor_metadata",
    "focus_domain",
    "stream_bank",
    "per_stream_metadata",
)

PREREQUISITE_STAGE_ORDER = (
    "identity_and_streams",
    "ownership_and_perception",
    "readiness",
    "scheduling_and_godot",
    "scorecard",
)

REQUIRED_GATE_VECTOR = {
    "G_M1_1": "Stale handles never resolve",
    "G_M1_2": "Ambiguity refuses without CDP; miss creates scoped tab",
    "G_M1_3": "Navigate preserves sibling events ordered",
    "G_M1_4": "Expired reply discarded; crash fences only its lane",
    "G_M1_5": "Ownership and STOP survive teardown",
    "G_M2_1": "Same resource excludes; different targets proceed",
    "G_M2_2": "Modal or wrong-target focus refuses before send",
    "G_M2_3": "Unreadable fresh gate refuses before send",
    "G_M2_4": "Keyboard/pointer require matching geometry and focus",
    "G_M2_5": "Expiry/replayed release never unlocks active command",
    "G_M3_1": "Batch identities and serial bytes agree",
    "G_M3_2": "Fusion never attaches sibling AX evidence",
    "G_M3_3": "Item failure isolated; runner reused",
    "G_M3_4": "DPR and boxes remain target-specific",
    "G_M3_5": "Independent predecessor histories match serial runs",
    "G_M4_1": "Rings and geometry epochs never alias",
    "G_M4_2": "Crop bounds and stable sequence validated",
    "G_M4_3": "Quota rollback conserves live reservations",
    "G_M4_4": "Torn or overwritten frames never delivered",
    "G_M4_5": "Retained frame remains exact after wrap/resize",
    "G_M5_1": "Interleaved histories equal solo histories",
    "G_M5_2": "Per-target FL ops equal solo totals",
    "G_M5_3": "Blink veto survives sibling interleaving",
    "G_M5_4": "Motion/chroma/eager hashes preserve real change",
    "G_M5_5": "Settle fails after gap, motion or stale frame",
    "G_M6_1": "Contended claims dispatch only once",
    "G_M6_2": "Continuously eligible target meets declared fair bound",
    "G_M6_3": "Deadline never restarts; uncertain timeout quarantines",
    "G_M6_4": "Global/per-target consumption cannot exceed budget",
    "G_M6_5": "STOP fences next send and focus serializes domains",
    "G_M7_1": "Same seed and trace give identical canonical bytes",
    "G_M7_2": "Injected cross-target state bleed fails scorecard",
    "G_M7_3": "Heterogeneous trial reports every target kind",
    "G_M7_4": "Pooled confusion equals sum and rejects missing seals",
    "G_M7_5": "Crash/timeout counted without false success",
    "G_M8_1": "Wrapper preserves public validators and handle binding",
    "G_M8_2": "Godot and CDP use independent cursors and claims",
    "G_M8_3": "Backpressure preserves complete batches and frame tick",
    "G_M8_4": "Replayed confirm and automated main save refuse",
    "G_M8_5": "Visual corroboration requires same-target basis/tick",
}

_DIAGNOSTIC_TIMING_KEYS = frozenset({
    "wall_time",
    "wall_ms",
    "wall_ns",
    "wall_s",
    "wall_seconds",
    "wall_ms_per_frame",
    "diagnostic_wall_time",
    "diagnostic_seconds",
    "diagnostic_ms",
    "diagnostic_timing",
    "elapsed_wall_ms",
    "elapsed_wall_ns",
    "elapsed_wall_s",
    "host_speed",
    "measured_host_speed",
    "host_ms",
    "host_name",
    "hostname",
    "host",
    "pid",
    "load1",
    "engine_ms",
    "generator_ms",
    "ms_per_frame",
    "ms_per_frame_1080p",
    "generator_ms_per_frame",
})

_BLEED_VIOLATION_TOKENS = frozenset({
    "cross_window_bleed",
    "cross_target_bleed",
    "state_bleed",
    "foreign_frame",
    "foreign_ax",
    "sibling_ax_bleed",
    "ring_alias",
    "epoch_alias",
    "reducer_bleed",
    "cursor_bleed",
    "claim_bleed",
})

_SAFETY_VIOLATION_TOKENS = frozenset({
    "cross_target_delivery",
    "unauthorized_focus",
    "stale_handle",
    "post_fence_send",
    "guard_bypass",
    "resource_contention",
    "concurrent_action_overflow",
    "budget_exceeded",
    "global_cap_exceeded",
    "target_cap_exceeded",
    "replayed_release",
    "replayed_confirm",
    "automated_main_save",
    "unreadable_fresh_gate",
    "geometry_focus_mismatch",
    "lock_order_violation",
    "mutex_spans_transport",
    "uncorroborated_visual_basis",
})

_FAULT_TERMINALS = frozenset({
    "CRASH",
    "TIMEOUT",
    "QUARANTINED",
    "FENCED",
    "CANCELLED",
    "ABORTED",
    "ERROR",
    "REFUSED",
})


class ScorecardValidationError(ScoreError, ValueError):
    """Base typed ValueError for multi-window scorecard validation failures."""

    def __init__(self, code: str, detail: str = ""):
        if not detail:
            detail = code
            code = "E_SCORECARD_VALIDATION"
        super().__init__(code, detail)


class WindowIdentityError(ScorecardValidationError):
    """Raised when window handle, target kind, or row identity is invalid or duplicated."""

    def __init__(self, detail: str):
        super().__init__("E_WINDOW_IDENTITY", detail)


class FrameSequenceError(ScorecardValidationError):
    """Raised when frame sequence ordering, bounds, or RGB admission invariants fail."""

    def __init__(self, detail: str):
        super().__init__("E_FRAME_SEQUENCE", detail)


class SealMismatchError(ScorecardValidationError):
    """Raised when a seal is missing, empty, or inconsistent across rows."""

    def __init__(self, detail: str):
        super().__init__("E_SEAL_MISMATCH", detail)


class TerminalValidationError(ScorecardValidationError):
    """Raised when terminal or expected_terminal metadata is missing or invalid."""

    def __init__(self, detail: str):
        super().__init__("E_TERMINAL_VALIDATION", detail)


class PrerequisiteAbsentError(ScorecardValidationError):
    """Raised when integration dispatch is attempted with absent prerequisites."""

    def __init__(self, detail: str):
        super().__init__("E_PREREQUISITE_ABSENT", detail)


def _require_int(value: Any, name: str, *, min_value: int | None = None, error_cls=ScorecardValidationError) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise error_cls(f"{name} must be an integer, got {type(value).__name__}")
    ival = int(value)
    if min_value is not None and ival < min_value:
        raise error_cls(f"{name} must be >= {min_value}, got {ival}")
    return ival


def _require_nonempty_str(value: Any, name: str, *, error_cls=ScorecardValidationError) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_cls(f"{name} must be a non-empty string")
    return value


def _validate_handle_like(handle: Any) -> tuple[str, str, int]:
    if handle is None:
        raise WindowIdentityError("handle is required")
    kind = getattr(handle, "kind", None)
    canonical_id = getattr(handle, "canonical_id", None)
    generation = getattr(handle, "generation", None)
    if not isinstance(kind, str) or kind not in SUPPORTED_TARGET_KINDS:
        raise WindowIdentityError(f"unsupported target kind: {kind!r}")
    if not isinstance(canonical_id, str) or not canonical_id.strip():
        raise WindowIdentityError("handle.canonical_id must be a non-empty string")
    gen = _require_int(generation, "handle.generation", min_value=1, error_cls=WindowIdentityError)
    return (kind, canonical_id, gen)


def canonical_handle_key(handle: Any) -> str:
    """Collision-free incarnation key: kind, length-delimited ID, generation."""
    kind, canonical_id, generation = _validate_handle_like(handle)
    return f"{kind}:{len(canonical_id)}:{canonical_id}:{generation}"


def canonical_target_sort_key(handle: Any) -> tuple[str, str, int]:
    """Return canonical ordering key for same-rank target lock acquisition."""
    return _validate_handle_like(handle)


def canonical_actuation_resource(handle: Any, destination: str | None = None) -> str:
    """Map target handle to its canonical actuation exclusion resource.

    A capsule and its child screen use the same canonical actuation resource
    when addressing the same destination. Godot and CDP keep separate resources.
    """
    kind, canonical_id, _ = _validate_handle_like(handle)
    dest = (destination or canonical_id).strip()
    if kind in ("capsule", "xvfb_screen", "x11_window"):
        base = dest.split("/", 1)[0]
        return f"display:{base}"
    return f"{kind}:{dest}"


def validate_lock_order(lock_sequence: tuple[str, ...] | list[str]) -> bool:
    """Validate that acquired metadata locks strictly follow METADATA_LOCK_ORDER."""
    rank_map = {name: idx for idx, name in enumerate(METADATA_LOCK_ORDER)}
    last_rank = -1
    for lock_name in lock_sequence:
        if lock_name not in rank_map:
            raise ScorecardValidationError("E_LOCK_ORDER", f"unknown metadata lock {lock_name!r}")
        rank = rank_map[lock_name]
        if rank < last_rank:
            raise ScorecardValidationError(
                "E_LOCK_ORDER",
                f"metadata lock order violation: {lock_name!r} acquired after rank {last_rank}",
            )
        last_rank = rank
    return True


def _admit_owned_rgb(rgb: Any, width: int, height: int) -> np.ndarray:
    """Validate and copy an RGB frame into an owned, contiguous, read-only uint8 array."""
    if isinstance(rgb, (bytes, bytearray, memoryview)):
        raw = np.frombuffer(rgb, dtype=np.uint8)
        if raw.size != width * height * 3:
            raise FrameSequenceError(
                f"RGB byte buffer size {raw.size} does not match {height}x{width}x3"
            )
        arr = raw.reshape((height, width, 3)).copy()
    elif isinstance(rgb, np.ndarray):
        if rgb.dtype != np.uint8:
            raise FrameSequenceError(f"RGB array must have uint8 dtype, got {rgb.dtype}")
        if rgb.shape != (height, width, 3):
            raise FrameSequenceError(
                f"RGB array shape {rgb.shape} does not match ({height}, {width}, 3)"
            )
        arr = np.ascontiguousarray(rgb, dtype=np.uint8).copy()
    else:
        raise FrameSequenceError("FrameRef.rgb must be a uint8 ndarray or RGB byte buffer")
    arr.flags.writeable = False
    return arr


# ---------------------------------------------------------------------------
# Canonical frozen dataclasses (exact field order per shared value contract)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int = 1

    def __post_init__(self) -> None:
        _validate_handle_like(self)


@dataclass(frozen=True)
class TargetRecord:
    handle: Handle
    transport: object
    owned: bool
    epoch: int
    session_id: str | None
    attachment_owned: bool
    closed: bool

    def __post_init__(self) -> None:
        _validate_handle_like(self.handle)
        _require_int(self.epoch, "TargetRecord.epoch", min_value=0, error_cls=WindowIdentityError)
        if not isinstance(self.owned, bool) or not isinstance(self.attachment_owned, bool) or not isinstance(self.closed, bool):
            raise WindowIdentityError("TargetRecord boolean flags must be bool")
        if self.session_id is not None and (not isinstance(self.session_id, str) or not self.session_id.strip()):
            raise WindowIdentityError("TargetRecord.session_id must be a non-empty string or None")


@dataclass(frozen=True)
class FrameRef:
    handle: Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: object
    digest: str

    def __post_init__(self) -> None:
        _validate_handle_like(self.handle)
        _require_int(self.epoch, "FrameRef.epoch", min_value=0, error_cls=FrameSequenceError)
        _require_int(self.frame_seq, "FrameRef.frame_seq", min_value=0, error_cls=FrameSequenceError)
        w = _require_int(self.width, "FrameRef.width", min_value=1, error_cls=FrameSequenceError)
        h = _require_int(self.height, "FrameRef.height", min_value=1, error_cls=FrameSequenceError)
        if not isinstance(self.digest, str) or len(self.digest) != 64 or any(c not in "0123456789abcdef" for c in self.digest):
            raise FrameSequenceError("FrameRef.digest must be 64 lowercase SHA-256 hexadecimal characters")
        owned_rgb = _admit_owned_rgb(self.rgb, w, h)
        actual_digest = hashlib.sha256(owned_rgb.tobytes()).hexdigest()
        if not hmac.compare_digest(actual_digest, self.digest):
            raise FrameSequenceError("FrameRef.digest does not match owned RGB bytes")
        object.__setattr__(self, "rgb", owned_rgb)


@dataclass(frozen=True)
class Observation:
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: FrameRef

    def __post_init__(self) -> None:
        h_key = _validate_handle_like(self.handle)
        ep = _require_int(self.epoch, "Observation.epoch", min_value=0, error_cls=FrameSequenceError)
        seq = _require_int(self.frame_seq, "Observation.frame_seq", min_value=0, error_cls=FrameSequenceError)
        _require_int(self.dpr_ppm, "Observation.dpr_ppm", min_value=1, error_cls=FrameSequenceError)
        if not isinstance(self.state, dict):
            raise ScorecardValidationError("E_OBSERVATION_STATE", "Observation.state must be a dict")
        if self.frame is None:
            raise FrameSequenceError("Observation.frame is required")
        f_key = _validate_handle_like(self.frame.handle)
        if f_key != h_key or self.frame.epoch != ep or self.frame.frame_seq != seq:
            raise WindowIdentityError("Observation frame handle/epoch/frame_seq mismatch")


@dataclass(frozen=True)
class Receipt:
    action_id: str
    handle: Handle
    status: str
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty_str(self.action_id, "Receipt.action_id")
        _validate_handle_like(self.handle)
        _require_nonempty_str(self.status, "Receipt.status")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ScorecardValidationError("E_RECEIPT", "Receipt.reason must be str or None")


@dataclass(frozen=True)
class ActionRequest:
    handle: Handle
    action_id: str
    epoch: int
    payload: dict
    cost: int
    deadline_ns: int

    def __post_init__(self) -> None:
        _validate_handle_like(self.handle)
        _require_nonempty_str(self.action_id, "ActionRequest.action_id")
        _require_int(self.epoch, "ActionRequest.epoch", min_value=0)
        if not isinstance(self.payload, dict):
            raise ScorecardValidationError("E_ACTION_REQUEST", "ActionRequest.payload must be a dict")
        _require_int(self.cost, "ActionRequest.cost", min_value=1)
        _require_int(self.deadline_ns, "ActionRequest.deadline_ns", min_value=0)


@dataclass(frozen=True)
class ActorLease:
    handle: Handle
    owner: str
    generation: int
    nonce: bytes
    expires_ns: int

    def __post_init__(self) -> None:
        _validate_handle_like(self.handle)
        _require_nonempty_str(self.owner, "ActorLease.owner")
        _require_int(self.generation, "ActorLease.generation", min_value=1)
        if not isinstance(self.nonce, bytes) or not self.nonce:
            raise ScorecardValidationError("E_ACTOR_LEASE", "ActorLease.nonce must be non-empty bytes")
        _require_int(self.expires_ns, "ActorLease.expires_ns", min_value=0)


@dataclass(frozen=True)
class FocusLease:
    lease: ActorLease
    domain: str
    epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ActorLease):
            _validate_handle_like(getattr(self.lease, "handle", None))
        _require_nonempty_str(self.domain, "FocusLease.domain")
        _require_int(self.epoch, "FocusLease.epoch", min_value=0)


@dataclass(frozen=True)
class StreamSpec:
    width: int
    height: int
    slots: int
    quota_bytes: int

    def __post_init__(self) -> None:
        _require_int(self.width, "StreamSpec.width", min_value=1)
        _require_int(self.height, "StreamSpec.height", min_value=1)
        _require_int(self.slots, "StreamSpec.slots", min_value=1)
        _require_int(self.quota_bytes, "StreamSpec.quota_bytes", min_value=1)


@dataclass(frozen=True)
class StreamToken:
    handle: Handle
    epoch: int

    def __post_init__(self) -> None:
        _validate_handle_like(self.handle)
        _require_int(self.epoch, "StreamToken.epoch", min_value=0)


@dataclass(frozen=True)
class Ticket:
    action_id: str
    handle: Handle

    def __post_init__(self) -> None:
        _require_nonempty_str(self.action_id, "Ticket.action_id")
        _validate_handle_like(self.handle)


@dataclass(frozen=True)
class ChangeResult:
    handle: Handle
    epoch: int
    settled: bool
    claims: np.ndarray
    ops: dict

    def __post_init__(self) -> None:
        _validate_handle_like(self.handle)
        _require_int(self.epoch, "ChangeResult.epoch", min_value=0)
        if not isinstance(self.settled, bool):
            raise ScorecardValidationError("E_CHANGE_RESULT", "ChangeResult.settled must be bool")
        arr = np.ascontiguousarray(self.claims).copy()
        arr.flags.writeable = False
        object.__setattr__(self, "claims", arr)
        if not isinstance(self.ops, dict):
            raise ScorecardValidationError("E_CHANGE_RESULT", "ChangeResult.ops must be a dict")


@dataclass(frozen=True)
class PerceptionResult:
    handle: Handle
    observation: Observation | None
    error: str | None = None

    def __post_init__(self) -> None:
        h_key = _validate_handle_like(self.handle)
        if self.observation is None and not self.error:
            raise ScorecardValidationError("E_PERCEPTION_RESULT", "PerceptionResult requires observation or error")
        if self.observation is not None:
            if _validate_handle_like(self.observation.handle) != h_key:
                raise WindowIdentityError("PerceptionResult observation handle mismatch")


@dataclass(frozen=True)
class WindowFrames:
    handle: Handle
    epoch: int
    frames: tuple
    frame_seqs: tuple
    dpr_ppm: int = 1_000_000

    def __post_init__(self) -> None:
        h_key = _validate_handle_like(self.handle)
        ep = _require_int(self.epoch, "WindowFrames.epoch", min_value=0, error_cls=FrameSequenceError)
        _require_int(self.dpr_ppm, "WindowFrames.dpr_ppm", min_value=1, error_cls=FrameSequenceError)
        raw_frames = tuple(self.frames)
        raw_seqs = tuple(
            _require_int(s, "WindowFrames.frame_seqs_item", min_value=0, error_cls=FrameSequenceError)
            for s in self.frame_seqs
        )
        if len(raw_frames) != len(raw_seqs):
            raise FrameSequenceError("WindowFrames.frames and frame_seqs must have equal length")
        for prev, cur in zip(raw_seqs, raw_seqs[1:]):
            if cur <= prev:
                raise FrameSequenceError("WindowFrames.frame_seqs must be strictly increasing")
        admitted_frames = []
        ref_shape = None
        for idx, (frame_obj, seq) in enumerate(zip(raw_frames, raw_seqs)):
            if isinstance(frame_obj, FrameRef) or hasattr(frame_obj, "rgb"):
                f_handle = getattr(frame_obj, "handle", self.handle)
                if _validate_handle_like(f_handle) != h_key:
                    raise WindowIdentityError(f"WindowFrames[{idx}] foreign frame handle")
                f_epoch = getattr(frame_obj, "epoch", ep)
                if f_epoch != ep:
                    raise WindowIdentityError(f"WindowFrames[{idx}] frame epoch {f_epoch} != {ep}")
                f_seq = getattr(frame_obj, "frame_seq", seq)
                if f_seq != seq:
                    raise FrameSequenceError(f"WindowFrames[{idx}] frame_seq {f_seq} != {seq}")
                width = _require_int(getattr(frame_obj, "width", frame_obj.rgb.shape[1]),
                                     "WindowFrames.width", min_value=1, error_cls=FrameSequenceError)
                height = _require_int(getattr(frame_obj, "height", frame_obj.rgb.shape[0]),
                                      "WindowFrames.height", min_value=1, error_cls=FrameSequenceError)
                arr = _admit_owned_rgb(frame_obj.rgb, width, height)
                if ref_shape is None:
                    ref_shape = arr.shape
                elif arr.shape != ref_shape:
                    raise FrameSequenceError("WindowFrames geometry changed within single epoch")
                admitted_frames.append(frame_obj if isinstance(frame_obj, FrameRef) else arr)
            elif isinstance(frame_obj, np.ndarray):
                if frame_obj.ndim != 3 or frame_obj.shape[2] != 3:
                    raise FrameSequenceError("WindowFrames ndarray must have shape (H, W, 3)")
                arr = _admit_owned_rgb(frame_obj, int(frame_obj.shape[1]), int(frame_obj.shape[0]))
                if ref_shape is None:
                    ref_shape = arr.shape
                elif arr.shape != ref_shape:
                    raise FrameSequenceError("WindowFrames geometry changed within single epoch")
                admitted_frames.append(arr)
            else:
                raise FrameSequenceError(f"WindowFrames[{idx}] must be FrameRef or uint8 RGB ndarray")
        object.__setattr__(self, "frames", tuple(admitted_frames))
        object.__setattr__(self, "frame_seqs", raw_seqs)


@dataclass(frozen=True)
class WindowDescription:
    handle: Handle
    epoch: int
    frame_seqs: tuple
    descriptors: tuple
    regions: tuple
    error: str | None = None

    def __post_init__(self) -> None:
        h_key = _validate_handle_like(self.handle)
        ep = _require_int(self.epoch, "WindowDescription.epoch", min_value=0, error_cls=FrameSequenceError)
        seqs = tuple(
            _require_int(s, "WindowDescription.frame_seqs_item", min_value=0, error_cls=FrameSequenceError)
            for s in self.frame_seqs
        )
        for prev, cur in zip(seqs, seqs[1:]):
            if cur <= prev:
                raise FrameSequenceError("WindowDescription.frame_seqs must be strictly increasing")
        descs = tuple(self.descriptors)
        regs = tuple(self.regions)
        for item in (*descs, *regs):
            if hasattr(item, "handle") and _validate_handle_like(item.handle) != h_key:
                raise WindowIdentityError("WindowDescription contains foreign handle evidence")
            if hasattr(item, "epoch") and _require_int(
                item.epoch, "WindowDescription.item.epoch", min_value=0,
                error_cls=WindowIdentityError,
            ) != ep:
                raise WindowIdentityError("WindowDescription contains mismatched epoch evidence")
        object.__setattr__(self, "frame_seqs", seqs)
        object.__setattr__(self, "descriptors", descs)
        object.__setattr__(self, "regions", regs)


# ---------------------------------------------------------------------------
# Frozen scorecard records (exact field order per scorecard contract)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    name: str
    targets: tuple
    events: tuple
    expected_outcomes: dict

    def __post_init__(self) -> None:
        _require_nonempty_str(self.name, "Scenario.name")
        object.__setattr__(self, "targets", tuple(self.targets))
        object.__setattr__(self, "events", tuple(self.events))
        if not isinstance(self.expected_outcomes, dict):
            raise ScorecardValidationError("E_SCENARIO", "Scenario.expected_outcomes must be a dict")


@dataclass(frozen=True)
class WindowEvidence:
    handle: Handle
    scene: str
    epoch: int
    frame_seq: int
    tp: int
    fp: int
    fn: int
    tn: int
    ops: dict
    frame_supported: bool
    dropped: bool
    exact: bool | None
    terminal: str | None
    expected_terminal: str | None
    seal: str
    violations: tuple = ()

    def __post_init__(self) -> None:
        _validate_handle_like(self.handle)
        _require_nonempty_str(self.scene, "WindowEvidence.scene", error_cls=WindowIdentityError)
        _require_int(self.epoch, "WindowEvidence.epoch", min_value=0, error_cls=WindowIdentityError)
        _require_int(self.frame_seq, "WindowEvidence.frame_seq", min_value=0, error_cls=FrameSequenceError)
        for name in ("tp", "fp", "fn", "tn"):
            _require_int(getattr(self, name), f"WindowEvidence.{name}", min_value=0)
        if not isinstance(self.ops, dict):
            raise ScorecardValidationError("E_OPS_TYPE", "WindowEvidence.ops must be a dict")
        if not isinstance(self.frame_supported, bool) or not isinstance(self.dropped, bool):
            raise ScorecardValidationError("E_FRAME_SUPPORT", "frame_supported and dropped must be bool")
        if self.exact is not None and not isinstance(self.exact, bool):
            raise ScorecardValidationError("E_FRAME_EXACT", "exact must be bool or None")
        if not isinstance(self.seal, str):
            raise SealMismatchError("WindowEvidence.seal must be a string")
        if not isinstance(self.violations, tuple):
            object.__setattr__(self, "violations", tuple(self.violations))


@dataclass(frozen=True)
class TrialReport:
    rows: tuple
    outcomes: dict
    faults: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        if not isinstance(self.outcomes, dict):
            raise ScorecardValidationError("E_TRIAL_REPORT", "TrialReport.outcomes must be a dict")
        object.__setattr__(self, "faults", tuple(self.faults))


@dataclass(frozen=True)
class Scorecard:
    windows: tuple
    pooled: dict
    metrics: dict
    violations: tuple
    passed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "windows", tuple(self.windows))
        object.__setattr__(self, "violations", tuple(self.violations))
        if not isinstance(self.pooled, dict) or not isinstance(self.metrics, dict):
            raise ScorecardValidationError("E_SCORECARD", "Scorecard.pooled and metrics must be dicts")
        if not isinstance(self.passed, bool):
            raise ScorecardValidationError("E_SCORECARD", "Scorecard.passed must be a bool")


# ---------------------------------------------------------------------------
# Mutable FakeClock fixture
# ---------------------------------------------------------------------------

@dataclass
class FakeClock:
    now_ns: int = 0

    def __post_init__(self) -> None:
        self.now_ns = _require_int(self.now_ns, "FakeClock.now_ns", min_value=0)

    def __call__(self) -> int:
        return int(self.now_ns)

    def advance(self, delta_ns: int) -> int:
        delta = _require_int(delta_ns, "delta_ns", min_value=0)
        self.now_ns += delta
        return int(self.now_ns)


# ---------------------------------------------------------------------------
# Ratio & validation helpers
# ---------------------------------------------------------------------------

def _make_ratio(
    numerator: int | None,
    denominator: int | None,
    *,
    pass_when=None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a mandatory ratio record retaining integer numerator and denominator.

    Absent or zero denominator is explicitly represented as None (null/AUSENTE),
    never replaced with 0 or 0/1, and always fails a mandatory gate.
    """
    num = 0 if numerator is None else _require_int(numerator, "ratio.numerator", min_value=0)
    if denominator is None:
        den = None
    else:
        raw_den = _require_int(denominator, "ratio.denominator", min_value=0)
        den = raw_den if raw_den > 0 else None

    if den is None:
        record: dict[str, Any] = {
            "numerator": num,
            "denominator": None,
            "defined": False,
            "status": AUSENTE,
            "fraction": None,
            "reduced_fraction": None,
            "permille": None,
            "ppm": None,
            "value": None,
            "passed": False,
        }
    else:
        frac = Fraction(num, den)
        passed = bool(pass_when(num, den)) if callable(pass_when) else True
        record = {
            "numerator": num,
            "denominator": den,
            "defined": True,
            "status": "MEASURED",
            "fraction": f"{num}/{den}",
            "reduced_fraction": f"{frac.numerator}/{frac.denominator}",
            "permille": (num * 1000) // den,
            "ppm": (num * 1_000_000) // den,
            "value": f"{num}/{den}",
            "passed": passed,
        }
    if extra:
        record.update(extra)
    return record


def _coerce_window_evidence(item: Any) -> WindowEvidence:
    if isinstance(item, WindowEvidence):
        return item
    if isinstance(item, dict):
        h = item.get("handle")
        if isinstance(h, dict):
            h = Handle(h["kind"], h["canonical_id"], h.get("generation", 1))
        return WindowEvidence(
            handle=h,
            scene=item["scene"],
            epoch=item["epoch"],
            frame_seq=item["frame_seq"],
            tp=item["tp"],
            fp=item["fp"],
            fn=item["fn"],
            tn=item["tn"],
            ops=item.get("ops", {}),
            frame_supported=item["frame_supported"],
            dropped=item["dropped"],
            exact=item.get("exact"),
            terminal=item.get("terminal"),
            expected_terminal=item.get("expected_terminal"),
            seal=item.get("seal", ""),
            violations=tuple(item.get("violations", ())),
        )
    h = getattr(item, "handle", None)
    if h is not None and not isinstance(h, Handle):
        h = Handle(h.kind, h.canonical_id, getattr(h, "generation", 1))
    return WindowEvidence(
        handle=h,
        scene=getattr(item, "scene"),
        epoch=getattr(item, "epoch"),
        frame_seq=getattr(item, "frame_seq"),
        tp=getattr(item, "tp"),
        fp=getattr(item, "fp"),
        fn=getattr(item, "fn"),
        tn=getattr(item, "tn"),
        ops=getattr(item, "ops", {}),
        frame_supported=getattr(item, "frame_supported"),
        dropped=getattr(item, "dropped"),
        exact=getattr(item, "exact", None),
        terminal=getattr(item, "terminal", None),
        expected_terminal=getattr(item, "expected_terminal", None),
        seal=getattr(item, "seal", ""),
        violations=tuple(getattr(item, "violations", ())),
    )


def _window_identity_key(row: WindowEvidence) -> tuple[str, str, int, str, int, int]:
    kind, cid, gen = _validate_handle_like(row.handle)
    return (kind, cid, gen, row.scene, row.epoch, row.frame_seq)


def _extract_numeric_ops(ops: dict[str, Any]) -> dict[str, int]:
    """Extract pure integer operation counters from a WindowEvidence.ops dict."""
    skip_keys = {
        "isolated_ops",
        "solo_ops",
        "stalls",
        "eligibility_stalls",
        "starvation",
        "wait_dispatches",
        "max_wait_dispatches",
        "queue_max",
        "queue_maxima",
        "quarantines",
        "recovery_generations",
        "silent_misses",
        "cross_window_bleed",
        "cross_target_deliveries",
        "safety_violations",
        "concurrent_actions",
        "committed_charge",
        "reserved_charge",
        "target_cap",
        "global_cap",
        "sim_elapsed_ns",
        "simulated_elapsed_ns",
        "confusion_7x7",
        "confusion",
        "lock_order",
        "foreign_handle",
        "foreign_row",
        "required_target_kinds",
        "expected_target_kinds",
        "heterogeneous_trial",
        *_DIAGNOSTIC_TIMING_KEYS,
    }
    if not isinstance(ops, dict):
        raise ScorecardValidationError("E_OPS_TYPE", "operations must be a dict")
    if any(not isinstance(k, str) for k in ops):
        raise ScorecardValidationError("E_OPS_TYPE", "operation keys must be strings")
    out: dict[str, int] = {}
    for k, v in sorted(ops.items()):
        if k in skip_keys or _is_diagnostic_timing_key(k):
            continue
        if v is None:
            raise ScorecardValidationError("E_OPS_NULL", f"operation counter {k!r} cannot be None")
        if isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)):
            raise ScorecardValidationError("E_OPS_TYPE", f"operation counter {k!r} must be an exact integer")
        ival = int(v)
        if ival < 0:
            raise ScorecardValidationError("E_OPS_NEGATIVE", f"operation counter {k!r} must be non-negative")
        out[k] = ival
    return out


def _validate_ops_evidence(ops: dict[str, Any]) -> None:
    """Refuse bool, float and stringified counters even in canonical witnesses."""
    _extract_numeric_ops(ops)
    for key in (
        "stalls", "eligibility_stalls", "starvation", "wait_dispatches",
        "max_wait_dispatches", "queue_max", "queue_maxima", "quarantines",
        "recovery_generations", "silent_misses", "cross_window_bleed",
        "cross_target_deliveries", "safety_violations", "concurrent_actions",
        "committed_charge", "reserved_charge", "target_cap", "global_cap",
        "sim_elapsed_ns", "simulated_elapsed_ns",
    ):
        if key in ops and ops[key] is not None:
            _require_int(ops[key], f"ops.{key}", min_value=0)
    for key in ("isolated_ops", "solo_ops"):
        if key in ops and ops[key] is not None:
            if not isinstance(ops[key], dict):
                raise ScorecardValidationError("E_ISOLATED_OPS", f"{key} must be a dict")
            _extract_numeric_ops(ops[key])
    for key in ("confusion_7x7", "confusion"):
        if key in ops and ops[key] is not None:
            try:
                arr = np.asarray(ops[key])
            except (TypeError, ValueError) as exc:
                raise ScorecardValidationError("E_CONFUSION_7X7", "invalid confusion evidence") from exc
            if arr.dtype.kind not in "iu" or (arr < 0).any():
                raise ScorecardValidationError("E_CONFUSION_7X7", "confusion counts must be non-negative integers")


# ---------------------------------------------------------------------------
# SHA-256 content witness for the complete ordered row set and trial manifest
# ---------------------------------------------------------------------------

def _score_manifest(rows: tuple[WindowEvidence, ...], outcomes: dict, faults: tuple) -> dict:
    """Bind each complete incarnation and event coordinate to manifest content."""
    identities = sorted({_validate_handle_like(row.handle) for row in rows})
    return {
        "schema": "vqa.multiwin_manifest/v1",
        "registered_handles": [list(identity) for identity in identities],
        "row_refs": [list(_window_identity_key(row)) for row in sorted(rows, key=_window_identity_key)],
        "outcomes": _canonicalize_value(outcomes),
        "faults": _canonicalize_value(tuple(faults)),
    }


def _computed_evidence_seal(rows: tuple[WindowEvidence, ...], manifest: dict) -> str:
    """SHA-256 of canonical compact JSON, with the self-referential seal excluded."""
    payload = {
        "schema": "vqa.multiwin_evidence_seal/v1",
        "manifest": manifest,
        "rows": [
            _canonicalize_value(replace(row, seal=""))
            for row in sorted(rows, key=_window_identity_key)
        ],
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def seal_trial(report: TrialReport) -> TrialReport:
    """Return an independently verifiable, content-sealed immutable trial.

    Sealing is a deterministic integrity witness, not a signature of provenance.
    The caller must supply isolated_ops from a separate production replay.
    """
    if not isinstance(report, TrialReport):
        raise ScorecardValidationError("E_TRIAL_REPORT", "seal_trial requires TrialReport")
    manifest = _score_manifest(report.rows, report.outcomes, report.faults)
    digest = _computed_evidence_seal(report.rows, manifest)
    return TrialReport(
        rows=tuple(replace(row, seal=digest) for row in report.rows),
        outcomes=report.outcomes,
        faults=report.faults,
    )


# ---------------------------------------------------------------------------
# Public scorecard evaluation: score_windows(rows) -> Scorecard
# ---------------------------------------------------------------------------

def score_windows(
    rows: Any,
    *,
    allowed_handles: set[Any] | tuple[Any, ...] | list[Any] | None = None,
    require_all_target_kinds: bool = False,
) -> Scorecard:
    """Evaluate per-window evidence and return a frozen Scorecard.

    Validates window identities `(handle, scene, epoch, frame_seq)`, rejects
    duplicate/foreign rows, missing terminals, and missing/mixed seals, and
    pools integer confusion counts explicitly across windows without renaming
    scenes or abusing duplicate-scene aggregation.
    """
    trial_outcomes: dict[str, Any] = {}
    trial_faults: tuple[Any, ...] = ()
    if isinstance(rows, TrialReport) or (hasattr(rows, "rows") and hasattr(rows, "outcomes")):
        trial_outcomes = dict(getattr(rows, "outcomes", {}))
        trial_faults = tuple(getattr(rows, "faults", ()))
        raw_rows = list(rows.rows)
    else:
        if rows is None or isinstance(rows, (str, bytes, dict)):
            raise ScorecardValidationError("E_ROWS_TYPE", "rows must be an iterable of WindowEvidence")
        raw_rows = list(rows)

    coerced_rows: list[WindowEvidence] = [_coerce_window_evidence(r) for r in raw_rows]

    allowed_key_set: set[tuple[str, str, int]] | None = None
    if allowed_handles is not None:
        allowed_key_set = {_validate_handle_like(_coerce_handle(h)) for h in allowed_handles}
    elif "allowed_handles" in trial_outcomes and trial_outcomes["allowed_handles"] is not None:
        allowed_key_set = {_validate_handle_like(_coerce_handle(h)) for h in trial_outcomes["allowed_handles"]}

    seen_identities: set[tuple[str, str, int, str, int, int]] = set()
    generation_by_target: dict[tuple[str, str], int] = {}
    seqs_by_stream: dict[tuple[str, str, int, str, int], list[int]] = {}
    rows_by_stream: dict[tuple[str, str, int, str, int], list[WindowEvidence]] = {}

    for idx, row in enumerate(coerced_rows):
        h_key = _validate_handle_like(row.handle)
        if allowed_key_set is not None and h_key not in allowed_key_set:
            raise WindowIdentityError(f"row[{idx}] foreign handle {h_key!r} not in allowed targets")
        base_identity = h_key[:2]
        existing_generation = generation_by_target.setdefault(base_identity, h_key[2])
        if existing_generation != h_key[2]:
            raise WindowIdentityError(f"mixed generations for {base_identity!r}: {existing_generation} and {h_key[2]}")
        if not isinstance(row.ops, dict):
            raise ScorecardValidationError("E_OPS_TYPE", f"row[{idx}].ops must be a dict")
        if row.ops.get("foreign_row") or row.ops.get("foreign_handle"):
            raise WindowIdentityError(f"row[{idx}] flagged as foreign window evidence")

        _require_nonempty_str(row.scene, f"row[{idx}].scene", error_cls=WindowIdentityError)
        _require_int(row.epoch, f"row[{idx}].epoch", min_value=0, error_cls=WindowIdentityError)
        _require_int(row.frame_seq, f"row[{idx}].frame_seq", min_value=0, error_cls=FrameSequenceError)

        for cname in ("tp", "fp", "fn", "tn"):
            _require_int(getattr(row, cname), f"row[{idx}].{cname}", min_value=0)

        if not isinstance(row.frame_supported, bool):
            raise ScorecardValidationError("E_FRAME_SUPPORT", f"row[{idx}].frame_supported must be bool")
        if not isinstance(row.dropped, bool):
            raise ScorecardValidationError("E_FRAME_DROP", f"row[{idx}].dropped must be bool")
        if row.exact is not None and not isinstance(row.exact, bool):
            raise ScorecardValidationError("E_FRAME_EXACT", f"row[{idx}].exact must be bool or None")

        if not isinstance(row.seal, str) or not row.seal:
            raise SealMismatchError(f"row[{idx}] is missing its evidence seal")

        ident = _window_identity_key(row)
        if ident in seen_identities:
            raise WindowIdentityError(f"duplicate window identity: {ident!r}")
        seen_identities.add(ident)

        stream_key = (h_key[0], h_key[1], h_key[2], row.scene, row.epoch)
        seqs_by_stream.setdefault(stream_key, []).append(row.frame_seq)
        rows_by_stream.setdefault(stream_key, []).append(row)

        if (row.terminal is None) != (row.expected_terminal is None):
            raise TerminalValidationError(
                f"row[{idx}] must provide both terminal and expected_terminal or neither"
            )
        if row.terminal is not None:
            _require_nonempty_str(row.terminal, f"row[{idx}].terminal", error_cls=TerminalValidationError)
        if row.expected_terminal is not None:
            _require_nonempty_str(
                row.expected_terminal, f"row[{idx}].expected_terminal", error_cls=TerminalValidationError
            )

        if "lock_order" in row.ops and row.ops["lock_order"] is not None:
            validate_lock_order(row.ops["lock_order"])

    # Validate per-stream frame_seq ordering and terminal presence
    for stream_key, seqs in seqs_by_stream.items():
        for prev_s, cur_s in zip(seqs, seqs[1:]):
            if cur_s <= prev_s:
                raise FrameSequenceError(
                    f"non-increasing frame_seq {seqs!r} for stream {stream_key!r}"
                )
        stream_rows = rows_by_stream[stream_key]
        if all(r.terminal is None or r.expected_terminal is None for r in stream_rows):
            raise TerminalValidationError(
                f"missing terminal state for window stream {stream_key!r}"
            )

    windows = tuple(coerced_rows)
    manifest = _score_manifest(windows, trial_outcomes, trial_faults)
    expected_seal = _computed_evidence_seal(windows, manifest)
    for idx, row in enumerate(windows):
        if len(row.seal) != 64 or not hmac.compare_digest(row.seal, expected_seal):
            raise SealMismatchError(f"row[{idx}] seal does not match canonical rows and manifest")
    n_windows = len(windows)
    violations: list[str] = []

    if n_windows == 0:
        violations.append("empty_windows")

    # 1. Pool integer confusion explicitly (INV-M7-2, G-M7-4)
    tp = sum(r.tp for r in windows)
    fp = sum(r.fp for r in windows)
    fn = sum(r.fn for r in windows)
    tn = sum(r.tn for r in windows)
    total_confusion = tp + fp + fn + tn
    pos_support = tp + fn
    pred_pos = tp + fp
    neg_support = fp + tn

    silent_misses = sum(_require_int(r.ops.get("silent_misses", 0), "silent_misses", min_value=0) for r in windows)
    pooled_7x7: np.ndarray | None = None
    for r in windows:
        c7 = r.ops.get("confusion_7x7")
        if c7 is not None:
            arr7 = np.asarray(c7)
            if arr7.shape != (7, 7) or arr7.dtype.kind not in "iu" or (arr7 < 0).any():
                raise ScorecardValidationError("E_CONFUSION_7X7", "confusion_7x7 must be a non-negative 7x7 integer matrix")
            arr7 = arr7.astype(np.int64)
            pooled_7x7 = arr7 if pooled_7x7 is None else (pooled_7x7 + arr7)

    honesty_summary: dict[str, Any] | None = None
    if pooled_7x7 is not None:
        c7_list = pooled_7x7.tolist()
        sm_ppm = silent_miss_ppm(c7_list)
        real_change_sup = int(sum(sum(c7_list[k]) for k in (2, 3, 4)))
        honesty_summary = {
            "confusion_7x7": c7_list,
            "silent_miss_ppm": sm_ppm if real_change_sup > 0 else None,
            "real_change_support": real_change_sup if real_change_sup > 0 else None,
            "class_honesty": class_honesty(c7_list),
        }
        if real_change_sup > 0 and sm_ppm > 0:
            silent_misses += int(sum(c7_list[r_idx][c_idx] for r_idx in (2, 3, 4) for c_idx in (0, 1)))

    precision_ratio = _make_ratio(tp, pred_pos if pred_pos > 0 else None)
    recall_ratio = _make_ratio(tp, pos_support if pos_support > 0 else None)
    f1_den = 2 * tp + fp + fn
    f1_ratio = _make_ratio(2 * tp, f1_den if f1_den > 0 else None)
    accuracy_ratio = _make_ratio(tp + tn, total_confusion if total_confusion > 0 else None)

    confusion_denominators_ok = (pos_support > 0 and pred_pos > 0 and total_confusion > 0)
    if not confusion_denominators_ok:
        violations.append("missing_denominator:pooled_confusion")
    if silent_misses > 0:
        violations.append("silent_misses_present")

    pooled_confusion_metric = _make_ratio(
        tp,
        pos_support if pos_support > 0 else None,
        pass_when=lambda _n, _d: confusion_denominators_ok and silent_misses == 0,
        extra={
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "total": total_confusion,
            "support": pos_support if pos_support > 0 else None,
            "predicted_positive": pred_pos if pred_pos > 0 else None,
            "negative_support": neg_support if neg_support > 0 else None,
            "precision": precision_ratio,
            "recall": recall_ratio,
            "f1": f1_ratio,
            "accuracy": accuracy_ratio,
            "silent_misses": silent_misses,
            "silent_miss_ppm": None if pos_support == 0 else (silent_misses * 1_000_000) // pos_support,
            "honesty": honesty_summary,
        },
    )

    # 2. Cross-window bleed (G-M7-2)
    bleed_count = 0
    for r in windows:
        bleed_count += _require_int(r.ops.get("cross_window_bleed", 0), "cross_window_bleed", min_value=0)
        bleed_count += _require_int(r.ops.get("cross_target_deliveries", 0), "cross_target_deliveries", min_value=0)
        for v in r.violations:
            v_norm = str(v).strip().lower()
            if v_norm in _BLEED_VIOLATION_TOKENS or "bleed" in v_norm or "alias" in v_norm or "foreign" in v_norm:
                bleed_count += 1
    if trial_outcomes.get("cross_window_bleed"):
        bleed_count += _require_int(trial_outcomes["cross_window_bleed"], "cross_window_bleed", min_value=0)
    if bleed_count > 0:
        violations.append("cross_window_bleed")

    cross_window_bleed_metric = _make_ratio(
        bleed_count,
        n_windows if n_windows > 0 else None,
        pass_when=lambda num, _den: num == 0,
        extra={"bleed_events": bleed_count},
    )
    if n_windows == 0:
        violations.append("missing_denominator:cross_window_bleed")

    # 3. Safety violations (ownership, focus, stale handle, post-fence, budget, resource contention)
    safety_count = 0
    for r in windows:
        safety_count += _require_int(r.ops.get("safety_violations", 0), "safety_violations", min_value=0)
        concurrent = _require_int(r.ops.get("concurrent_actions", 1), "concurrent_actions", min_value=0)
        if concurrent > 1:
            safety_count += concurrent - 1
            violations.append("concurrent_action_overflow")
        committed = _require_int(r.ops.get("committed_charge", 0), "committed_charge", min_value=0)
        reserved = _require_int(r.ops.get("reserved_charge", 0), "reserved_charge", min_value=0)
        target_cap = r.ops.get("target_cap")
        global_cap = r.ops.get("global_cap")
        if target_cap is not None and committed + reserved > _require_int(target_cap, "target_cap", min_value=0):
            safety_count += 1
            violations.append("target_cap_exceeded")
        if global_cap is not None and committed + reserved > _require_int(global_cap, "global_cap", min_value=0):
            safety_count += 1
            violations.append("global_cap_exceeded")
        for v in r.violations:
            v_norm = str(v).strip().lower()
            if v_norm in _SAFETY_VIOLATION_TOKENS or (
                v_norm not in _BLEED_VIOLATION_TOKENS
                and "stall" not in v_norm
                and "starv" not in v_norm
            ):
                safety_count += 1
                violations.append(f"safety:{v_norm}")
    if safety_count > 0 and "safety_violations" not in violations:
        violations.append("safety_violations")

    safety_violations_metric = _make_ratio(
        safety_count,
        n_windows if n_windows > 0 else None,
        pass_when=lambda num, _den: num == 0,
        extra={"violation_count": safety_count},
    )
    if n_windows == 0:
        violations.append("missing_denominator:safety_violations")

    # 4. Frame support, drops, and exactness
    supported_count = sum(1 for r in windows if r.frame_supported)
    dropped_count = sum(1 for r in windows if r.dropped)
    delivered_supported = sum(1 for r in windows if r.frame_supported and not r.dropped)
    exact_delivered = sum(
        1 for r in windows if r.frame_supported and not r.dropped and r.exact is True
    )
    pixel_mismatches = sum(
        1
        for r in windows
        if r.exact is False or (r.frame_supported and not r.dropped and r.exact is not True)
    )
    unrecorded_drop_errors = sum(
        1 for r in windows if (not r.frame_supported and not r.dropped and r.exact is True)
    )
    if delivered_supported == 0:
        violations.append("missing_denominator:frame_exactness")
    if pixel_mismatches > 0:
        violations.append("frame_exactness_mismatch")
    if supported_count == 0:
        violations.append("missing_denominator:frame_drops")

    frame_exactness_metric = _make_ratio(
        exact_delivered,
        delivered_supported if delivered_supported > 0 else None,
        pass_when=lambda num, den: num == den and pixel_mismatches == 0 and unrecorded_drop_errors == 0,
        extra={
            "supported": supported_count,
            "delivered": delivered_supported,
            "exact": exact_delivered,
            "pixel_mismatches": pixel_mismatches,
        },
    )

    frame_drops_metric = _make_ratio(
        dropped_count,
        supported_count if supported_count > 0 else None,
        pass_when=lambda _num, den: den > 0 and delivered_supported > 0 and unrecorded_drop_errors == 0,
        extra={
            "dropped": dropped_count,
            "supported": supported_count,
            "delivered": delivered_supported,
        },
    )

    # 5. Operation totals vs isolated replay (G-M5-2, INV-M7-2)
    per_window_ops: list[dict[str, Any]] = []
    pooled_ops: dict[str, int] = {}
    isolated_pooled_ops: dict[str, int] = {}
    op_matched_windows = 0
    op_mismatch_windows = 0
    missing_op_denom = False

    for r in sorted(windows, key=_window_identity_key):
        if r.ops is None:
            missing_op_denom = True
            continue
        win_ops = _extract_numeric_ops(r.ops)
        iso_raw = r.ops.get("isolated_ops", r.ops.get("solo_ops"))
        if iso_raw is None:
            raise ScorecardValidationError("E_ISOLATED_OPS", "independent isolated_ops comparator is required")
        if not isinstance(iso_raw, dict):
            raise ScorecardValidationError("E_ISOLATED_OPS", "isolated_ops must be a dict")
        iso_ops = _extract_numeric_ops(iso_raw)
        matched = (win_ops == iso_ops)
        if matched:
            op_matched_windows += 1
        else:
            op_mismatch_windows += 1
        for k, v in win_ops.items():
            pooled_ops[k] = pooled_ops.get(k, 0) + v
        for k, v in iso_ops.items():
            isolated_pooled_ops[k] = isolated_pooled_ops.get(k, 0) + v
        per_window_ops.append({
            "handle": canonical_handle_key(r.handle),
            "generation": r.handle.generation,
            "scene": r.scene,
            "epoch": r.epoch,
            "frame_seq": r.frame_seq,
            "ops": win_ops,
            "isolated_ops": iso_ops,
            "matched": matched,
        })

    if op_mismatch_windows > 0:
        violations.append("operation_totals_mismatch")
    if n_windows == 0 or missing_op_denom:
        violations.append("missing_denominator:operation_totals")

    operation_totals_metric = _make_ratio(
        op_matched_windows,
        n_windows if (n_windows > 0 and not missing_op_denom) else None,
        pass_when=lambda num, den: num == den and op_mismatch_windows == 0 and pooled_ops == isolated_pooled_ops,
        extra={
            "pooled_ops": dict(sorted(pooled_ops.items())),
            "isolated_pooled_ops": dict(sorted(isolated_pooled_ops.items())),
            "total_ops": sum(pooled_ops.values()),
            "isolated_total_ops": sum(isolated_pooled_ops.values()),
            "per_window": tuple(per_window_ops),
            "mismatched_windows": op_mismatch_windows,
        },
    )

    # 6. Eligibility stalls & 7. Starvation (G-M6-2)
    distinct_targets = {canonical_handle_key(r.handle) for r in windows}
    n_targets = len(distinct_targets)
    fair_bound = 2 * max(1, n_targets)
    total_stalls = 0
    stall_violation_count = 0
    starvation_count = 0
    max_wait_observed = 0
    queue_maxima = 0
    quarantines = 0
    recovery_generations = 0

    for r in windows:
        stalls_val = _require_int(
            r.ops.get("eligibility_stalls", r.ops.get("stalls", 0)),
            "eligibility_stalls",
            min_value=0,
        )
        total_stalls += stalls_val
        if r.ops.get("eligibility_stalls", 0) > 0 or any("stall" in str(v).lower() for v in r.violations):
            stall_violation_count += max(1, int(r.ops.get("eligibility_stalls", 0)))
        wait_disp = _require_int(
            r.ops.get("wait_dispatches", r.ops.get("max_wait_dispatches", 0)),
            "wait_dispatches",
            min_value=0,
        )
        if wait_disp > max_wait_observed:
            max_wait_observed = wait_disp
        starv_val = _require_int(r.ops.get("starvation", 0), "starvation", min_value=0)
        if wait_disp > fair_bound or starv_val > 0 or any("starv" in str(v).lower() for v in r.violations):
            starvation_count += max(1, starv_val)
        q_max = _require_int(
            r.ops.get("queue_max", r.ops.get("queue_maxima", 0)),
            "queue_max",
            min_value=0,
        )
        if q_max > queue_maxima:
            queue_maxima = q_max
        quarantines += _require_int(r.ops.get("quarantines", 0), "quarantines", min_value=0)
        rec_gen = _require_int(
            r.ops.get("recovery_generations", r.handle.generation),
            "recovery_generations",
            min_value=0,
        )
        if rec_gen > recovery_generations:
            recovery_generations = rec_gen

    if stall_violation_count > 0:
        violations.append("eligibility_stalls")
    if starvation_count > 0:
        violations.append("starvation_bound_exceeded")
    if n_windows == 0:
        violations.append("missing_denominator:eligibility_stalls")
        violations.append("missing_denominator:starvation")

    eligibility_stalls_metric = _make_ratio(
        stall_violation_count,
        n_windows if n_windows > 0 else None,
        pass_when=lambda num, _den: num == 0,
        extra={"total_stalls": total_stalls, "stall_violations": stall_violation_count},
    )

    starvation_metric = _make_ratio(
        starvation_count,
        n_windows if n_windows > 0 else None,
        pass_when=lambda num, _den: num == 0 and max_wait_observed <= fair_bound,
        extra={
            "starved_windows": starvation_count,
            "max_wait_dispatches": max_wait_observed,
            "fair_bound": fair_bound,
            "eligible_targets": n_targets,
        },
    )

    # 8. Terminal completeness & fault accounting (INV-M7-3, G-M7-5)
    terminal_rows = [r for r in windows if r.expected_terminal is not None or r.terminal is not None]
    admitted_actions = len(terminal_rows)
    matched_terminals = 0
    nominal_required = 0
    nominal_completed = 0
    fault_expected = 0
    fault_matched = 0
    unexpected_faults = 0
    unknown_terminals = 0
    crash_count = 0
    timeout_count = 0

    for r in terminal_rows:
        term = (r.terminal or "").strip().upper()
        exp_term = (r.expected_terminal or "").strip().upper()
        if term == "CRASH":
            crash_count += 1
        elif term == "TIMEOUT":
            timeout_count += 1
        elif term == "QUARANTINED":
            quarantines += 1

        if term == "UNKNOWN" or exp_term == "UNKNOWN":
            unknown_terminals += 1
            violations.append("unknown_terminal")
            continue

        is_expected_fault = exp_term in _FAULT_TERMINALS
        if is_expected_fault:
            fault_expected += 1
            if term == exp_term:
                fault_matched += 1
                matched_terminals += 1
            else:
                unexpected_faults += 1
                violations.append(f"fault_outcome_mismatch:{term}!={exp_term}")
        else:
            nominal_required += 1
            if term == exp_term and term not in _FAULT_TERMINALS:
                nominal_completed += 1
                matched_terminals += 1
            else:
                if term in _FAULT_TERMINALS:
                    unexpected_faults += 1
                violations.append(f"terminal_incomplete:{term}!={exp_term}")

    if admitted_actions == 0:
        violations.append("missing_denominator:terminal_completeness")

    nominal_ratio = _make_ratio(
        nominal_completed,
        nominal_required if nominal_required > 0 else (admitted_actions if admitted_actions > 0 else None),
        pass_when=lambda num, den: num == den,
    )

    terminal_completeness_metric = _make_ratio(
        matched_terminals,
        admitted_actions if admitted_actions > 0 else None,
        pass_when=lambda num, den: (
            num == den
            and unknown_terminals == 0
            and unexpected_faults == 0
            and (nominal_required == 0 or nominal_completed == nominal_required)
            and (fault_expected == 0 or fault_matched == fault_expected)
        ),
        extra={
            "nominal_completed": nominal_completed,
            "nominal_required": nominal_required,
            "nominal_completion": nominal_ratio,
            "fault_expected": fault_expected,
            "fault_matched": fault_matched,
            "unexpected_faults": unexpected_faults,
            "unknown_terminals": unknown_terminals,
            "crashes": crash_count,
            "timeouts": timeout_count,
        },
    )

    # Target kind coverage check (G-M7-3)
    observed_kinds = tuple(sorted({r.handle.kind for r in windows}))
    expected_kinds: set[str] = set()
    if require_all_target_kinds or trial_outcomes.get("heterogeneous_trial"):
        expected_kinds.update(SUPPORTED_TARGET_KINDS)
    if "expected_target_kinds" in trial_outcomes and trial_outcomes["expected_target_kinds"]:
        expected_kinds.update(trial_outcomes["expected_target_kinds"])
    for r in windows:
        if r.ops.get("heterogeneous_trial"):
            expected_kinds.update(SUPPORTED_TARGET_KINDS)
        if r.ops.get("expected_target_kinds"):
            expected_kinds.update(r.ops["expected_target_kinds"])
    missing_kinds = sorted(expected_kinds - set(observed_kinds))
    if missing_kinds:
        violations.append(f"missing_target_kinds:{','.join(missing_kinds)}")

    if trial_outcomes.get("prerequisite_blocked"):
        violations.append("prerequisite_blocked:AUSENTE")

    # Simulated throughput (actions / simulated elapsed time)
    sim_elapsed_ns = _require_int(
        trial_outcomes.get(
            "simulated_elapsed_ns",
            sum(_require_int(r.ops.get("simulated_elapsed_ns", r.ops.get("sim_elapsed_ns", 1_000_000)), "sim_elapsed_ns", min_value=0) for r in windows),
        ),
        "simulated_elapsed_ns",
        min_value=0,
    )
    simulated_throughput = _make_ratio(
        nominal_completed,
        sim_elapsed_ns if sim_elapsed_ns > 0 else None,
    )

    metrics = {
        "cross_window_bleed": cross_window_bleed_metric,
        "safety_violations": safety_violations_metric,
        "frame_exactness": frame_exactness_metric,
        "frame_drops": frame_drops_metric,
        "operation_totals": operation_totals_metric,
        "eligibility_stalls": eligibility_stalls_metric,
        "starvation": starvation_metric,
        "terminal_completeness": terminal_completeness_metric,
        "pooled_confusion": pooled_confusion_metric,
    }

    seal_value = expected_seal if n_windows else None
    pooled = {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "total": total_confusion,
        "support": pos_support if pos_support > 0 else None,
        "predicted_positive": pred_pos if pred_pos > 0 else None,
        "negative_support": neg_support if neg_support > 0 else None,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": precision_ratio,
        "recall": recall_ratio,
        "f1": f1_ratio,
        "accuracy": accuracy_ratio,
        "frames_total": n_windows,
        "frames_supported": supported_count,
        "frames_delivered": delivered_supported,
        "frames_dropped": dropped_count,
        "frames_exact": exact_delivered,
        "pixel_mismatches": pixel_mismatches,
        "ops": dict(sorted(pooled_ops.items())),
        "isolated_ops": dict(sorted(isolated_pooled_ops.items())),
        "stalls": total_stalls,
        "misses": fn,
        "silent_misses": silent_misses,
        "queue_maxima": queue_maxima,
        "quarantines": quarantines,
        "recovery_generations": recovery_generations,
        "drops": dropped_count,
        "crashes": crash_count,
        "timeouts": timeout_count,
        "faults": {
            "crashes": crash_count,
            "timeouts": timeout_count,
            "quarantines": quarantines,
            "matched_faults": fault_matched,
            "unexpected_faults": unexpected_faults,
            "trial_faults": list(trial_faults),
        },
        "seal": seal_value,
        "seal_manifest": manifest,
        "target_kinds": observed_kinds,
        "all_target_kinds_present": set(observed_kinds) == set(SUPPORTED_TARGET_KINDS),
        "simulated_throughput": simulated_throughput,
        "measured_host_speed": None,
        "native_capture": None,
        "G_T4": None,
        "G_T5": None,
        "absent_gates": {
            "measured_host_speed": AUSENTE,
            "native_capture": AUSENTE,
            "G-T4": AUSENTE,
            "G-T5": AUSENTE,
        },
    }
    if honesty_summary is not None:
        pooled["honesty"] = honesty_summary

    dedup_violations = tuple(sorted(set(violations)))
    all_metrics_pass = all(
        metrics[k].get("defined", False) is True
        and metrics[k].get("denominator") is not None
        and metrics[k].get("passed", False) is True
        for k in MANDATORY_METRIC_KEYS
    )
    overall_passed = bool(n_windows > 0 and all_metrics_pass and len(dedup_violations) == 0)

    return Scorecard(
        windows=windows,
        pooled=pooled,
        metrics=metrics,
        violations=dedup_violations,
        passed=overall_passed,
    )


def validate_scorecard(scorecard: Scorecard) -> Scorecard:
    """Validate that a Scorecard satisfies pooled confusion, ratio, and null-denominator invariants.

    Explicitly detects and rejects mutants that:
    - average per-window ratios instead of pooling integer confusion counts first,
    - replace null/None absent denominators or undefined ratio values with zero,
    - accept missing or mixed seals, or pass when mandatory denominators are absent.
    """
    if not isinstance(scorecard, Scorecard):
        raise ScorecardValidationError("E_SCORECARD_TYPE", "expected Scorecard instance")
    if set(scorecard.metrics.keys()) != set(MANDATORY_METRIC_KEYS):
        raise ScorecardValidationError("E_METRIC_KEYS", "Scorecard.metrics missing mandatory keys")

    windows = scorecard.windows
    manifest = scorecard.pooled.get("seal_manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") != "vqa.multiwin_manifest/v1":
        raise SealMismatchError("Scorecard is missing its canonical seal manifest")
    rebuilt_manifest = _score_manifest(windows, manifest.get("outcomes", {}), tuple(manifest.get("faults", ())))
    if manifest != rebuilt_manifest:
        raise SealMismatchError("Scorecard canonical manifest does not match row identities")
    expected_seal = _computed_evidence_seal(windows, manifest)
    if scorecard.pooled.get("seal") != (expected_seal if windows else None):
        raise SealMismatchError("Scorecard pooled seal does not match canonical manifest")
    for idx, row in enumerate(windows):
        if not isinstance(row.seal, str) or not hmac.compare_digest(row.seal, expected_seal):
            raise SealMismatchError(f"Scorecard row[{idx}] content seal mismatch")

    exp_tp = sum(w.tp for w in windows)
    exp_fp = sum(w.fp for w in windows)
    exp_fn = sum(w.fn for w in windows)
    exp_tn = sum(w.tn for w in windows)
    pc = scorecard.metrics["pooled_confusion"]
    if (
        scorecard.pooled.get("tp") != exp_tp
        or scorecard.pooled.get("fp") != exp_fp
        or scorecard.pooled.get("fn") != exp_fn
        or scorecard.pooled.get("tn") != exp_tn
        or pc.get("tp") != exp_tp
        or pc.get("fp") != exp_fp
        or pc.get("fn") != exp_fn
        or pc.get("tn") != exp_tn
    ):
        raise ScorecardValidationError("E_MUTANT_POOL", "pooled confusion does not equal elementwise sum")

    for rname, r_num, r_den in (
        ("precision", exp_tp, exp_tp + exp_fp),
        ("recall", exp_tp, exp_tp + exp_fn),
        ("f1", 2 * exp_tp, 2 * exp_tp + exp_fp + exp_fn),
        ("accuracy", exp_tp + exp_tn, exp_tp + exp_fp + exp_fn + exp_tn),
    ):
        r_obj = pc.get(rname)
        if not isinstance(r_obj, dict):
            raise ScorecardValidationError("E_MUTANT_RATIO", f"missing ratio dict for {rname}")
        if r_den == 0:
            if r_obj.get("denominator") is not None or r_obj.get("permille") is not None or r_obj.get("fraction") is not None:
                raise ScorecardValidationError(
                    "E_MUTANT_NULL_ZERO",
                    f"absent denominator for {rname} was replaced with zero instead of None",
                )
        else:
            if (
                r_obj.get("numerator") != r_num
                or r_obj.get("denominator") != r_den
                or r_obj.get("permille") != (r_num * 1000) // r_den
                or r_obj.get("fraction") != f"{r_num}/{r_den}"
            ):
                raise ScorecardValidationError(
                    "E_MUTANT_AVERAGE_RATIO",
                    f"{rname} does not match pooled integer ratio {r_num}/{r_den}",
                )

    for key in MANDATORY_METRIC_KEYS:
        m = scorecard.metrics[key]
        den = m.get("denominator")
        if den is not None and (type(den) is not int or den <= 0):
            raise ScorecardValidationError("E_MUTANT_NULL_ZERO", f"metric {key!r} has a noncanonical denominator")
        if den is None:
            if m.get("denominator") == 0 or m.get("permille") == 0 or m.get("fraction") in ("0/0", "0/1"):
                raise ScorecardValidationError(
                    "E_MUTANT_NULL_ZERO",
                    f"metric {key!r} replaced absent denominator with zero",
                )
            if m.get("passed") is True or scorecard.passed is True:
                raise ScorecardValidationError(
                    "E_MUTANT_GATE_PASS",
                    f"metric {key!r} with absent denominator cannot pass",
                )

    replayed = score_windows(TrialReport(
        rows=windows,
        outcomes=manifest["outcomes"],
        faults=tuple(manifest["faults"]),
    ))
    if canonical_bytes(scorecard.metrics) != canonical_bytes(replayed.metrics):
        raise ScorecardValidationError("E_MUTANT_METRIC", "mandatory metrics disagree with sealed production rows")
    if canonical_bytes(scorecard.pooled) != canonical_bytes(replayed.pooled):
        raise ScorecardValidationError("E_MUTANT_POOL", "pooled accounting disagrees with sealed production rows")
    expected_pass = bool(
        windows and not scorecard.violations
        and all(scorecard.metrics[k].get("defined") is True
                and type(scorecard.metrics[k].get("denominator")) is int
                and scorecard.metrics[k].get("denominator") > 0
                and scorecard.metrics[k].get("passed") is True
                for k in MANDATORY_METRIC_KEYS)
    )
    if scorecard.passed is not expected_pass:
        raise ScorecardValidationError("E_MUTANT_GATE_PASS", "overall pass flag contradicts mandatory gates")
    return scorecard


def score_sealed_window_scenes(scene_results: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Reuse sealed gui_synth aggregate only within its duplicate-scene contract."""
    return _sealed_aggregate(scene_results)


# ---------------------------------------------------------------------------
# Deterministic canonical serializer: canonical_bytes(report) -> bytes
# ---------------------------------------------------------------------------

def _is_diagnostic_timing_key(key: str) -> bool:
    if key in _DIAGNOSTIC_TIMING_KEYS:
        return True
    lower = key.lower()
    if lower.startswith("wall_") or lower.startswith("host_") or lower.startswith("diagnostic_"):
        return True
    if lower.endswith("_wall_ms") or lower.endswith("_wall_ns") or lower.endswith("_wall_s") or lower.endswith("_wall_seconds"):
        return True
    return False


def _canonicalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        raise ScorecardValidationError("E_CANONICAL_FLOAT", "float in integer-only canonical evidence")
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if isinstance(value, np.ndarray):
        if value.dtype.kind not in "iu":
            code = "E_CANONICAL_FLOAT" if value.dtype.kind == "f" else "E_CANONICAL_TYPE"
            raise ScorecardValidationError(code, "canonical arrays require integer dtype")
        return {
            "__ndarray__": True,
            "dtype": str(value.dtype),
            "shape": list(value.shape),
            "sha256": hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest(),
        }
    if isinstance(value, WindowEvidence):
        _validate_handle_like(value.handle)
        _require_nonempty_str(value.scene, "WindowEvidence.scene", error_cls=WindowIdentityError)
        for name in ("epoch", "frame_seq", "tp", "fp", "fn", "tn"):
            _require_int(getattr(value, name), f"WindowEvidence.{name}", min_value=0)
        _validate_ops_evidence(value.ops)
        return {
            "handle": _canonicalize_value(value.handle),
            "scene": value.scene,
            "epoch": value.epoch,
            "frame_seq": value.frame_seq,
            "tp": value.tp,
            "fp": value.fp,
            "fn": value.fn,
            "tn": value.tn,
            "ops": _canonicalize_value(value.ops),
            "frame_supported": bool(value.frame_supported),
            "dropped": bool(value.dropped),
            "exact": value.exact,
            "terminal": value.terminal,
            "expected_terminal": value.expected_terminal,
            "seal": value.seal,
            "violations": [_canonicalize_value(v) for v in value.violations],
        }
    if isinstance(value, TrialReport):
        sorted_rows = sorted(value.rows, key=_window_identity_key)
        return {
            "schema": "vqa.multiwin_trial_report/v1",
            "rows": [_canonicalize_value(r) for r in sorted_rows],
            "outcomes": _canonicalize_value(value.outcomes),
            "faults": [_canonicalize_value(f) for f in value.faults],
        }
    if isinstance(value, Scorecard):
        sorted_windows = sorted(value.windows, key=_window_identity_key)
        return {
            "schema": "vqa.multiwin_scorecard/v1",
            "windows": [_canonicalize_value(w) for w in sorted_windows],
            "pooled": _canonicalize_value(value.pooled),
            "metrics": _canonicalize_value(value.metrics),
            "violations": [_canonicalize_value(v) for v in value.violations],
            "passed": bool(value.passed),
        }
    if is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in fields(value):
            if _is_diagnostic_timing_key(f.name):
                continue
            out[f.name] = _canonicalize_value(getattr(value, f.name))
        return out
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ScorecardValidationError("E_CANONICAL_TYPE", "canonical object keys must be strings")
        cleaned: dict[str, Any] = {}
        for k, v in sorted(value.items()):
            if _is_diagnostic_timing_key(k):
                continue
            cleaned[k] = _canonicalize_value(v)
        return cleaned
    if isinstance(value, (list, tuple)):
        items = list(value)
        if items and all(isinstance(x, WindowEvidence) for x in items):
            items = sorted(items, key=_window_identity_key)
        return [_canonicalize_value(x) for x in items]
    if isinstance(value, (set, frozenset)):
        raise ScorecardValidationError("E_CANONICAL_TYPE", "unordered sets are not canonical evidence")
    raise ScorecardValidationError("E_CANONICAL_TYPE", f"unsupported canonical type: {type(value).__name__}")


def canonical_bytes(report: Any) -> bytes:
    """Return deterministic UTF-8 canonical JSON bytes excluding wall-clock timing."""
    payload = _canonicalize_value(report)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Offline trial runner: run_trial(scenario, *, seed, clock, adapters) -> TrialReport
# ---------------------------------------------------------------------------

def _resolve_adapter(adapters: dict[Any, Any], handle: Handle) -> tuple[str, Any]:
    """Look up adapter for handle while keeping Godot and CDP keys separate."""
    ckey = canonical_handle_key(handle)
    full_key = (handle.kind, handle.canonical_id, handle.generation)
    for candidate in (ckey, full_key, handle):
        if candidate in adapters:
            return ckey, adapters[candidate]
    # Read legacy adapter keys only for the original generation; never alias an
    # ABA incarnation to an older registration.
    if handle.generation == 1:
        for candidate in ((handle.kind, handle.canonical_id), f"{handle.kind}:{handle.canonical_id}"):
            if candidate in adapters:
                return ckey, adapters[candidate]
    return ckey, None


def _target_seed(seed: int, handle_key: str) -> int:
    digest = hashlib.sha256(f"{seed}:{handle_key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little")


def _coerce_handle(target: Any) -> Handle:
    if isinstance(target, Handle):
        return target
    if isinstance(target, TargetRecord):
        return target.handle
    if isinstance(target, dict):
        if "handle" in target:
            return _coerce_handle(target["handle"])
        return Handle(target["kind"], target["canonical_id"], target.get("generation", 1))
    if hasattr(target, "handle"):
        return _coerce_handle(target.handle)
    return Handle(target.kind, target.canonical_id, getattr(target, "generation", 1))


def run_trial(
    scenario: Scenario,
    *,
    seed: int,
    clock: FakeClock,
    adapters: dict[Any, Any],
) -> TrialReport:
    """Run an offline multi-window scenario with deterministic fake time and isolated target state."""
    if not isinstance(scenario, Scenario):
        scenario = Scenario(
            name=getattr(scenario, "name"),
            targets=tuple(getattr(scenario, "targets", ())),
            events=tuple(getattr(scenario, "events", ())),
            expected_outcomes=dict(getattr(scenario, "expected_outcomes", {})),
        )
    seed_val = _require_int(seed, "seed")
    if clock is None or not callable(clock) or not hasattr(clock, "advance"):
        raise ScorecardValidationError("E_CLOCK", "clock must be a FakeClock with __call__ and advance()")
    if not isinstance(adapters, dict):
        raise ScorecardValidationError("E_ADAPTERS", "adapters must be a dict mapping canonical handle keys to adapters")

    start_ns = clock()

    # Check prerequisite gate vector if specified in scenario.expected_outcomes
    prereqs = scenario.expected_outcomes.get("prerequisites")
    if prereqs is not None:
        missing_prereqs = [stage for stage in PREREQUISITE_STAGE_ORDER if not prereqs.get(stage, True)]
        if missing_prereqs:
            return TrialReport(
                rows=(),
                outcomes={
                    "scenario": scenario.name,
                    "seed": seed_val,
                    "status": AUSENTE,
                    "prerequisite_blocked": True,
                    "missing_prerequisites": tuple(missing_prereqs),
                    "simulated_elapsed_ns": 0,
                    "measured_host_speed": AUSENTE,
                    "native_capture": AUSENTE,
                    "G-T4": AUSENTE,
                    "G-T5": AUSENTE,
                },
                faults=tuple({"type": "PREREQUISITE_ABSENT", "stage": s, "status": AUSENTE} for s in missing_prereqs),
            )

    # Canonical target sorting & independent per-target reducer / RNG reset (INV-M7-1, G-M8-2)
    target_handles = [_coerce_handle(t) for t in scenario.targets]
    target_states: dict[str, dict[str, Any]] = {}
    adapter_map: dict[str, Any] = {}
    registered_incarnations: dict[tuple[str, str], int] = {}

    for h in sorted(target_handles, key=canonical_target_sort_key):
        ident = (h.kind, h.canonical_id)
        if ident in registered_incarnations:
            raise WindowIdentityError(f"duplicate or mixed target registrations for {ident!r}")
        registered_incarnations[ident] = h.generation
        ckey, adapter = _resolve_adapter(adapters, h)
        t_seed = _target_seed(seed_val, ckey)
        rng = np.random.default_rng(t_seed)
        if adapter is not None and hasattr(adapter, "reset"):
            try:
                adapter.reset(seed=t_seed)
            except TypeError:
                adapter.reset()
        adapter_map[ckey] = adapter
        target_states[ckey] = {
            "handle": h,
            "canonical_key": ckey,
            "actuation_resource": canonical_actuation_resource(h),
            "rng": rng,
            "seed": t_seed,
            "epoch": 0,
            "next_frame_seq": 0,
            "cursor": (0, 0),
            "claims_history": [],
            "ops_accum": {"decode": 0, "describe": 0, "change": 0, "fl": 0, "gm": 0},
            "fenced": False,
            "quarantined": False,
        }

    rows: list[WindowEvidence] = []
    faults: list[dict[str, Any]] = []
    outcomes: dict[str, Any] = {}
    cursors_by_key: dict[str, tuple[int, int]] = {}

    for ev_idx, event in enumerate(scenario.events):
        if isinstance(event, WindowEvidence):
            if event.seal:
                raise SealMismatchError("direct trial events must be unsealed; content seals are generated after the run")
            clock.advance(1_000_000)
            ckey = canonical_handle_key(event.handle)
            if ckey not in target_states:
                raise WindowIdentityError(f"unregistered trial handle {ckey!r}")
            rows.append(event)
            outcomes[ckey] = event.terminal
            continue

        ev_dict = dict(event) if isinstance(event, dict) else {
            f.name: getattr(event, f.name) for f in fields(event)
        } if is_dataclass(event) else dict(event)

        delta_ns = _require_int(ev_dict.get("delta_ns", ev_dict.get("duration_ns", 1_000_000)), "event.delta_ns", min_value=0)
        clock.advance(delta_ns)

        h = _coerce_handle(ev_dict.get("handle", ev_dict.get("target", target_handles[ev_idx % len(target_handles)] if target_handles else None)))
        ckey = canonical_handle_key(h)
        if ckey not in target_states:
            raise WindowIdentityError(f"event references unregistered incarnation {ckey!r}")

        tstate = target_states[ckey]
        adapter = adapter_map.get(ckey)

        old_epoch = tstate["epoch"]
        geometry_changed = bool(ev_dict.get("navigate") or ev_dict.get("resize"))
        if "epoch" in ev_dict:
            new_epoch = _require_int(ev_dict["epoch"], "event.epoch", min_value=0, error_cls=FrameSequenceError)
            if new_epoch < old_epoch or (geometry_changed and new_epoch <= old_epoch):
                raise FrameSequenceError("geometry change requires an explicit strictly newer epoch")
            if new_epoch > old_epoch:
                tstate["next_frame_seq"] = 0
            tstate["epoch"] = new_epoch
        elif geometry_changed:
            raise FrameSequenceError("geometry change requires an explicit new epoch")

        epoch = tstate["epoch"]
        next_sequence = tstate["next_frame_seq"]
        if "frame_seq" in ev_dict:
            frame_seq = _require_int(ev_dict["frame_seq"], "event.frame_seq", min_value=0, error_cls=FrameSequenceError)
            if frame_seq < next_sequence:
                raise FrameSequenceError("event.frame_seq must strictly increase in its epoch")
        else:
            frame_seq = next_sequence
        tstate["next_frame_seq"] = frame_seq + 1

        # Cursor updates remain strictly isolated per canonical key (G-M8-2)
        if "cursor" in ev_dict and ev_dict["cursor"] is not None:
            cx, cy = ev_dict["cursor"]
            tstate["cursor"] = (
                _require_int(cx, "cursor.x", error_cls=FrameSequenceError),
                _require_int(cy, "cursor.y", error_cls=FrameSequenceError),
            )
        else:
            dx = _require_int(ev_dict.get("dx", 1 + (tstate["seed"] % 5)), "cursor.dx",
                              error_cls=FrameSequenceError)
            dy = _require_int(ev_dict.get("dy", 1 + ((tstate["seed"] >> 4) % 5)), "cursor.dy",
                              error_cls=FrameSequenceError)
            tstate["cursor"] = (tstate["cursor"][0] + dx, tstate["cursor"][1] + dy)
        cursors_by_key[ckey] = tstate["cursor"]

        adapter_out: dict[str, Any] = {}
        if adapter is not None:
            if hasattr(adapter, "step"):
                res = adapter.step(ev_dict, clock=clock, rng=tstate["rng"])
                if isinstance(res, dict):
                    adapter_out = dict(res)
            elif callable(adapter):
                res = adapter(ev_dict, clock=clock, rng=tstate["rng"])
                if isinstance(res, dict):
                    adapter_out = dict(res)

        merged = {**adapter_out, **ev_dict}
        scene = merged.get("scene", f"{scenario.name}:{h.kind}")
        if merged.get("seal") not in (None, ""):
            raise SealMismatchError("trial row seal is derived from content; custom row seal refused")
        seal = ""

        expected_term = merged.get(
            "expected_terminal",
            scenario.expected_outcomes.get(
                ckey,
                scenario.expected_outcomes.get(
                    h.canonical_id,
                    scenario.expected_outcomes.get("default_terminal", "OK"),
                ),
            ),
        )
        fault_kind = merged.get("fault")
        row_violations = list(merged.get("violations", ()))

        if fault_kind is not None:
            f_upper = str(fault_kind).strip().upper()
            terminal = str(merged.get("terminal", f_upper))
            if f_upper in ("CRASH", "TIMEOUT", "QUARANTINED", "FENCED"):
                if f_upper == "CRASH":
                    tstate["fenced"] = True
                elif f_upper in ("TIMEOUT", "QUARANTINED"):
                    tstate["quarantined"] = True
                faults.append({
                    "handle": ckey,
                    "epoch": epoch,
                    "frame_seq": frame_seq,
                    "fault": f_upper,
                    "terminal": terminal,
                    "expected_terminal": expected_term,
                    "at_ns": clock(),
                })
        else:
            terminal = merged.get("terminal", expected_term)

        if merged.get("inject_bleed") or merged.get("cross_window_bleed"):
            row_violations.append("cross_window_bleed")

        tp = _require_int(merged.get("tp", 10 + (tstate["seed"] % 7)), "tp", min_value=0)
        fp = _require_int(merged.get("fp", 1), "fp", min_value=0)
        fn = _require_int(merged.get("fn", 1), "fn", min_value=0)
        tn = _require_int(merged.get("tn", 20 + (tstate["seed"] % 11)), "tn", min_value=0)

        frame_supported = merged.get("frame_supported", True)
        dropped = merged.get("dropped", False)
        if "exact" in merged:
            exact = merged["exact"]
        else:
            exact = None if (dropped or not frame_supported) else True

        default_ops = {
            "decode": 1,
            "describe": 2,
            "change": 2,
            "fl": 3 + (tstate["seed"] % 3),
            "gm": 2 + (tstate["seed"] % 2),
        }
        raw_ops = merged.get("ops", default_ops)
        if not isinstance(raw_ops, dict):
            raise ScorecardValidationError("E_OPS_TYPE", "event.ops must be a dict")
        raw_ops = dict(raw_ops)
        # Isolated measurements must come from a separate, caller-supplied replay.
        # Never satisfy the comparator by copying the interleaved counters.
        if merged.get("inject_bleed") or merged.get("cross_window_bleed"):
            raw_ops["cross_window_bleed"] = int(raw_ops.get("cross_window_bleed", 0)) + 1

        row = WindowEvidence(
            handle=h,
            scene=scene,
            epoch=epoch,
            frame_seq=frame_seq,
            tp=tp,
            fp=fp,
            fn=fn,
            tn=tn,
            ops=raw_ops,
            frame_supported=frame_supported,
            dropped=dropped,
            exact=exact,
            terminal=terminal,
            expected_terminal=expected_term,
            seal=seal,
            violations=tuple(row_violations),
        )
        rows.append(row)
        outcomes[ckey] = terminal

    elapsed_sim_ns = max(0, clock() - start_ns)
    report_outcomes = {
        "scenario": scenario.name,
        "seed": seed_val,
        "terminal_by_target": outcomes,
        "cursors_by_target": cursors_by_key,
        "actuation_resources": {
            ckey: st["actuation_resource"] for ckey, st in sorted(target_states.items())
        },
        "target_kinds": tuple(sorted({h.kind for h in target_handles} | {r.handle.kind for r in rows})),
        "expected_target_kinds": tuple(sorted({h.kind for h in target_handles})),
        "simulated_elapsed_ns": elapsed_sim_ns,
        "measured_host_speed": AUSENTE,
        "native_capture": AUSENTE,
        "G-T4": AUSENTE,
        "G-T5": AUSENTE,
    }
    return seal_trial(TrialReport(rows=tuple(rows), outcomes=report_outcomes, faults=tuple(faults)))


__all__ = [
    "AUSENTE",
    "SUPPORTED_TARGET_KINDS",
    "MANDATORY_METRIC_KEYS",
    "METADATA_LOCK_ORDER",
    "PREREQUISITE_STAGE_ORDER",
    "REQUIRED_GATE_VECTOR",
    "ScorecardValidationError",
    "WindowIdentityError",
    "FrameSequenceError",
    "SealMismatchError",
    "TerminalValidationError",
    "PrerequisiteAbsentError",
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
    "Scenario",
    "WindowEvidence",
    "TrialReport",
    "Scorecard",
    "FakeClock",
    "canonical_handle_key",
    "canonical_target_sort_key",
    "canonical_actuation_resource",
    "validate_lock_order",
    "score_windows",
    "validate_scorecard",
    "score_sealed_window_scenes",
    "canonical_bytes",
    "seal_trial",
    "run_trial",
]
