"""Select ViewPoint unittest surfaces from a Git comparison and a full input receipt."""

import argparse
import ast
from collections import Counter, defaultdict
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner import HERE, ROOT, git, inventory, read_receipt, revision


DIRECT = "components/vp-direct/"
IMPORT_ROOTS = ("", DIRECT, "agents/", "components/wender-agent/",
                "components/vision-engine/", "components/blender-visual-qa/",
                "daemon-adapter/", "memoryweb-builtin/", "tools/")
TOOLCHAIN = {
    "deps.lock", "Makefile", "Cargo.toml", "Cargo.lock", "pyproject.toml",
    "pytest.ini", "setup.cfg", "setup.py", "tox.ini", "package.json", "package-lock.json",
    "uv.lock", "Pipfile", "Pipfile.lock", ".python-version", "rust-toolchain.toml",
    "conftest.py", "safe_run.sh",
}


def git_paths(raw):
    return [os.fsdecode(path) for path in raw.split(b"\0") if path]


def tree(root, sha):
    files = {}
    for entry in git(root, "ls-tree", "-r", "-z", sha).split(b"\0"):
        if entry:
            info, name = entry.split(b"\t", 1)
            mode, kind, oid = info.decode().split(" ")
            files[os.fsdecode(name)] = (mode, kind, oid)
    return files


def changed_paths(root, before, after):
    return git_paths(git(root, "diff", "--name-only", "-z", "--no-renames", before, after, "--"))


def source_trees(root, paths, syntax):
    needed = {
        oid for name, (mode, kind, oid) in paths.items()
        if name.endswith(".py") and mode in {"100644", "100755"} and kind == "blob"
        and oid not in syntax
    }
    if needed:
        oids = sorted(needed)
        result = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"], check=True,
                                input=("\n".join(oids) + "\n").encode(), stdout=subprocess.PIPE)
        data = result.stdout
        cursor = 0
        for oid in oids:
            end = data.index(b"\n", cursor)
            got, kind, size = data[cursor:end].decode().split()
            if (got, kind) != (oid, "blob"):
                raise ValueError("unexpected Git blob response")
            start, stop = end + 1, end + 1 + int(size)
            try:
                node = ast.parse(data[start:stop])
                syntax[oid] = tuple(item for item in ast.walk(node)
                                    if isinstance(item, (ast.Import, ast.ImportFrom)))
            except SyntaxError:
                syntax[oid] = None
            cursor = stop + 1
        if cursor != len(data):
            raise ValueError("incomplete Git blob response")


def module_name(path):
    name = path.removesuffix(".py").replace("/", ".")
    return name.removesuffix(".__init__")


class Imports:
    def __init__(self, files, syntax, suites):
        self.files = files
        self.syntax = syntax
        self.modules = defaultdict(set)
        self.entries = defaultdict(list)
        for path in sorted(files):
            if not path.endswith(".py"):
                continue
            for root in IMPORT_ROOTS:
                if path.startswith(root):
                    self.modules[module_name(path[len(root):])].add(path)
            owner = owner_of(path, suites)
            if owner and is_test(path, owner):
                self.entries[owner].append(path)

    def resolve(self, name, directory):
        if not name:
            return set()
        parts = name.split(".")
        located = set()
        for end in range(1, len(parts) + 1):
            module = ".".join(parts[:end])
            located.update(self.modules.get(module, ()))
            candidate = directory.joinpath(*parts[:end])
            for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
                filename = path.as_posix()
                if filename in self.files:
                    located.add(filename)
        return located

    def dependencies(self, path):
        directory = PurePosixPath(path).parent
        imports = self.syntax.get(self.files[path][2])
        if imports is None:
            raise ValueError(f"unreadable Python dependency {path}")
        found = set()
        for item in imports:
            if isinstance(item, ast.Import):
                for name in item.names:
                    found.update(self.resolve(name.name, directory))
                continue
            origin = directory
            if item.level:
                for _ in range(item.level - 1):
                    origin = origin.parent
            base = item.module or ""
            found.update(self.resolve(base, origin))
            for name in item.names:
                if name.name != "*":
                    found.update(self.resolve(".".join(filter(None, (base, name.name))), origin))
        return found

    def closure(self, suite, observed):
        pending = self.entries[suite] + [name for name in observed
                                        if name in self.files and name.endswith(".py")]
        if not self.entries[suite]:
            raise ValueError(f"missing test entry for {suite}")
        visited = set()
        while pending:
            path = pending.pop()
            if path not in visited:
                visited.add(path)
                pending.extend(self.dependencies(path) - visited)
        return visited | set(observed)


