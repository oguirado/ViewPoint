# SPDX-License-Identifier: AGPL-3.0-only
"""Bake the original embedded PBR channels onto the studio UVs with Cycles CPU."""
import argparse
import json
from pathlib import Path
import sys
import time

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mesh import activate, checkpoint, import_mesh, points, require_topology, stats

SIZE = 4096
MARGIN = 8


def image(name, colorspace):
    out = bpy.data.images.new(name, width=SIZE, height=SIZE, alpha=True)
    out.generated_color = (0.0, 0.0, 0.0, 0.0)
    out.colorspace_settings.name = colorspace
    return out


def read_pixels(img):
    values = np.empty(SIZE * SIZE * 4, dtype=np.float32)
    img.pixels.foreach_get(values)
    return values.reshape(-1, 4)


def write_pixels(img, values):
    img.pixels.foreach_set(values.ravel())
    img.update()


def save_png(img, filename):
    img.filepath_raw = str(filename)
    img.file_format = "PNG"
    img.save()
    img.pack()


def select_pair(src, dst):
    activate(dst)
    src.select_set(True)


def bake(src, dst, target, img, bake_type, options, *, margin=None):
    checkpoint(f"before_bake_{img.name}")
    target.image = img
    target.id_data.nodes.active = target
    select_pair(src, dst)
    start = time.perf_counter()
    result = bpy.ops.object.bake(type=bake_type, use_selected_to_active=True, use_cage=options.use_cage,
                                 cage_extrusion=options.extrusion, max_ray_distance=options.ray,
                                 cage_object=options.cage_object,
                                 margin=options.margin if margin is None else margin,
                                 margin_type="EXTEND", normal_space="TANGENT")
    seconds = time.perf_counter() - start
    if result != {"FINISHED"}:
        raise RuntimeError(f"{img.name} bake returned {result}")
    print(f"Bake {img.name}: {seconds:.3f} s", flush=True)
    return seconds


def bake_hit_masks(src, dst, target, emit, options, evidence):
    color = emit.inputs["Color"]
    for link in list(color.links):
        emit.id_data.links.remove(link)
    color.default_value = (1.0, 1.0, 1.0, 1.0)
    witness = image("RayHitWitness", "Non-Color")
    for name, margin in (("direct", 0), ("extended", options.margin)):
        bake(src, dst, target, witness, "EMIT", options, margin=margin)
        hits = read_pixels(witness)[:, 0] > 0.5
        np.save(evidence / f"bake-ray-{name}.npy", np.packbits(hits))
    bpy.data.images.remove(witness)


