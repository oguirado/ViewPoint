#!/usr/bin/env python3
"""Scripted fake agent for --stub mode: no model, deterministic, valid intents.

Reads the newest frame record in RT/frames (for based_on_frame), then writes its
scripted intents atomically into RT/intents/<agent_id>/. The bpy scripts are real
and run in Blender 4.0.2 (see tests: they are executed in `blender -b`).

CLI: python3 stub_agent.py --agent modeler-agent --rt RT [--count N] [--delay-ms 0]
"""
import argparse
import json
import re
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import contract  # noqa: E402

PRELUDE = '''import bpy, bmesh, math
scene = bpy.data.scenes[0]
COL = "%(col)s"
col = bpy.data.collections.get(COL)
if col is None:
    col = bpy.data.collections.new(COL)
    scene.collection.children.link(col)

def mat(name, rgba):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        m.diffuse_color = rgba
    return m

def obj_for(name, data=None):
    full = COL + "." + name
    o = col.objects.get(full)
    if o is None:
        d = data if data is not None else bpy.data.meshes.new(full)
        o = bpy.data.objects.new(full, d)
        col.objects.link(o)
    return o

def fill(o, build, material=None):
    bm = bmesh.new()
    build(bm)
    bm.to_mesh(o.data)
    bm.free()
    o.data.update()
    if material is not None:
        o.data.materials.clear()
        o.data.materials.append(material)
    return o

def cube(name, loc, scale, material=None):
    o = fill(obj_for(name), lambda bm: bmesh.ops.create_cube(bm, size=1.0), material)
    o.location = loc
    o.scale = scale
    return o

def ball(name, loc, radius, material=None, scale=(1, 1, 1)):
    o = fill(obj_for(name), lambda bm: bmesh.ops.create_uvsphere(
        bm, u_segments=16, v_segments=10, radius=radius), material)
    o.location = loc
    o.scale = scale
    return o

def cone(name, loc, r1, r2, depth, material=None, rot=(0, 0, 0)):
    o = fill(obj_for(name), lambda bm: bmesh.ops.create_cone(
        bm, cap_ends=True, segments=16, radius1=r1, radius2=r2, depth=depth), material)
    o.location = loc
    o.rotation_euler = rot
    return o
'''

# (body, expect) per agent; script lane unless the body is a list of keys.
PLAYBOOK = {
    "modeler-agent": [
        ('cloth = mat("CHARMAT_cloth", (0.16, 0.33, 0.75, 1))\n'
         'cube("torso", (0, 0, 0.90), (0.55, 0.35, 0.55), cloth)\n'
         'cube("hips", (0, 0, 0.55), (0.48, 0.32, 0.22), cloth)\n',
         "A blue blocky torso and hips appear at the origin, chest centre at Z 0.90."),
        ('skin = mat("CHARMAT_skin", (0.95, 0.72, 0.55, 1))\n'
         'ball("head", (0, 0, 1.55), 0.36, skin)\n',
         "A round skin-coloured head appears above the torso, centre at Z 1.55."),
        ('skin = mat("CHARMAT_skin", (0.95, 0.72, 0.55, 1))\n'
         'cloth = mat("CHARMAT_cloth", (0.16, 0.33, 0.75, 1))\n'
         'for s, x in (("L", -1), ("R", 1)):\n'
         '    cube("arm." + s, (x * 0.46, 0, 0.93), (0.14, 0.14, 0.46), cloth)\n'
         '    ball("hand." + s, (x * 0.55, 0, 0.70), 0.10, skin)\n'
         '    cube("leg." + s, (x * 0.18, 0, 0.24), (0.18, 0.20, 0.44), cloth)\n'
         '    cube("foot." + s, (x * 0.18, -0.06, 0.04), (0.20, 0.32, 0.08), cloth)\n',
         "Two arms with hands and two legs with feet appear; the feet rest on Z 0."),
    ],
    "props-agent": [
        ('metal = mat("CHARMAT_metal", (0.75, 0.76, 0.80, 1))\n'
         'cone("sword.blade", (0.55, -0.05, 1.12), 0.035, 0.005, 0.80, metal)\n'
         'cube("sword.guard", (0.55, -0.05, 0.74), (0.22, 0.05, 0.04), metal)\n',
         "A short metal sword appears in the right hand, pointing up."),
        ('accent = mat("CHARMAT_accent", (0.85, 0.15, 0.12, 1))\n'
         'cone("shield", (-0.62, -0.10, 0.80), 0.28, 0.28, 0.05, accent, (math.radians(90), 0, 0))\n'
         'cone("crest", (0, 0, 2.02), 0.10, 0.0, 0.26, accent)\n',
         "A red round shield appears at the left hand and a red crest on top of the head."),
    ],
    "look-agent": [
        ('floor_m = mat("CHARMAT_floor", (0.80, 0.78, 0.72, 1))\n'
         'cone("floor", (0, 0, -0.01), 2.2, 2.2, 0.02, floor_m)\n'
         'for n, rgba in (("CHARMAT_skin", (0.96, 0.74, 0.56, 1)), ("CHARMAT_cloth", (0.14, 0.34, 0.78, 1)),\n'
         '                ("CHARMAT_metal", (0.78, 0.79, 0.83, 1)), ("CHARMAT_accent", (0.88, 0.16, 0.12, 1))):\n'
         '    mat(n, rgba).diffuse_color = rgba\n',
         "A pale floor disc appears under the character and the shared palette is set."),
        ('for n, loc, energy in (("key", (-2.5, -3.0, 3.5), 900.0), ("fill", (3.0, -2.0, 1.5), 300.0),\n'
         '                       ("rim", (0.5, 3.0, 3.0), 600.0)):\n'
         '    full = COL + ".light." + n\n'
         '    ld = bpy.data.lights.get(full) or bpy.data.lights.new(full, "AREA")\n'
         '    ld.energy = energy\n'
         '    o = obj_for("light." + n, data=ld)\n'
         '    o.location = loc\n',
         "Three area lights (key, fill, rim) appear around the character."),
    ],
    "camera-agent": [
        (["KP_1", "Home"], "Front orthographic view with the whole character framed."),
        (["KP_3", "Home"], "Side view with the whole character framed."),
        (["KP_4", "KP_4", "KP_Add"], "View orbited to a three-quarter angle and zoomed in one step."),
    ],
}


