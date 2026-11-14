"""JCODE-1 (JC-6 residual): SPEC_INT R1 / T-R1-neg - pyramid node level L8 is legal, L9 is not."""
import unittest

from visioncore import ids, pyramid
from visioncore.errors import VqaError


class NodeLevelL8(unittest.TestCase):
    def test_format_parse_l8(self):
        s = ids.node_id(3, 8, 0)
        self.assertEqual(s, "p3:L8:0000")
        self.assertEqual(ids.parse_any(s, 4080, 256), ("node", {"frame": 3, "level": 8, "morton": 0}))
        self.assertEqual(ids.parse_any(s, 4080, 16)[1]["level"], 8)

    def test_l9_rejected(self):
        with self.assertRaises(VqaError) as c:
            ids.parse_any("p3:L9:0000", 4080, 256)
        self.assertEqual(c.exception.code, "E_VC_ID_SYNTAX")
        with self.assertRaises(VqaError) as c:
            ids.node_id(3, 9, 0)
        self.assertEqual(c.exception.code, "E_VC_ID_RANGE")

    def test_level_cap_reaches_l8(self):
        self.assertEqual(pyramid.id_level_cap(pyramid.levels_for(255, 1)), 8)


if __name__ == "__main__":
    unittest.main()
