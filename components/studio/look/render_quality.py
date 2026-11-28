# SPDX-License-Identifier: AGPL-3.0-only
"""Registered studio SET checks and their Blender witness images."""
import hashlib
from pathlib import Path

import numpy as np

DARK_LIMIT = 0.08
MIN_IOU = 0.95
MIN_COVERAGE = 5
OFFSETS = ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1),
           (-.707, -.707), (-.707, .707), (.707, -.707), (.707, .707))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def studio_opaque(meshes):
    materials = {mat for obj in meshes for mat in obj.data.materials}
    if not materials or any(mat is None or mat.name.split(".")[0] != "Studio PBR"
                            for mat in materials):
        raise RuntimeError("studio_render_actor_material_roster")
    for mat in materials:
        # Eevee BLEND sorts whole objects. The baked coverage belongs to one
        # closed, skinned mesh, whose internal faces sort incorrectly in BLEND.
        # Keep the baked alpha hookup for other renderers and edit the SET copy.
        mat.blend_method = "OPAQUE"
        mat.shadow_method = "OPAQUE"
        (shader,) = [n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"]
        expected = {"base-color": "sRGB", "metallic-roughness": "Non-Color", "normal": "Non-Color"}
        textures = {n.image.name.split(".")[0]: n.image.colorspace_settings.name
                    for n in mat.node_tree.nodes if n.type == "TEX_IMAGE"}
        if (textures != expected or shader.inputs["Transmission Weight"].is_linked
                or shader.inputs["Transmission Weight"].default_value != 0
                or any(not shader.inputs[name].is_linked
                       for name in ("Base Color", "Alpha", "Normal", "Metallic", "Roughness"))):
            raise RuntimeError("studio_render_actor_pbr_graph")


def window_sum(data, width):
    half = width // 2
    values = np.pad(data, ((half + 1, half), (half + 1, half)), mode="constant")
    summed = values.cumsum(axis=0).cumsum(axis=1)
    return summed[width:, width:] - summed[:-width, width:] - summed[width:, :-width] + summed[:-width, :-width]


def dark_fragments(rgb, mask):
    luma = rgb @ np.array([.2126, .7152, .0722])
    core = window_sum(mask.astype(np.int32), 7) == 49
    interior = int(core.sum())
    if interior < 1024:
        raise ValueError("studio_render_shading_mask_too_small")
    local = window_sum(luma, 7) / 49
    dark = int((core & (luma < .72 * local) & (local - luma > .06)).sum())
    return {"dark_pixels": dark, "interior_pixels": interior, "ratio": dark / interior}


def lab(rgb):
    linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    xyz = linear @ np.array([[.4124564, .3575761, .1804375],
                             [.2126729, .7151522, .0721750],
                             [.0193339, .1191920, .9503041]]).T
    xyz /= np.array([.95047, 1., 1.08883])
    d = 6 / 29
    f = np.where(xyz > d**3, np.cbrt(xyz), xyz / (3 * d*d) + 4 / 29)
    return np.column_stack((116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])))


def rest_metrics(reference, candidate, reference_mask, candidate_mask):
    shared = reference_mask & candidate_mask
    union = reference_mask | candidate_mask
    if int(shared.sum()) < 1024:
        raise ValueError("studio_render_rest_masks_unregistered")
    de = np.linalg.norm(lab(reference[shared]) - lab(candidate[shared]), axis=1)
    return {"silhouette_iou": float(shared.sum() / union.sum()), "paired_pixels": int(shared.sum()),
            "delta_e76_mean": float(de.mean()), "delta_e76_p95": float(np.percentile(de, 95))}


