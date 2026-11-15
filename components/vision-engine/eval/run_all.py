"""Runner: executes every available experiment driver (X1-X5, X8-X10), collects vqa.eval-summary/v1 dicts and writes one
markdown report. Drivers of other lanes are imported when present, else reported AUSENTE. Token experiments (X0, X6, X7)
need a judge and real captures: AUSENTE. Deterministic output (no clock). Usage: python3 -m eval.run_all --out <dir> [--only X8,X9]"""
import argparse
import importlib
import sys
from pathlib import Path

from visioncore import canon

DRIVERS = (("X1", "eval.x1_determinism"), ("X2", "eval.x2_crossrun"), ("X3", "eval.x3_power"), ("X4", "eval.x4_fp"),
           ("X5", "eval.x5_cpu"), ("X8", "eval.x8_citations"), ("X9", "eval.x9_vmem"), ("X10", "eval.x10_compaction"))
TOKEN_X = (("X0", "v1.0 baseline re-run"), ("X6", "judge 2x2 Flash/Argon x S6 off/on"), ("X7", "placebo"))


def _summary(res):
    return res[1] if isinstance(res, tuple) else res


def run_one(x, modname, out):
    try:
        mod = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name != modname:
            return {"x": x, "verdict": "ERROR", "error": "import: %s" % e}
        return {"x": x, "verdict": "AUSENTE", "reason": "driver %s not present" % modname}
    try:
        s = dict(_summary(mod.run(out)))
    except Exception as e:  # a failing driver must not hide the others
        return {"x": x, "verdict": "ERROR", "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
    s.setdefault("x", x)
    return s


def _scalars(s):
    skip = {"schema", "x", "gate", "verdict", "source", "reason", "error"}
    out = []
    for k in sorted(s):
        v = s[k]
        if k not in skip and isinstance(v, (int, str, bool)) and len(str(v)) <= 90:
            out.append("%s=%s" % (k, v))
    return ", ".join(out[:14])


def report(summaries):
    L = ["# vision-engine eval report", "", "| X | gate | verdict | source |", "|---|---|---|---|"]
    for s in summaries:
        L.append("| %s | %s | %s | %s |" % (s["x"], s.get("gate", "-"), s["verdict"], s.get("source", "-")))
    for x, what in TOKEN_X:
        L.append("| %s | - | AUSENTE | needs judge tokens and real captures (%s) |" % (x, what))
    L += ["", "## Details", ""]
    for s in summaries:
        L.append("### %s" % s["x"])
        L.append("")
        L.append("- " + (_scalars(s) or s.get("reason") or s.get("error") or "-"))
        for k in ("reason", "error"):
            if k in s:
                L.append("- %s: %s" % (k, s[k]))
        L.append("")
    bad = [s["x"] for s in summaries if s["verdict"] in ("FAIL", "ERROR")]
    ran = [s["x"] for s in summaries if s["verdict"] not in ("AUSENTE",)]
    L.append("## Overall: %s (ran %s; failing %s)" % ("FAIL" if bad else "PASS", ",".join(ran) or "none", ",".join(bad) or "none"))
    return "\n".join(L) + "\n", not bad


def run(out_dir, only=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sums = [run_one(x, m, out) for x, m in DRIVERS if only is None or x in only]
    text, ok = report(sums)
    canon.write_atomic(out / "report.md", text.encode("utf-8"))
    canon.write_atomic(out / "summaries.json", canon.dumps({"summaries": sums}).encode("utf-8"))
    return sums, ok


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None, help="comma list, e.g. X8,X9,X10")
    a = ap.parse_args(argv)
    sums, ok = run(a.out, set(a.only.split(",")) if a.only else None)
    rc = 0 if ok else 5
    sys.stdout.write(canon.dumps({"code": "OK" if ok else "E_GATE", "rc": rc,
                                  "verdicts": {s["x"]: s["verdict"] for s in sums}}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
