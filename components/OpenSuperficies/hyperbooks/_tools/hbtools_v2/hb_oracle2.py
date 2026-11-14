#!/usr/bin/env python3
"""HB2-08: oracle coverage + mutation testing that a tie cannot dodge.

hb_oracle2.py --subject subject --out out/oracle_kill.json [--manifest fixtures/mutants_oracle.json] [--jobs 4]

Per mutant (hb.mutant.v2): (1) unique-substring check (n == 1 else E_AMBIG); (2) pre-condition on the clean run under line tracing:
the mutated line ran >= 1 time and, for cmp_* / tie_rev, a tie was observed (else E_ORACLE_UNEXERCISED, nothing is mutated);
(3) the clean run must print the injected nonce `HB_ORACLE_DONE <24-hex>` with rc 0 and, run under `-O`, at least 2 assertion
checks (else E_TEST_WEAK); (4) mutated run: killed iff the wait status is non-zero (exit or signal) or the nonce is missing;
(5) ranking mutants also face K = 8 permutation witnesses (any difference to the clean tree kills).
"""
import argparse, concurrent.futures as cf, hashlib, json, os, shutil, subprocess, sys, tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402

HERE = L.HERE
DRIVER = os.path.join(HERE, "oracle_driver.py")
WITNESS = os.path.join(HERE, "oracle_witness.py")
K_PERM = 8
TIMEOUT = 600


def nonce_for(mid):
    return hashlib.sha256(("oracle|" + mid).encode()).hexdigest()[:24]


def copy_tree(src, dst, extras=None):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    if extras:
        for f in sorted(os.listdir(extras)):
            if f.endswith(".py"):
                shutil.copy2(os.path.join(extras, f), os.path.join(dst, "tests", f))


def decode_status(rc):
    """-> (exit_code or None, signal or None) from a subprocess return code."""
    if rc < 0:
        return None, -rc
    return rc, None


def run_driver(root, modules, nonce, trace=None, spec=None, hits=None, opt=False, timeout=TIMEOUT):
    cmd = [sys.executable] + (["-O"] if opt else []) + [DRIVER, "--tests-dir", os.path.join(root, "tests"), "--modules", ",".join(modules), "--nonce", nonce]
    if trace:
        cmd += ["--trace", trace]
        if spec:
            cmd += ["--spec", json.dumps(spec)]
        cmd += ["--hits", hits]
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    except subprocess.TimeoutExpired:
        return {"rc": 124, "exit": 124, "signal": None, "nonce": False, "checks": 0, "timeout": True}
    out = p.stdout.split("\n")
    checks = next((int(l.split()[1]) for l in out if l.startswith("HB_ORACLE_CHECKS")), 0)
    ex, sg = decode_status(p.returncode)
    return {"rc": p.returncode, "exit": ex, "signal": sg, "nonce": ("HB_ORACLE_DONE " + nonce) in out, "checks": checks, "timeout": False}


def covering_modules(trace, anchor, all_modules):
    mods = sorted({t.split("::")[0].split("/")[-1][:-3] for t, s in trace["edges"] if s == anchor})
    return mods or all_modules


def apply_mutant(root, m):
    p = os.path.join(root, m["file"])
    t = open(p, encoding="utf-8").read()
    if t.count(m["before"]) != 1:
        return False
    open(p, "w", encoding="utf-8", newline="").write(t.replace(m["before"], m["after"], 1))
    return True


