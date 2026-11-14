"""G6 claim enforcement: reconcile the claims table (desired) with the broker's event ledger (observed).

Desired: RT/claims.json {"agent": ["col:<Collection>", ...]}; an agent's script job may only write
         collections it owns; "0 accepted cross-claim jobs" (04_LANZAMIENTO C4 pass criterion).
Observed: RT/events.jsonl. Four separate records are read, as in CT-0022 C1:
         requested = enqueue.write_set, decided = close.verdict, effect = close.changed_write_set /
         spill_write_set (measured by the bridge), uncertain = enqueue with no close.
The gate does not trust the verdict alone: a job that was refused must also show no measured effect,
and a job that was verified must show no effect outside its claim.
"""
import json
import os

from common import finding, result

REFUSALS = {"CLAIM_REFUSED", "NO_LEASE", "STALE_FRAME", "FRAME_EVICTED", "QUEUE_FULL", "DUPLICATE_EVENT",
            "BAD_INTENT", "PASS", "LEASE_EXPIRED", "ACTUATOR_BUSY", "FOOTPRINT_MISSING"}
CLEAN = {"STATE_VERIFIED", "VERIFIED"}


def load_events(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                out.append((ln, None))
                continue
            out.append((ln, rec))
    return out


def reconcile(claims, events, path="events.jsonl"):
    findings = []
    owner = {}
    for agent, cols in sorted(claims.items()):
        for c in cols:
            if c in owner:
                findings.append(finding("CLAIMS_OVERLAP", "claims.json", "%s is claimed by %s and %s"
                                        % (c, owner[c], agent)))
            owner[c] = agent
    enq, closed, n = {}, {}, 0
    for ln, rec in events:
        if rec is None:
            findings.append(finding("LEDGER_CORRUPT", "%s:%d" % (path, ln), "line is not JSON", "unobserved"))
            continue
        kind = rec.get("rec")
        eid = rec.get("event_id")
        if kind == "enqueue":
            n += 1
            enq[eid] = (ln, rec)
            if rec.get("agent_id") not in claims:
                findings.append(finding("UNKNOWN_AGENT", "%s:%d" % (path, ln), "enqueue by %r, not in claims.json"
                                        % rec.get("agent_id")))
        elif kind == "close":
            n += 1
            closed.setdefault(eid, []).append((ln, rec))
    for eid, (ln, e) in enq.items():
        if e.get("lane") != "script":
            continue
        agent = e.get("agent_id")
        claim = set(claims.get(agent, []))
        ws = e.get("write_set") or []
        outside = (not ws) or not set(ws) <= claim
        cl = closed.get(eid, [])
        if outside and not cl:
            findings.append(finding("CROSS_CLAIM_UNRESOLVED", "%s:%d" % (path, ln),
                                    "%s write_set %s is outside claim %s and has no close record"
                                    % (eid, sorted(ws), sorted(claim)), "unobserved"))
        for cln, c in cl:
            verdict = c.get("verdict")
            changed = c.get("changed_write_set")
            spill = c.get("spill_write_set") or []
            if outside and verdict not in REFUSALS:
                findings.append(finding("CROSS_CLAIM_ACCEPTED", "%s:%d" % (path, cln),
                                        "%s by %s wrote %s outside claim %s, verdict %s"
                                        % (eid, agent, sorted(ws), sorted(claim), verdict)))
            if verdict in REFUSALS and changed:
                findings.append(finding("REFUSED_BUT_EFFECT", "%s:%d" % (path, cln),
                                        "%s was %s yet the bridge measured changes in %s" % (eid, verdict, changed)))
            eff_out = sorted(set(changed or []) - claim) if isinstance(changed, list) else []
            if verdict in CLEAN and (spill or eff_out):
                findings.append(finding("SPILL_ACCEPTED", "%s:%d" % (path, cln),
                                        "%s closed %s but changed %s outside its claim/write_set"
                                        % (eid, verdict, sorted(set(spill) | set(eff_out)))))
    for eid, cl in closed.items():
        for cln, c in cl:
            if eid not in enq and c.get("verdict") not in REFUSALS and c.get("event_id"):
                findings.append(finding("ORPHAN_CLOSE", "%s:%d" % (path, cln),
                                        "%s closed %s without an enqueue record (job did not come through the queue)"
                                        % (eid, c.get("verdict"))))
    return findings, n


def run(claims_path=None, events_path=None):
    desired, observed = {}, {}
    if not claims_path or not os.path.isfile(claims_path):
        return result("G6_claim_enforcement", desired, observed,
                      [finding("CLAIMS_MISSING", str(claims_path), "no claims.json: desired state undeclared",
                               "unobserved")], 0)
    with open(claims_path) as f:
        claims = json.load(f)
    desired = {"claims": claims, "accepted_cross_claim_jobs": 0}
    if not events_path or not os.path.isfile(events_path):
        return result("G6_claim_enforcement", desired, observed,
                      [finding("LEDGER_MISSING", str(events_path), "no events.jsonl", "unobserved")], 0)
    events = load_events(events_path)
    findings, n = reconcile(claims, events, events_path)
    script = [r for _, r in events if r and r.get("rec") == "enqueue" and r.get("lane") == "script"]
    observed = {"records": len(events), "script_enqueues": len(script),
                "verdicts": sorted({r.get("verdict") for _, r in events if r and r.get("rec") == "close"} - {None})}
    return result("G6_claim_enforcement", desired, observed, findings, n)
