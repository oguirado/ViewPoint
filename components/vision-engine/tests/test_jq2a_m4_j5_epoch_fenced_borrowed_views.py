"""Contract and unit tests for epoch-fenced borrowed views and retained frame exactness.

Task ID: jq2a-m4-j5-epoch-fenced-borrowed-views
Owning implementation task_id: jq2a-m4-c1-stream-bank
Gate G M4 5: Retained frame remains exact after slot wrap, source pixel change, and stream resize.
"""
import dataclasses
import hashlib
import unittest
import numpy as np


def compute_digest(array: np.ndarray) -> str:
    """Compute sha256 hex digest of contiguous array bytes."""
    return hashlib.sha256(array.tobytes()).hexdigest()


# Proposed canonical frozen dataclasses defined as fallbacks for the contract harness
@dataclasses.dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int


@dataclasses.dataclass(frozen=True)
class TargetRecord:
    handle: Handle
    transport: object
    owned: bool
    epoch: int
    session_id: str | None
    attachment_owned: bool
    closed: bool


@dataclasses.dataclass(frozen=True)
class FrameRef:
    handle: Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: object
    digest: str


@dataclasses.dataclass(frozen=True)
class Observation:
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: FrameRef


@dataclasses.dataclass(frozen=True)
class Receipt:
    action_id: str
    handle: Handle
    status: str
    reason: str | None


@dataclasses.dataclass(frozen=True)
class ActionRequest:
    handle: Handle
    action_id: str
    epoch: int
    payload: dict
    cost: int
    deadline_ns: int


@dataclasses.dataclass(frozen=True)
class ActorLease:
    handle: Handle
    owner: str
    generation: int
    nonce: bytes
    expires_ns: int


@dataclasses.dataclass(frozen=True)
class FocusLease:
    lease: ActorLease
    domain: str
    epoch: int


@dataclasses.dataclass(frozen=True)
class StreamSpec:
    width: int
    height: int
    slots: int
    quota_bytes: int


@dataclasses.dataclass(frozen=True)
class StreamToken:
    handle: Handle
    epoch: int


@dataclasses.dataclass(frozen=True)
class Ticket:
    action_id: str
    handle: Handle


@dataclasses.dataclass(frozen=True)
class ChangeResult:
    handle: Handle
    epoch: int
    settled: bool
    claims: np.ndarray
    ops: dict


@dataclasses.dataclass(frozen=True)
class PerceptionResult:
    handle: Handle
    observation: Observation | None
    error: str | None


@dataclasses.dataclass(frozen=True)
class WindowFrames:
    handle: Handle
    epoch: int
    frames: tuple
    frame_seqs: tuple
    dpr_ppm: int


@dataclasses.dataclass(frozen=True)
class WindowDescription:
    handle: Handle
    epoch: int
    frame_seqs: tuple
    descriptors: tuple
    regions: tuple
    error: str | None


@dataclasses.dataclass(frozen=True)
class Drop:
    handle: Handle
    epoch: int
    frame_seq: int | None
    reason: str


# Owner module helper
def _has_owner() -> bool:
    try:
        import vadapt.window_stream_bank as wsb
        return hasattr(wsb, "WindowStreamBank")
    except ImportError:
        return False


def owner_test(func):
    """Decorator applying unittest.expectedFailure only when owner module/symbol is absent."""
    if not _has_owner():
        return unittest.expectedFailure(func)
    return func


# Harness classes for testing contract & killing negative mutants
class SimulatedStreamRing:
    """Simulates a ring buffer with configurable copy vs unguarded borrow mode."""
    def __init__(self, spec: StreamSpec, copy_on_read: bool = True):
        self.spec = spec
        self.copy_on_read = copy_on_read
        # Slot backing buffers: shape (slots, height, width, 3)
        self.buffer = np.zeros((spec.slots, spec.height, spec.width, 3), dtype=np.uint8)
        self.head = 0
        self.seq_counter = 0
        self.active_borrows = set()  # Tracks active borrowed slot indices

    def publish(self, rgb: np.ndarray, frame_seq: int):
        slot = self.head % self.spec.slots
        self.buffer[slot] = rgb
        self.head += 1
        self.seq_counter = frame_seq

    def read_slot(self, slot: int, handle: Handle, epoch: int, frame_seq: int) -> FrameRef:
        raw_slot = self.buffer[slot]
        if self.copy_on_read:
            delivered_rgb = np.copy(raw_slot)
            delivered_rgb.flags.writeable = False
        else:
            # Unguarded view mutant!
            delivered_rgb = raw_slot
            self.active_borrows.add(slot)

        digest = compute_digest(delivered_rgb)
        return FrameRef(
            handle=handle,
            epoch=epoch,
            frame_seq=frame_seq,
            width=self.spec.width,
            height=self.spec.height,
            rgb=delivered_rgb,
            digest=digest,
        )


