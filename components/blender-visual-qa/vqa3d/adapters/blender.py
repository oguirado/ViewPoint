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
    field = {"set_location": "value_um", "set_rotation": "value_mdeg",
             "set_scale": "value_ppm", "hide": "value"}.get(action.get("op"))
    if field is None or set(action) != {"op", "object", field}:
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

    def __init__(self, source=None, *, scratch_parent=None, timeout=90, width=96, height=72):
        self.source = Path(source) if source else ROOT / "examples/cube3.blend"
        self.timeout = integer(timeout)
        self.width, self.height = integer(width), integer(height)
        if self.timeout <= 0 or not 1 <= width <= 96 or not 1 <= height <= 72:
            raise ValueError("invalid_capture_limits")
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

    def _invoke(self, mode, action=None):
        request = self.scratch / "request.json"
        response = self.scratch / "response.json"
        if mode == "act":
            validate_action(action)
        request.write_text(canonical_json({"mode": mode, "action": action,
                                          "width": self.width, "height": self.height}), encoding="utf-8")
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
                output, _ = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                self.process_logs.append({"mode": mode, "pid": proc.pid, "pgid": pgid,
                                          "timeout": True, "output": (exc.output or b"").decode("utf-8", "replace")})
                raise TimeoutError("blender_timeout") from exc
            text = output.decode("utf-8", "replace")
            self.process_logs.append({"mode": mode, "pid": proc.pid, "pgid": pgid,
                                      "rc": proc.returncode, "env_sha256": env_sha256(env, self.scratch),
                                      "output": text})
            if proc.returncode != 0 or any(line.lstrip().startswith(("Error:", "ERROR", "Traceback")) for line in text.splitlines()):
                raise RuntimeError("blender_process_or_log_error:" + str(proc.returncode))
        result = json.loads(response.read_text(encoding="utf-8"))
        if result.get("ok") is not True:
            raise RuntimeError("blender_side_failed")
        self.runtime_version = result["runtime_version"]
        return result

    def probe(self):
        reason = None
        try:
            self._prepare()
            observation = self.capture("probe")
            if not observation.capture_available:
                raise RuntimeError("capture_unavailable")
        except Exception as exc:
            reason = type(exc).__name__ + ":" + str(exc)
            self.capabilities["capture_available"] = False
        return {"id": self.id, "version": self.version, "status": self.status,
                "available": reason is None, "reason": reason, "runtime_version": self.runtime_version,
                "capabilities": dict(self.capabilities), "renderer": "BLENDER_WORKBENCH"}

    def capture(self, tag):
        self.capabilities["capture_available"] = False
        self._prepare()
        result = self._invoke("capture")
        png = base64.b64decode(result["frame_png_base64"], validate=True) if result.get("frame_png_base64") else None
        observation = Observation.create(self.id, tag, result["state"], png, result.get("notes", []))
        self.capabilities["capture_available"] = observation.capture_available
        return observation

    def scene_state(self):
        self._prepare()
        return state_copy(self._invoke("state")["state"])

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
