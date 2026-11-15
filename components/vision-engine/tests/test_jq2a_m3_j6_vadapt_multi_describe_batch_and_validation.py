"""Gate G M3 1 / INV M3 1: vadapt.multi_describe validation and batching contract tests.

Owning implementation task_id: jq2a-m3-c1-multi-describe.
Module: vadapt.multi_describe
Symbol: describe_windows
"""

from dataclasses import dataclass
import importlib
from typing import Any, Callable, Sequence
import unittest

import numpy as np
from vadapt.multi_describe import Handle, WindowFrames, WindowDescription


# --- Canonical Frozen Dataclasses (Exact Field Order) ---

@dataclass(frozen=True)
class TargetRecord:
    handle: Handle
    transport: object
    owned: bool
    epoch: int
    session_id: str | None
    attachment_owned: bool
    closed: bool


@dataclass(frozen=True)
class Observation:
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: object  # Structural frame metadata is provided by its producer.


@dataclass(frozen=True)
class Receipt:
    action_id: str
    handle: Handle
    status: str
    reason: str | None


@dataclass(frozen=True)
class ActionRequest:
    handle: Handle
    action_id: str
    epoch: int
    payload: dict
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
class ChangeResult:
    handle: Handle
    epoch: int
    settled: bool
    claims: np.ndarray
    ops: dict


@dataclass(frozen=True)
class PerceptionResult:
    handle: Handle
    observation: Observation | None
    error: str | None


# --- Owner Probing and Decorator ---

def _probe_owner():
    try:
        mod = importlib.import_module("vadapt.multi_describe")
    except ModuleNotFoundError as e:
        if e.name in ("vadapt", "vadapt.multi_describe"):
            return None, False
        raise
    fn = getattr(mod, "describe_windows", None)
    if fn is None:
        return None, False
    return fn, True


OWNER_FN, HAS_OWNER = _probe_owner()


def owner_required(test_func):
    """Individually scoped expectedFailure applied only while owner module/symbol is absent."""
    if not HAS_OWNER:
        return unittest.expectedFailure(test_func)
    return test_func


# --- Fake Runners & Helpers for Testing ---

