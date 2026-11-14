"""Bounded in-RAM, per-target wrapper for the sealed visioncore S0-S4 pipeline.

No transport, focus/actor lock, filesystem output, or per-frame process is used.
Each handle incarnation and geometry epoch is a separate temporal history.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from typing import Sequence

import numpy as np

from visioncore import frame as core_frame
from vnative import adapters as pipeline


@dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int


@dataclass(frozen=True)
class WindowFrames:
    handle: Handle
    epoch: int
    frames: tuple
    frame_seqs: tuple
    dpr_ppm: int


@dataclass(frozen=True)
class WindowDescription:
    handle: Handle
    epoch: int
    frame_seqs: tuple
    descriptors: tuple
    regions: tuple
    error: str | None


class WindowInputError(ValueError):
    """Invalid input rejected before admission to the sealed core."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


_SUPPORTED_KINDS = frozenset(("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"))
_MAX_WORKERS = 8
_POOLS: dict[int, ThreadPoolExecutor] = {}
_POOL_GUARD = Lock()


@dataclass(frozen=True)
class _Admitted:
    index: int
    handle: object
    identity: tuple
    epoch: int
    frame_seqs: tuple
    frames: tuple
    dpr_ppm: int
    shape: tuple


@dataclass(frozen=True)
class _Batch:
    index: int  # original submission index of the first entry, not completion position
    entries: tuple
    shape: tuple


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise WindowInputError("E_M3_SCHEMA", f"{name} must be an integer >= {minimum}")
    return value


def _identity(handle: object) -> tuple:
    try:
        kind, canonical_id, generation = handle.kind, handle.canonical_id, handle.generation
    except AttributeError as exc:
        raise WindowInputError("E_M3_HANDLE", "handle needs kind, canonical_id, generation") from exc
    if (not isinstance(kind, str) or kind not in _SUPPORTED_KINDS
            or not isinstance(canonical_id, str) or not canonical_id):
        raise WindowInputError("E_M3_HANDLE", "unsupported kind or empty canonical_id")
    _integer(generation, "handle.generation")
    return kind, canonical_id, generation


def _admit(index: int, item: WindowFrames) -> _Admitted:
    ident = _identity(item.handle)
    epoch = _integer(item.epoch, "epoch")
    _integer(item.dpr_ppm, "dpr_ppm", minimum=1)
    if not isinstance(item.frames, tuple) or not isinstance(item.frame_seqs, tuple):
        raise WindowInputError("E_M3_SCHEMA", "frames and frame_seqs must be tuples")
    if not item.frames or len(item.frames) != len(item.frame_seqs):
        raise WindowInputError("E_M3_SEQUENCE", "nonempty, equal-length frames and frame_seqs required")
    last = -1
    for seq in item.frame_seqs:
        _integer(seq, "frame_seq")
        if seq <= last:
            raise WindowInputError("E_M3_SEQUENCE", "frame_seqs must be strictly increasing")
        last = seq

    shape = None
    owned = []
    for seq, fr in zip(item.frame_seqs, item.frames):
        # Structural frame references from window_stream_bank (when available) are
        # accepted without importing that not-yet-landed provider module.
        if not isinstance(fr, np.ndarray) and hasattr(fr, "rgb"):
            if not all(hasattr(fr, a) for a in ("handle", "epoch", "frame_seq", "height", "width")):
                raise WindowInputError("E_M3_FRAME_IDENTITY", "incomplete FrameRef metadata")
            if _identity(fr.handle) != ident or fr.epoch != epoch or fr.frame_seq != seq:
                raise WindowInputError("E_M3_FRAME_IDENTITY", "FrameRef disagrees with its window/epoch/sequence")
            rgb = fr.rgb
        else:
            rgb = fr
        try:
            arr = np.asarray(rgb)
        except (TypeError, ValueError) as exc:
            raise WindowInputError("E_M3_FRAME_SCHEMA", "unreadable RGB buffer") from exc
        if arr.ndim != 3 or arr.shape[2] != 3 or arr.shape[0] <= 0 or arr.shape[1] <= 0:
            raise WindowInputError("E_M3_FRAME_SHAPE", "expected RGB (height,width,3), positive dimensions")
        if arr.dtype != np.uint8 or not arr.flags.c_contiguous:
            raise WindowInputError("E_M3_FRAME_SCHEMA", "RGB must be C-contiguous uint8; no implicit conversion")
        if shape is None:
            shape = arr.shape
        elif arr.shape != shape:
            raise WindowInputError("E_M3_GEOMETRY", "a geometry epoch cannot contain different frame sizes")
        # Own every byte before executor admission; never borrow an M4 ring slot.
        buf = arr.copy(order="C")
        buf.setflags(write=False)
        if not isinstance(fr, np.ndarray) and hasattr(fr, "rgb"):
            if (type(fr.width) is not int or type(fr.height) is not int
                    or (fr.height, fr.width, 3) != shape):
                raise WindowInputError("E_M3_FRAME_SHAPE", "FrameRef dimensions disagree with RGB")
            digest = getattr(fr, "digest", None)
            if digest is not None:
                if (not isinstance(digest, str) or len(digest) != 64
                        or digest != sha256(buf.tobytes()).hexdigest()):
                    raise WindowInputError("E_M3_DIGEST", "FrameRef digest does not match admitted RGB")
        owned.append(buf)
    return _Admitted(index, item.handle, ident, epoch, item.frame_seqs, tuple(owned),
                     item.dpr_ppm, shape)


