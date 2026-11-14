"""Lane INT tests: Go symbols get no sub-anchors under the 681dc0b pin (hb_build.py:149 calls subanchor only for lang python), gates.sh wiring, Panel-3 oracle paths exist. Synthetic fixture only."""
import glob, json, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OSDIR = os.path.dirname(HERE)
REPO = os.path.abspath(os.path.join(OSDIR, "..", "..", ".."))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import fixture as F  # noqa: E402

BIG = "package demo\n\n// Big is a deliberately long function (> SYM_MAX bytes).\nfunc Big(a int) int {\n" + "".join(
    "\tif a == %d {\n\t\ta += %d\n\t}\n" % (i, i * 3 + 1) for i in range(1, 120)) + "\treturn a\n}\n"


class GoSubanchor(unittest.TestCase):
    def setUp(self):
        self.f = F.Fixture()
        self.addCleanup(self.f.cleanup)
        self.f.write_go(F.GO_SRC + BIG[len("package demo\n"):])

    def test_oversize_go_symbol_builds_without_children(self):
        self.assertGreater(len(BIG), 1800)
        rc, out = self.f.drv("build")
        self.assertNotIn("SyntaxError", out)
        self.assertNotEqual(rc, 5, out)
        syms = [json.loads(l) for l in open(os.path.join(self.f.root, F.BOOK, "symbols.jsonl")) if l.strip()]
        big = [s for s in syms if s["id"].endswith("::Big")]
        self.assertEqual(len(big), 1)
        self.assertGreater(big[0]["bytes"], 1800)
        self.assertFalse(big[0].get("children"), big[0].get("children"))
        self.assertFalse([s for s in syms if s.get("kind") == "subanchor"])


class GatesWiring(unittest.TestCase):
    def lines(self):
        return open(os.path.join(REPO, "scripts", "cu", "gates.sh"), encoding="utf-8").read().splitlines()

    def test_hb_os_cubook_gate_line(self):
        self.assertIn("g hb-os-cubook bash hyperbooks/_tools/os/hb_os_gates.sh HB-OS-cubook", self.lines())

    def test_contracts_cubook_gate_and_order(self):
        ls = self.lines()
        cc = [i for i, l in enumerate(ls) if l.startswith("g contracts-cubook ")]
        self.assertEqual(len(cc), 1)
        body = ls[cc[0]]
        for need in ("contracts_bin", "run_conformance.py", "contracts/cubook/tests"):
            self.assertIn(need, body)
        mut = [i for i, l in enumerate(ls) if "g mutants" in l]
        self.assertTrue(mut and cc[0] < mut[0], "contracts-cubook must precede mutants")


    def test_eval_suite_and_leak_check_are_gates(self):  # RD5A-02
        ls = self.lines()
        a = ls.index("g hb-os-cubook-clean git diff --exit-code -- hyperbooks/HB-OS-cubook")
        self.assertTrue(ls[a + 1].startswith("# RD5A-02"))
        self.assertEqual(ls[a + 2], "g hb-os-eval python3 -B -m unittest eval.hb_os.tests.test_result eval.hb_os.tests.test_measure eval.hb_os.tests.test_hb_os_prereg")
        self.assertEqual(ls[a + 3], "g hb-os-leak python3 -B eval/hb_os/leak_check.py")

    def test_clean_book_gate_follows_hb_os_cubook(self):  # JW5A-01
        ls = self.lines()
        a = ls.index("g hb-os-cubook bash hyperbooks/_tools/os/hb_os_gates.sh HB-OS-cubook")
        self.assertEqual(ls[a + 2], "g hb-os-cubook-clean git diff --exit-code -- hyperbooks/HB-OS-cubook")

    def test_go_vet_covers_cubook_and_all_clis_and_p5(self):  # ADC-W5A-05
        body = [l for l in self.lines() if l.startswith("g go-vet ")]
        self.assertEqual(len(body), 1)
        for need in ("-p 5", "./src/cubook/...", "./cmd/srf-cu-mcp/...", "./cmd/srf-cu-exec/...", "./cmd/srf-cu-snap/...", "./cmd/srf-cu-wlcap/..."):
            self.assertIn(need, body[0])
        self.assertFalse([l for l in self.lines() if l.startswith("g ") and "go test -p 2" in l])


class Panel3Oracles(unittest.TestCase):
    def _book(self):
        return open(os.path.join(REPO, "hyperbooks", "HB-OS-cubook", "book.md"), encoding="utf-8").read()

    def test_every_contracts_path_in_book_exists(self):
        paths = sorted(set(re.findall(r"contracts/cubook/[A-Za-z0-9_./-]+\.(?:py|json|md)", self._book())))
        self.assertTrue(paths)
        missing = [p for p in paths if not os.path.exists(os.path.join(REPO, p))]
        self.assertEqual(missing, [])

    def test_every_contracts_schema_and_runner_is_cited(self):  # JW5A-02: per id, not a count floor
        paths = set(re.findall(r"contracts/cubook/[A-Za-z0-9_./-]+\.(?:py|json|md)", self._book()))
        want = {os.path.relpath(p, REPO) for p in glob.glob(os.path.join(REPO, "contracts", "cubook", "schemas", "*.schema.json"))}
        want |= {"contracts/cubook/run_conformance.py", "contracts/cubook/tests/test_conformance.py"}
        self.assertGreaterEqual(len(want), 17)
        self.assertEqual(sorted(want - paths), [])

    def test_every_schema_id_is_cited(self):  # every $id in contracts/cubook/schemas appears in book.md
        book = self._book()
        ids = []
        for p in sorted(glob.glob(os.path.join(REPO, "contracts", "cubook", "schemas", "*.schema.json"))):
            sid = json.load(open(p, encoding="utf-8")).get("$id") or json.load(open(p, encoding="utf-8")).get("title")
            ids.append((os.path.basename(p), sid))
        self.assertTrue(all(sid for _, sid in ids), ids)
        self.assertEqual([f for f, sid in ids if sid not in book], [])


if __name__ == "__main__":
    unittest.main()
