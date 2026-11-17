"""S2-U1 gates: deterministic effects, surface support and adversarial controls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asset_factory import core
from tools.sculpt.propose import ProposalError, mesh_digest, propose, verify_generator


def fixture():
    mesh = {"schema": "af.sculpt-mesh/v1", "units": "um", "frame": "mesh_local",
            "vertices_um": [[0, 0, 0], [100000, 0, 0], [0, 100000, 0], [0, 0, 100000]],
            "triangles": [[0, 1, 3], [1, 2, 3], [2, 0, 3]]}
    regions = [{"id": "rim", "faces": [0, 1, 2]}]
    target = copy.deepcopy(mesh)
    target["vertices_um"][3] = [5000, 0, 90000]
    reference = {"schema": "af.sculpt-reference/v1", "source_mesh_sha256": mesh_digest(mesh),
                 "mapping": "vertex_index", "mesh": target}
    return mesh, regions, reference


class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.mesh, self.regions, self.reference = fixture()

    def call(self, **kwargs):
        args = dict(regions=self.regions, seed=17, reference=self.reference)
        args.update(kwargs)
        return propose(self.mesh, **args)

    def test_eight_candidates_from_all_three_signals(self):
        result = self.call()
        self.assertEqual(result["rc"], 0)
        candidates = result["regions"][0]["candidates"]
        self.assertEqual(len(candidates), 8)
        self.assertEqual({c["generator"] for c in candidates}, {"residual", "curvature", "boundary"})
        self.assertEqual(len({c["id"] for c in candidates}), 8)

    def test_repeatability_and_seed_sensitivity(self):
        original = copy.deepcopy((self.mesh, self.regions, self.reference))
        a = self.call()
        self.assertEqual(core.canonical_json(a), core.canonical_json(self.call()))
        b = self.call(seed=18)
        self.assertNotEqual(a["candidates_sha256"], b["candidates_sha256"])
        self.assertNotEqual(a["regions"][0]["candidates"][0]["path"],
                            b["regions"][0]["candidates"][0]["path"])
        self.assertEqual((self.mesh, self.regions, self.reference), original)

    def test_paths_are_surface_coordinates_and_steps_are_bounded(self):
        result = self.call()
        for c in result["regions"][0]["candidates"]:
            self.assertGreater(sum(v*v for v in c["delta_um"]), 0)
            self.assertLessEqual(sum(v*v for v in c["delta_um"]), result["max_step_um"]**2)
            self.assertEqual(c["mesh_sha256"], mesh_digest(self.mesh))
            for anchor in c["path"]:
                self.assertIn(anchor["face"], self.regions[0]["faces"])
                self.assertEqual(sum(anchor["barycentric_ppm"]), 1000000)
                self.assertTrue(all(type(v) is int and 0 <= v <= 1000000
                                    for v in anchor["barycentric_ppm"]))
                if c["generator"] == "boundary":
                    self.assertIn(0, anchor["barycentric_ppm"])

    def test_residual_points_toward_corresponding_reference(self):
        for c in self.call()["regions"][0]["candidates"]:
            if c["generator"] == "residual":
                self.assertGreater(c["delta_um"][0], 0)
                self.assertLess(c["delta_um"][2], 0)
                self.assertEqual(c["reference_sha256"], mesh_digest(self.reference["mesh"]))

    def test_two_regions_get_eight_each_and_order_is_canonical(self):
        regions = [{"id": "z", "faces": [0, 1]}, {"id": "a", "faces": [1, 2]}]
        a = self.call(regions=regions)
        b = self.call(regions=[{"id": "a", "faces": [2, 1]}, {"id": "z", "faces": [1, 0]}])
        self.assertEqual(a, b)
        self.assertEqual([len(r["candidates"]) for r in a["regions"]], [8, 8])

    def test_missing_reference_is_incomplete_without_fabricated_residual(self):
        result = self.call(reference=None)
        self.assertEqual(result["rc"], 3)
        self.assertIn("reference_unavailable", result["reason_codes"])
        self.assertEqual(len(result["regions"][0]["candidates"]), 8)
        self.assertNotIn("residual", {c["generator"] for c in result["regions"][0]["candidates"]})

    def test_closed_mesh_has_observed_zero_boundary(self):
        self.mesh["triangles"].append([0, 2, 1])
        self.reference["mesh"]["triangles"].append([0, 2, 1])
        self.reference["source_mesh_sha256"] = mesh_digest(self.mesh)
        result = self.call(regions=[{"id": "all", "faces": [0, 1, 2, 3]}])
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["regions"][0]["signals"]["boundary_edges"], 0)
        self.assertNotIn("boundary", {c["generator"] for c in result["regions"][0]["candidates"]})

    def test_region_cut_is_not_a_mesh_boundary(self):
        self.mesh["triangles"].append([0, 2, 1])
        self.reference["mesh"]["triangles"].append([0, 2, 1])
        self.reference["source_mesh_sha256"] = mesh_digest(self.mesh)
        result = self.call(regions=[{"id": "one", "faces": [0]}])
        self.assertEqual(result["regions"][0]["signals"]["boundary_edges"], 0)

    def test_stale_reference_and_topology_mismatch_refused(self):
        ref = copy.deepcopy(self.reference)
        ref["source_mesh_sha256"] = "0"*64
        with self.assertRaises(ProposalError) as e:
            self.call(reference=ref)
        self.assertEqual(e.exception.rc, 4)
        ref = copy.deepcopy(self.reference)
        ref["mesh"]["triangles"][0] = [1, 0, 3]
        with self.assertRaises(ProposalError) as e:
            self.call(reference=ref)
        self.assertEqual(e.exception.rc, 3)

    def test_invalid_inputs_and_budgets(self):
        mutations = [("seed", True, 2), ("seed", 1.5, 2), ("regions", [], 3),
                     ("regions", [{"id": "x", "faces": [99]}], 2),
                     ("regions", [{"id": "x", "faces": [0, 0]}], 2),
                     ("policy", {"k": 9}, 6), ("policy", {"max_vertices": 3}, 6),
                     ("policy", {"timeout_ms": 0}, 2), ("policy", {"typo": 3}, 2)]
        for key, value, rc in mutations:
            with self.subTest(key=key, value=value), self.assertRaises(ProposalError) as e:
                self.call(**{key: value})
            self.assertEqual(e.exception.rc, rc)
        for vertex in [[True, 0, 0], [0.5, 0, 0]]:
            mesh = copy.deepcopy(self.mesh)
            mesh["vertices_um"][0] = vertex
            with self.assertRaises(ProposalError) as e:
                propose(mesh, regions=self.regions, seed=17)
            self.assertEqual(e.exception.rc, 2)

    def test_degenerate_and_nonmanifold_geometry_refused(self):
        for triangles in [[[0, 0, 1]], [[0, 1, 3]]*3]:
            mesh = copy.deepcopy(self.mesh)
            mesh["triangles"] = triangles
            with self.assertRaises(ProposalError) as e:
                propose(mesh, regions=[{"id": "x", "faces": [0]}], seed=1)
            self.assertEqual(e.exception.rc, 2)

    def test_gate_accepts_real_generator(self):
        gate = verify_generator(self.mesh, regions=self.regions, reference=self.reference)
        self.assertEqual(gate["rc"], 0)
        self.assertGreater(gate["checks_evaluated"], 0)

    def test_gate_derives_a_distinct_seed_for_every_valid_start(self):
        for seed in (18, 2**64-1):
            gate = verify_generator(self.mesh, regions=self.regions, reference=self.reference, seed=seed)
            self.assertEqual(gate["rc"], 0)
        self.assertEqual(verify_generator(self.mesh, regions=self.regions, reference=self.reference,
                                         policy={"k": 9})["rc"], 6)

    def test_gate_mutant_ignoring_seed_goes_red(self):
        def ignores_seed(mesh, **kwargs):
            kwargs["seed"] = 17
            return propose(mesh, **kwargs)
        gate = verify_generator(self.mesh, regions=self.regions, reference=self.reference,
                                generator=ignores_seed)
        self.assertEqual(gate["rc"], 5)
        self.assertIn("seed_ignored", gate["reason_codes"])

    def test_gate_mutant_changing_only_metadata_goes_red(self):
        def metadata_only(mesh, **kwargs):
            seed = kwargs["seed"]
            kwargs["seed"] = 17
            result = propose(mesh, **kwargs)
            result["seed"] = seed
            for c in result["regions"][0]["candidates"]:
                c["id"] = str(seed) + c["id"]
            result["candidates_sha256"] = core.sha256_json(result["regions"])
            return result
        gate = verify_generator(self.mesh, regions=self.regions, reference=self.reference,
                                generator=metadata_only)
        self.assertEqual(gate["rc"], 5)

    def test_gate_mutant_changing_off_surface_path_goes_red(self):
        def off_surface(mesh, **kwargs):
            result = propose(mesh, **kwargs)
            result["regions"][0]["candidates"][0]["path"][0]["barycentric_ppm"] = [1, 1, 1]
            return result
        gate = verify_generator(self.mesh, regions=self.regions, reference=self.reference,
                                generator=off_surface)
        self.assertEqual(gate["rc"], 5)

    def test_gate_never_passes_empty_or_missing_evidence(self):
        def empty(mesh, **kwargs):
            result = propose(mesh, **kwargs)
            result["regions"] = []
            return result
        self.assertEqual(verify_generator(self.mesh, regions=self.regions,
                                         reference=self.reference, generator=empty)["rc"], 5)
        self.assertEqual(verify_generator(self.mesh, regions=self.regions, reference=None)["rc"], 3)

    def test_gate_does_not_trust_a_forged_displacement_ceiling(self):
        def forges_cap(mesh, **kwargs):
            result = propose(mesh, **kwargs)
            result["max_step_um"] *= 10
            return result
        gate = verify_generator(self.mesh, regions=self.regions, reference=self.reference,
                                generator=forges_cap)
        self.assertEqual(gate["rc"], 5)
        self.assertEqual(gate["reason_codes"], ["proposal_binding_invalid"])

    def test_timeout_is_budget_exceeded(self):
        with mock.patch("tools.sculpt.propose.api.time.monotonic_ns", side_effect=[0, 2000000]):
            with self.assertRaises(ProposalError) as error:
                self.call(policy={"timeout_ms": 1})
        self.assertEqual(error.exception.rc, 6)

    def test_proposals_conform_to_the_shipped_closed_schema(self):
        schema_path = Path(__file__).resolve().parents[1]/"tools/sculpt/propose/proposal.schema.json"
        schema = core.load_json(schema_path, max_bytes=2**20)
        result = self.call()
        core.validate(result, schema)
        result["regions"][0]["candidates"][0]["screen_x"] = 1
        with self.assertRaises(core.ContractError):
            core.validate(result, schema)

    def test_cli_refusal_preserves_output_and_success_is_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory)/"request.json"
            output = Path(directory)/"output.json"
            data = {"schema": "af.sculpt-propose-request/v1", "mesh": self.mesh,
                    "regions": self.regions, "reference": self.reference, "seed": 17, "policy": {}}
            request.write_text(core.canonical_json(data))
            command = [sys.executable, "-B", "-m", "tools.sculpt.propose", "--request", str(request),
                       "--out", str(output)]
            p = subprocess.run(command, capture_output=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
            self.assertEqual(p.stdout, output.read_bytes())
            self.assertEqual(p.stdout.decode(), core.canonical_json(json.loads(p.stdout)))
            output.write_bytes(b"caller bytes\x00")
            data["reference"] = None
            request.write_text(core.canonical_json(data))
            p = subprocess.run(command, capture_output=True, timeout=20)
            self.assertEqual(p.returncode, 3)
            self.assertEqual(output.read_bytes(), b"caller bytes\x00")
            request.write_text('{"seed":1,"seed":2}')
            p = subprocess.run(command, capture_output=True, timeout=20)
            self.assertEqual(p.returncode, 2)
            self.assertEqual(output.read_bytes(), b"caller bytes\x00")

    def test_digest_equal_in_fresh_interpreters_with_different_hash_seeds(self):
        code = ('from test_ws_s2_u1_propose import fixture; '
                'from tools.sculpt.propose import propose; m,r,f=fixture(); '
                'print(propose(m,regions=r,seed=17,reference=f)["candidates_sha256"])')
        outputs = []
        for seed in ("1", "39"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=os.pathsep.join([".", "tests"]))
            p = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                               capture_output=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stderr)
            outputs.append(p.stdout)
        self.assertEqual(*outputs)


if __name__ == "__main__":
    unittest.main()
