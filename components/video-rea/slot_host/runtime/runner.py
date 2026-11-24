# SPDX-License-Identifier: AGPL-3.0-only
"""Out-of-process supervision for bound executables.

The runner never signals a process group.  It launches the argv vector
directly (no shell), records the single PID it started, and on shutdown
terminates exactly that PID (SIGTERM, then SIGKILL only if it survives).
Interruptions leave failed attempts retained and produce an
UNCERTAIN_OUTCOME control state for receipt reconciliation.
"""

import copy
import hashlib
import math
import json
import os
import re
import subprocess
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryFile

from . import executable_custody
from . import process_lifecycle
from . import result_custody
from .binding import Binding, BindingRejected, verify_binding
from .conformance_authority import ConformanceLiveAuthority
from .live_park import LIVE_EXECUTION_PARKED, _DEFAULT_AUTHORITY, _FixtureLiveAuthority, authorize_live, live_admission
from .receipts import _ABSTAIN_REASONS, build_attempt_receipt, finalize_receipt, publish_retained_receipt, receipt_digest
from .receipts import evidence_accepted as _evidence_accepted
from .replay_lineage import resolve_replay_lineage
from .retention import RetentionAttempt
from . import evidence_projection

# Status pairs for rea.slot_result/v1 exchange.
_EXCHANGE_STATUS = {
    0: ("OK", "OK"),
    2: ("ERROR", "ERROR"),
    3: ("INCOMPLETE", "INCOMPLETE"),
    4: ("STALE", "STALE"),
    5: ("FAIL", "FAIL"),
    6: ("BUDGET_EXCEEDED", "BUDGET_EXCEEDED"),
}
_PAIRED_RC = {0, 2, 3, 4, 5, 6}
_LEVEL_RANK = {"none": 0, "cdp_atspi": 1, "vision": 2}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXCHANGE_SCHEMA = "rea.slot_result/v1"

# Omission permits inherited-pipe attribution only for an authorized fixture.
_OMITTED_WITNESS = result_custody.OMITTED_WITNESS


class _LaunchParked(Exception):
    """Authority was withdrawn before the child could start."""


def _proc_starttime(pid: int) -> int | None:
    """Live /proc starttime; owned by process_lifecycle (U09)."""
    return process_lifecycle.proc_starttime(pid)


def _filtered_env(manifest_allowlist: tuple[str, ...] | list[str], host_allowlist: frozenset[str] | set[str], overrides: dict | None = None) -> dict[str, str]:
    """Intersect the manifest allowlist with host policy (issue 4).

    Owned by :mod:`slot_host.runtime.executable_custody` (U08); this
    name stays as a compatibility alias.
    """
    return executable_custody.filtered_env(manifest_allowlist, host_allowlist, overrides)


def _decode_output(data: bytes, limit: int) -> tuple[str, bool]:
    """Bounded diagnostic decoding; owned by result_custody (U09)."""
    return result_custody.decode_output(data, limit)


def _collect_output(proc: subprocess.Popen, deadline: float, cap: int) -> tuple[bytes, bytes, bool, bool]:
    """Incrementally drain stdout/stderr until EOF, deadline, or cap.

    Compatibility alias owned by :mod:`slot_host.runtime.process_lifecycle`
    (U09).  Kept as a named wrapper so tests can patch
    ``slot_host.runtime.runner._collect_output``.
    """
    return process_lifecycle.collect_output(proc, deadline, cap)


class _DuplicateKeyError(ValueError):
    pass


