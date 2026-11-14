"""W8U host memory admission accountant, storage law, and purge settlement owner.

Production owner ``w8u-ve-memory`` (Execution Wave 1).
Uses only Python stdlib and landed ``vadapt`` prerequisites (``ramws``, ``rtring``,
``window_stream_bank``). Unmerged sibling W8U owners and contract values are not
imported or defaulted; omitted prerequisites or evidence report ``AUSENTE`` and
fail closed.
"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import struct
import threading
from vadapt import ramws, rtring, window_stream_bank

AUSENTE = "AUSENTE"
PREPARED = "PREPARED"
ACCEPT = "ACCEPT"

GATE_NAMES = (
    "Shared-memory accounting",
    "Storage law",
    "Purge settlement",
)

CLOSED_ERROR_CODES = (
    "E_TORN",
    "E_WIRE",
    "E_LENGTH",
    "E_HASH",
    "E_SEQ",
    "E_TICK",
    "E_BINDING",
    "E_GEOMETRY",
    "E_CREDIT",
    "E_BACKPRESSURE",
    "E_TIMEOUT",
    "E_ROOT",
    "E_RAM_QUOTA",
)

RESERVATION_CATEGORIES = (
    "ring_storage",
    "pending_raw_pair",
    "canonical_decoding_copies",
    "held_consumer_views",
    "control_buffers",
)

BINDING_KEYS = frozenset({
    "schema",
    "run_id",
    "lease_id",
    "seat_grant_sha256",
    "container_id",
    "target_handle",
    "epoch",
    "display",
    "xid",
    "width",
    "height",
    "frames_root",
    "frame_run_root",
    "image_sha256",
    "reader_sha256",
    "exec_allowlist_sha256",
    "max_record_bytes",
    "max_inflight_frames",
    "tick_mode",
})

TARGET_HANDLE_KEYS = frozenset({"kind", "canonical_id", "generation"})

FRAME_METADATA_KEYS = (
    "seq",
    "tick",
    "tick_valid",
    "capture_mono_ns",
    "width",
    "height",
    "stride",
    "pixel_format",
    "sha256",
    "binding_sha256",
)

RECEIPT_SCHEMA = "w8u.receipt/v1"
DEFAULT_RECEIPT_SECTIONS = (
    "schema",
    "identity",
    "populations",
    "first_frame",
    "last_frame",
    "foreign_action",
    "foreign_t2",
    "foreign_state",
    "verdict",
    "code",
    "storage_evidence",
    "listener_evidence",
    "exit_evidence",
    "cleanup_evidence",
)

FRAME_MAGIC = b"W8UF"
FRAME_VERSION = 1
FRAME_HEADER_BYTES = 128
FRAME_PIXEL_FORMAT_RGB24 = 1
FLAG_PAIR_UNDER_GRAB = 1 << 0
FLAG_TICK_VALID = 1 << 1
FLAG_ALLOWED_MASK = FLAG_PAIR_UNDER_GRAB | FLAG_TICK_VALID

CONTROL_MAGIC_CLIENT = b"W8UC"
CONTROL_MAGIC_HELLO_ACK = b"W8UH"
CONTROL_MAGIC_END = b"W8UE"
CONTROL_VERSION = 1
CONTROL_HEADER_BYTES = 44
CONTROL_KIND_HELLO = 1
CONTROL_KIND_ACK = 2
CONTROL_KIND_STOP = 3
HELLO_MAX_BODY_BYTES = 16 * 1024
PREFERRED_INITIAL_CREDIT_GRANT = 2

MIB = 1 << 20
MAX_U16 = (1 << 16) - 1
MAX_U32 = (1 << 32) - 1
MAX_U64 = (1 << 64) - 1

CONFINED_TOOL = "opensuperficies"
CONFINED_CAMPAIGN = "os-cu-0"

_FRAME_HDR_STRUCT = struct.Struct(">4sHHQQQQIIIHHQ32s32s")
_PAIR_LEN_STRUCT = struct.Struct(">Q")
_CTRL_HDR_STRUCT = struct.Struct(">4sHHI32s")
_ACK_BODY_STRUCT = struct.Struct(">QQ")
_END_BODY_STRUCT = struct.Struct(">QQI")
_XWD_HDR_STRUCT = struct.Struct(">25I")
_HEX64_CHARS = frozenset("0123456789abcdef")


class W8UMemoryError(ramws.RamError):
    """Closed W8U / RAM error with explicit code and optional structured detail."""

    def __init__(self, code, msg="", detail=None):
        if code not in CLOSED_ERROR_CODES and code not in (
            "E_RAM_NOT_TMPFS",
            "E_RAM_ESCAPE",
            "E_RAM_LOCKED",
            "E_RAM_SHARE",
            AUSENTE,
        ):
            raise ValueError("unrecognized W8U/RAM error code: %r" % (code,))
        super().__init__(code, msg)
        self.message = msg
        self.detail = detail or {}


def _exact_int(value, name, *, allow_zero=False, max_value=MAX_U64, error_code="E_RAM_QUOTA"):
    if type(value) is not int:
        raise W8UMemoryError(error_code, "%s must be an exact int (got %r)" % (name, value))
    lo = 0 if allow_zero else 1
    if value < lo or (max_value is not None and value > max_value):
        raise W8UMemoryError(
            error_code,
            "%s=%d outside allowed range [%d, %s]" % (name, value, lo, max_value),
        )
    return value


def _checked_mul(a, b, name="product", *, max_value=MAX_U64, error_code="E_RAM_QUOTA"):
    _exact_int(a, name + ".a", allow_zero=True, max_value=max_value, error_code=error_code)
    _exact_int(b, name + ".b", allow_zero=True, max_value=max_value, error_code=error_code)
    prod = a * b
    if max_value is not None and prod > max_value:
        raise W8UMemoryError(error_code, "%s multiplication overflow (%d * %d)" % (name, a, b))
    return prod


def _checked_sum(values, name="sum", *, max_value=MAX_U64, error_code="E_RAM_QUOTA"):
    total = 0
    for idx, val in enumerate(values):
        _exact_int(val, "%s[%d]" % (name, idx), allow_zero=True, max_value=max_value, error_code=error_code)
        total += val
        if max_value is not None and total > max_value:
            raise W8UMemoryError(error_code, "%s addition overflow at index %d" % (name, idx))
    return total


def _is_hex64(value):
    return type(value) is str and len(value) == 64 and all(c in _HEX64_CHARS for c in value)


def domain_sha256(domain: str, payload: bytes) -> str:
    """Compute a domain-separated SHA-256 hex digest for UI, simulation, or RGB24."""
    if domain not in ("ui", "simulation", "rgb24", "binding"):
        raise W8UMemoryError("E_HASH", "unsupported digest domain %r" % (domain,))
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise W8UMemoryError("E_HASH", "digest payload must be bytes-like")
    raw = bytes(payload)
    if domain == "rgb24" or domain == "binding":
        return hashlib.sha256(raw).hexdigest()
    prefix = ("w8u.%s.v1\x00" % domain).encode("ascii")
    return hashlib.sha256(prefix + raw).hexdigest()


# ---------------------------------------------------------------------------
# Revision-bound w8u-os-contract evidence & readiness
# ---------------------------------------------------------------------------

REQUIRED_CONTRACT_FIELDS = (
    "revision",
    "accepted",
    "binding_schema_id",
    "action_result_schema_id",
    "receipt_schema_id",
    "receipt_closed_fields",
    "control_combinations",
    "end_error_codes",
    "closed_error_numeric_map",
    "digest_byte_domains",
    "xwd_subset",
    "native_error_translation",
    "cross_language_vectors",
)


@dataclass(frozen=True)
class ContractEvidence:
    """Revision-bound w8u-os-contract evidence required before ACCEPT."""

    revision: str = AUSENTE
    accepted: bool = False
    binding_schema_id: str = AUSENTE
    action_result_schema_id: str = AUSENTE
    receipt_schema_id: str = AUSENTE
    receipt_closed_fields: tuple = ()
    control_combinations: dict = field(default_factory=dict)
    end_error_codes: dict = field(default_factory=dict)
    closed_error_numeric_map: dict = field(default_factory=dict)
    digest_byte_domains: dict = field(default_factory=dict)
    xwd_subset: dict = field(default_factory=dict)
    native_error_translation: dict = field(default_factory=dict)
    cross_language_vectors: dict = field(default_factory=dict)
    merged_production_owners: tuple = ()


def inspect_contract_evidence(contract) -> dict:
    """Inspect contract evidence and report any omitted/unfrozen items as AUSENTE."""
    if contract is None:
        return {
            "status": PREPARED,
            "missing": {k: AUSENTE for k in REQUIRED_CONTRACT_FIELDS},
            "ready": False,
        }
    missing = {}
    for key in REQUIRED_CONTRACT_FIELDS:
        val = getattr(contract, key, AUSENTE) if not isinstance(contract, dict) else contract.get(key, AUSENTE)
        if val is None or val == AUSENTE or val == "" or val == () or val == {}:
            missing[key] = AUSENTE

    ctrl = (
        getattr(contract, "control_combinations", {})
        if not isinstance(contract, dict)
        else contract.get("control_combinations", {})
    ) or {}
    for req_ctrl in ("HELLO", "HELLO_CONFIRM", "ACK", "STOP", "END"):
        if req_ctrl not in ctrl or ctrl[req_ctrl] in (None, AUSENTE, {}):
            missing["control_combinations.%s" % req_ctrl] = AUSENTE

    end_codes = (
        getattr(contract, "end_error_codes", {})
        if not isinstance(contract, dict)
        else contract.get("end_error_codes", {})
    ) or {}
    if "OK" not in end_codes or "unknown_code_refusal" not in end_codes:
        missing["end_error_codes.completeness"] = AUSENTE

    num_map = (
        getattr(contract, "closed_error_numeric_map", {})
        if not isinstance(contract, dict)
        else contract.get("closed_error_numeric_map", {})
    ) or {}
    for code in CLOSED_ERROR_CODES:
        if code not in num_map or type(num_map[code]) is not int:
            missing["closed_error_numeric_map.%s" % code] = AUSENTE

    vectors = (
        getattr(contract, "cross_language_vectors", {})
        if not isinstance(contract, dict)
        else contract.get("cross_language_vectors", {})
    ) or {}
    accepted_vecs = vectors.get("accepted") if isinstance(vectors, dict) else None
    rejected_vecs = vectors.get("rejected") if isinstance(vectors, dict) else None
    if not accepted_vecs or not isinstance(accepted_vecs, (list, tuple, dict)):
        missing["cross_language_vectors.accepted"] = AUSENTE
    if not isinstance(rejected_vecs, dict) or "oversized_header" not in rejected_vecs or "invalid_bitmap_bit_order" not in rejected_vecs:
        missing["cross_language_vectors.rejected"] = AUSENTE

    accepted_flag = (
        getattr(contract, "accepted", False)
        if not isinstance(contract, dict)
        else contract.get("accepted", False)
    )
    if accepted_flag is not True:
        missing["accepted"] = AUSENTE

    ready = len(missing) == 0
    return {
        "status": ACCEPT if ready else PREPARED,
        "missing": missing,
        "ready": ready,
    }


def require_accepted_contract(contract, *, error_code="E_BINDING"):
    """Require complete accepted revision-bound w8u-os-contract evidence."""
    report = inspect_contract_evidence(contract)
    if not report["ready"]:
        raise W8UMemoryError(
            error_code,
            "contract evidence incomplete or pending (%s): %s"
            % (report["status"], sorted(report["missing"].keys())),
            detail=report,
        )
    return contract


def translate_native_error(native_code, contract) -> str:
    """Translate a native validator error into a closed W8U error using frozen contract authority."""
    require_accepted_contract(contract, error_code="E_WIRE")
    mapping = (
        getattr(contract, "native_error_translation", {})
        if not isinstance(contract, dict)
        else contract.get("native_error_translation", {})
    )
    if not isinstance(native_code, str) or not native_code or native_code not in mapping:
        raise W8UMemoryError("E_WIRE", "unmapped native error %r (%s)" % (native_code, AUSENTE))
    target = mapping[native_code]
    if target not in CLOSED_ERROR_CODES:
        raise W8UMemoryError("E_WIRE", "invalid translated error %r" % (target,))
    return target


# ---------------------------------------------------------------------------
# Geometry & transport sizing formulas
# ---------------------------------------------------------------------------

def canonical_rgb24_bytes(width: int, height: int, *, error_code="E_RAM_QUOTA") -> int:
    """Return exact canonical RGB24 byte size (width * height * 3) with overflow checks."""
    w = _exact_int(width, "width", max_value=MAX_U32, error_code=error_code)
    h = _exact_int(height, "height", max_value=MAX_U32, error_code=error_code)
    pixels = _checked_mul(w, h, "width*height", max_value=MAX_U64, error_code=error_code)
    return _checked_mul(pixels, 3, "width*height*3", max_value=MAX_U64, error_code=error_code)


def actual_ring_bytes(slots: int, width: int, height: int) -> int:
    """Compute actual rtring.ring_bytes(slots, width, height) including 4096 alignment and headers."""
    s = _exact_int(slots, "slots", max_value=MAX_U32, error_code="E_RAM_QUOTA")
    w = _exact_int(width, "width", max_value=MAX_U32, error_code="E_RAM_QUOTA")
    h = _exact_int(height, "height", max_value=MAX_U32, error_code="E_RAM_QUOTA")
    slot_bytes = canonical_rgb24_bytes(w, h, error_code="E_RAM_QUOTA")
    hdr_total = _checked_sum([rtring.HDR, _checked_mul(s, rtring.SLOT_HDR, "slots*SLOT_HDR")], "ring_hdr")
    aligned_off = ((hdr_total + rtring.ALIGN - 1) // rtring.ALIGN) * rtring.ALIGN
    payload_total = _checked_mul(s, slot_bytes, "slots*slot_bytes")
    total = _checked_sum([aligned_off, payload_total], "ring_bytes")
    rtring_val = rtring.ring_bytes(s, w, h)
    if total != rtring_val:
        raise W8UMemoryError("E_RAM_QUOTA", "ring_bytes mismatch (%d != %d)" % (total, rtring_val))
    return total


def geometry_record_cap(width: int, height: int, *, error_code="E_RAM_QUOTA") -> int:
    """Return W8U geometry cap: 2 * (4 * width * height + 1 MiB) + 16 + 128."""
    w = _exact_int(width, "width", max_value=MAX_U32, error_code=error_code)
    h = _exact_int(height, "height", max_value=MAX_U32, error_code=error_code)
    px = _checked_mul(w, h, "width*height", max_value=MAX_U64, error_code=error_code)
    quad = _checked_mul(4, px, "4*width*height", max_value=MAX_U64, error_code=error_code)
    single_xwd_cap = _checked_sum([quad, MIB], "single_xwd_cap", max_value=MAX_U64, error_code=error_code)
    pair_xwd_cap = _checked_mul(2, single_xwd_cap, "pair_xwd_cap", max_value=MAX_U64, error_code=error_code)
    return _checked_sum([pair_xwd_cap, 16, FRAME_HEADER_BYTES], "geometry_record_cap", max_value=MAX_U64, error_code=error_code)


def effective_record_cap(width: int, height: int, negotiated_max_record_bytes: int, *, error_code="E_RAM_QUOTA") -> int:
    """Enforce the smaller of negotiated max_record_bytes and geometry_record_cap."""
    neg = _exact_int(negotiated_max_record_bytes, "max_record_bytes", max_value=MAX_U64, error_code=error_code)
    if neg <= FRAME_HEADER_BYTES + 16:
        raise W8UMemoryError(error_code, "max_record_bytes (%d) too small for header + inner lengths" % neg)
    geom = geometry_record_cap(width, height, error_code=error_code)
    return min(neg, geom)


# ---------------------------------------------------------------------------
# Canonical JSON, Binding, and Root Confinement Validation
# ---------------------------------------------------------------------------

def canonical_json_bytes(obj) -> bytes:
    """Encode an object to sorted compact UTF-8 integer-only JSON without LF."""
    def _check_val(v):
        if type(v) is bool or type(v) is float:
            raise W8UMemoryError("E_BINDING", "bool and float are forbidden in canonical JSON (%r)" % (v,))
        if type(v) is int:
            return
        if type(v) is str:
            if "\n" in v or "\r" in v:
                raise W8UMemoryError("E_BINDING", "linefeeds forbidden in canonical JSON string")
            return
        if type(v) is dict:
            for k, sub in v.items():
                if type(k) is not str or "\n" in k or "\r" in k:
                    raise W8UMemoryError("E_BINDING", "invalid dict key in canonical JSON")
                _check_val(sub)
            return
        if type(v) in (list, tuple):
            for sub in v:
                _check_val(sub)
            return
        raise W8UMemoryError("E_BINDING", "unsupported canonical JSON type %r" % (type(v),))

    _check_val(obj)
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if "\n" in encoded or "\r" in encoded:
        raise W8UMemoryError("E_BINDING", "canonical JSON must not contain LF/CR")
    return encoded.encode("utf-8")


def parse_canonical_json_bytes(raw: bytes):
    """Parse and verify exact canonical sorted compact UTF-8 integer-only JSON without LF."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise W8UMemoryError("E_WIRE", "canonical JSON input must be bytes")
    b = bytes(raw)
    if b"\n" in b or b"\r" in b:
        raise W8UMemoryError("E_BINDING", "canonical JSON must not contain LF/CR")
    try:
        text = b.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise W8UMemoryError("E_BINDING", "canonical JSON is not valid UTF-8") from exc

    def _reject_float(num_str):
        raise W8UMemoryError("E_BINDING", "float forbidden in canonical JSON: %s" % num_str)

    try:
        obj = json.loads(text, parse_float=_reject_float, parse_constant=_reject_float)
    except W8UMemoryError:
        raise
    except ValueError as exc:
        raise W8UMemoryError("E_BINDING", "invalid JSON: %s" % exc) from exc

    reencoded = canonical_json_bytes(obj)
    if reencoded != b:
        raise W8UMemoryError("E_BINDING", "non-canonical JSON key order or whitespace")
    return obj


