"""HBM1-B lane M1: HB-VC-a0-arch and HB-VC-s0-grid in the hb-hybrid/v1 profile (HB_DECISIONS 2.1-2.3, G-FLOOR).

(a) anchors, GOLD blocks and machine blocks of both books; (b) G-FLOOR; (c) lockstep admission accepts the profiled books and
rejects them without symbols.jsonl; (d) the generated companions rebuild byte-identically; (e) brute-force oracles for the
invariants the books state (grid rule, Morton, rank, padding, aligned blocks, record sizes, canonical hash, cls-before-xh64);
(f) in-process mutants, each killed only by its named oracle after a clean control run."""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hyperbooks", "_tools"))
sys.path.insert(0, os.path.join(ROOT, "hbtools_v2"))
sys.dont_write_bytecode = True
import admission  # noqa: E402

from visioncore import canon, desc, errors, ids, layout, params, pipeline  # noqa: E402
from visioncore.grid import Grid, aligned_block  # noqa: E402

HB = os.path.join(ROOT, "hyperbooks")
PM_BLOCK = re.compile(r"<!-- PM:BEGIN (\S+) -->\n```json\n(.*?)\n```\n<!-- PM:END \1 -->", re.S)
HB_BLOCK = re.compile(r"<!-- HB:BEGIN (hb\.[a-z_]+\.v\d+) -->\n```json\n(.*?)\n```\n<!-- HB:END -->", re.S)
SECTION_PM = {0: ["pm.node.v2", "pm.reading.v2"], 1: ["pm.claims.v2"], 2: ["pm.objects.v2", "pm.quantities.v2"], 3: ["pm.roadmap.v2"],
              4: ["pm.triptych.v2"], 5: ["pm.domains.v2"], 6: ["pm.limits.v2", "pm.search.v1"], 9: ["pm.edges.v2"]}
P3_FIELDS = ("INPUTS", "PRECONDITIONS", "OUTPUTS", "TIME", "SPACE", "ARITHMETIC", "FAILURE")
FORBIDDEN = ("ADV_RT1", "PLAN_mathcodec", "EX-MA02", "EX-MC01", "EX-OC03", "EX-OC05")

S0_SYMS = ["visioncore/grid.py::" + s for s in ("Grid", "Grid.pad", "Grid.pad_flags", "field_ids_of_rect", "aligned_block")]
A0_SYMS = ["visioncore/%s.py::%s" % (m, s) for m, names in (
    ("ids", ("field_id", "region_id", "parse_any", "field_ids_of_rect", "track_key")),
    ("layout", ("grid_params", "morton", "unmorton", "pack_tile_header", "parse_tile_header", "check_tile_records")),
    ("canon", ("dumps", "loads", "sha256_obj", "write_atomic")), ("errors", ("VqaError", "result_line")),
    ("params", ("effective", "params_sha256")),
    ("topology", ("parse_cpuinfo", "cpus_by_part", "big_cpus", "parse_cpu_list", "resolve_cpus", "pin")),
    ("parallel", ("describe_many",)), ("pipeline", ("analyze", "shm_run_dir", "open_shm_run")),
    ("frame", ("Frame", "validate_manifest")), ("decode", ("png", "png_fast"))) for s in names]
A0_SRC = ("visioncore/ids.py,visioncore/layout.py,visioncore/canon.py,visioncore/errors.py,visioncore/params.py,visioncore/topology.py,"
          "visioncore/parallel.py,visioncore/pipeline.py,visioncore/frame.py,visioncore/decode.py,tests/test_registry.py,"
          "tests/test_registry_fix.py,tests/test_e6_ids_l8.py,tests/test_decode.py,tests/test_topology.py,tests/test_parallel.py,tests/test_pipeline.py")

