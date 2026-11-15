"""Lane T gate tests: fixture book passes, every drift mutant is rc 5, no rc 1, no hidden skip. Synthetic fixture only."""
import json, os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OSDIR = os.path.dirname(HERE)
REPO = os.path.abspath(os.path.join(OSDIR, "..", "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, OSDIR)
sys.dont_write_bytecode = True
import fixture as F  # noqa: E402
import admission_os  # noqa: E402


def sha_file(p):
    import hashlib
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.f = F.Fixture()
        self.addCleanup(self.f.cleanup)
        self.f.build()

    def gate_names(self, failing=True):
        d = json.load(open(os.path.join(self.f.tmp, "gates.json")))
        return {g["gate"] for g in d["gates"] if (not g["pass"]) == failing}

    def regen_manifest(self):
        p = os.path.join(self.f.root, F.BOOK, "manifest.json")
        m = json.load(open(p))
        for k in m["files"]:
            m["files"][k] = sha_file(os.path.join(self.f.root, F.BOOK, k))
        json.dump(m, open(p, "w"))


class Baseline(Base):
    def test_fixture_passes_and_skips_are_mapped(self):
        rc, out = self.f.gates()
        self.assertEqual(rc, 0, out)
        d = json.load(open(os.path.join(self.f.tmp, "gates.json")))
        names = {g["gate"] for g in d["gates"]}
        self.assertGreaterEqual(len(d["skipped"]), 14)
        for s in d["skipped"]:
            self.assertTrue(s["reason"])
            self.assertIn(s["replacement"], names, s)
        for g in ("g_ids", "g_backlink", "g_budget", "g_subanchor", "g_determinism", "g_nofloat"):
            self.assertIn(g, names)
        self.assertEqual(d["metrics"]["skipped"], len(d["skipped"]))

    def test_book_manifest_hb_id_is_os(self):
        m = json.load(open(os.path.join(self.f.root, F.BOOK, "manifest.json")))
        self.assertEqual(m["hb_id"], "HB-OSCUBOOK-001")

    def test_goextract_deterministic(self):
        outs = []
        for i in (1, 2):
            d = os.path.join(self.f.tmp, "ex%d" % i)
            rc, o = self.f.drv("extract", "--out", d)
            self.assertEqual(rc, 0, o)
            outs.append(open(os.path.join(d, "bundle.json"), "rb").read())
        self.assertEqual(outs[0], outs[1])
        b = json.loads(outs[0])
        self.assertEqual(b["schema"], "hb.symbols.bundle/v1")
        self.assertEqual({a["lang"] for a in b["anchors"]}, {"go"})


class Mutants(Base):
    def assert_rc5(self, gate):
        rc, out = self.f.gates()
        self.assertEqual(rc, 5, out)
        self.assertIn(gate, self.gate_names(), out)

    def test_moved_line(self):
        self.f.write_go("// moved\n" + F.GO_SRC)
        self.assert_rc5("g_os_anchor")

    def test_edited_body_sha_ast(self):
        self.f.write_go(F.GO_SRC.replace("return a + b", "return b + a + 0"))
        self.assert_rc5("g_os_anchor")

    def test_code_only_edit_comment(self):
        self.f.write_go(F.GO_SRC.replace("Add sums two ints.", "Add sums two integers."))
        self.assert_rc5("g_os_drift")

    def test_book_only_edit(self):
        self.f.write_book(open(self.f.book_path()).read() + "\nunrelated prose edit\n")
        self.assert_rc5("g_os_drift")

    def test_book_only_edit_manifest_regenerated(self):
        t = open(self.f.book_path()).read().replace("demo.go:4-6 ok", "demo.go:4-60 ok")
        self.f.write_book(t)
        self.regen_manifest()
        self.assert_rc5("g_os_drift")

    def test_book_only_wrong_sha_ast_ref(self):
        t = open(self.f.book_path()).read().replace("demo.go:4-6 ok", "demo.go:4-6 sha_ast:deadbeefdeadbeef ok")
        self.f.write_book(t)
        self.regen_manifest()
        self.assert_rc5("g_os_drift")

    def test_both_together_passes_after_rebuild(self):
        self.f.write_go(F.GO_SRC.replace("return a + b", "return b + a + 0"))
        self.f.build()
        rc, out = self.f.gates()
        self.assertEqual(rc, 0, out)

    def test_missing_p3_field(self):
        t = open(self.f.book_path()).read().replace("# SPACE: x\n", "", 1)
        self.f.write_book(t)
        self.f.build()
        self.assert_rc5("g_os_floor")

    def test_measured_claim_without_receipt(self):
        claim = {"schema": "hb.claim.v2", "id": "C-DEMO", "kind": "behaviour", "statement": "Add adds.", "anchors": [F.SID_ADD], "etag": "MEASURED", "receipts": []}
        t = open(self.f.book_path()).read().replace("## <a id=\"hb-s1\"></a>", F.block("hb.claim.v2", claim) + "\n## <a id=\"hb-s1\"></a>", 1)
        self.f.write_book(t)
        self.f.build()
        self.assert_rc5("g_os_floor")

    def test_untagged_number_in_prose(self):
        self.f.write_book(open(self.f.book_path()).read() + "\nIt takes 9.5 ms here.\n")
        self.f.build()
        self.assert_rc5("g_os_floor")

    def test_untagged_number_inside_gen_block_is_exempt_but_prose_is_not(self):
        t = open(self.f.book_path()).read()
        gen = "<!-- HB:GEN BEGIN hb-s8 -->\n"
        self.assertEqual(t.count(gen), 1)
        self.f.write_book(t.replace(gen, gen + "A generated row with 9.5 ms in it.\n"))
        self.f.gates()
        self.assertNotIn("g_os_floor", self.gate_names(), "HB:GEN block must be exempt from the untagged-number scan")

    def test_public_symbol_without_section_fails_backlink(self):
        sp = os.path.join(self.f.root, F.BOOK, "symbols.jsonl")
        rows = [json.loads(l) for l in open(sp)]
        n = 0
        for r in rows:
            if r["id"] == F.SID_SUB:
                r["governs"]["sections"] = []
                n += 1
        self.assertEqual(n, 1)
        open(sp, "w").write("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows))
        rc, out = self.f.gates()
        self.assertEqual(rc, 5, out)
        self.assertIn("g_os_backlink", self.gate_names(), out)

    def test_vacuous_bench_fails(self):
        open(os.path.join(self.f.tmp, "bench", "hb_dev.jsonl"), "w").close()
        self.assert_rc5("g_bench_rows")

    def test_inv_to_missing_symbol(self):
        t = open(self.f.book_path()).read().replace('demo.go::Sub"],"text":"Sub is plain', 'demo.go::Gone"],"text":"Sub is plain')
        self.f.write_book(t)
        self.regen_manifest()
        rc, out = self.f.gates()
        self.assertEqual(rc, 5, out)


class NeverRc1(Base):
    def test_exit_codes(self):
        self.assertEqual(self.f.drv()[0], 2)
        self.assertEqual(self.f.drv("nope")[0], 2)
        env = dict(self.f.env, HBV2_SUBJECT=os.path.join(self.f.tmp, "missing"))
        r = subprocess.run([sys.executable, "-B", F.DRIVER, "extract"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 5)
        self.f.write_book("garbage")
        rc, out = self.f.drv("build")
        self.assertIn(rc, (0, 3, 5))
        self.assertNotEqual(rc, 1)

    def test_shell_entry_usage(self):
        r = subprocess.run(["bash", os.path.join(OSDIR, "hb_os_gates.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_no_literal_rc1_in_new_code(self):
        import re
        for f in ("hb_os_driver.py", "hb_os_checks.py", "admission_os.py", "hb_os_gates.sh", "goextract/main.go"):
            s = open(os.path.join(OSDIR, f)).read()
            self.assertIsNone(re.search(r"(sys\.exit\(1\)|return 1\b|exit 1\b|os\.Exit\(1\))", s), f)


class Admission(Base):
    def setUp(self):
        super().setUp()
        open(os.path.join(self.f.root, F.BOOK, "mathlinks.jsonl"), "w").write('{"id":"m","status":"AUSENTE"}\n')
        self.fmt = os.path.join(OSDIR, "OS_FORMAT.json")

    def test_admits_fixture_and_rejects_missing_companion(self):
        # the fixture book lacks index/ID-free companions? build+index wrote them all
        self.assertEqual(admission_os.run(self.fmt, self.f.root), [])
        os.remove(os.path.join(self.f.root, F.BOOK, "claims.jsonl"))
        self.assertTrue(any("claims.jsonl" in m for m in admission_os.run(self.fmt, self.f.root)))

    def test_rejects_forbidden_and_undeclared_receipt(self):
        open(os.path.join(self.f.root, F.BOOK, "triptych.json"), "w").write("{}")
        open(os.path.join(self.f.root, F.BOOK, "receipts", "R-x.json"), "w").write('{"id":"R-x"}')
        out = admission_os.run(self.fmt, self.f.root)
        self.assertTrue(any("triptych.json" in m for m in out))
        self.assertTrue(any("undeclared receipt" in m for m in out))

    def test_cli_rc(self):
        r = subprocess.run([sys.executable, "-B", os.path.join(OSDIR, "admission_os.py"), "--root", self.f.root], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(subprocess.run([sys.executable, "-B", os.path.join(OSDIR, "admission_os.py"), "--bogus", "x"], capture_output=True).returncode, 2)


class BookDrift(Base):
    """JW5A-01 / ADC-W5A-02(b): hb_os_gates.sh checks the stored (book_sha, code_sha) pair before any rebuild; no self-heal."""

    def setUp(self):
        super().setUp()
        open(os.path.join(self.f.root, F.BOOK, "mathlinks.jsonl"), "w").write('{"id":"m","status":"AUSENTE"}\n')

    def sh(self):
        env = dict(self.f.env, OS_FORMAT=os.path.join(OSDIR, "OS_FORMAT.json"))
        r = subprocess.run(["bash", os.path.join(OSDIR, "hb_os_gates.sh"), F.BOOK, os.path.join(self.f.tmp, "out")], env=env, capture_output=True, text=True, timeout=300)
        return r.returncode, r.stderr

    def test_control_unedited_passes(self):
        rc, err = self.sh()
        self.assertEqual(rc, 0, err[-600:])

    def test_prose_only_book_edit_fails(self):
        t = open(self.f.book_path()).read()
        self.assertIn("Prose reference", t)
        self.f.write_book(t.replace("Prose reference", "Prose mention"))
        rc, err = self.sh()
        self.assertEqual(rc, 5, err[-600:])
        self.assertIn("E_BOOK_DRIFT", err)

    def test_code_only_edit_does_not_self_heal(self):
        self.f.write_go(F.GO_SRC.replace("return a + b", "return b + a"))
        rc, err = self.sh()
        self.assertEqual(rc, 5, err[-600:])
        self.assertIn("E_BOOK_DRIFT", err)

    def test_missing_manifest_fails(self):  # RD5A-01: absent manifest.json must fail closed, not self-heal
        os.remove(os.path.join(self.f.root, F.BOOK, "manifest.json"))
        rc, err = self.sh()
        self.assertEqual(rc, 5, err[-600:])
        self.assertIn("E_BOOK_DRIFT", err)
        self.assertFalse(os.path.exists(os.path.join(self.f.root, F.BOOK, "manifest.json")), "gate regenerated the manifest")


class GoTooling(unittest.TestCase):
    """AW5-09/10: go vet over goextract + the cubook/mcp packages, go test of goextract, all -p 5 (never the whole tree)."""

    def go(self, *args):
        env = dict(os.environ, PATH=os.environ["PATH"] + ":/usr/local/go/bin")
        return subprocess.run(["go"] + list(args), cwd=REPO, env=env, capture_output=True, text=True, timeout=600)

    def test_vet(self):
        r = self.go("vet", "./hyperbooks/_tools/os/goextract/", "./src/cubook/...", "./cmd/srf-cu-mcp/...")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])

    def test_goextract_unit(self):
        r = self.go("test", "-p", "5", "-count=1", "-timeout", "120s", "./hyperbooks/_tools/os/goextract/")
        self.assertEqual(r.returncode, 0, r.stdout[-800:])


class Pins(unittest.TestCase):
    """W6 RP: hbtools_v2 is the vision-engine 681dc0b copy (language-neutral, --symbols-bundle); PROVENANCE records it and keeps the old pin as history."""
    TREE = "16acb463ee1b69103cf867414a1ec1100289bf77"
    ARCHIVE = "25cf23a293c9a35cc172deaaba22ff3aa59573d31e9b236bcc6325e9fe50c9ed"
    FORMAT_SHA = "c7c23e2899b782d118a52509bf8da43de4780fc25daa4bbdae0883fbf689d719"

    def tree_of(self, rel):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, GIT_INDEX_FILE=os.path.join(d, "idx"))
            subprocess.run(["git", "add", "-A", "--", rel], cwd=REPO, env=env, check=True, capture_output=True)
            return subprocess.run(["git", "write-tree", "--prefix=" + rel], cwd=REPO, env=env, capture_output=True, text=True, check=True).stdout.strip()

    def test_hbtools_tree_is_681dc0b(self):
        self.assertEqual(self.tree_of("hyperbooks/_tools/hbtools_v2"), self.TREE)

    def test_companion_format_untouched(self):
        self.assertEqual(sha_file(os.path.join(REPO, "hyperbooks", "COMPANION_FORMAT.json")), self.FORMAT_SHA)

    def test_provenance_records_new_pin_and_history(self):
        pv = json.load(open(os.path.join(REPO, "hyperbooks", "PROVENANCE.json")))
        self.assertEqual(pv["source_commit"], "681dc0b")
        it = [i for i in pv["items"] if i["path"] == "hyperbooks/_tools/hbtools_v2/"][0]
        self.assertEqual((it["git_tree"], it["git_archive_sha256"]), (self.TREE, self.ARCHIVE))
        self.assertEqual([h["source_commit"] for h in pv["history"]], ["e738181"])
        self.assertEqual(pv["history"][0]["git_tree"], "48bcac19a837b052864cb564f94cbbda9f96efbc")

    def test_no_go_behaviour_change(self):
        # Scope = the code HB-OS-cubook documents (anchors.jsonl: src/cubook and cmd/srf-cu-*); any change there, additions included, fails.
        # Other packages (W8 src/workerbridge, the desktop capsule fb tmpfs) are outside the book.
        r = subprocess.run(["git", "diff", "--stat", "4cc6765", "--", "src/cubook", ":(glob)cmd/srf-cu-*/**", ":(exclude)*_test.go"], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "", r.stdout)

    def test_driver_has_no_monkeypatch(self):
        src = open(os.path.join(OSDIR, "hb_os_driver.py"), encoding="utf-8").read()
        for bad in ("X.Repo =", "X.extract", "X.symbol_texts", "anchors_from_symbols =", "subanchor =", "render_gen ="):
            self.assertNotIn(bad, src)
        self.assertIn("symbols_bundle=", src)


if __name__ == "__main__":
    unittest.main()
