import unittest

from bpy_side import canon
from blender_visual_qa import selftest
from blender_visual_qa.drive import journal

GOLDEN = {
    (0, 0, 0): 0, (0, 1, 0): 1371419539, (0, 0, 1): 2065550767,
    (1, 0, 0): 1824779127, (7, 3, 5): 1057599536, (0, 1048575, 1048575): 1320578262,
}


class SeedTwins(unittest.TestCase):
    def test_golden_vector(self):
        for t, v in GOLDEN.items():
            self.assertEqual(canon.cycles_seed(*t), v)
            self.assertEqual(journal.cycles_seed(*t), v)

    def test_linear_scheme_is_not_used(self):  # br.seed_lin.15
        self.assertNotEqual(canon.cycles_seed(0, 1, 0), canon.cycles_seed(0, 0, 1))

    def test_out_of_range_refused(self):
        for t in ((1 << 24, 0, 0), (0, 1 << 20, 0), (0, 0, -1), (0, 0, 1.5)):
            with self.assertRaises(ValueError):
                canon.cycles_seed(*t)

    def test_selftest_rows_ok(self):
        self.assertTrue(all(r["ok"] for r in selftest.run()))


if __name__ == "__main__":
    unittest.main()
