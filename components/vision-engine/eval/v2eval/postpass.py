"""Postpasses over S2 tiles, with frames supplied in RAM, and per-window reducer isolation.

Global: any tile exactly consistent with a nonzero estimated vector (including
partial visible edge tiles). Flicker: every tracker declaration, including held
phases. Subtile: baseline CHANGED tiles only, radius s-1, exact changed blocks.
Legacy combined application order: global, then subtile, then flicker (later
claims overwrite earlier claims). Opt-in FL v3 gives GM, then subtile, then FL
priority. Legacy callers remain positive-only. An explicit baseline FLICKER
veto runs before positive claims; tracker abstention is no veto.
Claims are computed independently from the baseline, without ground truth.
"""
import ast
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
import hashlib
import importlib.util
import inspect
import sys
import textwrap
from threading import Lock
import time
import types

import numpy as np
from eval.v2proto_flicker import FlickerTracker
from eval.v2proto_global_motion import consensus_tiles, estimate_global_motion
from eval.v2proto_subtile import subtile_motion
from visioncore import change, desc
from visioncore.desc import luma_of_padded
from visioncore.grid import Grid
from . import native

VARIANTS = ("baseline", "global_motion", "flicker", "subtile", "combined")
SUPPORTED_TARGET_KINDS = ("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window")
KINDS = frozenset(SUPPORTED_TARGET_KINDS)
METADATA_LOCK_ORDER = (
    "scheduler_or_fence", "registry", "budget", "target_actor_metadata",
    "focus_domain", "stream_bank", "per_stream_metadata",
)


class DetectorInputError(ValueError):
    """Invalid target, epoch, geometry, sequence or evidence."""


class MissingEvidenceError(DetectorInputError):
    """An injected plugin or observation did not supply required evidence."""


class SequenceValidationError(DetectorInputError):
    """Non-increasing or mismatched frame sequence."""


class IdentityMismatchError(DetectorInputError):
    """Handle or epoch mismatch across bound records."""


class EpochValidationError(DetectorInputError):
    """Stale or regressing epoch."""


class GeometryValidationError(DetectorInputError):
    """Geometry or DPR mismatch within the same stream epoch."""


class TimestampValidationError(DetectorInputError):
    """Invalid or regressing monotonic timestamp."""


def _is_strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_nonnegative_int(value, name: str) -> int:
    if not _is_strict_int(value) or value < 0:
        raise DetectorInputError(f"{name} must be a nonnegative integer")
    return value


def _require_positive_int(value, name: str) -> int:
    if not _is_strict_int(value) or value <= 0:
        raise DetectorInputError(f"{name} must be a positive integer")
    return value


def _coerce_handle(value) -> "Handle":
    if isinstance(value, Handle):
        return value
    try:
        return Handle(value.kind, value.canonical_id, value.generation)
    except (AttributeError, TypeError) as exc:
        raise DetectorInputError("expected Handle-shaped value") from exc


def canonical_handle_key(handle) -> tuple[str, str, int]:
    """Return canonical sort key for same-rank target handles."""
    h = _coerce_handle(handle)
    return (h.kind, h.canonical_id, h.generation)


def canonical_actuation_resource(handle) -> tuple[str, str]:
    """Return canonical actuation destination resource key."""
    h = _coerce_handle(handle)
    cid = h.canonical_id.strip()
    for prefix in ("capsule:", "xvfb_screen:", "x11_window:", "screen:"):
        if cid.startswith(prefix):
            cid = cid[len(prefix):]
    for suffix in ("/xvfb_screen", ":xvfb_screen", "/screen", ":screen", "/child", ":child"):
        if cid.endswith(suffix) and len(cid) > len(suffix):
            cid = cid[:-len(suffix)]
    if h.kind in ("capsule", "xvfb_screen", "x11_window"):
        return ("desktop_destination", cid)
    return (h.kind, cid)


@dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int

    def __post_init__(self):
        if (not isinstance(self.kind, str) or self.kind not in KINDS
                or not isinstance(self.canonical_id, str) or not self.canonical_id):
            raise DetectorInputError("unsupported target kind or empty canonical_id")
        if not _is_strict_int(self.generation) or self.generation < 0:
            raise DetectorInputError("generation must be a nonnegative unbounded integer")

    def canonical_actuation_resource(self) -> tuple[str, str]:
        return canonical_actuation_resource(self)