def validate_confined_roots(frames_root: str, frame_run_root: str, run_id: str, *, check_fs_symlinks=True):
    """Require the exact opensuperficies/os-cu-0/<run_id> child; no aliases or fallback."""
    if type(frames_root) is not str or type(frame_run_root) is not str or type(run_id) is not str:
        raise W8UMemoryError("E_ROOT", "roots and run_id must be strings")
    if not run_id or not ramws.NAME_RE.fullmatch(run_id) or run_id in (".", ".."):
        raise W8UMemoryError("E_ROOT", "invalid run_id %r" % (run_id,))
    if "\x00" in frames_root or "\x00" in frame_run_root:
        raise W8UMemoryError("E_ROOT", "NUL byte in root path")
    if frames_root != ramws.ROOT:
        raise W8UMemoryError("E_ROOT", "frames_root must be exactly %s (got %r)" % (ramws.ROOT, frames_root))
    if frame_run_root.endswith("/"):
        raise W8UMemoryError("E_ROOT", "frame_run_root must not have trailing slash: %r" % (frame_run_root,))

    raw_parts = frame_run_root.split("/")
    if any(p in ("", ".", "..") for p in raw_parts[1:]) or raw_parts[0] != "":
        raise W8UMemoryError("E_ROOT", "unnormalized frame_run_root %r" % (frame_run_root,))

    expected = "%s/%s/%s/%s" % (ramws.ROOT, CONFINED_TOOL, CONFINED_CAMPAIGN, run_id)
    if frame_run_root != expected:
        raise W8UMemoryError(
            "E_ROOT",
            "frame_run_root %r is not the exact confined run child %r"
            % (frame_run_root, expected),
        )

    if check_fs_symlinks:
        cur = Path("/")
        for part in raw_parts[1:]:
            cur = cur / part
            if cur.is_symlink():
                raise W8UMemoryError("E_ROOT", "symlink alias in root hierarchy: %s" % cur)
    return True


