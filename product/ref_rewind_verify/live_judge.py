"""R3 live check: the judge against a real Blender under a vision job (F1 broker + F1 bridge).

Track P (claude-fable-5-2). Needs a FRESH RT and a running vision job with ref_broker/bridge.py loaded:

    VP_SWARM_NONCE=$N python3 live_judge.py --job vprw3 --rt /dev/shm/vqa-cu/ref-rw3 --evidence evidence

Scope: real probe jobs, real frames, real F1 diff_regions. The digest is an inline stand-in (F1 collection
digest + ui:view3d) because R1's digest v2 is not merged in this branch, and the "restore" is a hand-written
undo script, NOT R1's open_mainfile restore: this run proves the judge, not the rewind.
"""
import argparse
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import judge as J  # noqa: E402
B, RW = J.B, J.RW

DIGEST_FN = r'''
import bpy, hashlib, json
def _rw_digest():
    out = {}
    for col in bpy.data.collections:
        h = hashlib.sha256()
        for ob in sorted(col.objects, key=lambda o: o.name):
            mw = [round(v, 5) for row in ob.matrix_world for v in row]
            nv = len(ob.data.vertices) if ob.type == "MESH" and ob.data else -1
            h.update(repr((ob.name, ob.type, mw, ob.data.name if ob.data else "", nv)).encode())
        out["col:" + col.name] = h.hexdigest()[:16]
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                r3d = area.spaces.active.region_3d
                vm = [round(v, 4) for row in r3d.view_matrix for v in row]
                out["ui:view3d"] = hashlib.sha256(repr((vm, r3d.view_perspective)).encode()).hexdigest()[:16]
                return out
    return out
def _rw_print(obj):
    print("RW_RESULT " + json.dumps(obj, sort_keys=True, separators=(",", ":")))
def _rw_redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            area.tag_redraw()
'''

BOX_FN = r'''
def _rw_box(name, col, loc, size):
    import bpy
    s = size / 2.0
    vs = [(x * s, y * s, z * s) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    fs = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    me = bpy.data.meshes.new(name + "_mesh")
    me.from_pydata(vs, [], fs)
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    c = bpy.data.collections.get(col)
    if c is None:
        c = bpy.data.collections.new(col)
        bpy.context.scene.collection.children.link(c)
    c.objects.link(ob)
    bpy.context.view_layer.update()
    return ob
'''

SETUP = DIGEST_FN + BOX_FN + r'''
for n in ("Cube",):
    ob = bpy.data.objects.get(n)
    if ob is not None:
        bpy.data.objects.remove(ob, do_unlink=True)
_rw_box("CHAR_body", "CHAR_mesh", (0.0, 0.0, 2.0), 1.0)
c = bpy.data.collections.new("PROPS"); bpy.context.scene.collection.children.link(c)
bpy.context.view_layer.update(); _rw_redraw()
_rw_print({"op": "digest", "digest": _rw_digest()})
'''
DIGEST = DIGEST_FN + '_rw_print({"op": "digest", "digest": _rw_digest()})\n'
# a1: table 10x too large AND moves CHAR_body (spill)        a2: table under the body
WRONG = DIGEST_FN + BOX_FN + r'''
_rw_box("PROP_table", "PROPS", (0.0, 0.0, 0.0), 10.0)
bpy.data.objects["CHAR_body"].location.x += 3.0
bpy.context.view_layer.update(); _rw_redraw()
'''
RIGHT = DIGEST_FN + BOX_FN + r'''
_rw_box("PROP_table", "PROPS", (0.0, 0.0, 0.75), 1.5)
_rw_redraw()
'''
UNDO_WRONG = DIGEST_FN + r'''
ob = bpy.data.objects.get("PROP_table")
if ob is not None:
    me = ob.data
    bpy.data.objects.remove(ob, do_unlink=True)
    bpy.data.meshes.remove(me)
bpy.data.objects["CHAR_body"].location.x -= 3.0
bpy.context.view_layer.update(); _rw_redraw()
_rw_print({"op": "digest", "digest": _rw_digest()})
'''
PROBE = DIGEST_FN + r'''
from mathutils import Vector
def _zs(ob):
    return [(ob.matrix_world @ Vector(c)).z for c in ob.bound_box]
t, b = bpy.data.objects.get("PROP_table"), bpy.data.objects.get("CHAR_body")
ok = bool(t and b and max(_zs(t)) <= min(_zs(b)) + 1e-4)
%s
_rw_print({"value": {"table_top_le_body_bottom": ok}, "digest": _rw_digest()})
'''
PROBE_RO = PROBE % ""
PROBE_DIRTY = PROBE % 'bpy.data.objects["PROP_table"].location.z += 0.0625; bpy.context.view_layer.update()'
EQUALS = {"table_top_le_body_bottom": True}


