"""Blender-only remesh on private scratch; host measures exported bytes.

No mesh coordinates are authored here. Blender's OpenVDB remesher derives the
surface from the supplied generated asset. No decimation or island culling.
"""
import hashlib
import json
from pathlib import Path
import sys


def main():
    import bpy

    plan = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
    source = Path(plan["source"])
    if hashlib.sha256(source.read_bytes()).hexdigest() != plan["source_sha256"]:
        return 4
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.import_scene.gltf(filepath=str(source), merge_vertices=True)
    objects = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if len(objects) != 1:
        return 3
    obj = objects[0]
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    modifier = obj.modifiers.new("AF_REMESH", "REMESH")
    if plan["method"] == "voxel":
        if not bpy.app.build_options.openvdb:
            return 3
        modifier.mode = "VOXEL"
        modifier.voxel_size = plan["voxel_size_um"] / 1000000
        modifier.adaptivity = 0.0
    elif plan["method"] == "octree":
        modifier.mode = "SMOOTH"
        modifier.octree_depth = plan["octree_depth"]
        modifier.scale = 0.9
        modifier.use_smooth_shade = False
    else:
        return 2
    modifier.use_remove_disconnected = False
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    if not obj.data.polygons:
        return 3
    obj.data.calc_loop_triangles()
    if len(obj.data.loop_triangles) > plan["max_output_triangles"]:
        return 6
    # Export geometry only: host refuses textured/UV-bearing source assets.
    result = bpy.ops.export_scene.gltf(
        filepath=plan["candidate"], export_format="GLB", use_selection=True,
        export_texcoords=False, export_normals=False, export_materials="NONE",
        export_animations=False)
    return 0 if result == {"FINISHED"} else 2


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as exc:
        print("AF_REMESH_ERROR", type(exc).__name__)
        rc = 2
    print("AF_REMESH_RC", rc, flush=True)
    # Blender's --python-exit-code maps all nonzero Python exceptions to 2;
    # the host reads this marker to retain the stage's precise rc.
    if rc:
        raise RuntimeError("AF_REMESH_RC %d" % rc)
