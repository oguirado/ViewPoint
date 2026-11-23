"""Source-bound fixture replay using the installed CPU MuJoCo kit."""
from decimal import Decimal
from hashlib import sha256
import json
from math import ceil, floor
import secrets
import time
from xml.etree import ElementTree

from asset_factory import core
from asset_factory.domains import FRAME, PPM, UM_PER_METER
from tools.blender.assembly.geometry import AssemblyError, frame
from tools.physics import gates, worker
from tools.physics.contracts import Refusal, digest, inertia, integer, keys, require, vector
from tools.physics.fixtures import fall_fixture
from tools.physics.mjcf import write_mjcf

from .geometry import (
    MAX_COORDINATE_UM, MAX_EXTRA_VOLUME_PPM, MAX_GLTF_BYTES, MAX_OUTWARD_UM,
    collider_box, collision_metrics, mass_properties, render_box,
)

SCOPE = "axis-aligned-box-discrete-support-replay-fixture"
# [TARGET] Observation limits for this fixture profile, uncalibrated.
CONTACT_BAND_UM = 5000
MIN_SEPARATION_UM = 1000
TIMESTEP_US = 200  # [CITED] K1 refined kit step, retained as a fixture target.
DURATION_US = 750_000
MAX_SUPPORT_BYTES = 4096
IDENTITY = [PPM, 0, 0, 0]
DENSITY_UNIT = "kg/m3"
LIMITS = {"max_collision_outward_um": MAX_OUTWARD_UM,
          "max_extra_volume_ppm": MAX_EXTRA_VOLUME_PPM,
          "contact_band_um": CONTACT_BAND_UM, "min_separation_um": MIN_SEPARATION_UM,
          "min_density_kg_m3": 50,  # [TARGET] Uncalibrated solid fixture plausibility floor.
          "solver_iterations": 100, "solver_tolerance_ppb": 1, "integrator": "Euler",
          "timestep_us": TIMESTEP_US}


def _register(render_glb, registration, registration_sha256, support_data):
    require(type(registration) is dict, 3, "registration_unavailable")
    digest(registration_sha256)
    require(core.sha256_json(registration) == registration_sha256, 4, "registration_stale")
    for field in ("scale_um_per_glb_unit", "density_kg_m3", "density_unit"):
        require(registration.get(field) is not None, 3, field + "_required")
    keys(registration, ("render_sha256", "support_plane_sha256", "scale_um_per_glb_unit",
                        "density_kg_m3", "density_unit", "gravity_um_s2", "initial_center_height_um",
                        "separated_interval_us", "contact_interval_us"))
    require(type(render_glb) is bytes and render_glb, 3, "render_mesh_unavailable")
    require(len(render_glb) <= MAX_GLTF_BYTES, 6, "render_mesh_byte_budget")
    require(sha256(render_glb).hexdigest() == registration["render_sha256"], 4, "render_mesh_stale")
    require(type(support_data) is bytes and support_data, 3, "support_plane_unavailable")
    require(len(support_data) <= MAX_SUPPORT_BYTES, 6, "support_plane_byte_budget")
    require(sha256(support_data).hexdigest() == registration["support_plane_sha256"],
            4, "support_plane_stale")
    support = json.loads(support_data)
    keys(support, ("schema", "frame", "point_um", "normal"))
    require(core.canonical_json(support).encode("utf-8") == support_data
            and support["schema"] == "af.motion-support-plane/v1", 2, "support_plane_invalid")
    vector(support["point_um"], 3)
    vector(support["normal"], 3)
    require(support["frame"] == FRAME and support["normal"] == [0, 0, 1],
            3, "support_frame_outside_fixture_scope")
    integer(support["point_um"][2], -MAX_COORDINATE_UM, MAX_COORDINATE_UM)
    integer(registration["scale_um_per_glb_unit"], UM_PER_METER, UM_PER_METER)
    require(registration["density_unit"] == DENSITY_UNIT, 2, "density_unit_invalid")
    density = integer(registration["density_kg_m3"], 1, 50_000)
    require(density >= LIMITS["min_density_kg_m3"], 5, "density_below_fixture_floor")
    integer(registration["initial_center_height_um"], -MAX_COORDINATE_UM, MAX_COORDINATE_UM)
    vector(registration["gravity_um_s2"], 3)
    gx, gy, gz = registration["gravity_um_s2"]
    require(gx == gy == 0 and -20_000_000 <= gz < 0, 3, "gravity_outside_fixture_scope")
    separated, contact = registration["separated_interval_us"], registration["contact_interval_us"]
    vector(separated, 2, 0)
    vector(contact, 2, 0)
    require(0 == separated[0] < separated[1] < contact[0] < contact[1] <= DURATION_US
            and all(t % LIMITS["timestep_us"] == 0 for t in separated + contact),
            2, "contact_intervals_invalid")
    return support["point_um"][2]


