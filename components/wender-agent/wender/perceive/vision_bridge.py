# @module {"id":"wa.perceive.vision_bridge","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_adapter","wa.perceive.vserve_client"],"book":"HB-WA-s1-perceive"}
"""@module wa.perceive.vision_bridge - real hosted RAM-frame and C2 subprocess bridge.

Inputs are validated RGB24 ``Frame`` objects, including their width and height.
Dimensionless bytes and ``None`` are rejected: raw-byte shape recovery is undefined.
The deterministic offline default is ``FakeVisionEngine``. A host may supply a
``VisionEngineAdapter`` with the keyword-only ``adapter`` argument; this module
never imports an external checkout. Explicit subprocess selection uses C2 with
a scrubbed environment, anonymous memfd PNG descriptors, and RAM marks under
``/dev/shm/vqa-cu``. Explicit ``backend="persistent"`` opts into raw-RGB
vserve transport. [TARGET, unit decision] Runtime transport failure is reported
with a warning and ``last_fallback_reason`` before one-shot C2 is attempted
within the remaining call deadline. Configuration errors never fall back.
"""
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_FLOOR
import json
import hashlib
import warnings
import math
import os
from pathlib import Path
import re
import struct
import subprocess as _subprocess
import selectors
import time
import zlib

from .vision_adapter import (
    BRIDGE_STATUS, FakeVisionEngine, Frame, VisionEngineAdapter,
)


PERSISTENT_MODE = "vserve"

MAX_C2_OUTPUT_BYTES = 8 * 1024 * 1024

MAX_MARKS = 24
MAX_CHANGED_REGIONS = 16
MAX_CROPS = 8
ONE_SHOT_LATENCY_NOTE = "persistent reuse AUSENTE in one-shot C2; subprocess spawn cost per step [TARGET]"
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TESSDATA_PREFIX", "CU_STUB_LOG")


def _scrubbed_env():
    env = {
        "PATH": os.environ.get("PATH") or "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in _ENV_ALLOWLIST:
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def _validated_frame(frame):
    if not isinstance(frame, Frame):
        raise ValueError("frame must be a Frame with explicit width and height")
    # Revalidate even if a caller bypassed the frozen dataclass's constructor or
    # mutated it through object.__setattr__. Keep the original object and bytes.
    Frame.__post_init__(frame)
    return frame


# @anchor:wa.perceive.vision_bridge.screen_identity
def screen_identity(frame_rgb, *, adapter=None):
    """Return the adapter's screen identity for an explicit validated Frame."""
    frame = _validated_frame(frame_rgb)
    engine = FakeVisionEngine() if adapter is None else adapter
    return engine.screen_identity(frame)


# @anchor:wa.perceive.vision_bridge.changed_regions
def changed_regions(prev_frame_rgb, frame_rgb, *, adapter=None):
    """Return device-pixel changed regions for equal-sized validated Frames."""
    prev = _validated_frame(prev_frame_rgb)
    cur = _validated_frame(frame_rgb)
    if (prev.width, prev.height) != (cur.width, cur.height):
        raise ValueError("Frame dimensions do not match")
    engine = FakeVisionEngine() if adapter is None else adapter
    return engine.changed_regions(prev, cur)


# @anchor:wa.perceive.vision_bridge.screen_identities
def screen_identities(observations, *, adapter=None, runner=None):
    """Bind independent RGB stream observations to their own handles/epochs.

    Each batch result is indexed to its original observation even if the
    injected runner completes target sequences in reverse order. See
    parallel_perceive for the RAM-copy and error-receipt boundary.
    """
    from .parallel_perceive import perceive_many

    return perceive_many(observations, adapter=adapter, runner=runner)


# @anchor:wa.perceive.vision_bridge.VisionBridgeError
class VisionBridgeError(RuntimeError):
    """C2 launch, timeout, exit, or response-contract failure."""


# @anchor:wa.perceive.vision_bridge.ChangedRegion
@dataclass(frozen=True)
class ChangedRegion:
    x: int
    y: int
    w: int
    h: int
    score: float

    # @anchor:wa.perceive.vision_bridge.ChangedRegion.score_ppm
    @property
    def score_ppm(self):
        return _score_ppm(self.score)


# @anchor:wa.perceive.vision_bridge.Crop
@dataclass(frozen=True)
class Crop:
    x: int
    y: int
    w: int
    h: int
    reason: str


# @anchor:wa.perceive.vision_bridge.Mark
@dataclass(frozen=True)
class Mark:
    id: int
    x: int
    y: int
    w: int
    h: int
    kind: str
    score: float
    ocr_text: str | None = None
    ocr_score_promille: int | None = None
    visual_kind: str | None = None
    missing_reason: str | None = "ocr_not_requested"

    # @anchor:wa.perceive.vision_bridge.Mark.score_ppm
    @property
    def score_ppm(self):
        return _score_ppm(self.score)


# @anchor:wa.perceive.vision_bridge.DescribeResult
@dataclass(frozen=True)
class DescribeResult:
    schema: str
    frame_sha256: str
    width: int
    height: int
    changed: bool | None
    changed_regions: tuple[ChangedRegion, ...]
    crops: tuple[Crop, ...]
    timing_us: int
    marks: tuple[Mark, ...] = ()
    marks_png: str | None = None
    mark_cap_reached: bool = False

    # @anchor:wa.perceive.vision_bridge.DescribeResult.to_integer_evidence
    def to_integer_evidence(self):
        """Detached private evidence; callers still need schema/export admission."""
        evidence = asdict(self)
        del evidence["marks_png"]
        for key in ("changed_regions", "marks"):
            rows = []
            for record in getattr(self, key):
                row = asdict(record)
                del row["score"]
                row["score_ppm"] = record.score_ppm
                rows.append(row)
            evidence[key] = rows
        return evidence


def _score_ppm(score):
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("score must be a finite unit ratio")
    return int((Decimal(str(score)) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))


def _ram_destination(value):
    destination = Path(value).resolve()
    if not destination.is_relative_to("/dev/shm/vqa-cu"):
        raise ValueError("marks path must stay under /dev/shm/vqa-cu")
    try:
        mounts = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), left.split()[4]))
            if destination.is_relative_to(mount):
                mounts.append((len(mount.parts), right.split()[0]))
        if not mounts or max(mounts, key=lambda item: item[0])[1] not in ("tmpfs", "ramfs"):
            raise ValueError("marks destination requires verified RAM filesystem")
    except (OSError, IndexError) as exc:
        raise ValueError("RAM mount evidence unavailable") from exc
    return destination