CFG = {
    "HB-VC-s0-grid": dict(
        syms=S0_SYMS, src="visioncore/grid.py,tests/test_grid.py", receipts=("R-s0-grid-unittest", "R-s0-grid-table"),
        modules={"visioncore/grid.py": ("INV-S0-CELL-CAP", "INV-S0-RANK-BIJECTION", "INV-S0-EDGE-PAD", "INV-S0-PAD-COMMUTES", "INV-S0-PAD-FLAGS", "INV-S0-BLOCK-RANGE")},
        anchors=("hb-s5.grid",), needles=("per frame", "no tiles", "s = 8"), bid="HB-VCS0-001",
        codes=("E_GRID", "E_VC_ID_RANGE")),
    "HB-VC-a0-arch": dict(
        syms=A0_SYMS, src=A0_SRC, receipts=("R-a0-arch-unittest",),
        modules={"visioncore/%s.py" % m: ids_ for m, ids_ in (
            ("ids", ("INV-A0-IDS-ONLY", "INV-A0-IDS-STRICT", "INV-A0-IDS-RANGE")), ("layout", ("INV-A0-GRIDRULE", "INV-A0-MORTON", "INV-A0-TILE-ABI")),
            ("canon", ("INV-A0-NOFLOAT", "INV-A0-CANON-STABLE", "INV-A0-ATOMIC")), ("errors", ("INV-A0-RC", "INV-A0-RESULT-LINE")),
            ("params", ("INV-A0-PARAMS-INT", "INV-A0-PARAMS-PROFILE")), ("topology", ("INV-A0-TOPO-PURE", "INV-A0-TOPO-INTERSECT")),
            ("parallel", ("INV-A0-PAR-BYTEID", "INV-A0-PAR-LOUD")), ("pipeline", ("INV-A0-PIPE-DET", "INV-A0-PIPE-REFUSE", "INV-A0-PIPE-ORDER")),
            ("frame", ("INV-A0-FRAME-SHAPE", "INV-A0-FRAME-MANIFEST")), ("decode", ("INV-A0-DECODE-STRICT", "INV-A0-DECODE-FAST")))},
        anchors=tuple("hb-s5.%s" % m for m in ("ids", "layout", "canon", "errors", "params", "topology", "parallel", "pipeline", "frame", "decode")),
        needles=("widen ids", "reset_guard", "literal range", "native claim"), bid="HB-VCA0-001", codes=("E_VC_ID_SYNTAX", "E_VC_PARAM_RANGE", "E_BUDGET")),
}


def _read(book, name="book.md"):
    with open(os.path.join(HB, book, name), encoding="utf-8") as f:
        return f.read()


