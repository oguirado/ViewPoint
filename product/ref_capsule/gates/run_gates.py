#!/usr/bin/env python3
"""Run the ViewPoint capsule gate suite from a manifest (Track P, Claude-written: claude-fable-5-2).

    python3 -I -B run_gates.py --manifest manifest.json [--json out.json] [--only G1,G4]
    python3 -I -B run_gates.py --only G2 --logs 'swarm/proof/*.jsonl' 'product/proof/*.jsonl'   # secrets preflight

Manifest (all keys optional; a gate with no input is UNOBSERVED, which is not a pass):
  {"logs": ["<glob of proof *.jsonl>"], "sources": ["<dir or file>"], "worktrees": ["<git worktree>"],
   "base": "<git ref the branch must not differ from under swarm/>", "pids": [123],
   "bridges": ["<bridge.py>"], "claims": "<claims.json>", "events": "<events.jsonl>",
   "capsule_spec": "<vp-capsule/v1 spec.json>", "capsule_snapshot": "<snapshot.json>",
   "capsule_cgroot": "/sys/fs/cgroup", "freshness_ms": 30000,
   "waivers": [{"rule": "<rule id>", "where": "<substring of the finding's where>", "reason": "<why>"}]}

Exit code: 0 all GREEN, 1 any RED, 2 no RED but some UNOBSERVED. Read-only except for a temp dir (G5).
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import g1_paths  # noqa: E402
import g2_secrets  # noqa: E402
import g3_memcaps  # noqa: E402
import g4_pidstops  # noqa: E402
import g5_nonce  # noqa: E402
import g6_claims  # noqa: E402
import g7_reconcile  # noqa: E402
from common import GREEN, RED, UNOBSERVED, finding, restatus, stream_is_closed  # noqa: E402


def expand(patterns):
    out = []
    for p in patterns or []:
        hits = sorted(glob.glob(os.path.expanduser(p)))
        out += hits if hits else [p]
    return out


def run_all(m, only=None):
    logs = expand(m.get("logs"))
    sources = expand(m.get("sources"))
    gates = [
        ("G1", lambda: g1_paths.run(logs, sources, m.get("worktrees") or (), m.get("base"))),
        ("G2", lambda: g2_secrets.run(logs)),
        ("G3", lambda: g3_memcaps.run(logs, sources, m.get("pids") or ())),
        ("G4", lambda: g4_pidstops.run(logs, sources)),
        ("G5", lambda: g5_nonce.run(expand(m.get("bridges")))),
        ("G6", lambda: g6_claims.run(m.get("claims"), m.get("events"))),
        ("G7", lambda: g7_reconcile.run(m.get("capsule_spec"), m.get("capsule_snapshot"),
                                             freshness_ms=m.get("freshness_ms", 30000),
                                             cgroot=m.get("capsule_cgroot"))),
    ]
    results = []
    for gid, fn in gates:
        if only and gid not in only:
            continue
        results.append(fn())
    # an open stream log is still being written: whatever was observed in it is not final
    open_logs = [p for p in logs if os.path.isfile(p) and not stream_is_closed(p)]
    for r in results:
        if open_logs and r["gate"].startswith(("G1", "G2", "G3", "G4")) and r["status"] == GREEN:
            r["findings"].append(finding("LOG_STILL_OPEN", open_logs[0], "%d log(s) do not end with a result record"
                                         % len(open_logs), "unobserved"))
            r["status"] = UNOBSERVED
    apply_waivers(results, m.get("waivers") or [])
    for r in results:  # a command excerpt quoted by G1/G3/G4 must not re-publish a secret G2 would flag
        for f in r["findings"]:
            f["detail"] = scrub(f["detail"])
    return results


def scrub(text):
    for rule, rx in g2_secrets.VALUE_RULES:
        text = rx.sub("<redacted %s>" % rule, text)
    return text


def _where_matches(w, where):
    """One place only (xr C15): the whole `where`, or a path suffix that starts at a path
    component and carries the file name. A bare "/" or a directory fragment waives nothing."""
    if not isinstance(w, str) or not w.strip("/ .:"):
        return False
    if w == where:
        return True
    return where.endswith(w) and (w.startswith("/") or where[-len(w) - 1] == "/") \
        and bool(os.path.basename(w.split(":")[0]))


def apply_waivers(results, waivers):
    """A waiver is explicit, has a reason and stays in the report (severity "waived"); it only matches
    one rule at one place: {"rule": "DISPLAY_1", "where": "<substring of where>", "reason": "<text>"}."""
    for r in results:
        for f in r["findings"]:
            for w in waivers:
                if f["severity"] == "red" and w.get("reason") and w.get("where") and f["rule"] == w.get("rule") \
                        and _where_matches(w["where"], f["where"]):
                    f["severity"], f["waiver"] = "waived", w["reason"]
        restatus(r)


def overall(results):
    st = {r["status"] for r in results}
    return RED if RED in st else UNOBSERVED if (UNOBSERVED in st or not results) else GREEN


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--manifest")
    ap.add_argument("--logs", nargs="+", help="proof logs or globs; shortcut for a manifest with only `logs`")
    ap.add_argument("--json")
    ap.add_argument("--only", default="")
    ap.add_argument("--max-findings", type=int, default=8)
    a = ap.parse_args(argv)
    m = {}
    if a.manifest:
        with open(a.manifest) as f:
            m = json.load(f)
    if a.logs:
        m["logs"] = list(m.get("logs") or []) + a.logs
    if not a.manifest and not a.logs:
        ap.error("give --manifest and/or --logs")
    results = run_all(m, {x.strip() for x in a.only.split(",") if x.strip()} or None)
    for r in results:
        reds = [x for x in r["findings"] if x["severity"] == "red"]
        print("%-22s %-10s observed=%d red=%d other=%d" % (r["gate"], r["status"], r["observed_count"], len(reds),
                                                          len(r["findings"]) - len(reds)))
        for x in (reds + [y for y in r["findings"] if y["severity"] != "red"])[:a.max_findings]:
            print("    [%s] %s %s: %s" % (x["severity"], x["rule"], x["where"], x["detail"][:160]))
    st = overall(results)
    print("OVERALL %s" % st)
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"overall": st, "results": results}, f, indent=1, sort_keys=True)
    return {GREEN: 0, RED: 1, UNOBSERVED: 2}[st]


if __name__ == "__main__":
    sys.exit(main())