def _ocr_fields(candidate, requested):
    names = ("ocr_text", "ocr_score_promille", "visual_kind", "missing_reason")
    if not requested and not any(name in candidate for name in names):
        return None, None, None, "ocr_not_requested"
    text, confidence, kind, reason = (candidate[name] for name in names)
    for value in (text, reason):
        if value is not None:
            # vision-engine caps OCR text at 64 UTF-8 bytes (vadapt sanitize_ocr_text); a longer value means drift.
            if not isinstance(value, str) or not value or len(value.encode('utf-8')) > 64:
                raise ValueError("invalid OCR string")
            value.encode("utf-8", errors="strict")
    if kind not in ("textline", "pill", "icon_btn", "canvas_ctrl"):
        raise ValueError("invalid visual kind")
    if confidence is not None and (type(confidence) is not int or not 0 <= confidence <= 1000):
        raise ValueError("invalid OCR confidence")
    if text is None:
        if confidence is not None or reason is None:
            raise ValueError("missing OCR text requires reason and null confidence")
    elif confidence is None or reason is not None:
        raise ValueError("successful OCR requires confidence and no missing reason")
    return text, confidence, kind, reason


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("invalid integer")
    return value


def _box(row, width, height):
    x, y = _integer(row["x"]), _integer(row["y"])
    w, h = _integer(row["w"], 1), _integer(row["h"], 1)
    if x + w > width or y + h > height:
        raise ValueError("rectangle outside frame")
    return x, y, w, h