@dataclass(frozen=True)
class ChangeResult:
    handle: Handle
    epoch: int
    settled: bool
    claims: np.ndarray
    ops: dict

    def __post_init__(self):
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        _require_nonnegative_int(self.epoch, "epoch")
        if not isinstance(self.settled, bool):
            raise DetectorInputError("settled must be a bool")
        if not isinstance(self.claims, np.ndarray):
            raise DetectorInputError("claims must be a numpy.ndarray")
        if not isinstance(self.ops, dict):
            raise DetectorInputError("ops must be a dict")


def _ensure_fl_contextvar_accounting():
    """Ensure fl_blinkprior and fl_v3 use ContextVar per-call op accounting (INV-M5-2)."""
    from eval.v2x import fl_blinkprior, fl_v3
    if hasattr(fl_blinkprior, "ops_scope") and hasattr(fl_v3, "update_with_ops"):
        return
    ops_var = ContextVar("fl_blinkprior_ops", default=0)
    fl_blinkprior._ops = ops_var
    fl_blinkprior.__dict__.pop("OPS", None)

    def _fl_getattr(name):
        if name == "OPS":
            return fl_blinkprior._ops.get()
        raise AttributeError(name)

    def _add_ops(count):
        fl_blinkprior._ops.set(fl_blinkprior._ops.get() + int(count))

    @contextmanager
    def ops_scope():
        token = fl_blinkprior._ops.set(0)
        try:
            yield lambda: fl_blinkprior._ops.get()
        finally:
            fl_blinkprior._ops.reset(token)

    def update_with_ops(tracker, hashes):
        def counted():
            with ops_scope() as read_ops:
                res = tracker.update(hashes)
                return res, read_ops()
        return copy_context().run(counted)

    def reset_ops():
        fl_blinkprior._ops.set(0)

    class _OpsRewriter(ast.NodeTransformer):
        def visit_Global(self, node):
            names = [n for n in node.names if n != "OPS"]
            return ast.Global(names=names) if names else ast.Pass()

        def visit_AugAssign(self, node):
            self.generic_visit(node)
            if isinstance(node.target, ast.Name) and node.target.id == "OPS" and isinstance(node.op, ast.Add):
                return ast.Expr(value=ast.Call(
                    func=ast.Name(id="_add_ops", ctx=ast.Load()), args=[node.value], keywords=[]))
            return node

    for target_obj, attr_name in ((fl_blinkprior, "_as_u64_hashes"), (fl_blinkprior.FlickerTracker, "update")):
        fn = getattr(target_obj, attr_name)
        tree = ast.fix_missing_locations(_OpsRewriter().visit(ast.parse(textwrap.dedent(inspect.getsource(fn)))))
        ns = dict(fl_blinkprior.__dict__, _add_ops=_add_ops)
        exec(compile(tree, fl_blinkprior.__file__, "exec"), ns)
        setattr(target_obj, attr_name, ns[attr_name])

    fl_blinkprior.__getattr__ = _fl_getattr
    fl_blinkprior._add_ops = _add_ops
    fl_blinkprior.ops_scope = ops_scope
    fl_blinkprior.update_with_ops = update_with_ops
    fl_blinkprior.reset_ops = reset_ops
    fl_v3.reset_ops = reset_ops
    fl_v3.ops_scope = ops_scope
    fl_v3.update_with_ops = update_with_ops


_ensure_fl_contextvar_accounting()


def arrays(tiles):
    return {k: np.asarray(tiles[k], dtype=np.int64).copy() for k in ("cls", "mv_dx", "mv_dy")}


def apply_claims(baseline, claims, *, flicker_veto=None):
    """Copy the baseline, apply an explicit class-4 veto, then positive claims."""
    out = {k: a.copy() for k, a in baseline.items()}
    if flicker_veto is not None:
        if (not isinstance(flicker_veto, np.ndarray)
                or flicker_veto.dtype != np.bool_
                or flicker_veto.shape != baseline["cls"].shape):
            raise ValueError("flicker_veto must be a boolean baseline-shaped array")
        out["cls"][flicker_veto & (baseline["cls"] == 4)] = 2
    for claim in claims:
        mask = claim["mask"]
        out["cls"][mask] = claim["cls"]
        out["mv_dx"][mask] = np.asarray(claim["mv_dx"])[mask]
        out["mv_dy"][mask] = np.asarray(claim["mv_dy"])[mask]
    return out


