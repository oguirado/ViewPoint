"""Early, per-tile two-hash return detector for the V2EVAL2 fl API.

An A -> B -> A return qualifies after two completed runs in 8..40 frames
whose lengths differ by at most one frame; the inferred period must not
exceed max_half. Subsequent runs allow +/-1 frame around that fixed period.
The integer half period is their floor average, measured independently for
each tile; there is no toggle cohort or assumed blink cadence. A clean
30/30 blink therefore first qualifies at zero-based frame 60 (update 61).

Only qualifying return transitions emit FLICKER (4). Held frames emit no
claim, but retain the established period until a run exceeds period + 1.
A third hash, an incompatible run, or a stall clears timing evidence.
Third hashes also start a new two-hash epoch at the current frame. Each
scene uses a fresh tracker (or reset()). All state and inputs stay in RAM.
"""

import numpy as np

C_UNCHANGED = 0
C_CHANGED = 2
C_FLICKER = 4
UNCHANGED = C_UNCHANGED
CHANGED = C_CHANGED
FLICKER = C_FLICKER

_U64_MASK = (1 << 64) - 1
_MIN_HALF = 8
_MAX_HALF = 40


def _as_u64_hashes(hashes, n_tiles):
    """Validate a read-only hash vector without retaining the caller's buffer."""
    if isinstance(hashes, np.ndarray):
        if hashes.ndim != 1 or hashes.shape[0] != n_tiles:
            raise ValueError("hashes must have shape (n_tiles,)")
        if hashes.dtype.kind not in ("i", "u"):
            raise ValueError("hashes must have integer dtype")
        return hashes.astype(np.uint64, copy=False)
    seq = list(hashes)
    if len(seq) != n_tiles:
        raise ValueError("hashes length must equal n_tiles")
    out = np.empty(n_tiles, dtype=np.uint64)
    for i, value in enumerate(seq):
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
            raise ValueError("each hash must be an integer")
        out[i] = int(value) & _U64_MASK
    return out


class FlickerTracker:
    """Detect a two-run return, keeping each Morton-ordered tile independent."""

    UNCHANGED = C_UNCHANGED
    CHANGED = C_CHANGED
    FLICKER = C_FLICKER
    C_UNCHANGED = C_UNCHANGED
    C_CHANGED = C_CHANGED
    C_FLICKER = C_FLICKER

    def __init__(self, n_tiles, max_half=64):
        for name, value in (("n_tiles", n_tiles), ("max_half", max_half)):
            if (isinstance(value, (bool, np.bool_))
                    or not isinstance(value, (int, np.integer)) or int(value) < 1):
                raise ValueError(name + " must be a positive integer")
        self.n_tiles = int(n_tiles)
        self.max_half = int(max_half)
        self._limit = min(_MAX_HALF, self.max_half)
        self.reset()

    def reset(self):
        """Forget all hash identities and timing evidence at a scene boundary."""
        n = self.n_tiles
        self._initialized = False
        self._prev = np.zeros(n, dtype=np.uint64)
        self._a = np.zeros(n, dtype=np.uint64)
        self._b = np.zeros(n, dtype=np.uint64)
        self._has_b = np.zeros(n, dtype=bool)
        self._run = np.zeros(n, dtype=np.int64)
        self._previous_run = np.zeros(n, dtype=np.int64)
        self._half = np.zeros(n, dtype=np.int64)
        self._stalled = np.zeros(n, dtype=bool)

    def update(self, hashes):
        """Return independent int64 (states, half_periods) in input Morton order.

        Only state 4 claims a tile, with a positive period <= max_half.
        Every unclaimed tile has period zero; input arrays are never modified.
        """
        h = _as_u64_hashes(hashes, self.n_tiles)
        states = np.zeros(self.n_tiles, dtype=np.int64)
        half_periods = np.zeros(self.n_tiles, dtype=np.int64)
        if not self._initialized:
            self._initialized = True
            self._prev[:] = h
            self._a[:] = h
            self._run[:] = 1
            return states, half_periods

        same = h == self._prev
        changed = ~same
        states[changed] = C_CHANGED

        if same.any():
            # Saturation preserves all invalid-run decisions without overflow
            # on arbitrarily long static scenes.
            self._run[same] = np.minimum(self._run[same] + 1, _MAX_HALF + 2)
            active = self._half > 0
            stalled = same & np.where(
                active, self._run > self._half + 1, self._run > _MAX_HALF
            )
            self._half[stalled] = 0
            self._previous_run[stalled] = 0
            # The stalled run cannot later be reused as fresh timing evidence.
            self._stalled[stalled] = True

        if changed.any():
            third = changed & self._has_b & (h != self._a) & (h != self._b)
            self._a[third] = h[third]
            self._b[third] = 0
            self._has_b[third] = False
            self._previous_run[third] = 0
            self._half[third] = 0

            two_state = changed & ~third
            returning = two_state & self._has_b
            first_b = two_state & ~self._has_b
            self._b[first_b] = h[first_b]
            self._has_b[first_b] = True

            completed = two_state & ~self._stalled
            valid_run = (completed & (self._run >= _MIN_HALF)
                         & (self._run <= _MAX_HALF))
            active = self._half > 0
            continuing = (returning & completed & active
                          & (np.abs(self._run - self._half) <= 1))
            estimate = (self._run + self._previous_run) // 2
            starting = (returning & valid_run & ~active
                        & (self._previous_run >= _MIN_HALF)
                        & (self._previous_run <= _MAX_HALF)
                        & (np.abs(self._run - self._previous_run) <= 1)
                        & (estimate <= self._limit))
            claim = continuing | starting

            self._half[two_state & ~claim] = 0
            self._half[starting] = estimate[starting]
            states[claim] = C_FLICKER
            half_periods[claim] = self._half[claim]

            # An incompatible active run cannot seed an immediate new claim.
            keep_run = valid_run & ~(active & ~continuing)
            self._previous_run[two_state] = np.where(
                keep_run[two_state], self._run[two_state], 0
            )
            self._run[changed] = 1
            self._stalled[changed] = False

        self._prev[:] = h
        return states, half_periods
