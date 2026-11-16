"""vqa3d.receipt/v1; NOT action_receipt/v0.2 schema validity.

The shared meanings of verified, verification, error and after_frame retain
those field names. No wall clock is added: identical observations and obligations
produce identical receipts. Full states and PNGs remain in recorded trace data.
"""
from .canon import canonical_json, sha256_canonical
from .verdict import final_verdict, rc_of, PRECEDENCE

SCHEMA = "vqa3d.receipt/v1"


def observation_ref(observation):
    if observation is None:
        return {"obs_id": None, "frame_sha256": None, "state_sha256": None,
                "capture_available": False, "camera_source": "none", "camera_name": ""}
    return {key: getattr(observation, key) for key in
            ("obs_id", "frame_sha256", "state_sha256", "capture_available", "camera_source", "camera_name")}


def make_receipt(adapter, obligation, before, after, verdicts, profile=None):
    verdict = final_verdict([row["verdict"] for row in verdicts])
    after_ref = observation_ref(after)
    result = {"schema": SCHEMA,
              "adapter": {"id": adapter.id, "version": adapter.version, "status": adapter.status},
              "obligation": obligation, "before": observation_ref(before), "after": after_ref,
              "state_sha256_change": (before.state_sha256 != after.state_sha256) if before and after else False,
              "state_comparison_available": before is not None and after is not None,
              "verdicts": verdicts, "final_verdict": verdict, "verdict_rc": rc_of(verdict),
              "after_frame": after_ref, "verified": verdict in ("PASS", "ADVISORY"),
              "verification": {"method": "obligation", "detail": verdicts},
              "error": None if verdict in ("PASS", "ADVISORY") else max(verdicts, key=lambda row: PRECEDENCE[row["verdict"]])["reason"]}
    raw = getattr(adapter, "wrapped", adapter)
    if hasattr(raw, "environment_receipt"):
        result["declared_environment"] = raw.environment_receipt
    profile = profile if profile is not None else getattr(adapter, "profile_identity", None)
    if profile is not None:
        if (type(profile) is not dict or set(profile) != {"id", "sha256"} or
                type(profile["id"]) is not str or not profile["id"] or
                type(profile["sha256"]) is not str or len(profile["sha256"]) != 64 or
                any(c not in "0123456789abcdef" for c in profile["sha256"])):
            raise ValueError("invalid_receipt_profile_identity")
        result["profile"] = dict(profile)
    result["receipt_sha256"] = sha256_canonical(result)
    # Detach nested caller data so later mutation cannot invalidate the receipt.
    import json
    return json.loads(canonical_json(result))
