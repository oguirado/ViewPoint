"""Operation-scoped mathematical acceptance for factory gates (IU00).

canonical_digest(obj) returns a registry SHA-256 over core.canonical_json UTF-8
bytes, including its final newline. For existing files hash their original bytes.
outward_bounds(lower, upper, scale=1) takes [integer numerator, positive integer
denominator] pairs and returns (floor(lower * scale), ceil(upper * scale)).
Use scale=1_000_000 for a ratio in ppm. Verdict comparisons use exact fractions.

evaluate_gate(*, profile_digest, subject_digest, receipt_digest, read_checked)
returns rc/status/verdict, reasons and a registry-shaped gate when available.
The caller supplies protected pins. read_checked(role, digest) is a TRUSTED
reader that returns verified bytes or None. Its role-specific allowlists must
come from the policy owner, independent evaluator and qualification reviewer.
A candidate-controlled blob store is insufficient. The reader qualifies the
underlying domain, reference authority, control runs, evaluator certificates
and (for sampling) the statistical design. BASE checks byte identity, binding,
coverage and arithmetic. It never interprets a candidate's PASS as evidence.

The 'profile' bytes use schemas/af.acceptance-profile.v1.schema.json. Pin an
independent domain manifest describing the camera/pose/time envelope, required
views and material regions, exclusions, work ceilings and risk allocation.
Expand every required metric/domain occurrence to a distinct criterion ID.
The 'stage' bytes are af.math-evidence/v1, with the fields in _STAGE_FIELDS.
Observations carry id, method_digest, unit, lower, upper, confidence_ppm and
certificate_ref. A negative-control receipt pins the gate, _BINDINGS, expected_rc,
observed_rc and witness_digest. Freeze its digest in the profile. The reader verifies 'certificate' and 'negative-control' bytes
against their underlying witnesses before admission. A certificate repeats every
observation field except certificate_ref, adds subject_digest, profile_digest,
gate_id, domain_digest and witness_digest. BASE cross-checks the copy. Bounds are exact rational pairs before scaling. A missing
bound is unavailable. A positive integer source_rc propagates a stage refusal.
Profile, stage, certificate and negative-control JSON use core.canonical_json
bytes including LF. Qualification records retain their original byte pins, and
the underlying reviewed witnesses can be opaque bytes.
The 'proof' and 'calibration' records repeat the scope fields in _QUAL_SCOPE,
the criterion fields in _QUAL_FIELDS, and witness_digest. The trusted reader
also supplies independently checked underlying bytes as 'derivation' (checked
proof, preconditions and domain) or 'held-out-calibration' (split, method,
threshold search, all outcomes and held-out confirmation). Profiles pin the
reviewed qualification records without a circular reference to their own hash.

verify_record(raw, *, approved_scopes, read_checked) validates canonical bytes
of either factory record and recomputes every stored attempt from the stage
receipt. approved_scopes maps (gate_id, subject_digest) to allowed profile pins
selected for THIS record by its caller. Every attempt must identify a record
subject and use an allowed profile. Each key requires a verified PASS. A refusal
without a reconstructible gate cannot authenticate a stored gate. Obtain
the mapping independently of the record. A successful result carries the
record digest and no asset verdict.
Success means the record passed IU00 verification. Admission of a whole asset,
rights, required gates across stages and retention of attempts belong to INT.
"""
import hashlib
import json
import re
from collections.abc import Mapping
from fractions import Fraction
from functools import lru_cache
from importlib import metadata
from importlib.util import find_spec
from pathlib import Path

from . import core


