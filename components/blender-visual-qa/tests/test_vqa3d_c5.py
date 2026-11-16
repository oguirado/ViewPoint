"""Animation-scoped permissions and GPU diagnostic regressions."""
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from blender_visual_qa.drive.png import encode
from vqa3d.adapters import blender as driver, blender_side as side
from vqa3d.check import evaluate, validate_obligation
from vqa3d.contract import Observation
from vqa3d import profile
from vqa3d.receipt import make_receipt
from vqa3d.temporal import capability_check


class AnimatedPermissions(unittest.TestCase):
    def pair(self):
        before = {"frame_current": 1, "animation_paths": ["/objects/Rig/bones", "/objects/Skin/vertex_groups",
                  "/objects/Dancer/location_um"], "objects": {
            "Rig": {"bones": {"hand": {"head_world_um": [0, 0, 0]}}},
            "Skin": {"vertex_groups": {"chest": {"world_bbox_min_um": [0, 0, 0]}}},
            "Dancer": {"location_um": [0, 0, 0], "type": "MESH"},
            "Static": {"location_um": [0, 0, 0]}}}
        after = copy.deepcopy(before); after["frame_current"] = 805
        after["objects"]["Rig"]["bones"]["hand"]["head_world_um"][0] = 10
        after["objects"]["Skin"]["vertex_groups"]["chest"]["world_bbox_min_um"][0] = 10
        after["objects"]["Dancer"]["location_um"][0] = 10
        return before, after

    def run_pair(self, before, after, kind="state_change", **opts):
        png = encode(2, 2, bytes(range(12)))
        obligation = {"kind": kind, "allow_animated": True, **opts}
        if kind == "state_change": obligation["keys"] = ["/frame_current"]
        validate_obligation(obligation)
        return evaluate(Observation.create("fake", "before", before, png),
                        Observation.create("fake", "after", after, png), obligation)

    def test_frame_change_and_static_mutant_for_both_obligations(self):
        before, after = self.pair()
        for kind in ("state_change", "no_change"):
            self.assertEqual(self.run_pair(before, after, kind)[0], "PASS")
            mutant = copy.deepcopy(after); mutant["objects"]["Static"]["location_um"][0] = 1
            verdict, reason = self.run_pair(before, mutant, kind)
            self.assertEqual(verdict, "FAIL"); self.assertIn("/objects/Static/location_um", reason)
            mutant = copy.deepcopy(after); mutant["objects"]["Dancer"]["type"] = "EMPTY"
            self.assertEqual(self.run_pair(before, mutant, kind)[0], "FAIL")

    def test_requires_stable_metadata_frame_transition_and_boolean(self):
        before, after = self.pair()
        for key in ("frame_current", "animation_paths"):
            mutant = copy.deepcopy(after); mutant[key] = before[key] if key == "frame_current" else []
            self.assertEqual(self.run_pair(before, mutant)[0], "FAIL")
        del before["animation_paths"]; del after["animation_paths"]
        self.assertEqual(self.run_pair(before, after)[0], "FAIL")
        with self.assertRaisesRegex(ValueError, "allow_animated_requires_boolean"):
            validate_obligation({"kind": "no_change", "allow_animated": 1})
        profile._params("no_change", {"allow_animated": True})
        with self.assertRaises(ValueError): profile._params("no_change", {"allow_animated": 1})

    def test_no_change_still_requires_identical_pixels(self):
        before, after = self.pair()
        a = Observation.create("fake", "before", before, encode(1, 1, b"aaa"))
        b = Observation.create("fake", "after", after, encode(1, 1, b"bbb"))
        self.assertEqual(evaluate(a, b, {"kind": "no_change", "allow_animated": True}), ("FAIL", "unexpected_change"))


