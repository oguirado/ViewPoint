"""Deterministic builder of mutant-manifest.json (bvqa.mutant-manifest/v1, BSPEC_BORACLES section 4).

python3 adapters/example_scene/build_manifest.py [--check]   (pure stdlib, no Blender)
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
INTERVAL = {"start_render_index": 0, "end_render_index": 3}
with open(os.path.join(HERE, "registry.json")) as _fh:
    SD_ALL = json.load(_fh)["sd"]


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(obj):
    return hashlib.sha256(canon(obj).encode("utf-8")).hexdigest()


def file_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def M(mid, op, selector, params, sd=(), px=(), judge=(), kind="scene", tl=None, equivalent=False, applicability="base"):
    fault = {"operation": op, "selector": selector, "parameters": params}
    sd, px, judge = sorted(sd), sorted(px), sorted(judge)
    return {
        "id": mid, "kind": kind, "fault": fault, "fault_spec_sha256": sha(fault),
        "applicability": applicability, "interval": dict(INTERVAL),
        "expected_sd": sd, "expected_px": px, "expected_judge": judge,
        "expected_check_ids": sorted(set(sd).union(px, judge)), "equivalent": equivalent, "expected_tl": tl,
    }


SUBJ = {"tag": "subject"}
MUTANTS = [
    M("bm.flip_normals.01", "flip_normals", {"object": "TwoShell"}, {}, sd=["bo.mesh.inverted_normals"]),
    M("bm.flip_partial.02", "flip_normals", {"object": "Subject"}, {"permille": 100}, sd=["bo.mesh.inverted_normals"]),
    M("bm.delete_face.03", "delete_faces", SUBJ, {"count": 1}, sd=["bo.mesh.non_manifold"], judge=["mesh.holes_visible"], tl=4),
    M("bm.unlink_material.04", "unlink_material", SUBJ, {}, sd=["bo.mat.missing"], judge=["mat.wrong_look"]),
    M("bm.missing_texture.05", "missing_texture", {"object": "Textured"}, {}, sd=["bo.tex.missing"], px=["bp.magenta.attrib", "frame.magenta"], judge=["render.pink_material"]),
    M("bm.uv_stretch.06", "uv_scale", {"object": "Textured"}, {"factor": 8, "permille": 500}, sd=["bo.uv.stretch"], judge=["uv.texture_stretch_visible"]),
    M("bm.uv_delete.07", "uv_delete", {"object": "Textured"}, {}, sd=["bo.uv.missing"], judge=["uv.texture_stretch_visible"]),
    M("bm.scale_1000.08", "scale_object", SUBJ, {"permille": 1000000}, sd=["bo.cam.clipping", "bo.scale.anomaly"], judge=["cam.subject_framed"]),  # no frame.uniform: complete clipping (JB3-4) and a flat frame are exclusive here
    M("bm.subdiv.09", "add_subdiv", SUBJ, {"levels": 6}, sd=["bo.poly.budget"]),
    M("bm.camera_yaw.10", "camera_offset", {"object": "Cam"}, {"yaw_mdeg": 90000}, sd=["bo.cam.framing"], px=["bp.subject.visible"], judge=["cam.subject_framed"], tl=5),
    M("bm.clip_end.11", "camera_clip", {"object": "Cam"}, {"clip_end_mm": 100}, sd=["bo.cam.clipping"], px=["bp.subject.visible", "frame.uniform"], judge=["cam.subject_framed"]),
    M("bm.kill_lights.12", "kill_lights", {}, {}, sd=["bo.light.none"], px=["frame.black"], judge=["light.exposure"]),
    M("bm.zfight.13", "dup_coplanar", {"object": "Static"}, {"offset_um": 1}, sd=["bo.geom.zfight"], px=["bp.zfight.pixel"], judge=["geom.zfight_visible"]),
    M("bm.freeze_anim.14", "freeze_anim", SUBJ, {}, sd=["bo.anim.static"], px=["frame.frozen", "vc_frozen_subregion"], judge=["render.frozen_region"], tl=10),
    M("bm.drift.15", "drift", {"tag": "static"}, {"um_per_frame": 500}, sd=["bo.anim.static"], px=["vc_slow_drift"], judge=["anim.drift"], tl=11),
    M("bm.flicker.16", "flicker", {"object": "Static"}, {"period": 2}, px=["vc_flicker"], judge=["render.flicker"], tl=9),
    M("bm.hide_render.17", "hide_render", SUBJ, {}, sd=["bo.render.visibility"], px=["bp.subject.visible"], judge=["cam.subject_framed"]),
    M("bm.neg_scale.18", "scale_object", {"object": "TwoShell"}, {"axis": "x", "permille": -1000}, sd=SD_ALL, equivalent=True),
    M("bm.inv_shell.19", "flip_shell", {"object": "TwoShell"}, {"shell": "smallest"}, sd=["bo.mesh.inverted_normals"]),
    M("bm.bowtie.20", "bowtie", {"object": "Static"}, {}, sd=["bo.mesh.non_manifold"]),
    M("bm.degenerate.21", "collapse_face", {"object": "Static"}, {"count": 1}, sd=["bo.mesh.degenerate"]),
    M("bm.step_hold.22", "step_hold", {"object": "Static"}, {}, px=["vc_flicker"], kind="journal", equivalent=True, applicability="journal"),
]


def build():
    here = lambda n: os.path.join(HERE, n)
    manifest = {
        "schema": "bvqa.mutant-manifest/v1",
        "baseline_sha256": file_sha(here("make_base.py")),
        "adapter_sha256": file_sha(here("registry.json")),
        "checklist_sha256": file_sha(here("checklist.json")),
        "capture_plan_sha256": sha({"frames": [1, 2, 3, 4], "res": [160, 90], "spp": 2}),
        "blend_base_sha256": "built-by-make_base.py",
        "blender_version": "4.0.2",
        "mutants": MUTANTS,
        "clean_schedule": {"seeds": 30, "arm": "clean"},
    }
    return json.dumps(manifest, sort_keys=True, indent=1) + "\n"


if __name__ == "__main__":
    text = build()
    target = os.path.join(HERE, "mutant-manifest.json")
    if "--check" in sys.argv:
        with open(target) as fh:
            sys.exit(0 if fh.read() == text else 4)
    with open(target, "w") as fh:
        fh.write(text)
