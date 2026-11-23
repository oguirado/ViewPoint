"""Exploratory live capture + digest A/B on the same private Xvfb."""

from collections import defaultdict
from hashlib import sha256
import json
from math import ceil
import os
from pathlib import Path
from statistics import fmean, median
import sys
from time import perf_counter_ns, process_time_ns

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixture import COMPONENTS, PrivateX, PythonCapture, SURFACE
from capture import Capture

TIMED_CPUS = {16, 17, 18, 19}
WIDTH, HEIGHT = 1920, 1080
BLOCKS = 12
ARMS = ("python", "rust")


def check_affinity(*pids):
    for pid in pids:
        for tid in Path(f"/proc/{pid}/task").iterdir():
            if os.sched_getaffinity(int(tid.name)) != TIMED_CPUS:
                raise RuntimeError(f"unexpected affinity for task {tid.name}")


def check_memory_cap():
    path = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
    cgroup = Path("/sys/fs/cgroup") / path.lstrip("/")
    high = int((cgroup / "memory.high").read_text())
    maximum = int((cgroup / "memory.max").read_text())
    if (high, maximum) != (6 * 1024**3, 8 * 1024**3):
        raise RuntimeError("benchmark must run inside the assigned memory scope")
    return {"memory_high_bytes": high, "memory_max_bytes": maximum}


def arm_stats(samples):
    wall = sorted(sample[0] for sample in samples)
    process = [sample[1] for sample in samples]
    return {
        "verified_frames": len(samples),
        "wall_mean_ms": round(fmean(wall) / 1e6, 3),
        "wall_p50_ms": round(median(wall) / 1e6, 3),
        "wall_p95_ms": round(wall[ceil(len(wall) * 0.95) - 1] / 1e6, 3),
        "runner_cpu_mean_ms": round(fmean(process) / 1e6, 3),
    }


def benchmark(profile):
    caps = check_memory_cap()
    with PrivateX(WIDTH, HEIGHT) as x, Capture(x.display, x.pid) as native, PythonCapture(x) as legacy:
        check_affinity(os.getpid(), x.pid)
        if (native.info.width, native.info.height, native.info.row_stride, native.info.byte_len) != (
            WIDTH, HEIGHT, WIDTH * 3, WIDTH * HEIGHT * 3
        ):
            raise RuntimeError("capture geometry differs from the benchmark geometry")
        destination = bytearray(native.info.byte_len)
        expected = {}
        for pattern in (0, 1):
            x.pattern(pattern)
            pixels, digest = legacy.next_frame()
            rust_digest = native.next_frame(destination)
            if destination != pixels or rust_digest != digest or sha256(destination).digest() != digest:
                raise RuntimeError("live source parity failed")
            expected[pattern] = digest
        if expected[0] == expected[1]:
            raise RuntimeError("pattern change did not change the frame")

        def python():
            return legacy.next_frame()[1]

        def rust():
            return native.next_frame(destination)

        capture = {"python": python, "rust": rust}
        x.pattern(0)
        samples = defaultdict(list)
        for block in range(BLOCKS):
            order = ("python", "rust", "rust", "python")
            if block % 2:
                order = ("rust", "python", "python", "rust")
            for arm in order:
                for pattern in ((0, 1) if profile == "changing" else (0, 0)):
                    if profile == "changing":
                        x.pattern(pattern)
                    operation = capture[arm]
                    cpu_start = process_time_ns()
                    wall_start = perf_counter_ns()
                    digest = operation()
                    wall = perf_counter_ns() - wall_start
                    cpu = process_time_ns() - cpu_start
                    if digest != expected[pattern]:
                        raise RuntimeError(f"{arm} returned an unexpected frame on {profile}")
                    samples[arm].append((wall, cpu))
        check_affinity(os.getpid(), x.pid)

    return {
        "display": x.display,
        "geometry": [WIDTH, HEIGHT],
        "cpus": sorted(TIMED_CPUS),
        "blocks": BLOCKS,
        "caps": caps,
        "results": {arm: arm_stats(samples[arm]) for arm in ARMS},
    }


if __name__ == "__main__":
    files = (
        SURFACE / "vp_capture.h",
        COMPONENTS / "vision-engine/vadapt/x11_ramcap.py",
        COMPONENTS / "vision-engine/vadapt/xfb.py",
    )
    report = {profile: benchmark(profile) for profile in ("static", "changing")}
    report["sha256"] = {
        str(file.relative_to(COMPONENTS.parent)): sha256(file.read_bytes()).hexdigest()
        for file in files
    }
    print(json.dumps(report, indent=2, sort_keys=True))
