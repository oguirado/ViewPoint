"""P1 B-pair offline behavior/mutant contract: real XWD, w8u, bank, RGB.

Fixtures are constructed independently with struct, literal RGB pixels, and
hashlib. No disk frames, X server, encoder round trip, network or sleeps.
"""

from dataclasses import fields, replace
import hashlib
import struct
import threading
import unittest
from unittest.mock import patch

import numpy as np

from vadapt import proto1_pair_audit as audit
from vadapt import rtring, w8u_pair, xfb
from vadapt.window_stream_bank import (Drop, FrameRef, Handle, StreamToken, StreamSpec,
                                       WindowStreamBank)


W, H = 2, 1
# Literal oracle. Do not generate RGB from any production conversion routine.
RGB = bytes((0x11, 0x22, 0x33, 0x44, 0x55, 0x66))
RGB_SHA = hashlib.sha256(RGB).hexdigest()
LIMITS = w8u_pair.PairBounds(4096, 8, 256, 8192)


def handcrafted_xwd(*, order=0, pixels=RGB, width=W, height=H,
                    bytes_per_line=None, name=b"s\x00", colors=0):
    """Independent handwritten RGB-to-XWD test vector, NOT a production encoder."""
    if bytes_per_line is None:
        bytes_per_line = width * 4
    if len(pixels) != width * height * 3:
        raise ValueError("test oracle pixel geometry mismatch")
    hdr_size = 100 + len(name)
    values = (hdr_size, 7, 2, 24, width, height, 0, order,
              32, order, 32, 32, bytes_per_line, 4,
              0xFF0000, 0x00FF00, 0x0000FF, 8, 0, colors,
              width, height, 0, 0, 0)
    rows = bytearray()
    for y in range(height):
        row = bytearray()
        for x in range(width):
            at = 3 * (width * y + x)
            r, g, b = pixels[at:at + 3]
            row.extend((b, g, r, 0) if order == 0 else (0, r, g, b))
        if bytes_per_line > len(row):
            row.extend(b"\x00" * (bytes_per_line - len(row)))
        else:
            row = row[:bytes_per_line]  # deliberately malformed geometry control
        rows.extend(row)
    return struct.pack(">25I", *values) + name + b"\x00" * (colors * 12) + bytes(rows)


def wire(a, b):
    return struct.pack(">Q", len(a)) + a + struct.pack(">Q", len(b)) + b


def xwd_field(raw, index, val):
    output = bytearray(raw)
    struct.pack_into(">I", output, 4 * index, val)
    return bytes(output)