def _parse_description(stdout, *, ocr_requested=False):
    if len(stdout) > MAX_C2_OUTPUT_BYTES:
        raise VisionBridgeError("C2 stdout exceeds byte limit")
    try:
        row = json.loads(stdout)
        if not isinstance(row, dict):
            raise ValueError("response must be an object")
        if row["schema"] != "ve.cu_describe/v1":
            raise ValueError("unsupported schema")
        digest = row["frame_sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid RGB24 hash")
        width, height = _integer(row["width"], 1), _integer(row["height"], 1)
        changed = row["changed"]
        if changed is not None and type(changed) is not bool:
            raise ValueError("invalid changed flag")
        regions, crops = row["changed_regions"], row["crops"]
        if not isinstance(regions, list) or len(regions) > MAX_CHANGED_REGIONS:
            raise ValueError("invalid changed regions")
        if not isinstance(crops, list) or len(crops) > MAX_CROPS:
            raise ValueError("invalid crops")
        typed_regions = []
        for region in regions:
            if not isinstance(region, dict):
                raise ValueError("region must be an object")
            score = region["score"]
            _score_ppm(score)
            typed_regions.append(ChangedRegion(*_box(region, width, height), float(score)))
        if any(a.score < b.score for a, b in zip(typed_regions, typed_regions[1:])):
            raise ValueError("regions must be sorted by descending score")
        typed_crops = []
        for crop in crops:
            if not isinstance(crop, dict):
                raise ValueError("crop must be an object")
            if crop["reason"] not in ("changed", "salient"):
                raise ValueError("invalid crop reason")
            typed_crops.append(Crop(*_box(crop, width, height), crop["reason"]))
        marks, marks_png = [], None
        if ocr_requested or "marks" in row or "marks_png" in row:
            candidates, marks_png = row["marks"], row["marks_png"]
            if not isinstance(candidates, list) or len(candidates) > MAX_MARKS:
                raise ValueError("invalid marks")
            if not isinstance(marks_png, str):
                raise ValueError("marks_png must stay under /dev/shm/vqa-cu")
            _ram_destination(marks_png)
            for index, candidate in enumerate(candidates, 1):
                if not isinstance(candidate, dict):
                    raise ValueError("mark must be an object")
                if type(candidate["id"]) is not int or candidate["id"] != index:
                    raise ValueError("marks must have sequential unique ids")
                if candidate["kind"] not in ("button", "tab", "field", "row", "icon", "text", "unknown"):
                    raise ValueError("invalid mark kind")
                score = candidate["score"]
                _score_ppm(score)
                marks.append(Mark(index, *_box(candidate, width, height), candidate["kind"], float(score),
                                  *_ocr_fields(candidate, ocr_requested)))
        return DescribeResult(row["schema"], digest, width, height, changed,
                              tuple(typed_regions), tuple(typed_crops),
                              _integer(row["timing_us"]), tuple(marks), marks_png,
                              len(marks) == MAX_MARKS)
    except (ValueError, TypeError, KeyError, OverflowError, OSError, RuntimeError) as exc:
        raise VisionBridgeError("invalid C2 JSON response") from exc


def _write_png(stream, frame):
    def chunk(kind, data):
        stream.write(struct.pack(">I", len(data)))
        stream.write(kind)
        stream.write(data)
        stream.write(struct.pack(">I", zlib.crc32(data, zlib.crc32(kind))))

    stream.write(b"\x89PNG\r\n\x1a\n")
    chunk(b"IHDR", struct.pack(">IIBBBBB", frame.width, frame.height, 8, 2, 0, 0, 0))
    compressor = zlib.compressobj()
    stride = frame.width * 3
    data = memoryview(frame.data)
    # Feed at most one row, never build the filtered image or complete PNG.
    pending = bytearray()
    for start in range(0, len(data), stride):
        encoded = compressor.compress(b"\x00") + compressor.compress(data[start:start + stride])
        pending.extend(encoded)
        if len(pending) >= 65536:
            chunk(b"IDAT", pending)
            pending.clear()
    pending.extend(compressor.flush())
    chunk(b"IDAT", pending)
    chunk(b"IEND", b"")


