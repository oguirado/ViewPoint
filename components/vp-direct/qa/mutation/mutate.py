#!/usr/bin/env python3
"""Run bounded source mutations against suite entry points on a pinned Git tree."""

import argparse
import ast
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
DIRECT = Path("components/vp-direct")
SURFACES = ("channel/core", "perception/lifecycle", "rewind/machine", "bus/broker", "control/core")
DEPS = (
    "components/vision-engine/vadapt",
    "components/wender-agent/wender/__init__.py",
    "components/wender-agent/wender/learn/__init__.py",
    "components/wender-agent/wender/learn/cgroup_memory.py",
    "components/wender-agent/wender/learn/procfs_memory.py",
    "components/wender-agent/wender/state/__init__.py",
    "components/wender-agent/wender/state/handles.py",
    "components/wender-agent/wender/state/schema.py",
    "components/wender-agent/wender/perceive/__init__.py",
    "components/wender-agent/wender/perceive/session_registry.py",
    "components/blender-visual-qa/blender_visual_qa",
    "components/blender-visual-qa/vqa3d",
)
SUITE_ARGS = ("-I", "-B", "-m", "unittest", "discover", "-s")
CAP = ("--user", "--scope", "-q", "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G")
PREFIX = "MUT_RESULT "
DEFAULT_CPUS = (16, 17, 18, 19)
COMPARE = {
    ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="),
    ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"),
    ast.Gt: (">", ">="), ast.GtE: (">=", ">"),
    ast.Is: ("is", "is not"), ast.IsNot: ("is not", "is"),
    ast.In: ("in", "not in"), ast.NotIn: ("not in", "in"),
}
REPLY_CODES = (
    ("NO_LEASE", "HOLD"), ("HOLD", "UNCERTAIN_OUTCOME"),
    ("STALE_FRAME", "NO_LEASE"), ("UNCERTAIN_OUTCOME", "HOLD"),
    ("policy_denied", "op_unbound"), ("op_unbound", "policy_denied"),
    ("OK", "HOLD"), ("EVIDENCE_REQUIRED", "HOLD"),
    ("EFFECT_UNSETTLED", "DUPLICATE"), ("STALE_OBSERVATION", "NO_LEASE"),
)
FAMILIES = ("compare", "boolean", "off_by_one", "remove_guard", "return_code")
DEFAULT_BUDGET = 12
DEFAULT_TIMEOUT = 30
LIFECYCLE_TIMEOUT = 90  # The committed suite has a real 60-second rest check.


@dataclass(frozen=True)
class Edit:
    file: str
    line: int
    col: int
    function: str
    family: str
    before: str
    after: str

    def apply(self, original):
        lines = original.splitlines(keepends=True)
        line = lines[self.line - 1]
        old = self.before.encode()
        end = self.col + len(old)
        if line[self.col:end] != old:
            raise ValueError(f"stale edit location: {self.file}:{self.line}:{self.col}")
        lines[self.line - 1] = line[:self.col] + self.after.encode() + line[end:]
        changed = b"".join(lines)
        if changed == original:
            raise ValueError("mutation does not change the source")
        compile(changed, self.file, "exec")
        return changed


