#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Offline CLI for studio motion, scenes, measurements and scoped stages."""
import argparse
from functools import partial
import os
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from bvh import Refusal as BvhRefusal
from evidence import Rejected, atomic, checked_json, demand, digest, encode, pin, read_pin
import runner
import scope

HERE = Path(__file__).resolve().parent
CAMERAS = ("wide", "CAM_M", "CAM_A1", "CAM_A2")
MODES = ("keyed", "mocap")
ARTIFACT_SCHEMA = "viewpoint.motion-scene.artifacts/v1"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    app = Parser(description=__doc__)
    cmds = app.add_subparsers(dest="command", required=True)

    def output(cmd, *, blender=False):
        cmd.add_argument("--output", type=Path, required=True)
        if blender:
            cmd.add_argument("--blender", default="blender")
        return cmd

    def pinned_input(cmd, name, *, required=True):
        cmd.add_argument("--" + name, type=Path, required=required)
        cmd.add_argument("--" + name + "-sha256", required=required)

    layout = output(cmds.add_parser("layout"), blender=True)
    layout.add_argument("--camera", choices=CAMERAS, default="wide")
    clay = output(cmds.add_parser("clay"), blender=True)
    clay.add_argument("--mode", choices=("blocking", "final", "miss"), required=True)
    clay.add_argument("--camera", choices=CAMERAS[1:], default="CAM_M")
    clay.add_argument("--preview-frame", type=int)
    clay.add_argument("--shadow-frame", type=int)
    clay.add_argument("--movie", action="store_true", help="render the complete CPU H.264 master")
    anim = output(cmds.add_parser("animate"))
    anim.add_argument("--mode", choices=MODES, default="keyed")
    pinned_input(anim, "rig")
    pinned_input(anim, "registration")
    pinned_input(anim, "walk", required=False)
    pinned_input(anim, "jab", required=False)
    anim.add_argument("--walk-range", type=int, nargs=2, metavar=("START", "STOP"))
    anim.add_argument("--jab-range", type=int, nargs=3, metavar=("START", "PEAK", "STOP"))
    anim.add_argument("--plant-offset", type=float, nargs=3, default=(.17, .04, 0.))

    compose = output(cmds.add_parser("compose"), blender=True)
    compose.add_argument("--mode", choices=MODES, default="keyed")
    compose.add_argument("--camera", choices=(*CAMERAS, "authored"), default="authored")
    for name in ("animation", "registration", "motion-report"):
        pinned_input(compose, name)

    gate = cmds.add_parser("gate")
    gate.add_argument("--mode", choices=MODES, default="keyed")
    for name in ("registration", "motion-report", "scene-report", "animation", "scene"):
        pinned_input(gate, name)

    run = output(cmds.add_parser("run-morning"))
    run.add_argument("--plan", required=True, type=Path)
    run.add_argument("--evidence", action="append", default=[], metavar="STAGE=MANIFEST")
    return app


def external(path):
    demand(path is not None and not path.is_symlink(), "missing_or_symlink_input")
    path = path.absolute()
    demand(path.resolve() == path and path.is_file(), "missing_or_symlink_input")
    return path


def input_file(args, name):
    path = external(getattr(args, name))
    budget = 512 * 2**20 if name in ("rig", "animation", "scene") else 16 * 2**20
    demand(path.stat().st_size <= budget, "input_byte_budget:" + name)
    data = path.read_bytes()
    sha = digest(data)
    demand(sha == getattr(args, name + "_sha256"), "input_digest_mismatch:" + name)
    return path, data, sha


def profile(mode):
    import animation_polish as polish
    import keyed_motion
    if mode == "keyed":
        return keyed_motion.MASTER, keyed_motion.ARM, 172
    import retarget
    return next(iter(retarget.ACTIONS)), polish.ARM, polish.HOLD_END


