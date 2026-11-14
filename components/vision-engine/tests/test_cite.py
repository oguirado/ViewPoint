"""S6 citation verifier tests (SPEC_TIME §10): positive, adversarial (model cites ids without looking), stale, files."""
import copy
import struct
import tempfile
import unittest
from pathlib import Path

from visioncore import canon, cite, ids, layout, params

W, H = 1280, 720
GRID = layout.grid_ident(W, H)
PSHA = "ab" * 32
API = "1.1.0"
M = lambda tx, ty: layout.morton(tx, ty)  # noqa: E731
FROZEN_T, FLICK_T, CHG_T, MOVE_T = (5, 5), (10, 10), (20, 20), (30, 30)


def _rec(m, cls=0, flags=0, mv=(0, 0)):
    return struct.pack(layout.TILE_FMT, m, cls, flags, 0, 0, 0, 0, 0, 0, 0, mv[0], mv[1], 0, 0)


def _tiles(special):
    s, gw, gh = layout.grid_params(W, H)
    ms = sorted(M(x, y) for y in range(gh) for x in range(gw))
    out = []
    for fr in range(3):
        for m in ms:
            c = special.get((fr, m), (0, 0, (0, 0)))
            out.append(_rec(m, *c))
    return b"".join(out)


def _region(fr, tile, cls, edge=0, mv=(0, 0)):
    m = M(*tile)
    return {"id": ids.region_id(fr, m), "frame": fr, "cls": cls, "edge_ppm": edge, "mv": list(mv),
            "spans": [["%04x" % m, "%04x" % m]]}


def make_vision():
    sp = {(1, M(*FROZEN_T)): (5, 0, (0, 0)), (1, M(*FLICK_T)): (4, 0, (0, 0)), (1, M(*CHG_T)): (2, 1, (0, 0)),
          (1, M(*MOVE_T)): (3, 1, (3, -1))}
    regions = [_region(1, FROZEN_T, "FROZEN"), _region(1, FLICK_T, "FLICKER"), _region(1, CHG_T, "CHANGED", 900000),
               _region(1, MOVE_T, "MOVED", 500000, (3, -1))]
    frames = [{"frame": i, "n": [3600, 0, 0, 0, 0, 0, 0]} for i in range(3)]
    frames[1]["n"] = [3596, 0, 1, 1, 1, 1, 0]
    ktrack = ids.track_id(1, M(*FLICK_T))
    gops = [{"g": 0, "f0": 0, "n_frames": 3, "cut": False, "params_sha256": PSHA,
             "tracks": [{"k": ktrack, "first": 1, "last": 1, "cls_mask": 16, "flags": ["FLICKER"], "period": 2,
                         "r_rep": regions[1]["id"]}],
             "cycles": [], "events": []},
            {"g": 1, "f0": 2, "n_frames": 1, "cut": True, "params_sha256": PSHA, "tracks": [], "cycles": [],
             "events": [["FLASH", ids.global_id(2)]]}]
    return cite.build_vision(W, H, _tiles(sp), frames, regions, gops, PSHA, API), regions, ktrack


def make_pack():
    return {"schema": "vqa.pack/v2", "grid": GRID, "params_sha256": PSHA, "api_version": API,
            "frame_map": [[0, 0], [1, 1], [2, 2]], "gops": [0, 1], "z1_sha256": "00" * 32, "cap_bytes": 8000}


def fail(check, pointers, reason, quote=None):
    r = {"check_id": check, "status": "FAIL", "pointers": pointers, "reason_class": reason}
    if quote is not None:
        r["digest_quote"] = quote
    return r


