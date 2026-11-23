"""Blender Workbench capture on a scratch copy; the source is never saved.

A declared empty environment and the existing owned-process context enforce
bounded lifetime and cleanup of the recorded process group. Render PNGs use
anonymous RAM files in the side script; only JSON evidence is written to scratch.
"""
import base64
import json
import hashlib
import re
import signal
import time
import os
from pathlib import Path
import shutil
import subprocess

from blender_visual_qa.drive.launch import declared_env, env_sha256
from blender_visual_qa.drive import containment, worker
from ..kernel_cache import KernelCache, log_lines
from ..contract import EnvAdapter, Observation
from ..canon import canonical_json, integer, state_copy

ROOT = Path(__file__).resolve().parents[2]
BLENDER = Path("/usr/bin/blender")


def validate_environment(values):
    if type(values) is not dict or any(type(k) is not str or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k)
                                      or type(v) is not str or "\x00" in v for k, v in values.items()):
        raise ValueError("invalid_blender_environment")
    # Interpreter injection, loader, display/X-authority, session/D-Bus/XDG, and profile overrides do not belong to a renderer allowlist.
    if any(k.startswith(("LD_", "PYTHON", "BLENDER_USER_", "BLENDER_SYSTEM_", "XDG_", "DBUS_"))
           or k in {"HOME", "PATH", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"}
           for k in values):
        raise ValueError("unsafe_blender_environment_key")
    return dict(values)


def environment_receipt(values):
    return {k: {"sha256": hashlib.sha256(v.encode()).hexdigest(),
                **({"path": v} if k.endswith(("_PATH", "_DIR")) and Path(v).is_absolute() else {})}
            for k, v in sorted(values.items())}


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
                 probe_width=8, probe_height=8, probe_samples=1, probe_timeout=300,
                 blender_env=None, nice=10, cpus=None, profile_session=False, capture_timeout=30, kernel_cache=None,
                 captive_cuda=False):
        if type(captive_cuda) is not bool or (captive_cuda and profile_session):
            raise ValueError("invalid_captive_cuda_profile")
        if kernel_cache is not None and isinstance(blender_env, dict) and "XDG_CACHE_HOME" in blender_env:
            raise ValueError("kernel_cache_conflicts_with_XDG_CACHE_HOME")
        self.blender_env = validate_environment(blender_env or {})
        self.environment_receipt = environment_receipt(self.blender_env)
        if type(nice) is not int or not 10 <= nice <= 19:
            raise ValueError("blender_nice_requires_10_to_19")
        if cpus is not None and (type(cpus) is not str or not re.fullmatch(r"[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*", cpus)):
            raise ValueError("invalid_blender_cpus")
        self.nice, self.cpus, self.profile_session = nice, cpus, profile_session
        self.captive_cuda = captive_cuda
        self._session = self._session_owner = self._session_log = self._session_stderr = None
        self.child_environment = {}
        self.gpu_probe_diagnostics = {}
        self._request_offsets = (0, 0)
        self.sample_cache = None
        self.contact_feet, self.floor_declaration = [], None
        self.profile_metadata = None
        self._profile_deadline = None
        self._capture_failure = None
        self._session_failed = None
        self.capture_timeout = integer(capture_timeout)
        if self.capture_timeout <= 0:
            raise ValueError("invalid_capture_timeout")
        self.source = Path(source) if source else ROOT / "examples/cube3.blend"
        if kernel_cache is not None and "XDG_CACHE_HOME" in self.blender_env:
            raise ValueError("kernel_cache_conflicts_with_XDG_CACHE_HOME")
        self._kernel_cache = KernelCache(kernel_cache, self.source) if kernel_cache is not None else None
        self.kernel_cache_receipt = None
        self._kernel_lines = []
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
            owner = containment.workspace(self._scratch_parent)
            self.scratch = owner.__enter__()
            self._tmp = owner
            try:
                self.blend = self.scratch / "scene.blend"
                shutil.copyfile(self.source, self.blend)
                initial = self._invoke("init")
                if self._kernel_cache is not None:
                    self._kernel_cache.bind(self.runtime_version,
                        self.blender_env.get("CYCLES_KERNEL_PATH") or initial["kernel_source_path"],
                        self.scratch, managed=True)
                    self.kernel_cache_receipt = self._kernel_cache.receipt()
            except Exception:
                self.close()
                raise
            self.capabilities.update(scene_state=True, act=True)

    def _redact(self, text):
        for key, value in self.blender_env.items():
            if value and "path" not in self.environment_receipt[key]:
                text = text.replace(value, "sha256:" + self.environment_receipt[key]["sha256"])
        return text

    def _tail(self, path, start=0, limit=4096):
        if not path.exists():
            return ""
        return self._redact(worker.read_output(path, offset=start, limit=limit).decode("utf-8", "replace"))

    def _invoke(self, mode, action=None, *, frames=None, paths=None, device_type=None):
        result, error = None, None
        cache = self._kernel_cache if self._kernel_cache and self._kernel_cache.directory is not None else None
        before_cache = cache.snapshot() if cache else None
        self._kernel_lines = []
        self._request_offsets = tuple(path.stat().st_size if path else 0
                                      for path in (self._session_log, self._session_stderr))
        try:
            result = self._invoke_request(mode, action, frames=frames, paths=paths, device_type=device_type)
            if "environment" in result:
                self.child_environment = result["environment"]
            return result
        except Exception as exc:
            error = type(exc).__name__ + ":" + str(exc)
            if getattr(exc, "reason", "").startswith("signal:"):
                raise RuntimeError("driver_killed:" + signal.Signals(int(exc.reason.split(":")[1])).name) from exc
            raise
        finally:
            cache_event = None
            if cache and mode in {"capture", "gpu_probe"}:
                if self.profile_session:
                    self.flush_process_logs()
                    for filename, offset in zip(("session.log", "session.stderr.log"), self._request_offsets):
                        path = self.scratch / filename
                        if path.exists():
                            self._kernel_lines.extend(log_lines(self._tail(path, offset, 12000)))
                            self._kernel_lines = self._kernel_lines[-64:]
                cache_event = cache.finish(mode, before_cache, self._kernel_lines, error is None)
                self.kernel_cache_receipt = cache.receipt()
            if mode == "gpu_probe":
                self.flush_process_logs()
                log = self.process_logs[-1] if self.process_logs else {}
                stdout = (self._tail(self.scratch / "session.log", self._request_offsets[0])
                          if self.profile_session and hasattr(self, "scratch") else log.get("output", "")[-4096:])
                stderr = (self._tail(self.scratch / "session.stderr.log", self._request_offsets[1])
                          if self.profile_session and hasattr(self, "scratch") else log.get("stderr_tail", "")[-4096:])
                combined = stdout + "\n" + stderr
                stage = ("loading_render_kernels" if "Loading render kernels" in combined else "render")
                self.gpu_probe_diagnostics[device_type] = {
                    "ok": bool(result and result.get("gpu_render_ok") is True),
                    "reason": self._redact(error) if error else ("gpu_render_completed" if result and result.get("gpu_render_ok") is True else "gpu_render_not_confirmed"),
                    "stage": stage, "stdout_tail": self._redact(stdout)[-4096:],
                    "stderr_tail": self._redact(stderr)[-4096:],
                    "declared_environment": self.environment_receipt,
                    "child_environment": self.child_environment,
                    "width": self.probe_width, "height": self.probe_height, "samples": self.probe_samples,
                    **({key: result[key] for key in ("gpu_artifact_sha256", "gpu_devices")}
                       if result and "gpu_artifact_sha256" in result else {}),
                    **({"kernel_cache": cache_event} if cache_event is not None else {})}

    def _invoke_request(self, mode, action=None, *, frames=None, paths=None, device_type=None):
        request = self.scratch / "request.json"
        response = self.scratch / "response.json"
        if mode == "act":
            validate_action(action)
        env = {**declared_env(self.scratch, 1), **validate_environment(self.blender_env)}
        payload = {"mode": mode, "action": action, "environment_keys": sorted(env),
                                          "width": self.width, "height": self.height,
                                          "frames": frames, "paths": paths, "device_type": device_type,
                                          "capture_engine": self.capture_engine, "capture_samples": self.capture_samples,
                                          "capture_denoising": self.capture_denoising,
                                          "color_mode": self.color_mode, "color_depth": self.color_depth,
                                          "max_sample_frames": self.max_sample_frames,
                                          "probe_width": self.probe_width, "probe_height": self.probe_height,
                                          "probe_samples": self.probe_samples,
                                          "session": self.profile_session,
                                          "kernel_cache_declared": self._kernel_cache is not None,
                                          "contact_feet": self.contact_feet, "floor": self.floor_declaration}
        if mode == "gpu_probe" and (device_type != "CUDA" or self.profile_session):
            raise containment.ContainmentError("gpu_backend_or_session_without_device_admission:" + str(device_type))
        devices = worker.cuda_device_nodes() if mode == "gpu_probe" else ()
        if mode == "gpu_probe" and self.captive_cuda:
            containment._check_captive_cuda(True, devices, None)
        worker.publish(request, canonical_json(payload).encode())
        response.unlink(missing_ok=True)
        argv = [str(BLENDER), "-b", "--factory-startup", "-noaudio", "--disable-autoexec",
                str(self.blend), "--python-exit-code", "9", "--python",
                str(Path(__file__).with_name("blender_side.py")), "--", str(request), str(response)]
        if self._kernel_cache is not None:
            argv[1:1] = ["--debug-cycles", "--verbose", "2"]
        argv = ["nice", "-n", str(max(0, self.nice-os.getpriority(os.PRIO_PROCESS, 0)))] + argv
        if self.cpus is not None:
            argv = ["taskset", "-c", self.cpus] + argv
        side_script = Path(__file__).with_name("blender_side.py")
        inputs = (request, side_script) if mode in {"init", "act"} else (request, side_script, self.blend)
        runtime = ()
        runtime_aliases = ()
        if "CYCLES_KERNEL_PATH" in env:
            directory = Path(env["CYCLES_KERNEL_PATH"]).resolve(strict=True)
            if directory.is_relative_to("/tmp"):
                target = Path("/run/blender-input/kernel-source")
                runtime_aliases = ((directory, target),)
                env["CYCLES_KERNEL_PATH"] = str(target)
            else:
                runtime = (directory,)
                env["CYCLES_KERNEL_PATH"] = str(directory)
        settings = dict(env=env, inputs=inputs, runtime=runtime, runtime_aliases=runtime_aliases, executable=BLENDER)
        if self.profile_session:
            return self._session_invoke(argv, settings, payload, response)
        if mode == "gpu_probe":
            (self.scratch / "gpu-probe.png").unlink(missing_ok=True)
            settings["devices"] = devices
            if self.captive_cuda:
                settings["captive_cuda"] = True
        budget = (min(self.timeout, 15 if self.capture_engine == "eevee" else self.capture_timeout) if mode == "capture"
                  else min(self.timeout, self.probe_timeout) if mode == "gpu_probe" else self.timeout)
        with worker.admitted_process(argv, self.scratch, timeout=budget, **settings) as (proc, stdout, stderr, evidence):
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
            try:
                proc.stdin.close()
                proc.wait(timeout=budget)
            except subprocess.TimeoutExpired as exc:
                self.process_logs.append({"mode": mode, "pid": proc.pid, "pgid": pgid,
                                          "timeout": True, "declared_environment": self.environment_receipt,
                                          "stderr_tail": self._tail(stderr), "output": self._tail(stdout, limit=12000),
                                          "containment": evidence})
                raise TimeoutError("driver_timeout:" + str(budget)) from exc
            worker.verify_outputs(self.scratch)
            text = self._tail(stdout, limit=12000)
            stderr_text = self._tail(stderr, limit=12000)
            self._kernel_lines = log_lines(text + "\n" + stderr_text)
            self.process_logs.append({"mode": mode, "pid": proc.pid, "pgid": pgid,
                                      "rc": proc.returncode, "env_sha256": env_sha256(env, self.scratch),
                                      "declared_environment": self.environment_receipt,
                                      "stderr_tail": stderr_text[-4096:], "output": text[-12000:],
                                      "containment": evidence})
            if proc.returncode < 0:
                raise RuntimeError("driver_killed:" + signal.Signals(-proc.returncode).name)
            if response.is_file():
                error = json.loads(worker.read_output(response))
                if "environment" in error:
                    self.child_environment = error["environment"]
                if error.get("ok") is False:
                    raise RuntimeError(error.get("error", "blender_side_failed"))
            if proc.returncode != 0 or any(line.lstrip().startswith(("Error:", "ERROR", "Traceback")) for line in (text + "\n" + stderr_text).splitlines()):
                raise RuntimeError("blender_process_or_log_error:" + str(proc.returncode))
        result = json.loads(worker.read_output(response))
        if result.get("ok") is not True:
            raise RuntimeError("blender_side_failed")
        if mode == "gpu_probe":
            from blender_visual_qa.drive.png import decode
            artifact = worker.read_output(self.scratch / "gpu-probe.png")
            devices = result.get("gpu_devices")
            if (tuple(decode(artifact)[:2]) != (self.probe_width, self.probe_height) or not devices
                    or (self.captive_cuda and devices != [containment.CUDA_DEVICE])):
                raise RuntimeError("gpu_artifact_or_device_unverified")
            result["gpu_artifact_sha256"] = hashlib.sha256(artifact).hexdigest()
        self.runtime_version = result["runtime_version"]
        return result

    def _session_invoke(self, argv, settings, payload, response):
        mode = payload["mode"]
        if self._session_failed:
            raise RuntimeError(self._session_failed)
        call_started = time.monotonic()
        if self._profile_deadline is None:
            self._profile_deadline = call_started + self.timeout
        if self._session is None:
            logs = (self.scratch / "session.log", self.scratch / "session.stderr.log")
            owner = worker.admitted_process(argv, self.scratch, timeout=self.timeout, logs=logs, **settings)
            self._session, self._session_log, self._session_stderr, evidence = owner.__enter__()
            self._session_owner = owner
            self.process_logs.append({"mode": "profile_session", "pid": self._session.pid,
                                      "pgid": os.getpgid(self._session.pid), "env_sha256": env_sha256(settings["env"], self.scratch),
                                      "containment": evidence,
                                      "declared_environment": self.environment_receipt, "nice": self.nice, "cpus": self.cpus})
        else:
            rc = self._session.poll()
            if rc is not None:
                if rc < 0:
                    raise RuntimeError("driver_killed:" + signal.Signals(-rc).name)
                raise RuntimeError("driver_exited:" + str(rc))
            try:
                self._session.stdin.write((canonical_json(payload) + "\n").encode())
                self._session.stdin.flush()
            except BrokenPipeError as exc:
                rc = self._session.wait(timeout=1)
                reason = "driver_killed:" + signal.Signals(-rc).name if rc < 0 else "driver_exited:" + str(rc)
                raise RuntimeError(reason) from exc
        budget = min(self.timeout, self.probe_timeout) if mode == "gpu_probe" else self.timeout
        if mode == "capture":
            budget = min(budget, 15 if self.capture_engine == "eevee" else self.capture_timeout)
        deadline = min(time.monotonic() + budget, self._profile_deadline)
        while not response.is_file():
            rc = self._session.poll()
            if rc is not None:
                if rc < 0:
                    raise RuntimeError("driver_killed:" + signal.Signals(-rc).name)
                raise RuntimeError("driver_exited:" + str(rc))
            if time.monotonic() >= deadline:
                self._session_failed = "driver_timeout:" + str(budget)
                try:
                    self._session_owner.__exit__(None, None, None)
                except BrokenPipeError:
                    pass
                self._session_owner = None
                self.process_logs[0]["rc"] = self._session.returncode
                self.process_logs[0]["failed_mode"] = mode
                raise TimeoutError(self._session_failed)
            time.sleep(0.01)
        result = json.loads(worker.read_output(response))
        if "environment" in result:
            self.child_environment = result["environment"]
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "blender_side_failed"))
        self.process_logs[0]["nice"] = os.getpriority(os.PRIO_PROCESS, self._session.pid)
        self.process_logs[0]["affinity"] = sorted(os.sched_getaffinity(self._session.pid))
        self.process_logs[0].setdefault("requests", []).append({"mode": mode, "wall_ms": round((time.monotonic()-call_started)*1000)})
        self.runtime_version = result["runtime_version"]
        if "profile_metadata" in result:
            self.profile_metadata = result["profile_metadata"]
            self.profile_metadata["source_sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        return result

    def probe(self, render=False):
        reason = None
        try:
            self._prepare()
            observation = None if self.profile_session else self.capture("probe")
            if observation is not None and not observation.capture_available:
                raise RuntimeError("capture_unavailable")
        except Exception as exc:
            reason = type(exc).__name__ + ":" + str(exc)
            self.capabilities["capture_available"] = False
        report = {"id": self.id, "version": self.version, "status": self.status,
                "available": reason is None, "reason": reason, "runtime_version": self.runtime_version,
                "declared_environment": self.environment_receipt, "capabilities": dict(self.capabilities), "renderer": {"workbench": "BLENDER_WORKBENCH", "eevee": "BLENDER_EEVEE", "cycles": "CYCLES"}[self.capture_engine]}
        if render:
            report.update(cycles_devices={}, denoisers=[], view_transforms=[], looks={},
                          engines=[], gpu_render_ok=False, gpu_render_stderr_tail="", gpu_render_ok_by_type={})
            try:
                self._prepare()
                report.update(self._invoke("render_caps")["render_capabilities"])
                for device_type, names in report["cycles_devices"].items():
                    if self.profile_session or device_type == "CPU" or not names:
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
        if self.kernel_cache_receipt is not None:
            report["kernel_cache"] = self.kernel_cache_receipt
        report["gpu_probe"] = self.gpu_probe_diagnostics
        report["gpu_render_stderr_tail"] = "\n".join(
            d["stderr_tail"] or d["stdout_tail"] for d in self.gpu_probe_diagnostics.values())[-4096:] or report.get("gpu_render_stderr_tail", "")
        return report

    def gpu_probe(self, device_type):
        """Run a one-shot diagnostic, with captive admission only when explicitly configured."""
        try:
            if self.captive_cuda and device_type == "CUDA" and not self.profile_session:
                self._prepare()
            return self._invoke("gpu_probe", device_type=device_type).get("gpu_render_ok") is True
        except Exception:
            return False

    def capture(self, tag):
        if self._capture_failure:
            raise RuntimeError(self._capture_failure)
        self.capabilities["capture_available"] = False
        self._prepare()
        try:
            result = self._invoke("capture")
        except (TimeoutError, RuntimeError) as exc:
            if self.capture_engine == "eevee":
                self._capture_failure = "eevee_probe_timeout:15" if isinstance(exc, TimeoutError) else "eevee_unavailable:" + str(exc)
                raise RuntimeError(self._capture_failure) from exc
            raise
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
        if self.sample_cache is not None:
            return {str(f): {p: self.sample_cache[str(f)][p] for p in paths if p in self.sample_cache[str(f)]} for f in frames}
        return self._invoke("sample", frames=frames, paths=list(dict.fromkeys(paths + getattr(self, "contact_floor_paths", []))))["samples"]

    def act(self, action):
        validate_action(action)
        self._prepare()
        result = self._invoke("act", action)
        return {"ok": result["ok"], "op": action["op"]}

    def flush_process_logs(self):
        if self._session_log is not None:
            self.process_logs[0]["output"] = self._tail(self.scratch / "session.log", limit=12000)
            self.process_logs[0]["stderr_tail"] = self._tail(self.scratch / "session.stderr.log")

    def close(self):
        if self._session_owner is not None:
            try:
                if self._session.poll() is None:
                    self._session.stdin.close()
                    self._session.wait(timeout=1)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                pass
            try:
                self._session_owner.__exit__(None, None, None)
            except BrokenPipeError:
                pass  # A typed dead-driver result already owns the failed exchange.
            self.process_logs[0]["rc"] = self._session.returncode
            self.process_logs[0]["reaped"] = self._session.poll() is not None
            self._session_owner = self._session = None
        if self._session_log is not None:
            self.flush_process_logs()
            self._session_log = self._session_stderr = None
        if self._tmp is not None:
            self._tmp.__exit__(None, None, None)
            self._tmp = None
        self._closed = True
        self.capabilities = {key: False for key in self.capabilities}