def witness(root, seed):
    p = subprocess.run([sys.executable, WITNESS, "--root", root, "--seed", str(seed)], capture_output=True, text=True, timeout=TIMEOUT,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    return p.returncode, p.stdout


def one(subject, m, trace, all_modules, scratch, extras=None):
    mid = m["id"]
    nonce = nonce_for(mid)
    row = {"id": mid, "anchor": m["anchor"], "op": m["op"], "code": "OK"}
    src = open(os.path.join(subject, m["file"]), encoding="utf-8").read()
    if src.count(m["before"]) != 1:
        row.update(code="E_AMBIG", killed=False)
        return row
    mods = covering_modules(trace, m.get("cover", m["anchor"]), all_modules)
    if extras and m.get("extra"):
        mods = mods + [m["extra"]]
    row["modules"] = mods
    clean = os.path.join(scratch, mid + "_clean")
    copy_tree(subject, clean, extras)
    hits = os.path.join(scratch, mid + "_hits.json")
    c = run_driver(clean, mods, nonce, trace=os.path.join(clean, m["file"]) + ":" + str(m["line"]), spec=m.get("tie"), hits=hits)
    row["clean"] = {k: c[k] for k in ("rc", "nonce", "checks")}
    hj = json.load(open(hits)) if os.path.isfile(hits) else {"hits": 0, "tie": None}
    row["line_hits"] = hj["hits"]
    row["tie_observed"] = hj["tie"]
    shutil.rmtree(clean, ignore_errors=True)
    if c["rc"] != 0 or not c["nonce"]:
        row.update(code="E_ORACLE_CLEAN", killed=False)
        return row
    if hj["hits"] < 1 or (m.get("tie") and not hj["tie"]):
        row.update(code="E_ORACLE_UNEXERCISED", killed=False)
        return row
    cl = os.path.join(scratch, mid + "_opt")
    copy_tree(subject, cl, extras)
    o = run_driver(cl, mods, nonce, opt=True)
    shutil.rmtree(cl, ignore_errors=True)
    row["checks_O"] = o["checks"]
    weak = o["checks"] < 2
    mut = os.path.join(scratch, mid + "_mut")
    copy_tree(subject, mut, extras)
    apply_mutant(mut, m)
    r = run_driver(mut, mods, nonce)
    row["mutated"] = {"exit": r["exit"], "signal": r["signal"], "nonce": r["nonce"], "timeout": r["timeout"]}
    killed = r["rc"] != 0 or not r["nonce"]
    row["killed_by_tests"] = killed
    if m.get("perm"):
        diffs = 0
        base = os.path.join(scratch, mid + "_base")
        copy_tree(subject, base)
        for k in range(1, K_PERM + 1):
            rc1, o1 = witness(base, k)
            rc2, o2 = witness(mut, k)
            if rc1 != 0:
                row["code"] = "E_ORACLE_CLEAN"
            if rc1 != rc2 or o1 != o2:
                diffs += 1
        shutil.rmtree(base, ignore_errors=True)
        row["perm_diffs"] = diffs
        row["perm_runs"] = K_PERM
        killed = killed or diffs > 0
    shutil.rmtree(mut, ignore_errors=True)
    row["killed"] = killed
    if weak:
        row["code"] = "E_TEST_WEAK"
    return row


def run(subject, manifest, trace, jobs=4, extras=None):
    mans = L.jload(manifest)["mutants"]
    tests = sorted(f[:-3] for f in os.listdir(os.path.join(subject, "tests")) if f.startswith("test_") and f.endswith(".py"))
    scratch = tempfile.mkdtemp(prefix="hboracle")
    try:
        with cf.ThreadPoolExecutor(jobs) as ex:
            rows = list(ex.map(lambda m: one(subject, m, trace, tests, scratch, extras), mans))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    p = len(rows)
    k = sum(1 for r in rows if r.get("killed"))
    summ = {"p": p, "k": k, "unexercised": sum(1 for r in rows if r["code"] == "E_ORACLE_UNEXERCISED"), "weak": sum(1 for r in rows if r["code"] == "E_TEST_WEAK"),
            "clean_failures": sum(1 for r in rows if r["code"] == "E_ORACLE_CLEAN"), "survived": [r["id"] for r in rows if not r.get("killed")]}
    return rows, summ


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=L.SUBJECT)
    ap.add_argument("--manifest", default=os.path.join(L.HB, "fixtures", "mutants_oracle.json"))
    ap.add_argument("--trace", default=os.path.join(L.HB, "out", "trace.json"))
    ap.add_argument("--extras", default=os.path.join(L.HB, "oracle_extra"))
    ap.add_argument("--out", default=os.path.join(L.HB, "out", "oracle_kill.json"))
    ap.add_argument("--jobs", type=int, default=5)
    ap.add_argument("--no-baseline", action="store_true", help="skip the subject-suite-only baseline run")
    a = ap.parse_args(argv)
    trace = L.jload(a.trace)
    base = None
    if not a.no_baseline:
        brows, bsumm = run(a.subject, a.manifest, trace, a.jobs, None)
        base = {"summary": bsumm, "rows": brows}
    rows, summ = run(a.subject, a.manifest, trace, a.jobs, a.extras)
    inputs = {"manifest_sha256": L.sha_file(a.manifest)}
    sp = os.path.join(os.path.dirname(a.trace), "subject.sha256.json")
    if os.path.isfile(sp):
        inputs["subject_sha256"] = L.sha_file(sp)
    if os.path.isdir(a.extras):
        inputs["extras_sha256"] = L.sha256b(b"".join(L.readb(os.path.join(a.extras, f)) for f in sorted(os.listdir(a.extras)) if f.endswith(".py")))
    metrics = {k: v for k, v in summ.items() if isinstance(v, int)}
    if base:
        metrics["baseline_k"] = base["summary"]["k"]
        metrics["baseline_p"] = base["summary"]["p"]
    doc = {"schema": "hb.receipt.v2", "kind": "mutation", "tool": "hb_oracle2.py/2.0", "metrics": metrics, "summary": summ, "rows": rows,
           "baseline_subject_suite": base, "inputs": inputs}
    L.write_json(a.out, doc)
    print("k=%d p=%d unexercised=%d weak=%d survived=%s baseline_k=%s" % (summ["k"], summ["p"], summ["unexercised"], summ["weak"], summ["survived"],
                                                                      base["summary"]["k"] if base else None))
    return 0 if summ["k"] == summ["p"] and not summ["unexercised"] and not summ["weak"] and not summ["clean_failures"] else 5


if __name__ == "__main__":
    sys.exit(main())
