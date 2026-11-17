"""Blender-only reference projection; host re-verifies geometry and map coverage.

New projection shaders use bpy APIs; GLB/PNG parsing and UV coverage remain in
the sibling component. No model, no scene construction, no hand-built geometry.
Visibility is sampled at face centroids, not a per-texel truth claim.
"""
import argparse
from array import array
from collections import deque
import json
import math
from pathlib import Path
import resource
import sys
import time


def material(bpy, source, lo, hi, front, mirrored):
    m = bpy.data.materials.new("inferred_back" if mirrored else "projected_front")
    m.use_nodes = True
    n, links = m.node_tree.nodes, m.node_tree.links
    n.clear()
    geo = n.new("ShaderNodeNewGeometry")
    sep = n.new("ShaderNodeSeparateXYZ")
    links.new(geo.outputs["Position"], sep.inputs[0])
    coords = n.new("ShaderNodeCombineXYZ")
    for component, channel in ((0, "X"), (2, "Y")):
        mul, add = n.new("ShaderNodeMath"), n.new("ShaderNodeMath")
        mul.operation, add.operation = "MULTIPLY", "ADD"
        flip = component == 0 and ((front == "+Y") != mirrored)
        scale = (-1 if flip else 1) / (hi[component] - lo[component])
        mul.inputs[1].default_value = scale
        add.inputs[1].default_value = (1 if flip else 0) - lo[component] * scale
        links.new(sep.outputs[component], mul.inputs[0])
        links.new(mul.outputs[0], add.inputs[0])
        links.new(add.outputs[0], coords.inputs[channel])
    tex = n.new("ShaderNodeTexImage")
    tex.image, tex.extension, tex.interpolation = source, "EXTEND", "Linear"
    links.new(coords.outputs[0], tex.inputs["Vector"])
    emit, output = n.new("ShaderNodeEmission"), n.new("ShaderNodeOutputMaterial")
    links.new(tex.outputs["Color"], emit.inputs["Color"])
    links.new(emit.outputs[0], output.inputs["Surface"])
    return m, emit


