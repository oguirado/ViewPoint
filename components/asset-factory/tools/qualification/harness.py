"""Run a pinned candidate offline and retain evidence for independent review."""

from dataclasses import asdict
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from asset_factory import core, proc
from .gpu import (
    GPU_LOG_NAMES, SCOPE_LIMITS, GpuLaunch, reconcile_gpu_evidence, validate_handoff, validate_permit_evidence, validate_release, wait_for_scope,
)
from .manifest import JSON_LIMIT, Pins, Refusal, check_candidate
from .offline import PROBE_MESSAGE
from .sandbox import BOOTSTRAP, OUTBOUND, WORK, seccomp_library_path, validate_devices

RECEIPT_SCHEMA = "af.qualification-receipt/v1"
DEFAULT_LIMITS = proc.MemoryLimits(6 * 1024**3, 8 * 1024**3)
OFFLINE = Path(__file__).with_name("offline.py").resolve()
TRACE_LINE = re.compile(r"^\s*(?:\d+[ \t]+)?(" + "|".join(OUTBOUND) + r")\(")
OUTBOUND_SYSCALLS = ",".join(OUTBOUND)
TOOL_NAMES = ("unshare", "strace", "time", "bwrap")
SANDBOX = Path(__file__).with_name("sandbox.py").resolve()
LOG_NAMES = frozenset({"stdout", "stderr", "supervisor.stdout", "supervisor.stderr",
                       "namespace.json", "mount.json", "network.trace", "rss.kib", "supervision.json"})
SUPERVISION_FIELDS = (
    "role", "unit_name", "wall_ns", "returncode", "process_result", "process_reason",
    "process_refusal", "cleanup_proven", "cleanup_errors", "timed_out", "cgroup_peak_source",
    "cgroup_path", "cgroup_before_bytes", "cgroup_peak_bytes",
)


def write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(core.canonical_json(value))


def infrastructure(pins):
    tools = {}
    for name in TOOL_NAMES:
        found = shutil.which(name, path=os.defpath)
        if found is None:
            raise Refusal(3, "offline_tool_unavailable")
        tools[name] = Path(found).resolve()
    tools["supervisor_python"] = Path(sys.executable).resolve()
    tools["supervisor_script"] = OFFLINE
    tools["sandbox_launcher"] = SANDBOX
    try:
        tools["seccomp_library"] = seccomp_library_path()
    except (AttributeError, OSError) as exc:
        raise Refusal(3, "seccomp_library_unavailable") from exc
    facts = {}
    for name, path in tools.items():
        pin = {"path": str(path), "sha256": core.sha256_file(path)}
        pins.check(pin)
        facts[name] = pin
    return tools, facts


def pidns_scope_peak(path=None):
    """The isolated test backend shares the named outer scope with its driver."""
    try:
        relative = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                        if line.startswith("0::/"))
        root = Path("/sys/fs/cgroup")
        actual = (root / relative.lstrip("/")).resolve()
        if not actual.is_relative_to(root) or not actual.name.endswith(".scope"):
            return None, None
        if path is not None and actual != path:
            return None, None
        settings = {key: int((actual / ("memory." + key)).read_text())
                    for key in ("high", "max", "swap.max", "peak")}
        if (settings["high"], settings["max"], settings["swap.max"]) != (
                DEFAULT_LIMITS.high, DEFAULT_LIMITS.maximum, DEFAULT_LIMITS.swap):
            return None, None
        return actual, settings["peak"]
    except (OSError, ValueError, StopIteration):
        return None, None


def measurements(cgroup, rss, *, cuda, cuda_record):
    result = {
        "cgroup_peak_bytes": cgroup,
        "rss_peak_bytes": rss,
        "cuda_peak_allocated_bytes": None,
        "cuda_peak_reserved_bytes": None,
        "cuda_peak_source": None,
    }
    reasons = [name for name, value in (("cgroup_peak_unavailable", cgroup), ("rss_peak_unavailable", rss))
               if type(value) is not int or value <= 0]
    if cuda:
        allocated, reserved = None, None
        if type(cuda_record) is dict and set(cuda_record) == {"allocated_bytes", "reserved_bytes"}:
            allocated, reserved = cuda_record["allocated_bytes"], cuda_record["reserved_bytes"]
        if type(allocated) is not int or type(reserved) is not int or not 0 < allocated <= reserved:
            reasons.append("cuda_peak_unavailable")
        else:
            result.update(cuda_peak_allocated_bytes=allocated, cuda_peak_reserved_bytes=reserved,
                          cuda_peak_source="pilot_adapter")
    return result, reasons


