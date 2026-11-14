"""Offline capture contract tests using independent struct/zlib ring oracles."""
import gc
import hashlib
import struct
import unittest
import weakref
import zlib
from dataclasses import fields, replace

import numpy as np
from vadapt import rtring
from vadapt.multi_x11_ramcap import (
    CaptureFailure, CropSpec, Drop, FrameRef, Handle, MemoryStreamBank,
    TrustedScreen, capture_many,
)


def build_ring(w=4, h=3, *, seq=5, slots=2):
    """Literal v2 wire fixture; no subject encoder or filesystem frames."""
    pixels = bytes((7 + i * 29) % 256 for i in range(w * h * 3))
    off = (64 + slots * 64 + 4095) // 4096 * 4096
    raw = bytearray(off + slots * len(pixels))
    struct.pack_into("<8sIIIIQQQ", raw, 0, b"VQARING1", 2, slots, w, h,
                     len(pixels), off, seq + 1)
    k = seq % slots
    struct.pack_into("<QQI", raw, 64 + 64 * k, 2, seq, zlib.crc32(pixels))
    raw[off + k * len(pixels):off + (k + 1) * len(pixels)] = pixels
    return raw, pixels


def sliced_pixels(pixels, sw, x, y, w, h):
    return b"".join(pixels[((y + row) * sw + x) * 3:
                           ((y + row) * sw + x + w) * 3]
                    for row in range(h))


class Source:
    def __init__(self, screen=None):
        self.screen = screen
        self.calls = []

    def resolve(self, handle):
        self.calls.append(handle)
        if self.screen is None:
            raise KeyError("unregistered")
        return self.screen


class Bank(MemoryStreamBank):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.admissions = []
        self.publications = []

    def admit(self, requests):
        self.admissions.append(tuple(requests))
        return super().admit(requests)

    def publish(self, spec, image):
        self.publications.append((spec, image))
        return super().publish(spec, image)


class BadReceiptBank:
    def __init__(self, receipt):
        self.receipt = receipt
        self.admissions = []
        self.publications = []

    def admit(self, requests):
        self.admissions.append(tuple(requests))

    def publish(self, spec, image):
        self.publications.append(spec)
        return self.receipt(spec, image)


