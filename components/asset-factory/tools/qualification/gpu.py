"""Trusted host inputs and retained evidence for the bounded native GPU path."""

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable

from asset_factory import core, proc
from .manifest import JSON_LIMIT, SHA256, Refusal, fields
from .offline import GPU_SCOPE_LIMITS as SCOPE_LIMITS, MAX_HANDOFF_AGE_NS, validate_gpu_scope
from .sandbox import validate_devices

AUTHORITY_SCHEMA: str = "af.qualification-gpu-authority/v1"
GPU_LOG_NAMES = frozenset({
    "gpu-scope.json", "gpu-scope-exec.json", "gpu-devices.json", "gpu-authority.json",
    "gpu-release.json", "gpu-permit.json",
})
AUTHORITY_FIELDS = (
    "schema candidate_sha256 unit permit_sha256 input_sha256 seed admitted_monotonic_ns boot_id gpu_uuid "
    "authorized_monotonic_ns expires_monotonic_ns peer_pid peer_start_ticks"
)


@dataclass(frozen=True)
class GpuLaunch:
    """Host-owned configuration, separate from a candidate's pinned manifest."""

    devices: list
    authorize: Callable[[proc.Job, dict, Path, float], tuple[dict, dict]]
    release: Callable[[str], dict]
    gpu_uuid: str


def validate_handoff(handoff, scope, unit, candidate_sha256):
    fields(handoff, AUTHORITY_FIELDS, "gpu_authority_invalid")
    try:
        validate_gpu_scope(scope, unit)
    except RuntimeError as exc:
        raise Refusal(3, "gpu_scope_unproven") from exc
    if (handoff["schema"] != AUTHORITY_SCHEMA or handoff["unit"] != unit or
            handoff["candidate_sha256"] != candidate_sha256 or
            any(type(handoff[key]) is not str or SHA256.fullmatch(handoff[key]) is None
                for key in ("candidate_sha256", "permit_sha256", "input_sha256")) or
            type(handoff["seed"]) is not int or handoff["seed"] not in (42, 43, 45) or
            type(handoff["admitted_monotonic_ns"]) is not int or handoff["admitted_monotonic_ns"] <= 0 or
            type(handoff["peer_pid"]) is not int or handoff["peer_pid"] != scope["pid"] or
            type(handoff["peer_start_ticks"]) is not int or handoff["peer_start_ticks"] != scope["start_ticks"] or
            type(handoff["boot_id"]) is not str or not handoff["boot_id"] or
            type(handoff["gpu_uuid"]) is not str or not handoff["gpu_uuid"]):
        raise Refusal(4, "gpu_authority_binding_changed")
    validate_exec_freshness(handoff, handoff["authorized_monotonic_ns"])
    return handoff


def validate_job_peak(run, scope):
    try:
        validate_gpu_scope(scope, run["unit_name"])
    except RuntimeError as exc:
        raise Refusal(3, "gpu_scope_unproven") from exc
    peak = run["cgroup_peak_bytes"]
    if (run["cgroup_peak_source"] != "systemd_job" or run["cleanup_proven"] is not True or
            type(peak) is not int or not 0 < peak <= SCOPE_LIMITS["maximum"]):
        raise Refusal(3, "gpu_job_supervision_unproven")


def wait_for_scope(path, unit, deadline):
    """The trusted service publishes this before it can enter the GPU sandbox."""
    while not path.exists():
        if time.monotonic() >= deadline:
            raise Refusal(3, "gpu_scope_unproven")
        time.sleep(0.01)
    try:
        scope = core.load_json(path, max_bytes=JSON_LIMIT)
        validate_gpu_scope(scope, unit)
        if time.monotonic() >= deadline:
            raise RuntimeError("gpu_scope_unproven")
    except (core.ContractError, OSError, RuntimeError) as exc:
        raise Refusal(3, "gpu_scope_unproven") from exc
    return scope


def validate_permit_evidence(permit, handoff):
    scheduler = permit.get("scheduler") if type(permit) is dict else None
    if (type(scheduler) is not dict or permit.get("schema") != "af.trellis-native-permit/v1" or
            core.sha256_json(permit) != handoff["permit_sha256"] or
            permit.get("input_sha256") != handoff["input_sha256"] or
            permit.get("seed") != handoff["seed"] or permit.get("pipeline_type") != "512" or
            scheduler.get("admitted_monotonic_ns") != handoff["admitted_monotonic_ns"] or
            scheduler.get("boot_id") != handoff["boot_id"]):
        raise Refusal(4, "gpu_permit_binding_changed")


