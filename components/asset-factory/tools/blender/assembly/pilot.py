"""Compile and assemble registered humanoid parts against a pinned snapshot."""
import hashlib
import json
import math
import os
from pathlib import Path
import struct
from time import monotonic_ns
from subprocess import TimeoutExpired

from jsonschema import Draft202012Validator
from PIL import Image, ImageFilter, __version__ as PILLOW_VERSION

from asset_factory import core, proc
from asset_factory.domains import FRAME, PPM, UM_PER_METER
from tools.blender.helpers_contracts import GlbError, parse_json
from tools.registry import RegistryError
from . import camera, fixture, geometry as mesh_geometry
from .fixture import AssemblyError


HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE.parents[2] / "schemas" / "af.assembly-recipe.v1.schema.json"
TEMPLATE_DIGEST = "sha256:ea73d7ae5d83e1601d1fa8041e195797634e98d27c3ce629de5e35fd745a85ac"
TAXONOMY = {
    "arm": "humanoid/arm/upper", "head": "humanoid/head",
    "leg": "humanoid/leg/thigh", "torso": "humanoid/torso", "humanoid": "humanoid/character",
}
FRONT_AXES = {
    "+X": (1, 0, 0), "-X": (-1, 0, 0),
    "+Y": (0, 1, 0), "-Y": (0, -1, 0),
    "+Z": (0, 0, 1), "-Z": (0, 0, -1),
}
WORLD_FRONT = FRONT_AXES["-Y"]
RENDER = {"engine": "CYCLES", "device": "CPU", "size": 1024, "span_um": 2_120_000,
          "radius_um": 4_000_000, "projection": "orthographic", "grey": [128, 128, 128],
          "samples": 64, "denoise_window_px": 5, "flat_face_sample_size_px": 32,
          "views": [[name, list(direction)] for name, direction in fixture.VIEWS]}
POLICY = {"frame": FRAME, "max_gap_um": 0, "max_interpenetration_um": 0,
          "max_import_residual_nm": 500, "max_export_axis_seam_uncertainty_nm": 1000,
          "dimensions_um": list(fixture.DIMENSIONS_UM),
          "max_flat_face_luma_spread": 4,
           "min_front_cosine_ppm_by_role": {"head": 999_000, "leg": 998_000, "torso": 996_000, "arm": 984_000},
          "max_mesh_triangles": mesh_geometry.MESH_TRIANGLE_LIMIT,
          "max_socket_patch_triangles": mesh_geometry.PATCH_TRIANGLE_LIMIT,
          "scope": "fixture or rigid meshes with certified planar surface seams"}
READ_LIMITS = {"max_objects": 128, "max_bytes": 64 * 1024 * 1024}
RUNTIME_S = 180


def encode(value):
    return core.canonical_json(value).encode("utf-8")