def _object_pairs_no_dup(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise _DuplicateKeyError(f"duplicate JSON key {k!r}")
        obj[k] = v
    return obj


def _reject_constant(value):
    raise ValueError(f"non-finite JSON constant {value!r}")


def _strict_single_json_object(stdout_bytes: bytes):
    """Strict UTF-8 single JSON object; owned by result_custody (U09)."""
    return result_custody.strict_single_json_object(stdout_bytes)


def _exchange_documents(stdout_bytes: bytes, stderr_bytes: bytes) -> list[dict]:
    """Single pinned exchange; owned by result_custody (U09)."""
    return result_custody.exchange_documents(stdout_bytes, stderr_bytes)


def _exchange_doc_valid(value) -> bool:
    """Pinned result document check; owned by result_custody (U09)."""
    return result_custody.exchange_doc_valid(value)


def _operation_for_binding(binding: Binding) -> str:
    op_id = (binding.operation_id or "").lower()
    if op_id == "replay":
        return "replay"
    if op_id == "compare":
        return "compare"
    if op_id == "record":
        return "record"
    # Fall back to role: verify -> compare, replay -> replay.
    if binding.role == "verify":
        return "compare"
    if binding.role == "replay" and op_id in ("replay",):
        return "replay"
    return "record"


def _slot_role_for(binding: Binding, slot_role: str | None) -> str:
    if slot_role is not None:
        return slot_role
    if binding.role:
        return binding.role
    return "replay" if binding.operation_id == "replay" else "verify" if binding.operation_id == "compare" else "replay"


def _extract_observed_pid(stdout_bytes: bytes, stderr_bytes: bytes, started_pid: int) -> tuple[int, int | None]:
    """Provider-declared observed PID; owned by result_custody (U09)."""
    return result_custody.extract_observed_pid(stdout_bytes, stderr_bytes, started_pid)


def _extract_exchange_meta(stdout_bytes: bytes, stderr_bytes: bytes) -> dict | None:
    """Single pinned exchange or None; owned by result_custody (U09)."""
    return result_custody.extract_exchange_meta(stdout_bytes, stderr_bytes)


def _attach_shared_records(wrapper: dict, *, receipt: dict, binding: Binding, launched: bool,
                           command: dict, scope: dict, process: dict | None = None,
                           launch_observation: dict | None = None,
                           artifact_payload: bytes | None = None, artifact_sha256: str | None = None,
                           cleanup_state: str = "confirmed_retired",
                           cleanup_retirement_sha256: str | None = None,
                           abort_reason: str = "LIVE_EXECUTION_PARKED") -> dict:
    """Attach the offline v0.3 bundle (started + terminal + index).

    Never raises: an unrepresentable attempt (compare without source
    attempts, unmappable vision ticket, oversized payload) yields
    ``shared_records=None`` and clears acceptance for v0.3 bindings. Compare
    receipts project through :func:`evidence_projection.build_bundle`
    with their reference/candidate attempt ids by the caller.
    """
    try:
        if binding.retention_required or (receipt.get("attempt") or {}).get("operation") == "compare":
            wrapper["shared_records"] = None
            return wrapper
        wrapper["shared_records"] = evidence_projection.build_bundle(
            receipt, launched=launched,
            installation_ref=f"installation/{binding.slot_id}",
            command=command, scope=scope,
            process=process, launch_observation=launch_observation,
            artifact_payload=artifact_payload, artifact_sha256=artifact_sha256,
            cleanup_state=cleanup_state, cleanup_retirement_sha256=cleanup_retirement_sha256,
            abort_reason=abort_reason,
        )
    except Exception:
        wrapper["shared_records"] = None
        if binding.shared_schema_ref in (evidence_projection.V03_REF, evidence_projection.V03_ID):
            wrapper["evidence_accepted"] = False
    return wrapper


def run_bound(
    binding: Binding,
    argv: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict | None = None,
    timeout_s: float | None = None,
    max_output_bytes: int | None = None,
    slot_role: str | None = None,
    request_id: str = "req-fixture-0001",
    attempt_id: str = "att-fixture-0001",
    started_at: str = "2026-11-24T10:00:00Z",
    vision_pool=None,
    front: str = "ViewPoint_REA",
    capsule_perception_ceiling: str = "vision",
    perception_level: str | None = None,
    host_env_allowlist: frozenset[str] | set[str] = frozenset({"PATH"}),
    attempt_store=None,
    parent_episode_id: str | None = None,
    reference_episode_id: str | None = None,
    candidate_episode_id: str | None = None,
    producer_witness: dict | None = _OMITTED_WITNESS,
    tape_sha256: str | None = None,
    replay: dict | None = None,
    tape_resolver=None,
    media: dict | None = None,
    timeline: dict | None = None,
    shared_receipt_ref: str | None = None,
    live_authority=None,
) -> dict:
    """Execute argv under binding and return a supervised wrapper.

    Returns {"receipt": rea.slot_evidence_profile/v1, "custody": {...},
    "evidence_accepted": bool, "stdout":..., ...}.  The receipt alone is
    schema-valid; custody and captured bytes live in the wrapper (issue 3).
    A wrong process (observed pid != started pid) or changed binary never
    yields an accepted observation (issue 1).

    The admitted CLI replay fixture reads one UTF-8 JSON object to EOF on
    stdin. Its fields are the verified parent ID and replay block fields.
    """
    if live_authority is None:
        live_authority = _DEFAULT_AUTHORITY
    fixture_launch = type(live_authority) is _FixtureLiveAuthority
    conformance_launch = type(live_authority) is ConformanceLiveAuthority
    if type(argv) is not list or any(type(w) is not str for w in argv):
        raise ValueError("argv must be a list of strings")
    if timeout_s is not None and (
        type(timeout_s) not in (int, float) or timeout_s <= 0
        or (type(timeout_s) is float and not math.isfinite(timeout_s))
    ):
        raise ValueError("timeout_s must be finite and positive")
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes < 1):
        raise ValueError("max_output_bytes must be a positive int")
    if producer_witness is not _OMITTED_WITNESS and producer_witness is not None and not isinstance(producer_witness, dict):
        raise ValueError("producer_witness must be an object")
    operation = _operation_for_binding(binding)
    if (operation != "replay" and tape_sha256 is not None
            and (type(tape_sha256) is not str or _SHA256_RE.fullmatch(tape_sha256) is None)):
        raise ValueError("tape_sha256 must be a sha256 hex digest")

    if binding.execution_profile == "offline_fixture":
        raise BindingRejected("profile_not_admitted", "offline_fixture does not start processes")

    # Cap caller ceilings by operation limits (issue 4).
    effective_timeout = float(binding.timeout_s if timeout_s is None else min(timeout_s, binding.timeout_s))
    effective_max = int(binding.max_output_bytes) if max_output_bytes is None else min(int(max_output_bytes), int(binding.max_output_bytes))

    replay_input = None
    replay_receipt_fields = {}
    if operation == "replay":
        lineage = resolve_replay_lineage(
            {"parent_episode_id": parent_episode_id, "replay": replay},
            tape_sha256=tape_sha256, tape_resolver=tape_resolver,
        )
        parent_episode_id = lineage["parent_episode_id"]
        tape_sha256 = lineage["tape_sha256"]
        replay = lineage["replay"]
        replay_receipt_fields = {"parent_episode_id": parent_episode_id, "replay": replay}
        replay_input = json.dumps({"parent_episode_id": parent_episode_id, **replay}).encode("utf-8")

    # Perception determination and ceiling (issue 2).
    requested_level = perception_level if perception_level is not None else binding.perception_required
    if requested_level not in _LEVEL_RANK:
        raise ValueError(f"unknown perception_level {requested_level!r}")
    if requested_level not in {binding.perception_required, *binding.perception_optional}:
        return _perception_rejected(
            binding, request_id, attempt_id, started_at, "perception_denied", f"level {requested_level!r} not declared",
            None, capsule_perception_ceiling=capsule_perception_ceiling, **replay_receipt_fields,
        )
    if _LEVEL_RANK[requested_level] > _LEVEL_RANK[capsule_perception_ceiling]:
        return _perception_rejected(
            binding, request_id, attempt_id, started_at, "perception_denied",
            f"requested {requested_level!r} exceeds ceiling {capsule_perception_ceiling!r}",
            None, capsule_perception_ceiling=capsule_perception_ceiling, **replay_receipt_fields,
        )
    if requested_level != "none":
        raise BindingRejected("availability_parked", "owner-pending: T06")

    # Open and retain the attempt before dispatch.
    resolved_slot_role = _slot_role_for(binding, slot_role)
    # Thread the admitted replay profile through the runner (seed/controls
    # must reach the retained receipt; tape stays independent of result).
    if replay is not None:
        if not isinstance(replay, dict):
            raise ValueError("replay must be an object")
        opened_replay = copy.deepcopy(replay)
        _rep_sha = opened_replay.get("input_tape_sha256")
        if tape_sha256 is None and _rep_sha is not None:
            if type(_rep_sha) is not str or _SHA256_RE.fullmatch(_rep_sha) is None:
                raise ValueError("tape_sha256 must be a sha256 hex digest")
            tape_sha256 = _rep_sha
    elif tape_sha256 is not None:
        opened_replay = {"input_tape_sha256": tape_sha256, "determinism_mode": "uncontrolled"}
    else:
        opened_replay = None
    opened = build_attempt_receipt(
        attempt_id=attempt_id,
        request_id=request_id,
        operation=operation,
        slot_role=resolved_slot_role,
        slot_id=binding.slot_id,
        tool_version=binding.tool_version,
        upstream_licence_id=binding.upstream_licence_id,
        source_url=binding.source_url,
        use_disposition=binding.use_disposition,
        provider_id=binding.provider_id,
        provider_revision=binding.provider_revision,
        implementation_sha256=binding.implementation_sha256,
        producer_kind="external_slot",
        producer_id=binding.provider_id,
        producer_version=binding.provider_revision,
        producer_implementation_sha256=binding.implementation_sha256,
        process_bound=False,
        perception_level=requested_level,
        perception_ceiling=capsule_perception_ceiling,
        slot_result=None,
        control_outcome=None,
        started_at=started_at,
        parent_episode_id=parent_episode_id,
        reference_episode_id=reference_episode_id,
        candidate_episode_id=candidate_episode_id,
        replay=opened_replay,
        media=media,
        timeline=timeline,
        shared_schema_ref=binding.shared_schema_ref,
        manifest_sha256=binding.manifest_digest,
    )
    exe = binding.executable_path
    # Re-verify the binary digest immediately before dispatch from a
    # single O_NOFOLLOW open (U08 executable custody) so a binary changed
    # after inspection, a symlink, or a non-regular file produces no
    # accepted observation.  This is the early diagnostic; the
    # authoritative same-fd hash happens in stage_verified_executable.
    try:
        observed_now, source_identity = executable_custody.measure_verified_executable(
            exe, binding.implementation_sha256,
        )
    except executable_custody.ExecutableCustodyError as exc:
        _obs = getattr(exc, "observed_sha256", None) or binding.observed_sha256
        receipt = _rejected_wrapper(binding, opened, request_id, attempt_id, resolved_slot_role, started_at, reason="evidence_binding_rejected", detail=f"cannot verify executable: {exe}: {exc.detail}", attempt_store=None, observed_sha256=_obs)
        return receipt
    if observed_now != binding.observed_sha256:
        receipt = _rejected_wrapper(binding, opened, request_id, attempt_id, resolved_slot_role, started_at, reason="evidence_binding_rejected", detail=f"binary changed before dispatch: {observed_now}", attempt_store=None, observed_sha256=observed_now)
        return receipt
    if os.access(Path(exe), os.X_OK) is False:
        receipt = _rejected_wrapper(binding, opened, request_id, attempt_id, resolved_slot_role, started_at, reason="evidence_binding_rejected", detail=f"executable is not executable: {exe}", attempt_store=None, observed_sha256=observed_now)
        return receipt

    # Reject non-spawnable host_composition without an external adapter (issue 4).
    if binding.operation_binding.get("kind") == "host_composition":
        receipt = _rejected_wrapper(binding, opened, request_id, attempt_id, resolved_slot_role, started_at, reason="evidence_binding_rejected", detail="host_composition cannot spawn a subprocess", attempt_store=None, observed_sha256=observed_now)
        return receipt
    # Enforce required effects: a spawned child needs process.spawn:child,
    # and an empty declaration never enables a spawn.
    if "process.spawn:child" not in (binding.required_effects or ()):
        receipt = _rejected_wrapper(binding, opened, request_id, attempt_id, resolved_slot_role, started_at, reason="evidence_binding_rejected", detail="required_effects missing process.spawn:child", attempt_store=None, observed_sha256=observed_now)
        return receipt
    # Parked alternate execution transports never start a process.
    if binding.transport_kind == "mcp_stdio":
        raise BindingRejected("availability_parked", "alternate execution transport is parked; no process was created")
    # Typed-args custody (C20): a binding that declares typed_args
    # admits only argv matching those positions/types.  This runs with
    # the other integrity diagnostics, before authorize_live.
    try:
        typed_tail = executable_custody.build_typed_tail(binding, argv)
    except executable_custody.ExecutableCustodyError as exc:
        receipt = _rejected_wrapper(binding, opened, request_id, attempt_id, resolved_slot_role, started_at, reason="evidence_binding_rejected", detail=f"typed argv refused: {exc.detail}", attempt_store=None, observed_sha256=observed_now)
        return receipt
    retained = attempt_store if type(attempt_store) is RetentionAttempt else None
    retention_launch = binding.retention_required and retained is not None and retained.configured
    admission = dict(live_authority=live_authority, operation_id=binding.operation_id,
                     request_id=request_id, attempt_id=attempt_id)
    if retention_launch:
        if binding.shared_schema_ref not in (evidence_projection.V03_REF, evidence_projection.V03_ID):
            raise BindingRejected("profile_not_admitted", "retention requires the frozen live record pin")
        if effective_timeout < 0.001:
            raise BindingRejected("profile_not_admitted", "retention requires a positive millisecond limit")
        # Use the authority for the exact scope. The verified start and
        # one-shot claim supply the additional runner-only launch gates.
        admission["readiness_only"] = True
    if not authorize_live(binding, exe, **admission) or (conformance_launch and not callable(attempt_store)):
        return _rejected_wrapper(
            binding, opened, request_id, attempt_id, resolved_slot_role, started_at,
            reason=LIVE_EXECUTION_PARKED, detail="live execution authority is unavailable",
            attempt_store=None, observed_sha256=observed_now,
        )
    workdir = stage_dir = None
    created_scratch = created_stage = False
    full_argv: list[str] = []
    filtered: dict[str, str] = {}
    proc: subprocess.Popen | None = None
    started_pid = bound_starttime = None
    stdout_bytes = stderr_bytes = b""
    timed_out = overflow = False
    lifecycle_exc: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    refusal: tuple[str, str] | None = None
    scratch_removed = stage_removed = False
    retain_terminal = False
    started_record = start_ack = None
    claim_uncertain = False
    local_custody = retained.custody if retention_launch else None
    capture = None

    try:
        if attempt_store is not None:
            if not retention_launch:
                # Legacy fixture storage still receives the opened profile.
                retain_terminal = True
            confirmed = attempt_store(copy.deepcopy(opened))
            if confirmed is False or ((retention_launch or conformance_launch) and confirmed is not True):
                retain_terminal = False
                raise OSError("pre-stage readiness was not confirmed")

        workdir, created_scratch = process_lifecycle.create_run_scratch(cwd)
        stage_dir, created_stage = process_lifecycle.create_stage_dir()

        try:
            if fixture_launch and source_identity != live_authority.source_identity:
                raise executable_custody.ExecutableCustodyError(
                    "source_identity_mismatch", "fixture source object changed",
                )
            snapshot_path = executable_custody.stage_verified_executable(
                exe, binding.implementation_sha256, stage_dir, Path(exe).name,
                source_identity=source_identity,
            )
            filtered = _filtered_env(binding.env_allowlist, host_env_allowlist, env)
            full_argv = executable_custody.build_launch_argv(
                binding, str(snapshot_path), argv, typed_tail=typed_tail,
            )
            snap_fd = executable_custody.open_launch_snapshot(snapshot_path, binding.implementation_sha256)
            try:
                with (TemporaryFile() if replay_input is not None else nullcontext(subprocess.DEVNULL)) as child_stdin:
                    if replay_input is not None:
                        # An already-populated descriptor avoids waiting on a child to read stdin.
                        child_stdin.write(replay_input)
                        child_stdin.seek(0)
                    if retention_launch:
                        if not (created_scratch and created_stage):
                            raise OSError("retention requires a managed launch scope")
                        pinned_paths = {path: _path_identity(path) for path in (workdir, stage_dir, snapshot_path)}
                        started_record = _retention_start(
                            retained, opened, binding, full_argv, filtered, workdir,
                            effective_timeout, effective_max,
                        )
                        if local_custody is not None:
                            local_custody.stage(
                                retained, started_record, snap_fd, full_argv, Path(workdir), filtered, Path(stage_dir),
                            )
                        start_ack = retained.start(started_record)
                        retain_terminal = True
                    with live_admission(binding, **admission) as allowed:
                        if not allowed:
                            raise _LaunchParked
                        if retention_launch:
                            _, current = executable_custody.measure_verified_executable(
                                exe, binding.implementation_sha256,
                            )
                            current_start = _retention_start(
                                retained, opened, binding, full_argv, filtered, workdir,
                                effective_timeout, effective_max,
                            )
                            if (source_identity != current or current_start != started_record or any(
                                _path_identity(path) != identity for path, identity in pinned_paths.items()
                            )):
                                raise OSError("staged invocation changed before launch claim")
                            claim_uncertain = True
                            retained.claim(start_ack)
                            claim_uncertain = False
                        launch_timeout = (started_record["command"]["timeout_ms"] / 1000
                                          if retention_launch else effective_timeout)
                        deadline = process_lifecycle.effective_deadline(launch_timeout)
                        if local_custody is not None:
                            from .retention_witness import CredentialCapture

                            capture = CredentialCapture()
                        proc = subprocess.Popen(
                            full_argv, cwd=str(workdir), env=filtered,
                            stdout=capture.writer if capture is not None else subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=child_stdin, text=False,
                            start_new_session=False, executable=f"/proc/self/fd/{snap_fd}",
                            pass_fds=(snap_fd,),
                        )
                        started_pid = proc.pid
                        bound_starttime = _proc_starttime(started_pid)
                        if capture is not None:
                            capture.writer.close()
                            proc.stdout = capture
                            local_custody.launched(retained, started_pid, bound_starttime, capture)
            finally:
                os.close(snap_fd)
        except _LaunchParked:
            refusal = (LIVE_EXECUTION_PARKED, "live execution authority was withdrawn before spawn")
        except (OSError, ValueError) as exc:
            if proc is not None:
                raise
            refusal = ("evidence_binding_rejected", f"cannot prepare or spawn executable: {exc}")

        if proc is not None:
            stdout_bytes, stderr_bytes, timed_out, overflow = _collect_output(proc, deadline, effective_max)
            if not (timed_out or overflow):
                timed_out = not process_lifecycle.wait_for_exit(proc, deadline)
    except BaseException as exc:
        lifecycle_exc = exc
    finally:
        if proc is not None:
            try:
                if proc.returncode is None:
                    if type(bound_starttime) is int:
                        _terminate_only(started_pid, proc, grace_s=1.0, bound_starttime=bound_starttime)
                    else:
                        # The child handle allows a bounded wait for natural exit.
                        # Only a recorded identity can authorize a signal.
                        if proc.poll() is None:
                            process_lifecycle.wait_for_exit(proc, deadline)
            except BaseException as exc:
                cleanup_errors.append(exc)
                if type(bound_starttime) is int:
                    try:
                        _kill_only(started_pid, proc, bound_starttime=bound_starttime)
                    except BaseException:
                        pass
            finally:
                try:
                    _close_pipes(proc)
                except BaseException as exc:
                    cleanup_errors.append(exc)
        if workdir is not None:
            try:
                scratch_removed, error = process_lifecycle.destroy_run_scratch(workdir, created_scratch)
                if error is not None:
                    cleanup_errors.append(error)
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            stage_removed = process_lifecycle.destroy_stage_dir(stage_dir, created_stage)
            if not stage_removed:
                raise OSError(f"launch staging directory was not removed: {stage_dir}")
        except BaseException as exc:
            cleanup_errors.append(exc)

        if capture is not None:
            capture.close()
            if proc is not None and lifecycle_exc is None and not (timed_out or overflow or cleanup_errors):
                try:
                    local_custody.retired(retained, proc, capture)
                except (OSError, ValueError) as exc:
                    cleanup_errors.append(exc)

    uncertainty = lifecycle_exc is not None or bool(cleanup_errors)
    interruption = next(
        (exc for exc in (lifecycle_exc, *cleanup_errors) if exc is not None and not isinstance(exc, Exception)),
        None,
    )
    cleanup_observed = stage_removed and scratch_removed and (
        proc is None or (type(bound_starttime) is int and proc.returncode is not None)
    )
    if proc is None:
        if claim_uncertain:
            # Inspection is recovery evidence. Even a durable claim never
            # substitutes for the single verified acknowledgement at spawn.
            try:
                retained.inspect(started_record)
            except Exception:
                pass
            uncertainty = True
            reason, detail = "UNCERTAIN_OUTCOME", "the launch claim acknowledgement is unresolved"
        elif retention_launch and started_record is not None and start_ack is None:
            uncertainty = True
            reason, detail = "UNCERTAIN_OUTCOME", "the start acknowledgement is unresolved"
        elif uncertainty:
            error = lifecycle_exc if lifecycle_exc is not None else cleanup_errors[0]
            reason, detail = "UNCERTAIN_OUTCOME", f"launch lifecycle failed: {error}"
        else:
            assert refusal is not None
            reason, detail = refusal
        wrapper = _rejected_wrapper(
            binding, opened, request_id, attempt_id, resolved_slot_role, started_at,
            reason=reason, detail=detail, attempt_store=None, observed_sha256=observed_now,
            cleanup_observed=cleanup_observed,
        )
        if retention_launch:
            wrapper["shared_records"] = None
            if retain_terminal:
                wrapper["custody"]["start_sha256"] = start_ack["start_sha256"]
                wrapper["custody"]["retirement"] = "unknown"
                if not claim_uncertain:
                    retention_interrupt = _finish_retained(
                        wrapper, retained, started_record, proc_identity=None, captured=None,
                        cleanup_observed=cleanup_observed,
                    )
                    if interruption is None:
                        interruption = retention_interrupt
        elif retain_terminal:
            retention_interrupt = _retain_terminal(wrapper, attempt_store, conformance_launch)
            if interruption is None:
                interruption = retention_interrupt
        if interruption is not None:
            raise interruption
        return wrapper

    rc = proc.returncode
    observed_pid, observed_starttime, witness_bound = result_custody.resolve_producer_origin(
        stdout_bytes, stderr_bytes, started_pid, bound_starttime, producer_witness,
        registered_fixture=fixture_launch,
    )
    retained_evidence = None
    if retention_launch:
        try:
            retained_evidence = _observe_retained(
                retained, started_record, (started_pid, bound_starttime), stdout_bytes,
                cleanup_observed=cleanup_observed,
            )
            for field in ("media", "timeline"):
                observation = getattr(retained_evidence, field)
                if observation is not None:
                    if field in opened and opened[field] != observation:
                        raise ValueError(f"independent {field} proof differs from the opened profile")
                    opened[field] = copy.deepcopy(observation)
            observed_pid, observed_starttime = started_pid, bound_starttime
            witness_bound = (
                retained_evidence.attribution_receipt_sha256 is not None
                and retained_evidence.captured_sha256 == hashlib.sha256(stdout_bytes).hexdigest()
                and retained_evidence.cleanup_retirement_sha256 is not None
            )
        except BaseException as exc:
            retained_evidence = None
            witness_bound = False
            if interruption is None and not isinstance(exc, Exception):
                interruption = exc
    stdout_text, stdout_trunc = _decode_output(stdout_bytes, effective_max)
    stderr_text, stderr_trunc = _decode_output(
        stderr_bytes, effective_max - len(stdout_text.encode("utf-8")),
    )
    diagnostics_truncated = overflow or stdout_trunc or stderr_trunc
    rc_int = rc if rc is not None else -1
    exchange = None
    result_bytes = None
    measured_result_sha = None
    if not (uncertainty or timed_out or overflow or diagnostics_truncated or stderr_bytes.strip()):
        exchange = _extract_exchange_meta(stdout_bytes, stderr_bytes)
        result_bytes = result_custody.fixture_result_bytes(exchange)
        if result_bytes is not None:
            measured_result_sha = hashlib.sha256(result_bytes).hexdigest()

    exchange_mismatch = result_custody.exchange_identity_mismatch(
        exchange, request_id=request_id, provider_id=binding.provider_id,
        provider_revision=binding.provider_revision, provider_sha256=binding.provider_sha256,
        execution_profile=binding.execution_profile, implementation_sha256=binding.implementation_sha256,
        role=resolved_slot_role, rc=rc,
    )
    candidate_result = result_custody.build_slot_result(
        exchange, rc, timed_out=timed_out, overflow=overflow,
        interrupted=uncertainty, mismatch=exchange_mismatch,
    )
    exchange_mismatch = (
        candidate_result is None or candidate_result["sha256"] != measured_result_sha
    )
    producer_binding_verified = result_custody.producer_binding_verified(
        started_pid=started_pid, observed_pid=observed_pid, bound_starttime=bound_starttime,
        observed_starttime=observed_starttime, witness_bound=witness_bound,
        cleanup_observed=cleanup_observed, observed_now=observed_now,
        implementation_sha256=binding.implementation_sha256, overflow=overflow,
        timed_out=timed_out, interrupted=uncertainty, exchange_mismatch=exchange_mismatch, rc_int=rc_int,
    )
    slot_result = candidate_result if producer_binding_verified else None

    custody = {
        "producer_binding_verified": producer_binding_verified,
        "artifact_digest_verified": slot_result is not None,
        "cleanup_observed": cleanup_observed,
        "bound_process_id": started_pid,
        "observed_process_id": observed_pid,
        "bound_starttime": bound_starttime,
        "observed_starttime": observed_starttime,
        "timed_out": timed_out,
        "provider_id": binding.provider_id,
        "request_id": request_id,
        "observed_sha256": observed_now,
        "implementation_sha256": binding.implementation_sha256,
        "overflow": overflow,
        "stdout_truncated": diagnostics_truncated,
        "host_result_sha256": result_custody.host_result_sha256(stdout_bytes),
    }
    if fixture_launch:
        custody["offline_fixture"] = True
    elif not producer_binding_verified:
        custody["producer_custody_reason"] = "owner-pending: T03"
    if slot_result is not None:
        custody["result_sha256"] = measured_result_sha
    if tape_sha256 is not None:
        custody["input_tape_sha256"] = tape_sha256
    if interruption is not None:
        custody["interrupted"] = True

    # Build the schema-valid receipt via finalize with custody verification.
    # Finalize recomputes process_bound from custody; verdict only for compare.
    opened_attempt = copy_of_opened(opened)
    if uncertainty or timed_out or not cleanup_observed:
        opened_attempt["attempt"]["control_outcome"] = "UNCERTAIN_OUTCOME"
    verdict: dict | None = None
    if operation == "compare":
        if uncertainty or timed_out or not cleanup_observed:
            reason, detail = "uncertain_outcome", "execution or cleanup was not confirmed"
        elif overflow:
            reason, detail = "slot_budget_exceeded", "host output capture exceeded its limit"
        elif exchange_mismatch:
            reason, detail = "exchange_invalid", "provider exchange custody mismatch"
        elif not producer_binding_verified:
            reason, detail = "custody_unverified", custody.get("producer_custody_reason", "producer binding not verified")
        else:
            reason = {
                "ERROR": "slot_error", "STALE": "slot_stale", "BUDGET_EXCEEDED": "slot_budget_exceeded",
                "INCOMPLETE": "slot_incomplete", "FAIL": "slot_claim_only",
            }.get(slot_result["status"], "custody_unverified")
            detail = "host checks not evaluated in runtime"
        verdict = _compare_verdict(
            binding, binding.manifest_digest, result="abstain", abstain_reason=reason, abstain_detail=detail,
        )

    receipt = finalize_receipt(opened_attempt, slot_result, custody=custody, implementation_sha256=binding.implementation_sha256, verdict=verdict)
    # finalize strips private custody; custody lives in the wrapper.
    evidence_ok = producer_binding_verified and not timed_out and not overflow and slot_result is not None and slot_result.get("status") == "OK" and cleanup_observed
    # Compare verdicts are always abstain until the comparator qualifies a
    # match; record/replay acceptance depends only on custody + OK.
    if operation == "compare":
        evidence_ok = False if verdict and verdict.get("result") == "abstain" else evidence_ok
    _v03_attempt = evidence_projection.wire_attempt_id(attempt_id, retained=retention_launch)
    _launch_obs = {
        "observer_id": "observer/launch",
        "observation_id": f"launch/{_v03_attempt}",
        "receipt_sha256": evidence_projection.record_digest({"attempt_id": attempt_id, "pid": started_pid, "starttime_ticks": bound_starttime}),
        "executable_sha256": binding.implementation_sha256,
        "executed_path": full_argv[0] if full_argv else binding.executable_path,
    }
    artifact_payload = result_bytes if "slot_result" in receipt["attempt"] else None
    wrapper = {
        "receipt": receipt,
        "custody": custody,
        "evidence_accepted": bool(evidence_ok and _evidence_accepted({"receipt": receipt, "custody": custody, "evidence_accepted": evidence_ok})),
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": diagnostics_truncated,
        "rc": rc_int,
        "pid": started_pid,
        "timed_out": timed_out,
        "cleanup_observed": cleanup_observed,
    }
    if retention_launch:
        custody["start_sha256"] = start_ack["start_sha256"]
        custody["retirement"] = "unknown"
        retention_interrupt = _finish_retained(
            wrapper, retained, started_record, proc_identity=(started_pid, bound_starttime),
            captured=stdout_bytes, cleanup_observed=cleanup_observed,
            evidence=retained_evidence, artifact_payload=artifact_payload,
        )
        if interruption is None:
            interruption = retention_interrupt
    else:
        _attach_shared_records(
            wrapper,
            receipt=receipt, binding=binding, launched=True,
            command={"kind": "argv", "arguments": list(full_argv), "cwd": str(workdir),
                     "timeout_ms": max(1, int(effective_timeout * 1000)), "max_output_bytes": int(effective_max),
                     "env_sha256": evidence_projection.env_sha256(filtered)},
            scope={"kind": "posix_child", "id": f"rea-posix-child/{_v03_attempt}"},
            process={"pid": started_pid, "start_id": f"host/start/{started_pid}", "job_id": f"host/job/{started_pid}",
                     "starttime_ticks": bound_starttime} if bound_starttime is not None else None,
            launch_observation=_launch_obs,
            artifact_payload=artifact_payload,
            artifact_sha256=measured_result_sha if artifact_payload is not None else None,
            cleanup_state="confirmed_retired" if cleanup_observed else "unknown",
            cleanup_retirement_sha256=(
                evidence_projection.record_digest({"attempt_id": attempt_id, "cleanup": True, "pid": started_pid})
                if cleanup_observed else None
            ),
        )
        # Re-assert acceptance through the receipts gate (verdict abstain blocks).
        wrapper["evidence_accepted"] = bool(wrapper["evidence_accepted"] and _evidence_accepted(wrapper))
        if retain_terminal:
            retention_interrupt = _retain_terminal(wrapper, attempt_store, conformance_launch)
            if interruption is None:
                interruption = retention_interrupt
    if (wrapper["receipt"]["attempt"].get("control_outcome") == "UNCERTAIN_OUTCOME"
            and custody["cleanup_observed"] is True):
        custody["prior_attempt_id"] = attempt_id
        custody["prior_receipt_sha256"] = receipt_digest(wrapper["receipt"])
    if interruption is not None:
        raise interruption
    return wrapper


