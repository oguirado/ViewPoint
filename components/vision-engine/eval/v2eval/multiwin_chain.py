"""Multi-window perception chain with per-handle/epoch reducer isolation and settle oracle.

Partitions each stream reducer (S1/S2 baseline, BaselineFlickerVeto, FlickerTracker,
GM estimator, and luma/content caches) by (handle, epoch). Requires eager RGB
evidence before history capture, resets only the stream experiencing a sequence
gap or epoch advancement, and evaluates is_settled strictly from explicitly
configured consecutive valid quiet frames and fake monotonic age.
Missing or unsupported evidence is UNSETTLED (False) and never grants action
authorization.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import inspect
import sys
from threading import Lock

import numpy as np
from visioncore import change, desc
from visioncore.desc import luma_of_padded
from visioncore.grid import Grid

from eval.v2eval import postpass as _postpass
from eval.v2eval.postpass import (
    BaselineFlickerVeto,
    ChangeResult,
    DetectorInputError,
    EpochValidationError,
    GeometryValidationError,
    Handle,
    IdentityMismatchError,
    KINDS,
    METADATA_LOCK_ORDER,
    MissingEvidenceError,
    PostPass,
    SUPPORTED_TARGET_KINDS,
    SequenceValidationError,
    TimestampValidationError,
    _coerce_handle,
    _is_strict_int,
    _require_nonnegative_int,
    _require_positive_int,
    canonical_actuation_resource,
    canonical_handle_key,
)
from eval.v2x.per_window_detector import PerWindowDetector


def _admit_rgb(rgb, width: int, height: int) -> np.ndarray:
    """Validate and snapshot an owned C-contiguous read-only uint8 RGB array."""
    if isinstance(rgb, (bytes, bytearray, memoryview)):
        expected = width * height * 3
        if len(rgb) != expected:
            raise DetectorInputError(f"RGB buffer byte length {len(rgb)} != {expected}")
        arr = np.frombuffer(rgb, dtype=np.uint8).reshape((height, width, 3)).copy(order="C")
        arr.flags.writeable = False
        return arr
    if not isinstance(rgb, np.ndarray):
        raise DetectorInputError("rgb must be a numpy.ndarray or contiguous byte buffer")
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape != (height, width, 3):
        raise DetectorInputError(f"rgb must be uint8 of shape {(height, width, 3)}")
    if not rgb.flags.c_contiguous:
        raise DetectorInputError("rgb array must be C-contiguous")
    if rgb.flags.writeable:
        arr = np.array(rgb, dtype=np.uint8, copy=True, order="C")
        arr.flags.writeable = False
        return arr
    return rgb


@dataclass(frozen=True)
class TargetRecord:
    handle: Handle
    transport: object
    owned: bool
    epoch: int
    session_id: str | None
    attachment_owned: bool
    closed: bool

    def __post_init__(self):
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        if not isinstance(self.owned, bool) or not isinstance(self.attachment_owned, bool) or not isinstance(self.closed, bool):
            raise DetectorInputError("owned, attachment_owned, and closed must be bool")
        _require_nonnegative_int(self.epoch, "epoch")
        if self.session_id is not None and (not isinstance(self.session_id, str) or not self.session_id):
            raise DetectorInputError("session_id must be a non-empty str or None")


@dataclass(frozen=True)
class FrameRef:
    handle: Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: object
    digest: str

    def __post_init__(self):
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        _require_nonnegative_int(self.epoch, "epoch")
        _require_nonnegative_int(self.frame_seq, "frame_seq")
        _require_positive_int(self.width, "width")
        _require_positive_int(self.height, "height")
        admitted = _admit_rgb(self.rgb, self.width, self.height)
        object.__setattr__(self, "rgb", admitted)
        if not isinstance(self.digest, str) or not self.digest:
            raise DetectorInputError("digest must be a non-empty str")
        if len(self.digest) == 64:
            try:
                int(self.digest, 16)
            except ValueError as exc:
                raise DetectorInputError("64-char digest must be valid lowercase hex") from exc
            actual_hex = hashlib.sha256(admitted.tobytes(order="C")).hexdigest()
            if self.digest.lower() != actual_hex:
                raise DetectorInputError("FrameRef digest does not match RGB content sha256")


@dataclass(frozen=True)
class Observation:
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: FrameRef

    def __post_init__(self):
        h = _coerce_handle(self.handle)
        object.__setattr__(self, "handle", h)
        _require_nonnegative_int(self.epoch, "epoch")
        _require_nonnegative_int(self.frame_seq, "frame_seq")
        _require_positive_int(self.dpr_ppm, "dpr_ppm")
        if not isinstance(self.state, dict):
            raise DetectorInputError("state must be a dict")
        if self.frame is None:
            raise DetectorInputError("frame must be a FrameRef")
        try:
            fh = _coerce_handle(self.frame.handle)
            fe = self.frame.epoch
            fs = self.frame.frame_seq
        except AttributeError as exc:
            raise DetectorInputError("frame must provide handle, epoch, frame_seq") from exc
        if fh != h:
            raise IdentityMismatchError("Observation.handle and FrameRef.handle mismatch")
        if fe != self.epoch:
            raise EpochValidationError("Observation.epoch and FrameRef.epoch mismatch")
        if fs != self.frame_seq:
            raise SequenceValidationError("Observation.frame_seq and FrameRef.frame_seq mismatch")


@dataclass(frozen=True)
class Receipt:
    action_id: str
    handle: Handle
    status: str
    reason: str | None = None

    def __post_init__(self):
        if not isinstance(self.action_id, str) or not self.action_id:
            raise DetectorInputError("action_id must be a non-empty str")
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        if not isinstance(self.status, str) or not self.status:
            raise DetectorInputError("status must be a non-empty str")
        if self.reason is not None and not isinstance(self.reason, str):
            raise DetectorInputError("reason must be a str or None")


@dataclass(frozen=True)
class ActionRequest:
    handle: Handle
    action_id: str
    epoch: int
    payload: dict
    cost: int
    deadline_ns: int

    def __post_init__(self):
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        if not isinstance(self.action_id, str) or not self.action_id:
            raise DetectorInputError("action_id must be a non-empty str")
        _require_nonnegative_int(self.epoch, "epoch")
        if not isinstance(self.payload, dict):
            raise DetectorInputError("payload must be a dict")
        _require_nonnegative_int(self.cost, "cost")
        _require_nonnegative_int(self.deadline_ns, "deadline_ns")


@dataclass(frozen=True)
class ActorLease:
    handle: Handle
    owner: str
    generation: int
    nonce: bytes
    expires_ns: int

    def __post_init__(self):
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        if not isinstance(self.owner, str) or not self.owner:
            raise DetectorInputError("owner must be a non-empty str")
        _require_nonnegative_int(self.generation, "generation")
        if not isinstance(self.nonce, bytes) or len(self.nonce) == 0:
            raise DetectorInputError("nonce must be non-empty bytes")
        _require_nonnegative_int(self.expires_ns, "expires_ns")


@dataclass(frozen=True)
class FocusLease:
    lease: ActorLease
    domain: str
    epoch: int

    def __post_init__(self):
        if not isinstance(self.lease, ActorLease):
            raise DetectorInputError("lease must be an ActorLease")
        if not isinstance(self.domain, str) or not self.domain:
            raise DetectorInputError("domain must be a non-empty str")
        _require_nonnegative_int(self.epoch, "epoch")


@dataclass(frozen=True)
class StreamSpec:
    width: int
    height: int
    slots: int
    quota_bytes: int

    def __post_init__(self):
        _require_positive_int(self.width, "width")
        _require_positive_int(self.height, "height")
        _require_positive_int(self.slots, "slots")
        _require_positive_int(self.quota_bytes, "quota_bytes")


@dataclass(frozen=True)
class StreamToken:
    handle: Handle
    epoch: int

    def __post_init__(self):
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        _require_nonnegative_int(self.epoch, "epoch")


@dataclass(frozen=True)
class Ticket:
    action_id: str
    handle: Handle

    def __post_init__(self):
        if not isinstance(self.action_id, str) or not self.action_id:
            raise DetectorInputError("action_id must be a non-empty str")
        object.__setattr__(self, "handle", _coerce_handle(self.handle))


@dataclass(frozen=True)
class PerceptionResult:
    handle: Handle
    observation: Observation | None
    error: str | None = None

    def __post_init__(self):
        h = _coerce_handle(self.handle)
        object.__setattr__(self, "handle", h)
        if self.observation is None and (not isinstance(self.error, str) or not self.error):
            raise DetectorInputError("PerceptionResult without observation requires non-empty error")
        if self.observation is not None and _coerce_handle(self.observation.handle) != h:
            raise IdentityMismatchError("PerceptionResult.handle and Observation.handle mismatch")


@dataclass(frozen=True)
class WindowFrames:
    handle: Handle
    epoch: int
    frames: tuple
    frame_seqs: tuple
    dpr_ppm: int

    def __post_init__(self):
        h = _coerce_handle(self.handle)
        object.__setattr__(self, "handle", h)
        _require_nonnegative_int(self.epoch, "epoch")
        _require_positive_int(self.dpr_ppm, "dpr_ppm")
        frames_t = tuple(self.frames) if isinstance(self.frames, (tuple, list)) else None
        seqs_t = tuple(self.frame_seqs) if isinstance(self.frame_seqs, (tuple, list)) else None
        if frames_t is None or seqs_t is None or len(frames_t) != len(seqs_t) or len(frames_t) == 0:
            raise DetectorInputError("frames and frame_seqs must be non-empty sequences of equal length")
        prev_seq = None
        admitted_frames = []
        norm_seqs = []
        expected_shape = None
        for f, seq in zip(frames_t, seqs_t):
            _require_nonnegative_int(seq, "frame_seq")
            if prev_seq is not None and seq <= prev_seq:
                raise SequenceValidationError("WindowFrames.frame_seqs must be strictly increasing")
            prev_seq = seq
            norm_seqs.append(seq)
            if isinstance(f, FrameRef):
                if f.handle != h:
                    raise IdentityMismatchError("FrameRef.handle mismatch in WindowFrames")
                if f.epoch != self.epoch:
                    raise EpochValidationError("FrameRef.epoch mismatch in WindowFrames")
                if f.frame_seq != seq:
                    raise SequenceValidationError("FrameRef.frame_seq mismatch in WindowFrames")
                shape = (f.height, f.width, 3)
                if expected_shape is not None and shape != expected_shape:
                    raise GeometryValidationError("geometry mismatch across frames in WindowFrames")
                expected_shape = shape
                admitted_frames.append(f)
            elif isinstance(f, np.ndarray):
                if f.dtype != np.uint8 or f.ndim != 3 or f.shape[2] != 3 or min(f.shape[:2]) < 1:
                    raise DetectorInputError("frame array must be HxWx3 uint8")
                if expected_shape is not None and f.shape != expected_shape:
                    raise GeometryValidationError("geometry mismatch across frames in WindowFrames")
                expected_shape = f.shape
                admitted = _admit_rgb(f, f.shape[1], f.shape[0])
                admitted_frames.append(admitted)
            else:
                raise DetectorInputError("each frame in WindowFrames must be FrameRef or uint8 ndarray")
        object.__setattr__(self, "frames", tuple(admitted_frames))
        object.__setattr__(self, "frame_seqs", tuple(norm_seqs))


@dataclass(frozen=True)
class WindowDescription:
    handle: Handle
    epoch: int
    frame_seqs: tuple
    descriptors: tuple
    regions: tuple
    error: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        _require_nonnegative_int(self.epoch, "epoch")
        seqs_t = tuple(self.frame_seqs) if isinstance(self.frame_seqs, (tuple, list)) else None
        desc_t = tuple(self.descriptors) if isinstance(self.descriptors, (tuple, list)) else None
        regs_t = tuple(self.regions) if isinstance(self.regions, (tuple, list)) else None
        if seqs_t is None or desc_t is None or regs_t is None:
            raise DetectorInputError("frame_seqs, descriptors, and regions must be tuples")
        prev_seq = None
        for s in seqs_t:
            _require_nonnegative_int(s, "frame_seq")
            if prev_seq is not None and s <= prev_seq:
                raise SequenceValidationError("WindowDescription.frame_seqs must be strictly increasing")
            prev_seq = s
        if self.error is None and len(seqs_t) != len(desc_t):
            raise DetectorInputError("descriptors length must match frame_seqs when error is None")
        if self.error is not None and (not isinstance(self.error, str) or not self.error):
            raise DetectorInputError("error must be a non-empty str or None")
        object.__setattr__(self, "frame_seqs", seqs_t)
        object.__setattr__(self, "descriptors", desc_t)
        object.__setattr__(self, "regions", regs_t)


for _sym in (
    "_admit_rgb", "TargetRecord", "FrameRef", "Observation", "Receipt",
    "ActionRequest", "ActorLease", "FocusLease", "StreamSpec", "StreamToken",
    "Ticket", "PerceptionResult", "WindowFrames", "WindowDescription",
):
    setattr(_postpass, _sym, globals()[_sym])
    _pwd_mod = sys.modules.get("eval.v2x.per_window_detector")
    if _pwd_mod is not None and not hasattr(_pwd_mod, _sym):
        setattr(_pwd_mod, _sym, globals()[_sym])


class ChainConfigError(DetectorInputError):
    """Invalid MultiWindowChain configuration."""


@dataclass
class _ChainStream:
    handle: Handle
    epoch: int
    width: int
    height: int
    dpr_ppm: int
    grid: Grid = field(init=False)
    last_seq: int | None = None
    last_valid_ns: int | None = None
    prev_padded: np.ndarray | None = None
    prev_raw: np.ndarray | None = None
    prev_rgb: np.ndarray | None = None
    prev_digest: str | None = None
    s2_state: object | None = None
    quiet_count: int = 0
    has_unresolved_gap: bool = False
    has_unresolved_motion: bool = False
    has_regressed_time: bool = False

    def __post_init__(self):
        self.grid = Grid(self.width, self.height)


@dataclass
class _ChainLane:
    lock: Lock = field(default_factory=Lock)
    stream: _ChainStream | None = None
    max_epoch: int | None = None
    last_valid_ns: int | None = None


@contextmanager
def acquire_target_lanes(lanes_by_handle: dict, handles):
    """Acquire multiple per-target stream locks in canonical order; release in reverse."""
    ordered = sorted({_coerce_handle(h) for h in handles}, key=canonical_handle_key)
    acquired = []
    try:
        for h in ordered:
            lane = lanes_by_handle[h]
            lane.lock.acquire()
            acquired.append(lane.lock)
        yield ordered
    finally:
        for lk in reversed(acquired):
            lk.release()


class MultiWindowChain:
    """Multi-window perception chain and settlement oracle."""

    def __init__(self, quiet_frames: int, max_age_ns: int, *, detector=None):
        if not _is_strict_int(quiet_frames) or quiet_frames < 1:
            raise ChainConfigError("quiet_frames must be a positive integer")
        if not _is_strict_int(max_age_ns) or max_age_ns < 0:
            raise ChainConfigError("max_age_ns must be a nonnegative integer")
        self.quiet_frames = quiet_frames
        self.max_age_ns = max_age_ns
        self.detector = PerWindowDetector() if detector is None else detector
        self._bank_lock = Lock()
        self._lanes: dict[Handle, _ChainLane] = {}

    def _get_lane(self, handle: Handle) -> _ChainLane:
        with self._bank_lock:
            if handle not in self._lanes:
                self._lanes[handle] = _ChainLane()
            return self._lanes[handle]

    def _reset_detector_window(self, handle: Handle) -> None:
        reset_fn = getattr(self.detector, "reset_window", None)
        if callable(reset_fn):
            reset_fn(handle)
        else:
            fallback_reset = getattr(self.detector, "reset", None)
            if callable(fallback_reset):
                try:
                    fallback_reset(handle)
                except TypeError:
                    pass

    def reset_window(self, handle: Handle) -> None:
        """Reset only the stream state for ``handle``."""
        h = _coerce_handle(handle)
        lane = self._get_lane(h)
        with lane.lock:
            lane.stream = None
            self._reset_detector_window(h)

    @staticmethod
    def canonical_actuation_resource(handle: Handle) -> tuple[str, str]:
        """Return the canonical actuation resource key for ``handle``."""
        return canonical_actuation_resource(handle)

    def _validate_observation(self, observation: Observation) -> tuple[Handle, int, int, int, dict, FrameRef, np.ndarray]:
        if observation is None:
            raise MissingEvidenceError("observation must not be None")
        try:
            raw_handle = observation.handle
            epoch = observation.epoch
            frame_seq = observation.frame_seq
            dpr_ppm = observation.dpr_ppm
            state = observation.state
            frame = observation.frame
        except AttributeError as exc:
            raise DetectorInputError("observation must provide Observation fields") from exc

        handle = _coerce_handle(raw_handle)
        _require_nonnegative_int(epoch, "observation.epoch")
        _require_nonnegative_int(frame_seq, "observation.frame_seq")
        _require_positive_int(dpr_ppm, "observation.dpr_ppm")
        if not isinstance(state, dict):
            raise DetectorInputError("observation.state must be a dict")
        if frame is None:
            raise MissingEvidenceError("observation.frame must not be None")

        try:
            frame_handle = _coerce_handle(frame.handle)
            frame_epoch = frame.epoch
            frame_fseq = frame.frame_seq
            width = frame.width
            height = frame.height
            rgb = frame.rgb
            digest = frame.digest
        except AttributeError as exc:
            raise DetectorInputError("observation.frame must provide FrameRef fields") from exc

        if frame_handle != handle:
            raise IdentityMismatchError("Observation.handle and FrameRef.handle mismatch")
        _require_nonnegative_int(frame_epoch, "frame.epoch")
        if frame_epoch != epoch:
            raise EpochValidationError("Observation.epoch and FrameRef.epoch mismatch")
        if not _is_strict_int(frame_fseq) or frame_fseq != frame_seq:
            raise SequenceValidationError("Observation.frame_seq and FrameRef.frame_seq mismatch")
        _require_positive_int(width, "frame.width")
        _require_positive_int(height, "frame.height")
        if not isinstance(digest, str) or not digest:
            raise MissingEvidenceError("frame.digest must be a non-empty string")

        admitted = _admit_rgb(rgb, width, height)
        owned_rgb = np.array(admitted, dtype=np.uint8, copy=True, order="C")
        owned_rgb.flags.writeable = False
        return handle, epoch, frame_seq, dpr_ppm, state, frame, owned_rgb

    def _compute_eager_baseline(self, st: _ChainStream, owned_rgb: np.ndarray, state: dict) -> np.ndarray:
        grid = st.grid
        padded = np.array(grid.pad(owned_rgb), copy=True, order="C")
        y = luma_of_padded(padded)
        raw, words_eq = desc.describe_padded(
            padded, grid, words_prev=st.prev_padded, prev_tiles=st.prev_raw,
            y_plane=y, lazy_xh=False)
        if st.s2_state is None:
            st.s2_state = change.ClassState.init(raw)
            s2_tiles, st.s2_state, _ = change.classify_first(st.s2_state, raw, y_cur=y)
        else:
            s2_tiles, st.s2_state, _ = change.classify_tiles(
                st.s2_state, raw, words_eq, y_cur=y, grid=(grid.s, grid.gw, grid.gh))
        st.prev_padded = padded
        st.prev_raw = raw

        supplied = state.get("tiles")
        if supplied is None:
            supplied = state.get("s2_tiles")
        if supplied is None:
            supplied = state.get("baseline")

        if supplied is not None:
            if not isinstance(supplied, np.ndarray):
                raise DetectorInputError("supplied state tiles must be a numpy.ndarray")
            if supplied.dtype == desc.TILE_DTYPE:
                if supplied.shape != (grid.N,) or not np.array_equal(supplied["morton"], grid.mortons):
                    raise DetectorInputError("supplied TILE_DTYPE tiles must match grid and Morton order")
                out = supplied.copy()
                out.flags.writeable = False
                return out
            if supplied.size == grid.N and supplied.dtype.kind in ("i", "u"):
                out = s2_tiles.copy()
                flat_cls = supplied.ravel()
                if np.any((flat_cls < 0) | (flat_cls > 6)):
                    raise DetectorInputError("supplied tile classes out of range [0, 6]")
                out["cls"] = flat_cls.astype(np.uint8)
                out.flags.writeable = False
                return out
            raise DetectorInputError("unsupported supplied tiles format in observation.state")

        out = s2_tiles.copy()
        out.flags.writeable = False
        return out

    def _invoke_detector(
        self, handle: Handle, s2_tiles: np.ndarray, owned_rgb: np.ndarray,
        *, epoch: int, frame_seq: int, observation: Observation, now_ns: int | None
    ) -> ChangeResult:
        update_fn = getattr(self.detector, "update", None)
        if not callable(update_fn):
            raise MissingEvidenceError("detector must provide a callable update method")
        sig = inspect.signature(update_fn)
        pos_params = [
            p for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
        if len(pos_params) >= 3 or has_varargs:
            res = update_fn(handle, s2_tiles, owned_rgb, epoch=epoch, frame_seq=frame_seq)
        elif "now_ns" in sig.parameters:
            res = update_fn(observation, now_ns=now_ns)
        else:
            res = update_fn(observation)

        if res is None:
            raise MissingEvidenceError("detector returned None")
        claims = getattr(res, "claims", None)
        ops = getattr(res, "ops", None)
        if not isinstance(claims, np.ndarray) or claims.size == 0:
            raise MissingEvidenceError("detector must return non-empty numpy claims")
        if not isinstance(ops, dict):
            raise MissingEvidenceError("detector must return an ops dict")
        for v in ops.values():
            if v is None or not _is_strict_int(v) or v < 0:
                raise MissingEvidenceError("detector ops must contain valid nonnegative integer counts")
        return res

    @staticmethod
    def _has_unresolved_activity(
        claims: np.ndarray, prev_rgb: np.ndarray | None, owned_rgb: np.ndarray, state: dict
    ) -> bool:
        if claims.dtype == desc.TILE_DTYPE:
            cls = claims["cls"]
            if np.isin(cls, (change.C_CHANGED, change.C_MOVED, change.C_FLICKER, change.C_DRIFT)).any():
                return True
            if (claims["mv_dx"] != 0).any() or (claims["mv_dy"] != 0).any():
                return True
        elif np.any(claims != 0):
            return True

        state_claims = state.get("claims")
        if isinstance(state_claims, np.ndarray):
            if state_claims.dtype == desc.TILE_DTYPE:
                if np.isin(state_claims["cls"], (2, 3, 4, 6)).any():
                    return True
            elif np.any(state_claims != 0):
                return True

        if prev_rgb is not None and not np.array_equal(prev_rgb, owned_rgb):
            return True
        return False

    def _is_stream_settled(self, st: _ChainStream | None, *, now_ns: int) -> bool:
        if st is None or st.last_valid_ns is None or now_ns < st.last_valid_ns:
            return False
        if now_ns - st.last_valid_ns > self.max_age_ns:
            return False
        if st.has_unresolved_gap or st.has_unresolved_motion or st.has_regressed_time:
            return False
        return bool(st.quiet_count >= self.quiet_frames)

    def update(self, observation: Observation, *, now_ns: int | None = None) -> ChangeResult:
        """Process an observation for its target stream and return ``ChangeResult``."""
        if now_ns is not None and (not _is_strict_int(now_ns) or now_ns < 0):
            raise TimestampValidationError("now_ns must be a nonnegative integer or None")

        handle, epoch, frame_seq, dpr_ppm, state, frame, owned_rgb = self._validate_observation(observation)
        lane = self._get_lane(handle)

        with lane.lock:
            if lane.max_epoch is not None and epoch < lane.max_epoch:
                raise EpochValidationError("stale or regressing observation epoch")

            if now_ns is not None and lane.last_valid_ns is not None and now_ns < lane.last_valid_ns:
                if lane.stream is not None:
                    lane.stream.quiet_count = 0
                    lane.stream.has_regressed_time = True
                raise TimestampValidationError("regressing monotonic timestamp now_ns")

            st = lane.stream
            is_gap = False
            if st is not None:
                if epoch > st.epoch:
                    self._reset_detector_window(handle)
                    st = None
                else:
                    if (frame.width, frame.height) != (st.width, st.height) or dpr_ppm != st.dpr_ppm:
                        raise GeometryValidationError("geometry or dpr_ppm change requires a new epoch")
                    if frame_seq <= st.last_seq:
                        raise SequenceValidationError("non-increasing local frame_seq")
                    if frame_seq != st.last_seq + 1:
                        is_gap = True
                        self._reset_detector_window(handle)
                        st = None

            if st is None:
                st = _ChainStream(
                    handle=handle, epoch=epoch, width=frame.width,
                    height=frame.height, dpr_ppm=dpr_ppm)
                lane.stream = st
                lane.max_epoch = epoch

            try:
                s2_tiles = self._compute_eager_baseline(st, owned_rgb, state)
                det_res = self._invoke_detector(
                    handle, s2_tiles, owned_rgb, epoch=epoch,
                    frame_seq=frame_seq, observation=observation, now_ns=now_ns)
                claims = np.array(det_res.claims, copy=True)
                claims.flags.writeable = False
                ops = dict(det_res.ops)

                has_activity = self._has_unresolved_activity(claims, st.prev_rgb, owned_rgb, state)

                st.last_seq = frame_seq
                st.prev_rgb = owned_rgb
                st.prev_digest = frame.digest
                st.has_unresolved_gap = is_gap
                st.has_unresolved_motion = has_activity
                st.has_regressed_time = False

                if is_gap or has_activity or now_ns is None:
                    st.quiet_count = 0
                else:
                    st.quiet_count += 1

                if now_ns is not None:
                    st.last_valid_ns = now_ns
                    lane.last_valid_ns = now_ns
                    settled = self._is_stream_settled(st, now_ns=now_ns)
                else:
                    settled = False

                return ChangeResult(
                    handle=handle, epoch=epoch, settled=bool(settled),
                    claims=claims, ops=ops)
            except BaseException:
                lane.stream = None
                self._reset_detector_window(handle)
                raise

    def is_settled(self, handle: Handle, *, now_ns: int) -> bool:
        """Return ``True`` iff ``handle`` has settled within ``max_age_ns`` at ``now_ns``."""
        if now_ns is None or not _is_strict_int(now_ns) or now_ns < 0:
            return False
        try:
            h = _coerce_handle(handle)
        except DetectorInputError:
            return False
        with self._bank_lock:
            lane = self._lanes.get(h)
        if lane is None:
            return False
        with lane.lock:
            if lane.last_valid_ns is not None and now_ns < lane.last_valid_ns:
                return False
            return bool(self._is_stream_settled(lane.stream, now_ns=now_ns))


__all__ = [
    "ActionRequest", "ActorLease", "BaselineFlickerVeto", "ChainConfigError",
    "ChangeResult", "DetectorInputError", "EpochValidationError", "FocusLease",
    "FrameRef", "GeometryValidationError", "Handle", "IdentityMismatchError",
    "KINDS", "METADATA_LOCK_ORDER", "MissingEvidenceError", "MultiWindowChain",
    "Observation", "PerWindowDetector", "PerceptionResult", "PostPass",
    "Receipt", "SUPPORTED_TARGET_KINDS", "SequenceValidationError", "StreamSpec",
    "StreamToken", "TargetRecord", "Ticket", "TimestampValidationError",
    "WindowDescription", "WindowFrames", "acquire_target_lanes",
    "canonical_actuation_resource", "canonical_handle_key",
]