def pin(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _file_pin(path):
    return "sha256:" + core.sha256_file(path)


def _toolchain(blender):
    files = (blender, HERE / "pilot.py", HERE / "fixture.py", HERE / "worker.py",
             HERE / "geometry.py", HERE / "camera.py", SCHEMA_PATH)
    return encode({"pillow_version": PILLOW_VERSION, **{path.name: _file_pin(path) for path in files}})


def _role_side(name):
    role, _, side = name.partition("-")
    return role, side or "center"


def check_part_role(name, record):
    role, side = _role_side(name)
    if record.get("side") != side:
        raise AssemblyError("wrong_side")
    if record.get("anatomical_role") != role or record.get("taxonomy_path") != TAXONOMY[role]:
        raise AssemblyError("wrong_anatomical_role")


def _check_part_facing(record, transform):
    axis = record["labels"].get("front_axis")
    if axis is None:
        raise AssemblyError("missing_part_front_axis", 3)
    if axis not in FRONT_AXES:
        raise AssemblyError("invalid_part_front_axis", 2)
    rotation, _ = mesh_geometry.frame(transform)
    local_front = FRONT_AXES[axis]
    world_front = tuple(mesh_geometry.dot(row, local_front) for row in rotation)
    cosine = mesh_geometry.dot(world_front, WORLD_FRONT)
    limit = POLICY["min_front_cosine_ppm_by_role"][record["anatomical_role"]]
    if cosine * PPM < limit:
        raise AssemblyError("wrong_part_facing")
    return {"local_front_axis": axis, "world_front_cosine_num_den": [cosine.numerator, cosine.denominator],
            "min_world_front_cosine_ppm": limit}


def _profile(name):
    axis, facing, size = fixture.MATES[name]
    return encode({"joint": name, "axis": axis, "facing": facing, "dimensions_um": list(size),
                   "type": "surface_seam"})


def _socket(name, child):
    axis, sign, size = fixture.MATES[name]
    position = (0, 0, 0) if child else tuple(
        a - b for a, b in zip(fixture.PLACEMENTS[name], fixture.PLACEMENTS["torso"]))
    direction = -sign if child else sign
    return {
        "id": "root" if child else name, "semantic_role": "component" if child else "torso",
        "profile_digest": pin(_profile(name)), "mating_type": "surface_seam",
        "mating_dimensions_um": [*size, 1], "polarity": "plug" if child else "receptacle",
        "allowed_peer_roles": ["torso" if child else "component"],
        "frame": fixture.transform(position, fixture.ROTATIONS[axis, direction]),
        "fit_receipt_digest": None,
    }


def _descriptor(role, path, data, media):
    return {"digest": pin(data), "logical_path": path, "role": role,
            "size_bytes": len(data), "media_type": media}


def _record(name, kind, mesh, provenance, dimensions, taxonomy):
    absent = "AUSENTE: no production rights or qualification receipt for the local fixture"
    other = "AUSENTE: outside the procedural fixture profile"
    family = "af." + kind + "-record/v1"
    role, side = _role_side(name)
    files = [_descriptor("mesh", "mesh.glb", mesh, "model/gltf-binary")]
    values = {
        "schema": family, "record_kind": kind, "canonical_profile": "af.integer-json-lf/v1",
        "display_name": name, "category": "humanoid",
        "taxonomy_path": TAXONOMY[role],
        "annotations": {}, "labels": {},
        "classification": {"taxonomy_digest": pin(taxonomy), "method": "procedural-fixture",
                           "confidence_ppm": None, "receipt_digest": None,
                           "absence_reason": "AUSENTE: no statistical classification"},
        "dimensions": {"xyz_um": list(dimensions), "evidence_class": "MEASURED",
                       "method": "exact-integer-box-extents", "receipt_digest": pin(provenance),
                       "absence_reason": None},
        "coordinate_frame": "RH_Z_UP_FRONT_NEG_Y_UM",
        "symmetry": {"kind": "unknown", "plane_normal_axis": None, "plane_offset_um": None,
                     "radial_order": None, "counterpart_record_digest": None,
                     "receipt_digest": None, "absence_reason": other},
        "lineage": {"operation": "source", "parent_records": [], "parent_asset_digest": None,
                    "decomposition": None, "assembly_recipe_digest": None,
                    "generation_request_digest": None, "source_view_digests": [],
                    "original_record_digest": None, "absence_reasons": {}},
        "rights": {"status": "unknown", "components": [
            {"component": "original-procedural-fixture", "provenance_digest": pin(provenance),
             "subject_digest": pin(mesh), "licence_expression": None, "licence_text_digest": None,
             "commercial": None, "territory_excluded": [], "gated": None, "absence_reason": absent}],
            "attributions": [], "decision_receipt_digests": []},
        "geometry_digest": pin(mesh),
        "representations": {"cad": None, "visual": pin(mesh), "composition": None, "collision": None,
                            "physics": None, "rig": None, "motion": None,
                            "absence_reasons": {key: other for key in ("cad", "collision", "physics", "rig", "motion")}},
        "references": None, "files": files, "instances": [], "sockets": [], "materials": [],
        "lods": [], "dependencies": [], "gates": [],
    }
    if kind == "part":
        values.update(anatomical_role=role, side=side, reuse_status="candidate",
                      compatibility_profile_digests=[])
    return values


def _worker(blender, root, request):
    """Run exclusively through the owned process API and check retirement."""
    (root / "request.json").write_bytes(encode(request))
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="", HIP_VISIBLE_DEVICES="",
               ROCR_VISIBLE_DEVICES="", PYTHONDONTWRITEBYTECODE="1")
    started = monotonic_ns()
    job = proc.popen([blender, "--background", "--factory-startup", "--disable-autoexec",
                      "--threads", str(min(16, len(os.sched_getaffinity(0)))),
                      "--python-exit-code", "5", "--python", HERE / "worker.py", "--", root],
                     start_new_session=True, cwd=root, env=env, runtime_s=RUNTIME_S)
    try:
        stdout, stderr = proc.communicate(job, timeout=RUNTIME_S)
    except TimeoutExpired as exc:
        raise AssemblyError("blender_timeout", 6) from exc
    finally:
        cleanup = proc.cleanup(job)
        if cleanup:
            raise AssemblyError("blender_cleanup_unproven", 3)
    (root / "blender.log").write_bytes(stdout + stderr)
    if job.returncode != 0:
        if b"ValueError: fixture_reconstruction_mismatch" in stderr:
            raise AssemblyError("fixture_reconstruction_mismatch")
        if b"ValueError: part_reconstruction_mismatch" in stderr:
            raise AssemblyError("part_reconstruction_mismatch")
        raise AssemblyError("blender_failed: " + stderr.decode(errors="replace")[-1600:])
    return {"wall_ns": monotonic_ns() - started, "unit_name": job.unit_name,
            "memory_peak_bytes": job.result.memory_peak_bytes,
            "process_result": job.result.systemd_result}


