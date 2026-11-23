"""Blender-side, fixture-only relative mouth and jaw shape keys."""
from tools.input.evidence import require
from tools.lipsync import PPM, SHAPES
from tools.lipsync.blender_apply import apply_shape_keys


def bind_controls(rig, parts, scene, track):
    require(track["source"] != "audio_envelope", 3, "fixture_requires_mouth_and_jaw")
    require(len(parts) == len(rig.data.vertices) and {"upper_lip", "lower_lip", "chin"} <= set(parts),
            3, "missing_face_binding")
    rig.shape_key_add(name="Basis")
    for name in (*SHAPES[1:], "jaw"):
        block = rig.shape_key_add(name=name, from_mix=False)
        for part, point in zip(parts, block.data, strict=True):
            if part in {"upper_lip", "lower_lip"}:
                upper = part == "upper_lip"
                if name == "closed":
                    point.co.z += -0.002 if upper else 0.002
                elif name == "open":
                    point.co.z += 0.004 if upper else -0.002
                elif name == "wide":
                    point.co.x *= 1.18
                elif name == "round":
                    point.co.x *= 0.8
                elif name == "bite" and not upper:
                    point.co.y += 0.002
            if name == "jaw" and part in {"lower_lip", "chin"}:
                point.co.z -= 0.012
    return apply_shape_keys(track, rig, scene, first_frame=1,
                            mouth_keys={s:s for s in SHAPES[1:]}, jaw_key="jaw")


def reopened_samples(rig, scene, track):
    """Yield after Blender evaluates each keyframe. Vertex samples are world µm."""
    import bpy
    from tools.lipsync import validate_track

    validate_track(track)
    require(scene.frame_start == 1 and scene.frame_end == len(track["frames"]),
            4, "reopened_timeline_drift")
    from fractions import Fraction
    require(abs(scene.render.fps/scene.render.fps_base - float(Fraction(*track["fps"]))) < 1e-7,
            4, "reopened_timebase_drift")
    keys = rig.data.shape_keys
    require(keys is not None and set(keys.key_blocks.keys()) == {"Basis", *SHAPES[1:], "jaw"},
            5, "reopened_shape_key_set")
    for cue in track["frames"]:
        scene.frame_set(1 + cue["index"])
        evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        weights = {k:round(evaluated.data.shape_keys.key_blocks[k].value*PPM)
                   for k in (*SHAPES[1:], "jaw")}
        expected = {k:v for k,v in cue["mouth_ppm"].items() if k != "rest"}
        expected["jaw"] = cue["jaw_ppm"]
        require(weights == expected, 5, "reopened_control_drift")
        sample = {"index": cue["index"], "pts": cue["pts"], "controls_ppm": weights}
        sample["vertices_um"] = [[round(v * 1_000_000) for v in evaluated.matrix_world @ vertex.co]
                                     for vertex in evaluated.data.vertices]
        yield sample
