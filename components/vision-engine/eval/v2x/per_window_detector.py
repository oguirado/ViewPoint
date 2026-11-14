"""Per-handle/epoch deterministic detector for already-classified S2 frames.

No I/O or cross-target histories. Inputs are owned before invoking plugins;
per-target lanes serialize a stream while independent lanes may execute in
parallel. Absence of validated evidence is not a positive readiness signal.
"""
from collections import OrderedDict
from contextvars import copy_context
from dataclasses import dataclass, field
import hashlib
from threading import Lock

import numpy as np


KINDS = frozenset(("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"))


class DetectorInputError(ValueError):
    """Invalid target, epoch, geometry, sequence or evidence."""


class MissingEvidenceError(DetectorInputError):
    """An injected plugin did not supply the evidence needed for a claim."""


@dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int

    def __post_init__(self):
        if self.kind not in KINDS or not isinstance(self.canonical_id, str) or not self.canonical_id:
            raise DetectorInputError("unsupported target kind or empty canonical_id")
        if type(self.generation) is not int or self.generation < 0:
            raise DetectorInputError("generation must be a nonnegative unbounded integer")


@dataclass(frozen=True)
class ChangeResult:
    handle: Handle
    epoch: int
    settled: bool
    claims: np.ndarray
    ops: dict


@dataclass
class _Stream:
    epoch: int
    grid: object
    tracker: object
    veto: object
    last_seq: int | None = None
    prev_rgb: np.ndarray | None = None
    prev_y: np.ndarray | None = None
    prev_digest: bytes | None = None
    history: list = field(default_factory=list)
    cache: OrderedDict = field(default_factory=OrderedDict)
    clean_run: int = 0


@dataclass
class _Lane:
    lock: object = field(default_factory=Lock)
    stream: _Stream | None = None


def _nonnegative_int(value, name):
    if type(value) is not int or value < 0:
        raise DetectorInputError(name + " must be a nonnegative integer")


def _handle(value):
    # Other repositories can supply records with the same fields (no imports).
    if isinstance(value, Handle):
        return value
    try:
        return Handle(value.kind, value.canonical_id, value.generation)
    except (AttributeError, TypeError) as exc:
        raise DetectorInputError("expected Handle-shaped value") from exc


def _count(module):
    value = getattr(module, "OPS", None)
    if type(value) is int and value >= 0:
        return value
    return None