def _error(item: object, code: str, detail: str = "") -> WindowDescription:
    return WindowDescription(item.handle, item.epoch, getattr(item, "frame_seqs", ()), (), (),
                             f"{code}: {detail}" if detail else code)


def _batches(admitted: Sequence[_Admitted], outputs: list) -> list[_Batch]:
    """Group only consecutive chronology of the *same* incarnation+epoch+shape.

    Multiple slices of a target may be passed in a batch, including out of input
    order. Their predecessor is the previous sequence for THAT stream, never a
    same-sized frame from another handle. Overlaps/conflicting geometry refuse.
    """
    groups = {}
    for item in admitted:
        groups.setdefault((item.identity, item.epoch), []).append(item)
    batches = []
    for entries in groups.values():
        shapes = {e.shape for e in entries}
        if len(shapes) != 1:
            for e in entries:
                outputs[e.index] = _error(e, "E_M3_GEOMETRY_EPOCH", "same handle/epoch has different dimensions")
            continue
        ordered = sorted(entries, key=lambda e: e.frame_seqs[0])
        if any(earlier.frame_seqs[-1] >= later.frame_seqs[0]
               for earlier, later in zip(ordered, ordered[1:])):
            for e in entries:
                outputs[e.index] = _error(e, "E_M3_SEQUENCE_OVERLAP", "same stream has overlapping sequences")
            continue
        batches.append(_Batch(min(e.index for e in entries), tuple(ordered), ordered[0].shape))
    return batches


def _run_batch(batch: _Batch, params: dict | None) -> tuple:
    """Compute one sealed-core temporal history, return tagged per-input results."""
    try:
        frames, indices = [], []
        for entry in batch.entries:
            for seq, rgb in zip(entry.frame_seqs, entry.frames):
                frames.append(core_frame.Frame(i=len(frames), t_us=len(frames),
                                               render_index=seq, rgb=rgb))
                indices.append(entry.index)
        height, width, _ = batch.shape
        source = core_frame.MemoryFrameSource(frames, {
            "width": width, "height": height, "count": len(frames),
            "channels": 3, "pixel_format": "rgb24", "storage": "rgb24_raw",
        })
        # Always call S0-S4 serially *within* a target. S1's own multiprocessing
        # workers would mix scheduling layers and are unnecessary here.
        run = pipeline.analyze(source, params=params, hints=None, out_dir=None,
                               workers=1, cpus=[])
        if len(run.tiles) != len(indices) or len(run.regions) != len(indices):
            raise RuntimeError("sealed core returned an incomplete temporal sequence")
        tiles = [t.tobytes() for t in run.tiles]
        regions = [tuple(r.copy() for r in frame_regions) for frame_regions in run.regions]
        results = []
        offset = 0
        for entry in batch.entries:
            end = offset + len(entry.frames)
            results.append((entry.index, WindowDescription(
                entry.handle, entry.epoch, entry.frame_seqs,
                tuple(tiles[offset:end]), tuple(regions[offset:end]), None)))
            offset = end
        return batch.index, tuple(results)
    except Exception as exc:  # one core failure must not abort unrelated handles
        code = getattr(exc, "code", type(exc).__name__)
        return batch.index, tuple((e.index, _error(e, "E_M3_CORE_FAILED", str(code)))
                                  for e in batch.entries)


