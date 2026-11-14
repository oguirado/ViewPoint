# @module {"id":"wa.state.handles","layer":0,"owner":"STATE","deps":[],"book":"HB-WA-a0-arch"}
"""Shared target identities and validated immutable perception records.

Moved from wender/perceive/session_registry.py so schema and runtime share types.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import re

import numpy as np


SUPPORTED_KINDS = frozenset({"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"})
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class RegistryError(ValueError):
    """Invalid, conflicted or unavailable local resource identity."""


class StaleHandleError(RegistryError):
    """The generation was replaced, closed or was never registered."""


class AliasConflictError(RegistryError):
    """An alias or destination has already been claimed by another resource."""


class QuarantinedTargetError(RegistryError):
    """Deferred or failed cleanup requires reconciliation before reuse."""


class FrameValidationError(RegistryError):
    """Frame provenance, geometry or order is invalid."""


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise RegistryError(f"{name} must be an integer >= {minimum}")
    return value


def _name(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise RegistryError(f"{name} must be a nonempty canonical string")
    return value


@dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_KINDS:
            raise RegistryError(f"unsupported target kind: {self.kind!r}")
        _name(self.canonical_id, "canonical_id")
        _integer(self.generation, "generation", minimum=1)


def _handle(value: object) -> Handle:
    """Accept structurally matching same-/cross-repository handles, not class identity."""
    try:
        return Handle(value.kind, value.canonical_id, value.generation)
    except AttributeError as exc:
        raise RegistryError("value must expose kind, canonical_id and generation") from exc


def _epoch(value: int) -> int:
    return _integer(value, "epoch")


def _sequence(value: int) -> int:
    return _integer(value, "frame_seq")


def _owned_array(value: object, *, rgb: bool) -> np.ndarray:
    """Copy into an immutable bytes-backed ndarray; caller buffers can never alias it."""
    a = np.asarray(value)
    if a.dtype.hasobject:
        raise FrameValidationError("object arrays cannot be admitted")
    if rgb and (a.dtype != np.dtype("uint8") or a.ndim != 3 or a.shape[2] != 3):
        raise FrameValidationError("RGB must be H x W x 3 uint8")
    # A bytes-backed view cannot have its WRITEABLE flag re-enabled by consumers.
    owned = np.frombuffer(a.tobytes(order="C"), dtype=a.dtype).reshape(a.shape)
    return owned


@dataclass(frozen=True)
class TargetRecord:
    handle: Handle
    transport: object
    owned: bool
    epoch: int
    session_id: str | None
    attachment_owned: bool
    closed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _handle(self.handle))
        _epoch(self.epoch)
        if type(self.owned) is not bool or type(self.attachment_owned) is not bool or type(self.closed) is not bool:
            raise RegistryError("ownership and closed flags must be bool")
        if self.session_id is not None:
            _name(self.session_id, "session_id")
        elif self.attachment_owned:
            raise RegistryError("owned attachment requires an acknowledged session_id")


@dataclass(frozen=True)
class FrameRef:
    handle: Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: object
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _handle(self.handle))
        _epoch(self.epoch)
        _sequence(self.frame_seq)
        _integer(self.width, "width", minimum=1)
        _integer(self.height, "height", minimum=1)
        arr = _owned_array(self.rgb, rgb=True)
        if arr.shape[:2] != (self.height, self.width):
            raise FrameValidationError("RGB dimensions do not match width/height")
        actual = hashlib.sha256(arr.tobytes()).hexdigest()
        if not isinstance(self.digest, str) or not _DIGEST.fullmatch(self.digest) or self.digest != actual:
            raise FrameValidationError("RGB content digest mismatch")
        object.__setattr__(self, "rgb", arr)

    def __deepcopy__(self, memo: dict) -> FrameRef:
        copied = object.__new__(type(self))
        memo[id(self)] = copied
        for name, value in vars(self).items():
            object.__setattr__(copied, name, copy.deepcopy(value, memo))
        copied.rgb.setflags(write=self.rgb.flags.writeable)
        return copied

    @classmethod
    def from_rgb(cls, handle: Handle, epoch: int, frame_seq: int, rgb: object) -> FrameRef:
        a = np.asarray(rgb)
        if a.dtype != np.dtype("uint8") or a.ndim != 3 or a.shape[2] != 3:
            raise FrameValidationError("RGB must be H x W x 3 uint8")
        return cls(handle, epoch, frame_seq, a.shape[1], a.shape[0], a, hashlib.sha256(a.tobytes(order="C")).hexdigest())


@dataclass(frozen=True)
class Observation:
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: FrameRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _handle(self.handle))
        _epoch(self.epoch)
        _sequence(self.frame_seq)
        _integer(self.dpr_ppm, "dpr_ppm", minimum=1)
        if not isinstance(self.state, dict):
            raise RegistryError("state must be dict")
        if (self.handle, self.epoch, self.frame_seq) != (self.frame.handle, self.frame.epoch, self.frame.frame_seq):
            raise FrameValidationError("observation and frame identity/epoch/sequence differ")
        object.__setattr__(self, "state", copy.deepcopy(self.state))


@dataclass(frozen=True)
class Receipt:
    action_id: str
    handle: Handle
    status: str
    reason: str | None

    def __post_init__(self) -> None:
        _name(self.action_id, "action_id")
        object.__setattr__(self, "handle", _handle(self.handle))
        _name(self.status, "status")
        if self.reason is not None and not isinstance(self.reason, str):
            raise RegistryError("reason must be str or None")


@dataclass(frozen=True)
class ActionRequest:
    handle: Handle
    action_id: str
    epoch: int
    payload: dict
    cost: int
    deadline_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _handle(self.handle))
        _name(self.action_id, "action_id")
        _epoch(self.epoch)
        _integer(self.cost, "cost")
        _integer(self.deadline_ns, "deadline_ns")
        if not isinstance(self.payload, dict):
            raise RegistryError("payload must be dict")
        object.__setattr__(self, "payload", copy.deepcopy(self.payload))


@dataclass(frozen=True)
class ActorLease:
    handle: Handle
    owner: str
    generation: int
    nonce: bytes
    expires_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _handle(self.handle))
        _name(self.owner, "owner")
        _integer(self.generation, "generation", minimum=1)
        if self.generation != self.handle.generation:
            raise RegistryError("lease generation differs from handle")
        if not isinstance(self.nonce, bytes) or not self.nonce:
            raise RegistryError("nonce must be nonempty bytes")
        _integer(self.expires_ns, "expires_ns")


@dataclass(frozen=True)
class FocusLease:
    lease: ActorLease
    domain: str
    epoch: int

    def __post_init__(self) -> None:
        _name(self.domain, "domain")
        _epoch(self.epoch)


@dataclass(frozen=True)
class StreamSpec:
    width: int
    height: int
    slots: int
    quota_bytes: int

    def __post_init__(self) -> None:
        for field in ("width", "height", "slots", "quota_bytes"):
            _integer(getattr(self, field), field, minimum=1)
        if self.width * self.height * 3 * self.slots > self.quota_bytes:
            raise RegistryError("stream quota insufficient for owned RGB slots")


@dataclass(frozen=True)
class StreamToken:
    handle: Handle
    epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _handle(self.handle))
        _epoch(self.epoch)


@dataclass(frozen=True)
class Ticket:
    action_id: str
    handle: Handle

    def __post_init__(self) -> None:
        _name(self.action_id, "action_id")
        object.__setattr__(self, "handle", _handle(self.handle))


@dataclass(frozen=True)
class ChangeResult:
    handle: Handle
    epoch: int
    settled: bool
    claims: np.ndarray
    ops: dict

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _handle(self.handle))
        _epoch(self.epoch)
        if type(self.settled) is not bool or not isinstance(self.claims, np.ndarray) or not isinstance(self.ops, dict):
            raise RegistryError("settled must be bool, claims ndarray, ops dict")
        object.__setattr__(self, "claims", _owned_array(self.claims, rgb=False))
        object.__setattr__(self, "ops", copy.deepcopy(self.ops))


@dataclass(frozen=True)
class PerceptionResult:
    handle: Handle | None
    observation: Observation | None
    error: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _perception_handle(self.handle, self.observation, self.error))
        if self.observation is not None and self.observation.handle != self.handle:
            raise FrameValidationError("perception result handle mismatch")
        if self.error is not None and not isinstance(self.error, str):
            raise RegistryError("error must be str or None")


@dataclass(frozen=True)
class WindowFrames:
    handle: Handle
    epoch: int
    frames: tuple
    frame_seqs: tuple
    dpr_ppm: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _handle(self.handle))
        _epoch(self.epoch)
        _integer(self.dpr_ppm, "dpr_ppm", minimum=1)
        frames, seqs = tuple(self.frames), tuple(self.frame_seqs)
        if len(frames) != len(seqs):
            raise FrameValidationError("frame and sequence counts differ")
        last = -1
        frozen_frames = []
        for f, seq in zip(frames, seqs):
            _sequence(seq)
            if seq <= last:
                raise FrameValidationError("frame sequence must be strictly increasing")
            last = seq
            if isinstance(f, FrameRef):
                if (f.handle, f.epoch, f.frame_seq) != (self.handle, self.epoch, seq):
                    raise FrameValidationError("window frame provenance mismatch")
                frozen_frames.append(f)
            else:
                frozen_frames.append(_owned_array(f, rgb=True))
        object.__setattr__(self, "frames", tuple(frozen_frames))
        object.__setattr__(self, "frame_seqs", seqs)


@dataclass(frozen=True)
class WindowDescription:
    """Immutable description with optional descriptor and region payloads.

    Reject a cardinality mismatch only when both the payload being checked and
    frame_seqs are non-empty. Empty regions on success and empty payloads on
    error results are valid.
    """

    handle: Handle
    epoch: int
    frame_seqs: tuple
    descriptors: tuple
    regions: tuple
    error: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle", _handle(self.handle))
        _epoch(self.epoch)
        seqs = tuple(self.frame_seqs)
        last = -1
        for seq in seqs:
            _sequence(seq)
            if seq <= last:
                raise FrameValidationError("description frame sequence must increase")
            last = seq
        object.__setattr__(self, "frame_seqs", seqs)
        object.__setattr__(self, "descriptors", tuple(self.descriptors))
        object.__setattr__(self, "regions", tuple(self.regions))
        if self.descriptors and seqs and len(self.descriptors) != len(seqs):
            raise FrameValidationError("descriptors and frame_seqs must have equal length")
        if self.regions and seqs and len(self.regions) != len(seqs):
            raise FrameValidationError("regions and frame_seqs must have equal length")
        if self.error is not None and not isinstance(self.error, str):
            raise RegistryError("error must be str or None")


def _perception_handle(handle: object, observation: Observation | None, error: str | None) -> Handle | None:
    """An invalid input with no usable identity can produce only an error receipt.

    Successful observations and every supplied handle retain the canonical
    constructor checks. No synthetic target identity is invented for rejection.
    """
    if handle is None:
        if observation is not None or not isinstance(error, str) or not error:
            raise RegistryError("unbound perception result requires an error and no observation")
        return None
    return _handle(handle)
