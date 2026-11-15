"""RT1-L1: RAM frame ring (vadapt/rtring.py). Rings live in /dev/shm/vqa-ramws only (tool rt1_ring_test)."""
import hashlib
import os
import shutil
import statistics
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path

import numpy as np

from vadapt import ramws, rtring

ROOT = Path(__file__).resolve().parent.parent
TOOL = "rt1_ring_test"
BASE = Path(ramws.ROOT)
SCRATCH = ROOT / "build" / "room_L1"
SCRATCH.mkdir(parents=True, exist_ok=True)

PRODUCER = """
import sys, time
import numpy as np
from vadapt import rtring
path, slots, w, h, n, hz = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]), float(sys.argv[6])
# the parent owns the workspace; the child attaches a writer-side view through a stub ws
class WS:
    def path(self, rel): return __import__('pathlib').Path(path)
    def reserve(self, n): pass
    def release(self, p): pass
wr = rtring.RingWriter(WS(), slots, w, h)
print('ready', flush=True)
t0 = time.perf_counter()
for r in range(n):
    wr.publish(np.full((h, w, 3), r % 251, np.uint8), r)
    d = t0 + (r + 1) / hz - time.perf_counter()
    if d > 0: time.sleep(d)
wr.close()
"""


def sha(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


class Case(unittest.TestCase):
    def setUp(self):
        self.ws = []

    def tearDown(self):
        for w in self.ws:
            w.close()
        shutil.rmtree(BASE / TOOL, ignore_errors=True)
        try:
            BASE.rmdir()
        except OSError:
            pass

    def open(self, run="r1", quota=ramws.RUN_DEFAULT):
        w = ramws.open_run("c", run, quota, tool=TOOL)
        self.ws.append(w)
        return w


class TestGuards(Case):
    def test_path_outside_root_refused(self):
        ws = self.open()
        for bad in ("/tmp/x.rgb24", "../x", "a/../../x"):
            with self.assertRaises(ramws.RamError) as c:
                rtring.RingWriter(ws, 2, 8, 8, rel=bad)
            self.assertEqual(c.exception.code, "E_RAM_ESCAPE")
        with self.assertRaises(ramws.RamError) as c:
            rtring.RingReader("/tmp/not_in_ramws")
        self.assertEqual(c.exception.code, "E_RAM_ESCAPE")

    def test_over_quota_before_file(self):
        ws = self.open(quota=1 << 20)
        with self.assertRaises(ramws.RamError) as c:
            rtring.RingWriter(ws, 8, 1000, 600)
        self.assertEqual(c.exception.code, "E_RAM_QUOTA")
        self.assertFalse((ws.root / "ring" / "frames.rgb24").exists())
        self.assertEqual(ws.used, 0)

    def test_view_readonly_and_shape(self):
        ws = self.open()
        with rtring.RingWriter(ws, 4, 64, 32) as wr:
            wr.publish(np.full((32, 64, 3), 7, np.uint8))
            rd = rtring.RingReader(wr.path)
            f = rd.poll()
            self.assertEqual(f.view.shape, (32, 64, 3))
            self.assertFalse(f.view.flags.writeable)
            self.assertFalse(f.view.flags.owndata)
            with self.assertRaises(ValueError):
                f.view[0, 0, 0] = 1
            with self.assertRaises(ValueError):
                wr.publish(np.zeros((32, 64, 3), np.int16))
            del f
            rd.close()


class TestRoundTrip(Case):
    def test_1000_frames_1000x600_sha(self):
        ws = self.open()
        rng = np.random.default_rng(1)
        with rtring.RingWriter(ws, 8, 1000, 600) as wr:
            rd = rtring.RingReader(wr.path)
            for k in range(1000):
                src = rng.integers(0, 256, (600, 1000, 3), dtype=np.uint8) if k % 50 == 0 else src
                src = src.copy()
                src[0, 0, 0] = k & 255
                wr.publish(src, k)
                f = rd.acquire(k)
                self.assertEqual(f.render_index, k)
                self.assertEqual(sha(f.view), sha(src))
                self.assertTrue(f.valid())
                del f
            self.assertEqual((rd.gaps, rd.torn), ([], []))
            rd.close()

    def test_120_frames_4k_sha_and_acquire_latency(self):
        ws = self.open()
        rng = np.random.default_rng(2)
        base = rng.integers(0, 256, (2160, 3840, 3), dtype=np.uint8)
        lat = []
        with rtring.RingWriter(ws, 8, 3840, 2160) as wr:
            rd = rtring.RingReader(wr.path)
            for k in range(120):
                src = np.roll(base, k, axis=0)
                wr.publish(src, k)
                t = time.perf_counter()
                f = rd.acquire(k)
                lat.append(time.perf_counter() - t)
                self.assertEqual(sha(f.view), sha(src))
                del f
            print("4K acquire median ms %.4f" % (statistics.median(lat) * 1e3), file=sys.stderr)
            rd.close()
        self.assertEqual(ramws.check_disk(SCRATCH), [])

    def test_lapped_slot_is_dropped_with_log(self):
        ws = self.open()
        with rtring.RingWriter(ws, 2, 16, 16) as wr:
            rd = rtring.RingReader(wr.path)
            for r in range(5):
                wr.publish(np.full((16, 16, 3), r, np.uint8), r)
            self.assertIsNone(rd.acquire(1))
            self.assertEqual((rd.overrun, rd.torn), ([1], []))  # lapped is not torn
            f = rd.acquire(4)
            self.assertEqual(rd.gaps, [(0, 3)])
            del f
            rd.close()


class TestTorn(Case):
    def test_reader_refuses_mid_write_and_valid_flips(self):
        ws = self.open()
        with rtring.RingWriter(ws, 3, 16, 16) as wr:
            rd = rtring.RingReader(wr.path)
            wr.publish(np.full((16, 16, 3), 1, np.uint8), 0)
            f = rd.acquire(0)
            self.assertTrue(f.valid())
            for r in (1, 2):
                wr.publish(np.full((16, 16, 3), 1, np.uint8), r)
            wr.begin(3)  # slot 0 odd: frame 0 is being overwritten
            self.assertFalse(f.valid())
            self.assertIsNone(rd.acquire(3, retries=4))
            self.assertEqual(rd.torn, [3])
            del f
            rd.close()

    def test_read_helper_retries_then_drops(self):
        ws = self.open()
        with rtring.RingWriter(ws, 3, 16, 16) as wr:
            rd = rtring.RingReader(wr.path)
            wr.publish(np.full((16, 16, 3), 9, np.uint8), 0)
            hits = []

            def fn(v):
                hits.append(int(v[0, 0, 0]))
                if len(hits) == 1:  # producer laps this slot while the consumer is using the view
                    for r in (1, 2):
                        wr.publish(np.zeros((16, 16, 3), np.uint8), r)
                    wr.publish(np.zeros((16, 16, 3), np.uint8), 3)
                return hits[-1]
            res = rd.read(fn)
            self.assertEqual(rd.torn[0], 0)
            self.assertEqual(res[1], 3)  # next poll delivers the newest committed frame, never the torn one
            rd.close()

    def test_seq_wrap_past_2_32_and_2_64(self):
        for seq0 in ((1 << 32) - 4, (1 << 64) - 4):
            ws = self.open(run="w%d" % (seq0 >> 32))
            with rtring.RingWriter(ws, 2, 8, 8, _seq0=seq0) as wr:
                rd = rtring.RingReader(wr.path)
                for r in range(12):
                    wr.publish(np.full((8, 8, 3), r, np.uint8), r)
                    f = rd.acquire(r)
                    self.assertEqual(int(f.view[0, 0, 0]), r)
                    self.assertTrue(f.valid())
                    del f
                self.assertEqual(rd.torn, [])
                self.assertEqual(rtring._Q.unpack_from(wr._mm, rtring.HDR)[0] & 1, 0)
                rd.close()

    def test_producer_process_120hz_consumer_60hz(self):
        ws = self.open()
        w, h, n = 1000, 600, 1200
        path = str(ws.path(rtring.REL))
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        p = subprocess.Popen([sys.executable, "-c", PRODUCER, path, "3", str(w), str(h), str(n), "120"],
                             stdout=subprocess.PIPE, text=True, env=env, cwd=ROOT)
        try:
            self.assertEqual(p.stdout.readline().strip(), "ready")
            rd = rtring.RingReader(path)
            delivered, torn_accepted = [], 0
            end = time.monotonic() + 10 + 3
            nxt = time.monotonic()
            while time.monotonic() < end:
                res = rd.read(lambda v: (int(v[0, 0, 0]), bool((v == v[0, 0, 0]).all())))
                if res:
                    (val, uniform), r = res
                    delivered.append(r)
                    if not uniform or val != r % 251:
                        torn_accepted += 1
                    if r == n - 1:
                        break
                nxt += 1 / 60
                time.sleep(max(0, nxt - time.monotonic()))
            p.wait(timeout=15)
        finally:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
            p.stdout.close()
        self.assertEqual(torn_accepted, 0)
        self.assertGreater(len(delivered), 300)
        self.assertEqual(delivered, sorted(set(delivered)))
        # exact drop list: every index below the last delivered is either delivered, in a logged gap, or torn
        missing = set(range(delivered[-1] + 1)) - set(delivered)
        logged = set()
        for a, b in rd.gaps:
            logged.update(range(a, b + 1))
        dropped = logged | set(rd.overrun) | set(rd.torn)
        self.assertEqual(missing, dropped)
        self.assertEqual(rd.torn, [])
        self.assertGreater(len(missing), 0)  # 120 Hz vs 60 Hz must drop
        rd.close()


class TestClose(Case):
    def test_close_releases_and_disk_clean(self):
        ws = self.open()
        wr = rtring.RingWriter(ws, 4, 100, 50)
        self.assertEqual(ws.used, wr.size)
        path = wr.path
        wr.close()
        wr.close()
        self.assertFalse(path.exists())
        self.assertEqual(ws.used, 0)
        self.assertGreaterEqual(ws.receipt()["released_bytes"], rtring.ring_bytes(4, 100, 50))
        self.assertEqual(ramws.check_disk(SCRATCH), [])

    def test_close_with_live_view_does_not_raise(self):
        ws = self.open()
        wr = rtring.RingWriter(ws, 2, 8, 8)
        v = wr.begin()
        wr.close()  # F5: views alive -> unmap impossible -> file and reservation are KEPT
        self.assertTrue(wr.path.exists())
        self.assertEqual(ws.used, wr.size)
        del v
        wr.close()  # views dropped -> now released
        self.assertFalse(wr.path.exists())
        self.assertEqual(ws.used, 0)


class TestF4Reservation(Case):
    def test_collision_does_not_leak_reservation(self):
        ws = self.open()
        with rtring.RingWriter(ws, 2, 16, 16) as wr:
            used = ws.used
            with self.assertRaises(FileExistsError):
                rtring.RingWriter(ws, 2, 16, 16)
            self.assertEqual(ws.used, used)  # before: +ring_bytes leaked until ws.close()
            self.assertTrue(wr.path.exists())  # the first writer's file is untouched
        self.assertEqual(ws.used, 0)

    def test_failed_map_returns_reservation(self):
        ws = self.open()
        real = rtring._map

        def boom(*a):
            raise OSError("map failed")
        rtring._map = boom
        try:
            with self.assertRaises(OSError):
                rtring.RingWriter(ws, 2, 16, 16)
        finally:
            rtring._map = real
        self.assertFalse((ws.root / "ring" / "frames.rgb24").exists())
        self.assertLess(ws.used, rtring.ring_bytes(2, 16, 16))


class TestOrderAndCounters(Case):
    def test_poll_delivers_every_intact_slot_in_order(self):
        ws = self.open()
        with rtring.RingWriter(ws, 4, 8, 8) as wr:
            rd = rtring.RingReader(wr.path)
            for r in range(3):
                wr.publish(np.full((8, 8, 3), r, np.uint8), r)
            got = []
            while True:
                f = rd.poll()
                if f is None:
                    break
                got.append(f.render_index)
                del f
            self.assertEqual(got, [0, 1, 2])  # before: only the newest (2) was delivered
            self.assertEqual((rd.gaps, rd.overrun, rd.torn), ([], [], []))
            rd.close()

    def test_poll_lapped_counted_as_overrun_not_torn(self):
        ws = self.open()
        with rtring.RingWriter(ws, 2, 8, 8) as wr:
            rd = rtring.RingReader(wr.path)
            wr.publish(np.zeros((8, 8, 3), np.uint8), 0)
            f = rd.poll()
            self.assertEqual(f.render_index, 0)
            del f
            for r in range(1, 6):
                wr.publish(np.full((8, 8, 3), r, np.uint8), r)
            got = []
            while True:
                f = rd.poll()
                if f is None:
                    break
                got.append(f.render_index)
                del f
            self.assertEqual(got, [4, 5])
            self.assertEqual(rd.overrun, [1, 2, 3])
            self.assertEqual(rd.torn, [])
            self.assertEqual(rd.gaps, [(1, 3)])
            rd.close()


class TestCrc(Case):
    def test_corrupt_payload_with_stable_seq_is_torn(self):
        """A reordered/partial store that leaves seq even is caught only by the per-slot crc32."""
        ws = self.open()
        with rtring.RingWriter(ws, 2, 8, 8) as wr:
            wr.publish(np.full((8, 8, 3), 5, np.uint8), 0)
            wr._slot_view(0)[3, 3, 1] ^= 0xFF  # bytes change, seq untouched
            rd = rtring.RingReader(wr.path)
            self.assertIsNone(rd.acquire(0, retries=3))
            self.assertEqual((rd.torn, rd.overrun), ([0], []))
            rd.close()
            rd = rtring.RingReader(wr.path, verify=False)  # control: seq-only reader accepts it
            f = rd.acquire(0)
            self.assertIsNotNone(f)
            del f
            rd.close()

    def test_zero_copy_begin_commit_has_crc(self):
        ws = self.open()
        with rtring.RingWriter(ws, 2, 8, 8) as wr:
            v = wr.begin()
            v[...] = 9
            wr.commit()
            del v
            rd = rtring.RingReader(wr.path)
            f = rd.acquire(0)
            self.assertEqual(int(f.view[0, 0, 0]), 9)
            del f
            rd.close()

    def test_crc_mutant_killed(self):
        src = (ROOT / "vadapt" / "rtring.py").read_text()
        old = "if self.verify and zlib.crc32(v) != crc:"
        self.assertEqual(src.count(old), 1)
        for name, text in (("crc_ctl", src), ("crc_mut", src.replace(old, "if False:"))):
            mod = _load(text, name)
            ws = self.open(run=name)
            with mod.RingWriter(ws, 2, 8, 8) as wr:
                wr.publish(np.full((8, 8, 3), 5, np.uint8), 0)
                wr._slot_view(0)[0, 0, 0] = 77
                rd = mod.RingReader(wr.path)
                f = rd.acquire(0, retries=2)
                refused = f is None
                del f
                rd.close()
            self.assertEqual(refused, name == "crc_ctl")


def _load(src, name):
    m = types.ModuleType(name)
    m.__file__ = name
    exec(compile(src, name, "exec"), m.__dict__)
    return m


class TestMutant(Case):
    """Mutant: seq published (even, stable) BEFORE the memcpy. The mid-copy probe must kill it."""

    def probe(self, mod, run):
        ws = self.open(run=run)
        seen = []

        class W(mod.RingWriter):
            def _copy(self, k, frame):
                self._slot_view(k)[:4] = frame[:4]
                seen.append(rd.acquire(0, retries=2))  # reader attempts mid-copy
                self._slot_view(k)[4:] = frame[4:]
        wr = W(ws, 2, 8, 8)
        rd = mod.RingReader(wr.path, verify=False)  # seq protocol alone; crc is covered by TestCrc
        wr.publish(np.full((8, 8, 3), 5, np.uint8), 0)
        ok = seen[0] is None
        del seen[:]
        rd.close()
        wr.close()
        return ok

    def test_control_passes_and_mutant_killed(self):
        src = (ROOT / "vadapt" / "rtring.py").read_text()
        self.assertTrue(self.probe(_load(src, "rtring_control"), "ctl"))
        old = "        self._copy(k, frame)\n        self._commit()\n"
        new = "        self._commit()\n        self._copy(k, frame)\n"
        self.assertEqual(src.count(old), 1)
        self.assertFalse(self.probe(_load(src.replace(old, new), "rtring_mutant"), "mut"))


if __name__ == "__main__":
    unittest.main()
