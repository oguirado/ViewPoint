"""Tests for tools/hb_gates.py: a clean fixture tree passes every gate; every gate has a derived negative (rc 5)."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import hb_gates as H  # noqa: E402

SECS = ["Reading map", "Contracts", "Types", "DAG", "Triptych", "Subsystems", "Limits", "Narrative", "Symbols", "Tests and gates", "Provenance"]


def book_text(name, extra=""):
    out = ["# %s" % name, ""]
    for i, t in enumerate(SECS):
        out += ['## <a id="hb-s%d"></a>%d. %s' % (i, i, t), ""]
        if i == 4:
            out += ["**Oracle**: `tests/test_x.py`; reviewer mutants `bm.*`.", ""]
        elif i >= 8:
            out += ["Generated: `src/mod.py` (fixture).", ""]
        else:
            out += ["Text of section %d." % i, ""]
        if i == 1:
            out += ["- C-FX-ONE (MEASURED `tests/test_x.py`): ok.", extra, ""]
    return "\n".join(out)


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Tree(unittest.TestCase):
    ANCESTOR = None  # JW9b-5: a non-None value roots the fixture repo below an ancestor dir of that name

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) if self.ANCESTOR is None else Path(self.td.name) / self.ANCESTOR / "room_G" / "repo"
        shutil.copytree(ROOT / "hyperbooks/standards", self.root / "hyperbooks/standards")
        self.voc = json.loads((self.root / "hyperbooks/standards/vocabulary.json").read_text())
        for b in self.voc["books"]:
            self.w("hyperbooks/%s.md" % b, book_text(b))
        self.w("src/mod.py", '# @module {"id":"bv.mod","layer":1,"owner":"X","book":"HB-BVQA-DRIVE"}\n# @anchor:bv.mod.fn\ndef fn():\n    return 1\n')
        self.w("blender_visual_qa/mod.py", '# @module {"id":"bv.mod","layer":1,"owner":"X","book":"HB-BVQA-DRIVE"}\n# @anchor:bv.mod.fn\ndef fn():\n    return 1\n')
        self.w("tests/test_x.py", "x = 1\n")
        self.nodes([{"id": "bv.mod.fn", "kind": "anchor", "path": "blender_visual_qa/mod.py", "line": 2, "title": "fn"}])
        self.w("tests/router_bench/bench.py", "import sys\nsys.exit(0)\n")
        self.w("tools/build_catalogue.py", "import sys\nsys.exit(0)\n")
        self.w("tools/route_code.py", "import json\n")
        self.w("tools/hb_gates.py", "import re\n")

    def tearDown(self):
        self.td.cleanup()

    def w(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def nodes(self, rows):
        self.w("hyperbooks/nodes.jsonl", "".join(H.canon(r) for r in rows))

    def run_gate(self, g):
        return H.GATES[g](self.root)

    def book(self, name="HB-BVQA-DRIVE"):
        return self.root / "hyperbooks" / (name + ".md")

    def edit_book(self, fn, name="HB-BVQA-DRIVE"):
        p = self.book(name)
        p.write_text(fn(p.read_text()))

    def neg(self, gate, mutate, needle):
        """Derived negative: the gate passes on the clean tree and fails (naming `needle`) after exactly one mutation."""
        self.assertEqual(self.run_gate(gate), [], gate + " must pass clean")
        mutate()
        got = self.run_gate(gate)
        self.assertTrue(got and any(needle in x for x in got), (gate, needle, got))


class TestClean(Tree):
    def test_all_gates_pass_clean_and_cli_rc0(self):
        for g in H.GATES:
            self.assertEqual(self.run_gate(g), [], g)
        r = subprocess.run([sys.executable, "-B", str(ROOT / "tools/hb_gates.py"), "--all", "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout[:400])
        outs = {subprocess.run([sys.executable, "-B", str(ROOT / "tools/hb_gates.py"), "--all", "--root", str(self.root)], capture_output=True, text=True).stdout for _ in range(3)}
        self.assertEqual(len(outs), 1)
        d = json.loads(r.stdout)
        self.assertEqual([g["gate"] for g in d["gates"]], list(H.GATES))
        self.assertEqual(r.stdout, H.canon(d))

    def test_cli_rc5_and_usage(self):
        self.book().unlink()
        r = subprocess.run([sys.executable, "-B", str(ROOT / "tools/hb_gates.py"), "g_hb_schema", "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 5)
        self.assertEqual(json.loads(r.stdout)["gates"][0]["code"], "E_SCHEMA")
        self.assertEqual(subprocess.run([sys.executable, "-B", str(ROOT / "tools/hb_gates.py")], capture_output=True, text=True).returncode, 2)

    def test_standards_shipped(self):
        std = ROOT / "hyperbooks/standards"
        names = sorted(p.name for p in (std / "schemas").glob("*.schema.json"))
        self.assertEqual(len([n for n in names if n.startswith("hb.")]), 9)
        self.assertIn("ctx.node.v1.schema.json", names)
        for n in names:
            doc = json.loads((std / "schemas" / n).read_text())
            self.assertEqual(doc["$id"] + ".schema.json", n)
        self.assertEqual(len(self.voc["books"]), 7)
        self.assertEqual(set(self.voc["gate_codes"]), {g for g in H.GATES if g.startswith("g_hb_")} | {"g_stdlib", "g_fresh"})

    def test_validator_subset(self):
        s = {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer", "minimum": 1}}, "additionalProperties": False}
        self.assertEqual(H.validate({"a": 1}, s), [])
        self.assertTrue(H.validate({"a": True}, s))
        self.assertTrue(H.validate({"a": 0}, s))
        self.assertTrue(H.validate({}, s))
        self.assertTrue(H.validate({"a": 1, "b": 2}, s))


class TestNegatives(Tree):
    def test_schema_section_missing(self):
        self.neg("g_hb_schema", lambda: self.edit_book(lambda t: t.replace('## <a id="hb-s7"></a>7. Narrative', "## Narrative")), "bad_heading")

    def test_schema_book_absent_and_bad_node(self):
        self.neg("g_hb_schema", lambda: self.book("HB-BVQA-REPO").unlink(), "book_absent:HB-BVQA-REPO")
        self.nodes([{"id": "x", "kind": "anchor", "path": "p", "line": 1}])
        self.assertTrue(any("title:required" in x for x in self.run_gate("g_hb_schema")))

    def test_schema_sidecar_invalid(self):
        self.w("hyperbooks/HB-BVQA-DRIVE/anchors.jsonl", '{"schema":"hb.anchor.v1","anchor":"bv.mod.fn"}\n')
        self.assertTrue(any("anchors.jsonl" in x for x in self.run_gate("g_hb_schema")))

    def test_anchor_unresolved(self):
        self.neg("g_hb_anchor", lambda: self.edit_book(lambda t: t + "\nSee `bv.mod.nope`.\n"), "anchor_unresolved:bv.mod.nope")

    def test_drift_after_edit(self):
        lines = (self.root / "blender_visual_qa/mod.py").read_bytes().splitlines(keepends=True)
        row = {"schema": "hb.anchor.v1", "anchor": "bv.mod.fn", "path": "blender_visual_qa/mod.py", "line": 3, "end_line": 4,
               "span_sha256": sha(b"".join(lines[2:4]))}
        self.w("hyperbooks/HB-BVQA-DRIVE/anchors.jsonl", H.canon(row))
        self.neg("g_hb_drift", lambda: self.w("blender_visual_qa/mod.py", (self.root / "blender_visual_qa/mod.py").read_text().replace("return 1", "return 2")), ":drift")

    def receipt(self, **kw):
        r = {"schema": "hb.receipt.v1", "id": "R-fx-one", "kind": "test", "argv": ["python3", "tests/test_x.py"], "cwd": ".", "commit": None,
             "tree_clean": False, "sources": {"tests/test_x.py": sha(b"x = 1\n")}, "exit_code": 0, "output_sha256": "0" * 64, "metrics": {}}
        r.update(kw)
        self.w("hyperbooks/HB-BVQA-DRIVE/receipts/R-fx-one.json", json.dumps(r))

    def test_receipt_stale_source(self):
        self.receipt()
        self.edit_book(lambda t: t + "\nEvidence R-fx-one.\n")
        self.neg("g_hb_receipt", lambda: self.w("tests/test_x.py", "x = 2\n"), "E_STALE_RECEIPT:source_changed")

    def test_receipt_unresolved_dirty_commit_and_measured(self):
        self.neg("g_hb_receipt", lambda: self.edit_book(lambda t: t + "\nEvidence R-fx-missing.\n"), "receipt_unresolved:R-fx-missing")
        self.edit_book(lambda t: t.replace("\nEvidence R-fx-missing.\n", ""))
        self.receipt(commit="abc", tree_clean=False)
        self.assertTrue(any("commit_with_dirty_tree" in x for x in self.run_gate("g_hb_receipt")))
        self.receipt()
        self.assertEqual(self.run_gate("g_hb_receipt"), [])
        self.edit_book(lambda t: t.replace("(MEASURED `tests/test_x.py`)", "(MEASURED)"))
        self.assertTrue(any("MEASURED_without_receipt:C-FX-ONE" in x for x in self.run_gate("g_hb_receipt")))
        self.edit_book(lambda t: t.replace("(MEASURED)", "(MEASURED `tests/test_gone.py`)"))
        self.assertTrue(any("MEASURED_receipt_missing" in x for x in self.run_gate("g_hb_receipt")))

    def test_oracle_line_and_triptych(self):
        self.neg("g_hb_oracle", lambda: self.edit_book(lambda t: t.replace("**Oracle**", "Oracle")), "oracle_line_missing")
        self.edit_book(lambda t: t.replace("Oracle:", "**Oracle**:"))
        self.edit_book(lambda t: t.replace("`tests/test_x.py`; reviewer mutants `bm.*`.", "see below."))
        self.assertTrue(any("oracle_names_nothing" in x for x in self.run_gate("g_hb_oracle")))
        self.edit_book(lambda t: t.replace("see below.", "`tests/test_gone.py`."))
        self.assertTrue(any("oracle_path_missing" in x for x in self.run_gate("g_hb_oracle")))
        self.edit_book(lambda t: t.replace("`tests/test_gone.py`.", "`tests/test_x.py`."))
        mut = [{"id": "m%d" % i, "killed": True, "rc": 5} for i in range(3)]
        tri = {"schema": "hb.triptych.v1", "book_id": "HB-BVQA-DRIVE", "oracle": {"argv": ["python3", "tests/test_x.py"], "min_killed": 3, "mutants": mut}}
        self.w("hyperbooks/HB-BVQA-DRIVE/triptych.json", json.dumps(tri))
        self.assertEqual(self.run_gate("g_hb_oracle"), [])
        mut[0]["rc"] = 1
        self.w("hyperbooks/HB-BVQA-DRIVE/triptych.json", json.dumps(tri))
        self.assertTrue(any("kill_rc_not_5" in x or "below_min" in x for x in self.run_gate("g_hb_oracle")))

    def test_reading_empty_link_copy_mathlink(self):
        self.neg("g_hb_reading", lambda: self.edit_book(lambda t: t.replace("Text of section 3.", "")), "empty_section")
        self.edit_book(lambda t: t.replace("## <a id=\"hb-s3\"></a>3. DAG\n\n", "## <a id=\"hb-s3\"></a>3. DAG\n\nText of section 3.\n"))
        self.edit_book(lambda t: t.replace("Text of section 3.\n", "[x](docs/nope.md)\n", 1))
        self.assertTrue(any("link_unresolved" in x for x in self.run_gate("g_hb_reading")))
        self.edit_book(lambda t: t.replace("[x](docs/nope.md)", "> " + "copied spec words " * 20))
        self.assertTrue(any("copied_text_suspect" in x for x in self.run_gate("g_hb_reading")))
        self.edit_book(lambda t: t.replace("> " + "copied spec words " * 20, "ok"))
        row = {"schema": "hb.mathlink.v1", "book": "HB-BVQA-DRIVE", "status": "SEALED", "target_path": "s.md", "target_id": "T1", "quote": "q", "quote_sha256": sha(b"q")}
        self.w("hyperbooks/HB-BVQA-DRIVE/mathlinks.jsonl", H.canon(row))
        self.assertEqual(self.run_gate("g_hb_reading"), [])
        row["quote"] = "tampered"
        self.w("hyperbooks/HB-BVQA-DRIVE/mathlinks.jsonl", H.canon(row))
        self.assertTrue(any("mathlink_quote_sha" in x for x in self.run_gate("g_hb_reading")))

    def test_rebuild_forms(self):
        self.neg("g_hb_rebuild", lambda: self.edit_book(lambda t: t.replace("Generated: `src/mod.py`", "Generated: `src/gone.py`", 1)), "generator_source_missing")
        self.edit_book(lambda t: t.replace("Generated: `src/gone.py` (fixture).", "Handwritten prose", 1))
        self.assertTrue(any("not_declared_generated" in x for x in self.run_gate("g_hb_rebuild")))
        body = ["rows", "more"]
        sealed = "\n".join([H.seal_generated(body)] + body)
        self.edit_book(lambda t: t.replace("Handwritten prose", sealed, 1))
        self.assertEqual(self.run_gate("g_hb_rebuild"), [])
        self.edit_book(lambda t: t.replace("more", "MORE", 1))
        self.assertTrue(any("generated_body_changed" in x for x in self.run_gate("g_hb_rebuild")))
        self.edit_book(lambda t: t.replace("MORE", "more", 1).replace("\n", "\r\n"))
        self.assertTrue(any("not_lf" in x for x in self.run_gate("g_hb_rebuild")))

    def test_budget(self):
        big = {"id": "bv.mod.fn", "kind": "symbol", "path": "p", "line": 1, "title": "t", "purpose": "x" * 2100}
        self.neg("g_hb_budget", lambda: self.nodes([big]), "over_budget")
        big["kind"] = "section"
        self.nodes([big])
        self.assertEqual(self.run_gate("g_hb_budget"), [])

    def test_public(self):
        self.neg("g_hb_public", lambda: self.edit_book(lambda t: t + "\nAIza" + "A" * 30 + "\n"), "HB-BVQA-DRIVE.md")

    def test_router(self):
        self.neg("g_hb_router", lambda: self.nodes([{"id": "x", "kind": "symbol", "path": "", "line": 0, "title": "t"}]), "O-B11")
        self.nodes([])
        self.w("tests/router_bench/bench.py", "import sys\nsys.exit(5)\n")
        self.assertTrue(any("router_bench:rc5" in x for x in self.run_gate("g_hb_router")))
        (self.root / "tests/router_bench/bench.py").unlink()
        self.assertTrue(any("AUSENTE" in x for x in self.run_gate("g_hb_router")))

    def test_header_ratio(self):
        self.neg("g_hb_header", lambda: self.w("blender_visual_qa/mod.py", '# @module {"id":"bv.mod","layer":1,"owner":"X"}\n'), "header:book_field")

    def test_stdlib(self):
        self.neg("g_stdlib", lambda: self.w("hyperbooks/tool.py", "import numpy\n"), "non_stdlib:numpy")

    def test_fresh(self):
        self.neg("g_fresh", lambda: self.w("tools/build_catalogue.py", "import sys\nsys.exit(4)\n"), "rc4")
        self.w("tools/build_catalogue.py", "import sys\nsys.exit(0)\n")
        self.nodes([{"id": "bv.mod.gone", "kind": "anchor", "path": "blender_visual_qa/mod.py", "line": 1, "title": "t"}])
        self.assertTrue(any("anchor_stale" in x for x in self.run_gate("g_fresh")))


class TestBuildAncestor(Tree):
    """JW9b-5: code_files filters repo-RELATIVE components, so a checkout below an ancestor named build (the mandated
    scratch build/room_<lane>/) keeps every source file; a repo-relative build/ dir is still excluded."""
    ANCESTOR = "build"

    def test_code_files_kept_under_build_ancestor(self):
        self.assertIn("build", self.root.parts)
        got = [p.relative_to(self.root).as_posix() for p in H.code_files(self.root)]
        self.assertIn("blender_visual_qa/mod.py", got)
        self.assertIn("tools/hb_gates.py", got)

    def test_repo_relative_build_and_pycache_excluded(self):
        self.w("blender_visual_qa/build/junk.py", "x = 1\n")
        self.w("tools/__pycache__/junk.py", "x = 1\n")
        got = [p.relative_to(self.root).as_posix() for p in H.code_files(self.root)]
        self.assertNotIn("blender_visual_qa/build/junk.py", got)
        self.assertNotIn("tools/__pycache__/junk.py", got)

    def test_all_gates_pass_clean_and_cli_rc0(self):
        TestClean.test_all_gates_pass_clean_and_cli_rc0(self)

    def test_header_ratio(self):
        TestNegatives.test_header_ratio(self)

    def test_fresh(self):
        TestNegatives.test_fresh(self)


class TestPurity(unittest.TestCase):
    def test_no_volatile_calls_and_repo_gates(self):
        r = subprocess.run([sys.executable, "-B", str(ROOT / "tools/gates.py"), "g_pure", "g_threshold"], capture_output=True, text=True, cwd=str(ROOT))
        d = json.loads(r.stdout)
        self.assertEqual([g["failures"] for g in d["gates"] if g["gate"] == "g_pure"], [[]])
        self.assertFalse([f for g in d["gates"] for f in g["failures"] if "hb_gates" in f])


if __name__ == "__main__":
    unittest.main()
