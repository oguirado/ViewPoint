import unittest
from common import *
import d


class TestD(unittest.TestCase):
    def test_d2(self):
        self.assertEqual(d.d2(1), 5)

    def test_u1(self):
        self.assertEqual(d.d1(3), 6)
