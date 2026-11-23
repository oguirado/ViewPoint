"""Project the C1 registry and frozen request contracts onto MCP tool declarations."""

from control.core import control


_request_properties = control._request_schema["properties"]
_context = _request_properties["context"]["properties"]
_metadata = {
    "request_id": _request_properties["request_id"],
    "dry_run": _request_properties["dry_run"],
}
_context_fields = {name: schema for name, schema in _context.items()
                   if name not in {"caller", "agent_instance"}}


def _portable(value):
    """Inline local references so a client can validate without repository access."""
    if isinstance(value, list):
        return [_portable(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _portable(item) for key, item in value.items()
              if key not in {"$id", "$schema", "$defs", "$ref"}}
    if "$ref" in value:
        target = _portable(control._validation._store()[value["$ref"]])
        return {"allOf": [target, result]} if result else target
    return result


def _flat(args_schema):
    args = _portable(args_schema)
    args["properties"] = {**_portable(_context_fields), **args["properties"], **_portable(_metadata)}
    args["required"] = [*args["required"], *_metadata]
    return args


def _input_schema(tool):
    args = control._args_by_tool[tool]
    if "$ref" not in args:
        return _flat(args)

    union = control._validation._store()[args["$ref"]]
    shape = _flat({key: value for key, value in union.items() if key != "allOf"})
    shape["allOf"] = [
        {"if": _portable(branch["if"]),
         "then": _flat(control._validation._store()[branch["then"]["$ref"]])}
        for branch in union["allOf"]
    ]
    return shape


def registration_manifest(registry):
    """Generate ordered MCP declarations and bind them to the complete C1 snapshot."""
    entries = registry.entries()
    tools = []
    for entry in entries:
        tool = entry["tool"]
        description = "ViewPoint " + tool + "."
        if entry["operator_only"]:
            description += " Operator authorization required."
        if not entry["supported"]:
            description += " Registered for discovery. Execution awaits a qualified binding."
        tools.append({"name": tool, "description": description, "inputSchema": _input_schema(tool)})
    return {"registry_sha256": control.digest(entries), "tools": tools}
