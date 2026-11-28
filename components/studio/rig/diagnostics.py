# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare original-surface and conditioning trials and classify provider readbacks."""
from pathlib import Path

from glb_readback import geometry, joint_names
from pre_rig import normalise
from rig_common import document, invoke_blender, need, pin, write_bytes

HERE = Path(__file__).resolve().parent


def variant_record(variant, mesh, seed, repeats, *, topology=False):
    return {"id": variant, "path": mesh.name, "input": pin(mesh), "geometry": geometry(mesh, topology=topology),
            "seed": seed, "repeats": repeats}


def original_trial(original, seed):
    return variant_record("V3", original, seed, 1)


def prepare_variants(original, selected, control, out, *, seed, limit, v1_min, v2_min, blender):
    need(0 < limit <= v1_min <= v2_min, "diagnostic_triangle_targets")
    welded = out / "welded.glb"
    write_bytes(welded, normalise(selected.read_bytes()))
    invoke_blender(out, HERE / "subdivide.py", [welded, out, limit, v1_min, v2_min], blender=blender)
    variants = (("V5", control, seed, 1), ("V1", out / "V1.glb", seed, 2),
                ("V2", out / "V2.glb", seed, 2), ("V3", original, seed, 2),
                ("V4A", welded, seed + 1, 2), ("V4B", welded, seed + 2, 2))
    rows = [variant_record(*variant, topology=True) for variant in variants]
    for row, floor in zip(rows[1:3], (v1_min, v2_min), strict=True):
        need(row["geometry"]["triangles"] >= floor, "variant_below_triangle_floor:" + row["id"])
    return rows


def verify_readback(original, source, receipt_path):
    """Bind the original input and raw output to a completed host inference receipt."""
    receipt = document(receipt_path)
    input_pin, output_pin = pin(original), pin(source)
    need(receipt["schema"] == "af.unirig-host-inference/v1" and
         receipt["input_sha256"] == input_pin["sha256"] and
         receipt["rigged_sha256"] == output_pin["sha256"], "unirig_provider_receipt_mismatch")
    invocation = receipt["invocation"]
    request = receipt["request_sha256"]
    stages = receipt["stages"]
    need(isinstance(invocation, str) and bool(invocation) and isinstance(request, str)
         and len(request) == 64 and all(c in "0123456789abcdef" for c in request)
         and receipt["device"] == "gpu" and type(receipt["wall_ms"]) is int and receipt["wall_ms"] > 0
         and len(receipt["voxel_exchanges"]) == 1
         and [row["stage"] for row in stages] == ["extract", "skeleton", "skin", "merge"]
         and all(type(row["rc"]) is int and row["rc"] == 0 and type(row["wall_ms"]) is int
                 and row["wall_ms"] >= 0 for row in stages), "unirig_provider_incomplete")
    names = joint_names(source)
    return {"status": "COMPLETE", "invocation": invocation, "request_sha256": request,
            "input": input_pin, "output": output_pin, "receipt": pin(receipt_path),
            "joint_count": len(names), "joint_names": names, "provider_wall_ms": receipt["wall_ms"]}


def verify_failed_readback(mesh, attempt):
    """Admit failures from a committed scoped attempt and preserve its exit evidence."""
    source = pin(mesh)
    result = document(attempt / "result.json")
    launch = document(attempt / "host-launch.json")
    admission = document(attempt / "admission.json")
    scope_exit = document(attempt / "scope-exit.json")
    rc = result["provider_rc"]
    need(scope_exit["rc"] == 0 and scope_exit["command"] == admission["scope_command"]
         and admission["committed"] is True and type(rc) is int and rc != 0
         and result["status"] == "PROVIDER_FAILED" and result["invocation"] == launch["invocation"]
         and result["input_sha256"] == launch["input_sha256"] == source["sha256"]
         and result["variant"] == admission["variant"] and result["repeat"] == admission["repeat"]
         and result["seed"] == launch["generated_rig"]["seed"]
         and result["run_key"] == admission["run_key"], "diagnostic_failed_attempt_binding")
    return {"status": "PROVIDER_FAILED", "reason": "provider_timed_out" if rc == 124 else "provider_nonzero_exit",
            "invocation": result["invocation"], "input": source, "joint_count": None, "provider_rc": rc,
            "variant": result["variant"], "repeat": result["repeat"], "seed": result["seed"],
            "evidence": {name: pin(attempt / name) for name in
                         ("result.json", "host-launch.json", "admission.json", "scope-exit.json")}}


def report_trials(plan, observations):
    """Replay the control and two-repeat confirmation rule over verified readbacks."""
    by_variant = {row["id"]: row for row in plan}
    need(len(by_variant) == len(plan) and "V5" in by_variant, "diagnostic_plan_invalid")
    attempts = {}
    invocations = set()
    for row in observations:
        key = row["variant"], row["repeat"]
        variant = by_variant[row["variant"]]
        need(type(row["repeat"]) is int and 1 <= row["repeat"] <= variant["repeats"]
             and key not in attempts and row["invocation"] not in invocations
             and row["seed"] == variant["seed"] and row["input"] == variant["input"],
             "diagnostic_trial_binding")
        invocations.add(row["invocation"])
        attempts[key] = row
    control = attempts.get(("V5", 1))
    if control is None:
        return {"status": "PENDING", "winner": None, "reason": "control_readback_missing"}
    if control["status"] != "COMPLETE" or control["joint_count"] != 22:
        return {"status": "HOLD", "winner": None, "reason": "diagnostic_control_failed"}
    for variant, planned in by_variant.items():
        if variant == "V5":
            continue
        rows = [attempts.get((variant, repeat)) for repeat in range(1, planned["repeats"] + 1)]
        if sum(row is not None and row["status"] == "COMPLETE" and type(row["joint_count"]) is int
               and 12 <= row["joint_count"] <= 64 for row in rows) >= 2:
            return {"status": "CONFIRMED", "winner": variant}
    status = "COMPLETE_NO_WINNER" if len(attempts) == sum(row["repeats"] for row in plan) else "IN_PROGRESS"
    return {"status": status, "winner": None}
