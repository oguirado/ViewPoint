"""F0-U6 positive cases, exact-rc controls and killed implementation mutants.

Run with python3 -I -B -m unittest discover -s tests -p 'test_f0u6_*.py'.
Scratch core fixtures exercise U6 in isolation; they never establish U1 evidence.
"""
from asset_factory import proc as af_proc

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import docs_ratio, gates


CORE_FIXTURE = '''import hashlib, json
def canonical_json(obj):
    def check(value):
        if isinstance(value, float):
            raise ValueError("non_integer_number")
        if isinstance(value, dict):
            for item in value.values(): check(item)
        if isinstance(value, list):
            for item in value: check(item)
    check(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False) + "\\n"
def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def worst_rc(values):
    values = list(values)
    if any(type(rc) is not int or rc not in (0,2,3,4,5,6) for rc in values): return 2
    if 2 in values: return 2
    if 5 in values: return 5
    if not values or any(rc in (3,4,6) for rc in values): return 3
    return 0
'''
TEST_CORE = types.ModuleType("scratch_core")
exec(CORE_FIXTURE, TEST_CORE.__dict__)


@contextlib.contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def passing_legs():
    return [gates._leg(name, gates.LEGS.get(name, []), 0, 1, evidence={"synthetic_fixture": True})
            for name in gates.REQUIRED]


