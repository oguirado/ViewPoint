"""Isolated CPU evaluator. Imports sibling algorithms; never calls its writing CLIs."""
import base64
from dataclasses import replace
from importlib.machinery import PathFinder
from importlib.util import module_from_spec
import math
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from tools.mesh.contracts import (DISTANCE_MAX_PPM, IOU_MIN_PPM, MAX_BYTES, MeshError,
                                 canonical, digest, finish, loads, receipt, validate_receipt)


def _load_sibling_package():
    """Expose only the sibling's Python package, without exposing its tools."""
    name = "blender_visual_qa"
    if name in sys.modules:
        return
    spec = PathFinder.find_spec(name, [str(ROOT.parent / "blender-visual-qa")])
    if spec is None or spec.loader is None:
        return
    module = module_from_spec(spec)
    # The package initializer is declarative. Publish after successful execution.
    spec.loader.exec_module(module)
    sys.modules.setdefault(name, module)


_load_sibling_package()


def _document(data):
    """Reject features ignored by the sibling's static triangle model."""
    import json
    if len(data) < 20 or data[:4] != b"glTF":
        raise MeshError(2, "unsupported_asset")
    version, total = struct.unpack_from("<II", data, 4)
    length, kind = struct.unpack_from("<II", data, 12)
    if (version != 2 or total != len(data) or kind != 0x4E4F534A
            or length % 4 or length > len(data) - 20):
        raise MeshError(2, "malformed_glb")
    from tools.mesh.contracts import _pairs
    def bad_constant(_):
        raise MeshError(2, "non_finite_glb_json")
    doc = json.loads(data[20:20 + length], object_pairs_hook=_pairs, parse_constant=bad_constant)
    def finite(obj):
        if isinstance(obj, dict):
            return all(finite(v) for v in obj.values())
        if isinstance(obj, list):
            return all(finite(v) for v in obj)
        return type(obj) is not float or math.isfinite(obj)
    if not isinstance(doc, dict) or not finite(doc):
        raise MeshError(2, "malformed_glb_document")
    offset, bin_length = 20 + length, 0
    if offset < len(data):
        if offset + 8 > len(data):
            raise MeshError(2, "malformed_glb_chunks")
        bin_length, bin_kind = struct.unpack_from("<II", data, offset)
        if (bin_kind != 0x004E4942 or bin_length % 4 or offset + 8 + bin_length != len(data)):
            raise MeshError(2, "unsupported_glb_chunks")
    _validate_structure(doc, bin_length)
    def extensions(obj):
        if isinstance(obj, dict):
            return bool(obj.get("extensions")) or any(extensions(v) for v in obj.values())
        if isinstance(obj, list):
            return any(extensions(v) for v in obj)
        return False
    if (doc.get("skins") or doc.get("animations") or doc.get("extensionsRequired")
            or doc.get("extensionsUsed") or extensions(doc)):
        raise MeshError(2, "unsupported_dynamic_or_extension_asset")
    if any("uri" in item for key in ("buffers", "images") for item in doc.get(key, [])):
        raise MeshError(2, "external_resource")
    for mesh in doc.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if prim.get("targets") or any(k.startswith(("JOINTS", "WEIGHTS")) for k in prim.get("attributes", {})):
                raise MeshError(2, "unsupported_dynamic_asset")
    return doc