def _scenario(registration, collider, support_height_um, gravity_um_s2, inertial):
    require(TIMESTEP_US == LIMITS["timestep_us"], 5, "timestep_mismatch")
    source = {"path": "render.glb", "sha256": registration["render_sha256"],
              "media_type": "model/gltf-binary"}
    scene, protocol = fall_fixture(source)
    ground, body = scene["bodies"]
    ground["position_um"][2] = support_height_um
    body["position_um"][2] = registration["initial_center_height_um"]
    body["inertial"] = inertial
    box, = body["colliders"]
    box["position_um"] = list(collider["center_um"])
    box["data"]["half_extents_um"] = list(collider["half_extents_um"])
    protocol.update(gravity_um_s2=list(gravity_um_s2),
                    expected_gravity_um_s2=list(registration["gravity_um_s2"]),
                    timestep_us=TIMESTEP_US, steps=DURATION_US // TIMESTEP_US,
                    solver_iterations=LIMITS["solver_iterations"],
                    solver_tolerance_ppb=LIMITS["solver_tolerance_ppb"])
    require(protocol["contact"]["timeconst_us"] >= 2 * TIMESTEP_US, 5, "requested_solref_clamped")
    return scene, protocol


def _check_model_pins(xml, timestep_us):
    # The kit protocol has no integrator field. Validate the emitted model
    # options and bind those exact bytes to the worker's trace below.
    option = ElementTree.fromstring(xml).find("option")
    settings = option.attrib if option is not None else {}
    expected_tolerance = Decimal(LIMITS["solver_tolerance_ppb"]).scaleb(-9)
    expected_timestep = Decimal(timestep_us).scaleb(-6)
    for attribute, expected, reason in (
        ("iterations", str(LIMITS["solver_iterations"]), "solver_iterations_mismatch"),
        ("tolerance", format(expected_tolerance, "f"), "solver_tolerance_mismatch"),
        ("integrator", LIMITS["integrator"], "integrator_mismatch"),
        ("timestep", format(expected_timestep, "f"), "timestep_mismatch"),
    ):
        require(settings.get(attribute) == expected, 5, reason)


def _bottom(box, vertical_axis):
    return sum(min(lo * q, hi * q) for (lo, hi), q in zip(box, vertical_axis))


