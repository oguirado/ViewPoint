"""W13 S4: nodes.jsonl integrity (O-B11), router catalogue freshness, frozen router bench (recall@5 / MRR), derived negatives."""
import contextlib
import copy
import io
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bench = _load("bvqa_router_bench", HERE / "bench.py")
builder = _load("bvqa_router_builder", ROOT / "catalogue/router/build_router_catalogue.py")


def nodes():
    return [json.loads(ln) for ln in (ROOT / "hyperbooks/nodes.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]


def bad_rows(rows, root=ROOT):
    """O-B11 checker: rows with an empty path, line < 1, a missing file or a line beyond the file."""
    bad = []
    for r in rows:
        p = r.get("path", "")
        if not p or int(r.get("line", 0)) < 1:
            bad.append(r.get("id") or r.get("anchor"))
            continue
        f = Path(root) / p
        if not f.is_file() or int(r["line"]) > len(f.read_text(encoding="utf-8").splitlines()):
            bad.append(r.get("id") or r.get("anchor"))
    return bad


class NodesIntegrity(unittest.TestCase):
    def test_no_empty_path_line0(self):
        rows = nodes()
        self.assertGreaterEqual(len(rows), 100)
        self.assertEqual(bad_rows(rows), [])

    def test_router_symbols_resolve(self):
        rows = [json.loads(ln) for ln in (ROOT / "catalogue/router/catalogue/symbols.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(bad_rows(rows), [])
        self.assertEqual({r["anchor"] for r in rows}, {n["id"] for n in nodes()})

    def test_negative_empty_path_is_caught(self):
        rows = copy.deepcopy(nodes())
        rows[0]["path"], rows[0]["line"] = "", 0
        self.assertEqual(bad_rows(rows), [rows[0]["id"]])
        rows = copy.deepcopy(nodes())
        rows[1]["line"] = 10 ** 7
        self.assertEqual(bad_rows(rows), [rows[1]["id"]])

    def test_ids_unique_sorted_and_kinds(self):
        rows = nodes()
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertEqual({r["kind"] for r in rows} - {"anchor", "symbol", "section"}, set())
        for r in rows:
            if r["kind"] == "section":
                self.assertRegex(r["id"], r"^hb:HB-BVQA-[A-Z]+#hb-[a-z0-9]+$")
            self.assertTrue(set(r["cites"]) <= set(ids))

    def test_every_hyperbook_has_sections(self):
        books = {r["path"] for r in nodes() if r["kind"] == "section"}
        for b in sorted((ROOT / "hyperbooks").glob("HB-BVQA-*.md")):
            self.assertIn("hyperbooks/" + b.name, books)

    def test_every_code_anchor_is_a_node(self):
        ids = {r["id"] for r in nodes() if r["kind"] == "anchor"}
        for rel, line in [("bv.cli.main", None), ("bpy.canon.cycles_seed", None), ("tools.gates.main", None), ("bv.oracles.qualify_mutant", None)]:
            self.assertIn(rel, ids)


class BuilderDeterminism(unittest.TestCase):
    def test_generated_files_are_fresh(self):
        r = subprocess.run([sys.executable, "-B", str(ROOT / "catalogue/router/build_router_catalogue.py"), "--check"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_build_twice_identical(self):
        self.assertEqual(builder.build(str(ROOT)), builder.build(str(ROOT)))

    def test_stale_is_detected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "build") if (ROOT / "build").is_dir() else tempfile.TemporaryDirectory() as td:
            t = Path(td)
            for sub in ("blender_visual_qa", "bpy_side", "tools", "hyperbooks", "catalogue"):
                shutil.copytree(ROOT / sub, t / sub, ignore=shutil.ignore_patterns("__pycache__"))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(builder.main(["--root", str(t), "--check"]), 0)
                (t / "hyperbooks/nodes.jsonl").write_text("", encoding="utf-8")
                self.assertEqual(builder.main(["--root", str(t), "--check"]), 4)


class FrozenBench(unittest.TestCase):
    def test_frozen_digest_and_counts(self):
        fz = json.loads((HERE / "FROZEN.json").read_text(encoding="utf-8"))
        qs = bench.read_queries()
        self.assertEqual(fz["test_sha256"], bench.test_digest(qs))
        for s, n in fz["counts"].items():
            self.assertEqual(sum(1 for q in qs if q["split"] == s), n)
        self.assertEqual(len({q["qid"] for q in qs}), len(qs))
        self.assertEqual(len({q["query"] for q in qs}), len(qs))

    def test_hb_gate_router_item(self):
        """The S1 gate g_hb_router runs this bench (--split TEST) and the O-B11 row check: it must report nothing."""
        spec = importlib.util.spec_from_file_location("bvqa_hb_gates", ROOT / "tools/hb_gates.py")
        hb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hb)
        out = hb.g_hb_router(ROOT)
        self.assertEqual(out, [])

    def test_accepted_ids_exist(self):
        ids = {r["id"] for r in nodes()}
        for q in bench.read_queries():
            if q["kind"] != "pos":
                self.assertEqual(q["accept"], "")
                continue
            ok = [a for a in q["accept"].split(",") if (any(i.startswith(a[:-1]) for i in ids) if a.endswith("*") else a in ids)]
            self.assertTrue(ok, q["qid"])

    def test_test_split_at_target(self):
        r = bench.run([q for q in bench.read_queries() if q["split"] == "TEST"])
        self.assertGreaterEqual(r["recall5_ppm"], 700000, r)
        self.assertGreaterEqual(r["mrr_ppm"], 500000, r)
        self.assertEqual(r["neg_answered"], [])
        self.assertGreaterEqual(r["n_pos"], 15)
        self.assertGreaterEqual(r["n_neg"], 4)

    def test_cli_rc0_on_test(self):
        r = subprocess.run([sys.executable, "-B", str(HERE / "bench.py"), "--split", "TEST"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_negative_empty_catalogue_fails_target(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "catalogue").mkdir()
            for n in ("routes.tsv", "thresholds.tsv", "failures.tsv", "symbols.jsonl"):
                (Path(td) / "catalogue" / n).write_text("", encoding="utf-8")
            r = bench.run([q for q in bench.read_queries() if q["split"] == "TEST"], route_root=td)
        self.assertEqual(r["recall5_ppm"], 0)

    def test_negative_pre_s4_catalogue_misses_target(self):
        """The 18-row catalogue vendored at 6c549f1 (before W13) must not reach the target: this bench was red before S4."""
        old = subprocess.run(["git", "show", "2390e8d:catalogue/router/catalogue/symbols.jsonl"], capture_output=True, text=True, cwd=ROOT)
        if old.returncode:
            self.skipTest("commit 2390e8d not reachable")
        with tempfile.TemporaryDirectory() as td:
            cat = Path(td) / "catalogue"
            cat.mkdir()
            (cat / "symbols.jsonl").write_text(old.stdout, encoding="utf-8")
            for n in ("routes.tsv", "thresholds.tsv", "failures.tsv"):
                g = subprocess.run(["git", "show", "2390e8d:catalogue/router/catalogue/" + n], capture_output=True, text=True, cwd=ROOT)
                (cat / n).write_text(g.stdout, encoding="utf-8")
            r = bench.run([q for q in bench.read_queries() if q["split"] == "TEST"], route_root=td)
        self.assertLess(r["recall5_ppm"], 700000, r)

    def test_changed_test_split_is_stale_rc4(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            qs = (HERE / "queries.tsv").read_text(encoding="utf-8").replace("best pizza recipe", "best sushi recipe")
            (t / "queries.tsv").write_text(qs, encoding="utf-8")
            self.assertNotEqual(bench.test_digest(bench.read_queries(str(t / "queries.tsv"))), bench.test_digest(bench.read_queries()))


if __name__ == "__main__":
    unittest.main()
