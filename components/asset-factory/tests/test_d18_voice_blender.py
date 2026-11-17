"""Run the assigned applier on a procedurally generated head in background Blender."""

from pathlib import Path
import json
import os
import sys
import subprocess
import tempfile
from time import perf_counter_ns
import unittest

if __name__ == "__main__":
    sys.path[:0] = [str(Path(__file__).resolve().parent.parent),
                    str(Path(__file__).resolve().parent)]

from asset_factory import core, proc


def _exercise_blender(out):
    import bpy

    from test_d18_voice_track import DURATION, RATE, SCRIPT, build, timing_fixture
    from tools.lipsync import VoiceError
    from tools.lipsync.blender_apply import apply_shape_keys
    from tools.lipsync.rhubarb import track_from_cues

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.render.fps = 24
    scene.render.fps_base = 1.0
    bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, location=(0, 0, 0))
    bpy.context.object.name = "VOICE procedural head"
    mesh = bpy.data.meshes.new("VOICE lips")
    mesh.from_pydata(
        [(-0.3, -0.9, -0.26), (0.3, -0.9, -0.26), (0.3, -0.9, -0.23),
         (-0.3, -0.9, -0.23), (-0.3, -0.9, -0.23), (0.3, -0.9, -0.23),
         (0.3, -0.9, -0.20), (-0.3, -0.9, -0.20)],
        [], [(0, 1, 2, 3), (4, 5, 6, 7)],
    )
    lips = bpy.data.objects.new("VOICE mouth fixture", mesh)
    scene.collection.objects.link(lips)
    lips.shape_key_add(name="Basis")
    keys = dict(closed="LipSeal", open="LipOpen", wide="LipWide", round="LipRound", bite="LipBite")
    lower_offsets = dict(closed=0, open=-0.06, wide=-0.03, round=-0.04, bite=-0.02)
    widths = dict(closed=0.9, open=1.0, wide=1.2, round=0.65, bite=1.0)
    for shape, name in keys.items():
        block = lips.shape_key_add(name=name)
        for index, vertex in enumerate(block.data):
            vertex.co.x *= widths[shape]
            if index < 4:
                vertex.co.z += lower_offsets[shape]
    jaw = lips.shape_key_add(name="JawOpen")
    for index in range(4):
        jaw.data[index].co.z -= 0.14
        jaw.data[index].co.y -= 0.02

    track = build()
    bad_pins = dict(keys, closed="missing")
    for bindings, start, name, fps in (
        (bad_pins, 7, "rig_shape_key_missing", 24),
        (keys, 7, "blender_timebase_changed", 25),
        (dict(keys, closed="LipOpen"), 7, "rig_shape_keys_ambiguous", 24),
    ):
        scene.render.fps = fps
        try:
            apply_shape_keys(track, lips, scene, first_frame=start, mouth_keys=bindings,
                             jaw_key="JawOpen")
        except VoiceError as exc:
            assert exc.code == name, exc.code
        else:
            raise AssertionError(f"rig negative was accepted: {name}")
        data = lips.data.shape_keys.animation_data
        assert data is None or data.action is None
    scene.render.fps = 24
    cues = json.dumps({"mouthCues": [
        {"start": onset / RATE, "end": (onset + DURATION) / RATE, "value": shape}
        for (onset, _, _), shape in zip(SCRIPT, "ADCEG", strict=True)
    ]}).encode()
    rhubarb = track_from_cues(timing_fixture(), cues, fps=(24, 1), silence_peak_pcm=40, max_frames=100)
    rhubarb_action = apply_shape_keys(rhubarb, lips, scene, first_frame=7, mouth_keys=keys, jaw_key="JawOpen")
    for row in rhubarb["frames"]:
        scene.frame_set(7 + row["index"])
        for shape, name in keys.items():
            actual = lips.data.shape_keys.key_blocks[name].value
            assert abs(actual - row["mouth_ppm"][shape] / 1_000_000) < 1e-6
    lips.data.shape_keys.animation_data_clear()
    bpy.data.actions.remove(rhubarb_action)

    action = apply_shape_keys(track, lips, scene, first_frame=7, mouth_keys=keys, jaw_key="JawOpen")
    peak = closed = neutral = 0
    for index, row in enumerate(track["frames"]):
        scene.frame_set(7 + index)
        checked = [*(lips.data.shape_keys.key_blocks[name] for name in keys.values()), jaw]
        for key, weight in zip(checked, [*(row["mouth_ppm"][s] for s in keys), row["jaw_ppm"]],
                               strict=True):
            assert abs(key.value - weight / 1_000_000) < 1e-6, (index, key.name, key.value)
        posed = lips.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.vertices
        gap = (posed[3].co - posed[4].co).length
        if row["mouth_ppm"]["closed"]:
            assert gap < 1e-6 and abs(posed[3].co.x + 0.27) < 1e-6
            closed += 1
        elif row["mouth_ppm"]["rest"]:
            assert gap < 1e-6 and abs(posed[3].co.x + 0.30) < 1e-6
            neutral += 1
        if row["jaw_ppm"]:
            assert gap > 0 and posed[3].co.y < -0.9
            peak = max(peak, row["jaw_ppm"])
    assert peak and closed and neutral

    scene.frame_set(10, subframe=0.5)
    assert jaw.value == 0.0
    assert lips.data.shape_keys.key_blocks["LipSeal"].value == 1.0
    try:
        apply_shape_keys(track, lips, scene, first_frame=7, mouth_keys=keys, jaw_key="JawOpen")
    except VoiceError as exc:
        assert exc.code == "rig_animation_exists"
    else:
        raise AssertionError("existing action was replaced")
    assert lips.data.shape_keys.animation_data.action == action

    fallback = bpy.data.objects.new("VOICE jaw-only fixture", mesh.copy())
    scene.collection.objects.link(fallback)
    fallback.shape_key_clear()
    fallback.shape_key_add(name="Basis")
    fallback_jaw = fallback.shape_key_add(name="JawOpen")
    for index in range(4):
        fallback_jaw.data[index].co.z -= 0.14
    fallback_track = build(alignment=None)
    apply_shape_keys(fallback_track, fallback, scene, first_frame=7, mouth_keys={}, jaw_key="JawOpen")
    for row in fallback_track["frames"]:
        scene.frame_set(row["index"] + 7)
        actual = fallback.data.shape_keys.key_blocks["JawOpen"].value
        assert abs(actual - row["jaw_ppm"] / 1_000_000) < 1e-6
        posed = fallback.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.vertices
        gap = (posed[3].co - posed[4].co).length
        assert abs(gap - 0.14 * actual) < 1e-6
    core.atomic_write_json(out / "blender.json", {
        "blender": bpy.app.version_string,
        "cycles_device": scene.cycles.device,
        "aligned_frames": len(track["frames"]),
        "fallback_frames": len(fallback_track["frames"]),
        "rhubarb_frames": len(rhubarb["frames"]),
        "closed_frames": closed,
        "neutral_frames": neutral,
        "peak_jaw_ppm": peak,
    })


