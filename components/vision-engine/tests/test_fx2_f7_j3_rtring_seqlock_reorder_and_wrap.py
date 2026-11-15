"""Tests for production RingReader seqlock reordering, recovery from stale observations,
slot wrap-around, and accounting contracts in vadapt/rtring.py.
"""

import pathlib
import shutil
import tempfile
import unittest

import numpy as np

from vadapt import ramws, rtring

BASE_TEST_DIR = pathlib.Path(ramws.ROOT) / "fx2_f7_j3_rtring_test_tmp"


class WS:
    """Fake workspace pattern for in-memory / /dev/shm testing."""

    def __init__(self, root):
        self.root = pathlib.Path(root)

    def path(self, rel):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def reserve(self, n):
        pass

    def release(self, p):
        p = pathlib.Path(p)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


class StructWrapper:
    """Wrapper around struct.Struct instances that redirects unpack_from to custom buffer objects."""

    def __init__(self, real_struct):
        self._real = real_struct

    def unpack_from(self, buf, offset=0):
        if hasattr(buf, "unpack_from"):
            return buf.unpack_from(self._real, offset)
        return self._real.unpack_from(buf, offset)

    def __getattr__(self, attr):
        return getattr(self._real, attr)


class ScriptedStaleSeqView:
    """Mapping view proxy that simulates a finite stale observation prefix followed by coherent data."""

    def __init__(self, real_mm, target_off, stale_count=2, stale_seq=1):
        self._real_mm = real_mm
        self.target_off = target_off
        self.stale_count = stale_count
        self.stale_seq = stale_seq
        self.read_count = 0

    def unpack_from(self, struct_obj, offset=0):
        val = struct_obj.unpack_from(self._real_mm, offset)
        if offset == self.target_off:
            self.read_count += 1
            if self.read_count <= self.stale_count:
                return (self.stale_seq,)
        return val

    def __buffer__(self, flags):
        return memoryview(self._real_mm).__buffer__(flags)

    def __getattr__(self, attr):
        return getattr(self._real_mm, attr)


class StaleSecondSeqView:
    """Mapping view proxy that returns a stale sequence value on the second sequence read."""

    def __init__(self, real_mm, target_off, stale_s1, stale_ri=None):
        self._real_mm = real_mm
        self.target_off = target_off
        self.stale_s1 = stale_s1
        self.stale_ri = stale_ri
        self.seq_read_count = 0

    def unpack_from(self, struct_obj, offset=0):
        val = struct_obj.unpack_from(self._real_mm, offset)
        if offset == self.target_off:
            self.seq_read_count += 1
            if self.seq_read_count in (1, 2):
                return (self.stale_s1,)
        elif offset == self.target_off + 8 and self.stale_ri is not None:
            if self.seq_read_count == 1:
                return (self.stale_ri,)
        return val

    def __buffer__(self, flags):
        return memoryview(self._real_mm).__buffer__(flags)

    def __getattr__(self, attr):
        return getattr(self._real_mm, attr)


