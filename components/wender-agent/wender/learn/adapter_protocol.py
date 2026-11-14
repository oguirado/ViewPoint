# @module {"id":"wa.learn.adapter_protocol","layer":2,"owner":"LEARN","deps":["wa.state.schema"],"book":"HB-WA-s4-learn"}
"""@module wa.learn.adapter_protocol - Backend-free EpisodeAdapter Protocol and capability/reset contracts.

Closed T2, truthful UI-tree provenance, capture, reset/score/replay contracts.
Backend-free: imports neither Godot nor CDP. Source-neutral episode contracts.
"""
from __future__ import annotations

import ast
import dataclasses
from dataclasses import dataclass, field
import hashlib
import importlib
import importlib.util
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from wender.state import schema
from wender.state.schema import FrameRef, Handle, Observation, Receipt, StreamSpec, StreamToken


SUPPORTED_HANDLE_KINDS: frozenset[str] = frozenset(schema.TARGET_KINDS)

_SOURCE_TO_HANDLE_KIND: dict[str, str] = {
    "cdp": "cdp_tab",
    "cdp_tab": "cdp_tab",
    "godot": "godot_window",
    "godot_window": "godot_window",
    "atspi": "x11_window",
    "x11": "x11_window",
    "x11_window": "x11_window",
    "xvfb": "xvfb_screen",
    "xvfb_screen": "xvfb_screen",
    "capsule": "capsule",
    "vision": "x11_window",
}

FORBIDDEN_BACKEND_IMPORTS: tuple[str, ...] = (
    "cdp",
    "cdp_port",
    "cdp_pipe",
    "godot",
    "godot_adapter",
    "godot_window_target",
    "godot_executor",
    "godot_window_driver",
    "selenium",
    "playwright",
    "chromium",
)

FORBIDDEN_SIBLING_JQ2B_MODULES: tuple[str, ...] = (
    "episode_records",
    "t2_outcome_index",
    "t2_trace",
    "holdings_snapshot",
    "episode_spec",
    "episode_trail",
    "episode_metrics",
    "failure_taxonomy",
    "cgroup_memory",
    "procfs_memory",
    "holdings_dispatch",
    "window_loop_recovery",
    "corroboration_evidence",
    "holdings_abstention",
    "episode_provenance",
    "episode_segments",
)


class AusenteError(ValueError, RuntimeError):
    """Raised when a required prerequisite provider or evidence is absent (AUSENTE)."""


# @anchor:wa.learn.adapter_protocol.default_handle_kind
def default_handle_kind(source_kind: str) -> str:
    """Derive a default Handle kind strictly from SUPPORTED_HANDLE_KINDS."""
    if not isinstance(source_kind, str) or not source_kind:
        raise ValueError("source_kind must be a non-empty string")
    if source_kind in SUPPORTED_HANDLE_KINDS:
        return source_kind
    mapped = _SOURCE_TO_HANDLE_KIND.get(source_kind)
    if mapped is not None and mapped in SUPPORTED_HANDLE_KINDS:
        return mapped
    raise ValueError(
        f"Unsupported source_kind {source_kind!r}; cannot derive supported handle kind from {sorted(SUPPORTED_HANDLE_KINDS)}"
    )


# @anchor:wa.learn.adapter_protocol.min_stream_quota_bytes
def min_stream_quota_bytes(width: int, height: int, slots: int) -> int:
    """Compute minimum quota_bytes required to cover width * height * 3 * slots."""
    for name, val in (("width", width), ("height", height), ("slots", slots)):
        if type(val) is not int or val < 1:
            raise ValueError(f"{name} must be an integer >= 1")
    return width * height * 3 * slots


# @anchor:wa.learn.adapter_protocol.make_stream_spec
def make_stream_spec(width: int, height: int, slots: int, quota_bytes: int) -> StreamSpec:
    """Construct a canonical StreamSpec after validating quota_bytes >= width * height * 3 * slots."""
    min_quota = min_stream_quota_bytes(width, height, slots)
    if type(quota_bytes) is not int or quota_bytes < min_quota:
        raise ValueError(
            f"quota_bytes ({quota_bytes!r}) must be an integer >= {min_quota} (width * height * 3 * slots)"
        )
    return StreamSpec(width=width, height=height, slots=slots, quota_bytes=quota_bytes)


