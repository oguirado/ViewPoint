#!/usr/bin/env -S python3 -B
# SPDX-License-Identifier: AGPL-3.0-only
"""Search the curated snapshot and resolve destination-cleared recipes."""
import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

import asset_db as up
import metadata as meta
from asset_sources import allowed_name

ABSENT = meta.ABSENT
RETRY = "when the pinned inputs or required approvals are available"
SCHEMA = """
CREATE TABLE pin (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE curation (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, rights_scope TEXT NOT NULL,
    verdict TEXT NOT NULL, units TEXT NOT NULL, axes TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX facets ON curation(rights_scope, verdict, kind, units, axes);
CREATE TABLE compatibility (
    id TEXT NOT NULL REFERENCES curation(id), skeleton_id TEXT NOT NULL,
    rest_id TEXT NOT NULL, bind_id TEXT NOT NULL
);
CREATE INDEX compatibility_ids ON compatibility(skeleton_id, rest_id, bind_id, id);
CREATE VIRTUAL TABLE curation_fts USING fts5(
    id UNINDEXED, title, description, tags, action, rights, verdict, units, axes,
    skeleton, rest, provenance, tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE clip_fts USING fts5(
    id UNINDEXED, clip UNINDEXED, action, tokenize='porter unicode61'
);
"""


def read_json(path):
    return json.loads(path.read_bytes())


