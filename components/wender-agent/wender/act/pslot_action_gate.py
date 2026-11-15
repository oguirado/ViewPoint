# @module {"id":"wa.act.pslot_action_gate","layer":2,"owner":"ACT","deps":["wa.act.cdp_executor","wa.act.focus_policy","wa.perceive.session_registry"],"book":"HB-WA-s2-act"}
"""One-shot vision-evidence gate; no flow switch, provider, or authority adapter.

Public contract v1 is deliberately narrower than the CDP executor. Coordinate
click/hover/lpath_click require an authenticated observed backend binding. A
visual detector ID alone is insufficient. All authority and perception producers
are injected; missing production protocols are never replaced with defaults.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
import hashlib
import re
import time
from typing import Protocol

import numpy as np

from wender.act import cdp_executor
from wender.act.focus_policy import FocusPolicy, FocusRefused
from wender.perceive import ax
from wender.perceive.session_registry import ActorLease, FrameRef, Handle, Observation
from wender.state import schema


CONTRACT_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class Refusal(str, Enum):
    INVALID_REQUEST = "invalid_request"
    AUTHORITY_ABSENT = "authority_absent"
    AUTHORITY_REFUSED = "authority_refused"
    TARGET_MISMATCH = "target_mismatch"
    STALE_FRAME = "stale_frame"
    DIGEST_MISMATCH = "digest_mismatch"
    AMBIGUOUS_TARGET = "ambiguous_target"
    TARGET_ABSENT = "target_absent"
    EVIDENCE_ABSENT = "evidence_absent"
    POSTCONDITION_FAILED = "postcondition_failed"
    TRANSPORT_FAILED = "transport_failed"
    EXECUTOR_REFUSED = "executor_refused"


class GateRefused(ValueError):
    """Stable reason and whether transport may have had an effect; never retry it."""

    def __init__(self, reason: Refusal, message: str, *, dispatched: bool = False):
        self.reason = Refusal(reason)
        self.dispatched = bool(dispatched)
        super().__init__(message)


@dataclass(frozen=True)
class VisionTarget:
    """Selection provenance, kept outside the existing closed action payload."""

    handle: Handle
    epoch: int
    frame_seq: int
    digest: str
    node_id: str


@dataclass(frozen=True)
class GateReceipt:
    """Metadata only: no frame, state, provider output, action text, or nonce."""

    handle: Handle
    epoch: int
    target_id: str
    pre_frame_seq: int
    post_frame_seq: int
    pre_digest: str
    post_digest: str
    ok: int = 1
    verified: int = 1
    attempts: int = 1
    path: str = "coordinates"


class ObservationAuthority(Protocol):
    """Trusted issuance/currentness protocol, not implemented by SessionRegistry.

    get returns a live record with handle, epoch, closed, session_id, transport.
    validate_observation must authenticate issued state AND RGB provenance and
    reject superseded frames, mutated state, and unknown observations. Returning
    a truthy value other than True does not grant authority. Producers/fences
    must serialize target, focus and modal changes with writers; these calls are
    not a distributed atomic transaction. Consumed-focus validation between
    events is not supplied by the snapshot's public FocusPolicy API.
    """

    def get(self, handle: Handle) -> object: ...
    def validate_observation(self, observation: Observation) -> bool: ...


class WriterFence(Protocol):
    """Exclusive writer held through verification, including focus/modal stability.

    External event producers must honor this fence; the gate cannot enforce
    their cooperation or independently validate an already consumed focus grant.
    Quarantine requires external reconciliation, never automatic replay.
    """

    def acquire(self, lease: ActorLease) -> bool: ...
    def release(self, lease: ActorLease) -> None: ...
    def quarantine(self, lease: ActorLease) -> None: ...


class PerceptionReader(Protocol):
    """Return issued evidence; the post call must acquire it after the final send.

    Observation has no capture timestamp/action barrier. The producer must not
    relabel cached pre-action media with a fresh sequence or admit it as post.
    """

    def __call__(self) -> Observation: ...


def _refuse(reason, message):
    raise GateRefused(reason, message)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _action(action, target):
    if not isinstance(target, VisionTarget) or not isinstance(target.handle, Handle):
        _refuse(Refusal.INVALID_REQUEST, "VisionTarget with a real Handle is required")
    if (target.handle.kind != "cdp_tab" or not _integer(target.epoch)
            or not _integer(target.frame_seq) or not isinstance(target.node_id, str)
            or not target.node_id or not isinstance(target.digest, str)
            or not _DIGEST.fullmatch(target.digest)):
        _refuse(Refusal.INVALID_REQUEST, "malformed vision target binding")
    if not isinstance(action, dict):
        _refuse(Refusal.INVALID_REQUEST, "action must be a closed dictionary")
    result = copy.deepcopy(action)
    op = result.get("op")
    keys = {"op", "target", "path", "expect", "irreversible"}
    if op == "lpath_click":
        keys |= {"origin", "waypoint_policy"}
    if (set(result) - keys or op not in ("click", "hover", "lpath_click")
            or result.get("path") != "coordinates"
            or result.get("irreversible", False) is not False):
        _refuse(Refusal.INVALID_REQUEST, "unsupported or extended coordinate action")
    if result.get("target") != target.node_id:
        _refuse(Refusal.TARGET_MISMATCH, "action and vision target differ")
    expect = result.get("expect")
    if (not isinstance(expect, dict) or not expect
            or set(expect) - {"target_name", "target_states"}):
        _refuse(Refusal.INVALID_REQUEST, "explicit nonvacuous expectation is required")
    if "target_name" in expect and not isinstance(expect["target_name"], str):
        _refuse(Refusal.INVALID_REQUEST, "expected target name must be a string")
    if "target_states" in expect:
        states = expect["target_states"]
        if (not isinstance(states, dict) or not states
                or any(not isinstance(k, str) or not k or not isinstance(v, str)
                       for k, v in states.items())):
            _refuse(Refusal.INVALID_REQUEST, "expected states must be nonempty string pairs")
    if op == "lpath_click":
        origin = result.get("origin")
        if (not isinstance(origin, (list, tuple)) or len(origin) != 2
                or any(type(n) is not int for n in origin)
                or result.get("waypoint_policy", "horizontal_first") not in
                ("horizontal_first", "vertical_first")):
            _refuse(Refusal.INVALID_REQUEST, "invalid existing L-path parameters")
    return result


def _states(node):
    attrs = node.get("attrs")
    if (not isinstance(attrs, (list, tuple))
            or any(not isinstance(pair, (list, tuple)) or len(pair) != 2
                   or any(not isinstance(item, str) for item in pair) for pair in attrs)):
        _refuse(Refusal.EVIDENCE_ABSENT, "executor node attributes are absent or malformed")
    result = dict(attrs)
    if len(result) != len(attrs):
        _refuse(Refusal.AMBIGUOUS_TARGET, "duplicate attribute evidence")
    return result


def _expect_met(node, expect):
    states = _states(node)
    return (all(states.get(k) == v for k, v in expect.get("target_states", {}).items())
            and ("target_name" not in expect or
                 ax.normalize_name(node["name"]) == ax.normalize_name(expect["target_name"])))


def _node(nodes, node_id, *, previous=None):
    if not isinstance(nodes, (list, tuple)) or not nodes:
        _refuse(Refusal.EVIDENCE_ABSENT, "observed nodes are absent")
    for node in nodes:
        if (not isinstance(node, dict) or not isinstance(node.get("id"), str)
                or not node["id"] or not isinstance(node.get("role"), str)
                or not isinstance(node.get("name"), str)
                or not _integer(node.get("depth")) or not _integer(node.get("ordinal"))
                or not _integer(node.get("cardinality"), 1)
                or not isinstance(node.get("ancestors"), (list, tuple))
                or any(not isinstance(s, str) for s in node["ancestors"])):
            _refuse(Refusal.EVIDENCE_ABSENT, "executor-compatible observed node evidence required")
        _states(node)
    matches = [node for node in nodes if node["id"] == node_id]
    if not matches and previous is not None:
        # A name-derived ID may change; an authenticated backend/role is the only
        # allowed fallback, and its cardinality must still be exactly one.
        matches = [node for node in nodes if
                   (node.get("backend_id"), node["role"]) ==
                   (previous["backend_id"], previous["role"])]
    if not matches:
        _refuse(Refusal.TARGET_ABSENT, "selected target is absent")
    if len(matches) != 1:
        _refuse(Refusal.AMBIGUOUS_TARGET, "selected target is not unique")
    selected = matches[0]
    backend = selected.get("backend_id")
    if not _integer(backend, 1):
        _refuse(Refusal.EVIDENCE_ABSENT, "verified backend binding AUSENTE")
    if sum(node.get("backend_id") == backend for node in nodes) != 1:
        _refuse(Refusal.AMBIGUOUS_TARGET, "backend binding is ambiguous")
    if previous is not None and (backend, selected["role"]) != (
            previous["backend_id"], previous["role"]):
        _refuse(Refusal.TARGET_MISMATCH, "post target backend or role changed")
    match = ax.match(nodes, ax.describe(selected))
    if (match.get("candidates") != 1 or match.get("tie") or match.get("established")
            or not ax.level_allows_irreversible(match.get("level"))
            or match.get("node") is not selected):
        _refuse(Refusal.AMBIGUOUS_TARGET, "target fingerprint does not uniquely resolve")
    return selected


def _state_bytes(state):
    try:
        return schema.dumps(state).encode("utf-8")
    except Exception:
        _refuse(Refusal.EVIDENCE_ABSENT, "state must be canonical metadata without media")


def _geometry(state, observation, node):
    """v1 admits full, unscrolled viewport captures with exact CSS/DPR mapping.

    The snapshot executor does not expose a crop/scroll coordinate transform.
    Refuse those cases rather than infer a transform or emit off-frame input.
    """
    viewport = state.get("viewport")
    frame = observation.frame
    if (not isinstance(viewport, dict) or not _integer(viewport.get("w"), 1)
            or not _integer(viewport.get("h"), 1)
            or type(viewport.get("scroll_x", 0)) is not int
            or type(viewport.get("scroll_y", 0)) is not int
            or viewport.get("scroll_x", 0) != 0 or viewport.get("scroll_y", 0) != 0
            or viewport["w"] * observation.dpr_ppm != frame.width * 1000000
            or viewport["h"] * observation.dpr_ppm != frame.height * 1000000):
        _refuse(Refusal.EVIDENCE_ABSENT, "verified full-viewport CSS/DPR geometry AUSENTE")
    box = node.get("box")
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(value) is not int for value in box)
            or box[0] < 0 or box[1] < 0 or box[2] <= 0 or box[3] <= 0
            or box[0] + box[2] > viewport["w"] or box[1] + box[3] > viewport["h"]):
        _refuse(Refusal.EVIDENCE_ABSENT, "selected visual box is outside the verified frame")


def _snapshot(observation, target, registry, cdp, session_id, *, previous=None):
    post = previous is not None
    if not isinstance(observation, Observation) or not isinstance(observation.frame, FrameRef):
        _refuse(Refusal.EVIDENCE_ABSENT, "real Observation and FrameRef evidence is required")
    frame = observation.frame
    if observation.handle != target.handle or frame.handle != target.handle:
        _refuse(Refusal.TARGET_MISMATCH, "observation belongs to another target")
    if observation.epoch != target.epoch or frame.epoch != target.epoch:
        _refuse(Refusal.STALE_FRAME, "observation epoch changed")
    if (not _integer(observation.frame_seq) or frame.frame_seq != observation.frame_seq
            or (observation.frame_seq <= target.frame_seq if post else
                observation.frame_seq != target.frame_seq)):
        _refuse(Refusal.STALE_FRAME, "observation sequence is stale or inconsistent")
    if (not _integer(observation.dpr_ppm, 1) or not _integer(frame.width, 1)
            or not _integer(frame.height, 1) or not isinstance(frame.rgb, np.ndarray)
            or frame.rgb.dtype != np.uint8 or frame.rgb.shape != (frame.height, frame.width, 3)):
        _refuse(Refusal.EVIDENCE_ABSENT, "invalid frame geometry")
    actual_digest = hashlib.sha256(frame.rgb.tobytes(order="C")).hexdigest()
    if (not isinstance(frame.digest, str) or not _DIGEST.fullmatch(frame.digest)
            or actual_digest != frame.digest or (not post and frame.digest != target.digest)):
        _refuse(Refusal.DIGEST_MISMATCH, "RGB digest does not match authenticated selection")
    try:
        record = registry.get(target.handle)
        if (record is None or getattr(record, "closed", True) is not False
                or getattr(record, "handle", None) != target.handle
                or getattr(record, "transport", None) is not cdp
                or getattr(record, "session_id", None) != session_id):
            _refuse(Refusal.TARGET_MISMATCH, "live transport/session/target binding is absent")
        if getattr(record, "epoch", None) != target.epoch:
            _refuse(Refusal.STALE_FRAME, "current target epoch differs")
        before_validation = _state_bytes(observation.state)
        if registry.validate_observation(observation) is not True:
            _refuse(Refusal.AUTHORITY_REFUSED, "observation is not authenticated and current")
        state = copy.deepcopy(observation.state)
        if _state_bytes(state) != before_validation:
            _refuse(Refusal.AUTHORITY_REFUSED, "observation changed during validation")
    except GateRefused:
        raise
    except Exception:
        _refuse(Refusal.AUTHORITY_REFUSED, "observation authority failed")
    if not isinstance(state.get("source"), dict) or state["source"].get("kind") != "vision":
        _refuse(Refusal.EVIDENCE_ABSENT, "vision-source evidence AUSENTE")
    for name, value in (("handle", target.handle), ("epoch", target.epoch),
                        ("frame_seq", observation.frame_seq)):
        if name in state and state[name] != value:
            _refuse(Refusal.TARGET_MISMATCH, "state provenance aliases another observation")
    nodes = state.get("nodes")
    node = _node(nodes, target.node_id, previous=previous)
    if not post:
        _geometry(state, observation, node)
    return state, nodes, node


class _HeldFence:
    """Defer executor release until the outer gate has accepted its receipt."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.active = False
        self.quarantined = False

    def acquire(self, lease):
        result = self.delegate.acquire(lease)
        self.active = result is True
        return result

    def release(self, lease):
        # The real executor's finally cannot release before our final decision.
        pass

    def quarantine(self, lease):
        if self.active and not self.quarantined:
            self.quarantined = True
            self.delegate.quarantine(lease)

    def finish(self, lease, *, success, dispatched):
        if not self.active or self.quarantined:
            return
        if dispatched and not success:
            self.quarantine(lease)
            return
        try:
            self.delegate.release(lease)
            self.active = False
        except Exception:
            self.quarantine(lease)
            raise