def animate(args, path):
    import animation_polish as polish
    import bvh
    import keyed_motion
    import rest_adapter
    import rest_projection
    import retarget

    _, raw, rig_sha = input_file(args, "rig")
    _, registration, registration_sha = input_file(args, "registration")
    set_doc = checked_json(registration)
    polish.verify_set(set_doc)
    mapping = retarget.Retarget(set_doc, plant_offset=args.plant_offset)
    inputs = {"rig": rig_sha, "registration": registration_sha}
    _, arm, hold = profile(args.mode)
    selection = {}
    clip_values = (args.walk, args.walk_sha256, args.walk_range, args.jab, args.jab_sha256, args.jab_range)
    if args.mode == "keyed":
        demand(not any(clip_values), "clips_require_mocap_mode")
        keyed_profile = SimpleNamespace(ANTICIPATE=polish.ANTICIPATE, RECOVER=polish.RECOVER,
                                       RELEASE=polish.RELEASE, sole_specs=polish.sole_specs,
                                       cap_fraction=partial(polish.cap_fraction, hold_end=hold))
        state = keyed_motion.compute(mapping, bvh, rest_projection, rest_adapter, keyed_profile, raw)
        actions = keyed_motion.ACTIONS
    else:
        demand(all(value is not None for value in clip_values), "walk_and_jab_inputs_required")
        start, peak, stop = args.jab_range
        demand(start < peak < stop, "invalid_jab_interval")
        _, walk, inputs["walk"] = input_file(args, "walk")
        _, jab, inputs["jab"] = input_file(args, "jab")
        clips = {retarget.WALK: retarget.load_clip(walk, *args.walk_range),
                 retarget.JAB: retarget.load_clip(jab, start, stop)}
        import rigid_press

        base = mapping.compute(bvh, rest_projection, rest_adapter, raw, clips, (start, peak, stop))
        stable = rigid_press.register_floor(mapping, polish, raw, polish.correct(mapping, raw, base))
        state = rigid_press.aim_press(mapping, polish, raw, base, stable)
        selection = {"walk_range": args.walk_range, "jab_range": args.jab_range,
                     "plant_offset_m": list(args.plant_offset)}
        actions = retarget.ACTIONS

    glb = retarget.export_gltf(raw, state, actions)
    replay = retarget.read_animation(raw, glb, state, actions)
    metrics = mapping.evaluate(replay, state)
    retarget.gate(metrics)
    motion = polish.measure_motion(mapping, raw, replay, arm=arm)
    reasons = polish.motion_rejections(motion)
    demand(not reasons, ",".join(reasons))
    motion.update(mode=args.mode, status="PASS", motion_sha256=digest(glb), registration_sha256=registration_sha)
    files = {"retargeted.glb": glb, "projected.bvh": state["projected"], "joint-map.json": encode(state["mapping"]),
             "motion-report.json": encode(motion), "retarget-report.json": encode(metrics)}
    for name, data in files.items():
        atomic(path / name, data)
    return {"inputs": inputs, "mode": args.mode, "selection": selection}


def gate(args):
    import animation_polish as polish

    _, raw_set, registration_sha = input_file(args, "registration")
    _, raw_motion, motion_sha = input_file(args, "motion_report")
    _, raw_scene, _ = input_file(args, "scene_report")
    _, _, animation_sha = input_file(args, "animation")
    _, _, blend_sha = input_file(args, "scene")
    scene, motion = checked_json(raw_scene), checked_json(raw_motion)
    demand(motion["mode"] == args.mode and motion["registration_sha256"] == registration_sha
           and motion["status"] == "PASS" and scene["motion_report_sha256"] == motion_sha
           and scene["animation_sha256"] == motion["motion_sha256"] == animation_sha
           and scene["blend_sha256"] == blend_sha, "polish_evidence_binding")
    _, arm, hold = profile(args.mode)
    result = polish.assess_interaction(checked_json(raw_set), motion, scene["samples"],
                                       scene["sightlines"], scene["glyph_visible"], arm=arm, hold_end=hold)
    demand(result["status"] == "PASS", ",".join(result["reasons"]))
    return result


