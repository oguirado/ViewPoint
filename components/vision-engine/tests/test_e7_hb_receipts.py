"""FX4 JD2-1: receipt completeness. Negatives run on a temp copy of the live corpus; each fails on BASE_E (checker returned [])."""
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


class Copy(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.hb = os.path.join(self.d, "hyperbooks")
        shutil.copytree(HB, self.hb, ignore=shutil.ignore_patterns("__pycache__"))

    def corpus(self):
        return admission.check_corpus(self.hb, ROOT)

    def fmt(self, fn):
        p = os.path.join(self.hb, "COMPANION_FORMAT.json")
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        fn(d)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f)


class ReceiptCompleteness(Copy):
    def test_baseline_admitted(self):
        self.assertEqual(self.corpus(), [])

    def test_delete_referenced_receipt_rejected(self):
        os.remove(os.path.join(self.hb, "HB-VC-s0-grid", "receipts", "R-s0-grid-unittest.json"))
        self.assertTrue(any("R-s0-grid-unittest.json missing" in x for x in self.corpus()))

    def test_delete_all_receipts_rejected(self):
        for b in os.listdir(self.hb):
            rd = os.path.join(self.hb, b, "receipts")
            if os.path.isdir(rd):
                for f in os.listdir(rd):
                    os.remove(os.path.join(rd, f))
        self.assertTrue(self.corpus())

    def test_remove_receipt_directory_rejected(self):
        shutil.rmtree(os.path.join(self.hb, "HB-VC-s3-pyramid", "receipts"))
        self.assertTrue(any("receipts directory missing" in x for x in self.corpus()))

    def test_undeclared_receipt_rejected(self):
        src = os.path.join(self.hb, "HB-VC-s0-grid", "receipts", "R-s0-grid-table.json")
        shutil.copy(src, os.path.join(self.hb, "HB-VC-s0-grid", "receipts", "R-extra-one.json"))
        self.assertTrue(any("not declared" in x for x in self.corpus()))

    def test_duplicate_or_mismatched_id_rejected(self):
        rd = os.path.join(self.hb, "HB-VC-s0-grid", "receipts")
        p = os.path.join(rd, "R-s0-grid-table.json")
        with open(p, encoding="utf-8") as f:
            r = json.load(f)
        r["id"] = "R-s0-grid-unittest"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(r, f)
        self.assertTrue(any("!= file stem" in x for x in self.corpus()))

    def test_unresolved_prose_reference_rejected(self):
        bp = os.path.join(self.hb, "HB-VC-s1-desc", "book.md")
        with open(bp, "a", encoding="utf-8") as f:
            f.write("\nEvidence: `R-s1-desc-ghost` (rc 0).\n")
        self.assertTrue(any("R-s1-desc-ghost referenced but not in inventory" in x for x in self.corpus()))

    def test_missing_inventory_declaration_rejected(self):
        self.fmt(lambda d: d.pop("receipt_inventory"))
        self.assertTrue(any("receipt_inventory" in x for x in self.corpus()))

    def test_inventory_entry_without_file_rejected(self):
        self.fmt(lambda d: d["receipt_inventory"]["HB-VC-s2-change"].append("R-s2-change-new.json"))
        self.assertTrue(any("R-s2-change-new.json missing" in x for x in self.corpus()))

    def test_withdrawn_needs_reason_and_cannot_hide_existing(self):
        self.fmt(lambda d: d["withdrawn_receipts"].update({"R-s0-grid-unittest": "x"}))
        self.assertTrue(any("declared withdrawn" in x for x in self.corpus()))
        self.fmt(lambda d: d["withdrawn_receipts"].update({"R-s0-grid-unittest": " "}))
        self.assertTrue(any("non-empty reason" in x for x in self.corpus()))


class LiveInventory(unittest.TestCase):
    def test_live_inventory_complete(self):
        self.assertEqual(admission.check_receipt_inventory(HB), [])


SPEC_INT = os.environ.get("VE_SPEC_INT", "spec/SPEC_INT.md")


class MasterRegistry(unittest.TestCase):
    def test_master_registry_hdead(self):
        """JC2-1: SPEC_INT R5 (wins ties) H_DEAD default equals visioncore/params.py; fails on BASE_E (2 != 4)."""
        if not os.path.isfile(SPEC_INT):
            self.skipTest("SPEC_INT absent")
        import re
        sys.path.insert(0, ROOT)
        from visioncore import params
        with open(SPEC_INT, encoding="utf-8") as f:
            m = re.search(r"^\| `H_DEAD` / `W_HAM` / `W_CHROMA` \| (\d+) /", f.read(), re.M)
        self.assertIsNotNone(m)
        got = int(m.group(1))
        want = params.PARAMS["H_DEAD"]
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
