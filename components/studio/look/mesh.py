# SPDX-License-Identifier: AGPL-3.0-only
"""Generate the studio topology using installed Blender operations."""
import argparse
import json
from pathlib import Path
import sys

import bmesh
import bpy
import numpy as np
from mathutils.bvhtree import BVHTree


ROOT = Path(__file__).resolve().parent
TARGET = 32000


def checkpoint(phase):
    line = next(x for x in Path("/proc/meminfo").read_text().splitlines()
                if x.startswith("MemAvailable:"))
    free = int(line.split()[1]) * 1024
    print(f"RAM checkpoint {phase}: {free}", flush=True)
    if free < 20 * 1024 ** 3:
        print(json.dumps({"reason": "memory_checkpoint", "retry_when": "MemAvailable is at least 20 GiB",
                          "next_command": "rerun the admitted Blender phase"}), flush=True)
        raise RuntimeError("memory checkpoint below 20 GiB")


def clear():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.context.preferences.filepaths.save_version = 0


def import_mesh(path):
    bpy.ops.import_scene.gltf(filepath=str(path), import_pack_images=True)
    objects = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    if len(objects) != 1:
        raise RuntimeError(f"expected one mesh in {path}, found {len(objects)}")
    return objects[0]


def activate(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def apply(obj, name, kind, **settings):
    mod = obj.modifiers.new(name, kind)
    for key, value in settings.items():
        setattr(mod, key, value)
    bpy.ops.object.modifier_apply(modifier=mod.name)


def triangles(obj):
    obj.data.calc_loop_triangles()
    indices = np.empty(len(obj.data.loop_triangles) * 3, dtype=np.int32)
    obj.data.loop_triangles.foreach_get("vertices", indices)
    return indices.reshape(-1, 3)


def points(obj):
    coords = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", coords)
    matrix = np.array(obj.matrix_world)
    return (coords.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]).astype(np.float32)


def weld_exact(bm):
    """Collapse glTF attribute seams using exact coordinate equality."""
    first_by_position = {}
    weld = {}
    for vertex in bm.verts:
        position = tuple(vertex.co)
        first = first_by_position.setdefault(position, vertex)
        if first != vertex:
            weld[vertex] = first
    if weld:
        bmesh.ops.weld_verts(bm, targetmap=weld)


def stats(obj):
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        weld_exact(bm)
        faces = len(bm.faces)
        boundary = sum(e.is_boundary for e in bm.edges)
        nonmanifold = sum(not e.is_manifold for e in bm.edges)
        winding = sum(e.is_manifold and not e.is_contiguous for e in bm.edges)
        bad_verts = sum(not v.is_manifold for v in bm.verts)
        degenerate = sum(f.calc_area() <= 1.0e-14 for f in bm.faces)
        pending = set(bm.verts)
        components = []
        while pending:
            first = pending.pop()
            stack = [first]
            count = 1
            while stack:
                for edge in stack.pop().link_edges:
                    for vertex in edge.verts:
                        if vertex in pending:
                            pending.remove(vertex)
                            stack.append(vertex)
                            count += 1
            components.append(count)
        return {"triangles": len(triangles(obj)), "welded_faces": faces,
                "welded_vertices": len(bm.verts), "boundary_edges": boundary,
                "nonmanifold_edges_including_boundary": nonmanifold,
                "nonmanifold_vertices": bad_verts, "inconsistent_winding_edges": winding,
                "degenerate_faces": degenerate, "component_vertices": sorted(components, reverse=True),
                "signed_volume_source_units_cubed": bm.calc_volume(signed=True)}
    finally:
        bm.free()


def require_topology(topology):
    zeros = ("boundary_edges", "nonmanifold_edges_including_boundary", "nonmanifold_vertices",
             "inconsistent_winding_edges", "degenerate_faces")
    if (any(topology[k] for k in zeros)
            or not 0 < topology["triangles"] <= TARGET
            or topology["welded_faces"] != topology["triangles"]
            or topology["signed_volume_source_units_cubed"] <= 0):
        raise RuntimeError(f"topology gate: {topology}")



