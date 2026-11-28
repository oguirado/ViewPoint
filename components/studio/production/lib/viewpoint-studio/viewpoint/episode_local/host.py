# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only media command runner for the scoped local profile."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import common as C

GIB = 1 << 30
FLOOR = 20 * GIB
CAP = 8 * GIB
GROUPS = Path("/sys/fs/cgroup")
LOCALE = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"}
RUNTIME = dict(LOCALE, PATH="/usr/bin:/bin", TMPDIR="/tmp", XDG_CACHE_HOME="/tmp",
               XDG_CONFIG_HOME="/tmp", XDG_RUNTIME_DIR="/tmp", PYTHONDONTWRITEBYTECODE="1",
               BLENDER_USER_CONFIG="/tmp", LIBGL_ALWAYS_SOFTWARE="1", GALLIUM_DRIVER="llvmpipe",
               CUDA_VISIBLE_DEVICES="", NVIDIA_VISIBLE_DEVICES="none", MESA_SHADER_CACHE_DISABLE="true",
               __EGL_VENDOR_LIBRARY_FILENAMES="/usr/share/glvnd/egl_vendor.d/50_mesa.json",
               EGL_PLATFORM="surfaceless", LP_NUM_THREADS="2", OMP_NUM_THREADS="1")


def available():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:") and line.split()[-1] == "kB":
            return int(line.split()[1]) * 1024
    raise RuntimeError("MemAvailable unavailable")


def checkpoint():
    memory = available()
    C.require(memory >= FLOOR, "ram_checkpoint_hold")
    return memory


def cap_bytes():
    configured = os.environ.get("VP_EPISODE_MEMORY_MAX", "8G")
    C.require(configured in ("8G", "16G"), "invalid_scope_memory_max")
    return int(configured[:-1]) * GIB


def admission():
    cap = cap_bytes()
    memory = checkpoint()
    if cap == 16 * GIB:
        headroom = memory - cap
        C.require(headroom >= FLOOR, "ram_admission_hold")
        return {"available_bytes": memory, "reservation_bytes": cap, "rule": "explicit-16G",
                "post_reservation_headroom_bytes": headroom}
    reservations = {}
    for scope in GROUPS.rglob("*.scope"):
        try:
            if (scope / "memory.max").read_text().strip() != str(CAP):
                continue
            if "populated 1" in (scope / "cgroup.events").read_text().splitlines():
                reservations[str(scope)] = int((scope / "memory.current").read_text())
        except (FileNotFoundError, ProcessLookupError):
            continue
    used = sum(max(0, CAP - value) if value >= 256 * (1 << 20) else value
               for value in reservations.values())
    headroom = memory - used - cap
    C.require(headroom >= FLOOR, "ram_admission_hold")
    return {"available_bytes": memory, "reserved_bytes": used,
            "post_reservation_headroom_bytes": headroom, "live_8g_scopes": reservations,
            "reservation_bytes": cap, "rule": "8G-live-reservations"}


def containment():
    path = Path("/proc/self/cgroup").read_text().strip().split("::")[-1].lstrip("/")
    group = GROUPS / path
    limits = {name: (group / name).read_text().strip()
              for name in ("memory.high", "memory.max", "memory.swap.max")}
    return path, limits, sorted(os.sched_getaffinity(0)), os.nice(0)


def matches_scope(cap, priority, observed):
    path, limits, affinity, niceness = observed
    expected = {"memory.high": str(6 * GIB), "memory.max": str(cap), "memory.swap.max": "0"}
    return path.endswith(".scope") and limits == expected and affinity == list(range(16)) and niceness == priority


