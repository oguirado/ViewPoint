"""HBM1-B lane M2: HB-VC-s2-change and HB-VC-s3-pyramid in the hb-hybrid/v1 profile (HB_DECISIONS 2.1-2.3, G-FLOOR).

(a) anchors, GOLD blocks and machine blocks; (b) G-FLOOR (7-field header per public symbol, INV/AU, search, blocked_use, tags);
(c) behaviour witnesses cited by the books (threshold table, quantities, words_eq, candidate order, S2-before-xh64, id_level_cap);
(d) lockstep admission accepts the profiled books and rejects a missing companion; (e) the generated companions rebuild byte-identically."""
import ast
import glob
import json
import os
import re
import shutil
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
from visioncore import change as C  # noqa: E402
from visioncore import params as P  # noqa: E402
from visioncore import pyramid as PY  # noqa: E402

HB = os.path.join(ROOT, "hyperbooks")
PM_BLOCK = re.compile(r"<!-- PM:BEGIN (\S+) -->\n```json\n(.*?)\n```\n<!-- PM:END \1 -->", re.S)
HB_BLOCK = re.compile(r"<!-- HB:BEGIN (hb\.[a-z_]+\.v\d+) -->\n```json\n(.*?)\n```\n<!-- HB:END -->", re.S)
SECTION_PM = {0: ["pm.node.v2", "pm.reading.v2"], 1: ["pm.claims.v2"], 2: ["pm.objects.v2", "pm.quantities.v2"], 3: ["pm.roadmap.v2"],
              4: ["pm.triptych.v2"], 5: ["pm.domains.v2"], 6: ["pm.limits.v2", "pm.search.v1"], 9: ["pm.edges.v2"]}
P3_FIELDS = ("INPUTS", "PRECONDITIONS", "OUTPUTS", "TIME", "SPACE", "ARITHMETIC", "FAILURE")
ALLOWED_EX = {"EX-VG02", "EX-W01", "EX-VG-0015-2", "EX-VG-0015-3", "EX-VG-0034-2", "EX-SW-0267-1"}   # excerpt ids present in CTX_G2 / CTX_G3
BOOKS = {
    "HB-VC-s2-change": dict(hb_id="HB-VCS2-001", module="visioncore/change.py", key="change", src="visioncore/change.py,tests/test_change.py,tests/test_change_fix.py",
                            inv_min={"INV-S2-EXACT", "INV-S2-BAND", "INV-S2-COV", "INV-S2-ORDER", "INV-S2-CLS-BEFORE-XH", "INV-S2-PURE"},
                            receipts=("R-s2-change-unittest",), gen_sym="`visioncore/change.py::classify_tiles`"),
    "HB-VC-s3-pyramid": dict(hb_id="HB-VCS3-001", module="visioncore/pyramid.py", key="pyramid", src="visioncore/pyramid.py,tests/test_pyramid.py",
                             inv_min={"INV-S3-MAX-EXACT", "INV-S3-COVER", "INV-S3-BUDGET", "INV-S3-ZERO-SKIP", "INV-S3-FULL-EARLY", "INV-S3-AABB"},
                             receipts=("R-s3-pyramid-unittest", "R-s3-pyramid-select"), gen_sym="`visioncore/pyramid.py::Pyramid.select`"),
}
for _b, _c in BOOKS.items():
    _c["dir"] = os.path.join(HB, _b)
    with open(os.path.join(_c["dir"], "book.md"), encoding="utf-8") as _f:
        _c["text"] = _f.read()


def section_texts(text):
    marks = [(int(m.group(1)), m.start()) for m in re.finditer(r'^## <a id="hb-s(\d+)"></a>', text, re.M)]
    return {n: text[st:marks[i + 1][1] if i + 1 < len(marks) else len(text)] for i, (n, st) in enumerate(marks)}


def hb_blocks(text, schema):
    return [json.loads(m.group(2)) for m in HB_BLOCK.finditer(text) if m.group(1) == schema]


