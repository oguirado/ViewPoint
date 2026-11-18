"""Opt-in whole-gate and unittest scheduling. Run the suite with python -m tools.testing.shard.

Gate units are existing legs. Suite units are modules, preserving unittest fixtures.
Config and result files live outside the source inventory. A partial selection
has its own scope and never supplies the missing legs of a whole-gate receipt.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest

from asset_factory import core, proc
from tools.testing import impact


def _read(path):
    return core.load_json(path, max_bytes=32 * 1024**2)


def configuration(path):
    try:
        cfg = _read(path)
    except core.ContractError as exc:
        raise ValueError("schedule_config") from exc
    allowed = {"schema", "workers", "order", "duration_ns", "impact_graph",
               "impact_sha256", "toolchain_sha256", "toolchain_files", "module_history"}
    if (not isinstance(cfg, dict) or set(cfg) - allowed
            or cfg.get("schema") != "af.test-schedule/v1"):
        raise ValueError("schedule_config")
    if type(cfg.get("workers")) is not int or not 1 <= cfg["workers"] <= 16:
        raise ValueError("worker_count")
    if cfg.get("order", "longest") not in ("discovery", "longest"):
        raise ValueError("schedule_order")
    values = cfg.get("duration_ns", {})
    if not isinstance(values, dict) or any(type(v) is not int or v < 0 for v in values.values()):
        raise ValueError("duration_ns")
    modules = cfg.get("module_history", {})
    if (not isinstance(modules, dict) or any(not isinstance(row, dict)
            or type(row.get("wall_ns")) is not int or row["wall_ns"] < 0
            or not isinstance(row.get("members_sha256"), str) for row in modules.values())):
        raise ValueError("module_history")
    if "impact_graph" in cfg:
        graph = cfg["impact_graph"]
        if not isinstance(graph, str) or not graph:
            raise ValueError("impact_graph")
        cfg["impact_graph"] = Path(path).resolve().parent / graph
    return cfg


def partition(names, durations, workers, order="longest"):
    """LPT over measured weights. Use a median estimate only for placement."""
    names = list(names)
    if not names or len(set(names)) != len(names):
        raise ValueError("invalid_roster")
    if type(workers) is not int or not 1 <= workers <= 16:
        raise ValueError("worker_count")
    count = min(workers, len(names))
    if order == "discovery":
        return [names[i::count] for i in range(count)]
    if order != "longest":
        raise ValueError("schedule_order")
    known = sorted(durations[name] for name in names if name in durations)
    if any(type(v) is not int or v < 0 for v in known):
        raise ValueError("duration_ns")
    estimate = known[len(known) // 2] if known else 1
    weights = {name: durations.get(name, estimate) for name in names}
    bins = [[] for _ in range(count)]
    loads = [0] * count
    for name in sorted(names, key=lambda n: (-weights[n], n)):
        index = min(range(count), key=lambda i: (loads[i], len(bins[i]), i))
        bins[index].append(name)
        loads[index] += weights[name]
    return bins


def merge(plan, results, cancelled):
    if cancelled:
        raise InterruptedError("schedule_cancelled")
    if not plan or set(results) != set(range(len(plan))):
        raise ValueError("shard_missing")
    combined = {}
    for i, expected in enumerate(plan):
        rows = results[i]
        if (not expected or len(expected) != len(set(expected)) or set(rows) != set(expected)
                or combined.keys() & rows.keys()):
            raise ValueError("shard_membership")
        combined.update(rows)
    return combined


@contextmanager
def cancellation():
    stopped = threading.Event()
    def stop(signum, frame):
        stopped.set()
    previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield stopped
        if stopped.is_set():
            raise InterruptedError("schedule_cancelled")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _execute(plan, fn, stopped):
    base = sorted(os.sched_getaffinity(0))
    if not base or not set(base) <= set(range(16)) or len(plan) > len(base):
        raise ValueError("worker_affinity")
    def worker(index):
        # Children inherit the lane thread's CPU mask through the process helper.
        allowed = base[index::len(plan)]
        os.sched_setaffinity(0, allowed)
        try:
            if stopped.is_set():
                raise InterruptedError("schedule_cancelled")
            return fn(index, plan[index])
        except BaseException:
            stopped.set()
            raise
        finally:
            os.sched_setaffinity(0, base)
    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        futures = [pool.submit(copy_context().run, worker, i) for i in range(len(plan))]
        return {i: f.result() for i, f in enumerate(futures)}


def gate_legs(config, before, root, gate):
    selected, _ = impact.selection(gate.REQUIRED, impact.inputs_from_snapshot(before, root), config)
    plan = partition(selected, config.get("duration_ns", {}), config["workers"],
                     config.get("order", "longest"))
    try:
        with cancellation() as stop:
            def run(index, names):
                token = gate._LEG_CANCELLED.set(stop.is_set)
                try:
                    return {name: gate.run_leg(name, gate.LEGS[name], 300) for name in names}
                finally:
                    gate._LEG_CANCELLED.reset(token)
            rows = merge(plan, _execute(plan, run, stop), stop.is_set())
    except InterruptedError as exc:
        raise gate._GateInterrupted() from exc
    return [rows[name] for name in selected]


def _cases(node):
    if isinstance(node, unittest.TestCase):
        yield node
    elif type(node) is unittest.TestSuite:
        for child in node:
            yield from _cases(child)
    else:
        raise ValueError("unsupported_suite_fixture")


def discover(root, pattern):
    loader = unittest.TestLoader()
    suite = loader.discover(str(root / "tests"), pattern=pattern)
    if loader.errors:
        raise ValueError("discovery_errors: " + "\n".join(loader.errors))
    cases = list(_cases(suite))
    by_id = {case.id(): case for case in cases}
    if not cases or len(by_id) != len(cases):
        raise ValueError("empty_or_duplicate_discovery")
    return by_id


class _TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.times = {}
        self.successes = set()

    def addSuccess(self, test):
        self.successes.add(test.id())
        super().addSuccess(test)

    def startTest(self, test):
        super().startTest(test)
        self.start_ns = time.monotonic_ns()
        self.cpu_ns = time.process_time_ns()

    def stopTest(self, test):
        self.times[test.id()] = {"duration_ns": time.monotonic_ns() - self.start_ns,
                                 "process_cpu_ns": time.process_time_ns() - self.cpu_ns,
                                 "retries": 0, "outcome": "ok" if test.id() in self.successes else "unreported"}
        super().stopTest(test)


def run_worker(request_path):
    request = _read(request_path)
    cases = discover(Path(request["root"]), request["pattern"])
    ids = request["ids"]
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or not set(ids) <= cases.keys():
        raise ValueError("worker_membership")
    suite = unittest.TestSuite(cases[name] for name in ids)
    result = unittest.TextTestRunner(resultclass=_TimedResult, verbosity=2).run(suite)
    events = []
    setup_outcomes = {}
    for label, group in (("skip", result.skipped), ("xfail", result.expectedFailures),
                         ("failure", result.failures), ("error", result.errors),
                         ("unexpected_success", [(t, "") for t in result.unexpectedSuccesses])):
        for test, detail in group:
            name = test.id()
            # Subtests report against their parent method.
            parent = getattr(test, "test_case", test).id()
            if parent in result.times:
                result.times[parent]["outcome"] = label
            elif isinstance(test, unittest.suite._ErrorHolder):
                setup_outcomes[name] = label
            events.append([label, name, detail])
    # unittest reports blocked class/module setup once, without method callbacks.
    # Keep its original event and account for each blocked method with no test work.
    for name in ids:
        if name not in result.times:
            cls = type(cases[name])
            outcome = (setup_outcomes.get(f"setUpModule ({cls.__module__})")
                       or setup_outcomes.get(f"setUpClass ({unittest.util.strclass(cls)})"))
            if outcome is not None:
                result.times[name] = {"duration_ns": 0, "process_cpu_ns": 0,
                                      "retries": 0, "outcome": outcome}
    rc = 0 if result.wasSuccessful() else 5
    if rc == 0:
        if result.shouldStop:
            rc = 6
        elif set(result.times) != set(ids) or any(r["outcome"] == "unreported" for r in result.times.values()):
            rc = 3
    receipt = {"rows": result.times, "events": events, "rc": rc,
               "cpus": sorted(os.sched_getaffinity(0)), "nice": os.nice(0)}
    core.atomic_write_json(request_path.with_suffix(".result.json"), receipt)
    return rc


def _owned_worker(request, stop):
    with request.with_suffix(".log").open("xb") as log:
        argv = [sys.executable, "-B", "-m", "tools.testing.shard", "--worker", str(request)]
        job = proc.popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 3600
            while True:
                if stop.is_set():
                    raise InterruptedError("schedule_cancelled")
                if time.monotonic() >= deadline:
                    raise TimeoutError("shard_deadline")
                try:
                    rc = proc.wait_exit(job, timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            errors = proc.cleanup(job)
            if errors:
                raise ValueError("shard_cleanup:" + ",".join(errors))
    receipt = _read(request.with_suffix(".result.json"))
    if rc == 6 and receipt["rc"] == rc:
        raise InterruptedError("unittest_stopped")
    if (rc not in (0, 3, 5) or rc != receipt["rc"]
            or receipt["cpus"] != sorted(os.sched_getaffinity(0)) or receipt["nice"] != 15):
        raise ValueError("shard_exit_or_placement")
    if rc == 0:
        outcomes = {"ok", "skip", "xfail"}
        if (not all(r.get("outcome") in outcomes for r in receipt["rows"].values())
                or any(event[0] not in outcomes for event in receipt["events"])):
            raise ValueError("shard_outcome_mismatch")
    return receipt


def run_suite(config, root, pattern, output):
    from tools import gates
    proc.check_test_environment()
    if os.environ.get("AF_PROC_BACKEND") != "pidns":
        raise ValueError("suite_requires_pidns")
    root, output = root.resolve(strict=True), output.resolve()
    if output.is_relative_to(root):
        raise ValueError("evidence_inside_workload")
    output.mkdir(parents=True, exist_ok=False)
    snapshot = gates._snapshot(root, set())
    inputs = impact.inputs_from_snapshot(snapshot, root)
    cases = discover(root, pattern)
    selected, reason = impact.selection(cases, inputs, config)
    modules = {}
    for name in selected:
        modules.setdefault(type(cases[name]).__module__, []).append(name)
    estimates = config.get("duration_ns", {})
    # Estimate missing cases separately; never turn unknown history into measured zero.
    known = sorted(estimates[name] for name in selected if name in estimates)
    fallback = known[len(known) // 2] if known else 1
    costs = {mod: sum(estimates.get(name, fallback) for name in ids) for mod, ids in modules.items()}
    module_weights = []
    for module, ids in modules.items():
        row = config.get("module_history", {}).get(module)
        if row is not None and row["members_sha256"] == core.sha256_json(sorted(ids)):
            costs[module] = row["wall_ns"]
            module_weights.append(module)
    groups = partition(modules, costs, config["workers"], config.get("order", "longest"))
    plan = [[name for mod in mods for name in modules[mod]] for mods in groups]
    history = {"schema": "af.test-plan/v1", "roster": list(cases), "reason": reason,
               "inputs_sha256": core.sha256_json(inputs), "shards": plan,
               "measured_duration_count": len(known), "module_weights_used": sorted(module_weights)}
    core.atomic_write_json(output / "plan.json", history)
    with cancellation() as stop:
        def run(i, ids):
            path = output / f"worker-{i}.json"
            core.atomic_write_json(path, {"root": str(root), "pattern": pattern, "ids": ids})
            return _owned_worker(path, stop)
        receipts = _execute(plan, run, stop)
        if gates._snapshot(root, set()) != snapshot:
            raise ValueError("workload_drift")
        rows = merge(plan, {i: r["rows"] for i, r in receipts.items()}, stop.is_set())
        rc = core.worst_rc([r["rc"] for r in receipts.values()])
        result = {"schema": "af.test-run/v1", "scope": "exhaustive" if len(selected) == len(cases)
                  else "impact", "pattern": pattern, "rc": rc, "rows": rows,
                  "events": sorted((event for r in receipts.values() for event in r["events"]),
                                   key=lambda event: event[1]),
                  "plan_sha256": core.sha256_json(history)}
    core.atomic_write_json(output / "result.json", result)
    return rc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", type=Path)
    mode.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        if args.worker:
            proc.check_test_environment()
            return run_worker(args.worker)
        if args.out is None:
            raise ValueError("output_directory_required")
        return run_suite(configuration(args.config), args.root, args.pattern, args.out)
    except (InterruptedError, KeyboardInterrupt, TimeoutError):
        return 6
    except (ValueError, OSError, KeyError, TypeError, core.ContractError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
