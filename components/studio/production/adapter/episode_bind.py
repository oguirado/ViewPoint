#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bind a viewpoint.assemble-resolved/v1 document to an editable, digest-pinned episode.

Pure Python, no Blender. Every field of the bound document is copied from the resolved
document, re-hashed from the bytes it names, or derived from a digest-verified companion.
A refusal names one reason; episode.py prints it as JSON.
"""
import ast
import hashlib
import json
import re
import sqlite3
import struct
import subprocess
import sys
from contextlib import closing
from pathlib import Path

SCHEMA = "viewpoint.episode-bound/v1"
RESOLVED = "viewpoint.assemble-resolved/v1"
INTENT = "viewpoint.edit-intent/v1"
STAGE = "rp4.gem-pipeline.stage/v1"
POLISH = "rp4.studio-animation-polish/v2"
FIELDS = ("character", "rig", "motion", "set", "camera")
KINDS = {"character": {"mesh", "studio_mesh", "trellis"}, "rig": {"rig"}, "motion": {"motion"},
         "set": {"set", "scene"}, "camera": {"camera"}}
CAMERA_KEYS = ("name", "lens_mm", "sensor_width_mm", "location_m", "rotation_euler_rad", "target_m")
CONTRACT = {"CONTACT": "hand_contact", "BOTTOM": "button_bottomed", "POWER": "crt_power_on",
            "HOLD_END": "play_hold_end", "RELEASE": "button_released"}
SOUND = {"hand_contact": "switch press", "button_bottomed": None, "crt_power_on": "crt power-on hum",
         "play_hold_end": None, "button_released": "switch release"}
CAPTION = {"hand_contact": "[click]", "button_bottomed": None, "crt_power_on": "[CRT hums]",
           "play_hold_end": None, "button_released": "[click]"}
CAPTION_HOLD_FRAMES = 36
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
LOCALE = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC",
          "PYTHONDONTWRITEBYTECODE": "1"}


class Refused(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason, self.detail = reason, str(detail)


def need(condition, reason, detail=""):
    if not condition:
        raise Refused(reason, detail)


def canonical(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pin(path):
    path = Path(path)
    return {"path": str(path), "sha256": sha_file(path), "bytes": path.stat().st_size}


def verified(ref, label):
    """A companion is current only while its bytes match the digest that pinned it."""
    path = Path(ref["path"])
    need(path.is_file(), "companion_missing", f"{label}: {path}")
    need(sha_file(path) == ref["sha256"], "stale_companion", f"{label}: {path}")
    return path


def document(path):
    try:
        return json.loads(Path(path).read_bytes())
    except (ValueError, OSError) as exc:
        raise Refused("invalid_document", f"{path}: {exc}") from None


# ------------------------------------------------------------------ GLB, read without an importer

def glb(path):
    """The JSON document of one GLB together with its binary chunk."""
    data = Path(path).read_bytes()
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    need(magic == b"glTF" and version == 2 and length == len(data), "invalid_glb", path)
    chunks, offset = {}, 12
    while offset < length:
        size, kind = struct.unpack_from("<I4s", data, offset)
        chunks[kind] = data[offset + 8:offset + 8 + size]
        offset += 8 + size
    need(b"JSON" in chunks, "invalid_glb", path)
    return json.loads(chunks[b"JSON"]), chunks.get(b"BIN\x00", b"")


def glb_view(doc, binary, index):
    view = doc["bufferViews"][index]
    start = view.get("byteOffset", 0)
    return binary[start:start + view["byteLength"]]


def glb_accessor(doc, binary, index):
    accessor = doc["accessors"][index]
    width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[accessor["type"]]
    size = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 8}[accessor["componentType"]]
    # The exporters of these files write tightly packed accessors; a stride would be refused below.
    need(doc["bufferViews"][accessor["bufferView"]].get("byteStride", width * size) == width * size,
         "glb_accessor_strided", index)
    offset = accessor.get("byteOffset", 0)
    block = glb_view(doc, binary, accessor["bufferView"])
    return block[offset:offset + accessor["count"] * width * size]


def glb_skeleton(doc, binary):
    """Rest pose of the skin joints and the inverse bind matrices, as two digests."""
    need(len(doc.get("skins", [])) == 1, "glb_skin_count", len(doc.get("skins", [])))
    skin = doc["skins"][0]
    rest = [{key: doc["nodes"][node].get(key) for key in ("name", "translation", "rotation", "scale")}
            for node in skin["joints"]]
    return {"joints": [node["name"] for node in rest], "rest_pose_sha256": digest(canonical(rest)),
            "inverse_bind_sha256": digest(glb_accessor(doc, binary, skin["inverseBindMatrices"]))}


def glb_look(doc, binary):
    return [digest(glb_view(doc, binary, image["bufferView"])) for image in doc.get("images", [])
            if "bufferView" in image]


# ------------------------------------------------------------------ catalogue and receipts

class Catalogue:
    """Read-only use of the asset catalogue through its own entry point."""

    def __init__(self, library):
        self.library = Path(library).resolve(strict=True)
        self.script = Path(__file__).resolve().parents[1] / "library" / "asset_db.py"
        self.cache = {}

        pointer = (self.library / "CURRENT").read_text(encoding="utf-8").strip()
        need(DIGEST.fullmatch(pointer), "catalogue_snapshot_invalid", pointer)
        self.snapshot = pointer
        source = self.library / "snapshots" / pointer / "catalog.sqlite3"
        with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
            row = db.execute("SELECT value FROM settings WHERE key='roots'").fetchone()
        need(row is not None, "catalogue_roots_absent", source)
        self.roots = {label: Path(root) for label, root in json.loads(row[0]).items()}

    def path_of(self, ref):
        path = Path(ref["path"])
        if path.is_absolute():
            return path
        need(ref.get("root") in self.roots, "receipt_root_unknown", f"{ref.get('root')} for {path}")
        base = self.roots[ref["root"]]
        candidate = base / path
        need(candidate.resolve().is_relative_to(base), "receipt_path_outside_root", path)
        return candidate

    def run(self, *args):
        done = subprocess.run([sys.executable, "-B", str(self.script), "--library", str(self.library),
                               "--snapshot", self.snapshot, *args], capture_output=True, env=LOCALE)
        if done.returncode:
            try:
                refusal = json.loads(done.stderr)
                raise Refused(refusal["reason"], refusal.get("detail", ""))
            except (ValueError, KeyError):
                raise Refused("catalogue_error", done.stderr.decode(errors="replace")[-400:]) from None
        return json.loads(done.stdout)

    def manifest(self, asset_id):
        if asset_id not in self.cache:
            self.cache[asset_id] = self.run("show", asset_id)
        return self.cache[asset_id]


def certified(catalogue, manifest, asset_sha, stage):
    """The stage receipt that certified these exact bytes, re-hashed against its catalogue pin."""
    for origin in manifest["provenance"]:
        for ref in origin.get("receipts", []):
            if ref.get("binding") != "asset_sha256" or ref.get("status") != "PASS":
                continue
            path = verified({"path": str(catalogue.path_of(ref)), "sha256": ref["sha256"]},
                            f"receipt of {manifest['asset_id'][:14]}")
            data = document(path)
            if data.get("schema") == STAGE and data.get("stage") == stage and data.get("status") == "PASS" \
                    and (data.get("outputs") or {}).get(stage, {}).get("sha256") == asset_sha:
                return path, data, ref
    return None, None, None


def contract_constants(path):
    """The contact contract of the pinned script, read as literals and never run."""
    values = {}
    for node in ast.parse(Path(path).read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign):
            continue
        names = [item.id for target in node.targets for item in (target.elts if isinstance(target, ast.Tuple) else [target])
                 if isinstance(item, ast.Name)]
        try:
            literal = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
        if isinstance(literal, tuple) and len(literal) == len(names):
            values.update(dict(zip(names, literal)))
    need(all(type(values.get(key)) is int for key in CONTRACT), "contact_contract_absent", path)
    return {key: values[key] for key in CONTRACT}


# ------------------------------------------------------------------ one shot

def resolve_assets(shot):
    assets = {}
    for field in FIELDS:
        asset = (shot.get("assets") or {}).get(field)
        need(isinstance(asset, dict) and isinstance(asset.get("sha256"), str) and DIGEST.fullmatch(asset["sha256"]),
             "invalid_asset_reference", f"{shot.get('id')}/{field}")
        need(asset.get("kind") in KINDS[field], "wrong_asset_kind",
             f"{shot.get('id')}/{field} received {asset.get('kind')}")
        origins = asset.get("origins") or []
        need(origins and isinstance(origins[0].get("source_file"), str), "asset_origin_absent", f"{shot.get('id')}/{field}")
        path = Path(origins[0]["source_file"])
        need(path.is_file() and sha_file(path) == asset["sha256"], "asset_digest_mismatch", f"{shot.get('id')}/{field}")
        assets[field] = {"asset_id": asset["asset_id"], "kind": asset["kind"], "sha256": asset["sha256"],
                         "selector": asset.get("selector"), "path": str(path)}
    return assets


def camera_binding(shot_id, shot, registration, assets):
    definition = shot.get("camera_definition")
    need(isinstance(definition, dict) and isinstance(definition.get("camera"), dict), "camera_ambiguous",
         f"{shot_id}: the resolved shot names no single camera")
    need(definition.get("container_kind") == "set", "camera_container_unsupported", shot_id)
    camera = definition["camera"]
    need(all(key in camera for key in CAMERA_KEYS), "camera_definition_incomplete", shot_id)
    need(definition.get("camera_name") == camera["name"], "camera_ambiguous",
         f"{shot_id}: {definition.get('camera_name')!r} and {camera['name']!r} name different cameras")
    declared = registration.get("camera")
    need(isinstance(declared, dict) and declared.get("name") == camera["name"], "camera_ambiguous",
         f"{shot_id}: the SET registers {type(declared).__name__}, not one camera named {camera['name']!r}")
    need(declared == camera, "camera_registration_mismatch", shot_id)
    need(assets["camera"]["sha256"] == assets["set"]["sha256"], "camera_set_mismatch", shot_id)
    return {key: camera[key] for key in CAMERA_KEYS} | {
        "source": "SET.camera", "source_sha256": assets["camera"]["sha256"],
        "camera_uv_origin": camera.get("camera_uv_origin"),
        "screen_center_uv": (camera.get("witness_uv") or {}).get("screen_center")}


def set_producer(shot_id, assets, set_path, catalogue, companions):
    """set.py beside the registration, pinned by the lane manifest the catalogue recorded for the SET."""
    manifest = catalogue.manifest(assets["set"]["asset_id"])
    found = None
    for ref in manifest["provenance"][0].get("receipts", []):
        if ref.get("binding") != "asset_sha256":
            continue
        path = verified({"path": str(catalogue.path_of(ref)), "sha256": ref["sha256"]}, "SET lane receipt")
        files = document(path).get("files")
        rows = [row for row in files if isinstance(row, dict) and row.get("path") == "set.py"] if isinstance(files, list) else []
        if len(rows) == 1:
            found = (path, rows[0], ref)
    need(found is not None, "set_producer_unpinned", f"{shot_id}: no pinned lane manifest lists set.py")
    producer = set_path.with_name("set.py")
    need(producer.is_file() and sha_file(producer) == found[1]["sha256"], "stale_companion",
         f"set.py: {sha_file(producer) if producer.is_file() else 'missing'} against lane manifest {found[1]['sha256']}")
    companions += [{"role": "set_producer", "shot": shot_id, **pin(producer), "pinned_by": "SET lane manifest"},
                   {"role": "set_lane_manifest", "shot": shot_id, **pin(found[0]), "pinned_by": found[2]["binding"]}]


def bound_geometry(shot_id, assets, motion, rig, character, catalogue, companions):
    """The motion drives the selected rig, and that rig carries the selected character's look."""
    manifest = catalogue.manifest(assets["rig"]["asset_id"])
    path, receipt, ref = certified(catalogue, manifest, assets["rig"]["sha256"], "rig")
    need(path is not None, "rig_receipt_absent", f"{shot_id}: no rig stage receipt certifies these bytes")
    need((receipt["inputs"].get("selected") or {}).get("sha256") == assets["character"]["sha256"],
         "rig_does_not_bundle_character", f"{shot_id}: the rig was fitted from another LOOK")
    companions.append({"role": "rig_receipt", "shot": shot_id, **pin(path), "pinned_by": ref["binding"]})
    skeleton, rig_skeleton = glb_skeleton(motion[0], motion[1]), glb_skeleton(rig[0], rig[1])
    need(skeleton == rig_skeleton, "rig_motion_rest_mismatch", f"{shot_id}: motion and rig rest skeletons differ")
    look = glb_look(character[0], character[1])
    need(look and look == glb_look(motion[0], motion[1]) == glb_look(rig[0], rig[1]),
         "bound_geometry_look_mismatch", f"{shot_id}: rig-bound atlases differ from the character's")
    return skeleton, look


