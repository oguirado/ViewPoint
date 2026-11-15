"""W5 S3: router ladder, abstention, caps, gates and CLI on a synthetic store (SPEC_VMEM §8-§10)."""
import io
import json
import os
import random
import struct
import tempfile
import unittest
from contextlib import redirect_stdout

from visioncore import canon, layout
from visioncore.errors import VqaError
from vmem import cli, gates, router
from vmem.blobs import blob_path
from vmem.store import Store

GRID = "grid:1280x720:s16"
RUN1, RUN3 = "r_000001_aaaaaaaa", "r_000003_bbbbbbbb"
TK_A = "T:demo:hud:1280x720:ui:0a40-0a7f"
TK_B = "T:demo:hud:1280x720:ui:0b00-0b3f"
SHA = "0" * 64


def tile(xh, sig=0, y=100, sq=50):
    return struct.pack(layout.TILE_FMT, 0, 0, 0, y, 0, 0, sq, 0, 0, 0, 0, 0, sig, xh)


def scope(b):
    return {"app": "demo", "scene": "hud", "res": "1280x720", "grid": GRID, "build_seq": b}


def unit(n, kind, tk, b, summary, pin=None, layer="BG", sig=None, look=None, run=RUN1, **kw):
    pre = {"baseline": "BAS", "defect": "DEF", "verdict": "VER"}[kind]
    u = {"schema": "vmem.unit/v1", "id": "V-%s-%06d" % (pre, n), "key": "vision.demo.hud.%s.%s" % (tk[-4:], kind),
         "kind": kind, "layer": layer, "pin": pin, "status": "active", "scope": scope(b), "tk": tk,
         "subject": "ammo_label" if tk == TK_A else "health_bar", "predicate": "baseline", "summary": summary,
         "receipt_sha256": SHA, "class": "CHANGED", "born_gen": 1, "last_gen": 1, "value": "", "value_num": 0,
         "value_den": 1, "look": None, "links": [], "verdict": None, "check_id": None,
         "anchors": [{"run_id": run, "cite": run + "/f3:t0a40", "frame": 3, "render_index": None,
                      "bbox_px": [0, 0, 16, 16], "mv_px": [0, 0], "xh64": "0" * 16,
                      "sig64": sig or ("%016x" % n), "crop": None, "desc_blob": None}]}
    if look:
        u["look"] = look
    u.update(kw)
    return u


def look(root_blobs, tiles, ah):
    return {"grid": GRID, "n": len(tiles) // layout.TILE_SIZE, "ah64": ah, "y_q4_mean": 100, "desc_blob": root_blobs[tiles]}


class Syn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "store")
        self.addCleanup(self.tmp.cleanup)
        # three appearances of TK_A: v1 and v3 differ in every tile; noisy variant of v1 is for look_eq tests
        self.t1 = b"".join(tile(100 + i, sig=0xF0F0 + i) for i in range(4))
        self.t3 = b"".join(tile(900 + i, sig=0x0F0F << 20) for i in range(4))
        self.t1n = b"".join(tile(500 + i, sig=0xF0F0 + i, y=104, sq=52) for i in range(4))
        blobs = [(self.t1, "bin"), (self.t3, "bin"), (self.t1n, "bin")]
        shas = {k: canon.sha256_bytes(k) for k in (self.t1, self.t3, self.t1n)}
        self.shas = shas
        self.l1 = look(shas, self.t1, "1" * 16)
        self.l3 = look(shas, self.t3, "3" * 16)
        self.l1n = look(shas, self.t1n, "2" * 16)
        self.sigd = 0xDEADBEEF12345678
        units = [
            unit(1, "baseline", TK_A, 1, "ammo counter label baseline ok", "GOLDEN", "FG", look=self.l1),
            unit(2, "baseline", TK_A, 3, "ammo counter label moved right", "GOLDEN", "FG", look=self.l3, run=RUN3),
            unit(3, "baseline", TK_B, 1, "health bar fill baseline", "GOLDEN", "FG", look=self.l1),
            unit(4, "defect", TK_B, 2, "health bar flicker defect", "FAIL", "FG", sig="%016x" % self.sigd,
                 verdict="FAIL", predicate="flicker"),
            unit(5, "verdict", TK_B, 2, "health bar verdict fail flicker", "FAIL", "FG", sig="%016x" % (self.sigd ^ 0b101),
                 verdict="FAIL"),
        ]
        self.units = units
        files = {"units/active.jsonl": "".join(canon.dumps(u) for u in units).encode(),
                 "VISUAL_DIGEST.md": b"# digest\nscope demo/hud builds 1-3\n",
                 "catalogue/foreground.tsv": ("id\tkey\n" + "".join("%s\tk\n" % u["id"] for u in units)).encode()}
        files.update(router.build_tk_index(units))
        files.update(router.build_sig_index(units))
        st = Store(self.root)
        with st.lock():
            st.commit(files, blobs, extra={"fg_blob_bytes": 10}, strict=False)
        self.st = st

    def commit(self, files, **kw):
        with self.st.lock():
            return self.st.commit(files, strict=False, **kw)

    def cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv) + (["--store", self.root] if "--store" not in argv else []))
        out = buf.getvalue()
        self.assertEqual(out.count("\n"), 1)
        o = json.loads(out)
        self.assertEqual(o["rc"], rc)
        self.assertNotEqual(rc, 1)
        return rc, o


