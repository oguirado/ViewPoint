# SPDX-License-Identifier: AGPL-3.0-only
"""Projection layer: CLI, JSON, MCP and router entries for the runtime."""

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import threading
from functools import cache
from pathlib import Path

from ..projections.mcp_lane import control_reply
from ..projections.project import _host_mini_cli
from ..projections.schema import digest, jcs_bytes, require, standalone
from . import effect_isolate, error_diagnose, journal_verify, reconstruction_obligations, tape_generate
from .executable_custody import build_typed_tail
from .binding import BindingRejected, verify_binding
from .live_park import LIVE_EXECUTION_PARKED, _FixtureLiveAuthority, authorize_live, live_admission
from .replay_lineage import ReplayLineageError, ReplayUnavailableError, resolve_replay_lineage
from .retention import RetentionConflict, RetentionUncertain
from .runner import _OMITTED_WITNESS, run_bound

_SCHEMA_VERSION = "https://json-schema.org/draft/2020-12/schema"
_BASE = "https://viewpoint.local/contracts/rea/runtime/"
_PUBLIC_BASE = "https://viewpoint.local/contracts/rea/projections/host/"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

_OFFLINE_TOOLS = {
    tool.TOOL_ID: tool
    for tool in (effect_isolate, error_diagnose, journal_verify, reconstruction_obligations, tape_generate)
}

# Spec'd host mini-tools exposed by the external-binding unit (HOSTCORE_SPEC).
_ROUTES = {
    "slot.inspect": (
        "Validate the manifest and its pins, list roles and required grants, show the registration preview, and report readiness.",
        ["inspect", "slot", "manifest", "grant", "readiness"],
        "none",
    ),
    "behaviour.record": (
        "Record the original on a declared input tape via the runner-adapter.",
        ["record", "tape", "observation", "behaviour"],
        "none",
    ),
    "behaviour.replay": (
        "Exercise the candidate with the retained input tape via the runner-adapter.",
        ["replay", "tape", "candidate", "behaviour"],
        "none",
    ),
    **{name: (tool.SUMMARY, tool.KEYWORDS, "none") for name, tool in _OFFLINE_TOOLS.items()},
}

# Legacy runtime aliases kept for backward-compatible dispatch (not advertised).
_LEGACY_ROUTES = {
    "runtime.bind": _ROUTES["slot.inspect"],
    "runtime.execute": _ROUTES["behaviour.record"],
    "runtime.reconcile": (
        "Reconcile an interrupted attempt into a new receipt that references it.",
        ["reconcile", "interrupt", "uncertain", "receipt"],
        "none",
    ),
}

_LIVE_TOOLS = frozenset({"behaviour.record", "behaviour.replay"})
_ADAPTER_FIELDS = frozenset({
    "installation_ref", "slot_id", "operation_id", "argv", "host_grants", "timeout_s", "max_output_bytes",
    "started_at", "perception_level", "parent_episode_id", "reference_episode_id",
    "candidate_episode_id", "slot_role", "tape_sha256", "replay",
})
_PARK_DETAIL = "live external execution is parked pending owner integration; no process was spawned"
_INSTALLATIONS: dict[str, dict] = {}
# Module-local control claims coordinate joiners. The owner-configured
# retention association fences execution across coordinator restarts.
_REQUEST_STORE: dict[str, dict] = {}
_STORE_LOCK = threading.Lock()
_CLI_INPUT_CAP = 65536


def _check_input_cap(raw: str) -> str:
    if len(raw.encode("utf-8")) > _CLI_INPUT_CAP:
        raise ValueError("argument input exceeds byte cap")
    return raw


def _read_args_file(path_str: str) -> str:
    """Read --args-file through an opened descriptor with a cap before decode.

    The descriptor is checked as a regular file at the point of open
    (no blocking open on FIFOs or devices) and at most _CLI_INPUT_CAP
    bytes are read before UTF-8 decoding.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path_str, flags)
    except OSError as exc:
        raise ValueError(f"cannot open args-file: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("args-file must be a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(8192, _CLI_INPUT_CAP + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _CLI_INPUT_CAP:
                raise ValueError("argument input exceeds byte cap")
        data = b"".join(chunks)
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("argument input is not UTF-8") from exc


def _read_stdin_bounded() -> str:
    """Read stdin bounded to _CLI_INPUT_CAP bytes before decoding."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        data = buffer.read(_CLI_INPUT_CAP + 1)
        if len(data) > _CLI_INPUT_CAP:
            raise ValueError("argument input exceeds byte cap")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("argument input is not UTF-8") from exc
    raw = sys.stdin.read(_CLI_INPUT_CAP + 1)
    if raw is None:
        raw = ""
    if len(raw.encode("utf-8")) > _CLI_INPUT_CAP:
        raise ValueError("argument input exceeds byte cap")
    return raw


def _require_auth(host_policy: dict | None) -> dict:
    """Return authenticated verify_binding kwargs or refuse (NO_LEASE).

    Execution and preview never accept a caller-supplied payload
    ``host_grants`` claim as authority.  Without authenticated ``host_policy``
    there is no grant, so admission is refused before any launch.
    """
    auth = _auth_kwargs_from_policy(host_policy)
    if auth is None:
        raise BindingRejected("grant_refused", "missing authenticated host policy; caller claim does not authorize execution")
    return auth


def _perception_ceiling_of(host_policy: dict | None) -> str:
    ceiling = host_policy.get("perception_ceiling") if isinstance(host_policy, dict) else None
    if ceiling not in ("none", "cdp_atspi", "vision"):
        raise BindingRejected("perception_denied", "authenticated perception ceiling is missing or invalid")
    return ceiling


def _check_parked(manifest: dict, operation_id: str) -> None:
    """The operation's binding selects a matching primary or alternate transport."""
    transport = manifest["transport"]
    operation = next((op for op in manifest["operations"] if op["id"] == operation_id), None)
    if operation is None:
        raise BindingRejected("manifest_invalid", f"unknown operation {operation_id!r}")
    if transport["kind"] == "mcp_stdio" or (
        operation["binding"]["kind"] == "mcp_tool"
        and any(row["kind"] == "mcp_stdio" for row in transport.get("alternates", []))
    ):
        raise BindingRejected("availability_parked", "external MCP transport is parked")


