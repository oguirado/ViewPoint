"""W8U cumulative credit controller, reverse control, and frame state machine.

Pure injected-clock Python implementation for owner ``w8u-ve-credits`` with no
sibling W8U imports. Uses only stdlib and landed prerequisite modules
(``vadapt.window_stream_bank`` and ``vadapt.rtring``).
"""
from dataclasses import dataclass
import hashlib
import json
import os
import struct
from typing import Any, Callable
from vadapt import rtring
from vadapt import window_stream_bank

AUSENTE = "AUSENTE"

U16_MAX = (1 << 16) - 1
U32_MAX = (1 << 32) - 1
U64_MAX = (1 << 64) - 1

FRAME_MAGIC = b"W8UF"
FRAME_VERSION = 1
FRAME_HEADER_BYTES = 128
PIXEL_FORMAT_RGB24 = 1
FLAG_PAIR_UNDER_GRAB = 1 << 0
FLAG_TICK_VALID = 1 << 1
ALLOWED_FRAME_FLAGS_MASK = FLAG_PAIR_UNDER_GRAB | FLAG_TICK_VALID

GEOMETRY_OVERHEAD_BYTES = 1 << 20  # 1 MiB
INNER_LENGTH_BYTES = 16  # lenA u64 + lenB u64

CONTROL_MAGIC_HELLO = b"W8UC"
CONTROL_MAGIC_CONFIRM = b"W8UH"
CONTROL_MAGIC_END = b"W8UE"
CONTROL_HEADER_BYTES = 44  # magic(4) + version(2) + kind(2) + body_bytes(4) + binding_sha256(32)
CONTROL_KIND_HELLO = 1
CONTROL_KIND_ACK = 2
CONTROL_KIND_STOP = 3
HELLO_MAX_BODY_BYTES = 16 * 1024
PREFERRED_INITIAL_GRANT = 2

FRAMES_ROOT = "/dev/shm/vqa-ramws"
CONFINED_RUN_PREFIX = FRAMES_ROOT + "/opensuperficies/os-cu-0/"
RECEIPT_SCHEMA = "w8u.receipt/v1"

FRAME_HEADER_STRUCT = struct.Struct(">4sHHQQQQIIIHHQ32s32s")
INNER_LEN_STRUCT = struct.Struct(">Q")
CONTROL_HEADER_STRUCT = struct.Struct(">4sHHI32s")
ACK_BODY_STRUCT = struct.Struct(">QQ")
END_BODY_STRUCT = struct.Struct(">QQI")
XWD_HEADER_STRUCT = struct.Struct(">22IiiI")

