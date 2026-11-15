#!/usr/bin/env python3
"""Reconciler: flags agents whose TaskLight card was never closed.

It trusts neither the projector's state nor its intentions. Inputs:
  * events.jsonl  : which agents enqueued work (the order of record), and
  * ledger.jsonl  : what the context-wall server actually acknowledged
                    (only calls with ok == true count).
An agent is flagged when it enqueued and has no acknowledged close_task after
its last acknowledged post_plan. Exit 0 clean, 1 flagged, 2 bad input.

Further checks (w186 hardening), all from the same two files:
  CLOSED_WITH_OPEN_STEPS  close_task acknowledged while a step of the last
                          plan was never acknowledged done/blocked;
  PLAN_MISSING_EVENTS     more distinct enqueued events than plan steps;
  OSB_MISSING_CLAIM       an enqueued event without an rc-0 osb claim;
  OSB_DUPLICATE_POST      the same act (agent, act, ref, text) acknowledged
                          more than once: a retry or a restart posted twice;
  UNPARSEABLE_ENQUEUE     a line of events.jsonl that is not JSON but
                          mentions "enqueue": work may be invisible here.
Other unparseable lines are counted in "bad_lines" and do not flag.
"""
import argparse
import json
import sys


class Rows(list):
    """Parsed rows plus what could not be parsed (never silently dropped)."""
    bad = 0
    bad_enqueue = 0


MAX_LINE_CHARS = 1 << 20  # same cap as project.MAX_LINE_BYTES


def load_jsonl(path):
    rows = Rows()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line) if len(line) <= MAX_LINE_CHARS else None
            except (ValueError, RecursionError, MemoryError):
                r = None
            if isinstance(r, dict):
                rows.append(r)
            else:
                rows.bad += 1
                if "enqueue" in line:
                    rows.bad_enqueue += 1
    return rows


def reconcile(events, ledger, room=None):
    enq, enq_ids = {}, {}
    for r in events:
        if r.get("rec") == "enqueue" and isinstance(r.get("agent_id"), str):
            enq[r["agent_id"]] = enq.get(r["agent_id"], 0) + 1
            if isinstance(r.get("event_id"), str):
                enq_ids.setdefault(r["agent_id"], set()).add(r["event_id"])
    last = {}  # agent -> {"plan": idx, "close": idx, "open": {index: state}}
    for i, r in enumerate(ledger):
        if r.get("sink") != "tasklight" or "tool" not in r or r.get("ok") is not True:
            continue
        if room and r.get("room") != room:
            continue
        a = last.setdefault(r.get("agent"), {"plan": None, "close": None, "steps": {}, "n": 0})
        args = r.get("arguments") or {}
        if r["tool"] == "post_plan":
            a["plan"], a["n"] = i, len(args.get("steps") or [])
        elif r["tool"] == "post_step":
            a["steps"][args.get("index")] = args.get("state")
        elif r["tool"] == "close_task":
            a["close"] = i
    flags = []
    for agent in sorted(set(enq) | {k for k in last if k}):
        a = last.get(agent, {"plan": None, "close": None, "steps": {}, "n": 0})
        if agent in enq and a["plan"] is None:
            flags.append({"agent": agent, "code": "NO_PLAN", "detail": "%d enqueue, no acknowledged post_plan" % enq[agent]})
            continue
        if a["plan"] is not None and (a["close"] is None or a["close"] < a["plan"]):
            open_steps = [i for i in range(a["n"]) if a["steps"].get(i) not in ("done", "blocked")]
            flags.append({"agent": agent, "code": "MISSING_CLOSE_TASK",
                          "detail": "no acknowledged close_task after the last plan; open steps %r" % open_steps})
        elif a["plan"] is not None:
            open_steps = [i for i in range(a["n"]) if a["steps"].get(i) not in ("done", "blocked")]
            if open_steps:
                flags.append({"agent": agent, "code": "CLOSED_WITH_OPEN_STEPS",
                              "detail": "close_task acknowledged, steps %r never acknowledged terminal" % open_steps})
        if a["plan"] is not None and len(enq_ids.get(agent, ())) > a["n"]:
            flags.append({"agent": agent, "code": "PLAN_MISSING_EVENTS",
                          "detail": "%d distinct enqueued events, last plan has %d steps"
                                    % (len(enq_ids[agent]), a["n"])})
    flags.extend(reconcile_osb(enq_ids, ledger))
    flags.extend(reconcile_unacked(ledger))
    if getattr(events, "bad_enqueue", 0):
        flags.append({"agent": None, "code": "UNPARSEABLE_ENQUEUE",
                      "detail": "%d unparseable line(s) of events mention enqueue" % events.bad_enqueue})
    return flags


def reconcile_unacked(ledger):
    """Keyed calls (any act, any tool) that failed and were never acknowledged
    by a later row with the same key: lost deliveries, whatever their kind."""
    acked, failed = set(), {}
    for r in ledger:
        if not isinstance(r.get("key"), str) or r.get("sink") not in ("osb", "tasklight"):
            continue
        k = (r["sink"], r.get("room") if isinstance(r.get("room"), str) else None, r["key"])
        ok = r.get("rc") == 0 if r["sink"] == "osb" else r.get("ok") is True
        if ok:
            acked.add(k)
        else:
            failed[k] = r
    out = []
    for k, r in failed.items():
        if k in acked:
            continue
        what = r.get("act") if k[0] == "osb" else r.get("tool")
        agent = r.get("agent") if isinstance(r.get("agent"), str) else None
        out.append({"agent": agent, "code": "UNACKED_CALL",
                    "detail": "%s %s key %s failed and was never acknowledged" % (k[0], str(what)[:40], k[2][:40])})
    return out


def reconcile_osb(enq_ids, ledger):
    """Only when the ledger has osb rows at all (a TaskLight-only ledger is legal)."""
    posts = [r for r in ledger if r.get("sink") == "osb" and r.get("rc") == 0]
    if not any(r.get("sink") == "osb" for r in ledger):
        return []
    flags, seen = [], {}
    for r in posts:
        if r.get("act") == "verdict":  # stop/resume may legitimately repeat verbatim
            continue
        k = json.dumps([r.get("room"), r.get("agent"), r.get("act"), r.get("ref"), r.get("argv")],
                       sort_keys=True)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] == 2:
            flags.append({"agent": r.get("agent"), "code": "OSB_DUPLICATE_POST",
                          "detail": "%s ref=%s acknowledged more than once" % (r.get("act"), r.get("ref"))})
    claimed = {(r.get("agent"), r.get("ref")) for r in posts if r.get("act") == "claim"}
    for agent in sorted(enq_ids):
        missing = sorted(e for e in enq_ids[agent] if (agent, e) not in claimed)
        if missing:
            flags.append({"agent": agent, "code": "OSB_MISSING_CLAIM",
                          "detail": "no rc-0 claim for %r" % missing[:8]})
    return flags


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--events", required=True)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--room", help="only count TaskLight calls to this room")
    args = ap.parse_args(argv)
    try:
        events, ledger = load_jsonl(args.events), load_jsonl(args.ledger)
        flags = reconcile(events, ledger, args.room)
    except OSError as ex:
        print(json.dumps({"error": str(ex)}))
        return 2
    print(json.dumps({"flagged": flags, "clean": not flags,
                      "bad_lines": {"events": events.bad, "ledger": ledger.bad}}, sort_keys=True))
    return 1 if flags else 0


if __name__ == "__main__":
    sys.exit(main())
