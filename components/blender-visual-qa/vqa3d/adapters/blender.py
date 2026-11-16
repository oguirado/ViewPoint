"""Blender Workbench capture on a scratch copy; the source is never saved.

A declared empty environment and the existing owned-process context enforce
bounded lifetime and cleanup of the recorded process group. Render PNGs use
anonymous RAM files in the side script; only JSON evidence is written to scratch.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from blender_visual_qa.drive.launch import declared_env, env_sha256, _owned_process
from ..contract import EnvAdapter, Observation
from ..canon import canonical_json, integer, state_copy

ROOT = Path(__file__).resolve().parents[2]
BLENDER = Path("/usr/bin/blender")


def validate_action(action):
    """Transforms cross the process boundary only as quantised integers."""
    canonical_json(action)
    if type(action) is not dict:
        raise ValueError("action_must_be_object")
    if action.get("op") == "set_frame":
        if set(action) != {"op", "frame"}:
            raise ValueError("invalid_blender_action_fields")
        integer(action["frame"])
        return action
    field = {"set_location": "value_um", "set_rotation": "value_mdeg",
             "set_scale": "value_ppm", "hide": "value"}.get(action.get("op"))
    if field is None:
        raise ValueError("unsupported_action_op:" + str(action.get("op")))
    if set(action) != {"op", "object", field}:
        raise ValueError("invalid_blender_action_fields")
    if type(action["object"]) is not str or not action["object"]:
        raise ValueError("object_name_required")
    value = action[field]
    if action["op"] == "hide":
        if integer(value) not in (0, 1):
            raise ValueError("hide_requires_zero_one")
    else:
        if type(value) is not list or len(value) != 3:
            raise ValueError("transform_requires_integer_vector")
        for item in value:
            integer(item)
    return action


class BlenderAdapter(EnvAdapter):
    id = "blender"
    version = "1"
    status = "implemented"

    def __init__(self, source=None, *, scratch_parent=None, timeout=90, width=96, height=72,
                 max_width=96, max_height=72, capture_engine="workbench", capture_samples=1, capture_denoising=False,
                 max_capture_samples=4096, color_mode="RGB", color_depth="8", max_sample_frames=2000,
                 probe_width=8, probe_height=8, probe_samples=1, probe_timeout=120):
        self.source = Path(source) if source else ROOT / "examples/cube3.blend"
        self.timeout = integer(timeout)
        self.width, self.height = integer(width), integer(height)
        if self.timeout <= 0 or not 1 <= width <= integer(max_width) or not 1 <= height <= integer(max_height):
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
        self._scratch_parent = Path(scratch_parent) if scratch_parent is not None else None
        self._tmp = None
        self._closed = False
        self.process_logs = []
        self.capabilities = {"capture_available": False,
                             "scene_state": False, "act": False}
        self.runtime_version = None

    def _prepare(self):
        if self._closed:
            raise RuntimeError("adapter_closed")
        if self._tmp is None:
            if not BLENDER.is_file() or not self.source.is_file():
                raise RuntimeError("blender_or_source_unavailable")
            self._tmp = tempfile.TemporaryDirectory(prefix="vqa3d-blender-", dir=self._scratch_parent)
            self.scratch = Path(self._tmp.name)
            self.blend = self.scratch / "scene.blend"
            shutil.copyfile(self.source, self.blend)
            try:
                self._invoke("init")
            except Exception:
                self._tmp.cleanup()
                self._tmp = None
                raise
            self.capabilities.update(scene_state=True, act=True)

    def _invoke(self, mode, action=None, *, frames=None, paths=None, device_type=None):
        request = self.scratch / "request.json"
        response = self.scratch / "response.json"
        if mode == "act":
            validate_action(action)
        request.write_text(canonical_json({"mode": mode, "action": action,
                                          "width": self.width, "height": self.height,
                                          "frames": frames, "paths": paths, "device_type": device_type,
                                          "capture_engine": self.capture_engine, "capture_samples": self.capture_samples,
                                          "capture_denoising": self.capture_denoising,
                                          "color_mode": self.color_mode, "color_depth": self.color_depth,
                                          "max_sample_frames": self.max_sample_frames,
                                          "probe_width": self.probe_width, "probe_height": self.probe_height,
                                          "probe_samples": self.probe_samples}), encoding="utf-8")
        response.unlink(missing_ok=True)
        argv = [str(BLENDER), "-b", "--factory-startup", "-noaudio", "--disable-autoexec",
                str(self.blend), "--python-exit-code", "9", "--python",
                str(Path(__file__).with_name("blender_side.py")), "--", str(request), str(response)]
        env = declared_env(self.scratch, 1)
        with _owned_process(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
            try:
                output, _ = proc.communicate(timeout=min(self.timeout, self.probe_timeout) if mode == "gpu_probe" else self.timeout)
            except subprocess.TimeoutExpired as exc:
                self.process_logs.append({"mode": mode, "pid": proc.pid, "pgid": pgid,
                                          "timeout": True, "output": (exc.output or b"").decode("utf-8", "replace")})
                raise TimeoutError("blender_timeout") from exc
            text = output.decode("utf-8", "replace")
            self.process_logs.append({"mode": mode, "pid": proc.pid, "pgid": pgid,
                                      "rc": proc.returncode, "env_sha256": env_sha256(env, self.scratch),
                                      "output": text})
            if response.is_file():
                error = json.loads(response.read_text(encoding="utf-8"))
                if error.get("ok") is False:
                    raise RuntimeError(error.get("error", "blender_side_failed"))
            if proc.returncode != 0 or any(line.lstrip().startswith(("Error:", "ERROR", "Traceback")) for line in text.splitlines()):
                raise RuntimeError("blender_process_or_log_error:" + str(proc.returncode))
        result = json.loads(response.read_text(encoding="utf-8"))
        if result.get("ok") is not True:
            raise RuntimeError("blender_side_failed")
        self.runtime_version = result["runtime_version"]
        return result

    def probe(self, render=False):
        reason = None
        try:
            self._prepare()
            observation = self.capture("probe")
            if not observation.capture_available:
                raise RuntimeError("capture_unavailable")
        except Exception as exc:
            reason = type(exc).__name__ + ":" + str(exc)
            self.capabilities["capture_available"] = False
        report = {"id": self.id, "version": self.version, "status": self.status,
                "available": reason is None, "reason": reason, "runtime_version": self.runtime_version,
                "capabilities": dict(self.capabilities), "renderer": {"workbench": "BLENDER_WORKBENCH", "eevee": "BLENDER_EEVEE", "cycles": "CYCLES"}[self.capture_engine]}
        if render:
            report.update(cycles_devices={}, denoisers=[], view_transforms=[], looks={},
                          engines=[], gpu_render_ok=False, gpu_render_stderr_tail="", gpu_render_ok_by_type={})
            try:
                self._prepare()
                report.update(self._invoke("render_caps")["render_capabilities"])
                for device_type, names in report["cycles_devices"].items():
                    if device_type == "CPU" or not names:
                        continue
                    report["gpu_render_ok_by_type"][device_type] = False
                    try:
                        report["gpu_render_ok_by_type"][device_type] = self._invoke("gpu_probe", device_type=device_type).get("gpu_render_ok") is True
                    except Exception as exc:
                        tail = (self.process_logs[-1].get("output", "") if self.process_logs else "")
                        report["gpu_render_stderr_tail"] = (report["gpu_render_stderr_tail"] + "\n" + device_type + ":" + tail + "\n" + str(exc))[-4096:]
                report["gpu_render_ok"] = any(report["gpu_render_ok_by_type"].values())
            except Exception as exc:
                report["render_probe_error"] = type(exc).__name__ + ":" + str(exc)
        return report

    def capture(self, tag):
        self.capabilities["capture_available"] = False
        self._prepare()
        result = self._invoke("capture")
        png = base64.b64decode(result["frame_png_base64"], validate=True) if result.get("frame_png_base64") else None
        observation = Observation.create(self.id, tag, result["state"], png, result.get("notes", []),
                                         result.get("camera_source", "none"), result.get("camera_name", ""))
        self.capabilities["capture_available"] = observation.capture_available
        return observation

    def scene_state(self):
        self._prepare()
        return state_copy(self._invoke("state")["state"])

    def sample(self, frames, paths):
        from ..temporal import validate_sample_request
        validate_sample_request(frames, paths, self.max_sample_frames)
        self._prepare()
        return self._invoke("sample", frames=frames, paths=paths)["samples"]

    def act(self, action):
        validate_action(action)
        self._prepare()
        result = self._invoke("act", action)
        return {"ok": result["ok"], "op": action["op"]}

    def close(self):
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
        self._closed = True
        self.capabilities = {key: False for key in self.capabilities}
