# @module {"id":"wa.perceive.godot_window_target","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.godot","wa.state.schema","wa.perceive.session_registry"],"book":"HB-WA-s1-perceive"}
"""Handle-bound Godot T2 target, with fail-closed perception and admission.

No native viewport reader or producer-side flush control is supplied here.
"""
from __future__ import annotations
from dataclasses import dataclass
import copy
import hashlib
import threading
import weakref
from .godot import (
    GodotError, is_valid_control_path, node_id_for_control_path, validate_uitree,
)
from ..state import schema

from .session_registry import (
    Handle, FrameRef, Observation, RegistryError, SessionRegistry,
)

@dataclass(frozen=True)
class GodotReceipt:
    action_id: str
    handle: Handle
    status: str
    reason: str | None

class GodotProtocolError(GodotError):
    pass

class GodotPrerequisiteError(GodotProtocolError):
    pass


@dataclass(frozen=True)
class GodotMetadataObservation:
    """A validated STATE envelope with no claim of pixel/frame evidence.

    The canonical registry Observation requires a real FrameRef. Keep that
    invariant intact while retaining this target's metadata-only API.
    """
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: None = None

    def __post_init__(self):
        if _identity(self.handle)[0] != "godot_window":
            raise GodotProtocolError("wrong target kind")
        if not (_integer(self.epoch) and self.epoch >= 0
                and _integer(self.frame_seq) and self.frame_seq >= 0
                and _integer(self.dpr_ppm) and self.dpr_ppm > 0):
            raise GodotProtocolError("invalid metadata observation coordinates")
        _require_valid_base(self.state)
        if self.frame is not None:
            raise GodotProtocolError("metadata observation cannot carry a frame")
        object.__setattr__(self, "state", copy.deepcopy(self.state))


_owners = weakref.WeakKeyDictionary()

def _integer(x):
    return isinstance(x, int) and not isinstance(x, bool)

def _identity(h):
    try:
        value = (h.kind, h.canonical_id, h.generation)
    except AttributeError as exc:
        raise GodotProtocolError("invalid handle") from exc
    if not (isinstance(value[0], str) and isinstance(value[1], str)
            and value[1] and _integer(value[2]) and value[2] >= 1):
        raise GodotProtocolError("invalid handle identity")
    return value


def _require_valid_base(base):
    try:
        errors = schema.validate_state(base)
    except (TypeError, KeyError, AttributeError, IndexError) as exc:
        raise GodotProtocolError("base invalid shape") from exc
    if type(errors) is not list:
        raise GodotProtocolError("base validator must return an error list")
    if errors != []:
        raise GodotProtocolError("base invalid: " + "; ".join(map(str, errors)))


def _tick_from_state(base):
    stamps = set()
    for node in base["nodes"]:
        value = node.get("states", {}).get("tick")
        if value is not None:
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                raise GodotProtocolError("bad base tick")
            stamps.add(int(value))
    if len(stamps) > 1:
        raise GodotProtocolError("conflicting base ticks")
    return next(iter(stamps), None)

def _owned_frame(source, handle, epoch, previous_seq):
    try:
        source_identity, source_epoch = _identity(source.handle), source.epoch
    except AttributeError as exc:
        raise GodotProtocolError("pixel capture missing handle or epoch") from exc
    if (source_identity != _identity(handle) or not _integer(source_epoch)
            or source_epoch != epoch):
        raise GodotPrerequisiteError("stale pixel capture identity or epoch")
    if not (_integer(source.frame_seq) and source.frame_seq > previous_seq
            and _integer(source.width) and source.width > 0
            and _integer(source.height) and source.height > 0):
        raise GodotProtocolError("bad frame geometry or sequence")
    import numpy as np
    if isinstance(source.rgb, (bytes, bytearray, memoryview)):
        rgb = np.frombuffer(bytes(source.rgb), dtype=np.uint8)
        if rgb.size != source.width * source.height * 3:
            raise GodotProtocolError("RGB size mismatch")
        rgb = rgb.reshape(source.height, source.width, 3).copy()
    else:
        arr = np.asarray(source.rgb)
        if arr.dtype != np.uint8 or arr.shape != (source.height, source.width, 3):
            raise GodotProtocolError("RGB must be HxWx3 uint8")
        rgb = np.array(arr, copy=True, order="C")
    sha = hashlib.sha256(rgb.tobytes()).hexdigest()
    if sha != source.digest:
        raise GodotProtocolError("frame digest mismatch")
    rgb.setflags(write=False)
    return FrameRef(handle, epoch, source.frame_seq, source.width,
                    source.height, rgb, sha)

