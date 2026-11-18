"""D18 INPUT entry points independent of the legacy gate toolchain identity."""
from pathlib import Path

from tools.input import matte
from tools.input.evidence import InputIssue, bytes_digest, native_receipt, qualify_input, refuse, require
from tools.qualification import verify_receipt
from tools.qualification.manifest import Refusal, SHA256

__all__ = ["inspect_matte", "prepare_matte", "qualify_input"]


def _check_matte_source(source, proof, original, straight_alpha, read_checked, intent_digest):
    require(source in ("authored", "fixture", "birefnet"), 2, "invalid_matte_source")
    if source != "birefnet":
        require(proof is None, 2, "unexpected_birefnet_qualification")
    intent = matte.read_intent(read_checked, intent_digest)
    require(source == intent["matte_source"], 4, "matte_source_mismatch")
    if source != "birefnet":
        return None
    require(proof is not None, 5, "birefnet_qualification_required")
    require(type(proof) is dict and proof.keys() == {
        "evidence_dir", "receipt_sha256", "candidate_sha256",
    }, 2, "invalid_birefnet_qualification")
    evidence_dir = proof["evidence_dir"]
    receipt_sha = proof["receipt_sha256"]
    candidate_sha = proof["candidate_sha256"]
    require(isinstance(evidence_dir, (str, Path)) and Path(evidence_dir).is_absolute()
            and all(type(pin) is str and SHA256.fullmatch(pin) for pin in (receipt_sha, candidate_sha)),
            2, "invalid_birefnet_qualification")
    require(candidate_sha == intent["birefnet_candidate_sha256"],
            4, "birefnet_qualification_candidate_mismatch")
    require(type(original) is bytes and type(straight_alpha) is bytes, 2, "invalid_birefnet_matte_bytes")
    try:
        verified = verify_receipt(evidence_dir, expected_sha256=receipt_sha)
    except Refusal as exc:
        if str(exc) in {"qualification_pass_unproven", "receipt_evidence_unavailable"}:
            raise InputIssue(5, "birefnet_qualification_required") from exc
        raise InputIssue(exc.rc, str(exc)) from exc
    require(verified["rc"] == 0, 5, "birefnet_qualification_required")
    require(verified["provider"] == "birefnet" and verified["candidate_sha256"] == candidate_sha,
            4, "birefnet_qualification_candidate_mismatch")
    outputs = verified["runs"][1]["outputs"]
    for name, raw in (("original.png", original), ("straight.png", straight_alpha)):
        require(outputs.get(name) == {"sha256": bytes_digest(raw)[7:], "bytes": len(raw)},
                4, "birefnet_qualification_matte_mismatch")
    return {"receipt_sha256": receipt_sha, "candidate_sha256": candidate_sha}


def inspect_matte(original, straight_alpha, submitted, approved_mask, *, intent_digest, read_checked,
                  matte_source, birefnet_qualification=None):
    """Check the claimed source against the approved intent and verify the derivative."""
    try:
        binding = _check_matte_source(matte_source, birefnet_qualification, original, straight_alpha,
                                      read_checked, intent_digest)
    except InputIssue as exc:
        subject = bytes_digest(submitted) if type(submitted) is bytes else None
        return refuse(native_receipt("input.matte", subject, intent_digest), exc)
    receipt = matte.inspect_matte(original, straight_alpha, submitted, approved_mask,
                                  intent_digest=intent_digest, read_checked=read_checked)
    receipt["matte_source"] = matte_source
    if binding is not None:
        receipt["birefnet_qualification"] = binding
        receipt["birefnet_evidence_dir"] = str(birefnet_qualification["evidence_dir"])
    return receipt


def prepare_matte(original, straight_alpha, approved_mask, *, intent_digest, read_checked,
                  matte_source, birefnet_qualification=None):
    """Prepare the derivative after checking the same model provenance as inspection."""
    try:
        binding = _check_matte_source(matte_source, birefnet_qualification, original, straight_alpha,
                                      read_checked, intent_digest)
    except InputIssue as exc:
        return None, refuse(native_receipt("input.matte", None, intent_digest), exc)
    submitted, receipt = matte.prepare_matte(original, straight_alpha, approved_mask,
                                            intent_digest=intent_digest, read_checked=read_checked)
    receipt["matte_source"] = matte_source
    if binding is not None:
        receipt["birefnet_qualification"] = binding
        receipt["birefnet_evidence_dir"] = str(birefnet_qualification["evidence_dir"])
    return submitted, receipt
