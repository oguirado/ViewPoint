"""Fixed-seed property tests for RAM frame ring in vadapt/rtring.py.

Tests layout math functions data_off & ring_bytes, RingWriter, RingReader,
SPSC invariant safety, closed file behavior, and negative error conditions.
"""
import mmap
import random
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from vadapt import ramws, rtring

BASE_TEST_DIR = Path(ramws.ROOT) / "props_test_tmp"


class WS:
    """Fake workspace pattern for in-memory / /dev/shm testing."""

    def __init__(self, root):
        self.root = Path(root)

    def path(self, rel):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def reserve(self, n):
        pass

    def release(self, p):
        p = Path(p)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


class TestDataOffAndRingBytes(unittest.TestCase):
    def test_data_off_exact_and_negative(self):
        # Formula: (64 + slots * 64 + 4095) // 4096 * 4096
        for s in range(0, 100):
            expected = (64 + s * 64 + 4095) // 4096 * 4096
            self.assertEqual(rtring.data_off(s), expected)

        # Zero and negative slot values
        self.assertEqual(rtring.data_off(0), 4096)
        self.assertEqual(rtring.data_off(-1), 0)
        self.assertEqual(rtring.data_off(-10), 0)

        # Non-numeric / incompatible types raise TypeError
        for bad in ("2", None, [1], {"slots": 2}):
            with self.assertRaises(TypeError):
                rtring.data_off(bad)

    def test_ring_bytes_exact_and_negative(self):
        # Formula: data_off(slots) + slots * w * h * 3
        for s in range(0, 10):
            for w in (4, 8, 16):
                for h in (2, 4, 8):
                    expected = rtring.data_off(s) + s * w * h * 3
                    self.assertEqual(rtring.ring_bytes(s, w, h), expected)

        # Negative / zero slot or dimension cases
        self.assertEqual(rtring.ring_bytes(0, 4, 2), 4096)
        self.assertEqual(rtring.ring_bytes(2, 0, 2), 4096)
        self.assertEqual(rtring.ring_bytes(-1, 4, 2), -24)

        # Non-numeric / incompatible types raise TypeError
        for bad in ("2", None, [1], {"a": 1}):
            with self.assertRaises(TypeError):
                rtring.ring_bytes(bad, 4, 2)
            with self.assertRaises(TypeError):
                rtring.ring_bytes(2, bad, 2)
            with self.assertRaises(TypeError):
                rtring.ring_bytes(2, 4, bad)


class TestRingWriterAndReaderProps(unittest.TestCase):
    def setUp(self):
        BASE_TEST_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(BASE_TEST_DIR, ignore_errors=True)

    def test_seeds_0_to_4_200_random_ops(self):
        for seed in range(5):
            rnd = random.Random(seed)

            slots = rnd.randint(2, 5)
            w = rnd.randint(4, 16)
            h = rnd.randint(2, 8)

            with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
                ws = WS(tmpdir)
                rel_path = "ring_seed_%d/frames.rgb24" % seed
                wr = rtring.RingWriter(ws, slots, w, h, rel=rel_path)

                # Layout metadata verification
                expected_data_off = (64 + slots * 64 + 4095) // 4096 * 4096
                expected_total_bytes = expected_data_off + slots * w * h * 3
                self.assertEqual(wr.data_off, expected_data_off)
                self.assertEqual(wr.size, expected_total_bytes)
                self.assertEqual(rtring.ring_bytes(slots, w, h), expected_total_bytes)

                rd = rtring.RingReader(wr.path)
                self.assertEqual(rd.slots, slots)
                self.assertEqual(rd.width, w)
                self.assertEqual(rd.height, h)
                self.assertEqual(rd.data_off, expected_data_off)

                written_frames = {}
                last_delivered_r = -1
                last_seq_per_slot = {}
                write_r = 0

                for _ in range(200):
                    action = rnd.choice(["write", "write", "read_poll", "read_acquire"])
                    if action == "write":
                        frame_val = write_r & 255
                        frame = np.full((h, w, 3), frame_val, dtype=np.uint8)
                        frame[0, 0, 0] = (write_r * 17) & 255
                        written_frames[write_r] = frame

                        if rnd.random() < 0.5:
                            wr.publish(frame, write_r)
                        else:
                            v = wr.begin(write_r)
                            v[...] = frame
                            wr.commit()
                        write_r += 1

                    elif action == "read_poll":
                        f = rd.poll()
                        if f is not None:
                            r = f.render_index
                            self.assertGreater(r, last_delivered_r)
                            last_delivered_r = r
                            k = r % slots
                            if k in last_seq_per_slot:
                                self.assertGreater(f._seq, last_seq_per_slot[k])
                            last_seq_per_slot[k] = f._seq
                            self.assertEqual(f._seq & 1, 0)
                            np.testing.assert_array_equal(f.view, written_frames[r])
                            self.assertTrue(f.valid())
                            del f

                    elif action == "read_acquire":
                        h_head = rd.head()
                        if h_head >= 0:
                            target_r = rnd.randint(max(0, h_head - slots + 1), h_head)
                            f = rd.acquire(target_r)
                            if f is not None:
                                self.assertEqual(f.render_index, target_r)
                                k = target_r % slots
                                if k in last_seq_per_slot:
                                    self.assertGreaterEqual(f._seq, last_seq_per_slot[k])
                                np.testing.assert_array_equal(f.view, written_frames[target_r])
                                self.assertTrue(f.valid())
                                del f

                rd.close()
                wr.close()

    def test_operations_on_closed_reader_and_writer(self):
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            wr = rtring.RingWriter(ws, 3, 8, 4)
            rd = rtring.RingReader(wr.path)

            wr.publish(np.zeros((4, 8, 3), dtype=np.uint8), 0)

            # Refusal after writer closed
            wr.close()
            with self.assertRaises(ValueError) as ctx:
                wr.begin(1)
            self.assertEqual(str(ctx.exception), "ring closed")

            with self.assertRaises(ValueError) as ctx:
                wr.publish(np.zeros((4, 8, 3), dtype=np.uint8), 1)
            self.assertEqual(str(ctx.exception), "ring closed")

            # Refusal when mmap closed on reader
            rd.close()
            with self.assertRaises(ValueError) as ctx:
                rd.poll()
            self.assertEqual(str(ctx.exception), "mmap closed or invalid")

            with self.assertRaises(ValueError) as ctx:
                rd.acquire(0)
            self.assertEqual(str(ctx.exception), "mmap closed or invalid")

            with self.assertRaises(ValueError) as ctx:
                rd.head()
            self.assertEqual(str(ctx.exception), "mmap closed or invalid")


