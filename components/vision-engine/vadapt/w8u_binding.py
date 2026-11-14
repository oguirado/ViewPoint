"""Fail-closed W8U HELLO binding admission, independent of unmerged W8U owners.

This module admits *metadata*, not streaming credit, UI qualification, actions or
worker_ui. Contract, seat-grant, exact frame-channel and target authority are
injected. There is no production verifier or fallback to a local hash match.
"""

from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Callable, Mapping


FRAME_ROOT = "/dev/shm/vqa-ramws"  # vadapt/ramws.py:19, bridge/integration
CAMPAIGN = "os-cu-0"
TOOL = "opensuperficies"
MAX_HELLO_BYTES = 16 * 1024
U32 = (1 << 32) - 1
U64 = (1 << 64) - 1
_RE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_RE_SHA = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RE_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_BINDING_KEYS = frozenset((
    "schema", "run_id", "lease_id", "seat_grant_sha256", "container_id",
    "target_handle", "epoch", "display", "xid", "width", "height",
    "frames_root", "frame_run_root", "image_sha256", "reader_sha256",
    "exec_allowlist_sha256", "max_record_bytes", "max_inflight_frames",
    "tick_mode",
))
_TARGET_KEYS = frozenset(("kind", "canonical_id", "generation"))
_KINDS = frozenset(("cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"))
_TICK_MODES = frozenset(("committed", "unavailable"))