class VoiceBlenderTests(unittest.TestCase):
    def test_cpu_blender_applies_controls_to_a_generic_head(self):
        from test_d18_voice_track import build, check, timing_fixture

        self.assertEqual(os.environ.get("AF_PROC_BACKEND"), "pidns")
        with tempfile.TemporaryDirectory(prefix="voice-blender-") as directory:
            out = Path(directory)
            job = proc.popen(
                ["blender", "--background", "--factory-startup", "--disable-autoexec",
                 "--threads", "1", "--python-exit-code", "5",
                 "--python", str(Path(__file__).resolve()), "--", str(out)],
                start_new_session=True, stderr=subprocess.STDOUT, runtime_s=45,
            )
            try:
                stdout, _ = proc.communicate(job, timeout=45)
                self.assertEqual(job.returncode, 0, stdout.decode(errors="replace"))
            finally:
                self.assertEqual(proc.cleanup(job), [])
            result = json.loads((out / "blender.json").read_text())
        self.assertEqual(result["aligned_frames"], len(build()["frames"]))
        self.assertEqual(result["fallback_frames"], len(build(alignment=None)["frames"]))
        self.assertEqual(result["rhubarb_frames"], len(build()["frames"]))
        self.assertEqual(result["cycles_device"], "CPU")
        print("VOICE_BLENDER", json.dumps(result, sort_keys=True), flush=True)
        for alignment in (True, None):
            started = perf_counter_ns()
            track = build(alignment=alignment)
            build_ns = perf_counter_ns() - started
            receipt = check(track, timing_fixture())
            self.assertEqual(receipt["fixture_verdict"], "PASS")
            print("VOICE_TIMING", json.dumps(receipt, sort_keys=True), flush=True)
            measured = {"source": track["source"], "build_ns": build_ns,
                        "track_bytes": len(core.canonical_json(track).encode("utf-8"))}
            print("VOICE_COST", json.dumps(measured, sort_keys=True), flush=True)


if __name__ == "__main__":
    _exercise_blender(Path(sys.argv[sys.argv.index("--") + 1]))
