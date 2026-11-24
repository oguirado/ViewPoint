# SPDX-License-Identifier: AGPL-3.0-only
"""Monotonic activation for operator-signed live policies.

Production authority is fail-closed.  This module owns the durable
activation record that enforces:

* generation monotonicity (higher only, exact-repeat idempotent,
  same-generation conflict and rollback deny),
* UTC validity interval (not_before <= now < expires_at),
* monotonic clock high-water mark (clock rollback beyond the stored
  maximum denies),
* corrupt / missing previously-initialized state denies,
* atomic publish (generation, payload digest and revocation durable).

Signature, schema, audience, key-id and structural checks are delegated
to :mod:`slot_host.runtime.policy_signature`.  Every failure raises
:class:`PolicyActivationRejected` (or re-raises
:class:`PolicySignatureRejected` from that verifier) and never
publishes a partial state.

Activation state lives in a single JSON file (schema
``rea.live_activation/v1``) at a host-supplied path.  The host
provisions audience, key_id and minimum_generation via
:func:`initialize_activation_state` before first activation; the
first activation then requires that file to exist.  A genesis state
has generation 0 and null payload fields.  Once initialized, a missing
or corrupt file denies every later activation until an operator
recovery checkpoint is restored (writable-disk rollback cannot be
proven fresh without that independent anchor).
"""

import base64
import datetime
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from slot_host.runtime.policy_signature import (
    MAX_PAYLOAD_BYTES,
    PolicySignatureRejected,
    verify_and_parse_envelope,
)

__all__ = (
    "ACTIVATION_SCHEMA",
    "MAX_STATE_BYTES",
    "PolicyActivationRejected",
    "initialize_activation_state",
    "load_activation_state",
    "activate",
    "assert_active",
    "is_active",
)


ACTIVATION_SCHEMA = "rea.live_activation/v1"
MAX_STATE_BYTES = 32768
_MAX_GENERATION = (1 << 53) - 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_ZERO = "0" * 64
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_POLICY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_STATE_KEYS = frozenset((
    "schema", "audience", "key_id", "minimum_generation", "generation",
    "payload_sha256", "policy_id", "not_before", "expires_at",
    "activated_at", "max_clock", "revoked_generations",
))
_ENVELOPE_KEYS = frozenset(("payload_b64", "signature_b64"))