class BookChecks:
    """Mixin: the structural checks of one hybrid book (BOOK is set by the concrete class)."""
    BOOK = None

    @classmethod
    def setUpClass(cls):
        cls.cfg = CFG[cls.BOOK]
        cls.bd = os.path.join(HB, cls.BOOK)
        cls.text = _read(cls.BOOK)

    def sections(self):
        marks = [(int(m.group(1)), m.start()) for m in re.finditer(r'^## <a id="hb-s(\d+)"></a>', self.text, re.M)]
        return {n: self.text[st:marks[i + 1][1] if i + 1 < len(marks) else len(self.text)] for i, (n, st) in enumerate(marks)}

    def hb_blocks(self, schema):
        return [json.loads(m.group(2)) for m in HB_BLOCK.finditer(self.text) if m.group(1) == schema]

    def pm_block(self, name):
        return next(json.loads(m.group(2)) for m in PM_BLOCK.finditer(self.text) if m.group(1) == name)

    def prose(self):
        return re.sub(r"```.*?```", " ", self.text, flags=re.S)

    # (a) structure
    def test_anchors_hb_s0_to_s10_present_in_order(self):
        pos = []
        for n in range(11):
            m = re.search(r'<a id="hb-s%d"></a><a id="pm-s%d"></a>' % (n, n), self.text)
            self.assertIsNotNone(m, "hb-s%d anchor missing" % n)
            pos.append(m.start())
        self.assertEqual(pos, sorted(pos))
        self.assertEqual(len(self.sections()), 11)

    def test_gold_blocks_in_their_sections(self):
        secs = self.sections()
        for n, names in SECTION_PM.items():
            got = [m.group(1) for m in PM_BLOCK.finditer(secs[n])]
            for name in names:
                self.assertIn(name, got, "hb-s%d lacks %s" % (n, name))
        self.assertEqual([m.group(1) for m in PM_BLOCK.finditer(secs[0])], SECTION_PM[0])

    def test_every_pm_block_begin_parses(self):
        begins = re.findall(r"<!-- PM:BEGIN (\S+) -->", self.text)
        self.assertEqual(len(begins), 11)
        parsed = {m.group(1): json.loads(m.group(2)) for m in PM_BLOCK.finditer(self.text)}
        self.assertEqual(sorted(begins), sorted(parsed))
        for name, o in parsed.items():
            self.assertEqual(o["schema"], name)
        self.assertEqual(parsed["pm.node.v2"]["pm_id"], self.cfg["bid"])

    def test_machine_blocks_parse_and_match_schemas(self):
        import schemalib
        sdir = os.path.join(ROOT, "standards", "schemas")
        n = 0
        for m in HB_BLOCK.finditer(self.text):
            o = json.loads(m.group(2))
            self.assertEqual(schemalib.validate(o, m.group(1), schema_dir=sdir), [], m.group(1))
            n += 1
        self.assertGreaterEqual(n, 12)

    def test_generated_sections_only_in_fences(self):
        secs = self.sections()
        for n in (8, 9, 10):
            self.assertIn("<!-- HB:GEN BEGIN hb-s%d -->" % n, secs[n])
            self.assertIn("<!-- HB:GEN END hb-s%d -->" % n, secs[n])
        self.assertIn("`%s`" % self.cfg["syms"][0], secs[8])

    def test_mathlinks_only_sealed_or_ausente(self):
        rows = [json.loads(ln) for ln in _read(self.BOOK, "mathlinks.jsonl").splitlines() if ln.strip()]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn(r["status"], ("SEALED", "AUSENTE"))

    # (b) G-FLOOR
    def test_seven_fields_per_public_symbol(self):
        s4 = self.sections()[4]
        for sym in self.cfg["syms"]:
            m = re.search(r"# PM-P3-HEADER: %s\n((?:# [A-Z]+: .*\n)+)" % re.escape(sym), s4)
            self.assertIsNotNone(m, sym)
            self.assertEqual(tuple(re.findall(r"^# ([A-Z]+):", m.group(1), re.M)), P3_FIELDS, sym)
            for ln in m.group(1).splitlines():
                self.assertGreater(len(ln.split(":", 1)[1].strip()), 0, "%s empty field" % sym)

    def test_every_hb_s5_module_has_inv_and_au(self):
        blocks = {b["module"]: b for b in self.hb_blocks("hb.inv.v2")}
        self.assertEqual(sorted(blocks), sorted(self.cfg["modules"]))
        s5 = self.sections()[5]
        for mod, need in self.cfg["modules"].items():
            b = blocks[mod]
            self.assertGreaterEqual(len(b["inv"]), 2, mod)
            self.assertLessEqual(len(b["inv"]), 6, mod)
            self.assertGreaterEqual(len(b["au"]), 1, mod)
            self.assertLessEqual(len(b["au"]), 4, mod)
            self.assertTrue({i["id"] for i in b["inv"]} >= set(need), mod)
            for item in b["inv"] + b["au"]:
                self.assertLessEqual(len(json.dumps(item["text"]).encode()), 600 + 2, item["id"])
        for a in self.cfg["anchors"]:
            self.assertIn('<a id="%s"></a>' % a, s5)

    def test_every_governed_symbol_exists(self):
        syms = {json.loads(ln)["id"] for ln in _read(self.BOOK, "symbols.jsonl").splitlines() if ln.strip()}
        for b in self.hb_blocks("hb.inv.v2"):
            for item in b["inv"] + b["au"]:
                for s in item["symbols"]:
                    self.assertIn(s, syms, item["id"])

    def test_search_block_nonempty(self):
        s = self.pm_block("pm.search.v1")
        for k in ("questions_answered", "engineering_tasks", "synonyms", "exclusions", "keywords", "retrieval_triggers"):
            self.assertTrue(s[k], k)

    def test_blocked_use_content(self):
        lim = self.pm_block("pm.limits.v2")
        self.assertTrue(all(e["blocked_use"].strip() for e in lim["entries"]))
        joined = " ".join(e["blocked_use"] for e in lim["entries"]).lower()
        for needle in self.cfg["needles"]:
            self.assertIn(needle, joined)
        for e in lim["entries"]:   # every blocked_use is paired with a test or a [TARGET]
            self.assertTrue(e["support"].startswith("test") or "[TARGET]" in e["support"] or e["evidence"]["kind"] != "TARGET", e["id"])

    def test_zero_untagged_unit_numbers(self):
        import hb_claim
        self.assertEqual(hb_claim.untagged_numbers(self.prose()), [])

    def test_claims_agree_with_receipts(self):
        import hb_claim
        claims = self.hb_blocks("hb.claim.v2")
        self.assertGreaterEqual(len(claims), 2)
        for c in claims:
            if c["etag"] == "MEASURED":
                self.assertTrue(c["receipts"], c["id"])
        self.assertEqual(hb_claim.g_claim_book(self.text, ROOT, [c for c in claims if "value" in c]), [])

    def test_no_forbidden_citations(self):
        for bad in FORBIDDEN:
            self.assertNotIn(bad, self.text)

    def test_excerpts_cited_exist_in_ctx_names(self):
        cited = set(re.findall(r"EX-[A-Z0-9-]+", self.text))
        self.assertTrue(cited <= {"EX-VG01", "EX-VG02", "EX-OC04", "EX-SW04"}, cited)

    def test_exit_codes_come_from_errors_py(self):
        used = set(re.findall(r"`(E_[A-Z0-9_]+)` \| (\d) \|", self.text))
        self.assertTrue(used)
        for code, rc in used:
            self.assertEqual(errors.CODES[code], int(rc), code)
        self.assertTrue(all(int(rc) != 1 for _c, rc in used))
        self.assertIn("rc 1 is never used", self.text)
        for c in self.cfg["codes"][:2]:
            self.assertIn(c, {u[0] for u in used})

    def test_receipts_recorded_clean(self):
        for rid in self.cfg["receipts"]:
            path = os.path.join(self.bd, "receipts", rid + ".json")
            with open(path, encoding="utf-8") as f:
                rc = json.load(f)
            self.assertEqual(rc["exit_code"], 0, rid)
            self.assertIsNotNone(rc["commit"], rid)
            self.assertTrue(rc["tree_clean"], rid)
            self.assertEqual(admission.check_receipt(path, ROOT), [], rid)

    def test_companion_set_is_complete(self):
        for f in ("manifest.json", "aliases.json", "symbols.jsonl", "anchors.jsonl", "graph.json", "nodes.jsonl", "claims.jsonl",
                  "index/cols.json", "index/post.json", "index/vocab.json", "index/ID", "seed/slots.json", "mathlinks.jsonl"):
            self.assertTrue(os.path.isfile(os.path.join(self.bd, f)), f)
        for f in ("triptych.json", "imports.json"):
            self.assertFalse(os.path.exists(os.path.join(self.bd, f)), f)

    # (d) deterministic rebuild
    FILES = ("book.md", "symbols.jsonl", "nodes.jsonl", "anchors.jsonl", "claims.jsonl", "graph.json", "aliases.json", "manifest.json",
             "index/cols.json", "index/post.json", "index/vocab.json", "index/ID")

    def _run(self, root):
        env = dict(os.environ, HBV2_ROOT=root, HBV2_BOOK_SUB=".", HBV2_SUBJECT=".", PYTHONDONTWRITEBYTECODE="1", HBV2_SRC_DIRS=self.cfg["src"])
        for argv in (["hb_extract2.py", "--out", os.path.join(root, "out")], ["hb_trace.py", "--out", os.path.join(root, "out", "trace.json")],
                     ["hb_build.py", "--render-derived"], ["hb_index.py"]):
            r = subprocess.run([sys.executable, os.path.join(ROOT, "hbtools_v2", argv[0])] + argv[1:], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=900)
            self.assertEqual(r.returncode, 0, argv[0] + r.stderr[-400:])

    def test_rebuild_twice_is_byte_identical_to_committed(self):
        d = tempfile.mkdtemp(prefix="m1rb")
        self.addCleanup(shutil.rmtree, d, True)
        root = os.path.join(d, self.BOOK)
        os.makedirs(os.path.join(root, "out"))
        shutil.copy(os.path.join(self.bd, "book.md"), root)
        reads = []
        for _ in range(2):
            self._run(root)
            cur = {}
            for f in self.FILES:
                with open(os.path.join(root, f), "rb") as fh:
                    cur[f] = fh.read()
            reads.append(cur)
        self.assertEqual(reads[0], reads[1])
        for f in self.FILES:
            with open(os.path.join(self.bd, f), "rb") as fh:
                self.assertEqual(reads[0][f], fh.read(), "committed %s is stale" % f)