def clean_components(bm):
    """Discard only tiny, negligible-volume fragments left by closure and collapse."""
    bm.verts.index_update()
    bm.faces.index_update()
    pending = set(bm.verts)
    drop = []
    kept = []
    for first in bm.verts:
        if first not in pending:
            continue
        pending.remove(first)
        stack = [first]
        region = [first]
        while stack:
            vertex = stack.pop()
            for edge in vertex.link_edges:
                other = edge.other_vert(vertex)
                if other in pending:
                    pending.remove(other)
                    stack.append(other)
                    region.append(other)
        faces = {face for vertex in region for face in vertex.link_faces}
        coords = np.array([list(v.co) for v in region])
        low, high = coords.min(axis=0), coords.max(axis=0)
        # Signed fan-volume about a component vertex avoids world-origin cancellation.
        origin = first.co
        volume = 0.0
        for face in sorted(faces, key=lambda item: item.index):
            vertices = [(vertex.co - origin) for vertex in face.verts]
            start = vertices[0]
            for j in range(1, len(vertices) - 1):
                volume += start.dot(vertices[j].cross(vertices[j + 1])) / 6.0
        if abs(volume) < 1.0e-6 and (len(faces) < 96 or float(np.linalg.norm(high - low)) < 0.008):
            drop.extend(region)
        else:
            kept.append({"vertices": len(region), "faces": len(faces),
                         "signed_volume_source_units_cubed": volume,
                         "bounds_source_units": [low.tolist(), high.tolist()]})
    bmesh.ops.delete(bm, geom=drop, context="VERTS")
    return {"vertices_removed": len(drop),
            "retained": sorted(kept, key=lambda component: component["vertices"], reverse=True)}


def overlaps(obj, weld=False):
    verts = points(obj)
    faces = triangles(obj)
    if weld:
        verts, inverse = np.unique(verts, axis=0, return_inverse=True)
        faces = inverse[faces]
    bvh = BVHTree.FromPolygons(verts.tolist(), faces.tolist(), all_triangles=True, epsilon=0.0)
    candidates = bvh.overlap(bvh)
    # Blender tests the triangles here. Ignore connected triangles, whose
    # legitimate common edges and vertices belong to the topology gate.
    pairs = [(a, b) for a, b in candidates if a < b and not (set(faces[a]) & set(faces[b]))]
    return faces, pairs



def smooth_intersections(obj):
    faces, pairs = overlaps(obj)
    counts = [len(pairs)]
    adjacency = [[] for _ in obj.data.vertices]
    for edge in obj.data.edges:
        a, b = edge.vertices
        adjacency[a].append(b)
        adjacency[b].append(a)
    for _ in range(14):
        if not pairs:
            return counts
        original = points(obj)
        best = original
        winning = pairs
        involved = set(faces[np.asarray(pairs).ravel()].ravel().tolist())
        for weights in ((1.0, 0.55, 0.2), (1.0, 0.7, 0.45, 0.25, 0.1), (1.0,)):
            seen = set(involved)
            rings = [seen.copy()]
            for _ in weights[1:]:
                ring = {n for v in rings[-1] for n in adjacency[v]} - seen
                rings.append(ring)
                seen.update(ring)
            for factor, iterations in ((0.34, 2), (0.55, 6), (0.65, 12)):
                group = obj.vertex_groups.new(name="Local crossing repair")
                group_name = group.name
                for ring, weight in zip(rings, weights):
                    if ring:
                        group.add(sorted(ring), weight, "REPLACE")
                apply(obj, "Local crossing smooth", "SMOOTH", factor=factor,
                      iterations=iterations, vertex_group=group_name)
                group = obj.vertex_groups.get(group_name)
                if group is not None:
                    obj.vertex_groups.remove(group)
                _, after = overlaps(obj)
                if len(after) < len(winning):
                    best, winning = points(obj), after
                obj.data.vertices.foreach_set("co", original.ravel())
                obj.data.update()
                if not winning:
                    break
            if not winning:
                break
        obj.data.vertices.foreach_set("co", best.ravel())
        obj.data.update()
        if len(winning) >= len(pairs):
            break
        pairs = winning
        counts.append(len(pairs))
        print(f"crossing smooth: {counts}", flush=True)
    if pairs:
        reduced = points(obj)[faces[np.asarray(pairs).ravel()].ravel()]
        print(f"Remaining intersection bounds: {reduced.min(axis=0)} / {reduced.max(axis=0)}", flush=True)
        raise RuntimeError(f"nonincident face intersections remain: {counts}")
    return counts