def _path_identity(path):
    stat = os.stat(path, follow_symlinks=False)
    return stat.st_dev, stat.st_ino, stat.st_mode


def _retention_start(retention, opened, binding, argv, env, workdir, timeout, cap):
    admitted = retention.admission
    expected = {
        "installation_ref": binding.installation_ref, "slot_id": binding.slot_id,
        "provider_id": binding.provider_id, "provider_revision": binding.provider_revision,
        "manifest_sha256": binding.manifest_digest, "implementation_sha256": binding.implementation_sha256,
        "adapter_sha256": binding.adapter_sha256, "source_url": binding.source_url,
    }
    if any(admitted.get(key) != value for key, value in expected.items()):
        raise ValueError("staged invocation does not match admitted installation pins")
    start = evidence_projection.build_external_started(
        opened, installation_ref=admitted["installation_ref"],
        command={"kind": "argv", "arguments": list(argv), "cwd": str(workdir),
                 "timeout_ms": int(timeout * 1000), "max_output_bytes": cap,
                 "env_sha256": evidence_projection.env_sha256(env)},
        scope={"kind": "posix_child", "id": f"rea-posix-child/{admitted['attempt_id']}"},
        executor_id=admitted["executor_id"], request_sha=admitted["request_sha256"], retained=True,
    )
    if binding.adapter_sha256 is not None:
        start["external_process"]["adapter_sha256"] = binding.adapter_sha256
    return start