def read_json_evidence(path):
    try:
        return core.load_json(path, max_bytes=JSON_LIMIT)
    except core.ContractError:
        return None


def descriptor(path):
    return {"sha256": core.sha256_file(path), "bytes": path.stat().st_size}


def regular(path):
    return not path.is_symlink() and path.is_file()


def trace_counts(trace):
    attempts = denied_connects = containment_attempts = 0
    trace_started = False
    if regular(trace):
        with trace.open(errors="replace") as stream:
            for line in stream:
                trace_started |= "execve(" in line and " = 0" in line
                match = TRACE_LINE.match(line)
                if match:
                    attempts += 1
                    if match[1] == "connect" and "EACCES" in line:
                        denied_connects += 1
                elif re.match(r"^\s*(?:\d+[ \t]+)?clone3?\(", line) and "CLONE_UNTRACED" in line:
                    containment_attempts += 1
    return (attempts, denied_connects, containment_attempts) if trace_started else (None, None, None)


def wait_gpu(job, trace, deadline, timeout_s):
    while True:
        attempts, _, containment = trace_counts(trace)
        if containment:
            raise Refusal(5, "candidate_containment_attempt")
        if attempts:
            raise Refusal(5, "candidate_network_attempt")
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(job.unit_name, timeout_s)
        if proc.poll_exit(job) is not None:
            return
        time.sleep(0.1)


def rss_bytes(path):
    if regular(path):
        with path.open() as stream:
            reading = stream.read(64).strip()
        if reading.isascii() and reading.isdecimal():
            return int(reading) * 1024
    return None


def namespace_evidence(folder, caller_inode, caller_mount_inode):
    net = read_json_evidence(folder / "namespace.json")
    mount = read_json_evidence(folder / "mount.json")
    net_ok = (type(net) is dict and type(net.get("inode")) is int and
              net["inode"] != caller_inode and net.get("interfaces") == ["lo"] and
              net.get("loopback_up") is False and net.get("routes") is False)
    mount_ok = (type(mount) is dict and type(mount.get("mnt-namespace")) is int and
                mount["mnt-namespace"] != caller_mount_inode)
    return net if net_ok else None, mount if mount_ok else None