class ReversedFakeRunner:
    """Fake runner whose map method yields/returns execution in reverse order."""

    def __init__(self):
        self.dispatched_count = 0

    def map(self, fn: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
        jobs = tuple(items)
        self.dispatched_count += len(jobs)
        # Both real _Batch and harness (index, item) jobs return their own tag.
        completed = [fn(job) for job in reversed(jobs)]
        self.completed_batch_indices = tuple(result[0] for result in completed)
        return completed


class SequentialFakeRunner:
    """Standard fake runner executing sequentially."""

    def __init__(self):
        self.dispatched_count = 0

    def map(self, fn: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
        jobs = tuple(items)
        self.dispatched_count += len(jobs)
        return [fn(job) for job in jobs]  # never add an outer index tag


# --- Positive Control & Negative Mutants for Contract Harness ---

def _validate_window_frames(item: WindowFrames) -> None:
    """Shared pre-dispatch validation logic."""
    if item.dpr_ppm <= 0 or isinstance(item.dpr_ppm, bool):
        raise ValueError(f"Invalid non-positive or non-integer dpr_ppm: {item.dpr_ppm}")
    if len(item.frames) != len(item.frame_seqs):
        raise ValueError("frames and frame_seqs must have equal length")
    if len(item.frame_seqs) > 1:
        for a, b in zip(item.frame_seqs[:-1], item.frame_seqs[1:]):
            if b <= a:
                raise ValueError("frame_seqs must be strictly increasing")
    for f in item.frames:
        if not isinstance(f, np.ndarray):
            raise ValueError("frame must be a numpy ndarray")
        if f.dtype != np.uint8:
            raise ValueError(f"frame dtype must be uint8, got {f.dtype}")
        if not f.flags["C_CONTIGUOUS"]:
            raise ValueError("frame array must be contiguous uint8")


def positive_control_describe_windows(
    items: Sequence[WindowFrames],
    *,
    workers: int = 1,
    params: dict | None = None,
    runner: object | None = None,
    out_dir: None = None,
) -> list[WindowDescription]:
    """Positive control implementation fulfilling INV M3 1 contract."""
    # 1. Reject invalid items before runner dispatch
    for item in items:
        _validate_window_frames(item)

    def describe_single_item(item: WindowFrames) -> WindowDescription:
        try:
            descriptors = tuple(f.tobytes() for f in item.frames)
            regions = tuple((0, 0, f.shape[1], f.shape[0]) for f in item.frames)
            return WindowDescription(
                handle=item.handle,
                epoch=item.epoch,
                frame_seqs=item.frame_seqs,
                descriptors=descriptors,
                regions=regions,
                error=None,
            )
        except Exception as err:
            return WindowDescription(
                handle=item.handle,
                epoch=item.epoch,
                frame_seqs=item.frame_seqs,
                descriptors=(),
                regions=(),
                error=str(err),
            )

    if runner is not None and hasattr(runner, "map"):
        # Tag harness jobs explicitly; production _Batch jobs are already tagged.
        mapped = runner.map(lambda pair: (pair[0], describe_single_item(pair[1])),
                            tuple(enumerate(items)))
        if mapped and isinstance(mapped[0], tuple) and len(mapped[0]) == 2 and isinstance(mapped[0][0], int):
            # Re-sort by original submit index to preserve submit identity
            sorted_mapped = sorted(mapped, key=lambda x: x[0])
            return [res for _, res in sorted_mapped]
        else:
            return list(mapped)
    else:
        return [describe_single_item(item) for item in items]


def negative_mutant_completion_order_describe_windows(
    items: Sequence[WindowFrames],
    *,
    workers: int = 1,
    params: dict | None = None,
    runner: object | None = None,
    out_dir: None = None,
) -> list[WindowDescription]:
    """Mutant 1: Returns descriptions in worker completion order instead of submit order."""
    for item in items:
        _validate_window_frames(item)

    def describe_single_item(item: WindowFrames) -> WindowDescription:
        return WindowDescription(
            handle=item.handle,
            epoch=item.epoch,
            frame_seqs=item.frame_seqs,
            descriptors=(),
            regions=(),
            error=None,
        )

    if runner is not None and hasattr(runner, "map"):
        mapped = runner.map(lambda pair: (pair[0], describe_single_item(pair[1])),
                            tuple(enumerate(items)))
        # Flaw: Returns completion order directly without restoring submit index order
        return [res for _, res in mapped] if mapped and isinstance(mapped[0], tuple) else list(mapped)
    return [describe_single_item(item) for item in items]


def negative_mutant_accepts_non_positive_dpr_describe_windows(
    items: Sequence[WindowFrames],
    *,
    workers: int = 1,
    params: dict | None = None,
    runner: object | None = None,
    out_dir: None = None,
) -> list[WindowDescription]:
    """Mutant 2: Flaw - skips pre-dispatch validation and accepts non-positive DPR."""
    # Flaw: Missing pre-dispatch validation of dpr_ppm
    results = []
    for item in items:
        results.append(
            WindowDescription(
                handle=item.handle,
                epoch=item.epoch,
                frame_seqs=item.frame_seqs,
                descriptors=(),
                regions=(),
                error=None,
            )
        )
    return results


# --- Test Suite ---

class TestJq2aM3J6VadaptMultiDescribeBatchAndValidation(unittest.TestCase):
    """Gate G M3 1 / INV M3 1 multi-describe validation and batching tests."""

    def setUp(self):
        self.handle_a = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        self.handle_b = Handle(kind="cdp_tab", canonical_id="tab-2", generation=1)

        f_a1 = np.ones((10, 10, 3), dtype=np.uint8)
        f_a2 = np.ones((10, 10, 3), dtype=np.uint8) * 2
        f_b1 = np.ones((20, 20, 3), dtype=np.uint8) * 3

        self.wf_a = WindowFrames(
            handle=self.handle_a,
            epoch=1,
            frames=(f_a1, f_a2),
            frame_seqs=(100, 101),
            dpr_ppm=1000,
        )
        self.wf_b = WindowFrames(
            handle=self.handle_b,
            epoch=1,
            frames=(f_b1,),
            frame_seqs=(200,),
            dpr_ppm=1000,
        )

    # --- Harness Controls & Mutant Tests (Undecorated, always execute) ---

    def test_harness_positive_control_preserves_submit_identity_and_serial_bytes(self):
        """Positive control with reversed fake runner preserves submit identity and serial bytes."""
        runner = ReversedFakeRunner()
        items = [self.wf_a, self.wf_b]

        results = positive_control_describe_windows(items, runner=runner)

        self.assertEqual(len(results), 2)
        # Submit order preserved despite reverse runner completion
        self.assertEqual(results[0].handle, self.handle_a)
        self.assertEqual(results[1].handle, self.handle_b)

        # Serial bytes check
        expected_bytes_a0 = self.wf_a.frames[0].tobytes()
        self.assertEqual(results[0].descriptors[0], expected_bytes_a0)

    def test_harness_negative_mutant_completion_order_detected(self):
        """Harness detects completion-order mutant when runner executes in reverse."""
        runner = ReversedFakeRunner()
        items = [self.wf_a, self.wf_b]

        results = negative_mutant_completion_order_describe_windows(items, runner=runner)

        # Mutant returns completion order (b then a) instead of submit order (a then b)
        with self.assertRaises(AssertionError):
            self.assertEqual(results[0].handle, self.handle_a)

    def test_harness_negative_mutant_accepts_non_positive_dpr_detected(self):
        """Harness detects mutant that accepts non-positive DPR."""
        invalid_wf = WindowFrames(
            handle=self.handle_a,
            epoch=1,
            frames=(np.ones((5, 5, 3), dtype=np.uint8),),
            frame_seqs=(1,),
            dpr_ppm=0,  # Invalid non-positive DPR
        )

        # Positive control raises ValueError
        with self.assertRaises(ValueError):
            positive_control_describe_windows([invalid_wf])

        # Mutant fails to raise ValueError, so assertion fails
        with self.assertRaises(AssertionError):
            with self.assertRaises(ValueError):
                negative_mutant_accepts_non_positive_dpr_describe_windows([invalid_wf])

    # --- Owner Conformance Tests (Annotated with @owner_required) ---

    @owner_required
    def test_owner_multi_describe_rejects_non_positive_dpr_before_dispatch(self):
        """vadapt.multi_describe.describe_windows rejects dpr_ppm <= 0 before runner dispatch."""
        for invalid_dpr in [0, -100, -1]:
            invalid_wf = WindowFrames(
                handle=self.handle_a,
                epoch=1,
                frames=(np.ones((5, 5, 3), dtype=np.uint8),),
                frame_seqs=(1,),
                dpr_ppm=invalid_dpr,
            )
            runner = SequentialFakeRunner()
            results = OWNER_FN([invalid_wf], runner=runner)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].error,
                             "E_M3_SCHEMA: dpr_ppm must be an integer >= 1")
            self.assertEqual(results[0].descriptors, ())
            self.assertEqual(
                runner.dispatched_count,
                0,
                "Runner was dispatched despite invalid DPR pre-validation failure",
            )

    @owner_required
    def test_owner_multi_describe_rejects_non_contiguous_or_non_uint8_rgb(self):
        """vadapt.multi_describe.describe_windows rejects non-contiguous or non-uint8 RGB arrays."""
        # Non-uint8 frame
        f_float = np.ones((5, 5, 3), dtype=np.float32)
        invalid_wf1 = WindowFrames(
            handle=self.handle_a,
            epoch=1,
            frames=(f_float,),
            frame_seqs=(1,),
            dpr_ppm=1000,
        )
        runner1 = SequentialFakeRunner()
        result1 = OWNER_FN([invalid_wf1], runner=runner1)
        self.assertEqual(result1[0].error,
                         "E_M3_FRAME_SCHEMA: RGB must be C-contiguous uint8; no implicit conversion")
        self.assertEqual(result1[0].descriptors, ())
        self.assertEqual(runner1.dispatched_count, 0)

        # Non-contiguous frame slice
        f_full = np.ones((10, 10, 3), dtype=np.uint8)
        f_strided = f_full[::2, ::2, :]
        invalid_wf2 = WindowFrames(
            handle=self.handle_a,
            epoch=1,
            frames=(f_strided,),
            frame_seqs=(1,),
            dpr_ppm=1000,
        )
        runner2 = SequentialFakeRunner()
        result2 = OWNER_FN([invalid_wf2], runner=runner2)
        self.assertEqual(result2[0].error,
                         "E_M3_FRAME_SCHEMA: RGB must be C-contiguous uint8; no implicit conversion")
        self.assertEqual(result2[0].descriptors, ())
        self.assertEqual(runner2.dispatched_count, 0)

    @owner_required
    def test_owner_multi_describe_reversed_fake_runner_preserves_submit_identity(self):
        """vadapt.multi_describe.describe_windows preserves submit order when runner completes in reverse."""
        runner = ReversedFakeRunner()
        items = [self.wf_a, self.wf_b]

        results = OWNER_FN(items, runner=runner)

        self.assertEqual(runner.completed_batch_indices, (1, 0))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].handle, self.handle_a)
        self.assertEqual(results[1].handle, self.handle_b)
        self.assertEqual(results[0].epoch, self.wf_a.epoch)
        self.assertEqual(results[1].epoch, self.wf_b.epoch)
        self.assertEqual(results[0].frame_seqs, self.wf_a.frame_seqs)
        self.assertEqual(results[1].frame_seqs, self.wf_b.frame_seqs)

    @owner_required
    def test_owner_multi_describe_isolates_per_item_failures_without_retry(self):
        """vadapt.multi_describe.describe_windows isolates per-item errors without retrying completed work."""
        items = [self.wf_a, self.wf_b]

        # Use runner where fn(item_b) raises an exception during worker execution
        class ErrorInjectingRunner:
            def __init__(self):
                self.calls = 0

            def map(self, fn, items_seq):
                for batch in items_seq:
                    self.calls += 1
                    if any(entry.handle.canonical_id == "tab-2"
                           for entry in batch.entries):
                        # Raise after yielding the completed first tagged batch.
                        raise RuntimeError("Injected worker error for tab-2")
                    yield fn(batch)

        runner = ErrorInjectingRunner()
        results = OWNER_FN(items, runner=runner)

        self.assertEqual(len(results), 2)
        self.assertIsNone(results[0].error)
        self.assertIsNotNone(results[1].error)
        self.assertEqual(results[1].error, "E_M3_RUNNER_DIED: RuntimeError")
        self.assertEqual(runner.calls, 2, "Completed work was retried or items skipped unexpectedly")


if __name__ == "__main__":
    unittest.main()
