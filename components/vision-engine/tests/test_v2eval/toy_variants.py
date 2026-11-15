"""Deterministic contract fixtures, deliberately making visible claims."""
import numpy as np


def estimate_global_motion(prev_luma, cur_luma, max_d):
    return 1, 0, 0


def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
    h, w = cur_luma.shape
    mask = np.zeros(((h+s-1)//s, (w+s-1)//s), bool)
    mask[0, 0] = True
    return mask


class FlickerTracker:
    def __init__(self, n_tiles, max_half=64):
        self.n = n_tiles

    def update(self, hashes):
        states = np.zeros(self.n, dtype=np.int64)
        states[-1] = 4
        return states, np.ones(self.n, dtype=np.int64)


def subtile_motion(prev_luma, cur_luma, s, tile_xy, max_d):
    return 1, 0, 1
