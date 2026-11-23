"""Digest-approved offline runner -> validated, atomic af.rig/v1 generation.

The runner protocol is OUR boundary, not an inferred UniRig API: the approved
Python bridge accepts --request FILE --output-dir NEW_DIR and emits rigged.glb
there. It must complete skeleton, skin and merge stages, without downloads.
No such runtime is shipped or qualified. The default provider is blocked.

The approval digest is an explicit trusted-caller argument; an input file's
approved:true flag alone never grants execution. Licence/scheduler decisions
are upstream evidence. This module does not create a model lease.
"""
from __future__ import annotations
from asset_factory import proc as af_proc

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from asset_factory import core
from asset_factory.contracts import validate_provider, validate_slot
from .build.build import EXTENSIONS, contained, preflight
from .common import Refusal, ROOT, digest, integer, load, outcome, require, validate
from .gate import MAX_GLB_BYTES, inspect_bytes
from .publication import publish_directory

MAX_LOG_BYTES = 2**20  # [TARGET: bounded runtime diagnostic, DESIGN_CHOICE]


def admit(request, provider, recipe, *, approval, approved_build_sha256,
          build_inputs_root, weights_root, decision, factory, territory_policy, admission):
    """All authorization and pin checks precede any subprocess or output write."""
    validate_slot(request, "rig", "request")
    validate_provider(provider)
    require(provider["id"] == request["provider_id"] == "unirig" and
            provider["slot"] == "rig" and request["provider_revision"] == provider["revision"],
            4, "provider_stale")
    require(approval is not None and digest(approved_build_sha256), 3, "approved_build_missing")
    require(core.sha256_json(approval) == approved_build_sha256, 4, "approval_stale")
    validate(approval, "approval")
    require(approval["approved"] is True, 5, "build_not_approved")
    require(provider["runnable"] is True, 5, "provider_blocked")
    require(approval["provider_sha256"] == core.sha256_json(provider) and
            approval["recipe_sha256"] == core.sha256_json(recipe), 4, "build_stale")
    prepared = preflight(recipe, build_inputs_root, weights_root=weights_root)
    require(prepared["rc"] == 0, prepared["rc"], prepared["reason_codes"][0] if prepared["rc"] else "")
    require(approval["build_inputs_sha256"] == core.sha256_json(prepared), 4, "build_inputs_stale")
    require(type(approval["image_digest"]) is str and approval["image_digest"].startswith("sha256:") and
            digest(approval["image_digest"][7:]), 2, "image_digest_invalid")
    # Each capability is the hash of a measured probe authenticated by preflight.
    for name in EXTENSIONS:
        require(approval["capabilities"].get(name) == recipe["native_extensions"][name],
                3, "capability_unqualified")
    qualify = contained(build_inputs_root, approval["qualification"]["path"])
    require(qualify.is_file() and core.sha256_file(qualify) == approval["qualification"]["sha256"],
            4, "qualification_stale")
    q = load(qualify)
    validate(q, "qualification")
    require(type(q.get("rc")) is int and q["rc"] == 0 and q.get("recipe_sha256") == core.sha256_json(recipe)
            and q.get("image_digest") == approval["image_digest"]
            and integer(q.get("peak_rss_bytes"), 1, 2**63-1) and
            integer(q.get("wall_ms"), 1, 2**63-1), 3, "runtime_unqualified")
    peak_gib = provider["runtime"]["ram_peak_gb_measured"]
    require(integer(peak_gib, 1, 1024) and
            peak_gib == (q["peak_rss_bytes"] + 2**30-1)//2**30, 3, "peak_unmeasured")
    peak_ref = provider["runtime"]["ram_peak_measurement_ref"]
    require(peak_ref == approval["qualification"], 4, "peak_reference_stale")
    # Rebind licence evidence to the current user declaration; no default use or territory.
    require(decision is not None and factory is not None and territory_policy is not None,
            3, "licence_evidence_missing")
    require(type(decision.get("rc")) is int and decision["rc"] == 0 and
            decision.get("allowed") is True and decision.get("verdict") == "PASS" and
            decision.get("operation") == "rig" and decision.get("provider_id") == provider["id"],
            5, "licence_refused")
    require(decision.get("provider_sha256") == core.sha256_json(provider) and
            decision.get("configuration_sha256") == core.sha256_json(factory) and
            decision.get("policy_sha256") == core.sha256_json(territory_policy) == request["context"]["policy_sha256"]
            and type(factory.get("commercial_use")) is bool and
            decision.get("commercial_use") is factory["commercial_use"] is request["context"]["commercial_use"]
            and decision.get("jurisdiction") == factory.get("jurisdiction") == request["context"]["jurisdiction"],
            4, "licence_stale")
    require(admission is not None, 3, "scheduler_admission_missing")
    validate(admission, "admission")
    require(type(admission.get("rc")) is int and admission["rc"] == 0 and
            admission.get("request_sha256") == core.sha256_json(request) and
            admission.get("provider_sha256") == core.sha256_json(provider) and
            admission.get("lease_held") is True, 3, "scheduler_admission_unproven")
    require(request["params"].get("synthetic_fixture") is True,
            3, "container_runtime_unqualified")
    require(request["params"].get("device", "cpu") == "cpu",
            3, "gpu_runtime_unqualified")
    runner = contained(build_inputs_root, approval["runner"]["path"])
    require(runner.is_file() and runner.suffix == ".py" and
            core.sha256_file(runner) == approval["runner"]["sha256"], 4, "runner_stale")
    return runner


def _kill_group(pid):
    try:
        af_proc.signal_group(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def invoke(runner, request_path, output_path, scratch, timeout_s, weights_root):
    """Bounded service with process RSS measured inside the approved runner."""
    start = time.monotonic_ns()
    with (scratch / "runner.log").open("wb") as log:
        rss_path = scratch / "runtime-rss.json"
        helper = Path(__file__).with_name('python_metrics.py')
        job = af_proc.popen([sys.executable, "-I", str(helper), str(rss_path), str(runner), "--request", str(request_path),
            "--output-dir", str(output_path), "--weights-root", str(Path(weights_root).resolve())],
            cwd=scratch, env={"PATH": os.defpath, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
            "PIP_NO_INDEX": "1", "PYTHONDONTWRITEBYTECODE": "1", "CUDA_VISIBLE_DEVICES": ""},
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        try:
            while af_proc.poll_exit(job) is None:
                require((time.monotonic_ns()-start) <= timeout_s*10**9, 6, "deadline_exceeded")
                require(os.fstat(log.fileno()).st_size <= MAX_LOG_BYTES, 6, "runtime_log_budget")
                time.sleep(.01)
        finally:
            job.stop()
    output = (scratch / "runner.log").read_bytes()
    require(len(output) <= MAX_LOG_BYTES, 6, "runtime_log_budget")
    require(job.returncode == 0, 5 if job.returncode in (1, 5) else 2, "runtime_failed")
    require(not any(marker in output for marker in (b"Traceback (most recent call last)", b"ERROR:", b"Error:")),
            5, "runtime_error_in_log")
    try:
        measured = core.load_json(rss_path, max_bytes=4096)
        peak = measured['peak_rss_bytes']
        require(type(peak) is int and peak > 0, 3, "runtime_peak_unmeasured")
    except (OSError, ValueError, KeyError, TypeError, core.ContractError) as exc:
        raise Refusal(3, "runtime_peak_unmeasured") from exc
    return {"wall_ms": (time.monotonic_ns()-start)//1000000, "peak_rss_bytes": peak}

def run(request, *, input_root, output_dir, provider, recipe, policy, probes,
        approval=None, approved_build_sha256=None, build_inputs_root=None,
        weights_root=None, decision=None, factory=None, territory_policy=None, admission=None):
    started = time.monotonic_ns()
    scratch, metrics = None, {"wall_ms": None, "peak_rss_bytes": None}
    if type(request) is not dict or type(provider) is not dict:
        return core.ContractError("schema_mismatch").as_dict()
    try:
        validate_slot(request, "rig", "request")
        validate_provider(provider)
    except core.ContractError as exc:
        return exc.as_dict()
    result = {"schema": "af.rig/v1", "kind": "result", "slot": "rig",
              "request_id": request.get("request_id", "invalid"), "provider_id": "unirig",
              "provider_revision": provider.get("revision", "invalid"), "outputs": [], "receipts": [],
              "metrics": metrics, "absence_reasons": {}}
    try:
        runner = admit(request, provider, recipe, approval=approval, approved_build_sha256=approved_build_sha256,
                       build_inputs_root=build_inputs_root, weights_root=weights_root, decision=decision, factory=factory,
                       territory_policy=territory_policy, admission=admission)
        require(probes is not None, 3, "deformation_reference_missing")
        target = Path(output_dir)
        require(not target.exists() and not target.is_symlink() and target.parent.is_dir(),
                2, "new_generation_required")
        item = request["inputs"][0]
        source = contained(input_root, item["path"])
        require(source.is_file() and source.stat().st_size <= MAX_GLB_BYTES, 3, "input_unavailable")
        with source.open("rb") as stream:
            data = stream.read(MAX_GLB_BYTES + 1)
        require(len(data) <= MAX_GLB_BYTES, 6, "input_budget")
        require(hashlib.sha256(data).hexdigest() == item["sha256"], 4, "input_stale")
        scratch = Path(tempfile.mkdtemp(prefix=".unirig-", dir=target.parent))
        (scratch / "input.glb").write_bytes(data)
        staged = {**request, "inputs": [{**item, "path": "input.glb"}]}
        core.atomic_write_json(scratch / "request.json", staged)
        with runner.open("rb") as stream:
            pinned_runner = stream.read(2**20 + 1)
        require(len(pinned_runner) <= 2**20 and hashlib.sha256(pinned_runner).hexdigest() ==
                approval["runner"]["sha256"], 4, "runner_stale")
        (scratch / "runner.py").write_bytes(pinned_runner)
        metrics.update(invoke(scratch / "runner.py", scratch / "request.json", scratch / "runtime", scratch,
                              min(request["context"]["timeout_s"], 1800), weights_root))
        require(integer(metrics.get("peak_rss_bytes"), 1, 2**63-1), 3, "runtime_peak_unmeasured")
        candidate = scratch / "runtime" / "rigged.glb"
        require(candidate.is_file() and candidate.resolve().is_relative_to(scratch.resolve()) and
                candidate.stat().st_size <= MAX_GLB_BYTES, 5, "rigged_output_missing")
        # No service-provided PASS is consumed. Gate the exported bytes again here.
        with candidate.open("rb") as stream:
            rigged = stream.read(MAX_GLB_BYTES + 1)
        require(len(rigged) <= MAX_GLB_BYTES, 6, "output_budget")
        gate, skeleton = inspect_bytes(rigged, policy=policy, probes=probes)
        require(gate["rc"] == 0, gate["rc"], gate["reason_codes"][0] if gate["rc"] else "")
        publication = scratch / "publication"
        publication.mkdir()
        for name, payload in (("raw.glb", data), ("rigged.glb", rigged)):
            with (publication / name).open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        skeleton["input_sha256"] = item["sha256"]
        gate["input_sha256"] = item["sha256"]
        gate["request_sha256"] = core.sha256_json(request)
        gate["approved_build_sha256"] = approved_build_sha256
        validate(skeleton, "skeleton")
        validate(gate, "gate")
        core.atomic_write_json(publication / "skeleton.json", skeleton)
        core.atomic_write_json(publication / "gate.json", gate)
        core.atomic_write_json(publication / "request.json", request)
        for role, name, media in (("rigged_glb", "rigged.glb", "model/gltf-binary"),
                                  ("skeleton", "skeleton.json", "application/json")):
            result["outputs"].append({"role": role, "path": name, "media_type": media,
                                      "sha256": core.sha256_file(publication / name)})
        result["receipts"] = [{"schema": "af.rig-gate/v1", "path": "gate.json",
                               "sha256": core.sha256_file(publication / "gate.json")}]
        result.update(rc=0, status="OK")
        metrics["wall_ms"] = (time.monotonic_ns()-started)//1000000
        validate_slot(result, "rig", "result")
        core.atomic_write_json(publication / "result.json", result)
        publish_directory(publication, target)
    except af_proc.SignalRefusal as exc:
        result.update(rc=exc.rc, status=core.STATUS[exc.rc], outputs=[], receipts=[])
        result["absence_reasons"]["stage"] = exc.code
    except Refusal as exc:
        result.update(rc=exc.rc, status=core.STATUS[exc.rc], outputs=[], receipts=[])
        result["absence_reasons"]["stage"] = exc.code
    except core.ContractError as exc:
        result.update(rc=2, status="ERROR", outputs=[], receipts=[])
        result["absence_reasons"]["stage"] = exc.code
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result.update(rc=2, status="ERROR", outputs=[], receipts=[])
        result["absence_reasons"]["stage"] = "adapter_io_or_metadata"
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    if result["rc"] != 0:
        metrics["wall_ms"] = (time.monotonic_ns()-started)//1000000
    if metrics["peak_rss_bytes"] is None:
        result["absence_reasons"]["peak_rss_bytes"] = "runtime_not_measured"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--input-root", required=True)
    parser.add_argument("--output-dir", required=True)
    # Deliberately no CLI approval override: orchestration must pass verified
    # lease, licence and operator approval bindings through the library API.
    args = parser.parse_args(argv)
    try:
        result = run(load(args.request), input_root=args.input_root, output_dir=args.output_dir,
                     provider=load(ROOT / "provider.json"), recipe=load(ROOT / "build" / "recipe.json"),
                     policy=load(ROOT / "gate-policy.json"), probes=None)
    except (core.ContractError, OSError):
        result = core.ContractError("metadata_invalid").as_dict()
    return core.emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
