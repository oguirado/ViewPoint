"""W8U resident-pair offline public-path tests; all synthetic frames stay in RAM."""

import hashlib
import struct
import unittest
from unittest.mock import patch

from vadapt import xfb, w8u_pair as pair


BOUNDS = pair.PairBounds(max_header_bytes=4096, max_colors=16,
                         max_stride_bytes=4096, max_record_bytes=1 << 20)


def xwd(tick=0, *, width=3, height=2, name=b"test\x00", byte_order=0,
        bitmap_bit_order=None, colors=0, extra_header=0):
    """Generate a valid changing TrueColor XWD without filesystem access."""
    hs = xfb.MIN_HEADER + len(name) + extra_header
    bo = byte_order if bitmap_bit_order is None else bitmap_bit_order
    fields = (hs, 7, 2, 24, width, height, 0, byte_order, 32, bo, 32, 32,
              width * 4, 4, 0xFF0000, 0xFF00, 0xFF, 8, colors, colors,
              width, height, 0, 0, 0)
    rows = bytearray()
    for y in range(height):
        for x in range(width):
            r, g, b = (tick + x + y) % 256, (7 + x) % 256, (23 + y) % 256
            rows.extend((b, g, r, 0) if byte_order == 0 else (0, r, g, b))
    return (struct.pack(">25I", *fields) + name + b"Z" * extra_header
            + b"\x00" * (colors * xfb.COLOR_ENTRY) + bytes(rows))


class RedrawingFake:
    """Redraw on every ungrabbed read, hold pixels throughout an explicit grab."""

    def __init__(self):
        self.tick = 0
        self.held = False
        self.released = False
        self.receipt = None
        self.ungrab_ok = True
        self.expired = False
        self.mutate_during_grab = False
        self.reads = 0
        self.events = []

    def read(self, alternate_header=False):
        self.reads += 1
        if not self.held or self.mutate_during_grab:
            self.tick += 1
        self.events.append("read")
        return xwd(self.tick, name=b"alternate\x00" if alternate_header else b"frame\x00",
                   byte_order=1 if alternate_header else 0)

    def produce(self):
        assert not self.held
        self.held = True
        self.released = False
        self.events.append("grab")
        left = self.read()
        right = self.read(alternate_header=True)
        self.held = False
        self.released = self.ungrab_ok
        self.events.append("ungrab")
        self.receipt = object()  # opaque sender completion; only verifier checks it
        return pair.PairSample(left, right, self.receipt,
                               bytes.fromhex(xfb.frame_sha256(left)))

    def verify(self, token):
        if token is not self.receipt or not self.events or self.events[-1] != "ungrab":
            raise xfb.XwdError("E_WIRE", "unknown sender completion")
        return pair.VerifiedGrab(100, 201 if self.expired else 150, 200,
                                 True, self.released)


