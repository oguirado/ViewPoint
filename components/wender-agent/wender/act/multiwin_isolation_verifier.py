# @module {"id":"wa.act.multiwin_isolation_verifier","layer":2,"owner":"ACT","deps":["wa.act.cdp_executor","wa.act.focus_policy","wa.act.godot_executor","wa.perceive.session_registry","wa.state.schema"],"book":"HB-WA-s2-act"}
"""@module wa.act.multiwin_isolation_verifier - Action-isolation verifier for CDP and Godot executors.

Verifies issued-lease metadata, focus ownership, geometry epoch, and pre-dispatch frame
digest for CDP targets; verifies trusted-context mandates, command seq/tick/basis binding,
and single-use confirmation proposals for Godot T2 targets. Separates authorization,
dispatch, and verified effect so stale or foreign bindings fail closed before any fake
actor or sink is invoked, emitting deterministic canonical JSON audit records that
identify the target and violated predicate.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import hashlib
import importlib
import re
import threading
import time
from typing import Any, Callable

import numpy as np

from wender.act.cdp_executor import ActionError, _verify_expect, run_ax_action
from wender.act.focus_policy import (
    ActorLease,
    FocusClass,
    FocusLease,
    FocusPolicy,
    FocusRefused,
    Handle,
    Observation,
    _handle_key,
    _same_handle,
    _valid_integer,
    classify,
)
from wender.act.godot_executor import (
    CONFIRM_BINDING_KEYS,
    CURRENT_BOUNDARY_COMMANDS,
    FABRICATED_CONSENT_KEYS,
    IRREVERSIBLE_COMMANDS,
    OWNER_RESERVED_COMMANDS,
    REASON_CMD_MISMATCH,
    REASON_MANDATE_REQUIRED,
    REASON_OK,
    STATUS_ADMITTED,
    STATUS_REJECTED,
    GodotExecutor,
    map_ui_action,
)
from wender.perceive import ax
from wender.perceive.godot import (
    FORBIDDEN_AUTHORITY_KEYS,
    INBOX_RECORD_KEYS,
    REJECTED_CANONICAL_ALIASES,
)
from wender.state import schema

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")

# @anchor:wa.act.multiwin_isolation_verifier.STAGES
STAGES = ("authorization", "dispatch", "verified_effect")

# @anchor:wa.act.multiwin_isolation_verifier.CDP_PREDICATES
CDP_PREDICATES = (
    "handle_identity",
    "lease_ownership",
    "lease_expiry",
    "lease_generation",
    "focus_ownership",
    "epoch_binding",
    "frame_digest",
    "observation_freshness",
    "writer_fence",
    "target_match",
    "verified_effect",
)

# @anchor:wa.act.multiwin_isolation_verifier.GODOT_PREDICATES
GODOT_PREDICATES = (
    "inbox_record_shape",
    "trusted_mandate",
    "main_save_consent",
    "sequence_binding",
    "tick_window",
    "basis_binding",
    "proposal_single_use",
    "confirmation_binding",
    "schema_admission",
    "verified_effect",
)


# @anchor:wa.act.multiwin_isolation_verifier.IsolationViolationError
class IsolationViolationError(ValueError):
    """Raised when an action-isolation predicate fails in fail-closed mode."""

    def __init__(self, target: str, predicate: str, stage: str, detail: str, audit: dict[str, Any]) -> None:
        super().__init__(f"isolation violation on target={target!r} predicate={predicate!r} stage={stage!r}: {detail}")
        self.target = target
        self.predicate = predicate
        self.stage = stage
        self.detail = detail
        self.audit = audit


# @anchor:wa.act.multiwin_isolation_verifier.SiblingDependencyAbsentError
class SiblingDependencyAbsentError(ImportError):
    """Raised when a non-merged sibling dependency is requested lazily."""


# @anchor:wa.act.multiwin_isolation_verifier.require_sibling_dependency
def require_sibling_dependency(module_name: str) -> Any:
    """Lazy import guard blocking on unmerged sibling modules instead of fabricating APIs."""
    if not isinstance(module_name, str) or not module_name.strip():
        raise SiblingDependencyAbsentError("module_name must be a non-empty string prerequisite AUSENTE")
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise SiblingDependencyAbsentError(f"sibling dependency {module_name!r} prerequisite AUSENTE") from exc


def _is_exact_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _canonical_cdp_target_id(handle: Any, fallback_session_id: Any = None) -> str:
    if handle is not None:
        kind = getattr(handle, "kind", None)
        cid = getattr(handle, "canonical_id", None)
        gen = getattr(handle, "generation", None)
        if isinstance(kind, str) and kind and isinstance(cid, str) and cid and _is_exact_int(gen):
            return f"{kind}:{cid}@{gen}"
        if isinstance(cid, str) and cid:
            return cid
    if isinstance(fallback_session_id, str) and fallback_session_id:
        return f"session:{fallback_session_id}"
    return "cdp:unknown_target"


def _canonical_godot_target_id(record: Any, trusted_context: dict[str, Any] | None = None) -> str:
    if isinstance(record, dict):
        args = record.get("args")
        if isinstance(args, dict):
            for key in ("target_id", "company_id", "person_id", "property_id", "entity_id"):
                val = args.get(key)
                if isinstance(val, str) and val.strip():
                    return val
            conf = args.get("confirm")
            if isinstance(conf, dict):
                c_tid = conf.get("target_id")
                if isinstance(c_tid, str) and c_tid.strip():
                    return c_tid
        cmd = record.get("cmd")
        if isinstance(cmd, str) and cmd.strip():
            return f"cmd:{cmd}"
    if isinstance(trusted_context, dict):
        slot = trusted_context.get("slot_kind", trusted_context.get("slot"))
        if isinstance(slot, str) and slot.strip():
            return f"slot:{slot}"
    return "godot:unknown_target"


# @anchor:wa.act.multiwin_isolation_verifier.build_audit_record
def build_audit_record(
    *,
    surface: str,
    target: str,
    stage: str,
    violated_predicate: str | None,
    authorized: int,
    dispatched: int,
    verified_effect: int,
    actor_invocations: int,
    detail: str,
    pre_digest: str | None = None,
    post_digest: str | None = None,
    epoch: int | None = None,
    seq: int | None = None,
    tick: int | None = None,
    attempts: int = 0,
    proposal_modal_id: str | None = None,
) -> dict[str, Any]:
    """Construct a deterministic canonical-JSON-validated audit record."""
    if surface not in ("cdp", "godot"):
        raise ValueError(f"unsupported surface: {surface!r}")
    if stage not in STAGES:
        raise ValueError(f"unsupported stage: {stage!r}")
    if not isinstance(target, str) or not target:
        raise ValueError("target must be a non-empty string")
    if violated_predicate is not None and not isinstance(violated_predicate, str):
        raise ValueError("violated_predicate must be str or None")
    for flag_name, flag_val in (
        ("authorized", authorized),
        ("dispatched", dispatched),
        ("verified_effect", verified_effect),
    ):
        if not _is_exact_int(flag_val) or flag_val not in (0, 1):
            raise ValueError(f"{flag_name} must be 0 or 1")
    if not _is_exact_int(actor_invocations) or actor_invocations < 0:
        raise ValueError("actor_invocations must be a non-negative integer")
    if not _is_exact_int(attempts) or attempts < 0:
        raise ValueError("attempts must be a non-negative integer")

    ok = 1 if (violated_predicate is None and authorized == 1 and dispatched == 1 and verified_effect == 1) else 0
    record = {
        "ok": ok,
        "surface": surface,
        "target": target,
        "stage": stage,
        "violated_predicate": violated_predicate,
        "authorized": authorized,
        "dispatched": dispatched,
        "verified_effect": verified_effect,
        "actor_invocations": actor_invocations,
        "epoch": epoch,
        "seq": seq,
        "tick": tick,
        "proposal_modal_id": proposal_modal_id,
        "detail": str(detail),
        "stages": {
            "authorization": {
                "ok": authorized,
                "predicate": violated_predicate if stage == "authorization" else None,
            },
            "dispatch": {
                "ok": dispatched,
                "attempts": attempts,
                "predicate": violated_predicate if stage == "dispatch" else None,
            },
            "verified_effect": {
                "ok": verified_effect,
                "pre_digest": pre_digest,
                "post_digest": post_digest,
                "predicate": violated_predicate if stage == "verified_effect" else None,
            },
        },
    }
    schema.dumps(record)
    return record


# @anchor:wa.act.multiwin_isolation_verifier.format_audit_record
def format_audit_record(audit: dict[str, Any]) -> str:
    """Serialize an audit record into canonical deterministic JSON."""
    if not isinstance(audit, dict):
        raise ValueError("audit record must be a dict")
    if not isinstance(audit.get("target"), str) or not audit["target"]:
        raise ValueError("audit record missing non-empty target")
    if audit.get("stage") not in STAGES:
        raise ValueError("audit record has invalid stage")
    return schema.dumps(audit)


# @anchor:wa.act.multiwin_isolation_verifier.assert_audit_target_predicate
def assert_audit_target_predicate(
    audit: dict[str, Any],
    *,
    expected_target: str,
    expected_predicate: str | None,
) -> None:
    """Assert that a deterministic audit record matches expected target and violated predicate."""
    assert audit.get("target") == expected_target and audit.get("violated_predicate") == expected_predicate, (
        f"expected target={expected_target!r} predicate={expected_predicate!r}, "
        f"got target={audit.get('target')!r} predicate={audit.get('violated_predicate')!r}"
    )


# @anchor:wa.act.multiwin_isolation_verifier.StructuralTargetRecord
@dataclass(frozen=True)
class StructuralTargetRecord:
    """Explicit structural target record collaborator for FocusPolicy and run_ax_action."""

    handle: Handle
    epoch: int = 2
    session_id: str = "s1"
    closed: bool = False
    owned: bool = True


# @anchor:wa.act.multiwin_isolation_verifier.StructuralSessionRegistry
class StructuralSessionRegistry:
    """Explicitly labelled structural registry collaborator exposing get and validate_observation."""

    def __init__(self, records: list[StructuralTargetRecord] | tuple[StructuralTargetRecord, ...] = ()) -> None:
        self._lock = threading.RLock()
        self.records: dict[str, StructuralTargetRecord] = {r.handle.canonical_id: r for r in records}
        self.current: dict[str, Observation] = {}

    def register_record(self, record: StructuralTargetRecord) -> None:
        with self._lock:
            self.records[record.handle.canonical_id] = record

    def set_current_observation(self, observation: Observation) -> None:
        with self._lock:
            self.current[observation.handle.canonical_id] = observation

    def clear_observations(self) -> None:
        with self._lock:
            self.current.clear()

    def advance_epoch(self, handle: Handle) -> int:
        with self._lock:
            rec = self.records.get(handle.canonical_id)
            if rec is None or rec.closed or not _same_handle(rec.handle, handle):
                raise ActionError("cannot advance epoch on missing, closed or stale target")
            updated = replace(rec, epoch=rec.epoch + 1)
            self.records[handle.canonical_id] = updated
            return updated.epoch

    def close_target(self, handle: Handle) -> None:
        with self._lock:
            rec = self.records.get(handle.canonical_id)
            if rec is not None and _same_handle(rec.handle, handle):
                self.records[handle.canonical_id] = replace(rec, closed=True)
                self.current.pop(handle.canonical_id, None)

    def get(self, handle: Handle) -> StructuralTargetRecord | None:
        with self._lock:
            cid = getattr(handle, "canonical_id", None)
            if not isinstance(cid, str):
                return None
            return self.records.get(cid)

    def validate_observation(self, observation: Observation) -> bool:
        with self._lock:
            handle = getattr(observation, "handle", None)
            cid = getattr(handle, "canonical_id", None)
            if not isinstance(cid, str):
                return False
            return self.current.get(cid) is observation


# @anchor:wa.act.multiwin_isolation_verifier.StructuralLockOwnership
class StructuralLockOwnership:
    """Explicitly labelled structural lock collaborator exposing owns(lease, *, now_ns)."""

    def __init__(self, leases: list[ActorLease] | tuple[ActorLease, ...] = ()) -> None:
        self._lock = threading.RLock()
        self.issued: dict[bytes, ActorLease] = {lease.nonce: lease for lease in leases}

    def issue(self, lease: ActorLease) -> None:
        with self._lock:
            self.issued[lease.nonce] = lease

    def revoke(self, nonce: bytes) -> None:
        with self._lock:
            self.issued.pop(nonce, None)

    def owns(self, lease: ActorLease, *, now_ns: int) -> bool:
        with self._lock:
            nonce = getattr(lease, "nonce", None)
            if not isinstance(nonce, bytes):
                return False
            current = self.issued.get(nonce)
            return current is lease and _is_exact_int(now_ns) and now_ns < lease.expires_ns


# @anchor:wa.act.multiwin_isolation_verifier.StructuralWriterFence
class StructuralWriterFence:
    """Explicitly labelled structural writer fence collaborator exposing acquire, release, quarantine."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.busy: bool = False
        self.quarantined: bool = False
        self.active_nonce: bytes | None = None
        self.acquisitions: int = 0
        self.releases: int = 0
        self.quarantines: int = 0
        self.consumed_nonces: set[bytes] = set()

    def acquire(self, lease: ActorLease) -> bool:
        with self._lock:
            nonce = getattr(lease, "nonce", None)
            if self.busy or self.quarantined or not isinstance(nonce, bytes) or nonce in self.consumed_nonces:
                return False
            self.busy = True
            self.active_nonce = nonce
            self.acquisitions += 1
            return True

    def release(self, lease: ActorLease) -> None:
        with self._lock:
            if not self.busy:
                raise AssertionError("release without acquisition")
            nonce = getattr(lease, "nonce", None)
            if isinstance(nonce, bytes):
                self.consumed_nonces.add(nonce)
            self.busy = False
            self.active_nonce = None
            self.releases += 1

    def quarantine(self, lease: ActorLease) -> None:
        with self._lock:
            nonce = getattr(lease, "nonce", None)
            if isinstance(nonce, bytes):
                self.consumed_nonces.add(nonce)
            self.busy = True
            self.quarantined = True
            self.active_nonce = nonce
            self.quarantines += 1


