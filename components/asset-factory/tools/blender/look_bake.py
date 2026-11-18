"""Fixture-level texture encoding and Blender instrumentation for LOOK."""
import json
import os
import sys
from pathlib import Path

from .bake_support import Refusal, upstream


# The protected bundle and native stage fingerprints both hash this module.
# Match the precision used to prepare the registered QuadriFlow UV derivative.
PROTECTED_GLTF_ROUNDING_DIGITS = 7


def normal_probe(geometry, atlas, probe, samples):
    """Bind an independent signed relief observation to its GLB tangent frame."""
    from fractions import Fraction

    from . import glb_io, uv_quality

    if type(probe) is not dict or probe.keys() != {"texel", "cross_nt_sign"}:
        raise Refusal(2, "look_normal_probe_invalid")
    texel, cross_sign = probe["texel"], probe["cross_nt_sign"]
    if (type(texel) is not int or texel not in samples
            or type(cross_sign) is not int or cross_sign not in (-1, 1)):
        raise Refusal(2, "look_normal_probe_invalid")
    width, height = atlas["size"]
    y, x = divmod(texel, width)
    center = (Fraction(2*x + 1, 2*width), Fraction(2*y + 1, 2*height))
    doc, binary = glb_io.decode_glb(geometry)
    signs = []
    # The caller has already checked active geometry, UV bounds and the MikkTSpace
    # frame on every corner. Find the unique owner of this independently pinned texel.
    for mesh_id in uv_quality._active_meshes(doc):
        for prim in doc["meshes"][mesh_id]["primitives"]:
            attrs = prim["attributes"]
            uv = [tuple(map(Fraction, pair))
                  for pair in uv_quality._accessor(doc, binary, attrs["TEXCOORD_0"], "ff")]
            indices = iter(range(len(uv)))
            if "indices" in prim:
                index_id = prim["indices"]
                code = {5121: "B", 5123: "H", 5125: "I"}[doc["accessors"][index_id]["componentType"]]
                indices = (row[0] for row in uv_quality._accessor(doc, binary, index_id, code))
            tangents = list(uv_quality._accessor(doc, binary, attrs["TANGENT"], "ffff"))
            for tri in zip(indices, indices, indices):
                if min(uv_quality.weights(tuple(uv[i] for i in tri), center)) >= 0:
                    signs.append(int(tangents[tri[0]][3]))
    if len(signs) != 1:
        raise Refusal(3, "look_normal_probe_ownership_unresolved")
    # GLB bitangent B = w * cross(N, T). A sign against cross(N, T)
    # describes a physical direction independently of the encoded green channel.
    return {**probe, "tangent_w": signs[0], "expected_y_sign": signs[0] * cross_sign}


DATA_MAPS = ("normal", "ao", "roughness", "metal")
LOOK_MAP_ENCODINGS = {"base_color": "srgb", "normal": "linear", "orm": "linear"}
# The GLB uses the verified base level. Explicit chart-owned mips travel as
# separate evidence since an embedded PNG cannot specify the runtime mip chain.
LOOK_SAMPLER = {"magFilter": 9729, "minFilter": 9729, "wrapS": 33071, "wrapT": 33071}
_LINEAR_SCALE = 1_000_000_000
_SRGB_LINEAR = tuple(round(_LINEAR_SCALE * (
    byte / 255 / 12.92 if byte <= 10 else ((byte / 255 + 0.055) / 1.055)**2.4))
    for byte in range(256))


def _encode_srgb(linear):
    unit = min(linear / _LINEAR_SCALE, 1.0)
    value = 12.92 * unit if unit <= 0.0031308 else 1.055 * unit**(1 / 2.4) - 0.055
    return int(255 * value + 0.5)


