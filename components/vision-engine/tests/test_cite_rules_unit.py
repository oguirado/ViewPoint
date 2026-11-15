"""Unit tests for S6 citation rule helpers in visioncore/cite.py."""
import struct
import unittest

from visioncore import cite, layout

W, H = 32, 32  # 2x2 grid (16x16 tiles)
GRID = layout.grid_ident(W, H)
PSHA = "12" * 32
API = "1.1.0"


def _make_tile_bytes(m, cls=0, flags=0, mv=(0, 0)):
    return struct.pack(layout.TILE_FMT, m, cls, flags, 0, 0, 0, 0, 0, 0, 0, mv[0], mv[1], 0, 0)


def _build_test_vision():
    # 2x2 grid morton order: (0,0)=0, (1,0)=1, (0,1)=2, (1,1)=3
    # 3 frames: frame 0, 1, 2
    tile_recs = []
    # Frame 0: tile 0 is FROZEN (cls=5), tile 1,2,3 unchanged
    tile_recs.append(_make_tile_bytes(0, cls=5, flags=0, mv=(0, 0)))
    for m in range(1, 4):
        tile_recs.append(_make_tile_bytes(m, cls=0, flags=0, mv=(0, 0)))

    # Frame 1: tile m=0 (0,0) is FROZEN, m=1 (1,0) is FLICKER, m=2 (0,1) is CHANGED with edge, m=3 (1,1) is MOVED
    tile_recs.append(_make_tile_bytes(0, cls=5, flags=0, mv=(0, 0)))      # FROZEN (cls_id 5)
    tile_recs.append(_make_tile_bytes(1, cls=4, flags=0, mv=(0, 0)))      # FLICKER (cls_id 4)
    tile_recs.append(_make_tile_bytes(2, cls=2, flags=1, mv=(0, 0)))      # CHANGED (cls_id 2), edge flag=1
    tile_recs.append(_make_tile_bytes(3, cls=3, flags=1, mv=(2, -1)))    # MOVED (cls_id 3), edge flag=1, mv=(2,-1)

    # Frame 2: all unchanged
    for m in range(4):
        tile_recs.append(_make_tile_bytes(m, cls=0, flags=0, mv=(0, 0)))

    tiles_bin = b"".join(tile_recs)

    frames = [
        {"frame": 0, "n": [0, 0, 1, 0, 0, 0, 0]},
        {"frame": 1, "n": [0, 0, 1, 1, 1, 1, 0]},
        {"frame": 2, "n": [4, 0, 0, 0, 0, 0, 0]},
    ]

    regions = [
        {
            "id": "r1:0000", "frame": 1, "cls": "FROZEN", "edge_ppm": 0, "mv": [0, 0],
            "spans": [["0000", "0000"]]
        },
        {
            "id": "r1:0001", "frame": 1, "cls": "FLICKER", "edge_ppm": 0, "mv": [0, 0],
            "spans": [["0001", "0001"]]
        },
        {
            "id": "r1:0002", "frame": 1, "cls": "CHANGED", "edge_ppm": 500000, "mv": [0, 0],
            "spans": [["0002", "0002"]]
        },
        {
            "id": "r1:0003", "frame": 1, "cls": "MOVED", "edge_ppm": 500000, "mv": [2, -1],
            "spans": [["0003", "0003"]]
        },
    ]

    ktrack1 = "k1:0001"
    ktrack2 = "k1:0003"
    gops = [
        {
            "g": 0, "f0": 0, "n_frames": 2, "cut": False, "params_sha256": PSHA,
            "tracks": [
                {
                    "k": ktrack1, "first": 1, "last": 1, "cls_mask": 16, "flags": ["FLICKER", "CYCLE"], "period": 3,
                    "r_rep": "r1:0001", "mv_mode": [0, 0]
                },
                {
                    "k": ktrack2, "first": 1, "last": 1, "cls_mask": 8, "flags": ["APPEAR", "VANISH"], "period": 0,
                    "r_rep": "r1:0003", "mv_mode": [2, -1]
                },
            ],
            "cycles": [{"t": "f1:t0001", "p": 3}],
            "events": []
        },
        {
            "g": 1, "f0": 2, "n_frames": 1, "cut": True, "params_sha256": PSHA,
            "tracks": [],
            "cycles": [],
            "events": [["FLASH", "f2:all"]]
        }
    ]

    return cite.build_vision(W, H, tiles_bin, frames, regions, gops, PSHA, API)