@cache
def _manifest_schema() -> dict:
    path = Path(__file__).resolve().parents[2] / "contracts/manifest/rea.slot_manifest_v2.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    return standalone(schema, {})


def _validate_static_manifest(manifest: dict) -> None:
    try:
        require(manifest, _manifest_schema(), "Manifest")
    except (OSError, ValueError) as exc:
        raise BindingRejected("manifest_invalid", str(exc)) from exc


def _installation_fields(row) -> tuple[dict, str | None]:
    if isinstance(row, tuple) and len(row) == 2:
        manifest, executable = row
    elif isinstance(row, dict) and "manifest" in row:
        manifest, executable = row["manifest"], row.get("executable_path")
    else:
        raise BindingRejected("manifest_invalid", "invalid host installation record")
    if not isinstance(manifest, dict):
        raise BindingRejected("manifest_invalid", "host installation has no manifest object")
    return manifest, str(executable) if executable is not None else None


def _resolve_slot_payload(payload: dict, host) -> tuple[dict, str | None, str | None]:
    """Resolve the target and verify any explicit installation belongs to it."""
    if type(payload) is not dict:
        raise ValueError("payload must be an object")
    for field in ("installation_ref", "slot_id"):
        if field in payload and (type(payload[field]) is not str or not payload[field].strip()):
            raise ValueError(f"payload.{field} must be a nonempty string")
    ref = payload.get("installation_ref")
    slot_id = payload.get("slot_id")
    inventory = host if isinstance(host, dict) else _INSTALLATIONS
    if ref in inventory:
        manifest, exe = _installation_fields(inventory[ref])
        if slot_id is not None and manifest.get("slot_id") != slot_id:
            raise BindingRejected("manifest_invalid", "installation does not belong to the requested slot")
        return manifest, exe, ref
    if slot_id is not None:
        for key, row in inventory.items():
            manifest, exe = _installation_fields(row)
            if manifest.get("slot_id") == slot_id:
                # Preserve the static plan without selecting a substitute installation.
                return (manifest, None, None) if ref is not None else (manifest, exe, key)
    if ref is None and slot_id is None:
        raise ValueError("payload.installation_ref or payload.slot_id is required")
    raise BindingRejected("availability_not_installed", "no installation is registered for the requested selection")


def _auth_kwargs_from_policy(host_policy: dict | None) -> dict | None:
    """Translate authenticated host records into verify_binding auth args.

    A caller-supplied payload host_grants claim never authorizes; when the
    host supplies a policy, only its authenticated records do.
    """
    if host_policy is None:
        return None
    if not isinstance(host_policy, dict):
        raise ValueError("host_policy must be an object")
    grants = host_policy.get("grants", ())
    if type(grants) not in (list, tuple, set, frozenset) or any(type(g) is not str for g in grants):
        raise ValueError("host_policy.grants must be a list of strings")
    return {
        "auth_grants": set(grants),
        "auth_manifest_digest": host_policy.get("manifest_digest"),
        "auth_slot_id": host_policy.get("slot_id"),
        "auth_registry": host_policy.get("registry"),
        "auth_probes": host_policy.get("probes"),
        "auth_licence_disposition": host_policy.get("licence_disposition"),
        "auth_evidence_ref": host_policy.get("evidence_ref"),
        "auth_evidence_sha256": host_policy.get("evidence_sha256"),
        "auth_spawn_granted": host_policy.get("spawn_granted"),
    }


def register_installation(installation_ref: str, manifest: dict, executable_path: str) -> None:
    """Register a host installation binding for installation_ref resolution."""
    _INSTALLATIONS[installation_ref] = {"manifest": manifest, "executable_path": str(executable_path)}


def _payload_installation(payload: dict, host) -> tuple[dict, str]:
    if type(payload) is not dict or "installation_ref" not in payload:
        raise ValueError("payload.installation_ref is required")
    manifest, executable, ref = _resolve_slot_payload(payload, host)
    _validate_static_manifest(manifest)
    if ref is None or executable is None:
        raise BindingRejected("availability_not_installed", "requested executable is not installed")
    return manifest, executable


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant {value}.")


