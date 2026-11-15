# @module {"id":"wa.perceive.vision_engine_real","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_adapter"],"book":"HB-WA-s1-perceive"}
"""Real checkout bridge; engine code is imported only in a bounded child process.

Origin APIs: vision-engine/eval/v2x/sf_tiledesc.py and
vision-engine/visioncore/{pipeline,frame,change,regions}.py. No engine code is
ported here. VISION_ENGINE_ROOT selects the checkout, not a pip installation.
Frames travel over anonymous pipes and remain in RAM. The child forbids disk
writes and disables bytecode caches; it never supplies pipeline.out_dir.

screen_identity preserves the adapter's hex digest contract. Use same_screen
for the engine's heuristic comparison, rather than comparing digest strings.
Known engine defect B11: a global one-level brightness shift can break
same_screen. This bridge uses the engine result unchanged, without correction.
Each operation starts a fresh interpreter: isolation trades startup cost for
no changes to the caller's sys.path or engine entries in sys.modules.

Persistent describe_many path: reuses an injected bounded runner across calls,
validates frames before dispatch, indexes results by original identity, and
isolates per-item failure and runner death.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import pickle
import subprocess
import sys
from typing import Sequence

import numpy as np

from .vision_adapter import Frame, VisionEngineAdapter


# Default: sibling checkout in the assembled ViewPoint tree (components/vision-engine); override with VISION_ENGINE_ROOT.
_DEFAULT_ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[3] / "vision-engine")
_WORKER = r'''
import importlib.util
import os
import pickle
import sys

# Deny filesystem writes, including accidental future engine output paths.
def ram_only(event, args):
    if event == "open":
        mode, flags = args[1:3]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            )
        ):
            raise RuntimeError("STORAGE LAW: disk writes forbidden")
    if event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink"}:
        raise RuntimeError("STORAGE LAW: filesystem mutation forbidden")

sys.addaudithook(ram_only)
root, operation = sys.argv[1:3]
sys.path.insert(0, root)
import numpy as np

buffers = pickle.loads(sys.stdin.buffer.read())
arrays = [np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
          for width, height, data in buffers]
if operation == "regions":
    from visioncore.frame import Frame, MemoryFrameSource
    from visioncore.pipeline import analyze
    frames = [Frame(i, i, None, rgb) for i, rgb in enumerate(arrays)]
    source = MemoryFrameSource(frames, {"width": buffers[0][0], "height": buffers[0][1]})
    run = analyze(source, out_dir=None, workers=1)
    result = []
    for region in run.regions[-1]:
        x, y, w, h = region["bbox_px"]
        result.append(dict(x=int(x), y=int(y), w=int(w), h=int(h),
                           tiles=int(region["n_tiles"])))
    result.sort(key=lambda r: (r["y"], r["x"], r["h"], r["w"], r["tiles"]))
else:
    path = os.path.join(root, "eval", "v2x", "sf_tiledesc.py")
    spec = importlib.util.spec_from_file_location("_wender_sf_tiledesc", path)
    sf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sf)
    fingerprints = [sf.screen_fingerprint(rgb) for rgb in arrays]
    if operation == "fingerprint":
        result = fingerprints[0]
    elif operation == "compare":
        result = (sf.same_screen(*fingerprints), sf.fingerprint_distance(*fingerprints))
    else:
        raise ValueError("unknown bridge operation")
sys.stdout.buffer.write(pickle.dumps(result, protocol=4))
'''


# --- Canonical Frozen Record Dataclasses ---

try:
    from .session_registry import (
        Handle,
        TargetRecord,
        FrameRef,
        Observation,
        Receipt,
        ActionRequest,
        ActorLease,
        FocusLease,
        StreamSpec,
        StreamToken,
        Ticket,
        ChangeResult,
        PerceptionResult,
        WindowFrames,
        WindowDescription,
    )
except (ModuleNotFoundError, ImportError) as _sr_err:
    if isinstance(_sr_err, ModuleNotFoundError) and _sr_err.name not in (
        "wender.perceive.session_registry",
        "session_registry",
    ):
        raise

    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int

        def __getitem__(self, item):
            if item in ("kind", "canonical_id", "generation"):
                return getattr(self, item)
            raise KeyError(item)

    @dataclass(frozen=True)
    class TargetRecord:
        handle: Handle
        transport: object
        owned: bool
        epoch: int
        session_id: str | None
        attachment_owned: bool
        closed: bool

    @dataclass(frozen=True)
    class FrameRef:
        handle: Handle
        epoch: int
        frame_seq: int
        width: int
        height: int
        rgb: object
        digest: str

        def __post_init__(self):
            if hasattr(self.rgb, "flags") and hasattr(self.rgb, "dtype"):
                if getattr(self.rgb, "dtype", None) is not None:
                    if str(self.rgb.dtype) != "uint8":
                        raise ValueError("rgb must be uint8 array")
                try:
                    self.rgb.flags.writeable = False
                except (AttributeError, ValueError):
                    pass

    @dataclass(frozen=True)
    class Observation:
        handle: Handle
        epoch: int
        frame_seq: int
        dpr_ppm: int
        state: dict
        frame: FrameRef

        def __post_init__(self):
            if not isinstance(self.dpr_ppm, int) or isinstance(self.dpr_ppm, bool) or self.dpr_ppm <= 0:
                raise ValueError("dpr_ppm must be a positive int")
            if not isinstance(self.frame_seq, int) or isinstance(self.frame_seq, bool) or self.frame_seq < 0:
                raise ValueError("frame_seq must be a non-negative int")
            if not isinstance(self.epoch, int) or isinstance(self.epoch, bool) or self.epoch < 0:
                raise ValueError("epoch must be a non-negative int")

    @dataclass(frozen=True)
    class Receipt:
        action_id: str
        handle: Handle
        status: str
        reason: str | None = None

    @dataclass(frozen=True)
    class ActionRequest:
        handle: Handle
        action_id: str
        epoch: int
        payload: dict
        cost: int
        deadline_ns: int

    @dataclass(frozen=True)
    class ActorLease:
        handle: Handle
        owner: str
        generation: int
        nonce: bytes
        expires_ns: int

    @dataclass(frozen=True)
    class FocusLease:
        lease: ActorLease
        domain: str
        epoch: int

    @dataclass(frozen=True)
    class StreamSpec:
        width: int
        height: int
        slots: int
        quota_bytes: int

    @dataclass(frozen=True)
    class StreamToken:
        handle: Handle
        epoch: int

    @dataclass(frozen=True)
    class Ticket:
        action_id: str
        handle: Handle

    @dataclass(frozen=True)
    class ChangeResult:
        handle: Handle
        epoch: int
        settled: bool
        claims: np.ndarray
        ops: dict

    @dataclass(frozen=True)
    class PerceptionResult:
        handle: Handle
        observation: Observation | None = None
        error: str | None = None

    @dataclass(frozen=True)
    class WindowFrames:
        handle: Handle
        epoch: int
        frames: tuple
        frame_seqs: tuple
        dpr_ppm: int

        def __post_init__(self):
            if len(self.frames) != len(self.frame_seqs):
                raise ValueError("frames and frame_seqs must have equal length")
            for i in range(len(self.frame_seqs)):
                if not isinstance(self.frame_seqs[i], int) or isinstance(self.frame_seqs[i], bool) or self.frame_seqs[i] < 0:
                    raise ValueError("frame_seqs elements must be non-negative ints")
            for i in range(1, len(self.frame_seqs)):
                if self.frame_seqs[i] <= self.frame_seqs[i - 1]:
                    raise ValueError("frame_seqs must be strictly increasing")
            if not isinstance(self.dpr_ppm, int) or isinstance(self.dpr_ppm, bool) or self.dpr_ppm <= 0:
                raise ValueError("dpr_ppm must be a positive int")

    @dataclass(frozen=True)
    class WindowDescription:
        handle: Handle
        epoch: int
        frame_seqs: tuple
        descriptors: tuple
        regions: tuple
        error: str | None = None

        def __post_init__(self):
            if len(self.descriptors) != len(self.frame_seqs):
                raise ValueError("descriptors and frame_seqs must have equal length")
            if len(self.regions) != len(self.frame_seqs):
                raise ValueError("regions and frame_seqs must have equal length")
            for i in range(len(self.frame_seqs)):
                if not isinstance(self.frame_seqs[i], int) or isinstance(self.frame_seqs[i], bool) or self.frame_seqs[i] < 0:
                    raise ValueError("frame_seqs elements must be non-negative ints")


SUPPORTED_TARGET_KINDS = frozenset({"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"})


# --- Typed ValueError Subclasses for Admission Validation ---

# @anchor:wa.perceive.vision_engine_real.FrameValidationError
class FrameValidationError(ValueError):
    """Base error for frame admission validation."""
    pass


# @anchor:wa.perceive.vision_engine_real.InvalidDprError
class InvalidDprError(FrameValidationError):
    """Raised when per-target DPR is not a positive integer."""
    pass


# @anchor:wa.perceive.vision_engine_real.InvalidFrameSequenceError
class InvalidFrameSequenceError(FrameValidationError):
    """Raised when frame sequences are non-increasing or mismatch frame count."""
    pass


# @anchor:wa.perceive.vision_engine_real.InvalidTargetError
class InvalidTargetError(FrameValidationError):
    """Raised when target handle kind or identity is invalid or unsupported."""
    pass


# @anchor:wa.perceive.vision_engine_real.FrameDtypeError
class FrameDtypeError(FrameValidationError):
    """Raised when RGB frame is not contiguous uint8 with shape (H,W,3)."""
    pass


# @anchor:wa.perceive.vision_engine_real.RunnerDeathError
class RunnerDeathError(RuntimeError):
    """Raised when persistent worker runner dies or terminates unexpectedly."""
    pass


# @anchor:wa.perceive.vision_engine_real.validate_window_frames
def validate_window_frames(item: WindowFrames) -> WindowFrames:
    """Validate window frames before dispatch (INV-M3-1, INV-M3-2).

    Validates target handle kind, positive DPR, monotonic frame sequences,
    and contiguous uint8 RGB arrays, admitting them into owned read-only RAM.
    """
    if not hasattr(item, "handle"):
        raise InvalidTargetError("WindowFrames must bind a target handle")
    h = item.handle
    if not hasattr(h, "kind") or not hasattr(h, "canonical_id") or not hasattr(h, "generation"):
        raise InvalidTargetError("handle must have kind, canonical_id, and generation fields")
    if h.kind not in SUPPORTED_TARGET_KINDS:
        raise InvalidTargetError(
            f"Unsupported target kind: {h.kind!r}; supported: {sorted(SUPPORTED_TARGET_KINDS)}"
        )
    if not isinstance(h.canonical_id, str) or not h.canonical_id:
        raise InvalidTargetError("canonical_id must be a non-empty string")
    if not isinstance(h.generation, int) or isinstance(h.generation, bool):
        raise InvalidTargetError("generation must be an integer")

    if not isinstance(item.epoch, int) or isinstance(item.epoch, bool):
        raise FrameValidationError("epoch must be an integer")

    if not isinstance(item.dpr_ppm, int) or isinstance(item.dpr_ppm, bool) or item.dpr_ppm <= 0:
        raise InvalidDprError(f"dpr_ppm must be a positive integer, got {item.dpr_ppm!r}")

    frames = item.frames
    seqs = item.frame_seqs
    if not isinstance(frames, (tuple, list)) or not isinstance(seqs, (tuple, list)):
        raise InvalidFrameSequenceError("frames and frame_seqs must be sequence collections")
    if len(frames) != len(seqs):
        raise InvalidFrameSequenceError(
            f"frames and frame_seqs have unequal length ({len(frames)} vs {len(seqs)})"
        )
    if len(frames) == 0:
        raise InvalidFrameSequenceError("WindowFrames must contain at least one frame")

    for i in range(len(seqs)):
        if not isinstance(seqs[i], int) or isinstance(seqs[i], bool) or seqs[i] < 0:
            raise InvalidFrameSequenceError(f"frame_seqs element {i} must be a non-negative integer")
        if i > 0 and seqs[i] <= seqs[i - 1]:
            raise InvalidFrameSequenceError(
                f"frame_seqs must be strictly increasing: {seqs[i - 1]} >= {seqs[i]}"
            )

    admitted_frames = []
    first_shape = None
    for i, frame in enumerate(frames):
        if not isinstance(frame, np.ndarray):
            raise FrameDtypeError(f"frame {i} must be a numpy.ndarray, got {type(frame).__name__}")
        if frame.dtype != np.uint8:
            raise FrameDtypeError(f"frame {i} dtype must be uint8, got {frame.dtype}")
        if frame.ndim != 3 or frame.shape[2] != 3 or min(frame.shape[:2]) <= 0:
            raise FrameDtypeError(f"frame {i} must have shape (H, W, 3) with H>0, W>0, got {frame.shape}")
        if first_shape is None:
            first_shape = frame.shape[:2]
        elif frame.shape[:2] != first_shape:
            raise FrameValidationError(
                f"frame {i} shape {frame.shape[:2]} does not match stream geometry {first_shape}"
            )

        # Copy into owned RAM and mark read-only after admission (INV-M3-1, INV-M3-3)
        owned = np.ascontiguousarray(frame, dtype=np.uint8).copy()
        owned.flags.writeable = False
        admitted_frames.append(owned)

    return WindowFrames(
        handle=item.handle,
        epoch=item.epoch,
        frames=tuple(admitted_frames),
        frame_seqs=tuple(seqs),
        dpr_ppm=item.dpr_ppm,
    )


# @anchor:wa.perceive.vision_engine_real.RealVisionEngine
class RealVisionEngine(VisionEngineAdapter):
    """Adapter for RAM RGB24 Frames or NumPy uint8[H,W,3] arrays.

    Missing or incomplete checkouts fail explicitly; there is no fake fallback.
    """

    def __init__(self, *args, runner=None, **kwargs):
        self.runner = runner if runner is not None else (args[0] if args else None)
        root_val = kwargs.get("root", os.environ.get("VISION_ENGINE_ROOT", _DEFAULT_ROOT))
        self.root = Path(root_val).resolve()
        if self.runner is None:
            for relative in ("visioncore/pipeline.py", "eval/v2x/sf_tiledesc.py"):
                if not (self.root / relative).is_file():
                    raise FileNotFoundError(
                        "VISION_ENGINE_ROOT missing engine source: %s" % (self.root / relative)
                    )

    def _buffer(self, frame):
        if isinstance(frame, Frame):
            return frame.width, frame.height, frame.data
        # Do not import NumPy into the host just to load this optional adapter.
        import numpy as np
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
                or frame.ndim != 3 or frame.shape[2] != 3
                or min(frame.shape[:2]) <= 0):
            raise ValueError("frame must be a Frame or uint8 NumPy array of shape (H,W,3)")
        height, width = frame.shape[:2]
        return int(width), int(height), frame.tobytes(order="C")

    def _run(self, operation, *frames):
        buffers = [self._buffer(frame) for frame in frames]
        if any(buf[:2] != buffers[0][:2] for buf in buffers):
            if operation == "regions":
                raise ValueError("Frame dimensions do not match")
        completed = subprocess.run(
            [sys.executable, "-B", "-c", _WORKER, str(self.root), operation],
            input=pickle.dumps(buffers, protocol=4), capture_output=True,
            timeout=60, check=False,
        )
        if completed.returncode:
            raise RuntimeError("vision-engine %s failed: %s" % (
                operation, completed.stderr.decode("utf-8", errors="replace")[-4000:]))
        return pickle.loads(completed.stdout)

    # @anchor:wa.perceive.vision_engine_real.RealVisionEngine.describe_many
    def describe_many(self, items: Sequence[WindowFrames], *, runner=None) -> list[PerceptionResult]:
        """Describe multiple window frame sequences through a persistent runner.

        Reuses the instance runner across batches, validates frames before dispatch,
        indexes results by input identity, and isolates per-item failure and runner death.

        Args:
            items: Sequence of WindowFrames to process.
            runner: Optional runner override for this call only.

        Returns:
            list[PerceptionResult] indexed by original input identity.
        """
        active_runner = runner if runner is not None else self.runner
        if active_runner is None:
            raise ValueError("No worker runner provided or configured for describe_many")

        results = [None] * len(items)
        admitted_items = []

        # Phase 1: Validation before dispatch (INV-M3-1, INV-M3-2)
        for idx, item in enumerate(items):
            try:
                admitted = validate_window_frames(item)
                admitted_items.append((idx, admitted))
            except Exception as val_err:
                handle = _receipt_handle(getattr(item, "handle", None))
                results[idx] = PerceptionResult(
                    handle=handle,
                    observation=None,
                    error=f"{type(val_err).__name__}: {val_err}",
                )

        # Phase 2: Dispatch valid items to persistent runner
        runner_dead = False
        death_error_msg = None

        for idx, admitted in admitted_items:
            handle = admitted.handle

            if runner_dead:
                # Mark unfinished work after runner death without replaying completed items
                results[idx] = PerceptionResult(
                    handle=handle,
                    observation=None,
                    error=f"RunnerDeathError: runner terminated before dispatch; unfinished item ({death_error_msg})",
                )
                continue

            try:
                # Check runner liveness if exposed
                if hasattr(active_runner, "is_alive") and callable(active_runner.is_alive):
                    if not active_runner.is_alive():
                        runner_dead = True
                        death_error_msg = "runner is not alive"
                        results[idx] = PerceptionResult(
                            handle=handle,
                            observation=None,
                            error=f"RunnerDeathError: {death_error_msg}",
                        )
                        continue
                elif getattr(active_runner, "alive", True) is False:
                    runner_dead = True
                    death_error_msg = "runner marked not alive"
                    results[idx] = PerceptionResult(
                        handle=handle,
                        observation=None,
                        error=f"RunnerDeathError: {death_error_msg}",
                    )
                    continue

                # Dispatch single window sequence separately (INV-M3-2)
                if hasattr(active_runner, "describe_window"):
                    raw = active_runner.describe_window(admitted)
                elif hasattr(active_runner, "describe"):
                    raw = active_runner.describe(admitted)
                elif hasattr(active_runner, "run"):
                    raw = active_runner.run(admitted)
                elif callable(active_runner):
                    raw = active_runner(admitted)
                elif hasattr(active_runner, "describe_many"):
                    raw_list = active_runner.describe_many([admitted])
                    raw = raw_list[0] if raw_list else None
                else:
                    raise TypeError(
                        f"Injected runner {type(active_runner).__name__} does not implement describe_window or __call__"
                    )

                raw = _bind_window_result(raw, admitted)
                # Format result into PerceptionResult
                if isinstance(raw, PerceptionResult):
                    results[idx] = raw
                elif isinstance(raw, Observation):
                    results[idx] = PerceptionResult(handle=handle, observation=raw, error=None)
                elif isinstance(raw, WindowDescription):
                    if raw.error is not None:
                        results[idx] = PerceptionResult(handle=handle, observation=None, error=raw.error)
                    else:
                        last_frame = admitted.frames[-1]
                        digest = hashlib.sha256(last_frame.tobytes()).hexdigest()
                        frame_ref = FrameRef(
                            handle=handle,
                            epoch=admitted.epoch,
                            frame_seq=admitted.frame_seqs[-1],
                            width=int(last_frame.shape[1]),
                            height=int(last_frame.shape[0]),
                            rgb=last_frame,
                            digest=digest,
                        )
                        obs = Observation(
                            handle=handle,
                            epoch=admitted.epoch,
                            frame_seq=admitted.frame_seqs[-1],
                            dpr_ppm=admitted.dpr_ppm,
                            state={
                                "descriptors": raw.descriptors,
                                "regions": raw.regions,
                                "error": raw.error,
                            },
                            frame=frame_ref,
                        )
                        results[idx] = PerceptionResult(handle=handle, observation=obs, error=None)
                elif isinstance(raw, dict):
                    if raw.get("error") is not None:
                        results[idx] = PerceptionResult(handle=handle, observation=None, error=str(raw["error"]))
                    else:
                        last_frame = admitted.frames[-1]
                        digest = hashlib.sha256(last_frame.tobytes()).hexdigest()
                        frame_ref = FrameRef(
                            handle=handle,
                            epoch=admitted.epoch,
                            frame_seq=admitted.frame_seqs[-1],
                            width=int(last_frame.shape[1]),
                            height=int(last_frame.shape[0]),
                            rgb=last_frame,
                            digest=digest,
                        )
                        obs = Observation(
                            handle=handle,
                            epoch=admitted.epoch,
                            frame_seq=admitted.frame_seqs[-1],
                            dpr_ppm=admitted.dpr_ppm,
                            state=raw,
                            frame=frame_ref,
                        )
                        results[idx] = PerceptionResult(handle=handle, observation=obs, error=None)
                else:
                    last_frame = admitted.frames[-1]
                    digest = hashlib.sha256(last_frame.tobytes()).hexdigest()
                    frame_ref = FrameRef(
                        handle=handle,
                        epoch=admitted.epoch,
                        frame_seq=admitted.frame_seqs[-1],
                        width=int(last_frame.shape[1]),
                        height=int(last_frame.shape[0]),
                        rgb=last_frame,
                        digest=digest,
                    )
                    obs = Observation(
                        handle=handle,
                        epoch=admitted.epoch,
                        frame_seq=admitted.frame_seqs[-1],
                        dpr_ppm=admitted.dpr_ppm,
                        state={"result": raw},
                        frame=frame_ref,
                    )
                    results[idx] = PerceptionResult(handle=handle, observation=obs, error=None)

            except Exception as exc:
                # Distinguish runner death from item-level failure
                is_death = (
                    isinstance(exc, (RunnerDeathError, BrokenPipeError, ProcessLookupError, ConnectionResetError))
                    or getattr(exc, "runner_dead", False)
                    or getattr(active_runner, "alive", True) is False
                    or (hasattr(active_runner, "is_alive") and callable(active_runner.is_alive) and not active_runner.is_alive())
                )
                if is_death:
                    runner_dead = True
                    death_error_msg = f"{type(exc).__name__}: {exc}"
                    results[idx] = PerceptionResult(
                        handle=handle,
                        observation=None,
                        error=death_error_msg,
                    )
                else:
                    # Item failure is isolated
                    results[idx] = PerceptionResult(
                        handle=handle,
                        observation=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )

        return results

    # @anchor:wa.perceive.vision_engine_real.RealVisionEngine.screen_identity
    def screen_identity(self, frame):
        """Return the adapter's hex identity, derived from the engine fingerprint."""
        return hashlib.sha256(self.screen_fingerprint(frame)).hexdigest()[:16]

    # @anchor:wa.perceive.vision_engine_real.RealVisionEngine.screen_fingerprint
    def screen_fingerprint(self, frame):
        """Return the engine's immutable descriptor bytes, not pixels."""
        return self._run("fingerprint", frame)

    # @anchor:wa.perceive.vision_engine_real.RealVisionEngine.same_screen
    def same_screen(self, first, second):
        """Compare frames using the unmodified engine heuristic (including B11)."""
        return self._run("compare", first, second)[0]

    # @anchor:wa.perceive.vision_engine_real.RealVisionEngine.fingerprint_distance
    def fingerprint_distance(self, first, second):
        """Return the engine's integer fingerprint distance between frames."""
        return self._run("compare", first, second)[1]

    # @anchor:wa.perceive.vision_engine_real.RealVisionEngine.changed_regions
    def changed_regions(self, prev, cur):
        """Run real S0-S4 change/regions and return FakeVisionEngine-shaped records."""
        return self._run("regions", prev, cur)


