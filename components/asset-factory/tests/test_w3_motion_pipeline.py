"""Synthetic contract qualification, never a claim of model inference."""

import contextlib
from copy import deepcopy
from decimal import Decimal
import hashlib
from importlib.util import find_spec
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from asset_factory import core, contracts
from providers.mocapanything_v2 import adapter, bvh, gate
from providers.mocapanything_v2 import offline
from providers.mocapanything_v2.build import recipe
from providers.mocapanything_v2.contract import ROOT, digest, inspect_rig, load


def glb(doc):
    chunk = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode()
    chunk += b" " * (-len(chunk) % 4)
    return struct.pack("<4sIII4s", b"glTF", 2, 20 + len(chunk), len(chunk), b"JSON") + chunk


def fixture(root, *, drift_um=8000, rotation_pop=False, static=False):
    root = Path(root)
    rig_doc = {"asset": {"version": "2.0"}, "nodes": [
        {"name": "Root", "translation": [0, 1, 0], "children": [1, 2]},
        {"name": "LeftFoot", "translation": [-0.1, -1, 0]},
        {"name": "RightFoot", "translation": [0.1, -1, 0]}], "skins": [{"joints": [0, 1, 2]}]}
    rig = glb(rig_doc)
    hierarchy = """HIERARCHY
ROOT Root
{
 OFFSET 0 1000 0
 CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation
 JOINT LeftFoot
 {
  OFFSET -100 -1000 0
  CHANNELS 3 Zrotation Xrotation Yrotation
  End Site { OFFSET 0 0 0 }
 }
 JOINT RightFoot
 {
  OFFSET 100 -1000 0
  CHANNELS 3 Zrotation Xrotation Yrotation
  End Site { OFFSET 0 0 0 }
 }
}
MOTION
Frames: 120
Frame Time: 0.03333333333333333333333333333
"""
    rows = []
    for i in range(120):
        x = Decimal(0) if static else Decimal(drift_um) * i / 119 / 1000
        rot = 180 if rotation_pop and i == 60 else 0
        rows.append(f"{x} 0 0 {rot} 0 0 0 0 0 0 0 0")
    data = (hierarchy + "\n".join(rows) + "\n").encode()
    reference = {"schema": "af.motion-reference/v1", "rig_sha256": digest(rig),
                 "basis": "RH_Y_UP", "units_um": 1000, "fps_num": 30, "fps_den": 1,
                 "frame_count": 120, "joints": [
                     {"name": "Root", "rig_name": "Root", "parent": None, "offset_um": [0, 1000000, 0]},
                     {"name": "LeftFoot", "rig_name": "LeftFoot", "parent": "Root", "offset_um": [-100000, -1000000, 0]},
                     {"name": "RightFoot", "rig_name": "RightFoot", "parent": "Root", "offset_um": [100000, -1000000, 0]}],
                 "feet": {"LeftFoot": [0, 0, 0], "RightFoot": [0, 0, 0]},
                 "contacts": {"LeftFoot": [1] * 120, "RightFoot": [1] * 120}, "floor_y_um": 0}
    (root / "rig.glb").write_bytes(rig)
    video = b"synthetic capture fixture, not encoded video"
    (root / "video.synthetic").write_bytes(video)
    request = {"schema": "af.motion/v1", "kind": "request", "slot": "motion", "request_id": "synthetic-motion",
               "provider_id": "mocapanything_v2", "provider_revision": load(ROOT / "provider.json")["revision"],
               "inputs": [{"role": "rig_glb", "path": str(root / "rig.glb"), "sha256": digest(rig), "media_type": "model/gltf-binary"},
                          {"role": "video", "path": str(root / "video.synthetic"), "sha256": digest(video), "media_type": "video/x-synthetic"}],
               "params": {"seed": 42, "reference_sha256": core.sha256_json(reference), "synthetic_fixture": True},
               "context": {"jurisdiction": "ES", "commercial_use": False, "policy_sha256": "a" * 64,
                           "toolchain_sha256": "b" * 64, "timeout_s": 10}}
    observation = {"schema": "af.motion-observation/v1", "measurement_kind": "synthetic_fixture",
                   "bvh_sha256": digest(data), "rig_sha256": digest(rig), "request_sha256": core.sha256_json(request),
                   "video_sha256": digest(video), "reference_sha256": core.sha256_json(reference), "frames": []}
    clip = bvh.parse(data)
    for i, row in enumerate(clip.rows):
        positions, rotations, _ = bvh.forward(clip, row, 1000)
        observation["frames"].append({"frame_id": i, "source_index": i,
            "source_rgb_sha256": digest(f"synthetic-rgb-{i}".encode()), "pose_sha256": clip.frame_hashes[i],
            "sole_um": {"LeftFoot": [round(v) for v in positions[1]], "RightFoot": [round(v) for v in positions[2]]},
            "contacts": {"LeftFoot": 1, "RightFoot": 1}})
    (root / "candidate.bvh").write_bytes(data)
    for name, obj in (("reference", reference), ("observation", observation), ("request", request)):
        core.atomic_write_json(root / (name + ".json"), obj)
    return {"data": data, "rig": rig, "reference": reference, "observation": observation, "request": request}