def motion_contract(shot_id, assets, catalogue, companions):
    """Prop state, pose replay and stance events from the receipts that certified the motion bytes."""
    manifest = catalogue.manifest(assets["motion"]["asset_id"])
    path, receipt, ref = certified(catalogue, manifest, assets["motion"]["sha256"], "motion")
    need(path is not None, "prop_contract_absent", f"{shot_id}: no motion stage receipt certifies these bytes")
    companions.append({"role": "motion_receipt", "shot": shot_id, **pin(path), "pinned_by": ref["binding"]})
    need((receipt["inputs"].get("rig") or {}).get("sha256") == assets["rig"]["sha256"], "rig_motion_mismatch", shot_id)
    need((receipt["inputs"].get("set") or {}).get("sha256") == assets["set"]["sha256"], "motion_set_mismatch", shot_id)
    script = verified(receipt["inputs"]["animation_polish_script"], "animation polish script")
    companions.append({"role": "contact_contract_script", "shot": shot_id, **pin(script),
                       "pinned_by": "motion receipt input"})
    outputs = receipt["outputs"]
    data_path = verified(outputs["animation_polish_motion"], "motion document")
    report_path = verified(outputs["motion_report"], "motion report")
    companions += [{"role": "motion_document", "shot": shot_id, **pin(data_path), "pinned_by": "motion receipt output"},
                   {"role": "motion_report", "shot": shot_id, **pin(report_path), "pinned_by": "motion receipt output"}]
    data, report = document(data_path), document(report_path)
    need(data.get("schema") == POLISH and data.get("status") == "PASS" and data.get("reasons") == []
         and data.get("motion_sha256") == assets["motion"]["sha256"], "motion_document_binding", shot_id)
    need(len(data["hand_world_m"]) == len(data["root_world_m"]) and len(data["hand_vertex_indices"]) == 4
         and isinstance(report.get("stance"), dict), "motion_document_incomplete", shot_id)
    return contract_constants(script), data, report


