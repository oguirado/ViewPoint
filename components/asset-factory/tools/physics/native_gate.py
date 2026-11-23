"""Offline native qualification harness; retained observations, never asset generation.

Run with the caller's MuJoCo interpreter. Every native child is memory capped,
CPU pinned and bounded. A successful harness requires positive measurements and
specific red results followed by a restored positive. Production admission and
physical calibration of a visual mesh are separate from these analytic fixtures.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from asset_factory import core
from . import api, gates
from .contracts import Refusal, require
from .fixtures import fall_fixture
from .mjcf import write_mjcf


def check_control(name, observed, restored, expected_rc, expected_reason):
    """A green mutation, wrong refusal or incomplete repair never passes a control."""
    require(expected_rc in (2, 3, 4, 5, 6) and type(expected_rc) is int,
            2, "control_expectation_invalid")
    require(type(observed.get("rc")) is int and observed["rc"] == expected_rc
            and observed.get("reason_codes") == [expected_reason]
            and observed.get("verdict") == core.VERDICT[expected_rc],
            5, "negative_control_failed")
    require(type(restored.get("rc")) is int and restored["rc"] == 0
            and restored.get("verdict") == "PASS" and restored.get("reason_codes") == [],
            5, "restoration_failed")
    return {"name": name, "expected_rc": expected_rc, "observed_rc": observed["rc"],
            "expected_reason": expected_reason, "observed_reasons": observed["reason_codes"],
            "restored_rc": restored["rc"]}


def _observe(root, name, scene, protocol, deadline):
    directory = root / name
    directory.mkdir()
    core.atomic_write_json(directory / "scene.json", scene)
    core.atomic_write_json(directory / "protocol.json", protocol)
    traces, nonces, models = [], [], []
    for index, factor in enumerate((1, 2, 1)):
        dt, nonce = protocol["timestep_us"] // factor, secrets.token_hex(16)
        job_path, trace_path = directory / f"job-{index}.json", directory / f"trace-{index}.json"
        core.atomic_write_json(job_path, {"scene": scene, "protocol": protocol, "dt_us": dt,
                                         "steps": protocol["steps"] * factor, "run_id": nonce})
        xml = write_mjcf(scene, protocol, timestep_us=dt)
        (directory / f"model-{index}.xml").write_text(xml, encoding="utf-8")
        remaining = min(protocol["timeout_s"], deadline - time.monotonic())
        require(remaining > 0, 6, "deadline_exceeded")
        argv = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G",
                "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0", "taskset", "-c", "0-15",
                "nice", "-n", "15", sys.executable, "-B", "-m", "tools.physics.worker",
                "--job", str(job_path), "--trace", str(trace_path)]
        rc, stdout = api.run_child(argv, timeout_s=remaining, cwd=api.COMPONENT_ROOT)
        if rc != 0:
            require(rc in (2, 3, 4, 5, 6), 2, "worker_contract_invalid")
            # Worker stdout is bounded data; preserve its explicit refusal code.
            import json
            try:
                refusal = json.loads(stdout)
                require(type(refusal["rc"]) is int and refusal["rc"] == rc
                        and type(refusal["reason_codes"]) is list
                        and refusal["reason_codes"] and type(refusal["reason_codes"][0]) is str,
                        2, "worker_contract_invalid")
            except (ValueError, KeyError, TypeError):
                raise Refusal(2, "worker_contract_invalid") from None
            raise Refusal(rc, refusal["reason_codes"][0])
        require(trace_path.is_file(), 3, "trace_missing")
        traces.append(core.load_json(trace_path, max_bytes=api.MAX_TRACE_BYTES))
        nonces.append(nonce)
        models.append(hashlib.sha256(xml.encode("utf-8")).hexdigest())
    receipt = gates.evaluate(scene, protocol, traces, run_ids=nonces, model_sha256s=models)
    core.atomic_write_json(directory / "receipt.json", receipt)
    case = {"name": name, "rc": receipt["rc"], "reason_codes": receipt["reason_codes"],
            "metrics": receipt["metrics"], "sample_counts": [len(t["samples"]) for t in traces],
            "max_penetration_um": max([0] + [-c["distance_um"] for t in traces
                for sample in t["samples"] for c in sample["contacts"]]),
            "peak_rss_bytes": max(t["peak_rss_bytes"] for t in traces),
            "receipt_sha256": core.sha256_json(receipt)}
    return receipt, case, traces, nonces, models


def _primitive_scene(scene, protocol):
    """Bounded compile/readback fixture; all dimensions and inertias are analytic."""
    scene, protocol = deepcopy(scene), deepcopy(protocol)
    template = scene["bodies"][1]
    half = 100000  # [TARGET] fixture half-size, micrometres
    vertices = [[x, y, z] for x, y, z in
                ((-half,-half,-half),(half,-half,-half),(half,half,-half),(-half,half,-half),
                 (-half,-half,half),(half,-half,half),(half,half,half),(-half,half,half))]
    faces = [[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],
             [1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]]
    for index, (shape, data, tensor) in enumerate((
        ("sphere", {"radius_um": half}, [4000000000000000000]*3 + [0,0,0]),
        ("cylinder", {"radius_um": half, "half_height_um": half},
         [5833333333333333333,5833333333333333333,5000000000000000000,0,0,0]),
        ("convex_hull", {"vertices_um": vertices, "faces": faces},
         template["inertial"]["tensor_ug_um2"]),
        # Rotated anisotropic tensor: principal values 4, 6, 8 in scaled units.
        ("box", {"half_extents_um": [half]*3},
         [5000000000000000000,5000000000000000000,8000000000000000000,
          1000000000000000000,0,0]),
    )):
        body = deepcopy(template)
        body["id"] = "fixture_" + str(index)
        body["position_um"][0] = (index + 1) * 500000
        body["inertial"]["tensor_ug_um2"] = list(tensor)
        if index == 3:
            body["inertial"]["com_um"] = [1000,2000,3000]
        body["colliders"][0].update(id="geom_" + str(index), shape=shape, data=data)
        scene["bodies"].append(body)
    protocol["steps"] = protocol["free_fall"]["steps"] = 2  # [TARGET] readback fixture
    protocol["contact"]["require_contact"] = False
    return scene, protocol


def run(output_dir):
    start = time.monotonic_ns()
    try:
        target = Path(output_dir).absolute()
        require(not target.exists() and not target.is_symlink(), 2, "output_exists")
        require(target.parent.is_dir(), 2, "output_parent_missing")
        deadline = time.monotonic() + 240  # [TARGET] total harness wall budget
        with tempfile.TemporaryDirectory(prefix=".native-physics-", dir=target.parent) as temp:
            root = Path(temp)
            source = root / "analytic-source.json"
            core.atomic_write_json(source, {"fixture": "analytic rigid-body qualification", "generated_asset": False})
            scene, protocol = fall_fixture({"path": source.name, "sha256": core.sha256_file(source),
                                            "media_type": "application/json"})
            positive, case, traces, nonces, models = _observe(root, "analytic", scene, protocol, deadline)
            require(positive["rc"] == 0, positive["rc"],
                    positive["reason_codes"][0] if positive["reason_codes"] else "positive_failed")
            cases, controls = [case], []

            def restored():
                return gates.evaluate(scene, protocol, traces, run_ids=nonces, model_sha256s=models)

            for name, rc, reason in (("soft_contact",5,"penetration_exceeded"),
                                      ("overlap",5,"initial_intersection"),
                                      ("wrong_gravity",5,"wrong_gravity")):
                bad_scene, bad_protocol = deepcopy(scene), deepcopy(protocol)
                if name == "soft_contact":
                    bad_protocol["contact"]["timeconst_us"] = 20000  # [TARGET] planted soft contact
                elif name == "overlap":
                    bad_scene["bodies"][1]["position_um"][2] = 50000
                else:
                    bad_protocol["gravity_um_s2"] = [0,0,0]
                observed, case, *_ = _observe(root, name, bad_scene, bad_protocol, deadline)
                cases.append(case)
                controls.append(check_control(name, observed, restored(), rc, reason))

            # Explicit trace mutations test oracles on observations from a real solver.
            mutations = (
                ("reused_identity",4,"trace_binding_stale"),
                ("missing_sample",3,"trace_coverage_incomplete"),
                ("mass_units",5,"compiled_mass_mismatch"),
                ("tensor_units",5,"compiled_inertia_mismatch"),
                ("com_shift",5,"compiled_com_mismatch"),
                ("replay_damage",5,"replay_nondeterministic"),
                ("trajectory_damage",5,"timestep_diverged"),
                ("missing_contacts",5,"required_contact_absent"),
                ("frozen_motion",5,"analytic_fall_failed"),
            )
            for name, rc, reason in mutations:
                mutated = deepcopy(traces)
                if name == "reused_identity": mutated[0]["run_id"] = "old-run"
                elif name == "missing_sample": mutated[0]["samples"].pop()
                elif name == "mass_units": mutated[0]["admitted_inertia"][0]["mass_ug"] *= 1000
                elif name == "tensor_units": mutated[0]["admitted_inertia"][0]["tensor_ug_um2"][0] *= 1000
                elif name == "com_shift": mutated[0]["admitted_inertia"][0]["com_um"][0] += 1
                elif name == "replay_damage": mutated[2]["samples"][20]["states"][0]["position_um"][0] += 1
                elif name == "trajectory_damage": mutated[1]["samples"][40]["states"][0]["position_um"][0] += 20000
                else:
                    for trace in mutated:
                        for sample in trace["samples"]:
                            if name == "missing_contacts": sample["contacts"] = []
                            else: sample["states"][0]["position_um"] = scene["bodies"][1]["position_um"][:]
                observed = gates.evaluate(scene, protocol, mutated, run_ids=nonces, model_sha256s=models)
                controls.append(check_control(name, observed, restored(), rc, reason))
                core.atomic_write_json(root / (name + ".receipt.json"), observed)
            primitive_scene, primitive_protocol = _primitive_scene(scene, protocol)
            primitive, case, *_ = _observe(root, "compiled_primitives", primitive_scene, primitive_protocol, deadline)
            require(primitive["rc"] == 0, primitive["rc"],
                    primitive["reason_codes"][0] if primitive["reason_codes"] else "primitive_failed")
            cases.append(case)
            # Exercise the production tool's authentication and atomic publication too.
            published = api.qualification(root/"analytic/scene.json", root/"analytic/protocol.json",
                                          evidence_root=root, output_dir=root/"published")
            require(published["rc"] == 0, published["rc"],
                    published["reason_codes"][0] if published["reason_codes"] else "publication_failed")
            report = {"schema":"af.physics-native-gate/v1", "rc":0, "verdict":"PASS",
                      "scope":"native_analytic_fixtures_only", "threshold_status":"TARGET_UNCALIBRATED",
                      "checks_evaluated": len(controls) + 3, "controls":controls, "cases":cases,
                      "wall_ms":(time.monotonic_ns()-start)//1000000,
                      "peak_rss_bytes":max(c["peak_rss_bytes"] for c in cases)}
            schema = core.load_json(Path(__file__).with_name("native-gate.schema.json"), max_bytes=65536)
            core.validate(report, schema)
            core.atomic_write_json(root/"summary.json", report)
            api._publish_directory(root, target)
            return report
    except subprocess.TimeoutExpired:
        rc, code = 6, "deadline_exceeded"
    except Refusal as exc:
        rc, code = exc.rc, exc.code
    except (core.ContractError, OSError, ValueError, KeyError, TypeError):
        rc, code = 2, "native_gate_invalid"
    return {"rc":rc, "status":core.STATUS[rc], "verdict":core.VERDICT[rc], "reason_codes":[code]}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    return core.emit(run(args.out))


if __name__ == "__main__":
    sys.exit(main())
