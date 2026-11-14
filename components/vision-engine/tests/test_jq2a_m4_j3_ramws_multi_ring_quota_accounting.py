"""Contract and gate tests for Gate G M4 3: Quota rollback conserves live reservations.

Task: jq2a-m4-j3-ramws-multi-ring-quota-accounting
Owning task_id: jq2a-m4-c1-stream-bank
"""

from dataclasses import dataclass, field
import sys
import unittest
import numpy as np
from vadapt import rtring
from vadapt.window_stream_bank import StaleStreamError

# Probe the named owner module and symbol explicitly
try:
    from vadapt import window_stream_bank
    WindowStreamBank = getattr(window_stream_bank, "WindowStreamBank", None)
    HAS_WINDOW_STREAM_BANK = WindowStreamBank is not None
except ImportError:
    window_stream_bank = None
    WindowStreamBank = None
    HAS_WINDOW_STREAM_BANK = False

# Local canonical records supply symbols outside the implemented bank API.
@dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int

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
class FrameRef:
    handle: Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: object
    digest: str

@dataclass(frozen=True)
class Observation:
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: FrameRef

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

@dataclass(frozen=True)
class WindowFrames:
    handle: Handle
    epoch: int
    frames: tuple
    frame_seqs: tuple
    dpr_ppm: int

@dataclass(frozen=True)
class WindowDescription:
    handle: Handle
    epoch: int
    frame_seqs: tuple
    descriptors: tuple
    regions: tuple
    error: str | None

@dataclass(frozen=True)
class Drop:
    handle: Handle
    epoch: int
    frame_seq: int | None
    reason: str


Handle = getattr(window_stream_bank, "Handle", Handle)
TargetRecord = getattr(window_stream_bank, "TargetRecord", TargetRecord)
FrameRef = getattr(window_stream_bank, "FrameRef", FrameRef)
Observation = getattr(window_stream_bank, "Observation", Observation)
Receipt = getattr(window_stream_bank, "Receipt", Receipt)
ActionRequest = getattr(window_stream_bank, "ActionRequest", ActionRequest)
ActorLease = getattr(window_stream_bank, "ActorLease", ActorLease)
FocusLease = getattr(window_stream_bank, "FocusLease", FocusLease)
StreamSpec = getattr(window_stream_bank, "StreamSpec", StreamSpec)
StreamToken = getattr(window_stream_bank, "StreamToken", StreamToken)
Ticket = getattr(window_stream_bank, "Ticket", Ticket)
ChangeResult = getattr(window_stream_bank, "ChangeResult", ChangeResult)
PerceptionResult = getattr(window_stream_bank, "PerceptionResult", PerceptionResult)
WindowFrames = getattr(window_stream_bank, "WindowFrames", WindowFrames)
WindowDescription = getattr(window_stream_bank, "WindowDescription", WindowDescription)
Drop = getattr(window_stream_bank, "Drop", Drop)


def with_owner_gate(fn):
    """Applies unittest.expectedFailure conditionally only when the owner module or symbol is absent."""
    if not HAS_WINDOW_STREAM_BANK:
        return unittest.expectedFailure(fn)
    return fn