def _observe_retained(retention, started, proc_identity, captured, *, cleanup_observed):
    capture_sha = None if captured is None else hashlib.sha256(captured).hexdigest()
    evidence = retention.observation(started, {
        "process_identity": proc_identity, "captured_sha256": capture_sha,
        "cleanup_observed": cleanup_observed,
    })
    if proc_identity is None:
        if evidence.no_spawn is not True or evidence.process is not None or evidence.launch_observation is not None:
            raise ValueError("aborted terminal requires independent proof that no process spawned")
    elif (evidence.no_spawn is not False or type(evidence.process) is not dict
          or (evidence.process.get("pid"), evidence.process.get("starttime_ticks")) != proc_identity
          or evidence.captured_sha256 != capture_sha or evidence.launch_observation is None
          or evidence.launch_observation.get("observer_id") == started["executor_id"]
          or not _SHA256_RE.fullmatch(evidence.attribution_receipt_sha256 or "")):
        raise ValueError("terminal observation does not bind the launched process and captured octets")
    return evidence


def _finish_retained(wrapper, retention, started, *, proc_identity, captured, cleanup_observed,
                     evidence=None, artifact_payload=None):
    profile = wrapper["receipt"]
    try:
        if proc_identity is None:
            evidence = _observe_retained(retention, started, None, None, cleanup_observed=cleanup_observed)
        elif evidence is None:
            raise ValueError("no independent observation for the launched process")
        terminal_fields = evidence.terminal_fields(cleanup_observed=cleanup_observed)
        retirement = terminal_fields["cleanup_state"]
        if retirement != "confirmed_retired":
            profile["attempt"]["control_outcome"] = "UNCERTAIN_OUTCOME"
            wrapper["custody"]["cleanup_observed"] = False
            wrapper["cleanup_observed"] = False
        if proc_identity is None:
            terminal = evidence_projection.build_external_aborted(
                profile, started, abort_reason=profile["attempt"]["control_outcome"],
                retained=True, **terminal_fields,
            )
        else:
            terminal = evidence_projection.build_external_observed(
                profile, started, installation_ref=retention.admission["installation_ref"],
                scope=started["scope"], process=evidence.process, launch_observation=evidence.launch_observation,
                artifact_payload=artifact_payload, attribution_receipt_sha256=evidence.attribution_receipt_sha256,
                request_sha=retention.admission["request_sha256"], retained=True, **terminal_fields,
            )
        payloads = {} if captured is None else {"observation": captured}
        if artifact_payload is not None:
            payloads["provider_result"] = artifact_payload
        readback = retention.finish(
            started, terminal, profile, recordings=evidence.recordings, payloads=payloads,
        )
        wrapper["custody"]["retirement"] = retirement
        wrapper["shared_records"] = None
        wrapper["evidence_accepted"] = False
        if proc_identity is None or retirement != "confirmed_retired":
            return None
        outer = publish_retained_receipt(wrapper, readback)
        if outer is None:
            return None
        wrapper["receipt"] = outer
        wrapper["shared_records"] = {
            "started": readback.inspection["start"], "terminal": readback.inspection["terminal"],
            "retention_index": readback.index,
        }
        wrapper["evidence_accepted"] = True
    except BaseException as exc:
        _terminal_failed(wrapper)
        if not isinstance(exc, Exception):
            return exc
    return None


