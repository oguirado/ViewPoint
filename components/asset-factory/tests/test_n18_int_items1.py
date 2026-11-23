"""Focused regressions for the INT-ITEMS1 integration fixes."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from asset_factory import core
from tools import gates
from tools.blender import bake
from tools.pipeline import engine
from test_e2e_pipeline import SyntheticStages
import test_f0u6_gates as gate_controls


class BakePriorityTests(unittest.TestCase):
    def test_absolute_worker_priority_with_and_without_observed_scope(self):
        limits = {"memory_max_bytes": 8 * 2**30, "memory_high_bytes": 6 * 2**30,
                  "swap_max_bytes": 0}
        worker = ["blender", "-b"]
        for observed in (True, False):
            for inherited in (0, 10, 15):
                with self.subTest(observed=observed, inherited=inherited):
                    unavailable = None if observed else bake.topology.resources.ObservationUnavailable()
                    with (mock.patch.object(bake.topology.resources, "cgroup_observation",
                                            return_value=limits, side_effect=unavailable),
                          mock.patch.object(os, "nice", return_value=inherited),
                          mock.patch.object(bake.topology, "default_runner",
                                            return_value=(0, "")) as run):
                        self.assertEqual(bake._look_scoped_runner(worker, 10), (0, "", ""))
                    argv, timeout = run.call_args.args
                    self.assertEqual(timeout, 10)
                    if observed:
                        self.assertEqual(argv[:3], ["taskset", "-c", "0-15"])
                    else:
                        self.assertEqual(argv[:3], ["systemd-run", "--user", "--scope"])
                        self.assertIn("MemoryHigh=6G", argv)
                        self.assertIn("MemoryMax=8G", argv)
                        self.assertIn("MemorySwapMax=0", argv)
                        self.assertNotEqual(argv[:len(bake.topology.SCOPE_ARGV)], bake.topology.SCOPE_ARGV)
                    taskset = argv.index("taskset")
                    self.assertEqual(argv[taskset:taskset + 3], ["taskset", "-c", "0-15"])
                    launch = argv[taskset + 3:]
                    if launch == worker:
                        self.assertEqual(inherited, 15)
                        continue
                    self.assertEqual(launch[:4], [sys.executable, "-I", "-B", "-c"])
                    priority = {0: inherited}

                    def set_priority(which, who, value):
                        self.assertEqual((which, who), (os.PRIO_PROCESS, 0))
                        priority[who] = value

                    with (mock.patch.object(sys, "argv", ["-c", *launch[5:]]),
                          mock.patch.object(os, "setpriority", side_effect=set_priority),
                          mock.patch.object(os, "execvp") as exec_worker):
                        exec(launch[4], {})
                    exec_worker.assert_called_once_with(worker[0], worker)
                    self.assertEqual(priority[0], 15)


def comparable_report(report):
    """Normalize transient process labels and timings in a verified copy."""
    gates._validate_report(report)
    report = deepcopy(report)
    for leg in report["legs"]:
        del leg["duration_ms"]
        if leg["name"] not in gates.UNITTEST_LEGS:
            continue
        for key in ("stdout", "stderr"):
            stream = leg["evidence"].get(key)
            if stream is None:
                continue
            original = gates._stream_bytes(stream)
            normalized = re.sub(rb"(?m)^(Ran \d+ tests? in )\d+(?:\.\d+)?s$",
                                rb"\g<1><elapsed>", original)
            if leg["name"] == "PROCESS-SAFETY" and key == "stderr":
                normalized = re.sub(rb"(ResourceWarning: subprocess )\d+(?= is still running$)",
                                    rb"\g<1>?", normalized, flags=re.MULTILINE)
                normalized = re.sub(rb"(ResourceWarning: Implicitly cleaning up <_OwnedScratch '[^'\n]*/af-job-)"
                                    rb"[a-z0-9_]+(?='>$)", rb"\g<1>?", normalized, flags=re.MULTILINE)
            if normalized != original:
                leg["evidence"][key] = gates._stream_evidence(normalized)
    return core.canonical_json(report)


class GateEvidenceTests(gate_controls.U6Case):
    def test_hashes_raw_stdout_and_stderr(self):
        stdout = b"raw stdout \xff\r\n"
        stderr = (b"ResourceWarning: subprocess 71 is still running\n"
                  b"ResourceWarning: Implicitly cleaning up <_OwnedScratch '/tmp/af-job-abcd1234'>\n"
                  b"Ran 1 test in 0.001s\n")
        code = ("import sys; "
                f"sys.stdout.buffer.write({stdout!r}); sys.stderr.buffer.write({stderr!r})")
        leg = gates.run_leg("PROCESS-SAFETY", [sys.executable, "-B", "-c", code], 5)
        self.assertEqual(leg["rc"], 0)
        for key, raw in (("stdout", stdout), ("stderr", stderr)):
            with self.subTest(stream=key):
                evidence = leg["evidence"][key]
                self.assertEqual(evidence["bytes"], len(raw))
                self.assertEqual(evidence["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(evidence["text"], raw.decode(errors="replace"))
                self.assertEqual(gates._stream_bytes(evidence), raw)

    def test_serial_parallel_parity_and_raw_digest_refusal(self):
        root = self.component(scripts=True)
        (root / "tests" / "test_safety_proc_fixture.py").write_text(
            "import os, sys, unittest\n"
            "class Fixture(unittest.TestCase):\n"
            "    def test_fixture(self):\n"
            "        pid = os.getpid()\n"
            "        print(f'ResourceWarning: subprocess {pid} is still running', file=sys.stderr)\n"
            "        print(f\"ResourceWarning: Implicitly cleaning up <_OwnedScratch "
            "'/tmp/af-job-{pid}'>\", file=sys.stderr)\n"
            "        print('unrelated evidence id 17', file=sys.stderr)\n",
            encoding="utf-8",
        )
        reports = []
        for workers in (1, 2):
            report_path = self.base / f"workers-{workers}.json"
            receipt_path = self.base / f"receipt-workers-{workers}.json"
            result = self.cli(root, ["--all", "--root", str(root), "--workers", str(workers),
                                     "--report", str(report_path), "--receipt", str(receipt_path)])
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(gates._verify_pair(report_path, receipt_path, root=root), 0)
            report = json.loads(report_path.read_bytes())
            self.assertEqual(tuple(leg["name"] for leg in report["legs"]), gates.REQUIRED)
            reports.append(report)
        streams = [next(leg["evidence"]["stderr"] for leg in report["legs"]
                        if leg["name"] == "PROCESS-SAFETY") for report in reports]
        self.assertNotEqual(streams[0]["sha256"], streams[1]["sha256"])
        for report in reports:
            self.assertIn(b"unrelated evidence id 17", comparable_report(report).encode())
        self.assertEqual(comparable_report(reports[0]), comparable_report(reports[1]))

        # Refresh the outer report digest so refusal depends on the raw stream.
        streams[1]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            comparable_report(reports[1])
        core.atomic_write_json(report_path, reports[1])
        receipt = json.loads(receipt_path.read_bytes())
        receipt["report"]["sha256"] = core.sha256_file(report_path)
        core.atomic_write_json(receipt_path, receipt)
        self.assertEqual(gates._verify_pair(report_path, receipt_path, root=root), 2)

    def test_comparator_refuses_forged_stream_digest(self):
        legs = gate_controls.passing_legs()
        legs[0]["evidence"]["stdout"] = {"bytes": 3, "sha256": "0" * 64, "text": "raw"}
        report = gates.aggregate(gate_controls.passing_legs())
        report["legs"] = legs
        with self.assertRaises(ValueError):
            comparable_report(report)


class RunReasonTests(unittest.TestCase):
    def test_equal_precedence_stage_refusal_and_disposition_both_visible(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            backend = SyntheticStages(root)
            backend.failure["provider"] = 3
            run = engine.Pipeline(root / "output", backend, plan="character").run()
        self.assertEqual((run["rc"], run["status"], run["verdict"]), (3, "INCOMPLETE", "CANNOT_SEE"))
        self.assertTrue(any(row["rc"] for row in run["stages"]))
        self.assertEqual(run["reason_codes"], ["TARGET_UNCALIBRATED", "stage_not_pass"])

    def test_final_source_fault_after_all_stages_pass_is_not_stage_not_pass(self):
        def run_with_fault(fail_at):
            with TemporaryDirectory() as directory:
                root = Path(directory)
                backend = SyntheticStages(root)
                calls = [0]
                original = backend.verify_sources
                def verify_sources():
                    calls[0] += 1
                    if calls[0] == fail_at:
                        raise OSError("drift")
                    return original()
                backend.verify_sources = verify_sources
                return calls, engine.Pipeline(root / "output", backend).run()
        calls, nominal = run_with_fault(-1)
        self.assertEqual(nominal["rc"], 0)
        _, run = run_with_fault(calls[0])
        self.assertFalse(any(row["rc"] for row in run["stages"]))
        self.assertNotEqual(run["rc"], 0)
        self.assertNotIn("stage_not_pass", run["reason_codes"])
