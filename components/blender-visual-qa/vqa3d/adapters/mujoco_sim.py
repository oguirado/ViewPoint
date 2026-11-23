"""MuJoCo simulation, not physical reality. numpy is confined to this adapter.

Replay determinism claims concern the same model/actions and tested runtime.
Renderer availability is measured on capture, separately from physics support.
"""
import importlib
import os
from pathlib import Path
import sys
import threading

from blender_visual_qa.drive.png import encode
from ..canon import integer, quantise, state_copy
from ..contract import EnvAdapter, Observation

DEFAULT_MJCF = '''<mujoco model="vqa3d simulation">
<option timestep="0.002" gravity="0 0 -9.81"/>
<visual><global offwidth="96" offheight="72"/></visual>
<worldbody><light pos="0 -2 4"/><geom name="plane" type="plane" size="3 3 .1" rgba=".3 .4 .3 1"/>
<body name="box" pos="0 0 .5"><freejoint name="box_free"/>
<geom name="box_geom" type="box" size=".15 .15 .15" mass="1" rgba=".8 .2 .1 1"/></body>
<body name="hinge_body" pos=".6 0 .6"><joint name="hinge" type="hinge" axis="0 1 0"/>
<geom name="arm_geom" type="capsule" fromto="0 0 0 0 0 .5" size=".05" mass=".2" rgba=".2 .3 .8 1"/></body>
</worldbody><actuator><motor name="hinge_motor" joint="hinge" gear="1"/></actuator></mujoco>'''


class MuJoCoSimulationAdapter(EnvAdapter):
    id = "mujoco"
    version = "1"
    status = "implemented"

    def __init__(self, mjcf=None, *, width=96, height=72):
        self.width, self.height = integer(width), integer(height)
        if not 1 <= width <= 96 or not 1 <= height <= 72:
            raise ValueError("invalid_capture_dimensions")
        self.mjcf = mjcf
        self.model = self.data = self.renderer = self.mujoco = None
        self._closed = False
        self._render_error = None
        self._lock = threading.RLock()
        self.capabilities = {"capture_available": False, "scene_state": False, "act": False}
        self.backend = None

    def _ensure(self):
        if self._closed:
            raise RuntimeError("adapter_closed")
        if self.model is None:
            # Default before import, preserving an explicit caller selection.
            self.backend = os.environ.setdefault("MUJOCO_GL", "egl")
            loaded = sys.modules.get("mujoco")
            context_module = getattr(getattr(loaded, "GLContext", None), "__module__", "")
            if context_module in ("mujoco.egl", "mujoco.glfw", "mujoco.osmesa"):
                self.backend = context_module.rsplit(".", 1)[-1]
            if self.backend != "egl" or os.environ["MUJOCO_GL"] != "egl":
                raise RuntimeError("unsupported_MUJOCO_GL:" + self.backend)
            self.mujoco = importlib.import_module("mujoco")
            if self.mjcf is None:
                model = self.mujoco.MjModel.from_xml_string(DEFAULT_MJCF)
            elif isinstance(self.mjcf, Path) or not str(self.mjcf).lstrip().startswith("<"):
                model = self.mujoco.MjModel.from_xml_path(str(self.mjcf))
            else:
                model = self.mujoco.MjModel.from_xml_string(self.mjcf)
            data = self.mujoco.MjData(model)
            self.mujoco.mj_forward(model, data)
            self.model, self.data = model, data
            self.capabilities.update(scene_state=True, act=True)

    def probe(self, render=False):
        try:
            with self._lock:
                self._ensure()
                observation = self.capture("probe")
            return {"id": self.id, "version": self.version, "status": self.status,
                    "available": observation.capture_available, "runtime_version": self.mujoco.__version__,
                    "backend": self.backend,
                    "reason": None if observation.capture_available else ";".join(observation.notes),
                    "environment": "MuJoCo simulation", "capabilities": dict(self.capabilities),
                    "notes": list(observation.notes)}
        except Exception as exc:
            self.capabilities = {key: False for key in self.capabilities}
            return {"id": self.id, "version": self.version, "status": self.status,
                    "available": False, "environment": "MuJoCo simulation",
                    "backend": self.backend,
                    "reason": type(exc).__name__ + ":" + str(exc),
                    "capabilities": dict(self.capabilities)}

    def scene_state(self):
        with self._lock:
            self._ensure()
            # numpy scalar conversion is explicit; no ndarray or float escapes.
            contacts = sorted(sorted([int(c.geom1), int(c.geom2)]) for c in self.data.contact[:self.data.ncon])
            return state_copy({"time_us": quantise(float(self.data.time), 1000000),
                               "qpos": [quantise(float(x), 1000000) for x in self.data.qpos],
                               "qvel": [quantise(float(x), 1000000) for x in self.data.qvel],
                               "ctrl_milli": [quantise(float(x), 1000) for x in self.data.ctrl],
                               "ncon": int(self.data.ncon), "contact_geom_pairs": contacts})

    def capture(self, tag):
        with self._lock:
            self._ensure()
            state = self.scene_state()
            png = None
            # A render failure is sticky for this instance: no implicit retry.
            if self._render_error is None:
                try:
                    if self.renderer is None:
                        self.renderer = self.mujoco.Renderer(self.model, height=self.height, width=self.width)
                    self.renderer.update_scene(self.data)
                    rgb = self.renderer.render()
                    png = encode(self.width, self.height, rgb.tobytes())
                except Exception as exc:
                    self._render_error = type(exc).__name__ + ":" + str(exc)
                    try:
                        if self.renderer is not None:
                            self.renderer.close()
                    except Exception as close_exc:
                        self._render_error += ";renderer_close:" + str(close_exc)
                    finally:
                        self.renderer = None
            self.capabilities["capture_available"] = png is not None
            return Observation.create(self.id, tag, state, png,
                                      [] if png is not None else ["capture_unavailable:" + self._render_error])

    def act(self, action):
        with self._lock:
            self._ensure()
            if type(action) is not dict:
                raise ValueError("action_must_be_object")
            op = action.get("op")
            if op == "set_ctrl":
                index, value = integer(action["index"]), integer(action["value_milli"])
                if not 0 <= index < self.model.nu:
                    raise ValueError("control_index_out_of_range")
                self.data.ctrl[index] = value / 1000
            elif op == "step":
                n = integer(action["n"])
                if not 1 <= n <= 10000:
                    raise ValueError("step_count_out_of_range")
                for _ in range(n):
                    self.mujoco.mj_step(self.model, self.data)
                self.mujoco.mj_forward(self.model, self.data)
            else:
                raise ValueError("unsupported_simulation_action")
            return {"ok": True, "op": op}

    def close(self):
        with self._lock:
            try:
                if self.renderer is not None:
                    self.renderer.close()
            finally:
                self.renderer = self.data = self.model = None
                self._closed = True
                self.capabilities = {key: False for key in self.capabilities}
