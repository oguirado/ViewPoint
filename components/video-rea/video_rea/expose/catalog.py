# SPDX-License-Identifier: AGPL-3.0-only
"""Read a coherent selector/schema snapshot and project its offline MCP declaration."""

from copy import deepcopy
from pathlib import Path
import re

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from .wire import digest, loads


ROOT = Path(__file__).resolve().parents[2]
VP_SCHEMAS = ROOT.parent / "vp-direct" / "schemas"
RECORD_ID = re.compile(r"\brea\.[a-zA-Z0-9_.-]+/v[0-9]+\b")

_CONTEXT_PROPERTIES = {
    "caller": {"enum": ["agent", "operator"]},
    "agent_instance": {"type": "string", "minLength": 1},
    **{key: {"type": "string", "minLength": 1}
       for key in ("session_id", "capsule_id", "incarnation", "observation_id")},
    "authority_epoch": {"type": "integer", "minimum": 0},
}


def read(path):
    return loads(path.read_bytes())


class Catalog:
    def __init__(self, root):
        self.table = read(root / "vp_rea_call.op_table.json")
        self.tool = self.table["tool"]
        self.rows = self.table["op_table"]
        self.envelope = read(root / "vp_rea_call.args_v1.schema.json")
        selectors = self.envelope["properties"]["op"]["enum"]
        if len(selectors) != len(self.rows) or set(selectors) != self.rows.keys():
            raise ValueError("operation inventory differs from selector schema")
        definitions = [d for d in self.envelope["$defs"].values() if isinstance(d, dict)]
        self.resources = {d["title"]: d for d in definitions if "$id" in d and "title" in d}
        for row in self.rows.values():
            for key in ("args_schema", "reply_schema"):
                resource = self.resources.get(row[key])
                if not resource or digest(resource) != row[key + "_sha256"]:
                    raise ValueError(f"invalid resource pin for {row[key]}")
        selectors_to_refs = {
            rule["if"]["properties"]["op"]["const"]: rule["then"]["$ref"]
            for rule in self.envelope["allOf"]
            if "$ref" in rule.get("then", {})
        }
        if selectors_to_refs != {op: self.resources[row["args_schema"]]["$id"]
                                 for op, row in self.rows.items()}:
            raise ValueError("selector conditions differ from pinned argument resources")

        # Resolve only the schema dependency named by the REA contract, offline.
        artifact = read(VP_SCHEMAS / "vp.artifact_ref_v1.schema.json")
        registry = Registry().with_resources(
            (d["$id"], DRAFT202012.create_resource(d))
            for d in [self.envelope, artifact, *definitions] if "$id" in d
        )
        self.validator = Draft202012Validator(self.envelope, registry=registry)
        self.resolver = registry.resolver(self.envelope["$id"])
        # Offline preview has one local selector and its own closed carrier.
        self.metadata = {
            "request_id": {"type": "string", "minLength": 1},
            "dry_run": {"type": "boolean"},
        }
        context = {"type": "object", "properties": deepcopy(_CONTEXT_PROPERTIES),
                   "required": ["caller", "agent_instance"], "additionalProperties": False}
        properties = {"schema": {"const": "vp.control.req/v1"}, "op": {"const": self.tool},
                      "args": {"type": "object"}, "context": context, **self.metadata}
        self.request_schema = {"type": "object", "properties": properties,
                               "required": list(properties), "additionalProperties": False}
        self.request_validator = Draft202012Validator(self.request_schema)
        self.context_fields = {
            k: v for k, v in context["properties"].items()
            if k not in ("caller", "agent_instance")
        }

    def _portable(self, node, resolver, flatten):
        if isinstance(node, list):
            return [self._portable(item, resolver, flatten) for item in node]
        if not isinstance(node, dict):
            return node
        if "$id" in node:
            resolver = resolver.in_subresource(DRAFT202012.create_resource(node))
        result = {k: self._portable(v, resolver, flatten) for k, v in node.items()
                  if k not in {"$id", "$defs", "$ref"}}
        if "$ref" in node:
            ref = resolver.lookup(node["$ref"])
            target = self._portable(ref.contents, ref.resolver, flatten)
            return {"allOf": [target, result]} if result else target
        if flatten and node.get("$id") in flatten:
            result["properties"] = {**self.context_fields, **result["properties"], **self.metadata}
            result["required"] = [*result["required"], *self.metadata]
        return result

    def tools(self):
        extended = {self.envelope["$id"]}
        extended.update(self.resources[row["args_schema"]]["$id"] for row in self.rows.values())
        return [{"name": self.tool, "description": "Preview a local video reconstruction operation.",
                 "inputSchema": self._portable(self.envelope, self.resolver, extended)}]

    def describe(self, op):
        row = self.rows[op]
        return {"op": op, **deepcopy(row),
                "inputSchema": self._portable(self.resources[row["args_schema"]], self.resolver, ()),
                "resultSchema": self._portable(self.resources[row["reply_schema"]], self.resolver, ())}

    def plan(self, op):
        row = self.rows[op]
        resource = self.resources[row["reply_schema"]]
        records = RECORD_ID.findall(resource.get("description", ""))
        return {"op": op, "binding": deepcopy(row["binding"]), "risk_class": row["risk_class"],
                "supported": row["supported"], "args_schema": row["args_schema"],
                "reply_schema": row["reply_schema"], "produces": records or ["AUSENTE"], "executed": False}

