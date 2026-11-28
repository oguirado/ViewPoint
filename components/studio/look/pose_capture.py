# SPDX-License-Identifier: AGPL-3.0-only
"""Capture deterministic same-camera rest and posed witnesses in background Blender."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_quality import coverage, frame_roster, joint_roster, screenshot, sha, studio_opaque


@contextmanager
def hide(scene_objects):
    flags = [(obj, obj.hide_render, obj.hide_get()) for obj in scene_objects]
    try:
        for obj, _, _ in flags:
            obj.hide_render = True
            obj.hide_set(True)
        bpy.context.view_layer.update()
        yield
    finally:
        for obj, render, view in flags:
            obj.hide_render = render
            obj.hide_set(view)
        bpy.context.view_layer.update()


def pair(scene, folder, name, actor):
    files = {"image": folder / (name + ".png"), "mask": folder / (name + "-mask.png")}
    screenshot(scene, files["image"])
    screenshot(scene, files["mask"], actor)
    return {key: {"path": path.name, "sha256": sha(path)} for key, path in files.items()}


def capture(scene, config, output):
    arms = joint_roster(config["arms"])
    frames = frame_roster(config["frames"])
    if not (frame_roster(config["shaded_frames"]) | frame_roster(config["visibility_frames"])) <= frames:
        raise ValueError("studio_render_timeline_incomplete")
    rig = scene.objects[config["armature"]]
    actors = [scene.objects[name] for name in config["actors"]]
    reference = [scene.objects[name] for name in config["reference"]]
    if (rig.type != "ARMATURE" or not actors or not reference or set(actors) & set(reference)
            or any(obj.type != "MESH" or obj.hide_render or obj.hide_get() for obj in actors + reference)
            or scene.camera is None or scene.camera.data.type != "PERSP"):
        raise ValueError("studio_render_capture_roster")
    rest_frame = config["rest_frame"]
    if type(rest_frame) is not int or rest_frame < 1:
        raise ValueError("studio_render_rest_frame")

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.seed = 0
    scene.cycles.use_animated_seed = False
    scene.cycles.use_denoising = False
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 1
    scene.render.image_settings.color_depth = "8"
    if config.get("opaque_studio_material", False) is True:
        studio_opaque(actors + reference)

    scene.frame_set(rest_frame)
    rig.data.pose_position = "REST"
    with hide(reference):
        candidate = pair(scene, output, "candidate-rest", actors)
    with hide(actors):
        original = pair(scene, output, "reference-rest", reference)
    rig.data.pose_position = "POSE"
    limbs, posed = {}, {}
    with hide(reference):
        for frame in sorted(config["frames"]):
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            limbs[str(frame)] = coverage(scene, actors, rig, arms)
            if frame in config["shaded_frames"]:
                posed[str(frame)] = pair(scene, output, f"posed-{frame}", actors)
    return {key: config[key] for key in ("arms", "frames", "visibility_frames", "shaded_frames")} | {
        "rest": {"reference": original, "candidate": candidate}, "limbs": limbs, "posed": posed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    if not bpy.app.background:
        raise RuntimeError("studio_pose_capture_requires_background_blender")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = capture(bpy.context.scene, config, args.output)
    (args.output / "witnesses.json").write_text(
        json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