def _verify_frame_digest(frame: Any, expected_frame_digest: str | None) -> tuple[bool, str]:
    digest = getattr(frame, "digest", None)
    if not isinstance(digest, str) or not _HEX64_RE.fullmatch(digest):
        return False, "frame digest missing or not 64 lowercase hex characters"
    if expected_frame_digest is not None:
        if not isinstance(expected_frame_digest, str) or digest != expected_frame_digest:
            return False, "pre-dispatch frame digest does not match expected digest"
    rgb = getattr(frame, "rgb", None)
    if isinstance(rgb, np.ndarray):
        if rgb.dtype != np.dtype("uint8") or rgb.ndim != 3 or rgb.shape[2] != 3:
            return False, "frame RGB ndarray must be H x W x 3 uint8"
        width = getattr(frame, "width", None)
        height = getattr(frame, "height", None)
        if not _is_exact_int(width) or not _is_exact_int(height) or (height, width) != rgb.shape[:2]:
            return False, "frame RGB dimensions do not match frame width/height"
        expected_pixels = height * width
        if int(rgb.shape[0] * rgb.shape[1]) != expected_pixels:
            return False, "frame RGB pixel count mismatch"
        computed = hashlib.sha256(rgb.tobytes(order="C")).hexdigest()
        if computed != digest:
            return False, "frame RGB content SHA-256 differs from frame.digest"
    return True, digest


