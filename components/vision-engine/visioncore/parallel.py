# @module {"id":"vc.parallel","layer":4,"stage":"L4","owner":"ARCH","deps":["vc.errors","vc.s1","vc.topology"],"inputs":"frames,grid,workers,cpus","outputs":"TILE_DTYPE[count,N]","invariants":["integer-only","deterministic","byte-identical-to-serial"],"anti_uses":["per-engine branches","clock"],"oracle":"tests/test_parallel.py","book":"HB-VC-a0-arch"}
"""@module vc.parallel L4 - contiguous frame ranges with exact descriptor reuse in forked workers.
Shared outputs avoid pickling; optional S0 planes let the pipeline consume each calculation once."""
import mmap
import multiprocessing as mp
import os
from multiprocessing import shared_memory

import numpy as np

from . import desc
from .errors import VqaError

try:  # CP1 module: absence only disables automatic pinning
    from . import topology
except ImportError:  # pragma: no cover
    topology = None

_JOIN_S = 600
_G = {}  # fork-inherited read-only inputs of the running pool


def frames_array(source):
    """Frames of a source as uint8[count,H,W,3]: the raw memmap when the source carries one, else a stacked copy."""
    raw = getattr(source, "raw_array", None)
    if raw is not None:
        return raw
    return np.stack([np.asarray(f.rgb) for f in source])


def default_cpus():
    """Big-core ids from topology (CPU part 0xd85), [] when unknown (then workers are not pinned)."""
    if topology is None:
        return []
    try:
        return list(topology.big_cpus())
    except Exception:  # unreadable /proc: run unpinned
        return []


def _shared_array(shape, dtype):
    """Anonymous shared RAM; ndarray.base owns the mmap, including on error."""
    dtype = np.dtype(dtype)
    size = int(np.prod(shape)) * dtype.itemsize
    return np.ndarray(shape, dtype=dtype, buffer=mmap.mmap(-1, max(1, size)))


def _with_s0(out, s0):
    """Views keep the backing maps alive; repeated frames share immutable planes."""
    pads, luma, words, indices = s0
    return out, [pads[k] for k in indices], [luma[k] for k in indices], words


# Each fork writes disjoint frame indices, including its S0 planes and aliases.
# A range never reads another range's output: only predecessor input pixels.
# The returned views release anonymous mappings when the consumer drops them.
# Boundary descriptors compute every tile; exact pixel equality still feeds S2.
# Interior reuse copies only S1 fields, never classified or hinted output.
# S2 and lazy post-classification hashes remain ordered in the consumer.


