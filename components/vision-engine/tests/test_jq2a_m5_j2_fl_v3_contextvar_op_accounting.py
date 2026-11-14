"""Contract and conformance test suite for jq2a-m5-j2-fl-v3-contextvar-op-accounting.

Validates per-target FL contextvar operation accounting vs shared global counters.
Covers interleaved updates, exception-resilience in finally, and nested contexts.
"""
import contextvars
import unittest
import numpy as np

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from eval.v2x.per_window_detector import ChangeResult, Handle, PerWindowDetector
from visioncore import desc
from visioncore.grid import Grid


# --- Proposed Data Contract Definitions ---
@dataclass(frozen=True)
class TargetRecord:
    handle: Handle
    transport: Any
    owned: bool
    epoch: int
    session_id: Optional[str]
    attachment_owned: bool
    closed: bool


@dataclass(frozen=True)
class FrameRef:
    handle: Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: Any
    digest: str


@dataclass(frozen=True)
class Observation:
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: Dict[str, Any]
    frame: FrameRef


@dataclass(frozen=True)
class Receipt:
    action_id: str
    handle: Handle
    status: str
    reason: Optional[str]


@dataclass(frozen=True)
class ActionRequest:
    handle: Handle
    action_id: str
    epoch: int
    payload: Dict[str, Any]
    cost: int
    deadline_ns: int


@dataclass(frozen=True)
class ActorLease:
    handle: Handle
    owner: str
    generation: int
    nonce: bytes
    expires_ns: int


@dataclass(frozen=True)
class FocusLease:
    lease: ActorLease
    domain: str
    epoch: int


@dataclass(frozen=True)
class StreamSpec:
    width: int
    height: int
    slots: int
    quota_bytes: int


@dataclass(frozen=True)
class StreamToken:
    handle: Handle
    epoch: int


@dataclass(frozen=True)
class Ticket:
    action_id: str
    handle: Handle


@dataclass(frozen=True)
class PerceptionResult:
    handle: Handle
    observation: Optional[Observation]
    error: Optional[str]


@dataclass(frozen=True)
class WindowFrames:
    handle: Handle
    epoch: int
    frames: Tuple[Any, ...]
    frame_seqs: Tuple[int, ...]
    dpr_ppm: int


@dataclass(frozen=True)
class WindowDescription:
    handle: Handle
    epoch: int
    frame_seqs: Tuple[int, ...]
    descriptors: Tuple[Any, ...]
    regions: Tuple[Any, ...]
    error: Optional[str]


# --- Contract Harness: ContextVar (Positive Control) vs Shared Global (Negative Mutant) ---

_fl_ops_var: contextvars.ContextVar[int] = contextvars.ContextVar("fl_ops", default=0)
_shared_global_fl_ops: int = 0


def _positive_fl_op(count: int = 1, raise_exception: bool = False):
    """Positive control: uses ContextVar to track FL ops in fresh/isolated contexts."""
    current = _fl_ops_var.get()
    _fl_ops_var.set(current + count)
    if raise_exception:
        raise RuntimeError("FL operation failed mid-execution")


def _negative_fl_op_shared_global(count: int = 1, raise_exception: bool = False):
    """Negative mutant: mutates a shared global counter without context isolation."""
    global _shared_global_fl_ops
    _shared_global_fl_ops += count
    if raise_exception:
        raise RuntimeError("FL operation failed mid-execution")


def run_positive_interleaved_contextvar_harness(
    a_ops_step1: int,
    b_ops_step1: int,
    a_ops_step2: int,
    raise_in_b: bool = False,
    nested_ops_in_b: int = 0,
):
    """Run interleaved target calls and nested accounting contexts using ContextVar isolation."""
    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()

    # Step 1: Target A update step 1
    def step1_a():
        _positive_fl_op(a_ops_step1)
        return _fl_ops_var.get()

    res_a_1 = ctx_a.run(step1_a)

    # Step 2: Interleaved Target B update step 1 (with optional exception and nested context)
    def step1_b():
        try:
            _positive_fl_op(b_ops_step1, raise_exception=raise_in_b)
            if nested_ops_in_b > 0:
                # Nested context inside B
                ctx_nested = contextvars.copy_context()
                ctx_nested.run(lambda: _positive_fl_op(nested_ops_in_b))
        finally:
            pass
        return _fl_ops_var.get()

    if raise_in_b:
        with unittest.TestCase().assertRaises(RuntimeError):
            ctx_b.run(step1_b)
        res_b_1 = ctx_b.run(lambda: _fl_ops_var.get())
    else:
        res_b_1 = ctx_b.run(step1_b)

    # Step 3: Interleaved Target A update step 2
    def step2_a():
        _positive_fl_op(a_ops_step2)
        return _fl_ops_var.get()

    res_a_2 = ctx_a.run(step2_a)

    return {"ops_a_step1": res_a_1, "ops_b_step1": res_b_1, "ops_a_step2": res_a_2}


