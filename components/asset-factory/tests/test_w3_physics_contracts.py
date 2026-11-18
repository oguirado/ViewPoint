"""Synthetic analytic data tests; these do not claim a MuJoCo runtime PASS."""
import copy
from fractions import Fraction
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from xml.etree import ElementTree as ET

from asset_factory import core
from providers.mujoco import adapter
from tools.physics import api, contracts, gates, worker
from tools.physics.fixtures import fall_fixture
from tools.physics.intake import inspect_visual
from tools.physics.mjcf import write_mjcf


def synthetic_trace(scene, p, nonce, factor):
    """Test oracle states, not engine observations. Constant test contact after fall."""
    dt = p["timestep_us"] // factor
    b = scene["bodies"][1]
    samples = []
    for step in range(p["steps"]*factor+1):
        t = Fraction(step*dt, 1000000)
        xyz = [round(b["position_um"][j] + b["velocity_um_s"][j]*t
                     + Fraction(1,2)*p["expected_gravity_um_s2"][j]*t*t) for j in range(3)]
        contacts = []
        if xyz[2] <= 100000:
            xyz[2] = 100000
            contacts = [{"geom1":"ground","geom2":"box_geom","distance_um":0,"normal_force_uN":9810000}]
        samples.append({"step":step,"time_us":step*dt,
                        "states":[{"body":"box","position_um":xyz,"quaternion_ppm":[1000000,0,0,0]}],
                        "contacts":contacts})
    return {"schema":"af.physics-trace/v1","run_id":nonce,"scene_sha256":core.sha256_json(scene),
            "protocol_sha256":core.sha256_json(p),
            "model_sha256":hashlib.sha256(write_mjcf(scene,p,timestep_us=dt).encode()).hexdigest(),
            "timestep_us":dt,"runtime":{"name":"mujoco","version":worker.RUNTIME_VERSION,
            "module_sha256":"a"*64,"native_libraries":[{"name":"SYNTHETIC_TEST_ONLY.so","sha256":"b"*64}]},
            "admitted_inertia":[{"body":"box",**{k:v for k,v in b["inertial"].items() if k != "provenance"}}],
            "samples":samples,"wall_ms":0,"peak_rss_bytes":1024}


class PhysicsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "analytic-source.json"
        self.source.write_text('{"fixture":"analytic cube"}\n')
        ref = {"path":self.source.name,"sha256":core.sha256_file(self.source),"media_type":"application/json"}
        self.scene, self.p = fall_fixture(ref)
        self.p.update(timestep_us=10000, steps=60)
        self.p["free_fall"].update(steps=10,tolerance_um=1)
        self.scene_path,self.protocol_path=self.root/"scene.json",self.root/"protocol.json"
        self.save()

    def save(self):
        core.atomic_write_json(self.scene_path,self.scene)
        core.atomic_write_json(self.protocol_path,self.p)

    def traces(self):
        return [synthetic_trace(self.scene,self.p,n,f) for n,f in zip(("fresh1","fresh2","fresh3"),(1,2,1))]

    def evaluate(self,traces=None):
        traces=self.traces() if traces is None else traces
        models=[hashlib.sha256(write_mjcf(self.scene,self.p,timestep_us=self.p["timestep_us"]//f).encode()).hexdigest()
                for f in (1,2,1)]
        return gates.evaluate(self.scene,self.p,traces,run_ids=["fresh1","fresh2","fresh3"],model_sha256s=models)

    def test_analytic_oracles_synthetic_pass_and_closed_schemas(self):
        for name,obj in (("scene",self.scene),("protocol",self.p),("trace",self.traces()[0]),("receipt",self.evaluate())):
            core.validate(obj,core.load_json(Path(contracts.__file__).with_name(name+".schema.json"),max_bytes=100000))
        self.assertEqual(self.evaluate()["rc"],0)
        self.assertEqual(self.evaluate()["metrics"]["analytic_error_um"],1)

    def test_si_mass_inertia_and_transforms_are_written_exactly(self):
        node=ET.fromstring(write_mjcf(self.scene,self.p))
        inertia=node.find(".//inertial")
        self.assertEqual(inertia.attrib["mass"],"1.000000000")
        self.assertEqual(node.find("option").attrib["gravity"],"0.000000 0.000000 -9.810000")
        self.assertEqual(inertia.attrib["fullinertia"].split()[0],"0.006666666666666666667")

    def test_inertia_and_units_negative_controls_red_then_green(self):
        for mutation,code in ((lambda s:s.update(units={"length":"mm"}),"units_unsupported"),
                              (lambda s:s["bodies"][1]["inertial"].update(mass_ug=0),"physics_input_invalid"),
                              (lambda s:s["bodies"][1]["inertial"].update(tensor_ug_um2=[10,1,1,0,0,0]),"inertia_triangle_inequality")):
            bad=copy.deepcopy(self.scene);mutation(bad)
            with self.subTest(code=code),self.assertRaises(contracts.Refusal) as caught:
                contracts.validate_scene(bad)
            self.assertEqual(caught.exception.rc,2)
            self.assertEqual(caught.exception.code,code)
            contracts.validate_scene(self.scene)

    def test_hull_is_closed_convex_and_oriented(self):
        vertices=[[0,0,0],[100000,0,0],[0,100000,0],[0,0,100000]]
        faces=[[0,2,1],[0,1,3],[0,3,2],[1,2,3]]
        contracts.convex_hull(vertices,faces)
        for bad in (faces[:-1],faces+faces[:1],[f[::-1] for f in faces]):
            with self.assertRaises(contracts.Refusal): contracts.convex_hull(vertices,bad)
        contracts.convex_hull(vertices,faces)

    def test_analytic_cylinder_sphere_plane_and_hull_xml(self):
        geom=self.scene["bodies"][1]["colliders"][0]
        for shape,data in (("sphere",{"radius_um":100000}),("cylinder",{"radius_um":100000,"half_height_um":100000}),
                           ("convex_hull",{"vertices_um":[[0,0,0],[10,0,0],[0,10,0],[0,0,10]],
                                           "faces":[[0,2,1],[0,1,3],[0,3,2],[1,2,3]]})):
            geom.update(shape=shape,data=data)
            xml=ET.fromstring(write_mjcf(self.scene,self.p))
            self.assertIsNotNone(xml.find(".//geom[@name='ground']"))
            self.assertEqual(xml.find(".//geom[@name='box_geom']").attrib["type"],"mesh" if shape=="convex_hull" else shape)

    def test_source_tamper_and_escape_are_refused(self):
        self.source.write_text("changed")
        with self.assertRaises(contracts.Refusal) as error: contracts.load_inputs(self.scene_path,self.protocol_path,self.root)
        self.assertEqual(error.exception.rc,4)
        self.scene["source"]["path"]="../outside"
        with self.assertRaises(contracts.Refusal) as error: contracts.resolve_file(self.root,self.scene["source"])
        self.assertEqual(error.exception.code,"path_escape")

    def test_wrong_gravity_fails_even_with_convergence(self):
        self.p["gravity_um_s2"]=[0,0,0]
        result=self.evaluate()
        self.assertEqual((result["rc"],result["reason_codes"]),(5,["wrong_gravity"]))
        self.p["gravity_um_s2"]=self.p["expected_gravity_um_s2"][:]
        self.assertEqual(self.evaluate()["rc"],0)

    def test_contact_penetration_and_initial_intersection_controls(self):
        for step,depth,reason in ((0,1,"initial_intersection"),(50,5001,"penetration_exceeded")):
            traces=self.traces()
            traces[0]["samples"][step]["contacts"]=[{"geom1":"ground","geom2":"box_geom","distance_um":-depth,"normal_force_uN":1}]
            result=self.evaluate(traces)
            self.assertEqual(result["rc"],5)
            self.assertEqual(result["reason_codes"],[reason])
            self.assertEqual(self.evaluate()["rc"],0)

    def test_replayed_trace_and_missing_sample_controls(self):
        traces=self.traces();traces[0]["run_id"]="previous-run"
        self.assertEqual(self.evaluate(traces)["rc"],4)
        traces=self.traces();traces[0]["samples"].pop()
        self.assertEqual(self.evaluate(traces)["rc"],3)
        self.assertEqual(self.evaluate()["rc"],0)

    def test_divergent_trajectory_orientation_and_replay_controls(self):
        for field in ("position_um","quaternion_ppm"):
            traces=self.traces()
            state=traces[1]["samples"][40]["states"][0]
            state[field]=[1000000,1000000,1000000] if field=="position_um" else [0,1000000,0,0]
            self.assertEqual(self.evaluate(traces)["reason_codes"],["timestep_diverged"])
        traces=self.traces();traces[2]["samples"][20]["states"][0]["position_um"][0]+=1
        self.assertEqual(self.evaluate(traces)["reason_codes"],["replay_nondeterministic"])
        self.assertEqual(self.evaluate()["rc"],0)

    def test_false_analytic_trace_and_mass_readback_controls(self):
        traces=self.traces()
        for trace in traces:
            for sample in trace["samples"]:
                sample["states"][0]["position_um"]=[0,0,1000000]
        self.assertEqual(self.evaluate(traces)["reason_codes"],["analytic_fall_failed"])
        traces=self.traces();traces[0]["admitted_inertia"][0]["mass_ug"]*=1000
        self.assertEqual(self.evaluate(traces)["reason_codes"],["compiled_mass_mismatch"])

    def test_unavailable_runtime_and_budget_do_not_publish(self):
        job={"scene":self.scene,"protocol":self.p,"dt_us":self.p["timestep_us"],"steps":self.p["steps"],"run_id":"x"}
        with mock.patch.dict(sys.modules,{"mujoco":None}):
            with self.assertRaises(contracts.Refusal) as err: worker.collect(job)
        self.assertEqual((err.exception.rc,err.exception.code),(3,"mujoco_unavailable"))
        self.p["steps"]=5001;self.save()
        result=api.qualification(self.scene_path,self.protocol_path,evidence_root=self.root,output_dir=self.root/"new")
        self.assertEqual(result["rc"],2)
        self.assertFalse((self.root/"new").exists())

    def test_owned_process_timeout_is_reaped(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            api.run_child([sys.executable,"-c","import time; time.sleep(30)"],timeout_s=0.05,cwd=self.root)

    def test_existing_output_bytes_preserved(self):
        target=self.root/"existing";target.mkdir();(target/"sentinel").write_bytes(b"caller")
        result=api.qualification(self.scene_path,self.protocol_path,evidence_root=self.root,output_dir=target)
        self.assertEqual(result["rc"],2)
        self.assertEqual((target/"sentinel").read_bytes(),b"caller")

    def test_atomic_publication_only_after_all_three_oracles_pass(self):
        def synthetic_child(argv,**kwargs):
            job=core.load_json(Path(argv[argv.index("--job")+1]),max_bytes=100000)
            factor=self.p["timestep_us"]//job["dt_us"]
            trace=synthetic_trace(job["scene"],job["protocol"],job["run_id"],factor)
            core.atomic_write_json(Path(argv[argv.index("--trace")+1]),trace)
            return 0,b""
        with mock.patch.object(api,"run_child",side_effect=synthetic_child):
            result=api.qualification(self.scene_path,self.protocol_path,evidence_root=self.root,output_dir=self.root/"pass")
        self.assertEqual(result["rc"],0)
        self.assertEqual(len(list((self.root/"pass").glob("trace-*.json"))),3)
        self.assertTrue((self.root/"pass"/"receipt.json").exists())

    def test_visual_does_not_supply_mass(self):
        import struct
        body=b'{"meshes":[]}'
        body+=b' '*(-len(body)%4)
        path=self.root/"raw.glb"
        path.write_bytes(struct.pack("<4sIIII",b"glTF",2,20+len(body),len(body),0x4e4f534a)+body)
        result=inspect_visual(path)
        self.assertEqual(result["rc"],3)
        self.assertIsNone(result["metrics"]["physical_mass_ug"])

    def request(self):
        return {"schema":"af.physics/v1","kind":"request","slot":"physics","request_id":"drop",
                 "provider_id":"mujoco","provider_revision":"d"*40,"params":{},
                 "inputs":[{"role":role,"path":"x","media_type":"application/json","sha256":"a"*64}
                           for role in ("physics_scene","physics_protocol")],
                 "context":{"jurisdiction":"ES","commercial_use":False,"timeout_s":30,
                            "policy_sha256":"b"*64,"toolchain_sha256":"c"*64}}

    def test_missing_contacts_and_malformed_time_fail_closed(self):
        traces=self.traces()
        for t in traces:
            for sample in t["samples"]: sample["contacts"]=[]
        self.assertEqual(self.evaluate(traces)["reason_codes"],["required_contact_absent"])
        traces=self.traces();traces[0]["samples"][0]["time_us"]=False
        self.assertEqual(self.evaluate(traces)["rc"],2)
        traces=self.traces();traces[0]["samples"][0]["time_us"]=0.0
        self.assertEqual(self.evaluate(traces)["rc"],2)
        self.assertEqual(self.evaluate()["rc"],0)

    def test_runtime_version_and_child_rc_one_fail_closed(self):
        from types import SimpleNamespace
        job={"scene":self.scene,"protocol":self.p,"dt_us":self.p["timestep_us"],"steps":self.p["steps"],"run_id":"x"}
        with mock.patch.object(worker.importlib,"import_module",return_value=SimpleNamespace(__version__="0.0")):
            with self.assertRaises(contracts.Refusal) as caught:worker.collect(job)
        self.assertEqual(caught.exception.rc,4)
        with mock.patch.object(api,"run_child",return_value=(1,b"foreign failure")):
            result=api.qualification(self.scene_path,self.protocol_path,evidence_root=self.root,output_dir=self.root/"new")
        self.assertEqual(result["rc"],2)
        self.assertFalse((self.root/"new").exists())

    def test_worker_timeout_returns_budget_and_preserves_outputs(self):
        with mock.patch.object(api,"run_child",side_effect=subprocess.TimeoutExpired(["owned"],1)):
            result=api.qualification(self.scene_path,self.protocol_path,evidence_root=self.root,output_dir=self.root/"new")
        self.assertEqual((result["rc"],result["verdict"]),(6,"CANNOT_SEE"))
        self.assertFalse((self.root/"new").exists())

    def test_native_worker_error_boundary_keeps_stage_codes(self):
        import io
        job=self.root/"job.json";core.atomic_write_json(job,{})
        target=self.root/"caller-trace.json";target.write_bytes(b"caller")
        for failure,expected in ((RuntimeError("native failure"),2),(MemoryError(),6)):
            stream=io.StringIO()
            with mock.patch.object(worker,"collect",side_effect=failure),mock.patch("sys.stdout",stream):
                actual=worker.main(["--job",str(job),"--trace",str(target)])
            self.assertEqual(actual,expected)
            self.assertEqual(json.loads(stream.getvalue())["rc"],expected)
            self.assertEqual(target.read_bytes(),b"caller")

    def test_model_declaration_preserves_peak_and_context_guards(self):
        request=self.request()
        provider=core.load_json(adapter.HERE/"provider.json",max_bytes=100000)
        provider.update(runnable=True,commercial=True,gated=False)
        request["provider_revision"]=provider["revision"]
        factory={"commercial_use":False,"jurisdiction":"ES"};policy={"policy":"test"}
        request["context"]["policy_sha256"]=core.sha256_json(policy)
        decision={"schema":"af.licence-decision/v1","provider_id":"mujoco","operation":"physics",
                  "rc":5,"verdict":"FAIL","allowed":False,"jurisdiction":"ES","commercial_use":False,
                  "configuration_sha256":core.sha256_json(factory),"provider_sha256":core.sha256_json(provider),
                  "policy_sha256":core.sha256_json(policy)}
        with mock.patch.object(api,"qualification") as run:
            result=adapter.execute(request,provider,decision,factory,policy,evidence_root=self.root,output_dir=self.root/"new")
            self.assertEqual(result["rc"],3)
            decision.update(jurisdiction="GB")
            result=adapter.execute(request,provider,decision,factory,policy,evidence_root=self.root,output_dir=self.root/"new")
            self.assertEqual(result["rc"],4)
            decision.update(jurisdiction="ES")
            result=adapter.execute(request,provider,decision,factory,policy,evidence_root=self.root,output_dir=self.root/"new")
            self.assertEqual(result["rc"],3)
            run.assert_not_called()

    def test_physics_advisory_denial_publishes_bound_request(self):
        request = self.request()
        for item, source in zip(request["inputs"], (self.scene_path, self.protocol_path)):
            item.update(path=source.name, sha256=core.sha256_file(source))
        provider = core.load_json(adapter.HERE / "provider.json", max_bytes=100000)
        provider.update(runnable=True, commercial=False, gated=False)
        provider.pop("blocked_reason", None)
        provider["runtime"].update(ram_peak_gb_measured=1,
                                   ram_peak_measurement_ref={"path": "synthetic.json", "sha256": "a"*64})
        request["provider_revision"] = provider["revision"]
        factory = {"commercial_use": False, "jurisdiction": "ES"}
        policy = {"policy": "test"}
        request["context"]["policy_sha256"] = core.sha256_json(policy)
        decision = {"schema": "af.licence-decision/v1", "provider_id": "mujoco", "operation": "physics",
                    "rc": 5, "verdict": "FAIL", "allowed": False, **factory,
                    "configuration_sha256": core.sha256_json(factory),
                    "provider_sha256": core.sha256_json(provider),
                    "policy_sha256": core.sha256_json(policy)}

        def qualification(scene, protocol, *, evidence_root, output_dir):
            self.assertEqual((scene, protocol, evidence_root), (self.scene_path, self.protocol_path, self.root))
            output_dir.mkdir()
            trace = self.traces()[0]
            receipt = self.evaluate() | {"peak_rss_bytes": trace["peak_rss_bytes"]}
            core.atomic_write_json(output_dir / "trace-0.json", trace)
            core.atomic_write_json(output_dir / "receipt.json", receipt)
            return receipt

        output = self.root / "advisory-output"
        with mock.patch.object(api, "qualification", side_effect=qualification) as runner:
            result = adapter.execute(request, provider, decision, factory, policy,
                                     evidence_root=self.root, output_dir=output)
        self.assertEqual(result["rc"], 0, result)
        runner.assert_called_once()
        self.assertEqual(core.load_json(output / "request.json", max_bytes=100000)["context"],
                         request["context"])
        self.assertEqual(result["receipts"][0]["sha256"], core.sha256_file(output / "receipt.json"))

    def test_duplicate_keys_and_float_scene_refuse_without_output(self):
        for content in ('{"schema":"af.physics-scene/v1","schema":"again"}', '{"mass":1.0}'):
            self.scene_path.write_text(content)
            result=api.qualification(self.scene_path,self.protocol_path,evidence_root=self.root,output_dir=self.root/"new")
            self.assertEqual(result["rc"],2)
            self.assertFalse((self.root/"new").exists())

    def test_draft_request_provider_and_duplicate_roles(self):
        request={"schema":"af.physics/v1","kind":"request","slot":"physics","request_id":"drop",
                 "provider_id":"mujoco","provider_revision":"d"*40,"params":{},
                 "inputs":[{"role":role,"path":"x","media_type":"application/json","sha256":"a"*64}
                           for role in ("physics_scene","physics_protocol")],
                 "context":{"jurisdiction":"ES","commercial_use":False,"timeout_s":30,
                            "policy_sha256":"b"*64,"toolchain_sha256":"c"*64}}
        self.assertEqual(adapter.validate_request(request),"registered")
        request["request_id"] = "   "
        with self.assertRaises(contracts.Refusal) as caught:
            adapter.validate_request(request)
        self.assertEqual((caught.exception.rc, caught.exception.code), (2, "schema_mismatch"))
        request["request_id"] = "drop"
        request["inputs"][1]["role"]="physics_scene"
        with self.assertRaises(contracts.Refusal):adapter.validate_request(request)
        request["inputs"][1]["role"]="physics_protocol"
        provider=core.load_json(adapter.HERE/"provider.json",max_bytes=100000)
        request["provider_revision"]=provider["revision"]
        result=adapter.execute(request,provider,{}, {},{},evidence_root=self.root,output_dir=self.root/"new")
        self.assertEqual(result["rc"],5)
        self.assertEqual(result["absence_reasons"]["physics"],"provider_blocked")
        self.assertFalse((self.root/"new").exists())


if __name__ == "__main__":
    unittest.main()
