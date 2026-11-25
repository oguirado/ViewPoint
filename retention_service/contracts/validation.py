"""Local schema registration and validation for the retention wire contract."""

from hashlib import sha256
from ipaddress import ip_address
import json
import math
from pathlib import Path
import re

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from .formats import format_checker

DRAFT_URI = "https://json-schema.org/draft/2020-12/schema"
RECORD_V02_URI = "https://metaviewer.local/contracts/v0.2/attempt_evidence_record.schema.json"
RECORD_V03_URI = "https://metaviewer.local/contracts/v0.3/attempt_evidence_record.schema.json"
INDEX_V1_URI = "rea.retention_index/v1"
FROZEN_PINS = (
    (RECORD_V02_URI, "attempt_evidence_record.v0.2.schema.json",
     "b3bfd6715abfa60a0866b655d23088d8bee09b0cf8e0a3b44c156e5692c19d8c"),
    (RECORD_V03_URI, "attempt_evidence_record.v0.3.schema.json",
     "9402c78a4780cab53d9ad9726b103d05e76b504fe693c16b572078c9d8c98dc7"),
    (INDEX_V1_URI, "rea_retention_index.v1.schema.json",
     "9c4113e4981e0b9356e642534cc3fc9684ab03a51265ebdfe5e934f8207e98ad"),
)
FREEZE_LIST_SHA256 = "3af141229845b2e88dc36e698e29c62d36d4494b55c7b19653fef2ecd18c962d"
OFFLINE_RECORD_SHA256 = "99db48313f14d8c9a9d709b9a390bbc6b34c41934983b6f273bf4c3b37d7ee1b"
REJECTED_SCHEMA_REFS = frozenset((
    "evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending",
    "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v1",
))
REJECTED_RECEIPT_REFS = frozenset(("shared:fix", "shared:fix-attempt"))
WIRE_PATH = Path(__file__).with_name("wire.v1.schema.json")

# Supported assertions come from the validator. Scope-changing keywords require
# a separate contract review before this registry can admit them.
KEYWORDS = (set(Draft202012Validator.VALIDATORS) - {"$dynamicRef"}) | {
    "$schema", "$id", "$defs", "$comment", "title", "description", "default",
    "examples", "deprecated", "readOnly", "writeOnly", "then", "else",
    "minContains", "maxContains",
}


class ContractError(ValueError):
    """The supplied bytes or data violate the retention contract."""


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ContractError("duplicate JSON member")
        obj[key] = value
    return obj


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ContractError("non-finite JSON number")
    return value


def _reject_constant(_text):
    raise ContractError("non-finite JSON number")


def strict_json(raw):
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_float=_finite_float, parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ContractError("invalid UTF-8 JSON") from exc


def _schema_nodes(schema):
    yield schema
    if isinstance(schema, dict):
        for child in DRAFT202012.subresources_of(schema):
            yield from _schema_nodes(child)


def _check_schema(schema, resource, registry, checker):
    if schema.get("$schema") != DRAFT_URI:
        raise ContractError("expected Draft 2020-12 schema")
    Draft202012Validator.check_schema(schema)
    resolver = registry.resolver_with_root(resource)
    for node in _schema_nodes(schema):
        if isinstance(node, bool):
            continue
        unsupported = node.keys() - KEYWORDS
        if node is not schema:
            unsupported |= node.keys() & {"$schema", "$id"}
        if unsupported:
            raise ContractError(f"unsupported schema keyword or scope: {sorted(unsupported)}")
        if "format" in node and node["format"] not in checker.checkers:
            raise ContractError(f"unsupported format assertion: {node['format']}")
        if "pattern" in node:
            re.compile(node["pattern"])
        for pattern in node.get("patternProperties", {}):
            re.compile(pattern)
        if "$ref" in node:
            try:
                target = resolver.lookup(node["$ref"]).contents
            except Unresolvable as exc:
                raise ContractError(f"unresolved local schema reference: {node['$ref']}") from exc
            if not isinstance(target, (dict, bool)):
                raise ContractError("schema reference does not identify a schema")


def _verified_bytes(path, digest):
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != digest:
        raise ContractError(f"frozen digest mismatch: {path.name}")
    return raw


class ContractValidator:
    """Register verified frozen schemas in memory and validate named wire forms."""

    def __init__(self, frozen_dir, *, wire_path=WIRE_PATH):
        frozen_dir = Path(frozen_dir)
        _verified_bytes(frozen_dir / "FROZEN_SHA256SUMS", FREEZE_LIST_SHA256)
        frozen = {}
        for uri, filename, digest in FROZEN_PINS:
            schema = strict_json(_verified_bytes(frozen_dir / filename, digest))
            if schema.get("$id", uri) != uri:
                raise ContractError(f"frozen URI mismatch: {filename}")
            frozen[uri] = schema

        wire = strict_json(Path(wire_path).read_bytes())
        resources = {uri: DRAFT202012.create_resource(schema) for uri, schema in frozen.items()}
        # The empty registry has no retrieval callback for file or network URIs.
        registry = Registry().with_resources(resources.items())
        checker = format_checker()
        for uri, schema in frozen.items():
            _check_schema(schema, resources[uri], registry, checker)
        _check_schema(wire, DRAFT202012.create_resource(wire), registry, checker)

        self._pinned = {
            uri: Draft202012Validator(schema, registry=registry, format_checker=checker)
            for uri, schema in frozen.items()
        }
        self._wire = {
            name: Draft202012Validator(
                {**wire, "$ref": f"#/$defs/{name}"}, registry=registry, format_checker=checker,
            )
            for name in wire["$defs"]
        }

    def validate(self, name, value):
        if name not in self._wire:
            raise ContractError("unknown retention wire form")
        self._validate(self._wire[name], value)

    def validate_pinned(self, uri, value):
        if uri not in self._pinned:
            raise ContractError("unregistered schema URI")
        self._validate(self._pinned[uri], value)

    @staticmethod
    def _validate(validator, value):
        try:
            validator.validate(value)
        except ValidationError as exc:
            path = "/".join(map(str, exc.absolute_path))
            raise ContractError(f"schema assertion {exc.validator} failed at /{path}") from exc

    def load_config(self, config):
        self.validate("ServerConfig", config)
        effective = {"local_only": False, **config}
        if not effective["local_only"] and ip_address(effective["tcp_bind_ip"]).is_unspecified:
            raise ContractError("wildcard TCP binding is forbidden")
        return effective

    def validate_index(self, value):
        self.validate_pinned(INDEX_V1_URI, value)
        objects = value["objects"]
        if len({item["object_ref"] for item in objects}) != len(objects):
            raise ContractError("duplicate index object reference")
        profiles = [item for item in objects if item["role"] == "rea_profile"]
        if len(profiles) != 1 or "side" in profiles[0] or profiles[0]["sha256"] != value["profile_sha256"]:
            raise ContractError("index profile binding mismatch")

    def validate_live_receipt_binding(self, schema_ref, schema_sha256, receipt_ref):
        if not all(isinstance(value, str) for value in (schema_ref, schema_sha256, receipt_ref)):
            raise ContractError("receipt binding fields must be strings")
        if (schema_ref in REJECTED_SCHEMA_REFS or schema_sha256 == OFFLINE_RECORD_SHA256
                or (schema_ref, schema_sha256) != (
                    "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.3", FROZEN_PINS[1][2],
                )):
            raise ContractError("unapproved live receipt schema pin")
        if receipt_ref in REJECTED_RECEIPT_REFS:
            raise ContractError("offline receipt reference")
        self.validate("ReceiptRef", receipt_ref)
