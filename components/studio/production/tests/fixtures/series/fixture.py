#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Create original neutral scene data and test-only rights witnesses."""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "series"))

from series_contract import PROJECT, READBACK, canonical, digest, entity_pin, file_digest
from series_rights import POLICY

AXES = {"up": "+Z", "forward": "-Y", "handedness": "right"}
LOCAL = "fixture-local"
DRIVE = "fixture-drive"
DESTINATIONS = {LOCAL: "local", DRIVE: "drive"}


def put_json(path, value):
    path.write_bytes(canonical(value))
    return digest(canonical(value))


def build(root):
    root.mkdir()
    project = root / "source"
    parts = project / "parts"
    parts.mkdir(parents=True)
    evidence = root / "evidence"
    evidence.mkdir()

    def receipt(value):
        raw = value if isinstance(value, bytes) else canonical(value)
        pin = digest(raw)
        (evidence / pin).write_bytes(raw)
        return pin

    payloads = {
        "mira": ("character", "mira.obj", b"o Mira\nv 0 0 0\nv 0.4 0 0\nv 0 0 1.5\nf 1 2 3\n"),
        "workshop": ("set", "workshop.obj", b"o Workshop\nv 0 0 0\nv 3 0 0\nv 0 3 0\nf 1 2 3\n"),
        "atlas": ("texture", "atlas.ppm", b"P3\n2 1\n255\n30 140 160  239 182 82\n"),
        "mira-rig": ("rig", "rig.json", {"name": "Mira", "joints": [
            {"id": "root", "parent": None, "position_m": [0, 0, 0]},
            {"id": "arm", "parent": "root", "position_m": [0, 0, 1]},
        ]}),
        "walk-motion": ("motion", "walk.json", {"joint": "root", "fps": 24,
            "translation_m": [[0, [0, 0, 0]], [23, [0, -1, 0]]]}),
        "point-motion": ("motion", "point.json", {"joint": "arm", "fps": 24,
            "rotation_xyz_deg": [[0, [0, 0, 0]], [15, [0, 45, 0]]]}),
    }
    assets, grants = {}, {}
    input_pin = digest(b"Fixture-only source sketch for Mira and the workshop.\n")
    clearance = receipt({"schema": "series-input-clearance/v1", "input_sha256": input_pin,
                         "status": "approved", "destinations": list(DESTINATIONS)})
    for key, (kind, name, value) in payloads.items():
        raw = value if isinstance(value, bytes) else canonical(value)
        pin = digest(raw)
        (parts / name).write_bytes(raw)
        assets[key] = {"path": f"parts/{name}", "sha256": pin, "kind": kind}
        if key == "walk-motion":
            origin = "cc0-mocap"
            proof = receipt({"schema": "series-cc0-review/v1", "asset_sha256": pin,
                "license": "CC0-1.0", "source": "synthetic review witness for the original fixture motion",
                "license_document": receipt(b"Synthetic fixture witness. No external grant is asserted.\n")})
        else:
            origin = "factory-cleared"
            proof = receipt({"schema": "series-generation/v1", "asset_sha256": pin,
                             "inputs": {input_pin: clearance}})
        grants[key] = {"asset_sha256": pin, "origin": origin, "status": "approved",
                       "destinations": list(DESTINATIONS), "receipt": proof}

    character = {"id": "mira", "version": "v1", "identity": "Mira", "asset": "mira",
                 "dependencies": ["atlas"], "rig": "mira-rig", "rig_digest": assets["mira-rig"]["sha256"],
                 "palette": {"coat": "#1E8CA0", "trim": "#EFB652"}, "scale_m": "1.50",
                 "allowed_motions": ["walk-motion", "point-motion"]}
    setting = {"id": "workshop", "version": "v1", "identity": "Workshop", "asset": "workshop",
               "dependencies": ["atlas"], "rig": None, "rig_digest": None,
               "palette": {"wall": "#EFB652"}, "scale_m": "3.00", "allowed_motions": []}

    def appearance(entity, motion):
        return {"id": entity["id"], "version": entity["version"],
                "sha256": entity_pin(entity, AXES, assets), "motion": motion}

    def shot(key, start, end, motion):
        return {"id": key, "start": start, "end": end, "assets": [],
                "characters": [appearance(character, motion)], "set": appearance(setting, None)}

    result = {"schema": PROJECT, "series_id": "mira-workshop", "axes": AXES, "assets": assets,
              "bible": {"characters": [character], "sets": [setting]},
              "episodes": [{"id": "arrival", "fps": 24, "shots": [shot("entry", 0, 24, "walk-motion")]},
                           {"id": "signal", "fps": 24, "shots": [shot("reply", 8, 24, "point-motion")]}]}
    project_pin = put_json(project / "project.json", result)
    policy = {"schema": POLICY, "mode": "fixture", "destinations": DESTINATIONS,
              "inventory_sha256": None, "grants": grants}
    policy_pin = put_json(root / "policy.json", policy)
    return {"project": str(project / "project.json"), "project_sha256": project_pin,
            "policy": str(root / "policy.json"), "policy_sha256": policy_pin,
            "evidence": str(evidence)}


def sample_inventory(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE curation(id TEXT PRIMARY KEY, kind TEXT, rights_scope TEXT, verdict TEXT)")
        db.execute("CREATE VIRTUAL TABLE curation_fts USING fts5(id UNINDEXED, title)")
        db.executemany("INSERT INTO curation VALUES(?, ?, ?, ?)", [
            ("walk-motion", "motion", "unverified", "HOLD"),
            ("unreviewed", "motion", "unverified", "HOLD"),
            ("barred", "motion", "withheld", "WITHHELD"),
        ])
        db.executemany("INSERT INTO curation_fts VALUES(?, ?)", [
            ("walk-motion", "workshop walk"), ("unreviewed", "workshop jump"), ("barred", "workshop clap"),
        ])
    return file_digest(path)


def adapter_readback(root, project):
    return {
        "schema": READBACK,
        "project_sha256": file_digest(root / "project.json"),
        "axes": dict(AXES),
        "assets": {key: file_digest(root / asset["path"]) for key, asset in project["assets"].items()},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="an absent fixture directory under an existing parent")
    print(json.dumps(build(parser.parse_args().root), sort_keys=True))