def scoped(work, reads, commands):
    """Run media commands in a verified CPU scope and record each observed exit."""
    priority = max(15, os.nice(0))
    try:
        in_scope = matches_scope(cap_bytes(), priority, containment())
    except OSError:
        in_scope = False
    if in_scope:
        available_bytes = checkpoint()
        admitted = {"available_bytes": available_bytes, "reservation_bytes": cap_bytes(),
                    "additional_reservation_bytes": 0, "rule": "verified-existing-scope"}
    else:
        admitted = admission()
    receipt = work / "scope.json"
    plan = work / "scope-plan.json"
    C.write(plan, {"read_roots": [str(root) for root in reads], "commands": commands,
                   "admission": admitted, "niceness": priority})
    argv = [sys.executable, "-EB", "-m", "viewpoint.episode_local.host", str(plan)]
    if not in_scope:
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
                "-p", "MemoryHigh=6G", "-p", f"MemoryMax={admitted['reservation_bytes'] // GIB}G",
                "-p", "MemorySwapMax=0", "--", "nice", "-n", str(priority - os.nice(0)),
                "taskset", "-c", "0-15", *argv]
    runtime = Path(__file__).resolve().parents[2]
    result = subprocess.run(argv, cwd=runtime, env=dict(os.environ, **LOCALE), capture_output=True)
    if not receipt.exists():
        raise RuntimeError(f"scope exited {result.returncode}: {result.stderr.decode(errors='replace')[-400:]}")
    actual = C.load(receipt)
    actual["launcher_argv"] = argv
    actual["launcher_rc"] = result.returncode
    C.write(receipt, actual)
    if actual.get("reason"):
        raise C.Refusal(actual["reason"])
    if result.returncode != 0:
        raise RuntimeError(f"scope exited {result.returncode}: {result.stderr.decode(errors='replace')[-400:]}")
    return actual


def sandbox(work, roots):
    argv = ["bwrap", "--die-with-parent", "--unshare-net", "--new-session", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp", "--ro-bind", "/usr", "/usr"]
    for name in ("/lib", "/lib64", "/bin", "/sbin"):
        if Path(name).is_symlink():
            argv += ["--symlink", os.readlink(name), name]
        elif Path(name).exists():
            argv += ["--ro-bind", name, name]
    for name in ("/etc/alternatives", "/etc/ld.so.cache", "/etc/fonts"):
        if Path(name).exists():
            argv += ["--ro-bind", name, name]
    for root in dict.fromkeys(roots):
        if root != str(work):
            argv += ["--ro-bind", root, root]
    return [*argv, "--bind", str(work), str(work), "--chdir", str(work), "--"]


def inside(plan_path):
    work = plan_path.parent
    plan = C.load(plan_path)
    observed = containment()
    if not matches_scope(plan["admission"]["reservation_bytes"], plan["niceness"], observed):
        raise RuntimeError("effective scope mismatch")
    _, limits, affinity, niceness = observed
    record = {"admission": plan["admission"], "limits_bytes": {key: int(value) for key, value in limits.items()},
              "cpu_affinity": affinity, "niceness": niceness, "commands": [], "checkpoints_bytes": []}
    receipt = work / "scope.json"
    prefix = sandbox(work, plan["read_roots"])
    for command in plan["commands"]:
        try:
            record["checkpoints_bytes"].append(checkpoint())
        except C.Refusal as exc:
            record["reason"] = exc.reason
            C.write(receipt, record)
            return 1
        started = time.monotonic()
        with (work / (command["name"] + ".log")).open("wb") as stream:
            proc = subprocess.Popen([*prefix, *command["argv"]], env=RUNTIME,
                                    stdout=stream, stderr=subprocess.STDOUT)
            try:
                rc = proc.wait(timeout=command["timeout"])
            except subprocess.TimeoutExpired:
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait()
                rc = 124
        record["commands"].append({"argv": command["argv"], "name": command["name"],
                                   "rc": rc, "elapsed_s": round(time.monotonic() - started, 3)})
        C.write(receipt, record)
        if rc != 0:
            return 0  # the caller interprets the recorded child exit
    try:
        record["checkpoints_bytes"].append(checkpoint())
    except C.Refusal as exc:
        record["reason"] = exc.reason
        C.write(receipt, record)
        return 1
    C.write(receipt, record)
    return 0


if __name__ == "__main__":
    sys.exit(inside(Path(sys.argv[1])))
