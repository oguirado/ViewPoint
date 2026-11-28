# SPDX-License-Identifier: AGPL-3.0-only
"""Publish one inventory from typed contracts and the existing ViewPoint handler."""

import hashlib
import json
from pathlib import Path

from asset_factory import contracts
from viewpoint.handlers import local_capabilities

ROOT = Path(__file__).resolve().parent


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("ascii")


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() if hasattr(
            hashlib, "file_digest"
        ) else _stream_digest(source)


def _stream_digest(source):
    digest = hashlib.sha256()
    for block in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def inventory():
    slots = {}
    for name in contracts.SLOTS:
        slot = contracts.slot_contract(name)
        schema = f"slots/{name}.schema.json"
        slots[name] = {
            "contract": slot.schema_id,
            "schema_file": schema,
            "schema_sha256": sha256(ROOT / schema),
            "input_roles": [sorted(roles) for roles in slot.input_roles],
            "output_roles": [sorted(roles) for roles in slot.output_roles],
            "optional_output_roles": sorted(slot.optional_output_roles),
            "source": "studio" if name in contracts.EXTENSIONS else "asset_factory",
        }
    return {
        "schema": "vp.studio.registry/v1",
        "license": "AGPL-3.0-only",
        "slots": slots,
        "factory_operations": local_capabilities(),
    }


def mcp_manifest(registry_sha256, slots):
    request = {"type": "object", "properties": {"request": {"type": "object"}},
               "required": ["request"], "additionalProperties": False}
    return {
        "schema": "vp.studio.mcp-manifest/v1",
        "license": "AGPL-3.0-only",
        "server": {"name": "viewpoint-studio", "transport": "stdio",
                   "command": "viewpoint-studio", "args": ["--mcp"]},
        "registry_sha256": registry_sha256,
        "slots": sorted(slots),
        "tools": [
            {"name": "vp_studio_capabilities", "description": "Discover installed studio slots and their evidence.",
             "inputSchema": {"type": "object", "additionalProperties": False}},
            {"name": "vp_studio_dry_run", "description": "Validate a typed studio slot request.",
             "inputSchema": request},
            {"name": "vp_studio_run", "description": "Run a request through an explicitly configured provider.",
             "inputSchema": request},
        ],
    }
