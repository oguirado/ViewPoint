import hashlib
import os
import re
import tempfile
import unittest

from eval.hb_os import leak_check

ROOT = leak_check.ROOT
HB_OS = os.path.join(ROOT, "eval", "hb_os")
DEV_JSONL = leak_check.DEV_JSONL
DEV_SHA = os.path.join(HB_OS, "dev", "hb_dev.sha256")
TEST_JSONL = leak_check.TEST_JSONL
TEST_SHA = os.path.join(HB_OS, "heldout", "hb_test.sha256")
PREREG_MD = os.path.join(HB_OS, "PREREG.md")
AUTHOR_MD = os.path.join(HB_OS, "AUTHOR_NOTES.md")

REQUIRED_KEYS = {
    "qid",
    "split",
    "class",
    "text",
    "intent",
    "gold",
    "gold_tests",
    "gold_line",
    "receipt",
    "verdict",
    "source_event",
    "notes",
}

SID_RE = re.compile(r"^[A-Za-z0-9_./-]{1,160}::[A-Za-z_][A-Za-z0-9_.]{0,127}(#b[0-9]{1,3})?$")
GOLD_LINE_RE = re.compile(r"^([A-Za-z0-9_./-]+):L([0-9]+)-L([0-9]+)$")
CUBOOK_PKGS = [
    "src/cubook/action",
    "src/cubook/ajson",
    "src/cubook/capture",
    "src/cubook/exec",
    "src/cubook/mcp",
    "src/cubook/prodgate",
    "src/cubook/rc",
]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestHbOsPrereg(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.surface = leak_check.extract_surface(ROOT)

    def test_prereg_and_author_notes_exist(self):
        self.assertTrue(os.path.isfile(PREREG_MD), "PREREG.md missing")
        self.assertGreater(os.path.getsize(PREREG_MD), 100, "PREREG.md empty")
        self.assertTrue(os.path.isfile(AUTHOR_MD), "AUTHOR_NOTES.md missing")
        self.assertGreater(os.path.getsize(AUTHOR_MD), 100, "AUTHOR_NOTES.md empty")
        with open(AUTHOR_MD, "r", encoding="utf-8") as fh:
            notes = fh.read()
        self.assertIn("hyperbooks/", notes)
        self.assertIn("contracts/", notes)

    def test_sha256_seals_match(self):
        for jsonl_p, sha_p in [(DEV_JSONL, DEV_SHA), (TEST_JSONL, TEST_SHA)]:
            self.assertTrue(os.path.isfile(jsonl_p), f"missing {jsonl_p}")
            self.assertTrue(os.path.isfile(sha_p), f"missing {sha_p}")
            with open(sha_p, "r", encoding="utf-8") as fh:
                recorded = fh.read().strip().split()[0]
            actual = sha256_file(jsonl_p)
            self.assertEqual(actual, recorded, f"sha256 mismatch for {jsonl_p}")

    def test_row_schema_and_quotas(self):
        dev_rows = leak_check.load_jsonl(DEV_JSONL)
        test_rows = leak_check.load_jsonl(TEST_JSONL)
        self.assertEqual(len(dev_rows), 60, "DEV must have 60 rows")
        self.assertEqual(len(test_rows), 100, "TEST must have 100 rows")

        dev_classes = {"FIND": 0, "ABSENT": 0, "CLAIM": 0}
        test_classes = {"FIND": 0, "ABSENT": 0, "CLAIM": 0}
        dev_pkg_counts = {p: 0 for p in CUBOOK_PKGS}
        test_pkg_counts = {p: 0 for p in CUBOOK_PKGS}
        cli_count = 0
        wl_go_covered = False

        sym_map = self.surface["sym_map"]

        for split_name, rows, class_counts, pkg_counts in [
            ("DEV", dev_rows, dev_classes, dev_pkg_counts),
            ("TEST", test_rows, test_classes, test_pkg_counts),
        ]:
            for r in rows:
                self.assertEqual(set(r.keys()), REQUIRED_KEYS, f"bad keys in {r.get('qid')}")
                self.assertEqual(r["split"], split_name)
                cls = r["class"]
                self.assertIn(cls, class_counts)
                class_counts[cls] += 1
                self.assertIsInstance(r["gold"], list)
                self.assertIsInstance(r["gold_tests"], list)
                self.assertIsInstance(r["source_event"], str)
                self.assertTrue(r["source_event"].strip())

                if cls == "FIND":
                    self.assertEqual(len(r["gold"]), 1, f"{r['qid']} must have 1 gold sid")
                    sid = r["gold"][0]
                    self.assertRegex(sid, SID_RE, f"{r['qid']} invalid sid {sid}")
                    self.assertIn(sid, sym_map, f"{r['qid']} gold sid not in AST: {sid}")
                    sym = sym_map[sid]
                    expected_line = f"{sym['file']}:L{sym['start']}-L{sym['end']}"
                    self.assertEqual(r["gold_line"], expected_line, f"{r['qid']} gold_line mismatch")
                    m = GOLD_LINE_RE.match(r["gold_line"])
                    self.assertIsNotNone(m)
                    fpath, lstart, lend = m.group(1), int(m.group(2)), int(m.group(3))
                    full_p = os.path.join(ROOT, fpath)
                    self.assertTrue(os.path.isfile(full_p))
                    with open(full_p, "r", encoding="utf-8") as fh:
                        total_lines = len(fh.read().splitlines())
                    self.assertGreaterEqual(lstart, 1)
                    self.assertLessEqual(lend, total_lines)
                    pkg_dir = os.path.dirname(sym["file"])
                    if pkg_dir in pkg_counts:
                        pkg_counts[pkg_dir] += 1
                    if sym["file"].startswith("cmd/srf-cu-"):
                        cli_count += 1
                    if sym["file"] == "cmd/srf-cu-exec/wl.go":
                        wl_go_covered = True
                elif cls == "ABSENT":
                    self.assertEqual(r["gold"], [])
                    self.assertEqual(r["gold_line"], "")
                elif cls == "CLAIM":
                    self.assertIn(r["verdict"], ("supported", "contradicted"))
                    self.assertTrue(r["receipt"].startswith("claims.jsonl:"))
                    m = GOLD_LINE_RE.match(r["gold_line"])
                    self.assertIsNotNone(m, f"{r['qid']} invalid claim gold_line")
                    fpath, lstart, lend = m.group(1), int(m.group(2)), int(m.group(3))
                    full_p = os.path.join(ROOT, fpath)
                    self.assertTrue(os.path.isfile(full_p))
                    with open(full_p, "r", encoding="utf-8") as fh:
                        total_lines = len(fh.read().splitlines())
                    self.assertGreaterEqual(lstart, 1)
                    self.assertLessEqual(lend, total_lines)

        self.assertEqual(dev_classes, {"FIND": 30, "ABSENT": 22, "CLAIM": 8})
        self.assertEqual(test_classes, {"FIND": 60, "ABSENT": 40, "CLAIM": 0})
        for p in CUBOOK_PKGS:
            self.assertGreaterEqual(dev_pkg_counts[p], 2, f"DEV FIND < 2 for {p}")
            self.assertGreaterEqual(test_pkg_counts[p], 2, f"TEST FIND < 2 for {p}")
        self.assertEqual(cli_count, 10, "Total CLI FIND rows must be 10")
        self.assertTrue(wl_go_covered, "cmd/srf-cu-exec/wl.go must be covered by FIND gold")

    def test_leak_check_passes_and_catches_mutants(self):
        errs = leak_check.check_files(DEV_JSONL, TEST_JSONL, surface=self.surface)
        self.assertEqual(errs, [], f"leak_check failed: {errs}")

        dev_rows = leak_check.load_jsonl(DEV_JSONL)
        test_rows = leak_check.load_jsonl(TEST_JSONL)

        # Mutant 1: inject leaked Go identifier into a query text
        with tempfile.TemporaryDirectory() as d:
            m1_dev = os.path.join(d, "hb_dev_m1.jsonl")
            mut_rows = [dict(r) for r in dev_rows]
            mut_rows[0] = dict(mut_rows[0], text=mut_rows[0]["text"] + " ValidateManifestV1")
            with open(m1_dev, "w", encoding="utf-8") as fh:
                for r in mut_rows:
                    fh.write(leak_check.json.dumps(r) + "\n")
            m1_errs = leak_check.check_files(m1_dev, TEST_JSONL, surface=self.surface)
            self.assertTrue(any("E_LEAK" in e for e in m1_errs), f"Mutant 1 not caught: {m1_errs}")

        # Mutant 2: inject TEST row into DEV file
        with tempfile.TemporaryDirectory() as d:
            m2_dev = os.path.join(d, "hb_dev_m2.jsonl")
            mut_rows = [dict(r) for r in dev_rows] + [dict(test_rows[0])]
            with open(m2_dev, "w", encoding="utf-8") as fh:
                for r in mut_rows:
                    fh.write(leak_check.json.dumps(r) + "\n")
            m2_errs = leak_check.check_files(m2_dev, TEST_JSONL, surface=self.surface)
            self.assertTrue(any("E_BLIND" in e for e in m2_errs), f"Mutant 2 not caught: {m2_errs}")


if __name__ == "__main__":
    unittest.main()
