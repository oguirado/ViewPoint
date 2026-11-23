"""Canonical bytes and typed refusals for calibration evidence."""
import hashlib
import json

from asset_factory import core


class CalibrationError(ValueError):
    def __init__(self, rc, reason):
        super().__init__(reason)
        self.rc = rc


def require(condition, rc, reason):
    if not condition:
        raise CalibrationError(rc, reason)


def encode(obj):
    return core.canonical_json(obj).encode("utf-8")


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def parse(raw):
    require(type(raw) is bytes, 2, "expected_json_bytes")
    try:
        obj = json.loads(raw, object_pairs_hook=core._unique_object,
                         parse_float=core._reject_number, parse_constant=core._reject_number)
        canonical = encode(obj)
    except (core.ContractError, ValueError, RecursionError) as exc:
        raise CalibrationError(2, "invalid_json") from exc
    require(type(obj) is dict and canonical == raw, 2, "expected_canonical_object")
    return obj


def fields(obj, *names):
    require(type(obj) is dict and obj.keys() == set(names), 2, "unexpected_fields")
