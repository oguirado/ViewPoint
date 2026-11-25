"""Exact byte encodings, limits and shapes shared by the retention callers."""

from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from hashlib import sha256
import json
import re

from .validation import ContractError, strict_json

MAX_RECORD_BYTES = 33_554_432
MAX_ARTIFACT_BYTES = 65_536
# The bounded envelope includes space for its fixed keys and JSON formatting.
MAX_FINISH_BYTES = 4 * ((MAX_RECORD_BYTES + 2) // 3 + (MAX_ARTIFACT_BYTES + 2) // 3) + 1024
MAX_OFFSET = 9_007_199_254_740_991
MAX_PAGE_BYTES = 1_048_576
TENANT_HEADER = "Retention-Tenant"
DIGEST_HEADER = "Retention-SHA256"
PAGE_HEADERS = (
    "Retention-Object-Ref", "Retention-Object-Bytes", "Retention-Object-SHA256",
    "Retention-Page-Offset", "Retention-Page-Bytes", "Retention-Page-SHA256",
)
DENIAL_BODY = b'{"error":"unavailable"}'
DENIAL_RESPONSE = (
    b"HTTP/1.1 404 Not Found\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 23\r\n"
    b"Cache-Control: no-store\r\n\r\n"
    + DENIAL_BODY
)


def admission_bytes(admission, validator):
    """Canonicalize the flat string-only Admission in the RFC 8785 domain."""
    # Validate without the supplied digest, which is outside the hash domain.
    if not isinstance(admission, dict):
        raise ContractError("admission must be a JSON object")
    fields = dict(admission)
    fields.pop("admission_sha256", None)
    validator.validate("Admission", {**fields, "admission_sha256": "0" * 64})
    # Admission keys are fixed ASCII strings. Python's key order therefore
    # matches UTF-16 ordering and the schema permits no numeric fields.
    try:
        return json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ContractError("non-Unicode admission string") from exc


def verify_admission(admission, validator):
    validator.validate("Admission", admission)
    if sha256(admission_bytes(admission, validator)).hexdigest() != admission["admission_sha256"]:
        raise ContractError("admission digest mismatch")


def record_bytes(record):
    # Frozen tools/maintenance_sample.py json_bytes convention.
    return (json.dumps(record, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def decode_record(raw, name, validator):
    if name not in {"Start", "Terminal"}:
        raise ContractError("unknown retention record form")
    if len(raw) > MAX_RECORD_BYTES:
        raise ContractError("record exceeds byte limit")
    record = strict_json(raw)
    if record_bytes(record) != raw:
        raise ContractError("record bytes are not canonical")
    validator.validate(name, record)
    return record


def decode_start(raw, *, content_length, digest, validator):
    validator.validate("Hash", digest)
    if type(content_length) is not int or content_length != len(raw) or content_length > MAX_RECORD_BYTES:
        raise ContractError("start byte count mismatch")
    if sha256(raw).hexdigest() != digest:
        raise ContractError("start digest mismatch")
    return decode_record(raw, "Start", validator)


def decode_base64(encoded, *, maximum, allow_empty):
    if len(encoded) > 4 * ((maximum + 2) // 3) or len(encoded) % 4:
        raise ContractError("invalid base64 length")
    if not encoded:
        if allow_empty:
            return b""
        raise ContractError("empty base64")
    padding = 2 if encoded.endswith("==") else int(encoded.endswith("="))
    if len(encoded) // 4 * 3 - padding > maximum:
        raise ContractError("decoded bytes exceed limit")
    try:
        raw = b64decode(encoded, validate=True)
    except (Base64Error, ValueError) as exc:
        raise ContractError("invalid base64") from exc
    if b64encode(raw).decode("ascii") != encoded:
        raise ContractError("base64 bytes are not canonical")
    return raw


def decode_finish(raw, validator):
    if len(raw) > MAX_FINISH_BYTES:
        raise ContractError("finish envelope exceeds byte limit")
    envelope = strict_json(raw)
    validator.validate("Finish", envelope)
    terminal = decode_base64(envelope["terminal_b64"], maximum=MAX_RECORD_BYTES, allow_empty=False)
    artifact = decode_base64(envelope["artifact_b64"], maximum=MAX_ARTIFACT_BYTES, allow_empty=True)
    record = decode_record(terminal, "Terminal", validator)
    if record["artifact"]["bytes"] != len(artifact) or record["artifact"]["sha256"] != sha256(artifact).hexdigest():
        raise ContractError("artifact does not match terminal byte count and digest")
    return terminal, artifact


def tenant_header(values, validator):
    if len(values) != 1:
        raise ContractError("expected one Retention-Tenant header")
    validator.validate("Id", values[0])
    return values[0]


def _page_value(values, minimum, maximum, default):
    if not values:
        return default
    if len(values) != 1 or re.fullmatch(r"[0-9]+", values[0]) is None:
        raise ContractError("expected one unsigned decimal paging parameter")
    digits = values[0].lstrip("0") or "0"
    if len(digits) > len(str(maximum)):
        raise ContractError("paging parameter outside bounds")
    value = int(digits)
    if not minimum <= value <= maximum:
        raise ContractError("paging parameter outside bounds")
    return value


def page_parameters(offset=(), limit=()):
    return (
        _page_value(offset, 0, MAX_OFFSET, 0),
        _page_value(limit, 1, MAX_PAGE_BYTES, MAX_PAGE_BYTES),
    )
