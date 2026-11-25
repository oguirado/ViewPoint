# SPDX-License-Identifier: AGPL-3.0-only
"""Run a read-only prediction or full-read arm on an offline structured fixture.

This entrypoint records inspection only. It has no capsule, command or lease adapter.
"""

import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
from time import monotonic_ns

from .mesh_readback import (
    Refused, differences, identifier, read, require, request_and_receipt,
    shape, validate_identity,
)


def predict(directory, current_identity=None):
    coarse, coarse_pin = read(directory, "coarse.json")
    shape(coarse, ("identity", "patches"), "COARSE_SCHEMA")
    validate_identity(coarse["identity"])
    require(type(coarse["patches"]) is dict, "COARSE_SCHEMA")
    scores = {}
    for patch, row in coarse["patches"].items():
        shape(row, ("prior_mm", "current_mm", "transform_changed"), "COARSE_SCHEMA")
        require(identifier(patch) and type(row["transform_changed"]) is bool
                and all(type(row[key]) is int and row[key] >= 0 for key in ("prior_mm", "current_mm")),
                "COARSE_SCHEMA")
        closing = row["prior_mm"] - row["current_mm"]
        if closing > 0 or row["transform_changed"]:
            scores[patch] = (-int(row["transform_changed"]), -max(closing, 0), patch)
    ranked = sorted(scores, key=scores.__getitem__)
    crop = None
    stale_source = differences(coarse["identity"], current_identity) if current_identity else []
    if ranked and not stale_source and (directory / "prefetch-source.json").exists():
        crop, _ = read(directory, "prefetch-source.json")
        shape(crop, ("identity", "patch_id", "objects"), "CROP_SCHEMA")
        validate_identity(crop["identity"])
        require(crop["identity"] == coarse["identity"] and crop["patch_id"] == ranked[0],
                "PREFETCH_SOURCE_MISMATCH")
    return ranked, crop, coarse_pin, stale_source


def independent_readback(directory, receipt_pin, hint):
    cmd = [sys.executable, "-EsB", "-m", "perception.mesh_readback", str(directory), receipt_pin]
    env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8", TZ="UTC")
    with subprocess.Popen(cmd, cwd=Path(__file__).resolve().parents[1], env=env,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        try:
            stdout, stderr = child.communicate(json.dumps(hint).encode(), timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise
    try:
        os.waitpid(child.pid, os.WNOHANG)
    except ChildProcessError:
        leaked = False
    else:
        raise RuntimeError("readback child was still waitable after communicate")
    if child.returncode not in (0, 1) or stderr:
        raise RuntimeError(f"independent readback failed ({child.returncode}): {stderr.decode(errors='replace')}")
    report = json.loads(stdout)
    if (report["reader_pid"] != child.pid
            or report["status"] != ("reopened" if child.returncode == 0 else "refused")):
        raise RuntimeError("readback child PID or verdict mismatch")
    return report, leaked, cmd


def prepare_speculatively(directory, arm):
    if arm == "predict":
        started = monotonic_ns()
        state = predict(directory)
        return state, monotonic_ns() - started
    return None, 0


def inspection_plan(directory, arm, identity, speculation):
    state, prepare_ns = speculation
    ranked, prepared, coarse_pin, skipped = [], None, None, []
    if arm == "reactive":
        started = monotonic_ns()
        state = predict(directory, identity)
        prepare_ns = monotonic_ns() - started
    if state is not None:
        ranked, prepared, coarse_pin, skipped = state
    invalidated = differences(prepared["identity"], identity) if prepared is not None else []
    crop = None if invalidated else prepared
    hint = {"predict": arm != "full", "ranked_patch": ranked[0] if ranked else None, "crop": crop}
    preparation = {
        "coarse_sha256": coarse_pin,
        "ranking": ranked,
        "prepared": prepared is not None,
        "invalidated": invalidated,
        "skipped_stale_preparation": skipped,
        "prepare_ns": prepare_ns,
    }
    return hint, preparation


def preparation_report(preparation, observed):
    return {
        **preparation,
        "wasted_preparation": preparation["prepared"] and (
            bool(preparation["invalidated"]) or observed["status"] != "reopened" or observed["fallback"]),
    }


def run(directory, arm, expected_receipt_pin=None):
    episode_started = monotonic_ns()
    speculation = prepare_speculatively(directory, arm)
    request_started = monotonic_ns()
    request, receipt, receipt_pin = request_and_receipt(directory)
    require(expected_receipt_pin is None or expected_receipt_pin == receipt_pin,
            "SUPERVISED_RECEIPT_MISMATCH")
    receipt_verified = monotonic_ns()
    hint, preparation = inspection_plan(directory, arm, request["identity"], speculation)
    observed, leaked, reader_argv = independent_readback(directory, receipt_pin, hint)
    finished = monotonic_ns()
    if observed["status"] == "reopened":
        require(observed["identity"] == request["identity"] and observed["receipt_sha256"] == receipt_pin
                and observed["artifact_sha256"] == receipt["artifact_sha256"], "READBACK_BINDING_MISMATCH")
    return {
        "arm": arm,
        "request_identity": request["identity"],
        "producer_receipt_sha256": receipt_pin,
        **preparation_report(preparation, observed),
        "readback": observed,
        "reader_argv": reader_argv,
        "readback_child_leaked": leaked,
        "reader_peak_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        "entry_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "request_to_receipt_ns": receipt_verified - request_started,
        "receipt_to_readback_ns": finished - receipt_verified,
        "request_to_readback_ns": finished - request_started,
        "episode_ns": finished - episode_started,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--arm", choices=("full", "predict", "reactive"), required=True)
    parser.add_argument("--receipt-sha256", help="expected producer receipt digest")
    args = parser.parse_args()
    try:
        result = run(args.directory.resolve(strict=True), args.arm, args.receipt_sha256)
    except Refused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return int(result["readback"]["status"] != "reopened")


if __name__ == "__main__":
    sys.exit(main())