def owner_of(path, suites):
    if path.startswith(DIRECT):
        return next((suite for suite in suites if path.startswith(DIRECT + suite + "/")), None)
    return None


def is_test(path, suite):
    relative = path.removeprefix(DIRECT + suite + "/tests/")
    return relative != path and PurePosixPath(relative).match("test_*.py")


def changelog_fragment(path):
    name = PurePosixPath(path)
    return name.parent.as_posix() == "changes/unreleased" and name.suffix == ".md"


def invalidates_all(path):
    parts = PurePosixPath(path).parts
    return (parts[0] in {"contracts", ".github", "schemas", "fixtures"}
            or path.startswith((DIRECT + "config/", DIRECT + "qa/impact/"))
            or any(part in {"schemas", "fixtures", "shared_fixtures", "test_fixtures"}
                   for part in parts[:-1])
            or path.endswith(".schema.json")
            or parts[-1] in TOOLCHAIN
            or parts[-1].startswith(("requirements", "constraints")))


def changed_path_is_canonical(path):
    name = PurePosixPath(path)
    return bool(name.parts) and not name.is_absolute() and ".." not in name.parts and name.as_posix() == path


def pick(suites, paths, before, after, graphs, traces):
    def full(reason):
        return {"mode": "full", "reason": reason, "suites": list(suites)}

    if not all(changed_path_is_canonical(path) for path in paths):
        return full("noncanonical changed path")
    for path in paths:
        if invalidates_all(path):
            return full(f"shared input or toolchain changed: {path}")
        if not changelog_fragment(path) and (path not in before or path not in after):
            owner = owner_of(path, suites)
            if not owner or not is_test(path, owner) or path not in after:
                return full(f"unmodelled addition or removal: {path}")
        if ((path in before and before[path][0] == "120000")
                or (path in after and after[path][0] == "120000")):
            return full(f"symlink changed: {path}")
    relevant = {path for path in paths if not changelog_fragment(path)}
    if not relevant:
        return {"mode": "selected", "suites": [], "matches": {}}
    if traces is None or set(traces) != set(suites):
        return full("complete baseline input receipt unavailable")
    try:
        matches = {}
        covered = set()
        fixture_consumers = defaultdict(int)
        for suite in suites:
            observed = traces[suite]
            inputs = set().union(*(graph.closure(suite, observed) for graph in graphs))
            hits = relevant & inputs
            owned = {path for path in relevant if owner_of(path, suites) == suite}
            if hits or owned:
                matches[suite] = sorted(hits | owned)
            covered.update(hits)
            covered.update(path for path in owned
                           if is_test(path, suite) or PurePosixPath(path).name == "README.md")
            for path in hits:
                if PurePosixPath(path).name.startswith("fixture"):
                    fixture_consumers[path] += 1
        for path, consumers in fixture_consumers.items():
            if consumers > 1:
                return full(f"shared fixture changed: {path}")
        if relevant - covered:
            return full("unmapped changed input: " + min(relevant - covered))
        return {"mode": "selected", "suites": list(matches), "matches": matches}
    except ValueError as exc:
        return full(str(exc))


def audit_receipt(directory, supplements, suites, sha):
    summary, cases = read_receipt(directory, suites)
    if summary["revision"] != sha or summary["mode"] != "audit":
        raise ValueError("input receipt must be a full audit at the requested revision")
    rows = {row["suite"]: row for row in summary["suites"]}
    for supplement in supplements:
        partial, partial_cases = read_receipt(supplement)
        if partial["revision"] != sha or partial["mode"] != "audit" or partial["rc"]:
            raise ValueError("audit supplement must be green at the same revision")
        for row in partial["suites"]:
            suite = row["suite"]
            if rows[suite]["rc"] == 0:
                raise ValueError(f"audit supplement may only replace a failed suite: {suite}")
            rows[suite] = row
            cases = {key: result for key, result in cases.items() if key[0] != suite}
        cases.update(partial_cases)
    failing = [suite for suite, row in rows.items() if row["rc"]]
    if failing:
        raise ValueError("full audit still has failing suites: " + ", ".join(failing))
    return {**summary, "suites": list(rows.values()), "rc": 0}, cases


