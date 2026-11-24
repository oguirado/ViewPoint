# SPDX-License-Identifier: AGPL-3.0-only
"""External executable binding, grant and profile enforcement.

Implements the binding leg of external-binding: resolve a user-selected
executable to its SHA-256 digest per rea.slot_manifest/v2, and gate
dispatch on host grants and execution_profile.  No subprocess is started
here; the runner performs out-of-process execution under the binding
returned by verify_binding.

Owner-integration default-on: the conformance predicate in
:mod:`slot_host.runtime.conformance_authority` binds a fresh fixture
receipt to the exact :class:`Binding` identity.  Helpers
``binding_conformance_identity`` and ``verify_conformance_for_binding``
expose that identity and a fail-closed check without changing the
landed ``verify_binding`` gate for callers that do not supply a
receipt.  MAIN enrolment and Tools retention remain owner seams that
deny by default.
"""

import copy
import datetime
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .evidence_projection import is_registered_source_url

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SHA256_ZERO = "0" * 64
_HOST_GRANTS_DOC = "host policy grants cover the operation"
_LEVEL_RANK = {"none": 0, "cdp_atspi": 1, "vision": 2}
_SLOT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_TOP_LEVEL_KEYS = (
    "schema", "slot_id", "status", "upstream", "provider_ref", "contract_refs",
    "transport", "operations", "capabilities", "perception", "record_replay",
    "comparison", "evidence", "conformance", "discovery",
)
_PUBLISHED_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
_PUBLISHED_ID = "https://metaviewer.local/contracts/v0.2/attempt_evidence_record.schema.json"
_PUBLISHED_SHA = "b3bfd6715abfa60a0866b655d23088d8bee09b0cf8e0a3b44c156e5692c19d8c"
_V03_REF = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.3"
_V03_ID = "https://metaviewer.local/contracts/v0.3/attempt_evidence_record.schema.json"
_V03_SHA = "9402c78a4780cab53d9ad9726b103d05e76b504fe693c16b572078c9d8c98dc7"
_EVIDENCE_PINS = {
    _PUBLISHED_REF: (_PUBLISHED_REF, _PUBLISHED_SHA), _PUBLISHED_ID: (_PUBLISHED_REF, _PUBLISHED_SHA),
    _V03_REF: (_V03_REF, _V03_SHA), _V03_ID: (_V03_REF, _V03_SHA),
}
_FORBIDDEN_LICENCES = {"UNKNOWN", "NONE", "NOASSERTION"}

# Minimal manifest fields the host checks before dispatch.  Fuller schema
# validation lives in the projector; the runtime re-checks pins that
# determine custody: identity, provider digests and operation ceilings.
_OPERATIONS_REQUIRED = ("id", "role", "input_ref", "output_ref", "binding", "required_effects", "required_grants", "limits", "retry_semantics")
_CONTRACT_REF_REQUIRED = ("role", "request_schema_ref", "request_digest", "result_schema_ref", "result_digest", "execution_profile")


@dataclass(frozen=True, kw_only=True)
class Binding:
    """A host-verified executable binding keyed to the manifest digest."""

    manifest_digest: str
    slot_id: str
    provider_id: str
    provider_revision: str
    implementation_sha256: str
    adapter_sha256: str | None
    executable_path: str
    observed_sha256: str
    execution_profile: str
    grants: tuple[str, ...]
    transport_kind: str
    argv_prefix: tuple[str, ...]
    framing: str
    cwd_policy: str
    env_allowlist: tuple[str, ...]
    # Operation material the runner needs (issue 4).
    operation_id: str = ""
    role: str = ""
    operation_binding: dict = field(default_factory=dict)
    timeout_s: int = 5
    max_output_bytes: int = 4096
    required_effects: tuple[str, ...] = ()
    retry_semantics: dict = field(default_factory=dict)
    # Slot / upstream for the evidence profile (issue 3).
    tool_version: str = "1.0.0"
    upstream_licence_id: str = "MIT"
    source_url: str = "https://example.com/fixture"
    use_disposition: str = "external_execution"
    # Perception / evidence for lease and ceiling checks (issue 2).
    perception_required: str = "none"
    perception_optional: tuple[str, ...] = ()
    observation_sources: tuple[dict, ...] = ()
    shared_schema_ref: str = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending"
    shared_schema_sha256: str | None = None
    installation_ref: str | None = None
    provider_schema: str | None = None
    provider_sha256: str | None = None
    adapter_id: str | None = None
    request_schema_ref: str | None = None
    request_digest: str | None = None
    result_schema_ref: str | None = None
    result_digest: str | None = None
    retention_required: bool = False