class BaselineFlickerVeto:
    """Own fresh RGB-content evidence for one contiguous stream and fixed grid."""

    def __init__(self, grid):
        self.grid = grid
        self._partitions = {}
        self.reset()

    def reset(self, handle=None, epoch: int | None = None):
        if handle is not None:
            h = _coerce_handle(handle)
            for k in [k for k in self._partitions if k[0] == h and (epoch is None or k[1] == epoch)]:
                del self._partitions[k]
            return
        self.history = []
        self.previous_rgb = None
        self.last_frame_id = None
        self.current_tiles = None
        self.discontinuous = False
        self._partitions.clear()

    def update(self, tiles, rgb, *, frame_id=None, handle=None, epoch: int = 0):
        if handle is not None:
            h = _coerce_handle(handle)
            _require_nonnegative_int(epoch, "epoch")
            key = (h, epoch)
            sub = self._partitions.get(key)
            if sub is None:
                sub = BaselineFlickerVeto(self.grid)
                self._partitions[key] = sub
            decision = sub.update(tiles, rgb, frame_id=frame_id)
            self.history = list(sub.history)
            self.previous_rgb = sub.previous_rgb
            self.last_frame_id = sub.last_frame_id
            self.current_tiles = sub.current_tiles
            self.discontinuous = sub.discontinuous
            return decision

        from eval.v2x.fl_v3 import Reason, VetoDecision, veto_flicker
        grid = self.grid
        if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.shape != (grid.H, grid.W, 3):
            raise ValueError("RAM RGB uint8 frame must match grid")
        if (not isinstance(tiles, np.ndarray) or tiles.dtype != desc.TILE_DTYPE
                or tiles.shape != (grid.N,) or not np.array_equal(tiles["morton"], grid.mortons)):
            raise ValueError("baseline tiles must match the grid's TILE_DTYPE order")
        if frame_id is None:
            frame_id = 0 if self.last_frame_id is None else self.last_frame_id + 1
        if isinstance(frame_id, (bool, np.bool_)) or not isinstance(frame_id, (int, np.integer)) or frame_id < 0:
            raise ValueError("frame_id must be a nonnegative integer")
        frame_id = int(frame_id)
        discontinuous = self.last_frame_id is not None and frame_id != self.last_frame_id + 1
        if discontinuous:
            self.reset()
        self.discontinuous = discontinuous

        padded = grid.pad(rgb).copy()
        y = luma_of_padded(padded)
        previous = self.history[-1][1] if self.history else None
        raw, words_eq = desc.describe_padded(
            padded, grid, words_prev=self.previous_rgb, prev_tiles=previous, y_plane=y, lazy_xh=False)
        current = raw.copy()
        current["cls"] = tiles["cls"]
        old = self.history[-2] if len(self.history) == 2 else None
        recent = self.history[-1] if self.history else None
        evidence = {"evidence": {
            "frame_ids": (frame_id - 2, frame_id - 1, frame_id),
            "history_valid": np.ones(grid.N, dtype=np.bool_),
            "hash_valid": np.ones((3, grid.N), dtype=np.bool_),
        }}
        args = (current, old[1] if old else None, recent[1] if recent else None,
                words_eq, (grid.s, grid.gw, grid.gh))
        pixels = {"y_prev2": old[2] if old else None,
                  "y_prev1": recent[2] if recent else None, "y_cur": y, "params": evidence}
        decision = veto_flicker(*args, **pixels)
        if old is not None and (current["cls"] == 4).any():
            proposal = change.global_vector(y, recent[2], grid.s)
            if proposal is not None and proposal != (0, 0):
                motion = veto_flicker(*args, gm_vector=proposal, **pixels)
                f6 = motion.mask & (motion.reasons == Reason.VETO_F6)
                mask, reasons = decision.mask.copy(), decision.reasons.copy()
                mask |= f6
                reasons[f6] = Reason.VETO_F6
                decision = VetoDecision(mask, reasons)

        for owned in (padded, y, raw, current):
            owned.flags.writeable = False
        self.history = (self.history + [(frame_id, raw, y)])[-2:]
        self.previous_rgb = padded
        self.last_frame_id = frame_id
        self.current_tiles = current
        return decision


