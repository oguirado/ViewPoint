"""V2EVAL2 post-pass plug-in for global motion estimation backed by vqa_native_v2k."""

import numpy as np
from visioncore.layout import grid_params
from eval.native_v2k import load_native_v2k

_gm_estimate = None
_gm_consensus = None


def estimate_global_motion(prev_luma, cur_luma, max_d, k_top=16):
    """Estimate global integer displacement (dx, dy, score) using native vqa_k_gm_proto."""
    prev_luma = np.ascontiguousarray(prev_luma); cur_luma = np.ascontiguousarray(cur_luma)   # native code needs C-contiguous buffers (contract attack non_contig_readonly)
    global _gm_estimate
    if _gm_estimate is None:
        _gm_estimate = load_native_v2k().gm_estimate
    return _gm_estimate(prev_luma, cur_luma, int(max_d), int(k_top))


def consensus_tiles(prev, cur, s, dx, dy):
    """Return a boolean (gh, gw) numpy array of tiles matching prev shifted by (dx, dy)."""
    prev = np.ascontiguousarray(prev); cur = np.ascontiguousarray(cur)   # native code needs C-contiguous buffers (contract attack non_contig_readonly)
    global _gm_consensus
    if _gm_consensus is None:
        _gm_consensus = load_native_v2k().gm_consensus
    h, w = cur.shape
    grid_s, gw, gh = grid_params(w, h)
    s_val = grid_s if s is None else int(s)
    flat_mask = _gm_consensus(prev, cur, s_val, int(dx), int(dy))
    return np.asarray(flat_mask, dtype=bool).reshape((gh, gw))
