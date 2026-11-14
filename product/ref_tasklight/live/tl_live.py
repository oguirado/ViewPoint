#!/usr/bin/env python3
"""tl-live: LIVE proof of tasks/microtasks (tm1) on the reference stack, one private Blender (vision job vptl*).

Track P (claude-fable-5-2). Imports, read-only, the integrated stack of p-fable-demo (--stack): F1 broker + bridge,
auction scheduler (in-memory seam), rewind broker + R1 ops + R3 judge. 3 stub agents (modeler, props, camera; the
4th ring seat, look-agent, has no stub and passes) plan a task with microtasks, enqueue ONE microtask at a time as
contract intents carrying `tm`, and update / re-plan only from the broker's events.jsonl. props m1 is deliberately
wrong: the judge rejects it, the capsule rewinds (digest match), the microtask shows rolled_back and is re-planned
as m2 (supersedes m1), which is verified. Every transition is projected to OpenSwitchboard and to TaskLight.

    tl_live.py --rt /dev/shm/vqa-cu/ref-tl-live-<tag> --evidence evidence/<tag>      (exit 0 = all checks hold)
"""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PRODUCT, "ref_projection"))
import microtasks as M      # noqa: E402
import tl_project as TP     # noqa: E402
import project as P         # noqa: E402  (ref_projection sinks: Ledger, OsbSink, McpStdioClient, TaskLightSink)

RT_PREFIX, JOB_PREFIX = "/dev/shm/vqa-cu/ref-tl-live", "vptl"
STACK = os.environ.get("VP_REF_DIR") or os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
CW_REPO = P.CW_REPO
WALL = {"modeler-agent": "tl-modeler-agent", "props-agent": "tl-props-agent", "camera-agent": "tl-camera-agent"}


def build_board(L):
    head = L.COL + '_box("CHAR_head", "CHAR_mesh", 1.0, (0.0, 0.0, 5.0)); print("made CHAR_head")\n'
    mo = M.Task("modeler-agent", 1, "Model the character")
    m1 = mo.add("Create CHAR_body (2 m box at z=3)", "CHAR_body exists in CHAR_mesh",
                {"lane": "script", "write_set": ["col:CHAR_mesh"], "script": L.MODELER})
    mo.add("Add CHAR_head on top of the body", "CHAR_head exists in CHAR_mesh",
           {"lane": "script", "write_set": ["col:CHAR_mesh"], "script": head}, after=[m1.microtask_id])
    pr = M.Task("props-agent", 1, "Put a table under the character")
    probe = {"probe": L.PROBE_UNDER, "equals": True}
    p1 = pr.add("Create PROPS_table under CHAR_body", "table top at or below the body bottom; only PROPS_set changes",
                {"lane": "script", "write_set": ["col:PROPS_set"], "script": L.PROPS_WRONG, "expect": probe},
                after=[m1.microtask_id],
                plans=[{"text": "Create PROPS_table, 2 m, at the origin", "accept": "table top at or below the body bottom",
                        "intent": {"lane": "script", "write_set": ["col:PROPS_set"], "script": L.PROPS_GOOD, "expect": probe}}])
    ca = M.Task("camera-agent", 1, "Frame the scene from the front")
    ca.add("Front view (numpad 1)", "viewport shows the front view",
           {"lane": "gui", "keys": ["KP_1"], "write_set": []}, after=[p1.microtask_id, mo.microtasks[1].microtask_id])
    return M.Board([mo, pr, ca])


