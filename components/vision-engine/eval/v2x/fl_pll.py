"""Phase-locked loop (PLL) toggle tracker variant for per-tile FLICKER detection.

Algorithm: phase-locked toggle tracker:
- Tracks two-state descriptor hash toggles per tile.
- After two complete runs of similar length, predicts the frame of the next toggle.
- Counts a hit when a toggle arrives within +/-1 frame of the prediction and the new hash equals the expected one.
- Locks after 2 consecutive hits.
- Emits FLICKER (4) ONLY on hit toggles while locked (never on held frames where hash did not change).
- Loses lock on a miss (stalled phase without toggle, toggle far from prediction, or a third hash).
- Re-estimates the period by an integer moving average of the last 4 runs.

Operations counter unit (OPS):
Elementary pixel or element operation count, measured as the size in elements of every
numpy array compared, transformed, sliced, assigned, or reduced during tracker updates.
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
    """Reset the global operations counter to 0."""
    global OPS
    OPS = 0
    return 0


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
    """Per-tile phase-locked loop toggle tracker for FLICKER detection."""

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
        """Reset all per-tile tracker automaton state."""
        n = self.n_tiles
        self.initialized = False
        self.prev_hash = np.zeros(n, dtype=np.uint64)
        self.state_a = np.zeros(n, dtype=np.uint64)
        self.state_b = np.zeros(n, dtype=np.uint64)
        self.has_b = np.zeros(n, dtype=bool)
        self.phase = np.zeros(n, dtype=np.int64)  # 0 for state_a, 1 for state_b
        self.cur_len = np.zeros(n, dtype=np.int64)
        self.r0 = np.zeros(n, dtype=np.int64)
        self.r1 = np.zeros(n, dtype=np.int64)
        self.r2 = np.zeros(n, dtype=np.int64)
        self.r3 = np.zeros(n, dtype=np.int64)
        self.n_runs = np.zeros(n, dtype=np.int64)
        self.r_phase0 = np.zeros(n, dtype=np.int64)
        self.r_phase1 = np.zeros(n, dtype=np.int64)
        self.prev_r_phase0 = np.zeros(n, dtype=np.int64)
        self.prev_r_phase1 = np.zeros(n, dtype=np.int64)
        self.n_phase0 = np.zeros(n, dtype=np.int64)
        self.n_phase1 = np.zeros(n, dtype=np.int64)
        self.predicting = np.zeros(n, dtype=bool)
        self.predicted_len = np.zeros(n, dtype=np.int64)
        self.hits = np.zeros(n, dtype=np.int64)
        self.locked = np.zeros(n, dtype=bool)
        self.est_half = np.zeros(n, dtype=np.int64)

    def update(self, hashes):
        """Feed one descriptor hash per tile for current frame.

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
            self.phase[:] = 0
            self.cur_len[:] = 1
            OPS += n * 5
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        same = (h == self.prev_hash)
        changed = ~same
        OPS += n * 2

        states = np.full(n, C_UNCHANGED, dtype=np.int64)
        half_periods = np.zeros(n, dtype=np.int64)
        OPS += n * 2

        # --- Branch 1: Unchanged frames (held phase) ---
        if same.any():
            self.cur_len[same] += 1
            OPS += same.sum()

            # Stalled condition: held longer than max_half OR past predicted frame + 1
            over_max = same & (self.cur_len > self.max_half)
            over_pred = same & self.predicting & (self.cur_len > self.predicted_len + 1)
            stalled = over_max | over_pred
            OPS += same.sum() * 3

            if stalled.any():
                self.predicting[stalled] = False
                self.hits[stalled] = 0
                self.locked[stalled] = False
                self.est_half[stalled] = 0
                OPS += stalled.sum() * 4

        # --- Branch 2: Changed frames (Toggles) ---
        if changed.any():
            states[changed] = C_CHANGED
            OPS += changed.sum()

            last_run = self.cur_len.copy()
            OPS += n

            # Check for a 3rd state (neither state_a nor state_b)
            is_third = changed & self.has_b & (h != self.state_a) & (h != self.state_b)
            OPS += changed.sum() * 3

            if is_third.any():
                self.state_a[is_third] = h[is_third]
                self.has_b[is_third] = False
                self.phase[is_third] = 0
                self.cur_len[is_third] = 1
                self.n_runs[is_third] = 0
                self.r0[is_third] = 0
                self.r1[is_third] = 0
                self.r2[is_third] = 0
                self.r3[is_third] = 0
                self.r_phase0[is_third] = 0
                self.r_phase1[is_third] = 0
                self.prev_r_phase0[is_third] = 0
                self.prev_r_phase1[is_third] = 0
                self.n_phase0[is_third] = 0
                self.n_phase1[is_third] = 0
                self.predicting[is_third] = False
                self.predicted_len[is_third] = 0
                self.hits[is_third] = 0
                self.locked[is_third] = False
                self.est_half[is_third] = 0
                OPS += is_third.sum() * 16

            two_state = changed & (~is_third)
            OPS += changed.sum()

            if two_state.any():
                self.cur_len[two_state] = 1
                OPS += two_state.sum()

                first_b = two_state & (~self.has_b)
                OPS += two_state.sum()
                if first_b.any():
                    self.state_b[first_b] = h[first_b]
                    self.has_b[first_b] = True
                    OPS += first_b.sum() * 2

                too_long = two_state & (last_run > self.max_half)
                OPS += two_state.sum()
                if too_long.any():
                    self.predicting[too_long] = False
                    self.hits[too_long] = 0
                    self.locked[too_long] = False
                    self.est_half[too_long] = 0
                    self.n_runs[too_long] = 0
                    self.n_phase0[too_long] = 0
                    self.n_phase1[too_long] = 0
                    OPS += too_long.sum() * 7

                valid_toggle = two_state & (last_run <= self.max_half)
                OPS += two_state.sum()

                if valid_toggle.any():
                    curr_phase = np.where(h[valid_toggle] == self.state_b[valid_toggle], 1, 0)
                    prev_phase = 1 - curr_phase
                    OPS += valid_toggle.sum() * 2

                    r0_prev = self.r0.copy()
                    r1_prev = self.r1.copy()
                    r2_prev = self.r2.copy()

                    self.r3[valid_toggle] = r2_prev[valid_toggle]
                    self.r2[valid_toggle] = r1_prev[valid_toggle]
                    self.r1[valid_toggle] = r0_prev[valid_toggle]
                    self.r0[valid_toggle] = last_run[valid_toggle]
                    self.n_runs[valid_toggle] += 1
                    OPS += valid_toggle.sum() * 5

                    # Update phase-specific history
                    vt_indices = np.flatnonzero(valid_toggle)
                    p0_sub = (prev_phase == 0)
                    if p0_sub.any():
                        idx0 = vt_indices[p0_sub]
                        self.prev_r_phase0[idx0] = self.r_phase0[idx0]
                        self.r_phase0[idx0] = last_run[idx0]
                        self.n_phase0[idx0] += 1
                        OPS += idx0.size * 3

                    p1_sub = (prev_phase == 1)
                    if p1_sub.any():
                        idx1 = vt_indices[p1_sub]
                        self.prev_r_phase1[idx1] = self.r_phase1[idx1]
                        self.r_phase1[idx1] = last_run[idx1]
                        self.n_phase1[idx1] += 1
                        OPS += idx1.size * 3

                    # Evaluate hit / miss for predicting tiles
                    eval_pred = valid_toggle & self.predicting
                    OPS += valid_toggle.sum()
                    if eval_pred.any():
                        hit = eval_pred & (np.abs(last_run - self.predicted_len) <= 1)
                        miss = eval_pred & (~hit)
                        OPS += eval_pred.sum() * 2

                        if hit.any():
                            self.hits[hit] += 1
                            lock_now = hit & (self.hits >= 2)
                            self.locked[lock_now] = True
                            OPS += hit.sum() * 2

                            was_locked_hit = hit & self.locked
                            if was_locked_hit.any():
                                states[was_locked_hit] = C_FLICKER
                                OPS += was_locked_hit.sum()

                        if miss.any():
                            self.hits[miss] = 0
                            self.locked[miss] = False
                            self.predicting[miss] = False
                            self.est_half[miss] = 0
                            OPS += miss.sum() * 4

                    # Period estimation: integer moving average of last 4 runs
                    nr = self.n_runs[valid_toggle]
                    r0_v = self.r0[valid_toggle]
                    r1_v = self.r1[valid_toggle]
                    r2_v = self.r2[valid_toggle]
                    r3_v = self.r3[valid_toggle]

                    avg_4 = (r0_v + r1_v + r2_v + r3_v + 2) // 4
                    avg_2 = (r0_v + r1_v + 1) // 2
                    half_est = np.where(nr >= 4, avg_4, np.where(nr >= 2, avg_2, r0_v))
                    self.est_half[valid_toggle] = half_est
                    OPS += valid_toggle.sum() * 6

                    # Setup prediction for upcoming phase (curr_phase)
                    sym_ok = (self.n_runs[valid_toggle] >= 2) & (np.abs(self.r0[valid_toggle] - self.r1[valid_toggle]) <= 2)

                    np0 = self.n_phase0[valid_toggle]
                    np1 = self.n_phase1[valid_toggle]
                    rp0 = self.r_phase0[valid_toggle]
                    rp1 = self.r_phase1[valid_toggle]
                    prp0 = self.prev_r_phase0[valid_toggle]
                    prp1 = self.prev_r_phase1[valid_toggle]

                    pred_len_curr = np.where(
                        curr_phase == 0,
                        np.where(np0 >= 2, (rp0 + prp0 + 1) // 2, rp0),
                        np.where(np1 >= 2, (rp1 + prp1 + 1) // 2, rp1)
                    )

                    asym_similar = np.where(
                        curr_phase == 0,
                        (np0 < 2) | (np.abs(rp0 - prp0) <= 2),
                        (np1 < 2) | (np.abs(rp1 - prp1) <= 2)
                    )

                    asym_ok = (self.n_runs[valid_toggle] >= 2) & (np0 >= 1) & (np1 >= 1) & asym_similar

                    can_predict_sub = (sym_ok | asym_ok)
                    can_predict_vt = vt_indices[can_predict_sub]
                    OPS += valid_toggle.sum() * 8

                    if can_predict_vt.size > 0:
                        self.predicting[can_predict_vt] = True
                        self.predicted_len[can_predict_vt] = pred_len_curr[can_predict_sub]
                        OPS += can_predict_vt.size * 3

                    self.phase[valid_toggle] = curr_phase
                    OPS += valid_toggle.sum()

        flicker_mask = (states == C_FLICKER)
        half_periods[flicker_mask] = self.est_half[flicker_mask]
        self.prev_hash[:] = h
        OPS += n * 3

        return states, half_periods
