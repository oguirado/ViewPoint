"""Duty-cycle-aware per-tile FLICKER tracker for the V2 claim-only API.

A cycle has two independent run lengths, one for each uint64 hash. On the
third toggle, the completed A/B/A runs can establish a cycle when the two A
runs differ by at most one frame. B supplies the other phase's initial length;
each later run is compared with the previous run of its own phase. The first
observed run is a candidate reference, so an initially partial run can defer
acquisition until the fourth toggle. A and B need not have equal lengths.

Each tile retains at most eight toggle records in a sparse frame ring. Its
logical capacity is a power of two >= 8 * (2 * max_half), its slot is the input
frame index & (capacity - 1), and residency uses modular frame age. Keeping
eight records does not mean using only eight frame slots: sparse toggles eight
frames apart would all alias in an eight-slot frame-addressed ring. No dense
capacity-sized buffer, image, or luma history is allocated.

A declared tile survives one mismatched completed run and is released on the
second consecutive mismatch. A matching run clears that streak. A third hash
or a run longer than 2 * max_half resets immediately. Held frames always emit
UNCHANGED (0) with half period zero; only toggle frames can emit FLICKER (4).
The reported half period is the rounded mean of the latest two phase lengths
(ties round up), clipped to 1..max_half, including a retained mismatched run.

OPS is a module-level integer count of elementary scalar/array-element work:
comparisons, indexing, arithmetic, assignments, and ring residency operations.
It is deterministic bookkeeping, not a wall-clock or host-performance claim.
"""
from collections import OrderedDict

import numpy as np

C_UNCHANGED = 0
C_CHANGED = 2
C_FLICKER = 4

UNCHANGED = C_UNCHANGED
CHANGED = C_CHANGED
FLICKER = C_FLICKER

_U64_MASK = (1 << 64) - 1
_HISTORY_SIZE = 8

OPS = 0


def reset_ops():
    """Reset the module-level elementary-operation counter."""
    global OPS
    OPS = 0


def _as_u64_hashes(hashes, n_tiles):
    """Read a one-dimensional integer hash sequence without modifying it."""
    global OPS
    OPS += n_tiles
    if isinstance(hashes, np.ndarray):
        if hashes.ndim != 1 or hashes.shape[0] != n_tiles:
            raise ValueError("hashes must have shape (n_tiles,)")
        if hashes.dtype == np.bool_ or hashes.dtype.kind not in ("i", "u"):
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
        OPS += 3
    return out