# @anchor:wa.act.multiwin_isolation_verifier.authorize_cdp_action
def authorize_cdp_action(
    *,
    session_id: str,
    nodes: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    action: dict[str, Any],
    lease: Any,
    focus: Any,
    observation: Any,
    registry: Any,
    focus_policy: Any,
    writer_fence: Any,
    locks: Any = None,
    clock: Callable[[], int] | None = None,
    expected_frame_digest: str | None = None,
    allow_coordinates: bool = False,
) -> tuple[bool, str | None, str, str]:
    """Pre-dispatch authorization gate over lease, focus, epoch, frame digest, and target binding.

    Returns (ok, violated_predicate, target_id, detail). Never invokes CDP transport.
    """
    handle = getattr(lease, "handle", None)
    target_id = _canonical_cdp_target_id(handle, session_id)

    if any(v is None for v in (lease, focus, observation, registry, focus_policy, writer_fence)):
        return False, "lease_ownership", target_id, "missing mandatory authority collaborator"

    try:
        key = _handle_key(handle)
    except FocusRefused as exc:
        return False, "handle_identity", target_id, str(exc)

    if key[0] != "cdp_tab":
        return False, "handle_identity", target_id, "CDP isolation requires a cdp_tab handle"

    now_ns = time.monotonic_ns() if clock is None else clock()
    if not _valid_integer(now_ns):
        return False, "lease_expiry", target_id, "clock returned non-integer monotonic timestamp"

    owner = getattr(lease, "owner", None)
    nonce = getattr(lease, "nonce", None)
    if not isinstance(owner, str) or not owner.strip() or not isinstance(nonce, bytes) or not nonce:
        return False, "lease_ownership", target_id, "malformed actor lease owner or nonce"

    lease_gen = getattr(lease, "generation", None)
    if not _valid_integer(lease_gen) or lease_gen <= 0 or lease_gen != key[2]:
        return False, "lease_generation", target_id, "actor lease generation differs from target handle"

    expires_ns = getattr(lease, "expires_ns", None)
    if not _valid_integer(expires_ns) or now_ns >= expires_ns:
        return False, "lease_expiry", target_id, "actor lease is expired at pre-dispatch check"

    if locks is not None:
        owns_fn = getattr(locks, "owns", None)
        if not callable(owns_fn) or owns_fn(lease, now_ns=now_ns) is not True:
            return False, "lease_ownership", target_id, "actor lease nonce is not owned in lock provider"

    get_fn = getattr(registry, "get", None)
    if not callable(get_fn):
        return False, "handle_identity", target_id, "registry.get prerequisite AUSENTE"
    record = get_fn(handle)
    if record is None or getattr(record, "closed", True):
        return False, "handle_identity", target_id, "target handle is unknown or closed in registry"
    rec_handle = getattr(record, "handle", None)
    if not _same_handle(rec_handle, handle):
        return False, "lease_generation", target_id, "registry target handle generation superseded"
    if getattr(record, "session_id", None) is not None and record.session_id != session_id:
        return False, "handle_identity", target_id, "CDP session_id does not own canonical target"

    rec_epoch = getattr(record, "epoch", None)
    obs_epoch = getattr(observation, "epoch", None)
    focus_epoch = getattr(focus, "epoch", None)
    if not _valid_integer(rec_epoch) or rec_epoch < 0 or obs_epoch != rec_epoch or focus_epoch != rec_epoch:
        return False, "epoch_binding", target_id, "target epoch mismatch across registry, observation or focus grant"

    valid_obs_fn = getattr(registry, "validate_observation", None)
    if not callable(valid_obs_fn) or valid_obs_fn(observation) is not True:
        return False, "observation_freshness", target_id, "observation is not current in registry"

    obs_handle = getattr(observation, "handle", None)
    frame = getattr(observation, "frame", None)
    frame_seq = getattr(observation, "frame_seq", None)
    if (
        not _same_handle(obs_handle, handle)
        or frame is None
        or not _same_handle(getattr(frame, "handle", None), handle)
    ):
        return False, "handle_identity", target_id, "observation or frame handle aliases a foreign target"

    if getattr(frame, "epoch", None) != rec_epoch:
        return False, "epoch_binding", target_id, "frame epoch differs from target epoch"

    if (
        not _valid_integer(frame_seq)
        or frame_seq < 0
        or getattr(frame, "frame_seq", None) != frame_seq
        or not _valid_integer(getattr(observation, "dpr_ppm", None))
        or observation.dpr_ppm <= 0
        or not _valid_integer(getattr(frame, "width", None))
        or frame.width <= 0
        or not _valid_integer(getattr(frame, "height", None))
        or frame.height <= 0
        or not isinstance(getattr(observation, "state", None), dict)
    ):
        return False, "observation_freshness", target_id, "malformed observation or frame metadata"

    state = observation.state
    if "handle" in state and not _same_handle(state["handle"], handle):
        return False, "handle_identity", target_id, "observation state aliases a foreign target handle"
    if "epoch" in state and state["epoch"] != rec_epoch:
        return False, "epoch_binding", target_id, "observation state epoch is stale"
    if "frame_seq" in state and state["frame_seq"] != frame_seq:
        return False, "observation_freshness", target_id, "observation state frame_seq differs from observation"

    digest_ok, digest_info = _verify_frame_digest(frame, expected_frame_digest)
    if not digest_ok:
        return False, "frame_digest", target_id, digest_info

    domain = f"cdp:{session_id}"
    focus_lease = getattr(focus, "lease", None)
    if focus_lease != lease or getattr(focus, "domain", None) != domain:
        return False, "focus_ownership", target_id, "focus grant does not match actor lease or session domain"

    validate_fn = getattr(focus_policy, "validate", None)
    consume_fn = getattr(focus_policy, "consume", None)
    if not callable(validate_fn) or not callable(consume_fn):
        return False, "focus_ownership", target_id, "focus_policy validate/consume prerequisite AUSENTE"
    try:
        validate_fn(lease, focus, observation, domain=domain, now_ns=now_ns)
    except FocusRefused as exc:
        msg = str(exc)
        if "expired" in msg:
            return False, "lease_expiry", target_id, msg
        if "nonce is not current/owned" in msg or "malformed, foreign" in msg:
            return False, "lease_ownership", target_id, msg
        if "epoch" in msg:
            return False, "epoch_binding", target_id, msg
        return False, "focus_ownership", target_id, msg

    for method in ("acquire", "release", "quarantine"):
        if not callable(getattr(writer_fence, method, None)):
            return False, "writer_fence", target_id, f"writer fence {method} prerequisite AUSENTE"
    if getattr(writer_fence, "busy", False) or getattr(writer_fence, "quarantined", False):
        return False, "writer_fence", target_id, "writer fence is busy or quarantined"
    consumed = getattr(writer_fence, "consumed_nonces", None)
    if isinstance(consumed, set) and nonce in consumed:
        return False, "writer_fence", target_id, "lease nonce already consumed by writer fence"

    if not isinstance(nodes, (list, tuple)) or not isinstance(action, dict):
        return False, "target_match", target_id, "nodes or action input malformed"
    mode = "coordinates" if action.get("path") == "coordinates" else "ax"
    op = action.get("op")
    if classify(mode, op) == FocusClass.REFUSE:
        return False, "target_match", target_id, "unclassified or unsupported operation refused"
    if mode == "coordinates" and (not allow_coordinates or action.get("irreversible")):
        return False, "target_match", target_id, "coordinate route not authorized"
    if mode == "coordinates" and op not in ("click", "hover", "lpath_click"):
        return False, "target_match", target_id, "no safe coordinate route for this op"
    if op in ("press", "insert_text", "type", "select"):
        if state.get("focused_node_id") != action.get("target"):
            return False, "focus_ownership", target_id, "keyboard/edit requires target to be focused element"

    return True, None, target_id, digest_info


