"""Verdict algebra, one-way judge fold and mutant qualification (BSPEC_BORACLES sec 4, 5 J3)."""
import hashlib
import json

PASS, FAIL, CANNOT_SEE, ADVISORY, ERROR = "PASS", "FAIL", "CANNOT_SEE", "ADVISORY", "ERROR"
VERDICTS = (PASS, FAIL, CANNOT_SEE, ADVISORY)
# Fold precedence of v1.0: ERROR 2 > FAIL 1 > INCOMPLETE 3 > PASS 0.
RC_PASS, RC_FAIL, RC_ERROR, RC_INCOMPLETE = 0, 1, 2, 3
_PRECEDENCE = {ERROR: 4, FAIL: 3, CANNOT_SEE: 2, "INCOMPLETE": 2, ADVISORY: 1, PASS: 0}
_RC = {ERROR: RC_ERROR, FAIL: RC_FAIL, CANNOT_SEE: RC_INCOMPLETE, "INCOMPLETE": RC_INCOMPLETE, ADVISORY: RC_PASS, PASS: RC_PASS}
ID_RE = "bm."


class OracleError(Exception):
    pass


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_canonical(obj):
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


# @anchor:bv.oracles.max_precedence
def max_precedence(verdicts):
    """Worst verdict of the list; unknown strings are an error, never a pass."""
    worst = PASS
    for v in verdicts:
        if v not in _PRECEDENCE:
            raise OracleError("unknown_verdict:" + str(v))
        if _PRECEDENCE[v] > _PRECEDENCE[worst]:
            worst = v
    return worst


def rc_of(verdict):
    return _RC[verdict]


# @anchor:bv.oracles.final_verdict
def final_verdict(sd, px, judge):
    """J3 one-way: judge may add FAIL/INCOMPLETE, never clear an SD/PX FAIL. Judge ADVISORY/unparsed is CANNOT_SEE."""
    judge = [CANNOT_SEE if v not in _PRECEDENCE else v for v in judge]
    return max_precedence(list(sd) + list(px) + judge)


def validate_row(row):
    for key in ("schema", "check_id", "oracle_id", "metrics", "thresholds", "verdict", "reason"):
        if key not in row:
            raise OracleError("row_missing:" + key)
    if row["verdict"] not in VERDICTS:
        raise OracleError("row_verdict:" + str(row["verdict"]))
    for name, value in row["metrics"].items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise OracleError("row_non_integer_metric:" + name)


def _interval_rows(report, check_id, start, end):
    return [r for r in report.get("findings", []) if r.get("check_id") == check_id
            and (r.get("render_index") is None or start <= r["render_index"] <= end)]


def _fail_interval(report, check_id, start, end):
    """True iff every row of check_id inside [start,end] is FAIL and at least one exists."""
    rows = _interval_rows(report, check_id, start, end)
    return bool(rows) and all(r.get("verdict") == FAIL for r in rows)


# @anchor:bv.oracles.qualify_mutant
def qualify_mutant(entry, clean_report, mutant_report, receipt):
    """v1.0 kill rule. Returns {id,status,reason}; status KILLED|SURVIVED|INVALID|EQUIVALENT_FP|EQUIVALENT_OK."""
    mid = entry.get("id", "")
    expected = entry.get("expected_check_ids")
    iv = entry.get("interval") or {}
    start, end = iv.get("start_render_index"), iv.get("end_render_index")

    def out(status, reason):
        return {"id": mid, "status": status, "reason": reason}

    if (not isinstance(mid, str) or not mid.startswith(ID_RE) or not expected or len(expected) != len(set(expected))
            or type(start) is not int or type(end) is not int or start < 0 or end < start
            or not isinstance(entry.get("fault"), dict)
            or sha256_canonical(entry["fault"]) != entry.get("fault_spec_sha256")):
        return out("INVALID", "invalid_mutant_manifest_entry")
    if clean_report.get("verdict") != PASS or clean_report.get("rc") != RC_PASS:
        return out("INVALID", "clean_baseline_not_passing")
    if not isinstance(receipt, dict) or receipt.get("mutant_id") != mid:
        return out("INVALID", "fault_receipt_mismatch")
    if receipt.get("fault_spec_sha256") != entry["fault_spec_sha256"]:
        return out("INVALID", "fault_spec_sha256_mismatch")
    if receipt.get("witness_before") == receipt.get("witness_after") or not receipt.get("witness_before"):
        return out("INVALID", "mutant_noop")
    if mutant_report.get("verdict") == ERROR or mutant_report.get("rc") == RC_ERROR:
        return out("INVALID", "mutant_run_error")
    killed = all(_fail_interval(mutant_report, c, start, end) for c in expected)
    if entry.get("equivalent") is True:
        any_fail = mutant_report.get("verdict") == FAIL
        if any_fail or any(r.get("verdict") == FAIL for c in expected for r in _interval_rows(mutant_report, c, start, end)):
            return out("EQUIVALENT_FP", "equivalent_fp")
        # EQUIVALENT_OK needs observed evidence: absent or CANNOT_SEE/INCOMPLETE rows certify nothing (JB3-5).
        for c in expected:
            rows = _interval_rows(mutant_report, c, start, end)
            if not rows:
                return out("INVALID", "equivalent_evidence_missing")
            if any(r.get("verdict") not in (PASS, ADVISORY) for r in rows):
                return out("INVALID", "equivalent_evidence_incomplete")
        return out("EQUIVALENT_OK", "no_false_positive")
    return out("KILLED", "all_expected_fail") if killed else out("SURVIVED", "expected_check_not_failing")
