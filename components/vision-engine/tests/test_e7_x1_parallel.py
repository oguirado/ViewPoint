import unittest
from unittest import mock

from eval import x1_determinism as x1
from visioncore import pipeline

SMALL = dict(W=256, H=160, n=16)
CAP = (("clean_static", 1), ("scroll", 1))


class X1Parallel(unittest.TestCase):
    def test_x1_exercises_parallel_arm(self):
        seen = []
        real = pipeline.analyze

        def spy(src, *a, **k):
            seen.append(k.get("workers", 1))
            return real(src, *a, **k)

        with mock.patch.object(pipeline, "analyze", spy):
            rows, s = x1.run(None, repeats=2, captures=CAP, **SMALL)
        self.assertEqual(s["verdict"], "PASS")
        self.assertEqual(sorted(set(seen)), [1, x1.PAR_WORKERS])
        self.assertEqual(seen.count(1), seen.count(x1.PAR_WORKERS), 4)
        for r in rows:
            self.assertEqual((r["workers_serial"], r["workers_parallel"]), (1, x1.PAR_WORKERS))
            self.assertEqual(r["digest_sha256"], r["digest_parallel_sha256"])
        self.assertEqual(s["arms"], {"serial": 1, "parallel": x1.PAR_WORKERS})

    def test_x1_parallel_only_corruption_fails(self):
        def digest(name, seed, W, H, n, workers=1):
            return {"tiles.bin": "x" if workers == 1 else "y"}
        rows, s = x1.run(None, repeats=2, captures=CAP[:1], digest=digest, **SMALL)
        self.assertEqual(s["verdict"], "FAIL")
        self.assertEqual([r["rc"] for r in rows], [5, 5])

    def test_x1_no_parallel_arm_fails(self):
        rows, s = x1.run(None, repeats=1, captures=CAP[:1], par_workers=1, **SMALL)
        self.assertEqual(s["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
