# SPDX-License-Identifier: AGPL-3.0-only
"""Containment and RAM admission, read back from the kernel for the replay entry point.

The lane rules for heavy work are a user scope with MemoryHigh=6G, MemoryMax=8G and
MemorySwapMax=0, CPUs 0-15, and the review harness target of niceness 15. A command that is
supposed to run contained asks this module; the readback never trusts an environment flag.
"""
import json
import os
import sys
from pathlib import Path

GIB = 1 << 30
TARGET = {"memory_high": 6 * GIB, "memory_max": 8 * GIB, "memory_swap_max": 0, "cpus": list(range(16)),
          "niceness": 15}
TARGET_PROVENANCE = "CPU-only media scope configuration"
MIN_HEADROOM = 20 * GIB
NEW_SCOPE = 8 * GIB
IDLE = GIB // 4
USER_SLICE = Path("/sys/fs/cgroup") / "user.slice" / f"user-{os.getuid()}.slice"
DISPLAY_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")


class Unreadable(Exception):
    """A figure the rule needs cannot be read. Process exit 2."""


def _text(path):
    return Path(path).read_text().strip()


def _limit(path):
    value = _text(path)
    return None if value == "max" else int(value)


def own_cgroup():
    for line in _text("/proc/self/cgroup").splitlines():
        if line.startswith("0::"):
            return line.split("::", 1)[1].strip().lstrip("/")
    raise Unreadable("the cgroup v2 path of this process is unreadable")


def containment():
    """Limits this process actually runs under, beside the limits the rules require."""
    cgroup = own_cgroup()
    root = Path("/sys/fs/cgroup") / cgroup
    try:
        seen = {"memory_high": _limit(root / "memory.high"), "memory_max": _limit(root / "memory.max"),
                "memory_swap_max": _limit(root / "memory.swap.max"), "cpus": sorted(os.sched_getaffinity(0)),
                "niceness": os.getpriority(os.PRIO_PROCESS, 0)}
        current = int(_text(root / "memory.current"))
    except (OSError, ValueError) as exc:
        raise Unreadable(f"containment of {cgroup} is unreadable: {exc}") from exc
    problems = [name for name, value in TARGET.items() if seen[name] != value]
    displays = sorted(key for key in DISPLAY_KEYS if os.environ.get(key))
    if displays:
        problems.append("display_set")
    return {"cgroup": f"/{cgroup}", "seen": seen, "target": TARGET, "target_provenance": TARGET_PROVENANCE,
            "memory_current_bytes": current, "display_keys_set": displays, "problems": problems,
            "contained": not problems}


def admission(meminfo="/proc/meminfo", user_slice=USER_SLICE):
    """The RAM admission of the lane rule of 25 Nov, with its figure.

    Admit only if MemAvailable - reserved - 8 GiB >= 20 GiB, where reserved sums, over live 8 GiB
    scopes, (8 GiB - current use) for scopes using at least 256 MiB and current use for idle ones.
    """
    try:
        available = next(int(line.split()[1]) for line in _text(meminfo).splitlines()
                         if line.startswith("MemAvailable:")) * 1024
        busy = idle = reserved = 0
        scopes = []
        for directory, _, names in os.walk(user_slice):
            if not directory.endswith(".scope") or "memory.max" not in names:
                continue
            scope = Path(directory)
            try:
                if _limit(scope / "memory.max") != NEW_SCOPE or "populated 1" not in _text(scope / "cgroup.events").splitlines():
                    continue
                use = int(_text(scope / "memory.current"))
            except (FileNotFoundError, ProcessLookupError):
                continue
            live = use >= IDLE
            reserved += max(0, NEW_SCOPE - use) if live else use
            busy, idle = busy + live, idle + (use < IDLE)
            scopes.append({"scope": Path(directory).name, "current_bytes": use, "counts_as_busy": live})
    except (OSError, StopIteration, ValueError) as exc:
        raise Unreadable(f"RAM admission figures are unreadable: {exc}") from exc
    headroom = available - reserved - NEW_SCOPE
    admitted = headroom >= MIN_HEADROOM and available >= MIN_HEADROOM
    return {"status": "ADMIT" if admitted else "HOLD", "admitted": admitted, "available_bytes": available,
            "reserved_bytes": reserved, "new_scope_bytes": NEW_SCOPE, "projected_headroom_bytes": headroom,
            "minimum_bytes": MIN_HEADROOM, "live_8g_scopes_busy": busy, "live_8g_scopes_idle": idle,
            "live_8g_scopes": scopes}


def checkpoint():
    """Between stages: hold while MemAvailable itself is under 20 GiB."""
    try:
        available = next(int(line.split()[1]) for line in _text("/proc/meminfo").splitlines()
                         if line.startswith("MemAvailable:")) * 1024
    except (OSError, StopIteration, ValueError) as exc:
        raise Unreadable(f"MemAvailable is unreadable: {exc}") from exc
    return {"status": "ADMIT" if available >= MIN_HEADROOM else "HOLD", "available_bytes": available,
            "minimum_bytes": MIN_HEADROOM}


RETRY = {
    "containment": ("not_contained",
                    "inside systemd-run --user --scope -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0, "
                    "taskset -c 0-15, at niceness 15"),
    "admission": ("ram_admission_hold", "MemAvailable minus the reserve of live 8 GiB scopes minus 8 GiB is at least 20 GiB"),
    "checkpoint": ("ram_checkpoint_hold", "MemAvailable is at least 20 GiB"),
}
ENTRY = ["python3", "-B", str(Path(__file__).resolve().parent / "rp_review.py")]


def run(verb, require=False):
    """(payload, exit) of one readback. Without --require, containment is only reported."""
    try:
        if verb == "containment":
            data, ok = containment(), containment()["contained"]
        elif verb == "admission":
            data, ok = admission(), admission()["admitted"]
        elif verb == "checkpoint":
            data, ok = checkpoint(), checkpoint()["status"] == "ADMIT"
        else:
            return {"status": "HARNESS_ERROR", "error": "select containment, admission or checkpoint"}, 2
    except Unreadable as exc:
        return {"status": "HARNESS_ERROR", "error": str(exc)}, 2
    if ok or (verb == "containment" and not require):
        return data, 0
    reason, retry_when = RETRY[verb]
    return {"status": "HOLD", "reason": reason, "retry_when": retry_when, "next_command": ENTRY, **data}, 1


def main(argv):
    verb = argv[1] if len(argv) > 1 else ""
    data, code = run(verb, "--require" in argv)
    print(json.dumps(data, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
