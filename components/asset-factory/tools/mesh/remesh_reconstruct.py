"""Optional local reconstruction workers; no installation.

The host validates static geometry before this worker, and independently gates
the resulting GLB. Reconstruction is proposal generation, never acceptance.
"""
import hashlib
import json
import math
from pathlib import Path
import sys


def selective_closure(base, matrix, plan, structure):
    """Finite morphological proposals; keep only locally positive Euler changes.

    Digital Euler values select proposals, not acceptance. Marching-cubes
    topology and both independent geometry audits remain mandatory.
    """
    import numpy as np
    from scipy import ndimage
    from skimage.measure import euler_number
    from trimesh.voxel import ops
    history = []
    target = plan["topology_expectation"]["genus_by_primitive"][plan["mesh_name"]]
    components = plan["policy"]["component_allowlist"][plan["mesh_name"]]
    for iteration in range(plan["closing_iterations"]+1, plan["selective_closing_max"]+1):
        proposal = ndimage.binary_fill_holes(ndimage.binary_closing(
            base, structure=structure, iterations=iteration))
        labels, count = ndimage.label(proposal & ~matrix)
        if count > plan["selective_patch_limit"]:
            return 6, None, history
        kept = []
        for index, region in enumerate(ndimage.find_objects(labels), 1):
            roi = tuple(slice(max(0,s.start-2), min(matrix.shape[k],s.stop+2))
                        for k,s in enumerate(region))
            before = matrix[roi]
            after = before | (labels[roi] == index)
            change = int(euler_number(after, connectivity=1))-int(euler_number(before, connectivity=1))
            if change > 0:
                matrix[roi] = after
                kept.append({"id":index, "cells":int(np.count_nonzero(labels[roi] == index)),
                             "digital_euler_delta":change})
        mesh = ops.matrix_to_marching_cubes(matrix, pitch=1.0)
        euler = int(mesh.euler_number)
        history.append({"iteration":iteration, "patches_considered":int(count),
                        "patches_kept":kept, "surface_euler":euler})
        if mesh.is_watertight and 2*components-euler == 2*target:
            return 0, mesh, history
    return 5, None, history


def preserve_recesses(source_surface, solid, pitch, diagonal):
    """Retain buried source sheets as recesses connected to the exterior.

    Proposal constants are [TARGET], not acceptance bounds: seed depth 14000
    ppm, dilation/clearance two cells, closing three cells, at most 64 pockets.
    Both directional audits and exact topology still decide acceptance.
    """
    import numpy as np
    from scipy import ndimage
    from scipy.spatial import cKDTree
    structure = np.ones((3,3,3),dtype=bool)
    depth = ndimage.distance_transform_edt(solid)
    seed = source_surface & (depth > diagonal * 14000 / 1000000 / pitch)
    carve = ndimage.binary_dilation(seed,structure=structure,iterations=2)
    carve &= ndimage.binary_erosion(solid,structure=structure,iterations=2)
    carve = ndimage.binary_closing(carve,structure=structure,iterations=3)
    carve = ndimage.binary_fill_holes(carve)
    for _ in range(3):
        for axis in range(3):
            for plane in np.moveaxis(carve,axis,0):
                plane[:] = ndimage.binary_fill_holes(plane)
    labels,count = ndimage.label(carve)
    if count > 64:
        return 6, None, None
    surface = np.argwhere(solid & ~ndimage.binary_erosion(solid))
    if not len(surface):
        return 3, None, None
    tree = cKDTree(surface)
    channel = np.zeros_like(solid)
    for index,region in enumerate(ndimage.find_objects(labels),1):
        local = np.argwhere(labels[region] == index)
        local += np.array([s.start for s in region])
        distances,nearest = tree.query(local)
        chosen = int(np.argmin(distances))
        start,end = local[chosen],surface[nearest[chosen]]
        steps = int(np.max(np.abs(end-start)))+1
        line = np.round(np.linspace(start,end,steps)).astype(int)
        channel[tuple(line.T)] = True
    channel = ndimage.binary_dilation(channel,iterations=1)
    stats = {"seed_cells":int(np.count_nonzero(seed)), "pockets":int(count),
             "channel_cells":int(np.count_nonzero(channel)), "seed_depth_ppm":14000}
    return 0, solid & ~(carve | channel), stats