def look_mips(pixels, size, level, role):
    """Box-filter sRGB colour in linear light and never transfer alpha/data channels."""
    mips = [pixels]
    for _ in range(level):
        mip = bytearray()
        for y in range(0, size, 2):
            for x in range(0, size, 2):
                first = 4 * (y * size + x)
                corners = (first, first + 4, first + 4 * size, first + 4 * size + 4)
                for c in range(4):
                    values = [pixels[i + c] for i in corners]
                    if role == "base_color" and c < 3:
                        mip.append(_encode_srgb(sum(_SRGB_LINEAR[v] for v in values) / 4))
                    else:
                        mip.append((sum(values) + 2) // 4)
        pixels = bytes(mip)
        mips.append(pixels)
        size //= 2
    return mips


def chart_pixels(pixels, samples, gutters):
    """Copy only independently observed/chart-owned texels and their declared padding."""
    output = bytearray(len(pixels))
    for index in samples:
        start = 4 * index
        output[start:start + 4] = pixels[start:start + 4]
    for dst, src in gutters:
        output[4 * dst:4 * dst + 4] = output[4 * src:4 * src + 4]
    return bytes(output)


def debake(projected, samples, attenuation, floor):
    output = bytearray(len(projected))
    for index in samples:
        start = 4 * index
        if attenuation[index] < floor:
            raise Refusal(3, "look_attenuation_unresolved")
        output[start:start + 4] = bytes([
            *(_encode_srgb(_SRGB_LINEAR[v] * 255 / attenuation[index])
              for v in projected[start:start + 3]), 255])
    return bytes(output)


def relight_metrics(candidate, reference, regions, lights, changed, size, tile):
    """Fixture-domain radiance residuals from independently pinned linear irradiance."""
    from gates.texture_cleanliness import _SRGB_SCALE, _srgb_bounds

    bounds = _srgb_bounds()
    face_max, shadow_max, tiles = 0, 0, {}
    for i, region in enumerate(regions):
        if not region:
            continue
        start = i * 4
        pairs = list(zip(candidate[start:start + 3], reference[start:start + 3]))
        if region == 2:
            if changed[i] > lights[i]:
                darkening = max((bounds[b][1] - bounds[a][0] for a, b in pairs if a < b), default=0)
                shadow_max = max(shadow_max, darkening * changed[i])
            continue
        errors = [bounds[max(a, b)][1] - bounds[min(a, b)][0] if a != b else 0 for a, b in pairs]
        error = max(errors)
        face_max = max(face_max, error * changed[i])
        y, x = divmod(i, size)
        key = (x // tile, y // tile)
        total, count = tiles.get(key, (0, 0))
        tiles[key] = (total + sum(errors) * changed[i], count + 3)

    def upper(error, count=1):
        denominator = _SRGB_SCALE * 255 * count
        return (error * 1_000_000 + denominator - 1) // denominator

    return {
        "worst_face_tile_upper_ppm": max(upper(total, count) for total, count in tiles.values()),
        "changed_face_max_upper_ppm": upper(face_max),
        "changed_shadow_max_upper_ppm": upper(shadow_max),
        "changed_light_texels": sum(a != b and bool(region) for a, b, region in zip(lights, changed, regions)),
        "face_tiles_checked": len(tiles),
    }


def embed_look_maps(geometry, images):
    """Bind checked maps in one UV0 material slot without rewriting geometry bytes."""
    from . import glb_io

    try:
        doc, raw_bin = glb_io.decode_glb(geometry)
    except glb_io.GlbError as exc:
        raise Refusal(2, "look_geometry_glb_invalid") from exc
    if set(images) != LOOK_MAP_ENCODINGS.keys() or any(doc.get(k) for k in ("images", "textures", "samplers")):
        raise Refusal(3, "look_unsupported_source_materials")
    binary = bytearray(raw_bin)
    doc.update(images=[], textures=[], samplers=[dict(LOOK_SAMPLER)])
    for index, role in enumerate(LOOK_MAP_ENCODINGS):
        binary += b"\0" * (-len(binary) % 4)
        image = images[role]
        view = len(doc["bufferViews"])
        doc["bufferViews"].append({"buffer": 0, "byteOffset": len(binary), "byteLength": len(image)})
        binary += image
        doc["images"].append({"bufferView": view, "mimeType": "image/png"})
        doc["textures"].append({"source": index, "sampler": 0})
    doc["buffers"][0]["byteLength"] = len(binary)
    binary += b"\0" * (-len(binary) % 4)
    doc["materials"] = [{"pbrMetallicRoughness": {
        "baseColorTexture": {"index": 0}, "metallicRoughnessTexture": {"index": 2}},
        "normalTexture": {"index": 1}, "occlusionTexture": {"index": 2}}]
    for mesh in doc["meshes"]:
        for prim in mesh["primitives"]:
            prim["material"] = 0
    return glb_io.encode_glb(doc, bytes(binary))


def read_look_maps(raw, geometry):
    """Read the actual material slots and compare the geometry/atlas byte preimage."""
    from . import glb_io

    try:
        doc, binary = glb_io.decode_glb(raw)
    except glb_io.GlbError as exc:
        raise Refusal(2, "look_textured_glb_invalid") from exc
    bindings = {}
    if doc.get("samplers") != [LOOK_SAMPLER] or len(doc.get("materials", [])) != 1:
        raise Refusal(5, "look_material_or_sampler_drift")
    for mesh in doc["meshes"]:
        for prim in mesh["primitives"]:
            if prim.get("material") != 0 or not {"TEXCOORD_0", "TANGENT"} <= prim["attributes"].keys():
                raise Refusal(5, "look_material_slot_unbound")
    material = doc["materials"][0]
    pbr = material.get("pbrMetallicRoughness", {})
    infos = {"base_color": pbr.get("baseColorTexture"), "normal": material.get("normalTexture"),
             "orm": pbr.get("metallicRoughnessTexture")}
    if infos["orm"] != material.get("occlusionTexture"):
        raise Refusal(5, "look_orm_binding_drift")
    indices = set()
    for role, info in infos.items():
        if not isinstance(info, dict) or info.keys() != {"index"}:
            raise Refusal(5, "look_texture_slot_unbound")
        index = info["index"]
        tex = doc["textures"][index]
        image = doc["images"][tex["source"]]
        if tex.get("sampler") != 0 or image["mimeType"] != "image/png":
            raise Refusal(5, "look_texture_sampler_or_encoding")
        view = doc["bufferViews"][image["bufferView"]]
        start = view.get("byteOffset", 0)
        bindings[role] = binary[start:start + view["byteLength"]]
        indices.add(tex["source"])
    if len(indices) != len(LOOK_MAP_ENCODINGS):
        raise Refusal(5, "look_texture_role_alias")
    # Rebuilding the constrained encoding checks the exact primitive, image and
    # accessor inventory, including material factors and geometry buffer prefix.
    if embed_look_maps(geometry, bindings) != raw:
        raise Refusal(5, "look_geometry_or_material_drift")
    return bindings


def decode_look_png(raw, size):
    """Bound decompression for the canonical RGBA8 texture encoding."""
    import struct
    import zlib

    png = upstream("orm_pack")
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    if len(raw) < 57 or raw[:8] != png.PNG_SIG or raw[8:29] != b"\0\0\0\rIHDR" + header:
        raise Refusal(5, "look_texture_encoding_invalid")
    length = int.from_bytes(raw[33:37], "big")
    if raw[37:41] != b"IDAT" or len(raw) != 57 + length:
        raise Refusal(5, "look_texture_encoding_invalid")
    stride = size * 4 + 1
    decoder = zlib.decompressobj()
    try:
        data = decoder.decompress(raw[41:41 + length], size * stride + 1)
    except zlib.error as exc:
        raise Refusal(5, "look_texture_encoding_invalid") from exc
    if len(data) != size * stride or not decoder.eof or decoder.unused_data or any(data[::stride]):
        raise Refusal(5, "look_texture_encoding_invalid")
    pixels = b"".join(data[i + 1:i + stride] for i in range(0, len(data), stride))
    if png.encode_png(size, size, pixels) != raw:
        raise Refusal(5, "look_texture_encoding_invalid")
    return pixels


def run_look_worker(path, threads, preserve_atlas):
    """Blender entry point for the pinned sibling CPU worker."""
    import importlib.util
    from types import SimpleNamespace

    import bpy

    spec = importlib.util.spec_from_file_location("look_bake_worker", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    worker.THREADS = threads

    if preserve_atlas:
        from io_scene_gltf2.blender.exp import gltf2_blender_gather_primitives_extract as exporter

    def preserve_uv(_bpy, obj, _res, _margin):
        if len(obj.data.uv_layers) != 1:
            raise ValueError("LOOK-BAKE requires the admitted UV0")
        obj.data.uv_layers.active_index = 0

    def export(**kwargs):
        if not preserve_atlas:
            return bpy.ops.export_scene.gltf(**kwargs, export_normals=True, export_tangents=True)
        original_rounding = exporter.ROUNDING_DIGIT
        try:
            exporter.ROUNDING_DIGIT = PROTECTED_GLTF_ROUNDING_DIGITS
            return bpy.ops.export_scene.gltf(**kwargs, export_normals=True, export_tangents=True)
        finally:
            exporter.ROUNDING_DIGIT = original_rounding

    if preserve_atlas:
        worker.unwrap = preserve_uv
    ops = SimpleNamespace(**{key: getattr(bpy.ops, key) for key in ("wm", "object", "uv", "mesh", "import_scene")},
                          export_scene=SimpleNamespace(gltf=export))
    args = worker.parse_args(sys.argv)
    worker.require_ram_out(args.out_dir)
    report = worker.bake(SimpleNamespace(ops=ops, context=bpy.context, data=bpy.data), args)
    output = Path(args.out_dir)
    spaces = {}
    for image in bpy.data.images:
        image_path = Path(image.filepath_raw)
        if image_path.parent == output and image_path.stem in report["maps"]:
            role = image_path.stem
            if role in spaces:
                raise ValueError("ambiguous Blender bake image: " + role)
            spaces[role] = image.colorspace_settings.name
    report["map_color_spaces"] = spaces
    if preserve_atlas:
        report["gltf_rounding_digits"] = PROTECTED_GLTF_ROUNDING_DIGITS
    scene = bpy.context.scene
    report.update(threads=scene.render.threads, device=scene.cycles.device,
                  cpu_affinity=sorted(os.sched_getaffinity(0)), nice=os.nice(0))
    print("BVQA_BAKE " + json.dumps(report, sort_keys=True, separators=(",", ":")))
