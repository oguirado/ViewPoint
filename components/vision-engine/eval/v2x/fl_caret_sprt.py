"""Transition-only caret FLICKER claims using a per-tile fixed-point Wald SPRT.

The fl API is FlickerTracker(n_tiles, max_half=64). update(hashes) reads S2
uint64 hashes in Morton order and returns two integer arrays of shape
(n_tiles,). Only state 4 claims a tile, with a positive nominal half period.
Held frames and unsuccessful transitions never claim and return half period 0.
All history is bounded, per-tile RAM state; no frames or pixels are stored.

Hypotheses and integer derivation (log means the natural logarithm)
----------------------------------------------------------------
H1 is a two-state alternation with a stable half period; H0 is unrelated
content changes. A trial occurs whenever a run completes at a hash change.
For A -> B -> A, the arriving A must equal the hash two runs earlier, and
the just-completed B duration must be within +/-1 of the running half period.
Such a trial is a match; every other completed run is a mismatch, including
the first transition, when a return cannot yet be established.

The Bernoulli match probabilities are explicit model assumptions, not fitted
or measured rates: P_MATCH_H1 = 0.99, P_MATCH_H0 = 0.01. With Q10 fixed point,
SCORE_SCALE = 1024, ALPHA = 0.001 and BETA = 0.1:

UPPER_THRESHOLD = ceil(SCORE_SCALE * log((1 - BETA) / ALPHA)) = 6966
LOWER_THRESHOLD = floor(SCORE_SCALE * log(BETA / (1 - ALPHA))) = -2357
MATCH_INCREMENT = round(SCORE_SCALE * log(P_MATCH_H1 / P_MATCH_H0)) = 4705
MISMATCH_INCREMENT = round(SCORE_SCALE * log((1 - P_MATCH_H1) / (1 - P_MATCH_H0))) = -4705

The unrounded boundaries are 6965.652237644094 and -2356.822622884313;
outward rounding preserves the unrounded decision boundaries. Each completed run adds exactly
one integer increment. At score >= 6966 a successful transition claims
FLICKER; at score <= -2357 the score resets to 0 and a fresh period estimate
is seeded. The first transition therefore resets and seeds context without
supporting H1. Two subsequent clean returns first claim at frame 3 * H
(zero-based), i.e. frame 90 for H=30 and frame 45 for H=15.

After an upper decision the stored score is capped at 6966 for continued
monitoring. This continuation rule prevents unbounded confidence: two bad
transitions take 6966 -> 2261 -> -2444 -> reset, and neither bad transition
claims. The initial test uses Wald boundaries; this cap, repeated trials,
adaptive period estimates and correlated hashes do not establish an overall
empirical false-alarm probability of ALPHA or miss probability of BETA.

The running period is a rounded integer mean of eight phase lengths. A reset
seeds those eight slots with the latest complete duration, or the midpoint
of the latest two when they differ by at most 2. This allows 14/16 jitter to
find H=15 and allows recovery from a partially observed initial run. Only
matches subsequently update the mean. A nominal H must lie in 1..max_half;
an observed H+1 duration is allowed even at the upper boundary. A fresh
sequence of max_half+1 durations cannot seed an in-range nominal period.

A held run beyond H+1 (or max_half+1 without a candidate) conservatively
expires the trial. Its invalid duration is retained, saturating at
max_half+2, until the hash changes; a static tail cannot become a new run.
All update arithmetic and persistent likelihood/period state use integers.
Only the standard library and NumPy are required; there is no file I/O.
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

SCORE_SCALE = 1024
ALPHA = 0.001
BETA = 0.1
P_MATCH_H1 = 0.99
P_MATCH_H0 = 0.01
UPPER_THRESHOLD = 6966
LOWER_THRESHOLD = -2357
MATCH_INCREMENT = 4705
MISMATCH_INCREMENT = -4705

_PERIOD_WINDOW = 8
_U64_MASK = (1 << 64) - 1
_MAX_HALF_LIMIT = np.iinfo(np.int64).max // _PERIOD_WINDOW - 2


def _as_u64_hashes(hashes, n_tiles):
    """Validate without modifying the caller's Morton-ordered RAM input."""
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
        if isinstance(value, (bool, np.bool_)) or not isinstance(
            value, (int, np.integer)
        ):
            raise ValueError("each hash must be an integer")
        out[i] = int(value) & _U64_MASK
    return out


