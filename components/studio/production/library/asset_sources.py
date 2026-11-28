# SPDX-License-Identifier: AGPL-3.0-only
"""Source selection and evidence binding for local asset collections."""

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path

from asset_formats import ABSENT, JSON_LIMIT, KIND_LABEL, classify

SKIP_DIRS = {
    "node_modules", "__pycache__", "site-packages", "dist-packages", "venv", "virtualenv",
    "wheels", "credentials", "secrets", "auth", "appearance-site",
}
EVIDENCE_FILES = {
    "manifest.json", "receipt.json", "live-receipt.json", "frozen.json", "inputs.json",
    "motion-stage.json", "rig-stage.json", "review.json", "action_plan.json",
    "motion-receipt.json", "license.json", "licence.json", "rights.json", "clips.json",
}
RIG_TAGS = {
    "template-rig": "template rig",
    "unirig-transfer": "unirig transfer rig",
    "articulated": "articulated rig",
    "rigid-skin": "articulated rigid skin rig",
}
RUN_MARKER = re.compile(r"run-[a-z0-9_]+", re.I)
RIGHTS_KEYS = {
    "license", "licence", "license_id", "licence_id", "rights", "attribution",
    "derived_window_export_policy", "export_policy", "commercial_use", "redistribution",
    "licence_text", "licence_text_sha256", "license_text", "license_text_sha256",
    "grant_scope", "source_image", "source_image_sha256",
}

def allowed_name(name):
    lower = name.lower()
    return not (name.startswith(".") or lower in SKIP_DIRS or
                re.search(r"(?:^|[-_.])(?:credentials?|oauth|token|auth|secrets?)(?:[-_.]|$)", lower))

def source_file(root, relative):
    if relative.is_absolute() or not relative.parts or any(part == ".." or not allowed_name(part) for part in relative.parts):
        raise ValueError(f"unsafe source path {relative}")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"source link {relative}")
    if not current.is_file():
        raise FileNotFoundError(current)
    return current

def sha_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def source_rows(roots, base):
    def fail_walk(error):
        raise error

    for label, root in roots.items():
        for folder, dirs, files in os.walk(root, onerror=fail_walk):
            parent = Path(folder)
            dirs[:] = sorted(name for name in dirs if allowed_name(name) and parent / name != base
                             and not (parent / name).is_symlink())
            for name in sorted(files):
                path = parent / name
                if not allowed_name(name) or path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(root)
                kind = classify(relative)
                if kind:
                    yield label, relative, path, kind

