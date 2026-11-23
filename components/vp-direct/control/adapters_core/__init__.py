"""Core adapter handlers for the C1 dispatcher."""

from collections import defaultdict
from copy import deepcopy
from functools import partial
from hashlib import sha256
from typing import Protocol

from jsonschema import ValidationError

from ..core import control


_TOOLS = (
    "vp_capsule_call",
    "vp_capture_call",
    "vp_saved_data_call",
    "vp_guest_command_call",
    "vp_headless_call",
)
_CORE_COMPONENT = "agent"
_HEADLESS_TERMINAL = {
    "create": ("state_changed", "ready", "held"),
    "run": ("completed", "completed", "held"),
    "stop": ("state_changed", "stopped", "released"),
}
_UI_ONLY = {
    ("vp_capsule_call", "role_switch"),
    ("vp_capture_call", "screenshot"),
    ("vp_capture_call", "ram_export"),
    ("vp_capture_call", "ram_list"),
}


class CoreTransport(Protocol):
    """Provisioned Core peer and its normalized control exchange.

    The transport authenticates the peer and reports its bound component.
    Both methods return terminal vp.control.reply/v1 readback. Preview must
    use a non-mutating path.
    """

    @property
    def component(self) -> str: ...

    def execute(self, route: str, request: dict) -> dict: ...

    def preview(self, route: str, request: dict) -> dict: ...


class NativeCoreTransport:
    """Use an entered Agent ToolClient and its authenticated Core mailbox."""

    def __init__(self, client, *, timeout_ms):
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 60_000:
            raise ValueError("invalid Core request timeout")
        self._client = client
        self._timeout_ms = timeout_ms

    @property
    def component(self):
        return self._client.local["endpoint"]["component"]

    def preview(self, route, request):
        return self._reply(request, control.Outcome(False, "HOLD"))

    def execute(self, route, request):
        tool, args = request["op"], request["args"]
        if (self.component != _CORE_COMPONENT or tool == "vp_saved_data_call"
                or (tool, args["op"]) in _UI_ONLY):
            outcome = control.Outcome(False, "policy_denied")
        elif tool == "vp_headless_call" and args["op"] in _HEADLESS_TERMINAL:
            outcome = self._headless(request)
        elif tool == "vp_guest_command_call" and args["op"] == "create":
            outcome = self._guest_create(args)
        else:
            outcome = control.Outcome(False, "HOLD")
        return self._reply(request, outcome)

    def _owner_id(self):
        instance = self._client.local["endpoint"]["instance"]
        return "actor-" + sha256(f"agent:{instance}".encode()).hexdigest()

    @staticmethod
    def _reply(request, outcome):
        return control._reply(request, control.digest(request), outcome)

    def _headless(self, request):
        params = dict(request["args"])
        op = params.pop("op")
        native_id = "request-" + sha256(request["request_id"].encode()).hexdigest()
        body = {"schema": "headless_request/v0.2", "request_id": native_id, "op": op, "params": params}
        expected_schema = "headless_event/v0.2"
        event = self._client.request(body, expected_schema, timeout_ms=self._timeout_ms)
        if event["schema"] != expected_schema or event["request_id"] != native_id:
            raise ValidationError("Core event does not identify the request")
        data = event["data"]
        if event["event"] == "refused" and data["effect"] == "not_dispatched":
            code = data["code"]
            return control.Outcome(False, {"OWNER_MISMATCH": "OWNER", "POLICY": "policy_denied"}.get(code, code))
        if event["event"] == "outcome_unknown":
            return control.Outcome(False, "UNCERTAIN_OUTCOME")
        session = data["session"]
        if ((event["event"], session["phase"], session["ownership"]) != _HEADLESS_TERMINAL[op]
                or session["owner_id"] != self._owner_id()):
            raise ValidationError("Core headless event is not a terminal event for this owner")
        identity = {key: session[key] for key in ("workload_id", "generation")}
        if op != "create" and any(params[key] != value for key, value in identity.items()):
            raise ValidationError("Core headless event names a different workload")
        if op == "run":
            job = session["job"]
            n = params["n"]
            expected_value = n * (n + 1) * (2 * n + 1) // 6
            if (job["job_id"], job["n"], job["value"]) != (params["job_id"], n, expected_value):
                raise ValidationError("Core headless job result is unverified")
        return control.Outcome(True, "OK", identity)

    def _guest_create(self, args):
        # The guest session helper reserves and correlates the native create
        # and validates the event sequence before returning the ready session.
        session = self._client.guest_commands.create(timeout_ms=self._timeout_ms)
        if (session["phase"] != "ready" or session["ownership"] != "held"
                or session["owner_id"] != self._owner_id() or session["profile_id"] != args["profile_id"]):
            raise ValidationError("Core guest session is not ready for this owner and profile")
        return control.Outcome(True, "OK", {key: session[key] for key in ("workload_id", "generation")})


def build_bindings(registry: control.CapabilityRegistry, transport: CoreTransport):
    """Build the binding map for C1 from its frozen capability projection.

    C1 claims the request durably before invoking a handler. The transport gets
    one attempt per claim, including when the reply is missing or unverifiable.
    """
    operations_by_route = defaultdict(set)
    for tool in _TOOLS:
        for row in registry.get(tool)["op_table"]:
            binding = row["binding"]
            if row["supported"] and binding["kind"] == "core_route":
                operations_by_route[binding["name"]].add((tool, row["op"]))

    bindings = {}
    for route, operations in operations_by_route.items():
        run = partial(_dispatch, transport, route, frozenset(operations))
        bindings["core_route", route] = control.Handler(
            execute=partial(run, False), preview=partial(run, True),
        )
    return bindings


def _dispatch(transport, route, operations, dry_run, request):
    selector = request["args"]["op"]
    if (request["op"], selector) not in operations or request["dry_run"] is not dry_run:
        return control.Outcome(False, "policy_denied")
    if getattr(transport, "component", None) != _CORE_COMPONENT:
        return control.Outcome(False, "policy_denied")

    send = transport.preview if dry_run else transport.execute
    try:
        return _readback(request, send(route, deepcopy(request)))
    except (OSError, ValidationError):
        return control.Outcome(False, "UNCERTAIN_OUTCOME")


def _readback(request, reply):
    control._validation.validate_adapter_exchange(request, reply)
    if reply["effective_request_sha256"] != control.digest(request):
        raise ValidationError("Core readback has a different effective request digest")
    return control.Outcome(
        ok=reply["ok"],
        code=reply["code"],
        result=reply["result"] if reply["ok"] else {},
        evidence=tuple(reply["evidence"]),
        not_verified=tuple(reply["not_verified"]),
    )