def _jcs(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def handle_bind(payload: dict, *, request_id: str | None = None, host=None, host_policy: dict | None = None) -> dict:
    _require_auth(host_policy)
    raise BindingRejected("availability_parked", "public runtime.bind is parked")


def handle_slot_inspect(payload: dict, *, request_id: str | None = None, host=None,
                        host_policy: dict | None = None, retention=None) -> dict:
    """Validate the static plan and report passive installation readiness."""
    if type(payload) is not dict:
        raise ValueError("payload must be an object")
    operation_id = payload.get("operation_id", "record")
    if type(operation_id) is not str or not operation_id:
        raise ValueError("payload.operation_id must be a nonempty string")
    auth = _auth_kwargs_from_policy(host_policy)
    plan = {"operation_id": operation_id, "role": None, "transport_kind": None}
    reply = {
        "validation": {"ok": False},
        "effective_grants": sorted(auth["auth_grants"]) if auth is not None else [],
        "required_grants": [],
        "plan": plan,
    }
    try:
        manifest, exe, ref = _resolve_slot_payload(payload, host)
        _validate_static_manifest(manifest)
        op = next((op for op in manifest["operations"] if op["id"] == operation_id), None)
        if op is None:
            raise BindingRejected("manifest_invalid", f"unknown operation {operation_id!r}")
    except BindingRejected as exc:
        reply["validation"]["reason"] = exc.reason
        if exc.reason != "availability_not_installed":
            raise
        reply["readiness"] = {"ready": False, "availability": "not_installed"}
        return reply

    transport = manifest["transport"]
    plan.update(role=op["role"], transport_kind=transport["kind"])
    if op["binding"]["kind"] == "mcp_tool" and any(
        alt["kind"] == "mcp_stdio" for alt in transport.get("alternates", [])
    ):
        plan["transport_kind"] = "mcp_stdio"
    reply["required_grants"] = list(op["required_grants"])
    reply["validation"] = {"ok": True, "slot_id": manifest["slot_id"]}
    readiness = {"ready": False, "availability": "blocked"}
    reply["readiness"] = readiness
    try:
        if manifest["status"] == "not_installed" or exe is None or not Path(exe).is_file():
            raise BindingRejected("availability_not_installed", "requested executable is not installed")
        _check_parked(manifest, operation_id)
        if auth is None:
            return reply
        binding = verify_binding(
            manifest, exe, operation_id=operation_id, installation_ref=ref,
            requested_profile="external_process",
            capsule_perception_ceiling=_perception_ceiling_of(host_policy), retention=retention, **auth,
        )
        _require_runtime_perception(binding.perception_required)
    except BindingRejected as exc:
        readiness["availability"] = {
            "availability_not_installed": "not_installed",
            "availability_parked": "parked",
        }.get(exc.reason, "blocked")
    else:
        readiness.update(ready=True, availability="admitted", execution_profile=binding.execution_profile,
                         observed_sha256=binding.observed_sha256)
        reply["validation"]["manifest_digest"] = binding.manifest_digest
    return reply


def _validate_adapter_payload(payload, tool_id):
    if type(payload) is not dict:
        raise ValueError("payload must be an object")
    unknown = set(payload) - _ADAPTER_FIELDS
    if unknown:
        raise ValueError(f"unknown payload fields {sorted(unknown)!r}")
    if tool_id == "behaviour.replay" and payload.get("operation_id", "replay") != "replay":
        raise ValueError("behaviour.replay requires operation_id='replay'")
    if "operation_id" in payload and (type(payload["operation_id"]) is not str or not payload["operation_id"]):
        raise ValueError("operation_id must be a nonempty string")
    argv = payload.get("argv", [])
    if type(argv) is not list or any(type(word) is not str for word in argv):
        raise ValueError("argv must be a list of strings")


def _require_runtime_perception(perception):
    if perception != "none":
        raise BindingRejected("availability_parked", "owner-pending: T06")


def _resolve_adapter(payload, *, tool_id, host, host_policy, tape_sha256, tape_resolver, retention=None):
    auth = _require_auth(host_policy)
    if "grant.exec.external_process" not in auth["auth_grants"]:
        raise BindingRejected("grant_refused", "authenticated grant.exec.external_process is required for external execution")
    manifest, exe = _payload_installation(payload, host)
    operation_id = payload.get("operation_id", "replay" if tool_id == "behaviour.replay" else "record")
    _check_parked(manifest, operation_id)
    ceiling = _perception_ceiling_of(host_policy)
    binding = verify_binding(
        manifest, exe, operation_id=operation_id,
        requested_profile="external_process", capsule_perception_ceiling=ceiling,
        installation_ref=payload["installation_ref"], retention=retention, **auth,
    )
    perception = payload.get("perception_level", binding.perception_required)
    levels = ("none", "cdp_atspi", "vision")
    if perception not in (binding.perception_required, *binding.perception_optional) or (
        perception in levels and levels.index(perception) > levels.index(ceiling)
    ):
        raise BindingRejected("perception_denied", "requested perception is outside the binding or policy")
    _require_runtime_perception(perception)
    parent = payload.get("parent_episode_id")
    tape = tape_sha256 if tape_sha256 is not None else payload.get("tape_sha256")
    replay = None
    if operation_id == "replay":
        lineage = resolve_replay_lineage(payload, tape_sha256=tape_sha256, tape_resolver=tape_resolver)
        parent, tape, replay = (lineage[key] for key in ("parent_episode_id", "tape_sha256", "replay"))
    return binding, ceiling, parent, tape, replay


def _fixture_attempt_id(authority, request_id):
    return authority.attempt_for(request_id) if type(authority) is _FixtureLiveAuthority else None


def _run_adapter(payload: dict, *, request_id: str, host=None, vision_pool=None,
                 host_env_allowlist=frozenset({"PATH"}), host_policy=None,
                 producer_witness=_OMITTED_WITNESS, tape_sha256=None, shared_receipt_ref=None,
                 live_authority=None, tape_resolver=None, attempt_store=None, prepared=None,
                 retained_attempt=None) -> dict:
    _validate_adapter_payload(payload, None)
    binding, ceiling, resolved_parent, resolved_tape, resolved_replay = (
        prepared if prepared is not None else _resolve_adapter(
            payload, tool_id=None, host=host, host_policy=host_policy,
            tape_sha256=tape_sha256, tape_resolver=tape_resolver,
        )
    )
    if binding.retention_required:
        if retained_attempt is None:
            raise _ControlError(LIVE_EXECUTION_PARKED, _PARK_DETAIL)
        attempt_id = retained_attempt.admission["attempt_id"]
        attempt_store = retained_attempt
        shared_receipt_ref = None
    else:
        attempt_id = _fixture_attempt_id(live_authority, request_id)
        if attempt_id is None:
            raise _ControlError(LIVE_EXECUTION_PARKED, _PARK_DETAIL)
    if prepared is None and not authorize_live(
        binding, binding.executable_path, live_authority=live_authority,
        operation_id=binding.operation_id, request_id=request_id, attempt_id=attempt_id,
    ):
        raise _ControlError(LIVE_EXECUTION_PARKED, _PARK_DETAIL)
    return run_bound(
        binding,
        payload.get("argv", []),
        timeout_s=payload.get("timeout_s"),
        max_output_bytes=payload.get("max_output_bytes"),
        slot_role=payload.get("slot_role"),
        request_id=request_id,
        attempt_id=attempt_id,
        started_at=str(payload.get("started_at", "2026-11-24T10:00:00Z")),
        vision_pool=vision_pool,
        capsule_perception_ceiling=ceiling,
        perception_level=payload.get("perception_level"),
        host_env_allowlist=frozenset(host_env_allowlist),
        parent_episode_id=resolved_parent,
        reference_episode_id=payload.get("reference_episode_id"),
        candidate_episode_id=payload.get("candidate_episode_id"),
        producer_witness=producer_witness,
        tape_sha256=resolved_tape,
        replay=resolved_replay,
        tape_resolver=tape_resolver,
        shared_receipt_ref=shared_receipt_ref,
        live_authority=live_authority,
        attempt_store=attempt_store,
    )


def handle_record(payload: dict, *, request_id: str, host=None, vision_pool=None, host_policy: dict | None = None, producer_witness=_OMITTED_WITNESS, tape_sha256: str | None = None, shared_receipt_ref: str | None = None, live_authority=None, tape_resolver=None) -> dict:
    if isinstance(payload, dict) and "operation_id" not in payload:
        payload = dict(payload, operation_id="record")
    return _run_adapter(payload, request_id=request_id, host=host, vision_pool=vision_pool, host_policy=host_policy, producer_witness=producer_witness, tape_sha256=tape_sha256, shared_receipt_ref=shared_receipt_ref, live_authority=live_authority, tape_resolver=tape_resolver)


def handle_replay(payload: dict, *, request_id: str, host=None, vision_pool=None, host_policy: dict | None = None, producer_witness=_OMITTED_WITNESS, tape_sha256: str | None = None, shared_receipt_ref: str | None = None, live_authority=None, tape_resolver=None) -> dict:
    # Replay is its own operation; never fall back to record.
    if isinstance(payload, dict):
        op = payload.get("operation_id", "replay")
        if op != "replay":
            raise ValueError("behaviour.replay requires operation_id='replay'")
        if "operation_id" not in payload:
            payload = dict(payload, operation_id="replay")
    return _run_adapter(payload, request_id=request_id, host=host, vision_pool=vision_pool, host_policy=host_policy, producer_witness=producer_witness, tape_sha256=tape_sha256, shared_receipt_ref=shared_receipt_ref, live_authority=live_authority, tape_resolver=tape_resolver)


def handle_execute(payload: dict, *, request_id: str | None = None, host=None, vision_pool=None, host_policy: dict | None = None, producer_witness=_OMITTED_WITNESS, tape_sha256: str | None = None, shared_receipt_ref: str | None = None, live_authority=None, tape_resolver=None) -> dict:
    raise BindingRejected("availability_parked", "public runtime.execute is parked")


def handle_reconcile(payload: dict, *, request_id: str | None = None, host_policy: dict | None = None) -> dict:
    # Public reconciliation is parked: caller-supplied custody can never
    # prove cleanup.  Internal reconciliation must resolve the trusted
    # retained UNCERTAIN_OUTCOME attempt and its cleanup witness through
    # the host store (no public route in this revision).
    raise BindingRejected("availability_parked", "public runtime.reconcile is parked; use the authenticated internal route with a trusted retained witness")


def handler(tool_id: str):
    if tool_id == effect_isolate.TOOL_ID:
        return effect_isolate.isolate
    if tool_id == error_diagnose.TOOL_ID:
        return lambda payload, **_: error_diagnose.diagnose(payload)
    if tool_id == journal_verify.TOOL_ID:
        return journal_verify.verify
    if tool_id == reconstruction_obligations.TOOL_ID:
        return reconstruction_obligations.evaluate
    if tool_id == tape_generate.TOOL_ID:
        return tape_generate.generate
    if tool_id == "runtime.bind":
        return lambda payload, request_id=None, host=None, vision_pool=None, host_policy=None, producer_witness=_OMITTED_WITNESS, tape_sha256=None, shared_receipt_ref=None, live_authority=None, tape_resolver=None: handle_bind(payload, request_id=request_id, host=host, host_policy=host_policy)
    if tool_id == "runtime.execute":
        return lambda payload, request_id=None, host=None, vision_pool=None, host_policy=None, producer_witness=_OMITTED_WITNESS, tape_sha256=None, shared_receipt_ref=None, live_authority=None, tape_resolver=None: handle_execute(payload, request_id=request_id, host=host, vision_pool=vision_pool, host_policy=host_policy, producer_witness=producer_witness, tape_sha256=tape_sha256, shared_receipt_ref=shared_receipt_ref, live_authority=live_authority, tape_resolver=tape_resolver)
    if tool_id == "runtime.reconcile":
        return lambda payload, request_id=None, host=None, vision_pool=None, host_policy=None, producer_witness=_OMITTED_WITNESS, tape_sha256=None, shared_receipt_ref=None, live_authority=None, tape_resolver=None: handle_reconcile(payload, request_id=request_id, host_policy=host_policy)
    if tool_id == "slot.inspect":
        return lambda payload, request_id=None, host=None, vision_pool=None, host_policy=None, producer_witness=_OMITTED_WITNESS, tape_sha256=None, shared_receipt_ref=None, live_authority=None, tape_resolver=None, retention=None: handle_slot_inspect(payload, request_id=request_id, host=host, host_policy=host_policy, retention=retention)
    if tool_id == "behaviour.record":
        return lambda payload, request_id=None, host=None, vision_pool=None, host_policy=None, producer_witness=_OMITTED_WITNESS, tape_sha256=None, shared_receipt_ref=None, live_authority=None, tape_resolver=None: handle_record(payload, request_id=request_id or "req-record-0001", host=host, vision_pool=vision_pool, host_policy=host_policy, producer_witness=producer_witness, tape_sha256=tape_sha256, shared_receipt_ref=shared_receipt_ref, live_authority=live_authority, tape_resolver=tape_resolver)
    if tool_id == "behaviour.replay":
        return lambda payload, request_id=None, host=None, vision_pool=None, host_policy=None, producer_witness=_OMITTED_WITNESS, tape_sha256=None, shared_receipt_ref=None, live_authority=None, tape_resolver=None: handle_replay(payload, request_id=request_id or "req-replay-0001", host=host, vision_pool=vision_pool, host_policy=host_policy, producer_witness=producer_witness, tape_sha256=tape_sha256, shared_receipt_ref=shared_receipt_ref, live_authority=live_authority, tape_resolver=tape_resolver)
    raise ValueError(f"unknown runtime tool {tool_id!r}")


def control_request(tool_id: str, arguments: dict, *, context=None) -> dict:
    """Lift CLI/MCP wrapper arguments into the shared control lane."""
    if tool_id not in _ROUTES and tool_id not in _LEGACY_ROUTES:
        raise ValueError(f"unknown runtime tool {tool_id!r}")
    if type(arguments) is not dict or not {"request_id", "payload"} <= arguments.keys() or not arguments.keys() <= {"request_id", "payload", "dry_run"}:
        raise ValueError("Invalid runtime arguments: require request_id and payload.")
    request_id = arguments["request_id"]
    if type(request_id) is not str or _REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ValueError("Invalid request id.")
    dry_run = arguments.get("dry_run", False)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be boolean.")
    if type(arguments["payload"]) is not dict:
        raise ValueError("payload must be an object.")
    return {
        "schema": "vp.control.req/v1", "request_id": request_id, "op": "rea.slot.project",
        "context": context, "dry_run": dry_run,
        "args": {"route_id": f"host.{tool_id}", "arguments": {"payload": arguments["payload"]}},
    }


def handle_diagnostic_control(normalized):
    """Return the carrier reply after the control owner prepares this host route."""
    if normalized['route_id'] != f'host.{error_diagnose.TOOL_ID}':
        raise ValueError('Expected the error diagnosis route.')
    if normalized['dry_run']:
        result = {'kind': 'dry_run', 'dry_run': True, 'normalized_request': normalized, 'outside_capsule': True}
    else:
        payload = handler(error_diagnose.TOOL_ID)(normalized['arguments']['payload'])
        result = {'kind': 'host_result', 'payload': payload, 'outside_capsule': True}
    return control_reply(normalized['request_id'], 'OK', ok=True, result=result, effective=digest(normalized))


def _handle_fixture_control(normalized, tool, unavailable, host):
    if normalized['route_id'] != f'host.{tool}':
        raise ValueError(f'Expected the {tool} route.')
    request_id = normalized['request_id']
    effective = digest(normalized)
    if normalized['dry_run']:
        result = {'kind': 'dry_run', 'dry_run': True, 'normalized_request': normalized, 'outside_capsule': True}
    else:
        try:
            payload = handler(tool)(normalized['arguments']['payload'], request_id=request_id, host=host)
        except unavailable:
            return control_reply(request_id, 'AVAILABILITY_PARKED', ok=False, effective=effective)
        result = {'kind': 'host_result', 'payload': payload, 'outside_capsule': True}
    return control_reply(request_id, 'OK', ok=True, result=result, effective=effective)


def handle_journal_control(normalized, *, host=None):
    """Dispatch a prepared journal request through the authorized fixture host."""
    return _handle_fixture_control(normalized, journal_verify.TOOL_ID, journal_verify.JournalUnavailable, host)


def handle_reconstruction_control(normalized, *, host=None):
    """Evaluate reviewed obligations from the prepared host carrier."""
    return _handle_fixture_control(normalized, reconstruction_obligations.TOOL_ID,
                                   reconstruction_obligations.EvidenceUnavailable, host)


def handle_tape_control(normalized, *, host=None):
    """Dispatch a prepared tape request through the authorized fixture host."""
    return _handle_fixture_control(normalized, tape_generate.TOOL_ID, tape_generate.TapeUnavailable, host)


def handle_effect_control(normalized, *, host=None):
    """Run a prepared effect isolation request through the offline fixture host."""
    return _handle_fixture_control(normalized, effect_isolate.TOOL_ID, effect_isolate.EffectUnavailable, host)


def dispatch(tool_id: str, arguments: dict, *, context=None, host=None, vision_pool=None, host_policy: dict | None = None, producer_witness=_OMITTED_WITNESS, tape_sha256: str | None = None, shared_receipt_ref: str | None = None, live_authority=None, tape_resolver=None, attempt_store=None, retention=None) -> dict:
    """Common dispatcher used by CLI, MCP and JSON (control envelope)."""
    # arguments is the closed wrapper {request_id, dry_run, payload}.
    # Legacy fallback: a bare payload (manifest/...) is wrapped with a
    # generated request_id for backward-compatible internal callers.
    if type(arguments) is dict and not {"request_id", "payload"} <= arguments.keys() and ("manifest" in arguments or "installation_ref" in arguments):
        arguments = {"request_id": f"req-legacy-{abs(hash(_jcs(arguments))) % (10**8):08d}", "payload": arguments}
    if type(arguments) is not dict:
        raise ValueError("arguments must be an object")
    if not {"request_id", "payload"} <= arguments.keys():
        raise ValueError("arguments require request_id and payload")
    if not arguments.keys() <= {"request_id", "payload", "dry_run"}:
        raise ValueError("unknown arguments fields")
    request_id = arguments["request_id"]
    if type(request_id) is not str or _REQUEST_ID_RE.fullmatch(request_id) is None:
        raise ValueError("Invalid request id.")
    dry_run = arguments.get("dry_run", False)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be boolean.")
    payload = arguments["payload"]
    if type(payload) is not dict:
        raise ValueError("payload must be an object")
    if tool_id in _OFFLINE_TOOLS:
        if dry_run:
            require(payload, _OFFLINE_TOOLS[tool_id].ARGUMENT, "Host mini-tool payload")
            return {"ok": True, "dry_run": True, "request_id": request_id, "result": {"planned": True}}
        try:
            result = handler(tool_id)(payload, request_id=request_id, host=host)
        except (effect_isolate.EffectUnavailable, journal_verify.JournalUnavailable, reconstruction_obligations.EvidenceUnavailable,
                tape_generate.TapeUnavailable) as error:
            raise _ControlError("AVAILABILITY_PARKED", str(error)) from error
        return {"ok": True, "request_id": request_id, "tool_id": tool_id, "result": result}
    if tool_id in ("runtime.bind", "runtime.execute"):
        raise _ControlError("AVAILABILITY_PARKED", f"public {tool_id} is parked")
    is_live_tool = tool_id in _LIVE_TOOLS
    if is_live_tool:
        if "attempt_id" in payload:
            raise _ControlError("SCHEMA_INVALID", "the host assigns the execution attempt identity")
        _validate_adapter_payload(payload, tool_id)
    if tool_id == "slot.inspect":
        allowed_inspect = {"installation_ref", "slot_id", "operation_id", "host_grants"}
        unknown_check = set(payload) - allowed_inspect
        if unknown_check:
            raise ValueError(f"unknown payload fields {sorted(unknown_check)!r}")
    _auth_kwargs_from_policy(host_policy)
    intent_policy = host_policy
    if host_policy is not None and type(host_policy.get("grants")) is tuple:
        intent_policy = dict(host_policy, grants=list(host_policy["grants"]))
    intent = jcs_bytes({
        "tool_id": tool_id, "dry_run": dry_run, "payload": payload,
        "policy": intent_policy, "context": context,
    })
    intent_sha256 = hashlib.sha256(intent).hexdigest()
    retention_live = is_live_tool and retention is not None

    def resolve_attempt_id():
        if not retention_live:
            return _fixture_attempt_id(live_authority, request_id)
        try:
            return retention.associated_attempt(request_id, intent_sha256)
        except RetentionConflict as exc:
            raise conflict() from exc
        except (OSError, sqlite3.Error) as exc:
            raise _ControlError("UNCERTAIN_OUTCOME", "retention association is unavailable") from exc

    def resolve_current(attempt_id):
        if not is_live_tool:
            return None, None, {}, False
        try:
            resolved = _resolve_adapter(
                payload, tool_id=tool_id, host=host, host_policy=host_policy,
                tape_sha256=tape_sha256, tape_resolver=tape_resolver, retention=retention,
            )
            if dry_run or retention_live:
                build_typed_tail(resolved[0], payload.get("argv", []))
        except (BindingRejected, ReplayUnavailableError, ValueError, KeyError) as exc:
            raise _ControlError(_code_for_binding_error(exc), str(exc)) from exc
        binding, _, parent, tape, replay = resolved
        call = dict(live_authority=live_authority, operation_id=binding.operation_id,
                    request_id=request_id, attempt_id=attempt_id)
        if retention_live:
            call["readiness_only"] = True
        authorized = (dry_run or retention_live or attempt_id is not None) and authorize_live(
            binding, binding.executable_path, **call,
        )
        claim = {
            "intent": intent.decode("utf-8"), "installation": binding.installation_ref,
            "manifest": binding.manifest_digest, "implementation": binding.implementation_sha256,
            "path": binding.executable_path, "parent": parent, "tape": tape,
            "replay": replay, "live_authorized": authorized,
        }
        if retention_live:
            claim["retention"] = retention.claim_pins()
        elif attempt_id is not None:
            claim["fixture_attempt_id"] = attempt_id
        return resolved, digest(claim), call, authorized

    def conflict():
        return _ControlError("REQUEST_ID_CONFLICT", f"request {request_id!r} already claimed with different intent or authorization")

    with _STORE_LOCK:
        existing = _REQUEST_STORE.get(request_id)
        if existing is not None and existing["intent"] != intent:
            raise conflict()
    attempt_id = resolve_attempt_id() if is_live_tool else None
    prepared, claim_digest, call, authorized = resolve_current(attempt_id)
    if not is_live_tool:
        claim_digest = intent_sha256

    with _STORE_LOCK:
        entry = _REQUEST_STORE.get(request_id)
        executor = entry is None
        if executor:
            entry = {"intent": intent, "payload_digest": claim_digest, "reply": None,
                     "uncertain": False, "done": threading.Event(), "exc": None}
            _REQUEST_STORE[request_id] = entry
        elif entry["payload_digest"] != claim_digest:
            raise conflict()

    if not executor:
        entry["done"].wait()
        if entry.get("uncertain"):
            raise _ControlError("UNCERTAIN_OUTCOME", f"request {request_id!r} is UNCERTAIN_OUTCOME and must be reconciled")
        if entry.get("exc") is not None:
            raise entry["exc"]
        if entry.get("reply") is None:
            raise _ControlError("UNCERTAIN_OUTCOME", f"request {request_id!r} is UNCERTAIN_OUTCOME and must be reconciled")
        if is_live_tool:
            current, current_digest, call, _ = resolve_current(resolve_attempt_id())
            if current_digest != entry["payload_digest"]:
                raise conflict()
            with live_admission(current[0], **call) as allowed:
                if not allowed:
                    raise conflict()
                return copy.deepcopy(entry["reply"])
        if tool_id == "slot.inspect" and retention is not None:
            reply = copy.deepcopy(entry["reply"])
            reply["result"] = handle_slot_inspect(
                payload, request_id=request_id, host=host, host_policy=host_policy, retention=retention,
            )
            return reply
        return copy.deepcopy(entry["reply"])
    launched = False
    try:
        if is_live_tool:
            binding = prepared[0]
            if not authorized or not authorize_live(binding, binding.executable_path, **call):
                raise _ControlError(LIVE_EXECUTION_PARKED, _PARK_DETAIL)
            if dry_run:
                reply = {"ok": True, "dry_run": True, "request_id": request_id, "result": {"planned": True}}
                entry["reply"] = reply
                entry["done"].set()
                return copy.deepcopy(reply)
        retained_attempt = None
        if retention_live:
            try:
                retained_attempt = retention.admit(
                    binding, request_id, intent_sha256, claim_digest,
                    verify=lambda attempt: live_admission(binding, **(call | {"attempt_id": attempt})),
                )
            except RetentionConflict as exc:
                raise conflict() from exc
            except RetentionUncertain as exc:
                raise _ControlError("UNCERTAIN_OUTCOME", str(exc)) from exc
            if retained_attempt is None:
                raise _ControlError(LIVE_EXECUTION_PARKED, _PARK_DETAIL)
        launched = not dry_run
        try:
            if is_live_tool:
                result = _run_adapter(
                    payload, request_id=request_id, vision_pool=vision_pool,
                    producer_witness=producer_witness, shared_receipt_ref=shared_receipt_ref,
                    live_authority=live_authority, tape_resolver=tape_resolver,
                    attempt_store=attempt_store, prepared=prepared, retained_attempt=retained_attempt,
                )
            else:
                options = {"retention": retention} if tool_id == "slot.inspect" else {}
                result = handler(tool_id)(payload, request_id=request_id, host=host, host_policy=host_policy, **options)
        except BindingRejected as exc:
            raise _ControlError(_code_for_binding_error(exc), f"{exc.reason}: {exc.detail}") from exc
        except (ReplayLineageError, ReplayUnavailableError) as exc:
            raise _ControlError(_code_for_binding_error(exc), str(exc)) from exc
        # B-05: parked live refusals (run_bound returns a receipt with
        # control_outcome LIVE_EXECUTION_PARKED) must surface as an
        # ok=False control refusal, not as a successful reply result.
        # Emit _ControlError so CLI maps to exit 16 and the request-store
        # releases the claim (B-04) instead of caching an ok=True reply.
        if isinstance(result, dict):
            _rec = result.get("receipt", {})
            _ctrl = _rec.get("attempt", {}).get("control_outcome") if isinstance(_rec, dict) else None
            if _ctrl == LIVE_EXECUTION_PARKED:
                raise _ControlError(LIVE_EXECUTION_PARKED, "live external execution is parked pending owner integration; no process was spawned")
        reply_result = result
        reply = {"ok": True, "request_id": request_id, "tool_id": tool_id, "result": reply_result}
        # At-most-once UNCERTAIN_OUTCOME blocks retry until reconciled.
        uncertain = False
        if isinstance(reply_result, dict):
            cust = reply_result.get("custody", {})
            rec = reply_result.get("receipt", {})
            ctrl = rec.get("attempt", {}).get("control_outcome") if isinstance(rec, dict) else None
            if ctrl == "UNCERTAIN_OUTCOME" or cust.get("timed_out") is True:
                uncertain = True
        entry["reply"] = reply
        entry["uncertain"] = uncertain
        entry["done"].set()
        return copy.deepcopy(reply)
    except BaseException as exc:  # noqa: BLE001 - joiners must not hang on executor failure
        # A post-launch interruption (BaseException, not a deterministic
        # ValueError admission refusal) retains UNCERTAIN_OUTCOME so an
        # identical retry can never relaunch; a pre-launch refusal releases
        # the claim for a clean re-evaluation under corrected policy.
        if launched and not isinstance(exc, ValueError):
            entry["uncertain"] = True
            entry["exc"] = exc
            entry["done"].set()
        else:
            with _STORE_LOCK:
                if _REQUEST_STORE.get(request_id) is entry:
                    del _REQUEST_STORE[request_id]
            entry["exc"] = exc
            entry["done"].set()
        raise


class _ControlError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _code_for_binding_error(exc) -> str:
    if isinstance(exc, ReplayUnavailableError):
        return "AVAILABILITY_PARKED"
    if isinstance(exc, BindingRejected):
        mapping = {
            "grant_refused": "NO_LEASE",
            "profile_not_admitted": "PROFILE_NOT_ADMITTED",
            "perception_denied": "PERCEPTION_DENIED",
            "availability_blocked": "AVAILABILITY_BLOCKED",
            "availability_not_installed": "AVAILABILITY_NOT_INSTALLED",
            "availability_parked": "AVAILABILITY_PARKED",
            "live_execution_parked": "LIVE_EXECUTION_PARKED",
            "binding_rejected": "SCHEMA_INVALID",
            "manifest_invalid": "SCHEMA_INVALID",
        }
        return mapping.get(exc.reason, "SCHEMA_INVALID")
    return "SCHEMA_INVALID"


def _payload_schemas():
    argument_schemas: dict = {}
    result_schemas: dict = {}
    mini_tools: dict = {}
    mcp_tools: dict = {}
    router_entries: dict = {}
    for tool, (summary, keywords, level) in _ROUTES.items():
        payload_ref = _BASE + tool.replace(".", "/") + ".payload.json"
        result_ref = _BASE + tool.replace(".", "/") + ".result.json"
        wrapper_ref = _PUBLIC_BASE + tool.replace(".", "/") + ".input.json"
        row_id = f"host.{tool}"
        name = f"rea_{tool.replace('.', '_')}"
        argv = _host_mini_cli(tool) if tool != "slot.inspect" else ["slot", "inspect", "<slot_id>"]
        perception = {"required_level": level, "optional_levels": []}
        if tool == "slot.inspect":
            payload_schema = {
                "$schema": _SCHEMA_VERSION, "$id": payload_ref, "type": "object", "additionalProperties": False,
                "anyOf": [{"required": ["installation_ref"]}, {"required": ["slot_id"]}],
                "properties": {
                    "installation_ref": {"type": "string", "minLength": 1},
                    "slot_id": {"type": "string", "minLength": 1},
                    "operation_id": {"type": "string", "minLength": 1, "default": "record"},
                    "host_grants": {"type": "array", "items": {"type": "string"}, "default": []},
                },
            }
        elif tool in ("behaviour.record", "behaviour.replay"):
            payload_schema = {
                "$schema": _SCHEMA_VERSION, "$id": payload_ref, "type": "object", "additionalProperties": False,
                "required": ["installation_ref", "operation_id"],
                "properties": {
                    "installation_ref": {"type": "string", "minLength": 1},
                    "operation_id": {"type": "string", "minLength": 1},
                    "slot_id": {"type": "string", "minLength": 1},
                    "argv": {"type": "array", "items": {"type": "string"}, "default": []},
                    "host_grants": {"type": "array", "items": {"type": "string"}, "default": []},
                    "timeout_s": {"type": "number"},
                    "max_output_bytes": {"type": "integer", "minimum": 1},
                    "perception_level": {"enum": ["none", "cdp_atspi", "vision"]},
                    "parent_episode_id": {"type": "string"},
                    "reference_episode_id": {"type": "string"},
                    "candidate_episode_id": {"type": "string"},
                    "slot_role": {"type": "string"},
                    "tape_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                    "replay": {"type": "object"},
                    "started_at": {"type": "string"},
                },
            }
        else:
            payload_schema = {"$schema": _SCHEMA_VERSION, "$id": payload_ref, "type": "object", "additionalProperties": False}
        result_schema = {
            "$schema": _SCHEMA_VERSION, "$id": result_ref, "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "request_id": {"type": "string"},
                "tool_id": {"type": "string"},
                "result": {"type": "object"},
                "dry_run": {"type": "boolean"},
                "planned": {"type": "boolean"},
            },
        }
        wrapper_schema = {
            "$schema": _SCHEMA_VERSION, "$id": wrapper_ref, "type": "object", "additionalProperties": False,
            "required": ["request_id", "payload"],
            "properties": {
                "request_id": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"},
                "dry_run": {"type": "boolean", "default": False},
                "payload": {"$ref": payload_ref},
            },
        }
        if tool in _OFFLINE_TOOLS:
            definition = _OFFLINE_TOOLS[tool]
            payload_schema = {"$schema": _SCHEMA_VERSION, "$id": payload_ref, **copy.deepcopy(definition.ARGUMENT)}
            result_schema = {"$schema": _SCHEMA_VERSION, "$id": result_ref, **copy.deepcopy(definition.RESULT)}
        argument_schemas[payload_ref] = payload_schema
        argument_schemas[wrapper_ref] = wrapper_schema
        result_schemas[result_ref] = result_schema
        mini_tools[tool] = {
            "id": tool, "summary": summary, "keywords": list(keywords), "perception": perception,
            "argument_schema_ref": payload_ref, "result_schema_ref": result_ref,
            "required_effects": ["process.spawn:child"] if tool.startswith("behaviour.") else [],
            "handler": handler(tool),
        }
        if tool in _OFFLINE_TOOLS:
            mini_tools[tool].update(argument_schema_ref_sha256=digest(payload_schema),
                                    result_schema_ref_sha256=digest(result_schema))
        mcp_tools[name] = {"name": name, "title": row_id, "description": summary, "inputSchema": wrapper_schema}
        router_entries[row_id] = {
            "id": row_id, "owner": "viewpoint-rea", "operation": tool, "role": None,
            "manifest_digest": None, "provider_ref": None, "summary": summary,
            "search_terms": sorted({*keywords, tool, row_id}),
            "input_schema_ref": wrapper_ref, "output_schema_ref": "urn:vp.control.reply:v1",
            "mcp_name": name, "cli_argv_shape": argv,
            "json_contract_ref": {
                "control_request_schema_ref": "urn:vp.control.req:v1",
                "control_reply_schema_ref": "urn:vp.control.reply:v1", "op": "rea.slot.project", "route_id": row_id,
                "argument_schema_ref": payload_ref, "result_schema_ref": result_ref,
            },
            "perception": perception,
            "required_effects": ["process.spawn:child"] if tool.startswith("behaviour.") else [],
            "availability": "parked",
            "conformance_receipt_ref": None, "outside_capsule": True, "facets": {},
        }
    return argument_schemas, result_schemas, mini_tools, mcp_tools, router_entries