class TestRoute(Syn):
    def test_positive_answer(self):
        r = router.route(self.root, q="ammo label")
        self.assertIn(r["r"][0][0], ("V-BAS-000001", "V-BAS-000002"))
        self.assertEqual(r["r"][0][2], TK_A)
        self.assertFalse(r["abstain"]["flag"])
        self.assertLessEqual(len(canon.dumps(r)), 2000 + 16)
        # total order: deterministic repeat, ties by id asc
        self.assertEqual(r, router.route(self.root, q="ammo label"))

    def test_scores_integer_and_ranked(self):
        r = router.route(self.root, q="flicker")
        self.assertTrue(all(isinstance(x[1], int) and x[1] > 0 for x in r["r"]))
        self.assertEqual([x[1] for x in r["r"]], sorted((x[1] for x in r["r"]), reverse=True))

    def test_abstain_scope(self):
        with self.assertRaises(VqaError) as c:
            router.route(self.root, q="ammo", scope="other/scene")
        self.assertEqual((c.exception.code, c.exception.rc, c.exception.detail["abstain"]), ("NOT_IN_VISUAL_MEMORY", 3, "scope_unknown"))

    def test_abstain_lexical_unrecorded(self):
        with self.assertRaises(VqaError) as c:
            router.route(self.root, q="zeppelin hangar")
        self.assertEqual(c.exception.detail["abstain"], "no_lexical_hit")

    def test_abstain_build_and_tk(self):
        with self.assertRaises(VqaError) as c:
            router.route(self.root, q="ammo", build=99)
        self.assertEqual(c.exception.detail["abstain"], "build_out_of_range")
        with self.assertRaises(VqaError) as c:
            router.route(self.root, q="ammo", tk="T:demo:hud:1280x720:ui:0000-0001")
        self.assertEqual(c.exception.detail["abstain"], "tk_unknown")

    def test_first_match_wins(self):
        with self.assertRaises(VqaError) as c:  # scope (a) beats build (c) and lexical (f)
            router.route(self.root, q="zeppelin", scope="x/y", build=99)
        self.assertEqual(c.exception.detail["letter"], "a")

    def test_grid_mismatch_rc4(self):
        with self.assertRaises(VqaError) as c:
            router.route(self.root, q="ammo", grid="grid:1920x1080:s32")
        self.assertEqual((c.exception.code, c.exception.rc), ("E_STALE", 4))

    def test_usage(self):
        with self.assertRaises(VqaError) as c:
            router.route(self.root)
        self.assertEqual(c.exception.rc, 2)
        with self.assertRaises(VqaError):
            router.route(self.root, q="a", k=6)

    def test_byte_cap_overflow_and_rc6(self):
        big = [dict(u, summary="flicker " + "x" * 700, id="V-DEF-%06d" % (50 + i)) for i, u in enumerate(self.units[:4])]
        files = {"units/active.jsonl": "".join(canon.dumps(u) for u in big).encode()}
        self.commit(files)
        r = router.route(self.root, q="flicker", k=5)
        self.assertGreater(r["overflow"], 0)
        self.assertLessEqual(len(canon.dumps(r)), 2000 + 16)
        huge = [dict(self.units[0], summary="flicker " + "y" * 2100)]
        self.commit({"units/active.jsonl": canon.dumps(huge[0]).encode()})
        with self.assertRaises(VqaError) as c:
            router.route(self.root, q="flicker", k=1)
        self.assertEqual((c.exception.code, c.exception.rc), ("E_BUDGET", 6))


