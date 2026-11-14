"""Competing per-tile FLICKER tracker variant aimed at caret blink.

Phase-coherent cohort blink detector:
Carets span 1 or 2 tiles that toggle on the exact same frames. Tiles are grouped into
cohorts by their recent toggle-frame history (last 3 toggle frame timestamps).
Cohorts of 1 to 4 tiles that alternate between two hashes with run lengths within +/-1
of an estimated integer half-period in 1..max_half may declare FLICKER (4) starting from
the 3rd toggle (2 complete runs). Cohorts larger than 4 tiles require 4 complete runs.
FLICKER (4) is declared ONLY on toggle frames where the tile hash changed and equals
the hash of two runs earlier inside an established two-state cycle.

Ops counter tracks elementary numpy array/element operations: array comparisons,
additions, subtractions, masks, indexing, and reductions. Size of every array compared,
transformed or reduced is added to OPS.
"""
import numpy as np

C_UNCHANGED, C_JITTER, C_CHANGED, C_MOVED, C_FLICKER, C_FROZEN, C_DRIFT = range(7)
UNCHANGED = C_UNCHANGED
JITTER = C_JITTER
CHANGED = C_CHANGED
MOVED = C_MOVED
FLICKER = C_FLICKER
FROZEN = C_FROZEN
DRIFT = C_DRIFT

_U64_MASK = (1 << 64) - 1

OPS = 0


def reset_ops():
    """Reset the module-level OPS work counter to 0."""
    global OPS
    OPS = 0


def _add_ops(count):
    global OPS
    OPS += int(count)


def _as_u64_hashes(hashes, n_tiles):
    """Convert a 1-D sequence of length n_tiles of ints to a uint64 numpy array."""
    if isinstance(hashes, np.ndarray):
        if hashes.ndim != 1 or hashes.shape[0] != n_tiles:
            raise ValueError("hashes must have shape (n_tiles,)")
        if hashes.dtype == np.bool_ or hashes.dtype.kind not in ("i", "u"):
            raise ValueError("hashes must have integer dtype")
        _add_ops(n_tiles)
        return np.ascontiguousarray(hashes).view(np.uint64) if hashes.dtype.itemsize == 8 else hashes.astype(np.uint64)
    seq = list(hashes)
    if len(seq) != n_tiles:
        raise ValueError("hashes length must equal n_tiles")
    out = np.empty(n_tiles, dtype=np.uint64)
    for i, v in enumerate(seq):
        if isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)):
            raise ValueError("each hash must be an integer")
        out[i] = int(v) & _U64_MASK
    _add_ops(n_tiles)
    return out


def _estimate_half_period(r0, r1, r2, r3):
    """Vectorised integer half-period estimate over 4 compatible half-period runs."""
    rmin = np.minimum(np.minimum(r0, r1), np.minimum(r2, r3))
    rmax = np.maximum(np.maximum(r0, r1), np.maximum(r2, r3))
    center = (rmin + rmax) // 2
    mean_round = (r0 + r1 + r2 + r3 + 2) // 4
    return np.where(rmax - rmin == 2, center, mean_round)


