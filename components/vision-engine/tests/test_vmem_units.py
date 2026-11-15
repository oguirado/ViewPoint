import hashlib
import struct
import unittest

from visioncore import layout
from visioncore.errors import VqaError
from vmem import units as U

SC = {"app": "operator_zero", "scene": "combat_hud", "res": "1920x1080", "grid": "grid:1920x1080:s32", "build_seq": 7}
TK = U.track_key("operator_zero", "combat_hud", 1920, 1080, "ui", 0x0a40, 0x0a7f)
SHA = "ab" * 32


def anchor(run="r_000007_77abe901", crop=None, desc=None):
    return {"run_id": run, "cite": run + "/f84:t0a40", "frame": 84, "render_index": 84, "bbox_px": [0, 0, 32, 32],
            "mv_px": [0, 0], "xh64": "0" * 16, "sig64": "f" * 16, "crop": crop, "desc_blob": desc}


def mk(n=1, kind="baseline", **kw):
    kw.setdefault("anchors", [anchor()])
    return U.make_unit(n, kind, dict(SC), TK, **kw)


def tile(sig, xh, y=100, sq=50):
    return struct.pack(layout.TILE_FMT, 0, 0, 0, y, 0, 0, sq, 0, 0, 0, 0, 0, sig, xh)


class TestUnits(unittest.TestCase):
    def test_make_and_canonical(self):
        u = mk()
        self.assertEqual(u["layer"], "BG")
        self.assertTrue(U.dump_unit(u).endswith("\n"))
        self.assertLessEqual(len(U.dump_unit(u)), U.UNIT_MAX_B)
        self.assertEqual(U.load_units(U.dump_units([u])), [u])

    def test_fg_iff_pin(self):
        u = mk(pin="FAIL")
        self.assertEqual(u["layer"], "FG")
        with self.assertRaises(VqaError) as c:
            U.validate_unit(dict(u, layer="BG"))
        self.assertEqual(c.exception.rc, 2)
        with self.assertRaises(VqaError):
            U.validate_unit(dict(mk(), layer="FG"))

    def test_negatives(self):
        u = mk()
        for bad in (dict(u, schema="x"), dict(u, id="V-EVT-000001"), dict(u, **{"class": "UNCH"}),
                    dict(u, anchors=[anchor()] * 7), dict(u, value_den=0), dict(u, summary="x" * 161),
                    dict(u, tk="T:bad"), dict(u, value_num=1.5), dict(u, status="retired", layer="FG", pin="FAIL")):
            with self.assertRaises(VqaError):
                U.validate_unit(bad)
        with self.assertRaises(VqaError) as c:
            U.validate_unit(dict(u, summary="x" * 160, subject="y" * 48, value="z" * 64,
                                 anchors=[anchor()] * 6, check_id="c" * 64))
        self.assertEqual(c.exception.rc, 6)  # > 1,024 B
        with self.assertRaises(VqaError) as c:
            U.validate_unit(dict(u, scope=dict(SC, app="Bad App")))
        self.assertEqual(c.exception.code, "E_USAGE")

    def test_apply_pins(self):
        v = mk(2, "verdict", verdict="FAIL", check_id="render.flicker")
        p = mk(3, "verdict", verdict="PASS")
        out = U.apply_pins([v, p])
        self.assertEqual([x["pin"] for x in out], ["FAIL", None])
        self.assertEqual(U.apply_pins([mk(4, "defect")])[0]["layer"], "FG")

    def test_catalogue_bijection(self):
        us = [mk(1), mk(2, pin="GOLDEN", summary="a\tb"), mk(3, "verdict", pin="FAIL", verdict="FAIL")]
        txt = U.render_catalogue(us)
        self.assertEqual(txt.count("\n"), 3)
        self.assertTrue(U.check_catalogue(us, txt))
        with self.assertRaises(VqaError) as c:  # row missing (M-VM7)
            U.check_catalogue(us, "\n".join(txt.split("\n")[:-2]) + "\n")
        self.assertEqual((c.exception.rc, c.exception.detail["reason"]), (5, "g_catalogue"))
        with self.assertRaises(VqaError):
            U.check_catalogue(us, txt + txt.split("\n")[1] + "\n")

    def test_look_eq(self):
        t_a = tile(0xFFFF, 1) * 20
        blobs = {"a" * 64: t_a}
        la = {"grid": SC["grid"], "n": 20, "ah64": "1" * 16, "desc_blob": "a" * 64, "y_q4_mean": 100}
        self.assertEqual(U.look_eq(la, dict(la)), "SAME_EXACT")
        # noisy: every sig differs by 2 bits, y by 3
        lb = dict(la, ah64="2" * 16, desc_blob="b" * 64)
        blobs["b" * 64] = (tile(0xFFFF ^ 3, 2, y=103)) * 20
        self.assertEqual(U.look_eq(la, lb, blobs.__getitem__), "SAME_NOISY")
        # 2 of 20 tiles differ -> 90% < 95% -> DIFFERENT; 1 of 20 -> 95% -> SAME_NOISY
        bad = tile(0, 9)
        blobs["c" * 64] = tile(0xFFFF ^ 3, 2, y=103) * 18 + bad * 2
        blobs["d" * 64] = tile(0xFFFF ^ 3, 2, y=103) * 19 + bad
        self.assertEqual(U.look_eq(la, dict(lb, desc_blob="c" * 64), blobs.__getitem__), "DIFFERENT")
        self.assertEqual(U.look_eq(la, dict(lb, desc_blob="d" * 64), blobs.__getitem__), "SAME_NOISY")
        # Hamming 4 -> not same
        blobs["e" * 64] = tile(0xFFFF ^ 0xF, 2, y=103) * 20
        self.assertEqual(U.look_eq(la, dict(lb, desc_blob="e" * 64), blobs.__getitem__), "DIFFERENT")
        # grid mismatch -> rc 4
        with self.assertRaises(VqaError) as c:
            U.look_eq(la, dict(lb, grid="grid:1280x720:s16"), blobs.__getitem__)
        self.assertEqual(c.exception.rc, 4)

    def test_track_key(self):
        self.assertEqual(TK, "T:operator_zero:combat_hud:1920x1080:ui:0a40-0a7f")
        self.assertEqual(U.tk_hash8(TK), hashlib.sha256(TK.encode()).hexdigest()[:8])
        with self.assertRaises(VqaError):
            U.track_key("a", "b", 1, 1, "bogus", 0, 1)


if __name__ == "__main__":
    unittest.main()
