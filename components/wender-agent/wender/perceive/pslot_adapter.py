# @module {"id":"wa.perceive.pslot_adapter","layer":1,"owner":"PERCEIVE","deps":["wa.state.schema","wa.perceive.session_registry"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.pslot_adapter - perception-provider adapter and bound evidence sidecar.

Converts validated perception-provider elements into closed `wender.state/v1` nodes
with `source.kind == "vision"`, while binding immutable provider evidence (confidence,
provider identity/version, device/CSS boxes, and frame digest) to the exact `FrameRef`
digest, `Handle`, `epoch`, `frame_seq`, and `dpr_ppm` in a detached sidecar.

Invariants enforced:
- Closed STATE and NODE_KEYS are preserved (`wender/state/schema.py`); confidence and
  frame digest never enter STATE or node dicts.
- Structured CDP/AX, AT-SPI, Godot T2, and injected bpy evidence remain primary.
- Integer-only geometry and confidence_ppm (rejecting bool, float, negative or
  out-of-bounds boxes, and out-of-range confidence).
- Exact frame SHA-256, handle, epoch, frame_seq, and dpr_ppm provenance matching.
- Ambiguous element identities (duplicate keys, colliding node IDs, duplicate
  role+name+box under the same parent, invalid parent order) are refused.
- Network-first model role matrix with explicit AUSENTE sentinel for unconfigured or
  unknown capabilities, limits, and tariffs (absence is never zero) and quota-floor
  refusal before transport for the out-of-loop judge.