_PINS = {
    "af.acceptance-profile/v1": ("af.acceptance-profile.v1.schema.json",
                                 "f4b8775d1ac335f45d35cef47e245288f0da24ecedf93c40261221096219eebc"),
    "af.asset-record/v1": ("af.asset-record.v1.schema.json",
                           "f03ebabbfc85308f5b69e186dd9ec826d563fc563c90eba89287a51f91e72162"),
    "af.part-record/v1": ("af.part-record.v1.schema.json",
                          "6728fd2a67ca2caef9ee8126f4424563fcfda8f81afdf20aca22203941826421"),
}
_VERSIONS = {
    "jsonschema": "4.26.0", "referencing": "0.37.0", "jsonschema-specifications": "2025.9.1",
    "rpds-py": "0.30.0", "attrs": "23.2.0",
}
_BINDINGS = ("policy_digest", "domain_digest", "evaluator_digest", "toolchain_digest")
_STAGE_MATCH = (*_BINDINGS, "gate_id", "input_refs", "reference_refs")
_QUAL_SCOPE = (*_STAGE_MATCH, "operation", "role")
_CONTROL_FIELDS = {*_BINDINGS, "gate_id", "expected_rc", "observed_rc", "witness_digest"}
_STAGE_FIELDS = {
    *_STAGE_MATCH, "schema", "subject_digest", "profile_digest", "source_rc",
    "negative_control_receipt_refs", "wall_ns", "peak_bytes", "work_counts", "observations",
}
_QUAL_FIELDS = ("id", "unit", "scale", "operator", "limit", "basis", "method_digest",
                "uncertainty_kind", "min_confidence_ppm")
_OBS_FIELDS = {"id", "unit", "method_digest", "lower", "upper", "confidence_ppm", "certificate_ref"}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


class _Refusal(Exception):
    def __init__(self, rc, reason):
        super().__init__(reason)
        self.rc = rc
        self.reason = reason


class _ReaderError(_Refusal):
    """A reader contract failure interrupts the evaluation of the whole gate."""


def canonical_digest(obj):
    """Hash a factory JSON value. Invalid or unencodable values raise core.ContractError."""
    return "sha256:" + core.sha256_json(obj)


def _rational(pair):
    if not isinstance(pair, list) or len(pair) != 2 or any(type(x) is not int for x in pair):
        raise ValueError("invalid_rational")
    numerator, denominator = pair
    if denominator <= 0:
        raise ValueError("nonpositive_denominator")
    return Fraction(numerator, denominator)


def _scaled_bounds(lower, upper, scale):
    if type(scale) is not int or scale <= 0:
        raise ValueError("invalid_scale")
    low, high = _rational(lower) * scale, _rational(upper) * scale
    if low > high:
        raise ValueError("reversed_interval")
    return low, high


def _enclose(low, high):
    return low.__floor__(), high.__ceil__()


def outward_bounds(lower, upper, scale=1):
    """Enclose scaled rational endpoints. Invalid bounds or scale raise ValueError."""
    return _enclose(*_scaled_bounds(lower, upper, scale))


def _result(rc, reasons, **fields):
    return dict(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc], reasons=reasons, **fields)


def _valid_pin(pin):
    return type(pin) is str and _DIGEST.fullmatch(pin) is not None


def _parse(raw, canonical_role=None):
    if type(raw) is not bytes:
        raise _Refusal(2, "expected_json_bytes")
    obj = json.loads(raw.decode("utf-8"), object_pairs_hook=core._unique_object,
                     parse_float=core._reject_number, parse_constant=core._reject_number)
    if type(obj) is not dict:
        raise _Refusal(2, "expected_object")
    canonical = core.canonical_json(obj)
    if canonical_role is not None and raw != canonical.encode("utf-8"):
        raise _Refusal(2, "noncanonical_" + canonical_role)
    return obj


def _checked_bytes(read_checked, role, pin):
    if not _valid_pin(pin):
        raise _Refusal(2, "invalid_digest:" + role)
    try:
        data = read_checked(role, pin)
    except Exception as exc:
        raise _ReaderError(3, "reader_error:" + role) from exc
    if data is None:
        raise _Refusal(3, "missing:" + role)
    if type(data) is not bytes:
        raise _ReaderError(2, "expected_bytes:" + role)
    if "sha256:" + hashlib.sha256(data).hexdigest() != pin:
        raise _Refusal(4, "digest_mismatch:" + role)
    return data