def validate_binding(
    binding_input,
    *,
    contract,
    grant_authenticator=None,
    expected_target_handle=None,
    expected_epoch=None,
    expected_geometry=None,
    check_fs_symlinks=True,
) -> tuple[dict, bytes, bytes]:
    """Validate a canonical W8U binding (dict or canonical UTF-8 bytes) and return (dict, raw_bytes, sha256_raw)."""
    require_accepted_contract(contract, error_code="E_BINDING")
    if isinstance(binding_input, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(binding_input)
        if len(raw_bytes) > HELLO_MAX_BODY_BYTES:
            raise W8UMemoryError("E_LENGTH", "HELLO binding JSON exceeds 16 KiB (%d B)" % len(raw_bytes))
        binding = parse_canonical_json_bytes(raw_bytes)
    elif isinstance(binding_input, dict):
        raw_bytes = canonical_json_bytes(binding_input)
        if len(raw_bytes) > HELLO_MAX_BODY_BYTES:
            raise W8UMemoryError("E_LENGTH", "HELLO binding JSON exceeds 16 KiB (%d B)" % len(raw_bytes))
        binding = binding_input
    else:
        raise W8UMemoryError("E_BINDING", "binding must be dict or canonical UTF-8 bytes")

    if not isinstance(binding, dict) or set(binding.keys()) != BINDING_KEYS:
        raise W8UMemoryError("E_BINDING", "binding keys must match closed schema exactly")

    expected_schema = (
        getattr(contract, "binding_schema_id", AUSENTE)
        if not isinstance(contract, dict)
        else contract.get("binding_schema_id", AUSENTE)
    )
    if binding["schema"] != expected_schema:
        raise W8UMemoryError("E_BINDING", "binding schema mismatch: %r != %r" % (binding["schema"], expected_schema))

    for str_key in ("run_id", "lease_id", "container_id", "display"):
        val = binding[str_key]
        if type(val) is not str or not val:
            raise W8UMemoryError("E_BINDING", "binding.%s must be a non-empty string" % str_key)

    for hex_key in ("seat_grant_sha256", "image_sha256", "reader_sha256", "exec_allowlist_sha256"):
        if not _is_hex64(binding[hex_key]):
            raise W8UMemoryError("E_BINDING", "binding.%s must be 64 lowercase hex chars" % hex_key)

    th = binding["target_handle"]
    if not isinstance(th, dict) or set(th.keys()) != TARGET_HANDLE_KEYS:
        raise W8UMemoryError("E_BINDING", "target_handle must have exact keys kind/canonical_id/generation")
    try:
        norm_handle = window_stream_bank.Handle(th["kind"], th["canonical_id"], th["generation"])
    except window_stream_bank.StreamError as exc:
        raise W8UMemoryError("E_BINDING", "invalid target_handle: %s" % exc) from exc

    epoch = _exact_int(binding["epoch"], "epoch", allow_zero=True, max_value=MAX_U64, error_code="E_BINDING")
    _exact_int(binding["xid"], "xid", allow_zero=False, max_value=MAX_U32, error_code="E_BINDING")
    width = _exact_int(binding["width"], "width", allow_zero=False, max_value=MAX_U32, error_code="E_GEOMETRY")
    height = _exact_int(binding["height"], "height", allow_zero=False, max_value=MAX_U32, error_code="E_GEOMETRY")
    max_record_bytes = _exact_int(
        binding["max_record_bytes"], "max_record_bytes", allow_zero=False, max_value=MAX_U64, error_code="E_RAM_QUOTA"
    )
    if max_record_bytes <= FRAME_HEADER_BYTES + 16:
        raise W8UMemoryError("E_RAM_QUOTA", "max_record_bytes too small")
    _exact_int(
        binding["max_inflight_frames"],
        "max_inflight_frames",
        allow_zero=False,
        max_value=MAX_U32,
        error_code="E_RAM_QUOTA",
    )

    if binding["tick_mode"] not in ("committed", "unavailable"):
        raise W8UMemoryError("E_TICK", "tick_mode must be 'committed' or 'unavailable'")

    validate_confined_roots(
        binding["frames_root"],
        binding["frame_run_root"],
        binding["run_id"],
        check_fs_symlinks=check_fs_symlinks,
    )

    if expected_target_handle is not None:
        exp_h = window_stream_bank.Handle(
            expected_target_handle.kind if hasattr(expected_target_handle, "kind") else expected_target_handle["kind"],
            expected_target_handle.canonical_id
            if hasattr(expected_target_handle, "canonical_id")
            else expected_target_handle["canonical_id"],
            expected_target_handle.generation
            if hasattr(expected_target_handle, "generation")
            else expected_target_handle["generation"],
        )
        if norm_handle != exp_h:
            raise W8UMemoryError("E_BINDING", "target_handle mismatch (%r != %r)" % (norm_handle, exp_h))

    if expected_epoch is not None and epoch != expected_epoch:
        raise W8UMemoryError("E_BINDING", "epoch mismatch (%r != %r)" % (epoch, expected_epoch))

    if expected_geometry is not None:
        exp_w, exp_h_dim = expected_geometry
        if (width, height) != (exp_w, exp_h_dim):
            raise W8UMemoryError("E_GEOMETRY", "geometry mismatch ((%d, %d) != (%d, %d))" % (width, height, exp_w, exp_h_dim))

    if not callable(grant_authenticator):
        raise W8UMemoryError("E_BINDING", "grant_authenticator capability is required (%s)" % AUSENTE)
    if grant_authenticator(binding) is not True:
        raise W8UMemoryError("E_BINDING", "seat grant / capability authentication failed independently of digest")

    digest_raw = hashlib.sha256(raw_bytes).digest()
    return binding, raw_bytes, digest_raw


# ---------------------------------------------------------------------------
# W8U XWD Subset Wrapper Validation & Canonical RGB24 Conversion
# ---------------------------------------------------------------------------

