# SPDX-License-Identifier: AGPL-3.0-only
"""Measure one rig GLB over a list of poses (the oracle proper).

Runs inside ``blender -b --factory-startup --disable-autoexec``.  The GLB is
reopened on its own; nothing from the authoring pipeline is reused.  Every
pose is commanded in D format, applied through Blender's own FK, checked for
read-back, part rigidity and independent FK, then measured for joint gaps,
exposed interior, interpenetration, sole contact and floor contact.
Visibility is decided by CPU ray casting only: no renderer, no GPU.

argv after ``--``: path to the measure spec JSON written by the CLI.
"""
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import numpy as np  # noqa: E402
import bpy  # noqa: E402
from PIL import Image  # noqa: E402
import rigkit as rk  # noqa: E402
from pose_oracle import glb as glbmod  # noqa: E402
from pose_oracle import cameras as cammod  # noqa: E402
from pose_oracle.common import Refusal, checkpoint, effective_scope  # noqa: E402

SCHEMA = "rp4.pose-oracle.measure.v1"
NORMAL, INTERIOR, INTENDED = 0, 1, 2
PALETTE = [(222, 143, 5), (5, 162, 222), (128, 222, 5), (222, 5, 128), (5, 222, 212), (160, 5, 222), (222, 214, 5),
           (92, 92, 222), (5, 222, 92), (222, 92, 5), (200, 200, 200), (120, 60, 30), (60, 160, 120), (170, 120, 170),
           (120, 170, 120), (170, 60, 60), (60, 60, 170), (140, 140, 60), (60, 140, 140), (140, 60, 140)]


def fail(cls, detail, value, tolerance, where=""):
    return {"class": cls, "detail": detail, "value": value, "tolerance": tolerance, "where": where}


def face_normals(triangles):
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    area = np.linalg.norm(cross, axis=1)
    return cross / np.maximum(area[:, None], 1e-30), area


def spherical_surface(vertices, triangles, shape_tol):
    """Fit the evaluated vertices and bound the radial profile of their facets."""
    mean = vertices.mean(axis=0)
    offset = vertices - mean
    fit, _, rank, _ = np.linalg.lstsq(np.column_stack((2 * offset, np.ones(len(offset)))),
                                      np.sum(offset * offset, axis=1), rcond=None)
    center = mean + fit[:3]
    radii = np.linalg.norm(vertices - center, axis=1)
    normals, area = face_normals(triangles)
    residual = float(np.ptp(radii))
    if rank != 4 or not np.isfinite(radii).all() or residual > shape_tol or (area <= 1e-18).any():
        return None
    # Face planes give a lower radial bound even when the perpendicular misses a triangle.
    lower = np.abs(np.einsum("ij,ij->i", normals, triangles[:, 0] - center))
    return center, float(lower.min()), float(radii.max()), residual


def background_neighbours(codes, resolution):
    """Return the eight pixel neighbours of misses bordering visible geometry."""
    width, height = resolution
    padded = np.pad(codes.reshape(height, width), 1, constant_values=-1)
    offsets = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]
    touching = np.zeros((height, width), dtype=bool)
    for dy, dx in offsets:
        touching |= padded[1 + dy:1 + dy + height, 1 + dx:1 + dx + width] >= 0
    yy, xx = np.nonzero(touching & (codes.reshape(height, width) < 0))
    dy, dx = np.array(offsets).T
    ny, nx = yy[:, None] + dy, xx[:, None] + dx
    valid = (ny >= 0) & (ny < height) & (nx >= 0) & (nx < width)
    indices = np.where(valid, ny * width + nx, 0)
    labels = np.where(valid, codes[indices], -1)
    return yy * width + xx, indices, labels


def opposing_labels(labels, first, second):
    """A background pixel lies between the two parts along a pixel axis."""
    return ((labels[:, :4] == first) & (labels[:, 4:][:, ::-1] == second)
            | (labels[:, :4] == second) & (labels[:, 4:][:, ::-1] == first)).any(axis=1)


def hull2d(points):
    """Convex hull (monotone chain) of planar points; empty for fewer than three."""
    pts = sorted({(round(float(p[0]), 9), round(float(p[1]), 9)) for p in points})
    if len(pts) < 3:
        return []

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def margin(point, hull):
    """Signed distance of a point to a convex hull: positive inside."""
    if len(hull) < 3:
        return None
    point = np.asarray(point, dtype=np.float64)
    inside, best = True, np.inf
    for i, a in enumerate(hull):
        a = np.asarray(a)
        b = np.asarray(hull[(i + 1) % len(hull)])
        edge = b - a
        if np.linalg.norm(edge) < 1e-12:
            continue
        signed = float((edge[0] * (point[1] - a[1]) - edge[1] * (point[0] - a[0])) / np.linalg.norm(edge))
        best = min(best, abs(signed))
        inside &= signed >= 0
    return best if inside else -best