def _read_module_ops(module):
    value = getattr(module, "OPS", None)
    return value if type(value) is int and value >= 0 else None


class PostPass:
    """Postpass reducer supporting both single-stream and per-(handle, epoch) partitioned streams."""

    def __init__(self, grid, *, accelerated=True, cache_size=128, baseline_flicker_veto=False):
        self.grid = grid
        self.accelerated = accelerated
        self.cache_size = cache_size
        self.baseline_flicker_veto = baseline_flicker_veto
        self.cache = OrderedDict()
        if baseline_flicker_veto:
            from eval.v2x.fl_v3 import FlickerTracker as Tracker
        else:
            Tracker = FlickerTracker
        self.tracker = Tracker(grid.N, max_half=64)
        self.veto = BaselineFlickerVeto(grid) if baseline_flicker_veto else None
        self.prev = self.prev_key = self.last_frame_id = None
        self.last_ops = {"gm": 0, "fl": 0}
        self._window_locks = {}
        self._window_streams = {}
        self._bank_lock = Lock()
        self.wall_seconds = {name: float(0) for name in VARIANTS[1:4]}
        self.calls = {name: 0 for name in VARIANTS[1:4]}
        self.cache_hits = 0

    def reset(self, handle=None, epoch: int | None = None):
        """Start a new stream without changing cumulative diagnostic counters."""
        if handle is not None:
            h = _coerce_handle(handle)
            with self._bank_lock:
                for k in [k for k in self._window_streams if k[0] == h and (epoch is None or k[1] == epoch)]:
                    del self._window_streams[k]
            return
        self.tracker.reset()
        if self.veto is not None:
            self.veto.reset()
        self.prev = self.prev_key = self.last_frame_id = None
        self.cache.clear()
        with self._bank_lock:
            self._window_streams.clear()

    def reset_window(self, handle):
        """Reset only entries matching handle across all epochs (INV-M5-1)."""
        self.reset(handle=handle)

    def _get_window_postpass(self, handle: Handle, epoch: int, grid: Grid) -> tuple[Lock, "PostPass"]:
        with self._bank_lock:
            if handle not in self._window_locks:
                self._window_locks[handle] = Lock()
            lane_lock = self._window_locks[handle]
            for (h_key, ep_key) in list(self._window_streams.keys()):
                if h_key == handle and ep_key > epoch:
                    raise EpochValidationError("stale geometry epoch")
            key = (handle, epoch)
            sub = self._window_streams.get(key)
            if sub is not None:
                if (sub.grid.W, sub.grid.H) != (grid.W, grid.H):
                    raise GeometryValidationError("geometry change requires new epoch")
            else:
                for (h_key, ep_key) in list(self._window_streams.keys()):
                    if h_key == handle and ep_key < epoch:
                        del self._window_streams[(h_key, ep_key)]
                sub = PostPass(grid, accelerated=self.accelerated, cache_size=self.cache_size,
                               baseline_flicker_veto=self.baseline_flicker_veto)
                self._window_streams[key] = sub
            return lane_lock, sub

    def _empty(self, cls):
        return {"mask": np.zeros(self.grid.N, dtype=bool), "cls": cls,
                "mv_dx": np.zeros(self.grid.N, dtype=np.int64),
                "mv_dy": np.zeros(self.grid.N, dtype=np.int64)}

    def update(self, tiles, rgb, *, frame_key=None, frame_id=None,
               flicker_veto=None, handle=None, epoch: int = 0):
        if handle is not None:
            h = _coerce_handle(handle)
            _require_nonnegative_int(epoch, "epoch")
            if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
                raise ValueError("RAM RGB uint8 frame must match grid")
            win_grid = Grid(rgb.shape[1], rgb.shape[0]) if self.grid is None else self.grid
            lane_lock, sub = self._get_window_postpass(h, epoch, win_grid)
            with lane_lock:
                return sub.update(tiles, rgb, frame_key=frame_key, frame_id=frame_id, flicker_veto=flicker_veto)

        grid = self.grid
        if not isinstance(rgb, np.ndarray) or rgb.shape != (grid.H, grid.W, 3) or rgb.dtype != np.uint8:
            raise ValueError("RAM RGB uint8 frame must match grid")
        owned_rgb = np.array(rgb, dtype=np.uint8, copy=True, order="C")
        owned_rgb.flags.writeable = False
        base = arrays(tiles)
        if flicker_veto is not None:
            apply_claims(base, [], flicker_veto=flicker_veto)
        if frame_id is not None and self.veto is None:
            if isinstance(frame_id, (bool, np.bool_)) or not isinstance(frame_id, (int, np.integer)) or frame_id < 0:
                raise ValueError("frame_id must be a nonnegative integer")
            fid = int(frame_id)
            if self.last_frame_id is not None and fid != self.last_frame_id + 1:
                self.tracker.reset()
                self.prev = self.prev_key = None
                self.cache.clear()
            self.last_frame_id = fid

        claims = {"global_motion": self._empty(3), "subtile": self._empty(3), "flicker": self._empty(4)}
        start = time.perf_counter()
        tracker_tiles = tiles
        if self.veto is not None:
            decision = self.veto.update(tiles, owned_rgb, frame_id=frame_id)
            if self.veto.discontinuous:
                self.tracker.reset()
                self.prev = self.prev_key = None
                self.cache.clear()
            tracker_tiles = self.veto.current_tiles
            flicker_veto = decision.mask if flicker_veto is None else (flicker_veto | decision.mask)

        from eval.v2x import fl_v3 as _fl_v3_mod
        if self.veto is not None and callable(getattr(_fl_v3_mod, "update_with_ops", None)):
            (states, _), fl_ops = _fl_v3_mod.update_with_ops(self.tracker, tracker_tiles["xh64"])
        else:
            states, _ = copy_context().run(self.tracker.update, tracker_tiles["xh64"])
            fl_ops = 0

        claims["flicker"]["mask"] = states == 4
        if flicker_veto is not None:
            claims["flicker"]["veto_mask"] = flicker_veto.copy()
        self.wall_seconds["flicker"] += time.perf_counter() - start
        self.calls["flicker"] += 1
        start = time.perf_counter()
        cur = np.ascontiguousarray(luma_of_padded(owned_rgb), dtype=np.uint8)
        cur.flags.writeable = False
        conversion = time.perf_counter() - start
        gm_ops = 0
        if self.prev is not None:
            same = np.array_equal(self.prev, cur)
            key = (self.prev_key, frame_key)
            cached = self.cache.get(key) if None not in key else None
            if cached is not None:
                self.cache.move_to_end(key)
                self.cache_hits += 1
            start = time.perf_counter()
            if cached is not None:
                global_claim, subtile_all = cached
                claims["global_motion"] = {
                    k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in global_claim.items()
                }
            elif not same:
                def _run_gm():
                    estimate = native.estimate if self.accelerated else estimate_global_motion
                    vector = estimate(self.prev, cur, max(grid.W, grid.H) - 1)
                    if vector is not None and vector[:2] != (0, 0):
                        dx, dy = vector[:2]
                        mask = consensus_tiles(self.prev, cur, grid.s, dx, dy).ravel()[grid.flat]
                        return dx, dy, mask
                    return 0, 0, None

                dx, dy, gm_mask = copy_context().run(_run_gm)
                if gm_mask is not None:
                    claims["global_motion"]["mask"] = gm_mask
                    claims["global_motion"]["mv_dx"][:] = dx
                    claims["global_motion"]["mv_dy"][:] = dy
            self.wall_seconds["global_motion"] += time.perf_counter() - start + conversion
            self.calls["global_motion"] += 1
            start = time.perf_counter()
            ids = np.flatnonzero(base["cls"] == 2)
            if cached is None:
                subtile_all = None
            if not same and len(ids):
                subkey = tuple(int(x) for x in ids)
                if cached is not None and subtile_all is not None and subtile_all[0] == subkey:
                    matches = subtile_all[1]
                elif self.accelerated:
                    matches = native.subtiles(self.prev, cur, grid, ids, grid.s - 1)
                    subtile_all = (subkey, matches)
                else:
                    matches = np.zeros((len(ids), 3), dtype=np.int64)
                    for j, tile in enumerate(ids):
                        match = subtile_motion(self.prev, cur, grid.s,
                                               (int(grid.tx[tile]), int(grid.ty[tile])), grid.s - 1)
                        if match is not None:
                            matches[j] = match
                    subtile_all = (subkey, matches)
                matched = matches[:, 2] > 0
                selected = ids[matched]
                claims["subtile"]["mask"][selected] = True
                claims["subtile"]["mv_dx"][selected] = matches[matched, 0]
                claims["subtile"]["mv_dy"][selected] = matches[matched, 1]
            self.wall_seconds["subtile"] += time.perf_counter() - start + conversion
            self.calls["subtile"] += 1
            if None not in key and self.cache_size:
                self.cache[key] = (
                    {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in claims["global_motion"].items()},
                    subtile_all,
                )
                self.cache.move_to_end(key)
                while len(self.cache) > self.cache_size:
                    self.cache.popitem(last=False)
        else:
            for name in ("global_motion", "subtile"):
                self.wall_seconds[name] += conversion
                self.calls[name] += 1
        self.prev, self.prev_key = cur, frame_key
        self.last_ops = {"gm": gm_ops, "fl": fl_ops}
        output = {"baseline": base}
        for name in VARIANTS[1:4]:
            output[name] = apply_claims(
                base, [claims[name]], flicker_veto=flicker_veto if name == "flicker" else None)
        combined = [claims[name] for name in ("global_motion", "subtile", "flicker")]
        if self.veto is not None:
            occupied = np.zeros(grid.N, dtype=np.bool_)
            effective = []
            for claim in combined:
                effective.append(dict(claim, mask=claim["mask"] & ~occupied))
                occupied |= claim["mask"]
            combined = effective
        output["combined"] = apply_claims(base, combined, flicker_veto=flicker_veto)
        return output, claims


