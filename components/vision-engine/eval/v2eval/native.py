"""Exact integer kernels over RAM pipes to a bounded scratch executable.

Global search is a differential-tested transcription of the original prototype;
only its integer comparisons are batched. No dynamic library loading, pixels on
disk, approximate scores or search pruning.
"""
import atexit
import functools
import hashlib
from pathlib import Path
import struct
import subprocess

import numpy as np
from eval import v2proto_global_motion as gm

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = ROOT / "build/room_E"


class Kernels:
    def __init__(self, target):
        self.process = subprocess.Popen([str(target)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.input, self.output = self.process.stdin, self.process.stdout
        atexit.register(self.close)

    def close(self):
        if self.input.closed:
            return
        self.input.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.output.close()

    def send(self, *values):
        self.input.write(struct.pack("<"+"i"*len(values), *values))

    def read(self, n):
        self.input.flush()
        data = bytearray()
        while len(data) < n:
            block = self.output.read(n-len(data))
            if not block:
                raise RuntimeError("bounded kernel process exited")
            data.extend(block)
        return bytes(data)

    def plane(self, slot, array):
        array = np.ascontiguousarray(array, dtype=np.uint8)
        self.send(1, slot, *array.shape)
        self.input.write(array.tobytes())

    def sad_ids(self, p, c, dx, dy):
        self.send(2, p, c, dx, dy)
        return struct.unpack("<q", self.read(8))[0]

    def sad(self, p, c, dx, dy):
        self.plane(0, p); self.plane(1, c)
        return self.sad_ids(0, 1, dx, dy)


@functools.lru_cache(maxsize=1)
def library():
    source = Path(__file__).with_name("exact.c")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    SCRATCH.mkdir(parents=True, exist_ok=True)
    target = SCRATCH / ("exact_" + digest)
    if not target.exists():
        subprocess.run(["cc", "-O3", "-std=c11", str(source), "-o", str(target)],
                       check=True, timeout=60)
    return Kernels(target)


def score_many(prev, cur, vectors):
    """Batch the same scalar SAD evaluations; deduplicate identical queries."""
    unique = list(dict.fromkeys(vectors))
    if not unique:
        return []
    lib = library()
    lib.send(0)
    lib.plane(0, prev); lib.plane(1, cur)
    lib.send(4, 0, 1, len(unique))
    lib.input.write(np.asarray(unique, dtype="<i4").tobytes())
    values = np.frombuffer(lib.read(8*len(unique)), dtype="<i8")
    scores = dict(zip(unique, (int(v) for v in values)))
    return [(scores[(dx, dy)], dx, dy) for dx, dy in vectors]


def estimate(prev, cur, max_d, k_top=16):
    """Verbatim search decisions from v2proto_global_motion; only SAD is batched.

    Preserve duplicate candidates before sorting, the original tie key,
    refinement range, pyramid truncation, final neighborhood and uniqueness test.
    """
    prev_arr = np.asarray(prev, dtype=np.uint8)
    cur_arr = np.asarray(cur, dtype=np.uint8)
    if np.max(cur_arr) == np.min(cur_arr) and np.max(prev_arr) == np.min(prev_arr):
        return None
    pp, cp = [prev_arr], [cur_arr]
    while True:
        h, w = cp[-1].shape
        if h <= 32 or w <= 32:
            break
        pp.append(gm._downsample2x(pp[-1]))
        cp.append(gm._downsample2x(cp[-1]))
    levels = len(cp)
    radius = max_d // (1 << (levels-1))
    key = lambda v: (v[0], abs(v[1])+abs(v[2]), v[1], v[2])
    candidates = score_many(pp[-1], cp[-1], [(dx, dy)
        for dy in range(-radius, radius+1) for dx in range(-radius, radius+1)])
    candidates.sort(key=key)
    top = [(dx, dy) for _,dx,dy in candidates[:k_top]]
    for level in range(levels-2, -1, -1):
        limit = max_d // (1 << level)
        vectors = []
        for dx, dy in top:
            for ddy in range(-3, 4):
                for ddx in range(-3, 4):
                    x, y = dx*2+ddx, dy*2+ddy
                    if abs(x) <= limit and abs(y) <= limit:
                        vectors.append((x, y))
        candidates = score_many(pp[level], cp[level], vectors)
        candidates.sort(key=key)
        seen, top = set(), []
        for _,dx,dy in candidates:
            if (dx,dy) not in seen:
                seen.add((dx,dy))
                top.append((dx,dy))
                if len(top) >= k_top:
                    break
    bx, by = top[0]
    candidates = score_many(prev_arr, cur_arr, [(dx,dy)
        for dy in range(by-8, by+9) for dx in range(bx-8, bx+9)
        if abs(dx) <= max_d and abs(dy) <= max_d])
    candidates.sort(key=key)
    if not candidates:
        return None
    score, bx, by = candidates[0]
    non_adj = [v for v in candidates if max(abs(v[1]-bx), abs(v[2]-by)) >= 4]
    if non_adj and (non_adj[0][0] == score or non_adj[0][0]-score < 100):
        return None
    return int(bx), int(by), int(score)


@functools.lru_cache(maxsize=8)
def candidates(max_d):
    values = [(dx, dy) for dx in range(-max_d, max_d + 1)
              for dy in range(-max_d, max_d + 1) if dx or dy]
    values.sort(key=lambda p: (p[0]**2+p[1]**2, abs(p[0]), abs(p[1]), p[0], p[1]))
    return np.asarray(values, dtype="<i4").reshape(-1, 2)


def subtiles(prev, cur, grid, ids, max_d):
    p, c = (np.ascontiguousarray(a, dtype=np.uint8) for a in (prev, cur))
    tx = np.ascontiguousarray(grid.tx[ids], dtype="<i4")
    ty = np.ascontiguousarray(grid.ty[ids], dtype="<i4")
    cand = candidates(max_d)
    lib = library()
    lib.send(3, *c.shape, grid.s, grid.gw, grid.gh, len(ids), len(cand))
    for a in (p, c, tx, ty, cand):
        lib.input.write(a.tobytes())
    return np.frombuffer(lib.read(len(ids)*12), dtype="<i4").reshape(-1, 3).copy()
