#!/usr/bin/env python3
"""R4 LIVE PROOF of the rewindable Blender capsule (rw-build.md section 4), on one vision job.

    rewind_live.py --job vprw4 --rt /dev/shm/vqa-cu/ref-rw4 --evidence evidence/nominal --launch
    rewind_live.py --job vprw4 --rt /dev/shm/vqa-cu/ref-rw4-m1 --evidence evidence/mutant_no_restore \
        --launch --mutant no-restore
    rewind_live.py --job vprw4 --rt /dev/shm/vqa-cu/ref-rw4-m2 --evidence evidence/mutant_judge_accept_all \
        --launch --mutant judge-accept-all

Track P (claude-fable-5-2). `--launch` starts Blender 4.0.2 with the F1 bridge under the vision job
(fresh RT, fresh nonce) and stops that job at the end; without it the job must already run and
VP_SWARM_NONCE must be set. RT must be fresh and live under /dev/shm/vqa-cu/ref-rw*.

Nominal run: exit 0 only if every check holds.
Mutant run (must-FAIL): the same scenario with one guard broken. Exit 0 only if the designated check
went RED (the guard is alive); the red check list is saved next to the nominal evidence.
"""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rewindctl as CTL  # noqa: E402  (also puts F1, R0-R3 on sys.path)
import schema as RW      # noqa: E402
import broker as B       # noqa: E402
import judge as J        # noqa: E402
import ops as R1OPS      # noqa: E402

VISION = list(B.VISION)
SCOPE = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0"]
RT_PREFIX = "/dev/shm/vqa-cu/ref-rw"
MUTANTS = {"no-restore": "audit_clean", "judge-accept-all": "final_probe_only_accepted_work"}
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
# capsule setup before genesis (R2 note): claimed collections exist in p0; the factory Cube is removed
SETUP = COL + "".join("_col(%r)\n" % c[0][4:] for c in CLAIMS.values()) + '''
_c = bpy.data.objects.get("Cube")
if _c is not None: bpy.data.objects.remove(_c, do_unlink=True)
print("setup done")
'''
MODELER = COL + '_box("CHAR_body", "CHAR_mesh", 2.0, (0.0, 0.0, 3.0)); print("made CHAR_body")\n'
# a1, deliberately wrong: table 10x too large AND it moves CHAR_body (spill outside the write_set)
PROPS_WRONG = COL + ('_box("PROPS_table", "PROPS_set", 20.0, (0.0, 0.0, 0.0))\n'
                     'bpy.data.objects["CHAR_body"].location.z = 9.0; print("table 10x, body moved")\n')
PROPS_GOOD = COL + '_box("PROPS_table", "PROPS_set", 2.0, (0.0, 0.0, 0.0)); print("made PROPS_table")\n'
_Z = '''
import bpy, json
def _z(n):
    o = bpy.data.objects.get(n)
    if o is None: return None
    zs = [o.location.z + v.co.z * o.scale.z for v in o.data.vertices]
    return [round(min(zs), 4), round(max(zs), 4)]
_t, _b = _z("PROPS_table"), _z("CHAR_body")
'''
# read-only expect probe of the props intent: the table top is at or below the body bottom
PROBE_UNDER = _Z + 'print("RW_RESULT " + json.dumps({"value": bool(_t and _b and _t[1] <= _b[0] + 1e-6)}))\n'
# final probe (director): only accepted work is in the scene
PROBE_FINAL = _Z + ('print("RW_RESULT " + json.dumps({"value": {"table_z": _t, "body_z": _b, '
                    '"under": bool(_t and _b and _t[1] <= _b[0] + 1e-6)}}))\n')
FINAL_WANT = {"table_z": [-1.0, 1.0], "body_z": [2.0, 4.0], "under": True}


WITNESS_THRESH = 8   # gray levels per 8x8 cell [TARGET]. Measured on vprw4: restored-vs-p1 max 3 in one cell
#                      (viewport anti-aliasing still converging at capture), wrong-vs-p1 113 over 21866 cells.


