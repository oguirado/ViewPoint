import hashlib
import json
from pathlib import Path
import re
import resource
import struct
import sys
import time

from measure import Rejected, measure, require, unique


HERE = Path(__file__).resolve().parent
PROFILE = "rp4.rig.offline_fixture/v1"
TARGET = "derived.glb"


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def bind_inputs(request, roles):
    inputs = request["inputs"]
    require([row["role"] for row in inputs] == roles, "RIG_INPUT_ROLES")
    payloads = []
    for index, row in enumerate(inputs):
        name = f"input-{index}"
        require(row["file"] == name, "RIG_STAGING_PATH")
        with open(name, "rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        require(len(data) <= 1024 * 1024 and row["sha256"] == digest(data), "RIG_INPUT_PIN")
        payloads.append(data)
    return payloads


def expectation(raw, trusted_references):
    legacy = (HERE / "expectation.json").read_bytes()
    if raw == legacy:
        return json.loads(legacy)
    reason = "RIG_REFERENCE_NOT_ADMITTED"
    require(digest(raw) in trusted_references, reason)
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise Rejected(reason) from error
    require(type(value) is dict and value.keys() == {"asset_sha256", "measurements"}
            and type(value["asset_sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", value["asset_sha256"])
            and type(value["measurements"]) is dict, reason)
    expected = value["measurements"]
    fields = json.loads(legacy)
    require(expected.keys() == fields.keys() and all(type(expected[key]) is type(fields[key]) for key in fields),
            reason)
    return expected

def difference(expected, actual):
    fields = []
    if isinstance(expected, dict) and expected.keys() == actual.keys():
        fields = [key for key in sorted(expected) if expected[key] != actual[key]]
    else:
        fields = ["shape"]
    return fields


def operate(operation, request, trusted_references=()):
    params = request["params"]
    model = params.get("model", "rig.fixture")
    require(model in {"rig.fixture", "unirig"}, "UNKNOWN_MODEL")
    needs_reference = operation in {"qualify", "compare", "benchmark"}
    raw, *reference = bind_inputs(request, ["glb", "reference"] if needs_reference else ["glb"])
    require(model != "unirig", "approved_build_missing")
    input_sha = digest(raw)
    result = {"asset_sha256": input_sha, "profile": PROFILE}
    if needs_reference:
        expected = expectation(reference[0], trusted_references)
        result["reference_sha256"] = digest(reference[0])
    if operation == "repair":
        require(params["target"] == TARGET, "RIG_IN_PLACE_OR_UNGRANTED_TARGET")
        require(params["base_revision_sha256"] == input_sha, "RIG_STALE_BASE")
        report, repairs = measure(raw, diagnose=True)
        require(bool(repairs), "RIG_NO_REPAIR_NEEDED")
        revised = bytearray(raw)
        for _, offset, weights in repairs:
            struct.pack_into("<4f", revised, offset, *weights)
        derived = bytes(revised)
        after, _ = measure(derived)
        try:
            with open(TARGET, "xb") as stream:
                stream.write(derived)
        except FileExistsError as error:
            raise Rejected("RIG_DESTINATION_EXISTS") from error
        return result | {"derived_ref": TARGET, "derived_sha256": digest(derived),
                         "repaired_vertices": [row[0] for row in repairs], "before": report, "after": after}
    if operation == "benchmark":
        require(params["device"] == "cpu", "RIG_WRONG_DEVICE")
        elapsed, cold = [], None
        for index in range(params["samples"]):
            start = time.perf_counter_ns()
            report, _ = measure(raw)
            duration = time.perf_counter_ns() - start
            require(not difference(expected, report), "RIG_REFERENCE_MISMATCH")
            if index == 0:
                cold = duration
            else:
                elapsed.append(duration)
        return result | {"device": "cpu", "cold_measure_ns": cold, "warm_measure_ns": elapsed,
                         "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
    report, issues = measure(raw, diagnose=operation == "inspect")
    if operation == "inspect":
        diagnostics = [{"code": "RIG_SKIN_NOT_NORMALIZED", "vertex": row[0]} for row in issues]
        return result | {"structure": report, "issues": diagnostics}
    if operation == "preview":
        return result | {"perception_level": "none", "kind": "structural_pose_strip", "poses": report["poses"]}
    require(operation in {"qualify", "compare"}, "RIG_UNKNOWN_OPERATION")
    changed = difference(expected, report)
    if operation == "compare":
        return result | {"equal": not changed, "changed_fields": changed}
    require(not changed, "RIG_REFERENCE_MISMATCH")
    return result | {"qualification": "offline_fixture", "live_use_permitted": False,
                     "joints": len(report["joints"]), "vertices": len(report["positions"]),
                     "pose_samples": len(report["poses"])}


def main(operation, trusted_references=()):
    request = json.loads(Path("request.json").read_bytes())
    if request["schema"] != "vp3d.host_exchange.req/v1" or request["operation"] != operation:
        raise ValueError("Unexpected host envelope")
    reply = {"schema": "vp3d.host_exchange.reply/v1", "request_sha256": request["request_sha256"]}
    try:
        reply.update(code="OK", result=operate(operation, request, trusted_references))
    except Rejected as error:
        with open("diagnosis.json", "x", encoding="utf-8") as stream:
            json.dump({"reason_code": str(error), "request_sha256": request["request_sha256"]}, stream)
        reply.update(code="EXPECTATION_FAILED", result={})
    with open("reply.json", "x", encoding="utf-8") as stream:
        json.dump(reply, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)


if __name__ == "__main__":
    main(sys.argv[-1], tuple(sys.argv[1:-1]))
