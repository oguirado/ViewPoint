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
                "capture_available": False}
    return {key: getattr(observation, key) for key in
            ("obs_id", "frame_sha256", "state_sha256", "capture_available")}


def make_receipt(adapter, obligation, before, after, verdicts):
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
    result["receipt_sha256"] = sha256_canonical(result)
    # Detach nested caller data so later mutation cannot invalidate the receipt.
    import json
    return json.loads(canonical_json(result))