@lru_cache(maxsize=1)
def _runtime_versions():
    try:
        return {name: metadata.version(name) for name in _VERSIONS}
    except metadata.PackageNotFoundError:
        return {}


@lru_cache(maxsize=len(_PINS))
def _validator(family):
    dist = metadata.distribution("jsonschema")
    init_file = Path("jsonschema", "__init__.py")
    if init_file not in (dist.files or ()):
        raise _Refusal(3, "unqualified_validator_origin")
    origin = dist.locate_file(init_file).resolve()
    try:
        spec = find_spec("jsonschema")
    except (ImportError, ValueError) as exc:
        raise _Refusal(3, "unqualified_validator_origin") from exc
    if spec is None:
        raise _Refusal(3, "unavailable_validator")
    if not spec.origin or Path(spec.origin).resolve() != origin:
        raise _Refusal(3, "unqualified_validator_origin")
    import jsonschema
    if not getattr(jsonschema, "__file__", None) or Path(jsonschema.__file__).resolve() != origin:
        raise _Refusal(3, "unqualified_validator_origin")
    from jsonschema import Draft202012Validator
    from referencing import Registry

    filename, pin = _PINS[family]
    try:
        raw = (_SCHEMA_DIR / filename).read_bytes()
    except OSError as exc:
        raise _Refusal(3, "missing_validator_schema") from exc
    if hashlib.sha256(raw).hexdigest() != pin:
        raise _Refusal(4, "validator_schema_drift")
    schema = _parse(raw)
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        raise _Refusal(3, "invalid_validator_schema") from exc
    # The pinned documents have only fragment refs. Registry() has no retriever.
    offline = Registry()
    # Fail closed if the selected runtime silently ignores draft constraints.
    probe = Draft202012Validator({
        "$defs": {"positive": {"type": "integer", "minimum": 1}},
        "type": "object", "required": ["ids"], "additionalProperties": False,
        "properties": {"ids": {"type": "array", "uniqueItems": True, "minItems": 1,
                               "items": {"$ref": "#/$defs/positive"}}},
    }, registry=offline)
    if not probe.is_valid({"ids": [1]}):
        raise _Refusal(3, "validator_rejects_positive")
    for invalid in ({}, {"ids": []}, {"ids": [0]}, {"ids": [1, 1]}, {"ids": [1], "extra": 0}):
        if probe.is_valid(invalid):
            raise _Refusal(3, "validator_accepts_negative")
    validator = Draft202012Validator(schema, registry=offline)
    if validator.is_valid({"schema": family}):  # Every pinned family requires more fields.
        raise _Refusal(3, "validator_accepts_factory_negative")
    return validator


def _validate(family, obj):
    if _runtime_versions() != _VERSIONS:
        raise _Refusal(3, "unqualified_validator")
    try:
        validator = _validator(family)
    except ImportError as exc:
        raise _Refusal(3, "unavailable_validator") from exc
    error = next(validator.iter_errors(obj), None)
    if error is not None:
        raise _Refusal(2, "schema_mismatch:" + error.json_path)


def _index(rows):
    if type(rows) is not list:
        raise _Refusal(2, "expected_rows")
    result = {}
    for row in rows:
        if type(row) is not dict or type(row.get("id")) is not str:
            raise _Refusal(2, "invalid_check_id")
        if row["id"] in result:
            raise _Refusal(2, "duplicate_check:" + row["id"])
        result[row["id"]] = row
    return result


