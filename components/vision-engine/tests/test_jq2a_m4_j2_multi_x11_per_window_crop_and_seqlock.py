"""Contract unit tests for multi-X11 per-window crop bounds and seqlock stability (Gate G M4 2).

Task: jq2a-m4-j2-multi-x11-per-window-crop-and-seqlock
Owning implementation task_id: jq2a-m4-c2-ram-capture
"""

import hashlib
import struct
import unittest
from unittest import mock
import zlib
from dataclasses import dataclass
from typing import Sequence, Optional, Any
import numpy as np

from vadapt import multi_x11_ramcap, rtring
from vadapt.window_stream_bank import (
    Handle, FrameRef, Drop, StreamSpec, StreamToken, StreamError,
)
from vadapt.multi_x11_ramcap import CropSpec, TrustedScreen, MemoryStreamBank


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
    reason: Optional[str]


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
    observation: Optional[Observation]
    error: Optional[str]


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
    error: Optional[str]


class FakeTrustedSource:
    """Fake bytearray-backed source with distinct per-window RGB signatures for testing."""

    def __init__(self):
        self.windows = {}
        self.calls = []

    def add_window(self, canonical_id: str, width: int, height: int,
                   rgb_bytes: bytearray, frame_seq: int = 0,
                   kind: str = "xvfb_screen", *, generation: int = 1,
                   epoch: int = 1, slot_seq: int = 2, slots: int = 3):
        """Register provenance and one v2 committed slot, entirely in owned RAM."""
        if len(rgb_bytes) != width * height * 3:
            raise ValueError("fixture RGB length must match trusted geometry")
        handle = Handle(kind, canonical_id, generation)
        raw = bytearray(rtring.ring_bytes(slots, width, height))
        struct.pack_into("<8sIIIIQQQ", raw, 0, rtring.MAGIC, rtring.VERSION,
                         slots, width, height, len(rgb_bytes),
                         rtring.data_off(slots), frame_seq + 1)
        slot = frame_seq % slots
        header = rtring.HDR + slot * rtring.SLOT_HDR
        struct.pack_into("<QQ", raw, header, slot_seq, frame_seq)
        start = rtring.data_off(slots) + slot * len(rgb_bytes)
        raw[start:start + len(rgb_bytes)] = rgb_bytes
        struct.pack_into("<I", raw, header + rtring.CRC_OFF,
                         zlib.crc32(rgb_bytes))
        screen = TrustedScreen(handle, epoch, width, height, raw)
        self.windows[handle] = screen
        return screen

    def resolve(self, handle: Handle):
        self.calls.append(handle)
        return self.windows[handle]


