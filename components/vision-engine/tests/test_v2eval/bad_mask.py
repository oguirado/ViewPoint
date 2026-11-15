import numpy as np


def estimate_global_motion(*args):
    return 1, 0, 0


def consensus_tiles(*args):
    return np.ones((1, 1), dtype=np.int64)