"""

from __future__ import annotations

import abc
import copy
from dataclasses import dataclass
import hashlib
import re
import struct
import threading
import unicodedata
import zlib

import numpy as np

from wender.perceive import session_registry
from wender.state import schema

Handle = session_registry.Handle
FrameRef = session_registry.FrameRef
Observation = session_registry.Observation

AUSENTE = "AUSENTE"

VISION_SOURCE_KIND = "vision"
PRIMARY_EVIDENCE_KINDS = ("cdp", "atspi", "godot", "bpy")
PRUNE_VISION_NAME = "prune_vision_pslot_v1"
PRUNE_VISION_VERSION = 1
MAX_NAME_BYTES = 64
CONFIDENCE_PPM_MAX = 1_000_000
SIDECAR_SCHEMA_ID = "wender.pslot.sidecar/v1"
SIDECAR_BINARY_MAGIC = b"WAPS\x00\x01"

DEFAULT_PERCEIVER_MODEL = "Meta Muse Spark 1.3"
ALT_PERCEIVER_MODEL = "Gemini 3.8 Flash"
OUT_OF_LOOP_JUDGE_MODEL = "Gemini 4 Argon"
UNCONFIGURED_MODEL = "DeepSeek V4.1 Flash"

NETWORK_ROLE_DEFAULT_PERCEIVER = "default_perceiver"
NETWORK_ROLE_ALT_PERCEIVER = "alternative_perceiver"
NETWORK_ROLE_OUT_OF_LOOP_JUDGE = "out_of_loop_judge"
NETWORK_ROLE_NOT_CONFIGURED = "not_configured"

CAPABILITY_MATRIX = {
    DEFAULT_PERCEIVER_MODEL: {
        "model_id": DEFAULT_PERCEIVER_MODEL,
        "role": NETWORK_ROLE_DEFAULT_PERCEIVER,
        "configured": True,
        "in_loop_perceiver": True,
        "quota_floor_refusal_before_transport": False,
        "capabilities": AUSENTE,
        "limits": AUSENTE,
        "tariffs": AUSENTE,
        "quota_floor_ppm": AUSENTE,
    },
    ALT_PERCEIVER_MODEL: {
        "model_id": ALT_PERCEIVER_MODEL,
        "role": NETWORK_ROLE_ALT_PERCEIVER,
        "configured": True,
        "in_loop_perceiver": True,
        "quota_floor_refusal_before_transport": False,
        "capabilities": AUSENTE,
        "limits": AUSENTE,
        "tariffs": AUSENTE,
        "quota_floor_ppm": AUSENTE,
    },
    OUT_OF_LOOP_JUDGE_MODEL: {
        "model_id": OUT_OF_LOOP_JUDGE_MODEL,
        "role": NETWORK_ROLE_OUT_OF_LOOP_JUDGE,
        "configured": True,
        "in_loop_perceiver": False,
        "quota_floor_refusal_before_transport": True,
        "capabilities": AUSENTE,
        "limits": AUSENTE,
        "tariffs": AUSENTE,
        "quota_floor_ppm": AUSENTE,
    },
    UNCONFIGURED_MODEL: {
        "model_id": UNCONFIGURED_MODEL,
        "role": NETWORK_ROLE_NOT_CONFIGURED,
        "configured": False,
        "in_loop_perceiver": False,
        "quota_floor_refusal_before_transport": False,
        "capabilities": AUSENTE,
        "limits": AUSENTE,
        "tariffs": AUSENTE,
        "quota_floor_ppm": AUSENTE,
    },
}

ALLOWED_VISION_ROLES = frozenset((
    "RootWebArea", "main", "navigation", "banner", "contentinfo", "complementary",
    "region", "search", "form", "dialog", "alertdialog", "toolbar",
    "menu", "menubar", "menuitem", "menuitemcheckbox", "menuitemradio",
    "tablist", "tab", "tabpanel", "heading",
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option",
    "checkbox", "radio", "switch", "slider", "spinbutton",
    "status", "alert", "log", "progressbar", "timer", "marquee",
    "article", "note", "group", "application",
    "window", "panel", "label", "icon", "image", "canvas", "treeitem", "listitem",
))

ACTIONABLE_VISION_ROLES = frozenset((
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option",
    "checkbox", "radio", "switch", "slider", "spinbutton", "menuitem",
    "menuitemcheckbox", "menuitemradio", "tab",
))

ALLOWED_STATE_ATTRS = frozenset((
    "busy", "checked", "current", "disabled",
    "expanded", "hidden", "pressed", "selected",
))

_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_NODE_ID_RE = re.compile(r"^[0-9a-f]{16}\Z")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PslotAdapterError(ValueError):
    """Base refusal error for the PSLOT perception-provider adapter."""


class ProviderContractRefusalError(PslotAdapterError):
    """Refusal when provider contract, identity, capability, or quota is invalid or missing."""


class ProviderIdentityError(ProviderContractRefusalError):
    """Refusal when provider identity, version, or role binding is invalid."""


class ProviderCapabilityRefusalError(ProviderContractRefusalError):
    """Refusal when a model is unconfigured or capabilities/limits/tariffs are AUSENTE."""


class QuotaFloorRefusalError(ProviderContractRefusalError):
    """Refusal before transport when quota is below the configured quota floor."""


class FrameBindingRefusalError(PslotAdapterError):
    """Refusal when frame digest, handle, epoch, sequence, or DPR does not match."""


class GeometryRefusalError(PslotAdapterError):
    """Refusal when element geometry uses bool/non-int types or is out of bounds."""


class ElementValidationRefusalError(PslotAdapterError):
    """Refusal when element role, text, states, or confidence is invalid."""


class AmbiguousElementRefusalError(PslotAdapterError):
    """Refusal when element identities, coordinates, or parent links are ambiguous."""


class PrimaryEvidenceOverrideRefusalError(PslotAdapterError):
    """Refusal when attempting to override primary structured evidence (CDP/AX, AT-SPI, Godot T2, bpy)."""


def _is_strict_int(value: object) -> bool:
    return type(value) is int


def _require_strict_int(value: object, name: str, *, minimum: int | None = None, maximum: int | None = None,
                        error_cls: type[Exception] = PslotAdapterError) -> int:
    if type(value) is not int:
        raise error_cls(f"{name} must be a strict int (not {type(value).__name__})")
    if minimum is not None and value < minimum:
        raise error_cls(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise error_cls(f"{name} must be <= {maximum}, got {value}")
    return value


def _require_canonical_token(value: object, name: str, *, error_cls: type[Exception] = PslotAdapterError) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise error_cls(f"{name} must be a non-empty canonical string")
    if value == AUSENTE:
        raise error_cls(f"{name} is {AUSENTE} and cannot be admitted")
    if _CONTROL_CHAR_RE.search(value) or "\n" in value or "\r" in value or "\t" in value:
        raise error_cls(f"{name} contains forbidden control characters")
    return value


def _coerce_handle(value: object) -> Handle:
    if isinstance(value, Handle):
        return value
    try:
        if isinstance(value, dict):
            if sorted(value.keys()) != ["canonical_id", "generation", "kind"]:
                raise FrameBindingRefusalError("handle dict must have exact keys kind, canonical_id, generation")
            return Handle(value["kind"], value["canonical_id"], value["generation"])
        return Handle(value.kind, value.canonical_id, value.generation)
    except (AttributeError, KeyError, TypeError, session_registry.RegistryError) as exc:
        raise FrameBindingRefusalError(f"invalid target handle: {exc}") from exc


# @anchor:wa.perceive.pslot_adapter.normalize_vision_text
def normalize_vision_text(raw: object) -> str:
    """Validate and normalize provider element text into bounded canonical UTF-8."""
    if not isinstance(raw, str):
        raise ElementValidationRefusalError(f"element text must be str, got {type(raw).__name__}")
    if _CONTROL_CHAR_RE.search(raw):
        raise ElementValidationRefusalError("element text contains forbidden control characters or NUL")
    if not raw:
        return ""
    norm = unicodedata.normalize("NFC", raw)
    norm = re.sub(r"\s+", " ", norm).strip()
    norm = norm.replace("\\", "\\\\").replace("|", "\\|")
    encoded = norm.encode("utf-8")
    if len(encoded) > MAX_NAME_BYTES:
        norm = encoded[:MAX_NAME_BYTES].decode("utf-8", "ignore")
        if (len(norm) - len(norm.rstrip("\\"))) % 2:
            norm = norm[:-1]
    return norm


# @anchor:wa.perceive.pslot_adapter.ProviderIdentity
@dataclass(frozen=True)
class ProviderIdentity:
    """Validated perception provider identity, version, and network-first model binding."""

    provider_id: str
    provider_version: str
    model_id: str = DEFAULT_PERCEIVER_MODEL
    role: str = NETWORK_ROLE_DEFAULT_PERCEIVER

    def __post_init__(self) -> None:
        _require_canonical_token(self.provider_id, "provider_id", error_cls=ProviderIdentityError)
        _require_canonical_token(self.provider_version, "provider_version", error_cls=ProviderIdentityError)
        _require_canonical_token(self.model_id, "model_id", error_cls=ProviderIdentityError)
        _require_canonical_token(self.role, "role", error_cls=ProviderIdentityError)
        spec = CAPABILITY_MATRIX.get(self.model_id)
        if spec is None:
            raise ProviderCapabilityRefusalError(f"unknown model_id {self.model_id!r}: capability is {AUSENTE}")
        if not spec["configured"]:
            raise ProviderCapabilityRefusalError(f"model {self.model_id!r} is not configured in capability matrix")
        if self.role != spec["role"]:
            raise ProviderIdentityError(
                f"model {self.model_id!r} requires role {spec['role']!r}, got {self.role!r}"
            )

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "model_id": self.model_id,
            "role": self.role,
        }


# @anchor:wa.perceive.pslot_adapter.LocalHttpSeamConfig
@dataclass(frozen=True)
class LocalHttpSeamConfig:
    """Future design seam for a configured local HTTP interface with capped-unit hooks.

    Carries no evaluation dependency and performs no network or socket I/O.
    """

    endpoint: str = AUSENTE
    max_units: int | str = AUSENTE
    on_demand_hook: object = None
    enabled: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ProviderContractRefusalError("LocalHttpSeamConfig.enabled must be bool")
        if self.enabled:
            _require_canonical_token(self.endpoint, "endpoint", error_cls=ProviderContractRefusalError)
            if not (self.endpoint.startswith("http://127.0.0.1:") or self.endpoint.startswith("http://localhost:")):
                raise ProviderContractRefusalError("local HTTP seam endpoint must be loopback http://127.0.0.1 or localhost")
            _require_strict_int(self.max_units, "max_units", minimum=1, error_cls=ProviderContractRefusalError)
            if self.on_demand_hook is not None and not callable(self.on_demand_hook):
                raise ProviderContractRefusalError("on_demand_hook must be callable when provided")
        else:
            if self.max_units != AUSENTE:
                _require_strict_int(self.max_units, "max_units", minimum=1, error_cls=ProviderContractRefusalError)

    def invoke_capped_hook(self, requested_units: int) -> int:
        if not self.enabled:
            raise ProviderCapabilityRefusalError("local HTTP seam is not enabled")
        _require_strict_int(requested_units, "requested_units", minimum=1, error_cls=ProviderContractRefusalError)
        if type(self.max_units) is not int or requested_units > self.max_units:
            raise QuotaFloorRefusalError(f"requested_units {requested_units} exceeds capped max_units {self.max_units}")
        if self.on_demand_hook is not None:
            result = self.on_demand_hook(requested_units)
            return _require_strict_int(result, "hook_result", minimum=0, maximum=self.max_units,
                                       error_cls=ProviderContractRefusalError)
        return requested_units


# @anchor:wa.perceive.pslot_adapter.resolve_provider_capability
def resolve_provider_capability(model_id: str, *, operator_config: dict | None = None,
                                require_operational_limits: bool = False) -> dict:
    """Resolve model capability entry from CAPABILITY_MATRIX and explicit operator config.

    Unknown capabilities, limits, and tariffs remain AUSENTE until explicitly configured
    by the operator; absence is never coerced to zero.
    """
    _require_canonical_token(model_id, "model_id", error_cls=ProviderIdentityError)
    base = CAPABILITY_MATRIX.get(model_id)
    if base is None:
        raise ProviderCapabilityRefusalError(f"model {model_id!r} is unknown ({AUSENTE})")
    if not base["configured"]:
        raise ProviderCapabilityRefusalError(f"model {model_id!r} is not configured in capability matrix")

    resolved = copy.deepcopy(base)
    if operator_config is not None:
        if not isinstance(operator_config, dict):
            raise ProviderCapabilityRefusalError("operator_config must be a dict")
        for field in ("capabilities", "limits", "tariffs", "quota_floor_ppm"):
            if field in operator_config:
                val = operator_config[field]
                if val is None or val == AUSENTE:
                    raise ProviderCapabilityRefusalError(f"operator_config.{field} cannot be None or {AUSENTE}")
                if field == "quota_floor_ppm":
                    _require_strict_int(val, "quota_floor_ppm", minimum=1, maximum=CONFIDENCE_PPM_MAX,
                                        error_cls=ProviderCapabilityRefusalError)
                elif isinstance(val, dict):
                    if not val:
                        raise ProviderCapabilityRefusalError(f"operator_config.{field} must not be empty")
                    for k, v in val.items():
                        _require_canonical_token(k, f"{field}.key", error_cls=ProviderCapabilityRefusalError)
                        _require_strict_int(v, f"{field}.{k}", minimum=1, error_cls=ProviderCapabilityRefusalError)
                else:
                    raise ProviderCapabilityRefusalError(f"operator_config.{field} must be a non-empty dict")
                resolved[field] = copy.deepcopy(val)

    if require_operational_limits:
        for field in ("capabilities", "limits", "tariffs"):
            if resolved[field] == AUSENTE:
                raise ProviderCapabilityRefusalError(
                    f"model {model_id!r} field {field!r} is {AUSENTE}; explicit operator configuration required"
                )
        if resolved["quota_floor_refusal_before_transport"] and resolved["quota_floor_ppm"] == AUSENTE:
            raise ProviderCapabilityRefusalError(
                f"model {model_id!r} requires explicit quota_floor_ppm configuration (got {AUSENTE})"
            )
    return resolved


# @anchor:wa.perceive.pslot_adapter.check_judge_quota_floor
def check_judge_quota_floor(model_id: str, *, remaining_quota_ppm: object,
                            operator_config: dict | None = None,
                            transport: object = None) -> dict:
    """Enforce quota-floor refusal before transport for out-of-loop judge Gemini 4 Argon."""
    resolved = resolve_provider_capability(model_id, operator_config=operator_config, require_operational_limits=False)
    if resolved["role"] != NETWORK_ROLE_OUT_OF_LOOP_JUDGE:
        raise ProviderIdentityError(f"model {model_id!r} is not an out-of-loop judge")
    floor = resolved.get("quota_floor_ppm", AUSENTE)
    if floor == AUSENTE:
        raise QuotaFloorRefusalError(f"quota_floor_ppm for {model_id!r} is {AUSENTE}; refusal before transport")
    rem = _require_strict_int(
        remaining_quota_ppm,
        "remaining_quota_ppm",
        minimum=0,
        maximum=CONFIDENCE_PPM_MAX,
        error_cls=QuotaFloorRefusalError,
    )
    if rem < floor:
        raise QuotaFloorRefusalError(
            f"remaining_quota_ppm {rem} is below quota_floor_ppm {floor} for {model_id!r}; refused before transport"
        )
    return {
        "model_id": model_id,
        "role": resolved["role"],
        "remaining_quota_ppm": rem,
        "quota_floor_ppm": floor,
        "transport_touched": False if transport is None else getattr(transport, "call_count", 0) > 0,
    }


# @anchor:wa.perceive.pslot_adapter.ProviderElement
@dataclass(frozen=True)
class ProviderElement:
    """Validated perception-provider element prior to STATE node conversion."""

    element_key: str
    role: str
    text: str
    box: tuple[int, int, int, int]
    confidence_ppm: int
    parent_key: str | None = None
    states: tuple[tuple[str, str], ...] = ()
    explicit_node_id: str | None = None

    def __post_init__(self) -> None:
        _require_canonical_token(self.element_key, "element_key", error_cls=ElementValidationRefusalError)
        _require_canonical_token(self.role, "role", error_cls=ElementValidationRefusalError)
        if "|" in self.role or "\\" in self.role:
            raise ElementValidationRefusalError("role must not contain pipe or backslash characters")
        norm_text = normalize_vision_text(self.text)
        object.__setattr__(self, "text", norm_text)

        if not isinstance(self.box, (tuple, list)) or len(self.box) != 4:
            raise GeometryRefusalError("box must be a 4-tuple/list of (x, y, w, h) integers")
        x, y, w, h = self.box
        for coord_name, coord_val in (("x", x), ("y", y), ("w", w), ("h", h)):
            if type(coord_val) is not int:
                raise GeometryRefusalError(
                    f"box.{coord_name} must be a strict int, got {type(coord_val).__name__}"
                )
        if x < 0 or y < 0:
            raise GeometryRefusalError(f"box origin must be non-negative, got ({x}, {y})")
        if w <= 0 or h <= 0:
            raise GeometryRefusalError(f"box width and height must be positive (> 0), got ({w}, {h})")
        object.__setattr__(self, "box", (x, y, w, h))

        _require_strict_int(
            self.confidence_ppm,
            "confidence_ppm",
            minimum=0,
            maximum=CONFIDENCE_PPM_MAX,
            error_cls=ElementValidationRefusalError,
        )

        if self.parent_key is not None:
            _require_canonical_token(self.parent_key, "parent_key", error_cls=AmbiguousElementRefusalError)
            if self.parent_key == self.element_key:
                raise AmbiguousElementRefusalError(f"element {self.element_key!r} cannot be its own parent")

        if isinstance(self.states, dict):
            raw_items = sorted(self.states.items())
        elif isinstance(self.states, (tuple, list)):
            raw_items = list(self.states)
        else:
            raise ElementValidationRefusalError("states must be a dict or sequence of (key, value) pairs")

        normalized_states = []
        seen_state_keys = set()
        for item in raw_items:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ElementValidationRefusalError("each state entry must be a (key, value) pair")
            sk, sv = item
            if not isinstance(sk, str) or sk not in ALLOWED_STATE_ATTRS:
                raise ElementValidationRefusalError(f"unsupported state attribute {sk!r}")
            if sk in seen_state_keys:
                raise ElementValidationRefusalError(f"duplicate state attribute {sk!r}")
            if not isinstance(sv, str) or not sv or _CONTROL_CHAR_RE.search(sv) or "," in sv or "=" in sv or "|" in sv:
                raise ElementValidationRefusalError(f"invalid state value for {sk!r}: must be non-empty safe str")
            seen_state_keys.add(sk)
            normalized_states.append((sk, sv))
        normalized_states.sort(key=lambda pair: pair[0])
        object.__setattr__(self, "states", tuple(normalized_states))

        if self.explicit_node_id is not None:
            if not isinstance(self.explicit_node_id, str) or not _NODE_ID_RE.match(self.explicit_node_id):
                raise AmbiguousElementRefusalError("explicit_node_id must be 16 lowercase hex characters")


# @anchor:wa.perceive.pslot_adapter.ProviderResponse
@dataclass(frozen=True)
class ProviderResponse:
    """Immutable response envelope returned by an injected perception provider."""

    provider: ProviderIdentity
    frame_sha256: str
    handle: Handle
    epoch: int
    frame_seq: int
    elements: tuple
    dpr_ppm: int = 1_000_000
    url: str = ""
    title: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provider, ProviderIdentity):
            raise ProviderIdentityError("provider must be a ProviderIdentity instance")
        if not isinstance(self.frame_sha256, str) or not _SHA256_RE.match(self.frame_sha256):
            raise FrameBindingRefusalError("frame_sha256 must be 64 lowercase hex characters")
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        _require_strict_int(self.epoch, "epoch", minimum=0, error_cls=FrameBindingRefusalError)
        _require_strict_int(self.frame_seq, "frame_seq", minimum=0, error_cls=FrameBindingRefusalError)
        _require_strict_int(self.dpr_ppm, "dpr_ppm", minimum=1, error_cls=FrameBindingRefusalError)
        if not isinstance(self.url, str) or _CONTROL_CHAR_RE.search(self.url):
            raise ElementValidationRefusalError("url must be a valid string without control characters")
        if not isinstance(self.title, str) or _CONTROL_CHAR_RE.search(self.title):
            raise ElementValidationRefusalError("title must be a valid string without control characters")
        if not isinstance(self.elements, (tuple, list)):
            raise ElementValidationRefusalError("elements must be a tuple or list")
        object.__setattr__(self, "elements", tuple(self.elements))


# @anchor:wa.perceive.pslot_adapter.PerceptionProviderContract
class PerceptionProviderContract(abc.ABC):
    """Injected perception-provider contract for PSLOT Wave 1."""

    @abc.abstractmethod
    def identify(self) -> ProviderIdentity:
        """Return the validated ProviderIdentity for this provider."""
        raise NotImplementedError

    @abc.abstractmethod
    def perceive_frame(self, frame: FrameRef, *, dpr_ppm: int = 1_000_000,
                       url: str = "", title: str = "") -> ProviderResponse:
        """Perceive an in-RAM FrameRef and return a bound ProviderResponse."""
        raise NotImplementedError


# @anchor:wa.perceive.pslot_adapter.DeterministicFakeTransport
class DeterministicFakeTransport:
    """In-RAM deterministic fake transport with no network, sockets, or media persistence.

    Records only metadata digests (never RGB bytes) and supports optional concurrency
    hooks and quota-floor refusal before transport.
    """

    def __init__(self, *, operator_config: dict | None = None,
                 remaining_quota_ppm: int | str = CONFIDENCE_PPM_MAX,
                 on_send_hook=None) -> None:
        self._lock = threading.Lock()
        self._operator_config = copy.deepcopy(operator_config) if operator_config is not None else None
        self._remaining_quota_ppm = remaining_quota_ppm
        self._on_send_hook = on_send_hook
        self._calls: list[dict] = []

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self._calls)

    def calls_snapshot(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._calls)

    def dispatch(self, provider: ProviderIdentity, frame: FrameRef, *, dpr_ppm: int) -> dict:
        if not isinstance(provider, ProviderIdentity):
            raise ProviderIdentityError("transport dispatch requires ProviderIdentity")
        if not isinstance(frame, FrameRef):
            raise FrameBindingRefusalError("transport dispatch requires FrameRef")
        _require_strict_int(dpr_ppm, "dpr_ppm", minimum=1, error_cls=FrameBindingRefusalError)

        spec = CAPABILITY_MATRIX.get(provider.model_id)
        if spec is None or not spec["configured"]:
            raise ProviderCapabilityRefusalError(f"model {provider.model_id!r} is not configured for transport")
        if spec["quota_floor_refusal_before_transport"]:
            check_judge_quota_floor(
                provider.model_id,
                remaining_quota_ppm=self._remaining_quota_ppm,
                operator_config=self._operator_config,
            )

        if self._on_send_hook is not None:
            self._on_send_hook(provider, frame)

        receipt = {
            "provider_id": provider.provider_id,
            "provider_version": provider.provider_version,
            "model_id": provider.model_id,
            "role": provider.role,
            "handle": {
                "kind": frame.handle.kind,
                "canonical_id": frame.handle.canonical_id,
                "generation": frame.handle.generation,
            },
            "epoch": frame.epoch,
            "frame_seq": frame.frame_seq,
            "frame_sha256": frame.digest,
            "width": frame.width,
            "height": frame.height,
            "dpr_ppm": dpr_ppm,
        }
        schema.dumps(receipt)
        with self._lock:
            self._calls.append(receipt)
        return copy.deepcopy(receipt)


# @anchor:wa.perceive.pslot_adapter.DeterministicFakeProvider
class DeterministicFakeProvider(PerceptionProviderContract):
    """Deterministic in-memory fake perception provider for PSLOT unit and property tests."""

    def __init__(self, identity: ProviderIdentity | None = None, *,
                 elements: tuple | list = (),
                 transport: DeterministicFakeTransport | None = None,
                 override_frame_sha256: str | None = None,
                 override_handle: Handle | None = None,
                 override_epoch: int | None = None,
                 override_frame_seq: int | None = None,
                 override_dpr_ppm: int | None = None) -> None:
        if identity is None:
            identity = ProviderIdentity(
                provider_id="fake-muse-spark",
                provider_version="1.3.0",
                model_id=DEFAULT_PERCEIVER_MODEL,
                role=NETWORK_ROLE_DEFAULT_PERCEIVER,
            )
        if not isinstance(identity, ProviderIdentity):
            raise ProviderIdentityError("identity must be a ProviderIdentity")
        if identity.role == NETWORK_ROLE_OUT_OF_LOOP_JUDGE:
            raise ProviderIdentityError("out-of-loop judge cannot act as an in-loop perception provider")
        self._identity = identity
        self._elements = tuple(copy.deepcopy(elements))
        self._transport = transport if transport is not None else DeterministicFakeTransport()
        self._override_frame_sha256 = override_frame_sha256
        self._override_handle = override_handle
        self._override_epoch = override_epoch
        self._override_frame_seq = override_frame_seq
        self._override_dpr_ppm = override_dpr_ppm

    @property
    def transport(self) -> DeterministicFakeTransport:
        return self._transport

    def identify(self) -> ProviderIdentity:
        return self._identity

    def perceive_frame(self, frame: FrameRef, *, dpr_ppm: int = 1_000_000,
                       url: str = "", title: str = "") -> ProviderResponse:
        if not isinstance(frame, FrameRef):
            raise FrameBindingRefusalError("frame must be a FrameRef")
        self._transport.dispatch(self._identity, frame, dpr_ppm=dpr_ppm)
        return ProviderResponse(
            provider=self._identity,
            frame_sha256=self._override_frame_sha256 if self._override_frame_sha256 is not None else frame.digest,
            handle=self._override_handle if self._override_handle is not None else frame.handle,
            epoch=self._override_epoch if self._override_epoch is not None else frame.epoch,
            frame_seq=self._override_frame_seq if self._override_frame_seq is not None else frame.frame_seq,
            elements=copy.deepcopy(self._elements),
            dpr_ppm=self._override_dpr_ppm if self._override_dpr_ppm is not None else dpr_ppm,
            url=url,
            title=title,
        )


def _coerce_provider_element(raw: object) -> ProviderElement:
    if isinstance(raw, ProviderElement):
        return raw
    if not isinstance(raw, dict):
        raise ElementValidationRefusalError(f"provider element must be ProviderElement or dict, got {type(raw).__name__}")

    has_key = "element_key" in raw
    has_id = "id" in raw
    if has_key and has_id and raw["element_key"] != raw["id"]:
        raise AmbiguousElementRefusalError(
            f"conflicting element_key {raw['element_key']!r} and id {raw['id']!r}"
        )
    element_key = raw.get("element_key", raw.get("id"))
    if element_key is None:
        raise ElementValidationRefusalError("provider element is missing element_key/id")

    if "role" not in raw:
        raise ElementValidationRefusalError("provider element is missing role")
    role = raw["role"]

    has_text = "text" in raw
    has_name = "name" in raw
    if has_text and has_name and raw["text"] != raw["name"]:
        raise AmbiguousElementRefusalError(
            f"conflicting text {raw['text']!r} and name {raw['name']!r} on element {element_key!r}"
        )
    text = raw.get("text", raw.get("name", ""))

    if "box" not in raw:
        raise GeometryRefusalError(f"provider element {element_key!r} is missing box")
    box = raw["box"]

    has_conf_ppm = "confidence_ppm" in raw
    has_conf = "confidence" in raw
    if not has_conf_ppm and not has_conf:
        raise ElementValidationRefusalError(f"provider element {element_key!r} is missing confidence_ppm")
    if has_conf_ppm and has_conf and raw["confidence_ppm"] != raw["confidence"]:
        raise ElementValidationRefusalError(
            f"conflicting confidence_ppm and confidence on element {element_key!r}"
        )
    confidence_ppm = raw["confidence_ppm"] if has_conf_ppm else raw["confidence"]

    parent_key = raw.get("parent_key", raw.get("parent"))
    states = raw.get("states", ())
    explicit_node_id = raw.get("explicit_node_id", raw.get("node_id"))

    return ProviderElement(
        element_key=element_key,
        role=role,
        text=text,
        box=box,
        confidence_ppm=confidence_ppm,
        parent_key=parent_key,
        states=states,
        explicit_node_id=explicit_node_id,
    )


def _attrs_txt(attrs: tuple[tuple[str, str], ...]) -> str:
    return ",".join(f"{k}={v}" for k, v in attrs)


def _canonical_line(depth: int, role: str, name: str, attrs: tuple[tuple[str, str], ...], *, with_state: bool) -> str:
    return f"{depth}|{role}|{name}|{_attrs_txt(attrs) if with_state else ''}"


def _snapshot_sha(nodes: list[dict], *, with_state: bool) -> str:
    if not nodes:
        blob = ""
    else:
        blob = "\n".join(
            _canonical_line(n["depth"], n["role"], n["name"], n["attrs"], with_state=with_state)
            for n in nodes
        ) + "\n"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _vision_node_id(ancestors: tuple[str, ...], role: str, name: str, ordinal: int) -> str:
    base = "/".join(ancestors) + "\0" + role + "\0" + name + "\0"
    fp_stable = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    return hashlib.sha256(f"{fp_stable}\0{ordinal}".encode("utf-8")).hexdigest()[:16]


def _device_box_to_css_box(box: tuple[int, int, int, int], dpr_ppm: int) -> list[int]:
    x, y, w, h = box
    if dpr_ppm == 1_000_000:
        return [x, y, w, h]
    cx0 = (x * 1_000_000) // dpr_ppm
    cy0 = (y * 1_000_000) // dpr_ppm
    cx1 = ((x + w) * 1_000_000) // dpr_ppm
    cy1 = ((y + h) * 1_000_000) // dpr_ppm
    cw = max(1, cx1 - cx0)
    ch = max(1, cy1 - cy0)
    return [cx0, cy0, cw, ch]


# @anchor:wa.perceive.pslot_adapter.EvidenceSidecar
@dataclass(frozen=True)
class EvidenceSidecar:
    """Detached, immutable sidecar binding provider evidence to exact frame and target provenance."""

    schema: str
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    frame_sha256: str
    state_ax_sha256: str
    state_ax_stable_sha256: str
    provider: ProviderIdentity
    node_evidence: tuple[dict, ...]
    packed_zlib: bytes

    def __post_init__(self) -> None:
        if self.schema != SIDECAR_SCHEMA_ID:
            raise FrameBindingRefusalError(f"sidecar schema must be {SIDECAR_SCHEMA_ID!r}")
        object.__setattr__(self, "handle", _coerce_handle(self.handle))
        _require_strict_int(self.epoch, "epoch", minimum=0, error_cls=FrameBindingRefusalError)
        _require_strict_int(self.frame_seq, "frame_seq", minimum=0, error_cls=FrameBindingRefusalError)
        _require_strict_int(self.dpr_ppm, "dpr_ppm", minimum=1, error_cls=FrameBindingRefusalError)
        for field_name in ("frame_sha256", "state_ax_sha256", "state_ax_stable_sha256"):
            val = getattr(self, field_name)
            if not isinstance(val, str) or not _SHA256_RE.match(val):
                raise FrameBindingRefusalError(f"sidecar {field_name} must be 64 lowercase hex characters")
        if not isinstance(self.provider, ProviderIdentity):
            raise ProviderIdentityError("sidecar provider must be ProviderIdentity")
        if not isinstance(self.node_evidence, (tuple, list)):
            raise ElementValidationRefusalError("node_evidence must be a tuple or list")
        frozen_ev = tuple(copy.deepcopy(entry) for entry in self.node_evidence)
        for entry in frozen_ev:
            schema.dumps(entry)
        object.__setattr__(self, "node_evidence", frozen_ev)
        if not isinstance(self.packed_zlib, bytes) or not self.packed_zlib:
            raise FrameBindingRefusalError("packed_zlib must be non-empty bytes")

    def to_dict(self) -> dict:
        """Return a canonical, float-free dict copy suitable for `schema.dumps`."""
        out = {
            "schema": self.schema,
            "handle": {
                "kind": self.handle.kind,
                "canonical_id": self.handle.canonical_id,
                "generation": self.handle.generation,
            },
            "epoch": self.epoch,
            "frame_seq": self.frame_seq,
            "dpr_ppm": self.dpr_ppm,
            "frame_sha256": self.frame_sha256,
            "state_ax_sha256": self.state_ax_sha256,
            "state_ax_stable_sha256": self.state_ax_stable_sha256,
            "provider": self.provider.to_dict(),
            "node_evidence": copy.deepcopy(list(self.node_evidence)),
            "packed_crc32": zlib.crc32(self.packed_zlib) & 0xFFFFFFFF,
        }
        schema.dumps(out)
        return out

    def evidence_by_node_id(self) -> dict[str, dict]:
        return {entry["node_id"]: copy.deepcopy(entry) for entry in self.node_evidence}


# @anchor:wa.perceive.pslot_adapter.pack_sidecar_binary
def pack_sidecar_binary(frame_sha256: str, epoch: int, frame_seq: int, dpr_ppm: int,
                        node_evidence: tuple[dict, ...] | list[dict]) -> bytes:
    """Pack sidecar provenance and per-node integer evidence using stdlib struct and zlib."""
    if not isinstance(frame_sha256, str) or not _SHA256_RE.match(frame_sha256):
        raise FrameBindingRefusalError("frame_sha256 must be 64 lowercase hex chars")
    _require_strict_int(epoch, "epoch", minimum=0, error_cls=FrameBindingRefusalError)
    _require_strict_int(frame_seq, "frame_seq", minimum=0, error_cls=FrameBindingRefusalError)
    _require_strict_int(dpr_ppm, "dpr_ppm", minimum=1, error_cls=FrameBindingRefusalError)

    records = list(node_evidence)
    header = struct.pack(
        ">6s32sQQII",
        SIDECAR_BINARY_MAGIC,
        bytes.fromhex(frame_sha256),
        epoch,
        frame_seq,
        dpr_ppm,
        len(records),
    )
    chunks = [header]
    for rec in records:
        node_id_bytes = bytes.fromhex(rec["node_id"])
        dx, dy, dw, dh = rec["device_box"]
        cx, cy, cw, ch = rec["css_box"]
        conf = rec["confidence_ppm"]
        key_bytes = rec["element_key"].encode("utf-8")
        chunks.append(
            struct.pack(
                ">8siiiiiiiII",
                node_id_bytes,
                dx, dy, dw, dh,
                cx, cy, cw, ch,
                conf,
            )
            + struct.pack(">H", len(key_bytes))
            + key_bytes
        )
    raw_payload = b"".join(chunks)
    compressed = zlib.compress(raw_payload, level=6)
    crc = zlib.crc32(compressed) & 0xFFFFFFFF
    return struct.pack(">I", crc) + compressed


# @anchor:wa.perceive.pslot_adapter.unpack_sidecar_binary
def unpack_sidecar_binary(blob: bytes) -> dict:
    """Verify CRC32, decompress with stdlib zlib, and unpack binary sidecar records via struct."""
    if not isinstance(blob, bytes) or len(blob) < 8:
        raise FrameBindingRefusalError("sidecar binary blob is truncated or not bytes")
    (expected_crc,) = struct.unpack(">I", blob[:4])
    compressed = blob[4:]
    actual_crc = zlib.crc32(compressed) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise FrameBindingRefusalError("sidecar binary CRC32 mismatch")
    try:
        raw = zlib.decompress(compressed)
    except zlib.error as exc:
        raise FrameBindingRefusalError(f"sidecar binary zlib decompression failed: {exc}") from exc

    header_fmt = ">6s32sQQII"
    header_size = struct.calcsize(header_fmt)
    if len(raw) < header_size:
        raise FrameBindingRefusalError("sidecar binary header truncated")
    magic, sha_bytes, epoch, frame_seq, dpr_ppm, count = struct.unpack(header_fmt, raw[:header_size])
    if magic != SIDECAR_BINARY_MAGIC:
        raise FrameBindingRefusalError("sidecar binary magic mismatch")

    offset = header_size
    rec_fmt = ">8siiiiiiiII"
    rec_size = struct.calcsize(rec_fmt)
    items = []
    for _ in range(count):
        if offset + rec_size + 2 > len(raw):
            raise FrameBindingRefusalError("sidecar binary record truncated")
        node_id_bytes, dx, dy, dw, dh, cx, cy, cw, ch, conf = struct.unpack(
            rec_fmt, raw[offset:offset + rec_size]
        )
        offset += rec_size
        (key_len,) = struct.unpack(">H", raw[offset:offset + 2])
        offset += 2
        if offset + key_len > len(raw):
            raise FrameBindingRefusalError("sidecar binary element_key truncated")
        element_key = raw[offset:offset + key_len].decode("utf-8")
        offset += key_len
        items.append({
            "node_id": node_id_bytes.hex(),
            "element_key": element_key,
            "device_box": [dx, dy, dw, dh],
            "css_box": [cx, cy, cw, ch],
            "confidence_ppm": conf,
        })
    if offset != len(raw):
        raise FrameBindingRefusalError("sidecar binary has trailing bytes")
    return {
        "frame_sha256": sha_bytes.hex(),
        "epoch": epoch,
        "frame_seq": frame_seq,
        "dpr_ppm": dpr_ppm,
        "items": items,
    }


# @anchor:wa.perceive.pslot_adapter.AdaptedVisionBundle
@dataclass(frozen=True)
class AdaptedVisionBundle:
    """Complete output of PslotProviderAdapter: closed STATE, Observation, envelope, and sidecar."""

    observation: Observation
    state: dict
    sidecar: EvidenceSidecar
    envelope: dict

    def __post_init__(self) -> None:
        if not isinstance(self.observation, Observation):
            raise FrameBindingRefusalError("observation must be a session_registry.Observation")
        if not isinstance(self.sidecar, EvidenceSidecar):
            raise FrameBindingRefusalError("sidecar must be an EvidenceSidecar")
        state_copy = copy.deepcopy(self.state)
        errs = schema.validate_state(state_copy)
        if errs:
            raise schema.StateError("; ".join(errs))
        env_copy = copy.deepcopy(self.envelope)
        env_errs = schema.validate_target_descriptor(env_copy, expected_epoch=self.observation.epoch)
        if env_errs:
            raise schema.StateError("; ".join(env_errs))
        object.__setattr__(self, "state", state_copy)
        object.__setattr__(self, "envelope", env_copy)


# @anchor:wa.perceive.pslot_adapter.verify_sidecar_binding
def verify_sidecar_binding(sidecar: EvidenceSidecar, observation_or_state: Observation | dict,
                           *, frame: FrameRef | None = None,
                           expected_handle: Handle | None = None,
                           expected_epoch: int | None = None,
                           expected_frame_seq: int | None = None,
                           expected_dpr_ppm: int | None = None) -> bool:
    """Verify that an EvidenceSidecar is cryptographically and structurally bound to its FrameRef and STATE."""
    if not isinstance(sidecar, EvidenceSidecar):
        raise FrameBindingRefusalError("sidecar must be an EvidenceSidecar")

    if isinstance(observation_or_state, Observation):
        obs = observation_or_state
        state = obs.state
        if frame is None:
            frame = obs.frame
        if (sidecar.handle, sidecar.epoch, sidecar.frame_seq, sidecar.dpr_ppm) != (
            obs.handle, obs.epoch, obs.frame_seq, obs.dpr_ppm
        ):
            raise FrameBindingRefusalError("sidecar provenance differs from Observation")
    elif isinstance(observation_or_state, dict):
        state = observation_or_state
    else:
        raise FrameBindingRefusalError("observation_or_state must be an Observation or STATE dict")

    errs = schema.validate_state(state)
    if errs:
        raise FrameBindingRefusalError("STATE invalid: " + "; ".join(errs))
    if state["source"]["kind"] != VISION_SOURCE_KIND:
        raise FrameBindingRefusalError(f"STATE source.kind must be {VISION_SOURCE_KIND!r}")

    if expected_handle is not None and sidecar.handle != _coerce_handle(expected_handle):
        raise FrameBindingRefusalError("sidecar handle does not match expected_handle")
    if expected_epoch is not None and sidecar.epoch != expected_epoch:
        raise FrameBindingRefusalError("sidecar epoch does not match expected_epoch")
    if expected_frame_seq is not None and sidecar.frame_seq != expected_frame_seq:
        raise FrameBindingRefusalError("sidecar frame_seq does not match expected_frame_seq")
    if expected_dpr_ppm is not None and sidecar.dpr_ppm != expected_dpr_ppm:
        raise FrameBindingRefusalError("sidecar dpr_ppm does not match expected_dpr_ppm")

    if frame is not None:
        if not isinstance(frame, FrameRef):
            raise FrameBindingRefusalError("frame must be a FrameRef")
        actual_sha = hashlib.sha256(np.asarray(frame.rgb).tobytes()).hexdigest()
        if frame.digest != actual_sha or sidecar.frame_sha256 != actual_sha:
            raise FrameBindingRefusalError("sidecar frame_sha256 does not match frame digest")
        if (sidecar.handle, sidecar.epoch, sidecar.frame_seq) != (
            frame.handle, frame.epoch, frame.frame_seq
        ):
            raise FrameBindingRefusalError("sidecar handle/epoch/frame_seq does not match FrameRef")

    if (sidecar.state_ax_sha256 != state["ax_sha256"]
            or sidecar.state_ax_stable_sha256 != state["ax_stable_sha256"]):
        raise FrameBindingRefusalError("sidecar state SHA-256 digests do not match STATE")

    nodes = state["nodes"]
    if len(sidecar.node_evidence) != len(nodes):
        raise FrameBindingRefusalError("sidecar node_evidence count differs from STATE nodes count")

    unpacked = unpack_sidecar_binary(sidecar.packed_zlib)
    if (unpacked["frame_sha256"] != sidecar.frame_sha256
            or unpacked["epoch"] != sidecar.epoch
            or unpacked["frame_seq"] != sidecar.frame_seq
            or unpacked["dpr_ppm"] != sidecar.dpr_ppm
            or len(unpacked["items"]) != len(nodes)):
        raise FrameBindingRefusalError("packed binary sidecar does not match sidecar fields")

    for idx, (node, ev, bin_item) in enumerate(zip(nodes, sidecar.node_evidence, unpacked["items"])):
        if sorted(node.keys()) != sorted(schema.NODE_KEYS):
            raise FrameBindingRefusalError(f"node[{idx}] violates closed NODE_KEYS")
        if ev["node_id"] != node["id"] or bin_item["node_id"] != node["id"]:
            raise FrameBindingRefusalError(f"node[{idx}] id mismatch in sidecar")
        if ev["css_box"] != node["box"] or bin_item["css_box"] != node["box"]:
            raise FrameBindingRefusalError(f"node[{idx}] box mismatch in sidecar")
        if ev["frame_sha256"] != sidecar.frame_sha256:
            raise FrameBindingRefusalError(f"node[{idx}] frame_sha256 mismatch in sidecar")
        if ev["confidence_ppm"] != bin_item["confidence_ppm"]:
            raise FrameBindingRefusalError(f"node[{idx}] confidence_ppm mismatch in packed binary")
        _require_strict_int(
            ev["confidence_ppm"],
            "confidence_ppm",
            minimum=0,
            maximum=CONFIDENCE_PPM_MAX,
            error_cls=ElementValidationRefusalError,
        )
    return True


def _validate_and_verify_frame_ref(frame: object) -> tuple[FrameRef, str]:
    if not isinstance(frame, FrameRef):
        raise FrameBindingRefusalError("frame must be a session_registry.FrameRef instance")
    rgb = frame.rgb
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.dtype("uint8") or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise FrameBindingRefusalError("frame.rgb must be H x W x 3 uint8 ndarray")
    if rgb.shape[:2] != (frame.height, frame.width):
        raise FrameBindingRefusalError("frame.rgb shape does not match (height, width)")
    actual_sha = hashlib.sha256(rgb.tobytes(order="C")).hexdigest()
    if not isinstance(frame.digest, str) or not _SHA256_RE.match(frame.digest) or frame.digest != actual_sha:
        raise FrameBindingRefusalError("frame digest mismatch against RGB buffer")
    return frame, actual_sha


# @anchor:wa.perceive.pslot_adapter.adapt_provider_response
def adapt_provider_response(
    frame: FrameRef,
    response: ProviderResponse | dict,
    *,
    expected_handle: Handle | None = None,
    expected_epoch: int | None = None,
    expected_frame_seq: int | None = None,
    dpr_ppm: int = 1_000_000,
    allowed_roles: frozenset[str] = ALLOWED_VISION_ROLES,
    actionable_roles: frozenset[str] = ACTIONABLE_VISION_ROLES,
    min_confidence_ppm: int = 0,
    primary_evidence_kind: str | None = None,
    primary_state: dict | None = None,
    allow_when_primary_present: bool = False,
) -> AdaptedVisionBundle:
    """Validate a provider response against `frame` and build a closed vision STATE + sidecar."""
    if primary_evidence_kind is not None:
        if primary_evidence_kind in PRIMARY_EVIDENCE_KINDS and not allow_when_primary_present:
            raise PrimaryEvidenceOverrideRefusalError(
                f"structured primary evidence ({primary_evidence_kind!r}) remains primary and cannot be overridden by vision"
            )
    if primary_state is not None:
        if not isinstance(primary_state, dict):
            raise PrimaryEvidenceOverrideRefusalError("primary_state must be a dict when supplied")
        src_kind = (primary_state.get("source") or {}).get("kind")
        if src_kind in PRIMARY_EVIDENCE_KINDS and not allow_when_primary_present:
            raise PrimaryEvidenceOverrideRefusalError(
                f"structured primary STATE (source.kind={src_kind!r}) remains primary and cannot be replaced by vision"
            )

    frame, actual_frame_sha = _validate_and_verify_frame_ref(frame)
    _require_strict_int(dpr_ppm, "dpr_ppm", minimum=1, error_cls=FrameBindingRefusalError)
    _require_strict_int(
        min_confidence_ppm,
        "min_confidence_ppm",
        minimum=0,
        maximum=CONFIDENCE_PPM_MAX,
        error_cls=ElementValidationRefusalError,
    )

    if isinstance(response, dict):
        if "provider" not in response:
            raise ProviderIdentityError("response dict is missing provider identity")
        raw_prov = response["provider"]
        if isinstance(raw_prov, dict):
            prov = ProviderIdentity(
                provider_id=raw_prov.get("provider_id", ""),
                provider_version=raw_prov.get("provider_version", ""),
                model_id=raw_prov.get("model_id", DEFAULT_PERCEIVER_MODEL),
                role=raw_prov.get("role", NETWORK_ROLE_DEFAULT_PERCEIVER),
            )
        elif isinstance(raw_prov, ProviderIdentity):
            prov = raw_prov
        else:
            raise ProviderIdentityError("response.provider must be ProviderIdentity or dict")
        if "frame_sha256" not in response:
            raise FrameBindingRefusalError("response dict is missing frame_sha256")
        if "handle" not in response or "epoch" not in response or "frame_seq" not in response:
            raise FrameBindingRefusalError("response dict is missing handle, epoch, or frame_seq")
        if "elements" not in response:
            raise ElementValidationRefusalError("response dict is missing elements")
        resp = ProviderResponse(
            provider=prov,
            frame_sha256=response["frame_sha256"],
            handle=response["handle"],
            epoch=response["epoch"],
            frame_seq=response["frame_seq"],
            elements=response["elements"],
            dpr_ppm=response.get("dpr_ppm", dpr_ppm),
            url=response.get("url", ""),
            title=response.get("title", ""),
        )
    elif isinstance(response, ProviderResponse):
        resp = response
    else:
        raise ProviderContractRefusalError(f"response must be ProviderResponse or dict, got {type(response).__name__}")

    if resp.provider.role == NETWORK_ROLE_OUT_OF_LOOP_JUDGE:
        raise ProviderIdentityError("out-of-loop judge model cannot emit in-loop vision state nodes")

    if resp.frame_sha256 != actual_frame_sha:
        raise FrameBindingRefusalError(
            f"provider frame_sha256 {resp.frame_sha256!r} does not match frame digest {actual_frame_sha!r}"
        )
    if resp.handle != frame.handle:
        raise FrameBindingRefusalError("provider response handle does not match frame.handle")
    if resp.epoch != frame.epoch:
        raise FrameBindingRefusalError(f"provider response epoch {resp.epoch} != frame.epoch {frame.epoch}")
    if resp.frame_seq != frame.frame_seq:
        raise FrameBindingRefusalError(
            f"provider response frame_seq {resp.frame_seq} != frame.frame_seq {frame.frame_seq}"
        )
    if resp.dpr_ppm != dpr_ppm:
        raise FrameBindingRefusalError(f"provider response dpr_ppm {resp.dpr_ppm} != expected dpr_ppm {dpr_ppm}")

    if expected_handle is not None and frame.handle != _coerce_handle(expected_handle):
        raise FrameBindingRefusalError("frame.handle does not match expected_handle")
    if expected_epoch is not None:
        _require_strict_int(expected_epoch, "expected_epoch", minimum=0, error_cls=FrameBindingRefusalError)
        if frame.epoch != expected_epoch:
            raise FrameBindingRefusalError(f"frame.epoch {frame.epoch} != expected_epoch {expected_epoch}")
    if expected_frame_seq is not None:
        _require_strict_int(expected_frame_seq, "expected_frame_seq", minimum=0, error_cls=FrameBindingRefusalError)
        if frame.frame_seq != expected_frame_seq:
            raise FrameBindingRefusalError(
                f"frame.frame_seq {frame.frame_seq} != expected_frame_seq {expected_frame_seq}"
            )

    validated_elements: list[ProviderElement] = []
    seen_element_keys: set[str] = set()
    seen_signatures: set[tuple] = set()
    depth_by_key: dict[str, int] = {}
    ancestors_by_key: dict[str, tuple[str, ...]] = {}

    for raw_el in resp.elements:
        el = _coerce_provider_element(raw_el)
        if el.element_key in seen_element_keys:
            raise AmbiguousElementRefusalError(f"duplicate element_key {el.element_key!r}")
        if el.role not in allowed_roles:
            raise ElementValidationRefusalError(f"unsupported vision role {el.role!r}")
        if el.confidence_ppm < min_confidence_ppm:
            raise ElementValidationRefusalError(
                f"element {el.element_key!r} confidence_ppm {el.confidence_ppm} < min_confidence_ppm {min_confidence_ppm}"
            )

        x, y, w, h = el.box
        if x + w > frame.width or y + h > frame.height:
            raise GeometryRefusalError(
                f"element {el.element_key!r} box {el.box} exceeds frame bounds ({frame.width}, {frame.height})"
            )

        if el.parent_key is not None:
            if el.parent_key not in seen_element_keys:
                raise AmbiguousElementRefusalError(
                    f"element {el.element_key!r} references parent_key {el.parent_key!r} which does not precede it"
                )
            depth = depth_by_key[el.parent_key] + 1
            ancestors = ancestors_by_key[el.parent_key] + (el.role,)
        else:
            depth = 0
            ancestors = ()

        sig = (el.parent_key, el.role, el.text, el.box)
        if sig in seen_signatures:
            raise AmbiguousElementRefusalError(
                f"ambiguous element with identical parent, role, text, and box: {sig!r}"
            )
        seen_signatures.add(sig)
        seen_element_keys.add(el.element_key)
        depth_by_key[el.element_key] = depth
        ancestors_by_key[el.element_key] = ancestors
        validated_elements.append(el)

    ordinal_counter: dict[tuple[str, str], int] = {}
    pruned_nodes: list[dict] = []
    boxes_by_node_id: dict[str, list[int]] = {}
    node_evidence_list: list[dict] = []
    seen_node_ids: set[str] = set()

    for el in validated_elements:
        role_name_key = (el.role, el.text)
        ordinal = ordinal_counter.get(role_name_key, 0)
        ordinal_counter[role_name_key] = ordinal + 1
        depth = depth_by_key[el.element_key]
        ancestors = ancestors_by_key[el.element_key][:-1] if depth > 0 else ()
        computed_id = _vision_node_id(ancestors, el.role, el.text, ordinal)
        node_id = el.explicit_node_id if el.explicit_node_id is not None else computed_id
        if node_id in seen_node_ids:
            raise AmbiguousElementRefusalError(f"colliding 16-hex node_id {node_id!r}")
        seen_node_ids.add(node_id)

        css_box = _device_box_to_css_box(el.box, dpr_ppm)
        boxes_by_node_id[node_id] = css_box
        pruned_nodes.append({
            "id": node_id,
            "role": el.role,
            "name": el.text,
            "depth": depth,
            "ordinal": ordinal,
            "attrs": el.states,
        })
        node_evidence_list.append({
            "node_id": node_id,
            "element_key": el.element_key,
            "role": el.role,
            "name": el.text,
            "ordinal": ordinal,
            "depth": depth,
            "confidence_ppm": el.confidence_ppm,
            "device_box": list(el.box),
            "css_box": list(css_box),
            "frame_sha256": actual_frame_sha,
            "provider_id": resp.provider.provider_id,
            "provider_version": resp.provider.provider_version,
            "model_id": resp.provider.model_id,
        })

    ax_sha256 = _snapshot_sha(pruned_nodes, with_state=True)
    ax_stable_sha256 = _snapshot_sha(pruned_nodes, with_state=False)

    css_vp_w = max(1, (frame.width * 1_000_000) // dpr_ppm)
    css_vp_h = max(1, (frame.height * 1_000_000) // dpr_ppm)
    viewport = {
        "w": css_vp_w,
        "h": css_vp_h,
        "scroll_x": 0,
        "scroll_y": 0,
        "doc_w": css_vp_w,
        "doc_h": css_vp_h,
        "dpr_ppm": dpr_ppm,
    }

    state = schema.build_state(
        pruned_nodes,
        kind=VISION_SOURCE_KIND,
        url=resp.url,
        title=resp.title,
        ax_sha256=ax_sha256,
        ax_stable_sha256=ax_stable_sha256,
        prune={"name": PRUNE_VISION_NAME, "version": PRUNE_VISION_VERSION},
        raw_count=len(validated_elements),
        boxes=boxes_by_node_id,
        viewport=viewport,
        actionable_roles=actionable_roles,
    )

    packed_zlib = pack_sidecar_binary(
        actual_frame_sha,
        frame.epoch,
        frame.frame_seq,
        dpr_ppm,
        node_evidence_list,
    )
    sidecar = EvidenceSidecar(
        schema=SIDECAR_SCHEMA_ID,
        handle=frame.handle,
        epoch=frame.epoch,
        frame_seq=frame.frame_seq,
        dpr_ppm=dpr_ppm,
        frame_sha256=actual_frame_sha,
        state_ax_sha256=ax_sha256,
        state_ax_stable_sha256=ax_stable_sha256,
        provider=resp.provider,
        node_evidence=tuple(node_evidence_list),
        packed_zlib=packed_zlib,
    )
    observation = Observation(
        handle=frame.handle,
        epoch=frame.epoch,
        frame_seq=frame.frame_seq,
        dpr_ppm=dpr_ppm,
        state=state,
        frame=frame,
    )
    envelope = schema.build_target_descriptor(
        frame.handle,
        frame.epoch,
        state,
        frame_seq=frame.frame_seq,
        dpr_ppm=dpr_ppm,
    )
    verify_sidecar_binding(sidecar, observation, frame=frame)
    return AdaptedVisionBundle(
        observation=observation,
        state=state,
        sidecar=sidecar,
        envelope=envelope,
    )


# @anchor:wa.perceive.pslot_adapter.PslotProviderAdapter
class PslotProviderAdapter:
    """Adapter binding an injected perception provider to closed vision STATE and sidecar."""

    def __init__(self, provider: PerceptionProviderContract, *,
                 allowed_roles: frozenset[str] = ALLOWED_VISION_ROLES,
                 actionable_roles: frozenset[str] = ACTIONABLE_VISION_ROLES,
                 min_confidence_ppm: int = 0,
                 local_http_seam: LocalHttpSeamConfig | None = None) -> None:
        if provider is None or provider == AUSENTE:
            raise ProviderContractRefusalError("injected provider contract is missing or AUSENTE")
        identify_fn = getattr(provider, "identify", None)
        perceive_fn = getattr(provider, "perceive_frame", None)
        if not callable(identify_fn) or not callable(perceive_fn):
            raise ProviderContractRefusalError(
                "injected provider must expose callable identify() and perceive_frame()"
            )
        identity = identify_fn()
        if not isinstance(identity, ProviderIdentity):
            raise ProviderIdentityError("provider.identify() must return a ProviderIdentity")
        if identity.role == NETWORK_ROLE_OUT_OF_LOOP_JUDGE:
            raise ProviderIdentityError("out-of-loop judge cannot be registered as an in-loop perceiver adapter")
        if local_http_seam is not None and not isinstance(local_http_seam, LocalHttpSeamConfig):
            raise ProviderContractRefusalError("local_http_seam must be a LocalHttpSeamConfig")
        _require_strict_int(
            min_confidence_ppm,
            "min_confidence_ppm",
            minimum=0,
            maximum=CONFIDENCE_PPM_MAX,
            error_cls=ElementValidationRefusalError,
        )
        self._provider = provider
        self._identity = identity
        self._allowed_roles = frozenset(allowed_roles)
        self._actionable_roles = frozenset(actionable_roles)
        self._min_confidence_ppm = min_confidence_ppm
        self._local_http_seam = local_http_seam

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    @property
    def local_http_seam(self) -> LocalHttpSeamConfig | None:
        return self._local_http_seam

    def perceive_and_adapt(
        self,
        frame: FrameRef,
        *,
        expected_handle: Handle | None = None,
        expected_epoch: int | None = None,
        expected_frame_seq: int | None = None,
        dpr_ppm: int = 1_000_000,
        url: str = "",
        title: str = "",
        primary_evidence_kind: str | None = None,
        primary_state: dict | None = None,
        allow_when_primary_present: bool = False,
    ) -> AdaptedVisionBundle:
        """Invoke the injected provider on `frame` and adapt the validated response."""
        if primary_evidence_kind is not None and primary_evidence_kind in PRIMARY_EVIDENCE_KINDS and not allow_when_primary_present:
            raise PrimaryEvidenceOverrideRefusalError(
                f"structured primary evidence ({primary_evidence_kind!r}) remains primary; vision dispatch refused"
            )
        if primary_state is not None:
            if not isinstance(primary_state, dict):
                raise PrimaryEvidenceOverrideRefusalError("primary_state must be a dict")
            src_kind = (primary_state.get("source") or {}).get("kind")
            if src_kind in PRIMARY_EVIDENCE_KINDS and not allow_when_primary_present:
                raise PrimaryEvidenceOverrideRefusalError(
                    f"structured primary STATE ({src_kind!r}) remains primary; vision dispatch refused"
                )
        frame, _ = _validate_and_verify_frame_ref(frame)
        _require_strict_int(dpr_ppm, "dpr_ppm", minimum=1, error_cls=FrameBindingRefusalError)
        response = self._provider.perceive_frame(frame, dpr_ppm=dpr_ppm, url=url, title=title)
        if isinstance(response, ProviderResponse) and response.provider != self._identity:
            raise ProviderIdentityError("provider response identity differs from declared provider.identify()")
        if isinstance(response, dict) and "provider" in response:
            raw_p = response["provider"]
            if isinstance(raw_p, ProviderIdentity) and raw_p != self._identity:
                raise ProviderIdentityError("provider response identity differs from declared provider.identify()")
        return adapt_provider_response(
            frame,
            response,
            expected_handle=expected_handle,
            expected_epoch=expected_epoch,
            expected_frame_seq=expected_frame_seq,
            dpr_ppm=dpr_ppm,
            allowed_roles=self._allowed_roles,
            actionable_roles=self._actionable_roles,
            min_confidence_ppm=self._min_confidence_ppm,
            primary_evidence_kind=primary_evidence_kind,
            primary_state=primary_state,
            allow_when_primary_present=allow_when_primary_present,
        )


__all__ = [
    "ACTIONABLE_VISION_ROLES",
    "ALLOWED_STATE_ATTRS",
    "ALLOWED_VISION_ROLES",
    "ALT_PERCEIVER_MODEL",
    "AUSENTE",
    "AdaptedVisionBundle",
    "AmbiguousElementRefusalError",
    "CAPABILITY_MATRIX",
    "CONFIDENCE_PPM_MAX",
    "DEFAULT_PERCEIVER_MODEL",
    "DeterministicFakeProvider",
    "DeterministicFakeTransport",
    "ElementValidationRefusalError",
    "EvidenceSidecar",
    "FrameBindingRefusalError",
    "GeometryRefusalError",
    "LocalHttpSeamConfig",
    "MAX_NAME_BYTES",
    "NETWORK_ROLE_ALT_PERCEIVER",
    "NETWORK_ROLE_DEFAULT_PERCEIVER",
    "NETWORK_ROLE_NOT_CONFIGURED",
    "NETWORK_ROLE_OUT_OF_LOOP_JUDGE",
    "OUT_OF_LOOP_JUDGE_MODEL",
    "PRIMARY_EVIDENCE_KINDS",
    "PRUNE_VISION_NAME",
    "PRUNE_VISION_VERSION",
    "PerceptionProviderContract",
    "PrimaryEvidenceOverrideRefusalError",
    "ProviderCapabilityRefusalError",
    "ProviderContractRefusalError",
    "ProviderElement",
    "ProviderIdentity",
    "ProviderIdentityError",
    "ProviderResponse",
    "PslotAdapterError",
    "PslotProviderAdapter",
    "QuotaFloorRefusalError",
    "SIDECAR_BINARY_MAGIC",
    "SIDECAR_SCHEMA_ID",
    "UNCONFIGURED_MODEL",
    "VISION_SOURCE_KIND",
    "adapt_provider_response",
    "check_judge_quota_floor",
    "normalize_vision_text",
    "pack_sidecar_binary",
    "resolve_provider_capability",
    "unpack_sidecar_binary",
    "verify_sidecar_binding",
]
