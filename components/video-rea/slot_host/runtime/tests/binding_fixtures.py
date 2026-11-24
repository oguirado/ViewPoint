# SPDX-License-Identifier: AGPL-3.0-only
"""Test-owned installation and retention inputs for offline runtime fixtures."""

import datetime
import hashlib
import unittest
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from slot_host.projections.schema import digest
from slot_host.runtime import binding, runner, tools
from slot_host.runtime.live_park import _FixtureLiveAuthority

_GRANT = "grant.exec.external_process"
_BINDING_PARAMETERS = signature(binding.verify_binding).parameters


UNRESOLVED_EVIDENCE_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v1"
FIXTURE_RESULT_PAYLOAD = "offline result"
FIXTURE_RESULT_SHA256 = hashlib.sha256(FIXTURE_RESULT_PAYLOAD.encode()).hexdigest()
_RUN_BOUND = runner.run_bound
_RUN_OPTIONS = signature(_RUN_BOUND).parameters


class FixtureLaunch:
    """Test caller indicating that it owns the disposable executable."""

    def verify(self, *, binding, operation_id, request_id, attempt_id):
        return True


def run_fixture(binding, argv, **kwargs):
    """Mint the real request-bound capability at the runtime boundary."""
    if type(kwargs.get("live_authority")) is FixtureLaunch:
        try:
            capability = _FixtureLiveAuthority(
                binding,
                request_id=kwargs.get("request_id", _RUN_OPTIONS["request_id"].default),
                attempt_id=kwargs.get("attempt_id", _RUN_OPTIONS["attempt_id"].default),
            )
        except (ValueError, OSError):
            # Exercise the normal integrity refusal for changed executables.
            pass
        else:
            kwargs = dict(kwargs, live_authority=capability)
    return _RUN_BOUND(binding, argv, **kwargs)


def fixture_retention():
    """Simulate availability only within the test module using this fixture."""
    def fixture_attempt(authority, request_id):
        if type(authority) is _FixtureLiveAuthority:
            return authority.attempt_for(request_id)
        if authority is not None:
            return "fixture-" + hashlib.sha256(request_id.encode()).hexdigest()
        return None

    for seam in (
        patch.object(tools, "_fixture_attempt_id", side_effect=fixture_attempt),
        patch.object(binding, "_retention_available", return_value=True, create=True),
        patch.object(tools, "run_bound", run_fixture),
        patch.object(runner, "run_bound", run_fixture),
    ):
        seam.start()
        unittest.addModuleCleanup(seam.stop)


def registry_for(manifest, operation_id="record"):
    operation = next((op for op in manifest["operations"] if op["id"] == operation_id), {})
    role = operation.get("role")
    contract = next((row for row in manifest["contract_refs"] if row["role"] == role), {})
    provider = manifest["provider_ref"]
    adapter = manifest["capabilities"]["adapter_identity"]
    return {
        "role": role,
        "provider_id": provider.get("provider_id"),
        "adapter_id": adapter.get("adapter_id"),
        **{key: provider.get(key) for key in ("provider_sha256", "provider_revision", "implementation_sha256")},
        "adapter_sha256": adapter.get("adapter_sha256"),
        **{key: contract.get(key) for key in ("request_digest", "result_digest",
                                             "request_schema_ref", "result_schema_ref", "execution_profile")},
    }


def fixture_policy(manifest, operation_id="record"):
    ref = manifest["evidence"]["shared_schema_ref"]
    pin = binding._EVIDENCE_PINS.get(ref)
    return {
        "grants": [_GRANT],
        "spawn_granted": True,
        "manifest_digest": digest(manifest),
        "slot_id": manifest["slot_id"],
        "registry": registry_for(manifest, operation_id),
        "licence_disposition": "external_execution",
        "evidence_ref": ref,
        "evidence_sha256": pin[1] if pin else None,
        "perception_ceiling": "vision",
    }