def connect_ro(path):
    db = sqlite3.connect(f"{path.resolve(strict=True).as_uri()}?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    return db


def refuse(reason, detail, retry=RETRY, rc=1):
    up.reject(reason, detail, retry, rc)


write_path = up.writable


def verify(path, sha, cache, reason):
    if not isinstance(sha, str) or not up.DIGEST.fullmatch(sha):
        refuse(reason, f"missing or invalid digest for {path}")
    if not path.is_file() or up.cached_digest(path, cache)[0] != sha:
        refuse(reason, path)
    return path


@contextmanager
def source(snapshot, expected_sha, roots_override=None):
    path = snapshot / "catalog.sqlite3"
    verify(path, expected_sha, {}, "upstream_catalog_drift")
    with closing(connect_ro(path)) as db:
        settings = dict(db.execute("SELECT key,value FROM settings"))
        if settings["snapshot"] != snapshot.name and roots_override is None:
            refuse("upstream_snapshot_identity", snapshot)
        roots = roots_override or {label: Path(root) for label, root in json.loads(settings["roots"]).items()}
        yield db, roots


def catalogue_args(parser):
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--catalog-sha256", required=True)
    parser.add_argument("--trust-root", type=Path)
    parser.add_argument("--authority")
    parser.add_argument("--destination")


def source_settings(args):
    snapshot = args.snapshot.resolve(strict=True)
    if not up.DIGEST.fullmatch(snapshot.name):
        refuse("upstream_snapshot_identity", snapshot, rc=2)
    trust = (args.trust_root, args.authority, args.destination)
    if any(trust) and not all(trust):
        refuse("incomplete_trust_configuration", "trust root, authority and destination are required together", rc=2)
    if all(trust):
        root = args.trust_root.resolve(strict=True)
        if not root.is_dir():
            refuse("invalid_trust_root", root, rc=2)
        trust = (root, args.authority, args.destination)
    return snapshot, args.catalog_sha256, trust


def lookup_asset(snapshot, db, asset_id):
    if up.ASSET_ID.fullmatch(asset_id) and (snapshot / "manifests" / f"{asset_id}.json").is_symlink():
        refuse("manifest_link_forbidden", asset_id)
    item = up.get_item(snapshot, db, asset_id)
    row = db.execute("SELECT kind,source_sha256 FROM asset WHERE id = ?", (asset_id,)).fetchone()
    if (row["kind"], row["source_sha256"]) != (item["kind"], item["sha256"]):
        refuse("manifest_inventory_mismatch", asset_id)
    return item


def read_admissions(path, sha, trust, snapshot_sha):
    if path is None and sha is None:
        return {}
    if path is None or sha is None:
        refuse("unpaired_admission_pin", "supply the admission path and digest together")
    trusted_root, authority, destination = trust
    if not all(trust):
        refuse("trust_configuration_required", "supply a trusted root, authority and destination")
    path = Path(path).absolute()
    if (path != path.resolve() or not path.is_relative_to(trusted_root)
            or not all(allowed_name(part) for part in path.relative_to(trusted_root).parts)):
        refuse("untrusted_admission_path", path, "supply a canonical handoff in the configured trust root")
    if not path.is_file():
        refuse("admission_digest_mismatch", path)
    raw = path.read_bytes()
    if up.digest(raw) != sha:
        refuse("admission_digest_mismatch", path)
    record = json.loads(raw)
    if (record.get("schema") != "rp4.library-admission/v1" or record.get("destination") != destination
            or record.get("catalog_sha256") != snapshot_sha or record.get("verdict_authority") != authority
            or not isinstance(record.get("assets"), dict)
            or record.get("fixture_only", False) is not False):
        refuse("admission_binding_mismatch", path)
    return record["assets"]


def approved(item, entry, roots, cache):
    """Check approval against one exact asset origin and its complete object roster."""
    rights = item["licence_rights"]
    if rights.get("drive_policy") == "withhold":
        refuse("asset_withheld", item["asset_id"])
    first = item["provenance"][0]
    if (entry.get("asset_sha256") != item["sha256"]
            or entry.get("selector") != item["selector"]
            or entry.get("origin") != {"root": first["root"], "path": first["path"]}
            or entry.get("verdict") != "PASS" or entry.get("rights_scope") != "private"
            or not isinstance(entry.get("attribution"), str) or not entry["attribution"].strip()):
        refuse("asset_admission_mismatch", item["asset_id"])
    clips = entry.get("clips")
    if (not isinstance(clips, list) or any(not isinstance(clip, str) or not clip for clip in clips)
            or len(set(clips)) != len(clips)
            or (item["kind"] == "motion" and (
                not clips or not isinstance(item["animation_clips"], list)
                or not set(clips).issubset(item["animation_clips"])))
            or (item["kind"] != "motion" and clips)):
        refuse("clip_admission_mismatch", item["asset_id"])
    records = entry["receipts"]
    if set(records) != {"rights", "verdict"}:
        refuse("asset_approval_receipts_absent", item["asset_id"])
    for kind, record in records.items():
        proof = up.actual_source(roots, record)
        verify(proof, record["sha256"], cache, f"{kind}_receipt_drift")
    return entry


def build_index(destination, snapshot, snapshot_sha, admissions, admission_pin=None, roots_override=None):
    with source(snapshot, snapshot_sha, roots_override) as (db, roots):
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".curation-", delete=False) as stream:
            temporary = Path(stream.name)
        index = None
        try:
            index = sqlite3.connect(temporary)
            index.executescript(SCHEMA)
            pins = {"catalog_sha256": snapshot_sha, "snapshot": snapshot.name,
                    "admission": admission_pin, "schema": "rp4.library-curation/v1"}
            index.executemany("INSERT INTO pin VALUES (?,?)", [(k, json.dumps(v)) for k, v in pins.items()])
            cache = {}
            seen = set()
            for row in db.execute("SELECT * FROM asset ORDER BY id"):
                item = lookup_asset(snapshot, db, row["id"])
                detail = meta.summarize(item, roots, cache)
                admission = admissions.get(row["id"])
                if row["id"] in admissions:
                    approved(item, admission, roots, cache)
                    closure(item, admission, roots, cache)
                    if item["kind"] in {"motion", "rig"} and len(detail["skeletons"]) != 1:
                        refuse("skeleton_evidence_absent", item["asset_id"])
                    seen.add(row["id"])
                withheld = detail["rights"]["upstream_drive_policy"] == "withhold"
                seen_withheld = any(o.get("production_admission") == "WITHHELD" for o in detail["observations"])
                rights = "private" if admission else "withheld" if withheld else "unverified"
                verdict = "PASS" if admission else "WITHHELD" if withheld or seen_withheld else "HOLD"
                index.execute("INSERT INTO curation VALUES (?,?,?,?,?,?,?)", (
                    row["id"], row["kind"], rights, verdict, detail["units"], detail["axes"],
                    up.canonical(detail).decode()))
                index.executemany("INSERT INTO compatibility VALUES (?,?,?,?)", [
                    (row["id"], skin["skeleton_id"], skin["rest_id"], skin["bind_id"])
                    for skin in detail["skeletons"]
                ])
                origins = item["provenance"]
                searchable_actions = detail["actions"]
                if admission:
                    searchable_actions = [a for a in searchable_actions if a["clip"] in admission["clips"]]
                index.executemany("INSERT INTO clip_fts VALUES (?,?,?)", [
                    (row["id"], a["clip"], a["action"]) for a in searchable_actions
                ])
                values = (
                    row["id"], row["title"], row["description"], row["tags"],
                    " ".join(a["action"] for a in searchable_actions),
                    " ".join([rights, *detail["rights"]["recorded_scopes"]]),
                    " ".join([verdict, *(str(r.get("status", r.get("verdict", ""))) for r in detail["observations"])]),
                    detail["units"], detail["axes"],
                    " ".join(s["skeleton_id"] for s in detail["skeletons"]),
                    " ".join(s["rest_id"] for s in detail["skeletons"]),
                    " ".join(o["path"] for o in origins),
                )
                index.execute("INSERT INTO curation_fts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", values)
            if seen != admissions.keys():
                refuse("unknown_admission_asset", sorted(admissions.keys() - seen))
            index.commit()
            index.close()
            index = None
            os.replace(temporary, destination)
        finally:
            if index is not None:
                index.close()
            temporary.unlink(missing_ok=True)


