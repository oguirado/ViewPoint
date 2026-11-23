"""Read the vqa3d profile-trace family; never launch an evaluator.

Producer coordinates: vqa3d/{canon.py:13-37,receipt.py:21-51,
profile.py:688-719,1482-1494}. Hashes bind content, not its authenticity.
The caller supplies the source/profile pins and mandatory check coverage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

from asset_factory import core

MAX_RECEIPT_BYTES = 16 * 2**20
_DOMAIN_RC = {"PASS": 0, "ADVISORY": 0, "FAIL": 1, "ERROR": 2,
              "CANNOT_SEE": 3, "SKIP": 3, "UNCLEAR": 3}
_ORDER = {"PASS": 0, "ADVISORY": 1, "CANNOT_SEE": 2, "SKIP": 2,
          "UNCLEAR": 2, "FAIL": 3, "ERROR": 4}


class _Refusal(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason


def _require(condition, reason="schema_mismatch", rc=2):
    if not condition:
        raise _Refusal(rc, reason)


def _canonical(value):
    """Locally implement vqa3d canonical bytes, WITHOUT AF's newline."""
    def check(obj):
        if obj is None or type(obj) in (str, int, bool):
            return
        if type(obj) is list:
            for item in obj:
                check(item)
            return
        if type(obj) is dict and all(type(key) is str for key in obj):
            for item in obj.values():
                check(item)
            return
        raise _Refusal(2, "noncanonical_type")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _hash(value):
    return (type(value) is str and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _text(value):
    return type(value) is str and bool(value)


def _family(value, schema):
    _require(type(value) is dict)
    _require(value.get("schema") == schema, "unsupported_family")


def _identity(value, expected):
    _require(value is not None, "profile_missing", 3)
    _require(type(value) is dict and set(value) == {"id", "sha256"})
    _require(_text(value["id"]) and _hash(value["sha256"]))
    _require(value == expected, "profile_stale", 4)


def _verify_hash(value, field):
    supplied = value.get(field)
    _require(supplied is not None, field + "_missing", 3)
    _require(_hash(supplied), "invalid_hash")
    _require(supplied == _digest({k: v for k, v in value.items() if k != field}),
             field + "_stale", 4)


def _verdict(value):
    verdict = value.get("final_verdict")
    _require(type(verdict) is str and verdict in _DOMAIN_RC, "unknown_verdict")
    _require(type(value.get("verdict_rc")) is int
             and value["verdict_rc"] == _DOMAIN_RC[verdict], "verdict_rc_mismatch")
    return verdict


def _receipt_shape(receipt):
    adapter = receipt.get("adapter")
    _require(type(adapter) is dict
             and all(_text(adapter.get(k)) for k in ("id", "version", "status")))
    _require(_text(receipt.get("why")))
    for key in ("before", "after", "after_frame"):
        observation = receipt.get(key)
        _require(type(observation) is dict)
        _require(type(observation.get("capture_available")) is bool)
        _require(all(type(observation.get(k)) is str for k in ("camera_source", "camera_name")))
        _require("obs_id" in observation and (observation["obs_id"] is None
                                             or _text(observation["obs_id"])))
        for field in ("frame_sha256", "state_sha256"):
            _require(field in observation and (observation[field] is None or _hash(observation[field])))
        # observation_ref(None) has this exact absent shape. A present
        # observation always has state identity, even without a pixel capture.
        if observation["obs_id"] is None:
            absent = {"frame_sha256": None, "state_sha256": None,
                      "capture_available": False, "camera_source": "none", "camera_name": ""}
            _require(all(observation[k] == value for k, value in absent.items()),
                     "observation_reference_mismatch")
        else:
            _require(_hash(observation["state_sha256"]), "observation_reference_mismatch")
    for key in ("state_sha256_change", "state_comparison_available"):
        _require(type(receipt.get(key)) is bool)
    _require(receipt["after_frame"] == receipt["after"], "after_frame_mismatch")
    comparison = all(receipt[key]["obs_id"] is not None for key in ("before", "after"))
    _require(receipt["state_comparison_available"] == comparison, "state_comparison_mismatch")
    changed = comparison and receipt["before"]["state_sha256"] != receipt["after"]["state_sha256"]
    _require(receipt["state_sha256_change"] == changed, "state_change_mismatch")
    verification = receipt.get("verification")
    _require(type(verification) is dict and verification.get("method") == "obligation"
             and verification.get("detail") == receipt.get("verdicts"))
    _require("error" in receipt and (receipt["error"] is None or type(receipt["error"]) is str))
    if "wall_ms" in receipt:
        _require(type(receipt["wall_ms"]) is int and receipt["wall_ms"] >= 0)


def _result(rc, reasons=(), *, source=None, profile=None, trace_hash=None,
            file_hash=None, failed=()):
    # Malformed caller fields must not leak malformed AF records on refusal.
    profile = profile if type(profile) is dict else {}
    profile = {"id": profile.get("id") if _text(profile.get("id")) else None,
               "sha256": profile.get("sha256") if _hash(profile.get("sha256")) else None}
    return {"schema": "af.bridge-verdict/v1", "verdict": core.VERDICT[rc],
            "rc": rc, "reason_codes": list(reasons), "source_sha256": source if _hash(source) else None,
            "profile": profile,
            "receipt_file_sha256": file_hash, "trace_sha256": trace_hash if _hash(trace_hash) else None,
            "failed_checks": list(failed)}


def evaluate_trace(trace: dict, *, expected_source_sha256: str, expected_profile_id: str,
                   expected_profile_sha256: str, required_check_ids: tuple[str, ...]) -> dict:
    """Validate family, bindings and child evidence, then recompute the summary.

    SKIP/UNCLEAR are conservative incomplete extensions with domain rc 3.
    All children (including advisory/surplus children) remain visible to the gate.
    No GLB-to-scene conversion is inferred from a scene's source pin.
    """
    source, profile, trace_hash, failed = None, None, None, []
    try:
        _require(_hash(expected_source_sha256) and _hash(expected_profile_sha256)
                 and _text(expected_profile_id), "expectation_invalid")
        _require(type(required_check_ids) is tuple
                 and all(_text(item) for item in required_check_ids), "coverage_invalid")
        _require(len(set(required_check_ids)) == len(required_check_ids), "duplicate_check")
        _family(trace, "vqa3d.profile-trace/v1")
        _canonical(trace)
        profile = trace.get("profile")
        trace_hash = trace.get("trace_sha256")
        provenance = trace.get("provenance")
        _require(type(provenance) is dict)
        source = provenance.get("source_sha256")
        expected = {"id": expected_profile_id, "sha256": expected_profile_sha256}
        _identity(profile, expected)
        _require(source is not None, "source_missing", 3)
        _require(_hash(source), "invalid_hash")
        _require(source == expected_source_sha256, "source_stale", 4)
        receipts, summary = trace.get("receipts"), trace.get("summary")
        _require(type(receipts) is list)
        _family(summary, "vqa3d.profile-summary/v1")
        _identity(summary.get("profile"), expected)
        reported = _verdict(summary)
        _require(type(summary.get("checks")) is int and summary["checks"] >= 0)
        seen, gates, advisory = set(), [], []
        for receipt in receipts:
            _family(receipt, "vqa3d.receipt/v1")
            _receipt_shape(receipt)
            check_id = receipt.get("check_id")
            _require(_text(check_id))
            _require(check_id not in seen, "duplicate_check")
            seen.add(check_id)
            severity = receipt.get("severity")
            _require(severity in ("gate", "advisory"), "severity_invalid")
            _identity(receipt.get("profile"), expected)
            verdict = _verdict(receipt)
            _require(type(receipt.get("verified")) is bool)
            _require(type(receipt.get("obligation")) is dict)
            rows = receipt.get("verdicts")
            _require(type(rows) is list and bool(rows), "evidence_missing", 3)
            for row in rows:
                _require(type(row) is dict and type(row.get("verdict")) is str
                         and row["verdict"] in _ORDER, "unknown_verdict")
                _require(type(row.get("reason")) is str and _text(row.get("oracle")))
            recomputed = max((row["verdict"] for row in rows), key=_ORDER.__getitem__)
            _require(recomputed == verdict, "receipt_verdict_mismatch")
            verified = recomputed in ("PASS", "ADVISORY")
            _require(receipt["verified"] == verified, "receipt_verified_mismatch")
            error = None if verified else max(rows, key=lambda row: _ORDER[row["verdict"]])["reason"]
            _require(receipt["error"] == error, "receipt_error_mismatch")
            _verify_hash(receipt, "receipt_sha256")
            (gates if severity == "gate" else advisory).append(verdict)
            if verdict != "PASS":
                failed.append({"check_id": check_id, "severity": severity,
                               "final_verdict": verdict, "verdict_rc": receipt["verdict_rc"]})
        _verify_hash(trace, "trace_sha256")
        computed = max(gates, key=_ORDER.__getitem__) if gates else "CANNOT_SEE"
        if computed == "PASS" and any(v != "PASS" for v in advisory):
            computed = "ADVISORY"
        _require(summary["checks"] == len(receipts) and reported == computed,
                 "summary_mismatch")
        actions = {r["check_id"]: r["suggested_action"] for r in receipts
                   if r.get("suggested_action")}
        _require(summary.get("suggested_actions", {}) == actions, "summary_mismatch")
        missing = sorted(set(required_check_ids) - seen)
        # A required check cannot be demoted to advisory by the evidence producer.
        demoted = [r["check_id"] for r in receipts
                   if r["check_id"] in required_check_ids and r["severity"] != "gate"]
        all_verdicts = gates + advisory
        worst = max(all_verdicts, key=_ORDER.__getitem__) if all_verdicts else "CANNOT_SEE"
        if worst == "ERROR":
            rc, reasons = 2, ["upstream_error"]
        elif worst == "FAIL":
            rc, reasons = 5, ["upstream_fail"]
        elif not required_check_ids or missing or demoted or not gates:
            rc, reasons = 3, ["coverage_incomplete"]
        elif worst != "PASS":
            rc, reasons = 3, ["evidence_incomplete"]
        else:
            rc, reasons = 0, []
        for check_id in missing:
            failed.append({"check_id": check_id, "severity": "gate",
                           "final_verdict": "MISSING", "verdict_rc": None})
        return _result(rc, reasons, source=source, profile=profile,
                       trace_hash=trace_hash, failed=failed)
    except _Refusal as exc:
        return _result(exc.rc, [exc.reason], source=source, profile=profile,
                       trace_hash=trace_hash, failed=failed)
    except (RecursionError, ValueError, TypeError, UnicodeError):
        return _result(2, ["schema_mismatch"])


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_key")
        result[key] = value
    return result


def _reject_number(value):
    raise _Refusal(2, "non_integer_number")


def read_receipt(receipt_path: Path, *, expected_source_sha256: str, expected_profile_id: str,
                 expected_profile_sha256: str, required_check_ids: tuple[str, ...]) -> dict:
    """Read bounded regular-file bytes without writing to any caller path."""
    file_hash = None
    try:
        # NONBLOCK prevents an untrusted FIFO from hanging this reader.
        fd = os.open(receipt_path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "not_regular_file")
            raw = stream.read(MAX_RECEIPT_BYTES + 1)
        _require(len(raw) <= MAX_RECEIPT_BYTES, "receipt_budget_exceeded", 6)
        file_hash = hashlib.sha256(raw).hexdigest()
        _require(bool(raw.strip()), "receipt_empty", 3)
        trace = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_float=_reject_number, parse_constant=_reject_number)
        result = evaluate_trace(trace, expected_source_sha256=expected_source_sha256,
                                expected_profile_id=expected_profile_id,
                                expected_profile_sha256=expected_profile_sha256,
                                required_check_ids=required_check_ids)
        result["receipt_file_sha256"] = file_hash
        return result
    except FileNotFoundError:
        return _result(3, ["receipt_missing"])
    except _Refusal as exc:
        return _result(exc.rc, [exc.reason], file_hash=file_hash)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return _result(2, ["receipt_invalid"], file_hash=file_hash)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Refusal(2, "usage_error")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profile-sha256", required=True)
    parser.add_argument("--required-check", action="append", default=[])
    try:
        args = parser.parse_args(argv)
        result = read_receipt(args.receipt, expected_source_sha256=args.source_sha256,
                              expected_profile_id=args.profile,
                              expected_profile_sha256=args.profile_sha256,
                              required_check_ids=tuple(args.required_check))
    except _Refusal as exc:
        result = _result(exc.rc, [exc.reason])
    return core.emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
