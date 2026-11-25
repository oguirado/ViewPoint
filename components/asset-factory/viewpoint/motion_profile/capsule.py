from pathlib import Path
import math
import re
import resource
import sys
import time

from motion import (HASH, Rejected, checked_json, checked_rig, compare, demand, digest,
                    encode, measure, read_bvh, shape)


PROVENANCE = "vp3d.motion_fixture_action/v1"
REFERENCE = "vp3d.motion_fixture_reference/v1"
NATIVE_MODE = "supplied_bvh_with_video_clock"
SOURCE_TYPE = {"text": "text/plain", "video": "video/x-yuv4mpeg"}
REFERENCE_ACTIONS = {"qualify", "benchmark", "repair", "compare"}
ACTIONS = REFERENCE_ACTIONS | {"inspect", "preview"}


def create(name, data):
    path = Path(name)
    output = path.open("xb")
    try:
        with output:
            output.write(data)
    except OSError:
        path.unlink()
        raise


def create_revision(data, binding):
    create("derived.bvh", data)
    try:
        create("derived.action.json", encode(binding))
    except OSError:
        Path("derived.bvh").unlink()
        raise


def video_clock(raw, frame_time, frame_count):
    header, _, payload = raw.partition(b"\n")
    match = re.fullmatch(rb"YUV4MPEG2 W2 H2 F([1-9][0-9]{0,5}):([1-9][0-9]{0,5}) Ip A1:1 C420", header)
    demand(match is not None and len(payload) % 12 == 0
           and all(payload[i:i+6] == b"FRAME\n" for i in range(0, len(payload), 12)), "VIDEO_FIXTURE_INVALID")
    count = len(payload) // 12
    legacy_count = count == 1 and header == b"YUV4MPEG2 W2 H2 F8:1 Ip A1:1 C420"
    numerator, denominator = map(int, match.groups())
    demand(legacy_count or count == frame_count, "VIDEO_CLOCK_MISMATCH")
    demand(math.isclose(frame_time, denominator / numerator, rel_tol=0, abs_tol=1e-8),
           "VIDEO_CLOCK_MISMATCH")


CONCENTRATION_ROLE = "pose_reference_bvh"


def inputs(request, operation, mode):
    if mode == NATIVE_MODE:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from native import DIAGNOSTIC_ROLES, REFERENCE_ROLES, REPAIR_ROLES
        expected = DIAGNOSTIC_ROLES.copy()
        roles = {item["role"] for item in request["inputs"]}
        if operation in REFERENCE_ACTIONS or roles & REFERENCE_ROLES:
            expected.update(REFERENCE_ROLES)
        if operation == "inspect" and CONCENTRATION_ROLE in roles:
            expected.update(REFERENCE_ROLES | {CONCENTRATION_ROLE})
        if operation == "repair":
            expected.update(REPAIR_ROLES)
    else:
        expected = {"rig", "action_bvh", "action_binding", mode}
        if operation in REFERENCE_ACTIONS:
            expected.add("reference")
    files = {}
    for index, item in enumerate(request["inputs"]):
        role = item["role"]
        demand(role in expected and role not in files and item["file"] == f"input-{index}", "INPUT_ROLES_INVALID")
        raw = Path(item["file"]).read_bytes()
        demand(digest(raw) == item["sha256"], "INPUT_PIN_CHANGED")
        files[role] = raw
    demand(files.keys() == expected, "INPUT_ROLES_INVALID")
    if mode == "text":
        demand(bool(files[mode].decode("utf-8").strip()) and b"\0" not in files[mode], "TEXT_SPEC_INVALID")
    return files


