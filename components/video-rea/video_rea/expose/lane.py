# SPDX-License-Identifier: AGPL-3.0-only
"""Local JSON lane. register(op, fn) takes a trusted in-memory preview callback."""

from copy import deepcopy
from functools import lru_cache

from .catalog import Catalog, ROOT
from .preview import run_preview
from .wire import digest


class Exposure:
    """One coherent contract snapshot shared by discovery and local dispatch."""

    def __init__(self, contracts=ROOT / "contracts"):
        self.catalog = Catalog(contracts)
        self._handlers = {}

    def register(self, op, fn):
        if op not in self.catalog.rows or not callable(fn):
            raise ValueError("register a known selector and callable preview")
        self._handlers[op] = fn

    def request(self, args, request_id, dry_run, context=None):
        return {"schema": "vp.control.req/v1", "request_id": request_id, "op": self.catalog.tool,
                "context": {**(context or {}), "caller": "agent", "agent_instance": "video-rea-local"},
                "args": args, "dry_run": dry_run}

    def handle(self, request):
        fields = request if isinstance(request, dict) else {}
        reply = {"schema": "vp.control.reply/v1", "op": fields.get("op"),
                 "request_id": fields.get("request_id"), "ok": False, "code": "-32602",
                 "result": {}, "effective_request_sha256": None, "evidence": [], "not_verified": []}
        try:
            reply["effective_request_sha256"] = digest(request)
        except (TypeError, ValueError, RecursionError):
            return reply

        catalog = self.catalog
        if isinstance(fields.get("op"), str) and fields["op"] != catalog.tool:
            return {**reply, "code": "-32601"}
        if not catalog.request_validator.is_valid(request):
            return reply
        args = request["args"]
        op = args.get("op")
        if not isinstance(op, str):
            return reply
        if op not in catalog.rows:
            return {**reply, "code": "-32601"}

        row = catalog.rows[op]
        schema = catalog.resources[row["args_schema"]]
        if not catalog.validator.evolve(schema=schema).is_valid(args) or not catalog.validator.is_valid(args):
            return reply
        if any(args[k] != request["context"][k] for k in ("session_id", "capsule_id")
               if k in args and k in request["context"]):
            return reply

        parked = {**reply, "code": "HOLD", "result": {"plan": catalog.plan(op)}}
        if row["operator_only"] is not False or row["risk_class"] not in {"leer", "observar", "preparar"}:
            return {**parked, "code": "HUMAN_NEEDED"}
        fn = self._handlers.get(op)
        if not request["dry_run"] or fn is None:
            return parked

        try:
            result = run_preview(fn, deepcopy(args))
            expected = catalog.resources[row["reply_schema"]]
            if not catalog.validator.evolve(schema=expected).is_valid(result):
                return {**parked, "not_verified": ["Local preview returned an invalid result."]}
        except Exception:
            return {**parked, "not_verified": ["Local preview could not complete."]}
        return {**reply, "ok": True, "code": "OK", "result": result}


@lru_cache(maxsize=1)
def local():
    return Exposure()


def handle(request_envelope):
    return local().handle(request_envelope)


def register(op, fn):
    local().register(op, fn)

