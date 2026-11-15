"""V2EVAL2 post-pass plug-in for 8x8 sub-tile motion search backed by vqa_native_v2k."""

import numpy as np
from eval.native_v2k import load_native_v2k

_st_proto8_subtile_motion = None


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    """Find sub-tile integer translation (dx, dy, n_blocks) using native vqa_k_st_proto8."""
    prev_luma = np.ascontiguousarray(prev_luma); cur_luma = np.ascontiguousarray(cur_luma)   # native code needs C-contiguous buffers (contract attack non_contig_readonly)
    global _st_proto8_subtile_motion
    if _st_proto8_subtile_motion is None:
        _st_proto8_subtile_motion = load_native_v2k().st_proto8_subtile_motion
    return _st_proto8_subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d)