def blender(args, path):
    exe = args.blender
    demand(Path(exe).name != "", "missing_blender")
    job = {"operation": args.command, "camera": args.camera, "output": str(path)}
    if args.command == "clay":
        for frame in (args.preview_frame, args.shadow_frame):
            demand(frame is None or 1 <= frame <= 288, "invalid_render_frame")
        demand(not args.movie or args.mode != "blocking", "movie_requires_curves")
        job.update(mode=args.mode, preview_frame=args.preview_frame, shadow_frame=args.shadow_frame,
                   movie=args.movie)
    if args.command == "compose":
        for key in ("animation", "registration", "motion_report"):
            source, _, sha = input_file(args, key)
            job[key] = {"path": str(source), "sha256": sha}
        master, arm, hold = profile(args.mode)
        job.update(mode=args.mode, master=master, arm=list(arm), hold=hold)
    with TemporaryDirectory(dir=path) as runtime:
        runtime = Path(runtime)
        job_file = runtime / "job.json"
        job_file.write_bytes(encode(job))
        env = scope.environment() | {"TMPDIR": str(runtime),
                                     "BLENDER_USER_CONFIG": str(runtime / "config"),
                                     "BLENDER_USER_SCRIPTS": str(runtime / "scripts"),
                                     "BLENDER_USER_DATAFILES": str(runtime / "datafiles")}
        argv = [exe, "-b", "--factory-startup", "--disable-autoexec", "-noaudio", "-t", "4",
                "--python-exit-code", "2", "--python", str(HERE / "blender_worker.py"), "--", str(job_file)]
        log = runtime / "blender.log"
        with log.open("wb") as output:
            done = subprocess.run(argv, env=env, cwd=path, stdin=subprocess.DEVNULL, stdout=output,
                                  stderr=subprocess.STDOUT, check=False)
        refusal = runtime / "refusal.json"
        if refusal.exists():
            detail = checked_json(refusal.read_bytes())
            raise Rejected(detail["reason"], detail["retry_when"], detail["next_command"])
        if done.returncode:
            with log.open("rb") as transcript:
                transcript.seek(max(0, log.stat().st_size - 8192))
                sys.stderr.write(transcript.read().decode(errors="replace"))
            raise RuntimeError(f"blender_failed:{done.returncode}")
        result = checked_json((runtime / "result.json").read_bytes())
    return result


def artifact_operation(args):
    root = args.output.absolute()
    demand(root.resolve() == root, "symlink_refused")
    root.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".motion-scene-", dir=root.parent) as temporary:
        stage = Path(temporary)
        details = animate(args, stage) if args.command == "animate" else blender(args, stage)
        assets = sorted(stage.iterdir())
        demand(bool(assets) and all(item.is_file() and not item.is_symlink() for item in assets),
               "empty_artifact_bundle")
        receipt = {"schema": ARTIFACT_SCHEMA, "status": "PASS", "operation": args.command,
                   "details": details, "artifacts": [pin(stage, item) for item in assets]}
        atomic(stage / "receipt.json", encode(receipt))
        if root.is_dir():
            demand((root / "receipt.json").is_file() and not (root / "receipt.json").is_symlink(),
                   "output_directory_occupied")
            old = checked_json((root / "receipt.json").read_bytes())
            same = (old["schema"], old["status"], old["operation"], old["details"]) == (
                ARTIFACT_SCHEMA, "PASS", args.command, details)
            demand(same and {name.name for name in root.iterdir()} ==
                   {item["path"] for item in old["artifacts"]} | {"receipt.json"}, "output_directory_occupied")
            for item in old["artifacts"]:
                read_pin(root, item)
            if args.command == "animate":
                demand(old == receipt, "nonreproducible_animation")
            return old
        stage.rename(root)
    return receipt


def plan_argv(args):
    supplied = {}
    for item in args.evidence:
        name, sep, path = item.partition("=")
        demand(sep and name not in supplied, "invalid_evidence_argument")
        supplied[name] = external(Path(path))
    return runner.run(external(args.plan), args.output.absolute(), supplied)


def scoped(args, original):
    quiet = None
    if args.command == "run-morning":
        plan = runner.parse_plan(external(args.plan).read_bytes())
        quiet = plan.get("quiet_hours")
    inherited = os.environ.get("MOTION_SCENE_SCOPED_NICE")
    if inherited is not None:
        scope.effective(int(inherited))
        scope.checkpoint(quiet)
        result = plan_argv(args) if args.command == "run-morning" else artifact_operation(args)
        print(encode(result).decode(), end="")
        return 0
    admission = scope.admission(quiet)
    print(encode({"scope_admission": admission}).decode(), end="", file=sys.stderr)
    env = scope.environment() | {"MOTION_SCENE_SCOPED_NICE": str(os.nice(0))}
    argv = scope.command([sys.executable, "-B", str(Path(__file__).resolve()), *original])
    result = subprocess.run(argv, env=env, check=False)
    return result.returncode if result.returncode >= 0 else 2


def main(original):
    try:
        args = parser().parse_args(original)
        if args.command == "gate":
            print(encode(gate(args)).decode(), end="")
            return 0
        return scoped(args, original)
    except (Rejected, BlockingIOError) as error:
        if isinstance(error, BlockingIOError):
            error = Rejected("run_locked", "the current run releases its lock")
        print(encode(error.record([Path(__file__).name, *original])).decode(), end="")
        return 1
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, IndexError, struct.error) as error:
        print(encode(Rejected(str(error)).record([Path(__file__).name, *original])).decode(), end="")
        return 2
    except BvhRefusal as error:
        print(encode(Rejected(error.reason).record([Path(__file__).name, *original])).decode(), end="")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
