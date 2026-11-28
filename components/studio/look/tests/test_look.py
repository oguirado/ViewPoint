# SPDX-License-Identifier: AGPL-3.0-only
"""Offline witness and background Blender checks for the studio LOOK CLI."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

import look
import measure
import vision_review

ARMS = {side: [f"{side}-{joint}" for joint in ("shoulder", "elbow", "wrist")] for side in ("left", "right")}


class WorkCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def cli(self, *argv, rc=0):
        run = subprocess.run([sys.executable, "-B", str(look.HERE / "look.py"), *map(str, argv)],
                             env=look.stateless_environment(self.root), capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, rc, run.stdout + "\n" + run.stderr[-12000:])
        payload = json.loads(run.stdout)
        if rc:
            self.assertTrue({"reason", "retry_when", "next_command"} <= payload.keys())
        return payload


class NegativeTests(WorkCase):
    def test_malformed_cli_is_structured_harness_error(self):
        payload = self.cli("build", rc=2)
        self.assertEqual(payload["status"], "ERROR")
        self.assertIn("--source", payload["reason"])

    def test_pose_fragments_and_limb_removal_are_rejected(self):
        shape = (80, 80, 4)
        img = np.full(shape, 180, dtype=np.uint8)
        img[:, :, 3] = 255
        bad = img.copy()
        bad[::2, :, :3] = 10

        def ref(name, data):
            path = self.root / (name + ".png")
            Image.fromarray(data).save(path)
            return {"path": path.name, "sha256": look.sha(path)}

        normal = ref("original", img)
        mask = ref("mask", img)
        damaged = ref("damaged", bad)
        complete = {joint: {"anatomy_samples": 9, "visible_samples": 9}
                    for chain in ARMS.values() for joint in chain}
        data = {"arms": ARMS, "frames": [1, 2], "visibility_frames": [2], "shaded_frames": [2],
                "rest": {"reference": {"image": normal, "mask": mask},
                         "candidate": {"image": normal, "mask": mask}},
                "limbs": {"1": complete, "2": copy.deepcopy(complete)},
                "posed": {"2": {"image": normal, "mask": mask}}}
        path = self.root / "witnesses.json"
        look.write(path, data)
        self.assertEqual(self.cli("pose-gate", "--config", path)["status"], "PASS")
        data["limbs"]["2"]["left-wrist"] = {"anatomy_samples": 0, "visible_samples": 0}
        data["posed"]["2"]["image"] = damaged
        look.write(path, data)
        rejected = self.cli("pose-gate", "--config", path, rc=1)
        self.assertEqual(rejected["report"]["reasons"], [
            "studio_render_limb_presence:2", "studio_render_limb_visibility:2",
            "studio_render_shading_fragmentation:2"])

    def test_silhouette_and_color_gate_reject_on_real_pixels(self):
        rgba = np.full((80, 80, 4), 255, dtype=np.uint8)
        damaged = rgba.copy()
        damaged[:, :10, 3] = 0
        damaged[:, :, :3] = 2
        row, _ = measure.compare(rgba, damaged)
        views = {f"{p}_{v}_neutral": row for p in ("ortho", "shot") for v in measure.VIEWS}
        views.update({f"ortho_{v}_{light}": row for v in measure.VIEWS[:3] for light in measure.LIGHTS[1:]})
        with self.assertRaisesRegex(look.Refused, "studio_silhouette_iou"):
            look.measured_gate({"views": views}, {}, "generic")
        row["iou"] = 1
        with self.assertRaisesRegex(look.Refused, "studio_rendered_color_delta"):
            look.measured_gate({"views": views, "front_landmark_regions": {},
                                "delta_e76_aggregate": {"mean": row["delta_e76_mean"], "p95": row["delta_e76_p95"]}},
                               {}, "generic")

    def test_panel_profile_checks_tip_and_streak_even_with_matching_views(self):
        features = {key: {"iou": 1} for key in look.FEATURES}
        streaks = {view: {"passes": True} for view in measure.TORSO_WINDOWS}
        features["antenna_tips"]["iou"] = .9
        streaks["flank"]["passes"] = False
        self.assertEqual(measure.quality_failures(features, streaks), ["antenna_tip_iou", "grazing_torso_streaks"])

    def test_review_requires_rgb_and_major_matches_reject(self):
        sheet = self.root / "contact-sheet.png"
        Image.new("RGB", (40, 45), (20, 50, 80)).save(sheet)
        request = vision_review.make_request("fixture", sheet, {name: look.sha(sheet)
                    for name in ("source", "selected", "metrics", "contact_sheet")})
        path = self.root / "request.json"
        look.write(path, request)
        response = {"schema": vision_review.RESPONSE_SCHEMA, "request_sha256": look.sha(path),
                    "run_id": "fixture", "contact_sheet_sha256": look.sha(sheet), "decision": "ACCEPT",
                    "pixel_access": True, "pixel_rgb": [[20, 50, 80]] * 3,
                    "observations": {view: "Fixture inspection." for view in vision_review.VIEWS},
                    "findings": [{"view": "front", "severity": "MAJOR", "detail": "Missing antenna."}]}
        with self.assertRaisesRegex(ValueError, "studio_vision_severity_decision_mismatch"):
            vision_review.verify_response(request, look.sha(path), sheet, response)
        response["decision"] = "REJECT"
        self.assertEqual(vision_review.verify_response(request, look.sha(path), sheet, response)["vision"], "REJECT")
        response["pixel_rgb"] = [[20, 51, 80]] * 3
        with self.assertRaisesRegex(ValueError, "studio_vision_pixel_access_unproven"):
            vision_review.verify_response(request, look.sha(path), sheet, response)


class BlenderTests(WorkCase):
    def setUp(self):
        super().setUp()
        self.engine = os.environ.get("STUDIO_TEST_BLENDER", "blender")
        result = subprocess.run([self.engine, "-b", "--factory-startup", "--disable-autoexec", "--threads", "1",
                                 "--python-exit-code", "2", "--python", str(look.HERE / "tests/fixture_blender.py"),
                                 "--", str(self.root)], env=look.stateless_environment(self.root),
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_build_resume_and_vision_disposition(self):
        source = self.root / "source.glb"
        camera = self.root / "camera.json"
        look.write(camera, {"camera": {"location_m": [2, -3, .6], "target_m": [0, 0, 0], "sensor_width_mm": 36},
                            "robot_framing": {"reference_height_m": 1}})
        bundle = self.root / "bundle"
        arguments = ("build", "--source", source, "--geometry", source, "--camera", camera, "--output", bundle,
                     "--blender", self.engine, "--profile", "generic", "--depth", 7, "--atlas-size", 256,
                     "--render-size", 96, "--samples", 3, "--smooth-iterations", 0, "--margin", 8)
        first = self.cli(*arguments)
        self.assertGreater(first["triangles"], 12)
        self.assertEqual(first, self.cli(*arguments))
        cold_bundle = self.root / "cold"
        cold_args = tuple(cold_bundle if value == bundle else value for value in arguments)
        cold = self.cli(*cold_args)
        warm_manifest, cold_manifest = (look.document(folder / "bundle.json") for folder in (bundle, cold_bundle))
        changed = [name for name, digest in warm_manifest["files"].items() if cold_manifest["files"][name] != digest]
        self.assertEqual(changed, [], {
            name: [(folder / name).read_text().splitlines()[:10] for folder in (bundle, cold_bundle)]
            for name in changed if name.endswith(".json")
        })
        self.assertEqual(first, cold)

        path = self.root / "request.json"
        response_path = self.root / "response.json"
        binding = ("--bundle", bundle, "--bundle-sha256", first["bundle_sha256"],
                   "--run-id", "fixture", "--request", path)
        request = self.cli("vision-request", *binding)
        self.assertEqual(request, self.cli("vision-request", *binding))
        decision = ("vision-gate", *binding, "--response", response_path)
        response = {"schema": vision_review.RESPONSE_SCHEMA, "request_sha256": request["request_sha256"],
                    "contact_sheet_sha256": look.sha(bundle / "contact-sheet.png"), "run_id": "fixture",
                    "decision": "AUSENTE", "pixel_access": False, "reason": "Offline fixture"}
        look.write(response_path, response)
        self.assertEqual(self.cli(*decision)["qualification"], "MEASURED_ONLY")
        _, pixels = vision_review.samples(bundle / "contact-sheet.png")
        del response["reason"]
        response.update(decision="ACCEPT", pixel_access=True, pixel_rgb=pixels,
                        observations={view: "Synthetic matching view." for view in vision_review.VIEWS},
                        findings=[{"view": "front", "severity": "MINOR", "detail": "Synthetic fleck."}])
        look.write(response_path, response)
        accepted = self.cli(*decision)
        self.assertEqual(accepted["qualification"], "MEASURED_AND_VISION")
        self.assertEqual(accepted["polish_backlog"], response["findings"])
        response.update(decision="REJECT", findings=[{"view": "front", "severity": "MAJOR", "detail": "Test hole."}])
        look.write(response_path, response)
        rejected = self.cli(*decision, rc=1)
        self.assertEqual(rejected["reason"], "studio_vision_rejected")
        self.assertNotIn("selected", rejected)
        with (bundle / "studio.glb").open("ab") as stream:
            stream.write(b"changed")
        self.assertEqual(self.cli(*decision, rc=1)["reason"], "studio_bundle_artifact_changed:studio.glb")

    def test_capture_registered_rest_and_projected_limbs(self):
        path = self.root / "pose.json"
        look.write(path, {"armature": "Rig", "actors": ["Actor"], "reference": ["Reference"],
                          "arms": ARMS, "frames": [1], "visibility_frames": [1], "shaded_frames": [1],
                          "rest_frame": 1})
        output = self.root / "pose"
        capture = ("pose-capture", "--config", path, "--blend", self.root / "pose.blend",
                   "--output", output, "--blender", self.engine)
        result = self.cli(*capture)
        self.assertEqual(result, self.cli(*capture))
        self.assertEqual(result["rest"]["silhouette_iou"], 1)
        self.assertEqual(result, self.cli("pose-gate", "--config", output / "witnesses.json"))


if __name__ == "__main__":
    unittest.main()
