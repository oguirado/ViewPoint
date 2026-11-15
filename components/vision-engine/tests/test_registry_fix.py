import unittest

from visioncore import canon, ids, layout, params
from visioncore.errors import VqaError


class RegistryFixes(unittest.TestCase):
    def test_canon_rejects_nan_inf(self):  # RARCH-5
        for t in ("NaN", "Infinity", "[-Infinity]", '{"a":NaN}'):
            with self.assertRaises(VqaError, msg=t) as c:
                canon.loads(t)
            self.assertEqual((c.exception.code, c.exception.rc), ("E_FLOAT", 2))

    def test_ids_reject_trailing_newline(self):  # RARCH-9
        for s in ("f0:t0000\n", "f0:all\n", "r1:00a3\n", "g2\n", "V-EVT-000019\n", "r_000104_77abe901\n"):
            with self.assertRaises(VqaError, msg=repr(s)) as c:
                ids.parse_any(s)
            self.assertEqual(c.exception.code, "E_VC_ID_SYNTAX")
        self.assertEqual(ids.parse_any("f0:t0000")[0], "field")

    def test_profile_duplicate_casefold(self):  # RARCH-10
        with self.assertRaises(VqaError) as c:
            params.effective({"t0_q4": 40, "T0_Q4": 50})
        self.assertEqual((c.exception.code, c.exception.rc), ("E_SCHEMA", 2))
        self.assertEqual(params.effective({"t0_q4": 40})["T0_Q4"], 40)

    def test_layout_constants_come_from_params(self):  # RARCH-11
        for k in ("TILE_MIN_PX", "CELL_MAX", "AXIS_MAX"):
            self.assertEqual(getattr(layout, k), params.PARAMS[k])

    def test_track_key_formatter(self):  # RARCH-7
        tk = ids.track_key("operator_zero", "combat_hud", 1920, 1080, "ui", 0x0A40, 0x0A7F)
        self.assertEqual(tk, "T:operator_zero:combat_hud:1920x1080:ui:0a40-0a7f")
        self.assertEqual(ids.parse_any(tk)[0], "track_key")
        with self.assertRaises(VqaError):
            ids.track_key("Bad App", "s", 1, 1, "ui", 0, 1)


if __name__ == "__main__":
    unittest.main()
