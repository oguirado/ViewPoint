"""Record synthetic serial end-to-end measurements for local-open and TLS."""

import argparse
import json
import os
from math import ceil
from pathlib import Path
from statistics import median
from time import perf_counter

from .local import FROZEN_DIR
from .test_e2e import SyntheticREA
from .test_local import LocalREA


def scope():
    row = next(line for line in Path("/proc/self/cgroup").read_text().splitlines()
               if line.startswith("0::"))
    relative = row.removeprefix("0::")
    if not relative.endswith(".scope"):
        raise RuntimeError("measurement requires a systemd scope")
    base = Path("/sys/fs/cgroup") / relative.lstrip("/")
    limits = {key: int((base / key).read_text()) for key in
              ("memory.high", "memory.max", "memory.swap.max")}
    if tuple(limits.values()) != (6 << 30, 8 << 30, 0):
        raise RuntimeError("measurement scope differs from the required limits")
    return relative, base, limits


def rss_peak_bytes(pid):
    status = Path(f"/proc/{pid}/status").read_text()
    high_water = next(line for line in status.splitlines() if line.startswith("VmHWM:"))
    _, amount, unit = high_water.split()
    if unit != "kB":
        raise RuntimeError("unexpected Linux RSS unit")
    return int(amount) * 1024


def measure(samples):
    scope_name, cgroup, limits = scope()
    data = b"synthetic-rea-recording" * 52_000
    rows = {}
    os.environ.setdefault("RETENTION_FROZEN_DIR", str(FROZEN_DIR))
    for case, profiles in ((LocalREA, ("unix_local_open",)), (SyntheticREA, ("unix", "tcp_tls"))):
        case.setUpClass()
        try:
            for transport in profiles:
                test = case()
                try:
                    test.setUp()
                    with test.server() as server:
                        if Path(f"/proc/{server.pid}/cgroup").read_text().strip() != f"0::{scope_name}":
                            raise RuntimeError("server escaped the measurement scope")
                        test.ready(transport, "writer" if case is LocalREA else "runner")
                        test.ready(transport, "inspector")
                        latencies = []
                        begin = perf_counter()
                        for _ in range(samples):
                            start = perf_counter()
                            attempt = test.attempt(transport, recording=data)
                            test.readback(transport, attempt, recording=data)
                            latencies.append(perf_counter() - start)
                        duration = perf_counter() - begin
                        rows[transport] = {
                            "completed_flows": samples, "elapsed_seconds": duration,
                            "latency_ms": {
                                "median": median(latencies) * 1000,
                                "p95_nearest_rank": sorted(latencies)[ceil(0.95 * samples) - 1] * 1000,
                            },
                            "flows_per_second": samples / duration,
                            "recording_bytes_per_second_each_direction": samples * len(data) / duration,
                            "server_peak_rss_bytes": rss_peak_bytes(server.pid),
                            "client_and_signer_process_peak_rss_bytes": rss_peak_bytes("self"),
                        }
                finally:
                    test.doCleanups()
        finally:
            case.doClassCleanups()
    return {
        "wave": "retention-local-open", "classification": "MEASURED",
        "workload": "synthetic REA, serial, fresh writer and inspector sessions per flow",
        "flow": "admit and exact retry, start, claim, profile and recording upload, finish and exact retry, "
                "inspect, receipt lookup, exact artifact and paged object readback",
        "recording_bytes_per_flow": len(data),
        "scope": {"cgroup": scope_name, **limits, "aggregate_memory_peak_bytes": int(
            (cgroup / "memory.peak").read_text()
        )},
        "transports": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    values = measure(args.samples)
    args.output.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