def main():
    import bpy
    import bmesh
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    p = argparse.ArgumentParser()
    for flag in ("mesh", "reference", "out", "front"):
        p.add_argument("--" + flag, required=True)
    p.add_argument("--resolution", type=int, required=True)
    p.add_argument("--margin-px", type=int, required=True)
    a = p.parse_args(sys.argv[sys.argv.index("--") + 1:])
    started = time.monotonic()
    out = Path(a.out)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.import_scene.gltf(filepath=a.mesh)
    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objs or any(o.modifiers for o in objs):
        raise ValueError("static nonempty mesh required")
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    obj = bpy.context.object
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    mesh = obj.data
    if not mesh.polygons or len(mesh.polygons) > 1000000:
        raise ValueError("mesh polygon budget")
    lo = [min(v.co[i] for v in mesh.vertices) for i in range(3)]
    hi = [max(v.co[i] for v in mesh.vertices) for i in range(3)]
    if hi[0] <= lo[0] or hi[2] <= lo[2]:
        raise ValueError("projection has zero width or height")
    if any(not math.isfinite(c) for v in mesh.vertices for c in v.co):
        raise ValueError("nonfinite mesh")
    # Raw GLBs may duplicate every triangle corner. Exact-position welding
    # restores atlas adjacency without moving a position or removing a face.
    # The host still requires the full oriented world-triangle multiset.
    vertices_before = len(mesh.vertices)
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=0.0)
    bm.to_mesh(mesh)
    bm.free()
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    # New atlas only; preserve every input position and triangle.
    bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=a.margin_px / a.resolution,
                             margin_method="FRACTION", scale_to_bounds=True)
    bpy.ops.object.mode_set(mode="OBJECT")
    source = bpy.data.images.load(a.reference, check_existing=False)
    source.colorspace_settings.name = "sRGB"
    if source.size[0] * source.size[1] > 4096 ** 2:
        raise ValueError("reference pixel budget")
    mesh.materials.clear()
    materials = [material(bpy, source, lo, hi, a.front, mirrored) for mirrored in (False, True)]
    for m, _ in materials:
        mesh.materials.append(m)
    view = Vector((0, -1 if a.front == "-Y" else 1, 0))
    tree = BVHTree.FromPolygons([v.co for v in mesh.vertices], [tuple(p.vertices) for p in mesh.polygons])
    distance = (Vector(hi) - Vector(lo)).length * 2
    counts = [0, 0]
    for poly in mesh.polygons:
        # Classifies centroid visibility. Entire face inherits this sampled label.
        _, _, hit, _ = tree.ray_cast(poly.center + view * distance, -view, distance * 2)
        front_visible = hit == poly.index and poly.normal.dot(view) > 0
        poly.material_index = 0 if front_visible else 1
        counts[poly.material_index] += 1
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device, scene.cycles.samples = "CPU", 1
    scene.cycles.use_denoising = False
    bpy.context.view_layer.cycles.use_denoising = False
    scene.render.threads_mode, scene.render.threads = "FIXED", 4
    scene.render.bake.use_clear = False
    scene.render.bake.margin = a.margin_px
    scene.render.bake.margin_type = "EXTEND"
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure, scene.view_settings.gamma = 0, 1
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode, scene.render.image_settings.color_depth = "RGBA", "8"
    scene.render.film_transparent = True
    scene.render.resolution_x = scene.render.resolution_y = a.resolution
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = max(1, (hi[0] - lo[0]) / (hi[2] - lo[2]))
    scene.render.pixel_aspect_y = max(1, (hi[2] - lo[2]) / (hi[0] - lo[0]))
    camera = bpy.data.cameras.new("ProjectionCamera")
    cam_obj = bpy.data.objects.new("ProjectionCamera", camera)
    scene.collection.objects.link(cam_obj)
    cam_obj.location = (Vector(lo) + Vector(hi)) / 2 + view * distance
    cam_obj.rotation_euler = (-view).to_track_quat("-Z", "Y").to_euler()
    camera.type, camera.ortho_scale = "ORTHO", max(hi[0] - lo[0], hi[2] - lo[2])
    camera.clip_end = distance * 4
    camera.clip_start = max(distance / 100000, 0.000001)
    scene.camera = cam_obj

    def render_front(name):
        scene.render.filepath = str(out / name)
        bpy.ops.render.render(write_still=True)

    render_front("front_projected.png")

    def bake_image(name, colorspace):
        img = bpy.data.images.new(name, width=a.resolution, height=a.resolution, alpha=True)
        img.generated_color = (0, 0, 0, 0)
        img.colorspace_settings.name = colorspace
        for m, _ in materials:
            for node in m.node_tree.nodes:
                node.select = False
            target = m.node_tree.nodes.new("ShaderNodeTexImage")
            target.image, target.select = img, True
            m.node_tree.nodes.active = target
        bpy.ops.object.bake(type="EMIT")
        img.filepath_raw, img.file_format = str(out / (name + ".png")), "PNG"
        img.save()
        return img

    albedo = bake_image("base_color", "sRGB")
    for i, (m, emit) in enumerate(materials):
        for link in list(emit.inputs["Color"].links):
            m.node_tree.links.remove(link)
        emit.inputs["Color"].default_value = (1, 0, 0, 1) if i == 0 else (0, 1, 0, 1)
    provenance = bake_image("provenance", "Non-Color")
    # Deterministic UV-space nearest-neighbour inpainting and gutter extension.
    # Empty pixels never become front evidence: blue means inferred inpainting.
    # Seed order and four-neighbour order are fixed; no external model or RNG.
    colors, labels = array("f", [0]) * (a.resolution ** 2 * 4), array("f", [0]) * (a.resolution ** 2 * 4)
    albedo.pixels.foreach_get(colors)
    provenance.pixels.foreach_get(labels)
    known = bytearray(a.resolution ** 2)
    queue = deque()
    for i in range(a.resolution ** 2):
        if colors[4 * i + 3] >= 0.99 and labels[4 * i + 3] >= 0.99:
            known[i] = 1
            queue.append(i)
    if not queue:
        raise ValueError("no bake samples for deterministic inpainting")
    inpainted = 0
    while queue:
        i = queue.popleft()
        x, y = i % a.resolution, i // a.resolution
        for j in (i - 1 if x else -1, i + 1 if x + 1 < a.resolution else -1,
                  i - a.resolution if y else -1, i + a.resolution if y + 1 < a.resolution else -1):
            if j < 0 or known[j]:
                continue
            known[j] = 1
            colors[j * 4:j * 4 + 4] = colors[i * 4:i * 4 + 4]
            labels[j * 4:j * 4 + 4] = array("f", (0, 0, 1, 1))
            inpainted += 1
            queue.append(j)
    albedo.pixels.foreach_set(colors)
    provenance.pixels.foreach_set(labels)
    albedo.save()
    provenance.save()
    final = bpy.data.materials.new("ProjectedReferenceBaseColor")
    final.use_nodes = True
    bsdf = final.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Metallic"].default_value = 0
    bsdf.inputs["Roughness"].default_value = 1
    tex = final.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image, tex.extension, tex.interpolation = albedo, "EXTEND", "Linear"
    final.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    for poly in mesh.polygons:
        poly.material_index = 0
    mesh.materials.clear()
    mesh.materials.append(final)
    bpy.ops.export_scene.gltf(filepath=str(out / "textured.glb"), export_format="GLB",
                               use_selection=True, export_yup=True, export_animations=False,
                               export_normals=True, export_texcoords=True, export_materials="EXPORT")
    # Observe reconstruction through the baked atlas under the same unlit camera.
    emit = final.node_tree.nodes.new("ShaderNodeEmission")
    final.node_tree.links.new(tex.outputs["Color"], emit.inputs["Color"])
    final.node_tree.links.new(emit.outputs[0], final.node_tree.nodes.get("Material Output").inputs["Surface"])
    render_front("front_baked.png")
    rec = dict(rc=0, blender_version=bpy.app.version_string, device="CPU", triangles=len(mesh.loop_triangles),
               front_centroid_faces=counts[0], inferred_faces=counts[1],
               visibility_method="face-centroid-BVH-ray", color_space="sRGB-base-color",
               vertices_before_exact_weld=vertices_before, vertices_after_exact_weld=len(mesh.vertices),
               backfill="horizontal-reference-mirror-plus-UV-nearest-inpaint", delighting="NOT_PERFORMED",
               inpainted_atlas_pixels=inpainted,
               worker_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
               wall_ms=int((time.monotonic() - started) * 1000))
    (out / "worker.json").write_text(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