def fixture_binding_options(manifest, operation_id, overrides):
    if not isinstance(manifest, dict):
        return overrides
    try:
        policy = fixture_policy(manifest, operation_id)
    except (KeyError, TypeError):
        return overrides
    auth = tools._auth_kwargs_from_policy(policy)
    auth = {key: value for key, value in auth.items() if key in _BINDING_PARAMETERS}
    auth.pop("auth_probes", None)
    auth["auth_grants"] = set(overrides.get("host_grants") or ())
    if "auth_registry" in overrides and isinstance(overrides["auth_registry"], dict):
        # Legacy fixture registries already exercise their original pin omissions.
        registry = {key: auth["auth_registry"][key] for key in ("provider_id", "adapter_id", "role")}
        overrides = dict(overrides, auth_registry=registry | overrides["auth_registry"])
    if "auth_evidence_ref" in overrides and "auth_evidence_sha256" in _BINDING_PARAMETERS:
        pin = binding._EVIDENCE_PINS.get(overrides["auth_evidence_ref"])
        auth["auth_evidence_sha256"] = pin[1] if pin else None
    return {
        "capsule_perception_ceiling": policy["perception_ceiling"],
        "installation_ref": "installation:fixture-slot@test",
        **auth,
        **overrides,
    }


def verified_fixture_binding(manifest, executable, *, operation_id, **kwargs):
    return binding.verify_binding(
        manifest, executable, operation_id=operation_id,
        **fixture_binding_options(manifest, operation_id, kwargs),
    )


def execute_fixture(manifest, executable, argv, *, operation_id, **kwargs):
    options = fixture_binding_options(manifest, operation_id, kwargs)
    if "auth_evidence_sha256" not in _BINDING_PARAMETERS:
        options.pop("installation_ref", None)
    return runner.execute(manifest, executable, argv, operation_id=operation_id, **options)


def _dispatch_options(payload, kwargs):
    policy = kwargs.get("host_policy")
    if not isinstance(policy, dict) or not isinstance(payload, dict):
        return kwargs
    ref = payload.get("installation_ref")
    host = kwargs.get("host")
    inventory = host if isinstance(host, dict) else tools._INSTALLATIONS
    row = inventory.get(ref)
    if row is None:
        return dict(kwargs, host_policy={"perception_ceiling": "none"} | policy)
    manifest = row[0] if isinstance(row, tuple) else row["manifest"]
    baseline = fixture_policy(manifest, payload.get("operation_id", "record"))
    if isinstance(policy.get("registry"), dict):
        identity = {k: baseline["registry"][k] for k in ("provider_id", "adapter_id", "role")}
        policy = dict(policy, registry=identity | policy["registry"])
    return dict(kwargs, host_policy=baseline | policy)


def dispatch_fixture(tool_id, arguments, **kwargs):
    payload = arguments.get("payload")
    options = _dispatch_options(payload, kwargs)
    # Legacy fixture IDs are test inputs. Keep them out of the public payload
    # and bind them to the measured fixture through the private capability.
    if (tool_id in ("behaviour.record", "behaviour.replay") and isinstance(payload, dict)
            and "attempt_id" in payload and type(options.get("live_authority")) is FixtureLaunch):
        payload = dict(payload)
        attempt_id = payload.pop("attempt_id")
        inventory = options.get("host")
        if not isinstance(inventory, dict):
            inventory = tools._INSTALLATIONS
        manifest, executable = tools._installation_fields(inventory[payload["installation_ref"]])
        executable_sha = manifest["provider_ref"]["implementation_sha256"]
        measured = SimpleNamespace(
            executable_path=str(Path(executable).resolve()), operation_id=payload.get("operation_id", "record"),
            implementation_sha256=executable_sha, observed_sha256=executable_sha,
        )
        options["live_authority"] = _FixtureLiveAuthority(
            measured, request_id=arguments["request_id"], attempt_id=attempt_id,
        )
        arguments = dict(arguments, payload=payload)
    return tools.dispatch(tool_id, arguments, **options)


def record_fixture(payload, **kwargs):
    return tools.handle_record(payload, **_dispatch_options(payload, kwargs))


def replay_fixture(payload, **kwargs):
    return tools.handle_replay(payload, **_dispatch_options(payload, kwargs))


def probe_witness(installation_ref="installation:fixture-slot@test"):
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "installation_ref": installation_ref,
        "measured_at": (now - datetime.timedelta(minutes=1)).isoformat(),
        "expires_at": (now + datetime.timedelta(minutes=1)).isoformat(),
        "outcome": "passed",
    }
