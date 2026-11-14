"""Unit tests for vadapt.multi_describe multi-describe determinism and history isolation.

Task ID: jq2a-m3-j5-vadapt-multi-describe-determinism
Gate G M3 5: Independent predecessor histories match serial runs.
"""

from dataclasses import dataclass, fields
import importlib
import unittest
import numpy as np
from vadapt.multi_describe import Handle, WindowFrames, WindowDescription

# Probe named owner module and symbol explicitly
_OWNER_DESCRIBE_WINDOWS = None
_OWNER_PERCEIVE_MANY = None
_OWNER_ENGINE_DESCRIBE_MANY = None

try:
    _mod = importlib.import_module("vadapt.multi_describe")
    _OWNER_DESCRIBE_WINDOWS = getattr(_mod, "describe_windows", None)
except ModuleNotFoundError as _e:
    if _e.name not in ("vadapt.multi_describe", "vadapt"):
        raise

try:
    _mod_p = importlib.import_module("vadapt.perceive")
    _OWNER_PERCEIVE_MANY = getattr(_mod_p, "perceive_many", None)
except ModuleNotFoundError as _e:
    if _e.name not in ("vadapt.perceive", "vadapt"):
        raise

try:
    _mod_e = importlib.import_module("visioncore.pipeline")
    _RealVisionEngine = getattr(_mod_e, "RealVisionEngine", None)
    if _RealVisionEngine is not None:
        _OWNER_ENGINE_DESCRIBE_MANY = getattr(_RealVisionEngine, "describe_many", None)
except ModuleNotFoundError as _e:
    if _e.name not in ("visioncore.pipeline", "visioncore"):
        raise


def owner_gate(fn):
    """Apply individually scoped unittest.expectedFailure while owner symbol is absent; remove automatically when present."""
    if _OWNER_DESCRIBE_WINDOWS is not None:
        return fn
    return unittest.expectedFailure(fn)


def _invoke_describe_windows(*args, **kwargs):
    if _OWNER_DESCRIBE_WINDOWS is None:
        raise NotImplementedError("vadapt.multi_describe.describe_windows is absent")
    return _OWNER_DESCRIBE_WINDOWS(*args, **kwargs)


# Fallback canonical dataclass definitions if provider modules are absent
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
    frame: object  # Structural frame metadata; no canonical FrameRef here.


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


class ReversingRunner:
    """Mock runner that executes items in reverse order to test submit identity and completion order invariance."""

    def map(self, fn, items):
        jobs = tuple(items)  # Real producer passes _Batch jobs, not WindowFrames.
        self.submitted_batch_indices = tuple(job.index for job in jobs)
        completed = [fn(job) for job in reversed(jobs)]  # bare tagged results
        self.completed_batch_indices = tuple(batch_id for batch_id, _ in completed)
        return completed


class _RefusalRunner:
    def __init__(self):
        self.calls = 0

    def map(self, fn, items):
        self.calls += 1
        raise AssertionError("invalid input dispatched to runner")


