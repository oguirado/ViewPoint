# SPDX-License-Identifier: AGPL-3.0-only
"""Inspect a rig GLB: identity, skeleton, parts, seams, soles and rest integrity.

Runs inside ``blender -b --factory-startup --disable-autoexec`` and reopens
the GLB on its own.  The record it writes is the asset identity used by the
sweep generator, the comparator and the manifest.

argv after ``--``: path to the inspect spec JSON written by the CLI.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import numpy as np  # noqa: E402
import bpy  # noqa: E402
import rigkit as rk  # noqa: E402
from pose_oracle import glb as glbmod  # noqa: E402


FOOT_BAND_M = 0.10  # lowest part of each foot, used by the sweep's plant solver


def main():
    spec = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
    out_path = Path(spec["out"])
    report = {"schema": "rp4.pose-oracle.rig.v1", "blender": bpy.app.version_string, "rig": spec["rig"]}
    try:
        inspect(spec, report)
    except rk.RigError as error:
        report["error"] = f"{error.code}:{error.detail}"
    except Exception as error:  # harness failure; the caller maps it to rc 2
        import traceback
        report["error"] = f"harness:{type(error).__name__}:{error}"
        report["traceback"] = traceback.format_exc()[-1800:]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    print("INSPECT", report.get("error", "ok"))


def inspect(spec, report):
    path = spec["rig"]
    file = glbmod.Glb(path)
    rig = rk.import_rig(path)
    mapped = {s: j["bone"] for s, j in spec["joint_map"]["joints"].items() if j.get("bone")}
    missing = sorted(s for s, bone in mapped.items() if bone not in rig.index)
    report.update({
        "asset": {"path": path, "file_sha256": file.sha256, "bytes": len(file.bytes),
                  "structure_sha256": file.structure_digest(),
                  "generator": file.doc.get("asset", {}).get("generator"),
                  "mesh_names": [m.name for m in rig.meshes], "materials": file.structure()["materials"],
                  "vertices": rig.n_vertices, "triangles": rig.n_triangles,
                  "joints": len(rig.order), "constraints": rig.constraints,
                  "helper_objects": rig.helper_objects, "other_objects": rig.other_objects,
                  "parts": {p: {"vertices": int(len(rig.verts_of[p])), "triangles": int(len(rig.tris_of[p])),
                                "boundary_edges": int(len(rk.boundary_edges(rig, rig.tris_of[p])))}
                            for p in rig.part_names}},
        "order": rig.order,
        "bones": {b: {"parent": rig.parent[b], "head": [float(x) for x in rig.head[b]],
                      "flags": rig.bone_flags[b]} for b in rig.order},
        "map": {s: {"bone": bone, "side": spec["joint_map"]["joints"][s].get("side"),
                    "optional": bool(spec["joint_map"]["joints"][s].get("optional"))} for s, bone in mapped.items()},
        "map_missing": missing,
        "unmapped_bones": sorted(set(rig.order) - set(mapped.values()))})
    required = [s for s in spec["joint_map"].get("required_for_replay", []) if not spec["joint_map"]["joints"][s].get("optional")]
    report["map_unresolved_required"] = sorted(s for s in required if s not in mapped or mapped[s] not in rig.index)

    # ------------------------------------------------------------ rest integrity
    matrix, readback = rk.apply(rig, {})
    posed = rk.evaluated(rig)
    state, rigid, resid, fk = rk.rigid_state(rig, posed, matrix)
    glb_err, glb_mode = rk.glb_rest_error(file, rig.rest_v)
    soles = {}
    for side, sole in spec["joint_map"]["soles"].items():
        bone = sole["part"]
        if bone in rig.verts_of:
            idx = rig.verts_of[bone]
            z = rig.rest_v[idx, 2] - spec["floor_z_m"]
            sel = idx[z <= sole["band_m"]]
            foot = idx[z <= FOOT_BAND_M]
            soles[side] = {"joint": sole["joint"], "part": bone, "band_m": sole["band_m"],
                           "vertices": [[round(float(v), 7) for v in p] for p in rig.rest_v[sel]],
                           "foot_vertices": [[round(float(v), 6) for v in p] for p in rig.rest_v[foot]],
                           "rest_min_clearance_m": float(z.min())}
        else:
            soles[side] = {"joint": sole["joint"], "part": bone, "band_m": sole["band_m"], "vertices": [],
                           "error": "sole_part_missing"}
    low = rig.rest_v[:, 2].min()
    report["rest"] = {
        "eval_vs_data_m": float(np.linalg.norm(posed - rig.rest_v, axis=1).max()),
        "fk_readback_m": readback, "rigidity_m": resid, "fk_independent_m": fk,
        "glb_positions_max_m": glb_err, "glb_positions_mode": glb_mode,
        "min_z_m": float(low), "floor_z_m": spec["floor_z_m"],
        "bbox": [rig.rest_v.min(0).tolist(), rig.rest_v.max(0).tolist()]}

    # ------------------------------------------------------------ seams at rest
    seams = []
    band = spec["seam_band_m"]
    for parent, child in rk.seam_pairs(rig):
        a_pts, _, _, _ = rk.contact_samples(rig, parent, child, band)
        b_pts, _, _, _ = rk.contact_samples(rig, child, parent, band)
        pivots = [rig.head[b] for b in (parent, child)]
        seams.append({"parent": parent, "child": child,
                      "contact_samples": [int(len(a_pts)), int(len(b_pts))],
                      "pivot_distance_m": float(np.linalg.norm(pivots[1] - pivots[0])),
                      "parent_pivot_to_child_pivot_m": [float(x) for x in (pivots[1] - pivots[0])]})
    report["seams"] = seams
    report["soles"] = soles

    # ------------------------------------------------------------ winding sanity at rest
    tree = rk.tree_of(rigid, rig.tris)
    fpart = rig.part_of_tri
    centre = (rig.rest_v.min(0) + rig.rest_v.max(0)) / 2
    radius = float(np.linalg.norm(rig.rest_v.max(0) - rig.rest_v.min(0)) / 2)
    backfaces = {}
    for view in spec["census_views"]:
        camera = dict(view, distance_m=radius / (0.98 * 10.125 / 50.0), lens_mm=50.0, sensor_width_mm=36.0)
        origin, dirs, _ = rk.camera_rays(camera, centre, (320, 180))
        face, _, normal = rk.cast(tree, origin, dirs)
        hit = face >= 0
        back = hit & (np.einsum("ij,ij->i", normal, dirs) >= 0)
        backfaces[f"{view['az_deg']}/{view['el_deg']}"] = {p: int((back & (fpart[np.where(hit, face, 0)] == p)).sum())
                                                          for p in rig.part_names
                                                          if int((back & (fpart[np.where(hit, face, 0)] == p)).sum())}
    report["rest"]["backface_px"] = backfaces
    report["rest"]["backface_px_total"] = int(sum(sum(v.values()) for v in backfaces.values()))


if __name__ == "__main__":
    main()
