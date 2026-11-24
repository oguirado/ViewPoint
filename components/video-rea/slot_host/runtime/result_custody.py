# SPDX-License-Identifier: AGPL-3.0-only
"""Result-byte custody for REA slot host launches (U09, C10/C11/C21).

This module owns the host-side decision about which captured bytes may
count as an attributable provider result.  It validates the single
pinned exchange on stdout only, binds provider identity with exact
types (never string coercion), and refuses foreign or omitted result
origin.  The host never synthesizes a result from an OS exit status,
an output hash, stderr content, or a child-declared pid.

Fail-closed contract:
- Stdout must carry exactly one strict-UTF8 single JSON object with no
  duplicate keys and no non-finite constants.  Stderr never supplies
  a result; any stderr payload voids the exchange.
- A missing, foreign, or explicitly withheld producer witness never
  falls back to the spawned pid, an inherited stdout assertion, or a
  child-declared identity.  Only an omitted (trusted-default) witness
  may use the host-observed spawn identity, and even then a foreign
  child declaration downgrades custody.
- A valid exchange must pair status/rc per the pinned map and agree
  with the OS return code; any identity substitution, digest mismatch
  or status/rc mismatch yields no slot result.
- Capped overflow yields overflow=true with bounded encoded
  diagnostics and no provider result (never a synthesized
  BUDGET_EXCEEDED/6); malformed UTF-8 is replaced, never raised.
"""

from __future__ import annotations

import hashlib
import json
import re

_EXCHANGE_SCHEMA = "rea.slot_result/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PAIRS = {"OK": 0, "ERROR": 2, "INCOMPLETE": 3, "STALE": 4, "FAIL": 5, "BUDGET_EXCEEDED": 6}
_PAIRED_RC = {0, 2, 3, 4, 5, 6}

# Omission allows observation through a registered fixture's inherited pipe.
OMITTED_WITNESS = object()


class _DuplicateKeyError(ValueError):
    pass


