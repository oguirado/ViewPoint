# @module {"id":"wa.learn.cu_loop","layer":2,"owner":"LEARN","deps":["wa.learn.cu_memory","wa.learn.cu_tasks","wa.learn.dream_journal","wa.learn.model_client","wa.perceive.osc2","wa.perceive.srf_identity","wa.perceive.disambig","wa.perceive.vision_bridge"],"book":"HB-WA-s3-remember"}
"""C1/C3/C4 computer-use runner; external vision is accessed through C2.

Uses a local monotonic budget: BudgetGovernor needs registered target handles,
reservations and settlement, which C1 does not expose. Crops are RAM-only.
Fake fixtures should live in RAM; fixture RGB24 digests are decoded locally.
The CLI uses ModelClient and the C2 subprocess bridge. Crops stay in RAM.
"""
from __future__ import annotations

import argparse
import inspect
import hashlib
import struct
import zlib
import json
import re
import math
import os
import shutil
import threading
import uuid
from collections import deque, OrderedDict
from dataclasses import asdict
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Protocol

from wender.learn.model_client import (ModelClient, MuseBackend, ReplayBackend, BackendError,
                                       ActionValidationError, ModelTimeoutError, evidence_value, bounded_output,
                                       validate_action)
from wender.learn.cu_memory import (BlockedTargetFilter, NoProgressTracker, Receipt, WorkingMemoryRing,
                                    canonical_action_sig, canonical_snapshot_bytes, classify_step_outcome,
                                    fnv1a_64_hex, verify_action_postcondition)
from wender.learn.dream_journal import JournalWriter
from wender.learn.cu_tasks import observation_status
from wender.perceive.osc2 import join_osc2
from wender.perceive.vision_bridge import SubprocessVisionEngine, VisionBridgeError


# @anchor:wa.learn.cu_loop.Capsule
class Capsule(Protocol):
    def frame(self, *, state: bool = True) -> dict: ...
    def input(self, action: dict) -> dict: ...


# @anchor:wa.learn.cu_loop.Describer
class Describer(Protocol):
    def describe(self, frame: dict, *, prev: dict | None = None) -> dict: ...


# @anchor:wa.learn.cu_loop.Decider
class Decider(Protocol):
    backend: str

    def decide(self, *, goal: str, frame: dict, description: dict,
               feedback: dict | None = None) -> dict: ...


def _json_command(command, *, payload=None, timeout=30):
    result = subprocess.run(command, input=payload, text=True, capture_output=True,
                            check=True, timeout=timeout)
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("subprocess must return one JSON object")
    return value


# @anchor:wa.learn.cu_loop.SrfCapsule
class SrfCapsule:
    """C1 transport. State support is explicit; errors never trigger a retry."""

    # @anchor:wa.learn.cu_loop.SrfCapsule.__init__
    def __init__(self, container: str, binary: str = "srf-holdings-capsule", *,
                 state_available: bool = True, timeout: float = 30, dispatcher=None):
        self.container, self.binary = container, binary
        self.state_available, self.timeout = state_available, timeout
        self.dispatcher = dispatcher

    def frame(self, *, state=True):
        args = [self.binary, "frame", "--container", self.container]
        if state and self.state_available:
            args.append("--state")
        frame = _validate_frame(_json_command(args, timeout=self.timeout))
        if state and self.state_available and "lane" not in frame:
            raise ValueError("requested state evidence missing")
        return frame

    # @anchor:wa.learn.cu_loop.SrfCapsule.input
    def input(self, action):
        args = [self.binary, "input", "--container", self.container]
        if action["kind"] == "click":
            args += ["--click", f'{action["x"]},{action["y"]}', "--button", "1"]
        elif action["kind"] == "key":
            _live_action(action)
            args += ["--key", action["keysym"]]
        else:
            # C1 has no text input primitive. Do not silently reinterpret text
            # as keysyms (keyboard layouts and Unicode make this lossy).
            raise ValueError("C1 supports only click/key input; type is unavailable")
        if self.dispatcher is None:
            raise ValueError('DISPATCH_OWNER_UNAVAILABLE')
        # The trusted constructor port owns final admission and uncertainty;
        # its callback is the sole crossing into the physical transport.
        return self.dispatcher.dispatch(self, action,
            lambda: _receipt(_json_command(args, timeout=self.timeout)))