def select(root, before_sha, after_sha, paths, observed, supplements=()):
    suites = inventory()
    before, after = tree(root, before_sha), tree(root, after_sha)
    traces = None
    if observed:
        try:
            summary, _ = audit_receipt(observed, supplements, suites, before_sha)
        except (ValueError, KeyError, OSError) as exc:
            return {"mode": "full", "reason": f"base audit unavailable: {exc}", "suites": list(suites)}
        if all(row.get("inputs") for row in summary["suites"]):
            traces = {row["suite"]: row["inputs"] for row in summary["suites"]}
    syntax = {}
    for files in (before, after):
        source_trees(root, files, syntax)
    graphs = [Imports(files, syntax, suites) for files in (before, after)]
    return pick(suites, paths, before, after, graphs, traces)


def observed_impact(paths, before, after, before_cases, after_cases):
    paths = set(paths)
    hits = defaultdict(dict)
    for label, summary in (("before", before), ("after", after)):
        for row in summary["suites"]:
            if row.get("inputs") is None:
                raise ValueError("full history comparison needs traced suite receipts")
            touched = sorted(paths.intersection(row["inputs"]))
            if touched:
                hits[row["suite"]][label + "_inputs"] = touched
    changed = defaultdict(list)
    for key in sorted(before_cases.keys() | after_cases.keys()):
        if before_cases.get(key) != after_cases.get(key):
            changed[key[0]].append(key[1])
    for suite, cases in changed.items():
        hits[suite]["changed_tests"] = cases
    return dict(sorted(hits.items()))


def verify_history(root, commit, before_path, after_path, before_retries, after_retries):
    parent = revision(root, commit + "^")
    child = revision(root, commit)
    paths = changed_paths(root, parent, child)
    predicted = select(root, parent, child, paths, before_path, before_retries)
    suites = inventory()
    before, before_cases = audit_receipt(before_path, before_retries, suites, parent)
    after, after_cases = audit_receipt(after_path, after_retries, suites, child)
    affected = observed_impact(paths, before, after, before_cases, after_cases)
    misses = sorted(affected.keys() - set(predicted["suites"]))
    rc = int(not affected or bool(misses))
    return {"parent": parent, "commit": child, "paths": paths, "prediction": predicted,
            "before_counts": dict(Counter(before_cases.values())),
            "after_counts": dict(Counter(after_cases.values())),
            "oracle": affected, "missed": misses, "rc": rc}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    selection = command.add_parser("select")
    selection.add_argument("--base", required=True)
    selection.add_argument("--head", default="HEAD")
    selection.add_argument("--observed", type=Path)
    selection.add_argument("--paths0", action="store_true", help="read NUL-separated changed paths from stdin")
    history = command.add_parser("verify-history")
    history.add_argument("commit")
    history.add_argument("--before", required=True, type=Path)
    history.add_argument("--after", required=True, type=Path)
    history.add_argument("--before-retry", action="append", default=[], type=Path)
    history.add_argument("--after-retry", action="append", default=[], type=Path)
    selection.add_argument("--observed-retry", action="append", default=[], type=Path)
    args = parser.parse_args()
    if args.command == "verify-history":
        result = verify_history(ROOT, args.commit, args.before, args.after,
                                args.before_retry, args.after_retry)
    else:
        base, head = (revision(ROOT, ref) for ref in (args.base, args.head))
        paths = changed_paths(ROOT, base, head)
        if args.paths0 and set(git_paths(sys.stdin.buffer.read())) != set(paths):
            result = {"mode": "full", "reason": "paths diverge from the supplied revisions",
                      "suites": list(inventory())}
        else:
            result = select(ROOT, base, head, paths, args.observed, args.observed_retry)
    print(json.dumps(result, indent=2, sort_keys=True))
    return result.get("rc", 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"impact selector: {exc}")