# @anchor:wa.act.multiwin_isolation_verifier.verify_cdp_isolation
def verify_cdp_isolation(
    cdp: Any,
    session_id: str,
    nodes: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    action: dict[str, Any],
    read_nodes: Callable[[], Any],
    *,
    lease: Any,
    focus: Any,
    observation: Any,
    registry: Any,
    focus_policy: Any,
    writer_fence: Any,
    locks: Any = None,
    clock: Callable[[], int] | None = None,
    expected_frame_digest: str | None = None,
    retries: int = 3,
    wait_ms: int = 0,
    sleep: Callable[[float], None] = lambda _: None,
    allow_coordinates: bool = False,
    fail_closed: bool = False,
) -> dict[str, Any]:
    """Verify and execute a target-isolated CDP action with separated authorization, dispatch, and effect."""
    calls_list = getattr(cdp, "calls", None)
    initial_calls = len(calls_list) if isinstance(calls_list, list) else 0
    epoch = getattr(observation, "epoch", getattr(focus, "epoch", None))
    if not _is_exact_int(epoch):
        epoch = None

    ok_auth, pred, target_id, auth_detail = authorize_cdp_action(
        session_id=session_id,
        nodes=nodes,
        action=action,
        lease=lease,
        focus=focus,
        observation=observation,
        registry=registry,
        focus_policy=focus_policy,
        writer_fence=writer_fence,
        locks=locks,
        clock=clock,
        expected_frame_digest=expected_frame_digest,
        allow_coordinates=allow_coordinates,
    )
    after_auth_calls = len(calls_list) if isinstance(calls_list, list) else 0
    if not ok_auth:
        audit = build_audit_record(
            surface="cdp",
            target=target_id,
            stage="authorization",
            violated_predicate=pred or "lease_ownership",
            authorized=0,
            dispatched=0,
            verified_effect=0,
            actor_invocations=max(0, after_auth_calls - initial_calls),
            detail=auth_detail,
            pre_digest=getattr(getattr(observation, "frame", None), "digest", None)
            if isinstance(getattr(getattr(observation, "frame", None), "digest", None), str)
            else None,
            post_digest=None,
            epoch=epoch,
            attempts=0,
        )
        if fail_closed:
            raise IsolationViolationError(target_id, audit["violated_predicate"], "authorization", auth_detail, audit)
        return audit

    frame_digest = auth_detail
    try:
        receipt = run_ax_action(
            cdp,
            session_id,
            nodes,
            action,
            read_nodes,
            retries=retries,
            wait_ms=wait_ms,
            sleep=sleep,
            allow_coordinates=allow_coordinates,
            lease=lease,
            focus=focus,
            observation=observation,
            registry=registry,
            focus_policy=focus_policy,
            writer_fence=writer_fence,
            clock=clock,
        )
    except ActionError as exc:
        after_err_calls = len(calls_list) if isinstance(calls_list, list) else 0
        invocations = max(0, after_err_calls - initial_calls)
        err_text = str(exc)
        if "focus" in err_text:
            err_pred = "focus_ownership"
        elif "viewport" in err_text or "box" in err_text or "backend" in err_text or "target" in err_text or "AX" in err_text:
            err_pred = "target_match"
        elif "observation" in err_text or "frame" in err_text:
            err_pred = "observation_freshness"
        elif "writer" in err_text or "fence" in err_text:
            err_pred = "writer_fence"
        else:
            err_pred = "target_match"
        stage = "authorization" if invocations == 0 else "dispatch"
        audit = build_audit_record(
            surface="cdp",
            target=target_id,
            stage=stage,
            violated_predicate=err_pred,
            authorized=0 if invocations == 0 else 1,
            dispatched=1 if invocations > 0 else 0,
            verified_effect=0,
            actor_invocations=invocations,
            detail=err_text,
            pre_digest=frame_digest,
            post_digest=None,
            epoch=epoch,
            attempts=1 if invocations > 0 else 0,
        )
        if fail_closed:
            raise IsolationViolationError(target_id, err_pred, stage, err_text, audit) from exc
        return audit

    final_calls = len(calls_list) if isinstance(calls_list, list) else 0
    invocations = max(0, final_calls - initial_calls)
    attempts = int(receipt.get("attempts", 1))
    verified = 1 if (receipt.get("ok") == 1 and receipt.get("verified") == 1) else 0
    post_sha = receipt.get("post_sha")
    if verified == 1:
        return build_audit_record(
            surface="cdp",
            target=target_id,
            stage="verified_effect",
            violated_predicate=None,
            authorized=1,
            dispatched=1,
            verified_effect=1,
            actor_invocations=invocations,
            detail="authorized, dispatched once, and post-action effect verified",
            pre_digest=frame_digest,
            post_digest=post_sha,
            epoch=epoch,
            attempts=attempts,
        )

    err_detail = str(receipt.get("error") or "postcondition not established")
    audit = build_audit_record(
        surface="cdp",
        target=target_id,
        stage="verified_effect",
        violated_predicate="verified_effect",
        authorized=1,
        dispatched=1,
        verified_effect=0,
        actor_invocations=invocations,
        detail=err_detail,
        pre_digest=frame_digest,
        post_digest=post_sha,
        epoch=epoch,
        attempts=attempts,
    )
    if fail_closed:
        raise IsolationViolationError(target_id, "verified_effect", "verified_effect", err_detail, audit)
    return audit


