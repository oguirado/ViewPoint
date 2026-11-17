"""Opt-in topology candidates and raw-anchored LODs; no appearance approval.

Public API: run(raw_glb, output_dir, *, method, target_faces, lod_targets=(),
seed, policy, expected_source_sha256, timeout_s=300, blender='blender',
runner=default_runner, comparator=None, expected_toolchain_sha256=None).
Returns (stage_rc, af.topology-stage/v1 receipt). Output must be a new directory.

Inline af.topology-stage/v1 contract: schema, rc, status, scope='geometry-only',
reason_codes, raw_sha256, candidate_sha256, method, parameters, policy_sha256,
toolchain_sha256, artifacts, checks, wall_ms, peak_rss_bytes, absence_reasons.
Artifacts bind role/path/sha256, source_sha256, requested_faces, achieved_faces
and metrics_receipt_sha256. A refusal has no published artifacts.

policy contains silhouette_iou_min_ppm, hausdorff_max_ppm and mesh_policy.
The comparator has F2-U1's compare_meshes(raw, candidate, *, policy)
signature. A local adapter requires af.mesh-regression/v1 and integer metrics
hausdorff_forward_um, hausdorff_backward_um, raw_diag_um, silhouette_iou_ppm.
These directional names are an explicit integration assumption, not a claim
about an existing sibling implementation. A mismatch is CANNOT_SEE.
Injected adapters require allow_test_comparator=True, recorded as test-only.
mesh_policy requires sample_budget >= the raw triangle-corner/centre population
and raster_resolution >= MIN_RASTER_RESOLUTION (uncalibrated DESIGN_CHOICE).

quadriflow_target_quads optionally selects the solver's approximate target;
target_faces remains the hard exported-triangle cap. Without the explicit
solver parameter its mapping is target_faces//2. No retry or fallback occurs.
"""
from asset_factory import proc as af_proc
import argparse
import ctypes
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from providers.birefnet import resources

from .topology_glb import (TopologyError, canonical, digest, inspect_glb,
                           json_digest, read_bytes, _pairs, core)

STATUS = core.STATUS
SCOPE_ARGV = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G",
              "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0"]

# [TARGET] DESIGN_CHOICE, not calibrated appearance or continuous-distance proof.
MIN_RASTER_RESOLUTION = 96
RECEIPT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:af:topology-stage:v1", "type": "object", "additionalProperties": False,
    "properties": {"schema": {"const": "af.topology-stage/v1"}, "rc": {"type": "integer"},
                   "status": {"type": "string"}, "scope": {"const": "geometry-only"},
                   "reason_codes": {"type": "array"}, "raw_sha256": {"type": ["string", "null"]},
                   "candidate_sha256": {"type": ["string", "null"]}, "method": {"type": "string"},
                   "parameters": {"type": "object"}, "policy_sha256": {"type": ["string", "null"]},
                   "toolchain_sha256": {"type": ["string", "null"]}, "artifacts": {"type": "array"},
                   "checks": {"type": "array"}, "wall_ms": {"type": ["integer", "null"]},
                   "peak_rss_bytes": {"type": ["integer", "null"]}, "absence_reasons": {"type": "object"},
                   "evaluator": {"type": ["object", "null"]}},
}
RECEIPT_SCHEMA["required"] = list(RECEIPT_SCHEMA["properties"])


def _closed_object(properties, required=None):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties) if required is None else required}


_INT = {"type": "integer"}
_STR = {"type": "string"}
_ARTIFACT = _closed_object({key: _INT if key in ("requested_faces", "achieved_faces") else _STR for key in
    ("role", "path", "sha256", "source_sha256", "requested_faces", "achieved_faces",
     "metrics_receipt_path", "metrics_receipt_sha256")})
