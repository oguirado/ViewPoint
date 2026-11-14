import json, os, stat, sys, tempfile, threading, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.dirname(HERE)
sys.path.insert(0, WL)
import driver as D

# W4 lane M: host-side marker reader contract (AW4-06): O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC, fstat S_ISREG, st_nlink == 1, size caps,
# run_nonce + chunk echo, presence probes through lstat (never isfile/exists).

NONCE = "0123456789abcdef0123456789abcdef"

def ready(k, nonce=None, **kw):
    r = {"schema": D.READY_SCHEMA, "chunk": k, "first_render": 0, "frames": 2, "w": 1920, "h": 1080, "grid": 32, "det_sha256": []}
    if nonce is not None:
        r["run_nonce"] = nonce
    r.update(kw)
    return json.dumps(r)


def bounded(fn, secs=2.0):
    box = {}

    def t():
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=t, daemon=True)
    th.start()
    th.join(secs)
    return th.is_alive(), box


class Markers(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="wlm_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.script = os.path.join(self.d, "fake_analyze.py")
        with open(self.script, "w") as f:
            f.write("import json\nprint(json.dumps({'chunk': 0, 'frames': 2, 'grid': 32, 'class_counts': {'UNCHANGED': 1, 'JITTER': 0, 'CHANGED': 0, 'MOVED': 0, 'FLICKER': 0, 'FROZEN': 0, 'DRIFT': 0}, 'analyze_us_per_frame': 5, 'engine_wheel_sha8': '5e2395d5'}))\n")
        self.a = types.SimpleNamespace(analyze_script=self.script, analyze_timeout=20)

    def chunk(self, name="chunk_0"):
        c = os.path.join(self.d, name)
        os.mkdir(c)
        return c

    def test_fifo_no_block(self):
        c = self.chunk()
        fifo = os.path.join(c, "READY")
        os.mkfifo(fifo)
        try:
            blocked, box = bounded(lambda: (D._pending_chunks(self.d, set()),
                                            D._analyze_chunk(self.a, c, 0, 1920, 1080, 1e18)))
            self.assertFalse(blocked, "READY FIFO blocked the host (> 2 s)")
            e = box.get("e")
            self.assertIsInstance(e, D.DriverError, box)
            self.assertEqual(e.rc, 5)
            self.assertTrue(os.path.lexists(os.path.join(c, "ANALYZE_FAILED")))
        finally:
            try:
                os.close(os.open(fifo, os.O_RDWR | os.O_NONBLOCK))  # release a stuck opener, if any
            except OSError:
                pass

    def test_nonregular_refused(self):
        good = os.path.join(self.d, "good")
        with open(good, "w") as f:
            f.write("abc")
        got = D._open_marker(good, 16)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], b"abc")
        self.assertIsInstance(got[1], int)
        self.assertIsNone(D._open_marker(os.path.join(self.d, "missing"), 16))
        os.symlink(good, os.path.join(self.d, "ln"))
        os.link(good, os.path.join(self.d, "hl"))
        with open(os.path.join(self.d, "big"), "w") as f:
            f.write("x" * 17)
        open(os.path.join(self.d, "empty"), "w").close()
        os.mkdir(os.path.join(self.d, "dir"))
        os.mkfifo(os.path.join(self.d, "fifo"))
        for n in ("ln", "hl", "big", "empty", "dir", "fifo"):
            blocked, box = bounded(lambda n=n: D._open_marker(os.path.join(self.d, n), 16))
            self.assertFalse(blocked, n)
            self.assertIsInstance(box.get("e"), D.DriverError, n)
            self.assertEqual(box["e"].rc, 5, n)

    def test_nonce_chunk_echo(self):
        c = self.chunk()
        with open(os.path.join(c, "READY"), "w") as f:
            f.write(ready(0, NONCE))
        line = D._analyze_chunk(self.a, c, 0, 1920, 1080, 1e18, nonce=NONCE)
        self.assertIsNotNone(line)
        row = json.loads(open(os.path.join(c, "ANALYZED")).read())
        self.assertEqual((row["chunk"], row.get("run_nonce")), (0, NONCE))
        # READY of another run (replay) -> ANALYZE_FAILED rc 5, no ANALYZED
        c2 = self.chunk("chunk_1")
        with open(os.path.join(c2, "READY"), "w") as f:
            f.write(ready(1, "f" * 32))
        self.assertIsNone(D._analyze_chunk(self.a, c2, 1, 1920, 1080, 1e18, nonce=NONCE))
        self.assertFalse(os.path.lexists(os.path.join(c2, "ANALYZED")))
        self.assertEqual(json.loads(open(os.path.join(c2, "ANALYZE_FAILED")).read())["rc"], 5)
        # READY naming another chunk
        c3 = self.chunk("chunk_2")
        with open(os.path.join(c3, "READY"), "w") as f:
            f.write(ready(9, NONCE))
        self.assertIsNone(D._analyze_chunk(self.a, c3, 2, 1920, 1080, 1e18, nonce=NONCE))
        self.assertTrue(os.path.lexists(os.path.join(c3, "ANALYZE_FAILED")))

    def test_probe_no_follow(self):
        c = self.chunk()
        with open(os.path.join(c, "READY"), "w") as f:
            f.write(ready(0))
        self.assertEqual(D._pending_chunks(self.d, set()), [0])
        os.symlink("/nonexistent/target", os.path.join(c, "ANALYZED"))  # dangling: exists() says False, lstat says present
        self.assertEqual(D._pending_chunks(self.d, set()), [])
        c1 = self.chunk("chunk_1")
        with open(os.path.join(c1, "READY"), "w") as f:
            f.write(ready(1))
        real = self.chunk("real_1")
        os.rename(c1, os.path.join(self.d, "chunk_1.moved"))
        os.symlink(os.path.join(self.d, "chunk_1.moved"), os.path.join(self.d, "chunk_1"))  # symlinked chunk dir is never entered
        self.assertEqual(D._pending_chunks(self.d, set()), [])


if __name__ == "__main__":
    unittest.main()
