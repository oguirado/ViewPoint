# SPDX-License-Identifier: AGPL-3.0-only
"""ROUTER, MCP, CLI and JSON projections for one slot manifest."""

from .mcp_lane import SlotProjector, control_reply
from .project import (
    PublicationError,
    availability_for,
    cli_invoke,
    control_envelope,
    generate,
    jcs_bytes,
    json_invoke,
    load_manifest_bytes,
    manifest_digest,
    mcp_invoke,
    normalize_arguments,
    policy_sha256,
    router_invoke,
    verify_publication,
)

__all__ = (
    "SlotProjector",
    "PublicationError",
    "availability_for",
    "cli_invoke",
    "control_envelope",
    "generate",
    "jcs_bytes",
    "json_invoke",
    "load_manifest_bytes",
    "manifest_digest",
    "mcp_invoke",
    "normalize_arguments",
    "policy_sha256",
    "router_invoke",
    "verify_publication",
)
