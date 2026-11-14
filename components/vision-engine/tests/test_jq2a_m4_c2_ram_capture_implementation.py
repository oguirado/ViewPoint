"""Offline M4-C2 source/bank integration tests; bytearray rings, no services.

The fake is a trusted source *registration*, not a replacement capture
implementation. Every assertion uses the public capture_many/MemoryStreamBank
paths and the existing rtring v2 wire layout.
"""

import builtins
from dataclasses import FrozenInstanceError, fields
import hashlib
import os
import socket
import struct
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

import numpy as np

from vadapt import rtring
from vadapt.multi_x11_ramcap import (
    CropSpec, Drop, FrameRef, Handle, MemoryStreamBank, TrustedScreen, capture_many,
)


def make_pixels(width=6, height=4, seed=0):
    return (np.arange(width * height * 3, dtype=np.uint16)
            .reshape(height, width, 3) + seed).astype(np.uint8)


def make_ring(pixels, *, render_index=4, slots=3, slot_seq=2):
    """Model a committed producer header/payload in an actual bytearray ring."""
    height, width, _ = pixels.shape
    buf = bytearray(rtring.ring_bytes(slots, width, height))
    struct.pack_into("<8sIIIIQQQ", buf, 0, rtring.MAGIC, rtring.VERSION,
                     slots, width, height, width * height * 3,
                     rtring.data_off(slots), render_index + 1)
    slot = render_index % slots
    header_offset = rtring.HDR + slot * rtring.SLOT_HDR
    struct.pack_into("<QQ", buf, header_offset, slot_seq, render_index)
    data_offset = rtring.data_off(slots) + slot * width * height * 3
    payload = pixels.tobytes()
    buf[data_offset:data_offset + len(payload)] = payload
    struct.pack_into("<I", buf, header_offset + rtring.CRC_OFF, zlib.crc32(payload))
    return buf


class Source:
    def __init__(self, *screens):
        self.screens = {self.key(item.handle): item for item in screens}
        self.calls = []

    @staticmethod
    def key(handle):
        return handle.kind, handle.canonical_id, handle.generation

    def resolve(self, handle):
        self.calls.append(self.key(handle))
        return self.screens[self.key(handle)]


def target(name="screen0", generation=1):
    return Handle("xvfb_screen", name, generation)


def fixture(handle=None, pixels=None, *, epoch=2, seq=4, slots=3, slot_seq=2):
    handle = target() if handle is None else handle
    pixels = make_pixels() if pixels is None else pixels
    return TrustedScreen(handle, epoch, pixels.shape[1], pixels.shape[0],
                         make_ring(pixels, render_index=seq,
                                   slots=slots, slot_seq=slot_seq))


def spec(handle=None, *, epoch=2, x=1, y=1, width=3, height=2, seq=4):
    return CropSpec(target() if handle is None else handle,
                    epoch, x, y, width, height, seq)


