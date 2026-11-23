"""Read-only production UV/map verification; appearance remains a later gate.

F2's integrity reader intentionally cannot admit factory assets. This owner
adds a closed factory record, caller pins and semantic gate replay. It neither
changes that reader nor turns a map bundle into an approved finished asset.
"""
from pathlib import Path
import shutil
import tempfile

from asset_factory import core
from tools.blender import bake, uv, orm
from tools.blender.bake_support import read_bytes, require_receipt, result, upstream
from .contracts import BAKE_VERIFICATION
from .engine import Refusal, fault, file_ref, load, require


def _ok(record):
    require(type(record) is dict and type(record.get("rc")) is int
            and record["rc"] in core.RC, 2, "bundle_contract_invalid")
    require(record["rc"] == 0, record["rc"] or 2,
            (record.get("reason_codes") or ["bundle_refused"])[0])
    return record


@bake._bounded
def verify_bundle(bundle, *, expected_receipt_sha256, expected_high_sha256,
                  expected_low_sha256, expected_policy, timeout_s=300):
    """A successful result certifies production UV/maps, never appearance.

    All writers used for semantic replay receive private scratch. The original
    bundle is authenticated again afterwards and is never modified on refusal.
    """
    try:
        root = Path(bundle)
        _ok(bake.verify_bundle(root, expected_receipt_sha256=expected_receipt_sha256))
        rec = load(root / "bundle_receipt.json")
        tool = upstream("bake_pbr")
        names = {"source_high.glb", "source_low.glb", "low_uv.glb", "orm.png", "covered_albedo.png",
                 "bake_receipt.json", "uv_receipt.json", "orm_receipt.json", "pbr_receipt.json",
                 "ram_receipt.json"} | {name + ".png" for name in tool.MAPS}
        require({f["path"] for f in rec["files"]} == names and len(rec["files"]) == len(names),
                3, "production_bundle_incomplete")
        require(rec.get("runner") == {"kind": "scoped-cpu", "identity": "scoped_runner"},
                3, "injected_bake_runner")
        require(type(rec.get("checks_evaluated")) is int and rec["checks_evaluated"] > 0,
                3, "bundle_zero_checks")
        require(type(expected_policy) is dict and set(expected_policy) ==
                {"res", "margin_px", "cage_extrusion_um", "max_stretch_permille", "timeout_s"},
                2, "bundle_policy_invalid")
        require(rec.get("policy") == expected_policy, 4, "bundle_policy_stale")
        require(file_ref(root / "source_high.glb")["sha256"] == expected_high_sha256
                and file_ref(root / "source_low.glb")["sha256"] == expected_low_sha256,
                4, "bundle_source_stale")
        records = {name: load(root / (name + "_receipt.json")) for name in ("bake", "uv", "orm", "pbr")}
        # The first receipt binds both input snapshots; each following receipt
        # binds the exact predecessor bytes as well as its own semantic inputs.
        require(records["bake"].get("prev_receipt_sha256") is None, 4, "bake_predecessor_stale")
        checks = bake._check_bake(records["bake"], tool, root, root / "source_high.glb",
                                 root / "source_low.glb", expected_policy["res"])
        require(records["bake"].get("params", {}).get("margin_px") == expected_policy["margin_px"]
                and records["bake"].get("params", {}).get("cage_extrusion_um") == expected_policy["cage_extrusion_um"],
                4, "bake_policy_stale")
        for name, predecessor, schema in (("uv", "bake", upstream("uv_gate").SCHEMA),
                ("orm", "uv", upstream("orm_pack").SCHEMA),
                ("pbr", "orm", upstream("pbr_validate").SCHEMA)):
            require_receipt(0, records[name], schema, link="prev_sha256",
                            previous=file_ref(root / (predecessor + "_receipt.json"))["sha256"])
        uv_result = _ok(uv.check_uv(root / "low_uv.glb", previous=root / "bake_receipt.json",
                                  max_stretch=expected_policy["max_stretch_permille"]))
        require(uv_result["receipt"] == records["uv"], 4, "uv_semantics_stale")
        metrics = orm.verify_channels(root / "ao.png", root / "roughness.png", root / "metal.png", root / "orm.png")
        require(metrics == rec.get("orm"), 4, "orm_metrics_stale")
        # Replay pack/PBR into scratch rather than trusting rc/summary flags.
        with tempfile.TemporaryDirectory(prefix=".bundle-replay-", dir=root.parent) as tmp:
            replay = Path(tmp)
            for name in names:
                shutil.copyfile(root / name, replay / name)
            orm_rec, _ = orm.pack_maps(replay, previous=replay / "uv_receipt.json")
            require(orm_rec == records["orm"] and read_bytes(replay / "orm.png") == read_bytes(root / "orm.png"),
                    4, "orm_semantics_stale")
            pbr_rec = bake._pbr(replay, checks, replay / "orm_receipt.json")
            require(pbr_rec == records["pbr"] and
                    read_bytes(replay / "covered_albedo.png") == read_bytes(root / "covered_albedo.png"),
                    4, "pbr_semantics_stale")
        _ok(bake.verify_bundle(root, expected_receipt_sha256=expected_receipt_sha256))
        checked = sum(m["covered"] for m in checks["maps"].values()) + metrics["pixels_checked"] + sum(
            p["uv_tris"] for p in records["uv"]["prims"]) + records["pbr"]["checks"]
        require(rec["checks_evaluated"] == checked and checked > 0, 4, "bundle_check_count_stale")
        record = {"schema": "af.pipeline-bake-verification/v1", "rc": 0, "status": "OK",
            "scope": "production-uv-maps", "production_evidence": True,
            "receipt_sha256": expected_receipt_sha256, "source_high_sha256": expected_high_sha256,
            "source_low_sha256": expected_low_sha256, "policy_sha256": core.sha256_json(expected_policy),
            "checks_evaluated": checked, "semantic_receipts": [
                {"path": root.name + "/" + name + "_receipt.json",
                 "sha256": file_ref(root / (name + "_receipt.json"))["sha256"]}
                for name in ("bake", "uv", "orm", "pbr")],
            "pending_gates": ["final_glb_material_binding", "raw_geometry_regression", "appearance", "vqa3d"]}
        core.validate(record, BAKE_VERIFICATION)
        return record
    except Exception as exc:
        rc, reason = fault(exc)
        return result(rc, reason)