def projection_cage(dst, target, margin):
    """Point the local rebake along registered exterior views to avoid neighboring sheets."""
    x, y, z = points(dst).T

    def falloff(distance, inner, outer):
        t = np.clip((outer - distance) / (outer - inner), 0, 1)
        return t * t * (3 - 2 * t)

    gem = falloff(np.hypot(x, z + 0.061), 0.082, 0.113) * (y < -0.13)
    arm = (falloff(np.abs(y - 0.003), 0.062, 0.083)
           * falloff(np.abs(z + 0.148), 0.026, 0.047) * (x > 0.27))
    dome = (falloff(np.abs(x + 0.022), 0.063, 0.108)
            * falloff(np.abs(z - 0.248), 0.033, 0.060) * (y > 0.09))
    regions = np.stack((gem, arm, dome), axis=1).astype(np.float32)
    if not np.all(np.count_nonzero(regions, axis=0) > 0) or np.any(np.count_nonzero(regions, axis=1) > 1):
        raise RuntimeError("local bake must cover all three disjoint regions")
    weights = regions.sum(axis=1)
    normals = np.array([v.normal[:] for v in dst.data.vertices])
    directions = regions @ np.array([[0, -1, 0], [1, 0, 0], [0, 1, 0]])
    support = np.maximum(weights[:, None], 1e-8)
    directions = directions / support
    active = weights[:, None] > 0
    offset = np.where(active, 0.055 * directions, 0.010 * normals)
    cage = dst.copy()
    cage.data = dst.data.copy()
    cage.name = "Registered exterior projection cage"
    bpy.context.collection.objects.link(cage)
    for vertex, delta in zip(cage.data.vertices, offset):
        vertex.co += type(vertex.co)(delta)

    # Bake the same point weights onto both sides of each UV seam, with padding.
    attribute = dst.data.attributes.new(name="LocalProjectionWeight", type="FLOAT", domain="POINT")
    attribute.data.foreach_set("value", weights)
    tree = target.id_data
    out = next(n for n in tree.nodes if n.type == "OUTPUT_MATERIAL")
    shader = out.inputs["Surface"].links[0].from_socket
    attr_node = tree.nodes.new("ShaderNodeAttribute")
    attr_node.attribute_name = attribute.name
    emit = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(attr_node.outputs["Fac"], emit.inputs["Color"])
    tree.links.new(emit.outputs[0], out.inputs["Surface"])
    weight_image = image("LocalProjectionWeight", "Non-Color")
    target.image = weight_image
    tree.nodes.active = target
    activate(dst)
    checkpoint("before_local_weight_bake")
    if bpy.ops.object.bake(type="EMIT", use_selected_to_active=False, use_cage=False,
                          margin=margin, margin_type="EXTEND") != {"FINISHED"}:
        raise RuntimeError("local projection weight bake failed")
    mix = read_pixels(weight_image)[:, 0].copy()
    tree.links.new(shader, out.inputs["Surface"])
    tree.nodes.remove(emit)
    tree.nodes.remove(attr_node)
    dst.data.attributes.remove(attribute)
    bpy.data.images.remove(weight_image)
    record = {"method": "Cycles selected-to-active custom cage in three registered exterior directions",
              "exterior_directions": {"gem_ring": [0, -1, 0], "lower_arm": [1, 0, 0], "dome_back": [0, 1, 0]},
              "projection_extrusion_source_units": 0.055, "vertex_counts": np.count_nonzero(regions, axis=0).tolist(),
              "weighted_atlas_texels": int(np.count_nonzero(mix))}
    return cage, mix, record