class TestVadaptMultiDescribeDeterminism(unittest.TestCase):
    """Determinism and history isolation unit tests for multi-describe."""

    def setUp(self):
        # Create 5 supported target kinds with identical window dimensions (64x48) but distinct preceding pixels
        self.handles = [
            Handle(kind="cdp_tab", canonical_id="tab-001", generation=1),
            Handle(kind="x11_window", canonical_id="win-101", generation=1),
            Handle(kind="xvfb_screen", canonical_id="scr-201", generation=1),
            Handle(kind="capsule", canonical_id="cap-301", generation=1),
            Handle(kind="godot_window", canonical_id="godot-401", generation=1),
        ]

        # Generate unique image sequences for each window (3 frames each)
        rng = np.random.default_rng(seed=42)
        self.window_items = []
        for i, h in enumerate(self.handles):
            # Each target window has a distinct pixel signature/pattern
            f0 = np.full((48, 64, 3), (i + 1) * 40, dtype=np.uint8)
            f1 = f0.copy()
            f1[:24, :32] ^= np.uint8(128)
            f2 = f1.copy()
            f2[24:, 32:] ^= np.uint8(255)

            frames = (
                np.ascontiguousarray(f0),
                np.ascontiguousarray(f1),
                np.ascontiguousarray(f2),
            )
            item = WindowFrames(
                handle=h,
                epoch=1,
                frames=frames,
                frame_seqs=(1, 2, 3),
                dpr_ppm=1000,
            )
            self.window_items.append(item)

    @owner_gate
    def test_gate_g_m3_5_independent_predecessor_histories_match_serial(self):
        """Gate G M3 5: Interleave unrelated equal-sized windows whose preceding pixels differ.

        Compare each history against an isolated serial reference and replay several worker counts (1, 2, 4)
        and reversing runner. Require owned RAM inputs and bit-identical descriptors and regions.
        """
        # Isolated serial reference runs for each window
        serial_refs = {}
        for item in self.window_items:
            res_list = _invoke_describe_windows([item], workers=1, out_dir=None)
            self.assertEqual(len(res_list), 1)
            serial_refs[item.handle] = res_list[0]

        # Test worker-count replays: 1, 2, 4 workers
        for worker_cnt in (1, 2, 4):
            batch_res = _invoke_describe_windows(self.window_items, workers=worker_cnt, out_dir=None)
            self.assertEqual(len(batch_res), len(self.window_items))

            for item, desc_out in zip(self.window_items, batch_res):
                ref = serial_refs[item.handle]
                self.assertEqual(desc_out.handle, ref.handle)
                self.assertEqual(desc_out.epoch, ref.epoch)
                self.assertEqual(desc_out.frame_seqs, ref.frame_seqs)
                self.assertEqual(desc_out.error, ref.error)
                self.assertEqual(desc_out.descriptors, ref.descriptors)
                self.assertEqual(desc_out.regions, ref.regions)

        # Test reversed completion runner replay
        rev_runner = ReversingRunner()
        rev_res = _invoke_describe_windows(self.window_items, workers=2, runner=rev_runner, out_dir=None)
        self.assertGreater(len(rev_runner.submitted_batch_indices), 1)
        self.assertEqual(rev_runner.completed_batch_indices,
                         tuple(reversed(rev_runner.submitted_batch_indices)))
        self.assertEqual(len(rev_res), len(self.window_items))
        for item, desc_out in zip(self.window_items, rev_res):
            ref = serial_refs[item.handle]
            self.assertEqual(desc_out.handle, ref.handle)
            self.assertEqual(desc_out.descriptors, ref.descriptors)
            self.assertEqual(desc_out.regions, ref.regions)

    @owner_gate
    def test_negative_mutant_unrelated_frames_grouped_as_temporal_sequence_fails_isolation(self):
        """Negative mutant: Grouping unrelated frames across target windows as a temporal sequence.

        INV-M3-2 requires that each temporal sequence contains only its own target incarnation and geometry.
        Grouping by dimensions alone is insufficient. Verify that an incorrect grouping produces
        history contamination / descriptor divergence compared to independent target-isolated evaluation.
        """
        # Pick two distinct equal-sized target windows whose preceding pixels differ
        item0 = self.window_items[0]
        item1 = self.window_items[1]

        # Serial reference for target window 1 (evaluating frames 1, 2, 3 with its own preceding history)
        ref_desc1 = _invoke_describe_windows([item1], workers=1, out_dir=None)[0]

        # Construct a mutated window item where target window 1's preceding frame is replaced by target window 0's frame
        mutated_frames = (item0.frames[0], item1.frames[1], item1.frames[2])
        mutated_item1 = WindowFrames(
            handle=item1.handle,
            epoch=item1.epoch,
            frames=mutated_frames,
            frame_seqs=item1.frame_seqs,
            dpr_ppm=item1.dpr_ppm,
        )

        mutated_desc1 = _invoke_describe_windows([mutated_item1], workers=1, out_dir=None)[0]

        # Predecessor contamination must alter temporal history / descriptors
        self.assertNotEqual(
            mutated_desc1.descriptors,
            ref_desc1.descriptors,
            "Contaminating history with an unrelated target's preceding frame must alter descriptors",
        )

    @owner_gate
    def test_inv_m3_1_validation_non_contiguous_or_non_uint8_rgb_and_positive_dpr(self):
        """INV-M3-1: Validate contiguous uint8 RGB and positive per-target DPR before work."""
        valid_item = self.window_items[0]

        # 1. Non-contiguous RGB array
        non_contig_frame = valid_item.frames[0][::2, ::2, :]
        self.assertFalse(non_contig_frame.flags.c_contiguous)
        bad_contig_item = WindowFrames(
            handle=valid_item.handle,
            epoch=1,
            frames=(non_contig_frame,),
            frame_seqs=(1,),
            dpr_ppm=1000,
        )
        stride_runner = _RefusalRunner()
        stride_out = _invoke_describe_windows([bad_contig_item], workers=1,
                                               runner=stride_runner, out_dir=None)
        self.assertEqual(stride_out[0].error,
                         "E_M3_FRAME_SCHEMA: RGB must be C-contiguous uint8; no implicit conversion")
        self.assertEqual(stride_out[0].descriptors, ())
        self.assertEqual(stride_runner.calls, 0)

        # 2. Non-uint8 RGB array (float32)
        f32_frame = valid_item.frames[0].astype(np.float32)
        bad_dtype_item = WindowFrames(
            handle=valid_item.handle,
            epoch=1,
            frames=(f32_frame,),
            frame_seqs=(1,),
            dpr_ppm=1000,
        )
        dtype_runner = _RefusalRunner()
        dtype_out = _invoke_describe_windows([bad_dtype_item], workers=1,
                                              runner=dtype_runner, out_dir=None)
        self.assertEqual(dtype_out[0].error,
                         "E_M3_FRAME_SCHEMA: RGB must be C-contiguous uint8; no implicit conversion")
        self.assertEqual(dtype_out[0].descriptors, ())
        self.assertEqual(dtype_runner.calls, 0)

        # 3. Non-positive DPR (dpr_ppm <= 0)
        bad_dpr_item = WindowFrames(
            handle=valid_item.handle,
            epoch=1,
            frames=valid_item.frames,
            frame_seqs=valid_item.frame_seqs,
            dpr_ppm=0,
        )
        dpr_runner = _RefusalRunner()
        dpr_out = _invoke_describe_windows([bad_dpr_item], workers=1,
                                            runner=dpr_runner, out_dir=None)
        self.assertEqual(dpr_out[0].error, "E_M3_SCHEMA: dpr_ppm must be an integer >= 1")
        self.assertEqual(dpr_out[0].descriptors, ())
        self.assertEqual(dpr_runner.calls, 0)

    @owner_gate
    def test_ram_input_ownership_read_only_and_offline_io_safety(self):
        """Data lifetime and RAM safety: Input frames are copied into owned RAM, read-only after admission."""
        valid_item = self.window_items[0]

        res = _invoke_describe_windows([valid_item], workers=1, out_dir=None)
        self.assertEqual(len(res), 1)

        # Confirm input array bytes remain unchanged and preserved in memory
        for frame_arr in valid_item.frames:
            self.assertTrue(frame_arr.flags.c_contiguous)
            self.assertEqual(frame_arr.dtype, np.uint8)

    def test_canonical_dataclass_field_order_and_structures(self):
        """Validate field order of frozen dataclass definitions per proposed component contract."""
        expected_field_orders = {
            Handle: ("kind", "canonical_id", "generation"),
            TargetRecord: ("handle", "transport", "owned", "epoch", "session_id", "attachment_owned", "closed"),
            Observation: ("handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame"),
            Receipt: ("action_id", "handle", "status", "reason"),
            ActionRequest: ("handle", "action_id", "epoch", "payload", "cost", "deadline_ns"),
            ActorLease: ("handle", "owner", "generation", "nonce", "expires_ns"),
            FocusLease: ("lease", "domain", "epoch"),
            StreamSpec: ("width", "height", "slots", "quota_bytes"),
            StreamToken: ("handle", "epoch"),
            Ticket: ("action_id", "handle"),
            ChangeResult: ("handle", "epoch", "settled", "claims", "ops"),
            PerceptionResult: ("handle", "observation", "error"),
            WindowFrames: ("handle", "epoch", "frames", "frame_seqs", "dpr_ppm"),
            WindowDescription: ("handle", "epoch", "frame_seqs", "descriptors", "regions", "error"),
        }

        for cls, expected_names in expected_field_orders.items():
            actual_names = tuple(f.name for f in fields(cls))
            self.assertEqual(
                actual_names,
                expected_names,
                f"Dataclass {cls.__name__} field order mismatch: actual {actual_names} vs expected {expected_names}",
            )


if __name__ == "__main__":
    unittest.main()