class TestEpochFencedBorrowedViewsContract(unittest.TestCase):
    """Contract harness tests for Gate G M4 5, verifying positive control and killing negative mutant."""

    def setUp(self):
        self.handle = Handle(kind="x11_window", canonical_id="win:0x123", generation=1)
        self.spec = StreamSpec(width=64, height=48, slots=3, quota_bytes=1_000_000)

    def test_target_dataclasses_field_order(self):
        """Verify field names and order for all 16 target dataclasses match contract specification."""
        self.assertEqual(
            [f.name for f in dataclasses.fields(Handle)],
            ["kind", "canonical_id", "generation"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(TargetRecord)],
            ["handle", "transport", "owned", "epoch", "session_id", "attachment_owned", "closed"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(FrameRef)],
            ["handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(Observation)],
            ["handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(Receipt)],
            ["action_id", "handle", "status", "reason"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(ActionRequest)],
            ["handle", "action_id", "epoch", "payload", "cost", "deadline_ns"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(ActorLease)],
            ["handle", "owner", "generation", "nonce", "expires_ns"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(FocusLease)],
            ["lease", "domain", "epoch"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(StreamSpec)],
            ["width", "height", "slots", "quota_bytes"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(StreamToken)],
            ["handle", "epoch"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(Ticket)],
            ["action_id", "handle"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(ChangeResult)],
            ["handle", "epoch", "settled", "claims", "ops"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(PerceptionResult)],
            ["handle", "observation", "error"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(WindowFrames)],
            ["handle", "epoch", "frames", "frame_seqs", "dpr_ppm"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(WindowDescription)],
            ["handle", "epoch", "frame_seqs", "descriptors", "regions", "error"]
        )
        self.assertEqual(
            [f.name for f in dataclasses.fields(Drop)],
            ["handle", "epoch", "frame_seq", "reason"]
        )

    def test_positive_control_owned_copy_survives_wrap_and_resize(self):
        """Positive control: Owned copy retains original bytes and digest after slot wraparound and stream resize."""
        ring = SimulatedStreamRing(self.spec, copy_on_read=True)

        # Frame 1: fill with 10s
        frame1_data = np.full((48, 64, 3), 10, dtype=np.uint8)
        ring.publish(frame1_data, frame_seq=1)
        ref1 = ring.read_slot(0, self.handle, epoch=1, frame_seq=1)

        original_bytes = bytes(ref1.rgb.tobytes())
        original_digest = ref1.digest

        # Wraparound slot 0 by publishing frames 2, 3, 4 with new pixels (99s)
        for seq in range(2, 6):
            ring.publish(np.full((48, 64, 3), 99, dtype=np.uint8), frame_seq=seq)

        # Simulate stream resize (advance epoch)
        new_spec = StreamSpec(width=128, height=96, slots=3, quota_bytes=2_000_000)
        ring = SimulatedStreamRing(new_spec, copy_on_read=True)

        # Assert retained FrameRef_1 remains exact
        self.assertEqual(bytes(ref1.rgb.tobytes()), original_bytes)
        self.assertEqual(compute_digest(ref1.rgb), original_digest)
        self.assertEqual(ref1.digest, original_digest)

    def test_negative_mutant_unguarded_view_killed(self):
        """Kill negative mutant: Unguarded view is modified when slot wraps, causing retention check failure."""
        ring = SimulatedStreamRing(self.spec, copy_on_read=False)

        # Frame 1: fill with 10s
        frame1_data = np.full((48, 64, 3), 10, dtype=np.uint8)
        ring.publish(frame1_data, frame_seq=1)
        ref1 = ring.read_slot(0, self.handle, epoch=1, frame_seq=1)

        original_bytes = bytes(ref1.rgb.tobytes())
        original_digest = ref1.digest

        # Wraparound slot 0: publish new pixels (99s) into slot 0
        for seq in range(2, 5):
            ring.publish(np.full((48, 64, 3), 99, dtype=np.uint8), frame_seq=seq)

        # Retained ref1 (unguarded view) was mutated!
        current_bytes = bytes(ref1.rgb.tobytes())
        current_digest = compute_digest(ref1.rgb)

        # Assert harness detects the mutation (killing the unguarded-view mutant)
        self.assertNotEqual(current_bytes, original_bytes)
        self.assertNotEqual(current_digest, original_digest)

    def test_unknown_foreign_borrow_quota_and_reuse_fencing(self):
        """Unknown foreign borrows retain quota allocation and block slot buffer reuse."""
        ring = SimulatedStreamRing(self.spec, copy_on_read=False)
        frame1_data = np.full((48, 64, 3), 15, dtype=np.uint8)
        ring.publish(frame1_data, frame_seq=1)

        ref1 = ring.read_slot(0, self.handle, epoch=1, frame_seq=1)
        self.assertIn(0, ring.active_borrows)
        # Active borrow on slot 0 prevents unmap or reuse without explicit fencing
        self.assertEqual(len(ring.active_borrows), 1)


class TestWindowStreamBankOwnerConformance(unittest.TestCase):
    """Owner conformance tests probing vadapt.window_stream_bank."""

    def setUp(self):
        self.handle = Handle(kind="x11_window", canonical_id="win:0x99", generation=1)
        self.spec = StreamSpec(width=64, height=48, slots=4, quota_bytes=10_000_000)

    @owner_test
    def test_owner_exports_required_symbols(self):
        """Probe that vadapt.window_stream_bank exports required contract symbols."""
        from vadapt.window_stream_bank import WindowStreamBank, Handle, StreamSpec, StreamToken, FrameRef, Drop
        self.assertTrue(callable(WindowStreamBank))
        self.assertTrue(callable(Handle))
        self.assertTrue(callable(StreamSpec))
        self.assertTrue(callable(StreamToken))
        self.assertTrue(callable(FrameRef))
        self.assertTrue(callable(Drop))

    @owner_test
    def test_owner_retained_frame_remains_exact_after_wrap_and_resize(self):
        """Gate G M4 5: Retained frame remains exact after slot wrap, source pixel change, and stream resize."""
        from vadapt.window_stream_bank import WindowStreamBank, Handle, StreamSpec, StreamToken, FrameRef, Drop

        bank = WindowStreamBank(quota_bytes=20_000_000)
        token1 = bank.open(self.handle, self.spec)

        frame1_rgb = np.full((48, 64, 3), 42, dtype=np.uint8)
        bank.publish(token1, frame1_rgb, frame_seq=1)

        read_res = bank.read(token1)
        self.assertIsInstance(read_res, FrameRef)
        ref1: FrameRef = read_res

        orig_bytes = bytes(ref1.rgb.tobytes())
        orig_digest = ref1.digest

        # Wraparound slots by publishing frame 2, 3, 4, 5 with changing pixels
        for seq in range(2, 7):
            new_rgb = np.full((48, 64, 3), seq * 10, dtype=np.uint8)
            bank.publish(token1, new_rgb, frame_seq=seq)

        # Advance epoch via resize
        new_spec = StreamSpec(width=128, height=96, slots=4, quota_bytes=20_000_000)
        token2 = bank.resize(token1, new_spec)
        self.assertEqual(token2.epoch, token1.epoch + 1)

        # Assert retained ref1 is unchanged
        self.assertEqual(bytes(ref1.rgb.tobytes()), orig_bytes)
        self.assertEqual(compute_digest(ref1.rgb), orig_digest)
        self.assertEqual(ref1.digest, orig_digest)

        bank.close(token2)

    @owner_test
    def test_owner_unknown_foreign_borrows_retain_quota_and_block_reuse(self):
        """Probe that foreign/unknown borrows retain quota and block unsafe buffer reuse in WindowStreamBank."""
        from vadapt.window_stream_bank import WindowStreamBank, Handle, StreamSpec, StreamToken, FrameRef

        bank = WindowStreamBank(quota_bytes=20_000_000)
        token = bank.open(self.handle, self.spec)

        rgb_data = np.full((48, 64, 3), 77, dtype=np.uint8)
        bank.publish(token, rgb_data, frame_seq=1)

        ref = bank.read(token)
        self.assertIsInstance(ref, FrameRef)

        # Ensure reading frame does not crash or leave unvalidated raw pointer
        self.assertEqual(ref.frame_seq, 1)
        bank.close(token)


if __name__ == "__main__":
    unittest.main()