@contextmanager
def open_index(path, snapshot, catalog_sha, trust):
    with closing(connect_ro(path)) as index:
        pins = {key: json.loads(value) for key, value in index.execute("SELECT key,value FROM pin")}
        if (pins["catalog_sha256"] != catalog_sha or pins["snapshot"] != snapshot.name
                or pins["schema"] != "rp4.library-curation/v1"):
            refuse("curation_snapshot_mismatch", path)
        if pins["admission"] is None:
            admissions = {}
        else:
            admissions = read_admissions(*pins["admission"], trust, catalog_sha)
        yield index, admissions


def query_expression(query):
    terms = list(dict.fromkeys(word for word in re.findall(r"[^\W_]+", query.lower()) if word not in up.QUERY_STOP))
    if not 1 <= len(terms) <= 64:
        refuse("invalid_search_query", "supply 1 to 64 searchable words", rc=2)
    return " AND ".join(f'"{word}"' for word in terms)


def search(index, expr, args):
    if not 1 <= args.limit <= 100:
        refuse("invalid_search_limit", "supply a limit from 1 to 100", rc=2)
    rows = index.execute("""
        SELECT c.*, bm25(curation_fts,0,3,2,5,15,1,1,1,1,0.5,0.5,0.3) AS score
        FROM curation_fts JOIN curation c USING (id)
        WHERE curation_fts MATCH :expr
        AND (:rights IS NULL OR c.rights_scope=:rights)
        AND (:require_action=0 OR c.kind != 'motion' OR c.id IN (
            SELECT id FROM clip_fts WHERE clip_fts MATCH :expr))
        AND (:verdict IS NULL OR c.verdict=:verdict)
        AND (:kind IS NULL OR c.kind=:kind)
        AND (:units IS NULL OR c.units=:units)
        AND (:axes IS NULL OR c.axes=:axes)
        AND ((:skeleton IS NULL AND :rest IS NULL) OR EXISTS (
            SELECT 1 FROM compatibility k WHERE k.id=c.id
            AND (:skeleton IS NULL OR k.skeleton_id=:skeleton)
            AND (:rest IS NULL OR k.rest_id=:rest)))
        ORDER BY score,c.id
    """, {**vars(args), "expr": expr, "require_action": args.rights == "private"})
    return rows


