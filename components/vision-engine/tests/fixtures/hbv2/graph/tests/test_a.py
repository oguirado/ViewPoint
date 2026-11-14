import unittest
from common import *


class TestA(unittest.TestCase):
    def test_f1(self):
        self.assertEqual(A.f1(2), 5)

    def test_u1(self):
        self.assertEqual(U.u1(2), 4)

    def test_child(self):
        self.assertEqual(A.Child().run(), 6)