def _terminal_failed(wrapper):
    receipt = wrapper["receipt"]
    receipt["attempt"]["control_outcome"] = "UNCERTAIN_OUTCOME"
    receipt["attempt"].pop("slot_result", None)
    receipt["shared_receipt_adapter"].pop("shared_receipt_ref", None)
    receipt["shared_receipt_adapter"]["live_use_permitted"] = False
    receipt["observation"]["producer"]["process_bound"] = False
    if "verdict" in receipt:
        receipt["verdict"]["abstain_reason"] = "uncertain_outcome"
        receipt["verdict"]["abstain_detail"] = "terminal receipt retention failed"
    wrapper["shared_records"] = None
    wrapper["custody"].update(
        terminal_retention_failed=True, producer_binding_verified=False, artifact_digest_verified=False,
    )
    wrapper["custody"].pop("result_sha256", None)
    wrapper["evidence_accepted"] = False


def _retain_terminal(wrapper: dict, attempt_store, require_confirmation: bool) -> BaseException | None:
    try:
        committed = attempt_store(copy.deepcopy(wrapper["receipt"]))
        if committed is False or (require_confirmation and committed is not True):
            raise ValueError("terminal retention was not confirmed")
    except BaseException as exc:
        _terminal_failed(wrapper)
        if not isinstance(exc, Exception):
            return exc
    return None


