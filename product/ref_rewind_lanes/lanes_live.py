#!/usr/bin/env python3
"""R2 LIVE run: RewindBroker against a real Blender under a vision job with the F1 bridge loaded.

    VP_SWARM_NONCE=<nonce> lanes_live.py --job vprw2 --rt /dev/shm/vqa-cu/ref-rw2 --evidence evidence/ \
        [--ckpt-dir <product/ref_rewind_ckpt>] [--verify-dir <product/ref_rewind_verify>]

Needs a fresh RT. R1 (ops, Store) and R3 (judge) are loaded from their directories and used as they are.
Scenario: modeler commit -> props a1 spills (moves CHAR_body) -> restore -> a2 commit; a camera gui
intent queued before the restore closes STALE_FRAME; crash-free audit. Exit 0 only if every check holds.
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from rewind_broker import RewindBroker, RW  # noqa: E402

CLAIMS = {"modeler-agent": ["col:CHAR_mesh"], "props-agent": ["col:PROPS_set"],
          "look-agent": ["col:LOOK_lights"], "camera-agent": ["col:CAM_rig"]}
COL = '''
import bpy
def _col(n):
    c = bpy.data.collections.get(n) or bpy.data.collections.new(n)
    if c.name not in bpy.context.scene.collection.children: bpy.context.scene.collection.children.link(c)
    return c
def _box(name, col, size, loc):
    me = bpy.data.meshes.new(name); s = size / 2.0
    me.from_pydata([(-s,-s,-s),(s,-s,-s),(s,s,-s),(-s,s,-s),(-s,-s,s),(s,-s,s),(s,s,s),(-s,s,s)], [],
                   [(0,1,2,3),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)])
    ob = bpy.data.objects.new(name, me); ob.location = loc; _col(col).objects.link(ob); return ob
'''
MODELER = COL + '_box("CHAR_body", "CHAR_mesh", 2.0, (0.0, 0.0, 3.0)); print("made CHAR_body")\n'
PROPS_WRONG = COL + ('_box("PROPS_table", "PROPS_set", 20.0, (0.0, 0.0, 0.0))\n'
                     'bpy.data.objects["CHAR_body"].location.z = 9.0; print("table 10x, body moved")\n')
PROPS_GOOD = COL + '_box("PROPS_table", "PROPS_set", 2.0, (0.0, 0.0, 0.0)); print("made PROPS_table")\n'


def put(rt, agent, eid, frame, **kw):
    it = {"v": 1, "rec": "enqueue", "event_id": eid, "agent_id": agent, "based_on_frame": frame, "expect": "live"}
    it.update(kw)
    d = os.path.join(rt, "intents", agent)
    with open(os.path.join(d, eid + ".tmp"), "w") as f:
        json.dump(it, f)
    os.replace(os.path.join(d, eid + ".tmp"), os.path.join(d, eid + ".json"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--rt", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--ckpt-dir", default=os.path.join(PRODUCT, "ref_rewind_ckpt"))
    ap.add_argument("--verify-dir", default=os.path.join(PRODUCT, "ref_rewind_verify"))
    a = ap.parse_args()
    sys.path.insert(0, a.ckpt_dir)
    sys.path.insert(0, a.verify_dir)
    import ops                      # R1
    from store import Store         # R1
    import judge as J               # R3
    with open(os.path.join(a.rt, "claims.json"), "w") as f:
        json.dump(CLAIMS, f)
    b = RewindBroker(a.rt, a.job, os.environ.get("VP_SWARM_NONCE", ""), ops=ops, store=Store(a.rt), judge=J.judge,
                     job_cap_s=60.0, log=lambda m: print(m, file=sys.stderr))
    checks, steps = [], []

    def chk(name, ok, detail=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print("%s %s %s" % ("PASS" if ok else "FAIL", name, "" if detail is None else json.dumps(detail)[:300]))

    def closes(eid):
        return [r for r in RW.read_events(b.p_events) if r.get("rec") == "close" and r.get("event_id") == eid and not r.get("ingest")]

    # capsule setup before genesis: the claimed collections exist in p0 (linking a collection changes
    # "col:Scene Collection", which no agent may claim)
    setup = RW.dispatch_control(b, "ctl.setup.0", COL + "".join("_col(%r)\n" % c[0][4:] for c in CLAIMS.values()))
    chk("setup_collections", setup["ok"], setup.get("reason"))
    put(a.rt, "modeler-agent", "m1", b.frame, lane="script", write_set=["col:CHAR_mesh"], script=MODELER)
    steps.append(b.step())                                             # turn 1 modeler (genesis p0 first)
    chk("genesis_p0", "p0" in b.rw.points)
    chk("modeler_commit_p1", steps[-1] == "STATE_VERIFIED" and b.rw.last_good == "p1", steps[-1])
    p1_digest = dict(b.rw.points.get("p1", {}).get("digest") or {})
    put(a.rt, "camera-agent", "g1", b.frame, lane="gui", keys=["KP_1"], write_set=[])
    put(a.rt, "props-agent", "e1", b.frame, lane="script", write_set=["col:PROPS_set"], script=PROPS_WRONG,
        alternatives=[PROPS_GOOD])
    steps.append(b.step())                                             # turn 2 props: a1 rejected, a2 committed
    ev = RW.read_events(b.p_events)
    rew = [r for r in ev if r.get("rec") == "control" and r.get("op") == "rewind"]
    rb = [r for r in ev if r.get("rec") == "txn" and r.get("phase") == "rollback"]
    chk("a1_rolled_back", [r["attempt"] for r in rb] == ["e1.a1"], [(r["attempt"], r["verdict"], r["reason"]) for r in rb])
    chk("rewind_to_p1_digest_match", len(rew) == 1 and rew[0]["to"] == "p1" and rew[0]["digest_match"] is True, rew)
    chk("scene_epoch_2", b.scene_epoch == 2, b.scene_epoch)
    chk("a2_committed_p2", steps[-1] == "STATE_VERIFIED" and b.rw.last_good == "p2", steps[-1])
    p2 = dict(b.rw.points.get("p2", {}).get("digest") or {})
    delta = RW.digest_delta(p1_digest, p2)
    chk("only_accepted_work_in_p2", delta == ["col:PROPS_set"], delta)   # CHAR_body is where p1 had it
    steps.append(b.step())                                             # turn 3 look: PASS
    steps.append(b.step())                                             # turn 4 camera: stale by epoch
    g = closes("g1")
    chk("gui_stale_after_restore", [r["verdict"] for r in g] == ["STALE_FRAME"], [(r["verdict"], r.get("reason")) for r in g])
    put(a.rt, "camera-agent", "g1", b.frame, lane="gui", keys=["KP_1"], write_set=[])
    for _ in range(4):
        steps.append(b.step())
    g = closes("g1")
    chk("gui_resubmit_closed_on_new_epoch", len(g) == 2 and g[-1]["verdict"] in ("VERIFIED", RW.ALL_REJECTED),
        [(r["verdict"], r.get("reason")) for r in g])
    st = RW.replay(RW.read_events(b.p_events))
    chk("audit_no_violations", st.violations == [], st.violations)
    chk("mode_idle", b.mode == "IDLE", b.mode)
    ex = b.export_point(b.rw.confirmed_tick)
    chk("export_inside_frontier", ex["ok"], ex)
    chk("export_beyond_frontier_refused", b.export_point(b.rw.confirmed_tick + 1)["ok"] is False)
    snap = b.snapshot()
    b.close()
    b2 = RewindBroker(a.rt, a.job, os.environ.get("VP_SWARM_NONCE", ""), ops=ops, store=Store(a.rt), judge=J.judge)
    chk("replay_same_state", b2.snapshot() == snap)
    b2.close()
    os.makedirs(a.evidence, exist_ok=True)
    for n in ("events.jsonl", "cursor.jsonl"):
        shutil.copyfile(os.path.join(a.rt, n), os.path.join(a.evidence, n))
    for r in RW.read_events(b.p_events):
        if r.get("rec") == "ckpt":
            src = os.path.join(a.rt, "frames", "frame_%d.png" % r["frame"])
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(a.evidence, "frame_%s.png" % r["point_id"]))
    out = {"ok": all(c["ok"] for c in checks), "steps": steps, "summary": st.summary(), "checks": checks,
           "gui_resubmit_verdict": g[-1]["verdict"] if g else None}
    with open(os.path.join(a.evidence, "live_lanes.json"), "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    print(json.dumps({"ok": out["ok"], "steps": steps, "summary": st.summary()}))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