def _run_c2(command, *, cwd, capture_output, timeout, pass_fds, env):
    """Drain both pipes with a deadline and a hard per-pipe memory ceiling."""
    deadline = time.monotonic() + timeout
    with _subprocess.Popen(command, cwd=cwd, stdout=_subprocess.PIPE,
                           stderr=_subprocess.PIPE, pass_fds=pass_fds, env=env) as child:
        try:
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            with selectors.DefaultSelector() as selector:
                for name in buffers:
                    selector.register(getattr(child, name), selectors.EVENT_READ, name)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _subprocess.TimeoutExpired(command, timeout)
                    for key, _ in selector.select(remaining):
                        buf = buffers[key.data]
                        block = os.read(key.fileobj.fileno(), min(65536, MAX_C2_OUTPUT_BYTES - len(buf) + 1))
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        if len(buf) + len(block) > MAX_C2_OUTPUT_BYTES:
                            raise VisionBridgeError("C2 %s exceeds byte limit" % key.data)
                        buf.extend(block)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _subprocess.TimeoutExpired(command, timeout)
            child.wait(timeout=remaining)
            return _subprocess.CompletedProcess(command, child.returncode,
                                                 bytes(buffers["stdout"]), bytes(buffers["stderr"]))
        except BaseException:
            child.kill()
            child.wait()
            raise


class _C2Subprocess:
    """Local injectable runner; never modify the shared subprocess module."""

    run = staticmethod(_run_c2)
    TimeoutExpired = _subprocess.TimeoutExpired


subprocess = _C2Subprocess()


