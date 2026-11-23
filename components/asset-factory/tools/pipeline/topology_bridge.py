"""Pipeline topology orchestration using the landed worker and geometry gates.

The legacy topology consumer expects list-valued absence evidence. The native
mesh contract requires an object. Authenticate that contract first; adapt only
the in-memory view passed to the legacy gate, retaining native receipts intact.
No injected comparator or test-only evaluator can authorize this boundary.
"""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import time

from asset_factory import core
from tools.blender import topology
from tools.blender.bake_support import read_bytes
from tools.blender.topology_glb import TopologyError, inspect_glb
from tools.mesh import api as mesh, contracts
from .engine import Refusal, file_ref, require


def verify_regression(rc, receipt, raw, candidate, policy):
    try:
        contracts.validate_receipt(receipt)
        require(type(rc) is int and rc in core.RC and receipt["rc"] == rc,
                2, "regression_contract")
        bindings = [("input_sha256", file_ref(raw)["sha256"]),
                    ("policy_sha256", core.sha256_json(policy)),
                    ("toolchain_sha256", mesh.toolchain_identity())]
        # A refused repair has no published candidate bytes to authenticate.
        # Keep its native refusal after validating all available input bindings.
        require(rc != 0 or candidate is not None, 3, "regression_candidate_absent")
        if candidate is not None:
            bindings.append(("candidate_sha256", file_ref(candidate)["sha256"]))
        for key, expected in bindings:
            if rc == 0 or receipt[key] is not None:
                require(receipt[key] == expected, 4, "regression_binding_stale")
        if rc:
            reasons = receipt["failed"] or list(receipt["unavailable"].values())
            raise Refusal(rc, (reasons or ["regression_refused"])[0])
        contracts.validate_success(receipt, "compare")
        # This is representation adaptation, never absence suppression.
        view = copy.deepcopy(receipt)
        view["unavailable"] = sorted(receipt["unavailable"])
        topology._gate_regression(rc, view, file_ref(raw)["sha256"],
            file_ref(candidate)["sha256"], {"mesh_policy": policy,
                "silhouette_iou_min_ppm": contracts.IOU_MIN_PPM,
                "hausdorff_max_ppm": contracts.DISTANCE_MAX_PPM})
    except Exception as exc:
        refusal = exc if isinstance(exc, Refusal) else Refusal(
            getattr(exc, "rc", 2), getattr(exc, "reason", getattr(exc, "code", "regression_contract")))
        refusal.evidence_record = receipt
        raise refusal
    return receipt


def _quad_toolchain(executable):
    host = Path(topology.__file__)
    return core.sha256_json({
        "blender_sha256": topology._file_hash(Path(executable)),
        "worker_sha256": topology._file_hash(host.with_name("topology_worker.py")),
        "host_sha256": topology._file_hash(host),
        "inventory_sha256": topology._file_hash(host.with_name("topology_glb.py")),
        "bridge_sha256": topology._file_hash(Path(__file__)),
        "kernel_sha256": topology._file_hash(Path(core.__file__)),
    })


def _check_quad_cage_custody(raw, candidate, receipt, *, target_faces):
    """Check producer custody before an independent GEO proof is available."""
    try:
        require(type(receipt) is dict and receipt.get("method") == "quadriflow"
                and type(receipt.get("rc")) is int and receipt["rc"] == 0, 2, "topology_receipt_invalid")
        checks = receipt.get("checks")
        require(type(checks) is list and len(checks) == 1 and type(checks[0]) is dict
                and checks[0].get("role") == "master", 2, "topology_receipt_invalid")
        check = checks[0]
        cage_sha = check.get("quad_cage_sha256")
        require(cage_sha is not None and check.get("quad_cage_path") is not None,
                3, "required_quad_cage_missing")
        candidate = Path(candidate)
        require(check["quad_cage_path"] == candidate.with_suffix(".cage.json").name, 4, "quad_cage_path_stale")
        _, quads = topology.read_quad_cage(candidate, expected_sha256=cage_sha)
        raw_ref = file_ref(raw)
        require(receipt.get("raw_sha256") == raw_ref["sha256"]
                and file_ref(candidate.with_name("raw.glb"))["sha256"] == raw_ref["sha256"],
                4, "quad_cage_raw_stale")
        actual = inspect_glb(candidate)
        require(receipt.get("candidate_sha256") == check.get("candidate_sha256") == actual["sha256"],
                4, "quad_cage_candidate_stale")
        require(all(type(value) is int for value in
                    (target_faces, check.get("requested_faces"), check.get("achieved_faces")))
                and target_faces == check["requested_faces"]
                and 0 < 2 * quads == actual["faces"] == check.get("achieved_faces") <= target_faces,
                4, "quad_cage_face_counts_stale")
        executable = shutil.which("blender")
        require(executable is not None, 3, "blender_unavailable")
        require(receipt.get("toolchain_sha256") == _quad_toolchain(executable), 4, "quad_cage_toolchain_stale")
    except TopologyError as exc:
        raise Refusal(exc.rc, exc.reason) from exc
    return cage_sha, quads, actual["sha256"]