def coverage(scene, meshes, rig, arms):
    import bpy
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    depsgraph = bpy.context.evaluated_depsgraph_get()
    verts, faces = [], {side: [] for side in arms}
    weights = {side: set(chain) for side, chain in arms.items()}
    for obj in meshes:
        mesh = obj.evaluated_get(depsgraph)
        groups = {group.index: group.name for group in obj.vertex_groups}
        totals = {s: [sum(g.weight for g in vertex.groups if groups[g.group] in names)
                      for vertex in mesh.data.vertices] for s, names in weights.items()}
        base = len(verts)
        verts.extend(mesh.matrix_world @ vertex.co for vertex in mesh.data.vertices)
        mesh.data.calc_loop_triangles()
        for p in mesh.data.loop_triangles:
            corners = p.vertices[:]
            for side, arm in faces.items():
                if sum(totals[side][v] for v in corners) >= 1.5:
                    arm.append(tuple(v + base for v in corners))

    trees = {s: BVHTree.FromPolygons(verts, polys, all_triangles=True) for s, polys in faces.items() if polys}
    cam = scene.camera
    origin, axes = cam.matrix_world.translation, cam.matrix_world.to_3x3()
    right, up = axes @ Vector((1, 0, 0)), axes @ Vector((0, 1, 0))
    result = {}
    for side, chain in arms.items():
        for name in chain:
            b = rig.pose.bones.get(name)
            if b is None:
                result[name] = {"uv": None, "anatomy_samples": 0, "visible_samples": 0}
                continue
            center = rig.matrix_world @ b.head
            u, v, depth = world_to_camera_view(scene, cam, center)
            anatomy, visible = 0, 0
            if side in trees and depth > 0 and 0 <= u <= 1 and 0 <= v <= 1:
                for x, y in OFFSETS:
                    ray = (center + .045 * (x * right + y * up)) - origin
                    direction = ray.normalized()
                    start = max(0, ray.length - .15)
                    hit, _, _, _ = trees[side].ray_cast(origin + start * direction, direction, .30)
                    if hit is None:
                        continue
                    anatomy += 1
                    depth_at_surface = (hit - origin).length
                    cast = scene.ray_cast(depsgraph, origin, direction, distance=depth_at_surface + .004)
                    if cast[0] and cast[4].original in meshes and (cast[1] - origin).length >= depth_at_surface - .004:
                        visible += 1
            result[name] = {"uv": [float(u), float(v)], "anatomy_samples": anatomy, "visible_samples": visible}
    return result



def screenshot(scene, destination, actor_only=None):
    """Write a beauty frame or an opaque silhouette using Cycles CPU."""
    import bpy

    if scene.render.engine != "CYCLES" or scene.cycles.device != "CPU":
        raise ValueError("studio_render_requires_cycles_cpu")
    render = scene.render
    layer = bpy.context.view_layer
    old = render.filepath, render.film_transparent, render.image_settings.color_mode, render.image_settings.file_format
    override = layer.material_override
    hidden = []
    mask_material = None
    try:
        if actor_only is not None:
            hidden = [obj for obj in scene.objects if obj.type in {"MESH", "FONT", "CURVE", "SURFACE", "META"}
                      and obj not in actor_only and not obj.hide_render]
            for obj in hidden:
                obj.hide_render = True
            mask_material = bpy.data.materials.new("Studio silhouette override")
            layer.material_override = mask_material
            render.film_transparent = True
        render.filepath = str(destination)
        render.image_settings.file_format = "PNG"
        render.image_settings.color_mode = "RGBA"
        bpy.ops.render.render(write_still=True, scene=scene.name)
    finally:
        for obj in hidden:
            obj.hide_render = False
        layer.material_override = override
        if mask_material is not None:
            bpy.data.materials.remove(mask_material)
        render.filepath, render.film_transparent, render.image_settings.color_mode, render.image_settings.file_format = old


def checked_image(ref, root):
    from PIL import Image

    path = (root / ref["path"]).resolve(strict=True)
    if sha(path) != ref["sha256"]:
        raise ValueError("studio_render_witness_changed:" + path.name)
    with Image.open(path, formats=["PNG"]) as image:
        if image.mode != "RGBA":
            raise ValueError("studio_render_requires_rgba_png")
        return np.array(image)


