"""fix-io regression tests for the xr-io cross-review (O1, O3).

O1: record.sh must refuse every spelling Xlib parses as display 1
    (":01", ":001.0", ...), not only the literal ":1".
O3: two cursor records of one agent with equal ts must not both be drawn.

The O1 test never lets a real ffmpeg run: a stub `ffmpeg` that exits at
once is first on PATH, so even a broken guard cannot grab a display.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_harden import cur, plan  # noqa: E402

RECORD = os.path.join(HERE, "record.sh")


class DisplayOneGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="vp-f2-fixio-")
        self.addCleanup(shutil.rmtree, self.d, True)
        stub = os.path.join(self.d, "bin")
        os.mkdir(stub)
        p = os.path.join(stub, "ffmpeg")
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, PATH=stub + os.pathsep + os.environ["PATH"],
                        VP_RECORD_NO_SCOPE="1")

    def rec(self, display):
        out = os.path.join(self.d, "out")
        r = subprocess.run(["bash", RECORD, display, "t", out], env=self.env,
                           capture_output=True, text=True, timeout=30)
        return r.returncode, r.stderr, out

    def test_every_spelling_of_display_1_is_refused(self):
        for disp in (":1", ":1.0", ":01", ":001", ":001.0", ":01.1", ":0001.00"):
            rc, err, out = self.rec(disp)
            self.assertEqual(rc, 3, "%r not refused: rc=%s %s" % (disp, rc, err))
            self.assertIn("off limits", err)
            self.assertFalse(os.path.exists(out), "%r created the outdir" % disp)

    def test_malformed_display_is_rejected(self):
        for disp in (":1x", ":2foo", ":", ":1.", ":2.x", "host:1", ":-1", ": 1",
                     ":1 ", ":2\n:1"):
            rc, err, out = self.rec(disp)
            self.assertEqual(rc, 2, "%r: rc=%s %s" % (disp, rc, err))
            self.assertFalse(os.path.exists(out))

    def test_other_displays_pass_the_guard(self):
        # control: the guard is not a blanket refusal (stub ffmpeg exits -> rc 5)
        for disp in (":5", ":10", ":11.0", ":100", ":05"):
            rc, err, _ = self.rec(disp)
            self.assertEqual(rc, 5, "%r: rc=%s %s" % (disp, rc, err))


class EqualTsCursors(unittest.TestCase):
    def test_same_agent_same_ts_keeps_only_the_last_record(self):
        p = plan([cur("look-agent", "act", 1.0, x=100),
                  cur("look-agent", "act", 1.0, x=900)], [])
        curs = [i for i in p["items"] if i["type"] == "cursor"]
        self.assertEqual(len(curs), 1, curs)
        self.assertEqual(curs[0]["x"], 900)

    def test_other_agent_at_same_ts_is_kept(self):
        p = plan([cur("look-agent", "act", 1.0, x=100),
                  cur("modeler-agent", "act", 1.0, x=900)], [])
        self.assertEqual(len([i for i in p["items"] if i["type"] == "cursor"]), 2)


if __name__ == "__main__":
    unittest.main()
