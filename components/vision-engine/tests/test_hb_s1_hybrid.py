"""HBP-W2 lane S1: HB-VC-s1-desc in the hb-hybrid/v1 profile (HB_DECISIONS 2.1-2.3, G-FLOOR).

(a) anchors, GOLD blocks and machine blocks; (b) G-FLOOR; (c) lockstep admission accepts the profiled book and rejects it
without symbols.jsonl; (d) admission still rejects companion files in a non-profiled book (F-B12); (e) the generated
companions rebuild byte-identically."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hyperbooks", "_tools"))
sys.path.insert(0, os.path.join(ROOT, "hbtools_v2"))
sys.dont_write_bytecode = True
import admission  # noqa: E402

HB = os.path.join(ROOT, "hyperbooks")
BOOK = "HB-VC-s1-desc"
BD = os.path.join(HB, BOOK)
with open(os.path.join(BD, "book.md"), encoding="utf-8") as _f:
    TEXT = _f.read()

PM_BLOCK = re.compile(r"<!-- PM:BEGIN (\S+) -->\n```json\n(.*?)\n```\n<!-- PM:END \1 -->", re.S)
HB_BLOCK = re.compile(r"<!-- HB:BEGIN (hb\.[a-z_]+\.v\d+) -->\n```json\n(.*?)\n```\n<!-- HB:END -->", re.S)
SECTION_PM = {0: ["pm.node.v2", "pm.reading.v2"], 1: ["pm.claims.v2"], 2: ["pm.objects.v2", "pm.quantities.v2"], 3: ["pm.roadmap.v2"],
              4: ["pm.triptych.v2"], 5: ["pm.domains.v2"], 6: ["pm.limits.v2", "pm.search.v1"], 9: ["pm.edges.v2"]}
P3_FIELDS = ("INPUTS", "PRECONDITIONS", "OUTPUTS", "TIME", "SPACE", "ARITHMETIC", "FAILURE")
P3_SYMBOLS = ("ycocg", "luma_of_padded", "isqrt", "describe", "describe_padded", "fill_xh64", "xh64_of", "_compute")


def section_texts():
    """-> {n: text of section n} split on the numbered '## <a id="hb-sN">' headings."""
    marks = [(int(m.group(1)), m.start()) for m in re.finditer(r'^## <a id="hb-s(\d+)"></a>', TEXT, re.M)]
    out = {}
    for i, (n, st) in enumerate(marks):
        out[n] = TEXT[st:marks[i + 1][1] if i + 1 < len(marks) else len(TEXT)]
    return out


def hb_blocks(schema):
    return [json.loads(m.group(2)) for m in HB_BLOCK.finditer(TEXT) if m.group(1) == schema]


def prose_for_numbers():
    """Prose with fenced code and machine-block JSON removed (the numbers the untagged-number rule applies to)."""
    return re.sub(r"```.*?```", " ", TEXT, flags=re.S)


class SectionsAndBlocks(unittest.TestCase):
    def test_anchors_hb_s0_to_s10_present_in_order(self):
        pos = []
        for n in range(11):
            m = re.search(r'<a id="hb-s%d"></a><a id="pm-s%d"></a>' % (n, n), TEXT)
            self.assertIsNotNone(m, "hb-s%d anchor missing" % n)
            pos.append(m.start())
        self.assertEqual(pos, sorted(pos))
        self.assertEqual(len(section_texts()), 11)

    def test_gold_blocks_in_their_sections(self):
        secs = section_texts()
        for n, names in SECTION_PM.items():
            got = [m.group(1) for m in PM_BLOCK.finditer(secs[n])]
            for name in names:
                self.assertIn(name, got, "hb-s%d lacks %s" % (n, name))
        # the order inside a section follows the table of HB_DECISIONS 2.1
        self.assertEqual([m.group(1) for m in PM_BLOCK.finditer(secs[0])], SECTION_PM[0])

    def test_every_pm_block_begin_parses(self):
        begins = re.findall(r"<!-- PM:BEGIN (\S+) -->", TEXT)
        self.assertEqual(len(begins), 11)
        parsed = {m.group(1): json.loads(m.group(2)) for m in PM_BLOCK.finditer(TEXT)}
        self.assertEqual(sorted(begins), sorted(parsed))
        for name, o in parsed.items():
            self.assertEqual(o["schema"], name)

    def test_machine_blocks_parse_and_match_schemas(self):
        import schemalib
        sdir = os.path.join(ROOT, "standards", "schemas")
        n = 0
        for m in HB_BLOCK.finditer(TEXT):
            o = json.loads(m.group(2))
            self.assertEqual(schemalib.validate(o, m.group(1), schema_dir=sdir), [], m.group(1))
            n += 1
        self.assertGreaterEqual(n, 12)

    def test_generated_sections_only_in_fences(self):
        secs = section_texts()
        for n in (8, 9, 10):
            self.assertIn("<!-- HB:GEN BEGIN hb-s%d -->" % n, secs[n])
            self.assertIn("<!-- HB:GEN END hb-s%d -->" % n, secs[n])
        self.assertIn("`visioncore/desc.py::describe_padded`", secs[8])

    def test_mathlinks_only_sealed_or_ausente(self):
        with open(os.path.join(BD, "mathlinks.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn(r["status"], ("SEALED", "AUSENTE"))


class GFloor(unittest.TestCase):
    def test_seven_fields_per_listed_symbol(self):
        s4 = section_texts()[4]
        for sym in P3_SYMBOLS:
            m = re.search(r"# PM-P3-HEADER: visioncore/desc\.py::%s\n((?:# [A-Z]+: .*\n)+)" % re.escape(sym), s4)
            self.assertIsNotNone(m, sym)
            keys = re.findall(r"^# ([A-Z]+):", m.group(1), re.M)
            self.assertEqual(tuple(keys), P3_FIELDS, sym)
            for ln in m.group(1).splitlines():
                self.assertGreater(len(ln.split(":", 1)[1].strip()), 0, "%s empty field" % sym)

    def test_hb_s5_desc_inv_and_au(self):
        blocks = [b for b in hb_blocks("hb.inv.v2") if b["module"] == "visioncore/desc.py"]
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertGreaterEqual(len(b["inv"]), 2)
        self.assertGreaterEqual(len(b["au"]), 1)
        ids = {i["id"] for i in b["inv"]}
        for need in ("INV-S1-CAUCHY", "INV-S1-ISQRT", "INV-S1-SIG64-STRICT", "INV-S1-LAZY-XH64", "INV-S1-REUSE-EXACT", "INV-S1-TILE-LOCAL"):
            self.assertIn(need, ids)
        self.assertIn('<a id="hb-s5.desc"></a>', section_texts()[5])

    def test_search_block_nonempty(self):
        s = next(json.loads(m.group(2)) for m in PM_BLOCK.finditer(TEXT) if m.group(1) == "pm.search.v1")
        for k in ("questions_answered", "engineering_tasks", "synonyms", "exclusions", "keywords", "retrieval_triggers"):
            self.assertTrue(s[k], k)

    def test_blocked_use_content(self):
        lim = next(json.loads(m.group(2)) for m in PM_BLOCK.finditer(TEXT) if m.group(1) == "pm.limits.v2")
        self.assertTrue(all(e["blocked_use"].strip() for e in lim["entries"]))
        joined = " ".join(e["blocked_use"] for e in lim["entries"]).lower()
        for needle in ("fnv-1a", "float std", "luma-only xh64", "jitter xh64 is a label"):
            self.assertIn(needle, joined)

    def test_zero_untagged_unit_numbers(self):
        import hb_claim
        self.assertEqual(hb_claim.untagged_numbers(prose_for_numbers()), [])

    def test_claims_agree_with_receipts(self):
        import hb_claim
        claims = hb_blocks("hb.claim.v2")
        self.assertGreaterEqual(len(claims), 3)
        for c in claims:
            if c["etag"] == "MEASURED":
                self.assertTrue(c["receipts"], c["id"])
        found = hb_claim.g_claim_book(TEXT, ROOT, [c for c in claims if "value" in c])
        self.assertEqual(found, [])

    def test_no_forbidden_citations(self):
        for bad in ("ADV_RT1", "PLAN_mathcodec", "EX-MA02", "EX-MC01", "EX-OC03", "EX-OC05"):
            self.assertNotIn(bad, TEXT)

    def test_untagged_number_checker_is_not_vacuous(self):
        import hb_claim
        self.assertEqual(hb_claim.untagged_numbers("describe takes 9.5 ms here"), ["9.5 ms"])

    def test_exit_codes_come_from_errors_py(self):
        from visioncore import errors
        used = set(re.findall(r"`(E_[A-Z0-9_]+)` \| (\d) \|", TEXT))
        self.assertTrue(used)
        for code, rc in used:
            self.assertEqual(errors.CODES[code], int(rc), code)
        self.assertTrue(all(int(rc) != 1 for _c, rc in used))
        self.assertIn("rc 1 is never used", TEXT)

    def test_receipts_recorded_clean(self):
        for n in ("unittest", "bench"):
            with open(os.path.join(BD, "receipts", "R-s1-desc-%s.json" % n), encoding="utf-8") as f:
                rc = json.load(f)
            self.assertEqual(rc["exit_code"], 0)
            self.assertIsNotNone(rc["commit"], n)
            self.assertTrue(rc["tree_clean"], n)
        self.assertEqual(admission.check_receipts(HB, ROOT), [])


def _decl():
    with open(os.path.join(HB, "COMPANION_FORMAT.json"), encoding="utf-8") as f:
        return json.load(f)


class TempCorpus(unittest.TestCase):
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
        return self.hb

    def check(self):
        return admission.check_corpus(self.hb, ROOT)


class LockstepAdmission(TempCorpus):
    def test_live_declaration(self):
        d = _decl()
        self.assertEqual(d["profiles"], {b: "hb-hybrid/v1" for b in d["expected_books"]})
        self.assertEqual(len(d["expected_books"]), 10)
        self.assertIn(BOOK, d["profiles"])
        self.assertEqual(d["receipt_inventory"]["HB-VN-s0s1"], ["R-vn-s0s1-unittest.json"])
        self.assertEqual(admission.check_format(HB), [])
        self.assertEqual(admission.check_companions(HB), [])

    def test_profiled_book_accepted(self):
        self.corpus([BOOK], [BOOK])
        self.assertEqual(self.check(), [])

    def test_profiled_book_rejected_without_symbols_jsonl(self):
        self.corpus([BOOK], [BOOK])
        os.remove(os.path.join(self.hb, BOOK, "symbols.jsonl"))
        self.assertTrue(any("requires symbols.jsonl" in x for x in self.check()))

    def test_profiled_book_rejected_without_index_or_fence(self):
        self.corpus([BOOK], [BOOK])
        shutil.rmtree(os.path.join(self.hb, BOOK, "index"))
        self.assertTrue(any("index/ID" in x for x in self.check()))
        p = os.path.join(self.hb, BOOK, "book.md")
        with open(p, encoding="utf-8") as f:
            t = f.read()
        with open(p, "w", encoding="utf-8") as f:
            f.write(t.replace("<!-- HB:GEN END hb-s9 -->", ""))
        self.assertTrue(any("fence hb-s9" in x for x in self.check()))

    def test_unprofiled_declaration_of_same_book_is_rejected(self):
        self.corpus([BOOK], [])
        self.assertTrue(any("no profile" in x for x in self.check()))

    def test_bad_profile_id_rejected(self):
        self.corpus([BOOK], [BOOK])
        p = os.path.join(self.hb, "COMPANION_FORMAT.json")
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["profiles"][BOOK] = "hb-hybrid/v9"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.assertTrue(any("profile of" in x for x in self.check()))


class NonProfiledGuard(TempCorpus):
    """F-B12: a book without a profile stays on the reduced format; a generated file in it is rejected.
    All live books are profiled, so the reduced book is a temporary fixture: a live book copy stripped of its generated set."""

    RED = "HB-VC-s0-grid"

    def reduced(self):
        self.corpus([self.RED], [])
        bd = os.path.join(self.hb, self.RED)
        for name in sorted(set(admission.PROFILE_REQUIRED) | set(admission.PROFILE_NEVER)):
            p = os.path.join(bd, name)
            if os.path.isfile(p):
                os.remove(p)
        shutil.rmtree(os.path.join(bd, "index"), True)
        return bd

    def test_reduced_book_accepted(self):
        self.reduced()
        self.assertEqual(self.check(), [])

    def test_companion_file_in_reduced_book_rejected(self):
        for name in ("symbols.jsonl", "manifest.json", "graph.json", "nodes.jsonl"):
            self.setUp()
            self.reduced()
            with open(os.path.join(self.hb, self.RED, name), "w") as f:
                f.write("{}\n")
            self.assertTrue(any("no profile" in x and name in x for x in self.check()), name)

    def test_index_dir_in_reduced_book_rejected(self):
        self.reduced()
        os.makedirs(os.path.join(self.hb, self.RED, "index"))
        with open(os.path.join(self.hb, self.RED, "index", "ID"), "w") as f:
            f.write("x\n")
        self.assertTrue(any("index/ID" in x for x in self.check()))

    def test_live_reduced_books_carry_no_companions(self):
        live = [x for x in admission.check_companions(HB) if "no profile" in x]
        self.assertEqual(live, [])


class Rebuild(unittest.TestCase):
    """The generated companions are a pure function of book.md + the subject: a rebuild in a scratch copy is byte-identical."""

    FILES = ("book.md", "symbols.jsonl", "nodes.jsonl", "anchors.jsonl", "claims.jsonl", "graph.json", "aliases.json", "manifest.json",
             "index/cols.json", "index/post.json", "index/vocab.json", "index/ID")

    def _read(self, root):
        out = {}
        for f in self.FILES:
            with open(os.path.join(root, f), "rb") as fh:
                out[f] = fh.read()
        return out

    def _run(self, root):
        env = dict(os.environ, HBV2_ROOT=root, HBV2_BOOK_SUB=".", HBV2_SUBJECT=".", PYTHONDONTWRITEBYTECODE="1",
                   HBV2_SRC_DIRS="visioncore/desc.py,tests/test_desc.py")
        for argv in (["hb_extract2.py", "--out", os.path.join(root, "out")], ["hb_trace.py", "--out", os.path.join(root, "out", "trace.json")],
                     ["hb_build.py", "--render-derived"], ["hb_index.py"]):
            r = subprocess.run([sys.executable, os.path.join(ROOT, "hbtools_v2", argv[0])] + argv[1:], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0, argv[0] + r.stderr[-400:])

    def test_rebuild_twice_is_byte_identical_to_committed(self):
        d = tempfile.mkdtemp(prefix="s1rb")
        self.addCleanup(shutil.rmtree, d, True)
        root = os.path.join(d, BOOK)
        os.makedirs(root)
        shutil.copy(os.path.join(BD, "book.md"), root)
        os.makedirs(os.path.join(root, "out"))
        self._run(root)
        first = self._read(root)
        self._run(root)
        second = self._read(root)
        self.assertEqual(first, second)
        for f in self.FILES:
            with open(os.path.join(BD, f), "rb") as fh:
                self.assertEqual(first[f], fh.read(), "committed %s is stale" % f)


if __name__ == "__main__":
    unittest.main()
