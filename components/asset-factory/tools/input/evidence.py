"""Canonical native evidence and the INPUT-to-BASE acceptance boundary."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from asset_factory import core, math_acceptance
from asset_factory.domains import pair

canonical_digest = math_acceptance.canonical_digest
MAX_RECORD_BYTES = 1024 * 1024


class InputIssue(ValueError):
    def __init__(self, rc, code):
        super().__init__(code)
        self.rc, self.code = rc, code


def require(condition, rc, code):
    if not condition:
        raise InputIssue(rc, code)


def bytes_digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def is_digest(pin):
    return (type(pin) is str and len(pin) == 71 and pin.startswith("sha256:")
            and all(char in "0123456789abcdef" for char in pin[7:]))


def canonical_bytes(value):
    return core.canonical_json(value).encode("utf-8")


def checked_record(reader, role, pin):
    require(is_digest(pin), 2, "invalid_" + role + "_pin")
    raw = reader(role, pin)
    require(raw is not None, 3, "missing_" + role)
    require(type(raw) is bytes, 2, "invalid_" + role + "_bytes")
    require(len(raw) <= MAX_RECORD_BYTES, 6, role + "_byte_budget")
    require(bytes_digest(raw) == pin, 4, "stale_" + role)
    data = json.loads(raw)
    require(type(data) is dict and canonical_bytes(data) == raw, 2, "noncanonical_" + role)
    return data


def closed(value, schema, *keys):
    require(type(value) is dict and value.keys() == {"schema", *keys}
            and value["schema"] == schema, 2, "invalid_" + schema)


def invented_details(value):
    require(type(value) is list and all(type(item) is str and item.strip() for item in value),
            2, "invalid_invented_details")


def point(value):
    return [pair(value), pair(value)]


def native_receipt(gate_id, subject_digest, domain_digest):
    return {"schema": "af.input-evidence/v1", "gate_id": gate_id, "subject_digest": subject_digest,
            "domain_digest": domain_digest, "source_rc": 0, "reason_codes": [], "metrics": {}}


def refuse(receipt, error):
    if isinstance(error, InputIssue):
        rc, reason = error.rc, error.code
    elif isinstance(error, MemoryError):
        rc, reason = 6, "memory_budget_exceeded"
    else:
        rc, reason = 2, str(error) or "malformed_input"
    receipt.update(source_rc=rc, reason_codes=[reason])
    return receipt


def _verdict(rc, reasons, *, diagnostic=False):
    return {"rc": rc, "status": core.STATUS[rc], "verdict": core.VERDICT[rc], "reasons": reasons,
            "gate": None, "disposition": "DIAGNOSTIC" if diagnostic else core.VERDICT[rc]}


METRIC_RULES = {
    "input.views": {
        "coverage": ("ppm", 1_000_000, "ge", "DERIVED"),
        "landmark_error": ("millipx", 1, "le", "CALIBRATED"),
        "lighting_flatness": ("ppm", 1_000_000, "le", "CALIBRATED"),
    },
    "input.matte": {
        "alpha_iou": ("ppm", 1_000_000, "ge", "CALIBRATED"),
        "boundary_iou": ("ppm", 1_000_000, "ge", "CALIBRATED"),
        "alpha_outside_pixels": ("count", 1, "eq", "DERIVED"),
        "margin": ("ppm", 1_000_000, "ge", "CALIBRATED"),
        "foreground": ("ppm", 1_000_000, "ge", "CALIBRATED"),
        "border_pixels": ("count", 1, "eq", "DERIVED"),
    },
    "input.raw": {
        "component_count": ("count", 1, "eq", "DERIVED"),
        "degenerate_faces": ("count", 1, "le", "CALIBRATED"),
        "forbidden_support_intersections": ("count", 1, "eq", "DERIVED"),
        **{"extent_" + axis: ("um", 1, "le", "CALIBRATED") for axis in "xyz"},
    },
}


def qualify_input(native, *, profile_digest=None, receipt_digest=None, read_checked=None):
    """Bind independently reviewed BASE observations to a locally recomputed receipt.

    The caller recomputes native evidence from the pinned bytes with its producer.
    BASE's reader remains external and authorizes every role independently.
    """
    try:
        rc = native["source_rc"]
        if rc:
            return _verdict(rc, native["reason_codes"])
        if native.get("phase") == "calibration":
            return _verdict(3, ["calibration_capture_only"], diagnostic=True)
        if profile_digest is None and receipt_digest is None:
            return _verdict(3, ["base_qualification_missing"], diagnostic=True)
        require(read_checked is not None, 3, "checked_reader_missing")
        profile = checked_record(read_checked, "profile", profile_digest)
        stage = checked_record(read_checked, "stage", receipt_digest)
        for key in ("gate_id", "domain_digest"):
            require(profile.get(key) == native[key], 4, "input_profile_scope_drift")
        for key in ("input_refs", "reference_refs"):
            require(profile.get(key) == native[key], 4, "input_profile_reference_drift")
        require(stage.get("source_rc") == 0 and stage.get("subject_digest") == native["subject_digest"],
                4, "input_stage_subject_or_result_drift")
        measurements = native["metrics"]
        rules = METRIC_RULES[native["gate_id"]]
        require(measurements.keys() == rules.keys(), 3, "input_metric_coverage_missing")
        criteria = profile.get("criteria")
        observations = stage.get("observations")
        require(type(criteria) is list and type(observations) is list, 2, "invalid_base_observations")
        for rows in (criteria, observations):
            require(all(type(row) is dict and type(row.get("id")) is str for row in rows), 2,
                    "invalid_base_observations")
            require(len(rows) == len(rules) and {row["id"] for row in rows} == rules.keys(),
                    3, "input_metric_coverage_missing")
        for rule in criteria:
            unit, scale, operator, basis = rules[rule["id"]]
            require((rule.get("unit"), rule.get("scale"), rule.get("operator"))
                    == (unit, scale, operator), 4, "input_criterion_semantics_drift")
            require(rule.get("basis") in (basis, "TARGET"), 4, "input_criterion_basis_drift")
        for observed in observations:
            bounds = [observed.get("lower"), observed.get("upper")]
            require(canonical_bytes(bounds) == canonical_bytes(measurements[observed["id"]])
                    and observed.get("confidence_ppm") is None, 4, "input_observation_drift")
        result = math_acceptance.evaluate_gate(profile_digest=profile_digest,
                                               subject_digest=native["subject_digest"],
                                               receipt_digest=receipt_digest, read_checked=read_checked)
        result["disposition"] = "DIAGNOSTIC" if result["rc"] == 3 else result["verdict"]
        return result
    except InputIssue as exc:
        return _verdict(exc.rc, [exc.code], diagnostic=exc.rc == 3)
    except (ValueError, TypeError, KeyError, core.ContractError, RecursionError):
        return _verdict(2, ["invalid_input_evidence"])


def verify_input_receipt(raw, recomputed):
    """Recomputed must come from the producer and independently pinned witnesses."""
    require(type(raw) is bytes and raw == canonical_bytes(recomputed), 4, "input_receipt_drift")
    return bytes_digest(raw)


def publish_input_receipt(target: Path, receipt):
    """Commit a complete receipt atomically at a new path, with no clobber."""
    data = canonical_bytes(receipt)
    with NamedTemporaryFile(dir=target.parent) as staged:
        staged.write(data)
        staged.flush()
        os.fsync(staged.fileno())
        os.link(staged.name, target)
    fd = os.open(target.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return bytes_digest(data)