def perform(request):
    started = time.perf_counter_ns()
    operation, params = request["operation"], request["params"]
    mode = params["source_mode"]
    default_model = "tools.motion.native" if mode == NATIVE_MODE else "vp3d-motion-fixture"
    model = params.get("model", default_model)
    demand(model in {"tools.motion.native", "vp3d-motion-fixture", "mocapanything_v2"}, "UNKNOWN_MODEL")
    files = inputs(request, operation, mode)
    if model == "mocapanything_v2" and mode == "text":
        raise Rejected("text_unsupported")
    if model == "mocapanything_v2" and mode == "video":
        raise Rejected("research_only_unverified_runtime")
    demand(model == default_model, "MODEL_MODE_UNSUPPORTED")
    if operation == "repair":
        demand(params["destination"] == "derived", "IN_PLACE_FORBIDDEN")
    if mode == NATIVE_MODE:
        from asset_factory import core
        from providers.mocapanything_v2.bvh import Refusal
        from native import perform as perform_native

        demand("swap_adjacent_frames" not in params, "RETIMING_NOT_ADMISSIBLE")
        try:
            return perform_native(operation, params, files, started, create_revision)
        except Refusal as error:
            raise Rejected(error.reason) from error
        except core.ContractError as error:
            raise Rejected(error.code) from error
    demand("advance_foot_frames" not in params, "RETIMING_NOT_ADMISSIBLE")
    rig = checked_rig(files["rig"])
    rig_sha, bvh_sha = digest(files["rig"]), digest(files["action_bvh"])
    binding = checked_json(files["action_binding"])
    shape(binding, "schema rig_sha256 bvh_sha256 source_mode source_sha256 source_media_type parent_bvh_sha256",
          "ACTION_BINDING_INVALID")
    parent = binding["parent_bvh_sha256"]
    demand(binding["schema"] == PROVENANCE and binding["bvh_sha256"] == bvh_sha
           and (parent is None or (type(parent) is str and HASH.fullmatch(parent) and parent != bvh_sha)),
           "ACTION_BINDING_INVALID")
    demand(binding["rig_sha256"] == rig_sha, "WRONG_RIG_BINDING")
    demand(binding["source_mode"] == mode and binding["source_sha256"] == digest(files[mode])
           and binding["source_media_type"] == SOURCE_TYPE[mode], "SOURCE_BINDING_MISMATCH")

    hierarchy, headers, rows, frame_time = read_bvh(files["action_bvh"], rig)
    if mode == "video":
        video_clock(files[mode], frame_time, len(rows))
    before = measure(rig, rows, frame_time)
    if operation in REFERENCE_ACTIONS:
        reference = checked_json(files["reference"])
        shape(reference, "schema rig_sha256 measurements", "REFERENCE_BINDING_INVALID")
        demand(reference["schema"] == REFERENCE and reference["rig_sha256"] == rig_sha
               and type(reference["measurements"]) is dict, "REFERENCE_BINDING_INVALID")
        expected = reference["measurements"]
    result = {"source_mode": mode, "scope": "offline_fixture", "rig_sha256": rig_sha, "bvh_sha256": bvh_sha}
    if operation == "inspect":
        result.update(measurements=before, joint_order=[joint["name"] for joint in rig["joints"]])
    elif operation == "preview":
        result["contact_strip"] = [
            {"frame": frame, "time_s": sample["time_s"], "soles": sample["soles"]}
            for frame, sample in enumerate(before["timeline"])
        ]
    elif operation == "repair":
        demand(params["base_revision_sha256"] == bvh_sha, "STALE_BASE_REVISION")
        swap = params.get("swap_adjacent_frames")
        demand(swap is not None, "RETIMING_NOT_ADMISSIBLE")
        left, right = swap
        demand(0 < left < right == left + 1 < len(rows) - 1, "RETIMING_NOT_ADMISSIBLE")
        try:
            compare(before, expected)
        except Rejected as error:
            demand(str(error) == "CONTACT_ONSET_MISMATCH", "RETIMING_NOT_ADMISSIBLE")
        else:
            raise Rejected("RETIMING_NOT_NEEDED")
        rows[left], rows[right] = rows[right], rows[left]
        rendered = hierarchy + "MOTION\n" + "\n".join(headers + [
            " ".join(format(value, ".17g") for value in row) for row in rows
        ]) + "\n"
        revised = rendered.encode("ascii")
        _, _, reread, revised_time = read_bvh(revised, rig)
        after = measure(rig, reread, revised_time)
        compare(after, expected)
        revised_sha = digest(revised)
        demand(revised_sha != bvh_sha, "RETIMING_NOT_NEEDED")
        derived = binding | {"parent_bvh_sha256": bvh_sha, "bvh_sha256": revised_sha}
        create_revision(revised, derived)
        result.update(derived_binding=derived, derived_bvh=rendered,
                      before_onsets_frame=before["contact_onsets_frame"], after_onsets_frame=after["contact_onsets_frame"])
    else:
        compare(before, expected)
        result.update(match=True, reference_sha256=digest(files["reference"]),
                      frames=len(rows), joints=len(rig["joints"]))
        if operation == "benchmark":
            result.update(device="cpu", wall_ns=time.perf_counter_ns() - started,
                          peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
    return result


def main():
    request = checked_json(Path("request.json").read_bytes())
    if request["schema"] != "vp3d.host_exchange.req/v1" or sys.argv[1:] != [request["operation"]]:
        raise RuntimeError("Unbound host exchange")
    if request["operation"] not in ACTIONS:
        raise RuntimeError("Unknown motion operation")
    try:
        result = perform(request)
        code = "OK"
    except (Rejected, ValueError) as error:
        reason = str(error) if isinstance(error, Rejected) else "MALFORMED_INPUT"
        code, result = "EXPECTATION_FAILED", {}
        create("diagnosis.json", encode({"reason_code": reason, "request_sha256": request["request_sha256"]}))
    create("reply.json", encode({"schema": "vp3d.host_exchange.reply/v1",
                                "request_sha256": request["request_sha256"], "code": code, "result": result}))


if __name__ == "__main__":
    main()
