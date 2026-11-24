# SPDX-License-Identifier: AGPL-3.0-only
"""Shared JSON framing for local exposure transports."""

import hashlib
import json
import math


MAX_LINE_BYTES = 262144  # Same inbound limit as vp-direct/expose/mcp.


def _unique(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate JSON key")
        values[key] = value
    return values


def _finite(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def loads(raw):
    if isinstance(raw, bytes):
        if len(raw) > MAX_LINE_BYTES:
            raise ValueError("input exceeds byte cap")
        raw = raw.decode("utf-8")
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_LINE_BYTES:
            raise ValueError("input exceeds byte cap")
    value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_finite, parse_float=_finite)
    if "\\u" in raw:
        # Reject unpaired surrogate escapes before echoing an ID on the UTF-8 wire.
        encode(value)
    return value


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()

