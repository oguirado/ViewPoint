"""Per-tile FLICKER tracker variant using spatial hysteresis dead band (fl_deadband).

Method from screened paper:
State update: inside' = (d <= R + M) if inside else (d <= R), in 64-bit integers.
Dead Band Theorem: with margin M zero, alternating jitter yields one event per sample,
with M at least the jitter amplitude exactly one event occurs.

Applied per tile to the run-length statistic:
- d is the absolute difference between the current run length and the previous run of the same phase.
- R = 1 frame, M = 1 frame.
- A tile enters the periodic state when same-phase run bounds pass (d <= R).
- Emits FLICKER (4) only on toggle frames inside a two-hash cycle while inside periodic state.
- Leaves periodic state only when a run fails the widened bound R + M (d > R + M).
- Half period is the latest run length clipped to 1..max_half.
- A third hash resets the tile.

Exposes module-level integer OPS and reset_ops().
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


def deadband_step(state, d, R, M):
    """Pure function deadband_step for spatial hysteresis dead band.

    inside' = (d <= R + M) if inside else (d <= R), in 64-bit integers.
    """
    if isinstance(state, np.ndarray) or isinstance(d, np.ndarray):
        state_bool = np.asarray(state, dtype=bool)
        d_arr = np.asarray(d, dtype=np.int64)
        r_val = np.asarray(R, dtype=np.int64)
        m_val = np.asarray(M, dtype=np.int64)
        return np.where(state_bool, d_arr <= (r_val + m_val), d_arr <= r_val)
    st = bool(state)
    d_val = int(d)
    r_val = int(R)
    m_val = int(M)
    limit = r_val + m_val if st else r_val
    return d_val <= limit


def _as_u64_hashes(hashes, n_tiles):
    """Convert a 1-D sequence of length n_tiles of ints to a uint64 numpy array."""
    global OPS
    OPS += int(n_tiles)
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
    """Per-tile FLICKER tracker using spatial hysteresis dead band."""

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
        self.last_run_a = np.zeros(n, dtype=np.int64)
        self.last_run_b = np.zeros(n, dtype=np.int64)
        self.has_run_a = np.zeros(n, dtype=bool)
        self.has_run_b = np.zeros(n, dtype=bool)
        self.inside = np.zeros(n, dtype=bool)
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
            self.cur_len[:] = 1
            self.inside[:] = False
            self.has_run_a[:] = False
            self.has_run_b[:] = False
            self.half_period[:] = 0
            OPS += n * 8
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

            # Tiles exceeding max_half while held drop claim / reset
            over_max = same & self.inside & (self.cur_len > self.max_half + 1)
            OPS += n * 3
            if over_max.any():
                m = np.count_nonzero(over_max)
                self.inside[over_max] = False
                self.half_period[over_max] = 0
                self.has_run_a[over_max] = False
                self.has_run_b[over_max] = False
                OPS += m * 4

            over_max_unlocked = same & (~self.inside) & (self.cur_len > self.max_half)
            OPS += n * 3
            if over_max_unlocked.any():
                m = np.count_nonzero(over_max_unlocked)
                self.has_run_a[over_max_unlocked] = False
                self.has_run_b[over_max_unlocked] = False
                OPS += m * 2

        # --- Toggle frames (changed hash) ---
        if changed.any():
            m_changed = np.count_nonzero(changed)
            states[changed] = C_CHANGED
            OPS += m_changed

            prev_len = self.cur_len.copy()
            old_has_b = self.has_b.copy()
            OPS += n * 2

            # Check for third hash: has_b is True and new hash is neither state_a nor state_b
            third = changed & old_has_b & (h != self.state_a) & (h != self.state_b)
            OPS += n * 4
            if third.any():
                m = np.count_nonzero(third)
                self.inside[third] = False
                self.state_a[third] = h[third]
                self.has_b[third] = False
                self.has_run_a[third] = False
                self.has_run_b[third] = False
                self.cur_len[third] = 1
                self.half_period[third] = 0
                OPS += m * 7

            two_state = changed & (~third)
            OPS += n * 2
            if two_state.any():
                # First toggle to state_b
                first_b = two_state & (~old_has_b)
                OPS += n * 2
                if first_b.any():
                    m = np.count_nonzero(first_b)
                    self.state_b[first_b] = h[first_b]
                    self.has_b[first_b] = True
                    self.last_run_a[first_b] = prev_len[first_b]
                    self.has_run_a[first_b] = True
                    self.cur_len[first_b] = 1
                    self.inside[first_b] = False
                    OPS += m * 6

                # Subsequent toggles within two-state cycle
                subseq = two_state & old_has_b
                OPS += n * 2
                if subseq.any():
                    # Phase of completed run
                    was_a = subseq & (self.prev_hash == self.state_a)
                    was_b = subseq & (self.prev_hash == self.state_b)
                    OPS += n * 3

                    # Process phase A completions
                    if was_a.any():
                        m_a = np.count_nonzero(was_a)
                        has_prev = was_a & self.has_run_a
                        OPS += m_a
                        if has_prev.any():
                            d_a = np.abs(prev_len[has_prev] - self.last_run_a[has_prev])
                            st_prime = deadband_step(self.inside[has_prev], d_a, R=1, M=1)
                            self.inside[has_prev] = st_prime
                            OPS += np.count_nonzero(has_prev) * 4
                        self.last_run_a[was_a] = prev_len[was_a]
                        self.has_run_a[was_a] = True
                        OPS += m_a * 2

                    # Process phase B completions
                    if was_b.any():
                        m_b = np.count_nonzero(was_b)
                        has_prev = was_b & self.has_run_b
                        OPS += m_b
                        if has_prev.any():
                            d_b = np.abs(prev_len[has_prev] - self.last_run_b[has_prev])
                            st_prime = deadband_step(self.inside[has_prev], d_b, R=1, M=1)
                            self.inside[has_prev] = st_prime
                            OPS += np.count_nonzero(has_prev) * 4
                        self.last_run_b[was_b] = prev_len[was_b]
                        self.has_run_b[was_b] = True
                        OPS += m_b * 2

                    self.cur_len[subseq] = 1
                    OPS += np.count_nonzero(subseq)

                    # Emit FLICKER (4) for tiles that are inside (periodic state)
                    emit_mask = subseq & self.inside
                    OPS += n
                    if emit_mask.any():
                        m_emit = np.count_nonzero(emit_mask)
                        hp = np.clip(prev_len[emit_mask], 1, self.max_half)
                        self.half_period[emit_mask] = hp
                        states[emit_mask] = C_FLICKER
                        out_half_period[emit_mask] = hp
                        OPS += m_emit * 4

        self.prev_hash[:] = h
        OPS += n
        return states, out_half_period
