# SPDX-License-Identifier: AGPL-3.0-only
"""Series, asset, and continuity contracts shared by the CLI and the gate."""
import hashlib
import json
import re
from decimal import Decimal
from pathlib import PurePosixPath
from stat import S_ISREG

PROJECT = "series-project/v1"
SNAPSHOT = "series-snapshot/v1"
READBACK = "series-readback/v1"
JSON_LIMIT = 16 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
SHA = re.compile(r"[0-9a-f]{64}")
COLOR = re.compile(r"#[0-9A-F]{6}")
SCALE = re.compile(r"(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,15})?")
PATH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}")
WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10))}


class Refusal(Exception):
    def __init__(self, reason, detail):
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail


def need(condition, reason, detail):
    if not condition:
        raise Refusal(reason, detail)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("ascii")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "invalid_json", f"duplicate JSON key {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise Refusal("invalid_json", f"unsupported JSON number {value}")


def parse_json(data, label):
    need(len(data) <= JSON_LIMIT, "document_too_large", label)
    try:
        return json.loads(data, object_pairs_hook=_unique, parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise Refusal("invalid_json", label) from exc


def read_json(path):
    with path.open("rb") as source:
        return parse_json(source.read(JSON_LIMIT + 1), path.name)


def fields(value, keys, label):
    need(isinstance(value, dict) and value.keys() == set(keys.split()),
         "invalid_contract", label)


def matches(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def identifier(value, label):
    need(matches(value, ID), "invalid_identifier", label)


def sha(value, label):
    need(matches(value, SHA), "invalid_sha256", label)


def identifiers(values, label, *, nonempty=False):
    need(isinstance(values, list) and (values or not nonempty), "invalid_contract", label)
    for value in values:
        identifier(value, label)
    need(len(set(values)) == len(values), "duplicate_identifier", label)


def portable_path(value):
    need(isinstance(value, str) and 0 < len(value) <= 240, "invalid_asset_path", repr(value))
    path = PurePosixPath(value)
    need(path.parts and path.as_posix() == value and all(
        matches(part, PATH_PART) and not part.endswith(".")
        and part.split(".", 1)[0].upper() not in WINDOWS_DEVICES for part in path.parts
    ), "invalid_asset_path", value)
    return path


def asset_file(root, relative, label):
    current = root
    for part in portable_path(relative).parts:
        current = current / part
        need(not current.is_symlink(), "asset_symlink", label)
    try:
        regular = S_ISREG(current.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        regular = False
    need(regular, "asset_missing", label)
    return current


def hash_stream(source, destination=None):
    hasher = hashlib.sha256()
    while block := source.read(1024 * 1024):
        hasher.update(block)
        if destination is not None:
            destination.write(block)
    return hasher.hexdigest()


def file_digest(path):
    with path.open("rb") as source:
        return hash_stream(source)


def verify_assets(root, assets):
    for key, asset in sorted(assets.items()):
        path = asset_file(root, asset["path"], key)
        need(file_digest(path) == asset["sha256"], "asset_digest_mismatch", key)


def asset_map(assets):
    need(isinstance(assets, dict) and assets, "invalid_contract", "assets")
    used_paths = {"project.json"}
    for key, asset in assets.items():
        identifier(key, "asset id")
        fields(asset, "kind path sha256", key)
        identifier(asset["kind"], key)
        sha(asset["sha256"], key)
        parts = portable_path(asset["path"]).as_posix().lower().split("/")
        path = "/".join(parts)
        need(path not in used_paths, "asset_path_collision", key)
        used_paths.add(path)
    for path in used_paths:
        parts = path.split("/")
        need(all("/".join(parts[:i]) not in used_paths for i in range(1, len(parts))),
             "asset_path_collision", path)


def axes_contract(value):
    fields(value, "up forward handedness", "axes")
    axis = re.compile(r"[+-][XYZ]")
    need(matches(value["up"], axis) and matches(value["forward"], axis)
         and value["up"][1] != value["forward"][1]
         and value["handedness"] in ("right", "left"), "invalid_axes", "axes")


def dependency(assets, key, label, kind=None):
    identifier(key, label)
    need(key in assets, "asset_reference_missing", f"{label}: {key}")
    if kind is not None:
        need(assets[key]["kind"] == kind, "asset_kind_mismatch", f"{label}: {key}")
    return key


def entity_pin(entity, axes, assets):
    keys = [entity["asset"], *entity["dependencies"], *entity["allowed_motions"]]
    if entity["rig"] is not None:
        keys.append(entity["rig"])
    return digest(canonical({"entity": entity, "axes": axes,
                             "assets": {key: assets[key]["sha256"] for key in keys}}))


def bible_catalog(project):
    bible, assets = project["bible"], project["assets"]
    fields(bible, "characters sets", "bible")
    catalog = {}
    closure = set()
    for group, kind in (("characters", "character"), ("sets", "set")):
        rows = bible[group]
        need(isinstance(rows, list) and rows, "invalid_contract", group)
        records = {}
        for record in rows:
            fields(record, "id version identity asset dependencies rig rig_digest palette scale_m allowed_motions", group)
            identifier(record["id"], group)
            identifier(record["version"], group)
            key = record["id"], record["version"]
            need(key not in records, "duplicate_bible_version", list(key))
            identity = record["identity"]
            need(isinstance(identity, str) and identity.strip() and len(identity) <= 200,
                 "invalid_identity", list(key))
            scale = record["scale_m"]
            need(matches(scale, SCALE) and Decimal(scale) > 0, "invalid_scale", list(key))
            palette = record["palette"]
            need(isinstance(palette, dict) and palette, "invalid_palette", list(key))
            for color_id, color in palette.items():
                identifier(color_id, "palette color")
                need(matches(color, COLOR), "invalid_palette", list(key))
            closure.add(dependency(assets, record["asset"], group, kind))
            if kind == "set" and record["rig"] is None:
                need(record["rig_digest"] is None, "bible_rig_mismatch", list(key))
            else:
                rig = dependency(assets, record["rig"], group, "rig")
                need(record["rig_digest"] == assets[rig]["sha256"], "bible_rig_mismatch", list(key))
                closure.add(rig)
            for field, asset_kind in (("dependencies", None), ("allowed_motions", "motion")):
                identifiers(record[field], field)
                closure.update(dependency(assets, item, field, asset_kind) for item in record[field])
            records[key] = (record, entity_pin(record, project["axes"], assets))
        catalog[group] = records
    return catalog, closure


def project_contract(project):
    fields(project, "schema series_id axes assets bible episodes", "project")
    need(project["schema"] == PROJECT, "unsupported_schema", "project")
    identifier(project["series_id"], "series id")
    axes_contract(project["axes"])
    asset_map(project["assets"])
    return bible_catalog(project)


def continuity(project):
    catalog, closure = project_contract(project)
    episodes = project["episodes"]
    need(isinstance(episodes, list) and episodes, "invalid_contract", "episodes")
    episode_ids = set()
    shots = 0
    for episode in episodes:
        fields(episode, "id fps shots", "episode")
        identifier(episode["id"], "episode id")
        need(episode["id"] not in episode_ids, "duplicate_episode", episode["id"])
        episode_ids.add(episode["id"])
        need(type(episode["fps"]) is int and 0 < episode["fps"] <= 240,
             "invalid_timeline", episode["id"])
        need(isinstance(episode["shots"], list) and episode["shots"], "invalid_timeline", episode["id"])
        seen = set()
        previous_end = 0
        for shot in episode["shots"]:
            fields(shot, "id start end characters set assets", "shot")
            identifier(shot["id"], "shot id")
            label = f'{episode["id"]}/{shot["id"]}'
            need(shot["id"] not in seen, "duplicate_shot", label)
            seen.add(shot["id"])
            start, end = shot["start"], shot["end"]
            need(type(start) is int and type(end) is int and 0 <= previous_end <= start < end,
                 "invalid_timeline", label)
            previous_end = end
            identifiers(shot["assets"], label)
            closure.update(dependency(project["assets"], key, label) for key in shot["assets"])
            match_ref(catalog["sets"], shot["set"], label, "set")
            need(isinstance(shot["characters"], list), "invalid_timeline", label)
            characters = set()
            for appearance in shot["characters"]:
                record = match_ref(catalog["characters"], appearance, label, "character")
                need(record["id"] not in characters, "duplicate_character", label)
                characters.add(record["id"])
            shots += 1
    need(closure == project["assets"].keys(), "asset_outside_closure",
         sorted(project["assets"].keys() - closure))
    return {"series_id": project["series_id"], "episodes": len(episodes),
            "shots": shots, "assets": len(closure)}


def match_ref(catalog, ref, label, kind):
    fields(ref, "id version sha256 motion", label)
    identifier(ref["id"], label)
    identifier(ref["version"], label)
    sha(ref["sha256"], label)
    key = ref["id"], ref["version"]
    need(key in catalog and catalog[key][1] == ref["sha256"],
         f"{kind}_version_mismatch", {"shot": label, "id": key[0], "version": key[1]})
    record = catalog[key][0]
    need(ref["motion"] is None or ref["motion"] in record["allowed_motions"],
         "motion_not_allowed", {"shot": label, "id": key[0], "motion": ref["motion"]})
    return record


def verify_readback(project, observed):
    fields(observed, "schema project_sha256 axes assets", "adapter readback")
    need(observed["schema"] == READBACK, "unsupported_schema", "adapter readback")
    need(observed["project_sha256"] == digest(canonical(project)),
         "adapter_project_mismatch", "adapter readback")
    need(observed["axes"] == project["axes"], "adapter_axes_mismatch", "adapter readback")
    expected = {key: asset["sha256"] for key, asset in project["assets"].items()}
    need(observed["assets"] == expected, "adapter_asset_mismatch", "adapter readback")
