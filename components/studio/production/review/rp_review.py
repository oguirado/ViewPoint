#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Entry point of the pinned review runner and blinded vision panel.

Exit 0  accepted or verified
Exit 1  the protocol refused (one JSON line: reason, retry_when, next_command)
Exit 2  the check could not run as specified
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rpr_common as C  # noqa: E402
import rpr_host as H  # noqa: E402
import rpr_runner as R  # noqa: E402
import rpr_panel as P  # noqa: E402
import rpr_receipts as X  # noqa: E402

HOST_VERBS = ("containment", "admission", "checkpoint")


def emit(data):
    print(json.dumps(data, sort_keys=True, ensure_ascii=False))


def runner_commands(commands):
    declared = commands.add_parser("declared", help="derive the contact plan from pinned take records")
    declared.add_argument("--movie-sha", required=True)
    declared.add_argument("--set", type=Path)
    declared.add_argument("--motion-report", type=Path)
    declared.add_argument("--contacts", type=Path)
    declared.add_argument("--out", type=Path, required=True)

    build = commands.add_parser("build", help="stage a movie by digest and write its sample pack")
    build.add_argument("--movie", type=Path, required=True)
    build.add_argument("--sha256", required=True)
    build.add_argument("--declared", type=Path, required=True)
    build.add_argument("--pack", type=Path, required=True)

    verify = commands.add_parser("verify", help="refuse a pack that disagrees with its pinned movie")
    verify.add_argument("--pack", type=Path, required=True)
    verify.add_argument("--movie", type=Path)
    verify.add_argument("--sha256")
    verify.add_argument("--no-pixels", action="store_true")

    replay = commands.add_parser("replay", help="rebuild the manifest and require identical bytes")
    replay.add_argument("--pack", type=Path, required=True)
    replay.add_argument("--record", type=Path)

    restore = commands.add_parser("restore", help="rebuild from the pinned movie and put back drifted pack files")
    restore.add_argument("--pack", type=Path, required=True)

    host = commands.add_parser("containment", help="read back the scope, affinity and niceness this process runs under")
    host.add_argument("--require", action="store_true", help="exit 1 unless the lane limits hold")
    commands.add_parser("admission", help="RAM admission figure for one more 8 GiB scope; exit 1 on hold")
    commands.add_parser("checkpoint", help="hold while MemAvailable is under 20 GiB")

    plan = commands.add_parser("plan", help="print the sampling plan and blind segments of a pack")
    plan.add_argument("--pack", type=Path, required=True)

    identifier = commands.add_parser("pack-id", help="print the opaque pack id of a movie digest")
    identifier.add_argument("--sha256", required=True)


def run_declared(args):
    if args.contacts:
        if args.set or args.motion_report:
            raise C.Harness("--contacts cannot be combined with --set or --motion-report")
        plan = R.declared_from_contacts(args.contacts, args.movie_sha)
    elif args.set and args.motion_report:
        plan = R.declared_from_sources(args.set, args.motion_report, args.movie_sha)
    else:
        raise C.Harness("declared needs --contacts, or both --set and --motion-report")
    R.validate_declared(plan, args.movie_sha)
    C.write_json(args.out, plan)
    return {"status": "DECLARED", "out": str(args.out), "sha256": C.sha_file(args.out),
            "hand_button": plan["hand_button"], "foot_plant_count": len(plan["foot_plant"]),
            "phases": sorted(plan["phases"])}


def run_plan(args):
    manifest, manifest_sha = R.load_manifest(args.pack)
    protocol = C.load_protocol()
    return {"status": "PLANNED", "pack": str(args.pack), "movie_sha256": manifest["movie"]["sha256"],
            "manifest_sha256": manifest_sha, "catalog_sha256": manifest["catalog_sha256"],
            "sampling_rule": R.sampling_rule(manifest["sampling"]), "sample_count": manifest["sample_count"],
            "consecutive_ranges": manifest["consecutive_ranges"], "camera": manifest["declared"].get("camera"),
            "windows": manifest["windows"], "segments": P.plan_segments(manifest, protocol)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="verb", required=True)
    runner_commands(commands)
    parser.add_argument("--protocol", type=Path, help="protocol JSON with pinned auxiliary files alongside it")
    parser.add_argument("--seat-registry", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--observer", type=Path, help="path to an executable observer adapter")
    P.panel_commands(commands)
    args = parser.parse_args(argv)
    try:
        if args.protocol:
            C.PROTOCOL_FILE = args.protocol.resolve(strict=True)
            C.PROTOCOL_DIR = C.PROTOCOL_FILE.parent
        X.SEATS = args.seat_registry.resolve(strict=True) if args.seat_registry else None
        X.LEDGER = args.ledger.resolve(strict=True) if args.ledger else None
        P.OBSERVER = args.observer.resolve(strict=True) if args.observer else None
        if args.verb == "declared":
            emit(run_declared(args))
        elif args.verb == "build":
            emit(R.build(args.movie, args.sha256, C.read_json(args.declared), args.pack))
        elif args.verb == "verify":
            emit(R.verify(args.pack, args.movie, args.sha256, pixels=not args.no_pixels))
        elif args.verb == "replay":
            emit(R.replay(args.pack, args.record))
        elif args.verb == "restore":
            emit(R.restore(args.pack))
        elif args.verb == "plan":
            emit(run_plan(args))
        elif args.verb == "pack-id":
            emit({"pack_id": R.pack_id_for(args.sha256)})
        elif args.verb in HOST_VERBS:
            data, rc = H.run(args.verb, getattr(args, "require", False))
            emit(data)
            return rc
        else:
            data, rc = P.dispatch(args)
            emit(data)
            return rc
        return 0
    except C.Refused as exc:
        emit({"status": "REFUSED", "reason": exc.reason, "retry_when": exc.retry_when,
              "next_command": exc.next_command})
        return 1
    except (C.Harness, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        emit({"status": "HARNESS_ERROR", "reason": "harness_error", "detail": str(exc),
              "retry_when": "after correcting the review inputs", "next_command": sys.argv})
        return 2


if __name__ == "__main__":
    sys.exit(main())
