"""Per-tile FLICKER tracker variant using state-sequence autocorrelation.

Measures periodic two-state descriptor-hash toggling by computing integer
autocorrelation over per-tile state histories. Declares FLICKER (4) only on
toggle frames inside an established two-state cycle that match the dominant
lag (estimated half-period) within +/-1 frame. Held phases never emit 4.

Elementary operation counts are tracked via module-level OPS integer and
reset_ops(). Unit: element operations performed across numpy array comparisons,
transformations, and reductions (1 element operation per array element per step).
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
    """Reset the module-level elementary operation counter."""
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
    out = np.empty(n_tiles, dtype=np.uint64)
    for i, v in enumerate(seq):
        if isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)):
            raise ValueError("each hash must be an integer")
        out[i] = int(v) & _U64_MASK
    OPS += n_tiles
    return out


class FlickerTracker:
    """Per-tile deterministic integer autocorrelation tracker for two-state periodic FLICKER."""

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
        """Reset all per-tile tracker state."""
        n = self.n_tiles
        self.initialized = False
        self.prev_hash = np.zeros(n, dtype=np.uint64)
        self.state_a = np.zeros(n, dtype=np.uint64)
        self.state_b = np.zeros(n, dtype=np.uint64)
        self.has_b = np.zeros(n, dtype=bool)
        self.cur_len = np.zeros(n, dtype=np.int64)
        self.r0 = np.zeros(n, dtype=np.int64)
        self.r1 = np.zeros(n, dtype=np.int64)
        self.r2 = np.zeros(n, dtype=np.int64)
        self.n_runs = np.zeros(n, dtype=np.int64)
        self.half_period = np.zeros(n, dtype=np.int64)

    def update(self, hashes):
        """Feed one descriptor hash per tile for the current frame.

        Returns (states, half_periods) as int64 numpy arrays of shape (n_tiles,).
        """
        global OPS
        h = _as_u64_hashes(hashes, self.n_tiles)
        n = self.n_tiles

        if not self.initialized:
            self.initialized = True
            self.prev_hash[:] = h
            self.state_a[:] = h
            self.has_b[:] = False
            self.cur_len[:] = 1
            self.n_runs[:] = 0
            self.half_period[:] = 0
            OPS += 7 * n
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        same = h == self.prev_hash
        changed = ~same
        OPS += 2 * n

        states = np.full(n, C_UNCHANGED, dtype=np.int64)
        OPS += n

        # Branch 1: Unchanged tile frames
        if same.any():
            self.cur_len[same] += 1
            OPS += n
            over_max = same & (self.cur_len > self.max_half)
            OPS += 2 * n
            if over_max.any():
                self.n_runs[over_max] = 0
                self.half_period[over_max] = 0
                OPS += 2 * n

        # Branch 2: Changed tile frames
        if changed.any():
            states[changed] = C_CHANGED
            OPS += n

            # Check third state: new hash is neither state_a nor state_b when state_b is set
            third = changed & self.has_b & (h != self.state_a) & (h != self.state_b)
            OPS += 4 * n
            if third.any():
                self.state_a[third] = h[third]
                self.has_b[third] = False
                self.cur_len[third] = 1
                self.n_runs[third] = 0
                self.half_period[third] = 0
                OPS += 5 * n

            two_state = changed & (~third)
            OPS += 2 * n
            if two_state.any():
                first_b = two_state & (~self.has_b)
                OPS += 2 * n
                if first_b.any():
                    self.state_b[first_b] = h[first_b]
                    self.has_b[first_b] = True
                    OPS += 2 * n

                prev_len = self.cur_len.copy()
                OPS += n
                too_long = two_state & (prev_len > self.max_half)
                OPS += 2 * n
                if too_long.any():
                    self.n_runs[too_long] = 0
                    self.half_period[too_long] = 0
                    self.cur_len[too_long] = 1
                    OPS += 3 * n

                valid_run = two_state & (prev_len <= self.max_half)
                OPS += 2 * n
                if valid_run.any():
                    nr0 = prev_len
                    nr1 = self.r0.copy()
                    nr2 = self.r1.copy()
                    old_n = self.n_runs.copy()
                    OPS += 3 * n

                    self.r2[valid_run] = nr1[valid_run]
                    self.r1[valid_run] = nr0[valid_run]
                    self.r0[valid_run] = nr0[valid_run]
                    self.cur_len[valid_run] = 1
                    OPS += 4 * n

                    # Check 2-run compatibility
                    ok2 = (
                        (old_n >= 1)
                        & (np.abs(nr0 - nr1) <= 2)
                        & (nr0 >= 1)
                        & (nr1 >= 1)
                        & (nr0 <= self.max_half)
                        & (nr1 <= self.max_half)
                    )
                    OPS += 6 * n

                    # Check 3-run compatibility (autocorrelation of 3 consecutive toggle intervals)
                    rmin3 = np.minimum(nr0, np.minimum(nr1, nr2))
                    rmax3 = np.maximum(nr0, np.maximum(nr1, nr2))
                    OPS += 4 * n

                    h3 = np.where(
                        rmax3 - rmin3 == 2,
                        (rmin3 + rmax3) // 2,
                        (nr0 + nr1 + nr2 + 2) // 3,
                    )
                    OPS += 5 * n

                    ok3 = (
                        ok2
                        & (old_n >= 2)
                        & ((rmax3 - rmin3) <= 2)
                        & (rmin3 >= 1)
                        & (rmax3 <= self.max_half)
                        & (np.abs(nr0 - h3) <= 1)
                        & (np.abs(nr1 - h3) <= 1)
                        & (np.abs(nr2 - h3) <= 1)
                    )
                    OPS += 8 * n

                    new_n = np.ones(n, dtype=np.int64)
                    new_n = np.where(ok2, 2, new_n)
                    new_n = np.where(ok3, old_n + 1, new_n)
                    OPS += 3 * n

                    self.n_runs[valid_run] = new_n[valid_run]
                    OPS += n

                    matching_3 = valid_run & ok3
                    not_matching_3 = valid_run & (~ok3)
                    OPS += 2 * n

                    if matching_3.any():
                        self.half_period[matching_3] = h3[matching_3]
                        OPS += n
                    if not_matching_3.any():
                        self.half_period[not_matching_3] = 0
                        OPS += n

                    # Emit FLICKER (4) ONLY on toggle frames where 3 consecutive runs match the lag
                    flicker_emit = valid_run & ok3 & (self.n_runs >= 3)
                    OPS += 3 * n
                    if flicker_emit.any():
                        states[flicker_emit] = C_FLICKER
                        OPS += n

        self.prev_hash[:] = h
        OPS += n
        return states, self.half_period.copy()
