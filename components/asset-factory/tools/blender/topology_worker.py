"""Blender-only scratch worker. Host independently verifies every output.

Run using blender -b --factory-startup --python-exit-code 2 --python FILE
-- PLAN.json. No host package is imported and no caller path is published.
"""
import json
import sys
from pathlib import Path


def main():
    import bpy
    import bmesh

    plan = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.context.scene.render.engine = "CYCLES"
    bpy.context.scene.cycles.device = "CPU"
    # glTF splits coincident vertices at attribute seams. Reconstruct Blender
    # connectivity using the importer's explicit merge option; never move points.
    bpy.ops.import_scene.gltf(filepath=plan["source"], merge_vertices=True)
    objects = sorted((o for o in bpy.context.scene.objects if o.type == "MESH"), key=lambda o: o.name)
    if len(objects) != 1:
        print("AF_TOPOLOGY_CAPABILITY single_mesh_required")
        return 3
    obj = objects[0]
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    # Reconstruct exact-coordinate connectivity, including normal seams that
    # glTF's attribute-aware importer cannot merge. No distinct point is moved.
    # remove_doubles(dist=0) does not merge on this Blender build; use an exact
    # target map with weld_verts instead. Corner UVs remain per face loop.
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        unique, targets = {}, {}
        for vertex in bm.verts:
            key = tuple(vertex.co)
            if key in unique:
                targets[vertex] = unique[key]
            else:
                unique[key] = vertex
        bmesh.ops.weld_verts(bm, targetmap=targets)
        bm.to_mesh(obj.data)
        obj.data.update()
    finally:
        bm.free()
    if plan["method"] == "quadriflow":
        props = bpy.ops.object.quadriflow_remesh.get_rna_type().properties.keys()
        if not {"target_faces", "seed", "mode"}.issubset(props):
            print("AF_TOPOLOGY_CAPABILITY quadriflow_unavailable")
            return 3
        result = bpy.ops.object.quadriflow_remesh(mode="FACES", target_faces=plan["target_faces"],
                                                seed=plan["seed"], use_preserve_sharp=False,
                                                use_preserve_boundary=True)
        if result != {"FINISHED"}:
            print("AF_TOPOLOGY_SOLVER_FAILED quadriflow_not_finished")
            return 5
    elif plan["method"] == "decimate":
        obj.data.calc_loop_triangles()
        count = len(obj.data.loop_triangles)
        if not count:
            return 3
        modifier = obj.modifiers.new("AF_TOPOLOGY", "DECIMATE")
        modifier.decimate_type = "COLLAPSE"
        modifier.ratio = plan["target_faces"] / count
        modifier.use_collapse_triangulate = True
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    else:
        return 2
    result = bpy.ops.export_scene.gltf(filepath=plan["candidate"], export_format="GLB",
                                      use_selection=True, export_texcoords=True,
                                      export_materials="EXPORT")
    return 0 if result == {"FINISHED"} else 2


if __name__ == "__main__":
    code = main()
    print("AF_TOPOLOGY_RC", code)
    if code:
        raise RuntimeError("AF_TOPOLOGY_RC %s" % code)
