"""Adversarial regression controls for publication and complete source binding."""
from asset_factory import proc as af_proc
from signal_fixture import owned_signal_case, signal_current

import contextlib
import io
import json
import os
from pathlib import Path
import signal
from unittest import mock

from test_f0u6_gates import U6Case, passing_legs
from tools import docs_ratio, gates


class RepairTests(U6Case):
    def assert_interrupted_replace_preserves(self, index, existing):
        root = self.component(scripts=True)
        paths = self.base / "report.json", self.base / "receipt.json"
        if existing:
            for path in paths:
                path.write_bytes(b"caller bytes\n")
        replace = os.replace
        injected = False

        def interrupt_after_replace(source, target):
            nonlocal injected
            replace(source, target)
            if Path(target) == paths[index] and not injected:
                injected = True
                signal_current(signal.SIGTERM)

        by_name = {leg["name"]: leg for leg in passing_legs()}
        with mock.patch.object(gates, "run_leg", side_effect=lambda name, *_: by_name[name]), \
                mock.patch.object(gates.os, "replace", side_effect=interrupt_after_replace), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            rc = gates.main(["--all", "--root", str(root),
                             "--report", str(paths[0]), "--receipt", str(paths[1])])
        self.assertTrue(injected)
        self.assertEqual(rc, 6)
        self.assertEqual(json.loads(output.getvalue())["reason_codes"], ["deadline_exceeded"])
        for path in paths:
            if existing:
                self.assertEqual(path.read_bytes(), b"caller bytes\n")
            else:
                self.assertFalse(path.exists(), str(path))

    # A-SAFE: R44 requires the signal sender to own the interrupted child.
    @owned_signal_case
    def test_signal_after_report_replace_preserves_bytes(self):
        self.assert_interrupted_replace_preserves(0, True)

    # A-SAFE: R44 requires an owning controller, never a parent-directed relay.
    @owned_signal_case
    def test_signal_after_receipt_replace_preserves_bytes(self):
        self.assert_interrupted_replace_preserves(1, True)

    # A-SAFE: R44 requires an owning controller, never a parent-directed relay.
    @owned_signal_case
    def test_signal_after_report_replace_removes_new_outputs(self):
        self.assert_interrupted_replace_preserves(0, False)

    # A-SAFE: R44 requires an owning controller, never a parent-directed relay.
    @owned_signal_case
    def test_signal_after_receipt_replace_removes_new_outputs(self):
        self.assert_interrupted_replace_preserves(1, False)

    def test_cli_refuses_retargetable_executable_symlink(self):
        root = self.component(scripts=True)
        tests = root / "tests"
        alias = tests / "test_f0u1_fixture.py"
        alias.unlink()
        passing = tests / "passing_case.py"
        failing = tests / "failing_case.py"
        passing.write_text("import unittest\nfrom pathlib import Path\n"
                           "class Fixture(unittest.TestCase):\n"
                           "    def test_fixture(self):\n"
                           "        alias = Path(__file__)\n"
                           "        alias.unlink()\n"
                           "        alias.symlink_to('failing_case.py')\n"
                           "        self.assertTrue(True)\n")
        failing.write_text("import unittest\nclass Fixture(unittest.TestCase):\n"
                           "    def test_fixture(self):\n        self.fail('planted')\n")
        alias.symlink_to(passing.name)
        paths = self.base / "report.json", self.base / "receipt.json"
        for path in paths:
            path.write_bytes(b"caller bytes\n")
        result = self.cli(root, ["--all", "--root", str(root),
                                 "--report", str(paths[0]), "--receipt", str(paths[1])])
        self.assertEqual(result.returncode, 2, result.stdout.decode() + str(alias.readlink()))
        self.assertEqual(json.loads(result.stdout)["reason_codes"], ["source_symlink"])
        self.assertEqual(alias.readlink(), Path(passing.name))
        self.assertEqual([path.read_bytes() for path in paths], [b"caller bytes\n"] * 2)

    def publish_real_pair(self, *, inside=False):
        root = self.component(scripts=True)
        directory = root if inside else self.base
        paths = directory / "report.json", directory / "receipt.json"
        result = self.cli(root, ["--all", "--root", str(root),
                                 "--report", str(paths[0]), "--receipt", str(paths[1])])
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 0)
        return root, paths

    def test_added_stub_invalidates_real_receipt(self):
        root, paths = self.publish_real_pair()
        (root / "_afstub_review.py").write_text("# forbidden stub\n")
        self.assertEqual(gates._stub_scan(root)["rc"], 5)
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 4)

    def test_added_markdown_invalidates_real_receipt(self):
        root, paths = self.publish_real_pair()
        (root / "bundle.md").write_text("doc\n" * 10000)
        self.assertEqual(docs_ratio.measure_docs_ratio(root)["rc"], 5)
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 4)

    def test_empty_provenance_refuses_publication(self):
        _, report, paths = self.publication()
        report["source_files"] = []
        for path in paths:
            path.write_bytes(b"caller bytes\n")
        self.assertEqual(gates._publish(report, *paths, root=self.base / "repo/components/asset-factory"), 3)
        self.assertEqual([path.read_bytes() for path in paths], [b"caller bytes\n"] * 2)

    def test_omitted_source_refuses_publication(self):
        root, report, paths = self.publication()
        report["source_files"] = [ref for ref in report["source_files"]
                                  if ref["path"] != str(root / "fixture.py")]
        self.assertEqual(gates._publish(report, *paths, root=self.base / "repo/components/asset-factory"), 4)
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())

    def test_empty_tool_provenance_cannot_see(self):
        _, paths = self.publish_real_pair()
        receipt = json.loads(paths[1].read_bytes())
        receipt["tool_files"] = []
        paths[1].write_text(json.dumps(receipt))
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 3)

    def test_empty_test_provenance_cannot_see(self):
        _, paths = self.publish_real_pair()
        receipt = json.loads(paths[1].read_bytes())
        receipt["evidence_files"] = []
        paths[1].write_text(json.dumps(receipt))
        self.assertEqual(gates._verify_pair(*paths, root=self.base / "repo/components/asset-factory"), 3)

    def test_inside_root_pair_verifies_and_republishes(self):
        root, paths = self.publish_real_pair(inside=True)
        result = self.cli(root, ["--all", "--root", str(root),
                                 "--report", str(paths[0]), "--receipt", str(paths[1])])
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertEqual(gates._verify_pair(*paths, root=root), 0)
        self.assertFalse(list(root.glob(".report.json.*")))
        self.assertFalse(list(root.glob(".receipt.json.*")))

    def test_consumer_requires_explicit_root(self):
        _, paths = self.publish_real_pair()
        with self.assertRaises(TypeError):
            gates._verify_pair(*paths)

    def test_receipt_for_different_root_is_stale(self):
        _, paths = self.publish_real_pair()
        other = self.base / "other"
        other.mkdir()
        self.assertEqual(gates._verify_pair(*paths, root=other), 4)

    def test_partial_tool_provenance_is_stale(self):
        root, paths = self.publish_real_pair()
        receipt = json.loads(paths[1].read_bytes())
        receipt["tool_files"].pop()
        paths[1].write_text(json.dumps(receipt))
        self.assertEqual(gates._verify_pair(*paths, root=root), 4)

    def test_partial_test_provenance_is_stale(self):
        root, paths = self.publish_real_pair()
        receipt = json.loads(paths[1].read_bytes())
        receipt["evidence_files"].pop()
        paths[1].write_text(json.dumps(receipt))
        self.assertEqual(gates._verify_pair(*paths, root=root), 4)