RECEIPT_SCHEMA["properties"]["artifacts"]["items"] = _ARTIFACT
RECEIPT_SCHEMA["properties"]["parameters"] = _closed_object({
    "target_faces": _INT, "lod_targets": {"type": "array", "items": _INT}, "seed": _INT,
    "timeout_s": _INT, "import_merge_vertices": {"type": "boolean"}, "seam_weld": _STR,
    "weld_tolerance_um": _INT, "quadriflow_preserve_sharp": {"type": "boolean"}, "face_count_unit": _STR,
    "quadriflow_target_quads": {"type": ["integer", "null"]}, "minimum_sample_budget": _INT,
    "minimum_raster_resolution": _INT}, required=[])
RECEIPT_SCHEMA["properties"]["evaluator"] = {"oneOf": [{"type": "null"}, _closed_object({
    "module": _STR, "callable": _STR, "module_sha256": _STR, "test_only": {"type": "boolean"}})]}
RECEIPT_SCHEMA["properties"]["checks"]["items"] = _closed_object({
    "role": _STR, "requested_faces": _INT, "achieved_faces": _INT, "source_faces": _INT,
    "candidate_sha256": _STR, "uv_sha256": {"type": "array", "items": _STR},
    "rc": _INT, "metrics": {"type": ["object", "null"]}, "failed": {"type": ["array", "null"]},
    "metrics_receipt_sha256": _STR, "evaluator_toolchain_sha256": _STR},
    required=["role", "requested_faces", "achieved_faces", "source_faces", "candidate_sha256"])


def _validate_receipt(record):
    """Validate the closed stage envelope even when the visible F0 stub is used."""
    canonical(record)
    _require(isinstance(record, dict) and set(record) == set(RECEIPT_SCHEMA["required"]),
             2, "receipt_schema_invalid")
    try:
        core.validate(record, RECEIPT_SCHEMA)
    except core.ContractError as exc:
        raise TopologyError(2, "receipt_schema_invalid") from exc
    types = {"string": str, "integer": int, "object": dict, "array": list, "null": type(None), "boolean": bool}
    def shape(value, rule):
        if "oneOf" in rule:
            candidates = []
            for branch in rule["oneOf"]:
                try:
                    shape(value, branch)
                    candidates.append(branch)
                except TopologyError:
                    pass
            _require(len(candidates) == 1, 2, "receipt_schema_invalid")
        if "const" in rule:
            _require(value == rule["const"], 2, "receipt_schema_invalid")
        if "type" in rule:
            allowed = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
            _require(type(value) in tuple(types[name] for name in allowed), 2, "receipt_schema_invalid")
        if "properties" in rule:
            _require(isinstance(value, dict) and set(rule.get("required", [])) <= set(value) and
                     (rule.get("additionalProperties") is not False or set(value) <= set(rule["properties"])),
                     2, "receipt_schema_invalid")
            for key, child in value.items():
                if key in rule["properties"]:
                    shape(child, rule["properties"][key])
        if "items" in rule:
            for child in value:
                shape(child, rule["items"])
    shape(record, RECEIPT_SCHEMA)
    _require(record["rc"] in STATUS and record["status"] == STATUS[record["rc"]], 2, "receipt_schema_invalid")
    if record["rc"] == 0:
        _require(bool(record["artifacts"]) and bool(record["checks"]) and record["evaluator"] is not None,
                 2, "receipt_schema_invalid")
    else:
        _require(record["artifacts"] == [], 2, "receipt_schema_invalid")


def _publish_directory(stage, output):
    """Linux atomic no-replace rename; no empty destination reservation exists.

    Interrupted runs can leave .topology-* scratch only. Its owner.json is
    diagnostic: inspect it and verify the owner is gone before manual removal.
    No scratch is promoted or recovered automatically; caller paths stay intact.
    """
    library = ctypes.CDLL(None, use_errno=True)
    _require(hasattr(library, "renameat2"), 3, "atomic_publish_unavailable")
    rename = library.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # Linux AT_FDCWD and RENAME_NOREPLACE, validated by the race control.
    if rename(-100, os.fsencode(stage), -100, os.fsencode(output), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(output))


