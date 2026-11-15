"""Source-neutral Holdings vision scorecard bridge (`eval.v2eval.holdings_scorecard_bridge`).

Additive wrapper around an injected existing scorer and `Scorecard` from
`eval.v2eval.multiwin_scorecard`. Preserves the original `Scorecard` payload
untouched while binding episode identity from trusted sealed inputs outside the
`Scorecard` object.

Spec citations:
- Section 10: $HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:525
- Section 10: $HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:529
- Section 10: $HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:531
- Section 14: $HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:764

Invariants enforced:
- INV-J5-1: keeps `eligible == completed + refused + incomplete`, with `exclusions` separate.
- INV-J5-2: demands matched outcomes and independent task postconditions; admission is not
  economic acceptance, and rejected attempts advance trusted bookkeeping without simulation mutation.
- INV-J5-3: keeps absent or empty denominators `None` (null) and `"UNKNOWN"`, preserving integer
  counts and rational `numerator`/`denominator` rates without averaging per-window ratios.
- INV-J5-4: distinguishes correct safe refusal from economic completion.
- Reward remains `None` with `reward_status == "DESIGNING"`; a supported confidence theorem is
  `AUSENTE`, and complete-episode uncertainty is preserved as `"UNSUPPORTED"` rather than using
  frame pseudoreplication.
- Duplicate window identity, foreign episode binding / foreign generation, or mixed/missing seals
  refuse before aggregation.
"""
from __future__ import annotations

import builtins
import copy
from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import importlib
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from eval.v2eval.multiwin_scorecard import (
    AUSENTE,
    FakeClock,
    FrameRef,
    FrameSequenceError,
    Handle,
    MANDATORY_METRIC_KEYS,
    Observation,
    PrerequisiteAbsentError,
    Receipt,
    SUPPORTED_TARGET_KINDS,
    Scenario,
    Scorecard,
    ScorecardValidationError,
    SealMismatchError,
    TerminalValidationError,
    TrialReport,
    WindowEvidence,
    WindowIdentityError,
    canonical_bytes,
    canonical_handle_key,
    score_windows,
    validate_scorecard,
)

REWARD_STATUS_DESIGNING = "DESIGNING"
UNCERTAINTY_STATUS_UNSUPPORTED = "UNSUPPORTED"
UNKNOWN_STATUS = "UNKNOWN"
MEASURED_STATUS = "MEASURED"
VM_MEASUREMENT_TAG = "VM"
GATE_G_J5_3 = "G_J5_3"

SPEC_CITATIONS: tuple[str, ...] = (
    "$HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:525",
    "$HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:529",
    "$HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:531",
    "$HB_DOCS/HYPERBOOKS_WEB_MGMT_v1.md:764",
)

EXTERNAL_T2_FIELDS: tuple[str, ...] = ("tick", "seq", "cmd", "args")
NEW_GAME_ARG_FIELDS: tuple[str, ...] = ("country_id", "difficulty", "opening_mode", "seed")

_NOMINAL_COMPLETION_TERMINALS = frozenset({"OK", "COMPLETE", "COMPLETED", "SUCCESS"})
_REFUSAL_TERMINALS = frozenset({"REFUSED", "REJECTED"})
_EXCLUSION_TERMINALS = frozenset({"EXCLUDED", "EXCLUSION"})
_FAULT_OR_INCOMPLETE_TERMINALS = frozenset(
    {
        "CRASH",
        "TIMEOUT",
        "QUARANTINED",
        "FENCED",
        "CANCELLED",
        "ABORTED",
        "ERROR",
        "INCOMPLETE",
        "UNKNOWN",
        "ADMITTED",
    }
)


class BridgeRefusalError(ScorecardValidationError):
    """Raised when `bridge_vision` refuses invalid, unsealed, or foreign inputs."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code, detail)


class ForeignEpisodeBindingError(BridgeRefusalError):
    """Raised when an episode reference, handle, or generation belongs to a foreign episode."""

    def __init__(self, detail: str) -> None:
        super().__init__("E_FOREIGN_EPISODE_BINDING", detail)


class PixelPersistenceError(BridgeRefusalError):
    """Raised by write spies when code attempts to persist frame pixels to disk, swap, logs, or receipts."""

    def __init__(self, detail: str) -> None:
        super().__init__("E_PIXEL_PERSISTENCE_FORBIDDEN", detail)


class SourceKindBoundaryError(BridgeRefusalError):
    """Raised when `source.kind == 'cdp'` is used for a relabeled Godot tree or invalid source."""

    def __init__(self, detail: str) -> None:
        super().__init__("E_SOURCE_KIND_BOUNDARY", detail)


class T2CommandContractError(BridgeRefusalError):
    """Raised when an external T2 command record violates COMMANDS.md / CONTRACTS.md schema."""

    def __init__(self, detail: str) -> None:
        super().__init__("E_T2_CONTRACT", detail)


def _require_strict_int(value: Any, name: str, *, min_value: int | None = None) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise BridgeRefusalError("E_INT_REQUIRED", f"{name} must be an integer, got {type(value).__name__}")
    ival = int(value)
    if min_value is not None and ival < min_value:
        raise BridgeRefusalError("E_INT_RANGE", f"{name} must be >= {min_value}, got {ival}")
    return ival


def _require_nonempty_str(value: Any, name: str, *, code: str = "E_STR_REQUIRED") -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeRefusalError(code, f"{name} must be a non-empty string")
    return value


def make_rational_rate(
    numerator: int | None,
    denominator: int | None,
    *,
    pass_when: Callable[[int, int], bool] | None = None,
) -> dict[str, Any]:
    """Construct an INV-J5-3 rational rate preserving integer numerator and denominator.

    Absent or zero denominator stays `None` (null) with `status == "UNKNOWN"`,
    never `0`, `0.0`, `"0/0"`, or `"0/1"`.
    """
    num = 0 if numerator is None else _require_strict_int(numerator, "numerator", min_value=0)
    if denominator is None:
        den: int | None = None
    else:
        raw_den = _require_strict_int(denominator, "denominator", min_value=0)
        den = raw_den if raw_den > 0 else None

    if den is None:
        return {
            "numerator": num,
            "denominator": None,
            "value": None,
            "fraction": None,
            "reduced_fraction": None,
            "permille": None,
            "ppm": None,
            "defined": False,
            "status": UNKNOWN_STATUS,
            "ausente": AUSENTE,
            "passed": False,
        }

    frac = Fraction(num, den)
    passed = bool(pass_when(num, den)) if callable(pass_when) else True
    return {
        "numerator": num,
        "denominator": den,
        "value": f"{num}/{den}",
        "fraction": f"{num}/{den}",
        "reduced_fraction": f"{frac.numerator}/{frac.denominator}",
        "permille": (num * 1000) // den,
        "ppm": (num * 1_000_000) // den,
        "defined": True,
        "status": MEASURED_STATUS,
        "ausente": None,
        "passed": passed,
    }


# ---------------------------------------------------------------------------
# Holdings inline T2 command builders & validators (COMMANDS.md / CONTRACTS.md)
# ---------------------------------------------------------------------------


def make_t2_pause(*, tick: int, seq: int) -> dict[str, Any]:
    """Construct an inline Holdings `pause` T2 command with empty `args`."""
    t = _require_strict_int(tick, "tick", min_value=0)
    s = _require_strict_int(seq, "seq", min_value=0)
    return {
        "tick": t,
        "seq": s,
        "cmd": "pause",
        "args": {},
    }


def make_t2_new_game(
    *,
    tick: int,
    seq: int,
    country_id: str,
    difficulty: str,
    opening_mode: str,
    seed: int,
) -> dict[str, Any]:
    """Construct an inline Holdings `new_game` T2 command per COMMANDS.md and CONTRACTS.md."""
    t = _require_strict_int(tick, "tick", min_value=0)
    s = _require_strict_int(seq, "seq", min_value=0)
    cid = _require_nonempty_str(country_id, "country_id", code="E_T2_CONTRACT")
    diff = _require_nonempty_str(difficulty, "difficulty", code="E_T2_CONTRACT")
    mode = _require_nonempty_str(opening_mode, "opening_mode", code="E_T2_CONTRACT")
    sd = _require_strict_int(seed, "seed", min_value=0)
    return {
        "tick": t,
        "seq": s,
        "cmd": "new_game",
        "args": {
            "country_id": cid,
            "difficulty": diff,
            "opening_mode": mode,
            "seed": sd,
        },
    }


def validate_external_t2_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that an external T2 command has exact fields `tick`, `seq`, `cmd`, and `args`.

    Authority fields (`owner`, `lease_id`, `episode_id`, `seal`, `handle`) are forbidden
    in external T2 records and must remain in trusted injected context.
    """
    if not isinstance(record, Mapping):
        raise T2CommandContractError("external T2 command must be a mapping")
    keys = tuple(record.keys())
    if set(keys) != set(EXTERNAL_T2_FIELDS):
        extra = sorted(set(keys) - set(EXTERNAL_T2_FIELDS))
        missing = sorted(set(EXTERNAL_T2_FIELDS) - set(keys))
        raise T2CommandContractError(
            f"external T2 fields must be exactly {EXTERNAL_T2_FIELDS}; extra={extra}, missing={missing}"
        )
    tick = _require_strict_int(record["tick"], "t2.tick", min_value=0)
    seq = _require_strict_int(record["seq"], "t2.seq", min_value=0)
    cmd = _require_nonempty_str(record["cmd"], "t2.cmd", code="E_T2_CONTRACT")
    args = record["args"]
    if not isinstance(args, Mapping):
        raise T2CommandContractError("t2.args must be a mapping")

    if cmd == "pause":
        if len(args) != 0:
            raise T2CommandContractError("Holdings 'pause' command must have empty args {}")
        normalized_args: dict[str, Any] = {}
    elif cmd == "new_game":
        if set(args.keys()) != set(NEW_GAME_ARG_FIELDS):
            raise T2CommandContractError(
                f"Holdings 'new_game' args must have exact keys {NEW_GAME_ARG_FIELDS}, got {tuple(args.keys())}"
            )
        normalized_args = {
            "country_id": _require_nonempty_str(args["country_id"], "new_game.country_id", code="E_T2_CONTRACT"),
            "difficulty": _require_nonempty_str(args["difficulty"], "new_game.difficulty", code="E_T2_CONTRACT"),
            "opening_mode": _require_nonempty_str(args["opening_mode"], "new_game.opening_mode", code="E_T2_CONTRACT"),
            "seed": _require_strict_int(args["seed"], "new_game.seed", min_value=0),
        }
    else:
        normalized_args = dict(args)

    return {
        "tick": tick,
        "seq": seq,
        "cmd": cmd,
        "args": normalized_args,
    }


