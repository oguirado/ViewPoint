"""FLICKER tracker variant: fl_fadeguard (FadeGuard conservative FLICKER tracker).

Tracks periodic two-state descriptor-hash toggling per tile with distinctness memory:
keeps a small set of distinct hashes seen within the last 2 * max_half frames with
their last-seen frame timestamps. Any tile with > 2 distinct hashes in that window
is blocked from declaring or emitting FLICKER, rejecting slow fades and scrolls.

Unblocked tiles declare after 3 complete runs within +/- 1 frame of a common
integer half-period in 1..max_half, and emit state 4 (FLICKER) ONLY on toggle
frames where the new hash matches the hash from two runs earlier inside an
established two-state cycle. On held frames, state 0 (UNCHANGED) or 2 (CHANGED) is returned.

Work unit:
OPS counts elementary array operations performed across tiles in each update call,
measured as the total number of elements processed in comparisons, assignments,
and vector reductions.
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
    """Reset global operation counter to zero."""
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


def _estimate_half_period_3(r0, r1, r2):
    """Estimate nominal half-period over 3 compatible run lengths."""
    rmin = np.minimum(r0, np.minimum(r1, r2))
    rmax = np.maximum(r0, np.maximum(r1, r2))
    center = (rmin + rmax) // 2
    mean_round = (r0 + r1 + r2 + 1) // 3
    return np.where(rmax - rmin == 2, center, mean_round)


class FlickerTracker:
    """Per-tile FadeGuard FLICKER tracker with distinctness memory."""

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
        self.frame_idx = 0

        # Distinctness memory: up to 3 slots per tile with last-seen frame index
        # slot_h shape: (n_tiles, 3), slot_t shape: (n_tiles, 3)
        self.slot_h = np.zeros((n, 3), dtype=np.uint64)
        self.slot_t = np.full((n, 3), -1, dtype=np.int64)

        self.prev_hash = np.zeros(n, dtype=np.uint64)
        self.state_a = np.zeros(n, dtype=np.uint64)
        self.state_b = np.zeros(n, dtype=np.uint64)
        self.has_b = np.zeros(n, dtype=bool)

        self.cur_len = np.zeros(n, dtype=np.int64)
        self.n_runs = np.zeros(n, dtype=np.int64)
        self.r0 = np.zeros(n, dtype=np.int64)  # most recent completed run
        self.r1 = np.zeros(n, dtype=np.int64)  # second most recent completed run
        self.r2 = np.zeros(n, dtype=np.int64)  # third most recent completed run

        self.in_flicker = np.zeros(n, dtype=bool)
        self.half_period = np.zeros(n, dtype=np.int64)

    def update(self, hashes):
        """Feed one descriptor hash per tile for current frame.

        Returns (states, half_periods) as int64 numpy arrays of shape (n_tiles,).
        """
        import eval.v2x.fl_fadeguard as _mod
        n = self.n_tiles
        h = _as_u64_hashes(hashes, n)
        cur_t = self.frame_idx
        self.frame_idx += 1
        window = 2 * self.max_half

        _mod.OPS += n  # hash ingestion & conversion count

        if not self.initialized:
            self.initialized = True
            self.prev_hash[:] = h
            self.slot_h[:, 0] = h
            self.slot_t[:, 0] = cur_t
            self.state_a[:] = h
            self.has_b[:] = False
            self.cur_len[:] = 1
            self.n_runs[:] = 0
            self.in_flicker[:] = False
            self.half_period[:] = 0
            _mod.OPS += 8 * n
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        # 1. Update distinctness memory for each tile
        # Expire slots older than cur_t - window
        expired = (self.slot_t < (cur_t - window)) & (self.slot_t >= 0)
        self.slot_t[expired] = -1
        _mod.OPS += 3 * n

        # Match current hash h against active slots
        m0 = (self.slot_h[:, 0] == h) & (self.slot_t[:, 0] >= 0)
        m1 = (self.slot_h[:, 1] == h) & (self.slot_t[:, 1] >= 0)
        m2 = (self.slot_h[:, 2] == h) & (self.slot_t[:, 2] >= 0)
        matched = m0 | m1 | m2
        _mod.OPS += 6 * n

        # Update last-seen timestamp for matched slots
        self.slot_t[m0, 0] = cur_t
        self.slot_t[m1, 1] = cur_t
        self.slot_t[m2, 2] = cur_t
        _mod.OPS += 3 * n

        # Insert new hash into free/expired slot or overwrite oldest slot if unmatched
        unmatched = ~matched
        if unmatched.any():
            idx_unmatched = np.flatnonzero(unmatched)
            for i in idx_unmatched:
                hi = h[i]
                st = self.slot_t[i]
                free_slots = np.flatnonzero(st < 0)
                if len(free_slots) > 0:
                    s_idx = free_slots[0]
                else:
                    s_idx = np.argmin(st)
                self.slot_h[i, s_idx] = hi
                self.slot_t[i, s_idx] = cur_t
            _mod.OPS += len(idx_unmatched) * 6

        # Recount active distinct hashes in window
        active_slots = self.slot_t >= 0
        active_count = np.sum(active_slots, axis=1)
        blocked = active_count > 2
        _mod.OPS += 4 * n

        # 2. Check hash changes and run tracking
        same = h == self.prev_hash
        changed = ~same
        _mod.OPS += 2 * n

        states = np.zeros(n, dtype=np.int64)

        # --- Branch 1: hash unchanged ---
        if same.any():
            self.cur_len[same] += 1
            _mod.OPS += np.count_nonzero(same)

            # Stalling / over max_half check
            over_max = same & (self.cur_len > self.max_half)
            if over_max.any():
                self.in_flicker[over_max] = False
                self.half_period[over_max] = 0
                self.n_runs[over_max] = 0
                _mod.OPS += 3 * np.count_nonzero(over_max)

            # Active FLICKER tiles that exceed half_period + 1 stall
            active_same = same & self.in_flicker
            if active_same.any():
                stalled = active_same & (self.cur_len > self.half_period + 1)
                if stalled.any():
                    self.in_flicker[stalled] = False
                    self.half_period[stalled] = 0
                    self.n_runs[stalled] = 0
                    _mod.OPS += 3 * np.count_nonzero(stalled)

        # --- Branch 2: hash changed ---
        if changed.any():
            states[changed] = C_CHANGED
            _mod.OPS += np.count_nonzero(changed)

            # Check for 3rd state relative to active 2-state cycle (state_a, state_b)
            third = changed & self.has_b & (h != self.state_a) & (h != self.state_b)
            if third.any():
                self.state_a[third] = h[third]
                self.has_b[third] = False
                self.cur_len[third] = 1
                self.n_runs[third] = 0
                self.in_flicker[third] = False
                self.half_period[third] = 0
                _mod.OPS += 6 * np.count_nonzero(third)

            two_state = changed & (~third)
            if two_state.any():
                first_b = two_state & (~self.has_b)
                if first_b.any():
                    self.state_b[first_b] = h[first_b]
                    self.has_b[first_b] = True
                    _mod.OPS += 2 * np.count_nonzero(first_b)

                prev_len = self.cur_len.copy()
                too_long = two_state & (prev_len > self.max_half)
                if too_long.any():
                    self.n_runs[too_long] = 0
                    self.in_flicker[too_long] = False
                    self.half_period[too_long] = 0
                    self.cur_len[too_long] = 1
                    _mod.OPS += 4 * np.count_nonzero(too_long)

                valid_run = two_state & (prev_len <= self.max_half)
                if valid_run.any():
                    # Just completed run `prev_len`. Shift history:
                    nr0 = prev_len
                    nr1 = self.r0.copy()
                    nr2 = self.r1.copy()
                    old_n = self.n_runs.copy()

                    # Check compatibility of completed runs
                    ok2 = (old_n >= 1) & (np.abs(nr0 - nr1) <= 2)

                    rmin3 = np.minimum(nr0, np.minimum(nr1, nr2))
                    rmax3 = np.maximum(nr0, np.maximum(nr1, nr2))
                    h3 = np.where(rmax3 - rmin3 == 2, (rmin3 + rmax3) // 2, (nr0 + nr1 + nr2 + 1) // 3)
                    ok3 = (
                        ok2
                        & (old_n >= 2)
                        & ((rmax3 - rmin3) <= 2)
                        & (np.abs(nr0 - h3) <= 1)
                        & (np.abs(nr1 - h3) <= 1)
                        & (np.abs(nr2 - h3) <= 1)
                    )

                    new_n = np.where(ok3, old_n + 1, np.where(ok2, 2, 1))

                    self.r0[valid_run] = nr0[valid_run]
                    self.r1[valid_run] = nr1[valid_run]
                    self.r2[valid_run] = nr2[valid_run]
                    self.n_runs[valid_run] = new_n[valid_run]
                    self.cur_len[valid_run] = 1
                    _mod.OPS += 15 * np.count_nonzero(valid_run)

                    # Check FLICKER declaration or continuation upon completing a valid run (new_n >= 3)
                    flick = valid_run & (new_n >= 3) & (~blocked)
                    if flick.any():
                        hp_est = _estimate_half_period_3(self.r0, self.r1, self.r2)
                        cycle_match = self.has_b & (
                            ((self.n_runs % 2 == 1) & (h == self.state_b))
                            | ((self.n_runs % 2 == 0) & (h == self.state_a))
                        )
                        flick_ok = flick & cycle_match
                        self.in_flicker[flick_ok] = True
                        self.half_period[flick_ok] = hp_est[flick_ok]

                        flick_fail = flick & (~cycle_match)
                        self.in_flicker[flick_fail] = False
                        self.half_period[flick_fail] = 0
                        _mod.OPS += 4 * np.count_nonzero(flick)

                    # Reset flicker if run sequence broke (new_n < 3)
                    broken = valid_run & (new_n < 3)
                    if broken.any():
                        self.in_flicker[broken] = False
                        self.half_period[broken] = 0
                        _mod.OPS += 2 * np.count_nonzero(broken)

        # Apply blocking: any blocked tile loses FLICKER state
        if blocked.any():
            self.in_flicker[blocked] = False
            self.half_period[blocked] = 0
            _mod.OPS += 2 * np.count_nonzero(blocked)

        # 3. Emission rule: emit state 4 ONLY on toggle frames for active, unblocked FLICKER tiles
        emit_flicker = changed & self.in_flicker & (~blocked)
        states[emit_flicker] = C_FLICKER
        self.prev_hash[:] = h
        _mod.OPS += 3 * n

        out_hp = np.where(self.in_flicker & (~blocked), self.half_period, 0)
        return states, out_hp