class U6Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="af-u6-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        # Integration uses the real U1 core; isolation uses an explicit test fixture.
        patch = mock.patch.object(gates, "core", gates.core or TEST_CORE)
        patch.start()
        self.addCleanup(patch.stop)

    def component(self, *, scripts=False):
        repo = self.base / "repo"
        root = repo / "components" / "asset-factory"
        root.mkdir(parents=True, exist_ok=True)
        license_data = (ROOT / "LICENSE").read_bytes()
        (repo / "LICENSE").write_bytes(license_data)
        (root / "LICENSE").write_bytes(license_data)
        (root / "NOTICE").write_text(gates.NOTICE_TEXT, encoding="utf-8")
        (root / "README.md").write_text("AGPL-3.0-only F0-skeleton\n", encoding="utf-8")
        (root / "fixture.py").write_text("# fixture\n" * 4, encoding="utf-8")
        if scripts:
            (root / "tools").mkdir()
            for name in ("gates.py", "docs_ratio.py"):
                shutil.copyfile(ROOT / "tools" / name, root / "tools" / name)
            (root / "asset_factory").mkdir()
            shutil.copyfile(ROOT / "asset_factory" / "proc.py", root / "asset_factory" / "proc.py")
            shutil.copyfile(ROOT / "asset_factory" / "proc_pidns.py", root / "asset_factory" / "proc_pidns.py")
            (root / "asset_factory" / "__init__.py").write_text("", encoding="utf-8")
            (root / "asset_factory" / "core.py").write_text(CORE_FIXTURE, encoding="utf-8")
            (root / "tests").mkdir()
            filenames = ["test_f0u%d_fixture.py" % number for number in range(1, 7)]
            filenames.append("test_w3_contracts_fixture.py")
            filenames.append("test_safety_proc_fixture.py")
            for number, filename in enumerate(filenames, 1):
                body = ("import unittest\nclass Fixture(unittest.TestCase):\n"
                        "    def test_fixture(self):\n        self.assertEqual(1, 1)\n")
                if number == 6:
                    body = ("import unittest\nfrom tools.gates import _self_test\n"
                            "class Fixture(unittest.TestCase):\n"
                            "    def test_fixture(self):\n"
                            "        self.assertEqual(_self_test()['rc'], 0)\n")
                (root / "tests" / filename).write_text(body)
        return root

    def ratio_fixture(self):
        root = self.base / "ratio"
        root.mkdir()
        (root / "a.py").write_bytes(b"line\nline\n")
        (root / "a.md").write_bytes(b"doc\n")
        return root

    def unit_argv(self, kind="pass"):
        bodies = {
            "pass": "import unittest; unittest.main()",
            "fail": "import unittest; unittest.main()",
            "skip": "import unittest; unittest.main()",
        }
        if kind == "empty":
            return [sys.executable, "-I", "-B", "-c", bodies["pass"]]
        line = {"pass": "self.assertEqual(1, 1)", "fail": "self.fail('planted')",
                "skip": "self.skipTest('planted skip')", "expected": "self.fail('expected')"}[kind]
        decorator = "    @unittest.expectedFailure\n" if kind == "expected" else ""
        code = ("import unittest\nclass C(unittest.TestCase):\n" + decorator +
                "    def test_case(self): " + line + "\nunittest.main()\n")
        return [sys.executable, "-I", "-B", "-c", code]

    def cli(self, root, argv):
        bootstrap = ("import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); "
                     "runpy.run_module('tools.gates',run_name='__main__')")
        return subprocess.run([sys.executable, "-I", "-B", "-c", bootstrap, str(root), *argv],
                              cwd=root, capture_output=True, timeout=30,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def publication(self):
        root = self.component(scripts=True)
        report = gates.aggregate(passing_legs())
        report["source_files"] = gates._snapshot(root, set())
        paths = self.base / "report.json", self.base / "receipt.json"
        return root, report, paths

    def run_fake_whole(self, root, legs=None):
        legs = legs or passing_legs()
        by_name = {leg["name"]: leg for leg in legs}
        with mock.patch.object(gates, "run_leg", side_effect=lambda name, argv, timeout: by_name[name]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            rc = gates.main(["--all", "--root", str(root),
                             "--report", str(self.base / "report.json"),
                             "--receipt", str(self.base / "receipt.json")])
        return rc, json.loads(output.getvalue())


class AggregationTests(U6Case):
    def test_all_required_legs_pass(self):
        report = gates.aggregate(passing_legs())
        self.assertEqual(report["rc"], 0)
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["scope"], "F0-skeleton")
        self.assertEqual(report["checks_evaluated"], len(gates.REQUIRED))

    def test_complete_leg_registry(self):
        names = {"F0-U1", "F0-U2", "F0-U3", "F0-U4", "F0-U5", "F0-U6-selftest",
                 "F0-U6-docs-ratio", "F0-U6-licensing", "F0-stub-scan", "W3-CONTRACTS", "PROCESS-SAFETY"}
        self.assertEqual(set(gates.LEGS), names)
        for number, name in enumerate(gates.REQUIRED[:6], 1):
            self.assertEqual(gates.LEGS[name], ["python3", "-B", "-m", "unittest", "discover",
                                              "-s", "tests", "-p", "test_f0u%d_*.py" % number])

    def test_planted_failing_leg_blocks_whole_gate(self):
        legs = passing_legs()
        legs[0].update(rc=5, status="FAIL", failures=["planted"])
        report = gates.aggregate(legs)
        self.assertEqual(report["rc"], 5)
        self.assertEqual(report["verdict"], "FAIL")

    def test_missing_leg_is_incomplete(self):
        self.assertEqual(gates.aggregate(passing_legs()[1:])["rc"], 3)
        self.assertEqual(gates.aggregate([])["rc"], 3)
        with mock.patch.dict(gates.LEGS, {}, clear=True):
            self.assertEqual(gates.aggregate([])["rc"], 3)

    def test_zero_checks_not_pass(self):
        legs = passing_legs()
        legs[0]["checks_evaluated"] = 0
        self.assertEqual(gates.aggregate(legs)["rc"], 3)

    def test_empty_evidence_cannot_see(self):
        legs = passing_legs()
        legs[0]["evidence"] = {}
        self.assertEqual(gates.aggregate(legs)["rc"], 3)

    def test_pass_with_failures_is_error(self):
        legs = passing_legs()
        legs[0]["failures"] = ["contradictory failure"]
        self.assertEqual(gates.aggregate(legs)["rc"], 2)

    def test_duplicate_and_unknown_leg_error(self):
        legs = passing_legs()
        self.assertEqual(gates.aggregate(legs + [legs[0]])["rc"], 2)
        legs[0]["name"] = "unknown"
        self.assertEqual(gates.aggregate(legs)["rc"], 2)

    def test_invalid_leg_contracts_error(self):
        changes = ({"rc": 1}, {"rc": True}, {"status": "FAIL"}, {"checks_evaluated": True},
                   {"duration_ms": -1}, {"evidence": []}, {"extra": "property"})
        for change in changes:
            with self.subTest(change=change):
                legs = passing_legs()
                legs[0].update(change)
                self.assertEqual(gates.aggregate(legs)["rc"], 2)

    def test_precedence_and_specific_leg_codes(self):
        for codes, expected in (([0, 6], 3), ([4, 0], 3), ([6, 5], 5), ([5, 2], 2),
                                ([2, 6], 2), ([3, 5], 5)):
            with self.subTest(codes=codes):
                legs = passing_legs()
                for leg, rc in zip(legs, codes):
                    leg.update(rc=rc, status=gates.STATUS[rc])
                report = gates.aggregate(legs)
                self.assertEqual(report["rc"], expected)
                self.assertEqual([leg["rc"] for leg in report["legs"][:len(codes)]], codes)

    def test_self_test_kills_fail_and_zero_checks(self):
        self.assertEqual(gates._self_test(), {"rc": 0, "checks_evaluated": 3, "failures": []})


class RunnerTests(U6Case):
    def test_bounded_child_completion(self):
        result = gates.run_leg("F0-U1", self.unit_argv(), 5)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["checks_evaluated"], 1)
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertEqual(result["evidence"]["stderr"]["sha256"], hashlib.sha256(
            result["evidence"]["stderr"]["text"].encode()).hexdigest())

    def test_declared_report_fail_translation(self):
        result = gates.run_leg("F0-U1", self.unit_argv("fail"), 5)
        self.assertEqual(result["evidence"]["child_rc"], 1)
        self.assertEqual(result["rc"], 5)

    def test_report_fail_never_stage_rc1(self):
        self.assertEqual(gates.run_leg("F0-U1", self.unit_argv("fail"), 5)["rc"], 5)

    def test_unexplained_child_rc1_rejected(self):
        argv = [sys.executable, "-I", "-c", "raise SystemExit(1)"]
        self.assertEqual(gates.run_leg("F0-U1", argv, 5)["rc"], 2)
        self.assertEqual(gates.run_leg("F0-U6-docs-ratio", argv, 5)["rc"], 2)

    def test_zero_tests_is_incomplete(self):
        self.assertEqual(gates.run_leg("F0-U1", self.unit_argv("empty"), 5)["rc"], 3)

    def test_skipped_leg_not_pass(self):
        result = gates.run_leg("F0-U1", self.unit_argv("skip"), 5)
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["checks_evaluated"], 0)

    def test_expected_failure_not_coverage(self):
        result = gates.run_leg("F0-U1", self.unit_argv("expected"), 5)
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["checks_evaluated"], 0)

    def test_unknown_child_exit_is_error(self):
        code = "import sys; print('Ran 1 test in 0.001s'); sys.exit(7)"
        result = gates.run_leg("F0-U1", [sys.executable, "-I", "-c", code], 5)
        self.assertEqual(result["rc"], 2)

    def test_ambiguous_summary_is_error(self):
        code = "print('Ran 1 test in 0.001s\\nRan 2 tests in 0.002s')"
        self.assertEqual(gates.run_leg("F0-U1", [sys.executable, "-I", "-c", code], 5)["rc"], 2)

    def test_invalid_runner_arguments(self):
        for timeout in (0, -1, True, 301):
            self.assertEqual(gates.run_leg("F0-U1", self.unit_argv(), timeout)["rc"], 2)
        self.assertEqual(gates.run_leg("unknown", ["true"], 5)["rc"], 2)
        self.assertEqual(gates.run_leg("F0-U1", ["/nonexistent-af-u6-command"], 5)["rc"], 2)

    def test_output_budget_exceeded(self):
        argv = [sys.executable, "-I", "-c", "print('x' * 1000)"]
        with mock.patch.object(gates, "MAX_OUTPUT_BYTES", 100):
            self.assertEqual(gates.run_leg("F0-U1", argv, 5)["rc"], 6)

    def test_child_timeout_is_budget_exceeded(self):
        argv = [sys.executable, "-I", "-c", "import time; time.sleep(30)"]
        result = gates.run_leg("F0-U1", argv, 1)
        self.assertEqual(result["rc"], 6)
        self.assertEqual(result["failures"], ["deadline_exceeded"])

    def test_timeout_kills_owned_descendant(self):
        pid_path = self.base / "pid"
        code = ("import subprocess,sys,time; from pathlib import Path; "
                "p=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)']); "
                "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)")
        result = gates.run_leg("F0-U1", [sys.executable, "-I", "-c", code, str(pid_path)], 1)
        self.assertEqual(result["rc"], 6)
        pid = int(pid_path.read_text())
        self.addCleanup(self.kill_if_live, pid)
        deadline = time.monotonic() + 2
        live = True
        while time.monotonic() < deadline:
            state = Path("/proc") / str(pid) / "stat"
            try:
                live = state.read_text().split(")", 1)[1].strip().split()[0] != "Z"
            except FileNotFoundError:
                live = False
            if not live:
                break
            time.sleep(0.02)
        self.assertFalse(live, "owned descendant survived timeout")

    @staticmethod
    def kill_if_live(pid):
        path = Path(f'/proc/{pid}/stat')
        if path.exists():
            assert path.read_text().rsplit(')', 1)[1].split()[0] in ('Z', 'X'), 'owned descendant survived cleanup'

    def test_json_leg_contract_and_exit_agreement(self):
        for text, exitcode, expected in (
                ('{"rc":0,"checks_evaluated":1,"failures":[]}', 0, 0),
                ('{"rc":0,"checks_evaluated":0,"failures":[]}', 0, 3),
                ('{"rc":0,"checks_evaluated":1,"failures":[]}', 5, 2),
                ('{"rc":true,"checks_evaluated":1,"failures":[]}', 1, 2),
                ('{"rc":0,"rc":0,"checks_evaluated":1,"failures":[]}', 0, 2),
                ('{"rc":0,"checks_evaluated":1.0,"failures":[]}', 0, 2)):
            code = "import sys; print(sys.argv[1]); sys.exit(int(sys.argv[2]))"
            result = gates.run_leg("F0-U6-licensing",
                                   [sys.executable, "-I", "-c", code, text, str(exitcode)], 5)
            self.assertEqual(result["rc"], expected)


