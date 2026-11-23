# SPDX-License-Identifier: AGPL-3.0-only
"""Validate normalized rea.slot.project requests and their correlated replies.

Accepted requests use the existing vp.control.req/v1 envelope. Pre-acceptance
failures raise SlotRequestError with the control lane's InvalidRequest codes.
"""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

from jsonschema import ValidationError


_schemas = Path(__file__).resolve().parents[1] / "schemas"
_spec = importlib.util.spec_from_file_location(
    "vp_slot_schema_validation", _schemas / "validation.py"
)
_validation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_validation)

REQUEST = "vp.control.req/v1"
REPLY = "vp.control.reply/v1"
CONTROL_OP = "rea.slot.project"

canonical_bytes = _validation.canonical_bytes
validate_contract = _validation.validate_contract


def _resource(identifier):
    return _validation._store().get("urn:" + identifier.replace("/", ":"))


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class SlotRequestError(ValueError):
    """A lane error for input that cannot form a frozen control reply."""

    def __init__(self, code):
        super().__init__(str(code))
        self.code = code


def _validate_projection(name, instance):
    _validation.validate_definition(REPLY, name, instance)


def _validate_params(route_id, params, operation_role):
    definitions = _resource(REPLY)["$defs"]
    pinned = definitions["slotOperationRoutes"]["properties"].get(route_id)
    if pinned is not None:
        if operation_role is not None and operation_role != pinned["const"]:
            raise ValidationError("Bound role disagrees with the pinned slot operation")
        operation_role = pinned["const"]
    if operation_role not in ("replay", "sample"):
        operation_role = "other"
    _validate_projection("slotParams" + operation_role.title(), params)


def validate_request(envelope, *, operation_role=None):
    """Validate an envelope against ``vp.control.req/v1``.

    Raises ``jsonschema.ValidationError`` on failure. No dispatch.
    """
    validate_contract(REQUEST, envelope)
    if envelope["op"] == CONTROL_OP:
        args = envelope["args"]
        _validate_params(args["route_id"], args["arguments"].get("params", {}), operation_role)


def validate_reply(reply):
    """Validate a reply against ``vp.control.reply/v1``."""
    validate_contract(REPLY, reply)


def build_request(route_id, arguments, request_id, dry_run=False, context=None, *, operation_role=None):
    """Build and validate a normalised slot request envelope.

    ``arguments`` holds the public properties other than ``request_id``
    and ``dry_run`` (PROJECTIONS.md ``args`` mapping). ``context`` is the
    authenticated control context; when omitted it defaults to the test
    caller/agent pair. Returns a fresh canonical envelope dict.
    """
    if not isinstance(route_id, str) or not route_id:
        raise SlotRequestError("SCHEMA_INVALID")
    if not isinstance(arguments, dict):
        raise SlotRequestError("SCHEMA_INVALID")
    if not isinstance(request_id, str) or not request_id:
        raise SlotRequestError("SCHEMA_INVALID")
    if not isinstance(dry_run, bool):
        raise SlotRequestError("SCHEMA_INVALID")
    if context is None:
        context = {"caller": "agent", "agent_instance": "slot-local"}
    if not isinstance(context, dict):
        raise SlotRequestError("SCHEMA_INVALID")
    if {"request_id", "dry_run"} & arguments.keys():
        raise SlotRequestError("SCHEMA_INVALID")
    envelope = {
        "schema": REQUEST,
        "request_id": request_id,
        "op": CONTROL_OP,
        "context": deepcopy(context),
        "args": {"route_id": route_id, "arguments": deepcopy(arguments)},
        "dry_run": dry_run,
    }
    validate_request(envelope, operation_role=operation_role)
    try:
        return json.loads(canonical_bytes(envelope))
    except (ValueError, TypeError) as exc:
        raise SlotRequestError(-32602) from exc


_REFUSAL_CODES = frozenset({
    "HOLD", "HUMAN_NEEDED", "NO_LEASE", "REQUEST_ID_CONFLICT",
    "UNCERTAIN_OUTCOME", "SCHEMA_INVALID", "PERCEPTION_DENIED",
    "POOL_TIMEOUT", "PROFILE_NOT_ADMITTED", "AVAILABILITY_NOT_INSTALLED",
    "AVAILABILITY_BLOCKED", "AVAILABILITY_PARKED",
})


