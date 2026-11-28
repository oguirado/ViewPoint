# SPDX-License-Identifier: AGPL-3.0-only
"""rigkit: Blender-side primitives for the pose oracle.

Everything here runs inside ``blender -b --factory-startup --disable-autoexec``.
No renderer, no GPU, no EGL: visibility is decided by CPU ray casting through
``mathutils.bvhtree``.  Coordinates are the armature object space of the
reopened GLB, which the importer reports as Blender world (Z up, metres).
"""
import json

import numpy as np
import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

# Anatomical basis as columns: forward (-Y), left (+X), up (+Z) in the rest frame.
ANAT = np.array([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
I3 = np.eye(3)
I4 = np.eye(4)


class RigError(Exception):
    """Refusal that is about the input rig, not about a verdict."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


# ----------------------------------------------------------------- rotations
def quat_to_mat(q):
    x, y, z, w = np.asarray(q, dtype=np.float64) / np.linalg.norm(q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def mat_to_quat(m):
    m = np.asarray(m, dtype=np.float64)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = np.sqrt(trace + 1.0) * 2
        q = [(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s]
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = [0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s]
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = [(m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s]
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = [(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s]
    q = np.array(q)
    if q[3] < 0:
        q = -q
    return q / np.linalg.norm(q)


def rot_angle_deg(a, b):
    """Angle between two rotation matrices, degrees."""
    c = (np.trace(a.T @ b) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def np_mat(matrix):
    return np.array([[float(matrix[r][c]) for c in range(4)] for r in range(4)], dtype=np.float64)


def kabsch(x, y):
    """Rotation R and offset t minimising |R x + t - y| (both (n, 3))."""
    cx, cy = x.mean(0), y.mean(0)
    u, _, vt = np.linalg.svd((x - cx).T @ (y - cy))
    d = np.sign(np.linalg.det(vt.T @ u.T))
    r = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    return r, cy - r @ cx


# ----------------------------------------------------------------- import
class Rig:
    """A reopened GLB reduced to one skeleton and a set of rigid parts."""


def rigid_carrier(obj, arm):
    """Follow the asset's native bone attachment or its Copy Transforms carrier."""
    while obj and obj != arm:
        if obj.parent == arm and obj.parent_type == "BONE":
            return obj.parent_bone
        constraints = [c for c in obj.constraints
                       if c.type == "COPY_TRANSFORMS" and c.target == arm and c.subtarget
                       and not c.mute and c.influence == 1]
        if len(constraints) == 1:
            return constraints[0].subtarget
        if constraints:
            raise RigError("ambiguous_bone_carrier", obj.name)
        obj = obj.parent
    return None


def _mesh_arrays(obj, arm):
    me = obj.data
    me.calc_loop_triangles()
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    world = np_mat(obj.matrix_world)
    rest = co @ world[:3, :3].T + world[:3, 3]
    tris = np.array([tuple(t.vertices) for t in me.loop_triangles], dtype=np.int64).reshape(-1, 3)
    face_mat = np.array([t.material_index for t in me.loop_triangles], dtype=np.int64)
    mod = [m for m in obj.modifiers if m.type == "ARMATURE" and m.object == arm]
    n = len(me.vertices)
    names = [g.name for g in obj.vertex_groups]
    bone = np.full(n, -1, dtype=np.int64)
    slack = np.ones(n)
    if mod:
        for v in me.vertices:
            total, best = 0.0, (-1, 0.0)
            for g in v.groups:
                total += g.weight
                if g.weight > best[1]:
                    best = (g.group, g.weight)
            if best[0] >= 0:
                bone[v.index] = best[0]
                slack[v.index] = total - best[1]
    else:
        carrier = rigid_carrier(obj, arm)
        if carrier is None or carrier not in arm.data.bones:
            raise RigError("unrigged_mesh", obj.name)
        index = names.index(carrier) if carrier in names else -1
        if index < 0:
            index = len(names)
            names.append(carrier)
        bone[:] = index
        slack[:] = 0.0
    return rest, tris, face_mat, bone, slack, [m.name if m else "" for m in me.materials], names


def import_rig(path):
    bpy.ops.wm.read_homefile(app_template="", use_empty=True)
    bpy.ops.import_scene.gltf(filepath=path)
    helper = set()
    for coll in bpy.data.collections:
        if coll.name.startswith("glTF_not_exported"):
            helper |= {o.name for o in coll.all_objects}
    objects = [o for o in bpy.data.objects if o.name not in helper]
    return collect_rig(path, objects, helper)


def open_authored(path):
    """Open the native candidate and select only its declared robot assembly."""
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    metadata = json.loads(scene["studio_rig"] if "studio_rig" in scene else scene["sn_b_joint"])
    collection = bpy.data.collections[metadata["collection"]]
    active = [o for o in collection.all_objects if o.type == "MESH" and not o.hide_render]
    arms = [o for o in scene.objects if o.type == "ARMATURE"]
    if len(arms) != 1:
        raise RigError("armature_roster", str(len(arms)))
    arm = arms[0]
    arm.animation_data_clear()
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    rig = collect_rig(path, [arm, *sorted(active, key=lambda obj: obj.name)], [])
    arm.data.pose_position = "POSE"
    bpy.context.view_layer.update()
    rig.assembly_metadata = metadata
    return rig


def nonmanifold_edges(obj):
    mesh = obj.data
    counts = np.zeros(len(mesh.edges), dtype=np.int32)
    for polygon in mesh.polygons:
        for i in polygon.loop_indices:
            counts[mesh.loops[i].edge_index] += 1
    return int(np.count_nonzero(counts != 2))


def collect_rig(path, objects, helper):
    arms = [o for o in objects if o.type == "ARMATURE"]
    if len(arms) != 1:
        raise RigError("armature_roster", f"{len(arms)}")
    arm = arms[0]
    if np.abs(np_mat(arm.matrix_world) - I4).max() > 1e-6:
        raise RigError("armature_transform_unexpected", arm.name)
    rig = Rig()
    rig.path = path
    rig.arm = arm
    rig.helper_objects = sorted(helper)
    rig.other_objects = sorted(o.name for o in objects if o.type not in ("ARMATURE", "MESH"))
    rig.bones = [b.name for b in arm.data.bones]
    rig.parent = {b.name: (b.parent.name if b.parent else None) for b in arm.data.bones}
    rig.rest = {b.name: np_mat(b.matrix_local) for b in arm.data.bones}
    rig.head = {b.name: np.array(b.head_local, dtype=np.float64) for b in arm.data.bones}
    rig.order = []
    seen = set()

    def visit(name):
        if name in seen:
            return
        parent = rig.parent[name]
        if parent is not None:
            if parent not in rig.parent:
                raise RigError("orphan_bone", name)
            visit(parent)
        seen.add(name)
        rig.order.append(name)

    for name in rig.bones:
        visit(name)
    rig.index = {name: i for i, name in enumerate(rig.order)}
    rig.bone_flags = {b.name: {"connected": b.use_connect, "inherit_rotation": b.use_inherit_rotation,
                               "inherit_scale": b.inherit_scale} for b in arm.data.bones}
    rig.constraints = sum(len(pb.constraints) for pb in arm.pose.bones)

    rest, tris, face_mat, bone, slack, materials = [], [], [], [], [], []
    mesh_of, local_index = [], []
    vbone = []
    n_v = n_t = 0
    rig.meshes = []
    for obj in objects:
        if obj.type != "MESH":
            continue
        r, t, fm, b, s, mats, names = _mesh_arrays(obj, arm)
        lookup = {i: rig.index[name] for i, name in enumerate(names) if name in rig.index}
        rest.append(r)
        tris.append(t + n_v)
        face_mat.append(fm + len(materials) if mats else np.full(len(fm), -1, dtype=np.int64))
        vbone.append(np.array([lookup.get(i, -1) for i in b], dtype=np.int64))
        slack.append(s)
        materials.extend(mats)
        mesh_of.append(np.full(len(r), len(rig.meshes), dtype=np.int64))
        local_index.append(np.arange(len(r), dtype=np.int64))
        rig.meshes.append(obj)
        n_v += len(r)
        n_t += len(t)
    if not rig.meshes:
        raise RigError("skinned_mesh_missing")
    rig.rest_v = np.vstack(rest)
    rig.tris = np.vstack(tris)
    rig.face_mat = np.concatenate(face_mat)
    rig.vbone = np.concatenate(vbone)
    rig.vslack = np.concatenate(slack)
    rig.mesh_of = np.concatenate(mesh_of)
    rig.local_index = np.concatenate(local_index)
    rig.materials = materials
    rig.n_vertices, rig.n_triangles = len(rig.rest_v), len(rig.tris)
    blended = int(((rig.vbone < 0) | (rig.vslack > 1e-4)).sum())
    if blended:
        raise RigError("skin_not_rigid", f"{blended} vertices without a single unit-weight joint")
    rig.fbone = rig.vbone[rig.tris[:, 0]]
    mixed = int(((rig.vbone[rig.tris[:, 0]] != rig.vbone[rig.tris[:, 1]]) |
                 (rig.vbone[rig.tris[:, 0]] != rig.vbone[rig.tris[:, 2]])).sum())
    if mixed:
        raise RigError("part_ambiguous", f"{mixed} triangles span two joints")
    rig.part_names = [rig.order[i] for i in sorted(set(rig.fbone.tolist())) if i >= 0]
    rig.part_of_tri = np.array([rig.order[i] if i >= 0 else "" for i in rig.fbone])
    rig.tris_of = {b: np.flatnonzero(rig.part_of_tri == b) for b in rig.part_names}
    rig.verts_of = {b: np.flatnonzero(rig.vbone == rig.index[b]) for b in rig.part_names}
    rig.material_names = sorted({m for m in materials if m})
    return rig


# ----------------------------------------------------------------- posing
def targets(rig, command):
    """Pose matrices in armature space for a D-format command.

    ``command`` maps bone name -> {"q": anatomical quaternion (fwd, left, up
    components) of the joint's rest-relative rotation, "t": optional pivot
    offset in armature axes (metres, applied to the root only)}.  A bone
    without a command keeps its rest relation to its parent.
    """
    pose_r, pose_h, matrix, basis, delta_of = {}, {}, {}, {}, {}
    for name in rig.order:
        parent = rig.parent[name]
        local = rig.rest[name]
        rest_r = local[:3, :3]
        if name in command:
            delta = ANAT @ quat_to_mat(command[name]["q"]) @ ANAT.T
        elif parent is None:
            delta = I3
        else:
            delta = delta_of[parent]
        delta_of[name] = delta
        pose_r[name] = delta @ rest_r
        if parent is None:
            offset = np.asarray(command.get(name, {}).get("t", [0.0, 0.0, 0.0]), dtype=np.float64)
            pose_h[name] = rig.head[name] + offset
        else:
            pose_h[name] = pose_h[parent] + delta_of[parent] @ (rig.head[name] - rig.head[parent])
        matrix[name] = I4.copy()
        matrix[name][:3, :3] = pose_r[name]
        matrix[name][:3, 3] = pose_h[name]
        if parent is None:
            basis[name] = np.linalg.inv(local) @ matrix[name]
        else:
            basis[name] = np.linalg.inv(matrix[parent] @ np.linalg.inv(rig.rest[parent]) @ local) @ matrix[name]
    return matrix, basis


def apply(rig, command):
    """Apply a D-format command and return the FK read-back error in metres."""
    matrix, basis = targets(rig, command)
    for pb in rig.arm.pose.bones:
        pb.matrix_basis = Matrix([row.tolist() for row in basis[pb.name]])
    bpy.context.view_layer.update()
    worst = 0.0
    for pb in rig.arm.pose.bones:
        worst = max(worst, float(np.abs(np_mat(pb.matrix) - matrix[pb.name]).max()))
    return matrix, worst


def evaluated(rig):
    deps = bpy.context.evaluated_depsgraph_get()
    out = np.empty_like(rig.rest_v)
    for k, obj in enumerate(rig.meshes):
        ev = obj.evaluated_get(deps)
        me = ev.to_mesh()
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        world = np_mat(ev.matrix_world)
        out[rig.mesh_of == k] = co.reshape(-1, 3) @ world[:3, :3].T + world[:3, 3]
        ev.to_mesh_clear()
    return out


def rigid_state(rig, posed, matrix=None):
    """Per-part rigid transforms fitted to Blender's own evaluation."""
    state = {}
    worst_resid, worst_fk = 0.0, 0.0
    for part in rig.part_names:
        idx = rig.verts_of[part]
        r, t = kabsch(rig.rest_v[idx], posed[idx])
        fitted = rig.rest_v[idx] @ r.T + t
        resid = float(np.linalg.norm(fitted - posed[idx], axis=1).max())
        state[part] = (r, t)
        worst_resid = max(worst_resid, resid)
        if matrix is not None:
            predicted = matrix[part] @ np.linalg.inv(rig.rest[part])
            fk = float(np.linalg.norm(rig.rest_v[idx] @ predicted[:3, :3].T + predicted[:3, 3] - fitted, axis=1).max())
            worst_fk = max(worst_fk, fk)
    posed_rigid = np.empty_like(rig.rest_v)
    for part in rig.part_names:
        idx = rig.verts_of[part]
        r, t = state[part]
        posed_rigid[idx] = rig.rest_v[idx] @ r.T + t
    return state, posed_rigid, worst_resid, worst_fk


# ----------------------------------------------------------------- geometry
def tree_of(verts, tris):
    return BVHTree.FromPolygons(verts.tolist(), tris.tolist(), all_triangles=True, epsilon=0.0)


def cast(tree, origin, dirs, max_distance=1e6):
    """First hit per ray: face index (-1 miss), distance, geometric normal."""
    o = Vector([float(x) for x in origin])
    vecs = [Vector((float(d[0]), float(d[1]), float(d[2]))) for d in dirs]
    face = np.full(len(vecs), -1, dtype=np.int64)
    dist = np.zeros(len(vecs))
    normal = np.zeros((len(vecs), 3))
    for i, d in enumerate(vecs):
        loc, nrm, index, dd = tree.ray_cast(o, d, max_distance)
        if loc is not None:
            face[i] = index
            dist[i] = dd
            normal[i] = (nrm[0], nrm[1], nrm[2])
    return face, dist, normal


def nearest(tree, points):
    """Nearest surface point per query: face, location, geometric normal."""
    locs = np.zeros((len(points), 3))
    normals = np.zeros((len(points), 3))
    faces = np.full(len(points), -1, dtype=np.int64)
    for i, p in enumerate(points):
        loc, nrm, index, _ = tree.find_nearest(Vector((float(p[0]), float(p[1]), float(p[2]))))
        if loc is not None:
            locs[i] = (loc[0], loc[1], loc[2])
            normals[i] = (nrm[0], nrm[1], nrm[2])
            faces[i] = index
    return faces, locs, normals


def boundary_edges(rig, tri_ids):
    """Edges used by exactly one triangle of a part (its open rims)."""
    e = np.concatenate([rig.tris[tri_ids][:, [0, 1]], rig.tris[tri_ids][:, [1, 2]], rig.tris[tri_ids][:, [2, 0]]])
    e = np.sort(e, axis=1)
    key = e[:, 0].astype(np.int64) * len(rig.rest_v) + e[:, 1]
    uniq, count = np.unique(key, return_counts=True)
    rim = uniq[count == 1]
    return np.column_stack([rim // len(rig.rest_v), rim % len(rig.rest_v)])


# Vertices within this distance of the other triangle's plane count as on it: a cut
# shell's shared rims are duplicated to a few micrometres, far below any real crossing.
CONTACT_PLANE_TOL = 1e-5


def _edge_distance(point, tri):
    """In-plane distance from a point to the nearest edge of a triangle."""
    best = np.inf
    for i, j in ((0, 1), (1, 2), (2, 0)):
        a, b = tri[i], tri[j]
        ab = b - a
        t = 0.0 if ab @ ab < 1e-30 else float(np.clip((point - a) @ ab / (ab @ ab), 0.0, 1.0))
        best = min(best, float(np.linalg.norm(point - (a + ab * t))))
    return best


def tri_tri(ta, tb, plane_tol=CONTACT_PLANE_TOL):
    """Classify one triangle pair.

    Returns (kind, depth_m).  kind is one of none, coplanar, contact, cross.
    For a crossing, depth_m is the half-width of the overlap band: the
    in-plane distance from the middle of the intersection segment to the
    nearest edge of either triangle.  It is 0 for a mere contact.
    """
    na = np.cross(ta[1] - ta[0], ta[2] - ta[0])
    nb = np.cross(tb[1] - tb[0], tb[2] - tb[0])
    la, lb = np.linalg.norm(na), np.linalg.norm(nb)
    if la < 1e-16 or lb < 1e-16:
        return "none", 0.0
    na, nb = na / la, nb / lb
    sa = nb @ (ta - tb[0]).T
    sb = na @ (tb - ta[0]).T
    if (sa > plane_tol).all() or (sa < -plane_tol).all() or (sb > plane_tol).all() or (sb < -plane_tol).all():
        return "none", 0.0
    if np.abs(sa).max() <= plane_tol and np.abs(sb).max() <= plane_tol:
        return "coplanar", 0.0
    direction = np.cross(na, nb)
    direction /= np.linalg.norm(direction)

    def interval(tri, signed):
        pts = []
        for i, j in ((0, 1), (1, 2), (2, 0)):
            if (signed[i] > plane_tol and signed[j] < -plane_tol) or (signed[i] < -plane_tol and signed[j] > plane_tol):
                pts.append(tri[i] + (tri[j] - tri[i]) * signed[i] / (signed[i] - signed[j]))
        pts.extend(tri[i] for i in range(3) if abs(signed[i]) <= plane_tol)
        if len(pts) < 2:
            return None
        t = np.array([p @ direction for p in pts])
        return t.min(), t.max()

    ia, ib = interval(ta, sa), interval(tb, sb)
    if ia is None or ib is None:
        return "contact", 0.0
    lo, hi = max(ia[0], ib[0]), min(ia[1], ib[1])
    if hi - lo <= 1e-7:
        return "contact", 0.0
    ca, cb = float(na @ ta[0]), float(nb @ tb[0])  # plane constants: na.x = ca, nb.x = cb
    c = float(na @ nb)
    base = ((ca - cb * c) * na + (cb - ca * c) * nb) / (1.0 - c * c)
    middle = base + direction * (0.5 * (lo + hi))
    depth = min(_edge_distance(middle, ta), _edge_distance(middle, tb))
    return ("cross" if depth > 0.0 else "contact"), depth


def tri_tri_batch(va, vb, plane_tol=CONTACT_PLANE_TOL):
    """Vectorised rejection followed by an exact test for the survivors."""
    a0, a1, a2 = va[:, 0], va[:, 1], va[:, 2]
    b0, b1, b2 = vb[:, 0], vb[:, 1], vb[:, 2]
    na = np.cross(a1 - a0, a2 - a0)
    nb = np.cross(b1 - b0, b2 - b0)
    la = np.linalg.norm(na, axis=1)
    lb = np.linalg.norm(nb, axis=1)
    ok = (la > 1e-16) & (lb > 1e-16)
    na = np.divide(na, la[:, None], out=np.zeros_like(na), where=la[:, None] > 0)
    nb = np.divide(nb, lb[:, None], out=np.zeros_like(nb), where=lb[:, None] > 0)
    sa = np.einsum("ij,ikj->ik", nb, va - b0[:, None, :])
    sb = np.einsum("ij,ikj->ik", na, vb - a0[:, None, :])
    reject = ((sa > plane_tol).all(1) | (sa < -plane_tol).all(1) |
              (sb > plane_tol).all(1) | (sb < -plane_tol).all(1) | ~ok)
    out_kind = np.full(len(va), "none", dtype=object)
    out_depth = np.zeros(len(va))
    for k in np.flatnonzero(~reject):
        out_kind[k], out_depth[k] = tri_tri(va[k], vb[k], plane_tol)
    return out_kind, out_depth


# ----------------------------------------------------------------- cameras
def camera_basis(camera, target):
    """Camera origin and orthonormal basis (forward, right, up) looking at target."""
    az, el = np.radians(camera["az_deg"]), np.radians(camera["el_deg"])
    # Azimuth is measured from the character's facing direction (-Y), towards its left (+X).
    facing = np.array([0.0, -1.0, 0.0])
    left = np.array([1.0, 0.0, 0.0])
    horizontal = np.cos(az) * facing + np.sin(az) * left
    view_dir_to_camera = np.cos(el) * horizontal + np.sin(el) * np.array([0.0, 0.0, 1.0])
    origin = target + camera["distance_m"] * view_dir_to_camera
    forward = -view_dir_to_camera
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    return origin, forward, right, up


def rays_from_basis(origin, forward, right, up, lens, sensor, resolution, shift=(0.0, 0.0)):
    """Pinhole rays for every pixel centre, row-major from the top-left."""
    width, height = resolution
    forward, right, up = map(np.asarray, (forward, right, up))
    sensor_h = sensor * height / width
    xs = ((np.arange(width) + 0.5) / width - 0.5) * sensor + shift[0] * sensor
    ys = (0.5 - (np.arange(height) + 0.5) / height) * sensor_h + shift[1] * sensor_h
    grid_x, grid_y = np.meshgrid(xs, ys)
    dirs = (forward[None, :] * lens + right[None, :] * grid_x.reshape(-1, 1)
            + up[None, :] * grid_y.reshape(-1, 1))
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    return dirs, sensor / lens / width * 1000.0


def camera_rays(camera, target, resolution):
    """Rays and millimetres per pixel (at unit depth times distance) for a camera aimed at a target."""
    origin, forward, right, up = camera_basis(camera, target)
    dirs, mm_per_px = rays_from_basis(origin, forward, right, up, camera["lens_mm"], camera["sensor_width_mm"], resolution)
    return origin, dirs, mm_per_px


def glb_rest_error(file, rest_v):
    """Largest distance between GLB node-space positions and reopened rest vertices.

    The GLB is decoded by the oracle's own reader, so an importer that bakes a
    stale inverse bind into the mesh shows up here.
    """
    positions, _, _, _ = file.skin_arrays(0)
    to_blender = positions[:, [0, 2, 1]] * np.array([1.0, -1.0, 1.0])
    if len(to_blender) == len(rest_v):
        err = float(np.linalg.norm(rest_v - to_blender, axis=1).max())
        if err <= 1e-5:
            return err, "index_aligned"
    from mathutils.kdtree import KDTree

    def furthest(a, b):
        kd = KDTree(len(b))
        for i, p in enumerate(b):
            kd.insert(p, i)
        kd.balance()
        return max(kd.find(p)[2] for p in a)

    return float(max(furthest(to_blender, rest_v), furthest(rest_v, to_blender))), "nearest"


# ----------------------------------------------------------------- seams
def seam_pairs(rig):
    """(parent part, child part) for every part whose nearest ancestor with geometry exists."""
    pairs = []
    for child in rig.part_names:
        parent = rig.parent[child]
        while parent is not None and parent not in rig.verts_of:
            parent = rig.parent[parent]
        if parent is not None:
            pairs.append((parent, child))
    return pairs


def contact_samples(rig, part, other, band):
    """Surface samples of ``part`` that lie within ``band`` of ``other`` at rest.

    Returns rest positions, the nearest rest point on ``other``, the sample
    kind (0 vertex, 1 edge midpoint) and the rest distance.
    """
    tree = tree_of(rig.rest_v, rig.tris[rig.tris_of[other]])
    idx = rig.verts_of[part]
    base = rig.rest_v[idx]
    _, loc, _ = nearest(tree, base)
    dist = np.linalg.norm(loc - base, axis=1)
    keep = dist <= band
    points, nearest_pts, kinds = [base[keep]], [loc[keep]], [np.zeros(int(keep.sum()), dtype=np.int64)]
    distances = [dist[keep]]
    rim = boundary_edges(rig, rig.tris_of[part])
    if len(rim) and keep.any:
        inside = np.zeros(len(rig.rest_v), dtype=bool)
        inside[idx[keep]] = True
        edges = rim[inside[rim[:, 0]] & inside[rim[:, 1]]]
        if len(edges):
            mid = rig.rest_v[edges[:, 0]] * 0.5 + rig.rest_v[edges[:, 1]] * 0.5
            _, loc_mid, _ = nearest(tree, mid)
            dist_mid = np.linalg.norm(loc_mid - mid, axis=1)
            ok = dist_mid <= band
            points.append(mid[ok])
            nearest_pts.append(loc_mid[ok])
            kinds.append(np.ones(int(ok.sum()), dtype=np.int64))
            distances.append(dist_mid[ok])
    return np.vstack(points), np.vstack(nearest_pts), np.concatenate(kinds), np.concatenate(distances)


def transform_points(state, part, points):
    r, t = state[part]
    return points @ r.T + t


def camera_rays_matrix(matrix, lens, sensor_w, sensor_h, sensor_fit, shift_x, shift_y, resolution):
    """Pinhole rays through a Blender camera (world matrix), pixel centres, row-major."""
    width, height = resolution
    matrix = np.asarray(matrix, dtype=np.float64)
    origin = matrix[:3, 3]
    right, up, forward = matrix[:3, 0], matrix[:3, 1], -matrix[:3, 2]
    if sensor_fit == "AUTO":
        sensor_fit = "HORIZONTAL" if width >= height else "VERTICAL"
    if sensor_fit == "VERTICAL":
        sensor_w = sensor_h * width / height
    else:
        sensor_h = sensor_w * height / width
    xs = ((np.arange(width) + 0.5) / width - 0.5) * sensor_w + shift_x * sensor_w
    ys = (0.5 - (np.arange(height) + 0.5) / height) * sensor_h + shift_y * sensor_h
    grid_x, grid_y = np.meshgrid(xs, ys)
    dirs = forward[None, :] * lens + right[None, :] * grid_x.reshape(-1, 1) + up[None, :] * grid_y.reshape(-1, 1)
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    return origin, dirs, sensor_w / lens / width * 1000.0
