# SPDX-License-Identifier: AGPL-3.0-only
"""Orchestrate the local editorial departments from an editable project."""
from pathlib import Path

from blocking import resolve
from board import write_boards, write_sources
from common import Parser, destination, digest, dump, load, project_snapshot, require, run, slot, valid_slug
from conform import conform, timeline
from sound import cues, write_sound


def execute(project_root, out, dialogue, job=None):
    docs, pins = project_snapshot(project_root)
    if job is not None:
        require(job["documents"] == pins, "request_identity_conflict",
                "submit an explicit revision of the request with current document digests")
    blocking = resolve(docs)
    sheet = cues(docs, blocking, dialogue)
    with destination(out) as stage:
        dump(stage / "blocking.json", blocking)
        write_boards(blocking, stage)
        sources = write_sources(blocking, stage)
        sound = write_sound(sheet, stage)
        edit = timeline(blocking, sources, sound)
        dump(stage / "timeline.json", edit)
        receipt = conform(edit, stage, stage / "master.mp4")
        request = {"schema": "rp4.editorial-receipt/v1", "status": "DELIVERED",
                   "project": pins,
                   "dialogue": dialogue, "timeline_sha256": digest(stage / "timeline.json"), "conform": receipt}
        if job is not None:
            request["request_id"] = job["request_id"]
        dump(stage / "receipt.json", request)
    return request


def main():
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("discover")
    build = sub.add_parser("build")
    slot(build)
    build.add_argument("--dialogue", action="store_true")
    request = sub.add_parser("execute")
    request.add_argument("--request", type=Path, required=True)
    slot(request)
    args = parser.parse_args()
    if args.operation == "discover":
        return {"schema": "rp4.editorial-capabilities/v1", "operations": {
            "board": "board.py", "blocking": "blocking.py", "sound": "sound.py",
            "conform": "conform.py", "build": "editorial.py build", "execute": "editorial.py execute",
            "panel": "panel.py"}, "contract": "schemas.json", "example": "fixture/request.json",
            "exit": {"success": 0, "refused": 1, "harness_error": 2}}
    if args.operation == "build":
        return execute(args.project, args.out, args.dialogue)
    job = load(args.request)
    require(job["schema"] == "rp4.editorial-request/v1" and job["op"] == "editorial.build"
            and valid_slug(job["request_id"]) and type(job["dialogue"]) is bool,
            "invalid_request", "use the documented request and operation")
    return execute(args.project, args.out, job["dialogue"], job)


if __name__ == "__main__":
    raise SystemExit(run(main))