def prop_state(shot_id, registration, frames):
    markers, button, screen = registration["markers"], registration["button"], registration["screen"]
    need((markers.get("HAND_CONTACT"), markers.get("BUTTON_BOTTOMED"), markers.get("BUTTON_RELEASED")) ==
         (frames["CONTACT"], frames["BOTTOM"], frames["RELEASE"]), "set_cue_mismatch", shot_id)
    return {"PLAY Button": {"contact": frames["CONTACT"], "bottomed": frames["BOTTOM"],
                           "hold_end": frames["HOLD_END"], "release": frames["RELEASE"], "travel_m": button["travel_m"]},
            "CRT Screen": {"power_on": frames["POWER"], "off": screen["off_emission_strength"],
                           "on": screen["on_emission_strength"]}}


def check_ranges(shot_id, source, clip_frames, end_frame, prop):
    for name in ("record_in", "record_out"):
        need(1 <= source[name] <= clip_frames, "record_outside_clip", f"{shot_id}/{name}")
        need(1 <= source[name] <= end_frame, "record_outside_set", f"{shot_id}/{name}")
    button, screen = prop["PLAY Button"], prop["CRT Screen"]
    need(button["bottomed"] <= screen["power_on"], "early_screen_activation",
         f"{shot_id}: the CRT powers on at {screen['power_on']} before the cap bottoms at {button['bottomed']}")
    need(1 < button["contact"] < button["bottomed"] <= screen["power_on"] <= button["hold_end"]
         < button["release"] <= end_frame, "prop_state_order", shot_id)


