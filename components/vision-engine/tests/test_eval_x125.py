import itertools
import json
import tempfile
import unittest
from pathlib import Path

from eval import x1_determinism as x1, x2_crossrun as x2, x5_cpu as x5

SMALL = dict(W=256, H=160, n=16)


class X1(unittest.TestCase):
    def test_pass_and_rows(self):
        with tempfile.TemporaryDirectory() as d:
            rows, s = x1.run(d, repeats=2, captures=(("clean_static", 1), ("scroll", 1)), **SMALL)
            self.assertEqual(s["verdict"], "PASS")
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(r["schema"] == "vqa.eval-row/v1" and r["x"] == "X1" and r["rc"] == 0 for r in rows))
            lines = (Path(d) / "X1.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 4)
            json.loads(lines[0])
            self.assertEqual(json.loads((Path(d) / "X1.summary.json").read_text())["gate"], "G-E1")

    def test_nondeterminism_fails(self):
        c = itertools.count()
        rows, s = x1.run(None, repeats=3, captures=(("clean_static", 1),), W=256, H=160, n=16,
                         digest=lambda *a: {"tiles.bin": str(next(c) // 2)})   # FX4: serial+parallel call per repeat; both arms agree, repeats drift
        self.assertEqual(s["verdict"], "FAIL")
        self.assertEqual([r["rc"] for r in rows], [0, 5, 5])

    def test_cli_rc(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(x1.main(["--out", d, "--repeats", "1"]), 0)


class X2(unittest.TestCase):
    def test_aligned_below_limit(self):
        rows, s = x2.run(None, pairs=(("clean_static", 1), ("gpu_jitter", 1)), **SMALL)
        self.assertEqual(s["granularity"], "tile")
        self.assertTrue(all(r["changed_ppm"] <= x2.LIMIT_PPM for r in rows))

    def test_misaligned_control_is_high(self):
        rows, _ = x2.run(None, pairs=(("scroll", 1),), **SMALL)
        self.assertGreater(rows[0]["control_shift1_ppm"], x2.LIMIT_PPM)
        self.assertGreater(rows[0]["control_shift1_ppm"], rows[0]["changed_ppm"])


class X5(unittest.TestCase):
    def test_pass_and_fail_limits(self):
        P95 = {"256x160": 10 ** 9}   # FX4: the gate fails closed without a p95 limit per size
        _, ok = x5.run(None, sizes=((256, 160, 10 ** 9),), n=16, repeats=1, p95_limits=P95)
        self.assertEqual(ok["verdict"], "PASS")
        _, nop95 = x5.run(None, sizes=((256, 160, 10 ** 9),), n=16, repeats=1, p95_limits={})
        self.assertEqual(nop95["verdict"], "FAIL")
        _, badp = x5.run(None, sizes=((256, 160, 10 ** 9),), n=16, repeats=1, p95_limits={"256x160": 0})
        self.assertEqual(badp["verdict"], "FAIL")
        rows, bad = x5.run(None, sizes=((256, 160, 0),), n=16, repeats=1)
        self.assertEqual(bad["verdict"], "FAIL")
        self.assertEqual(rows[0]["frames"], 15)
        self.assertGreater(rows[0]["cpu_ms_frame_med_milli"], 0)


if __name__ == "__main__":
    unittest.main()
