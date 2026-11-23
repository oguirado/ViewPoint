"""Focused controls for the whole-gate overlap and owned job retirement."""

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from unittest import mock

from test_f0u6_gates import U6Case, passing_legs
from tools import gates


def comparable_report(report):
    """Remove elapsed times and rehash streams that embed a unittest elapsed time."""
    report = deepcopy(report)
    for leg in report["legs"]:
        del leg["duration_ms"]
        if leg["name"] not in gates.UNITTEST_LEGS:
            continue
        for key in ("stdout", "stderr"):
            stream = leg["evidence"].get(key)
            if stream is None:
                continue
            original = stream["text"].encode("utf-8")
            normalized = re.sub(rb"(?m)^(Ran \d+ tests? in )\d+(?:\.\d+)?s$",
                                rb"\g<1><elapsed>", original)
            if normalized != original:
                if stream["bytes"] != len(original) or stream["sha256"] != hashlib.sha256(original).hexdigest():
                    raise ValueError("unittest output does not match its evidence")
                stream.update(text=normalized.decode(), bytes=len(normalized),
                              sha256=hashlib.sha256(normalized).hexdigest())
    return gates.core.canonical_json(report)


# The only raw child launch is in the artificial descendant fixture. The gate and
# test controller launch and retire their actual owned jobs through proc.
_DESCENDANT = """\
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
                         start_new_session=True)
path = Path(sys.argv[1])
temp = path.with_suffix('.tmp')
temp.write_text(f'{os.getpid()} {child.pid}')
temp.replace(path)
time.sleep(30)
"""