class PerWindowDetector:
    """Stream bank; a gap or higher geometry epoch resets only its own lane.

    ``gm`` implements estimate_global_motion/consensus_tiles, ``fl`` exports a
    FlickerTracker and ops_scope/update_with_ops, and ``veto`` is a factory
    accepting Grid and returning an independent BaselineFlickerVeto-compatible
    instance. The defaults are the production eval components.
    """

    SETTLE_CONSECUTIVE = 3
    MAX_CACHE_PAIRS = 32

    def __init__(self, *, gm=None, fl=None, veto=None):
        if veto is not None and not callable(veto):
            raise DetectorInputError("veto must be an independent per-stream factory")
        self._gm = gm
        self._fl = fl
        self._veto_factory = veto
        self._lock = Lock()
        self._lanes = {}

    def _lane(self, handle):
        with self._lock:
            if handle not in self._lanes:
                self._lanes[handle] = _Lane()
            return self._lanes[handle]

    def reset_window(self, handle):
        handle = _handle(handle)
        lane = self._lane(handle)
        with lane.lock:
            lane.stream = None

    def _new_stream(self, epoch, grid):
        from . import fl_v3
        from eval.v2eval.postpass import BaselineFlickerVeto

        fl = self._fl if self._fl is not None else fl_v3
        cls = getattr(fl, "FlickerTracker", None)
        if cls is None:
            raise MissingEvidenceError("FL must export FlickerTracker")
        tracker = cls(grid.N, max_half=64)
        factory = self._veto_factory or BaselineFlickerVeto
        veto = factory(grid)
        if not callable(getattr(veto, "update", None)):
            raise MissingEvidenceError("veto must implement update")
        return _Stream(epoch=epoch, grid=grid, tracker=tracker, veto=veto)

    def update(self, handle, tiles, rgb, *, epoch: int, frame_seq: int) -> ChangeResult:
        from visioncore import desc
        from visioncore.grid import Grid

        handle = _handle(handle)
        _nonnegative_int(epoch, "epoch")
        _nonnegative_int(frame_seq, "frame_seq")
        if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8
                or rgb.ndim != 3 or rgb.shape[2] != 3 or min(rgb.shape[:2]) < 1):
            raise DetectorInputError("rgb must be a nonempty HxWx3 uint8 ndarray")
        # Snapshot BEFORE any plugin receives input. No retained view of caller RAM.
        owned_rgb = np.array(rgb, dtype=np.uint8, copy=True, order="C")
        owned_rgb.flags.writeable = False
        grid = Grid(owned_rgb.shape[1], owned_rgb.shape[0])
        if (not isinstance(tiles, np.ndarray) or tiles.dtype != desc.TILE_DTYPE
                or tiles.shape != (grid.N,) or not np.array_equal(tiles["morton"], grid.mortons)):
            raise DetectorInputError("tiles must match the exact S2 grid, dtype and Morton order")
        baseline = tiles.copy()
        baseline.flags.writeable = False
        lane = self._lane(handle)
        with lane.lock:  # Same target serial; no registry mutex during plugin calls.
            st = lane.stream
            if st is not None:
                if epoch < st.epoch:
                    raise DetectorInputError("stale geometry epoch")
                if epoch == st.epoch:
                    if (grid.W, grid.H) != (st.grid.W, st.grid.H):
                        raise DetectorInputError("geometry change requires new epoch")
                    if frame_seq <= st.last_seq:
                        raise DetectorInputError("non-increasing local frame_seq")
                if epoch != st.epoch or frame_seq != st.last_seq + 1:
                    st = None  # Stream gap/geometry reset is local to this handle.
            if st is None:
                st = self._new_stream(epoch, grid)
                lane.stream = st
            try:
                result = self._step(st, handle, baseline, owned_rgb, frame_seq)
            except BaseException:
                # A partially advanced tracker/veto cannot be used for a claim.
                lane.stream = None
                raise
            return result

    def _step(self, st, handle, baseline, rgb, frame_seq):
        from visioncore import desc
        from visioncore.desc import luma_of_padded
        from . import gm_bands_hv, fl_v3

        gm = self._gm if self._gm is not None else gm_bands_hv
        fl = self._fl if self._fl is not None else fl_v3
        grid = st.grid
        padded = np.array(grid.pad(rgb), copy=True, order="C")
        # Eager content hashes, never native lazy/output-filled S2 hashes.
        eager, _ = desc.describe_padded(padded, grid, lazy_xh=False)
        if (eager.dtype != baseline.dtype or eager.shape != baseline.shape
                or not np.array_equal(eager["morton"], baseline["morton"])):
            raise MissingEvidenceError("eager descriptor order invalid")
        eager = eager.copy()
        eager["cls"] = baseline["cls"]
        eager.flags.writeable = False
        y = np.ascontiguousarray(luma_of_padded(padded), dtype=np.uint8).copy()
        y.flags.writeable = False
        digest = hashlib.sha256(rgb.tobytes(order="C")).digest()

        decision = st.veto.update(baseline, rgb, frame_id=frame_seq)
        mask = getattr(decision, "mask", None)
        if (not isinstance(mask, np.ndarray) or mask.dtype != np.bool_
                or mask.shape != (grid.N,) or np.any(mask & (baseline["cls"] != 4))):
            raise MissingEvidenceError("veto must be a baseline-only Boolean mask")
        # For injected vetoes too: exact current RGB descriptors take precedence.
        if st.veto is None:
            raise MissingEvidenceError("missing per-stream veto evidence")

        def fl_call():
            if callable(getattr(fl, "update_with_ops", None)):
                result, ops = fl.update_with_ops(st.tracker, eager["xh64"])
            elif callable(getattr(fl, "ops_scope", None)):
                with fl.ops_scope() as read_ops:
                    result = st.tracker.update(eager["xh64"])
                    ops = read_ops()
            else:
                result, ops = st.tracker.update(eager["xh64"]), None
            states, _ = result
            if not isinstance(states, np.ndarray) or states.shape != (grid.N,):
                raise MissingEvidenceError("FL tracker must return states for every tile")
            return states, ops

        fl_states, fl_ops = copy_context().run(fl_call)
        gm_mask = np.zeros(grid.N, dtype=np.bool_)
        dx = dy = 0
        gm_ops = 0
        if st.prev_y is not None and not np.array_equal(st.prev_y, y):
            key = (st.prev_digest, digest)
            cached = st.cache.get(key)
            if cached is not None:
                st.cache.move_to_end(key)
                gm_mask, dx, dy = cached
                gm_mask = gm_mask.copy()
            else:
                def gm_call():
                    estimator = getattr(gm, "estimate_global_motion", None)
                    consensus = getattr(gm, "consensus_tiles", None)
                    if not callable(estimator) or not callable(consensus):
                        raise MissingEvidenceError("GM requires estimate_global_motion and consensus_tiles")
                    vector = estimator(st.prev_y, y, max(grid.W, grid.H) - 1)
                    ops = _count(gm)
                    m = np.zeros(grid.N, dtype=np.bool_)
                    x = z = 0
                    if vector is not None:
                        if (not isinstance(vector, (tuple, list)) or len(vector) < 2
                                or any(type(k) is not int for k in vector[:2])):
                            raise MissingEvidenceError("GM returned invalid displacement")
                        x, z = vector[:2]
                        if (x, z) != (0, 0):
                            c = consensus(st.prev_y, y, grid.s, x, z)
                            if (not isinstance(c, np.ndarray) or c.dtype != np.bool_
                                    or c.shape != (grid.gh, grid.gw)):
                                raise MissingEvidenceError("GM consensus must match grid")
                            m = c.ravel()[grid.flat].copy()
                            second = _count(gm)
                            ops = None if ops is None or second is None else ops + second
                    return m, x, z, ops

                gm_mask, dx, dy, gm_ops = copy_context().run(gm_call)
                st.cache[key] = (gm_mask.copy(), dx, dy)
                if len(st.cache) > self.MAX_CACHE_PAIRS:
                    st.cache.popitem(last=False)

        output = baseline.copy()
        # First apply ONLY the evidence-backed baseline FLICKER demotion.
        output["cls"][mask] = 2
        # Positive FL never overrides independently certified GM or baseline MOVED.
        fm = (fl_states == 4) & ~gm_mask & (baseline["cls"] != 3)
        output["cls"][fm] = 4
        output["mv_dx"][fm] = 0
        output["mv_dy"][fm] = 0
        output["cls"][gm_mask] = 3
        output["mv_dx"][gm_mask] = dx
        output["mv_dy"][gm_mask] = dy
        output.flags.writeable = False

        # No wall clock is present in this API: frame freshness means exact
        # local adjacency. Settling never bridges a reset, missing frame,
        # changed baseline, output claim, or changed RGB evidence.
        clean = (st.prev_rgb is not None and np.array_equal(st.prev_rgb, rgb)
                 and not mask.any() and not gm_mask.any() and not fm.any()
                 and not np.isin(baseline["cls"], (2, 3, 4, 6)).any())
        st.clean_run = st.clean_run + 1 if clean else 0
        settled = st.clean_run >= self.SETTLE_CONSECUTIVE
        st.last_seq = frame_seq
        st.prev_rgb, st.prev_y, st.prev_digest = rgb, y, digest
        st.history.append((frame_seq, eager))
        if len(st.history) > 2:
            st.history.pop(0)
        return ChangeResult(handle, st.epoch, settled, output,
                            {"gm": gm_ops, "fl": fl_ops})
