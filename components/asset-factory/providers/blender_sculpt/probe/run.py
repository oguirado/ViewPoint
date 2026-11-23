"""Host-only S1-U1 runner. Run inside the task's systemd memory/CPU scope.

No caller mesh, GUI session, model, renderer or frame capture is used. Each
background case owns a process group; Path B owns a separate Xvfb GUI process.
"""
from asset_factory import proc as af_proc

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from asset_factory import core

HERE = Path(__file__).resolve().parent
MARKER = "AF_SCULPT_PROBE "
PHASE = "AF_SCULPT_PHASE "
MAX_LOG_BYTES = 1024 * 1024  # [TARGET] diagnostic read budget, not a mesh metric.
EXPECTED_VERSION = "4.0.2"


def result(rc, reasons=(), **fields):
    return dict(rc=rc, status=core.STATUS[rc], reason_codes=list(reasons), **fields)


def verify_stroke(row, *, strength_ppm, background):
    """FINISHED alone cannot prove a null or an effective stroke."""
    if type(strength_ppm) is not int or strength_ppm not in (0, 500000) or type(background) is not bool:
        return result(2, ["invalid_verifier_arguments"])
    if type(row) is not dict:
        return result(3, ["stroke_evidence_missing"])
    if row.get("rc") != 0 or type(row.get("rc")) is not int:
        rc = row.get("rc")
        return result(rc if type(rc) is int and rc in core.RC and rc else 2,
                      row.get("reason_codes", ["stroke_evidence_invalid"]))
    if row.get("runtime_version") != EXPECTED_VERSION:
        return result(4, ["runtime_version_drift"])
    ctx = row.get("execution_context", {})
    if (row.get("operator_poll") is not True or row.get("operator_return") != ["FINISHED"]
            or ctx.get("background") is not background or ctx.get("mode") != "SCULPT"
            or type(row.get("strength_ppm")) is not int or row.get("strength_ppm") != strength_ppm):
        return result(3, ["stroke_execution_unproven"])
    before, after = row.get("before_sha256"), row.get("after_sha256")
    vb, va = row.get("before_vertex_sha256"), row.get("after_vertex_sha256")
    for value in (before, after, vb, va):
        if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            return result(3, ["mesh_digest_missing"])
    unchanged = before == after
    if row.get("mesh_unchanged") is not unchanged:
        return result(2, ["inconsistent_mesh_digest"])
    if unchanged != (strength_ppm == 0):
        return result(5, ["null_stroke_changed_mesh" if strength_ppm == 0 else "nonzero_stroke_no_effect"])
    count = row.get("changed_vertices")
    if type(count) is not int or count < 0 or (count == 0) != (vb == va):
        return result(2, ["vertex_delta_inconsistent"])
    if (vb == va) != (strength_ppm == 0):
        return result(5, ["vertex_effect_unproven"])
    return result(0)


def qualify_pair(rows, *, background):
    if len(rows) != 2:
        return result(3, ["stroke_pair_missing"])
    checks = [verify_stroke(row, strength_ppm=strength, background=background)
              for row, strength in zip(rows, (0, 500000))]
    if rows[0].get("before_sha256") != rows[1].get("before_sha256"):
        checks.append(result(4, ["fixture_drift"]))
    rc = core.worst_rc(check["rc"] for check in checks)
    return result(rc, sorted({r for check in checks for r in check["reason_codes"]}), checks=checks)


