# @module {"id":"vc.s0","layer":1,"stage":"S0","owner":"CORE","deps":["vc.ids","vc.layout"],"book":"HB-VC-s0-grid"}
"""@module vc.s0 L1 - S0 tile grid and Morton order (SPEC_CORE §1)."""
import numpy as np

from . import ids, layout


class Grid:  # @anchor:vc.s0.grid
    """Tile grid of a W x H frame. Per-tile tables are in ascending Morton order over existing cells."""

    def __init__(self, W, H):
        s, gw, gh = layout.grid_params(W, H)
        self.W, self.H, self.s, self.gw, self.gh = W, H, s, gw, gh
        self.N = gw * gh
        self.ident = layout.grid_ident(W, H)
        mort = np.empty((gh, gw), dtype=np.uint16)
        for ty in range(gh):
            for tx in range(gw):
                mort[ty, tx] = layout.morton(tx, ty)
        self.morton = mort
        perm = np.argsort(mort.reshape(-1), kind="stable")
        self.flat = perm  # Morton rank -> row-major cell index
        self.mortons = mort.reshape(-1)[perm]
        self.tx = (perm % gw).astype(np.int32)
        self.ty = (perm // gw).astype(np.int32)
        order = np.empty(self.N, dtype=np.int16)
        order[perm] = np.arange(self.N, dtype=np.int16)
        self.order = order.reshape(gh, gw)

    def pad(self, rgb):
        """Complete edge tiles by edge replication (SPEC_CORE §1 Padding). -> uint8[gh*s, gw*s, 3]."""
        ph, pw = self.gh * self.s - self.H, self.gw * self.s - self.W
        if ph == 0 and pw == 0:
            return rgb
        return np.pad(rgb, ((0, ph), (0, pw), (0, 0)), mode="edge")

    def pad_flags(self):
        """bool[N]: tile crosses the right or bottom border."""
        return ((self.tx + 1) * self.s > self.W) | ((self.ty + 1) * self.s > self.H)


def field_ids_of_rect(frame, rect_px, grid):  # @anchor:vc.s0.field_ids_of_rect
    return ids.field_ids_of_rect(rect_px, grid.W, grid.H, frame)


def aligned_block(bbox_t):  # @anchor:vc.s0.aligned_block
    """bbox_t (tx0,ty0,tx1,ty1) -> (k, m0, m1): smallest aligned 2^k block containing it."""
    tx0, ty0, tx1, ty1 = bbox_t
    k = 0
    while (tx0 >> k) != (tx1 >> k) or (ty0 >> k) != (ty1 >> k):
        k += 1
    m0 = layout.morton((tx0 >> k) << k, (ty0 >> k) << k)
    return k, m0, m0 + 4 ** k - 1