def relax_torso(obj, iterations):
    """Reduce voxel column ridges while protecting appendages and raised trim."""
    xyz = points(obj)
    x, y, z = xyz.T
    def taper(value, low, high):
        t = np.clip((value - low) / (high - low), 0, 1)
        return t * t * (3 - 2 * t)

    emblem_radius = np.hypot(x, z + 0.061)
    trim = np.where(y < -0.125, taper(emblem_radius, 0.105, 0.153), 1)
    region = (taper(z, -0.31, -0.24) * (1 - taper(z, 0.071, 0.145))
              * (1 - taper(np.abs(x), 0.214, 0.264)) * trim)
    group = obj.vertex_groups.new(name="Torso panel relaxation")
    weights = np.rint(region * 32).astype(np.uint8)
    for weight in np.unique(weights[weights > 0]):
        group.add(np.flatnonzero(weights == weight).tolist(), float(weight) / 32, "REPLACE")
    name = group.name
    apply(obj, "Relax torso voxel columns", "SMOOTH", factor=0.65, iterations=iterations,
          vertex_group=name)
    group = obj.vertex_groups.get(name)
    if group is not None:
        obj.vertex_groups.remove(group)
    return {"iterations": iterations, "factor": 0.65, "weighted_vertices": int(np.count_nonzero(weights))}


def generate(source, octree_depth, destination, relaxation, softening, cap, tip_depth, tip_cap, torso_iterations):
    clear()
    obj = import_mesh(source)
    activate(obj)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    obj.name = "Studio Mesh"
    obj.data.materials.clear()
    checkpoint("before_voxel")
    apply(obj, "Octree voxel remesh", "REMESH", mode="BLOCKS", octree_depth=octree_depth,
          scale=0.9, use_remove_disconnected=False, threshold=0.05, use_smooth_shade=True)
    print(f"Before smooth and collapse: {stats(obj)}", flush=True)
    from blockfix import close_block_contacts
    closure = close_block_contacts(obj)
    print(f"Octree closure: {closure}", flush=True)
    apply(obj, "Reduce voxel stair stepping", "SMOOTH", factor=relaxation, iterations=softening)
    torso = relax_torso(obj, torso_iterations) if torso_iterations else None
    original = len(triangles(obj))
    uv_margin = 0.007
    output = {"voxel_method": "installed Remesh modifier, BLOCKS octree dual contouring",
              "octree_depth": octree_depth, "octree_scale": 0.9, "closure": closure,
              "global_smooth_factor": relaxation, "global_smooth_iterations": softening,
              "triangles_after_closure": original, "decimate_ratio": min(1, cap / original)}
    if torso is not None:
        output["torso_panel_relaxation"] = torso
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if original > cap:
        apply(obj, "Quadric collapse", "DECIMATE", ratio=cap / original, use_collapse_triangulate=True)
    apply(obj, "Stable triangle diagonals", "TRIANGULATE")
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        cleanup = clean_components(bm)
        bm.to_mesh(obj.data)
    finally:
        bm.free()
    antenna = None
    if tip_depth:
        from antenna import refine_tips
        reference = import_mesh(source)
        antenna = refine_tips(obj, reference, tip_depth, tip_cap)
        bpy.data.objects.remove(reference, do_unlink=True)
        activate(obj)
    overlap_counts = smooth_intersections(obj)
    for poly in obj.data.polygons:
        poly.use_smooth = True
    result = stats(obj)
    require_topology(result)
    checkpoint("before_unwrap")
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(island_margin=uv_margin, scale_to_bounds=True)
    bpy.ops.object.mode_set(mode="OBJECT")
    output.update(fragment_cleanup=cleanup, nonincident_face_intersection_pairs_by_smoothing_step=overlap_counts,
                  topology=result, uv_island_margin_fraction=uv_margin)
    if antenna is not None:
        output["antenna_refinement"] = antenna
    (destination / "remesh.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output), flush=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(destination / "remesh.blend"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("depth", type=int)
    parser.add_argument("--smooth-factor", type=float, default=0.45)
    parser.add_argument("--smooth-iterations", type=int, default=2)
    parser.add_argument("--cap", type=int, default=31900)
    parser.add_argument("--tip-depth", type=int, default=0)
    parser.add_argument("--tip-cap", type=int, default=1000)
    parser.add_argument("--torso-iterations", type=int, default=0)
    parser.add_argument("--geometry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    sys.path.insert(0, str(ROOT))
    generate(args.geometry, args.depth, args.output, args.smooth_factor, args.smooth_iterations, args.cap,
             args.tip_depth, args.tip_cap, args.torso_iterations)
