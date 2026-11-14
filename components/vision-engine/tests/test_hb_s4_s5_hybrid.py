"""HBM1-B lane M3: HB-VC-s4-regions and HB-VC-s5-gop in the hb-hybrid/v1 profile (HB_DECISIONS 2.1-2.3, G-FLOOR).

Per book: (a) anchors, GOLD blocks and machine blocks; (b) G-FLOOR; (c) lockstep admission accepts the profiled book and rejects it
without symbols.jsonl; (d) the generated companions rebuild byte-identically; plus book-vs-code checks (constants, offsets, exit codes,
narrative trace, documented limits). The live COMPANION_FORMAT.json profile entries are applied by lane E1, not tested here."""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "hyperbooks", "_tools"))
sys.path.insert(0, os.path.join(ROOT, "hbtools_v2"))
sys.dont_write_bytecode = True
import admission  # noqa: E402

HB = os.path.join(ROOT, "hyperbooks")
PM_BLOCK = re.compile(r"<!-- PM:BEGIN (\S+) -->\n```json\n(.*?)\n```\n<!-- PM:END \1 -->", re.S)
HB_BLOCK = re.compile(r"<!-- HB:BEGIN (hb\.[a-z_]+\.v\d+) -->\n```json\n(.*?)\n```\n<!-- HB:END -->", re.S)
SECTION_PM = {0: ["pm.node.v2", "pm.reading.v2"], 1: ["pm.claims.v2"], 2: ["pm.objects.v2", "pm.quantities.v2"], 3: ["pm.roadmap.v2"],
              4: ["pm.triptych.v2"], 5: ["pm.domains.v2"], 6: ["pm.limits.v2", "pm.search.v1"], 9: ["pm.edges.v2"]}
P3_FIELDS = ("INPUTS", "PRECONDITIONS", "OUTPUTS", "TIME", "SPACE", "ARITHMETIC", "FAILURE")

CFG = {
    "HB-VC-s4-regions": {
        "bid": "HB-VCS4-001", "module": "visioncore/regions.py", "src": "visioncore/regions.py,tests/test_regions.py", "sub": "hb-s5.regions",
        "p3": ("regions_of", "adjacency", "match_regions", "region_tiles", "mv_in_moore"),
        "inv": ("INV-S4-ROOT-MIN", "INV-S4-GROUP-ISOLATION", "INV-S4-SPANS-EXACT", "INV-S4-MATCH-BIJECTION", "INV-S4-SHIFT-FLOOR", "INV-S4-RANGE-CHECK"),
        "au": ("AU-S4-FIRST-SEEN", "AU-S4-ADJ-IN-PACK", "AU-S4-MAXIMUM-MATCHING", "AU-S4-TRUNC-SHIFT"),
        "needles": ("maximum-cardinality", "profile", "pack or vmem", "native"),
        "receipts": ("R-s4-regions-unittest",), "gen_sym": "`visioncore/regions.py::match_regions`",
        "forbidden": ("ADV_RT1", "PLAN_mathcodec", "EX-MA02", "EX-MC01", "EX-OC03", "EX-OC05", "EX-PM-213-1", "EX-PM-1132-1"),
    },
    "HB-VC-s5-gop": {
        "bid": "HB-VCS5-001", "module": "visioncore/gop.py", "src": "visioncore/gop.py,tests/test_gop.py", "sub": "hb-s5.gop",
        "p3": ("rle_bound", "rle_encode", "rle_decode", "dirty_mask", "dirty_ppm_of", "cut_flags", "plan_gops", "zlib_bound", "max_gop_bytes", "parse_header",
               "split_blobs", "encode_run", "decode_gop", "build_tracks", "find_cycles", "gop_record", "write_gops"),
        "inv": ("INV-S5-RLE-BOUND", "INV-S5-ROUNDTRIP", "INV-S5-INTEGRITY", "INV-S5-IDENTITY-SIZE", "INV-S5-CUT-PLAN", "INV-S5-CYCLE-TRUE-HASH"),
        "au": ("AU-S5-REMATCH", "AU-S5-ZLIB-IDENTITY", "AU-S5-UNCHECKED-SLICE", "AU-S5-BOUND-REUSE"),
        "needles": ("as met", "valueerror", "pair atomicity", "native"),
        "receipts": ("R-s5-gop-unittest", "R-s5-gop-rle-bound"), "gen_sym": "`visioncore/gop.py::decode_gop`",
        "forbidden": ("ADV_RT1", "PLAN_mathcodec", "EX-MA02", "EX-MC01", "EX-OC03", "EX-OC05", "PM-213.1", "rle_encode_u16", "VGOP\\x01"),
    },
}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Book:
    def __init__(self, name):
        self.name, self.cfg = name, CFG[name]
        self.dir = os.path.join(HB, name)
        self.text = read(os.path.join(self.dir, "book.md"))

    def sections(self):
        marks = [(int(m.group(1)), m.start()) for m in re.finditer(r'^## <a id="hb-s(\d+)"></a>', self.text, re.M)]
        return {n: self.text[st:marks[i + 1][1] if i + 1 < len(marks) else len(self.text)] for i, (n, st) in enumerate(marks)}

    def hb_blocks(self, schema):
        return [json.loads(m.group(2)) for m in HB_BLOCK.finditer(self.text) if m.group(1) == schema]

    def pm_block(self, name):
        return next(json.loads(m.group(2)) for m in PM_BLOCK.finditer(self.text) if m.group(1) == name)

    def prose(self):
        return re.sub(r"```.*?```", " ", self.text, flags=re.S)