def _object_pairs_no_dup(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise _DuplicateKeyError(f"duplicate JSON key {k!r}")
        obj[k] = v
    return obj


def _reject_constant(value):
    raise ValueError(f"non-finite JSON constant {value!r}")


def proc_starttime(pid: int) -> int | None:
    """Live /proc starttime for pid, or None when unavailable."""
    # Lazy import avoids a cycle: the lifecycle module owns liveness,
    # this module only needs starttime for declared-identity checks.
    from . import process_lifecycle

    return process_lifecycle.proc_starttime(pid)


def decode_output(data: bytes, limit: int) -> tuple[str, bool]:
    """Bound encoded diagnostics to *limit* bytes; malformed UTF-8 is replaced.

    Limit both raw input and the UTF-8 encoding of the returned text.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative int")
    encoded = data[:limit].decode("utf-8", errors="replace").encode("utf-8")
    return encoded[:limit].decode("utf-8", errors="ignore"), len(data) > limit or len(encoded) > limit


def strict_single_json_object(stdout_bytes: bytes):
    """Strict UTF-8 single JSON object on stdout, or None.

    Rejects duplicate keys, non-finite constants, non-UTF-8 bytes and
    anything other than exactly one JSON object (surrounding whitespace
    allowed).  Never reads stderr.
    """
    try:
        text = stdout_bytes.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped, object_pairs_hook=_object_pairs_no_dup, parse_constant=_reject_constant)
    except (ValueError, _DuplicateKeyError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def exchange_documents(stdout_bytes: bytes, stderr_bytes: bytes) -> list[dict]:
    """Single pinned exchange on stdout only; stderr never supplies a result."""
    _ = stderr_bytes
    obj = strict_single_json_object(stdout_bytes or b"")
    return [obj] if obj is not None else []


def exchange_doc_valid(value) -> bool:
    """A complete pinned result document: schema, paired status/rc, sha."""
    if not isinstance(value, dict):
        return False
    if value.get("schema") != _EXCHANGE_SCHEMA:
        return False
    status = value.get("status")
    rc = value.get("rc")
    if type(status) is not str or type(rc) is not int:
        return False
    if status not in _PAIRS or _PAIRS.get(status) != rc:
        return False
    sha = value.get("sha256")
    return type(sha) is str and _SHA256_RE.fullmatch(sha) is not None


def extract_exchange_meta(stdout_bytes: bytes, stderr_bytes: bytes):
    """The single pinned provider exchange on the pipe, or None.

    Exactly one JSON object is a single exchange; zero or several objects
    are not a valid exchange.  Callers validate identity bindings
    against the bound attempt; a mismatch downgrades custody.
    """
    docs = exchange_documents(stdout_bytes, stderr_bytes)
    if len(docs) == 1:
        return docs[0]
    return None


def extract_observed_pid(stdout_bytes: bytes, stderr_bytes: bytes, started_pid: int) -> tuple[int, int | None]:
    """Provider-declared observed PID from stdout only.

    Strict single JSON object on stdout.  A declared foreign identifier
    that is not a positive int preserves the mismatch instead of
    falling back.  Stderr is never consulted.
    """
    _ = stderr_bytes
    obj = strict_single_json_object(stdout_bytes or b"")
    if isinstance(obj, dict):
        for key in ("observed_process_id", "observed_pid", "producer_pid"):
            if key in obj:
                val = obj.get(key)
                if type(val) is int and val > 0:
                    return val, proc_starttime(val)
                return 0, None
        custody_obj = obj.get("custody")
        if isinstance(custody_obj, dict) and "observed_process_id" in custody_obj:
            val = custody_obj.get("observed_process_id")
            if type(val) is int and val > 0:
                return val, proc_starttime(val)
            return 0, None
    return started_pid, proc_starttime(started_pid)


def resolve_producer_origin(
    stdout_bytes: bytes,
    stderr_bytes: bytes,
    started_pid: int,
    bound_starttime: int | None,
    producer_witness=None,
    *,
    registered_fixture: bool = False,
) -> tuple[int, int | None, bool]:
    """Bind fixture capture to the launched process and exact stdout bytes.

    Production observation requires the owner-authenticated T03 mechanism.
    An explicit fixture witness supersedes inherited-pipe attribution and must
    agree with the recorded spawn identity and the host's stdout digest.
    """
    if registered_fixture is not True:
        if isinstance(producer_witness, dict):
            pid, starttime = producer_witness.get("process_id"), producer_witness.get("starttime")
            if type(pid) is int and type(starttime) is int:
                return pid, starttime, False
        return 0, None, False
    if producer_witness is None:
        return 0, None, False
    declared_pid, declared_starttime = extract_observed_pid(stdout_bytes or b"", stderr_bytes or b"", started_pid or 0)
    observed_pid, observed_starttime = declared_pid, declared_starttime
    if observed_pid == started_pid and observed_starttime is None:
        observed_starttime = bound_starttime
    if producer_witness is not OMITTED_WITNESS:
        if not isinstance(producer_witness, dict):
            return 0, None, False
        w_pid = producer_witness.get("process_id")
        w_st = producer_witness.get("starttime")
        w_sha = producer_witness.get("host_result_sha256")
        if type(w_pid) is int and type(w_st) is int:
            observed_pid, observed_starttime = w_pid, w_st
            witness_bound = (
                w_pid == started_pid
                and w_st == bound_starttime
                and type(w_sha) is str
                and w_sha == host_result_sha256(stdout_bytes)
            )
            if declared_pid != started_pid:
                witness_bound = False
            return observed_pid, observed_starttime, witness_bound
        return 0, None, False
    return observed_pid, observed_starttime, declared_pid == started_pid


def exchange_identity_mismatch(exchange_meta, *, request_id: str, provider_id: str,
                               provider_revision: str, execution_profile: str,
                               implementation_sha256: str, role: str, rc,
                               provider_sha256: str | None = None) -> bool:
    """True when the exchange identity disagrees with the bound attempt.

    Uses exact types (no str() coercion; null is invalid).  A present
    ``provider_digest`` fails closed (it can never be validated against
    a stored pin in this layer).  The declared status/rc pair must be
    well-formed and agree with the OS return code.
    """
    if not isinstance(exchange_meta, dict):
        return True
    for key, bound_val in (
        ("request_id", request_id),
        ("provider_id", provider_id),
        ("provider_revision", provider_revision),
        ("provider_sha256", provider_sha256),
        ("execution_profile", execution_profile),
        ("implementation_sha256", implementation_sha256),
        ("role", role),
        ("slot", role),
        ("record_schema", _EXCHANGE_SCHEMA),
    ):
        if key in exchange_meta:
            if exchange_meta[key] is None or type(exchange_meta[key]) is not type(bound_val) or exchange_meta[key] != bound_val:
                return True
    if "provider_digest" in exchange_meta:
        return True
    for key in ("status", "rc"):
        if key in exchange_meta and exchange_meta.get("schema") == _EXCHANGE_SCHEMA:
            declared_status = exchange_meta.get("status")
            declared_rc = exchange_meta.get("rc")
            if declared_status is not None or declared_rc is not None:
                if type(declared_rc) is not int or type(declared_status) is not str or declared_status not in _PAIRS or _PAIRS.get(declared_status) != declared_rc:
                    return True
                if declared_rc is not None and rc is not None and declared_rc != int(rc):
                    return True
                break
    return False


def build_slot_result(exchange_meta, rc, *, timed_out: bool, overflow: bool,
                      interrupted: bool, mismatch: bool):
    """Build attempt.slot_result only from a valid pinned exchange.

    Returns the pinned result dict or None.  Timed-out, overflowed,
    interrupted or mismatched captures yield None; a well-formed
    exchange whose declared rc disagrees with the OS rc yields None.
    The host never synthesizes a result from the exit status or a
    capture hash, and overflow never yields BUDGET_EXCEEDED/6.
    """
    if interrupted or timed_out or overflow or mismatch:
        return None
    if not exchange_doc_valid(exchange_meta):
        return None
    declared_rc = exchange_meta.get("rc")
    if type(rc) is not int or rc != declared_rc:
        return None
    return {
        "schema": _EXCHANGE_SCHEMA,
        "status": exchange_meta.get("status"),
        "rc": int(declared_rc),
        "sha256": exchange_meta.get("sha256"),
    }


def fixture_result_bytes(exchange: dict | None) -> bytes | None:
    """Offline fixture digest object: exact UTF-8 bytes of result_payload."""
    payload = exchange.get("result_payload") if isinstance(exchange, dict) else None
    if type(payload) is not str:
        return None
    try:
        return payload.encode("utf-8")
    except UnicodeEncodeError:
        return None


def output_sha256(stdout_bytes: bytes, stderr_bytes: bytes, cap: int) -> str:
    """Host digest of retained capture bytes, capped to *cap* bytes."""
    output_bytes = (stdout_bytes or b"") + (stderr_bytes or b"")
    return hashlib.sha256(output_bytes[:cap]).hexdigest()


def host_result_sha256(stdout_bytes: bytes) -> str:
    """Host-measured digest of the retained stdout bytes."""
    return hashlib.sha256(stdout_bytes or b"").hexdigest()


def producer_binding_verified(*, started_pid, observed_pid, bound_starttime,
                              observed_starttime, witness_bound: bool,
                              cleanup_observed: bool, observed_now,
                              implementation_sha256: str, overflow: bool,
                              timed_out: bool, interrupted: bool,
                              exchange_mismatch: bool, rc_int: int) -> bool:
    """True only when the pid/starttime match is exact and every gate passes.

    Missing starttimes never match and never certify custody.  An
    over-cap, timed-out, interrupted or mismatched capture never
    verifies, and the OS rc must be a paired provider rc.
    """
    pid_match = (
        type(started_pid) is int and started_pid > 1 and observed_pid == started_pid
        and type(bound_starttime) is int and type(observed_starttime) is int
        and bound_starttime == observed_starttime
    )
    return bool(
        pid_match
        and witness_bound
        and cleanup_observed
        and observed_now == implementation_sha256
        and not overflow
        and (rc_int in _PAIRED_RC if not interrupted else False)
        and not timed_out
        and not interrupted
        and not exchange_mismatch
    )