class TestSig(Syn):
    def test_hit_and_miss(self):
        r = router.route(self.root, sig=self.sigd)
        self.assertEqual(r["r"][0][0], "V-DEF-000004")
        with self.assertRaises(VqaError) as c:
            router.route(self.root, sig=self.sigd ^ 0xFFFF)
        self.assertEqual(c.exception.detail["abstain"], "sig_no_hit")

    def test_d3_found_d4_needs_scan(self):
        q3 = self.sigd ^ 0b10101
        self.assertIn("V-DEF-000004", [x[0] for x in router.route(self.root, sig=q3)["r"]])
        q4 = self.sigd ^ 0b1000_0100_0010_0001  # 4 bits, one per nibble in one band
        with self.assertRaises(VqaError):
            router.route(self.root, sig=q4)
        self.assertEqual(router.route(self.root, sig=q4, scan=True)["r"][0][0], "V-DEF-000004")

    def test_band_exact_vs_linear(self):
        rnd = random.Random(7)
        keys = [rnd.getrandbits(64) for _ in range(600)]
        units = [unit(i + 1, "defect", TK_B, 1, "k", "FAIL", "FG", sig="%016x" % k) for i, k in enumerate(keys)]
        self.commit(dict([("units/active.jsonl", "".join(canon.dumps(u) for u in units).encode())] +
                         list(router.build_sig_index(units).items())))
        v = router.View(self.root)
        for _ in range(40):
            q = rnd.choice(keys)
            for d in range(4):
                bits = rnd.sample(range(64), d)
                for b in bits:
                    q ^= 1 << b
                want = sorted((bin(q ^ k).count("1"), "V-DEF-%06d" % (i + 1)) for i, k in enumerate(keys)
                              if bin(q ^ k).count("1") <= 3)
                self.assertEqual(router.sig_lookup(v, q), want)

    def test_seen_defect(self):
        r = router.seen_defect(self.root, self.sigd ^ 1)
        self.assertEqual([h["unit_id"] for h in r["hits"]][0], "V-DEF-000004")
        self.assertEqual(r["hits"][0]["dist"], 1)
        with self.assertRaises(VqaError) as c:
            router.seen_defect(self.root, self.sigd ^ 0xFFFFFFFF)
        self.assertEqual(c.exception.rc, 3)


class TestRegion(Syn):
    def test_last_good(self):
        r = router.last_good(self.root, tk=TK_A)
        self.assertEqual((r["unit_id"], r["build_seq"]), ("V-BAS-000002", 3))
        self.assertEqual(router.last_good(self.root, subject="health_bar")["unit_id"], "V-BAS-000003")
        with self.assertRaises(VqaError) as c:
            router.last_good(self.root, tk="T:demo:hud:1280x720:ui:ffff-ffff")
        self.assertEqual(c.exception.rc, 3)
        with self.assertRaises(VqaError):
            router.last_good(self.root)

    def test_diff_region_first_change(self):
        r = router.diff_region(self.root, TK_A)
        self.assertEqual(r["first_change"]["build_seq"], 3)
        self.assertEqual(r["first_change"]["look_eq"], "DIFFERENT")
        self.assertEqual(r["first_change"]["ah64_old"], "1" * 16)
        self.assertIsNone(router.diff_region(self.root, TK_A, from_build=3)["first_change"])
        self.assertIsNone(router.diff_region(self.root, TK_A, from_build=1, to_build=2)["first_change"])
        self.assertIsNone(router.diff_region(self.root, TK_B)["first_change"])

    def test_diff_region_ingest_order(self):
        us = [dict(u, scope=dict(u["scope"], build_seq=None, build_src="ingest_order")) for u in self.units[:2]]
        self.commit({"units/active.jsonl": "".join(canon.dumps(u) for u in us).encode()})
        r = router.diff_region(self.root, TK_A)
        self.assertIn("first_run", r)
        self.assertNotIn("first_change", r)

    def test_look_eq(self):
        s = self.shas
        a = dict(self.l1)
        self.assertEqual(router.look_eq(self.root, a, dict(a)), "SAME_EXACT")
        self.assertEqual(router.look_eq(self.root, a, self.l1n), "SAME_NOISY")
        self.assertEqual(router.look_eq(self.root, a, self.l3), "DIFFERENT")
        with self.assertRaises(VqaError) as c:
            router.look_eq(self.root, a, dict(self.l3, grid="grid:1920x1080:s32"))
        self.assertEqual(c.exception.rc, 4)
        # noise above T0 is a real difference
        self.assertEqual(router.look_eq(self.root, a, self.l1n, t0_q4=1), "DIFFERENT")


