#!/usr/bin/env -S python3 -B
# SPDX-License-Identifier: AGPL-3.0-only
"""Stage or cold-resolve the exact shot dependencies of a two-recipe collection."""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import library as lib

up = lib.up


def safe_relative(path):
    rel = Path(path)
    if rel.is_absolute() or str(rel) != path or not rel.parts or ".." in rel.parts:
        lib.refuse("unsafe_package_path", path)
    return rel


def copy_pinned(source, destination, expected):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    lib.verify(destination, expected, {}, "staged_digest_mismatch")


def shot_keys(resolved):
    return [(
        shot["assets"]["motion"]["sha256"], shot["motion_clip"],
        shot["assets"]["set"]["sha256"], shot["assets"]["set"]["selector"],
        shot["assets"]["camera"]["sha256"], shot["assets"]["camera"]["selector"],
        shot["frame_start"], shot["frame_end"],
    ) for shot in resolved["shots"]]


def stage(destination, recipes, source_snapshot, db, roots, index, admissions, fixture=False, admission_pin=None):
    if destination.exists():
        lib.refuse("collection_destination_exists", destination)
    if not fixture and admission_pin is None:
        lib.refuse("production_admission_absent", "supply an admitted private catalogue")
    if len(recipes) != 2:
        lib.refuse("distinct_recipe_required", "supply the film recipe and a distinct reuse recipe")
    results = [lib.authorize_assembly(p, source_snapshot, db, roots, index, admissions) for p in recipes]
    first, second = (item[0] for item in results)
    if first["episode_id"] == second["episode_id"] or shot_keys(first) == shot_keys(second):
        lib.refuse("distinct_recipe_required", second["episode_id"])
    characters = [{s["assets"]["character"]["sha256"] for s in res["shots"]} for res in (first, second)]
    if len(characters[0]) != 1 or characters[0] != characters[1]:
        lib.refuse("shared_character_mismatch", characters)
    selected_ids = {asset["asset_id"] for res in (first, second) for shot in res["shots"]
                    for asset in shot["assets"].values()}
    if not fixture and selected_ids != admissions.keys():
        lib.refuse("nonminimal_admission_roster", sorted(admissions.keys() - selected_ids),
                   "when the trusted admission ledger is limited to the two recipe closures")

    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".staging-") as scratch:
        work = Path(scratch) / "collection"
        work.mkdir()
        subset = work / "catalog"
        (subset / "manifests").mkdir(parents=True)
        objects = {}
        root_labels = set()
        asset_ids = set()
        assembly = []
        cache = {}

        def include(obj):
            label = obj["root"]
            if not isinstance(label, str) or not up.ROOT_ID.fullmatch(label) or label not in roots:
                lib.refuse("invalid_storage_root", label)
            root_labels.add(label)
            relative = str(Path("payload", label) / safe_relative(obj["path"]))
            existing = objects.get(relative)
            if existing and existing != obj["sha256"]:
                lib.refuse("collection_path_collision", relative)
            if not existing:
                source_path = up.actual_source(roots, obj)
                copy_pinned(source_path, work / relative, obj["sha256"])
                objects[relative] = obj["sha256"]

        for i, (path, (resolved, receipt)) in enumerate(zip(recipes, results)):
            for obj in receipt["objects"]:
                include(obj)
            for asset_id, info in receipt["approvals"].items():
                asset_ids.add(asset_id)
                for proof in info["receipts"].values():
                    include(proof)
            dest = f"recipe-{i + 1}.json"
            copy_pinned(path, work / dest, receipt["recipe_sha256"])
            assembly.append({"recipe": dest, "sha256": receipt["recipe_sha256"],
                             "episode_id": resolved["episode_id"],
                             "approvals": receipt["approvals"]})
        if admission_pin:
            ledger_path = Path(admission_pin[0])
            lib.verify(ledger_path, admission_pin[1], cache, "admission_digest_mismatch")
            copy_pinned(ledger_path, work / "admission.json", admission_pin[1])

        with closing(sqlite3.connect(subset / "catalog.sqlite3")) as portable:
            portable.execute("CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            portable.execute(db.execute("SELECT sql FROM sqlite_schema WHERE name='asset'").fetchone()[0])
            portable.executemany("INSERT INTO settings VALUES (?,?)", [("snapshot", "catalog"), ("roots", "{}")])
            for asset_id in sorted(asset_ids):
                item = db.execute("SELECT * FROM asset WHERE id=?", (asset_id,)).fetchone()
                portable.execute("INSERT INTO asset VALUES (?,?,?,?,?,?,?)", tuple(item))
                copy_pinned(source_snapshot / "manifests" / f"{asset_id}.json",
                            subset / "manifests" / f"{asset_id}.json", item["manifest_sha256"])
            portable.commit()
        files = {path.relative_to(work).as_posix(): {"sha256": up.sha_file(path), "bytes": path.stat().st_size}
                 for path in sorted(work.rglob("*")) if path.is_file()}
        handoff = {
            "schema": "rp4.library-collection/v1", "status": "FIXTURE" if fixture else "SHOT_INPUTS_VERIFIED",
            "destination": lib.DESTINATION, "source_snapshot": source_snapshot.name,
            "shared_character_sha256": next(iter(characters[0])),
            "shared_shot_payload_sha256": sorted(
                {o["sha256"] for o in results[0][1]["objects"]} &
                {o["sha256"] for o in results[1][1]["objects"]}),
            "recipes": assembly, "root_labels": sorted(root_labels),
            "measurement_class": "[MEASURED]", "files": files,
        }
        manifest = up.canonical(handoff)
        up.atomic_write(work / "COLLECTION.json", manifest)
        collection_sha = up.digest(manifest)
        os.replace(work, destination)
    return {**handoff, "collection_sha256": collection_sha,
            "assembly_timing_ns": {r["episode_id"]: receipt["assembly_ns"] for r, receipt in results}}


def cold_resolve(package, expected):
    started = time.monotonic_ns()
    if not package.is_dir() or package.is_symlink():
        lib.refuse("invalid_package_path", package)
    list_path = package / "COLLECTION.json"
    cache = {}
    lib.verify(list_path, expected, cache, "collection_index_drift")
    handoff = lib.read_json(list_path)
    if handoff["schema"] != "rp4.library-collection/v1":
        lib.refuse("invalid_collection_schema", package)
    listed = handoff["files"]
    paths = list(package.rglob("*"))
    if any(path.is_symlink() for path in paths):
        lib.refuse("package_link_forbidden", package)
    actual = {path.relative_to(package).as_posix() for path in paths if path.is_file()}
    if actual != listed.keys() | {"COLLECTION.json"}:
        lib.refuse("collection_file_roster_mismatch", sorted(actual ^ (listed.keys() | {"COLLECTION.json"})))
    for relative, info in listed.items():
        obj = lib.verify(package / safe_relative(relative), info["sha256"], cache, "collection_object_drift")
        if obj.stat().st_size != info["bytes"]:
            lib.refuse("collection_object_size_mismatch", relative)

    roots = {label: package / "payload" / safe_relative(label) for label in handoff["root_labels"]}
    subset = package / "catalog"
    results = []
    with lib.source(subset, listed["catalog/catalog.sqlite3"]["sha256"], roots) as (db, _):
        for recipe in handoff["recipes"]:
            path = package / safe_relative(recipe["recipe"])
            lib.verify(path, recipe["sha256"], cache, "recipe_digest_mismatch")
            resolved = up.assemble(argparse.Namespace(spec=path), subset, db, roots)
            results.append(resolved)
    return {
        "status": "COLD_RESOLVED", "collection_sha256": expected,
        "episodes": results, "elapsed_ns": time.monotonic_ns() - started,
        "measurement_class": "[MEASURED]",
    }


def main():
    parser = lib.Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("stage")
    lib.catalogue_args(build)
    build.add_argument("--index", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("film", type=Path)
    build.add_argument("reuse", type=Path)
    cold = commands.add_parser("cold-resolve")
    cold.add_argument("package", type=Path)
    cold.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if args.command == "cold-resolve":
        if args.package.absolute() != args.package.resolve():
            lib.refuse("invalid_package_path", args.package)
        return cold_resolve(args.package.resolve(), args.sha256)
    snapshot, sha, trust = lib.source_settings(args)
    with lib.source(snapshot, sha) as (db, roots), lib.open_index(args.index, snapshot, sha, trust) as (index, entries):
        pin = json.loads(index.execute("SELECT value FROM pin WHERE key='admission'").fetchone()[0])
        destination = lib.write_path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        return stage(destination, [args.film, args.reuse], snapshot, db, roots, index, entries,
                     admission_pin=pin)


if __name__ == "__main__":
    lib.main = main
    raise SystemExit(lib.run())
