# SPDX-License-Identifier: AGPL-3.0-only
"""Build the Studio SET template in Blender background mode.

Use build_scene() from an assembly script. Coordinates are in metres, Z is up,
the robot starts on camera left, and the button travels along world +Y.
Use key_interaction() to replace the default timing after choosing a contact frame.
The preview stand-in is created separately and is never saved in set.blend.
"""
from math import cos, sin, tau

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

from evidence import Rejected


WIDTH, HEIGHT = 480, 270
FPS, LAST_FRAME = 24, 288
MARKERS = {
    "APPROACH": 1,
    "PLANT_AND_JAB": 121,
    "HAND_CONTACT": 163,
    "BUTTON_BOTTOMED": 169,
    "BUTTON_RELEASED": 180,
    "RECOVERY_AND_SCREEN": 193,
}
PREVIEWS = ("APPROACH", "BUTTON_BOTTOMED", "RECOVERY_AND_SCREEN")
APPROACH_ROOT = (-1.66, -0.65, 0.0)
PLANT_ROOT = (-0.39, -0.44, 0.0)
ROBOT_HEIGHT = 1.2
CONTACT = (0.22, 0.10, 0.91)
TRAVEL = 0.038
SCREEN_CENTER = (0.935, 0.277, 1.157)
SCREEN_STRENGTH = 1.65


def srgb_channel(channel):
    value = int(channel, 16) / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def rgba(color):
    return tuple(srgb_channel(color[i:i + 2]) for i in (0, 2, 4)) + (1.0,)


def material(name, color, roughness, metallic=0.0):
    result = bpy.data.materials.new(name)
    result.use_nodes = True
    rgb = rgba(color)
    result.diffuse_color = rgb
    shader = result.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = rgb
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["Metallic"].default_value = metallic
    return result


def powdercoat():
    result = material("Floor | slate linoleum", "474d4a", 0.55)
    tree = result.node_tree
    shader = tree.nodes.get("Principled BSDF")
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 360
    noise.inputs["Detail"].default_value = 2
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = rgba("424845")
    ramp.color_ramp.elements[1].color = rgba("515750")
    tree.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
    return result


def surface(obj, name, mat, bevel=0):
    obj.name = name
    obj.data.materials.append(mat)
    if bevel:
        modifier = obj.modifiers.new("Edge radius", "BEVEL")
        modifier.width = bevel
        modifier.segments = 4
        if hasattr(obj.data, "use_auto_smooth"):
            obj.data.use_auto_smooth = True
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
        modifier = obj.modifiers.new("Weighted normals", "WEIGHTED_NORMAL")
        modifier.keep_sharp = True
    return obj


def box(name, center, size, mat, bevel=0.0):
    bpy.ops.mesh.primitive_cube_add(size=1, location=center)
    obj = bpy.context.object
    obj.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return surface(obj, name, mat, bevel)


def cylinder(name, center, axis, radius, length, mat, bevel=0.006):
    rotation = Vector(axis).to_track_quat("Z", "Y").to_euler()
    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=radius, depth=length,
                                       location=center, rotation=rotation)
    return surface(bpy.context.object, name, mat, bevel)


def button_collar(mat):
    segments = 64
    # A hollow sleeve leaves the moving cap visible throughout its stroke.
    sections = ((0.121, 0.110), (0.108, 0.110),
                (0.108, 0.197), (0.121, 0.197))
    vertices = [
        (CONTACT[0] + radius * cos(tau * i / segments), y,
         CONTACT[2] + radius * sin(tau * i / segments))
        for radius, y in sections for i in range(segments)
    ]
    faces = []
    for ring in range(len(sections)):
        here = ring * segments
        following = ((ring + 1) % len(sections)) * segments
        for i in range(segments):
            j = (i + 1) % segments
            faces.append((here + i, here + j, following + j, following + i))
    mesh = bpy.data.meshes.new("Button collar")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(mesh.name, mesh)
    bpy.context.collection.objects.link(obj)
    return surface(obj, obj.name, mat, 0.002)


def ellipsoid(name, center, radii, mat):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=16, location=center)
    obj = bpy.context.object
    obj.scale = radii
    for polygon in obj.data.polygons:
        polygon.use_smooth = True
    return surface(obj, name, mat)


def text_object(name, body, position, size, mat):
    curve = bpy.data.curves.new(name, "FONT")
    curve.body = body
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    curve.size = size
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.location = position
    obj.rotation_euler.x = 1.5707963267948966
    curve.materials.append(mat)
    return obj


