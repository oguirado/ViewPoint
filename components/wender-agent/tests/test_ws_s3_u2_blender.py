"""S3-U2 structured Blender contract and planted negative controls."""
import copy
import hashlib
import json
import unittest
from pathlib import Path

from wender.state import schema


def snapshot():
    """Synthetic integer observation; limits and units are explicit test inputs."""
    return {
        "schema": "wender.blender-scene/v1", "scene_name": "Fixture",
        "blender_version": "4.0.2", "micrometers_per_unit": 1000000,
        "frame": 1, "active_object": "Cube", "objects": [{
            "name": "Cube", "type": "MESH", "selected": True, "hidden": False,
            "basis_ppm": [1000000, 0, 0, 0, 1000000, 0, 0, 0, 1000000],
            "translation_um": [0, 0, 0], "transform_sha256": "a" * 64,
            "mesh": {"vertices": 8, "edges": 12, "faces": 6,
                     "boundary_edges": 0, "nonmanifold_edges": 0,
                     "bounds_um": [-1000000] * 3 + [1000000] * 3,
                     "geometry_sha256": "b" * 64},
        }],
    }


class TestBlenderRegistration(unittest.TestCase):
    def test_scene_schema_matches_closed_validator(self):
        from wender.state import blender
        machine = json.loads((Path(blender.__file__).parent / "wender.blender-scene.v1.schema.json").read_text())
        self.assertEqual(set(machine["required"]), blender.SCENE_KEYS)
        self.assertEqual(set(machine["$defs"]["object"]["required"]), blender.OBJECT_KEYS)
        self.assertEqual(set(machine["$defs"]["mesh"]["required"]), blender.MESH_KEYS)
        self.assertFalse(machine["additionalProperties"])

    def test_source_kind_registered_in_code_and_schema(self):
        self.assertIn("blender", schema.SOURCE_KINDS)
        machine = json.loads((Path(schema.__file__).parent / "wender.state.v1.schema.json").read_text())
        self.assertEqual(machine["properties"]["source"]["properties"]["kind"]["enum"],
                         list(schema.SOURCE_KINDS))


class TestBlenderState(unittest.TestCase):
    def test_conversion_and_refusal_perform_no_file_writes(self):
        from unittest import mock
        from wender.perceive.blender import build_blender_state, validate_blender_state
        raw = snapshot()
        with mock.patch("builtins.open", side_effect=AssertionError("unexpected IO")), \
                mock.patch("os.replace", side_effect=AssertionError("unexpected publication")):
            observation = build_blender_state(raw, max_bytes=100000)
            validate_blender_state(observation, max_bytes=100000)
            raw["objects"][0]["basis_ppm"][0] = 0.5
            with self.assertRaises(schema.StateError):
                build_blender_state(raw, max_bytes=100000)

    def setUp(self):
        from wender.perceive import blender
        self.api = blender

    def test_equal_scene_equal_bytes_and_binds_sidecar(self):
        first = self.api.build_blender_state(snapshot(), max_bytes=100000)
        second = self.api.build_blender_state(copy.deepcopy(snapshot()), max_bytes=100000)
        self.assertEqual(schema.dumps(first), schema.dumps(second))
        self.assertEqual(schema.validate_state(first["state"]), [])
        self.assertEqual(first["state"]["source"]["kind"], "blender")
        self.assertEqual(first["blender"]["base_sha256"], hashlib.sha256(
            schema.dumps(first["state"]).encode()).hexdigest())
        self.api.validate_blender_state(first, max_bytes=100000)

    def test_dictionary_and_object_order_do_not_change_bytes(self):
        raw = snapshot()
        other = copy.deepcopy(raw["objects"][0])
        other["name"] = "Another"
        raw["objects"].append(other)
        first = self.api.build_blender_state(raw, max_bytes=100000)
        raw["objects"].reverse()
        raw = dict(reversed(list(raw.items())))
        self.assertEqual(schema.dumps(first), schema.dumps(
            self.api.build_blender_state(raw, max_bytes=100000)))

    def test_float_refused_at_all_state_boundaries(self):
        for field in ("frame", "nested", "nan", "bool_count"):
            raw = snapshot()
            if field == "frame":
                raw["frame"] = 1.0
            elif field == "nested":
                raw["objects"][0]["translation_um"][0] = 0.25
            elif field == "nan":
                raw["objects"][0]["basis_ppm"][0] = float("nan")
            else:
                raw["objects"][0]["mesh"]["vertices"] = True
            with self.subTest(field=field), self.assertRaises(schema.StateError):
                self.api.build_blender_state(raw, max_bytes=100000)
        envelope = self.api.build_blender_state(snapshot(), max_bytes=100000)
        envelope["state"]["source"]["title"] = 0.5
        self.assertTrue(schema.validate_state(envelope["state"]))
        with self.assertRaises(schema.StateError):
            schema.dumps(envelope)

    def test_changed_geometry_changes_observation_identity(self):
        raw = snapshot()
        first = self.api.build_blender_state(raw, max_bytes=100000)
        raw["objects"][0]["mesh"]["geometry_sha256"] = "c" * 64
        second = self.api.build_blender_state(raw, max_bytes=100000)
        self.assertNotEqual(schema.dumps(first), schema.dumps(second))
        # AX hashes remain hashes of prune fields, never hashes of geometry.
        self.assertEqual(first["state"]["ax_sha256"], second["state"]["ax_sha256"])

    def test_sidecar_tamper_refused(self):
        envelope = self.api.build_blender_state(snapshot(), max_bytes=100000)
        envelope["blender"]["scene"]["frame"] += 1
        with self.assertRaises(schema.StateError):
            self.api.validate_blender_state(envelope, max_bytes=100000)

    def test_base_tamper_refused_even_with_recomputed_base_hash(self):
        envelope = self.api.build_blender_state(snapshot(), max_bytes=100000)
        envelope["state"]["nodes"][0]["name"] = "Other scene"
        envelope["blender"]["base_sha256"] = hashlib.sha256(
            schema.dumps(envelope["state"]).encode()).hexdigest()
        with self.assertRaises(schema.StateError):
            self.api.validate_blender_state(envelope, max_bytes=100000)

    def test_unknown_missing_duplicate_and_nonfinite_refused(self):
        for mutation in ("unknown", "missing", "duplicate", "bad_bounds", "unknown_active"):
            raw = snapshot()
            if mutation == "unknown":
                raw["extra"] = 1
            elif mutation == "missing":
                del raw["objects"][0]["mesh"]["boundary_edges"]
            elif mutation == "duplicate":
                raw["objects"].append(copy.deepcopy(raw["objects"][0]))
            elif mutation == "unknown_active":
                raw["active_object"] = "Missing"
            else:
                raw["objects"][0]["mesh"]["bounds_um"] = [2, 0, 0, 1, 1, 1]
            with self.subTest(mutation=mutation), self.assertRaises(schema.StateError):
                self.api.build_blender_state(raw, max_bytes=100000)

    def test_budget_refusal_has_stage_code_six(self):
        raw = snapshot()
        before = copy.deepcopy(raw)
        with self.assertRaises(self.api.BlenderStateError) as caught:
            self.api.build_blender_state(raw, max_bytes=1)
        self.assertEqual(caught.exception.rc, 6)
        self.assertEqual(raw, before)

    def test_unknown_kind_still_refused(self):
        envelope = self.api.build_blender_state(snapshot(), max_bytes=100000)
        envelope["state"]["source"]["kind"] = "unregistered"
        self.assertTrue(schema.validate_state(envelope["state"]))


if __name__ == "__main__":
    unittest.main()