class RatioTests(U6Case):
    def test_ratio_equal_boundary_passes(self):
        result = docs_ratio.measure_docs_ratio(self.ratio_fixture())
        self.assertEqual(result["rc"], 0)
        self.assertEqual((result["markdown_lines"], result["code_schema_lines"]), (1, 2))
        self.assertEqual(result["schema"], "af.docs-ratio/v1")

    def test_planted_markdown_bundle_fails(self):
        root = self.ratio_fixture()
        (root / "bundle.md").write_bytes(b"planted\n" * 3)
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 5)

    def test_untracked_markdown_counted(self):
        root = self.ratio_fixture()
        (root / "UNTRACKED.markdown").write_bytes(b"extra\n")
        result = docs_ratio.measure_docs_ratio(root)
        self.assertEqual(result["markdown_lines"], 2)
        self.assertEqual(result["rc"], 5)

    def test_empty_denominator_incomplete(self):
        self.assertEqual(docs_ratio.measure_docs_ratio(self.base)["rc"], 3)

    def test_sorted_ratio_inventory(self):
        root = self.ratio_fixture()
        (root / "z.json").write_text("{}\n")
        first = docs_ratio.measure_docs_ratio(root)
        self.assertEqual(first, docs_ratio.measure_docs_ratio(root))
        self.assertEqual([entry["path"] for entry in first["files"]], ["a.md", "a.py", "z.json"])
        for entry in first["files"]:
            self.assertEqual(entry["sha256"], hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest())

    def test_denominator_allowlist_and_physical_lines(self):
        root = self.ratio_fixture()
        (root / "b.sh").write_bytes(b"\nlast")
        (root / "b.schema.json").write_bytes(b"{}\n")
        (root / "b.json").write_bytes(b"padding\n" * 100)
        result = docs_ratio.measure_docs_ratio(root)
        self.assertEqual(result["code_schema_lines"], 5)
        (root / "a.md").write_bytes(b"\n\nlast")
        result = docs_ratio.measure_docs_ratio(root)
        self.assertEqual(result["markdown_lines"], 3)
        self.assertEqual(result["rc"], 5)

    def test_escape_symlink_refused(self):
        root = self.ratio_fixture()
        other = self.base / "ratio-other"
        other.mkdir()
        (other / "code.py").write_text("outside\n")
        (root / "escape.py").symlink_to(other / "code.py")
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 2)

    def test_directory_escape_and_broken_link_refused(self):
        root = self.ratio_fixture()
        (root / "directory").symlink_to(self.base, target_is_directory=True)
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 2)
        (root / "directory").unlink()
        (root / "broken.py").symlink_to(root / "absent")
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 2)

    def test_internal_alias_does_not_inflate_credit(self):
        root = self.ratio_fixture()
        (root / "alias.py").symlink_to(root / "a.py")
        (root / "loop").symlink_to(root, target_is_directory=True)
        result = docs_ratio.measure_docs_ratio(root)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["code_schema_lines"], 2)

    def test_binary_code_padding_refused(self):
        root = self.ratio_fixture()
        (root / "padding.py").write_bytes(b"\x00\n")
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 2)

    def test_png_asset_is_bound_without_line_credit(self):
        import zlib
        def chunk(kind, body):
            return (len(body).to_bytes(4, "big") + kind + body +
                    zlib.crc32(kind + body).to_bytes(4, "big"))
        root = self.ratio_fixture()
        baseline = docs_ratio.measure_docs_ratio(root)
        image = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", b"\x00\x00\x00\x01" * 2 +
                 b"\x08\x02\x00\x00\x00") + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) +
                 chunk(b"IEND", b""))
        asset = root / "reference.png"
        asset.write_bytes(image)
        measured = docs_ratio.measure_docs_ratio(root)
        self.assertEqual(measured["rc"], 0)
        for key in ("markdown_lines", "code_schema_lines"):
            self.assertEqual(measured[key], baseline[key])
        self.assertIn({"path": "reference.png", "class": "other", "lines": 0,
                       "sha256": hashlib.sha256(image).hexdigest()}, measured["files"])
        asset.write_bytes(image[:-1] + bytes([image[-1] ^ 1]))
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 2)
        asset.write_bytes(image + b"hidden trailing bytes")
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 2)
        asset.unlink()
        (root / "padding.bin").write_bytes(b"\x00\n")
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 2)

    def test_non_utf8_counted_text_refused(self):
        root = self.ratio_fixture()
        (root / "a.md").write_bytes(b"\xff")
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 2)

    def test_ratio_budgets_and_missing_root(self):
        root = self.ratio_fixture()
        with mock.patch.object(docs_ratio, "MAX_BYTES", 1):
            self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 6)
        with mock.patch.object(docs_ratio, "MAX_FILES", 1):
            self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 6)
        self.assertEqual(docs_ratio.measure_docs_ratio(root / "missing")["rc"], 2)

    def test_docs_cli_and_usage_rc(self):
        root = self.ratio_fixture()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(docs_ratio.main(["--root", str(root)]), 0)
        self.assertEqual(json.loads(out.getvalue())["rc"], 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(docs_ratio.main([]), 2)


class LicensingTests(U6Case):
    def test_agpl_text_and_verified_notice(self):
        root = self.component()
        self.assertEqual(gates._licensing(root)["rc"], 0)
        self.assertEqual((ROOT / "NOTICE").read_text(), gates.NOTICE_TEXT)
        self.assertEqual((ROOT / "LICENSE").read_bytes(), (ROOT.parents[1] / "LICENSE").read_bytes())

    def test_mutated_license_rejected(self):
        root = self.component()
        with (root / "LICENSE").open("ab") as stream:
            stream.write(b"planted mutation\n")
        self.assertEqual(gates._licensing(root)["rc"], 5)

    def test_missing_spdx_rejected(self):
        root = self.component()
        (root / "README.md").write_text("no election\n")
        self.assertEqual(gates._licensing(root)["rc"], 5)

    def test_unverified_notice_claim_rejected(self):
        root = self.component()
        with (root / "NOTICE").open("a") as stream:
            stream.write("Unverified vendor ownership claim\n")
        result = gates._licensing(root)
        self.assertEqual(result["rc"], 5)
        self.assertEqual(result["failures"], ["notice_provenance_unverified"])

    def test_missing_licensing_file_cannot_see(self):
        root = self.component()
        (root / "NOTICE").unlink()
        self.assertEqual(gates._licensing(root)["rc"], 3)

    def test_stub_scan_negative_control(self):
        root = self.component()
        self.assertEqual(gates._stub_scan(root)["rc"], 0)
        (root / "_afstub_planted.py").write_text("# forbidden\n")
        self.assertEqual(gates._stub_scan(root)["rc"], 5)


class PublicationTests(U6Case):
    def test_valid_hash_bound_receipts(self):
        root, report, paths = self.publication()
        self.assertEqual(gates._publish(report, *paths, root=self.base / "repo/components/asset-factory"), 0)
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 0)
        receipt = json.loads(paths[1].read_bytes())
        self.assertEqual(receipt["schema"], "af.gate-receipt/v1")
        self.assertEqual(receipt["scope"], "F0-skeleton")
        self.assertEqual(receipt["report"]["sha256"], hashlib.sha256(paths[0].read_bytes()).hexdigest())
        self.assertEqual(receipt["source_files"], report["source_files"])
        self.assertEqual(len(receipt["tool_files"]), 2)
        self.assertEqual(len(receipt["evidence_files"]), 8)
        self.assertEqual(paths[0].read_bytes(), gates.core.canonical_json(report).encode())

    def test_atomic_receipt_pair_validates(self):
        _, report, paths = self.publication()
        for path in paths:
            path.write_bytes(b"old caller bytes")
        replace = gates.os.replace
        with mock.patch.object(gates.os, "replace", wraps=replace) as calls, \
                mock.patch.object(gates.os, "fsync", wraps=gates.os.fsync) as sync:
            self.assertEqual(gates._publish(report, *paths, root=self.base / "repo/components/asset-factory"), 0)
        self.assertEqual([Path(call.args[1]) for call in calls.call_args_list], list(paths))
        self.assertGreaterEqual(sync.call_count, 4)
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 0)

    def test_missing_receipt_not_pass(self):
        _, report, paths = self.publication()
        gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        paths[1].unlink()
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 3)

    def test_tampered_report_hash(self):
        _, report, paths = self.publication()
        gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        with paths[0].open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 4)

    def test_tampered_bound_source(self):
        root, report, paths = self.publication()
        gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        (root / "fixture.py").write_text("# drift\n")
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 4)

    def test_unknown_receipt_schema(self):
        _, report, paths = self.publication()
        gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        receipt = json.loads(paths[1].read_bytes())
        receipt["schema"] = "vqa.receipt/v1"
        paths[1].write_text(json.dumps(receipt))
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 2)

    def test_receipt_malformed_duplicate_float_and_scope(self):
        _, report, paths = self.publication()
        gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        original = paths[1].read_bytes()
        for change in ({"scope": "appearance"}, {"extra": 1}, {"source_files": []}):
            receipt = json.loads(original)
            receipt.update(change)
            paths[1].write_text(json.dumps(receipt))
            self.assertIn(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), (2, 4))
        for data in (b'{"schema":1,"schema":2}', b'{"schema":NaN}', b'{"schema":1.0}'):
            paths[1].write_bytes(data)
            self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 2)

    def test_invalid_input_preserves_existing_outputs(self):
        _, report, paths = self.publication()
        for path in paths:
            path.write_bytes(b"caller-owned\n")
        report["scope"] = "appearance"
        with self.assertRaises(ValueError):
            gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        self.assertEqual([path.read_bytes() for path in paths], [b"caller-owned\n"] * 2)

    def test_nonzero_report_preserves_caller_bytes(self):
        _, report, paths = self.publication()
        for path in paths:
            path.write_bytes(b"caller-owned\n")
        legs = passing_legs()
        legs[0].update(rc=5, status="FAIL", failures=["planted"])
        report = gates.aggregate(legs)
        self.assertEqual(gates._publish(report, *paths, root=self.base / "repo/components/asset-factory"), 5)
        self.assertEqual([path.read_bytes() for path in paths], [b"caller-owned\n"] * 2)

    def test_interrupted_publication_never_accepted(self):
        _, report, paths = self.publication()
        for path in paths:
            path.write_bytes(b"caller-owned\n")
        replace = gates.os.replace
        failed = False
        def interrupt(source, target):
            nonlocal failed
            if Path(target) == paths[1] and not failed:
                failed = True
                raise OSError("planted second replacement failure")
            return replace(source, target)
        with mock.patch.object(gates.os, "replace", side_effect=interrupt):
            with self.assertRaises(OSError):
                gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        self.assertEqual([path.read_bytes() for path in paths], [b"caller-owned\n"] * 2)
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 2)
        self.assertEqual(sorted(path.name for path in self.base.iterdir()),
                         ["receipt.json", "repo", "report.json"])

    def test_crash_between_replacements_is_detected(self):
        _, report, paths = self.publication()
        gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        report["legs"][0]["duration_ms"] += 1
        paths[0].write_text(gates.core.canonical_json(report))
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 4)

    def test_output_alias_symlink_and_hardlink_refused(self):
        _, report, paths = self.publication()
        with self.assertRaises(ValueError):
            gates._publish(report, paths[0], paths[0], root=self.base / "repo/components/asset-factory")
        paths[0].write_text("sentinel")
        paths[1].symlink_to(paths[0])
        with self.assertRaises(ValueError):
            gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        paths[1].unlink()
        os.link(paths[0], paths[1])
        with self.assertRaises(ValueError):
            gates._publish(report, *paths, root=self.base / "repo/components/asset-factory")
        self.assertEqual(paths[0].read_text(), "sentinel")

    def test_source_collision_refused(self):
        root, report, paths = self.publication()
        source = root / "fixture.py"
        old = source.read_bytes()
        with self.assertRaises(ValueError):
            gates._publish(report, source, paths[1], {source}, root=root)
        self.assertEqual(source.read_bytes(), old)