def copy_of_opened(opened: dict) -> dict:
    return copy.deepcopy(opened)


def _compare_verdict(binding: Binding, manifest_digest: str, *, result: str, abstain_reason: str, abstain_detail: str) -> dict:
    policy_id = "policy:fixture@1"
    policy_version = "1"
    policy_sha = __import__("hashlib").sha256(f"{policy_id}@{policy_version}".encode()).hexdigest()
    return {
        "comparator_id": "host.compare",
        "comparator_version": "1",
        "result": result,
        "abstain_reason": abstain_reason,
        "abstain_detail": abstain_detail[:2000],
        "policy": {"id": policy_id, "version": policy_version, "sha256": policy_sha},
        "checks": [],
        "contrast_ids": [],
        "negative_controls": [],
        "oracle_independence": "independent",
        "independent_qualification": False,
    }


def _perception_rejected(
    binding: Binding, request_id: str, attempt_id: str, started_at: str,
    reason: str, detail: str, attempt_store, capsule_perception_ceiling: str = "vision",
    *, parent_episode_id: str | None = None, replay: dict | None = None,
) -> dict:
    # Preserve the caller's actual ceiling; never promote to required.
    ceiling = capsule_perception_ceiling if capsule_perception_ceiling in _LEVEL_RANK else "vision"
    opened = build_attempt_receipt(
        attempt_id=attempt_id, request_id=request_id, operation=_operation_for_binding(binding),
        slot_role=binding.role or "replay", slot_id=binding.slot_id, tool_version=binding.tool_version,
        upstream_licence_id=binding.upstream_licence_id, source_url=binding.source_url, use_disposition=binding.use_disposition,
        provider_id=binding.provider_id, provider_revision=binding.provider_revision, implementation_sha256=binding.implementation_sha256,
        producer_kind="external_slot", producer_id=binding.provider_id, producer_version=binding.provider_revision,
        producer_implementation_sha256=binding.implementation_sha256, process_bound=False,
        perception_level="none", perception_ceiling=ceiling,
        parent_episode_id=parent_episode_id, replay=replay,
        started_at=started_at, shared_schema_ref=binding.shared_schema_ref, manifest_sha256=binding.manifest_digest,
    )
    opened["attempt"]["control_outcome"] = "PERCEPTION_DENIED"
    custody = {"producer_binding_verified": False, "artifact_digest_verified": False, "cleanup_observed": True, "bound_process_id": None, "observed_process_id": None, "bound_starttime": None, "observed_starttime": None, "timed_out": False, "provider_id": binding.provider_id, "request_id": request_id, "observed_sha256": None, "implementation_sha256": binding.implementation_sha256}
    verdict = _compare_verdict(binding, binding.manifest_digest, result="abstain", abstain_reason="perception_ceiling", abstain_detail=detail) if _operation_for_binding(binding) == "compare" else None
    receipt = finalize_receipt(opened, None, custody=custody, implementation_sha256=binding.implementation_sha256, verdict=verdict)
    if attempt_store is not None and not binding.retention_required:
        attempt_store(copy.deepcopy(receipt))
    wrapper = {"receipt": receipt, "custody": custody, "evidence_accepted": False, "stdout": "", "stderr": "", "stdout_truncated": False, "rc": None, "pid": None, "timed_out": False, "cleanup_observed": True}
    _attach_shared_records(wrapper, receipt=receipt, binding=binding, launched=False,
                           command={"kind": "argv", "arguments": [binding.executable_path], "cwd": "/",
                                    "timeout_ms": max(1, int(binding.timeout_s * 1000)), "max_output_bytes": int(binding.max_output_bytes),
                                    "env_sha256": evidence_projection.env_sha256({})},
                           scope={"kind": "posix_child", "id": f"rea-posix-child/{evidence_projection.v03_attempt_id(attempt_id)}"},
                           cleanup_state="confirmed_retired",
                           cleanup_retirement_sha256=evidence_projection.record_digest({"attempt_id": attempt_id, "cleanup": True, "pid": None}),
                           abort_reason=(reason if isinstance(reason, str) and reason else "LIVE_EXECUTION_PARKED"))
    return wrapper


