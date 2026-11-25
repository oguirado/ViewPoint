"""Build or rasterize an editable text plate in headless Blender."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time

import bpy
import cairo
from mathutils import Vector


SLOT_SCHEMA = "af.text/v1"
LOCALE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")
JOB_SCHEMA = "text-slot/job-v1"
RECEIPT_SCHEMA = "text-slot/receipt-v1"
DIGEST = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
EPSILON = 1e-6


class SlotError(ValueError):
    pass


def require(condition, code, detail):
    if not condition:
        raise SlotError(f"{code}: {detail}")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def vector(value, size):
    return (isinstance(value, list) and len(value) == size
            and all(number(item) and 0 <= item <= 1 for item in value))


def rectangle(value):
    return vector(value, 4) and value[0] < value[2] and value[1] < value[3]


def contains(outer, inner, tolerance=0):
    left, top, right, bottom = inner
    return (outer[0] - tolerance <= left and outer[1] - tolerance <= top
            and right <= outer[2] + tolerance and bottom <= outer[3] + tolerance)


def unique_ids(rows, label):
    ids = [row.get("id") for row in rows]
    require(all(isinstance(item, str) and IDENTIFIER.fullmatch(item) for item in ids)
            and len(set(ids)) == len(ids), "invalid_ids", label)


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key", key)
        result[key] = value
    return result


def reject_constant(value):
    raise SlotError(f"invalid_json_number: {value}")


def valid_digest(value):
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def resolve_font(path, font):
    require(isinstance(font, dict) and valid_digest(font.get("sha256")),
            "missing_font_digest", path)
    require(isinstance(font.get("path"), str) and font["path"].strip(),
            "invalid_font_path", path)
    font_path = (path.parent / font["path"]).resolve()
    require(font_path.is_file(), "font_missing", font_path)
    require(sha256(font_path.read_bytes()) == font["sha256"], "font_digest_mismatch", font_path)
    return font_path, font["sha256"]


def load_job(path):
    raw = path.read_bytes()
    payload = json.loads(raw, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)
    require(isinstance(payload, dict), "invalid_schema", path)
    context = None
    job = payload
    if payload.get("schema") == SLOT_SCHEMA:
        require(set(payload) == {"schema", "context", "request"}, "invalid_envelope", path)
        context = payload["context"]
        require(isinstance(context, dict)
                and set(context) == {"policy_sha256", "toolchain_sha256", "timeout_s"}
                and all(valid_digest(context[key]) for key in ("policy_sha256", "toolchain_sha256"))
                and type(context["timeout_s"]) is int and context["timeout_s"] > 0,
                "invalid_context", path)
        job = payload["request"]
    require(isinstance(job, dict) and job.get("schema") == JOB_SCHEMA, "invalid_schema", path)
    if context is None:
        default_font = "legacy"
        fonts = {default_font: resolve_font(path, job.get("font"))}
    else:
        locale = job.get("locale")
        require(isinstance(locale, str) and LOCALE.fullmatch(locale),
                "invalid_locale", path)
        font_map = job.get("fonts")
        default_font = job.get("default_font")
        require(isinstance(font_map, dict) and font_map
                and all(IDENTIFIER.fullmatch(alias) for alias in font_map)
                and isinstance(default_font, str) and default_font in font_map
                and "font" not in job, "invalid_font_map", path)
        fonts = {alias: resolve_font(path, ref) for alias, ref in font_map.items()}

    fps, start, end = (job.get(key) for key in ("fps", "frame_start", "frame_end"))
    require(all(type(value) is int for value in (fps, start, end))
            and 1 <= fps <= 32767 and 0 <= start <= end <= 1048574,
            "invalid_timeline", path)
    safe = job.get("safe_area")
    require(rectangle(safe), "invalid_safe_area", path)

    profiles = job.get("profiles")
    require(isinstance(profiles, list) and profiles and all(isinstance(p, dict) for p in profiles),
            "invalid_profiles", path)
    unique_ids(profiles, "profiles")
    for profile in profiles:
        require(all(type(profile.get(key)) is int and 4 <= profile[key] <= 65536
                    for key in ("width", "height")), "invalid_dimensions", profile["id"])

    cues = job.get("cues")
    require(isinstance(cues, list) and cues and all(isinstance(cue, dict) for cue in cues),
            "invalid_cues", path)
    unique_ids(cues, "cues")
    for cue in cues:
        label = cue["id"]
        if context is not None:
            alias = cue.get("font", default_font)
            require(isinstance(alias, str) and alias in fonts, "unknown_cue_font", label)
        require(cue.get("kind") in ("title", "caption", "lower_third"), "invalid_kind", label)
        text = cue.get("text")
        require(isinstance(text, str) and text.strip()
                and all(character.isprintable() or character == "\n" for character in text),
                "invalid_text", label)
        require(rectangle(cue.get("box")) and contains(safe, cue["box"]), "unsafe_box", label)
        require(cue.get("align") in ("left", "center", "right")
                and cue.get("valign") in ("top", "middle", "bottom"), "invalid_alignment", label)
        require(number(cue.get("size")) and 0 < cue["size"] <= 1
                and number(cue.get("padding", 0)) and 0 <= cue.get("padding", 0) < 1,
                "invalid_size", label)
        require(vector(cue.get("color"), 4) and cue["color"][3] > 0
                and ("background" not in cue or vector(cue["background"], 4)),
                "invalid_color", label)
        first, last = cue.get("start"), cue.get("end")
        require(type(first) is int and type(last) is int and start <= first <= last <= end,
                "invalid_cue_timing", label)
        fades = (cue.get("fade_in", 0), cue.get("fade_out", 0))
        require(all(type(fade) is int and fade >= 0 for fade in fades)
                and sum(fades) <= last - first, "invalid_fade", label)
    return job, sha256(raw), context, fonts, default_font


def blender_rgb(rgb):
    return tuple(channel / 12.92 if channel < 0.04045 else ((channel + .055) / 1.055) ** 2.4
                 for channel in rgb)


def set_visibility(obj, cue):
    for frame, hidden in ((cue["start"] - 1, True), (cue["start"], False),
                          (cue["end"], False), (cue["end"] + 1, True)):
        obj.hide_render = hidden
        obj.keyframe_insert(data_path="hide_render", frame=frame)


def style(obj, color, cue, visible_endpoints):
    material = bpy.data.materials.new(obj.name)
    material.diffuse_color = (*blender_rgb(color[:3]), color[3])
    material.blend_method = "BLEND"
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = "Fill Color"
    emission.inputs["Color"].default_value = (*material.diffuse_color[:3], 1.0)
    emission.location = (-420, 80)
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    transparent.location = (-420, 230)
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.name = "Opacity"
    mix.location = (-160, 120)
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (50, 120)
    tree.links.new(transparent.outputs[0], mix.inputs[1])
    tree.links.new(emission.outputs[0], mix.inputs[2])
    tree.links.new(mix.outputs[0], output.inputs[0])
    first, last = cue["start"], cue["end"]
    fade_in, fade_out = cue.get("fade_in", 0), cue.get("fade_out", 0)
    keyframes = {first - 1: 0, first + fade_in: color[3],
                 last - fade_out: color[3], last + 1: 0}
    if not visible_endpoints:
        if fade_in:
            keyframes[first] = 0
        if fade_out:
            keyframes[last] = 0
    for frame, opacity in sorted(keyframes.items()):
        mix.inputs[0].default_value = opacity
        mix.inputs[0].keyframe_insert(data_path="default_value", frame=frame)
    for curve in tree.animation_data.action.fcurves:
        for key in curve.keyframe_points:
            key.interpolation = "LINEAR"
    obj.data.materials.append(material)
    obj["text_slot_cue"] = cue["id"]
    set_visibility(obj, cue)


def bounds(obj):
    bpy.context.view_layer.update()
    corners = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).bound_box
    return (min(point[0] for point in corners), min(point[1] for point in corners),
            max(point[0] for point in corners), max(point[1] for point in corners))


def wrap_text(obj, text, width, label):
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for match in re.finditer(r"([^\S\n]*)(\S+)", paragraph):
            spaces, word = match.groups()
            trial = f"{line}{spaces}{word}" if line else word
            obj.data.body = trial
            left, _, right, _ = bounds(obj)
            if right - left <= width + EPSILON:
                line = trial
                continue
            require(line, "layout_overflow", f"{label}: word exceeds box")
            lines.append(line)
            obj.data.body = word
            left, _, right, _ = bounds(obj)
            require(right - left <= width + EPSILON, "layout_overflow", f"{label}: word exceeds box")
            line = word
        lines.append(line)
    return "\n".join(lines)


def marker(scene, name, frame):
    scene.timeline_markers.new(name, frame=frame)


def create_cue(scene, cue, font, profile, z, layer_spacing, font_ref):
    width, height = profile["width"], profile["height"]
    aspect = width / height
    lft, top, rgt, bot = cue["box"]
    left, right = lft * aspect, rgt * aspect
    bottom, upper = 1 - bot, 1 - top
    pad = cue.get("padding", 0)
    inset = pad + 1 / height
    box_width, box_height = right - left - 2 * inset, upper - bottom - 2 * inset
    label = f"{profile['id']}/{cue['id']}"
    require(box_width > 0 and box_height > 0, "layout_overflow", f"{label}: padding consumes box")

    label_name = cue["id"] + ".text"
    curve = bpy.data.curves.new(label_name, "FONT")
    curve.font = font
    curve.font_bold = curve.font_italic = curve.font_bold_italic = font
    curve.align_x = cue["align"].upper()
    curve.size = cue["size"]
    curve.resolution_u = 16
    obj = bpy.data.objects.new(label_name, curve)
    scene.collection.objects.link(obj)
    curve.body = wrap_text(obj, cue["text"], box_width, label)
    x0, y0, x1, y1 = bounds(obj)
    glyph_width, glyph_height = x1 - x0, y1 - y0
    require(glyph_width > 0 and glyph_height > 0, "empty_glyph_mesh", label)
    require(glyph_width <= box_width + EPSILON and glyph_height <= box_height + EPSILON,
            "layout_overflow", f"{label}: {glyph_width:.5f}×{glyph_height:.5f} exceeds "
            f"{box_width:.5f}×{box_height:.5f} after wrapping")
    x = {"left": left + inset, "center": (left + right - glyph_width) / 2,
         "right": right - inset - glyph_width}[cue["align"]]
    y = {"bottom": bottom + inset, "middle": (bottom + upper - glyph_height) / 2,
         "top": upper - inset - glyph_height}[cue["valign"]]
    obj.location = (x - x0, y - y0, z)
    obj["text_slot_kind"] = cue["kind"]
    obj["text_slot_original_text"] = cue["text"]
    if font_ref is not None:
        alias, digest = font_ref
        obj["text_slot_font_alias"] = alias
        obj["text_slot_font_sha256"] = digest
        obj["text_slot_built_body"] = curve.body
        obj["text_slot_built_size"] = curve.size
    style(obj, cue["color"], cue, visible_endpoints=font_ref is not None)
    if cue.get("background", [0, 0, 0, 0])[3]:
        bg = bpy.data.meshes.new(cue["id"] + ".background")
        a, b = x0 - pad, y0 - pad
        c, d = x1 + pad, y1 + pad
        bg.from_pydata([(a, b, 0), (c, b, 0), (c, d, 0), (a, d, 0)], [], [(0, 1, 2, 3)])
        backing = bpy.data.objects.new(bg.name, bg)
        scene.collection.objects.link(backing)
        backing.parent = obj
        backing.location.z = -layer_spacing / 2
        style(backing, cue["background"], cue, visible_endpoints=font_ref is not None)
    marker(scene, cue["id"] + " in", cue["start"])
    marker(scene, cue["id"] + " out (inclusive)", cue["end"])


def create_scene(job, job_digest, fonts, default_font, profile, is_factory, deadline):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0
    scene = bpy.context.scene
    scene.name = "Text plate " + profile["id"]
    scene.frame_start, scene.frame_end = job["frame_start"], job["frame_end"]
    scene.render.fps, scene.render.fps_base = job["fps"], 1.0
    scene.render.resolution_x = profile["width"]
    scene.render.resolution_y = profile["height"]
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "Standard"
    aspect = profile["width"] / profile["height"]
    camera = bpy.data.cameras.new("Plate Camera")
    camera.type = "ORTHO"
    camera.ortho_scale = max(1.0, aspect)
    scene.camera = bpy.data.objects.new(camera.name, camera)
    scene.collection.objects.link(scene.camera)
    scene.camera.location = (aspect / 2, .5, 10)
    scene["text_slot_schema"] = JOB_SCHEMA
    scene["text_slot_profile"] = profile["id"]
    scene["text_slot_job_sha256"] = job_digest
    scene["text_slot_font_sha256"] = fonts[default_font][1]
    scene["text_slot_safe_area"] = job["safe_area"]
    if is_factory:
        scene["text_slot_contract"] = SLOT_SCHEMA
        scene["text_slot_locale"] = job["locale"]
        scene["text_slot_default_font"] = default_font
        scene["text_slot_fonts"] = {alias: ref[1] for alias, ref in fonts.items()}

    packed_fonts = {}
    layer_spacing = 1 / len(job["cues"])
    for index, cue in enumerate(job["cues"]):
        check_deadline(deadline)
        alias = cue.get("font", default_font) if is_factory else default_font
        font_path, font_digest = fonts[alias]
        if font_path not in packed_fonts:
            font = bpy.data.fonts.load(str(font_path))
            font.pack()
            require(packed_digest(font) == font_digest, "font_pack_mismatch", font_path)
            font.filepath = "//" + font_path.name
            packed_fonts[font_path] = font
        create_cue(scene, cue, packed_fonts[font_path], profile, index * layer_spacing,
                   layer_spacing, (alias, font_digest) if is_factory else None)
    scene.frame_set(min(cue["start"] if is_factory
                        else cue["start"] + bool(cue.get("fade_in")) for cue in job["cues"]))
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.spaces.active.region_3d.view_perspective = "CAMERA"
    return scene


def packed_digest(font):
    return sha256(font.packed_file.data) if font and font.packed_file else None


def inspect_source(scene):
    require(scene.get("text_slot_schema") == JOB_SCHEMA and scene.camera is not None,
            "invalid_source", scene.name)
    safe = list(scene.get("text_slot_safe_area", []))
    font_digest = scene.get("text_slot_font_sha256")
    job_digest = scene.get("text_slot_job_sha256")
    require(rectangle(safe) and isinstance(font_digest, str) and DIGEST.fullmatch(font_digest)
            and isinstance(job_digest, str) and DIGEST.fullmatch(job_digest),
            "invalid_source_pins", scene.name)
    require(scene.render.film_transparent, "nontransparent_source", scene.name)
    factory = scene.get("text_slot_contract") == SLOT_SCHEMA
    if factory:
        fonts = dict(scene.get("text_slot_fonts", {}))
        default_font = scene.get("text_slot_default_font")
        locale = scene.get("text_slot_locale")
        require(fonts and all(IDENTIFIER.fullmatch(alias) and valid_digest(digest)
                              for alias, digest in fonts.items())
                and isinstance(default_font, str) and fonts.get(default_font) == font_digest
                and isinstance(locale, str) and LOCALE.fullmatch(locale),
                "invalid_source_pins", scene.name)
    objects = []
    for obj in scene.objects:
        if obj.type != "FONT" or "text_slot_cue" not in obj:
            continue
        curve = obj.data
        digest = font_digest
        editable = {"id": obj["text_slot_cue"], "kind": obj["text_slot_kind"],
                    "text": curve.body, "size": curve.size}
        if factory:
            alias = obj.get("text_slot_font_alias")
            digest = obj.get("text_slot_font_sha256")
            require(isinstance(alias, str) and valid_digest(digest) and fonts.get(alias) == digest,
                    "invalid_cue_font_pin", obj.name)
            editable.update(font=alias, font_sha256=digest,
                            source_modified=(curve.body != obj.get("text_slot_built_body")
                                             or curve.size != obj.get("text_slot_built_size")))
        require(all(packed_digest(font) == digest
                    for font in (curve.font, curve.font_bold, curve.font_italic, curve.font_bold_italic)),
                "packed_font_mismatch", obj.name)
        objects.append(editable)
    require(objects, "no_editable_text", scene.name)
    receipt = {"schema": RECEIPT_SCHEMA, "profile": scene["text_slot_profile"],
               "job_sha256": job_digest, "font_sha256": font_digest, "safe_area": safe,
               "fps": scene.render.fps / scene.render.fps_base, "editable_text": objects,
               "backend": "cairo-cpu/blender-evaluated-mesh"}
    if factory:
        receipt.update(locale=locale, default_font=default_font, fonts=fonts,
                       source_modified=any(obj["source_modified"] for obj in objects))
    return receipt


def srgb(channel):
    return 12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055


def render_color(obj):
    nodes = obj.active_material.node_tree.nodes
    alpha = nodes["Opacity"].inputs[0].default_value
    return (*map(srgb, nodes["Fill Color"].inputs["Color"].default_value[:3]), alpha)


def project_mesh(obj, depsgraph, projection, width, height):
    evaluated = obj.evaluated_get(depsgraph)
    transform = projection @ evaluated.matrix_world
    mesh = evaluated.to_mesh()
    try:
        points = []
        for vertex in mesh.vertices:
            clip = transform @ Vector((*vertex.co, 1))
            points.append(((1 + clip.x / clip.w) * width / 2, (1 - clip.y / clip.w) * height / 2))
        polygons = [tuple(poly.vertices) for poly in mesh.polygons]
    finally:
        evaluated.to_mesh_clear()
    require(points and polygons, "empty_visible_mesh", obj.name)
    return points, polygons


def draw_layer(ctx, points, polygons):
    for polygon in polygons:
        path = [points[index] for index in polygon]
        area = sum(x * ny - nx * y for (x, y), (nx, ny) in zip(path, path[1:] + path[:1]))
        if abs(area) < EPSILON:
            continue
        if area < 0:
            path.reverse()
        ctx.move_to(*path[0])
        for point in path[1:]:
            ctx.line_to(*point)
        ctx.close_path()
    ctx.fill()


def render_plate(scene, frame, filepath, safe):
    scene.frame_set(frame)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    camera = scene.camera.evaluated_get(depsgraph)
    width, height = (scene.render.resolution_x, scene.render.resolution_y)
    safe_pixels = (math.ceil(safe[0] * width), math.ceil(safe[1] * height),
                   math.floor(safe[2] * width), math.floor(safe[3] * height))
    require(scene.render.resolution_percentage == 100, "unsupported_resolution_scale", scene.name)
    projection = camera.calc_matrix_camera(depsgraph, x=width, y=height) @ camera.matrix_world.inverted()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    try:
        layers = [obj for obj in scene.objects if "text_slot_cue" in obj and not obj.hide_render]
        # Rebuild ordering after frame evaluation so editable motion is reflected in plates.
        layers.sort(key=lambda obj: (obj.matrix_world.translation.z, obj.name))
        for obj in layers:
            color = render_color(obj)
            if color[3] <= 0:
                continue
            points, polygons = project_mesh(obj, depsgraph, projection, width, height)
            xs, ys = zip(*points)
            box = (min(xs), min(ys), max(xs), max(ys))
            require(contains(safe_pixels, box, EPSILON), "unsafe_render_geometry",
                    f"{obj.name}: frame {frame}: {box}")
            ctx.set_source_rgba(*color)
            draw_layer(ctx, points, polygons)
        surface.write_to_png(str(filepath))
    finally:
        surface.finish()
    return {"frame": frame, "path": str(filepath.relative_to(filepath.parent.parent)),
            "sha256": sha256(filepath.read_bytes())}


def check_deadline(deadline):
    require(deadline is None or time.monotonic() < deadline,
            "timeout", "text build exceeded its context budget")


def write_plates(scene, out, frames, receipt, deadline=None):
    frames = frames or range(scene.frame_start, scene.frame_end + 1)
    require(len(set(frames)) == len(frames)
            and all(scene.frame_start <= frame <= scene.frame_end for frame in frames),
            "invalid_render_frames", scene.name)
    (out / "plates").mkdir()
    receipt["dimensions"] = [scene.render.resolution_x, scene.render.resolution_y]
    receipt["frames"] = []
    for frame in sorted(frames):
        check_deadline(deadline)
        receipt["frames"].append(render_plate(
            scene, frame, out / "plates" / f"{frame:06d}.png", receipt["safe_area"]))
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@contextmanager
def new_output(path):
    require(not path.exists() and not path.is_symlink(), "output_exists", path)
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".text-slot-") as workdir:
        stage = Path(workdir) / "result"
        stage.mkdir()
        yield stage
        stage.rename(path)


def build(args):
    started = time.monotonic()
    path = args.job.resolve(strict=True)
    job, digest, context, fonts, default_font = load_job(path)
    deadline = started + context["timeout_s"] if context is not None else None
    results = []
    with new_output(args.output) as stage:
        for profile in job["profiles"]:
            check_deadline(deadline)
            scene = create_scene(job, digest, fonts, default_font, profile, context is not None,
                                 deadline)
            out = stage / profile["id"]
            out.mkdir()
            source = out / "source.blend"
            require(bpy.ops.wm.save_as_mainfile(filepath=str(source), relative_remap=False)
                    == {"FINISHED"}, "source_save_failed", source)
            receipt = inspect_source(scene)
            receipt["source_sha256"] = sha256(source.read_bytes())
            write_plates(scene, out, args.frame, receipt, deadline)
            if context is not None:
                native_receipt = out / "receipt.json"
                results.append({"id": profile["id"],
                                "receipt": native_receipt.relative_to(stage).as_posix(),
                                "receipt_sha256": sha256(native_receipt.read_bytes())})
        if context is not None:
            write_json(stage / "receipt.json", {
                "schema": SLOT_SCHEMA, "context": context, "request_sha256": digest,
                "result": {"profiles": results},
            })
            check_deadline(deadline)


def render(args):
    source = args.source.resolve(strict=True)
    require(bpy.ops.wm.open_mainfile(filepath=str(source), load_ui=False, use_scripts=False)
            == {"FINISHED"}, "source_reopen_failed", source)
    scene = bpy.context.scene
    receipt = inspect_source(scene)
    receipt["source_sha256"] = sha256(source.read_bytes())
    with new_output(args.output) as stage:
        write_plates(scene, stage, args.frame, receipt)


def cli(arguments):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, entry, input_name in (("build", build, "job"), ("render", render, "source")):
        command = commands.add_parser(name)
        command.add_argument("--" + input_name, type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--frame", type=int, action="append", help="Repeat to select frames")
        command.set_defaults(entry=entry)
    args = parser.parse_args(arguments)
    args.output = args.output.absolute()
    try:
        args.entry(args)
    except (SlotError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"TEXT_SLOT_REJECT {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