def validate_release(release, unit, handoff):
    fields(release, "unit permit_sha256 fence_removed compute_idle gpu_uuid", "gpu_release_invalid")
    if (release["unit"] != unit or release["gpu_uuid"] != handoff["gpu_uuid"] or
            release["permit_sha256"] != handoff["permit_sha256"] or
            release["fence_removed"] is not True or release["compute_idle"] is not True):
        raise Refusal(3, "gpu_release_unproven")


def validate_device_witnesses(expected, pinned, mounted):
    try:
        matches = validate_devices(pinned) == validate_devices(mounted) == expected
    except OSError as exc:
        raise Refusal(4, "gpu_device_witness_changed") from exc
    if not matches:
        raise Refusal(4, "gpu_device_witness_changed")


def validate_reobserved_scope(initial, observed, unit):
    try:
        validate_gpu_scope(observed, unit)
    except RuntimeError as exc:
        raise Refusal(4, "gpu_scope_changed") from exc
    if observed != initial:
        raise Refusal(4, "gpu_scope_changed")
    return observed


def validate_exec_freshness(handoff, executed_ns):
    admitted = handoff["admitted_monotonic_ns"]
    authorized = handoff["authorized_monotonic_ns"]
    expires = handoff["expires_monotonic_ns"]
    if (any(type(value) is not int for value in (admitted, authorized, executed_ns, expires)) or
            not 0 < admitted <= authorized <= executed_ns <= expires or
            expires != admitted + MAX_HANDOFF_AGE_NS):
        raise Refusal(4, "gpu_exec_stale")


def reconcile_gpu_evidence(folder, run, policy, candidate_sha256, output=None):
    fields(policy, "devices devices_sha256 gpu_uuid", "gpu_device_policy_invalid")
    expected = validate_devices(policy["devices"])
    pinned = core.load_json(folder / "gpu-devices.json", max_bytes=JSON_LIMIT)
    entry = core.load_json(folder / "supervisor.stdout", max_bytes=JSON_LIMIT)
    fields(entry, "mounted_devices authority_sha256 boot_id executed_monotonic_ns", "gpu_entry_invalid")
    validate_device_witnesses(expected, pinned, entry["mounted_devices"])
    if policy["devices_sha256"] != core.sha256_json(expected):
        raise Refusal(4, "gpu_device_witness_changed")
    scope = core.load_json(folder / "gpu-scope.json", max_bytes=JSON_LIMIT)
    validate_job_peak(run, scope)
    observed = core.load_json(folder / "gpu-scope-exec.json", max_bytes=JSON_LIMIT)
    validate_reobserved_scope(scope, observed, run["unit_name"])
    handoff = core.load_json(folder / "gpu-authority.json", max_bytes=JSON_LIMIT)
    validate_handoff(handoff, scope, run["unit_name"], candidate_sha256)
    if entry["authority_sha256"] != core.sha256_json(handoff) or entry["boot_id"] != handoff["boot_id"]:
        raise Refusal(4, "gpu_authority_binding_changed")
    validate_exec_freshness(handoff, entry["executed_monotonic_ns"])
    if handoff["gpu_uuid"] != policy["gpu_uuid"]:
        raise Refusal(4, "gpu_device_witness_changed")
    permit = core.load_json(folder / "gpu-permit.json", max_bytes=JSON_LIMIT)
    validate_permit_evidence(permit, handoff)
    release = core.load_json(folder / "gpu-release.json", max_bytes=JSON_LIMIT)
    validate_release(release, run["unit_name"], handoff)
    if output is not None:
        measurement = core.load_json(Path(output) / "result.json", max_bytes=JSON_LIMIT)
        phases = measurement.get("stage_wall_ns")
        if (type(phases) is not dict or set(phases) !=
                {"import", "weight_load", "conditioning", "sampling", "decode", "write"} or
                any(type(ns) is not int or ns <= 0 for ns in phases.values())):
            raise Refusal(3, "gpu_phase_telemetry_unproven")
        if any(measurement.get(output_key) != handoff[key] for output_key, key in (
                ("native_permit_sha256", "permit_sha256"), ("input_sha256", "input_sha256"),
                ("seed", "seed"))):
            raise Refusal(4, "gpu_output_authority_mismatch")
        outputs = run["outputs"]
        if (measurement.get("output_sha256") != outputs.get("raw.glb", {}).get("sha256") or
                measurement.get("model_native") != {
                    "path": "model-native.npz", "sha256": outputs.get("model-native.npz", {}).get("sha256")
                }):
            raise Refusal(4, "gpu_output_digest_mismatch")
