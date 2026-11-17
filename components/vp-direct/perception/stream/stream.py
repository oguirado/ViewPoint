"""An owner-scoped, pull-driven producer over vadapt.x11_ramcap.

The lifecycle owner supplies an admitted broker binding and the associated
trusted private Xvfb source. Calls are serialized per stream. Only owned RAM
rings are covered by scrub verification. Pixels explicitly copied by a reader
are outside that boundary.
"""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import secrets
import sys
import threading

_DIRECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_DIRECT))
sys.path.insert(0, str(_DIRECT.parent / "vision-engine"))

from schemas.validation import validate_contract
from vadapt import ramws, x11_ramcap
from .storage import ScrubWorkspace


class StreamError(RuntimeError):
    pass


@dataclass(frozen=True)
class StreamToken:
    endpoint: str
    surface_id: str
    incarnation: str
    owner: str
    authority_epoch: int
    generation: int
    nonce: str


@dataclass(frozen=True)
class Observation:
    token: StreamToken
    observation_id: str
    render_index: int
    frame_sha256: str

    @property
    def stream_generation(self):
        return self.token.generation


class PerceptionStream:
    """pause retains storage. wake reaps it and proves fresh output before admission.

    close() may be retried after a retained mapping is released. A quarantined
    producer must settle through close() before the next wake. No transition
    changes the application, broker binding, or session authority.
    """
    def __init__(self, binding, source_config, *, ring_slots, interval_ns, ttl_ns,
                 max_frame_bytes, max_ring_bytes, workspace_root=ramws.ROOT):
        validate_contract("vp.surface_binding/v1", binding)
        # RamCapture source aliases are local names. The broker endpoint may
        # be a URI and remains part of the issued identity verbatim.
        self._identity = (binding["endpoint"], binding["surface_id"], binding["incarnation"],
                          binding["owner"], int(binding["authority_epoch"]))
        self._workspace_root = workspace_root
        self._lock = threading.Lock()
        self._state = "OFF"
        self._generation = 0
        self._producer = None
        self._observation = self._frame = None
        self._charge = 0
        self._workspace = None
        self._last_scrubbed = None
        self._capture = x11_ramcap.RamCapture(
            {"source": source_config}, max_frame_bytes=max_frame_bytes,
            max_session_bytes=max_ring_bytes, max_total_bytes=max_ring_bytes,
            max_sessions=1, ring_slots=ring_slots, interval_ns=interval_ns,
            ttl_ns=ttl_ns, workspace_factory=self._allocate)

    def _allocate(self, producer, size):
        self._workspace = ScrubWorkspace(ramws.open_run(
            "streams", producer, size, tool="vp-perception-stream", root=self._workspace_root))
        return self._workspace

    def status(self):
        with self._lock:
            return dict(state=self._state, stream_generation=self._generation,
                        reserved_bytes=self._charge, ring_id=self._producer,
                        last_scrubbed_ring=self._last_scrubbed,
                        fresh_observation_id=(self._observation.observation_id
                                              if self._observation else None))

    def _retire(self):
        self._observation = self._frame = None
        # Fence all reads even when Stop, Reap or the next Start fails.
        self._state = "QUARANTINED"

    def _require(self, token):
        if (self._state != "ACTIVE" or self._observation is None
                or type(token) is not StreamToken or type(token.generation) is not int
                or type(token.authority_epoch) is not int or type(token.nonce) is not str
                or token != self._observation.token):
            raise StreamError("stale stream token")

    def _settle(self):
        if self._producer is None:
            return
        self._retire()
        producer = self._producer
        receipt = self._capture.Reap(producer, force=True)
        if receipt != {"reaped": [producer], "retained": []}:
            raise StreamError("ring retained or release unverified")
        if self._capture.SizeBytes(producer) != 0:
            raise StreamError("backend still charges reaped ring")
        if self._workspace is not None:
            if not self._workspace.closed:
                raise StreamError("workspace release unverified")
            if self._workspace.allocated_bytes:
                if not self._workspace.scrubbed:
                    raise StreamError("ring scrub unverified")
                self._last_scrubbed = producer
        self._producer = self._workspace = None
        self._charge = 0

    def _sample(self, token, minimum_index):
        frame = self._capture.capture_once(self._producer, wait=True)
        try:
            if (frame is None or type(frame.render_index) is not int
                    or frame.render_index < minimum_index or not frame.valid()
                    or not frame.view.nbytes):
                raise StreamError("no fresh producer publication")
            digest = hashlib.sha256(frame.view).hexdigest()
            if not frame.valid():
                raise StreamError("frame changed during observation")
            observation = Observation(token, secrets.token_hex(16),
                                      frame.render_index, digest)
            self._frame = frame
            self._observation = observation
            return observation
        finally:
            del frame  # A failure traceback must not keep a borrowed mapping.

    def wake(self):
        with self._lock:
            if self._state not in ("ASLEEP", "OFF"):
                raise StreamError("wake requires a paused or closed stream")
            self._settle()
            self._state = "WAKING"
            self._producer = secrets.token_hex(16)
            try:
                meta = self._capture.Start(self._producer, "source", background=False)
                self._charge = self._capture.SizeBytes(self._producer)
                if (meta["session_id"] != self._producer or meta["source"] != "source"
                        or meta["state"] != "running" or meta["frames"] != 1
                        or self._workspace is None or self._charge <= 0
                        or self._workspace.allocated_bytes != self._charge
                        or meta["ring_bytes"] != self._charge):
                    raise StreamError("Start did not open a new running producer")
                generation = self._generation + 1
                token = StreamToken(*self._identity, generation, secrets.token_hex(32))
                observation = self._sample(token, meta["frames"])
                self._generation = generation
                self._state = "ACTIVE"
                return observation
            except BaseException:
                self._retire()
                self._charge = max(self._charge, self._capture.SizeBytes(self._producer))
                if self._workspace is not None and not self._workspace.closed:
                    self._charge = max(self._charge, self._workspace.allocated_bytes)
                if self._charge:
                    self._capture.Stop(self._producer)
                elif self._workspace is None or self._workspace.closed:
                    # Start already rolled back before admission.
                    self._producer = self._workspace = None
                raise

    def observe(self, token):
        with self._lock:
            self._require(token)
            try:
                return self._sample(token, self._observation.render_index + 1)
            except BaseException:
                self._retire()
                self._capture.Stop(self._producer)
                raise

    def read(self, token, observation_id):
        """Copy the current observation for its authorized reader."""
        with self._lock:
            self._require(token)
            if observation_id != self._observation.observation_id or not self._frame.valid():
                raise StreamError("stale observation")
            pixels = self._frame.view.tobytes()
            if not self._frame.valid() or hashlib.sha256(pixels).hexdigest() != self._observation.frame_sha256:
                raise StreamError("frame changed during read")
            return pixels

    def pause(self, token):
        with self._lock:
            self._require(token)
            self._retire()
            if (not self._capture.Stop(self._producer)
                    or self._capture.SizeBytes(self._producer) != self._charge):
                raise StreamError("retained pause unverified")
            self._state = "ASLEEP"

    def close(self):
        with self._lock:
            self._settle()
            self._state = "OFF"