def shot_events(shot_id, motion_sha, frames, report):
    events = [{"id": name, "motion_sha256": motion_sha, "source_frame": frames[key], "shot": shot_id,
               "sound": SOUND[name], "caption": CAPTION[name]} for key, name in CONTRACT.items()]
    for side, stances in report["stance"].items():
        events += [{"id": f"footfall_{side}_{number}", "motion_sha256": motion_sha, "source_frame": row["frames"][0] + 1,
                    "shot": shot_id, "sound": f"footfall {side}", "caption": None}
                   for number, row in enumerate(stances) if row["frames"][0] + 1 > 1]
    # Stance ranges of the motion report are zero-based and half-open; these are one-based and inclusive.
    stances = [{"side": side, "source_in": row["frames"][0] + 1, "source_out": row["frames"][1]}
               for side, rows in report["stance"].items() for row in rows]
    return sorted(events, key=lambda event: (event["source_frame"], event["id"])), stances


def bind_shot(number, shot, wish, resolved, catalogue, entry):
    shot_id = shot.get("id")
    need(isinstance(shot_id, str) and shot_id.strip(), "invalid_shot_id", shot_id)
    start, end = shot.get("frame_start"), shot.get("frame_end")
    need(type(start) is int and type(end) is int and 1 <= start <= end, "invalid_shot_frames", shot_id)
    assets = resolve_assets(shot)
    set_path = Path(assets["set"]["path"])
    need(set_path.suffix == ".json", "set_registration_absent", f"{shot_id}: {set_path.name}")
    registration = document(set_path)
    need(registration.get("start_frame") == 1 and type(registration.get("end_frame")) is int
         and all(isinstance(registration.get(key), dict) for key in ("markers", "button", "screen")),
         "set_registration_invalid", shot_id)
    need(resolved["fps"] == registration.get("fps"), "edit_rate_requires_retiming",
         f"episode {resolved['fps']} fps against a {registration.get('fps')} fps source: retiming is not implemented")
    camera = camera_binding(shot_id, shot, registration, assets)
    companions = []
    set_producer(shot_id, assets, set_path, catalogue, companions)
    motion, rig, character = glb(assets["motion"]["path"]), glb(assets["rig"]["path"]), glb(assets["character"]["path"])
    skeleton, look = bound_geometry(shot_id, assets, motion, rig, character, catalogue, companions)
    clips = {item.get("name"): item for item in motion[0].get("animations", [])}
    clip = shot.get("motion_clip")
    need(isinstance(clip, str) and clip in clips, "motion_clip_missing", f"{shot_id}/{clip}")
    counts = {motion[0]["accessors"][sampler["input"]]["count"] for sampler in clips[clip]["samplers"]}
    need(len(counts) == 1, "motion_clip_uneven", f"{shot_id}/{clip}")
    clip_frames = counts.pop()
    frames, data, report = motion_contract(shot_id, assets, catalogue, companions)
    # The certified document replays the milestone clip only; another clip has no replay to check against.
    need(len(data["hand_world_m"]) == clip_frames, "pose_replay_unavailable",
         f"{shot_id}: the certified motion document covers {len(data['hand_world_m'])} frames, the clip {clip} has {clip_frames}")
    prop = prop_state(shot_id, registration, frames)
    if "prop_state" in wish:
        need(wish["prop_state"] == prop, "prop_state_override_mismatch", shot_id)
    head, tail = wish.get("handle_head", 0), wish.get("handle_tail", 0)
    need(type(head) is int and type(tail) is int and head >= 0 and tail >= 0, "invalid_handles", shot_id)
    source = {"in": start, "out": end, "handle_head": head, "handle_tail": tail,
              "record_in": start - head, "record_out": end + tail}
    events, stances = shot_events(shot_id, assets["motion"]["sha256"], frames, report)
    check_ranges(shot_id, source, clip_frames, registration["end_frame"], prop)
    transition = wish.get("transition_in", "start" if number == 0 else "cut")
    need(transition == ("start" if number == 0 else "cut"), "transition_unsupported", f"{shot_id}: {transition}")
    bound = {
        "index": number, "id": shot_id, "label": wish.get("label", shot_id),
        "edit_start": None, "edit_end": None, "transition_in": transition, "source": source,
        "assets": assets, "clip": {"name": clip, "frames": clip_frames, "channels": len(clips[clip]["channels"])},
        "rig_binding": {"joints": skeleton["joints"], "rest_pose_sha256": skeleton["rest_pose_sha256"],
                        "inverse_bind_sha256": skeleton["inverse_bind_sha256"], "look_sha256": look,
                        "hand_vertex_indices": data["hand_vertex_indices"]},
        "camera": camera, "prop_state": prop, "set_end_frame": registration["end_frame"],
        "pose_replay": {"hand_world_m": data["hand_world_m"], "root_world_m": data["root_world_m"]},
        "events": events, "stances": stances}
    return bound, companions