def room_cli(*args):
    p = subprocess.run(["python3", "-m", "bridge.room"] + list(args), capture_output=True, text=True, timeout=60,
                       cwd=CW_REPO, env=dict(os.environ, PYTHONPATH=CW_REPO))
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"rc": p.returncode, "raw": p.stdout[-400:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", default=JOB_PREFIX)
    ap.add_argument("--rt", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--stack", default=STACK)
    ap.add_argument("--no-auction", action="store_true")
    ap.add_argument("--osb-room", default="viewpoint-capsule-test")
    ap.add_argument("--tasklight-room", default="demo")
    a = ap.parse_args()
    a.rt, a.evidence, a.mutant = os.path.abspath(a.rt), os.path.abspath(a.evidence), None
    if not a.rt.startswith(RT_PREFIX) or not a.job.startswith(JOB_PREFIX) or a.tasklight_room == "viewpoint":
        print("refused: RT under %s*, job %s*, TaskLight room never 'viewpoint'" % (RT_PREFIX, JOB_PREFIX))
        return 2
    if os.path.exists(os.path.join(a.rt, "events.jsonl")):
        print("refused: %s is not fresh" % a.rt)
        return 2
    sys.path.insert(0, os.path.join(a.stack, "ref_demo"))
    import demo_live as D                      # read-only: seam + auction wiring helpers
    AD = D.install_seamed_broker()
    sys.path.insert(0, os.path.join(a.stack, "ref_rewind_demo"))
    import rewind_live as L                    # read-only: launch/stop, capsule setup, scripts, probes
    CTL, RW, B, R1OPS = L.CTL, L.RW, L.B, L.R1OPS
    stages = {"scheduler": "round_robin" if a.no_auction else D.wire_auction(AD), "stack": a.stack}
    os.makedirs(a.rt, exist_ok=True)
    os.makedirs(a.evidence, exist_ok=True)
    with open(os.path.join(a.rt, "claims.json"), "w") as f:
        json.dump(L.CLAIMS, f)
    os.environ["VP_SWARM_NONCE"] = secrets.token_hex(8)
    board = build_board(L)
    ledger = P.Ledger(os.path.join(a.rt, "tl_ledger.jsonl"))
    client = P.McpStdioClient()
    proj = TP.MicrotaskProjector(P.OsbSink(a.osb_room, ledger), P.TaskLightSink(a.tasklight_room, ledger, client),
                                 wall_agent=lambda x: WALL.get(x, "tl-" + x))
    before = room_cli("status", "--room", a.tasklight_room)
    lam0 = max([v.get("lamport", 0) for v in (before.get("last_by_agent") or {}).values()] or [0])
    checks = []

    def chk(name, ok, detail=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print("%s %s %s" % ("PASS" if ok else "FAIL", name, "" if detail is None else json.dumps(detail)[:300]), flush=True)

    rc, tail = L.launch(a.job, a.rt, os.environ["VP_SWARM_NONCE"])
    print("vision start rc=%d %s" % (rc, tail), flush=True)
    if rc != 0:
        L.stop(a.job, a.rt)
        return 2
    final, snap_png = None, None
    try:
        stages["display"] = D.display_of(a.rt)
        b = CTL.make_broker(a.rt, a.job, job_cap_s=60.0)
        setup, n = {"ok": False}, 0
        while not setup["ok"] and n < 40:
            setup = RW.dispatch_control(b, "ctl.setup.%d" % n, L.SETUP)
            n += 1
        chk("setup_bridge_answers", setup["ok"], setup.get("reason"))
        b.vision.act(["--xy", B.HOVER_XY])
        # -- PLAN phase: every agent writes its task (tm1 task file) and posts the plan BEFORE any intent
        for t in board.tasks:
            d = os.path.join(a.rt, "tasks", t.agent_id)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, t.task_id + ".json"), "w") as f:
                json.dump({"v": 1, "rec": "task", "op": "open", "task_id": t.task_id, "agent_id": t.agent_id, "title": t.title,
                           "plan": [{"microtask_id": m.microtask_id, "text": m.text, "accept": m.accept, "after": m.after}
                                    for m in t.microtasks]}, f)
            proj.plan(t)
        seen, turns = 0, 0
        while turns < 40 and b.mode == "IDLE" and not all(t.closed for t in board.tasks):
            for m, it in board.next_intents(b.frame):          # INTENT: one microtask per agent at a time
                M.write_intent(a.rt, it)
            b.step()
            turns += 1
            recs = RW.read_events(b.p_events)
            for r in recs[seen:]:                              # the log is the only source of state changes
                for tr in board.feed(r):
                    proj.apply(tr, board)
            seen = len(recs)
        stages["turns"] = turns
        recs = RW.read_events(b.p_events)
        au = CTL.audit(recs)
        chk("rewind_audit_clean", au["ok"], au["problems"][:3])
        fp = RW.dispatch_control(b, RW.control_job_id("probe", "final"), L.PROBE_FINAL)
        final = (fp["result"] or {}).get("value") if fp["ok"] else None
        chk("final_probe_only_accepted_work", final == L.FINAL_WANT, final)
        chk("mode_idle", b.mode == "IDLE", b.mode)
        snap_png = b.frame_png
        b.close()
    except Exception as e:  # noqa: BLE001
        chk("scenario_completed", False, "%s: %s" % (type(e).__name__, e))
    finally:
        print("vision stop rc=%d %s" % L.stop(a.job, a.rt), flush=True)

    # ---- checks on the model, the log and the projections
    recs = RW.read_events(os.path.join(a.rt, "events.jsonl")) if os.path.exists(os.path.join(a.rt, "events.jsonl")) else []
    by = {m.microtask_id: m for m in board.all()}
    bad, good = by.get("t.props-agent.1.m1"), by.get("t.props-agent.1.m2")
    chk("all_tasks_done", all(t.done() and t.closed for t in board.tasks), {t.task_id: t.done() for t in board.tasks})
    chk("no_model_violations", not board.violations, board.violations)
    chk("wrong_microtask_rolled_back", bool(bad) and bad.history == ["pending", "queued", "acting", "rolled_back"] and bad.proven,
        bad and [bad.history, bad.reason])
    chk("replanned_microtask_verified", bool(good) and good.supersedes == "t.props-agent.1.m1"
        and good.history == ["pending", "queued", "acting", "verified"], good and good.to_json())
    enq = [r for r in recs if r.get("rec") == "enqueue"]
    chk("every_enqueue_carries_tm", len(enq) == len(by) and all((r.get("tm") or {}).get("microtask_id") == r["event_id"] for r in enq),
        [r.get("event_id") for r in enq])
    rew = [r for r in recs if r.get("rec") == "control" and r.get("op") == "rewind"]
    chk("log_has_judge_reject_rewind_commit",
        any(r.get("rec") == "judge" and r.get("pick") is False and r.get("attempt") == "t.props-agent.1.m1.a1" for r in recs)
        and any(r.get("digest_match") is True and r.get("attempt") == "t.props-agent.1.m1.a1" for r in rew)
        and any(r.get("rec") == "txn" and r.get("phase") == "commit" and r.get("event_id") == "t.props-agent.1.m2" for r in recs),
        [(r.get("to"), r.get("digest_match")) for r in rew])
    order = [r["event_id"] for r in recs if r.get("rec") == "txn" and r.get("phase") == "begin"]
    chk("handoff_order_respected", order and order[0] == "t.modeler-agent.1.m1" and order[-1] == "t.camera-agent.1.m1", order)
    rows = ledger.rows
    tl = [r for r in rows if r.get("sink") == "tasklight"]
    osb = [r for r in rows if r.get("sink") == "osb"]
    chk("tasklight_all_calls_acknowledged", tl and all(r["ok"] for r in tl) and not proj.failed,
        {"calls": len(tl), "failed": proj.failed[:5]})
    chk("osb_all_posts_rc0", osb and all(r.get("rc") == 0 for r in osb), {"posts": len(osb)})
    closed = sorted(r["agent"] for r in tl if r["tool"] == "close_task" and r["ok"])
    chk("close_task_for_3_agents", closed == sorted(WALL.values()), closed)
    plans = [r for r in tl if r["tool"] == "post_plan" and r["agent"] == WALL["props-agent"]]
    chk("replan_appends_step", len(plans) == 2 and len(plans[1]["arguments"]["steps"]) == 2
        and plans[1]["arguments"]["steps"][0] == plans[0]["arguments"]["steps"][0]
        and "retry of m1" in plans[1]["arguments"]["steps"][1]["text"], [p["arguments"]["steps"] for p in plans])
    blocked = [r for r in tl if r["tool"] == "post_step" and r["agent"] == WALL["props-agent"]
               and r["arguments"]["index"] == 0 and r["arguments"]["state"] == "blocked"]
    chk("wall_step_blocked_with_rollback_outcome", blocked and blocked[-1]["arguments"].get("outcome", "").startswith("rolled back to p"),
        blocked and blocked[-1]["arguments"])
    # ---- read back: TaskLight store (CLI read, MCP read_inbox) and the OpenSwitchboard room
    wall = room_cli("read", "--room", a.tasklight_room, "--since", str(lam0))
    mine = [e for e in wall.get("events", []) if e.get("agent") in WALL.values()]
    inbox = {}
    try:
        if not client.alive():
            client.start()
        for ag in WALL.values():
            ok, res = client.call("read_inbox", {"room": a.tasklight_room, "agent": ag})
            inbox[ag] = {"ok": ok, "response": res}
    except P.McpError as e:
        inbox["error"] = str(e)
    client.stop()
    acts = {}
    for e in mine:
        acts[e.get("act")] = acts.get(e.get("act"), 0) + 1
    chk("wall_store_read_back", len(mine) >= len(tl) and acts.get("close", 0) == 3, acts)
    hand = json.dumps(inbox.get(WALL["props-agent"], {}))
    chk("handoff_in_downstream_inbox", "tm1 handoff t.modeler-agent.1.m1 -" in hand, hand[:200])
    p = subprocess.run(["python3", "-I", P.OSB, "read", "--room", a.osb_room, "--json"], capture_output=True, text=True, timeout=60)
    refs = {m.microtask_id for m in board.all()}
    room = [json.loads(x) for x in p.stdout.splitlines() if x.strip().startswith("{")]
    refs |= {t.task_id for t in board.tasks}
    room = [e for e in room if str(e.get("evidence", ""))[4:] in refs][-len(osb):]
    chk("osb_room_read_back", len(room) == len(osb), {"room": len(room), "posted": len(osb)})

    # ---- evidence
    for n in ("events.jsonl", "cursor.jsonl", "claims.json"):
        if os.path.exists(os.path.join(a.rt, n)):
            shutil.copyfile(os.path.join(a.rt, n), os.path.join(a.evidence, n))
    if os.path.isdir(os.path.join(a.rt, "tasks")):
        shutil.copytree(os.path.join(a.rt, "tasks"), os.path.join(a.evidence, "tasks"), dirs_exist_ok=True)
    with open(os.path.join(a.evidence, "tasklight_mcp_responses.jsonl"), "w") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in tl)
    with open(os.path.join(a.evidence, "osb_posts.jsonl"), "w") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in osb)
    with open(os.path.join(a.evidence, "osb_room_readback.jsonl"), "w") as f:
        f.writelines(json.dumps(r, sort_keys=True) + "\n" for r in room)
    with open(os.path.join(a.evidence, "tasklight_state_dump.json"), "w") as f:
        json.dump({"room": a.tasklight_room, "status_before": before, "status_after": room_cli("status", "--room", a.tasklight_room),
                   "events_of_this_run": mine, "mcp_read_inbox": inbox}, f, indent=1, sort_keys=True)
    with open(os.path.join(a.evidence, "board.json"), "w") as f:
        json.dump(board.to_json(), f, indent=1, sort_keys=True)
    for label, by_ev in (("frame_m1_wrong.png", "t.props-agent.1.m1.a1"), ("frame_final.png", None)):
        src = L.frame_of(a.rt, by_ev) if by_ev else snap_png
        if src and os.path.exists(src):
            shutil.copyfile(src, os.path.join(a.evidence, label))
    red = [c["check"] for c in checks if not c["ok"]]
    kinds = {}
    for r in recs:
        kinds[r.get("rec")] = kinds.get(r.get("rec"), 0) + 1
    out = {"ok": not red, "red": red, "checks": checks, "stages": stages, "job": a.job, "rt": a.rt, "final_probe": final,
           "events_by_rec": kinds, "tasklight_calls": len(tl), "osb_posts": len(osb), "wall_agents": WALL}
    with open(os.path.join(a.evidence, "live.json"), "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    print(json.dumps({"ok": out["ok"], "red": red, "checks": len(checks), "events_by_rec": kinds,
                      "tasklight_calls": len(tl), "osb_posts": len(osb)}))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