def _require(condition, rc, reason):
    if not condition:
        raise TopologyError(rc, reason)


def _integer(value, minimum=0):
    _require(type(value) is int and value >= minimum, 2, "invalid_integer")
    return value


def _hash_match(actual, expected, reason):
    _require(isinstance(expected, str) and len(expected) == 64 and
             all(c in "0123456789abcdef" for c in expected), 2, "invalid_digest")
    _require(actual == expected, 4, reason)


def _check_destination(raw, output):
    _require(raw != output and output not in raw.parents,
             2, "raw_output_alias")
    _require(not output.exists(), 2, "destination_exists")
    _require(output.parent.is_dir(), 2, "destination_parent_missing")


def _write(path, data):
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _check_preservation(before, after):
    _require(before["uv_primitives"] in (0, before["primitives"]), 3, "partial_uv_unsupported")
    _require(before["materials_sha256"] == after["materials_sha256"] and
             before["image_sha256"] == after["image_sha256"] and
             before["texture_state_sha256"] == after["texture_state_sha256"] and
             (not before["uv_primitives"] or
              (after["uv_primitives"] == after["primitives"] and
               before["uv_primitives"] == before["primitives"])),
             5, "material_or_uv_stripped")
    _require(not any(any(span) for span in before["uv_spans"]) or
             any(any(span) for span in after["uv_spans"]), 5, "uv_collapsed")


def _check_reduction(before, after, target):
    _require(after["faces"] <= target and after["faces"] < before["faces"] and
             after["sha256"] != before["sha256"], 5, "face_budget_unmet_or_fake_lod")


def _gate_regression(rc, receipt, raw_sha, candidate_sha, policy):
    _require(type(rc) is int and rc in STATUS and isinstance(receipt, dict), 2, "regression_contract")
    canonical(receipt)
    _require(receipt.get("schema") == "af.mesh-regression/v1", 2, "regression_family")
    _require(type(receipt.get("rc")) is int and receipt["rc"] == rc and
             receipt.get("status") == STATUS[rc], 2, "regression_rc_status")
    _hash_match(receipt.get("input_sha256"), raw_sha, "regression_raw_stale")
    _hash_match(receipt.get("candidate_sha256"), candidate_sha, "regression_candidate_stale")
    _hash_match(receipt.get("policy_sha256"), json_digest(policy["mesh_policy"]), "regression_policy_stale")
    _require(rc == 0, rc, "regression_refused")
    _require(isinstance(receipt.get("failed"), list) and isinstance(receipt.get("unavailable"), list),
             2, "regression_contract")
    _require(not receipt.get("failed"), 5, "regression_failed_checks")
    _require(not receipt.get("unavailable"), 3, "regression_checks_unavailable")
    metrics = receipt.get("metrics", {})
    _require(isinstance(metrics, dict), 2, "regression_contract")
    _require(_valid_digest(receipt.get("toolchain_sha256")), 2, "regression_toolchain_invalid")
    names = ("hausdorff_forward_um", "hausdorff_backward_um", "raw_diag_um", "silhouette_iou_ppm")
    _require(all(metrics.get(name) is not None for name in names), 3, "regression_metric_missing")
    for name in names:
        _integer(metrics[name])
    diagonal = metrics["raw_diag_um"]
    _require(diagonal > 0, 3, "raw_scale_unknown")
    _require(metrics["silhouette_iou_ppm"] <= 1_000_000, 2, "iou_invalid")
    _require(metrics["silhouette_iou_ppm"] >= policy["silhouette_iou_min_ppm"], 5, "silhouette_regression")
    _require(max(metrics["hausdorff_forward_um"], metrics["hausdorff_backward_um"]) * 1_000_000
             <= diagonal * policy["hausdorff_max_ppm"], 5, "surface_regression")
    return receipt