class TestFx2F7J3RtringSeqlockReorderAndWrap(unittest.TestCase):
    def setUp(self):
        BASE_TEST_DIR.mkdir(parents=True, exist_ok=True)
        self._orig_structs = {
            name: getattr(rtring, name) for name in ("_G", "_Q", "_QQ", "_I")
        }
        for name, s in self._orig_structs.items():
            setattr(rtring, name, StructWrapper(s))

    def tearDown(self):
        for name, s in self._orig_structs.items():
            setattr(rtring, name, s)
        shutil.rmtree(BASE_TEST_DIR, ignore_errors=True)

    def test_recovery_from_finite_stale_observation_prefix_followed_by_coherent_commit(self):
        """Production RingReader retries past a finite stale-observation prefix and recovers coherent frame."""
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            with rtring.RingWriter(ws, 2, 8, 8) as wr:
                wr.publish(np.full((8, 8, 3), 42, np.uint8), 0)

                rd = rtring.RingReader(wr.path)
                # Slot 0 header is at HDR (64). Script 2 stale odd (in-flight) sequence observations
                view = ScriptedStaleSeqView(rd._mm, rtring.HDR, stale_count=2, stale_seq=1)
                rd._mm = view

                f, status = rd._try(0, retries=5)
                self.assertIsNotNone(f)
                self.assertIsNone(status)
                self.assertEqual(f.render_index, 0)
                self.assertEqual(int(f.view[0, 0, 0]), 42)
                # 2 stale attempts (1 read each = 2 reads) + 1 coherent attempt (2 seq reads = 2 reads) = 4 total seq reads
                self.assertEqual(view.read_count, 4)

                del f
                rd.close()

    def test_f02_stale_second_seq_load_reordering_lapped(self):
        """Demonstrated missing behavior: reader fence defect on aarch64 causes new render_index > r with stale seq to return lapped without retry."""
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            wr = rtring.RingWriter(ws, 2, 8, 8)
            wr.publish(np.full((8, 8, 3), 10, np.uint8), 0)

            rd = rtring.RingReader(wr.path)
            wr._begin(2)

            view = StaleSecondSeqView(rd._mm, rtring.HDR, stale_s1=0)
            rd._mm = view

            f, status = rd._try(0, retries=4)

            self.assertGreater(view.seq_read_count, 2)
            self.assertEqual(status, "torn")
            self.assertIsNone(f)

            del f
            rd.close()
            wr.close()

    def test_f02_stale_second_seq_load_reordering_absent(self):
        """Demonstrated missing behavior: reader fence defect on aarch64 causes render_index < r with stale seq to return absent without retry."""
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            wr = rtring.RingWriter(ws, 2, 8, 8)

            rd = rtring.RingReader(wr.path)
            wr._begin(1)

            view = StaleSecondSeqView(rd._mm, rtring.HDR + rtring.SLOT_HDR, stale_s1=0, stale_ri=0)
            rd._mm = view

            f, status = rd._try(1, retries=4)

            self.assertGreater(view.seq_read_count, 2)
            self.assertEqual(status, "torn")
            self.assertIsNone(f)

            del f
            rd.close()
            wr.close()

    def test_stable_different_render_index_controls(self):
        """Healthy control: stable slots with different render index return absent (ri < r) or lapped (ri > r)."""
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            with rtring.RingWriter(ws, 2, 8, 8) as wr:
                wr.publish(np.full((8, 8, 3), 1, np.uint8), 0)

                rd = rtring.RingReader(wr.path)

                # Query slot 1 before publication -> ri=0 < r=1 -> absent
                f_abs, status_abs = rd._try(1, retries=3)
                self.assertIsNone(f_abs)
                self.assertEqual(status_abs, "absent")

                # Writer publishes frame 1 (slot 1), 2 (slot 0), 3 (slot 1)
                wr.publish(np.full((8, 8, 3), 2, np.uint8), 1)
                wr.publish(np.full((8, 8, 3), 3, np.uint8), 2)
                wr.publish(np.full((8, 8, 3), 4, np.uint8), 3)

                # Query slot 0 for r=0 -> current ri=2 > 0 -> lapped
                f_lap, status_lap = rd._try(0, retries=3)
                self.assertIsNone(f_lap)
                self.assertEqual(status_lap, "lapped")

                del f_abs, f_lap
                rd.close()

    def test_slot_reuse_across_ring_laps_and_exact_accounting(self):
        """Slot reuse across multiple ring laps verifies exact delivery, overrun, torn, gap, and head tracking."""
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            with rtring.RingWriter(ws, 3, 16, 16) as wr:
                rd = rtring.RingReader(wr.path)

                # Phase 1: Publish frames 0..2 (1st lap)
                for r in range(3):
                    wr.publish(np.full((16, 16, 3), r + 10, np.uint8), r)

                # Consume 0 and 1 via poll
                f0 = rd.poll()
                f1 = rd.poll()
                self.assertEqual(f0.render_index, 0)
                self.assertEqual(f1.render_index, 1)
                self.assertEqual(rd.last, 1)

                # Phase 2: Writer publishes frames 3..7 (laps ring slots multiple times)
                for r in range(3, 8):
                    wr.publish(np.full((16, 16, 3), r + 10, np.uint8), r)

                # Consumer polls next: slot 2 (frame 2) was lapped by frame 5, slot 0 by frame 6, slot 1 by frame 7
                # Lowest available in ring is max(last + 1 = 2, head - slots + 1 = 8 - 3 = 5) -> lo = 5.
                # Dropped frames 2, 3, 4 counted as overrun
                got = []
                while True:
                    f = rd.poll()
                    if f is None:
                        break
                    got.append((f.render_index, int(f.view[0, 0, 0])))
                    del f

                self.assertEqual(got, [(5, 15), (6, 16), (7, 17)])
                self.assertEqual(rd.overrun, [2, 3, 4])
                self.assertEqual(rd.gaps, [(2, 4)])
                self.assertEqual(rd.torn, [])
                self.assertEqual(rd.last, 7)
                self.assertEqual(rd.head(), 7)

                del f0, f1
                rd.close()

    def test_combined_callback_overwrite_post_use_rejection_recovery_and_crc_corruption(self):
        """Combine callback-time overwrite, post-use rejection, CRC corruption, and coherent retry recovery in rd.read()."""
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            with rtring.RingWriter(ws, 2, 8, 8) as wr:
                wr.publish(np.full((8, 8, 3), 1, np.uint8), 0)

                rd = rtring.RingReader(wr.path)
                callback_calls = []

                def fn(v):
                    val = int(v[0, 0, 0])
                    callback_calls.append(val)
                    if len(callback_calls) == 1:
                        # 1) Overwrite slot 0 by publishing frames 1 and 2 (laps slot 0)
                        wr.publish(np.full((8, 8, 3), 2, np.uint8), 1)
                        # 2) Corrupt CRC on slot 1 (frame 1 payload) while keeping seq stable
                        wr._slot_view(1)[0, 0, 0] ^= 0xFF
                        # 3) Publish frame 2 to slot 0 with valid payload & CRC
                        wr.publish(np.full((8, 8, 3), 3, np.uint8), 2)
                    return val

                res = rd.read(fn, retries=8)

                # Expected sequence of events:
                # - Call 1 on frame 0: fn executes. Writer publishes frame 1 (corrupts slot 1 CRC) and frame 2 (slot 0).
                # - Post-use check on frame 0 sees f.valid() == False. Render index 0 added to rd.torn.
                # - rd.read() retries: poll(latest=True) acquires frame 2 (head = 2).
                # - Call 2 on frame 2: fn executes, returns 3. f.valid() == True.
                # - rd.read() returns (3, 2).
                self.assertIsNotNone(res)
                result_val, delivered_ri = res
                self.assertEqual(result_val, 3)
                self.assertEqual(delivered_ri, 2)
                self.assertEqual(callback_calls, [1, 3])
                self.assertEqual(rd.torn, [0])

                rd.close()


if __name__ == "__main__":
    unittest.main()