@lru_cache(maxsize=512)
def read_evidence(path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > JSON_LIMIT:
            return None
        raw = path.read_bytes()
        data = json.loads(raw)
        if isinstance(data, dict):
            return raw, data, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        return None
    return None

def evidence_rights(data):
    return {key: data[key] for key in sorted(RIGHTS_KEYS & data.keys())}

def pinned_receipt_rights(label, root, data):
    for section in ("inputs", "input_pins", "outputs"):
        fields = data.get(section)
        if not isinstance(fields, dict):
            continue
        for name, pin in fields.items():
            if not re.search(r"(?:licen[cs]e|rights)", name, re.I) or not isinstance(pin, dict):
                continue
            if not isinstance(pin.get("path"), str) or not isinstance(pin.get("sha256"), str):
                continue
            candidate = Path(pin["path"])
            try:
                relative = candidate.relative_to(root)
                payload = read_evidence(source_file(root, relative))
            except (OSError, ValueError):
                continue
            if payload and payload[2] == pin["sha256"] and (terms := evidence_rights(payload[1])):
                yield {"root": label, "path": relative.as_posix(), "sha256": payload[2],
                       "binding": "receipt_rights_pin", "terms": terms}

def licence_bindings(roots):
    rights = {}
    for label, root in roots.items():
        relative = Path("CLIPS.json")
        try:
            payload = read_evidence(source_file(root, relative))
        except (OSError, ValueError):
            continue
        if payload is None:
            continue
        _, data, sha = payload
        for clip in data.get("clips", []):
            if isinstance(clip, dict) and isinstance(clip.get("sha256"), str):
                terms = evidence_rights(clip)
                if terms:
                    rights.setdefault(clip["sha256"], []).append({
                        "root": label, "path": relative.as_posix(), "sha256": sha, "terms": terms,
                        "clip": clip.get("id", ABSENT), "category": clip.get("category", ABSENT),
                    })
    return rights


def media_index(roots):
    result = {}
    for label, root in roots.items():
        relative = Path("MATERIAL_INDEX.json")
        try:
            payload = read_evidence(source_file(root, relative))
        except (OSError, ValueError):
            continue
        if payload is None:
            continue
        _, data, sha = payload
        for clip in data.get("clips", []):
            description = clip.get("summary") or clip.get("description") or clip.get("what_it_shows")
            if not isinstance(description, str):
                description = next((clip[key] for key in ("proves", "claim", "demonstrates")
                                    if isinstance(clip.get(key), str)), ABSENT)
            for file in clip.get("files", []):
                path = Path(file.get("path", ""))
                if path.is_absolute():
                    if not path.is_relative_to(root):
                        continue
                    path = path.relative_to(root)
                result[label, path.as_posix()] = {
                    "root": label, "path": relative.as_posix(), "sha256": sha,
                    "clip": clip.get("clip_id", ABSENT), "role": file.get("role", ABSENT),
                    "description": description,
                }
    return result


def evidence_for(label, relative, root, digest):
    receipts = []
    rights = []
    source = root / relative
    scope = str(relative.parts[0]) if len(relative.parts) > 1 else ABSENT
    run_ids = set(RUN_MARKER.findall(relative.as_posix()))
    parents = [source.parent, *source.parent.parents]
    for parent in parents:
        if not parent.is_relative_to(root) :
            break
        parent_relative = parent.relative_to(root)
        if len(parents) >= 12 and len(parent_relative.parts) > 1 and len(relative.parts) - len(parent_relative.parts) > 4:
            continue
        names = EVIDENCE_FILES | {f"{source.stem}.rights.json", f"{source.stem}.license.json", f"{source.stem}.licence.json"}
        for name in sorted(names | {"MANIFEST.json", "CLIPS.json", "FROZEN.json", "ACTION_PLAN.json"}):
            path = parent / name
            payload = read_evidence(path)
            if payload is None:
                continue
            raw, data, sha = payload
            evidence_right = path.stem.startswith(source.stem + ".") and parent == source.parent
            exact = digest.encode() in raw
            lane_context = len(parent_relative.parts) == 1 and name in {"MANIFEST.json", "receipt.json"}
            if not (exact or evidence_right or lane_context):
                continue
            evidence_path = path.relative_to(root).as_posix()
            reference = {"root": label, "path": evidence_path, "sha256": sha,
                         "binding": "asset_sha256" if exact else "named_sidecar" if evidence_right else "lane_context"}
            if exact or lane_context:
                for key in ("status", "verdict", "production_admission", "admission", "decision"):
                    value = data.get(key)
                    if type(value) in (str, bool, int, float) or (isinstance(value, dict) and len(json.dumps(value)) <= 2048):
                        reference[key] = value
                receipts.append(reference)
            if exact or evidence_right:
                terms = evidence_rights(data)
                if terms:
                    rights.append({**reference, "terms": terms})
                rights.extend(pinned_receipt_rights(label, root, data))
            for key in ("run_id", "run", "attempt_id"):
                value = data.get(key)
                if exact and isinstance(value, str):
                    run_ids.update(RUN_MARKER.findall(value))
    return {"root": label, "path": relative.as_posix(), "unit_or_collection": scope,
            "run_ids": sorted(run_ids) or ABSENT, "receipts": receipts or ABSENT}, rights

def semantic(kind, sources, metadata):
    paths = [entry["path"] for entry in sources]
    variants = [path.lower() for path in paths]
    tags = set(kind.split("_"))
    clauses = []
    supplied = [entry["media_index"]["description"] for entry in sources
                if entry.get("media_index", {}).get("description", ABSENT) != ABSENT]
    tags.update(word for path in variants for word in re.findall(r"[a-z0-9]+", path) if len(word) > 1)
    if kind == "rig":
        for key, label in RIG_TAGS.items():
            if any(key in path.replace("_", "-") for path in variants):
                tags.update(label.split())
                clauses.append(label)
    if kind == "motion":
        if any("keyanim" in path or "keyed" in path for path in variants):
            tags.add("keyed")
        if any("mocap" in path or path.endswith(".bvh") for path in variants):
            tags.add("mocap")
        if metadata.get("clips", ABSENT) != ABSENT:
            for name in metadata["clips"]:
                tags.update(re.findall(r"[a-z0-9]+", name.lower())[:18])
    title = metadata.get("camera_name", Path(paths[0]).stem)
    cleanup = re.sub(r"[_-]+", " ", title)
    qualifier = ", ".join(clauses)
    description = f"{KIND_LABEL[kind].capitalize()} {cleanup}."
    if qualifier:
        description = f"{KIND_LABEL[kind].capitalize()} {cleanup}, {qualifier}."
    if supplied:
        description += f" {supplied[0]}"
    return title, description, sorted(tags), "source_index_and_path_labels" if supplied else "path_and_format_labels"