class TestJQ2AM4J2MultiX11PerWindowCropAndSeqlock(unittest.TestCase):
    """Contract unit tests for Gate G M4 2: Crop bounds and stable sequence validated."""

    def _require_owner(self):
        """Use the merged owner directly; import failures must remain failures."""
        return multi_x11_ramcap.capture_many

    def _bank(self, quota_bytes=1 << 20):
        bank = MemoryStreamBank(quota_bytes=quota_bytes, slots=2)

        def close():
            bank.close()
            self.assertEqual(bank.reserved_bytes, 0)

        self.addCleanup(close)
        return bank

    def test_valid_crop_byte_exact_per_window_signatures(self):
        """Positive control: valid crops on distinct per-window RGB signatures are byte-exact."""
        capture_many = self._require_owner()

        # Create two windows with distinct RGB patterns
        source = FakeTrustedSource()

        # Window 1: 64x64 solid red (255, 0, 0)
        w1_rgb = bytearray([255, 0, 0] * (64 * 64))
        h1 = Handle(kind="xvfb_screen", canonical_id="win_01", generation=1)
        source.add_window("win_01", width=64, height=64, rgb_bytes=w1_rgb, frame_seq=2, kind="xvfb_screen")

        # Window 2: 32x32 solid green (0, 255, 0)
        w2_rgb = bytearray([0, 255, 0] * (32 * 32))
        h2 = Handle(kind="xvfb_screen", canonical_id="screen_02", generation=1)
        source.add_window("screen_02", width=32, height=32, rgb_bytes=w2_rgb, frame_seq=4, kind="xvfb_screen")

        # Define valid crop specs
        spec1 = CropSpec(handle=h1, epoch=1, x=0, y=0, width=16, height=16, frame_seq=2)
        spec2 = CropSpec(handle=h2, epoch=1, x=8, y=8, width=16, height=16, frame_seq=4)

        results = capture_many([spec1, spec2], source=source, bank=self._bank())

        self.assertEqual(len(results), 2)
        ref1, ref2 = results[0], results[1]

        # Verify ref1 is FrameRef and byte-exact red
        self.assertIsInstance(ref1, FrameRef)
        self.assertEqual(ref1.handle, h1)
        self.assertEqual(ref1.width, 16)
        self.assertEqual(ref1.height, 16)
        expected_ref1_rgb = np.full((16, 16, 3), [255, 0, 0], dtype=np.uint8)
        np.testing.assert_array_equal(ref1.rgb, expected_ref1_rgb)

        # Verify ref2 is FrameRef and byte-exact green
        self.assertIsInstance(ref2, FrameRef)
        self.assertEqual(ref2.handle, h2)
        self.assertEqual(ref2.width, 16)
        self.assertEqual(ref2.height, 16)
        expected_ref2_rgb = np.full((16, 16, 3), [0, 255, 0], dtype=np.uint8)
        np.testing.assert_array_equal(ref2.rgb, expected_ref2_rgb)
        for ref, expected in ((ref1, expected_ref1_rgb), (ref2, expected_ref2_rgb)):
            self.assertEqual(ref.digest, hashlib.sha256(expected.tobytes()).hexdigest())
            owner = ref.rgb
            while isinstance(owner, np.ndarray) and owner.base is not None:
                owner = owner.base
            self.assertIs(type(owner), bytes)
            with self.assertRaises(ValueError):
                ref.rgb.setflags(write=True)
            self.assertFalse(ref.rgb.flags.writeable)

    def test_negative_and_overflowing_crop_bounds_refused(self):
        """Negative mutant: crop bounds outside trusted source geometry are rejected with Drop."""
        capture_many = self._require_owner()

        source = FakeTrustedSource()
        h1 = Handle(kind="xvfb_screen", canonical_id="win_bounds", generation=1)
        source.add_window("win_bounds", width=64, height=48, rgb_bytes=bytearray(64 * 48 * 3), frame_seq=2)

        out_of_bounds_specs = [
            # Negative x
            CropSpec(handle=h1, epoch=1, x=-1, y=0, width=10, height=10, frame_seq=2),
            # Negative y
            CropSpec(handle=h1, epoch=1, x=0, y=-5, width=10, height=10, frame_seq=2),
            # Width overflow (x + width > 64)
            CropSpec(handle=h1, epoch=1, x=60, y=0, width=10, height=10, frame_seq=2),
            # Height overflow (y + height > 48)
            CropSpec(handle=h1, epoch=1, x=0, y=40, width=10, height=10, frame_seq=2),
            # Excess width larger than window
            CropSpec(handle=h1, epoch=1, x=0, y=0, width=100, height=10, frame_seq=2),
        ]

        # The inclusive right/bottom boundary is a valid positive control.
        good = CropSpec(h1, 1, 54, 38, 10, 10, 2)
        positive, = capture_many([good], source=source, bank=self._bank())
        self.assertIsInstance(positive, FrameRef)
        bank = self._bank()
        results = capture_many(out_of_bounds_specs, source=source, bank=bank)
        self.assertEqual(len(results), len(out_of_bounds_specs))

        for res in results:
            self.assertIsInstance(res, Drop)
            self.assertEqual(res.handle, h1)
            self.assertEqual(res.reason, "invalid_crop")
        self.assertEqual(bank.reserved_bytes, 0)

    def test_odd_and_changing_seqlock_sequence_refused(self):
        """Negative mutant: odd sequence or active seqlock write yields Drop rather than torn frame."""
        capture_many = self._require_owner()

        source = FakeTrustedSource()
        h1 = Handle(kind="xvfb_screen", canonical_id="win_seq", generation=1)

        # Render index and seqlock sequence are independent: odd indices are valid.
        screen = source.add_window("win_seq", width=64, height=64,
                                   rgb_bytes=bytearray(64 * 64 * 3), frame_seq=1)

        odd_spec = CropSpec(handle=h1, epoch=1, x=0, y=0, width=16, height=16, frame_seq=1)
        positive, = capture_many([odd_spec], source=source, bank=self._bank())
        self.assertIsInstance(positive, FrameRef)
        header = rtring.HDR + (1 % 3) * rtring.SLOT_HDR
        struct.pack_into("<Q", screen.ring, header, 3)
        bank = self._bank()
        results = capture_many([odd_spec], source=source, bank=bank)

        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Drop)
        self.assertEqual(results[0].handle, h1)
        self.assertEqual(results[0].reason, "torn")
        self.assertEqual(bank.reserved_bytes, 0)

        # Advance only the even seqlock sequence at the real copy boundary.
        # Every retry must revalidate; no public copy-revalidation API exists.
        struct.pack_into("<Q", screen.ring, header, 2)
        original_bytes = bytes
        copies = []

        def copy_then_advance(*args, **kwargs):
            owned = original_bytes(*args, **kwargs)
            seq = struct.unpack_from("<Q", screen.ring, header)[0]
            struct.pack_into("<Q", screen.ring, header, seq + 2)
            copies.append(seq)
            return owned

        with mock.patch.object(multi_x11_ramcap, "bytes", copy_then_advance, create=True):
            changing, = capture_many([odd_spec], source=source, bank=bank)
        self.assertIsInstance(changing, Drop)
        self.assertEqual(changing.handle, h1)
        self.assertEqual(changing.reason, "torn")
        self.assertEqual(len(copies), 8)
        self.assertEqual(bank.reserved_bytes, 0)

    def test_unsupported_source_mode_and_target_kinds_refused(self):
        """Unsupported target kind or source mode rejects discovery without launching X11."""
        capture_many = self._require_owner()

        source = FakeTrustedSource()

        # Unknown kinds are refused by the actual constructor, not a shadow record.
        with self.assertRaises(StreamError):
            Handle(kind="unsupported_directx", canonical_id="win_dx", generation=1)
        # A valid handle kind can still be unsupported by trusted Xvfb capture.
        unsupported_h = Handle(kind="x11_window", canonical_id="win_dx", generation=1)

        spec = CropSpec(handle=unsupported_h, epoch=1, x=0, y=0, width=16, height=16, frame_seq=2)

        results = capture_many([spec], source=source, bank=self._bank())
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Drop)
        self.assertEqual(results[0].handle, unsupported_h)
        self.assertEqual(results[0].reason, "unsupported_mode")
        self.assertEqual(source.calls, [])

    def test_quota_admission_transactional_rollback(self):
        """Quota admission across requested rings is transactional; rollback preserves pre-existing reservations."""
        capture_many = self._require_owner()

        source = FakeTrustedSource()
        h1 = Handle(kind="xvfb_screen", canonical_id="win_quota_1", generation=1)
        h2 = Handle(kind="xvfb_screen", canonical_id="win_quota_2", generation=1)

        source.add_window("win_quota_1", width=64, height=64, rgb_bytes=bytearray(64 * 64 * 3), frame_seq=2)
        source.add_window("win_quota_2", width=64, height=64, rgb_bytes=bytearray(64 * 64 * 3), frame_seq=2)

        # Both crops are in bounds; their combined new reservations exceed quota.
        spec1 = CropSpec(handle=h1, epoch=1, x=0, y=0, width=16, height=16, frame_seq=2)
        spec2 = CropSpec(handle=h2, epoch=1, x=0, y=0, width=16, height=16, frame_seq=2)
        existing = CropSpec(handle=h1, epoch=1, x=32, y=0, width=16, height=16, frame_seq=2)
        unit = rtring.ring_bytes(2, 16, 16)
        bank = self._bank(quota_bytes=2 * unit)
        positive, = capture_many([existing], source=source, bank=bank)
        self.assertIsInstance(positive, FrameRef)
        token = bank.token_for(existing)
        self.assertIsInstance(token, StreamToken)
        self.assertEqual(bank.reserved_bytes, unit)

        results = capture_many([spec1, spec2], source=source, bank=bank)
        self.assertEqual(len(results), 2)
        for spec, result in zip((spec1, spec2), results):
            self.assertIsInstance(result, Drop)
            self.assertEqual(result.handle, spec.handle)
            self.assertEqual(result.reason, "quota")
            self.assertIsNone(bank.token_for(spec))
        self.assertEqual(bank.reserved_bytes, unit)
        self.assertIs(bank.token_for(existing), token)


if __name__ == "__main__":
    unittest.main()