def materialize(material, base, packed, normal):
    tree = material.node_tree
    shader = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")

    def texture(img, label):
        node = tree.nodes.new("ShaderNodeTexImage")
        node.image = img
        node.label = label
        return node

    color = texture(base, "Base Color")
    mr = texture(packed, "Metallic Roughness")
    normals = texture(normal, "Tangent Normal")
    split = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(mr.outputs["Color"], split.inputs["Color"])
    tree.links.new(split.outputs["Green"], shader.inputs["Roughness"])
    tree.links.new(split.outputs["Blue"], shader.inputs["Metallic"])
    tree.links.new(color.outputs["Color"], shader.inputs["Base Color"])
    tree.links.new(color.outputs["Alpha"], shader.inputs["Alpha"])
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    tree.links.new(normals.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], shader.inputs["Normal"])
    material.blend_method = "BLEND"
    material.use_backface_culling = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extrusion", type=float, default=0.012)
    parser.add_argument("--ray", type=float, default=0.05)
    parser.add_argument("--margin", type=int, default=MARGIN)
    parser.add_argument("--use-cage", action="store_true")
    parser.add_argument("--local-projection", action="store_true")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--size", type=int, default=4096)
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    global SIZE
    SIZE = args.size
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    evidence = output / "evidence"
    evidence.mkdir(exist_ok=True)
    scene = bpy.context.scene
    dst = bpy.context.active_object
    args.cage_object = ""
    require_topology(stats(dst))
    src = import_mesh(args.source)
    if len(src.data.materials) != 1 or not src.data.materials[0].use_nodes:
        raise RuntimeError("source must have one node-based PBR material")
    for obj in (src, dst):
        coords = points(obj)
        print(f"Bake subject {obj.name}: bounds {coords.min(axis=0)} / {coords.max(axis=0)}, "
              f"UV layers {[uv.name for uv in obj.data.uv_layers]}", flush=True)
    source_material = src.data.materials[0]
    source_tree = source_material.node_tree
    shader = next(node for node in source_tree.nodes if node.type == "BSDF_PRINCIPLED")
    out = next(node for node in source_tree.nodes if node.type == "OUTPUT_MATERIAL")
    expected = ("Base Color", "Alpha", "Roughness", "Metallic")
    sockets = {}
    for key in expected:
        value = shader.inputs[key]
        if value.is_linked:
            sockets[key] = value.links[0].from_socket
        elif key == "Base Color":
            node = source_tree.nodes.new("ShaderNodeRGB")
            node.outputs[0].default_value = value.default_value
            sockets[key] = node.outputs[0]
        else:
            node = source_tree.nodes.new("ShaderNodeValue")
            node.outputs[0].default_value = value.default_value
            sockets[key] = node.outputs[0]
    emit = source_tree.nodes.new("ShaderNodeEmission")
    pack = source_tree.nodes.new("ShaderNodeCombineColor")
    for channel, field in zip(("Red", "Green", "Blue"), ("Alpha", "Roughness", "Metallic")):
        source_tree.links.new(sockets[field], pack.inputs[channel])
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.use_denoising = False
    scene.cycles.use_preview_denoising = False
    scene.cycles.samples = 1
    scene.render.threads_mode = "FIXED"
    scene.render.threads = args.threads
    scene.render.bake.use_selected_to_active = True
    scene.render.bake.use_cage = args.use_cage
    scene.render.bake.max_ray_distance = args.ray
    scene.render.bake.cage_extrusion = args.extrusion
    scene.render.bake.margin = args.margin
    scene.render.bake.margin_type = "EXTEND"
    scene.render.bake.use_clear = True
    scene.render.bake.normal_space = "TANGENT"

    mat = bpy.data.materials.new("Studio PBR")
    mat.use_nodes = True
    dst.data.materials.clear()
    dst.data.materials.append(mat)
    target = mat.node_tree.nodes.new("ShaderNodeTexImage")
    base = image("base-color", "sRGB")
    packed = image("metallic-roughness", "Non-Color")
    normal = image("normal", "Non-Color")
    source_tree.links.new(emit.outputs[0], out.inputs["Surface"])
    source_tree.links.new(sockets["Base Color"], emit.inputs["Color"])
    bake(src, dst, target, base, "EMIT", args)
    source_tree.links.new(pack.outputs["Color"], emit.inputs["Color"])
    bake(src, dst, target, packed, "EMIT", args)
    bake_hit_masks(src, dst, target, emit, args, evidence)
    source_tree.links.new(shader.outputs["BSDF"], out.inputs["Surface"])
    bake(src, dst, target, normal, "NORMAL", args)

    base_pixels = read_pixels(base)
    data_pixels = read_pixels(packed)
    normal_pixels = read_pixels(normal)
    local_projection = None
    if args.local_projection:
        cage, mix, local_projection = projection_cage(dst, target, args.margin)
        local_options = argparse.Namespace(use_cage=True, cage_object=cage.name, extrusion=0.0,
                                           ray=0.10, margin=args.margin)
        local_projection["max_ray_distance_operator_argument_source_units"] = local_options.ray
        local = image("LocalColor", "sRGB")
        local_data = image("LocalMetallicRoughness", "Non-Color")
        local_normals = image("LocalNormal", "Non-Color")
        source_tree.links.new(emit.outputs[0], out.inputs["Surface"])
        source_tree.links.new(sockets["Base Color"], emit.inputs["Color"])
        bake(src, dst, target, local, "EMIT", local_options)
        source_tree.links.new(pack.outputs["Color"], emit.inputs["Color"])
        bake(src, dst, target, local_data, "EMIT", local_options)
        source_tree.links.new(shader.outputs["BSDF"], out.inputs["Surface"])
        bake(src, dst, target, local_normals, "NORMAL", local_options)
        projection_data = read_pixels(local_data)
        indices = np.flatnonzero((mix > 0) & (projection_data[:, 0] > 0.5))
        blend = mix[indices, None]
        local_projection["reprojected_texels"] = len(indices)
        for destination, transfer in ((base_pixels, read_pixels(local)), (data_pixels, projection_data),
                                      (normal_pixels, read_pixels(local_normals))):
            destination[indices] += blend * (transfer[indices] - destination[indices])
        del transfer, projection_data, mix, blend, indices
        for image_data in (local, local_data, local_normals):
            bpy.data.images.remove(image_data)
        bpy.data.objects.remove(cage, do_unlink=True)
    alpha = data_pixels[:, 0]
    hit = alpha > 0.5
    print(f"Bake texel ranges: base {base_pixels.min(axis=0)} / {base_pixels.max(axis=0)}, "
          f"packed {data_pixels.min(axis=0)} / {data_pixels.max(axis=0)}, hits {hit.sum()}", flush=True)
    if hit.sum() < SIZE ** 2 * 0.05:
        raise RuntimeError("insufficient selected-to-active texture transfer")
    np.save(evidence / "bake-hits.npy", np.packbits(hit))
    vectors = normal_pixels[:, :3] * 2.0 - 1.0
    magnitude = np.linalg.norm(vectors, axis=1)
    valid_normal = hit & (magnitude > 0.5)
    flipped = valid_normal & (vectors[:, 2] < 0)
    # The raw glTF is two-sided. Make the sampled normal face the receiving
    # surface's shading hemisphere, as it would on the visible source side.
    vectors[flipped] *= -1
    vectors[valid_normal] /= magnitude[valid_normal, None]
    vectors[~valid_normal] = (0.0, 0.0, 1.0)
    normal_pixels[:, :3] = np.clip(0.5 + 0.5 * vectors, 0.0, 1.0)
    normal_pixels[:, 3] = 1.0
    write_pixels(normal, normal_pixels)
    # Keep source alpha near a missed ray so missing color cannot be hidden from render comparison.
    base_pixels[:, 3] = np.where(hit, alpha, 1.0)
    data_pixels[:, 0] = 0
    write_pixels(base, base_pixels)
    write_pixels(packed, data_pixels)
    mat.node_tree.nodes.remove(target)
    materialize(mat, base, packed, normal)
    save_png(base, output / "base-color.png")
    save_png(packed, output / "metallic-roughness.png")
    save_png(normal, output / "normal.png")
    del base_pixels, data_pixels, alpha
    record = {"engine": "Cycles CPU", "blender": bpy.app.version_string, "image_size_px": SIZE,
              "selected_to_active": True, "use_cage": args.use_cage, "ray_start_extrusion_source_units": args.extrusion,
              "max_ray_distance_operator_argument_source_units": args.ray,
              "max_ray_distance_source_units": args.ray if not args.use_cage else None,
              "margin_px": args.margin, "tangent_normal": "+X +Y +Z", "source_normal_map": False,
              "ray_hit_witness": {"method": "constant white source emission, same selected-to-active cage",
                                  "direct_mask": "bake-ray-direct.npy", "direct_margin_px": 0,
                                  "extended_mask": "bake-ray-extended.npy", "extended_margin_px": args.margin},
              "source_opacity_transfer_marked_atlas_texels_including_margin": int(hit.sum()),
              "normal_valid_texels_including_margin": int(valid_normal.sum()),
              "normal_faceforward_flips_including_margin": int(flipped.sum())}
    if local_projection:
        record["local_projection"] = local_projection
    (evidence / "bake.json").write_text(json.dumps(record, indent=2) + "\n")
    checkpoint("before_export")
    activate(dst)
    result = bpy.ops.export_scene.gltf(filepath=str(output / "studio.glb"), export_format="GLB",
                                        use_selection=True, export_tangents=True, export_yup=True,
                                        export_image_format="AUTO")
    if result != {"FINISHED"}:
        raise RuntimeError(f"GLB export returned {result}")


if __name__ == "__main__":
    main()
