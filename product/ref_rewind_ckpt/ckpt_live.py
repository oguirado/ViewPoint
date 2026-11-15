#!/usr/bin/env python3
"""R1 LIVE spike S1 + live proof of mark/restore against a running vision job with the F1 bridge.

    VP_SWARM_NONCE=<nonce> ckpt_live.py --job vprw1 --rt /dev/shm/vqa-cu/ref-rw1 --evidence evidence/

Needs a fresh RT. Every control job goes through schema.dispatch_control (F1 Broker._dispatch_script,
HMAC-checked). Exit 0 only if every check holds; the check list is written to <evidence>/live_s1.json.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ref_rewind"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ref_broker"))
import broker as B  # noqa: E402
import schema as RW  # noqa: E402
import ops  # noqa: E402
from store import Store  # noqa: E402

EDIT = '''
col = bpy.data.collections.new("RW_test"); bpy.context.scene.collection.children.link(col)
me = bpy.data.meshes.new("RW_cube")
me.from_pydata([(0,0,0),(2,0,0),(2,2,0),(0,2,0),(0,0,2),(2,0,2),(2,2,2),(0,2,2)], [],
               [(0,1,2,3),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)])
ob = bpy.data.objects.new("RW_cube", me); ob.location = (0.0, 0.0, 2.5); col.objects.link(ob)
ob.modifiers.new("RW_bevel", "BEVEL").width = 0.2
mat = bpy.data.materials.new("RW_mat"); mat.use_nodes = True
mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1.0, 0.1, 0.1, 1.0)
me.materials.append(mat)
bpy.context.scene.render.resolution_x = 1234
for a in bpy.data.window_managers[0].windows[0].screen.areas:
    if a.type == "VIEW_3D":
        a.spaces.active.region_3d.view_distance *= 0.5
        a.tag_redraw()
print("edit done")
'''
# changes only state that the F1 digest does not see (finding F4)
EDIT_F4 = '''
bpy.data.materials["Material"].diffuse_color = (0.0, 1.0, 0.0, 1.0)
bpy.data.objects["Cube"].modifiers.new("RW_sub", "SUBSURF").levels = 2
print("f4 edit done")
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--rt", required=True)
    ap.add_argument("--evidence", required=True)
    a = ap.parse_args()
    os.makedirs(a.evidence, exist_ok=True)
    br = B.Broker(a.rt, a.job, os.environ["VP_SWARM_NONCE"], job_cap_s=RW.RESTORE_CAP_S)
    st = Store(a.rt)
    checks, n = [], [0]

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:600]})
        print("%s %s %s" % ("PASS" if ok else "FAIL", name, str(detail)[:300]), flush=True)

    def ctl(kind, script, *parts):
        n[0] += 1
        return RW.dispatch_control(br, RW.control_job_id(kind, *(parts or (n[0],))), script)

    def digest():
        r = ctl("digest", ops.digest_script())
        return (r["result"] or {}).get("digest") if r["ok"] else None

    def job(eid, script):
        return br._dispatch_script({"event_id": eid, "script": script, "write_set": [],
                                    "script_sha256": hashlib.sha256(script.encode()).hexdigest()})

    def mark(pid, tick, epoch, pinned=()):
        r = ctl("mark", ops.mark_script(st.path_for(pid)), pid)
        if not r["ok"] or not r["result"]:
            return r, None
        return r, st.admit(pid, tick, epoch, r["result"]["digest"], max(br.frame, 0), pinned=pinned)

    def restore(pid, epoch_after, load_ui=True):
        path = st.verify(pid)
        return ctl("restore", ops.restore_script(path, load_ui, sha256=st.get(pid)["blend_sha256"]),
                   pid, "e%d" % epoch_after)

    def frame(tag):
        time.sleep(1.5)
        br._capture(None, [])
        dst = os.path.join(a.evidence, "frame_%s.png" % tag)
        shutil.copyfile(br.frame_png, dst)
        return dst

    def inside(regs):
        fx, fy, fw, fh = B.FOOTPRINT      # regions that intersect the viewport footprint
        return [r for r in regs if r[0] < fx + fw and r[0] + r[2] > fx and r[1] < fy + fh and r[1] + r[3] > fy]

    epoch = RW.SCENE_EPOCH_0
    # -- S1.1 mark from the timer job
    r, rec0 = mark("p0", 0, epoch)
    check("S1.mark_from_timer", r["ok"] and rec0 is not None, {k: r.get(k) for k in ("code", "job_ms", "reason")})
    if rec0 is None:
        return finish(a, checks, br)
    d0 = rec0["digest"]
    check("mark.result_shape", r["result"]["op"] == "mark" and r["result"]["bytes"] == rec0["bytes"],
          "bytes=%d via=%s ms=%s" % (rec0["bytes"], r["result"].get("via"), r["result"].get("ms")))
    check("digest.v2_namespaces", "render" in d0 and "ui:view3d" in d0 and any(k.startswith("mat:") for k in d0)
          and any(k.startswith("col:") for k in d0), sorted(d0))
    # -- S1.2 open_mainfile from the timer job returns, authenticated, digest equal (also closes the splash)
    epoch += 1
    r = restore("p0", epoch)
    check("S1.open_mainfile_returns", r["ok"], {k: r.get(k) for k in ("code", "job_ms", "reason")})
    check("S1.restore_digest_equals_point", r["ok"] and RW.digest_match(r["result"]["digest"], d0),
          "via=%s ms=%s" % ((r["result"] or {}).get("via"), (r["result"] or {}).get("ms")))
    # -- S1.3 the bridge timer survived the load (must-fail twin: test_ckpt non-persistent bridge)
    d = digest()
    check("S1.timer_alive_after_load", d is not None and RW.digest_match(d, d0))
    f_base = frame("base_p0")
    # -- edit covered by digest v2: collection, material, modifier, render, view
    v, ex = job("edit1", EDIT)
    d1 = digest()
    delta = RW.digest_delta(d0, d1 or {})
    check("edit.delta_keys", v in ("STATE_VERIFIED", "VERIFIED_WITH_SPILL")
          and set(delta) >= {"col:RW_test", "mat:RW_mat", "render", "ui:view3d"}, "%s %s %s" % (v, delta, ex.get("out_tail", "")[-200:]))
    f_edit = frame("after_edit")
    reg = inside(B.diff_regions(f_base, f_edit))
    check("witness.edit_visible_in_footprint", bool(reg), reg[:4])
    # -- rollback: restore p0, digest + view + frame equal
    epoch += 1
    r = restore("p0", epoch)
    check("rollback.digest_equals_p0", r["ok"] and RW.digest_match(r["result"]["digest"], d0),
          RW.digest_delta(d0, (r["result"] or {}).get("digest") or {}))
    f_back = frame("restored_p0")
    regs = B.diff_regions(f_base, f_back)
    check("S1.witness_frame_equals_base_in_footprint", inside(regs) == [], "all regions: %s" % regs[:6])
    # -- finding F4: state invisible to the F1 digest is inside digest v2 and is rolled back
    v, ex = job("edit2", EDIT_F4)
    d2 = digest()
    delta = RW.digest_delta(d0, d2 or {})
    check("F4.f1_digest_blind_v2_sees", v == "STATE_VERIFIED" and "mat:Material" in delta and any(k.startswith("col:") for k in delta),
          "F1 changed=%s v2 delta=%s" % (ex.get("changed_write_set"), delta))
    epoch += 1
    r = restore("p0", epoch)
    check("F4.rolled_back", r["ok"] and RW.digest_match(r["result"]["digest"], d0))
    # -- GUI camera turn: a view key is inside the rollback domain only with load_ui=True
    br.vision.act(["--xy", B.HOVER_XY])
    rc, out = br.vision.act(["--key", "KP_1", "--expect-change", "%d,%d,%d,%d" % B.FOOTPRINT])
    time.sleep(1.0)
    d3 = digest()
    check("gui.key_changes_only_view", rc == 0 and RW.digest_delta(d0, d3 or {}) == ["ui:view3d"], "rc=%s delta=%s" % (rc, RW.digest_delta(d0, d3 or {})))
    epoch += 1
    r = restore("p0", epoch, load_ui=False)
    check("gui.load_ui_false_keeps_view", r["ok"] and RW.digest_delta(d0, r["result"]["digest"]) == ["ui:view3d"],
          RW.digest_delta(d0, (r["result"] or {}).get("digest") or {}))
    epoch += 1
    r = restore("p0", epoch, load_ui=True)
    check("S1.load_ui_true_restores_view", r["ok"] and RW.digest_match(r["result"]["digest"], d0))
    f_view = frame("restored_view")
    check("gui.witness_frame_equals_base", inside(B.diff_regions(f_base, f_view)) == [])
    # -- dedup: unchanged scene -> no new file
    r, rec1 = mark("p1", 1, epoch, pinned=("p0",))
    check("store.dedup_live", rec1 is not None and rec1.get("dedup_of") == "p0" and not os.path.exists(st.path_for("p1")), rec1 and rec1.get("dedup_of"))
    # -- tamper: a changed point p2, one byte flipped
    job("edit3", EDIT)
    r, rec2 = mark("p2", 2, epoch, pinned=("p0",))
    check("mark.second_point", rec2 is not None and "dedup_of" not in rec2 and rec2["blend_sha256"] != rec0["blend_sha256"])
    d_before = digest()
    p2 = st.path_for("p2")
    with open(p2, "r+b") as f:
        f.seek(os.path.getsize(p2) // 2)
        b = f.read(1)
        f.seek(-1, 1)
        f.write(bytes([b[0] ^ 1]))
    try:
        st.verify("p2")
        check("tamper.store_refuses", False, "verify passed on a flipped byte")
    except RW.CkptError as e:
        check("tamper.store_refuses", e.code == RW.CKPT_CORRUPT, e.code)
    r = ctl("restore", ops.restore_script(p2, True, sha256=rec2["blend_sha256"]), "p2", "e%d" % (epoch + 1))
    check("tamper.blender_side_refuses", (not r["ok"]) and r["code"] == "JOB_ERROR" and RW.digest_match(digest(), d_before), r["code"])
    epoch += 1
    r = restore("p0", epoch)
    check("final.back_to_p0", r["ok"] and RW.digest_match(r["result"]["digest"], d0), "scene_epoch=%d" % epoch)
    return finish(a, checks, br, {"points": {p: st.get(p) for p in st.points()}, "scene_epoch": epoch,
                                  "ckpt_bytes": st.bytes_used()})


def finish(a, checks, br, extra=None):
    ok = all(c["ok"] for c in checks)
    out = {"ok": ok, "passed": sum(c["ok"] for c in checks), "failed": sum(not c["ok"] for c in checks),
           "checks": checks, "job": a.job, "rt": a.rt}
    out.update(extra or {})
    with open(os.path.join(a.evidence, "live_s1.json"), "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    br.close()
    print("LIVE %s %d/%d" % ("GREEN" if ok else "RED", out["passed"], len(checks)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
