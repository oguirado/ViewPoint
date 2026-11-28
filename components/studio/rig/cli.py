#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Studio rig preparation, binding, transfer, and CPU validation."""
import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory

import numpy as np

from bindfix import Refusal as BindRefusal, recompute_inverse_binds
from diagnostics import original_trial, prepare_variants, report_trials, verify_failed_readback, verify_readback
from pre_rig import normalise
from rigid_distance import distance_bounds
from rigid_skin import RIGIDITY_M
from rig_common import ENV, Hold, document, need, pin, write, write_bytes
from template_rig import qualify
from transfer import transfer


def refusal(reason, *, retry="Correct the reported precondition and rerun.", argv=None):
    print(json.dumps({"reason": str(reason), "retry_when": retry,
                      "next_command": [sys.argv[0], *sys.argv[1:]] if argv is None else argv}, sort_keys=True))


class Parser(argparse.ArgumentParser):
    def error(self, message):
        refusal("usage:" + message, argv=[sys.argv[0], "--help"])
        raise SystemExit(2)


def parser():
    root = Parser(description=__doc__)
    subs = root.add_subparsers(dest="command", required=True)

    def command(name, paths):
        sub = subs.add_parser(name)
        for flag in paths.split():
            sub.add_argument("--" + flag, type=Path, required=True)
        return sub

    command("weld", "input out")
    command("bindfix", "input out").add_argument("--timeout", type=int, default=60)
    command("rigidity", "poses out").add_argument("--tolerance-m", type=float, default=RIGIDITY_M)
    template = command("template", "selected template joint-map out-dir")
    template.add_argument("--method", choices=("auto", "rigid"), required=True)
    template.add_argument("--bind-tolerance-m", type=float, default=1e-6)
    template.add_argument("--blender", default="blender")
    original = command("unirig-plan", "original out")
    original.add_argument("--seed", type=int, required=True)
    diagnostic = command("diagnose-prepare", "original selected control out-dir")
    diagnostic.add_argument("--seed", type=int, required=True)
    diagnostic.add_argument("--simplification-limit", type=int, default=50_000)
    diagnostic.add_argument("--v1-min", type=int, default=60_000)
    diagnostic.add_argument("--v2-min", type=int, default=125_000)
    diagnostic.add_argument("--blender", default="blender")
    command("diagnose-report", "plan observations out")
    unirig = command("unirig-transfer", "original source inference selected joint-map out-dir")
    unirig.add_argument("--height-m", type=float, required=True)
    unirig.add_argument("--blender", default="blender")
    return root


def safe_output(path, inputs):
    path = path.resolve()
    need(path not in {item.resolve(strict=True) for item in inputs}, "output_aliases_input")
    return path


def positive_finite(number):
    need(math.isfinite(number) and number > 0, "invalid_positive_tolerance")
    return number


def check_rigidity(path, tolerance):
    with np.load(path, allow_pickle=False) as data:
        rest, parts = data["rest"], data["parts"]
        names = sorted(set(data.files) - {"rest", "parts"})
        need(rest.ndim == 2 and rest.shape[1:] == (3,) and len(rest) > 0 and np.isfinite(rest).all()
             and parts.shape == (len(rest),) and parts.dtype.kind in "iu" and bool(names),
             "rigidity_input_shape")
        masks = {str(part): parts == part for part in np.unique(parts)}
        rows = {}
        for name in names:
            pose = data[name]
            need(pose.shape == rest.shape and np.isfinite(pose).all(), "rigidity_input_shape")
            rows[name] = distance_bounds(rest, pose, masks)
    failures = [{"pose": pose, "part": part, "bound_m": value}
                for pose, bounds in rows.items() for part, value in bounds.items() if value > tolerance]
    return {"status": "HOLD" if failures else "PASS", "tolerance_m": tolerance,
            "poses": rows, "failures": failures}


