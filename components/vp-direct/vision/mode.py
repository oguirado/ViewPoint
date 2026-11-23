"""Run frame acquisition, perception and planning under the P4 drain fence."""

import io
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic_ns

from PIL import Image

from bus.broker import BrokerError
from control.core.control import Handler, Outcome
from frames.source import FrameSource, FrameUnavailable, HostFrameState
from metaviewer_agents.observe import ObservationError, build_observation
from metaviewer_agents.perception.base import Frame, PerceptionError
from metaviewer_agents.perception.vcore import project_element_table
from perception.lifecycle import LifecycleError, PerceptionLifecycle
from privacy import PrivacyBlocked, PrivacyState, classify_host_privacy, host_privacy_for
from schemas.validation import ValidationError, validate_contract

from .admission import PooledPerceiver, VisionAdmissionError


# The sentinel runs synchronously inside the admitted capture. A shared frame
# source must fence the requesting owner, even when two modes use it.
_capture_owner = ContextVar("vision_capture_owner", default=None)


class VisionError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition, code):
    if not condition:
        raise VisionError(code)


@dataclass(frozen=True)
class CapturedFrame:
    pixels: Frame
    captured_ns: int
    capture_us: int
    atspi_sha256: str | None


class VisionFrames:
    """Adapt S5 to RGB24 using ports provisioned by the session owner.

    For host capture, S5 requires an A2 sentinel for the bound capture footprint.
    The read-only AT-SPI port resolves the supplied window binding on its
    provisioned session bus and returns a complete U1c snapshot on success.
    """

    def __init__(self, source, current, demand, *, atspi=None, clock=monotonic_ns):
        validate_contract("vp.demand/v1", demand)
        require(demand["interest"] == "VERIFIED", "NO_LEASE")
        self._source = source
        self._current = current
        self._demand = demand.copy()
        self._atspi = atspi
        self._clock = clock
        self._privacy_binding = None
        self._privacy_owner = None

    @classmethod
    def from_host_ports(cls, current, backends, register, demand, *, binding,
                        read_privacy_evidence, record_gap, request_human, atspi, clock=monotonic_ns):
        """Bind S5 and R2 to the same A2 sentinel for this host source."""
        def read_bound(bound):
            evidence = read_privacy_evidence(bound)
            metadata = classify_host_privacy(bound, evidence)
            capture = _capture_owner.get()
            owner = capture[1] if capture is not None and capture[0] is frames else frames._privacy_owner
            if (owner is not None and metadata is not None
                    and metadata.state in (PrivacyState.LOGIN, PrivacyState.REAUTHENTICATION)):
                # A2 writes U2a before it requests a human. Fence P4 durably
                # while the classified login still belongs to this footprint.
                owner.hold_for_privacy(before_notice=True)
            return evidence

        privacy_for = host_privacy_for(binding, read_bound, record_gap=record_gap, request_human=request_human)
        source = FrameSource(current, backends, register, privacy_for=privacy_for, clock=clock)
        frames = cls(source, current, demand, atspi=atspi, clock=clock)
        frames._privacy_binding = binding
        return frames

    def _bind_lifecycle(self, owner):
        # S5 can use the source without entering a VisionMode capture. Keep
        # the owner of this sentinel's footprint across those direct calls.
        bound = self._privacy_binding
        if (bound is not None and owner.binding["owner"] == self._demand["consumer"]
                and all(owner.binding[key] == getattr(bound, key)
                        for key in ("surface_id", "incarnation", "authority_epoch"))):
            self._privacy_owner = owner
            return True
        return False

    def confirm_privacy_clear(self, binding):
        """Use A2's bound revision checks without acquiring content or ending a gap."""
        state = self.current(binding)
        sentinel = self._source._sentinel(state)
        if sentinel is None:
            return False
        with sentinel._lock:
            first = sentinel._read()
            return (first is not None and first.binding == state.privacy_binding()
                    and first.state is PrivacyState.CLEAR and sentinel._read() == first
                    and self.current(binding) == state)

    def provision_lifecycle(self, *args, **ports):
        """Provision P4 with this source's A2 check, including for opaque adapters."""
        owner = PerceptionLifecycle(*args, confirm_privacy_clear=self.confirm_privacy_clear, **ports)
        self._bind_lifecycle(owner)
        return owner

    def current(self, binding):
        state = self._current(binding["surface_id"])
        require(isinstance(state, HostFrameState) and all(
            getattr(state, field) == binding[field]
            for field in ("surface_id", "incarnation", "authority_epoch")
        ), "STALE_FRAME")
        require(self._demand["surface_id"] == state.surface_id
                and self._demand["consumer"] == binding["owner"], "NO_LEASE")
        if state.display == ":0":
            require(state.hidden and state.backend == "cdp" and self._atspi is not None,
                    "DISPLAY_UNSUPPORTED")
        else:
            require(re.fullmatch(r"xpra:[0-9]+", state.display), "DISPLAY_UNSUPPORTED")
        return state

    def _host_readback(self, state, sentinel):
        def acquire(metadata):
            if metadata.binding != state.privacy_binding() or self._current(state.surface_id) != state:
                return metadata, None, False
            return metadata, self._atspi(state), True

        metadata, tree, current = sentinel.observe(acquire, lambda staged: staged)
        require(current, "STALE_FRAME")
        require(isinstance(tree, dict) and tree.get("atspi_capture") == "polling_only"
                and isinstance(tree.get("observation_id"), str) and tree["observation_id"]
                and type(tree.get("raw_count")) is int and tree["raw_count"] > 0
                and isinstance(tree.get("ax_sha256"), str)
                and re.fullmatch(r"[a-f0-9]{64}", tree["ax_sha256"]), "E_PRODUCER_ABSENT")
        return metadata, tree["ax_sha256"]

    def capture(self, state):
        # A frame-port adapter may delegate its capture to these host frames.
        request = _capture_owner.get()
        if self._privacy_owner is None and request is not None:
            self._bind_lifecycle(request[1])
        sentinel = atspi_metadata = atspi_digest = None
        try:
            if state.display == ":0":
                # Reuse the sentinel that S5 checks for this exact footprint.
                sentinel = self._source._sentinel(state)
                atspi_metadata, atspi_digest = self._host_readback(state, sentinel)
            return self._capture_frame(state, sentinel, atspi_metadata, atspi_digest)
        except PrivacyBlocked as exc:
            raise VisionError(exc.code) from None

    def _capture_frame(self, state, sentinel, atspi_metadata, atspi_digest):
        require(self._current(state.surface_id) == state, "STALE_FRAME")
        started = self._clock()
        descriptor = self._source.capture(self._demand)
        ram_ref = descriptor["ram_ref"]
        try:
            validate_contract("vp.frame/v1", descriptor)
            if state.display == ":0":
                require(descriptor["frame_kind"] == "cdp_png"
                        and descriptor["witness"]["cdp_target_id"] == state.cdp_target_id,
                        "DISPLAY_UNSUPPORTED")
            ended = self._clock()
            captured = descriptor["captured_mono_ns"]
            size = descriptor["width"], descriptor["height"]
            require(started <= captured <= ended and size == state.raster_size, "STALE_FRAME")
            png = self._source.read_ram(ram_ref)
            if sentinel is not None:
                current = sentinel.observe(lambda metadata: metadata, lambda metadata: metadata)
                require(current == atspi_metadata, "STALE_FRAME")
            require(sha256(png).hexdigest() == descriptor["frame_sha256"], "STALE_FRAME")
            with Image.open(io.BytesIO(png), formats=("PNG",)) as image:
                require(image.size == size, "STALE_FRAME")
                opaque = Image.new("RGBA", size, (0, 0, 0, 255))
                opaque.alpha_composite(image.convert("RGBA"))
                pixels = Frame(*size, opaque.convert("RGB").tobytes())
        finally:
            self._source.release(ram_ref)
        return CapturedFrame(pixels, captured, (ended - started) // 1000, atspi_digest)


def local_adapters(*, ve_root, wender_root, timeout, allow_oneshot_fallback=False,
                   marks_out=None, ocr_marks=False, tessdata_prefix=None, pool_helper=None):
    """Build locked providers and admit pixel work through the host vision pool."""
    from metaviewer_agents.perception.vcore import VCorePerceptionAdapter
    from metaviewer_agents.wender_bridge import WenderSubprocessAdapter

    def engine():
        return VCorePerceptionAdapter(ve_root, timeout=timeout, verify_lock=True,
                                      marks_out=marks_out, ocr_marks=ocr_marks,
                                      tessdata_prefix=tessdata_prefix,
                                      fallback_oneshot=allow_oneshot_fallback)

    return (
        lambda: PooledPerceiver(engine, pool_helper),
        lambda: WenderSubprocessAdapter(wender_root=wender_root, timeout=timeout, verify_lock=True),
    )


@contextmanager
def _ram_frame(pixels):
    # Wender's existing observation contract requires a path to RGB24 bytes.
    with TemporaryDirectory(prefix="vp-vision-", dir="/dev/shm") as directory:
        path = Path(directory) / "frame.rgb"
        path.write_bytes(pixels)
        yield str(path)


class VisionMode:
    """One surface's vision job. Planning never dispatches or verifies an act.

    Register drain with the owner of P4's drain_agent callback. P4 calls it
    after admitted callbacks finish. It takes only the resource lock so a
    control call waiting on a P4 transition cannot deadlock the drain.
    """

    _OPS = ("vp_vision_start", "vp_vision_stop", "vp_vision_status")

    def __init__(self, lifecycle, frames, adapters, *, session_origin_ns, capsule_id=None,
                 allow_oneshot_fallback=False):
        require(type(session_origin_ns) is int and session_origin_ns >= 0
                and type(allow_oneshot_fallback) is bool, "HOLD")
        self._lifecycle = lifecycle
        self._frames = frames
        self._capture_frames = frames
        # A port can expose the host's capture as a bound method. Register it
        # before S5 can capture directly. Opaque adapters that share S5 must
        # provision their lifecycle through the host frames before exposing S5.
        host_frames = frames if isinstance(frames, VisionFrames) else getattr(
            getattr(frames, "capture", None), "__self__", None)
        if isinstance(host_frames, VisionFrames) and host_frames._bind_lifecycle(lifecycle):
            self._capture_frames = host_frames
        self._vision_factory, self._planner_factory = adapters
        self._origin_ns = session_origin_ns
        self._capsule_id = capsule_id
        self._allow_fallback = allow_oneshot_fallback
        self._control = Lock()
        self._resources = Lock()
        self._perceiver = self._planner = None
        self._history = self._transport = None
        self._job = None
        self._attempted = self._tick = 0

    def handlers(self):
        """Supply the execute and dry-run ports for C1's non-adapter tools."""
        return {op: Handler(self._wire_execute, self._wire_preview) for op in self._OPS}

    @staticmethod
    def _wire_result(request, outcome):
        if not outcome.ok:
            return outcome
        # The frozen legacy result accepts an empty acknowledgement. It has
        # no result shape for reporting the active vision job.
        if request["op"] == "vp_vision_status":
            return Outcome(False, "HOLD", not_verified=("SCHEMA_GAP: vision status result",))
        return Outcome(True, outcome.code)

    def _wire_execute(self, request):
        return self._wire_result(request, self.execute(request))

    def _wire_preview(self, request):
        return self._wire_result(request, self.preview(request))

    def _scope(self, request):
        context = request["context"]
        binding = self._lifecycle.binding
        require(context["agent_instance"] == binding["owner"], "NO_LEASE")
        expected = dict(session_id=self._lifecycle.session_id, capsule_id=self._capsule_id,
                        incarnation=binding["incarnation"], authority_epoch=binding["authority_epoch"])
        for key in ("session_id", "capsule_id"):
            if key in request["args"]:
                require(request["args"][key] == expected[key], "identity_mismatch")
        for key in expected.keys() & context.keys():
            require(context[key] == expected[key], "identity_mismatch")

    def _check_lease(self):
        try:
            lease = self._lifecycle.broker.lease(self._lifecycle.session_id)
            require(lease["state"] == "AGENT" and lease["holder"] == self._lifecycle.binding["owner"],
                    "NO_LEASE")
        except (BrokerError, VisionError):
            self._lifecycle._require_privacy_clear()
            raise

    def _call(self, op):
        return self._lifecycle.vp_perception_call(
            dict(op=op, session_id=self._lifecycle.session_id, reason="vision"))

    @staticmethod
    def _active(receipt):
        observation = receipt["fresh_observation_id"]
        require(receipt["complete"] is True and receipt["to"] == "ACTIVE"
                and isinstance(observation, str) and observation, "STALE_FRAME")
        return observation

    def _status(self, receipt):
        with self._resources:
            return dict(running=self._job is not None and receipt["to"] == "ACTIVE"
                        and receipt["complete"] is True and self._attempted < self._job[1],
                        perception_state=receipt["to"], attempts=self._attempted,
                        observation_id=receipt["fresh_observation_id"], last_transport=self._transport)

    def _control_call(self, request, *, preview):
        try:
            validate_contract("vp.control.req/v1", request)
            require(request["op"] in self._OPS, "-32601")
            with self._control:
                self._scope(request)
                self._check_lease()
                preview = preview or request["dry_run"]
                if request["op"] == "vp_vision_stop" and not preview:
                    # Allow owner cleanup even while P4 refuses a fresh status.
                    self._job = None
                    self.drain()
                receipt = self._call("status")
                if request["op"] == "vp_vision_start":
                    args = request["args"]
                    job = args["goal"], args["steps"]
                    require(bool(job[0].strip()), "-32602")
                    require(self._job is None or self._job == job, "HOLD")
                    require(self._job is None or self._attempted < job[1], "HOLD")
                    with self._resources:
                        require(self._job is not None or self._perceiver is None, "HOLD")
                    self._frames.current(self._lifecycle.binding)
                    if preview:
                        return Outcome(True, "OK", self._status(receipt))
                    if receipt["to"] != "ACTIVE":
                        receipt = self._call("wake")
                    self._active(receipt)
                    if self._job is None:
                        self._job = job
                        self._attempted = 0
                return Outcome(True, "OK", self._status(receipt))
        except (VisionError, LifecycleError, BrokerError) as exc:
            return Outcome(False, str(exc))
        except ValidationError:
            return Outcome(False, "-32602")
        except (FrameUnavailable, OSError, PerceptionError, VisionAdmissionError):
            return Outcome(False, "HOLD")

    def execute(self, request):
        """Call locally with the bound owner's result, before C1 wire projection."""
        return self._control_call(request, preview=False)

    def preview(self, request):
        return self._control_call(request, preview=True)

    def drain(self):
        """Release vision-owned resources after the last admitted operation."""
        with self._resources:
            self._history = self._transport = None
            if self._perceiver is not None:
                self._perceiver.close()
                self._perceiver = None
            self._planner = None
        return True

    def analyze(self, observation_id):
        """Return a candidate plan and tri-state comparison for the cited observation."""
        with self._control:
            require(self._job is not None, "NO_LEASE")
            try:
                self._check_lease()
                result = self._lifecycle.run_agent(
                    "analysis", observation_id, lambda: self._analyze(observation_id))
            except (VisionError, LifecycleError, BrokerError, ObservationError, PerceptionError,
                    FrameUnavailable, OSError, ValidationError) as exc:
                code = str(exc) if isinstance(exc, BrokerError) else getattr(exc, "code", None)
                if code == "HUMAN_NEEDED":
                    try:
                        self.drain()
                    except (OSError, PerceptionError, VisionAdmissionError):
                        raise VisionError("HOLD") from None
                else:
                    with self._resources:
                        self._history = None
                if isinstance(exc, VisionError):
                    raise
                if not isinstance(exc, (LifecycleError, BrokerError)) and code not in (
                    "identity_mismatch", "STALE_FRAME", "HUMAN_NEEDED"
                ):
                    code = "HOLD"
                raise VisionError(code) from None
            return result

    def _analyze(self, observation_id):
        with self._resources:
            goal, steps = self._job
            require(self._attempted < steps, "HOLD")
            binding = self._lifecycle.binding
            state = self._frames.current(binding)
            previous = None
            if self._history is not None and self._history[:2] == (observation_id, state):
                previous = self._history[2]
            self._history = None
            stream = self._lifecycle.stream.status()
            require(stream["state"] == "ACTIVE" and stream["fresh_observation_id"] == observation_id,
                    "STALE_FRAME")
            token = _capture_owner.set((self._capture_frames, self._lifecycle))
            try:
                captured = self._frames.capture(state)
            except (FrameUnavailable, VisionError) as exc:
                if exc.code == "HUMAN_NEEDED":
                    self._lifecycle.hold_for_privacy()
                raise
            finally:
                _capture_owner.reset(token)
            require(self._frames.current(binding) == state, "STALE_FRAME")
            frame = captured.pixels
            digest = sha256(frame.data).hexdigest()
            self._tick += 1
            with _ram_frame(frame.data) as path:
                ref = dict(schema="frame_ref/v0.2", surface_id=state.surface_id,
                           generation=stream["stream_generation"], tick=self._tick,
                           ts_ns=captured.captured_ns - self._origin_ns, width=frame.width,
                           height=frame.height, format="rgb24", ramws_path=path,
                           sha256=digest, capture_us=captured.capture_us)
                build_observation(ref, surface_id=state.surface_id, generation=ref["generation"],
                                  contract_version="v0.2")
                self._attempted += 1
                vision, changed, fallback = self._perceive(ref, frame, previous)
                self._revalidate(observation_id, state)
                observation = build_observation(ref, vision=vision, surface_id=state.surface_id,
                                                generation=ref["generation"], contract_version="v0.2")
                if self._planner is None:
                    self._planner = self._planner_factory()
                table = project_element_table(ref, vision)
                plan = self._lifecycle.run_agent(
                    "decision", observation_id, lambda: self._planner.plan_step(observation, table, goal))
                self._revalidate(observation_id, state)
            self._history = observation_id, state, frame
            return dict(observation_id=observation_id, frame_sha256=digest, changed=changed,
                        atspi_sha256=captured.atspi_sha256, transport=self._transport,
                        fallback_from="vserve" if fallback else None, fallback_reason=fallback, plan=plan)

    def _revalidate(self, observation_id, state):
        require(self._frames.current(self._lifecycle.binding) == state, "STALE_FRAME")
        # The owner rechecks authority even when the geometry did not change.
        self._lifecycle.run_agent("analysis", observation_id, lambda: None)
        stream = self._lifecycle.stream.status()
        require(stream["state"] == "ACTIVE" and stream["fresh_observation_id"] == observation_id,
                "STALE_FRAME")

    def _perceive(self, ref, frame, previous):
        if self._perceiver is None:
            self._perceiver = self._vision_factory()
        provider = self._perceiver
        self._transport = None
        prior_error = provider.last_vserve_error
        try:
            vision = provider.perceive(ref, frame, prev=previous)
        except VisionAdmissionError:
            raise VisionError("HOLD") from None
        transport = provider.last_transport
        fallback = None
        if transport == "oneshot":
            error = provider.last_vserve_error
            fallback = getattr(error, "code", None)
            require(self._allow_fallback and error is not prior_error
                    and isinstance(fallback, str) and fallback
                    and fallback not in ("identity_mismatch", "STALE_FRAME"), "HOLD")
        else:
            require(transport == "vserve", "HOLD")
        payload = provider.last_c2_payload
        require(isinstance(payload, dict) and payload.get("frame_sha256") == ref["sha256"],
                "STALE_FRAME")
        changed = payload.get("changed")
        require("changed" in payload and ((changed is None and previous is None)
                or (type(changed) is bool and previous is not None)), "HOLD")
        self._transport = transport
        return vision, changed, fallback
