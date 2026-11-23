"""Collision boundary contracts; draft registration is explicit, never bypassed."""
from pathlib import Path
import re

from asset_factory import core

HERE = Path(__file__).resolve().parent
REVISION = "131010c4f03fec375ea88c5c6263f120046cb7f1"
VERSION = "1.0.14"
MAX_BYTES = 128 * 2**20  # [TARGET: bounded GLB acquisition]
MAX_PAIRS = 20_000_000  # [TARGET: gate arithmetic budget]
MAX_TIMEOUT_S = 300  # [TARGET: owned worker lifetime]
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class Refusal(Exception):
    def __init__(self, rc, code):
        self.rc, self.code = rc, code
        super().__init__(code)


def require(condition, rc, code):
    if not condition:
        raise Refusal(rc, code)


def integer(value, low=0, high=None):
    return type(value) is int and value >= low and (high is None or value <= high)


def schema(name):
    return core.load_json(HERE / name, max_bytes=128 * 2**10)


def validate_request(request):
    """Use registered contract when available, otherwise the byte-pinned draft."""
    from asset_factory import contracts
    if "collision" in contracts.SLOTS:
        try:
            contracts.validate_slot(request, "collision", "request")
        except core.ContractError as exc:
            raise Refusal(exc.rc, exc.code) from exc
        contract = "registered"
    else:
        core.validate(request, schema("collision.schema.json"))
        contract = "draft"
    require(request["provider_id"] == "coacd", 2, "provider_invalid")
    require(re.fullmatch(r"[0-9a-f]{40}", request["provider_revision"]) is not None,
            2, "unpinned_revision")
    require(request["provider_revision"] == REVISION, 4, "provider_stale")
    require(bool(request["request_id"]), 2, "request_invalid")
    require(len(request["inputs"]) == 1, 2, "input_exclusivity")
    source = request["inputs"][0]
    require(source["role"] == "visual_glb" and source["media_type"] == "model/gltf-binary"
            and bool(DIGEST.fullmatch(source["sha256"])), 2, "input_invalid")
    params = request["params"]
    require(set(params) == {"seed", "threshold_ppm", "resolution", "max_hulls"}, 2, "params_invalid")
    require(integer(params["seed"], 0, 2**31 - 1)
            and integer(params["threshold_ppm"], 10000, 1000000)
            and integer(params["resolution"], 1000, 10000)
            and integer(params["max_hulls"], 1, 256), 2, "params_invalid")
    context = request["context"]
    require(integer(context["timeout_s"], 1, MAX_TIMEOUT_S), 2, "timeout_invalid")
    require(bool(context["jurisdiction"]) and all(DIGEST.fullmatch(context[k])
            for k in ("policy_sha256", "toolchain_sha256")), 2, "context_invalid")
    return contract


def validate_policy(policy):
    core.validate(policy, schema("gate-policy.schema.json"))
    bounds = {"max_hulls": (1, 256), "max_faces_per_hull": (4, 4096),
              "max_lost_volume_ppm": (0, 10000), "max_extra_volume_ppm": (0, 1000000),
              "sample_count": (256, 65536), "sampling_seed": (0, 2**31 - 1),
              "plane_tolerance_um": (0, 100), "timeout_s": (1, MAX_TIMEOUT_S)}
    for key, (low, high) in bounds.items():
        require(integer(policy[key], low, high), 2, "policy_invalid")
    require(len(policy["clearance_segments_um"]) <= 256, 6, "probe_budget_exceeded")
    for segment in policy["clearance_segments_um"]:
        require(len(segment) == 2 and all(len(point) == 3 for point in segment)
                and all(type(x) is int and abs(x) <= 10**12 for p in segment for x in p)
                and segment[0] != segment[1], 2, "clearance_probe_invalid")


def validate_job(job):
    """Closed executable boundary, checked before runtime queries or writes."""
    require(type(job) is dict, 2, "job_invalid")
    operation = job.get("operation")
    require(type(operation) is str and operation in
            ("probe", "inspect", "gate", "generate", "qualify"), 2, "operation_invalid")
    fields = {"operation", "source_sha256", "candidate_sha256", "policy_sha256",
              "toolchain_sha256", "gate_policy", "params"}
    if operation == "qualify":
        fields.add("qualification_runtime")
    require(set(job) == fields, 2, "job_invalid")
    core.canonical_json(job)
    for key in ("source_sha256", "candidate_sha256", "policy_sha256", "toolchain_sha256"):
        needed = (key == "toolchain_sha256" or key == "source_sha256" and operation != "probe"
                  or key == "candidate_sha256" and operation == "gate"
                  or key == "policy_sha256" and operation in ("gate", "generate", "qualify"))
        value = job[key]
        require(type(value) is str and DIGEST.fullmatch(value) is not None
                if needed else value is None, 2, "job_invalid")
    if operation in ("gate", "generate", "qualify"):
        validate_policy(job["gate_policy"])
    else:
        require(job["gate_policy"] is None, 2, "job_invalid")
    if operation in ("generate", "qualify"):
        params = job["params"]
        bounds = {"seed": (0, 2**31-1), "threshold_ppm": (10000, 1000000),
                  "resolution": (1000, 10000), "max_hulls": (1, 256)}
        require(type(params) is dict and set(params) == set(bounds), 2, "params_invalid")
        require(all(integer(params[k], *bound) for k, bound in bounds.items()), 2, "params_invalid")
        if operation == "qualify":
            require(type(job["qualification_runtime"]) is dict, 2, "job_invalid")
    else:
        require(job["params"] is None, 2, "job_invalid")


def receipt(source_sha256=None, policy_sha256=None):
    return {"schema": "af.collision-gate/v1", "rc": 3, "status": "INCOMPLETE",
            "verdict": "CANNOT_SEE", "source_sha256": source_sha256,
            "candidate_sha256": None, "policy_sha256": policy_sha256,
            "toolchain_sha256": None, "reason_codes": [], "metrics": {},
            "absence_reasons": {}, "hulls": [], "source_inventory": [], "runtime": {},
            "replay_verified": False, "thresholds_calibrated": False}


def finish(record, rc, reasons):
    record.update(rc=rc, status=core.STATUS[rc], verdict=core.VERDICT[rc], reason_codes=reasons)
    core.validate(record, schema("gate-receipt.schema.json"))
    return record


def result(request, record, outputs=None, receipts=None):
    obj = {"schema": "af.collision/v1", "kind": "result", "slot": "collision",
           "request_id": request["request_id"], "provider_id": request["provider_id"],
           "provider_revision": request["provider_revision"], "rc": record["rc"],
           "status": record["status"], "outputs": outputs or [], "receipts": receipts or [],
           "metrics": {"wall_ms": record["metrics"].get("wall_ms"),
                       "peak_rss_bytes": record["metrics"].get("peak_rss_bytes")},
           "absence_reasons": {}}
    for key, value in obj["metrics"].items():
        if value is None:
            obj["absence_reasons"][key] = "measurement_unavailable"
    core.validate(obj, schema("collision.schema.json"))
    return obj