def verify_quad_cage(raw, candidate, receipt, *, target_faces, geometry_proof):
    """Bind a stage-owned or authenticated receipt to the caller's qualified GEO proof."""
    cage_sha, quads, candidate_sha = _check_quad_cage_custody(raw, candidate, receipt, target_faces=target_faces)
    require(type(geometry_proof) is dict and geometry_proof.get("schema") == "af.mesh-regression/v2"
            and type(geometry_proof.get("rc")) is int and geometry_proof["rc"] == 0
            and geometry_proof.get("verdict") == "PASS"
            and type(geometry_proof.get("measurement_rc")) is int and geometry_proof["measurement_rc"] == 0,
            3, "quad_cage_geometry_unqualified")
    levels, subjects = geometry_proof.get("levels"), geometry_proof.get("subjects")
    require(type(levels) is list and type(subjects) is list and len(levels) == len(subjects) == 2
            and type(levels[1]) is dict, 2, "quad_cage_geometry_contract")
    quad = levels[1].get("quad_cage")
    require(type(quad) is dict, 3, "required_quad_cage_missing")
    require(subjects[1] == "sha256:" + candidate_sha and quad.get("cage_digest") == "sha256:" + cage_sha
            and type(quad.get("quads")) is int and quad["quads"] == quads,
            4, "cage_export_drift")
    return cage_sha


def run_candidate(raw, directory, *, method, target_faces, seed, policy, timeout_s,
                  quadriflow_target_quads=None):
    """Write only owned stage scratch; the pipeline owns atomic publication.

    The worker, preservation checks, reduction checks and distance/silhouette
    algorithms stay in their original modules. No LODs are requested by this CLI.
    """
    require(method in ("identity", "decimate", "quadriflow"), 2, "method_invalid")
    require(type(timeout_s) is int and 0 < timeout_s <= 300, 2, "timeout_invalid")
    require(type(seed) is int and seed >= 0, 2, "seed_invalid")
    contracts.validate_policy(policy)
    deadline = time.monotonic() + timeout_s
    original = file_ref(raw)
    before = inspect_glb(raw)
    target = before["faces"] if method == "identity" else target_faces
    require(type(target) is int and target > 0, 2, "target_faces_required")
    require(method == "identity" or target < before["faces"], 2, "target_not_reduction")
    solver_target = target // 2 if method == "quadriflow" else target
    if quadriflow_target_quads is not None:
        require(method == "quadriflow" and type(quadriflow_target_quads) is int
                and 0 < 2 * quadriflow_target_quads <= target, 2, "quadriflow_solver_target_invalid")
        solver_target = quadriflow_target_quads
    require(solver_target > 0, 2, "quadriflow_solver_target_invalid")
    minimum = max(4 * before["faces"], before["sample_population"])
    require(policy["sample_budget"] >= minimum and
            policy["raster_resolution"] >= topology.MIN_RASTER_RESOLUTION,
            2, "measurement_policy_weakened")
    require(before["uv_primitives"] in (0, before["primitives"]), 3, "partial_uv_unsupported")
    directory.mkdir()
    source, candidate = directory / "raw.glb", directory / "master.glb"
    source.write_bytes(read_bytes(raw))
    if method == "identity":
        candidate.write_bytes(read_bytes(source))
    else:
        executable = shutil.which("blender")
        require(executable is not None, 3, "blender_unavailable")
        plan = {"source": str(source), "candidate": str(candidate), "method": method,
                "target_faces": solver_target, "seed": seed}
        nice_adjustment = 15
        if method == "quadriflow":
            current_nice = os.nice(0)
            require(current_nice <= 15, 3, "topology_nice_unavailable")
            nice_adjustment -= current_nice
            plan["quad_cage"] = str(candidate.with_suffix(".cage.json"))
            toolchain = _quad_toolchain(executable)
        core.atomic_write_json(directory / "operation.json", plan)
        argv = topology.SCOPE_ARGV + ["taskset", "-c", "0-15", "nice", "-n", str(nice_adjustment), executable,
            "-b", "--factory-startup", "--threads", "1", "--python-exit-code", "2",
            "--python", str(Path(topology.__file__).with_name("topology_worker.py")),
            "--", str(directory / "operation.json")]
        try:
            # QuadriFlow already has an explicit CPU policy relative to the inherited nice.
            code, output = topology.default_runner(argv, topology._remaining(deadline),
                                                   reuse_scope=method != "quadriflow")
        except subprocess.TimeoutExpired as exc:
            raise Refusal(6, "deadline_exceeded") from exc
        topology._check_worker(code, output)
    after = inspect_glb(candidate)
    check = {"role": "master", "requested_faces": target, "achieved_faces": after["faces"]}
    record = {"rc": 0, "status": "OK", "method": method, "raw_sha256": original["sha256"],
              "candidate_sha256": after["sha256"], "policy_sha256": core.sha256_json(policy),
              "checks": [check]}
    if method == "quadriflow":
        try:
            cage_sha, quads = topology.read_quad_cage(candidate)
        except TopologyError as exc:
            raise Refusal(exc.rc, exc.reason) from exc
        require(after["faces"] == 2 * quads, 4, "cage_export_coverage")
        record["toolchain_sha256"] = toolchain
        check.update(candidate_sha256=after["sha256"], source_faces=before["faces"],
                     quad_cage_path=candidate.with_suffix(".cage.json").name, quad_cage_sha256=cage_sha)
    require(policy["sample_budget"] >= after["sample_population"], 2, "measurement_policy_weakened")
    topology._check_preservation(before, after)
    if method != "identity":
        topology._check_reduction(before, after, target)
    code, receipt = topology._bounded_compare(mesh.compare_meshes, source, candidate,
                                              policy, topology._remaining(deadline))
    verify_regression(code, receipt, source, candidate, policy)
    require(file_ref(raw) == original and file_ref(source)["sha256"] == original["sha256"],
            4, "source_stale")
    if method == "quadriflow":
        _check_quad_cage_custody(raw, candidate, record, target_faces=target)
    topology._remaining(deadline)
    core.atomic_write_json(directory / "master.regression.json", receipt)
    (directory / "LOD0.glb").write_bytes(read_bytes(candidate))
    check["metrics_receipt_sha256"] = core.sha256_json(receipt)
    return record