class FlickerTracker:
    """Track independent two-hash cycles in the input's Morton tile order."""

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
            or int(max_half) < 1
        ):
            raise ValueError("max_half must be a positive integer")
        self.n_tiles = int(n_tiles)
        self.max_half = int(max_half)
        self._run_limit = 2 * self.max_half
        self.capacity = 1 << (_HISTORY_SIZE * self._run_limit - 1).bit_length()
        self._slot_mask = self.capacity - 1
        # Normally a uint64 frame counter; keep its modular horizon larger
        # than the logical ring even for unusually large max_half arguments.
        self._frame_mask = (1 << max(64, self.capacity.bit_length() + 1)) - 1
        self.reset()

    def reset(self):
        """Start a fresh scene, preserving constructor configuration."""
        n = self.n_tiles
        self.initialized = False
        self.frame_idx = 0
        self.prev_hash = np.zeros(n, dtype=np.uint64)
        self.cur_len = np.zeros(n, dtype=np.int64)
        self.in_flicker = np.zeros(n, dtype=bool)
        self.half_period = np.zeros(n, dtype=np.int64)
        self._misses = np.zeros(n, dtype=np.uint8)
        self._stalled = np.zeros(n, dtype=bool)
        # slot -> (frame, outgoing hash, incoming hash, completed run length)
        # Ordered insertion permits eviction of exactly the ninth event.
        self._rings = [OrderedDict() for _ in range(n)]

    def _clear_tile(self, tile):
        """Discard all cycle evidence without inventing a new run boundary."""
        global OPS
        OPS += len(self._rings[tile]) + 4
        self._rings[tile].clear()
        self.in_flicker[tile] = False
        self.half_period[tile] = 0
        self._misses[tile] = 0

    def _resident_ring(self, tile, frame):
        """Expire records by modular frame age, before reusing a frame slot."""
        global OPS
        ring = self._rings[tile]
        while ring:
            oldest = next(iter(ring.values()))
            age = (frame - oldest[0]) & self._frame_mask
            OPS += 4
            if age < self.capacity:
                break
            ring.popitem(last=False)
            OPS += 1
        return ring

    def update(self, hashes):
        """Return fresh int64 (states, half_periods) arrays of shape (n_tiles,)."""
        global OPS
        n = self.n_tiles
        h = _as_u64_hashes(hashes, n)
        states = np.zeros(n, dtype=np.int64)
        half_periods = np.zeros(n, dtype=np.int64)
        OPS += 2 * n

        if not self.initialized:
            self.initialized = True
            self.prev_hash[:] = h
            self.cur_len[:] = 1
            OPS += 2 * n
            return states, half_periods

        self.frame_idx = (self.frame_idx + 1) & self._frame_mask
        frame = self.frame_idx
        same = h == self.prev_hash
        changed = ~same
        held = same & ~self._stalled
        self.cur_len[held] += 1
        overlong = held & (self.cur_len > self._run_limit)
        OPS += 7 * n + int(np.count_nonzero(held))

        # A held run includes the current frame. Freeze its length after a
        # stall so its eventual tail cannot become a fresh short-run sample.
        stalled_indices = np.flatnonzero(overlong)
        OPS += n + int(stalled_indices.size)
        for tile in stalled_indices:
            self._clear_tile(tile)
            self._stalled[tile] = True
            OPS += 1

        changed_indices = np.flatnonzero(changed)
        states[changed] = C_CHANGED
        OPS += n + 2 * int(changed_indices.size)
        for tile in changed_indices:
            length = int(self.cur_len[tile])
            old_hash = int(self.prev_hash[tile])
            new_hash = int(h[tile])
            self.cur_len[tile] = 1
            OPS += 4

            if self._stalled[tile] or length > self._run_limit:
                self._clear_tile(tile)
                self._stalled[tile] = False
                OPS += 3
                # This toggle starts the first trustworthy post-stall run.
                continue

            ring = self._resident_ring(tile, frame)
            recent = reversed(ring.values())
            previous = next(recent, None)
            same_phase = next(recent, None)
            OPS += 4

            # After A -> B, the only allowed next toggle is B -> A. Checking
            # the previous outgoing hash also works after old records expire.
            if previous is not None and new_hash != previous[1]:
                self._clear_tile(tile)
                OPS += 2
                # The third hash starts fresh; the discarded transition is
                # not evidence for a new pair of phases.
                continue

            agrees = (
                same_phase is not None
                and abs(length - same_phase[3]) <= 1
            )
            OPS += 4
            if self.in_flicker[tile]:
                self._misses[tile] = 0 if agrees else int(self._misses[tile]) + 1
                if self._misses[tile] >= 2:
                    self.in_flicker[tile] = False
                OPS += 4
            elif agrees:
                # Two prior genuine toggle records plus this toggle provide
                # the A/B/A suffix. No initial pseudo-toggle is inserted.
                self.in_flicker[tile] = True
                self._misses[tile] = 0
                OPS += 2

            if len(ring) == _HISTORY_SIZE:
                ring.popitem(last=False)
                OPS += 1
            slot = frame & self._slot_mask
            ring[slot] = (frame, old_hash, new_hash, length)
            OPS += 6

            if self.in_flicker[tile]:
                hp = min(self.max_half, max(1, (length + previous[3] + 1) // 2))
                self.half_period[tile] = hp
                states[tile] = C_FLICKER
                half_periods[tile] = hp
                OPS += 8
            else:
                self.half_period[tile] = 0
                OPS += 1

        self.prev_hash[:] = h
        OPS += n
        return states, half_periods