class TestNegativeCases(unittest.TestCase):
    def setUp(self):
        BASE_TEST_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(BASE_TEST_DIR, ignore_errors=True)

    def test_writer_init_invalid_slots_and_sizes(self):
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)

            # Zero or negative slots/width/height, non-int or bool values
            invalid_configs = [
                (0, 8, 4),
                (-1, 8, 4),
                (3, 0, 4),
                (3, -8, 4),
                (3, 8, 0),
                (3, 8, -4),
                (True, 8, 4),
                (3, False, 4),
                (3, 8, True),
                (2.0, 8, 4),
            ]

            for slots, w, h in invalid_configs:
                with self.assertRaises(ValueError) as ctx:
                    rtring.RingWriter(ws, slots, w, h, rel="invalid_init_%s_%s_%s/frames.rgb24" % (slots, w, h))
                self.assertEqual(str(ctx.exception), "slots/width/height must be positive ints")

    def test_writer_publish_invalid_frame_shape_dtype(self):
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            with rtring.RingWriter(ws, 2, 8, 4) as wr:
                # Wrong dtype
                bad_dtype = np.zeros((4, 8, 3), dtype=np.float32)
                with self.assertRaises(ValueError) as ctx:
                    wr.publish(bad_dtype)
                self.assertEqual(str(ctx.exception), "frame must be uint8 (4, 8, 3)")

                # Wrong shape: width / height mismatched or odd shapes not matching (4, 8, 3)
                bad_shapes = [
                    np.zeros((4, 8), dtype=np.uint8),
                    np.zeros((4, 8, 4), dtype=np.uint8),
                    np.zeros((3, 8, 3), dtype=np.uint8),
                    np.zeros((4, 7, 3), dtype=np.uint8),
                    np.zeros((5, 9, 3), dtype=np.uint8),
                ]
                for bad_frame in bad_shapes:
                    with self.assertRaises(ValueError) as ctx:
                        wr.publish(bad_frame)
                    self.assertEqual(str(ctx.exception), "frame must be uint8 (4, 8, 3)")

    def test_writer_begin_render_index_order(self):
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            with rtring.RingWriter(ws, 3, 8, 4) as wr:
                wr.publish(np.zeros((4, 8, 3), dtype=np.uint8), 5)
                with self.assertRaises(ValueError) as ctx:
                    wr.begin(5)
                self.assertEqual(str(ctx.exception), "render_index must increase (5 <= 5)")

                with self.assertRaises(ValueError) as ctx:
                    wr.begin(3)
                self.assertEqual(str(ctx.exception), "render_index must increase (3 <= 5)")

    def test_writer_begin_double_begin_without_commit(self):
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)
            with rtring.RingWriter(ws, 3, 8, 4) as wr:
                v1 = wr.begin()
                with self.assertRaises(ValueError) as ctx:
                    wr.begin()
                self.assertEqual(str(ctx.exception), "previous frame not committed")
                del v1

    def test_reader_init_small_file_or_invalid_magic(self):
        with tempfile.TemporaryDirectory(dir=BASE_TEST_DIR) as tmpdir:
            ws = WS(tmpdir)

            # File smaller than header (64 bytes)
            small_path = ws.path("small_ring/frames.rgb24")
            small_path.parent.mkdir(parents=True, exist_ok=True)
            small_path.write_bytes(b"VQARING1_too_short")

            with self.assertRaises(ValueError) as ctx:
                rtring.RingReader(small_path)
            self.assertEqual(str(ctx.exception), "ring file too small")

            # Corrupted magic/version
            bad_magic_path = ws.path("bad_magic/frames.rgb24")
            bad_magic_path.parent.mkdir(parents=True, exist_ok=True)
            bad_magic_path.write_bytes(b"X" * 128)

            with self.assertRaises(ValueError) as ctx:
                rtring.RingReader(bad_magic_path)
            self.assertEqual(str(ctx.exception), "not a VQARING1 v2 file")


if __name__ == "__main__":
    unittest.main()