def caption_plan(cues, total_frames):
    """Merge simultaneous cues and end each caption before the next onset."""
    captions = []
    for cue in sorted(cues, key=lambda row: row["edit_frame"]):
        if not cue["caption"]:
            continue
        frame = cue["edit_frame"]
        if captions and captions[-1]["start"] == frame:
            captions[-1]["cues"].append(cue["id"])
            captions[-1]["text"] += " " + cue["caption"]
        else:
            captions.append({"start": frame, "cues": [cue["id"]], "text": cue["caption"]})
    for number, caption in enumerate(captions):
        next_start = captions[number + 1]["start"] if number + 1 < len(captions) else total_frames
        caption["end"] = min(caption["start"] + CAPTION_HOLD_FRAMES, next_start) - 1
    return captions


def layout(shots, fps):
    """Edit ranges are contiguous and in array order; cues land where their source frame was cut in."""
    need(shots, "episode_has_no_shots")
    seen, cues, omitted, start = set(), [], [], 0
    for shot in shots:
        need(shot["id"] not in seen, "duplicate_shot_id", shot["id"])
        seen.add(shot["id"])
        shot["edit_start"], shot["edit_end"] = start, start + shot["source"]["out"] - shot["source"]["in"]
        start = shot["edit_end"] + 1
        for event in shot["events"]:
            inside = shot["source"]["in"] <= event["source_frame"] <= shot["source"]["out"]
            (cues if inside else omitted).append(event if inside else f"{shot['id']}:{event['id']}")
    for cue in cues:
        cue["edit_frame"] = next(shot["edit_start"] + cue["source_frame"] - shot["source"]["in"] for shot in shots
                                 if shot["id"] == cue["shot"])
        cue["time_s"] = round(cue["edit_frame"] / fps, 6)
    return {"total_edit_frames": start, "cues": cues, "cues_omitted": omitted, "captions": caption_plan(cues, start)}