class S0Book(BookChecks, unittest.TestCase):
    BOOK = "HB-VC-s0-grid"


class A0Book(BookChecks, unittest.TestCase):
    BOOK = "HB-VC-a0-arch"


# ---------------------------------------------------------------- (c) lockstep admission
def _decl():
    with open(os.path.join(HB, "COMPANION_FORMAT.json"), encoding="utf-8") as f:
        return json.load(f)


BOOKS = ("HB-VC-a0-arch", "HB-VC-s0-grid")


class LockstepAdmission(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.hb = os.path.join(self.d, "hyperbooks")
        os.makedirs(self.hb)

    def corpus(self, books, profiled):
        live = _decl()
        for b in books:
            shutil.copytree(os.path.join(HB, b), os.path.join(self.hb, b), ignore=shutil.ignore_patterns("__pycache__"))
        d = {k: v for k, v in live.items() if k not in ("profiles", "expected_books", "receipt_inventory")}
        d["expected_books"] = list(books)
        d["receipt_inventory"] = {b: live["receipt_inventory"][b] for b in books}
        d["profiles"] = {b: "hb-hybrid/v1" for b in profiled}
        with open(os.path.join(self.hb, "COMPANION_FORMAT.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_profiled_books_accepted(self):
        self.corpus(BOOKS, BOOKS)
        self.assertEqual(admission.check_corpus(self.hb, ROOT), [])

    def test_profiled_book_rejected_without_symbols_jsonl(self):
        for b in BOOKS:
            self.setUp()
            self.corpus([b], [b])
            os.remove(os.path.join(self.hb, b, "symbols.jsonl"))
            self.assertTrue(any("requires symbols.jsonl" in x for x in admission.check_corpus(self.hb, ROOT)), b)

    def test_profiled_book_rejected_without_index_or_fence(self):
        for b in BOOKS:
            self.setUp()
            self.corpus([b], [b])
            shutil.rmtree(os.path.join(self.hb, b, "index"))
            self.assertTrue(any("index/ID" in x for x in admission.check_corpus(self.hb, ROOT)), b)
            p = os.path.join(self.hb, b, "book.md")
            with open(p, encoding="utf-8") as f:
                t = f.read()
            with open(p, "w", encoding="utf-8") as f:
                f.write(t.replace("<!-- HB:GEN END hb-s9 -->", ""))
            self.assertTrue(any("fence hb-s9" in x for x in admission.check_corpus(self.hb, ROOT)), b)

    def test_unprofiled_declaration_is_rejected(self):
        for b in BOOKS:
            self.setUp()
            self.corpus([b], [])
            self.assertTrue(any("no profile" in x for x in admission.check_corpus(self.hb, ROOT)), b)

    def test_live_declaration_matches_companion_files(self):
        """Before lane E1 the live declaration lacks both profile entries and admission reports exactly them; after E1 it reports none."""
        profiles = _decl().get("profiles", {})
        found = admission.check_companions(HB)
        for b in BOOKS:
            self.assertEqual(b in profiles, not any(x.startswith(b) and "no profile" in x for x in found), b)


# ---------------------------------------------------------------- (e) brute-force oracles of the stated invariants
SIZES = (1, 15, 16, 17, 255, 256, 257, 1023, 1024, 1025, 1280, 1920, 4080, 4081, 4097, 8191, 8192)


def _ok_at(W, H, s):
    gw, gh = -(-W // s), -(-H // s)
    return gw * gh <= layout.CELL_MAX and max(gw, gh) <= layout.AXIS_MAX


class Oracles(unittest.TestCase):
    def test_grid_params_minimal_and_total(self):
        self.assertEqual(layout.grid_params(8192, 8192), (128, 64, 64))
        for W in SIZES:
            for H in SIZES:
                s, gw, gh = layout.grid_params(W, H)
                self.assertIn(s, (16, 32, 64, 128), (W, H))
                self.assertEqual((gw, gh), (-(-W // s), -(-H // s)))
                self.assertTrue(_ok_at(W, H, s), (W, H))
                if s > 16:
                    self.assertFalse(_ok_at(W, H, s // 2), (W, H))
                g = Grid(W, H)
                self.assertLessEqual(g.N, 4096)
        for W, H in ((0, 1), (8193, 1), (1, 0), (1, 8193), (1.0, 5), ("5", 5)):
            with self.assertRaises(errors.VqaError) as c:
                layout.grid_params(W, H)
            self.assertEqual((c.exception.code, c.exception.rc), ("E_GRID", 2))

    def test_morton_is_a_bijection(self):
        seen = set()
        for tx in range(256):
            for ty in range(256):
                m = layout.morton(tx, ty)
                self.assertEqual(layout.unmorton(m), (tx, ty))
                seen.add(m)
        self.assertEqual(seen, set(range(65536)))
        for bad in ((256, 0), (0, 256), (-1, 0)):
            with self.assertRaises(errors.VqaError) as c:
                layout.morton(*bad)
            self.assertEqual((c.exception.code, c.exception.rc), ("E_VC_ID_RANGE", 3))

    def test_rank_is_not_the_code(self):
        g = Grid(8192, 16)
        self.assertEqual((g.gw, g.gh), (128, 1))
        self.assertEqual(int(g.mortons[2]), 4)
        self.assertEqual(int(g.order[0, 2]), 2)
        for W, H in ((1280, 720), (8192, 16), (16, 4320), (1000, 700)):
            g = Grid(W, H)
            self.assertTrue(np.all(np.diff(g.mortons.astype(np.int64)) > 0))
            for r in range(g.N):
                self.assertEqual(int(g.order[g.ty[r], g.tx[r]]), r)
                self.assertEqual(int(g.flat[g.order[g.ty[r], g.tx[r]]]), int(g.ty[r]) * g.gw + int(g.tx[r]))
                self.assertEqual(int(g.mortons[r]), layout.morton(int(g.tx[r]), int(g.ty[r])))

    def test_pad_is_zero_copy_when_aligned(self):
        g = Grid(1280, 720)
        a = np.zeros((720, 1280, 3), np.uint8)
        self.assertIs(g.pad(a), a)
        g2 = Grid(1000, 700)
        b = np.zeros((700, 1000, 3), np.uint8)
        self.assertIsNot(g2.pad(b), b)
        self.assertEqual(g2.pad(b).shape, (g2.gh * g2.s, g2.gw * g2.s, 3))

    def test_pad_commutes_with_lift(self):
        rng = np.random.RandomState(7)
        for W, H in ((1000, 700), (37, 21), (100, 17)):
            g = Grid(W, H)
            f = rng.randint(0, 256, (H, W, 3)).astype(np.uint8)
            y = desc.ycocg(f)[0]
            ph, pw = g.gh * g.s - H, g.gw * g.s - W
            expect = np.pad(y, ((0, ph), (0, pw)), mode="edge")
            self.assertTrue(np.array_equal(desc.luma_of_padded(g.pad(f)), expect), (W, H))

    def test_pad_flags_are_the_border_tiles(self):
        for W, H in ((1000, 700), (1280, 720), (37, 21), (8192, 16), (100, 17)):
            g = Grid(W, H)
            f = g.pad_flags()
            want = (g.tx == g.gw - 1) & (W % g.s != 0) | (g.ty == g.gh - 1) & (H % g.s != 0)
            self.assertTrue(np.array_equal(f, want), (W, H))
            self.assertEqual(int(f.sum()), (g.gh if W % g.s else 0) + (g.gw if H % g.s else 0) - (1 if W % g.s and H % g.s else 0))

    def test_aligned_block_minimal_and_contiguous(self):
        for tx0 in range(8):
            for tx1 in range(tx0, 8):
                for ty0 in range(0, 8, 3):
                    for ty1 in range(ty0, 8, 2):
                        k, m0, m1 = aligned_block((tx0, ty0, tx1, ty1))
                        self.assertEqual(m1, m0 + 4 ** k - 1)
                        codes = [layout.morton(x, y) for x in range(tx0, tx1 + 1) for y in range(ty0, ty1 + 1)]
                        self.assertTrue(m0 <= min(codes) and max(codes) <= m1)
                        if k > 0:   # the next smaller aligned block cannot hold the box
                            self.assertTrue((tx0 >> (k - 1)) != (tx1 >> (k - 1)) or (ty0 >> (k - 1)) != (ty1 >> (k - 1)))

    def test_layout_record_sizes(self):
        self.assertEqual((struct.calcsize(layout.TILE_FMT), layout.TILE_SIZE), (32, 32))
        self.assertEqual((struct.calcsize(layout.TILE_HDR_FMT), layout.TILE_HDR_SIZE), (64, 64))
        self.assertEqual((struct.calcsize(layout.GLOBAL_MOTION_FMT), layout.GLOBAL_MOTION_SIZE), (8, 8))
        off = 0
        for f, o in zip(re.findall(r"[HBbQ]", layout.TILE_FMT), layout.TILE_OFFSETS):
            self.assertEqual(o, off)
            off += struct.calcsize("<" + f)
        self.assertEqual(len(layout.TILE_FIELDS), 14)

    def test_canon_hash_covers_the_newline(self):
        o = {"b": [1, 2], "a": "é"}
        s = canon.dumps(o)
        self.assertTrue(s.endswith("\n") and not s.endswith("\n\n"))
        self.assertEqual(canon.sha256_obj(o), hashlib.sha256(s.encode("utf-8")).hexdigest())
        self.assertNotEqual(canon.sha256_obj(o), hashlib.sha256(s[:-1].encode("utf-8")).hexdigest())
        self.assertEqual(canon.sha256_obj({"a": "é", "b": [1, 2]}), canon.sha256_obj(o))

    def test_cls_is_written_before_xh64(self):
        from tests.test_pipeline import make_run
        d = make_run("scroll")
        seen = []
        real = desc.fill_xh64

        def spy(out, padded, grid, prev_xh64=None, hash_all=False):
            seen.append(out["cls"].copy())
            return real(out, padded, grid, prev_xh64, hash_all)

        desc.fill_xh64 = spy
        try:
            run = pipeline.analyze(pipeline.ManifestSource(d / "frames.manifest.json"), None, None, None)
        finally:
            desc.fill_xh64 = real
        self.assertEqual(len(seen), len(run.tiles))
        for k in range(len(seen)):
            self.assertTrue(np.array_equal(seen[k], run.tiles[k]["cls"]), k)
        self.assertTrue(any((c >= layout.CLS_DIRTY_MIN).any() for c in seen[1:]))


# ---------------------------------------------------------------- (f) in-process mutants
def _run_named(name):
    res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.defaultTestLoader.loadTestsFromName(name))
    return res.testsRun, len(res.failures) + len(res.errors)


@contextlib.contextmanager
def _patched_item(d, key, value):
    old = d[key]
    d[key] = value
    try:
        yield
    finally:
        d[key] = old


@contextlib.contextmanager
def _patched(obj, attr, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, old)


def _zero_pad(real):
    def pad(a, pad_width, mode="constant", **kw):
        return real(a, pad_width, mode="constant")
    return pad


MUTANTS = {   # name -> (patch factory, oracles that must each fail)
    "m_pad_zero": (lambda: _patched(np, "pad", _zero_pad(np.pad)), ("tests.test_grid.TestGrid.test_pad_edge_replication",)),
    "m_grid_cap_overflow": (lambda: _patched(layout, "CELL_MAX", 8192), ("tests.test_grid.TestGrid.test_table", "tests.test_registry.TR1.test_grid_table")),
    "m_morton_rowmajor": (lambda: _patched(layout, "morton", lambda tx, ty: (ty << 8) | tx),
                          ("tests.test_grid.TestGrid.test_morton_order_and_roundtrip", "tests.test_registry.TR1.test_morton_roundtrip")),
    "m_rc1_exit": (lambda: _patched_item(errors.CODES, "E_GRID", 1), ("tests.test_registry.TR4.test_codes",)),
    "m_float_in_canon": (lambda: _patched(canon, "_check_ints", lambda o: None), ("tests.test_registry.TR3.test_canonical",)),
}


class MutantKills(unittest.TestCase):
    """CQ-T-2: clean control first, zero mutants is a failure, a kill is credited to the named oracle only."""

    def test_control_run_is_clean(self):
        names = {o for _f, (_p, oracles) in MUTANTS.items() for o in oracles}
        self.assertTrue(names)
        for n in sorted(names):
            ran, bad = _run_named(n)
            self.assertEqual((ran, bad), (1, 0), n)

    def test_noop_patch_is_not_a_kill(self):
        with _patched(layout, "CELL_MAX", layout.CELL_MAX):
            self.assertEqual(_run_named("tests.test_grid.TestGrid.test_table"), (1, 0))

    def test_every_mutant_is_killed_by_its_named_oracle(self):
        self.assertGreaterEqual(len(MUTANTS), 5)
        for name, (patch, oracles) in MUTANTS.items():
            for o in oracles:
                with patch():
                    ran, bad = _run_named(o)
                self.assertEqual(ran, 1, name)
                self.assertGreaterEqual(bad, 1, "%s survived %s" % (name, o))
        for _name, (_p, oracles) in MUTANTS.items():   # the patches are undone: the oracles pass again
            for o in oracles:
                self.assertEqual(_run_named(o), (1, 0), o)


if __name__ == "__main__":
    unittest.main()