def witness_differ(png_a, png_b):
    return B.diff_regions(png_a, png_b, thresh=WITNESS_THRESH)


class NoRestoreOps:
    """MUTANT no-restore: the restore job answers honestly but restores nothing."""
    mark_script = staticmethod(R1OPS.mark_script)
    digest_script = staticmethod(R1OPS.digest_script)

    @staticmethod
    def restore_script(point_path, load_ui=True, sha256=None):
        return R1OPS.digest_script()


def accept_all_judge(attempt, base, after, run_probe=None):
    """MUTANT judge-accept-all: a dead oracle (hbkernel F7)."""
    return RW.judge_rec(attempt["attempt"], True, [RW.oracle("delta", True, "MUTANT accept-all"),
                                                   RW.oracle("cost", True, "job_ms=0")])


def put(rt, agent, eid, frame, **kw):
    it = {"v": 1, "rec": "enqueue", "event_id": eid, "agent_id": agent, "based_on_frame": frame, "expect": "live"}
    it.update(kw)
    d = os.path.join(rt, "intents", agent)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, eid + ".tmp"), "w") as f:
        json.dump(it, f)
    os.replace(os.path.join(d, eid + ".tmp"), os.path.join(d, eid + ".json"))


def launch(job, rt, nonce):
    app = ("env VP_SWARM_RT=%s VP_SWARM_NONCE=%s XDG_CONFIG_HOME=%s/xdg blender --factory-startup --python %s"
           % (rt, nonce, rt, os.path.join(CTL.PRODUCT, "ref_broker", "bridge.py")))
    p = subprocess.run(VISION + ["start", "--job", job, "--size", "1920x1080", "--mem", "8G", "--wait-title", "Blender",
                                 "--app", app], capture_output=True, text=True, timeout=180, cwd=rt)
    return p.returncode, p.stdout.strip()[-400:]


def stop(job, rt):
    p = subprocess.run(VISION + ["stop", "--job", job], capture_output=True, text=True, timeout=120, cwd=rt)
    return p.returncode, p.stdout.strip()[-300:]


def frame_of(rt, by_event):
    """PNG of the newest frame record captured for `by_event`, or None."""
    d, best = os.path.join(rt, "frames"), None
    for n in os.listdir(d):
        if n.endswith(".json"):
            with open(os.path.join(d, n)) as f:
                r = json.load(f)
            if r.get("by_event") == by_event and (best is None or r["frame"] > best["frame"]):
                best = r
    return best["png"] if best else None


def make_video(rt, out_mp4):
    """All frames of the run, 1 fps, in frame order. Returns (ok, detail)."""
    d = os.path.join(rt, "frames")
    nums = sorted(int(n[6:-4]) for n in os.listdir(d) if n.startswith("frame_") and n.endswith(".png"))
    lst = os.path.join(rt, "video.txt")
    with open(lst, "w") as f:
        for n in nums:
            f.write("file '%s'\nduration 1.0\n" % os.path.join(d, "frame_%d.png" % n))
        if nums:
            f.write("file '%s'\n" % os.path.join(d, "frame_%d.png" % nums[-1]))
    p = subprocess.run(SCOPE + ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-vf",
                                "scale=1280:720,format=yuv420p", "-r", "10", out_mp4], capture_output=True, text=True, timeout=300)
    return p.returncode == 0 and os.path.exists(out_mp4), (p.stderr or "")[-200:] or "%d frames" % len(nums)