def _receipt_handle(value):
    """Preserve a canonical target when malformed input can still identify it."""
    try:
        return Handle(value.kind, value.canonical_id, value.generation)
    except (AttributeError, ValueError, TypeError):
        return None


def _bind_window_result(raw, admitted):
    """Bind typed runner responses to the admitted window before publication."""
    if raw is None:
        raise FrameValidationError("runner returned no result")
    if isinstance(raw, (PerceptionResult, Observation, WindowDescription)):
        if _receipt_handle(raw.handle) != admitted.handle:
            raise FrameValidationError("runner result target handle mismatch")
    if isinstance(raw, PerceptionResult):
        if raw.error is not None:
            if not isinstance(raw.error, str) or not raw.error or raw.observation is not None:
                raise FrameValidationError("runner returned an invalid error receipt")
            return PerceptionResult(admitted.handle, None, raw.error)
        if raw.observation is None:
            raise FrameValidationError("runner returned no observation or error")
        return PerceptionResult(admitted.handle, _bind_window_result(raw.observation, admitted), None)
    if isinstance(raw, WindowDescription):
        if (raw.epoch, tuple(raw.frame_seqs)) != (admitted.epoch, admitted.frame_seqs):
            raise FrameValidationError("runner result window epoch/sequence mismatch")
    if isinstance(raw, Observation):
        if (raw.epoch, raw.frame_seq, raw.dpr_ppm) != (
                admitted.epoch, admitted.frame_seqs[-1], admitted.dpr_ppm):
            raise FrameValidationError("runner result observation epoch/sequence/DPR mismatch")
        last = admitted.frames[-1]
        frame = raw.frame
        expected = (admitted.handle, admitted.epoch, admitted.frame_seqs[-1],
                    int(last.shape[1]), int(last.shape[0]),
                    hashlib.sha256(last.tobytes(order="C")).hexdigest())
        if (_receipt_handle(frame.handle), frame.epoch, frame.frame_seq,
                frame.width, frame.height, frame.digest) != expected:
            raise FrameValidationError("runner result frame binding mismatch")
        verified = FrameRef(frame.handle, frame.epoch, frame.frame_seq, frame.width,
                            frame.height, frame.rgb, frame.digest)
        return Observation(admitted.handle, raw.epoch, raw.frame_seq, raw.dpr_ppm,
                           raw.state, verified)
    return raw