def screen_material():
    result = material("CRT | phosphor glass", "11191a", 0.33)
    tree = result.node_tree
    shader = tree.nodes.get("Principled BSDF")
    texcoord = tree.nodes.new("ShaderNodeTexCoord")
    stripes = tree.nodes.new("ShaderNodeTexWave")
    stripes.wave_type = "BANDS"
    stripes.bands_direction = "Z"
    stripes.inputs["Scale"].default_value = 11
    stripes.inputs["Distortion"].default_value = 0
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = rgba("417e75")
    ramp.color_ramp.elements[1].color = rgba("8fe4cb")
    tree.links.new(texcoord.outputs["Generated"], stripes.inputs["Vector"])
    tree.links.new(stripes.outputs["Fac"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], shader.inputs["Emission Color"])
    return result


def make_television(mats):
    tv = bpy.data.objects.new("Television", None)
    bpy.context.collection.objects.link(tv)
    tv.empty_display_type = "CUBE"
    tv.empty_display_size = 0.1
    tv.location = (0.935, 0.67, 1.117)

    def attach(obj):
        # Scene objects were authored in world coordinates around the TV anchor.
        obj.location -= tv.location
        obj.parent = tv
        return obj

    attach(box("CRT cabinet", (0.935, 0.694, 1.12), (1.03, 0.66, 0.666),
               mats["paint"], 0.103))
    attach(box("CRT rear housing", (0.935, 1.005, 1.13), (0.78, 0.47, 0.51),
               mats["dark"], 0.12))
    attach(box("CRT front trim", (0.935, 0.365, 1.114), (0.969, 0.125, 0.597),
               mats["dark"], 0.064))
    attach(box("CRT glass gasket", (0.935, 0.317, 1.157), (0.827, 0.057, 0.502),
               mats["black"], 0.025))
    glass = attach(box("CRT Screen", (0.935, 0.311, 1.157), (0.766, 0.068, 0.444),
                       screen_material(), 0.030))
    for i in range(6):
        attach(box("Speaker slot", (0.621 + i * 0.033, 0.297, 0.881),
                   (0.020, 0.011, 0.016), mats["black"], 0.004))
    attach(text_object("CRT fascia lettering", "PHOSPHOR", (0.976, 0.296, 0.885),
                       0.034, mats["ink"]))
    attach(cylinder("CRT tuner", (1.286, 0.29, 0.885), (0, 1, 0), 0.024, 0.032,
                    mats["metal"]))
    attach(box("Button enclosure", (0.22, 0.288, 0.912), (0.30, 0.188, 0.252),
               mats["dark"], 0.039))
    attach(button_collar(mats["metal"]))
    button = attach(cylinder("PLAY Button", (0.22, 0.126, 0.91), (0, 1, 0),
                             0.104, 0.052, mats["amber"], 0.009))
    legend = text_object("PLAY lettering", "PLAY", (0, 0, 0), 0.058, mats["black"])
    # The cap's local +Z points inward. Parent with the cap's inverse so the
    # legend keeps its world orientation and travels with the cap.
    bpy.context.view_layer.update()
    legend.matrix_parent_inverse = button.matrix_world.inverted()
    legend.parent = button
    legend.location = (CONTACT[0], CONTACT[1] - 0.001, CONTACT[2])
    return tv, button, glass


def area_light(name, position, target, power, size, tint):
    data = bpy.data.lights.new(name, "AREA")
    data.energy = power
    data.shape = "DISK"
    data.size = size
    data.color = tint
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = position
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    return obj


def emission_input(glass):
    return glass.active_material.node_tree.nodes.get("Principled BSDF").inputs["Emission Strength"]


def key_interaction(button, glass, *, contact, bottomed, released, power_on, hold_end=None):
    """Replace solely the PLAY cap travel and CRT emission channels.

    Contact starts with the cap at rest; power-on may coincide with any point
    after contact once the cap has bottomed. Frames follow the scene's 1-based
    convention. Final assembly should align its event markers with these keys.
    """
    if not (1 < contact < bottomed < released <= LAST_FRAME
            and bottomed <= power_on <= LAST_FRAME):
        raise ValueError("interaction frame order")
    if hold_end is not None and not bottomed < hold_end < released:
        raise ValueError("interaction hold order")
    if "rest_y" not in button:
        button["rest_y"] = button.location.y
    base = button["rest_y"]
    stage = bpy.context.scene
    node_tree = glass.active_material.node_tree
    strength = emission_input(glass)
    strength_path = strength.path_from_id("default_value")
    for owner, path, index in ((button, "location", 1), (node_tree, strength_path, 0)):
        action = owner.animation_data.action if owner.animation_data else None
        existing = action.fcurves.find(path, index=index) if action else None
        if existing:
            action.fcurves.remove(existing)
    cap_keys = [(1, 0), (contact, 0), (bottomed, TRAVEL)]
    if hold_end is not None:
        cap_keys.append((hold_end, TRAVEL))
    for frame, displacement in (*cap_keys, (released, 0)):
        button.location.y = base + displacement
        button.keyframe_insert("location", index=1, frame=frame)
    for keyframe in button.animation_data.action.fcurves.find("location", index=1).keyframe_points:
        keyframe.interpolation = "LINEAR"
    for frame, value in ((1, 0), (power_on - 1, 0), (power_on, SCREEN_STRENGTH),
                         (LAST_FRAME, SCREEN_STRENGTH)):
        strength.default_value = value
        strength.keyframe_insert("default_value", frame=frame)
    for keyframe in node_tree.animation_data.action.fcurves.find(strength_path).keyframe_points:
        keyframe.interpolation = "CONSTANT"
    stage.frame_set(stage.frame_current)


