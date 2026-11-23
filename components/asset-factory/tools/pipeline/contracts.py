"""Closed F0-kernel contracts for the pipeline's own records."""
from asset_factory import core


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string"}
NULL_TEXT = {"type": ["string", "null"]}
REF = obj({"path": TEXT, "sha256": TEXT})
REFS = {"type": "array", "items": REF}
RC = {"type": "integer", "enum": list(core.RC)}
VERDICT = {"enum": ["PASS", "FAIL", "CANNOT_SEE", "ERROR"]}
STAGE = obj({
    "schema": {"const": "af.pipeline-stage/v1"}, "stage": TEXT, "key": TEXT,
    "rc": RC, "status": TEXT, "verdict": VERDICT,
    "inputs": REFS, "previous": {"oneOf": [REF, {"type": "null"}]},
    "parameters": {"type": "object"}, "toolchain_sha256": TEXT,
    "outputs": REFS, "evidence": REFS, "subject": NULL_TEXT,
    "checks_evaluated": {"type": "integer"}, "wall_ms": {"type": "integer"},
    "peak_rss_bytes": {"type": ["integer", "null"]},
    "absence_reasons": {"type": "object"}, "reason_codes": {"type": "array", "items": TEXT},
})
RUN = obj({
    "schema": {"const": "af.pipeline-run/v1"}, "scope": TEXT,
    "rc": RC, "status": TEXT, "verdict": VERDICT, "run_key": TEXT,
    "stages": {"type": "array", "items": obj({"stage": TEXT, "rc": RC,
        "verdict": VERDICT, "receipt": REF, "cache_hit": {"type": "boolean"}})},
    "checks_evaluated": {"type": "integer"}, "wall_ms": {"type": "integer"},
    "artifact": {"oneOf": [REF, {"type": "null"}]},
    "reason_codes": {"type": "array", "items": TEXT},
})
BAKE_VERIFICATION = obj({
    "schema": {"const": "af.pipeline-bake-verification/v1"}, "rc": {"const": 0},
    "status": {"const": "OK"}, "scope": {"const": "production-uv-maps"},
    "production_evidence": {"const": True}, "receipt_sha256": TEXT,
    "source_high_sha256": TEXT, "source_low_sha256": TEXT, "policy_sha256": TEXT,
    "checks_evaluated": {"type": "integer"}, "semantic_receipts": REFS,
    "pending_gates": {"type": "array", "items": TEXT},
})


def validate_stage(record):
    core.validate(record, STAGE)
    rc = record["rc"]
    if record["status"] != core.STATUS[rc] or record["verdict"] != core.VERDICT[rc]:
        raise core.ContractError("rc_status_verdict_mismatch")
    for field in ("key", "toolchain_sha256"):
        if len(record[field]) != 64 or any(c not in "0123456789abcdef" for c in record[field]):
            raise core.ContractError("digest_invalid")
    if record["checks_evaluated"] < 0 or record["wall_ms"] < 0 or (
            record["peak_rss_bytes"] is not None and record["peak_rss_bytes"] < 0):
        raise core.ContractError("metric_invalid")
    for ref in record["inputs"] + record["outputs"] + record["evidence"] + ([record["previous"]] if record["previous"] else []):
        if not ref["path"] or len(ref["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in ref["sha256"]):
            raise core.ContractError("reference_invalid")
    if rc == 0 and (record["checks_evaluated"] <= 0 or not record["outputs"] or not record["evidence"]):
        raise core.ContractError("pass_without_evidence")
    if rc and (record["outputs"] or record["subject"] is not None):
        raise core.ContractError("refusal_with_artifact")
    if record["subject"] is not None and record["subject"] not in [r["path"] for r in record["outputs"]]:
        raise core.ContractError("subject_unbound")
    if record["peak_rss_bytes"] is None and "peak_rss_bytes" not in record["absence_reasons"]:
        raise core.ContractError("null_metric_without_reason")