class HybridChecks:
    """Mixin: one subclass per book sets BOOK."""
    BOOK = None

    @classmethod
    def setUpClass(cls):
        cls.b = Book(cls.BOOK)

    # ---- (a) anchors, blocks
    def test_anchors_hb_s0_to_s10_present_in_order(self):
        pos = []
        for n in range(11):
            m = re.search(r'<a id="hb-s%d"></a><a id="pm-s%d"></a>' % (n, n), self.b.text)
            self.assertIsNotNone(m, "hb-s%d anchor missing" % n)
            pos.append(m.start())
        self.assertEqual(pos, sorted(pos))
        self.assertEqual(len(self.b.sections()), 11)

    def test_gold_blocks_in_their_sections(self):
        secs = self.b.sections()
        for n, names in SECTION_PM.items():
            got = [m.group(1) for m in PM_BLOCK.finditer(secs[n])]
            for name in names:
                self.assertIn(name, got, "hb-s%d lacks %s" % (n, name))
        self.assertEqual([m.group(1) for m in PM_BLOCK.finditer(secs[0])], SECTION_PM[0])

    def test_every_pm_block_parses_with_its_schema(self):
        begins = re.findall(r"<!-- PM:BEGIN (\S+) -->", self.b.text)
        self.assertEqual(len(begins), 11)
        parsed = {m.group(1): json.loads(m.group(2)) for m in PM_BLOCK.finditer(self.b.text)}
        self.assertEqual(sorted(begins), sorted(parsed))
        for name, o in parsed.items():
            self.assertEqual(o["schema"], name)
            if "pm_id" in o:
                self.assertEqual(o["pm_id"], self.b.cfg["bid"])

    def test_machine_blocks_parse_and_match_schemas(self):
        import schemalib
        sdir = os.path.join(ROOT, "standards", "schemas")
        n = 0
        for m in HB_BLOCK.finditer(self.b.text):
            o = json.loads(m.group(2))
            self.assertEqual(schemalib.validate(o, m.group(1), schema_dir=sdir), [], m.group(1))
            n += 1
        self.assertGreaterEqual(n, 12)

    def test_generated_sections_only_in_fences(self):
        secs = self.b.sections()
        for n in (8, 9, 10):
            self.assertIn("<!-- HB:GEN BEGIN hb-s%d -->" % n, secs[n])
            self.assertIn("<!-- HB:GEN END hb-s%d -->" % n, secs[n])
        self.assertIn(self.b.cfg["gen_sym"], secs[8])

    def test_mathlinks_only_sealed_or_ausente(self):
        rows = [json.loads(ln) for ln in read(os.path.join(self.b.dir, "mathlinks.jsonl")).splitlines() if ln.strip()]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn(r["status"], ("SEALED", "AUSENTE"))
        self.assertEqual([x for x in admission.check_mathlinks(HB, ROOT) if x.startswith(self.BOOK)], [])
        self.assertTrue(any(r["status"] == "SEALED" for r in rows))

    # ---- (b) G-FLOOR
    def test_seven_fields_per_public_symbol(self):
        s4 = self.b.sections()[4]
        mod = self.b.cfg["module"]
        for sym in self.b.cfg["p3"]:
            m = re.search(r"# PM-P3-HEADER: %s::%s\n((?:# [A-Z]+: .*\n)+)" % (re.escape(mod), re.escape(sym)), s4)
            self.assertIsNotNone(m, sym)
            keys = re.findall(r"^# ([A-Z]+):", m.group(1), re.M)
            self.assertEqual(tuple(keys), P3_FIELDS, sym)
            for ln in m.group(1).splitlines():
                self.assertGreater(len(ln.split(":", 1)[1].strip()), 0, "%s empty field" % sym)

    def test_every_anchored_symbol_has_a_header(self):
        """The code's @anchor: functions are all covered by a PM-P3-HEADER."""
        code = read(os.path.join(ROOT, self.b.cfg["module"]))
        anchored = re.findall(r"def (\w+)\(.*#\s*@anchor:", code)
        self.assertTrue(anchored)
        for sym in anchored:
            self.assertIn(sym, self.b.cfg["p3"])

    def test_hb_s5_inv_and_au(self):
        blocks = [x for x in self.b.hb_blocks("hb.inv.v2") if x["module"] == self.b.cfg["module"]]
        self.assertEqual(len(blocks), 1)
        blk = blocks[0]
        self.assertTrue(2 <= len(blk["inv"]) <= 6 and 1 <= len(blk["au"]) <= 4)
        self.assertEqual(tuple(i["id"] for i in blk["inv"]), self.b.cfg["inv"])
        self.assertEqual(tuple(i["id"] for i in blk["au"]), self.b.cfg["au"])
        for it in blk["inv"] + blk["au"]:
            self.assertLessEqual(len(it["text"].encode("utf-8")), 600, it["id"])
        self.assertIn('<a id="%s"></a>' % self.b.cfg["sub"], self.b.sections()[5])

    def test_triptych_panel1_matches_inv_block(self):
        tri = self.b.pm_block("pm.triptych.v2")
        self.assertEqual(tuple(i["id"] for i in tri["panel1"]["invariants"]), self.b.cfg["inv"])
        self.assertEqual(tuple(i["id"] for i in tri["panel2"]["anti_uses"]), self.b.cfg["au"])
        self.assertEqual(sorted(tri["panel3"]["coverage_map"]), sorted(self.b.cfg["inv"]))

    def test_search_block_nonempty(self):
        s = self.b.pm_block("pm.search.v1")
        for k in ("questions_answered", "engineering_tasks", "synonyms", "exclusions", "keywords", "retrieval_triggers"):
            self.assertTrue(s[k], k)

    def test_blocked_use_content(self):
        lim = self.b.pm_block("pm.limits.v2")
        self.assertTrue(all(e["blocked_use"].strip() for e in lim["entries"]))
        joined = " ".join(e["blocked_use"] for e in lim["entries"]).lower()
        for needle in self.b.cfg["needles"]:
            self.assertIn(needle, joined)
        for e in lim["entries"]:
            self.assertTrue(e["support"].startswith("test: ") or e["support"].startswith("[TARGET]"), e["id"])

    def test_zero_untagged_unit_numbers(self):
        import hb_claim
        self.assertEqual(hb_claim.untagged_numbers(self.b.prose()), [])

    def test_untagged_number_checker_is_not_vacuous(self):
        import hb_claim
        self.assertEqual(hb_claim.untagged_numbers("encode takes 9.5 ms here"), ["9.5 ms"])

    def test_claims_agree_with_receipts(self):
        import hb_claim
        claims = self.b.hb_blocks("hb.claim.v2")
        self.assertGreaterEqual(len(claims), 2)
        for c in claims:
            if c["etag"] == "MEASURED":
                self.assertTrue(c["receipts"], c["id"])
        self.assertEqual(hb_claim.g_claim_book(self.b.text, ROOT, [c for c in claims if "value" in c]), [])

    def test_claim_check_detects_a_wrong_number(self):
        import hb_claim
        c = dict(self.b.hb_blocks("hb.claim.v2")[0])
        c["value"] = dict(c["value"], number="99999")
        self.assertNotEqual(hb_claim.g_claim_book("", ROOT, [c]), [])

    def test_no_forbidden_citations(self):
        for bad in self.b.cfg["forbidden"]:
            self.assertNotIn(bad, self.b.text)

    def test_exit_codes_come_from_errors_py(self):
        from visioncore import errors
        used = set(re.findall(r"`(E_[A-Z0-9_]+)` \| (\d) \|", self.b.text))
        self.assertTrue(used)
        for code, rc in used:
            self.assertEqual(errors.CODES[code], int(rc), code)
        self.assertTrue(all(int(rc) != 1 for _c, rc in used))
        self.assertIn("rc 1 is never used", self.b.text)

    def test_receipts_recorded_clean(self):
        for rid in self.b.cfg["receipts"]:
            rc = json.loads(read(os.path.join(self.b.dir, "receipts", rid + ".json")))
            self.assertEqual(rc["exit_code"], 0, rid)
            self.assertIsNotNone(rc["commit"], rid)
            self.assertTrue(rc["tree_clean"], rid)
        mine = [x for x in admission.check_receipts(HB, ROOT) if self.BOOK in x or any(r in x for r in self.b.cfg["receipts"])]
        self.assertEqual(mine, [])

    def test_seed_slots_describe_the_book(self):
        sl = json.loads(read(os.path.join(self.b.dir, "seed", "slots.json")))
        self.assertEqual((sl["book"], sl["book_id"], sl["profile"]), (self.BOOK, self.b.cfg["bid"], "hb-hybrid/v1"))
        self.assertEqual(tuple(sl["p3_symbols"]), self.b.cfg["p3"])
        self.assertEqual(tuple(sl["inv"]), self.b.cfg["inv"])
        self.assertEqual(sl["env"]["HBV2_SRC_DIRS"], self.b.cfg["src"])


