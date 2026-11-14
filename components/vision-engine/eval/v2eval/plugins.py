"""Bounded process adapter for independent, claim-only competitive variants."""
import importlib
import hashlib
import math
import multiprocessing as mp
from pathlib import Path
import sys
import time

import numpy as np
from .postpass import apply_claims, arrays

REFERENCES = {"gm=eval.v2proto_global_motion", "fl=eval.v2proto_flicker", "st=eval.v2proto_subtile"}


def parse_variant(value):
    kind, sep, module = value.partition("=")
    if not sep or kind not in ("gm", "fl", "st") or not module or not all(
            p.isidentifier() for p in module.split(".")):
        raise ValueError("variant must be gm|fl|st=importable.module")
    return kind, module


def integer(value):
    return isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))


def vector(value, radius, *, blocks=False):
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError("expected (dx, dy, score/blocks) or None")
    dx, dy, third = value
    if not integer(dx) or not integer(dy) or abs(int(dx)) > radius or abs(int(dy)) > radius:
        raise ValueError("invalid displacement")
    if blocks:
        if not integer(third) or third <= 0 or (dx == 0 and dy == 0):
            raise ValueError("invalid changed-block claim")
    elif isinstance(third, (bool, np.bool_)) or not isinstance(third, (int, float, np.integer, np.floating)) or not math.isfinite(third) or third < 0:
        raise ValueError("invalid SAD score")
    return int(dx), int(dy), third


def _worker(pipe, spec, grid):
    """Count executed Python bytecodes, including Python dependencies, not VM time.

    NumPy/C internals are opaque; the explicit counter scope accompanies results.
    Import/constructor and every complete frame are bounded by the parent.
    """
    try:
        kind, module_name = parse_variant(spec)
        module = importlib.import_module(module_name)
        tracker = module.FlickerTracker(grid.N, max_half=64) if kind == "fl" else None
        if kind == "gm":
            if not callable(module.estimate_global_motion) or not callable(module.consensus_tiles):
                raise ValueError("global motion API must be callable")
        if kind == "st":
            if not callable(module.subtile_motion):
                raise ValueError("subtile API must be callable")
        digest = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        pipe.send((True, digest))
        while True:
            payload = pipe.recv()
            if payload is None:
                break
            prev, cur, tiles = payload
            for a in (prev, cur, tiles):
                if a is not None:
                    a.flags.writeable = False
            claim = {"mask": np.zeros(grid.N, bool), "cls": 4 if kind == "fl" else 3,
                     "mv_dx": np.zeros(grid.N, np.int64), "mv_dy": np.zeros(grid.N, np.int64)}
            ops = 0
            def trace(frame, event, arg):
                nonlocal ops
                if event == "call":
                    frame.f_trace_opcodes = True
                elif event == "opcode":
                    ops += 1
                return trace
            started = time.perf_counter()
            # Python 3.12 enables opcode events only if a frame opts in first.
            sys._getframe().f_trace_opcodes = True
            sys.settrace(trace)
            try:
                if kind == "fl":
                    result = tracker.update(tiles["xh64"])
                    if not isinstance(result, (tuple, list)) or len(result) != 2:
                        raise ValueError("invalid tracker pair")
                    states, halves = map(np.asarray, result)
                    if any(a.shape != (grid.N,) or a.dtype.kind not in "iu" for a in (states, halves)):
                        raise ValueError("invalid tracker arrays")
                    if not np.isin(states, [0, 1, 2, 3, 4, 5, 6]).all() or (halves > 64).any() or (halves < 0).any() or ((states == 4) & (halves == 0)).any():
                        raise ValueError("invalid tracker states/half periods")
                    claim["mask"] = states == 4
                elif prev is not None and not np.array_equal(prev, cur):
                    if kind == "gm":
                        radius = max(grid.W, grid.H)-1
                        v = vector(module.estimate_global_motion(prev, cur, radius), radius)
                        if v is not None and v[:2] != (0, 0):
                            dx, dy = v[:2]
                            mask = np.asarray(module.consensus_tiles(prev, cur, grid.s, dx, dy))
                            if mask.shape != (grid.gh, grid.gw) or mask.dtype != np.bool_:
                                raise ValueError("invalid boolean consensus mask")
                            claim["mask"] = mask.ravel()[grid.flat]
                            claim["mv_dx"][:] = dx
                            claim["mv_dy"][:] = dy
                    else:
                        for t in np.flatnonzero(tiles["cls"] == 2):
                            v = vector(module.subtile_motion(prev, cur, grid.s,
                                       (int(grid.tx[t]), int(grid.ty[t])), grid.s-1), grid.s-1, blocks=True)
                            if v is not None:
                                claim["mask"][t] = True
                                claim["mv_dx"][t], claim["mv_dy"][t] = v[:2]
            finally:
                sys.settrace(None)
            pipe.send((True, (claim, ops, time.perf_counter()-started)))
    except BaseException as exc:
        sys.settrace(None)
        try:
            pipe.send((False, type(exc).__name__ + ": " + str(exc)[:300]))
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()


class Variant:
    def __init__(self, spec, grid, frame_budget_ms=1000):
        parse_variant(spec)
        if not math.isfinite(frame_budget_ms) or frame_budget_ms <= 0:
            raise ValueError("positive finite frame budget required")
        self.spec, self.budget = spec, frame_budget_ms/1000
        self.status, self.error = "OK", None
        self.ops, self.seconds, self.frames = 0, 0.0, 0
        ctx = mp.get_context("spawn")
        self.pipe, child = ctx.Pipe()
        self.process = ctx.Process(target=_worker, args=(child, spec, grid), daemon=True)
        self.process.start()
        child.close()
        self.module_sha256 = self._receive()

    def _receive(self):
        try:
            if not self.pipe.poll(self.budget):
                raise TimeoutError("per-frame budget exceeded (includes startup for initialization)")
            ok, result = self.pipe.recv()
            if not ok:
                raise ValueError(result)
            return result
        except (OSError, EOFError, ValueError, TimeoutError) as exc:
            self.status, self.error = "FAILED", str(exc)
            self.close()
            return None

    def update(self, prev, cur, tiles):
        if self.status == "FAILED":
            return None
        try:
            self.pipe.send((prev, cur, tiles))
        except (OSError, EOFError) as exc:
            self.status, self.error = "FAILED", str(exc)
            self.close()
            return None
        result = self._receive()
        if result is None:
            return None
        claim, ops, seconds = result
        self.ops += ops
        self.seconds += seconds
        self.frames += 1
        return apply_claims(arrays(tiles), [claim])

    def close(self):
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=1)
        self.pipe.close()
