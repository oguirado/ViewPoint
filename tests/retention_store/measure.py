"""Record scoped C08 baselines for the legacy recorder and the new store."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import resource
from statistics import median
import sys
import tempfile
from time import perf_counter_ns

from tests.retention_store.test_store import Fixture, INSPECTOR

SCOPE_LIMITS = {"memory.high": 6 << 30, "memory.max": 8 << 30, "memory.swap.max": 0}
SAMPLES = 30


def scope():
    found = next(line.split("::", 1)[1] for line in Path("/proc/self/cgroup").read_text().splitlines()
                 if line.startswith("0::"))
    directory = Path("/sys/fs/cgroup") / found.lstrip("/")
    limits = {key: int((directory / key).read_text()) for key in SCOPE_LIMITS}
    if not directory.name.endswith(".scope") or limits != SCOPE_LIMITS:
        raise RuntimeError("measurement is outside the required systemd resource scope")
    return found, directory, limits


def summarize(timings):
    return {name: {"samples": len(values), "median_ns": median(values),
                   "p95_ns": sorted(values)[(95 * len(values) + 99) // 100 - 1]}
            for name, values in timings.items()}


def measure(mode, samples=SAMPLES, parallelism=1):
    cgroup, cgroup_dir, limits = scope()
    times = defaultdict(list)

    def timed(name, call, *args):
        start = perf_counter_ns()
        result = call(*args)
        times[name].append(perf_counter_ns() - start)
        return result

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp).resolve()
        begin = perf_counter_ns()
        if mode == "reference":
            try:
                from tools.attempt_evidence import AttemptStore, record_digest
                from tests.test_attempt_evidence import example
            except ModuleNotFoundError as exc:
                if exc.name not in {"tools.attempt_evidence", "tests.test_attempt_evidence"}:
                    raise
                raise RuntimeError(
                    "reference mode requires tools.attempt_evidence and tests.test_attempt_evidence"
                ) from exc
            store = AttemptStore(path)

            def attempt_cycle(index):
                attempt = f"{index + 1:032x}"
                record, terminal = example("valid"), example("valid.observed")
                record["attempt_id"] = terminal["execution"]["attempt_id"] = attempt
                execution_sha = record_digest(terminal["execution"])
                terminal["artifact"]["execution_sha256"] = execution_sha
                terminal["cleanup"]["execution_sha256"] = execution_sha
                timed("start", store.dispatch, record, lambda _: None)
                args = (attempt, terminal["execution"], terminal["artifact"], b"hello",
                        terminal["cleanup"])
                timed("finish", store.finish, *args)
                timed("finish_exact_retry", store.finish, *args)
                if timed("inspect", store.verify, attempt)["state"] != "observed":
                    raise RuntimeError("reference readback did not verify")

            if parallelism == 1:
                for index in range(samples):
                    attempt_cycle(index)
            else:
                with ThreadPoolExecutor(max_workers=parallelism) as pool:
                    batch = parallelism * 4
                    for start in range(0, samples, batch):
                        list(pool.map(attempt_cycle, range(start, min(start + batch, samples))))
        elif mode == "retention" and parallelism == 1:
            fixture = Fixture()
            with fixture.store(path) as store:
                for index in range(samples):
                    attempt = f"{index + 1:032x}"
                    _, ack = timed("admit_start_claim", fixture.start, store, attempt)
                    first, created = timed("finish", fixture.finish, store, ack)
                    retry = timed("finish_exact_retry", fixture.finish, store, ack)
                    if not created or retry != (first, False):
                        raise RuntimeError("retention finish replay did not verify")
                    inspection = timed("inspect", store.inspect, INSPECTOR, attempt)
                    if inspection["state"] != "committed":
                        raise RuntimeError("retention readback did not verify")
        else:
            raise ValueError("unknown measurement mode")
        elapsed = perf_counter_ns() - begin
        return {
            "provenance": "[MEASURED]",
            "attempts": samples,
            "parallelism": parallelism,
            "elapsed_ns": elapsed,
            "attempts_per_second": samples * 1_000_000_000 / elapsed,
            "latency": summarize(times),
            "ram_peak": {
                "process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                "scope_memory_peak_bytes": int((cgroup_dir / "memory.peak").read_text()),
            },
            "scope": {"path": cgroup, **limits},
        }


if __name__ == "__main__":
    samples = int(sys.argv[2]) if len(sys.argv) > 2 else SAMPLES
    parallelism = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    print(json.dumps(measure(sys.argv[1], samples, parallelism), indent=2, sort_keys=True))