class TestPairAuditBehavior(unittest.TestCase):
    """This behavior class is re-instantiated fresh for each semantic mutant."""

    def setUp(self):
        self.handle = Handle("xvfb_screen", "root/screen0", 3)
        self.spec = StreamSpec(W, H, 2, rtring.ring_bytes(2, W, H))
        self.bank = WindowStreamBank(quota_bytes=self.spec.quota_bytes)
        self.receipt = object()
        self.events = []
        self.first = handcrafted_xwd(order=0, name=b"fb\x00")
        self.second = handcrafted_xwd(order=1, name=b"get-image\x00", colors=1,
                                      bytes_per_line=12)
        self.bytes = wire(self.first, self.second)
        self.verify_grab = self._grab
        self.verify_collection = self._collection
        self.auditor = self.make_auditor()

    def make_auditor(self, *, run_id="test-run-01", handle=None, bank=None,
                     collection=None, max_skew=45, check_cancelled=None):
        return audit.PairAuditor(
            run_id=run_id, target_handle=self.handle if handle is None else handle,
            expected_geometry=(W, H), bounds=LIMITS,
            bank=self.bank if bank is None else bank, bank_spec=self.spec,
            verify_sender_completion=self.verify_grab,
            verify_collection=self.verify_collection if collection is None else collection,
            max_skew_ns=max_skew, check_cancelled=check_cancelled)

    def _grab(self, receipt):
        self.assertIs(receipt, self.receipt)
        self.events.append("verified-grab")
        return w8u_pair.VerifiedGrab(100, 180, 220, True, True)

    def _collection(self, receipt):
        self.assertIs(receipt, self.receipt)
        self.events.append("verified-collection")
        return audit.VerifiedCollection(self.handle, 110, 140, True, True, True)

    def accept(self, *, payload=None, advertised=RGB_SHA, auditor=None):
        obj = self.auditor if auditor is None else auditor
        return obj.accept(self.bytes if payload is None else payload,
                          advertised_rgb24_sha256=advertised,
                          sender_completion=self.receipt)

    def assert_refused_unchanged(self, code, *, payload=None, advertised=RGB_SHA,
                                 auditor=None):
        obj = self.auditor if auditor is None else auditor
        original_payload = None if payload is None else bytes(payload)
        prev = (obj.frame_count, obj.ack_credits, obj.next_sequence,
                self.bank.reserved_bytes, obj.token)
        last = self.bank.read(obj.token) if obj.token is not None else None
        with self.assertRaises(audit.PairAuditRefusal) as err:
            self.accept(payload=payload, advertised=advertised, auditor=obj)
        self.assertEqual(err.exception.code, code)
        self.assertEqual((obj.frame_count, obj.ack_credits, obj.next_sequence,
                          self.bank.reserved_bytes, obj.token), prev)
        self.assertEqual(self.bank.read(obj.token) if obj.token is not None else None, last)
        if original_payload is not None:
            self.assertEqual(bytes(payload), original_payload)
        return err.exception

    def test_positive_cross_byte_order_owned_and_bank_accounting(self):
        self.assertNotEqual(self.first, self.second)
        mutable = bytearray(self.bytes)
        got = self.accept(payload=mutable)
        self.assertEqual(got.rgb24, RGB)
        self.assertEqual(got.identity.rgb24_sha256, RGB_SHA)
        self.assertEqual(got.stride, W * 3)
        self.assertEqual(got.identity.frame_seq, 1)
        self.assertEqual((got.identity.width, got.identity.height), (W, H))
        self.assertEqual(tuple(f.name for f in fields(audit.FrameIdentity)),
                         ("frame_seq", "width", "height", "rgb24_sha256", "binding_sha256"))
        self.assertEqual(got.mode, w8u_pair.RESIDENT_PAIR)
        self.assertEqual(got.ack_credit, 1)
        self.assertEqual(self.auditor.frame_count, 1)
        self.assertEqual(self.auditor.ack_credits, 1)
        self.assertEqual(self.bank.reserved_bytes, self.spec.quota_bytes)
        self.assertEqual(self.events, ["verified-grab", "verified-collection"])
        ref = self.bank.read(self.auditor.token)
        self.assertIsInstance(ref, FrameRef)
        self.assertFalse(ref.rgb.flags.writeable)
        self.assertEqual(ref.rgb.tobytes(), RGB)
        self.assertEqual(self.auditor.check_bank_frame(ref, got), got.identity)
        mutable[:] = b"\xCC" * len(mutable)
        self.assertEqual(got.rgb24, RGB)
        self.assertEqual(self.bank.read(self.auditor.token).rgb.tobytes(), RGB)
        self.bank.close(self.auditor.token)
        self.assertEqual(self.bank.reserved_bytes, 0)

    def test_positive_reverse_legs_and_next_sequence_no_extra_quota(self):
        first = self.accept()
        second = self.accept(payload=wire(self.second, self.first))
        self.assertEqual(first.identity.frame_seq, 1)
        self.assertEqual(second.identity.frame_seq, 2)
        self.assertEqual(self.auditor.frame_count, 2)
        self.assertEqual(self.auditor.ack_credits, 2)
        self.assertEqual(self.bank.reserved_bytes, self.spec.quota_bytes)
        self.assertNotEqual(first.identity.binding_sha256,
                            second.identity.binding_sha256)
        self.assertEqual(self.bank.read(self.auditor.token).frame_seq, 2)

    def test_torn_one_leg_and_no_publication(self):
        one_bit = bytearray(self.second)
        one_bit[-6] ^= 1
        error = self.assert_refused_unchanged("E_TORN", payload=wire(self.first, one_bit))
        self.assertIsInstance(error.__cause__, xfb.XwdError)
        self.assertEqual(error.__cause__.code, "E_TORN")
        self.assertIsNone(self.auditor.token)

    def test_wrong_advertised_sha_and_no_publication(self):
        error = self.assert_refused_unchanged("E_HASH", advertised="f" * 64)
        self.assertIsInstance(error.__cause__, xfb.XwdError)
        self.assertEqual(error.__cause__.code, "E_HASH")

    def test_bad_payload_length_before_conversion(self):
        for payload in (b"", self.bytes[:-1], self.bytes + b"extra",
                        struct.pack(">Q", (1 << 64) - 1) + self.bytes[8:],
                        self.bytes[:8 + len(self.first)] + struct.pack(">Q", 2**63)
                        + self.second):
            with self.subTest(length=len(payload)), patch.object(xfb, "rgb24") as convert:
                self.assert_refused_unchanged("E_LENGTH", payload=payload)
                convert.assert_not_called()

    def test_wrong_either_leg_geometry(self):
        for side in ("left", "right"):
            wrong = handcrafted_xwd(width=1, height=1, pixels=RGB[:3])
            bad = wire(wrong, self.second) if side == "left" else wire(self.first, wrong)
            with self.subTest(side=side):
                self.assert_refused_unchanged("E_GEOMETRY", payload=bad)

    def test_wrong_either_leg_xwd_stride(self):
        # Exact 4-byte undersized row for a 2-pixel width; XWD parser must reject.
        bad = handcrafted_xwd(bytes_per_line=4)
        for side in ("left", "right"):
            corrupted = wire(bad, self.second) if side == "left" else wire(self.first, bad)
            with self.subTest(side=side):
                cause = self.assert_refused_unchanged("E_GEOMETRY", payload=corrupted)
                self.assertIsInstance(cause.__cause__, xfb.XwdError)
                self.assertEqual(cause.__cause__.code, "E_XWD_GEOMETRY")

    def test_native_parser_byte_order_translation_and_cause(self):
        bad = xwd_field(self.first, 7, 2)
        err = self.assert_refused_unchanged("E_WIRE", payload=wire(bad, self.second))
        self.assertIsInstance(err.__cause__, xfb.XwdError)
        self.assertEqual(err.__cause__.code, "E_XWD_BYTE_ORDER")

    def test_native_parser_version_translation_and_cause(self):
        bad = xwd_field(self.second, 1, 9)
        err = self.assert_refused_unchanged("E_WIRE", payload=wire(self.first, bad))
        self.assertIsInstance(err.__cause__, xfb.XwdError)
        self.assertEqual(err.__cause__.code, "E_XWD_VERSION")

    def test_grab_not_released_is_timeout_not_torn(self):
        self.verify_grab = lambda receipt: w8u_pair.VerifiedGrab(100, 180, 220, True, False)
        aud = self.make_auditor()
        err = self.assert_refused_unchanged("E_TIMEOUT", auditor=aud)
        self.assertEqual(err.__cause__.code, "E_TIMEOUT")

    def test_grab_did_not_cover_two_legs(self):
        self.verify_grab = lambda receipt: w8u_pair.VerifiedGrab(100, 180, 220, False, True)
        err = self.assert_refused_unchanged("E_TORN", auditor=self.make_auditor())
        self.assertEqual(err.__cause__.code, "E_TORN")

    def test_expired_grab_is_timeout(self):
        self.verify_grab = lambda receipt: w8u_pair.VerifiedGrab(100, 221, 220, True, True)
        self.assert_refused_unchanged("E_TIMEOUT", auditor=self.make_auditor())

    def test_missing_collection_is_unknown_not_zero(self):
        aud = self.make_auditor(collection=lambda receipt: None)
        self.assert_refused_unchanged("E_PROVENANCE", auditor=aud)
        self.assert_refused_unchanged("E_PROVENANCE", auditor=self.make_auditor(
            collection=lambda receipt: audit.VerifiedCollection(
                self.handle, None, 140, True, True, True)))

    def test_collection_wrong_target_or_wrong_root_is_refused(self):
        other = Handle("xvfb_screen", "other", 3)
        for evidence in (audit.VerifiedCollection(other, 110, 140, True, True, True),
                         audit.VerifiedCollection(self.handle, 110, 140, True, False, True),
                         audit.VerifiedCollection(self.handle, 110, 140, True, True, False),
                         audit.VerifiedCollection(self.handle, 110, 140, False, True, True)):
            with self.subTest(evidence=evidence):
                aud = self.make_auditor(collection=lambda receipt, ev=evidence: ev)
                self.assert_refused_unchanged("E_PROVENANCE", auditor=aud)

    def test_collection_outside_grab_is_refused(self):
        aud = self.make_auditor(collection=lambda receipt: audit.VerifiedCollection(
            self.handle, 90, 140, True, True, True))
        self.assert_refused_unchanged("E_PROVENANCE", auditor=aud)

    def test_collection_skew_owner_limit(self):
        self.assert_refused_unchanged("E_SKEW", auditor=self.make_auditor(max_skew=29))
        ok = self.accept(auditor=self.make_auditor(max_skew=30))
        self.assertEqual(ok.identity.frame_seq, 1)

    def test_cancel_before_publication_keeps_quota(self):
        aud = self.make_auditor(check_cancelled=lambda: True)
        self.assert_refused_unchanged("E_TIMEOUT", auditor=aud)

    def test_forged_sender_flag_cannot_attest_grab(self):
        self.verify_grab = lambda receipt: True
        untrusted = self.make_auditor()
        err = self.assert_refused_unchanged("E_WIRE", auditor=untrusted)
        self.assertIsInstance(err.__cause__, xfb.XwdError)
        self.assertEqual(err.__cause__.code, "E_WIRE")

    def test_valid_previous_frame_unchanged_after_rejected_second(self):
        first = self.accept()
        with self.assertRaises(audit.PairAuditRefusal) as cm:
            self.accept(advertised="a" * 64)
        self.assertEqual(cm.exception.code, "E_HASH")
        self.assertEqual(self.auditor.next_sequence, 2)
        self.assertEqual(self.auditor.frame_count, 1)
        self.assertEqual(self.auditor.ack_credits, 1)
        self.assertEqual(self.bank.reserved_bytes, self.spec.quota_bytes)
        self.assertEqual(self.bank.read(self.auditor.token).frame_seq, 1)
        self.assertEqual(self.auditor.check_bank_frame(
            self.bank.read(self.auditor.token), first), first.identity)
        next_frame = self.accept()
        self.assertEqual(next_frame.identity.frame_seq, 2)

    def test_full_structural_binding_not_timestamp_join(self):
        initial = self.accept()
        altered_run = self.make_auditor(run_id="test-run-02", bank=WindowStreamBank(
            quota_bytes=self.spec.quota_bytes))
        other_run = self.accept(auditor=altered_run)
        self.assertNotEqual(initial.identity.binding_sha256,
                            other_run.identity.binding_sha256)
        other_handle = Handle("xvfb_screen", "root/screen0", 4)
        key_a = audit._binding_sha256("test-run-01", StreamToken(self.handle, 0),
                                      1, W, H, RGB_SHA)
        key_b = audit._binding_sha256("test-run-01", StreamToken(other_handle, 0),
                                      1, W, H, RGB_SHA)
        key_c = audit._binding_sha256("test-run-01", StreamToken(self.handle, 1),
                                      1, W, H, RGB_SHA)
        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)
        self.assertEqual(len(initial.identity.binding_sha256), 64)

    def test_frame_identity_rejects_bool_zero_bad_sha(self):
        for seq, width, height, digest, binding in (
                (True, W, H, RGB_SHA, "0" * 64),
                (0, W, H, RGB_SHA, "0" * 64),
                (1, True, H, RGB_SHA, "0" * 64),
                (1, W, 0, RGB_SHA, "0" * 64)):
            with self.subTest(seq=seq, width=width, height=height):
                with self.assertRaises(audit.PairAuditRefusal) as cm:
                    audit.FrameIdentity(seq, width, height, digest, binding)
                self.assertEqual(cm.exception.code, "E_BINDING")
        for invalid_digest in ("A" * 64, "a" * 63, True):
            with self.subTest(digest=invalid_digest):
                with self.assertRaises(audit.PairAuditRefusal) as cm:
                    audit.FrameIdentity(1, W, H, invalid_digest, "0" * 64)
                self.assertEqual(cm.exception.code, "E_HASH")
        valid = audit.FrameIdentity(1, W, H, RGB_SHA, "0" * 64)
        self.assertEqual(valid.frame_seq, 1)

    def test_run_id_cannot_escape_ram_root(self):
        self.assertEqual(audit.expected_run_ram_root("test-run-01"),
                         "/dev/shm/vqa-ramws/opensuperficies/os-cu-0/test-run-01")
        for val in ("../x", "..", "a..b", "", "/dev/shm/", True, "x" * 65):
            with self.subTest(run_id=val), self.assertRaises(audit.PairAuditRefusal) as cm:
                audit.expected_run_ram_root(val)
            self.assertEqual(cm.exception.code, "E_BINDING")

    def test_check_bank_frame_rejects_drop_wrong_epoch_and_changed_pixel(self):
        admitted = self.accept()
        ref = self.bank.read(self.auditor.token)
        for bad in (Drop(ref.handle, ref.epoch, None, "absent"),
                    replace(ref, epoch=1), replace(ref, digest="0" * 64)):
            with self.subTest(bad=bad), self.assertRaises(audit.PairAuditRefusal) as cm:
                self.auditor.check_bank_frame(bad, admitted)
            self.assertEqual(cm.exception.code, "E_BINDING")
        altered = replace(ref, rgb=np.zeros((H, W, 3), dtype=np.uint8))
        with self.assertRaises(audit.PairAuditRefusal) as cm:
            self.auditor.check_bank_frame(altered, admitted)
        self.assertEqual(cm.exception.code, "E_TORN")

    def test_cannot_forge_owner_admission_receipt(self):
        original = self.accept()
        fake = replace(original)  # equal fields, but never admitted by this owner
        ref = self.bank.read(self.auditor.token)
        with self.assertRaises(audit.PairAuditRefusal) as cm:
            self.auditor.check_bank_frame(ref, fake)
        self.assertEqual(cm.exception.code, "E_BINDING")
        other = self.make_auditor(bank=WindowStreamBank(
            quota_bytes=self.spec.quota_bytes))
        with self.assertRaises(audit.PairAuditRefusal) as cm:
            other.check_bank_frame(ref, original)
        self.assertEqual(cm.exception.code, "E_BINDING")
        self.assertEqual(self.auditor.check_bank_frame(ref, original), original.identity)

    def test_bank_quota_refusal_is_typed_and_does_not_create_frame_credit(self):
        other_bank = WindowStreamBank(quota_bytes=self.spec.quota_bytes - 1)
        auditor = self.make_auditor(bank=other_bank)
        with self.assertRaises(audit.PairAuditRefusal) as cm:
            self.accept(auditor=auditor)
        self.assertEqual(cm.exception.code, "E_BANK")
        self.assertEqual((other_bank.reserved_bytes, auditor.frame_count,
                          auditor.ack_credits, auditor.next_sequence), (0, 0, 0, 1))

    def test_two_threads_sequential_identity_allocation(self):
        barrier = threading.Barrier(3)
        returned, faults = [], []
        lock = threading.Lock()
        def worker():
            barrier.wait()
            try:
                result = self.accept()
                with lock:
                    returned.append(result)
            except Exception as exc:
                with lock:
                    faults.append(exc)
        t1, t2 = threading.Thread(target=worker), threading.Thread(target=worker)
        t1.start(); t2.start(); barrier.wait(); t1.join(); t2.join()
        self.assertEqual(faults, [])
        self.assertEqual(sorted(x.identity.frame_seq for x in returned), [1, 2])
        self.assertEqual((self.auditor.frame_count, self.auditor.ack_credits), (2, 2))
        self.assertEqual(self.bank.reserved_bytes, self.spec.quota_bytes)