def mcp_descriptors():
    _, _, _, mcp_tools, _ = _payload_schemas()
    return mcp_tools


def router_entries():
    _, _, _, _, router_entries_dict = _payload_schemas()
    return router_entries_dict


ARGUMENT_SCHEMAS, RESULT_SCHEMAS, MINI_TOOLS, MCP_TOOLS, ROUTER_ENTRIES = _payload_schemas()


def main(argv: list[str] | None = None, *, dispatch_fn=None, context=None, retention=None) -> int:
    parser = argparse.ArgumentParser(prog="slot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mini = sub.add_parser("mini")
    mini.add_argument("tool", choices=tuple(_ROUTES))
    source = mini.add_mutually_exclusive_group(required=True)
    source.add_argument("--args")
    source.add_argument("--args-file", metavar="PATH")
    mini.add_argument("--request-id")
    mini.add_argument("--dry-run", default=None, action="store_true")
    parsed = parser.parse_args(argv)
    try:
        if parsed.args_file == "-":
            raw = _read_stdin_bounded()
        elif parsed.args_file is not None:
            raw = _read_args_file(parsed.args_file)
        else:
            raw = parsed.args
        if raw is None:
            parser.error("either --args or --args-file is required")
        _check_input_cap(raw)
        try:
            arguments = json.loads(raw, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)
        except ValueError as exc:
            print(f"invalid JSON: {exc}", file=sys.stderr)
            return 14
        except RecursionError as exc:
            print(f"invalid JSON: {exc}", file=sys.stderr)
            return 14
        if type(arguments) is not dict:
            raise ValueError("Expected a JSON argument object.")
        for key, flag in (("request_id", parsed.request_id), ("dry_run", parsed.dry_run)):
            if flag is not None:
                if key in arguments and (type(arguments[key]) is not type(flag) or arguments[key] != flag):
                    raise ValueError(f"The flag disagrees with argument {key}.")
                arguments[key] = flag
        envelope = control_request(parsed.tool, arguments, context=context)
    except (OSError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 14
    if dispatch_fn is None:
        print("The authenticated slot control lane is unavailable.", file=sys.stderr)
        return 16
    try:
        options = {"retention": retention} if retention is not None else {}
        reply = dispatch_fn(parsed.tool, arguments, **options)
    except _ControlError as exc:
        print(str(exc), file=sys.stderr)
        mapping = {"HOLD": 10, "HUMAN_NEEDED": 10, "NO_LEASE": 11, "REQUEST_ID_CONFLICT": 12, "UNCERTAIN_OUTCOME": 13, "SCHEMA_INVALID": 14, "PERCEPTION_DENIED": 15, "POOL_TIMEOUT": 15, "PROFILE_NOT_ADMITTED": 16, "AVAILABILITY_NOT_INSTALLED": 16, "AVAILABILITY_BLOCKED": 16, "AVAILABILITY_PARKED": 16, "LIVE_EXECUTION_PARKED": 16}
        return mapping.get(exc.code, 1)
    except BindingRejected as exc:
        print(f"{exc.reason}: {exc.detail}", file=sys.stderr)
        mapping = {"HOLD": 10, "HUMAN_NEEDED": 10, "NO_LEASE": 11, "REQUEST_ID_CONFLICT": 12, "UNCERTAIN_OUTCOME": 13, "SCHEMA_INVALID": 14, "PERCEPTION_DENIED": 15, "POOL_TIMEOUT": 15, "PROFILE_NOT_ADMITTED": 16, "AVAILABILITY_NOT_INSTALLED": 16, "AVAILABILITY_BLOCKED": 16, "AVAILABILITY_PARKED": 16, "LIVE_EXECUTION_PARKED": 16}
        return mapping.get(_code_for_binding_error(exc), 1)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 14
    print(_jcs(reply))
    if reply.get("ok") is True:
        return 0
    code = reply.get("code", "ERROR") if isinstance(reply, dict) else "ERROR"
    return {"HOLD": 10, "HUMAN_NEEDED": 10, "NO_LEASE": 11, "REQUEST_ID_CONFLICT": 12, "UNCERTAIN_OUTCOME": 13, "SCHEMA_INVALID": 14, "PERCEPTION_DENIED": 15, "POOL_TIMEOUT": 15, "PROFILE_NOT_ADMITTED": 16, "AVAILABILITY_NOT_INSTALLED": 16, "AVAILABILITY_BLOCKED": 16, "AVAILABILITY_PARKED": 16, "LIVE_EXECUTION_PARKED": 16}.get(code, 1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
