"""JD3-1/JD3-2: the S2 companion book is bound to current source; the sig64 confidence-mask query routes.

JD3-1 (JUDGE_SEAL1c_JDOCS): documented thresholds, cited anchors, span coordinates and receipt metadata of
hyperbooks/HB-VC-s2-change/book.md must agree with visioncore/change.py, params.py and the receipt JSON.
JD3-2: 'Where are sig64 confidence masks generated?' must answer vc.s2.sig_conf on a catalogue built from the
current tree (built in memory into a temp dir; the committed catalogue is not touched).
"""
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import unittest

from visioncore import change as C
from visioncore import params as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK = os.path.join(ROOT, "hyperbooks", "HB-VC-s2-change", "book.md")
SRC = os.path.join(ROOT, "visioncore", "change.py")
RECEIPT = os.path.join(ROOT, "hyperbooks", "HB-VC-s2-change", "receipts", "R-s2-change-unittest.json")
QUERY = "Where are sig64 confidence masks generated?"


def _load_tool(name):
    spec = importlib.util.spec_from_file_location("_e13_" + name, os.path.join(ROOT, "tools", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _source_anchors():
    out = set()
    for d in ("visioncore",):
        for f in sorted(os.listdir(os.path.join(ROOT, d))):
            if f.endswith(".py"):
                with open(os.path.join(ROOT, d, f), encoding="utf-8") as fh:
                    out |= set(re.findall(r"@anchor:([\w.]+)", fh.read()))
    return out


def book_defects(book, source=None, receipt=None):
    """List of contradictions between book text and the current source/receipt (empty = bound)."""
    if source is None:
        with open(SRC, encoding="utf-8") as f:
            source = f.read()
    if receipt is None:
        with open(RECEIPT, encoding="utf-8") as f:
            receipt = json.load(f)
    out = []
    # 1. documented thresholds: rows "| `NAME` | <int> |" must equal params.PARAMS / change module constants
    for name, val in re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \| (-?\d+)(?: [^|]*)? \|", book, re.M):
        if name in P.PARAMS:
            code = P.PARAMS[name]
        elif hasattr(C, name):
            code = getattr(C, name)
        else:
            out.append("threshold %s not in params.py or change.py" % name)
            continue
        if int(val) != int(code):
            out.append("threshold %s: book %s != code %s" % (name, val, code))
    for name in ("FROZEN_PRE_ACT", "FROZEN_COV_PPM", "H_DEAD", "SIG_DEAD_Q4", "DRIFT_U_Q4"):
        if not re.search(r"^\| `%s` \| -?\d+(?: [^|]*)? \|" % name, book, re.M):
            out.append("threshold %s undocumented" % name)
    # 2. obsolete inline values / claims that contradict current code (JD3-1 categories)
    stale = [
        (r"pre_act ≥ 4|pre_act < 4|hist16_before & 0xFF", "freeze activity: full history, FROZEN_PRE_ACT = %d" % C.FROZEN_PRE_ACT),
        (r"300000 ppm|coverage < 300000", "FROZEN_COV_PPM is %d" % P.PARAMS["FROZEN_COV_PPM"]),
        (r"No island-vector candidate", "island_vector exists" if "def island_vector" in source else None),
        (r"`SHADE_ONLY` \(b2\) is never set|declared only", "SHADE_ONLY is set" if "|= np.uint8(FLAG_SHADE_ONLY)" in source else None),
        (r"DRIFT uses `DRIFT_U_Q4` \(35\) instead of `T_enter`|`drift_u = DRIFT_U_Q4`", "drift_u = max(T_enter, DRIFT_U_Q4)"
         if "max(t_enter, p[\"DRIFT_U_Q4\"])" in source else None),
        (r"no `# @anchor:` tags", "change.py has anchors" if "# @anchor:vc.s2." in source else None),
        (r"flips\(hist16", "FLICKER is A-B-A only"),
    ]
    for pat, why in stale:
        if why and re.search(pat, book):
            out.append("stale claim /%s/: %s" % (pat, why))
    # 3. cited anchors exist in source
    have = _source_anchors()
    for a in sorted(set(re.findall(r"`(vc\.(?:s2|p)\.[A-Za-z0-9_]+)`", book))):
        if a not in have:
            out.append("anchor %s absent from source" % a)
    for a in sorted(x for x in have if x.startswith("vc.s2.")):
        if "`%s`" % a not in book:
            out.append("source anchor %s undocumented" % a)
    # 4. span coordinates: any "L<a>-L<b>" span with a hash must match current bytes
    lines = source.encode("utf-8").splitlines(keepends=True)
    for sym, a, b, h in re.findall(r"`([^`]+)` \| L(\d+)-L(\d+) \| `([0-9a-f]{64})`", book):
        if hashlib.sha256(b"".join(lines[int(a) - 1:int(b)])).hexdigest() != h:
            out.append("span %s L%s-L%s stale" % (sym, a, b))
    for sym, a, b, h in re.findall(r"change\.py#(\w+)@L(\d+)-L(\d+):([0-9a-f]{12})", book):
        if not hashlib.sha256(b"".join(lines[int(a) - 1:int(b)])).hexdigest().startswith(h):
            out.append("inline span %s L%s-L%s stale" % (sym, a, b))
    # 5. receipt metadata the book states must agree with the receipt JSON
    if re.search(r"tree_clean:\s*false", book) and receipt.get("tree_clean") is not False:
        out.append("book says tree_clean:false, receipt %r" % receipt.get("tree_clean"))
    if re.search(r"commit:\s*null", book) and receipt.get("commit") is not None:
        out.append("book says commit:null, receipt bound")
    if re.search(r"receipts/R-s2-change-full-suite\.json", book):
        out.append("book lists withdrawn receipt R-s2-change-full-suite as present")
    return out


def _route_fresh(q, k=5):
    bc = _load_tool("build_catalogue")
    rc = _load_tool("route_code")
    out, _info = bc.build(ROOT)
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "catalogue"))
        for name, text in out.items():
            with open(os.path.join(td, "catalogue", name), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        return rc.route(q, k, td)


class S2BookBinding(unittest.TestCase):
    def test_book_bound_to_source(self):
        with open(BOOK, encoding="utf-8") as f:
            self.assertEqual(book_defects(f.read()), [])

    def test_restored_stale_prose_fails(self):
        with open(BOOK, encoding="utf-8") as f:
            book = f.read()
        for bad in ("`pre_act ≥ 4`", "| `FROZEN_COV_PPM` | 300000 ppm |", "1. No island-vector candidate (§3.5)",
                    "2. `SHADE_ONLY` (b2) is never set", "3. DRIFT uses `DRIFT_U_Q4` (35) instead of `T_enter`",
                    "Note: `change.py` carries **no `# @anchor:` tags**",
                    "`receipts/R-s2-change-full-suite.json` (`tree_clean:false`, `commit:null`)",
                    "| `thresholds` | L27-L31 | `%s` |" % ("233ad86e6ec76914174fd27ed524341268cc16bf9d00237d4e6c9054a4154ca0"),
                    "| `FROZEN_PRE_ACT` | 4 frames |"):
            with self.subTest(bad=bad):
                self.assertNotEqual(book_defects(book + "\n" + bad + "\n"), [])

    def test_receipt_tree_metadata_consistent(self):
        with open(RECEIPT, encoding="utf-8") as f:
            rc = json.load(f)
        if rc["commit"] is not None:
            self.assertIs(rc["tree_clean"], True)


class SigConfRoute(unittest.TestCase):
    def test_confidence_mask_query_routes_to_sig_conf(self):
        res = _route_fresh(QUERY)
        self.assertFalse(res["abstain"]["flag"], res["abstain"])
        self.assertIn("vc.s2.sig_conf", [r[0] for r in res["r"]])

    def test_dead_band_query_still_routes(self):
        res = _route_fresh("where is the sig64 dead band")
        self.assertIn("vc.s2.sig_conf", [r[0] for r in res["r"]])

    def test_absent_topic_still_abstains(self):
        res = _route_fresh("quantum blockchain teleportation ledger")
        self.assertTrue(res["abstain"]["flag"])


if __name__ == "__main__":
    unittest.main()
