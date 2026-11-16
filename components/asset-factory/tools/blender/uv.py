"""UV admission using the sibling uv_gate, with non-empty UV evidence required."""
from .bake_support import Refusal, read_bytes, require_receipt, result, safe_glb, sha, upstream


def check_uv(mesh, *, previous=None, max_stretch=4000, allow_mirror=False):
    """No path is written. Uniform mirroring is an upstream coverage limitation.

    max_stretch is a caller policy in permille, not a calibrated factory threshold.
    """
    try:
        if type(max_stretch) is not int or max_stretch < 1000 or type(allow_mirror) is not bool:
            raise Refusal(2, "uv_policy_invalid")
        raw = read_bytes(mesh)
        safe_glb(raw)
        gate = upstream("uv_gate")
        args = ["--mesh", str(mesh), "--require-uv", "1", "--max-stretch", str(max_stretch),
                "--allow-mirror", "1" if allow_mirror else "0"]
        prev = sha(read_bytes(previous)) if previous else ""
        if previous:
            args += ["--prev-receipt", str(previous)]
        rc, receipt = gate.run(args)
        require_receipt(rc, receipt, gate.SCHEMA, link="prev_sha256", previous=prev)
        if receipt.get("input_sha256") != sha(raw) or read_bytes(mesh) != raw:
            raise Refusal(4, "uv_input_drift")
        if not receipt.get("primitives_checked") or not receipt.get("prims") or any(
                type(p.get("uv_tris")) is not int or p["uv_tris"] <= 0 for p in receipt["prims"]):
            raise Refusal(3, "uv_zero_checks")
        return result(0, receipt=receipt)
    except Refusal as exc:
        return result(exc.rc, exc.reason)
    except Exception as exc:
        return result(2, "uv_error", detail=str(exc))