def _stage(profile, profile_digest, subject_digest, receipt, read_checked):
    if set(receipt) != _STAGE_FIELDS or receipt["schema"] != "af.math-evidence/v1":
        raise _Refusal(2, "invalid_evidence_envelope")
    expected = {k: profile[k] for k in _STAGE_MATCH}
    expected.update(profile_digest=profile_digest, subject_digest=subject_digest)
    if any(receipt[k] != v for k, v in expected.items()):
        raise _Refusal(4, "stage_identity_drift")
    rc = receipt["source_rc"]
    if type(rc) is not int or rc not in core.RC:
        raise _Refusal(2, "invalid_source_rc")
    if rc:
        raise _Refusal(rc, "source_refusal")
    if any(type(receipt[k]) is not int or receipt[k] < 0 for k in ("wall_ns", "peak_bytes")):
        raise _Refusal(2, "invalid_resource_measurement")
    counts = receipt["work_counts"]
    if type(counts) is not dict or any(type(v) is not int or v < 0 for v in counts.values()):
        raise _Refusal(2, "invalid_work_count")
    if not any(counts.values()):
        raise _Refusal(3, "empty_work")
    controls = receipt["negative_control_receipt_refs"]
    if type(controls) is not list or not all(_valid_pin(p) for p in controls):
        raise _Refusal(2, "invalid_control_refs")
    if not controls:
        raise _Refusal(3, "missing_negative_controls")
    if set(controls) != set(profile["negative_control_receipt_refs"]) or len(set(controls)) != len(controls):
        raise _Refusal(4, "negative_control_set_drift")
    for pin in controls:
        control = _parse(_checked_bytes(read_checked, "negative-control", pin), "negative-control")
        if control.keys() != _CONTROL_FIELDS:
            raise _Refusal(2, "invalid_negative_control")
        for key in (*_BINDINGS, "gate_id"):
            if control.get(key) != profile[key]:
                raise _Refusal(4, "negative_control_scope_drift")
        expected, observed = control.get("expected_rc"), control.get("observed_rc")
        if type(expected) is not int or type(observed) is not int or not _valid_pin(control.get("witness_digest")):
            raise _Refusal(2, "invalid_negative_control")
        if expected not in (2, 3, 4, 5, 6) or observed != expected:
            raise _Refusal(3, "negative_control_did_not_discriminate")
    return _index(receipt["observations"])


def _qualify(profile, rule, read_checked):
    if rule["basis"] == "TARGET":
        raise _Refusal(3, "uncalibrated_target")
    if rule["basis"] == "DERIVED":
        role, underlying = "proof", "derivation"
    else:
        role, underlying = "calibration", "held-out-calibration"
    proof = _parse(_checked_bytes(read_checked, role, rule[role + "_receipt_digest"]))
    binding = {k: profile[k] for k in _QUAL_SCOPE}
    binding.update({k: rule[k] for k in _QUAL_FIELDS})
    if set(proof) != binding.keys() | {"witness_digest"}:
        raise _Refusal(2, "invalid_qualification_binding")
    if any(proof[k] != v or type(proof[k]) is not type(v) for k, v in binding.items()):
        raise _Refusal(4, "qualification_scope_drift")
    if not _checked_bytes(read_checked, underlying, proof["witness_digest"]):
        raise _Refusal(3, "empty_qualification_witness")


def _compare(operator, limit, low, high):
    if operator == "le":
        if high <= limit:
            return 0
        return 5 if low > limit else 3
    if operator == "ge":
        if low >= limit:
            return 0
        return 5 if high < limit else 3
    if low == high == limit:
        return 0
    return 5 if high < limit or low > limit else 3


def _check_certificate(receipt, observation, read_checked):
    cert = _parse(_checked_bytes(read_checked, "certificate", observation["certificate_ref"]), "certificate")
    binding = {k: observation[k] for k in _OBS_FIELDS - {"certificate_ref"}}
    binding.update({k: receipt[k] for k in ("subject_digest", "profile_digest", "gate_id", "domain_digest")})
    if set(cert) != binding.keys() | {"witness_digest"} or not _valid_pin(cert["witness_digest"]):
        raise _Refusal(2, "invalid_certificate")
    # Canonical comparison also distinguishes JSON booleans from integers inside pairs.
    if core.canonical_json({k: cert[k] for k in binding}) != core.canonical_json(binding):
        raise _Refusal(4, "certificate_observation_mismatch")