# @anchor:wa.perceive.vision_bridge.SubprocessVisionEngine
class SubprocessVisionEngine(VisionEngineAdapter):
    """C2 subprocess adapter; Frame inputs use anonymous Linux RAM descriptors.

    ``describe`` also accepts existing PNG paths. Checkout selection is explicit
    through ``ve_root`` or VQA_VE_ROOT; no external Python code is imported here.
    """

    def __init__(self, ve_root=None, *, timeout=2.0, python="python3", persistent=False):
        root = ve_root if ve_root is not None else os.environ.get("VQA_VE_ROOT")
        if not root:
            raise VisionBridgeError("ve_root or VQA_VE_ROOT is required")
        self.ve_root = Path(root).resolve()
        if not self.ve_root.is_dir():
            raise VisionBridgeError("vision-engine checkout is not a directory")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.timeout = timeout
        self.python = os.fspath(python)
        if type(persistent) is not bool:
            raise ValueError("persistent must be a boolean")
        self._client = None
        self._closed = False
        self.last_vserve_error = None
        self.last_transport = None
        self.last_fallback_reason = None
        if persistent:
            from .vserve_client import VServeClient
            self._client = VServeClient(self.ve_root, python=self.python, timeout=timeout)
            self.persistent_mode = PERSISTENT_MODE

    def close(self):
        self._closed = True
        if self._client is not None:
            self._client.close()

    # @anchor:wa.perceive.vision_bridge.SubprocessVisionEngine.describe
    def describe(self, frame, prev=None, *, masks=(), marks_out=None, ocr_marks=False):
        """Describe a Frame or existing PNG path, preserving C2 score order."""
        if self._closed:
            raise VisionBridgeError("vision engine is closed")
        if type(ocr_marks) is not bool or (ocr_marks and marks_out is None):
            raise ValueError("OCR requires boolean opt-in and RAM marks output")
        if isinstance(frame, Frame):
            _validated_frame(frame)
        if isinstance(prev, Frame):
            _validated_frame(prev)
        if isinstance(frame, Frame) and isinstance(prev, Frame):
            if (frame.width, frame.height) != (prev.width, prev.height):
                raise ValueError("Frame dimensions do not match")
        if marks_out is not None:
            marks_out = _ram_destination(marks_out)
        masks = tuple(tuple(_integer(v, 0 if i < 2 else 1) for i, v in enumerate(mask)) for mask in masks)
        if any(len(mask) != 4 for mask in masks):
            raise ValueError("mask must contain x,y,w,h")
        deadline = time.monotonic() + self.timeout
        self.last_fallback_reason = None
        # Keep both transport errors available if C2 also fails.
        self.last_vserve_error = None
        persistent_result = None
        if self._client is not None:
            from .vserve_client import VServeError
            try:
                persistent_result = self._client.describe(frame, prev, masks=masks, marks_out=marks_out,
                                                         ocr_marks=ocr_marks, deadline=deadline)
                self.last_transport = "vserve"
            except VServeError as exc:
                self.last_vserve_error = exc
                self.last_fallback_reason = str(exc)
                warnings.warn("vserve runtime failure; falling back to C2: " + str(exc), RuntimeWarning,
                              stacklevel=2)
                if time.monotonic() >= deadline:
                    raise VisionBridgeError("vserve call deadline expired: " + str(exc)) from exc
        with ExitStack() as stack:
            fds = []

            def png_path(value):
                if not isinstance(value, Frame):
                    return str(Path(value).resolve())
                fd = os.memfd_create("wender-c2-frame", os.MFD_CLOEXEC)
                fds.append(fd)
                stream = stack.enter_context(os.fdopen(fd, "wb"))
                _write_png(stream, value)
                stream.flush()
                stream.seek(0)
                return "/proc/self/fd/%d" % fd

            try:
                if persistent_result is not None:
                    result = _subprocess.CompletedProcess([], 0, json.dumps(persistent_result).encode(), b"")
                else:
                    self.last_transport = "c2"
                    command = [self.python, "-m", "vadapt.cu_describe", "--frame", png_path(frame)]
                    if prev is not None:
                        command += ["--prev", png_path(prev)]
                    for mask in masks:
                        if len(mask) != 4:
                            raise ValueError("mask must contain x,y,w,h")
                        values = [_integer(v, 0 if i < 2 else 1) for i, v in enumerate(mask)]
                        command += ["--mask", ",".join(map(str, values))]
                    if marks_out is not None:
                        command += ["--marks-out", str(marks_out)]
                    if ocr_marks:
                        command.append("--ocr-marks")
                    command.append("--json")
                    result = subprocess.run(command, cwd=self.ve_root, capture_output=True,
                                            timeout=(self.timeout if self._client is None else max(0.001, deadline - time.monotonic())), pass_fds=tuple(fds),
                                            env=_scrubbed_env())
            except subprocess.TimeoutExpired as exc:
                raise VisionBridgeError("C2 subprocess timed out") from exc
            except OSError as exc:
                raise VisionBridgeError("C2 subprocess could not execute") from exc
        if result.returncode:
            raise VisionBridgeError("C2 subprocess exited with code %d: %s" %
                                    (result.returncode, result.stderr.decode("utf-8", errors="replace")[:1000]))
        description = _parse_description(result.stdout, ocr_requested=ocr_marks)
        if marks_out is not None:
            if description.marks_png != str(marks_out) or not marks_out.is_file():
                raise VisionBridgeError("C2 marks output disagrees with --marks-out")
        if (description.changed is None) != (prev is None):
            raise VisionBridgeError("C2 changed flag disagrees with --prev")
        if isinstance(frame, Frame) and (description.width, description.height) != (frame.width, frame.height):
            raise VisionBridgeError("C2 frame dimensions disagree with input")
        if self._client is not None and isinstance(frame, Frame):
            if description.frame_sha256 != hashlib.sha256(frame.data).hexdigest():
                self.close()
                raise VisionBridgeError("frame RGB24 custody digest mismatch")
        return description

    # @anchor:wa.perceive.vision_bridge.SubprocessVisionEngine.screen_identity
    def screen_identity(self, frame):
        """Return the C2 RGB24 digest truncated to the adapter identity width."""
        return self.describe(_validated_frame(frame)).frame_sha256[:16]

    # @anchor:wa.perceive.vision_bridge.SubprocessVisionEngine.changed_regions
    def changed_regions(self, prev, cur):
        """Project C2 regions onto the existing integer-only adapter records.

        C2 has no tile count; each returned rectangle is one region unit.
        """
        result = self.describe(_validated_frame(cur), prev=_validated_frame(prev))
        return sorted(({"x": r.x, "y": r.y, "w": r.w, "h": r.h, "tiles": 1}
                       for r in result.changed_regions),
                      key=lambda r: (r["y"], r["x"], r["h"], r["w"], r["tiles"]))


# @anchor:wa.perceive.vision_bridge.select_vision_engine
def select_vision_engine(backend="fake", **kwargs):
    """Select an explicit backend; failed subprocess configuration never falls back."""
    if backend == "fake":
        return FakeVisionEngine(**kwargs)
    if backend == "subprocess":
        return SubprocessVisionEngine(**kwargs)
    if backend == "persistent":
        if not kwargs.get("ve_root") and not os.environ.get("VQA_VE_ROOT"):
            raise ValueError("persistent configuration AUSENTE: ve_root or VQA_VE_ROOT is required")
        return SubprocessVisionEngine(persistent=True, **kwargs)
    raise ValueError("unknown vision backend: %s" % backend)