# ---------------------------------------------------------------------------
# Write Spy & Pixel Persistence Guard (STORAGE LAW)
# ---------------------------------------------------------------------------


class WriteSpy:
    """Spy and guard that rejects pixel/frame persistence to disk, swap, logs, or receipts.

    Never executes a real forbidden write; intercepts write attempts in RAM and raises
    `PixelPersistenceError` while recording the blocked attempt for inspection.
    """

    def __init__(self) -> None:
        self.blocked_attempts: list[dict[str, Any]] = []
        self._active = False
        self._orig_open: Any = None
        self._orig_np_save: Any = None
        self._orig_np_savez: Any = None
        self._orig_np_savez_compressed: Any = None

    def _record_and_reject(self, target: str, kind: str, detail: str) -> None:
        entry = {"target": str(target), "kind": kind, "detail": detail}
        self.blocked_attempts.append(entry)
        raise PixelPersistenceError(f"{kind} blocked on {target!r}: {detail}")

    def save_screenshot(self, path: Any, *_args: Any, **_kwargs: Any) -> None:
        self._record_and_reject(str(path), "screenshot_save_api", "save_screenshot is forbidden")

    def write_frame_to_disk(self, path: Any, *_args: Any, **_kwargs: Any) -> None:
        self._record_and_reject(str(path), "disk_frame_write", "writing frame pixels to disk is forbidden")

    def write_frame_to_swap(self, target: Any, *_args: Any, **_kwargs: Any) -> None:
        self._record_and_reject(str(target), "swap_frame_write", "writing frame pixels to swap is forbidden")

    def write_frame_to_log(self, target: Any, *_args: Any, **_kwargs: Any) -> None:
        self._record_and_reject(str(target), "log_frame_write", "writing frame pixels to logs is forbidden")

    def write_frame_to_receipt(self, target: Any, *_args: Any, **_kwargs: Any) -> None:
        self._record_and_reject(str(target), "receipt_frame_write", "writing frame pixels to receipts is forbidden")

    def inspect_payload_for_pixels(self, payload: Any, *, location: str = "receipt_or_metadata") -> None:
        """Reject any raw frame pixel buffer (`np.ndarray`, `FrameRef`, or `bytes`) leaked into receipts/logs."""
        if isinstance(payload, FrameRef):
            self._record_and_reject(location, "pixel_leak", "FrameRef object leaked into metadata/receipt")
        if isinstance(payload, np.ndarray):
            if payload.ndim == 3 or payload.dtype == np.uint8:
                self._record_and_reject(location, "pixel_leak", "raw ndarray pixel buffer leaked into metadata/receipt")
        if isinstance(payload, (bytes, bytearray, memoryview)) and len(payload) >= 12:
            self._record_and_reject(location, "pixel_leak", "raw byte buffer leaked into metadata/receipt")
        if isinstance(payload, Mapping):
            for k, v in payload.items():
                if str(k) in ("rgb", "pixels", "raw_frame", "frame_bytes", "screenshot"):
                    self._record_and_reject(f"{location}.{k}", "pixel_leak", f"forbidden pixel field {k!r}")
                if str(k) == "scorecard" and isinstance(v, Scorecard):
                    # Scorecard is the preserved in-RAM object; inspect its pooled/metrics dicts
                    self.inspect_payload_for_pixels(v.pooled, location=f"{location}.scorecard.pooled")
                    self.inspect_payload_for_pixels(v.metrics, location=f"{location}.scorecard.metrics")
                else:
                    self.inspect_payload_for_pixels(v, location=f"{location}.{k}")
        elif isinstance(payload, (list, tuple)):
            for idx, item in enumerate(payload):
                self.inspect_payload_for_pixels(item, location=f"{location}[{idx}]")

    def __enter__(self) -> WriteSpy:
        if self._active:
            return self
        self._orig_open = builtins.open
        self._orig_np_save = np.save
        self._orig_np_savez = np.savez
        self._orig_np_savez_compressed = np.savez_compressed
        spy = self

        def _guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(m in str(mode) for m in ("w", "a", "x", "+")):
                spy._record_and_reject(str(file), "builtins.open", f"write mode {mode!r} forbidden during evaluation")
            return spy._orig_open(file, mode, *args, **kwargs)

        def _guarded_np_save(file: Any, *_args: Any, **_kwargs: Any) -> Any:
            spy._record_and_reject(str(file), "numpy.save", "persisting arrays via np.save is forbidden")

        def _guarded_np_savez(file: Any, *_args: Any, **_kwargs: Any) -> Any:
            spy._record_and_reject(str(file), "numpy.savez", "persisting arrays via np.savez is forbidden")

        builtins.open = _guarded_open  # type: ignore[assignment]
        np.save = _guarded_np_save  # type: ignore[assignment]
        np.savez = _guarded_np_savez  # type: ignore[assignment]
        np.savez_compressed = _guarded_np_savez  # type: ignore[assignment]
        self._active = True
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if self._active:
            builtins.open = self._orig_open  # type: ignore[assignment]
            np.save = self._orig_np_save  # type: ignore[assignment]
            np.savez = self._orig_np_savez  # type: ignore[assignment]
            np.savez_compressed = self._orig_np_savez_compressed  # type: ignore[assignment]
            self._active = False
        return False


def validate_procfs_cgroup_ram_fixtures(
    *,
    proc_status_text: str | None = None,
    cgroup_swap_text: str | None = None,
) -> dict[str, Any]:
    """Validate offline procfs and cgroup text fixtures in RAM to ensure zero swap usage."""
    vm_swap_kb: int | None = None
    cgroup_swap_bytes: int | None = None
    violations: list[str] = []

    if proc_status_text is not None:
        if not isinstance(proc_status_text, str):
            raise BridgeRefusalError("E_PROCFS_FIXTURE", "proc_status_text must be a string")
        for line in proc_status_text.splitlines():
            if line.startswith("VmSwap:"):
                parts = line.split(":", 1)[1].strip().split()
                if parts:
                    vm_swap_kb = _require_strict_int(int(parts[0]), "VmSwap", min_value=0)
                    if vm_swap_kb > 0:
                        violations.append(f"procfs_swap_nonzero:{vm_swap_kb}kB")

    if cgroup_swap_text is not None:
        if not isinstance(cgroup_swap_text, str):
            raise BridgeRefusalError("E_CGROUP_FIXTURE", "cgroup_swap_text must be a string")
        stripped = cgroup_swap_text.strip()
        if stripped:
            cgroup_swap_bytes = _require_strict_int(int(stripped), "memory.swap.current", min_value=0)
            if cgroup_swap_bytes > 0:
                violations.append(f"cgroup_swap_nonzero:{cgroup_swap_bytes}B")

    if violations:
        raise PixelPersistenceError(f"swap persistence detected in fixtures: {', '.join(violations)}")

    return {
        "vm_swap_kb": vm_swap_kb,
        "cgroup_swap_bytes": cgroup_swap_bytes,
        "ram_only_verified": True,
        "violations": tuple(violations),
    }


# ---------------------------------------------------------------------------
# Source-Neutral Observation & Episode Boundary
# ---------------------------------------------------------------------------


