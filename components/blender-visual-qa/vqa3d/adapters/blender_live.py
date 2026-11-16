"""Attach to an existing GUI; never launch, save, or terminate Blender.

B02/B14 and FA-0030: a successful action is not evidence of a fresh draw.
Counters/provenance live in observation notes, outside comparable scene state.
The bridge is an explicitly trusted local script executor, not a sandbox.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from blender_visual_qa.drive.png import decode
from ..canon import canonical_json, integer, sha256_canonical, state_copy
from ..contract import EnvAdapter, Observation
from .blender import validate_action as validate_transform

MARKER = "VQA3D_RESPONSE "


def validate_action(action):
    canonical_json(action)  # Refuse every float before publishing anything.
    if type(action) is dict and action.get("op") == "run_script":
        if set(action) != {"op", "path"} or type(action["path"]) is not str or not action["path"]:
            raise ValueError("run_script_requires_path")
        return action
    return validate_transform(action)


class BlenderLiveAdapter(EnvAdapter):
    id = "blender_live"
    version = "1"
    status = "implemented"

    def __init__(self, live_dir=None, *, timeout=10, frame_timeout=10,
                 poll_interval=0.05, width=96, height=72, viewport=False,
                 max_width=96, max_height=72, timeout_ceiling=180,
                 capture_engine="workbench", capture_samples=1, capture_denoising=False, max_capture_samples=4096,
                 color_mode="RGB", color_depth="8", max_sample_frames=2000,
                 probe_width=8, probe_height=8, probe_samples=1, probe_timeout=120):
        directory = live_dir if live_dir is not None else os.environ.get("VQA3D_LIVE_DIR")
        self.live_dir = Path(directory).resolve() if directory else None
        if any(type(v) not in (int, float) or not 0 < v <= timeout_ceiling for v in
               (timeout, frame_timeout, poll_interval)):
            raise ValueError("invalid_live_timeouts")
        self.timeout, self.frame_timeout, self.poll_interval = timeout, frame_timeout, poll_interval
        self.width, self.height = integer(width), integer(height)
        if not 1 <= width <= integer(max_width) or not 1 <= height <= integer(max_height) or type(viewport) is not bool:
            raise ValueError("invalid_capture_limits")
        if capture_engine not in ("workbench", "eevee", "cycles"):
            raise ValueError("invalid_capture_engine")
        if not 1 <= integer(capture_samples) <= integer(max_capture_samples):
            raise ValueError("invalid_capture_samples")
        if color_mode not in ("RGB", "RGBA", "BW") or color_depth not in ("8", "16"):
            raise ValueError("invalid_capture_color")
        if type(capture_denoising) is not bool:
            raise ValueError("invalid_capture_denoising")
        self.capture_engine, self.capture_samples = capture_engine, capture_samples
        self.capture_denoising = capture_denoising
        self.color_mode, self.color_depth = color_mode, color_depth
        self.max_sample_frames = integer(max_sample_frames)
        self.probe_width, self.probe_height, self.probe_samples = map(integer, (probe_width, probe_height, probe_samples))
        self.probe_timeout = integer(probe_timeout)
        if min(self.max_sample_frames, self.probe_width, self.probe_height, self.probe_samples, self.probe_timeout) <= 0:
            raise ValueError("invalid_probe_or_sample_limits")
        self.viewport = viewport
        self.runtime_version = None
        self.capabilities = {"capture_available": False, "scene_state": False, "act": False}
        self.process_logs = []  # Exchange receipts, not GUI process ownership.
        self.action_receipts = []
        self._pending_draw = None
        self._last_action = None
        self._closed = False
        self._lock = threading.RLock()
        self._owned = set()

    def _check_target(self):
        path = self.live_dir / "bridge.json"
        if path.is_file():
            pid = integer(json.loads(path.read_text())["pid"])
            if pid <= 0:
                raise ValueError("invalid_live_target_pid")
            try:
                os.kill(integer(pid), 0)
            except ProcessLookupError:
                raise RuntimeError("live_target_dead") from None
            stat = Path("/proc") / str(pid) / "stat"
            if stat.is_file() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                raise RuntimeError("live_target_dead")

    def _exchange(self, mode, *, deadline=None, **fields):
        if self._closed:
            raise RuntimeError("adapter_closed")
        if self.live_dir is None:
            raise RuntimeError("VQA3D_LIVE_DIR_required")
        self._check_target()
        inbox, outbox = self.live_dir / "inbox", self.live_dir / "outbox"
        if not inbox.is_dir() or not outbox.is_dir():
            raise RuntimeError("live_bridge_directories_unavailable")
        request_id = "vqa3d_" + uuid.uuid4().hex
        request = dict(fields, request_id=request_id, mode=mode, live_dir=str(self.live_dir),
                       width=self.width, height=self.height, viewport=self.viewport,
                       capture_engine=self.capture_engine, capture_samples=self.capture_samples,
                       capture_denoising=self.capture_denoising,
                       color_mode=self.color_mode, color_depth=self.color_depth,
                       max_sample_frames=self.max_sample_frames,
                       probe_width=self.probe_width, probe_height=self.probe_height, probe_samples=self.probe_samples)
        # Read bytes at the host: execute exactly the shipped payload, not a
        # possibly stale module cache inside the long-lived GUI.
        payload = Path(__file__).with_name("blender_live_payload.py").read_text(encoding="utf-8")
        source = ("import json\nVQA3D_REQUEST = " + repr(canonical_json(request)) + "\n" +
                  "exec(compile(" + repr(payload) + ", " + repr(str(Path(__file__).with_name("blender_live_payload.py"))) +
                  ", 'exec'), {'bpy': bpy, '__file__': " + repr(str(Path(__file__).with_name("blender_live_payload.py"))) +
                  ", 'VQA3D_REQUEST': json.loads(VQA3D_REQUEST)})\n")
        script, response = inbox / (request_id + ".py"), outbox / (request_id + ".out")
        temp = inbox / (request_id + ".tmp")
        self._owned.update((temp, script, response))
        if mode == "capture":
            self._owned.add(self.live_dir / (request_id + ".png"))
            if self.viewport:
                self._owned.add(self.live_dir / (request_id + ".viewport.png"))
        limit = min(time.monotonic() + self.timeout, deadline) if deadline is not None else time.monotonic() + self.timeout
        try:
            with temp.open("x", encoding="utf-8") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, script)
            while time.monotonic() < limit:
                self._check_target()
                valid = False
                try:
                    lines = response.read_text(encoding="utf-8").splitlines()
                    status = lines[0]
                    # Only the final complete, structured line from OUR request.
                    envelope = next((line[len(MARKER):] for line in reversed(lines) if line.startswith(MARKER)), None)
                    result = json.loads(envelope) if envelope is not None else None
                    valid = (status in ("OK", "ERROR") and type(result) is dict and
                             result.get("request_id") == request_id and type(result.get("ok")) is bool and
                             result["ok"] == (status == "OK"))
                    if valid:
                        canonical_json(result)
                        self.runtime_version = result.get("runtime_version", self.runtime_version)
                        self.process_logs.append({"request_id": request_id, "mode": mode,
                                                  "status_line": status, "response": result})
                        return result, status
                except (OSError, ValueError, IndexError, UnicodeError):
                    if valid:
                        raise  # A matched complete response must satisfy the canonical schema.
                    pass  # Partial, foreign, malformed or mismatched responses.
                time.sleep(min(self.poll_interval, max(0, limit - time.monotonic())))
            raise TimeoutError("bridge_no_answer:" + mode)
        finally:
            for path in (temp, script, response):
                path.unlink(missing_ok=True)

    @staticmethod
    def _require(result):
        if result.get("ok") is not True:
            raise RuntimeError("live_payload_error:" + str(result.get("error", "ERROR")))
        for key in ("frame_seq", "depsgraph_seq"):
            if integer(result[key]) < 0:
                raise ValueError("invalid_live_counter")
        return result

    def _ping(self, deadline=None):
        return self._require(self._exchange("ping", deadline=deadline)[0])

    def probe(self, render=False):
        with self._lock:
            result, reason = {}, None
            try:
                result = self._ping()
                if type(self.runtime_version) is not str or not self.runtime_version:
                    raise RuntimeError("blender_version_missing")
                if result.get("background") is not False:
                    raise RuntimeError("blender_background_not_GUI")
                if not result.get("camera"):
                    raise RuntimeError("scene_camera_missing")
                if result.get("has_view3d") is not True:
                    raise RuntimeError("view3d_context_missing")
                self.capabilities.update(capture_available=True, scene_state=True, act=True)
            except Exception as exc:
                reason = type(exc).__name__ + ":" + str(exc)
                self.capabilities = {key: False for key in self.capabilities}
            report = {"id": self.id, "version": self.version, "status": self.status,
                    "available": reason is None, "reason": reason, "runtime_version": self.runtime_version,
                    "background": result.get("background"), "camera": result.get("camera"),
                    "frame_seq": result.get("frame_seq"), "depsgraph_seq": result.get("depsgraph_seq"),
                    "capabilities": dict(self.capabilities)}
            if render:
                report.update(engines=[], cycles_devices={}, denoisers=[], view_transforms=[], looks={},
                              gpu_render_ok=False, gpu_render_ok_by_type={}, gpu_render_stderr_tail="")
                try:
                    caps = self._require(self._exchange("render_caps")[0])
                    report.update(caps["render_capabilities"])
                    for device_type, names in report["cycles_devices"].items():
                        if device_type == "CPU" or not names:
                            continue
                        report["gpu_render_ok_by_type"][device_type] = False
                        try:
                            result = self._require(self._exchange("gpu_probe", device_type=device_type,
                                                   deadline=time.monotonic()+self.probe_timeout)[0])
                            report["gpu_render_ok_by_type"][device_type] = result.get("gpu_render_ok") is True
                        except Exception as exc:
                            report["gpu_render_stderr_tail"] = (report["gpu_render_stderr_tail"] + "\n" + device_type + ":" + str(exc))[-4096:]
                    report["gpu_render_ok"] = any(report["gpu_render_ok_by_type"].values())
                except Exception as exc:
                    report["render_probe_error"] = type(exc).__name__ + ":" + str(exc)
            return report

    def scene_state(self):
        with self._lock:
            return state_copy(self._require(self._exchange("state")[0])["state"])

    def _observation(self, tag, result, png=None, reason=None):
        metadata = {"capture_engine": self.capture_engine, "capture_samples": self.capture_samples}
        metadata.update({key: result.get(key) for key in ("frame_seq", "depsgraph_seq", "camera_source", "camera")})
        if reason:
            metadata["reason"] = reason
        if self._last_action:
            metadata["action_receipt"] = self._last_action
        if "viewport" in result:
            metadata["viewport"] = result["viewport"]
        notes = (canonical_json(metadata),) + ((reason,) if reason else ())
        obs = Observation.create(self.id, tag, result.get("state", {}), png, notes,
                                 result.get("camera_source", "none") if result.get("camera") else "none",
                                 result.get("camera") or "")
        self.capabilities["capture_available"] = obs.capture_available
        return obs

    def capture(self, tag):
        with self._lock:
            deadline = time.monotonic() + self.frame_timeout
            if self._pending_draw is not None:
                last = {}
                while True:
                    ping_started = time.monotonic()
                    result = self._ping(deadline)
                    if result is not None and result["frame_seq"] > self._pending_draw:
                        break
                    if result is not None:
                        last = result
                    # Do not publish a final ping with less budget than a round
                    # trip just measured on this bridge. A successful stale
                    # response is evidence; a ping timeout must still raise.
                    ping_budget = max(2 * (time.monotonic() - ping_started), 2 * self.poll_interval)
                    if time.monotonic() + ping_budget >= deadline:
                        time.sleep(max(0, deadline-time.monotonic()))
                        # Preserve only an actual post-action state read; no
                        # additional unbounded roundtrip after the draw deadline.
                        return self._observation(tag, last, reason="stale_frame_timeout")
                    time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
            result, _ = self._exchange("capture", deadline=deadline, after_seq=self._pending_draw)
            path = self.live_dir / (result["request_id"] + ".png")
            viewport_path = self.live_dir / (result["request_id"] + ".viewport.png")
            try:
                self._require(result)
                if result.get("reason"):
                    return self._observation(tag, result, reason=result["reason"])
                if self._pending_draw is not None and result["frame_seq"] <= self._pending_draw:
                    return self._observation(tag, result, reason="stale_frame_timeout")
                png = path.read_bytes()
                w, h, _ = decode(png)
                if w > self.width or h > self.height:
                    raise ValueError("live_frame_too_large")
                self._pending_draw = None
                return self._observation(tag, result, png)
            finally:
                path.unlink(missing_ok=True)
                viewport_path.unlink(missing_ok=True)

    def act(self, action):
        validate_action(action)
        with self._lock:
            fields = {"action": action}
            digest = None
            if action["op"] == "run_script":
                source = Path(action["path"]).read_bytes()
                digest = hashlib.sha256(source).hexdigest()
                fields.update(script_base64=base64.b64encode(source).decode("ascii"), script_sha256=digest)
            result, status = self._exchange("act", **fields)
            receipt = {"request_id": result["request_id"], "op": action["op"], "status_line": status,
                       "script_sha256": digest, "ok": result["ok"], "action": json.loads(canonical_json(action))}
            if status == "OK":
                self._require(result)
                integer(result["before_frame_seq"])
                receipt.update({key: result[key] for key in ("before_frame_seq", "frame_seq", "depsgraph_seq")})
            receipt["receipt_sha256"] = sha256_canonical(receipt)
            self.action_receipts.append(receipt)
            self._last_action = receipt
            if status == "ERROR":
                raise RuntimeError("live_action_ERROR:" + canonical_json(receipt) + ":" + str(result.get("error", "")))
            self._require(result)
            integer(result["before_frame_seq"])
            self._pending_draw = result["frame_seq"]
            return dict(receipt)

    def close(self):
        with self._lock:
            for path in self._owned:
                path.unlink(missing_ok=True)
            self._closed = True
            self.capabilities = {key: False for key in self.capabilities}
