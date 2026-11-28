#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Validate, resolve, archive, and restore a recurring series."""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path
from stat import S_IFREG

from series_contract import (
    JSON_LIMIT, SNAPSHOT, Refusal, asset_file, canonical, continuity, digest, fields,
    file_digest, hash_stream, identifier, need, parse_json, portable_path, read_json,
    sha, verify_assets, verify_readback,
)
from series_rights import Rights

os.environ.update(LC_ALL="C.UTF-8", LANG="C.UTF-8", TZ="UTC", PYTHONDONTWRITEBYTECODE="1")


def document(path):
    path = path.resolve()
    project = read_json(path)
    stats = continuity(project)
    return path.parent, project, stats


def transport_path(path, drive_root, rights):
    if rights.backend == "local":
        need(drive_root is None, "drive_not_allowed", rights.destination)
        return path
    need(drive_root is not None and drive_root.is_dir(), "drive_root_required", rights.destination)
    relative = portable_path(path.as_posix())
    root = drive_root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        need(not current.is_symlink(), "drive_path_symlink", relative.as_posix())
    return current


def zip_entry(name):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def publish_archive(staged, target, zip_pin, backend):
    def same_archive():
        return not target.is_symlink() and target.is_file() and file_digest(target) == zip_pin

    if backend == "local":
        try:
            os.link(staged, target)
        except FileExistsError:
            need(same_archive(), "archive_output_conflict", target.name)
        return
    lock = target.with_suffix(".lock")
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise Refusal("archive_output_locked", target.name) from exc
    try:
        if target.exists() or target.is_symlink():
            need(same_archive(), "archive_output_conflict", target.name)
        else:
            os.replace(staged, target)
    finally:
        lock.rmdir()


def archive_series(args, rights):
    root, project, stats = document(args.project)
    assets = project["assets"]
    for key, asset in sorted(assets.items()):
        rights.require(key, asset)
    out = transport_path(args.output, args.drive_root, rights).resolve()
    need(out.is_dir(), "output_directory_missing", out.name)
    snapshot = {
        "schema": SNAPSHOT, "destination": rights.destination, "backend": rights.backend,
        "mode": "fixture" if args.fixture else "production", "policy_sha256": rights.pin,
        "project": project,
    }
    metadata = canonical(snapshot)
    need(len(metadata) <= JSON_LIMIT, "document_too_large", "snapshot.json")
    pin = digest(metadata)
    target = out / f"{pin}.zip"
    with tempfile.TemporaryDirectory(dir=out, prefix=".series-export-") as stage:
        staged = Path(stage) / "series.zip"
        with zipfile.ZipFile(staged, "w", allowZip64=True) as archive:
            archive.writestr(zip_entry("snapshot.json"), metadata)
            written = set()
            for key, asset in sorted(assets.items(), key=lambda pair: (pair[1]["sha256"], pair[0])):
                blob = asset["sha256"]
                path = asset_file(root, asset["path"], key)
                with path.open("rb") as source:
                    if blob in written:
                        actual = hash_stream(source)
                    else:
                        with archive.open(zip_entry(f"objects/{blob}"), "w", force_zip64=True) as sink:
                            actual = hash_stream(source, sink)
                need(actual == blob, "asset_digest_mismatch", key)
                written.add(blob)
        zip_pin = file_digest(staged)
        publish_archive(staged, target, zip_pin, rights.backend)
    return {**stats, "archive": str(target), "snapshot_sha256": pin,
            "archive_sha256": zip_pin, "bytes": target.stat().st_size, "objects": len(written)}


def empty_root(root):
    return not root.is_symlink() and (not root.exists() or
                                     (root.is_dir() and next(root.iterdir(), None) is None))


def verified_snapshot(archive, args, rights):
    entries = archive.infolist()
    names = {entry.filename for entry in entries}
    need(len(names) == len(entries), "archive_duplicate_entry", "zip")
    need(all(entry.compress_type == zipfile.ZIP_STORED and not entry.flag_bits & 1
             for entry in entries), "archive_entry_unsupported", "zip")
    need("snapshot.json" in names, "archive_snapshot_missing", "zip")
    info = archive.getinfo("snapshot.json")
    need(info.file_size <= JSON_LIMIT, "document_too_large", "snapshot.json")
    raw = archive.read(info)
    need(digest(raw) == args.snapshot_sha256, "snapshot_digest_mismatch", "snapshot.json")
    snapshot = parse_json(raw, "snapshot.json")
    fields(snapshot, "schema destination backend mode policy_sha256 project", "snapshot")
    need(snapshot["schema"] == SNAPSHOT, "unsupported_schema", "snapshot")
    need(snapshot["destination"] == rights.destination and snapshot["backend"] == rights.backend,
         "archive_destination_mismatch", rights.destination)
    need(snapshot["mode"] == ("fixture" if args.fixture else "production"),
         "archive_mode_mismatch", snapshot["mode"])
    sha(snapshot["policy_sha256"], "export policy pin")
    project = snapshot["project"]
    stats = continuity(project)
    need(canonical(snapshot) == raw, "snapshot_noncanonical", "snapshot.json")
    expected = {f"objects/{asset['sha256']}" for asset in project["assets"].values()}
    need(not expected - names, "archive_object_missing", sorted(expected - names))
    need(names == expected | {"snapshot.json"}, "archive_unexpected_entry",
         sorted(names - expected - {"snapshot.json"}))
    for key, asset in sorted(project["assets"].items()):
        rights.require(key, asset)
    return snapshot, stats


