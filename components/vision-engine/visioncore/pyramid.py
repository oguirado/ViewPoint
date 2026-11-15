# @module {"id":"vc.s3","layer":2,"stage":"S3","owner":"CORE","deps":["vc.errors","vc.ids","vc.layout","vc.p"],"inputs":"tile records (morton, cls, resid), Grid","outputs":"max-residual quadtree, bounded node selection, O(1) AABB query","invariants":["integer-only","deterministic","zero-subtree-skip"],"anti_uses":["float thresholds","random selectors"],"oracle":"tests/test_pyramid.py","book":"HB-VC-s3-pyramid"}
"""@module vc.s3 L2 - S3 max-residual quadtree pyramid over Morton index space (SPEC_CORE §4)."""
import heapq

import numpy as np

from . import ids, layout, params as P
from .errors import VqaError


def levels_for(gw, gh):  # @anchor:vc.s3.levels_for
    """L = max(1, ceil(log2(max(gw, gh)))) via bit_length (no float)."""
    return max(1, (max(gw, gh) - 1).bit_length())


def id_level_cap(L):
    """Highest level <= L that ids.node_id can format (the registry grammar bounds it; JC-6)."""
    for k in range(L, 0, -1):
        try:
            ids.node_id(0, k, 0)
            return k
        except VqaError:
            continue
    return 0


def r_sel(params=None):  # @anchor:vc.s3.r_sel
    """R_SEL = T_exit >> 2 in resid units (resid = r >> 2)."""
    p = P.PARAMS if params is None else params
    return (p["T0_Q4"] * (1000 - p["HYST_BAND_PERMILLE"]) // 1000) >> 2


class Pyramid:  # @anchor:vc.s3.pyramid
    """Level k holds 4^(L-k) nodes; node i covers Morton [i*4^k, (i+1)*4^k). Level 0 = leaves."""

    def __init__(self, tiles, grid):
        self.L = L = levels_for(grid.gw, grid.gh)
        self.grid = grid
        size = 4 ** L
        m = tiles["morton"].astype(np.int64)
        leaf = np.zeros(size, dtype=np.uint8)
        leaf[m] = tiles["resid"]
        cnt = np.zeros(size, dtype=np.uint16)
        cnt[m] = tiles["cls"] >= layout.CLS_DIRTY_MIN
        real = np.zeros(size, dtype=np.uint16)
        real[m] = 1
        self.mx, self.cnt, self.real = [leaf], [cnt], [real]
        for _ in range(L):
            self.mx.append(self.mx[-1].reshape(-1, 4).max(axis=1))
            self.cnt.append(self.cnt[-1].reshape(-1, 4).sum(axis=1, dtype=np.uint16))
            self.real.append(self.real[-1].reshape(-1, 4).sum(axis=1, dtype=np.uint16))
        self.last_visits = 0
        self.id_cap = id_level_cap(L)  # < L only if the id grammar cannot name the root: select() then emits a forest

    def node(self, level, i):  # @anchor:vc.s3.node
        """-> (level, m0, max, cnt) for node i of `level`."""
        return (level, i << (2 * level), int(self.mx[level][i]), int(self.cnt[level][i]))

    def node_id(self, frame, level, i):  # @anchor:vc.s3.node_id
        return ids.node_id(frame, level, i << (2 * level))

    def children(self, level, i):  # @anchor:vc.s3.children
        """Child node indices (level-1) with real tiles under them, ascending."""
        if level == 0:
            return []
        return [c for c in range(4 * i, 4 * i + 4) if self.real[level - 1][c]]

    def bound(self, level, i):  # @anchor:vc.s3.bound
        """Coarse-to-fine upper bound: max resid of every tile under the node (exact)."""
        return int(self.mx[level][i])

    def query_aabb(self, x, y, w, h):  # @anchor:vc.s3.query_aabb
        """Pixel rect -> <= 4 nodes (level, m0, max, cnt) covering its tile box at the smallest level."""
        g = self.grid
        if w <= 0 or h <= 0:
            return []
        x1, y1 = min(x + w, g.W) - 1, min(y + h, g.H) - 1
        x0, y0 = max(x, 0), max(y, 0)
        if x1 < x0 or y1 < y0:
            return []
        tx0, ty0, tx1, ty1 = x0 // g.s, y0 // g.s, x1 // g.s, y1 // g.s
        m = (max(tx1 - tx0 + 1, ty1 - ty0 + 1) - 1).bit_length()
        out = []
        for by in range(ty0 >> m, (ty1 >> m) + 1):
            for bx in range(tx0 >> m, (tx1 >> m) + 1):
                i = layout.morton(bx, by)
                if self.real[m][i]:
                    out.append(self.node(m, i))
        return sorted(out, key=lambda n: n[1])

    def max_aabb(self, x, y, w, h):  # @anchor:vc.s3.max_aabb
        return max([n[2] for n in self.query_aabb(x, y, w, h)], default=0)

    def select(self, B, rsel=None):  # @anchor:vc.s3.select
        """<= B nodes covering every tile with resid > rsel; never descends nodes with max <= rsel."""
        if not isinstance(B, int) or B < 1:
            raise VqaError("E_USAGE", {"B": B})
        rs = r_sel() if rsel is None else rsel
        L, out, heap, visits = self.L, [], [], 0

        def push(level, i):
            heapq.heappush(heap, (-int(self.mx[level][i]), -level, i << (2 * level), level, i))

        if self.id_cap >= L:
            if self.mx[L][0] > rs:
                push(L, 0)
        else:  # root not representable as an id: start from the nodes of the highest representable level (<= 4^(L-cap))
            for i in range(len(self.mx[self.id_cap])):
                if self.real[self.id_cap][i] and self.mx[self.id_cap][i] > rs:
                    push(self.id_cap, i)
        while heap:
            _, _, _, level, i = heapq.heappop(heap)
            visits += 1
            if level == 0 or self.cnt[level][i] == self.real[level][i] or len(out) + len(heap) + 4 > B:
                out.append(self.node(level, i))
                continue
            for c in self.children(level, i):
                if self.mx[level - 1][c] > rs:
                    push(level - 1, c)
        self.last_visits = visits
        return out
