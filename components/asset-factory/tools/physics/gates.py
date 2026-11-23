"""Independent integer oracles over freshly collected, input-bound traces.

G12 is scoped to rigid-body qualification scenes with an explicit free-fall
interval. Contact distances are sampled at every step; this is not a continuous
collision or shape-fidelity certificate. Accuracy thresholds are caller-declared
within their ceilings; the analytic qualification SI reference is frozen.
"""
from fractions import Fraction
import math

from asset_factory import core
from .contracts import (Refusal, require, keys, integer, vector, digest, quaternion,
                        validate_scene, validate_protocol, NORMALIZATION_LENGTH_UM,
                        MAX_TRAJECTORY_DISPLACEMENT_UM)
from .worker import RUNTIME_VERSION


def _distance(a, b):
    squared = sum((x-y)**2 for x, y in zip(a, b))
    root = math.isqrt(squared)
    root += root*root != squared
    return root


def _error_ppm(a, b, scale):
    return (_distance(a, b) * 1000000 + scale-1) // scale


def _trace(trace, scene, p, run_id, dt, model_sha256):
    core.canonical_json(trace)
    keys(trace, ("schema", "run_id", "scene_sha256", "protocol_sha256", "model_sha256",
                 "timestep_us", "runtime", "admitted_inertia", "samples", "wall_ms", "peak_rss_bytes"))
    require(trace["schema"] == "af.physics-trace/v1")
    require(trace["run_id"] == run_id and trace["scene_sha256"] == core.sha256_json(scene)
            and trace["protocol_sha256"] == core.sha256_json(p)
            and trace["model_sha256"] == model_sha256, 4, "trace_binding_stale")
    integer(trace["timestep_us"], dt, dt)
    runtime = trace["runtime"]
    keys(runtime, ("name", "version", "module_sha256", "native_libraries"))
    require(runtime["name"] == "mujoco" and runtime["version"] == RUNTIME_VERSION,
            4, "runtime_version_stale")
    digest(runtime["module_sha256"])
    require(type(runtime["native_libraries"]) is list and runtime["native_libraries"],
            3, "native_runtime_identity_unavailable")
    for lib in runtime["native_libraries"]:
        keys(lib, ("name", "sha256"))
        require(type(lib["name"]) is str and "/" not in lib["name"])
        digest(lib["sha256"])
    integer(trace["wall_ms"], 0)
    integer(trace["peak_rss_bytes"], 1)
    dynamic = [b for b in scene["bodies"] if b["dynamic"]]
    require(type(trace["admitted_inertia"]) is list and len(trace["admitted_inertia"]) == len(dynamic))
    mass_error_ppm, tensor_error_ppm, com_error_um = 0, 0, 0
    for body, admitted in zip(dynamic, trace["admitted_inertia"]):
        keys(admitted, ("body", "mass_ug", "com_um", "tensor_ug_um2"))
        require(admitted["body"] == body["id"])
        integer(admitted["mass_ug"], 1)
        vector(admitted["com_um"], 3)
        vector(admitted["tensor_ug_um2"], 6)
        expected = body["inertial"]
        tol = p["inertia_tolerance_ppm"]
        mass_difference = abs(admitted["mass_ug"]-expected["mass_ug"])
        mass_error_ppm = max(mass_error_ppm,
                             (mass_difference*1000000 + expected["mass_ug"]-1)//expected["mass_ug"])
        require(abs(admitted["mass_ug"]-expected["mass_ug"])*1000000 <= expected["mass_ug"]*tol,
                5, "compiled_mass_mismatch")
        scale = max(expected["tensor_ug_um2"][:3])
        tensor_difference = max(abs(a-b) for a,b in zip(admitted["tensor_ug_um2"], expected["tensor_ug_um2"]))
        tensor_error_ppm = max(tensor_error_ppm, (tensor_difference*1000000 + scale-1)//scale)
        com_error_um = max(com_error_um, max(abs(a-b) for a,b in zip(admitted["com_um"], expected["com_um"])))
        require(tensor_difference*1000000 <= scale*tol, 5, "compiled_inertia_mismatch")
        require(admitted["com_um"] == expected["com_um"], 5, "compiled_com_mismatch")
    samples = trace["samples"]
    require(type(samples) is list and samples, 3, "trace_coverage_absent")
    count = p["steps"] * p["timestep_us"] // dt
    require(len(samples) == count+1, 3, "trace_coverage_incomplete")
    geom_ids = {g["id"] for b in scene["bodies"] for g in b["colliders"]}
    maximum, contact_samples = 0, 0
    for step, sample in enumerate(samples):
        keys(sample, ("step", "time_us", "states", "contacts"))
        integer(sample["step"], step, step)
        integer(sample["time_us"], step*dt, step*dt)
        require(type(sample["states"]) is list and len(sample["states"]) == len(dynamic))
        for body, state in zip(dynamic, sample["states"]):
            keys(state, ("body", "position_um", "quaternion_ppm"))
            require(state["body"] == body["id"])
            vector(state["position_um"], 3)
            quaternion(state["quaternion_ppm"])
            if step == 0:
                require(state["position_um"] == body["position_um"]
                        and state["quaternion_ppm"] == body["quaternion_ppm"], 5, "initial_state_mismatch")
        contacts = sample["contacts"]
        require(type(contacts) is list and len(contacts) <= p["contact"]["max_contacts"])
        contact_samples += bool(contacts)
        for contact in contacts:
            keys(contact, ("geom1", "geom2", "distance_um", "normal_force_uN"))
            require(contact["geom1"] in geom_ids and contact["geom2"] in geom_ids
                    and contact["geom1"] != contact["geom2"])
            integer(contact["distance_um"])
            integer(contact["normal_force_uN"], 0)
            depth = max(0, -contact["distance_um"])
            if step == 0:
                require(depth <= p["contact"]["initial_penetration_um"], 5, "initial_intersection")
            maximum = max(maximum, depth)
            require(depth <= p["contact"]["max_penetration_um"], 5, "penetration_exceeded")
    require(not p["contact"]["require_contact"] or contact_samples > 0, 5, "required_contact_absent")
    return maximum, contact_samples, mass_error_ppm, tensor_error_ppm, com_error_um


def evaluate(scene, protocol, traces, *, run_ids, model_sha256s):
    """The caller owns fresh worker invocations and the expected nonces/model hashes."""
    metrics, reasons = {}, []
    trace_hashes = []
    try:
        require(type(traces) is list and all(type(t) is dict for t in traces),
                code="trace_contract_invalid")
        require(type(run_ids) is list and type(model_sha256s) is list,
                code="trace_contract_invalid")
        trace_hashes = [core.sha256_json(t) for t in traces]
        validate_scene(scene)
        validate_protocol(protocol)
        p = protocol
        require(p["gravity_um_s2"] == p["expected_gravity_um_s2"], 5, "wrong_gravity")
        require(len(traces) == len(run_ids) == len(model_sha256s) == 3, 3, "runs_incomplete")
        require(len(set(run_ids)) == 3, code="run_identity_reused")
        contacts = [_trace(t, scene, p, nonce, dt, sha)
                    for t, nonce, dt, sha in zip(traces, run_ids,
                        (p["timestep_us"], p["timestep_us"]//2, p["timestep_us"]), model_sha256s)]
        require(traces[0]["runtime"] == traces[1]["runtime"] == traces[2]["runtime"],
                4, "runtime_identity_drift")
        metrics["max_penetration_um"] = max(x[0] for x in contacts)
        metrics["contact_samples"] = sum(x[1] for x in contacts)
        metrics["compiled_mass_error_ppm"] = max(x[2] for x in contacts)
        metrics["compiled_tensor_error_ppm"] = max(x[3] for x in contacts)
        metrics["compiled_com_error_um"] = max(x[4] for x in contacts)
        require(traces[0]["samples"] == traces[2]["samples"], 5, "replay_nondeterministic")
        maximum, rotations, displacement = 0, 0, 0
        for coarse, fine in zip(traces[0]["samples"], traces[1]["samples"][::2]):
            for a, b in zip(coarse["states"], fine["states"]):
                displacement = max(displacement, _distance(a["position_um"], b["position_um"]))
                maximum = max(maximum, _error_ppm(a["position_um"], b["position_um"], NORMALIZATION_LENGTH_UM))
                qa, qb = a["quaternion_ppm"], b["quaternion_ppm"]
                rotations = max(rotations, min(_error_ppm(qa, qb, 1000000),
                                               _error_ppm(qa, [-v for v in qb], 1000000)))
        metrics.update(trajectory_error_ppm=maximum, quaternion_error_ppm=rotations,
                       trajectory_displacement_um=displacement,
                       normalization_length_um=NORMALIZATION_LENGTH_UM)
        require(displacement <= MAX_TRAJECTORY_DISPLACEMENT_UM
                and max(maximum, rotations) <= p["max_trajectory_error_ppm"], 5, "timestep_diverged")
        fall = p["free_fall"]
        body = next((b for b in scene["bodies"] if b["id"] == fall["body_id"] and b["dynamic"]), None)
        require(body is not None, code="analytic_body_missing")
        # Pure translation is the admitted analytic fixture, with no hidden torque/COM offset.
        require(body["inertial"]["com_um"] == [0,0,0] and body["angular_velocity_urad_s"] == [0,0,0],
                3, "analytic_assumptions_unproven")
        error = Fraction(0)
        for trace in traces:
            limit_us = fall["steps"] * p["timestep_us"]
            for sample in trace["samples"]:
                if sample["time_us"] > limit_us:
                    break
                require(not sample["contacts"], 5, "analytic_interval_contact")
                t = Fraction(sample["time_us"], 1000000)
                state = next(s for s in sample["states"] if s["body"] == body["id"])
                for axis in range(3):
                    expected = body["position_um"][axis] + body["velocity_um_s"][axis]*t \
                               + Fraction(1,2)*p["expected_gravity_um_s2"][axis]*t*t
                    error = max(error, abs(state["position_um"][axis]-expected))
        metrics["analytic_error_um"] = math.ceil(error)
        require(error <= fall["tolerance_um"], 5, "analytic_fall_failed")
        rc = 0
    except Refusal as exc:
        rc, reasons = exc.rc, [exc.code]
    except (core.ContractError, KeyError, TypeError, ValueError, StopIteration):
        rc, reasons = 2, ["trace_contract_invalid"]
    return {"schema": "af.physics-receipt/v1", "rc": rc, "status": core.STATUS[rc],
            "verdict": core.VERDICT[rc], "reason_codes": reasons,
            "scene_sha256": _identity(scene), "protocol_sha256": _identity(protocol),
            "trace_sha256s": trace_hashes, "metrics": metrics,
            "scope": "rigid_body_analytic_qualification", "threshold_status": "TARGET_UNCALIBRATED"}


def _identity(obj):
    try:
        return core.sha256_json(obj)
    except core.ContractError:
        return None
