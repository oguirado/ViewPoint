"""Tiny RAM-backed Python observation models for stale slot headers (F-02).

These scripted loads do not prove CPU memory ordering or acquire/release fences.
"""

import pathlib
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

from vadapt import ramws, rtring


class StructWrapper:
    """Script selected header reads without replacing the real mmap buffer."""

    def __init__(self, real_struct, reads):
        self._real, self._reads = real_struct, reads

    def unpack_from(self, buf, offset=0):
        value = self._real.unpack_from(buf, offset)
        return self._reads.observe(self._real.format, buf, offset, value)

    def __getattr__(self, attr):
        return getattr(self._real, attr)


class HeaderReads:
    """Finite scripts fall back to the actual header; all views stay zero-copy."""

    def __init__(self, reader, slot, seq=(), index=(), headers=()):
        self.mm, self.off = reader._mm, rtring.HDR + slot * rtring.SLOT_HDR
        self.seq, self.index, self.headers = iter(seq), iter(index), iter(headers)
        self.seq_reads = self.index_reads = self.header_reads = 0

    def observe(self, fmt, buf, offset, value):
        if buf is self.mm:
            if fmt == "<Q" and offset == self.off:
                self.seq_reads += 1
                return (next(self.seq, value[0]),)
            if fmt == "<Q" and offset == self.off + 8:
                self.index_reads += 1
                return (next(self.index, value[0]),)
            if fmt == "<QQ" and offset == self.off:
                self.header_reads += 1
                return next(self.headers, value)
        return value

    def __enter__(self):
        self.patch = mock.patch.multiple(
            rtring, _Q=StructWrapper(rtring._Q, self),
            _QQ=StructWrapper(rtring._QQ, self),
        )
        self.patch.__enter__()
        return self

    def __exit__(self, *args):
        return self.patch.__exit__(*args)