# @anchor:wa.learn.adapter_protocol.make_receipt
def make_receipt(
    action_id: str,
    handle: Handle,
    status: str = "applied",
    reason: str | None = None,
) -> Receipt:
    """Construct a canonical Receipt always passing an explicit reason field."""
    return Receipt(action_id=action_id, handle=handle, status=status, reason=reason)


# @anchor:wa.learn.adapter_protocol.require_prerequisite_provider
def require_prerequisite_provider(
    module_name: str,
    required_symbols: Sequence[str] = (),
) -> Any:
    """Lazily import a prerequisite provider module and fail loudly as AUSENTE when absent."""
    if not isinstance(module_name, str) or not module_name.strip():
        raise AusenteError("AUSENTE: prerequisite module_name must be a non-empty string")
    try:
        mod = importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError) as exc:
        raise AusenteError(f"AUSENTE: prerequisite provider '{module_name}' is absent: {exc}") from exc
    if mod is None:
        raise AusenteError(f"AUSENTE: prerequisite provider '{module_name}' is None")
    for sym in required_symbols:
        if not hasattr(mod, sym):
            raise AusenteError(
                f"AUSENTE: prerequisite provider '{module_name}' is missing canonical symbol '{sym}'"
            )
    return mod


# @anchor:wa.learn.adapter_protocol.verify_prerequisite_contracts
def verify_prerequisite_contracts(*, require_window_stream_bank: bool = False) -> dict[str, Any]:
    """Lazily import real JQ2-A prerequisite providers and verify canonical runtime types."""
    sr = require_prerequisite_provider(
        "wender.perceive.session_registry",
        ("Handle", "Observation", "FrameRef", "Receipt", "StreamSpec", "StreamToken", "SUPPORTED_KINDS"),
    )
    wsb = None
    if require_window_stream_bank:
        wsb = require_prerequisite_provider(
            "wender.perceive.window_stream_bank",
            ("StreamSpec", "StreamToken"),
        )
    else:
        try:
            spec = importlib.util.find_spec("wender.perceive.window_stream_bank")
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None
        if spec is not None:
            wsb = require_prerequisite_provider(
                "wender.perceive.window_stream_bank",
                ("StreamSpec", "StreamToken"),
            )

    if Handle is not sr.Handle or Observation is not sr.Observation or FrameRef is not sr.FrameRef or Receipt is not sr.Receipt:
        raise AusenteError("AUSENTE: canonical runtime types in adapter_protocol diverged from session_registry")
    if StreamSpec is not sr.StreamSpec or StreamToken is not sr.StreamToken:
        raise AusenteError("AUSENTE: StreamSpec/StreamToken diverged from session_registry")

    return {
        "session_registry": sr,
        "window_stream_bank": wsb,
        "Handle": sr.Handle,
        "Observation": sr.Observation,
        "FrameRef": sr.FrameRef,
        "Receipt": sr.Receipt,
        "StreamSpec": sr.StreamSpec if wsb is None else wsb.StreamSpec,
        "StreamToken": sr.StreamToken if wsb is None else wsb.StreamToken,
        "SUPPORTED_KINDS": sr.SUPPORTED_KINDS,
    }


# @anchor:wa.learn.adapter_protocol.verify_neutral_import_graph
def verify_neutral_import_graph(source_text: str | None = None) -> bool:
    """Verify Gate G J0 1: core adapter_protocol imports neither backend ports nor sibling JQ2-B modules."""
    if source_text is None:
        mod_file = Path(__file__).resolve()
        source_text = mod_file.read_text(encoding="utf-8")
    parsed = ast.parse(source_text)

    imported_modules: list[str] = []
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.append(node.module)
                for alias in node.names:
                    imported_modules.append(f"{node.module}.{alias.name}")
            else:
                for alias in node.names:
                    imported_modules.append(alias.name)

    for imp in imported_modules:
        parts = imp.split(".")
        for forbidden in FORBIDDEN_BACKEND_IMPORTS:
            if imp == forbidden or imp.startswith(forbidden + ".") or forbidden in parts:
                raise ValueError(
                    f"Gate G J0 1 / INV J0 1 violation: forbidden backend import '{imp}'"
                )
        for sibling in FORBIDDEN_SIBLING_JQ2B_MODULES:
            if sibling in parts:
                raise ValueError(
                    f"Wave 1 dependency violation: forbidden sibling JQ2-B import '{imp}'"
                )
    return True


