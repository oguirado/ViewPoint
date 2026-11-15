"""w8_repro_carrier.py

Test-only Python consumer carrier module for harvest and aggregator identity contract testing.

Restated binding w8-repro-v1:
Domain literal: "w8-repro-v1" (11 UTF-8 bytes)
Length prefix: 8 CITED unsigned big-endian bytes encoding len(canonical_json_bytes)
Canonical integer-JSON bytes for identity components.

Identity contains exactly 12 semantic fields:
1. normalization_version
2. repository_identity
3. base_commit
4. base_image_toolchain_digest
5. warm_cache_manifest_digest
6. gate_argv
7. cwd
8. outcome_class
9. assertion_signature
10. selected_frames
11. affected_target
12. reproducer_input_identity

Provenance fields stay strictly OUTSIDE identity:
task_id, attempt_id, lease_id, sender, timestamps, cost, pulse, epoch, request_id, source_body.

Note: This carrier mapping is for test/contract verification only and is NOT an unmerged production API.
"""

import json
import struct
import hashlib
from typing import Any, Dict, List, Optional, Tuple, Union

DOMAIN_LITERAL = b"w8-repro-v1"

REQUIRED_IDENTITY_KEYS = {
    "normalization_version",
    "repository_identity",
    "base_commit",
    "base_image_toolchain_digest",
    "warm_cache_manifest_digest",
    "gate_argv",
    "cwd",
    "outcome_class",
    "assertion_signature",
    "selected_frames",
    "affected_target",
    "reproducer_input_identity",
}

class IdentityError(Exception):
    """Base exception for identity contract errors."""
    pass

class RejectError(IdentityError):
    def __init__(self, code: int, class_name: str, detail: str):
        super().__init__(f"{class_name}: {detail}")
        self.code = code
        self.class_name = class_name
        self.detail = detail

def _check_json_value(val: Any, depth: int = 0) -> None:
    if depth > 128:
        raise RejectError(5, "MALFORMED", "JSON nesting depth exceeded")
    if isinstance(val, float):
        raise RejectError(5, "NONCANONICAL_NUMBER", "floats not permitted in integer-JSON")
    if isinstance(val, dict):
        for k, v in val.items():
            if not isinstance(k, str):
                raise RejectError(5, "MALFORMED", "dict keys must be strings")
            _check_json_value(v, depth + 1)
    elif isinstance(val, list):
        for elem in val:
            _check_json_value(elem, depth + 1)
    elif isinstance(val, (bool, int, str, type(None))):
        pass
    else:
        raise RejectError(5, "MALFORMED", f"unsupported type {type(val)}")

def strict_json_parse(json_str: Union[str, bytes]) -> Any:
    """Parses JSON strictly forbidding duplicate keys and float numbers."""
    if isinstance(json_str, bytes):
        try:
            json_str = json_str.decode("utf-8")
        except UnicodeDecodeError:
            raise RejectError(5, "MALFORMED", "invalid UTF-8 bytes")

    def dict_raise_on_duplicates(ordered_pairs):
        d = {}
        for k, v in ordered_pairs:
            if k in d:
                raise RejectError(5, "DUPLICATE_KEY", f"duplicate key {k!r}")
            d[k] = v
        return d

    try:
        val = json.loads(
            json_str,
            parse_float=lambda s: (_ for _ in ()).throw(RejectError(5, "NONCANONICAL_NUMBER", "float values rejected")),
            parse_constant=lambda s: (_ for _ in ()).throw(RejectError(5, "NONCANONICAL_NUMBER", f"constant {s!r} rejected")),
            object_pairs_hook=dict_raise_on_duplicates
        )
    except json.JSONDecodeError as err:
        raise RejectError(5, "MALFORMED", f"JSON decode error: {err}")

    _check_json_value(val)
    return val

def canonical_encode(val: Any, depth: int = 0) -> bytes:
    """Canonical integer-JSON encoding.

    Sorted keys, compact, integer-only, strict control escapes for strings (<32 escape as \\u00xx or \\n, \\r, \\t, etc.).
    """
    if depth > 128:
        raise RejectError(5, "MALFORMED", "JSON nesting limit exceeded")

    if isinstance(val, float):
        raise RejectError(5, "NONCANONICAL_NUMBER", "float values rejected")
    if val is None:
        return b"null"
    if isinstance(val, bool):
        return b"true" if val else b"false"
    if isinstance(val, int):
        return str(val).encode("ascii")
    if isinstance(val, str):
        out = ['"']
        for char in val:
            codepoint = ord(char)
            if char in ('"', '\\'):
                out.append('\\' + char)
            elif codepoint < 32:
                short_escapes = {8: '\\b', 12: '\\f', 10: '\\n', 13: '\\r', 9: '\\t'}
                if codepoint in short_escapes:
                    out.append(short_escapes[codepoint])
                else:
                    out.append(f"\\u{codepoint:04x}")
            else:
                out.append(char)
        out.append('"')
        return "".join(out).encode("utf-8")
    if isinstance(val, dict):
        keys = sorted(val.keys())
        parts = []
        for k in keys:
            k_bytes = canonical_encode(k, depth + 1)
            v_bytes = canonical_encode(val[k], depth + 1)
            parts.append(k_bytes + b":" + v_bytes)
        return b"{" + b",".join(parts) + b"}"
    if isinstance(val, list):
        parts = [canonical_encode(elem, depth + 1) for elem in val]
        return b"[" + b",".join(parts) + b"]"

    raise RejectError(5, "MALFORMED", f"unsupported JSON value type {type(val)}")