def _validate_structure(doc, bin_length):
    """Validate supported static glTF structure before the permissive reader.

    This is a boundary validator, not a second geometry engine. References and
    byte spans must never acquire Python indexing/coercion semantics.
    """
    def reject():
        raise MeshError(2, "malformed_glb_structure")
    def uint(value):
        if type(value) is not int or value < 0:
            reject()
        return value
    def records(key):
        values = doc.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
            reject()
        return values
    def ref(value, values):
        if uint(value) >= len(values):
            reject()
        return values[value]
    asset = doc.get("asset")
    if not isinstance(asset, dict) or asset.get("version") != "2.0" or asset.get("minVersion", "2.0") != "2.0":
        reject()
    buffers, views, accessors = records("buffers"), records("bufferViews"), records("accessors")
    if len(buffers) > 1:
        reject()
    for buffer in buffers:
        size = uint(buffer.get("byteLength"))
        if size > bin_length or bin_length - size > 3:
            reject()
    if bin_length and not buffers:
        reject()
    for view in views:
        buffer = ref(view.get("buffer"), buffers)
        start, size = uint(view.get("byteOffset", 0)), uint(view.get("byteLength"))
        if start + size > buffer["byteLength"]:
            reject()
        if "byteStride" in view:
            stride = uint(view["byteStride"])
            if not 4 <= stride <= 252 or stride % 4:
                reject()
    sizes = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
    components = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
    for acc in accessors:
        count, start = uint(acc.get("count")), uint(acc.get("byteOffset", 0))
        ct = uint(acc.get("componentType"))
        if ct not in sizes or acc.get("type") not in components or "sparse" in acc:
            reject()
        if "normalized" in acc and type(acc["normalized"]) is not bool:
            reject()
        if "bufferView" not in acc:
            reject()  # Implicit zero/sparse accessors are outside this subset.
        view = ref(acc["bufferView"], views)
        element = sizes[ct] * components[acc["type"]]
        stride = view.get("byteStride", element)
        end = start + ((count - 1) * stride + element if count else 0)
        if (stride < element or stride % sizes[ct] or start % sizes[ct]
                or (view.get("byteOffset", 0) + start) % sizes[ct] or end > view["byteLength"]):
            reject()
    nodes, scenes, meshes, materials = records("nodes"), records("scenes"), records("meshes"), records("materials")
    if "scene" in doc:
        ref(doc["scene"], scenes)
    for scene in scenes:
        if not isinstance(scene.get("nodes", []), list):
            reject()
        for index in scene.get("nodes", []):
            ref(index, nodes)
    for node in nodes:
        if "mesh" in node:
            ref(node["mesh"], meshes)
        if not isinstance(node.get("children", []), list):
            reject()
        for index in node.get("children", []):
            ref(index, nodes)
    for mesh in meshes:
        prims = mesh.get("primitives")
        if not isinstance(prims, list):
            reject()
        for prim in prims:
            if not isinstance(prim, dict) or type(prim.get("mode", 4)) is not int or prim.get("mode", 4) != 4:
                reject()
            attrs = prim.get("attributes")
            if not isinstance(attrs, dict) or "POSITION" not in attrs:
                reject()
            for index in attrs.values():
                ref(index, accessors)
            pos = ref(attrs["POSITION"], accessors)
            if pos["type"] != "VEC3" or pos["componentType"] != 5126 or pos.get("normalized", False):
                reject()
            for name, index in attrs.items():
                acc = accessors[index]
                if acc["count"] != pos["count"]:
                    reject()
                if name in ("NORMAL", "TEXCOORD_0"):
                    if acc["type"] != ("VEC3" if name == "NORMAL" else "VEC2"):
                        reject()
            if "indices" in prim:
                acc = ref(prim["indices"], accessors)
                if (acc["type"] != "SCALAR" or acc["componentType"] not in (5121, 5123, 5125)
                        or acc.get("normalized", False)):
                    reject()
            if "material" in prim:
                ref(prim["material"], materials)


def _parse(data):
    from blender_visual_qa.assets import glb_core
    _document(data)
    scene = glb_core.parse_glb(data)
    if not scene.prims or not any(p.indices for p in scene.prims):
        raise MeshError(3, "empty_geometry")
    if any(not p.indices for p in scene.prims):
        raise MeshError(3, "empty_primitive")
    if any(p.skinned or glb_core.det3_q48(p.world_q16) == 0 for p in scene.prims):
        raise MeshError(3, "unsupported_or_zero_scale")
    points = []
    for prim in scene.prims:
        world = glb_core.world_positions_um(prim)
        points.extend(world[i] for i in prim.indices)
    if all(max(p[k] for p in points) == min(p[k] for p in points) for k in range(3)):
        raise MeshError(3, "zero_extent_geometry")
    return scene


def _set(rec, key, value):
    rec["metrics"][key] = value
    if value is not None:
        rec["unavailable"].pop(key, None)


