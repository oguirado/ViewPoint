# SPDX-License-Identifier: AGPL-3.0-only
"""Regenerate the REA definitions in a vp.control.reply/v1 schema."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re


REPLY = "urn:vp.control.reply:v1"
OP = "rea.slot.project"
NULL = {"type": "null"}
OUTSIDE = {"const": True}


def ref(name):
    return {"$ref": f"{REPLY}#/$defs/{name}"}


def obj(properties, required=None):
    return {
        "type": "object", "properties": properties, "additionalProperties": False,
        "required": list(properties) if required is None else required,
    }


def array(items, **constraints):
    return {"type": "array", "items": items, **constraints}


def nullable(shape):
    return {"anyOf": [shape, NULL]}


def when(key, shape, then, otherwise=None):
    rule = {"if": {"properties": {key: shape}, "required": [key]}, "then": then}
    if otherwise is not None:
        rule["else"] = otherwise
    return rule


def tagged(line):
    return re.findall(r"`([a-zA-Z_][a-zA-Z_0-9]*)(?::[^`]+)?`", line)


def generate(manifest, projections, examples):
    source = manifest["$defs"]

    # Expand manifest-local references so the result is self-contained.
    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            expanded = {}
            if "$ref" in value:
                name = value["$ref"].removeprefix("#/$defs/")
                expanded.update(expand(source[name]))
            siblings = {key: expand(item) for key, item in value.items() if key != "$ref"}
            if expanded.keys() & (siblings.keys() - {"description"}):
                return {"allOf": [expanded, siblings]}
            expanded.update(siblings)
            return expanded
        return value

    def clause(prefix):
        line, = (line for line in projections.splitlines() if line.startswith(prefix))
        return line

    def fields(prefix, end):
        return tagged(clause(prefix).split(end, 1)[0])

    manifest_props = manifest["properties"]
    text = expand(source["text"])
    digest = expand(source["sha256"])
    effect_set = expand(source["effects"])
    level = expand(source["level"])
    slot_id = expand(manifest_props["slot_id"])
    op_id = expand(source["opId"])
    op_props = source["operation"]["properties"]
    availability = {"enum": re.findall(
        r"^\| `([a-z_]+)` \|", projections.split("## Availability and qualification", 1)[1]
        .split("## Router entries", 1)[0], re.M
    )}
    if not availability["enum"]:
        raise ValueError("Missing availability rules")
    binding_kind = {"enum": [
        branch["properties"]["kind"]["const"] for branch in source["binding"]["oneOf"]
    ] + [None]}
    ref_or_null = nullable(text)

    # Describe and inspect retain the public parts of the manifest.
    describe_keys = fields("The closed describe object contains ", ". Copy")
    describe = obj({
        key: digest if key == "manifest_digest" else expand(manifest_props[key])
        for key in describe_keys
    })
    describe_props = describe["properties"]
    # Authorization can redact every licence evidence coordinate.
    describe_props["upstream"]["properties"]["licence_evidence_refs"].pop("minItems")
    probes = describe_props["capabilities"]["properties"]["prerequisite_probes"]
    probe_fields = fields("The closed describe object contains ", ". Each operation")
    probe_fields = probe_fields[probe_fields.index("id"):probe_fields.index("verified_state") + 1]
    probe_states = tagged(clause("The closed describe object contains ").split(
        "`verified_state` in ", 1
    )[1].split(". Each operation", 1)[0])
    probes["items"] = obj({
        key: {"enum": probe_states} if key == "verified_state" else probes["items"]["properties"][key]
        for key in probe_fields
    })
    operation_clause = clause("The closed describe object contains ").split(
        "Each operation is a closed object containing the manifest's ", 1
    )[1].split(" from its binding type", 1)[0]
    describe_props["operations"]["items"] = obj({
        key: expand(op_props[key]) if key in op_props else {
            "binding_kind": binding_kind, "availability": availability,
            "conformance_receipt_ref": ref_or_null,
        }[key]
        for key in tagged(operation_clause)
    })
    # A filtered describe may legitimately return a single operation.
    describe["allOf"] = []
    complete_describe = ref("slotDescribe") | {"allOf": []}
    for guard in manifest["allOf"]:
        if set(guard["if"]["required"]) <= describe_props.keys():
            rules = {key: value for key, value in guard["then"]["properties"].items()
                     if key in describe_props}
            if "operations" in rules:
                complete_describe["allOf"].append(expand(when(
                    "status", guard["if"]["properties"]["status"],
                    {"properties": {"operations": rules.pop("operations")}},
                )))
            describe["allOf"].append(expand(guard | {"then": {"properties": rules}}))

    inspect_clause = clause("The closed describe object contains ").split(
        "The closed inspect object contains ", 1
    )[1].split(". The private execution", 1)[0]
    inspect_fields = {
        "slot": ref("slotDescribeComplete"), "installation_refs": array(text, uniqueItems=True),
        "selected_installation_ref": ref_or_null, "conformance_receipt_ref": ref_or_null,
        "unresolved_conformance_refs": array(text, uniqueItems=True),
    }

    # Router rows have distinct external, independent-host and case shapes.
    slot_part = slot_id["pattern"].removeprefix("^").removesuffix("$")
    op_part = op_id["pattern"].removeprefix("^").removesuffix("$")
    external = r"ext\." + slot_part
    external_op = external + r"\.(?!conform$)" + op_part
    external_case = external + r"\.conform\." + op_part
    host = r"host\.[a-z_]+\.[a-z_]+"

    def route(pattern):
        return {"type": "string", "pattern": f"^(?:{pattern})$"}

    route_id = route(f"{external}(?:\\.{op_part}(?:\\.{op_part})?)?|{host}")
    contract_clause = clause("| `json_contract_ref` |")
    upstream = source["upstream"]["properties"]
    caps = source["capabilities"]["properties"]
    facet_shapes = {
        "target_kinds": expand(caps["target_kinds"]), "platforms": expand(caps["platforms"]),
        "transport_kind": expand(source["transport"]["properties"]["kind"]),
        "adapter_identity": expand(caps["adapter_identity"]),
        "determinism_modes": expand(source["recordReplay"]["properties"]["determinism_modes"]),
        "artifact_roles": expand(source["evidence"]["properties"]["artifact_roles"]),
        "comparator_id": expand(source["comparison"]["properties"]["comparator_id"]),
        "upstream": obj({key: expand(upstream[key]) for key in ("name", "version", "licence_id")}),
        "binding_kind": binding_kind, "case_id": op_id, "case_ref": text,
    }
    facet_clause = clause("| `facets` |").split("On `ext.*`, include ", 1)[1]
    external_facet_keys = [
        key for key in tagged(facet_clause.split("Case rows", 1)[0]) if key in facet_shapes
    ]
    case_facets = {key: facet_shapes[key] for key in ("case_id", "case_ref")}
    carrier_shapes = {
        "control_request_schema_ref": {"const": "urn:vp.control.req:v1"},
        "control_reply_schema_ref": {"const": REPLY}, "op": {"const": OP}, "route_id": route_id,
    }
    common_ref_keys = [key for key in tagged(contract_clause.split("On external", 1)[0])
                       if key in carrier_shapes]
    common_ref = {key: carrier_shapes[key] for key in common_ref_keys}
    contract = expand(source["contractRef"])
    external_ref = obj(common_ref | contract["properties"] | {
        "request_selector": expand(op_props["input_ref"]),
        "result_selector": expand(op_props["output_ref"]),
    })
    external_ref["allOf"] = contract["allOf"]
    host_ref = obj(common_ref | {"argument_schema_ref": text, "result_schema_ref": text}, common_ref_keys)
    host_ref["dependentRequired"] = {
        "argument_schema_ref": ["result_schema_ref"], "result_schema_ref": ["argument_schema_ref"],
    }
    host_ref = {"anyOf": [obj(common_ref), host_ref]}

    provider = expand(source["providerRef"])
    perception = source["perception"]
    row_levels = obj({key: expand(perception["properties"][key])
                      for key in ("required_level", "optional_levels")})
    row_levels["allOf"] = expand(perception["allOf"])
    external_facets = {key: facet_shapes[key] for key in external_facet_keys}
    row = obj({
        "id": route_id, "owner": {"const": "viewpoint-rea"}, "manifest_digest": ref_or_null,
        "provider_ref": nullable(provider), "role": nullable(expand(source["role"])),
        "operation": text, "summary": text, "search_terms": expand(source["textSet"]),
        "input_schema_ref": ref_or_null, "output_schema_ref": {"const": REPLY},
        "mcp_name": text, "cli_argv_shape": array(text, minItems=1),
        "json_contract_ref": {}, "required_effects": effect_set, "perception": nullable(row_levels),
        "availability": availability, "conformance_receipt_ref": ref_or_null,
        "outside_capsule": OUTSIDE, "facets": {},
    })
    row["oneOf"] = [
        {"properties": {
            "id": route(external_op), "operation": op_id, "manifest_digest": digest,
            "provider_ref": provider, "role": expand(source["role"]),
            "json_contract_ref": external_ref, "perception": row_levels, "facets": obj(external_facets),
        }},
        {"properties": {
            "id": route(external + r"(?:\.conform)?"), "operation": {"enum": ["describe", "conform"]},
            "manifest_digest": digest, "provider_ref": provider, "role": NULL,
            "json_contract_ref": obj(common_ref), "facets": obj(external_facets),
        }},
        {"properties": {
            "id": route(external_case), "operation": route(r"conform\." + op_part),
            "manifest_digest": digest, "provider_ref": provider, "role": NULL,
            "json_contract_ref": obj(common_ref), "facets": obj(external_facets | case_facets),
        }},
        {"properties": {
            "id": route(host), "manifest_digest": NULL, "provider_ref": NULL, "role": NULL,
            "json_contract_ref": host_ref, "facets": obj({}),
        }},
    ]
    manifest_props_by_profile = next(
        guard["then"]["properties"]["provider_ref"] for guard in manifest["allOf"]
        if "contract_refs" in guard["if"]["required"]
    )
    row["allOf"] = [
        when("json_contract_ref", {"required": ["execution_profile"], "properties": {
            "execution_profile": {"const": "external_process"},
        }}, {"properties": {"provider_ref": expand(manifest_props_by_profile)}}),
        when("perception", NULL, {"properties": {"availability": {"const": "parked"}}}),
        when("id", {"const": "host.slot.list"}, {"properties": {
            "perception": {"const": {"required_level": "none", "optional_levels": []}},
            "required_effects": {"maxItems": 0}, "json_contract_ref": obj(common_ref),
        }}),
    ]

    # PROJECTIONS enumerates the v1 keys, and pinned declarations bind role to operation.
    params_clause = clause("Resolve `params` after verifying ").split("Preserve ", 1)[1]
    common, replay, sample = re.fullmatch(r"(.+) wherever declared, (.+) for `replay`, and (.+) for `sample`\.", params_clause).groups()
    role_keys = {"replay": tagged(common) + tagged(replay), "sample": tagged(common) + tagged(sample)}
    params = {role: obj(dict.fromkeys(keys, {}), []) for role, keys in role_keys.items()}
    params["other"] = obj({}, [])
    route_roles = {}
    routes_by_role = {}
    for example in examples:
        for operation in example["operations"]:
            role = operation["role"] if operation["role"] in params else "other"
            route_name = f"ext.{example['slot_id']}.{operation['id']}"
            if route_name in route_roles:
                raise ValueError(f"Duplicate example operation {route_name}")
            route_roles[route_name] = {"const": operation["role"]}
            routes_by_role.setdefault(role, []).append(route_name)
    params_by_route = [
        when("route_id", {"enum": sorted(routes)}, {"properties": {
            "params": ref("slotParams" + role.title()),
        }}) for role, routes in sorted(routes_by_role.items())
    ]
    normalization = clause("Normalize an external call to a closed object ")
    external_keys = tagged(normalization.split(". Use the public", 1)[0])
    other_keys = tagged(normalization.split("Other routes normalize to a closed object with required ", 1)[1]
                        .split(". Use the normalized", 1)[0])
    request_id = {"type": "string", "pattern": re.search(
        r"matching `([^`]+)`", clause("All public wrappers require ")
    )[1]}
    inputs = array(obj({"role": text, "ref": text, "sha256": digest, "path": text,
                       "media_type": text, "record_schema": text}, ["role", "ref"]))
    normalized = {
        "route_id": route(external_op), "manifest_digest": digest,
        "slot_id": slot_id, "op_id": op_id, "installation_ref": text, "request_id": request_id,
        "inputs": inputs, "params": {"anyOf": [ref("slotParams" + role.title()) for role in params]},
        "perception_level": level, "determinism_mode": text,
        "dry_run": {"const": True}, "policy_sha256": digest,
    }
    dry_ext = obj({key: normalized[key] for key in external_keys}) | {"allOf": params_by_route}
    dry_other = obj({key: (normalized | {
        "route_id": route(f"{external}(?:\\.conform(?:\\.{op_part})?)?|{host}"),
        "manifest_digest": nullable(digest),
        "arguments": obj({"slot_id": slot_id, "op_id": op_id, "case_id": op_id,
                          "installation_ref": text, "all": {"type": "boolean"}, "payload": {}}, []),
    })[key] for key in other_keys})
    dry_other["allOf"] = [
        when("route_id", route(host), {"properties": {"manifest_digest": NULL}},
             {"properties": {"manifest_digest": digest}}),
    ]
    dry_result = obj({
        "kind": {"const": "dry_run"}, "dry_run": {"const": True},
        "normalized_request": {"oneOf": [dry_ext, dry_other]}, "outside_capsule": OUTSIDE,
    })

    result_rules = clause("Preserve `OK/0`")
    pairs = [(status, int(rc)) for status, rc in re.findall(r"`([A-Z_]+)/(\d+)`", result_rules)]
    if not pairs:
        raise ValueError("Missing exchange status rules")
    verdict = obj({
        "result": {"enum": tagged(result_rules.split("object requires `result` in ", 1)[1]
                                  .split(", nonempty strings", 1)[0])},
        "comparator_id": text, "comparator_version": text,
        "verdict_ref": ref_or_null, "abstain_reason": text,
    }, ["result", "comparator_id", "comparator_version", "verdict_ref"])
    verdict["allOf"] = [when("result", {"const": "abstain"}, {"required": ["abstain_reason"]},
                             {"not": {"required": ["abstain_reason"]}})]
    exchange = obj({
        "kind": {"const": "slot_exchange"}, "status": {"enum": [status for status, _ in pairs]},
        "rc": {"type": "integer"}, "slot_result_ref": ref_or_null, "evidence_ref": ref_or_null,
        "receipt_ref": ref_or_null, "availability": {"const": "admitted"},
        "produced_by": text, "perception_used": level, "outside_capsule": OUTSIDE,
    })
    exchange["allOf"] = [when("status", {"const": status}, {"properties": {"rc": {"const": rc}}})
                         for status, rc in pairs]
    compare = deepcopy(exchange)
    compare["properties"]["verdict"] = ref("verdict")
    compare["required"].append("verdict")
    host_result = obj({"kind": {"const": "host_result"}, "payload": {"not": NULL},
                       "outside_capsule": OUTSIDE})
    inspect = deepcopy(host_result)
    inspect["properties"]["payload"] = obj({key: inspect_fields[key] for key in tagged(inspect_clause)})
    case_item = obj({
        "case_id": op_id, "outcome": {"enum": ["pass", "fail", "abstain"]},
        "case_receipt_ref": ref_or_null, "reason": ref_or_null,
    })
    case_item["allOf"] = [when("outcome", {"const": "pass"}, {"properties": {"reason": NULL}},
                               {"properties": {"reason": text}})]

    return {
        "slotOperationRoutes": obj(route_roles, []),
        "slotDescribe": describe,
        "slotDescribeComplete": complete_describe,
        "slotDescribeResult": obj({"kind": {"const": "slot_describe"}, "slot": ref("slotDescribe")}),
        "slotListResult": obj({"kind": {"const": "slot_list"}, "rows": array(row)}),
        "dryRunResult": dry_result,
        "hostResult": host_result,
        "hostInspectResult": inspect,
        "slotExchangeResult": exchange,
        "slotExchangeCompareResult": compare,
        "conformanceResult": obj({
            "kind": {"const": "conformance"}, "cases": array(case_item),
            "conformance_receipt_ref": ref_or_null, "outside_capsule": OUTSIDE,
        }),
        "verdict": verdict,
        **{"slotParams" + role.title(): shape for role, shape in params.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--projections", required=True)
    parser.add_argument("--examples", required=True)
    parser.add_argument("--reply", required=True)
    parser.add_argument("--check", action="store_true", help="compare generated bytes without writing")
    args = parser.parse_args()

    def relative(value):
        path = Path(value)
        if path.is_absolute() or not path.resolve().is_relative_to(Path.cwd().resolve()):
            parser.error(f"expected a repository-relative path: {value}")
        return path

    def read_json(path):
        return json.loads(path.read_text(encoding="utf-8"))

    reply_path = relative(args.reply)
    reply = read_json(reply_path)
    examples = [read_json(path) for path in sorted(relative(args.examples).glob("*.slot.json"))]
    if not examples:
        parser.error("the examples directory has no slot declarations")
    reply["$defs"].update(generate(
        read_json(relative(args.manifest)), relative(args.projections).read_text(encoding="utf-8"), examples,
    ))
    slot_success, = (rule for rule in reply["allOf"] if rule["if"].get("properties") == {
        "ok": {"const": True}, "op": {"const": OP},
    })
    slot_success["then"]["properties"]["result"]["anyOf"] = [
        ref(name) for name in (
            "slotExchangeResult", "dryRunResult", "hostResult", "slotDescribeResult",
            "slotListResult", "conformanceResult", "slotExchangeCompareResult",
        )
    ]
    data = (json.dumps(reply, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if args.check:
        if data != reply_path.read_bytes():
            parser.exit(1, "generated reply schema differs from the committed schema\n")
    else:
        reply_path.write_bytes(data)


if __name__ == "__main__":
    main()
