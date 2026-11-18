"""Offline frame sequence oracles on real, anonymous file storage."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import ValidationError

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS.parents[1]))

import frames
from frames import FileFrameSequence, FrameSequenceError, SequenceStatus


_BINDING = _TESTS.parents[2] / "schemas/tests/examples/vp.surface_binding_v1.valid.json"
_CANARY = b"private-owner-a-frame-canary"


class FrameSequenceTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=_TESTS)
        self.addCleanup(self.scratch.cleanup)

    def sequence(self, owner="owner-a", *, slots=1, limit=64):
        binding = dict(json.loads(_BINDING.read_bytes()), owner=owner)
        sequence = FileFrameSequence(binding, directory=self.scratch.name,
                                     slots=slots, max_frame_bytes=limit)
        self.addCleanup(sequence.close, owner)
        return sequence

    def test_file_fallback_preserves_frame_order_and_reports_copy_mode(self):
        sequence = self.sequence(slots=2)
        render = b"encoded-render-frame"
        video = b"decoded-video-frame"
        first = sequence.append("owner-a", render)
        second = sequence.append("owner-a", video)

        self.assertEqual((first.index, second.index), (0, 1))
        self.assertEqual(sequence.status("owner-a"), SequenceStatus(128, 0, 0, True))
        self.assertEqual(sequence.status("owner-a").pending_bytes, 128)
        self.assertEqual(os.fstat(sequence._storage.fileno()).st_nlink, 0)
        self.assertEqual(os.listdir(self.scratch.name), [])
        for info, expected in ((first, render), (second, video)):
            self.assertEqual(info.copy, "file")
            self.assertEqual(info.sha256, sha256(expected).hexdigest())
            with sequence.borrow("owner-a", info.index) as borrow:
                self.assertEqual(borrow.frame, info)
                self.assertEqual(borrow.read(), expected)

    def test_bad_binding_refused_before_allocating_storage(self):
        with patch.object(frames, "TemporaryFile") as allocate:
            with self.assertRaises(ValidationError):
                FileFrameSequence({"owner": "owner-a"}, directory=self.scratch.name,
                                  slots=1, max_frame_bytes=64)
            allocate.assert_not_called()

    def foreign_canary(self, sequence, index, output):
        with self.assertRaisesRegex(FrameSequenceError, "owner mismatch"):
            with sequence.borrow("owner-b", index) as borrow:
                output.append(borrow.read())
        self.assertEqual(output, [])

    def test_owner_isolation_refuses_foreign_reads_and_mutations(self):
        a = self.sequence()
        b = self.sequence("owner-b")
        first = a.append("owner-a", _CANARY)
        other = b.append("owner-b", b"owner-b-frame")
        self.assertEqual(first.index, other.index)
        self.foreign_canary(a, first.index, [])
        for operation in (lambda: a.append("owner-b", b"injected"),
                          lambda: a.close("owner-b"), lambda: a.status("owner-b")):
            with self.assertRaisesRegex(FrameSequenceError, "owner mismatch"):
                operation()
        with b.borrow("owner-b", other.index) as borrow:
            self.assertEqual(borrow.read(), b"owner-b-frame")
        with a.borrow("owner-a", first.index) as borrow:
            self.assertEqual(borrow.read(), _CANARY)

    def assert_drain_refused(self, sequence, borrow):
        self.assertFalse(sequence.close("owner-a"), "charge settled before borrow drained")
        self.assertEqual(sequence.status("owner-a"), SequenceStatus(64, 64, 1, False))
        self.assertEqual(borrow.read(), _CANARY)

    def test_drain_fences_new_work_and_keeps_charge_until_borrow_exits(self):
        sequence = self.sequence()
        info = sequence.append("owner-a", _CANARY)
        with sequence.borrow("owner-a", info.index) as borrow:
            self.assert_drain_refused(sequence, borrow)
            with self.assertRaisesRegex(FrameSequenceError, "closed to new work"):
                sequence.append("owner-a", b"late")
            with self.assertRaisesRegex(FrameSequenceError, "closed to new work"):
                with sequence.borrow("owner-a", info.index):
                    self.fail("accepted a new borrow during drain")
        with self.assertRaisesRegex(FrameSequenceError, "borrow has ended"):
            borrow.read()
        self.assertTrue(sequence.close("owner-a"))
        self.assertEqual(sequence.status("owner-a"), SequenceStatus(0, 0, 0, False))
        self.assertEqual(sequence.status("owner-a").pending_bytes, 0)
        self.assertTrue(sequence.close("owner-a"))

    def test_wrap_waits_for_every_borrow_and_refuses_the_retired_index(self):
        sequence = self.sequence()
        frame = sequence.append("owner-a", _CANARY)
        with ThreadPoolExecutor(max_workers=1) as worker:
            with sequence.borrow("owner-a", frame.index) as first:
                with sequence.borrow("owner-a", frame.index) as second:
                    self.assertEqual(second.read(), _CANARY)
                    self.assertEqual(sequence.status("owner-a").outstanding_borrows, 2)
                append = worker.submit(sequence.append, "owner-a", b"new")
                with self.assertRaisesRegex(FrameSequenceError, "outstanding borrows"):
                    append.result(timeout=5)
                self.assertEqual(first.read(), _CANARY)
        next_frame = sequence.append("owner-a", b"new")
        self.assertEqual(next_frame.index, frame.index + 1)
        with self.assertRaisesRegex(FrameSequenceError, "not published"):
            with sequence.borrow("owner-a", frame.index):
                self.fail("reused a retired index")

    def assert_scrubbed_tail(self, sequence):
        fd = sequence._storage.fileno()
        old = sequence.append("owner-a", _CANARY)
        new = sequence.append("owner-a", b"x")
        self.assertEqual(new.index, old.index + 1)
        self.assertEqual(os.pread(fd, len(_CANARY), 0), b"x" + bytes(len(_CANARY) - 1))

    def test_scrub_before_recycle_and_before_release(self):
        sequence = self.sequence()
        self.assert_scrubbed_tail(sequence)
        fd = os.dup(sequence._storage.fileno())
        self.addCleanup(os.close, fd)
        self.assertTrue(sequence.close("owner-a"))
        self.assertEqual(os.pread(fd, len(_CANARY), 0), bytes(len(_CANARY)))
        self.assertEqual(sequence.status("owner-a").reserved_bytes, 0)

    def test_close_scrubs_written_regions_without_materializing_sparse_gaps(self):
        slot_size = 256 * 1024
        sequence = self.sequence(slots=2, limit=slot_size)
        sequence.append("owner-a", b"first")
        sequence.append("owner-a", b"second")
        fd = sequence._storage.fileno()
        with patch.object(frames, "_write", wraps=frames._write) as write:
            self.assertTrue(sequence.close("owner-a"))
        self.assertEqual([call.args for call in write.call_args_list],
                         [(fd, bytes(5), 0), (fd, bytes(6), slot_size)])

    def test_failed_scrub_readback_quarantines_and_keeps_reservation(self):
        sequence = self.sequence()
        sequence.append("owner-a", _CANARY)
        with patch.object(frames, "_write", return_value=None):
            with self.assertRaisesRegex(FrameSequenceError, "scrub readback failed"):
                sequence.append("owner-a", b"new")
            with self.assertRaisesRegex(FrameSequenceError, "scrub readback failed"):
                sequence.close("owner-a")
        self.assertEqual(sequence.status("owner-a"), SequenceStatus(64, 64, 0, False))
        self.assertTrue(sequence.close("owner-a"))
        self.assertEqual(sequence.status("owner-a").reserved_bytes, 0)

    def test_partial_write_is_fenced_and_scrubbed_at_close(self):
        sequence = self.sequence()
        storage_fd = sequence._storage.fileno()
        original_pwrite = os.pwrite

        def short_write(fd, data, offset):
            self.assertEqual(fd, storage_fd)
            return original_pwrite(fd, data[:3], offset)

        with patch.object(frames.os, "pwrite", side_effect=short_write):
            with self.assertRaisesRegex(FrameSequenceError, "short frame storage write"):
                sequence.append("owner-a", _CANARY)
        self.assertEqual(sequence.status("owner-a"), SequenceStatus(64, 64, 0, False))
        with self.assertRaisesRegex(FrameSequenceError, "closed to new work"):
            with sequence.borrow("owner-a", 0):
                self.fail("borrowed a partially written frame")
        fd = os.dup(storage_fd)
        self.addCleanup(os.close, fd)
        self.assertTrue(sequence.close("owner-a"))
        self.assertEqual(os.pread(fd, 3, 0), bytes(3))

    def test_oversize_is_local_refusal_and_valid_file_sequence_still_works(self):
        sequence = self.sequence(limit=4)
        first = sequence.append("owner-a", b"abcd")
        with self.assertRaisesRegex(FrameSequenceError, "within the slot limit") as refusal:
            sequence.append("owner-a", b"abcde")
        self.assertNotIn("BUDGET_EXCEEDED", str(refusal.exception))
        with sequence.borrow("owner-a", first.index) as borrow:
            self.assertEqual(borrow.read(), b"abcd")
        self.assertEqual(sequence.append("owner-a", b"e").index, first.index + 1)

    def test_borrow_refuses_bytes_that_no_longer_match_the_published_digest(self):
        sequence = self.sequence()
        info = sequence.append("owner-a", _CANARY)
        os.pwrite(sequence._storage.fileno(), b"?", 0)
        with sequence.borrow("owner-a", info.index) as borrow:
            with self.assertRaisesRegex(FrameSequenceError, "no longer matches publication"):
                borrow.read()

    def test_mutant_bypass_owner_check_leaks_the_canary(self):
        sequence = self.sequence()
        info = sequence.append("owner-a", _CANARY)
        output = []
        with patch.object(sequence, "_require_owner", return_value=None) as bypass:
            with self.assertRaises(AssertionError):
                self.foreign_canary(sequence, info.index, output)
            bypass.assert_called_once_with("owner-b")
        self.assertEqual(output, [_CANARY])

    def test_mutant_release_before_drain_fails_the_charge_oracle(self):
        sequence = self.sequence()
        info = sequence.append("owner-a", _CANARY)
        with sequence.borrow("owner-a", info.index) as borrow:
            with patch.object(sequence, "_borrow_count", return_value=0) as forget:
                with self.assertRaisesRegex(AssertionError, "charge settled before borrow drained"):
                    self.assert_drain_refused(sequence, borrow)
                forget.assert_called_once_with()
            self.assertEqual(sequence.status("owner-a").reserved_bytes, 0)

    def test_mutant_recycle_without_scrub_leaves_canary_tail(self):
        sequence = self.sequence()
        with patch.object(frames, "_scrub", return_value=None) as omit:
            with self.assertRaises(AssertionError):
                self.assert_scrubbed_tail(sequence)
            omit.assert_called_once_with(sequence._storage.fileno(), 0, len(_CANARY))
        self.assertEqual(os.pread(sequence._storage.fileno(), len(_CANARY) - 1, 1),
                         _CANARY[1:])


if __name__ == "__main__":
    unittest.main()