class TestAnswer(Syn):
    def test_check_answer(self):
        u = self.units[0]
        good = {"citations": [{"unit_id": u["id"], "cite": u["anchors"][0]["cite"], "quote": "ammo counter label"}]}
        self.assertEqual(router.check_answer(self.root, good)["checked"], 1)
        bad = {"citations": [{"unit_id": u["id"], "cite": RUN3 + "/f9:t0000", "quote": "ammo counter label"}]}
        with self.assertRaises(VqaError) as c:
            router.check_answer(self.root, bad)
        self.assertEqual((c.exception.code, c.exception.rc), ("E_CITE", 3))
        short = {"citations": [dict(good["citations"][0], quote="ammo counte")]}  # 11 chars
        with self.assertRaises(VqaError) as c:
            router.check_answer(self.root, short)
        self.assertEqual(c.exception.detail["abstain"], "check_failed")
        forged = {"citations": [dict(good["citations"][0], quote="never recorded text")]}
        with self.assertRaises(VqaError):
            router.check_answer(self.root, forged)
        with self.assertRaises(VqaError):
            router.check_answer(self.root, {"citations": [dict(good["citations"][0], unit_id="V-BAS-009999")]})

    def test_answer_roundtrips_check(self):
        a = router.answer(self.root, q="ammo label")
        self.assertEqual((a["schema"], a["status"], a["rc"]), ("vmem.answer/v1", "answered", 0))
        self.assertLessEqual(a["budget_tok"], router.ANSWER_SOFT_TOK)
        router.check_answer(self.root, a)

    def test_token_caps(self):
        a = router.answer(self.root, q="ammo label", max_tok=700)
        self.assertTrue(a["budget_tok"] <= 700 or len(a["levels"]) == 2)
        self.assertEqual(a["levels"][:2], ["V0", "V1"])
        old = router.ANSWER_HARD_TOK
        router.ANSWER_HARD_TOK = 10
        try:
            with self.assertRaises(VqaError) as c:
                router.answer(self.root, q="ammo label")
            self.assertEqual((c.exception.code, c.exception.rc), ("E_BUDGET", 6))
        finally:
            router.ANSWER_HARD_TOK = old

    def test_answer_abstains(self):
        with self.assertRaises(VqaError) as c:
            router.answer(self.root, q="zeppelin")
        self.assertEqual(c.exception.rc, 3)

    def test_stale_when_current_moves(self):
        v = router.View(self.root)
        self.commit({"units/active.jsonl": b""})
        with self.assertRaises(VqaError) as c:
            v.finish()
        self.assertEqual((c.exception.code, c.exception.rc), ("E_STALE", 4))


class TestGates(Syn):
    def test_clean_passes(self):
        r = gates.verify(self.root)
        self.assertEqual(set(r["gates"].values()), {0})
        self.assertEqual(r["fg_count"], 5)
        self.assertEqual(gates.verify(self.root)["fg_ids_sha256"], r["fg_ids_sha256"])
        self.assertEqual(r["absent"], ["g_determinism", "g_cache_stable"])

    def _mutate(self, fn, gate):
        us = [dict(u) for u in self.units]
        files = {"units/active.jsonl": "".join(canon.dumps(u) for u in fn(us)).encode()}
        files.update(router.build_tk_index(us))
        self.commit(files)
        self.assertEqual(gates.verify(self.root)["gates"][gate], 5, gate)

    def test_layer_pin_mutant(self):
        def m(us):
            us[0]["pin"] = None
            return us
        self._mutate(m, "g_layer_pin")

    def test_fg_monotone_mutant(self):
        self._mutate(lambda us: us[1:], "g_fg_monotone")

    def test_bad_id_and_cite_mutant(self):
        def m(us):
            us[0]["tk"] = "T:bad"
            return us
        self._mutate(m, "g_ids")

        def m2(us):
            us[1]["anchors"] = [dict(us[1]["anchors"][0], cite="r_000009_cccccccc/f1:t0000")]
            return us
        self._mutate(m2, "g_cite")

    def test_grid_mutant(self):
        def m(us):
            us[0]["look"] = dict(us[0]["look"], grid="grid:1920x1080:s32")
            return us
        self._mutate(m, "g_grid")

    def test_caps_mutant(self):
        def m(us):
            us[0]["summary"] = "z" * 1100
            return us
        self._mutate(m, "g_caps")

    def test_float_rc5(self):
        self.commit({"units/active.jsonl": b'{"id":"V-BAS-000001","x":1.5}\n'})
        with self.assertRaises(VqaError) as c:
            gates.verify(self.root)
        self.assertEqual((c.exception.code, c.exception.rc, c.exception.detail["reason"]), ("E_GATE", 5, "g_nofloat"))

    def test_catalogue_mutant(self):
        self.commit({"units/active.jsonl": "".join(canon.dumps(u) for u in self.units).encode(),
                     "catalogue/foreground.tsv": b"id\tkey\nV-BAS-000001\tk\n"})
        self.assertEqual(gates.verify(self.root)["gates"]["g_catalogue"], 5)

    def test_blob_corruption(self):
        p = blob_path(self.root, self.shas[self.t1], "bin")
        os.chmod(p, 0o644)
        with open(p, "wb") as f:
            f.write(b"corrupt")
        self.assertEqual(gates.verify(self.root)["gates"]["g_blob_integrity"], 5)