def _metric(profile, rule, receipt, observation, read_checked):
    fields = ("basis", "limit", "operator", "proof_receipt_digest", "calibration_receipt_digest")
    criterion = {k: rule[k] for k in fields}
    criterion["policy_digest"] = profile["policy_digest"]
    metric = {k: rule[k] for k in ("id", "unit", "method_digest")}
    metric.update(criterion=criterion, value=None, evidence_class="AUSENTE", uncertainty=None,
                  uncertainty_kind="unavailable", absence_reason=None)
    try:
        if observation is None:
            raise _Refusal(3, "missing_check")
        if set(observation) != _OBS_FIELDS:
            raise _Refusal(2, "invalid_observation")
        if any(observation[k] != rule[k] for k in ("method_digest", "unit")):
            raise _Refusal(4, "observation_identity_drift")
        if not _valid_pin(observation["certificate_ref"]):
            raise _Refusal(3, "missing_certificate")
        _check_certificate(receipt, observation, read_checked)
        if observation["lower"] is None or observation["upper"] is None:
            raise _Refusal(3, "missing_bound")
        low, high = _scaled_bounds(observation["lower"], observation["upper"], rule["scale"])
        lower, upper = _enclose(low, high)
        kind = rule["uncertainty_kind"]
        confidence = observation["confidence_ppm"]
        if kind == "exact" and low != high:
            raise _Refusal(2, "inexact_point")
        if kind == "sampled":
            if confidence is None:
                raise _Refusal(3, "missing_confidence")
            if type(confidence) is not int or not 0 < confidence < 1000000:
                raise _Refusal(2, "invalid_confidence")
            if confidence < rule["min_confidence_ppm"]:
                raise _Refusal(3, "insufficient_confidence")
        elif confidence is not None:
            raise _Refusal(2, "unexpected_confidence")
        metric.update(value=lower if rule["operator"] == "ge" else upper,
                      evidence_class="MEASURED", uncertainty_kind=kind,
                      uncertainty=dict(lower=lower, upper=upper, confidence_ppm=confidence,
                                       method_digest=rule["method_digest"]))
        _qualify(profile, rule, read_checked)
        rc = _compare(rule["operator"], rule["limit"], low, high)
        reason = {0: None, 3: "interval_crosses_limit", 5: "witnessed_violation"}[rc]
    except _ReaderError:
        raise
    except _Refusal as exc:
        rc, reason = exc.rc, exc.reason
    except ValueError as exc:
        rc, reason = 2, str(exc)
    if metric["value"] is None:
        metric["absence_reason"] = reason
    return metric, rc, reason


def _aggregate(codes):
    aggregate = core.worst_rc(codes)
    if aggregate == 3:
        # Preserve specific causes in a stage result; core collapses them across stages.
        if 4 in codes:
            return 4
        if 6 in codes:
            return 6
    return aggregate


