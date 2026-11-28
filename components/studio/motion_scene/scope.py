# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only execution, RAM admission and effective scope readback on Linux."""
from datetime import datetime, time
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from evidence import Rejected, demand

GIB = 1 << 30
CGROUP = Path("/sys/fs/cgroup")
CPUS = tuple(range(16))
LIMITS = {"memory.high": 6 * GIB, "memory.max": 8 * GIB, "memory.swap.max": 0}
ENVIRONMENT = {
    "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC",
    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
    "CUDA_VISIBLE_DEVICES": "", "NVIDIA_VISIBLE_DEVICES": "none",
    "LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "llvmpipe", "EGL_PLATFORM": "surfaceless",
    "MESA_SHADER_CACHE_DISABLE": "true", "BLENDER_USER_RESOURCES": "",
    "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "LP_NUM_THREADS": "4",
}


def environment():
    result = os.environ | ENVIRONMENT
    for key in ("DISPLAY", "WAYLAND_DISPLAY"):
        result.pop(key, None)
    return result


def available():
    row, = (line for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:"))
    _, count, unit = row.split()
    if unit != "kB":
        raise RuntimeError("unverified_mem_available")
    return int(count) * 1024


def checkpoint(quiet_hours=None):
    if quiet_hours:
        start, end, zone = quiet_hours
        now = datetime.now(ZoneInfo(zone)).time()
        start, end = time.fromisoformat(start), time.fromisoformat(end)
        inside = start <= now < end if start <= end else now >= start or now < end
        if inside:
            raise Rejected("quiet_hours", f"local time is outside {start}–{end} in {zone}")
    memory = available()
    if memory < 20 * GIB:
        raise Rejected("insufficient_ram_at_checkpoint", "available memory reaches 20 GiB")
    return memory


def admission(quiet_hours=None):
    checkpoint(quiet_hours)
    scopes = {}
    for scope in sorted(CGROUP.rglob("*.scope")):
        if not (scope / "memory.max").is_file():
            continue
        try:
            if (scope / "memory.max").read_text().strip() == str(8 * GIB):
                scopes[str(scope.relative_to(CGROUP))] = int((scope / "memory.current").read_text())
        except FileNotFoundError:
            if scope.exists():
                raise
    memory = checkpoint(quiet_hours)
    reserved = sum(max(0, 8 * GIB - usage) for usage in scopes.values() if usage >= GIB // 4)
    remaining = memory - reserved - 8 * GIB
    if remaining < 20 * GIB:
        raise Rejected(f"insufficient_ram_headroom:{remaining}", "projected memory reaches 20 GiB")
    return {"available_bytes": memory, "live_8g_scopes_bytes": scopes,
            "reserve_bytes": reserved, "requested_bytes": 8 * GIB,
            "projected_headroom_bytes": remaining}


def effective(parent_nice):
    row, = (line for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::"))
    group = CGROUP / row.removeprefix("0::").lstrip("/")
    limits = {key: int((group / key).read_text()) for key in LIMITS}
    cpus, nice = tuple(sorted(os.sched_getaffinity(0))), os.nice(0)
    demand(group.suffix == ".scope" and cpus == CPUS and limits == LIMITS and nice == parent_nice
           and not ({"DISPLAY", "WAYLAND_DISPLAY"} & os.environ.keys())
           and os.environ.get("CUDA_VISIBLE_DEVICES") == "", "effective_scope_mismatch")
    return {"limits_bytes": limits, "cpus": list(cpus), "nice": nice}


def command(argv):
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            "--expand-environment=no", "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G",
            "-p", "MemorySwapMax=0", "--", "taskset", "-c", "0-15", *argv]