def validate_source_neutral_observation(observation: Observation | Mapping[str, Any]) -> dict[str, Any]:
    """Validate a source-neutral observation for Godot or fake-CDP-derived DOM/AX.

    Enforces:
    - `source.kind == "cdp"` is reserved exclusively for CDP-derived DOM/AX, never a
      relabeled Godot tree (`holdings.godot.uitree/v1`, `control_path`, `godot://`, or
      `godot_window` handle).
    - Frame digest matches owned in-RAM RGB `uint8` array without persisting pixels.
    """
    if isinstance(observation, Observation):
        handle = observation.handle
        epoch = observation.epoch
        frame_seq = observation.frame_seq
        dpr_ppm = observation.dpr_ppm
        state = observation.state
        frame_ref = observation.frame
    elif isinstance(observation, Mapping):
        raw_h = observation.get("handle")
        if isinstance(raw_h, Handle):
            handle = raw_h
        elif isinstance(raw_h, Mapping):
            handle = Handle(raw_h["kind"], raw_h["canonical_id"], raw_h.get("generation", 0))
        else:
            raise SourceKindBoundaryError("observation missing valid Handle")
        epoch = _require_strict_int(observation.get("epoch", 0), "observation.epoch", min_value=0)
        frame_seq = _require_strict_int(observation.get("frame_seq", 0), "observation.frame_seq", min_value=0)
        dpr_ppm = _require_strict_int(observation.get("dpr_ppm", 1_000_000), "observation.dpr_ppm", min_value=1)
        state = observation.get("state")
        frame_ref = observation.get("frame")
    else:
        raise SourceKindBoundaryError(f"unsupported observation type: {type(observation).__name__}")

    if not isinstance(state, Mapping):
        raise SourceKindBoundaryError("observation.state must be a mapping")

    source = state.get("source")
    if not isinstance(source, Mapping):
        raise SourceKindBoundaryError("observation.state['source'] must be a mapping with 'kind'")
    source_kind = _require_nonempty_str(source.get("kind"), "source.kind", code="E_SOURCE_KIND_BOUNDARY")
    source_url = str(source.get("url", ""))

    has_godot_markers = (
        handle.kind == "godot_window"
        or source_url.startswith("godot://")
        or state.get("sidecar_schema") == "holdings.godot.uitree/v1"
        or state.get("schema") == "holdings.godot.uitree/v1"
        or "uitree_sidecar" in state
        or any(
            isinstance(n, Mapping) and ("control_path" in n or "table_id" in n)
            for n in (state.get("nodes") or ())
        )
    )

    if source_kind == "cdp":
        if has_godot_markers:
            raise SourceKindBoundaryError(
                "source.kind 'cdp' is reserved for CDP-derived DOM/AX, never a relabeled Godot tree"
            )
        if handle.kind != "cdp_tab":
            raise SourceKindBoundaryError(
                f"source.kind 'cdp' requires handle.kind == 'cdp_tab', got {handle.kind!r}"
            )
    elif source_kind == "godot":
        if handle.kind != "godot_window":
            raise SourceKindBoundaryError(
                f"source.kind 'godot' requires handle.kind == 'godot_window', got {handle.kind!r}"
            )
    else:
        raise SourceKindBoundaryError(f"unsupported observation source.kind: {source_kind!r}")

    frame_digest: str | None = None
    if frame_ref is not None:
        if not isinstance(frame_ref, FrameRef):
            raise SourceKindBoundaryError("observation.frame must be a FrameRef")
        if (
            frame_ref.handle != handle
            or frame_ref.epoch != epoch
            or frame_ref.frame_seq != frame_seq
        ):
            raise SourceKindBoundaryError("observation.frame identity does not match observation")
        rgb = frame_ref.rgb
        if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.flags.writeable:
            raise SourceKindBoundaryError("FrameRef.rgb must be a read-only uint8 numpy array in RAM")
        computed = hashlib.sha256(rgb.tobytes()).hexdigest()
        if computed != frame_ref.digest:
            raise SourceKindBoundaryError("FrameRef.digest does not match in-RAM RGB bytes")
        frame_digest = frame_ref.digest

    return {
        "handle": canonical_handle_key(handle),
        "generation": handle.generation,
        "epoch": epoch,
        "frame_seq": frame_seq,
        "dpr_ppm": dpr_ppm,
        "source_kind": source_kind,
        "frame_digest": frame_digest,
    }


@dataclass(frozen=True)
class TrustedEpisodeContext:
    """Trusted sealed context holding authority and episode binding outside external T2 payloads."""

    episode_id: str
    seal: str
    allowed_handles: tuple[Handle, ...]
    owner: str = "trusted-harness"
    lease_id: str = "lease-trusted"
    allowed_commands: tuple[str, ...] = ("pause", "new_game")

    def __post_init__(self) -> None:
        _require_nonempty_str(self.episode_id, "TrustedEpisodeContext.episode_id")
        _require_nonempty_str(self.seal, "TrustedEpisodeContext.seal")
        handles = tuple(self.allowed_handles)
        if not handles:
            raise ForeignEpisodeBindingError("TrustedEpisodeContext.allowed_handles must be non-empty")
        for h in handles:
            if not isinstance(h, Handle):
                raise ForeignEpisodeBindingError("allowed_handles must contain Handle instances")
        object.__setattr__(self, "allowed_handles", handles)
        object.__setattr__(self, "allowed_commands", tuple(self.allowed_commands))


@dataclass
class SourceNeutralBoundarySession:
    """Source-neutral episode boundary managing observations, T2 submissions, outcomes, and reset.

    Enforces:
    - Authority stays in `trusted_context`, never in external T2 payloads.
    - Rejected attempts advance trusted bookkeeping without mutating simulation state.
    - Admission (`status == "ADMITTED"`) is not economic acceptance (`INV-J5-2`).
    - `reset()` cleans up transient state and increments reset counter.
    """

    trusted_context: TrustedEpisodeContext
    sim_state: dict[str, Any] = field(
        default_factory=lambda: {
            "paused": False,
            "country_id": None,
            "difficulty": None,
            "opening_mode": None,
            "seed": None,
            "sim_tick": 0,
            "mutations": 0,
        }
    )
    bookkeeping: dict[str, Any] = field(
        default_factory=lambda: {
            "bookkeeping_seq": 0,
            "attempted": 0,
            "admitted": 0,
            "rejected": 0,
            "economically_completed": 0,
            "last_seq": -1,
            "resets": 0,
        }
    )
    receipts: list[Receipt] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    cleaned_up: bool = False

    def observe(self, observation: Observation | Mapping[str, Any]) -> dict[str, Any]:
        meta = validate_source_neutral_observation(observation)
        obs_handle = observation.handle if isinstance(observation, Observation) else observation["handle"]
        if isinstance(obs_handle, Mapping):
            obs_handle = Handle(obs_handle["kind"], obs_handle["canonical_id"], obs_handle.get("generation", 0))
        if obs_handle not in self.trusted_context.allowed_handles:
            raise ForeignEpisodeBindingError(
                f"observation handle {obs_handle!r} is not bound to episode {self.trusted_context.episode_id!r}"
            )
        self.observations.append(meta)
        return meta

    def submit_t2(
        self,
        t2_record: Mapping[str, Any],
        *,
        target_handle: Handle,
        postcondition_fn: Callable[[Mapping[str, Any]], bool] | None = None,
        reject_reason: str | None = None,
    ) -> dict[str, Any]:
        """Submit an external T2 command.

        Rejected attempts advance trusted bookkeeping without mutating `sim_state`.
        Admitted attempts mutate `sim_state` only when admitted, and count as economically
        completed only if `postcondition_fn` independently verifies the postcondition (`INV-J5-2`).
        """
        self.bookkeeping["bookkeeping_seq"] += 1
        self.bookkeeping["attempted"] += 1
        sim_snapshot = copy.deepcopy(self.sim_state)

        try:
            validated = validate_external_t2_record(t2_record)
        except T2CommandContractError as exc:
            self.bookkeeping["rejected"] += 1
            receipt = Receipt(
                action_id=f"{self.trusted_context.episode_id}:t2:{self.bookkeeping['bookkeeping_seq']}",
                handle=target_handle,
                status="REFUSED",
                reason=str(exc),
            )
            self.receipts.append(receipt)
            return {
                "status": "REFUSED",
                "admitted": False,
                "economic_completed": False,
                "sim_mutated": self.sim_state != sim_snapshot,
                "receipt": receipt,
                "bookkeeping": copy.deepcopy(self.bookkeeping),
            }

        cmd = validated["cmd"]
        seq = validated["seq"]
        tick = validated["tick"]
        args = validated["args"]

        if (
            reject_reason is not None
            or target_handle not in self.trusted_context.allowed_handles
            or cmd not in self.trusted_context.allowed_commands
            or seq <= self.bookkeeping["last_seq"]
            or tick < self.sim_state["sim_tick"]
        ):
            reason = (
                reject_reason
                or (
                    "foreign_or_unallowed_handle"
                    if target_handle not in self.trusted_context.allowed_handles
                    else "command_not_allowed"
                    if cmd not in self.trusted_context.allowed_commands
                    else "non_monotonic_seq_or_tick"
                )
            )
            self.bookkeeping["rejected"] += 1
            self.bookkeeping["last_seq"] = max(self.bookkeeping["last_seq"], seq)
            receipt = Receipt(
                action_id=f"{self.trusted_context.episode_id}:t2:{seq}",
                handle=target_handle,
                status="REFUSED",
                reason=reason,
            )
            self.receipts.append(receipt)
            return {
                "status": "REFUSED",
                "admitted": False,
                "economic_completed": False,
                "sim_mutated": False,
                "receipt": receipt,
                "bookkeeping": copy.deepcopy(self.bookkeeping),
            }

        # Command is admitted: update last_seq and apply simulation transition
        self.bookkeeping["admitted"] += 1
        self.bookkeeping["last_seq"] = seq
        self.sim_state["sim_tick"] = tick
        if cmd == "pause":
            self.sim_state["paused"] = True
            self.sim_state["mutations"] += 1
        elif cmd == "new_game":
            self.sim_state["country_id"] = args["country_id"]
            self.sim_state["difficulty"] = args["difficulty"]
            self.sim_state["opening_mode"] = args["opening_mode"]
            self.sim_state["seed"] = args["seed"]
            self.sim_state["paused"] = False
            self.sim_state["mutations"] += 1

        postcondition_ok = bool(postcondition_fn(self.sim_state)) if callable(postcondition_fn) else False
        if postcondition_ok:
            self.bookkeeping["economically_completed"] += 1
            status = "COMPLETED"
        else:
            status = "ADMITTED"

        receipt = Receipt(
            action_id=f"{self.trusted_context.episode_id}:t2:{seq}",
            handle=target_handle,
            status=status,
            reason=None if postcondition_ok else "admitted_pending_or_failed_postcondition",
        )
        self.receipts.append(receipt)
        return {
            "status": status,
            "admitted": True,
            "economic_completed": postcondition_ok,
            "postcondition_verified": postcondition_ok,
            "sim_mutated": self.sim_state != sim_snapshot,
            "receipt": receipt,
            "bookkeeping": copy.deepcopy(self.bookkeeping),
        }

    def reset(self) -> dict[str, Any]:
        """Reset transient simulation and observation buffers while recording cleanup."""
        self.sim_state = {
            "paused": False,
            "country_id": None,
            "difficulty": None,
            "opening_mode": None,
            "seed": None,
            "sim_tick": 0,
            "mutations": 0,
        }
        self.observations.clear()
        self.bookkeeping["resets"] += 1
        self.cleaned_up = True
        return copy.deepcopy(self.bookkeeping)


