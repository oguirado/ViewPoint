#!/usr/bin/env python3
"""LIVE smoke for the reference broker against a running vision job with bridge.py loaded.

    VP_SWARM_NONCE=<nonce> smoke_live.py --job vpref1 --rt /dev/shm/vqa-cu/ref1 --evidence evidence/

Plays three scripted (non-LLM) agents for 3 rounds of 4 turns through the broker CLI:
  round 1: modeler-agent job, look-agent job, camera-agent gui intent citing frame 0 -> STALE_FRAME
  round 2: camera-agent re-observes the head frame and resubmits the same event -> VERIFIED (rebased)
  round 3: props-agent read-only inspection job (checks the Blender objects)
Exit 0 only if every expectation holds. Copies events.jsonl, cursor.jsonl and frames to --evidence.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLAIMS = {"modeler-agent": ["col:CHAR_mesh"], "props-agent": ["col:PROPS_set"],
          "look-agent": ["col:LOOK_lights"], "camera-agent": ["col:CAM_rig"]}

MODELER = '''
import bpy, bmesh
col = bpy.data.collections.get("CHAR_mesh") or bpy.data.collections.new("CHAR_mesh")
if col.name not in bpy.context.scene.collection.children: bpy.context.scene.collection.children.link(col)
me = bpy.data.meshes.new("CHAR_body"); bm = bmesh.new()
bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=12, radius=1.2); bm.to_mesh(me); bm.free()
ob = bpy.data.objects.new("CHAR_body", me); ob.location = (0.0, 0.0, 3.0); col.objects.link(ob)
print("made", ob.name, len(me.vertices))
'''
LOOK = '''
import bpy
col = bpy.data.collections.get("LOOK_lights") or bpy.data.collections.new("LOOK_lights")
if col.name not in bpy.context.scene.collection.children: bpy.context.scene.collection.children.link(col)
ld = bpy.data.lights.new("LOOK_key", type="AREA"); ld.energy = 800.0; ld.size = 3.0
ob = bpy.data.objects.new("LOOK_key", ld); ob.location = (4.0, -4.0, 6.0); col.objects.link(ob)
print("made", ob.name)
'''
INSPECT = '''
import bpy, json
print("SCENE " + json.dumps({c.name: sorted(o.name for o in c.objects) for c in bpy.data.collections}, sort_keys=True))
'''


def put(rt, agent, eid, **fields):
    it = {"v": 1, "rec": "enqueue", "event_id": eid, "agent_id": agent, "expect": fields.pop("expect", "")}
    it.update(fields)
    d = os.path.join(rt, "intents", agent)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, eid + ".tmp"), "w") as f:
        json.dump(it, f)
    os.replace(os.path.join(d, eid + ".tmp"), os.path.join(d, eid + ".json"))


def head_frame(rt):
    return max(int(n[6:-5]) for n in os.listdir(os.path.join(rt, "frames")) if n.endswith(".json"))


def run_round(job, rt):
    p = subprocess.run([sys.executable, os.path.join(HERE, "broker.py"), "run", "--job", job, "--rt", rt, "--turns", "4"],
                       capture_output=True, text=True)
    sys.stderr.write(p.stderr)
    print(p.stdout.strip())
    return p.returncode, json.loads(p.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--rt", required=True)
    ap.add_argument("--evidence", required=True)
    a = ap.parse_args()
    rt = a.rt
    os.makedirs(rt, exist_ok=True)
    if os.path.exists(os.path.join(rt, "events.jsonl")):
        sys.exit("refusing to reuse an RT that already has events.jsonl: %s" % rt)
    with open(os.path.join(rt, "claims.json"), "w") as f:
        json.dump(CLAIMS, f)
    put(rt, "modeler-agent", "modeler-agent-1", lane="script", kind="job", based_on_frame=0,
        write_set=["col:CHAR_mesh"], script=MODELER, expect="CHAR_body sphere above the cube")
    put(rt, "look-agent", "look-agent-1", lane="script", kind="job", based_on_frame=0,
        write_set=["col:LOOK_lights"], script=LOOK, expect="LOOK_key area light")
    put(rt, "camera-agent", "camera-agent-1", lane="gui", kind="key", based_on_frame=0,
        write_set=[], keys=["KP_1", "KP_3"], expect="front view then right view")
    fails = []

    def expect(name, got, want):
        ok = got == want
        print("%s %s: %s" % ("PASS" if ok else "FAIL", name, got if ok else "got %r want %r" % (got, want)))
        if not ok:
            fails.append(name)

    rc, r1 = run_round(a.job, rt)
    expect("round1 verdicts", r1.get("verdicts"), ["STATE_VERIFIED", "PASS", "STATE_VERIFIED", "STALE_FRAME"])
    # scripted camera agent between rounds: re-observes the head frame and resubmits the same event
    put(rt, "camera-agent", "camera-agent-1", lane="gui", kind="key", based_on_frame=head_frame(rt),
        write_set=[], keys=["KP_1", "KP_3"], expect="front view then right view")
    rc, r2 = run_round(a.job, rt)
    expect("round2 verdicts", r2.get("verdicts"), ["PASS", "PASS", "PASS", "VERIFIED"])
    # round 3: props-agent checks the Blender objects with a read-only job
    put(rt, "props-agent", "props-agent-1", lane="script", kind="job", based_on_frame=head_frame(rt),
        write_set=["col:PROPS_set"], script=INSPECT, expect="read-only scene inventory")
    rc, r3 = run_round(a.job, rt)
    expect("round3 verdicts", r3.get("verdicts"), ["PASS", "STATE_VERIFIED", "PASS", "PASS"])

    recs = [json.loads(l) for l in open(os.path.join(rt, "events.jsonl"))]
    insp = next((r for r in recs if r["rec"] == "close" and r.get("event_id") == "props-agent-1"), {})
    scene = {}
    for line in (insp.get("out_tail") or "").splitlines():
        if line.startswith("SCENE "):
            scene = json.loads(line[6:])
    expect("blender CHAR_mesh objects", scene.get("CHAR_mesh"), ["CHAR_body"])
    expect("blender LOOK_lights objects", scene.get("LOOK_lights"), ["LOOK_key"])
    expect("inspection job changed nothing", insp.get("changed_write_set"), [])
    cam = [r for r in recs if r["rec"] == "close" and r.get("event_id") == "camera-agent-1"]
    expect("camera closes", [(c["verdict"], c.get("rebased"), c.get("acts_done")) for c in cam],
           [("STALE_FRAME", False, None), ("VERIFIED", True, 2)])
    frames = sorted(int(n[6:-5]) for n in os.listdir(os.path.join(rt, "frames")) if n.endswith(".json"))
    expect("frames", frames, [0, 1, 2, 3, 4])
    shas = [json.load(open(os.path.join(rt, "frames", "frame_%d.json" % n)))["sha256"] for n in frames]
    if len(shas) == 5:
        expect("frames 0,1,2,3 pixel-distinct", len(set(shas[:4])), 4)

    os.makedirs(a.evidence, exist_ok=True)
    for n in ("events.jsonl", "cursor.jsonl", "claims.json"):
        shutil.copyfile(os.path.join(rt, n), os.path.join(a.evidence, n))
    for n in frames:
        for ext in ("json", "png"):
            shutil.copyfile(os.path.join(rt, "frames", "frame_%d.%s" % (n, ext)), os.path.join(a.evidence, "frame_%d.%s" % (n, ext)))
    print("SMOKE %s (%d failed)" % ("FAIL" if fails else "PASS", len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