def restore_series(args, rights):
    sha(args.snapshot_sha256, "snapshot pin")
    package = transport_path(args.archive, args.drive_root, rights)
    requested = args.into
    need(requested.name and requested.name != "..", "restore_root_not_empty", str(requested))
    root = requested.parent.resolve() / requested.name
    need(empty_root(root), "restore_root_not_empty", root.name)
    with zipfile.ZipFile(package, "r") as archive:
        snapshot, stats = verified_snapshot(archive, args, rights)
        project = snapshot["project"]
        with tempfile.TemporaryDirectory(dir=root.parent, prefix=".series-restore-") as stage:
            target = Path(stage) / "project"
            target.mkdir()
            (target / "project.json").write_bytes(canonical(project))
            restored = {}
            for key, asset in sorted(project["assets"].items()):
                path = target / asset["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                blob = asset["sha256"]
                if blob in restored:
                    shutil.copyfile(restored[blob], path)
                    continue
                with archive.open(f"objects/{blob}") as source, path.open("xb") as sink:
                    actual = hash_stream(source, sink)
                need(actual == blob, "archive_object_digest_mismatch", key)
                restored[blob] = path
            need(empty_root(root), "restore_root_not_empty", root.name)
            try:
                os.replace(target, root)
            except OSError:
                need(empty_root(root), "restore_root_not_empty", root.name)
                raise
    return {**stats, "root": str(root), "snapshot_sha256": args.snapshot_sha256,
            "export_policy_sha256": snapshot["policy_sha256"], "active_policy_sha256": rights.pin,
            "objects": len(restored)}


def emit(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))


def rejection(reason, detail):
    emit({"reason": reason, "detail": detail,
          "retry_when": "after the stated input, evidence, or precondition has been corrected",
          "next_command": "python3 -B series.py --help"})


class CLI(argparse.ArgumentParser):
    def error(self, message):
        rejection("invalid_arguments", message)
        raise SystemExit(2)


def rights_flags(parser):
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--fixture", action="store_true")


def cli():
    parser = CLI(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="verify continuity and referenced local bytes")
    check.add_argument("--project", type=Path, required=True)
    check.add_argument("--readback", type=Path)
    rights = commands.add_parser("rights", help="classify project and indexed library assets")
    rights.add_argument("--project", type=Path)
    rights.add_argument("--limit", type=int)
    rights_flags(rights)
    search = commands.add_parser("search", help="search the pinned curation FTS5 table")
    search.add_argument("--project", type=Path)
    search.add_argument("--query", required=True)
    search.add_argument("--eligible-only", action="store_true")
    search.add_argument("--limit", type=int, default=20)
    rights_flags(search)
    resolve = commands.add_parser("resolve", help="verify eligibility and bytes for an exact asset ID")
    resolve.add_argument("--project", type=Path, required=True)
    resolve.add_argument("--asset-id", required=True)
    rights_flags(resolve)
    export = commands.add_parser("export", help="create a deterministic content-addressed ZIP")
    export.add_argument("--project", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--drive-root", type=Path)
    rights_flags(export)
    restore = commands.add_parser("import", help="verify and restore into an empty root")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--snapshot-sha256", required=True)
    restore.add_argument("--into", type=Path, required=True)
    restore.add_argument("--drive-root", type=Path)
    rights_flags(restore)
    return parser


def run(args):
    if args.command == "check":
        root, project, stats = document(args.project)
        verify_assets(root, project["assets"])
        if args.readback is not None:
            verify_readback(project, read_json(args.readback))
        return stats
    rights = Rights(args)
    if args.command == "export":
        return archive_series(args, rights)
    if args.command == "import":
        return restore_series(args, rights)
    if args.command == "resolve":
        root, project, _ = document(args.project)
        identifier(args.asset_id, "asset id")
        need(args.asset_id in project["assets"], "asset_reference_missing", args.asset_id)
        asset = project["assets"][args.asset_id]
        result = rights.require(args.asset_id, asset)
        verify_assets(root, {args.asset_id: asset})
        return {**result, **asset}
    assets = document(args.project)[1]["assets"] if args.project else {}
    need(args.limit is None or args.limit >= 0, "invalid_limit", args.limit)
    if args.command == "search":
        return rights.report(assets, query=args.query, eligible_only=args.eligible_only, limit=args.limit)
    need(args.project is not None or args.inventory is not None, "rights_subject_absent", args.command)
    return rights.report(assets, limit=args.limit)


def main():
    try:
        emit(run(cli().parse_args()))
    except Refusal as exc:
        rejection(exc.reason, exc.detail)
        return 1
    except (zipfile.BadZipFile, NotImplementedError) as exc:
        rejection("archive_invalid", str(exc))
        return 1
    except (OSError, sqlite3.Error) as exc:
        rejection("runtime_error", str(exc))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
