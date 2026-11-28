# SPDX-License-Identifier: AGPL-3.0-only
"""Lease-bound host exchange for outside-capsule control."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from . import channels, ledger, scene


CHANNELS = (*channels.CHANNEL_ADAPTERS, "vision")
_PROBE_CHANNELS = ("cdp", "at_spi")
VISION_PENDING = "VISION_POOL_PENDING"
_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_SHA256 = r"[0-9a-f]{64}"
_SUPERVISOR = Path(__file__).with_name("supervisor.py").resolve()
_DESCRIPTOR_BACKEND = _SUPERVISOR.parents[2] / "asset_factory/proc_pidns.py"
_NOT_VERIFIED = ("production_admission", "live_capsule_transport", "shared_receipt_publication")


@dataclass(frozen=True)
class VisionQueue:
    cap: int = 1
    order: str = "FIFO"
    timeout_s: int = 60
    ttl_s: int = 300
    custody: tuple[str, ...] = ("front", "pid", "starttime")
    hold_through: tuple[str, ...] = ("capture", "readback", "shutdown")


VISION_QUEUE = VisionQueue()


class Refused(ValueError):
    """A specific contract rejection suitable for a closed host reply."""


class VisionPending(Refused):
    queue = VISION_QUEUE

    def __init__(self):
        super().__init__(VISION_PENDING)


@dataclass(frozen=True)
class Call:
    request_id: str
    attempt_id: str
    capsule_id: str
    incarnation: str
    channel: str
    operation: str
    artifact: str
    args_sha256: str


@dataclass(frozen=True)
class Grant:
    call: Call
    lease_id: str
    valid_from_ns: int
    expires_ns: int


@dataclass(frozen=True)
class Adapter:
    """Host-registered commands and code dependencies for an offline binding."""

    export_argv: tuple[str, ...]
    readback_argv: tuple[str, ...]
    dependency_files: tuple[str, ...] = ()
    module_roots: tuple[str, ...] = ()
    probe_argv: tuple[str, ...] = ()


def _require(condition, code):
    if not condition:
        raise Refused(code)


def _matches(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def arguments_sha256(arguments):
    return sha256(_json(arguments)).hexdigest()


def _remaining(grant):
    now = time.monotonic_ns()
    _require(grant.valid_from_ns <= now, "GRANT_NOT_YET_VALID")
    _require(now < grant.expires_ns, "EXPIRED_GRANT")


def _child_env(directory, module_roots):
    return {
        "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC", "PATH": "/usr/bin:/bin",
        "HOME": str(directory), "TMPDIR": str(directory), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0", "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": ":".join(module_roots),
    }


def _file_pin(path):
    file = Path(path)
    _require(file.is_absolute(), "INVALID_ADAPTER")
    try:
        with file.open("rb") as stream:
            digest = sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise Refused("ADAPTER_PIN_UNAVAILABLE") from exc
    return {"path": str(file), "sha256": digest.hexdigest()}


def _command_pin(argv):
    _require(type(argv) is tuple and argv and all(type(word) is str and word for word in argv)
             and Path(argv[0]).is_absolute(), "INVALID_ADAPTER")
    paths = [argv[0], *(word for word in argv[1:]
                       if Path(word).is_absolute() and Path(word).is_file())]
    return {"argv": list(argv), "argv_sha256": arguments_sha256(argv),
            "files": [_file_pin(path) for path in dict.fromkeys(paths)]}


def _adapter_pin(adapter):
    pin = {
        "export": _command_pin(adapter.export_argv),
        "readback": _command_pin(adapter.readback_argv),
        "dependencies": [_file_pin(path) for path in (_SUPERVISOR, _DESCRIPTOR_BACKEND,
                                                      *adapter.dependency_files)],
        "module_roots": list(adapter.module_roots),
    }
    if adapter.probe_argv:
        pin["probe"] = _command_pin(adapter.probe_argv)
    return pin


def _check_pins(pin):
    commands = ("export", "readback", "probe") if "probe" in pin else ("export", "readback")
    for file in (*pin["dependencies"], *(file for key in commands for file in pin[key]["files"])):
        _require(_file_pin(file["path"]) == file, "ADAPTER_PIN_CHANGED")


def _run(argv, directory, message, grant, module_roots):
    _remaining(grant)
    try:
        child = subprocess.run(
            (sys.executable, "-I", "-B", str(_SUPERVISOR)), cwd=directory,
            env=_child_env(directory, module_roots), stdout=subprocess.PIPE,
            input=_json({"argv": argv, "message": message, "expires_ns": grant.expires_ns}),
        )
        envelope = json.loads(child.stdout)
    except (OSError, ValueError) as exc:
        raise Refused("SUPERVISOR_FAILED") from exc
    _require(child.returncode == 0 and type(envelope) is dict, "SUPERVISOR_FAILED")
    if "error" in envelope:
        raise Refused(envelope["error"])
    reply = envelope.pop("reply")
    _require(type(reply) is dict and reply.get("process") == envelope["process"],
             "ADAPTER_PROCESS_MISMATCH")
    _require(reply.get("call") == message["call"] and reply.get("lease_ref") == message["lease_ref"],
             "ADAPTER_CALL_MISMATCH")
    return envelope, reply


def _artifact(directory, name):
    try:
        return scene.inspect(directory, name)
    except scene.SceneRefused as exc:
        raise Refused(str(exc)) from exc


def _exchange(call, wire, grant, adapter, pin, directory, request_sha256, channel_evidence):
    identity = asdict(call)
    lease_ref = sha256(grant.lease_id.encode()).hexdigest()
    public = {"call": identity, "lease_ref": lease_ref}
    outbound = {**public, "arguments": wire}
    node, proof = channel_evidence
    observation_process = None
    if adapter.probe_argv:
        observed, reply = _run(adapter.probe_argv, directory, outbound, grant, adapter.module_roots)
        _require(reply.get("status") == "observed" and type(reply.get("observation")) is dict,
                 "CHANNEL_OBSERVATION_UNAVAILABLE")
        try:
            node, proof = channels.CHANNEL_ADAPTERS[call.channel](identity, wire, reply["observation"])
        except channels.WireRefused as exc:
            raise Refused("CHANNEL_OBSERVATION_MISMATCH") from exc
        observation_process = observed["process"]
        proof["observation"] = observed
        outbound = {**outbound, "channel_evidence": proof}
    export, exported = _run(adapter.export_argv, directory, outbound, grant, adapter.module_roots)
    _require(observation_process != export["process"], "PROBE_NOT_INDEPENDENT")
    _require(exported.get("status") == "exported", "EXPORT_REFUSED")
    observed, fingerprint = _artifact(directory, call.artifact)
    artifact, structure, provenance = (observed[key] for key in ("artifact", "structure", "provenance"))
    _require(exported.get("artifact") == artifact, "EXPORT_ARTIFACT_MISMATCH")
    _require(provenance.get("producer") == export["process"], "WRONG_EXPORTING_PROCESS")
    _require(provenance == {**public, "producer": export["process"], "channel_evidence": proof},
             "ARTIFACT_PROVENANCE_MISMATCH")
    _require(structure["nodes"] == [node], "CHANNEL_SCENE_MISMATCH")

    # The only observer inputs are the public call and the redacted lease reference.
    observer, readback = _run(adapter.readback_argv, directory, public, grant, adapter.module_roots)
    _require(observer["process"] not in (export["process"], observation_process), "READBACK_NOT_INDEPENDENT")
    _require(readback.get("status") == "reopened", "READBACK_ARTIFACT_MISMATCH")
    _require(type(readback.get("provenance")) is dict, "READBACK_ARTIFACT_MISMATCH")
    _require(readback["provenance"].get("producer") == export["process"],
             "WRONG_EXPORTING_PROCESS")
    _require(all(readback.get(key) == observed[key] for key in observed),
             "READBACK_ARTIFACT_MISMATCH")
    _require(_artifact(directory, call.artifact) == (observed, fingerprint), "ARTIFACT_CHANGED")
    if call.channel == "command":
        _require(readback.get("engine_reimport") == {
            "nodes": structure["nodes"], "vertices": structure["vertices"],
            "triangles": structure["triangles"],
        }, "REIMPORT_MISMATCH")
    _check_pins(pin)
    _remaining(grant)
    return {
        "schema": "vp3d.external_control_receipt/v1",
        "status": "verified",
        "mode": "offline_fixture",
        "scope": "offline_fixture",
        "live_use_permitted": False,
        "not_verified": list(_NOT_VERIFIED),
        "request_id": call.request_id,
        "request_sha256": request_sha256,
        "exchange_request_sha256": arguments_sha256(outbound),
        "call": identity,
        "lease": {"lease_ref": lease_ref, "valid_from_ns": grant.valid_from_ns,
                  "expires_ns": grant.expires_ns},
        "adapter": pin,
        "artifact": artifact,
        "structure": structure,
        "channel_evidence": proof,
        "export": {**export, "status": "exported", "artifact": artifact},
        "readback": {**observer, "status": "reopened", "artifact": artifact,
                     "exporter_process": readback["provenance"]["producer"], "structure": structure,
                     "engine_reimport": readback.get("engine_reimport")},
        "receipt_contracts": {
            "host_attempt_schema": "vp3d.host_attempt_receipt/v1",
            "profile": "rea.receipt_profile_3d/v1",
            "shared_receipt_adapter": {
                "binding_state": "pending_publication", "live_use_permitted": False,
                "shared_schema_id": None, "shared_schema_sha256": None,
            },
        },
    }


def invoke(call, arguments, grant, *, adapters, exchange_root):
    """Run one granted, host-registered offline exchange and return its receipt."""
    _require(type(call) is Call, "INVALID_CALL")
    identity = asdict(call)
    _require(call.channel in CHANNELS and _matches(_SHA256, call.args_sha256)
             and all(_matches(_ID, identity[key]) for key in (
                 "request_id", "attempt_id", "capsule_id", "incarnation", "operation", "artifact"
             )), "INVALID_CALL")
    _require(type(arguments) is dict, "ARGUMENTS_INVALID")
    try:
        arg_bytes = _json(arguments)
    except (TypeError, ValueError, RecursionError) as exc:
        raise Refused("ARGUMENTS_INVALID") from exc
    _require(sha256(arg_bytes).hexdigest() == call.args_sha256, "ARGUMENTS_MISMATCH")
    _require(type(grant) is Grant and grant.call == call and _matches(_ID, grant.lease_id)
             and type(grant.valid_from_ns) is int and type(grant.expires_ns) is int
             and grant.valid_from_ns < grant.expires_ns, "GRANT_MISMATCH")
    _remaining(grant)

    root = Path(exchange_root).resolve(strict=True)
    ledger_path = root / "external-control.sqlite"
    request_sha256 = arguments_sha256({key: value for key, value in identity.items()
                                      if key != "attempt_id"})
    try:
        ledger.claim(ledger_path, call.request_id, call.attempt_id, request_sha256)
    except ledger.ClaimRefused as exc:
        raise Refused(str(exc)) from exc

    try:
        _remaining(grant)
        if call.channel == "vision":
            raise VisionPending()
        adapter = adapters.get((call.channel, call.operation))
        _require(type(adapter) is Adapter, "ENDPOINT_ABSENT")
        _require(adapter.export_argv != adapter.readback_argv, "READBACK_NOT_INDEPENDENT")
        _require(type(adapter.probe_argv) is tuple, "INVALID_ADAPTER")
        if adapter.probe_argv:
            _require(call.channel in _PROBE_CHANNELS, "INVALID_ADAPTER")
            _require(adapter.probe_argv not in (adapter.export_argv, adapter.readback_argv),
                     "PROBE_NOT_INDEPENDENT")
        _require(all(Path(path).is_absolute() and Path(path).is_dir()
                     for path in adapter.module_roots), "INVALID_ADAPTER")
        wire = json.loads(arg_bytes)
        try:
            channel_evidence = channels.CHANNEL_ADAPTERS[call.channel](identity, wire)
        except channels.WireRefused as exc:
            raise Refused(str(exc)) from exc
        except ImportError as exc:
            raise Refused("CHANNEL_CONTRACT_ABSENT") from exc
        pin = _adapter_pin(adapter)
        directory = root / arguments_sha256([call.request_id, call.attempt_id])
        directory.mkdir(mode=0o700)
        receipt = _exchange(call, wire, grant, adapter, pin, directory, request_sha256,
                            channel_evidence)
    except Refused:
        ledger.finish(ledger_path, call.request_id, committed=False)
        raise
    ledger.finish(ledger_path, call.request_id, committed=True)
    return receipt