def _check_support(trace, registration, render, proxy, plane_um, dt):
    separated_start, separated_end = registration["separated_interval_us"]
    contact_start, contact_end = registration["contact_interval_us"]
    samples = trace.get("samples")
    require(type(samples) is list and len(samples) == DURATION_US // dt + 1,
            3, "trace_coverage_incomplete")
    # The integer trace rounds each quaternion component to a ppm and vertical
    # position to a micrometre. A normalized quaternion perturbation is at most
    # twice the original component-vector error. Rotated-point error adds a
    # further factor of two times the radius. The L1 radius bounds this box.
    radius_um = max(sum(max(abs(lo), abs(hi)) for lo, hi in box) for box in (render, proxy))
    pad_um = 1 + (4 * radius_um + PPM - 1) // PPM
    separated_count, contact_count = 0, 0
    min_separation, max_contact_gap = None, 0
    for step, sample in enumerate(samples):
        t = step * dt
        require(type(sample) is dict and sample.get("step") == step and sample.get("time_us") == t,
                3, "trace_coverage_incomplete")
        in_separation = separated_start <= t <= separated_end
        in_contact = contact_start <= t <= contact_end
        if not (in_separation or in_contact):
            continue
        contacts = sample.get("contacts")
        states = sample.get("states")
        require(type(contacts) is list and type(states) is list and len(states) == 1,
                3, "contact_observation_missing")
        state, = states
        require(type(state) is dict and state.get("body") == "box"
                and "position_um" in state and "quaternion_ppm" in state,
                3, "contact_pose_missing")
        vector(state["position_um"], 3)
        vector(state["quaternion_ppm"], 4)
        w, x, y, z = state["quaternion_ppm"]
        rotation, _ = frame({"translation_um": [0, 0, 0], "rotation_xyzw_ppm": [x, y, z, w],
                             "uniform_scale_ppm": PPM})
        center_height = state["position_um"][2] - plane_um
        gap = center_height + _bottom(render, rotation[2])
        support_distances = []
        for observation in contacts:
            require(type(observation) is dict
                    and {"geom1", "geom2", "distance_um"} <= observation.keys(),
                    3, "contact_observation_missing")
            if {observation["geom1"], observation["geom2"]} == {"ground", "box_geom"}:
                support_distances.append(integer(observation["distance_um"]))
        if in_separation:
            lower = floor(gap) - pad_um
            require(not contacts and lower >= MIN_SEPARATION_UM, 5, "registered_separation_failed")
            separated_count += 1
            min_separation = lower if min_separation is None else min(min_separation, lower)
        else:
            require(support_distances, 5, "registered_contact_absent")
            proxy_gap = center_height + _bottom(proxy, rotation[2])
            require(abs(min(support_distances) - proxy_gap) <= pad_um + 1,
                    5, "registered_contact_distance_mismatch")
            upper = ceil(abs(gap)) + pad_um
            require(upper <= CONTACT_BAND_UM, 5, "registered_contact_gap_exceeded")
            contact_count += 1
            max_contact_gap = max(max_contact_gap, upper)
    expected = (contact_end - contact_start) // dt + 1
    require(separated_count == (separated_end - separated_start) // dt + 1
            and contact_count == expected, 3, "contact_observation_missing")
    return separated_count, contact_count, expected, min_separation, max_contact_gap