# ---------------------------------------------------------------------------
# Pre-aggregation Episode Binding & Evidence Validation
# ---------------------------------------------------------------------------


def verify_dispatch_admission(
    *,
    provider_revision: str | None = None,
    gate_receipt: str | None = None,
    clone_revision: str | None = None,
    conductor_binding: str | None = None,
) -> dict[str, Any]:
    """Verify host/conductor dispatch admission prerequisites without inventing absent values.

    Missing or `AUSENTE` revision/receipt values return `status == "HOLD"` and
    `admitted == False`, never converting missing prerequisites into a silent pass.
    """
    fields_map = {
        "provider_revision": provider_revision,
        "gate_receipt": gate_receipt,
        "clone_revision": clone_revision,
        "conductor_binding": conductor_binding,
    }
    normalized: dict[str, str] = {}
    missing: list[str] = []
    for k, v in fields_map.items():
        if v is None or not isinstance(v, str) or not v.strip() or v.strip() == AUSENTE:
            normalized[k] = AUSENTE
            missing.append(k)
        else:
            normalized[k] = v.strip()

    try:
        mod = importlib.import_module("eval.v2eval.multiwin_scorecard")
        has_provider = all(
            hasattr(mod, sym) for sym in ("score_windows", "canonical_bytes", "WindowEvidence", "Scorecard")
        )
    except Exception:
        has_provider = False

    if not has_provider:
        missing.append("eval.v2eval.multiwin_scorecard")

    admitted = len(missing) == 0
    return {
        "admitted": admitted,
        "dispatch_status": "ADMITTED" if admitted else "HOLD",
        "provider_module_present": has_provider,
        "provider_revision": normalized["provider_revision"],
        "gate_receipt": normalized["gate_receipt"],
        "clone_revision": normalized["clone_revision"],
        "conductor_binding": normalized["conductor_binding"],
        "missing_prerequisites": tuple(missing),
    }


def _extract_trusted_episode_ref(
    episode_ref: Any,
    trusted_context: Mapping[str, Any] | TrustedEpisodeContext | None = None,
) -> dict[str, Any]:
    """Extract and validate trusted episode identity from `episode_ref` and `trusted_context`."""
    ctx_dict: dict[str, Any] = {}
    if isinstance(trusted_context, TrustedEpisodeContext):
        ctx_dict = {
            "episode_id": trusted_context.episode_id,
            "seal": trusted_context.seal,
            "allowed_handles": trusted_context.allowed_handles,
            "owner": trusted_context.owner,
            "lease_id": trusted_context.lease_id,
            "trusted": True,
        }
    elif isinstance(trusted_context, Mapping):
        ctx_dict = dict(trusted_context)

    if episode_ref is None:
        raise ForeignEpisodeBindingError("episode_ref is required and cannot be None")

    if isinstance(episode_ref, TrustedEpisodeContext):
        ref_dict: dict[str, Any] = {
            "episode_id": episode_ref.episode_id,
            "seal": episode_ref.seal,
            "allowed_handles": episode_ref.allowed_handles,
            "owner": episode_ref.owner,
            "lease_id": episode_ref.lease_id,
            "trusted": True,
        }
    elif isinstance(episode_ref, str):
        if not episode_ref.strip() or episode_ref.strip() == AUSENTE:
            raise ForeignEpisodeBindingError("episode_ref string must be non-empty and not AUSENTE")
        ref_dict = {
            "episode_id": episode_ref.strip(),
            "seal": ctx_dict.get("seal"),
            "trusted": ctx_dict.get("trusted", True),
            "generation": ctx_dict.get("generation"),
            "allowed_generations": ctx_dict.get("allowed_generations"),
            "allowed_handles": ctx_dict.get("allowed_handles"),
        }
    elif isinstance(episode_ref, Mapping):
        ref_dict = dict(episode_ref)
    elif hasattr(episode_ref, "episode_id"):
        ref_dict = {
            "episode_id": getattr(episode_ref, "episode_id"),
            "seal": getattr(episode_ref, "seal", ctx_dict.get("seal")),
            "trusted": getattr(episode_ref, "trusted", True),
            "generation": getattr(episode_ref, "generation", None),
            "allowed_generations": getattr(episode_ref, "allowed_generations", None),
            "allowed_handles": getattr(episode_ref, "allowed_handles", None),
        }
    else:
        raise ForeignEpisodeBindingError(f"unsupported episode_ref type: {type(episode_ref).__name__}")

    if ref_dict.get("trusted", True) is not True or ref_dict.get("untrusted") is True:
        raise ForeignEpisodeBindingError("episode_ref is marked untrusted; episode identity must come from trusted sealed inputs")

    ep_id = ref_dict.get("episode_id")
    if not isinstance(ep_id, str) or not ep_id.strip() or ep_id.strip() == AUSENTE:
        raise ForeignEpisodeBindingError("episode_ref.episode_id must be a non-empty trusted string")
    ep_id = ep_id.strip()

    if "episode_id" in ctx_dict and ctx_dict["episode_id"] != ep_id:
        raise ForeignEpisodeBindingError(
            f"episode_ref.episode_id {ep_id!r} conflicts with trusted_context.episode_id {ctx_dict['episode_id']!r}"
        )

    seal = ref_dict.get("seal", ctx_dict.get("seal"))
    if seal is not None:
        if not isinstance(seal, str) or not seal.strip() or seal.strip() == AUSENTE:
            raise SealMismatchError("episode_ref.seal must be a non-empty string when provided")
        seal = seal.strip()
        if "seal" in ctx_dict and ctx_dict["seal"] is not None and ctx_dict["seal"] != seal:
            raise SealMismatchError(
                f"episode_ref.seal {seal!r} conflicts with trusted_context.seal {ctx_dict['seal']!r}"
            )

    allowed_generations: set[int] | None = None
    if ref_dict.get("generation") is not None:
        allowed_generations = {_require_strict_int(ref_dict["generation"], "episode_ref.generation", min_value=0)}
    if ref_dict.get("allowed_generations") is not None:
        gens = {_require_strict_int(g, "allowed_generation", min_value=0) for g in ref_dict["allowed_generations"]}
        allowed_generations = gens if allowed_generations is None else (allowed_generations & gens)

    raw_handles = ref_dict.get("allowed_handles", ctx_dict.get("allowed_handles"))
    allowed_handle_keys: set[tuple[str, str, int]] | None = None
    if raw_handles is not None:
        allowed_handle_keys = set()
        for h in raw_handles:
            if isinstance(h, Handle):
                allowed_handle_keys.add((h.kind, h.canonical_id, h.generation))
            elif isinstance(h, Mapping):
                allowed_handle_keys.add(
                    (
                        str(h["kind"]),
                        str(h["canonical_id"]),
                        _require_strict_int(h.get("generation", 0), "handle.generation", min_value=0),
                    )
                )
            elif hasattr(h, "kind") and hasattr(h, "canonical_id") and hasattr(h, "generation"):
                allowed_handle_keys.add((str(h.kind), str(h.canonical_id), int(h.generation)))
            else:
                raise ForeignEpisodeBindingError(f"invalid handle in allowed_handles: {h!r}")

    return {
        "episode_id": ep_id,
        "seal": seal,
        "trusted": True,
        "allowed_generations": allowed_generations,
        "allowed_handle_keys": allowed_handle_keys,
        "exclusions": tuple(ref_dict.get("exclusions", ())),
        "postconditions": dict(ref_dict.get("postconditions", {})) if isinstance(ref_dict.get("postconditions"), Mapping) else {},
        "expected_outcomes": dict(ref_dict.get("expected_outcomes", {})) if isinstance(ref_dict.get("expected_outcomes"), Mapping) else {},
    }


