"""Per-tile FLICKER tracker variant based on run-length histograms.

Tracks run lengths for two alternating states (state A and state B) per tile.
Declares FLICKER (4) only on frames where the tile hash changes and the last 2
complete runs of state A agree within +/- 1 frame and the last 2 complete runs
of state B agree within +/- 1 frame in a strictly alternating cycle.
Emits FLICKER (4) only on frame transitions, never during held frames.

OPS count unit: total number of elementary array element comparisons, math
operations, array assignments, and logical reductions performed on numpy arrays.
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
    """Reset the global OPS counter to zero."""
    global OPS
    OPS = 0


def _as_u64_hashes(hashes, n_tiles):
    """Convert a 1-D sequence of length n_tiles of ints to a uint64 numpy array."""
    global OPS
    if isinstance(hashes, np.ndarray):
        OPS += hashes.size
        if hashes.ndim != 1 or hashes.shape[0] != n_tiles:
            raise ValueError("hashes must have shape (n_tiles,)")
        if hashes.dtype == np.bool_ or hashes.dtype.kind not in ("i", "u"):
            raise ValueError("hashes must have integer dtype")
        return np.ascontiguousarray(hashes).view(np.uint64) if hashes.dtype.itemsize == 8 else hashes.astype(np.uint64)
    seq = list(hashes)
    if len(seq) != n_tiles:
        raise ValueError("hashes length must equal n_tiles")
    OPS += n_tiles
    out = np.empty(n_tiles, dtype=np.uint64)
    for i, v in enumerate(seq):
        if isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)):
            raise ValueError("each hash must be an integer")
        out[i] = int(v) & _U64_MASK
    return out


class FlickerTracker:
    """Per-tile FLICKER tracker variant using run-length histogram consistency."""

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
        self.prev_hash = np.zeros(n, dtype=np.uint64)
        self.state_a = np.zeros(n, dtype=np.uint64)
        self.state_b = np.zeros(n, dtype=np.uint64)
        self.has_b = np.zeros(n, dtype=bool)
        self.cur_state = np.zeros(n, dtype=np.uint8)  # 0 for A, 1 for B
        self.cur_len = np.zeros(n, dtype=np.int64)

        # Last 6 run lengths for A and B per tile
        self.a_runs = np.zeros((n, 6), dtype=np.int64)
        self.b_runs = np.zeros((n, 6), dtype=np.int64)
        self.n_a_runs = np.zeros(n, dtype=np.int64)
        self.n_b_runs = np.zeros(n, dtype=np.int64)

    def update(self, hashes):
        """Feed one descriptor hash per tile for the current frame.

        Returns (states, half_periods) as int64 numpy arrays of shape (n_tiles,).
        """
        global OPS
        n = self.n_tiles
        h = _as_u64_hashes(hashes, n)

        if not self.initialized:
            self.initialized = True
            self.prev_hash[:] = h
            self.state_a[:] = h
            self.has_b[:] = False
            self.cur_state[:] = 0
            self.cur_len[:] = 1
            self.n_a_runs[:] = 0
            self.n_b_runs[:] = 0
            self.a_runs.fill(0)
            self.b_runs.fill(0)
            OPS += n * 8
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        same = h == self.prev_hash
        changed = ~same
        OPS += n * 2

        states = np.full(n, C_UNCHANGED, dtype=np.int64)
        half_periods = np.zeros(n, dtype=np.int64)
        OPS += n * 2

        if same.any():
            self.cur_len[same] += 1
            over_max = same & (self.cur_len > self.max_half)
            OPS += same.sum() * 2 + n
            if over_max.any():
                self.n_a_runs[over_max] = 0
                self.n_b_runs[over_max] = 0
                self.a_runs[over_max] = 0
                self.b_runs[over_max] = 0
                OPS += over_max.sum() * 4

        if changed.any():
            states[changed] = C_CHANGED
            OPS += changed.sum()

            is_third = changed & self.has_b & (h != self.state_a) & (h != self.state_b)
            OPS += n * 3
            if is_third.any():
                self.state_a[is_third] = h[is_third]
                self.has_b[is_third] = False
                self.cur_state[is_third] = 0
                self.cur_len[is_third] = 1
                self.n_a_runs[is_third] = 0
                self.n_b_runs[is_third] = 0
                self.a_runs[is_third] = 0
                self.b_runs[is_third] = 0
                OPS += is_third.sum() * 8

            two_state = changed & (~is_third)
            OPS += n
            if two_state.any():
                first_b = two_state & (~self.has_b)
                OPS += n
                if first_b.any():
                    self.state_b[first_b] = h[first_b]
                    self.has_b[first_b] = True
                    OPS += first_b.sum() * 2

                prev_len = self.cur_len.copy()
                OPS += n

                trans_a_to_b = two_state & (self.cur_state == 0) & (h == self.state_b)
                OPS += n * 2
                if trans_a_to_b.any():
                    valid_a = trans_a_to_b & (prev_len <= self.max_half)
                    invalid_a = trans_a_to_b & (prev_len > self.max_half)
                    OPS += trans_a_to_b.sum() * 2
                    if invalid_a.any():
                        self.n_a_runs[invalid_a] = 0
                        self.n_b_runs[invalid_a] = 0
                        self.a_runs[invalid_a] = 0
                        self.b_runs[invalid_a] = 0
                        OPS += invalid_a.sum() * 4
                    if valid_a.any():
                        m = valid_a.sum()
                        self.a_runs[valid_a] = np.column_stack([self.a_runs[valid_a, 1:], prev_len[valid_a]])
                        self.n_a_runs[valid_a] += 1
                        OPS += m * 7

                    self.cur_state[trans_a_to_b] = 1
                    self.cur_len[trans_a_to_b] = 1
                    OPS += trans_a_to_b.sum() * 2

                trans_b_to_a = two_state & (self.cur_state == 1) & (h == self.state_a)
                OPS += n * 2
                if trans_b_to_a.any():
                    valid_b = trans_b_to_a & (prev_len <= self.max_half)
                    invalid_b = trans_b_to_a & (prev_len > self.max_half)
                    OPS += trans_b_to_a.sum() * 2
                    if invalid_b.any():
                        self.n_a_runs[invalid_b] = 0
                        self.n_b_runs[invalid_b] = 0
                        self.a_runs[invalid_b] = 0
                        self.b_runs[invalid_b] = 0
                        OPS += invalid_b.sum() * 4
                    if valid_b.any():
                        m = valid_b.sum()
                        self.b_runs[valid_b] = np.column_stack([self.b_runs[valid_b, 1:], prev_len[valid_b]])
                        self.n_b_runs[valid_b] += 1
                        OPS += m * 7

                    self.cur_state[trans_b_to_a] = 0
                    self.cur_len[trans_b_to_a] = 1
                    OPS += trans_b_to_a.sum() * 2

                cand = two_state & (self.n_a_runs >= 2) & (self.n_b_runs >= 2)
                OPS += n * 2
                if cand.any():
                    a_last1 = self.a_runs[:, -1]
                    a_last2 = self.a_runs[:, -2]
                    b_last1 = self.b_runs[:, -1]
                    b_last2 = self.b_runs[:, -2]

                    a_ok = (np.abs(a_last1 - a_last2) <= 1) & (a_last1 <= self.max_half) & (a_last1 >= 1) & (a_last2 <= self.max_half) & (a_last2 >= 1)
                    b_ok = (np.abs(b_last1 - b_last2) <= 1) & (b_last1 <= self.max_half) & (b_last1 >= 1) & (b_last2 <= self.max_half) & (b_last2 >= 1)

                    flicker_mask = cand & a_ok & b_ok
                    OPS += n * 12
                    if flicker_mask.any():
                        states[flicker_mask] = C_FLICKER
                        a_est = (a_last1 + a_last2 + 1) // 2
                        b_est = (b_last1 + b_last2 + 1) // 2
                        hp_est = (a_est + b_est + 1) // 2
                        half_periods[flicker_mask] = hp_est[flicker_mask]
                        OPS += flicker_mask.sum() * 5

        self.prev_hash[:] = h
        OPS += n
        return states, half_periods