# @anchor:wa.learn.adapter_protocol.capabilities
@dataclass(frozen=True)
class Capabilities:
    """Structural capabilities naming tree, frame, t2, reset, observer, source_kind and nullable binding_id."""
    tree: bool
    frame: bool
    t2: bool
    reset: bool
    observer: bool
    source_kind: str
    binding_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.tree, bool):
            raise ValueError("tree capability must be a bool")
        if not isinstance(self.frame, bool):
            raise ValueError("frame capability must be a bool")
        if not isinstance(self.t2, bool):
            raise ValueError("t2 capability must be a bool")
        if not isinstance(self.reset, bool):
            raise ValueError("reset capability must be a bool")
        if not isinstance(self.observer, bool):
            raise ValueError("observer capability must be a bool")
        if not isinstance(self.source_kind, str) or not self.source_kind:
            raise ValueError("source_kind must be a non-empty string")
        if self.binding_id is not None and not isinstance(self.binding_id, str):
            raise ValueError("binding_id must be a string or None")

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


# @anchor:wa.learn.adapter_protocol.launcher_evidence
@dataclass(frozen=True)
class LauncherEvidence:
    """Launcher evidence establishes capabilities; adapter self-assertion does not."""
    source_kind: str
    tree: bool
    frame: bool
    t2: bool
    reset: bool
    observer: bool
    binding_id: str | None = None
    launcher_id: str = "launcher"
    metadata: dict = field(default_factory=dict)

    def to_capabilities(self) -> Capabilities:
        return Capabilities(
            tree=self.tree,
            frame=self.frame,
            t2=self.t2,
            reset=self.reset,
            observer=self.observer,
            source_kind=self.source_kind,
            binding_id=self.binding_id,
        )


def capabilities_from_launcher(evidence: LauncherEvidence | dict | None) -> Capabilities:
    """Establish Capabilities strictly from launcher evidence.

    Adapter self-assertion is forbidden and raises ValueError.
    """
    if evidence is None or isinstance(evidence, Capabilities):
        raise ValueError("Launcher evidence is required to establish capabilities; adapter self-assertion is forbidden")
    if isinstance(evidence, LauncherEvidence):
        return evidence.to_capabilities()
    if isinstance(evidence, dict):
        if evidence.get("self_asserted") is True:
            raise ValueError("Adapter self-assertion of capabilities is forbidden")
        required_keys = ("tree", "frame", "t2", "reset", "observer", "source_kind")
        for k in required_keys:
            if k not in evidence:
                raise ValueError(f"Missing required launcher evidence key: {k}")
        return Capabilities(
            tree=bool(evidence["tree"]),
            frame=bool(evidence["frame"]),
            t2=bool(evidence["t2"]),
            reset=bool(evidence["reset"]),
            observer=bool(evidence["observer"]),
            source_kind=str(evidence["source_kind"]),
            binding_id=evidence.get("binding_id"),
        )
    raise TypeError(f"Expected LauncherEvidence or dict, got {type(evidence).__name__}")


# @anchor:wa.learn.adapter_protocol.unsupported_evidence
@dataclass(frozen=True)
class UnsupportedEvidence:
    """Typed unsupported evidence preserving unavailable capability truth."""
    capability: str
    reason: str
    source_kind: str
    details: dict = field(default_factory=dict)

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


class CapabilityError(ValueError):
    """Raised when an operation requires a capability that is missing or unavailable."""
    def __init__(self, message: str, evidence: UnsupportedEvidence | None = None):
        super().__init__(message)
        self.evidence = evidence


class FencedGenerationError(ValueError):
    """Raised when an operation is attempted with an expired / stale handle generation."""
    pass