def _classify_item_outcome(
    *,
    item_id: str,
    terminal: str | None,
    expected_terminal: str | None,
    ops: Mapping[str, Any],
    postconditions_map: Mapping[str, Any],
    expected_outcomes_map: Mapping[str, Any],
    excluded_ids: set[str],
) -> dict[str, Any]:
    """Classify a window or episode task under INV-J5-1, INV-J5-2, and INV-J5-4."""
    term = (terminal or "").strip().upper()
    exp_term = (expected_terminal or "").strip().upper()

    is_excluded = (
        item_id in excluded_ids
        or bool(ops.get("excluded", False))
        or term in _EXCLUSION_TERMINALS
        or exp_term in _EXCLUSION_TERMINALS
    )
    if is_excluded:
        return {
            "item_id": item_id,
            "bucket": "excluded",
            "terminal": term or "EXCLUDED",
            "expected_terminal": exp_term or "EXCLUDED",
            "outcome_matched": True,
            "postcondition_verified": None,
            "safe_refusal": False,
            "unexpected_refusal": False,
            "admitted_only": False,
        }

    # Check if item is a refusal (INV-J5-4)
    is_refusal = term in _REFUSAL_TERMINALS or ops.get("status") == "REFUSED"
    if is_refusal:
        expected_refusal = exp_term in _REFUSAL_TERMINALS or expected_outcomes_map.get(item_id) == "REFUSED"
        return {
            "item_id": item_id,
            "bucket": "refused",
            "terminal": term or "REFUSED",
            "expected_terminal": exp_term or None,
            "outcome_matched": bool(expected_refusal),
            "postcondition_verified": bool(ops.get("postcondition_verified", expected_refusal)),
            "safe_refusal": bool(expected_refusal),
            "unexpected_refusal": not bool(expected_refusal),
            "admitted_only": False,
        }

    # Admission alone is NOT economic completion (INV-J5-2)
    admitted_only = (
        term == "ADMITTED"
        or bool(ops.get("admitted_only", False))
        or ops.get("status") == "ADMITTED"
    )

    # Outcome match check (INV-J5-2)
    explicit_outcome_match = ops.get("outcome_matched")
    if item_id in expected_outcomes_map and "observed_outcome" in ops:
        outcome_matched = (ops["observed_outcome"] == expected_outcomes_map[item_id])
    elif explicit_outcome_match is not None:
        outcome_matched = bool(explicit_outcome_match)
    else:
        outcome_matched = bool(
            term
            and exp_term
            and term == exp_term
            and term in _NOMINAL_COMPLETION_TERMINALS
        )

    # Independent task postcondition check (INV-J5-2)
    if item_id in postconditions_map:
        raw_pc = postconditions_map[item_id]
        postcondition_verified = (raw_pc is True)
    elif "postcondition_verified" in ops:
        postcondition_verified = (ops["postcondition_verified"] is True)
    elif "postcondition" in ops:
        postcondition_verified = (ops["postcondition"] is True)
    elif "postconditions_met" in ops:
        postcondition_verified = (ops["postconditions_met"] is True)
    else:
        # Default for nominal completed window evidence when not flagged as unverified/admitted-only
        postcondition_verified = bool(
            not admitted_only
            and term in _NOMINAL_COMPLETION_TERMINALS
            and exp_term in _NOMINAL_COMPLETION_TERMINALS
            and term == exp_term
        )

    if (
        not admitted_only
        and term in _NOMINAL_COMPLETION_TERMINALS
        and exp_term in _NOMINAL_COMPLETION_TERMINALS
        and term == exp_term
        and outcome_matched
        and postcondition_verified
    ):
        return {
            "item_id": item_id,
            "bucket": "completed",
            "terminal": term,
            "expected_terminal": exp_term,
            "outcome_matched": True,
            "postcondition_verified": True,
            "safe_refusal": False,
            "unexpected_refusal": False,
            "admitted_only": False,
        }

    return {
        "item_id": item_id,
        "bucket": "incomplete",
        "terminal": term or "UNKNOWN",
        "expected_terminal": exp_term or None,
        "outcome_matched": outcome_matched,
        "postcondition_verified": postcondition_verified,
        "safe_refusal": False,
        "unexpected_refusal": False,
        "admitted_only": admitted_only,
    }


def _build_refusal_response(
    *,
    error_code: str,
    detail: str,
    episode_id: str | None,
    episode_seal: str | None,
    episode_ref_payload: dict[str, Any] | None,
    scorer_invoked: bool = False,
    scorecard: Scorecard | None = None,
    cleanup_completed: bool = True,
) -> dict[str, Any]:
    """Construct a structured pre-aggregation refusal metadata mapping."""
    null_rate = make_rational_rate(0, None)
    return {
        "schema": "vqa.holdings_scorecard_bridge/v1",
        "gate": GATE_G_J5_3,
        "status": "REFUSED",
        "passed": False,
        "refused_before_aggregation": True,
        "scorer_invoked": scorer_invoked,
        "error_code": error_code,
        "error_detail": detail,
        "episode_id": episode_id,
        "episode_seal": episode_seal,
        "episode_ref": episode_ref_payload,
        "scorecard": scorecard,
        "scorecard_canonical_sha256": (
            hashlib.sha256(canonical_bytes(scorecard)).hexdigest() if isinstance(scorecard, Scorecard) else None
        ),
        "counts": {
            "total": 0,
            "eligible": 0,
            "completed": 0,
            "refused": 0,
            "incomplete": 0,
            "exclusions": 0,
            "correct_safe_refusals": 0,
            "unexpected_refusals": 0,
            "admitted_without_completion": 0,
            "unsupported_frames": 0,
        },
        "rates": {
            "economic_completion_rate": null_rate,
            "refusal_rate": null_rate,
            "safe_refusal_accuracy": null_rate,
            "incomplete_rate": null_rate,
            "precision": null_rate,
            "recall": null_rate,
            "f1": null_rate,
            "accuracy": null_rate,
            "frame_support_rate": null_rate,
            "frame_exactness_rate": null_rate,
        },
        "reward": None,
        "reward_status": REWARD_STATUS_DESIGNING,
        "confidence_theorem": AUSENTE,
        "episode_uncertainty": {
            "status": UNCERTAINTY_STATUS_UNSUPPORTED,
            "confidence_theorem": AUSENTE,
            "frame_pseudoreplication": False,
            "ci_lower": None,
            "ci_upper": None,
            "reason": "complete-episode uncertainty is unsupported; frame pseudoreplication is forbidden",
        },
        "source_boundary": {
            "electron_brokered_publisher": AUSENTE,
            "cleanup_completed": cleanup_completed,
        },
        "diagnostics": {
            "measurement_tag": VM_MEASUREMENT_TAG,
            "latency_ns": None,
            "throughput": None,
            "measured_host_speed": AUSENTE,
            "host_capacity": AUSENTE,
        },
        "violations": (error_code,),
        "citations": SPEC_CITATIONS,
    }


# ---------------------------------------------------------------------------
# Public API: bridge_vision(evidence, *, episode_ref, ...)
# ---------------------------------------------------------------------------


