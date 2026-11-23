"""Synthetic analytic rig fixtures; no model, downloads, Blender or real build."""
import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest import mock

from asset_factory import core
from asset_factory.contracts import validate_provider, validate_slot
from providers.unirig import adapter, gate
from providers.unirig.build import build
from providers.unirig.common import ROOT, Refusal, load, validate
from providers.unirig.publication import publish_directory


def sha(data):
    return hashlib.sha256(data).hexdigest()


def rig_fixture(*, weights=None, ibm=None, mutate=None):
    """A synthetic triangle and two joints with an analytic quarter-turn oracle."""
    binary, views, accessors = bytearray(), [], []

    def accessor(rows, kind, component, fmt):
        while len(binary) % 4:
            binary.append(0)
        start = len(binary)
        for row in rows:
            binary.extend(struct.pack("<" + fmt * len(row), *row))
        views.append({"buffer": 0, "byteOffset": start, "byteLength": len(binary)-start})
        accessors.append({"bufferView": len(views)-1, "componentType": component,
                          "count": len(rows), "type": kind})
        return len(accessors)-1

    positions = accessor([(0, 1, 0), (1, 1, 0), (2, 1, 0)], "VEC3", 5126, "f")
    joints = accessor([(0, 0, 0, 0), (1, 0, 0, 0), (1, 0, 0, 0)], "VEC4", 5121, "B")
    weights = accessor(weights or [(1, 0, 0, 0)]*3, "VEC4", 5126, "f")
    inverse = list(gate.IDENTITY)
    inverse[12] = -1
    binds = accessor(ibm or [gate.IDENTITY, inverse], "MAT4", 5126, "f")
    doc = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(binary)}],
           "bufferViews": views, "accessors": accessors,
           "meshes": [{"primitives": [{"attributes": {"POSITION": positions, "JOINTS_0": joints,
                                                        "WEIGHTS_0": weights}}]}],
           "nodes": [{"mesh": 0, "skin": 0}, {"name": "root", "children": [2]},
                     {"name": "hinge", "translation": [1, 0, 0]}],
           "skins": [{"joints": [1, 2], "inverseBindMatrices": binds}],
           "scenes": [{"nodes": [0, 1]}], "scene": 0}
    if mutate:
        mutate(doc, binary)
    return pack_glb(doc, binary)


def pack_glb(doc, binary):
    text = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    binary = bytes(binary) + b"\0" * (-len(binary) % 4)
    return (struct.pack("<4sII", b"glTF", 2, 28+len(text)+len(binary)) +
            struct.pack("<II", len(text), 0x4E4F534A) + text +
            struct.pack("<II", len(binary), 0x004E4942) + binary)


def policy():
    return load(ROOT / "gate-policy.json")


def probes(data, p=None):
    p = p or policy()
    pose = {"name": "analytic_hinge_quarter_turn", "local_matrices_ppm": {"2": [
        0, 1000000, 0, 0, -1000000, 0, 0, 0, 0, 0, 1000000, 0, 1000000, 0, 0, 1000000]},
        "samples": [{"mesh_node": 0, "primitive": 0, "vertex": 2, "expected_um": [0, 1000000, 0]}]}
    return {"schema": "af.rig-probes/v1", "rigged_sha256": sha(data),
            "policy_sha256": core.sha256_json(p), "reference_kind": "independent_expected_positions",
            "reference_sha256": core.sha256_json({"poses": [pose]}), "poses": [pose]}


