"""P1 W3 signflip: exact p for known deltas; claim only when p < 0.05; two-sided (M8)."""
import unittest
from fractions import Fraction as F


class TestSignFlip(unittest.TestCase):
    def test_all_positive_n6_p_1_32(self):
        from eval.hb_router.compare import sign_flip_p
        self.assertEqual(sign_flip_p([F(1)] * 6), F(1, 32))
        self.assertEqual(sign_flip_p([F(1)] * 6), sign_flip_p([F(-1)] * 6))

    def test_two_sided_symmetry(self):
        from eval.gui_synth import signflip
        d = [F(2), F(1), F(3), F(1), F(1), F(2), F(1)]
        pp = signflip.paired_p(d)
        pn = signflip.paired_p([-x for x in d])
        self.assertEqual(pp, pn)
        self.assertEqual(pp, F(2, 128))  # only +all / -all reach |sum|=11 of 2^7

    def test_zero_deltas_no_claim(self):
        from eval.gui_synth import signflip
        r = _compare([0, 0, 0, 0], [0, 0, 0, 0])
        self.assertEqual(r["p_exact"], "1/1")
        self.assertIn("no claim", r["claim"])

    def test_claim_only_below_005(self):
        r = _compare([1] * 6, [2] * 6)  # p = 1/32 < 0.05
        self.assertEqual(r["p_exact"], "1/32")
        self.assertNotIn("no claim", r["claim"])
        r = _compare([1] * 4, [2] * 4)  # p = 1/8
        self.assertEqual(r["p_exact"], "1/8")
        self.assertIn("no claim", r["claim"])
        r = _compare([1] * 6, [2] * 6, split=None)  # significant but not split-scoped: never a claim
        self.assertEqual(r["p_exact"], "1/32")
        self.assertIn("no claim", r["claim"])

    def test_uses_hb_router_sign_flip(self):
        from eval.gui_synth import signflip
        from eval.hb_router import compare
        self.assertIs(signflip.sign_flip_p, compare.sign_flip_p)


def _unit_result(vals):
    return {"units": {"GATE:s%d" % i: {"m": "%d/1" % v} for i, v in enumerate(vals)}}


def _compare(a, b, split="GATE"):
    from eval.gui_synth import signflip
    return signflip.compare(_unit_result(a), _unit_result(b), "m", split=split)


if __name__ == "__main__":
    unittest.main()
