"""CP3: judge request batching (one call per pack), static prefix stability, response cache hit/miss."""
import shutil
import tempfile
import unittest

from visioncore import pack as P
from visioncore.errors import VqaError
from tests.test_pack import batch, fixture


class JudgeBatchTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cp3_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def pk(self, n=3, **kw):
        gops, regs = fixture()
        return P.pack(batch(gops[:n], regs), **kw)

    def test_prefix_stable_across_batches(self):
        a = P.build_request(self.pk(3), b"RUBRIC")
        b = P.build_request(self.pk(2, batch_no=7), b"RUBRIC")
        self.assertEqual(a.prefix_sha256, b.prefix_sha256)
        self.assertTrue(a.body.startswith(a.prefix))
        self.assertNotEqual(a.request_sha256, b.request_sha256)
        self.assertEqual(a.prefix_sha256, P.canon.sha256_bytes(a.prefix))
        self.assertNotEqual(P.build_request(self.pk(3), b"OTHER").prefix_sha256, a.prefix_sha256)

    def test_request_deterministic(self):
        self.assertEqual(P.build_request(self.pk()).request_sha256, P.build_request(self.pk()).request_sha256)

    def test_hit_miss_one_call(self):
        cache, calls = P.ResponseCache(self.d), []
        budget = P.RunTokenBudget()

        def call(req):
            calls.append(req.request_sha256)
            return b'{"verdict":"PASS"}'
        r1, rc1 = P.judge_batch(self.pk(), call, cache, budget=budget)
        r2, rc2 = P.judge_batch(self.pk(), call, P.ResponseCache(self.d), budget=budget)
        self.assertEqual((rc1["cache"], rc2["cache"]), ("miss", "hit"))
        self.assertEqual(r1, r2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(budget.calls, 1)  # cache hits do not consume the run allowance
        self.assertEqual(rc1["request_sha256"], rc2["request_sha256"])
        _, rc3 = P.judge_batch(self.pk(2), call, cache, budget=budget)
        self.assertEqual(rc3["cache"], "miss")
        self.assertEqual(len(calls), 2)
        self.assertEqual(budget.calls, 2)
        self.assertEqual((cache.hits, cache.misses), (0, 2))

    def test_negative(self):
        cache = P.ResponseCache(self.d)
        for bad in ("../x", "ab", "G" * 64):
            with self.assertRaises(VqaError):
                cache.get(bad)
        with self.assertRaises(VqaError):
            P.judge_batch(self.pk(), lambda r: "text", cache, budget=P.RunTokenBudget())
        with self.assertRaises(VqaError):
            P.build_request(self.pk(), "str")
        self.assertIsNone(cache.get("0" * 64))


if __name__ == "__main__":
    unittest.main()