# @anchor:wa.learn.adapter_protocol.reset_result
@dataclass(frozen=True)
class ResetResult:
    """Reset returns initial_state_sha256, build_id, content_id, schema_id, trusted_context_ref, presentation_digest."""
    initial_state_sha256: str
    build_id: str
    content_id: str
    schema_id: str
    trusted_context_ref: str
    presentation_digest: str | None = None

    def __post_init__(self):
        if not isinstance(self.initial_state_sha256, str) or len(self.initial_state_sha256) != 64:
            raise ValueError("initial_state_sha256 must be a 64-character hex string")
        if not isinstance(self.build_id, str) or not self.build_id:
            raise ValueError("build_id must be a non-empty string")
        if not isinstance(self.content_id, str) or not self.content_id:
            raise ValueError("content_id must be a non-empty string")
        if not isinstance(self.schema_id, str) or not self.schema_id:
            raise ValueError("schema_id must be a non-empty string")
        if not isinstance(self.trusted_context_ref, str) or not self.trusted_context_ref:
            raise ValueError("trusted_context_ref must be a non-empty string")
        if self.presentation_digest is not None and not isinstance(self.presentation_digest, str):
            raise ValueError("presentation_digest must be str or None")

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


# @anchor:wa.learn.adapter_protocol.approved_frame_metadata
@dataclass(frozen=True)
class ApprovedFrameMetadata:
    """Approved bounded metadata returned by observe_frame_digest, avoiding pixel/receipt export."""
    digest: str
    width: int
    height: int
    epoch: int
    frame_seq: int
    dpr_ppm: int
    source_kind: str

    def __post_init__(self):
        if not isinstance(self.digest, str) or len(self.digest) != 64:
            raise ValueError("digest must be a 64-character hex string")
        if type(self.width) is not int or self.width <= 0:
            raise ValueError("width must be a positive int")
        if type(self.height) is not int or self.height <= 0:
            raise ValueError("height must be a positive int")
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("epoch must be a non-negative int")
        if type(self.frame_seq) is not int or self.frame_seq < 0:
            raise ValueError("frame_seq must be a non-negative int")
        if type(self.dpr_ppm) is not int or self.dpr_ppm <= 0:
            raise ValueError("dpr_ppm must be a positive int")
        if not isinstance(self.source_kind, str) or not self.source_kind:
            raise ValueError("source_kind must be a non-empty str")

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


# @anchor:wa.learn.adapter_protocol.command
@dataclass(frozen=True)
class Command:
    """Holdings T2 command record."""
    tick: int
    seq: int
    cmd: str
    args: dict = field(default_factory=dict)

    def __post_init__(self):
        if type(self.tick) is not int or self.tick < 0:
            raise ValueError("tick must be a non-negative int")
        if type(self.seq) is not int or self.seq < 0:
            raise ValueError("seq must be a non-negative int")
        if not isinstance(self.cmd, str) or not self.cmd:
            raise ValueError("cmd must be a non-empty string")
        if not isinstance(self.args, dict):
            raise ValueError("args must be a dict")

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


def make_pause_command(tick: int, seq: int) -> Command:
    """A pause command has tick, seq, cmd set to pause and empty args."""
    return Command(tick=tick, seq=seq, cmd="pause", args={})


# @anchor:wa.learn.adapter_protocol.outcome
@dataclass(frozen=True)
class Outcome:
    """Holdings outcome record."""
    seq: int
    requested_tick: int
    applied_tick: int
    order_seq_id: int
    status: str
    reason: str | None
    state_sha256: str

    def __post_init__(self):
        for field_name in ("seq", "requested_tick", "applied_tick", "order_seq_id"):
            val = getattr(self, field_name)
            if type(val) is not int or val < 0:
                raise ValueError(f"{field_name} must be a non-negative int")
        if not isinstance(self.status, str) or not self.status:
            raise ValueError("status must be a non-empty string")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ValueError("reason must be str or None")
        if not isinstance(self.state_sha256, str) or len(self.state_sha256) != 64:
            raise ValueError("state_sha256 must be a 64-character hex string")

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