def check_textured_glb(mesh, *, expected_maps, sampler_policy,
                       expected_geometry=None, expected_geometry_sha256=None):
    """Read-only handoff gate for a future final material-binding owner.

    Callers pin albedo/normal/ORM image-file digests and the full glTF sampler
    policy. No glTF defaults or inferred texture identities pass this boundary.
    ORM is linear by its occlusion/metallicRoughness bindings; albedo is sRGB by
    its baseColor binding. Texture transforms/extensions are unsupported.
    """
    try:
        if set(expected_maps) != {"albedo", "normal", "orm"} or set(sampler_policy) != {
                "magFilter", "minFilter", "wrapS", "wrapT"} or any(type(v) is not int for v in sampler_policy.values()):
            raise Refusal(2, "binding_policy_invalid")
        for digest in expected_maps.values():
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise Refusal(2, "binding_policy_invalid")
        raw = read_bytes(mesh)
        doc = safe_glb(raw)
        if doc.get("extensions") or doc.get("extensionsUsed") or doc.get("extensionsRequired"):
            raise Refusal(3, "root_extension_unsupported")
        _, binary = upstream("glb_core")._split(raw)
        if not doc.get("materials"):
            raise Refusal(3, "materials_missing")

        def image_hash(binding):
            if not isinstance(binding, dict) or "index" not in binding:
                raise Refusal(5, "texture_binding_missing")
            if binding.get("extensions") or binding.get("texCoord", 0) != 0:
                raise Refusal(3, "texture_mapping_unsupported")
            if set(binding) - {"index", "texCoord", "scale", "strength"}:
                raise Refusal(3, "texture_info_unsupported")
            if any(type(binding.get(key, 1)) not in (int, float) or binding.get(key, 1) != 1
                   for key in ("scale", "strength")):
                raise Refusal(5, "texture_factor_drift")
            index = binding["index"]
            if type(index) is not int or index < 0:
                raise Refusal(2, "texture_index_invalid")
            texture = doc.get("textures", [])[index]
            if texture.get("extensions"):
                raise Refusal(3, "texture_mapping_unsupported")
            if set(texture) - {"source", "sampler", "name"}:
                raise Refusal(3, "texture_key_unsupported")
            source, sampler = texture.get("source"), texture.get("sampler")
            if type(source) is not int or source < 0 or type(sampler) is not int or sampler < 0:
                raise Refusal(5, "texture_binding_missing")
            if doc.get("samplers", [])[sampler] != sampler_policy:
                raise Refusal(5, "sampler_drift")
            image = doc.get("images", [])[source]
            if image.get("mimeType") != "image/png" or type(image.get("bufferView")) is not int or image["bufferView"] < 0:
                raise Refusal(3, "embedded_png_required")
            view = doc.get("bufferViews", [])[image["bufferView"]]
            offset, length = view.get("byteOffset", 0), view.get("byteLength")
            if view.get("buffer") != 0 or type(offset) is not int or type(length) is not int or offset < 0 or length <= 0 or offset + length > len(binary):
                raise Refusal(2, "image_view_invalid")
            png = binary[offset:offset + length]
            upstream("orm_pack").decode_png(png)
            return sha(png)

        checked = 0
        for mesh_doc in doc.get("meshes", []):
            for primitive in mesh_doc.get("primitives", []):
                if primitive.get("extensions"):
                    raise Refusal(3, "primitive_extension_unsupported")
                material = primitive.get("material")
                if type(material) is not int or material < 0 or "TEXCOORD_0" not in primitive.get("attributes", {}):
                    raise Refusal(5, "textured_primitive_missing")
                mat = doc["materials"][material]
                if mat.get("extensions"):
                    raise Refusal(3, "material_extension_unsupported")
                if set(mat) - {"name", "pbrMetallicRoughness", "normalTexture", "occlusionTexture"}:
                    raise Refusal(3, "material_key_unsupported")
                pbr = mat.get("pbrMetallicRoughness", {})
                if set(pbr) - {"baseColorTexture", "metallicRoughnessTexture", "baseColorFactor", "metallicFactor", "roughnessFactor"}:
                    raise Refusal(3, "pbr_key_unsupported")
                if pbr.get("baseColorFactor", [1, 1, 1, 1]) != [1, 1, 1, 1] or any(
                        type(pbr.get(key, 1)) not in (int, float) or pbr.get(key, 1) != 1
                        for key in ("metallicFactor", "roughnessFactor")):
                    raise Refusal(5, "material_factor_drift")
                bindings = [("albedo", pbr.get("baseColorTexture")), ("normal", mat.get("normalTexture")),
                            ("orm", mat.get("occlusionTexture")), ("orm", pbr.get("metallicRoughnessTexture"))]
                for role, binding in bindings:
                    if image_hash(binding) != expected_maps[role]:
                        raise Refusal(5, "texture_bytes_drift")
                    checked += 1
        if not checked:
            raise Refusal(3, "binding_zero_checks")
        if expected_geometry is None or expected_geometry_sha256 is None:
            raise Refusal(3, "geometry_pin_missing")
        geometry = read_bytes(expected_geometry)
        if sha(geometry) != expected_geometry_sha256:
            raise Refusal(4, "geometry_pin_drift")
        safe_glb(geometry)
        g = upstream("glb_core")
        def inventory(data):
            return [(p.world_q16, p.positions_um, p.indices, p.uvs_q16)
                    for p in g.parse_glb(data).prims]
        if inventory(raw) != inventory(geometry):
            raise Refusal(5, "geometry_or_uv_drift")
        uv_result = check_uv(mesh)
        if uv_result["rc"]:
            return uv_result
        if read_bytes(expected_geometry) != geometry:
            raise Refusal(4, "geometry_pin_drift")
        tool = upstream("pbr_validate")
        rc, rec = tool.run(["--glb", str(mesh)])
        require_receipt(rc, rec, tool.SCHEMA)
        if not rec.get("checks"):
            raise Refusal(3, "pbr_zero_checks")
        if read_bytes(mesh) != raw or rec.get("input_sha256") != sha(raw):
            raise Refusal(4, "pbr_input_drift")
        return result(0, checks_evaluated=checked + rec["checks"], receipt=rec)
    except Refusal as exc:
        return result(exc.rc, exc.reason)
    except Exception as exc:
        return result(2, "binding_error", detail=str(exc))
