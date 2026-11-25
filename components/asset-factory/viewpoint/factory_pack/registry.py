# SPDX-License-Identifier: AGPL-3.0-only
"""Put the observed chain through the Phase F disk registry and reopen it."""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from asset_factory.core import canonical_json
from tools.registry import Registry


def pin(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def record(work, names):
    missing = "local offline fixture outside release qualification"
    media = {
        "asset.glb": ("mesh", "model/gltf-binary"),
        "rig-action.glb": ("rig", "model/gltf-binary"),
        "simulation.json": ("simulation", "application/json"),
        "edited.blend": ("scene", "application/octet-stream"),
        "preview.mkv": ("preview", "video/x-matroska"),
    }
    contents = {name: (work / name).read_bytes() for name in names}
    taxonomy = b"prop/handle"
    provenance = (work / "job.json").read_bytes()
    blobs = {pin(data): data for data in [*contents.values(), taxonomy, provenance]}
    files = []
    for name in names:
        role, mime = media[name]
        data = contents[name]
        files.append({"digest": pin(data), "role": role, "logical_path": name,
                      "size_bytes": len(data), "media_type": mime})
    value = {
        "schema": "af.asset-record/v1", "record_kind": "asset",
        "canonical_profile": "af.integer-json-lf/v1", "display_name": "Factory fixture",
        "category": "prop", "taxonomy_path": "prop/handle", "annotations": {}, "labels": {},
        "classification": {"taxonomy_digest": pin(taxonomy), "method": "fixture",
                           "confidence_ppm": None, "receipt_digest": None, "absence_reason": missing},
        "dimensions": {"xyz_um": None, "evidence_class": "AUSENTE", "method": "fixture",
                       "receipt_digest": None, "absence_reason": missing},
        "coordinate_frame": "RH_Z_UP_FRONT_NEG_Y_UM",
        "symmetry": dict(kind="unknown", plane_normal_axis=None, plane_offset_um=None, radial_order=None,
                         counterpart_record_digest=None, receipt_digest=None, absence_reason=missing),
        "lineage": dict(operation="source", parent_records=[], parent_asset_digest=None, decomposition=None,
                        assembly_recipe_digest=None, generation_request_digest=None, source_view_digests=[],
                        original_record_digest=None, absence_reasons={}),
        "rights": {"status": "unknown", "components": [
            dict(component="fixture", provenance_digest=pin(provenance), subject_digest=pin(contents["asset.glb"]),
                 licence_expression=None, licence_text_digest=None, commercial=None, territory_excluded=[],
                 gated=None, absence_reason=missing)], "attributions": [], "decision_receipt_digests": []},
        "geometry_digest": pin(contents["asset.glb"]),
        "representations": dict.fromkeys(("cad", "visual", "composition", "collision", "physics", "rig", "motion")),
        "references": None, "files": sorted(files, key=lambda row: (row["role"], row["logical_path"], row["digest"])),
        "instances": [], "sockets": [], "materials": [], "lods": [], "dependencies": sorted(blobs), "gates": [],
    }
    value["representations"]["absence_reasons"] = {}
    return canonical_json(value).encode(), blobs, contents


def main():
    mode, directory, *names = sys.argv[1:]
    work = Path(directory)
    root = work / "registry"
    registry = Registry(root, namespace="factorypack")
    data, blobs, contents = record(work, names)
    locator = "af://factorypack/fixture@" + pin(data)
    if mode == "put":
        root.mkdir()
        snapshot = registry.put("fixture", data, blobs=blobs, expected_snapshot=registry.initialize())
        (work / "registry-snapshot").write_text(snapshot + "\n")
        return
    if mode != "resolve":
        raise ValueError("unknown registry operation")
    snapshot = (work / "registry-snapshot").read_text().strip()
    result = registry.resolve(locator, snapshot=snapshot)
    if result["record"] != json.loads(data) or result["locator"] != locator:
        raise ValueError("registry record mismatch")
    for content in contents.values():
        if registry.read_blob(locator, pin(content), snapshot=snapshot) != content:
            raise ValueError("registry byte mismatch")
    (work / "registry-resolution.json").write_text(json.dumps({
        "snapshot": snapshot, "locator": locator,
        "read_back": {name: pin(content) for name, content in sorted(contents.items())},
    }, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
