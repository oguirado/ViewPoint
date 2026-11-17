"""Harness checks independent of runtime availability; native_gate measures MuJoCo."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.physics import native_gate
from tools.physics.contracts import Refusal, validate_scene, validate_protocol
from tools.physics.fixtures import fall_fixture


class NativeGateTests(unittest.TestCase):
    def test_only_specific_red_then_restored_green_passes(self):
        red = {"rc":5,"verdict":"FAIL","reason_codes":["penetration_exceeded"]}
        green = {"rc":0,"verdict":"PASS","reason_codes":[]}
        for bad in (green, {**red,"rc":3}, {**red,"reason_codes":["wrong_gravity"]},
                    {**red,"verdict":"PASS"}):
            with self.assertRaises(Refusal) as caught:
                native_gate.check_control("contact",bad,green,5,"penetration_exceeded")
            self.assertEqual(caught.exception.code,"negative_control_failed")
        record = native_gate.check_control("contact",red,green,5,"penetration_exceeded")
        self.assertEqual((record["observed_rc"],record["restored_rc"]),(5,0))
        with self.assertRaises(Refusal) as caught:
            native_gate.check_control("contact",red,red,5,"penetration_exceeded")
        self.assertEqual(caught.exception.code,"restoration_failed")

    def test_control_cannot_expect_green_or_accept_boolean_rc(self):
        green = {"rc":0,"verdict":"PASS","reason_codes":[]}
        for expected in (0,1,True):
            with self.assertRaises(Refusal):
                native_gate.check_control("bad",green,green,expected,"")
        with self.assertRaises(Refusal):
            native_gate.check_control("bad",{"rc":False,"verdict":"PASS","reason_codes":[]},green,5,"x")

    def test_refined_protocol_keeps_all_accuracy_guards(self):
        scene, protocol = fall_fixture({"path":"analytic.json","sha256":"a"*64,"media_type":"application/json"})
        validate_scene(scene); validate_protocol(protocol)
        self.assertEqual(protocol["steps"]*protocol["timestep_us"],1000000)
        self.assertEqual(protocol["free_fall"]["steps"]*protocol["timestep_us"],100000)
        self.assertEqual(protocol["max_trajectory_error_ppm"],10000)
        self.assertEqual(protocol["inertia_tolerance_ppm"],1000)
        self.assertEqual(protocol["contact"]["max_penetration_um"],5000)
        self.assertEqual(protocol["free_fall"]["tolerance_um"],1000)
        compiled, short = native_gate._primitive_scene(scene,protocol)
        validate_scene(compiled); validate_protocol(short)
        self.assertEqual(len(compiled["bodies"]),6)
        self.assertEqual(compiled["bodies"][-1]["inertial"]["com_um"],[1000,2000,3000])
        self.assertNotEqual(compiled["bodies"][-1]["inertial"]["tensor_ug_um2"][3],0)
        broken=deepcopy(compiled);broken["bodies"][4]["colliders"][0]["data"]["faces"].pop()
        with self.assertRaises(Refusal): validate_scene(broken)

    def test_refusal_preserves_caller_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"existing";target.mkdir()
            sentinel=target/"caller";sentinel.write_bytes(b"unchanged")
            result=native_gate.run(target)
            self.assertEqual((result["rc"],result["reason_codes"]),(2,["output_exists"]))
            self.assertEqual(sentinel.read_bytes(),b"unchanged")


if __name__ == "__main__":
    unittest.main()