def _pool(workers: int) -> ThreadPoolExecutor:
    # One long-lived executor per possible bounded worker count. At most eight
    # pools / 36 threads in this process, not a new pool for each call.
    with _POOL_GUARD:
        if workers not in _POOLS:
            _POOLS[workers] = ThreadPoolExecutor(max_workers=workers,
                                                 thread_name_prefix=f"multi-describe-{workers}")
        return _POOLS[workers]


def describe_windows(items: Sequence[WindowFrames], *, workers: int = 1,
                     params: dict | None = None, runner: object | None = None,
                     out_dir: None = None) -> list[WindowDescription]:
    """Describe windows independently with bounded persistent workers and identity-safe assembly.

    ``descriptors`` contains one immutable ``TILE_DTYPE`` byte string per frame;
    ``regions`` contains one tuple of core region dictionaries per frame.
    Validation and per-window core failures return typed error descriptions.
    Every externally supplied frame is copied into private read-only RAM before
    any runner.map call. An injected runner must expose map(fn, items), but may
    complete or yield work in arbitrary order. No completed job is re-dispatched.
    """
    if out_dir is not None:
        raise WindowInputError("E_M3_OUT_DIR", "out_dir must be None; disk output is forbidden")
    _integer(workers, "workers", minimum=1)
    if params is not None and not isinstance(params, dict):
        raise WindowInputError("E_M3_PARAMS", "params must be a dict or None")
    # Freeze the callers' outer container and params before asynchronous work.
    items = tuple(items)
    if not items:
        return []
    outputs: list[WindowDescription | None] = [None] * len(items)
    admitted = []
    for index, item in enumerate(items):
        try:
            admitted.append(_admit(index, item))
        except WindowInputError as exc:
            if not all(hasattr(item, attr) for attr in ("handle", "epoch", "frame_seqs")):
                raise  # no trustworthy identity for an error receipt
            outputs[index] = _error(item, exc.code, str(exc).partition(": ")[2])
    batches = _batches(admitted, outputs)
    if not batches:
        return outputs  # type: ignore[return-value]
    fn = lambda batch: _run_batch(batch, None if params is None else dict(params))
    map_runner = runner if runner is not None else _pool(min(workers, len(batches), _MAX_WORKERS))
    pending = {batch.index: batch for batch in batches}
    runner_failure = None
    try:
        mapped = map_runner.map(fn, tuple(batches))
        for result in mapped:
            if (not isinstance(result, tuple) or len(result) != 2
                    or type(result[0]) is not int or result[0] not in pending):
                # A broken runner may return duplicates, but we never overwrite
                # already collected results or retry work that may have run.
                runner_failure = "E_M3_RUNNER_PROTOCOL"
                continue
            batch_id, descriptions = result
            batch = pending.pop(batch_id)
            expected = {entry.index for entry in batch.entries}
            seen = set()
            if not isinstance(descriptions, tuple):
                descriptions = ()
            for pair in descriptions:
                if (not isinstance(pair, tuple) or len(pair) != 2
                        or type(pair[0]) is not int or pair[0] not in expected
                        or pair[0] in seen or not isinstance(pair[1], WindowDescription)):
                    runner_failure = "E_M3_RUNNER_PROTOCOL"
                    continue
                idx, description = pair
                expected.remove(idx)
                seen.add(idx)
                outputs[idx] = description
            for entry in batch.entries:
                if entry.index in expected:
                    outputs[entry.index] = _error(entry, "E_M3_RUNNER_PROTOCOL", "missing result")
    except Exception as exc:
        runner_failure = f"E_M3_RUNNER_DIED: {type(exc).__name__}"
    for batch in pending.values():
        for entry in batch.entries:
            outputs[entry.index] = _error(entry, runner_failure or "E_M3_RUNNER_INCOMPLETE")
    return outputs  # type: ignore[return-value]