def _rejected_wrapper(binding: Binding, opened: dict, request_id: str, attempt_id: str, slot_role: str, started_at: str, reason: str, detail: str, attempt_store, observed_sha256: str | None = None, timed_out: bool = False, cleanup_observed: bool = True, perception_level: str | None = None) -> dict:
    custody = {"producer_binding_verified": False, "artifact_digest_verified": False, "cleanup_observed": cleanup_observed, "bound_process_id": None, "observed_process_id": None, "bound_starttime": None, "observed_starttime": None, "timed_out": timed_out, "provider_id": binding.provider_id, "request_id": request_id, "observed_sha256": observed_sha256 if observed_sha256 is not None else binding.observed_sha256, "implementation_sha256": binding.implementation_sha256}
    if reason == "UNCERTAIN_OUTCOME":
        _abstain_rep = "uncertain_outcome"
    elif reason == LIVE_EXECUTION_PARKED:
        _abstain_rep = "control_refused"
    elif reason not in _ABSTAIN_REASONS:
        _abstain_rep = "control_refused"
    else:
        _abstain_rep = reason
    verdict = _compare_verdict(binding, binding.manifest_digest, result="abstain", abstain_reason=_abstain_rep, abstain_detail=detail) if _operation_for_binding(binding) == "compare" else None
    # For record/replay rejected receipts carry no verdict (R1/R2); the
    # control outcome records the refusal.
    attempt_copy = copy_of_opened(opened)
    attempt_copy["attempt"]["control_outcome"] = reason
    receipt = finalize_receipt(attempt_copy, None, custody=custody, implementation_sha256=binding.implementation_sha256, verdict=verdict)
    if attempt_store is not None and not binding.retention_required:
        attempt_store(copy.deepcopy(receipt))
    wrapper = {"receipt": receipt, "custody": custody, "evidence_accepted": False, "stdout": "", "stderr": "", "stdout_truncated": False, "rc": None, "pid": None, "timed_out": timed_out, "cleanup_observed": cleanup_observed}
    _attach_shared_records(wrapper, receipt=receipt, binding=binding, launched=False,
                           command={"kind": "argv", "arguments": [binding.executable_path], "cwd": "/",
                                    "timeout_ms": max(1, int(binding.timeout_s * 1000)), "max_output_bytes": int(binding.max_output_bytes),
                                    "env_sha256": evidence_projection.env_sha256({})},
                           scope={"kind": "posix_child", "id": f"rea-posix-child/{evidence_projection.v03_attempt_id(attempt_id)}"},
                           cleanup_state="confirmed_retired" if cleanup_observed else "unknown",
                           cleanup_retirement_sha256=(
                               evidence_projection.record_digest({"attempt_id": attempt_id, "cleanup": True, "pid": None})
                               if cleanup_observed else None
                           ),
                           abort_reason=(reason if isinstance(reason, str) and reason else "LIVE_EXECUTION_PARKED"))
    return wrapper


def _terminate_only(pid: int, proc: subprocess.Popen, grace_s: float, bound_starttime: int | None = None) -> bool:
    """SIGTERM exactly the bound pid; owned by process_lifecycle (U09).

    Fails closed without a recorded int identity still matching live
    state; never signals a group.
    """
    return process_lifecycle.terminate_only(pid, proc, grace_s, bound_starttime)


def _kill_only(pid: int, proc: subprocess.Popen, bound_starttime: int | None = None) -> bool:
    """SIGKILL exactly the bound pid; owned by process_lifecycle (U09)."""
    return process_lifecycle.kill_only(pid, proc, bound_starttime)


def _close_pipes(proc: subprocess.Popen) -> None:
    """Close pipe handles; owned by process_lifecycle (U09)."""
    process_lifecycle.close_pipes(proc)


def _safe_kill(pid: int, starttime: int | None = None) -> bool:
    """SIGKILL one recorded pid only with a verified identity (U09).

    Production alias owned by :mod:`slot_host.runtime.process_lifecycle`.
    A bare pid, None, bool or mismatched starttime never signals.
    """
    return process_lifecycle.safe_kill(pid, starttime)


