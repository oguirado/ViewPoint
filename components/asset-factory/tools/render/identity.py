"""Semantic render provenance beyond the kernel's bounded schema vocabulary."""
import re
from pathlib import Path

from asset_factory import core
from tools.render.pipeline import Refusal, VERSION

ROLES = frozenset(("blend_scene", "blender_binary", "oidn_binary", "render_script",
                   "pipeline_script", "receipt_schema"))


def integer(value, low, high, reason):
    if type(value) is not int or not low <= value <= high:
        raise Refusal(2, reason)


def digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise Refusal(2, "digest_invalid")


def sources(items, *, dependencies=False):
    roles, paths = {}, set()
    for item in items:
        role = item["role"]
        if role not in ROLES and not (dependencies and role == "scene_dependency"):
            raise Refusal(2, "input_role_invalid")
        path = Path(item["path"])
        if not path.is_absolute() or str(path) in paths:
            raise Refusal(2, "input_path_invalid_or_duplicate")
        paths.add(str(path))
        digest(item["sha256"])
        if role != "scene_dependency":
            if role in roles:
                raise Refusal(2, "duplicate_input_role")
            roles[role] = item
    if set(roles) != ROLES:
        raise Refusal(3, "toolchain_provenance_incomplete")
    return roles


def request_sha256(parameters, toolchain):
    return core.sha256_json({"parameters": parameters, "toolchain": toolchain})


def settings(parameters, frame, samples):
    return {"device": parameters["device"], "seed": parameters["seed"],
            "resolution_percentage": parameters["resolution_percentage"],
            "frame": frame, "samples": samples}


def parameters(record):
    for field in ("device", "seed", "resolution_percentage"):
        if field not in record:
            raise Refusal(3, "run_settings_unavailable")
    if record["device"] != "CPU":
        raise Refusal(2, "unsupported_render_device")
    integer(record["seed"], 0, 2**31 - 1, "seed_invalid")
    integer(record["resolution_percentage"], 1, 100, "resolution_invalid")


def frame(record, params, toolchain, request_hash, job_hash):
    for field, low, high in (("frame", 0, 2**31 - 1), ("samples", 1, 2**31 - 1),
                              ("resolution_percentage", 1, 100),
                              ("width", 1, 40000000), ("height", 1, 40000000)):
        integer(record[field], low, high, "frame_range_invalid:" + field)
    if record["width"] * record["height"] > 40000000:
        raise Refusal(2, "frame_size_invalid")
    if record["device"] != "CPU" or record["denoiser"] != {"name": "OIDN", "version": VERSION, "device": "CPU"}:
        raise Refusal(2, "frame_device_or_denoiser_invalid")
    for value in record["timings_ms"].values():
        integer(value, 0, 2**63 - 1, "timing_invalid")
    integer(record["view"]["gamma_milli"], 1, 2**31 - 1, "gamma_invalid")
    if any(not record["view"][k].strip() for k in ("transform", "look", "display")):
        raise Refusal(2, "view_invalid")
    actual = sources(record["inputs"], dependencies=True)
    if actual != toolchain:
        raise Refusal(4, "frame_toolchain_drift")
    for field in ("seed", "request_sha256", "job_sha256"):
        if field not in record:
            raise Refusal(3, "frame_provenance_unavailable")
    integer(record["seed"], 0, 2**31 - 1, "seed_invalid")
    digest(record["request_sha256"])
    digest(record["job_sha256"])
    if (record["seed"] != params["seed"] or record["device"] != params["device"]
            or record["resolution_percentage"] != params["resolution_percentage"]
            or record["request_sha256"] != request_hash or record["job_sha256"] != job_hash):
        raise Refusal(4, "frame_request_drift")


def job(record, params, request_hash, expected):
    if type(record) is not dict or set(record) != {"mode", "device", "seed", "resolution_percentage", "request_sha256", "tasks"}:
        raise Refusal(2, "render_job_invalid")
    if (record["mode"] != "render" or record["request_sha256"] != request_hash
            or any(record[k] != params[k] or type(record[k]) is not type(params[k])
                   for k in ("device", "seed", "resolution_percentage"))):
        raise Refusal(4, "render_job_request_drift")
    if type(record["tasks"]) is not list or len(record["tasks"]) != len(expected):
        raise Refusal(3, "render_job_coverage_incomplete")
    for task, (frame_id, samples) in zip(record["tasks"], expected):
        if (type(task) is not dict or set(task) != {"frame", "samples", "dir"}
                or type(task["frame"]) is not int or type(task["samples"]) is not int):
            raise Refusal(2, "render_task_invalid")
        if (task["frame"], task["samples"]) != (frame_id, samples):
            raise Refusal(4, "render_task_identity_drift")
        path = Path(task["dir"])
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise Refusal(2, "render_task_path_invalid")