class TestCli(Syn):
    def test_verbs_rc(self):
        rc, o = self.cli("route", "--q", "ammo label")
        self.assertEqual((rc, o["code"]), (0, "OK"))
        self.assertEqual(o["detail"]["r"][0][2], TK_A)
        rc, o = self.cli("route", "--q", "zeppelin")
        self.assertEqual((rc, o["code"]), (3, "NOT_IN_VISUAL_MEMORY"))
        rc, o = self.cli("route", "--q", "ammo", "--grid", "grid:1920x1080:s32")
        self.assertEqual((rc, o["code"]), (4, "E_STALE"))
        rc, o = self.cli("get", "V-BAS-000001")
        self.assertEqual((rc, o["detail"]["id"]), (0, "V-BAS-000001"))
        rc, o = self.cli("get", "V-BAS-000999")
        self.assertEqual(rc, 3)
        rc, o = self.cli("last-good", "--tk", TK_A)
        self.assertEqual(rc, 0)
        rc, o = self.cli("diff-region", "--tk", TK_A)
        self.assertEqual(o["detail"]["first_change"]["build_seq"], 3)
        rc, o = self.cli("seen-defect", "--sig", "%016x" % self.sigd)
        self.assertEqual(rc, 0)
        rc, o = self.cli("evidence", "V-DEF-000004")
        self.assertEqual(rc, 0)
        rc, o = self.cli("verify")
        self.assertEqual((rc, o["detail"]["fg_count"]), (0, 5))

    def test_usage_errors(self):
        self.assertEqual(self.cli("route")[0], 2)
        self.assertEqual(self.cli("bogusverb")[0], 2)
        self.assertEqual(self.cli("route", "--sig", "xyz")[0], 2)
        self.assertEqual(self.cli("check-answer")[0], 2)
        rc, o = self.cli("ingest")
        self.assertEqual((rc, o["detail"]["reason"]), (2, "missing_arg"))

    def test_budget_and_gate_rc(self):
        self.commit({"units/active.jsonl": canon.dumps(dict(self.units[0], summary="flicker " + "y" * 2100)).encode()})
        self.assertEqual(self.cli("route", "--q", "flicker", "--k", "1")[0], 6)
        self.assertEqual(self.cli("verify")[0], 5)

    def test_check_answer_cli_and_rollback(self):
        a = router.answer(self.root, q="ammo label")
        p = os.path.join(self.tmp.name, "ans.json")
        with open(p, "w") as f:
            f.write(canon.dumps(a))
        self.assertEqual(self.cli("check-answer", "--answer", p)[0], 0)
        a["citations"][0]["quote"] = "a made up sentence"
        with open(p, "w") as f:
            f.write(canon.dumps(a))
        self.assertEqual(self.cli("check-answer", "--answer", p)[0], 3)
        self.commit({"units/active.jsonl": b""})
        rc, o = self.cli("rollback", "--to", "1")
        self.assertEqual((rc, o["detail"]["gen"]), (0, 1))
        self.assertEqual(self.cli("rollback", "--to", "9")[0], 3)

    def test_store_untouched_by_reads(self):
        before = sorted((dp, tuple(sorted(fn))) for dp, _d, fn in os.walk(self.root))
        self.cli("route", "--q", "ammo")
        self.cli("verify")
        self.assertEqual(before, sorted((dp, tuple(sorted(fn))) for dp, _d, fn in os.walk(self.root)))

    def test_no_float_in_vmem_router_sources(self):
        import ast
        for f in ("router", "gates", "cli"):
            with open(os.path.join(os.path.dirname(router.__file__), f + ".py")) as fh:
                src = fh.read()
            for n in ast.walk(ast.parse(src)):
                self.assertFalse(isinstance(n, ast.Constant) and isinstance(n.value, float), f)
                self.assertFalse(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div), f)


if __name__ == "__main__":
    unittest.main()
