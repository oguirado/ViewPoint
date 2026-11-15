# @module {"id":"va.cu_regions","layer":4,"owner":"ARCH","deps":["vc.s0","vc.s1","vc.s2","vc.s4"],"book":"HB-VC-s2-change"}
"""In-memory S0-S4 pair bridge; dispatches to visioncore_native when present with automatic Python fallback.

Lives in the unsealed adapters package, outside top-level vnative book scope.
C2 reports exact changes, including S2's perceptually suppressed jitter. Promote
such tiles after classification; retain S2 MOVED groups and S4 connectivity.
"""
import numpy as np

from visioncore import change, desc, regions
from visioncore.grid import Grid
from vnative.adapters import kernels


def pair_regions(previous, current):  # @anchor:va.cu_regions.pair_regions
    """Return existing engine region records for a masked, same-size RGB pair."""
    height, width, _ = current.shape
    if np.array_equal(previous, current):
        return []
    with kernels.installed():
        grid = Grid(width, height)
        before, after = grid.pad(previous), grid.pad(current)
        y_before = desc.luma_of_padded(before)
        old, _ = desc.describe_padded(before, grid, y_plane=y_before, lazy_xh=True)
        state = change.ClassState.init(old)
        change.classify_first(state, old, y_cur=y_before)
        y_after = desc.luma_of_padded(after)
        new, equal = desc.describe_padded(after, grid, words_prev=before,
                                        prev_tiles=old, y_plane=y_after, lazy_xh=True)
        classified, _, _ = change.classify_tiles(
            state, new, equal, y_cur=y_after, grid=(grid.s, grid.gw, grid.gh))
        # Exact equality is authoritative for C2, unlike the perceptual stream API.
        classified['cls'][equal] = change.C_UNCHANGED
        suppressed = ~equal & ~np.isin(classified['cls'], (
            change.C_CHANGED, change.C_MOVED, change.C_FLICKER,
            change.C_FROZEN, change.C_DRIFT))
        classified['cls'][suppressed] = change.C_CHANGED
        return regions.regions_of(classified, width, height, 1)


def pair_regions_plus(previous, current):  # @anchor:va.cu_regions.pair_regions_plus
    """Return (records, adjacency) for a masked, same-size RGB pair."""
    records = pair_regions(previous, current)
    return (records, kernels.adjacency(records)) if records else ([], [])
