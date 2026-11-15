"""RT predicates, rejecting fixtures, and positive controls for code navigation."""
import ast
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import build_code_index as bci, route_code as rc

ROOT = Path(__file__).resolve().parent.parent


# @anchor:va.test_route_code_unit.TestCodeIndex [HB-VC-a0-arch]
class TestCodeIndex(unittest.TestCase):
    def _fixture(self, root, anchored=True):
        p = root / "visioncore/sample_mod.py"
        p.parent.mkdir(exist_ok=True)
        p.write_text('# @module {"stage":"S2"}\n'
                     '# @anchor:vc.sample.anchored_fn\n'
                     'def anchored_fn(x):\n    """Flicker jitter tile classification."""\n    return x + 1\n'
                     + ('# @anchor:vc.sample.other\n' if anchored else '')
                     + 'def other(y):\n    """Classify flicker jitter tile."""\n    return anchored_fn(y)\n'
                     'def _private_helper(z):\n    return z\n')
        return p

    def _call(self, main, args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = main(args)
        self.assertEqual(len(stream.getvalue().splitlines()), 1)
        obj = json.loads(stream.getvalue())
        self.assertEqual(status, obj["rc"])
        self.assertEqual(stream.getvalue(), bci.canon(obj))
        return status, obj

    # @anchor:va.test_route_code_unit.test_must_fail_and_positive_control_coverage_readonly [HB-VC-a0-arch]
    def test_must_fail_and_positive_control_coverage_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = self._fixture(root, False)
            before = p.read_bytes()
            for main in (bci.main, rc.main):
                status, obj = self._call(main, ["--root", tmp, "--check"])
                self.assertEqual(status, 5)
                self.assertEqual(obj["code"], "E_MISSING_ANCHORS")
                self.assertEqual([(r["file"], r["line"], r["symbol"]) for r in obj["missing"]],
                                 [("visioncore/sample_mod.py", 6, "other")])
                self.assertEqual(p.read_bytes(), before)
            self._fixture(root, True)
            for main in (bci.main, rc.main):
                self.assertEqual(self._call(main, ["--root", tmp, "--check"])[1]["missing"], [])

    # @anchor:va.test_route_code_unit.test_check_exports_decorators_async_strings_and_traversal [HB-VC-a0-arch]
    def test_check_exports_decorators_async_strings_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = self._fixture(root)
            p.write_text('__all__ = ["_exported", "good"]\n'
                         'def ignored(): pass\n'
                         'def _exported(): pass\n'
                         '# @anchor:vc.sample.good\n'
                         '@decorate\nasync def good(\n    value,\n):\n    return value\n')
            missing = bci.check_public_anchors(tmp)
            self.assertEqual([r["symbol"] for r in missing], ["_exported"])
            p.write_text(p.read_text().replace('def _exported()', '# @anchor:vc.sample.exported\ndef _exported()'))
            self.assertEqual(bci.check_public_anchors(tmp), [])
            p.write_text('"""\n# @anchor:vc.fake.tag\n"""\ndef untagged(): pass\n')
            self.assertEqual(bci.check_public_anchors(tmp)[0]["symbol"], "untagged")
            for scope in ("../outside.py", "/tmp", "does-not-exist.py"):
                self.assertEqual(self._call(rc.main, ["--root", tmp, "--check", "--scope", scope])[0], 2)

    # @anchor:va.test_route_code_unit.test_build_deterministic_and_companions [HB-VC-a0-arch]
    def test_build_deterministic_and_companions(self):
        with tempfile.TemporaryDirectory() as tmp:
            one, two = Path(tmp) / "one", Path(tmp) / "two"
            a, b = bci.build(ROOT, one), bci.build(ROOT, two)
            self.assertEqual(a["index_sha256"], b["index_sha256"])
            files = lambda p: {f.relative_to(p).as_posix(): f.read_bytes() for f in p.rglob("*") if f.is_file()}
            self.assertEqual(files(one), files(two))
            manifest = json.loads((one / "manifest.json").read_text())
            self.assertEqual(manifest["max_file_bytes"], max(len(raw) for raw in files(one).values()))
            self.assertLessEqual(manifest["max_file_bytes"], bci.CAP)
            companions = json.loads((one / "companions.json").read_text())
            self.assertEqual(set(companions["books"]), set(bci.BOOK_STAGE))
            for book in companions["books"].values():
                self.assertTrue(book["sections"])
                self.assertIn("graph.json", book["companion_files"])
            r = rc.lookup_anchor("vc.s2.classify", ROOT, one)["anchor"]
            self.assertEqual((r["stage"], r["path"]), ("S2", "visioncore/change.py"))
            self.assertTrue(r["doc"])
            self.assertIn("vc.s2.residual", r["callees"])
            with patch.object(rc._bci(), "build_in_memory", side_effect=AssertionError("must read disk")):
                self.assertEqual(rc.lookup_anchor("vc.s2.sig_conf", ROOT, one)["rc"], 0)

    # @anchor:va.test_route_code_unit.test_query_lookup_stages_and_negatives [HB-VC-a0-arch]
    def test_query_lookup_stages_and_negatives(self):
        for flag, value in (("--anchor", "vc.s2.sig_conf"), ("--stage", "S2"), ("--stage", "ALL"),
                            ("--stage", "HB-VC-s2-change")):
            status, obj = self._call(rc.main, [flag, value, "--root", str(ROOT)])
            self.assertEqual(status, 0)
            self.assertLessEqual(len(rc.canon(obj).encode()), 2000)
            if flag == "--anchor":
                self.assertEqual((obj["anchor"]["symbol"], obj["anchor"]["line"], obj["anchor"]["stage"]), ("sig_conf", 67, "S2"))
            else:
                self.assertEqual([r[0] for r in obj["r"]], sorted(r[0] for r in obj["r"]))
        for flag in ("--anchor", "--stage"):
            self.assertEqual(self._call(rc.main, [flag, "DOES_NOT_EXIST", "--root", str(ROOT)])[0], 3)
        result = rc.route("why is a tile classified as flicker instead of jitter", 3, str(ROOT))
        self.assertFalse(result["abstain"]["flag"])
        self.assertTrue({"vc.s2.classify", "vc.p.HYST_BAND_PERMILLE"} & {r[0] for r in result["r"]})
        for row in result["r"]:
            self.assertEqual(len(row), 6)
            self.assertIsInstance(row[3], int)
            self.assertGreater(row[3], 0)
            self.assertTrue(row[5])
        self.assertTrue(rc.route("quantum teleportation protocol", 3, str(ROOT))["abstain"]["flag"])

    # @anchor:va.test_route_code_unit.test_tiebreak_source_addition_and_explicit_stale [HB-VC-a0-arch]
    def test_tiebreak_source_addition_and_explicit_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            self._fixture(root)
            cat = root / "catalogue"
            cat.mkdir()
            rows = [dict(anchor=a, path="visioncore/" + a[3] + ".py", line=1, symbol="same", doc="zebra")
                    for a in ("vc.b.beta", "vc.a.alpha")]
            (cat / "symbols.jsonl").write_text("".join(bci.canon(r) for r in rows))
            self.assertEqual([r[0] for r in rc.route("zebra", 2, root)["r"]], ["vc.a.alpha", "vc.b.beta"])
            out = Path(tmp) / "index"
            bci.build(root, out)
            args = ["--root", str(root), "--index", str(out), "--anchor", "vc.sample.other"]
            self.assertEqual(self._call(rc.main, args)[0], 0)
            new = root / "visioncore/new.py"
            new.write_text('# @anchor:vc.sample.new\ndef new(): pass\n')
            self.assertEqual(self._call(rc.main, args)[0], 4)
            # An implicit cache refreshes from source and resolves the added anchor.
            self.assertEqual(rc.lookup_anchor("vc.sample.new", root)["rc"], 0)
            new.unlink()
            source = root / "visioncore/sample_mod.py"
            source.write_text(source.read_text() + "\n# changed\n")
            self.assertEqual(self._call(rc.main, args)[0], 4)
            self.assertEqual(rc.lookup_anchor("vc.sample.other", root)["rc"], 0)

    # @anchor:va.test_route_code_unit.test_tamper_duplicate_atomic_refusal_and_invalid_cli [HB-VC-a0-arch]
    def test_tamper_duplicate_atomic_refusal_and_invalid_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            source = self._fixture(root)
            out = Path(tmp) / "index"
            bci.build(root, out)
            args = ["--root", str(root), "--index", str(out), "--anchor", "vc.sample.other"]
            target = out / "docs/0.jsonl"
            good = target.read_bytes()
            target.write_bytes(good + b"\n")
            self.assertEqual(self._call(rc.main, args)[0], 5)
            target.write_bytes(good)
            self.assertEqual(self._call(rc.main, args)[0], 0)
            before = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
            source.write_text(source.read_text() + '# @anchor:vc.sample.other\ndef duplicate(): pass\n')
            self.assertEqual(self._call(bci.main, ["--root", str(root), "--out", str(out)])[0], 3)
            self.assertEqual(before, {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()})
            self._fixture(root)
            self.assertEqual(self._call(bci.main, ["--root", str(root), "--out", str(out), "--cap", "1"])[0], 2)
            self.assertEqual(self._call(bci.main, ["--root", str(root), "--out", str(root / "tools")])[0], 2)
        for main in (rc.main, bci.main):
            self.assertEqual(self._call(main, ["--unknown"])[0], 2)
        self.assertEqual(self._call(rc.main, ["--k", "0", "--q", "flicker"])[0], 2)
        self.assertEqual(self._call(rc.main, [])[0], 2)

    # @anchor:va.test_route_code_unit.test_live_check_is_deterministic_and_read_only [HB-VC-a0-arch]
    def test_live_check_is_deterministic_and_read_only(self):
        before = {p: p.read_bytes() for pkg in bci.PKG_DIRS for p in (ROOT / pkg).rglob("*.py")}
        one = bci.check_public_anchors(ROOT)
        self.assertTrue(one)  # Existing legacy gaps remain explicit.
        self.assertEqual(one, bci.check_public_anchors(ROOT))
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        for rel in ("tools/build_code_index.py", "tools/route_code.py"):
            self.assertEqual(bci.check_public_anchors(ROOT, [rel]), [])
        status, result = self._call(rc.main, ["--root", str(ROOT), "--check"])
        self.assertEqual(status, 5)
        self.assertEqual(result["code"], "E_MISSING_ANCHORS")
        self.assertEqual(result["missing_count"], len(one))
        self.assertTrue(result["missing"])
        self.assertEqual(result["missing"], one[:len(result["missing"])])
        self.assertEqual(result["overflow"], len(one) - len(result["missing"]))
        self.assertLessEqual(len(rc.canon(result).encode()), rc.MAX_B)
        self.assertEqual(self._call(bci.main, ["--root", str(ROOT), "--check"])[1]["missing"], one)


    # @anchor:va.test_route_code_unit.test_catalogue_anchor_hyperbook_fallback [HB-VC-a0-arch]
    def test_catalogue_anchor_hyperbook_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            self._fixture(root)
            anchor = "vc.sample.anchored_fn"
            cat = root / "catalogue"
            cat.mkdir()
            (cat / "symbols.jsonl").write_text(bci.canon(dict(
                anchor=anchor, path="visioncore/sample_mod.py", line=2,
                symbol="anchored_fn", doc="Flicker jitter tile classification.")))
            book = root / "hyperbooks/HB-VC-s2-change"
            book.mkdir(parents=True)
            for filename, token in (("nodes.jsonl", "quasar"), ("claims.jsonl", "nebula")):
                with self.subTest(filename=filename):
                    # Missing companion evidence must abstain even for a known catalogue anchor.
                    self.assertTrue(rc.route(token, root=root)["abstain"]["flag"])
                    (book / filename).write_text(bci.canon(dict(
                        anchors=[dict(a=anchor)], purpose=token if filename == "nodes.jsonl" else "",
                        statement=token if filename == "claims.jsonl" else "")))
                    out = Path(tmp) / filename
                    bci.build(root, out)
                    for index_dir in (None, out):
                        docs, _, _ = rc.load(root, index_dir)
                        self.assertNotIn(token, docs[anchor]["_legacy"]["text"])
                        result = rc.route(token, root=root, index_dir=index_dir)
                        self.assertFalse(result["abstain"]["flag"])
                        self.assertEqual(result["r"][0][0], anchor)
                        self.assertIn(token, result["r"][0][5])
                    (book / filename).unlink()

    # @anchor:va.test_route_code_unit.test_legacy_score_unchanged_with_hyperbook_terms [HB-VC-a0-arch]
    def test_legacy_score_unchanged_with_hyperbook_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            cat = root / "catalogue"
            cat.mkdir()
            anchor = "vc.sample.anchored_fn"
            (cat / "symbols.jsonl").write_text(bci.canon(dict(
                anchor=anchor, path="visioncore/sample_mod.py", line=2,
                symbol="anchored_fn", doc="Flicker jitter tile classification.")))
            before = rc.route("flicker", root=root)
            book = root / "hyperbooks/HB-VC-s2-change"
            book.mkdir(parents=True)
            (book / "nodes.jsonl").write_text(bci.canon(dict(
                anchors=[dict(a=anchor)], purpose="quasar " * 100)))
            self.assertEqual(rc.route("flicker", root=root), before)
            docs, _, _ = rc.load(root)
            self.assertNotEqual(rc.score(["flicker"], docs),
                                rc.score(["flicker"], {anchor: docs[anchor]["_legacy"]}))

    # @anchor:va.test_route_code_unit.test_disk_columns_and_postings_are_consumed [HB-VC-a0-arch]
    def test_disk_columns_and_postings_are_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            self._fixture(root)
            out = Path(tmp) / "index"
            bci.build(root, out)
            original = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
            for rel in ("cols.json", "post/fl.json"):
                with self.subTest(rel=rel):
                    for name, raw in original.items():
                        (out / name).write_bytes(raw)
                    self.assertFalse(rc.route("flicker", root=root, index_dir=out)["abstain"]["flag"])
                    obj = json.loads((out / rel).read_text())
                    if rel == "cols.json":
                        obj["anchor"][0] = "vc.invalid.column"
                    else:
                        obj["flicker"]["z"]["doc"][0] = 999999
                    (out / rel).write_text(bci.canon(obj))
                    # Re-sign: failure must come from consuming the data, not its checksum.
                    manifest = json.loads((out / "manifest.json").read_text())
                    manifest["files"][rel] = bci.sha((out / rel).read_bytes())
                    identity = bci.sha(bci.canon(dict(files=manifest["files"], sources=manifest["sources"])).encode())
                    manifest["index_sha256"] = identity
                    (out / "manifest.json").write_text(bci.canon(manifest))
                    (out / "ID").write_text(identity + "\n")
                    status, result = self._call(rc.main, ["--root", str(root), "--index", str(out), "--q", "flicker"])
                    self.assertEqual((status, result["code"]), (5, "E_INDEX"))

    # @anchor:va.test_route_code_unit.test_sharding_and_budget_positive_negative_controls [HB-VC-a0-arch]
    def test_sharding_and_budget_positive_negative_controls(self):
        recs = {}
        for i in range(80):
            a = "vc.sample.item" + str(i)
            r = bci._record(a, "visioncore/sample.py", i + 1)
            r["zones"] = {z: ["token" + str(i)] * 4 for z in bci.ZONES}
            recs[a] = r
        companions = {"schema": "vqa.hb-companions/v1", "books": {}}
        files, _ = bci.build_files(recs, companions, {}, {}, cap=4096)
        shards = json.loads(files["shards.json"])
        self.assertTrue(shards)
        def read_map(rel):
            if rel in shards:
                return {k: v for child in shards[rel] for k, v in read_map(child).items()}
            return json.loads(files[rel])
        inline = read_map("cols.json")["shards"]
        self.assertTrue(inline)
        self.assertEqual(inline, {k: v for k, v in shards.items() if not k.startswith("cols")})
        self.assertLessEqual(max(map(len, files.values())), 4096)
        first = next(iter(recs))
        recs[first]["doc"] = "x" * 5000
        with self.assertRaises(bci.BuildError) as e:
            bci.build_files(recs, companions, {}, {}, cap=4096)
        self.assertEqual(e.exception.rc, 6)
        self.assertEqual(rc._pack(dict(code="OK", rc=0, r=[["x" * 3000]], overflow=0))["overflow"], 1)
        self.assertEqual(rc._pack(dict(code="OK", rc=0, r=[], anchor="x" * 3000))["rc"], 6)


    # @anchor:va.test_route_code_unit.test_adjacent_anchor_cannot_cover_another_function [HB-VC-a0-arch]
    def test_adjacent_anchor_cannot_cover_another_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = self._fixture(root)
            p.write_text('def tagged(): pass  # @anchor:vc.sample.tagged\ndef missing(): pass\n')
            self.assertEqual([r["symbol"] for r in bci.check_public_anchors(root)], ["missing"])
            p.write_text(p.read_text().replace('def missing', '# @anchor:vc.sample.missing\ndef missing'))
            self.assertEqual(bci.check_public_anchors(root), [])

    # @anchor:va.test_route_code_unit.test_stage_priority_graph_edges_and_book_lookup [HB-VC-a0-arch]
    def test_stage_priority_graph_edges_and_book_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = self._fixture(root)
            book = root / "hyperbooks/HB-VC-s1-desc"
            book.mkdir(parents=True)
            section = dict(schema="hb.section.v2", anchor="hb-s1", title="Spectral zebra", purpose="zebra classification",
                           code_anchors=["visioncore/sample_mod.py::anchored_fn"])
            (book / "book.md").write_text(bci.canon(section))
            graph = dict(etypes=["CALLS", "IMPORTS"],
                         ids=["visioncore/sample_mod.py::anchored_fn", "visioncore/sample_mod.py::other"],
                         fwd=dict(row_ptr=[0, 1, 1], col=[1], etype=[0]))
            (book / "graph.json").write_text(bci.canon(graph))
            index = bci.build_in_memory(root)
            a = index["records"]["vc.sample.anchored_fn"]
            self.assertEqual(a["stage"], "S2")  # Explicit source stage outranks S1 companion.
            self.assertIn("vc.sample.other", a["callees"])  # Primary graph, absent from AST body.
            self.assertIn("vc.sample.other", a["callers"])  # Inverse fallback AST call.
            self.assertEqual(rc.lookup_anchor("hb:HB-VC-s1-desc#hb-s1", root)["rc"], 0)
            self.assertEqual(rc.route("hb:HB-VC-s1-desc#hb-s1", root=root)["r"][0][3], 1000000)
            p.write_text(p.read_text().replace('{"stage":"S2"}', '{"book":"HB-VC-s0-grid"}'))
            self.assertEqual(bci.build_in_memory(root)["records"][a["anchor"]]["stage"], "S0")
            p.write_text(p.read_text().replace('# @module {"book":"HB-VC-s0-grid"}\n', ''))
            self.assertEqual(bci.build_in_memory(root)["records"][a["anchor"]]["stage"], "S1")
            graph["fwd"]["etype"] = [1]
            (book / "graph.json").write_text(bci.canon(graph))
            self.assertNotIn("vc.sample.other", bci.build_in_memory(root)["records"][a["anchor"]]["callees"])

    # @anchor:va.test_route_code_unit.test_index_verification_and_publish_rollback [HB-VC-a0-arch]
    def test_index_verification_and_publish_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            source = self._fixture(root)
            out = Path(tmp) / "index"
            bci.build(root, out)
            args = ["--root", str(root), "--out", str(out), "--check", "--verify-index"]
            self.assertEqual(self._call(bci.main, args)[0], 0)
            identity = (out / "ID").read_bytes()
            (out / "ID").write_bytes(b"tampered\n")
            self.assertEqual(self._call(bci.main, args)[0], 5)
            (out / "ID").write_bytes(identity)
            real_replace = bci.os.replace
            def fail_publish(src, dst):
                if str(src) == str(out) + ".tmp":
                    raise OSError("injected publish failure")
                return real_replace(src, dst)
            # Both import paths refer to the stdlib os module, so patch affects the CLI loader too.
            with patch.object(bci.os, "replace", side_effect=fail_publish):
                self.assertEqual(self._call(bci.main, ["--root", str(root), "--out", str(out)])[0], 2)
            self.assertEqual((out / "ID").read_bytes(), identity)
            self.assertEqual(self._call(bci.main, args)[0], 0)
            source.unlink()
            self.assertEqual(self._call(rc.main, ["--root", str(root), "--index", str(out), "--stage", "S2"])[0], 4)
            self.assertEqual(bci.build(root, out)["rc"], 0)
            self.assertEqual(rc.list_stages("S2", root, out)["rc"], 3)


    # @anchor:va.test_route_code_unit.test_malformed_corpus_and_absent_root_are_schema_refusals [HB-VC-a0-arch]
    def test_malformed_corpus_and_absent_root_are_schema_refusals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = self._fixture(root)
            for main in (bci.main, rc.main):
                self.assertEqual(self._call(main, ["--root", str(root / "absent"), "--check"])[0], 2)
                p.write_text("def invalid(:\n")
                self.assertEqual(self._call(main, ["--root", tmp, "--check"])[0], 2)
                self._fixture(root)
                self.assertEqual(self._call(main, ["--root", tmp, "--check"])[0], 0)


    # @anchor:va.test_route_code_unit.test_rb_fixture_fails_before_backfill_and_passes_after [HB-VC-a0-arch]
    def test_rb_fixture_fails_before_backfill_and_passes_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "vadapt/cu_fixture.py"
            path.parent.mkdir()
            before = ('# @module {"book":"HB-VC-s2-change","stage":"S2"}\n'
                      '# @anchor:va.cu_fixture.describe_box\n'
                      'def describe_box(): return 1\n'
                      'def export_marks(): return describe_box()\n'
                      'def _internal_helper(): return None\n')
            path.write_text(before)
            args = ["--root", tmp, "--check", "--scope", "vadapt/cu_fixture.py"]
            status, payload = self._call(rc.main, args)
            self.assertEqual(status, 5)
            self.assertEqual(payload["code"], "E_MISSING_ANCHORS")
            self.assertEqual(payload["missing_count"], 1)
            self.assertEqual(payload["missing"][0]["symbol"], "export_marks")
            self.assertEqual(path.read_text(), before)
            after = before.replace('def export_marks',
                                   '# @anchor:va.cu_fixture.export_marks\ndef export_marks')
            self.assertEqual(ast.dump(ast.parse(before)), ast.dump(ast.parse(after)))
            path.write_text(after)
            status, payload = self._call(rc.main, args)
            self.assertEqual(status, 0)
            self.assertEqual(payload["code"], "OK")
            self.assertEqual(payload["missing"], [])
            self.assertEqual(payload["missing_count"], 0)

    # @anchor:va.test_route_code_unit.test_rb_cu_and_rt_scope_full_coverage_and_stage [HB-VC-a0-arch]
    def test_rb_cu_and_rt_scope_full_coverage_and_stage(self):
        cu = ["vadapt/cu_describe.py", "vadapt/cu_marks.py", "vadapt/cu_ocr.py",
              "vnative/adapters/cu_regions.py"]
        scope = cu + ["vnative/adapters/__init__.py", "tools/build_code_index.py",
                      "tools/route_code.py"]
        args = ["--root", str(ROOT), "--check"]
        for path in scope:
            args.extend(["--scope", path])
        status, payload = self._call(rc.main, args)
        self.assertEqual(status, 0)
        self.assertEqual(payload["code"], "OK")
        self.assertEqual(payload["missing_count"], 0)
        self.assertEqual(payload["missing"], [])
        records = bci.scan_code_and_ast(ROOT, {})
        for path in cu:
            with self.subTest(path=path):
                self.assertEqual(bci.check_public_anchors(ROOT, [path]), [])
                symbols = [r for r in records.values() if r["path"] == path]
                self.assertTrue(symbols)
                self.assertEqual({r["book"] for r in symbols}, {"HB-VC-s2-change"})
                self.assertEqual({r["stage"] for r in symbols}, {"S2"})
        adapter = records["va.adapters.analyze"]
        self.assertEqual((adapter["book"], adapter["stage"], adapter["symbol"]),
                         ("HB-VC-a0-arch", "ADAPT", "analyze"))

    # @anchor:va.test_route_code_unit.test_rb_sealed_vnative_and_hyperbooks_untouched [HB-VC-a0-arch]
    def test_rb_sealed_vnative_and_hyperbooks_untouched(self):
        pins = {
            "__init__.py": (113, "43228c8aeb0bbbd0"),
            "backend.py": (3873, "db83fd65a2c03196"),
            "dual.py": (1885, "db4baf2a74fad176"),
            "run.py": (3194, "ef5a5a41f43776ef"),
        }
        book = (ROOT / "hyperbooks/HB-VN-s0s1/book.md").read_text()
        for name, expected in pins.items():
            with self.subTest(path=name):
                raw = (ROOT / "vnative" / name).read_bytes()
                self.assertIn(expected[1], book)
                self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()[:16]), expected)
                # Same-size mutation must fail the seal predicate; size alone cannot pass.
                mutated = bytes([raw[0] ^ 1]) + raw[1:]
                self.assertNotEqual((len(mutated), hashlib.sha256(mutated).hexdigest()[:16]),
                                    expected)


if __name__ == "__main__":
    unittest.main()
