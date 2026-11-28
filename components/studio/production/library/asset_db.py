#!/usr/bin/env -S python3 -B
# SPDX-License-Identifier: AGPL-3.0-only
"""Build, search and verify the local ViewPoint asset catalogue."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import struct
import sys
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
os.environ.update(LC_ALL="C.UTF-8", LANG="C.UTF-8", TZ="UTC")

from asset_formats import ABSENT, IMAGES, MOVIES, gltf_document, inspect, preview, relative_uri
from asset_sources import evidence_for, licence_bindings, media_index, read_evidence, semantic, sha_file, source_file, source_rows

BASE = Path(__file__).resolve().parent
ASSET_SCHEMA = "viewpoint.asset/v1"
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ASSET_ID = re.compile(r"[a-z_]+-[0-9a-f]{64}\Z")
ROOT_ID = re.compile(r"[a-z][a-z0-9_-]*\Z")
QUERY_STOP = {"a", "an", "and", "for", "in", "of", "on", "that", "the", "to", "with"}
ACTION_TERMS = {"press", "presses", "walk", "walks", "run", "runs", "jump", "jumps", "jab", "hit", "hits", "turn", "turns", "wave", "waves"}
MEDIA_TERMS = {"audio", "music", "voice", "sound", "still", "image", "thumbnail", "photo"}
INSPECT_ERRORS = (OSError, ValueError, IndexError, KeyError, TypeError, struct.error)
ENVIRONMENT = {key: os.environ[key] for key in ("LC_ALL", "LANG", "TZ")}

class Refusal(Exception):
    def __init__(self, reason, detail, retry_when, rc=1):
        super().__init__(detail)
        self.reason = reason
        self.retry_when = retry_when
        self.rc = rc

def reject(reason, detail, retry_when="when the catalogue or request matches its recorded inputs", rc=1):
    raise Refusal(reason, str(detail), retry_when, rc)

def canonical(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")

def digest(data):
    return hashlib.sha256(data).hexdigest()

def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".write-", delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

def writable(value):
    path = Path(value).absolute()
    if path.resolve() != path or (path.is_file() and path.stat().st_nlink != 1):
        reject("output_alias", path, "when a canonical output path is supplied", 2)
    return path

def parse_roots(values):
    if not values:
        reject("source_roots_required", "--root", "with at least one label=/absolute/directory", 2)
    roots = {}
    for value in values:
        label, separator, folder = value.partition("=")
        if not separator or not ROOT_ID.fullmatch(label) or label in roots:
            reject("invalid_root", value, "when every root is a unique label=/absolute/directory", 2)
        path = Path(folder)
        if not path.is_absolute() or not path.is_dir():
            reject("invalid_root", value, "when the source directory exists", 2)
        roots[label] = path.resolve(strict=True)
    return dict(sorted(roots.items()))

def actual_source(roots, ref):
    try:
        return source_file(roots[ref["root"]], Path(ref["path"]))
    except FileNotFoundError as exc:
        reject("missing_source", exc, "when the original source is restored or a new catalogue is indexed")
    except (ValueError, KeyError, TypeError) as exc:
        reject("invalid_source_path", exc)

def cached_digest(path, cache):
    before = path.stat()
    key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    if key not in cache:
        sha = sha_file(path)
        after = path.stat()
        if key != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            reject("source_changed_during_read", path, "when source producers have finished writing")
        cache[key] = sha
    return cache[key], before.st_size

def check_digest(path, expected, cache):
    if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
        reject("invalid_digest", path)
    if cached_digest(path, cache)[0] != expected:
        reject("digest_mismatch", path, "when the pinned bytes are restored or a new catalogue is indexed")

def media_dependency(path, root, uri, cache):
    candidate = Path(os.path.normpath(path.parent / relative_uri(uri)))
    rel = candidate.relative_to(root)
    try:
        target = source_file(root, rel)
        sha, size = cached_digest(target, cache)
        return {"uri": uri, "path": rel.as_posix(), "sha256": sha, "bytes": size}
    except (OSError, ValueError):
        return {"uri": uri, "status": ABSENT}

def rights_record(entries, mocap):
    if not entries:
        return {"status": ABSENT, "drive_policy": "requires_review", "evidence": []}
    restrictive = {"local_use_only", "no_external_redistribution", "no_publication", "not_cleared", "denied"}
    local_only = any(part in json.dumps(entry["terms"], sort_keys=True).lower().replace("-", "_")
                     for entry in entries for part in restrictive)
    return {"status": "source_terms_recorded", "drive_policy": "withhold" if local_only or mocap else "requires_review",
            "evidence": entries}

def companion_paths(kind, sources, roots, by_path):
    if kind in {"contact_sheet", "render", "take", "audio", "material"}:
        return []
    found = set()
    for source in sources:
        label = source["root"]
        relative = Path(source["path"])
        parent = relative.parent
        stem = relative.stem
        candidates = [parent / f"{stem}{joiner}{suffix}" for joiner in ("-", "_", ".")
                      for suffix in ("contact-sheet.png", "contact_sheet.png", "grid.png", "preview.png")]
        for evidence in source["receipts"] if source["receipts"] != ABSENT else []:
            if evidence["binding"] != "asset_sha256":
                continue
            record = read_evidence(actual_source(roots, evidence))
            outputs = record[1].get("outputs") if record else None
            if not isinstance(outputs, dict):
                continue
            for name, pin in outputs.items():
                if not isinstance(pin, dict) or not isinstance(pin.get("path"), str) or not isinstance(pin.get("sha256"), str):
                    continue
                candidate = Path(pin["path"])
                if (candidate.is_relative_to(roots[label]) and candidate.suffix.lower() in IMAGES and
                        re.search(r"contact|thumbnail|preview|grid|sheet", name, re.I)):
                    candidate = candidate.relative_to(roots[label])
                    if by_path.get((label, candidate.as_posix())) == pin["sha256"]:
                        found.add(pin["sha256"])
        candidates.extend(parent / name for name in ("contact-sheet.png", "contact_sheet.png"))
        for path in candidates:
            record = by_path.get((label, path.as_posix()))
            if record and (roots[label] / path).suffix.lower() in IMAGES:
                found.add(record)
    return sorted(found)

SQL_INIT = """
PRAGMA journal_mode=DELETE;
PRAGMA user_version=1;
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE asset (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
    tags TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, source_sha256 TEXT NOT NULL
);
CREATE TABLE origin (
    root TEXT NOT NULL, path TEXT NOT NULL, id TEXT NOT NULL REFERENCES asset(id),
    PRIMARY KEY (root, path, id)
);
CREATE VIRTUAL TABLE lexical USING fts5(id UNINDEXED,title,description,tags,paths,tokenize='porter unicode61');
"""

def collect(roots):
    hashes = {}
    groups = defaultdict(list)
    by_path = {}
    for label, relative, path, guessed in source_rows(roots, BASE):
        sha, size = cached_digest(path, hashes)
        key = (guessed, sha)
        groups[key].append((label, relative, path, size))
        by_path[(label, relative.as_posix())] = sha
    return hashes, groups, by_path

def prepare_preview(groups, jobs, reused):
    tasks = {}
    for (_, sha), rows in groups.items():
        path = rows[0][2]
        if sha not in reused and path.suffix.lower() in IMAGES | MOVIES | {".glb", ".vrm"}:
            tasks.setdefault(sha, path)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    digests = iter(sorted(tasks, key=lambda sha: (tasks[sha].suffix.lower() not in MOVIES, sha)))
    pending = {}

    def refill():
        while len(pending) < jobs * 4:
            sha = next(digests, None)
            if sha is None:
                break
            pending[executor.submit(preview, tasks[sha])] = sha

    try:
        refill()
        while pending:
            done, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                yield pending.pop(future), *future.result()
            refill()
    finally:
        executor.shutdown(cancel_futures=True)

def reuse_previews(previous, destination, wanted):
    records = {}
    known = set()
    if previous is None:
        return records, known
    for path in (previous / "manifests").glob("*.json"):
        item, _ = load_manifest(path)
        sha = item["sha256"]
        thumbnails = item["contact_sheet_thumbnails"]
        if item["selector"] or sha in records or sha not in wanted or thumbnails == ABSENT:
            continue
        for thumb in thumbnails:
            if thumb["relation"] != "generated_from_asset" or thumb["source_sha256"] != sha:
                continue
            content_hash = thumb["sha256"]
            name = f"{content_hash}.jpg"
            old = previous / "thumbnails" / name
            if not old.is_file() or old.is_symlink() or sha_file(old) != content_hash:
                continue
            if content_hash not in known:
                os.link(old, destination / name)
                known.add(content_hash)
            fields = ("duration_seconds", "width", "height", "average_frame_rate", "thumbnail_times_seconds", "thumbnail_basis")
            data = item["technical"] if isinstance(item["technical"], dict) else {}
            records[sha] = ({key: thumb[key] for key in ("path", "sha256", "bytes")},
                            {key: data[key] for key in fields if key in data})
            break
    return records, known

def manifest_ref(asset_id, source_digest):
    return {"schema": ASSET_SCHEMA, "asset_id": asset_id, "sha256": source_digest}

def reuse_argv(command, asset_id, library):
    argv = ["asset_db.py", "--library", str(library)]
    return [*argv, command, asset_id]

def make_manifests(roots, groups, by_path, hashes, preview_records, library):
    licenses = licence_bindings(roots)
    material_index = media_index(roots)
    for (guessed, sha), rows in sorted(groups.items()):
        label, relative, path, size = rows[0]
        failure = None
        try:
            data, cameras, uris = inspect(path)
        except INSPECT_ERRORS as exc:
            data, cameras, uris = {}, [], []
            failure = f"{type(exc).__name__}: {exc}"
        kind = guessed
        if guessed == "mesh" and data.get("rig", {}).get("joint_roster"):
            kind = "rig"
        if guessed == "mesh" and data.get("clips"):
            kind = "motion"
        # The original class anchors IDs when format inspection refines the displayed class.
        raw_id = f"{guessed}-{sha}"
        sources = []
        licence = list(licenses.get(sha, []))
        for origin_label, origin_relative, origin_path, _ in rows:
            src, rights = evidence_for(origin_label, origin_relative, roots[origin_label], sha)
            if (origin_label, origin_relative.as_posix()) in material_index:
                src["media_index"] = material_index[origin_label, origin_relative.as_posix()]
            licence.extend(rights)
            if uris:
                refs = []
                remaining = [(origin_path, uri) for uri in uris]
                visited = set()
                while remaining:
                    owner, uri = remaining.pop(0)
                    if (owner, uri) in visited:
                        continue
                    visited.add((owner, uri))
                    try:
                        ref = media_dependency(owner, roots[origin_label], uri, hashes)
                    except ValueError:
                        ref = {"uri": uri, "status": ABSENT}
                    ref["relative_to"] = owner.relative_to(roots[origin_label]).as_posix()
                    refs.append(ref)
                    if "path" in ref and Path(ref["path"]).suffix.lower() == ".mtl":
                        material_path = roots[origin_label] / ref["path"]
                        try:
                            remaining.extend((material_path, link) for link in inspect(material_path)[2])
                        except INSPECT_ERRORS:
                            ref["dependency_metadata"] = ABSENT
                src["dependencies"] = refs
            sources.append(src)
        thumbnails = []
        direct = preview_records.get(sha)
        if direct:
            thumbnails.append({**direct[0], "relation": "generated_from_asset", "source_sha256": sha})
            data.update(direct[1])
        for related in companion_paths(kind, sources, roots, by_path):
            other = preview_records.get(related)
            if other and related != sha:
                thumbnails.append({**other[0], "relation": "source_companion", "source_sha256": related})
        mocap = bool(licenses.get(sha)) or any(
            "mocap" in str(row[1]).lower() and guessed == "motion" for row in rows
        )
        rights = rights_record(licence, mocap)
        if mocap and rights["drive_policy"] == "requires_review" and any(
            re.search(r"keyanim/.*/mocap/|studio-qloop-4/.*/mocap-cpu/", str(row[1]).lower()) for row in rows
        ):
            rights["drive_policy"] = "withhold"
            rights["basis"] = "Source path denotes a derived mocap branch. External redistribution clearance is AUSENTE."
        manifest = {**manifest_ref(raw_id, sha), "kind": kind, "selector": None,
                    "bytes": size, "format": path.suffix.lower().lstrip("."), "provenance": sources,
                    "licence_rights": rights, "triangle_count": data.pop("triangles", ABSENT),
                    "rig": data.pop("rig", ABSENT), "materials": data.pop("materials", ABSENT),
                    "animation_clips": data.pop("clips", ABSENT), "technical": data or ABSENT,
                    "contact_sheet_thumbnails": thumbnails or ABSENT,
                    "metadata_read_error": failure or ABSENT}
        yield add_description(manifest, library)
        for selector, name, camera in cameras:
            derived = manifest.copy()
            derived.update(asset_id=f"camera-{digest(canonical([sha, selector]))}", kind="camera",
                           selector=selector, triangle_count=ABSENT, rig=ABSENT, materials=ABSENT,
                           animation_clips=ABSENT,
                           technical={"camera": camera, "camera_name": name, "container_kind": kind})
            yield add_description(derived, library)

def add_description(manifest, library):
    metadata = manifest["technical"] if isinstance(manifest["technical"], dict) else {}
    data = {**metadata, "clips": manifest["animation_clips"]}
    title, description, tags, basis = semantic(manifest["kind"], manifest["provenance"], data)
    manifest.update(title=title, description=description, tags=tags, description_basis=basis,
                    reuse={"inspect_argv": reuse_argv("show", manifest["asset_id"], library),
                           "resolve_argv": reuse_argv("resolve", manifest["asset_id"], library)})
    return manifest

def index(args, library):
    roots = parse_roots(args.root)
    for label, root in roots.items():
        if not root.is_dir():
            reject("missing_source_root", f"{label}={root}", "when the configured source roots are mounted", 2)
        if library.is_relative_to(root) or root.is_relative_to(library):
            reject("recursive_library", root, "when the source roots are separate from the output library", 2)
    library.mkdir(parents=True, exist_ok=True)
    snapshots = library / "snapshots"
    snapshots.mkdir(exist_ok=True)
    hashes, groups, by_path = collect(roots)
    if not groups:
        reject("empty_catalogue", "No matching files under the configured roots.", "when at least one asset is available")
    print(f"Indexing {sum(map(len, groups.values()))} source paths.", file=sys.stderr, flush=True)
    pointer = library / "CURRENT"
    previous = None
    if pointer.is_file():
        current_id = pointer.read_text(encoding="utf-8").strip()
        if not DIGEST.fullmatch(current_id):
            reject("invalid_snapshot", current_id)
        previous = snapshots / current_id
    with tempfile.TemporaryDirectory(dir=snapshots, prefix=".building-") as temporary:
        build = Path(temporary)
        (build / "manifests").mkdir()
        (build / "thumbnails").mkdir()
        previews, known = reuse_previews(previous, build / "thumbnails", {sha for _, sha in groups})
        for sha, sheet, data in prepare_preview(groups, args.jobs, previews):
            if sheet:
                content_hash = digest(sheet)
                name = f"{content_hash}.jpg"
                if content_hash not in known:
                    (build / "thumbnails" / name).write_bytes(sheet)
                    known.add(content_hash)
                previews[sha] = ({"path": f"thumbnails/{name}", "sha256": content_hash,
                                  "bytes": len(sheet)}, data)
        print(f"Built {len(known)} unique contact sheet thumbnails.", file=sys.stderr, flush=True)
        counts = Counter()
        seal = hashlib.sha256(canonical({key: str(value) for key, value in roots.items()}))
        with sqlite3.connect(build / "catalog.sqlite3") as database:
            database.executescript(SQL_INIT)
            root_json = {key: str(value) for key, value in roots.items()}
            database.execute("INSERT INTO settings VALUES ('roots', ?)", (canonical(root_json).decode(),))
            for item in make_manifests(roots, groups, by_path, hashes, previews, library):
                asset_id = item["asset_id"]
                if not ASSET_ID.fullmatch(asset_id):
                    reject("invalid_asset_id", asset_id)
                manifest_path = build / "manifests" / f"{asset_id}.json"
                payload = canonical(item)
                manifest_sha = digest(payload)
                try:
                    with manifest_path.open("xb") as stream:
                        stream.write(payload)
                except FileExistsError:
                    reject("duplicate_id", asset_id)
                seal.update(manifest_sha.encode())
                tags = " ".join(item["tags"])
                database.execute("INSERT INTO asset VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (asset_id, item["kind"], item["title"], item["description"], tags, manifest_sha, item["sha256"]))
                aliases = [(origin["root"], origin["path"], asset_id) for origin in item["provenance"]]
                database.executemany("INSERT INTO origin VALUES (?, ?, ?)", aliases)
                paths_text = " ".join(path for _, path, _ in aliases)
                database.execute("INSERT INTO lexical VALUES (?, ?, ?, ?, ?)",
                                 (asset_id, item["title"], item["description"], tags, paths_text))
                counts[item["kind"]] += 1
            snapshot = seal.hexdigest()
            database.execute("INSERT INTO settings VALUES ('snapshot', ?)", (snapshot,))
        final = snapshots / snapshot
        try:
            build.rename(final)
        except OSError:
            if not final.is_dir():
                raise
            for folder in ("manifests", "thumbnails"):
                if {path.name for path in (build / folder).iterdir()} != {path.name for path in (final / folder).iterdir()}:
                    reject("snapshot_conflict", final, "when the existing snapshot is restored or safely removed")
            if sha_file(build / "catalog.sqlite3") != sha_file(final / "catalog.sqlite3"):
                reject("snapshot_conflict", final, "when the existing snapshot is restored or safely removed")
            for file in (build / "manifests").iterdir():
                if sha_file(file) != sha_file(final / "manifests" / file.name):
                    reject("snapshot_conflict", final, "when the existing snapshot is restored or safely removed")
            for file in (build / "thumbnails").iterdir():
                if sha_file(final / "thumbnails" / file.name) != file.stem:
                    reject("snapshot_conflict", final, "when the existing snapshot is restored or safely removed")
        atomic_write(library / "CURRENT", (snapshot + "\n").encode())
    return {"snapshot": snapshot, "database": str(final / "catalog.sqlite3"),
            "counts": dict(counts), "assets": sum(counts.values()), "source_paths": len(by_path),
            "contact_sheets": len(known), "roots": root_json}

@contextmanager
def catalogue(library, snapshot_id=None):
    if snapshot_id is None:
        pointer = library / "CURRENT"
        if not pointer.is_file():
            reject("catalogue_absent", library, "after asset_db.py index completes", 2)
        snapshot_id = pointer.read_text(encoding="utf-8").strip()
    if not DIGEST.fullmatch(snapshot_id):
        reject("invalid_snapshot", snapshot_id)
    snapshot = library / "snapshots" / snapshot_id
    database_path = snapshot / "catalog.sqlite3"
    if not database_path.is_file():
        reject("catalogue_absent", database_path, "after asset_db.py index completes", 2)
    database = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    try:
        settings = dict(database.execute("SELECT key, value FROM settings"))
        if settings.get("snapshot") != snapshot_id:
            reject("snapshot_mismatch", snapshot)
        roots = {name: Path(folder) for name, folder in json.loads(settings["roots"]).items()}
        yield snapshot, database, roots
    finally:
        database.close()

def load_manifest(path):
    try:
        data = path.read_bytes()
        item = json.loads(data)
        asset_id = item["asset_id"]
        if item.get("schema") != ASSET_SCHEMA or not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
            raise ValueError("invalid asset manifest schema or ID")
        return item, digest(data)
    except (ValueError, TypeError, KeyError) as exc:
        reject("invalid_manifest", f"{path}: {exc}")

def get_item(snapshot, database, asset_id):
    if not ASSET_ID.fullmatch(asset_id):
        reject("invalid_asset_id", asset_id)
    row = database.execute("SELECT manifest_sha256 FROM asset WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        reject("unknown_asset_id", asset_id, "when the shot list references a catalogued asset ID")
    path = snapshot / "manifests" / f"{asset_id}.json"
    item, sha = load_manifest(path)
    if item["asset_id"] != asset_id or sha != row["manifest_sha256"]:
        reject("manifest_digest_mismatch", path)
    return item

def find_assets(args, database):
    terms = list(dict.fromkeys(word for word in re.findall(r"[^\W_]+", args.query.lower()) if word not in QUERY_STOP))
    if not terms or len(terms) > 64:
        reject("invalid_search_query", "Expected 1 to 64 searchable words.", "when query words are supplied", 2)
    query = " OR ".join(f'"{word}"' for word in terms)
    action_query = args.kind is None and ACTION_TERMS.intersection(terms) and not MEDIA_TERMS.intersection(terms)
    rows = database.execute(
        """SELECT asset.id,kind,asset.title,asset.description,asset.tags,
                  bm25(lexical,0,3,5,12,0.3) *
                  CASE WHEN ? AND kind IN ('motion','take') THEN 1.2
                       WHEN ? AND kind IN ('audio','render','contact_sheet') THEN 0.85
                       ELSE 1.0 END AS score
           FROM lexical JOIN asset ON asset.id=lexical.id
           WHERE lexical MATCH ? AND (? IS NULL OR kind=?)
           ORDER BY score,asset.id LIMIT ?""", (bool(action_query), bool(action_query), query, args.kind, args.kind, args.limit))
    return {"query": args.query, "results": [
        {"asset_id": row["id"], "kind": row["kind"], "title": row["title"],
         "description": row["description"], "tags": row["tags"].split(), "score": row["score"]} for row in rows
    ]}

def resolve_source(item, roots, cache, all_origins=False):
    origins = []
    for ref in item["provenance"]:
        path = actual_source(roots, ref)
        check_digest(path, item["sha256"], cache)
        dependencies = []
        for dep in ref.get("dependencies", []):
            if "sha256" not in dep:
                dependencies.append(dep)
                continue
            dep_path = actual_source(roots, {"root": ref["root"], **dep})
            check_digest(dep_path, dep["sha256"], cache)
            dependencies.append({**dep, "source_file": str(dep_path)})
        origins.append({"source_file": str(path), "root": ref["root"], "path": ref["path"],
                        "dependencies": dependencies})
        if not all_origins:
            break
    if not origins:
        reject("missing_source", item["asset_id"])
    return {"asset_id": item["asset_id"], "kind": item["kind"], "sha256": item["sha256"],
            "selector": item["selector"], "origins": origins}

def verified(snapshot, database, roots):
    rows = {row["id"]: row for row in database.execute("SELECT * FROM asset")}
    cache = {}
    manifests = {}
    for path in sorted((snapshot / "manifests").glob("*.json")):
        if path.is_symlink():
            reject("invalid_manifest", path)
        item, man_sha = load_manifest(path)
        asset_id = item["asset_id"]
        if asset_id in manifests:
            reject("duplicate_id", asset_id, "when exactly one manifest declares each asset ID")
        manifests[asset_id] = (item, man_sha, path)
    if rows.keys() != manifests.keys():
        reject("manifest_inventory_mismatch", "SQLite rows and manifest IDs differ.")
    for asset_id, (item, man_sha, path) in manifests.items():
        row = rows[asset_id]
        if row["manifest_sha256"] != man_sha or row["source_sha256"] != item["sha256"] or path.stem != asset_id:
            reject("manifest_digest_mismatch", path)
        resolved = resolve_source(item, roots, cache, all_origins=True)
        for origin in item["provenance"]:
            refs = origin["receipts"] if origin["receipts"] != ABSENT else []
            for ref in [*refs, *([origin["media_index"]] if "media_index" in origin else [])]:
                check_digest(actual_source(roots, ref), ref["sha256"], cache)
        for ref in item["licence_rights"]["evidence"]:
            check_digest(actual_source(roots, ref), ref["sha256"], cache)
        previews = item["contact_sheet_thumbnails"]
        for thumbnail in previews if previews != ABSENT else []:
            local = snapshot / thumbnail["path"]
            if local.is_symlink() or local != snapshot / "thumbnails" / f"{thumbnail['sha256']}.jpg":
                reject("invalid_thumbnail_path", local)
            check_digest(local, thumbnail["sha256"], cache)
        yield item, man_sha, path, resolved

def remote_target(remote, root):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*:", remote):
        reject("invalid_remote", remote, "when a configured rclone remote name ending in ':' is supplied", 2)
    parts = PurePosixPath(root)
    if root.startswith("/") or not parts.parts or any(
        part in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]*", part) for part in root.split("/")
    ):
        reject("invalid_remote_root", root, "when a relative Drive folder is supplied", 2)
    return remote + root.rstrip("/")

def object_key(bucket, sha, suffix):
    if not DIGEST.fullmatch(sha):
        reject("invalid_digest", sha)
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        suffix = ".bin"
    return f"{bucket}/{sha[:2]}/{sha}{suffix}"

def drive_files(target, snapshot, database, roots):
    objects = {}
    previews = set()
    entries = []
    withheld = []
    incomplete = []
    records = list(verified(snapshot, database, roots))
    restrictions = {item["sha256"] for item, _, _, _ in records if item["licence_rights"]["drive_policy"] == "withhold"}
    shared = {}
    for item, man_sha, manifest, locators in records:
        asset_id, source_sha = item["asset_id"], item["sha256"]
        if source_sha in restrictions:
            withheld.append(asset_id)
            continue
        viable = []
        for origin in locators["origins"]:
            if all("sha256" in dep and dep["sha256"] not in restrictions for dep in origin["dependencies"]):
                viable.append(origin)
        if not viable:
            incomplete.append(asset_id)
            continue
        source = viable[0]
        object_path = shared.setdefault(source_sha, object_key("objects", source_sha, Path(source["source_file"]).suffix.lower()))
        objects.setdefault(object_path, {"sha256": source_sha, "source": source["source_file"]})
        entry = {"asset_id": asset_id, "kind": item["kind"], "description": item["description"], "tags": item["tags"],
                 "drive_policy": item["licence_rights"]["drive_policy"], "selector": item["selector"],
                 "payload": {"path": object_path, "sha256": source_sha, "source_relative_path": source["path"]},
                 "dependencies": [], "thumbnails": []}
        for dep in source["dependencies"]:
            sha = dep["sha256"]
            key = shared.setdefault(sha, object_key("objects", sha, Path(dep["path"]).suffix.lower()))
            objects.setdefault(key, {"sha256": sha, "source": dep["source_file"]})
            entry["dependencies"].append({"object": key, "sha256": sha, "uri": dep["uri"],
                                          "relative_to": dep["relative_to"], "source_relative_path": dep["path"]})
        for thumb in item["contact_sheet_thumbnails"] if item["contact_sheet_thumbnails"] != ABSENT else []:
            if thumb["source_sha256"] in restrictions:
                continue
            key = object_key("thumbnails", thumb["sha256"], ".jpg")
            previews.add(key)
            objects.setdefault(key, {"sha256": thumb["sha256"], "source": str(snapshot / thumb["path"])})
            entry["thumbnails"].append(key)
        key = object_key("manifests", man_sha, ".json")
        objects[key] = {"sha256": man_sha, "source": str(manifest)}
        entry["manifest"] = key
        entries.append(entry)

    remote_index = {"schema": "viewpoint.asset-drive-index/v1", "snapshot": snapshot.name,
                    "destination": target, "assets": entries}
    remote_data = canonical(remote_index)
    index_key = object_key("indexes", digest(remote_data), ".json")
    plan_data = {"schema": "viewpoint.asset-drive-plan/v1", "snapshot": snapshot.name, "destination": target,
                 "objects": [{"destination": key, **value} for key, value in sorted(objects.items())],
                 "remote_index": {"destination": index_key, "sha256": digest(remote_data)},
                 "withheld_asset_ids": withheld, "incomplete_asset_ids": incomplete}
    return plan_data, remote_data, len(entries), len(previews)

def sync_plan(args, snapshot, database, roots):
    target = remote_target(args.remote, args.root)
    if not args.dry_run:
        reject("dry_run_required", target, "with --dry-run")
    plan_data, remote_data, assets, previews = drive_files(target, snapshot, database, roots)
    index_output = writable(args.index_out or (snapshot.parent.parent / "drive-index.json"))
    plan_output = writable(args.plan_out or (snapshot.parent.parent / "drive-plan.json"))
    if index_output == plan_output:
        reject("output_path_collision", index_output, "when the plan and index output paths are distinct", 2)
    atomic_write(index_output, remote_data)
    atomic_write(plan_output, canonical(plan_data))
    return {"dry_run": True, "network_calls": 0, "snapshot": snapshot.name, "destination": target,
            "assets": assets, "objects": len(plan_data["objects"]), "thumbnails": previews,
            "withheld": len(plan_data["withheld_asset_ids"]), "incomplete": len(plan_data["incomplete_asset_ids"]),
            "remote_index": plan_data["remote_index"],
            "plan_file": str(plan_output), "local_index_file": str(index_output),
            "rclone_versioned_upload_argv": ["rclone", "copy", "--immutable", "<staged-objects>", target],
            "rclone_publish_index_argv": ["rclone", "copyto", "--immutable", "<staged-index>",
                                         target + "/" + plan_data["remote_index"]["destination"]]}

def stage(args):
    destination = writable(args.destination)
    if destination.exists():
        reject("destination_exists", destination, "when the staging destination is new")
    plan = json.loads(Path(args.plan).read_bytes())
    index_data = Path(args.index).read_bytes()
    remote_index = json.loads(index_data)
    if (not isinstance(plan, dict) or not isinstance(remote_index, dict) or
        plan.get("schema") != "viewpoint.asset-drive-plan/v1" or
        remote_index.get("schema") != "viewpoint.asset-drive-index/v1" or
        (plan["snapshot"], plan["destination"]) != (remote_index["snapshot"], remote_index["destination"]) or
        plan["remote_index"] != {"destination": object_key("indexes", digest(index_data), ".json"), "sha256": digest(index_data)}):
        reject("plan_index_mismatch", args.index)
    wanted = set()
    for item in remote_index["assets"]:
        wanted.update([item["payload"]["path"], item["manifest"], *item["thumbnails"]])
        wanted.update(dep["object"] for dep in item["dependencies"])
    seen = set()
    snapshot_id = plan["snapshot"]
    if not DIGEST.fullmatch(snapshot_id):
        reject("invalid_snapshot", snapshot_id)
    with catalogue(writable(args.library)) as (snapshot, database, roots):
        if snapshot.name != snapshot_id:
            reject("stale_drive_plan", snapshot_id, "after regenerating the plan from the active catalogue")
        remote, separator, target_root = plan["destination"].partition(":")
        if not separator:
            reject("invalid_remote", plan["destination"])
        expected, index_bytes, _, _ = drive_files(remote_target(remote + separator, target_root), snapshot, database, roots)
        if plan != expected or index_data != index_bytes:
            reject("drive_plan_mismatch", args.plan, "after regenerating the plan from the active catalogue")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".stage-") as temporary:
            tmp = Path(temporary)
            for entry in plan["objects"]:
                key, sha = entry["destination"], entry["sha256"]
                posix = PurePosixPath(key)
                if (len(posix.parts) != 3 or posix.parts[0] not in {"objects", "manifests", "thumbnails"} or
                        key != object_key(posix.parts[0], sha, posix.suffix) or key in seen or key not in wanted):
                    reject("invalid_drive_object", key)
                source = Path(entry["source"])
                bases = (snapshot / "manifests", snapshot / "thumbnails") if posix.parts[0] != "objects" else tuple(roots.values())
                root = next((folder for folder in bases if source.is_relative_to(folder)), None)
                if not root:
                    reject("invalid_drive_source", source)
                try:
                    source = source_file(root, source.relative_to(root))
                except (OSError, ValueError) as exc:
                    reject("invalid_drive_source", exc)
                output = tmp / "payload" / key
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
                check_digest(output, sha, {})
                seen.add(key)
            if seen != wanted:
                reject("incomplete_drive_plan", "The remote index references unstaged objects.")
            (tmp / "remote-index.json").write_bytes(index_data)
            tmp.rename(destination)
    return {"staged_objects": len(seen), "payload_directory": str(destination / "payload"),
            "remote_index": str(destination / "remote-index.json"), "network_calls": 0}

SHOT_FIELDS = {"character": {"mesh", "studio_mesh", "trellis"}, "rig": {"rig"},
               "motion": {"motion"}, "set": {"set"}, "camera": {"camera"}}

def assembly_error(reason, detail):
    reject(reason, detail, "when the shot list and catalogue meet the assembly interface")

def assemble(args, snapshot, database, roots):
    spec = json.loads(Path(args.spec).read_bytes())
    if not isinstance(spec, dict):
        assembly_error("invalid_shot_list", args.spec)
    fps = spec.get("fps")
    size = spec.get("resolution")
    if (spec.get("schema") != "viewpoint.assemble/v1" or not isinstance(spec.get("episode_id"), str) or
        not spec["episode_id"].strip() or type(fps) is not int or not 1 <= fps <= 240 or
        not isinstance(size, list) or len(size) != 2 or any(type(value) is not int or value <= 0 for value in size) or
        not isinstance(spec.get("shots"), list) or not spec["shots"]):
        assembly_error("invalid_shot_list", args.spec)
    result_shots = []
    seen = set()
    cache = {}
    for shot in spec["shots"]:
        if not isinstance(shot, dict):
            assembly_error("invalid_shot", shot)
        shot_id = shot.get("id")
        start = shot.get("frame_start")
        end = shot.get("frame_end")
        clip = shot.get("motion_clip")
        if not isinstance(shot_id, str) or not shot_id.strip() or shot_id in seen:
            assembly_error("invalid_shot_id", shot_id)
        if type(start) is not int or type(end) is not int or not 1 <= start <= end or not isinstance(clip, str):
            assembly_error("invalid_shot_frames_or_clip", shot_id)
        seen.add(shot_id)
        render_assets = {}
        items = {}
        for field, kinds in SHOT_FIELDS.items():
            ref = shot.get(field)
            if not isinstance(ref, dict) or not isinstance(ref.get("asset_id"), str) or not isinstance(ref.get("sha256"), str):
                assembly_error("invalid_asset_reference", f"{shot_id}/{field}")
            item = get_item(snapshot, database, ref["asset_id"])
            if item["kind"] not in kinds:
                assembly_error("wrong_asset_kind", f"{shot_id}/{field} received {item['kind']}")
            if item["sha256"] != ref["sha256"]:
                assembly_error("shot_pin_mismatch", f"{shot_id}/{field}")
            items[field] = item
            render_assets[field] = resolve_source(item, roots, cache)
            if any("sha256" not in dep for dep in render_assets[field]["origins"][0]["dependencies"]):
                assembly_error("missing_asset_dependency", f"{shot_id}/{field}")
        camera = items["camera"]["technical"]
        if not isinstance(camera, dict) or not isinstance(camera.get("camera"), dict):
            assembly_error("camera_definition_absent", shot_id)
        if camera.get("container_kind") == "set" and items["camera"]["sha256"] != items["set"]["sha256"]:
            assembly_error("camera_set_mismatch", shot_id)
        motion = items["motion"]
        clips = motion["animation_clips"]
        if clip and (clips == ABSENT or clip not in clips):
            assembly_error("unknown_motion_clip", f"{shot_id}/{clip}")
        if not clip and clips != ABSENT and clips:
            assembly_error("motion_clip_required", shot_id)
        if items["rig"]["rig"] != ABSENT and motion["rig"] != ABSENT:
            rig_joints = set(items["rig"]["rig"]["joint_roster"])
            motion_joints = set(motion["rig"]["joint_roster"])
            if rig_joints and motion_joints and not motion_joints.issubset(rig_joints):
                assembly_error("motion_rig_joint_mismatch", shot_id)
        technical = items["set"]["technical"]
        if isinstance(technical, dict) and ("start_frame" in technical and start < technical["start_frame"] or
                                          "end_frame" in technical and end > technical["end_frame"]):
            assembly_error("shot_outside_set_frame_range", shot_id)
        result_shots.append({"id": shot_id, "frame_start": start, "frame_end": end,
                             "motion_clip": clip, "assets": render_assets,
                             "camera_definition": camera})
    return {"schema": "viewpoint.assemble-resolved/v1", "snapshot": snapshot.name,
            "episode_id": spec["episode_id"], "fps": fps, "resolution": size, "shots": result_shots}

class Parser(argparse.ArgumentParser):
    def error(self, message):
        reject("invalid_arguments", message, "when arguments match asset_db.py --help", 2)

def integer(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"expected {low} to {high}")
        return number
    return parse

def parser():
    main = Parser(description=__doc__)
    main.add_argument("--library", required=True, help="catalogue directory")
    main.add_argument("--snapshot", help="read a specific published snapshot digest")
    cmds = main.add_subparsers(dest="command", required=True)
    build = cmds.add_parser("index", help="index the supplied source trees and publish a snapshot")
    build.add_argument("--root", action="append", required=True, help="label=/absolute/path")
    build.add_argument("--jobs", type=integer(1, 8), default=6)
    find = cmds.add_parser("find", help="rank lexical and tag matches")
    find.add_argument("query")
    find.add_argument("--kind", choices=sorted(set(SHOT_FIELDS) | set().union(*SHOT_FIELDS.values()) |
                                              {"render", "contact_sheet", "take", "audio", "material"}))
    find.add_argument("--limit", type=integer(1, 500), default=20)
    for command in ("show", "resolve"):
        select = cmds.add_parser(command)
        select.add_argument("asset_id")
    cmds.add_parser("verify", help="recheck manifest IDs and source, evidence and thumbnail hashes")
    sync = cmds.add_parser("sync", help="plan a Drive mirror offline")
    sync.add_argument("--remote", required=True)
    sync.add_argument("--root", required=True)
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--plan-out")
    sync.add_argument("--index-out")
    staging = cmds.add_parser("stage", help="materialize a verified Drive plan locally")
    staging.add_argument("--plan", required=True)
    staging.add_argument("--index", required=True)
    staging.add_argument("--destination", required=True)
    shot = cmds.add_parser("assemble", help="validate a shot list and resolve its pinned inputs")
    shot.add_argument("spec")
    shot.add_argument("--out")
    return main

def run(args):
    library = writable(args.library)
    if args.snapshot and args.command in {"index", "stage"}:
        reject("snapshot_option_unsupported", args.command, "omit --snapshot for this operation", 2)
    if args.command == "index":
        return index(args, library)
    if args.command == "stage":
        return stage(args)
    with catalogue(library, args.snapshot) as (snapshot, database, roots):
        if args.command == "find":
            return find_assets(args, database)
        if args.command in {"show", "resolve"}:
            item = get_item(snapshot, database, args.asset_id)
            return item if args.command == "show" else resolve_source(item, roots, {}, all_origins=True)
        if args.command == "verify":
            assets = sum(1 for _ in verified(snapshot, database, roots))
            return {"snapshot": snapshot.name, "assets_verified": assets}
        if args.command == "sync":
            return sync_plan(args, snapshot, database, roots)
        result = assemble(args, snapshot, database, roots)
        if args.out:
            atomic_write(writable(args.out), canonical(result))
        return result

def main():
    try:
        result = run(parser().parse_args())
        sys.stdout.buffer.write(canonical(result))
        return 0
    except Refusal as exc:
        retry = shlex.join(["asset_db.py", *sys.argv[1:]])
        sys.stderr.buffer.write(canonical({"reason": exc.reason, "detail": str(exc),
                                           "retry_when": exc.retry_when, "next_command": retry}))
        return exc.rc
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        sys.stderr.buffer.write(canonical({"reason": "harness_error", "detail": str(exc),
                                           "retry_when": "when the local dependency or input is available and valid",
                                           "next_command": shlex.join(["asset_db.py", *sys.argv[1:]])}))
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