# @anchor:wa.learn.adapter_protocol.write_spy
class WriteSpy:
    """Audit spy ensuring no raw pixels, hook receipts, or opaque checkpoint bytes are exported or persisted."""
    PROHIBITED_KEYS = frozenset({
        "rgb",
        "pixels",
        "raw_pixels",
        "frame_bytes",
        "hook_receipt",
        "hook_receipts",
        "capture_receipt",
        "tree_dump",
        "checkpoint_bytes",
        "authority",
        "auth_token",
    })

    def __init__(self):
        self.recorded_entries: list[Any] = []

    def inspect_and_record(self, entry: Any) -> None:
        self.assert_no_leakage(entry)
        self.recorded_entries.append(entry)

    @staticmethod
    def assert_no_leakage(data: Any, path: str = "$") -> None:
        if data is None or isinstance(data, (int, str, bool, float)):
            return
        if hasattr(data, "shape") or hasattr(data, "dtype"):
            raise ValueError(f"Pixel/buffer leakage rejected at {path}: found array buffer")
        if isinstance(data, (bytes, bytearray, memoryview)):
            if len(data) > 64:
                raise ValueError(f"Raw opaque buffer leakage rejected at {path}: found {len(data)} bytes")
        if isinstance(data, dict):
            for k, v in data.items():
                if k in WriteSpy.PROHIBITED_KEYS:
                    raise ValueError(f"Prohibited volatile key '{k}' (leakage rejected) found at {path}.{k}")
                WriteSpy.assert_no_leakage(v, f"{path}.{k}")
        elif isinstance(data, (list, tuple)):
            for i, item in enumerate(data):
                WriteSpy.assert_no_leakage(item, f"{path}[{i}]")
        elif dataclasses.is_dataclass(data) and not isinstance(data, type):
            for f in dataclasses.fields(data):
                if f.name in WriteSpy.PROHIBITED_KEYS:
                    raise ValueError(
                        f"Prohibited volatile field '{f.name}' (leakage rejected) in dataclass {type(data).__name__}"
                    )
                WriteSpy.assert_no_leakage(getattr(data, f.name), f"{path}.{f.name}")


# @anchor:wa.learn.adapter_protocol.episode_adapter
@runtime_checkable
class EpisodeAdapter(Protocol):
    """Source-neutral, backend-free EpisodeAdapter Protocol.

    Exposes observe_tree with handle and epoch returning Observation,
    observe_frame_digest with observation returning approved metadata,
    submit_t2_batch with commands and trusted context returning Receipt values,
    read_outcomes, and reset with seed, descriptor and checkpoint.
    """
    @property
    def capabilities(self) -> Capabilities:
        ...

    def observe_tree(self, handle: Handle, epoch: int) -> Observation:
        ...

    def observe_frame_digest(self, observation: Observation) -> ApprovedFrameMetadata:
        ...

    def submit_t2_batch(self, commands: Sequence[Command | dict], trusted_context: object) -> list[Receipt]:
        ...

    def read_outcomes(self) -> list[Outcome]:
        ...

    def reset(self, seed: int, descriptor: dict, checkpoint: bytes | None = None) -> ResetResult:
        ...


