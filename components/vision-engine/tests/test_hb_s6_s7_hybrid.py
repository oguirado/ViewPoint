"""HBM1-B lane M4: HB-VC-s6-pack-cite and HB-VC-s7-vmem in the hb-hybrid/v1 profile (HB_DECISIONS 2.1-2.3, G-FLOOR).

(a) anchors, GOLD blocks and machine blocks; (b) G-FLOOR (7-field header per anchored symbol, INV/AU per module, search block, tagged
numbers); (c) constants and exit codes agree with the code; (d) lockstep admission accepts a profiled book and rejects it without
its generated companions; (e) the generated companions rebuild byte-identically. HB_S67_DIR overrides the hyperbooks directory
(used to show that the pre-migration books fail these tests)."""
import ast
import importlib
import json
import os
import re
import shutil
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

HB = os.environ.get("HB_S67_DIR") or os.path.join(ROOT, "hyperbooks")
PM_BLOCK = re.compile(r"<!-- PM:BEGIN (\S+) -->\n```json\n(.*?)\n```\n<!-- PM:END \1 -->", re.S)
HB_BLOCK = re.compile(r"<!-- HB:BEGIN (hb\.[a-z_]+\.v\d+) -->\n```json\n(.*?)\n```\n<!-- HB:END -->", re.S)
SECTION_PM = {0: ["pm.node.v2", "pm.reading.v2"], 1: ["pm.claims.v2"], 2: ["pm.objects.v2", "pm.quantities.v2"], 3: ["pm.roadmap.v2"],
              4: ["pm.triptych.v2"], 5: ["pm.domains.v2"], 6: ["pm.limits.v2", "pm.search.v1"], 9: ["pm.edges.v2"]}
P3_FIELDS = ("INPUTS", "PRECONDITIONS", "OUTPUTS", "TIME", "SPACE", "ARITHMETIC", "FAILURE")
FORBIDDEN = ("ADV_RT1", "PLAN_mathcodec", "EX-MA02", "EX-MC01", "EX-OC03", "EX-OC05", "35,000", "35000", "80,000", "80000", "FG_ACTIVE", "witness")

S6_TESTS = ["tests/test_pack.py", "tests/test_cite.py"]
S7_TESTS = ["tests/test_vmem_units.py", "tests/test_vmem_store.py", "tests/test_vmem_router.py", "tests/test_vmem_compact.py", "tests/test_vmem_fixes.py"]
BOOKS = {
    "HB-VC-s6-pack-cite": {
        "hb_id": "HB-VCS6-001", "sources": ["visioncore/pack.py", "visioncore/cite.py"], "tests": S6_TESTS,
        "src_dirs": ",".join(["visioncore/pack.py", "visioncore/cite.py"] + S6_TESTS), "modules": {"pack": "visioncore/pack.py", "cite": "visioncore/cite.py"},
        "receipts": ["R-s6-pack-cite-unittest", "R-s6-z1-size", "R-s6-anchors"], "unittest": "R-s6-pack-cite-unittest", "tests_run": 47,
        "extra_p3": ["visioncore/pack.py::RunTokenBudget"], "anchors": 13,
        "need_inv": ["INV-S6-Z1-STATIC", "INV-S6-FIT-LEVELS", "INV-S6-FIT-ORDER", "INV-S6-ATOMIC", "INV-S6-TOKCAP", "INV-S6-BLIND", "INV-S6-CITE-RULES",
                     "INV-S6-STALE-FIRST", "INV-S6-FAILCLOSED", "INV-S6-QUOTE-BYTES", "INV-S6-PASSTHROUGH", "INV-S6-PURE"],
        "blocked": ("scaling claim", "pass result", "minimum quote length", "x8", "killed-by-oracle"),
    },
    "HB-VC-s7-vmem": {
        "hb_id": "HB-VCS7-001", "sources": ["vmem/%s.py" % m for m in ("blobs", "cli", "compact", "gates", "gen", "ingest", "receipts", "router", "store", "tiers", "units")],
        "tests": S7_TESTS, "src_dirs": ",".join(["vmem/"] + S7_TESTS),
        "modules": {m: "vmem/%s.py" % m for m in ("blobs", "gen", "receipts", "store", "ingest", "units", "tiers", "compact", "router", "gates", "cli")},
        "receipts": ["R-s7-vmem-unittest", "R-s7-vmem-inventory"], "unittest": "R-s7-vmem-unittest", "tests_run": 101, "extra_p3": [], "anchors": 16,
        "need_inv": ["INV-S7-GEN-ATOMIC", "INV-S7-JOURNAL-CHAIN", "INV-S7-JOURNAL-ROTATE", "INV-S7-STORE-ATOMIC", "INV-S7-SIG-EXACT", "INV-S7-ABSTAIN",
                     "INV-S7-COMPACT-FG", "INV-S7-TIER-PERMILLE", "INV-S7-UNIT-FG-IFF-PIN"],
        "blocked": ("killed-by-oracle", "determinism proof", "nfs or multi-host", "cross-run"),
    },
}


