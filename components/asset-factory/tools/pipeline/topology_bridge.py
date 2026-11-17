"""Pipeline topology orchestration using the landed worker and geometry gates.

The legacy topology consumer expects list-valued absence evidence. The native
mesh contract requires an object. Authenticate that contract first; adapt only
the in-memory view passed to the legacy gate, retaining native receipts intact.
No injected comparator or test-only evaluator can authorize this boundary.
"""
import copy
from pathlib import Path
import shutil
import subprocess
import time

from asset_factory import core
from tools.blender import topology
from tools.blender.bake_support import read_bytes
from tools.blender.topology_glb import inspect_glb
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


def run_candidate(raw, directory, *, method, target_faces, seed, policy, timeout_s):
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
                "target_faces": target // 2 if method == "quadriflow" else target, "seed": seed}
        core.atomic_write_json(directory / "operation.json", plan)
        argv = topology.SCOPE_ARGV + ["taskset", "-c", "0-15", "nice", "-n", "15", executable,
            "-b", "--factory-startup", "--threads", "1", "--python-exit-code", "2",
            "--python", str(Path(topology.__file__).with_name("topology_worker.py")),
            "--", str(directory / "operation.json")]
        try:
            code, output = topology.default_runner(argv, topology._remaining(deadline))
        except subprocess.TimeoutExpired as exc:
            raise Refusal(6, "deadline_exceeded") from exc
        topology._check_worker(code, output)
    after = inspect_glb(candidate)
    require(policy["sample_budget"] >= after["sample_population"], 2, "measurement_policy_weakened")
    topology._check_preservation(before, after)
    if method != "identity":
        topology._check_reduction(before, after, target)
    code, receipt = topology._bounded_compare(mesh.compare_meshes, source, candidate,
                                              policy, topology._remaining(deadline))
    verify_regression(code, receipt, source, candidate, policy)
    require(file_ref(raw) == original and file_ref(source)["sha256"] == original["sha256"],
            4, "source_stale")
    topology._remaining(deadline)
    core.atomic_write_json(directory / "master.regression.json", receipt)
    (directory / "LOD0.glb").write_bytes(read_bytes(candidate))
    return {"rc": 0, "status": "OK", "method": method, "raw_sha256": original["sha256"],
            "candidate_sha256": after["sha256"], "policy_sha256": core.sha256_json(policy),
            "checks": [{"role": "master", "requested_faces": target, "achieved_faces": after["faces"],
                        "metrics_receipt_sha256": core.sha256_json(receipt)}]}