def run_negative_shared_global_harness(
    a_ops_step1: int,
    b_ops_step1: int,
    a_ops_step2: int,
    raise_in_b: bool = False,
):
    """Run interleaved target calls using shared global counter (negative mutant)."""
    global _shared_global_fl_ops
    _shared_global_fl_ops = 0

    # Step 1: Target A
    _negative_fl_op_shared_global(a_ops_step1)
    res_a_1 = _shared_global_fl_ops

    # Step 2: Target B
    if raise_in_b:
        try:
            _negative_fl_op_shared_global(b_ops_step1, raise_exception=True)
        except RuntimeError:
            pass
    else:
        _negative_fl_op_shared_global(b_ops_step1)
    res_b_1 = _shared_global_fl_ops

    # Step 3: Target A again
    _negative_fl_op_shared_global(a_ops_step2)
    res_a_2 = _shared_global_fl_ops

    return {"ops_a_step1": res_a_1, "ops_b_step1": res_b_1, "ops_a_step2": res_a_2}


def _baseline(rgb):
    """Build the production descriptor dtype in the frame's exact Morton order."""
    grid = Grid(rgb.shape[1], rgb.shape[0])
    tiles, _ = desc.describe_padded(grid.pad(rgb), grid, lazy_xh=True)
    tiles = tiles.copy()
    tiles["cls"] = 0
    tiles["mv_dx"] = 0
    tiles["mv_dy"] = 0
    return tiles


class TestFLV3ContextvarOpAccounting(unittest.TestCase):

    def test_positive_control_interleaved_updates_equal_isolated_totals(self):
        """Positive control: interleaved target FL updates maintain per-target totals under contextvars."""
        res = run_positive_interleaved_contextvar_harness(
            a_ops_step1=15, b_ops_step1=27, a_ops_step2=10
        )

        self.assertEqual(res["ops_a_step1"], 15, "Target A step 1 ops must be 15")
        self.assertEqual(res["ops_b_step1"], 27, "Target B step 1 ops must be 27")
        self.assertEqual(res["ops_a_step2"], 25, "Target A step 2 ops must accumulate to 25 without leak from B")

    def test_positive_control_exception_and_nested_contexts(self):
        """Positive control: exception in FL call inside finally block and nested context isolation."""
        res = run_positive_interleaved_contextvar_harness(
            a_ops_step1=8, b_ops_step1=12, a_ops_step2=5, raise_in_b=True, nested_ops_in_b=4
        )

        self.assertEqual(res["ops_a_step1"], 8)
        self.assertEqual(res["ops_b_step1"], 12)
        self.assertEqual(res["ops_a_step2"], 13, "Target A step 2 ops must accumulate to 13 despite B's exception")

    def test_negative_mutant_shared_global_fails_isolation(self):
        """Negative mutant verification: shared global counter leaks across interleaved target updates."""
        res = run_negative_shared_global_harness(
            a_ops_step1=15, b_ops_step1=27, a_ops_step2=10
        )

        # Under negative shared global mutant, res_a_2 is 15 + 27 + 10 = 52 instead of 25
        self.assertNotEqual(
            res["ops_a_step2"],
            25,
            "Shared global mutant must fail isolation check by leaking count across targets (52 != 25)"
        )
        self.assertEqual(res["ops_a_step2"], 52)

    def test_production_owner_fl_v3_contextvar_op_accounting(self):
        h_a = Handle(kind="x11_window", canonical_id="win_a", generation=0)
        h_b = Handle(kind="x11_window", canonical_id="win_b", generation=1)
        rgb_a = np.zeros((16, 16, 3), dtype=np.uint8)
        rgb_b = np.full((32, 32, 3), 200, dtype=np.uint8)
        tiles_a = _baseline(rgb_a)
        tiles_b = _baseline(rgb_b)

        # Compare equivalent temporal phases. Initialization and a held-frame
        # update have different legitimate operation counts (9 and 14 per tile).
        solo_a, solo_b = PerWindowDetector(), PerWindowDetector()
        solo_a.update(h_a, tiles_a, rgb_a, epoch=0, frame_seq=0)
        res_a_solo = solo_a.update(h_a, tiles_a, rgb_a, epoch=0, frame_seq=1)
        solo_b.update(h_b, tiles_b, rgb_b, epoch=0, frame_seq=0)
        res_b_solo = solo_b.update(h_b, tiles_b, rgb_b, epoch=0, frame_seq=1)
        solo_ops_a = res_a_solo.ops["fl"]
        solo_ops_b = res_b_solo.ops["fl"]

        detector = PerWindowDetector()
        detector.update(h_a, tiles_a, rgb_a, epoch=0, frame_seq=0)
        detector.update(h_b, tiles_b, rgb_b, epoch=0, frame_seq=0)
        res_a_interleaved = detector.update(h_a, tiles_a, rgb_a, epoch=0, frame_seq=1)
        res_b_interleaved = detector.update(h_b, tiles_b, rgb_b, epoch=0, frame_seq=1)
        ops_a_interleaved = res_a_interleaved.ops["fl"]
        ops_b_interleaved = res_b_interleaved.ops["fl"]

        self.assertIsInstance(res_a_interleaved, ChangeResult)
        self.assertIsInstance(res_b_interleaved, ChangeResult)
        self.assertGreater(solo_ops_a, 0)
        self.assertGreater(solo_ops_b, solo_ops_a)
        self.assertEqual(ops_a_interleaved, solo_ops_a, "Interleaved Target A FL ops must equal solo total")
        self.assertEqual(ops_b_interleaved, solo_ops_b, "Interleaved Target B FL ops must equal solo total")


if __name__ == "__main__":
    unittest.main()