class FlickerTracker:
    """Phase-coherent cohort blink detector FLICKER tracker."""

    UNCHANGED = C_UNCHANGED
    CHANGED = C_CHANGED
    FLICKER = C_FLICKER
    C_UNCHANGED = C_UNCHANGED
    C_CHANGED = C_CHANGED
    C_FLICKER = C_FLICKER

    def __init__(self, n_tiles, max_half=64):
        if isinstance(n_tiles, (bool, np.bool_)) or not isinstance(n_tiles, (int, np.integer)) or int(n_tiles) < 1:
            raise ValueError("n_tiles must be a positive integer")
        if isinstance(max_half, (bool, np.bool_)) or not isinstance(max_half, (int, np.integer)) or int(max_half) < 1:
            raise ValueError("max_half must be a positive integer")
        self.n_tiles = int(n_tiles)
        self.max_half = int(max_half)
        self.reset()

    def reset(self):
        """Reset all per-tile automaton state."""
        n = self.n_tiles
        self.initialized = False
        self.frame_idx = np.int64(0)
        self.prev_hash = np.zeros(n, dtype=np.uint64)
        self.state_a = np.zeros(n, dtype=np.uint64)
        self.state_b = np.zeros(n, dtype=np.uint64)
        self.has_b = np.zeros(n, dtype=bool)
        self.cur_len = np.zeros(n, dtype=np.int64)
        self.toggle_count = np.zeros(n, dtype=np.int64)
        self.last_toggles = np.full((n, 3), -1, dtype=np.int64)
        self.runs = np.zeros((n, 4), dtype=np.int64)
        self.half_period = np.zeros(n, dtype=np.int64)

    def update(self, hashes):
        """Feed one descriptor hash per tile for current frame.

        Returns (states, half_periods) as int64 numpy arrays of shape (n_tiles,).
        """
        h = _as_u64_hashes(hashes, self.n_tiles)
        n = self.n_tiles
        _add_ops(n)

        if not self.initialized:
            self.initialized = True
            self.frame_idx = np.int64(0)
            self.prev_hash[:] = h
            self.state_a[:] = h
            self.has_b[:] = False
            self.cur_len[:] = 1
            self.toggle_count[:] = 0
            self.last_toggles[:] = -1
            self.runs[:] = 0
            self.half_period[:] = 0
            _add_ops(n * 8)
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        self.frame_idx += 1
        f = self.frame_idx

        same = (h == self.prev_hash)
        changed = ~same
        _add_ops(n * 2)

        states = np.zeros(n, dtype=np.int64)
        half_periods = np.zeros(n, dtype=np.int64)
        _add_ops(n * 2)

        if same.any():
            _add_ops(n)
            self.cur_len[same] += 1
            over_max = same & (self.cur_len > self.max_half)
            _add_ops(n * 2)
            if over_max.any():
                self.toggle_count[over_max] = 0
                self.last_toggles[over_max] = -1
                self.runs[over_max] = 0
                self.half_period[over_max] = 0
                _add_ops(n * 4)

        if changed.any():
            _add_ops(n)
            states[changed] = C_CHANGED
            _add_ops(n)

            third = changed & self.has_b & (h != self.state_a) & (h != self.state_b)
            _add_ops(n * 4)
            if third.any():
                self.state_a[third] = h[third]
                self.has_b[third] = False
                self.cur_len[third] = 1
                self.toggle_count[third] = 0
                self.last_toggles[third] = -1
                self.runs[third] = 0
                self.half_period[third] = 0
                _add_ops(n * 7)

            two_state = changed & (~third)
            _add_ops(n * 2)
            if two_state.any():
                first_b = two_state & (~self.has_b)
                _add_ops(n * 2)
                if first_b.any():
                    self.state_b[first_b] = h[first_b]
                    self.has_b[first_b] = True
                    _add_ops(n * 2)

                prev_len = self.cur_len.copy()
                _add_ops(n)

                too_long = two_state & (prev_len > self.max_half)
                _add_ops(n * 2)
                if too_long.any():
                    self.toggle_count[too_long] = 0
                    self.last_toggles[too_long] = -1
                    self.runs[too_long] = 0
                    self.half_period[too_long] = 0
                    _add_ops(n * 4)

                valid = two_state & (prev_len <= self.max_half)
                _add_ops(n * 2)

                if valid.any():
                    unq, inv, counts = np.unique(self.last_toggles, axis=0, return_inverse=True, return_counts=True)
                    cohort_sizes = counts[inv]
                    _add_ops(n * 3)

                    r0 = prev_len
                    r1 = self.runs[:, 0]
                    r2 = self.runs[:, 1]
                    r3 = self.runs[:, 2]
                    _add_ops(n * 4)

                    old_tc = self.toggle_count
                    n_runs = old_tc
                    _add_ops(n)

                    small_cohort = cohort_sizes <= 4
                    req_runs = np.where(small_cohort, 2, 4)
                    _add_ops(n * 2)

                    has_req_runs = valid & (n_runs >= req_runs)
                    _add_ops(n * 2)

                    hp2 = (r0 + r1 + 1) // 2
                    ok2 = (r0 <= self.max_half) & (r1 <= self.max_half) & (r1 >= 1) & (np.abs(r0 - r1) <= 2)
                    _add_ops(n * 5)

                    rmin3 = np.minimum(r0, np.minimum(r1, r2))
                    rmax3 = np.maximum(r0, np.maximum(r1, r2))
                    h3 = np.where(rmax3 - rmin3 == 2, (rmin3 + rmax3) // 2, (r0 + r1 + r2 + 1) // 3)
                    ok3 = ok2 & (r2 >= 1) & ((rmax3 - rmin3) <= 2) & (np.abs(r0 - h3) <= 1) & (np.abs(r1 - h3) <= 1) & (np.abs(r2 - h3) <= 1)
                    _add_ops(n * 10)

                    rmin4 = np.minimum(rmin3, r3)
                    rmax4 = np.maximum(rmax3, r3)
                    h4 = _estimate_half_period(r0, r1, r2, r3)
                    ok4 = ok3 & (r3 >= 1) & ((rmax4 - rmin4) <= 2) & (np.abs(r0 - h4) <= 1) & (np.abs(r1 - h4) <= 1) & (np.abs(r2 - h4) <= 1) & (np.abs(r3 - h4) <= 1)
                    _add_ops(n * 12)

                    is_runs2 = (n_runs == 2)
                    is_runs3 = (n_runs == 3)
                    is_runs4 = (n_runs >= 4)
                    _add_ops(n * 3)

                    flick_ok = np.where(is_runs2, ok2, np.where(is_runs3, ok3, np.where(is_runs4, ok4, False)))
                    flick_hp = np.where(is_runs2, hp2, np.where(is_runs3, h3, np.where(is_runs4, h4, 0)))
                    _add_ops(n * 6)

                    flick = has_req_runs & flick_ok
                    _add_ops(n)

                    if flick.any():
                        states[flick] = C_FLICKER
                        half_periods[flick] = flick_hp[flick]
                        self.half_period[flick] = flick_hp[flick]
                        _add_ops(n * 3)

                    self.runs[valid, 3] = self.runs[valid, 2]
                    self.runs[valid, 2] = self.runs[valid, 1]
                    self.runs[valid, 1] = self.runs[valid, 0]
                    self.runs[valid, 0] = r0[valid]

                    self.last_toggles[valid, 2] = self.last_toggles[valid, 1]
                    self.last_toggles[valid, 1] = self.last_toggles[valid, 0]
                    self.last_toggles[valid, 0] = f

                    self.toggle_count[valid] += 1
                    self.cur_len[valid] = 1
                    _add_ops(n * 10)

        self.prev_hash[:] = h
        _add_ops(n)
        return states, half_periods