class BindingRefusal(ValueError):
    """Closed W8U error-code refusal; never implicitly admits an invalid HELLO."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _refuse(code, message):
    raise BindingRefusal(code, message)


def _exact_int(v, name, *, minimum=0, maximum=U64, code="E_BINDING"):
    if type(v) is not int or not minimum <= v <= maximum:
        _refuse(code, f"{name} must be an exact integer in range")
    return v


def _nonempty_str(v, name):
    if type(v) is not str or not v or any(ord(c) < 32 or ord(c) == 127 for c in v):
        _refuse("E_BINDING", f"invalid {name}")
    return v


def _id(v, name):
    if type(v) is not str or not _RE_ID.fullmatch(v) or v in (".", ".."):
        _refuse("E_BINDING", f"invalid {name}")
    return v


def _sha(v, name):
    if type(v) is not str or not _RE_SHA.fullmatch(v):
        _refuse("E_BINDING", f"invalid lowercase SHA-256 for {name}")
    return v


def _closed(value, keys, context):
    if type(value) is not dict or frozenset(value) != keys:
        _refuse("E_BINDING", f"{context} has missing, extra or non-string keys")


def _pairs_closed(pairs):
    output = {}
    for k, v in pairs:
        if k in output:
            _refuse("E_BINDING", f"duplicate JSON key {k!r}")
        output[k] = v
    return output


def _bad_constant(value):
    _refuse("E_BINDING", f"non-JSON number {value}")


def _canonical(data: dict) -> bytes:
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, RuntimeError) as exc:
        raise BindingRefusal("E_BINDING", "non-canonical HELLO mapping") from exc


def _decode_hello(hello):
    if type(hello) is bytes:
        original = bytes(hello)
    elif type(hello) is bytearray or type(hello) is memoryview:
        original = bytes(hello)  # snapshot before any verifier is called
    elif type(hello) is dict:
        # JSON roundtrip makes a *deep*, primitive, owned snapshot; unsupported
        # objects and cycles refuse rather than leaking caller-owned structures.
        original = _canonical(hello)
    else:
        _refuse("E_BINDING", "HELLO must be bytes or a plain mapping")
    if not original or len(original) > MAX_HELLO_BYTES:
        _refuse("E_LENGTH", "HELLO JSON outside 1..16384 bytes")
    try:
        data = json.loads(original.decode("utf-8"),
                          object_pairs_hook=_pairs_closed,
                          parse_constant=_bad_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, BindingRefusal):
            raise
        raise BindingRefusal("E_BINDING", "invalid UTF-8 JSON") from exc
    if type(data) is not dict:
        _refuse("E_BINDING", "HELLO must be a JSON object")
    _closed(data, _BINDING_KEYS, "binding")
    if _canonical(data) != original:
        _refuse("E_BINDING", "HELLO JSON is not exact sorted compact canonical UTF-8")
    return data, original


@dataclass(frozen=True)
class TargetIdentity:
    kind: str
    canonical_id: str
    generation: int


@dataclass(frozen=True)
class BindingFields:
    schema: str
    run_id: str
    lease_id: str
    seat_grant_sha256: str
    container_id: str
    target_handle: TargetIdentity
    epoch: int
    display: str
    xid: int
    width: int
    height: int
    frames_root: str
    frame_run_root: str
    image_sha256: str
    reader_sha256: str
    exec_allowlist_sha256: str
    max_record_bytes: int
    max_inflight_frames: int
    tick_mode: str


@dataclass(frozen=True)
class ContractProof:
    """Return only from an independently trusted accepted-contract verifier."""
    schema: str
    accepted: bool
    revision_sha: str
    evidence_sha256: str


@dataclass(frozen=True)
class GrantProof:
    """Authenticated grant facts and independent measured storage roots."""
    authenticated: bool
    measured: bool
    contract_revision: str
    claims: BindingFields
    grant_root: str
    observation_root: str
    ingest_root: str
    grant_run_root: str
    observation_run_root: str
    ingest_run_root: str
    private_display: bool


@dataclass(frozen=True)
class FrameChannelProof:
    """Authenticated, measured exact channel, including reserved capacity."""
    authenticated: bool
    measured: bool
    contract_revision: str
    binding_sha256: str
    target_handle: TargetIdentity
    epoch: int
    display: str
    xid: int
    width: int
    height: int
    frames_root: str
    frame_run_root: str
    image_sha256: str
    reader_sha256: str
    exec_allowlist_sha256: str
    max_record_bytes: int
    reserved_slots: int
    reserved_bytes: int
    tick_mode: str
    private_display: bool


@dataclass(frozen=True)
class CurrentTarget:
    """Snapshot provided by an authoritative live target-registry adapter."""
    measured: bool
    contract_revision: str
    target_handle: TargetIdentity
    epoch: int
    display: str
    xid: int
    width: int
    height: int


@dataclass(frozen=True)
class CommitCoordinationProof:
    """Separate render/capture coordination, *not* grab-pair equality."""
    authenticated: bool
    independent: bool
    contract_revision: str
    app_render_revision: int
    capture_revision: int


@dataclass(frozen=True)
class AdmittedBinding:
    fields: BindingFields
    canonical_json: bytes
    binding_sha256: str
    source_contract_revision: str

    @property
    def runtime_state(self):
        # Qualification and receipt evidence belong to later merged owners.
        return "HOLD"

    @property
    def worker_ui(self):
        return False


def _fields(data):
    raw_target = data["target_handle"]
    _closed(raw_target, _TARGET_KEYS, "target_handle")
    kind = raw_target["kind"]
    if type(kind) is not str or kind not in _KINDS:
        _refuse("E_BINDING", "unsupported target kind")
    target = TargetIdentity(
        kind, _nonempty_str(raw_target["canonical_id"], "canonical_id"),
        _exact_int(raw_target["generation"], "generation", maximum=U64),
    )
    mode = data["tick_mode"]
    if type(mode) is not str or mode not in _TICK_MODES:
        _refuse("E_TICK", "unknown tick modality")
    return BindingFields(
        schema=_nonempty_str(data["schema"], "schema"),
        run_id=_id(data["run_id"], "run_id"),
        lease_id=_id(data["lease_id"], "lease_id"),
        seat_grant_sha256=_sha(data["seat_grant_sha256"], "seat_grant_sha256"),
        container_id=_nonempty_str(data["container_id"], "container_id"),
        target_handle=target,
        epoch=_exact_int(data["epoch"], "epoch"),
        display=_nonempty_str(data["display"], "display"),
        xid=_exact_int(data["xid"], "xid", minimum=1, maximum=U32),
        width=_exact_int(data["width"], "width", minimum=1, maximum=U32, code="E_GEOMETRY"),
        height=_exact_int(data["height"], "height", minimum=1, maximum=U32, code="E_GEOMETRY"),
        frames_root=_nonempty_str(data["frames_root"], "frames_root"),
        frame_run_root=_nonempty_str(data["frame_run_root"], "frame_run_root"),
        image_sha256=_sha(data["image_sha256"], "image_sha256"),
        reader_sha256=_sha(data["reader_sha256"], "reader_sha256"),
        exec_allowlist_sha256=_sha(data["exec_allowlist_sha256"], "exec_allowlist_sha256"),
        max_record_bytes=_exact_int(data["max_record_bytes"], "max_record_bytes", minimum=144, code="E_LENGTH"),
        max_inflight_frames=_exact_int(data["max_inflight_frames"], "max_inflight_frames", minimum=1, code="E_CREDIT"),
        tick_mode=mode,
    )


def _strict_root(path):
    if type(path) is not str or path != FRAME_ROOT or os.path.normpath(path) != path:
        _refuse("E_ROOT", "frames root is not the pinned canonical tmpfs root")
    _no_symlinks(path)


def _no_symlinks(path):
    # Verify every currently existing component, without creating anything.
    # Missing components require injected *measured* evidence; they are not
    # treated as filesystem authentication by this function.
    curr = os.path.sep
    for part in path.split(os.path.sep)[1:]:
        if not part:
            _refuse("E_ROOT", "empty path component or alias")
        curr = os.path.join(curr, part)
        if os.path.islink(curr):
            _refuse("E_ROOT", "symlink in root or run path")
    if os.path.realpath(path) != path:
        _refuse("E_ROOT", "non-canonical root/run path")


def _validate_roots(f, grant, channel):
    _strict_root(f.frames_root)
    expected = f"{FRAME_ROOT}/{TOOL}/{CAMPAIGN}/{f.run_id}"
    if f.frame_run_root != expected or os.path.normpath(f.frame_run_root) != expected:
        _refuse("E_ROOT", "run path is not the exact confined campaign child")
    _no_symlinks(f.frame_run_root)
    if any(v != f.frames_root for v in (
        grant.grant_root, grant.observation_root, grant.ingest_root, channel.frames_root,
    )) or any(v != f.frame_run_root for v in (
        grant.grant_run_root, grant.observation_run_root,
        grant.ingest_run_root, channel.frame_run_root,
    )):
        _refuse("E_ROOT", "grant, observation, ingest and channel root disagreement")


def _call_required(verifier, *arguments, code="E_BINDING"):
    if not callable(verifier):
        _refuse(code, "independent authentication callback absent")
    try:
        return verifier(*arguments)
    except BindingRefusal:
        raise
    except Exception as exc:
        raise BindingRefusal(code, "authority could not verify the binding") from exc


def _assert_target_types(target):
    if type(target) is not TargetIdentity:
        _refuse("E_BINDING", "authority provided mutable/unknown target identity")
    if type(target.kind) is not str or target.kind not in _KINDS:
        _refuse("E_BINDING", "authority target kind invalid")
    _nonempty_str(target.canonical_id, "authority canonical_id")
    _exact_int(target.generation, "authority generation")


def _assert_grant_claim_types(claims):
    if type(claims) is not BindingFields:
        _refuse("E_BINDING", "grant claims are not immutable binding fields")
    _assert_target_types(claims.target_handle)
    raw = {field: getattr(claims, field) for field in _BINDING_KEYS if field != "target_handle"}
    raw["target_handle"] = dict(kind=claims.target_handle.kind,
                                canonical_id=claims.target_handle.canonical_id,
                                generation=claims.target_handle.generation)
    # Even frozen dataclasses can be manually constructed with mutable/numeric
    # coercions; validate the verifier's facts as strictly as the wire facts.
    return _fields(raw)


def _assert_channel_fact_types(channel):
    _assert_target_types(channel.target_handle)
    for name in ("epoch", "xid", "width", "height", "max_record_bytes"):
        _exact_int(getattr(channel, name), "channel." + name)
    for name in ("frames_root", "frame_run_root", "display", "tick_mode"):
        _nonempty_str(getattr(channel, name), "channel." + name)
    for name in ("image_sha256", "reader_sha256", "exec_allowlist_sha256",
                 "binding_sha256"):
        _sha(getattr(channel, name), "channel." + name)


def _assert_current_fact_types(current):
    _assert_target_types(current.target_handle)
    for name in ("epoch", "xid", "width", "height"):
        _exact_int(getattr(current, name), "current." + name)
    _nonempty_str(current.display, "current.display")


def admit_hello(hello, *, contract_verifier: Callable,
                grant_verifier: Callable, channel_verifier: Callable,
                target_snapshot: Callable,
                commit_verifier: Callable | None = None) -> AdmittedBinding:
    """Authenticate the exact immutable HELLO descriptor before any credit.

    Verifier adapters MUST independently authenticate source identity, contract
    acceptance, grant, channel capability and measurement. Boolean assertions or
    a caller-supplied hash are not substitutes for a trusted adapter. No I/O,
    allocation, capture, ACK, worker_ui promotion or recipient side effects
    occur in this layer. Exceptions are fail-closed W8U BindingRefusal values.
    """
    data, canonical = _decode_hello(hello)
    f = _fields(data)
    digest = hashlib.sha256(canonical).hexdigest()

    # Contract and authentication are *separate* from the descriptor digest.
    contract = _call_required(contract_verifier, canonical, digest)
    if (type(contract) is not ContractProof or contract.accepted is not True
            or type(contract.schema) is not str or f.schema != contract.schema
            or type(contract.revision_sha) is not str
            or not _RE_COMMIT.fullmatch(contract.revision_sha)
            or type(contract.evidence_sha256) is not str
            or not _RE_SHA.fullmatch(contract.evidence_sha256)):
        _refuse("E_BINDING", "accepted revision-bound literal schema authority absent")

    grant = _call_required(grant_verifier, canonical, digest)
    if (type(grant) is not GrantProof or grant.authenticated is not True
            or grant.measured is not True or grant.private_display is not True
            or grant.contract_revision != contract.revision_sha
            or type(grant.claims) is not BindingFields
            or type(grant.claims.target_handle) is not TargetIdentity):
        _refuse("E_BINDING", "independently authenticated and measured exact grant absent")
    if _assert_grant_claim_types(grant.claims) != f:
        _refuse("E_BINDING", "authenticated grant claims do not match HELLO")

    channel = _call_required(channel_verifier, canonical, digest)
    if (type(channel) is not FrameChannelProof
            or channel.authenticated is not True or channel.measured is not True
            or channel.private_display is not True
            or channel.contract_revision != contract.revision_sha
            or channel.binding_sha256 != digest
            or type(channel.target_handle) is not TargetIdentity):
        _refuse("E_BINDING", "authenticated exact frame channel capability absent")
    _assert_channel_fact_types(channel)

    _validate_roots(f, grant, channel)

    # Compare all bound channel identity fields. A pinned digest alone would not
    # demonstrate that this channel or its backing allocation is the right one.
    for field in (
        "target_handle", "epoch", "display", "xid", "width", "height",
        "image_sha256", "reader_sha256", "exec_allowlist_sha256",
        "max_record_bytes", "tick_mode",
    ):
        if getattr(channel, field) != getattr(f, field):
            _refuse("E_BINDING", f"frame channel {field} mismatch")

    current = _call_required(target_snapshot)
    if (type(current) is not CurrentTarget or current.measured is not True
            or current.contract_revision != contract.revision_sha
            or type(current.target_handle) is not TargetIdentity):
        _refuse("E_BINDING", "current registry attestation absent")
    _assert_current_fact_types(current)
    if current.target_handle != f.target_handle or current.epoch != f.epoch:
        _refuse("E_BINDING", "foreign target or stale generation/epoch")
    if (current.width, current.height, current.display, current.xid) != (
            f.width, f.height, f.display, f.xid):
        _refuse("E_GEOMETRY", "geometry or private display/xid changed")

    cap = 2 * (4 * f.width * f.height + (1 << 20)) + 16 + 128
    if cap > U64:
        _refuse("E_GEOMETRY", "geometry-derived cap overflows unsigned wire size")
    if f.max_record_bytes > cap:
        _refuse("E_LENGTH", "negotiated record cap exceeds geometry cap")
    if (_exact_int(channel.reserved_slots, "reserved_slots", minimum=1)
            < f.max_inflight_frames):
        _refuse("E_CREDIT", "negotiated capacity exceeds reserved frame slots")
    if (_exact_int(channel.reserved_bytes, "reserved_bytes", minimum=1)
            < f.max_record_bytes * f.max_inflight_frames):
        _refuse("E_RAM_QUOTA", "reserved RAM insufficient for negotiated credit")

    if f.tick_mode == "committed":
        commit = _call_required(commit_verifier, f, grant, channel, current, code="E_TICK")
        if (type(commit) is not CommitCoordinationProof
                or commit.authenticated is not True or commit.independent is not True
                or commit.contract_revision != contract.revision_sha):
            _refuse("E_TICK", "independent app render/capture coordination absent")
        _exact_int(commit.app_render_revision, "app_render_revision", code="E_TICK")
        _exact_int(commit.capture_revision, "capture_revision", code="E_TICK")
        if commit.app_render_revision != commit.capture_revision:
            _refuse("E_TICK", "render and capture are not bound to one revision")

    return AdmittedBinding(fields=f, canonical_json=canonical,
                           binding_sha256=digest,
                           source_contract_revision=contract.revision_sha)