class _Transport:
    def __init__(self, delegate, session_id, preflight):
        self.delegate = delegate
        self.session_id = session_id
        self.preflight = preflight
        self.dispatched = False
        self.failure = None

    def send(self, method, params, *, session_id=None):
        if method != "Input.dispatchMouseEvent" or session_id != self.session_id:
            _refuse(Refusal.EXECUTOR_REFUSED, "executor attempted an unbound transport operation")
        try:
            self.preflight()
        except GateRefused as exc:
            # The real executor conservatively turns send errors into an
            # uncertain receipt. Preserve our precise local guard refusal.
            self.failure = exc
            raise
        # Mark before send: an exception/timeout does not establish no effect.
        self.dispatched = True
        return self.delegate.send(method, params, session_id=session_id)


def run_vision_action(cdp, session_id, action, *, target, observation,
                      read_observation: PerceptionReader, registry: ObservationAuthority,
                      lease, focus, focus_policy, writer_fence: WriterFence,
                      clock=None, wait_ms=0, sleep=time.sleep) -> GateReceipt:
    """Execute one existing coordinate action and prove a fresh visible effect.

    Only the declared collaborators may supply authority/perception. The reader
    runs under the writer fence, once to reconfirm the exact selected snapshot
    and once after the complete action. The latter must have a newer sequence,
    changed RGB, stable target/epoch/geometry, and actual expected node evidence.
    A failed postcondition or partial send quarantines the writer; no retry or
    inference from a transport acknowledgement occurs. See the owned design note
    for production prerequisites that remain AUSENTE in this dependency wave.
    """
    request = _action(action, target)
    if not isinstance(session_id, str) or not session_id:
        _refuse(Refusal.INVALID_REQUEST, "explicit CDP session is required")
    if not _integer(wait_ms) or not callable(sleep) or (clock is not None and not callable(clock)):
        _refuse(Refusal.INVALID_REQUEST, "invalid clock, sleep, or wait interval")
    for collaborator, methods in ((registry, ("get", "validate_observation")),
                                  (writer_fence, ("acquire", "release", "quarantine")),
                                  (cdp, ("send",))):
        if collaborator is None or any(not callable(getattr(collaborator, m, None)) for m in methods):
            _refuse(Refusal.AUTHORITY_ABSENT, "required structural collaborator AUSENTE")
    if (not isinstance(lease, ActorLease) or focus is None
            or not isinstance(focus_policy, FocusPolicy)
            or not callable(getattr(getattr(focus_policy, "locks", None), "owns", None))):
        _refuse(Refusal.AUTHORITY_ABSENT, "actor/focus ownership authority AUSENTE")
    if not callable(read_observation):
        _refuse(Refusal.EVIDENCE_ABSENT, "perception reader AUSENTE")
    if focus_policy.registry is not registry or lease.handle != target.handle:
        _refuse(Refusal.TARGET_MISMATCH, "authority is bound to another registry/target")
    now = time.monotonic_ns if clock is None else clock
    baseline, nodes, selected = _snapshot(observation, target, registry, cdp, session_id)
    baseline_bytes = _state_bytes(baseline)
    if _expect_met(selected, request["expect"]):
        _refuse(Refusal.POSTCONDITION_FAILED, "expectation already true before action")

    def preflight():
        try:
            state, _, _ = _snapshot(observation, target, registry, cdp, session_id)
            if _state_bytes(state) != baseline_bytes:
                _refuse(Refusal.STALE_FRAME, "selected node evidence changed before dispatch")
            instant = now()
            if (not _integer(instant) or instant >= lease.expires_ns
                    or focus_policy.locks.owns(lease, now_ns=instant) is not True):
                _refuse(Refusal.AUTHORITY_REFUSED, "actor lease no longer owned or live")
        except GateRefused:
            raise
        except Exception:
            _refuse(Refusal.AUTHORITY_REFUSED, "pre-send authority check failed")

    fence = _HeldFence(writer_fence)
    transport = _Transport(cdp, session_id, preflight)
    post_observation = None
    reader_calls = 0
    callback_failure = None

    def read_nodes():
        nonlocal post_observation, reader_calls, callback_failure
        reader_calls += 1
        try:
            current = read_observation()
            if not transport.dispatched:
                state, fresh_nodes, _ = _snapshot(current, target, registry, cdp, session_id)
                if _state_bytes(state) != baseline_bytes:
                    _refuse(Refusal.STALE_FRAME, "pre-action reader changed selected evidence")
                return fresh_nodes
            state, fresh_nodes, fresh_target = _snapshot(
                current, target, registry, cdp, session_id, previous=selected)
            if (current.dpr_ppm != observation.dpr_ppm
                    or (current.frame.width, current.frame.height) !=
                    (observation.frame.width, observation.frame.height)
                    or state.get("viewport") != baseline.get("viewport")):
                _refuse(Refusal.STALE_FRAME, "post-action geometry changed without a new contract")
            _geometry(state, current, fresh_target)
            if (current.frame.digest == target.digest
                    or not _expect_met(fresh_target, request["expect"])):
                _refuse(Refusal.POSTCONDITION_FAILED, "fresh visible expected effect not established")
            post_observation = current
            return fresh_nodes
        except GateRefused as exc:
            callback_failure = exc
            raise
        except Exception:
            callback_failure = GateRefused(Refusal.EVIDENCE_ABSENT, "perception evidence unreadable")
            raise callback_failure from None

    success = False
    try:
        # Validate the real policy early for a stable authority refusal, then let
        # the executor revalidate and consume its actual one-use grant.
        focus_policy.validate(lease, focus, observation, domain="cdp:" + session_id, now_ns=now())
        outcome = cdp_executor.run_ax_action(
            transport, session_id, nodes, request, read_nodes, retries=1,
            wait_ms=wait_ms, sleep=sleep, allow_coordinates=True,
            lease=lease, focus=focus, observation=observation, registry=registry,
            focus_policy=focus_policy, writer_fence=fence, clock=now)
        if callback_failure is not None:
            raise callback_failure
        if transport.failure is not None:
            raise transport.failure
        if (not isinstance(outcome, dict) or outcome.get("ok") != 1
                or outcome.get("verified") != 1 or outcome.get("attempts") != 1
                or outcome.get("path") != "coordinates" or reader_calls != 2
                or post_observation is None or not transport.dispatched):
            _refuse(Refusal.TRANSPORT_FAILED if transport.dispatched else Refusal.EXECUTOR_REFUSED,
                    "executor did not establish a single verified effect")
        receipt = GateReceipt(target.handle, target.epoch, target.node_id,
                              target.frame_seq, post_observation.frame_seq,
                              target.digest, post_observation.frame.digest)
        success = True
        return receipt
    except GateRefused as exc:
        exc.dispatched = transport.dispatched
        raise
    except FocusRefused:
        raise GateRefused(Refusal.AUTHORITY_REFUSED, "focus authority refused",
                          dispatched=transport.dispatched) from None
    except cdp_executor.ActionError as exc:
        reason = (Refusal.AUTHORITY_REFUSED if isinstance(exc.__cause__, FocusRefused)
                  else Refusal.EXECUTOR_REFUSED)
        failure = callback_failure or transport.failure or GateRefused(reason, "real executor refused")
        failure.dispatched = transport.dispatched
        raise failure from None
    except Exception:
        raise GateRefused(Refusal.TRANSPORT_FAILED if transport.dispatched else Refusal.AUTHORITY_REFUSED,
                          "collaborator failed", dispatched=transport.dispatched) from None
    finally:
        try:
            fence.finish(lease, success=success, dispatched=transport.dispatched)
        except Exception:
            raise GateRefused(Refusal.AUTHORITY_REFUSED, "writer fence finalization failed",
                              dispatched=transport.dispatched) from None


__all__ = ["CONTRACT_VERSION", "Refusal", "GateRefused", "VisionTarget", "GateReceipt",
           "ObservationAuthority", "WriterFence", "PerceptionReader", "run_vision_action"]
