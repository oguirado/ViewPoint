"""Prepare and verify an atlas derivative of a cage-bound static mesh."""
from collections import Counter
import importlib.util
import os
from pathlib import Path
import shutil
import struct
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.blender import bake, glb_io, look_bake
from tools.blender.bake_support import Refusal, read_bytes, sha, upstream
from tools.input.evidence import InputIssue, bytes_digest
from tools.mesh.raw_admission import _accessor, _active_meshes, _primitives


UV_MARGIN_PX = 2


def oriented_face(triangle):
    a, b, c = triangle
    return min((a, b, c), (b, c, a), (c, a, b))


def same_geometry(source, derivative, *, max_faces):
    """Compare exact oriented triangles, identifying seam copies by position."""
    try:
        src_doc, src_bin = glb_io.decode_glb(source)
        dst_doc, dst_bin = glb_io.decode_glb(derivative)
        src_nodes = [node.get("name") for node in src_doc["nodes"] if "mesh" in node]
        dst_nodes = [node.get("name") for node in dst_doc["nodes"] if "mesh" in node]
        if len(src_nodes) != 1 or src_nodes != dst_nodes or not src_nodes[0]:
            raise Refusal(4, "uv_derivative_geometry_drift")
        def inventory(doc, binary):
            triangles = Counter(oriented_face(tri) for _, faces in _primitives(doc, binary, max_faces)
                                for tri in faces)
            attrs = [prim["attributes"] for mesh_id in _active_meshes(doc)
                     for prim in doc["meshes"][mesh_id]["primitives"]]
            vertices = {v for prim in attrs for v in _accessor(doc, binary, prim["POSITION"], "fff")}
            return triangles, vertices, attrs

        original, original_vertices, _ = inventory(src_doc, src_bin)
        derived, derived_vertices, attributes = inventory(dst_doc, dst_bin)
        if original != derived or original_vertices != derived_vertices:
            raise Refusal(4, "uv_derivative_geometry_drift")
        if any(not {"NORMAL", "TEXCOORD_0", "TANGENT"} <= attrs.keys() for attrs in attributes):
            raise Refusal(3, "uv_derivative_attributes_missing")
        return sum(original.values())
    except InputIssue as exc:
        raise Refusal(exc.rc, exc.code) from exc
    except (glb_io.GlbError, KeyError, ValueError, IndexError, struct.error) as exc:
        raise Refusal(2, "uv_derivative_glb_invalid") from exc


def prepare(source, output, *, resolution, timeout_s):
    """Run the existing Blender unwrap and export UV0 and tangent attributes."""
    blender = shutil.which("blender")
    if not blender:
        raise Refusal(3, "uv_derivative_blender_unavailable")
    worker = upstream("bake_pbr").WORKER.resolve()
    argv = [blender, "--background", "--factory-startup", "--threads", "1",
            "--python-exit-code", "2", "--python", str(Path(__file__).resolve()),
            "--", str(worker), str(source), str(output), str(resolution)]
    code, log, _ = bake._look_scoped_runner(argv, timeout_s)
    if code != 0 or "AF_UV_DERIVATIVE_READY" not in log.splitlines():
        raise Refusal(3, "uv_derivative_worker_unavailable")
    return read_bytes(output)


def verify_export(candidate, bundle, look_receipt, expected_sha256):
    """Reopen the exported GLB and bind each embedded image to bundle replay."""
    raw = read_bytes(candidate)
    digest = sha(raw)
    if digest != expected_sha256:
        raise Refusal(4, "look_export_bundle_drift")
    geometry = read_bytes(bundle / "source_low.glb")
    maps = look_bake.read_look_maps(raw, geometry)
    if {role: bytes_digest(image) for role, image in maps.items()} != look_receipt["map_digests"]:
        raise Refusal(4, "look_export_bundle_drift")
    return {"export_sha256": digest, "map_digests": look_receipt["map_digests"]}


if __name__ == "__main__":
    # Blender runs this script directly. Use only the pinned worker's importer
    # and UV operation. No Cycles, image projection or mesh repair runs here.
    import bpy

    worker_path, source, output, resolution = sys.argv[sys.argv.index("--") + 1:]
    spec = importlib.util.spec_from_file_location("af_uv_unwrap_worker", worker_path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    meshes = worker.import_meshes(bpy, source)
    if len(meshes) != 1 or meshes[0].modifiers or os.nice(0) != 15:
        raise ValueError("expected a single static mesh in the admitted CPU scope")
    low = meshes[0]
    for layer in list(low.data.uv_layers):
        low.data.uv_layers.remove(layer)
    worker.unwrap(bpy, low, int(resolution), UV_MARGIN_PX)
    # Blender 4.0 rounds both directions to four decimals by default.
    # Preserve enough precision to check the RP3 orthogonality bound.
    from io_scene_gltf2.blender.exp import gltf2_blender_gather_primitives_extract as exporter

    if not hasattr(exporter, "ROUNDING_DIGIT"):
        raise ValueError("Blender glTF exporter precision control is unavailable")
    exporter.ROUNDING_DIGIT = 7
    bpy.ops.export_scene.gltf(filepath=output, export_format="GLB",
                              export_materials="NONE", export_normals=True, export_tangents=True)
    print("AF_UV_DERIVATIVE_READY", flush=True)
