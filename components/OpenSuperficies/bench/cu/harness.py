#!/usr/bin/env python3
"""Pre-registered CU benchmark harness skeleton (os-cu-0 W1). No measurement runs.

rc: 0 ok, 2 usage/schema, 5 refused. Never 1.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

SCHEMA = "os.cu.prereg/v1"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TOP = {"schema", "version", "seeds", "min_claim_seeds", "wilson_z_milli", "arms", "metrics", "tasks"}
ARM = {"id", "model", "role"}
TASK_REF = {"id", "task_file", "task_sha256"}
TASK = {"id", "surface", "task", "oracle", "budget"}
ORACLE = {"channel", "description"}
BUDGET = {"steps", "wall_s"}
METRICS = ["success_permille", "actions_median", "actions_p90", "cost_tok_mean",
           "cost_usd_e4_mean", "wrong_per_1000", "abstain_permille", "stop_hist"]
EFFECTS = {"NONE", "EFFECT_OK", "WRONG_EFFECT", "SIDE_EFFECT"}
STEP = {"task", "seed", "arm", "step", "effect", "final", "stop", "tok", "usd_e4"}


def _isint(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _closed(obj, keys, where, errs):
    if not isinstance(obj, dict):
        errs.append("%s: not an object" % where)
        return False
    for k in sorted(set(obj) ^ keys):
        errs.append("%s: %s key %s" % (where, "unknown" if k in obj else "missing", k))
    return True


def _floats(v, where, errs):
    if isinstance(v, float):
        errs.append("%s: float not allowed" % where)
    elif isinstance(v, dict):
        for k, x in v.items():
            _floats(x, where + "." + str(k), errs)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            _floats(x, "%s[%d]" % (where, i), errs)


def canonical_sha256(m):
    b = json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(b).hexdigest()


def validate(m, base):
    """Return a list of error strings; empty means valid. base = dir of task files."""
    errs = []
    _floats(m, "$", errs)
    if not _closed(m, TOP, "$", errs):
        return errs
    if m.get("schema") != SCHEMA:
        errs.append("schema must be %s" % SCHEMA)
    for k in ("version", "min_claim_seeds", "wilson_z_milli"):
        if not _isint(m.get(k)) or m[k] <= 0:
            errs.append("%s must be positive int" % k)
    seeds = m.get("seeds")
    if _closed(seeds, {"cal", "test"}, "seeds", errs):
        for s in ("cal", "test"):
            v = seeds.get(s)
            if not (isinstance(v, list) and v and all(_isint(x) and x > 0 for x in v)) or len(set(v)) != len(v):
                errs.append("seeds.%s must be non-empty unique positive ints" % s)
        if isinstance(seeds.get("cal"), list) and isinstance(seeds.get("test"), list):
            if set(seeds["cal"]) & set(seeds["test"]):
                errs.append("seeds: cal/test overlap")
            if _isint(m.get("min_claim_seeds")) and len(seeds["test"]) < m["min_claim_seeds"]:
                errs.append("seeds.test smaller than min_claim_seeds")
    arms = m.get("arms")
    if not isinstance(arms, list) or not arms:
        errs.append("arms must be non-empty list")
    else:
        for i, a in enumerate(arms):
            if _closed(a, ARM, "arms[%d]" % i, errs):
                if not all(isinstance(a.get(k), str) and a[k] for k in ARM):
                    errs.append("arms[%d]: fields must be non-empty strings" % i)
    if m.get("metrics") != METRICS:
        errs.append("metrics must equal the pre-registered list")
    tasks = m.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errs.append("tasks must be non-empty list")
        return errs
    ids = [t.get("id") for t in tasks if isinstance(t, dict)]
    if len(set(ids)) != len(ids):
        errs.append("tasks: duplicate id")
    for i, t in enumerate(tasks):
        w = "tasks[%d]" % i
        if not _closed(t, TASK_REF, w, errs):
            continue
        rel = t.get("task_file")
        if not isinstance(rel, str) or os.path.isabs(rel) or ".." in rel.split("/"):
            errs.append("%s: task_file must be relative without .." % w)
            continue
        p = os.path.join(base, rel)
        tasks_dir = os.path.realpath(os.path.join(base, "tasks"))
        if not os.path.realpath(p).startswith(tasks_dir + os.sep):
            errs.append("%s: task_file escapes tasks dir" % w)
            continue
        try:
            raw = open(p, "rb").read()
        except OSError:
            errs.append("%s: task_file unreadable" % w)
            continue
        if hashlib.sha256(raw).hexdigest() != t.get("task_sha256"):
            errs.append("%s: task_sha256 mismatch" % w)
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            errs.append("%s: task_file not JSON" % w)
            continue
        _floats(d, w, errs)
        if _closed(d, TASK, w + ".task", errs):
            if d.get("id") != t.get("id"):
                errs.append("%s: task id mismatch" % w)
            if _closed(d.get("oracle"), ORACLE, w + ".oracle", errs):
                if not all(isinstance(d["oracle"][k], str) and d["oracle"][k] for k in ORACLE):
                    errs.append("%s.oracle: empty field" % w)
            if _closed(d.get("budget"), BUDGET, w + ".budget", errs):
                if not all(_isint(d["budget"][k]) and d["budget"][k] > 0 for k in BUDGET):
                    errs.append("%s.budget: must be positive ints" % w)
    return errs


def wilson_lb_permille(k, n, z_milli=1960, min_n=5):
    """Wilson lower bound in permille, integer arithmetic; None (no claim) if n < min_n."""
    if n < min_n or not 0 <= k <= n:
        return None
    Z = z_milli * z_milli  # z^2 in 1e-6 units
    X = Z * (4 * k * (n - k) * 10 ** 6 + n * Z) // (4 * n)
    s = _isqrt(X)
    num = k * 10 ** 6 + Z // 2 - s
    den = n * 10 ** 6 + Z
    return max(0, 1000 * num // den)


def _isqrt(x):
    import math
    return math.isqrt(x)


def _pctl(sorted_vals, pct):
    n = len(sorted_vals)
    return sorted_vals[max(0, (pct * n + 99) // 100 - 1)]


def metrics_from_steps(steps):
    """Pure integer metrics per task x arm from act_steps records (episode = task, seed, arm)."""
    eps = {}
    for r in steps:
        if set(r) != STEP:
            raise ValueError("step record keys")
        if r["effect"] not in EFFECTS or not all(_isint(r[k]) for k in ("seed", "step", "final", "tok", "usd_e4")):
            raise ValueError("step record values")
        eps.setdefault((r["task"], r["arm"]), {}).setdefault(r["seed"], []).append(r)
    out = {}
    for (task, arm), by_seed in sorted(eps.items()):
        n = len(by_seed)
        lens, tok, usd, wrong, total, succ, abst, hist = [], 0, 0, 0, 0, 0, 0, {}
        for seed in sorted(by_seed):
            rows = sorted(by_seed[seed], key=lambda r: r["step"])
            lens.append(len(rows))
            fin = [r for r in rows if r["final"]]
            stop = fin[-1]["stop"] if fin else "INCOMPLETE"
            hist[stop] = hist.get(stop, 0) + 1
            succ += stop == "EFFECT_OK"
            abst += stop == "ABSTAIN"
            for r in rows:
                tok += r["tok"]
                usd += r["usd_e4"]
                wrong += r["effect"] in ("WRONG_EFFECT", "SIDE_EFFECT")
                total += 1
        lens.sort()
        out.setdefault(task, {})[arm] = {
            "episodes": n,
            "success_permille": 1000 * succ // n,
            "actions_median": lens[(n - 1) // 2],
            "actions_p90": _pctl(lens, 90),
            "cost_tok_mean": tok // n,
            "cost_usd_e4_mean": usd // n,
            "wrong_per_1000": 1000 * wrong // total,
            "abstain_permille": 1000 * abst // n,
            "stop_hist": dict(sorted(hist.items())),
        }
    return out


def tree_dirty():
    """Dirty or unverifiable = True. Always checks the repo that contains this harness, whatever the cwd."""
    top = subprocess.run(["git", "-C", REPO, "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=60)
    if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != os.path.realpath(REPO):
        return True
    r = subprocess.run(["git", "-C", REPO, "status", "--porcelain"], capture_output=True, text=True, timeout=60)
    return r.returncode != 0 or bool(r.stdout.strip())


def untracked(paths):
    """Paths that are not a tracked, unmodified file of REPO (untracked, ignored, modified, outside, unverifiable)."""
    bad = []
    for p in paths:
        real = os.path.realpath(p)
        r = subprocess.run(["git", "--literal-pathspecs", "-C", REPO, "status", "--porcelain", "--ignored", "-z",
                            "--", os.path.relpath(real, os.path.realpath(REPO))],
                           capture_output=True, text=True, timeout=60)
        if not os.path.isfile(real) or r.returncode != 0 or r.stdout:
            bad.append(p)
    return bad


TAMPER = ("task_sha256 mismatch", "task_file unreadable", "task_file escapes tasks dir")


def errs_rc(errs):
    """5 only when every error is task-file tamper; any other schema error is 2."""
    return 5 if all(e.endswith(TAMPER) for e in errs) else 2


def _out(obj):
    print(json.dumps(obj, sort_keys=True))


def _load(path):
    with open(path, "rb") as f:
        return json.loads(f.read())


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write("usage: %s\n" % message)
        raise SystemExit(2)


def main(argv=None):
    ap = _Parser(prog="harness.py", add_help=False)
    ap.add_argument("--dry-run", metavar="PREREG")
    ap.add_argument("--run", metavar="PREREG")
    ap.add_argument("--split", choices=["cal", "test"])
    ap.add_argument("--resource-ok", metavar="F")
    ap.add_argument("--expect-sha", metavar="SHA")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if bool(a.dry_run) == bool(a.run):
        sys.stderr.write("usage: exactly one of --dry-run/--run\n")
        return 2
    path = a.dry_run or a.run
    if a.run and not a.split:
        sys.stderr.write("usage: --run requires --split cal|test\n")
        return 2
    try:
        m = _load(path)
    except (OSError, ValueError):
        sys.stderr.write("E_SCHEMA: prereg unreadable\n")
        return 2
    errs = validate(m, os.path.dirname(os.path.abspath(path)))
    sha = canonical_sha256(m) if isinstance(m, dict) else ""
    if errs:
        _out({"status": "invalid", "errors": errs})
        return errs_rc(errs)
    if a.dry_run:
        _out({"status": "valid", "prereg_sha256": sha, "tasks": len(m["tasks"]),
              "seeds_cal": m["seeds"]["cal"], "seeds_test": m["seeds"]["test"]})
        return 0
    if not a.resource_ok or not os.path.isfile(a.resource_ok):
        _out({"status": "refused", "reason": "resource_ok_missing"})
        return 5
    if a.expect_sha != sha:
        _out({"status": "refused", "reason": "sha_drift", "prereg_sha256": sha})
        return 5
    if a.split == "test" and (not os.path.realpath(path).startswith(os.path.realpath(REPO) + os.sep)
                              or tree_dirty()):
        _out({"status": "refused", "reason": "dirty_tree"})
        return 5
    if a.split == "test":
        base = os.path.dirname(os.path.abspath(path))
        bad = untracked([path] + [os.path.join(base, t["task_file"]) for t in m["tasks"]])
        if bad:
            _out({"status": "refused", "reason": "untracked_input", "files": bad})
            return 5
    _out({"status": "authorized", "split": a.split, "prereg_sha256": sha, "runs_executed": 0})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never rc 1
        sys.stderr.write("E_INTERNAL: %s\n" % type(e).__name__)
        sys.exit(7)
