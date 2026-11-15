"""Failure fixtures: each API exercises a different containment path."""
import time


def estimate_global_motion(*args):
    raise RuntimeError("toy raises")


class FlickerTracker:
    def __init__(self, n_tiles, max_half=64):
        pass

    def update(self, hashes):
        return [4], [1]


def subtile_motion(*args):
    time.sleep(10)


def consensus_tiles(*args):
    return None