class RigGateTests(unittest.TestCase):
    def check(self, data, rc=0, reason=None, p=None, ref=True):
        p = p or policy()
        result, skeleton = gate.inspect_bytes(data, policy=p, probes=probes(data, p) if ref else None)
        self.assertEqual(result["rc"], rc, result)
        self.assertEqual(result["status"], core.STATUS[rc])
        self.assertEqual(result["verdict"], core.VERDICT[rc])
        self.assertEqual(skeleton is not None, rc == 0)
        core.canonical_json(result)
        validate(result, "gate")
        if reason:
            self.assertIn(reason, result["reason_codes"])
        return result

    def test_analytic_bind_weights_and_deformation_pass(self):
        result = self.check(rig_fixture())
        self.assertEqual(result["metrics"], {"vertices": 3, "joints": 2, "weight_sum_error_ppm": 0,
                         "bind_error_um": 0, "deformation_error_um": 0, "deformation_samples": 1})

    def test_no_reference_cannot_see(self):
        self.check(rig_fixture(), 3, "deformation_reference_missing", ref=False)

    def test_skeleton_only_goes_red_then_clean_rig_green(self):
        data = rig_fixture(mutate=lambda d, b: d["meshes"].clear())
        self.check(data, 2)
        self.check(rig_fixture())

    def test_no_skin_goes_red(self):
        self.check(rig_fixture(mutate=lambda d, b: d["skins"].clear()), 5, "skin_missing")

    def test_missing_weights_goes_red(self):
        self.check(rig_fixture(mutate=lambda d, b: d["meshes"][0]["primitives"][0]["attributes"].pop("WEIGHTS_0")),
                   5, "skinning_attributes_missing")

    def test_bad_sum_goes_red_then_clean_rig_green(self):
        self.check(rig_fixture(weights=[(.95, 0, 0, 0), (1, 0, 0, 0), (1, 0, 0, 0)]), 5, "weight_sum")
        self.check(rig_fixture())

    def test_zero_negative_nan_infinite_weights(self):
        for w, rc in ((0, 5), (-1, 5), (float("nan"), 2), (float("inf"), 2)):
            with self.subTest(w=w):
                self.check(rig_fixture(weights=[(w, 0, 0, 0), (1, 0, 0, 0), (1, 0, 0, 0)]), rc)

    def test_joint_index_out_of_range(self):
        def mutate(d, b):
            view = d["bufferViews"][d["accessors"][1]["bufferView"]]
            b[view["byteOffset"]] = 255
        self.check(rig_fixture(mutate=mutate), 5, "joint_index")

    def test_wrong_bone_is_detected_by_deformation_not_weight_sum(self):
        def mutate(d, b):
            view = d["bufferViews"][d["accessors"][1]["bufferView"]]
            b[view["byteOffset"]+8] = 0
        self.check(rig_fixture(mutate=mutate), 5, "deformation_mismatch")
        self.check(rig_fixture())

    def test_lbs_half_turn_collapse_rejected_against_independent_radius_reference(self):
        # Analytic reference rotates the last point rigidly halfway through a
        # half-turn. Exported LBS with equal opposing palettes collapses it to
        # the pivot despite perfect weight sums. Never substitute a DQ kernel.
        data = rig_fixture(weights=[(1, 0, 0, 0), (1, 0, 0, 0), (.5, .5, 0, 0)])
        ref = probes(data)
        ref["poses"][0]["local_matrices_ppm"]["2"] = [
            -1000000, 0, 0, 0, 0, -1000000, 0, 0, 0, 0, 1000000, 0, 1000000, 0, 0, 1000000]
        ref["reference_sha256"] = core.sha256_json({"poses": ref["poses"]})
        result, _ = gate.inspect_bytes(data, policy=policy(), probes=ref)
        self.assertEqual(result["rc"], 5, result)
        self.assertEqual(result["metrics"]["weight_sum_error_ppm"], 0)
        self.assertIn("deformation_mismatch", result["reason_codes"])
        self.check(rig_fixture())

    def test_mutant_ignoring_inverse_binds_goes_red(self):
        # A translated joint makes ignoring the inverse bind observable.
        original = gate.mul
        def mutant(a, b):
            return a if b[12] == -1 else original(a, b)
        with mock.patch.object(gate, "mul", side_effect=mutant):
            with self.assertRaises(AssertionError):
                self.check(rig_fixture())
        self.check(rig_fixture())

    def test_singular_bind(self):
        self.check(rig_fixture(ibm=[gate.IDENTITY, [0]*16]), 5, "matrix_not_affine")
        singular = list(gate.IDENTITY)
        singular[0] = 0
        self.check(rig_fixture(ibm=[gate.IDENTITY, singular]), 5, "singular_bind_matrix")

    def test_nan_bind(self):
        matrix = list(gate.IDENTITY)
        matrix[0] = float("nan")
        self.check(rig_fixture(ibm=[gate.IDENTITY, matrix]), 2, "nonfinite_accessor")

    def test_rest_bind_drift(self):
        self.check(rig_fixture(ibm=[gate.IDENTITY, gate.IDENTITY]), 5, "bind_pose_mismatch")

    def test_bind_count(self):
        self.check(rig_fixture(ibm=[gate.IDENTITY]), 5, "inverse_bind_count")

    def test_hierarchy_cycle(self):
        self.check(rig_fixture(mutate=lambda d, b: d["nodes"][2].update(children=[1])), 5, "hierarchy_cycle")

    def test_unknown_parent_and_duplicate_child(self):
        self.check(rig_fixture(mutate=lambda d, b: d["nodes"][1].update(children=[8])), 5, "hierarchy_invalid")
        self.check(rig_fixture(mutate=lambda d, b: d["nodes"][1].update(children=[2, 2])), 5, "hierarchy_invalid")

    def test_unskinned_extra_mesh(self):
        self.check(rig_fixture(mutate=lambda d, b: d["nodes"].append({"mesh": 0})), 5, "unskinned_mesh")

    def test_extra_influences_are_not_silently_ignored(self):
        self.check(rig_fixture(mutate=lambda d, b: d["meshes"][0]["primitives"][0]["attributes"].update(WEIGHTS_1=2)),
                   3, "extra_influences_unsupported")

    def test_normalized_integer_weights(self):
        def mutate(d, b):
            a = d["accessors"][2]
            a.update(componentType=5121, normalized=True)
            view = d["bufferViews"][a["bufferView"]]
            for v in range(3):
                b[view["byteOffset"] + 4*v:view["byteOffset"] + 4*v + 4] = bytes([255, 0, 0, 0])
            view["byteLength"] = 12
        self.check(rig_fixture(mutate=mutate))

    def test_bad_accessor_bounds(self):
        self.check(rig_fixture(mutate=lambda d, b: d["accessors"][0].update(byteOffset=10000000)), 2, "accessor_bounds")

    def test_missing_triangles_and_bad_skeleton_root(self):
        self.check(rig_fixture(mutate=lambda d, b: d["accessors"][0].update(count=1)), 5, "triangles_missing")
        self.check(rig_fixture(mutate=lambda d, b: d["skins"][0].update(skeleton=9)), 2, "skeleton_root_invalid")
        self.check(rig_fixture(mutate=lambda d, b: d["skins"][0].update(skeleton=0)), 5, "skeleton_root_not_ancestor")

    def test_invalid_policy_float_and_malformed_document_fail_canonically(self):
        p = policy()
        p["bind_error_um"] = 1.5
        result, _ = gate.inspect_bytes(rig_fixture(), policy=p)
        self.assertEqual(result["rc"], 2)
        core.canonical_json(result)
        self.check(pack_glb([], b""), 2)

    def test_off_axis_bind_error_is_observed(self):
        inverse = list(gate.IDENTITY)
        inverse[12] = -1
        inverse[8] = 1
        self.check(rig_fixture(ibm=[gate.IDENTITY, inverse]), 5, "bind_pose_mismatch")

    def test_uri_and_compression_refused(self):
        self.check(rig_fixture(mutate=lambda d, b: d["buffers"][0].update(uri="external.bin")), 2, "buffer_invalid")
        self.check(rig_fixture(mutate=lambda d, b: d.update(extensionsRequired=["KHR_draco_mesh_compression"])),
                   3, "required_extension_unsupported")

    def test_header_and_truncation(self):
        self.check(b"not a GLB", 2, "glb_header")
        self.check(rig_fixture()[:-4], 2, "glb_header")

    def test_budget_and_policy_no_bool_as_integer(self):
        p = policy()
        p["max_vertices"] = 2
        self.check(rig_fixture(), 6, "accessor_budget", p=p)
        p["max_vertices"] = True
        self.check(rig_fixture(), 2, p=p)

    def test_probe_drift_and_empty_coverage(self):
        data, p = rig_fixture(), policy()
        ref = probes(data)
        ref["rigged_sha256"] = "0"*64
        self.assertEqual(gate.inspect_bytes(data, policy=p, probes=ref)[0]["rc"], 4)
        ref = probes(data)
        ref["poses"].clear()
        ref["reference_sha256"] = core.sha256_json({"poses": []})
        self.assertNotEqual(gate.inspect_bytes(data, policy=p, probes=ref)[0]["rc"], 0)

    def test_missing_observed_movement_cannot_pass(self):
        data = rig_fixture()
        ref = probes(data)
        ref["poses"][0]["samples"] = [{"mesh_node": 0, "primitive": 0, "vertex": 0,
                                        "expected_um": [0, 1000000, 0]}]
        ref["reference_sha256"] = core.sha256_json({"poses": ref["poses"]})
        self.assertEqual(gate.inspect_bytes(data, policy=policy(), probes=ref)[0]["rc"], 3)

    def test_quantized_receipt_does_not_round_failure_down(self):
        p = policy()
        p["weight_sum_error_ppm"] = 0
        self.check(rig_fixture(weights=[(.99999994, 0, 0, 0), (1, 0, 0, 0), (1, 0, 0, 0)]), 5, "weight_sum", p=p)

    def test_mutant_ignoring_sum_is_killed_by_unchanged_assertion(self):
        data = rig_fixture(weights=[(.95, 0, 0, 0), (1, 0, 0, 0), (1, 0, 0, 0)])
        original = gate.require
        def mutant(condition, rc, code):
            return None if code == "weight_sum" else original(condition, rc, code)
        self.check(data, 5, "weight_sum")
        with mock.patch.object(gate, "require", side_effect=mutant):
            with self.assertRaises(AssertionError):
                self.check(data, 5, "weight_sum")
        self.check(rig_fixture())


class OfflineBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.weights = self.root / "weights"
        self.weights.mkdir()
        self.recipe = load(ROOT / "build" / "recipe.json")

    def synthetic_build(self):
        recipe = copy.deepcopy(self.recipe)
        launch = b"# synthetic inert offline build fixture\n"
        recipe["launch_scripts_sha256"] = {k: sha(launch) for k in recipe["launch_scripts_sha256"]}
        recipe["weights"] = [{"repo": "synthetic/weights", "revision": "a"*40,
                              "files": [{"path": "skin.ckpt", "sha256": sha(b"synthetic") }]}]
        (self.weights / "skin.ckpt").write_bytes(b"synthetic")
        self.shipped = copy.deepcopy(recipe)
        with tarfile.open(self.inputs / "source.tar", "w") as archive:
            for name in recipe["launch_scripts_sha256"]:
                info = tarfile.TarInfo("synthetic/"+name)
                info.size = len(launch)
                archive.addfile(info, io.BytesIO(launch))
        recipe["source"]["archive_sha256"] = core.sha256_file(self.inputs / "source.tar")
        recipe["base_image"] = "synthetic/base@sha256:" + "a"*64
        recipe["native_extensions"] = {k: sha(k.encode()) for k in build.EXTENSIONS}
        (self.inputs / "requirements.lock").write_text("synthetic==1 --hash=sha256:" + "b"*64 + "\n")
        recipe["requirements_lock_sha256"] = core.sha256_file(self.inputs / "requirements.lock")
        (self.inputs / "wheels").mkdir()
        (self.inputs / "wheels/synthetic.whl").write_bytes(b"not installed by tests")
        recipe["wheel_files"] = [{"path": "wheels/synthetic.whl", "sha256": core.sha256_file(self.inputs / "wheels/synthetic.whl")}]
        for name, data, key in (("runtime-probe.json", {"rc": 0, "architecture": "aarch64", "extensions": recipe["native_extensions"]}, "runtime_probe_sha256"),
                                ("licence-review.json", {"rc": 0, "source_revision": recipe["source"]["revision"],
                                  "weights_sha256": core.sha256_json(recipe["weights"])}, "licence_review_sha256")):
            core.atomic_write_json(self.inputs / name, data)
            recipe[key] = core.sha256_file(self.inputs / name)
        original = build.load
        patch = mock.patch.object(build, "load", side_effect=lambda p: self.shipped if Path(p) == ROOT / "build/recipe.json" else original(p))
        patch.start()
        self.addCleanup(patch.stop)
        return recipe

    def test_shipped_manifest_valid_and_blocked(self):
        p = load(ROOT / "provider.json")
        validate_provider(p)
        self.assertIs(p["runnable"], False)
        self.assertIsNone(p["runtime"]["ram_peak_gb_measured"])
        self.assertEqual(len(p["weights"][0]["files"]), 2)

    def test_shipped_recipe_cannot_build_and_preserves_context(self):
        target = self.root / "existing"
        target.mkdir()
        (target / "sentinel").write_bytes(b"caller")
        result = build.prepare(self.recipe, self.inputs, target)
        self.assertEqual(result["rc"], 3)
        self.assertEqual((target / "sentinel").read_bytes(), b"caller")

    def test_complete_synthetic_offline_build_green(self):
        r = self.synthetic_build()
        result = build.preflight(r, self.inputs, weights_root=self.weights)
        self.assertEqual(result["rc"], 0, result)
        result = build.prepare(r, self.inputs, self.root / "context", weights_root=self.weights)
        self.assertEqual(result["rc"], 0, result)
        self.assertTrue((self.root / "context/Dockerfile").is_file())
        self.assertFalse((self.root / "context/skin.ckpt").exists())

    def test_floating_code_pin_and_missing_extension_red(self):
        r = self.synthetic_build()
        r["source"]["revision"] = "main"
        self.assertEqual(build.preflight(r, self.inputs, weights_root=self.weights)["rc"], 4)
        r["source"]["revision"] = self.recipe["source"]["revision"]
        r["native_extensions"]["flash_attn"] = None
        self.assertEqual(build.preflight(r, self.inputs, weights_root=self.weights)["rc"], 3)

    def test_tampered_wheel_red_then_restored_green(self):
        r = self.synthetic_build()
        wheel = self.inputs / "wheels/synthetic.whl"
        original = wheel.read_bytes()
        wheel.write_bytes(b"changed")
        self.assertEqual(build.preflight(r, self.inputs, weights_root=self.weights)["rc"], 4)
        wheel.write_bytes(original)
        self.assertEqual(build.preflight(r, self.inputs, weights_root=self.weights)["rc"], 0)

    def test_tampered_weight_and_symlink_escape(self):
        r = self.synthetic_build()
        weight = self.weights / "skin.ckpt"
        weight.write_bytes(b"changed")
        self.assertEqual(build.preflight(r, self.inputs, weights_root=self.weights)["rc"], 4)
        weight.unlink()
        (self.root / "outside").write_bytes(b"synthetic")
        weight.symlink_to(self.root / "outside")
        self.assertEqual(build.preflight(r, self.inputs, weights_root=self.weights)["rc"], 2)

    def test_source_archive_escape(self):
        r = self.synthetic_build()
        with tarfile.open(self.inputs / "source.tar", "w") as archive:
            info = tarfile.TarInfo("../escape")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        r["source"]["archive_sha256"] = core.sha256_file(self.inputs / "source.tar")
        self.assertEqual(build.preflight(r, self.inputs, weights_root=self.weights)["rc"], 2)

    def test_comment_only_requirements_lock_is_not_complete(self):
        r = self.synthetic_build()
        (self.inputs / "requirements.lock").write_text("# no pins\n")
        r["requirements_lock_sha256"] = core.sha256_file(self.inputs / "requirements.lock")
        result = build.preflight(r, self.inputs, weights_root=self.weights)
        self.assertEqual(result["rc"], 2, result)
        self.assertIn("requirements_not_hash_locked", result["reason_codes"])

    def request(self, data):
        territory = {"schema": "af.territory-groups/v1", "countries": ["ES"], "groups": {}}
        return {"schema": "af.rig/v1", "kind": "request", "slot": "rig", "request_id": "synthetic-case",
                "provider_id": "unirig", "provider_revision": self.recipe["source"]["revision"],
                "inputs": [{"role": "glb", "path": "input.glb", "media_type": "model/gltf-binary", "sha256": sha(data)}],
                "params": {"seed": 42}, "context": {"jurisdiction": "ES", "commercial_use": False,
                "policy_sha256": core.sha256_json(territory), "toolchain_sha256": "a"*64, "timeout_s": 1}}

    def runtime(self, *, script=None):
        r = self.synthetic_build()
        data = rig_fixture()
        (self.root / "input.glb").write_bytes(data)
        request = self.request(data)
        request["params"].update(synthetic_fixture=True, device="cpu")
        provider = load(ROOT / "provider.json")
        provider.update(runnable=True, commercial=True, gated=False, weights=r["weights"])
        provider.pop("blocked_reason")
        q = {"schema": "af.unirig-qualification/v1", "rc": 0, "recipe_sha256": core.sha256_json(r), "image_digest": "sha256:"+"c"*64,
             "peak_rss_bytes": 2**20, "wall_ms": 10}
        core.atomic_write_json(self.inputs / "qualification.json", q)
        ref = {"path": "qualification.json", "sha256": core.sha256_file(self.inputs / "qualification.json")}
        provider["runtime"].update(ram_peak_gb_measured=1, ram_peak_measurement_ref=ref)
        runner = self.inputs / "runner.py"
        if script is None:
            script = ("import argparse,base64\nfrom pathlib import Path\np=argparse.ArgumentParser()\n"
                      "p.add_argument('--request');p.add_argument('--output-dir');p.add_argument('--weights-root')\n"
                      "a=p.parse_args();out=Path(a.output_dir);out.mkdir()\n"
                      "(out/'rigged.glb').write_bytes(base64.b64decode(" + repr(__import__('base64').b64encode(data).decode()) + "))\n")
        runner.write_text(script)
        ready = build.preflight(r, self.inputs, weights_root=self.weights)
        self.assertEqual(ready["rc"], 0, ready)
        approval = {"schema": "af.unirig-build-approval/v1", "approved": True,
                    "recipe_sha256": core.sha256_json(r), "provider_sha256": core.sha256_json(provider),
                    "build_inputs_sha256": core.sha256_json(ready), "image_digest": q["image_digest"],
                    "capabilities": r["native_extensions"], "qualification": ref,
                    "runner": {"path": "runner.py", "sha256": core.sha256_file(runner)}}
        factory = {"jurisdiction": "ES", "commercial_use": False}
        territory = {"schema": "af.territory-groups/v1", "countries": ["ES"], "groups": {}}
        decision = {"rc": 0, "allowed": True, "verdict": "PASS", "provider_id": "unirig", "operation": "rig",
                    "commercial_use": False, "jurisdiction": "ES", "provider_sha256": core.sha256_json(provider),
                    "configuration_sha256": core.sha256_json(factory), "policy_sha256": core.sha256_json(territory)}
        admission = {"schema": "af.unirig-run-admission/v1", "rc": 0, "lease_held": True, "request_sha256": core.sha256_json(request),
                     "provider_sha256": core.sha256_json(provider)}
        return request, {"input_root": self.root, "output_dir": self.root / "output", "provider": provider,
                         "recipe": r, "policy": policy(), "probes": probes(data), "approval": approval,
                         "approved_build_sha256": core.sha256_json(approval), "build_inputs_root": self.inputs,
                         "weights_root": self.weights, "decision": decision, "factory": factory,
                         "territory_policy": territory, "admission": admission}

    def test_unapproved_request_launches_nothing_and_preserves_caller(self):
        request = self.request(rig_fixture())
        target = self.root / "output"
        target.mkdir()
        (target / "sentinel").write_bytes(b"caller")
        with mock.patch.object(adapter.subprocess, "Popen") as child:
            result = adapter.run(request, input_root=self.root, output_dir=target,
                                 provider=load(ROOT / "provider.json"), recipe=self.recipe, policy=policy(), probes=None)
            self.assertEqual(result["rc"], 3, result)
            child.assert_not_called()
        self.assertEqual((target / "sentinel").read_bytes(), b"caller")
        self.assertEqual(result["outputs"], [])

    def test_approved_synthetic_runner_measures_and_atomically_publishes(self):
        request, kw = self.runtime()
        result = adapter.run(request, **kw)
        self.assertEqual(result["rc"], 0, result)
        validate_slot(result, "rig", "result")
        self.assertGreater(result["metrics"]["peak_rss_bytes"], 0)
        out = kw["output_dir"]
        self.assertEqual(load(out / "result.json"), result)
        self.assertEqual((out / "raw.glb").read_bytes(), (self.root / "input.glb").read_bytes())
        self.assertEqual(len(result["outputs"]), 2)
        self.assertEqual(load(out / "gate.json")["input_sha256"], request["inputs"][0]["sha256"])
        self.assertFalse(list(self.root.glob(".unirig-*")))

    def test_approval_flag_alone_cannot_run(self):
        request, kw = self.runtime()
        kw["approved_build_sha256"] = None
        with mock.patch.object(adapter.subprocess, "Popen") as child:
            self.assertEqual(adapter.run(request, **kw)["rc"], 3)
            child.assert_not_called()

    def test_capability_or_approval_drift_launches_nothing(self):
        request, kw = self.runtime()
        kw["approval"]["capabilities"] = {"spconv": "b"*64}
        with mock.patch.object(adapter.subprocess, "Popen") as child:
            self.assertEqual(adapter.run(request, **kw)["rc"], 4)
            child.assert_not_called()
        kw["approved_build_sha256"] = core.sha256_json(kw["approval"])
        with mock.patch.object(adapter.subprocess, "Popen") as child:
            self.assertEqual(adapter.run(request, **kw)["rc"], 3)
            child.assert_not_called()

    def test_licence_and_scheduler_refusals_precede_subprocess(self):
        request, kw = self.runtime()
        kw["decision"]["allowed"] = False
        with mock.patch.object(adapter.subprocess, "Popen") as child:
            self.assertEqual(adapter.run(request, **kw)["rc"], 5)
            child.assert_not_called()
        kw["decision"]["allowed"] = True
        kw["admission"] = None
        with mock.patch.object(adapter.subprocess, "Popen") as child:
            self.assertEqual(adapter.run(request, **kw)["rc"], 3)
            child.assert_not_called()

    def test_backend_timeout_cleans_up_and_preserves_caller(self):
        request, kw = self.runtime(script="import time\ntime.sleep(10)\n")
        result = adapter.run(request, **kw)
        self.assertEqual(result["rc"], 6, result)
        self.assertFalse(kw["output_dir"].exists())
        self.assertFalse(list(self.root.glob(".unirig-*")))

    def test_exit_one_and_hidden_engine_error_never_pass(self):
        request, kw = self.runtime(script="print('ERROR: synthetic engine refusal')\n")
        self.assertEqual(adapter.run(request, **kw)["rc"], 5)
        (self.inputs / "runner.py").write_text("raise SystemExit(1)\n")
        kw["approval"]["runner"]["sha256"] = core.sha256_file(self.inputs / "runner.py")
        kw["approved_build_sha256"] = core.sha256_json(kw["approval"])
        self.assertEqual(adapter.run(request, **kw)["rc"], 5)

    def test_forged_deformation_does_not_publish(self):
        request, kw = self.runtime()
        kw["probes"]["poses"][0]["samples"][0]["expected_um"] = [0, 0, 0]
        kw["probes"]["reference_sha256"] = core.sha256_json({"poses": kw["probes"]["poses"]})
        self.assertEqual(adapter.run(request, **kw)["rc"], 5)
        self.assertFalse(kw["output_dir"].exists())

    def test_no_overwrite_even_for_empty_existing_directory(self):
        first, target = self.root / "first", self.root / "target"
        first.mkdir()
        target.mkdir()
        with self.assertRaises(OSError):
            publish_directory(first, target)
        self.assertTrue(first.is_dir())
        self.assertTrue(target.is_dir())


if __name__ == "__main__":
    unittest.main()