def _rejected_receipt(
    binding: Binding,
    request_id: str,
    attempt_id: str,
    slot_role: str,
    started_at: str,
    reason: str,
    detail: str,
    cleanup_observed: bool = True,
    observed_pid: int | None = None,
) -> dict:
    """Backward-compatible rejected receipt (wrapper form)."""
    opened = build_attempt_receipt(
        attempt_id=attempt_id, request_id=request_id, operation=_operation_for_binding(binding),
        slot_role=slot_role, slot_id=binding.slot_id, tool_version=binding.tool_version,
        upstream_licence_id=binding.upstream_licence_id, source_url=binding.source_url, use_disposition=binding.use_disposition,
        provider_id=binding.provider_id, provider_revision=binding.provider_revision, implementation_sha256=binding.implementation_sha256,
        producer_kind="external_slot", producer_id=binding.provider_id, producer_version=binding.provider_revision,
        producer_implementation_sha256=binding.implementation_sha256, process_bound=False,
        started_at=started_at, shared_schema_ref=binding.shared_schema_ref, manifest_sha256=binding.manifest_digest,
    )
    opened["attempt"]["control_outcome"] = reason
    custody = {"producer_binding_verified": False, "artifact_digest_verified": False, "cleanup_observed": cleanup_observed, "bound_process_id": observed_pid, "observed_process_id": observed_pid, "bound_starttime": None, "observed_starttime": None, "timed_out": False, "provider_id": binding.provider_id, "request_id": request_id, "observed_sha256": binding.observed_sha256, "implementation_sha256": binding.implementation_sha256}
    receipt = finalize_receipt(opened, None, custody=custody, implementation_sha256=binding.implementation_sha256)
    wrapper = {"receipt": receipt, "custody": custody, "evidence_accepted": False, "stdout": "", "stderr": "", "stdout_truncated": False, "rc": None, "pid": observed_pid, "timed_out": False, "cleanup_observed": cleanup_observed}
    _attach_shared_records(wrapper, receipt=receipt, binding=binding, launched=False,
                           command={"kind": "argv", "arguments": [binding.executable_path], "cwd": "/",
                                    "timeout_ms": max(1, int(binding.timeout_s * 1000)), "max_output_bytes": int(binding.max_output_bytes),
                                    "env_sha256": evidence_projection.env_sha256({})},
                           scope={"kind": "posix_child", "id": f"rea-posix-child/{evidence_projection.v03_attempt_id(attempt_id)}"},
                           cleanup_state="confirmed_retired",
                           cleanup_retirement_sha256=evidence_projection.record_digest({"attempt_id": attempt_id, "cleanup": True, "pid": observed_pid}),
                           abort_reason=(reason if isinstance(reason, str) and reason else "LIVE_EXECUTION_PARKED"))
    return wrapper


def execute(manifest: dict, executable_path: str, argv: list[str], *, operation_id: str, host_grants: list[str], allow_pending_evidence: bool = False, **kwargs) -> dict:
    """Convenience: bind then run; JSON-serialisable wrapper."""
    attempt_store = kwargs.pop("attempt_store", None)
    requested_profile = kwargs.pop("requested_profile", None)
    capsule_perception_ceiling = kwargs.pop("capsule_perception_ceiling", None)
    live_authority = kwargs.pop("live_authority", None)
    if live_authority is None:
        live_authority = _DEFAULT_AUTHORITY
    auth_kwargs = {
        key: kwargs.pop(key)
        for key in (
            "auth_grants",
            "auth_manifest_digest",
            "auth_slot_id",
            "auth_registry",
            "auth_probes",
            "auth_licence_disposition",
            "auth_evidence_ref",
            "auth_evidence_sha256",
            "installation_ref",
            "auth_spawn_granted",
        )
        if key in kwargs
    }
    try:
        binding = verify_binding(manifest, executable_path, operation_id=operation_id, host_grants=set(host_grants), allow_pending_evidence=allow_pending_evidence, requested_profile=requested_profile, capsule_perception_ceiling=capsule_perception_ceiling, **auth_kwargs)
    except BindingRejected as exc:
        # Retain a rejected attempt when the caller monitors via attempt_store.
        if attempt_store is not None and not isinstance(attempt_store, RetentionAttempt):
            try:
                # Re-verify minimal binding fields for the receipt.
                import copy as _copy
                # Best-effort receipt without a full binding.
                try:
                    _tmp_binding = None
                    # Derive required fields from manifest directly.
                    _operation_id_for_receipt = operation_id
                    _op = None
                    _mapped_op = "replay" if operation_id == "replay" else "compare" if operation_id == "compare" else "record"
                    for _o in manifest.get("operations", []):
                        if isinstance(_o, dict) and _o.get("id") == _operation_id_for_receipt:
                            _op = _o
                            break
                    _role = (_op or {}).get("role", "replay")
                    _opened = build_attempt_receipt(
                        attempt_id=str(kwargs.get("attempt_id", "att-execute-rejected")),
                        request_id=str(kwargs.get("request_id", "req-execute-rejected")),
                        operation=_mapped_op,
                        slot_role=_role if _role in ("replay", "verify") else "replay",
                        slot_id=str(manifest.get("slot_id", "fixture-slot")),
                        tool_version="1.0.0",
                        upstream_licence_id=str((manifest.get("upstream", {}) or {}).get("licence_id", "MIT")),
                        source_url=str((manifest.get("upstream", {}) or {}).get("source_url", "https://example.com/fixture")),
                        use_disposition="external_execution",
                        provider_id=str((manifest.get("provider_ref", {}) or {}).get("provider_id", "")) or None,
                        provider_revision=str((manifest.get("provider_ref", {}) or {}).get("provider_revision", "")) or None,
                        implementation_sha256=str((manifest.get("provider_ref", {}) or {}).get("implementation_sha256", "")) or None,
                        producer_kind="external_slot",
                        producer_id=str((manifest.get("provider_ref", {}) or {}).get("provider_id", "")) or str(manifest.get("slot_id", "fixture-slot")),
                        producer_version=str((manifest.get("provider_ref", {}) or {}).get("provider_revision", "")) or None,
                        producer_implementation_sha256=str((manifest.get("provider_ref", {}) or {}).get("implementation_sha256", "")) or None,
                        process_bound=False,
                        perception_level="none",
                        perception_ceiling=capsule_perception_ceiling,
                        slot_result=None,
                        control_outcome=exc.reason.upper() if exc.reason else "manifest_invalid",
                        started_at=str(kwargs.get("started_at", "2026-11-24T10:00:00Z")),
                        shared_schema_ref=str((manifest.get("evidence", {}) or {}).get("shared_schema_ref", "evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending")),
                    )
                    _opened["attempt"]["control_outcome"] = exc.reason.upper() if exc.reason else "manifest_invalid"
                    _custody = {"producer_binding_verified": False, "artifact_digest_verified": False, "cleanup_observed": True, "bound_process_id": None, "observed_process_id": None, "bound_starttime": None, "observed_starttime": None, "timed_out": False, "provider_id": str((manifest.get("provider_ref", {}) or {}).get("provider_id", "")), "request_id": str(kwargs.get("request_id", "req-execute-rejected")), "observed_sha256": None, "implementation_sha256": None}
                    _receipt = finalize_receipt(_opened, None, custody=_custody)
                    attempt_store(_copy.deepcopy(_receipt))
                except Exception:
                    pass
            except Exception:
                pass
        raise
    return run_bound(binding, argv, capsule_perception_ceiling=capsule_perception_ceiling or "none", attempt_store=attempt_store, live_authority=live_authority, **kwargs)
