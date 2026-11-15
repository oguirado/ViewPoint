"""PBR bake worker. Run INSIDE Blender:
  blender -b --factory-startup -noaudio --python bpy_side/assets/bake_worker.py -- \
      --high HIGH.glb --low LOD0.glb --out-dir DIR --res 1024 --margin-px 8 --cage-extrusion-um 5000 --samples 16
Joins the LOD0 meshes, re-unwraps them (uv.smart_project, then uv.pack_islands) and bakes with Cycles on the CPU,
selected (high) to active (low): NORMAL (tangent space), AO, ROUGHNESS, and EMIT with the high material's Base Color
(albedo.png) or Metallic (metal.png) temporarily re-wired into an emission shader. Every image starts magenta and the
bake never clears it, so unwritten texels stay magenta for the host. Writes {albedo,normal,ao,roughness,metal}.png and
low_uv.glb (the joined LOD0 with the new UVs, no materials).
Prints one line `BVQA_BAKE {json}` (integers only; informational, the host re-verifies every output). rc 2 on error.
"""
import argparse
import json
import math
import os
import sys

MAPS = (("normal", "NORMAL", "Non-Color"), ("ao", "AO", "Non-Color"), ("roughness", "ROUGHNESS", "Non-Color"),
        ("albedo", "EMIT", "sRGB"), ("metal", "EMIT", "Non-Color"))
MAGENTA = (1.0, 0.0, 1.0, 1.0)
DEFAULT_ALBEDO = (0.8, 0.8, 0.8, 1.0)
ANGLE_LIMIT_DEG = 66.0
THREADS = 5
UM_PER_M = 1000000.0


RAMWS_ROOT = os.environ.get("VQA_RAMWS_ROOT", "/dev/shm/vqa-ramws")


def require_ram_out(path):
    """STORAGE LAW L2: frames/maps/scratch may only be written under the ramws root (tmpfs); anything else exits 5."""
    real = os.path.realpath(path)
    if not (real + os.sep).startswith(os.path.realpath(RAMWS_ROOT) + os.sep):
        sys.stderr.write("E_RAM_NOT_TMPFS: %s is outside %s\n" % (path, RAMWS_ROOT))
        sys.exit(5)