def candidates(path, source):
    """Build small source edits from AST spans. Columns are UTF-8 byte offsets."""
    tree = ast.parse(source, filename=path)
    lines = source.splitlines()
    active_codes = {node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    def edit(line, start, end, function, family, after):
        before = lines[line - 1][start:end].decode()
        return Edit(path, line, start, function, family, before, after)

    def operator(left, right, function, family, old, new):
        if left.end_lineno != right.lineno:
            return
        line = lines[right.lineno - 1]
        gap = line[left.end_col_offset:right.col_offset]
        if gap.strip() == old.encode():
            start = left.end_col_offset + len(gap) - len(gap.lstrip())
            yield edit(right.lineno, start, start + len(old), function, family, new)

    def walk(node, function="", comparison=False, in_return=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if function:
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                old, new = COMPARE[type(node.ops[0])]
                yield from operator(node.left, node.comparators[0], function, "compare", old, new)
            if isinstance(node, ast.BoolOp) and len(node.values) == 2:
                old, new = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
                yield from operator(*node.values, function, "boolean", old, new)
            if (isinstance(node, ast.If) and not node.orelse and len(node.body) == 1
                    and isinstance(node.body[0], (ast.Return, ast.Raise))
                    and node.lineno == node.test.lineno == node.test.end_lineno):
                yield edit(node.lineno, node.test.col_offset, node.test.end_col_offset,
                           function, "remove_guard", "False")
            if isinstance(node, ast.Constant):
                if type(node.value) is int and comparison:
                    yield edit(node.lineno, node.col_offset, node.end_col_offset,
                               function, "off_by_one", str(node.value + 1))
                if in_return and isinstance(node.value, str):
                    for old, new in REPLY_CODES:
                        if node.value == old and new in active_codes:
                            yield edit(node.lineno, node.col_offset, node.end_col_offset,
                                       function, "return_code", repr(new))
        for child in ast.iter_child_nodes(node):
            # Only direct numeric operands of comparisons or counter increments.
            comparison_operand = isinstance(node, (ast.Compare, ast.AugAssign))
            yield from walk(child, function, comparison_operand,
                            in_return or isinstance(node, ast.Return))

    return list(dict.fromkeys(walk(tree)))


def select(catalogue, covered, budget):
    """Balance operators, and prefer a new file or function to a second edit there."""
    grouped = defaultdict(deque)
    locations = set()
    functions = Counter()
    files = Counter()
    picked = []
    for edit in sorted(catalogue, key=lambda row: (row.file, row.line, row.col, row.family)):
        if edit.line in covered.get(edit.file, ()):
            grouped[edit.family].append(edit)
    while len(picked) < budget:
        start = len(picked)
        for family in FAMILIES:
            eligible = [edit for edit in grouped[family] if (edit.file, edit.line) not in locations]
            if eligible:
                chosen = min(eligible, key=lambda row: (files[row.file], functions[row.file, row.function],
                                                        row.file, row.line, row.col))
                picked.append(chosen)
                locations.add((chosen.file, chosen.line))
                files[chosen.file] += 1
                functions[chosen.file, chosen.function] += 1
            if len(picked) == budget:
                break
        if len(picked) == start:
            break
    return picked


def git(*args):
    return subprocess.check_output(("git", *args), cwd=REPO)


def snapshot(revision, destination):
    # Only tracked product and import dependencies. Ignore other QA lanes and receipts.
    direct_paths = git("ls-tree", "-z", "--name-only", f"{revision}:{DIRECT}").split(b"\0")
    paths = [str(DIRECT / os.fsdecode(path)) for path in direct_paths if path and path != b"qa"]
    with tempfile.TemporaryFile(dir=destination.parent) as archive:
        subprocess.run(("git", "archive", "--format=tar", revision, "--", *paths, *DEPS),
                       cwd=REPO, stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:") as tar:
            tar.extractall(destination, filter="data")


def write_wrapper(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o700)


def run_suite(root, surface, work, watch, edit, timeout, cpus=DEFAULT_CPUS):
    tests_dir = root / DIRECT / surface / "tests"
    safe_run = tests_dir / "safe_run.sh"
    cpu_list = ",".join(map(str, cpus))
    with tempfile.TemporaryDirectory(dir=work, prefix="launch-") as launch:
        launch = Path(launch)
        config = launch / "probe.json"
        config.write_text(json.dumps({
            "root": str(root), "watch": watch, "cpus": list(cpus),
            "mutant": asdict(edit) if edit else None,
            "sha256": sha256((root / edit.file).read_bytes()).hexdigest() if edit else None,
        }), encoding="utf-8")
        # Leave the committed sh wrapper intact. Bound its real scope, and instrument
        # only its unittest process. The probe forwards the original unittest argv.
        write_wrapper(launch, "systemd-run", f'exec /usr/bin/systemd-run -p RuntimeMaxSec={timeout}s "$@"\n')
        python = shlex.quote(sys.executable)
        probe = shlex.quote(str(work / "probe.py"))
        write_wrapper(launch, "python3", f'''
if [ "$#" -ge 4 ] && [ "$1 $2 $3 $4" = "-I -B -m unittest" ]; then
    shift 4
    exec {python} -I -B {probe} {shlex.quote(str(config))} "$@"
fi
exec {python} "$@"
''')
        (launch / "home").mkdir()
        (launch / "tmp").mkdir()
        env = {key: os.environ[key] for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
               if key in os.environ}
        env.update(PATH=str(launch) + ":" + os.defpath, HOME=str(launch / "home"),
                   TMPDIR=str(launch / "tmp"), LC_ALL="C.UTF-8")
        if safe_run.is_file():
            command = ["taskset", "-c", cpu_list, "sh", str(safe_run)]
            entry = f"taskset -c {cpu_list} sh {DIRECT / surface / 'tests/safe_run.sh'}"
        elif surface == "control/core":
            command = ["taskset", "-c", cpu_list, "systemd-run", *CAP, "--", "python3",
                       *SUITE_ARGS, str(tests_dir), "-p", "test_*.py", "-v"]
            entry = (f"taskset -c {cpu_list} systemd-run {' '.join(CAP)} "
                     f"-p RuntimeMaxSec={timeout}s -- python3 {' '.join(SUITE_ARGS)} "
                     f"{DIRECT / surface / 'tests'} -p 'test_*.py' -v")
        else:
            return {"rc": None, "entry": "AUSENTE", "error": "committed safe_run.sh missing"}

        start = time.monotonic()
        try:
            run = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True,
                                 timeout=timeout + 10, errors="replace")
        except subprocess.TimeoutExpired:
            return {"rc": None, "entry": entry, "timeout": True, "elapsed_s": time.monotonic() - start}
        capped_out = {"rc": run.returncode, "entry": entry, "elapsed_s": time.monotonic() - start}
        markers = [line[len(PREFIX):] for line in run.stdout.splitlines() if line.startswith(PREFIX)]
        if len(markers) != 1:
            return {**capped_out, "error": "missing or ambiguous suite probe",
                    "diagnostic": (run.stdout + run.stderr)[-4000:]}
        receipt = json.loads(markers[0])
        if receipt.get("error"):
            receipt["diagnostic"] = run.stderr[-4000:]
        return {**capped_out, **receipt}


def disposition(baseline, run):
    if run.get("timeout"):
        return "timeout"
    if (run.get("error") or run.get("tests") != baseline["tests"] or run.get("tests", 0) == 0
            or run.get("skipped") != baseline["skipped"] or run.get("fixed_outcomes") != baseline["fixed_outcomes"]):
        return "invalid"
    failed = run["failures"] + run["errors"]
    if run["rc"] != int(bool(failed)):
        return "invalid"
    if not run["hit_cases"]:
        return "unactivated"
    if failed:
        return "killed" if run.get("activated_failed_cases") else "inconclusive"
    return "survived"


def measure(revision, surfaces, budget, timeout, output, mutation_id=None, *, cpus=DEFAULT_CPUS):
    probe_source = (HERE / "probe_strict.py").read_bytes()
    report = {"revision": revision, "budget_per_surface": budget, "suite_timeout_s": timeout,
              "cpus": list(cpus), "requested_mutant_id": mutation_id,
              "probe_sha256": sha256(probe_source).hexdigest(),
              "score": "killed / (killed + survived)", "surfaces": {}}
    start = time.monotonic()
    with tempfile.TemporaryDirectory(dir=HERE, prefix=".mutation-") as temporary:
        work = Path(temporary)
        (work / "probe.py").write_bytes(probe_source)
        for surface in surfaces:
            suite_timeout = timeout
            if suite_timeout is None:
                suite_timeout = LIFECYCLE_TIMEOUT if surface == "perception/lifecycle" else DEFAULT_TIMEOUT
            baseline_root = work / "baseline"
            snapshot(revision, baseline_root)
            try:
                paths = sorted((baseline_root / DIRECT / surface).glob("*.py"))
                sources = {p.relative_to(baseline_root).as_posix(): p.read_bytes()
                           for p in paths if not p.name.startswith("LIVE") and p.is_file()}
                catalogue = [edit for file, source in sources.items() for edit in candidates(file, source)]
                watch = {file: sorted({edit.line for edit in catalogue if edit.file == file})
                         for file in sources}
                baseline = run_suite(baseline_root, surface, work, watch, None, suite_timeout, cpus)
                row = {"baseline": baseline, "eligible_sites": len(catalogue),
                       "selected_ids": None, "mutants": []}
                if timeout is None:
                    row["suite_timeout_s"] = suite_timeout
                report["surfaces"][surface] = row
                if baseline.get("rc") == 0 and baseline.get("tests", 0) > 0 and not baseline.get("error"):
                    edits = select(catalogue, baseline["coverage"], budget)
                    row["selected_ids"] = list(range(1, len(edits) + 1))
                    for number, edit in enumerate(edits, 1):
                        if mutation_id is not None and number != mutation_id:
                            continue
                        mutant_root = work / "mutant"
                        snapshot(revision, mutant_root)
                        try:
                            original = sources[edit.file]
                            (mutant_root / edit.file).write_bytes(edit.apply(original))
                            run = run_suite(mutant_root, surface, work, {edit.file: [edit.line]}, edit,
                                            suite_timeout, cpus)
                        except (ValueError, SyntaxError) as exc:
                            run = {"rc": None, "error": str(exc)}
                        finally:
                            shutil.rmtree(mutant_root)
                        status = disposition(baseline, run)
                        row["mutants"].append({"id": number, "edit": asdict(edit), "status": status,
                                               "suite": run})
                counts = Counter(mutant["status"] for mutant in row["mutants"])
                scored = counts["killed"] + counts["survived"]
                row.update(counts=dict(sorted(counts.items())),
                           mutation_score=counts["killed"] / scored if scored else None)
                print(f"{surface}: suite rc={baseline.get('rc')}, {dict(counts)}", flush=True)
            finally:
                shutil.rmtree(baseline_root)
        report["elapsed_s"] = time.monotonic() - start
        receipt = work / "receipt.json"
        receipt.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # Publish the completed report once, without replacing an existing receipt.
        os.link(receipt, output)
    return report


def cpu_range(value):
    ends = value.split("-")
    if len(ends) <= 2 and all(part.isascii() and part.isdecimal() for part in ends):
        first, last = int(ends[0]), int(ends[-1])
        if first <= last < (os.cpu_count() or 0):
            return tuple(range(first, last + 1))
    raise argparse.ArgumentTypeError("expected a CPU number or an inclusive range such as 12-15")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--only", choices=SURFACES, action="append", help="repeat to select multiple surfaces")
    parser.add_argument("--budget", default=DEFAULT_BUDGET, type=int)
    parser.add_argument("--timeout", type=int, help="per-suite seconds, defaults to 90 for lifecycle and 30 otherwise")
    parser.add_argument("--output", required=True)
    parser.add_argument("--cpus", type=cpu_range, default=DEFAULT_CPUS, help="CPU number or range (default: 16-19)")
    parser.add_argument("--mutant", type=int, help="rerun one selected mutant by ID with a single --only surface")
    args = parser.parse_args()
    if args.budget < 1 or (args.timeout is not None and args.timeout < 1):
        parser.error("budget and timeout must be positive")
    output = Path(args.output).resolve()
    if output.parent != HERE or output.suffix != ".json" or output.exists():
        parser.error("output must be a new JSON file directly inside the mutation surface")
    revision = git("rev-parse", "--verify", "--end-of-options", args.revision + "^{commit}").decode().strip()
    surfaces = tuple(dict.fromkeys(args.only or SURFACES))
    if args.mutant is not None and (len(surfaces) != 1 or not 1 <= args.mutant <= args.budget):
        parser.error("--mutant requires one surface and an ID within the budget")
    report = measure(revision, surfaces, args.budget, args.timeout, output, args.mutant, cpus=args.cpus)
    return int(any(row["mutation_score"] is None or set(row["counts"]) - {"killed", "survived", "unactivated"}
                   for row in report["surfaces"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
