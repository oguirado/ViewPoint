"""Unit tests for wender.act.api_path: AppApiAdapter, ApiCallSpec, BpyExampleAdapter, FakeApiAdapter."""

import unittest
from wender.act.api_path import (
    ApiCallSpec,
    ApiRefused,
    AppApiAdapter,
    BpyExampleAdapter,
    FakeApiAdapter,
    run_api_action,
)
from wender.state.schema import dumps


class TestApiPath(unittest.TestCase):
    def test_allowlist_enforcement_and_capabilities(self):
        adapter = BpyExampleAdapter()
        self.assertEqual(
            adapter.capabilities,
            (
                "bpy.ops.mesh.primitive_cube_add",
                "bpy.ops.object.delete",
                "bpy.ops.transform.translate",
            ),
        )
        spec_valid = ApiCallSpec("blender", "bpy.ops.mesh.primitive_cube_add", {"size": 2})
        receipt = run_api_action(adapter, spec_valid)
        self.assertEqual(receipt["adapter"], "blender_bpy")
        self.assertEqual(receipt["result"]["status"], "planned")

        spec_unknown = ApiCallSpec("blender", "bpy.ops.mesh.primitive_monkey_add", {})
        with self.assertRaises(ApiRefused):
            run_api_action(adapter, spec_unknown)

    def test_float_refusal(self):
        # Floats in params at top level or nested must be refused
        with self.assertRaises(ApiRefused):
            ApiCallSpec("blender", "bpy.ops.transform.translate", {"value": 1.5})

        with self.assertRaises(ApiRefused):
            ApiCallSpec("blender", "bpy.ops.transform.translate", {"values": [1, 2, 3.0]})

        with self.assertRaises(ApiRefused):
            ApiCallSpec("blender", "bpy.ops.transform.translate", {"meta": {"scale": 2.5}})

        # Integers in micrometres are allowed
        spec_int = ApiCallSpec(
            "blender",
            "bpy.ops.transform.translate",
            {"value_um": [1000000, 0, -500000]},
        )
        self.assertEqual(spec_int.params["value_um"], [1000000, 0, -500000])

    def test_irreversible_refusal(self):
        adapter = BpyExampleAdapter()
        spec_delete = ApiCallSpec("blender", "bpy.ops.object.delete", {"use_global": False})

        # Refused without confirm_irreversible=True
        with self.assertRaises(ApiRefused):
            run_api_action(adapter, spec_delete, confirm_irreversible=False)

        # Allowed with confirm_irreversible=True
        receipt = run_api_action(adapter, spec_delete, confirm_irreversible=True)
        self.assertEqual(receipt["result"]["status"], "planned")
        self.assertEqual(receipt["call"], "bpy.ops.object.delete")

    def test_snippet_byte_stable(self):
        adapter = BpyExampleAdapter()
        spec = ApiCallSpec(
            "blender",
            "bpy.ops.transform.translate",
            {"value": [100, 200, 300], "orient_type": "GLOBAL"},
        )
        receipt1 = run_api_action(adapter, spec)
        receipt2 = run_api_action(adapter, spec)

        bytes1 = dumps(receipt1)
        bytes2 = dumps(receipt2)

        self.assertEqual(bytes1, bytes2)
        self.assertIn("import bpy\n", receipt1["result"]["snippet"])
        self.assertIn("bpy.ops.transform.translate(", receipt1["result"]["snippet"])

    def test_injection_attempt_refused(self):
        # Semicolons, newlines, spaces, or non-dotted names in call must be refused by ApiCallSpec
        invalid_calls = [
            "bpy.ops.mesh.primitive_cube_add; import os; os.system('ls')",
            "bpy.ops.mesh.primitive_cube_add\nimport os",
            "bpy.ops.mesh.primitive_cube_add()",
            "single_word",
            "bpy..ops",
            "bpy.ops.mesh.",
            "bpy.ops.mesh.cube add",
            "bpy.ops.mesh.primitive_cube_add--",
        ]
        for bad_call in invalid_calls:
            with self.assertRaises(ApiRefused, msg=f"Should refuse bad call: {bad_call!r}"):
                ApiCallSpec("blender", bad_call, {})

    def test_fake_api_adapter(self):
        fake = FakeApiAdapter(name="test_fake")
        self.assertEqual(fake.capabilities, ("fake.ops.add", "fake.ops.delete"))

        spec_add = ApiCallSpec("fake_prog", "fake.ops.add", {"count": 1})
        receipt = run_api_action(fake, spec_add)

        self.assertEqual(receipt["adapter"], "test_fake")
        self.assertEqual(len(fake.recorded_calls), 1)
        self.assertEqual(fake.recorded_calls[0]["call"], "fake.ops.add")

        # Check dumps serialization of receipt (canonical JSON)
        canonical_json = dumps(receipt)
        self.assertIsInstance(canonical_json, str)


if __name__ == "__main__":
    unittest.main()