def build_scene():
    """Create a clean, meter-scale stage. Returns the new active scene."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = "Studio SET"
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 48
    scene.cycles.use_denoising = False
    scene.cycles.seed = 24
    scene.render.resolution_x = WIDTH
    scene.render.resolution_y = HEIGHT
    scene.render.resolution_percentage = 100
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 4
    scene.render.fps = FPS
    scene.render.fps_base = 1.0
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.view_settings.view_transform = "AgX"
    scene.frame_start, scene.frame_end = 1, LAST_FRAME
    for name, frame in MARKERS.items():
        scene.timeline_markers.new(name, frame=frame)

    world = bpy.data.worlds.new("Room ambient")
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.35, 0.41, 0.46, 1.0)
    background.inputs["Strength"].default_value = 0.14
    scene.world = world

    wall = material("Room | petrol enamel", "314b4d", 0.75)
    side = material("Room | side wall", "4c6260", 0.72)
    trim = material("Room | charcoal wall trim", "263b3b", 0.58)
    mats = {
        "paint": material("Cabinet | warm enamel", "a57247", 0.36, 0.1),
        "dark": material("Cabinet | graphite polymer", "353c3a", 0.47),
        "black": material("Deep graphite", "171e1d", 0.59),
        "metal": material("Stand | bare oxidized steel", "859895", 0.32, 0.76),
        "amber": material("PLAY | safety amber", "efb755", 0.39, 0.08),
        "ink": material("CRT | ivory ink", "ded1b1", 0.56),
    }
    box("Floor", (0, 0, -0.072), (16, 14, 0.144), powdercoat(), 0.02)
    box("Back wall", (0, 2.31, 2.504), (15, 0.18, 5.0), wall)
    box("Left wall", (-3.42, -2.63, 2.5), (0.18, 10, 5), side)
    box("Back skirting", (0, 2.194, 0.079), (14, 0.044, 0.158), trim, 0.004)
    box("Left skirting", (-3.309, -2.596, 0.079), (0.043, 9.64, 0.158), trim, 0.004)
    box("Back rail", (0, 2.197, 1.73), (12, 0.042, 0.047), trim, 0.005)
    for x in (-2.92, -0.78, 1.36, 3.5):
        box("Wall seam", (x, 2.216, 0.93), (0.013, 0.016, 1.59), trim, 0.003)

    box("Stand deck", (0.819, 0.602, 0.746), (1.56, 0.87, 0.084), mats["dark"], 0.035)
    box("Stand lower shelf", (0.819, 0.611, 0.244), (1.44, 0.71, 0.042), mats["dark"], 0.027)
    for x in (0.19, 1.448):
        for y in (0.292, 0.923):
            box("Stand upright", (x, y, 0.365), (0.053, 0.055, 0.696),
                mats["metal"], 0.009)
            box("Stand foot", (x, y, 0.021), (0.077, 0.083, 0.042), mats["black"], 0.009)
    _, button, glass = make_television(mats)
    key_interaction(button, glass, contact=MARKERS["HAND_CONTACT"],
                    bottomed=MARKERS["BUTTON_BOTTOMED"], released=MARKERS["BUTTON_RELEASED"],
                    power_on=MARKERS["RECOVERY_AND_SCREEN"])

    area_light("Key | warm side", (-3.0, -1.15, 3.25), (-0.3, 0.05, 0.86),
               480, 1.85, (1.0, 0.84, 0.68))
    area_light("Fill | soft front", (2.85, -2.9, 2.02), (0.35, 0.3, 0.86),
               105, 2.65, (0.72, 0.83, 1.0))
    area_light("Rim | rear", (1.71, 1.7, 3.16), (-0.12, 0.12, 0.86),
               285, 1.35, (0.58, 0.82, 1.0))

    data = bpy.data.cameras.new("Full-body three-quarter")
    data.lens = 45
    data.clip_start = 0.06
    data.clip_end = 35
    camera = bpy.data.objects.new(data.name, data)
    bpy.context.collection.objects.link(camera)
    camera.location = (2.42, -5.60, 2.06)
    camera_target = Vector((-0.04, 0.26, 0.86))
    camera["target_m"] = tuple(camera_target)
    camera.rotation_euler = (camera_target - camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = camera
    scene.frame_set(1)
    return scene




def preview_name(marker):
    return f"preview_{MARKERS[marker]:03d}_{marker.lower()}.png"


def camera_uv(scene, coordinates):
    uv = world_to_camera_view(scene, scene.camera, Vector(coordinates))
    if uv.z <= 0:
        raise Rejected("camera_witness_behind_camera")
    return [round(uv.x, 6), round(uv.y, 6)]


def describe_scene(scene):
    camera = scene.camera
    button = scene.objects["PLAY Button"]
    lights = {}
    for obj in scene.objects:
        if obj.type == "LIGHT":
            lights[obj.name] = {
                "type": obj.data.type,
                "location_m": list(obj.location),
                "rotation_euler_rad": list(obj.rotation_euler),
                "power_w": obj.data.energy,
                "diameter_m": obj.data.size,
                "color_linear_rgb": list(obj.data.color),
            }
    witnesses = {
        "button_rest_contact": CONTACT,
        "button_depressed_contact": (CONTACT[0], CONTACT[1] + TRAVEL, CONTACT[2]),
        "screen_center": SCREEN_CENTER,
    }
    for dx in (-0.34, 0.34):
        for dz in (-0.18, 0.18):
            witnesses[f"screen_{dx:+.2f}_{dz:+.2f}"] = (
                SCREEN_CENTER[0] + dx, SCREEN_CENTER[1], SCREEN_CENTER[2] + dz,
            )
    for name, root in (("approach", APPROACH_ROOT), ("plant", PLANT_ROOT)):
        for side in (-0.24, 0.24):
            for height in (0, ROBOT_HEIGHT):
                for depth in (-0.20, 0.20):
                    key = f"{name}_envelope_{side:+.2f}_{height:.1f}_{depth:+.2f}"
                    witnesses[key] = (root[0] + side, root[1] + depth, height)
    return {
        "unit": scene.name,
        "units": "metres, Z up",
        "fps": scene.render.fps,
        "start_frame": scene.frame_start,
        "end_frame": scene.frame_end,
        "duration_seconds": scene.frame_end / scene.render.fps,
        "frame_to_seconds": "(frame - 1) / fps",
        "markers": {marker.name: marker.frame for marker in scene.timeline_markers},
        "phases_seconds": {"approach": [0, 5], "plant_and_jab": [5, 8],
                           "recovery_and_screen": [8, 12]},
        "camera": {"name": camera.name, "lens_mm": camera.data.lens,
                   "location_m": list(camera.location), "target_m": list(camera["target_m"]),
                   "rotation_euler_rad": list(camera.rotation_euler),
                   "sensor_width_mm": camera.data.sensor_width,
                   "camera_uv_origin": "bottom left",
                   "witness_uv": {name: camera_uv(scene, pos) for name, pos in witnesses.items()}},
        "lights": lights,
        "button": {"object": button.name, "legend_object": "PLAY lettering",
                   "rest_contact_world_m": CONTACT, "depressed_contact_world_m":
                       (CONTACT[0], CONTACT[1] + TRAVEL, CONTACT[2]),
                   "axis_world": [0, 1, 0], "travel_m": TRAVEL,
                   "travel_key_frames": [1, MARKERS["HAND_CONTACT"], MARKERS["BUTTON_BOTTOMED"],
                                         MARKERS["BUTTON_RELEASED"]]},
        "screen": {"object": "CRT Screen", "center_world_m": SCREEN_CENTER, "power_on_frame": MARKERS["RECOVERY_AND_SCREEN"],
                   "off_emission_strength": 0, "on_emission_strength": SCREEN_STRENGTH},
        "robot_framing": {"reference_height_m": ROBOT_HEIGHT,
                          "approach_root_world_m": APPROACH_ROOT, "plant_root_world_m": PLANT_ROOT,
                          "standin": "preview only"},
        "preview": {"engine": "Cycles CPU", "samples": scene.cycles.samples,
                    "resolution_px": [WIDTH, HEIGHT],
                    "frames": {marker: preview_name(marker) for marker in PREVIEWS}},
    }
