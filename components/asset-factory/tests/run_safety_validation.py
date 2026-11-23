"""Run only under the operator's systemd/taskset/nice/PID-namespace wrapper.

The namespace shell starts `sleep 900` before this driver and passes its PID.
Logs and validation records belong outside the component, at --out.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from asset_factory import proc


def identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "state": fields[0], "start_time": fields[19],
            "comm": Path(f"/proc/{pid}/comm").read_text().strip()}


def run(name, argv, root, out, timeout_s):
    started = time.monotonic_ns()
    timed_out = False
    with (out / (name + ".log")).open("wb") as log:
        child = proc.popen(argv, cwd=root, stdout=log, stderr=log,
                           start_new_session=True)
        try:
            child.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:
                proc.signal_group(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
    return {"name": name, "argv": argv, "cwd": str(root),
            "exit_code": child.returncode,
            "rc": 6 if timed_out else 0 if child.returncode == 0 else 5,
            "duration_ms": (time.monotonic_ns() - started) // 1000000,
            "log": str(out / (name + ".log"))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--canary", type=int, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    before = identity(args.canary)
    assert before["comm"] == "sleep" and before["state"] not in ("Z", "X")
    assert Path("/proc/1/comm").read_text().strip() == "bash", "PID namespace required"
    proc.signal_pid(args.canary, 0)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    python = [sys.executable, "-B", "-m", "unittest"]
    results = []
    results.append(run("safety-controls", python + [
        "test_safety_proc", "test_w5_rigmotion_repair.OwnedDescendants",
        "test_w5_rigmotion_repair.PublicationAndAdmission.test_R4_nonfixture_host_execution_is_refused_before_launch",
        "test_w5_rigmotion_repair.PublicationAndAdmission.test_R4_host_gpu_request_is_refused_before_launch",
        "-v"], root, out, 60))
    results.append(run("full-suite", python + ["discover", "-s", "tests", "-p", "test_*.py", "-q"],
                       root, out, 600))
    results.append(run("whole-gate", [sys.executable, "-B", "-m", "tools.gates", "--all",
        "--root", str(root), "--report", str(out / "whole-gate.json"),
        "--receipt", str(out / "whole-gate.receipt.json")], root, out, 180))
    wender = root.parent / "wender-agent"
    for name, pattern in (("wender-cdp", "*cdp_pipe*.py"),
                          ("wender-vision", "*vision_bridge*.py"),
                          ("wender-vserve", "test_vserve_client_unit.py")):
        results.append(run(name, python + ["discover", "-s", "tests", "-p", pattern, "-q"],
                           wender, out, 60))
    after = identity(args.canary)
    proc.signal_pid(args.canary, 0)
    survived = (before["pid"] == after["pid"] and before["start_time"] == after["start_time"]
                and after["state"] not in ("Z", "X"))
    record = {"schema": "af.safety-validation/v1", "results": results,
              "pid_namespace": os.readlink("/proc/self/ns/pid"),
              "canary_before": before, "canary_after": after, "canary_survived": survived,
              "all_passed": survived and all(row["rc"] == 0 for row in results)}
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    (out / "validation.json").write_text(encoded)
    print(encoded, end="")
    return 0 if record["all_passed"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