class FlickerTracker:
    """Independent fixed-point run evidence for each Morton tile index."""

    UNCHANGED = C_UNCHANGED
    CHANGED = C_CHANGED
    FLICKER = C_FLICKER
    C_UNCHANGED = C_UNCHANGED
    C_CHANGED = C_CHANGED
    C_FLICKER = C_FLICKER

    def __init__(self, n_tiles, max_half=64):
        if (
            isinstance(n_tiles, (bool, np.bool_))
            or not isinstance(n_tiles, (int, np.integer))
            or int(n_tiles) < 1
        ):
            raise ValueError("n_tiles must be a positive integer")
        if (
            isinstance(max_half, (bool, np.bool_))
            or not isinstance(max_half, (int, np.integer))
            or not 1 <= int(max_half) <= _MAX_HALF_LIMIT
        ):
            raise ValueError("max_half must be a positive integer within int64 range")
        self.n_tiles = int(n_tiles)
        self.max_half = int(max_half)
        self.reset()

    def reset(self):
        """Start a fresh scene with no run history or likelihood evidence."""
        n = self.n_tiles
        self.initialized = False
        self.prev_hash = np.zeros(n, dtype=np.uint64)
        self.previous_run_hash = np.zeros(n, dtype=np.uint64)
        self.has_previous_run = np.zeros(n, dtype=bool)
        self.run_length = np.zeros(n, dtype=np.int64)
        self.previous_run_length = np.zeros(n, dtype=np.int64)
        self.scores = np.zeros(n, dtype=np.int64)
        self.half_period = np.zeros(n, dtype=np.int64)
        self._period_runs = np.zeros((_PERIOD_WINDOW, n), dtype=np.int64)
        self._period_sum = np.zeros(n, dtype=np.int64)
        self._period_index = np.zeros(n, dtype=np.int64)
        self._expired = np.zeros(n, dtype=bool)

    def _clear_evidence(self, tiles):
        """Abandon an episode while retaining the observed run boundaries."""
        self.scores[tiles] = 0
        self.half_period[tiles] = 0
        self._period_runs[:, tiles] = 0
        self._period_sum[tiles] = 0
        self._period_index[tiles] = 0

    def _seed_period(self, tiles):
        """Seed a new episode from observed durations, without positive evidence."""
        current = self.run_length[tiles]
        previous = self.previous_run_length[tiles]
        pair = (
            self.has_previous_run[tiles]
            & (previous <= self.max_half + 1)
            & (current <= self.max_half + 1)
            & (np.abs(current - previous) <= 2)
        )
        midpoint = np.minimum(current, previous) + (
            np.abs(current - previous) + 1
        ) // 2
        seed = np.where(pair, midpoint, current)
        seed = np.where((seed >= 1) & (seed <= self.max_half), seed, 0)
        self.half_period[tiles] = seed
        self._period_runs[:, tiles] = seed
        self._period_sum[tiles] = seed * _PERIOD_WINDOW
        self._period_index[tiles] = 0

    def _observe_period(self, tiles):
        """Replace one slot of the integer running mean after a match."""
        positions = self._period_index[tiles]
        old = self._period_runs[positions, tiles]
        new = self.run_length[tiles]
        self._period_sum[tiles] += new - old
        self._period_runs[positions, tiles] = new
        self._period_index[tiles] = (positions + 1) % _PERIOD_WINDOW
        estimate = (self._period_sum[tiles] + _PERIOD_WINDOW // 2) // _PERIOD_WINDOW
        self.half_period[tiles] = np.clip(estimate, 1, self.max_half)

    def update(self, hashes):
        """Read one frame of hashes; return independent int64 claim arrays."""
        h = _as_u64_hashes(hashes, self.n_tiles)
        states = np.full(self.n_tiles, C_UNCHANGED, dtype=np.int64)
        half_periods = np.zeros(self.n_tiles, dtype=np.int64)
        if not self.initialized:
            self.prev_hash[:] = h
            self.run_length[:] = 1
            self.initialized = True
            return states, half_periods

        changed = h != self.prev_hash
        same = ~changed
        if same.any():
            self.run_length[same] = np.minimum(
                self.run_length[same] + 1, self.max_half + 2
            )
            limit = np.where(
                self.half_period > 0, self.half_period + 1, self.max_half + 1
            )
            expired = same & ~self._expired & (self.run_length > limit)
            if expired.any():
                self._clear_evidence(expired)
                self._expired[expired] = True

        if changed.any():
            tiles = np.flatnonzero(changed)
            states[tiles] = C_CHANGED
            match = (
                self.has_previous_run[tiles]
                & (h[tiles] == self.previous_run_hash[tiles])
                & ~self._expired[tiles]
                & (self.half_period[tiles] > 0)
                & (self.run_length[tiles] <= self.max_half + 1)
                & (np.abs(self.run_length[tiles] - self.half_period[tiles]) <= 1)
            )
            scores = self.scores[tiles] + np.where(
                match, MATCH_INCREMENT, MISMATCH_INCREMENT
            )
            reject = scores <= LOWER_THRESHOLD
            claim = match & (scores >= UPPER_THRESHOLD)
            self.scores[tiles] = np.minimum(scores, UPPER_THRESHOLD)

            matched_tiles = tiles[match]
            if matched_tiles.size:
                self._observe_period(matched_tiles)
            rejected_tiles = tiles[reject]
            if rejected_tiles.size:
                self._clear_evidence(rejected_tiles)
                self._seed_period(rejected_tiles)

            claimed_tiles = tiles[claim]
            states[claimed_tiles] = C_FLICKER
            half_periods[claimed_tiles] = self.half_period[claimed_tiles]

            # Preserve the outgoing run as context for the arriving run. The
            # transition frame counts only toward the new run, whose length is 1.
            self.previous_run_hash[tiles] = self.prev_hash[tiles]
            self.previous_run_length[tiles] = self.run_length[tiles]
            self.has_previous_run[tiles] = True
            self.run_length[tiles] = 1
            self._expired[tiles] = False

        self.prev_hash[:] = h
        return states, half_periods
