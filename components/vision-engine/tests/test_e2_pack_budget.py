"""FX-E r1 RW3-2/RW3-3: ADV-13 run/retry allowances, each clause tested independently."""
import unittest
from visioncore import pack
from visioncore.errors import VqaError
from visioncore.params import PARAMS

CALL, RUN, RETRY = PARAMS["PACK_CALL_TOK"], PARAMS["PACK_RUN_TOK"], PARAMS["PACK_RETRY_TOK"]


class BudgetTests(unittest.TestCase):
    def refused(self, b, tokens, retry=False):
        with self.assertRaises(VqaError) as cm:
            b.charge(tokens, retry=retry)
        self.assertEqual((cm.exception.code, cm.exception.rc), ("E_BUDGET", 6))

    def test_many_small_calls_accepted_until_run_cap(self):
        b = pack.RunTokenBudget()
        unit = RUN // 62
        n = RUN // unit
        for _ in range(n):
            b.charge(unit)
        self.assertEqual(b.tokens, unit * n)
        self.assertGreater(n, 4)
        b.charge(RUN - b.tokens)
        self.assertEqual(b.tokens, RUN)
        self.refused(b, 1)

    def test_single_call_over_cap_refused_on_fresh_budget(self):
        b = pack.RunTokenBudget()
        self.refused(b, CALL + 1)
        self.assertEqual(vars(b), vars(pack.RunTokenBudget()))
        b.charge(CALL)

    def test_primary_run_cap_independent_of_call_count(self):
        b = pack.RunTokenBudget()
        for _ in range(RUN // CALL):
            b.charge(CALL)
        self.refused(b, RUN - b.tokens + 1)

    def test_retry_count_cap_below_token_cap(self):
        b = pack.RunTokenBudget()
        b.charge(1000)
        b.charge(10, retry=True)
        b.charge(10, retry=True)
        self.assertLess(b.tokens, RETRY)
        self.refused(b, 10, retry=True)

    def test_retry_total_cap(self):
        b = pack.RunTokenBudget()
        for _ in range(RUN // CALL):
            b.charge(CALL)
        b.charge(CALL, retry=True)
        self.refused(b, RETRY - b.tokens + 1, retry=True)
        b.charge(RETRY - b.tokens, retry=True)
        self.assertEqual(b.tokens, RETRY)

    def test_primary_cap_holds_after_a_retry(self):
        b = pack.RunTokenBudget()
        b.charge(CALL)
        b.charge(1000, retry=True)
        for _ in range(RUN // CALL - 1):
            b.charge(CALL)
        self.assertLess(b.tokens, RETRY)
        self.refused(b, 1)