def read(book, name="book.md"):
    with open(os.path.join(HB, book, name), encoding="utf-8") as f:
        return f.read()


def section_texts(text):
    marks = [(int(m.group(1)), m.start()) for m in re.finditer(r'^## <a id="hb-s(\d+)"></a>', text, re.M)]
    return {n: text[st:marks[i + 1][1] if i + 1 < len(marks) else len(text)] for i, (n, st) in enumerate(marks)}


def hb_blocks(text, schema):
    return [json.loads(m.group(2)) for m in HB_BLOCK.finditer(text) if m.group(1) == schema]


def pm_blocks(text):
    return {m.group(1): json.loads(m.group(2)) for m in PM_BLOCK.finditer(text)}


def anchored_symbols(path):
    """-> [qualname] of every def/class carrying an '# @anchor:' comment on its line or the line above."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    out = []

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + ch.name
                if "@anchor:" in lines[ch.lineno - 1] or (ch.lineno > 1 and lines[ch.lineno - 2].strip().startswith("# @anchor:")):
                    out.append(q)
                if isinstance(ch, ast.ClassDef):
                    walk(ch, q + ".")
    walk(ast.parse(src), "")
    return out


def test_names(paths):
    names = set()
    for p in paths:
        with open(os.path.join(ROOT, p), encoding="utf-8") as f:
            names |= set(re.findall(r"def (test_\w+)", f.read()))
    return names


class PerBook(unittest.TestCase):
    def each(self):
        return [(book, cfg, read(book)) for book, cfg in BOOKS.items()]


class SectionsAndBlocks(PerBook):
    def test_anchors_hb_s0_to_s10_present_in_order(self):
        for book, _cfg, text in self.each():
            pos = []
            for n in range(11):
                m = re.search(r'<a id="hb-s%d"></a><a id="pm-s%d"></a>' % (n, n), text)
                self.assertIsNotNone(m, "hb-s%d anchor missing" % n)
                pos.append(m.start())
            self.assertEqual(pos, sorted(pos))
            self.assertEqual(len(section_texts(text)), 11)

    def test_gold_blocks_in_their_sections(self):
        for _book, _cfg, text in self.each():
            secs = section_texts(text)
            for n, names in SECTION_PM.items():
                got = [m.group(1) for m in PM_BLOCK.finditer(secs[n])]
                for name in names:
                    self.assertIn(name, got, "hb-s%d lacks %s" % (n, name))
            self.assertEqual([m.group(1) for m in PM_BLOCK.finditer(secs[0])], SECTION_PM[0])

    def test_every_pm_block_begin_parses(self):
        for _book, _cfg, text in self.each():
            begins = re.findall(r"<!-- PM:BEGIN (\S+) -->", text)
            self.assertEqual(len(begins), 11)
            parsed = pm_blocks(text)
            self.assertEqual(sorted(begins), sorted(parsed))
            for name, o in parsed.items():
                self.assertEqual(o["schema"], name)

    def test_machine_blocks_parse_and_match_schemas(self):
        import schemalib
        sdir = os.path.join(ROOT, "standards", "schemas")
        for _book, _cfg, text in self.each():
            n = 0
            for m in HB_BLOCK.finditer(text):
                self.assertEqual(schemalib.validate(json.loads(m.group(2)), m.group(1), schema_dir=sdir), [], m.group(1))
                n += 1
            self.assertGreaterEqual(n, 12)

    def test_generated_sections_only_in_fences_and_book_id(self):
        for _book, cfg, text in self.each():
            secs = section_texts(text)
            for n in (8, 9, 10):
                self.assertIn("<!-- HB:GEN BEGIN hb-s%d -->" % n, secs[n])
                self.assertIn("<!-- HB:GEN END hb-s%d -->" % n, secs[n])
            self.assertIn("`%s::" % cfg["sources"][0], secs[8])
            self.assertEqual(pm_blocks(text)["pm.node.v2"]["pm_id"], cfg["hb_id"])
            self.assertRegex(cfg["hb_id"], r"^HB-[A-Z0-9]+-[0-9]{3}$")

    def test_mathlinks_only_sealed_or_ausente(self):
        for book, _cfg, _t in self.each():
            with open(os.path.join(HB, book, "mathlinks.jsonl"), encoding="utf-8") as f:
                for ln in f:
                    if ln.strip():
                        self.assertIn(json.loads(ln)["status"], ("SEALED", "AUSENTE"))


class GFloor(PerBook):
    def test_seven_fields_per_anchored_symbol(self):
        for _book, cfg, text in self.each():
            s4 = section_texts(text)[4]
            sids = []
            for path in cfg["sources"]:
                sids += ["%s::%s" % (path, q) for q in anchored_symbols(path)]
            self.assertEqual(len(sids), cfg["anchors"], "anchor count drifted")
            for sid in sids + cfg["extra_p3"]:
                m = re.search(r"# PM-P3-HEADER: %s\n((?:# [A-Z]+: .*\n)+)" % re.escape(sid), s4)
                self.assertIsNotNone(m, sid)
                self.assertEqual(tuple(re.findall(r"^# ([A-Z]+):", m.group(1), re.M)), P3_FIELDS, sid)
                for ln in m.group(1).splitlines():
                    self.assertGreater(len(ln.split(":", 1)[1].strip()), 0, "%s empty field" % sid)

    def test_hb_s5_module_inv_and_au(self):
        for _book, cfg, text in self.each():
            blocks = {b["module"]: b for b in hb_blocks(text, "hb.inv.v2")}
            s5 = section_texts(text)[5]
            self.assertEqual(sorted(blocks), sorted(cfg["modules"].values()))
            for key, path in cfg["modules"].items():
                self.assertIn('<a id="hb-s5.%s"></a>' % key, s5)
                b = blocks[path]
                self.assertGreaterEqual(len(b["inv"]), 2, path)
                self.assertLessEqual(len(b["inv"]), 6, path)
                self.assertGreaterEqual(len(b["au"]), 1, path)
                self.assertLessEqual(len(b["au"]), 4, path)
            ids = {i["id"] for b in blocks.values() for i in b["inv"]}
            for need in cfg["need_inv"]:
                self.assertIn(need, ids)

    def test_every_inv_has_an_existing_test(self):
        for _book, cfg, text in self.each():
            names = test_names(cfg["tests"])
            tri = pm_blocks(text)["pm.triptych.v2"]
            cmap = tri["panel3"]["coverage_map"]
            inv_ids = {i["id"] for b in hb_blocks(text, "hb.inv.v2") for i in b["inv"]}
            self.assertEqual(set(cmap), inv_ids)
            for inv, t in cmap.items():
                self.assertIn(t, names, inv)
            for ref in re.findall(r"\[MEASURED, tests\.\w+\.(test_\w+)\]", text):
                self.assertIn(ref, names)
            for o in pm_blocks(text).values():
                for ev in re.findall(r'"test": ?"(test_\w+)"', json.dumps(o)):
                    self.assertIn(ev, names)

    def test_search_block_nonempty(self):
        for _book, _cfg, text in self.each():
            s = pm_blocks(text)["pm.search.v1"]
            for k in ("questions_answered", "engineering_tasks", "synonyms", "exclusions", "keywords", "retrieval_triggers"):
                self.assertTrue(s[k], k)

    def test_blocked_use_content(self):
        for _book, cfg, text in self.each():
            lim = pm_blocks(text)["pm.limits.v2"]
            self.assertTrue(all(e["blocked_use"].strip() for e in lim["entries"]))
            joined = (" ".join(e["blocked_use"] for e in lim["entries"]) + " " + text).lower()
            for needle in cfg["blocked"]:
                self.assertIn(needle, joined)

    def test_zero_untagged_unit_numbers(self):
        import hb_claim
        for _book, _cfg, text in self.each():
            self.assertEqual(hb_claim.untagged_numbers(re.sub(r"```.*?```", " ", text, flags=re.S)), [])

    def test_untagged_number_checker_is_not_vacuous(self):
        import hb_claim
        self.assertEqual(hb_claim.untagged_numbers("pack takes 9.5 ms here"), ["9.5 ms"])

    def test_claims_agree_with_receipts(self):
        import hb_claim
        for _book, _cfg, text in self.each():
            claims = hb_blocks(text, "hb.claim.v2")
            self.assertGreaterEqual(len(claims), 3)
            for c in claims:
                self.assertTrue(c["receipts"], c["id"])
            self.assertEqual(hb_claim.g_claim_book(text, ROOT, [c for c in claims if "value" in c]), [])

    def test_no_forbidden_or_fabricated_content(self):
        for book, _cfg, text in self.each():
            for bad in FORBIDDEN:
                self.assertFalse(bad in text, "%s: %s" % (book, bad))

    def test_exit_codes_come_from_errors_py(self):
        from visioncore import errors
        for _book, _cfg, text in self.each():
            used = set(re.findall(r"`(E_[A-Z0-9_]+)` \| (\d) \|", text))
            self.assertTrue(used)
            for code, rc in used:
                self.assertEqual(errors.CODES[code], int(rc), code)
            self.assertTrue(all(int(rc) != 1 for _c, rc in used))
            self.assertIn("rc 1 is never used", text)


class CodeAgreement(unittest.TestCase):
    """The tagged constants of pm.quantities are the constants of the code (the pre-migration books carried 35,000 and 80,000)."""

    def quantities(self, book):
        return {e["quantity"]: e for e in pm_blocks(read(book))["pm.quantities.v2"]["entries"]}

    def test_s6_constants(self):
        from visioncore.params import PARAMS
        q = self.quantities("HB-VC-s6-pack-cite")
        for name in ("PACK_GOP_B", "PACK_BATCH_B", "PACK_CALL_TOK", "PACK_RUN_TOK", "PACK_RETRY_TOK", "Z1_MAX_B"):
            self.assertEqual(int(q[name]["value_latex"]), PARAMS[name], name)
        cite = importlib.import_module("visioncore.cite")
        self.assertEqual(int(q["POINTERS_MAX"]["value_latex"]), cite.POINTERS_MAX)
        pack = importlib.import_module("visioncore.pack")
        self.assertEqual(pack._TOK_HARD, PARAMS["PACK_CALL_TOK"])

    def test_s7_constants(self):
        q = self.quantities("HB-VC-s7-vmem")
        mods = {m: importlib.import_module("vmem." + m) for m in ("receipts", "units", "gates", "tiers", "router", "gen", "compact")}
        want = {"VM_JSEG_CAP": mods["receipts"].VM_JSEG_CAP, "VM_JREC_MAX": mods["receipts"].VM_JREC_MAX, "UNIT_MAX_B": mods["units"].UNIT_MAX_B,
                "DIGEST_MAX_B": mods["tiers"].DIGEST_MAX_B, "SHORTLIST_B": mods["router"].SHORTLIST_B, "ANSWER_SOFT_TOK": mods["router"].ANSWER_SOFT_TOK,
                "ANSWER_HARD_TOK": mods["router"].ANSWER_HARD_TOK, "VM_GEN_KEEP": mods["gen"].VM_GEN_KEEP, "GEN_MAX": mods["gen"].GEN_MAX,
                "SIG_HAM_MAX": mods["router"].SIG_HAM_MAX}
        for name, v in want.items():
            self.assertEqual(int(q[name]["value_latex"]), v, name)
        # the duplicated definitions the book warns about must agree
        self.assertEqual(mods["gen"].VM_GEN_KEEP, mods["compact"].VM_GEN_KEEP)
        self.assertEqual(mods["units"].UNIT_MAX_B, mods["gates"].UNIT_MAX_B)
        self.assertEqual(mods["tiers"].DIGEST_MAX_B, mods["gates"].DIGEST_MAX_B)

    def test_s7_abstention_letters_and_gates(self):
        router, gates = importlib.import_module("vmem.router"), importlib.import_module("vmem.gates")
        text = read("HB-VC-s7-vmem")
        for letter, name in router.ABSTAIN.items():
            self.assertIn("%s %s" % (letter, name), text)
        for g in gates.GATES:
            self.assertIn(g, text)
        self.assertEqual(len(gates.GATES), 13)

    def test_receipts_recorded_clean(self):
        for book, cfg in BOOKS.items():
            for rid in cfg["receipts"]:
                path = os.path.join(HB, book, "receipts", rid + ".json")
                with open(path, encoding="utf-8") as f:
                    rc = json.load(f)
                self.assertEqual(rc["exit_code"], 0, rid)
                self.assertIsNotNone(rc["commit"], rid)
                self.assertTrue(rc["tree_clean"], rid)
                self.assertEqual(admission.check_receipt(path, ROOT), [], rid)
            with open(os.path.join(HB, book, "receipts", cfg["unittest"] + ".json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["metrics"]["tests_run"], cfg["tests_run"])


def _decl():
    with open(os.path.join(HB, "COMPANION_FORMAT.json"), encoding="utf-8") as f:
        return json.load(f)


class TempCorpus(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.hb = os.path.join(self.d, "hyperbooks")
        os.makedirs(self.hb)

    def corpus(self, book, profiled):
        live = _decl()
        shutil.copytree(os.path.join(HB, book), os.path.join(self.hb, book), ignore=shutil.ignore_patterns("__pycache__"))
        d = {k: v for k, v in live.items() if k not in ("profiles", "expected_books", "receipt_inventory")}
        d["expected_books"] = [book]
        d["receipt_inventory"] = {book: live["receipt_inventory"][book]}
        d["profiles"] = {book: "hb-hybrid/v1"} if profiled else {}
        with open(os.path.join(self.hb, "COMPANION_FORMAT.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)

    def check(self):
        return admission.check_corpus(self.hb, ROOT)


class LockstepAdmission(TempCorpus):
    def test_profiled_books_accepted(self):
        for book in BOOKS:
            self.setUp()
            self.corpus(book, True)
            self.assertEqual(self.check(), [], book)

    def test_profiled_book_rejected_without_symbols_jsonl(self):
        for book in BOOKS:
            self.setUp()
            self.corpus(book, True)
            os.remove(os.path.join(self.hb, book, "symbols.jsonl"))
            self.assertTrue(any("requires symbols.jsonl" in x for x in self.check()), book)

    def test_profiled_book_rejected_without_index_or_fence(self):
        for book in BOOKS:
            self.setUp()
            self.corpus(book, True)
            shutil.rmtree(os.path.join(self.hb, book, "index"))
            self.assertTrue(any("index/ID" in x for x in self.check()), book)
            p = os.path.join(self.hb, book, "book.md")
            with open(p, encoding="utf-8") as f:
                t = f.read()
            with open(p, "w", encoding="utf-8") as f:
                f.write(t.replace("<!-- HB:GEN END hb-s9 -->", ""))
            self.assertTrue(any("fence hb-s9" in x for x in self.check()), book)

    def test_unprofiled_declaration_of_a_hybrid_book_is_rejected(self):
        for book in BOOKS:
            self.setUp()
            self.corpus(book, False)
            self.assertTrue(any("no profile" in x for x in self.check()), book)

    def test_bad_profile_id_rejected(self):
        for book in BOOKS:
            self.setUp()
            self.corpus(book, True)
            p = os.path.join(self.hb, "COMPANION_FORMAT.json")
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            d["profiles"][book] = "hb-hybrid/v9"
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f)
            self.assertTrue(any("profile of" in x for x in self.check()), book)


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

    def _run(self, root, cfg):
        env = dict(os.environ, HBV2_ROOT=root, HBV2_BOOK_SUB=".", HBV2_SUBJECT=".", PYTHONDONTWRITEBYTECODE="1", HBV2_SRC_DIRS=cfg["src_dirs"])
        for argv in (["hb_extract2.py", "--out", os.path.join(root, "out")], ["hb_trace.py", "--out", os.path.join(root, "out", "trace.json")],
                     ["hb_build.py", "--render-derived"], ["hb_index.py"]):
            r = subprocess.run([sys.executable, os.path.join(ROOT, "hbtools_v2", argv[0])] + argv[1:], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=900)
            self.assertEqual(r.returncode, 0, argv[0] + r.stderr[-400:] + r.stdout[-400:])

    def test_rebuild_twice_is_byte_identical_to_committed(self):
        for book, cfg in BOOKS.items():
            with self.subTest(book=book):
                d = tempfile.mkdtemp(prefix="s67rb")
                self.addCleanup(shutil.rmtree, d, True)
                root = os.path.join(d, book)
                os.makedirs(os.path.join(root, "out"))
                shutil.copy(os.path.join(HB, book, "book.md"), root)
                self._run(root, cfg)
                first = self._read(root)
                self._run(root, cfg)
                self.assertEqual(first, self._read(root))
                for f in self.FILES:
                    with open(os.path.join(HB, book, f), "rb") as fh:
                        self.assertEqual(first[f], fh.read(), "committed %s/%s is stale" % (book, f))


if __name__ == "__main__":
    unittest.main()