class BindingRejected(ValueError):
    """Raised when digest, profile or grant gates fail before dispatch."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

    def __str__(self):
        return f"{self.reason}: {self.detail}"


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_digest(manifest: dict) -> str:
    """RFC 8785 JCS manifest digest (reuse projection canonicalization)."""
    try:
        from slot_host.projections.schema import digest as _jcs_digest
    except ImportError:  # pragma: no cover - import layout fallback
        try:
            from ..projections.schema import digest as _jcs_digest  # type: ignore[no-redef]
        except ImportError:
            _jcs_digest = None  # type: ignore[assignment]
    if _jcs_digest is not None:
        try:
            return _jcs_digest(manifest)
        except Exception as exc:
            raise BindingRejected("manifest_invalid", f"manifest is not JCS canonical: {exc}") from exc
    # Fallback identical to slot_host/projections/schema.py canonical logic.
    import math
    import re as _re
    from decimal import Decimal

    def _num(value):
        if isinstance(value, int):
            if abs(value) < 2**53:
                return str(value)
            try:
                converted = float(value)
            except OverflowError as error:
                raise ValueError("Number is outside the canonical JSON range.") from error
            if int(converted) != value:
                raise ValueError("Integer loses precision in canonical JSON.")
            value = converted
        if not math.isfinite(value):
            raise ValueError("Nonfinite JSON number.")
        if value == 0:
            return "0"
        decimal = Decimal(repr(value))
        if 1e-6 <= abs(value) < 1e21:
            fixed = format(decimal, "f")
            return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
        coefficient, exponent = format(decimal, "e").split("e")
        if "." in coefficient:
            coefficient = coefficient.rstrip("0").rstrip(".")
        return f"{coefficient}e{int(exponent):+d}"

    def _canon(value):
        import json as _json

        kind = type(value)
        if value is None or kind is bool:
            return _json.dumps(value)
        if kind is str:
            return _json.dumps(value, ensure_ascii=False)
        if kind in (int, float):
            return _num(value)
        if kind is list:
            return "[" + ",".join(map(_canon, value)) + "]"
        if kind is dict and all(type(k) is str for k in value):
            keys = sorted(value, key=lambda k: k.encode("utf-16-be"))
            return "{" + ",".join(_canon(k) + ":" + _canon(value[k]) for k in keys) + "}"
        raise ValueError("Expected a JSON value.")

    try:
        canonical = _canon(manifest)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BindingRejected("manifest_invalid", f"manifest is not JCS canonical: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _licence_use_disposition(licence: str) -> str:
    """Derive use disposition from the licence expression.

    FSL appearing anywhere in the expression (including compound
    ``MIT AND FSL-1.1-ALv2``) marks research-only material.
    """
    if re.search(r"\bFSL\b", licence, flags=re.IGNORECASE):
        return "internal_research"
    return "external_execution"


def _check_upstream(upstream: dict) -> tuple[str, str, str, str]:
    """Validate upstream release/licence inputs with no fallback defaults."""
    if not isinstance(upstream, dict):
        raise BindingRejected("manifest_invalid", "upstream must be an object")
    for key in ("name", "source_url", "version", "licence_id", "licence_evidence_refs", "component_notices", "user_installed"):
        if key not in upstream:
            raise BindingRejected("manifest_invalid", f"upstream missing {key!r}")
    name = upstream.get("name")
    if type(name) is not str or not name.strip():
        raise BindingRejected("manifest_invalid", "upstream.name must be a nonempty string")
    source_url = upstream.get("source_url")
    if type(source_url) is not str or not source_url.lower().startswith("https://") or any(c.isspace() for c in source_url):
        raise BindingRejected("manifest_invalid", "upstream.source_url must be an https URI")
    version = upstream.get("version")
    if type(version) is not str or not version.strip():
        raise BindingRejected("manifest_invalid", "upstream.version must be a nonempty string")
    licence = upstream.get("licence_id")
    if type(licence) is not str or not licence.strip():
        raise BindingRejected("manifest_invalid", "upstream.licence_id must be a nonempty string")
    if licence.strip().upper() in _FORBIDDEN_LICENCES:
        raise BindingRejected("manifest_invalid", "upstream.licence_id is forbidden")
    refs = upstream.get("licence_evidence_refs")
    if type(refs) is not list or not refs or any(type(r) is not str or not r.strip() for r in refs):
        raise BindingRejected("manifest_invalid", "upstream.licence_evidence_refs must be a nonempty list of strings")
    notices = upstream.get("component_notices")
    if type(notices) is not list or any(type(r) is not str for r in notices):
        raise BindingRejected("manifest_invalid", "upstream.component_notices must be a list of strings")
    if upstream.get("user_installed") is not True:
        raise BindingRejected("manifest_invalid", "upstream.user_installed must be true")
    lowered = name.strip().lower()
    if ("raea" in lowered or "raea" in source_url.lower()) and "FSL" not in licence:
        raise BindingRejected("manifest_invalid", "raea material requires FSL licence")
    return name, source_url, version, licence


def _is_sha256(value) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None and value != _SHA256_ZERO


def _get_operation(manifest: dict, operation_id: str | None) -> dict:
    ops = manifest.get("operations")
    if not isinstance(ops, list) or not ops:
        raise BindingRejected("manifest_invalid", "operations must be a nonempty list")
    if operation_id is None:
        # Default to first operation for inspection; runner always passes an id.
        return ops[0]
    for op in ops:
        if isinstance(op, dict) and op.get("id") == operation_id:
            return op
    raise BindingRejected("manifest_invalid", f"operation {operation_id!r} not declared")


def _get_contract_ref(manifest: dict, role: str) -> dict | None:
    for row in manifest.get("contract_refs", []):
        if isinstance(row, dict) and row.get("role") == role:
            return row
    return None


def check_grants(operation: dict, host_grants: set[str] | frozenset[str]) -> None:
    """Require operation.required_grants subset of host policy grants."""
    required = operation.get("required_grants")
    if not isinstance(required, list):
        raise BindingRejected("grant_refused", "required_grants must be a list")
    missing = [g for g in required if g not in host_grants]
    if missing:
        raise BindingRejected(
            "grant_refused",
            f"missing host grants {missing!r}; { _HOST_GRANTS_DOC }",
        )


def check_profile(manifest: dict, operation: dict, requested_profile: str | None = None) -> str:
    """Resolve and enforce the admitted execution_profile for the operation role.

    Returns the admitted profile (offline_fixture or external_process).
    Raises BindingRejected with PROFILE_NOT_ADMITTED or related reason.
    """
    role = operation.get("role")
    contract = _get_contract_ref(manifest, role) if role else None
    if contract is None:
        raise BindingRejected("profile_not_admitted", f"no contract_ref for role {role!r}")
    for key in _CONTRACT_REF_REQUIRED:
        if key not in contract:
            raise BindingRejected("manifest_invalid", f"contract_ref missing {key!r}")
    profile = contract.get("execution_profile")
    if profile not in ("offline_fixture", "external_process"):
        raise BindingRejected("profile_not_admitted", f"unsupported execution_profile {profile!r}")
    # v1 exchange URIs pin offline_fixture per manifest schema.
    for key in ("request_schema_ref", "result_schema_ref"):
        ref = contract.get(key, "")
        if ref in ("urn:rea.slot_request:v1", "urn:rea.slot_result:v1"):
            if profile != "offline_fixture":
                raise BindingRejected("profile_not_admitted", "v1 exchange requires offline_fixture")
            break
    if profile == "external_process":
        from .retention_deployment import local_open_profile

        try:
            pins = local_open_profile()[0]["successor_route"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BindingRejected("profile_not_admitted", "REA successor route pins are unavailable") from exc
        for field, key in (("request_schema_ref", "request"), ("result_schema_ref", "result")):
            if contract.get(field) != pins[key]:
                raise BindingRejected("profile_not_admitted", f"external_process requires {pins[key]}")
    if requested_profile is not None and requested_profile != profile:
        # Live request under fixture-only admission (or vice versa) is refused
        # before any process starts.
        raise BindingRejected("profile_not_admitted", f"requested {requested_profile!r} but admitted {profile!r}")
    provider = manifest.get("provider_ref", {})
    if not isinstance(provider, dict):
        raise BindingRejected("manifest_invalid", "provider_ref must be an object")
    provider_schema = provider.get("schema")
    if profile == "external_process" and provider_schema != pins["provider"]:
        raise BindingRejected("profile_not_admitted", f"external_process requires {pins['provider']}")
    # Provider identity pins required for external admission (idx 4).
    for _pk in ("schema", "provider_id", "provider_revision", "provider_sha256", "implementation_sha256"):
        if _pk not in provider:
            raise BindingRejected("manifest_invalid", f"provider_ref missing {_pk!r}")
    if type(provider.get("provider_id")) is not str or not provider["provider_id"].strip():
        raise BindingRejected("manifest_invalid", "provider_ref.provider_id must be a nonempty string")
    if type(provider.get("schema")) is not str or not provider["schema"].strip():
        raise BindingRejected("manifest_invalid", "provider_ref.schema must be a nonempty string")
    if profile == "external_process":
        if provider.get("schema") != pins["provider"]:
            raise BindingRejected("profile_not_admitted", f'external_process requires {pins["provider"]}')
        rev = provider.get("provider_revision")
        if type(rev) is not str or not rev.strip() or rev.strip() in ("unpinned", "0.0.0"):
            raise BindingRejected("manifest_invalid", "external_process requires a concrete provider_revision")
    status = manifest.get("status")
    if status == "registered":
        ops = manifest.get("operations", [])
        ids = {op.get("id") for op in ops if isinstance(op, dict)}
        if not {"record", "replay", "compare"} <= ids:
            raise BindingRejected("manifest_invalid", "registered manifest requires record, replay and compare operations")
        upstream = manifest.get("upstream", {})
        up_version = upstream.get("version") if isinstance(upstream, dict) else None
        up_url = upstream.get("source_url") if isinstance(upstream, dict) else None
        if up_version in ("unpinned", "0.0.0"):
            raise BindingRejected("manifest_invalid", "registered manifest requires a concrete upstream version")
        if provider.get("provider_revision") == "unpinned":
            raise BindingRejected("manifest_invalid", "registered manifest requires a concrete provider_revision")
        if not is_registered_source_url(up_url):
            raise BindingRejected("manifest_invalid", "registered manifest requires a valid upstream.source_url")
        if not _is_sha256(provider.get("provider_sha256")):
            raise BindingRejected("profile_not_admitted", "provider_sha256 is not a concrete pin")
        if not _is_sha256(provider.get("implementation_sha256")):
            raise BindingRejected("profile_not_admitted", "implementation_sha256 is not a concrete pin")
        if not _is_sha256(contract.get("request_digest")) or not _is_sha256(contract.get("result_digest")):
            raise BindingRejected("profile_not_admitted", "contract digests must be concrete pins")
        caps = manifest.get("capabilities", {})
        adapter = caps.get("adapter_identity", {}) if isinstance(caps, dict) else {}
        if not isinstance(adapter, dict) or not _is_sha256(adapter.get("adapter_sha256")):
            raise BindingRejected("profile_not_admitted", "registered manifest requires concrete adapter_sha256")
    if profile == "external_process":
        if status == "not_installed":
            raise BindingRejected("availability_not_installed", "manifest is not_installed; no installed binding")
        if not _is_sha256(provider.get("provider_sha256")):
            raise BindingRejected("profile_not_admitted", "provider_sha256 is not a concrete pin")
        if not _is_sha256(provider.get("implementation_sha256")):
            raise BindingRejected("profile_not_admitted", "implementation_sha256 is not a concrete pin")
        for key in ("request_digest", "result_digest"):
            if not _is_sha256(contract.get(key)):
                raise BindingRejected("profile_not_admitted", f"{key} is not a concrete pin")
        caps = manifest.get("capabilities", {})
        adapter = caps.get("adapter_identity", {}) if isinstance(caps, dict) else {}
        adapter_sha = adapter.get("adapter_sha256") if isinstance(adapter, dict) else None
        if adapter_sha is not None and not _is_sha256(adapter_sha):
            raise BindingRejected("profile_not_admitted", "adapter_sha256 is not a concrete pin")
    return profile


def _check_operation_refs(manifest: dict, operation: dict, contract: dict) -> None:
    """Verify input_ref/output_ref role selectors agree with contract_refs (issue 4)."""
    role = operation.get("role")
    req_base = contract.get("request_schema_ref", "")
    res_base = contract.get("result_schema_ref", "")
    expected_in = f"{req_base}#slot={role}"
    expected_out = f"{res_base}#slot={role}"
    if operation.get("input_ref") != expected_in:
        raise BindingRejected("manifest_invalid", f"input_ref {operation.get('input_ref')!r} != {expected_in!r}")
    if operation.get("output_ref") != expected_out:
        raise BindingRejected("manifest_invalid", f"output_ref {operation.get('output_ref')!r} != {expected_out!r}")


def _check_transport_and_perception(manifest: dict, capsule_perception_ceiling: str) -> tuple[str, tuple[str, ...], tuple[dict, ...]]:
    """Enforce outside-capsule transport and perception ordering."""
    transport = manifest.get("transport", {})
    if not isinstance(transport, dict):
        raise BindingRejected("manifest_invalid", "transport must be an object")
    if transport.get("driver_location") != "outside_capsule":
        raise BindingRejected("perception_denied", "transport.driver_location must be outside_capsule")
    if transport.get("executable_binding") != "user_selected":
        raise BindingRejected("manifest_invalid", "transport.executable_binding must be user_selected")
    perception = manifest.get("perception", {})
    if not isinstance(perception, dict):
        raise BindingRejected("manifest_invalid", "perception must be an object")
    if "required_level" not in perception or "optional_levels" not in perception or "observation_sources" not in perception:
        raise BindingRejected("manifest_invalid", "perception requires required_level, optional_levels and observation_sources")
    required_level = perception.get("required_level")
    optional_levels = perception.get("optional_levels")
    if required_level not in _LEVEL_RANK:
        raise BindingRejected("manifest_invalid", f"unknown required_level {required_level!r}")
    if type(optional_levels) is not list:
        raise BindingRejected("manifest_invalid", "optional_levels must be a list")
    if len(set(optional_levels)) != len(optional_levels):
        raise BindingRejected("manifest_invalid", "optional_levels must be unique")
    for lvl in optional_levels:
        if lvl not in ("cdp_atspi", "vision"):
            raise BindingRejected("manifest_invalid", f"unknown optional_level {lvl!r}")
        if _LEVEL_RANK[lvl] <= _LEVEL_RANK[required_level]:
            raise BindingRejected("manifest_invalid", f"optional_level {lvl!r} must exceed required_level {required_level!r}")
    if required_level == "cdp_atspi" and any(v != "vision" for v in optional_levels):
        raise BindingRejected("manifest_invalid", "cdp_atspi required_level allows only vision optional")
    if required_level == "vision" and len(optional_levels) != 0:
        raise BindingRejected("manifest_invalid", "vision required_level allows no optional levels")
    if capsule_perception_ceiling not in _LEVEL_RANK:
        raise BindingRejected("manifest_invalid", f"unknown capsule ceiling {capsule_perception_ceiling!r}")
    if _LEVEL_RANK[required_level] > _LEVEL_RANK[capsule_perception_ceiling]:
        raise BindingRejected("perception_denied", f"required_level {required_level!r} exceeds ceiling {capsule_perception_ceiling!r}")
    sources = perception.get("observation_sources")
    if type(sources) is not list or not sources:
        raise BindingRejected("manifest_invalid", "observation_sources must be a nonempty list")
    seen_ids: set[str] = set()
    for src in sources:
        if not isinstance(src, dict):
            raise BindingRejected("manifest_invalid", "observation_source must be an object")
        sid = src.get("id")
        if type(sid) is not str or not sid.strip():
            raise BindingRejected("manifest_invalid", "observation_source id must be nonempty")
        if sid in seen_ids:
            raise BindingRejected("manifest_invalid", f"duplicate observation_source id {sid!r}")
        seen_ids.add(sid)
        if src.get("outside_capsule") is not True:
            raise BindingRejected("perception_denied", f"observation_source {sid!r} must be outside_capsule")
        level = src.get("level")
        if level not in _LEVEL_RANK:
            raise BindingRejected("manifest_invalid", f"unknown source level {level!r}")
        if level == "vision":
            vp = src.get("vision_pool")
            if not isinstance(vp, dict):
                raise BindingRejected("manifest_invalid", "vision source requires vision_pool metadata")
            if vp.get("required") is not True or vp.get("owner") != "ViewPoint_Tools" or vp.get("ticket_field") != "vision_pool_ticket":
                raise BindingRejected("manifest_invalid", "vision_pool metadata mismatch")
        elif "vision_pool" in src:
            raise BindingRejected("manifest_invalid", "only vision sources may declare vision_pool")
    declared = {required_level, *optional_levels}
    present = {s.get("level") for s in sources}
    if present != declared:
        raise BindingRejected("manifest_invalid", "observation_sources must cover required and optional levels exactly")
    return required_level, tuple(optional_levels), tuple(sources)


def _check_transport_lists(transport: dict) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
    for key in ("kind", "driver_location", "executable_binding", "argv_prefix", "framing", "cwd_policy", "env_allowlist"):
        if key not in transport:
            raise BindingRejected("manifest_invalid", f"transport missing {key!r}")
    kind = transport.get("kind")
    if kind not in ("cli_argv", "mcp_stdio"):
        raise BindingRejected("manifest_invalid", "unsupported transport kind")
    argv_prefix = transport.get("argv_prefix")
    if type(argv_prefix) is not list or any(type(w) is not str for w in argv_prefix):
        raise BindingRejected("manifest_invalid", "transport.argv_prefix must be a list of strings")
    env_allowlist = transport.get("env_allowlist")
    if type(env_allowlist) is not list or any(type(w) is not str for w in env_allowlist):
        raise BindingRejected("manifest_invalid", "transport.env_allowlist must be a list of strings")
    framing = transport.get("framing")
    if framing not in ("none", "jsonrpc_stdio"):
        raise BindingRejected("manifest_invalid", "unsupported framing")
    expected = "none" if kind == "cli_argv" else "jsonrpc_stdio"
    if framing != expected:
        raise BindingRejected("manifest_invalid", f"framing {framing!r} does not match kind {kind!r}")
    cwd_policy = transport.get("cwd_policy")
    if cwd_policy != "run_scratch":
        raise BindingRejected("manifest_invalid", "cwd_policy must be run_scratch")
    alternates = transport.get("alternates", [])
    if alternates is not None:
        if type(alternates) is not list or len(alternates) > 1:
            raise BindingRejected("manifest_invalid", "transport.alternates must be a list of at most one entry")
        for alt in alternates:
            if not isinstance(alt, dict):
                raise BindingRejected("manifest_invalid", "alternate must be an object")
            alt_kind = alt.get("kind")
            if alt_kind not in ("cli_argv", "mcp_stdio"):
                raise BindingRejected("manifest_invalid", "unsupported alternate kind")
            if alt_kind == kind:
                raise BindingRejected("manifest_invalid", "alternate kind must differ from primary kind")
            if alt.get("framing") != ("none" if alt_kind == "cli_argv" else "jsonrpc_stdio"):
                raise BindingRejected("manifest_invalid", "alternate framing does not match kind")
            if type(alt.get("argv_prefix")) is not list or any(type(w) is not str for w in alt.get("argv_prefix", [])):
                raise BindingRejected("manifest_invalid", "alternate argv_prefix must be a list of strings")
    return kind, tuple(argv_prefix), framing, tuple(env_allowlist)


def _check_operation_binding(operation: dict, transport_kind: str) -> dict:
    bnd = operation.get("binding")
    if not isinstance(bnd, dict) or "kind" not in bnd:
        raise BindingRejected("manifest_invalid", "operation.binding must declare kind")
    kind = bnd.get("kind")
    if kind == "cli_subcommand":
        if transport_kind != "cli_argv":
            raise BindingRejected("manifest_invalid", "cli_subcommand requires cli_argv transport")
        argv = bnd.get("argv")
        if type(argv) is not list or not argv or any(type(w) is not str for w in argv):
            raise BindingRejected("manifest_invalid", "cli_subcommand argv must be a nonempty list of strings")
        typed = bnd.get("typed_args")
        if type(typed) is not list:
            raise BindingRejected("manifest_invalid", "cli_subcommand typed_args must be a list")
        names: set[str] = set()
        positions: list[int] = []
        for item in typed:
            if not isinstance(item, dict) or "name" not in item or "type" not in item or "position" not in item:
                raise BindingRejected("manifest_invalid", "typed_args entries require name, type and position")
            if item.get("type") not in ("path", "string", "integer", "boolean"):
                raise BindingRejected("manifest_invalid", "typed_args type is unsupported")
            if type(item.get("position")) is not int or item["position"] < 0:
                raise BindingRejected("manifest_invalid", "typed_args position must be a nonnegative integer")
            if item["name"] in names:
                raise BindingRejected("manifest_invalid", "typed_args names must be unique")
            names.add(item["name"])
            positions.append(item["position"])
        if sorted(positions) != list(range(len(positions))):
            raise BindingRejected("manifest_invalid", "typed_args positions must be contiguous from zero")
    elif kind == "mcp_tool":
        if transport_kind != "mcp_stdio":
            raise BindingRejected("manifest_invalid", "mcp_tool requires mcp_stdio transport")
        if type(bnd.get("tool")) is not str or not bnd["tool"].strip():
            raise BindingRejected("manifest_invalid", "mcp_tool requires a tool name")
    elif kind == "host_composition":
        mini = bnd.get("mini_tool")
        if type(mini) is not str or re.fullmatch(r"[a-z_]+[.][a-z_]+", mini) is None:
            raise BindingRejected("manifest_invalid", "host_composition mini_tool is invalid")
        call = bnd.get("native_call")
        if call is not None and (type(call) is not str or re.fullmatch(r"[A-Za-z0-9_.-]+", call) is None):
            raise BindingRejected("manifest_invalid", "host_composition native_call is invalid")
    else:
        raise BindingRejected("manifest_invalid", f"unsupported operation binding kind {kind!r}")
    retry = operation.get("retry_semantics")
    if not isinstance(retry, dict) or retry.get("mode") not in ("at_most_once", "idempotent") or retry.get("on_interrupt") not in ("UNCERTAIN_OUTCOME", "retry"):
        raise BindingRejected("manifest_invalid", "retry_semantics mode/on_interrupt are invalid")
    if retry["mode"] == "at_most_once" and retry["on_interrupt"] != "UNCERTAIN_OUTCOME":
        raise BindingRejected("manifest_invalid", "at_most_once requires UNCERTAIN_OUTCOME")
    if retry["mode"] == "idempotent" and retry["on_interrupt"] != "retry":
        raise BindingRejected("manifest_invalid", "idempotent requires retry")
    if type(operation.get("required_effects")) is not list or any(type(e) is not str for e in operation["required_effects"]):
        raise BindingRejected("manifest_invalid", "required_effects must be a list of strings")
    if type(operation.get("required_grants")) is not list or any(type(g) is not str for g in operation["required_grants"]):
        raise BindingRejected("manifest_invalid", "required_grants must be a list of strings")
    return bnd


def check_prerequisite_probes(manifest: dict, probes, installation_ref: str | None) -> None:
    declarations = manifest["capabilities"].get("prerequisite_probes", [])
    if type(declarations) is not list:
        raise BindingRejected("manifest_invalid", "capabilities.prerequisite_probes must be a list")
    now = datetime.datetime.now(datetime.timezone.utc)
    for probe in declarations:
        if not isinstance(probe, dict) or type(probe.get("id")) is not str or not probe["id"].strip():
            raise BindingRejected("manifest_invalid", "prerequisite probe requires a nonempty id")
        if "argv" in probe and (type(probe["argv"]) is not list or any(type(arg) is not str for arg in probe["argv"])):
            raise BindingRejected("manifest_invalid", "prerequisite probe argv must be a list of strings")
        if "effects" in probe and type(probe["effects"]) is not list:
            raise BindingRejected("manifest_invalid", "prerequisite probe effects must be a list")
        if "requires_binding" in probe and probe["requires_binding"] is not True:
            raise BindingRejected("manifest_invalid", "prerequisite probe requires_binding must be true")
        probe_id = probe["id"]
        record = probes.get(probe_id) if isinstance(probes, dict) else None
        if (not installation_ref or not isinstance(record, dict)
                or record.get("installation_ref") != installation_ref or record.get("outcome") != "passed"):
            raise BindingRejected("availability_blocked", f"required probe {probe_id!r} has no passing installation witness")
        try:
            measured = datetime.datetime.fromisoformat(record["measured_at"])
            expiry = datetime.datetime.fromisoformat(record["expires_at"])
            current = measured <= now < expiry
        except (KeyError, TypeError, ValueError):
            current = False
        if not current:
            raise BindingRejected("availability_blocked", f"required probe {probe_id!r} is stale or invalid")


def _retention_available(binding: Binding, *, retention=None) -> bool:
    if binding.shared_schema_ref in (_PUBLISHED_REF, _PUBLISHED_ID):
        # The v0.2 schema cannot describe an external launch.
        raise BindingRejected("profile_not_admitted", "owner-pending: T02")
    from .conformance_authority import tools_retention_available

    try:
        return tools_retention_available(
            binding=binding, operation_id=binding.operation_id, retention=retention,
        ) is True
    except Exception:
        return False


def verify_binding(
    manifest: dict,
    executable_path: str | Path,
    *,
    operation_id: str,
    host_grants: set[str] | frozenset[str] | None = None,
    requested_profile: str | None = None,
    capsule_perception_ceiling: str | None = None,
    allow_pending_evidence: bool = False,
    auth_grants: set[str] | frozenset[str] | None = None,
    auth_manifest_digest: str | None = None,
    auth_slot_id: str | None = None,
    auth_registry: dict | None = None,
    auth_probes: dict | None = None,
    auth_licence_disposition: str | None = None,
    auth_evidence_ref: str | None = None,
    auth_evidence_sha256: str | None = None,
    auth_spawn_granted: bool | None = None,
    installation_ref: str | None = None,
    retention=None,
) -> Binding:
    """Bind a user-selected executable to its manifest digest.

    Raises BindingRejected when the digest, grant or profile gate fails.
    Every check occurs before dispatch. The legacy allow_pending_evidence
    argument cannot authorize external_process.
    """
    if type(manifest) is not dict:
        raise BindingRejected("manifest_invalid", "manifest must be an object")
    # The manifest digest and launch arguments must refer to the same snapshot.
    manifest = copy.deepcopy(manifest)
    for key in _TOP_LEVEL_KEYS:
        if key not in manifest:
            raise BindingRejected("manifest_invalid", f"missing {key!r}")
    if manifest.get("schema") != "rea.slot_manifest/v2":
        raise BindingRejected("manifest_invalid", "unsupported manifest schema")
    slot_id = manifest.get("slot_id")
    if type(slot_id) is not str or len(slot_id) > 24 or _SLOT_ID_RE.fullmatch(slot_id) is None:
        raise BindingRejected("manifest_invalid", "slot_id must match the manifest grammar")
    if manifest.get("status") not in ("not_installed", "registered"):
        raise BindingRejected("manifest_invalid", "unknown manifest status")
    if not isinstance(manifest.get("provider_ref"), dict):
        raise BindingRejected("manifest_invalid", "provider_ref must be an object")
    name, source_url, version, licence = _check_upstream(manifest.get("upstream"))
    operation = _get_operation(manifest, operation_id)
    for key in _OPERATIONS_REQUIRED:
        if key not in operation:
            raise BindingRejected("manifest_invalid", f"operation missing {key!r}")
    limits = operation.get("limits")
    if not isinstance(limits, dict) or type(limits.get("timeout_s")) is not int or type(limits.get("max_output_bytes")) is not int:
        raise BindingRejected("manifest_invalid", "limits timeout_s/max_output_bytes must be integers")
    if limits["timeout_s"] < 1 or limits["max_output_bytes"] < 1:
        raise BindingRejected("manifest_invalid", "limits must be positive")

    profile = check_profile(manifest, operation, requested_profile)
    external = profile == "external_process"
    if external:
        if (type(auth_grants) not in (set, frozenset)
                or "grant.exec.external_process" not in auth_grants):
            raise BindingRejected("grant_refused", "authenticated grant.exec.external_process is required for external execution")
        effects = operation["required_effects"]
        if type(effects) is not list or "process.spawn:child" not in effects:
            raise BindingRejected("manifest_invalid", "external_process requires process.spawn:child in required_effects")
        if auth_spawn_granted is not True:
            raise BindingRejected("grant_refused", "host policy does not authorize process.spawn:child")
        if type(capsule_perception_ceiling) is not str or capsule_perception_ceiling not in _LEVEL_RANK:
            raise BindingRejected("perception_denied", "authenticated perception ceiling is missing or invalid")
    elif capsule_perception_ceiling is None:
        capsule_perception_ceiling = "none"
    grants = set(auth_grants if auth_grants is not None else host_grants or ())
    check_grants(operation, grants)
    if external and "grant.exec.external_process" not in operation["required_grants"]:
        raise BindingRejected("manifest_invalid", "external_process requires grant.exec.external_process in required_grants")

    contract = _get_contract_ref(manifest, operation.get("role"))
    if contract is None:
        raise BindingRejected("manifest_invalid", "missing contract_ref for operation role")
    _check_operation_refs(manifest, operation, contract)
    transport = manifest.get("transport")
    if not isinstance(transport, dict):
        raise BindingRejected("manifest_invalid", "transport must be an object")
    kind, argv_prefix, framing, env_allowlist = _check_transport_lists(transport)
    if external and (kind == "mcp_stdio" or (
        isinstance(operation.get("binding"), dict) and operation["binding"].get("kind") == "mcp_tool"
        and any(row["kind"] == "mcp_stdio" for row in (transport.get("alternates") or []))
    )):
        raise BindingRejected("availability_parked", "external MCP transport is parked")
    bnd = _check_operation_binding(operation, kind)
    required_level, optional_levels, sources = _check_transport_and_perception(manifest, capsule_perception_ceiling)
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict):
        raise BindingRejected("manifest_invalid", "evidence must be an object")
    shared_ref = evidence.get("shared_schema_ref")
    if type(shared_ref) is not str or not shared_ref.strip():
        raise BindingRejected("manifest_invalid", "evidence.shared_schema_ref must be a nonempty string")
    evidence_pin = _EVIDENCE_PINS.get(shared_ref)
    manifest_hash = _manifest_digest(manifest)
    if external and (type(installation_ref) is not str or not installation_ref.strip()
                     or auth_slot_id is None or auth_manifest_digest is None):
        raise BindingRejected("manifest_invalid", "authenticated installation, slot and manifest digest are required")
    if auth_slot_id is not None and auth_slot_id != slot_id:
        raise BindingRejected("manifest_invalid", f"installation slot {slot_id!r} does not belong to target {auth_slot_id!r}")
    if auth_manifest_digest is not None and auth_manifest_digest != manifest_hash:
        raise BindingRejected("manifest_invalid", "presented manifest digest does not match authenticated installation pin")
    if external and type(auth_registry) is not dict:
        raise BindingRejected("profile_not_admitted", "authenticated registry resolution is required for external execution")
    if auth_registry is not None:
        provider_obj = manifest.get("provider_ref", {})
        caps_obj = manifest.get("capabilities", {})
        adapter_obj_r = caps_obj.get("adapter_identity", {}) if isinstance(caps_obj, dict) else {}
        for reg_key, manifest_val in [
            ("provider_id", provider_obj.get("provider_id")),
            ("role", operation["role"]),
            ("adapter_id", adapter_obj_r.get("adapter_id") if isinstance(adapter_obj_r, dict) else None),
            ("provider_sha256", provider_obj.get("provider_sha256") if isinstance(provider_obj, dict) else None),
            ("provider_revision", provider_obj.get("provider_revision") if isinstance(provider_obj, dict) else None),
            ("implementation_sha256", provider_obj.get("implementation_sha256") if isinstance(provider_obj, dict) else None),
            ("adapter_sha256", adapter_obj_r.get("adapter_sha256") if isinstance(adapter_obj_r, dict) else None),
            ("request_digest", contract.get("request_digest")),
            ("result_digest", contract.get("result_digest")),
            ("request_schema_ref", contract.get("request_schema_ref")),
            ("result_schema_ref", contract.get("result_schema_ref")),
            ("execution_profile", contract.get("execution_profile")),
        ]:
            if reg_key not in auth_registry:
                raise BindingRejected("profile_not_admitted", f"registered resource {reg_key!r} does not resolve")
            if auth_registry.get(reg_key) != manifest_val:
                raise BindingRejected("profile_not_admitted", f"registered resource {reg_key!r} differs from manifest pin")
    if not isinstance(manifest["capabilities"], dict):
        raise BindingRejected("manifest_invalid", "capabilities must be an object")
    check_prerequisite_probes(manifest, auth_probes, installation_ref)

    use_disp = _licence_use_disposition(licence)
    if external and (use_disp != "external_execution" or auth_licence_disposition != "external_execution"):
        raise BindingRejected("availability_blocked", "reviewed intended use does not permit external_execution")

    # Resolve the user-selected installation to an absolute path before any
    # scratch working directory exists, so a same-named file under the
    # scratch can never redirect the launch.
    path = Path(executable_path)
    # Pin the installation directory while leaving the final component for
    # the launch gate to open without following symlinks.
    try:
        path = path.parent.resolve() / path.name
    except (OSError, RuntimeError) as exc:
        raise BindingRejected("binding_rejected", f"cannot resolve installation directory: {exc}") from exc
    if not path.is_file():
        raise BindingRejected("availability_not_installed", f"executable not a file: {path}")
    if not os.access(path, os.X_OK):
        raise BindingRejected("binding_rejected", f"executable is not executable: {path}")

    provider = manifest["provider_ref"]
    expected_impl = provider.get("implementation_sha256")
    caps = manifest.get("capabilities", {})
    adapter_obj = caps.get("adapter_identity", {}) if isinstance(caps, dict) else {}
    caps_adapter = adapter_obj.get("adapter_sha256") if isinstance(adapter_obj, dict) else None
    try:
        observed = sha256_of_file(path)
    except FileNotFoundError as exc:
        raise BindingRejected("availability_not_installed", f"executable was removed: {path}") from exc
    except (OSError, ValueError) as exc:
        raise BindingRejected("binding_rejected", f"cannot hash executable: {exc}") from exc

    if profile == "external_process":
        if not _is_sha256(expected_impl):
            raise BindingRejected("profile_not_admitted", "external_process requires implementation_sha256 pin")
        if observed != expected_impl:
            raise BindingRejected(
                "binding_rejected",
                f"observed {observed} != pinned {expected_impl}",
            )
        if manifest.get("status") == "registered" and not _is_sha256(caps_adapter):
            raise BindingRejected("profile_not_admitted", "adapter_sha256 pin missing for registered manifest")

    else:
        # offline_fixture still checks the digest when a pin exists, so a
        # tampered fixture is visible.  Null pins mean discovery-only.
        if _is_sha256(expected_impl) and observed != expected_impl:
            raise BindingRejected("binding_rejected", f"fixture observed {observed} != pinned {expected_impl}")

    if external:
        authenticated_pin = _EVIDENCE_PINS.get(auth_evidence_ref) if type(auth_evidence_ref) is str else None
        if evidence_pin is None or authenticated_pin != evidence_pin or auth_evidence_sha256 != evidence_pin[1]:
            raise BindingRejected("profile_not_admitted", "shared evidence reference and canonical digest must match the authenticated contract")
    elif auth_evidence_ref is not None and (type(auth_evidence_ref) is not str or _EVIDENCE_PINS.get(auth_evidence_ref) != evidence_pin):
        evidence_pin = None
    binding = Binding(
        manifest_digest=manifest_hash,
        slot_id=manifest["slot_id"],
        provider_id=str(provider.get("provider_id", "")),
        provider_revision=provider.get("provider_revision") if type(provider.get("provider_revision")) is str else "",
        implementation_sha256=str(expected_impl or ""),
        adapter_sha256=caps_adapter if isinstance(caps_adapter, str) else None,
        executable_path=str(path),
        provider_schema=provider.get("schema"),
        provider_sha256=provider.get("provider_sha256"),
        observed_sha256=observed,
        execution_profile=profile,
        grants=tuple(sorted(grants)),
        transport_kind=kind,
        argv_prefix=argv_prefix,
        framing=framing,
        cwd_policy=transport.get("cwd_policy"),
        env_allowlist=env_allowlist,
        operation_id=operation.get("id", operation_id),
        role=operation.get("role", ""),
        operation_binding=dict(bnd),
        timeout_s=int(limits["timeout_s"]),
        max_output_bytes=int(limits["max_output_bytes"]),
        required_effects=tuple(operation.get("required_effects", [])),
        retry_semantics=dict(operation.get("retry_semantics", {})),
        tool_version=version,
        upstream_licence_id=licence,
        source_url=source_url,
        use_disposition=use_disp,
        perception_required=required_level,
        perception_optional=optional_levels,
        observation_sources=sources,
        shared_schema_ref=shared_ref,
        shared_schema_sha256=evidence_pin[1] if evidence_pin is not None else None,
        installation_ref=installation_ref,
        adapter_id=adapter_obj.get("adapter_id") if isinstance(adapter_obj, dict) else None,
        request_schema_ref=contract["request_schema_ref"],
        request_digest=contract["request_digest"],
        result_schema_ref=contract["result_schema_ref"],
        result_digest=contract["result_digest"],
        retention_required=external and retention is not None,
    )
    if external:
        retention_options = {"retention": retention} if retention is not None else {}
        if not _retention_available(binding, **retention_options):
            raise BindingRejected("profile_not_admitted", "owner-pending: T01")
    return binding


# --------------------------------------------------------------------------
# Owner-integration default-on: conformance identity helpers
# --------------------------------------------------------------------------

def binding_conformance_identity(binding) -> dict:
    """Return the exact identity a fixture receipt must match.

    The mapping is the minimal closed set used by the conformance
    predicate: slot, operation, implementation digest and manifest
    digest, plus optional provider/adapter pins when the binding
    exposes them.  No executable path, argv or environment is included.
    """
    try:
        slot = getattr(binding, "slot_id", None)
    except Exception:
        slot = None
    try:
        op = getattr(binding, "operation_id", None)
    except Exception:
        op = None
    try:
        impl = getattr(binding, "implementation_sha256", None)
    except Exception:
        impl = None
    try:
        md = getattr(binding, "manifest_digest", None)
    except Exception:
        md = None
    try:
        pr = getattr(binding, "provider_revision", None)
    except Exception:
        pr = None
    try:
        ad = getattr(binding, "adapter_sha256", None)
    except Exception:
        ad = None
    return {
        "slot_id": str(slot) if isinstance(slot, str) else "",
        "operation_id": str(op) if isinstance(op, str) else "",
        "implementation_sha256": str(impl) if isinstance(impl, str) else "",
        "manifest_digest": str(md) if isinstance(md, str) else "",
        "provider_revision": str(pr) if isinstance(pr, str) else "",
        "adapter_sha256": str(ad) if isinstance(ad, str) else "",
    }


def verify_conformance_for_binding(
    binding,
    operation_id: str,
    *,
    policy,
    receipt=None,
    enrolment_check=None,
    retention_check=None,
    retention=None,
    now=None,
    activation_state: dict | None = None,
) -> bool:
    """Fail-closed conformance check for a verified Binding.

    Returns strictly ``True`` only when
    :func:`slot_host.runtime.conformance_authority.evaluate_default_on`
    allows: baseline coverage, fresh receipt matching the exact binding,
    MAIN enrolment and Tools retention.  Any exception, stale value or
    non-True seam result returns ``False``.  The helper never consults
    a filesystem registry or environment.
    """
    try:
        from .conformance_authority import evaluate_default_on  # lazy to avoid cycle
        decision = evaluate_default_on(
            binding=binding,
            operation_id=operation_id,
            policy=policy,
            receipt=receipt,
            enrolment_check=enrolment_check,
            retention_check=retention_check,
            retention=retention,
            now=now,
            activation_state=activation_state,
        )
        return decision.allowed is True
    except Exception:
        return False


# Public operation surface for the JSON/CLI/MCP layer.
def bind_executable(manifest: dict, executable_path: str, operation_id: str, host_grants: list[str], allow_pending_evidence: bool = False, **auth_kwargs) -> dict:
    """JSON-serialisable projection without raw paths, argv or env values."""
    binding = verify_binding(manifest, executable_path, operation_id=operation_id, host_grants=set(host_grants), allow_pending_evidence=allow_pending_evidence, **auth_kwargs)
    return {
        "manifest_digest": binding.manifest_digest,
        "slot_id": binding.slot_id,
        "provider_id": binding.provider_id,
        "provider_revision": binding.provider_revision,
        "implementation_sha256": binding.implementation_sha256,
        "adapter_sha256": binding.adapter_sha256,
        "observed_sha256": binding.observed_sha256,
        "execution_profile": binding.execution_profile,
        "transport_kind": binding.transport_kind,
        "framing": binding.framing,
        "cwd_policy": binding.cwd_policy,
        "grants": list(binding.grants),
    }
