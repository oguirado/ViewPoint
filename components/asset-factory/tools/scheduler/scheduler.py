"""F0 host scheduler: injected backends, explicit evidence, persistent leases.

start registers ownership before allocation; run performs the allocation. A backend
must cooperate with cancellation. A late task, missing ownership, or unproven
release fences the lease; there is deliberately no crash-recovery shortcut.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import inspect
import os
from pathlib import Path
import stat
import time
from typing import Protocol

from asset_factory import core


MAX_JSON_BYTES = 16 * 2**20
_SCHEMAS = Path(__file__).with_name("schemas")
_SCHEMA_CACHE = {name: core.load_json(_SCHEMAS / (name + ".schema.json"), max_bytes=MAX_JSON_BYTES)
                 for name in ("request", "observation", "receipt")}
_PENDING: set[asyncio.Task] = set()


class Backend(Protocol):
    async def start(self, request: dict, deadline_ns: int) -> str: ...
    async def run(self, handle: str, request: dict, deadline_ns: int) -> dict: ...
    async def stop(self, handle: str, deadline_ns: int) -> None: ...
    async def observe(self, handle: str | None, deadline_ns: int) -> dict: ...


class _Refusal(Exception):
    def __init__(self, rc, code):
        self.rc, self.code = rc, code


def _result(rc, *reasons):
    return {"rc": rc, "status": core.STATUS[rc], "reason_codes": list(reasons)}


def _integer(value, path, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise core.ContractError("schema_mismatch", path)
    return value


def _text(value, path):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise core.ContractError("schema_mismatch", path)
    return value


def _digest(value, path):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise core.ContractError("schema_mismatch", path)


def _shape(value, schema, root=None, path=""):
    """Enforce scheduler shapes even while the deliberately minimal stub is used."""
    root = schema if root is None else root
    if "$ref" in schema:
        node = root
        for key in schema["$ref"].removeprefix("#/").split("/"):
            node = node[key]
        return _shape(value, node, root, path)
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _shape(value, branch, root, path)
                matches += 1
            except core.ContractError:
                pass
        if matches != 1:
            raise core.ContractError("schema_mismatch", path)
        return
    kinds = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "boolean": type(value) is bool, "null": value is None}
    if "type" in schema and not kinds[schema["type"]]:
        raise core.ContractError("schema_mismatch", path)
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise core.ContractError("schema_mismatch", path)
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        raise core.ContractError("schema_mismatch", path)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise core.ContractError("missing_required", path + "/" + key)
        for key, item in value.items():
            child = properties.get(key, schema.get("additionalProperties", True))
            if child is False:
                raise core.ContractError("unknown_property", path + "/" + key)
            if isinstance(child, dict):
                _shape(item, child, root, path + "/" + key)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise core.ContractError("schema_mismatch", path)
        for index, item in enumerate(value):
            _shape(item, schema.get("items", {}), root, path + "/" + str(index))


def _validate(payload, name):
    schema = _SCHEMA_CACHE[name]
    core.canonical_json(payload)
    core.validate(payload, schema)
    _shape(payload, schema)
    if name == "receipt":
        if (payload["status"] != core.STATUS[payload["rc"]] or
            payload["rc"] != core.worst_rc([payload["operation_rc"], payload["cleanup_rc"]])):
            raise core.ContractError("rc_status_mismatch")
        _digest(payload["request_sha256"], "request_sha256")
        _integer(payload["wall_ms"], "wall_ms")
        if payload["peak_rss_bytes"] is None:
            _text(payload["absence_reasons"].get("peak_rss_bytes"), "absence_reasons/peak_rss_bytes")
        else:
            _integer(payload["peak_rss_bytes"], "peak_rss_bytes")
        if payload["rc"] == 0 and (not payload["handle"] or payload["admission_observation"] is None or
                                   payload["release_observation"] is None or payload["release_observation"]["residents"] != []):
            raise core.ContractError("backend_contract")


def _request(request):
    _validate(request, "request")
    for key in ("request_id", "provider_id", "provider_revision", "lease_path"):
        _text(request[key], key)
    if request["provider_revision"] in ("main", "master", "latest", "HEAD"):
        raise core.ContractError("unpinned_revision", "provider_revision")
    if not Path(request["lease_path"]).is_absolute():
        raise core.ContractError("request_invalid", "lease_path")
    for key in ("stage_request_sha256", "provider_sha256", "decision_sha256"):
        _digest(request[key], key)
    context = request["context"]
    for key in ("policy_sha256", "toolchain_sha256"):
        _digest(context[key], "context/" + key)
    _integer(context["timeout_s"], "context/timeout_s", minimum=1)
    memory = request["memory"]
    _integer(memory["ceiling_bytes"], "memory/ceiling_bytes", minimum=1)
    _integer(memory["min_free_bytes"], "memory/min_free_bytes")
    if memory["measured_peak_bytes"] is not None:
        _integer(memory["measured_peak_bytes"], "memory/measured_peak_bytes")
        if memory["measurement_ref"] is None:
            raise core.ContractError("request_invalid", "memory/measurement_ref")
    elif memory["measurement_ref"] is not None:
        raise core.ContractError("request_invalid", "memory/measurement_ref")
    if memory["measurement_ref"] is not None:
        _text(memory["measurement_ref"]["path"], "memory/measurement_ref/path")
        _digest(memory["measurement_ref"]["sha256"], "memory/measurement_ref/sha256")


def _binding(request):
    evidence = request.get("evidence")
    if evidence is None:
        raise _Refusal(3, "authorization_unproven")
    stage, provider, factory, decision = (evidence[k] for k in ("stage_request", "provider", "factory", "decision"))
    if core.sha256_json(stage) != request["stage_request_sha256"]:
        raise _Refusal(4, "request_stale")
    if core.sha256_json(provider) != request["provider_sha256"]:
        raise _Refusal(4, "provider_stale")
    if core.sha256_json(decision) != request["decision_sha256"]:
        raise _Refusal(4, "decision_stale")
    for key, expected in (("configuration_sha256", core.sha256_json(factory)),
                          ("provider_sha256", request["provider_sha256"]),
                          ("policy_sha256", request["context"]["policy_sha256"])):
        if decision.get(key) != expected:
            raise _Refusal(4, "decision_stale")
    rc = decision.get("rc")
    if (decision.get("schema") != "af.licence-decision/v1" or type(rc) is not int or rc not in core.RC
            or decision.get("verdict") != core.VERDICT[rc] or decision.get("allowed") is not (rc == 0)):
        raise core.ContractError("request_invalid", "evidence/decision")
    if (decision.get("provider_id") != request["provider_id"] or
        decision.get("operation") != request["slot"] or
        decision.get("jurisdiction") != request["context"].get("jurisdiction") or
        decision.get("commercial_use") is not request["context"].get("commercial_use")):
        raise _Refusal(4, "decision_stale")
    expected = _build(stage, provider, factory, decision, lease_path=Path(request["lease_path"]))
    if any(request[key] != expected[key] for key in expected):
        raise _Refusal(4, "request_stale")
    if provider.get("runnable") is not True:
        raise _Refusal(5, "provider_blocked")


def _build(stage_request, provider, factory, decision, *, lease_path):
    try:
        context = deepcopy(stage_request["context"])
        memory = factory["memory"]
        peak = provider["runtime"]["ram_peak_gb_measured"]
        measurement_ref = provider["runtime"]["ram_peak_measurement_ref"]
        if provider.get("id") == "trellis2":
            # A calibration is a workload observation, never a provider-wide
            # permission. Missing or additional workload controls stay unknown.
            from providers.trellis2.admission import calibrated_peak
            peak, measurement_ref = calibrated_peak(provider, stage_request.get("params"))
        if peak is not None:
            _integer(peak, "runtime/ram_peak_gb_measured")
        ceiling = _integer(memory["ceiling_gb_default"], "memory/ceiling_gb_default", minimum=1)
        floor = _integer(memory["min_free_gb_before_start"], "memory/min_free_gb_before_start")
        if type(memory["max_resident_models"]) is not int or memory["max_resident_models"] != 1:
            raise core.ContractError("request_invalid", "memory/max_resident_models")
        if (stage_request["kind"] != "request" or stage_request["slot"] != provider["slot"] or
            stage_request["schema"] != "af." + provider["slot"] + "/v1" or
            provider["schema"] != "af.provider/v1" or factory["schema"] != "af.factory-config/v1" or
            stage_request["provider_id"] != provider["id"] or
            stage_request["provider_revision"] != provider["revision"] or
            factory["slots"][provider["slot"]]["provider"] != provider["id"] or
            factory.get("jurisdiction") != context.get("jurisdiction") or
            factory.get("commercial_use") is not context.get("commercial_use")):
            raise core.ContractError("request_invalid")
        return {"schema": "af.scheduler-request/v1", "request_id": stage_request["request_id"],
                "slot": stage_request["slot"], "provider_id": provider["id"],
                "provider_revision": provider["revision"], "kind": provider["kind"],
                "stage_request_sha256": core.sha256_json(stage_request),
                "provider_sha256": core.sha256_json(provider), "decision_sha256": core.sha256_json(decision),
                "context": context, "memory": {"max_resident_models": 1, "ceiling_bytes": ceiling * 2**30,
                    "min_free_bytes": floor * 2**30, "measured_peak_bytes": None if peak is None else peak * 2**30,
                    "measurement_ref": None if peak is None else deepcopy(measurement_ref)},
                "lease_path": str(lease_path)}
    except (KeyError, TypeError) as exc:
        raise core.ContractError("request_invalid") from exc


def build_request(stage_request: dict, provider: dict, factory: dict, decision: dict, *, lease_path: Path) -> dict:
    """Only GiB-to-bytes boundary; input objects are copied, never mutated."""
    request = _build(stage_request, provider, factory, decision, lease_path=lease_path)
    request["evidence"] = deepcopy({"stage_request": stage_request, "provider": provider,
                                    "factory": factory, "decision": decision})
    _request(request)
    return request


def native_admission(provider, params, *, lease, ceiling_bytes, min_free_bytes, image_id, provider_path):
    """Host-only admission snapshot made while the native model lease is held.

    This pre-allocation receipt is not a completed scheduler execution receipt.
    The lease holder must remain alive and attest it to the runtime at entry.
    """
    from providers.trellis2 import admission as native
    try:
        peak, reference = native.calibrated_peak(provider, params, provider_path=provider_path)
        if peak is None:
            return _result(3, "calibration_unavailable")
        if not lease.held():
            return _result(5, "lease_not_held")
        native.need(type(ceiling_bytes) is int and ceiling_bytes > 0 and
                    type(min_free_bytes) is int and min_free_bytes >= 20 * 2**30,
                    2, "memory_policy_invalid")
        calibration = native.calibration(provider, provider_path)
        native.need(image_id == calibration["image_id"], 4, "calibration_stale")
        available = native.mem_available()
        if peak * 2**30 > ceiling_bytes:
            return _result(5, "peak_over_ceiling")
        if available - peak * 2**30 < min_free_bytes:
            return _result(5, "headroom_below_floor")
        return dict(_result(0), schema="af.trellis-native-admission/v1", lease=lease.evidence(),
                    admitted_monotonic_ns=time.monotonic_ns(), boot_id=native.boot_id(),
                    host_mem_available_bytes=available, measured_peak_bytes=peak * 2**30,
                    ceiling_bytes=ceiling_bytes, min_free_bytes=min_free_bytes,
                    calibration_sha256=reference["sha256"], image_id=image_id)
    except native.Refusal as exc:
        return _result(exc.rc, exc.code)


def _observation(observation):
    _validate(observation, "observation")
    _text(observation["sample_id"], "sample_id")
    for key in ("monotonic_ns", "host_mem_available_bytes", "cgroup_headroom_bytes", "effective_ceiling_bytes", "swap_limit_bytes"):
        value = observation[key]
        if value is None:
            _text(observation["absence_reasons"].get(key), "absence_reasons/" + key)
        else:
            _integer(value, key)
    residents = observation["residents"]
    if residents is None:
        _text(observation["absence_reasons"].get("residents"), "absence_reasons/residents")
    else:
        handles = set()
        for resident in residents:
            for key in ("handle", "provider_id", "provider_revision"):
                _text(resident[key], "residents/" + key)
            if resident["handle"] in handles:
                raise core.ContractError("observation_invalid", "residents")
            handles.add(resident["handle"])


def _limits(request, observation):
    if observation["effective_ceiling_bytes"] is None or observation["swap_limit_bytes"] is None:
        raise _Refusal(3, "cgroup_unknown")
    if (observation["effective_ceiling_bytes"] > request["memory"]["ceiling_bytes"] or
        observation["swap_limit_bytes"] != 0):
        raise _Refusal(5, "ceiling_unenforced")
    peak = request["memory"]["measured_peak_bytes"]
    if peak is None:
        raise _Refusal(3, "peak_unmeasured")
    if observation["effective_ceiling_bytes"] < peak:
        raise _Refusal(5, "peak_over_ceiling")


def admit(request: dict, observation: dict) -> dict:
    """Pure admission: authorization, complete observations and integer arithmetic."""
    try:
        _request(request)
        _binding(request)
    except core.ContractError:
        return _result(2, "request_invalid")
    except _Refusal as exc:
        return _result(exc.rc, exc.code)
    try:
        _observation(observation)
    except core.ContractError:
        return _result(2, "observation_invalid")
    memory = request["memory"]
    missing = []
    if memory["measured_peak_bytes"] is None or memory["measured_peak_bytes"] == 0:
        missing.append("peak_unmeasured")
    for key, reason in (("host_mem_available_bytes", "memavailable_unknown"),
                        ("cgroup_headroom_bytes", "cgroup_unknown"),
                        ("residents", "resident_unknown"), ("monotonic_ns", "sample_unknown")):
        if observation[key] is None:
            missing.append(reason)
    if missing:
        return _result(3, *missing)
    if observation["residents"] != []:
        return _result(5, "resident_present")
    if memory["measured_peak_bytes"] > memory["ceiling_bytes"]:
        return _result(5, "peak_over_ceiling")
    try:
        _limits(request, observation)
    except _Refusal as exc:
        return _result(exc.rc, exc.code)
    headroom = min(observation["host_mem_available_bytes"], observation["cgroup_headroom_bytes"])
    if headroom - memory["measured_peak_bytes"] < memory["min_free_bytes"]:
        return _result(5, "headroom_below_floor")
    return _result(0)


def _retire(task):
    _PENDING.discard(task)
    if not task.cancelled():
        task.exception()


async def _call(method, *args, deadline_ns, lease, capture_start=False):
    remaining = (deadline_ns - time.monotonic_ns()) / 10**9
    if remaining <= 0:
        raise _Refusal(6, "deadline_exceeded")
    call = method(*args, deadline_ns)
    if not inspect.isawaitable(call):
        raise core.ContractError("backend_contract")
    task = asyncio.ensure_future(call)
    if capture_start:
        lease["start_task"] = task
    try:
        done, _ = await asyncio.wait({task}, timeout=remaining)
    except asyncio.CancelledError:
        task.cancel()
        lease["pending"].append(task)
        _PENDING.add(task)
        task.add_done_callback(_retire)
        raise
    if not done:
        task.cancel()
        lease["pending"].append(task)
        _PENDING.add(task)
        task.add_done_callback(_retire)
        raise _Refusal(6, "deadline_exceeded")
    result = task.result()
    if capture_start:
        _capture_start(lease, result)
    if time.monotonic_ns() > deadline_ns:
        raise _Refusal(6, "deadline_exceeded")
    return result


async def _observe(backend, handle, deadline_ns, lease):
    before = time.monotonic_ns()
    observation = deepcopy(await _call(backend.observe, handle, deadline_ns=deadline_ns, lease=lease))
    _observation(observation)
    stamp = observation["monotonic_ns"]
    if stamp is None:
        raise _Refusal(3, "sample_unknown")
    if not before <= stamp <= time.monotonic_ns():
        raise core.ContractError("observation_invalid", "monotonic_ns")
    previous = lease.get("last_observation")
    seen = lease.setdefault("sample_ids", set())
    if (previous is not None and stamp <= previous["monotonic_ns"]) or observation["sample_id"] in seen:
        raise core.ContractError("observation_invalid", "sample_id")
    seen.add(observation["sample_id"])
    lease["last_observation"] = observation
    return observation


def _acquire(request):
    path = Path(request["lease_path"])
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    lease = {"path": path, "fd": fd, "identity": os.fstat(fd), "request": request,
             "handle": None, "start_attempted": False, "pending": [], "last_observation": None}
    try:
        data = core.canonical_json({"request_sha256": core.sha256_json(request)}).encode("utf-8")
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # Incomplete lease publication is fenced for explicit recovery.
        os.close(fd)
        raise
    return lease


def _unlink_owned(lease):
    current = lease["path"].lstat()
    original = lease["identity"]
    if (not stat.S_ISREG(current.st_mode) or current.st_ino != original.st_ino or
        current.st_dev != original.st_dev or current.st_nlink != 1):
        raise core.ContractError("backend_contract", "lease_path")
    lease["path"].unlink()


def _capture_start(lease, handle):
    """Retain ownership before interpreting a late successful start as timeout."""
    if not isinstance(handle, str) or not handle:
        raise core.ContractError("backend_contract")
    lease["handle"] = handle
    data = core.canonical_json({"request_sha256": core.sha256_json(lease["request"]),
                                "handle": handle}).encode("utf-8")
    fd = lease["fd"]
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.ftruncate(stream.fileno(), len(data))
        os.fsync(stream.fileno())


async def _settle_pending(lease, deadline):
    pending = [task for task in lease["pending"] if not task.done()]
    if pending:
        remaining = max(0, (deadline - time.monotonic_ns()) / 10**9)
        await asyncio.wait(pending, timeout=remaining)
    if any(not task.done() for task in lease["pending"]):
        raise _Refusal(3, "release_unproven")


async def release(lease: dict, backend: Backend) -> dict:
    """Stop only the owned handle; unlink only after fresh physical release proof."""
    observation = None
    try:
        request = lease["request"]
        deadline = time.monotonic_ns() + request["context"]["timeout_s"] * 10**9
        if lease["handle"] is None:
            await _settle_pending(lease, deadline)
            task = lease.get("start_task")
            if task is not None and task.done() and not task.cancelled() and task.exception() is None:
                _capture_start(lease, task.result())
        if lease["start_attempted"]:
            if not isinstance(lease["handle"], str) or not lease["handle"]:
                raise _Refusal(3, "release_unproven")
            stopped = await _call(backend.stop, lease["handle"], deadline_ns=deadline, lease=lease)
            if stopped is not None:
                raise core.ContractError("backend_contract")
            observation = await _observe(backend, lease["handle"], deadline, lease)
            if observation["residents"] != []:
                raise _Refusal(3, "release_unproven")
        await _settle_pending(lease, deadline)
        _unlink_owned(lease)
        return dict(_result(0), observation=observation)
    except core.ContractError as exc:
        return dict(_result(2, "backend_contract", exc.code), observation=observation)
    except (Exception, asyncio.CancelledError):
        return dict(_result(3, "release_unproven"), observation=observation)
    finally:
        if lease.get("fd") is not None:
            os.close(lease["fd"])
            lease["fd"] = None


async def _finalize(lease, backend):
    task = asyncio.create_task(release(lease, backend))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            return result, cancelled
        except asyncio.CancelledError:
            cancelled = True


def _receipt(request, start_ns):
    return {"schema": "af.scheduler-receipt/v1", "request_sha256": core.sha256_json(request),
            "handle": None, "admission_observation": None, "release_observation": None,
            "rc": 3, "status": "INCOMPLETE", "operation_rc": 3, "cleanup_rc": 0,
            "reason_codes": [], "wall_ms": 0, "peak_rss_bytes": None,
            "absence_reasons": {"peak_rss_bytes": "backend_did_not_measure"}}


async def execute(request: dict, backend: Backend) -> dict:
    """A scheduling OK proves the injected lifecycle, never asset appearance."""
    start_ns = time.monotonic_ns()
    request = deepcopy(request)
    receipt = _receipt(request, start_ns)
    lease = None
    operation_rc = 0
    cleanup_rc = 0
    reasons = []
    try:
        _request(request)
        _binding(request)
        try:
            lease = _acquire(request)
        except FileExistsError:
            raise _Refusal(5, "lease_held") from None
        deadline = start_ns + request["context"]["timeout_s"] * 10**9
        observation = await _observe(backend, None, deadline, lease)
        receipt["admission_observation"] = observation
        decision = admit(request, observation)
        if decision["rc"] != 0:
            reasons.extend(decision["reason_codes"])
            raise _Refusal(decision["rc"], decision["reason_codes"][0])
        lease["start_attempted"] = True
        handle = await _call(backend.start, deepcopy(request), deadline_ns=deadline, lease=lease, capture_start=True)
        if not isinstance(handle, str) or not handle:
            raise core.ContractError("backend_contract")
        lease["handle"] = receipt["handle"] = handle
        current = await _observe(backend, handle, deadline, lease)
        residents = current["residents"]
        if residents is None:
            raise _Refusal(3, "resident_unknown")
        if residents != [{"handle": handle, "provider_id": request["provider_id"],
                          "provider_revision": request["provider_revision"]}]:
            raise core.ContractError("backend_contract", "residents")
        _limits(request, current)
        result = await _call(backend.run, handle, deepcopy(request), deadline_ns=deadline, lease=lease)
        if (not isinstance(result, dict) or type(result.get("rc")) is not int or
            result["rc"] not in core.RC or result.get("status") != core.STATUS[result["rc"]] or
            not isinstance(result.get("reason_codes"), list) or
            any(not isinstance(code, str) for code in result["reason_codes"])):
            raise core.ContractError("backend_contract")
        core.canonical_json(result)
        operation_rc = result["rc"]
        reasons.extend(result["reason_codes"])
        peak = result.get("peak_rss_bytes")
        if peak is not None:
            _integer(peak, "peak_rss_bytes")
            receipt["peak_rss_bytes"] = peak
            receipt["absence_reasons"].pop("peak_rss_bytes")
            if peak > request["memory"]["ceiling_bytes"]:
                operation_rc = operation_rc or 6
                reasons.append("ceiling_exceeded")
    except _Refusal as exc:
        operation_rc = exc.rc
        reasons.append(exc.code)
    except asyncio.CancelledError:
        operation_rc = 6
        reasons.append("cancelled")
    except core.ContractError as exc:
        operation_rc = 2
        reasons.append("backend_contract" if lease and lease["start_attempted"] else "request_invalid")
        reasons.append(exc.code)
    except Exception:
        operation_rc = 2
        reasons.append("backend_contract")
    finally:
        if lease is not None:
            cleanup, cancelled = await _finalize(lease, backend)
            cleanup_rc = cleanup["rc"]
            receipt["handle"] = lease["handle"]
            receipt["release_observation"] = cleanup["observation"]
            reasons.extend(cleanup["reason_codes"])
            if cancelled and operation_rc == 0:
                operation_rc = 6
                reasons.append("cancelled")
    receipt["operation_rc"], receipt["cleanup_rc"] = operation_rc, cleanup_rc
    receipt["rc"] = core.worst_rc([operation_rc, cleanup_rc])
    receipt["status"] = core.STATUS[receipt["rc"]]
    receipt["reason_codes"] = sorted(set(reasons))
    receipt["wall_ms"] = (time.monotonic_ns() - start_ns) // 10**6
    _validate(receipt, "receipt")
    return receipt


def _backend():
    # The production backend and effective-child-limit proof are AUSENTE in F0.
    return None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise core.ContractError("request_invalid", message)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        request = core.load_json(args.request, max_bytes=MAX_JSON_BYTES)
        _request(request)
        if (args.request.resolve() == args.receipt.resolve() or
            args.receipt.resolve() == Path(request["lease_path"]).resolve() or
            (args.receipt.exists() and args.request.samefile(args.receipt))):
            raise core.ContractError("request_invalid", "receipt_alias")
        if not args.receipt.parent.is_dir() or (args.receipt.exists() and not args.receipt.is_file()):
            raise core.ContractError("request_invalid", "receipt_parent")
        backend = _backend()
        if backend is None:
            operation_rc, reason = 3, "backend_unavailable"
            try:
                _binding(request)
            except _Refusal as exc:
                operation_rc, reason = exc.rc, exc.code
            result = _receipt(request, time.monotonic_ns())
            result.update(_result(core.worst_rc([operation_rc, 0]), reason), operation_rc=operation_rc)
        else:
            result = asyncio.run(execute(request, backend))
        _validate(result, "receipt")
        if result["rc"] == 0:
            try:
                core.atomic_write_json(args.receipt, result)
            except OSError as exc:
                raise core.ContractError("publish_failed", str(args.receipt)) from exc
        return core.emit(result)
    except core.ContractError as exc:
        return core.emit(exc.as_dict())
    except (OSError, ValueError, RecursionError):
        return core.emit(core.ContractError("request_invalid").as_dict())


if __name__ == "__main__":
    raise SystemExit(main())
