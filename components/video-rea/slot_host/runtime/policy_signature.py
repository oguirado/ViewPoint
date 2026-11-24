# SPDX-License-Identifier: AGPL-3.0-only
"""Closed operator-signed live-policy envelope verification.

Verifies an ``{"payload_b64": ..., "signature_b64": ...}`` envelope
carrying the exact UTF-8 policy bytes and a raw Ed25519 signature.
The verifier uses only the Python 3.12 standard library plus the
installed ``/usr/bin/openssl`` binary through ``pkeyutl -verify
-rawin`` with a fixed argv, ``shell=False``, a controlled environment
and a deadline.  Every failure raises
:class:`PolicySignatureRejected` (fail closed); no public-host path,
algorithm, key or URL is ever selected from the envelope.

Verification order: bound the envelope, strict-decode canonical
base64, require the exact 64-byte Ed25519 signature length, read the
host-pinned public key (regular file, no symlink, owner is the
effective uid or root, not world-writable), extract its DER
SubjectPublicKeyInfo, require the Ed25519 DER prefix, derive
``key_id`` as lowercase SHA-256 of the DER bytes, verify the exact
payload bytes (including any final newline) with OpenSSL, then
strict-parse the payload JSON (duplicate keys, unknown fields,
non-finite values and invalid types all deny) and bind schema,
purpose, audience, key_id, generation and the UTC validity
interval.

There is no policy reserialization during signature verification:
the signed bytes are the decoded payload bytes, verbatim.
"""

import base64
import datetime
import errno
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

__all__ = (
    "OPENSSL",
    "POLICY_SCHEMA",
    "POLICY_PURPOSE",
    "MAX_PAYLOAD_BYTES",
    "MAX_ENVELOPE_BYTES",
    "MAX_PUBLIC_KEY_BYTES",
    "DEFAULT_TIMEOUT_S",
    "PolicySignatureRejected",
    "key_id_for_public_key",
    "encode_envelope",
    "verify_and_parse_envelope",
)

OPENSSL = "/usr/bin/openssl"
POLICY_SCHEMA = "rea.live_policy/v1"
POLICY_PURPOSE = "viewpoint.rea.external_execution"

ED25519_SIG_LEN = 64
ED25519_DER_LEN = 44
_ED25519_DER_PREFIX = bytes.fromhex("302a300506032b6570032100")

MAX_PAYLOAD_BYTES = 65536
MAX_ENVELOPE_BYTES = 131072
MAX_PUBLIC_KEY_BYTES = 8192
DEFAULT_TIMEOUT_S = 5.0
_MAX_TIMEOUT_S = 60.0
_MAX_LIST_ITEMS = 512
_MAX_SCOPE_RULES = 512
_MAX_RESTRICTIONS = 512

_CONTROLLED_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}