@dataclass
class _DetectorStream:
    epoch: int
    grid: Grid
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
class _DetectorLane:
    lock: Lock = field(default_factory=Lock)
    stream: _DetectorStream | None = None


class PerWindowDetector:
    """Stream bank; a gap or higher geometry epoch resets only its own lane."""

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

    def _lane(self, handle: Handle) -> _DetectorLane:
        with self._lock:
            if handle not in self._lanes:
                self._lanes[handle] = _DetectorLane()
            return self._lanes[handle]

    def reset_window(self, handle) -> None:
        handle = _coerce_handle(handle)
        lane = self._lane(handle)
        with lane.lock:
            lane.stream = None

    def _new_stream(self, epoch: int, grid: Grid) -> _DetectorStream:
        from eval.v2x import fl_v3
        fl = self._fl if self._fl is not None else fl_v3
        cls = getattr(fl, "FlickerTracker", None)
        if cls is None:
            raise MissingEvidenceError("FL must export FlickerTracker")
        tracker = cls(grid.N, max_half=64)
        factory = self._veto_factory or BaselineFlickerVeto
        veto = factory(grid)
        if not callable(getattr(veto, "update", None)):
            raise MissingEvidenceError("veto must implement update")
        return _DetectorStream(epoch=epoch, grid=grid, tracker=tracker, veto=veto)

    def update(self, handle, tiles, rgb, *, epoch: int, frame_seq: int) -> ChangeResult:
        handle = _coerce_handle(handle)
        _require_nonnegative_int(epoch, "epoch")
        _require_nonnegative_int(frame_seq, "frame_seq")
        if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8
                or rgb.ndim != 3 or rgb.shape[2] != 3 or min(rgb.shape[:2]) < 1):
            raise DetectorInputError("rgb must be a nonempty HxWx3 uint8 ndarray")
        owned_rgb = np.array(rgb, dtype=np.uint8, copy=True, order="C")
        owned_rgb.flags.writeable = False
        grid = Grid(owned_rgb.shape[1], owned_rgb.shape[0])
        if not isinstance(tiles, np.ndarray):
            raise DetectorInputError("tiles must be a numpy.ndarray")
        if tiles.dtype == desc.TILE_DTYPE:
            if tiles.shape != (grid.N,) or not np.array_equal(tiles["morton"], grid.mortons):
                raise DetectorInputError("tiles must match the exact S2 grid, dtype and Morton order")
            baseline = tiles.copy()
        elif tiles.size == grid.N and tiles.dtype.kind in ("i", "u"):
            padded_tmp = np.array(grid.pad(owned_rgb), copy=True, order="C")
            baseline, _ = desc.describe_padded(padded_tmp, grid, lazy_xh=False)
            baseline = baseline.copy()
            flat_cls = tiles.ravel()
            if np.any((flat_cls < 0) | (flat_cls > 6)):
                raise DetectorInputError("tile class values out of range [0, 6]")
            baseline["cls"] = flat_cls.astype(np.uint8)
        else:
            raise DetectorInputError("tiles must match the exact S2 grid, dtype and Morton order")
        baseline.flags.writeable = False

        lane = self._lane(handle)
        with lane.lock:
            st = lane.stream
            if st is not None:
                if epoch < st.epoch:
                    raise EpochValidationError("stale geometry epoch")
                if epoch == st.epoch:
                    if (grid.W, grid.H) != (st.grid.W, st.grid.H):
                        raise GeometryValidationError("geometry change requires new epoch")
                    if frame_seq <= st.last_seq:
                        raise SequenceValidationError("non-increasing local frame_seq")
                if epoch != st.epoch or frame_seq != st.last_seq + 1:
                    st = None
            if st is None:
                st = self._new_stream(epoch, grid)
                lane.stream = st
            try:
                return self._step(st, handle, baseline, owned_rgb, frame_seq)
            except BaseException:
                lane.stream = None
                raise

    def _step(self, st: _DetectorStream, handle: Handle, baseline: np.ndarray,
              rgb: np.ndarray, frame_seq: int) -> ChangeResult:
        from eval.v2x import gm_bands_hv, fl_v3
        gm = self._gm if self._gm is not None else gm_bands_hv
        fl = self._fl if self._fl is not None else fl_v3
        grid = st.grid
        padded = np.array(grid.pad(rgb), copy=True, order="C")
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
                    ops = _read_module_ops(gm)
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
                            second = _read_module_ops(gm)
                            ops = None if ops is None or second is None else ops + second
                    return m, x, z, ops

                gm_mask, dx, dy, gm_ops = copy_context().run(gm_call)
                st.cache[key] = (gm_mask.copy(), dx, dy)
                if len(st.cache) > self.MAX_CACHE_PAIRS:
                    st.cache.popitem(last=False)

        output = baseline.copy()
        output["cls"][mask] = 2
        fm = (fl_states == 4) & ~gm_mask & (baseline["cls"] != 3)
        output["cls"][fm] = 4
        output["mv_dx"][fm] = 0
        output["mv_dy"][fm] = 0
        output["cls"][gm_mask] = 3
        output["mv_dx"][gm_mask] = dx
        output["mv_dy"][gm_mask] = dy
        output.flags.writeable = False

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
        return ChangeResult(handle, st.epoch, settled, output, {"gm": gm_ops, "fl": fl_ops})


def _register_per_window_detector_fallback():
    """Provide eval.v2x.per_window_detector in sys.modules if not yet landed on disk."""
    mod_name = "eval.v2x.per_window_detector"
    if mod_name in sys.modules:
        return
    spec = importlib.util.find_spec(mod_name)
    if spec is not None and spec.origin is not None:
        return
    import eval.v2x as v2x_pkg
    mod = types.ModuleType(mod_name)
    mod.__package__ = "eval.v2x"
    for sym in (
        "KINDS", "SUPPORTED_TARGET_KINDS", "METADATA_LOCK_ORDER",
        "DetectorInputError", "MissingEvidenceError", "SequenceValidationError",
        "IdentityMismatchError", "EpochValidationError", "GeometryValidationError",
        "TimestampValidationError", "Handle", "ChangeResult", "PerWindowDetector",
        "canonical_handle_key", "canonical_actuation_resource",
    ):
        setattr(mod, sym, globals()[sym])
    sys.modules[mod_name] = mod
    setattr(v2x_pkg, "per_window_detector", mod)


_register_per_window_detector_fallback()