def _components(scene, policy, rec):
    from blender_visual_qa.assets import glb_core, mesh_gate
    # Topology is tested after world transforms, while keeping primitives disjoint.
    world = glb_core.Scene([replace(p, positions_um=glb_core.world_positions_um(p),
                                   world_q16=glb_core.IDENTITY_Q16) for p in scene.prims])
    rc, body = mesh_gate.gate_scene(world, max_genus=0, allow_open=policy["allow_open"],
                                     require_uv=policy["require_uv"], allow_uv_overlap=False)
    if rc not in (0, 5):
        raise MeshError(3 if rc == 3 else 2, "topology_unavailable")
    inventories = body["prims"]
    _set(rec, "component_inventories", inventories)
    rec["failed"].extend("topology:" + key for key in body["failed"])
    observed = {}
    for item in inventories:
        name = item["name"]
        observed[name] = observed.get(name, 0) + 1
        if policy["component_allowlist"].get(name) != item["components"]:
            rec["failed"].append("component_policy:" + name)
        # Local topology nulls are explicit, too (e.g. genus of an open mesh).
        for key, value in item.items():
            if value is None:
                rec["unavailable"]["component:" + name + ":" + key] = "undefined_for_open_or_nonmanifold_mesh"
    if any(n != 1 for n in observed.values()) or set(observed) != set(policy["component_allowlist"]):
        rec["failed"].append("component_inventory_mismatch")


def _distance_failed(forward, backward, diag):
    return max(forward, backward) * 1000000 > diag * DISTANCE_MAX_PPM


def _silhouette(before, after, policy):
    from blender_visual_qa.assets import heal, ortho4
    res = policy["raster_resolution"]
    bt, at = ortho4.scene_triangles(before), ortho4.scene_triangles(after)
    c2, ext = ortho4.frame(bt)
    if not ext:
        raise MeshError(3, "silhouette_undefined")
    by_view = {}
    for view in ortho4.VIEWS:
        b = ortho4.rasterise(bt, view, c2, ext, res)
        a = ortho4.rasterise(at, view, c2, ext, res)
        union = ortho4.popcount([x | y for x, y in zip(b, a)])
        if not union:
            raise MeshError(3, "silhouette_empty_union")
        by_view[view] = ortho4.popcount([x & y for x, y in zip(b, a)]) * 1000000 // union
    aggregate = heal.silhouette_iou_ppm(before, after, res=res)
    if aggregate is None:
        raise MeshError(3, "silhouette_unavailable")
    return min(aggregate, *by_view.values()), aggregate, by_view


