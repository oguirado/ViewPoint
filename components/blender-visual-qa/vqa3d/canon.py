"""Canonical JSON and explicit finite, half-even quantisation.

Booleans are JSON booleans in metadata, never accepted as integer state or
integer parameters. Runtime floating-point values cross the boundary only via
quantisation; canonical serialization itself never silently rounds them.
"""
import hashlib
import json
import math
from decimal import Decimal, ROUND_HALF_EVEN


def validate(value, *, allow_bool=True):
    if value is None or type(value) in (str, int):
        return
    if type(value) is bool and allow_bool:
        return
    if type(value) is list:
        for item in value:
            validate(item, allow_bool=allow_bool)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("canonical_object_key_must_be_string")
            validate(item, allow_bool=allow_bool)
        return
    raise ValueError("noncanonical_type:" + type(value).__name__)


def canonical_json(value):
    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_canonical(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def integer(value):
    if type(value) is not int:
        raise ValueError("integer_required")
    return value


def state_copy(value):
    if type(value) is not dict:
        raise ValueError("state_must_be_object")
    validate(value, allow_bool=False)
    return json.loads(canonical_json(value))


def quantise(value, scale):
    if type(value) not in (int, float, Decimal) or type(scale) is not int or scale <= 0:
        raise ValueError("invalid_quantisation_input")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite_quantisation_input")
    return int((number * scale).to_integral_value(rounding=ROUND_HALF_EVEN))


def metres_to_micrometres(value):
    return quantise(value, 1000000)


def radians_to_millidegrees(value):
    if type(value) not in (int, float, Decimal):
        raise ValueError("invalid_angle")
    return quantise(Decimal(str(value)) * Decimal(180) / Decimal(str(math.pi)), 1000)