def run_gate(f, **kwargs):
    return gate.evaluate(f["data"], f["rig"], f["reference"], f["observation"],
                         request_sha256=core.sha256_json(f["request"]),
                         video_sha256=f["request"]["inputs"][1]["sha256"], scope="synthetic_fixture", **kwargs)


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.f = fixture(self.root)

    def assert_red_then_green(self, mutate, rc, reason=None):
        self.assertEqual(run_gate(self.f)["rc"], 0)
        bad = deepcopy(self.f)
        mutate(bad)
        result = run_gate(bad)
        self.assertEqual(result["rc"], rc, result)
        if reason:
            self.assertIn(reason, result["reason_codes"])
        self.assertEqual(run_gate(self.f)["rc"], 0)

    def accept(self, **kwargs):
        return adapter.accept_bvh(self.f["request"], self.root / "candidate.bvh", self.root / "reference.json",
                                  self.root / "observation.json", self.root / "published", **kwargs)

    def test_measured_positive_fixture(self):
        report = run_gate(self.f)
        self.assertEqual(report["rc"], 0, report)
        self.assertEqual(report["scope"], "synthetic_fixture")
        self.assertEqual(report["metrics"]["frames"], 120)
        self.assertEqual(report["metrics"]["max_stance_slide_um"], 8000)
        self.assertEqual(report["metrics"]["min_clearance_um"], 0)
        self.assertEqual(report["checks_evaluated"], 6)
        core.canonical_json(report)

    def test_planted_fifty_mm_drag_red_then_green(self):
        bad = fixture(self.root, drift_um=50000)
        self.assertEqual(run_gate(bad)["rc"], 5)
        self.assertIn("foot_slide", run_gate(bad)["reason_codes"])
        self.assertEqual(run_gate(fixture(self.root))["rc"], 0)

    def test_planted_rotation_pop_red_then_green(self):
        result = run_gate(fixture(self.root, rotation_pop=True))
        self.assertEqual(result["rc"], 5)
        self.assertIn("bone_rotation_continuity", result["reason_codes"])
        self.assertEqual(run_gate(fixture(self.root))["rc"], 0)

    def test_changed_pose_hash_is_stale(self):
        self.assert_red_then_green(lambda f: f["observation"]["frames"][60].update(pose_sha256="f" * 64), 4, "pose_frame_stale")

    def test_replayed_frame_identity_red_then_green(self):
        self.assert_red_then_green(lambda f: f["observation"]["frames"].__setitem__(60, deepcopy(f["observation"]["frames"][59])),
                                   5, "frame_identity_mismatch")

    def test_dropped_frame_red_then_green(self):
        self.assert_red_then_green(lambda f: f["observation"]["frames"].pop(60), 5, "frame_count_mismatch")

    def test_static_pose_is_not_replay(self):
        f = fixture(self.root, static=True)
        self.assertEqual(len(set(bvh.parse(f["data"]).frame_hashes)), 1)
        for frame in f["observation"]["frames"]:
            frame["source_rgb_sha256"] = "d" * 64
        self.assertEqual(run_gate(f)["rc"], 0)

    def test_contact_relabelling_cannot_hide_drag(self):
        self.assert_red_then_green(lambda f: f["observation"]["frames"][60]["contacts"].update(LeftFoot=0),
                                   5, "contact_relabelled")

    def test_missing_sole_surface_cannot_see(self):
        self.assert_red_then_green(lambda f: f["observation"]["frames"][60]["sole_um"].pop("LeftFoot"),
                                   3, "sole_surface_unobserved")

    def test_bone_only_evidence_cannot_see(self):
        self.assert_red_then_green(lambda f: f["observation"].update(measurement_kind="bone_only"),
                                   3, "sole_surface_unobserved")

    def test_sole_floor_penetration_red_then_green(self):
        def mutate(f):
            f["observation"]["frames"][60]["sole_um"]["LeftFoot"][1] = -5000
        self.assert_red_then_green(mutate, 5, "feet_floor")

    def test_stance_hover_red_then_green(self):
        def mutate(f):
            f["observation"]["frames"][60]["sole_um"]["LeftFoot"][1] = 20000
        self.assert_red_then_green(mutate, 5, "plant_height")

    def test_surface_trace_must_agree_with_fk(self):
        def mutate(f):
            f["observation"]["frames"][60]["sole_um"]["LeftFoot"][2] = 1500
        self.assert_red_then_green(mutate, 5, "sole_binding_mismatch")

    def test_longer_vector_is_refused(self):
        self.assert_red_then_green(lambda f: f["observation"]["frames"][60]["sole_um"]["LeftFoot"].append(4), 2, "vector_invalid")

    def test_float_receipt_refused(self):
        self.assert_red_then_green(lambda f: f["observation"]["frames"][60]["sole_um"]["LeftFoot"].__setitem__(0, 1.5), 2)

    def test_boolean_contact_refused(self):
        self.assert_red_then_green(lambda f: f["observation"]["frames"][60]["contacts"].update(LeftFoot=True), 2)

    def test_bound_request_drift(self):
        self.assert_red_then_green(lambda f: f["request"]["params"].update(seed=7), 4, "observation_stale")

    def test_input_video_drift(self):
        self.assert_red_then_green(lambda f: f["request"]["inputs"][1].update(sha256="e" * 64), 4, "observation_stale")

    def test_missing_stance_cannot_see(self):
        f = deepcopy(self.f)
        f["reference"]["contacts"]["LeftFoot"] = [0] * 120
        f["observation"]["reference_sha256"] = core.sha256_json(f["reference"])
        self.assertEqual(run_gate(f)["rc"], 3)
        self.assertIn("stance_unobserved", run_gate(f)["reason_codes"])

    def test_swapped_skeleton_red_then_green(self):
        f = deepcopy(self.f)
        f["reference"]["joints"][1]["rig_name"] = "RightFoot"
        f["reference"]["joints"][2]["rig_name"] = "LeftFoot"
        f["observation"]["reference_sha256"] = core.sha256_json(f["reference"])
        self.assertEqual(run_gate(f)["rc"], 5)
        self.assertIn("rest_pose_mismatch", run_gate(f)["reason_codes"])
        self.assertEqual(run_gate(self.f)["rc"], 0)

    def test_uncalibrated_real_surface_never_passes(self):
        obs = deepcopy(self.f["observation"])
        obs["measurement_kind"] = "skinned_surface"
        report = gate.evaluate(self.f["data"], self.f["rig"], self.f["reference"], obs,
                               request_sha256=core.sha256_json(self.f["request"]),
                               video_sha256=self.f["request"]["inputs"][1]["sha256"])
        self.assertEqual(report["rc"], 3)
        self.assertEqual(report["verdict"], "CANNOT_SEE")
        self.assertIn("contact_thresholds_uncalibrated", report["reason_codes"])

    def test_caller_cannot_weaken_frozen_policy(self):
        policy = load(ROOT / "contact-policy.json")
        policy["max_stance_slide_um"] = 1000000
        result = run_gate(self.f, policy=policy)
        self.assertEqual(result["rc"], 4)
        self.assertIn("policy_stale", result["reason_codes"])

    def test_bounded_gate_deadline(self):
        self.assertEqual(run_gate(self.f, deadline_ns=time.monotonic_ns() - 1)["rc"], 6)

    def test_slot_fixture_publication(self):
        result, proof = self.accept(synthetic_fixture=True)
        self.assertEqual(result["rc"], 0, proof)
        contracts.validate_slot(result, "motion", "result")
        target = self.root / "published"
        self.assertEqual((target / "action.bvh").read_bytes(), self.f["data"])
        self.assertEqual(load(target / "contact.json")["scope"], "synthetic_fixture")
        self.assertEqual(core.sha256_file(target / "contact.json"), result["receipts"][0]["sha256"])
        self.assertEqual((target / "result.json").read_text(), core.canonical_json(result))

    def test_refusal_preserves_existing_output_bytes(self):
        target = self.root / "published"
        target.mkdir()
        sentinel = target / "action.bvh"
        sentinel.write_bytes(b"caller bytes")
        result, proof = self.accept(synthetic_fixture=True)
        self.assertEqual(result["rc"], 5)
        self.assertEqual(sentinel.read_bytes(), b"caller bytes")
        self.assertEqual(list(target.iterdir()), [sentinel])

    def test_failed_gate_never_publishes(self):
        self.f = fixture(self.root, drift_um=50000)
        result, proof = self.accept(synthetic_fixture=True)
        self.assertEqual(result["rc"], 5)
        self.assertFalse((self.root / "published").exists())
        self.assertFalse(list(self.root.glob(".motion-stage-*")))

    def test_tampered_rig_refusal(self):
        (self.root / "rig.glb").write_bytes(b"changed")
        result, proof = self.accept(synthetic_fixture=True)
        self.assertEqual(result["rc"], 4)
        self.assertFalse((self.root / "published").exists())

    def test_tampered_video_refusal(self):
        (self.root / "video.synthetic").write_bytes(b"changed")
        self.assertEqual(self.accept(synthetic_fixture=True)[0]["rc"], 4)

    def test_synthetic_publication_requires_request_opt_in(self):
        self.f["request"]["params"].pop("synthetic_fixture")
        self.assertEqual(self.accept(synthetic_fixture=True)[0]["rc"], 2)

    def test_default_does_not_publish_synthetic_evidence(self):
        self.assertEqual(self.accept()[0]["rc"], 2)
        self.assertFalse((self.root / "published").exists())

    def test_stage_failure_cleans_staged_bytes(self):
        with patch.object(core, "atomic_write_json", side_effect=OSError("planted IO")):
            self.assertEqual(self.accept(synthetic_fixture=True)[0]["rc"], 2)
        self.assertFalse((self.root / "published").exists())
        self.assertFalse(list(self.root.glob(".motion-stage-*")))
        self.assertFalse(list(self.root.glob("*.motion-lock")))

    def test_racing_empty_output_directory_is_not_replaced(self):
        commit = adapter._commit_generation
        def race(stage, target):
            target.mkdir()
            commit(stage, target)
        with patch.object(adapter, "_commit_generation", side_effect=race):
            result, proof = self.accept(synthetic_fixture=True)
        self.assertEqual(result["rc"], 5)
        self.assertEqual(list((self.root / "published").iterdir()), [])
        self.assertFalse(list(self.root.glob(".motion-stage-*")))

    def test_staged_byte_tampering_red_then_green(self):
        write = core.atomic_write_json
        def tamper(path, obj):
            write(path, obj)
            if path.name == "contact.json":
                path.write_bytes(b"{}\n")
        with patch.object(core, "atomic_write_json", side_effect=tamper):
            result, proof = self.accept(synthetic_fixture=True)
        self.assertEqual(result["rc"], 4)
        self.assertFalse((self.root / "published").exists())
        self.assertEqual(self.accept(synthetic_fixture=True)[0]["rc"], 0)

    def test_no_inference_start_with_blocked_recipe(self):
        with patch("subprocess.Popen", side_effect=AssertionError("must not start")) as launch:
            result, proof = adapter.generate(self.f["request"])
        launch.assert_not_called()
        self.assertEqual(result["rc"], 3)
        self.assertEqual(proof["verdict"], "CANNOT_SEE")
        expected = [
            "motion_root_missing", "birefnet_root_missing", "source_checkout_missing",
            "dinov2_large_unpinned", "dependency_lock_missing", "runtime_image_unpinned",
            "birefnet_feature_bridge_unverified", "model_peak_unmeasured", "model_release_unproven",
        ]
        expected.extend(
            "runtime_missing_" + module
            for module in ("torch", "transformers", "safetensors", "timm")
            if find_spec(module) is None
        )
        self.assertCountEqual(proof["reason_codes"], expected)
        contracts.validate_slot(result, "motion", "result")

    def test_manifest_is_a_valid_blocked_motion_provider(self):
        provider = load(ROOT / "provider.json")
        contracts.validate_provider(provider)
        self.assertIs(provider["runnable"], False)
        self.assertIsNone(provider["runtime"]["ram_peak_gb_measured"])
        self.assertIsNone(provider["commercial"])

    def test_provider_revision_main_is_still_rejected(self):
        provider = load(ROOT / "provider.json")
        provider["revision"] = "main"
        with self.assertRaises(core.ContractError) as ctx:
            contracts.validate_provider(provider)
        self.assertEqual(ctx.exception.code, "unpinned_revision")

    def test_text_branch_refused(self):
        request = deepcopy(self.f["request"])
        request["inputs"][1] = {"role": "text", "text": "walk"}
        with self.assertRaises(bvh.Refusal) as ctx:
            adapter.generate(request)
        self.assertEqual(ctx.exception.rc, 2)
        self.assertEqual(ctx.exception.reason, "text_unsupported")

    def test_cli_usage_rc_two_never_one(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(adapter.main([]), 2)
            self.assertEqual(adapter.main(["accept-bvh"]), 2)

    def test_read_only_intake_reports_static_mesh_cannot_see(self):
        path = self.root / "static.glb"
        path.write_bytes(glb({"asset": {"version": "2.0"}, "nodes": []}))
        before = path.read_bytes()
        report = adapter.inspect_input(path)
        self.assertEqual(report["rc"], 3)
        self.assertIn("rig_skin_missing", report["reason_codes"])
        self.assertEqual(before, path.read_bytes())

    def test_intake_never_approves_skin_quality(self):
        self.assertEqual(adapter.inspect_input(self.root / "rig.glb")["rc"], 3)


class BvhTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = fixture(Path(self.temp.name))

    def test_analytic_fk_child_follows_root_rotation(self):
        clip = bvh.parse(self.f["data"])
        row = list(clip.rows[0])
        row[3] = Decimal(90)
        positions, _, _ = bvh.forward(clip, row, 1000)
        self.assertAlmostEqual(positions[1][0], 1000000)
        self.assertAlmostEqual(positions[1][1], 900000)

    def test_rotation_wrap_shortest_arc(self):
        self.assertLessEqual(bvh.rotation_delta_mdeg(bvh.rotation("Z", 359), bvh.rotation("Z", 1)), 2000)
        self.assertEqual(bvh.rotation_delta_mdeg(bvh.rotation("Y", 0), bvh.rotation("Y", 360)), 0)

    def test_malformed_bvh_mutants(self):
        data = self.f["data"]
        mutants = [(data.replace(b"JOINT LeftFoot", b"JOINT RightFoot"), 2),
                   (data.replace(b"Xposition Yposition Zposition", b"Xposition Xposition Zposition"), 2),
                   (data.replace(b"Frames: 120", b"Frames: 119"), 5),
                   (data.replace(b"Frame Time: 0.03333333333333333333333333333", b"Frame Time: NaN"), 2),
                   (data.replace(b"Frame Time: 0.03333333333333333333333333333", b"Frame Time: 0"), 2),
                   (data[:200], 2), (data.replace(b"0 0 0 0 0 0", b"inf 0 0 0 0 0", 1), 2)]
        for mutant, expected in mutants:
            with self.subTest(expected=expected, mutant=mutant[:35]):
                with self.assertRaises(bvh.Refusal) as ctx:
                    bvh.parse(mutant)
                self.assertEqual(ctx.exception.rc, expected)
        self.assertEqual(len(bvh.parse(data).rows), 120)

    def test_ascii_and_byte_budget(self):
        for data, rc in ((b"\xff", 2), (b"\0", 2), (b"", 6)):
            with self.assertRaises(bvh.Refusal) as ctx:
                bvh.parse(data)
            self.assertEqual(ctx.exception.rc, rc)

    def test_cyclic_skin_refused(self):
        bad = glb({"asset": {"version": "2.0"}, "nodes": [{"name": "a", "children": [1]},
                   {"name": "b", "children": [0]}], "skins": [{"joints": [0, 1]}]})
        with self.assertRaises(bvh.Refusal) as ctx:
            inspect_rig(bad)
        self.assertEqual(ctx.exception.rc, 2)

    def test_nonidentity_rest_rotation_is_unseen(self):
        bad = glb({"asset": {"version": "2.0"}, "nodes": [{"name": "a", "rotation": [1, 0, 0, 0]}],
                   "skins": [{"joints": [0]}]})
        with self.assertRaises(bvh.Refusal) as ctx:
            inspect_rig(bad)
        self.assertEqual(ctx.exception.rc, 3)


class OfflineTests(unittest.TestCase):
    def test_custom_code_drift_refuses_before_ml_import_or_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(offline, "verify_inventory", side_effect=bvh.Refusal(4, "weight_stale")), \
                    patch.object(offline, "install_network_guard", side_effect=AssertionError("must not install")):
                with self.assertRaises(bvh.Refusal) as ctx:
                    offline.load_birefnet(tmp)
                self.assertEqual(ctx.exception.rc, 4)

    def test_unlisted_custom_code_refuses_before_ml_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "unlisted.py").write_text("raise RuntimeError('must not import')\n")
            with patch.object(offline, "verify_inventory", return_value=[]), \
                    patch.object(offline, "install_network_guard", side_effect=AssertionError("must not install")):
                with self.assertRaises(bvh.Refusal) as ctx:
                    offline.load_birefnet(tmp)
                self.assertEqual(ctx.exception.rc, 4)
                self.assertEqual(ctx.exception.reason, "unlisted_model_code")

    def test_missing_local_root_cannot_see(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bvh.Refusal) as ctx:
                recipe.verify_inventory(Path(tmp) / "absent", {"revision": "c" * 40, "files": []})
            self.assertEqual(ctx.exception.rc, 3)

    def test_local_inventory_red_then_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weights.bin"
            path.write_bytes(b"pinned synthetic weights")
            inventory = {"revision": "c" * 40, "files": [{"path": path.name, "bytes": path.stat().st_size,
                                                          "sha256": core.sha256_file(path)}]}
            self.assertEqual(len(recipe.verify_inventory(tmp, inventory)), 1)
            path.write_bytes(b"X" + path.read_bytes()[1:])
            with self.assertRaises(bvh.Refusal) as ctx:
                recipe.verify_inventory(tmp, inventory)
            self.assertEqual(ctx.exception.rc, 4)
            path.write_bytes(b"pinned synthetic weights")
            self.assertEqual(len(recipe.verify_inventory(tmp, inventory)), 1)

    def test_path_escape_and_unpinned_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, revision, rc in (("../weights", "c" * 40, 2), ("weight", "main", 2)):
                inventory = {"revision": revision, "files": [{"path": name, "bytes": 1, "sha256": "a" * 64}]}
                with self.assertRaises(bvh.Refusal) as ctx:
                    recipe.verify_inventory(tmp, inventory)
                self.assertEqual(ctx.exception.rc, rc)

    def test_guard_blocks_rmbg_fetch_and_child_bypass(self):
        # Hook lifetime is process-wide; isolate it and bound the owned process.
        script = '''
import socket, subprocess
from providers.mocapanything_v2.offline import install_network_guard
from providers.mocapanything_v2.bvh import Refusal
install_network_guard()
blocked = 0
for call in [lambda: socket.getaddrinfo("huggingface.co", 443),
             lambda: subprocess.Popen(["true"])]:
    try: call()
    except Refusal: blocked += 1
print(blocked)
'''
        proc = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, timeout=10, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), b"2")

    def test_preflight_never_passes_missing_runtime(self):
        record = recipe.preflight()
        self.assertEqual(record["rc"], 3)
        self.assertEqual(record["verdict"], "CANNOT_SEE")
        self.assertIn("dinov2_large_unpinned", record["reason_codes"])


if __name__ == "__main__":
    unittest.main()