def validate_identity_dict(identity_obj: Dict[str, Any]) -> None:
    if not isinstance(identity_obj, dict):
        raise RejectError(5, "MALFORMED", "identity object must be a dictionary")

    keys = set(identity_obj.keys())
    if keys != REQUIRED_IDENTITY_KEYS:
        missing = REQUIRED_IDENTITY_KEYS - keys
        extra = keys - REQUIRED_IDENTITY_KEYS
        if missing:
            raise RejectError(5, "MISSING_KEY", f"missing identity keys: {missing}")
        if extra:
            raise RejectError(5, "UNKNOWN_KEY", f"extra/unknown identity keys: {extra}")

    # Types verification for identity fields
    if not isinstance(identity_obj["normalization_version"], str):
        raise RejectError(5, "MALFORMED", "normalization_version must be str")
    if not isinstance(identity_obj["repository_identity"], str):
        raise RejectError(5, "MALFORMED", "repository_identity must be str")
    if not isinstance(identity_obj["base_commit"], str):
        raise RejectError(5, "MALFORMED", "base_commit must be str")
    if not isinstance(identity_obj["base_image_toolchain_digest"], str):
        raise RejectError(5, "MALFORMED", "base_image_toolchain_digest must be str")
    if not isinstance(identity_obj["warm_cache_manifest_digest"], str):
        raise RejectError(5, "MALFORMED", "warm_cache_manifest_digest must be str")
    if not isinstance(identity_obj["gate_argv"], list) or not all(isinstance(x, str) for x in identity_obj["gate_argv"]):
        raise RejectError(5, "MALFORMED", "gate_argv must be list of strings")
    if not isinstance(identity_obj["cwd"], str):
        raise RejectError(5, "MALFORMED", "cwd must be str")
    if not isinstance(identity_obj["outcome_class"], str):
        raise RejectError(5, "MALFORMED", "outcome_class must be str")
    if not isinstance(identity_obj["assertion_signature"], str):
        raise RejectError(5, "MALFORMED", "assertion_signature must be str")
    if not isinstance(identity_obj["selected_frames"], list) or not all(isinstance(x, str) for x in identity_obj["selected_frames"]):
        raise RejectError(5, "MALFORMED", "selected_frames must be list of strings")
    if not isinstance(identity_obj["affected_target"], dict) or set(identity_obj["affected_target"].keys()) != {"path", "symbol"}:
        raise RejectError(5, "MALFORMED", "affected_target must be dict with path and symbol")
    if not isinstance(identity_obj["affected_target"]["path"], str) or not isinstance(identity_obj["affected_target"]["symbol"], str):
        raise RejectError(5, "MALFORMED", "affected_target fields must be strings")
    if not isinstance(identity_obj["reproducer_input_identity"], str):
        raise RejectError(5, "MALFORMED", "reproducer_input_identity must be str")

def compute_w8_repro_v1_hash(identity_obj: Dict[str, Any]) -> str:
    """Computes SHA-256 hash for w8-repro-v1 framing."""
    validate_identity_dict(identity_obj)
    canon_bytes = canonical_encode(identity_obj)
    length_prefix = struct.pack(">Q", len(canon_bytes))
    framed_data = DOMAIN_LITERAL + length_prefix + canon_bytes
    return hashlib.sha256(framed_data).hexdigest()

class ProvenanceBinding:
    """Represents bound provenance for a harvest receipt."""
    def __init__(
        self,
        request_id: str,
        task_id: str,
        attempt_id: str,
        lease_id: str,
        epoch: int,
        source_body: str,
        sender: str,
        cost: Optional[int] = None,
        timestamp: Optional[int] = None,
    ):
        if not (request_id and task_id and attempt_id and lease_id and source_body and sender):
            raise RejectError(5, "STALE_ORIGIN", "all provenance fields required")
        self.request_id = request_id
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.lease_id = lease_id
        self.epoch = epoch
        self.source_body = source_body
        self.sender = sender
        self.cost = cost
        self.timestamp = timestamp

class ObservationStore:
    """Append-only set union observation store."""
    def __init__(self):
        # map repro_hash -> list of receipts/observations
        self.repro_index: Dict[str, List[Dict[str, Any]]] = {}
        self.unkeyed_pending: List[Dict[str, Any]] = []
        self.seen_exact_bytes: set = set()

    def add_observation(
        self,
        identity_obj: Optional[Dict[str, Any]],
        provenance: ProvenanceBinding,
        cost: Optional[int] = None,
        early_ack: bool = False,
    ) -> Tuple[Optional[str], bool]:
        if early_ack:
            raise RejectError(5, "EARLY_ACK", "early ack rejected before validation/commit")
        if cost is None:
            raise RejectError(5, "MISSING_COST", "cost must be explicitly provided, never default to 0")

        obs = {
            "provenance": {
                "request_id": provenance.request_id,
                "task_id": provenance.task_id,
                "attempt_id": provenance.attempt_id,
                "lease_id": provenance.lease_id,
                "epoch": provenance.epoch,
                "source_body": provenance.source_body,
                "sender": provenance.sender,
                "cost": cost,
            }
        }

        if identity_obj is None:
            self.unkeyed_pending.append(obs)
            return None, False

        repro_hash = compute_w8_repro_v1_hash(identity_obj)
        canon_bytes = canonical_encode(identity_obj)

        # Check exact bytes for deduplication / exact hit merge
        exact_key = (repro_hash, canon_bytes)
        merged = False
        if exact_key in self.seen_exact_bytes:
            merged = True
        else:
            self.seen_exact_bytes.add(exact_key)

        if repro_hash not in self.repro_index:
            self.repro_index[repro_hash] = []

        self.repro_index[repro_hash].append(obs)
        return repro_hash, merged