def _valid_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _bounded_compare(comparator, raw, candidate, policy, timeout_s):
    """Bound trusted Python work; the mesh API owns any external services."""
    from asset_factory.deadline import run
    result = run(comparator, timeout_s, raw, candidate, policy=policy)
    _require(isinstance(result, (tuple, list)) and len(result) == 2, 2, 'regression_contract')
    try:
        encoded = canonical(list(result)).encode('utf-8')
    except (ValueError, TypeError, TopologyError) as exc:
        raise TopologyError(2, 'regression_contract') from exc
    _require(len(encoded) <= 2**20, 6, 'regression_budget_exceeded')
    return result


def default_runner(argv, timeout_s):
    """Bounded logs, owned process group, mandatory host memory scope."""
    if argv[:len(SCOPE_ARGV)] == SCOPE_ARGV:
        argv = resources.bounded_scope_prefix(SCOPE_ARGV) + argv[len(SCOPE_ARGV):]
    with tempfile.TemporaryFile() as log:
        env = {key: os.environ[key] for key in ("PATH", "LANG", "CUDA_VISIBLE_DEVICES",
                                               "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
               if key in os.environ}
        if argv[0] == "systemd-run":
            argv = argv[:len(SCOPE_ARGV)] + ["env", "-u", "DBUS_SESSION_BUS_ADDRESS",
                   "-u", "XDG_RUNTIME_DIR"] + argv[len(SCOPE_ARGV):]
            for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
                if key in os.environ: env[key] = os.environ[key]
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
        proc = af_proc.popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        try:
            deadline = time.monotonic() + timeout_s
            while af_proc.poll_exit(proc) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout_s)
                time.sleep(min(0.01, remaining))
        finally:
            # The composed job publishes its result only after owned cleanup.
            try:
                af_proc.signal_group(proc, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=10)
        log.seek(0, os.SEEK_END)
        log.seek(max(0, log.tell() - 16384))
        return proc.returncode, log.read().decode("utf-8", errors="replace")


def _check_worker(rc, output):
    if "AF_TOPOLOGY_SOLVER_FAILED" in output:
        raise TopologyError(5, "solver_failed")
    if "AF_TOPOLOGY_CAPABILITY" in output or "AF_TOPOLOGY_RC 3" in output:
        raise TopologyError(3, "backend_unavailable")
    _require(type(rc) is int and rc == 0, 2, "backend_failed")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    _require(remaining > 0, 6, "deadline_exceeded")
    return remaining