def _classify_godot_rejection(reason: str) -> tuple[str, str]:
    if reason == "CONSENT_MAIN_SAVE":
        return "main_save_consent", "authorization"
    if reason == REASON_MANDATE_REQUIRED or reason in ("MANDATE_REQUIRED", "UNAUTHORIZED", "MANDATE", "OWNER_MANDATE", "FORBIDDEN"):
        return "trusted_mandate", "authorization"
    if reason in ("SEQUENCE", "DUPLICATE_SEQ"):
        return "sequence_binding", "authorization"
    if reason == "TICK_WINDOW":
        return "tick_window", "authorization"
    if reason == "STALE_CONFIRM":
        return "basis_binding", "authorization"
    if reason == "USED_CONFIRM":
        return "proposal_single_use", "authorization"
    if reason in ("NEEDS_CONFIRM", "TARGET_MISMATCH", "TERMS_MISMATCH") or reason == REASON_CMD_MISMATCH or reason in ("CMD_MISMATCH", "COMMAND_MISMATCH"):
        return "confirmation_binding", "authorization"
    if reason == "ACTION_UNAVAILABLE":
        return "schema_admission", "authorization"
    return "inbox_record_shape", "authorization"


# @anchor:wa.act.multiwin_isolation_verifier.authorize_godot_command
def authorize_godot_command(
    executor: GodotExecutor,
    record: Any,
    *,
    committed_tick: int,
    basis_sha256: str,
) -> tuple[bool, str | None, str, str]:
    """Pre-dispatch inspection of a Godot inbox record before executor.dispatch writes to sink."""
    target_id = _canonical_godot_target_id(record, getattr(executor, "_trusted_context", None))
    if not _is_exact_int(committed_tick) or committed_tick < 0:
        return False, "tick_window", target_id, "committed_tick must be a non-negative integer"
    if not isinstance(basis_sha256, str) or not basis_sha256.strip():
        return False, "basis_binding", target_id, "basis_sha256 must be a non-empty string"
    if not isinstance(record, dict):
        return False, "inbox_record_shape", target_id, "Godot inbox record must be a dict"
    if sorted(record.keys()) != sorted(INBOX_RECORD_KEYS):
        if any(k in FABRICATED_CONSENT_KEYS for k in record) and executor._is_main_slot_automated():
            return False, "main_save_consent", target_id, "automated main-save consent cannot be fabricated"
        return False, "inbox_record_shape", target_id, "Godot inbox record must have exactly args, cmd, seq and tick"

    tick = record.get("tick")
    seq = record.get("seq")
    cmd = record.get("cmd")
    args = record.get("args")
    if not _is_exact_int(tick) or tick < 0 or not _is_exact_int(seq) or seq < 1:
        return False, "inbox_record_shape", target_id, "tick must be >= 0 and seq must be >= 1"
    if not isinstance(cmd, str) or not cmd.strip() or not isinstance(args, dict):
        return False, "inbox_record_shape", target_id, "cmd must be non-empty str and args must be dict"
    try:
        schema.dumps(record)
    except Exception as exc:
        return False, "inbox_record_shape", target_id, f"non-canonical or float value in record: {exc}"

    if seq in executor._seen_seqs or seq != executor.next_seq:
        return False, "sequence_binding", target_id, f"expected contiguous seq {executor.next_seq}, got {seq}"

    return True, None, target_id, "pre-check passed"


