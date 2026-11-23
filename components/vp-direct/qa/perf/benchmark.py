"""Profile the real P3 → P5 → VServe path on a synthetic RAM-only fixture."""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import struct
import sys
from tempfile import TemporaryDirectory
from time import CLOCK_MONOTONIC, clock_gettime_ns

DIRECT = Path(os.environ["VP_PERF_DIRECT"]).resolve(strict=True)
sys.path.insert(0, str(DIRECT))

from perception.pool import PerceptionPool, PoolError
from perception.pool import pool as pool_module
from perception.pool.transport import VisionWorker
from perception.stream import PerceptionStream, StreamError
from vadapt.rtring import ring_bytes
from vadapt.x11_ramcap import SourceConfig

CPUS = set(range(16, 20))
PROFILES = ((32, 32), (1920, 1080))
SEED = 521
WARMUPS = 2


def monotonic_ns():
    return clock_gettime_ns(CLOCK_MONOTONIC)


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def verify_affinity(pid):
    # Follow all existing children, including children of worker threads.
    tasks = list(Path(f"/proc/{pid}/task").iterdir())
    children = set()
    for task in tasks:
        require(os.sched_getaffinity(int(task.name)) == CPUS, "unexpected CPU affinity")
        children.update(int(child) for child in (task / "children").read_text().split())
    return {pid: len(tasks)} | {
        child: threads for child_pid in sorted(children)
        for child, threads in verify_affinity(child_pid).items()
    }


def read_kv(path):
    return {key: int(value) for key, value in
            (line.split() for line in path.read_text().splitlines())}


def host_snapshot(scope):
    proc_stat = Path("/proc/stat").read_text().splitlines()
    cpu_ticks = {parts[0]: [int(tick) for tick in parts[1:9]]
                 for parts in (line.split() for line in proc_stat)
                 if parts[0] in {f"cpu{i}" for i in range(20)}}
    thermal = Path("/sys/class/thermal")
    zones = {zone.name: int((zone / "temp").read_text())
             for zone in sorted(thermal.glob("thermal_zone*"))}
    cooling = {device.name: int((device / "cur_state").read_text())
               for device in sorted(thermal.glob("cooling_device*"))
               if (device / "type").read_text().strip() == "Processor"}
    cpufreq = Path("/sys/devices/system/cpu/cpufreq")
    limits = {cpu: int((cpufreq / f"policy{cpu}" / "perf_limited").read_text())
              for cpu in sorted(CPUS)}
    available_kib = next(int(line.split()[1])
                         for line in Path("/proc/meminfo").read_text().splitlines()
                         if line.startswith("MemAvailable:"))
    require(zones and cooling and len(cpu_ticks) == 20, "missing host telemetry")
    return {
        "mono_ns": monotonic_ns(),
        "loadavg": os.getloadavg(),
        "mem_available_kib": available_kib,
        "cpu_psi": Path("/proc/pressure/cpu").read_text().splitlines(),
        "memory_psi": Path("/proc/pressure/memory").read_text().splitlines(),
        "temperature_millicelsius": zones,
        "processor_cooling_state": cooling,
        "cpu_perf_limited": limits,
        "cpu_ticks": cpu_ticks,
        "memory_current_bytes": int((scope / "memory.current").read_text()),
        "memory_peak_bytes": int((scope / "memory.peak").read_text()),
        "cgroup_cpu_stat": read_kv(scope / "cpu.stat"),
    }


def pss_kib(pids):
    return {
        pid: next(int(line.split()[1]) for line in
                  Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()
                  if line.startswith("Pss:"))
        for pid in pids
    }


def make_fixture(width, height, *, alternate=False):
    rng = random.Random(SEED)
    rgb_row = rng.randbytes(width * 3)
    rgb_tail = rgb_row
    if alternate:
        # Change a tile near the end of the buffer so equality must inspect the common prefix.
        tail_bytes = min(width, 32) * 3
        rgb_tail = rgb_row[:-tail_bytes] + bytes(255 - value for value in rgb_row[-tail_bytes:])

    def pack(row):
        packed = bytearray(width * 4)
        packed[0::4], packed[1::4], packed[2::4] = row[2::3], row[1::3], row[0::3]
        return packed

    tail_rows = min(height, 32)
    head_rows = height - tail_rows
    header = (100, 7, 2, 24, width, height, 0, 0, 32, 0, 32, 32, 4 * width,
              4, 0xff0000, 0xff00, 0xff, 8, 0, 0, width, height, 0, 0, 0)
    fixture = struct.pack(">25I", *header) + pack(rgb_row) * head_rows + pack(rgb_tail) * tail_rows
    return fixture, sha256(rgb_row * head_rows + rgb_tail * tail_rows).hexdigest()