def main(plan):
    try:
        import trimesh
    except ImportError:
        return 3
    source = Path(plan["source"])
    if hashlib.sha256(source.read_bytes()).hexdigest() != plan["source_sha256"]:
        return 4
    mesh = trimesh.load(str(source), force="mesh", process=False)
    if not len(mesh.faces):
        return 3
    if plan["method"] == "voxel_grid":
        import numpy as np
        from scipy import ndimage
        from trimesh.voxel import ops
        pitch = plan["voxel_size_um"] / 1000000
        padding = max(3, plan["closing_iterations"] + 1, plan.get("selective_closing_max",0)+1)
        dims = [math.ceil(float(x) / pitch) + 2 * padding + 3 for x in mesh.extents]
        if math.prod(dims) > plan["max_grid_cells"]:
            return 6
        voxels = mesh.voxelized(pitch=pitch, method="subdivide", max_iter=10)
        matrix = np.pad(voxels.matrix, padding)
        if matrix.size > plan["max_grid_cells"]:
            return 6
        # Explicit one-cell morphological closure, then fill enclosed voids.
        # No largest-component selection: every source island participates.
        base = matrix.copy() if plan.get("selective_closing_max",0) else None
        structure = np.ones((3, 3, 3), dtype=bool) if plan.get("closing_shape", "cross") == "cube" else None
        matrix = ndimage.binary_closing(matrix, structure=structure, iterations=plan["closing_iterations"])
        matrix = ndimage.binary_fill_holes(matrix)
        axes = plan.get("fill_axes", [0, 1, 2] if plan.get("fill_cross_sections", False) else [])
        if axes:
            # Explicit bounded proposal to close residual tunnels. Each plane
            # uses SciPy's hole filler; shape preservation is measured later.
            # This may close intended openings and therefore cannot bypass
            # either directional distance or any silhouette assertion.
            for _ in range(3):
                previous_count = int(np.count_nonzero(matrix))
                for axis in axes:
                    planes = np.moveaxis(matrix, axis, 0)
                    for plane in planes:
                        plane[:] = ndimage.binary_fill_holes(plane)
                if int(np.count_nonzero(matrix)) == previous_count:
                    break
        history = []
        if plan.get("selective_closing_max",0):
            rc, out, history = selective_closure(base,matrix,plan,structure)
            if rc:
                return rc
        else:
            out = ops.matrix_to_marching_cubes(matrix, pitch=1.0)
        recesses = None
        if plan.get("preserve_recesses",False):
            rc,matrix,recesses = preserve_recesses(base,matrix,pitch,float(np.linalg.norm(mesh.extents)))
            if rc:
                return rc
            out = ops.matrix_to_marching_cubes(matrix,pitch=1.0)
        out.apply_translation([-padding, -padding, -padding])
        out.apply_transform(voxels.transform)
    else:
        return 2
    if not len(out.faces):
        return 3
    if len(out.faces) > plan["max_output_triangles"]:
        return 6
    scene = trimesh.Scene()
    scene.add_geometry(out, node_name=plan["mesh_name"], geom_name=plan["mesh_name"])
    Path(plan["candidate"]).write_bytes(scene.export(file_type="glb"))
    if plan.get("reconstruction_trace"):
        trace={"schema":"af.remesh-reconstruction/v1", "rc":0,
               "input_sha256":plan["source_sha256"],
               "candidate_sha256":hashlib.sha256(Path(plan["candidate"]).read_bytes()).hexdigest(),
               "selective_trials":history, "recesses":recesses, "triangles":len(out.faces)}
        Path(plan["reconstruction_trace"]).write_text(json.dumps(trace,sort_keys=True,separators=(",",":"))+"\n")
    return 0


if __name__ == "__main__":
    try:
        rc = main(json.loads(Path(sys.argv[1]).read_text()))
    except Exception as exc:
        print("AF_REMESH_ERROR", type(exc).__name__, str(exc)[:240])
        rc = 2
    print("AF_REMESH_RC", rc, flush=True)
    raise SystemExit(rc)