def stop_owned(process):
    """Signal only the group created by this runner, including on normal exit."""
    try:
        af_proc.signal_group(process, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        af_proc.wait_exit(process, timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        af_proc.signal_group(process, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


def environment(directory):
    home = directory / "home"
    home.mkdir(exist_ok=True)
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(directory),
            "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "4",
            "LIBGL_ALWAYS_SOFTWARE": "1", "MESA_LOADER_DRIVER_OVERRIDE": "llvmpipe",
            "__GLX_VENDOR_LIBRARY_NAME": "mesa"}


def parse_record(text):
    def reject(_):
        raise ValueError("non_integer_number")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_key")
            value[key] = item
        return value

    value = json.loads(text, object_pairs_hook=unique, parse_float=reject, parse_constant=reject)
    core.canonical_json(value)
    return value


def process_case(argv, *, directory, timeout_s):
    log = directory / "process.log"
    started = time.monotonic_ns()
    with log.open("xb") as stream:
        process = af_proc.popen(argv, stdout=stream, stderr=subprocess.STDOUT,
                                   env=environment(directory), cwd=directory, start_new_session=True)
        timed_out = False
        try:
            af_proc.wait_exit(process, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            stop_owned(process)
    size = log.stat().st_size
    meta = {"argv": argv, "process_rc": process.returncode,
            "wall_ms": (time.monotonic_ns() - started) // 1000000,
            "log": str(log), "log_bytes": size, "log_sha256": core.sha256_file(log)}
    if timed_out:
        return result(6, ["deadline_exceeded"], **meta)
    if size > MAX_LOG_BYTES:
        return result(6, ["diagnostic_budget_exceeded"], **meta)
    lines = log.read_text(errors="replace").splitlines()
    records = [line[len(MARKER):] for line in lines if line.startswith(MARKER)]
    try:
        phases = [parse_record(line[len(PHASE):]) for line in lines if line.startswith(PHASE)]
    except (ValueError, core.ContractError):
        return result(2, ["probe_phase_invalid"], **meta)
    meta["last_phase"] = phases[-1] if phases else None
    if len(records) != 1:
        return result(3, ["process_signal" if process.returncode < 0 else "probe_record_missing"], **meta)
    try:
        row = parse_record(records[0])
        if type(row["rc"]) is not int or row["rc"] not in core.RC or row["status"] != core.STATUS[row["rc"]]:
            raise ValueError("invalid_stage_rc")
        if process.returncode != row["rc"]:
            raise ValueError("process_record_rc_mismatch")
    except (ValueError, KeyError, TypeError, core.ContractError):
        return result(2, ["probe_record_invalid"], **meta)
    return dict(row, **meta)


def headless_case(blender, directory, case, strength, timeout_s):
    directory.mkdir()
    row = process_case([str(blender), "-b", "--factory-startup", "--disable-autoexec",
                         "--python-exit-code", "2", "--python", str(HERE / "bpy_probe.py"),
                         "--", "--case", case, "--strength-ppm", str(strength)],
                        directory=directory, timeout_s=timeout_s)
    row.setdefault("case", case)
    row.setdefault("strength_ppm", strength)
    return row


def live_cases(blender, bvqa_root, directory, timeout_s):
    """Use the actual vqa3d adapter; own GUI only, no observe/capture calls."""
    directory.mkdir()
    bridge = bvqa_root / "vqa3d/adapters/blender_live_bridge.py"
    if not bridge.is_file() or not Path("/usr/bin/xvfb-run").is_file():
        return result(3, ["live_runtime_unavailable"]), []
    sys.path.insert(0, str(bvqa_root))
    try:
        adapter_class = importlib.import_module("vqa3d.adapters.blender_live").BlenderLiveAdapter
    except ImportError as exc:
        return result(3, ["live_adapter_unavailable"], error=str(exc)), []
    live_dir = directory / "bridge"
    live_dir.mkdir()
    env = environment(directory)
    env["VQA3D_LIVE_DIR"] = str(live_dir)
    argv = ["/usr/bin/xvfb-run", "-a", "-e", str(directory / "xvfb.log"),
            "-s", "-screen 0 1280x720x24 -nolisten tcp", str(blender),
            "--factory-startup", "--disable-autoexec", "--python", str(bridge)]
    adapter, rows, actions = None, [], []
    started = time.monotonic_ns()
    with (directory / "process.log").open("xb") as log:
        process = af_proc.popen(argv, env=env, cwd=directory, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + timeout_s
            while not (live_dir / "bridge.json").is_file():
                if af_proc.poll_exit(process) is not None:
                    raise RuntimeError("live_process_exited")
                if time.monotonic() >= deadline:
                    raise TimeoutError("live_start_deadline")
                time.sleep(0.05)
            adapter = adapter_class(live_dir=live_dir, timeout=timeout_s)
            transport = adapter.probe(render=False)
            if transport["available"] is not True:
                return result(3, ["live_transport_unavailable"], transport=transport), rows
            for strength in (0, 500000):
                target = directory / ("stroke_" + str(strength) + ".json")
                script = directory / ("stroke_" + str(strength) + ".py")
                # The adapter hashes these exact bytes and the GUI verifies them.
                script.write_text(
                    "import importlib.util, os\nfrom pathlib import Path\n"
                    "spec=importlib.util.spec_from_file_location('af_s1_u1', " + repr(str(HERE / "bpy_probe.py")) + ")\n"
                    "module=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                    "record=module.probe('view3d', " + str(strength) + ", reuse_fixture=" + repr(strength != 0) + ")\n"
                    "target=Path(" + repr(str(target)) + ")\n"
                    "temp=target.with_suffix('.tmp')\n"
                    "with temp.open('x') as stream:\n stream.write(module.canonical(record)); stream.flush(); os.fsync(stream.fileno())\n"
                    "os.replace(temp,target)\n", encoding="utf-8")
                action = adapter.act({"op": "run_script", "path": str(script)})
                if action["ok"] is not True or action["script_sha256"] != core.sha256_file(script):
                    raise ValueError("live_script_identity_unproven")
                actions.append(action)
                rows.append(core.load_json(target, max_bytes=MAX_LOG_BYTES))
            qualification = qualify_pair(rows, background=False)
            return dict(qualification, transport=transport, actions=actions,
                        adapter_source_sha256=core.sha256_file(bvqa_root / "vqa3d/adapters/blender_live.py"),
                        bridge_source_sha256=core.sha256_file(bridge),
                        payload_source_sha256=core.sha256_file(bvqa_root / "vqa3d/adapters/blender_live_payload.py")), rows
        except (TimeoutError, RuntimeError, ValueError, OSError, core.ContractError) as exc:
            return result(6 if isinstance(exc, TimeoutError) else 3, ["live_execution_unproven"],
                          error=type(exc).__name__ + ": " + str(exc), actions=actions), rows
        finally:
            cleanup_error = None
            try:
                if adapter is not None:
                    adapter.close()
            except Exception as exc:
                cleanup_error = type(exc).__name__
                raise
            finally:
                try:
                    stop_owned(process)
                finally:
                    cleanup = {"process_reaped": None if process is None else af_proc.poll_exit(process) is not None,
                               "adapter_cleanup_error": cleanup_error,
                               "wall_ms": (time.monotonic_ns() - started) // 1000000, "argv": argv}
                    core.atomic_write_json(directory / "lifecycle.json", cleanup)


def run(*, blender, output_dir, bvqa_root, timeout_s=30):
    if type(timeout_s) is not int or not 1 <= timeout_s <= 60:
        raise core.ContractError("invalid_timeout")
    blender, output_dir, bvqa_root = map(lambda p: Path(p).resolve(), (blender, output_dir, bvqa_root))
    if not blender.is_file() or not os.access(blender, os.X_OK) or not output_dir.is_dir():
        raise core.ContractError("probe_path_invalid")
    directory = Path(tempfile.mkdtemp(prefix="measurement-", dir=output_dir))
    rows = []
    for name, strength in (("rna", 0), ("rna", 0), ("unknown", 0), ("direct", 0),
                           ("no_window", 0), ("object", 0), ("view3d", 0), ("view3d", 500000)):
        row = headless_case(blender, directory / (str(len(rows)) + "_" + name), name, strength, timeout_s)
        rows.append(row)
    first, second, unknown = rows[:3]
    rna_rc = 0
    if first.get("rc") != 0 or second.get("rc") != 0 or "rna" not in first or "rna" not in second:
        rna_rc = 3
    elif (first.get("rna_sha256") != core.sha256_json(first["rna"])
          or second.get("rna_sha256") != core.sha256_json(second["rna"])
          or first["rna_sha256"] != second["rna_sha256"]):
        rna_rc = 4
    elif first.get("runtime_version") != EXPECTED_VERSION:
        rna_rc = 4
    else:
        core.atomic_write_json(directory / "rna_dump.json", first["rna"])
    unknown_ok = unknown.get("rc") == 2 and unknown.get("reason_codes") == ["unknown_operator"]
    path_a = qualify_pair(rows[-2:], background=True)
    path_b, live_rows = live_cases(blender, bvqa_root, directory / "live", timeout_s)
    # This unit characterizes capability. Unsupported headless stays separately red.
    capability_rc = 0 if path_a["rc"] == 0 or path_b["rc"] == 0 else 3
    rc = core.worst_rc([rna_rc, 0 if unknown_ok else 5, capability_rc])
    report = result(rc, [] if rc == 0 else ["capability_qualification_incomplete"],
                    unit="S1-U1", verdict=core.VERDICT[rc], evidence_dir=str(directory),
                    runtime_version=first.get("runtime_version"), blender=str(blender),
                    blender_sha256=core.sha256_file(blender), probe_sha256=core.sha256_file(HERE / "bpy_probe.py"),
                    runner_sha256=core.sha256_file(Path(__file__)), rna_sha256=first.get("rna_sha256"),
                    rna_repeat_verified=rna_rc == 0, unknown_operator_control=unknown_ok,
                    path_a_headless=path_a, path_b_live=path_b,
                    headless_observations=rows, live_observations=live_rows,
                    recommended_path="headless" if path_a["rc"] == 0 else "live" if path_b["rc"] == 0 else None,
                    scope="Synthetic sphere capability only; no appearance, transaction or arbitrary-asset qualification.")
    # New owned evidence directory: diagnostics survive refusals. Caller publication is separate.
    core.atomic_write_json(directory / "observations.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bvqa-root", type=Path, required=True)
    parser.add_argument("--timeout-s", type=int, default=30)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        report = run(blender=args.blender, output_dir=args.output_dir,
                     bvqa_root=args.bvqa_root, timeout_s=args.timeout_s)
        if report["rc"] == 0 and args.receipt is not None:
            core.atomic_write_json(args.receipt, report)
        return core.emit(report)
    except (OSError, core.ContractError) as exc:
        return core.emit(exc.as_dict() if isinstance(exc, core.ContractError) else result(2, ["probe_io_error"]))
    except KeyboardInterrupt:
        return core.emit(result(6, ["probe_interrupted"]))
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