MAT_RE = re.compile(r'"(CHARMAT_[A-Za-z0-9_]+)"')


def build_intents(agent_id, based_on_frame, count=None, start=1, claims=None, rewind=False):
    """Return the list of intent dicts this stub would write.
    rewind=True adds the optional rw1 field `touches` (every CHARMAT_* material the job
    names): under the rewind judge a material change that is not declared there is
    DELTA_OUTSIDE and the turn is rolled back."""
    claims = contract.DEFAULT_CLAIMS if claims is None else claims
    claim = claims[agent_id]
    colname = claim[0][4:]
    out = []
    steps = PLAYBOOK[agent_id][: count if count is not None else None]
    for i, (body, expect) in enumerate(steps):
        rec = {"v": contract.V, "rec": "enqueue",
               "event_id": "%s-%d" % (agent_id, start + i), "agent_id": agent_id}
        if isinstance(body, list):
            rec.update(lane="gui", kind="key", based_on_frame=based_on_frame,
                       write_set=[], keys=list(body), expect=expect)
        else:
            rec.update(lane="script", kind="job", based_on_frame=based_on_frame,
                       write_set=[claim[0]], script=PRELUDE % {"col": colname} + body,
                       expect=expect)
            if rewind:
                rec["touches"] = sorted(set("mat:" + m for m in MAT_RE.findall(body)))
        out.append(rec)
    return out


def write_intent(rt, rec):
    """Atomic write of one intent file; returns its path."""
    d = os.path.join(rt, "intents", rec["agent_id"])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, rec["event_id"] + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def next_event_number(rt, agent_id):
    d = os.path.join(rt, "intents", agent_id)
    best = 0
    try:
        for n in os.listdir(d):
            m = contract.EVENT_ID_RE.match(n[:-5]) if n.endswith(".json") else None
            if m:
                best = max(best, int(m.group("n")))
    except OSError:
        pass
    return best + 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="scripted stub agent")
    ap.add_argument("--agent", required=True, choices=contract.AGENT_IDS)
    ap.add_argument("--rt", default=None)
    ap.add_argument("--count", type=int, default=None)
    ap.add_argument("--delay-ms", type=int, default=0, help="pause between intents")
    ap.add_argument("--wait-frame-s", type=float, default=5.0)
    ap.add_argument("--rewind", action="store_true",
                    help="add the rw1 `touches` field (materials the job names)")
    a = ap.parse_args(argv)
    rt = contract.rt_root(a.rt)
    deadline = time.time() + a.wait_frame_s
    frame = contract.newest_frame(rt)
    while frame is None and time.time() < deadline:
        time.sleep(0.1)
        frame = contract.newest_frame(rt)
    if frame is None:
        print(json.dumps({"type": "error", "agent_id": a.agent, "error": "no frame in RT/frames"}),
              flush=True)
        return 3
    claims = contract.load_claims(rt)
    start = next_event_number(rt, a.agent)
    for rec in build_intents(a.agent, frame, a.count, start, claims, rewind=a.rewind):
        p = write_intent(rt, rec)
        print(json.dumps({"type": "stub_intent", "agent_id": a.agent,
                          "event_id": rec["event_id"], "path": p,
                          "based_on_frame": frame}), flush=True)
        if a.delay_ms:
            time.sleep(a.delay_ms / 1000.0)
    print(json.dumps({"type": "result", "agent_id": a.agent, "is_error": False}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
