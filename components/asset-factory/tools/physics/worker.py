"""Owned headless MuJoCo subprocess. No viewer, GL, external XML or downloads."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import math
from pathlib import Path
import resource
import sys
import time

from asset_factory import core
from .contracts import MAX_INPUT_BYTES, Refusal, require, validate_scene, validate_protocol
from .mjcf import write_mjcf

RUNTIME_VERSION = "4.0.1"  # [CITED] catalogue pin; local qualification is separate.


def fixed(value, scale=1000000):
    value = float(value)
    require(math.isfinite(value), 5, "nonfinite_state")
    return round(value * scale)


def collect(job):
    scene, protocol = job["scene"], job["protocol"]
    validate_scene(scene)
    validate_protocol(protocol)
    require(job["dt_us"] in (protocol["timestep_us"], protocol["timestep_us"] // 2))
    require(job["steps"] * job["dt_us"] == protocol["steps"] * protocol["timestep_us"])
    try:
        mj = importlib.import_module("mujoco")
    except ImportError as exc:
        raise Refusal(3, "mujoco_unavailable") from exc
    require(mj.__version__ == RUNTIME_VERSION, 4, "runtime_version_stale")
    import numpy as np  # MuJoCo dependency, confined to the runtime worker.
    xml = write_mjcf(scene, protocol, timestep_us=job["dt_us"])
    model = mj.MjModel.from_xml_string(xml)
    data = mj.MjData(model)
    dynamic = [b for b in scene["bodies"] if b["dynamic"]]
    ids = [mj.mj_name2id(model, mj.mjtObj.mjOBJ_BODY, b["id"]) for b in dynamic]
    admitted = []
    for body, bid in zip(dynamic, ids):
        j = model.body_jntadr[bid]
        adr = model.jnt_dofadr[j]
        data.qvel[adr:adr+3] = np.array(body["velocity_um_s"]) / 1000000
        data.qvel[adr+3:adr+6] = np.array(body["angular_velocity_urad_s"]) / 1000000
        rotation = np.empty(9)
        mj.mju_quat2Mat(rotation, model.body_iquat[bid])
        rotation = rotation.reshape(3, 3)
        tensor = rotation @ np.diag(model.body_inertia[bid]) @ rotation.T
        admitted.append({"body": body["id"], "mass_ug": fixed(model.body_mass[bid], 10**9),
                         "com_um": [fixed(x) for x in model.body_ipos[bid]],
                         "tensor_ug_um2": [fixed(tensor[i, j], 10**21)
                                           for i, j in ((0,0),(1,1),(2,2),(0,1),(0,2),(1,2))]})
    start, samples = time.monotonic_ns(), []
    for step in range(job["steps"] + 1):
        require(time.monotonic_ns()-start <= protocol["timeout_s"]*10**9, 6, "deadline_exceeded")
        # Recompute derived positions and contacts after integration, at this sample's time.
        mj.mj_forward(model, data)
        require(data.ncon <= protocol["contact"]["max_contacts"], 6, "contact_budget_exceeded")
        require(not any(w.number for w in data.warning), 5, "solver_warning")
        contacts = []
        for ci in range(data.ncon):
            contact, force = data.contact[ci], np.empty(6)
            mj.mj_contactForce(model, data, ci, force)
            contacts.append({"geom1": mj.mj_id2name(model, mj.mjtObj.mjOBJ_GEOM, contact.geom1),
                             "geom2": mj.mj_id2name(model, mj.mjtObj.mjOBJ_GEOM, contact.geom2),
                             "distance_um": fixed(contact.dist), "normal_force_uN": fixed(force[0])})
        states = []
        for body, bid in zip(dynamic, ids):
            q = [fixed(x) for x in data.xquat[bid]]
            if next((x for x in q if x), 0) < 0:
                q = [-x for x in q]
            states.append({"body": body["id"], "position_um": [fixed(x) for x in data.xpos[bid]],
                           "quaternion_ppm": q})
        samples.append({"step": step, "time_us": step*job["dt_us"], "states": states,
                        "contacts": sorted(contacts, key=core.canonical_json)})
        if step < job["steps"]:
            mj.mj_step(model, data)
    module_path = Path(mj.__file__)
    return {"schema": "af.physics-trace/v1", "run_id": job["run_id"],
            "scene_sha256": core.sha256_json(scene), "protocol_sha256": core.sha256_json(protocol),
            "model_sha256": hashlib.sha256(xml.encode()).hexdigest(), "timestep_us": job["dt_us"],
            "runtime": {"name": "mujoco", "version": mj.__version__,
                        "module_sha256": core.sha256_file(module_path),
                        "native_libraries": [{"name": p.name, "sha256": core.sha256_file(p)}
                                             for p in sorted(module_path.parent.glob("libmujoco.so*"))]},
            "admitted_inertia": admitted, "samples": samples,
            "wall_ms": (time.monotonic_ns()-start)//1000000,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--trace", required=True)
    args = parser.parse_args(argv)
    try:
        result = collect(core.load_json(Path(args.job), max_bytes=MAX_INPUT_BYTES))
        core.atomic_write_json(Path(args.trace), result)
        return 0
    except Refusal as exc:
        return core.emit({"rc": exc.rc, "status": core.STATUS[exc.rc], "reason_codes": [exc.code]})
    except MemoryError:
        return core.emit({"rc": 6, "status": "BUDGET_EXCEEDED", "reason_codes": ["memory_budget_exceeded"]})
    except (core.ContractError, OSError, ValueError, TypeError, KeyError,
            RuntimeError, AttributeError, OverflowError) as exc:
        return core.emit({"rc": 2, "status": "ERROR", "reason_codes": ["runtime_contract_error"],
                          "detail": type(exc).__name__})


if __name__ == "__main__":
    sys.exit(main())
