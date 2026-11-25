#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Outside-capsule, source-only Factory Pack replay."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import site
import stat
import struct
import subprocess
import sys

from fixture_blend_bytes import write_fixture_blend

HERE = Path(__file__).resolve().parent
FACTORY = HERE.parents[1]
MODEL_DIRS = {
    "MODEL-DINO2": "dinov2-giant", "MODEL-DINO3": "dinov3-vitl16-pretrain-lvd1689m",
    "MODEL-HI3D": "Hi3Dv1.0", "MODEL-HUNYUAN": "Hunyuan3D-2.1",
    "MODEL-MATTE": "BiRefNet", "MODEL-MOCAP": "MoCapAnythingV2", "MODEL-PART": "PartCrafter",
    "MODEL-TRELLIS1": "TRELLIS-image-large", "MODEL-TRELLIS2": "TRELLIS.2-4B",
    "MODEL-TRIPO": "TripoSG", "MODEL-UNIRIG": "UniRig", "MODEL-UPSCALE": "RealESRGAN",
}
CHECKPOINT_SUFFIXES = {".pt", ".pth", ".ckpt", ".bin", ".safetensors", ".gguf",
                       ".onnx", ".tflite", ".npz", ".npy", ".pkl", ".h5"}
ENV = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8", TZ="UTC", PYTHONDONTWRITEBYTECODE="1")
SCOPE = ["systemd-run", "--user", "--scope", "-p", "MemoryHigh=6G", "-p", "MemoryMax=8G",
         "-p", "MemorySwapMax=0", "nice", "-n", "15", "taskset", "-c", "0-15"]


