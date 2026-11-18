"""Source-bound exact topology expectations; the legacy genus-zero gate stays intact.

This isolated evaluator calls the sibling's topology, distance and silhouette
engines. A declaration is caller policy, not inferred from a candidate result.
It approves geometry only, never reference appearance or provider readiness.
"""
from dataclasses import replace
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from asset_factory import core
from tools.mesh import api, contracts, worker


def require(condition, rc, reason):
    if not condition:
        raise contracts.MeshError(rc, reason)


def validate_expectation(value, source_hash, policy):
    contracts.integer_tree(value)
    keys = {"schema", "asset_id", "asset_class", "source_sha256", "reference",
            "genus_by_primitive", "rationale"}
    require(isinstance(value, dict) and set(value) == keys, 2, "topology_expectation_keys")
    require(value["schema"] == "af.asset-topology-expectation/v1", 2, "topology_expectation_schema")
    for key in ("asset_id", "asset_class", "rationale"):
        require(type(value[key]) is str and bool(value[key].strip()), 2, "topology_expectation_" + key)
    require(contracts.valid_hash(value["source_sha256"]), 2, "topology_source_pin")
    require(value["source_sha256"] == source_hash, 4, "topology_source_stale")
    reference = value["reference"]
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"} and
            type(reference["path"]) is str and bool(reference["path"]) and
            contracts.valid_hash(reference["sha256"]), 2, "topology_reference_pin")
    genus = value["genus_by_primitive"]
    require(isinstance(genus, dict) and set(genus) == set(policy["component_allowlist"]) and
            all(type(g) is int and 0 <= g <= 32 for g in genus.values()), 2, "topology_genus_expectation")
    require(not policy["allow_open"], 2, "topology_closed_required")


def policy_identity(policy, expectation):
    return contracts.json_digest({"mesh_policy": policy, "topology_expectation": expectation})


def tool_identity():
    return core.sha256_json({"gate": core.sha256_file(Path(__file__)),
                             "engines": api.toolchain_identity()})


def evaluate(raw, candidate, policy, expectation):
    """Return the existing mesh receipt family with the declared policy pinned."""
    rec = contracts.receipt(contracts.digest(raw), contracts.digest(candidate),
                            policy_identity(policy, expectation), tool_identity())
    try:
        contracts.validate_policy(policy)
        validate_expectation(expectation, rec["input_sha256"], policy)
        if "toolchain_sha256" in policy:
            require(policy["toolchain_sha256"] == api.toolchain_identity(), 4, "toolchain_stale")
        before, after = worker._parse(raw), worker._parse(candidate)
        from blender_visual_qa.assets import glb_core, lod_ladder, mesh_gate
        world = glb_core.Scene([replace(p, positions_um=glb_core.world_positions_um(p),
                                       world_q16=glb_core.IDENTITY_Q16) for p in after.prims])
        expected = expectation["genus_by_primitive"]
        _, topology = mesh_gate.gate_scene(world, max_genus=max(expected.values()),
                                          allow_open=False, require_uv=policy["require_uv"],
                                          allow_uv_overlap=False)
        inventories = topology["prims"]
        require(bool(inventories), 3, "topology_empty")
        rec["failed"].extend("topology:" + code for code in topology["failed"])
        observed = []
        for inv in inventories:
            name = inv["name"]
            observed.append(name)
            if inv["components"] != policy["component_allowlist"].get(name):
                rec["failed"].append("component_policy:" + name)
            require(inv["genus"] is not None, 3, "topology_genus_unavailable")
            # Equality rejects both invented tunnels and a removed intended handle.
            if inv["genus"] != expected.get(name):
                rec["failed"].append("topology:genus_expectation:" + name)
        if sorted(observed) != sorted(expected):
            rec["failed"].append("component_inventory_mismatch")
        worker._set(rec, "component_inventories", inventories)
        points = []
        for prim in before.prims:
            world_points = glb_core.world_positions_um(prim)
            points.extend(world_points[i] for i in prim.indices)
        diag = lod_ladder.bbox_diag_um(points)
        require(bool(diag), 3, "raw_scale_undefined")
        forward, backward = lod_ladder.hausdorff_um(before.prims, after.prims, diag,
                                                   samples=policy["sample_budget"])
        for key, value in (("raw_diag_um", diag), ("hausdorff_forward_um", forward),
                           ("hausdorff_backward_um", backward),
                           ("hausdorff_relative_ppm", (max(forward, backward)*1000000+diag-1)//diag)):
            worker._set(rec, key, value)
        if worker._distance_failed(forward, backward, diag):
            rec["failed"].append("hausdorff_relative")
        iou, aggregate, views = worker._silhouette(before, after, policy)
        worker._set(rec, "silhouette_iou_ppm", iou)
        rec["metrics"].update(distance_estimator="sampled_vertices_and_centroids_point_to_triangle",
                              distance_is_exact=False, sample_budget=policy["sample_budget"],
                              coverage_limits=["sampled_surface_only", "four_yaw_views_no_top_bottom"],
                              raster_resolution=policy["raster_resolution"], view_basis=policy["view_basis"],
                              thresholds="DESIGN_CHOICE_uncalibrated", silhouette_aggregate_ppm=aggregate,
                              silhouette_per_view_ppm=views)
        if iou < contracts.IOU_MIN_PPM:
            rec["failed"].append("silhouette_iou")
        rec["failed"] = sorted(set(rec["failed"]))
        contracts.finish(rec, 5 if rec["failed"] else 0)
    except contracts.MeshError as exc:
        contracts.finish(rec, exc.rc, exc.code)
    except ImportError:
        contracts.finish(rec, 3, "evaluator_unavailable")
    contracts.validate_receipt(rec)
    contracts.validate_success(rec, "compare")
    return rec


def main(path):
    plan = core.load_json(Path(path), max_bytes=131072)
    raw, candidate = api._read(Path(plan["source"])), api._read(Path(plan["candidate"]))
    require(contracts.digest(raw) == plan["source_sha256"] and
            contracts.digest(candidate) == plan["candidate_sha256"], 4, "gate_input_stale")
    expectation = plan["topology_expectation"]
    require(core.sha256_file(Path(expectation["reference"]["path"])) == expectation["reference"]["sha256"],
            4, "topology_reference_stale")
    result = evaluate(raw, candidate, plan["policy"], expectation)
    core.atomic_write_json(Path(plan["mesh_receipt"]), result)
    return result["rc"]


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1])
    except contracts.MeshError as exc:
        rc = exc.rc
    except Exception:
        rc = 2
    print("AF_REMESH_RC", rc, flush=True)
    raise SystemExit(rc)