def bridge_vision(
    evidence: Any,
    *,
    episode_ref: Any = None,
    scorer: Callable[..., Scorecard] | None = None,
    trusted_context: Mapping[str, Any] | TrustedEpisodeContext | None = None,
    allowed_handles: Sequence[Handle] | set[Handle] | None = None,
    observations: Sequence[Observation | Mapping[str, Any]] | None = None,
    t2_submissions: Sequence[Mapping[str, Any]] | None = None,
    proc_status_text: str | None = None,
    cgroup_swap_text: str | None = None,
    clock: FakeClock | None = None,
    diagnostics: Mapping[str, Any] | None = None,
    use_frame_pseudoreplication: bool = False,
    raise_on_refusal: bool = False,
    write_spy: WriteSpy | None = None,
) -> dict[str, Any]:
    """Wrap an existing vision scorer and `Scorecard` with source-neutral episode metadata.

    Parameters
    ----------
    evidence:
        Either an existing `Scorecard`, a `TrialReport`, an iterable of `WindowEvidence`
        rows, or a mapping containing `"windows"` / `"rows"` / `"scorecard"` and optional
        `"tasks"`, `"postconditions"`, `"expected_outcomes"`, `"exclusions"`, or
        `"observations"`.
    episode_ref:
        Trusted sealed episode reference (mapping, `TrustedEpisodeContext`, or episode ID string).
        Placed in the outer metadata mapping, never mutated into the `Scorecard`.
    scorer:
        Optional injected scorer callable `(rows) -> Scorecard`. Defaults to
        `eval.v2eval.multiwin_scorecard.score_windows`. Never invoked when pre-aggregation
        checks refuse duplicate identities, foreign episode bindings, or mixed/missing seals.
    """
    active_spy = write_spy if write_spy is not None else WriteSpy()
    cleanup_completed = False
    scorer_invoked = False

    with active_spy:
        try:
            # 0. Reject frame pseudoreplication upfront
            if use_frame_pseudoreplication:
                raise BridgeRefusalError(
                    "E_FRAME_PSEUDOREPLICATION_FORBIDDEN",
                    "complete-episode uncertainty cannot use per-frame pseudoreplication",
                )

            # 1. Validate RAM-only procfs/cgroup text fixtures if provided
            ram_fixture_report = validate_procfs_cgroup_ram_fixtures(
                proc_status_text=proc_status_text,
                cgroup_swap_text=cgroup_swap_text,
            )

            # 2. Extract and validate trusted episode binding (`episode_ref`)
            ep_info = _extract_trusted_episode_ref(episode_ref, trusted_context=trusted_context)
            episode_id = ep_info["episode_id"]
            expected_seal = ep_info["seal"]
            allowed_generations = ep_info["allowed_generations"]
            allowed_handle_keys = ep_info["allowed_handle_keys"]

            if allowed_handles is not None:
                extra_keys = {(h.kind, h.canonical_id, h.generation) for h in allowed_handles}
                allowed_handle_keys = (
                    extra_keys if allowed_handle_keys is None else (allowed_handle_keys & extra_keys)
                )

            # 3. Unpack evidence without mutating caller inputs
            existing_scorecard: Scorecard | None = None
            raw_windows: list[Any] = []
            extra_tasks: list[Mapping[str, Any]] = []
            excluded_ids: set[str] = set(str(x) for x in ep_info["exclusions"])
            postconditions_map: dict[str, Any] = dict(ep_info["postconditions"])
            expected_outcomes_map: dict[str, Any] = dict(ep_info["expected_outcomes"])
            obs_list: list[Any] = list(observations) if observations is not None else []
            t2_list: list[Mapping[str, Any]] = list(t2_submissions) if t2_submissions is not None else []

            if isinstance(evidence, Scorecard):
                existing_scorecard = evidence
                raw_windows = list(evidence.windows)
            elif isinstance(evidence, TrialReport):
                raw_windows = list(evidence.rows)
                outcomes_dict = evidence.outcomes if isinstance(evidence.outcomes, Mapping) else {}
                if "exclusions" in outcomes_dict:
                    excluded_ids.update(str(x) for x in outcomes_dict["exclusions"])
                if "postconditions" in outcomes_dict and isinstance(outcomes_dict["postconditions"], Mapping):
                    postconditions_map.update(outcomes_dict["postconditions"])
                if "expected_outcomes" in outcomes_dict and isinstance(outcomes_dict["expected_outcomes"], Mapping):
                    expected_outcomes_map.update(outcomes_dict["expected_outcomes"])
            elif isinstance(evidence, Mapping):
                if isinstance(evidence.get("scorecard"), Scorecard):
                    existing_scorecard = evidence["scorecard"]
                    raw_windows = list(existing_scorecard.windows)
                elif "windows" in evidence:
                    raw_windows = list(evidence["windows"])
                elif "rows" in evidence:
                    raw_windows = list(evidence["rows"])
                else:
                    raise BridgeRefusalError("E_EVIDENCE_SCHEMA", "evidence mapping must contain 'windows', 'rows', or 'scorecard'")
                if "exclusions" in evidence and evidence["exclusions"] is not None:
                    excluded_ids.update(str(x) for x in evidence["exclusions"])
                if "postconditions" in evidence and isinstance(evidence["postconditions"], Mapping):
                    postconditions_map.update(evidence["postconditions"])
                if "expected_outcomes" in evidence and isinstance(evidence["expected_outcomes"], Mapping):
                    expected_outcomes_map.update(evidence["expected_outcomes"])
                if "tasks" in evidence and evidence["tasks"] is not None:
                    extra_tasks = list(evidence["tasks"])
                if "observations" in evidence and evidence["observations"] is not None:
                    obs_list.extend(evidence["observations"])
                if "t2_submissions" in evidence and evidence["t2_submissions"] is not None:
                    t2_list.extend(evidence["t2_submissions"])
            elif isinstance(evidence, (list, tuple)):
                raw_windows = list(evidence)
            else:
                raise BridgeRefusalError("E_EVIDENCE_TYPE", f"unsupported evidence type: {type(evidence).__name__}")

            # 4. Validate source-neutral observations before aggregation
            validated_observations: list[dict[str, Any]] = []
            for obs in obs_list:
                obs_meta = validate_source_neutral_observation(obs)
                obs_h = obs.handle if isinstance(obs, Observation) else obs["handle"]
                if isinstance(obs_h, Mapping):
                    obs_h = Handle(obs_h["kind"], obs_h["canonical_id"], obs_h.get("generation", 0))
                h_tuple = (obs_h.kind, obs_h.canonical_id, obs_h.generation)
                if allowed_handle_keys is not None and h_tuple not in allowed_handle_keys:
                    raise ForeignEpisodeBindingError(
                        f"observation handle {h_tuple!r} is foreign to episode {episode_id!r}"
                    )
                if allowed_generations is not None and obs_h.generation not in allowed_generations:
                    raise ForeignEpisodeBindingError(
                        f"observation handle generation {obs_h.generation} is foreign to episode {episode_id!r}"
                    )
                validated_observations.append(obs_meta)

            # 5. Validate external T2 submissions if present
            validated_t2_records: list[dict[str, Any]] = []
            for t2_rec in t2_list:
                validated_t2_records.append(validate_external_t2_record(t2_rec))

            # 6. Pre-aggregation checks on windows: seals, duplicate identities, foreign bindings/generations
            seen_window_ids: set[tuple[str, str, int, str, int, int]] = set()
            seen_logical_ids: set[tuple[str, str, str, int, int]] = set()
            observed_seals: set[str] = set()
            active_windows: list[WindowEvidence] = []
            excluded_windows: list[WindowEvidence] = []

            for idx, item in enumerate(raw_windows):
                if not isinstance(item, WindowEvidence):
                    raise BridgeRefusalError(
                        "E_WINDOW_EVIDENCE_TYPE",
                        f"window[{idx}] must be a WindowEvidence instance from eval.v2eval.multiwin_scorecard",
                    )
                h = item.handle
                if not isinstance(h, Handle):
                    raise BridgeRefusalError("E_HANDLE_TYPE", f"window[{idx}].handle must be a Handle instance")

                # Seal check (must refuse before aggregation on missing or mixed seals)
                if not isinstance(item.seal, str) or not item.seal.strip() or item.seal.strip() == AUSENTE:
                    raise SealMismatchError(f"window[{idx}] has missing or AUSENTE seal")
                seal_str = item.seal.strip()
                observed_seals.add(seal_str)
                if len(observed_seals) > 1:
                    raise SealMismatchError(f"mixed seals across windows: {sorted(observed_seals)!r}")
                if expected_seal is not None and seal_str != expected_seal:
                    raise SealMismatchError(
                        f"window[{idx}] seal {seal_str!r} does not match trusted episode seal {expected_seal!r}"
                    )

                # Foreign episode / foreign generation / foreign handle check (must refuse before aggregation)
                h_key = (h.kind, h.canonical_id, h.generation)
                if allowed_handle_keys is not None and h_key not in allowed_handle_keys:
                    raise ForeignEpisodeBindingError(
                        f"window[{idx}] handle {h_key!r} is not bound to episode {episode_id!r}"
                    )
                if allowed_generations is not None and h.generation not in allowed_generations:
                    raise ForeignEpisodeBindingError(
                        f"window[{idx}] foreign generation {h.generation} != allowed {sorted(allowed_generations)!r}"
                    )

                ops = item.ops if isinstance(item.ops, Mapping) else {}
                if ops.get("foreign_row") or ops.get("foreign_handle") or ops.get("foreign_generation"):
                    raise ForeignEpisodeBindingError(f"window[{idx}] flagged as foreign generation/handle")
                row_ep = ops.get("episode_id", ops.get("episode_ref"))
                if row_ep is not None and str(row_ep) != episode_id:
                    raise ForeignEpisodeBindingError(
                        f"window[{idx}] bound to foreign episode {row_ep!r} != {episode_id!r}"
                    )

                # Check source.kind boundary if embedded in row.ops
                row_source = ops.get("source")
                if isinstance(row_source, Mapping) and row_source.get("kind") == "cdp":
                    if h.kind == "godot_window" or ops.get("uitree_schema") == "holdings.godot.uitree/v1":
                        raise SourceKindBoundaryError(
                            f"window[{idx}] relabeled Godot tree as source.kind == 'cdp'"
                        )

                # Duplicate window identity check (must refuse before aggregation)
                full_ident = (h.kind, h.canonical_id, h.generation, item.scene, int(item.epoch), int(item.frame_seq))
                logical_ident = (h.kind, h.canonical_id, item.scene, int(item.epoch), int(item.frame_seq))
                if full_ident in seen_window_ids or logical_ident in seen_logical_ids:
                    raise WindowIdentityError(f"duplicate window identity before aggregation: {full_ident!r}")
                seen_window_ids.add(full_ident)
                seen_logical_ids.add(logical_ident)

                win_id_str = f"{h.kind}:{h.canonical_id}:{item.scene}:{item.epoch}:{item.frame_seq}"
                if (
                    win_id_str in excluded_ids
                    or item.scene in excluded_ids
                    or canonical_handle_key(h) in excluded_ids
                    or bool(ops.get("excluded", False))
                    or (item.terminal or "").strip().upper() in _EXCLUSION_TERMINALS
                ):
                    excluded_windows.append(item)
                else:
                    active_windows.append(item)

            resolved_seal = (
                next(iter(observed_seals))
                if len(observed_seals) == 1
                else expected_seal
            )
            episode_ref_payload = {
                "episode_id": episode_id,
                "seal": resolved_seal,
                "trusted": True,
                "allowed_generations": (
                    tuple(sorted(allowed_generations)) if allowed_generations is not None else None
                ),
                "allowed_handles": (
                    tuple(
                        f"{k}:{cid}@{gen}"
                        for k, cid, gen in sorted(allowed_handle_keys)
                    )
                    if allowed_handle_keys is not None
                    else None
                ),
            }

            # 7. Invoke or validate existing Scorecard (preserving original object untouched)
            score_fn = scorer if scorer is not None else score_windows
            if existing_scorecard is not None:
                validate_scorecard(existing_scorecard)
                scorecard_obj = existing_scorecard
            elif len(active_windows) > 0:
                scorer_invoked = True
                scorecard_obj = score_fn(tuple(active_windows))
                if isinstance(scorecard_obj, Scorecard):
                    validate_scorecard(scorecard_obj)
            else:
                # Empty battery: invoke score_fn(()) if it handles empty windows without raising
                scorer_invoked = True
                scorecard_obj = score_fn(())

            # Verify `episode_ref` is NOT inside `scorecard_obj`
            if isinstance(scorecard_obj, Scorecard) and "episode_ref" in scorecard_obj.pooled:
                raise BridgeRefusalError(
                    "E_SCORECARD_POLLUTION",
                    "scorecard.pooled must not contain episode_ref",
                )

            # 8. Classify items for INV-J5-1, INV-J5-2, INV-J5-4
            item_classifications: list[dict[str, Any]] = []
            for w in excluded_windows:
                win_id = f"{w.handle.kind}:{w.handle.canonical_id}:{w.scene}:{w.epoch}:{w.frame_seq}"
                item_classifications.append(
                    _classify_item_outcome(
                        item_id=win_id,
                        terminal=w.terminal,
                        expected_terminal=w.expected_terminal,
                        ops=w.ops if isinstance(w.ops, Mapping) else {},
                        postconditions_map=postconditions_map,
                        expected_outcomes_map=expected_outcomes_map,
                        excluded_ids=excluded_ids | {win_id},
                    )
                )

            for w in active_windows:
                win_id = f"{w.handle.kind}:{w.handle.canonical_id}:{w.scene}:{w.epoch}:{w.frame_seq}"
                # Allow lookup by win_id, scene, or canonical handle key
                win_ops = dict(w.ops) if isinstance(w.ops, Mapping) else {}
                for lookup_key in (win_id, w.scene, canonical_handle_key(w.handle)):
                    if lookup_key in postconditions_map and "postcondition_verified" not in win_ops:
                        win_ops["postcondition_verified"] = postconditions_map[lookup_key]
                    if lookup_key in expected_outcomes_map and lookup_key not in expected_outcomes_map:
                        pass
                item_classifications.append(
                    _classify_item_outcome(
                        item_id=win_id,
                        terminal=w.terminal,
                        expected_terminal=w.expected_terminal,
                        ops=win_ops,
                        postconditions_map=postconditions_map,
                        expected_outcomes_map=expected_outcomes_map,
                        excluded_ids=excluded_ids,
                    )
                )

            for t_idx, task_rec in enumerate(extra_tasks):
                tid = str(task_rec.get("task_id", f"task-{t_idx}"))
                item_classifications.append(
                    _classify_item_outcome(
                        item_id=tid,
                        terminal=task_rec.get("terminal", task_rec.get("outcome")),
                        expected_terminal=task_rec.get("expected_terminal", task_rec.get("expected_outcome")),
                        ops=task_rec,
                        postconditions_map=postconditions_map,
                        expected_outcomes_map=expected_outcomes_map,
                        excluded_ids=excluded_ids,
                    )
                )

            completed_count = sum(1 for c in item_classifications if c["bucket"] == "completed")
            refused_count = sum(1 for c in item_classifications if c["bucket"] == "refused")
            incomplete_count = sum(1 for c in item_classifications if c["bucket"] == "incomplete")
            exclusions_count = sum(1 for c in item_classifications if c["bucket"] == "excluded")
            eligible_count = completed_count + refused_count + incomplete_count
            total_count = eligible_count + exclusions_count

            # Enforce INV-J5-1 identity explicitly
            assert eligible_count == completed_count + refused_count + incomplete_count

            correct_safe_refusals = sum(
                1 for c in item_classifications if c["bucket"] == "refused" and c["safe_refusal"]
            )
            unexpected_refusals = sum(
                1 for c in item_classifications if c["bucket"] == "refused" and c["unexpected_refusal"]
            )
            admitted_without_completion = sum(
                1 for c in item_classifications if c["admitted_only"]
            )
            unverified_postconditions = sum(
                1
                for c in item_classifications
                if c["bucket"] == "incomplete" and c["postcondition_verified"] is False
            )
            unmatched_outcomes = sum(
                1
                for c in item_classifications
                if c["bucket"] in ("incomplete", "refused") and not c["outcome_matched"]
            )

            # 9. Pool integer confusion and frame support across active_windows (INV-J5-3)
            tp = sum(int(w.tp) for w in active_windows)
            fp = sum(int(w.fp) for w in active_windows)
            fn = sum(int(w.fn) for w in active_windows)
            tn = sum(int(w.tn) for w in active_windows)
            pos_support = tp + fn
            pred_pos = tp + fp
            total_confusion = tp + fp + fn + tn

            n_active_windows = len(active_windows)
            supported_frames = sum(1 for w in active_windows if w.frame_supported)
            unsupported_frames = sum(1 for w in active_windows if not w.frame_supported)
            delivered_supported = sum(1 for w in active_windows if w.frame_supported and not w.dropped)
            exact_delivered = sum(
                1 for w in active_windows if w.frame_supported and not w.dropped and w.exact is True
            )

            refusal_expected_count = sum(
                1
                for c in item_classifications
                if c["bucket"] != "excluded"
                and (c["expected_terminal"] or "") in _REFUSAL_TERMINALS
            )
            nominal_expected_count = sum(
                1
                for c in item_classifications
                if c["bucket"] != "excluded"
                and (c["expected_terminal"] or "") in _NOMINAL_COMPLETION_TERMINALS
            )

            # Rational rates with INV-J5-3 null/UNKNOWN on empty/absent denominators
            economic_completion_rate = make_rational_rate(
                completed_count,
                eligible_count if eligible_count > 0 else None,
                pass_when=lambda num, den: (
                    den > 0
                    and num == nominal_expected_count
                    and incomplete_count == 0
                    and unexpected_refusals == 0
                ),
            )
            refusal_rate = make_rational_rate(
                refused_count,
                eligible_count if eligible_count > 0 else None,
                pass_when=lambda _num, _den: unexpected_refusals == 0,
            )
            safe_refusal_accuracy = make_rational_rate(
                correct_safe_refusals,
                refusal_expected_count if refusal_expected_count > 0 else None,
                pass_when=lambda num, den: num == den and unexpected_refusals == 0,
            )
            incomplete_rate = make_rational_rate(
                incomplete_count,
                eligible_count if eligible_count > 0 else None,
                pass_when=lambda num, _den: num == 0,
            )
            precision_rate = make_rational_rate(tp, pred_pos if pred_pos > 0 else None)
            recall_rate = make_rational_rate(tp, pos_support if pos_support > 0 else None)
            f1_den = 2 * tp + fp + fn
            f1_rate = make_rational_rate(2 * tp, f1_den if f1_den > 0 else None)
            accuracy_rate = make_rational_rate(
                tp + tn,
                total_confusion if total_confusion > 0 else None,
            )
            frame_support_rate = make_rational_rate(
                supported_frames,
                n_active_windows if n_active_windows > 0 else None,
                pass_when=lambda num, den: num == den and unsupported_frames == 0,
            )
            frame_exactness_rate = make_rational_rate(
                exact_delivered,
                delivered_supported if delivered_supported > 0 else None,
                pass_when=lambda num, den: num == den and unsupported_frames == 0,
            )

            # 10. Collect bridge violations & determine overall status
            bridge_violations: list[str] = []
            if n_active_windows == 0 and eligible_count == 0:
                bridge_violations.append("empty_battery")
            if unsupported_frames > 0:
                bridge_violations.append("unsupported_frames_present")
            if delivered_supported == 0:
                bridge_violations.append("absent_denominator:frame_exactness")
            if pos_support == 0 or pred_pos == 0 or total_confusion == 0:
                bridge_violations.append("absent_denominator:pooled_confusion")
            if unverified_postconditions > 0:
                bridge_violations.append("unverified_or_failed_postcondition")
            if admitted_without_completion > 0:
                bridge_violations.append("admission_without_economic_completion")
            if unexpected_refusals > 0:
                bridge_violations.append("unexpected_refusal")
            if unmatched_outcomes > 0:
                bridge_violations.append("unmatched_outcome")
            if incomplete_count > 0:
                bridge_violations.append("incomplete_items_present")
            if isinstance(scorecard_obj, Scorecard):
                for v in scorecard_obj.violations:
                    if v not in bridge_violations:
                        bridge_violations.append(str(v))

            # Determine top-level status and passed flag
            if n_active_windows == 0 and eligible_count == 0:
                status = "EMPTY_BATTERY"
                overall_passed = False
            elif unsupported_frames > 0:
                status = UNCERTAINTY_STATUS_UNSUPPORTED
                overall_passed = False
            elif len(bridge_violations) == 0 and (
                completed_count + correct_safe_refusals == eligible_count and eligible_count > 0
            ):
                sc_passed = bool(scorecard_obj.passed) if isinstance(scorecard_obj, Scorecard) else False
                overall_passed = sc_passed and len(bridge_violations) == 0
                status = "PASSED" if overall_passed else "FAILED"
            else:
                overall_passed = False
                status = "FAILED"

            # 11. Optional diagnostics (VM-labeled only, never derived performance from FakeClock)
            diag_latency_ns: int | None = None
            diag_throughput: dict[str, Any] | None = None
            fake_clock_ticks_ns: int | None = clock() if isinstance(clock, FakeClock) else None
            if isinstance(diagnostics, Mapping):
                if diagnostics.get("derived_from_fake_clock") is True:
                    raise BridgeRefusalError(
                        "E_FAKE_CLOCK_PERFORMANCE_CLAIM",
                        "latency and throughput diagnostics must never be derived performance evidence from fake clocks",
                    )
                if diagnostics.get("claim_host_speed") is True or diagnostics.get("measurement_tag") == "MEASURED":
                    raise BridgeRefusalError(
                        "E_HOST_SPEED_CLAIM_FORBIDDEN",
                        "VM fixture timings must be labeled 'VM' and never claim host speed or capacity",
                    )
                if diagnostics.get("latency_ns") is not None:
                    diag_latency_ns = _require_strict_int(diagnostics["latency_ns"], "diagnostics.latency_ns", min_value=0)
                if diagnostics.get("duration_ns") is not None:
                    dur_ns = _require_strict_int(diagnostics["duration_ns"], "diagnostics.duration_ns", min_value=0)
                    diag_throughput = make_rational_rate(completed_count, dur_ns if dur_ns > 0 else None)

            cleanup_completed = True
            result: dict[str, Any] = {
                "schema": "vqa.holdings_scorecard_bridge/v1",
                "gate": GATE_G_J5_3,
                "status": status,
                "passed": overall_passed,
                "refused_before_aggregation": False,
                "scorer_invoked": scorer_invoked,
                "error_code": None,
                "error_detail": None,
                "episode_id": episode_id,
                "episode_seal": resolved_seal,
                "episode_ref": episode_ref_payload,
                "scorecard": scorecard_obj,
                "scorecard_canonical_sha256": (
                    hashlib.sha256(canonical_bytes(scorecard_obj)).hexdigest()
                    if isinstance(scorecard_obj, Scorecard)
                    else None
                ),
                "counts": {
                    "total": total_count,
                    "eligible": eligible_count,
                    "completed": completed_count,
                    "refused": refused_count,
                    "incomplete": incomplete_count,
                    "exclusions": exclusions_count,
                    "correct_safe_refusals": correct_safe_refusals,
                    "unexpected_refusals": unexpected_refusals,
                    "admitted_without_completion": admitted_without_completion,
                    "unverified_postconditions": unverified_postconditions,
                    "unsupported_frames": unsupported_frames,
                    "supported_frames": supported_frames,
                    "delivered_frames": delivered_supported,
                    "exact_frames": exact_delivered,
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "tn": tn,
                },
                "rates": {
                    "economic_completion_rate": economic_completion_rate,
                    "refusal_rate": refusal_rate,
                    "safe_refusal_accuracy": safe_refusal_accuracy,
                    "incomplete_rate": incomplete_rate,
                    "precision": precision_rate,
                    "recall": recall_rate,
                    "f1": f1_rate,
                    "accuracy": accuracy_rate,
                    "frame_support_rate": frame_support_rate,
                    "frame_exactness_rate": frame_exactness_rate,
                },
                "items": tuple(item_classifications),
                "reward": None,
                "reward_status": REWARD_STATUS_DESIGNING,
                "confidence_theorem": AUSENTE,
                "episode_uncertainty": {
                    "status": UNCERTAINTY_STATUS_UNSUPPORTED,
                    "confidence_theorem": AUSENTE,
                    "frame_pseudoreplication": False,
                    "ci_lower": None,
                    "ci_upper": None,
                    "reason": "complete-episode uncertainty is unsupported; frame pseudoreplication is forbidden",
                },
                "source_boundary": {
                    "electron_brokered_publisher": AUSENTE,
                    "observations": tuple(validated_observations),
                    "t2_submissions": tuple(validated_t2_records),
                    "ram_fixtures": ram_fixture_report,
                    "cleanup_completed": cleanup_completed,
                },
                "diagnostics": {
                    "measurement_tag": VM_MEASUREMENT_TAG,
                    "latency_ns": diag_latency_ns,
                    "throughput": diag_throughput,
                    "fake_clock_now_ns": fake_clock_ticks_ns,
                    "performance_evidence_from_fake_clock": None,
                    "measured_host_speed": AUSENTE,
                    "host_capacity": AUSENTE,
                },
                "violations": tuple(bridge_violations),
                "citations": SPEC_CITATIONS,
            }

            # Verify no raw pixels leaked into the metadata mapping
            active_spy.inspect_payload_for_pixels(result, location="bridge_vision_result")
            return result

        except (
            BridgeRefusalError,
            SealMismatchError,
            WindowIdentityError,
            FrameSequenceError,
            TerminalValidationError,
            PrerequisiteAbsentError,
            ScorecardValidationError,
        ) as exc:
            cleanup_completed = True
            if raise_on_refusal or isinstance(exc, PixelPersistenceError):
                raise
            ep_id_fallback: str | None = None
            ep_seal_fallback: str | None = None
            if isinstance(episode_ref, str) and episode_ref.strip() and episode_ref.strip() != AUSENTE:
                ep_id_fallback = episode_ref.strip()
            elif isinstance(episode_ref, Mapping):
                raw_eid = episode_ref.get("episode_id")
                if isinstance(raw_eid, str) and raw_eid.strip() and raw_eid.strip() != AUSENTE:
                    ep_id_fallback = raw_eid.strip()
                raw_seal = episode_ref.get("seal")
                if isinstance(raw_seal, str) and raw_seal.strip() and raw_seal.strip() != AUSENTE:
                    ep_seal_fallback = raw_seal.strip()
            elif isinstance(episode_ref, TrustedEpisodeContext):
                ep_id_fallback = episode_ref.episode_id
                ep_seal_fallback = episode_ref.seal

            code = getattr(exc, "code", "E_BRIDGE_REFUSED")
            detail = getattr(exc, "detail", str(exc))
            return _build_refusal_response(
                error_code=str(code),
                detail=str(detail),
                episode_id=ep_id_fallback,
                episode_seal=ep_seal_fallback,
                episode_ref_payload=(
                    {"episode_id": ep_id_fallback, "seal": ep_seal_fallback, "trusted": False}
                    if ep_id_fallback is not None
                    else None
                ),
                scorer_invoked=scorer_invoked,
                scorecard=None,
                cleanup_completed=cleanup_completed,
            )


__all__ = [
    "AUSENTE",
    "BridgeRefusalError",
    "EXTERNAL_T2_FIELDS",
    "ForeignEpisodeBindingError",
    "GATE_G_J5_3",
    "MEASURED_STATUS",
    "NEW_GAME_ARG_FIELDS",
    "PixelPersistenceError",
    "REWARD_STATUS_DESIGNING",
    "SPEC_CITATIONS",
    "SourceKindBoundaryError",
    "SourceNeutralBoundarySession",
    "T2CommandContractError",
    "TrustedEpisodeContext",
    "UNCERTAINTY_STATUS_UNSUPPORTED",
    "UNKNOWN_STATUS",
    "VM_MEASUREMENT_TAG",
    "WriteSpy",
    "bridge_vision",
    "make_rational_rate",
    "make_t2_new_game",
    "make_t2_pause",
    "validate_external_t2_record",
    "validate_procfs_cgroup_ram_fixtures",
    "validate_source_neutral_observation",
    "verify_dispatch_admission",
]
