"""Record the C08 offline contract baseline inside the required systemd scope."""

import argparse
from base64 import b64decode
from collections import defaultdict
from hashlib import sha256
from importlib.metadata import version
import json
from math import ceil
from pathlib import Path
import platform
from statistics import median
import subprocess
from time import perf_counter_ns

from .test_contracts import VECTOR_PATH, check_vector, load_vectors
from .validation import ContractValidator, FREEZE_LIST_SHA256, FROZEN_PINS, strict_json

SCHEMA_REPEATS = 100
VECTOR_REPEATS = 30
SCOPE_PROPERTIES = {
    "MemoryHigh": str(6 * 1024**3),
    "MemoryMax": str(8 * 1024**3),
    "MemorySwapMax": "0",
}


def scope_witness():
    cgroup = Path("/proc/self/cgroup").read_text().strip()
    unit = cgroup.rsplit("/", 1)[-1]
    if not unit.endswith(".scope"):
        raise RuntimeError("the measurement needs a systemd user scope")
    result = subprocess.run(
        ["systemctl", "--user", "show", unit,
         *(f"--property={name}" for name in SCOPE_PROPERTIES)],
        check=True, capture_output=True, text=True,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines())
    if properties != SCOPE_PROPERTIES:
        raise RuntimeError(f"unexpected scope properties: {properties}")
    return {"unit": unit, "cgroup": cgroup, **{key: int(value) for key, value in properties.items()}}


def latency_stats(samples):
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "min_ns": ordered[0],
        "median_ns": median(ordered),
        "p95_ns": ordered[ceil(0.95 * len(ordered)) - 1],
        "max_ns": ordered[-1],
    }


def schema_cases(vectors):
    direct = {"admission": "Admission", "config": "ServerConfig"}
    encoded = {"start": "Start", "terminal": "Terminal", "finish": "Finish"}
    for vector in vectors:
        if not vector["valid"]:
            continue
        kind, value = vector["kind"], vector["data"]
        if kind == "schema":
            yield vector["form"], value
        elif kind in direct:
            yield direct[kind], value
        elif kind in encoded:
            yield encoded[kind], strict_json(b64decode(value))


def schema_latency(validator, vectors):
    cases = list(schema_cases(vectors))
    durations = defaultdict(list)
    for _ in range(SCHEMA_REPEATS):
        for name, value in cases:
            before = perf_counter_ns()
            validator.validate(name, value)
            durations[name].append(perf_counter_ns() - before)
    all_durations = [item for form in durations.values() for item in form]
    return {
        "aggregate": latency_stats(all_durations),
        "by_form": {name: latency_stats(form) for name, form in sorted(durations.items())},
        "positive_vectors": len(cases),
        "repeats": SCHEMA_REPEATS,
    }


def vector_throughput(validator, vectors):
    before = perf_counter_ns()
    for _ in range(VECTOR_REPEATS):
        for vector in vectors:
            check_vector(vector, validator)
    elapsed = perf_counter_ns() - before
    count = len(vectors) * VECTOR_REPEATS
    return {
        "repeats": VECTOR_REPEATS, "vectors_checked": count, "elapsed_ns": elapsed,
        "vectors_per_second": count * 1_000_000_000 / elapsed,
    }


def run(args):
    scope = scope_witness()
    validator = ContractValidator(args.frozen_dir)
    vectors = load_vectors()
    for vector in vectors:
        check_vector(vector, validator)

    source = Path(__file__).parent
    build_inputs = {
        path.name: sha256(path.read_bytes()).hexdigest()
        for path in sorted(source.iterdir())
        if path.suffix in {".py", ".json", ".txt"}
    }
    build_digest = sha256(json.dumps(build_inputs, sort_keys=True).encode()).hexdigest()
    return {
        "wave": "retention-contract",
        "medicion_cell": "C08",
        "classification": "MEASURED",
        "clock": "perf_counter_ns",
        "scope": scope,
        "python": platform.python_version(),
        "machine": platform.machine(),
        "dependencies": {package: version(package) for package in ("jsonschema", "referencing")},
        "tools_base_commit": args.tools_base,
        "rea_read_only_commit": args.rea_head,
        "accepted_design_sha256": sha256(args.accepted_design.read_bytes()).hexdigest(),
        "design_review_sha256": sha256(args.design_review.read_bytes()).hexdigest(),
        "frozen_sha256": {
            "FROZEN_SHA256SUMS": FREEZE_LIST_SHA256,
            **{filename: digest for _, filename, digest in FROZEN_PINS},
        },
        "build_inputs_sha256": build_inputs,
        "build_sha256": build_digest,
        "fixture_sha256": build_inputs[VECTOR_PATH.name],
        "vectors": len(vectors),
        "positive_vectors": sum(vector["valid"] for vector in vectors),
        "negative_vectors": sum(not vector["valid"] for vector in vectors),
        "schema_validation_latency": schema_latency(validator, vectors),
        "vector_throughput": vector_throughput(validator, vectors),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-dir", type=Path, required=True)
    parser.add_argument("--tools-base", required=True)
    parser.add_argument("--rea-head", required=True)
    parser.add_argument("--accepted-design", type=Path, required=True)
    parser.add_argument("--design-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"C08 benchmark recorded {result['vectors']} vectors at "
          f"{result['vector_throughput']['vectors_per_second']:.2f} vectors/s [MEASURED]")


if __name__ == "__main__":
    main()