def pm_block(text, name):
    return next(json.loads(m.group(2)) for m in PM_BLOCK.finditer(text) if m.group(1) == name)


def public_symbols(module):
    """Public top-level functions/classes and public methods of a module (the symbols G-FLOOR demands a header for)."""
    with open(os.path.join(ROOT, module), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and not n.name.startswith("_"):
            out.append(n.name)
            if isinstance(n, ast.ClassDef):
                out += ["%s.%s" % (n.name, m.name) for m in n.body if isinstance(m, ast.FunctionDef) and not m.name.startswith("_")]
    return out


def per_book(fn):
    """Run a (self, book, cfg) check for every book of the lane, one subTest each."""
    def wrapper(self):
        for b, c in BOOKS.items():
            with self.subTest(book=b):
                fn(self, b, c)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class SectionsAndBlocks(unittest.TestCase):
    @per_book
    def test_anchors_hb_s0_to_s10_present_in_order(self, b, c):
        pos = []
        for n in range(11):
            m = re.search(r'<a id="hb-s%d"></a><a id="pm-s%d"></a>' % (n, n), c["text"])
            self.assertIsNotNone(m, "hb-s%d anchor missing" % n)
            pos.append(m.start())
        self.assertEqual(pos, sorted(pos))
        self.assertEqual(len(section_texts(c["text"])), 11)

    @per_book
    def test_gold_blocks_in_their_sections(self, b, c):
        secs = section_texts(c["text"])
        for n, names in SECTION_PM.items():
            got = [m.group(1) for m in PM_BLOCK.finditer(secs[n])]
            for name in names:
                self.assertIn(name, got, "hb-s%d lacks %s" % (n, name))
        self.assertEqual([m.group(1) for m in PM_BLOCK.finditer(secs[0])], SECTION_PM[0])

    @per_book
    def test_every_pm_block_parses_with_its_schema_and_id(self, b, c):
        begins = re.findall(r"<!-- PM:BEGIN (\S+) -->", c["text"])
        self.assertEqual(len(begins), 11)
        parsed = {m.group(1): json.loads(m.group(2)) for m in PM_BLOCK.finditer(c["text"])}
        self.assertEqual(sorted(begins), sorted(parsed))
        for name, o in parsed.items():
            self.assertEqual(o["schema"], name)
            if name != "pm.search.v1" or "pm_id" in o:
                self.assertEqual(o["pm_id"], c["hb_id"])

    @per_book
    def test_machine_blocks_parse_and_match_schemas(self, b, c):
        import schemalib
        sdir = os.path.join(ROOT, "standards", "schemas")
        n = 0
        for m in HB_BLOCK.finditer(c["text"]):
            o = json.loads(m.group(2))
            self.assertEqual(schemalib.validate(o, m.group(1), schema_dir=sdir), [], m.group(1))
            if m.group(1) == "hb.section.v2":
                self.assertEqual(o["hb_id"], c["hb_id"])
            n += 1
        self.assertGreaterEqual(n, 12)

    @per_book
    def test_generated_sections_only_in_fences(self, b, c):
        secs = section_texts(c["text"])
        for n in (8, 9, 10):
            self.assertIn("<!-- HB:GEN BEGIN hb-s%d -->" % n, secs[n])
            self.assertIn("<!-- HB:GEN END hb-s%d -->" % n, secs[n])
        self.assertIn(c["gen_sym"], secs[8])

    @per_book
    def test_mathlinks_only_sealed_or_ausente_and_verified(self, b, c):
        with open(os.path.join(c["dir"], "mathlinks.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
        self.assertTrue(rows)
        for r in rows:
            self.assertIn(r["status"], ("SEALED", "AUSENTE"))
        self.assertEqual(admission.check_mathlinks(HB, ROOT), [])

    @per_book
    def test_slots_file_names_the_subject_and_env(self, b, c):
        with open(os.path.join(c["dir"], "seed", "slots.json"), encoding="utf-8") as f:
            s = json.load(f)
        self.assertEqual((s["book"], s["book_id"], s["profile"]), (b, c["hb_id"], "hb-hybrid/v1"))
        self.assertEqual(s["env"]["HBV2_SRC_DIRS"], c["src"])
        self.assertEqual(s["env"]["HBV2_ROOT"], "hyperbooks/" + b)
        self.assertEqual(sorted(s["sections"]), sorted("hb-s%d" % n for n in range(11)))


class GFloor(unittest.TestCase):
    @per_book
    def test_seven_fields_per_public_symbol(self, b, c):
        s4 = section_texts(c["text"])[4]
        syms = public_symbols(c["module"])
        self.assertGreaterEqual(len(syms), 11)
        for sym in syms:
            m = re.search(r"# PM-P3-HEADER: %s::%s\n((?:# [A-Z]+: .*\n)+)" % (re.escape(c["module"]), re.escape(sym)), s4)
            self.assertIsNotNone(m, sym)
            keys = re.findall(r"^# ([A-Z]+):", m.group(1), re.M)
            self.assertEqual(tuple(keys), P3_FIELDS, sym)
            for ln in m.group(1).splitlines():
                self.assertGreater(len(ln.split(":", 1)[1].strip()), 0, "%s empty field" % sym)

    @per_book
    def test_hb_s5_inv_and_au(self, b, c):
        blocks = [x for x in hb_blocks(c["text"], "hb.inv.v2") if x["module"] == c["module"]]
        self.assertEqual(len(blocks), 1)
        x = blocks[0]
        self.assertTrue(2 <= len(x["inv"]) <= 6)
        self.assertTrue(1 <= len(x["au"]) <= 4)
        self.assertTrue(c["inv_min"] <= {i["id"] for i in x["inv"]})
        for e in x["inv"] + x["au"]:
            self.assertLessEqual(len(e["text"].encode()), 600)
        self.assertIn('<a id="hb-s5.%s"></a>' % c["key"], section_texts(c["text"])[5])

    @per_book
    def test_search_block_nonempty(self, b, c):
        s = pm_block(c["text"], "pm.search.v1")
        for k in ("questions_answered", "engineering_tasks", "synonyms", "exclusions", "keywords", "retrieval_triggers"):
            self.assertTrue(s[k], k)

    @per_book
    def test_blocked_use_paired_with_a_test_or_target(self, b, c):
        alltests = ""
        for fp in glob.glob(os.path.join(ROOT, "tests", "*.py")):
            with open(fp, encoding="utf-8") as f:
                alltests += f.read()
        lim = pm_block(c["text"], "pm.limits.v2")
        self.assertGreaterEqual(len(lim["entries"]), 6)
        for e in lim["entries"]:
            self.assertTrue(e["blocked_use"].strip(), e["id"])
            if not e["support"].startswith("[TARGET]"):
                self.assertIn("def %s(" % e["support"], alltests, e["id"])

    @per_book
    def test_zero_untagged_unit_numbers(self, b, c):
        import hb_claim
        self.assertEqual(hb_claim.untagged_numbers(re.sub(r"```.*?```", " ", c["text"], flags=re.S)), [])

    def test_untagged_number_checker_is_not_vacuous(self):
        import hb_claim
        self.assertEqual(hb_claim.untagged_numbers("classify takes 3.5 ms here"), ["3.5 ms"])

    @per_book
    def test_claims_agree_with_receipts(self, b, c):
        import hb_claim
        claims = hb_blocks(c["text"], "hb.claim.v2")
        self.assertGreaterEqual(len(claims), 3)
        for cl in claims:
            if cl["etag"] == "MEASURED":
                self.assertTrue(cl["receipts"], cl["id"])
        self.assertEqual(hb_claim.g_claim_book(c["text"], ROOT, [x for x in claims if "value" in x]), [])

    @per_book
    def test_no_forbidden_or_unknown_citations(self, b, c):
        for bad in ("ADV_RT1", "PLAN_mathcodec", "EX-MA02", "EX-MC01", "EX-OC03", "EX-OC05"):
            self.assertNotIn(bad, c["text"])
        cited = set(re.findall(r"\bEX-[A-Za-z0-9-]+", c["text"]))
        self.assertLessEqual(cited, ALLOWED_EX, sorted(cited - ALLOWED_EX))

    @per_book
    def test_exit_codes_come_from_errors_py(self, b, c):
        from visioncore import errors
        used = set(re.findall(r"`(E_[A-Z0-9_]+)` \| (\d) \|", c["text"]))
        self.assertTrue(used)
        for code, rc in used:
            self.assertEqual(errors.CODES[code], int(rc), code)
        self.assertTrue(all(int(rc) != 1 for _c, rc in used))

    @per_book
    def test_receipts_present_ok_and_consistent(self, b, c):
        for r in c["receipts"]:
            with open(os.path.join(c["dir"], "receipts", r + ".json"), encoding="utf-8") as f:
                rc = json.load(f)
            self.assertEqual(rc["exit_code"], 0, r)
            self.assertEqual(rc["id"], r)
            if rc["commit"] is not None:
                self.assertIs(rc["tree_clean"], True, r)
        self.assertEqual(admission.check_receipts(HB, ROOT), [])


class S2Witnesses(unittest.TestCase):
    """Behaviour and source facts the S2 book cites."""

    def test_s2_quantities_match_code(self):
        q = {e["id"]: e["value_latex"] for e in pm_block(BOOKS["HB-VC-s2-change"]["text"], "pm.quantities.v2")["entries"]}
        te, tx, du = C.thresholds()
        code = {"Q-S2-TENTER": te, "Q-S2-TEXIT": tx, "Q-S2-DRIFTU": du, "Q-S2-WCHROMA": C.W_CHROMA, "Q-S2-WHAM": C.W_HAM, "Q-S2-HDEAD": P.PARAMS["H_DEAD"],
                "Q-S2-SIGDEAD": P.PARAMS["SIG_DEAD_Q4"], "Q-S2-FROZENM": P.PARAMS["FROZEN_M"], "Q-S2-FROZENPRE": C.FROZEN_PRE_ACT,
                "Q-S2-FROZENCOV": P.PARAMS["FROZEN_COV_PPM"], "Q-S2-DRIFTSTALL": P.PARAMS["DRIFT_STALL"], "Q-S2-RATIO": P.PARAMS["MV_SAD_RATIO_DEN"],
                "Q-S2-SHARE": P.PARAMS["GLOBAL_SHARE_PPM"], "Q-S2-DMAX": C.MOTION_D_MAX, "Q-S2-ISLAND": C.ISLAND_MAX, "Q-S2-SHADEDEN": C.SHADE_DEN,
                "Q-S2-SHADEEDGE": C.SHADE_EDGE_MAX}
        for k, v in code.items():
            self.assertEqual(q[k], str(v), k)

    def test_words_eq_tiles_are_never_active_classes(self):
        """INV-S2-EXACT: where words_eq is set the tile is never JITTER/CHANGED/MOVED/FLICKER (only UNCHANGED, FROZEN or DRIFT)."""
        rng = np.random.RandomState(20261018)
        n = 96
        t = np.zeros(n, C.TILE_DT)
        t["morton"] = np.arange(n)
        st = C.ClassState.init(t)
        saw_eq = saw_class = False
        for _f in range(10):
            t = t.copy()
            t["y_q4"] = np.clip(t["y_q4"].astype(np.int64) + rng.randint(-90, 90, n), 0, 4080)
            t["sig_q4"] = rng.randint(0, 200, n)
            t["sig64"] = rng.randint(0, 2 ** 62, n, dtype=np.int64).astype(np.uint64)
            eq = rng.rand(n) < 0.4
            out, st, _s = C.classify_tiles(st, t, eq)
            cls = out["cls"]
            if eq.any():
                saw_eq = True
                self.assertTrue(np.isin(cls[eq], (C.C_UNCHANGED, C.C_FROZEN, C.C_DRIFT)).all())
            if np.isin(cls[~eq], (C.C_CHANGED, C.C_JITTER)).any():
                saw_class = True
        self.assertTrue(saw_eq and saw_class)   # the property is not vacuous: unequal tiles do reach the active classes

    def test_candidate_order_and_strict_tie_in_source(self):
        """INV-S2-ORDER (source binding): global first, then island vectors in sorted order for their own islands, then the pool; strict '<'."""
        with open(os.path.join(ROOT, "visioncore", "change.py"), encoding="utf-8") as f:
            src = f.read()
        i_glob = src.index("expl_g = offer(gv, cand_base, True)")
        i_own = src.index("for v in sorted(by_vec):")
        i_pool = src.index("for v in sorted(by_vec):", i_own + 1)
        self.assertLess(i_glob, i_own)
        self.assertLess(i_own, i_pool)
        self.assertEqual(src.count("for v in sorted(by_vec):"), 2)
        self.assertIn("& (sad_c < best)", src)
        self.assertNotIn("sad_c <= best", src)
        self.assertIn("[:ISLAND_MAX]", src)

    def test_pipeline_classifies_before_filling_xh64(self):
        """INV-S2-CLS-BEFORE-XH: cls is committed by S2 before desc.fill_xh64 reads it."""
        with open(os.path.join(ROOT, "visioncore", "pipeline.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertLess(src.index("change.classify_tiles("), src.index("desc.fill_xh64(out"))
        with open(os.path.join(ROOT, "visioncore", "desc.py"), encoding="utf-8") as f:
            self.assertIn("cls >= layout.CLS_DIRTY_MIN", f.read())

    def test_threshold_table_rows_are_in_hb_s2(self):
        s2 = section_texts(BOOKS["HB-VC-s2-change"]["text"])[2]
        for name in ("T0_Q4", "HYST_BAND_PERMILLE", "H_DEAD", "SIG_DEAD_Q4", "DRIFT_U_Q4", "FROZEN_PRE_ACT", "FROZEN_COV_PPM", "ISLAND_MAX"):
            self.assertRegex(s2, r"(?m)^\| `%s` \| \d+" % name)
        for a in ("vc.s2.drift", "vc.s2.residual", "vc.s2.sig_conf", "vc.s2.motion", "vc.s2.island_vector", "vc.s2.classify", "vc.s2.classify_first"):
            self.assertIn("`%s`" % a, BOOKS["HB-VC-s2-change"]["text"])


class S3Witnesses(unittest.TestCase):
    def test_id_level_cap_equals_depth(self):
        for L in range(1, 9):
            self.assertEqual(PY.id_level_cap(L), L)

    def test_documented_depths_and_rsel(self):
        self.assertEqual([PY.levels_for(*g) for g in ((80, 45), (60, 34), (64, 64), (1, 1))], [7, 6, 6, 1])
        self.assertEqual(PY.r_sel(), 7)
        self.assertEqual(PY.r_sel(), (P.PARAMS["T0_Q4"] * (1000 - P.PARAMS["HYST_BAND_PERMILLE"]) // 1000) >> 2)

    def test_quantities_match_receipts(self):
        c = BOOKS["HB-VC-s3-pyramid"]
        q = {e["id"]: e["value_latex"] for e in pm_block(c["text"], "pm.quantities.v2")["entries"]}
        with open(os.path.join(c["dir"], "receipts", "R-s3-pyramid-select.json"), encoding="utf-8") as f:
            m = json.load(f)["metrics"]
        for k, key in (("Q-S3-CASES", "cases"), ("Q-S3-VIOL", "violations"), ("Q-S3-EXCESS", "max_excess_over_B"), ("Q-S3-VISITS", "max_visits"), ("Q-S3-BUILD", "build_ms_720p_dense")):
            self.assertEqual(q[k], str(m[key]), k)
        self.assertEqual(q["Q-S3-RSEL"], str(PY.r_sel()))

    def test_out_of_range_morton_is_not_a_vqaerror_today(self):
        """LIM-S3-5 is documented as a limit, not a contract: a Morton code outside 4**L fails with IndexError."""
        from visioncore.grid import Grid
        g = Grid(1280, 720)
        t = np.zeros(1, dtype=[("morton", "<u2"), ("cls", "u1"), ("resid", "u1")])
        t["morton"] = 4 ** PY.levels_for(g.gw, g.gh)
        with self.assertRaises(IndexError):
            PY.Pyramid(t, g)


def _decl():
    with open(os.path.join(HB, "COMPANION_FORMAT.json"), encoding="utf-8") as f:
        return json.load(f)


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

    def test_live_declaration_is_absent_or_correct(self):
        """E1 owns COMPANION_FORMAT.json; until it lands the entries are absent, afterwards they must be exactly hb-hybrid/v1."""
        for b in BOOKS:
            self.assertIn(_decl().get("profiles", {}).get(b), (None, "hb-hybrid/v1"))
            self.assertIn(b, _decl()["expected_books"])
            self.assertEqual(sorted(_decl()["receipt_inventory"][b]), sorted(r + ".json" for r in BOOKS[b]["receipts"]))

    def test_profiled_books_accepted(self):
        self.corpus(list(BOOKS), list(BOOKS))
        self.assertEqual(admission.check_corpus(self.hb, ROOT), [])

    def test_profiled_book_rejected_without_a_companion(self):
        for b in BOOKS:
            self.setUp()
            self.corpus([b], [b])
            os.remove(os.path.join(self.hb, b, "symbols.jsonl"))
            self.assertTrue(any("requires symbols.jsonl" in x for x in admission.check_corpus(self.hb, ROOT)), b)

    def test_profiled_book_rejected_without_fence(self):
        for b in BOOKS:
            self.setUp()
            self.corpus([b], [b])
            p = os.path.join(self.hb, b, "book.md")
            with open(p, encoding="utf-8") as f:
                t = f.read()
            with open(p, "w", encoding="utf-8") as f:
                f.write(t.replace("<!-- HB:GEN END hb-s9 -->", ""))
            self.assertTrue(any("fence hb-s9" in x for x in admission.check_corpus(self.hb, ROOT)), b)

    def test_unprofiled_declaration_of_same_book_is_rejected(self):
        for b in BOOKS:
            self.setUp()
            self.corpus([b], [])
            self.assertTrue(any("no profile" in x for x in admission.check_corpus(self.hb, ROOT)), b)


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

    def _run(self, root, src):
        env = dict(os.environ, HBV2_ROOT=root, HBV2_BOOK_SUB=".", HBV2_SUBJECT=".", PYTHONDONTWRITEBYTECODE="1", HBV2_SRC_DIRS=src)
        for argv in (["hb_extract2.py", "--out", os.path.join(root, "out")], ["hb_trace.py", "--out", os.path.join(root, "out", "trace.json")],
                     ["hb_build.py", "--render-derived"], ["hb_index.py"]):
            r = subprocess.run([sys.executable, os.path.join(ROOT, "hbtools_v2", argv[0])] + argv[1:], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=900)
            self.assertEqual(r.returncode, 0, argv[0] + r.stderr[-400:] + r.stdout[-400:])

    def test_rebuild_twice_is_byte_identical_to_committed(self):
        for b, c in BOOKS.items():
            with self.subTest(book=b):
                d = tempfile.mkdtemp(prefix="m2rb")
                self.addCleanup(shutil.rmtree, d, True)
                root = os.path.join(d, b)
                os.makedirs(os.path.join(root, "out"))
                shutil.copy(os.path.join(c["dir"], "book.md"), root)
                self._run(root, c["src"])
                first = self._read(root)
                self._run(root, c["src"])
                second = self._read(root)
                self.assertEqual(first, second)
                for f in self.FILES:
                    with open(os.path.join(c["dir"], f), "rb") as fh:
                        self.assertEqual(first[f], fh.read(), "committed %s/%s is stale" % (b, f))


if __name__ == "__main__":
    unittest.main()
