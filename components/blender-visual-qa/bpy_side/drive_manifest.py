"""Inspect a scratch blend into bvqa.scene/v1; surface-safe emit_manifest equivalent."""
import argparse
import hashlib
import json
import math
import os
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(__file__))
from drive_common import DriveError, Parser, load_json, assign_indices, hint_name, run, select_scene, warnings, write


def entry():
    import bpy
    p = Parser()
    p.add_argument("--out", required=True)
    p.add_argument("--journal", required=True)
    args = p.parse_args(sys.argv[sys.argv.index("--") + 1:])
    j = load_json(args.journal)
    scene = select_scene(bpy, j)
    objects, materials = assign_indices(bpy)
    from mathutils import Vector
    rows = []
    deps = bpy.context.evaluated_depsgraph_get()
    for obj in objects:
        tags = sorted(set(t for t in str(obj.get("bvqa_tags", "")).split("|") if t))
        evaluated = obj.evaluated_get(deps)
        mesh = evaluated.to_mesh()
        try:
            mesh.calc_loop_triangles()
            triangles = len(mesh.loop_triangles)
            n_verts, n_faces = len(mesh.vertices), len(mesh.polygons)
        finally:
            evaluated.to_mesh_clear()
        corners = [evaluated.matrix_world @ Vector(c) for c in evaluated.bound_box]
        bbox = [[math.floor(fn(c[i] for c in corners)*1000) for i in range(3)] for fn in (min, max)]
        rows.append({"parent": obj.parent.name if obj.parent else None,
                     "collection": sorted(c.name for c in obj.users_collection), "data": obj.data.name,
                     "materials": sorted(m.name for m in obj.data.materials if m),
                     "visible_render": not obj.hide_render, "world_bbox_mm": bbox,
                     "n_verts": n_verts, "n_faces": n_faces, "modifiers": [m.type for m in obj.modifiers],
                     "animated": bool(obj.animation_data), "name": obj.name, "type": obj.type, "pass_index": obj.pass_index,
                     "hint_name": hint_name(obj), "tags": tags, "hide_render": bool(obj.hide_render),
                     "tris": triangles, "loc_mm": [round(v*1000) for v in obj.location],
                     "rot_mdeg": [round(math.degrees(v)*1000) for v in obj.rotation_euler],
                     "scale_ppm": [round(v*1000000) for v in obj.scale]})
    names = {o.name for o in bpy.data.objects}
    mat_names = {m.name for m in materials}
    for item in j["animated"]:
        if item["name"] not in (mat_names if item["kind"] == "material" else names):
            raise DriveError("E_SCHEMA", "animated.name:" + item["name"])
    def file_hash(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1048576), b""):
                h.update(block)
        return h.hexdigest()
    mat_rows = []
    for mat in materials:
        images = {}
        if mat.use_nodes:
            for node in mat.node_tree.nodes:
                image = getattr(node, "image", None)
                if image is not None:
                    path = bpy.path.abspath(image.filepath)
                    images[image.name] = {"name": image.name, "filepath": image.filepath,
                        "packed": bool(image.packed_file), "exists": bool(image.packed_file) or os.path.isfile(path),
                        "w": int(image.size[0]), "h": int(image.size[1])}
        mat_rows.append({"name": mat.name, "pass_index": mat.pass_index, "use_nodes": bool(mat.use_nodes),
                         "images": [images[name] for name in sorted(images)]})
    build_hash = bpy.app.build_hash.decode() if isinstance(bpy.app.build_hash, bytes) else str(bpy.app.build_hash)
    manifest = {"schema": "bvqa.scene/v1", "blend_sha256": file_hash(bpy.data.filepath),
                "blender": {"version": bpy.app.version_string, "build_hash": build_hash,
                            "binary_sha256": file_hash(bpy.app.binary_path)},
                "frame_range": [scene.frame_start, scene.frame_end], "fps": [scene.render.fps, round(scene.render.fps_base)],
                "res": [scene.render.resolution_x, scene.render.resolution_y], "engine": scene.render.engine,
                "view_transform": scene.view_settings.view_transform,
                "cameras": [{"name": o.name, "lens_um": math.floor(o.data.lens*1000),
                             "sensor_um": math.floor(o.data.sensor_width*1000),
                             "clip_mm": [math.floor(o.data.clip_start*1000), math.floor(o.data.clip_end*1000)],
                             "ortho": o.data.type == "ORTHO"} for o in sorted(scene.objects, key=lambda o:o.name) if o.type == "CAMERA"],
                "lights": [{"name": o.name, "type": o.data.type, "energy_mW": math.floor(o.data.energy*1000)}
                           for o in sorted(scene.objects, key=lambda o:o.name) if o.type == "LIGHT"],
                "world": {"name": scene.world.name if scene.world else None, "use_nodes": bool(scene.world and scene.world.use_nodes)}, "scene": scene.name, "camera": scene.camera.name,
                "view_layer": j["view_layer"], "objects": rows,
                "materials": mat_rows,
                "budgets": j.get("budgets", {"tris_object": 200000, "tris_scene": 1000000}),
                "animated": j["animated"], "warnings": warnings(bpy, scene)}
    write(args.out, manifest)
    return {"objects": len(rows)}


if __name__ == "__main__":
    run(entry)