class CaptureContractTests(unittest.TestCase):
    def setUp(self):
        self.handle = Handle("xvfb_screen", "unit", 1)
        self.raw, self.pixels = build_ring()
        self.source = Source(TrustedScreen(self.handle, 2, 4, 3, self.raw))
        self.a = CropSpec(self.handle, 2, 0, 0, 2, 2, 5)
        self.b = CropSpec(self.handle, 2, 2, 1, 2, 2, 5)
        self.bank = Bank(quota_bytes=65536, slots=2)

    def run_capture(self, *specs, source=None, bank=None):
        return capture_many(specs,
                            source=self.source if source is None else source,
                            bank=self.bank if bank is None else bank)

    def drop(self, result, reason):
        self.assertIsInstance(result, Drop)
        self.assertEqual(result.reason, reason)
        self.assertNotIsInstance(result, FrameRef)

    def test_record_fields_and_legacy_generation(self):
        self.assertEqual(tuple(f.name for f in fields(CropSpec)),
                         ("handle", "epoch", "x", "y", "width", "height", "frame_seq"))
        self.assertEqual(tuple(f.name for f in fields(TrustedScreen)),
                         ("handle", "epoch", "width", "height", "ring"))
        self.assertEqual(tuple(f.name for f in fields(FrameRef)),
                         ("handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"))
        self.assertEqual(tuple(f.name for f in fields(Drop)),
                         ("handle", "epoch", "frame_seq", "reason"))
        self.assertEqual(Handle("xvfb_screen", "wire", 0).generation, 0)

    def test_positive_independent_immutable_crops_and_hashes(self):
        self.assertEqual(len(self.raw), rtring.ring_bytes(2, 4, 3))
        a, b = self.run_capture(self.a, self.b)
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(len(self.bank.admissions), 1)
        self.assertEqual(len(self.bank.publications), 2)
        for spec, image in self.bank.publications:
            expected = sliced_pixels(self.pixels, 4, spec.x, spec.y, spec.width, spec.height)
            self.assertEqual(image.tobytes(), expected)
            self.assertFalse(image.flags.writeable)
            with self.assertRaises(ValueError):
                image.setflags(write=True)  # bank cannot tamper with source crop
        self.assertFalse(np.shares_memory(self.bank.publications[0][1],
                                          self.bank.publications[1][1]))
        for spec, ref in ((self.a, a), (self.b, b)):
            self.assertIsInstance(ref, FrameRef)
            expected = sliced_pixels(self.pixels, 4, spec.x, spec.y, spec.width, spec.height)
            self.assertEqual((ref.handle, ref.epoch, ref.frame_seq, ref.width, ref.height),
                             (spec.handle, spec.epoch, spec.frame_seq, spec.width, spec.height))
            self.assertEqual(ref.rgb.tobytes(), expected)
            self.assertEqual(ref.rgb.shape, (spec.height, spec.width, 3))
            self.assertEqual(ref.digest, hashlib.sha256(expected).hexdigest())
            self.assertFalse(ref.rgb.flags.writeable)
            with self.assertRaises(ValueError):
                ref.rgb.setflags(write=True)
        self.assertNotEqual(a.digest, b.digest)
        self.assertFalse(np.shares_memory(a.rgb, b.rgb))
        self.raw[:] = b"\0" * len(self.raw)
        self.bank.close()
        self.assertEqual(a.rgb.tobytes(), sliced_pixels(self.pixels, 4, 0, 0, 2, 2))
        self.assertEqual(b.rgb.tobytes(), sliced_pixels(self.pixels, 4, 2, 1, 2, 2))

    def test_wrong_target_and_unregistered_zero_generation(self):
        wrong = replace(self.a, handle=Handle("x11_window", "window", 1))
        zero = replace(self.a, handle=Handle("xvfb_screen", "wire", 0))
        self.drop(self.run_capture(wrong)[0], "unsupported_mode")
        self.drop(self.run_capture(zero)[0], "invalid_handle")
        self.assertEqual(self.source.calls, [])
        self.assertEqual(self.bank.admissions, [])

    def test_epoch_and_sequence_must_be_exact_nonnegative_integers(self):
        for epoch in (False, -1, 1.5):
            with self.subTest(epoch=epoch):
                self.drop(self.run_capture(replace(self.a, epoch=epoch))[0], "stale_epoch")
        for seq in (-1, True, 1.5, (1 << 64) - 1, 1 << 64):
            with self.subTest(seq=seq):
                self.drop(self.run_capture(replace(self.a, frame_seq=seq))[0],
                          "invalid_sequence")
        self.assertEqual(self.source.calls, [])
        self.assertEqual(self.bank.reserved_bytes, 0)

    def test_crop_coordinates_are_integer_positive_and_in_bounds(self):
        for spec in (replace(self.a, x=-1), replace(self.a, x=True),
                     replace(self.a, x=3), replace(self.a, y=0.25),
                     replace(self.a, width=0), replace(self.a, height=False),
                     replace(self.a, y=2)):
            with self.subTest(spec=spec):
                self.drop(self.run_capture(spec)[0], "invalid_crop")
        self.assertEqual(self.bank.admissions, [])
        self.assertEqual(self.bank.publications, [])

    def test_identity_geometry_and_malformed_provenance(self):
        wrong_id = TrustedScreen(Handle("xvfb_screen", "other", 1), 2, 4, 3, self.raw)
        for screen, reason in (
            (wrong_id, "source_identity"),
            ({"handle": self.handle}, "source_identity"),
            (TrustedScreen(self.handle, 3, 4, 3, self.raw), "stale_epoch"),
            (TrustedScreen(self.handle, 2, True, 3, self.raw), "source_geometry"),
            (TrustedScreen(self.handle, 2, 0, 3, self.raw), "source_geometry"),
        ):
            with self.subTest(reason=reason, screen=screen):
                self.source.screen = screen
                self.drop(self.run_capture(self.a)[0], reason)
        self.assertEqual(self.bank.reserved_bytes, 0)

    def test_source_ring_wire_header_and_size_are_checked(self):
        for fault in ("magic", "width", "short"):
            with self.subTest(fault=fault):
                raw = bytearray(self.raw)
                if fault == "magic":
                    raw[:8] = b"BADMAGIC"
                elif fault == "width":
                    struct.pack_into("<I", raw, 16, 10)
                else:
                    raw.pop()
                self.source.screen = TrustedScreen(self.handle, 2, 4, 3, raw)
                self.drop(self.run_capture(self.a)[0], "invalid_source_ring")
        self.assertEqual(self.bank.publications, [])
        self.assertEqual(self.bank.reserved_bytes, 0)

    def test_odd_seqlock_and_bad_crc_are_torn(self):
        for fault in ("odd", "crc"):
            with self.subTest(fault=fault):
                raw = bytearray(self.raw)
                if fault == "odd":
                    struct.pack_into("<Q", raw, 64 + 64 * (5 % 2), 3)
                else:
                    raw[4096 + (5 % 2) * 4 * 3 * 3] ^= 255
                self.source.screen = TrustedScreen(self.handle, 2, 4, 3, raw)
                self.drop(self.run_capture(self.a)[0], "torn")
        self.assertEqual(self.bank.reserved_bytes, 0)

    def test_absent_lapped_and_invalid_slot_indices(self):
        for head, slot_index, reason in ((5, 5, "absent"),
                                         (8, 5, "lapped"),  # exactly two slots old
                                         (10, 5, "lapped"),
                                         (6, 7, "lapped"),
                                         (6, 4, "absent")):
            with self.subTest(head=head, slot_index=slot_index):
                raw = bytearray(self.raw)
                struct.pack_into("<Q", raw, 40, head)
                struct.pack_into("<Q", raw, 64 + 64 * (5 % 2) + 8, slot_index)
                self.source.screen = TrustedScreen(self.handle, 2, 4, 3, raw)
                self.drop(self.run_capture(self.a)[0], reason)
        self.assertEqual(self.bank.publications, [])

    def test_source_unavailable_is_cached(self):
        missing = Source()
        result = self.run_capture(self.a, self.b, source=missing)
        self.assertEqual([x.reason for x in result], ["source_unavailable"] * 2)
        self.assertEqual(missing.calls, [self.handle])
        self.assertEqual(self.bank.admissions, [])

    def test_sibling_survives_invalid_and_absent(self):
        result = self.run_capture(replace(self.b, x=5), self.a,
                                  replace(self.b, frame_seq=4))
        self.assertEqual([type(x) for x in result], [Drop, FrameRef, Drop])
        self.assertEqual((result[0].reason, result[2].reason),
                         ("invalid_crop", "absent"))
        self.assertEqual(len(self.source.calls), 1)
        self.assertEqual(len(self.bank.publications), 1)

    def test_quota_is_transactional_before_any_publication(self):
        one = 4096 + 2 * 2 * 2 * 3
        self.bank = Bank(quota_bytes=2 * one - 1, slots=2)
        out = self.run_capture(self.a, self.b)
        self.assertEqual([x.reason for x in out], ["quota", "quota"])
        self.assertEqual(self.bank.reserved_bytes, 0)
        self.assertFalse(self.bank._rings)
        self.assertFalse(self.bank._geometry)
        self.assertEqual(self.bank.publications, [])

    def test_failed_new_quota_does_not_touch_existing_reservation(self):
        one = 4096 + 2 * 2 * 2 * 3
        self.bank = Bank(quota_bytes=one, slots=2)
        self.assertIsInstance(self.run_capture(self.a)[0], FrameRef)
        keys = set(self.bank._rings)
        out = self.run_capture(self.a, self.b)
        self.assertEqual([x.reason for x in out], ["quota", "quota"])
        self.assertEqual(self.bank.reserved_bytes, one)
        self.assertEqual(set(self.bank._rings), keys)
        self.assertEqual(len(self.bank.publications), 1)

    def test_geometry_change_requires_a_new_epoch(self):
        self.assertIsInstance(self.run_capture(self.a)[0], FrameRef)
        reserved = self.bank.reserved_bytes
        raw, _ = build_ring(5, 3)
        self.source.screen = TrustedScreen(self.handle, 2, 5, 3, raw)
        changed = replace(self.a, x=3)
        self.drop(self.run_capture(changed)[0], "geometry_epoch")
        self.assertEqual(self.bank.reserved_bytes, reserved)
        self.source.screen = TrustedScreen(self.handle, 3, 5, 3, raw)
        self.assertIsInstance(self.run_capture(replace(changed, epoch=3))[0], FrameRef)

    def test_invalid_bank_receipts_mint_no_frames(self):
        expected = sliced_pixels(self.pixels, 4, 0, 0, 2, 2)
        immutable = np.frombuffer(expected, dtype=np.uint8).reshape(2, 2, 3)
        mutable = np.array(immutable, copy=True)
        mutable.setflags(write=False)
        digest = hashlib.sha256(expected).hexdigest()
        functions = (
            lambda s, a: Drop(s.handle, s.epoch, s.frame_seq, "absent"),
            lambda s, a: FrameRef(s.handle, s.epoch, s.frame_seq, 2, 2,
                                  immutable, "0" * 64),
            lambda s, a: FrameRef(s.handle, s.epoch, s.frame_seq, 2, 2,
                                  mutable, digest),
            lambda s, a: FrameRef(s.handle, s.epoch + 1, s.frame_seq, 2, 2,
                                  immutable, digest),
        )
        for fn in functions:
            with self.subTest(fn=fn):
                bad = BadReceiptBank(fn)
                self.drop(self.run_capture(self.a, bank=bad)[0], "bank_invalid_receipt")
                self.assertEqual(len(bad.admissions), 1)
                self.assertEqual(bad.publications, [self.a])
        self.assertEqual(self.bank.reserved_bytes, 0)

    def test_close_releases_actual_owned_ring_and_keeps_frame(self):
        ref = self.run_capture(self.a)[0]
        self.assertIsInstance(ref, FrameRef)
        owned = next(iter(self.bank._rings.values()))
        weak = weakref.ref(owned)
        payload = ref.rgb.tobytes()
        del owned
        self.bank.close()
        gc.collect()
        self.assertIsNone(weak())
        self.assertEqual(self.bank.reserved_bytes, 0)
        self.assertFalse(self.bank._rings)
        self.assertEqual(ref.rgb.tobytes(), payload)
        with self.assertRaises(ValueError):
            ref.rgb.setflags(write=True)

    def test_protocol_errors_have_no_side_effects(self):
        with self.assertRaisesRegex(TypeError, "source must expose"):
            capture_many([self.a], source=object(), bank=self.bank)
        with self.assertRaisesRegex(TypeError, "bank must expose"):
            capture_many([self.a], source=self.source, bank=object())
        with self.assertRaisesRegex(TypeError, "specs must contain CropSpec"):
            self.run_capture(self.a, object())
        self.assertEqual(self.source.calls, [])
        self.assertEqual(self.bank.reserved_bytes, 0)


if __name__ == "__main__":
    unittest.main()
