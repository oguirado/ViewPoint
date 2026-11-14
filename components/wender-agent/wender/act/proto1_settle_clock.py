# @module {"id":"wa.act.proto1_settle_clock","layer":2,"owner":"ACT","deps":["wa.perceive.session_registry","wa.act.window_scheduler"],"book":"HB-WA-s2-act"}
"""Fail-closed PROTO1 Area B evidence join, not a transport or receipt owner.

All durations, polling limits and the single absolute deadline are supplied by
its caller. The registry and WriterFence are real authority objects. Nothing in
this module writes to a transport, fabricates owner receipts or clears STOP.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import threading
from types import MappingProxyType


SIGNALS = frozenset({"visual", "dom_layout", "scene_tick", "inflight_work"})
KINDS = frozenset({"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OUTCOMES = frozenset({"APPLIED", "ADMITTED_ONLY", "FAILED", "UNKNOWN"})


class SettleRefused(RuntimeError):
    """Deterministic, observable refusal with no success side effect."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _int(value: object, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SettleRefused("INVALID_INTEGER")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise SettleRefused("INVALID_DIGEST")
    return value


def _name(value: object) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or "\x00" in value):
        raise SettleRefused("INVALID_IDENTITY")
    return value


def _handle(value: object) -> tuple[str, str, int]:
    try:
        kind, canonical_id, generation = value.kind, value.canonical_id, value.generation
    except AttributeError as exc:
        raise SettleRefused("INVALID_HANDLE") from exc
    if kind not in KINDS:
        raise SettleRefused("INVALID_HANDLE")
    return kind, _name(canonical_id), _int(generation, 1)


@dataclass(frozen=True)
class RuntimeHandle:
    kind: str
    canonical_id: str
    generation: int

    def __post_init__(self):
        _handle(self)


@dataclass(frozen=True)
class FrameIdentity:
    frame_seq: int
    width: int
    height: int
    rgb24_sha256: str
    binding_sha256: str

    def __post_init__(self):
        _int(self.frame_seq)
        _int(self.width, 1)
        _int(self.height, 1)
        _digest(self.rgb24_sha256)
        _digest(self.binding_sha256)


@dataclass(frozen=True)
class AdapterInput:
    """PROTO1 adapter data; deliberately outside the closed seat schema."""

    run_id: str
    target_handle: object
    epoch: int
    binding_sha256: str
    frame_refs: tuple[FrameIdentity, ...]
    state_sha256: str
    action_id: str
    trace_sha256: str
    outcome: str
    owner_receipt: bytes | None

    def __post_init__(self):
        _name(self.run_id)
        object.__setattr__(self, "target_handle", RuntimeHandle(*_handle(self.target_handle)))
        _int(self.epoch)
        _digest(self.binding_sha256)
        _digest(self.state_sha256)
        _name(self.action_id)
        _digest(self.trace_sha256)
        if self.outcome not in _OUTCOMES:
            raise SettleRefused("INVALID_OUTCOME")
        if self.owner_receipt is not None and type(self.owner_receipt) is not bytes:
            raise SettleRefused("INVALID_RECEIPT_BYTES")
        if not isinstance(self.frame_refs, tuple) or not self.frame_refs:
            raise SettleRefused("INVALID_FRAMES")
        prior = -1
        for ref in self.frame_refs:
            if not isinstance(ref, FrameIdentity) or ref.frame_seq <= prior:
                raise SettleRefused("INVALID_FRAMES")
            if ref.binding_sha256 != self.binding_sha256:
                raise SettleRefused("BINDING_MISMATCH")
            prior = ref.frame_seq


@dataclass(frozen=True)
class BackendProfile:
    name: str
    extension_version: int
    applicability: dict[str, bool]

    def __post_init__(self):
        _name(self.name)
        if type(self.extension_version) is not int or self.extension_version != 1:
            raise SettleRefused("UNKNOWN_EXTENSION_VERSION")
        if (not isinstance(self.applicability, dict)
                or set(self.applicability) != SIGNALS
                or any(type(flag) is not bool for flag in self.applicability.values())
                or not any(self.applicability.values())):
            raise SettleRefused("PROFILE_INCOMPLETE")
        object.__setattr__(self, "applicability", MappingProxyType(dict(self.applicability)))


@dataclass(frozen=True)
class SettleConfig:
    deadline_ns: int
    poll_interval_ns: int
    quiet_window_ns: int
    max_sample_age_ns: int
    max_catch_up_steps: int
    max_polls: int

    def __post_init__(self):
        _int(self.deadline_ns, 1)
        _int(self.poll_interval_ns, 1)
        _int(self.quiet_window_ns, 1)
        _int(self.max_sample_age_ns, 1)
        _int(self.max_catch_up_steps)
        _int(self.max_polls, 1)


