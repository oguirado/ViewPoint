# SPDX-License-Identifier: AGPL-3.0-only
"""Factory-local ViewPoint dry runs and the frozen vp_factory_call wire adapter."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import sys
from threading import Lock

from asset_factory.core import ContractError, canonical_json, sha256_json
from tools.registry import Registry, RegistryError


_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_SHA256 = r"[0-9a-f]{64}"
_LOCATOR = r"af://[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*@sha256:" + _SHA256
_ARG_PATTERNS = {"goal_id": _ID, "locator": _LOCATOR}
_REQUEST_KEYS = {"request_id", "op", "args", "risk_class", "operator_only"}
_MAX_REQUEST_BYTES = 65536
_MAX_REPLAY_ENTRIES = 1024
_WIRE_TOOL = "vp_factory_call"
_WIRE_SCHEMA = "vp.control.req/v1"
_WIRE_REQUEST_KEYS = {"schema", "request_id", "op", "context", "args", "dry_run"}
_WIRE_CONTEXT_IDS = {"agent_instance", "session_id", "capsule_id", "incarnation", "observation_id"}
_WIRE_ARG_KEYS = {"op", "goal_id", "inputs"}
_WIRE_REF_KEYS = {"schema", "locator", "sha256", "bytes", "media_type", "stage", "copy"}
_WIRE_LOCATOR = r"(?:af://|file:///)[A-Za-z0-9_][A-Za-z0-9._/@:+-]*"
_REPLAY_NOTE = "The local CLI keeps no replay ledger. The C4 mailbox ledger is authoritative."


@dataclass(frozen=True)
class _Operation:
    inputs: tuple[str, ...]
    checkpoint: str
    pending_unit: str | None
    phase: str = "after"
    risk_class: str = "preparar"
    operator_only: bool = False


_OPERATIONS = {
    "af.select_frames": _Operation(("video_sha256",), "selected_frames", "INPUT"),
    "af.matte": _Operation(("frames_sha256",), "admitted_mattes", "Q-B"),
    "af.generate": _Operation(("matte_sha256", "permit_sha256"), "raw_shape_and_permit", "Q-T"),
    "af.repair": _Operation(("raw_mesh_sha256",), "repaired_mesh", "GEO"),
    "af.lod": _Operation(("mesh_sha256",), "lod_chain", "GEO"),
    "af.project_views": _Operation(("registered_views_sha256", "mesh_sha256"),
                                   "projected_views", "LOOK-BAKE"),
    "af.uv_bake": _Operation(("mesh_sha256", "projection_sha256"), "uv_and_baked_maps", "LOOK-BAKE"),
    "af.face_fit": _Operation(("mesh_sha256", "face_views_sha256"), "face_controls", "FACE"),
    "af.assemble": _Operation(("recipe_sha256",), "assembled_scene", "PARTS"),
    "af.rig": _Operation(("mesh_sha256",), "rigged_scene", "SKIN"),
    "af.motion_from_video": _Operation(("video_sha256", "rig_sha256"), "body_action", "ACT"),
    "af.lipsync": _Operation(("audio_sha256", "face_rig_sha256"), "lip_action", "VOICE"),
    "af.render": _Operation(("scene_sha256", "settings_sha256"), "render_settings", "INT", phase="before"),
    "af.mux": _Operation(("frames_sha256", "audio_sha256"), "muxed_film", "INT"),
    "af.registry.resolve": _Operation(("locator",), "registry_snapshot", "REG",
                                      phase="reference", risk_class="leer"),
    "af.registry.put": _Operation(("record_sha256",), "registered_snapshot", "REG"),
    "af.publish": _Operation(("export_plan_sha256",), "accepted_local_release", "PUB",
                             phase="before", risk_class="enviar", operator_only=True),
    "vqa3d.score": _Operation(("trace_sha256", "profile_sha256"), "appearance_score", None,
                              phase="reference", risk_class="leer"),
    "vqa3d.gate": _Operation(("trace_sha256", "profile_sha256"), "acceptance_state", None,
                             phase="reference", risk_class="leer"),
}


def _checkpoint(operation: _Operation) -> dict:
    return {"backend": "blender_rw1", "name": operation.checkpoint, "phase": operation.phase}


def _arg_pattern(name: str) -> str:
    return _ARG_PATTERNS.get(name, _SHA256)


def _arg_names(operation: _Operation) -> tuple[str, ...]:
    return ("goal_id", *operation.inputs)


def local_capabilities() -> list[dict]:
    """Return the local command inventory for owner schema reconciliation."""
    return [
        {
            "name": name,
            "args_schema": {
                "type": "object",
                "properties": {
                    # The negative lookahead requires the true end of input in JSON Schema.
                    key: {"type": "string", "pattern": r"^" + _arg_pattern(key) + r"(?![\s\S])"}
                    for key in _arg_names(operation)
                },
                "required": list(_arg_names(operation)),
                "additionalProperties": False,
            },
            "risk_class": operation.risk_class,
            "operator_only": operation.operator_only,
            "handler": "viewpoint.handlers:LocalDryRun.handle",
            "checkpoint": _checkpoint(operation),
            "pending_unit": operation.pending_unit,
        }
        for name, operation in sorted(_OPERATIONS.items())
    ]


def _valid_args(args: object, operation: _Operation) -> bool:
    return (type(args) is dict and args.keys() == set(_arg_names(operation))
            and all(type(value) is str and re.fullmatch(_arg_pattern(key), value)
                    for key, value in args.items()))


def _id(value: object) -> bool:
    return type(value) is str and re.fullmatch(_ID, value) is not None


def _nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _locator(value: object) -> bool:
    return (type(value) is str and re.fullmatch(_WIRE_LOCATOR, value) is not None
            and not {".", ".."}.intersection(value.split("/")))


def _artifact_ref(value: object) -> bool:
    if type(value) is not dict or value.keys() != _WIRE_REF_KEYS:
        return False
    locator, digest = value["locator"], value["sha256"]
    return (
        value["schema"] == "vp.artifact_ref/v1"
        and _locator(locator)
        and type(digest) is str and re.fullmatch(_SHA256, digest) is not None
        and ("@sha256:" not in locator or locator.partition("@sha256:")[2] == digest)
        and _nonnegative_integer(value["bytes"])
        and type(value["media_type"]) is str and value["media_type"].isprintable()
        and _id(value["stage"]) and value["copy"] in ("none", "file")
    )


def _wire_context(context: object) -> bool:
    return (
        type(context) is dict and {"caller", "agent_instance"} <= context.keys()
        and context.keys() <= (_WIRE_CONTEXT_IDS | {"caller", "authority_epoch"})
        and context["caller"] in ("agent", "operator")
        and all(_id(context[key]) for key in context.keys() & _WIRE_CONTEXT_IDS)
        and ("authority_epoch" not in context or _nonnegative_integer(context["authority_epoch"]))
    )


def _wire_args(args: dict) -> bool:
    return (
        _WIRE_ARG_KEYS <= args.keys() <= (_WIRE_ARG_KEYS | {"permit_ref", "seed"})
        and _id(args["goal_id"])
        and type(args["inputs"]) is list and all(_artifact_ref(ref) for ref in args["inputs"])
        and ("permit_ref" not in args or _id(args["permit_ref"]) or _locator(args["permit_ref"]))
        and ("seed" not in args or _nonnegative_integer(args["seed"]))
    )


def _charge_request(value: object, remaining: int, depth: int = 0) -> int:
    """Bound work before allocating a canonical encoding of an in-process request."""
    if depth > 8 or remaining < 1:
        raise ValueError("request budget exceeded")
    kind = type(value)
    if kind is str:
        return remaining - len(value) - 2
    if kind is int:
        # A decimal digit carries fewer than four bits.
        return remaining - max(1, (value.bit_length() + 3) // 4)
    if value is None or kind is bool:
        return remaining - 1
    if kind is list:
        remaining -= max(0, len(value) - 1)
        for item in value:
            remaining = _charge_request(item, remaining, depth + 1)
    elif kind is dict:
        remaining -= len(value)
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("request keys must be strings")
            remaining = _charge_request(key, remaining, depth + 1)
            remaining = _charge_request(item, remaining, depth + 1)
    else:
        raise ValueError("request must contain JSON values")
    return remaining - 2


def _bounded_request(request: dict) -> bool:
    try:
        return (_charge_request(request, _MAX_REQUEST_BYTES) >= 0
                and len(canonical_json(request).encode("utf-8")) <= _MAX_REQUEST_BYTES)
    except (ContractError, ValueError, RecursionError):
        return False


@lru_cache(maxsize=1)
def _wire_rows() -> dict:
    with Path(__file__).with_name("capabilities.json").open(encoding="utf-8") as stream:
        manifest = json.load(stream, object_pairs_hook=_unique_object)
    capability, = manifest["capabilities"]
    if capability["tool"] != _WIRE_TOOL or capability["json_op"] != _WIRE_TOOL:
        raise ValueError("invalid factory capability")
    rows = capability["op_table"]
    if capability["ops"] != [row["op"] for row in rows] or len(rows) != len(set(capability["ops"])):
        raise ValueError("invalid factory operation table")
    return {row["op"]: row for row in rows}


def _refusal(code: str) -> dict:
    return {"ok": False, "code": code, "executed": False}


def _wire_reply(request: dict, digest: str, code: str, notes: list[str] | None = None) -> dict:
    return {
        "schema": "vp.control.reply/v1",
        "request_id": request["request_id"],
        "op": _WIRE_TOOL,
        "ok": False,
        "code": code,
        "result": {},
        "effective_request_sha256": digest,
        "evidence": [],
        "not_verified": [*(notes or []), _REPLAY_NOTE],
    }


def _invalid_wire(request, *, bounded):
    identifier = request.get("request_id")
    if type(identifier) is not str:
        return _refusal("INVALID_REQUEST")
    notes = []
    if bounded:
        digest = sha256_json(request)
    else:
        digest = sha256_json({})
        notes.append("Request rejected before canonical admission. Effective request is empty.")
    # The owner requires a nonempty UTF-8 correlation field even on refusal.
    if not identifier or len(identifier) > _MAX_REQUEST_BYTES or not _valid_utf8(identifier):
        identifier = "invalid-" + digest
        notes.append("Original request ID cannot be returned within the reply contract.")
    return _wire_reply({"request_id": identifier}, digest, "INVALID_REQUEST", notes)


def _valid_utf8(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class LocalDryRun:
    """Plan locally with bounded, instance-local replay. C4 owns durable replay."""

    def __init__(self, *, allowed_root: Path | None = None, registry: Registry | None = None):
        self._allowed_root = None
        if allowed_root is not None:
            root = Path(allowed_root)
            if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
                raise ValueError("invalid artifact root")
            self._allowed_root = root
        self._registry = registry
        self._requests: dict[str, str] = {}
        self._lock = Lock()

    def handle(self, request: object, *, dry_run: bool | None = None) -> dict:
        if type(request) is not dict:
            return _refusal("INVALID_REQUEST")
        wire = ("schema" in request or request.get("op") == _WIRE_TOOL
                or "context" in request or "dry_run" in request
                or ("request_id" in request and not (_REQUEST_KEYS <= request.keys())))
        if not _bounded_request(request):
            return _invalid_wire(request, bounded=False) if wire else _refusal("INVALID_REQUEST")
        if wire:
            return self._handle_wire(request, dry_run)
        return self._handle_local(request, dry_run)

    def _handle_wire(self, request: dict, dry_run: bool | None) -> dict:
        if (request.keys() != _WIRE_REQUEST_KEYS or request["schema"] != _WIRE_SCHEMA
                or request["op"] != _WIRE_TOOL or not _id(request["request_id"])
                or not _wire_context(request["context"])):
            return _invalid_wire(request, bounded=True)

        digest = sha256_json(request)
        if request["dry_run"] is not True or (dry_run is not None and dry_run is not True):
            return _wire_reply(request, digest, "DRY_RUN_REQUIRED")
        args = request["args"]
        if type(args) is not dict or type(args.get("op")) is not str or not _wire_args(args):
            return _wire_reply(request, digest, "INVALID_ARGS")
        name = args["op"]
        try:
            row = _wire_rows().get(name)
        except (OSError, ValueError, KeyError, TypeError):
            return _wire_reply(request, digest, "NOT_IMPLEMENTED", ["Factory capability manifest unavailable."])
        operation = _OPERATIONS.get(name)
        if row is None or operation is None:
            return _wire_reply(request, digest, "ARGS_SCHEMA_ABSENT")

        expected = [key.removesuffix("_sha256") for key in operation.inputs if key != "permit_sha256"]
        if (sorted(ref["stage"] for ref in args["inputs"]) != sorted(expected)
                or ("permit_sha256" in operation.inputs and "permit_ref" not in args)):
            return _wire_reply(request, digest, "INVALID_ARGS")
        resolve = name == "af.registry.resolve" and self._registry is not None
        if resolve:
            if args.keys() != _WIRE_ARG_KEYS or not args["inputs"][0]["locator"].startswith("af://"):
                return _wire_reply(request, digest, "INVALID_ARGS")
            if (row["risk_class"], row["operator_only"]) != ("leer", False):
                return _wire_reply(request, digest, "POLICY_MISMATCH")
        locators = [ref["locator"] for ref in args["inputs"]]
        if "permit_ref" in args:
            locators.append(args["permit_ref"])
        for locator in locators:
            if locator.startswith("file://"):
                path = Path(locator.removeprefix("file://"))
                try:
                    if (self._allowed_root is None or self._allowed_root not in path.parents
                            or path.resolve(strict=True) != path or not path.is_file()):
                        return _wire_reply(request, digest, "INVALID_ARGS")
                except (OSError, RuntimeError):
                    return _wire_reply(request, digest, "INVALID_ARGS")

        receipt = self._dispatch(
            request["request_id"], name, args, digest, operation,
            row["risk_class"], row["operator_only"],
        )
        if "checkpoint" not in receipt:
            return _wire_reply(request, digest, receipt["code"])
        if resolve:
            return self._resolve_registry(request, digest)
        checkpoint = receipt["checkpoint"]
        notes = [
            f"risk_class={receipt['risk_class']}, operator_only={str(receipt['operator_only']).lower()}",
            "Dry run only. No stage executed and no checkpoint taken.",
            f"Checkpoint intent only: {checkpoint['backend']}/{checkpoint['name']} ({checkpoint['phase']}).",
        ]
        if receipt["pending_unit"]:
            notes.append(f"Pending unit: {receipt['pending_unit']}.")
        # The owner success schema requires actual result artifacts and a receipt.
        return _wire_reply(request, digest, "HOLD", notes)

    def _resolve_registry(self, request: dict, digest: str) -> dict:
        """Resolve through a service-configured, namespace-authorized reader."""
        ref, = request["args"]["inputs"]
        locator = ref["locator"]
        # A digest locator pins the record. For a tagged name the reference's digest pins its catalog.
        snapshot = None if "@sha256:" in locator else "sha256:" + ref["sha256"]
        try:
            closure = self._registry.resolve(locator, snapshot=snapshot, max_objects=128)
        except RegistryError as exc:
            if exc.reason == "name_unavailable":
                return _wire_reply(request, digest, "REGISTRY_NAME_UNAVAILABLE")
            code = {2: "INVALID_ARGS", 3: "REGISTRY_UNAVAILABLE", 4: "REGISTRY_STALE",
                    5: "REGISTRY_REFUSED", 6: "BUDGET_EXCEEDED"}[exc.rc]
            return _wire_reply(request, digest, code)

        pinned = closure["locator"]
        version = closure["record_digest"]
        record = closure["record"]
        receipt = {"path": pinned, "sha256": version.removeprefix("sha256:")}
        pipeline_verdict = record["annotations"].get("pipeline_asset_verdict")
        # Record annotations report an upstream assertion. REG verifies record and gate closure.
        gate = {
            "asset_name": pinned.removeprefix("af://").split("@", 1)[0],
            "display_name": record["display_name"],
            "version": version,
            "snapshot": closure["snapshot"],
            "requested_tag": locator.rsplit(":", 1)[1] if snapshot is not None else None,
            "content_digests": closure["objects"],
            "gate_verdicts": {
                pin: [{key: gate[key] for key in ("gate_id", "receipt_digest", "verdict")}
                      for gate in member["gates"]]
                for pin, member in (closure["lineage_records"] | {version: record}).items()
            },
            "transport_verdict": "PASS",
            "production_verdict": "CANNOT_SEE",
            "pipeline_asset_verdict": (pipeline_verdict if pipeline_verdict in
                                       ("PASS", "FAIL", "CANNOT_SEE", "ERROR") else None),
        }
        reply = _wire_reply(request, digest, "OK", ["Production acceptance is outside registry resolution."])
        reply["ok"] = True
        reply["evidence"] = [receipt]
        reply["result"] = {
            "artifact_refs": [{"schema": "vp.artifact_ref/v1", "locator": pinned,
                               "sha256": receipt["sha256"], "bytes": closure["objects"][version],
                               "media_type": "application/json", "stage": "record", "copy": "none"}],
            "receipt": receipt,
            "gate": canonical_json(gate).rstrip("\n"),
        }
        return reply

    def _handle_local(self, request: dict, dry_run: bool | None) -> dict:
        if dry_run is not True:
            return _refusal("DRY_RUN_REQUIRED")
        if (request.keys() != _REQUEST_KEYS or not _id(request["request_id"])
                or type(request["op"]) is not str):
            return _refusal("INVALID_REQUEST")
        name = request["op"]
        operation = _OPERATIONS.get(name)
        if operation is None:
            return _refusal("UNKNOWN_OP")
        if (type(request["risk_class"]) is not str or request["risk_class"] != operation.risk_class
                or request["operator_only"] is not operation.operator_only):
            return _refusal("POLICY_MISMATCH")
        if not _valid_args(request["args"], operation):
            return _refusal("INVALID_ARGS")
        return self._dispatch(
            request["request_id"], name, request["args"],
            sha256_json(request | {"dry_run": True}), operation,
            operation.risk_class, operation.operator_only,
        )

    def _dispatch(self, request_id: str, name: str, args: dict, digest: str,
                  operation: _Operation, risk_class: str, operator_only: bool) -> dict:
        with self._lock:
            previous = self._requests.get(request_id)
            if previous is None:
                if len(self._requests) >= _MAX_REPLAY_ENTRIES:
                    return _refusal("BUDGET_EXCEEDED")
                self._requests[request_id] = digest
            elif previous != digest:
                return _refusal("REQUEST_ID_CONFLICT")

        planned = operation.pending_unit is None
        return {
            "ok": planned,
            "code": "DRY_RUN" if planned else "NOT_IMPLEMENTED",
            "op": name,
            "request_id": request_id,
            "request_sha256": digest,
            "goal_id": args["goal_id"],
            "risk_class": risk_class,
            "operator_only": operator_only,
            "dry_run": True,
            "executed": False,
            "checkpoint": _checkpoint(operation),
            "checkpoint_taken": False,
            "pending_unit": operation.pending_unit,
        }


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate JSON key")
    return obj


def main(argv: list[str] | None = None, stdin=None, stdout=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read a wire or legacy local JSON request from stdin")
    mode.add_argument("--execute", action="store_true", help="execute a pinned local episode request")
    parser.add_argument("--episode-state-root", type=Path)
    parser.add_argument("--episode-archive-root", type=Path)
    parser.add_argument("--episode-restore-root", type=Path)
    parser.add_argument("--episode-policy", type=Path)
    parser.add_argument("--episode-policy-sha256")
    mode.add_argument("--list-capabilities", action="store_true", help="print the local inventory")
    parser.add_argument("--artifact-root", type=Path, help="canonical directory for local file references")
    parser.add_argument("--registry-root", type=Path, help="absolute directory for read-only registry lookup")
    parser.add_argument("--registry-namespace", help="authorized namespace for registry lookup")
    options = parser.parse_args(argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if options.execute:
        from .episode_local import entry
        return entry.main(options, stdin, stdout)
    if options.list_capabilities:
        stdout.write(canonical_json(local_capabilities()))
        return 0

    try:
        if (options.registry_root is None) != (options.registry_namespace is None):
            raise ValueError("registry root and namespace must be supplied together")
        registry = (Registry(options.registry_root, namespace=options.registry_namespace)
                    if options.registry_root is not None else None)
        lane = LocalDryRun(allowed_root=options.artifact_root, registry=registry)
    except (ValueError, OSError, RuntimeError, RegistryError):
        stdout.write(canonical_json(_refusal("INVALID_ARGS")))
        return 2
    try:
        source = getattr(stdin, "buffer", stdin)
        raw = source.read(_MAX_REQUEST_BYTES + 1)
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ValueError("request size limit")
        request = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        receipt = _refusal("INVALID_REQUEST")
    else:
        receipt = lane.handle(request, dry_run=True)
    stdout.write(canonical_json(receipt))
    if receipt["ok"]:
        return 0
    return {"NOT_IMPLEMENTED": 3, "HOLD": 3, "REGISTRY_NAME_UNAVAILABLE": 3,
            "REGISTRY_UNAVAILABLE": 3, "REGISTRY_STALE": 4, "REGISTRY_REFUSED": 5,
            "BUDGET_EXCEEDED": 6}.get(receipt["code"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
