"""Offline remesh proposals, independently gated before atomic publication.

run(raw_glb, output_dir, *, expected_source_sha256, policy, method,
    voxel_size_um, octree_depth=7, closing_iterations=1,
    max_grid_cells=64000000, max_output_triangles=900000, timeout_s=900)

The new output directory contains raw.glb, remesh.glb, mesh.json, distance.json
and receipt.json only on success. Existing destinations and all refusal bytes
are preserved. No model, download, material synthesis or appearance approval.
af.remesh-stage/v1 and af.remesh-distance-audit/v1 are inline local contracts;
this tool consumes the existing topology slot, not a new slot family.
"""
import argparse
import importlib.metadata
import importlib.util
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

from asset_factory import core
from providers.birefnet import resources
from tools.blender.topology import default_runner, _publish_directory
from tools.mesh import api, contracts
from tools.mesh import remesh_gate
from tools.mesh.worker import _document

ROOT = Path(__file__).resolve().parents[2]
RECONSTRUCT = Path(__file__).with_name("remesh_reconstruct.py")
BLENDER_WORKER = ROOT / "tools/blender/remesh_worker.py"
AUDITOR = ROOT / "tools/blender/remesh_measure.py"
EXPECTATION_GATE = Path(remesh_gate.__file__)
SCOPE = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G",
         "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0",
         "taskset", "-c", "0-15", "nice", "-n", "15"]


def _require(condition, rc, reason):
    if not condition:
        raise contracts.MeshError(rc, reason)


def _integer(value, lo, hi):
    _require(type(value) is int and lo <= value <= hi, 2, "invalid_parameter")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    _require(remaining > 0, 6, "deadline_exceeded")
    return remaining


def _worker(argv, deadline, *, measurement=False):
    code, log = default_runner(resources.bounded_scope_prefix(SCOPE) +
                               ['env', 'CUDA_VISIBLE_DEVICES='] + argv, _remaining(deadline))
    markers = re.findall(r"^AF_REMESH_RC ([023456])$", log, re.MULTILINE)
    _require(len(markers) == 1, 3, "worker_completion_unproven")
    rc = int(markers[0])
    _require((rc == 0 and code == 0) or (rc != 0 and code in (rc, 2)), 2, "worker_exit_mismatch")
    if rc and not (measurement and rc == 5):
        raise contracts.MeshError(rc, "worker_refused")
    return rc


