"""Replay audit of the readiness column of 'auction' records (Track P, Claude Fable 5.2).

Replay is the oracle: from events.jsonl alone, rebuild the judge's memory record by record and,
at every `auction` record, recompute the readiness of each eligible row from the records that
precede it. A scheduler that never consulted the judge, used a stale memory, or let a gated
intent win an auction is flagged.

    python3 -m judge.replay --rt RT        # rc 0 clean, rc 1 violations (one JSON line each)

Codes
    READINESS_MISMATCH  recorded readiness differs from the recomputed score
    UNKNOWN_INTENT      an eligible row names an event with no preceding enqueue record
    GATED_WINNER        the winner's recorded or recomputed readiness is 0 and the reason is plain 'auction'
    NO_HEAD_FRAME       the auction record carries no head_frame (readiness cannot be audited)
Bids, prices, balances and the wait bound are audited by the scheduler's own verify_log.
"""
import argparse
import json
import os
import sys

from .judge import Judge, LLM_CLAMP

UNREADY_OK = ("aging_guard", "all_unready", "sole")  # reasons that may serve a readiness-0 intent


def audit(records, claims, gui_agents=None, llm_tolerance=0):
    """Returns (violations, stats). violations: [{"line", "code", "turn", "agent_id", "event_id", ...}].

    llm_tolerance: accept |recorded - recomputed| <= this (use LLM_CLAMP when cached advice was in play;
    advice is not in events.jsonl, so it cannot be replayed). A gated row must still be exactly 0.
    """
    j = Judge(claims, gui_agents)
    intents, out = {}, []
    stats = {"auctions": 0, "rows": 0, "holds": 0}
    for n, r in enumerate(records, 1):
        if not isinstance(r, dict):
            continue
        if r.get("rec") == "enqueue" and isinstance(r.get("event_id"), str):
            intents[r["event_id"]] = r
        if r.get("rec") == "auction":
            if r.get("reason") == "hold":
                stats["holds"] += 1
            else:
                stats["auctions"] += 1
                out.extend(_audit_one(n, r, j, intents, llm_tolerance, stats))
        j.observe(r)
    return out, stats


def _audit_one(n, r, j, intents, tol, stats):
    bad = []

    def flag(code, row, **kw):
        v = {"line": n, "code": code, "turn": r.get("turn"), "agent_id": row.get("agent_id"),
             "event_id": row.get("event_id")}
        v.update(kw)
        bad.append(v)

    head = r.get("head_frame")
    rows = [x for x in (r.get("eligible") or []) if isinstance(x, dict)]
    if type(head) is not int:
        if rows:
            flag("NO_HEAD_FRAME", {})
        return bad
    for row in rows:
        stats["rows"] += 1
        it = intents.get(row.get("event_id"))
        if it is None or it.get("agent_id") != row.get("agent_id"):
            flag("UNKNOWN_INTENT", row)
            continue
        strict = j.score(it, {"head_frame": head}, advice=None)
        # the pixel-identity rebase proof is the broker's and is not in the log: accept either reading
        allowed = {strict["score"]}
        if strict["gate"] == "STALE_FRAME":
            allowed.add(j.score(it, {"head_frame": head}, advice=None, identical_pixels=True)["score"])
        got = row.get("readiness")
        ok = type(got) is int and any(abs(got - a) <= (tol if a > 0 else 0) for a in allowed)
        if not ok:
            flag("READINESS_MISMATCH", row, recorded=got, recomputed=sorted(allowed), gate=strict["gate"])
        if (row.get("agent_id") == r.get("winner") and row.get("event_id") == r.get("event_id")
                and (got == 0 or max(allowed) == 0) and r.get("reason") not in UNREADY_OK):
            flag("GATED_WINNER", row, reason=r.get("reason"), gate=strict["gate"])
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rt", default=os.environ.get("VP_SWARM_RT", "/dev/shm/vqa-cu/ref"))
    ap.add_argument("--llm", action="store_true", help="tolerate the bounded LLM adjustment (+-%d)" % LLM_CLAMP)
    a = ap.parse_args(argv)
    try:
        with open(os.path.join(a.rt, "claims.json")) as f:
            claims = json.load(f)
    except (OSError, ValueError):
        claims = {}
    records = []
    try:
        with open(os.path.join(a.rt, "events.jsonl")) as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    records.append(None)  # keeps line numbers honest
    except OSError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    bad, stats = audit(records, claims, llm_tolerance=LLM_CLAMP if a.llm else 0)
    for v in bad:
        print(json.dumps(v, sort_keys=True))
    stats["violations"] = len(bad)
    print(json.dumps(stats, sort_keys=True))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