def _mesh_request(shapes, positions):
    prepared = {}
    for name, boxes in shapes.items():
        vertices, faces = fixture.boundary(boxes)
        prepared[name] = {"vertices": [list(vertex) for vertex in vertices],
                          "faces": [list(face) for face in faces], "position": list(positions[name])}
        if name.startswith("arm-"):
            prepared[name]["hand"] = boxes["hand"]
    return prepared


def author_parts(registry, *, root, blender, view_template, expected_snapshot):
    """Create registered candidate parts in the caller's private fixture registry."""
    template = view_template.read_bytes()
    if pin(template) != TEMPLATE_DIGEST:
        raise AssemblyError("view_template_digest", 4)
    blender = blender.resolve(strict=True)
    toolchain = _toolchain(blender)
    taxonomy = encode(TAXONOMY)
    if registry.current_snapshot() != expected_snapshot:
        raise AssemblyError("stale_publish_snapshot", 4)
    shapes = fixture.parts()
    fixture.check_geometry(shapes, fixture.PLACEMENTS)
    root.mkdir()
    process = _worker(blender, root, {"operation": "author", "units": UM_PER_METER,
                                    "parts": _mesh_request(shapes, fixture.PLACEMENTS)})
    rows, matings, approved = [], [], {}
    snapshot = expected_snapshot
    for name, boxes in shapes.items():
        mesh = (root / (name + ".glb")).read_bytes()
        provenance = encode({"frame": FRAME, "kind": "original-procedural-box-union", "instance": name,
                             "toolchain_digest": pin(toolchain), "boxes": boxes})
        sockets = ([_socket(child, False) for child in fixture.MATES] if name == "torso"
                   else [_socket(name, True)])
        profile_bytes = [_profile(sock["id"] if name == "torso" else name) for sock in sockets]
        record = _record(name, "part", mesh, provenance, fixture.dimensions(list(boxes.values())), taxonomy)
        record["labels"]["front_axis"] = "-Y"
        record["sockets"] = sockets
        record["compatibility_profile_digests"] = sorted(pin(data) for data in profile_bytes)
        record["representations"]["composition"] = pin(provenance)
        blobs = {pin(data): data for data in (mesh, taxonomy, provenance, toolchain, *profile_bytes)}
        record["dependencies"] = sorted(blobs)
        raw = encode(record)
        snapshot = registry.put(name, raw, blobs=blobs, expected_snapshot=snapshot)
        approved[name] = pin(raw)
        rows.append({"instance_id": name, "locator": f"af://{registry.namespace}/{name}@{pin(raw)}",
                     "part_record_sha256": pin(raw), "artifact_sha256": pin(mesh),
                     "socket_set_sha256": pin(encode(sockets)),
                     "transform": fixture.transform(fixture.PLACEMENTS[name])})
        if name in fixture.MATES:
            matings.append({"parent_socket": name, "child_instance_id": name,
                            "child_socket": "root", "profile_sha256": pin(_profile(name)),
                            "seam_mode": "none"})
    recipe = encode({
        "schema": "af.assembly-recipe/v1", "category": "character", "snapshot": snapshot,
        "root_instance_id": "torso", "target_dimensions_um": list(fixture.DIMENSIONS_UM),
        "view_template_sha256": TEMPLATE_DIGEST, "taxonomy_sha256": pin(taxonomy),
        "toolchain_sha256": pin(toolchain), "render_profile_sha256": pin(encode(RENDER)),
        "acceptance_policy_sha256": pin(encode(POLICY)), "parts": rows, "matings": matings,
    })
    return recipe, approved, process