def _accept(envelope, *, lane_identity=None, operation_role=None):
    if not isinstance(envelope, dict):
        raise SlotRequestError(-32602)
    if envelope.get("op") != CONTROL_OP:
        raise SlotRequestError(-32601)
    if not isinstance(envelope.get("request_id"), str) or not envelope["request_id"]:
        raise SlotRequestError(-32602)
    try:
        validate_request(envelope, operation_role=operation_role)
        if lane_identity is not None:
            envelope = deepcopy(envelope)
            envelope["context"].update(
                caller=lane_identity.caller, agent_instance=lane_identity.agent_instance,
            )
            validate_contract(REQUEST, envelope)
        encoded = canonical_bytes(envelope)
    except (ValidationError, ValueError, TypeError) as exc:
        raise SlotRequestError(-32602) from exc
    return json.loads(encoded), hashlib.sha256(encoded).hexdigest()


def refusal_reply(request, code="SCHEMA_INVALID", *, operation_role=None):
    """Build a contract refusal for an accepted, effective request."""
    if not isinstance(code, str) or code not in _REFUSAL_CODES:
        raise SlotRequestError("SCHEMA_INVALID")
    request, request_digest = _accept(request, operation_role=operation_role)
    reply = {
        "schema": REPLY,
        "request_id": request["request_id"],
        "op": request["op"],
        "ok": False,
        "code": code,
        "result": {},
        "effective_request_sha256": request_digest,
        "evidence": [],
        "not_verified": [],
    }
    validate_reply(reply)
    return json.loads(canonical_bytes(reply))


def _validate_result(request, result, operation_role):
    route_id = request["args"]["route_id"]
    route = route_id.split(".")
    if request["dry_run"]:
        _validate_projection("dryRunResult", result)
        preview = result["normalized_request"]
        if (preview["route_id"] != route_id or preview["request_id"] != request["request_id"]):
            raise ValidationError("Preview does not identify the request")
        if "params" in preview:
            _validate_params(route_id, preview["params"], operation_role)
    elif route_id == "host.slot.inspect":
        _validate_projection("hostInspectResult", result)
        if result["payload"]["slot"]["slot_id"] != request["args"]["arguments"].get("slot_id"):
            raise ValidationError("Inspect result identifies a different slot")
    elif route_id == "host.slot.list":
        _validate_projection("slotListResult", result)
    elif route[0] == "host":
        _validate_projection("hostResult", result)
    elif route[0] == "ext" and len(route) == 2:
        _validate_projection("slotDescribeResult", result)
        slot = result["slot"]
        if slot["slot_id"] != route[1]:
            raise ValidationError("Describe result identifies a different slot")
        op_id = request["args"]["arguments"].get("op_id")
        if op_id is None:
            _validate_projection("slotDescribeComplete", slot)
        elif [operation["id"] for operation in slot["operations"]] != [op_id]:
            raise ValidationError("Describe result identifies a different operation")
    elif route[0] == "ext" and len(route) >= 3 and route[2] == "conform":
        _validate_projection("conformanceResult", result)
    elif route[0] == "ext" and len(route) == 3:
        shape = "slotExchangeCompareResult" if route[2] == "compare" else "slotExchangeResult"
        _validate_projection(shape, result)
    else:
        raise ValidationError("Unknown slot result route")


def handle_envelope(envelope, dispatch, *, lane_identity=None, operation_role=None):
    """Dispatch a validated, canonical envelope and check its correlated reply.

    An authenticated carrier can pass its provisioned LaneIdentity to stamp
    the context before dispatch. Otherwise the incoming context is effective.
    A projector can pass the verified v1 role for a route outside the pinned
    examples. Both inputs come from the host, independently of the reply.
    """
    canonical, expected_digest = _accept(
        envelope, lane_identity=lane_identity, operation_role=operation_role,
    )
    if not callable(dispatch):
        raise SlotRequestError("SCHEMA_INVALID")
    reply = dispatch(deepcopy(canonical))
    validate_reply(reply)
    if (reply["request_id"] != canonical["request_id"] or reply["op"] != canonical["op"]
            or reply["effective_request_sha256"] != expected_digest):
        raise ValidationError("Reply does not identify the effective request")
    if reply["ok"]:
        _validate_result(canonical, reply["result"], operation_role)
    return json.loads(canonical_bytes(reply))


handle_request = handle_envelope
make_request = build_request
make_refusal = refusal_reply

__all__ = [
    "CONTROL_OP",
    "REQUEST",
    "REPLY",
    "SlotRequestError",
    "build_request",
    "canonical_bytes",
    "digest",
    "handle_envelope",
    "handle_request",
    "make_refusal",
    "make_request",
    "refusal_reply",
    "validate_reply",
    "validate_request",
    "validate_contract",
]