def closure(item, entry, roots, cache):
    origin = item["provenance"][0]
    label = origin["root"]
    owner = up.actual_source(roots, origin)
    files = {origin["path"]: item["sha256"]}
    dependencies = {}
    for dep in origin.get("dependencies", []):
        if "sha256" not in dep:
            refuse("missing_asset_dependency", dep.get("uri", ABSENT))
        try:
            recorded = up.actual_source(roots, {"root": label, **dep})
        except up.Refusal as exc:
            if exc.reason == "missing_source":
                refuse("missing_asset_dependency", dep["uri"])
            raise
        if not recorded.is_file() or up.cached_digest(recorded, cache)[0] != dep["sha256"]:
            refuse("missing_or_changed_companion", dep.get("uri", recorded))
        dependency_key = (dep["relative_to"], dep["uri"])
        if dependency_key in dependencies:
            refuse("ambiguous_asset_dependency", dependency_key)
        dependencies[dependency_key] = recorded
        files[dep["path"]] = dep["sha256"]
    verify(owner, item["sha256"], cache, "asset_bytes_drift")
    if owner.suffix.lower() in meta.GLTF | {".obj"}:
        required = [(origin["path"], uri) for uri in up.inspect(owner)[2]]
        visited = set()
        for parent, uri in required:
            key = (parent, uri)
            if key in visited:
                continue
            target = dependencies.get(key)
            actual = up.media_dependency(roots[label] / parent, roots[label], uri, cache)
            if target is None or actual.get("path") != target.relative_to(roots[label]).as_posix():
                refuse("missing_asset_dependency", f"{parent}: {uri}")
            visited.add(key)
            if target.suffix.lower() == ".mtl":
                relative = target.relative_to(roots[label]).as_posix()
                required.extend((relative, link) for link in up.inspect(target)[2])
        if visited != dependencies.keys():
            refuse("unexpected_asset_dependency", item["asset_id"])
    companions = entry["companions"]
    if companions.keys() & files.keys():
        refuse("duplicate_asset_companion", item["asset_id"])
    files.update(companions)
    if files != entry["objects"]:
        refuse("unapproved_dependency_closure", item["asset_id"])
    result = []
    for relative, sha in sorted(files.items()):
        path = up.actual_source(roots, {"root": label, "path": relative})
        verify(path, sha, cache, "missing_or_changed_companion")
        result.append({"root": label, "path": relative, "source": str(path),
                       "sha256": sha, "bytes": path.stat().st_size})
    return result


def compatibility(rig, motion, clip):
    if (rig["units"] == ABSENT or rig["axes"] == ABSENT
            or (rig["units"], rig["axes"]) != (motion["units"], motion["axes"])):
        refuse("motion_rig_space_mismatch", clip)
    rig_skins = rig["skeletons"]
    motion_skins = motion["skeletons"]
    if len(rig_skins) != 1 or len(motion_skins) != 1:
        refuse("skeleton_evidence_absent", clip)
    fixed, moving = rig_skins[0], motion_skins[0]
    for key, reason in (("skeleton_id", "motion_rig_skeleton_mismatch"),
                        ("rest_id", "motion_rig_rest_mismatch"),
                        ("bind_id", "motion_rig_bind_mismatch")):
        if fixed[key] != moving[key]:
            refuse(reason, clip)
    if not isinstance(moving["clips"].get(clip), list):
        refuse("clip_targets_not_compatible", clip)


def authorize_assembly(spec_path, snapshot, db, roots, index, admissions):
    objects = {}
    approvals = {}
    cache = {}
    spec_bytes = spec_path.read_bytes()
    spec = json.loads(spec_bytes)
    for shot in spec["shots"]:
        selected = {}
        for role, kinds in up.SHOT_FIELDS.items():
            ref = shot[role]
            asset_id = ref["asset_id"]
            entry = admissions.get(asset_id)
            pinned = lookup_asset(snapshot, db, asset_id)
            if pinned["kind"] not in kinds:
                refuse("wrong_asset_kind", f"{shot['id']}/{role}")
            if pinned["sha256"] != ref["sha256"]:
                refuse("shot_pin_mismatch", f"{shot['id']}/{role}")
            row = index.execute("SELECT * FROM curation WHERE id=?", (asset_id,)).fetchone()
            if entry is None or row is None or row["rights_scope"] != "private" or row["verdict"] != "PASS":
                refuse("asset_withheld", f"{shot['id']}/{role}/{asset_id}")
            if asset_id not in approvals:
                approved(pinned, entry, roots, cache)
                for obj in closure(pinned, entry, roots, cache):
                    key = (obj["root"], obj["path"])
                    if key in objects and objects[key]["sha256"] != obj["sha256"]:
                        refuse("collection_path_collision", key)
                    objects[key] = obj
                approvals[asset_id] = {field: entry[field] for field in ("receipts", "attribution", "origin")}
            if role == "motion" and shot["motion_clip"] not in entry["clips"]:
                refuse("clip_withheld", shot["motion_clip"])
            if role in {"rig", "motion"}:
                selected[role] = meta.summarize(pinned, roots, cache)
        rig_approval = admissions[shot["rig"]["asset_id"]]
        if rig_approval.get("bound_character_sha256") != shot["character"]["sha256"]:
            refuse("rig_character_binding_mismatch", shot["id"])
        compatibility(selected["rig"], selected["motion"], shot["motion_clip"])
    with tempfile.NamedTemporaryFile(prefix=".recipe-", suffix=".json") as pinned_spec:
        pinned_spec.write(spec_bytes)
        pinned_spec.flush()
        resolved = up.assemble(argparse.Namespace(spec=pinned_spec.name), snapshot, db, roots)
    return resolved, {
        "schema": "rp4.library-assembly/v1", "episode_id": resolved["episode_id"],
        "recipe_sha256": up.digest(spec_bytes),
        "objects": [objects[key] for key in sorted(objects)], "approvals": approvals,
    }


