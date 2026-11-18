"""Measure a short offline ABBA series using a frozen synthetic focus trace."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from time import sleep

T2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(T2))
from score import INTERVAL, components, digest, timing_distributions, validate_contract
from LIVE_t2 import Markers
from telemetry import HostTelemetry, now, signature

ORDER = ("baseline", "candidate", "candidate", "baseline")


def main():
    repo = T2.parents[3]
    fixture = T2.parents[1] / "schemas/tests/examples/vp.focus_trace_v1.valid.json"
    data = fixture.read_bytes()
    scope, cgroup = signature()
    telemetry = HostTelemetry(cgroup, [], [])
    iterations, samples = [], []
    start = now()
    for index, arm in enumerate(ORDER):
        due = start + index * INTERVAL
        sleep(max(0, due - now()) / 1_000_000_000)
        markers = Markers()
        markers.admitted()
        trace = json.loads(data)
        validate_contract("vp.focus_trace/v1", trace)
        result = digest(trace)
        markers.first_byte()
        iterations.append(dict(markers.finish(), arm=arm, order_index=index, result_sha256=result))
        samples.append(telemetry.read())
    sleep(max(0, start + len(ORDER) * INTERVAL - now()) / 1_000_000_000)
    end = now()
    report = {
        "kind": "offline fixture calibration", "numeric_evidence": "MEASURED",
        "commit": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
        "fixture": str(fixture.relative_to(repo)), "fixture_sha256": sha256(data).hexdigest(),
        "workload": "validate and hash the frozen synthetic focus trace",
        "workload_sha256": {str(path.relative_to(T2)): sha256(path.read_bytes()).hexdigest() for path in
                            (Path(__file__).resolve(), T2 / "score.py", T2 / "telemetry.py", T2 / "LIVE_t2.py")},
        "scope": scope, "warmups": 0, "iterations": len(iterations), "paired_order": list(ORDER),
        "start_mono_ns": start, "end_mono_ns": end, "interval_ns": INTERVAL,
        "raw_iterations": iterations, "samples": samples, "result_sha256": result,
        "failed": 0, "refused": 0, "timing": timing_distributions([components(row) for row in iterations]),
        "competing_cgroups": None, "throttle_counters": None, "kernel_uvm_watermarks": None,
        "raw_receipt": str((T2 / "calibration.json").relative_to(repo)),
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
