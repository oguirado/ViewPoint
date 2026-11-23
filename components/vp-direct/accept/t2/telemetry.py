"""Read-only GB10 telemetry for one capped benchmark arm."""

import os
from pathlib import Path
from threading import Event, Thread
from time import CLOCK_MONOTONIC, clock_gettime_ns

from score import CPUS, INTERVAL, WINDOW, require

CGROUP = Path("/sys/fs/cgroup")


def now():
    return clock_gettime_ns(CLOCK_MONOTONIC)


def cpu_list(spec):
    cpus = []
    for item in spec.split(","):
        ends = [int(part) for part in item.split("-")]
        cpus.extend(range(ends[0], ends[-1] + 1))
    return cpus


def effective_cpuset(cgroup):
    # Scope leaves can inherit cpuset from the closest enabled ancestor.
    for parent in (cgroup, *cgroup.parents):
        if not parent.is_relative_to(CGROUP):
            break
        source = parent / "cpuset.cpus.effective"
        if source.is_file():
            return cpu_list(source.read_text().strip()), source
    require(False, "effective cpuset AUSENTE")


def signature():
    paths = [line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")]
    require(len(paths) == 1, "cgroup v2 scope unavailable")
    cgroup = CGROUP / paths[0].lstrip("/")
    cpuset, source = effective_cpuset(cgroup)
    scope = {"path": paths[0], "affinity": sorted(os.sched_getaffinity(0)),
             "cpuset": cpuset, "cpuset_source": str(source),
             "memory_high": int((cgroup / "memory.high").read_text()),
             "memory_max": int((cgroup / "memory.max").read_text())}
    require(cgroup.name.endswith(".scope") and scope["affinity"] == CPUS and set(CPUS) <= set(scope["cpuset"])
            and (scope["memory_high"], scope["memory_max"]) == (6 * 1024**3, 8 * 1024**3),
            "benchmark requires the capped scope and assigned CPUs")
    return scope, cgroup


def fields(path):
    return dict(line.split(maxsplit=1) for line in path.read_text().splitlines())


def pressure(path):
    rows = fields(path)
    return {kind: [float(dict(token.split("=") for token in rows[kind].split())[key])
                   for key in ("avg10", "avg60", "avg300")] for kind in ("some", "full")}


class HostTelemetry:
    def __init__(self, cgroup, competing_names, throttle_paths):
        self.cgroup = cgroup
        self.thermal_paths = sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
        self.throttle_paths = sorted(set(Path(path).resolve(strict=True) for path in throttle_paths))
        require(all(path.is_relative_to("/sys") for path in self.throttle_paths), "throttle source must be under sysfs")
        self.competing = {}
        for name in competing_names:
            path = (CGROUP / name.lstrip("/")).resolve(strict=True)
            require(path.is_relative_to(CGROUP) and path != CGROUP
                    and not cgroup.is_relative_to(path) and not path.is_relative_to(cgroup),
                    "competing cgroup overlaps the benchmark scope")
            require(path not in self.competing.values(), "duplicate competing cgroup")
            self.competing[name] = path

    def read(self):
        started = now()
        mem = fields(Path("/proc/meminfo"))["MemAvailable:"].split()
        require(mem[1:] == ["kB"], "MemAvailable units unknown")
        available = int(mem[0]) * 1024
        current = int((self.cgroup / "memory.current").read_text())
        competing = {name: {"memory_bytes": int((path / "memory.current").read_text()),
                            "cpu_usec": int(fields(path / "cpu.stat")["usage_usec"])}
                     for name, path in self.competing.items()}
        sample = {
            "mono_ns": started, "mem_available_bytes": available, "load": os.getloadavg(),
            "cpu_psi": pressure(Path("/proc/pressure/cpu")),
            "memory_psi": pressure(Path("/proc/pressure/memory")),
            "scope_cpu_psi": pressure(self.cgroup / "cpu.pressure"),
            "scope_memory_psi": pressure(self.cgroup / "memory.pressure"),
            "scope_memory_current": current, "scope_memory_peak": int((self.cgroup / "memory.peak").read_text()),
            "thermal_millic": max((int(path.read_text()) for path in self.thermal_paths), default=None),
            "throttle": {str(path): int(path.read_text()) for path in self.throttle_paths},
            "competing": competing,
        }
        sample["end_mono_ns"] = now()
        return sample


class Sampler:
    def __init__(self, telemetry, samples, start):
        self.telemetry, self.samples, self.start = telemetry, samples, start
        self.stop = Event()
        self.error = None
        self.thread = Thread(target=self._run)

    def _run(self):
        try:
            for due in range(self.start, self.start + WINDOW, INTERVAL):
                if self.stop.wait(max(0, due - now()) / 1_000_000_000):
                    return
                sample = self.telemetry.read()
                require(sample["end_mono_ns"] < due + INTERVAL, "missed resource sampling slot")
                self.samples.append(sample)
            self.stop.wait(max(0, self.start + WINDOW - now()) / 1_000_000_000)
        except Exception as exc:
            self.error = exc

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, error, *_):
        if error is not None:
            self.stop.set()
        self.thread.join()
        if error is None and self.error is not None:
            raise self.error
