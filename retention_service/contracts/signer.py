"""Byte-level assertions for the specified signer frames."""

from struct import Struct

from .validation import ContractError

LENGTH = Struct(">I")
FIELD_LENGTH = Struct(">H")
VERSION = 1
P256_SHA256 = 1
NONCE_BYTES = 32
DIGEST_BYTES = 32
MAX_HANDLE_BYTES = 256
MAX_SIGNATURE_BYTES = 72
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
FRAME_HEADER_BYTES = 2 + NONCE_BYTES + FIELD_LENGTH.size


def _payload(frame, minimum, maximum):
    if not LENGTH.size + minimum <= len(frame) <= LENGTH.size + maximum:
        raise ContractError("signer frame size outside bounds")
    (declared,) = LENGTH.unpack_from(frame)
    if declared != len(frame) - LENGTH.size:
        raise ContractError("signer frame length mismatch")
    payload = frame[LENGTH.size:]
    if payload[0] != VERSION:
        raise ContractError("unsupported signer frame version")
    (field_size,) = FIELD_LENGTH.unpack_from(payload, 2 + NONCE_BYTES)
    return payload[1], payload[2:2 + NONCE_BYTES], field_size, payload[FRAME_HEADER_BYTES:]


def decode_sign_request(frame):
    opcode, nonce, handle_size, fields = _payload(
        frame, FRAME_HEADER_BYTES + 1 + DIGEST_BYTES,
        FRAME_HEADER_BYTES + MAX_HANDLE_BYTES + DIGEST_BYTES,
    )
    if opcode != P256_SHA256 or not 1 <= handle_size <= MAX_HANDLE_BYTES:
        raise ContractError("unknown signer opcode or invalid handle length")
    if len(fields) != handle_size + DIGEST_BYTES:
        raise ContractError("signer request field length mismatch")
    try:
        handle = fields[:handle_size].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ContractError("non-ASCII signer handle") from exc
    return nonce, handle, fields[handle_size:]


def _signature_integer(signature, cursor):
    if len(signature) - cursor < 2 or signature[cursor] != 2:
        raise ContractError("expected DER integer")
    size = signature[cursor + 1]
    start = cursor + 2
    end = start + size
    if not 1 <= size <= 33 or end > len(signature):
        raise ContractError("invalid DER integer length")
    value = signature[start:end]
    if value[0] >= 128 or (size > 1 and value[0] == 0 and value[1] < 128):
        raise ContractError("invalid DER integer encoding")
    if not 0 < int.from_bytes(value, "big") < P256_ORDER:
        raise ContractError("DER integer outside P-256 bounds")
    return end


def _check_der_signature(signature):
    if not 8 <= len(signature) <= MAX_SIGNATURE_BYTES or signature[:2] != bytes((48, len(signature) - 2)):
        raise ContractError("invalid DER signature sequence")
    cursor = _signature_integer(signature, 2)
    if _signature_integer(signature, cursor) != len(signature):
        raise ContractError("unexpected DER signature suffix")


def decode_sign_reply(frame, expected_nonce):
    status, nonce, signature_size, signature = _payload(
        frame, FRAME_HEADER_BYTES, FRAME_HEADER_BYTES + MAX_SIGNATURE_BYTES,
    )
    if nonce != expected_nonce or signature_size != len(signature):
        raise ContractError("signer reply nonce or length mismatch")
    if status == 1 and not signature:
        return None
    if status != 0:
        raise ContractError("invalid signer reply status or payload")
    _check_der_signature(signature)
    return signature