def sha(path):
    return RW.sha256_file(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--rt", required=True)
    ap.add_argument("--evidence", required=True)
    a = ap.parse_args()
    nonce = os.environ.get("VP_SWARM_NONCE", "")
    if os.path.exists(os.path.join(a.rt, "events.jsonl")):
        print("RT is not fresh", file=sys.stderr)
        return 2
    with open(os.path.join(a.rt, "claims.json"), "w") as f:
        json.dump({"modeler-agent": ["col:CHAR_mesh"], "props-agent": ["col:PROPS"]}, f)
    b = B.Broker(a.rt, a.job, nonce, job_cap_s=30.0)
    n = [0]
    checks, log = [], {"judge_version": RW.JUDGE_VERSION, "footprint": list(J.FOOTPRINT), "records": {}, "frames": {}}

    def ctl(kind, src):
        n[0] += 1
        r = RW.dispatch_control(b, RW.control_job_id(kind, n[0]), src)
        if not r["ok"]:
            raise SystemExit("control job %s failed: %s %s" % (kind, r["code"], r["reason"]))
        return r

    def frame(tag):
        time.sleep(1.5)                      # let the viewport repaint
        b._capture(None, None)
        log["frames"][tag] = {"png": b.frame_png, "sha256": sha(b.frame_png)}
        return b.frame_png

    def run_probe(src):
        n[0] += 1
        r = RW.dispatch_control(b, RW.control_job_id("probe", n[0]), src)
        res = r["result"] or {}
        return {"ok": r["ok"], "result": res.get("value"), "digest": res.get("digest")}

    def check(name, got, want):
        ok = got == want
        checks.append({"check": name, "ok": ok, "got": got, "want": want})
        print("%s %s got=%s" % ("PASS" if ok else "FAIL", name, json.dumps(got)))

    def attempt(k, src, probe_src=PROBE_RO):
        it = {"event_id": "props-agent-1", "agent_id": "props-agent", "lane": "script", "write_set": ["col:PROPS"],
              "expect": {"probe": probe_src, "equals": EQUALS}}
        att = RW.attempt_id("props-agent-1", k)
        verdict, extra = b._dispatch_script({"event_id": att, "script": src, "write_set": it["write_set"],
                                             "script_sha256": hashlib.sha256(src.encode()).hexdigest()})
        after_png = frame("after_a%d" % k)
        after = ctl("digest", DIGEST)["result"]["digest"]
        j = J.judge({"attempt": att, "intent": it, "script": src, "verdict": verdict, "extra": extra},
                    {"point_id": "p0", "digest": base_digest, "frame_png": base_png},
                    {"digest": after, "frame_png": after_png, "job_ms": extra.get("job_ms")}, run_probe=run_probe)
        b._append(j)
        log["records"][att] = {"f1_verdict": verdict, "judge": j}
        return verdict, j, after_png

    try:
        rc, _ = b.vision.act(["--xy", B.HOVER_XY])     # dismiss the Blender splash (it covers the viewport centre)
        log["splash_click_rc"] = rc
        time.sleep(1.0)
        base_digest = ctl("digest", SETUP)["result"]["digest"]
        base_png = frame("base")
        log["base_digest"] = base_digest

        # a1: wrong edit (10x table + moved body)
        v1, j1, wrong_png = attempt(1, WRONG)
        check("a1 judge rejects the wrong edit", j1["pick"], False)
        check("a1 oracles", [(o["name"], o["ok"]) for o in j1["oracles"]],
              [("expect", False), ("delta", False), ("witness", True), ("cost", True)])
        check("a1 rollback witness before any undo is red", J.rollback_witness(base_png, wrong_png)["ok"], False)

        # hand-written undo (stand-in for R1 restore), then the rollback witness
        undone = ctl("digest", UNDO_WRONG)["result"]["digest"]
        check("undo: digest equals base", RW.digest_match(undone, base_digest), True)
        restored_png = frame("restored")
        rw = J.rollback_witness(base_png, restored_png)
        log["records"]["rollback_witness"] = rw
        check("undo: rollback witness inside FOOTPRINT", rw["ok"], True)

        # a2: right edit from the same base
        v2, j2, right_png = attempt(2, RIGHT)
        check("a2 f1 verdict", v2, "STATE_VERIFIED")
        check("a2 judge accepts", j2["pick"], True)
        check("a2 oracles", [(o["name"], o["ok"]) for o in j2["oracles"]],
              [("expect", True), ("delta", True), ("witness", True), ("cost", True)])
        check("best() picks a2", (J.best([j1, j2]) or {}).get("attempt"), "props-agent-1.a2")

        # a probe that moves the table by 6 cm: right answer, dirty digest
        d2 = ctl("digest", DIGEST)["result"]["digest"]
        o = J.expect_oracle({"expect": {"probe": PROBE_DIRTY, "equals": EQUALS}}, d2, run_probe)
        log["records"]["dirty_probe"] = o
        check("mutating probe is PROBE_NOT_READONLY", o, RW.oracle("expect", False, RW.PROBE_NOT_READONLY))
    finally:
        b.close()
    log["checks"] = checks
    log["pass"] = bool(checks) and all(c["ok"] for c in checks)
    os.makedirs(a.evidence, exist_ok=True)
    with open(os.path.join(a.evidence, "live_judge.json"), "w") as f:
        json.dump(log, f, indent=1, sort_keys=True)
    with open(os.path.join(a.rt, "events.jsonl"), "rb") as fi, open(os.path.join(a.evidence, "live_events.jsonl"), "wb") as fo:
        fo.write(fi.read())
    print("LIVE %s %d/%d" % ("PASS" if log["pass"] else "FAIL", sum(c["ok"] for c in checks), len(checks)))
    return 0 if log["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