class TestSemanticMutants(unittest.TestCase):
    """Fresh behavior instances, healthy proof, semantic patch, restoration, reproof."""

    def test_fresh_behavior_suite_and_mutation_assertion_kills(self):
        loader = unittest.TestLoader()
        def execute():
            suite = loader.loadTestsFromTestCase(TestPairAuditBehavior)
            result = unittest.TestResult()
            suite.run(result)
            return result
        clean = execute()
        self.assertGreater(clean.testsRun, 0)
        self.assertEqual(clean.errors, [])
        self.assertEqual(clean.failures, [])
        self.assertEqual(clean.skipped, [])
        self.assertEqual(clean.expectedFailures, [])
        self.assertEqual(clean.unexpectedSuccesses, [])
        original = audit._binding_sha256
        mutant_variants = (
            ("drop_collection_gate", "_require_collection",
             lambda value, grab, *, target_handle, max_skew_ns: None),
            ("corrupt_source_code_translation", "_translated_xwd_code", lambda code: "E_WIRE"),
            ("omit_owner_binding", "_binding_sha256",
             lambda run_id, token, seq, width, height, digest:
                 original("fixed-run-id", token, seq, width, height, digest)),
            ("accept_bool_sequence", "_positive",
             lambda value, name, *, zero_ok=False, maximum=None: value),
        )
        killed, surviving, accidental_errors = [], [], []
        for name, target, mutation in mutant_variants:
            with patch.object(audit, target, mutation):
                result = execute()
            if result.errors:
                accidental_errors.append((name, len(result.errors)))
            elif result.failures and all("AssertionError" in tb for _, tb in result.failures):
                killed.append(name)
            else:
                surviving.append(name)
        self.assertEqual(accidental_errors, [])
        restored = execute()
        self.assertGreater(restored.testsRun, 0)
        self.assertEqual((restored.errors, restored.failures, restored.skipped,
                          restored.expectedFailures, restored.unexpectedSuccesses),
                         ([], [], [], [], []))
        self.assertEqual(surviving, [])
        self.assertEqual(len(killed), len(mutant_variants))
        print("SEMANTIC_MUTANTS killed=%d applicable=%d survivors=%s equivalent_exclusions=0" % (
            len(killed), len(mutant_variants), surviving))


if __name__ == "__main__":
    unittest.main()