class GatePerfTests(U6Case):
    def gate_args(self, root, label, workers):
        return ["--all", "--root", str(root), "--workers", str(workers),
                "--report", str(self.base / f"{label}.report.json"),
                "--receipt", str(self.base / f"{label}.receipt.json")]

    def test_report_matches_except_elapsed_times(self):
        root = self.component(scripts=True)
        for label, workers in (("serial", 1), ("parallel", 2)):
            result = self.cli(root, self.gate_args(root, label, workers))
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            report = self.base / f"{label}.report.json"
            receipt = self.base / f"{label}.receipt.json"
            self.assertEqual(gates._verify_pair(report, receipt, root=root), 0)
            self.assertEqual(result.stdout, report.read_bytes())
        serial, parallel = (json.loads((self.base / f"{label}.report.json").read_bytes())
                            for label in ("serial", "parallel"))
        self.assertEqual(comparable_report(serial), comparable_report(parallel))
        self.assertEqual(tuple(leg["name"] for leg in parallel["legs"]), gates.REQUIRED)

    def test_worker_limit_rejects_unmeasured_counts(self):
        root = self.component(scripts=True)
        for workers in (0, 3):
            with self.subTest(workers=workers):
                result = self.cli(root, self.gate_args(root, "excess", workers))
                self.assertEqual(result.returncode, 2)
                self.assertIn(b"invalid choice", result.stderr)

    def test_process_warnings_are_hashed_as_raw_child_bytes(self):
        # Orchestrator ruling 18 Nov: evidence describes what the child printed;
        # PID/af-job normalisation belongs to report comparison only.
        code = (
            "import sys; print(sys.argv[1], file=sys.stderr); "
            "print('Ran 1 test in 0.001s', file=sys.stderr)"
        )
        for pid, scratch_id in ((71, "abcd1234"), (103, "efgh5678")):
            log = (f"diagnostic 1127: ResourceWarning: subprocess {pid} is still running\n"
                   f"ResourceWarning: Implicitly cleaning up <_OwnedScratch '/tmp/af-job-{scratch_id}'>\n"
                   "unrelated evidence id 17")
            with self.subTest(pid=pid):
                result = gates.run_leg("PROCESS-SAFETY", [sys.executable, "-B", "-c", code, log], 5)
                self.assertEqual(result["rc"], 0)
                raw = (log + "\nRan 1 test in 0.001s\n").encode()
                self.assertEqual(result["evidence"]["stderr"],
                                 {"bytes": len(raw), "text": raw.decode(),
                                  "sha256": hashlib.sha256(raw).hexdigest()})

    def test_missing_and_duplicate_leg_refuse(self):
        nominal = {leg["name"]: leg for leg in passing_legs()}
        with mock.patch.object(gates, "run_leg", side_effect=lambda name, *_: nominal[name]):
            complete = gates.aggregate(gates._run_legs(2))
            without_early = {name: argv for name, argv in gates.LEGS.items()
                             if name != "F0-U6-selftest"}
            with mock.patch.object(gates, "LEGS", without_early):
                missing = gates.aggregate(gates._run_legs(2))
            duplicate_records = {**nominal, "F0-U6-selftest": nominal["F0-U3"]}
            with mock.patch.dict(nominal, duplicate_records):
                duplicate = gates.aggregate(gates._run_legs(2))
        self.assertEqual(complete["rc"], 0)
        self.assertEqual(missing["rc"], 3)
        self.assertIn("F0-U6-selftest:missing_leg", missing["failures"])
        self.assertEqual(duplicate["rc"], 2)
        self.assertIn("invalid_or_duplicate_leg", duplicate["failures"])

    def test_parallel_refuses_failed_child_and_unittest_skip(self):
        early = "F0-U6-selftest"
        nominal = {leg["name"]: leg for leg in passing_legs()}
        run_leg = gates.run_leg
        for code, expected, reason in (
            ("print('Ran 1 test in 0.001s'); raise SystemExit(1)", 5, "unittest_failed"),
            ("print('Ran 1 test in 0.001s\\nOK (skipped=1)')", 3, "unittest_coverage_incomplete"),
        ):
            with self.subTest(reason=reason):
                def run(name, argv, timeout):
                    return run_leg(name, [sys.executable, "-B", "-c", code], 5) if name == early else nominal[name]

                with mock.patch.object(gates, "run_leg", side_effect=run):
                    report = gates.aggregate(gates._run_legs(2))
                leg = next(item for item in report["legs"] if item["name"] == early)
                self.assertEqual(leg["rc"], expected)
                self.assertEqual(report["rc"], expected)
                self.assertIn(f"{early}:{reason}", report["failures"])

    def assert_drained(self, marker):
        for pid in map(int, marker.read_text().split()):
            self.assertGreater(pid, 1)
            self.assertFalse(Path(f"/proc/{pid}").exists(), f"owned process {pid} survived")

    def test_timeout_retires_session_and_descendant_once(self):
        marker = self.base / "timeout-pids"
        with mock.patch.object(gates.af_proc, "cleanup", wraps=gates.af_proc.cleanup) as cleanup:
            result = gates.run_leg("F0-U3", [sys.executable, "-B", "-c", _DESCENDANT, str(marker)], 1)
        self.assertEqual(result["rc"], 6)
        self.assertEqual(result["failures"], ["deadline_exceeded"])
        self.assertLess(result["duration_ms"], 6000)
        cleanup.assert_called_once()
        self.assert_drained(marker)

    def test_parallel_sigterm_retires_both_active_legs_once(self):
        root = self.component(scripts=True)
        markers = [self.base / name for name in ("foreground-pids", "background-pids")]
        actions = self.base / "cleanup-units"
        driver = """\
import sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from tools import gates
actions, foreground, background, code = sys.argv[1:5]
cleanup = gates.af_proc.cleanup
def observe(job):
    with Path(actions).open('a') as stream:
        stream.write(job.unit_name + '\\n')
    return cleanup(job)
gates.af_proc.cleanup = observe
gates.LEGS = {
    name: [sys.executable, '-B', '-c', code, marker]
    for name, marker in (('F0-U3', foreground), ('F0-U6-selftest', background))
}
raise SystemExit(gates.main(sys.argv[5:]))
"""
        argv = [sys.executable, "-I", "-B", "-c", driver, str(root), str(actions),
                *map(str, markers), _DESCENDANT, *self.gate_args(root, "interrupted", 2)]
        child = gates.af_proc.popen(argv, cwd=root, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not all(marker.exists() for marker in markers) and time.monotonic() < deadline:
                if gates.af_proc.poll_exit(child) is not None:
                    break
                time.sleep(0.02)
            self.assertTrue(all(marker.exists() for marker in markers), "both legs must overlap")
            gates.af_proc.signal_pid(child, signal.SIGTERM)
            stdout, stderr = gates.af_proc.communicate(child, timeout=8)
            self.assertEqual(child.returncode, 6, stderr.decode())
            self.assertEqual(json.loads(stdout)["reason_codes"], ["deadline_exceeded"])
            self.assertEqual(sorted(Counter(actions.read_text().splitlines()).values()), [1, 1])
            for marker in markers:
                self.assert_drained(marker)
            for artifact in ("interrupted.report.json", "interrupted.receipt.json"):
                self.assertFalse((self.base / artifact).exists())
        finally:
            gates.af_proc.cleanup(child)