def run_offline(root, role, command, tools, bindings, caller_inode, caller_mount_inode,
                timeout_s, limits, *, cuda, gpu=None, candidate_sha256=None):
    folder = root / role
    folder.mkdir(mode=0o700)
    work = folder / "work"
    work.mkdir(mode=0o700)
    for child in ("cache", "tmp", "runtime"):
        (work / child).mkdir(mode=0o700)
    if role == "candidate":
        (work / "output").mkdir(mode=0o700)
    trace, rss_path, namespace = (
        folder / name for name in ("network.trace", "rss.kib", "namespace.json")
    )
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        runtime_dir_path = Path(runtime_dir)
        runtime_dir = str(runtime_dir_path.resolve())
        if not runtime_dir_path.is_absolute() or Path(runtime_dir).is_relative_to("/usr"):
            raise Refusal(3, "offline_runtime_dir_unproven")
    else:
        runtime_dir = None
    config = folder / "launcher.json"
    configuration = {
        "work": str(work), "mount_info": str(folder / "mount.json"),
        "runtime_dir": runtime_dir, "bindings": bindings, "command": [str(arg) for arg in command],
        "bwrap": str(tools["bwrap"]), "python": str(tools["supervisor_python"]),
        "launcher": str(SANDBOX), "seccomp_library": str(tools["seccomp_library"]),
    }
    if gpu is not None:
        configuration.update(gpu_devices=gpu.devices, gpu_witness=str(folder / "gpu-devices.json"),
                             gpu_authority=str(folder / "gpu-authority.json"))
    write_json(config, configuration)
    argv = [
        tools["unshare"], "--user", "--map-current-user", "--net", "--",
        tools["supervisor_python"], "-I", "-B", OFFLINE, str(caller_inode), namespace,
        tools["strace"], "--kill-on-exit", "-f", "-qq", "-s", "0", "-o", trace,
        "-e", "trace=execve,execveat,clone,clone3," + OUTBOUND_SYSCALLS,
        "-e", "raw=execve,execveat," + OUTBOUND_SYSCALLS,
        "--", tools["time"], "-q", "-f", "%M", "-o", rss_path, "--",
        tools["supervisor_python"], "-I", "-S", "-B", SANDBOX, config,
    ]
    if gpu is not None:
        argv = [tools["supervisor_python"], "-I", "-S", "-B", OFFLINE, "gpu-scope",
                folder, str(timeout_s), *argv]
    env = {
        "PATH": os.defpath, "HOME": str(WORK), "XDG_CACHE_HOME": str(WORK / "cache"),
        "XDG_RUNTIME_DIR": str(WORK / "runtime"), "TMPDIR": str(WORK / "tmp"),
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
        "LC_ALL": "C",
    }
    if not cuda:
        env["CUDA_VISIBLE_DEVICES"] = ""
    scope = None
    before = None
    if os.environ.get("AF_PROC_BACKEND") == "pidns":
        scope, before = pidns_scope_peak()
    start = time.monotonic_ns()
    job = None
    timed_out = False
    launch_issue = None
    cleanup_errors = []
    unit_name = None
    gpu_issue = None
    handoff = None
    try:
        job = proc.popen(argv, cwd=work, env=env, start_new_session=True,
                         memory_limits=limits, runtime_s=timeout_s)
        unit_name = job.unit_name
        if gpu is not None:
            deadline = start / 1_000_000_000 + timeout_s
            # Scope setup is bounded separately so a service that failed preflight
            # does not hold the model scheduler until the full inference timeout.
            scope = wait_for_scope(folder / "gpu-scope.json", unit_name,
                                   min(deadline, time.monotonic() + 5))
            handoff, permit = gpu.authorize(job, scope, folder, deadline)
            validate_handoff(handoff, scope, unit_name, candidate_sha256)
            validate_permit_evidence(permit, handoff)
            if handoff["gpu_uuid"] != gpu.gpu_uuid:
                raise Refusal(4, "gpu_device_witness_changed")
            write_json(folder / "gpu-permit.json", permit)
            if time.monotonic() >= deadline:
                raise Refusal(6, "candidate_time_budget")
            core.atomic_write_json(folder / "gpu-authority.json", handoff)
            wait_gpu(job, trace, deadline, timeout_s)
        else:
            elapsed = (time.monotonic_ns() - start) / 1_000_000_000
            job.wait(timeout=max(0, timeout_s - elapsed))
    except Refusal as exc:
        gpu_issue = exc
    except subprocess.TimeoutExpired:
        timed_out = True
    except (OSError, proc.SignalRefusal) as exc:
        launch_issue = getattr(exc, "code", type(exc).__name__)
        unit_name = unit_name or getattr(exc, "unit_name", None)
        cleanup_errors = list(getattr(exc, "cleanup_errors", []))
    finally:
        if job is not None:
            cleanup_errors = proc.cleanup(job)
    wall_ns = time.monotonic_ns() - start

    result = job.result if job is not None else None
    cleanup_proven = result is not None and not cleanup_errors
    if handoff is not None and cleanup_proven:
        try:
            release = gpu.release(unit_name)
            validate_release(release, unit_name, handoff)
            write_json(folder / "gpu-release.json", release)
        except Refusal as exc:
            gpu_issue = gpu_issue or exc
    logs = {}
    if job is not None:
        for name in ("stdout", "stderr"):
            supervisor_path = folder / ("supervisor." + name)
            shutil.copyfile(getattr(job, name + "_path"), supervisor_path)
            logs[supervisor_path.name] = descriptor(supervisor_path)
            candidate_path = work / ("." + name)
            if cleanup_proven and regular(candidate_path):
                path = folder / name
                shutil.copyfile(candidate_path, path)
                logs[name] = descriptor(path)
    cgroup = result.memory_peak_bytes if result is not None else None
    peak_source = "systemd_job" if cgroup is not None else None
    if result is not None and result.systemd_result == "pidns-exit" and scope is not None:
        _, after = pidns_scope_peak(scope)
        if before is not None and after is not None and after >= before:
            cgroup, peak_source = after, "pidns_test_scope"

    net, mount = namespace_evidence(folder, caller_inode, caller_mount_inode)
    attempts, denied_connects, containment_attempts = trace_counts(trace)
    rss = rss_bytes(rss_path)
    witness_paths = [namespace, folder / "mount.json", trace, rss_path]
    if gpu is not None:
        witness_paths.extend(folder / name for name in GPU_LOG_NAMES)
    for path in witness_paths:
        if regular(path):
            logs[path.name] = descriptor(path)
    output = work / "output"
    if role == "candidate" and cleanup_proven and not output.is_symlink() and output.is_dir():
        os.replace(output, root / "output")
    run = {
        "role": role, "unit_name": unit_name, "wall_ns": wall_ns,
        "returncode": result.returncode if result is not None else None,
        "process_result": result.systemd_result if result is not None else None,
        "process_reason": result.reason if result is not None else None,
        "process_refusal": launch_issue,
        "cleanup_proven": cleanup_proven, "cleanup_errors": cleanup_errors,
        "timed_out": timed_out or (result is not None and result.systemd_result == "timeout"),
        "network_namespace": net, "mount_namespace": mount,
        "network_attempts": attempts, "denied_connects": denied_connects,
        "containment_attempts": containment_attempts,
        "cgroup_peak_source": peak_source, "cgroup_path": str(scope) if scope is not None else None,
        "cgroup_before_bytes": before, "cgroup_peak_bytes": cgroup, "logs": logs,
    }
    supervision = folder / "supervision.json"
    write_json(supervision, {key: run[key] for key in SUPERVISION_FIELDS})
    logs[supervision.name] = descriptor(supervision)
    if gpu_issue is not None:
        run["gpu_refusal"] = {"rc": gpu_issue.rc, "code": gpu_issue.code}
    return run, cgroup, rss