def run(args):
    operation = args.command
    if operation in ("weld", "bindfix"):
        out = safe_output(args.out, [args.input])
        data = args.input.read_bytes()
        raw = normalise(data) if operation == "weld" else recompute_inverse_binds(data, timeout_s=args.timeout)
        write_bytes(out, raw)
        return {"status": "PASS", "output": pin(out)}
    if operation == "rigidity":
        out = safe_output(args.out, [args.poses])
        report = check_rigidity(args.poses, positive_finite(args.tolerance_m))
        write(out, report)
        if report["failures"]:
            raise Hold("rigidity_exceeded")
        return report
    if operation == "unirig-plan":
        out = safe_output(args.out, [args.original])
        report = original_trial(args.original, args.seed)
        write(out, report)
        return report
    if operation == "diagnose-report":
        trials = document(args.observations)
        plan = document(args.plan)
        inputs = [args.plan, args.observations]
        observations = []
        # Paths in the observation file are relative to its own directory.
        for trial in trials:
            base = args.observations.parent
            source = base / trial["input_path"]
            if "failed_attempt_path" in trial:
                attempt = base / trial["failed_attempt_path"]
                inputs.extend([source, *(attempt / name for name in ("result.json", "host-launch.json",
                                                                    "admission.json", "scope-exit.json"))])
                observation = verify_failed_readback(source, attempt)
            else:
                paths = [source, base / trial["output_path"], base / trial["receipt_path"]]
                inputs.extend(paths)
                observation = verify_readback(*paths)
                observation.update(variant=trial["variant"], repeat=trial["repeat"], seed=trial["seed"])
            observations.append(observation)
        out = safe_output(args.out, inputs)
        report = report_trials(plan, observations)
        report["observations"] = sorted(observations, key=lambda row: (row["variant"], row["repeat"]))
        write(out, report)
        if report["status"] == "HOLD":
            raise Hold(report["reason"])
        return report
    if operation == "diagnose-prepare":
        inputs = [args.original.resolve(strict=True), args.selected.resolve(strict=True),
                  args.control.resolve(strict=True)]
        out = args.out_dir.resolve()
        for name in ("welded.glb", "V1.glb", "V2.glb", "trials.json"):
            safe_output(out / name, inputs)
        out.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=out) as scratch:
            work = Path(scratch)
            rows = prepare_variants(*inputs, work, seed=args.seed, limit=args.simplification_limit,
                                    v1_min=args.v1_min, v2_min=args.v2_min, blender=args.blender)
            for name in ("welded.glb", "V1.glb", "V2.glb"):
                write_bytes(out / name, (work / name).read_bytes())
        write(out / "trials.json", rows)
        return {"status": "PREPARED", "variants": rows}
    if operation == "template":
        inputs = [args.selected, args.template, args.joint_map]
        out = args.out_dir.resolve()
        artifact = safe_output(out / "rig.glb", inputs)
        report_path = safe_output(out / "report.json", inputs)
        out.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=out) as scratch:
            rig, report = qualify(*inputs, Path(scratch), method=args.method, blender=args.blender,
                                  tolerance=positive_finite(args.bind_tolerance_m))
            write_bytes(artifact, rig.read_bytes())
        write(report_path, report)
        if report["status"] == "HOLD":
            raise Hold(report["pose_refusal"])
        return report
    inputs = [args.original, args.source, args.inference, args.selected, args.joint_map]
    out = args.out_dir.resolve()
    artifact = safe_output(out / "rig.glb", inputs)
    report_path = safe_output(out / "report.json", inputs)
    map_path = safe_output(out / "joint-map.json", inputs)
    receipt = verify_readback(args.original, args.source, args.inference)
    source = args.source.read_bytes()
    need(sha256(source).hexdigest() == receipt["output"]["sha256"], "unirig_input_changed")
    selected, mapping, height = args.selected.read_bytes(), document(args.joint_map), positive_finite(args.height_m)
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out) as scratch:
        work = Path(scratch)
        result, report = transfer(source, selected, mapping, height, receipt, work, blender=args.blender)
        write_bytes(artifact, result.read_bytes())
        write_bytes(map_path, (work / "JOINT_MAP.json").read_bytes())
    write(report_path, report)
    if report["status"] == "HOLD":
        raise Hold(report["pose_refusal"])
    return report


def main():
    os.environ.update(ENV)
    try:
        print(json.dumps(run(parser().parse_args()), sort_keys=True, allow_nan=False))
    except BindRefusal as error:
        refusal(error.code)
        return 1 if error.rc == 5 else 2
    except Hold as error:
        refusal(error)
        return 1
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError, struct.error,
            np.linalg.LinAlgError, subprocess.TimeoutExpired) as error:
        refusal(f"harness:{type(error).__name__}:{error}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
