"""run_bench: argument/budget refusals (no Blender) and a 2-scene smoke run (skips when /usr/bin/blender is missing)."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from blender_visual_qa.truth import run_bench as rb

HAVE_BLENDER = os.path.exists("/usr/bin/blender")


def run_main(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = rb.main(argv)
    return rc, buf.getvalue()


class ValidateTests(unittest.TestCase):
    def test_unknown_bench_rc2(self):
        rc, out = run_main(["--out", tempfile.mkdtemp(), "--benches", "B1,B99"])
        self.assertEqual(rc, 2)
        self.assertIn("E_USAGE", out)

    def test_budget_refusals(self):
        d = tempfile.mkdtemp()
        for argv in (["--res", "640x360"], ["--frames", "61"], ["--spp", "8"], ["--spp", "4,4"], ["--frames", "1"]):
            rc, out = run_main(["--out", d] + argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("E_BUDGET", out)

    def test_missing_blender_rc2(self):
        old = rb.BLENDER
        rb.BLENDER = "/nonexistent/blender"
        try:
            rc, out = run_main(["--out", tempfile.mkdtemp(), "--benches", "B1"])
        finally:
            rb.BLENDER = old
        self.assertEqual(rc, 2)
        self.assertIn("E_NO_BLENDER", out)

    def test_cmd_is_pinned(self):
        cmd = rb.blender_cmd("x.blend")
        self.assertIn("--factory-startup", cmd)
        self.assertIn("-noaudio", cmd)
        self.assertLessEqual(int(cmd[cmd.index("-t") + 1]), 4)
        self.assertNotIn("--enable-autoexec", cmd)

    def test_summary_md_handles_failures(self):
        rep = {"grid": "g", "spp": 4, "frames": 4, "renderer": "r", "benches": [], "calibration": {},
               "failures": [{"bench": "B2", "error": "boom"}]}
        md = rb.summary_md(rep)
        self.assertIn("B2: boom", md)


@unittest.skipUnless(HAVE_BLENDER, "needs /usr/bin/blender")
class SmokeTests(unittest.TestCase):
    def test_two_scene_smoke(self):
        d = Path(tempfile.mkdtemp())
        rc, out = run_main(["--out", str(d), "--benches", "B1,B6", "--frames", "4"])
        self.assertIn(rc, (0, 5), out)  # 5 = calibration refused is a legal, reported outcome
        rep = json.loads((d / "calib_report.json").read_text())
        self.assertEqual([b["bench"] for b in rep["benches"]], ["B1", "B6"])
        self.assertEqual(rep["failures"], [])
        for k in ("calibration", "grid", "renderer"):
            self.assertIn(k, rep)
        self.assertTrue((d / "SUMMARY.md").exists())
        self.assertTrue((d / "rows.jsonl").exists())
        self.assertEqual(bool(rep["profile"]), rc == 0)
        for b in rep["benches"]:
            self.assertGreaterEqual(b["cpu_ms_per_frame"], 0)
        # static scene must never produce MOVED truth tiles
        self.assertNotIn("3", [t for t, v in rep["benches"][1]["metrics"]["tl"].items() if v["n"]])


if __name__ == "__main__":
    unittest.main()