@dataclass(frozen=True)
class SignalEvidence:
    signal: str
    target_handle: object
    epoch: int
    action_id: str
    binding_sha256: str
    source_frame: FrameIdentity
    state_sha256: str
    trace_sha256: str
    outcome: str
    observed_ns: int
    quiet: bool | None = None
    inflight_count: int | None = None


@dataclass(frozen=True)
class SettleResult:
    target_handle: RuntimeHandle
    epoch: int
    action_id: str
    source_frame: FrameIdentity
    state_sha256: str
    outcome: str
    settled_at_ns: int
    deadline_ns: int


class SettleCoordinator:
    """Join independent backend pillars at one provenance and absolute deadline.

    `registry` must be the genuine SessionRegistry exposing `resolve(handle)`;
    `writer_fence` must be the shared WriterFence, already bound to the handle.
    The clock returns monotonic integer nanoseconds. `pace(ns)` is an injected
    scheduling channel; it must not extend the configured deadline.
    """

    def __init__(self, *, registry, writer_fence, clock, pace):
        if (not callable(getattr(registry, "resolve", None))
                or not callable(getattr(writer_fence, "reason", None))
                or not callable(getattr(writer_fence, "resource_for", None))
                or not hasattr(writer_fence, "metadata_lock")
                or not callable(clock) or not callable(pace)):
            raise SettleRefused("PRODUCTION_DEFECT_PROVIDER")
        self.registry = registry
        self.writer_fence = writer_fence
        self.clock = clock
        self.pace = pace
        self._lock = threading.RLock()
        self._last_ns = None
        self._result = None

    def _now(self) -> int:
        sample = _int(self.clock())
        with self._lock:
            if self._last_ns is not None and sample < self._last_ns:
                raise SettleRefused("CLOCK_REGRESSION")
            self._last_ns = sample
        return sample

    def _authority(self, adapter: AdapterInput, now: int, deadline: int) -> None:
        if now >= deadline:
            raise SettleRefused("ABSOLUTE_DEADLINE")
        handle = adapter.target_handle
        try:
            record = self.registry.resolve(handle)
        except Exception as exc:
            raise SettleRefused("STALE_GENERATION") from exc
        if (_handle(getattr(record, "handle", None)) != _handle(handle)
                or getattr(record, "closed", True) is not False):
            raise SettleRefused("STALE_GENERATION")
        if type(getattr(record, "epoch", None)) is not int or record.epoch != adapter.epoch:
            raise SettleRefused("STALE_EPOCH")
        if self.writer_fence.resource_for(handle) is None:
            raise SettleRefused("UNBOUND_FENCE")
        reason = self.writer_fence.reason(handle)
        if reason is not None:
            raise SettleRefused("STOPPED" if reason == "STOPPED" else reason)

    def validate_action(self, adapter: AdapterInput, config: SettleConfig) -> None:
        """B-action structural gate only; neither admits nor sends an action."""
        if not isinstance(adapter, AdapterInput) or not isinstance(config, SettleConfig):
            raise SettleRefused("INVALID_ADAPTER")
        with self.writer_fence.metadata_lock:
            self._authority(adapter, self._now(), config.deadline_ns)

    @staticmethod
    def accept_owner_receipt(adapter: AdapterInput):
        """B-receipt is unavailable without the real merged W8U validator."""
        if not isinstance(adapter, AdapterInput):
            raise SettleRefused("INVALID_ADAPTER")
        if adapter.owner_receipt is None:
            raise SettleRefused("RECEIPT_UNKNOWN")
        raise SettleRefused("OWNER_VALIDATOR_AUSENTE")

    @staticmethod
    def _join(snapshot, adapter, profile, start, now, max_age, previous):
        if not isinstance(snapshot, dict):
            raise SettleRefused("EVIDENCE_UNKNOWN")
        wanted = {n for n in SIGNALS if profile.applicability[n]}
        if any(n in snapshot for n in SIGNALS - wanted):
            raise SettleRefused("INAPPLICABLE_SIGNAL")
        if any(n not in snapshot for n in wanted):
            raise SettleRefused("MISSING_SIGNAL")
        if set(snapshot) != wanted:
            raise SettleRefused("UNKNOWN_SIGNAL")
        all_quiet = True
        frame = adapter.frame_refs[-1]
        for name in sorted(wanted):
            evidence = snapshot[name]
            if evidence is None:
                raise SettleRefused("EVIDENCE_UNKNOWN")
            if not isinstance(evidence, SignalEvidence) or evidence.signal != name:
                raise SettleRefused("INVALID_SIGNAL")
            if type(evidence.epoch) is not int:
                raise SettleRefused("INVALID_INTEGER")
            if (_handle(evidence.target_handle) != _handle(adapter.target_handle)
                    or evidence.epoch != adapter.epoch
                    or evidence.action_id != adapter.action_id
                    or evidence.binding_sha256 != adapter.binding_sha256
                    or evidence.source_frame != frame
                    or evidence.state_sha256 != adapter.state_sha256
                    or evidence.trace_sha256 != adapter.trace_sha256):
                raise SettleRefused("EVIDENCE_IDENTITY_MISMATCH")
            if evidence.outcome != "APPLIED" or adapter.outcome != "APPLIED":
                raise SettleRefused("APPLICATION_NOT_CONFIRMED")
            t = _int(evidence.observed_ns)
            if (t < start or t > now or now - t > max_age
                    or t < previous.get(name, start)):
                raise SettleRefused("STALE_EVIDENCE")
            previous[name] = t
            if name == "inflight_work":
                if evidence.quiet is not None:
                    raise SettleRefused("INVALID_SIGNAL")
                if evidence.inflight_count is None:
                    raise SettleRefused("EVIDENCE_UNKNOWN")
                _int(evidence.inflight_count)
                all_quiet = all_quiet and evidence.inflight_count == 0
            else:
                if type(evidence.quiet) is not bool or evidence.inflight_count is not None:
                    raise SettleRefused("EVIDENCE_UNKNOWN")
                all_quiet = all_quiet and evidence.quiet
        return all_quiet

    def settle(self, adapter: AdapterInput, profile: BackendProfile,
               config: SettleConfig, signal_reader) -> SettleResult:
        """Poll all applicable pillars; commit success only under the STOP fence."""
        if not isinstance(profile, BackendProfile) or not callable(signal_reader):
            raise SettleRefused("INVALID_PROFILE_OR_READER")
        with self.writer_fence.metadata_lock:
            self._result = None
        self.validate_action(adapter, config)
        if adapter.outcome != "APPLIED":
            raise SettleRefused("APPLICATION_NOT_CONFIRMED")
        start = self._now()
        quiet_since = None
        previous = {}
        for poll in range(config.max_polls):
            with self.writer_fence.metadata_lock:
                self._authority(adapter, self._now(), config.deadline_ns)
            try:
                snapshot = signal_reader()
            except Exception as exc:
                raise SettleRefused("EVIDENCE_PROVIDER_ERROR") from exc
            now = self._now()
            with self.writer_fence.metadata_lock:
                self._authority(adapter, now, config.deadline_ns)
            quiet = self._join(snapshot, adapter, profile, start, now,
                               config.max_sample_age_ns, previous)
            if quiet:
                if quiet_since is None:
                    quiet_since = now
                if now - quiet_since >= config.quiet_window_ns:
                    with self.writer_fence.metadata_lock:
                        committed = self._now()
                        self._authority(adapter, committed, config.deadline_ns)
                        if committed - quiet_since < config.quiet_window_ns:
                            raise SettleRefused("EVIDENCE_UNKNOWN")
                        result = SettleResult(RuntimeHandle(*_handle(adapter.target_handle)),
                                              adapter.epoch, adapter.action_id,
                                              adapter.frame_refs[-1], adapter.state_sha256,
                                              adapter.outcome, committed, config.deadline_ns)
                        with self._lock:
                            self._result = result
                        return result
            else:
                quiet_since = None
            if poll + 1 == config.max_polls:
                raise SettleRefused("POLL_LIMIT")
            target_tick = start + (poll + 1) * config.poll_interval_ns
            behind = max(0, (now - target_tick) // config.poll_interval_ns + 1)
            target_tick += max(0, behind - config.max_catch_up_steps) * config.poll_interval_ns
            ns = min(max(0, target_tick - now), max(0, config.deadline_ns - now))
            self.pace(ns)
        raise SettleRefused("POLL_LIMIT")

    def is_settled(self, handle, epoch) -> bool:
        """Scheduler chain hook: check identity, STOP and deadline each time."""
        if type(epoch) is not int or epoch < 0:
            return False
        with self.writer_fence.metadata_lock:
            with self._lock:
                result = self._result
            if result is None or _handle(handle) != _handle(result.target_handle) or result.epoch != epoch:
                return False
            try:
                record = self.registry.resolve(handle)
                now = self._now()
                return (record.closed is False and record.epoch == epoch
                        and now < result.deadline_ns
                        and self.writer_fence.resource_for(handle) is not None
                        and self.writer_fence.reason(handle) is None)
            except Exception:
                return False
