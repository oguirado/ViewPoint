import json, os, shutil, sys, tempfile, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import driver as D
from test_driver_markers import NONCE, ready

# W4 FX1 lane X2 (ADC-W4-02): the nonce comparison is unconditional (a READY with a foreign run_nonce is never analysed, even
# when the driver has no START nonce) and _put_marker fixes the mode on the fd it created, not through the path.


class Fx1(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="fx1_")
        self.addCleanup(lambda: shutil.rmtree(self.d, ignore_errors=True))
        self.script = os.path.join(self.d, "an.py")
        with open(self.script, "w") as f:
            f.write("import json\nprint(json.dumps({'chunk': 0, 'frames': 2, 'grid': 32, 'class_counts': {}, 'analyze_us_per_frame': 5, 'engine_wheel_sha8': '5e2395d5'}))\n")
        self.a = types.SimpleNamespace(analyze_script=self.script, analyze_timeout=20)

    def chunk(self, name="chunk_0"):
        c = os.path.join(self.d, name)
        os.mkdir(c)
        return c

    def test_nonce_none_foreign_ready_refused(self):
        c = self.chunk()
        with open(os.path.join(c, "READY"), "w") as f:
            f.write(ready(0, NONCE))
        self.assertIsNone(D._analyze_chunk(self.a, c, 0, 1920, 1080, 1e18, nonce=None))
        self.assertFalse(os.path.lexists(os.path.join(c, "ANALYZED")))
        self.assertEqual(json.loads(open(os.path.join(c, "ANALYZE_FAILED")).read())["rc"], 5)

    def test_nonce_none_plain_ready_still_passes(self):
        c = self.chunk()
        with open(os.path.join(c, "READY"), "w") as f:
            f.write(ready(0))
        self.assertIsNotNone(D._analyze_chunk(self.a, c, 0, 1920, 1080, 1e18, nonce=None))
        self.assertTrue(os.path.lexists(os.path.join(c, "ANALYZED")))

    def test_put_marker_fchmod_on_fd(self):
        p = os.path.join(self.d, "M")
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        with mock.patch.object(os, "chmod", wraps=os.chmod) as pc, mock.patch.object(os, "fchmod", wraps=os.fchmod) as fc:
            D._put_marker(p, "x\n")
        self.assertEqual(pc.call_count, 0, "path chmod used")
        self.assertEqual(fc.call_count, 1)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
