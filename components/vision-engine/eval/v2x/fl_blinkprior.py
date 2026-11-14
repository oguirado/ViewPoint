"""Caret-blink temporal prior FLICKER tracker variant for V2 claim-only post-pass.

OPS unit: count of element-level array operations (comparisons, arithmetic,
bitwise operations, logical reductions, array assignments, and indexed updates)
performed per update step across n_tiles.
"""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context

import numpy as np

_ops = ContextVar("fl_blinkprior_ops", default=0)


def __getattr__(name):
    if name == "OPS":
        return _ops.get()
    raise AttributeError(name)


def _add_ops(count):
    _ops.set(_ops.get() + int(count))


@contextmanager
def ops_scope():
    """Use a fresh context-local per-call count; restore even on exception."""
    token = _ops.set(0)
    try:
        yield lambda: _ops.get()
    finally:
        _ops.reset(token)


def update_with_ops(tracker, hashes):
    """Return ((states, half_periods), charged_ops) in a copied context."""
    context = copy_context()

    def counted():
        with ops_scope() as read_ops:
            result = tracker.update(hashes)
            total = read_ops()
        return result, total

    return context.run(counted)

C_UNCHANGED, C_JITTER, C_CHANGED, C_MOVED, C_FLICKER, C_FROZEN, C_DRIFT = range(7)
UNCHANGED = C_UNCHANGED
JITTER = C_JITTER
CHANGED = C_CHANGED
MOVED = C_MOVED
FLICKER = C_FLICKER
FROZEN = C_FROZEN
DRIFT = C_DRIFT

_U64_MASK = (1 << 64) - 1


def reset_ops():
    """Reset the current context operation counter to zero."""
    _ops.set(0)


def _as_u64_hashes(hashes, n_tiles):
    """Convert a 1-D sequence of length n_tiles of ints to a uint64 numpy array."""
    if isinstance(hashes, np.ndarray):
        _add_ops(hashes.size)
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
    _add_ops(n_tiles)
    return out


class FlickerTracker:
    """Per-tile deterministic integer automaton with caret-blink temporal prior."""

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
        self.min_half = 7 if self.max_half >= 7 else 1
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
        self.in_flicker = np.zeros(n, dtype=bool)
        self.half_period = np.zeros(n, dtype=np.int64)

    def update(self, hashes):
        """Feed one descriptor hash per tile for current frame.

        Returns (states, half_periods) as int64 numpy arrays of shape (n_tiles,).
        """
        n = self.n_tiles
        h = _as_u64_hashes(hashes, n)

        if not self.initialized:
            self.initialized = True
            self.prev_hash[:] = h
            self.state_a[:] = h
            self.has_b[:] = False
            self.cur_len[:] = 1
            self.n_runs[:] = 0
            self.in_flicker[:] = False
            self.half_period[:] = 0
            _add_ops(n * 8)
            return np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)

        same = h == self.prev_hash
        changed = ~same
        _add_ops(n * 2)

        states = np.full(n, C_UNCHANGED, dtype=np.int64)
        _add_ops(n)

        # Branch 1: Tile hash unchanged (held frame)
        if same.any():
            _add_ops(n)
            self.cur_len[same] += 1
            _add_ops(n)

            # Check if held phase exceeds upper bound of current cycle / max_half
            max_allowed = np.where(self.in_flicker, self.half_period + 1, self.max_half)
            _add_ops(n * 2)
            over_max = same & (self.cur_len > max_allowed)
            _add_ops(n * 2)
            if over_max.any():
                _add_ops(n)
                self.n_runs[over_max] = 0
                self.in_flicker[over_max] = False
                self.half_period[over_max] = 0
                self.has_b[over_max] = False
                _add_ops(n * 4)

        # Branch 2: Tile hash changed (toggle frame)
        if changed.any():
            _add_ops(n)
            states[changed] = C_CHANGED
            _add_ops(n)

            # Check for 3rd state (breaks 2-state cycle)
            third = changed & self.has_b & (h != self.state_a) & (h != self.state_b)
            _add_ops(n * 4)
            if third.any():
                _add_ops(n)
                self.state_a[third] = h[third]
                self.has_b[third] = False
                self.cur_len[third] = 1
                self.n_runs[third] = 0
                self.in_flicker[third] = False
                self.half_period[third] = 0
                _add_ops(n * 6)

            two_state = changed & (~third)
            _add_ops(n * 2)
            if two_state.any():
                _add_ops(n)
                first_b = two_state & (~self.has_b)
                _add_ops(n * 2)
                if first_b.any():
                    _add_ops(n)
                    self.state_b[first_b] = h[first_b]
                    self.has_b[first_b] = True
                    _add_ops(n * 2)

                prev_len = self.cur_len.copy()
                _add_ops(n)

                # Reset if previous run was out of valid caret blink range
                too_long = two_state & (prev_len > self.max_half)
                too_short = two_state & (prev_len < self.min_half)
                invalid_len = too_long | too_short
                _add_ops(n * 5)
                if invalid_len.any():
                    _add_ops(n)
                    self.n_runs[invalid_len] = 0
                    self.in_flicker[invalid_len] = False
                    self.half_period[invalid_len] = 0
                    self.cur_len[invalid_len] = 1
                    _add_ops(n * 4)

                valid_run = two_state & (~invalid_len)
                _add_ops(n * 2)
                if valid_run.any():
                    _add_ops(n)
                    nr0 = prev_len
                    nr1 = self.r0.copy()
                    old_n = self.n_runs.copy()
                    _add_ops(n * 2)

                    new_n = np.ones(n, dtype=np.int64)
                    _add_ops(n)

                    # Check symmetry with previous run: abs(nr0 - nr1) <= 2 (allowing H+1 vs H-1 jitter)
                    diff_ok = np.abs(nr0 - nr1) <= 2
                    _add_ops(n * 2)
                    ok2 = (old_n >= 1) & diff_ok
                    _add_ops(n * 2)
                    new_n = np.where(ok2, old_n + 1, new_n)
                    _add_ops(n * 2)

                    self.r0[valid_run] = nr0[valid_run]
                    self.r1[valid_run] = nr1[valid_run]
                    self.n_runs[valid_run] = new_n[valid_run]
                    self.cur_len[valid_run] = 1
                    _add_ops(n * 4)

                    # Estimate half-period as average/rounded of r0 and r1
                    hp_est = (self.r0 + self.r1 + 1) // 2
                    _add_ops(n * 3)

                    # Declare FLICKER on 3rd toggle (new_n >= 3)
                    # which provides 2 complete runs (r0 and r1) as evidence
                    flick = valid_run & (new_n >= 3)
                    _add_ops(n * 2)
                    if flick.any():
                        _add_ops(n)
                        self.in_flicker[flick] = True
                        self.half_period[flick] = hp_est[flick]
                        _add_ops(n * 2)

                    not_flick = valid_run & (new_n < 3)
                    _add_ops(n * 2)
                    if not_flick.any():
                        _add_ops(n)
                        self.in_flicker[not_flick] = False
                        self.half_period[not_flick] = 0
                        _add_ops(n * 2)

        # State 4 (FLICKER) is emitted ONLY on frames where the tile hash changed AND in_flicker is True
        emit_flicker = changed & self.in_flicker
        _add_ops(n * 2)
        states[emit_flicker] = C_FLICKER
        _add_ops(n)

        self.prev_hash[:] = h
        _add_ops(n)

        return states, self.half_period.copy()
