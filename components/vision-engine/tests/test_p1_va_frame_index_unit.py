"""Independent-byte, real-bank positive and typed negative frame-index controls.

Runs offline in RAM. Only the production WindowStreamBank, FrameIndex, validators
and accounting implement the tested behavior; no digest or frame source is faked.
"""

from dataclasses import fields, replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import unittest

import numpy as np

from vadapt.rtring import ring_bytes
from vadapt.window_stream_bank import (Drop, FrameRef, Handle, StreamSpec,
                                      WindowStreamBank)
from vadapt.proto1_frame_index import (FrameIdentity, FrameIndex, FrameIndexError,
                                       Session, compute_binding_sha256)


# Distinct, independent handwritten byte oracles, not an encoder round trip.
BYTES_1 = b"\x00\x01\x02\x03\x04\x05"
BYTES_2 = b"\xff\x10\x20\x30\x40\x50"
BYTES_3 = b"\x90\x81\x72\x63\x54\x45"
ORACLE_1 = hashlib.sha256(BYTES_1).hexdigest()
ORACLE_2 = hashlib.sha256(BYTES_2).hexdigest()


class FrameIndexContractTests(unittest.TestCase):
    def setUp(self):
        self.handle = Handle("cdp_tab", "target-01", 1)
        self.charge = ring_bytes(3, 2, 1)
        self.spec = StreamSpec(2, 1, 3, self.charge)
        self.bank = WindowStreamBank(quota_bytes=self.charge)
        self.token = self.bank.open(self.handle, self.spec)
        self.addCleanup(lambda: self.bank.close(self.token))
        self.session = Session("run-01", self.handle, self.token.epoch)
        self.index = FrameIndex(self.session, capacity=2)

    def _publish(self, seq=1, data=BYTES_1):
        # Native publish/read both execute, including seqlock and CRC validation.
        array = np.frombuffer(data, dtype=np.uint8).reshape(1, 2, 3).copy()
        self.assertIsNone(self.bank.publish(self.token, array, frame_seq=seq))
        frame = self.bank.read(self.token)
        self.assertIsInstance(frame, FrameRef)
        self.assertEqual(frame.frame_seq, seq)
        self.assertFalse(frame.rgb.flags.writeable)
        self.assertEqual(frame.rgb.tobytes(), data)
        self.assertEqual(frame.digest, hashlib.sha256(data).hexdigest())
        return frame

    def _assert_refusal(self, code, operation, *, index=None):
        idx = self.index if index is None else index
        before = (idx.size, idx.outstanding, self.bank.reserved_bytes)
        with self.assertRaises(FrameIndexError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual((idx.size, idx.outstanding, self.bank.reserved_bytes), before)

    def _admit_first(self):
        frame = self._publish()
        return frame, self.index.admit(frame)

    def test_positive_real_bank_and_independent_oracle_and_exact_lookup(self):
        first, id1 = self._admit_first()
        self.assertEqual(id1.rgb24_sha256, ORACLE_1)
        self.assertEqual(self.index.lookup(self.session, id1), id1)
        self.assertEqual(tuple(f.name for f in fields(FrameIdentity)),
                         ("frame_seq", "width", "height", "rgb24_sha256", "binding_sha256"))
        self.assertEqual((id1.frame_seq, id1.width, id1.height), (1, 2, 1))
        # Binding oracle authored here independently of the production builder.
        binding_json = '["run-01","cdp_tab","target-01",1,0,1,2,1,"' + ORACLE_1 + '"]'
        self.assertEqual(id1.binding_sha256,
                         hashlib.sha256(binding_json.encode("utf-8")).hexdigest())
        self.assertEqual(compute_binding_sha256(self.session, 1, 2, 1, ORACLE_1),
                         id1.binding_sha256)
        self.assertNotIn("rgb", FrameIdentity.__slots__)
        self.assertNotIn("pixels", FrameIdentity.__slots__)
        self.assertEqual(self.index.size, 1)
        second = self._publish(2, BYTES_2)
        id2 = self.index.admit(second)
        self.assertEqual(id2.rgb24_sha256, ORACLE_2)
        self.assertEqual(self.index.lookup(self.session, id1), id1)
        self.assertEqual(self.index.lookup(self.session, id2), id2)
        self.assertNotEqual(id1.binding_sha256, id2.binding_sha256)
        self.assertEqual(self.index.size, 2)
        self.assertEqual(self.bank.reserved_bytes, self.charge)
        self.assertEqual(first.rgb.tobytes(), BYTES_1)

    def test_missing_digest_and_mismatch_must_fail(self):
        frame = self._publish()
        self._assert_refusal("E_FRAME_DIGEST_MISSING",
                             lambda: self.index.admit(replace(frame, digest="")))
        self._assert_refusal("E_FRAME_DIGEST_MISMATCH",
                             lambda: self.index.admit(replace(frame, digest="f" * 64)))
        self._assert_refusal("E_FRAME_DIGEST_FORMAT",
                             lambda: self.index.admit(replace(frame, digest="F" * 64)))
        self.assertEqual(self.index.admit(frame).rgb24_sha256, ORACLE_1)

    def test_conflicting_and_identical_sequence_reuse_must_fail(self):
        frame, first = self._admit_first()
        other_bytes = np.frombuffer(BYTES_2, dtype=np.uint8).reshape(1, 2, 3).copy()
        conflict = replace(frame, rgb=other_bytes, digest=ORACLE_2)
        self._assert_refusal("E_FRAME_SEQUENCE_CONFLICT", lambda: self.index.admit(conflict))
        self._assert_refusal("E_FRAME_SEQUENCE_REUSE", lambda: self.index.admit(frame))
        self.assertEqual(self.index.lookup(self.session, first), first)

    def test_sequence_must_start_one_and_not_skip_and_bool_refused(self):
        frame = self._publish(2, BYTES_2)
        self._assert_refusal("E_FRAME_SEQUENCE", lambda: self.index.admit(frame))
        self._assert_refusal("E_FRAME_INTEGER",
                             lambda: self.index.admit(replace(frame, frame_seq=True)))
        self.assertEqual(self.index.size, 0)

    def test_capacity_never_silently_evicts(self):
        self.index = FrameIndex(self.session, capacity=1)
        first, id1 = self._admit_first()
        second = self._publish(2, BYTES_2)
        self._assert_refusal("E_FRAME_CAPACITY", lambda: self.index.admit(second))
        self.assertEqual(self.index.lookup(self.session, id1), id1)
        self.assertEqual(self.index.size, 1)
        self.assertEqual(self.bank.reserved_bytes, self.charge)
        self.assertEqual(first.frame_seq, 1)

    def test_cross_run_generation_kind_and_epoch_lookup_must_fail(self):
        _, identity = self._admit_first()
        other_run = Session("run-02", self.handle, self.token.epoch)
        other_generation = Session("run-01", Handle("cdp_tab", "target-01", 2), 0)
        other_kind = Session("run-01", Handle("x11_window", "target-01", 1), 0)
        other_epoch = Session("run-01", self.handle, 1)
        self._assert_refusal("E_FRAME_FOREIGN_BINDING",
                             lambda: self.index.lookup(other_run, identity))
        self._assert_refusal("E_FRAME_FOREIGN_BINDING",
                             lambda: self.index.lookup(other_generation, identity))
        self._assert_refusal("E_FRAME_FOREIGN_BINDING",
                             lambda: self.index.lookup(other_kind, identity))
        self._assert_refusal("E_FRAME_STALE_EPOCH",
                             lambda: self.index.lookup(other_epoch, identity))
        self.assertEqual(self.index.lookup(self.session, identity), identity)

    def test_foreign_binding_does_not_authorize_digest_only(self):
        frame, identity = self._admit_first()
        foreign = Session("run-02", self.handle, 0)
        binding_for_other_run = compute_binding_sha256(foreign, 1, 2, 1, ORACLE_1)
        self.assertNotEqual(identity.binding_sha256, binding_for_other_run)
        forged = replace(identity, binding_sha256=binding_for_other_run)
        self._assert_refusal("E_FRAME_FOREIGN_BINDING",
                             lambda: self.index.lookup(self.session, forged))
        self.assertEqual(frame.digest, identity.rgb24_sha256)

    def test_foreign_frame_source_and_stale_frame_epoch(self):
        frame = self._publish()
        wrong_kind = replace(frame, handle=Handle("x11_window", "target-01", 1))
        wrong_gen = replace(frame, handle=Handle("cdp_tab", "target-01", 2))
        wrong_epoch = replace(frame, epoch=1)
        self._assert_refusal("E_FRAME_FOREIGN_BINDING", lambda: self.index.admit(wrong_kind))
        self._assert_refusal("E_FRAME_FOREIGN_BINDING", lambda: self.index.admit(wrong_gen))
        self._assert_refusal("E_FRAME_STALE_EPOCH", lambda: self.index.admit(wrong_epoch))
        self.assertEqual(self.index.admit(frame).rgb24_sha256, ORACLE_1)

    def test_input_mutation_cannot_modify_admitted_record(self):
        frame = self._publish()
        mutable_bytes = frame.rgb.copy()
        caller_frame = replace(frame, rgb=mutable_bytes)
        identity = self.index.admit(caller_frame)
        before = identity
        mutable_bytes[0, 0, 0] = 200
        object.__setattr__(caller_frame, "digest", ORACLE_2)
        self.assertEqual(self.index.lookup(self.session, before), before)
        self.assertEqual(self.index.lookup(self.session, before).rgb24_sha256, ORACLE_1)
        self.assertNotEqual(mutable_bytes.tobytes(), BYTES_1)

    def test_returned_identity_and_session_mutation_cannot_change_index(self):
        _, caller_identity = self._admit_first()
        original = FrameIdentity(1, 2, 1, ORACLE_1, caller_identity.binding_sha256)
        object.__setattr__(caller_identity, "rgb24_sha256", ORACLE_2)
        self.assertEqual(self.index.lookup(self.session, original), original)
        returned = self.index.lookup(self.session, original)
        object.__setattr__(returned, "binding_sha256", "e" * 64)
        self.assertEqual(self.index.lookup(self.session, original), original)
        exposed_session = self.index.session
        object.__setattr__(exposed_session, "run_id", "altered")
        self.assertEqual(self.index.session.run_id, "run-01")
        self.assertEqual(self.index.lookup(self.session, original), original)

    def test_structural_session_detaches_mutable_handle(self):
        class MutableHandle:
            kind = "cdp_tab"
            canonical_id = "target-01"
            generation = 1
        source = MutableHandle()
        session = Session("run-01", source, 0)
        source.kind, source.canonical_id, source.generation = "x11_window", "change", 17
        self.assertEqual(session.target_handle, self.handle)
        self.assertEqual(self.index.session, session)
        self.assertEqual(self.index.session.target_handle, self.handle)

    def test_retirement_blocks_outstanding_pins_and_preserves_evidence(self):
        _, identity = self._admit_first()
        self.assertEqual(self.index.pin(self.session, identity), identity)
        self.assertEqual(self.index.pin(self.session, identity), identity)
        self.assertEqual(self.index.outstanding, 2)
        self._assert_refusal("E_FRAME_OUTSTANDING", lambda: self.index.retire(self.session))
        self.assertEqual(self.index.lookup(self.session, identity), identity)
        self.index.unpin(self.session, identity)
        self._assert_refusal("E_FRAME_OUTSTANDING", lambda: self.index.retire(self.session))
        self.index.unpin(self.session, identity)
        self.assertEqual(self.index.retire(self.session), 1)
        self.assertEqual(self.index.size, 0)
        self._assert_refusal("E_FRAME_RETIRED", lambda: self.index.lookup(self.session, identity))
        self._assert_refusal("E_FRAME_RETIRED", lambda: self.index.retire(self.session))
        self.assertEqual(self.bank.reserved_bytes, self.charge)

    def test_generation_advance_never_revives_retired_session(self):
        _, old = self._admit_first()
        self.assertEqual(self.index.retire(self.session), 1)
        same = Session("run-01", self.handle, 0)
        self._assert_refusal("E_FRAME_STALE_EPOCH", lambda: self.index.advance(same))
        next_session = Session("run-01", Handle("cdp_tab", "target-01", 2), 0)
        self.index.advance(next_session)
        self._assert_refusal("E_FRAME_FOREIGN_BINDING",
                             lambda: self.index.lookup(self.session, old))
        self._assert_refusal("E_FRAME_FOREIGN_BINDING",
                             lambda: self.index.lookup(next_session, old))
        # New independent native stream incarnation with sequence reset.
        self.assertIsNone(self.bank.close(self.token))
        newer = self.bank.open(next_session.target_handle, self.spec)
        self.token = newer
        new_data = np.frombuffer(BYTES_1, dtype=np.uint8).reshape(1, 2, 3).copy()
        self.bank.publish(newer, new_data, frame_seq=1)
        new_frame = self.bank.read(newer)
        self.assertIsInstance(new_frame, FrameRef)
        self.assertNotEqual(self.index.admit(new_frame).binding_sha256, old.binding_sha256)
        self.assertEqual(self.index.size, 1)

    def test_epoch_advance_and_backward_generation_refused(self):
        _, old = self._admit_first()
        self.index.retire(self.session)
        newer = Session("run-01", self.handle, 1)
        self.index.advance(newer)
        self._assert_refusal("E_FRAME_NOT_RETIRED", lambda: self.index.advance(newer))
        self._assert_refusal("E_FRAME_STALE_EPOCH", lambda: self.index.lookup(self.session, old))
        # Transition back after next retirement is forbidden.
        self.assertEqual(self.index.retire(newer), 0)
        self._assert_refusal("E_FRAME_STALE_EPOCH", lambda: self.index.advance(self.session))

    def test_deterministic_retirement_lookup_barrier(self):
        _, old = self._admit_first()
        start = threading.Barrier(2)
        retired = threading.Barrier(2)
        events = []

        def retire_between_lookups():
            start.wait(timeout=5)
            events.append(("retired_count", self.index.retire(self.session)))
            retired.wait(timeout=5)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(retire_between_lookups)
            self.assertEqual(self.index.lookup(self.session, old), old)
            start.wait(timeout=5)
            retired.wait(timeout=5)
            future.result(timeout=5)
        self.assertEqual(events, [("retired_count", 1)])
        self._assert_refusal("E_FRAME_RETIRED", lambda: self.index.lookup(self.session, old))
        self.index.advance(Session("run-01", Handle("cdp_tab", "target-01", 2), 0))
        self._assert_refusal("E_FRAME_FOREIGN_BINDING",
                             lambda: self.index.lookup(self.session, old))

    def test_pin_race_retirement_never_drops_outstanding_obligation(self):
        _, old = self._admit_first()
        pinned = threading.Barrier(2)
        released = threading.Barrier(2)

        def hold_reference():
            self.index.pin(self.session, old)
            pinned.wait(timeout=5)
            released.wait(timeout=5)
            self.index.unpin(self.session, old)

        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(hold_reference)
            pinned.wait(timeout=5)
            self._assert_refusal("E_FRAME_OUTSTANDING", lambda: self.index.retire(self.session))
            released.wait(timeout=5)
            worker.result(timeout=5)
        self.assertEqual(self.index.outstanding, 0)
        self.assertEqual(self.index.retire(self.session), 1)

    def test_drop_never_admits_a_receipt(self):
        self._assert_refusal("E_FRAME_UNAVAILABLE",
                             lambda: self.index.admit(Drop(self.handle, 0, None, "absent")))
        self.assertEqual(self.index.size, 0)

    def test_strict_frame_identity_refuses_missing_and_bool_fields(self):
        with self.assertRaises(FrameIndexError) as b:
            FrameIdentity(True, 2, 1, ORACLE_1, "0" * 64)
        self.assertEqual(b.exception.code, "E_FRAME_INTEGER")
        with self.assertRaises(FrameIndexError) as m:
            FrameIdentity(1, 2, 1, None, "0" * 64)
        self.assertEqual(m.exception.code, "E_FRAME_DIGEST_MISSING")
        with self.assertRaises(FrameIndexError) as d:
            FrameIdentity(1, 2, 1, ORACLE_1, "A" * 64)
        self.assertEqual(d.exception.code, "E_FRAME_DIGEST_FORMAT")
        with self.assertRaises(FrameIndexError) as c:
            FrameIndex(self.session, capacity=True)
        self.assertEqual(c.exception.code, "E_FRAME_INTEGER")

    def test_invalid_run_id_cannot_claim_root_or_residency(self):
        for run_id in ("..", ".", "../escape", "aa/../bb", "/tmp", "", "a\\b", "a..b"):
            with self.subTest(run_id=run_id):
                with self.assertRaises(FrameIndexError) as found:
                    Session(run_id, self.handle, 0)
                self.assertEqual(found.exception.code, "E_FRAME_RUN_ID")
        self.assertEqual(self.index.session.run_id, "run-01")

    def test_reference_unpin_without_pin_refused(self):
        _, identity = self._admit_first()
        self._assert_refusal("E_FRAME_NOT_PINNED",
                             lambda: self.index.unpin(self.session, identity))


if __name__ == "__main__":
    unittest.main()