class Oracle:
    def __init__(self, spec):
        self.spec = spec
        self.tol = {k: v["value"] for k, v in spec["contract"]["tolerances"].items()}
        self.floor = spec["floor_z_m"]
        self.rest_area = {}
        self.contact_points = {}

    # ------------------------------------------------------------ setup
    def load(self):
        """Identity first: a substituted asset is rejected without being measured."""
        spec, tol = self.spec, self.tol
        path = spec["rig"]
        expected = spec.get("expected") or {}
        failures = []
        # The byte-level pin is decided before anything is parsed: other bytes, other asset.
        data = Path(path).read_bytes()
        asset = {"path": path, "file_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        if expected.get("file_sha256") and expected["file_sha256"] != asset["file_sha256"]:
            failures.append(fail("asset_substituted", f"file sha256 {asset['file_sha256'][:16]} is not the pinned "
                                 f"{expected['file_sha256'][:16]}", asset["file_sha256"], expected["file_sha256"], "asset"))
        native = Path(path).suffix == ".blend"
        if not failures and not native:
            file = glbmod.Glb(path)
            self.file = file
            structure = file.structure_digest()
            bind_error, bind_joints = file.bind_error()
            asset.update({"structure_sha256": structure, "generator": file.doc.get("asset", {}).get("generator"),
                          "materials": file.structure()["materials"], "inverse_bind_m": bind_error,
                          "inverse_bind_joints": bind_joints})
            if expected.get("structure_sha256") and expected["structure_sha256"] != structure:
                failures.append(fail("asset_substituted", f"structure {structure[:16]} is not the pinned "
                                     f"{expected['structure_sha256'][:16]}", structure, expected["structure_sha256"], "asset"))
            if not failures and bind_error > tol["inverse_bind_m"]:
                failures.append(fail("stale_inverse_bind", "inverse-bind matrices disagree with the rest skeleton",
                                     bind_error, tol["inverse_bind_m"], "asset"))
        self.asset, self.asset_failures = asset, failures
        if failures:
            # A substituted or mis-bound asset is rejected on identity and is not measured.
            self.rest, self.rest_failures = None, []
            return
        rig = rk.open_authored(path) if native else rk.import_rig(path)
        self.native = native
        asset["exterior_closure"] = {"status": "AUSENTE",
                                    "reason": "No registered GLB shell topology across exported vertex splits."}
        if native:
            asset.update({"inverse_bind_m": None, "inverse_bind_joints": None,
                          "bind_format": "native Blender bone carrier",
                          "assembly_objects": [obj.name for obj in rig.meshes],
                          "nonmanifold_edges_by_object": {obj.name: rk.nonmanifold_edges(obj) for obj in rig.meshes},
                          "declared_stops_deg": {j["name"]: j.get("swing_hard_stop_deg")
                                                for j in rig.assembly_metadata["joints"]},
                          "declared_soles": rig.assembly_metadata["soles"]})
            nonmanifold = asset["nonmanifold_edges_by_object"]
            asset["exterior_closure"] = {"status": "REJECT" if any(nonmanifold.values()) else "PASS",
                                        "mesh_count": len(nonmanifold), "nonmanifold_edges": sum(nonmanifold.values())}
        self.rig = rig
        self.parts = rig.part_names
        code = {p: i for i, p in enumerate(self.parts)}
        self.tri_code = np.array([code[p] for p in rig.part_of_tri], dtype=np.int64)
        asset.update({"vertices": rig.n_vertices, "triangles": rig.n_triangles, "joints": len(rig.order),
                      "triangles_within_budget": rig.n_triangles <= int(tol["triangle_budget"]),
                       "triangle_budget": {"limit": int(tol["triangle_budget"]),
                                           "status": ("PASS" if rig.n_triangles <= tol["triangle_budget"] else "REJECT")
                                           if spec.get("enforce_budget") else "AUSENTE"},
                      "parts": {p: {"vertices": int(len(rig.verts_of[p])), "triangles": int(len(rig.tris_of[p])),
                                    "boundary_edges": int(len(rk.boundary_edges(rig, rig.tris_of[p])))}
                                for p in self.parts},
                      "constraints": rig.constraints, "helper_objects": rig.helper_objects,
                      "order": rig.order, "parents": rig.parent,
                      "heads": {b: [float(x) for x in rig.head[b]] for b in rig.order}})
        self.asset = asset
        jmap = spec["joint_map"]
        self.bone_of = {s: j["bone"] for s, j in jmap["joints"].items() if j.get("bone")}
        required = [s for s in jmap.get("required_for_replay", []) if not jmap["joints"][s].get("optional")]
        asset["map_unresolved"] = sorted(s for s, b in self.bone_of.items() if b not in rig.index)
        missing = sorted(s for s in required if s not in self.bone_of or self.bone_of[s] not in rig.index)
        asset["map_unresolved_required"] = missing
        if missing:
            raise rk.RigError("joint_map_unresolved", "required joints without a bone in this GLB: " + ",".join(missing))
        self.semantic_of = {b: s for s, b in self.bone_of.items() if b in rig.index}
        unmapped = sorted(p for p in self.parts if p not in self.semantic_of)
        asset["unmapped_parts"] = unmapped
        if unmapped:
            raise rk.RigError("unmapped_part", "bones carrying geometry without a joint-map entry: " + ",".join(unmapped))
        self.attachments = {j["bone"] for j in jmap["joints"].values() if j.get("attachment") and j.get("bone")}
        self.rest_centre = {p: rig.rest_v[rig.verts_of[p]].mean(0) for p in self.parts}
        self.rest_radius = {p: float(np.linalg.norm(rig.rest_v[rig.verts_of[p]] - self.rest_centre[p], axis=1).max())
                            for p in self.parts}
        self.local_tris = {p: np.searchsorted(rig.verts_of[p], rig.tris[rig.tris_of[p]]) for p in self.parts}

        intended = {}
        for source in (spec["contract"].get("intended", {}), jmap.get("intended", {})):
            for key, values in source.items():
                intended[key] = intended.get(key, []) + [v for v in values if v not in intended.get(key, [])]
        self.permitted = {tuple(sorted(self.bone_of.get(n, n) for n in pair))
                          for pair in intended.get("permitted_overlaps", [])}
        interior_mats = [self.bone_of.get(n, n) for n in intended.get("interior_materials", [])]
        visible_mats = [self.bone_of.get(n, n) for n in intended.get("intended_visible_materials", [])]
        self.intended_parts = {self.bone_of.get(n, n) for n in intended.get("intended_interior_parts", [])}
        self.material_class = np.zeros(len(rig.tris), dtype=np.int64)
        for index, name in enumerate(rig.materials):
            kind = NORMAL
            if any(n and n in name for n in interior_mats):
                kind = INTERIOR
            elif any(n and n in name for n in visible_mats):
                kind = INTENDED
            self.material_class[rig.face_mat == index] = kind
        self.intended = {k: v for k, v in intended.items()}

        self.soles = {}
        for side, sole in jmap["soles"].items():
            bone = self.bone_of.get(sole["part"], sole["part"])
            if bone in rig.verts_of:
                idx = rig.verts_of[bone]
                in_patch = np.zeros(len(rig.rest_v), dtype=bool)
                in_patch[idx[rig.rest_v[idx, 2] - self.floor <= sole["band_m"]]] = True
                tris = rig.tris_of[bone][in_patch[rig.tris[rig.tris_of[bone]]].all(1)]
                self.soles[side] = {"bone": bone, "patch": np.flatnonzero(in_patch), "tris": tris,
                                    "ankle": self.bone_of.get(sole["joint"], bone)}

        self.rest_tree = {p: rk.tree_of(rig.rest_v[rig.verts_of[p]], self.local_tris[p]) for p in self.parts}
        self.seams = []
        for parent, child in rk.seam_pairs(rig):
            if child in self.attachments:
                continue
            a_pts, _, _, a_d0 = rk.contact_samples(rig, parent, child, tol["seam_band_m"])
            b_pts, _, _, b_d0 = rk.contact_samples(rig, child, parent, tol["seam_band_m"])
            pivot = rig.head[child]
            pts = np.vstack([a_pts, b_pts])
            near_pivot = float(np.linalg.norm(pts - pivot, axis=1).min()) if len(pts) else 0.0
            radius = max(tol["seam_radius_min_m"], tol["seam_radius_factor"] * near_pivot)
            keep_a = np.linalg.norm(a_pts - pivot, axis=1) <= radius if len(a_pts) else np.zeros(0, bool)
            keep_b = np.linalg.norm(b_pts - pivot, axis=1) <= radius if len(b_pts) else np.zeros(0, bool)
            self.seams.append({"a": parent, "b": child, "codes": (code[parent], code[child]),
                               "a_pts": a_pts[keep_a], "b_pts": b_pts[keep_b],
                               "a_d0": a_d0[keep_a], "b_d0": b_d0[keep_b], "radius": radius,
                               "joint": self.semantic_of.get(child, child),
                               "ignored": int(len(pts) - keep_a.sum() - keep_b.sum())})
        asset["seams"] = [{"joint": s["joint"], "parent": s["a"], "child": s["b"], "region_radius_m": s["radius"],
                           "samples": int(len(s["a_pts"]) + len(s["b_pts"])), "outer_contacts_ignored": s["ignored"]}
                          for s in self.seams]
        asset["intended"] = {k: len(v) for k, v in intended.items()}

        self.groups = []
        for group in spec["cameras"]["groups"]:
            target = group["target"]
            if target.startswith("chain:"):
                semantics = [s for s in jmap.get("chains", {}).get(target.split(":", 1)[1], []) if s in self.bone_of]
                bones = [self.bone_of[s] for s in semantics if self.bone_of[s] in rig.verts_of]
                if bones:
                    self.groups.append({"group": group, "semantics": semantics,
                                        "selector": np.concatenate([rig.verts_of[b] for b in bones])})
            else:
                self.groups.append({"group": group, "semantics": [], "selector": np.arange(len(rig.rest_v))})
        asset["camera_groups"] = [g["group"]["name"] for g in self.groups]
        self.cameras = spec["cameras"]
        self.resolution = tuple(spec["cameras"]["resolution"])
        self.pixel_threshold = max(1, int(np.ceil(
            tol["interior_px_min"] * np.prod(self.resolution) / (960 * 540))))
        self.camera_frames = {}
        self.joints = getattr(rig, "assembly_metadata", {}).get("joints", [])
        self.native_parts = {obj.name: obj for obj in rig.meshes} if self.joints else {}
        mesh_triangles = {}
        if self.joints:
            mesh_indices = rig.mesh_of[rig.tris[:, 0]]
            mesh_triangles = {obj.name: rig.tris[mesh_indices == i] for i, obj in enumerate(rig.meshes)}
        self.bearing_triangles = {
            j["name"]: (mesh_triangles[j["socket"]], mesh_triangles[j["ball"]]) for j in self.joints
        }

    def bearing_fit(self, posed, failures):
        """Measure the actual spherical race and ball at the evaluated pose."""
        metrics = {}
        if not self.joints:
            return metrics
        depsgraph = bpy.context.evaluated_depsgraph_get()
        fit_limit = (self.spec.get("reference") or {}).get("pivot_tolerance_m")
        low_limit, high_limit = self.tol["bearing_clearance_min_m"], self.tol["bearing_clearance_max_m"]
        shape_tol = self.tol["rigidity_m"]
        for joint in self.joints:
            name = joint["name"]
            cup_tri, ball_tri = self.bearing_triangles[name]
            ball_cloud = posed[np.unique(ball_tri)]
            cup_faces = posed[cup_tri]
            cup_normals, _ = face_normals(cup_faces)
            outward = cup_faces.mean(axis=1) - posed[np.unique(cup_tri)].mean(axis=0)
            # Inner race normals point towards the cup interior. Outer faces and lip facets
            # do not. Use the mesh centroid only to classify faces, then fit the race itself.
            inward = np.einsum("ij,ij->i", cup_normals, outward) < -0.65 * np.linalg.norm(outward, axis=1)
            cup_cloud = posed[np.unique(cup_tri[inward])]
            ball_fit = spherical_surface(ball_cloud, posed[ball_tri], shape_tol) if len(ball_cloud) >= 4 else None
            cup_fit = spherical_surface(cup_cloud, cup_faces[inward], shape_tol) if len(cup_cloud) >= 4 else None
            socket, ball = (self.native_parts[joint[key]].evaluated_get(depsgraph)
                            for key in ("socket", "ball"))
            parent, child = rk.np_mat(socket.matrix_world), rk.np_mat(ball.matrix_world)
            origin_offset = float(np.linalg.norm(parent[:3, 3] - child[:3, 3]))
            mouth = parent[:3, :3] @ joint["mouth_axis_local"]
            stem = child[:3, :3] @ joint["stem_axis_local"]
            cosine = float(mouth @ stem / (np.linalg.norm(mouth) * np.linalg.norm(stem)))
            swing = float(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0))))
            limit = joint["swing_hard_stop_deg"]
            row = {"object_origin_offset_m": origin_offset, "swing_deg": swing, "declared_stop_deg": limit,
                   "race_vertices": len(cup_cloud), "ball_vertices": len(ball_cloud)}
            metrics[name] = row
            if cup_fit is None or ball_fit is None:
                row["surface_status"] = "AUSENTE"
                failures.append(fail("bearing_surface_unverified", "evaluated mating surfaces could not be fitted",
                                     None, shape_tol, name))
            else:
                cup_center, cup_min, cup_max, cup_error = cup_fit
                ball_center, ball_min, ball_max, ball_error = ball_fit
                distance = float(np.linalg.norm(cup_center - ball_center))
                lower, upper = cup_min - ball_max - distance, cup_max - ball_min + distance
                clear = lower >= low_limit and upper <= high_limit
                row.update({"surface_status": "PASS" if clear else "REJECT", "center_offset_m": distance,
                            "cup_center_m": cup_center.tolist(), "ball_center_m": ball_center.tolist(),
                            "race_radius_bounds_m": [cup_min, cup_max], "ball_radius_bounds_m": [ball_min, ball_max],
                            "radial_fit_span_m": {"race": cup_error, "ball": ball_error},
                            "clearance_bounds_m": [lower, upper], "clearance_limits_m": [low_limit, high_limit]})
                if fit_limit is not None and distance > fit_limit:
                    failures.append(fail("pivot_shifted", "evaluated ball and race centres diverge",
                                         distance, fit_limit, name))
                if not clear:
                    failures.append(fail("bearing_clearance_out_of_range",
                                         "conservative evaluated race clearance bounds exceed registration",
                                         [lower, upper], [low_limit, high_limit], name))
            if swing > limit + 1e-4:
                failures.append(fail("joint_stop_exceeded", "command passes the declared mechanical stop",
                                     swing, limit, name))
        return metrics

    def reference_checks(self, failures):
        reference = self.spec.get("reference") or {}
        pivots, tips = {}, {}
        for name, point in reference.get("pivots_m", {}).items():
            bone = self.bone_of[name]
            delta = float(np.linalg.norm(self.rig.head[bone] - point))
            pivots[name] = delta
            if delta > reference["pivot_tolerance_m"]:
                failures.append(fail("pivot_shifted", f"{name} moved from the registered pivot", delta,
                                     reference["pivot_tolerance_m"], name))
        for name, item in reference.get("fork_tips", {}).items():
            bone = self.bone_of[item["part"]]
            points = np.asarray(item["points_m"])
            if bone not in self.rest_tree:
                failures.append(fail("fork_tip_damaged", "registered carrier has no geometry", None,
                                     reference["tip_tolerance_m"], name))
                tips[name] = {"points": len(points), "distance_m": None}
                continue
            _, nearest, _ = rk.nearest(self.rest_tree[bone], points)
            distance = float(np.linalg.norm(points - nearest, axis=1).max())
            tips[name] = {"points": len(points), "distance_m": distance}
            if distance > reference["tip_tolerance_m"]:
                failures.append(fail("fork_tip_damaged", "registered tip surface is missing or displaced",
                                     distance, reference["tip_tolerance_m"], name))
        return {"pivot_error_m": pivots, "fork_tips": tips}

    # ------------------------------------------------------------ rest
    def rest_checks(self):
        rig, tol = self.rig, self.tol
        matrix, readback = rk.apply(rig, {})
        posed = rk.evaluated(rig)
        state, rigid, resid, fk = rk.rigid_state(rig, posed, matrix)
        self.rest_rigid = rigid
        self.state, self.rigid = state, rigid
        self.part_tree = {}
        self.global_tree = rk.tree_of(rigid, rig.tris)
        failures = []
        if readback > tol["fk_readback_m"]:
            failures.append(fail("pose_not_reproduced", "rest pose read-back deviates from the commanded rest",
                                 readback, tol["fk_readback_m"], "rest"))
        if resid > tol["rigidity_m"]:
            failures.append(fail("part_deformed", "a part is not rigid at rest", resid, tol["rigidity_m"], "rest"))
        if fk > tol["fk_readback_m"]:
            failures.append(fail("pose_not_reproduced", "independent FK disagrees with Blender at rest", fk,
                                 tol["fk_readback_m"], "rest"))
        glb_err, glb_mode = (rk.glb_rest_error(self.file, rig.rest_v)
                             if hasattr(self, "file") else (None, "native Blender"))
        self.asset["rest_bind_m"], self.asset["rest_bind_mode"] = glb_err, glb_mode
        # float32 data carry about one ulp per metre; never gate below four ulp of the largest coordinate.
        limit = max(tol["rest_bind_m"], 4 * float(np.finfo(np.float32).eps) * float(np.abs(rig.rest_v).max()))
        self.asset["rest_bind_limit_m"] = limit
        if glb_err is not None and glb_err > limit:
            failures.append(fail("stale_inverse_bind", "reopened rest vertices differ from the GLB node-space positions",
                                 glb_err, limit, "rest"))
        self.rest = {"fk_readback_m": readback, "rigidity_m": resid, "fk_independent_m": fk,
                     "eval_vs_data_m": float(np.linalg.norm(posed - rig.rest_v, axis=1).max()),
                     "rest_bind_m": glb_err, "rest_bind_mode": glb_mode,
                     "min_z_m": float(rigid[:, 2].min() - self.floor)}
        self.rest["registered_geometry"] = self.reference_checks(failures)
        self.rest["bearing_fit"] = self.bearing_fit(posed, failures)
        self.rest["soles"] = {side: self.sole_metrics(side, matrix, rigid, None) for side in self.soles}
        self.pose_id = "rest"
        views = self.build_views(rigid)
        self.rest["seams"] = self.seam_gaps(rigid, views, failures, at_rest=True)
        for seam_rec, seam in zip(self.rest["seams"], self.seams):
            lowest = np.inf
            for src, dst in ((seam["a"], seam["b"]), (seam["b"], seam["a"])):
                pts = rig.rest_v[rig.verts_of[src]]
                _, locs, _ = rk.nearest(self.rest_tree[dst], pts)
                lowest = min(lowest, float(np.linalg.norm(locs - pts, axis=1).min()))
            seam_rec["rest_min_distance_m"] = lowest
            if lowest > tol["rest_gap_visible_max_m"]:
                failures.append(fail("rest_seam_open", f"{seam['joint']} is not assembled at rest: {seam['a']} and "
                                     f"{seam['b']} stay {lowest * 1000:.2f} mm apart", lowest,
                                     tol["rest_gap_visible_max_m"], seam["joint"]))
        self.rest["interior"] = self.census(rigid, views, {}, failures)
        self.rest_group_px = self.rest["interior"]["by_group"]
        return failures

    # ------------------------------------------------------------ per pose
    def measure(self, pose):
        checkpoint()
        rig, tol = self.rig, self.tol
        command, skipped = {}, []
        for semantic, entry in pose["joints"].items():
            bone = self.bone_of.get(semantic)
            if bone is None or bone not in rig.index:
                skipped.append(semantic)
                continue
            command[bone] = {k: entry[k] for k in ("q", "t") if k in entry}
        rec = {"pose_id": pose.get("pose_id"), "family": pose.get("family"), "plant": list(pose.get("plant", [])),
               "joints_not_commanded": skipped, "failures": []}
        if not command:
            rec["failures"].append(fail("pose_not_reproduced", "no commanded joint resolves in this rig", 0, 1, "pose"))
            rec["verdict"] = "REJECT"
            return rec
        failures = rec["failures"]
        matrix, readback = rk.apply(rig, command)
        posed = rk.evaluated(rig)
        state, rigid, resid, fk = rk.rigid_state(rig, posed, matrix)
        rec["fk"] = {"readback_m": readback, "rigidity_m": resid, "independent_m": fk}
        rec["bearing_fit"] = self.bearing_fit(posed, failures)
        if readback > tol["fk_readback_m"]:
            failures.append(fail("pose_not_reproduced", "pose read-back deviates from the command", readback,
                                 tol["fk_readback_m"], "pose"))
        if fk > tol["fk_readback_m"]:
            failures.append(fail("pose_not_reproduced", "independent FK disagrees with Blender", fk,
                                 tol["fk_readback_m"], "pose"))
        if resid > tol["rigidity_m"]:
            deviations = {p: float(np.linalg.norm(rig.rest_v[rig.verts_of[p]] @ state[p][0].T + state[p][1]
                                                  - posed[rig.verts_of[p]], axis=1).max()) for p in self.parts}
            worst = max(deviations, key=deviations.get)
            failures.append(fail("part_deformed", f"part {worst} does not move rigidly", resid, tol["rigidity_m"], worst))

        self.state, self.rigid, self.matrix = state, rigid, matrix
        self.pose_id = pose.get("pose_id")
        self.part_tree = {}
        self.global_tree = rk.tree_of(rigid, rig.tris)
        low = int(np.argmin(rigid[:, 2]))
        lowest = next(p for p in self.parts if low in rig.verts_of[p])
        rec["floor"] = {"min_z_m": float(rigid[low, 2] - self.floor), "lowest_part": lowest}
        if rigid[low, 2] - self.floor < -tol["floor_penetration_m"]:
            failures.append(fail("floor_penetration", f"{lowest} is below the floor", rigid[low, 2] - self.floor,
                                 -tol["floor_penetration_m"], lowest))
        planted = list(pose.get("plant", []))
        self.contact_points.clear()
        rec["soles"] = {side: self.sole_metrics(side, matrix, rigid, failures) for side in planted}
        # Informational: is the hips pivot over the area the planted soles actually touch?
        hips = self.bone_of.get("hips")
        if hips in matrix:
            points = [point for patch in self.contact_points.values() for point in patch]
            support = margin(matrix[hips][:2, 3], hull2d(points))
            if support is not None:
                rec["support_margin_m"] = support
        views = self.build_views(rigid)
        rec["gaps"] = self.seam_gaps(rigid, views, failures, at_rest=False)
        rec["collisions"] = self.collisions(failures)
        rec["interior"] = self.census(rigid, views, pose, failures)
        rec["views"] = len(views)
        rec["verdict"] = "REJECT" if failures else "PASS"
        return rec

    def sole_metrics(self, side, matrix, rigid, failures):
        rig, tol = self.rig, self.tol
        sole = self.soles.get(side)
        if sole is None or not len(sole["tris"]):
            if failures is not None:
                failures.append(fail("sole_contact_insufficient", f"no sole patch for {side}", 0.0, "band_m", side))
            return {"error": "sole_patch_missing"}
        foot = rig.verts_of[sole["bone"]]
        clearance = rigid[foot, 2] - self.floor
        tris = rig.tris[sole["tris"]]
        pts = rigid[tris]
        area = np.abs(np.cross(pts[:, 1] - pts[:, 0], pts[:, 2] - pts[:, 0])[:, 2]) * 0.5
        touching = (rigid[tris, 2] - self.floor <= tol["sole_clearance_max_m"]).all(1)
        contact = float(area[touching].sum())
        if side not in self.rest_area:
            rest = rig.rest_v[tris]
            self.rest_area[side] = float(np.abs(np.cross(rest[:, 1] - rest[:, 0], rest[:, 2] - rest[:, 0])[:, 2]).sum() * 0.5)
        contact_pts = pts[touching & (area > 0)].reshape(-1, 3)
        self.contact_points[side] = contact_pts.tolist()
        metrics = {"clearance_min_m": float(clearance.min()), "contact_area_m2": contact,
                   "contact_fraction": contact / self.rest_area[side] if self.rest_area[side] else 0.0,
                   "rest_area_m2": self.rest_area[side], "patch_vertices": int(len(sole["patch"])),
                   "patch_triangles": int(len(sole["tris"]))}
        ankle = matrix[sole["ankle"]][:3, 3]
        if touching.any():
            metrics["support_offset_m"] = float(np.linalg.norm(pts[touching].reshape(-1, 3).mean(0)[:2] - ankle[:2]))
        if failures is not None:
            if clearance.min() > tol["sole_clearance_max_m"]:
                failures.append(fail("sole_not_touching", f"{side} sole is off the floor at plant", float(clearance.min()),
                                     tol["sole_clearance_max_m"], side))
            if metrics["contact_fraction"] < tol["sole_contact_fraction_min"] or contact < tol["sole_contact_area_min_m2"]:
                failures.append(fail("sole_contact_insufficient", f"{side} sole contact area is too small at plant",
                                     metrics["contact_fraction"], tol["sole_contact_fraction_min"], side))
        return metrics

    def tree(self, part):
        if part not in self.part_tree:
            self.part_tree[part] = rk.tree_of(self.rigid[self.rig.verts_of[part]], self.local_tris[part])
        return self.part_tree[part]

    def build_views(self, rigid):
        fixed = self.spec.get("fixed_camera_frames")
        if fixed is not None:
            views = fixed[self.pose_id]
            self.camera_frames[self.pose_id] = views
            return [dict(view) for view in views]
        views = []
        for entry in self.groups:
            group, selector = entry["group"], entry["selector"]
            lo, hi = rigid[selector].min(0), rigid[selector].max(0)
            centre = (lo + hi) / 2
            radius = float(np.linalg.norm(hi - lo) / 2) + 0.02
            for view in group["views"]:
                camera = dict(view, distance_m=cammod.distance_for(self.cameras, radius, view["fill"]),
                              lens_mm=self.cameras["lens_mm"], sensor_width_mm=self.cameras["sensor_width_mm"])
                origin, forward, right, up = rk.camera_basis(camera, centre)
                views.append({"name": f"{group['name']}@{view['az_deg']:.0f}f{view['el_deg']:+.0f}", "group": group["name"],
                              "origin": list(origin), "forward": list(forward), "right": list(right), "up": list(up),
                              "semantics": entry["semantics"], "cast": True,
                              "mm_per_px_at_unit_distance": self.cameras["sensor_width_mm"] / self.cameras["lens_mm"]
                              / self.resolution[0] * 1000.0, "film": False})
        self.camera_frames[self.pose_id] = views
        return views

    def seam_gaps(self, rigid, views, failures, at_rest):
        """Seam opening per joint: visible gap beyond the registered rest state.

        At rest the absolute visible seam is gated by ``rest_gap_visible_max_m``;
        in a pose the opening beyond each sample's own rest distance is gated by
        ``gap_visible_m``.  Only samples on the outward side of the other part
        count; samples behind its surface are interpenetration, not gap.
        """
        tol, state = self.tol, self.state
        origins = np.array([v["origin"] for v in views])
        out = []
        for seam in self.seams:
            rec = {"joint": seam["joint"], "seam": f"{seam['a']}>{seam['b']}", "gap_visible_m": 0.0,
                   "open_visible_m": 0.0, "open_samples": 0, "clearance_samples": 0, "visible_views": 0,
                   "example_views": [], "region_radius_m": seam["radius"]}
            mids, widths, opens = [], [], []
            for src, dst, pts0, d0 in ((seam["a"], seam["b"], seam["a_pts"], seam["a_d0"]),
                                       (seam["b"], seam["a"], seam["b_pts"], seam["b_d0"])):
                if not len(pts0):
                    continue
                pts = rk.transform_points(state, src, pts0)
                _, locs, normals = rk.nearest(self.tree(dst), pts)
                vec = pts - locs
                dist = np.linalg.norm(vec, axis=1)
                outside = np.einsum("ij,ij->i", vec, normals) > 0
                opening = np.where(outside, dist - d0, 0.0)
                keep = outside & (dist > tol["gap_visible_m"]) if at_rest else opening > tol["gap_visible_m"]
                if not at_rest and keep.any():
                    # New free space only counts from both viewpoints: the midpoint of each free
                    # segment must lie behind the other part's rest surface in both rest frames.
                    # A material point that merely turns away from a clearance stays a non-gap.
                    mid = (pts[keep] + locs[keep]) * 0.5
                    r_src, t_src = state[src]
                    r_dst, t_dst = state[dst]
                    in_dst = (mid - t_dst) @ r_dst
                    in_src = (mid - t_src) @ r_src
                    _, a_star, n_a = rk.nearest(self.rest_tree[src], in_dst)
                    _, b_star, n_b = rk.nearest(self.rest_tree[dst], in_src)
                    new_free = ((np.einsum("ij,ij->i", in_dst - a_star, n_a) < -1e-6)
                                & (np.einsum("ij,ij->i", in_src - b_star, n_b) < -1e-6))
                    rec["clearance_samples"] = rec.get("clearance_samples", 0) + int((~new_free).sum())
                    keep = np.flatnonzero(keep)[new_free]
                rec["open_samples"] += int((opening > tol["gap_visible_m"]).sum()) if at_rest else int(len(keep))
                if len(keep):
                    keep = np.asarray(keep)
                    mids.append((pts[keep] + locs[keep]) * 0.5)
                    widths.append(dist[keep])
                    opens.append(opening[keep])
            if mids:
                mid, width, opening = np.vstack(mids), np.concatenate(widths), np.concatenate(opens)
                visible = np.zeros(len(mid), dtype=bool)
                for k, origin in enumerate(origins):
                    ray = mid - origin
                    length = np.linalg.norm(ray, axis=1)
                    face, hit_dist, _ = rk.cast(self.global_tree, origin, ray / length[:, None], 1e6)
                    seen = (face < 0) | (hit_dist >= length - 1e-6)
                    if seen.any():
                        rec["visible_views"] += 1
                        if len(rec["example_views"]) < 4:
                            rec["example_views"].append(views[k]["name"])
                    visible |= seen
                if visible.any():
                    rec["gap_visible_m"] = float(width[visible].max())
                    rec["open_visible_m"] = float(opening[visible].max())
            if at_rest and rec["gap_visible_m"] > tol["rest_gap_visible_max_m"]:
                failures.append(fail("rest_seam_open", f"{seam['joint']} seam {rec['seam']} is {rec['gap_visible_m'] * 1000:.2f} mm "
                                     f"open at rest in line of sight of {rec['visible_views']} held-out views",
                                     rec["gap_visible_m"], tol["rest_gap_visible_max_m"], seam["joint"]))
            if not at_rest and rec["open_visible_m"] > tol["gap_visible_m"]:
                failures.append(fail("joint_gap_visible",
                                     f"{seam['joint']} seam {rec['seam']} opens {rec['open_visible_m'] * 1000:.2f} mm beyond rest "
                                     f"(absolute {rec['gap_visible_m'] * 1000:.2f} mm), in line of sight of {rec['visible_views']} held-out views",
                                     rec["open_visible_m"], tol["gap_visible_m"], seam["joint"]))
            out.append(rec)
        return out

    def collisions(self, failures):
        rig, tol, state, rigid = self.rig, self.tol, self.state, self.rigid
        centres = {p: self.rest_centre[p] @ state[p][0].T + state[p][1] for p in self.parts}
        out = []
        for i, a in enumerate(self.parts):
            for b in self.parts[i + 1:]:
                if np.linalg.norm(centres[a] - centres[b]) > self.rest_radius[a] + self.rest_radius[b]:
                    continue
                candidates = self.tree(a).overlap(self.tree(b))
                if not candidates:
                    continue
                ia = np.array([c[0] for c in candidates])
                ib = np.array([c[1] for c in candidates])
                va = rigid[rig.verts_of[a]][self.local_tris[a][ia]]
                vb = rigid[rig.verts_of[b]][self.local_tris[b][ib]]
                kinds, depths = rk.tri_tri_batch(va, vb)
                cross = kinds == "cross"
                depth = float(depths[cross].max()) if cross.any() else 0.0
                permitted = tuple(sorted((a, b))) in self.permitted
                example = None
                if cross.any():
                    worst = np.flatnonzero(cross)[int(np.argmax(depths[cross]))]
                    example = {"a_triangle_centre_m": [round(float(x), 5) for x in va[worst].mean(0)],
                               "a_triangle": [int(x) for x in self.local_tris[a][ia[worst]]],
                               "b_triangle": [int(x) for x in self.local_tris[b][ib[worst]]]}
                out.append({"a": a, "b": b, "candidates": int(len(candidates)), "crossing": int(cross.sum()),
                            "depth_m": depth, "permitted": permitted, "example": example})
                if not permitted and depth > tol["interpenetration_m"]:
                    failures.append(fail("interpenetration", f"{a} and {b} interpenetrate {depth * 1000:.2f} mm "
                                         f"over {int(cross.sum())} triangle pairs", depth, tol["interpenetration_m"],
                                         f"{a}|{b}"))
        return out

    def census(self, rigid, views, pose, failures):
        tol, rig = self.tol, self.rig
        rec = {"views": len(views), "views_cast": 0, "views_reused": 0, "worst_view": None, "worst_part": None,
               "worst_px": 0, "worst_mm2": 0.0, "interior_views": 0, "by_group": {}, "by_part": {},
               "see_through": {"threshold_px": int(tol["see_through_px_min"]),
                               "heldout_views_cast": 0, "by_seam": {}}}
        all_views = list(views)
        film = pose.get("film_camera")
        if film:
            all_views.append({"name": "film", "group": "film", "film": True, "camera_matrix": film})
        intended_codes = np.array([p in self.intended_parts for p in self.parts])
        seam_regions = [(s, rk.transform_points(self.state, s["b"], rig.head[s["b"]]))
                        for s in self.seams]
        for view in all_views:
            if not view.get("cast", True):
                rec["views_reused"] += 1
                continue
            rec["views_cast"] += 1
            if view["film"]:
                cam = view["camera_matrix"]
                origin, dirs, scale = rk.camera_rays_matrix(cam["matrix_world"], cam["lens_mm"], cam["sensor_width_mm"],
                                                            cam["sensor_height_mm"], cam["sensor_fit"],
                                                            cam["shift_x"], cam["shift_y"], self.resolution)
            else:
                dirs, scale = rk.rays_from_basis(view["origin"], view["forward"], view["right"], view["up"],
                                                 self.cameras["lens_mm"], self.cameras["sensor_width_mm"], self.resolution)
                origin = view["origin"]
            face, dist, normal = rk.cast(self.global_tree, origin, dirs, 1e6)
            hit = face >= 0
            codes = np.full(len(face), -1, dtype=np.int64)
            codes[hit] = self.tri_code[face[hit]]
            if not view["film"]:
                rec["see_through"]["heldout_views_cast"] += 1
                background, indices, labels = background_neighbours(codes, self.resolution)
                for seam, center in seam_regions:
                    a, b = seam["codes"]
                    border = opposing_labels(labels, a, b)
                    if not border.any():
                        continue
                    pixels, tags = indices[border], labels[border]
                    points = np.asarray(origin) + dirs[pixels] * dist[pixels, None]
                    near = np.sum((points - center) ** 2, axis=2) <= seam["radius"] ** 2
                    enclosed = opposing_labels(np.where(near, tags, -1), a, b)
                    count = int(enclosed.sum())
                    by_seam = rec["see_through"]["by_seam"]
                    name = seam["joint"]
                    if count > by_seam.get(name, {}).get("pixels", 0):
                        row = {"pixels": count, "view": view["name"], "parts": [seam["a"], seam["b"]]}
                        if self.spec.get("dump_dir") and count >= tol["see_through_px_min"]:
                            image = np.full((len(dirs), 3), 24, dtype=np.uint8)
                            image[codes == a] = PALETTE[a % len(PALETTE)]
                            image[codes == b] = PALETTE[b % len(PALETTE)]
                            misses = background[border][enclosed]
                            image[misses] = (255, 255, 255)
                            width, height = self.resolution
                            view_id = view["name"].replace("@", "_at_").replace("+", "p").replace("-", "m")
                            path = Path(self.spec["dump_dir"]) / f"{self.pose_id}__{name}__{view_id}.png"
                            path.parent.mkdir(parents=True, exist_ok=True)
                            Image.fromarray(image.reshape(height, width, 3)).save(path)
                            row.update({"image": str(path), "pixel_xy": list(zip(
                                (misses % width).tolist(), (misses // width).tolist()))})
                        by_seam[name] = row
            back = hit & (np.einsum("ij,ij->i", normal, dirs) >= 0) & (self.material_class[face] == NORMAL)
            marked = hit & (self.material_class[face] == INTERIOR)
            back &= ~intended_codes[np.where(back, codes, 0)]
            interior = back | marked
            if interior.any():
                counts = np.bincount(codes[interior], minlength=len(self.parts))
                for code in np.flatnonzero(counts):
                    name, pixels = self.parts[code], int(counts[code])
                    if pixels > rec["by_part"].get(name, {}).get("pixels", 0):
                        rec["by_part"][name] = {"pixels": pixels, "view": view["name"]}
                part = int(counts.argmax())
                px = int(counts[part])
                rec["by_group"][view["group"]] = max(rec["by_group"].get(view["group"], 0), px)
                rec["interior_views"] += 1
                if px > rec["worst_px"]:
                    mm2 = px * (scale * float(dist[interior & (codes == part)].mean())) ** 2
                    rec.update({"worst_view": view["name"], "worst_part": self.parts[part], "worst_px": px, "worst_mm2": mm2})
                    if self.spec.get("dump_dir"):
                        # Evidence image: part colours, interior hits in red, marked interior in magenta.
                        image = np.full((len(dirs), 3), 24, dtype=np.uint8)
                        for code, part_name in enumerate(self.parts):
                            image[codes == code] = PALETTE[code % len(PALETTE)]
                        image[back] = (235, 40, 40)
                        image[marked] = (235, 40, 225)
                        safe = view["name"].replace("@", "_at_").replace("+", "p").replace("-", "m").replace("/", "_")
                        path = Path(self.spec["dump_dir"]) / f"{self.pose_id}__{safe}.png"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        Image.fromarray(image.reshape(self.resolution[1], self.resolution[0], 3)).save(path)
                        rec["worst_view_image"] = str(path)
        for part, row in rec["by_part"].items():
            if row["pixels"] >= self.pixel_threshold:
                failures.append(dict(fail("socket_exposed", "visible interior in the first-hit pixel census",
                                          row["pixels"], self.pixel_threshold, part), view=row["view"]))
        for joint, row in rec["see_through"]["by_seam"].items():
            if row["pixels"] >= tol["see_through_px_min"]:
                failures.append(dict(fail("seam_see_through", "background borders both parts in a held-out view",
                                          row["pixels"], tol["see_through_px_min"], joint), view=row["view"]))
        return rec

    # ------------------------------------------------------------ summary
    def worst_of(self, poses):
        """Worst observed value per registered class over all poses (lower is worse only for sole contact)."""
        worst = {}

        def keep(key, value, **info):
            lower = key == "sole_contact_insufficient"
            if key not in worst or (value < worst[key]["value"] if lower else value > worst[key]["value"]):
                worst[key] = dict(value=value, **info)

        for rec in poses:
            for gap in rec.get("gaps", []):
                keep("joint_gap_visible", gap["open_visible_m"], joint=gap["joint"], seam=gap["seam"],
                     absolute_m=gap["gap_visible_m"], pose=rec["pose_id"])
            interior = rec.get("interior", {})
            for joint, row in interior.get("see_through", {}).get("by_seam", {}).items():
                keep("seam_see_through", row["pixels"], joint=joint, view=row["view"], pose=rec["pose_id"])
            keep("socket_exposed", interior.get("worst_px", 0), part=interior.get("worst_part"),
                 view=interior.get("worst_view"), mm2=interior.get("worst_mm2"), pose=rec["pose_id"])
            for col in rec.get("collisions", []):
                if not col["permitted"]:
                    keep("interpenetration", col["depth_m"], pair=f"{col['a']}|{col['b']}", pose=rec["pose_id"])
            for side, sole in rec.get("soles", {}).items():
                if "clearance_min_m" in sole:
                    keep("sole_not_touching", sole["clearance_min_m"], side=side, pose=rec["pose_id"])
                    keep("sole_contact_insufficient", sole["contact_fraction"], side=side,
                         area_m2=sole["contact_area_m2"], pose=rec["pose_id"])
            floor = rec.get("floor", {})
            keep("floor_penetration", -floor.get("min_z_m", 0.0), part=floor.get("lowest_part"), pose=rec["pose_id"])
            fk = rec.get("fk", {})
            keep("rigidity", fk.get("rigidity_m", 0.0), pose=rec["pose_id"])
            keep("fk_independent", fk.get("independent_m", 0.0), pose=rec["pose_id"])
        return worst

    def run(self):
        self.load()
        if self.asset_failures:
            return self.unmeasured()
        rest_failures = self.rest_checks()
        if self.native and not self.joints:
            rest_failures.append(fail("bearing_surface_unverified", "native assembly has no registered mating surfaces",
                                      None, None, "asset"))
        if self.spec.get("enforce_budget") and not self.asset["triangles_within_budget"]:
            rest_failures.append(fail("triangle_budget_exceeded", "exterior and internal triangle total",
                                      self.asset["triangles"], self.tol["triangle_budget"], "asset"))
        for name, count in self.asset.get("nonmanifold_edges_by_object", {}).items():
            if count:
                rest_failures.append(fail("exterior_not_closed", "mesh edges with non-manifold incidence",
                                          count, 0, name))
        failures = [*self.asset_failures, *rest_failures]
        poses = [self.measure(pose) for pose in self.spec["poses"]]
        for rec in poses:
            failures += rec["failures"]
        return {"asset": self.asset, "asset_failures": self.asset_failures, "rest": self.rest,
                "rest_failures": rest_failures, "poses": poses,
                "verdict": "REJECT" if failures else "PASS",
                "rejected_classes": sorted({f["class"] for f in failures}),
                "camera_frames": self.camera_frames,
                "worst": self.worst_of(poses)}

    def unmeasured(self):
        poses = [{"pose_id": pose.get("pose_id"), "family": pose.get("family"), "verdict": "NOT_MEASURED",
                  "failures": [], "joints_not_commanded": [], "note": "asset rejected on identity"} for pose in self.spec["poses"]]
        return {"asset": self.asset, "asset_failures": self.asset_failures, "rest": None, "rest_failures": [],
                "poses": poses, "verdict": "REJECT", "rejected_classes": sorted({f["class"] for f in self.asset_failures}),
                "worst": {}, "measured": False}


def main():
    spec = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
    out_path = Path(spec["out"])
    report = {"schema": SCHEMA, "blender": bpy.app.version_string, "shard": spec.get("shard")}
    try:
        ok, scope = effective_scope()
        report["scope"] = dict(scope, effective=ok)
        if spec.get("require_scope") and not ok:
            raise rk.RigError("effective_scope_mismatch", json.dumps(scope, sort_keys=True))
        report.update(Oracle(spec).run())
    except Refusal as error:
        report["refusal"] = error.payload(["./oracle", "measure"])
    except rk.RigError as error:
        report["refusal"] = {"reason": f"{error.code}:{error.detail}"[:400]}
    except Exception as error:  # harness failure; the caller maps it to rc 2
        import traceback
        report["refusal"] = {"reason": f"harness:{type(error).__name__}:{error}"[:400],
                             "traceback": traceback.format_exc()[-1800:]}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    print("MEASURE", "refused" if "refusal" in report else report["verdict"])


if __name__ == "__main__":
    main()