def _make_test_pack():
    return {
        "schema": "vqa.pack/v2",
        "grid": GRID,
        "params_sha256": PSHA,
        "api_version": API,
        "frame_map": [[0, 0], [1, 1], [2, 2]],
        "gops": [0, 1]
    }


class CiteRulesUnitTest(unittest.TestCase):
    def setUp(self):
        self.v = _build_test_vision()
        self.pack = _make_test_pack()
        self.win = cite.window_frames(self.v, self.pack)

    def test_tile(self):
        # Line 79
        # Positive case: valid tile unpack
        tile0 = cite._tile(self.v, 1, 0)
        self.assertIsNotNone(tile0)
        self.assertEqual(tile0["morton"], 0)
        self.assertEqual(tile0["cls"], 5)  # FROZEN

        tile3 = cite._tile(self.v, 1, 3)
        self.assertIsNotNone(tile3)
        self.assertEqual(tile3["mv_dx"], 2)
        self.assertEqual(tile3["mv_dy"], -1)

        # Negative cases:
        # 1) Non-existent frame
        self.assertIsNone(cite._tile(self.v, 99, 0))
        # 2) Non-existent morton index in grid
        self.assertIsNone(cite._tile(self.v, 1, 99))
        # 3) Short tiles buffer
        v_short = dict(self.v, tiles=self.v["tiles"][:10])
        self.assertIsNone(cite._tile(v_short, 1, 0))

    def test_spans_tiles(self):
        # Line 90
        # Positive case: row with spans converting hex morton to (x, y)
        row = {"spans": [["0000", "0001"], ["0003", "0003"]]}
        tiles = cite._spans_tiles(row)
        # morton 0=(0,0), 1=(1,0), 3=(1,1)
        self.assertEqual(tiles, {(0, 0), (1, 0), (1, 1)})

        # Negative case: empty spans or missing spans key
        self.assertEqual(cite._spans_tiles({}), set())
        self.assertEqual(cite._spans_tiles({"spans": []}), set())

    def test_cls_of_mask(self):
        # Line 132
        # Positive case: cls_mask bit flags -> class strings
        # Bit 2 (4): CHANGED, Bit 3 (8): MOVED, Bit 4 (16): FLICKER, Bit 5 (32): FROZEN
        mask = (1 << 2) | (1 << 4) | (1 << 5)
        self.assertEqual(cite._cls_of_mask(mask), {"CHANGED", "FLICKER", "FROZEN"})

        # Negative case: mask = 0 -> empty set
        self.assertEqual(cite._cls_of_mask(0), set())

    def test_facts(self):
        # Line 136
        # Field facts
        # Positive: f1:t0000 has no cycle
        d_f1_t0 = {"frame": 1, "morton": 0}
        facts_f0 = cite._facts(self.v, "field", d_f1_t0)
        self.assertEqual(facts_f0, {
            "exists": True, "cls": {"FROZEN"}, "trk": set(), "edge": False,
            "all_unch": False, "cut": False, "flash": False, "cycle": False,
            "mv": (0, 0), "period": 0
        })

        # Positive: f1:t0001 has cycle
        d_f1_t1 = {"frame": 1, "morton": 1}
        facts_f1 = cite._facts(self.v, "field", d_f1_t1)
        self.assertEqual(facts_f1["cycle"], True)
        self.assertEqual(facts_f1["period"], 3)

        # Field negative
        facts_f_absent = cite._facts(self.v, "field", {"frame": 99, "morton": 0})
        self.assertEqual(facts_f_absent, {
            "exists": False, "cls": set(), "trk": set(), "edge": False,
            "all_unch": False, "cut": False, "flash": False, "cycle": False,
            "mv": (0, 0), "period": 0
        })

        # Region facts
        # Positive
        d_r1_0003 = {"frame": 1, "morton": 3}
        facts_r3 = cite._facts(self.v, "region", d_r1_0003)
        self.assertEqual(facts_r3, {
            "exists": True, "cls": {"MOVED"}, "trk": {"APPEAR", "VANISH"}, "edge": True,
            "all_unch": False, "cut": False, "flash": False, "cycle": False,
            "mv": (2, -1), "period": 0
        })

        # Region negative
        facts_r_absent = cite._facts(self.v, "region", {"frame": 1, "morton": 99})
        self.assertFalse(facts_r_absent["exists"])

        # Global facts
        # Positive: frame 2 has cut and flash
        facts_g2 = cite._facts(self.v, "global", {"frame": 2})
        self.assertEqual(facts_g2["cut"], True)
        self.assertEqual(facts_g2["flash"], True)

        # Global negative
        facts_g_absent = cite._facts(self.v, "global", {"frame": 99})
        self.assertFalse(facts_g_absent["exists"])

        # Track facts
        # Positive
        d_k1_0001 = {"frame": 1, "morton": 1}
        facts_t1 = cite._facts(self.v, "track", d_k1_0001)
        self.assertEqual(facts_t1, {
            "exists": True, "cls": {"FLICKER"}, "trk": {"FLICKER", "CYCLE"}, "edge": False,
            "all_unch": False, "cut": False, "flash": False, "cycle": True,
            "mv": (0, 0), "period": 3
        })

        # Track negative
        facts_t_absent = cite._facts(self.v, "track", {"frame": 1, "morton": 99})
        self.assertFalse(facts_t_absent["exists"])

        # GOP facts
        # Positive
        facts_gop0 = cite._facts(self.v, "gop", {"gop": 0})
        self.assertEqual(facts_gop0, {
            "exists": True, "cls": {"FLICKER", "MOVED"}, "trk": {"FLICKER", "CYCLE", "APPEAR", "VANISH"},
            "edge": False, "all_unch": False, "cut": False, "flash": False, "cycle": True,
            "mv": (0, 0), "period": 0
        })

        # GOP negative
        facts_gop_absent = cite._facts(self.v, "gop", {"gop": 99})
        self.assertFalse(facts_gop_absent["exists"])

    def test_evidence(self):
        # Line 185
        # Rule 5 exact mapping:
        # static_content -> f["exists"]
        f_exists = {"exists": True, "cls": set(), "trk": set(), "all_unch": False, "cycle": False, "edge": False, "cut": False, "flash": False}
        f_nonexist = dict(f_exists, exists=False)
        self.assertTrue(cite._evidence("static_content", f_exists))
        self.assertFalse(cite._evidence("static_content", f_nonexist))

        # frozen -> FROZEN in cls or trk or all_unch
        self.assertTrue(cite._evidence("frozen", dict(f_exists, cls={"FROZEN"})))
        self.assertTrue(cite._evidence("frozen", dict(f_exists, trk={"FROZEN"})))
        self.assertTrue(cite._evidence("frozen", dict(f_exists, all_unch=True)))
        self.assertFalse(cite._evidence("frozen", f_exists))

        # flicker -> FLICKER in cls or trk & {FLICKER, CYCLE} or cycle
        self.assertTrue(cite._evidence("flicker", dict(f_exists, cls={"FLICKER"})))
        self.assertTrue(cite._evidence("flicker", dict(f_exists, trk={"CYCLE"})))
        self.assertTrue(cite._evidence("flicker", dict(f_exists, cycle=True)))
        self.assertFalse(cite._evidence("flicker", f_exists))

        # teleport -> trk & {APPEAR, VANISH} or MOVED in cls
        self.assertTrue(cite._evidence("teleport", dict(f_exists, trk={"APPEAR"})))
        self.assertTrue(cite._evidence("teleport", dict(f_exists, cls={"MOVED"})))
        self.assertFalse(cite._evidence("teleport", f_exists))

        # overlap -> (cls & {CHANGED, MOVED}) and edge
        self.assertTrue(cite._evidence("overlap", dict(f_exists, cls={"CHANGED"}, edge=True)))
        self.assertTrue(cite._evidence("overlap", dict(f_exists, cls={"MOVED"}, edge=True)))
        self.assertFalse(cite._evidence("overlap", dict(f_exists, cls={"CHANGED"}, edge=False)))
        self.assertFalse(cite._evidence("overlap", f_exists))

        # drift -> DRIFT in cls or trk
        self.assertTrue(cite._evidence("drift", dict(f_exists, cls={"DRIFT"})))
        self.assertTrue(cite._evidence("drift", dict(f_exists, trk={"DRIFT"})))
        self.assertFalse(cite._evidence("drift", f_exists))

        # missing -> CHANGED in cls or VANISH in trk
        self.assertTrue(cite._evidence("missing", dict(f_exists, cls={"CHANGED"})))
        self.assertTrue(cite._evidence("missing", dict(f_exists, trk={"VANISH"})))
        self.assertFalse(cite._evidence("missing", f_exists))

        # black -> cut or flash
        self.assertTrue(cite._evidence("black", dict(f_exists, cut=True)))
        self.assertTrue(cite._evidence("black", dict(f_exists, flash=True)))
        self.assertFalse(cite._evidence("black", f_exists))

        # Unknown / unsupported reason class -> False
        self.assertFalse(cite._evidence("unsupported_reason", f_exists))

    def test_render_quote(self):
        # Line 207
        # Field quote with period (f1:t0001 has cycle period=3)
        q_field = cite.render_quote("f1:t0001", self.v, "field", {"frame": 1, "morton": 1})
        self.assertEqual(q_field, "f1:t0001 FLICKER p=3")

        # Region quote with mv
        q_region = cite.render_quote("r1:0003", self.v, "region", {"frame": 1, "morton": 3})
        self.assertEqual(q_region, "r1:0003 MOVED mv=2,-1")

        # Track quote
        q_track = cite.render_quote("k1:0001", self.v, "track", {"frame": 1, "morton": 1})
        self.assertEqual(q_track, "k1:0001 FLICKER p=3")

        # Global quote
        q_global = cite.render_quote("f0:all", self.v, "global", {"frame": 0})
        self.assertEqual(q_global, "f0:all ALL")

        # Non-existent field pointer (fallback UNCHANGED)
        q_nonexist = cite.render_quote("f9:t0000", self.v, "field", {"frame": 9, "morton": 0})
        self.assertEqual(q_nonexist, "f9:t0000 UNCHANGED")

    def test_pointer_tiles(self):
        # Line 279
        # Field
        tiles_f = cite._pointer_tiles(self.v, "field", {"morton": 2})
        self.assertEqual(tiles_f, {(0, 1)})

        # Region
        tiles_r = cite._pointer_tiles(self.v, "region", {"frame": 1, "morton": 3})
        self.assertEqual(tiles_r, {(1, 1)})

        # Track
        tiles_t = cite._pointer_tiles(self.v, "track", {"frame": 1, "morton": 1})
        self.assertEqual(tiles_t, {(1, 0)})

        # Global / GOP (None -> whole frame)
        self.assertIsNone(cite._pointer_tiles(self.v, "global", {"frame": 0}))
        self.assertIsNone(cite._pointer_tiles(self.v, "gop", {"gop": 0}))

        # Non-existent region/track (empty set)
        self.assertEqual(cite._pointer_tiles(self.v, "region", {"frame": 9, "morton": 0}), set())

    def test_flagged_tiles(self):
        # Line 291
        # Positive: "frozen" reason class looking at frame 1 -> region r1:0000 (morton 0 -> (0,0))
        flagged_frozen = cite._flagged_tiles(self.v, "frozen", {1})
        self.assertEqual(flagged_frozen, {(0, 0)})

        # Positive: "flicker" reason class in frame 1 -> region r1:0001 and track k1:0001 (morton 1 -> (1,0))
        flagged_flicker = cite._flagged_tiles(self.v, "flicker", {1})
        self.assertEqual(flagged_flicker, {(1, 0)})

        # Negative: frame set {2} has no flagged regions/tracks
        self.assertEqual(cite._flagged_tiles(self.v, "frozen", {2}), set())

    def test_relevant(self):
        # Line 306
        # static_content always relevant -> True
        self.assertTrue(cite._relevant(self.v, "static_content", "field", {"morton": 0}, {0}, {}))

        # black -> depends on cut or flash
        self.assertTrue(cite._relevant(self.v, "black", "global", {"frame": 2}, {2}, {"cut": True, "flash": False}))
        self.assertFalse(cite._relevant(self.v, "black", "global", {"frame": 0}, {0}, {"cut": False, "flash": False}))

        # Spatial relevance: tile within Moore ring (+-1) of flagged tile
        # Flagged tile for frozen in frame 1 is (0,0). Pointer tile (1,0) is neighbor -> relevant True
        d_field_neighbor = {"frame": 1, "morton": 1}
        f_field_neighbor = cite._facts(self.v, "field", d_field_neighbor)
        self.assertTrue(cite._relevant(self.v, "frozen", "field", d_field_neighbor, {1}, f_field_neighbor))

        # Negative case: no flagged tiles for drift -> relevant False
        self.assertFalse(cite._relevant(self.v, "drift", "field", d_field_neighbor, {1}, f_field_neighbor))

    def test_check_pointer(self):
        # Line 321
        # Test rules 1-6 precedence order
        # Rule 1: E_CITE_SYNTAX (malformed pointer string)
        ok, code, kind, d = cite._check_pointer(self.v, "invalid_ptr", "frozen", None, self.win)
        self.assertEqual((ok, code), (False, "E_CITE_SYNTAX"))

        # Rule 2: E_CITE_UNKNOWN (unknown/out-of-bounds pointer)
        ok, code, kind, d = cite._check_pointer(self.v, "f9:t0000", "frozen", None, self.win)
        self.assertEqual((ok, code), (False, "E_CITE_UNKNOWN"))

        # Rule 3: E_CITE_WINDOW (pointer outside window)
        win_narrow = cite.Window({0}, {0}, {0}, set())
        ok, code, kind, d = cite._check_pointer(self.v, "f1:t0000", "frozen", None, win_narrow)
        self.assertEqual((ok, code), (False, "E_CITE_WINDOW"))

        # Rule 4: E_CITE_QUOTE (quote mismatch)
        ok, code, kind, d = cite._check_pointer(self.v, "f1:t0000", "frozen", "f1:t0000 WRONG_QUOTE", self.win)
        self.assertEqual((ok, code), (False, "E_CITE_QUOTE"))

        # Rule 5: E_CITE_CLASS (vision check on static_content or reason evidence mismatch)
        ok, code, kind, d = cite._check_pointer(self.v, "f1:t0000", "static_content", None, self.win, check_id="render.flicker")
        self.assertEqual((ok, code), (False, "E_CITE_CLASS"))

        ok, code, kind, d = cite._check_pointer(self.v, "f1:t0000", "teleport", None, self.win)
        self.assertEqual((ok, code), (False, "E_CITE_CLASS"))

        # Rule 6: E_CITE_IRRELEVANT (relevance mismatch)
        # f0:t0000 has evidence (FROZEN in cls), frame 0 is in win_f0.seen={0}, but for reason "drift" there are no flagged tiles in frame 0.
        win_f0 = cite.Window({0}, {0}, {0}, set())
        ok, code, kind, d = cite._check_pointer(self.v, "f0:t0000", "drift", None, win_f0)
        self.assertEqual((ok, code), (False, "E_CITE_CLASS"))  # Wait! f0:t0000 is FROZEN, reason is "drift", so Rule 5 (evidence) fails with E_CITE_CLASS first!

        # To reach Rule 6: evidence must pass, but relevance fails!
        # f0:t0000 has evidence for "frozen" (cls FROZEN). In win_f0 ({0}), _flagged_tiles(v, "frozen", {0}) looks at regions/tracks in frame 0.
        # There are NO regions/tracks in frame 0 in self.v!
        # So _flagged_tiles for frame 0 is empty set!
        # So evidence passes (FROZEN in cls), but _relevant returns False!
        ok, code, kind, d = cite._check_pointer(self.v, "f0:t0000", "frozen", None, win_f0)
        self.assertEqual((ok, code), (False, "E_CITE_IRRELEVANT"))

        # Successful pass
        q_ok = cite.render_quote("f1:t0000", self.v, "field", {"frame": 1, "morton": 0})
        ok, code, kind, d = cite._check_pointer(self.v, "f1:t0000", "frozen", q_ok, self.win)
        self.assertEqual((ok, code, kind), (True, None, "field"))

    def test_result_and_verdict(self):
        # Line 372 (_result) and Line 443 (_verdict)
        # 1) All accepted
        res_ok = {"check_id": "c1", "status": "FAIL", "reason_class": "frozen", "pointers": ["f1:t0000"]}
        out, codes, accepted = cite._result(self.v, res_ok, self.win)
        self.assertTrue(accepted)
        self.assertEqual(codes, set())
        self.assertEqual(out["status_out"], "FAIL")
        self.assertEqual(out["accepted_pointers"], ["f1:t0000"])

        v_ok = cite.verify_citations(self.v, {"results": [res_ok]}, self.pack)
        self.assertEqual(v_ok["rc"], 0)
        self.assertEqual(v_ok["receipt"], "CV:PASS")

        # 2) One rejected
        res_rej = {"check_id": "c2", "status": "FAIL", "reason_class": "frozen", "pointers": ["f9:t0000"]}
        out_rej, codes_rej, accepted_rej = cite._result(self.v, res_rej, self.win)
        self.assertFalse(accepted_rej)
        self.assertIn("E_CITE_UNKNOWN", codes_rej)
        self.assertIn("E_CITE_NONE", codes_rej)
        self.assertEqual(out_rej["status_out"], "CANNOT_SEE")

        v_rej = cite.verify_citations(self.v, {"results": [res_rej]}, self.pack)
        self.assertEqual(v_rej["rc"], 3)
        self.assertEqual(v_rej["receipt"], "CV:FAIL:UK,NO")

        # 3) Malformed claim / overflow pointers (>8)
        res_overflow = {"check_id": "c3", "status": "FAIL", "reason_class": "frozen", "pointers": ["f1:t0000"] * 9}
        out_of, codes_of, _ = cite._result(self.v, res_overflow, self.win)
        self.assertEqual(len(out_of["pointers"]), 9)
        self.assertEqual(out_of["pointers"][8]["code"], "E_CITE_SYNTAX")

        # 4) Stale pack
        stale_pack = dict(self.pack, params_sha256="00" * 32)
        v_stale = cite.verify_citations(self.v, {"results": [res_ok]}, stale_pack)
        self.assertEqual(v_stale["rc"], 4)
        self.assertEqual(v_stale["code"], "E_STALE")


if __name__ == "__main__":
    unittest.main()