def evaluate_gate(*, profile_digest, subject_digest, receipt_digest, read_checked):
    """Evaluate every required ID in a single protected gate, in stable ID order."""
    try:
        if not callable(read_checked):
            raise _Refusal(2, "invalid_reader")
        if not _valid_pin(subject_digest):
            raise _Refusal(2, "invalid_subject_digest")
        profile = _parse(_checked_bytes(read_checked, "profile", profile_digest), "profile")
        _validate("af.acceptance-profile/v1", profile)
        rules = _index(profile["criteria"])
        receipt = _parse(_checked_bytes(read_checked, "stage", receipt_digest), "stage")
        observations = _stage(profile, profile_digest, subject_digest, receipt, read_checked)
        if observations.keys() - rules.keys():
            raise _Refusal(2, "undeclared_check")
        metrics, codes, reasons = [], [], []
        for name in sorted(rules):
            metric, rc, reason = _metric(profile, rules[name], receipt, observations.get(name), read_checked)
            metrics.append(metric)
            codes.append(rc)
            if reason:
                reasons.append(name + ":" + reason)
        rc = _aggregate(codes)
        gate = {k: profile[k] for k in ("gate_id", "evaluator_digest", "policy_digest")}
        gate.update(subject_digest=subject_digest, receipt_digest=receipt_digest,
                    receipt_schema=receipt["schema"], profile_digest=profile_digest,
                    required_check_ids=sorted(rules), rc=rc, verdict=core.VERDICT[rc],
                    metrics=metrics, absence_reasons=reasons)
        return _result(rc, reasons, gate=gate)
    except _Refusal as exc:
        return _result(exc.rc, [exc.reason], gate=None)
    except (ValueError, core.ContractError, RecursionError):
        return _result(2, ["malformed_json"], gate=None)


def _scope_snapshot(scopes):
    if not isinstance(scopes, Mapping):
        raise _Refusal(2, "invalid_approved_scopes")
    result = {}
    try:
        for key, pins in scopes.items():
            if (type(key) is not tuple or len(key) != 2 or type(key[0]) is not str
                    or not re.fullmatch(r"[a-z][a-z0-9_.-]*", key[0]) or not _valid_pin(key[1])
                    or not isinstance(pins, (set, frozenset))):
                raise ValueError
            snapshot = frozenset(pins)
            if not all(_valid_pin(pin) for pin in snapshot):
                raise ValueError
            result[key] = snapshot
    except Exception as exc:
        raise _Refusal(2, "invalid_approved_scopes") from exc
    return result


def verify_record(raw, *, approved_scopes, read_checked):
    """Return a digest only for a canonical record with corroborated gate entries."""
    try:
        if not callable(read_checked):
            raise _Refusal(2, "invalid_reader")
        approved_scopes = _scope_snapshot(approved_scopes)
        record = _parse(raw, "record")
        family = record.get("schema")
        if family not in ("af.asset-record/v1", "af.part-record/v1"):
            raise _Refusal(2, "unknown_record_schema")
        _validate(family, record)
        subjects = {record["geometry_digest"], *(f["digest"] for f in record["files"])}
        subjects.update(v for k, v in record["representations"].items() if k != "absence_reasons" and v)
        verified = set()
        attempts = [(gate["gate_id"], gate["receipt_digest"]) for gate in record["gates"]]
        if attempts != sorted(set(attempts)):
            raise _Refusal(2, "noncanonical_gate_attempts")
        for gate in record["gates"]:
            _index(gate["metrics"])
            scope = (gate["gate_id"], gate["subject_digest"])
            if gate["subject_digest"] not in subjects:
                raise _Refusal(4, "foreign_record_subject")
            if gate["profile_digest"] not in approved_scopes.get(scope, ()):
                raise _Refusal(4, "unapproved_record_scope")
            decision = evaluate_gate(profile_digest=gate["profile_digest"],
                                     subject_digest=gate["subject_digest"],
                                     receipt_digest=gate["receipt_digest"], read_checked=read_checked)
            if decision["gate"] is None or (gate["verdict"] == "PASS" and decision["rc"]):
                return _result(decision["rc"], decision["reasons"], record_digest=None)
            if decision["gate"] != gate:
                raise _Refusal(4, "record_gate_mismatch")
            if gate["verdict"] == "PASS":
                verified.add(scope)
        if approved_scopes.keys() - verified:
            raise _Refusal(3, "missing_required_gate")
        return dict(rc=0, status="OK", reasons=[], record_digest=canonical_digest(record))
    except _Refusal as exc:
        return _result(exc.rc, [exc.reason], record_digest=None)
    except (ValueError, core.ContractError, RecursionError):
        return _result(2, ["malformed_json"], record_digest=None)
