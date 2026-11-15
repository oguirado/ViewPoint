"""Blender-only side script: bpy and standard library, no host package imports."""
import base64
from decimal import Decimal, ROUND_HALF_EVEN
import json
import math
import os
import sys


def quantise(value, scale):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite_state")
    return int((number * scale).to_integral_value(rounding=ROUND_HALF_EVEN))


def scene_state(bpy):
    return {"objects": {obj.name: {"type": obj.type,
            "location_um": [quantise(v, 1000000) for v in obj.location],
            "rotation_mdeg": [quantise(Decimal(str(v)) * 180 / Decimal(str(math.pi)), 1000) for v in obj.rotation_euler],
            "scale_ppm": [quantise(v, 1000000) for v in obj.scale],
            "hide_render": int(obj.hide_render)} for obj in sorted(bpy.data.objects, key=lambda o: o.name)}}


def apply(bpy, action):
    if type(action) is not dict or action.get("op") not in ("set_location", "set_rotation", "set_scale", "hide"):
        raise ValueError("unsupported_action")
    obj = bpy.data.objects.get(action.get("object", ""))
    if obj is None:
        raise ValueError("object_not_found")
    field = {"set_location": "value_um", "set_rotation": "value_mdeg",
             "set_scale": "value_ppm", "hide": "value"}[action["op"]]
    if set(action) != {"op", "object", field}:
        raise ValueError("invalid_blender_action_fields")
    value = action.get(field)
    if action["op"] == "hide":
        if type(value) is not int or value not in (0, 1):
            raise ValueError("hide_requires_zero_one")
        obj.hide_render = bool(value)
    else:
        if type(value) is not list or len(value) != 3 or not all(type(v) is int for v in value):
            raise ValueError("transform_requires_integer_vector")
        value = ([math.radians(v / 1000) for v in value] if action["op"] == "set_rotation"
                 else [v / 1000000 for v in value])
        attr = {"set_location": "location", "set_rotation": "rotation_euler", "set_scale": "scale"}[action["op"]]
        setattr(obj, attr, value)
    bpy.context.view_layer.update()


def ensure_camera(bpy, scene):
    """Preserve a source scene's camera; configure only our fallback."""
    if scene.camera is None:
        camera = bpy.data.objects.new("VQA3D_Camera", bpy.data.cameras.new("VQA3D_Camera"))
        scene.collection.objects.link(camera)
        scene.camera = camera
        camera.location = (5, -7, 5)
        camera.rotation_euler = (math.radians(60), 0, math.radians(35))
        camera.data.type = "ORTHO"
        camera.data.ortho_scale = 8


def main():
    import bpy
    request_path, response_path = sys.argv[sys.argv.index("--") + 1:]
    with open(request_path, encoding="utf-8") as stream:
        request = json.load(stream)
    scene = bpy.context.scene
    mode = request["mode"]
    if mode == "init":
        ensure_camera(bpy, scene)
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = request["width"]
    scene.render.resolution_y = request["height"]
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.use_file_extension = False
    scene.render.film_transparent = False
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "OBJECT"
    scene.display.shading.show_shadows = True
    scene.display.shading.show_cavity = True
    if mode == "act":
        apply(bpy, request["action"])
    if mode in ("init", "act"):
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
    result = {"ok": True, "state": scene_state(bpy), "runtime_version": bpy.app.version_string}
    if mode == "capture":
        bpy.ops.render.render()
        # save_render writes into an anonymous RAM file: no PNG disk artifact.
        fd = os.memfd_create("vqa3d-frame.png")
        try:
            bpy.data.images["Render Result"].save_render("/proc/self/fd/" + str(fd), scene=scene)
            os.lseek(fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(fd), "rb") as stream:
                result["frame_png_base64"] = base64.b64encode(stream.read()).decode("ascii")
        finally:
            os.close(fd)
    with open(response_path, "w", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    main()