def prepare(registry, raw, *, expected_recipe_digest, approved_parts, blender, view_template):
    """Preflight a bounded canonical recipe before handing any bytes to Blender."""
    if type(raw) is not bytes:
        raise AssemblyError("recipe_bytes_required", 2)
    if len(raw) > 32_768:
        raise AssemblyError("recipe_byte_budget", 6)
    if pin(raw) != expected_recipe_digest:
        raise AssemblyError("recipe_digest_mismatch", 4)
    try:
        recipe = parse_json(raw)
        canonical = encode(recipe)
    except (GlbError, core.ContractError) as exc:
        raise AssemblyError("recipe_schema_or_canonical_bytes", 2) from exc
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_bytes()))
    if not validator.is_valid(recipe) or canonical != raw:
        raise AssemblyError("recipe_schema_or_canonical_bytes", 2)

    pinned = {
        "view_template_sha256": view_template.read_bytes(), "taxonomy_sha256": encode(TAXONOMY),
        "toolchain_sha256": _toolchain(blender.resolve(strict=True)),
        "render_profile_sha256": encode(RENDER), "acceptance_policy_sha256": encode(POLICY),
    }
    if (recipe["view_template_sha256"] != TEMPLATE_DIGEST
            or any(pin(data) != recipe[key] for key, data in pinned.items())):
        raise AssemblyError("profile_or_toolchain_digest", 4)

    rows = {row["instance_id"]: row for row in recipe["parts"]}
    matings = {row["child_instance_id"]: row for row in recipe["matings"]}
    if list(rows) != list(fixture.PLACEMENTS) or list(matings) != list(fixture.MATES):
        raise AssemblyError("incomplete_or_unordered_mating_tree", 3)
    if approved_parts.keys() != rows.keys():
        raise AssemblyError("missing_approved_part_bindings", 3)

    fixture_mode = recipe.get("geometry_profile", "fixture") == "fixture"
    snapshot = recipe["snapshot"]
    records, meshes = {}, {}
    for name, row in rows.items():
        located = _part_read(registry.resolve, row["locator"], snapshot=snapshot)
        record = located["record"]
        if record["schema"] != "af.part-record/v1":
            raise AssemblyError("part_record_required", 2)
        check_part_role(name, record)
        if located["record_digest"] != row["part_record_sha256"] or approved_parts[name] != located["record_digest"]:
            raise AssemblyError("part_digest_mismatch", 4)
        if record["classification"]["taxonomy_digest"] != recipe["taxonomy_sha256"]:
            raise AssemblyError("taxonomy_digest_mismatch", 4)
        if record["geometry_digest"] != row["artifact_sha256"]:
            raise AssemblyError("mesh_digest_mismatch", 4)
        if pin(encode(record["sockets"])) != row["socket_set_sha256"]:
            raise AssemblyError("socket_set_digest_mismatch", 4)
        records[name] = record
        meshes[name] = _part_read(registry.read_blob, row["locator"], row["artifact_sha256"], snapshot=snapshot)

    if fixture_mode:
        geometry, request = _prepare_fixture(registry, recipe, rows, matings, records, meshes)
    else:
        geometry, request = _prepare_planar(registry, recipe, rows, matings, records, meshes)
    if recipe["target_dimensions_um"] != list(geometry["dimensions_um"]):
        raise AssemblyError("physical_scale")
    geometry["part_facing"] = {
        name: _check_part_facing(records[name], row["transform"]) for name, row in rows.items()
    }
    return recipe, geometry, meshes, request, pinned


def _part_read(reader, *args, snapshot):
    try:
        return reader(*args, snapshot=snapshot, **READ_LIMITS)
    except RegistryError as exc:
        raise AssemblyError("part_unresolvable", exc.rc) from exc


def _prepare_fixture(registry, recipe, rows, matings, records, meshes):
    shapes, anchors = {}, {}
    for name, row in rows.items():
        record = records[name]
        socket_set = ([_socket(child, False) for child in fixture.MATES] if name == "torso"
                      else [_socket(name, True)])
        if record["sockets"] != socket_set:
            raise AssemblyError("incompatible_socket_profile")
        if name in matings:
            mating = matings[name]
            if (mating.get("parent_instance_id", "torso") != "torso"
                    or mating["child_socket"] != "root" or mating["parent_socket"] != name
                    or mating["profile_sha256"] != pin(_profile(name))):
                raise AssemblyError("incompatible_socket_profile")
        transform = row["transform"]
        if transform["rotation_xyzw_ppm"] != fixture.IDENTITY:
            raise AssemblyError("recipe_schema_or_canonical_bytes", 2)
        witness = _part_read(registry.read_blob, row["locator"], record["representations"]["composition"],
                             snapshot=recipe["snapshot"])
        try:
            shape = parse_json(witness)
        except GlbError as exc:
            raise AssemblyError("wrong_fixture_witness", 4) from exc
        if (type(shape) is not dict or type(shape.get("boxes")) is not dict
                or shape != {"frame": FRAME, "kind": "original-procedural-box-union", "instance": name,
                             "toolchain_digest": recipe["toolchain_sha256"], "boxes": shape["boxes"]}):
            raise AssemblyError("wrong_fixture_witness", 4)
        shapes[name] = shape["boxes"]
        anchors[name] = tuple(transform["translation_um"])

    result = fixture.check_geometry(shapes, anchors)
    request = _mesh_request(shapes, anchors)
    residuals = {}
    for name, mesh in meshes.items():
        if records[name]["dimensions"]["xyz_um"] != list(fixture.dimensions(shapes[name].values())):
            raise AssemblyError("physical_scale")
        vertices, faces, residual = mesh_geometry.decode_mesh(mesh, POLICY["max_import_residual_nm"])
        expected = request[name]
        if mesh_geometry.oriented_triangles(vertices, faces) != mesh_geometry.oriented_triangles(
                expected["vertices"], expected["faces"]):
            raise AssemblyError("fixture_reconstruction_mismatch")
        residuals[name] = residual
    result["source_coordinate_residual_nm"] = residuals
    return result, request


