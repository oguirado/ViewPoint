"""Collect suite timings and run deterministic LPT shards. Linux only."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
CHECKOUT = "/tmp/work/source"
BAD_RESULTS = {"fail", "error", "unexpected_success"}
TIMED_GUARDS = {"qa/replay", "qa/mutation"}
VALIDATION_CPUSET = "12-15"
RESERVED_CPUS = set(range(16, 20))


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          stdout=subprocess.PIPE).stdout


def inventory():
    return tuple((HERE / "suites.txt").read_text().splitlines())


def revision(root, ref):
    return git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def snapshot(root, sha, scratch):
    source = scratch / "source"
    source.mkdir()
    archive = scratch / "source.tar"
    git(root, "archive", "-o", str(archive), sha)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(source)], check=True)
    archive.unlink()
    git(source, "init", "--quiet", "--template=")
    (source / ".git/HEAD").write_text(sha + "\n")
    (source / ".git/objects/info/alternates").write_text("/tmp/work/git-objects\n")
    return source


def verify_scope():
    scopes = Path("/proc/self/cgroup").read_text().splitlines()
    path, = [line.removeprefix("0::/") for line in scopes if line.startswith("0::/")]
    if not path.endswith(".scope"):
        raise ValueError("suite worker requires a capped systemd scope")
    for name, limit in (("memory.high", 6 << 30), ("memory.max", 8 << 30)):
        value = (Path("/sys/fs/cgroup") / path / name).read_text().strip()
        if not value.isdecimal() or not 0 < int(value) <= limit:
            raise ValueError(f"suite worker exceeds {name}: {value}")


def cpu_ranges(value):
    ranges = []
    for part in value.split(","):
        match = re.fullmatch(r"([0-9]+)(?:-([0-9]+)(?::([0-9]+))?)?", part)
        if not match:
            raise argparse.ArgumentTypeError(f"invalid CPU list: {value}")
        first, last, step = match.groups()
        start, stop, stride = int(first), int(last or first), int(step or 1)
        if stop < start or stride == 0:
            raise argparse.ArgumentTypeError(f"invalid CPU list: {value}")
        ranges.append(range(start, stop + 1, stride))
    return ranges


def verify_affinity(expected, nice):
    effective = sorted(os.sched_getaffinity(0))
    if effective != expected:
        raise ValueError(f"effective CPU affinity {effective} does not match {expected}")
    if nice is not None and os.nice(0) != nice:
        raise ValueError(f"validation requires nice {nice}")


def schedule_affinity(requested):
    inherited = os.sched_getaffinity(0)
    chosen = set()
    for cpus in requested:
        for cpu in cpus:
            if cpu not in inherited:
                raise ValueError(f"requested CPU {cpu} is outside inherited affinity {sorted(inherited)}")
            chosen.add(cpu)
    if chosen & RESERVED_CPUS and chosen != inherited:
        raise ValueError("reserved CPUs require exact CPU affinity from the caller")
    if chosen != inherited:
        os.sched_setaffinity(0, chosen)
    priority = None if chosen & RESERVED_CPUS else 19
    if priority is not None:
        current_priority = os.nice(0)
        if current_priority != priority:
            os.nice(priority - current_priority)
    affinity = sorted(chosen)
    verify_affinity(affinity, priority)
    return affinity, priority


def suite_command(source, objects, case, suite, traced, dependencies):
    runtime = "/run/user/" + str(os.getuid())
    manager = runtime + "/systemd/private"
    # The mutation suite launches its own capped scopes. Bind just the scope
    # manager endpoint, never the desktop session bus or runtime directory.
    supervisor = (
        ["--ro-bind", manager, "/tmp/runtime/systemd/private"]
        if suite == "qa/mutation" else ["--unshare-pid"]
    )
    extra = []
    path = "/usr/bin:/bin"
    if suite == "guest/custody":
        for key, target in (("cargo_registry", "/tmp/cargo/registry"), ("rust", "/tmp/rust"),
                            ("go", "/tmp/go-toolchain"), ("go_modules", "/tmp/go-modules")):
            extra += ["--ro-bind", dependencies[key], target]
        extra += ["--setenv", "CARGO_HOME", "/tmp/cargo",
                  "--setenv", "GOMODCACHE", "/tmp/go-modules", "--setenv", "GOPATH", "/tmp/go"]
        path = "/tmp/rust/bin:/tmp/go-toolchain/bin:" + path
    if suite == "recorder/store":
        tools = Path(dependencies["memoryweb_tools"])
        schema = "standards/schemas/mw.v1.schema.json"
        extra += ["--ro-bind", str(tools), "/tmp/memoryweb/tools",
                  "--ro-bind", str(tools.parent / schema), "/tmp/memoryweb/" + schema,
                  "--setenv", "MEMORYWEB_TOOLS", "/tmp/memoryweb/tools"]
    return [
        "bwrap", "--die-with-parent", "--new-session", "--unshare-user",
        "--unshare-net", "--unshare-ipc", "--unshare-uts",
        "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm",
        "--tmpfs", "/home", "--tmpfs", "/root", "--tmpfs", "/run", "--tmpfs", "/tmp",
        "--dir", "/tmp/runtime", "--dir", "/tmp/impact-home", *supervisor,
        "--bind", str(source), CHECKOUT, "--ro-bind", str(objects), "/tmp/work/git-objects",
        "--bind", str(case), "/tmp/work/case",
        "--ro-bind", str(HERE / "measure.py"), "/tmp/work/measure.py",
        "--clearenv", "--setenv", "PATH", path,
        "--setenv", "LANG", "C.UTF-8", "--setenv", "HOME", "/tmp/impact-home",
        "--setenv", "XDG_RUNTIME_DIR", "/tmp/runtime",
        "--setenv", "OPENBLAS_NUM_THREADS", "1", "--setenv", "OMP_NUM_THREADS", "1",
        "--setenv", "P3_TEST_RAM", "/dev/shm", "--setenv", "P4_TEST_RAM", "/dev/shm",
        "--setenv", "P7_TEST_RAM", "/dev/shm", "--setenv", "REP_SOURCE", CHECKOUT,
        "--setenv", "REP_REVISION", (source / ".git/HEAD").read_text().strip(),
        *extra, "--chdir", CHECKOUT, "--",
        sys.executable, "-IB", "/tmp/work/measure.py", CHECKOUT, suite,
        "/tmp/work/case/cases.json", "trace" if traced else "measure",
    ]


def run_suite(source, objects, scratch, suite, timeout, traced, dependencies):
    with tempfile.TemporaryDirectory(prefix="case-", dir=scratch) as name:
        case = Path(name)
        started = None if traced else time.perf_counter()
        try:
            child = subprocess.run(suite_command(source, objects, case, suite, traced, dependencies),
                                   capture_output=True, timeout=timeout)
            rc = child.returncode
            log = (child.stdout + child.stderr).decode(errors="replace")
        except subprocess.TimeoutExpired:
            rc, log = 124, f"suite exceeded {timeout}s"
        elapsed = None if traced else time.perf_counter() - started
        evidence = case / "cases.json"
        payload = json.loads(evidence.read_text()) if evidence.exists() else {}
        tests = payload.get("cases", [])
        counts = Counter(row["result"] for row in tests)
        discovered, seen = payload.get("discovered"), payload.get("ran")
        measured = sum(row["duration_s"] is not None for row in tests)
        if rc == 0 and (not discovered or discovered != seen or len(tests) != seen
                    or (not traced and measured != seen)
                        or BAD_RESULTS.intersection(counts)):
            rc = 2
            log += "\nIncomplete per-test receipt."
        info = {"suite": suite, "rc": rc, "duration_s": elapsed,
                "discovered": discovered, "ran": seen, "counts": dict(counts)}
        if traced:
            info["inputs"] = payload.get("inputs")
        if rc:
            info["diagnostic"] = log[-8000:]
        return info, [{"suite": suite, **row} for row in tests]


def pack_lpt(weights, workers):
    if workers < 1:
        raise ValueError("worker count must be positive")
    if not weights or any(not math.isfinite(weight) or weight <= 0 for weight in weights.values()):
        raise ValueError("each suite needs a finite positive wall duration")
    bins = [[] for _ in range(min(workers, len(weights)))]
    totals = [0.0] * len(bins)
    for suite in sorted(weights, key=lambda name: (-weights[name], name)):
        target = min(range(len(bins)), key=lambda index: (totals[index], index))
        bins[target].append(suite)
        totals[target] += weights[suite]
    verify_partition(weights.keys(), bins)
    return bins


def verify_partition(suites, bins):
    if Counter(suite for shard in bins for suite in shard) != Counter(suites):
        raise ValueError("shard membership must match the input suites exactly once")


def read_receipt(directory, expected=None):
    summary = json.loads((directory / "summary.json").read_text())
    if expected is None:
        expected = tuple(row["suite"] for row in summary["suites"])
        if len(set(expected)) != len(expected) or not set(expected) <= set(inventory()):
            raise ValueError("receipt has unknown or duplicate suites")
    data = (directory / "tests.jsonl").read_bytes()
    if hashlib.sha256(data).hexdigest() != summary["ledger_sha256"]:
        raise ValueError("test ledger hash differs from its summary")
    verify_partition(expected, summary["shards"])
    verify_partition(expected, [[row["suite"] for row in summary["suites"]]])
    cases = {}
    counts = Counter()
    by_suite = {suite: Counter() for suite in expected}
    for line in data.splitlines():
        row = json.loads(line)
        key = row["suite"], row["test"]
        if key in cases or key[0] not in by_suite:
            raise ValueError("unexpected or duplicate test in ledger")
        cases[key] = row["result"]
        counts.update([row["result"]])
        by_suite[key[0]].update([row["result"]])
    if counts != summary["counts"]:
        raise ValueError("summary outcome counts differ from the ledger")
    for row in summary["suites"]:
        if (by_suite[row["suite"]] != row["counts"]
                or (row["rc"] == 0 and row["ran"] != sum(row["counts"].values()))):
            raise ValueError(f"incomplete ledger for {row['suite']}")
    if bool(summary["rc"]) != any(row["rc"] for row in summary["suites"]):
        raise ValueError("run and suite exit status disagree")
    return summary, cases


def load_weights(path, suites, sha):
    data, _ = read_receipt(path)
    if data.get("mode") != "collect" or data["revision"] != sha or data["rc"]:
        raise ValueError("timing receipt must be complete and from the pinned weights revision")
    weights = {row["suite"]: row["duration_s"] for row in data["suites"]}
    if not set(suites) <= weights.keys():
        raise ValueError("selected suite is missing from the timing receipt")
    return {suite: weights[suite] for suite in suites}


def compare_runs(first, second, suites):
    serial, serial_cases = read_receipt(first, suites)
    shard, shard_cases = read_receipt(second, suites)
    if (serial["mode"] != "collect" or shard["mode"] != "shard"
            or serial["revision"] != shard["revision"] or serial["workers"] != 1
            or serial["cpu_affinity"] != shard["cpu_affinity"]):
        raise ValueError("comparison requires identical revisions and affinity, with serial first")
    missing = sorted(serial_cases.keys() - shard_cases.keys())
    extra = sorted(shard_cases.keys() - serial_cases.keys())
    changed = sorted(key for key in serial_cases.keys() & shard_cases.keys()
                     if serial_cases[key] != shard_cases[key])
    return {"revision": serial["revision"], "tests": len(serial_cases),
            "workers": shard["workers"], "serial_s": serial["wall_s"], "shard_s": shard["wall_s"],
            "missing": missing, "extra": extra, "changed_results": changed,
            "rc": int(bool(missing or extra or changed or serial["rc"] or shard["rc"]))}


def run_worker(request_path):
    verify_scope()
    request = json.loads(request_path.read_text())
    verify_affinity(request["cpu_affinity"], request["nice"])
    scratch = request_path.parent
    results = []
    cases = []
    for suite in request["suites"]:
        info, tests = run_suite(Path(request["source"]), Path(request["objects"]), scratch,
                                suite, request["timeout"], request["trace"], request["dependencies"])
        results.append(info)
        cases.extend(tests)
    request_path.with_suffix(".result").write_text(json.dumps({"suites": results, "cases": cases}))
    return int(any(info["rc"] for info in results))


def invoke_shard(request_path):
    process = subprocess.run([sys.executable, "-IB", __file__, "_worker", str(request_path)],
                             capture_output=True)
    result_path = request_path.with_suffix(".result")
    if not result_path.exists() or process.returncode not in (0, 1):
        raise ValueError(f"worker did not emit a complete receipt (rc={process.returncode}): "
                         + process.stderr.decode(errors="replace")[-2000:])
    return json.loads(result_path.read_text())


def run(args):
    output = args.output.resolve()
    if not output.is_relative_to(HERE) or output == HERE:
        raise ValueError("output must be a fresh directory inside this surface")
    sha = revision(ROOT, args.rev)
    source_paths = set(os.fsdecode(git(ROOT, "ls-tree", "-rz", "--name-only", sha)).split("\0"))
    suites = inventory()
    chosen = tuple(args.suite) if args.suite else suites
    if len(set(chosen)) != len(chosen) or any(suite not in suites for suite in chosen):
        raise ValueError("unknown or duplicate suite selection")
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.workers <= 0:
        raise ValueError("timeout and worker count must be positive")
    weights_sha = None
    if args.command == "collect":
        bins = [list(chosen)]
    elif args.command == "audit":
        workers = min(args.workers, len(chosen))
        bins = [list(chosen[index::workers]) for index in range(workers)]
    elif args.weights:
        weights_sha = revision(ROOT, args.weights_rev) if args.weights_rev else sha
        bins = pack_lpt(load_weights(args.weights, chosen, weights_sha), args.workers)
    else:
        raise ValueError("sharding requires a complete timing receipt")
    verify_partition(chosen, bins)
    prerequisites = {}
    needed = []
    if "guest/custody" in chosen:
        needed.extend(("rust", "cargo_registry", "go", "go_modules"))
    if "recorder/store" in chosen:
        needed.append("memoryweb_tools")
    for key in needed:
        directory = getattr(args, key)
        if directory is None or not directory.is_dir():
            option = key.replace("_", "-")
            raise ValueError(f"required offline input --{option} is unavailable")
        prerequisites[key] = str(directory.resolve())
    traced = args.command == "audit"
    if traced:
        if args.guard_cpuset and not set(chosen) <= TIMED_GUARDS:
            raise ValueError("guard cpuset is reserved for suites that assert timed CPU affinity")
        affinity = sorted(RESERVED_CPUS) if args.guard_cpuset else list(range(12, 16))
        priority = None if args.guard_cpuset else 19
        verify_affinity(affinity, priority)
    else:
        affinity, priority = schedule_affinity(args.cpuset)
    verify_scope()
    output.mkdir()
    objects = os.fsdecode(git(ROOT, "rev-parse", "--path-format=absolute",
                              "--git-path", "objects")).strip()
    with tempfile.TemporaryDirectory(prefix=".work-", dir=output) as name:
        scratch = Path(name)
        source = snapshot(ROOT, sha, scratch)
        requests = []
        for index, shard in enumerate(bins):
            path = scratch / f"shard-{index}.json"
            path.write_text(json.dumps({"source": str(source), "objects": objects, "suites": shard,
                                        "timeout": args.timeout, "trace": traced,
                                        "cpu_affinity": affinity, "nice": priority,
                                        "dependencies": prerequisites}))
            requests.append(path)
        started = None if traced else time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(bins)) as pool:
            results = list(pool.map(invoke_shard, requests))
        wall = None if traced else time.perf_counter() - started

    suite_rows = [row for shard in results for row in shard["suites"]]
    verify_partition(chosen, [[row["suite"] for row in suite_rows]])
    case_rows = [row for shard in results for row in shard["cases"]]
    if len({(row["suite"], row["test"]) for row in case_rows}) != len(case_rows):
        raise ValueError("duplicate test IDs")
    for row in suite_rows:
        if row.get("inputs") is not None:
            row["inputs"] = [name for name in row["inputs"] if name in source_paths]
    counts = Counter(row["result"] for row in case_rows)
    rc = int(any(row["rc"] for row in suite_rows))
    ledger = output / "tests.jsonl"
    with ledger.open("x", encoding="utf-8") as stream:
        for row in sorted(case_rows, key=lambda row: (row["suite"], row["test"])):
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    summary = {"revision": sha, "cpu_affinity": affinity, "nice": os.nice(0),
               "mode": args.command,
               "workers": len(bins), "wall_s": wall, "shards": bins,
               "counts": dict(counts), "rc": rc, "suites": suite_rows,
               "ledger_sha256": hashlib.sha256(ledger.read_bytes()).hexdigest()}
    if weights_sha is not None:
        summary["weights_revision"] = weights_sha
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: summary[key] for key in ("revision", "workers", "wall_s", "counts", "rc")}))
    for row in suite_rows:
        if row["rc"]:
            print(f"{row['suite']} rc={row['rc']}\n{row['diagnostic']}", file=sys.stderr)
    return rc


def main(argv):
    if argv[:1] == ["_worker"]:
        return run_worker(Path(argv[1]))
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="command", required=True)
    compare = modes.add_parser("compare")
    compare.add_argument("serial", type=Path)
    compare.add_argument("shard", type=Path)
    for mode in ("collect", "shard", "audit"):
        command = modes.add_parser(mode)
        command.add_argument("--rev", default="HEAD")
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--suite", action="append")
        command.add_argument("--timeout", type=float, default=180)
        command.add_argument("--workers", type=int, default=1)
        for name in ("rust", "cargo-registry", "go", "go-modules", "memoryweb-tools"):
            command.add_argument("--" + name, type=Path)
        if mode == "audit":
            command.add_argument("--guard-cpuset", action="store_true")
        else:
            command.add_argument("--cpuset", type=cpu_ranges, default=VALIDATION_CPUSET,
                                 help="Linux CPU list within caller affinity (default: %(default)s)")
        if mode == "shard":
            command.add_argument("--weights", type=Path, required=True)
            command.add_argument("--weights-rev", help="pin a frozen weight receipt from another Git revision")
    args = parser.parse_args(argv)
    if args.command == "compare":
        report = compare_runs(args.serial, args.shard, inventory())
        print(json.dumps(report, indent=2))
        return report["rc"]
    if args.command == "collect" and args.workers != 1:
        parser.error("collect always runs serially")
    return run(args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"impact runner: {exc}")
