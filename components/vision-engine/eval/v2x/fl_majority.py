"""Per-tile FLICKER tracker variant using majority vote over 5-toggle windows.

Recognises periodic two-state descriptor-hash toggling by keeping per tile the
last 5 toggle frame indices and the hashes involved. At each toggle frame,
computes the 4 inter-toggle intervals d1, d2, d3, d4.

FLICKER (state 4) is declared on a toggle frame when:
  1. At least 3 of the 4 inter-toggle intervals agree within +/-1 with their
     integer median M = (s1 + s2) // 2 (where s0 <= s1 <= s2 <= s3).
  2. The latest inter-toggle interval d4 = T4 - T3 agrees with M within +/-1.
  3. Every toggle alternates between exactly two hashes (H0 == H2 == H4 and
     H1 == H3 with H4 != H3).
  4. The median half-period M is within 1..max_half frames.

State 4 (FLICKER) is emitted ONLY on frames where the tile hash changed. On
held frames where the hash is unchanged, state 0 (UNCHANGED) is emitted.

Work unit (OPS):
  Module-level integer OPS and reset_ops() function track elementary array/element
  operations (comparisons, indexing, arithmetic, logical ops, and reductions)
  scaled by the number of array elements processed.
"""
import numpy as np

C_UNCHANGED = 0
C_CHANGED = 2
C_FLICKER = 4

UNCHANGED = C_UNCHANGED
CHANGED = C_CHANGED
FLICKER = C_FLICKER

_U64_MASK = (1 << 64) - 1

OPS = 0


def reset_ops():
    """Reset the global elementary operation counter OPS to zero."""
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
    OPS += len(seq)
    if len(seq) != n_tiles:
        raise ValueError("hashes length must equal n_tiles")
    out = np.empty(n_tiles, dtype=np.uint64)
    for i, v in enumerate(seq):
        if isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)):
            raise ValueError("each hash must be an integer")
        out[i] = int(v) & _U64_MASK
    return out


class FlickerTracker:
    """Per-tile deterministic majority-vote automaton for two-state periodic FLICKER."""

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
        self.frame_idx = 0
        self.prev_hash = np.zeros(n, dtype=np.uint64)
        self.n_toggles = np.zeros(n, dtype=np.int64)
        self.last_toggle_frames = np.zeros((n, 5), dtype=np.int64)
        self.last_toggle_hashes = np.zeros((n, 5), dtype=np.uint64)

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
            self.frame_idx = 0
            OPS += n * 2
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        self.frame_idx += 1
        current_frame = self.frame_idx

        same = h == self.prev_hash
        changed = ~same
        OPS += n * 2

        states = np.zeros(n, dtype=np.int64)  # C_UNCHANGED (0)
        half_periods = np.zeros(n, dtype=np.int64)
        OPS += n * 2

        if changed.any():
            states[changed] = C_CHANGED
            OPS += int(changed.sum())

            idx_changed = np.flatnonzero(changed)
            num_changed = idx_changed.size
            OPS += num_changed

            self.n_toggles[idx_changed] += 1
            OPS += num_changed

            # Update 5-toggle frame and hash history for changed tiles
            self.last_toggle_frames[idx_changed, 0:4] = self.last_toggle_frames[idx_changed, 1:5]
            self.last_toggle_frames[idx_changed, 4] = current_frame
            self.last_toggle_hashes[idx_changed, 0:4] = self.last_toggle_hashes[idx_changed, 1:5]
            self.last_toggle_hashes[idx_changed, 4] = h[idx_changed]
            OPS += num_changed * 10

            has_5_local = self.n_toggles[idx_changed] >= 5
            OPS += num_changed

            if has_5_local.any():
                idx_has_5 = idx_changed[has_5_local]
                k = idx_has_5.size
                OPS += k

                tf = self.last_toggle_frames[idx_has_5]  # shape (k, 5)
                th = self.last_toggle_hashes[idx_has_5]  # shape (k, 5)
                OPS += k * 10

                # 4 inter-toggle intervals
                d1 = tf[:, 1] - tf[:, 0]
                d2 = tf[:, 2] - tf[:, 1]
                d3 = tf[:, 3] - tf[:, 2]
                d4 = tf[:, 4] - tf[:, 3]
                OPS += k * 4

                d_all = np.column_stack([d1, d2, d3, d4])  # shape (k, 4)
                OPS += k * 4

                # Integer median M over 4 intervals
                d_sorted = np.sort(d_all, axis=1)
                median = (d_sorted[:, 1] + d_sorted[:, 2]) // 2
                OPS += k * 8

                # Count intervals within +/-1 of integer median
                diffs = np.abs(d_all - median[:, None])
                agrees = (diffs <= 1).sum(axis=1)
                OPS += k * 12

                majority_ok = agrees >= 3
                latest_ok = np.abs(d4 - median) <= 1
                bound_ok = (median >= 1) & (median <= self.max_half)
                OPS += k * 5

                # Alternating two-state hash check: H0 == H2 == H4 and H1 == H3 and H4 != H3
                h0, h1, h2, h3, h4 = th[:, 0], th[:, 1], th[:, 2], th[:, 3], th[:, 4]
                hash_ok = (h4 == h2) & (h2 == h0) & (h3 == h1) & (h4 != h3)
                OPS += k * 4

                flicker_mask = majority_ok & latest_ok & bound_ok & hash_ok
                OPS += k * 3

                if flicker_mask.any():
                    idx_flicker = idx_has_5[flicker_mask]
                    states[idx_flicker] = C_FLICKER
                    half_periods[idx_flicker] = median[flicker_mask]
                    OPS += idx_flicker.size * 2

        self.prev_hash[:] = h
        OPS += n
        return states, half_periods
