# SPDX-License-Identifier: AGPL-3.0-only
"""Extract joint poses, the film camera and the declared stance from a frozen take.

Runs inside ``blender -b --factory-startup --disable-autoexec``.  The take is
opened read-only and never saved.  For each requested SET frame the script
records every joint's rest-relative rotation (D format, anatomical axes) and
the root offset in world coordinates, measures the robot's own sole geometry
in the take, then reopens the pinned rig GLB in a fresh empty scene and
replays the frame twice (raw bone basis, and the D-format command) to report
how faithfully the GLB reproduces the film geometry.

argv after ``--``: path to the extraction spec JSON written by the CLI.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np  # noqa: E402
import bpy  # noqa: E402
from mathutils import Matrix  # noqa: E402
import rigkit as rk  # noqa: E402


def matrix(rows):
    return Matrix([rows[0:4], rows[4:8], rows[8:12], rows[12:16]])


def main():
    spec = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
    out_path = Path(spec["out"])
    report = {"schema": "rp4.pose-oracle.extract.v1", "blender": bpy.app.version_string,
              "blend": spec["blend"], "rig_glb": spec["rig"], "frames": {}}
    try:
        extract(spec, report)
    except rk.RigError as error:
        report["error"] = f"{error.code}:{error.detail}"
    except Exception as error:  # harness failure; the caller maps it to rc 2
        import traceback
        report["error"] = f"harness:{type(error).__name__}:{error}"
        report["traceback"] = traceback.format_exc()[-1800:]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    print("EXTRACT", report.get("error", "ok"))


def extract(spec, report):
    bpy.ops.wm.open_mainfile(filepath=spec["blend"], load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    arms = [o for o in scene.objects if o.type == "ARMATURE"]
    skinned = [o for o in scene.objects if o.type == "MESH" and any(m.type == "ARMATURE" for m in o.modifiers)]
    if len(arms) != 1 or not skinned:
        raise rk.RigError("take_roster", f"armatures={len(arms)} skinned={len(skinned)}")
    arm = arms[0]
    # The robot is the largest skinned mesh; later takes add SET-attached covers and blended gaiters
    # that are not part of the rig GLB.  They are listed, not measured.
    skin = max(skinned, key=lambda o: len(o.data.vertices))
    influences = {}
    for vert in skin.data.vertices:
        count = sum(1 for g in vert.groups if g.weight > 1e-6)
        influences[str(count)] = influences.get(str(count), 0) + 1
    report["scene"] = {
        "fps": scene.render.fps / scene.render.fps_base, "frame_start": scene.frame_start,
        "frame_end": scene.frame_end, "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "engine": scene.render.engine, "armature": arm.name, "mesh": skin.name,
        "vertices": len(skin.data.vertices), "influences": influences,
        "armature_matrix_world": rk.np_mat(arm.matrix_world).tolist(), "objects": len(scene.objects),
        "other_skinned_meshes": [o.name for o in skinned if o is not skin],
        "bone_attached_meshes": sorted(o.name for o in scene.objects
                                       if o.type == "MESH" and o.parent == arm and o not in skinned)}
    cam = scene.camera
    if cam is not None:
        report["scene"]["camera"] = {
            "name": cam.name, "matrix_world": rk.np_mat(cam.matrix_world).tolist(), "lens_mm": cam.data.lens,
            "sensor_width_mm": cam.data.sensor_width, "sensor_height_mm": cam.data.sensor_height,
            "sensor_fit": cam.data.sensor_fit, "shift_x": cam.data.shift_x, "shift_y": cam.data.shift_y,
            "clip_start": cam.data.clip_start, "clip_end": cam.data.clip_end, "type": cam.data.type}
    semantic = {v["bone"]: k for k, v in spec["joint_map"]["joints"].items()}
    world = rk.np_mat(arm.matrix_world)
    rest = {pb.name: world @ rk.np_mat(pb.bone.matrix_local) for pb in arm.pose.bones}
    skin_world = rk.np_mat(skin.matrix_world)
    sole_vertices = {}
    for side, sole in spec["joint_map"]["soles"].items():
        if sole["part"] in skin.vertex_groups:
            index = skin.vertex_groups[sole["part"]].index
            sole_vertices[side] = np.array([v.index for v in skin.data.vertices
                                            if any(g.group == index and g.weight > 0.5 for g in v.groups)],
                                           dtype=np.int64)
    take_positions = {}
    for frame in spec["frames"]:
        scene.frame_set(int(frame))
        deps = bpy.context.evaluated_depsgraph_get()
        joints = {}
        for pb in arm.pose.bones:
            posed = world @ rk.np_mat(pb.matrix)
            delta = posed[:3, :3] @ rest[pb.name][:3, :3].T
            entry = {"bone": pb.name,
                     "q": [float(x) for x in rk.mat_to_quat(rk.ANAT.T @ delta @ rk.ANAT)],
                     "basis": [float(x) for row in pb.matrix_basis for x in row],
                     "head_world": [float(x) for x in posed[:3, 3]]}
            if pb.parent is None:
                entry["t"] = [float(x) for x in (posed[:3, 3] - rest[pb.name][:3, 3])]
            joints[semantic.get(pb.name, pb.name)] = entry
        ev = skin.evaluated_get(deps)
        me = ev.to_mesh()
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        pts = co.reshape(-1, 3) @ skin_world[:3, :3].T + skin_world[:3, 3]
        ev.to_mesh_clear()
        take_positions[str(frame)] = pts
        soles = {}
        for side, verts in sole_vertices.items():
            clearance = pts[verts, 2] - spec["floor_z_m"] if len(verts) else np.array([np.nan])
            soles[side] = {"min_clearance_m": float(clearance.min()), "vertices": int(len(verts))}
        report["frames"][str(frame)] = {
            "set_frame": int(frame), "movie_frame": int(frame) - 1, "joints": joints,
            "plant_declared": spec["stance"].get(str(frame), []), "soles_in_take": soles}

    # ------------------------------------------------------------ replay on the GLB
    rig = rk.import_rig(spec["rig"])
    same = len(rig.rest_v) == len(next(iter(take_positions.values())))
    report["replay_rig"] = {"armature": rig.arm.name, "vertices": rig.n_vertices, "triangles": rig.n_triangles,
                            "same_vertex_count": same, "parts": rig.part_names}
    for key, frame_rec in report["frames"].items():
        pts_take = take_positions[key]
        by_bone = {v["bone"]: v for v in frame_rec["joints"].values()}
        rec = {"basis_max_m": None, "dformat_max_m": None, "fk_readback_m": None, "head_max_m": None}
        frame_rec["replay"] = rec
        if not all(bone in by_bone for bone in rig.order) or pts_take.shape != rig.rest_v.shape:
            continue
        for pb in rig.arm.pose.bones:
            pb.matrix_basis = matrix(by_bone[pb.name]["basis"])
        bpy.context.view_layer.update()
        rec["basis_max_m"] = float(np.linalg.norm(rk.evaluated(rig) - pts_take, axis=1).max())
        command = {bone: {k: by_bone[bone][k] for k in ("q", "t") if k in by_bone[bone]} for bone in rig.order}
        _, readback = rk.apply(rig, command)
        rec["fk_readback_m"] = readback
        rec["dformat_max_m"] = float(np.linalg.norm(rk.evaluated(rig) - pts_take, axis=1).max())
        rec["head_max_m"] = max(float(np.linalg.norm(rk.np_mat(pb.matrix)[:3, 3] - np.array(by_bone[pb.name]["head_world"])))
                                for pb in rig.arm.pose.bones)


if __name__ == "__main__":
    main()