class TempCorpus(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.hb = os.path.join(self.d, "hyperbooks")
        os.makedirs(self.hb)

    def corpus(self, books, profiled):
        live = json.loads(read(os.path.join(HB, "COMPANION_FORMAT.json")))
        for b in books:
            shutil.copytree(os.path.join(HB, b), os.path.join(self.hb, b), ignore=shutil.ignore_patterns("__pycache__"))
        d = {k: v for k, v in live.items() if k not in ("profiles", "expected_books", "receipt_inventory")}
        d["expected_books"] = list(books)
        d["receipt_inventory"] = {b: live["receipt_inventory"][b] for b in books}
        d["profiles"] = {b: "hb-hybrid/v1" for b in profiled}
        with open(os.path.join(self.hb, "COMPANION_FORMAT.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)

    def check(self):
        return admission.check_corpus(self.hb, ROOT)


class LockstepMixin:
    BOOK = None

    def test_profiled_book_accepted(self):
        self.corpus([self.BOOK], [self.BOOK])
        self.assertEqual(self.check(), [])

    def test_profiled_book_rejected_without_symbols_jsonl(self):
        self.corpus([self.BOOK], [self.BOOK])
        os.remove(os.path.join(self.hb, self.BOOK, "symbols.jsonl"))
        self.assertTrue(any("requires symbols.jsonl" in x for x in self.check()))

    def test_profiled_book_rejected_without_index_or_fence(self):
        self.corpus([self.BOOK], [self.BOOK])
        shutil.rmtree(os.path.join(self.hb, self.BOOK, "index"))
        self.assertTrue(any("index/ID" in x for x in self.check()))
        p = os.path.join(self.hb, self.BOOK, "book.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(read(p).replace("<!-- HB:GEN END hb-s9 -->", ""))
        self.assertTrue(any("fence hb-s9" in x for x in self.check()))

    def test_unprofiled_declaration_of_same_book_is_rejected(self):
        self.corpus([self.BOOK], [])
        self.assertTrue(any("no profile" in x for x in self.check()))


class RebuildMixin:
    BOOK = None
    FILES = ("book.md", "symbols.jsonl", "nodes.jsonl", "anchors.jsonl", "claims.jsonl", "graph.json", "aliases.json", "manifest.json",
             "index/cols.json", "index/post.json", "index/vocab.json", "index/ID")

    def _read(self, root):
        out = {}
        for f in self.FILES:
            with open(os.path.join(root, f), "rb") as fh:
                out[f] = fh.read()
        return out

    def _run(self, root):
        env = dict(os.environ, HBV2_ROOT=root, HBV2_BOOK_SUB=".", HBV2_SUBJECT=".", PYTHONDONTWRITEBYTECODE="1", HBV2_SRC_DIRS=CFG[self.BOOK]["src"])
        for argv in (["hb_extract2.py", "--out", os.path.join(root, "out")], ["hb_trace.py", "--out", os.path.join(root, "out", "trace.json")],
                     ["hb_build.py", "--render-derived"], ["hb_index.py"]):
            r = subprocess.run([sys.executable, os.path.join(ROOT, "hbtools_v2", argv[0])] + argv[1:], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0, argv[0] + r.stderr[-400:])

    def test_rebuild_twice_is_byte_identical_to_committed(self):
        d = tempfile.mkdtemp(prefix="m3rb")
        self.addCleanup(shutil.rmtree, d, True)
        root = os.path.join(d, self.BOOK)
        os.makedirs(os.path.join(root, "out"))
        shutil.copy(os.path.join(HB, self.BOOK, "book.md"), root)
        self._run(root)
        first = self._read(root)
        self._run(root)
        self.assertEqual(first, self._read(root))
        for f in self.FILES:
            with open(os.path.join(HB, self.BOOK, f), "rb") as fh:
                self.assertEqual(first[f], fh.read(), "committed %s is stale" % f)


class S4Checks(HybridChecks, unittest.TestCase):
    BOOK = "HB-VC-s4-regions"


class S5Checks(HybridChecks, unittest.TestCase):
    BOOK = "HB-VC-s5-gop"


class S4Lockstep(LockstepMixin, TempCorpus):
    BOOK = "HB-VC-s4-regions"


class S5Lockstep(LockstepMixin, TempCorpus):
    BOOK = "HB-VC-s5-gop"


class S4Rebuild(RebuildMixin, unittest.TestCase):
    BOOK = "HB-VC-s4-regions"


class S5Rebuild(RebuildMixin, unittest.TestCase):
    BOOK = "HB-VC-s5-gop"


# ---------------------------------------------------------------- book versus code
class S4BookVersusCode(unittest.TestCase):
    def test_constants_in_the_book_match_regions_py(self):
        from visioncore import regions
        t = read(os.path.join(HB, "HB-VC-s4-regions", "book.md"))
        self.assertEqual((regions.MATCH_OVERLAP_PPM, regions.SHADE_ONLY_PPM), (500000, 900000))
        self.assertIn("`MATCH_OVERLAP_PPM = 500000`", t)
        self.assertIn("`SHADE_ONLY_PPM = 900000`", t)

    def test_shift_is_round_half_up_by_floor_division(self):
        """INV-S4-SHIFT-FLOOR: a C-truncating shift would differ for negative mv."""
        s = 16
        for mv in (-24, -17, -9, -8, -1, 0, 7, 8, 15, 24):
            self.assertEqual((mv + s // 2) // s, -((-(mv + s // 2) + s - 1) // s))
        self.assertEqual((-9 + 8) // 16, -1)           # floor
        self.assertEqual(int((-9 + 8) / 16), 0)        # truncation would give 0

    def test_ownership_phrase_matches_code(self):
        import inspect
        from visioncore import regions
        src = inspect.getsource(regions.match_regions)
        self.assertIn('a["group"] == "MOV"', src)
        self.assertIn("(a[\"mv\"][0] + s // 2) // s", src)


class NarrativeTrace(unittest.TestCase):
    def test_s4_trace_is_true(self):
        import numpy as np
        from visioncore import layout, regions as R
        from visioncore.change import TILE_DT

        def mk(cells):
            items = sorted((layout.morton(x, y), v) for (x, y), v in cells.items())
            t = np.zeros(len(items), TILE_DT)
            for i, (m, v) in enumerate(items):
                t[i]["morton"], t[i]["cls"], t[i]["resid"] = m, v, 10
            return t
        blk = lambda x0: {(x, y): 2 for x in range(x0, x0 + 2) for y in (3, 4)}  # noqa: E731
        f7 = dict(blk(3))
        f7[(10, 10)] = 5
        r7 = R.regions_of(mk(f7), 1280, 720, 7)
        self.assertEqual(sorted(r["group"] for r in r7), ["CHG", "FRZ"])
        chg = next(r for r in r7 if r["group"] == "CHG")
        self.assertEqual((chg["n_tiles"], chg["bbox_t"], chg["block"]), (4, [3, 3, 4, 4], [3, "0000", "003f"]))
        r8 = R.regions_of(mk(blk(4)), 1280, 720, 8)
        lk = R.match_regions(r7, r8, (16, 0), 16)
        self.assertEqual(lk["links"], [[chg["id"], r8[0]["id"], 4]])
        self.assertEqual((lk["split"], lk["merge"]), ([], []))

    def test_s5_trace_is_true(self):
        from tests import test_gop as T
        from visioncore import gop
        self.assertEqual((T.G.N, T.G.s, len(gop._cells(T.G.gw, T.G.gh)[3])), (160, 16, 4))
        seq = T.run_seq(25)
        gops = gop.encode_run(seq, T.G)
        plan = gop.plan_gops([gop.dirty_ppm_of(int(gop.dirty_mask(t).sum()), T.G.N) for t in seq])
        self.assertTrue(all(p["end"] - p["start"] <= 20 for p in plan))
        got = [[fr for _t, fr in gop.decode_gop(g.blob)[1]] for g in gops]
        self.assertEqual(got, T.expected_committed(seq, plan))
        g0 = gops[0]
        hdr = gop.parse_header(g0.blob)
        body = g0.blob[gop.HDR_SIZE + hdr["izlen"]:]
        t, nd, pad1 = struct.unpack_from("<IHH", body, 0)
        self.assertEqual(pad1, 0)
        self.assertEqual(len(body), hdr["body_len"])
        self.assertLessEqual(gop.PHDR_SIZE + 8 * 4 + struct.unpack_from("<I", body, 16)[0], len(body))


class S5BookVersusCode(unittest.TestCase):
    def test_header_offsets_in_the_book_match_struct(self):
        from visioncore import gop
        t = read(os.path.join(HB, "HB-VC-s5-gop", "book.md"))
        names = ("magic", "ver", "hdr_len", "gw", "gh", "tile_px", "K", "f0", "n_frames", "flags", "izlen", "body_len", "key_sha8", "body_sha8")
        offs, off = {}, 0
        for nm, ch in zip(names, ("4s", "H", "H", "H", "H", "H", "H", "I", "H", "H", "I", "I", "Q", "Q")):
            sz = struct.calcsize("<" + ch)
            offs[nm], off = off, off + sz
        self.assertEqual(off, gop.HDR_SIZE)
        line = re.search(r"Header \(`HDR_FMT`\) offsets: (.*?)\.", t).group(1)
        for nm, o in re.findall(r"(\w+) (\d+)", line.split("; ")[0].replace("`HDR_SIZE`", "")):
            if nm in offs:
                self.assertEqual(offs[nm], int(o), nm)
        pnames = ("t", "n_dirty", "pad", "gdx", "gdy", "gshare_ppm", "dlen", "pad")
        poff, off = [], 0
        for ch in ("I", "H", "H", "h", "h", "I", "I", "I"):
            poff.append(off)
            off += struct.calcsize("<" + ch)
        self.assertEqual(off, gop.PHDR_SIZE)
        pl = re.search(r"PHdr offsets: (.*?)\.", t).group(1)
        self.assertEqual([int(x) for x in re.findall(r"\w+ (\d+)", pl)], poff)
        self.assertIn("`PHDR_FMT`", t)
        self.assertEqual((gop.HDR_FMT, gop.PHDR_FMT), ("<4sHHHHHHIHHIIQQ", "<IHHhhIII"))

    def test_bounds_and_constants_match_code_and_receipt(self):
        from visioncore import gop, params
        rc = json.loads(read(os.path.join(HB, "HB-VC-s5-gop", "receipts", "R-s5-gop-rle-bound.json")))["metrics"]
        self.assertEqual(gop.rle_bound(131072), rc["rle_bound_131072"])
        self.assertEqual(gop.max_gop_bytes(4096, 20, 64, 64), rc["max_gop_bytes_4096_20"])
        self.assertEqual(gop.max_gop_bytes(4096, 20, 64, 64), 2651295)
        self.assertEqual(gop.rle_bound(4), rc["n4_encoded"])
        self.assertEqual(gop.rle_bound(512), rc["n512_encoded"])
        for n in (0, 1, 254, 255, 256, 510, 511, 10000):
            self.assertEqual(gop.rle_bound(n), n + 2 + 2 * (n // 255))
            self.assertEqual(gop.zlib_bound(n), n + (n >> 12) + (n >> 14) + (n >> 25) + 13)
        self.assertEqual(params.PARAMS["GOP_K"], 20)
        self.assertEqual((gop.FROZEN_TRACK_PPM, gop.CYCLE_P_MAX, gop.FLAGS_MASK), (500000, 8, 7))
        self.assertEqual((params.PARAMS["CUT_PPM_1F"], params.PARAMS["CUT_PPM_2F"]), (800000, 500000))

    def test_error_codes_not_registered_carry_rc2_via_gerr(self):
        from visioncore import errors, gop
        for code in ("E_DELTA_TOKEN", "E_DELTA_LEN", "E_DELTA_BOUND", "E_GOP_MAGIC", "E_GOP_VERSION", "E_GOP_GRID", "E_GOP_PAD", "E_GOP_MASK"):
            self.assertNotIn(code, errors.CODES, code)
            self.assertEqual(gop._gerr(code).rc, 2, code)
        t = read(os.path.join(HB, "HB-VC-s5-gop", "book.md"))
        self.assertIn("are not registered in `errors.CODES`", t)


class Limits(unittest.TestCase):
    """The hb-s6 limits are facts about the current code; flip these when the RT fix lands (then update the book)."""

    @staticmethod
    def _rebuilt(blob, body, nframes=None):
        from visioncore import gop
        h = gop.parse_header(blob)
        off = gop.HDR_SIZE + h["izlen"]
        hdr = bytearray(blob[:gop.HDR_SIZE])
        struct.pack_into("<I", hdr, 28, len(body))
        struct.pack_into("<Q", hdr, 40, gop._sha8(body))
        if nframes is not None:
            struct.pack_into("<H", hdr, 20, nframes)
        return bytes(hdr) + blob[gop.HDR_SIZE:off] + body

    def test_truncated_mask_leaks_valueerror(self):
        from tests import test_gop as T
        from visioncore import gop
        from visioncore.errors import VqaError
        blob = gop.encode_run(T.run_seq(6), T.G)[0].blob
        h = gop.parse_header(blob)
        body = blob[gop.HDR_SIZE + h["izlen"]:]
        bad = self._rebuilt(blob, body[:gop.PHDR_SIZE + 4])
        with self.assertRaises(Exception) as c:
            gop.decode_gop(bad)
        self.assertIsInstance(c.exception, ValueError)
        self.assertNotIsInstance(c.exception, VqaError)

    def test_zero_n_frames_is_accepted(self):
        from tests import test_gop as T
        from visioncore import gop
        blob = gop.encode_run(T.run_seq(6), T.G)[0].blob
        hdr, frames = gop.decode_gop(self._rebuilt(blob, b"", 0))
        self.assertEqual((hdr["n_frames"], len(frames)), (0, 1))

    def test_write_gops_replaces_the_two_files_one_after_the_other(self):
        import inspect
        from visioncore import gop
        src = inspect.getsource(gop.write_gops)
        self.assertEqual(src.count("canon.write_atomic("), 2)
        self.assertLess(src.index('"gops.bin"'), src.index('"gops.jsonl"'))

    def test_decoder_and_codec_rejections_documented_in_inv(self):
        from visioncore import gop
        from visioncore.errors import VqaError
        with self.assertRaises(VqaError) as c:
            gop.rle_decode(bytes(gop.rle_bound(4) + 1), 4)
        self.assertEqual((c.exception.code, c.exception.rc), ("E_DELTA_BOUND", 2))
        with self.assertRaises(VqaError) as c:
            gop.parse_header(b"short")
        self.assertEqual((c.exception.code, c.exception.rc), ("E_GOP_MAGIC", 2))


if __name__ == "__main__":
    unittest.main()
