"""Apply a viseme track to explicitly bound relative Blender shape keys."""

from fractions import Fraction
from math import isclose

from . import ENVELOPE, PPM, SHAPES, VoiceError, validate_track


def apply_shape_keys(track, rig, scene, *, first_frame, mouth_keys, jaw_key):
    """Create an action on the rig's shape keys and hold each exact frame sample.

    A fresh action is required. The caller owns the scene/view timeline and
    must compare the rendered PTS with the track during rendering and mux.
    """
    validate_track(track)
    frames = track["frames"]
    frame_property = scene.bl_rna.properties["frame_current"]
    if (type(first_frame) is not int or not frame_property.hard_min <= first_frame
            or first_frame + len(frames) - 1 > frame_property.hard_max):
        raise VoiceError("invalid_blender_frame_range")
    expected_fps = Fraction(*track["fps"])
    actual_fps = scene.render.fps / scene.render.fps_base
    # fps_base is represented as a Blender float. The track retains exact PTS.
    if not isclose(actual_fps, float(expected_fps), rel_tol=1e-7):
        raise VoiceError("blender_timebase_changed", rc=4)

    mouth_shapes = SHAPES[1:] if track["source"] != ENVELOPE else ()
    if (type(mouth_keys) is not dict or set(mouth_keys) != set(mouth_shapes)
            or any(type(name) is not str or not name for name in (*mouth_keys.values(), jaw_key))):
        raise VoiceError("invalid_rig_bindings")
    names = [*(mouth_keys[shape] for shape in mouth_shapes), jaw_key]
    if len(names) != len(set(names)):
        raise VoiceError("rig_shape_keys_ambiguous")

    keys = getattr(getattr(rig, "data", None), "shape_keys", None)
    if keys is None or not keys.use_relative or rig.data.users != 1:
        raise VoiceError("relative_shape_keys_required")
    animation = keys.animation_data
    if animation and (animation.action or animation.drivers or animation.nla_tracks):
        raise VoiceError("rig_animation_exists")
    blocks = [keys.key_blocks.get(name) for name in names]
    for block in blocks:
        if block is None:
            raise VoiceError("rig_shape_key_missing", rc=3)
        if (block == keys.reference_key or block.relative_key != keys.reference_key
                or block.mute or block.slider_min > 0 or block.slider_max < 1):
            raise VoiceError("incompatible_shape_key")

    old_values = [block.value for block in blocks]
    try:
        for row in frames:
            frame = first_frame + row["index"]
            values = [*(row["mouth_ppm"][shape] for shape in mouth_shapes), row["jaw_ppm"]]
            for block, value in zip(blocks, values, strict=True):
                block.value = value / PPM
                if not block.keyframe_insert(data_path="value", frame=frame, group="VOICE"):
                    raise VoiceError("blender_key_insertion_failed")
        action = keys.animation_data.action
        for curve in action.fcurves:
            for point in curve.keyframe_points:
                point.interpolation = "CONSTANT"
        return action
    except Exception:
        import bpy

        animation = keys.animation_data
        action = animation.action if animation else None
        if animation:
            animation.action = None
        if action and not action.users:
            bpy.data.actions.remove(action)
        for block, value in zip(blocks, old_values, strict=True):
            block.value = value
        raise