def combined_checks(run, cgroup, rss, limits, *, cuda=False, cuda_record=None, gpu=False):
    telemetry, reasons = measurements(cgroup, rss, cuda=cuda, cuda_record=cuda_record)
    run.update(telemetry)
    checks = [(3, reason) for reason in reasons]
    if run["process_refusal"] is not None:
        checks.append((3, run["process_refusal"]))
    if not run["cleanup_proven"]:
        checks.append((3, "cleanup_unproven"))
    if gpu and "gpu_refusal" in run:
        checks.append((run["gpu_refusal"]["rc"], run["gpu_refusal"]["code"]))
    log_names = LOG_NAMES | GPU_LOG_NAMES if gpu else LOG_NAMES
    if (run["network_namespace"] is None or run.get("mount_namespace") is None or
            run["network_attempts"] is None or run["logs"].keys() != log_names):
        checks.append((3, "offline_evidence_unproven"))
    if (run["timed_out"] or run["process_result"] == "oom-kill" or
            (cgroup is not None and cgroup > limits.maximum)):
        checks.append((6, "candidate_time_budget" if run["timed_out"] else "memory_budget"))
    return checks


def output_evidence(output, inventory, schema):
    if output.is_symlink() or not output.is_dir():
        return {}, False
    records = {}
    valid = True
    expected = set(inventory)
    for path in sorted(output.iterdir()):
        if path.name not in expected or path.is_symlink() or not path.is_file():
            valid = False
            continue
        try:
            records[path.name] = descriptor(path)
        except (OSError, core.ContractError):
            valid = False
    if not valid or records.keys() != expected:
        return records, False
    try:
        core.validate(core.load_json(output / "result.json", max_bytes=JSON_LIMIT), schema)
    except core.ContractError:
        return records, False
    return records, True


