"""Early declaration per-tile FLICKER tracker variant for V2 claim-only post-pass.

Recognises periodic two-state descriptor-hash toggling with half-period in
1..max_half frames, tolerating +/-1 frame period jitter. Declares FLICKER (4)
early on the third toggle as soon as two complete runs exist, agree within +/-1
frame, and the new hash equals the hash two runs earlier.

Emits FLICKER (4) ONLY on toggle frames inside an established two-state cycle.
On held frames where the tile content does not change, emits UNCHANGED (0).

Includes a guard that blocks the tile for 2 * max_half frames once more than 2
distinct hashes are observed in a sliding window of size 2 * max_half frames.

OPS counts the number of elementary pixel or element operations performed across
all array comparisons, transformations, and reductions.
"""
import numpy as np

C_UNCHANGED, C_JITTER, C_CHANGED, C_MOVED, C_FLICKER, C_FROZEN, C_DRIFT = range(7)
UNCHANGED = C_UNCHANGED
CHANGED = C_CHANGED
FLICKER = C_FLICKER

_U64_MASK = (1 << 64) - 1

OPS = 0


def reset_ops():
    """Reset the module-level operation counter to zero."""
    global OPS
    OPS = 0


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
    """Per-tile early-declaration FLICKER tracker variant."""

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
        w = 2 * self.max_half
        self.initialized = False
        self.prev_hash = np.zeros(n, dtype=np.uint64)
        self.cur_len = np.zeros(n, dtype=np.int64)
        self.first_toggle_done = np.zeros(n, dtype=bool)
        self.r1 = np.zeros(n, dtype=np.int64)
        self.r2 = np.zeros(n, dtype=np.int64)
        self.h1 = np.zeros(n, dtype=np.uint64)
        self.h2 = np.zeros(n, dtype=np.uint64)
        self.n_complete_runs = np.zeros(n, dtype=np.int64)
        self.blocked_counter = np.zeros(n, dtype=np.int64)
        self.frame_idx = 0
        self.history = np.zeros((n, w), dtype=np.uint64)

    def update(self, hashes):
        """Feed one descriptor hash per tile for the current frame.

        Returns (states, half_periods) as int64 numpy arrays of shape (n_tiles,).
        """
        global OPS
        h = _as_u64_hashes(hashes, self.n_tiles)
        n = self.n_tiles
        w = 2 * self.max_half

        if not self.initialized:
            self.initialized = True
            self.prev_hash[:] = h
            self.cur_len[:] = 1
            self.history[:, 0] = h
            self.frame_idx = 0
            OPS += int(n * 4)
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        self.frame_idx += 1
        f = self.frame_idx
        self.history[:, f % w] = h
        OPS += int(n)

        # Check for > 2 distinct hashes in recent window of size <= w
        win_len = min(f + 1, w)
        w_arr = self.history[:, :win_len]
        u1 = w_arr[:, 0:1]
        diff1 = (w_arr != u1)
        first_diff_idx = np.argmax(diff1, axis=1)
        u2 = w_arr[np.arange(n), first_diff_idx][:, None]
        diff2 = diff1 & (w_arr != u2)
        gt2_distinct = diff2.any(axis=1)
        OPS += int(win_len * n * 3 + n * 2)

        # Guard: block tile for 2 * max_half frames if > 2 distinct hashes seen in window
        self.blocked_counter[gt2_distinct] = w
        self.n_complete_runs[gt2_distinct] = 0
        self.first_toggle_done[gt2_distinct] = False
        OPS += int(n * 2)

        same = (h == self.prev_hash)
        changed = ~same
        OPS += int(n * 2)

        states = np.zeros(n, dtype=np.int64)
        half_periods = np.zeros(n, dtype=np.int64)

        # Branch 1: tile hash unchanged from previous frame
        if same.any():
            self.cur_len[same] += 1
            over_max = same & (self.cur_len > self.max_half)
            if over_max.any():
                self.n_complete_runs[over_max] = 0
                self.first_toggle_done[over_max] = False
            OPS += int(n * 4)

        # Branch 2: tile hash changed from previous frame
        if changed.any():
            states[changed] = C_CHANGED
            prev_len = self.cur_len[changed]
            new_h = h[changed]

            # A run ends at a toggle frame
            valid_run = (prev_len <= self.max_half) & (prev_len >= 1)
            invalid_indices = np.where(changed)[0][~valid_run]
            valid_indices = np.where(changed)[0][valid_run]

            if len(invalid_indices) > 0:
                self.n_complete_runs[invalid_indices] = 0
                self.first_toggle_done[invalid_indices] = False

            if len(valid_indices) > 0:
                prev_len_valid = prev_len[valid_run]
                new_h_valid = new_h[valid_run]

                # Check which valid tiles are on their 1st toggle (end of run 0 before observation)
                is_first = ~self.first_toggle_done[valid_indices]
                first_indices = valid_indices[is_first]
                subsequent_indices = valid_indices[~is_first]

                if len(first_indices) > 0:
                    # First toggle ends run 0 (started before observation, invalid as evidence)
                    self.first_toggle_done[first_indices] = True
                    self.r1[first_indices] = prev_len_valid[is_first]
                    self.h1[first_indices] = self.prev_hash[first_indices]
                    self.n_complete_runs[first_indices] = 0

                if len(subsequent_indices) > 0:
                    # Subsequent toggles end complete runs (Run 1, Run 2, ...)
                    self.r2[subsequent_indices] = self.r1[subsequent_indices]
                    self.h2[subsequent_indices] = self.h1[subsequent_indices]
                    self.r1[subsequent_indices] = prev_len_valid[~is_first]
                    self.h1[subsequent_indices] = self.prev_hash[subsequent_indices]
                    self.n_complete_runs[subsequent_indices] += 1

                # Check FLICKER declaration conditions:
                # 1. At least 2 complete runs
                # 2. Runs agree within +/- 1 frame
                # 3. New hash equals hash two runs earlier (h2)
                # 4. Tile not blocked by > 2 distinct hashes guard
                # 5. Run lengths <= max_half
                has_2_runs = self.n_complete_runs[valid_indices] >= 2
                jitter_ok = np.abs(self.r1[valid_indices] - self.r2[valid_indices]) <= 1
                hash_match = new_h_valid == self.h2[valid_indices]
                not_blocked = self.blocked_counter[valid_indices] == 0
                within_max = (self.r1[valid_indices] <= self.max_half) & (self.r2[valid_indices] <= self.max_half)

                flick_mask = has_2_runs & jitter_ok & hash_match & not_blocked & within_max
                flick_idx = valid_indices[flick_mask]

                if len(flick_idx) > 0:
                    states[flick_idx] = C_FLICKER
                    # Estimate half period as average of r1 and r2 rounded
                    half_periods[flick_idx] = (self.r1[flick_idx] + self.r2[flick_idx] + 1) // 2

            self.cur_len[changed] = 1
            OPS += int(n * 12)

        self.prev_hash[:] = h
        self.blocked_counter[self.blocked_counter > 0] -= 1
        OPS += int(n * 2)

        return states, half_periods
