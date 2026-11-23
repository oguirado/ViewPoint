"""Collect local GB10 benchmark provenance and enforce sample placement."""

from hashlib import sha256
import os
from pathlib import Path
import subprocess
from time import clock_gettime_ns, CLOCK_MONOTONIC

from score import CPUS, DIRECT, MEMORY_HIGH, MEMORY_MAX, ORDER, require
from perception.receipts import verify_transition
from perception.stream import PerceptionStream

def now():
    return clock_gettime_ns(CLOCK_MONOTONIC)


class TimedStream(PerceptionStream):
    def _sample(self, token, minimum_index):
        observation = super()._sample(token, minimum_index)
        self.first_byte_ns = now()  # First readable, validated fresh publication.
        return observation


class TransitionVerifier:
    """Verify P4's publication hook for the synchronous T1 runner."""

    def __init__(self):
        self._checked = []

    def __call__(self, receipt, proof):
        self._checked.append((verify_transition(receipt, proof), proof))

    def call(self, owner, op):
        self._checked.clear()
        try:
            receipt = owner.vp_perception_call({"op": op, "session_id": owner.session_id, "reason": "t1"})
            require(len(self._checked) == 1, "P7 lacks independent verification")
            verified, proof = self._checked[0]
            require(verified == receipt and proof.session_id == owner.session_id, "P4/P7 receipt mismatch")
            states = [proof.prior_state, *(record["perception"] for record in proof.broker_records)]
            return receipt, verified, states, proof.observation
        finally:
            self._checked.clear()


def verify_frame(owner, observation):
    require(observation is not None, "Fresh observation absent")
    pixels = owner.read_frame(observation.observation_id)
    require(pixels and sha256(pixels).hexdigest() == observation.frame_sha256,
            "Fresh frame readback differs")


def scope_path():
    paths = [line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
             if line.startswith("0::")]
    require(len(paths) == 1, "AUSENTE: unified scope")
    return paths[0]


def scope_dir():
    return Path("/sys/fs/cgroup") / scope_path().lstrip("/")


def placement():
    cpus = sorted(os.sched_getaffinity(0))
    require(tuple(cpus) == CPUS, "Timed samples require taskset -c 16-19")
    scope = scope_dir()
    high = int((scope / "memory.high").read_text())
    maximum = int((scope / "memory.max").read_text())
    require((high, maximum) == (MEMORY_HIGH, MEMORY_MAX), "Incorrect benchmark scope limits")
    root = Path("/sys/fs/cgroup")
    effective, cpuset_inherited = None, None
    for directory in (scope, *scope.parents):
        if not directory.is_relative_to(root):
            break
        try:
            effective = (directory / "cpuset.cpus.effective").read_text().strip()
        except FileNotFoundError:
            continue
        cpuset_inherited = directory != scope
        break
    return {"affinity": cpus, "effective_cpuset": effective, "cpuset_inherited": cpuset_inherited,
            "memory_high_bytes": high, "memory_max_bytes": maximum}


def optional_counter(path):
    try:
        return int(path.read_text())
    except (OSError, ValueError):
        return None


def competing_paths(names, scope):
    root = Path("/sys/fs/cgroup")
    paths = {}
    for name in names:
        path = (root / name.lstrip("/")).resolve(strict=True)
        require(path.is_relative_to(root) and not scope.is_relative_to(path) and not path.is_relative_to(scope),
                "Competing cgroup overlaps the T1 scope or lies outside cgroupfs")
        require(path not in paths.values(), "Duplicate competing cgroup")
        paths[name] = path
    return paths


def throttle_paths(names):
    paths = {}
    for name in names:
        path = Path(name).resolve(strict=True)
        require(path.is_relative_to("/sys") and path not in paths.values(), "Invalid throttle source")
        paths[name] = path
    return paths


def host_sample(*, competing=None, throttle=None):
    scope = scope_dir()
    stamp = now()
    load = Path("/proc/loadavg").read_text().split()[:3]
    psi = {}
    for name in ("cpu", "memory"):
        psi[name] = {}
        for line in Path(f"/proc/pressure/{name}").read_text().splitlines():
            kind, *fields = line.split()
            psi[name][kind] = dict(field.split("=", 1) for field in fields)
    temperatures = [optional_counter(path) for path in Path("/sys/class/thermal").glob("thermal_zone*/temp")]
    throttle_by_cpu = {str(cpu): optional_counter(
        Path(f"/sys/devices/system/cpu/cpu{cpu}/thermal_throttle/core_throttle_count"))
        for cpu in CPUS}
    competing_sample = {}
    for name, path in (competing or {}).items():
        cpu = dict(line.split() for line in (path / "cpu.stat").read_text().splitlines())
        competing_sample[name] = {"memory_bytes": int((path / "memory.current").read_text()),
                                  "cpu_usec": int(cpu["usage_usec"])}
    memory = next(line for line in Path("/proc/meminfo").read_text().splitlines()
                  if line.startswith("MemAvailable:"))
    return {"mono_ns": stamp, "load1_5_15": list(map(float, load)), "psi": psi,
            "mem_available_bytes": int(memory.split()[1]) * 1024,
            "thermal_max_millic": max((t for t in temperatures if t is not None), default=None),
            "throttle_events_by_cpu": throttle_by_cpu, "uvm_watermarks": None,
            "throttle": {name: optional_counter(path) for name, path in (throttle or {}).items()},
            "competing": competing_sample,
            "scope_peak_bytes": optional_counter(scope / "memory.peak")}


def context(arm, order_index, *, workload, fixture_sha256, warmups, samples, competing=None, throttle=None):
    require(0 <= order_index < len(ORDER) and ORDER[order_index] == arm, "Invalid ABBA run position")
    return {"tag": "MEASURED", "clock": "CLOCK_MONOTONIC", "arm": arm, "order_index": order_index,
            "commit": subprocess.check_output(["git", "-C", str(DIRECT.parents[1]), "rev-parse", "HEAD"],
                                              text=True).strip(),
            "workload": workload, "fixture_sha256": fixture_sha256, "warmups": warmups,
            "samples": samples, "placement": placement(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "before": host_sample(competing=competing, throttle=throttle)}


def paired_receipt(scenario, benchmark, plan_sha256, samples):
    """Use the paired arm wrapper shared with T2 around the T1 scenario."""
    bench = dict(benchmark)
    placement = bench.pop("placement")
    cpuset = placement["effective_cpuset"]
    require(bool(cpuset), "AUSENTE: effective cpuset")
    spans = [tuple(map(int, group.split("-"))) for group in cpuset.split(",")]
    scope = {"affinity": placement["affinity"],
             "cpuset": [cpu for span in spans for cpu in range(span[0], span[-1] + 1)],
             "memory_high": placement["memory_high_bytes"], "memory_max": placement["memory_max_bytes"]}
    return {
        "arm": bench.pop("arm"), "numeric_evidence": bench.pop("tag"), "commit": bench.pop("commit"),
        "boot_id": bench.pop("boot_id"), "plan_sha256": plan_sha256, "scope": scope, "error": None,
        "start_mono_ns": scenario["start_mono_ns"], "end_mono_ns": scenario["end_mono_ns"],
        "samples": samples, "benchmark": bench, "scenario": scenario,
    }
