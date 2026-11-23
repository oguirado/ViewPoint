"""Recompute fixture I02 evidence with BASE on the host's qualified Python runtime."""
from pathlib import Path
import resource
import time

from asset_factory import core, domains, math_acceptance
from tools.input.evidence import bytes_digest, canonical_bytes, checked_record, require
from . import evaluation
from .blender_fixture import decoded_rgb


def assess_images(receipt, *, read_checked, negative_pin, archive=None):
    """Return BASE's TARGET disposition plus the scoped numeric fixture comparison.

    read_checked authorizes the source observations, reopened scene/render bytes
    and a separately executed shifted-mouth control. archive(role, pin, bytes)
    optionally retains every canonical BASE input for independent replay.
    """
    start = time.monotonic_ns()
    stored = {}

    def put(role, obj):
        raw = canonical_bytes(obj)
        pin = bytes_digest(raw)
        stored[role, pin] = raw
        if archive:
            archive(role, pin, raw)
        return pin

    args = receipt["args"]
    subject = "sha256:" + receipt["scene_sha256"]
    scene = read_checked("face-candidate-scene", subject)
    require(scene is not None and bytes_digest(scene) == subject, 4, "base_face_scene_drift")
    views, _ = evaluation.heldout(args, read_checked, receipt["scene_sha256"])
    registration = checked_record(read_checked, "face-views", "sha256:" + args["face_views_sha256"])
    require(registration["heldout_pin"] == "sha256:" + receipt["heldout_sha256"]
            and registration["track_pin"] == "sha256:" + receipt["track_sha256"],
            4, "base_face_input_drift")
    witness = checked_record(read_checked, "face-independent-observations", registration["heldout_pin"])
    require(witness["source_projection_pin"] == "sha256:" + receipt["source_projection_sha256"],
            4, "base_source_projection_drift")
    from tools.lipsync import validate_track
    track = checked_record(read_checked, "face-viseme-track", registration["track_pin"])
    validate_track(track)
    require(len(receipt["frames"]) == len(track["frames"]), 3, "base_frame_coverage")
    negative = checked_record(read_checked, "face-negative-receipt", negative_pin)
    require(negative["args"] == args and negative["heldout_sha256"] == receipt["heldout_sha256"]
            and negative["rc"] == 5 and "held_out_landmark_limit" in negative["reasons"]
            and "protected_tile_limit.front" in negative["reasons"], 3, "base_control_unwitnessed")

    policy = {"max_channel_error_upper_ppm": evaluation.MAX_CHANNEL_PPM,
              "worst_tile_error_upper_ppm": evaluation.WORST_TILE_PPM}
    source_files = [Path(evaluation.__file__), Path(__file__), Path(math_acceptance.__file__)]
    scope = dict(gate_id="face.i02", domain_digest="sha256:" + args["face_views_sha256"],
                 evaluator_digest="sha256:" + core.sha256_file(Path(evaluation.__file__)),
                 policy_digest=put("policy", policy),
                 toolchain_digest=put("toolchain", {p.name:core.sha256_file(p) for p in source_files}))
    control = put("negative-control", dict(**scope, observed_rc=5, expected_rc=5, witness_digest=negative_pin))
    binding = dict(**scope, input_refs=["sha256:" + args["mesh_sha256"],registration["track_pin"]],
                   reference_refs=[registration["heldout_pin"]], negative_control_receipt_refs=[control])
    criteria, observations = [], []
    pixels = 0
    for view, domain in views.items():
        require(len(domain["reference_images"]) == len(track["frames"]), 3, "base_reference_coverage")
        worst = dict.fromkeys(policy, 0)
        for index, frame in enumerate(receipt["frames"]):
            cue = track["frames"][index]
            require((frame["index"],frame["pts"]) == (cue["index"],cue["pts"]), 4, "base_frame_pts_drift")
            ref_pin = domain["reference_images"][index]
            actual_pin = frame["rendered"][view]
            reference = decoded_rgb(read_checked("face-reference-render", ref_pin), ref_pin, domain["size"])
            candidate = decoded_rgb(read_checked("face-reopened-render", actual_pin), actual_pin, domain["size"])
            rect = domain["protected_rect"]
            channel, tile = evaluation.protected_residuals(reference, candidate, domain["size"], rect,
                                                          domain["tile_size"])
            pixels += (rect[2] - rect[0]) * (rect[3] - rect[1])
            for key, value in zip(policy, (channel, tile), strict=True):
                upper = math_acceptance.outward_bounds(domains.pair(value), domains.pair(value), domains.PPM)[1]
                require(receipt["protected_metrics"][view][index][key] == upper, 4, "base_face_metric_drift")
                worst[key] = max(worst[key], value)
        for name, limit in policy.items():
            shared = dict(id=view+"."+name, method_digest=scope["evaluator_digest"], unit="ppm")
            criteria.append(dict(**shared, operator="le", limit=limit, scale=domains.PPM, basis="TARGET",
                                 uncertainty_kind="exact", min_confidence_ppm=None,
                                 target_reason="synthetic_fixture_only", calibration_receipt_digest=None,
                                 proof_receipt_digest=None))
            pair = domains.pair(worst[name])
            observations.append(dict(**shared, lower=pair, upper=pair, confidence_ppm=None))

    profile = put("profile", dict(schema="af.acceptance-profile/v1", **binding, operation="af.face_fit",
                                  role="procedural_fixture", criteria=criteria))
    native = put("face-readback", receipt)
    for obs in observations:
        cert = dict(**obs, subject_digest=subject, profile_digest=profile, gate_id=scope["gate_id"],
                    domain_digest=scope["domain_digest"], witness_digest=native)
        obs["certificate_ref"] = put("certificate", cert)
    stage = put("stage", dict(schema="af.math-evidence/v1", **binding, subject_digest=subject,
                              profile_digest=profile, source_rc=0, work_counts={"protected_pixels":pixels},
                              wall_ns=time.monotonic_ns()-start,
                              peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                              observations=observations))
    result = math_acceptance.evaluate_gate(profile_digest=profile, subject_digest=subject,
                                          receipt_digest=stage, read_checked=lambda role,pin: stored.get((role,pin)))
    gate = result.get("gate")
    require(gate and result["rc"] == 3 and len(gate["metrics"]) == len(criteria)
            and all(reason.endswith(":uncalibrated_target") for reason in result["reasons"]),
            3, "base_fixture_measurements_unavailable")
    within = all(m["value"] <= m["criterion"]["limit"] for m in gate["metrics"])
    return {"base":result, "i02_decision":evaluation.I02_DECISION, "fixture_i02_verdict":"PASS" if within else "FAIL",
            "profile_digest":profile, "stage_digest":stage}
