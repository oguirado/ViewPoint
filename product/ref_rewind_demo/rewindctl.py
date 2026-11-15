#!/usr/bin/env python3
"""rewindctl: frozen CLI of the rewindable Blender capsule (unit R4, contract rw1).

Track P (declared Claude work, claude-fable-5-2). Stdlib only. Integrates, without editing them:
F1 product/ref_broker, R0 product/ref_rewind (schema), R1 product/ref_rewind_ckpt (ops, Store),
R2 product/ref_rewind_lanes (RewindBroker), R3 product/ref_rewind_verify (judge).

    VP_SWARM_NONCE=<nonce> rewindctl.py run    --job J --rt RT --turns N   # rc 0 idle, 3 FROZEN/RECONCILE
    VP_SWARM_NONCE=<nonce> rewindctl.py mark   --job J --rt RT             # director checkpoint of the current state
    VP_SWARM_NONCE=<nonce> rewindctl.py rewind --job J --rt RT --to last_good [--resume]
    VP_SWARM_NONCE=<nonce> rewindctl.py export --job J --rt RT --tick T    # checkpoint file inside the commit frontier
    rewindctl.py status --rt RT                 # fold of events.jsonl, no Blender contact
    rewindctl.py audit  --rt RT | --events F    # rc 0 clean, 1 violations, 2 unreadable
    rewindctl.py emit-script mark|restore|digest [--point-path P] [--sha256 H] [--no-load-ui]

`status`, `audit` and `emit-script` never touch Blender, vision or the nonce.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCT = os.path.dirname(HERE)
for _d in ("ref_rewind_verify", "ref_rewind_ckpt", "ref_rewind_lanes", "ref_rewind", "ref_broker"):
    _p = os.path.join(PRODUCT, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)
import schema as RW  # noqa: E402  (R0)

AUDIT_CHECKS = ("replay", "closed", "rollback_restored", "commit_judged", "commit_chain", "commit_delta")
# The demo judge: R3 oracles with outside-FOOTPRINT regions reported, not deciding, on every lane.
# Reason (measured, R2 live run): F1's gui dispatch clicks the viewport before the key, the click
# changes the selection, and the outliner/properties repaint lands in the base->after frame diff.
# The spill of the key itself is still decided by F1 (VERIFIED_WITH_SPILL is a rollback verdict).
JUDGE_STRICT_OUTSIDE = False


def audit(records, skip=()):
    """Audit of one events.jsonl (list of records). Pure: no Blender, no files.
    Returns {"ok", "problems": [[check, seq, msg]], "summary", "commits", "rollbacks"}.
    `skip` names checks to leave out; it exists for the negative controls only."""
    problems = []

    def bad(check, seq, msg):
        if check not in skip:
            problems.append([check, seq, msg])

    st = RW.replay(records)
    # replay: the shared R0 fold reports no violation (epochs, attempt reuse, tick order, ...)
    for seq, code, msg in st.violations:
        bad("replay", seq, "%s: %s" % (code, msg))
    # closed: nothing is left half-done
    if st.open_txn is not None:
        bad("closed", st.open_txn.get("seq"), "txn %s is still open" % st.open_txn["attempt"])
    if st.frozen:
        bad("closed", None, "capsule is frozen (failed restore or control stop)")
    enq, judged, rew_ok, commits, rollbacks = {}, {}, set(), [], []
    genesis = next(iter(st.points), None)
    head = genesis
    for r in records:
        rec = r.get("rec")
        if rec == "enqueue":
            enq[r.get("event_id")] = r
        elif rec == "judge":
            judged[r.get("attempt")] = r.get("pick")
        elif rec == "control" and r.get("op") == "rewind":
            if r.get("digest_match") is True:
                rew_ok.add((r.get("attempt"), r.get("to")))
        elif rec == "txn" and r.get("phase") == "rollback":
            rollbacks.append(r["attempt"])
            # rollback_restored: every rejected attempt has its own rewind to its base with digest_match=true
            if (r["attempt"], r.get("base_point")) not in rew_ok:
                bad("rollback_restored", r.get("seq"), "rollback %s without a rewind to %s with digest_match=true"
                    % (r["attempt"], r.get("base_point")))
        elif rec == "txn" and r.get("phase") == "commit":
            a, base, point = r["attempt"], r.get("base_point"), r.get("point")
            commits.append([a, base, point, r.get("tick")])
            # commit_judged: only an attempt the judge picked is committed
            if judged.get(a) is not True:
                bad("commit_judged", r.get("seq"), "commit %s without judge pick=true" % a)
            # commit_chain: accepted states form one chain from the genesis point
            if base != head:
                bad("commit_chain", r.get("seq"), "commit %s is based on %s, the accepted head is %s" % (a, base, head))
            head = point
            # commit_delta: what a commit added is inside what its intent declared (write_set + touches)
            it = enq.get(r.get("event_id")) or {}
            if base in st.points and point in st.points:
                delta = RW.digest_delta(st.points[base]["digest"], st.points[point]["digest"])
                out = RW.delta_outside(delta, it)
                if out or not delta:
                    bad("commit_delta", r.get("seq"), "commit %s: delta %s, undeclared %s (write_set %s)"
                        % (a, json.dumps(delta), json.dumps(out), json.dumps(it.get("write_set"))))
            else:
                bad("commit_delta", r.get("seq"), "commit %s cites a point without a ckpt record" % a)
    if head != st.last_good:
        bad("commit_chain", None, "last_good %s is not the head of the accepted chain %s" % (st.last_good, head))
    return {"ok": not problems, "problems": problems, "summary": st.summary(), "commits": commits,
            "rollbacks": rollbacks, "skipped": sorted(skip),
            "final_digest_id": RW.digest_id(st.points[st.last_good]["digest"]) if st.last_good in st.points else None}


def status(records):
    st = RW.replay(records)
    mode = "IDLE"
    for r in records:
        if r.get("rec") == "control" and r.get("op") in ("reconcile", "stop", "resume"):
            mode = {"reconcile": "RECONCILE", "stop": "FROZEN", "resume": "IDLE"}[r["op"]]
    s = st.summary()
    s.update({"mode": mode, "records": len(records),
              "last_rewind": ({k: st.rewinds[-1].get(k) for k in ("to", "by", "digest_match", "scene_epoch_after", "attempt")}
                              if st.rewinds else None)})
    return s


def make_broker(rt, job, ops=None, judge=None, **kw):
    """RewindBroker wired with R1 ops/Store and the R3 judge. `ops` / `judge` overrides are for the
    must-FAIL runs of rewind_live.py only."""
    import ops as R1ops                       # R1
    from store import Store                   # R1
    import judge as J                         # R3
    from rewind_broker import RewindBroker    # R2
    kw.setdefault("log", lambda m: print(m, file=sys.stderr))
    return RewindBroker(rt, job, os.environ.get("VP_SWARM_NONCE", ""), ops=ops or R1ops, store=Store(rt),
                        judge=judge or J.make_judge(strict_outside=JUDGE_STRICT_OUTSIDE), **kw)


def emit_script(kind, point_path=None, sha256=None, load_ui=True):
    """bpy source of a mark / restore / digest control job (R1 ops, unchanged). The script prints
    `RW_RESULT <json>` as its last stdout line; any broker that owns a bridge can dispatch it."""
    import ops as R1ops
    if kind == "digest":
        return R1ops.digest_script()
    if not point_path or not os.path.isabs(point_path):
        raise SystemExit("emit-script %s needs an absolute --point-path" % kind)
    if kind == "mark":
        return R1ops.mark_script(point_path)
    return R1ops.restore_script(point_path, load_ui, sha256=sha256)


def _events(a):
    path = a.events or os.path.join(a.rt, "events.jsonl")
    if not os.path.exists(path):
        print(json.dumps({"ok": False, "code": "NO_EVENTS", "path": path}))
        sys.exit(2)
    return RW.read_events(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rt_default = os.environ.get("VP_SWARM_RT", "/dev/shm/vqa-cu/ref-rw")
    for name in ("run", "mark", "rewind", "export"):
        p = sub.add_parser(name)
        p.add_argument("--job", required=True)
        p.add_argument("--rt", default=rt_default)
        if name == "run":
            p.add_argument("--turns", type=int, required=True)
        elif name == "rewind":
            p.add_argument("--to", required=True, help="a point id or the literal last_good")
            p.add_argument("--reason", default="")
            p.add_argument("--resume", action="store_true", help="control resume after a matching restore")
        elif name == "export":
            p.add_argument("--tick", type=int, required=True)
    for name in ("status", "audit"):
        p = sub.add_parser(name)
        p.add_argument("--rt", default=rt_default)
        p.add_argument("--events", default=None, help="an events.jsonl file instead of RT/events.jsonl")
    e = sub.add_parser("emit-script")
    e.add_argument("kind", choices=("mark", "restore", "digest"))
    e.add_argument("--point-path")
    e.add_argument("--sha256", default=None)
    e.add_argument("--no-load-ui", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "emit-script":
        sys.stdout.write(emit_script(a.kind, a.point_path, a.sha256, not a.no_load_ui))
        return 0
    if a.cmd == "status":
        print(json.dumps(status(_events(a)), sort_keys=True))
        return 0
    if a.cmd == "audit":
        out = audit(_events(a))
        print(json.dumps(out, sort_keys=True))
        return 0 if out["ok"] else 1

    import broker as B
    try:
        b = make_broker(a.rt, a.job)
    except B.BrokerError as ex:
        print(json.dumps({"ok": False, "code": "BROKER", "reason": str(ex)}))
        return 2
    try:
        if a.cmd == "run":
            steps = []
            for _ in range(a.turns):
                steps.append(b.step())
                if b.mode != "IDLE":
                    break
            print(json.dumps({"ok": b.mode == "IDLE", "mode": b.mode, "steps": steps, "summary": b.rw.summary()}, sort_keys=True))
            return 0 if b.mode == "IDLE" else 3
        if a.cmd == "mark":
            if b.mode != "IDLE" or b.rw.open_txn is not None:
                print(json.dumps({"ok": False, "code": "NOT_IDLE", "mode": b.mode}))
                return 3
            if b.rw.last_good is None:
                pre = b._pre_dispatch()      # genesis point
                rec, code = (b.rw.points.get(b.rw.last_good), None) if pre is None else (None, pre)
            else:
                rec, code = b._mark(b.tick)  # an extra point of the confirmed state; last_good is unchanged
            print(json.dumps({"ok": rec is not None, "code": code or "OK",
                              "point": rec and {k: rec[k] for k in ("point_id", "tick", "scene_epoch", "blend_sha256", "bytes")}},
                             sort_keys=True))
            return 0 if rec is not None else 1
        if a.cmd == "rewind":
            to = b.rw.last_good if a.to == "last_good" else a.to
            try:
                out = b.rewind(to, by="director", reason=a.reason)
                if out["ok"] and a.resume and b.mode != "IDLE":
                    b.control("resume", "director", "rewindctl rewind --resume: %s" % a.reason)
                    out["mode"] = b.mode
            except B.BrokerError as ex:
                out = {"ok": False, "code": str(ex).split(":")[0], "reason": str(ex), "to": to}
            print(json.dumps(out, sort_keys=True))
            return 0 if out["ok"] else 1
        if a.cmd == "export":
            out = b.export_point(a.tick)
            print(json.dumps(out, sort_keys=True))
            return 0 if out["ok"] else 1
    finally:
        b.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