class TestAdvRtringStaleSeq(unittest.TestCase):
    def setUp(self):
        if not ramws.is_ram_fs("/dev/shm"):
            self.skipTest("verified RAM storage unavailable on /dev/shm")
        tmp = tempfile.TemporaryDirectory(prefix="fx2-f7-ring-", dir="/dev/shm")
        self.addCleanup(tmp.cleanup)
        self.ram_root = pathlib.Path(tmp.name)
        self.ws = ramws.open_run(
            "c", "r", 1 << 20, tool="fx2_f7_ring", root=self.ram_root,
            statvfs=lambda _: types.SimpleNamespace(
                f_bavail=ramws.FREE_MIN + (1 << 20), f_frsize=1),
        )
        self.addCleanup(self.ws.close)
        self.rings = 0

    def ring(self, *, slots=2, seq0=0, verify=True):
        self.rings += 1
        wr = rtring.RingWriter(
            self.ws, slots, 8, 8, rel="ring%d/frames.rgb24" % self.rings,
            _seq0=seq0,
        )
        self.addCleanup(wr.close)
        rd = rtring.RingReader(wr.path, root=self.ram_root, verify=verify)
        self.addCleanup(rd.close)
        return wr, rd

    def test_f02_stale_second_seq_load_reordering_lapped(self):
        """Two stale scalar seq loads cannot justify an incoherent ri > r."""
        wr, rd = self.ring()
        wr.publish(np.full((8, 8, 3), 10, np.uint8), 0)
        stale = wr._seq(0)  # Published frame 0 has seq=2, not the initial 0.
        wr._begin(2)  # Real header is now (seq=3, ri=2), still in flight.
        with HeaderReads(rd, 0, seq=(stale, stale)) as reads:
            f, status = rd._try(0, retries=4)
        self.assertIsNone(f)
        self.assertEqual(status, "torn")
        self.assertGreater(reads.seq_reads, 2)
        self.assertEqual((rd.last, rd.gaps, rd.overrun, rd.torn), (-1, [], [], []))

    def test_f02_stale_second_seq_load_reordering_absent(self):
        """Use an actual ri < r mismatch, not a non-buffer proxy TypeError."""
        wr, rd = self.ring()
        stale = wr._seq(1)
        wr._begin(1)  # Slot 1 has ri=1, below requested r=3 in the same slot.
        with HeaderReads(rd, 1, seq=(stale, stale)) as reads:
            f, status = rd._try(3, retries=4)
        self.assertIsNone(f)
        self.assertEqual(status, "torn")
        self.assertGreater(reads.seq_reads, 2)
        self.assertEqual((rd.last, rd.gaps, rd.overrun, rd.torn), (-1, [], [], []))

    def test_coherent_lapped_and_absent_keep_classification_and_counters(self):
        for seq0 in (0, rtring.MASK - 1):
            with self.subTest(seq0=seq0):
                wr, rd = self.ring(seq0=seq0)
                for r in (0, 2):
                    wr.publish(np.full((8, 8, 3), r, np.uint8), r)
                self.assertEqual(rd._try(0, retries=1), (None, "lapped"))
                self.assertEqual(rd._try(4, retries=1), (None, "absent"))
                self.assertIsNone(rd.acquire(0, retries=1))
                self.assertIsNone(rd.acquire(4, retries=1))
                self.assertEqual((rd.last, rd.gaps, rd.overrun, rd.torn),
                                 (-1, [], [0], []))

    def test_coherent_empty_and_skipped_publication_are_absent(self):
        wr, rd = self.ring()
        self.assertEqual(rd._try(1, retries=1), (None, "absent"))
        for r in (0, 3):
            wr.publish(np.full((8, 8, 3), r, np.uint8), r)
        self.assertEqual(rd._try(2, retries=1), (None, "absent"))
        self.assertIsNone(rd.acquire(2, retries=1))
        self.assertEqual((rd.last, rd.gaps, rd.overrun, rd.torn), (-1, [], [], []))

    def test_changed_index_observation_retries_and_recovers_frame(self):
        wr, rd = self.ring()
        wr.publish(np.full((8, 8, 3), 7, np.uint8), 0)
        # Scalar ri reports a different slot generation; the paired reread
        # reveals the real (seq=2, ri=0), then the second attempt succeeds.
        with HeaderReads(rd, 0, index=(2,)) as reads:
            f, status = rd._try(0, retries=2)
        self.assertIsNone(status)
        self.assertIsNotNone(f)
        self.assertEqual(reads.index_reads, 2)
        self.assertEqual(int(f.view[0, 0, 0]), 7)
        self.assertTrue(f.valid())
        del f

    def test_stale_paired_confirmation_consumes_existing_attempt_budget(self):
        wr, rd = self.ring()
        wr.publish(np.full((8, 8, 3), 1, np.uint8), 0)
        old_header = (wr._seq(0), 0)
        wr.publish(np.full((8, 8, 3), 2, np.uint8), 2)
        for retries, expected in ((1, "torn"), (2, "lapped")):
            with self.subTest(retries=retries):
                # The entire confirmation tuple can be stale too. A mismatch
                # retries; a later fully coherent observation may classify.
                with HeaderReads(rd, 0, headers=(old_header,)) as reads:
                    f, status = rd._try(0, retries=retries)
                self.assertIsNone(f)
                self.assertEqual(status, expected)
                self.assertEqual(reads.index_reads, retries)

    def test_odd_and_zero_budget_never_extend_attempts(self):
        wr, rd = self.ring()
        wr._begin(0)
        for retries in (0, 1, 4):
            with self.subTest(retries=retries):
                with HeaderReads(rd, 0) as reads:
                    self.assertEqual(rd._try(0, retries=retries), (None, "torn"))
                self.assertEqual(reads.seq_reads, retries)
                self.assertEqual((reads.index_reads, reads.header_reads), (0, 0))
        self.assertIsNone(rd.acquire(0, retries=4))
        self.assertEqual((rd.last, rd.gaps, rd.overrun, rd.torn), (-1, [], [], [0]))

    def test_matching_index_sequence_change_retries_then_succeeds(self):
        wr, rd = self.ring()
        wr.publish(np.full((8, 8, 3), 9, np.uint8), 0)
        with HeaderReads(rd, 0, seq=(0,)) as reads:
            f, status = rd._try(0, retries=2)
        self.assertIsNone(status)
        self.assertEqual(reads.index_reads, 2)
        self.assertEqual(int(f.view[0, 0, 0]), 9)
        del f

    def test_guarantee_crc_mismatch_never_returned_as_frame(self):
        wr, rd = self.ring()
        wr.publish(np.full((8, 8, 3), 42, np.uint8), 0)
        wr._slot_view(0)[0, 0, 0] ^= 0xFF  # Keep the actual even seq=2.
        self.assertIsNone(rd.acquire(0, retries=3))
        self.assertEqual(rd._try(0, retries=3), (None, "torn"))
        self.assertEqual((rd.last, rd.gaps, rd.overrun, rd.torn), (-1, [], [], [0]))
        rd.verify = False  # Existing opt-out remains a separate control.
        f = rd.acquire(0, retries=1)
        self.assertIsNotNone(f)
        del f

    def test_guarantee_frame_valid_turns_false_after_later_commit(self):
        wr, rd = self.ring()
        wr.publish(np.full((8, 8, 3), 1, np.uint8), 0)
        f = rd.acquire(0)
        self.assertEqual(f.view.shape, (8, 8, 3))
        self.assertFalse(f.view.flags.writeable)
        self.assertFalse(f.view.flags.owndata)
        self.assertTrue(np.shares_memory(f.view, np.frombuffer(rd._mm, np.uint8)))
        with self.assertRaises(ValueError):
            f.view[0, 0, 0] = 7
        self.assertTrue(f.valid())
        wr.publish(np.full((8, 8, 3), 2, np.uint8), 1)
        self.assertTrue(f.valid())
        view = wr.begin(2)
        self.assertFalse(f.valid())  # Invalid from begin, before commit.
        view[...] = 3
        wr.commit()
        del view
        self.assertFalse(f.valid())
        self.assertEqual(int(f.view[0, 0, 0]), 3)  # Same storage, not a copy.
        del f

    def test_poll_order_lapped_and_absent_accounting(self):
        wr, rd = self.ring()
        wr.publish(np.zeros((8, 8, 3), np.uint8), 0)
        f = rd.poll()
        self.assertEqual(f.render_index, 0)
        del f
        for r in range(1, 6):
            wr.publish(np.full((8, 8, 3), r, np.uint8), r)
        for r in (4, 5):
            f = rd.poll(retries=1)
            self.assertEqual(f.render_index, r)
            del f
        self.assertIsNone(rd.poll())
        self.assertEqual((rd.gaps, rd.overrun, rd.torn), ([(1, 3)], [1, 2, 3], []))

        wr, rd = self.ring(slots=3)
        for r in (0, 2):
            wr.publish(np.full((8, 8, 3), r, np.uint8), r)
        for r in (0, 2):
            f = rd.poll(retries=1)
            self.assertEqual(f.render_index, r)
            del f
        self.assertEqual((rd.last, rd.gaps, rd.overrun, rd.torn),
                         (2, [(1, 1)], [], []))

    def test_live_writer_view_keeps_reservation_until_released(self):
        wr, rd = self.ring()
        self.assertEqual(self.ws.used, wr.size)
        view = wr.begin()
        wr.close()
        self.assertTrue(wr.path.exists())
        self.assertEqual(self.ws.used, wr.size)
        del view
        rd.close()
        wr.close()
        self.assertFalse(wr.path.exists())
        self.assertEqual(self.ws.used, 0)
        released = self.ws.receipt()["released_bytes"]
        self.assertGreaterEqual(released, wr.size)
        wr.close()
        self.assertEqual(self.ws.receipt()["released_bytes"], released)


if __name__ == "__main__":
    unittest.main()
