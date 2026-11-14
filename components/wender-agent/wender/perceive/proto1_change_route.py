# @module {"id":"wa.perceive.proto1_change_route","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.session_registry","wa.perceive.vision_adapter"],"book":"HB-WA-s1-perceive"}
"""Fail-closed C-lane change-evidence routing for registered RAM-only observations.

Public entrypoints:

* ``ChangeEvidenceRouter.route(observation)`` routes one structurally bound
  Observation. A successful first sample publishes BASELINE; consecutive
  samples publish CHANGED or UNCHANGED with the detector's validated regions.
  Exactly-once replay returns ``None`` and never invokes the publisher again.
* ``ChangeEvidenceRouter.route_many(observations, *, runner=None)`` groups by
  full (kind, canonical_id, generation, epoch), serializes each lane, and
  returns ordered ``RouteOutcome`` objects even if group completion is reversed.
* ``c_score_identity_preconditions(evidence)`` checks a contiguous, identical
  C-score evidence window, but *never* fabricates a score or settled verdict.

The current merged producer exposes ``VisionEngineAdapter.screen_identity`` and
``changed_regions``, not a budget/exhaustion telemetry or calibrated C-score
producer. The caller MUST inject a genuine detector and a publisher; there is
no permissive fallback. Our finite pixel-byte ceiling is an admission budget,
not a measured CPU time or a claim about a host. A detector exception or lack of
verified regions never implies settled. No file or network I/O occurs here.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import re
from threading import RLock
from typing import Callable, Iterable

import numpy as np

from .session_registry import (
    FrameRef, Handle, Observation, RegistryError, SessionRegistry,
    SUPPORTED_KINDS,
)
from .vision_adapter import Frame, VisionEngineAdapter

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX16 = re.compile(r"[0-9a-f]{16}\Z")


class ChangeRouteError(ValueError):
    """Typed refusal; no publication follows a refused sample."""

    def __init__(self, code: str, explanation: str) -> None:
        self.code = code
        super().__init__(f"{code}: {explanation}")


class DetectorExhausted(RuntimeError):
    """An injected detector may signal exhausted operations without a verdict."""


def _number(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ChangeRouteError("INVALID_EVIDENCE", f"{label} must be exact int >= {minimum}")
    return value


def _identity(handle: object) -> tuple[str, str, int]:
    """Check attributes, not a Python class, alias, or object identity."""
    try:
        kind, canonical_id, generation = handle.kind, handle.canonical_id, handle.generation
    except AttributeError as exc:
        raise ChangeRouteError("INVALID_HANDLE", "kind, canonical_id, generation required") from exc
    if type(kind) is not str or kind not in SUPPORTED_KINDS:
        raise ChangeRouteError("INVALID_HANDLE", "unsupported target kind")
    if (type(canonical_id) is not str or not canonical_id or
            canonical_id != canonical_id.strip() or "\x00" in canonical_id):
        raise ChangeRouteError("INVALID_HANDLE", "invalid canonical_id")
    _number(generation, "generation", minimum=1)
    return kind, canonical_id, generation


def _owned_rgb(rgb: object, width: int, height: int) -> bytes:
    """Require immutable source storage and make another owned RAM snapshot.

    A readonly ndarray whose *ultimate* owner is writable storage is unsafe:
    the producer could rewrite it between validation and detector admission.
    Real SessionRegistry.FrameRef arrays have an immutable bytes root.
    """
    count = width * height * 3
    if type(rgb) is bytes:
        if len(rgb) != count:
            raise ChangeRouteError("BAD_PIXELS", "RGB24 byte count differs from geometry")
        return rgb
    if not isinstance(rgb, np.ndarray):
        raise ChangeRouteError("BAD_PIXELS", "RGB24 requires immutable bytes or ndarray")
    if (rgb.dtype != np.dtype("uint8") or rgb.shape != (height, width, 3)
            or not rgb.flags.c_contiguous or rgb.flags.writeable):
        raise ChangeRouteError("BAD_PIXELS", "RGB24 must be contiguous readonly HxWx3 uint8")
    owner = rgb
    owners_seen = set()
    while isinstance(owner, (np.ndarray, memoryview)):
        if id(owner) in owners_seen:
            raise ChangeRouteError("BAD_PIXELS", "cyclic buffer ownership")
        owners_seen.add(id(owner))
        new_owner = owner.base if isinstance(owner, np.ndarray) else owner.obj
        if new_owner is None:
            break
        owner = new_owner
    if type(owner) is not bytes:
        raise ChangeRouteError("BAD_PIXELS", "RGB24 source is not immutable owned bytes")
    return rgb.tobytes(order="C")


def _snapshot(observation: object) -> Observation:
    """Revalidate all structural fields, SHA, state binding and immutable bytes."""
    try:
        handle = observation.handle
        epoch = observation.epoch
        frame_seq = observation.frame_seq
        dpr = observation.dpr_ppm
        state = observation.state
        source_frame = observation.frame
    except AttributeError as exc:
        raise ChangeRouteError("INVALID_EVIDENCE", "Observation fields missing") from exc
    handle_tuple = _identity(handle)
    epoch = _number(epoch, "epoch")
    frame_seq = _number(frame_seq, "frame_seq")
    dpr = _number(dpr, "dpr_ppm", minimum=1)
    if not isinstance(state, dict) or not isinstance(state.get("nodes"), list):
        raise ChangeRouteError("INVALID_EVIDENCE", "AX state must have nodes list")
    if "handle" in state:
        state_handle = state["handle"]
        if isinstance(state_handle, dict):
            try:
                state_handle = Handle(state_handle["kind"], state_handle["canonical_id"],
                                      state_handle["generation"])
            except (KeyError, RegistryError) as exc:
                raise ChangeRouteError("INVALID_EVIDENCE", "invalid state handle") from exc
        if _identity(state_handle) != handle_tuple:
            raise ChangeRouteError("SIBLING_EVIDENCE", "AX state belongs to a different handle")
    if "epoch" in state and (type(state["epoch"]) is not int or state["epoch"] != epoch):
        raise ChangeRouteError("STALE_EPOCH", "AX state epoch differs")
    viewport = state.get("viewport")
    if isinstance(viewport, dict) and "dpr_ppm" in viewport and (
        type(viewport["dpr_ppm"]) is not int or viewport["dpr_ppm"] != dpr
    ):
        raise ChangeRouteError("INVALID_EVIDENCE", "viewport DPR mismatch")
    for node in state["nodes"]:
        if not isinstance(node, dict):
            raise ChangeRouteError("INVALID_EVIDENCE", "AX node must be dict")
        if "handle" in node:
            node_handle = node["handle"]
            if isinstance(node_handle, dict):
                try:
                    node_handle = Handle(node_handle["kind"], node_handle["canonical_id"],
                                         node_handle["generation"])
                except (KeyError, RegistryError) as exc:
                    raise ChangeRouteError("INVALID_EVIDENCE", "invalid node handle") from exc
            if _identity(node_handle) != handle_tuple:
                raise ChangeRouteError("SIBLING_EVIDENCE", "AX node belongs to sibling")
        if "epoch" in node and (type(node["epoch"]) is not int or node["epoch"] != epoch):
            raise ChangeRouteError("STALE_EPOCH", "AX node epoch differs")
    try:
        frame_tuple = _identity(source_frame.handle)
        fe, fs = source_frame.epoch, source_frame.frame_seq
        width, height = source_frame.width, source_frame.height
        digest, rgb = source_frame.digest, source_frame.rgb
    except AttributeError as exc:
        raise ChangeRouteError("INVALID_EVIDENCE", "FrameRef fields missing") from exc
    if (frame_tuple, fe, fs) != (handle_tuple, epoch, frame_seq):
        raise ChangeRouteError("SIBLING_EVIDENCE", "FrameRef full tuple differs")
    _number(width, "width", minimum=1)
    _number(height, "height", minimum=1)
    if type(digest) is not str or not _HEX64.fullmatch(digest):
        raise ChangeRouteError("BAD_DIGEST", "source SHA-256 format invalid")
    pixels = _owned_rgb(rgb, width, height)
    if hashlib.sha256(pixels).hexdigest() != digest:
        raise ChangeRouteError("BAD_DIGEST", "source RGB24 SHA-256 mismatch")
    stable_handle = Handle(*handle_tuple)
    frozen_frame = FrameRef(stable_handle, epoch, frame_seq, width, height,
                            np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3), digest)
    return Observation(stable_handle, epoch, frame_seq, dpr, deepcopy(state), frozen_frame)


@dataclass(frozen=True)
class ChangedRegion:
    """Validated integer device-coordinate rectangle (no pixels)."""

    x: int
    y: int
    w: int
    h: int
    tiles: int


@dataclass(frozen=True)
class RoutedEvidence:
    """Immutable routing metadata with a bytes-backed FrameRef in RAM."""

    handle: Handle
    epoch: int
    frame_seq: int
    frame: FrameRef
    source_sha256: str
    screen_identity: str
    status: str                    # BASELINE, CHANGED or UNCHANGED
    regions: tuple[ChangedRegion, ...]
    admitted_work_units: int      # conservative ceiling, NOT elapsed CPU time
    settled: None = None          # C-score/calibrated settlement is AUSENTE


@dataclass(frozen=True)
class RouteOutcome:
    """Submission-order outcome; refused rows contain no routed publication."""

    index: int
    evidence: RoutedEvidence | None
    error: str | None


@dataclass(frozen=True)
class CScoreIdentityPrerequisites:
    handle: Handle
    epoch: int
    frame_seqs: tuple[int, ...]
    source_digests: tuple[str, ...]
    score: None = None
    settled: None = None
    missing_producer: str = "AUSENTE"


@dataclass
class _Lane:
    first_seq: int
    last_seq: int
    last_digest: str
    dimensions: tuple[int, int]
    last_frame: FrameRef
    # A bounded window for C-score; old redeliveries remain no-ops by the
    # contiguous accepted-sequence invariant, never a re-publication.
    history: OrderedDict[int, RoutedEvidence] = field(default_factory=OrderedDict)


def _regions(regions: object, width: int, height: int) -> tuple[ChangedRegion, ...]:
    if type(regions) is not list:
        raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "detector regions unavailable")
    if len(regions) > width * height:
        raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "too many detector regions")
    result = []
    for item in regions:
        if not isinstance(item, dict):
            raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "region must be dict")
        vals = []
        for name in ("x", "y", "w", "h", "tiles"):
            vals.append(_number(item.get(name), f"region.{name}", minimum=1 if name in ("w", "h", "tiles") else 0))
        x, y, w, h, tiles = vals
        if x + w > width or y + h > height:
            raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "region outside frame")
        result.append(ChangedRegion(*vals))
    if result != sorted(result, key=lambda r: (r.y, r.x, r.h, r.w, r.tiles)):
        raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "detector regions not sorted")
    return tuple(result)


class ChangeEvidenceRouter:
    """Thread-safe registered-target C-lane router with exactly-once claims.

    The injected publisher is called once per accepted evidence tuple, under a
    lock after revalidating its registry epoch. If publishing raises, the claim
    remains consumed (uncertain send); never retry the same tuple implicitly.
    No detector or publisher is guessed when missing.
    """

    def __init__(self, registry: SessionRegistry, *, adapter: VisionEngineAdapter,
                 publisher: Callable[[RoutedEvidence], None], max_rgb_bytes: int = 1_000_000,
                 max_work_units: int = 4_000_000, max_lanes: int = 32,
                 max_history: int = 8, max_batch_items: int = 32) -> None:
        if not isinstance(registry, SessionRegistry):
            raise ChangeRouteError("AUSENTE", "genuine SessionRegistry required")
        if (not callable(getattr(adapter, "screen_identity", None)) or
                not callable(getattr(adapter, "changed_regions", None))):
            raise ChangeRouteError("AUSENTE", "merged VisionEngineAdapter detector required")
        if not callable(publisher):
            raise ChangeRouteError("AUSENTE", "publisher callback required")
        for name, value in (("max_rgb_bytes", max_rgb_bytes), ("max_work_units", max_work_units),
                            ("max_lanes", max_lanes), ("max_history", max_history),
                            ("max_batch_items", max_batch_items)):
            _number(value, name, minimum=1)
        self._registry = registry
        self._adapter = adapter
        self._publisher = publisher
        self._max_rgb_bytes = max_rgb_bytes
        self._max_work_units = max_work_units
        self._max_lanes = max_lanes
        self._max_history = max_history
        self._max_batch_items = max_batch_items
        self._lock = RLock()
        self._lanes: dict[tuple[str, str, int, int], _Lane] = {}
        self._publication_count = 0

    def _live(self, observation: Observation) -> None:
        """Fence STOP, retired generations, old epochs and replaced aliases."""
        try:
            record = self._registry.resolve(observation.handle)
        except RegistryError as exc:
            raise ChangeRouteError("STALE_GENERATION", str(exc)) from exc
        if (_identity(record.handle) != _identity(observation.handle) or
                type(record.epoch) is not int or record.epoch != observation.epoch or
                record.closed):
            raise ChangeRouteError("STALE_EPOCH", "registry epoch or handle is no longer active")

    def _admitted(self, observation: Observation) -> None:
        try:
            self._registry.admit_observation(
                observation.handle, observation.frame,
                state=observation.state, dpr_ppm=observation.dpr_ppm,
            )
        except RegistryError as exc:
            raise ChangeRouteError("UNREGISTERED_FRAME", str(exc)) from exc

    def route(self, observation: object) -> RoutedEvidence | None:
        """Publish a checked frame exactly once, or refuse without publication.

        Precondition: registry.admit_frame and registry.admit_observation were
        used for this live handle/epoch/sequence. The producer may pass a
        structurally equivalent Observation class, not just the local class.
        Frame bytes never leave RAM except through the caller's publisher.
        """
        sample = _snapshot(observation)
        key = (*_identity(sample.handle), sample.epoch)
        dimensions = (sample.frame.width, sample.frame.height)
        with self._lock:
            self._live(sample)
            lane = self._lanes.get(key)
            if lane is not None:
                if dimensions != lane.dimensions:
                    raise ChangeRouteError("SAME_EPOCH_RESIZE", "geometry requires explicit epoch bump")
                if sample.frame_seq <= lane.last_seq:
                    if sample.frame_seq < lane.first_seq:
                        raise ChangeRouteError("STALE_SEQUENCE", "sequence predates first accepted frame")
                    prior = lane.history.get(sample.frame_seq)
                    if prior is not None and prior.source_sha256 != sample.frame.digest:
                        raise ChangeRouteError("REPLAY_CONFLICT", "same claim has different content")
                    return None  # idempotent, even for older accepted claims outside the window
                if sample.frame_seq != lane.last_seq + 1:
                    raise ChangeRouteError("FRAME_GAP", "missing predecessor in this lane")
            if lane is None and len(self._lanes) >= self._max_lanes:
                raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "bounded lane capacity exhausted")
            count = sample.frame.width * sample.frame.height * 3
            # Includes pixel digest and finite detector scans; not a CPU timing.
            work_units = 4 * count
            if count > self._max_rgb_bytes or work_units > self._max_work_units:
                raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "pixel work admission ceiling exhausted")
            self._admitted(sample)
            current = Frame(sample.frame.width, sample.frame.height,
                            sample.frame.rgb.tobytes(order="C"))
            try:
                screen_id = self._adapter.screen_identity(current)
                if type(screen_id) is not str or not _HEX16.fullmatch(screen_id):
                    raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "detector identity missing")
                if lane is None:
                    found = ()
                    status = "BASELINE"
                else:
                    previous = Frame(lane.last_frame.width, lane.last_frame.height,
                                     lane.last_frame.rgb.tobytes(order="C"))
                    found = _regions(self._adapter.changed_regions(previous, current),
                                     sample.frame.width, sample.frame.height)
                    status = "UNCHANGED" if sample.frame.digest == lane.last_digest else "CHANGED"
                    if status == "UNCHANGED" and found:
                        raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "detector contradicts identical pixels")
            except ChangeRouteError:
                raise
            except (DetectorExhausted, TimeoutError) as exc:
                raise ChangeRouteError("INSUFFICIENT_EVIDENCE", f"detector budget exhausted: {exc}") from exc
            except Exception as exc:
                raise ChangeRouteError("DETECTOR_ERROR", f"{type(exc).__name__}: {exc}") from exc
            # Registry can advance/retire concurrently under its own lock.
            self._live(sample)
            self._admitted(sample)
            event = RoutedEvidence(
                sample.handle, sample.epoch, sample.frame_seq, sample.frame,
                sample.frame.digest, screen_id, status, found, work_units,
            )
            if lane is None:
                lane = _Lane(sample.frame_seq, sample.frame_seq, sample.frame.digest,
                             dimensions, sample.frame)
                self._lanes[key] = lane
            else:
                lane.last_seq = sample.frame_seq
                lane.last_digest = sample.frame.digest
                lane.last_frame = sample.frame
            lane.history[sample.frame_seq] = event
            if len(lane.history) > self._max_history:
                lane.history.popitem(last=False)
            try:
                self._publisher(event)
            except Exception as exc:
                # The publication could have happened before the exception;
                # keep the claim and do not send again under uncertainty.
                raise ChangeRouteError("PUBLICATION_UNCERTAIN", f"{type(exc).__name__}: {exc}") from exc
            self._publication_count += 1
            return event

    def route_many(self, observations: Iterable[object], *, runner=None) -> tuple[RouteOutcome, ...]:
        """Run at most 4 independent lanes per wave; return submission order.

        A runner supports ``map(function, jobs)`` and may yield groups in reverse
        completion order. A rejection remains local to its target's row.
        """
        items = list(observations)
        if not items:
            raise ChangeRouteError("AUSENTE", "batch has no observations")
        if len(items) > self._max_batch_items:
            raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "bounded batch capacity exhausted")
        groups = OrderedDict()
        for idx, item in enumerate(items):
            try:
                target = (*_identity(item.handle), _number(item.epoch, "epoch"))
            except (ChangeRouteError, AttributeError):
                target = ("invalid", idx)
            groups.setdefault(target, []).append((idx, item))
        ordered: list[RouteOutcome | None] = [None] * len(items)

        def handle_group(job):
            completed = []
            for idx, obs in job:
                try:
                    event = self.route(obs)
                    completed.append(RouteOutcome(idx, event, None))
                except ChangeRouteError as exc:
                    completed.append(RouteOutcome(idx, None, str(exc)))
            return completed

        values = list(groups.values())
        if runner is None:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="change-route") as pool:
                for start in range(0, len(values), 4):
                    for completed in pool.map(handle_group, values[start:start + 4]):
                        for outcome in completed:
                            ordered[outcome.index] = outcome
        else:
            for start in range(0, len(values), 4):
                for completed in runner.map(handle_group, values[start:start + 4]):
                    for outcome in completed:
                        if (type(outcome.index) is not int or
                                not 0 <= outcome.index < len(ordered) or
                                ordered[outcome.index] is not None):
                            raise ChangeRouteError("RUNNER_ERROR", "duplicate or malformed completion")
                        ordered[outcome.index] = outcome
        if any(item is None for item in ordered):
            raise ChangeRouteError("RUNNER_ERROR", "missing completion")
        return tuple(ordered)

    def lane_history(self, handle: object, epoch: int) -> tuple[RoutedEvidence, ...]:
        """Return a defensive, bounded immutable record of a specific lane."""
        key = (*_identity(handle), _number(epoch, "epoch"))
        with self._lock:
            lane = self._lanes.get(key)
            return tuple(lane.history.values()) if lane is not None else ()

    @property
    def publication_count(self) -> int:
        with self._lock:
            return self._publication_count


def c_score_identity_preconditions(evidence: Iterable[object]) -> CScoreIdentityPrerequisites:
    """Require one complete full-identity, same-geometry, contiguous RGB window.

    These are *only* C-score identity prerequisites. The actual calibration,
    detector exhaustion reporting and score computation are AUSENTE from the
    merged adapter; callers MUST NOT interpret the result as a settled verdict.
    """
    events = list(evidence)
    if len(events) < 2:
        raise ChangeRouteError("AUSENTE", "C-score requires at least two samples")
    bound = None
    dimensions = None
    last_seq = None
    seqs = []
    digests = []
    for item in events:
        try:
            ident = _identity(item.handle)
            epoch = _number(item.epoch, "epoch")
            seq = _number(item.frame_seq, "frame_seq")
            frame = item.frame
            frame_ident = _identity(frame.handle)
            source = item.source_sha256
            width, height = frame.width, frame.height
            fr_epoch, fr_seq = frame.epoch, frame.frame_seq
            rgb = frame.rgb
            status = item.status
        except AttributeError as exc:
            raise ChangeRouteError("INVALID_EVIDENCE", "C-score evidence fields missing") from exc
        if status not in ("BASELINE", "CHANGED", "UNCHANGED"):
            raise ChangeRouteError("INSUFFICIENT_EVIDENCE", "incomplete change verdict")
        if (frame_ident, fr_epoch, fr_seq) != (ident, epoch, seq):
            raise ChangeRouteError("SIBLING_EVIDENCE", "score window has foreign frame")
        _number(width, "width", minimum=1)
        _number(height, "height", minimum=1)
        if type(source) is not str or not _HEX64.fullmatch(source) or (
                hashlib.sha256(_owned_rgb(rgb, width, height)).hexdigest() != source
                or frame.digest != source):
            raise ChangeRouteError("BAD_DIGEST", "C-score pixel evidence mismatch")
        identity = (*ident, epoch)
        if bound is None:
            bound, dimensions = identity, (width, height)
        elif bound != identity or dimensions != (width, height):
            raise ChangeRouteError("SIBLING_EVIDENCE", "C-score window crossed an identity/epoch/geometry")
        if last_seq is not None and seq != last_seq + 1:
            raise ChangeRouteError("FRAME_GAP", "C-score samples not contiguous")
        last_seq = seq
        seqs.append(seq)
        digests.append(source)
    return CScoreIdentityPrerequisites(Handle(*bound[:3]), bound[3],
                                       tuple(seqs), tuple(digests))
