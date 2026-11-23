"""Asset-bound topology policy; independent red controls, no engine relaxation."""
import copy
import math
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_f2u1_mesh import glb, policy, mutant
from tools.mesh import api, contracts, remesh, remesh_gate


def torus():
    # Parametric synthetic fixture, not a production asset or model output.
    n, m = 24, 12
    points = []
    for i in range(n):
        u = 2 * math.pi * i / n
        for j in range(m):
            v = 2 * math.pi * j / m
            points.append(tuple(round(x * 1000000) for x in
                                ((1+.3*math.cos(v))*math.cos(u),
                                 (1+.3*math.cos(v))*math.sin(u), .3*math.sin(v))))
    faces = []
    for i in range(n):
        for j in range(m):
            a, b = i*m+j, ((i+1)%n)*m+j
            c, d = ((i+1)%n)*m+(j+1)%m, i*m+(j+1)%m
            faces.extend(((a,b,c),(a,c,d)))
    return glb([{"name":"body", "verts":points, "faces":faces}])


def expectation(raw, genus=1):
    return {"schema":"af.asset-topology-expectation/v1", "asset_id":"synthetic-handle",
            "asset_class":"closed_fixture_with_one_handle", "source_sha256":contracts.digest(raw),
            "reference":{"path":"synthetic-reference", "sha256":"a"*64},
            "genus_by_primitive":{"body":genus},
            "rationale":"Declared one handle before evaluating any candidate."}


