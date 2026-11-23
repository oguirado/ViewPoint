"""Offline structural and cross-field checks for the vp-direct wire contracts."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, RefResolver, ValidationError


SCHEMA_DIR = Path(__file__).resolve().parent


class _LocalResolver(RefResolver):
    def resolve_remote(self, uri):
        raise ValidationError(f"Unknown local contract reference {uri!r}")


@lru_cache(maxsize=1)
def _store():
    resources = {}
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for resource in (document, *document.get("$defs", {}).values()):
            name = resource.get("$id")
            if name:
                if name in resources:
                    raise ValueError(f"Duplicate contract {name}")
                resources[name] = resource
    return resources


def canonical_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _check_forge(tool):
    params = tool["args_schema"]
    if len(canonical_bytes(params)) > 4096:
        raise ValidationError("FORGE_SCHEMA_INVALID: argument schema exceeds 4096 bytes")
    if set(params.get("required", ())) - params.get("properties", {}).keys():
        raise ValidationError("FORGE_SCHEMA_INVALID: required parameter is undeclared")


def _check_capability(entry):
    if "op_table" in entry and [row["op"] for row in entry["op_table"]] != entry["ops"]:
        raise ValidationError("ops must be the ordered projection of op_table")


def _check_index(index):
    entries = index["entries"]
    keys = []
    for entry in entries:
        for op in entry["ops"] or [""]:
            keys.append((entry["tool"], op))
    if keys != sorted(set(keys)):
        raise ValidationError("Index entries must be unique and ordered by (tool, op)")
    content = {k: v for k, v in index.items() if k != "index_sha256"}
    if index["index_sha256"] != hashlib.sha256(canonical_bytes(content)).hexdigest():
        raise ValidationError("index_sha256 does not match canonical index content")


def _check_route_reply(reply):
    for candidate in reply["candidates"]:
        args = candidate["json"]["args"]
        flat = candidate["mcp_call"]["arguments"]
        if any(key not in flat or flat[key] != value for key, value in args.items()):
            raise ValidationError("MCP arguments disagree with the normalized JSON request")
        request = candidate["json"]
        metadata = dict(request["context"], request_id=request["request_id"], dry_run=request["dry_run"])
        for key in flat.keys() - args.keys():
            if key not in metadata or flat[key] != metadata[key]:
                raise ValidationError("MCP field is outside the normalized request")


def _check_trace(trace):
    start, end = trace["start_mono_ns"], trace["end_mono_ns"]
    timestamps = [start, *(event["mono_ns"] for event in trace["events"]), end]
    if timestamps != sorted(timestamps):
        raise ValidationError("Focus events must be ordered within the trace interval")


def _check_pack(pack):
    if pack["requested"] != pack["resolved"]:
        raise ValidationError("Image pack resolution differs from its requested pins or modes")


def _check_snapshot(receipt):
    participants = set(receipt["participants"])
    acked = set()
    for ack in receipt["acks"]:
        participant = ack["participant"]
        if ack["generation"] != receipt["generation"] or participant not in participants or participant in acked:
            raise ValidationError("Snapshot acknowledgement has the wrong generation or participant")
        acked.add(participant)
    if receipt["complete"] and acked != participants:
        raise ValidationError("Complete snapshot lacks participant acknowledgements")


def _check_admission(decision):
    if decision["admitted"] and decision["reserved_peak_bytes"] < decision["old_ring_bytes"] + decision["new_ring_bytes"]:
        raise ValidationError("Resize peak must cover the old and new ring together")


def _check_guest(guest):
    if guest.get("workload_id") == guest["surface_id"]:
        raise ValidationError("workload_id cannot stand in for surface_id")


def _check_receipt(receipt):
    levels = _store()["urn:vp.capability:v1"]["properties"]["risk_class"]["enum"]
    risk = receipt["risk"]
    if levels.index(risk["risk_class"]) < levels.index(risk["child_max"]):
        raise ValidationError("Composed risk is below a child risk")


_SEMANTIC = {
    "vp.forge_tool/v1": _check_forge,
    "vp.capability/v1": _check_capability,
    "vp.tool_route.index_entry/v1": _check_capability,
    "vp.tool_route.index/v1": _check_index,
    "vp.tool_route.reply/v1": _check_route_reply,
    "vp.focus_trace/v1": _check_trace,
    "vp.image_pack/v1": _check_pack,
    "vp.snapshot_receipt/v1": _check_snapshot,
    "vp.admission/v1": _check_admission,
    "vp.guest/v1": _check_guest,
    "vp.forge_receipt/v1": _check_receipt,
}


def validate_contract(identifier, instance):
    """Validate a local contract ID, including the constraints on encoded bytes and sibling values."""
    store = _store()
    schema = store["urn:" + identifier.replace("/", ":")]
    resolver = _LocalResolver.from_schema(schema, store=store)
    Draft202012Validator(schema, resolver=resolver).validate(instance)
    # Apply cross-field checks to named records in adapter/envelope payloads too.
    def check(value):
        if isinstance(value, list):
            for item in value:
                check(item)
        elif isinstance(value, dict):
            tag = value.get("schema")
            if isinstance(tag, str) and tag in _SEMANTIC:
                _SEMANTIC[tag](value)
            for item in value.values():
                check(item)
    if isinstance(instance, dict) and instance.get("schema") != identifier and identifier in _SEMANTIC:
        _SEMANTIC[identifier](instance)
    if identifier in ("vp.oflane.reply/v1", "vp.control.reply/v1") and instance["op"] == "vp_tool_route":
        result = instance["result"]
        if "verdict" in result:
            _check_route_reply(result)
    check(instance)



def validate_adapter_exchange(request, reply):
    """Check a correlated exchange before using its result. Dispatch admission is separate."""
    validate_contract("vp.control.req/v1", request)
    validate_contract("vp.control.reply/v1", reply)
    tool = request["op"]
    if reply["op"] != tool or reply["request_id"] != request["request_id"]:
        raise ValidationError("Reply does not identify the request")
    op = request["args"].get("op")
    row = _adapter_rows().get(tool, {}).get("ops", {}).get(op)
    if row is None or not row.get("args_schema") or not row.get("reply_schema"):
        raise ValidationError("ARGS_SCHEMA_ABSENT")
    if reply["ok"]:
        if row.get("refusal_only"):
            raise ValidationError("Parked operation has no successful reply")
        validate_contract(row["reply_schema"], reply["result"])


@lru_cache(maxsize=1)
def _adapter_rows():
    return json.loads((SCHEMA_DIR / "adapter_ops.json").read_text(encoding="utf-8"))
