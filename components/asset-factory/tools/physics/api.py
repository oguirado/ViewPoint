"""Bounded qualification runner; one atomic directory publication on PASS only."""
from __future__ import annotations
from asset_factory import proc as af_proc

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time

from asset_factory import core
from .contracts import MAX_INPUT_BYTES, Refusal, require, load_inputs
from .gates import evaluate
from .mjcf import write_mjcf

MAX_TRACE_BYTES = 64 * 2**20  # [TARGET] fixed trace memory and IO ceiling
COMPONENT_ROOT = Path(__file__).resolve().parents[2]
CLEANUP_TIMEOUT_S = 1  # [TARGET] independent release observation deadline


def _release_child(job):
    """The service manager proves complete cgroup release."""
    try:
        job.stop()
    except af_proc.SignalRefusal as exc:
        raise Refusal(3, "release_unproven") from exc

def run_child(argv, *, timeout_s, cwd):
    """Own the group; reap and observe release on success, timeout and error."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        env = {"PATH": os.defpath, "PYTHONPATH": str(COMPONENT_ROOT),
               "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            if key in os.environ:
                env[key] = os.environ[key]
        proc = af_proc.popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout,
                                stderr=stderr, start_new_session=True,
                                env=env)
        try:
            af_proc.wait_exit(proc, timeout=timeout_s)
        finally:
            _release_child(proc)
        stdout.seek(0)
        return proc.returncode, stdout.read(8193)


def qualification(scene_path, protocol_path, *, evidence_root, output_dir):
    """Internal tool, not scheduler/licence admission. No model or viewer is launched.

    A missing analytic interval is CANNOT_SEE. This tool never certifies the
    collision derivative's fidelity to the visual source. The slot adapter
    performs provider/licence admission separately before calling this tool.
    """
    start = time.monotonic_ns()
    try:
        scene, p = load_inputs(Path(scene_path), Path(protocol_path), Path(evidence_root))
        target = Path(output_dir)
        require(not target.exists() and not target.is_symlink(), 2, "output_exists")
        require(target.parent.is_dir(), 2, "output_parent_missing")
        deadline = time.monotonic() + p["timeout_s"]
        with tempfile.TemporaryDirectory(prefix=".physics-", dir=target.parent) as temp:
            scratch = Path(temp)
            traces, run_ids, models = [], [], []
            for index, factor in enumerate((1, 2, 1)):
                dt = p["timestep_us"] // factor
                nonce = secrets.token_hex(16)
                model = write_mjcf(scene, p, timestep_us=dt)
                job = {"scene": scene, "protocol": p, "dt_us": dt,
                       "steps": p["steps"]*factor, "run_id": nonce}
                jobpath, tracepath = scratch / f"job-{index}.json", scratch / f"trace-{index}.json"
                core.atomic_write_json(jobpath, job)
                remaining = deadline-time.monotonic()
                require(remaining > 0, 6, "deadline_exceeded")
                argv = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G",
                        "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0",
                        "taskset", "-c", "0-15", "nice", "-n", "15",
                        sys.executable, "-B", "-m", "tools.physics.worker", "--job", str(jobpath),
                        "--trace", str(tracepath)]
                child_rc, out = run_child(argv, timeout_s=remaining, cwd=COMPONENT_ROOT)
                if child_rc != 0:
                    require(child_rc in (2, 3, 4, 5, 6) and len(out) <= 8192, 2, "worker_contract_invalid")
                    try:
                        refusal = json.loads(out)
                    except (ValueError, UnicodeError) as exc:
                        raise Refusal(2, "worker_contract_invalid") from exc
                    require(type(refusal) is dict and refusal.get("rc") == child_rc
                            and type(refusal.get("reason_codes")) is list
                            and len(refusal["reason_codes"]) > 0
                            and all(type(c) is str for c in refusal["reason_codes"]),
                            2, "worker_contract_invalid")
                    raise Refusal(child_rc, refusal["reason_codes"][0])
                require(tracepath.is_file(), 3, "trace_missing")
                require(tracepath.stat().st_size <= MAX_TRACE_BYTES, 6, "trace_budget_exceeded")
                traces.append(core.load_json(tracepath, max_bytes=MAX_TRACE_BYTES))
                run_ids.append(nonce)
                models.append(hashlib.sha256(model.encode()).hexdigest())
            # Caller data is authenticated again before committing the generation.
            again_scene, again_p = load_inputs(Path(scene_path), Path(protocol_path), Path(evidence_root))
            require(scene == again_scene and p == again_p, 4, "input_changed_during_run")
            receipt = evaluate(scene, p, traces, run_ids=run_ids, model_sha256s=models)
            if receipt["rc"] != 0:
                return receipt
            receipt["wall_ms"] = (time.monotonic_ns()-start)//1000000
            receipt["peak_rss_bytes"] = max(t["peak_rss_bytes"] for t in traces)
            # Three raw traces plus MJCFs stay beside the independently checked receipt.
            for index, factor in enumerate((1, 2, 1)):
                (scratch / f"model-{index}.xml").write_text(write_mjcf(scene, p, timestep_us=p["timestep_us"]//factor))
                (scratch / f"job-{index}.json").unlink()
            core.atomic_write_json(scratch / "scene.json", scene)
            core.atomic_write_json(scratch / "protocol.json", p)
            core.atomic_write_json(scratch / "receipt.json", receipt)  # commit marker is last
            # Existing targets, including empty directories, are never replaced.
            _publish_directory(scratch, target)
            return receipt
    except subprocess.TimeoutExpired:
        rc, code = 6, "deadline_exceeded"
    except af_proc.SignalRefusal as exc:
        rc, code = exc.rc, exc.code
    except Refusal as exc:
        rc, code = exc.rc, exc.code
    except (core.ContractError, OSError, ValueError, TypeError, KeyError):
        rc, code = 2, "physics_input_invalid"
    return {"schema": "af.physics-receipt/v1", "rc": rc, "status": core.STATUS[rc],
            "verdict": core.VERDICT[rc], "reason_codes": [code], "metrics": {},
            "scene_sha256": None, "protocol_sha256": None, "trace_sha256s": [],
            "scope": "rigid_body_analytic_qualification", "threshold_status": "TARGET_UNCALIBRATED"}


def _publish_directory(source, target):
    # Linux renameat2(RENAME_NOREPLACE), required by the byte-preserving refusal rule.
    import ctypes
    import errno
    libc = ctypes.CDLL(None, use_errno=True)
    require(hasattr(libc, "renameat2"), 3, "atomic_publish_unavailable")
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        err = ctypes.get_errno()
        raise Refusal(2, "output_exists" if err in (errno.EEXIST, errno.ENOTEMPTY) else "publish_io_error")


def main(argv=None):
    parser = argparse.ArgumentParser()
    for flag in ("scene", "protocol", "root", "out"):
        parser.add_argument("--"+flag, required=True)
    args = parser.parse_args(argv)
    return core.emit(qualification(args.scene, args.protocol, evidence_root=args.root, output_dir=args.out))


if __name__ == "__main__":
    sys.exit(main())