def _engine_identity(method):
    """Pin installed optional packages by actual file bytes, not version alone."""
    if method != "voxel_grid":
        return {}, []
    engines, roots = {}, set()
    for name in ("numpy", "scipy", "skimage", "trimesh"):
        spec = importlib.util.find_spec(name)
        _require(spec is not None and spec.origin is not None, 3, "engine_unavailable:" + name)
        roots.add(str(Path(spec.origin).parent.parent))
        try:
            dist = importlib.metadata.distribution("scikit-image" if name == "skimage" else name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise contracts.MeshError(3, "engine_inventory_unavailable") from exc
        _require(dist.files is not None, 3, "engine_inventory_unavailable")
        files = {}
        for entry in dist.files:
            if str(entry).startswith(name + "/") and str(entry).endswith((".py", ".so")):
                path = Path(dist.locate_file(entry))
                files[str(entry)] = core.sha256_file(path)
        _require(bool(files), 3, "engine_inventory_unavailable")
        engines[name] = {"version": dist.version, "files_sha256": core.sha256_json(files)}
    return engines, sorted(roots)


def _tool_identity(blender, engines):
    return core.sha256_json({"files": {p.name: core.sha256_file(p) for p in
                             (Path(__file__), RECONSTRUCT, BLENDER_WORKER, AUDITOR, EXPECTATION_GATE,
                              Path(resources.__file__))},
                             "blender_sha256": core.sha256_file(Path(blender)),
                             "mesh_gate_sha256": api.toolchain_identity(), "engines": engines})


def _validate_audit(audit, raw_hash, candidate_hash):
    keys = {"schema", "rc", "input_sha256", "candidate_sha256", "raw_diag_um", "forward_um",
            "backward_um", "forward_cover_estimate_um", "backward_cover_estimate_um",
            "forward_samples", "backward_samples", "distance_is_exact", "estimator", "coverage_limit"}
    _require(isinstance(audit, dict) and set(audit) == keys, 2, "audit_contract")
    _require(audit["schema"] == "af.remesh-distance-audit/v1" and
             type(audit["rc"]) is int and audit["rc"] in (0, 5) and
             audit["input_sha256"] == raw_hash and audit["candidate_sha256"] == candidate_hash and
             audit["distance_is_exact"] is False and
             audit["estimator"] == "all_vertices_and_triangle_centres_BVH", 2, "audit_binding")
    for key in ("raw_diag_um", "forward_samples", "backward_samples"):
        _require(type(audit[key]) is int and audit[key] > 0, 3, "audit_coverage_unproven")
    metrics = [audit[k] for k in ("forward_um", "backward_um", "forward_cover_estimate_um",
                                "backward_cover_estimate_um")]
    _require(all(type(x) is int and x >= 0 for x in metrics), 2, "audit_metric")
    measured_rc = 0 if max(metrics) * 1000000 <= audit["raw_diag_um"] * contracts.DISTANCE_MAX_PPM else 5
    _require(audit["rc"] == measured_rc, 2, "audit_verdict_mismatch")
    _require(measured_rc == 0, 5, "dense_distance_or_cover")


def run(raw_glb, output_dir, *, expected_source_sha256, policy, method,
        voxel_size_um, octree_depth=7, closing_iterations=1, max_grid_cells=64000000,
        max_output_triangles=900000, timeout_s=900, blender="blender", fill_cross_sections=False,
        closing_shape="cross", fill_axes=None, topology_expectation=None,
        selective_closing_max=0, selective_patch_limit=512, preserve_recesses=False, geometry_scope=None):
    started = time.monotonic()
    rec = {"schema": "af.remesh-stage/v1", "rc": 3, "status": "INCOMPLETE", "verdict": "CANNOT_SEE",
           "scope": "geometry-only", "reason_codes": [], "raw_sha256": None,
           "candidate_sha256": None, "policy_sha256": None, "toolchain_sha256": None,
           "parameters": {}, "engines": {}, "mesh_receipt": None, "distance_audit": None,
           "topology_expectation_sha256": None,
           "reconstruction": None,
           "artifacts": [], "wall_ms": None, "peak_rss_bytes": None,
           "absence_reasons": {"peak_rss_bytes": "not_independently_measured",
                               "appearance": "requires_separate_reference_appearance_gate"}}
    try:
        _integer(timeout_s, 1, 1800)
        deadline = started + timeout_s
        contracts.validate_policy(policy)
        policy = contracts.loads(contracts.canonical(policy))
        _require(not policy["allow_open"] and not policy["require_uv"], 2, "closed_geometry_policy_required")
        _require(policy["raster_resolution"] >= 96, 2, "raster_coverage_below_floor")
        _require(method in ("voxel_grid", "voxel", "octree"), 2, "unknown_method")
        _integer(voxel_size_um, 1, 1000000)
        _integer(octree_depth, 5, 9)
        _integer(closing_iterations, 1, 8)
        _integer(selective_closing_max, 0, 8)
        _integer(selective_patch_limit, 1, 1024)
        _require(type(preserve_recesses) is bool,2,"invalid_parameter")
        _require(not preserve_recesses or selective_closing_max > 0,2,"recesses_require_selective_closure")
        _require(type(fill_cross_sections) is bool, 2, "invalid_parameter")
        _require(closing_shape in ("cross", "cube"), 2, "invalid_parameter")
        if fill_axes is None:
            fill_axes = [0, 1, 2] if fill_cross_sections else []
        _require(isinstance(fill_axes, (list, tuple)) and len(fill_axes) <= 3 and
                 all(type(axis) is int and axis in (0, 1, 2) for axis in fill_axes) and
                 len(set(fill_axes)) == len(fill_axes), 2, "invalid_fill_axes")
        fill_axes = list(fill_axes)
        if selective_closing_max:
            _require(closing_iterations < selective_closing_max <= closing_iterations+3 and
                     method == "voxel_grid" and closing_shape == "cube" and not fill_axes and
                     topology_expectation is not None, 2, "selective_closure_requires_declared_topology")
        _integer(max_grid_cells, 1, 64000000)
        _integer(max_output_triangles, 1, 900000)
        _require(contracts.valid_hash(expected_source_sha256), 2, "source_pin_required")
        source, output = Path(raw_glb), Path(output_dir).absolute()
        _require(not os.path.lexists(output), 2, "output_exists")
        _require(output.parent.is_dir(), 2, "output_parent_missing")
        raw = api._acquire(source, deadline)
        raw_hash = contracts.digest(raw)
        rec["raw_sha256"] = raw_hash
        _require(raw_hash == expected_source_sha256, 4, "source_stale")
        doc = _document(raw)
        _require(len(doc.get("meshes", [])) == 1 and len(doc["meshes"][0]["primitives"]) == 1,
                 3, "single_primitive_required")
        primitive = doc["meshes"][0]["primitives"][0]
        _require(not doc.get("materials") and not doc.get("textures") and not doc.get("images") and
                 not set(primitive["attributes"]) - {"POSITION", "NORMAL"},
                 3, "attribute_preserving_remesh_unavailable")
        names = [node.get("name") for node in doc.get("nodes", []) if "mesh" in node]
        _require(len(names) == 1 and type(names[0]) is str and bool(names[0]), 2, "mesh_name_required")
        if topology_expectation is None:
            _require(policy["component_allowlist"] == {names[0]: 1}, 2, "single_closed_component_required")
        else:
            topology_expectation = contracts.loads(contracts.canonical(topology_expectation))
            remesh_gate.validate_expectation(topology_expectation, raw_hash, policy)
            _require(set(policy["component_allowlist"]) == {names[0]}, 2, "component_inventory_mismatch")
            ref = topology_expectation["reference"]
            _require(contracts.digest(api._acquire(Path(ref["path"]), deadline)) == ref["sha256"],
                     4, "topology_reference_stale")
            rec["topology_expectation_sha256"] = contracts.json_digest(topology_expectation)
        pos = doc["accessors"][primitive["attributes"]["POSITION"]]
        population = pos["count"] + doc["accessors"][primitive["indices"]]["count"] // 3
        _require(policy["sample_budget"] >= min(population, 2048), 2, "sample_coverage_below_floor")
        binary = shutil.which(blender)
        _require(binary is not None, 3, "blender_unavailable")
        engines, roots = _engine_identity(method)
        rec["engines"] = engines
        tool_hash = _tool_identity(binary, engines)
        rec["toolchain_sha256"] = tool_hash
        rec["policy_sha256"] = (contracts.json_digest(policy) if topology_expectation is None else
                                remesh_gate.policy_identity(policy, topology_expectation))
        params = {"method": method, "voxel_size_um": voxel_size_um, "octree_depth": octree_depth,
                  "closing_iterations": closing_iterations, "max_grid_cells": max_grid_cells,
                  "max_output_triangles": max_output_triangles, "timeout_s": timeout_s,
                  "fill_cross_sections": fill_cross_sections, "fill_axes": fill_axes,
                  "closing_shape": closing_shape}
        params.update(selective_closing_max=selective_closing_max, selective_patch_limit=selective_patch_limit,
                      preserve_recesses=preserve_recesses)
        rec["parameters"] = params
        with tempfile.TemporaryDirectory(prefix=".remesh-", dir=output.parent) as temporary:
            scratch = Path(temporary)
            stage = scratch / "publish"
            stage.mkdir()
            snapshot, candidate = stage / "raw.glb", stage / "remesh.glb"
            snapshot.write_bytes(raw)
            plan = dict(params, source=str(snapshot), source_sha256=raw_hash,
                        candidate=str(candidate), mesh_name=names[0])
            if selective_closing_max:
                plan.update(policy=policy,topology_expectation=topology_expectation,
                            reconstruction_trace=str(stage / "reconstruction.json"))
            plan_path = scratch / "plan.json"
            core.atomic_write_json(plan_path, plan)
            if method == "voxel_grid":
                argv = ["env", "PYTHONPATH=" + os.pathsep.join(roots), sys.executable,
                        "-s", "-B", str(RECONSTRUCT), str(plan_path)]
            else:
                argv = [binary, "-b", "--factory-startup", "-t", "8", "--python-exit-code", "2",
                        "--python", str(BLENDER_WORKER), "--", str(plan_path)]
            _worker(argv, deadline)
            if selective_closing_max:
                trace=core.load_json(stage / "reconstruction.json", max_bytes=262144)
                _require(trace["schema"] == "af.remesh-reconstruction/v1" and
                         type(trace["rc"]) is int and trace["rc"] == 0 and
                         trace["input_sha256"] == raw_hash and
                         trace["candidate_sha256"] == core.sha256_file(candidate), 2, "reconstruction_binding")
                rec["reconstruction"] = trace
            candidate_bytes = api._acquire(candidate, deadline)
            candidate_hash = contracts.digest(candidate_bytes)
            rec["candidate_sha256"] = candidate_hash
            if geometry_scope is not None:
                from tools.mesh.certificate import certify
                proof = certify([raw, candidate_bytes], **geometry_scope)
                rec["geometry_certificate"] = proof
                _require(proof["rc"] == 0, proof["rc"], next(iter(proof["reason_codes"]), "geometry_refused"))
            candidate_doc = _document(candidate_bytes)
            triangles = sum(candidate_doc["accessors"][p["indices"]]["count"] // 3
                            for m in candidate_doc["meshes"] for p in m["primitives"])
            _require(0 < triangles <= max_output_triangles, 6, "output_triangle_budget")
            remaining = math.floor(_remaining(deadline))
            _require(remaining >= policy["timeout_s"], 6, "gate_budget_unavailable")
            if topology_expectation is None:
                rc, mesh = api.compare_meshes(snapshot, candidate, policy=policy)
            else:
                plan.update(candidate_sha256=candidate_hash, policy=policy,
                            topology_expectation=topology_expectation,
                            mesh_receipt=str(stage / "mesh.json"))
                core.atomic_write_json(plan_path, plan)
                _worker([sys.executable, "-I", "-B", str(EXPECTATION_GATE), str(plan_path)],
                        min(deadline, time.monotonic() + policy["timeout_s"]), measurement=True)
                mesh = core.load_json(stage / "mesh.json", max_bytes=131072)
                rc = mesh["rc"]
                _require(mesh["policy_sha256"] == remesh_gate.policy_identity(policy,topology_expectation),
                         2, "topology_expectation_binding")
                _require(mesh["toolchain_sha256"] == remesh_gate.tool_identity(),2,"topology_toolchain_binding")
            contracts.validate_receipt(mesh)
            contracts.validate_success(mesh, "compare")
            rec["mesh_receipt"] = mesh
            _require(type(rc) is int and rc == mesh["rc"] and mesh["input_sha256"] == raw_hash and
                     mesh["candidate_sha256"] == candidate_hash and
                     mesh["policy_sha256"] == rec["policy_sha256"], 2, "mesh_gate_binding")
            _require(rc == 0, rc, "mesh_gate_refused")
            plan.update(candidate_sha256=candidate_hash, audit=str(stage / "distance.json"))
            core.atomic_write_json(plan_path, plan)
            _worker([binary, "-b", "--factory-startup", "-t", "8", "--python-exit-code", "2",
                     "--python", str(AUDITOR), "--", str(plan_path)], deadline, measurement=True)
            audit = core.load_json(stage / "distance.json", max_bytes=65536)
            rec["distance_audit"] = audit
            _validate_audit(audit, raw_hash, candidate_hash)
            _require(contracts.digest(api._acquire(source, deadline)) == raw_hash, 4, "source_stale")
            _require(contracts.digest(api._acquire(snapshot, deadline)) == raw_hash and
                     contracts.digest(api._acquire(candidate, deadline)) == candidate_hash, 4, "scratch_stale")
            current_engines, _ = _engine_identity(method)
            _require(_tool_identity(binary, current_engines) == tool_hash, 4, "toolchain_stale")
            if topology_expectation is not None:
                ref = topology_expectation["reference"]
                _require(contracts.digest(api._acquire(Path(ref["path"]), deadline)) == ref["sha256"],
                         4, "topology_reference_stale")
                core.atomic_write_json(stage / "topology-expectation.json", topology_expectation)
            _remaining(deadline)
            core.atomic_write_json(stage / "mesh.json", mesh)
            rec.update(rc=0, status="OK", verdict="PASS", wall_ms=round((time.monotonic() - started) * 1000))
            rec["artifacts"] = [{"role": role, "path": name, "sha256": core.sha256_file(stage / name)}
                                for role, name in (("raw_glb", "raw.glb"), ("topology_glb", "remesh.glb"),
                                                   ("mesh_receipt", "mesh.json"), ("distance_audit", "distance.json"))]
            core.atomic_write_json(stage / "policy.json", policy)
            rec["artifacts"].append({"role": "mesh_policy", "path": "policy.json",
                                      "sha256": core.sha256_file(stage / "policy.json")})
            if topology_expectation is not None:
                rec["artifacts"].append({"role": "topology_expectation", "path": "topology-expectation.json",
                                          "sha256": core.sha256_file(stage / "topology-expectation.json")})
            for name, role in (("reconstruction.json","reconstruction_trace"),):
                if (stage / name).is_file():
                    rec["artifacts"].append({"role":role,"path":name,"sha256":core.sha256_file(stage / name)})
            core.atomic_write_json(stage / "receipt.json", rec)
            _publish_directory(stage, output)
        return 0, rec
    except contracts.MeshError as exc:
        rc, reason = exc.rc, exc.code
    except core.ContractError as exc:
        rc, reason = 2, exc.code
    except TimeoutError:
        rc, reason = 6, "deadline_exceeded"
    except Exception as exc:
        import subprocess
        rc, reason = (6, "deadline_exceeded") if isinstance(exc, subprocess.TimeoutExpired) else (2, "io_or_contract_error")
    rec.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc], reason_codes=[reason], artifacts=[],
               wall_ms=round((time.monotonic() - started) * 1000))
    return rc, rec


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["certify"]:
        from tools.mesh.certificate import main as certificate_main
        return certificate_main(argv[1:])
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--method", choices=("voxel_grid", "voxel", "octree"), required=True)
    parser.add_argument("--voxel-size-um", type=int, required=True)
    parser.add_argument("--octree-depth", type=int, default=7)
    parser.add_argument("--closing-iterations", type=int, default=1)
    parser.add_argument("--fill-cross-sections", action="store_true")
    parser.add_argument("--fill-axis", type=int, action="append")
    parser.add_argument("--closing-shape", choices=("cross", "cube"), default="cross")
    parser.add_argument("--topology-expectation", type=Path)
    parser.add_argument("--selective-closing-max",type=int,default=0)
    parser.add_argument("--selective-patch-limit",type=int,default=512)
    parser.add_argument("--preserve-recesses",action="store_true")
    parser.add_argument("--max-grid-cells", type=int, default=64000000)
    parser.add_argument("--timeout-s", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        policy = core.load_json(args.policy, max_bytes=65536)
        expectation = (core.load_json(args.topology_expectation, max_bytes=65536)
                       if args.topology_expectation is not None else None)
        rc, record = run(args.raw, args.output_dir, expected_source_sha256=args.source_sha256,
                         policy=policy, method=args.method, voxel_size_um=args.voxel_size_um,
                         octree_depth=args.octree_depth, closing_iterations=args.closing_iterations,
                         max_grid_cells=args.max_grid_cells, timeout_s=args.timeout_s,
                         fill_cross_sections=args.fill_cross_sections, closing_shape=args.closing_shape,
                         fill_axes=args.fill_axis, topology_expectation=expectation,
                         selective_closing_max=args.selective_closing_max,
                         selective_patch_limit=args.selective_patch_limit,
                         preserve_recesses=args.preserve_recesses)
    except (core.ContractError, OSError) as exc:
        record = {"rc": 2, "status": "ERROR", "reason_codes": [getattr(exc, "code", "io_error")]}
        rc = 2
    sys.stdout.write(core.canonical_json(record))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