class CaptureManyContract(unittest.TestCase):
    def assert_drop(self, result, reason):
        self.assertIsInstance(result, Drop)
        self.assertEqual(result.reason, reason)

    def test_valid_crop_is_byte_exact_and_owned_immutable(self):
        """G M4 2 positive control: neither stale nor differently sampled."""
        pixels = make_pixels()
        screen = fixture(pixels=pixels)
        bank = MemoryStreamBank(quota_bytes=1 << 16, slots=2)
        got, = capture_many([spec()], source=Source(screen), bank=bank)
        self.assertIsInstance(got, FrameRef)
        expected = pixels[1:3, 1:4]
        np.testing.assert_array_equal(got.rgb, expected)
        self.assertEqual(got.rgb.tobytes(), expected.tobytes())
        self.assertEqual(got.digest, hashlib.sha256(expected.tobytes()).hexdigest())
        self.assertEqual((got.width, got.height, got.epoch, got.frame_seq), (3, 2, 2, 4))
        self.assertEqual(got.handle, screen.handle)
        self.assertEqual(got.rgb.dtype, np.uint8)
        self.assertTrue(got.rgb.flags.c_contiguous)
        self.assertFalse(got.rgb.flags.writeable)
        root = got.rgb
        while isinstance(root, np.ndarray) and root.base is not None:
            root = root.base
        self.assertIs(type(root), bytes)
        with self.assertRaises(ValueError):
            got.rgb.setflags(write=True)
        with self.assertRaises(ValueError):
            got.rgb[0, 0, 0] = 255
        # No mutable source buffer or ring wrap may change the published copy.
        screen.ring[:] = b"\x00" * len(screen.ring)
        np.testing.assert_array_equal(got.rgb, expected)
        self.assertEqual(bank.reserved_bytes, rtring.ring_bytes(2, 3, 2))

    def test_odd_sequence_is_torn_not_accepted(self):
        """Named mutant: dropping the odd-sequence predicate must fail."""
        screen = fixture(slot_seq=3)
        bank = MemoryStreamBank(quota_bytes=1 << 16)
        got, = capture_many([spec()], source=Source(screen), bank=bank)
        self.assert_drop(got, "torn")
        self.assertEqual(bank.reserved_bytes, 0)

    def test_out_of_bounds_crop_is_never_clipped_or_published(self):
        """Named mutant: x+width crossing trusted width must fail."""
        screen = fixture()
        bank = MemoryStreamBank(quota_bytes=1 << 16)
        got, = capture_many([spec(x=4, width=3)], source=Source(screen), bank=bank)
        self.assert_drop(got, "invalid_crop")
        self.assertEqual(bank.reserved_bytes, 0)
        got, = capture_many([spec(y=3, height=2)], source=Source(screen), bank=bank)
        self.assert_drop(got, "invalid_crop")
        self.assertEqual(bank.reserved_bytes, 0)

    def test_invalid_integer_domains_are_fail_closed(self):
        source = Source(fixture())
        for changes in ({"x": -1}, {"x": True}, {"width": 0},
                        {"height": -2}, {"width": 1.5}, {"epoch": True},
                        {"seq": False}, {"seq": -1}):
            with self.subTest(changes=changes):
                got, = capture_many([spec(**changes)], source=source)
                self.assertIsInstance(got, Drop)
        good, = capture_many([spec(x=3, y=2, width=3, height=2)], source=source)
        self.assertIsInstance(good, FrameRef)  # inclusive right/bottom edge

    def test_only_registered_xvfb_screens_can_be_captured(self):
        source = Source(fixture())
        for kind in ("x11_window", "cdp_tab", "capsule", "godot_window", "other"):
            with self.subTest(kind=kind):
                unknown = (SimpleNamespace(kind=kind, canonical_id="screen0", generation=1)
                           if kind == "other" else Handle(kind, "screen0", 1))
                got, = capture_many([spec(handle=unknown)], source=source)
                self.assert_drop(got, "unsupported_mode")
        self.assertEqual(source.calls, [])  # refuse before invoking capture

    def test_source_identity_epoch_and_layout_mismatches(self):
        h = target()
        pixels = make_pixels()
        trusted = fixture(handle=h, pixels=pixels)
        other = fixture(handle=target("other"), pixels=pixels)
        got, = capture_many([spec(handle=h)], source=Source(other))
        self.assert_drop(got, "source_unavailable")
        got, = capture_many([spec(epoch=3)], source=Source(trusted))
        self.assert_drop(got, "stale_epoch")
        tampered = bytearray(trusted.ring)
        struct.pack_into("<I", tampered, 16, 5)  # overwrite ring width header
        bad_ring = TrustedScreen(h, 2, pixels.shape[1], pixels.shape[0], tampered)
        got, = capture_many([spec()], source=Source(bad_ring))
        self.assert_drop(got, "invalid_source_ring")

    def test_wrong_render_index_or_crc_never_delivered(self):
        screen = fixture()
        raw = bytearray(screen.ring)
        off = rtring.HDR + 4 % 3 * rtring.SLOT_HDR
        struct.pack_into("<Q", raw, off + 8, 9)
        wrong = TrustedScreen(screen.handle, 2, screen.width, screen.height, raw)
        got, = capture_many([spec()], source=Source(wrong))
        self.assert_drop(got, "lapped")
        raw = bytearray(screen.ring)
        payload = rtring.data_off(3) + 4 % 3 * screen.width * screen.height * 3
        raw[payload] ^= 1
        crc_bad = TrustedScreen(screen.handle, 2, screen.width, screen.height, raw)
        got, = capture_many([spec()], source=Source(crc_bad))
        self.assert_drop(got, "torn")

    def test_lapped_and_absent_source_frames(self):
        pixels = make_pixels()
        newer = fixture(pixels=pixels, seq=6, slots=2)
        got, = capture_many([spec(seq=4)], source=Source(newer))
        self.assert_drop(got, "lapped")
        got, = capture_many([spec(seq=7)], source=Source(newer))
        self.assert_drop(got, "absent")
        raw = bytearray(newer.ring)
        struct.pack_into("<Q", raw, rtring.HEAD_OFF, 0)
        empty = TrustedScreen(newer.handle, 2, newer.width, newer.height, raw)
        got, = capture_many([spec(seq=0)], source=Source(empty))
        self.assert_drop(got, "absent")

    def test_multiplex_crops_screens_generations_independent_tokens_and_charges(self):
        a = target("screen0", 1)
        b = target("screen1", 1)
        anew = target("screen0", 2)
        screens = [fixture(h, make_pixels(seed=i)) for i, h in enumerate((a, b, anew))]
        source = Source(*screens)
        first = spec(a, x=0, y=0, width=2, height=2)
        second = spec(a, x=2, y=1, width=2, height=2)
        third = spec(b, x=0, y=0, width=2, height=2)
        fourth = spec(anew, x=0, y=0, width=2, height=2)
        bank = MemoryStreamBank(quota_bytes=1 << 16, slots=2)
        frames = capture_many([first, second, third, fourth], source=source, bank=bank)
        self.assertTrue(all(isinstance(frame, FrameRef) for frame in frames))
        self.assertEqual(len(frames), 4)
        tokens = [bank.token_for(s) for s in (first, second, third, fourth)]
        self.assertEqual(len(set(tokens)), 4)
        self.assertEqual(bank.reserved_bytes, 4 * rtring.ring_bytes(2, 2, 2))
        self.assertEqual(len(source.calls), 3)  # shared source, distinct crops
        self.assertNotEqual(frames[0].digest, frames[1].digest)

    def test_batch_admission_exact_boundary_and_rollback_preserves_existing(self):
        screen = fixture()
        source = Source(screen)
        first = spec(x=0, y=0, width=2, height=2)
        new_a = spec(x=2, y=0, width=2, height=2)
        new_b = spec(x=4, y=0, width=2, height=2)
        unit = rtring.ring_bytes(2, 2, 2)
        bank = MemoryStreamBank(quota_bytes=2 * unit, slots=2)
        got, = capture_many([first], source=source, bank=bank)
        self.assertIsInstance(got, FrameRef)
        self.assertEqual(bank.reserved_bytes, unit)
        failures = capture_many([new_a, new_b], source=source, bank=bank)
        self.assertEqual([f.reason for f in failures], ["quota", "quota"])
        self.assertEqual(bank.reserved_bytes, unit)
        self.assertIsNone(bank.token_for(new_a))
        self.assertIsNotNone(bank.token_for(first))
        # One exact-boundary reservation is accepted, charged exactly once.
        got, = capture_many([new_a], source=source, bank=bank)
        self.assertIsInstance(got, FrameRef)
        self.assertEqual(bank.reserved_bytes, 2 * unit)
        got, = capture_many([new_a], source=source, bank=bank)
        self.assert_drop(got, "stale_sequence")
        self.assertEqual(bank.reserved_bytes, 2 * unit)
        bank.close()
        self.assertEqual(bank.reserved_bytes, 0)

    def test_epoch_geometry_pinned_across_calls(self):
        h = target()
        small = fixture(handle=h, pixels=make_pixels())
        bank = MemoryStreamBank(quota_bytes=1 << 16, slots=2)
        one, = capture_many([spec(handle=h)], source=Source(small), bank=bank)
        self.assertIsInstance(one, FrameRef)
        larger = fixture(handle=h, pixels=make_pixels(8, 5))
        two, = capture_many([spec(handle=h)], source=Source(larger), bank=bank)
        self.assert_drop(two, "geometry_epoch")
        self.assertEqual(bank.reserved_bytes, rtring.ring_bytes(2, 3, 2))
        epoch_new = fixture(handle=h, pixels=make_pixels(8, 5), epoch=3)
        three, = capture_many([spec(handle=h, epoch=3)], source=Source(epoch_new), bank=bank)
        self.assertIsInstance(three, FrameRef)
        self.assertEqual(bank.reserved_bytes, 2 * rtring.ring_bytes(2, 3, 2))

    def test_public_records_exact_order_and_frozen(self):
        from vadapt.multi_x11_ramcap import StreamToken
        self.assertEqual([f.name for f in fields(CropSpec)],
                         ["handle", "epoch", "x", "y", "width", "height", "frame_seq"])
        self.assertEqual([f.name for f in fields(Handle)],
                         ["kind", "canonical_id", "generation"])
        self.assertEqual([f.name for f in fields(FrameRef)],
                         ["handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"])
        self.assertEqual([f.name for f in fields(Drop)],
                         ["handle", "epoch", "frame_seq", "reason"])
        self.assertEqual([f.name for f in fields(StreamToken)], ["handle", "epoch"])
        with self.assertRaises(FrozenInstanceError):
            spec().x = 99

    def test_injected_bank_noop_cannot_produce_false_success(self):
        class NoOpBank:
            def admit(self, requests):
                pass
            def publish(self, request, image):
                return None
        result, = capture_many([spec()], source=Source(fixture()), bank=NoOpBank())
        self.assert_drop(result, "bank_invalid_receipt")

    def test_capture_path_forbids_disk_network_and_process_launch(self):
        source = Source(fixture())
        bank = MemoryStreamBank(quota_bytes=1 << 16)
        def forbidden(*_args, **_kwargs):
            raise AssertionError("forbidden I/O in pure RAM path")
        with (mock.patch.object(builtins, "open", forbidden),
              mock.patch.object(os, "open", forbidden),
              mock.patch.object(socket, "socket", forbidden),
              mock.patch.object(subprocess, "Popen", forbidden)):
            got, = capture_many([spec()], source=source, bank=bank)
        self.assertIsInstance(got, FrameRef)


if __name__ == "__main__":
    unittest.main()
