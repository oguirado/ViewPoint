# SPDX-License-Identifier: AGPL-3.0-only
"""Compile pinned REA manifests to a single route table and four public faces."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

REQUEST = "vp.control.req/v1"
REPLY = "vp.control.reply/v1"
SELECTOR = "rea.slot.project"
DISCOVERY = "vp3d.host_discovery/v1"
REQUEST_ID = r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}"
SLOT_ID = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
OPERATION_ID = r"[a-z][a-z0-9_]*"
DIGEST = r"[0-9a-f]{64}"
ARGUMENTS = {
    "inputs": {
        "type": "array", "maxItems": 16, "default": [],
        "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "role": {"type": "string", "minLength": 1},
                "ref": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "pattern": f"^{DIGEST}$(?!\\s)"},
            },
            "required": ["role", "ref", "sha256"],
        },
    },
    "params": {"type": "object", "default": {}},
    "perception_level": {"enum": ["none", "cdp_atspi", "vision"], "default": "none"},
}
EXIT = {
    "OK": 0, "SCHEMA_INVALID": 2, "DIVERGENT_DEFAULT": 2, "ALIAS_COLLISION": 2,
    "EXPECTATION_FAILED": 3, "EXCHANGE_PATH_OUTSIDE_ROOT": 3, "INPUT_PIN_MISMATCH": 3,
    "UNKNOWN_TOOL_ID": 7, "ABSENT": 7, "REQUEST_ID_CONFLICT": 12,
    "UNCERTAIN_OUTCOME": 13, "PERCEPTION_DENIED": 15,
    "PROFILE_NOT_ADMITTED": 16, "AVAILABILITY_NOT_INSTALLED": 16,
}


class Refusal(Exception):
    def __init__(self, code, fingerprint=None):
        super().__init__(code)
        self.code = code
        self.fingerprint = fingerprint


def canonical(value):
    # Same LF-terminated encoding as vp-direct/schemas/validation.py.
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value):
    return sha256(canonical(value)).hexdigest()


def parse(data):
    def unique(pairs):
        obj = {}
        for name, value in pairs:
            if name in obj:
                raise ValueError("Duplicate JSON member")
            obj[name] = value
        return obj

    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant {value}")

    value = json.loads(data, object_pairs_hook=unique, parse_constant=reject_constant)
    canonical(value)  # Also reject non-finite overflow or illegal Unicode.
    return value


def pinned(pin):
    path = Path(pin["path"])
    data = path.read_bytes()
    if sha256(data).hexdigest() != pin["sha256"]:
        raise ValueError(f"Digest mismatch for {path}")
    return data


def closed(properties, required=()):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(required)}


def validate(schema, value):
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise Refusal("SCHEMA_INVALID") from exc


def defaults(schema):
    return {key: spec["default"] for key, spec in schema["properties"].items() if "default" in spec}


def control_overlay(request, reply):
    """Extend the pinned on-disk carriers in memory for this local candidate."""
    request, reply = deepcopy(request), deepcopy(reply)
    for schema in (request, reply):
        schema["properties"]["op"]["enum"].append(SELECTOR)
    op = {"properties": {"op": {"const": SELECTOR}}, "required": ["op"]}
    route_args = closed({"route_id": {"type": "string", "minLength": 1},
                         "arguments": {"type": "object"}}, ("route_id", "arguments"))
    request["allOf"].append({"if": op, "then": {"properties": {"args": route_args}}})
    reply["allOf"][0]["if"]["properties"]["op"]["not"]["enum"].append(SELECTOR)
    reply["allOf"].append({"if": op, "then": {
        "if": {"properties": {"ok": {"const": True}}, "required": ["ok"]},
        "then": {"properties": {"code": {"const": "OK"}, "result": {"type": "object"}}},
        "else": {"properties": {"code": {"enum": sorted(set(EXIT) - {"OK"})},
                                "result": closed({})}},
    }})
    return request, reply


class Catalog:
    def __init__(self, documents, base, domain, bindings):
        registry = Registry().with_resource(base["$id"], Resource.from_contents(base))
        validator = Draft202012Validator(domain, registry=registry)
        self.routes = {}
        self.cli = {}
        self.mcp = {}
        for document in documents:
            validator.validate(document)
            slot = document["slot_id"]
            manifest_sha = digest(document)
            for operation in sorted(document["operations"], key=lambda row: row["id"]):
                name = operation["id"]
                if name == "conform":
                    raise Refusal("ALIAS_COLLISION")  # Reserved ext.<slot>.conform route.
                route_id = f"ext.{slot}.{name}"
                adapter = bindings.get(route_id)
                admitted = adapter is not None
                if admitted:
                    if (document["status"] != "not_installed"
                            or adapter["mode"] != "offline_fixture"
                            or adapter["manifest_sha256"] != manifest_sha
                            or operation["binding"]["kind"] != "cli_subcommand"
                            or operation["binding"]["argv"] != [name]
                            or operation["binding"]["typed_args"]
                            or document["perception"]["required_level"] != "none"):
                        raise Refusal("PROFILE_NOT_ADMITTED")
                    if (not adapter["command"] or not Path(adapter["command"][0]).is_absolute()
                            or type(adapter["timeout_s"]) is not int or adapter["timeout_s"] < 1
                            or type(adapter["max_output_bytes"]) is not int
                            or adapter["max_output_bytes"] < 1):
                        raise Refusal("PROFILE_NOT_ADMITTED")
                    for member in ("params", "result"):
                        schema = adapter[member]
                        Draft202012Validator.check_schema(schema)
                        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                            raise Refusal("SCHEMA_INVALID")
                params = deepcopy(adapter["params"]) if admitted else {"type": "object"}
                # Default params only when the pinned adapter accepts the empty object.
                params.pop("default", None)
                if Draft202012Validator(params).is_valid({}):
                    params["default"] = {}
                arguments = deepcopy(ARGUMENTS)
                arguments["params"] = params
                required = () if "default" in params else ("params",)
                public = closed(arguments | {
                    "request_id": {"type": "string", "pattern": f"^{REQUEST_ID}$(?!\\s)"},
                    "dry_run": {"type": "boolean", "default": False},
                }, ("request_id", *required))
                json_args = closed(arguments, required)
                row = {
                    "id": route_id, "slot_id": slot, "operation": name, "role": operation["role"],
                    "input_ref": operation["input_ref"], "output_ref": operation["output_ref"],
                    "summary": document["discovery"]["operation_descriptions"][name],
                    "keywords": sorted(document["discovery"]["search_terms"]),
                    "manifest_sha256": manifest_sha, "slot_version": document["upstream"]["version"],
                    "availability": "offline_fixture" if admitted else document["status"],
                    "live_use_permitted": False, "outside_capsule": True,
                    "callable": admitted, "limits": operation["limits"],
                    "cli": {"argv": ["slot", "call", slot, name], "argument_schema": deepcopy(public)},
                    "mcp": {"name": f"rea_ext_{slot.replace('-', '_')}__{name}",
                            "inputSchema": deepcopy(public)},
                    "json": {"schema": REQUEST, "reply_schema": REPLY, "op": SELECTOR,
                             "route_id": route_id, "argument_schema": json_args,
                             "dry_run": {"type": "boolean", "default": False}},
                    "router": {"id": route_id, "argument_schema": deepcopy(public)},
                }
                cli, mcp = tuple(row["cli"]["argv"]), row["mcp"]["name"]
                if route_id in self.routes or cli in self.cli or mcp in self.mcp:
                    raise Refusal("ALIAS_COLLISION")
                self.check_defaults(row)
                self.routes[route_id], self.cli[cli], self.mcp[mcp] = row, route_id, route_id
        # Bindings for removed routes are dormant. Existing attempts keep their snapshots.

    @staticmethod
    def check_defaults(row):
        json_defaults = defaults(row["json"]["argument_schema"])
        json_defaults["dry_run"] = row["json"]["dry_run"]["default"]
        public = (row["cli"]["argument_schema"], row["mcp"]["inputSchema"],
                  row["router"]["argument_schema"])
        if any(canonical(defaults(schema)) != canonical(json_defaults) for schema in public):
            raise Refusal("DIVERGENT_DEFAULT")

    def discovery(self):
        rows = [self.routes[key] for key in sorted(self.routes)]
        return {"schema": DISCOVERY, "revision_sha256": digest(rows), "routes": rows}

    def tools(self):
        return [{"name": row["mcp"]["name"], "inputSchema": row["mcp"]["inputSchema"],
                 "description": row["summary"], "title": row["id"]}
                for row in sorted(self.routes.values(), key=lambda row: row["mcp"]["name"])
                if row["callable"]]