# @anchor:wa.learn.adapter_protocol.neutral_adapter
class NeutralEpisodeAdapter:
    """Production backend-free, source-neutral EpisodeAdapter implementation.

    Enforces:
    - Launcher evidence establishes capabilities; adapter self-assertion does not.
    - Missing required capabilities raise ValueError (CapabilityError) before any injected I/O.
    - Generations start at >= 1; default handle kinds derive strictly from SUPPORTED_HANDLE_KINDS.
    - Reset fences old generations (stale handle reuse raises FencedGenerationError).
    - Preserves unavailable capability as typed UnsupportedEvidence.
    - Receipts always carry an explicit reason field.
    """
    def __init__(
        self,
        launcher_evidence: LauncherEvidence | dict,
        *,
        initial_handle: Handle | None = None,
        tree_provider: Callable[[Handle, int], Observation] | None = None,
        frame_provider: Callable[[Observation], ApprovedFrameMetadata] | None = None,
        t2_executor: Callable[[Sequence[Command | dict], object], list[Receipt]] | None = None,
        outcomes_reader: Callable[[], list[Outcome]] | None = None,
        reset_handler: Callable[[int, dict, bytes | None], ResetResult] | None = None,
    ):
        self._capabilities = capabilities_from_launcher(launcher_evidence)
        self._generation: int = 1
        if initial_handle is not None:
            if getattr(initial_handle, "kind", None) not in SUPPORTED_HANDLE_KINDS:
                raise ValueError(f"Unsupported handle kind: {getattr(initial_handle, 'kind', None)!r}")
            gen = getattr(initial_handle, "generation", None)
            if type(gen) is not int or gen < 1:
                raise ValueError(f"Handle generation must be an integer >= 1, got {gen!r}")
            self._handle = Handle(
                kind=initial_handle.kind,
                canonical_id=initial_handle.canonical_id,
                generation=initial_handle.generation,
            )
            self._generation = self._handle.generation
        else:
            derived_kind = default_handle_kind(self._capabilities.source_kind)
            self._handle = Handle(
                kind=derived_kind,
                canonical_id="initial",
                generation=self._generation,
            )
        self._epoch: int = 0
        self._tree_provider = tree_provider
        self._frame_provider = frame_provider
        self._t2_executor = t2_executor
        self._outcomes_reader = outcomes_reader
        self._reset_handler = reset_handler
        self._outcomes: list[Outcome] = []

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    @property
    def handle(self) -> Handle:
        return self._handle

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def epoch(self) -> int:
        return self._epoch

    def query_capability(self, cap_name: str) -> bool | UnsupportedEvidence:
        """Query capability; if unsupported, return typed UnsupportedEvidence."""
        if not hasattr(self._capabilities, cap_name):
            return UnsupportedEvidence(
                capability=cap_name,
                reason=f"Unknown capability {cap_name}",
                source_kind=self._capabilities.source_kind,
            )
        supported = getattr(self._capabilities, cap_name)
        if not supported:
            return UnsupportedEvidence(
                capability=cap_name,
                reason=f"Capability '{cap_name}' is not supported by launcher evidence",
                source_kind=self._capabilities.source_kind,
            )
        return True

    def _require_capability(self, cap_name: str) -> None:
        evidence = self.query_capability(cap_name)
        if isinstance(evidence, UnsupportedEvidence):
            raise CapabilityError(
                f"Missing required capability '{cap_name}': {evidence.reason}",
                evidence=evidence,
            )

    def _check_generation_fence(self, handle: Handle) -> None:
        gen = getattr(handle, "generation", None)
        if type(gen) is not int or gen < 1:
            raise FencedGenerationError(f"Invalid handle generation {gen!r}: must be integer >= 1")
        if gen < self._generation:
            raise FencedGenerationError(
                f"Generation fence violation: handle generation {gen} < active generation {self._generation}"
            )
        if gen > self._generation:
            raise FencedGenerationError(
                f"Handle generation {gen} > active generation {self._generation}"
            )
        if handle.canonical_id != self._handle.canonical_id or handle.kind != self._handle.kind:
            raise FencedGenerationError(
                f"Handle identity ({handle.kind}, {handle.canonical_id}) does not match active target ({self._handle.kind}, {self._handle.canonical_id})"
            )

    def observe_tree(self, handle: Handle, epoch: int) -> Observation:
        # Capability check strictly before any validation or injected I/O
        self._require_capability("tree")
        self._check_generation_fence(handle)
        if type(epoch) is not int or epoch < 0:
            raise ValueError("epoch must be a non-negative integer")
        if self._tree_provider is not None:
            obs = self._tree_provider(handle, epoch)
            self._epoch = epoch
            return obs
        raise ValueError("No tree provider configured for observe_tree")

    def observe_frame_digest(self, observation: Observation) -> ApprovedFrameMetadata:
        # Capability check strictly before any validation or injected I/O
        self._require_capability("frame")
        if observation is None or not hasattr(observation, "handle"):
            raise ValueError("Valid Observation is required for observe_frame_digest")
        self._check_generation_fence(observation.handle)
        if self._frame_provider is not None:
            meta = self._frame_provider(observation)
            WriteSpy.assert_no_leakage(meta)
            return meta
        frame_ref = observation.frame
        if frame_ref is None or not hasattr(frame_ref, "digest"):
            raise ValueError("Observation contains no frame digest")
        meta = ApprovedFrameMetadata(
            digest=frame_ref.digest,
            width=frame_ref.width,
            height=frame_ref.height,
            epoch=observation.epoch,
            frame_seq=observation.frame_seq,
            dpr_ppm=observation.dpr_ppm,
            source_kind=self._capabilities.source_kind,
        )
        WriteSpy.assert_no_leakage(meta)
        return meta

    def submit_t2_batch(self, commands: Sequence[Command | dict], trusted_context: object) -> list[Receipt]:
        # Capability check strictly before any validation or injected I/O
        self._require_capability("t2")
        if trusted_context is None:
            raise ValueError("trusted_context is required for submit_t2_batch")
        if self._t2_executor is not None:
            raw_receipts = self._t2_executor(commands, trusted_context)
            normalized: list[Receipt] = []
            for r in raw_receipts:
                if isinstance(r, Receipt):
                    normalized.append(r)
                elif isinstance(r, Mapping):
                    normalized.append(
                        make_receipt(
                            action_id=str(r["action_id"]),
                            handle=r.get("handle", self._handle),
                            status=str(r.get("status", "applied")),
                            reason=r.get("reason", None),
                        )
                    )
                else:
                    raise TypeError(f"Expected Receipt or Mapping from t2_executor, got {type(r).__name__}")
            return normalized
        receipts: list[Receipt] = []
        for cmd in commands:
            if isinstance(cmd, Mapping):
                seq = cmd["seq"]
                reason = cmd.get("reason", None)
                cmd_handle = cmd.get("handle", None)
            else:
                seq = cmd.seq
                reason = getattr(cmd, "reason", None)
                cmd_handle = getattr(cmd, "handle", None)
            if cmd_handle is not None:
                self._check_generation_fence(cmd_handle)
            action_id = f"cmd-{seq}"
            receipts.append(
                make_receipt(
                    action_id=action_id,
                    handle=self._handle,
                    status="applied",
                    reason=reason,
                )
            )
        return receipts

    def read_outcomes(self) -> list[Outcome]:
        # Capability check strictly before any injected I/O
        self._require_capability("observer")
        if self._outcomes_reader is not None:
            return list(self._outcomes_reader())
        return list(self._outcomes)

    def record_outcome(self, outcome: Outcome) -> None:
        self._require_capability("observer")
        if not isinstance(outcome, Outcome):
            raise TypeError(f"Expected Outcome, got {type(outcome).__name__}")
        self._outcomes.append(outcome)

    def reset(self, seed: int, descriptor: dict, checkpoint: bytes | None = None) -> ResetResult:
        # Capability check strictly before any state mutation or injected I/O
        self._require_capability("reset")
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        if not isinstance(descriptor, Mapping):
            raise ValueError("descriptor must be a mapping")
        if checkpoint is not None and not isinstance(checkpoint, (bytes, bytearray, memoryview)):
            raise ValueError("checkpoint must be bytes or None")
        # Fence old generation: increment generation monotonically (starting from >= 1 -> >= 2)
        self._generation += 1
        new_handle = Handle(
            kind=self._handle.kind,
            canonical_id=self._handle.canonical_id,
            generation=self._generation,
        )
        self._handle = new_handle
        self._epoch = 0
        if self._reset_handler is not None:
            return self._reset_handler(seed, dict(descriptor), None if checkpoint is None else bytes(checkpoint))
        state_dict = descriptor.get("state", dict(descriptor))
        canonical_state_bytes = schema.dumps(state_dict).encode("utf-8")
        state_sha = hashlib.sha256(canonical_state_bytes).hexdigest()
        build_id = str(descriptor.get("build_id", f"build-{seed}-{self._generation}"))
        content_id = str(descriptor.get("content_id", f"content-{seed}"))
        schema_id = str(descriptor.get("schema", schema.SCHEMA_ID))
        trusted_ref = str(descriptor.get("trusted_context_ref", f"trusted-ctx-{seed}-{self._generation}"))
        presentation_digest = descriptor.get("presentation_digest", None)
        return ResetResult(
            initial_state_sha256=state_sha,
            build_id=build_id,
            content_id=content_id,
            schema_id=schema_id,
            trusted_context_ref=trusted_ref,
            presentation_digest=presentation_digest,
        )