_SHA256_ZERO = "0" * 64
_SHA256_RE = re.compile(r"[0-9a-f]{64}$")
_POLICY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_OP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_GRANT_RE = re.compile(r"^grant\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_MAX_GENERATION = (1 << 53) - 1

_TOP_KEYS = frozenset((
    "$comment",
    "schema", "purpose", "policy_id", "issuer", "key_id", "audience",
    "generation", "issued_at", "not_before", "expires_at",
    "baseline", "scope", "restrictions", "revocation",
))
_TOP_REQUIRED = frozenset((
    "schema", "purpose", "policy_id", "issuer", "key_id", "audience",
    "generation", "issued_at", "not_before", "expires_at",
    "baseline", "scope", "restrictions", "revocation",
))
_BASELINE_KEYS = frozenset((
    "mode", "allowed_profiles", "allowed_transports", "required_grants",
    "perception_ceiling", "shared_evidence_ref",
    "shared_evidence_sha256", "conformance_kit_sha256",
    "registry_generation", "enrollment_ref",
))
_SCOPE_KEYS = frozenset((
    "slot_id", "namespace", "installation_ref", "manifest_sha256",
    "provider_revision", "implementation_sha256", "operation_ids",
))
_RESTRICTION_KEYS = frozenset((
    "kind", "target", "subject", "perception_ceiling", "timeout_s",
    "max_output_bytes", "grant_id", "env_name",
))
_REVOCATION_KEYS = frozenset((
    "slot_ids", "operation_ids", "installation_refs",
    "implementation_sha256s", "revoked_generations",
))
_ALLOWED_PROFILES = frozenset(("external_process",))
_ALLOWED_TRANSPORTS = frozenset(("cli_subcommand", "cli_argv"))
_PERCEPTION_LEVELS = frozenset(("none", "cdp_atspi", "vision"))
_RESTRICTION_KINDS = frozenset((
    "disable", "preview_only", "require_existing_approval",
    "limit_principal", "lower_perception_ceiling", "limit_timeout",
    "limit_output_bytes", "remove_grant", "remove_env",
    "shorten_qualification_validity",
))
_ENVELOPE_KEYS = frozenset(("payload_b64", "signature_b64"))


class PolicySignatureRejected(ValueError):
    """Raised when any envelope, key, signature or payload check fails."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

    def __str__(self):
        return f"{self.reason}: {self.detail}"


def _deny(reason: str, detail: str):
    raise PolicySignatureRejected(reason, detail)


def _is_int(value) -> bool:
    return type(value) is int


def _is_str(value) -> bool:
    return type(value) is str


def _object_pairs_no_dup(pairs):
    obj = {}
    for k, v in pairs:
        if not isinstance(k, str):
            _deny("json_invalid", "non-string JSON object key")
        if k in obj:
            _deny("duplicate_key", f"duplicate JSON key {k!r}")
        obj[k] = v
    return obj


def _reject_constant(value):
    _deny("json_invalid", f"non-finite JSON constant {value!r}")


def _check_finite(obj, path="$"):
    if isinstance(obj, float):
        if not math.isfinite(obj):
            _deny("json_invalid", f"non-finite JSON number at {path}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_finite(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_finite(v, f"{path}[{i}]")


def _strict_loads(raw: bytes, *, what: str):
    if not isinstance(raw, (bytes, bytearray)):
        _deny("json_invalid", f"{what} must be bytes")
    raw = bytes(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _deny("json_invalid", f"{what} is not UTF-8: {exc}")
    try:
        obj = json.loads(text, object_pairs_hook=_object_pairs_no_dup,
                         parse_constant=_reject_constant)
    except PolicySignatureRejected:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        _deny("json_invalid", f"{what} is not strict JSON: {exc}")
    _check_finite(obj)
    return obj


def _decode_canonical_base64(text, field: str) -> bytes:
    if not _is_str(text):
        _deny("envelope_invalid", f"{field} must be a base64 string")
    if not text or text != text.strip() or len(text) % 4 != 0:
        _deny("envelope_invalid", f"{field} is not canonical base64")
    if _B64_RE.fullmatch(text) is None:
        _deny("envelope_invalid", f"{field} is not canonical base64")
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        _deny("envelope_invalid", f"{field} is not canonical base64")
    if base64.b64encode(decoded).decode("ascii") != text:
        _deny("envelope_invalid", f"{field} is not canonical base64")
    return decoded


def _validate_timeout(timeout_s):
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        _deny("verify_timeout", "timeout must be a positive finite number")
    value = float(timeout_s)
    if not math.isfinite(value) or value <= 0 or value > _MAX_TIMEOUT_S:
        _deny("verify_timeout", "timeout must be in (0, 60]")
    return value


def _run_openssl(args, *, timeout_s: float) -> tuple[int, bytes, bytes]:
    """Run the fixed OpenSSL binary with fixed argv (no shell)."""
    argv = [OPENSSL] + list(args)
    try:
        proc = subprocess.run(
            argv, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout_s, env=dict(_CONTROLLED_ENV),
        )
    except subprocess.TimeoutExpired:
        _deny("verify_timeout", f"openssl deadline exceeded ({timeout_s}s)")
    except OSError as exc:
        _deny("verify_unavailable", f"openssl unavailable: {exc}")
    out = proc.stdout if isinstance(proc.stdout, bytes) else b""
    err = proc.stderr if isinstance(proc.stderr, bytes) else b""
    return proc.returncode, out, err


def _read_pinned_public_key(public_key_path) -> bytes:
    if not isinstance(public_key_path, (str, os.PathLike)):
        _deny("public_key_invalid", "public key path must be str or PathLike")
    path = os.fspath(public_key_path)
    if not _is_str(path) or not path or not os.path.isabs(path):
        _deny("public_key_invalid", "public key path must be absolute")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | cloexec)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            _deny("public_key_untrusted", "pinned public key is a symlink")
        _deny("public_key_invalid", f"cannot open pinned public key: {exc}")
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            _deny("public_key_untrusted", f"cannot stat pinned public key: {exc}")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            _deny("public_key_untrusted", "pinned public key must be a regular file")
        try:
            euid = os.geteuid()
        except (AttributeError, OSError):
            _deny("public_key_untrusted", "cannot determine effective uid")
        if st.st_uid not in (euid, 0):
            _deny("public_key_untrusted", "pinned public key owner is untrusted")
        if st.st_mode & 0o002:
            _deny("public_key_untrusted", "pinned public key is world-writable")
        if st.st_size <= 0 or st.st_size > MAX_PUBLIC_KEY_BYTES:
            _deny("public_key_invalid", "pinned public key size out of bounds")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PUBLIC_KEY_BYTES:
                _deny("public_key_invalid", "pinned public key exceeds byte cap")
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if not data or len(data) > MAX_PUBLIC_KEY_BYTES:
        _deny("public_key_invalid", "pinned public key size out of bounds")
    return data


def _write_snapshot(directory: str, name: str, data: bytes) -> str:
    path = str(Path(directory) / name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return path


def _der_key_id(pub_snap: str, *, timeout_s: float) -> str:
    rc, out, _err = _run_openssl(
        ["pkey", "-pubin", "-in", pub_snap, "-outform", "DER"],
        timeout_s=timeout_s)
    if rc != 0:
        _deny("public_key_invalid", "pinned public key is not valid SPKI")
    der = out if isinstance(out, bytes) else b""
    if len(der) != ED25519_DER_LEN or not der.startswith(_ED25519_DER_PREFIX):
        _deny("public_key_wrong_algorithm", "pinned public key is not Ed25519")
    return hashlib.sha256(der).hexdigest()


def _check_text(value, field: str, *, min_len: int = 1, max_len: int = 128):
    if not _is_str(value):
        _deny("policy_invalid", f"{field} must be a string")
    if len(value) < min_len or len(value) > max_len:
        _deny("policy_invalid", f"{field} length out of bounds")
    if value != value.strip() or any(ord(c) < 0x20 for c in value):
        _deny("policy_invalid", f"{field} has control/edge whitespace")


def _check_sha256(value, field: str):
    if not _is_str(value) or _SHA256_RE.fullmatch(value) is None or value == _SHA256_ZERO:
        _deny("policy_invalid", f"{field} must be lowercase SHA-256 hex")


def _parse_timestamp(value, field: str) -> datetime.datetime:
    if not _is_str(value) or _TIMESTAMP_RE.fullmatch(value) is None:
        _deny("policy_invalid", f"{field} must be UTC RFC3339 Zulu seconds")
    try:
        parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _deny("policy_invalid", f"{field} is not a valid calendar time")
    return parsed.replace(tzinfo=datetime.timezone.utc)


def _unique_text_list(value, field: str, pattern, *, max_len: int = 128):
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        _deny("policy_invalid", f"{field} must be a bounded list")
    for item in value:
        if not _is_str(item) or pattern.fullmatch(item) is None:
            _deny("policy_invalid", f"{field} has an invalid entry")
    if len(set(value)) != len(value):
        _deny("policy_invalid", f"{field} must have distinct entries")
    return value


def _validate_baseline(baseline):
    if not isinstance(baseline, dict):
        _deny("policy_invalid", "baseline must be an object")
    if not set(baseline).issubset(_BASELINE_KEYS) or not _BASELINE_KEYS.issubset(set(baseline)):
        _deny("policy_invalid", "baseline has missing/unknown fields")
    if baseline.get("mode") != "live_after_conformance":
        _deny("policy_invalid", "baseline.mode must be live_after_conformance")
    profiles = baseline.get("allowed_profiles")
    if not isinstance(profiles, list) or not profiles or not set(profiles).issubset(_ALLOWED_PROFILES):
        _deny("policy_invalid", "baseline.allowed_profiles mismatch")
    if len(set(profiles)) != len(profiles):
        _deny("policy_invalid", "baseline.allowed_profiles must be distinct")
    transports = baseline.get("allowed_transports")
    if not isinstance(transports, list) or not transports or not set(transports).issubset(_ALLOWED_TRANSPORTS):
        _deny("policy_invalid", "baseline.allowed_transports mismatch")
    if len(set(transports)) != len(transports):
        _deny("policy_invalid", "baseline.allowed_transports must be distinct")
    grants = baseline.get("required_grants")
    if not isinstance(grants, list) or not grants:
        _deny("policy_invalid", "baseline.required_grants must be nonempty")
    _unique_text_list(grants, "baseline.required_grants", _GRANT_RE)
    if baseline.get("perception_ceiling") not in _PERCEPTION_LEVELS:
        _deny("policy_invalid", "baseline.perception_ceiling mismatch")
    ref = baseline.get("shared_evidence_ref")
    if not _is_str(ref) or not re.fullmatch(r"evidence-store:EVIDENCE_RECEIPT_SCHEMA@v[0-9]+\.[0-9]+", ref):
        _deny("policy_invalid", "baseline.shared_evidence_ref must be versioned")
    _check_sha256(baseline.get("shared_evidence_sha256"), "baseline.shared_evidence_sha256")
    _check_sha256(baseline.get("conformance_kit_sha256"), "baseline.conformance_kit_sha256")
    reg = baseline.get("registry_generation")
    if not _is_int(reg) or reg < 1 or reg > _MAX_GENERATION:
        _deny("policy_invalid", "baseline.registry_generation out of bounds")
    _check_text(baseline.get("enrollment_ref"), "baseline.enrollment_ref", max_len=256)


def _validate_scope(scope):
    if not isinstance(scope, list) or len(scope) > _MAX_SCOPE_RULES:
        _deny("policy_invalid", "scope must be a bounded list")
    for i, rule in enumerate(scope):
        field = f"scope[{i}]"
        if not isinstance(rule, dict):
            _deny("policy_invalid", f"{field} must be an object")
        if not set(rule).issubset(_SCOPE_KEYS):
            _deny("policy_invalid", f"{field} has unknown fields")
        has_slot = "slot_id" in rule
        has_ns = "namespace" in rule
        if has_slot == has_ns:
            _deny("policy_invalid", f"{field} needs exactly one of slot_id/namespace")
        if has_slot and not (_is_str(rule["slot_id"]) and _SLOT_ID_RE.fullmatch(rule["slot_id"])):
            _deny("policy_invalid", f"{field}.slot_id invalid")
        if has_ns and not (_is_str(rule["namespace"]) and _SLOT_ID_RE.fullmatch(rule["namespace"])):
            _deny("policy_invalid", f"{field}.namespace invalid")
        if "installation_ref" in rule:
            _check_text(rule["installation_ref"], f"{field}.installation_ref", max_len=256)
        if "manifest_sha256" in rule:
            _check_sha256(rule["manifest_sha256"], f"{field}.manifest_sha256")
        if "implementation_sha256" in rule:
            _check_sha256(rule["implementation_sha256"], f"{field}.implementation_sha256")
        if "provider_revision" in rule:
            _check_text(rule["provider_revision"], f"{field}.provider_revision")
        if "operation_ids" in rule:
            ops = rule["operation_ids"]
            if not isinstance(ops, list) or not ops or len(ops) > 64:
                _deny("policy_invalid", f"{field}.operation_ids must be nonempty bounded")
            _unique_text_list(ops, f"{field}.operation_ids", _OP_ID_RE)


def _validate_restrictions(restrictions):
    if not isinstance(restrictions, list) or len(restrictions) > _MAX_RESTRICTIONS:
        _deny("policy_invalid", "restrictions must be a bounded list")
    for i, item in enumerate(restrictions):
        field = f"restrictions[{i}]"
        if not isinstance(item, dict):
            _deny("policy_invalid", f"{field} must be an object")
        if not set(item).issubset(_RESTRICTION_KEYS) or "kind" not in item or "target" not in item:
            _deny("policy_invalid", f"{field} needs kind/target, no unknown fields")
        kind = item.get("kind")
        if kind not in _RESTRICTION_KINDS:
            _deny("policy_invalid", f"{field}.kind mismatch")
        _check_text(item.get("target"), f"{field}.target")
        if "subject" in item:
            _check_text(item["subject"], f"{field}.subject")
        if "perception_ceiling" in item and item["perception_ceiling"] not in _PERCEPTION_LEVELS:
            _deny("policy_invalid", f"{field}.perception_ceiling mismatch")
        if "timeout_s" in item:
            val = item["timeout_s"]
            if not _is_int(val) or val < 1 or val > 3600:
                _deny("policy_invalid", f"{field}.timeout_s out of bounds")
        if "max_output_bytes" in item:
            val = item["max_output_bytes"]
            if not _is_int(val) or val < 1 or val > 16777216:
                _deny("policy_invalid", f"{field}.max_output_bytes out of bounds")
        if "grant_id" in item and not (_is_str(item["grant_id"]) and _GRANT_RE.fullmatch(item["grant_id"])):
            _deny("policy_invalid", f"{field}.grant_id invalid")
        if "env_name" in item and not (_is_str(item["env_name"]) and _ENV_NAME_RE.fullmatch(item["env_name"])):
            _deny("policy_invalid", f"{field}.env_name invalid")
        if kind == "lower_perception_ceiling" and "perception_ceiling" not in item:
            _deny("policy_invalid", f"{field} needs perception_ceiling")
        if kind == "limit_timeout" and "timeout_s" not in item:
            _deny("policy_invalid", f"{field} needs timeout_s")
        if kind == "limit_output_bytes" and "max_output_bytes" not in item:
            _deny("policy_invalid", f"{field} needs max_output_bytes")
        if kind == "remove_grant" and "grant_id" not in item:
            _deny("policy_invalid", f"{field} needs grant_id")
        if kind == "remove_env" and "env_name" not in item:
            _deny("policy_invalid", f"{field} needs env_name")
        if kind in ("disable", "preview_only", "require_existing_approval",
                    "limit_principal", "shorten_qualification_validity"):
            for forbidden in ("perception_ceiling", "timeout_s", "max_output_bytes",
                              "grant_id", "env_name"):
                if forbidden in item:
                    _deny("policy_invalid", f"{field}.{forbidden} not allowed for {kind}")


def _validate_revocation(revocation):
    if not isinstance(revocation, dict):
        _deny("policy_invalid", "revocation must be an object")
    if not set(revocation).issubset(_REVOCATION_KEYS) or not _REVOCATION_KEYS.issubset(set(revocation)):
        _deny("policy_invalid", "revocation has missing/unknown fields")
    _unique_text_list(revocation.get("slot_ids"), "revocation.slot_ids", _SLOT_ID_RE)
    _unique_text_list(revocation.get("operation_ids"), "revocation.operation_ids", _OP_ID_RE)
    refs = revocation.get("installation_refs")
    if not isinstance(refs, list) or len(refs) > _MAX_LIST_ITEMS or len(set(refs)) != len(refs):
        _deny("policy_invalid", "revocation.installation_refs must be distinct bounded")
    for ref in refs:
        _check_text(ref, "revocation.installation_refs[]", max_len=256)
    shas = revocation.get("implementation_sha256s")
    if not isinstance(shas, list) or len(shas) > _MAX_LIST_ITEMS or len(set(shas)) != len(shas):
        _deny("policy_invalid", "revocation.implementation_sha256s must be distinct bounded")
    for sha in shas:
        _check_sha256(sha, "revocation.implementation_sha256s[]")
    gens = revocation.get("revoked_generations")
    if not isinstance(gens, list) or len(gens) > _MAX_LIST_ITEMS or len(set(gens)) != len(gens):
        _deny("policy_invalid", "revocation.revoked_generations must be distinct bounded")
    for gen in gens:
        if not _is_int(gen) or gen < 1 or gen > _MAX_GENERATION:
            _deny("policy_invalid", "revocation.revoked_generations out of bounds")


def _validate_policy(policy) -> tuple:
    if not isinstance(policy, dict):
        _deny("policy_invalid", "policy payload must be a JSON object")
    if not set(policy).issubset(_TOP_KEYS) or not _TOP_REQUIRED.issubset(set(policy)):
        _deny("policy_invalid", "policy has missing/unknown top-level fields")
    if "$comment" in policy:
        _check_text(policy["$comment"], "$comment", max_len=1024)
    if policy.get("schema") != POLICY_SCHEMA:
        _deny("policy_invalid", "schema must be rea.live_policy/v1")
    if policy.get("purpose") != POLICY_PURPOSE:
        _deny("policy_invalid", "purpose mismatch")
    if not _is_str(policy.get("policy_id")) or _POLICY_ID_RE.fullmatch(policy["policy_id"]) is None:
        _deny("policy_invalid", "policy_id invalid")
    _check_text(policy.get("issuer"), "issuer")
    _check_sha256(policy.get("key_id"), "key_id")
    _check_text(policy.get("audience"), "audience")
    gen = policy.get("generation")
    if not _is_int(gen) or gen < 1 or gen > _MAX_GENERATION:
        _deny("policy_invalid", "generation out of bounds")
    issued = _parse_timestamp(policy.get("issued_at"), "issued_at")
    not_before = _parse_timestamp(policy.get("not_before"), "not_before")
    expires = _parse_timestamp(policy.get("expires_at"), "expires_at")
    if not not_before < expires:
        _deny("policy_invalid", "not_before must be before expires_at")
    if not issued <= expires:
        _deny("policy_invalid", "issued_at must not be after expires_at")
    _validate_baseline(policy.get("baseline"))
    _validate_scope(policy.get("scope"))
    _validate_restrictions(policy.get("restrictions"))
    _validate_revocation(policy.get("revocation"))
    return issued, not_before, expires


def key_id_for_public_key(public_key_path, *, timeout_s=DEFAULT_TIMEOUT_S) -> str:
    """Return lowercase SHA-256 of the DER SPKI for a pinned Ed25519 key.

    Reads ``public_key_path`` under the same ownership and size rules
    as envelope verification and refuses any non-Ed25519 key.
    """
    timeout = _validate_timeout(timeout_s)
    try:
        pub_bytes = _read_pinned_public_key(public_key_path)
        with tempfile.TemporaryDirectory(prefix="rea-policy-") as td:
            pub_snap = _write_snapshot(td, "pub.pem", pub_bytes)
            return _der_key_id(pub_snap, timeout_s=timeout)
    except PolicySignatureRejected:
        raise
    except Exception as exc:
        _deny("public_key_invalid", f"key_id derivation failed: {exc}")


def encode_envelope(payload_bytes, signature_bytes) -> bytes:
    """Encode exact payload/signature bytes as a canonical envelope.

    This helper only packages already-signed bytes (operator device);
    it never signs and it never selects a key, algorithm or path.
    """
    if not isinstance(payload_bytes, (bytes, bytearray)):
        _deny("envelope_invalid", "payload must be bytes")
    if not isinstance(signature_bytes, (bytes, bytearray)):
        _deny("envelope_invalid", "signature must be bytes")
    payload = bytes(payload_bytes)
    sig = bytes(signature_bytes)
    if not payload or len(payload) > MAX_PAYLOAD_BYTES:
        _deny("envelope_invalid", "payload size out of bounds")
    if len(sig) != ED25519_SIG_LEN:
        _deny("envelope_invalid", "signature must be 64 bytes")
    envelope = {
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "signature_b64": base64.b64encode(sig).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"


def _envelope_parts(envelope) -> tuple[bytes, bytes]:
    if isinstance(envelope, (bytes, bytearray)):
        raw = bytes(envelope)
        if len(raw) > MAX_ENVELOPE_BYTES:
            _deny("envelope_invalid", "envelope exceeds byte cap")
        if not raw:
            _deny("envelope_invalid", "envelope is empty")
        obj = _strict_loads(raw, what="envelope")
        if not isinstance(obj, dict):
            _deny("envelope_invalid", "envelope must be a JSON object")
    elif _is_str(envelope):
        raw = envelope.encode("utf-8")
        if len(raw) > MAX_ENVELOPE_BYTES:
            _deny("envelope_invalid", "envelope exceeds byte cap")
        if not envelope:
            _deny("envelope_invalid", "envelope is empty")
        obj = _strict_loads(raw, what="envelope")
        if not isinstance(obj, dict):
            _deny("envelope_invalid", "envelope must be a JSON object")
    elif isinstance(envelope, dict):
        obj = envelope
    else:
        _deny("envelope_invalid", "envelope must be bytes, str or dict")
    if set(obj.keys()) != _ENVELOPE_KEYS:
        _deny("envelope_invalid", "envelope needs exactly payload_b64/signature_b64")
    payload = _decode_canonical_base64(obj.get("payload_b64"), "payload_b64")
    sig = _decode_canonical_base64(obj.get("signature_b64"), "signature_b64")
    if len(sig) != ED25519_SIG_LEN:
        _deny("envelope_invalid", "signature must be 64 bytes")
    if not payload or len(payload) > MAX_PAYLOAD_BYTES:
        _deny("envelope_invalid", "payload size out of bounds")
    return payload, sig


def verify_and_parse_envelope(envelope, *, public_key_path, expected_audience,
                              expected_key_id=None, now=None,
                              timeout_s=DEFAULT_TIMEOUT_S) -> dict:
    """Verify a signed policy envelope and return its parsed payload.

    Raises :class:`PolicySignatureRejected` on any envelope, key,
    signature, structural or binding failure.  On success returns the
    verified payload dict (signature and semantics already checked).
    """
    try:
        timeout = _validate_timeout(timeout_s)
        if not _is_str(expected_audience) or not expected_audience \
                or len(expected_audience) > 128 or expected_audience != expected_audience.strip():
            _deny("audience_invalid", "expected_audience must be nonempty text")
        if expected_key_id is not None:
            if not _is_str(expected_key_id) or _SHA256_RE.fullmatch(expected_key_id) is None:
                _deny("key_id_invalid", "expected_key_id must be SHA-256 hex")
        if now is None:
            moment = datetime.datetime.now(datetime.timezone.utc)
        elif isinstance(now, datetime.datetime):
            if now.tzinfo is None:
                _deny("policy_invalid", "now must be timezone-aware")
            moment = now.astimezone(datetime.timezone.utc)
        else:
            _deny("policy_invalid", "now must be a timezone-aware datetime")
        payload_bytes, sig_bytes = _envelope_parts(envelope)
        pub_bytes = _read_pinned_public_key(public_key_path)
        with tempfile.TemporaryDirectory(prefix="rea-policy-") as td:
            pub_snap = _write_snapshot(td, "pub.pem", pub_bytes)
            payload_snap = _write_snapshot(td, "payload.bin", payload_bytes)
            sig_snap = _write_snapshot(td, "sig.bin", sig_bytes)
            derived_key_id = _der_key_id(pub_snap, timeout_s=timeout)
            if expected_key_id is not None and derived_key_id != expected_key_id:
                _deny("key_id_mismatch", "pinned key does not match expected key_id")
            rc, _out, _err = _run_openssl(
                ["pkeyutl", "-verify", "-pubin", "-inkey", pub_snap,
                 "-rawin", "-in", payload_snap, "-sigfile", sig_snap],
                timeout_s=timeout)
            if rc != 0:
                _deny("signature_invalid", "openssl pkeyutl verify failed")
        policy = _strict_loads(payload_bytes, what="policy payload")
        if not isinstance(policy, dict):
            _deny("policy_invalid", "policy payload must be a JSON object")
        _issued, not_before, expires = _validate_policy(policy)
        if policy.get("key_id") != derived_key_id:
            _deny("key_id_mismatch", "payload key_id does not match pinned key")
        if policy.get("audience") != expected_audience:
            _deny("audience_mismatch", "payload audience does not match host")
        if moment < not_before:
            _deny("policy_not_yet_valid", "policy is not yet valid")
        if not moment < expires:
            _deny("policy_expired", "policy has expired")
        return policy
    except PolicySignatureRejected:
        raise
    except Exception as exc:
        _deny("policy_invalid", f"verification failed closed: {exc}")