class AnimationDeclarations(unittest.TestCase):
    def obj(self, name, **kw):
        return NS(name=name, type="MESH", animation_data=None, data=None, parent=None,
                  constraints=[], modifiers=[], **kw)

    def anim(self, path):
        return NS(action=NS(fcurves=[NS(data_path=path, mute=False)]), nla_tracks=[], drivers=[])

    def test_action_nla_constraint_skin_and_parent_without_static_exemption(self):
        rig=self.obj("Rig/~"); rig.type="ARMATURE"; rig.animation_data=self.anim('pose.bones["hand"].location')
        skin=self.obj("Skin"); skin.modifiers=[NS(type="ARMATURE", object=rig)]
        child=self.obj("Child"); child.parent=rig
        nla=self.obj("Nla"); nla.animation_data=NS(action=None,drivers=[],nla_tracks=[NS(mute=False,
            strips=[NS(mute=False,action=self.anim("location").action)])])
        constrained=self.obj("Constrained"); constrained.constraints=[NS(mute=False,influence=1,target=nla)]
        static=self.obj("Static")
        paths=side.animation_paths(NS(objects=[rig,skin,child,nla,constrained,static],camera=None))
        for expected in ("/objects/Rig~1~0/bones", "/objects/Skin/vertex_groups", "/objects/Child/world_location_um",
                         "/objects/Nla/location_um", "/objects/Constrained/world_location_um"):
            self.assertIn(expected,paths)
        self.assertFalse(any("Static" in p for p in paths))
        self.assertNotIn("/objects/Skin/location_um",paths)
        self.assertNotIn("/objects/Constrained/location_um",paths)


class GPUReports(unittest.TestCase):
    def test_failed_render_retains_child_environment_and_stderr_in_receipt(self):
        fake = """#!/usr/bin/python3
import os,json,sys,hashlib
req,res=sys.argv[sys.argv.index('--')+1:]
x=json.load(open(req))
while True:
    environment={k:{'sha256':hashlib.sha256(os.environ[k].encode()).hexdigest()} for k in x['environment_keys'] if k in os.environ}
    result={'ok':True,'runtime_version':'fake','state':{},'environment':environment}
    if x['mode']=='gpu_probe':
        print('compiler failure sm_121 ' + os.environ['RENDER_OPTION'],file=sys.stderr,flush=True)
        result.update(ok=False,error='kernel_compile_failed')
    with open(res+'.tmp','w') as out: json.dump(result,out)
    os.replace(res+'.tmp',res)
    if not x.get('session'): break
    line=sys.stdin.readline()
    if not line: break
    x=json.loads(line)
"""
        for session in (False, True):
            with self.subTest(session=session), tempfile.TemporaryDirectory() as temp:
                root=Path(temp);exe=root/'blender';exe.write_text(fake);exe.chmod(0o700)
                source=root/'scene.blend';source.write_bytes(b'fixture')
                adapter=driver.BlenderAdapter(source,profile_session=session,blender_env={'RENDER_OPTION':'secret-value'})
                try:
                    with patch.object(driver,'BLENDER',exe):
                        adapter.scene_state()
                        self.assertFalse(adapter.gpu_probe('CUDA'))
                    diag=adapter.gpu_probe_diagnostics['CUDA']
                    self.assertIn('sm_121',diag['stderr_tail'])
                    self.assertEqual(diag['child_environment']['RENDER_OPTION'],adapter.environment_receipt['RENDER_OPTION'])
                    self.assertNotIn('secret-value',json.dumps(diag))
                    probe={'cycles_devices':{'CUDA':['GB10']},'gpu_probe':adapter.gpu_probe_diagnostics}
                    verdict,reason=capability_check(probe,{'kind':'capability','device_type':'CUDA'})
                    self.assertEqual(verdict,'CANNOT_SEE');self.assertIn('kernel_compile_failed',reason)
                    receipt=make_receipt(adapter,{'kind':'capability'},None,None,[{'verdict':verdict,'reason':reason}])
                    self.assertEqual(receipt['gpu_probe']['CUDA'],diag)
                    self.assertLessEqual(len(diag['stderr_tail']),4096)
                finally: adapter.close()


if __name__ == "__main__": unittest.main()
