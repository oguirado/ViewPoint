"""Render-sheet worker. Run INSIDE Blender:
  blender -b --factory-startup -noaudio --python bpy_side/assets/sheet_worker.py -- \
      --glb G --out DIR --views P0:persp:0:15000,...,OT:ortho:0:90000 --passes beauty,clay,unlit_albedo,normal_ws,mask \
      --res 384 --spp 64 --fov-mdeg 40000 --fit-num 6 --fit-den 5 --ortho-permille 1100 [--threads N] [--scale-x-permille 1100]
Writes DIR/<view>_<pass>.png (RGB8) and prints one line `BVQA_SHEET {json}` (integers only).
Camera: the orbit_worker rule (D = fit*R/tan(FOV/2) from the visible-mesh AABB centre; glTF azimuth 0 = +Z toward +X,
Blender (x, y, z) = (x, -z, y)); ortho views use ortho_scale = 2R*ortho_permille/1000. One world-fixed sun (size-invariant).
Passes: beauty (asset materials, lit) | clay (grey diffuse override, lit) | unlit_albedo (base colour as emission, unlit)
| normal_ws (glTF world normal *0.5+0.5, Raw) | mask (white on black, Raw). unlit_albedo edits materials, so it runs last.
rc 0 ok; rc 2 + `BVQA_SHEET_ERROR {json}` on bad input.
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import q  # noqa: E402

MDEG = 1000.0
PERMILLE = 1000.0
PASS_ORDER = ("beauty", "clay", "normal_ws", "mask", "unlit_albedo")
HIDE_SUFFIX = ("-col", "-convcol", "_collision")
EL_CLAMP_MDEG = 89900


RAMWS_ROOT = os.environ.get("VQA_RAMWS_ROOT", "/dev/shm/vqa-ramws")


def require_ram_out(path):
    """STORAGE LAW L2: frames/maps/scratch may only be written under the ramws root (tmpfs); anything else exits 5."""
    real = os.path.realpath(path)
    if not (real + os.sep).startswith(os.path.realpath(RAMWS_ROOT) + os.sep):
        sys.stderr.write("E_RAM_NOT_TMPFS: %s is outside %s\n" % (path, RAMWS_ROOT))
        sys.exit(5)


def parse_args(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="sheet_worker.py")
    p.add_argument("--glb", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--views", required=True, help="comma list id:persp|ortho:az_mdeg:el_mdeg")
    p.add_argument("--passes", required=True)
    p.add_argument("--res", type=int, required=True)
    p.add_argument("--spp", type=int, required=True)
    p.add_argument("--fov-mdeg", type=int, required=True)
    p.add_argument("--fit-num", type=int, default=6)
    p.add_argument("--fit-den", type=int, default=5)
    p.add_argument("--ortho-permille", type=int, default=1100)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--scale-x-permille", type=int, default=0)
    return p.parse_args(argv)


def fail(code, msg):
    print("BVQA_SHEET_ERROR " + json.dumps({"code": code, "error": msg}, sort_keys=True))
    sys.stdout.flush()
    raise SystemExit(2)


def parse_views(spec):
    views = []
    for item in spec.split(","):
        parts = item.split(":")
        if len(parts) != 4 or parts[1] not in ("persp", "ortho"):
            fail("E_USAGE", "bad view %r" % item)
        views.append({"id": parts[0], "kind": parts[1], "az_mdeg": int(parts[2]), "el_mdeg": int(parts[3])})
    return views


def direction(az_mdeg, el_mdeg):
    """Unit vector (Blender axes) from the target toward the camera; same convention as orbit_worker.place()."""
    az = math.radians(az_mdeg / MDEG)
    el = math.radians(max(-EL_CLAMP_MDEG, min(EL_CLAMP_MDEG, el_mdeg)) / MDEG)
    return (math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el))


def main():
    import bpy
    import mathutils
    a = parse_args(sys.argv)
    require_ram_out(a.out)
    views = parse_views(a.views)
    passes = [p for p in a.passes.split(",") if p]
    if not os.path.isfile(a.glb):
        fail("E_IO", "glb missing")
    if any(p not in PASS_ORDER for p in passes):
        fail("E_USAGE", "unknown pass in %s" % passes)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        bpy.ops.import_scene.gltf(filepath=a.glb)
    except Exception as exc:  # importer raises RuntimeError on garbage input
        fail("E_SCHEMA", str(exc)[:200])
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    lo, hi, meshes, tris = [math.inf] * 3, [-math.inf] * 3, [], 0
    for ob in scene.objects:
        if ob.type != "MESH":
            continue
        if ob.name.endswith(HIDE_SUFFIX):
            ob.hide_render = True
            continue
        if ob.hide_render:
            continue
        meshes.append(ob.name)
        ob.data.calc_loop_triangles()
        tris += len(ob.data.loop_triangles)
        for c in ob.bound_box:
            w = ob.matrix_world @ mathutils.Vector(c)
            for k in range(3):
                lo[k], hi[k] = min(lo[k], w[k]), max(hi[k], w[k])
    if not meshes:
        fail("E_SCHEMA", "no visible mesh")
    centre = mathutils.Vector([(lo[k] + hi[k]) / 2.0 for k in range(3)])
    radius = max((mathutils.Vector(hi) - mathutils.Vector(lo)).length / 2.0, 1e-6)
    if a.scale_x_permille:
        for o in scene.objects:
            if o.parent is None and o.type in ("MESH", "EMPTY"):
                o.scale.x *= a.scale_x_permille / PERMILLE
    fov = math.radians(a.fov_mdeg / MDEG)
    dist = a.fit_num * radius / (a.fit_den * math.tan(fov / 2.0))

    cam_data = bpy.data.cameras.new("sheet_cam")
    cam_data.angle = fov
    cam_data.clip_start = max(dist - 2.0 * radius, 1e-3)
    cam_data.clip_end = dist + 4.0 * radius
    cam = bpy.data.objects.new("sheet_cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    cam.rotation_mode = "QUATERNION"
    sun_data = bpy.data.lights.new("sheet_sun", "SUN")
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("sheet_sun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(50), 0.0, math.radians(30))
    world = bpy.data.worlds.new("sheet_world")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    scene.world = world

    r = scene.render
    r.engine = "CYCLES"
    r.resolution_x = r.resolution_y = a.res
    r.resolution_percentage = 100
    r.film_transparent = False
    r.threads_mode = "FIXED"
    r.threads = max(1, a.threads)
    r.use_persistent_data = False
    r.use_stamp = False
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.image_settings.color_depth = "8"
    r.image_settings.compression = 15
    cy = scene.cycles
    cy.device = "CPU"
    cy.samples = a.spp
    cy.seed = 0
    cy.use_animated_seed = False
    cy.use_denoising = False
    cy.use_adaptive_sampling = False

    def emission(name, colour=None):
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        nt = m.node_tree
        for n in list(nt.nodes):
            nt.nodes.remove(n)
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        em = nt.nodes.new("ShaderNodeEmission")
        em.inputs[1].default_value = 1.0
        if colour is not None:
            em.inputs[0].default_value = colour
        nt.links.new(em.outputs[0], out.inputs[0])
        return m, nt, em

    clay = bpy.data.materials.new("sheet_clay")
    clay.use_nodes = True
    bsdf = clay.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.8, 0.8, 0.8, 1.0)
    bsdf.inputs["Roughness"].default_value = 1.0
    mask, _, _ = emission("sheet_mask", (1.0, 1.0, 1.0, 1.0))
    normal, nnt, nem = emission("sheet_normal")
    geo = nnt.nodes.new("ShaderNodeNewGeometry")
    sep = nnt.nodes.new("ShaderNodeSeparateXYZ")
    neg = nnt.nodes.new("ShaderNodeMath")
    neg.operation = "MULTIPLY"
    neg.inputs[1].default_value = -1.0
    comb = nnt.nodes.new("ShaderNodeCombineXYZ")
    mad = nnt.nodes.new("ShaderNodeVectorMath")
    mad.operation = "MULTIPLY_ADD"
    mad.inputs[1].default_value = (0.5, 0.5, 0.5)
    mad.inputs[2].default_value = (0.5, 0.5, 0.5)
    nnt.links.new(geo.outputs["Normal"], sep.inputs[0])
    nnt.links.new(sep.outputs[0], comb.inputs[0])
    nnt.links.new(sep.outputs[2], comb.inputs[1])
    nnt.links.new(sep.outputs[1], neg.inputs[0])
    nnt.links.new(neg.outputs[0], comb.inputs[2])
    nnt.links.new(comb.outputs[0], mad.inputs[0])
    nnt.links.new(mad.outputs[0], nem.inputs[0])
    own = {clay.name, mask.name, normal.name}

    def unlit_in_place():
        done = 0
        for m in list(bpy.data.materials):
            if m.name in own or not m.use_nodes:
                continue
            nt = m.node_tree
            outs = [n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"]
            prs = [n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"]
            if not outs or not prs:
                continue
            em = nt.nodes.new("ShaderNodeEmission")
            em.inputs[1].default_value = 1.0
            bc = prs[0].inputs["Base Color"]
            if bc.is_linked:
                nt.links.new(bc.links[0].from_socket, em.inputs[0])
            else:
                em.inputs[0].default_value = tuple(bc.default_value)
            for o in outs:
                nt.links.new(em.outputs[0], o.inputs[0])
            done += 1
        return done

    override = {"beauty": None, "clay": clay, "normal_ws": normal, "mask": mask, "unlit_albedo": None}
    os.makedirs(a.out, exist_ok=True)
    vl = bpy.context.view_layer
    cams, written, unlit = {}, [], 0
    for p in sorted(passes, key=PASS_ORDER.index):
        if p == "unlit_albedo":
            unlit = unlit_in_place()
        vl.material_override = override[p]
        sun.hide_render = p not in ("beauty", "clay")
        grey = 0.5 if p in ("beauty", "clay") else 0.0
        bg.inputs[0].default_value = (grey, grey, grey, 1.0)
        bg.inputs[1].default_value = 1.0
        scene.view_settings.view_transform = "Raw" if p in ("normal_ws", "mask") else "Standard"
        for v in views:
            d = mathutils.Vector(direction(v["az_mdeg"], v["el_mdeg"]))
            if v["kind"] == "ortho":
                cam_data.type = "ORTHO"
                cam_data.ortho_scale = 2.0 * radius * a.ortho_permille / PERMILLE
            else:
                cam_data.type = "PERSP"
            cam.location = centre + d * dist
            cam.rotation_quaternion = (-d).to_track_quat("-Z", "Y")
            vl.update()
            cams[v["id"]] = [q.um(cam.location[k]) for k in range(3)]
            name = "%s_%s" % (v["id"], p)
            r.filepath = os.path.join(a.out, name + ".png")
            bpy.ops.render.render(write_still=True)
            written.append(name + ".png")
    print("BVQA_SHEET " + json.dumps({
        "files": written, "blender": bpy.app.version_string, "meshes": sorted(meshes), "tris": tris,
        "centre_um": [q.um(v) for v in centre], "radius_um": q.um(radius), "distance_um": q.um(dist),
        "camera_um": cams, "unlit_materials": unlit}, sort_keys=True))


if __name__ == "__main__":
    main()