def _compare(before, after, policy, rec):
    from blender_visual_qa.assets import glb_core, lod_ladder
    raw_points = []
    for prim in before.prims:
        world = glb_core.world_positions_um(prim)
        raw_points.extend(world[i] for i in prim.indices)
    diag = lod_ladder.bbox_diag_um(raw_points)
    if not diag:
        raise MeshError(3, "raw_scale_undefined")
    _set(rec, "raw_diag_um", diag)
    _components(after, policy, rec)
    forward, backward = lod_ladder.hausdorff_um(before.prims, after.prims, diag,
                                               samples=policy["sample_budget"])
    _set(rec, "hausdorff_forward_um", forward)
    _set(rec, "hausdorff_backward_um", backward)
    # Ceil ppm preserves the strict rational threshold, without float arithmetic.
    _set(rec, "hausdorff_relative_ppm", (max(forward, backward) * 1000000 + diag - 1) // diag)
    rec["metrics"].update({"distance_estimator": "sampled_vertices_and_centroids_point_to_triangle",
                           "distance_is_exact": False, "sample_budget": policy["sample_budget"],
                           "coverage_limits": ["sampled_surface_only", "four_yaw_views_no_top_bottom"],
                           "raster_resolution": policy["raster_resolution"],
                           "view_basis": policy["view_basis"], "thresholds": "DESIGN_CHOICE_uncalibrated"})
    if _distance_failed(forward, backward, diag):
        rec["failed"].append("hausdorff_relative")
    iou, aggregate, by_view = _silhouette(before, after, policy)
    _set(rec, "silhouette_iou_ppm", iou)
    rec["metrics"].update({"silhouette_aggregate_ppm": aggregate, "silhouette_per_view_ppm": by_view})
    if iou < IOU_MIN_PPM:
        rec["failed"].append("silhouette_iou")


def _repair(scene, raw, policy, rec):
    from blender_visual_qa.assets import glb_core, heal
    if "repair" not in policy:
        raise MeshError(2, "repair_policy_required")
    # A clean input is returned byte-for-byte; no unnecessary reserialization.
    _components(scene, policy, rec)
    if not rec["failed"]:
        return raw
    rec["failed"] = []
    doc = _document(raw)
    if (doc.get("materials") or doc.get("textures") or doc.get("images")
            or doc.get("extras") or any(p.normals_q16 is not None for p in scene.prims)
            or any(set(p.get("attributes", {})) - {"POSITION", "TEXCOORD_0"}
                   for m in doc.get("meshes", []) for p in m.get("primitives", []))):
        raise MeshError(2, "unsupported_repair_attributes")
    limits = policy["repair"]
    out, stats = [], []
    for prim in scene.prims:
        pos = list(prim.positions_um)
        ix = prim.indices
        triangles = [tuple(ix[k:k + 3]) for k in range(0, len(ix), 3)]
        triangles, stat, vmap = heal.heal_mesh(pos, triangles, max_passes=limits["max_passes"],
                                               tol=policy["weld_tolerance_um"],
                                               max_hole_edges=limits["max_hole_edges"],
                                               reflect=glb_core.det3_q48(prim.world_q16) < 0)
        if stat["culled_faces"] * 1000 > len(ix) // 3 * limits["cull_max_permille"]:
            raise MeshError(5, "repair_cull_budget")
        if prim.uvs_q16 is not None:
            lookup = heal.uv_lookup(prim, vmap)
            if stat["patch_faces"] or any(frozenset(t) not in lookup for t in triangles):
                raise MeshError(3, "uv_preserving_repair_unavailable")
            repaired, synth = heal.build_prim(prim, pos, triangles, lookup)
            if synth:
                raise MeshError(3, "uv_synthesis_forbidden")
        else:
            repaired = replace(prim, positions_um=pos, indices=[i for t in triangles for i in t])
        stats.append(dict(stat, name=prim.name))
        out.append(repaired)
    rec["metrics"]["repair_operations"] = stats
    return glb_core.write_glb(glb_core.Scene(out))


def evaluate(request):
    rec = request["receipt"]
    artefact = None
    try:
        raw = base64.b64decode(request["raw"], validate=True)
        scene = _parse(raw)
        policy = request["policy"]
        operation = request["operation"]
        if operation == "components":
            _components(scene, policy, rec)
        else:
            if operation == "repair":
                artefact = _repair(scene, raw, policy, rec)
                candidate = artefact
            elif operation == "compare":
                candidate = base64.b64decode(request["candidate"], validate=True)
            else:
                raise MeshError(2, "unknown_operation")
            rec["candidate_sha256"] = digest(candidate)
            after = _parse(candidate)
            _compare(scene, after, policy, rec)
        rec["failed"] = sorted(set(rec["failed"]))
        rc, rec = finish(rec, 5 if rec["failed"] else 0)
    except MeshError as exc:
        rc, rec = finish(rec, exc.rc, exc.code)
    except ImportError:
        rc, rec = finish(rec, 3, "evaluator_unavailable")
    except Exception:
        # Malformed GLB/sibling exception is never evidence of success.
        rc, rec = finish(rec, 2, "unsupported_asset_or_evaluator_error")
    if rc != 0:
        artefact = None
    validate_receipt(rec)
    return {"receipt": rec, "artefact": base64.b64encode(artefact).decode("ascii") if artefact is not None else None}


def main():
    try:
        data = sys.stdin.buffer.read(3 * MAX_BYTES + 1)
        if len(data) > 3 * MAX_BYTES:
            raise MeshError(6, "request_budget_exceeded")
        result = evaluate(loads(data))
    except MeshError as exc:
        _, rec = finish(receipt(), exc.rc, exc.code)
        result = {"receipt": rec, "artefact": None}
    except (ValueError, TypeError, KeyError, RecursionError):
        _, rec = finish(receipt(), 2, "invalid_worker_request")
        result = {"receipt": rec, "artefact": None}
    sys.stdout.write(canonical(result))
    return result["receipt"]["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
