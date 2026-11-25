#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Hunyuan split-mode Asset Factory refusal and offline evidence entry."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

PROVIDER = "hunyuan3d21"
MODES = ("shape", "paintpbr")
FACTORY = Path(__file__).resolve().parents[2]
BLOCKED = {"disposition": "BLOCKED", "qualified": False, "maturity": "L1"}


class Refused(Exception):
    pass


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    def unique(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("duplicate JSON key")
            data[key] = value
        return data

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def confined(root, label):
    rel = Path(label)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise Refused("artifact_path_invalid")
    target = root / rel
    if target.resolve() != target or not target.is_relative_to(root):
        raise Refused("artifact_path_invalid")
    return target


def run_request(args):
    from asset_factory import contracts, core
    from licence.check import load_policy

    request = read_json(args.request)
    contracts.validate_slot(request, "generate", "request")
    if request["provider_id"] != PROVIDER:
        raise ValueError("unexpected provider")
    mode = request["params"].get("mode")
    if mode not in MODES:
        raise ValueError("unknown mode")

    context = request["context"]
    policy = load_policy(args.factory_root / "config/territory-groups.v1.json")
    source = read_json(args.factory_root / "providers/hunyuan3d21/provider.json")
    contracts.validate_provider(source)
    if source["runnable"] or set(source["territory_excluded"]) != {"EU", "UK", "KR"}:
        raise ValueError("unexpected provider policy")
    excluded = set(source["territory_excluded"])
    excluded.update(country for group in source["territory_excluded"]
                    for country in policy["groups"][group])

    profile = read_json(args.profile)
    if set(profile) != {"schema", "jurisdiction"} or profile["schema"] != "rp4.hunyuan.local-profile/v1":
        raise ValueError("invalid local profile")
    local = profile["jurisdiction"]
    declared = context.get("jurisdiction")
    if local is not None and type(local) is not str:
        raise ValueError("invalid local jurisdiction")
    if context["policy_sha256"] != core.sha256_json(policy):
        code, reason = 4, "territory_policy_stale"
    elif local in excluded or declared in excluded:
        code, reason = 5, "territory_excluded"
    elif local in policy["countries"] and declared is not None and local != declared:
        code, reason = 5, "profile_jurisdiction_mismatch"
    else:
        # Country declarations never authorize this blocked integration build.
        code, reason = 5, "eligibility_unverified"

    result = {"schema": "af.generate/v1", "kind": "result", "slot": "generate",
              "provider_id": PROVIDER, "provider_revision": request["provider_revision"],
              "request_id": request["request_id"], "rc": code, "status": core.STATUS[code],
              "outputs": [], "receipts": [], "metrics": {"wall_ms": None, "peak_rss_bytes": None},
              "absence_reasons": {"outputs": reason, "wall_ms": "inference_not_started",
                                  "peak_rss_bytes": "inference_not_started"}}
    contracts.validate_slot(result, "generate", "result")
    return code, result


def audit_weights(args):
    inventory = read_json(args.inventory)
    pins = inventory["modes"][args.mode]["weights"]
    if not pins:
        raise ValueError("weight pins required")
    checked = []
    root = args.root.resolve(strict=True)
    for pin in pins:
        path = confined(root, pin["relative_path"])
        if not path.is_file():
            raise Refused("missing_local_weight")
        if path.stat().st_size != pin["bytes"] or sha(path) != pin["sha256"]:
            raise Refused("local_weight_mismatch")
        checked.append(pin["relative_path"])
    return 0, {**BLOCKED, "mode": args.mode, "scope": "inventory_only", "checked": checked}


def verify_artifact(root, ref):
    path = confined(root, ref["path"])
    if not path.is_file() or sha(path) != ref["sha256"]:
        raise Refused("artifact_missing_or_stale")
    return path


def embedded_image(doc, blob, texture):
    image = doc["images"][doc["textures"][texture["index"]]["source"]]
    view = doc["bufferViews"][image["bufferView"]]
    offset = view.get("byteOffset", 0)
    data = blob[offset:offset + view["byteLength"]]
    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
        raise Refused("paint_map_encoding_invalid")
    return data


def inspect_candidate(args):
    from asset_factory import contracts, core
    from tools.blender import glb_io

    result = read_json(args.candidate)
    try:
        contracts.validate_slot(result, "generate", "result")
    except core.ContractError as exc:
        raise Refused("candidate_contract_invalid") from exc
    if result["rc"] or result["provider_id"] != PROVIDER:
        raise Refused("candidate_result_ineligible")
    outputs = {item["role"]: item for item in result["outputs"]}
    root = args.root.resolve(strict=True)
    try:
        mesh = verify_artifact(root, outputs["mesh_glb"])
        doc, blob = glb_io.decode_glb(mesh.read_bytes())
    except glb_io.GlbError as exc:
        raise Refused("invalid_candidate_mesh") from exc
    if args.mode == "paintpbr":
        if "texture" not in outputs:
            raise Refused("paint_texture_missing")
        sidecar = verify_artifact(root, outputs["texture"])
        for mesh_entry in doc["meshes"]:
            for primitive in mesh_entry["primitives"]:
                try:
                    if "TEXCOORD_0" not in primitive["attributes"]:
                        raise KeyError("TEXCOORD_0")
                    material = doc["materials"][primitive["material"]]["pbrMetallicRoughness"]
                    albedo = embedded_image(doc, blob, material["baseColorTexture"])
                    metallic_roughness = embedded_image(doc, blob, material["metallicRoughnessTexture"])
                except (KeyError, IndexError, TypeError) as exc:
                    raise Refused("paint_pbr_maps_missing") from exc
                if albedo == metallic_roughness or hashlib.sha256(albedo).hexdigest() != outputs["texture"]["sha256"]:
                    raise Refused("paint_pbr_maps_unbound")
        checks = ["static_glb", "uv", "embedded_albedo", "embedded_metallic_roughness", sidecar.name]
    else:
        checks = ["static_glb"]
    for ref in result["receipts"]:
        verify_artifact(root, ref)
    return 0, {**BLOCKED, "mode": args.mode, "scope": "structural_candidate_check", "checked": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factory-root", type=Path, default=FACTORY)
    sub = parser.add_subparsers(dest="operation", required=True)
    run = sub.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--profile", type=Path, required=True)
    audit = sub.add_parser("audit-weights")
    audit.add_argument("--inventory", type=Path, required=True)
    inspect = sub.add_parser("inspect-candidate")
    inspect.add_argument("--candidate", type=Path, required=True)
    for command in (audit, inspect):
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--mode", choices=MODES, required=True)
    args = parser.parse_args()
    args.factory_root = args.factory_root.resolve(strict=True)
    sys.path.insert(0, str(args.factory_root))
    from asset_factory import core

    actions = {"run": run_request, "audit-weights": audit_weights,
               "inspect-candidate": inspect_candidate}
    try:
        code, result = actions[args.operation](args)
    except Refused as exc:
        code, result = 5, {**BLOCKED, "mode": args.mode, "reason": str(exc)}
    except (OSError, TypeError, ValueError, KeyError, core.ContractError) as exc:
        code, result = 2, {"error": str(exc)}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
