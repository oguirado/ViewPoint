# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted host composition for the Tools local-open deployment."""

import importlib
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from threading import RLock

from . import evidence_projection as projection
from .retention import RetentionClients, RetentionIdentity


_COMPONENT = Path(__file__).resolve().parents[2]
_CONFIG = _COMPONENT / "slot_host" / "retention.local-open.v1.json"
_PROFILE = _COMPONENT / "contracts" / "evidence" / "rea.slot_evidence_profile_v1.schema.json"
_IMPORT_LOCK = RLock()


def local_open_profile():
    """Read the REA-owned pins and verify the exact in-tree profile schema."""
    config = json.loads(_CONFIG.read_bytes())
    raw = _PROFILE.read_bytes()
    if (config["schema"] != "rea.retention.local-open/v1"
            or config["profile"] != {"id": projection.PROFILE_SCHEMA_V1, "sha256": sha256(raw).hexdigest()}
            or config["witness_module"] != "slot_host.runtime.retention_witness"):
        raise ValueError("local-open REA profile pins do not match the installed schema or witness")
    route = config["successor_route"]
    if set(route) != {"provider", "request", "result"} or not all(type(value) is str and value for value in route.values()):
        raise ValueError("local-open successor route pins are incomplete")
    return config, raw


def tools_source():
    directory = os.environ.get("REA_RETENTION_SRC")
    if not directory:
        raise OSError("owner-pending: T01 (REA_RETENTION_SRC is unset)")
    return Path(directory).resolve(strict=True)


def import_tools(module_name):
    """Import Tools from the configured read-only export, including on a clean sys.path."""
    source = tools_source()
    if not module_name.startswith("retention_service."):
        raise ValueError("expected a Tools retention module")
    with _IMPORT_LOCK:
        previous_path, previous_bytecode = sys.path, sys.dont_write_bytecode
        sys.path = [str(source), *previous_path]
        sys.dont_write_bytecode = True
        try:
            parts = module_name.split(".")
            for level in range(1, len(parts) + 1):
                name = ".".join(parts[:level])
                spec = importlib.util.find_spec(name)
                paths = [] if spec is None else (
                    [spec.origin] if spec.origin else list(spec.submodule_search_locations or ())
                )
                if not paths or not all(Path(path).resolve().is_relative_to(source) for path in paths):
                    raise ValueError(f"{name} must come from REA_RETENTION_SRC")
            return importlib.import_module(module_name)
        finally:
            sys.path, sys.dont_write_bytecode = previous_path, previous_bytecode


@dataclass(frozen=True)
class LocalRetention:
    """Authenticated host ports and the separate denial-tenant read port."""

    clients: RetentionClients
    denial: RetentionIdentity
    denial_tenant: str

    def control_clients(self, association_path):
        return replace(self.clients, tenant=self.denial_tenant, inspector=self.denial,
                       association_path=association_path)

    def check_denial_endpoint(self):
        with self.denial.client.session(self.denial_tenant) as session:
            reply = session.ready(
                "inspect", principal=self.denial.principal,
                policy_revision=self.clients.policy_revision, acl_revision=self.clients.acl_revision,
            )
        challenge = reply.get("challenge") if isinstance(reply, dict) else None
        return type(challenge) is str and re.fullmatch(r"[0-9a-f]{64}", challenge) is not None and reply == {
            "challenge": challenge, "verb": "inspect", "tenant": self.denial_tenant,
            "principal": self.denial.principal, "policy_revision": self.clients.policy_revision,
            "acl_revision": self.clients.acl_revision, "record_pin": projection.V03_SHA,
            "index_pin": projection.INDEX_SHA,
        }


def load_local_retention(association_path=None, *, observe_terminal=None):
    """Resolve the roles and Unix endpoints from the Tools-generated deployment."""
    from .retention_witness import LocalWitness, pinned_profiles

    config, profile = local_open_profile()
    local = import_tools("retention_service.server.local")
    transport = import_tools("retention_service.transport.client")
    contracts = import_tools("retention_service.contracts.validation")
    wire = import_tools("retention_service.contracts.wire")
    deployment = local.load_local()
    defaults = deployment.defaults
    if association_path is None:
        association_path = deployment.store.parent / "rea-associations.sqlite3"
    custody = LocalWitness.current(create=True) if observe_terminal is None else None
    if defaults["profile"] != "local-open":
        raise ValueError("expected the Tools local-open deployment")
    validator = contracts.ContractValidator(local.FROZEN_DIR)

    def identity(role):
        endpoint = transport.Endpoint(unix=deployment.socket_path(role))
        return RetentionIdentity(transport.Client(None, endpoint, validator),
                                 defaults["endpoints"][role]["principal"])

    pins = config["profile"]
    route = config["successor_route"]
    return LocalRetention(
        RetentionClients(
            tenant=defaults["endpoints"]["writer"]["tenant"],
            authority=identity("writer"), runner=identity("writer"), inspector=identity("inspector"),
            policy_revision=defaults["policy_revision"], acl_revision=defaults["acl_revision"],
            profile_schema=pins["id"], profile_sha256=pins["sha256"], association_path=association_path,
            admission_bytes=wire.admission_bytes,
            validate_profile=pinned_profiles({(pins["id"], pins["sha256"]): profile}),
            observe_terminal=custody.observe_terminal if custody is not None else observe_terminal,
            local_open=True, custody=custody,
            successor_route=(route["provider"], route["request"], route["result"]),
        ),
        identity("denial"), defaults["endpoints"]["denial"]["tenant"],
    )