class PolicyActivationRejected(ValueError):
    """Raised when monotonic / state / clock checks fail (fail closed)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

    def __str__(self):
        return f"{self.reason}: {self.detail}"


def _deny(reason: str, detail: str):
    raise PolicyActivationRejected(reason, detail)


def _is_int(v) -> bool:
    return type(v) is int


def _is_str(v) -> bool:
    return type(v) is str


def _object_pairs_no_dup(pairs):
    obj = {}
    for k, v in pairs:
        if not isinstance(k, str):
            _deny("state_corrupt", "non-string JSON object key in activation state")
        if k in obj:
            _deny("state_corrupt", f"duplicate JSON key {k!r} in activation state")
        obj[k] = v
    return obj


def _reject_constant(v):
    _deny("state_corrupt", f"non-finite JSON constant {v!r} in activation state")


def _check_finite(obj, path="$"):
    if isinstance(obj, float):
        if not math.isfinite(obj):
            _deny("state_corrupt", f"non-finite JSON number at {path} in activation state")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_finite(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_finite(v, f"{path}[{i}]")


def _strict_load_state(raw: bytes):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _deny("state_corrupt", f"activation state is not UTF-8: {exc}")
    try:
        obj = json.loads(text, object_pairs_hook=_object_pairs_no_dup,
                         parse_constant=_reject_constant)
    except PolicyActivationRejected:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        _deny("state_corrupt", f"activation state is not strict JSON: {exc}")
    _check_finite(obj)
    return obj


def _parse_zulu(value, field: str) -> datetime.datetime:
    if not _is_str(value) or _TIMESTAMP_RE.fullmatch(value) is None:
        _deny("state_corrupt", f"activation state {field} must be UTC Zulu seconds")
    try:
        parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _deny("state_corrupt", f"activation state {field} is not a valid calendar time")
    return parsed.replace(tzinfo=datetime.timezone.utc)


def _format_z(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:
        _deny("state_corrupt", "clock must be timezone-aware")
    utc = dt.astimezone(datetime.timezone.utc).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_audience(audience) -> str:
    if not _is_str(audience) or not audience or len(audience) > 128 or audience != audience.strip():
        _deny("audience_invalid", "audience must be nonempty trimmed text <=128")
    if any(ord(c) < 0x20 for c in audience):
        _deny("audience_invalid", "audience has control characters")
    return audience


def _validate_key_id(key_id, field="key_id") -> str:
    if not _is_str(key_id) or _SHA256_RE.fullmatch(key_id) is None or key_id == _SHA256_ZERO:
        _deny("key_id_invalid", f"{field} must be lowercase SHA-256 hex")
    return key_id


def _validate_state_path(state_path) -> Path:
    if isinstance(state_path, (str, os.PathLike)):
        p = Path(str(state_path))
    else:
        _deny("state_path_invalid", "state_path must be str or PathLike")
    if not str(p) or not p.is_absolute():
        _deny("state_path_invalid", "state_path must be absolute")
    if p.name in ("", ".", "..") or ".." in p.parts:
        _deny("state_path_invalid", "activation state path is invalid")
    return p


@contextmanager
def _state_directory(path: Path):
    """Open each ancestor without following symlinks and pin the final parent."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for i, component in enumerate(path.parent.parts[1:], start=1):
            try:
                child = os.open(component, flags, dir_fd=fd)
            except OSError as exc:
                _deny("state_path_invalid", f"cannot open trusted activation directory: {exc}")
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            ancestor = i < len(path.parent.parts) - 1
            sticky_root_ancestor = ancestor and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not sticky_root_ancestor):
                _deny("state_path_invalid", "activation directory ownership or permissions are untrusted")
        yield fd
    finally:
        os.close(fd)


def _validate_state_dict(obj):
    if not isinstance(obj, dict):
        _deny("state_corrupt", "activation state must be a JSON object")
    if set(obj.keys()) != _STATE_KEYS:
        # No unknown or missing fields (closed schema)
        missing = _STATE_KEYS - set(obj.keys())
        extra = set(obj.keys()) - _STATE_KEYS
        if missing or extra:
            _deny("state_corrupt", f"activation state fields mismatch (missing={sorted(missing)!r} extra={sorted(extra)!r})")
    if obj.get("schema") != ACTIVATION_SCHEMA:
        _deny("state_corrupt", "activation state schema mismatch")
    audience = _validate_audience(obj.get("audience"))
    # key_id may be validated only if not None; genesis may have key_id
    kid = obj.get("key_id")
    if kid is not None:
        _validate_key_id(kid)
    else:
        _deny("state_corrupt", "activation state key_id must be a string")
    mg = obj.get("minimum_generation")
    if not _is_int(mg) or mg < 1 or mg > _MAX_GENERATION:
        _deny("state_corrupt", "activation state minimum_generation out of bounds")
    gen = obj.get("generation")
    if not _is_int(gen) or gen < 0 or gen > _MAX_GENERATION:
        _deny("state_corrupt", "activation state generation out of bounds")
    # payload / policy fields
    ph = obj.get("payload_sha256")
    pid = obj.get("policy_id")
    nb = obj.get("not_before")
    exp = obj.get("expires_at")
    act = obj.get("activated_at")
    mx = obj.get("max_clock")
    rev = obj.get("revoked_generations")
    if not isinstance(rev, list):
        _deny("state_corrupt", "activation state revoked_generations must be bounded list")
    for g in rev:
        if not _is_int(g) or g < 1 or g > _MAX_GENERATION:
            _deny("state_corrupt", "activation state revoked_generations out of bounds")
    if len(set(rev)) != len(rev):
        _deny("state_corrupt", "activation state revoked_generations must be distinct")
    if gen == 0:
        if not (ph is None and pid is None and nb is None and exp is None and act is None):
            _deny("state_corrupt", "genesis activation state must have null payload fields")
        if rev != []:
            _deny("state_corrupt", "genesis activation state revoked_generations must be []")
        if mx is not None:
            _parse_zulu(mx, "max_clock")
    else:
        if not _is_str(ph) or _SHA256_RE.fullmatch(ph) is None or ph == _SHA256_ZERO:
            _deny("state_corrupt", "activation state payload_sha256 invalid")
        if not _is_str(pid) or _POLICY_ID_RE.fullmatch(pid) is None:
            _deny("state_corrupt", "activation state policy_id invalid")
        nb_dt = _parse_zulu(nb, "not_before")
        exp_dt = _parse_zulu(exp, "expires_at")
        if not nb_dt < exp_dt:
            _deny("state_corrupt", "activation state not_before must be before expires_at")
        _parse_zulu(act, "activated_at")
        if mx is not None:
            _parse_zulu(mx, "max_clock")
    return obj


