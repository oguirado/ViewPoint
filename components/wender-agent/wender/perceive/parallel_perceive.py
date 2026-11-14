# @module {"id":"wa.perceive.parallel_perceive","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_adapter"],"book":"HB-WA-s1-perceive"}
"""Target-incarnation-scoped, RAM-only concurrent PERCEIVE batches.

Results retain their original input index even when a runner completes groups out
of order. Each (handle, geometry epoch) runs as an independent temporal sequence;
no transport, focus or actuation operations occur inside a worker.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import hashlib

import numpy as np

from .vision_adapter import FakeVisionEngine, Frame, fuse_regions

try:
    from .session_registry import Handle, FrameRef, Observation, PerceptionResult
except ModuleNotFoundError as exc:
    # Only the absent provider is eligible for a structural fallback. An import
    # error INSIDE an installed provider is never concealed.
    if exc.name != __package__ + ".session_registry":
        raise

    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None
        error: str | None


_TARGET_KINDS = frozenset({"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"})
_MAX_WORKERS = 4

# @anchor:wa.perceive.parallel_perceive.ObservationError
class ObservationError(ValueError):
    """Invalid or unbound input: a single item is rejected, not the batch."""


@lru_cache(maxsize=1)
def _default_runner():
    """One bounded, persistent pool, rather than a new pool per observation."""
    return ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="perceive")


def _exact_nonnegative(name, value):
    if type(value) is not int or value < 0:
        raise ObservationError("%s must be a non-negative integer" % name)
    return value


def _handle_key(handle):
    try:
        kind, ident, generation = handle.kind, handle.canonical_id, handle.generation
    except AttributeError as exc:
        raise ObservationError("handle fields missing") from exc
    if kind not in _TARGET_KINDS or not isinstance(ident, str) or not ident:
        raise ObservationError("unsupported or empty target handle")
    _exact_nonnegative("generation", generation)
    return kind, ident, generation


def _prepare(observation):
    """Validate the complete binding; take an owned, readonly pixel snapshot."""
    try:
        original = observation.handle
        key = _handle_key(original)
        epoch = _exact_nonnegative("epoch", observation.epoch)
        seq = _exact_nonnegative("frame_seq", observation.frame_seq)
        dpr = observation.dpr_ppm
        state = observation.state
        ref = observation.frame
    except AttributeError as exc:
        raise ObservationError("observation fields missing") from exc
    if type(dpr) is not int or dpr <= 0:
        raise ObservationError("dpr_ppm must be a positive integer")
    if not isinstance(state, dict) or not isinstance(state.get("nodes"), list):
        raise ObservationError("AX state must contain a nodes list")
    viewport = state.get("viewport")
    if isinstance(viewport, dict) and "dpr_ppm" in viewport and viewport["dpr_ppm"] != dpr:
        raise ObservationError("AX viewport DPR differs from observation DPR")
    if "handle" in state:
        if _handle_key(_structural_handle(state["handle"])) != key:
            raise ObservationError("state target handle mismatch")
    if "epoch" in state and state["epoch"] != epoch:
        raise ObservationError("state geometry epoch mismatch")
    try:
        frame_key = _handle_key(ref.handle)
        frame_epoch, frame_seq = ref.epoch, ref.frame_seq
        height, width, rgb, digest = ref.height, ref.width, ref.rgb, ref.digest
    except AttributeError as exc:
        raise ObservationError("frame reference fields missing") from exc
    if frame_key != key or frame_epoch != epoch or frame_seq != seq:
        raise ObservationError("frame handle/epoch/sequence does not match observation")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ObservationError("frame dimensions must be positive integers")
    if isinstance(rgb, np.ndarray):
        if (rgb.dtype != np.uint8 or rgb.shape != (height, width, 3)
                or not rgb.flags.c_contiguous):
            raise ObservationError("frame must be contiguous uint8 RGB[H,W,3]")
        pixels = rgb
    elif isinstance(rgb, (bytes, bytearray)):
        if len(rgb) != height * width * 3:
            raise ObservationError("RGB byte count does not match geometry")
        pixels = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
    else:
        raise ObservationError("frame rgb must be RGB ndarray or raw RGB24 bytes")
    # Always copy, including read-only ndarray and immutable bytes: a verified
    # stream slot may be released or reused before the runner begins its work.
    owned = np.array(pixels, dtype=np.uint8, order="C", copy=True)
    owned.flags.writeable = False
    actual_digest = hashlib.sha256(owned.tobytes(order="C")).hexdigest()
    if (not isinstance(digest, str) or len(digest) != 64
            or digest != actual_digest):
        raise ObservationError("frame content SHA-256 mismatch")
    handle = Handle(*key)
    owned_ref = FrameRef(handle, epoch, seq, width, height, owned, actual_digest)
    return Observation(handle, epoch, seq, dpr, deepcopy(state), owned_ref)


def _structural_handle(value):
    """Allow a dict-binding in an AX envelope without importing another repo."""
    if isinstance(value, dict):
        try:
            return Handle(value["kind"], value["canonical_id"], value["generation"])
        except KeyError as exc:
            raise ObservationError("state handle binding incomplete") from exc
    return value


def _error(handle, kind, exc):
    return _bound_error(handle, "%s:%s:%s" % (kind, type(exc).__name__, str(exc)))


def _describe_group(job):
    """Process a single incarnation in strictly ascending LOCAL frame order."""
    rows, adapter = job
    engine = FakeVisionEngine() if adapter is None else adapter
    previous = None
    completed = []
    for idx, obs in rows:
        frame = Frame(obs.frame.width, obs.frame.height, obs.frame.rgb.tobytes(order="C"))
        try:
            identity = engine.screen_identity(frame)
            if not isinstance(identity, str) or len(identity) != 16 or any(
                c not in "0123456789abcdef" for c in identity
            ):
                raise ValueError("adapter returned invalid screen identity")
            regions = [] if previous is None else engine.changed_regions(previous, frame)
            if not isinstance(regions, list):
                raise ValueError("adapter returned non-list regions")
            target = {"kind": obs.handle.kind, "canonical_id": obs.handle.canonical_id,
                      "generation": obs.handle.generation}
            fusion = fuse_regions({"state": obs.state, "handle": target, "epoch": obs.epoch,
                                   "dpr_ppm": obs.dpr_ppm}, regions, obs.dpr_ppm,
                                  handle=obs.handle, epoch=obs.epoch)
            descriptor = None
            if callable(getattr(engine, "screen_fingerprint", None)):
                desc = engine.screen_fingerprint(frame)
                if not isinstance(desc, (bytes, bytearray)):
                    raise ValueError("adapter descriptor must be bytes")
                descriptor = bytes(desc).hex()
            state = deepcopy(obs.state)
            state["_perception"] = {"handle": target, "epoch": obs.epoch,
                                    "frame_seq": obs.frame_seq, "dpr_ppm": obs.dpr_ppm,
                                    "screen_identity": identity, "descriptor_hex": descriptor,
                                    "regions": deepcopy(regions), "fusion": fusion}
            completed.append((idx, PerceptionResult(obs.handle, Observation(
                obs.handle, obs.epoch, obs.frame_seq, obs.dpr_ppm, state, obs.frame), None)))
            previous = frame
        except Exception as exc:  # one failed item must not poison the next item
            completed.append((idx, _error(obs.handle, "PERCEPTION_ERROR", exc)))
            previous = None  # never bridge a failed frame with a later frame
    return completed


# @anchor:wa.perceive.parallel_perceive.perceive_many
def perceive_many(observations, *, adapter, runner=None):
    """Return one typed PerceptionResult for each input, in original input order.

    The injected runner has map(callable, jobs); its completion order may
    differ from submission order. A failed runner only marks undelivered items.
    """
    items = list(observations)
    results = [None] * len(items)
    groups = {}
    last_seq = {}
    geometry = {}
    for idx, raw in enumerate(items):
        handle = getattr(raw, "handle", None)
        try:
            obs = _prepare(raw)
            key = (*_handle_key(obs.handle), obs.epoch)
            dimensions = (obs.frame.width, obs.frame.height)
            if key in geometry and geometry[key] != dimensions:
                raise ObservationError("geometry changed without changing epoch")
            previous_seq = last_seq.get(key, -1)
            if obs.frame_seq <= previous_seq:
                raise ObservationError("non-increasing frame_seq in target incarnation")
            geometry[key] = dimensions
            last_seq[key] = obs.frame_seq
            groups.setdefault(key, []).append((idx, obs))
        except (ObservationError, ValueError, TypeError) as exc:
            results[idx] = _error(handle, "VALIDATION_ERROR", exc)
    active_runner = _default_runner() if runner is None else runner
    # Limit in-flight admission to the same four groups as the shared pool,
    # rather than letting executor.map pre-submit an unbounded queue.
    rows = list(groups.values())
    for offset in range(0, len(rows), _MAX_WORKERS):
        jobs = [(row, adapter) for row in rows[offset:offset + _MAX_WORKERS]]
        submitted = {idx: obs for group_rows, _ in jobs for idx, obs in group_rows}
        try:
            for group_results in active_runner.map(_describe_group, jobs):
                for idx, result in group_results:
                    if type(idx) is not int or idx not in submitted:
                        raise RuntimeError("runner returned invalid result index")
                    if results[idx] is not None:
                        raise RuntimeError("runner returned duplicate completed result")
                    results[idx] = _bind_result(result, submitted[idx])
            if any(results[idx] is None for idx in submitted):
                raise RuntimeError("runner omitted completed result")
        except Exception as exc:
            for group_rows, _ in jobs:
                for idx, obs in group_rows:
                    if results[idx] is None:
                        results[idx] = _error(obs.handle, "RUNNER_ERROR", exc)
    return results


def _bound_error(handle, message):
    """Keep an attributable handle, or an explicitly unbound error-only receipt."""
    try:
        handle = Handle(*_handle_key(handle))
    except (ObservationError, ValueError, TypeError):
        handle = None
    return PerceptionResult(handle, None, message)


def _bind_result(result, expected):
    """Admit only a complete result for the submitted observation at this index.

    A runner may change delivery order, but cannot replace a target, frame, or
    geometry epoch. Previously admitted completions survive a later bad result.
    """
    if not isinstance(result, PerceptionResult):
        raise RuntimeError("runner returned a non-PerceptionResult")
    if _handle_key(result.handle) != _handle_key(expected.handle):
        raise RuntimeError("runner result target handle mismatch")
    if result.error is not None:
        if not isinstance(result.error, str) or not result.error or result.observation is not None:
            raise RuntimeError("runner returned an invalid error receipt")
        return PerceptionResult(expected.handle, None, result.error)
    if result.observation is None:
        raise RuntimeError("runner returned no observation or error")
    observed = _prepare(result.observation)
    if (_handle_key(observed.handle), observed.epoch, observed.frame_seq,
            observed.dpr_ppm, observed.frame.width, observed.frame.height,
            observed.frame.digest) != (
            _handle_key(expected.handle), expected.epoch, expected.frame_seq,
            expected.dpr_ppm, expected.frame.width, expected.frame.height,
            expected.frame.digest):
        raise RuntimeError("runner result observation binding mismatch")
    metadata = observed.state.get("_perception")
    if (not isinstance(metadata, dict)
            or _handle_key(_structural_handle(metadata.get("handle"))) != _handle_key(expected.handle)
            or (metadata.get("epoch"), metadata.get("frame_seq"), metadata.get("dpr_ppm"))
            != (expected.epoch, expected.frame_seq, expected.dpr_ppm)):
        raise RuntimeError("runner result perception binding mismatch")
    return PerceptionResult(expected.handle, observed, None)