class TimedPool(PerceptionPool):
    """Observe budget admission without replacing its check."""

    def _check_budget(self, size):
        super()._check_budget(size)
        self.admit_ns = monotonic_ns()


class TimedStream(PerceptionStream):
    def read(self, token, observation_id):
        self.read_begin_ns = monotonic_ns()
        pixels = super().read(token, observation_id)
        self.first_byte_ns = monotonic_ns()
        return pixels


class TimedWorker(VisionWorker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        describe = self._vserve.describe

        def timed_backend(*args, **kwargs):
            self.backend_begin_ns = monotonic_ns()
            result = describe(*args, **kwargs)
            self.backend_end_ns = monotonic_ns()
            return result

        self._vserve.describe = timed_backend

    def describe(self, transport, frame, previous, masks):
        self.begin_ns = monotonic_ns()
        result = super().describe(transport, frame, previous, masks)
        self.end_ns = monotonic_ns()
        self.pid = self._vserve._proc.pid
        return result


def expect_refusal(records, name, operation, exception, expected):
    try:
        operation()
    except exception as error:
        require(str(error) == expected, f"wrong refusal for {name}: {error}")
        records.append({"name": name, "reason": str(error)})
    else:
        raise AssertionError(f"missing refusal for {name}")


def measure(profile, binding, workspace, scope, count, block, *, changing=False):
    width, height = profile
    total_bytes = width * height * 3
    fixture_path = workspace / "source.xwd"
    fixture, frame_digest = make_fixture(width, height)
    fixture_path.write_bytes(fixture)
    engine = DIRECT.parent / "vision-engine"
    report = {"profile": profile, "frame_workload": "changing" if changing else "unchanged",
              "fixture_sha256": sha256(fixture).hexdigest(), "frame_sha256": frame_digest,
              "runs": [], "session_open_ns": [], "refusals": []}
    states = ("ASLEEP", "OFF", "ACTIVE")
    if changing:
        alternate, alternate_digest = make_fixture(width, height, alternate=True)
        require(alternate_digest != frame_digest, "changing fixture repeats its frame")
        report.update(alternate_fixture_sha256=sha256(alternate).hexdigest(),
                      alternate_frame_sha256=alternate_digest)
        states = ("ACTIVE",)
    for state in states:
        pool = TimedPool(engine, max_sessions=1, max_frame_bytes=total_bytes,
                         max_reserved_bytes=2 * total_bytes, transport="vserve",
                         timeout=10, worker_factory=TimedWorker)
        ring_capacity = ring_bytes(2, width, height)
        stream = TimedStream(binding, SourceConfig(str(workspace), "source.xwd", width, height,
                                                  "synthetic XWD fixture"),
                             ring_slots=2, interval_ns=1_000, ttl_ns=10**11,
                             max_frame_bytes=4 * width * height + 100,
                             max_ring_bytes=ring_capacity,
                             workspace_root=workspace / "rings")
        try:
            opened_ns = monotonic_ns()
            session = pool.open_session("perf", binding=binding, trust_domain="fixture")
            report["session_open_ns"].append(monotonic_ns() - opened_ns)
            expect_refusal(report["refusals"], "pool_saturation",
                           lambda: pool.open_session("extra", binding=binding, trust_domain="fixture"),
                           PoolError, "session capacity exhausted")
            observation = None if state == "OFF" else stream.wake()
            worker_pid = None
            for index in range(1 + WARMUPS + count):
                expected_digest = frame_digest
                if changing:
                    current, expected_digest = (alternate, alternate_digest) if index % 2 else (fixture, frame_digest)
                    fixture_path.write_bytes(current)
                if state == "ASLEEP":
                    stream.pause(observation.token)
                    expect_refusal(report["refusals"], "retired_asleep_token",
                                   lambda: stream.read(observation.token, observation.observation_id),
                                   StreamError, "stale stream token")
                    require(stream.status()["reserved_bytes"] == ring_capacity,
                            "asleep producer released its ring early")
                elif state == "OFF":
                    stream.close()
                    require(stream.status()["reserved_bytes"] == 0, "OFF ring remains charged")
                    if observation is not None:
                        expect_refusal(report["refusals"], "retired_off_token",
                                       lambda: stream.read(observation.token, observation.observation_id),
                                       StreamError, "stale stream token")
                source_submit_ns = monotonic_ns()
                observation = (stream.observe(observation.token) if state == "ACTIVE"
                               else stream.wake())
                submit_ns = monotonic_ns()
                result = session.describe_observation(stream, observation, width=width, height=height)
                complete_ns = monotonic_ns()
                worker = session._partition.worker
                processes = verify_affinity(os.getpid())
                require(worker.pid in processes, "VServe is outside the measured process tree")
                require(worker_pid in (None, worker.pid), "VServe restarted in a warm stratum")
                worker_pid = worker.pid
                require(result.diagnostics.last_transport == "vserve"
                        and result.diagnostics.last_fallback_reason is None
                        and result.diagnostics.last_vserve_error is None, "hidden fallback")
                require(observation.frame_sha256 == expected_digest
                        and result.description.frame_sha256 == expected_digest
                        and result.description.changed is
                        (changing if state == "ACTIVE" and index else None), "frame/history drift")
                usage = asdict(pool.status())
                require(usage == dict(active_sessions=1, retiring_sessions=0, inflight=0,
                                      partitions=1, reserved_bytes=total_bytes), "pool charge drift")
                require(source_submit_ns <= submit_ns <= pool.admit_ns <= stream.read_begin_ns
                        <= stream.first_byte_ns <= worker.begin_ns <= worker.backend_begin_ns
                        <= worker.backend_end_ns <= worker.end_ns <= complete_ns,
                        "incomplete or out-of-order phase markers")
                elapsed = complete_ns - submit_ns
                input_read = stream.first_byte_ns - stream.read_begin_ns
                backend = worker.backend_end_ns - worker.backend_begin_ns
                report["runs"].append({
                    "block": block, "state_before_source": state,
                    "stratum": "cold" if index == 0 else "warmup" if index <= WARMUPS else "warm",
                    "frame_sha256": result.description.frame_sha256, "changed": result.description.changed,
                    "source_submit_ns": source_submit_ns, "submit_ns": submit_ns,
                    "admit_ns": pool.admit_ns, "first_byte_ns": stream.first_byte_ns,
                    "complete_ns": complete_ns, "queue_ns": pool.admit_ns - submit_ns,
                    "wake_ns": stream.first_byte_ns - pool.admit_ns,
                    "end_to_end_ns": elapsed, "source_transition_ns": submit_ns - source_submit_ns,
                    "input_read_ns": input_read, "backend_ns": backend,
                    "pool_transport_ns": worker.end_ns - worker.begin_ns - backend,
                    "backend_reported_us": result.description.timing_us,
                    "pool_residual_ns": elapsed - input_read - backend,
                    "pool_usage": usage, "pids_and_thread_counts": processes,
                    "pss_kib": pss_kib(processes) if index in (0, WARMUPS + count) else None,
                    "host": host_snapshot(scope),
                })
        finally:
            try:
                stream.close()
            finally:
                pool.close()
        require(pool.status().reserved_bytes == 0 and stream.status()["reserved_bytes"] == 0,
                "storage charge did not settle")
        require(len(verify_affinity(os.getpid())) == 1, "owned VServe child did not exit")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--arm", required=True, choices=("baseline", "candidate"))
    parser.add_argument("--samples", type=int, default=6, choices=range(1, 21))
    args = parser.parse_args()
    require(verify_affinity(os.getpid()), "benchmark affinity is unavailable")
    cgroup = Path("/proc/self/cgroup").read_text().strip().removeprefix("0::/")
    scope = Path("/sys/fs/cgroup") / cgroup
    limits = {name: int((scope / name).read_text()) for name in ("memory.high", "memory.max")}
    require(limits == {"memory.high": 6 * 2**30, "memory.max": 8 * 2**30},
            "scope memory limits are missing")
    before = host_snapshot(scope)
    require(before["loadavg"][1] <= 24 and before["mem_available_kib"] >= 20 * 2**20,
            "host outside load or memory bounds")
    binding_bytes = (DIRECT / "schemas/tests/examples/vp.surface_binding_v1.valid.json").read_bytes()
    binding = json.loads(binding_bytes)
    report = {"base_commit": args.base, "arm": args.arm, "clock": "CLOCK_MONOTONIC",
              "cpu_affinity": sorted(CPUS), "limits_bytes": limits, "seed": SEED,
              "benchmark_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
              "pool_sha256": sha256(Path(pool_module.__file__).read_bytes()).hexdigest(),
              "stream_sha256": sha256((DIRECT / "perception/stream/stream.py").read_bytes()).hexdigest(),
              "binding_sha256": sha256(binding_bytes).hexdigest(), "warmups": WARMUPS,
              "warm_samples_per_state_per_block": args.samples, "before": before,
              "blocks": [], "failures": []}
    try:
        with TemporaryDirectory(dir="/dev/shm") as ram:
            profiles = (*PROFILES, *reversed(PROFILES))
            for changing in (False, True):
                for profile in profiles:
                    report["blocks"].append(measure(profile, binding, Path(ram), scope,
                                                    args.samples, len(report["blocks"]) + 1,
                                                    changing=changing))
    except Exception as error:
        report["failures"].append({"block": len(report["blocks"]) + 1,
                                   "type": type(error).__name__, "reason": str(error)})
        print(json.dumps(report, separators=(",", ":")), flush=True)
        raise
    report["after"] = host_snapshot(scope)
    print(json.dumps(report, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