class TestW8UPair(unittest.TestCase):
    def assertCode(self, code, operation):
        with self.assertRaises(xfb.XwdError) as cm:
            operation()
        self.assertEqual(cm.exception.code, code)

    def test_positive_header_variants_canonical_rgb_and_owned_hash(self):
        a, b = xwd(12, name=b"first\x00"), xwd(12, name=b"second\x00", byte_order=1, colors=2)
        self.assertNotEqual(a, b)
        ev = pair.validate_pair(a, b, expected_geometry=(3, 2), bounds=BOUNDS,
                                advertised_sha256=bytes.fromhex(xfb.frame_sha256(a)))
        self.assertEqual(ev.rgb24, xfb.rgb24(a))
        self.assertIs(type(ev.rgb24), bytes)
        self.assertEqual(ev.sha256, hashlib.sha256(ev.rgb24).hexdigest())
        self.assertEqual((ev.width, ev.height, ev.stride, ev.mode), (3, 2, 9, pair.RESIDENT_PAIR))
        self.assertEqual(ev.framebuffer_sha256, ev.getimage_sha256)
        changed = bytearray(a)
        owned = pair.validate_pair(changed, b, expected_geometry=(3, 2), bounds=BOUNDS).rgb24
        changed[-4] ^= 10
        self.assertEqual(owned, ev.rgb24)

    def test_continuously_redrawing_legacy_fallback_is_bounded_torn(self):
        source = RedrawingFake()
        self.assertCode("E_TORN", lambda: pair.capture_unsynchronised_consistent(
            source.read, expected_geometry=(3, 2), bounds=BOUNDS, tries=8))
        self.assertEqual(source.reads, 8)
        self.assertEqual(len(set(range(1, source.tick + 1))), 8)

    def test_grab_held_pair_agrees_then_redraw_resumes_and_publishes_after_ungrab(self):
        source = RedrawingFake()
        publications = []

        def publish(evidence):
            self.assertEqual(source.events[-1], "ungrab")
            publications.append(evidence)
            return len(publications)

        self.assertEqual(pair.capture_resident_pair(
            source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=source.verify, publish=publish), 1)
        self.assertEqual(len(publications), 1)
        self.assertNotEqual(xfb.frame_sha256(source.read()), publications[0].sha256)
        self.assertEqual(source.events[:4], ["grab", "read", "read", "ungrab"])

    def test_grab_claim_cannot_hide_mutation_between_legs(self):
        source = RedrawingFake()
        source.mutate_during_grab = True
        publications = []
        self.assertCode("E_TORN", lambda: pair.capture_resident_pair(
            source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=source.verify, publish=publications.append))
        self.assertEqual(publications, [])
        self.assertTrue(source.released)

    def test_forged_advertised_sha_has_no_publication(self):
        source = RedrawingFake()
        publications = []

        def forged():
            sample = source.produce()
            return pair.PairSample(sample.framebuffer_xwd, sample.getimage_xwd,
                                   sample.sender_completion, b"\x11" * 32)

        self.assertCode("E_HASH", lambda: pair.capture_resident_pair(
            forged, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=source.verify, publish=publications.append))
        self.assertEqual(publications, [])

    def test_ungrab_failure_refuses_even_equal_legs(self):
        source = RedrawingFake()
        source.ungrab_ok = False
        publications = []
        self.assertCode("E_TIMEOUT", lambda: pair.capture_resident_pair(
            source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=source.verify, publish=publications.append))
        self.assertEqual(publications, [])

    def test_expired_grab_refuses_even_equal_legs(self):
        source = RedrawingFake()
        source.expired = True
        publications = []
        self.assertCode("E_TIMEOUT", lambda: pair.capture_resident_pair(
            source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=source.verify, publish=publications.append))
        self.assertEqual(publications, [])

    def test_cancellation_just_before_publish_refuses(self):
        source = RedrawingFake()
        publications = []
        self.assertCode("E_TIMEOUT", lambda: pair.capture_resident_pair(
            source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=source.verify, publish=publications.append,
            check_cancelled=lambda: True))
        self.assertEqual(publications, [])
        self.assertTrue(source.released)

    def test_verifier_must_be_injected_not_sender_flag(self):
        source = RedrawingFake()
        publications = []
        self.assertCode("E_WIRE", lambda: pair.capture_resident_pair(
            source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=None, publish=publications.append))
        self.assertEqual(source.reads, 0)
        self.assertCode("E_WIRE", lambda: pair.capture_resident_pair(
            source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=lambda _: True, publish=publications.append))
        self.assertEqual(publications, [])

    def test_missing_advertised_sha_cannot_be_admitted(self):
        source = RedrawingFake()
        published = []
        def missing():
            sample = source.produce()
            return pair.PairSample(sample.framebuffer_xwd, sample.getimage_xwd,
                                   sample.sender_completion)
        self.assertCode("E_HASH", lambda: pair.capture_resident_pair(
            missing, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=source.verify, publish=published.append))
        self.assertEqual(published, [])

    def test_verified_grab_must_cover_both_legs(self):
        source = RedrawingFake()
        publications = []
        self.assertCode("E_TORN", lambda: pair.capture_resident_pair(
            source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=lambda _: pair.VerifiedGrab(100, 150, 200, False, True),
            publish=publications.append))
        self.assertEqual(publications, [])

    def test_bounded_metadata_rejected_before_canonical_allocation(self):
        for raw in (xwd(0, extra_header=4097), xwd(0, colors=17)):
            with self.subTest(size=len(raw)), patch.object(xfb, "rgb24") as canonical:
                self.assertCode("E_LENGTH", lambda: pair.validate_pair(
                    raw, xwd(), expected_geometry=(3, 2), bounds=BOUNDS))
                canonical.assert_not_called()

    def test_invalid_bitmap_bit_order_is_rejected(self):
        raw = xwd(0, bitmap_bit_order=2)
        with patch.object(xfb, "rgb24") as canonical:
            self.assertCode("E_XWD_BYTE_ORDER", lambda: pair.validate_pair(
                raw, xwd(), expected_geometry=(3, 2), bounds=BOUNDS))
            canonical.assert_not_called()

    def test_record_cap_checked_before_pixel_conversion(self):
        tight = pair.PairBounds(4096, 16, 4096, 128 + 16 + 2 * len(xwd()) - 1)
        with patch.object(xfb, "rgb24") as canonical:
            self.assertCode("E_LENGTH", lambda: pair.validate_pair(
                xwd(), xwd(), expected_geometry=(3, 2), bounds=tight))
            canonical.assert_not_called()

    def test_forged_sha_with_valid_pixels_is_hash_not_torn(self):
        raw = xwd(9)
        self.assertCode("E_HASH", lambda: pair.validate_pair(
            raw, raw, expected_geometry=(3, 2), bounds=BOUNDS,
            advertised_sha256=b"\xaa" * 32))
        self.assertCode("E_HASH", lambda: pair.validate_pair(
            raw, raw, expected_geometry=(3, 2), bounds=BOUNDS,
            advertised_sha256="F" * 64))

    def test_strict_geometry_and_explicit_numeric_limits(self):
        raw = xwd()
        self.assertCode("E_GEOMETRY", lambda: pair.validate_pair(
            raw, raw, expected_geometry=(3, True), bounds=BOUNDS))
        self.assertCode("E_GEOMETRY", lambda: pair.validate_pair(
            raw, raw, expected_geometry=(2, 2), bounds=BOUNDS))
        self.assertCode("E_LENGTH", lambda: pair.PairBounds(4096, 0, 4096, True))
        self.assertCode("E_LENGTH", lambda: pair.validate_pair(
            raw, raw, expected_geometry=(3, 2), bounds=None))

    def test_closed_selector_never_downgrades(self):
        self.assertEqual(pair.select_capture_mode("resident_pair"), "resident_pair")
        self.assertEqual(pair.select_capture_mode("unsynchronised_consistent"), "unsynchronised_consistent")
        for invalid in ("auto", "fallback", "RESIDENT_PAIR", "", None, True):
            self.assertCode("E_WIRE", lambda invalid=invalid: pair.select_capture_mode(invalid))

    def test_separate_explicit_static_consistent_mode(self):
        raw = xwd()
        ev = pair.capture_unsynchronised_consistent(
            lambda: raw, expected_geometry=(3, 2), bounds=BOUNDS, tries=2)
        self.assertEqual(ev.mode, pair.UNSYNCHRONISED_CONSISTENT)
        self.assertEqual(ev.sha256, xfb.frame_sha256(raw))

    def test_last_good_is_never_reused_after_rejected_pair(self):
        source = RedrawingFake()
        published = []
        pair.capture_resident_pair(source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
                                   verify_sender_completion=source.verify, publish=published.append)
        source.mutate_during_grab = True
        self.assertCode("E_TORN", lambda: pair.capture_resident_pair(
            source.produce, expected_geometry=(3, 2), bounds=BOUNDS,
            verify_sender_completion=source.verify, publish=published.append))
        self.assertEqual(len(published), 1)


