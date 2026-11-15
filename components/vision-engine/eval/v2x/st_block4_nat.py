"""V2EVAL2 post-pass plug-in for 4x4 sub-tile motion search backed by vqa_native_v2k."""

import numpy as np
from eval.native_v2k import load_native_v2k

_st_block4_subtile_motion = None
_st_block4_batch = None


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile integer translation (dx, dy, n_blocks) using native vqa_k_st_block4."""
    prev_luma = np.ascontiguousarray(prev_luma); cur_luma = np.ascontiguousarray(cur_luma)   # native code needs C-contiguous buffers (contract attack non_contig_readonly)
    global _st_block4_subtile_motion
    if _st_block4_subtile_motion is None:
        _st_block4_subtile_motion = load_native_v2k().st_block4_subtile_motion
    return _st_block4_subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d)


def subtile_motion_batch(prev_luma, cur_luma, s, tiles, max_d):
    """Batch evaluate 4x4 sub-tile motion across a list of (tx, ty) tiles."""
    prev_luma = np.ascontiguousarray(prev_luma); cur_luma = np.ascontiguousarray(cur_luma)   # native code needs C-contiguous buffers (contract attack non_contig_readonly)
    global _st_block4_batch
    if _st_block4_batch is None:
        _st_block4_batch = load_native_v2k().st_block4_batch
    return _st_block4_batch(prev_luma, cur_luma, s, tiles, max_d)