def run(raw_glb, output_dir, *, method, target_faces, seed, policy,
        expected_source_sha256, lod_targets=(), timeout_s=300, blender="blender",
        runner=default_runner, comparator=None, expected_toolchain_sha256=None,
        quadriflow_target_quads=None, allow_test_comparator=False):
    started = time.monotonic_ns()
    record = {"schema": "af.topology-stage/v1", "rc": 3, "status": "INCOMPLETE", "scope": "geometry-only",
              "reason_codes": [], "raw_sha256": None, "candidate_sha256": None, "method": method,
              "parameters": {}, "policy_sha256": None, "toolchain_sha256": None,
              "artifacts": [], "checks": [], "wall_ms": None, "peak_rss_bytes": None,
              "evaluator": None,
              "absence_reasons": {"peak_rss_bytes": "not_independently_measured",
                                  "appearance": "F2-U3 appearance approval is separate"}}
    try:
        _require(method in ("identity", "decimate", "quadriflow"), 2, "unknown_method")
        _integer(target_faces, 1)
        _require(method != "quadriflow" or (target_faces >= 2 and target_faces % 2 == 0),
                 2, "quadriflow_even_triangle_budget_required")
        if quadriflow_target_quads is not None:
            _integer(quadriflow_target_quads, 1)
            _require(method == "quadriflow" and quadriflow_target_quads * 2 <= target_faces,
                     2, "quadriflow_solver_target_invalid")
        quad_target = quadriflow_target_quads if quadriflow_target_quads is not None else target_faces // 2
        _integer(seed)
        _require(seed <= 2**31 - 1, 2, "seed_out_of_range")
        _integer(timeout_s, 1)
        _require(timeout_s <= 3600, 2, "timeout_out_of_range")  # [TARGET]
        deadline = time.monotonic() + timeout_s
        _require(isinstance(lod_targets, (tuple, list)) and len(lod_targets) <= 8, 2, "invalid_lod_targets")
        targets = list(lod_targets)
        previous = target_faces
        for target in targets:
            _integer(target, 1)
            _require(target < previous, 2, "lod_budgets_not_decreasing")
            previous = target
        _require(isinstance(policy, dict) and set(policy) ==
                 {"silhouette_iou_min_ppm", "hausdorff_max_ppm", "mesh_policy"}, 2, "policy_invalid")
        _integer(policy["silhouette_iou_min_ppm"], 1)
        _integer(policy["hausdorff_max_ppm"])
        _require(950000 <= policy["silhouette_iou_min_ppm"] <= 1_000_000 and
                 policy["hausdorff_max_ppm"] <= 20000 and isinstance(policy["mesh_policy"], dict),
                 2, "policy_weakened")  # [TARGET] uncalibrated DESIGN_CHOICE.
        record["policy_sha256"] = json_digest(policy)
        record["parameters"] = {"target_faces": target_faces, "lod_targets": targets,
                                "seed": seed, "timeout_s": timeout_s,
                                "import_merge_vertices": True,
                                "seam_weld": "exact_positions", "weld_tolerance_um": 0,
                                "quadriflow_preserve_sharp": False,
                                "face_count_unit": "glb_triangles",
                                "quadriflow_target_quads": quad_target if method == "quadriflow" else None}
        raw_path, requested_output = Path(raw_glb), Path(output_dir)
        _require(not requested_output.is_symlink(), 2, "raw_output_alias")
        raw, output = raw_path.resolve(strict=True), requested_output.resolve()
        _check_destination(raw, output)
        before = inspect_glb(raw)
        _require(before["uv_primitives"] in (0, before["primitives"]), 3, "partial_uv_unsupported")
        record["raw_sha256"] = before["sha256"]
        _hash_match(before["sha256"], expected_source_sha256, "source_stale")
        _require((method == "identity" and target_faces == before["faces"]) or
                 (method != "identity" and target_faces < before["faces"]), 2, "target_not_reduction")
        mesh_policy = policy["mesh_policy"]
        _require(type(mesh_policy.get("sample_budget")) is int and
                 mesh_policy["sample_budget"] >= max(4 * before["faces"], before["sample_population"]) and
                 type(mesh_policy.get("raster_resolution")) is int and
                 mesh_policy["raster_resolution"] >= MIN_RASTER_RESOLUTION,
                 2, "measurement_policy_weakened")
        record["parameters"].update(minimum_sample_budget=max(4 * before["faces"], before["sample_population"]),
                                     minimum_raster_resolution=MIN_RASTER_RESOLUTION)
        _require(type(allow_test_comparator) is bool, 2, "invalid_test_flag")
        if comparator is None:
            try:
                comparator = importlib.import_module("tools.mesh.api").compare_meshes
            except (ImportError, AttributeError) as exc:
                raise TopologyError(3, "regression_evaluator_unavailable") from exc
        elif not allow_test_comparator:
            try:
                production = importlib.import_module("tools.mesh.api").compare_meshes
            except (ImportError, AttributeError):
                production = None
            _require(comparator is production, 3, "untrusted_evaluator")
        module = sys.modules.get(comparator.__module__)
        _require(module is not None and getattr(module, "__file__", None) is not None, 3, "evaluator_identity_unknown")
        record["evaluator"] = {"module": comparator.__module__, "callable": comparator.__qualname__,
                               "module_sha256": _file_hash(Path(module.__file__)),
                               "test_only": allow_test_comparator}
        worker = Path(__file__).with_name("topology_worker.py")
        executable = shutil.which(blender) if method != "identity" or targets else None
        _require(executable is not None or (method == "identity" and not targets), 3, "blender_unavailable")
        toolchain = {"worker_sha256": digest(read_bytes(worker)),
                     "host_sha256": digest(read_bytes(Path(__file__))),
                     "inventory_sha256": digest(read_bytes(Path(__file__).with_name("topology_glb.py"))),
                     "blender_sha256": _file_hash(Path(executable)) if executable else None,
                     "kernel_sha256": _file_hash(Path(core.__file__)), "evaluator": record["evaluator"]}
        with tempfile.TemporaryDirectory(prefix=".topology-", dir=output.parent) as scratch:
            _write(Path(scratch) / "owner.json", canonical({"pid": os.getpid(), "uid": os.getuid(),
                   "output": str(output), "recovery": "manual_only_after_owner_exit"}).encode())
            stage = Path(scratch) / "publish"
            stage.mkdir()
            _write(stage / "raw.glb", read_bytes(raw))
            _hash_match(_file_hash(stage / "raw.glb"), before["sha256"], "source_stale")
            artifacts, checks = [], []
            record["checks"] = checks
            master = stage / "master.glb"
            for index, target in enumerate([target_faces] + targets):
                role = "master" if index == 0 else "LOD%d" % index
                candidate = stage / (role + ".glb")
                source = stage / "raw.glb" if index == 0 else master
                source_info = before if index == 0 else inspect_glb(master)
                operation = method if index == 0 else "decimate"
                if operation == "identity":
                    _write(candidate, read_bytes(source))
                else:
                    _require(target < source_info["faces"], 5, "face_budget_unmet_or_fake_lod")
                    plan = {"source": str(source), "candidate": str(candidate), "method": operation,
                            "target_faces": quad_target if operation == "quadriflow" else target, "seed": seed}
                    plan_path = Path(scratch) / (role + ".json")
                    _write(plan_path, canonical(plan).encode())
                    argv = SCOPE_ARGV + [executable, "-b", "--factory-startup", "--threads", "1",
                                        "--python-exit-code", "2", "--python", str(worker), "--", str(plan_path)]
                    result, text = runner(argv, _remaining(deadline))
                    _check_worker(result, text)
                _require(candidate.is_file() and not candidate.is_symlink(), 3, "candidate_missing")
                after = inspect_glb(candidate)
                _require(mesh_policy["sample_budget"] >= after["sample_population"], 2, "measurement_policy_weakened")
                check = {"role": role, "requested_faces": target, "achieved_faces": after["faces"],
                         "source_faces": source_info["faces"], "candidate_sha256": after["sha256"]}
                checks.append(check)
                _check_preservation(before, after)
                check["uv_sha256"] = after["uv_sha256"]
                if operation != "identity":
                    _check_reduction(source_info, after, target)
                if index > 1:
                    _require(after["faces"] < checks[-2]["achieved_faces"], 5, "lod_achieved_not_decreasing")
                _remaining(deadline)
                code, evidence = _bounded_compare(comparator, stage / "raw.glb", candidate,
                                                  policy["mesh_policy"], _remaining(deadline))
                _remaining(deadline)
                check.update(rc=code, metrics=evidence.get("metrics") if isinstance(evidence, dict) else None,
                             failed=evidence.get("failed") if isinstance(evidence, dict) else None)
                _gate_regression(code, evidence, before["sha256"], after["sha256"], policy)
                check["evaluator_toolchain_sha256"] = evidence["toolchain_sha256"]
                evidence_path = role + ".regression.json"
                _write(stage / evidence_path, canonical(evidence).encode())
                check["metrics_receipt_sha256"] = json_digest(evidence)
                artifacts.append({"role": role, "path": role + ".glb", "sha256": after["sha256"],
                                  "source_sha256": source_info["sha256"], "requested_faces": target,
                                  "achieved_faces": after["faces"], "metrics_receipt_path": evidence_path,
                                  "metrics_receipt_sha256": json_digest(evidence)})
                if index == 0:
                    _write(stage / "LOD0.glb", read_bytes(master))
            # Rehash the complete bundle and original immediately before commit.
            _hash_match(_file_hash(raw), before["sha256"], "source_stale")
            _hash_match(_file_hash(stage / "raw.glb"), before["sha256"], "scratch_raw_stale")
            for artifact in artifacts:
                _hash_match(_file_hash(stage / artifact["path"]), artifact["sha256"], "candidate_stale")
                _hash_match(_file_hash(stage / artifact["metrics_receipt_path"]),
                            artifact["metrics_receipt_sha256"], "regression_receipt_stale")
            _hash_match(_file_hash(stage / "LOD0.glb"), artifacts[0]["sha256"], "lod0_stale")
            toolchain["evaluator_toolchains"] = sorted({check["evaluator_toolchain_sha256"] for check in checks})
            record["toolchain_sha256"] = json_digest(toolchain)
            if expected_toolchain_sha256 is not None:
                _hash_match(record["toolchain_sha256"], expected_toolchain_sha256, "toolchain_stale")
            lod0 = dict(artifacts[0], role="LOD0", path="LOD0.glb", source_sha256=artifacts[0]["sha256"])
            artifacts.append(lod0)
            _remaining(deadline)
            record.update(rc=0, status="OK", artifacts=artifacts, checks=checks,
                          candidate_sha256=artifacts[0]["sha256"])
            record["wall_ms"] = (time.monotonic_ns() - started) // 1_000_000
            _validate_receipt(record)
            _write(stage / ".receipt.pending", canonical(record).encode())
            os.replace(stage / ".receipt.pending", stage / "receipt.json")
            stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(stage_fd)
            finally:
                os.close(stage_fd)
            parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            renamed = False
            try:
                _publish_directory(stage, output)
                renamed = True
                os.fsync(parent_fd)
            except BaseException:
                if renamed:
                    _publish_directory(output, stage)
                raise
            finally:
                os.close(parent_fd)
        return 0, record
    except af_proc.SignalRefusal as exc:
        rc, reason = exc.rc, exc.code
    except TopologyError as exc:
        rc, reason = exc.rc, exc.reason
    except subprocess.TimeoutExpired:
        rc, reason = 6, "deadline_exceeded"
    except Exception:
        rc, reason = 2, "io_or_contract_error"
    record.update(rc=rc, status=STATUS[rc], reason_codes=[reason], artifacts=[],
                  candidate_sha256=None, wall_ms=(time.monotonic_ns() - started) // 1_000_000)
    return rc, record


def _file_hash(path):
    import hashlib
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--method", choices=("identity", "decimate", "quadriflow"), required=True)
    parser.add_argument("--target-faces", type=int, required=True)
    parser.add_argument("--quadriflow-target-quads", type=int)
    parser.add_argument("--lod-target", type=int, action="append", default=[])
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--toolchain-sha256")
    try:
        args = parser.parse_args(argv)
        policy = json.loads(read_bytes(args.policy), object_pairs_hook=_pairs,
                            parse_float=lambda _: (_ for _ in ()).throw(TopologyError(2, "non_integer_number")))
        rc, receipt = run(args.source, args.out_dir, method=args.method, target_faces=args.target_faces,
                          seed=args.seed, policy=policy, expected_source_sha256=args.source_sha256,
                          lod_targets=args.lod_target, timeout_s=args.timeout, blender=args.blender,
                          quadriflow_target_quads=args.quadriflow_target_quads,
                          expected_toolchain_sha256=args.toolchain_sha256)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    except TopologyError as exc:
        rc, receipt = exc.rc, exc.as_dict()
    except Exception:
        rc, receipt = 2, {"rc": 2, "status": "ERROR", "reason_codes": ["policy_invalid"]}
    print(canonical(receipt), end="")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