class GodotWindowTarget:
    TICK_TRAIL_CAP = 24

    @classmethod
    def bind(cls, handle, client, executor, *, registries):
        if _identity(handle)[0] != "godot_window":
            raise GodotProtocolError("wrong target kind")
        if not isinstance(registries, dict):
            raise GodotProtocolError("registries must be a dict")
        if not (callable(getattr(client, "poll_outbox", None))
                and isinstance(getattr(client, "enabled", None), bool)
                and callable(getattr(executor, "dispatch", None))
                and _integer(getattr(executor, "next_seq", None))):
            raise GodotProtocolError("missing public client/executor API")
        if client.enabled != bool(getattr(executor, "transport_enabled", False)):
            raise GodotProtocolError("client/executor T2 transport modes differ")
        registry = registries.get("session_registry")
        if not isinstance(registry, SessionRegistry):
            raise GodotPrerequisiteError("active SessionRegistry required")
        try:
            active = registry.resolve(handle)
        except RegistryError as exc:
            raise GodotPrerequisiteError("target handle is not registered") from exc
        epoch = registries.get("epoch", active.epoch)
        if (not _integer(epoch) or epoch < 0 or epoch != active.epoch
                or _identity(active.handle) != _identity(handle) or active.closed):
            raise GodotPrerequisiteError("stale target registration or epoch")
        for obj in (client, executor):
            held = _owners.get(obj)
            if held is not None and held() is not None:
                raise GodotProtocolError("client/executor already bound")
        target = cls(handle, client, executor, dict(registries), epoch)
        _owners[client] = weakref.ref(target)
        _owners[executor] = weakref.ref(target)
        return target

    def __init__(self, handle, client, executor, registries, epoch):
        self.handle, self.client, self.executor = handle, client, executor
        self.registries, self.epoch = registries, epoch
        self._session_registry = registries["session_registry"]
        self._lock = threading.RLock()
        self._closed = False
        self._retirement = None
        self._last_observation = None
        self.pending_bytes = b""
        self.command_seq = executor.next_seq
        self.committed_tick = None
        self.basis_sha256 = None
        self.sidecar = None
        self._base = None
        self._bases, self._sidecars, self._batches = [], [], []
        self._depth, self._frame_seq = 0, 0
        self.visual_corroborated = False
        self._inflight, self._outcomes, self._receipts = {}, [], []

    @property
    def tick_trail(self):
        with self._lock:
            return tuple(copy.deepcopy(r) for batch in self._batches for r in batch)

    @property
    def receipts(self):
        with self._lock:
            return tuple(self._receipts)

    def is_backpressured(self):
        with self._lock:
            return self._depth >= self.TICK_TRAIL_CAP

    def take_batches(self, count=None):
        with self._lock:
            self._require_current()
            if count is None:
                count = len(self._batches)
            if not _integer(count) or count < 0:
                raise ValueError("bad count")
            taken = self._batches[:count]
            del self._batches[:count]
            self._depth -= sum(map(len, taken))
            return tuple(copy.deepcopy(b) for b in taken)

    def _require_current(self):
        if self._closed:
            raise GodotPrerequisiteError("target is retired")
        try:
            active = self._session_registry.resolve(self.handle)
        except RegistryError as exc:
            raise GodotPrerequisiteError("target registration is stale or closed") from exc
        if (_identity(active.handle) != _identity(self.handle) or active.closed
                or active.epoch != self.epoch):
            raise GodotPrerequisiteError("target generation or epoch changed")
        return active

    def snapshot(self, *, require_pixels=False):
        """Return current in-RAM evidence without promoting metadata to pixels."""
        with self._lock:
            self._require_current()
            value = self._last_observation
            if value is None or (require_pixels and not isinstance(value, Observation)):
                raise GodotPrerequisiteError("pixel observation unavailable")
            if isinstance(value, GodotMetadataObservation):
                return GodotMetadataObservation(value.handle, value.epoch, value.frame_seq,
                                                value.dpr_ppm, value.state)
            return Observation(value.handle, value.epoch, value.frame_seq,
                               value.dpr_ppm, value.state, value.frame)

    def _retire(self, *, stop):
        # Tombstone locally first. Registry STOP quarantines pending cleanup;
        # callers, not this target, must explicitly acknowledge reconciliation.
        with self._lock:
            if self._closed:
                return self._retirement
            self._closed = True
            self.visual_corroborated = False
            self._inflight.clear()
            self._outcomes.clear()
            for obj in (self.client, self.executor):
                held = _owners.get(obj)
                if held is not None and held() is self:
                    del _owners[obj]
            self._retirement = (
                self._session_registry.stop(self.handle) if stop
                else self._session_registry.close(self.handle)
            )
            return self._retirement

    def close(self):
        """Retire this binding and close the registered, owned transport only."""
        return self._retire(stop=False)

    def stop(self):
        """Retire with an explicit, quarantined cleanup obligation and no I/O."""
        return self._retire(stop=True)

    def rebind(self, handle, client, executor, *, registries):
        """Retire the old generation before binding a separately registered one."""
        self.close()
        return type(self).bind(handle, client, executor, registries=registries)

    def _verify_pair(self, base, sidecar):
        _require_valid_base(base)
        if not isinstance(sidecar, dict) or not isinstance(sidecar.get("nodes"), dict):
            raise GodotProtocolError("sidecar nodes must be an object")
        for nid, node in sidecar["nodes"].items():
            path = node.get("control_path") if isinstance(node, dict) else None
            if not is_valid_control_path(path):
                raise GodotProtocolError("invalid control_path")
            if "/" in path and path.split("/", 1)[0] != node.get("screen_id"):
                raise GodotProtocolError("control_path must be rooted at screen_id")
            # Hash the literal path. Slash-to-dot normalization changes identity.
            if node_id_for_control_path(path) != nid:
                raise GodotProtocolError("control_path hash mismatch")
        try:
            errors = validate_uitree(base, sidecar, self.registries)
        except (TypeError, KeyError, AttributeError, IndexError) as exc:
            raise GodotProtocolError("sidecar invalid shape") from exc
        if type(errors) is not list:
            raise GodotProtocolError("sidecar validator must return an error list")
        if errors != []:
            raise GodotProtocolError("sidecar invalid: " + "; ".join(map(str, errors)))
        return sidecar["base_sha256"], _tick_from_state(base)

    def _accept_outcomes(self):
        remaining = []
        for r in self._outcomes:
            entry = self._inflight.get(r["seq"])
            if entry is None:
                continue
            action_id, tick, epoch, admitted_basis = entry
            if (epoch != self.epoch or r["requested_tick"] != tick
                    or r["state_sha256"] != admitted_basis
                    or r["applied_tick"] < tick
                    or r["status"] not in ("accepted", "rejected")):
                remaining.append(r)
                continue
            self._receipts.append(GodotReceipt(action_id, self.handle, r["status"], r["reason"]))
            if r["status"] == "accepted":
                self.committed_tick = max(self.committed_tick or 0, r["applied_tick"])
            del self._inflight[r["seq"]]
        self._outcomes = remaining

    def perceive(self, data: bytes, *, frame: FrameRef | None = None) -> Observation | GodotMetadataObservation:
        with self._lock:
            return self._perceive_locked(data, frame=frame)

    def _perceive_locked(self, data, *, frame):
        self._require_current()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise GodotProtocolError("data must be bytes")
        # Reject invalid pixel identities, digest and sequence before the codec
        # can advance pending bytes or the target can admit new state.
        chosen = _owned_frame(frame, self.handle, self.epoch, self._frame_seq) if frame is not None else None
        if not self.client.enabled:
            raise GodotPrerequisiteError("T2 transport OFF")
        result = self.client.poll_outbox(bytes(data))
        batches = [tuple(copy.deepcopy(r) for r in b) for b in result.batches]
        if any(len(b) > self.TICK_TRAIL_CAP for b in batches):
            raise GodotProtocolError("oversized received batch")
        bases, sidecars = list(self._bases), list(self._sidecars)
        outcomes, pairs = [], []
        for batch in batches:
            for r in batch:
                if r.get("schema") == schema.SCHEMA_ID:
                    bases.append(r)
                elif r.get("schema") == "holdings.godot.uitree/v1":
                    sidecars.append(r)
                elif all(k in r for k in ("seq", "requested_tick", "applied_tick",
                                           "state_sha256", "order_seq_id", "status", "reason")):
                    outcomes.append(r)
        while bases and sidecars:
            base, sidecar = bases.pop(0), sidecars.pop(0)
            digest, tick = self._verify_pair(base, sidecar)
            if self.committed_tick is not None and tick is not None and tick < self.committed_tick:
                raise GodotProtocolError("tick rollback")
            pairs.append((base, sidecar, digest, tick))
        self._bases, self._sidecars = bases, sidecars
        for batch in batches:
            self._batches.append(batch)
            self._depth += len(batch)
        self.pending_bytes = bytes(result.pending)
        self._outcomes.extend(outcomes)
        if pairs:
            base, sidecar, digest, tick = pairs[-1]
            self._base, self.sidecar = copy.deepcopy(base), copy.deepcopy(sidecar)
            self.basis_sha256 = digest
            if tick is not None:
                self.committed_tick = tick
        self._accept_outcomes()
        if self._base is None:
            raise GodotPrerequisiteError("awaiting valid base and sidecar")
        self.visual_corroborated = False
        if chosen is not None:
            proof = self.registries.get("frame_evidence")
            if callable(proof) and self.committed_tick is not None:
                self.visual_corroborated = proof(chosen) == (
                    self.committed_tick, self.basis_sha256)
            self._require_current()
        vp = self._base.get("viewport") or {}
        dpr = vp.get("dpr_ppm", 1000000)
        if not _integer(dpr) or dpr <= 0:
            raise GodotProtocolError("invalid dpr_ppm")
        if chosen is None:
            observation = GodotMetadataObservation(self.handle, self.epoch, self._frame_seq,
                                                   dpr, self._base)
        else:
            self._require_current()
            try:
                registered = self._session_registry.admit_frame(
                    self.handle, chosen.frame_seq, chosen.rgb, epoch=self.epoch)
                observation = self._session_registry.admit_observation(
                    self.handle, registered, state=self._base, dpr_ppm=dpr)
            except RegistryError as exc:
                raise GodotPrerequisiteError("pixel admission rejected by active registry") from exc
            self._require_current()
            self._frame_seq = registered.frame_seq
        self._last_observation = observation
        return observation

    def dispatch(self, record: dict, *, action_id: str, prerequisite_gate=None):
        with self._lock:
            return self._dispatch_locked(record, action_id=action_id,
                                         prerequisite_gate=prerequisite_gate)

    def _dispatch_locked(self, record, *, action_id, prerequisite_gate):
        self._require_current()
        if self.is_backpressured() or self.committed_tick is None or self.basis_sha256 is None:
            raise GodotPrerequisiteError("target blocked by backlog or missing basis/tick")
        if not isinstance(self._last_observation, Observation) or not self.visual_corroborated:
            raise GodotPrerequisiteError("current corroborated pixel observation required")
        gate = prerequisite_gate or self.registries.get("prerequisite_gate")
        if not callable(gate) or gate(self.handle, self.epoch) is not True:
            raise GodotPrerequisiteError("upstream integration prerequisites AUSENTE")
        # A gate may synchronously retire or rebind this target.
        self._require_current()
        if not isinstance(action_id, str) or not action_id:
            raise GodotProtocolError("action_id required")
        if not isinstance(record, dict) or record.get("seq") != self.executor.next_seq:
            raise GodotProtocolError("stale or missing executor sequence")
        if not _integer(record.get("tick")) or record["tick"] < self.committed_tick:
            raise GodotProtocolError("stale command tick")
        # Last local identity check, immediately before the unchanged actor.
        self._require_current()
        result = self.executor.dispatch(copy.deepcopy(record),
                                        self.committed_tick, self.basis_sha256)
        self.command_seq = self.executor.next_seq
        if str(result.get("status")).lower() == "admitted":
            self._inflight[record["seq"]] = (
                action_id, record["tick"], self.epoch, self.basis_sha256)
        return result