def qualify(candidate, evidence_dir, *, timeout_s, memory_limits=DEFAULT_LIMITS, gpu=None):
    """Produce a new receipt. Evidence PASS still requires independent admission."""
    if type(timeout_s) is not int or not 1 <= timeout_s <= 86400:
        raise ValueError("timeout_s must be a positive proc runtime limit")
    if type(memory_limits) is not proc.MemoryLimits:
        raise TypeError("memory_limits must be proc.MemoryLimits")
    root = Path(evidence_dir).absolute()
    root.mkdir(mode=0o700)
    caller_inode = os.stat("/proc/self/ns/net").st_ino
    caller_mount_inode = os.stat("/proc/self/ns/mnt").st_ino
    receipt = {
        "schema": RECEIPT_SCHEMA, "candidate_sha256": None,
        "provider": candidate.get("provider") if type(candidate) is dict and
                    type(candidate.get("provider")) is str else None,
        "review_required": True, "caller_netns_inode": caller_inode,
        "caller_mountns_inode": caller_mount_inode, "runs": [],
        "timeout_s": timeout_s, "memory_limits": asdict(memory_limits), "infrastructure": {},
    }
    checks = []
    pins = Pins()
    try:
        if os.environ.get("AF_PROC_BACKEND") == "pidns" and memory_limits != DEFAULT_LIMITS:
            raise Refusal(3, "pidns_test_scope_limits_unproven")
        receipt["candidate_sha256"] = core.sha256_json(candidate)
        interpreter, entrypoint, schema, import_roots = check_candidate(candidate, pins)
        manifest = root / "candidate.json"
        write_json(manifest, candidate)
        pins.check({"path": str(manifest), "sha256": receipt["candidate_sha256"]})
        cuda = candidate["runtime"]["cuda"]
        if gpu is not None:
            if type(gpu) is not GpuLaunch or not callable(gpu.authorize) or not callable(gpu.release) or not cuda:
                raise Refusal(2, "gpu_device_policy_invalid")
            if os.environ.get("AF_PROC_BACKEND", "systemd") != "systemd" or asdict(memory_limits) != SCOPE_LIMITS:
                raise Refusal(3, "gpu_scope_unproven")
            try:
                devices = validate_devices(gpu.devices)
            except OSError as exc:
                raise Refusal(2, "gpu_device_policy_invalid") from exc
            # Freeze host policy bytes before starting either child.
            devices = [dict(node) for node in devices]
            gpu = GpuLaunch(devices, gpu.authorize, gpu.release, gpu.gpu_uuid)
            receipt["gpu_policy"] = {"devices": devices, "devices_sha256": core.sha256_json(devices),
                                     "gpu_uuid": gpu.gpu_uuid}
        pilot_pins = set(pins.files)
        tools, receipt["infrastructure"] = infrastructure(pins)

        def bindings(paths):
            return [[str(path), str(pins.files[path][1][0]), *pins.files[path][1][1:]]
                    for path in sorted(paths)]

        supervisor_pins = {tools["supervisor_python"], OFFLINE, SANDBOX}
        probe = [tools["supervisor_python"], "-I", "-B", OFFLINE, "probe"]
        control_limits = DEFAULT_LIMITS if gpu is not None else memory_limits
        run, cgroup, rss = run_offline(
            root, "denied-fetch", probe, tools, bindings(supervisor_pins),
            caller_inode, caller_mount_inode, timeout_s, control_limits, cuda=False)
        receipt["runs"].append(run)
        checks += combined_checks(run, cgroup, rss, control_limits)
        log = root / "denied-fetch" / "stdout"
        run["fetch_probe_passed"] = (run["returncode"] == 0 and run["network_attempts"] == 1 and
                                     run["denied_connects"] == 1 and log.is_file() and
                                     log.read_bytes() == (PROBE_MESSAGE + "\n").encode())
        if not run["fetch_probe_passed"]:
            checks.append((3, "denied_fetch_unproven"))
        if not checks:
            pins.verify_unchanged()
            output = root / "output"
            command = [interpreter, "-I", "-S", "-B", "-c", BOOTSTRAP,
                       entrypoint, manifest, WORK / "output", *import_roots]
            run, cgroup, rss = run_offline(
                root, "candidate", command, tools, bindings(pilot_pins | supervisor_pins),
                caller_inode, caller_mount_inode, timeout_s, memory_limits, cuda=cuda,
                **({"gpu": gpu, "candidate_sha256": receipt["candidate_sha256"]} if gpu is not None else {}))
            receipt["runs"].append(run)
            cuda_path = output / "cuda-peaks.json"
            cuda_record = None
            if cuda and not output.is_symlink() and not cuda_path.is_symlink() and cuda_path.is_file():
                cuda_record = read_json_evidence(cuda_path)
            checks += combined_checks(run, cgroup, rss, memory_limits, cuda=cuda, cuda_record=cuda_record,
                                      gpu=gpu is not None)
            if run["containment_attempts"]:
                checks.append((5, "candidate_containment_attempt"))
            if run["network_attempts"]:
                checks.append((5, "candidate_network_attempt"))
            if run["returncode"] not in (None, 0) and not run["timed_out"] and run["process_result"] != "oom-kill":
                checks.append((5, "candidate_run_failed"))
            run["outputs"], valid = output_evidence(output, candidate["outputs"], schema)
            if not valid and run["returncode"] == 0:
                checks.append((5, "candidate_output_contract"))
            if not checks and cuda:
                if gpu is None:
                    raise Refusal(3, "gpu_device_policy_unavailable")
                reconcile_gpu_evidence(root / "candidate", run, receipt["gpu_policy"],
                                       receipt["candidate_sha256"], output)
            pins.verify_unchanged()
    except Refusal as exc:
        checks.append((exc.rc, exc.code))
    except core.ContractError as exc:
        checks.append((2, exc.code))
    except OSError:
        checks.append((3, "qualification_evidence_unavailable"))

    # Witnessed violations take precedence over inconclusive measurements.
    priority = (2, 5, 4, 6, 3)
    codes = {code for code, _ in checks}
    rc = next((code for code in priority if code in codes), 0)
    receipt.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc],
                   reason_codes=list(dict.fromkeys(reason for _, reason in checks)))
    core.atomic_write_json(root / "qualification.json", receipt)
    return receipt