MANDATORY_CASES = tuple("tests.test_w8u_ve_pair_unit.TestW8UPair." + method for method in (
    "test_positive_header_variants_canonical_rgb_and_owned_hash",
    "test_continuously_redrawing_legacy_fallback_is_bounded_torn",
    "test_grab_held_pair_agrees_then_redraw_resumes_and_publishes_after_ungrab",
    "test_grab_claim_cannot_hide_mutation_between_legs",
    "test_forged_advertised_sha_has_no_publication",
    "test_ungrab_failure_refuses_even_equal_legs",
    "test_expired_grab_refuses_even_equal_legs",
    "test_cancellation_just_before_publish_refuses",
    "test_verifier_must_be_injected_not_sender_flag",
    "test_verified_grab_must_cover_both_legs",
    "test_missing_advertised_sha_cannot_be_admitted",
    "test_bounded_metadata_rejected_before_canonical_allocation",
    "test_invalid_bitmap_bit_order_is_rejected",
    "test_record_cap_checked_before_pixel_conversion",
    "test_forged_sha_with_valid_pixels_is_hash_not_torn",
    "test_strict_geometry_and_explicit_numeric_limits",
    "test_closed_selector_never_downgrades",
    "test_separate_explicit_static_consistent_mode",
    "test_last_good_is_never_reused_after_rejected_pair",
))


if __name__ == "__main__":
    unittest.main()