def find_hits(index, db, roots, snapshot, admissions, args):
    matches = []
    cache = {}
    expr = query_expression(args.query)
    for row in search(index, expr, args):
        item = lookup_asset(snapshot, db, row["id"])
        detail = json.loads(row["detail"])
        if args.rights == "private":
            if row["id"] not in admissions:
                refuse("asset_withheld", row["id"])
            approval = approved(item, admissions[row["id"]], roots, cache)
            closure(item, approval, roots, cache)
            if item["kind"] == "motion":
                matching = {r[0] for r in index.execute(
                    "SELECT clip FROM clip_fts WHERE clip_fts MATCH ? AND id=?", (expr, row["id"]))}
                detail["actions"] = [a for a in detail["actions"]
                                     if a["clip"] in matching and a["clip"] in approval["clips"]]
                if not detail["actions"]:
                    refuse("clip_index_drift", row["id"], rc=2)
        hit = {key: row[key] for key in ("id", "kind", "rights_scope", "verdict", "units", "axes", "score")}
        matches.append({**hit, "rank": len(matches) + 1, "title": item["title"],
                        "sha256": item["sha256"], "actions": detail["actions"],
                        "skeletons": detail["skeletons"], "first_origin": item["provenance"][0],
                        "origin_count": detail["origin_count"]})
        if len(matches) == args.limit:
            break
    return {"query": args.query, "rights_filter": args.rights or ABSENT, "results": matches}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        refuse("invalid_arguments", message, "when the command arguments match --help", 2)


def main():
    parser = Parser(description=__doc__)
    catalogue_args(parser)
    parser.add_argument("--index", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--admission", type=Path)
    build.add_argument("--admission-sha256")
    find = commands.add_parser("find")
    find.add_argument("query")
    find.add_argument("--rights", choices=("private", "withheld", "unverified"))
    for field in ("verdict", "kind", "units", "axes", "skeleton", "rest"):
        find.add_argument(f"--{field}")
    find.add_argument("--limit", type=int, default=10)
    show = commands.add_parser("show")
    show.add_argument("asset_id")
    assemble = commands.add_parser("assemble")
    assemble.add_argument("recipe", type=Path)
    assemble.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    snapshot, sha, trust = source_settings(args)
    if args.command == "build":
        admissions = read_admissions(args.admission, args.admission_sha256, trust, sha)
        pin = [str(args.admission.absolute()), args.admission_sha256] if args.admission else None
        build_index(write_path(args.index), snapshot, sha, admissions, pin)
        return {"status": "indexed", "snapshot": snapshot.name, "private_approvals": len(admissions)}
    with source(snapshot, sha) as (db, roots), open_index(args.index, snapshot, sha, trust) as (index, admissions):
        if args.command == "assemble":
            result, receipt = authorize_assembly(args.recipe, snapshot, db, roots, index, admissions)
            if args.receipt:
                up.atomic_write(write_path(args.receipt), up.canonical(receipt))
            return result
        if args.command == "show":
            item = lookup_asset(snapshot, db, args.asset_id)
            row = index.execute("SELECT * FROM curation WHERE id=?", (args.asset_id,)).fetchone()
            if row is None:
                refuse("curation_missing_asset", args.asset_id, rc=2)
            return {**dict(row), "detail": json.loads(row["detail"]), "manifest": item}
        return find_hits(index, db, roots, snapshot, admissions, args)


def run():
    try:
        result = main()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except up.Refusal as exc:
        print(json.dumps({"reason": exc.reason, "retry_when": exc.retry_when,
                          "next_command": [sys.executable, "-B", *sys.argv], "detail": str(exc)},
                         ensure_ascii=False))
        return exc.rc
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, sqlite3.Error) as exc:
        print(json.dumps({"reason": "harness_error", "retry_when": "when the inputs and host are valid",
                          "next_command": [sys.executable, "-B", *sys.argv], "detail": str(exc)},
                         ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