def scenario(a, checks):
    """Steps 1-6 of rw-build.md section 4. Returns the result dict (without the verdict)."""
    rt = a.rt
    b = CTL.make_broker(rt, a.job, ops=NoRestoreOps if a.mutant == "no-restore" else None,
                        judge=accept_all_judge if a.mutant == "judge-accept-all" else None, job_cap_s=60.0)
    steps = []

    def chk(name, ok, detail=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print("%s %s %s" % ("PASS" if ok else "FAIL", name, "" if detail is None else json.dumps(detail)[:400]), flush=True)

    def events(rec=None, **m):
        return [r for r in RW.read_events(b.p_events) if (rec is None or r.get("rec") == rec)
                and all(r.get(k) == v for k, v in m.items())]

    def closes(eid):
        return [r for r in events("close", event_id=eid) if not r.get("ingest")]

    def step_until(eid, n_close, cap=8):
        while len(closes(eid)) < n_close and cap > 0 and b.mode == "IDLE":
            steps.append(b.step())
            cap -= 1

    # -- capsule setup: bridge up, splash away (R3 note), claimed collections before genesis (R2 note)
    setup, t0 = {"ok": False}, time.time()
    n = 0
    while not setup["ok"] and time.time() - t0 < 60:
        setup = RW.dispatch_control(b, "ctl.setup.%d" % n, SETUP)
        n += 1
    chk("setup_bridge_answers", setup["ok"], setup.get("reason"))
    rc, _ = b.vision.act(["--xy", B.HOVER_XY])
    chk("splash_dismissed", rc in (0, 3), rc)
    # -- 1. modeler creates CHAR_body in col:CHAR_mesh -> verified, judge accept, commit p1
    put(rt, "modeler-agent", "m1", b.frame, lane="script", write_set=["col:CHAR_mesh"], script=MODELER)
    step_until("m1", 1)
    chk("1_genesis_p0", "p0" in b.rw.points)
    c = closes("m1")
    chk("1_modeler_verified_commit_p1", [r["verdict"] for r in c] == ["STATE_VERIFIED"] and b.rw.last_good == "p1"
        and b.rw.judged.get("m1.a1") is True, [(r["verdict"], r.get("reason")) for r in c])
    p1 = b.rw.points.get("p1") or {}
    # -- 2. props: a1 wrong (10x table, moves CHAR_body), a2 right. camera queues a gui intent first.
    put(rt, "camera-agent", "g1", b.frame, lane="gui", keys=["KP_1"], write_set=[])
    put(rt, "props-agent", "e1", b.frame, lane="script", write_set=["col:PROPS_set"], script=PROPS_WRONG,
        alternatives=[PROPS_GOOD], expect={"probe": PROBE_UNDER, "equals": True})
    step_until("e1", 1)
    j1 = (events("judge", attempt="e1.a1") or [{}])[0]
    failed = {o["name"]: o["detail"] for o in j1.get("oracles", []) if not o["ok"]}
    chk("2_a1_rejected_by_probe_and_delta", j1.get("pick") is False and "expect" in failed and "delta" in failed
        and "DELTA_OUTSIDE" in failed.get("delta", ""), failed or j1)
    # -- 3. control rewind to p1: restore OK, digest v2 equal, frame equal in the viewport, epoch 1 -> 2
    rew = events("control", op="rewind")
    chk("3_rewind_to_p1_digest_match", len(rew) >= 1 and rew[0]["to"] == "p1" and rew[0]["digest_match"] is True
        and rew[0].get("attempt") == "e1.a1" and rew[0]["by"] == "broker", rew[:1])
    chk("3_scene_epoch_1_to_2", bool(rew) and rew[0]["scene_epoch_after"] == 2 and p1.get("scene_epoch") == 1,
        [p1.get("scene_epoch"), rew and rew[0]["scene_epoch_after"]])
    p1_png = os.path.join(rt, "frames", "frame_%s.png" % p1.get("frame"))
    restored_png = frame_of(rt, "ctl.restore.p1.e2")
    p1_png = p1_png if os.path.exists(p1_png) else None
    w = J.rollback_witness(p1_png, restored_png, differ=witness_differ)
    chk("3_viewport_frame_equals_p1", w["ok"], w["detail"])
    # control of that witness on real frames: the frame of the wrong attempt is NOT the p1 frame
    wn = J.rollback_witness(p1_png, frame_of(rt, "e1.a1"), differ=witness_differ)
    chk("3_witness_alive_wrong_frame_differs", wn["ok"] is False and "WITNESS_NOT_RESTORED" in wn["detail"], wn["detail"])
    rb = events("txn", phase="rollback")
    chk("3_a1_rolled_back", [r["attempt"] for r in rb][:1] == ["e1.a1"] and rb[0]["verdict"] == RW.JUDGE_REJECT,
        [(r["attempt"], r["verdict"]) for r in rb])
    # -- 4. a2 applied from the same base -> accept -> commit p2
    cm = events("txn", phase="commit", event_id="e1")
    chk("4_a2_committed_p2_from_p1", len(cm) == 1 and cm[0]["attempt"] == "e1.a2" and cm[0]["base_point"] == "p1"
        and cm[0]["point"] == "p2" and b.rw.last_good == "p2", cm)
    p2 = b.rw.points.get("p2") or {}
    delta = RW.digest_delta(p1.get("digest") or {}, p2.get("digest") or {})
    chk("4_p2_adds_only_props", delta == ["col:PROPS_set"], delta)
    # -- 5. the gui intent queued before the rewind closes STALE_FRAME; resubmitted on the new epoch -> VERIFIED
    step_until("g1", 1)
    g = closes("g1")
    chk("5_gui_stale_after_rewind", [r["verdict"] for r in g] == ["STALE_FRAME"], [(r["verdict"], r.get("reason")) for r in g])
    if b.mode == "IDLE":
        put(rt, "camera-agent", "g2", b.frame, lane="gui", keys=["KP_1"], write_set=[])
        step_until("g2", 1)
    g2 = closes("g2")
    chk("5_gui_resubmit_verified", [r["verdict"] for r in g2] == ["VERIFIED"] and b.rw.attempts.get("g2.a1") == "commit",
        [(r["verdict"], r.get("reason"), r.get("attempts")) for r in g2])
    # -- 6. audit of events.jsonl (in process and through the frozen CLI) + what is really in Blender
    recs = RW.read_events(b.p_events)
    au = CTL.audit(recs)
    cli = subprocess.run([sys.executable, os.path.join(HERE, "rewindctl.py"), "audit", "--rt", rt],
                         capture_output=True, text=True, timeout=60)
    chk("audit_clean", au["ok"] and cli.returncode == 0, {"problems": au["problems"][:4], "cli_rc": cli.returncode})
    live = RW.dispatch_control(b, RW.control_job_id("digest", "final"), R1OPS.digest_script())
    live_digest = (live["result"] or {}).get("digest") if live["ok"] else None
    lg = b.rw.points.get(b.rw.last_good) or {}
    chk("final_live_digest_equals_last_good", RW.digest_match(live_digest, lg.get("digest")),
        {"last_good": b.rw.last_good, "delta": RW.digest_delta(live_digest or {}, lg.get("digest") or {})})
    fp = RW.dispatch_control(b, RW.control_job_id("probe", "final"), PROBE_FINAL)
    got = (fp["result"] or {}).get("value") if fp["ok"] else None
    chk("final_probe_only_accepted_work", got == FINAL_WANT, {"got": got, "want": FINAL_WANT})
    chk("mode_idle", b.mode == "IDLE", b.mode)
    ex = b.export_point(b.rw.confirmed_tick)
    chk("export_inside_frontier", ex["ok"], {k: ex.get(k) for k in ("code", "point", "tick")})
    chk("export_beyond_frontier_refused", b.export_point(b.rw.confirmed_tick + 1)["ok"] is False)
    snap = b.snapshot()
    b.close()
    b2 = CTL.make_broker(rt, a.job)
    chk("replay_same_state", b2.snapshot() == snap)
    b2.close()
    return {"steps": steps, "summary": au["summary"], "audit": au, "audit_cli_rc": cli.returncode,
            "final_probe": got, "points": {p: {k: r[k] for k in ("tick", "scene_epoch", "blend_sha256", "bytes", "frame")}
                                           for p, r in RW.replay(recs).points.items()}}


def save_evidence(a, out):
    ev, rt = a.evidence, a.rt
    os.makedirs(ev, exist_ok=True)
    for n in ("events.jsonl", "cursor.jsonl", "claims.json"):
        if os.path.exists(os.path.join(rt, n)):
            shutil.copyfile(os.path.join(rt, n), os.path.join(ev, n))
    man = os.path.join(rt, RW.CKPT_DIRNAME, "manifest.jsonl")
    if os.path.exists(man):
        shutil.copyfile(man, os.path.join(ev, "ckpt_manifest.jsonl"))
    keep = {}
    for p, r in out.get("points", {}).items():
        keep["frame_%s.png" % p] = os.path.join(rt, "frames", "frame_%d.png" % r["frame"])
    for label, by in (("frame_e1a1_wrong.png", "e1.a1"), ("frame_restored_p1.png", "ctl.restore.p1.e2")):
        keep[label] = frame_of(rt, by)
    if not a.mutant:
        for label, src in keep.items():
            if src and os.path.exists(src):
                shutil.copyfile(src, os.path.join(ev, label))
        ok, detail = make_video(rt, os.path.join(ev, "rewind_live.mp4"))
        out["video"] = {"ok": ok, "detail": detail}
    else:
        src = keep.get("frame_e1a1_wrong.png")
        if src and os.path.exists(src):
            shutil.copyfile(src, os.path.join(ev, "frame_e1a1_wrong.png"))
    with open(os.path.join(ev, "live.json"), "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--rt", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--mutant", choices=sorted(MUTANTS), default=None)
    a = ap.parse_args()
    a.rt = os.path.abspath(a.rt)
    if not a.rt.startswith(RT_PREFIX) or not a.job.startswith("vprw"):
        print("refused: RT must be under %s* and the job must be named vprw*" % RT_PREFIX)
        return 2
    if os.path.exists(os.path.join(a.rt, "events.jsonl")):
        print("refused: %s is not fresh" % a.rt)
        return 2
    os.makedirs(a.rt, exist_ok=True)
    with open(os.path.join(a.rt, "claims.json"), "w") as f:
        json.dump(CLAIMS, f)
    if a.launch:
        os.environ["VP_SWARM_NONCE"] = secrets.token_hex(8)
        rc, tail = launch(a.job, a.rt, os.environ["VP_SWARM_NONCE"])
        print("vision start rc=%d %s" % (rc, tail), flush=True)
        if rc != 0:
            return 2
    checks, out = [], {}
    try:
        out = scenario(a, checks)
    except Exception as e:  # noqa: BLE001  (a crash is a failed run, and the job is still stopped)
        checks.append({"check": "scenario_completed", "ok": False, "detail": "%s: %s" % (type(e).__name__, e)})
        print("FAIL scenario_completed %s: %s" % (type(e).__name__, e), flush=True)
    finally:
        if a.launch:
            print("vision stop rc=%d %s" % stop(a.job, a.rt), flush=True)
    red = [c["check"] for c in checks if not c["ok"]]
    out.update({"mutant": a.mutant, "checks": checks, "red": red, "job": a.job, "rt": a.rt,
                "contract": RW.CONTRACT_VERSION, "judge_strict_outside": CTL.JUDGE_STRICT_OUTSIDE})
    if a.mutant:
        target = MUTANTS[a.mutant]
        out["must_fail"] = {"designated_check": target, "went_red": target in red}
        out["ok"] = target in red            # the must-FAIL run holds when the designated check is RED
    else:
        out["ok"] = not red
    save_evidence(a, out)
    print(json.dumps({"ok": out["ok"], "mutant": a.mutant, "red": red, "summary": out.get("summary")}))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
