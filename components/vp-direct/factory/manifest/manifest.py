"""Load the asset-factory contribution to the ViewPoint capability registry."""

import argparse
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path

from jsonschema import ValidationError


TOOL = "vp_factory_call"
CLI = "factory"
CAPABILITY = "vp.capability/v1"
MANIFEST = "vp.capability_manifest/v1"
REQUEST = "vp.control.req/v1"
REPLY = "vp.control.reply/v1"
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
REPO = SCHEMAS.parents[2]
ASSET_FACTORY = REPO / "components/asset-factory"
MODEL_OPS = frozenset({"af.generate", "af.matte", "af.rig", "af.motion_from_video"})

_spec = spec_from_file_location("vp_factory_schema_validation", SCHEMAS / "validation.py")
_validation = module_from_spec(_spec)
_spec.loader.exec_module(_validation)
validate_contract = _validation.validate_contract


class ManifestError(ValueError):
    """The manifest cannot contribute a factory capability."""


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ManifestError(f"Duplicate JSON member: {name}")
        result[name] = value
    return result


def _reject_constant(value):
    raise ManifestError(f"Invalid JSON number: {value}")


def _schema(filename):
    return json.loads((SCHEMAS / filename).read_bytes())


@lru_cache(maxsize=1)
def _contracts():
    return (
        _schema("vp_factory_call.args_v1.schema.json"),
        _schema("vp.control.req_v1.schema.json"),
        _schema("vp.capability_v1.schema.json")["properties"]["risk_class"]["enum"],
        _schema("adapter_ops.json")[TOOL]["ops"],
    )


def _digest(value):
    return sha256(_validation.canonical_bytes(value)).hexdigest()


def _hunyuan(model):
    name = model.get("name", "")
    return isinstance(name, str) and "".join(c for c in name.casefold() if c.isalnum()) == "hunyuan3d21"


def _model_qualified(entry, verified_qualifications):
    """Reconcile declarations against independent pins for the exact model weights."""
    model = entry.get("provider", {}).get("model")
    if model is None:
        return None
    qualification = model["qualification"]
    receipt = qualification.get("receipt")
    if qualification["state"] != "runnable":
        return False
    if receipt is not None:
        pin = (model["name"], model["weights"]["tree_sha256"], receipt["path"], receipt["sha256"])
        if pin in verified_qualifications and model["license"]["territory_ok"] is True:
            return True
    # Retain an otherwise valid receipt reference for diagnostics, if present.
    qualification["state"] = "unqualified"
    qualification["receipt"] = receipt
    qualification["reason"] = "Qualification or territory approval is unverified"
    return False


def _row(op, declaration, frozen, qualified, levels):
    policy = frozen[op]
    if declaration is not None:
        if (levels.index(declaration["risk_class"]) < levels.index(policy["risk_class"])
                or policy["operator_only"] and not declaration["operator_only"]):
            raise ManifestError(f"Manifest weakens frozen policy for {op}")
        for field in ("args_schema_sha256", "reply_schema_sha256", "binding"):
            if declaration[field] != policy[field]:
                raise ManifestError(f"Manifest disagrees with frozen {field} for {op}")
    row = {key: deepcopy(policy[key]) for key in (
        "supported", "operator_only", "risk_class", "args_schema_sha256",
        "reply_schema_sha256", "binding",
    )}
    if declaration is not None:
        row.update(declaration)
    row["op"] = op
    row["supported"] = (
        declaration is not None
        and declaration["supported"]
        and policy["supported"]
        and policy["binding"]["kind"] == "factory_cmd"
        and not policy.get("refusal_only", False)
        and (qualified is True or qualified is None and op not in MODEL_OPS)
    )
    return row


def _read_manifest(raw):
    document = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if (not isinstance(document, dict) or document.get("schema") != MANIFEST
            or document.get("origin") != "factory"
            or not isinstance(document.get("capabilities"), list)):
        raise ManifestError(f"Expected a factory {MANIFEST} document")
    capabilities = document["capabilities"]
    for entry in capabilities:
        if not isinstance(entry, dict):
            raise ManifestError("Malformed factory capability")
        model = entry.get("provider")
        if isinstance(model, dict) and isinstance(model.get("model"), dict) and _hunyuan(model["model"]):
            raise ManifestError("Hunyuan3D-2.1 is excluded")
    if len(capabilities) != 1:
        raise ManifestError(f"Expected one registry entry for {TOOL}")
    return capabilities[0]


def _registry_entry(entry, verified_qualifications):
    if (entry.get("tool"), entry.get("cli"), entry.get("json_op"), entry.get("origin")) != (
        TOOL, CLI, TOOL, "factory"
    ):
        raise ManifestError(f"Expected {TOOL}, {CLI}, and factory origin")
    if not isinstance(entry.get("supported"), bool):
        raise ManifestError("Capability support must be explicit")

    args, _, levels, frozen = _contracts()
    try:
        qualified = _model_qualified(entry, verified_qualifications)
        # Validate declaration shape before evaluating support. The effective
        # capability is validated again after admission policy is applied.
        submitted = deepcopy(entry)
        submitted["supported"] = False
        for row in submitted.get("op_table", ()):
            if row.get("supported") is True:
                row["supported"] = False
        validate_contract(CAPABILITY, submitted)
    except (AttributeError, KeyError, TypeError, ValidationError) as exc:
        raise ManifestError("Invalid factory capability") from exc

    if entry["args_schema_sha256"] != _digest(args):
        raise ManifestError("Factory argument schema digest mismatch")
    if not entry["ops"]:
        raise ManifestError("The factory operation list is empty")
    declarations = {row["op"]: row for row in entry.get("op_table", ())}
    if set(entry["ops"]) - frozen.keys():
        raise ManifestError("ARGS_SCHEMA_ABSENT")
    rows = [_row(op, declarations.get(op), frozen, qualified, levels) for op in entry["ops"]]
    entry["op_table"] = rows
    entry["supported"] = any(row["supported"] for row in rows)
    entry["operator_only"] = all(row["operator_only"] for row in rows)
    entry["risk_class"] = max((row["risk_class"] for row in rows), key=levels.index)
    validate_contract(CAPABILITY, entry)
    return entry


