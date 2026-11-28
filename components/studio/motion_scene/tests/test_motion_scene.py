# SPDX-License-Identifier: AGPL-3.0-only
"""Offline motion and scene controls, including CPU-only Blender readback."""
import copy
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import numpy as np

import animation_polish as polish
import bvh
import keyed_motion
import rest_adapter
import rest_projection
import retarget
import runner
import scope
from evidence import Rejected, digest, encode, file_sha

HERE = Path(__file__).resolve().parent
CLI = HERE.parent / "motion_scene.py"
PROGRAM = [sys.executable, str(HERE / "fixture_stage.py")]
PARENTS = [None, 0, 1, 2, 3, 4, 3, 6, 7, 8, 3, 10, 11, 12, 0, 14, 15, 16, 0, 18, 19, 20]


def set_doc():
    return {"units": "metres, Z up", "fps": 24, "end_frame": 288,
            "markers": {"HAND_CONTACT": 163, "BUTTON_BOTTOMED": 169, "BUTTON_RELEASED": 180},
            "button": {"object": "PLAY Button", "axis_world": [0, 1, 0], "travel_m": .038,
                       "travel_key_frames": [1, 163, 169, 180], "rest_contact_world_m": [.22, .10, .91],
                       "depressed_contact_world_m": [.22, .138, .91]},
            "screen": {"object": "CRT Screen", "on_emission_strength": 1.65},
            "robot_framing": {"approach_root_world_m": [-1.66, -.65, 0],
                              "plant_root_world_m": [-.39, -.44, 0]}}


def interaction_fixture():
    registration = set_doc()
    motion = {"schema": polish.SCHEMA, "settle_pelvis_delta_m": .08,
              "feet": {side: [{"frame": f, "stance_start": 1, "height_m": .0003, "slide_m": 0.}
                              for f in range(1, 289)] for side in ("left", "right")},
              "root_world_m": [], "hand_world_m": []}
    samples = []
    for frame in range(1, 289):
        y = .10 + .038 * polish.cap_fraction(frame)
        hand = [.22, y if 163 <= frame <= 180 else y - .06, .91]
        root = [-1.5 + min(frame / 120, 1), -.4, .48]
        samples.append({"frame": frame, "root_world_m": root.copy(), "cap_world_m": [.22, y, .91],
                        "hand_world_m": hand.copy(), "wrist_world_m": [.22, y - .05, .91],
                        "screen_emission": 0. if frame < 171 else 1.65})
        motion["root_world_m"].append(root)
        motion["hand_world_m"].append(hand)
    sightlines = {str(f): {"arm": {f"bone_{i}": {"visible_samples": 6, "uv": [.5, .5]} for i in polish.ARM},
                          "cap_visible": True, "screen_visible": True} for f in polish.SIGHT_FRAMES}
    return registration, motion, samples, sightlines, True


def glb_fixture():
    nodes = [{"name": f"bone_{i}", "translation": [.011, .025, -.008]} for i in range(len(PARENTS))]
    for child, parent in enumerate(PARENTS[1:], 1):
        nodes[parent].setdefault("children", []).append(child)
    doc = {"asset": {"version": "2.0"}, "nodes": nodes, "meshes": [],
           "skins": [{"joints": list(range(len(nodes)))}], "scenes": [{"nodes": [0]}], "scene": 0,
           "buffers": [{"byteLength": 4}], "bufferViews": [], "accessors": []}
    raw = encode(doc)
    raw += b" " * (-len(raw) % 4)
    return struct.pack("<4sIII4s", b"glTF", 2, 32 + len(raw), len(raw), b"JSON") + raw + b"\x04\0\0\0BIN\0\0\0\0\0"


class MotionSceneTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def cli(self, *argv, rc=0):
        result = subprocess.run([sys.executable, "-B", str(CLI), *map(str, argv)],
                                text=True, capture_output=True, check=False, timeout=180)
        self.assertEqual(result.returncode, rc, (result.stdout[-3000:], result.stderr[-5000:]))
        return json.loads(result.stdout)

    def test_layout_and_clay_cli_make_reopenable_artifacts(self):
        dest = self.root / "set"
        first = self.cli("layout", "--camera", "CAM_M", "--output", dest)
        self.assertEqual(first, self.cli("layout", "--camera", "CAM_M", "--output", dest))
        self.assertEqual(first["details"], {"camera": "CAM_M", "frames": 288, "fps": 24})
        registration = json.loads((dest / "SET.json").read_bytes())
        self.assertEqual(registration["camera"]["name"], "CAM_M")
        self.assertAlmostEqual(registration["button"]["travel_m"], .038)
        for mode in ("blocking", "final", "miss"):
            row = self.cli("clay", "--mode", mode, "--output", self.root / mode)
            measured = row["details"]
            self.assertEqual(measured["mode"], mode)
            self.assertEqual(measured["keyed_frames"] == 288, mode != "blocking")
            self.assertLessEqual(measured["max_ik_overreach_m"], .006)
            self.assertAlmostEqual(measured["peak_travel_m"], 0. if mode == "miss" else .038, places=6)
            self.assertEqual(measured["screen_on_frame"], None if mode == "miss" else 195)
        for row in first["artifacts"]:
            self.assertEqual(row["sha256"], file_sha(dest / row["path"]))

    def test_clay_movie_encoder_preserves_small_fixture_clock(self):
        import os

        if not all(shutil.which(exe) for exe in ("ffmpeg", "ffprobe")):
            self.fail("FFmpeg fixture dependencies are missing")
        expression = (
            "import bpy, sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(HERE.parent)!r}); "
            "from blender_worker import movie; "
            "scene = bpy.context.scene; scene.frame_end = 3; "
            "scene.render.resolution_x = 64; scene.render.resolution_y = 36; "
            "scene.cycles.samples = 1; "
            f"movie(scene, Path({str(self.root)!r}))"
        )
        env = scope.environment() | {"TMPDIR": str(self.root),
                                     "BLENDER_USER_CONFIG": str(self.root / "config")}
        result = subprocess.run(["blender", "-b", "--factory-startup", "--disable-autoexec",
                                 "-noaudio", "-t", "4", "--python-exit-code", "2",
                                 "--python-expr", expression], cwd=self.root, env=env,
                                capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stdout[-5000:] + result.stderr[-1000:])
        metadata = subprocess.check_output(
            ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(self.root / "clay.mp4")],
            text=True, env=os.environ)
        stream, = json.loads(metadata)["streams"]
        self.assertEqual((stream["codec_name"], stream["r_frame_rate"], stream["nb_read_frames"]),
                         ("h264", "24/1", "3"))

    def test_certified_rest_projection_and_named_glb_readback(self):
        raw = glb_fixture()
        rests, parents, nodes, origins, _ = retarget.target_rest(raw, rest_adapter)
        angles = np.linspace(.01, .2, retarget.COUNT)
        bases = retarget.Rotation.from_rotvec(np.column_stack(
            [angles, np.zeros(retarget.COUNT), np.zeros(retarget.COUNT)])).as_matrix()
        bases = np.broadcast_to(bases[:, None], (retarget.COUNT, len(nodes), 3, 3))
        roots = np.tile(origins[0], (retarget.COUNT, 1))
        roots[:, 0] += angles
        pos, local, _, _, projected = retarget.rest_playback(
            rest_projection, bvh, raw, rests, origins, parents, retarget.CMU, bases, roots)
        self.assertEqual(len(bvh.parse(projected).rows), 288)
        state = {"local": local, "nodes": nodes, "parents": parents, "positions": pos}
        output = retarget.export_gltf(raw, state, keyed_motion.ACTIONS)
        self.assertEqual(output, retarget.export_gltf(raw, state, keyed_motion.ACTIONS))
        replay = retarget.read_animation(raw, output, state, keyed_motion.ACTIONS)
        np.testing.assert_allclose(replay["world"][:, 0, :3, 3], roots, atol=1e-7)
        with self.assertRaisesRegex(Rejected, "rig_digest_mismatch"):
            rest_adapter.read_rest(raw, "0" * 64)

    def test_keyed_leg_solve_reaches_authored_stance(self):
        points = np.array([[0, .43, .02], [0, .20, .06], [0, .05, .01], [0, .03, .09]])
        bases = np.broadcast_to(np.eye(3), (4, 3, 3)).copy()
        target = np.array([.04, .07, -.03])
        keyed_motion.solve_leg(retarget, points, bases, range(4), target, np.eye(3), np.eye(3),
                               np.array([0., 0., 1.]), 0.)
        reconstructed = points[0] + bases[0] @ (points[1] - points[0]) + bases[1] @ (points[2] - points[1])
        np.testing.assert_allclose(reconstructed, target, atol=1e-10)
        np.testing.assert_allclose(np.linalg.det(bases), np.ones(4), atol=1e-10)

    def test_interaction_and_trajectory_accept_matched_frames(self):
        report = polish.assess_interaction(*interaction_fixture())
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["hand_contact_frames"], [163, 180])
        self.assertEqual(report["screen_on_frame"], 171)
        self.assertAlmostEqual(report["button_travel_m"], .038)

    def test_missing_evidence_resumes_without_repeating_passed_parents(self):
        plan = self.root / "plan.json"
        stages = [
            {"name": "layout", "parents": [], "argv": [*PROGRAM, "write", "{output}/set.txt", "layout"],
             "outputs": {"set": "set.txt"}, "evidence": []},
            {"name": "motion", "parents": ["layout"],
             "argv": [*PROGRAM, "copy", "{evidence:sample}", "{output}/clip.txt"],
             "outputs": {"clip": "clip.txt"}, "evidence": ["sample"]},
            {"name": "replay", "parents": ["motion"],
             "argv": [*PROGRAM, "copy", "{parent:motion:clip}", "{output}/scene.txt"],
             "outputs": {"scene": "scene.txt"}, "evidence": ["sample"],
             "producer": [*PROGRAM, "evidence", "{evidence_manifest}", "{stage}", "{plan_sha256}", "{parents}"]},
        ]
        plan.write_bytes(encode({"schema": runner.PLAN_SCHEMA, "stages": stages}))
        run = self.root / "run"
        missing = self.cli("run-morning", "--plan", plan, "--output", run, rc=1)
        self.assertEqual(missing["reason"], "evidence_missing:motion")
        first = run / "layout" / "receipt.json"
        identity = (file_sha(first), first.stat().st_ino)
        sample = self.root / "sample.txt"
        sample.write_text("synthetic motion\n", encoding="utf-8")
        manifest = self.root / "evidence.json"
        manifest.write_bytes(encode({"schema": runner.EVIDENCE_SCHEMA, "stage": "motion",
                                     "plan_sha256": file_sha(plan), "parents": {"layout": identity[0]},
                                     "files": {"sample": {"path": sample.name, "sha256": file_sha(sample)}}}))
        argv = ("run-morning", "--plan", plan, "--output", run)
        done = self.cli(*argv, "--evidence", f"motion={manifest}")
        self.assertEqual(done, self.cli(*argv))
        self.assertEqual(identity, (file_sha(first), first.stat().st_ino))
        self.assertEqual((run / "replay" / "scene.txt").read_bytes(), sample.read_bytes())
        self.assertEqual(set(done["stages"]), {"layout", "motion", "replay"})
        (run / "motion" / "clip.txt").write_text("altered\n", encoding="utf-8")
        self.assertEqual(self.cli(*argv, rc=1)["reason"], "artifact_sha256_mismatch")