def _decode_png(path):
    """Decode non-interlaced 8-bit RGB/RGBA fixture PNGs using the stdlib."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid fixture PNG signature")
    offset, compressed = 8, bytearray()
    width = height = channels = None
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        chunk = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
                raise ValueError("fixture PNG requires non-interlaced RGB/RGBA8")
            channels = 3 if color == 2 else 4
        elif kind == b"IDAT":
            compressed.extend(chunk)
        elif kind == b"IEND":
            break
        offset += length + 12
    if not width or not height or channels is None:
        raise ValueError("fixture PNG is missing IHDR")
    raw = zlib.decompress(compressed)
    stride = width * channels
    if len(raw) != height * (stride + 1):
        raise ValueError("invalid fixture PNG scanline length")
    previous, rgb = bytearray(stride), bytearray()
    for y in range(height):
        start = y * (stride + 1)
        mode = raw[start]
        row = bytearray(raw[start + 1:start + 1 + stride])
        if mode > 4:
            raise ValueError("invalid PNG filter")
        for i in range(stride):
            left = row[i - channels] if i >= channels else 0
            above = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            p = left + above - corner
            distances = (abs(p - left), abs(p - above), abs(p - corner))
            paeth = (left, above, corner)[distances.index(min(distances))]
            predictor = (0, left, above, (left + above) // 2, paeth)[mode]
            row[i] = (row[i] + predictor) & 255
        for i in range(0, stride, channels):
            rgb.extend(row[i:i + 3])
        previous = row
    return bytes(rgb), width, height


def _fixture_rgb(path):
    rgb, width, height = _decode_png(path)
    return hashlib.sha256(rgb).hexdigest(), width, height


# @anchor:wa.learn.cu_loop.FakeCapsule
class FakeCapsule:
    """Advance one indexed frame after each input; reads alone are idempotent.

    index.json uses the C1 fixture entries. RGB/RGBA8 PNGs are decoded to
    derive genuine RGB24 digests; no frames are copied or persisted.
    """

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.index = json.loads((self.directory / "index.json").read_text())
        if not isinstance(self.index, list) or not self.index:
            raise ValueError("fixture index must be a nonempty array")
        self._images = {}
        self.inputs = []
        self._position = 0
        for row in self.index:
            path = (self.directory / row["png"]).resolve()
            if not path.is_relative_to(self.directory) or not path.is_file():
                raise ValueError("fixture PNG must exist inside fixture directory")
            self._images[row["png"]] = _fixture_rgb(path)

    def frame(self, *, state=True):
        row = self.index[self._position]
        digest, width, height = self._images[row["png"]]
        frame = {"path": str(self.directory / row["png"]), "seq": row["seq"],
                 "sha256_rgb24": digest,
                 "generation": row.get("generation", 0),
                 "width": width, "height": height,
                 "capture_mono_ns": time.monotonic_ns()}
        if state and row.get("state_sha256") is not None:
            frame["lane"] = {"state_sha256": row["state_sha256"],
                             "tick": row.get("tick"), "revision": row.get("revision", row.get("tick")),
                             "elements": row.get("elements", [])}
        return frame

    def input(self, action):
        self.inputs.append(dict(action))
        self._position = min(self._position + 1, len(self.index) - 1)
        return {"input_mono_ns": time.monotonic_ns()}


# @anchor:wa.learn.cu_loop.BridgeDescriber
class BridgeDescriber:
    """Loop transport for the real C2 subprocess bridge."""

    def __init__(self, ve_root=None, *, masks=(), timeout=30, marks=True):
        self.engine = SubprocessVisionEngine(ve_root, timeout=timeout)
        self._configure_marks(masks, marks)

    def _configure_marks(self, masks, marks):
        self.masks = masks
        self.marks = marks
        self._marks_dir = None

    def __del__(self):
        directory = getattr(self, "_marks_dir", None)
        if directory is not None:
            directory.cleanup()

    # @anchor:wa.learn.cu_loop.BridgeDescriber.describe
    def describe(self, frame, *, prev=None):
        return self._describe_with_engine(self.engine, frame, prev)

    def _describe_with_engine(self, engine, frame, prev):
        options = {}
        if self.marks:
            if self._marks_dir is None:
                root = Path("/dev/shm/vqa-cu")
                root.mkdir(parents=True, exist_ok=True)
                self._marks_dir = tempfile.TemporaryDirectory(dir=root, prefix="marks-")
            options["marks_out"] = Path(self._marks_dir.name) / "marks.png"
        return asdict(engine.describe(frame["path"],
                      prev=None if prev is None else prev["path"], masks=self.masks, **options))


# @anchor:wa.learn.cu_loop.WarmBridgeDescriber
class WarmBridgeDescriber(BridgeDescriber):
    """Single-use describe service for a caller-authenticated seat and capsule.

    Pass the authenticated seat, trust domain and exact capsule to run_loop.
    Only the admitted loop may submit frames. The host calls fence on takeover
    or any authority, incarnation, stream or privacy change, then creates a new
    scope for a fresh grant. The worker starts lazily on the first describe.
    """

    _binding_lock = threading.Lock()
    _active_capsules = {}

    # @anchor:wa.learn.cu_loop.WarmBridgeDescriber.__init__
    def __init__(self, capsule: Capsule, *, seat_id: str, trust_domain: str,
                 ve_root=None, masks=(), timeout=30, marks=True):
        if not all(isinstance(value, str) and value.strip() for value in (seat_id, trust_domain)):
            raise ValueError("warm describe requires a seat and trust domain")
        self._binding = (capsule, seat_id, trust_domain)
        self._owner_pid = os.getpid()
        self._exchange_lock = threading.Lock()
        self._started = threading.Event()
        self._fenced = threading.Event()
        self._engine = SubprocessVisionEngine(ve_root, timeout=timeout, persistent=True,
                                              fallback_to_c2=False)
        self._configure_marks(masks, marks)

    def _check_owner(self):
        if os.getpid() != self._owner_pid:
            raise VisionBridgeError("warm describe scope belongs to another process")

    def _begin_episode(self, capsule, seat_id, trust_domain):
        self._check_owner()
        with self._binding_lock:
            bound_capsule, bound_seat, bound_domain = self._binding
            if capsule is not bound_capsule:
                raise VisionBridgeError("warm describe scope belongs to another capsule")
            if not isinstance(seat_id, str) or seat_id != bound_seat:
                raise VisionBridgeError("warm describe scope requires its authenticated seat")
            if not isinstance(trust_domain, str) or trust_domain != bound_domain:
                raise VisionBridgeError("warm describe scope requires its authenticated trust domain")
            if self._started.is_set() or self._fenced.is_set():
                raise VisionBridgeError("warm describe scope has already been used or fenced")
            if id(capsule) in self._active_capsules:
                raise VisionBridgeError("capsule already has an active warm describe scope")
            self._active_capsules[id(capsule)] = self
            self._started.set()

    def __enter__(self):
        self._check_owner()
        if self._fenced.is_set():
            raise VisionBridgeError("warm describe scope is fenced")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.fence()

    # @anchor:wa.learn.cu_loop.WarmBridgeDescriber.describe
    def describe(self, frame, *, prev=None):
        raise VisionBridgeError("warm describe requires frames captured by the bound run_loop")

    def _check_active(self):
        if self._fenced.is_set() or not self._started.is_set():
            raise VisionBridgeError("warm describe scope is inactive or fenced")

    def _describe_bound(self, frame, *, prev=None):
        self._check_owner()
        with self._exchange_lock:
            self._check_active()
            result = self._describe_with_engine(self._engine, frame, prev)
            self._check_active()
            if (result["frame_sha256"], result["width"], result["height"]) != (
                    frame["sha256_rgb24"], frame["width"], frame["height"]):
                raise VisionBridgeError("warm describe reply disagrees with captured frame")
            return result

    # @anchor:wa.learn.cu_loop.WarmBridgeDescriber.fence
    def fence(self):
        """Invalidate pending replies, interrupt the exchange, and reap."""
        self._check_owner()
        with self._binding_lock:
            self._fenced.set()
        # Interrupt pipe I/O before waiting on the exchange that owns the marks.
        self._engine._client.cancel()
        with self._exchange_lock:
            try:
                self._engine.close()
            finally:
                if self._marks_dir is not None:
                    self._marks_dir.cleanup()
                    self._marks_dir = None
            with self._binding_lock:
                key = id(self._binding[0])
                if self._active_capsules.get(key) is self:
                    del self._active_capsules[key]


def _encode_png(rgb, width, height):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))
    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3]
                   for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# @anchor:wa.learn.cu_loop._blocked_targets
def _blocked_targets(feedback, extra_blocked=None):
    rows = list(extra_blocked or [])[:4]
    try:
        action = feedback.get('rejected_action')
        action = json.loads(action) if isinstance(action, str) else action
        if isinstance(action, dict):
            target_action = {'kind': 'click'}
            if action.get('mark_id') is not None:
                target_action['mark_id'] = action['mark_id']
            elif action.get('element_id') is not None:
                target_action['element_id'] = action['element_id']
            elif action.get('x') is not None and action.get('y') is not None:
                target_action.update(x=action['x'], y=action['y'])
            target = canonical_action_sig(target_action).split(':', 1)[1].rsplit(':', 1)[0]
            if target != 'none':
                rows.insert(0, dict(target=target, reason=str(feedback.get('error_class') or 'validation_error')[:64]))
    except (ValueError, TypeError, OverflowError):
        pass
    result = []
    for row in evidence_value(rows):
        if not isinstance(row, dict):
            continue
        safe = {k: str(row.get(k, ''))[:96] for k in ('target', 'reason')}
        if safe not in result and len(json.dumps(result + [safe])) <= 400:
            result.append(safe)
        if len(result) == 4:
            break
    return result


# @anchor:wa.learn.cu_loop._compact_state_delta
def _compact_state_delta(frame, description, trace_steps):
    regions = description.get('changed_regions') or []
    overlaps = []
    for elem in frame.get('lane', {}).get('elements', []):
        try:
            if any(_overlaps(elem.get('bbox'), reg) for reg in regions) and elem.get('id'):
                # Structural identifiers only; never names or scraped text.
                ident = canonical_action_sig({'kind': 'click', 'element_id': elem['id']}).split(':', 2)[2].rsplit(':', 1)[0]
                overlaps.append(ident)
        except (ValueError, TypeError, KeyError):
            continue
        if len(overlaps) >= 8:
            break
    old = trace_steps[-1].get('status', {}) if trace_steps and isinstance(trace_steps[-1], dict) else {}
    old = old if isinstance(old, dict) else {}
    new = observation_status(frame)
    diff = {k: [old.get(k), v] for k, v in new.items()
            if k not in ('status_text', 'focus', 'route') and old.get(k) != v}
    row = evidence_value(dict(changed=description.get('changed') if type(description.get('changed')) is bool else None, regions_n=len(regions),
                              overlaps=overlaps, status_diff=dict(list(diff.items())[:8])))
    while len(json.dumps(row)) > 600 and (row['status_diff'] or row['overlaps']):
        if row['status_diff']:
            row['status_diff'].pop(next(reversed(row['status_diff'])))
        else:
            row['overlaps'].pop()
    return row


# @anchor:wa.learn.cu_loop._enrich_retry_feedback
def _enrich_retry_feedback(feedback, frame, description, trace_steps, extra_blocked=None):
    row = evidence_value(dict(feedback))
    row['validator_message'] = str(row.get('validator_message', ''))[:300]
    row['blocked_targets'] = _blocked_targets(feedback, extra_blocked)
    row['state_delta'] = _compact_state_delta(frame, description, trace_steps)
    # A rejected type action may contain the user's input; retain its length only.
    try:
        rejected = json.loads(row['rejected_action']) if isinstance(row.get('rejected_action'), str) else row.get('rejected_action')
        if isinstance(rejected, dict) and 'text' in rejected:
            rejected = {k: v for k, v in rejected.items() if k != 'text'} | {'value_len': len(str(rejected['text']))}
            row['rejected_action'] = json.dumps(rejected)
    except (ValueError, TypeError):
        pass
    for key in ('rejected_action', 'raw_output'):
        if len(json.dumps(row)) > 2000 and key in row:
            row[key] = '[bounded]'
    for key in list(row):
        if len(json.dumps(row)) <= 2000:
            break
        if key not in ('validator_message', 'blocked_targets', 'state_delta'):
            del row[key]
    return row


# @anchor:wa.learn.cu_loop.ModelDecider
class ModelDecider:
    """ModelClient adapter with run-owned RAM crops and bounded trace context."""

    def __init__(self, backend, *, keep_crops=False, images="marks"):
        self.client = ModelClient(backend)
        self.backend = "muse" if isinstance(backend, MuseBackend) else "replay"
        self.keep_crops = keep_crops
        if images not in ("marks", "full"):
            raise ValueError("invalid image policy")
        self.images = images
        self._muse_workspace = None
        self.crops_dir = Path("/dev/shm/vqa-cu") / uuid.uuid4().hex / "crops"
        self.crops_dir.mkdir(parents=True)
        self.trace_steps = []
        self.memory = WorkingMemoryRing()
        self.blocked_targets_history = []
        self._decision = 0
        self.capabilities = None

    # @anchor:wa.learn.cu_loop.ModelDecider.start_episode
    def start_episode(self):
        self.trace_steps = []
        self.memory = WorkingMemoryRing()
        self.blocked_targets_history = []
        backend = self.client.backend
        if isinstance(backend, MuseBackend):
            self.crops_dir.mkdir(parents=True, exist_ok=True)
            self._muse_workspace = self.crops_dir.parent / "musews"
            self._muse_workspace.mkdir()
            backend.workspace = str(self._muse_workspace)
            backend.session_id = str(uuid.uuid4())

    def set_effort(self, frame, description, previous_ok, need_reasoning, *,
                   first_step=False, feedback_retry=False):
        backend = self.client.backend
        if isinstance(backend, MuseBackend):
            grounded = bool(description.get("marks") or frame.get("lane", {}).get("elements"))
            if backend.effort_policy == "auto":
                if feedback_retry:
                    reason = "feedback_retry"
                elif first_step:
                    reason = "first_step"
                elif need_reasoning is True:
                    reason = "need_reasoning"
                elif not previous_ok:
                    reason = "previous_not_ok"
                elif not grounded:
                    reason = "ungrounded"
                else:
                    reason = "auto_low"
                backend.effort = "low" if reason == "auto_low" else "medium"
            else:
                reason = "fixed_policy"
                backend.effort = backend.effort_policy
                if (need_reasoning is True or feedback_retry) and backend.effort == "low":
                    backend.effort = "medium"
                    reason = "feedback_retry" if feedback_retry else "need_reasoning"
            self.effort_context = {"effort_reason": reason,
                                   "effort_need_reasoning": need_reasoning}

    # @anchor:wa.learn.cu_loop.ModelDecider.decide
    def decide(self, *, goal, frame, description, feedback=None):
        self.client.last_raw_output = "null"
        if feedback is not None:
            return self._proposal(self.client.act(goal, frame["path"], description,
                                                  **self._last_options, feedback=_enrich_retry_feedback(
                                                      feedback, frame, description, self.trace_steps, self.blocked_targets_history)))
        paths = []
        rects = ([] if description.get("marks_png") and self.images != "full"
                 else description.get("crops", [])[:4])
        if rects:
            rgb, width, height = _decode_png(Path(frame["path"]))
            for i, rect in enumerate(rects):
                x, y, w, h = _rect(rect)
                if x + w > width or y + h > height:
                    raise ValueError("crop outside frame")
                pixels = b"".join(rgb[((y + j) * width + x) * 3:((y + j) * width + x + w) * 3]
                                  for j in range(h))
                path = self.crops_dir / f"{self._decision}-{i}.png"
                path.write_bytes(_encode_png(pixels, w, h))
                paths.append(str(path))
        self._decision += 1
        options = {} if self.capabilities is None else {"capabilities": self.capabilities}
        self._last_options = dict(crop_paths=paths,
                                  elements=frame.get("lane", {}).get("elements", []),
                                  trace_steps=self.memory.for_muse() if self.backend == "muse" else self.memory.last(3), images=self.images,
                                  lane_status=observation_status(frame), **options)
        return self._proposal(self.client.act(goal, frame["path"], description, **self._last_options))

    def _proposal(self, result):
        # The loop independently resolves the original single grounding choice.
        if "mark_id" in result["action"] or "element_id" in result["action"]:
            result["action"].pop("x", None)
            result["action"].pop("y", None)
        return result

    # @anchor:wa.learn.cu_loop.ModelDecider.close
    def close(self):
        if self._muse_workspace is not None:
            shutil.rmtree(self._muse_workspace, ignore_errors=True)
            self._muse_workspace = None
        if not self.keep_crops and self.crops_dir.exists():
            shutil.rmtree(self.crops_dir)
            self.crops_dir.parent.rmdir()


# @anchor:wa.learn.cu_loop.CommandDescriber
class CommandDescriber:
    def __init__(self, command=None, *, timeout=30):
        self.command = list(command or [sys.executable, "-m", "vadapt.cu_describe"])
        self.timeout = timeout

    def describe(self, frame, *, prev=None):
        args = self.command + ["--frame", frame["path"]]
        if prev is not None:
            args += ["--prev", prev["path"]]
        return _json_command(args + ["--json"], timeout=self.timeout)


# @anchor:wa.learn.cu_loop.ReplayDecider
class ReplayDecider:
    backend = "replay"

    def __init__(self, proposals):
        self._proposals = iter(proposals)

    def decide(self, *, goal, frame, description, feedback=None):
        try:
            return next(self._proposals)
        except StopIteration as exc:
            raise ValueError("replay exhausted before done") from exc


# @anchor:wa.learn.cu_loop.CommandDecider
class CommandDecider:
    backend = "muse"

    def __init__(self, command, *, timeout=30):
        if not command:
            raise ValueError("muse requires --muse-command; transport API AUSENTE")
        self.command, self.timeout = list(command), timeout

    def decide(self, *, goal, frame, description, feedback=None):
        payload = {"goal": goal, "frame": frame, "description": description}
        if feedback is not None:
            payload["feedback"] = feedback
        return _json_command(self.command, payload=json.dumps(payload), timeout=self.timeout)


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _rect(region):
    if not isinstance(region, dict):
        raise ValueError("region must be an object")
    return tuple(_integer(region[k], k, 1 if k in ("w", "h") else 0)
                 for k in ("x", "y", "w", "h"))


def _overlaps(a, b):
    x, y, w, h = _rect(a)
    u, v, p, q = _rect(b)
    return x < u + p and u < x + w and y < v + q and v < y + h


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid SHA256 digest")


def _validate_frame(frame):
    if not isinstance(frame, dict):
        raise ValueError("frame must be an object")
    frame = dict(frame)
    if "state" in frame:
        if "lane" in frame and frame["lane"] != frame["state"]:
            raise ValueError("contradictory state/lane")
        frame["lane"] = frame.pop("state")
    for key in ("width", "height"):
        _integer(frame[key], key, 1)
    for key in ("seq", "capture_mono_ns"):
        _integer(frame[key], key)
    generation = frame["generation"]
    if not ((type(generation) is int and generation >= 0) or
            (isinstance(generation, str) and generation)):
        raise ValueError("invalid generation")
    _digest(frame["sha256_rgb24"])
    path = Path(frame["path"]).resolve(strict=True)
    if not path.is_relative_to("/dev/shm"):
        raise ValueError("frame must live in RAM")
    digest, width, height = _fixture_rgb(path)
    if (digest, width, height) != (frame["sha256_rgb24"], frame["width"], frame["height"]):
        raise ValueError("frame pixels disagree with metadata")
    if "lane" in frame:
        lane = frame["lane"]
        if not isinstance(lane, dict):
            raise ValueError("state must be an object")
        _digest(lane.get("state_sha256"))
        for key in ("tick", "revision"):
            _integer(lane.get(key), key)
        if not isinstance(lane.get("elements"), list):
            raise ValueError("elements must be an array")
        lane = dict(lane)
        frame["lane"] = lane
        normalized = []
        for element in lane["elements"]:
            if not isinstance(element, dict):
                raise ValueError("invalid element")
            element = dict(element)
            for source, target in (("stable_id", "id"), ("anchor", "name")):
                if source in element:
                    if not isinstance(element[source], str):
                        raise ValueError("invalid element text")
                    if target in element and element[target] != element[source]:
                        raise ValueError("contradictory element " + target)
                    element[target] = element[source]
            element.setdefault("bbox", None)
            if not isinstance(element.get("id"), str):
                raise ValueError("invalid element")
            for key in ("role", "name"):
                if key in element and not isinstance(element[key], str):
                    raise ValueError("invalid element text")
            normalized.append(element)
            # Offscreen and bbox-less elements are valid observations. C3
            # validates the targeted element's box when grounding a click.
        lane["elements"] = normalized
    return frame


def _safe_evidence(value):
    """Keep diagnostics JSON-safe even when the rejected proposal was not."""
    if isinstance(value, dict):
        return {str(k): _safe_evidence(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_evidence(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"invalid_type": type(value).__name__}


def _receipt(value):
    if not isinstance(value, dict) or set(value) != {"input_mono_ns"}:
        raise ValueError("invalid input receipt")
    _integer(value["input_mono_ns"], "input_mono_ns")
    return value


def _live_action(action):
    if action["kind"] == "type":
        raise ValueError("C1 type unavailable")
    if action["kind"] == "key" and re.fullmatch(r"[A-Za-z0-9_+]{1,32}", action["keysym"]) is None:
        raise ValueError("invalid C1 keysym")


def _action(proposal, frame, marks=None):
    validated = validate_action(proposal, frame["width"], frame["height"],
                                frame.get("lane", {}).get("elements", []), marks)
    if validated["expectation"]["kind"] == "changed_region":
        _rect(validated["expectation"].get("region"))
    action = validated["action"]
    if action["kind"] in ("key", "type") and not action.get("keysym" if action["kind"] == "key" else "text"):
        raise ValueError("empty input value")
    return action


def _changed(before, after):
    return before["sha256_rgb24"] != after["sha256_rgb24"] or (
        before.get("lane", {}).get("state_sha256") is not None and
        after.get("lane", {}).get("state_sha256") is not None and
        before["lane"]["state_sha256"] != after["lane"]["state_sha256"])


# @anchor:wa.learn.cu_loop._trace_evidence
def _trace_evidence(value):
    # Usage counters are quantities, even though their names contain 'token'.
    if isinstance(value, dict):
        return {str(k): (v if k in ('input_tokens', 'cached_tokens', 'reasoning_tokens', 'output_tokens')
                        and type(v) is int and v >= 0 else
                        '[REDACTED]' if evidence_value({str(k): None})[str(k)] == '[REDACTED]' else
                        _trace_evidence(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trace_evidence(v) for v in value]
    return evidence_value(value)


# @anchor:wa.learn.cu_loop._state_hash
def _state_hash(frame):
    status = observation_status(frame)
    elements = frame.get('lane', {}).get('elements')
    # Digest-only legacy adapters lack element-level semantic state.
    if not elements:
        status = dict(status, state_sha256=frame.get('lane', {}).get('state_sha256'))
    return fnv1a_64_hex(canonical_snapshot_bytes(status, elements))


# @anchor:wa.learn.cu_loop._verify
def _verify(before, after, description, expectation, *, action=None):
    post = verify_action_postcondition(action or {}, before, after, description, expectation=expectation)
    if post is not None:
        return post
    kind = expectation["kind"]
    old = before.get("lane", {}).get("state_sha256")
    new = after.get("lane", {}).get("state_sha256")
    if kind == "state_change" and (old is None or new is None):
        raise ValueError("requested state evidence missing")
    if kind == "state_change":
        return {"method": "state_sha256", "ok": old != new,
                "detail": "lane state digest comparison"}
    if expectation.get("region") is not None:
        ok = any(_overlaps(expectation["region"], r)
                 for r in description["changed_regions"])
        return {"method": "changed_region", "ok": ok,
                "detail": "expected region overlap"}
    # A none expectation still requires observed change; it is not evidence.
    return {"method": "none", "ok": before["sha256_rgb24"] != after["sha256_rgb24"],
            "detail": "whole-frame RGB digest comparison"}


# @anchor:wa.learn.cu_loop.run_loop
def run_loop(capsule: Capsule, describer: Describer, decider: Decider, *,
             goal: str, trace, max_steps=20, max_ms=30000, after_timeout_ms=1500,
             poll_ms=30, clock=time.monotonic, sleep=time.sleep, pixel_only=False,
             goal_check=None, goal_distance=None, no_progress_steps=2,
             journal_path=None, escalate_decider=None, integration=None,
             seat_id: str | None = None, trust_domain: str | None = None):
    """Append one C4 line per decision, including done (not an executed step).

    steps counts executed actions. A correction consumes an additional budget
    slot. Done is recorded but neither verified nor charged as an action. Providers must bound their own blocking operations;
    deadlines are checked again before any input is sent. Repeated meaningful
    snapshots after verified actions detect cycles/no-ops; predicate distance
    is telemetry only. Repeated refusal codes are tracked per action. Warm
    describe requires the caller's authenticated seat and trust domain.
    """
    for value, name in ((max_steps, "max_steps"), (max_ms, "max_ms"),
                        (after_timeout_ms, "after_timeout_ms")):
        _integer(value, name)
    _integer(poll_ms, "poll_ms", 1)
    _integer(no_progress_steps, "no_progress_steps", 1)
    start = clock()
    deadline = start + max_ms / 1000
    warm = describer if isinstance(describer, WarmBridgeDescriber) else None

    stage = "budget"
    proposal = action = receipt = last_frame = last_observation = None
    input_started = False
    failure = None
    attempts = []
    decision_evidence = None
    corrections = 0
    budget_used = 0
    goal_verified = False
    stagnant = 0
    best_distance = None
    seen_snapshots = deque(maxlen=12)
    refusals = OrderedDict()
    tracker = NoProgressTracker(no_progress_steps=no_progress_steps)
    escalations, loop_score = [], 0
    journal = JournalWriter(journal_path) if journal_path is not None and warm is None else None
    tier3_used = False
    memory_ring = WorkingMemoryRing()
    osc2_transforms = {}

    def snapshot(frame):
        status = observation_status(frame)
        values = {k: v for k, v in status.items()
                  if k not in ("last_refusal_code", "status_text") and v is not None}
        return json.dumps(values, sort_keys=True) if values else None
    def call(provider, method, *args, **kwargs):
        if warm is not None:
            warm._check_active()
        if clock() >= deadline:
            raise TimeoutError("episode budget exhausted")
        if isinstance(provider, WarmBridgeDescriber):
            target = provider._engine
        elif isinstance(provider, (BridgeDescriber, ModelDecider)):
            target = provider.engine if isinstance(provider, BridgeDescriber) else provider.client.backend
        else:
            target = provider
        if hasattr(target, "timeout"):
            provider_timeout = target
            old_timeout = provider_timeout.timeout
            provider_timeout.timeout = min(old_timeout, max(.001, deadline - clock()))
            try:
                return getattr(provider, method)(*args, **kwargs)
            finally:
                provider_timeout.timeout = old_timeout
        return getattr(provider, method)(*args, **kwargs)
    steps = verified = failures = consecutive = 0
    previous = None
    reason = "max_steps"
    previous_ok = False
    need_reasoning = False
    describe_method = "describe"
    if warm is not None:
        warm._begin_episode(capsule, seat_id, trust_domain)
        describe_method = "_describe_bound"
    try:
        if warm is not None and journal_path is not None:
            journal = JournalWriter(journal_path)
        if isinstance(decider, ModelDecider) and isinstance(capsule, SrfCapsule):
            decider.capabilities = ["click", "key", "wait", "done"]
        if isinstance(decider, ModelDecider):
            decider.start_episode()
        while budget_used < max_steps:
            if clock() >= deadline:
                reason = "max_ms"
                break
            proposal = action = receipt = None
            decision_evidence = None
            corrections = 0
            correction_classes = []
            input_started = False
            stage = "capture"
            before = call(capsule, "frame", state=not pixel_only)
            if not isinstance(capsule, SrfCapsule):
                before = _validate_frame(before)
            before_describe = dict(before) if warm is not None else before
            last_frame = before
            if not pixel_only and "lane" not in before:
                raise ValueError("requested state evidence missing")
            initial_snapshot = snapshot(before)
            if initial_snapshot is not None:
                seen_snapshots.append(initial_snapshot)
            stage = "predicate_before"
            predicate_before = None if goal_check is None else goal_check(before)
            goal_verified = predicate_before is True
            distance_before = None if goal_distance is None else goal_distance(before)
            if best_distance is None and distance_before is not None:
                best_distance = distance_before
            stage = "describe"
            desc = call(describer, describe_method, before_describe, prev=previous)
            if integration is not None:
                stage = 'script'
                def note_script_input(possible):
                    nonlocal input_started
                    input_started = input_started or possible
                if _script_before_model(integration, before, desc, clock=clock,
                                        deadline=deadline, note_input=note_script_input):
                    reason = 'script_completed'
                    goal_verified = True
                    break
            join_osc2(before, desc, osc2_transforms)
            if clock() >= deadline:
                raise TimeoutError("episode budget exhausted")
            model_start = clock()
            feedback = None
            model_calls = []
            while True:
                stage = "decision"
                attempt_start = clock()
                proposal = None
                try:
                    kwargs = {"goal": goal, "frame": before, "description": desc}
                    if feedback is not None:
                        kwargs["feedback"] = feedback
                    if isinstance(decider, ModelDecider):
                        decider.set_effort(before, desc, previous_ok,
                                           need_reasoning, first_step=steps == 0,
                                           feedback_retry=feedback is not None)
                    try:
                        if integration is not None and integration.charge_model() is not True:
                            raise ValueError('MODEL_BUDGET')
                        proposal = call(decider, "decide", **kwargs)
                    finally:
                        if isinstance(decider, ModelDecider):
                            if isinstance(decider.client.backend, MuseBackend):
                                decider.client.last_model.update(decider.effort_context)
                                if isinstance(proposal, dict):
                                    decider.client.last_model["need_reasoning"] = proposal.get("need_reasoning")
                            model_calls.append(dict(decider.client.last_model))
                    if isinstance(proposal, dict) and isinstance(proposal.get("observation"), str):
                        last_observation = proposal["observation"]
                    if warm is not None:
                        warm._check_active()
                    if clock() >= deadline:
                        raise TimeoutError("episode budget exhausted")
                    stage = "validation"
                    action = _action(proposal, before, desc.get("marks", []))
                    if isinstance(capsule, SrfCapsule):
                        _live_action(action)
                    if proposal["expectation"]["kind"] == "state_change" and "lane" not in before and not pixel_only:
                        raise ValueError("requested state evidence missing")
                    decision_evidence = None
                    break
                except Exception as exc:
                    decision_evidence = {
                        "raw_output": getattr(exc, "raw_output",
                            decider.client.last_raw_output if isinstance(decider, ModelDecider)
                            else bounded_output(proposal)),
                        "validator_message": str(evidence_value(str(exc)))[:300],
                        "latency_ms": max(0, (clock() - attempt_start) * 1000)}
                    attempts.append({"stage": stage, "error_class": type(exc).__name__,
                                     "corrections": corrections, **decision_evidence})
                    retryable = stage == "decision" and (
                        isinstance(exc, ModelTimeoutError) or
                        isinstance(exc, BackendError) and "did not reach a terminal state" in str(exc))
                    validation = (isinstance(exc, ActionValidationError) or stage == "validation")
                    if (not (retryable or validation) or corrections
                            or (not retryable and decision_evidence["raw_output"] == "null")
                            or budget_used + 1 >= max_steps or clock() >= deadline):
                        raise
                    corrections = 1
                    correction_classes.append(type(exc).__name__)
                    attempts[-1]["correction_class"] = type(exc).__name__
                    budget_used += 1
                    # Reuse the exact observation/crops; no UI input or recapture.
                    feedback = {"validator_message": decision_evidence["validator_message"],
                                "error_class": type(exc).__name__,
                                "rejected_action": getattr(exc, "rejected_action", bounded_output(
                                    proposal.get("action") if isinstance(proposal, dict) else proposal))}
            need_reasoning = proposal.get("need_reasoning") is True
            latency = max(0, (clock() - model_start) * 1000)
            if clock() >= deadline:
                reason = "max_ms"
                break
            pre_hash = _state_hash(before)
            if 'lane' in before and action['kind'] != 'done' and predicate_before is not True:
                allowed, block_reason = tracker.can_execute(action, pre_hash)
                if not allowed:
                    escalations.append(dict(event='escalation', reason=block_reason, step=steps + 1,
                                            action_sig=canonical_action_sig(action), pre_state_hash=pre_hash))
                    loop_score = max(loop_score, 2)
                    reason = 'no_progress'
                    break
            receipt = None
            after = before
            if action["kind"] == "done":
                verification = {"method": "none", "ok": False, "detail": "done; no action verified"}
                reason = "done"
            else:
                if action["kind"] == "wait":
                    stage = "wait"
                    sleep(min(action["ms"] / 1000, max(0, deadline - clock())))
                    if clock() >= deadline:
                        raise TimeoutError("episode budget exhausted")
                else:
                    stage = "input"
                    if clock() >= deadline:
                        raise TimeoutError("episode budget exhausted")
                    input_started = True
                    if integration is not None:
                        integration.possible_input(action)
                        receipt = integration.dispatch(capsule, action,
                            lambda: call(capsule, "input", action))
                    else:
                        receipt = call(capsule, "input", action)
                    _receipt(receipt)
                steps += 1
                budget_used += 1
                until = min(deadline, clock() + after_timeout_ms / 1000)
                while clock() < deadline:
                    stage = "after_capture"
                    after = call(capsule, "frame", state=not pixel_only)
                    if not isinstance(capsule, SrfCapsule):
                        after = _validate_frame(after)
                    last_frame = after
                    if not pixel_only and "lane" not in after:
                        raise ValueError("requested state evidence missing")
                    if _changed(before, after) or clock() >= until:
                        break
                    sleep(min(poll_ms / 1000, max(0, until - clock())))
                if clock() >= deadline:
                    stage = "after_capture"
                    raise TimeoutError("episode budget exhausted")
                # Compare against the pre-action frame, never the preceding poll.
                stage = "after_describe"
                after_desc = call(describer, describe_method, after, prev=before_describe)
                stage = "verification"
                expectation = proposal["expectation"]
                if pixel_only and expectation["kind"] == "state_change":
                    expectation = {"kind": "none"}
                if integration is not None:
                    semantic = integration.verify(action, before, after)
                    verification = {'method': 'semantic', 'ok': semantic is True,
                                    'detail': 'host semantic result'}
                else:
                    verification = _verify(before, after, after_desc, expectation, action=action)
            stage = "predicate_after"
            predicate_after = None if goal_check is None else goal_check(after)
            goal_verified = predicate_after is True
            distance_after = None if goal_distance is None else goal_distance(after)
            progress = None
            if verification["ok"] and goal_distance is not None:
                progress = (distance_after is not None and best_distance is not None
                            and distance_after < best_distance)
                if distance_after is not None:
                    best_distance = distance_after if best_distance is None else min(best_distance, distance_after)
            repeated_refusal = False
            if action["kind"] != "done":
                current_snapshot = snapshot(after)
                if verification["ok"] and current_snapshot is not None:
                    stagnant = stagnant + 1 if current_snapshot in seen_snapshots else 0
                    seen_snapshots.append(current_snapshot)
                else:
                    stagnant = 0
                code = observation_status(after)["last_refusal_code"]
                if code is not None:
                    refusal_key = (canonical_action_sig(action), code)
                    refusals[refusal_key] = refusals.get(refusal_key, 0) + 1
                    repeated_refusal = refusals[refusal_key] >= 2
                    while len(refusals) > 8:
                        refusals.popitem(last=False)
            outcome_class = None
            post_hash = _state_hash(after)
            if action['kind'] != 'done':
                outcome_class = classify_step_outcome(changed=_changed(before, after), verify_ok=verification['ok'],
                    snapshot_revisited=stagnant > 0 or bool(after.get('lane', {}).get('elements')) and (post_hash == pre_hash or post_hash[:12] in tracker.visited_state_hashes),
                    predicate_before=predicate_before, predicate_after=predicate_after,
                    distance_before=distance_before, distance_after=distance_after,
                    refusal_code=observation_status(after)['last_refusal_code'])
                if outcome_class == 'already_done_noop':
                    consecutive = stagnant = 0
                elif verification['ok']:
                    verified += 1
                    consecutive = 0
                else:
                    failures += 1
                    consecutive += 1
                tracking = tracker.record_step(action, pre_hash, post_hash, outcome=outcome_class,
                    refusal_code=observation_status(after)['last_refusal_code'],
                    distance_before=distance_before, distance_after=distance_after)
                if after.get('lane', {}).get('elements'):
                    stagnant = tracker.stagnant_steps
                loop_score = tracking['loop_score']
                if tracking['should_escalate']:
                    escalations.append(dict(event='escalation', reason=tracking['escalate_reason'], step=steps, loop_score=loop_score))
            if verification["ok"] and goal_verified:
                reason = "goal_reached"
            row = {"schema": "cu.trace/v1", "step": steps if action["kind"] != "done" else steps + 1,
                   "frame": {k: before[k] for k in ("seq", "sha256_rgb24", "generation")},
                   "describe": {"changed": desc["changed"], "regions_n": len(desc["changed_regions"])},
                   "model": {"backend": decider.backend, "latency_ms": latency, "action": {k: v for k, v in action.items() if k != "text"} | ({"value_len": len(action["text"])} if "text" in action else {}), "drafted_ahead": getattr(proposal, "extra_objects", 0), "corrections": corrections,
                             "correction_classes": correction_classes},
                   "status_before": observation_status(before), "status": observation_status(after),
                   "progress": {"distance_before": distance_before, "distance_after": distance_after,
                                "made_progress": progress, "stagnant_steps": stagnant, "outcome_class": outcome_class, "loop_score": loop_score},
                   "grounding": {"raw_coordinates": action['kind'] == 'click' and
                                  'element_id' not in action and 'mark_id' not in action,
                                  "osc2_transform": before.get('osc2_transform')},
                   "input": receipt, "after": {k: after[k] for k in ("seq", "sha256_rgb24")},
                   "verify": verification,
                   "budget": {"steps_left": max(0, max_steps - budget_used),
                              "ms_left": max(0, int((deadline - clock()) * 1000))}}
            previous_ok = verification["ok"] is True
            if isinstance(decider, ModelDecider) and isinstance(decider.client.backend, MuseBackend):
                row["model"].update(decider.client.last_model)
                row["model"]["calls"] = model_calls
            row.update(predicate_before=predicate_before is True,
                       predicate_after=predicate_after is True,
                       predicate_before_unknown=predicate_before is None,
                       predicate_after_unknown=predicate_after is None)
            attempts.append({"proposal": proposal, "action": action, "expectation": proposal["expectation"],
                             "receipt": receipt, "verify": verification,
                             "corrections": corrections, "latency_ms": latency})
            row = _trace_evidence(row)
            for status_key in ('status_before', 'status'):
                row[status_key].pop('status_text', None)
            compact = None
            if action['kind'] != 'done':
                result_name = {'already_done_noop': 'noop_done', 'wrong_target_noop': 'noop_wrong',
                               'stagnant_verified': 'stagnant', 'sealed': 'sealed'}[outcome_class]
                compact = memory_ring.append(Receipt(steps, action['kind'], canonical_action_sig(action),
                    'refused' if observation_status(after)['last_refusal_code'] else result_name,
                    verification['method'], post_hash, ok=verification['ok'], pre_state_hash=pre_hash,
                    refusal_code=observation_status(after)['last_refusal_code'],
                    dist_delta=distance_before - distance_after if distance_before is not None and distance_after is not None else None))
                if isinstance(decider, ModelDecider):
                    if decider.memory is not memory_ring:
                        decider.memory.append(compact)
                    if compact.refusal_code:
                        decider.blocked_targets_history = (decider.blocked_targets_history + [
                            dict(target=compact.target_sig, reason=compact.refusal_code)])[-4:]
                if journal is not None:
                    journal.append(dict(pre_hash=pre_hash, post_hash=post_hash, action_sig=canonical_action_sig(action),
                                        exit_code=0 if verification['ok'] else 1, score_q16=65536 if verification['ok'] else 0,
                                        receipt=compact.to_dict()))
            trace.write(json.dumps(row, separators=(",", ":")) + "\n")
            trace.flush()
            if integration is not None and input_started and verification['ok'] is not True and outcome_class != 'already_done_noop':
                goal_verified = False
                integration.uncertain(action)
                reason = 'reconciliation_required'
                break
            if isinstance(decider, ModelDecider):
                decider.trace_steps = (decider.trace_steps + [row])[-3:]
            previous = before_describe
            if reason in ("done", "goal_reached"):
                break
            if stagnant >= no_progress_steps or repeated_refusal:
                if not escalations or escalations[-1]['step'] != steps:
                    escalations.append(dict(event='escalation', reason='repeated_refusal' if repeated_refusal else 'stagnant_revisit', step=steps, loop_score=loop_score))
                if escalate_decider is not None and not tier3_used:
                    tier3_used = True
                    if isinstance(decider, ModelDecider):
                        decider.close()
                    decider = escalate_decider
                    if isinstance(decider, ModelDecider):
                        decider.start_episode()
                        decider.memory = memory_ring
                else:
                    reason = 'no_progress'
                    break
            if consecutive >= 3:
                reason = "verify_failures"
                break
            if clock() >= deadline:
                reason = "max_ms"
                break
    except Exception as exc:
        if input_started and integration is not None:
            goal_verified = False
            integration.uncertain(action)
        current = exc
        timed_out = False
        while current is not None:
            timed_out |= isinstance(current, (subprocess.TimeoutExpired, TimeoutError))
            current = current.__cause__
        reason = "max_ms" if clock() >= deadline else "timeout" if timed_out else "failure"
        failure = {"stage": stage, "error_class": type(exc).__name__,
                   "proposal": proposal, "action": action, "receipt": receipt,
                   "input_effect_unknown": input_started,
                   "corrections": corrections,
                   **(decision_evidence or {}),
                   "expectation": proposal.get("expectation") if isinstance(proposal, dict) else None}
    finally:
        try:
            if warm is not None:
                warm.fence()
        finally:
            if isinstance(decider, ModelDecider):
                decider.close()
    if failure and decision_evidence is not None:
        failure["proposal"] = decision_evidence["raw_output"]
        proposal = decision_evidence["raw_output"]
    return _safe_evidence(evidence_value({"steps": steps, "verified_ok": verified, "failures": failures, "stop_reason": reason,
            "outcome": "model_claimed_done" if reason == "done" else "stopped",
            "goal_verified": goal_verified,
            "escalations": escalations, "escalate_reason": escalations[-1]["reason"] if escalations else None,
            "loop_score": loop_score,
            "goal_evidence": "verified" if goal_verified else "not_reached",
            "terminal_observation": last_observation,
            "final_frame": None if last_frame is None else {k: last_frame[k] for k in
                ("path", "seq", "sha256_rgb24", "width", "height", "generation", "capture_mono_ns")}, "failure": failure, "attempts": attempts,
            "terminal_attempt": {"stage": stage, "proposal": proposal, "action": action,
                                 "receipt": receipt, "input_effect_unknown": bool(failure and input_started),
                                 **(decision_evidence or {})}}))


# @anchor:wa.learn.cu_loop._script_before_model
def _script_before_model(owners, before, description, *, clock, deadline, note_input):
    """Trusted injected owner proves restart readiness and ordinary lookup misses.

    Bound advancement by both the episode deadline and the exact finite plan.
    The runner retains state even if finishing/draining raises after input.
    """
    if owners.check_ready() is not True:
        raise ValueError('RESTART_UNCERTAIN')
    session = owners.select_script(before, description)
    if session is None:
        if owners.check_script_miss() is not True:
            raise ValueError('SCRIPT_NOT_ORDINARY_MISS')
        return False
    state = None
    uncertain_input = False
    try:
        remaining = len(session.custody.plan.nodes) + 1
        while remaining:
            if clock() >= deadline:
                raise TimeoutError('episode budget exhausted')
            state = run_script_turn(session, owners=owners)
            disposition = getattr(state.input_disposition, 'value', None)
            uncertain_input = uncertain_input or disposition == 'input_uncertain'
            note_input(disposition != 'not_sent')
            if clock() >= deadline:
                raise TimeoutError('episode budget exhausted')
            if state.outcome is not None:
                accepted = owners.accept_script(session, state)
                outcome = state.outcome
                if (uncertain_input
                        or getattr(outcome.input_disposition, 'value', None) == 'input_uncertain'
                        or outcome.reconciliation_required is not False
                        or getattr(outcome.failure, 'value', None) in (
                            'EPISODE_DEADLINE', 'EPISODE_CANCELLATION', 'UNCLASSIFIED')
                        or getattr(outcome.owner_drained.value, 'value', None) != 'TRUE'
                        or (accepted is not True
                            and owners.check_terminal(session, outcome) is not True)
                        or owners.check_ready() is not True):
                    raise ValueError('SCRIPT_TERMINAL_UNAVAILABLE')
                return accepted is True
            remaining -= 1
        raise ValueError('SCRIPT_TERMINAL_UNAVAILABLE')
    except Exception:
        # Missing state after an entered runner is possible input, never NOT_SENT.
        try:
            retained = owners.runner_state(session)
        except Exception:
            note_input(True)
        else:
            note_input(retained is None or getattr(retained.input_disposition, 'value', None) != 'not_sent')
        raise


# @anchor:wa.learn.cu_loop.run_script_turn
def run_script_turn(session, *, owners):
    """Injected higher-layer coordinator owns the single runner state."""
    return owners.advance_session(session)



# @anchor:wa.learn.cu_loop.main
def main(argv=None, *, goal_check=None, goal_distance=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--capsule", required=True)
    parser.add_argument("--decider", required=True)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--no-progress-steps", type=int, default=2)
    parser.add_argument("--max-ms", type=int, default=30000)
    parser.add_argument("--after-timeout-ms", type=int, default=1500)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--capsule-binary", default="srf-holdings-capsule")
    parser.add_argument("--no-state", action="store_true")
    parser.add_argument("--ve-root")
    parser.add_argument("--mask", action="append", default=[])
    parser.add_argument("--effort", choices=("auto", "low", "medium", "xhigh"), default="auto")
    parser.add_argument("--images", choices=("marks", "full"), default="marks")
    parser.add_argument("--muse-timeout", type=float, default=240)
    parser.add_argument("--muse-workspace", default=None,
                        help="Deprecated compatibility option; the loop owns an empty RAM workspace")
    parser.add_argument("--keep-crops", action="store_true")
    parser.add_argument("--no-marks", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.capsule.startswith("srf:"):
            capsule = SrfCapsule(args.capsule[4:], args.capsule_binary,
                                 state_available=not args.no_state, timeout=max(.001, args.max_ms / 1000))
        elif args.capsule.startswith("fake:"):
            capsule = FakeCapsule(args.capsule[5:])
        else:
            raise ValueError("capsule must be srf:<container> or fake:<dir>")
        masks = [tuple(map(int, mask.split(","))) for mask in args.mask]
        describer = BridgeDescriber(args.ve_root, masks=masks,
                                    marks=not args.no_marks and isinstance(capsule, SrfCapsule))
        if args.decider.startswith("replay:"):
            backend = ReplayBackend(args.decider[7:])
        elif args.decider == "muse":
            backend = MuseBackend(args.muse_workspace, effort=args.effort, timeout=args.muse_timeout)
        else:
            raise ValueError("decider must be replay:<file> or muse")
        decider = ModelDecider(backend, keep_crops=args.keep_crops, images=args.images)
        try:
            with open(args.trace, "a", encoding="utf-8") as trace:
                summary = run_loop(capsule, describer, decider, goal=args.goal, trace=trace,
                                   max_steps=args.max_steps, max_ms=args.max_ms,
                                   after_timeout_ms=args.after_timeout_ms, pixel_only=args.no_state,
                                   goal_check=goal_check, goal_distance=goal_distance,
                                   no_progress_steps=args.no_progress_steps)
        finally:
            decider.close()
        print(json.dumps(summary))
        return 0 if summary["stop_reason"] in ("done", "goal_reached") else 2
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, BackendError, VisionBridgeError) as exc:
        print(json.dumps({"stop_reason": "failure", "goal_verified": False,
                          "failure": {"stage": "setup", "error_class": type(exc).__name__,
                                      "input_effect_unknown": False}}))
        return 2


from wender.perceive.srf_identity import SurfaceNotReady as _SurfaceNotReady, SurfaceRefused as _SurfaceRefused  # noqa: E402  (web cycle read retry)

# An empty contenteditable composer reports value_len 1 (trailing newline) on the capsule [MEASURED 2026-10-24].
_EMPTY_COMPOSER = (0, 1)


def _web_composer(ax):
    from wender.perceive.srf_identity import _eligible
    rows = [e for e in ax.get('elements', []) if e.get('role') == 'textbox'
            and e.get('region') == 'main' and isinstance(e.get('id'), str) and e['id']
            and e.get('visible', True) is True and _eligible(e)]   # producer omits 'visible' for visible nodes (same rule as _eligible)
    if not rows or ax.get('truncated') is not False:
        return None
    area = max(e['box'][2] * e['box'][3] for e in rows)
    largest = [e for e in rows if e['box'][2] * e['box'][3] == area]
    if len(largest) != 1:
        return None
    target = largest[0]
    return target if sum(e.get('id') == target['id'] for e in ax['elements']) == 1 else None


# @anchor:wa.learn.cu_loop._web_generating
def _web_generating(ax):
    from wender.perceive.srf_identity import _generating
    return _generating(ax)


def _web_complete(ax, ownership):
    from wender.perceive.srf_identity import last_answer_container, _generating
    if not isinstance(ax.get('elements'), list) or ax.get('truncated') is True:
        return None
    if _generating(ax):
        return False
    if ownership is None:
        return None
    return True if last_answer_container(ax, ownership=ownership) is not None else None


# @anchor:wa.learn.cu_loop._web_viewport_bar
def _web_viewport_bar(ax):
    from wender.perceive.srf_identity import _terminal_bar, _eligible
    rows = ax.get('elements', [])
    if ax.get('truncated') is not False:
        return None
    candidates = []
    for cid in {(e.get('container') or {}).get('id') for e in rows} - {None}:
        terminal = _terminal_bar(ax, cid)
        if terminal is not None and all(_eligible(e) for e in rows if
                (e.get('container') or {}).get('id') == cid and e.get('name', '').casefold() in ('copy', 'helpful')):
            candidates.append((cid, terminal))
    return candidates[0] if len(candidates) == 1 else None


# @anchor:wa.learn.cu_loop._web_prompt_matches
def _web_prompt_matches(surface, node, task):
    """Compare private question evidence using the measured producer normalization."""
    value = node.get('name', '')
    if len(value) >= 120 or node.get('truncated') is True:
        value = surface.text(node['id'])
    if not isinstance(value, str):
        return False
    normalized = re.sub(r'[^a-z0-9]', '', task.lower())
    return bool(normalized) and re.sub(r'[^a-z0-9]', '', value.lower()) == normalized


# @anchor:wa.learn.cu_loop._web_viewport_harvest
def _web_viewport_harvest(surface, ax, ownership, *, task, origin, observe, send, ready):
    from wender.perceive.disambig import verify_harvested_text
    rows = ax['elements']
    anchors = [e for e in rows if e.get('region') == 'main' and
               e.get('role') == 'StaticText' and e.get('actionable') is not True
               and isinstance(e.get('box'), list)]
    if not anchors:
        raise BackendError('THREAD_ANCHOR_UNAVAILABLE')
    send({'kind': 'click', 'element': anchors[0], 'purpose': 'viewport_thread_focus'})
    send({'kind': 'key', 'keysym': 'Home'})
    parts, seen, preamble, total, started, duration = [], {}, set(), 0, False, False
    page_ids, stagnant = set(), 0
    try:
        for page in range(60):
            ready()
            current = observe()
            if (current.get('truncated') is not False or _web_generating(current) or
                    (current.get('window') or {}).get('url', current.get('url')) != origin):
                raise BackendError('HARVEST_INVALIDATED')
            rows = current['elements']
            composer = _web_composer(current)
            if composer is None:
                raise BackendError('HARVEST_INVALIDATED')
            composer_cid = (composer.get('container') or {}).get('id')
            if not isinstance(composer_cid, str) or not composer_cid:
                raise BackendError('HARVEST_INVALIDATED')
            cut = next((i for i, e in enumerate(rows) if composer_cid is not None and
                        (e.get('container') or {}).get('id') == composer_cid), len(rows))
            rows = rows[:cut]
            if page == 0 and not any(e.get('role') in ('heading', 'StaticText') and e.get('region') == 'main' and
                    (e.get('container') or {}).get('id') not in ownership.boundary_containers and
                    _web_prompt_matches(surface, e, task) for e in rows):
                raise BackendError('HARVEST_TOP_UNPROVEN')
            bar = _web_viewport_bar(dict(current, elements=rows))
            current_ids = {e.get('id') for e in rows if e.get('role') == 'StaticText'} - {None}
            stagnant = stagnant + 1 if not current_ids - page_ids else 0
            page_ids.update(current_ids)
            for node in rows[:bar[1] if bar else len(rows)]:
                if (node.get('role') != 'StaticText' or node.get('region') != 'main' or
                        node.get('actionable') is True or not isinstance(node.get('box'), list) or
                        (node.get('container') or {}).get('id') in ownership.boundary_containers or
                        (composer_cid is not None and (node.get('container') or {}).get('id') == composer_cid)):
                    continue
                value, ident = node.get('name'), node.get('id')
                if not isinstance(value, str) or not isinstance(ident, str) or not ident:
                    raise BackendError('HARVEST_TEXT_UNAVAILABLE')
                if ident in preamble:
                    continue
                if not started:
                    preamble.add(ident)
                    if re.fullmatch(r'Finished(?:\s+(?:\d+m )?\d+s)?', value):
                        started = True
                        duration = value != 'Finished'
                    continue
                if not duration:
                    preamble.add(ident)
                    if not re.fullmatch(r'(?:\d+m )?\d+s', value):
                        raise BackendError('HARVEST_MARKER_UNPROVEN')
                    duration = True
                    continue
                if len(value) >= 120 or node.get('truncated') is True:
                    ready()
                    value = surface.text(ident)
                    ready()
                    if not isinstance(value, str):
                        raise BackendError('HARVEST_TEXT_UNAVAILABLE')
                if ident in seen:
                    if seen[ident] != value:
                        raise BackendError('HARVEST_NODE_CHANGED')
                    continue
                total += len(value.encode('utf-8')) + bool(parts)
                if total > 65536 or len(seen) >= 4096:
                    raise BackendError('HARVEST_OVERFLOW')
                seen[ident] = value
                parts.append(value)
            if (bar is not None and started) or stagnant >= 2:
                text = '\n'.join(parts)
                if not started or not duration or verify_harvested_text(text, ownership)[0] is not True:
                    raise BackendError('HARVEST_INVALID')
                fresh = observe()
                if ((fresh.get('window') or {}).get('url', fresh.get('url')) != origin or
                        fresh.get('truncated') is not False or _web_generating(fresh)):
                    raise BackendError('HARVEST_INVALIDATED')
                return text
            if page == 59:
                raise BackendError('HARVEST_PAGE_LIMIT')
            send({'kind': 'key', 'keysym': 'Page_Down'})
    finally:
        parts.clear()
        seen.clear()
        preamble.clear()
        page_ids.clear()


def _web_harvest(surface, ax, ownership, *, task, observe, send, ready):
    from wender.perceive.srf_identity import last_answer_container, _terminal_bar, _generating
    from wender.perceive.disambig import verify_harvested_text
    cid = last_answer_container(ax, ownership=ownership)
    if cid is None:
        raise BackendError('HARVEST_INCOMPLETE')
    ready()
    text = surface.text(cid)
    ready()
    origin = (ax.get('window') or {}).get('url', ax.get('url'))
    if not isinstance(origin, str):
        raise BackendError('HARVEST_SCOPE_UNAVAILABLE')
    if text is not None:
        if not isinstance(text, str) or len(text.encode('utf-8')) > 65536:
            raise BackendError('HARVEST_OVERFLOW')
        if verify_harvested_text(text, ownership)[0] is not True:
            raise BackendError('HARVEST_INVALID')
        fresh = observe()
        if (last_answer_container(fresh, ownership=ownership) != cid or
                (fresh.get('window') or {}).get('url', fresh.get('url')) != origin):
            raise BackendError('HARVEST_INVALIDATED')
        return text
    # None is missing full-container evidence, never an empty successful result.
    # This fallback requires a complete observed panel and refuses ordinary
    # viewport inventories that do not expose that geometry.
    panels = [e for e in ax.get('elements', []) if e.get('role') in ('article', 'document')
              and e.get('region') == 'main' and (e.get('container') or {}).get('id') == cid]
    if len(panels) != 1:
        raise BackendError('THREAD_ANCHOR_UNAVAILABLE')
    origin = (ax.get('window') or {}).get('url', ax.get('url'))
    english = any(re.match(r'^Finished\b', e.get('name', ''), re.I)
                  and (e.get('container') or {}).get('id') == cid for e in ax['elements'])
    send({'kind': 'click', 'element': panels[0], 'purpose': 'thread_focus'})
    send({'kind': 'key', 'keysym': 'Home'})
    current = observe()
    parts, seen, total = [], {}, 0
    try:
        for page in range(60):
            ready()
            if (current.get('truncated') is not False or _generating(current) or
                    (current.get('window') or {}).get('url', current.get('url')) != origin):
                raise BackendError('HARVEST_INVALIDATED')
            rows = current.get('elements', [])
            owned = [e for e in rows if e.get('region') == 'main' and
                     (e.get('container') or {}).get('id') == cid]
            top = -1
            if page == 0:
                headings = [i for i, e in enumerate(rows) if e in owned and
                            e.get('role') == 'heading' and
                            _web_prompt_matches(surface, e, task)]
                if len(headings) != 1:
                    raise BackendError('HARVEST_TOP_UNPROVEN')
                top = headings[0]
            terminal = _terminal_bar(current, cid, english=english)
            body = [e for i, e in enumerate(rows) if i > top and (terminal is None or i < terminal)
                    and e.get('region') == 'main' and (e.get('container') or {}).get('id') == cid
                    and e.get('role') in ('paragraph', 'text', 'StaticText')
                    and e.get('actionable') is not True]
            for node in body:
                ident = node.get('id')
                if not isinstance(ident, str) or not ident:
                    raise BackendError('HARVEST_ID_UNAVAILABLE')
                value = node.get('name')
                if not isinstance(value, str):
                    raise BackendError('HARVEST_TEXT_UNAVAILABLE')
                if node.get('truncated') is True or value.endswith(('…', '...')):
                    if re.fullmatch(r'[0-9a-fA-F]+', ident) is None:
                        raise BackendError('HARVEST_TEXT_UNAVAILABLE')
                    ready()
                    value = surface.text(ident)
                    ready()
                    if not isinstance(value, str):
                        raise BackendError('HARVEST_TEXT_UNAVAILABLE')
                if ' '.join(value.split()) == ' '.join(task.split()) or re.fullmatch(
                        r'(?i)\s*(?:Finished\s+\d+\s*s|Investigado\s+\d+\s*s|Searching.*|Buscando.*|Copy|Copiar|Helpful|Sources|Fuentes)\s*', value):
                    continue
                if ident in seen:
                    if seen[ident] != value:
                        raise BackendError('HARVEST_NODE_CHANGED')
                    continue
                if len(seen) >= 4096:
                    raise BackendError('HARVEST_NODE_LIMIT')
                total += len(value.encode('utf-8')) + (1 if parts else 0)
                if total > 65536:
                    raise BackendError('HARVEST_OVERFLOW')
                seen[ident] = value
                parts.append(value)
            if terminal is not None:
                text = '\n'.join(parts)
                if verify_harvested_text(text, ownership)[0] is not True:
                    raise BackendError('HARVEST_INVALID')
                return text
            if page == 59:
                raise BackendError('HARVEST_PAGE_LIMIT')
            positions = {e.get('id'): e.get('box') for e in body if e.get('id')}
            send({'kind': 'key', 'keysym': 'Page_Down'})
            following = observe()
            moved = any(e.get('id') in positions and isinstance(e.get('box'), list)
                        and isinstance(positions[e['id']], list) and
                        e['box'][1] != positions[e['id']][1]
                        for e in following.get('elements', [])
                        if (e.get('container') or {}).get('id') == cid)
            if not moved:
                raise BackendError('THREAD_SCROLL_UNPROVEN')
            current = following
    finally:
        parts.clear()
        seen.clear()


# @anchor:wa.learn.cu_loop.run_web_cycle
def run_web_cycle(surface, planner, executor, memory, *, task, trace, max_seconds=300,
                  postcheck_seconds=2, wait_seconds=180, clock=time.monotonic, sleep=time.sleep,
                  memory_hooks=None, integration=None, fast_path=False):
    """Reviewed one-send workflow; all private evidence stays in cycle RAM.

    fast_path (memory tier, opt-in): the reviewed workflow is used as the plan without a planner call,
    and a known step with exactly one grounded option is executed without a model choice. Anything
    ambiguous or unknown still goes to the models; every postcheck and safety gate is unchanged.
    """
    from wender.perceive.srf_identity import ax_options, last_answer_container, _generating
    from wender.perceive.disambig import capture_turn_boundary
    from wender.learn.cu_tasks import web_predicate
    cycle_start = clock()
    deadline = cycle_start + max_seconds
    stages = dict.fromkeys(('t_home_ready', 't_composer_found', 't_typed',
                            't_sent_verified', 't_done_detected', 't_harvest_done'))
    counters = dict(model_calls=0, inputs_sent=0, wrong_or_refused_inputs=0, recoveries=0, fast_path_hits=0)
    prompt_matches = False
    history, escalations, completed = [], [], []
    ring, block_filter = WorkingMemoryRing(), BlockedTargetFilter()
    harvested, reason, failure, cleanup_ok = None, 'failure', None, False
    integration = integration if integration is not None else getattr(surface, 'integration', None)
    possible_input, last_dispatched, ownership, latest = False, None, None, None
    bound_length = None
    bound_composer = None
    thread_url = None
    thread_heading = False
    plan = None
    def note_script_input(possible):
        nonlocal possible_input
        possible_input = possible_input or possible
    def emit(row):
        trace.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
        trace.flush()
    def metadata(backend):
        allowed = ('input_tokens', 'output_tokens', 'latency_ms')
        raw = getattr(backend, 'last_model', {})
        return {k: raw[k] for k in allowed if isinstance(raw, dict) and
                type(raw.get(k)) in (int, float) and math.isfinite(raw[k]) and raw[k] >= 0}
    def ready():
        if clock() >= deadline:
            raise TimeoutError('cycle budget exhausted')
        if integration.check_ready() is not True:
            raise BackendError('RESTART_UNCERTAIN')
        if hasattr(surface, 'timeout'):
            surface.timeout = min(surface.timeout, deadline - clock())
    def escalate(why, intent):
        row = {'event': 'escalation', 'reason': why, 'intent': intent}
        escalations.append(row)
        counters['recoveries'] += 1
        if memory_hooks is not None:
            memory_hooks.on_escalation(why, intent)
        emit(row)
    def choose(backend, intent, options):
        ready()
        if integration.charge_model() is not True:
            raise BackendError('MODEL_BUDGET')
        kwargs = {}
        if backend is planner and 'lessons' in inspect.signature(backend.choose).parameters:
            kwargs['lessons'] = memory.get('lessons', [])
        rows = ring.for_muse() if isinstance(backend, MuseBackend) else ring.for_local()
        counters['model_calls'] += 1
        chosen = backend.choose(intent, options, rows, **kwargs)
        ready()
        return chosen
    def send(physical):
        nonlocal possible_input, last_dispatched
        try:
            ready()
            if physical['kind'] == 'key' and physical['keysym'].casefold() in ('escape', 'esc'):
                raise BackendError('GENERATION_KEY_REFUSED')
            if physical['kind'] == 'click' and re.search(r'^(?:Stop|Detener)\b',
                                                         physical['element'].get('name', ''), re.I):
                raise BackendError('GENERATION_CONTROL_REFUSED')
            last_dispatched = physical
            possible_input = True
            def actuate():
                counters['inputs_sent'] += 1
                return surface.input(physical)
            return integration.dispatch(surface, physical, actuate)
        except Exception:
            counters['wrong_or_refused_inputs'] += 1
            raise
    def observe():
        nonlocal latest
        for attempt in range(15):   # producer exit 4 (not ready) is transient while typing/re-rendering [MEASURED]
            try:
                ready()
                surface.frame()
                ready()
                latest = surface.ax()
                ready()
                break
            except (_SurfaceNotReady, _SurfaceRefused):   # reads only: rc 6 is transient while the page navigates [MEASURED]
                if attempt == 14 or clock() >= deadline:
                    raise
                sleep(.2)
        if any(re.search(r'captcha|verify you are human|l[ií]mite alcanzado|rate limit|usage limit',
                         e.get('name', ''), re.I) for e in latest['elements']):
            raise BackendError('OPERATOR_INTERVENTION')
        return latest
    def recognized(ax):
        return web_predicate({'kind': 'url', 'pattern': r'^https://(?:www\.)?perplexity\.ai(?:/|$)'}, ax) is True
    def poll(predicate, seconds):
        end = min(deadline, clock() + max(0, seconds))
        while True:
            try:
                ax = observe()
            except _SurfaceNotReady:
                # Producer exit 4 = transiently unavailable (page re-render): retry until the step deadline.
                if clock() >= end:
                    raise
                sleep(min(.2, max(0, end - clock())))
                continue
            if predicate(ax) is True:
                return ax
            if clock() >= end:
                if possible_input:
                    raise BackendError('RECONCILIATION_REQUIRED')
                raise BackendError('POSTCHECK_FAILED')
            sleep(min(.1, end - clock()))
    def composer_at(ax, ident):
        target = _web_composer(ax)
        return target if target and target['id'] == ident else None
    def focus(target):
        send({'kind': 'click', 'element': target})
        ident = target['id']
        return poll(lambda ax: bool((t := composer_at(ax, ident)) and
                    (t.get('focused') is True or (t.get('states') or {}).get('focused') is True)), postcheck_seconds)
    def step_options(step, ax):
        for element in ax.get('elements', []):
            block_filter.check(element)
        action = step['action']
        composer = _web_composer(ax) if action.get('control') == 'composer' else None
        projection = {'elements': [composer]} if composer is not None else ax
        if action['kind'] == 'harvest':
            from wender.perceive.srf_identity import _plain_answer_control
            cid = last_answer_container(ax, ownership=ownership)
            if cid is None and thread_url is not None:
                cid = (_web_viewport_bar(ax) or (None,))[0]
            if cid is not None:
                projection = {'elements': [e for e in ax['elements'] if
                    (e.get('container') or {}).get('id') == cid and e.get('role') == 'button' and
                    re.fullmatch(r'Copy|Copiar', e.get('name', ''), re.I) and
                    _plain_answer_control(e, ax['elements'])]}
        options, targets = ax_options(projection)
        safe = {k for k, e in targets.items() if not block_filter._BLOCK_RE.search(e.get('name', ''))
                and not re.search(r'^(?:Stop|Detener|Investigando)\b', e.get('name', ''), re.I)}
        options = [o for o in options if o['id'] in safe or o['id'] == 'navigation']
        targets = {k: v for k, v in targets.items() if k in safe}
        action = step['action']
        if action['kind'] == 'wait':
            return [{'id': 'wait', 'role': 'action', 'name': 'Wait for research completion',
                     'region': 'main', 'container_ordinal': None}], {}, recognized(ax) and bool(_web_composer(ax))
        control = memory['control_map'][action['control']]
        wanted = {k for k, e in targets.items() if e.get('role') == control['role']
                  and re.search(control['pattern'], e.get('name', ''))
                  and e.get('region') == control['region']}
        if action.get('control') == 'composer':
            composer = _web_composer(ax)
            wanted = {k for k in wanted if composer is not None and targets[k]['id'] == composer['id']}
        if action['kind'] == 'harvest':
            cid = last_answer_container(ax, ownership=ownership)
            if cid is None and thread_url is not None:
                cid = (_web_viewport_bar(ax) or (None,))[0]
            wanted = {k for k in wanted if cid is not None and (targets[k].get('container') or {}).get('id') == cid}
        selected = [o for o in options if o['id'] in wanted]
        return selected or options, targets, bool(selected) and recognized(ax)
    try:
        if integration is None:
            raise BackendError('DISPATCH_OWNER_UNAVAILABLE')
        # Entry gates precede scripts, providers and all physical input.
        if not isinstance(task, str) or not task or len(task) > 65536 or not task.isascii():
            counters['wrong_or_refused_inputs'] += 1
            raise BackendError('TASK_REFUSED')
        ready()
        if hasattr(surface, 'trace'):
            surface.trace = trace
        surface.start()
        before = observe()
        script_completed = _script_before_model(integration, before, {'task': task}, clock=clock,
                                deadline=deadline, note_input=note_script_input)
        if script_completed:
            plan = []
        if plan is None and fast_path and memory.get('workflow', {}).get('steps'):
            plan = [dict(step) for step in memory['workflow']['steps']]   # reviewed plan, no planner call
            counters['fast_path_hits'] += 1
        if plan is None:
            if integration.charge_model() is not True:
                raise BackendError('MODEL_BUDGET')
            counters['model_calls'] += 1
            plan = planner.plan(task, memory)
            ready()
        if memory_hooks is not None:
            memory_hooks.on_plan(memory)
        canonical = memory['workflow']['steps']
        if not script_completed and (not isinstance(plan, list) or len(plan) != len(canonical)):
            raise BackendError('INVALID_WORKFLOW')
        for step, template in zip(plan, canonical):
            if any(step.get(k) != template[k] for k in ('intent', 'action', 'postcheck')):
                raise BackendError('UNREVIEWED_WORKFLOW')
        emit({'event': 'plan', 'intents': [s['intent'] for s in plan], 'model': metadata(planner)})
        for step in plan:
            possible_input = False
            intent, action = step['intent'], step['action']
            before = observe()
            if action['kind'] == 'navigate_home':
                if intent != 'new_thread' or action != {'kind': 'navigate_home'}:
                    raise BackendError('UNREVIEWED_NAVIGATION')
                if not recognized(before) or _web_composer(before) is None:
                    escalate('unknown_screen', intent)
                    raise BackendError('UNKNOWN_SCREEN')
                send({'kind': 'key', 'keysym': 'ctrl+l'})
                send({'kind': 'type', 'text': 'https://www.perplexity.ai/'})
                send({'kind': 'key', 'keysym': 'Return'})
                after = poll(lambda ax: bool(recognized(ax) and
                             re.fullmatch(r'https://(?:www\.)?perplexity\.ai/(?:[?#].*)?',
                                          (ax.get('window') or {}).get('url', ax.get('url', ''))) and
                             (t := _web_composer(ax)) and
                             type(t.get('value_len')) is int and t['value_len'] >= 0 and not _generating(ax)), postcheck_seconds)
                stages['t_home_ready'] = (clock() - cycle_start) * 1000
                stages['t_composer_found'] = stages['t_home_ready']
                completed.append(intent)
                emit({'event': 'step', 'intent': intent, 'ok': True})
                continue
            if action['kind'] == 'harvest' and thread_url is not None and \
                    last_answer_container(before, ownership=ownership) is None:
                # Reviewed paging harvest (os-cu-0 page model): the answer bar is below the fold, so there is no
                # on-screen control to choose; read the thread page by page instead of asking a model.
                if (before.get('window') or {}).get('url', before.get('url')) != thread_url:
                    raise BackendError('THREAD_CHANGED')
                harvested = _web_viewport_harvest(surface, before, ownership, task=task,
                    origin=thread_url, observe=observe, send=send, ready=ready)
                prompt_matches = True  # Home verified the private question before collecting text.
                stages['t_harvest_done'] = (clock() - cycle_start) * 1000
                completed.append(intent)
                emit({'event': 'step', 'intent': intent, 'ok': True})
                continue
            options, targets, known = step_options(step, before)
            backend = executor
            if not known:
                escalate('unknown_screen', intent)
                backend = planner
            if fast_path and known and len(options) == 1 and options[0].get('id') not in (None, 'navigation', 'abstain'):
                chosen = options[0]['id']   # memory tier: one grounded candidate on a known screen
                counters['fast_path_hits'] += 1
            else:
                chosen = choose(backend, intent, options)
            if chosen == 'abstain' and backend is not planner:
                escalate('abstain', intent)
                backend = planner
                chosen = choose(planner, intent, options)
            if not known or chosen == 'navigation' or chosen not in {o['id'] for o in options}:
                raise BackendError('NO_REVIEWED_BINDING')
            target = targets.get(chosen)
            if action['kind'] == 'type':
                ident = target['id']
                bound_composer = ident
                focused = focus(target)
                initial = composer_at(focused, ident).get('value_len')
                if type(initial) is int and initial not in _EMPTY_COMPOSER:
                    # Perplexity restores an unsent draft into the home composer [MEASURED]:
                    # clear it inside the focused composer (reviewed keys), then re-check.
                    send({'kind': 'key', 'keysym': 'ctrl+a'})
                    send({'kind': 'key', 'keysym': 'BackSpace'})
                    cleared = poll(lambda ax: bool((t := composer_at(ax, ident)) and
                                   t.get('value_len') in _EMPTY_COMPOSER), postcheck_seconds)
                    initial = composer_at(cleared, ident).get('value_len')
                if type(initial) is not int or initial not in _EMPTY_COMPOSER:
                    raise BackendError('COMPOSER_NOT_EMPTY')
                # Splitting only after bounded ASCII admission avoids partial unsafe input.
                for line_index, line in enumerate(task.split('\n')):
                    if line_index:
                        send({'kind': 'key', 'keysym': 'shift+Return'})
                    for offset in range(0, len(line), 1500):
                        send({'kind': 'type', 'text': line[offset:offset + 1500]})
                after = poll(lambda ax: bool((t := composer_at(ax, ident)) and
                             t.get('value_len') in {len(task), initial + len(task)}), postcheck_seconds)   # the empty-composer placeholder (1) is not additive [MEASURED]
                bound_length = composer_at(after, ident)['value_len']
                stages['t_typed'] = (clock() - cycle_start) * 1000
            elif action['kind'] == 'key':
                if action.get('keysym') != 'Return':
                    raise BackendError('UNREVIEWED_KEY')
                if target['id'] != bound_composer:
                    raise BackendError('COMPOSER_CHANGED')
                focused = focus(target)
                ident = target['id']
                if composer_at(focused, ident).get('value_len') != bound_length or _generating(focused):
                    raise BackendError('SUBMISSION_UNPROVEN')
                # Boundary is the final fresh observation immediately before submit.
                ownership = capture_turn_boundary(focused, turn_index=1, task_text=task,
                                                   prev_answer_sha256=None)
                send({'kind': 'key', 'keysym': 'Return'})
                after = poll(lambda ax: bool((t := composer_at(ax, ident)) and
                             t.get('value_len') in _EMPTY_COMPOSER) or _generating(ax) or
                             _web_complete(ax, ownership) is True, max(postcheck_seconds, 20))   # submit -> /search/ can take up to 20 s [os-cu-0 measured]
                stages['t_sent_verified'] = (clock() - cycle_start) * 1000
                prompt_matches = any(e.get('role') == 'heading' and e.get('region') == 'main' and
                                     (e.get('container') or {}).get('id') not in ownership.boundary_containers and
                                     _web_prompt_matches(surface, e, task)
                                     for e in after.get('elements', []))
                submitted_url = (after.get('window') or {}).get('url', after.get('url'))
                if isinstance(submitted_url, str) and re.fullmatch(r'https://(?:www\.)?perplexity\.ai/search/[^/?#]+(?:[?#].*)?', submitted_url):
                    thread_url, thread_heading = submitted_url, prompt_matches
            elif action['kind'] == 'wait':
                # Completion is independent of the below-fold answer action bar.
                end_wait = min(deadline, clock() + wait_seconds)
                idle_polls, stop_seen, sent_at = 0, False, clock()
                while True:
                    after = observe()
                    url = (after.get('window') or {}).get('url', after.get('url'))
                    is_thread = isinstance(url, str) and re.fullmatch(r'https://(?:www\.)?perplexity\.ai/search/[^/?#]+(?:[?#].*)?', url)
                    if is_thread:
                        if thread_url is not None and url != thread_url:
                            raise BackendError('THREAD_CHANGED')
                        thread_url = url
                        headings = [e for e in after['elements'] if e.get('role') == 'heading'
                                    and e.get('region') == 'main' and
                                    (e.get('container') or {}).get('id') not in ownership.boundary_containers]
                        if any(not _web_prompt_matches(surface, e, task) for e in headings):
                            raise BackendError('THREAD_CHANGED')
                        thread_heading = thread_heading or any(e.get('role') == 'heading' and
                            e.get('region') == 'main' and
                            (e.get('container') or {}).get('id') not in ownership.boundary_containers and
                            _web_prompt_matches(surface, e, task)
                            for e in after['elements'])
                    # Done is decided from the composer bar, which is always on screen (os-cu-0 page model,
                    # measured): an actionable 'Stop response' was seen and has been absent for 2 consecutive
                    # polls; if Stop is never seen, done 30 s after the verified send. The answer action bar is
                    # looked for only during the paging harvest. Never Esc, never 'Scroll to end' here.
                    generating = _web_generating(after)
                    stop_seen = stop_seen or generating
                    idle_polls = 0 if generating else idle_polls + 1
                    if recognized(after) and thread_url is not None and url == thread_url and (
                            (stop_seen and idle_polls >= 2) or
                            (not stop_seen and clock() - sent_at >= 30)):
                        break
                    if clock() >= end_wait:
                        raise BackendError('POSTCHECK_FAILED')
                    sleep(min(1, max(0, end_wait - clock())))
                stages['t_done_detected'] = (clock() - cycle_start) * 1000
                prompt_matches = thread_heading or any(e.get('role') == 'heading' and e.get('region') == 'main' and
                                     (e.get('container') or {}).get('id') not in ownership.boundary_containers and
                                     _web_prompt_matches(surface, e, task)
                                     for e in after.get('elements', []))
            elif action['kind'] == 'harvest':
                if thread_url is not None:
                    if (before.get('window') or {}).get('url', before.get('url')) != thread_url:
                        raise BackendError('THREAD_CHANGED')
                    if last_answer_container(before, ownership=ownership) is None:
                        harvested = _web_viewport_harvest(surface, before, ownership, task=task,
                            origin=thread_url, observe=observe, send=send, ready=ready)
                        prompt_matches = True  # Home verified the private question before collecting text.
                    else:
                        if not thread_heading:
                            raise BackendError('HARVEST_TOP_UNPROVEN')
                        harvested = _web_harvest(surface, before, ownership, task=task,
                            observe=observe, send=send, ready=ready)
                else:
                    harvested = _web_harvest(surface, before, ownership, task=task,
                                             observe=observe, send=send, ready=ready)
                stages['t_harvest_done'] = (clock() - cycle_start) * 1000
            else:
                raise BackendError('UNSUPPORTED_ACTION')
            completed.append(intent)
            row = {'event': 'step', 'intent': intent, 'id': chosen, 'ok': True,
                   'backend': 'muse' if backend is planner else 'local', 'model': metadata(backend)}
            history.append({k: row[k] for k in ('intent', 'id', 'ok')})
            # No private snapshot digests, producer identifiers or action payloads.
            ring.append(Receipt(len(history), intent, intent, 'sealed', 'web_postcheck',
                                'verified', ok=True, option_id=chosen))
            emit(row)
        reason = 'goal_reached'
    except Exception as exc:
        if possible_input and integration is not None:
            integration.uncertain(last_dispatched)
        failure = {'error_class': type(exc).__name__, 'detail': (str(exc) if isinstance(exc, BackendError) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', str(exc)) else type(exc).__name__)}  # detail = our own closed codes only, never page text
        reason = 'timeout' if isinstance(exc, TimeoutError) else 'failure'
        emit({'event': reason, **failure})
    finally:
        try:
            surface.stop()
            cleanup_ok = True
        except Exception as exc:
            failure = {'error_class': type(exc).__name__, 'stage': 'cleanup'}
            reason = 'cleanup_failed'
        emit({'event': 'cleanup', 'ok': cleanup_ok})
        latest, ownership, last_dispatched, bound_composer = None, None, None, None
    return {'stop_reason': reason, 'goal_verified': reason == 'goal_reached' and cleanup_ok,
            'completed': completed, 'escalations': escalations, 'failure': failure,
            'cleanup_ok': cleanup_ok, 'harvested': harvested if reason == 'goal_reached' else None,
            **stages, **counters, 'prompt_matches': prompt_matches,
            'agent_ms': stages['t_sent_verified'],
            'answer_wait_ms': (stages['t_done_detected'] - stages['t_sent_verified']
                               if stages['t_done_detected'] is not None and stages['t_sent_verified'] is not None else None),
            'harvest_ms': (stages['t_harvest_done'] - stages['t_done_detected']
                           if stages['t_harvest_done'] is not None and stages['t_done_detected'] is not None else None),
            'blocked_controls': block_filter.counts(),
            **({'drift': memory_hooks.summary()} if memory_hooks is not None else {})}


if __name__ == "__main__":
    raise SystemExit(main())
