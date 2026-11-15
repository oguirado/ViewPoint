"""FX W5 r1 JW5-2..JW5-5, JW5-7: HB-VC companion corpus admission (positive on the live tree, negatives on temp copies)."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hyperbooks", "_tools"))
import admission  # noqa: E402

HB = os.path.join(ROOT, "hyperbooks")


def _receipt(**kw):
    rc = {"schema": "hb.receipt.v1", "id": "R-x", "kind": "test", "argv": ["python3", "-m", "unittest", "t"], "cwd": ".",
          "commit": None, "tree_clean": False, "sources": {}, "exit_code": 0, "output_sha256": "0" * 64, "metrics": {}}
    rc.update(kw)
    return rc


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def write(self, rel, text):
        p = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p


class LiveCorpus(unittest.TestCase):
    def test_corpus_admitted(self):
        self.assertEqual(admission.check_corpus(HB, ROOT), [])

    def test_tracked_runners_exist(self):
        for r in ("sel.py", "rle.py", "reseal.py", "bench_desc.py"):
            self.assertTrue(os.path.isfile(os.path.join(HB, "_tools", r)), r)

    def test_every_book_has_mathlinks_and_format(self):
        self.assertTrue(os.path.isfile(os.path.join(HB, "COMPANION_FORMAT.json")))
        for b in os.listdir(HB):
            if b.startswith("HB-VC-"):
                self.assertTrue(os.path.isfile(os.path.join(HB, b, "mathlinks.jsonl")), b)

    def test_s7_prose_matches_tree(self):
        with open(os.path.join(HB, "HB-VC-s7-vmem", "book.md"), encoding="utf-8") as f:
            t = f.read()
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "vmem", "ingest.py")))
        self.assertNotIn("`vmem/ingest.py` AUSENTE", t)
        self.assertNotIn("Not implemented today (AUSENTE): `vmem/ingest.py`", t)


class Receipts(Tmp):
    def test_commit_with_dirty_tree_rejected(self):
        p = self.write("r.json", json.dumps(_receipt(commit="a" * 40, tree_clean=False)))
        self.assertTrue(any("tree_clean" in x for x in admission.check_receipt(p, self.d)))

    def test_commit_clean_accepted(self):
        p = self.write("r.json", json.dumps(_receipt(commit="a" * 40, tree_clean=True)))
        self.assertEqual(admission.check_receipt(p, self.d), [])

    def test_absent_runner_rejected(self):
        p = self.write("r.json", json.dumps(_receipt(argv=["python3", "build/room_H4/sel.py"])))
        self.assertTrue(any("runner" in x for x in admission.check_receipt(p, self.d)))

    def test_source_drift_and_absent_rejected(self):
        self.write("src.py", "x=1\n")
        good = hashlib.sha256(b"x=1\n").hexdigest()
        p = self.write("r.json", json.dumps(_receipt(sources={"src.py": good})))
        self.assertEqual(admission.check_receipt(p, self.d), [])
        self.write("src.py", "x=2\n")
        self.assertTrue(any("drift" in x for x in admission.check_receipt(p, self.d)))
        p = self.write("r2.json", json.dumps(_receipt(sources={"gone.py": good})))
        self.assertTrue(any("absent" in x for x in admission.check_receipt(p, self.d)))

    def test_missing_key_rejected(self):
        rc = _receipt()
        del rc["metrics"]
        p = self.write("r.json", json.dumps(rc))
        self.assertTrue(any("metrics" in x for x in admission.check_receipt(p, self.d)))


class Mathlinks(Tmp):
    QUOTE = "the cell bound is 4096"

    def _paper(self):
        return self.write("paper.html", '<a id="c4"></a> ' + self.QUOTE + "\n")

    def _row(self, **kw):
        row = {"schema": "hb.mathlink.v1", "target_id": "VG-0152", "claimed_location": "C4", "status": "SEALED",
               "target_path": "paper.html", "target_anchor": 'id="c4"', "quote": self.QUOTE,
               "quote_sha256": hashlib.sha256(self.QUOTE.encode()).hexdigest()}
        row.update(kw)
        return row

    def test_sealed_row_verifies(self):
        self._paper()
        self.assertEqual(admission.check_mathlink_row(self._row(), self.d), [])

    def test_missing_target_rejected(self):
        self.assertTrue(any("absent" in x for x in admission.check_mathlink_row(self._row(), self.d)))

    def test_altered_quote_rejected(self):
        self._paper()
        r = self._row(quote="the cell bound is 8192")
        self.assertTrue(admission.check_mathlink_row(r, self.d))

    def test_hash_mismatch_and_missing_anchor_rejected(self):
        self._paper()
        self.assertTrue(any("hash" in x for x in admission.check_mathlink_row(self._row(quote_sha256="0" * 64), self.d)))
        self.assertTrue(any("anchor" in x for x in admission.check_mathlink_row(self._row(target_anchor="id=nope"), self.d)))

    def test_ausente_row_may_not_claim_quote(self):
        r = {"schema": "hb.mathlink.v1", "target_id": "VG-1", "claimed_location": "x", "status": "AUSENTE"}
        self.assertEqual(admission.check_mathlink_row(r, self.d), [])
        self.assertTrue(admission.check_mathlink_row(dict(r, quote="invented"), self.d))

    def test_book_without_mathlinks_rejected(self):
        self.write("hb/HB-VC-x/book.md", "x\n")
        self.assertTrue(any("mathlinks" in x for x in admission.check_mathlinks(os.path.join(self.d, "hb"), self.d)))


class Claims(Tmp):
    def _book(self, line, **rc):
        self.write("hb/HB-VC-x/book.md", line + "\n")
        self.write("hb/HB-VC-x/receipts/r.json", json.dumps(_receipt(id="R-x-unit", **rc)))
        return os.path.join(self.d, "hb")

    def test_test_count_must_match_receipt(self):
        hb = self._book("receipt `R-x-unit` (rc 0, 42 tests)", metrics={"tests_run": 47})
        self.assertTrue(any("42 tests" in x for x in admission.check_claims(hb)))
        hb = self._book("receipt `R-x-unit` (rc 0, 47 tests)", metrics={"tests_run": 47})
        self.assertEqual(admission.check_claims(hb), [])

    def test_rc0_claim_must_match_exit_code(self):
        hb = self._book("receipt `R-x-unit` (rc 0)", exit_code=1)
        self.assertTrue(any("rc 0" in x for x in admission.check_claims(hb)))

    def test_pinned_sha_must_match_file(self):
        self.write("m.py", "a=1\n")
        good = hashlib.sha256(b"a=1\n").hexdigest()
        self.write("hb/HB-VC-x/book.md", "| `m.py` | `%s` |\n" % good)
        self.assertEqual(admission.check_pins(os.path.join(self.d, "hb"), self.d), [])
        self.write("m.py", "a=2\n")
        self.assertTrue(admission.check_pins(os.path.join(self.d, "hb"), self.d))

    def test_nonzero_exit_needs_waiver(self):
        p = self.write("r.json", json.dumps(_receipt(exit_code=1)))
        self.assertTrue(any("waiver" in x for x in admission.check_receipt(p, self.d)))
        self.write("hyperbooks/COMPANION_FORMAT.json", json.dumps({"waivers": {"R-x": "why"}}))
        self.assertEqual(admission.check_receipt(p, self.d), [])


class Format(Tmp):
    def test_missing_format_rejected(self):
        self.assertTrue(admission.check_format(self.d))

    def test_prose_absence_claim_checked(self):
        self.write("vmem/ingest.py", "x\n")
        self.write("hb/HB-VC-x/book.md", "`vmem/ingest.py` is AUSENTE here.\n`other/none.py` is AUSENTE.\n")
        out = admission.check_prose(os.path.join(self.d, "hb"), self.d)
        self.assertEqual(len(out), 1)
        self.assertIn("vmem/ingest.py", out[0])


class ProfileLockstep(Tmp):
    """HBM1 wave C (E1): every expected book is hb-hybrid/v1 in the live declaration; dropping an entry is rejected."""

    def _decl(self):
        with open(os.path.join(HB, "COMPANION_FORMAT.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_all_expected_books_profiled(self):
        d = self._decl()
        self.assertEqual(len(d["expected_books"]), 10)
        self.assertEqual(d["profiles"], {b: "hb-hybrid/v1" for b in d["expected_books"]})

    def test_dropping_a_profile_is_rejected(self):
        d = self._decl()
        victim = "HB-VC-s7-vmem"
        del d["profiles"][victim]
        hb = os.path.join(self.d, "hyperbooks")
        shutil.copytree(HB, hb, ignore=shutil.ignore_patterns("__pycache__", "_tools"))
        with open(os.path.join(hb, "COMPANION_FORMAT.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)
        errs = admission.check_corpus(hb, ROOT)
        self.assertTrue(any(victim in e and "no profile" in e for e in errs), errs[:5])


if __name__ == "__main__":
    unittest.main()
