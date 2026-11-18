"""Independent exported fixture and discriminating PHYS controls."""
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
from importlib.util import find_spec
from itertools import product
import os
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

from asset_factory import core
from asset_factory.domains import FRAME
from tools.blender.glb_io import encode_glb
from tools.physgate import geometry, replay


# [TARGET] Exact binary fractions in glTF metres and independently specified
# support observations in the registered factory (Z-up) frame.
HALF = [125_000, 62_500, 62_500]
GRAVITY = [0, 0, -9_810_000]
requires_mujoco = unittest.skipUnless(
    find_spec("mujoco") is not None, f"MuJoCo unavailable in interpreter {sys.executable}")


def box_glb(*, open_box=False, concave=False):
    vertices = list(product((-0.125, 0.125), (-0.0625, 0.0625), (-0.0625, 0.0625)))
    if concave:
        vertices[-1] = (0.0625, 0.03125, 0.03125)
    faces = [
        (0, 1, 3), (0, 3, 2), (4, 6, 5), (5, 6, 7),
        (0, 4, 5), (0, 5, 1), (2, 3, 6), (3, 7, 6),
        (0, 2, 4), (2, 6, 4), (1, 5, 7), (1, 7, 3),
    ]
    if open_box:
        faces.pop()
    positions = b"".join(struct.pack("<fff", x, z, -y) for x, y, z in vertices)
    indices = b"".join(struct.pack("<HHH", *triangle) for triangle in faces)
    document = {
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}], "meshes": [{"primitives": [{"attributes": {"POSITION": 0},
                                                         "indices": 1}]}],
        "buffers": [{"byteLength": len(positions) + len(indices)}],
        "bufferViews": [{"buffer": 0, "byteLength": len(positions)},
                        {"buffer": 0, "byteOffset": len(positions), "byteLength": len(indices)}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(vertices), "type": "VEC3",
                       "min": [-0.125, -0.0625, -0.0625], "max": [0.125, 0.0625, 0.0625]},
                      {"bufferView": 1, "componentType": 5123, "count": len(faces)*3, "type": "SCALAR"}],
    }
    binary = positions + indices
    return encode_glb(document, binary + bytes(-len(binary) % 4))


class PhysFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = core.load_json(
            Path("schemas/af.physics-receipt.v1.schema.json"), max_bytes=64 * 1024)

    def setUp(self):
        self.mesh = box_glb()
        self.support = core.canonical_json({
            "schema": "af.motion-support-plane/v1", "frame": FRAME,
            "point_um": [0, 0, 0], "normal": [0, 0, 1],
        }).encode()
        self.plan = {"render_sha256": sha256(self.mesh).hexdigest(),
                     "support_plane_sha256": sha256(self.support).hexdigest(),
                     "scale_um_per_glb_unit": 1_000_000, "density_kg_m3": 640, "density_unit": "kg/m3",
                     "gravity_um_s2": GRAVITY, "initial_center_height_um": 1_000_000,
                     "separated_interval_us": [0, 400_000],
                     "contact_interval_us": [550_000, 700_000]}
        self.collider = {"center_um": [0, 0, 0], "half_extents_um": HALF.copy()}

    def check(self, **changes):
        args = dict(render_glb=self.mesh, registration=self.plan,
                    registration_sha256=core.sha256_json(self.plan), support_data=self.support,
                    collider=self.collider, support_height_um=0, gravity_um_s2=GRAVITY)
        args.update(changes)
        receipt, evidence = replay.run_fixture(**args)
        core.validate(receipt, self.schema)
        return receipt, evidence

    def refusal(self, rc, reason, **changes):
        value, _ = self.check(**changes)
        self.assertEqual((value["rc"], value["reason_codes"]), (rc, [reason]))
        self.assertEqual(value["production_verdict"], "CANNOT_SEE")
        return value

    @requires_mujoco
    def test_live_geometry_mass_and_support_replay(self):
        receipt, evidence = self.check()
        destination = os.environ.get("PHYS_EVIDENCE_DIR")
        if destination:
            self.save_evidence(Path(destination), receipt, evidence)
        self.assertEqual(receipt["rc"], 0, receipt)
        self.assertEqual(receipt["threshold_status"], "TARGET_UNCALIBRATED")
        self.assertEqual({key: receipt["limits"][key] for key in (
            "min_density_kg_m3", "solver_iterations", "solver_tolerance_ppb",
            "integrator", "timestep_us",
        )}, {"min_density_kg_m3": 50, "solver_iterations": 100,
             "solver_tolerance_ppb": 1, "integrator": "Euler", "timestep_us": 200})
        collision, mass, motion = (receipt["metrics"][key] for key in ("collision", "mass", "replay"))
        self.assertEqual(mass["density_unit"], "kg/m3")
        self.assertEqual(collision["source_volume_um3"], 3_906_250_000_000_000)
        self.assertEqual(collision["lost_volume_upper_ppm"], 0)
        self.assertEqual(collision["collision_inward_upper_um"], 0)
        self.assertEqual(collision["collision_outward_upper_um"], 0)
        self.assertEqual(collision["initial_clearance_lower_um"], 937_500)
        self.assertEqual(mass["mass_ng"], [2_500_000_000_000] * 2)
        self.assertEqual(mass["com_um"], [[0, 0]] * 3)
        lengths = [Fraction(1, 4), Fraction(1, 8), Fraction(1, 8)]
        for index in range(3):
            expected = Fraction(5, 2) * sum(d*d for j, d in enumerate(lengths) if j != index) / 12
            lower, upper = mass["inertia_e18"][index]
            scaled = expected * 10**18
            self.assertEqual((lower, upper), (scaled.__floor__(), scaled.__ceil__()))
        self.assertEqual(motion["contact_observed"], motion["contact_expected"])
        self.assertGreater(motion["contact_expected"], 0)
        self.assertEqual(len({trace["run_id"] for trace in evidence["traces"]}), 3)
        self.assertEqual(evidence["traces"][0]["samples"], evidence["traces"][2]["samples"])
        contradictory = deepcopy(receipt)
        contradictory["status"] = "STALE"
        with self.assertRaises(core.ContractError):
            core.validate(contradictory, self.schema)
        empty = deepcopy(receipt)
        empty["trace_sha256s"] = []
        with self.assertRaises(core.ContractError):
            core.validate(empty, self.schema)
        altered_settings = deepcopy(receipt)
        altered_settings["limits"]["solver_iterations"] = 1
        with self.assertRaises(core.ContractError):
            core.validate(altered_settings, self.schema)

    def save_evidence(self, destination, receipt, evidence):
        for name, data in (("render.glb", self.mesh), ("support.json", self.support)):
            with (destination / name).open("xb") as stream:
                stream.write(data)
        for name, document in (("registration", self.plan), ("collider", self.collider),
                               ("scene", evidence["scene"]), ("protocol", evidence["protocol"]),
                               ("kit-receipt", evidence.get("kit_receipt")), ("receipt", receipt)):
            if document is not None:
                with (destination / (name + ".json")).open("x", encoding="utf-8") as stream:
                    stream.write(core.canonical_json(document))
        for index, trace in enumerate(evidence["traces"]):
            with (destination / f"trace-{index}.json").open("x", encoding="utf-8") as stream:
                stream.write(core.canonical_json(trace))
            with (destination / f"model-{index}.xml").open("x", encoding="utf-8") as stream:
                stream.write(evidence["models"][index])

    def test_truncated_and_displaced_colliders_fail(self):
        for mutation in (
            {"center_um": [0, 0, 0], "half_extents_um": [122_500, *HALF[1:]]},
            {"center_um": [100, 0, 0], "half_extents_um": HALF},
        ):
            with self.subTest(mutation=mutation):
                value = self.refusal(5, "collider_truncated", collider=mutation)
                self.assertGreater(value["metrics"]["collision"]["collision_inward_upper_um"], 0)

    def test_nonpositive_collider_half_extents_fail(self):
        for half in (-125_000, 0):
            with self.subTest(half=half):
                self.refusal(5, "collider_inverted",
                             collider={**self.collider, "half_extents_um": [half, *HALF[1:]]})

    def test_outward_bounds_and_initial_intersection(self):
        mesh = geometry.render_box(self.mesh)
        expanded = {"center_um": [0, 0, 0], "half_extents_um": [125_100, *HALF[1:]]}
        metrics = geometry.collision_metrics(mesh, geometry.collider_box(expanded), 1_000_000, 0)
        self.assertEqual((metrics["collision_inward_upper_um"], metrics["collision_outward_upper_um"],
                          metrics["lost_volume_upper_ppm"], metrics["extra_volume_upper_ppm"]),
                         (0, 100, 0, 800))
        excess = {"center_um": [0, 0, 0], "half_extents_um": [126_100, *HALF[1:]]}
        self.refusal(5, "collider_clearance_budget_exceeded", collider=excess)
        plan = {**self.plan, "initial_center_height_um": HALF[2]}
        self.refusal(5, "initial_clearance_not_positive", registration=plan,
                     registration_sha256=core.sha256_json(plan))

    def test_open_render_mesh_refuses(self):
        mesh = box_glb(open_box=True)
        plan = {**self.plan, "render_sha256": sha256(mesh).hexdigest()}
        self.refusal(5, "render_box_not_closed", render_glb=mesh, registration=plan,
                     registration_sha256=core.sha256_json(plan))

    def test_concave_render_has_no_fixture_collision_evidence(self):
        mesh = box_glb(concave=True)
        plan = {**self.plan, "render_sha256": sha256(mesh).hexdigest()}
        receipt, evidence = self.check(render_glb=mesh, registration=plan,
                                       registration_sha256=core.sha256_json(plan))
        self.assertEqual((receipt["rc"], receipt["reason_codes"]), (3, ["render_box_scope_unproven"]))
        self.assertEqual(receipt["verdict"], "CANNOT_SEE")
        self.assertNotIn("collision", receipt["metrics"])
        self.assertEqual(evidence["traces"], [])

    def test_implausible_density_refuses_in_declared_si_units(self):
        plan = {**self.plan, "density_kg_m3": 1}
        self.refusal(5, "density_below_fixture_floor", registration=plan,
                     registration_sha256=core.sha256_json(plan))

    def test_density_unit_is_required_and_must_be_si(self):
        missing = self.plan.copy()
        del missing["density_unit"]
        self.refusal(3, "density_unit_required", registration=missing,
                     registration_sha256=core.sha256_json(missing))
        wrong = {**self.plan, "density_unit": "g/cm3"}
        self.refusal(2, "density_unit_invalid", registration=wrong,
                     registration_sha256=core.sha256_json(wrong))

    def test_protocol_drift_fails_before_replay(self):
        scenario = replay._scenario
        for field, value, reason in (
            ("solver_iterations", 1, "solver_iterations_mismatch"),
            ("solver_tolerance_ppb", 2, "solver_tolerance_mismatch"),
            ("timestep_us", 1000, "timestep_mismatch"),
        ):
            def changed(*args):
                scene, protocol = scenario(*args)
                protocol[field] = value
                return scene, protocol

            with self.subTest(field=field), patch.object(replay, "_scenario", side_effect=changed):
                self.refusal(5, reason)

    def test_timestep_drift_has_its_own_reason(self):
        with patch.object(replay, "TIMESTEP_US", 1000):
            self.refusal(5, "timestep_mismatch")

    def test_emitted_model_options_must_match(self):
        write_mjcf = replay.write_mjcf
        for original, changed, reason in (
            ('iterations="100"', 'iterations="1"', "solver_iterations_mismatch"),
            ('tolerance="0.000000001"', 'tolerance="0.000000002"', "solver_tolerance_mismatch"),
            ('integrator="Euler"', 'integrator="RK4"', "integrator_mismatch"),
            ('timestep="0.000200"', 'timestep="0.001000"', "timestep_mismatch"),
        ):
            def altered(*args, **kwargs):
                return write_mjcf(*args, **kwargs).replace(original, changed)

            with self.subTest(reason=reason), patch.object(replay, "write_mjcf", side_effect=altered):
                self.refusal(5, reason)

    def test_phys_rejects_invalid_inertia_before_mjcf_generation(self):
        mass_properties = replay.mass_properties
        for reason in ("inertia_not_positive_definite", "inertia_triangle_inequality"):
            def with_invalid_tensor(*args):
                mass, inertial = mass_properties(*args)
                a, b, c, _, _, _ = inertial["tensor_ug_um2"]
                if reason == "inertia_not_positive_definite":
                    inertial["tensor_ug_um2"] = [a, b, c, a + b, 0, 0]
                else:
                    inertial["tensor_ug_um2"] = [b + c + 1, b, c, 0, 0, 0]
                return mass, inertial

            with self.subTest(reason=reason), patch.object(
                replay, "mass_properties", side_effect=with_invalid_tensor
            ), patch.object(replay, "write_mjcf", wraps=replay.write_mjcf) as write_mjcf:
                self.refusal(2, reason)
                write_mjcf.assert_not_called()

    def test_missing_density_or_scale_refuses(self):
        for field in ("density_kg_m3", "scale_um_per_glb_unit"):
            with self.subTest(field=field):
                plan = dict(self.plan)
                del plan[field]
                self.refusal(3, field + "_required", registration=plan,
                             registration_sha256=core.sha256_json(plan))

    def test_wrong_gravity_and_displaced_support_fail(self):
        self.refusal(5, "wrong_gravity", gravity_um_s2=[0, 0, -1_000_000])
        self.refusal(5, "support_surface_displaced", support_height_um=500)
        self.refusal(4, "support_plane_stale", support_data=self.support.replace(b"[0,0,0]", b"[0,0,1]"))

    @requires_mujoco
    def test_replay_observation_is_required(self):
        collect = replay.worker.collect

        def omit(job):
            trace = collect(job)
            sample = next(s for s in trace["samples"] if s["time_us"] == 600_000)
            del sample["contacts"]
            return trace

        with patch.object(replay.worker, "collect", side_effect=omit):
            self.refusal(3, "contact_observation_missing")

    @requires_mujoco
    def test_observed_contact_distance_and_time_must_match(self):
        collect = replay.worker.collect

        def shifted_distance(job):
            trace = collect(job)
            sample = next(s for s in trace["samples"] if s["time_us"] == 600_000)
            for contact in sample["contacts"]:
                contact["distance_um"] -= 500
            return trace

        with patch.object(replay.worker, "collect", side_effect=shifted_distance):
            self.refusal(5, "registered_contact_distance_mismatch")

        def missing_sample(job):
            trace = collect(job)
            trace["samples"].pop(600_000 // job["dt_us"])
            return trace

        with patch.object(replay.worker, "collect", side_effect=missing_sample):
            self.refusal(3, "trace_coverage_incomplete")

    @requires_mujoco
    def test_observed_empty_contact_and_reused_trace_fail(self):
        collect = replay.worker.collect

        def empty(job):
            trace = collect(job)
            next(s for s in trace["samples"] if s["time_us"] == 600_000)["contacts"] = []
            return trace

        with patch.object(replay.worker, "collect", side_effect=empty):
            self.refusal(5, "registered_contact_absent")

        def stale(job):
            trace = collect(job)
            trace["run_id"] = "earlier-attempt"
            return trace

        with patch.object(replay.worker, "collect", side_effect=stale):
            self.refusal(4, "trace_binding_stale")