def describe_many(source, grid, workers=1, cpus=None, params=None, lazy_xh=False, return_s0=False):  # @anchor:vc.parallel.describe_many
    """source (FrameSource or uint8[count,H,W,3]) -> TILE_DTYPE[count,N], row k = full describe of frame k.
    workers<=1: serial in-process. cpus: explicit cpu ids (worker w pins to cpus[w % len]); None: big cores from
    topology, empty: unpinned. Results are assembled by frame index, never by arrival.
    lazy_xh=True leaves hashes zero for the caller to fill after sequential S2 classification.
    return_s0=True returns (tiles, padded_frames, luma_planes, words_eq) in anonymous shared RAM.
    Each range's first descriptor is full, but its equality compares the actual preceding frame."""
    frames = source if isinstance(source, (np.ndarray, list, tuple)) else frames_array(source)
    count = len(frames)
    shape = frames.shape[1:] if isinstance(frames, np.ndarray) else next(
        (f.shape for f in frames if f.shape != (grid.H, grid.W, 3)), (grid.H, grid.W, 3))
    if shape != (grid.H, grid.W, 3):
        raise VqaError("E_VC_FRAME_SHAPE", {"shape": list(shape), "grid": grid.ident})
    workers = max(1, min(int(workers), count)) if count else 1
    s0 = None
    if return_s0:
        ph, pw = grid.gh * grid.s, grid.gw * grid.s
        s0 = (_shared_array((count, ph, pw, 3), np.uint8),
              _shared_array((count, ph, pw), np.int16),
              _shared_array((count, grid.N), bool),
              _shared_array((count,), np.int64))
    if workers == 1:
        out = np.zeros((count, grid.N), dtype=desc.TILE_DTYPE)
        prev = prev_tiles = None
        for k in range(count):
            padded = grid.pad(frames[k])
            y = desc.luma_of_padded(padded)
            tiles, equal = desc.describe_padded(padded, grid, prev, None, params,
                                               prev_tiles=prev_tiles, y_plane=y, lazy_xh=lazy_xh)
            out[k] = tiles
            if s0 is not None:
                s0[0][k], s0[1][k], s0[2][k], s0[3][k] = padded, y, equal, k
            prev, prev_tiles = padded, tiles
        return _with_s0(out, s0) if return_s0 else out
    cpus = default_cpus() if cpus is None else list(cpus)
    shape = (count, grid.N)
    shm = shared_memory.SharedMemory(create=True, size=max(1, count * grid.N * desc.TILE_DTYPE.itemsize))
    procs = []
    try:
        _G.update(frames=frames, grid=grid, params=params, lazy_xh=lazy_xh, s0=s0)
        ctx = mp.get_context("fork")
        for w in range(workers):
            ks = range(w * count // workers, (w + 1) * count // workers)
            cpu = cpus[w % len(cpus)] if cpus else None
            p = ctx.Process(target=_worker, args=(w, ks, shm.name, shape, cpu))
            p.start()
            procs.append(p)
        bad = []
        for w, p in enumerate(procs):
            p.join(_JOIN_S)
            if p.is_alive():
                p.kill()
                p.join()
            if p.exitcode != 0:
                bad.append(w)
        if bad:
            raise VqaError("E_IO", {"reason": "describe worker failed", "workers": bad[:8]})
        view = np.ndarray(shape, dtype=desc.TILE_DTYPE, buffer=shm.buf)
        out = view.copy()
        del view
        return _with_s0(out, s0) if return_s0 else out
    finally:
        for p in procs:
            if p.is_alive():
                p.kill()
                p.join()
        _G.clear()
        shm.close()
        shm.unlink()


def _worker(widx, ks, shm_name, shape, cpu):
    try:
        if cpu is not None and hasattr(os, "sched_setaffinity"):
            os.sched_setaffinity(0, {cpu})
        shm = shared_memory.SharedMemory(name=shm_name)
        out = np.ndarray(shape, dtype=desc.TILE_DTYPE, buffer=shm.buf)
        frames, grid, params = _G["frames"], _G["grid"], _G["params"]
        # Pixels at k-1 are sufficient for boundary equality; boundary descriptors
        # are computed in full. No descriptor from another range is needed.
        prev = grid.pad(frames[ks.start - 1]) if ks.start else None
        prev_tiles = None
        for k in ks:
            same = prev_tiles is not None and np.array_equal(frames[k], frames[k - 1])
            if same:
                # Exact whole-frame reuse also shares the immutable S0 planes.
                tiles = prev_tiles
                equal = np.ones(grid.N, dtype=bool)
            else:
                if _G["s0"] is None:
                    padded = grid.pad(frames[k])
                else:
                    padded = _G["s0"][0][k]
                    padded[:grid.H, :grid.W] = frames[k]
                    padded[grid.H:, :grid.W] = frames[k][-1:]
                    padded[:, grid.W:] = padded[:, grid.W - 1:grid.W]
                y = desc.luma_of_padded(padded)
                tiles, equal = desc.describe_padded(padded, grid, prev, None, params,
                                                   prev_tiles=prev_tiles, y_plane=y, lazy_xh=_G["lazy_xh"])
            out[k] = tiles
            if _G["s0"] is not None:
                pads, luma, words, indices = _G["s0"]
                if same:
                    indices[k] = indices[k - 1]
                else:
                    luma[k] = y
                    indices[k] = k
                words[k] = equal
            prev, prev_tiles = padded, tiles
        del out
        shm.close()
    except BaseException:
        os._exit(3)
    os._exit(0)