def _prepare_planar(registry, recipe, rows, matings, records, meshes):
    parts, request, residuals = {}, {}, {}
    for name, row in rows.items():
        vertices, faces, residuals[name] = mesh_geometry.decode_mesh(meshes[name], POLICY["max_import_residual_nm"])
        lo, hi = mesh_geometry.bounds(vertices)
        declared = records[name]["dimensions"]
        if (declared["evidence_class"] != "MEASURED" or declared["receipt_digest"] is None
                or list(mesh_geometry.offset(hi, lo)) != declared["xyz_um"] or any(a == b for a, b in zip(lo, hi))):
            raise AssemblyError("physical_scale")
        matrix, position = mesh_geometry.frame(row["transform"])
        world = [mesh_geometry.transform_point(matrix, position, vertex) for vertex in vertices]
        if any(value.denominator != 1 for point in world for value in point):
            raise AssemblyError("assembly_lattice_unproven", 3)
        parts[name] = {"world": [tuple(map(int, point)) for point in world],
                       "faces": faces, "frame": (matrix, position)}
        request[name] = {
            "vertices": list(map(list, vertices)), "faces": list(map(list, faces)), "position": list(position),
            "rotation": [[[entry.numerator, entry.denominator] for entry in row] for row in matrix],
            "world_vertices": list(map(list, parts[name]["world"])),
        }

    torso_rotation, torso_origin = parts["torso"]["frame"]
    torso_inverse = mesh_geometry.transpose(torso_rotation)
    midpoints = {}
    for name, part in parts.items():
        along_x = [mesh_geometry.dot(torso_inverse[0], mesh_geometry.offset(point, torso_origin))
                   for point in part["world"]]
        midpoints[name] = min(along_x) + max(along_x)
    for name in parts:
        if (name.endswith("-left") and midpoints[name] <= midpoints["torso"]
                or name.endswith("-right") and midpoints[name] >= midpoints["torso"]):
            raise AssemblyError("wrong_side")

    sockets = {name: {sock["id"]: sock for sock in record["sockets"]} for name, record in records.items()}
    resolved, occupied, pairs, seams = {"torso"}, set(), set(), {}
    pending = dict(matings)
    while pending:
        ready = [name for name, mating in pending.items() if mating.get("parent_instance_id", "torso") in resolved]
        if not ready:
            raise AssemblyError("incomplete_or_unordered_mating_tree", 3)
        for name in ready:
            mating = pending.pop(name)
            parent_name = mating.get("parent_instance_id", "torso")
            a, b = mating["parent_socket"], mating["child_socket"]
            if a not in sockets[parent_name] or b not in sockets[name]:
                raise AssemblyError("incompatible_socket_profile")
            attachments = {(parent_name, a), (name, b)}
            if occupied.intersection(attachments):
                raise AssemblyError("socket_already_occupied")
            digest = mating["profile_sha256"]
            if any(digest not in records[key]["compatibility_profile_digests"] for key in (parent_name, name)):
                raise AssemblyError("incompatible_socket_profile")
            profile_bytes = _part_read(registry.read_blob, rows[parent_name]["locator"], digest,
                                       snapshot=recipe["snapshot"])
            try:
                profile = parse_json(profile_bytes)
                if encode(profile) != profile_bytes:
                    raise AssemblyError("incompatible_socket_profile")
            except (GlbError, core.ContractError) as exc:
                raise AssemblyError("incompatible_socket_profile") from exc
            seams[name] = mesh_geometry.check_mating(sockets[parent_name][a], sockets[name][b], mating,
                                                      parts[parent_name], parts[name], profile)
            resolved.add(name)
            occupied.update(attachments)
            pairs.add(frozenset((parent_name, name)))

    lo, hi = mesh_geometry.check_separation(parts, pairs)
    return ({"dimensions_um": mesh_geometry.offset(hi, lo), "bounds_um": [list(lo), list(hi)], "seams": seams,
             "source_coordinate_residual_nm": residuals}, request)