class Refusal(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def get(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def looks_like_weights(path):
    if path.suffix.lower() in CHECKPOINT_SUFFIXES:
        return True
    with path.open("rb") as stream:
        lead = stream.read(8)
        if lead.startswith((b"GGUF", b"\x93NUMPY")):
            return True
        if len(lead) != 8:
            return False
        length, = struct.unpack("<Q", lead)
        if not 8 <= length <= min(1_000_000, path.stat().st_size - 8):
            return False
        try:
            header = json.loads(stream.read(length))
        except (UnicodeError, ValueError):
            return False
    return isinstance(header, dict) and any(
        isinstance(entry, dict) and {"dtype", "shape", "data_offsets"} <= entry.keys()
        for entry in header.values())


def inventory(root):
    found = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        status = path.lstat().st_mode
        require(not stat.S_ISLNK(status), f"distribution_symlink:{name}")
        if stat.S_ISDIR(status):
            continue
        require(stat.S_ISREG(status), f"distribution_nonregular:{name}")
        require(not looks_like_weights(path), f"checkpoint_in_distribution:{name}")
        found[name] = sha(path)
    return found


def distribution(root, bundle):
    expected = get(bundle)
    require(expected.get("schema") == "vp3d.factory-bundle/v1", "bundle_schema")
    actual = inventory(root)
    require(actual == expected["files"], "distribution_inventory_mismatch")
    return {"files": len(actual), "candidate_sha256": hashlib.sha256(
        json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def discover(model_root):
    require(model_root.is_dir(), "model_root_unavailable")
    options = []
    for unit, name in MODEL_DIRS.items():
        options.append({"provider_evidence": unit, "directory": name,
                        "presence": "present" if (model_root / name).is_dir() else "missing",
                        "inference_admitted": False,
                        "scope_evidence_sha256": sha(HERE / "evidence" / unit / "provider-bundle.json")})
    return {"schema": "vp3d.factory-pack-discovery/v1", "model_root": str(model_root),
            "presence_only": True, "configured_providers": options}


def glb(path):
    data = path.read_bytes()
    if struct.unpack_from("<4sII", data) != (b"glTF", 2, len(data)):
        raise ValueError("invalid glb envelope")
    length, kind = struct.unpack_from("<I4s", data, 12)
    if kind != b"JSON":
        raise ValueError("missing glb JSON")
    return json.loads(data[20:20 + length]), data[28 + length:]


def admit_job(job):
    fields = {"id", "width_um", "height_um", "drop_um", "radius_um", "angle_mdeg",
              "camera_x_um", "frames", "oracle"}
    require(isinstance(job, dict) and job.keys() == fields, "fixture_fields")
    require(isinstance(job["id"], str) and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", job["id"]),
            "fixture_identifier")
    for name, lower, upper in (("width_um", 300_000, 900_000), ("height_um", 500_000, 1_500_000),
                               ("drop_um", 650_000, 1_150_000), ("radius_um", 60_000, 180_000),
                               ("angle_mdeg", 15_000, 60_000), ("camera_x_um", 2_900_000, 3_800_000)):
        require(type(job[name]) is int and lower <= job[name] <= upper, f"fixture_range:{name}")
    require(job["frames"] == [1, 2, 3], "fixture_frame_scope")
    oracle = job["oracle"]
    require(isinstance(oracle, dict) and oracle.keys() == {"contact_us", "vertices", "triangles"},
            "fixture_oracle_fields")
    require(oracle["vertices"] == 8 and oracle["triangles"] == 12, "fixture_topology_oracle")
    require(job["radius_um"] == round(job["width_um"] / 50_000) * 10_000, "fixture_proxy_radius")
    contact = math.sqrt(2 * (job["drop_um"] / 1_000_000) / 9.81) * 1_000_000
    interval = oracle["contact_us"]
    require(isinstance(interval, list) and len(interval) == 2 and
            all(type(value) is int and abs(value - contact) <= 5_000 for value in interval)
            and interval[0] <= contact <= interval[1], "fixture_contact_oracle")


def validate_asset(path, job):
    model, _ = glb(path)
    primitive = model["meshes"][0]["primitives"][0]
    position = model["accessors"][primitive["attributes"]["POSITION"]]
    # Flat-shaded glTF export may split corners along the faces.
    require(position["count"] in (job["oracle"]["vertices"], 24), "asset_vertex_count")
    require(model["accessors"][primitive["indices"]]["count"] == 3 * job["oracle"]["triangles"],
            "asset_triangle_count")
    dimensions = sorted(round((high - low) * 1_000_000)
                        for low, high in zip(position["min"], position["max"]))
    expect = sorted([job["width_um"], round(job["width_um"] * .66), job["height_um"]])
    require(dimensions == expect, "asset_dimensions")
    return dimensions


def validate_skin(path):
    model, binary = glb(path)
    primitive, = model["meshes"][0]["primitives"]
    attributes = primitive["attributes"]
    require(bool(model.get("skins")) and {"JOINTS_0", "WEIGHTS_0"} <= attributes.keys(), "skin_missing")
    skin, = model["skins"]
    joint_names = {model["nodes"][entry]["name"] for entry in skin["joints"]}
    require(joint_names == {"Base", "Tip"}, "skin_joint_set")

    def rows(index, fmt):
        accessor = model["accessors"][index]
        view = model["bufferViews"][accessor["bufferView"]]
        item = struct.Struct(fmt)
        start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        stride = view.get("byteStride", item.size)
        return [item.unpack_from(binary, start + stride * row) for row in range(accessor["count"])]

    joint_data = model["accessors"][attributes["JOINTS_0"]]
    joints = rows(attributes["JOINTS_0"], {5121: "<4B", 5123: "<4H"}[joint_data["componentType"]])
    weights = rows(attributes["WEIGHTS_0"], "<4f")
    require(len(joints) == len(weights) > 0, "skin_accessor_length")
    used = set()
    for row, weight in zip(joints, weights, strict=True):
        require(all(0 <= value <= 1 for value in weight)
                and abs(sum(weight) - 1) < 1e-6, "skin_weight_sum")
        used.update(joint for joint, value in zip(row, weight, strict=True) if value > 0)
    require(used == {0, 1}, "skin_unreferenced_joint")
    require(any(channel["target"]["path"] == "rotation"
                for action in model.get("animations", [])
                for channel in action["channels"]), "action_missing")
    return {"joints": sorted(joint_names), "weighted_vertices": len(weights)}


def validate_contact(result, job, rig_sha):
    require(result["source_rig_sha256"] == rig_sha, "simulation_source_mismatch")
    require(result["source_mesh_width_um"] == job["width_um"], "simulation_proxy_mismatch")
    require([sample["frame"] for sample in result["presentation"]] == job["frames"],
            "presentation_frames_mismatch")
    low, high = job["oracle"]["contact_us"]
    require(low <= result["first_contact_us"] <= high, "contact_time_outside_oracle")
    first, mid, final = (row["height_um"] for row in result["presentation"])
    require(first == job["drop_um"] + job["radius_um"] and first > mid > final,
            "simulation_track_mismatch")


def run_step(command, work, name, scoped=False, capture=False):
    argv = (SCOPE if scoped else []) + [str(value) for value in command]
    log = work / (name + ".log")
    environment = dict(ENV, HOME=str(work / "home"), TMPDIR=str(work / "tmp"),
                       XDG_CACHE_HOME=str(work / "cache"), XDG_CONFIG_HOME=str(work / "config"),
                       BLENDER_USER_CONFIG=str(work / "config"), PYTHONUSERBASE=site.getuserbase())
    result = subprocess.run(argv, cwd=work, env=environment, timeout=150,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    log.write_bytes(result.stderr + (b"" if capture else result.stdout))
    if result.returncode:
        raise RuntimeError(f"{name} rc={result.returncode}: {log.read_text(errors='replace')[-1800:]}")
    return result.stdout


def blender(script, args, work, stage):
    run_step(["blender", "-b", "--factory-startup", "--disable-autoexec", "-t", "4",
              "--python-exit-code", "2", "--python", HERE / script, "--", *args],
             work, stage, scoped=True)


def scene_op(operation, fields, work, name):
    request, reply = (work / f"{name}.{extension}.json" for extension in ("request", "reply"))
    write(request, {"operation": f"af.scene.{operation}", **fields})
    cli = FACTORY / "tools/blender/scene_ops/scene_cli.py"
    run_step([sys.executable, "-B", cli, "--in", request, "--out", reply], work, name, scoped=True)
    value = get(reply)
    require(value["ok"] is True, f"{name}_refused")
    return value["result"]


def replay(work, source_job, mujoco, mutant=None):
    work.mkdir(exist_ok=False, parents=False)
    for name in ("home", "tmp", "cache", "config"):
        (work / name).mkdir()
    job = get(source_job)
    admit_job(job)
    job_file = work / "job.json"
    write(job_file, job)
    blender("fixture_blender.py", ["asset", work, job_file] +
            (["--remove-skin"] if mutant == "skin" else []), work, "asset-rig")
    dimensions = validate_asset(work / "asset.glb", job)
    skin = validate_skin(work / "rig-action.glb")
    rig_sha = sha(work / "rig-action.glb")

    physics = work / "simulation.json"
    run_step([mujoco, "-B", HERE / "physics_fixture.py", job_file, physics, rig_sha, dimensions[1]],
             work, "physics", scoped=True)
    simulation = get(physics)
    if mutant == "contact":
        simulation["first_contact_us"] += 40000
        write(physics, simulation)
    validate_contact(simulation, job, rig_sha)

    blender("fixture_blender.py", ["scene", work, job_file], work, "compose")
    write_fixture_blend(work / "composed-native.blend", work / "scene.blend")
    base = {"blend": str(work / "scene.blend"), "base_revision_sha256": sha(work / "scene.blend")}
    scene_op("inspect", base, work, "scene-before")
    edited = work / "edited.blend"
    native_edit = work / "edited-native.blend"
    edit = scene_op("edit", {
        **base, "scene": "FactoryScene", "output_blend": str(native_edit),
        "edits": [{"op": "object.set_location", "object": "ReviewCamera",
                   "location": [job["camera_x_um"] / 1_000_000, -5, 2.65]}],
    }, work, "scene-edit")
    require(edit["operations_applied"] == 1
            and edit["source_revision_sha256"] == base["base_revision_sha256"] == sha(work / "scene.blend")
            and edit["revision_sha256"] == sha(native_edit),
            "scene_edit_custody")
    write_fixture_blend(native_edit, edited)
    read = work / "readback.json"
    blender("inspect_blender.py", [edited, read, *job["frames"]], work, "external-scene-read")
    readback = get(read)
    require(readback["camera_x_um"] == job["camera_x_um"] and readback["groups"] == 2
            and readback["action"] == "Pivot action", "scene_oracle")
    observed = readback["samples"]
    require([item["frame"] for item in observed] == job["frames"], "readback_frames")
    require([item["contact_body_z_um"] for item in observed] ==
            [item["height_um"] for item in simulation["presentation"]], "scene_track_readback")
    initial, _, final = (item["mesh_um"] for item in observed)
    require(len(initial) == len(final) == job["oracle"]["vertices"], "readback_vertex_count")
    require(sum(before != after for before, after in zip(initial, final, strict=True)) == 4,
            "skin_deformation_oracle")
    blender("fixture_blender.py", ["render", work, job_file], work, "render")
    picture = {}
    for frame in job["frames"]:
        path = work / f"frame-{frame:03}.png"
        raw = run_step(["ffmpeg", "-v", "error", "-xerror", "-i", path, "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], work, f"decode-{frame}", capture=True)
        require(len(raw) == 160 * 120 * 3 and len(set(raw)) > 16, "render_pixel_oracle")
        picture[path.name] = hashlib.sha256(raw).hexdigest()
    require(len(set(picture.values())) == len(job["frames"]), "duplicated_render_frame")
    movie = work / "preview.mkv"
    run_step(["ffmpeg", "-v", "error", "-framerate", "24", "-start_number", str(job["frames"][0]),
              "-i", "frame-%03d.png", "-an", "-c:v", "ffv1", "-pix_fmt", "bgr0",
              "-fflags", "+bitexact", "-flags:v", "+bitexact", "-threads:v", "1", movie],
             work, "silent-preview")
    raw = run_step(["ffmpeg", "-v", "error", "-xerror", "-i", movie, "-map", "0:v:0",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], work, "preview-decode", capture=True)
    stride = 160 * 120 * 3
    require(len(raw) == stride * len(picture) and list(picture.values()) == [
        hashlib.sha256(raw[start:start + stride]).hexdigest() for start in range(0, len(raw), stride)
    ], "preview_frame_identity")
    probe = run_step(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                      "-of", "json", movie], work, "preview-streams", capture=True)
    require(json.loads(probe)["streams"] == [{"codec_type": "video"}], "preview_not_silent")
    artifact_names = ("asset.glb", "rig-action.glb", "simulation.json", "edited.blend", "preview.mkv")
    run_step([sys.executable, "-B", HERE / "registry.py", "put", work, *artifact_names],
             work, "registry-put")
    run_step([sys.executable, "-B", HERE / "registry.py", "resolve", work, *artifact_names],
             work, "registry-fresh-reader")
    result = {"job": job["id"], "scope": "local_offline_fixture", "asset_dimensions_um": dimensions,
              "skin": skin, "simulation": simulation, "scene": {"camera_x_um": readback["camera_x_um"]},
              "decoded_rgb_sha256": picture, "registry": get(work / "registry-resolution.json"),
              "artifacts": {name: sha(work / name) for name in artifact_names}}
    write(work / "chain.json", result)
    return result


def mode_receipt(selector, capability):
    root = HERE / "evidence"
    if capability == "live":
        raise Refusal("live_binding_unavailable:SHARED-LIVE")
    if capability == "voiced":
        raise Refusal("voiced_media_unadmitted:RP3-REVIEW")
    if selector in MODEL_DIRS:
        names = ("provider-bundle.json", "receipt.json")
    elif re.fullmatch(r"GOLD-[a-z_]+", selector) and (root / selector).is_dir():
        names = ("maturity.json", "receipt.json")
    else:
        raise Refusal("mode_receipt_unknown")
    return {"selector": selector, "scope": "predecessor_evidence", "new_admission": False,
            "sources": {name: {"sha256": sha(root / selector / name),
                               "document": get(root / selector / name)} for name in names}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_subparsers(dest="command", required=True)
    loc = action.add_parser("discover")
    loc.add_argument("--model-root", type=Path, required=True)
    loc.add_argument("--out", type=Path, required=True)
    scan = action.add_parser("verify-distribution")
    scan.add_argument("--bundle", type=Path, required=True)
    evidence = action.add_parser("mode-evidence")
    evidence.add_argument("selector")
    evidence.add_argument("--capability", choices=["recorded", "live", "voiced"], default="recorded")
    job = action.add_parser("run")
    job.add_argument("--job", type=Path, required=True)
    job.add_argument("--out", type=Path, required=True)
    job.add_argument("--mujoco-python", type=Path, required=True)
    job.add_argument("--negative-control", choices=["skin", "contact"])
    try:
        args = parser.parse_args()
        if args.command == "discover":
            write(args.out, discover(args.model_root.resolve(strict=True)))
        elif args.command == "verify-distribution":
            print(json.dumps(distribution(FACTORY.parent, args.bundle), sort_keys=True))
        elif args.command == "mode-evidence":
            print(json.dumps(mode_receipt(args.selector, args.capability), sort_keys=True))
        else:
            value = replay(args.out.resolve(), args.job, args.mujoco_python.absolute(),
                           args.negative_control)
            print(json.dumps(value, sort_keys=True))
    except Refusal as error:
        print(f"REFUSED {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"HARNESS_ERROR {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
