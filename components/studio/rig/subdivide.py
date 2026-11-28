# SPDX-License-Identifier: AGPL-3.0-only
"""Blender background entry point for linear, zero-smoothing subdivision."""
import math
from pathlib import Path
import sys

import bmesh
import bpy


def surface_stats(mesh):
    mesh.calc_loop_triangles()
    return len(mesh.loop_triangles), math.fsum(triangle.area for triangle in mesh.loop_triangles)


def main():
    source, out, limit, target1, target2 = sys.argv[sys.argv.index("--") + 1:]
    source, out = Path(source), Path(out)
    limit, target1, target2 = map(int, (limit, target1, target2))
    if not bpy.app.background:
        raise RuntimeError("blender_background_required")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source))
    obj, = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    base = obj.data
    triangles, area = surface_stats(base)
    if triangles >= limit or triangles != len(base.polygons):
        raise ValueError("expected_subthreshold_triangular_studio_mesh")

    for variant, target, all_edges in (("V1", target1, False), ("V2", target2, True)):
        mesh = base.copy()
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.edges.index_update()
        ordered = sorted(bm.edges, key=lambda edge: (-edge.calc_length(), edge.index))
        if not all_edges:
            required = target - triangles
            increment = 0
            for count, edge in enumerate(ordered, 1):
                increment += len(edge.link_faces)
                if increment >= required:
                    break
            else:
                raise ValueError("insufficient_edges_for_v1")
            ordered = ordered[:count]
        bmesh.ops.subdivide_edges(bm, edges=ordered, cuts=1, smooth=0.0, use_grid_fill=True,
                                 quad_corner_type="STRAIGHT_CUT")
        bmesh.ops.triangulate(bm, faces=list(bm.faces))
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()
        observed, new_area = surface_stats(mesh)
        if observed < target or not math.isclose(new_area, area, rel_tol=1e-5):
            raise ValueError(f"{variant}_subdivision_geometry_gate: {observed}, {new_area}, {area}")
        obj.data = mesh
        result = bpy.ops.export_scene.gltf(
            filepath=str(out / f"{variant}.glb"), export_format="GLB",
            export_normals=False, export_texcoords=False, export_materials="NONE",
            export_animations=False, export_skins=False, export_yup=True)
        if result != {"FINISHED"}:
            raise RuntimeError(f"{variant}_export_failed")
        print(f"{variant} triangles={observed} split_edges={len(ordered)} "
              f"area={new_area:.10g} baseline_area={area:.10g}", flush=True)


if __name__ == "__main__":
    main()