def _render_rig(geometry):
    if "bounds_um" not in geometry:
        return dict(RENDER, center_um=[0, 0, geometry["dimensions_um"][2] // 2])
    lo, hi = geometry["bounds_um"]
    width, depth, height = geometry["dimensions_um"]
    span = (6 * max(width + depth, height) + 4) // 5
    return dict(RENDER, center_um=[(a + b) // 2 for a, b in zip(lo, hi)],
                span_um=span, radius_um=2 * span)


def _single(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _check_camera(observed, direction, rig, rgba):
    center = tuple(n / UM_PER_METER for n in rig["center_um"])
    radius = rig["radius_um"] / UM_PER_METER
    expected = camera.world_matrix(direction, center, radius)
    expected_aim = tuple(map(_single, center))
    keys = {"type", "ortho_scale", "matrix_world", "aim", "rgba_sha256", "shift_xy", "pixel_aspect_xy"}
    if type(observed) is not dict or observed.keys() != keys:
        raise AssemblyError("camera_evidence_invalid", 2)
    try:
        span = float.fromhex(observed["ortho_scale"])
        aim = tuple(map(float.fromhex, observed["aim"]))
        matrix = tuple(tuple(map(float.fromhex, row)) for row in observed["matrix_world"])
        if (len(matrix) != 4 or any(len(row) != 4 for row in matrix)
                or not all(math.isfinite(x) for row in matrix for x in row)):
            raise ValueError("matrix")
        # Blender stores orientation as a binary32 transform and may reconstruct
        # it through its rotation representation. Compare integer ppm entries exactly.
        orientation = tuple(tuple(round(x * UM_PER_METER) for x in row[:3]) for row in matrix)
        expected_orientation = tuple(tuple(round(x * UM_PER_METER) for x in row[:3]) for row in expected)
        location = tuple(row[3] for row in matrix[:3])
        expected_location = tuple(_single(row[3]) for row in expected[:3])
        shift = tuple(map(float.fromhex, observed["shift_xy"]))
        aspect = tuple(map(float.fromhex, observed["pixel_aspect_xy"]))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AssemblyError("camera_evidence_invalid", 2) from exc
    if (observed["type"] != "ORTHO" or span != _single(rig["span_um"] / UM_PER_METER)
            or aim != expected_aim or orientation != expected_orientation
            or location != expected_location or matrix[3][3] != 1.0
            or shift != (0.0, 0.0) or aspect != (1.0, 1.0)):
        raise AssemblyError("camera_rig_mismatch")
    if observed["rgba_sha256"] != _file_pin(rgba):
        raise AssemblyError("camera_raster_digest_mismatch", 4)
    return {"center_um": rig["center_um"], "direction_xy": list(direction), "frame": FRAME,
            "span_um": rig["span_um"], "radius_um": rig["radius_um"], "projection": rig["projection"],
            "radius_error_nm": math.ceil(abs(math.dist(location, aim) - radius) * 10**9),
            **observed}


def _denoise(source, window):
    # Preserve RGBA coverage and silhouettes. Filter the wholly opaque
    # interior only. The original render is retained verbatim.
    alpha = source.getchannel("A")
    opaque = alpha.filter(ImageFilter.MinFilter(window)).point([0] * 255 + [255])
    rgb = source.convert("RGB")
    result = Image.composite(rgb.filter(ImageFilter.MedianFilter(window)), rgb, opaque)
    result.putalpha(alpha)
    return result


def _flat_lighting(root, render, digest):
    path = root / "lighting-flat.png"
    if not path.is_file() or digest is None:
        raise AssemblyError("flat_lighting_evidence_unavailable", 3)
    if _file_pin(path) != digest:
        raise AssemblyError("flat_lighting_digest_mismatch", 4)
    with Image.open(path) as source:
        size, sample = render["size"], render["flat_face_sample_size_px"]
        if source.mode != "RGBA" or source.size != (size, size):
            raise AssemblyError("flat_lighting_witness_invalid")
        low = (size - sample) // 2
        patch = source.crop((low, low, low + sample, low + sample))
        if patch.getchannel("A").getextrema() != (255, 255):
            raise AssemblyError("flat_lighting_witness_invalid")
        raw_min, raw_max = patch.convert("L").getextrema()
        filtered = _denoise(source, render["denoise_window_px"])
        luma_min, luma_max = filtered.crop((low, low, low + sample, low + sample)).convert("L").getextrema()
    spread = luma_max - luma_min
    if spread > POLICY["max_flat_face_luma_spread"]:
        raise AssemblyError("flat_lighting_spread")
    return {"sha256": digest, "sample_size_px": sample, "luma_min": luma_min,
            "luma_max": luma_max, "luma_spread": spread, "raw_luma_spread": raw_max - raw_min,
            "denoise_window_px": render["denoise_window_px"]}


def _composite_views(root, rows, cameras=None, rig=None, *, height_um=None, fixture_mode=True):
    if cameras is None or rig is None:
        raise AssemblyError("camera_evidence_unavailable", 3)
    if height_um is None:
        raise AssemblyError("assembled_height_unavailable", 3)
    if type(cameras) is not dict or cameras.keys() != dict(fixture.VIEWS).keys():
        raise AssemblyError("incomplete_camera_observations", 3)
    expected = {"rgba-" + name + ".png" for name, _ in fixture.VIEWS}
    if {path.name for path in root.glob("rgba-*.png")} != expected:
        raise AssemblyError("incomplete_body_views", 3)
    paths, views = [], []
    size = rig["size"]
    grey = tuple(rig["grey"])
    for name, direction in fixture.VIEWS:
        rgba = root / ("rgba-" + name + ".png")
        preview = root / ("humanoid_" + name + ".png")
        camera_witness = _check_camera(cameras[name], direction, rig, rgba)
        with Image.open(rgba) as source:
            if source.mode != "RGBA" or source.size != (size, size):
                raise AssemblyError("invalid_view_raster")
            bound = source.getchannel("A").getbbox()
            if bound is None or min(bound[0], bound[1], size-bound[2], size-bound[3]) <= 0:
                raise AssemblyError("empty_or_clipped_view")
            background = Image.new("RGBA", source.size, (*grey, 255))
            denoised = _denoise(source, rig["denoise_window_px"])
            Image.alpha_composite(background, denoised).convert("RGB").save(preview)
        views.append({"view_id": name, "camera": camera_witness, "camera_digest": pin(encode(camera_witness)),
                      "support_bbox_px": list(bound), "preview_digest": _file_pin(preview),
                      "rgba_digest": _file_pin(rgba)})
        paths.extend((rgba, preview))
    pose = [[row["instance_id"], row["transform"]] for row in rows]
    um_per_cm = UM_PER_METER // 100
    height_cm = (height_um + um_per_cm // 2) // um_per_cm
    return paths, {"asset": "humanoid", "category": "character", "height_cm": height_cm, "height_um": height_um,
                   "views": views, "pose_digest": pin(encode(pose)), "symmetric": None,
                   "symmetry_absence_reason": "AUSENTE: assembled symmetry was not measured",
                   "closeups": [], "materials": [],
                   "invented_details": ["All geometry is original procedural design."] if fixture_mode else [],
                   "source_frames": []}


def assemble(registry, raw, *, expected_recipe_digest, approved_parts, expected_snapshot,
             root, blender, view_template):
    """Verify, render, and register a candidate asset. Root is private attempt evidence."""
    recipe, geometry, meshes, request_parts, pinned = prepare(
        registry, raw, expected_recipe_digest=expected_recipe_digest, approved_parts=approved_parts,
        blender=blender, view_template=view_template,
    )
    if registry.current_snapshot() != expected_snapshot:
        raise AssemblyError("stale_publish_snapshot", 4)
    root.mkdir()
    for name, mesh in meshes.items():
        (root / (name + ".glb")).write_bytes(mesh)
    (root / "recipe.json").write_bytes(raw)
    rig = _render_rig(geometry)
    fixture_mode = recipe.get("geometry_profile", "fixture") == "fixture"
    process = _worker(blender.resolve(strict=True), root, {
        "operation": "assemble", "units": UM_PER_METER, "parts": request_parts,
        "render": rig,
        "max_import_residual_nm": POLICY["max_import_residual_nm"],
    })
    measured = json.loads((root / "worker-result.json").read_bytes())
    if measured["reconstructed_parts"].keys() != approved_parts.keys():
        raise AssemblyError("incomplete_part_reconstruction", 3)
    if measured["render_device"] != rig["device"]:
        raise AssemblyError("unexpected_render_device")
    if any(measured.get(key) != rig[value] for key, value in
           (("render_engine", "engine"), ("samples", "samples"))):
        raise AssemblyError("render_profile_mismatch")
    measured["flat_lighting"] = _flat_lighting(root, rig, measured.get("flat_face_sha256"))
    # For tilted seams, a unit normal's L1 norm is below two. The two
    # reimported surfaces can each move by one measured coordinate residual.
    seam_uncertainty = (2 if fixture_mode else 4) * measured["reconstructed_master"]["max_coordinate_residual_nm"]
    if seam_uncertainty > POLICY["max_export_axis_seam_uncertainty_nm"]:
        raise AssemblyError("export_seam_uncertainty")
    geometry["export_axis_seam_uncertainty_nm"] = seam_uncertainty
    if not fixture_mode:
        geometry["source_seam_uncertainty_nm"] = 4 * max(geometry["source_coordinate_residual_nm"].values())
    view_paths, manifest = _composite_views(
        root, recipe["parts"], measured.get("cameras"), rig,
        height_um=geometry["dimensions_um"][2], fixture_mode=fixture_mode,
    )
    manifest.update(recipe_digest=expected_recipe_digest, view_template_digest=TEMPLATE_DIGEST,
                    geometry_profile=recipe.get("geometry_profile", "fixture"),
                    scene_digest=_file_pin(root / "scene.blend"), part_record_digests=approved_parts,
                    render_profile_digest=recipe["render_profile_sha256"])
    (root / "views.json").write_bytes(encode(manifest))
    geometry["dimensions_um"] = list(geometry["dimensions_um"])
    receipt = encode({"geometry": geometry, "execution": measured, "process": process,
                      "assembly_scene_digest": _file_pin(root / "scene.glb"),
                      "view_manifest_digest": pin(encode(manifest)), "recipe_digest": expected_recipe_digest})
    (root / "receipt.json").write_bytes(receipt)

    mesh = (root / "scene.glb").read_bytes()
    record = _record("humanoid", "asset", mesh, receipt, geometry["dimensions_um"], pinned["taxonomy_sha256"])
    if not fixture_mode:
        record["classification"]["method"] = "registered-rigid-mesh-assembly"
        record["dimensions"]["method"] = "exact-micrometre-mesh-extents"
        record["rights"]["components"][0]["component"] = "registered-rigid-mesh-assembly"
        record["rights"]["components"][0]["absence_reason"] = "AUSENTE: production rights decision for the assembly"
    record["files"][0]["logical_path"] = "scene.glb"
    artifacts = [
        (root / "scene.blend", "blender-scene", "application/x-blender"),
        (root / "recipe.json", "assembly-recipe", "application/json"),
        (root / "views.json", "view-manifest", "application/json"),
        (root / "receipt.json", "assembly-evidence", "application/json"),
        (root / "lighting-flat.png", "lighting-witness", "image/png"),
    ]
    artifacts.extend((path, "body-view", "image/png") for path in view_paths)
    blobs = {pin(data): data for data in (mesh, *pinned.values())}
    for path, role, media in artifacts:
        data = path.read_bytes()
        blobs[pin(data)] = data
        record["files"].append(_descriptor(role, path.name, data, media))
    record["files"].sort(key=lambda entry: (entry["role"], entry["logical_path"], entry["digest"]))
    record["instances"] = [{"instance_id": row["instance_id"], "record_digest": row["part_record_sha256"],
                            "transform": row["transform"], "material_override_digest": None}
                           for row in recipe["parts"]]
    record["lineage"].update(operation="assembled", parent_records=sorted(approved_parts.values()),
                             assembly_recipe_digest=expected_recipe_digest)
    record["representations"]["composition"] = expected_recipe_digest
    record["dependencies"] = sorted(set(blobs) | set(approved_parts.values()))
    encoded = encode(record)
    snapshot = registry.put("humanoid", encoded, blobs=blobs, expected_snapshot=expected_snapshot)
    locator = f"af://{registry.namespace}/humanoid@{pin(encoded)}"
    resolved = registry.resolve(locator, snapshot=snapshot, **READ_LIMITS)
    if resolved["lineage_records"].keys() != set(approved_parts.values()):
        raise AssemblyError("incomplete_registered_lineage", 3)
    (root / "registration.json").write_bytes(encode({
        "locator": locator, "snapshot": snapshot, "parents": approved_parts,
        "closure_objects": len(resolved["objects"]), "closure_bytes": sum(resolved["objects"].values()),
    }))
    return resolved