class FactoryManifest:
    """One validated snapshot for registry registration, discovery and offline preview."""

    def __init__(self, entry, evidence):
        self._entry = entry
        self._evidence = evidence
        self._rows = {row["op"]: row for row in entry["op_table"]}

    def registry_entry(self):
        """Pass this entry to the shared capability registry."""
        return deepcopy(self._entry)

    def tools_list(self):
        """MCP tool contribution with a flat inputSchema and manifest-only selectors."""
        args, request, _, _ = _contracts()
        selected = list(self._rows)
        # Keep the referenced payload resources local for offline MCP clients.
        rules = []
        definitions = {}
        for op in selected:
            name = f"{op}.args"
            payload = deepcopy(args["$defs"][name])
            del payload["$id"], payload["additionalProperties"]
            payload["properties"]["inputs"]["items"]["$ref"] = "#/$defs/artifact_ref"
            definitions[name] = payload
            rules.append({
                "if": {"properties": {"op": {"const": op}}, "required": ["op"]},
                "then": {"$ref": f"#/$defs/{name}"},
            })
        artifact = _schema("vp.artifact_ref_v1.schema.json")
        del artifact["$id"]
        definitions["artifact_ref"] = artifact
        context_fields = {
            key: value for key, value in request["properties"]["context"]["properties"].items()
            if key not in request["properties"]["context"]["required"]
        }
        return [{
            "name": TOOL,
            "inputSchema": deepcopy({
                "type": "object",
                "properties": {
                    **args["properties"],
                    **context_fields,
                    "op": {"enum": selected},
                    "request_id": request["properties"]["request_id"],
                    "dry_run": request["properties"]["dry_run"],
                },
                "required": [*args["required"], "request_id", "dry_run"],
                "additionalProperties": False,
                "allOf": rules,
                "$defs": definitions,
            }),
        }]

    def cli_ops(self):
        return {self._entry["cli"]: list(self._rows)}

    def json_lane_ops(self):
        return {self._entry["json_op"]: list(self._rows)}

    def preview(self, request):
        """Return a frozen control receipt for an already normalized dry-run."""
        validate_contract(REQUEST, request)
        if request["op"] != TOOL or request["dry_run"] is not True:
            raise ManifestError(f"Expected a {TOOL} dry-run")
        row = self._rows.get(request["args"]["op"])
        if row is None:
            raise ManifestError("Operation is absent from the factory manifest")

        evidence = dict(self._evidence)
        if row["operator_only"] and request["context"]["caller"] != "operator":
            code = "OWNER"
        else:
            code = "OK" if row["supported"] else "HOLD"
        result = {"artifact_refs": [], "receipt": evidence} if code == "OK" else {}
        reply = {
            "schema": REPLY,
            "request_id": request["request_id"],
            "op": TOOL,
            "ok": code == "OK",
            "code": code,
            "result": result,
            "effective_request_sha256": _digest(request),
            "evidence": [evidence],
            "not_verified": ["factory execution"],
        }
        _validation.validate_adapter_exchange(request, reply)
        return reply


def load_manifest(path, *, asset_factory_root=ASSET_FACTORY, verified_qualifications=()):
    """Read a manifest under the trusted factory root.

    Qualification pins are (model name, weight tree SHA-256, receipt path,
    receipt SHA-256) tuples supplied by the verifier. Manifest claims cannot
    add a pin, and the loader never opens paths supplied inside the document.
    """
    root = Path(asset_factory_root).resolve()
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ManifestError("Manifest must be a file under the asset-factory root")
    raw = path.read_bytes()
    try:
        entry = _registry_entry(_read_manifest(raw), frozenset(verified_qualifications))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ManifestError("Invalid factory manifest JSON") from exc
    path_ref = path.relative_to(REPO) if path.is_relative_to(REPO) else path
    return FactoryManifest(entry, {"path": path_ref.as_posix(), "sha256": sha256(raw).hexdigest()})


def main(argv=None):
    """Offline CLI for inspecting M1 dry-runs before the live lanes are wired."""
    parser = argparse.ArgumentParser(description="Preview a factory control request")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--json-arg", required=True, type=Path)
    parser.add_argument("--dry-run", required=True, action="store_true")
    options = parser.parse_args(argv)
    try:
        manifest = load_manifest(options.manifest)
        request = json.loads(options.json_arg.read_bytes(), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
        validate_contract(REQUEST, request)
        request["context"]["caller"] = "agent"
        request["dry_run"] = options.dry_run
        reply = manifest.preview(request)
    except (OSError, ValueError, ValidationError) as exc:
        parser.error(str(exc))
    print(_validation.canonical_bytes(reply).decode("utf-8"), end="")
    return 0 if reply["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