class WholeCliTests(U6Case):
    def test_whole_cli_positive_scratch_integration(self):
        root = self.component(scripts=True)
        report, receipt = self.base / "report.json", self.base / "receipt.json"
        result = self.cli(root, ["--all", "--root", str(root),
                                 "--report", str(report), "--receipt", str(receipt)])
        self.assertEqual(result.returncode, 0, result.stderr.decode() + result.stdout.decode())
        payload = json.loads(result.stdout)
        self.assertEqual(payload["rc"], 0)
        self.assertEqual(len(payload["legs"]), 11)
        self.assertEqual(gates._verify_pair(report, receipt, root=root), 0)

    def test_whole_cli_negative_preserves_outputs_and_runs_all(self):
        root = self.component(scripts=True)
        failing = root / "tests" / "test_f0u1_fixture.py"
        failing.write_text(failing.read_text().replace("self.assertEqual(1, 1)", "self.fail('planted')"))
        paths = self.base / "report.json", self.base / "receipt.json"
        for path in paths:
            path.write_bytes(b"preserve me")
        result = self.cli(root, ["--all", "--root", str(root),
                                 "--report", str(paths[0]), "--receipt", str(paths[1])])
        self.assertEqual(result.returncode, 5)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["legs"]), 11)
        self.assertEqual(payload["legs"][1]["rc"], 0)
        self.assertEqual([path.read_bytes() for path in paths], [b"preserve me"] * 2)

    def test_missing_sibling_suite_cannot_see(self):
        root = self.component(scripts=True)
        (root / "tests" / "test_f0u2_fixture.py").unlink()
        result = self.cli(root, ["--all", "--root", str(root),
                                 "--report", str(self.base / "report.json"),
                                 "--receipt", str(self.base / "receipt.json")])
        self.assertEqual(result.returncode, 3)
        self.assertFalse((self.base / "report.json").exists())

    def test_missing_core_cannot_see(self):
        root = self.component(scripts=True)
        shutil.rmtree(root / "asset_factory")
        result = self.cli(root, ["--self-test"])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["reason_codes"], ["kernel_missing"])

    def test_main_self_test_and_usage(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(gates.main(["--self-test"]), 0)
        self.assertEqual(json.loads(output.getvalue())["checks_evaluated"], 3)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(gates.main([]), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gates.main(["--all"]), 2)

    def test_main_detects_source_drift(self):
        root = self.component()
        by_name = {leg["name"]: leg for leg in passing_legs()}
        def changing(name, argv, timeout):
            if name == gates.REQUIRED[-1]:
                (root / "fixture.py").write_text("changed\n")
            return by_name[name]
        with mock.patch.object(gates, "run_leg", side_effect=changing), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            rc = gates.main(["--all", "--root", str(root),
                             "--report", str(self.base / "report.json"),
                             "--receipt", str(self.base / "receipt.json")])
        self.assertEqual(rc, 3)
        self.assertIn("source_drift", json.loads(output.getvalue())["failures"])
        self.assertFalse((self.base / "report.json").exists())

    def test_main_missing_registry_entry_cannot_see(self):
        root = self.component()
        with mock.patch.dict(gates.LEGS, {k: v for k, v in gates.LEGS.items() if k != "F0-U1"}, clear=True):
            rc, report = self.run_fake_whole(root)
        self.assertEqual(rc, 3)
        self.assertEqual(report["legs"][0]["rc"], 3)

    def test_main_refuses_json_source_collision(self):
        root = self.component()
        source = root / "factory.json"
        source.write_text('{"schema":"af.factory-config/v1"}\n')
        previous = source.read_bytes()
        by_name = {leg["name"]: leg for leg in passing_legs()}
        with mock.patch.object(gates, "run_leg", side_effect=lambda name, argv, timeout: by_name[name]), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = gates.main(["--all", "--root", str(root), "--report", str(source),
                             "--receipt", str(self.base / "receipt.json")])
        self.assertEqual(rc, 2)
        self.assertEqual(source.read_bytes(), previous)

    def test_sigterm_cleans_owned_child_before_refusal(self):
        root = self.component(scripts=True)
        marker = root / "owned_pid"
        sleeper = root / "tests" / "test_f0u1_fixture.py"
        sleeper.write_text("import os,time\nfrom pathlib import Path\n"
                           "Path('owned_pid').write_text(str(os.getpid()))\ntime.sleep(30)\n")
        bootstrap = ("import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); "
                     "runpy.run_module('tools.gates',run_name='__main__')")
        argv = [sys.executable, "-I", "-B", "-c", bootstrap, str(root), "--all",
                "--root", str(root), "--report", str(self.base / "report.json"),
                "--receipt", str(self.base / "receipt.json")]
        process = af_proc.popen(argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "test leg did not start")
            pid = int(marker.read_text())
            self.addCleanup(RunnerTests.kill_if_live, pid)
            af_proc.signal_pid(process, signal.SIGTERM)
            stdout, stderr = af_proc.communicate(process, timeout=8)
            self.assertEqual(process.returncode, 6, stderr.decode())
            self.assertEqual(json.loads(stdout)["reason_codes"], ["deadline_exceeded"])
            self.assertFalse(Path(f'/proc/{pid}').exists(), 'owned leg was not reaped')
            self.assertFalse((self.base / "report.json").exists())
        finally:
            if af_proc.poll_exit(process) is None:
                af_proc.signal_pid(process, signal.SIGKILL)
                af_proc.communicate(process, timeout=5)


class MutationProofTests(U6Case):
    def test_mutation_controls_go_red(self):
        # Temporarily compile altered implementations, run the unchanged original
        # assertion, then discard the mutant. Each must yield an assertion failure.
        mutants = [
            ("docs", "0 if 2 * result[\"markdown_lines\"] <= code else 5", "0 if True else 5",
             RatioTests, "test_planted_markdown_bundle_fails"),
            ("docs", "3 if code == 0 else", "0 if code == 0 else",
             RatioTests, "test_empty_denominator_incomplete"),
            ("docs", 'if not resolved.is_relative_to(root):', 'if False:',
             RatioTests, "test_escape_symlink_refused"),
            ("docs", 'if b"\\x00" in data:', 'if False:',
             RatioTests, "test_binary_code_padding_refused"),
            ("docs", 'suffix in (".py", ".sh")', 'suffix in (".py", ".sh", ".json")',
             RatioTests, "test_denominator_allowlist_and_physical_lines"),
            ("gates", 'if item["rc"] == 0 and item["checks_evaluated"] == 0:', 'if False:',
             AggregationTests, "test_zero_checks_not_pass"),
            ("gates", 'elif item["rc"] == 0 and not item["evidence"]:', 'elif False:',
             AggregationTests, "test_empty_evidence_cannot_see"),
            ("gates", '(leg["rc"] == 0 and bool(leg["failures"]))', 'False',
             AggregationTests, "test_pass_with_failures_is_error"),
            ("gates", 'valid.append(_leg(name, LEGS.get(name, []), 3, failures=["missing_leg"]))',
             'valid.append(_leg(name, LEGS.get(name, []), 0, 1))',
             AggregationTests, "test_missing_leg_is_incomplete"),
            ("gates", 'rc = core.worst_rc([leg["rc"] for leg in valid] + ([2] if failures else []))',
             'rc = 0', AggregationTests, "test_planted_failing_leg_blocks_whole_gate"),
            ("gates", 'if leg["name"] in names:', 'if False:',
             AggregationTests, "test_duplicate_and_unknown_leg_error"),
            ("gates", 'rc, failures = 5, ["unittest_failed"]', 'rc, failures = 0, []',
             RunnerTests, "test_declared_report_fail_translation"),
            ("gates", 'rc, failures = 3, ["unittest_coverage_incomplete"]', 'rc, failures = 0, []',
             RunnerTests, "test_zero_tests_is_incomplete"),
            ("gates", 'rc = 3 if count == 0 or skipped or expected else 0', 'rc = 0',
             RunnerTests, "test_skipped_leg_not_pass"),
            ("gates", 'rc, failures = 6, ["deadline_exceeded" if timed_out else "output_over_budget"]',
             'rc, failures = 0, []', RunnerTests, "test_child_timeout_is_budget_exceeded"),
            ("gates", 'if hashlib.sha256(license_bytes).digest() != hashlib.sha256(original).digest():',
             'if False:', LicensingTests, "test_mutated_license_rejected"),
            ("gates", 'if b"AGPL-3.0-only" not in readme:', 'if False:',
             LicensingTests, "test_missing_spdx_rejected"),
            ("gates", 'if notice != NOTICE_TEXT.encode("utf-8"):', 'if False:',
             LicensingTests, "test_unverified_notice_claim_rejected"),
            ("gates", 'return _summary(5 if paths else 0, 1, paths)', 'return _summary(0, 1, [])',
             LicensingTests, "test_stub_scan_negative_control"),
            ("gates", 'receipt["report"]["sha256"] != hashlib.sha256(report_data).hexdigest()', 'False',
             PublicationTests, "test_tampered_report_hash"),
            ("gates", 'if core.sha256_file(path) != ref["sha256"]:', 'if False:',
             PublicationTests, "test_tampered_bound_source"),
            ("gates", 'for path in reversed(replaced):', 'for path in ():',
             PublicationTests, "test_interrupted_publication_never_accepted"),
            ("gates", 'os.fsync(stream.fileno())', 'pass',
             PublicationTests, "test_atomic_receipt_pair_validates"),
            ("gates", 'payload.get("schema") != schema', 'False',
             WholeCliTests, "test_main_refuses_json_source_collision"),
        ]
        for module_name, before, after, case, method in mutants:
            with self.subTest(rule=method):
                original = docs_ratio if module_name == "docs" else gates
                source = Path(original.__file__).read_text(encoding="utf-8")
                self.assertEqual(source.count(before), 1, "mutation anchor drift")
                mutant = types.ModuleType("u6_mutant")
                mutant.__file__ = original.__file__
                exec(compile(source.replace(before, after, 1), original.__file__, "exec"), mutant.__dict__)
                if module_name == "gates":
                    mutant.core = gates.core
                result = unittest.TestResult()
                with mock.patch.object(sys.modules[__name__], "docs_ratio" if module_name == "docs" else "gates", mutant):
                    case(method).run(result)
                self.assertGreater(len(result.failures), 0, "mutant survived unchanged assertion")
                self.assertEqual(result.errors, [], "a test error is not a killed mutant")


if __name__ == "__main__":
    unittest.main()
