"""Exercise the resource inventory against synthetic procfs and cgroup snapshots."""

from contextlib import redirect_stderr, redirect_stdout
from functools import partial
from io import StringIO
import json
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parents[1]))
from baseline import inventory


class BaselineTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(dir=TEST_DIR)
        self.addCleanup(scratch.cleanup)
        self.proc_root = Path(scratch.name) / "proc"
        self.cgroup_root = Path(scratch.name) / "cgroup"
        self.collect = partial(inventory.collect, proc_root=self.proc_root, cgroup_root=self.cgroup_root)
        self.uid = os.getuid()
        self.slice = f"/user.slice/user-{self.uid}.slice"
        self.alpha = f"{self.slice}/alpha.scope"
        self.beta = f"{self.slice}/beta.scope"
        self.scopes = [("beta", self.beta), ("alpha", self.alpha)]
        snapshot = json.loads((TEST_DIR / "recorded_inventory.json").read_text())
        for name, root in (("proc", self.proc_root), ("cgroup", self.cgroup_root)):
            for key, contents in snapshot[name].items():
                path = root / key.replace("@SLICE@", self.slice).lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    contents.replace("@SLICE@", self.slice).replace("@UID@", str(self.uid)),
                    encoding="utf-8",
                )

    def assert_cgroup_only_charge(self, report):
        self.assertEqual(report["charged_bytes"], 8192 + 7168)
        self.assertEqual([row["current_bytes"] for row in report["scopes"]], [8192, 7168])
        self.assertEqual(
            [[process["pss_bytes"] for process in row["processes"]] for row in report["scopes"]],
            [[2048, 1024], [3072]],
        )

    def assert_gpu_unknown(self, report):
        self.assertEqual(set(report["gpu"]), {"status"})
        self.assertIn(report["gpu"]["status"], {"unsupported", "unaccounted", "AUSENTE"})

    def test_every_admitted_scope_is_covered_in_reproducible_order(self):
        report = self.collect(self.scopes)
        self.assertIs(report["complete"], True)
        self.assertEqual(report, self.collect(list(reversed(self.scopes))))
        self.assertEqual(
            [(row["owner"], row["cgroup"]) for row in report["scopes"]],
            [("alpha", self.alpha), ("beta", self.beta)],
        )
        self.assertEqual(
            [[process["pid"] for process in row["processes"]] for row in report["scopes"]],
            [[101, 102], [201]],
        )

    def test_cgroup_charge_excludes_pss_and_descendants(self):
        self.assert_cgroup_only_charge(self.collect(self.scopes))

    def test_unknown_gpu_is_a_status(self):
        self.assert_gpu_unknown(self.collect(self.scopes))

    def test_partial_sample_is_refused(self):
        current = self.cgroup_root / self.beta.lstrip("/") / "memory.current"
        for path in (current, self.proc_root / "201/smaps_rollup"):
            with self.subTest(path=path.name):
                contents = path.read_bytes()
                path.unlink()
                with self.assertRaises(inventory.IncompleteInventory):
                    self.collect(self.scopes)
                path.write_bytes(contents)

    def test_oracle_rejects_sum_pss_and_cgroup_mutant(self):
        def sum_pss_and_cgroup(scopes):
            pss = sum(process["pss_bytes"] for row in scopes for process in row["processes"])
            return sum(row["current_bytes"] for row in scopes) + pss

        with patch.object(inventory, "_charged_bytes", sum_pss_and_cgroup):
            with self.assertRaises(AssertionError):
                self.assert_cgroup_only_charge(self.collect(self.scopes))

    def test_oracle_rejects_default_gpu_zero_mutant(self):
        with patch.object(inventory, "_GPU_STATUS", 0):
            with self.assertRaises(AssertionError):
                self.assert_gpu_unknown(self.collect(self.scopes))

    def test_duplicate_and_nested_charges_are_refused_before_sampling(self):
        for cgroup in (self.alpha, f"{self.alpha}/worker"):
            with self.subTest(cgroup=cgroup):
                with patch.object(inventory, "_sample_scope") as sample:
                    with self.assertRaises(inventory.IncompleteInventory):
                        self.collect([("one", self.alpha), ("two", cgroup)])
                    sample.assert_not_called()

    def test_foreign_cgroup_is_refused_before_sampling(self):
        with patch.object(inventory, "_sample_scope") as sample:
            with self.assertRaises(inventory.IncompleteInventory):
                self.collect([("foreign", f"/user.slice/user-{self.uid + 1}.slice")])
            sample.assert_not_called()

    def test_foreign_process_is_refused_before_memory_read(self):
        foreign_uid = str(self.uid + 1)
        path = self.proc_root / "201/status"
        path.write_text("Uid:\t" + "\t".join([foreign_uid] * 4) + "\n", encoding="utf-8")
        with patch.object(inventory, "_read_process_file", wraps=inventory._read_process_file) as read:
            with self.assertRaisesRegex(inventory.IncompleteInventory, "ownership of process 201"):
                self.collect([("beta", self.beta)])
        self.assertEqual([call.args[1] for call in read.call_args_list], ["status"])

    def test_changed_cgroup_membership_refuses_sample(self):
        (self.proc_root / "102/cgroup").write_text(f"0::{self.beta}\n", encoding="utf-8")
        with self.assertRaisesRegex(inventory.IncompleteInventory, "process 102 changed cgroups"):
            self.collect(self.scopes)

    def test_membership_churn_refuses_sample(self):
        first = {101: self.alpha, 102: f"{self.alpha}/worker"}
        with patch.object(inventory, "_members", side_effect=[first, {101: self.alpha}]):
            with self.assertRaisesRegex(inventory.IncompleteInventory, "changed during sampling"):
                self.collect([("alpha", self.alpha)])

    def test_cli_prints_one_json_inventory_from_caller_scope(self):
        stdout = StringIO()
        with (
            patch.object(inventory, "collect", wraps=self.collect) as collect,
            patch.object(sys, "argv", ["baseline", "--json"]),
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as exit_code,
        ):
            runpy.run_module("baseline", run_name="__main__", alter_sys=True)
        self.assertEqual(exit_code.exception.code, 0)
        collect.assert_called_once_with(None)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        report = json.loads(stdout.getvalue())
        self.assertEqual(
            [(row["owner"], row["cgroup"]) for row in report["scopes"]],
            [("caller", self.alpha)],
        )
        self.assertEqual(report["charged_bytes"], 8192)

    def test_cli_partial_sample_has_no_stdout_and_exits_nonzero(self):
        (self.cgroup_root / self.beta.lstrip("/") / "memory.current").unlink()
        stdout, stderr = StringIO(), StringIO()
        with (
            patch.object(inventory, "collect", wraps=self.collect),
            patch.object(sys, "argv", ["baseline", "--json", "--scope", f"beta={self.beta}"]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_code,
        ):
            runpy.run_module("baseline", run_name="__main__", alter_sys=True)
        self.assertEqual(exit_code.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("incomplete inventory", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