class NegativeControls(unittest.TestCase):
    def test_feet_and_press_gates_have_specific_negatives(self):
        original = interaction_fixture()
        registration, motion, samples, sights, glyph = copy.deepcopy(original)
        motion["feet"]["left"][8]["slide_m"] = .011
        with self.assertRaisesRegex(Rejected, "polish_foot_slide"):
            polish.assess_interaction(registration, motion, samples, sights, glyph)

        for key, amount, reason in (
                ("root_world_m", .011, "polish_blender_root_replay"),
                ("hand_world_m", .0011, "polish_blender_hand_replay")):
            mutated = copy.deepcopy(original)
            mutated[2][35][key][0] += amount
            self.assertEqual(polish.assess_interaction(*mutated)["reasons"], [reason])
        mutated = copy.deepcopy(original)
        mutated[2][170]["screen_emission"] = 0.
        self.assertEqual(polish.assess_interaction(*mutated)["reasons"], ["polish_screen_outside_pressed_hold"])
        mutated = copy.deepcopy(original)
        mutated[2][167]["cap_world_m"][1] -= .002
        self.assertEqual(polish.assess_interaction(*mutated)["reasons"], ["polish_button_travel"])

    def test_rejects_live_foreground_and_untrusted_plan_path(self):
        with self.assertRaisesRegex(Rejected, "foreground_blender_refused"):
            runner.substitute(["blender", "--python", "fixture.py"], {})
        with self.assertRaisesRegex(Rejected, "reserved_stage_output"):
            runner.parse_plan(encode({"schema": runner.PLAN_SCHEMA, "stages": [{
                "name": "layout", "parents": [], "argv": PROGRAM,
                "outputs": {"set": "../outside"}, "evidence": []}]}))


def main():
    import os

    scope_record = scope.effective(int(os.environ["MOTION_SCENE_SCOPED_NICE"]))
    if sys.argv[1] == "negative":
        classes = (NegativeControls,)
    else:
        classes = (MotionSceneTests, NegativeControls)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(case) for case in classes)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({"tests": result.testsRun, "failures": len(result.failures),
                      "errors": len(result.errors), "scope": scope_record}, sort_keys=True))
    if not result.wasSuccessful():
        return 2
    return 1 if sys.argv[1] == "negative" else 0


if __name__ == "__main__":
    sys.exit(main())
