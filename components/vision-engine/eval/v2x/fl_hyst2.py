"""Per-tile FLICKER tracker variant with explicit two-state automaton and hysteresis (fl_hyst2).

Unit of OPS: Count of elementary per-element operations performed on NumPy arrays.
Each array comparison, logical operation, indexing/masking, arithmetic update, or reduction
adds the total size (number of elements) of the involved arrays or operated subset to OPS.
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

STATE_IDLE = 0
STATE_CANDIDATE = 1
STATE_LOCKED = 2

OPS = 0


def reset_ops():
    """Reset the global OPS counter to zero."""
    global OPS
    OPS = 0


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


class FlickerTracker:
    """Per-tile FLICKER tracker using explicit two-state automaton with hysteresis."""

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
        self.aut_state = np.zeros(n, dtype=np.int64)  # 0=IDLE, 1=CANDIDATE, 2=LOCKED
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
        n = self.n_tiles
        h = _as_u64_hashes(hashes, n)

        if not self.initialized:
            self.initialized = True
            self.prev_hash[:] = h
            self.state_a[:] = h
            self.has_b[:] = False
            self.aut_state[:] = STATE_IDLE
            self.cur_len[:] = 1
            self.n_runs[:] = 0
            self.half_period[:] = 0
            OPS += n * 7
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        same = (h == self.prev_hash)
        changed = ~same
        OPS += n * 2

        states = np.full(n, C_UNCHANGED, dtype=np.int64)
        out_half_period = np.zeros(n, dtype=np.int64)
        OPS += n * 2

        # --- Held frames (same hash) ---
        if same.any():
            m_same = np.count_nonzero(same)
            self.cur_len[same] += 1
            OPS += m_same

            # Locked tiles where a toggle was missed (exceeded half_period + 1) -> fall to IDLE
            stalled_locked = same & (self.aut_state == STATE_LOCKED) & (self.cur_len > self.half_period + 1)
            OPS += n * 3
            if stalled_locked.any():
                m = np.count_nonzero(stalled_locked)
                self.aut_state[stalled_locked] = STATE_IDLE
                self.half_period[stalled_locked] = 0
                self.n_runs[stalled_locked] = 0
                self.has_b[stalled_locked] = False
                OPS += m * 4

            # Non-locked tiles exceeding max_half -> reset to IDLE
            over_max = same & (self.aut_state != STATE_LOCKED) & (self.cur_len > self.max_half)
            OPS += n * 3
            if over_max.any():
                m = np.count_nonzero(over_max)
                self.aut_state[over_max] = STATE_IDLE
                self.half_period[over_max] = 0
                self.n_runs[over_max] = 0
                self.has_b[over_max] = False
                OPS += m * 4

        # --- Toggle frames (changed hash) ---
        if changed.any():
            m_changed = np.count_nonzero(changed)
            states[changed] = C_CHANGED
            OPS += m_changed

            old_has_b = self.has_b.copy()
            old_n_runs = self.n_runs.copy()
            old_aut_state = self.aut_state.copy()
            OPS += n * 3

            # Third hash check: has_b is True and new hash is neither state_a nor state_b
            third = changed & old_has_b & (h != self.state_a) & (h != self.state_b)
            OPS += n * 4
            if third.any():
                m = np.count_nonzero(third)
                self.aut_state[third] = STATE_IDLE
                self.state_a[third] = h[third]
                self.has_b[third] = False
                self.cur_len[third] = 1
                self.n_runs[third] = 0
                self.half_period[third] = 0
                OPS += m * 6

            two_state = changed & (~third)
            OPS += n * 2
            if two_state.any():
                prev_len = self.cur_len.copy()
                OPS += n

                # First toggle to state_b (first_b)
                first_b = two_state & (~old_has_b)
                OPS += n * 2
                if first_b.any():
                    m = np.count_nonzero(first_b)
                    self.state_b[first_b] = h[first_b]
                    self.has_b[first_b] = True
                    self.r0[first_b] = prev_len[first_b]
                    self.n_runs[first_b] = 1
                    self.cur_len[first_b] = 1
                    self.aut_state[first_b] = STATE_IDLE
                    OPS += m * 6

                # Subsequent toggles within two-state cycle
                subseq = two_state & old_has_b
                OPS += n * 2
                if subseq.any():
                    is_locked = old_aut_state == STATE_LOCKED
                    too_long = subseq & (
                        (is_locked & (prev_len > self.half_period + 1))
                        | ((~is_locked) & (prev_len > self.max_half))
                        | (prev_len > self.max_half)
                    )
                    OPS += n * 5
                    if too_long.any():
                        m = np.count_nonzero(too_long)
                        self.aut_state[too_long] = STATE_IDLE
                        self.half_period[too_long] = 0
                        self.n_runs[too_long] = 0
                        self.cur_len[too_long] = 1
                        OPS += m * 4

                    valid = subseq & (~too_long)
                    OPS += n * 2
                    if valid.any():
                        # Toggle 2: completes run 1 (r1), transitions IDLE -> CANDIDATE
                        t2 = valid & (old_n_runs == 1)
                        OPS += n * 2
                        if t2.any():
                            m = np.count_nonzero(t2)
                            self.r1[t2] = prev_len[t2]
                            self.n_runs[t2] = 2
                            self.cur_len[t2] = 1
                            self.aut_state[t2] = STATE_CANDIDATE
                            OPS += m * 4

                        # Toggle 3: completes run 2 (r2), checks run agreement to LOCK
                        t3 = valid & (old_n_runs == 2)
                        OPS += n * 2
                        if t3.any():
                            m = np.count_nonzero(t3)
                            self.r2[t3] = prev_len[t3]
                            self.n_runs[t3] = 3
                            self.cur_len[t3] = 1
                            OPS += m * 3

                            # Check complete runs r0, r1, r2 agree within +/-1 frame
                            r0_v = self.r0[t3]
                            r1_v = self.r1[t3]
                            r2_v = prev_len[t3]
                            rmin = np.minimum(np.minimum(r0_v, r1_v), r2_v)
                            rmax = np.maximum(np.maximum(r0_v, r1_v), r2_v)
                            agree = (rmax - rmin) <= 2
                            OPS += m * 5

                            # Estimate half_period
                            center = (rmin + rmax) // 2
                            mean_round = (r0_v + r1_v + r2_v + 1) // 3
                            hp_est = np.where(rmax - rmin == 2, center, mean_round)
                            OPS += m * 5

                            t3_indices = np.flatnonzero(t3)
                            t3_agree = t3_indices[agree]
                            t3_fail = t3_indices[~agree]

                            if len(t3_agree) > 0:
                                self.aut_state[t3_agree] = STATE_LOCKED
                                self.half_period[t3_agree] = hp_est[agree]
                                states[t3_agree] = C_FLICKER
                                out_half_period[t3_agree] = hp_est[agree]
                                OPS += len(t3_agree) * 4

                            if len(t3_fail) > 0:
                                self.aut_state[t3_fail] = STATE_IDLE
                                self.half_period[t3_fail] = 0
                                self.n_runs[t3_fail] = 0
                                OPS += len(t3_fail) * 3

                        # Toggle 4+: subsequent toggles while LOCKED
                        t_sub = valid & (old_n_runs >= 3)
                        OPS += n * 2
                        if t_sub.any():
                            m = np.count_nonzero(t_sub)
                            # Update run history shift
                            self.r0[t_sub] = self.r1[t_sub]
                            self.r1[t_sub] = self.r2[t_sub]
                            self.r2[t_sub] = prev_len[t_sub]
                            self.cur_len[t_sub] = 1
                            OPS += m * 4

                            locked_sub = t_sub & (old_aut_state == STATE_LOCKED)
                            OPS += n * 2
                            if locked_sub.any():
                                ml = np.count_nonzero(locked_sub)
                                states[locked_sub] = C_FLICKER
                                out_half_period[locked_sub] = self.half_period[locked_sub]
                                OPS += ml * 2

        self.prev_hash[:] = h
        OPS += n
        return states, out_half_period