def paired_images(pair, root):
    color, mask = (checked_image(pair[key], root) for key in ("image", "mask"))
    if color.shape != mask.shape:
        raise ValueError("studio_render_witness_dimensions")
    return color[:, :, :3] / 255, mask[:, :, 3] > 127


def limb_rejections(limbs, arms, visibility_frames):
    reasons = []
    joints = {name for chain in arms.values() for name in chain}
    for frame, samples in limbs.items():
        if samples.keys() != joints:
            raise ValueError("studio_render_joint_roster:" + frame)
        for sample in samples.values():
            anatomy, visible = sample["anatomy_samples"], sample["visible_samples"]
            if type(anatomy) is not int or type(visible) is not int or not 0 <= visible <= anatomy <= len(OFFSETS):
                raise ValueError("studio_render_invalid_joint_samples:" + frame)
        if any(sample["anatomy_samples"] < MIN_COVERAGE for sample in samples.values()):
            reasons.append(f"studio_render_limb_presence:{frame}")
        endpoints = [name for chain in arms.values() for name in chain[1:]]
        if frame in visibility_frames and any(samples[name]["visible_samples"] < MIN_COVERAGE for name in endpoints):
            reasons.append(f"studio_render_limb_visibility:{frame}")
    return reasons


def joint_roster(arms):
    if (not isinstance(arms, dict) or arms.keys() != {"left", "right"}
            or any(not isinstance(chain, list) or len(chain) != 3
                   or any(not isinstance(name, str) or not name.strip() for name in chain) for chain in arms.values())
            or len({name for chain in arms.values() for name in chain}) != 6):
        raise ValueError("studio_render_arm_roster")
    return arms


def frame_roster(frames):
    if (not isinstance(frames, list) or not frames or any(type(frame) is not int or frame < 1 for frame in frames)
            or len(set(frames)) != len(frames)):
        raise ValueError("studio_render_frame_roster")
    return {str(frame) for frame in frames}


def evaluate_witnesses(config, root):
    """Measure registered rest/posed PNGs and check Blender limb samples."""
    arms = joint_roster(config["arms"])
    limbs = config["limbs"]
    frames = frame_roster(config["frames"])
    visible = frame_roster(config["visibility_frames"])
    shaded = frame_roster(config["shaded_frames"])
    if limbs.keys() != frames or config["posed"].keys() != shaded or not (visible | shaded) <= frames:
        raise ValueError("studio_render_timeline_incomplete")
    rest = config["rest"]
    reference, reference_mask = paired_images(rest["reference"], root)
    candidate, candidate_mask = paired_images(rest["candidate"], root)
    if reference.shape != candidate.shape:
        raise ValueError("studio_render_rest_dimensions")
    metrics = rest_metrics(reference, candidate, reference_mask, candidate_mask)
    reasons = []
    if metrics["silhouette_iou"] < MIN_IOU:
        reasons.append("studio_render_rest_silhouette")
    if metrics["delta_e76_mean"] > 17 or metrics["delta_e76_p95"] > 43:
        reasons.append("studio_render_rest_color")
    reasons.extend(limb_rejections(limbs, arms, visible))
    fragments = {}
    for frame, pair in sorted(config["posed"].items(), key=lambda item: int(item[0])):
        rgb, mask = paired_images(pair, root)
        if rgb.shape != reference.shape:
            raise ValueError("studio_render_witness_dimensions")
        fragments[frame] = dark_fragments(rgb, mask)
        if fragments[frame]["ratio"] > DARK_LIMIT:
            reasons.append(f"studio_render_shading_fragmentation:{frame}")
    return {"status": "REJECT" if reasons else "PASS", "rest": metrics, "fragmentation": fragments,
            "limbs": limbs, "reasons": reasons}