def _read_state_bytes(state_path: Path, *, directory_fd: int) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(state_path.name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        _deny("state_missing", "activation state file is missing")
    except OSError as exc:
        _deny("state_corrupt", f"cannot open activation state: {exc}")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _deny("state_corrupt", "activation state must be a regular file")
        if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            _deny("state_corrupt", "activation state ownership or permissions are untrusted")
        if info.st_size > MAX_STATE_BYTES:
            _deny("state_corrupt", "activation state exceeds byte cap")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(MAX_STATE_BYTES + 1)
    except OSError as exc:
        _deny("state_corrupt", f"cannot read activation state: {exc}")
    finally:
        os.close(fd)
    if len(data) > MAX_STATE_BYTES:
        _deny("state_corrupt", "activation state exceeds byte cap")
    return data


def load_activation_state(state_path) -> dict:
    """Read and validate one immutable activation state snapshot."""
    path = _validate_state_path(state_path)
    with _state_directory(path) as directory_fd:
        raw = _read_state_bytes(path, directory_fd=directory_fd)
    return _validate_state_dict(_strict_load_state(raw))


def _write_state_atomic(state_path: Path, state: dict, *, directory_fd: int) -> None:
    data = json.dumps(state, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(data) > MAX_STATE_BYTES:
        _deny("state_corrupt", "new activation state exceeds byte cap")
    name = ".rea-activation-" + secrets.token_hex(16)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, state_path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _validate_now(now):
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    if not isinstance(now, datetime.datetime):
        _deny("policy_invalid", "now must be a timezone-aware datetime")
    if now.tzinfo is None:
        _deny("policy_invalid", "now must be timezone-aware")
    try:
        return now.astimezone(datetime.timezone.utc).replace(microsecond=0)
    except Exception:
        _deny("policy_invalid", "now is invalid")


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


def _extract_payload_bytes(envelope) -> bytes:
    # Mirrors policy_signature envelope decoding but fail-closed via PolicyActivationRejected
    # if envelope is malformed after verification (defense in depth).
    # We accept bytes / str / dict like verify_and_parse_envelope.
    if isinstance(envelope, (bytes, bytearray)):
        raw = bytes(envelope)
        if not raw or len(raw) > 131072:
            _deny("envelope_invalid", "envelope exceeds byte cap or empty")
        try:
            obj = _strict_load_state(raw)  # strict JSON parse for envelope object
        except PolicyActivationRejected as e:
            # Re-use envelope reasoning; state_corrupt is not accurate for envelope.
            # Convert to envelope_invalid for caller clarity (but signature path already verified).
            _deny("envelope_invalid", f"envelope JSON error: {e.detail}")
    elif _is_str(envelope):
        raw = envelope.encode("utf-8")
        if not envelope or len(raw) > 131072:
            _deny("envelope_invalid", "envelope exceeds byte cap or empty")
        try:
            obj = _strict_load_state(raw)
        except PolicyActivationRejected as e:
            _deny("envelope_invalid", f"envelope JSON error: {e.detail}")
    elif isinstance(envelope, dict):
        obj = envelope
    else:
        _deny("envelope_invalid", "envelope must be bytes, str or dict")
    if not isinstance(obj, dict) or set(obj.keys()) != _ENVELOPE_KEYS:
        _deny("envelope_invalid", "envelope needs exactly payload_b64/signature_b64")
    payload = _decode_canonical_base64(obj.get("payload_b64"), "payload_b64")
    # signature bytes checked by verifier, but we need payload bounds here
    if not payload or len(payload) > MAX_PAYLOAD_BYTES:
        _deny("envelope_invalid", "payload size out of bounds")
    return payload


def initialize_activation_state(state_path, *, audience, key_id, minimum_generation=1, now=None) -> dict:
    """Provision a genesis activation state.

    Creates a generation-0 record at ``state_path`` with the provisioned
    audience/key_id and minimum_generation, and the monotonic clock
    seeded to ``now`` if given.  Raises ``state_exists`` if a state file
    already exists (caller must not silently overwrite a committed
    generation).
    """
    p = _validate_state_path(state_path)
    audience = _validate_audience(audience)
    key_id = _validate_key_id(key_id)
    if not _is_int(minimum_generation) or minimum_generation < 1 or minimum_generation > _MAX_GENERATION:
        _deny("minimum_generation", "minimum_generation out of bounds")
    moment = _validate_now(now) if now is not None else datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    state = {
        "schema": ACTIVATION_SCHEMA,
        "audience": audience,
        "key_id": key_id,
        "minimum_generation": minimum_generation,
        "generation": 0,
        "payload_sha256": None,
        "policy_id": None,
        "not_before": None,
        "expires_at": None,
        "activated_at": None,
        "max_clock": _format_z(moment),
        "revoked_generations": [],
    }
    # Validate before write (defense in depth)
    _validate_state_dict(state)
    with activation_lock(p) as directory_fd:
        try:
            os.stat(p.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            _write_state_atomic(p, state, directory_fd=directory_fd)
        else:
            _deny("state_exists", "activation state already exists; refusing to reinitialize")
    return state


def _payload_digest(payload_bytes: bytes) -> str:
    return hashlib.sha256(payload_bytes).hexdigest()


@contextmanager
def activation_lock(state_path):
    """Serialize policy publication and final admission on a host-owned lock."""
    path = _validate_state_path(state_path)
    with _state_directory(path) as directory_fd:
        flags = os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open(f".{path.name}.lock", flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            _deny("state_corrupt", f"cannot open activation lock: {exc}")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                _deny("state_corrupt", "activation lock ownership, permissions or file type are untrusted")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield directory_fd
        finally:
            os.close(fd)


@dataclass(frozen=True, kw_only=True)
class SignedPolicy:
    """Host-supplied signed envelope and the independent trust and state locations."""

    envelope: bytes
    public_key_path: Path
    expected_audience: str
    state_path: Path
    expected_key_id: str | None = None

    def read(self, moment):
        if type(self.envelope) is not bytes:
            _deny("envelope_invalid", "expected an immutable signed envelope")
        return assert_active(
            self.envelope, public_key_path=self.public_key_path,
            expected_audience=self.expected_audience, expected_key_id=self.expected_key_id,
            state_path=self.state_path, now=moment, return_policy=True,
        )


def activate(envelope, *, public_key_path, expected_audience, expected_key_id=None, now=None, state_path, timeout_s=5.0) -> dict:
    """Verify and publish an activation under the same lock used for launch."""
    with activation_lock(state_path) as directory_fd:
        return _activate(
            envelope, public_key_path=public_key_path, expected_audience=expected_audience,
            expected_key_id=expected_key_id, now=now, state_path=state_path, timeout_s=timeout_s,
            directory_fd=directory_fd,
        )


def _activate(envelope, *, public_key_path, expected_audience, expected_key_id=None, now=None, state_path, timeout_s=5.0, directory_fd: int) -> dict:
    """Verify a signed policy envelope and atomically commit it if monotonic.

    Verification (signature, schema, purpose, key, audience, structural
    and interval checks) is delegated to
    :func:`verify_and_parse_envelope`.  Monotonic generation and clock
    checks are then applied against the committed state:

    * re-activation with the same generation and same payload digest
      idempotently succeeds (generation unchanged, clock may advance),
    * same generation with a different payload denies
      (``generation_conflict``),
    * any lower generation denies (``generation_rollback``),
    * a ``now`` earlier than the stored maximum denies
      (``clock_rollback``),
    * a missing or corrupt state file denies (``state_missing`` /
      ``state_corrupt``).

    On success the new generation, payload digest, policy_id, validity
    interval and revocation list are durably published atomically.
    """
    p = _validate_state_path(state_path)
    moment = _validate_now(now) if now is not None else datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    # Delegate signature + structural + interval checks to the U03 verifier.
    # PolicySignatureRejected propagates (fail closed); timeout stays bounded.
    try:
        policy = verify_and_parse_envelope(
            envelope,
            public_key_path=public_key_path,
            expected_audience=expected_audience,
            expected_key_id=expected_key_id,
            now=moment,
            timeout_s=timeout_s,
        )
    except PolicySignatureRejected:
        raise
    except Exception as exc:  # defensive: never let unexpected escape as success
        _deny("envelope_invalid", f"signature verification failed: {exc}")

    # Payload digest binds the exact signed bytes (incl. final newline).
    payload_bytes = _extract_payload_bytes(envelope)
    payload_hash = _payload_digest(payload_bytes)

    # Load committed state (must exist; genesis created via initialize).
    raw = _read_state_bytes(p, directory_fd=directory_fd)
    obj = _strict_load_state(raw)
    state = _validate_state_dict(obj)

    # Provisioned audience / key must still match (defense against
    # swapped state vs expected config).
    expected_audience = _validate_audience(expected_audience)
    if state["audience"] != expected_audience:
        _deny("audience_mismatch", "activation state audience does not match expected")
    if state["key_id"] != policy["key_id"]:
        _deny("key_id_mismatch", "activation state key_id does not match signed policy")
    if expected_key_id is not None:
        _validate_key_id(expected_key_id, "expected_key_id")
        if state["key_id"] != expected_key_id:
            _deny("key_id_mismatch", "activation state key_id does not match expected")
    # Policy itself already binds key/audience via verifier, but also
    # ensure policy generation respects the provisioned minimum.
    mg = state["minimum_generation"]
    pg = policy["generation"]
    if not _is_int(pg) or pg < 1 or pg > _MAX_GENERATION:
        _deny("policy_invalid", "policy generation out of bounds")
    if pg < mg:
        _deny("minimum_generation", "policy generation below provisioned minimum")

    # Monotonic clock: reject now earlier than the stored high water.
    stored_max = state.get("max_clock")
    if stored_max is not None:
        max_dt = _parse_zulu(stored_max, "max_clock")
        if moment < max_dt:
            _deny("clock_rollback", f"now {moment!r} is earlier than stored max_clock {stored_max!r}")
        # If moment == max_dt, allowed; new max stays same or advances
        new_max = moment if moment > max_dt else max_dt
    else:
        new_max = moment

    committed_gen = state["generation"]
    committed_hash = state["payload_sha256"]

    if pg < committed_gen:
        _deny("generation_rollback", f"policy generation {pg} is below committed {committed_gen}")
    revoked = set(state["revoked_generations"]) | set(policy["revocation"]["revoked_generations"])
    if pg in revoked:
        _deny("generation_revoked", f"policy generation {pg} is revoked")
    if pg == committed_gen:
        if payload_hash != committed_hash:
            _deny("generation_conflict", f"same generation {pg} with different payload digest")
        if not set(policy["revocation"]["revoked_generations"]) <= set(state["revoked_generations"]):
            _deny("active_mismatch", "signed revocations are missing from committed state")
        if _format_z(new_max) != state["max_clock"]:
            state["max_clock"] = _format_z(new_max)
            _write_state_atomic(p, state, directory_fd=directory_fd)
        return state

    new_state = {
        "schema": ACTIVATION_SCHEMA,
        "audience": state["audience"],
        "key_id": state["key_id"],
        "minimum_generation": state["minimum_generation"],
        "generation": pg,
        "payload_sha256": payload_hash,
        "policy_id": policy["policy_id"],
        "not_before": policy["not_before"],
        "expires_at": policy["expires_at"],
        "activated_at": _format_z(moment),
        "max_clock": _format_z(new_max),
        "revoked_generations": sorted(revoked),
    }
    # Validate then publish atomically.
    _validate_state_dict(new_state)
    _write_state_atomic(p, new_state, directory_fd=directory_fd)
    return new_state


def assert_active(envelope, *, public_key_path, expected_audience, expected_key_id=None, now=None, state_path, timeout_s=5.0, return_policy=False) -> dict:
    """Assert that ``envelope`` is the currently committed policy.

    Re-verifies the signature and then checks that the envelope's
    generation and payload digest exactly match the committed state
    and that ``now`` is still inside its validity interval.  Any
    disagreement denies (recovery check).
    """
    p = _validate_state_path(state_path)
    moment = _validate_now(now) if now is not None else datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    try:
        policy = verify_and_parse_envelope(
            envelope,
            public_key_path=public_key_path,
            expected_audience=expected_audience,
            expected_key_id=expected_key_id,
            now=moment,
            timeout_s=timeout_s,
        )
    except PolicySignatureRejected:
        raise
    payload_bytes = _extract_payload_bytes(envelope)
    payload_hash = _payload_digest(payload_bytes)
    state = load_activation_state(p)
    if state["generation"] == 0:
        _deny("not_active", "no policy has been committed")
    if policy["generation"] != state["generation"]:
        _deny("active_mismatch", f"active policy generation {policy['generation']} != committed {state['generation']}")
    if payload_hash != state["payload_sha256"]:
        _deny("active_mismatch", "active policy payload digest does not match committed")
    for name in ("policy_id", "audience", "key_id", "not_before", "expires_at"):
        if policy[name] != state[name]:
            _deny("active_mismatch", f"active {name} does not match committed")
    revoked = policy["revocation"]["revoked_generations"]
    if policy["generation"] in state["revoked_generations"] or policy["generation"] in revoked:
        _deny("generation_revoked", "active policy generation has been revoked")
    if not set(revoked) <= set(state["revoked_generations"]):
        _deny("active_mismatch", "signed revocations are missing from committed state")
    # Interval still valid at moment (verify already checked, but double-check via stored)
    nb_dt = _parse_zulu(policy["not_before"], "not_before")
    exp_dt = _parse_zulu(policy["expires_at"], "expires_at")
    if not (nb_dt <= moment < exp_dt):
        _deny("policy_expired" if moment >= exp_dt else "policy_not_yet_valid", "active policy is outside its validity interval")
    # Clock monotonic still holds
    stored_max = state.get("max_clock")
    if stored_max is not None and moment < _parse_zulu(stored_max, "max_clock"):
        _deny("clock_rollback", "active check clock is earlier than stored max_clock")
    return policy if return_policy else state


def is_active(state_path, *, now=None) -> bool:
    """Return True iff a policy is committed and still within expiry at ``now``.

    Never raises on missing/corrupt state – returns False (fail closed).
    Performs no signature re-verification; callers needing a proof plus
    interval check should use :func:`assert_active`.
    """
    try:
        state = load_activation_state(state_path)
        if state["generation"] == 0 or state["generation"] in state["revoked_generations"]:
            return False
        moment = _validate_now(now) if now is not None else datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        nb_dt = _parse_zulu(state["not_before"], "not_before")
        exp_dt = _parse_zulu(state["expires_at"], "expires_at")
        if not (nb_dt <= moment < exp_dt):
            return False
        stored_max = state.get("max_clock")
        if stored_max is not None and moment < _parse_zulu(stored_max, "max_clock"):
            return False
        return True
    except Exception:
        return False