class CiteTests(unittest.TestCase):
    def setUp(self):
        self.v, self.regions, self.ktrack = make_vision()
        self.pack = make_pack()

    def run_v(self, *results, pack=None):
        return cite.verify_citations(self.v, {"results": list(results)}, pack or self.pack)

    def one(self, ptr, reason, quote=None):
        o = self.run_v(fail("c1", [ptr], reason, quote))
        return o, o["results"][0]["pointers"][0]["code"]

    def f(self, fr, t):
        return ids.field_id(fr, M(*t))

    # ---- positive
    def test_ok_each_reason(self):
        cases = [(self.f(1, FROZEN_T), "frozen"), (self.f(1, FLICK_T), "flicker"), (self.f(1, MOVE_T), "teleport"),
                 (self.f(1, CHG_T), "overlap"), (self.f(1, CHG_T), "missing"), (self.f(1, MOVE_T), "overlap"),
                 (self.regions[0]["id"], "frozen"), (self.ktrack, "flicker"), (ids.global_id(2), "black"),
                 (ids.gop_id(1), "black"), (self.f(0, (1, 1)), "static_content")]
        for ptr, reason in cases:
            o, code = self.one(ptr, reason)
            self.assertEqual((o["rc"], code), (0, "OK"), (ptr, reason, o))
            self.assertEqual(o["receipt"], "CV:PASS")

    def test_neighbour_tile_relevant(self):
        # Moore ring +-1: a tile next to the flagged FROZEN tile, claimed frozen via the global all-unchanged rule
        near = self.f(1, (FROZEN_T[0] + 1, FROZEN_T[1]))
        o, code = self.one(near, "frozen")
        self.assertEqual(code, "E_CITE_CLASS")  # unchanged tile is not frozen evidence even when relevant

    def test_global_frozen_all_unchanged(self):
        o, code = self.one(ids.global_id(0), "frozen")
        self.assertEqual((o["rc"], code), (0, "OK"))

    def test_quote_ok(self):
        p = self.f(1, MOVE_T)
        q = cite.render_quote(p, self.v, *ids.parse_any(p))
        self.assertEqual(q, p + " MOVED mv=3,-1")
        o, code = self.one(p, "teleport", q)
        self.assertEqual((o["rc"], code), (0, "OK"))

    def test_pass_and_cannot_see_untouched(self):
        o = self.run_v({"check_id": "p", "status": "PASS"}, {"check_id": "c", "status": "CANNOT_SEE"})
        self.assertEqual((o["rc"], o["counts"]), (0, {"accepted": 0, "rejected": 0}))

    # ---- adversarial: cites ids without looking
    def test_forged_pointers_and_quotes(self):
        good = self.f(1, FROZEN_T)
        forged = [
            ("f1:t0a4", "frozen", None, "E_CITE_SYNTAX"),  # 3 hex
            ("f01:t0000", "frozen", None, "E_CITE_SYNTAX"),  # padded frame
            ("f1:T0000", "frozen", None, "E_CITE_SYNTAX"),  # upper case
            ("r_000104_77abe901/" + good, "frozen", None, "E_CITE_SYNTAX"),  # judge must not emit run prefix
            ("f1:t" + "0" * 30, "frozen", None, "E_CITE_SYNTAX"),  # too long
            ("V-EVT-000019", "frozen", None, "E_CITE_SYNTAX"),  # wrong kind
            (self.f(1, (200, 200)), "frozen", None, "E_CITE_UNKNOWN"),  # tx outside grid
            (self.f(99, FROZEN_T), "frozen", None, "E_CITE_UNKNOWN"),  # frame absent
            ("r1:ffff", "frozen", None, "E_CITE_UNKNOWN"),  # region absent
            ("k1:ffff", "flicker", None, "E_CITE_UNKNOWN"),
            ("g9", "black", None, "E_CITE_UNKNOWN"),
            (good, "frozen", "f1:t0000 FROZEN", "E_CITE_QUOTE"),  # forged quote
            (good, "flicker", None, "E_CITE_CLASS"),  # real tile, wrong reason
            (self.f(1, (50, 40)), "frozen", None, "E_CITE_CLASS"),  # unchanged tile claimed frozen
            (ids.global_id(1), "black", None, "E_CITE_CLASS"),  # no cut at frame 1
            (self.f(1, FLICK_T), "drift", None, "E_CITE_CLASS"),
        ]
        for ptr, reason, quote, want in forged:
            o, code = self.one(ptr, reason, quote)
            self.assertEqual((o["rc"], code, o["results"][0]["status_out"]), (3, want, "CANNOT_SEE"), (ptr, o))
        self.assertEqual(o["counts"]["accepted"], 0)

    def test_window_rule(self):
        pk = copy.deepcopy(self.pack)
        pk["frame_map"] = [[0, 0]]
        pk["gops"] = []
        o = self.run_v(fail("c", [self.f(1, FROZEN_T)], "frozen"), pack=pk)
        self.assertEqual(o["results"][0]["pointers"][0]["code"], "E_CITE_WINDOW")
        pk["crop_frames"] = [1]  # crop frames extend the window
        self.assertEqual(self.run_v(fail("c", [self.f(1, FROZEN_T)], "frozen"), pack=pk)["rc"], 0)

    def test_window_kinds(self):
        pk = copy.deepcopy(self.pack)
        pk["frame_map"], pk["gops"] = [[0, 0]], ["g0"]  # wire form: string gop ids
        # g0 covers frames 0..2 (BATCH) but only frame 0 is SEEN: f/r pointers outside SEEN are not citable
        o = self.run_v(fail("c", [self.f(1, FROZEN_T)], "frozen"), pack=pk)
        self.assertEqual(o["results"][0]["pointers"][0]["code"], "E_CITE_WINDOW")
        # a region printed in V3 is citable although its frame is not SEEN
        rid = self.regions[0]["id"]
        pk["v3"] = {"g": [{"g": 0, "i": [["k1:0000", rid, "FROZEN", [0, 0, 1, 1], 1, [0, 0], 0, [], rid + " FROZEN"]]}]}
        self.assertEqual(self.run_v(fail("c", [rid], "frozen"), pack=pk)["rc"], 0)
        # a track overlapping BATCH is citable; a string gop id in pack is accepted
        self.assertEqual(self.run_v(fail("c", [self.ktrack], "flicker"), pack=pk)["rc"], 0)
        self.assertEqual(self.run_v(fail("c", ["g0"], "flicker"), pack=pk)["rc"], 0)
        self.assertEqual(self.run_v(fail("c", ["g1"], "flicker"), pack=pk)["results"][0]["pointers"][0]["code"],
                         "E_CITE_WINDOW")

    def test_static_content_vision_check(self):
        for cid in ("render.flicker", "ui.overlap", "player.teleport", "gui.scroll_stuck"):
            o = self.run_v(fail(cid, [self.f(1, FROZEN_T)], "static_content"))
            self.assertEqual((o["rc"], o["results"][0]["pointers"][0]["code"]), (3, "E_CITE_CLASS"), cid)
            self.assertEqual(o["results"][0]["status_out"], "CANNOT_SEE")
        self.assertEqual(self.run_v(fail("ui.text", [self.f(1, FROZEN_T)], "static_content"))["rc"], 0)

    def test_irrelevant_far_pointer(self):
        # FROZEN evidence on a real FROZEN tile exists, but a claimed global-frame pointer with nothing flagged nearby:
        # drift claim on a real DRIFT-free scene via track pointer is class-rejected; relevance: tile cls ok, far region
        v, regions, _ = make_vision()
        far = (60, 40)
        sp = {(1, M(*far)): (5, 0, (0, 0))}  # FROZEN tile with no flagged region anywhere near
        v["tiles"] = _tiles(sp)
        o = cite.verify_citations(v, {"results": [fail("c", [self.f(1, far)], "frozen")]}, self.pack)
        # FROZEN region at FROZEN_T exists in window but is far from `far` tile
        self.assertEqual(o["results"][0]["pointers"][0]["code"], "E_CITE_IRRELEVANT")
        self.assertEqual(o["receipt"], "CV:FAIL:RE,NO")

    def test_no_pointers_and_overflow(self):
        o = self.run_v(fail("c", [], "frozen"))
        self.assertEqual((o["rc"], o["receipt"]), (3, "CV:FAIL:NO"))
        ptrs = [self.f(1, FROZEN_T)] * 9
        o = self.run_v(fail("c", ptrs, "frozen"))
        self.assertEqual(o["rc"], 0)  # first 8 accepted
        self.assertEqual(o["results"][0]["pointers"][8]["code"], "E_CITE_SYNTAX")

    def test_mixed_valid_and_forged_never_rejects_valid(self):
        o = self.run_v(fail("a", [self.f(1, FROZEN_T)], "frozen"), fail("b", ["f1:t0a4"], "frozen"))
        self.assertEqual((o["rc"], o["counts"]), (3, {"accepted": 1, "rejected": 1}))
        self.assertEqual(o["results"][0]["status_out"], "FAIL")
        self.assertEqual(o["results"][1]["status_out"], "CANNOT_SEE")

    def test_bad_reason_class_fails_closed(self):
        o = self.run_v({"check_id": "c", "status": "FAIL", "pointers": [self.f(1, FROZEN_T)]})
        self.assertEqual(o["rc"], 3)

    # ---- stale / usage
    def test_stale(self):
        for key, val in (("params_sha256", "cd" * 32), ("grid", "grid:1920x1080:s32"), ("api_version", "9.9.9")):
            pk = dict(self.pack, **{key: val})
            o = self.run_v(fail("c", [self.f(1, FROZEN_T)], "frozen"), pack=pk)
            self.assertEqual((o["rc"], o["code"]), (4, "E_STALE"), key)

    def test_usage(self):
        self.assertEqual(cite.verify_citations(self.v, {"nope": 1}, self.pack)["rc"], 2)
        pk = dict(self.pack)
        del pk["grid"]
        self.assertEqual(self.run_v(fail("c", [], "frozen"), pack=pk)["rc"], 2)

    def test_deterministic_and_canonical(self):
        r = [fail("c", [self.f(1, FROZEN_T), "zz"], "frozen")]
        a, b = self.run_v(*r), self.run_v(*r)
        self.assertEqual(canon.dumps(a), canon.dumps(b))
        self.assertIsNone(params.PARAMS.get("NOT_A_ROW"))

    # ---- files
    def test_verify_dir_roundtrip(self):
        s, gw, gh = layout.grid_params(W, H)
        hdr = layout.pack_tile_header(s, W, H, gw, gh, gw * gh, 3, 0, PSHA, API)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "tiles.bin").write_bytes(hdr + self.v["tiles"])
            (d / "frames.jsonl").write_text("".join(canon.dumps({"frame": i, "n": self.v["frames"][i]["n"]})
                                                    for i in range(3)))
            (d / "regions.jsonl").write_text("".join(canon.dumps(r) for r in self.regions))
            (d / "gops.jsonl").write_text("".join(canon.dumps(g) for g in self.v["gops"]))
            jo = {"results": [fail("c", [self.f(1, FROZEN_T)], "frozen")]}
            (d / "pack.json").write_text(canon.dumps(self.pack))
            (d / "jo.json").write_text(canon.dumps(jo))
            o = cite.verify_dir(d, d / "pack.json", d / "jo.json")
            self.assertEqual(o["rc"], 0, o)
            self.assertEqual(cite.verify_dir(d / "missing", self.pack, jo)["rc"], 2)


if __name__ == "__main__":
    unittest.main()