CLOSED_ERRORS = (
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


class W8UCreditError(ValueError):
    """Closed W8U error raised on contract, wire, credit, backpressure, or timeout predicates."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status: str = "failed",
        retained_charges: int = 0,
        detail: dict[str, Any] | None = None,
    ):
        if code not in CLOSED_ERRORS:
            raise ValueError(f"unrecognized W8U error code: {code!r}")
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.status = status
        self.retained_charges = retained_charges
        self.detail = dict(detail) if detail else {}


def _require_exact_int(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = U64_MAX,
    err_code: str = "E_BINDING",
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise W8UCreditError(err_code, f"{name} must be an exact integer in [{minimum}, {maximum}]")
    return value


def _require_hex_sha256(value: Any, name: str, *, err_code: str = "E_BINDING") -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise W8UCreditError(err_code, f"{name} must be 64 lowercase hex characters")
    return value


def _require_nonempty_str(value: Any, name: str, *, err_code: str = "E_BINDING") -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise W8UCreditError(err_code, f"{name} must be a non-empty string")
    return value


class InjectedClock:
    """Deterministic injected monotonic clock in nanoseconds (never sleeps)."""

    def __init__(self, initial_ns: int = 0):
        self._now_ns = _require_exact_int(
            initial_ns, "initial_ns", minimum=0, maximum=U64_MAX, err_code="E_TIMEOUT"
        )

    def now_ns(self) -> int:
        return self._now_ns

    def advance_ns(self, delta_ns: int) -> int:
        delta = _require_exact_int(
            delta_ns, "delta_ns", minimum=0, maximum=U64_MAX, err_code="E_TIMEOUT"
        )
        if self._now_ns + delta > U64_MAX:
            raise W8UCreditError("E_TIMEOUT", "monotonic clock u64 overflow")
        self._now_ns += delta
        return self._now_ns

    def set_ns(self, target_ns: int) -> int:
        target = _require_exact_int(
            target_ns, "target_ns", minimum=0, maximum=U64_MAX, err_code="E_TIMEOUT"
        )
        if target < self._now_ns:
            raise W8UCreditError("E_TIMEOUT", "monotonic clock cannot regress")
        self._now_ns = target
        return self._now_ns


@dataclass(frozen=True)
class ContractEvidence:
    """Revision-bound w8u-os-contract authority frozen before execution."""

    revision: str
    accepted: bool
    binding_schema_id: str
    action_result_schema_id: str
    control_specs: dict[tuple[bytes, int], dict[str, Any]]
    end_error_codes: dict[str, int]
    digest_domains: dict[str, bytes]
    native_error_map: dict[str, str]
    xwd_subset: dict[str, Any]
    receipt_closed_fields: dict[str, tuple[str, ...]]
    vectors: dict[str, Any]


def validate_contract_evidence(contract: ContractEvidence | Any) -> ContractEvidence:
    """Verify accepted revision-bound w8u-os-contract evidence; refuse AUSENTE or pending."""
    if contract is None or contract is AUSENTE:
        raise W8UCreditError("E_BINDING", "w8u-os-contract evidence is AUSENTE")
    if not isinstance(contract, ContractEvidence):
        raise W8UCreditError("E_BINDING", "contract must be a ContractEvidence instance")
    if type(contract.accepted) is not bool or not contract.accepted:
        raise W8UCreditError("E_BINDING", "pending contract is PREPARED only; ACCEPT requires accepted=True")
    _require_nonempty_str(contract.revision, "contract.revision")
    _require_nonempty_str(contract.binding_schema_id, "contract.binding_schema_id")
    _require_nonempty_str(contract.action_result_schema_id, "contract.action_result_schema_id")

    if not isinstance(contract.control_specs, dict) or not contract.control_specs:
        raise W8UCreditError("E_BINDING", "contract.control_specs is AUSENTE")
    required_controls = (
        (CONTROL_MAGIC_HELLO, CONTROL_KIND_HELLO),
        (CONTROL_MAGIC_HELLO, CONTROL_KIND_ACK),
        (CONTROL_MAGIC_HELLO, CONTROL_KIND_STOP),
    )
    for key in required_controls:
        if key not in contract.control_specs:
            raise W8UCreditError("E_BINDING", f"missing control spec for {key!r}")
    has_confirm = any(m == CONTROL_MAGIC_CONFIRM for m, _ in contract.control_specs)
    has_end = any(m == CONTROL_MAGIC_END for m, _ in contract.control_specs)
    if not (has_confirm and has_end):
        raise W8UCreditError("E_BINDING", "contract must freeze W8UH confirm and W8UE END specs")

    if not isinstance(contract.end_error_codes, dict):
        raise W8UCreditError("E_BINDING", "contract.end_error_codes is AUSENTE")
    required_end_keys = set(CLOSED_ERRORS) | {"OK"}
    if set(contract.end_error_codes.keys()) != required_end_keys:
        raise W8UCreditError("E_BINDING", "contract.end_error_codes must freeze OK and all CLOSED_ERRORS")
    seen_codes = set()
    for name, num in contract.end_error_codes.items():
        _require_exact_int(num, f"end_error_codes[{name}]", minimum=0, maximum=U32_MAX)
        seen_codes.add(num)
    if len(seen_codes) != len(contract.end_error_codes):
        raise W8UCreditError("E_BINDING", "contract.end_error_codes must be injective")

    if not isinstance(contract.digest_domains, dict):
        raise W8UCreditError("E_BINDING", "contract.digest_domains is AUSENTE")
    for dom in ("ui", "sim", "rgb24", "binding"):
        val = contract.digest_domains.get(dom)
        if not isinstance(val, (bytes, bytearray)) or not val:
            raise W8UCreditError("E_BINDING", f"digest domain {dom!r} is AUSENTE")
    if len(set(bytes(v) for v in contract.digest_domains.values())) != len(contract.digest_domains):
        raise W8UCreditError("E_BINDING", "digest domains must be distinct")

    if not isinstance(contract.native_error_map, dict) or not contract.native_error_map:
        raise W8UCreditError("E_BINDING", "contract.native_error_map is AUSENTE")
    for nat_err, w8u_err in contract.native_error_map.items():
        _require_nonempty_str(nat_err, "native_error_key")
        if w8u_err not in CLOSED_ERRORS:
            raise W8UCreditError("E_BINDING", f"native error maps to unknown code {w8u_err!r}")

    if not isinstance(contract.xwd_subset, dict):
        raise W8UCreditError("E_BINDING", "contract.xwd_subset is AUSENTE")
    required_xwd_keys = {
        "byte_order",
        "bitmap_bit_order",
        "min_header_bytes",
        "max_header_bytes",
        "max_ncolors",
        "allowed_bpp",
        "bitmap_pad_bits",
        "red_mask",
        "green_mask",
        "blue_mask",
    }
    if not required_xwd_keys.issubset(contract.xwd_subset.keys()):
        raise W8UCreditError("E_BINDING", "contract.xwd_subset is incomplete")

    if not isinstance(contract.receipt_closed_fields, dict):
        raise W8UCreditError("E_BINDING", "contract.receipt_closed_fields is AUSENTE")
    required_receipt_sections = (
        "top",
        "identity",
        "populations",
        "frame_summary",
        "foreign",
        "verdict",
        "evidence",
    )
    for sec in required_receipt_sections:
        if sec not in contract.receipt_closed_fields or not contract.receipt_closed_fields[sec]:
            raise W8UCreditError("E_BINDING", f"receipt_closed_fields[{sec!r}] is AUSENTE")

    if not isinstance(contract.vectors, dict):
        raise W8UCreditError("E_BINDING", "contract.vectors is AUSENTE")
    accepted_vecs = contract.vectors.get("accepted")
    rejected_vecs = contract.vectors.get("rejected")
    if not isinstance(accepted_vecs, (list, tuple)) or not accepted_vecs:
        raise W8UCreditError("E_BINDING", "contract.vectors['accepted'] is AUSENTE")
    if not isinstance(rejected_vecs, (list, tuple)) or not rejected_vecs:
        raise W8UCreditError("E_BINDING", "contract.vectors['rejected'] is AUSENTE")
    rejected_kinds = {v.get("kind") for v in rejected_vecs if isinstance(v, dict)}
    if "oversized_header" not in rejected_kinds or "invalid_bitmap_bit_order" not in rejected_kinds:
        raise W8UCreditError(
            "E_BINDING",
            "contract.vectors['rejected'] must include oversized_header and invalid_bitmap_bit_order",
        )
    return contract


def _validate_canonical_json_value(value: Any, path: str = "$") -> None:
    if type(value) is int:
        return
    if type(value) is str:
        if "\x00" in value:
            raise W8UCreditError("E_BINDING", f"NUL byte in string at {path}")
        return
    if isinstance(value, (bool, float)) or value is None:
        raise W8UCreditError("E_BINDING", f"non-integer/bool/float/null forbidden in canonical JSON at {path}")
    if isinstance(value, list):
        for idx, item in enumerate(value):
            _validate_canonical_json_value(item, f"{path}[{idx}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if type(k) is not str:
                raise W8UCreditError("E_BINDING", f"non-string key at {path}")
            _validate_canonical_json_value(v, f"{path}.{k}")
        return
    raise W8UCreditError("E_BINDING", f"unsupported JSON type at {path}: {type(value).__name__}")


def canonical_json_bytes(obj: dict[str, Any]) -> bytes:
    """Serialize dict to sorted compact UTF-8 integer-only JSON bytes without LF."""
    if not isinstance(obj, dict):
        raise W8UCreditError("E_BINDING", "canonical JSON root must be an object")
    _validate_canonical_json_value(obj)
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if "\n" in text or "\r" in text:
        raise W8UCreditError("E_BINDING", "canonical JSON must not contain LF or CR")
    return text.encode("utf-8")


def parse_canonical_binding_json(raw_bytes: bytes) -> dict[str, Any]:
    """Parse and verify canonical compact UTF-8 integer-only JSON bytes without LF."""
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise W8UCreditError("E_WIRE", "HELLO body must be bytes")
    raw = bytes(raw_bytes)
    if len(raw) == 0 or len(raw) > HELLO_MAX_BODY_BYTES:
        raise W8UCreditError("E_LENGTH", f"HELLO body length {len(raw)} out of [1, {HELLO_MAX_BODY_BYTES}]")
    if b"\n" in raw or b"\r" in raw:
        raise W8UCreditError("E_BINDING", "canonical JSON must not contain LF or CR")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise W8UCreditError("E_WIRE", "HELLO body is not valid UTF-8") from exc
    try:
        parsed = json.loads(
            text,
            parse_float=lambda s: (_ for _ in ()).throw(W8UCreditError("E_BINDING", f"float forbidden: {s}")),
        )
    except W8UCreditError:
        raise
    except ValueError as exc:
        raise W8UCreditError("E_WIRE", "invalid JSON in HELLO body") from exc
    if not isinstance(parsed, dict):
        raise W8UCreditError("E_BINDING", "binding JSON must be an object")
    _validate_canonical_json_value(parsed)
    if canonical_json_bytes(parsed) != raw:
        raise W8UCreditError("E_BINDING", "binding JSON is not in sorted compact canonical form")
    return parsed


def validate_roots(
    frames_root: Any, frame_run_root: Any, *, run_id: Any = None
) -> tuple[str, str]:
    """Require one exact campaign child, bound to run_id when available; no aliases or fallback."""
    if type(frames_root) is not str or type(frame_run_root) is not str:
        raise W8UCreditError("E_ROOT", "frames_root and frame_run_root must be strings")
    if "\x00" in frames_root or "\x00" in frame_run_root:
        raise W8UCreditError("E_ROOT", "NUL byte in root path")
    if frames_root != FRAMES_ROOT:
        raise W8UCreditError("E_ROOT", f"frames_root must be exactly {FRAMES_ROOT!r}")
    if not frame_run_root.startswith(CONFINED_RUN_PREFIX):
        raise W8UCreditError(
            "E_ROOT",
            f"frame_run_root must be confined under {CONFINED_RUN_PREFIX!r}",
        )
    if frame_run_root.endswith("/"):
        raise W8UCreditError("E_ROOT", "frame_run_root must not have a trailing slash")
    rel = frame_run_root[len(CONFINED_RUN_PREFIX):]
    if not rel or "/" in rel or "\\" in rel or rel.startswith("."):
        raise W8UCreditError("E_ROOT", "frame_run_root must name one nonempty campaign child")
    if run_id is not None:
        if (type(run_id) is not str or not run_id or "/" in run_id
                or "\\" in run_id or "\x00" in run_id or run_id.startswith(".")):
            raise W8UCreditError("E_ROOT", "run_id must be a single nonempty path component")
        if frame_run_root != CONFINED_RUN_PREFIX + run_id:
            raise W8UCreditError("E_ROOT", "frame_run_root must be the exact campaign child for run_id")
    current = os.path.sep
    for part in frame_run_root.split(os.path.sep)[1:]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise W8UCreditError("E_ROOT", "symlink in root or run path")
    if os.path.realpath(frame_run_root) != frame_run_root:
        raise W8UCreditError("E_ROOT", "frame_run_root must not resolve through an alias")
    return frames_root, frame_run_root


def geometry_cap_bytes(width: int, height: int) -> int:
    """Compute geometry cap: twice the sum of 4*width*height and 1 MiB, plus 16 inner length bytes and 128 header bytes."""
    w = _require_exact_int(width, "width", minimum=1, maximum=U32_MAX, err_code="E_GEOMETRY")
    h = _require_exact_int(height, "height", minimum=1, maximum=U32_MAX, err_code="E_GEOMETRY")
    return 2 * (4 * w * h + GEOMETRY_OVERHEAD_BYTES) + INNER_LENGTH_BYTES + FRAME_HEADER_BYTES


def effective_record_cap(width: int, height: int, max_record_bytes: int) -> int:
    """Return the smaller of negotiated max_record_bytes and geometry_cap_bytes."""
    neg = _require_exact_int(
        max_record_bytes, "max_record_bytes", minimum=1, maximum=U64_MAX, err_code="E_LENGTH"
    )
    return min(neg, geometry_cap_bytes(width, height))


@dataclass(frozen=True)
class ConfirmedBinding:
    """Validated and authenticated W8U session binding."""

    schema: str
    run_id: str
    lease_id: str
    seat_grant_sha256: str
    container_id: str
    target_handle: window_stream_bank.Handle
    epoch: int
    display: str | int
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
    canonical_bytes: bytes
    binding_sha256: bytes
    binding_sha256_hex: str

    @property
    def effective_cap_bytes(self) -> int:
        return effective_record_cap(self.width, self.height, self.max_record_bytes)


def validate_binding(
    binding_input: dict[str, Any] | bytes,
    *,
    contract: ContractEvidence,
    grant_authenticator: Callable[[dict[str, Any]], bool] | None,
    expected_target: window_stream_bank.Handle | Any | None = None,
    expected_epoch: int | None = None,
    expected_geometry: tuple[int, int] | None = None,
) -> ConfirmedBinding:
    """Validate canonical binding structure, roots, geometry, and independent capability grant."""
    contract = validate_contract_evidence(contract)
    if isinstance(binding_input, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(binding_input)
        data = parse_canonical_binding_json(raw_bytes)
    elif isinstance(binding_input, dict):
        raw_bytes = canonical_json_bytes(binding_input)
        if len(raw_bytes) > HELLO_MAX_BODY_BYTES:
            raise W8UCreditError("E_LENGTH", "canonical binding exceeds 16 KiB")
        data = parse_canonical_binding_json(raw_bytes)
    else:
        raise W8UCreditError("E_BINDING", "binding must be canonical bytes or dict")

    if set(data.keys()) != BINDING_KEYS:
        raise W8UCreditError("E_BINDING", "binding keys must match exact closed schema")

    if data["schema"] != contract.binding_schema_id:
        raise W8UCreditError("E_BINDING", f"binding schema must equal {contract.binding_schema_id!r}")

    run_id = _require_nonempty_str(data["run_id"], "run_id")
    lease_id = _require_nonempty_str(data["lease_id"], "lease_id")
    seat_grant_sha256 = _require_hex_sha256(data["seat_grant_sha256"], "seat_grant_sha256")
    container_id = _require_nonempty_str(data["container_id"], "container_id")

    th_raw = data["target_handle"]
    if not isinstance(th_raw, dict) or set(th_raw.keys()) != TARGET_HANDLE_KEYS:
        raise W8UCreditError("E_BINDING", "target_handle must have exact keys kind/canonical_id/generation")
    try:
        handle = window_stream_bank.Handle(
            th_raw["kind"], th_raw["canonical_id"], th_raw["generation"]
        )
    except window_stream_bank.StreamError as exc:
        raise W8UCreditError("E_BINDING", str(exc)) from exc

    epoch = _require_exact_int(data["epoch"], "epoch", minimum=0, maximum=U64_MAX)
    display = data["display"]
    if type(display) is str:
        _require_nonempty_str(display, "display")
    elif type(display) is int:
        _require_exact_int(display, "display", minimum=0, maximum=U32_MAX)
    else:
        raise W8UCreditError("E_BINDING", "display must be str or exact int")

    xid = _require_exact_int(data["xid"], "xid", minimum=1, maximum=U64_MAX)
    width = _require_exact_int(data["width"], "width", minimum=1, maximum=U32_MAX, err_code="E_GEOMETRY")
    height = _require_exact_int(data["height"], "height", minimum=1, maximum=U32_MAX, err_code="E_GEOMETRY")
    if width * 3 > U32_MAX:
        raise W8UCreditError("E_GEOMETRY", "stride width*3 overflows u32")

    frames_root, frame_run_root = validate_roots(
        data["frames_root"], data["frame_run_root"], run_id=run_id
    )
    image_sha256 = _require_hex_sha256(data["image_sha256"], "image_sha256")
    reader_sha256 = _require_hex_sha256(data["reader_sha256"], "reader_sha256")
    exec_allowlist_sha256 = _require_hex_sha256(data["exec_allowlist_sha256"], "exec_allowlist_sha256")

    max_record_bytes = _require_exact_int(
        data["max_record_bytes"],
        "max_record_bytes",
        minimum=FRAME_HEADER_BYTES + INNER_LENGTH_BYTES + 1,
        maximum=U64_MAX,
        err_code="E_LENGTH",
    )
    max_inflight_frames = _require_exact_int(
        data["max_inflight_frames"],
        "max_inflight_frames",
        minimum=1,
        maximum=U32_MAX,
        err_code="E_CREDIT",
    )

    tick_mode = data["tick_mode"]
    if tick_mode not in ("committed", "unavailable"):
        raise W8UCreditError("E_TICK", f"invalid tick_mode: {tick_mode!r}")

    if expected_target is not None:
        try:
            exp_h = window_stream_bank.Handle(
                expected_target.kind, expected_target.canonical_id, expected_target.generation
            )
        except AttributeError as exc:
            raise W8UCreditError("E_BINDING", "invalid expected_target") from exc
        if handle != exp_h:
            raise W8UCreditError("E_BINDING", "target_handle/generation mismatch")

    if expected_epoch is not None:
        if epoch != expected_epoch:
            raise W8UCreditError("E_BINDING", "binding epoch mismatch")

    if expected_geometry is not None:
        if (width, height) != tuple(expected_geometry):
            raise W8UCreditError("E_GEOMETRY", "binding geometry mismatch")

    if grant_authenticator is None or grant_authenticator is AUSENTE or not callable(grant_authenticator):
        raise W8UCreditError("E_BINDING", "grant_authenticator capability is AUSENTE")
    if grant_authenticator(dict(data)) is not True:
        raise W8UCreditError("E_BINDING", "independent seat grant / capability authentication failed")

    domain_prefix = bytes(contract.digest_domains["binding"])
    binding_digest = hashlib.sha256(domain_prefix + raw_bytes).digest()

    return ConfirmedBinding(
        schema=data["schema"],
        run_id=run_id,
        lease_id=lease_id,
        seat_grant_sha256=seat_grant_sha256,
        container_id=container_id,
        target_handle=handle,
        epoch=epoch,
        display=display,
        xid=xid,
        width=width,
        height=height,
        frames_root=frames_root,
        frame_run_root=frame_run_root,
        image_sha256=image_sha256,
        reader_sha256=reader_sha256,
        exec_allowlist_sha256=exec_allowlist_sha256,
        max_record_bytes=max_record_bytes,
        max_inflight_frames=max_inflight_frames,
        tick_mode=tick_mode,
        canonical_bytes=raw_bytes,
        binding_sha256=binding_digest,
        binding_sha256_hex=binding_digest.hex(),
    )