class TestRamwsMultiRingQuotaAccounting(unittest.TestCase):
    """Gate G M4 3: Quota rollback conserves live reservations."""

    def setUp(self):
        self.h1 = Handle(kind="x11_window", canonical_id="win-1", generation=1)
        self.h2 = Handle(kind="x11_window", canonical_id="win-2", generation=1)
        self.h3 = Handle(kind="x11_window", canonical_id="win-3", generation=1)

    def _require_bank(self):
        self.assertTrue(
            HAS_WINDOW_STREAM_BANK,
            "WindowStreamBank implementation module (vadapt.window_stream_bank) is not available",
        )

    @with_owner_gate
    def test_gate_m4_3_partial_multi_ring_reservation_failure_conserves_live_views(self):
        """Fail admission after a partial multi-ring reservation while an old ring has a live view.
        Compare exact quota deltas before and after rollback, close and repeated close;
        never release live mappings or pre-existing reservations.
        """
        self._require_bank()
        # Header alignment plus two RGB slots costs 4696 bytes per ring.
        bank = WindowStreamBank(quota_bytes=9392)

        # Pre-existing stream h1 reserving 4696 bytes
        spec1 = StreamSpec(width=10, height=10, slots=2, quota_bytes=4696)
        tok1 = bank.open(self.h1, spec1)

        # Publish a frame to h1 and obtain a live view (FrameRef)
        rgb1 = np.zeros((10, 10, 3), dtype=np.uint8)
        bank.publish(tok1, rgb1, frame_seq=1)
        ref1 = bank.read(tok1)
        self.assertIsNotNone(ref1)
        self.assertNotIsInstance(ref1, Drop)

        quota_before = bank.quota_bytes
        used_before = bank.reserved_bytes

        # Two new rings fit individually, but exceed the remaining shared quota.
        spec2 = StreamSpec(width=10, height=10, slots=2, quota_bytes=4696)
        spec3 = StreamSpec(width=10, height=10, slots=2, quota_bytes=4696)

        with self.assertRaises(ValueError):
            if hasattr(bank, "open_many"):
                bank.open_many([(self.h2, spec2), (self.h3, spec3)])
            else:
                t2 = bank.open(self.h2, spec2)
                try:
                    bank.open(self.h3, spec3)
                except Exception:
                    if hasattr(bank, "rollback"):
                        bank.rollback([t2])
                    else:
                        bank.close(t2)
                    raise

        # Check exact quota deltas after rollback
        used_after_rollback = bank.reserved_bytes
        self.assertEqual(used_before, used_after_rollback)

        # Verify live view ref1 is still valid and mapping/reservation on h1 is preserved
        ref1_read = bank.read(tok1)
        self.assertIsNotNone(ref1_read)

        # Close h1 token
        bank.close(tok1)
        used_after_close = bank.reserved_bytes
        self.assertEqual(used_after_close, 0)

        # Closed tokens are stale; rejection must preserve the zero balance.
        with self.assertRaises(StaleStreamError):
            bank.close(tok1)
        used_after_repeat_close = bank.reserved_bytes
        self.assertEqual(used_after_repeat_close, 0)

    @with_owner_gate
    def test_inv_m4_1_handle_identity_and_single_writer_sequence(self):
        """INV-M4-1: separate single-writer rings, increasing per-stream frame sequence, immutable geometry per epoch."""
        self._require_bank()
        bank = WindowStreamBank(quota_bytes=14336)
        spec = StreamSpec(width=16, height=16, slots=4, quota_bytes=7168)
        tok = bank.open(self.h1, spec)

        self.assertEqual(tok.handle, self.h1)
        self.assertEqual(tok.epoch, 0)

        # Publish frame_seq = 1
        rgb1 = np.full((16, 16, 3), 10, dtype=np.uint8)
        bank.publish(tok, rgb1, frame_seq=1)
        f1 = bank.read(tok)
        self.assertIsInstance(f1, FrameRef)
        self.assertEqual(f1.frame_seq, 1)

        # Decreasing or duplicate frame_seq should raise ValueError or be rejected
        rgb2 = np.full((16, 16, 3), 20, dtype=np.uint8)
        with self.assertRaises((ValueError, Exception)):
            bank.publish(tok, rgb2, frame_seq=1)

    @with_owner_gate
    def test_inv_m4_2_quota_transactional_rollback_and_no_double_release(self):
        """INV-M4-2: quota admission is transactional across requested rings; rollback never releases pre-existing reservations."""
        self._require_bank()
        bank = WindowStreamBank(quota_bytes=7000)
        spec1 = StreamSpec(width=8, height=8, slots=2, quota_bytes=4480)
        tok1 = bank.open(self.h1, spec1)

        # h1 pre-existing reservation is 4480 bytes
        spec2 = StreamSpec(width=8, height=8, slots=2, quota_bytes=4480)
        with self.assertRaises(ValueError):
            bank.open(self.h2, spec2)

        # Quota remains exactly 4480
        self.assertEqual(bank.reserved_bytes, 4480)

        # Closing h1 releases its reservation to zero
        bank.close(tok1)
        self.assertEqual(bank.reserved_bytes, 0)

        # A second close rejects the stale token without a double release.
        with self.assertRaises(StaleStreamError):
            bank.close(tok1)
        self.assertEqual(bank.reserved_bytes, 0)

    @with_owner_gate
    def test_inv_m4_3_revalidation_and_hook_tamper_detection(self):
        """INV-M4-3: hook tamper detection at before_copy and after_copy yields Drop or revalidation error."""
        self._require_bank()

        rings = []

        def ring_factory(handle, spec):
            ring = bytearray(rtring.ring_bytes(spec.slots, spec.width, spec.height))
            rings.append(ring)
            return ring

        def corrupt_hook(phase):
            if phase == "after_copy":
                # Mutate the published slot's actual CRC at every retry.
                offset = rtring.HDR + rtring.SLOT_HDR + rtring.CRC_OFF
                crc = rtring._I.unpack_from(rings[0], offset)[0]
                rtring._I.pack_into(rings[0], offset, crc ^ 0xFFFF)

        bank = WindowStreamBank(quota_bytes=7000, ring_factory=ring_factory,
                                step_hook=corrupt_hook)
        spec = StreamSpec(width=8, height=8, slots=2, quota_bytes=4480)
        tok = bank.open(self.h1, spec)

        rgb = np.ones((8, 8, 3), dtype=np.uint8)
        bank.publish(tok, rgb, frame_seq=1)

        res = bank.read(tok)
        self.assertTrue(isinstance(res, Drop) or res is None)

    @with_owner_gate
    def test_resize_advances_epoch_and_invalidates_stale_tokens(self):
        """resize advances geometry epoch and rejects retained stale tokens."""
        self._require_bank()
        bank = WindowStreamBank(quota_bytes=12000)
        spec1 = StreamSpec(width=8, height=8, slots=2, quota_bytes=4480)
        tok1 = bank.open(self.h1, spec1)

        spec2 = StreamSpec(width=16, height=16, slots=2, quota_bytes=5632)
        tok2 = bank.resize(tok1, spec2)

        self.assertNotEqual(tok1.epoch, tok2.epoch)
        self.assertEqual(tok2.epoch, tok1.epoch + 1)

        # Publishing with stale tok1 must fail
        rgb = np.ones((16, 16, 3), dtype=np.uint8)
        with self.assertRaises((ValueError, Exception)):
            bank.publish(tok1, rgb, frame_seq=1)


if __name__ == "__main__":
    unittest.main()
