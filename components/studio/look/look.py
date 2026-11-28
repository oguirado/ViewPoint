# SPDX-License-Identifier: AGPL-3.0-only
"""Studio LOOK component entry point."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

from PIL import Image

from inspect_glb import verify_asset
import measure
import render_quality
import vision_review

HERE = Path(__file__).resolve().parent
BUNDLE_SCHEMA = "viewpoint.studio-look.bundle/v1"
ARTIFACTS = ("studio.glb", "studio-metrics.json", "contact-sheet.png")
PROFILES = ("generic", "panel-actor")
ATLAS_SIZES = (64, 128, 256, 512, 1024, 2048, 4096)
TOPOLOGY_DEFECTS = ("boundary_edges", "nonmanifold_edges_including_boundary", "nonmanifold_vertices",
                    "inconsistent_winding_edges", "degenerate_faces", "nonincident_face_intersection_pairs")
FEATURES = {"image_left_hand", "image_right_hand", "image_left_foot", "image_right_foot", "antenna_tips"}


class Refused(ValueError):
    """A completed gate found a reason to withhold the stage output."""


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def document(path):
    def no_duplicates(pairs):
        obj = dict(pairs)
        if len(obj) != len(pairs):
            raise ValueError("duplicate_json_key")
        return obj

    obj = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    if not isinstance(obj, dict):
        raise ValueError("expected_json_object")
    return obj


def encode(row):
    return json.dumps(row, indent=2, sort_keys=True, allow_nan=False) + "\n"


def write(path, row):
    # Callers write in a private staging directory.
    path.write_text(encode(row), encoding="utf-8")


def write_request(path, row):
    payload = encode(row).encode("utf-8")
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent) as handle:
            handle.write(payload)
            handle.flush()
            os.link(handle.name, path)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != payload:
            raise Refused("studio_vision_request_destination_conflict") from None


def canonical_png(path):
    """Keep the Blender pixel values and discard encoder metadata."""
    with Image.open(path, formats=["PNG"]) as frame:
        image = Image.frombytes("RGBA", frame.size, frame.convert("RGBA").tobytes())
    image.save(path, format="PNG")


def stateless_environment(scratch):
    env = os.environ.copy()
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
        env.pop(key, None)
    env.update(LC_ALL="C.UTF-8", LANG="C.UTF-8", TZ="UTC", PYTHONDONTWRITEBYTECODE="1",
               CUDA_VISIBLE_DEVICES="", NVIDIA_VISIBLE_DEVICES="none", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", XDG_CACHE_HOME=str(scratch), TMPDIR=str(scratch))
    return env


def blender(args, scratch, script, values, blend=None):
    command = [args.blender, "-b", "--factory-startup", "--disable-autoexec", "--threads", "1",
               "--python-exit-code", "2"]
    if blend is not None:
        command.append(str(blend))
    command += ["--python", str(HERE / script), "--", *map(str, values)]
    result = subprocess.run(command, env=stateless_environment(scratch), check=False,
                            stdout=sys.stderr, stderr=sys.stderr)
    if result.returncode:
        raise RuntimeError(f"{script} exited {result.returncode}")


def limits_match(row, field, *, lower=0, upper=math.inf):
    value = row[field]
    return type(value) in (float, int) and math.isfinite(value) and lower <= value <= upper


def measured_gate(metrics, readback, profile):
    views = metrics["views"]
    neutral = {f"{projection}_{view}_neutral" for projection in ("ortho", "shot") for view in measure.VIEWS}
    roster = neutral | {f"ortho_{view}_{light}" for view in measure.VIEWS[:3] for light in measure.LIGHTS[1:]}
    if views.keys() != roster:
        raise Refused("studio_view_roster")
    if any(not limits_match(row, "iou", lower=measure.IOU_MIN, upper=1) for row in views.values()):
        raise Refused("studio_silhouette_iou")
    features = metrics["front_landmark_regions"]
    if features.keys() != (FEATURES if profile == "panel-actor" else set()):
        raise Refused("studio_feature_roster")
    if any(not limits_match(row, "iou", lower=measure.FEATURE_MIN, upper=1) for row in features.values()):
        raise Refused("studio_feature_silhouette_iou")
    if profile == "panel-actor":
        streaks = metrics["grazing_torso_streaks"]
        local = metrics["local_quality"]
        if (streaks.keys() != measure.TORSO_WINDOWS.keys()
                or local.keys() != measure.REGIONS.keys()
                or any(row["lights"].keys() != measure.REGION_LIMITS[name].keys() for name, row in local.items())):
            raise Refused("studio_panel_region_roster")
        defects = measure.quality_failures(features, streaks) + measure.local_failures(local)
        if defects:
            raise Refused("studio_panel_quality:" + ",".join(defects))
    colors = ("delta_e76_mean", "delta_e76_p95")
    aggregate = metrics["delta_e76_aggregate"]
    if (any(not limits_match(row, key) for row in views.values() for key in colors)
            or any(not limits_match(aggregate, key) for key in ("mean", "p95"))):
        raise Refused("studio_rendered_color_invalid")
    mean, p95 = (math.fsum(views[name][key] for name in sorted(neutral)) / len(neutral) for key in colors)
    if mean > measure.COLOR_MEAN_MAX or p95 > measure.COLOR_P95_MAX:
        raise Refused("studio_rendered_color_delta")
    topology = metrics["topology"]
    triangles = readback["triangles"]
    if (type(triangles) is not int or not 0 < triangles <= 32_000
            or any(type(topology[key]) is not int or topology[key] != triangles for key in ("triangles", "welded_faces"))):
        raise Refused("studio_triangle_budget")
    if (any(type(topology[key]) is not int or topology[key] != 0 for key in TOPOLOGY_DEFECTS)
            or not limits_match(topology, "signed_volume_source_units_cubed", lower=0)
            or topology["signed_volume_source_units_cubed"] == 0
            or topology["component_vertices"] != readback["component_vertices"]
            or topology["welded_vertices"] != sum(readback["component_vertices"])):
        raise Refused("studio_topology_unqualified")
    coverage = readback["coverage"]
    if (not limits_match(coverage, "interior_coverage_after_margin", lower=.98, upper=1)
            or coverage["interior_opacity_transfer_witness_texels_after_margin"] /
            coverage["occupied_interior_texels"] < .98):
        raise Refused("studio_bake_coverage")
    return {"triangles": triangles, "min_view_iou": min(row["iou"] for row in views.values()),
            "mean_neutral_delta_e76": mean, "mean_neutral_p95_delta_e76": p95}


def verify_files(folder, files):
    for name, digest in files.items():
        path = folder / name
        if path.resolve() != path or not path.is_relative_to(folder) or not path.is_file() or sha(path) != digest:
            raise Refused("studio_bundle_artifact_changed:" + name)


def executable_pin(command):
    executable = shutil.which(command)
    if executable is None:
        raise ValueError("studio_blender_unavailable")
    return sha(Path(executable))


def implementation_pins():
    return {path.name: sha(path) for path in sorted(HERE.glob("*.py"))}


def read_bundle(folder, build_inputs=None):
    row = document(folder / "bundle.json")
    if row["schema"] != BUNDLE_SCHEMA or row["settings"]["profile"] not in PROFILES:
        raise ValueError("studio_bundle_schema")
    if build_inputs is not None and (row["inputs"], row["settings"], row["implementation_sha256"]) != build_inputs:
        raise Refused("studio_bundle_destination_conflict")
    names = set(ARTIFACTS) | {"readback.json"}
    required = ("base-color.png", "metallic-roughness.png", "normal.png", "evidence/bake.json",
                "evidence/bake-hits.npy", "evidence/bake-ray-direct.npy", "evidence/bake-ray-extended.npy")
    if not names.union(required) <= row["files"].keys():
        raise ValueError("studio_bundle_artifacts_missing")
    verify_files(folder, row["files"])
    readback = verify_asset(folder, require_channel_variation=row["settings"]["profile"] == "panel-actor")
    if readback != document(folder / "readback.json"):
        raise Refused("studio_bundle_readback_changed")
    measured = measured_gate(document(folder / "studio-metrics.json"), readback, row["settings"]["profile"])
    return row, measured


def camera_settings(path):
    data = document(path)
    camera = data["camera"]
    location, target = camera["location_m"], camera["target_m"]
    if any(not isinstance(point, list) or len(point) != 3 or
           any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
           for point in (location, target)):
        raise ValueError("studio_camera_vectors")
    if (location[:2] == target[:2] or not limits_match(camera, "sensor_width_mm", lower=1, upper=100)
            or not limits_match(data["robot_framing"], "reference_height_m", lower=1e-6)):
        raise ValueError("studio_camera_framing")
    return data


def build(args):
    folder = args.output.resolve()
    paths = {"source": args.source.resolve(strict=True), "geometry": args.geometry.resolve(strict=True),
             "camera": args.camera.resolve(strict=True)}
    if any(paths[key].suffix.lower() != ".glb" for key in ("source", "geometry")):
        raise ValueError("studio_input_requires_glb")
    if args.profile == "panel-actor" and args.render_size != 432:
        raise ValueError("studio_panel_profile_requires_432px")
    settings = {name: getattr(args, name) for name in ("profile", "depth", "cap", "atlas_size",
                "render_size", "samples", "extrusion", "ray", "margin", "smooth_factor", "smooth_iterations")}
    if (not 3 <= args.depth <= 10 or not 12 <= args.cap <= 32_000
            or not 32 <= args.render_size <= 2048 or not 1 <= args.samples <= 256
            or not 0 <= args.smooth_iterations <= 100 or not 0 <= args.smooth_factor <= 1
            or not 0 <= args.margin <= min(args.atlas_size // 4, 64)
            or not all(math.isfinite(x) and x > 0 for x in (args.extrusion, args.ray))):
        raise ValueError("studio_build_options_out_of_range")
    if args.profile == "panel-actor" and args.cap > 29_100:
        raise ValueError("studio_panel_profile_requires_tip_budget")

    pins = {key: sha(path) for key, path in paths.items()}
    # Pin the executable and implementation by content while keeping location out of the bundle.
    settings["blender_sha256"] = executable_pin(args.blender)
    implementation = implementation_pins()
    build_inputs = pins, settings, implementation
    if folder.exists():
        _, measured = read_bundle(folder, build_inputs)
        return measured | {"status": "PASS", "bundle_sha256": sha(folder / "bundle.json")}

    folder.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder.parent, prefix=".studio-build-") as temporary:
        root = Path(temporary)
        work = root / "bundle"
        work.mkdir()
        scratch = root / "engine"
        scratch.mkdir()
        snapshots = {}
        for key, path in paths.items():
            snapshots[key] = root / (key + path.suffix.lower())
            shutil.copyfile(path, snapshots[key])
            if sha(snapshots[key]) != pins[key]:
                raise Refused("studio_input_changed:" + key)
        camera_settings(snapshots["camera"])
        panel = args.profile == "panel-actor"
        refine = ["--tip-depth", 6, "--tip-cap", 1900, "--torso-iterations", 46] if panel else []
        remesh = root / "remesh"
        blender(args, scratch, "mesh.py", [args.depth, "--geometry", snapshots["geometry"], "--output", remesh,
                "--cap", args.cap, "--smooth-factor", args.smooth_factor,
                "--smooth-iterations", args.smooth_iterations, *refine])
        local = ["--local-projection"] if panel else []
        blender(args, scratch, "bake.py", ["--source", snapshots["source"], "--output", work, "--use-cage",
                "--size", args.atlas_size, "--extrusion", args.extrusion, "--ray", args.ray,
                "--margin", args.margin, *local], remesh / "remesh.blend")
        shutil.copyfile(remesh / "remesh.json", work / "evidence/remesh.json")
        renders = work / "rendered"
        blender(args, scratch, "render.py", [renders, "--source", snapshots["source"], "--asset", work / "studio.glb",
                "--camera", snapshots["camera"], "--size", args.render_size, "--samples", args.samples])
        for path in sorted(renders.glob("*.png")):
            canonical_png(path)
        metrics = measure.collect(renders, "studio", panel)
        metrics.update(topology=document(renders / "topology.json"), cameras=document(renders / "cameras.json"))
        if panel:
            metrics.update(grazing_torso_streaks=measure.grazing_streaks(renders, "studio"),
                           front_emblem=measure.emblem_colors(renders),
                           local_quality=measure.local_quality(renders, "studio"))
        readback = verify_asset(work, require_channel_variation=panel)
        measured = measured_gate(metrics, readback, args.profile)
        measure.sheet(renders, work / "contact-sheet.png")
        write(work / "studio-metrics.json", metrics)
        write(work / "readback.json", readback)
        files = {path.relative_to(work).as_posix(): sha(path) for path in sorted(work.rglob("*")) if path.is_file()}
        write(work / "bundle.json", {"schema": BUNDLE_SCHEMA, "inputs": pins, "settings": settings,
                                     "implementation_sha256": implementation, "files": files})
        try:
            work.rename(folder)
        except OSError:
            if not folder.exists():
                raise
            _, measured = read_bundle(folder, build_inputs)
    return measured | {"status": "PASS", "bundle_sha256": sha(folder / "bundle.json")}


def vision(args):
    folder = args.bundle.resolve(strict=True)
    if sha(folder / "bundle.json") != args.bundle_sha256:
        raise Refused("studio_bundle_digest_changed")
    row, measured = read_bundle(folder)
    sheet = folder / "contact-sheet.png"
    selected = "studio.glb"
    hashes = {"source": row["inputs"]["source"], "selected": row["files"][selected],
              "metrics": row["files"]["studio-metrics.json"], "contact_sheet": row["files"][sheet.name]}
    request = vision_review.make_request(args.run_id, sheet, hashes)
    if args.command == "vision-request":
        if args.request.resolve().is_relative_to(folder):
            raise ValueError("studio_vision_request_must_be_outside_bundle")
        write_request(args.request, request)
        return {"status": "REVIEW_REQUIRED", "request_sha256": sha(args.request)}
    request_sha = hashlib.sha256(encode(request).encode("utf-8")).hexdigest()
    if sha(args.request) != request_sha:
        raise Refused("studio_vision_request_changed")
    try:
        review = vision_review.verify_response(request, request_sha, sheet, document(args.response))
    except ValueError as error:
        raise Refused(str(error)) from error
    if review["vision"] == "REJECT":
        raise Refused("studio_vision_rejected", review)
    return measured | review | {"status": "PASS", "look_mode": "studio",
                                "qualification": "MEASURED_ONLY" if review["vision"] == "AUSENTE" else "MEASURED_AND_VISION",
                                "selected": {"path": selected, "sha256": hashes["selected"]}}


def pose_capture(args):
    destination = args.output.resolve()
    config = args.config.resolve(strict=True)
    blend = args.blend.resolve(strict=True)
    pins = {"scene": sha(blend), "config": sha(config), "blender": executable_pin(args.blender)}
    implementation = implementation_pins()

    def resume():
        receipt = document(destination / "capture.json")
        if (receipt["inputs"] != pins or receipt["implementation_sha256"] != implementation
                or not {"witnesses.json", "render-quality.json"} <= receipt["files"].keys()):
            raise Refused("studio_pose_output_conflict")
        verify_files(destination, receipt["files"])
        report = render_quality.evaluate_witnesses(document(destination / "witnesses.json"), destination)
        if report != document(destination / "render-quality.json"):
            raise Refused("studio_pose_report_changed")
        return report

    if destination.exists():
        report = resume()
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".studio-pose-") as scratch:
            root = Path(scratch)
            work = root / "capture"
            work.mkdir()
            scene, settings = root / "scene.blend", root / "config.json"
            shutil.copyfile(blend, scene)
            shutil.copyfile(config, settings)
            if sha(scene) != pins["scene"] or sha(settings) != pins["config"]:
                raise Refused("studio_pose_input_changed")
            blender(args, root, "pose_capture.py", ["--config", settings, "--output", work], scene)
            witness = work / "witnesses.json"
            witnesses = document(witness)
            pairs = [*witnesses["rest"].values(), *witnesses["posed"].values()]
            for pair in pairs:
                for ref in pair.values():
                    path = work / ref["path"]
                    canonical_png(path)
                    ref["sha256"] = sha(path)
            write(witness, witnesses)
            report = render_quality.evaluate_witnesses(witnesses, work)
            write(work / "render-quality.json", report)
            files = {path.name: sha(path) for path in sorted(work.iterdir())}
            write(work / "capture.json", {"inputs": pins, "implementation_sha256": implementation, "files": files})
            try:
                work.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
                report = resume()
    if report["reasons"]:
        raise Refused(report["reasons"][0], report)
    return report


def pose_gate(args):
    report = render_quality.evaluate_witnesses(document(args.config), args.config.resolve().parent)
    if report["reasons"]:
        raise Refused(report["reasons"][0], report)
    return report


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        print(encode({"status": "ERROR", "reason": message,
                      "retry_when": "Supply the required CLI arguments",
                      "next_command": "python3 -B look.py --help"}), end="")
        self.exit(2)


def main():
    parser = CommandParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("build", help="Produce and admit an offline studio LOOK bundle")
    for name in ("source", "geometry", "camera", "output"):
        stage.add_argument("--" + name, type=Path, required=True)
    stage.add_argument("--profile", choices=PROFILES, required=True)
    stage.add_argument("--blender", default="blender")
    stage.add_argument("--depth", type=int, default=8)
    stage.add_argument("--cap", type=int, default=29_100)
    stage.add_argument("--atlas-size", type=int, choices=ATLAS_SIZES, default=4096)
    stage.add_argument("--render-size", type=int, default=432)
    stage.add_argument("--samples", type=int, default=12)
    stage.add_argument("--extrusion", type=float, default=.01)
    stage.add_argument("--ray", type=float, default=.025)
    stage.add_argument("--margin", type=int, default=24)
    stage.add_argument("--smooth-factor", type=float, default=.5)
    stage.add_argument("--smooth-iterations", type=int, default=6)
    stage.set_defaults(action=build)
    for command in ("vision-request", "vision-gate"):
        review = commands.add_parser(command)
        review.add_argument("--bundle", type=Path, required=True)
        review.add_argument("--bundle-sha256", required=True)
        review.add_argument("--run-id", required=True)
        review.add_argument("--request", type=Path, required=True)
        if command == "vision-gate":
            review.add_argument("--response", type=Path, required=True)
        review.set_defaults(action=vision)
    capture = commands.add_parser("pose-capture", help="Capture and gate configured Blender poses on CPU")
    for name in ("blend", "config", "output"):
        capture.add_argument("--" + name, type=Path, required=True)
    capture.add_argument("--blender", default="blender")
    capture.set_defaults(action=pose_capture)
    pose = commands.add_parser("pose-gate", help="Check registered rest images and posed limb/shading witnesses")
    pose.add_argument("--config", type=Path, required=True)
    pose.set_defaults(action=pose_gate)
    args = parser.parse_args()
    try:
        print(encode(args.action(args)), end="")
        return 0
    except Refused as error:
        rc, status = 1, "REJECT"
        reason, *details = error.args
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, IndexError, struct.error) as error:
        rc, status = 2, "ERROR"
        reason, details = str(error), []
    retry = {"build": "Recheck the input GLBs and camera, then repeat the build",
             "vision-request": "Use a verified bundle and a fresh request destination",
             "vision-gate": "Obtain a matching independent response for an admitted bundle",
             "pose-capture": "Use a registered Blender scene and a fresh output directory",
             "pose-gate": "Provide complete same-camera witness data for all configured frames"}[args.command]
    print(encode({"status": status, "reason": reason, "retry_when": retry,
                  "next_command": f"python3 -B look.py {args.command} --help",
                  **({"report": details[0]} if details else {})}), end="")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
