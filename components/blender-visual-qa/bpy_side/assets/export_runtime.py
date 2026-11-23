"""LOD ladder worker. Run INSIDE Blender:
  blender -b --factory-startup -noaudio --python bpy_side/assets/export_runtime.py -- \
      --in SRC.glb --out LADDER.glb --plan PLAN.json
PLAN = {prefix: {"f0": int, "caps": [cap0, cap1, cap2], "floor": int}} (computed by the host, budget B(f0, p)).
Each visual mesh object P becomes P_LOD0 (untouched copy) and P_LOD1/P_LOD2: DECIMATE COLLAPSE (triangulated) from
LOD0 with the largest ratio whose evaluated triangle count is <= cap_k; a transition that is not required keeps the
previous LOD. Helper meshes (*_collision, -col, -convcol) are exported unchanged and never get LODs.
Prints one line `BVQA_LOD {json}` (integers only; informational, the host re-parses the GLB). rc 2 on bad input.
"""
import argparse
import json
import sys

HELPERS = ("_collision", "-col", "-convcol")
SEARCH_STEPS = 18


def parse_args(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="export_runtime.py")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--plan", required=True)
    return p.parse_args(argv)


def fail(msg):
    print("BVQA_LOD_ERROR " + json.dumps({"error": msg}, sort_keys=True))
    sys.stdout.flush()
    raise SystemExit(2)


def tri_count(bpy, ob):
    dg = bpy.context.evaluated_depsgraph_get()
    me = ob.evaluated_get(dg).to_mesh()
    me.calc_loop_triangles()
    n = len(me.loop_triangles)
    ob.evaluated_get(dg).to_mesh_clear()
    return n


def bake(bpy, ob):
    """Replace ob's mesh with its evaluated mesh (modifiers applied) without operator context."""
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    ob.modifiers.clear()
    ob.data = me


def decimate(bpy, base, name, cap):
    """Copy of base decimated to the largest COLLAPSE ratio with triangles <= cap (bisection on the ratio)."""
    ob = base.copy()
    ob.data = base.data.copy()
    ob.name = name
    ob.data.name = name
    for col in base.users_collection:
        col.objects.link(ob)
    mod = ob.modifiers.new("bvqa_lod", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.use_collapse_triangulate = True
    lo, hi, best = 0.0, 1.0, None
    for _ in range(SEARCH_STEPS):
        mid = (lo + hi) / 2.0
        mod.ratio = mid
        n = tri_count(bpy, ob)
        if n <= cap:
            lo, best = mid, (mid, n)
        else:
            hi = mid
    mod.ratio = best[0] if best else lo
    bake(bpy, ob)
    return ob, tri_count(bpy, ob), int(round((best[0] if best else lo) * 1000000))


def main():
    import bpy

    a = parse_args(sys.argv)
    try:
        with open(a.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        fail("plan unreadable: %s" % exc)
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    try:
        bpy.ops.import_scene.gltf(filepath=a.inp)
    except RuntimeError as exc:
        fail("import failed: %s" % exc)
    report = {}
    meshes = [ob for ob in bpy.context.scene.objects if ob.type == "MESH" and not ob.name.endswith(HELPERS)]
    for ob in sorted(meshes, key=lambda o: o.name):
        prefix = ob.name
        if prefix not in plan:
            fail("mesh %s not in plan %s" % (prefix, sorted(plan)))
        caps = plan[prefix]["caps"]
        src_name = prefix
        ob.name = prefix + "_LOD0"
        ob.data.name = prefix + "_LOD0"
        prev, counts, ratios = ob, [tri_count(bpy, ob)], [1000000]
        for k in (1, 2):
            name = "%s_LOD%d" % (prefix, k)
            if counts[-1] > caps[k] and plan[prefix]["f0"] > plan[prefix]["floor"]:
                lod, n, r = decimate(bpy, ob, name, caps[k])
            else:  # saturated / floor-preserved transition: keep the previous geometry
                lod = prev.copy()
                lod.data = prev.data.copy()
                lod.name = name
                lod.data.name = name
                for col in prev.users_collection:
                    col.objects.link(lod)
                n, r = counts[-1], ratios[-1]
            prev = lod
            counts.append(n)
            ratios.append(r)
        report[src_name] = {"tris": counts, "ratio_ppm": ratios, "caps": caps}
    if not report:
        fail("no visual mesh in %s" % a.inp)
    kw = dict(filepath=a.out, export_format="GLB", use_selection=False, export_apply=True, export_yup=True,
              export_animations=False, export_extras=False)
    try:
        bpy.ops.export_scene.gltf(**kw)
    except TypeError:
        kw.pop("export_animations")
        bpy.ops.export_scene.gltf(**kw)
    print("BVQA_LOD " + json.dumps({"prefixes": report}, sort_keys=True))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