def roster(resolved, intent):
    resolved_ids = [shot.get("id") for shot in resolved["shots"]]
    wanted = [shot.get("id") for shot in intent.get("shots") or []]
    need(wanted and len(set(wanted)) == len(wanted), "edit_intent_shots", "the edit intent lists each shot once")
    need(len(set(resolved_ids)) == len(resolved_ids), "duplicate_shot_id", resolved_ids)
    for shot_id in wanted:
        need(shot_id in resolved_ids, "edit_shot_dropped", f"{shot_id} is in the edit and absent from the resolved document")
    for shot_id in resolved_ids:
        need(shot_id in wanted, "edit_shot_unexpected", f"{shot_id} is resolved and absent from the edit")
    need(resolved_ids == wanted, "edit_shot_reordered", f"resolved {resolved_ids} against edit {wanted}")


def bind(resolved_path, intent_path, catalogue, entry):
    resolved, intent = document(resolved_path), document(intent_path)
    need(isinstance(resolved, dict) and resolved.get("schema") == RESOLVED, "resolved_schema", resolved_path)
    need(resolved.get("snapshot") == catalogue.snapshot, "catalogue_snapshot_changed", resolved_path)
    fps, pixels = resolved.get("fps"), resolved.get("resolution")
    need(type(fps) is int and 1 <= fps <= 240 and isinstance(pixels, list) and len(pixels) == 2
         and all(type(value) is int and value > 0 for value in pixels), "resolved_schema", resolved_path)
    need(isinstance(resolved.get("shots"), list) and resolved["shots"], "resolved_schema", resolved_path)
    need(isinstance(intent, dict) and intent.get("schema") == INTENT, "edit_intent_schema", intent_path)
    need(intent.get("episode_id") == resolved.get("episode_id"), "edit_intent_episode", intent.get("episode_id"))
    roster(resolved, intent)
    companions, shots = [], []
    for number, (shot, wish) in enumerate(zip(resolved["shots"], intent["shots"])):
        need(isinstance(shot, dict) and isinstance(wish, dict), "invalid_shot", number)
        bound, found = bind_shot(number, shot, wish, resolved, catalogue, entry)
        shots.append(bound)
        companions += found
    episode = layout(shots, fps)
    return {
        "schema": SCHEMA, "episode_id": resolved["episode_id"],
        "resolved_document": pin(resolved_path), "edit_intent": pin(intent_path),
        "catalogue": {"library": str(catalogue.library), "script_sha256": sha_file(catalogue.script),
                      "snapshot": resolved.get("snapshot")},
        "timebase": {"source_fps": fps, "edit_rate_fps": fps, "source_index_origin": 1, "edit_index_origin": 0},
        "pixels": pixels, "shots": shots, "companions": companions,
        "cues": episode["cues"], "cues_omitted": episode["cues_omitted"], "captions": episode["captions"],
        "total_edit_frames": episode["total_edit_frames"], "entry_point": entry}