# @anchor:wa.act.multiwin_isolation_verifier.verify_godot_isolation
def verify_godot_isolation(
    executor: GodotExecutor,
    record: dict[str, Any],
    *,
    committed_tick: int,
    basis_sha256: str,
    sink_list: list[bytes],
    fail_closed: bool = False,
) -> dict[str, Any]:
    """Verify a Godot T2 command over mandates, seq/tick/basis binding, and single-use proposals."""
    if not isinstance(sink_list, list):
        raise ValueError("sink_list must be an in-RAM list")
    initial_writes = len(sink_list)
    initial_seq = executor.next_seq
    target_id = _canonical_godot_target_id(record, getattr(executor, "_trusted_context", None))
    seq_val = record.get("seq") if isinstance(record, dict) and _is_exact_int(record.get("seq")) else None
    tick_val = record.get("tick") if isinstance(record, dict) and _is_exact_int(record.get("tick")) else None

    ok_pre, pre_pred, target_id, pre_detail = authorize_godot_command(
        executor,
        record,
        committed_tick=committed_tick,
        basis_sha256=basis_sha256,
    )
    if not ok_pre and (
        not isinstance(record, dict)
        or not _is_exact_int(committed_tick)
        or committed_tick < 0
        or not isinstance(basis_sha256, str)
        or not basis_sha256.strip()
    ):
        audit = build_audit_record(
            surface="godot",
            target=target_id,
            stage="authorization",
            violated_predicate=pre_pred or "inbox_record_shape",
            authorized=0,
            dispatched=0,
            verified_effect=0,
            actor_invocations=len(sink_list) - initial_writes,
            detail=pre_detail,
            pre_digest=basis_sha256 if isinstance(basis_sha256, str) else None,
            post_digest=None,
            seq=seq_val,
            tick=tick_val,
            attempts=0,
        )
        if fail_closed:
            raise IsolationViolationError(target_id, audit["violated_predicate"], "authorization", pre_detail, audit)
        return audit

    outcome = executor.dispatch(record, committed_tick=committed_tick, basis_sha256=basis_sha256)
    writes_delta = len(sink_list) - initial_writes
    status = outcome.get("status")
    reason = str(outcome.get("reason", ""))

    if status == STATUS_ADMITTED:
        seq_advanced = executor.next_seq == initial_seq + 1
        single_write_ok = (writes_delta == 1) if executor.transport_enabled else (writes_delta == 0)
        modal_id = outcome.get("modal_id")
        proposal_consumed_ok = True
        if isinstance(modal_id, str) and modal_id in executor.proposals:
            proposal_consumed_ok = executor.proposals[modal_id].get("used") is True
        effect_ok = 1 if (seq_advanced and single_write_ok and proposal_consumed_ok) else 0
        violated = None if effect_ok == 1 else "verified_effect"
        audit = build_audit_record(
            surface="godot",
            target=target_id,
            stage="verified_effect",
            violated_predicate=violated,
            authorized=1,
            dispatched=1,
            verified_effect=effect_ok,
            actor_invocations=writes_delta,
            detail="admitted and single sink write verified" if effect_ok == 1 else "post-dispatch sink or sequence invariant failed",
            pre_digest=basis_sha256,
            post_digest=basis_sha256,
            seq=outcome.get("seq", seq_val),
            tick=outcome.get("tick", tick_val),
            attempts=1,
            proposal_modal_id=modal_id if isinstance(modal_id, str) else None,
        )
        if fail_closed and violated is not None:
            raise IsolationViolationError(target_id, violated, "verified_effect", audit["detail"], audit)
        return audit

    modal_id = outcome.get("modal_id")
    pred, stage = _classify_godot_rejection(reason)
    audit = build_audit_record(
        surface="godot",
        target=target_id,
        stage=stage,
        violated_predicate=pred,
        authorized=0,
        dispatched=0,
        verified_effect=0,
        actor_invocations=writes_delta,
        detail=f"rejected with reason={reason}",
        pre_digest=basis_sha256,
        post_digest=None,
        seq=seq_val,
        tick=tick_val,
        attempts=0,
        proposal_modal_id=modal_id if isinstance(modal_id, str) else None,
    )
    if fail_closed:
        raise IsolationViolationError(target_id, pred, stage, audit["detail"], audit)
    return audit