class ExactTopologyTests(unittest.TestCase):
    def test_receipt_uses_only_registered_mesh_schema_fields(self):
        raw=torus()
        result=remesh_gate.evaluate(raw,raw,policy(),expectation(raw))
        schema=json.loads(Path(api.__file__).with_name("mesh-regression.schema.json").read_text())
        self.assertLessEqual(set(result),set(schema["properties"]))
        self.assertLessEqual(set(result["metrics"]),set(schema["properties"]["metrics"]["properties"]))

    def test_legacy_refuses_torus_then_declared_exact_handle_passes(self):
        raw = torus()
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary)/"torus.glb"; p.write_bytes(raw)
            self.assertEqual(api.compare_meshes(p,p,policy=policy())[0], 5)
        result = remesh_gate.evaluate(raw,raw,policy(),expectation(raw))
        self.assertEqual(result["rc"],0,result)
        self.assertEqual(result["metrics"]["component_inventories"][0]["genus"],1)
        self.assertEqual(result["policy_sha256"],remesh_gate.policy_identity(policy(),expectation(raw)))

    def test_extra_and_removed_handle_red_then_exact_green(self):
        raw = torus()
        for declared in (0,2):
            result = remesh_gate.evaluate(raw,raw,policy(),expectation(raw,declared))
            self.assertEqual(result["rc"],5,result)
            self.assertIn("topology:genus_expectation:body",result["failed"])
        self.assertEqual(remesh_gate.evaluate(raw,raw,policy(),expectation(raw))["rc"],0)

    def test_wrong_source_is_stale_and_boolean_or_omitted_genus_is_error(self):
        raw = torus(); declared = expectation(raw)
        declared["source_sha256"] = "b"*64
        self.assertEqual(remesh_gate.evaluate(raw,raw,policy(),declared)["rc"],4)
        for genus in (True, None, -1):
            declared = expectation(raw,genus)
            self.assertEqual(remesh_gate.evaluate(raw,raw,policy(),declared)["rc"],2)
        declared=expectation(raw); declared["genus_by_primitive"]={}
        self.assertEqual(remesh_gate.evaluate(raw,raw,policy(),declared)["rc"],2)

    def test_declared_component_count_is_exact(self):
        raw = glb([{"name":"body"},{"name":"extra", "translation":[3,0,0]}])
        declared=expectation(raw,0)
        result=remesh_gate.evaluate(raw,raw,policy(),declared)
        self.assertEqual(result["rc"],5,result)
        self.assertIn("component_inventory_mismatch",result["failed"])

    def test_declaring_handle_never_bypasses_shape_or_closedness(self):
        raw = torus()
        candidate = glb()
        result = remesh_gate.evaluate(raw,candidate,policy(),expectation(raw))
        self.assertEqual(result["rc"],5,result)
        self.assertIn("hausdorff_relative",result["failed"])
        self.assertIn("silhouette_iou",result["failed"])
        opened=glb([{"name":"body","faces":[(0,2,1),(0,1,3),(0,3,2)]}])
        result=remesh_gate.evaluate(opened,opened,policy(),expectation(opened,0))
        self.assertEqual(result["rc"],5,result)
        self.assertIn("topology:open_boundary",result["failed"])

    def test_mutant_ignoring_exact_genus_is_red_then_restored_green(self):
        raw=torus(); declared=expectation(raw,2)
        changed=mutant(remesh_gate, 'if inv["genus"] != expected.get(name):',
                       'if False:  # planted exact-topology bypass')
        with self.assertRaises(AssertionError):
            self.assertEqual(changed["evaluate"](raw,raw,policy(),declared)["rc"],5)
        self.assertEqual(remesh_gate.evaluate(raw,raw,policy(),declared)["rc"],5)

    def test_reference_drift_refuses_before_reconstruction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); raw=root/"raw.glb"; raw.write_bytes(glb())
            ref=root/"reference"; ref.write_bytes(b"changed")
            declared=expectation(raw.read_bytes(),0)
            declared["reference"]["path"]=str(ref)
            with mock.patch.object(remesh,"default_runner") as runner:
                rc, result=remesh.run(raw,root/"output",expected_source_sha256=contracts.digest(raw.read_bytes()),
                                      policy=policy(raster_resolution=96),method="voxel_grid",voxel_size_um=3000,
                                      topology_expectation=declared)
            self.assertEqual(rc,4,result); runner.assert_not_called()
            self.assertFalse((root/"output").exists())

    def test_stage_declared_handle_negative_then_positive_publishes_bound_policy(self):
        from test_w3_remesh_stage import RemeshStageTests
        fixture=RemeshStageTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        raw=torus(); fixture.raw.write_bytes(raw); fixture.original=raw; fixture.candidate=raw
        reference=fixture.root/"reference"; reference.write_bytes(b"synthetic reference")
        declared=expectation(raw,0)
        declared["reference"]={"path":str(reference),"sha256":remesh.core.sha256_file(reference)}
        def runner(argv,timeout):
            if str(remesh.EXPECTATION_GATE) in argv:
                plan=remesh.core.load_json(Path(argv[-1]),max_bytes=131072)
                result=remesh_gate.evaluate(Path(plan["source"]).read_bytes(),
                                             Path(plan["candidate"]).read_bytes(),plan["policy"],
                                             plan["topology_expectation"])
                remesh.core.atomic_write_json(Path(plan["mesh_receipt"]),result)
                return result["rc"],"AF_REMESH_RC %d\n"%result["rc"]
            return fixture.fake_runner(argv,timeout)
        fixture.runner.side_effect=runner
        pol=policy(raster_resolution=96,sample_budget=2048)
        rc,result=fixture.run_stage(topology_expectation=declared,policy=pol)
        self.assertEqual(rc,5,result); self.assertFalse(fixture.dest.exists())
        declared["genus_by_primitive"]["body"]=1
        rc,result=fixture.run_stage(topology_expectation=declared,policy=pol)
        self.assertEqual(rc,0,result)
        self.assertEqual(remesh.core.load_json(fixture.dest/"topology-expectation.json",max_bytes=65536),declared)
        self.assertEqual(result["topology_expectation_sha256"],contracts.json_digest(declared))
        self.assertEqual(result["policy_sha256"],remesh_gate.policy_identity(
            pol,declared))

    def test_proposal_shape_and_axes_refuse_boolean_or_duplicate_axis(self):
        from test_w3_remesh_stage import RemeshStageTests
        fixture=RemeshStageTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        for changes in ({"closing_shape":"unbounded"},{"fill_axes":[True]},
                        {"fill_axes":[0,0]},{"closing_iterations":9},
                        {"selective_closing_max":5}, {"preserve_recesses":True},
                        {"selective_patch_limit":0}, {"preserve_recesses":1}):
            self.assertEqual(fixture.run_stage(**changes)[0],2)
        fixture.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
