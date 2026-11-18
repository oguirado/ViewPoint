"""Author the ACT walk/gesture on the supplied SKIN GLB in background Blender.

Run with --background --factory-startup --disable-autoexec and pass the
retained skin and an evidence directory after Blender's -- separator.
"""
from fractions import Fraction
from pathlib import Path
import sys
import time

import bpy
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from asset_factory import core
from providers.unirig.gate import node_matrix
from tools.motion import action

FRAME_START = 1
AUTHORED_FPS = 120
DURATION = 2
ACTION_NAME = "ACT Walk and Gesture"
GLTF = Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))


def _rotation(matrix):
    return Matrix([[matrix[col*4 + row] for col in range(3)] for row in range(3)]).to_quaternion()


def _xyzw(quaternion):
    return (quaternion.x, quaternion.y, quaternion.z, quaternion.w)


def _create_action(armature, scene):
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.render.fps = AUTHORED_FPS
    scene.frame_start = FRAME_START
    scene.frame_end = FRAME_START + DURATION * AUTHORED_FPS
    lifts = {"left_foot": [0, 0, 0, .125, 0, 0, 0, 0, 0],
             "right_foot": [0, 0, 0, 0, 0, .125, 0, 0, 0]}
    gestures = {"left_elbow": [0, 0, .15, .30, .30, .15, 0, 0, 0],
                "right_elbow": [0, -.12, -.18, -.12, 0, .10, .12, .10, 0]}
    for i in range(9):
        frame = FRAME_START + i * (AUTHORED_FPS // 4)
        for name, heights in lifts.items():
            bone = armature.pose.bones[name]
            basis = (armature.matrix_world @ bone.bone.matrix_local).to_3x3()
            bone.location = basis.inverted() @ Vector((0, 0, heights[i]))
            bone.keyframe_insert(data_path="location", frame=frame, group=name)
        for name, angles in gestures.items():
            bone = armature.pose.bones[name]
            basis = (armature.matrix_world @ bone.bone.matrix_local).to_3x3()
            bone.rotation_mode = "QUATERNION"
            bone.rotation_quaternion = Quaternion(basis.inverted() @ Vector((0, 0, 1)), angles[i])
            bone.keyframe_insert(data_path="rotation_quaternion", frame=frame, group=name)
    motion = armature.animation_data.action
    motion.name = ACTION_NAME
    for fcurve in motion.fcurves:
        for key in fcurve.keyframe_points:
            key.interpolation = "LINEAR"
    return {name: "translation" for name in lifts} | {name: "rotation" for name in gestures}


def _animation_tracks(scene, armature, skin, paths):
    _, rest_world = skin.hierarchy()
    names = {node["name"]: i for i, node in enumerate(skin.doc["nodes"])}
    samples = {name: [] for name in paths}
    times = tuple(Fraction(i, 4) for i in range(9))
    for t in times:
        scene.frame_set(int(FRAME_START + t * AUTHORED_FPS))
        for name, path in paths.items():
            index = names[name]
            node = skin.doc["nodes"][index]
            bone = armature.pose.bones[name]
            blender_basis = (GLTF @ (armature.matrix_world @ bone.bone.matrix_local).to_3x3()).to_quaternion()
            bind_to_glb = _rotation(rest_world[index]).inverted() @ blender_basis
            rest_rot = _rotation(node_matrix(node))
            if path == "translation":
                translated = rest_rot @ (bind_to_glb @ bone.location)
                result = tuple(Vector(node.get("translation", (0, 0, 0))) + translated)
            else:
                result = _xyzw(rest_rot @ bind_to_glb @ bone.rotation_quaternion @ bind_to_glb.inverted())
            samples[name].append(result)
    return [action.Track(names[name], path, times, tuple(samples[name])) for name, path in paths.items()]


def _blender_witness(scene, armature, mesh, anatomy):
    floor = min(p[1] for p in anatomy["vertices_um"])
    scene.frame_set(FRAME_START)
    rest_points = [GLTF @ (mesh.matrix_world @ v.co) for v in mesh.data.vertices]
    soles = {name: [i for i, p in enumerate(rest_points)
                    if round(p.y * 10**6) == floor and p.x * side > 0]
             for name, side in (("left_foot", 1), ("right_foot", -1))}
    assert all(soles.values()), "imported mesh lacks the registered sole patches"

    def um(point):
        return [round(float(value) * 10**6) for value in point]

    frames = {}
    for fps in (24, 30):
        rows = []
        for sample in range(DURATION * fps + 1):
            pts = Fraction(sample, fps)
            scene.frame_set(int(FRAME_START + pts * AUTHORED_FPS))
            evaluated = mesh.evaluated_get(bpy.context.evaluated_depsgraph_get())
            rows.append({"pts": [pts.numerator, pts.denominator],
                         "joints": {name: um(GLTF @ (armature.matrix_world @ bone.head))
                                    for name, bone in armature.pose.bones.items()},
                         "soles": {name: sorted(um(GLTF @ (evaluated.matrix_world @
                                                         evaluated.data.vertices[i].co))
                                                for i in indices)
                                   for name, indices in soles.items()}})
        frames[str(fps)] = rows
    scene.frame_set(FRAME_START)
    return {"blender_version": bpy.app.version_string, "frames": frames,
            "source_fps": AUTHORED_FPS}


def main():
    source, anatomy_file, output = map(Path, sys.argv[sys.argv.index("--") + 1:])
    skin = action.open_glb(source.read_bytes(), time.monotonic_ns() + 60_000_000_000)
    anatomy = core.load_json(anatomy_file, max_bytes=action.MAX_ACTION_BYTES)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    result = bpy.ops.import_scene.gltf(filepath=str(source))
    assert result == {"FINISHED"}, result
    (armature,) = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    (mesh,) = [obj for obj in bpy.context.scene.objects if obj.type == "MESH" and
               any(m.type == "ARMATURE" and m.object == armature for m in obj.modifiers)]
    assert set(armature.pose.bones.keys()) == anatomy["joint_origins"].keys()
    scene = bpy.context.scene
    paths = _create_action(armature, scene)
    tracks = _animation_tracks(scene, armature, skin, paths)
    (output / "walk-gesture.glb").write_bytes(action.encode(skin, ACTION_NAME, tracks))
    core.atomic_write_json(output / "blender-witness.json", _blender_witness(scene, armature, mesh, anatomy))
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(output / "walk-gesture.blend"))
    print("ACT_EXPORT_OK", bpy.app.version_string)


if __name__ == "__main__":
    main()