def run_fixture(*, render_glb, registration, registration_sha256, support_data,
                collider, support_height_um, gravity_um_s2):
    """Run fresh h, h/2 and h traces, returning a receipt and replay evidence.

    The caller supplies the independent registration pin and runs the worker
    under the prescribed CPU scope. Collecting a trace compiles a fresh model
    in this process. Only the scoped fixture verdict can pass.
    """
    start = time.monotonic_ns()
    receipt = {
        "schema": "af.physics-receipt/v1", "scope": SCOPE,
        "threshold_status": "TARGET_UNCALIBRATED", "production_verdict": "CANNOT_SEE",
        "registration_sha256": None, "render_sha256": None, "support_plane_sha256": None,
        "collider_sha256": None, "scene_sha256": None, "protocol_sha256": None,
        "trace_sha256s": [], "limits": dict(LIMITS), "metrics": {}, "cost": None,
    }
    evidence = {"traces": [], "models": []}
    rc, reasons = 0, []
    try:
        plane_um = _register(render_glb, registration, registration_sha256, support_data)
        receipt.update(registration_sha256=registration_sha256,
                       render_sha256=registration["render_sha256"],
                       support_plane_sha256=registration["support_plane_sha256"])
        render, proxy = render_box(render_glb), collider_box(collider)
        receipt["collider_sha256"] = core.sha256_json(collider)
        metrics = receipt["metrics"]
        collision = collision_metrics(render, proxy, registration["initial_center_height_um"], plane_um)
        metrics["collision"] = collision
        # For positive-volume closed boxes, exact zero loss implies containment
        # on all axes, so the inward distance is zero for every admitted proxy.
        require(collision["lost_volume_upper_ppm"] == 0, 5, "collider_truncated")
        require(collision["collision_outward_upper_um"] <= MAX_OUTWARD_UM
                and collision["extra_volume_upper_ppm"] <= MAX_EXTRA_VOLUME_PPM,
                5, "collider_clearance_budget_exceeded")
        require(collision["initial_clearance_lower_um"] > 0, 5, "initial_clearance_not_positive")

        mass, inertial = mass_properties(render, registration["density_kg_m3"], receipt["render_sha256"])
        require(inertial["com_um"] == [0, 0, 0] and all(lo + hi == 0 for lo, hi in render),
                3, "fixture_center_not_origin")
        inertia(inertial, receipt["render_sha256"])
        mass["density_unit"] = DENSITY_UNIT
        metrics["mass"] = mass
        integer(support_height_um)
        require(support_height_um == plane_um, 5, "support_surface_displaced")
        vector(gravity_um_s2, 3)
        require(gravity_um_s2 == registration["gravity_um_s2"], 5, "wrong_gravity")
        scene, protocol = _scenario(registration, collider, support_height_um, gravity_um_s2, inertial)
        timestep_us = protocol["timestep_us"]
        require(type(timestep_us) is int and timestep_us == LIMITS["timestep_us"],
                5, "timestep_mismatch")
        evidence.update(scene=scene, protocol=protocol)
        receipt.update(scene_sha256=core.sha256_json(scene), protocol_sha256=core.sha256_json(protocol))
        traces, models = evidence["traces"], evidence["models"]
        run_ids, model_hashes, observations = [], [], []
        for dt in (timestep_us, timestep_us // 2, timestep_us):
            nonce = secrets.token_hex(16)
            xml = write_mjcf(scene, protocol, timestep_us=dt)
            _check_model_pins(xml, dt)
            model_hash = sha256(xml.encode("utf-8")).hexdigest()
            trace = worker.collect({"scene": scene, "protocol": protocol, "dt_us": dt,
                                    "steps": DURATION_US // dt, "run_id": nonce})
            traces.append(trace)
            models.append(xml)
            receipt["trace_sha256s"].append(core.sha256_json(trace))
            require(trace.get("run_id") == nonce and trace.get("model_sha256") == model_hash
                    and trace.get("timestep_us") == dt
                    and trace.get("scene_sha256") == receipt["scene_sha256"]
                    and trace.get("protocol_sha256") == receipt["protocol_sha256"], 4, "trace_binding_stale")
            observations.append(_check_support(trace, registration, render, proxy, plane_um, dt))
            run_ids.append(nonce)
            model_hashes.append(model_hash)

        kit = gates.evaluate(scene, protocol, traces, run_ids=run_ids, model_sha256s=model_hashes)
        evidence["kit_receipt"] = kit
        require(kit["rc"] == 0, kit["rc"], next(iter(kit["reason_codes"]), "kit_replay_failed"))
        metrics["replay"] = {
            **kit["metrics"], "separated_observed": sum(row[0] for row in observations),
            "contact_observed": sum(row[1] for row in observations),
            "contact_expected": sum(row[2] for row in observations),
            "min_registered_separation_lower_um": min(row[3] for row in observations),
            "max_registered_contact_gap_upper_um": max(row[4] for row in observations),
        }
        receipt["cost"] = {"peak_process_rss_bytes": max(t["peak_rss_bytes"] for t in traces),
                           "worker_wall_ms": sum(t["wall_ms"] for t in traces)}
    except Refusal as exc:
        rc, reasons = exc.rc, [exc.code]
    except MemoryError:
        rc, reasons = 6, ["fixture_memory_budget"]
    except (core.ContractError, AssemblyError, ElementTree.ParseError, TypeError,
            ValueError, KeyError, RuntimeError, OverflowError):
        rc, reasons = 2, ["fixture_input_or_runtime_invalid"]
    receipt.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc],
                   reason_codes=reasons, wall_ns=time.monotonic_ns() - start)
    return receipt, evidence