def parse_args(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="bake_worker.py")
    p.add_argument("--high", required=True)
    p.add_argument("--low", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--res", type=int, required=True)
    p.add_argument("--margin-px", type=int, required=True)
    p.add_argument("--cage-extrusion-um", type=int, required=True)
    p.add_argument("--samples", type=int, required=True)
    return p.parse_args(argv)


def fail(msg):
    print("BVQA_BAKE_ERROR " + json.dumps({"error": msg}, sort_keys=True))
    sys.stdout.flush()
    raise SystemExit(2)


def import_meshes(bpy, path):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    return [o for o in bpy.data.objects if o not in before and o.type == "MESH"]


def select_only(bpy, objs, active=None):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = active or (objs[0] if objs else None)


def join_low(bpy, low):
    select_only(bpy, low, low[0])
    if len(low) > 1:
        bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    ob.name = "bvqa_bake_low"
    return ob


def unwrap(bpy, ob, res, margin_px):
    select_only(bpy, [ob], ob)
    if not ob.data.uv_layers:
        ob.data.uv_layers.new(name="UVMap")
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    margin = margin_px / float(res)
    bpy.ops.uv.smart_project(angle_limit=math.radians(ANGLE_LIMIT_DEG), island_margin=margin, scale_to_bounds=False)
    bpy.ops.uv.pack_islands(rotate=True, margin=margin)
    bpy.ops.object.mode_set(mode="OBJECT")


def bake_material(bpy, ob):
    mat = bpy.data.materials.new("bvqa_bake_target")
    mat.use_nodes = True
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    mat.node_tree.nodes.active = tex
    ob.data.materials.clear()
    ob.data.materials.append(mat)
    return tex


def high_materials(bpy, high):
    """Unique node materials of the high meshes; meshes without one get a default Principled material."""
    mats = []
    for ob in high:
        if not ob.data.materials or not any(ob.data.materials):
            m = bpy.data.materials.new("bvqa_bake_default")
            m.use_nodes = True
            ob.data.materials.clear()
            ob.data.materials.append(m)
        for m in ob.data.materials:
            if m is not None and m not in mats:
                if not m.use_nodes:
                    m.use_nodes = True
                mats.append(m)
    return mats


def rewire_emit(mat, which):
    """Route Base Color (albedo) or Metallic (metal) into an emission surface; returns an undo closure."""
    nt = mat.node_tree
    out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
    if out is None:
        out = nt.nodes.new("ShaderNodeOutputMaterial")
    surface = out.inputs["Surface"]
    old = surface.links[0].from_socket if surface.is_linked else None
    bsdf = old.node if old is not None and old.node.type == "BSDF_PRINCIPLED" else \
        next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Strength"].default_value = 1.0
    src = bsdf.inputs["Base Color" if which == "albedo" else "Metallic"] if bsdf is not None else None
    if src is not None and src.is_linked:
        nt.links.new(src.links[0].from_socket, em.inputs["Color"])
    elif src is not None and which == "albedo":
        em.inputs["Color"].default_value = tuple(src.default_value)
    elif src is not None:
        v = float(src.default_value)
        em.inputs["Color"].default_value = (v, v, v, 1.0)
    else:
        em.inputs["Color"].default_value = DEFAULT_ALBEDO if which == "albedo" else (0.0, 0.0, 0.0, 1.0)
    nt.links.new(em.outputs["Emission"], surface)

    def undo():
        if old is not None:
            nt.links.new(old, surface)
        nt.nodes.remove(em)
    return undo


def setup_cycles(bpy, samples):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = samples
    # Blender 4.0.2 here is built without OpenImageDenoise: with denoising on, every bake pass comes back all zero.
    sc.cycles.use_denoising = False
    sc.render.threads_mode = "FIXED"
    sc.render.threads = THREADS


# @anchor:bpy.assets.bake_worker.bake
def bake(bpy, a):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    high = import_meshes(bpy, a.high)
    low = import_meshes(bpy, a.low)
    if not high or not low:
        fail("high or low has no mesh objects")
    low_ob = join_low(bpy, low)
    unwrap(bpy, low_ob, a.res, a.margin_px)
    tex = bake_material(bpy, low_ob)
    mats = high_materials(bpy, high)
    setup_cycles(bpy, a.samples)
    select_only(bpy, high + [low_ob], low_ob)
    cage_m = a.cage_extrusion_um / UM_PER_M
    report = {"res": a.res, "high_objects": len(high), "low_objects": len(low), "maps": []}
    for name, kind, space in MAPS:
        img = bpy.data.images.new("bvqa_" + name, a.res, a.res, alpha=False)
        img.generated_color = MAGENTA
        img.colorspace_settings.name = space
        tex.image = img
        undo = [rewire_emit(m, name) for m in mats] if kind == "EMIT" else []
        try:
            bpy.ops.object.bake(type=kind, use_selected_to_active=True, cage_extrusion=cage_m, max_ray_distance=0.0,
                                margin=a.margin_px, margin_type="EXTEND", normal_space="TANGENT", use_clear=False,
                                target="IMAGE_TEXTURES")
        finally:
            for u in undo:
                u()
        img.filepath_raw = os.path.join(a.out_dir, name + ".png")
        img.file_format = "PNG"
        img.save()
        report["maps"].append(name)
    select_only(bpy, [low_ob], low_ob)
    try:
        bpy.ops.export_scene.gltf(filepath=os.path.join(a.out_dir, "low_uv.glb"), export_format="GLB",
                                  use_selection=True, export_materials="NONE", export_animations=False)
    except TypeError:
        bpy.ops.export_scene.gltf(filepath=os.path.join(a.out_dir, "low_uv.glb"), export_format="GLB",
                                  use_selection=True, export_materials="PLACEHOLDER", export_animations=False)
    report["low_tris"] = sum(len(p.vertices) - 2 for p in low_ob.data.polygons)
    report["uv_layers"] = len(low_ob.data.uv_layers)
    return report


def main():
    import bpy

    a = parse_args(sys.argv)
    require_ram_out(a.out_dir)
    os.makedirs(a.out_dir, exist_ok=True)
    try:
        report = bake(bpy, a)
    except SystemExit:
        raise
    except Exception as exc:  # any bpy failure is a worker error (rc 2), never a silent pass
        fail("%s: %s" % (type(exc).__name__, exc))
    print("BVQA_BAKE " + json.dumps(report, sort_keys=True, separators=(",", ":")))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