# @anchor:wa.act.multiwin_isolation_verifier.CdpIsolationVerifier
class CdpIsolationVerifier:
    """Thread-safe verifier orchestrating CDP isolation checks and deterministic audit logs."""

    def __init__(
        self,
        registry: StructuralSessionRegistry,
        locks: StructuralLockOwnership,
        focus_policy: FocusPolicy,
        writer_fence: StructuralWriterFence,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.registry = registry
        self.locks = locks
        self.focus_policy = focus_policy
        self.writer_fence = writer_fence
        self.clock = clock
        self._lock = threading.RLock()
        self.audit_log: list[dict[str, Any]] = []

    def verify_and_run(
        self,
        cdp: Any,
        session_id: str,
        nodes: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        action: dict[str, Any],
        read_nodes: Callable[[], Any],
        *,
        lease: Any,
        focus: Any,
        observation: Any,
        expected_frame_digest: str | None = None,
        allow_coordinates: bool = False,
        fail_closed: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            audit = verify_cdp_isolation(
                cdp,
                session_id,
                nodes,
                action,
                read_nodes,
                lease=lease,
                focus=focus,
                observation=observation,
                registry=self.registry,
                focus_policy=self.focus_policy,
                writer_fence=self.writer_fence,
                locks=self.locks,
                clock=self.clock,
                expected_frame_digest=expected_frame_digest,
                allow_coordinates=allow_coordinates,
                fail_closed=fail_closed,
            )
            self.audit_log.append(copy.deepcopy(audit))
            return audit


# @anchor:wa.act.multiwin_isolation_verifier.GodotIsolationVerifier
class GodotIsolationVerifier:
    """Thread-safe verifier orchestrating Godot T2 isolation checks over an in-RAM sink list."""

    def __init__(self, executor: GodotExecutor, sink_list: list[bytes]) -> None:
        self.executor = executor
        self.sink_list = sink_list
        self._lock = threading.RLock()
        self.audit_log: list[dict[str, Any]] = []

    def verify_and_dispatch(
        self,
        record: dict[str, Any],
        *,
        committed_tick: int,
        basis_sha256: str,
        fail_closed: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            audit = verify_godot_isolation(
                self.executor,
                record,
                committed_tick=committed_tick,
                basis_sha256=basis_sha256,
                sink_list=self.sink_list,
                fail_closed=fail_closed,
            )
            self.audit_log.append(copy.deepcopy(audit))
            return audit

    def verify_and_dispatch_batch(
        self,
        records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        committed_tick: int,
        basis_sha256: str,
    ) -> list[dict[str, Any]]:
        with self._lock:
            initial_writes = len(self.sink_list)
            outcomes = self.executor.dispatch_batch(
                records,
                committed_tick=committed_tick,
                basis_sha256=basis_sha256,
            )
            writes_delta = len(self.sink_list) - initial_writes
            audits: list[dict[str, Any]] = []
            for rec, out in zip(records, outcomes):
                target_id = _canonical_godot_target_id(rec, getattr(self.executor, "_trusted_context", None))
                seq_val = rec.get("seq") if isinstance(rec, dict) and _is_exact_int(rec.get("seq")) else None
                tick_val = rec.get("tick") if isinstance(rec, dict) and _is_exact_int(rec.get("tick")) else None
                if out.get("status") == STATUS_ADMITTED:
                    effect_ok = 1 if (writes_delta == 1 or not self.executor.transport_enabled) else 0
                    audit = build_audit_record(
                        surface="godot",
                        target=target_id,
                        stage="verified_effect",
                        violated_predicate=None if effect_ok == 1 else "verified_effect",
                        authorized=1,
                        dispatched=1,
                        verified_effect=effect_ok,
                        actor_invocations=writes_delta,
                        detail="batch command admitted before single sink write",
                        pre_digest=basis_sha256,
                        post_digest=basis_sha256,
                        seq=seq_val,
                        tick=tick_val,
                        attempts=1,
                    )
                else:
                    reason = str(out.get("reason", "SCHEMA"))
                    pred, stage = _classify_godot_rejection(reason)
                    audit = build_audit_record(
                        surface="godot",
                        target=target_id,
                        stage=stage,
                        violated_predicate=pred,
                        authorized=0,
                        dispatched=0,
                        verified_effect=0,
                        actor_invocations=0,
                        detail=f"batch command rejected with reason={reason}",
                        pre_digest=basis_sha256,
                        post_digest=None,
                        seq=seq_val,
                        tick=tick_val,
                        attempts=0,
                        proposal_modal_id=out.get("modal_id") if isinstance(out.get("modal_id"), str) else None,
                    )
                self.audit_log.append(copy.deepcopy(audit))
                audits.append(audit)
            return audits
