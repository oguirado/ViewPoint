# SPDX-License-Identifier: AGPL-3.0-only
"""Localize frozen judge observations after the blind geometry measurement."""
import hashlib
import json
from pathlib import Path

SCHEMA = "rp4.pose-oracle.sweep.v1"
GAP_CLASSES = ("joint_gap_visible", "seam_see_through")
# Each location is the measured joint, part, sole, or exact collision pair.
CITED_CHECKS = {
    "ankle_gap": (GAP_CLASSES, ("left_ankle", "right_ankle")),
    "wrist_gap": (GAP_CLASSES, ("left_wrist", "right_wrist")),
    "shoulder_socket": (("socket_exposed",), ("left_shoulder", "right_shoulder")),
    "elbow_cut": (GAP_CLASSES, ("left_elbow", "right_elbow")),
    "foot_overlap": (("interpenetration",), ("left_ankle|right_ankle",)),
    "balance_on_tip": (("sole_contact_insufficient",), ("left", "right")),
    "foot_hover": (("sole_not_touching",), ("left", "right")),
}
CITED_TO_ORACLE = {name: list(classes) for name, (classes, _) in CITED_CHECKS.items()}


def corroborate(judge, rec, semantic_of):
    rows = []
    for observation in judge["cited"]:
        classes, locations = CITED_CHECKS.get(observation, ((), ()))
        matches = []
        for failure in rec.get("failures", []):
            where = failure["where"]
            parts = where.split("|")
            anatomy = "|".join(sorted(semantic_of.get(part, part) for part in parts))
            if failure["class"] in classes and anatomy in locations:
                matches.append({"class": failure["class"], "anatomy": anatomy, "where": where,
                                "view": failure.get("view"), "value": failure["value"]})
        if not classes or rec["verdict"] == "NOT_MEASURED":
            status = "UNVERIFIED"
        else:
            status = "CORROBORATED" if matches else "NOT_CORROBORATED"
        rows.append({"pose_id": rec["pose_id"], "movie_frame": judge["movie_frame"],
                     "set_frame": judge["movie_frame"] + 1, "observation": observation,
                     "failure_index_sha256": judge["failure_index_sha256"],
                     "status": status, "expected_classes": list(classes),
                     "expected_anatomy": list(locations), "matches": matches})
    return rows


def cited(failure_index):
    """(take, set frame) -> {"classes": [...], "cited": [...]} for every judge-cited frame."""
    out = {}
    for run in failure_index["runs"]:
        found = {}
        for section in ("requested_classes", "related_controls"):
            for name, entry in run.get(section, {}).items():
                if not isinstance(entry, dict) or "movie_frames" not in entry:
                    continue
                for movie_frame in entry["movie_frames"]:
                    key = (run["run_id"], int(movie_frame) + 1)  # zero-based movie frame -> one-based SET frame
                    found.setdefault(key, {"classes": set(), "cited": []})
                    found[key]["classes"].update(CITED_TO_ORACLE.get(name, []))
                    found[key]["cited"].append(name)
        for key, value in found.items():
            out[key] = {"classes": sorted(value["classes"]), "cited": sorted(value["cited"])}
    return out


def annotate(pose_dir, failure_index_path, reference_digest=None):
    """Write ``expect`` into every historical pose file and return the sweep records."""
    failure_index = json.loads(Path(failure_index_path).read_text(encoding="utf-8"))
    index_sha = hashlib.sha256(Path(failure_index_path).read_bytes()).hexdigest()
    table = cited(failure_index)
    records = []
    for path in sorted(Path(pose_dir).glob("*.pose.json")):
        pose = json.loads(path.read_text(encoding="utf-8"))
        source = pose.get("source", {})
        key = (source.get("take"), int(source.get("set_frame", -1)))
        pose.pop("expect", None)
        pose.pop("judge", None)
        if key in table:
            pose["expect"] = {"verdict": "reject", "classes": table[key]["classes"], "basis": "judge-cited"}
            pose["judge"] = {"cited": table[key]["cited"], "movie_frame": key[1] - 1,
                             "failure_index_sha256": index_sha}
        else:
            pose["expect"] = {"verdict": "none", "basis": "uncited",
                              "note": "no judge observation recorded for this frame; measured, no expectation"}
        data = (json.dumps(pose, sort_keys=True, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        path.write_bytes(data)
        records.append({"pose_id": pose["pose_id"], "family": pose["family"], "file": path.name,
                        "sha256": hashlib.sha256(data).hexdigest(), "plant": pose.get("plant", []),
                        "expect": pose["expect"]})
    digest = hashlib.sha256(json.dumps([[r["pose_id"], r["sha256"]] for r in records], sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    manifest = {"schema": SCHEMA, "generator": "pose-oracle historical", "profile": "historical",
                "failure_index_sha256": index_sha, "cited_to_oracle": CITED_TO_ORACLE,
                "count": len(records), "poses": records, "poses_digest": digest}
    if reference_digest is not None:
        manifest["reference"] = reference_digest
    return manifest
