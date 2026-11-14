"""Research prototype for vision v2: per-tile long-period FLICKER automaton.

Recognises periodic two-state descriptor-hash toggling with half-period in
1..max_half frames, tolerating +/-1 frame period jitter, declaring FLICKER (4)
after 2 full periods (4 half-periods), returning the measured half-period, and
dropping back on any third state or when a phase exceeds half_period + 1 / stalls.
Integer-only state machine; stdlib and numpy only.
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


def _as_u64_hashes(hashes, n_tiles):
    """Convert a 1-D sequence of length n_tiles of ints to a uint64 numpy array."""
    if isinstance(hashes, np.ndarray):
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
    return out


def _estimate_half_period(r0, r1, r2, r3):
    """Vectorised integer half-period estimate over 4 compatible half-period runs."""
    return (r0 + r1 + r2 + r3 + 2) // 4


def _compatible3(r0, r1, r2):
    """Check whether 3 consecutive half-period runs match a common half-period H within +/-1."""
    h3 = (r0 + r1 + r2 + 1) // 3
    rmin = np.minimum(r0, np.minimum(r1, r2))
    rmax = np.maximum(r0, np.maximum(r1, r2))
    has_center = (r0 == h3) | (r1 == h3) | (r2 == h3)
    ok3 = (
        (h3 >= 1)
        & ((rmax - rmin) <= 2)
        & np.where(rmax - rmin == 2, has_center, True)
        & (np.abs(r0 - h3) <= 1)
        & (np.abs(r1 - h3) <= 1)
        & (np.abs(r2 - h3) <= 1)
        & (np.abs((r0 + r1) - 2 * h3) <= 1)
        & (np.abs((r1 + r2) - 2 * h3) <= 1)
    )
    return ok3, h3


def _compatible4(r0, r1, r2, r3):
    """Check whether 4 consecutive half-period runs (2 full periods) match a common H within +/-1."""
    h4 = _estimate_half_period(r0, r1, r2, r3)
    rmin = np.minimum(np.minimum(r0, r1), np.maximum(0, np.minimum(r2, r3)))
    rmax = np.maximum(np.maximum(r0, r1), np.maximum(r2, r3))
    has_center = (r0 == h4) | (r1 == h4) | (r2 == h4) | (r3 == h4)
    ok4 = (
        (h4 >= 1)
        & ((rmax - rmin) <= 2)
        & np.where(rmax - rmin == 2, has_center, True)
        & (np.abs(r0 - h4) <= 1)
        & (np.abs(r1 - h4) <= 1)
        & (np.abs(r2 - h4) <= 1)
        & (np.abs(r3 - h4) <= 1)
        & (np.abs((r0 + r1) - 2 * h4) <= 1)
        & (np.abs((r1 + r2) - 2 * h4) <= 1)
        & (np.abs((r2 + r3) - 2 * h4) <= 1)
    )
    return ok4, h4


class FlickerTracker:
    """Per-tile deterministic integer automaton for two-state periodic FLICKER."""

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
        self.cur_len = np.zeros(n, dtype=np.int64)
        self.n_runs = np.zeros(n, dtype=np.int64)
        self.r0 = np.zeros(n, dtype=np.int64)
        self.r1 = np.zeros(n, dtype=np.int64)
        self.r2 = np.zeros(n, dtype=np.int64)
        self.r3 = np.zeros(n, dtype=np.int64)
        self.in_flicker = np.zeros(n, dtype=bool)
        self.half_period = np.zeros(n, dtype=np.int64)
        self.clean_start = np.zeros(n, dtype=bool)
        self.stalled_hold = np.zeros(n, dtype=bool)

    def _clear_runs(self, mask):
        self.n_runs[mask] = 0
        self.r0[mask] = 0
        self.r1[mask] = 0
        self.r2[mask] = 0
        self.r3[mask] = 0
        self.in_flicker[mask] = False
        self.half_period[mask] = 0
        self.clean_start[mask] = False

    def update(self, hashes):
        """Feed one descriptor hash per tile for the current frame.

        Returns (states, half_periods) as int64 numpy arrays of shape (n_tiles,).
        """
        h = _as_u64_hashes(hashes, self.n_tiles)
        n = self.n_tiles
        if not self.initialized:
            self.initialized = True
            self.prev_hash[:] = h
            self.state_a[:] = h
            self.has_b[:] = False
            self.cur_len[:] = 1
            self.n_runs[:] = 0
            self.in_flicker[:] = False
            self.half_period[:] = 0
            self.clean_start[:] = True
            self.stalled_hold[:] = False
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        same = h == self.prev_hash
        changed = ~same
        states = np.full(n, C_UNCHANGED, dtype=np.int64)

        # --- Branch 1: tile hash unchanged from previous frame ---
        if same.any():
            self.cur_len[same] += 1
            over_max = same & (self.cur_len > self.max_half)
            if over_max.any():
                self._clear_runs(over_max)
                self.stalled_hold[over_max] = True

            # Active FLICKER tiles that exceed allowed upper jitter bound drop immediately.
            active_same = same & self.in_flicker
            if active_same.any():
                rmin3 = np.minimum(self.r0, np.minimum(self.r1, self.r2))
                stalled = active_same & (
                    (self.cur_len > self.half_period + 1)
                    | (self.cur_len - rmin3 > 2)
                    | ((self.cur_len + self.r0) > 2 * self.half_period + 1)
                )
                if stalled.any():
                    self._clear_runs(stalled)
                    self.stalled_hold[stalled] = True

            # Warm-up tiles whose held phase exceeds compatibility with prior runs drop warm-up runs.
            warm_same = same & (~self.in_flicker) & (self.n_runs >= 1)
            if warm_same.any():
                h3_warm = (self.r0 + self.r1 + self.r2 + 1) // 3
                rmin2 = np.minimum(self.r0, self.r1)
                rmin3_w = np.minimum(rmin2, self.r2)
                warm_stalled = warm_same & (
                    ((self.n_runs == 1) & (self.cur_len > self.r0 + 2))
                    | ((self.n_runs == 2) & ((self.cur_len > rmin2 + 2) | (self.cur_len > self.r0 + 2)))
                    | ((self.n_runs >= 3) & ((self.cur_len > h3_warm + 1) | (self.cur_len > rmin3_w + 2)))
                )
                if warm_stalled.any():
                    self._clear_runs(warm_stalled)

        # --- Branch 2: tile hash changed from previous frame ---
        if changed.any():
            states[changed] = C_CHANGED
            third = changed & self.has_b & (h != self.state_a) & (h != self.state_b)
            if third.any():
                self.state_a[third] = self.prev_hash[third]
                self.state_b[third] = h[third]
                self.has_b[third] = True
                self.cur_len[third] = 1
                self._clear_runs(third)
                self.stalled_hold[third] = False

            two_state = changed & (~third)
            if two_state.any():
                first_b = two_state & (~self.has_b)
                if first_b.any():
                    self.state_b[first_b] = h[first_b]
                    self.has_b[first_b] = True

                prev_len = self.cur_len.copy()
                too_long = two_state & ((prev_len > self.max_half) | self.stalled_hold)
                if too_long.any():
                    self._clear_runs(too_long)
                    self.cur_len[too_long] = 1
                    self.stalled_hold[too_long] = False

                valid_run = two_state & (~too_long)
                if valid_run.any():
                    nr0 = prev_len
                    nr1 = self.r0.copy()
                    nr2 = self.r1.copy()
                    nr3 = self.r2.copy()
                    old_n = self.n_runs.copy()

                    # Determine longest compatible suffix (1..4 runs) within +/-1 of a common integer H.
                    new_n = np.ones(n, dtype=np.int64)

                    ok2 = (old_n >= 1) & (np.abs(nr0 - nr1) <= 2)
                    new_n = np.where(ok2, 2, new_n)

                    ok3_raw, _ = _compatible3(nr0, nr1, nr2)
                    ok3 = ok2 & (old_n >= 2) & ok3_raw
                    new_n = np.where(ok3, 3, new_n)

                    ok4_raw, h4 = _compatible4(nr0, nr1, nr2, nr3)
                    ok4 = ok3 & (old_n >= 3) & ok4_raw & ((nr0 + nr1 + nr2 + nr3) >= 4 * h4 - 1)
                    new_n = np.where(ok4, 4, new_n)

                    dropped_history = valid_run & (old_n >= 1) & (new_n < np.minimum(old_n + 1, 4))
                    if dropped_history.any():
                        self.clean_start[dropped_history] = False

                    self.r0[valid_run] = nr0[valid_run]
                    self.r1[valid_run] = np.where(new_n >= 2, nr1, 0)[valid_run]
                    self.r2[valid_run] = np.where(new_n >= 3, nr2, 0)[valid_run]
                    self.r3[valid_run] = np.where(new_n >= 4, nr3, 0)[valid_run]
                    self.n_runs[valid_run] = new_n[valid_run]
                    self.cur_len[valid_run] = 1

                    flick = valid_run & (new_n >= 4)
                    not_flick = valid_run & (new_n < 4)
                    if flick.any():
                        self.in_flicker[flick] = True
                        self.half_period[flick] = h4[flick]
                    if not_flick.any():
                        self.in_flicker[not_flick] = False
                        self.half_period[not_flick] = 0

        # --- Declare FLICKER when 4th clean half-period completes 2 full periods (4 * H frames) ---
        cand4 = (
            (~self.in_flicker)
            & self.clean_start
            & (self.n_runs == 3)
            & (self.cur_len <= self.max_half)
        )
        if cand4.any():
            ok3_prev, h3 = _compatible3(self.r0, self.r1, self.r2)
            ok4_cur, hp_est = _compatible4(self.cur_len, self.r0, self.r1, self.r2)
            two_full_periods = (self.r2 + self.r1 + self.r0 + self.cur_len) >= (4 * hp_est)
            promote = (
                cand4
                & ok3_prev
                & ok4_cur
                & (self.cur_len == h3)
                & (hp_est == h3)
                & two_full_periods
            )
            if promote.any():
                self.in_flicker[promote] = True
                self.half_period[promote] = hp_est[promote]

        states[self.in_flicker] = C_FLICKER
        self.prev_hash[:] = h
        return states, self.half_period.copy()
