# SPDX-License-Identifier: AGPL-3.0-only
"""Build the editable stepped and splined clay scenes for studio."""
import bisect
import math

import bpy
from mathutils import Quaternion, Vector

from layout import build_clay_set
from layout_geometry import CAP_REST as CAP_REST_M, CAP_AXIS as CAP_AXIS_WORLD, CAP_TRAVEL, HAND_CONTACT
from studio_set import SCREEN_CENTER

FRAME_END = 288
FPS = 24
START = Vector((-1.32, -.20, 0))
FORWARD = Vector((.978, .209, 0)).normalized()
RIGHT = Vector((FORWARD.y, -FORWARD.x, 0))
CAP_REST = Vector(CAP_REST_M)
CAP_AXIS = Vector(CAP_AXIS_WORLD)
CAP_SIDE = Vector((CAP_AXIS.y, -CAP_AXIS.x, 0))
PAD_TARGET = Vector(HAND_CONTACT)
SCREEN = Vector(SCREEN_CENTER)
FLOOR = 0.
SOLE = (.151, .159)
SOLE_SHIFT = .020
THIGH, SHIN = .165, .157
UPPER, FOREARM = .219, .182
PAD_REACH = .069
TRAVEL = CAP_TRAVEL
REST_HEIGHT_M = 1.2

# Forward displacement, lateral weight shift, hip height. One-based frames.
ROOT_KEYS = [
    (1, .035, -.036, .304), (11, .060, .034, .312),
    (22, .137, .012, .301), (26, .174, -.038, .283),
    (32, .230, -.059, .313), (42, .327, -.009, .301),
    (46, .365, .041, .283), (52, .426, .066, .315),
    (62, .520, .010, .301), (66, .555, -.040, .281),
    (72, .616, -.063, .316), (82, .710, -.010, .300),
    (86, .745, .041, .282), (92, .806, .066, .314),
    (102, .897, .010, .300), (106, .937, -.042, .281),
    (112, .992, -.060, .313), (122, 1.083, -.010, .300),
    (126, 1.122, .037, .282), (132, 1.168, .054, .308),
    (141, 1.215, .021, .297), (149, 1.231, .001, .269),
    (158, 1.233, -.002, .300), (166, 1.233, -.002, .298),
    (177, 1.206, .014, .285), (183, 1.201, .015, .287),
    (191, 1.253, -.004, .303), (195, 1.268, -.010, .304),
    (201, 1.263, -.007, .303), (205, 1.250, .001, .300),
    (211, 1.217, .017, .286), (220, 1.222, .009, .306),
    (234, 1.233, -.003, .300), (256, 1.233, -.003, .297),
    (288, 1.233, -.003, .297),
]
# Each tuple is a toe-off frame, a landing frame and a fixed world-space endpoint.
STEPS = {
    "L": (.079, ((4, 22, .268), (44, 62, .627), (84, 102, .994), (124, 141, 1.349))),
    "R": (-.048, ((24, 42, .449), (64, 82, .811), (104, 122, 1.129))),
}
# Torso yaw, forward lean, side lean, and head look blend.
ACTING = [
    (1, .19, .035, .024, 0), (22, .30, .056, .01, 0),
    (42, .12, .060, -.015, 0), (62, .31, .060, .01, 0),
    (82, .13, .052, -.010, 0), (102, .29, .057, .01, 0),
    (122, .12, .052, -.01, 0), (141, .24, .056, .008, 0),
    (149, .27, .149, -.021, 0), (158, .35, .018, 0, .11),
    (166, .46, .007, -.015, .15), (177, .62, -.127, -.027, .12),
    (183, .65, -.134, -.038, .13), (191, .68, .12, -.015, .13),
    (195, .73, .162, -.022, .13), (201, .73, .152, -.02, .15),
    (205, .70, .100, -.02, .15), (212, .46, -.181, .041, .24),
    (220, .41, -.078, .030, .61), (237, .50, .035, -.01, 1.0),
    (246, .49, .045, -.01, 1.0), (266, .52, .023, -.015, .96),
    (288, .52, .023, -.015, .96),
]
# Hand override blend, forward cap penetration request, tangential and upward offsets.
HAND = [
    (1, 0, -.27, -.02, -.12), (149, 0, -.27, -.02, -.12),
    (160, .42, -.27, -.04, -.12), (170, 1, -.24, -.072, -.155),
    (182, 1, -.290, -.080, -.102), (186, 1, -.195, -.042, -.070),
    (190, 1, -.031, -.005, -.005), (191, 1, 0, 0, 0),
    (195, 1, TRAVEL, 0, 0), (198, 1, TRAVEL, 0, 0),
    (204, 1, 0, 0, 0), (205, 1, -.028, -.010, .008),
    (211, 1, -.215, -.051, .057), (219, .70, -.260, -.043, -.04),
    (232, 0, -.27, -.02, -.12), (288, 0, -.27, -.02, -.12),
]


def smooth(u):
    return u * u * (3 - 2 * u)


def channel(rows, frame):
    """Shape-preserving cubic interpolation. Plateaus stay flat."""
    frames = [row[0] for row in rows]
    if frame <= frames[0]:
        return rows[0][1:]
    if frame >= frames[-1]:
        return rows[-1][1:]
    left = bisect.bisect_right(frames, frame) - 1
    width = frames[left + 1] - frames[left]
    t = (frame - frames[left]) / width

    def slope(row, col):
        if row == 0 or row == len(frames) - 1:
            return 0
        before = (rows[row][col] - rows[row - 1][col]) / (frames[row] - frames[row - 1])
        after = (rows[row + 1][col] - rows[row][col]) / (frames[row + 1] - frames[row])
        return 0 if before * after <= 0 else 2 * before * after / (before + after)

    result = []
    for col in range(1, len(rows[0])):
        a, b = rows[left][col], rows[left + 1][col]
        m, n = width * slope(left, col), width * slope(left + 1, col)
        result.append(a + t * (m + t * (3*(b-a) - 2*m - n + t * (2*(a-b) + m+n))))
    return result


def foot(side, frame):
    distance, swings = STEPS[side]
    direction = -1 if side == "L" else 1
    stride = lift = pitch = 0.0
    for takeoff, land, destination in swings:
        if frame <= takeoff:
            break
        if frame < land:
            u = (frame - takeoff) / (land - takeoff)
            stride = (destination - distance) * smooth(u)
            lift = (.067 if land < 124 else .047) * math.sin(math.pi*u) ** 1.25
            pitch = -.27 * math.sin(2 * math.pi*u)
            break
        distance = destination
    yaw = math.atan2(FORWARD.y, FORWARD.x) - direction * .105
    aim = Vector((math.cos(yaw), math.sin(yaw), 0))
    right = Vector((aim.y, -aim.x, 0))
    pos = START + FORWARD * (distance + stride) + RIGHT * (.129 * direction)
    rotation = Quaternion((0, 0, 1), yaw - math.pi/2) @ Quaternion((1, 0, 0), pitch)
    pos.z = FLOOR + lift + abs(math.sin(pitch)) * SOLE[1] / 2
    ankle = pos + rotation @ Vector((0, -SOLE_SHIFT, .069))
    return pos, rotation, ankle, aim, right


def joint(a, b, first, second, pole):
    diff = b - a
    dist = diff.length
    unit = diff.normalized()
    distance = min(max(dist, abs(first-second) + 1e-5), first+second-1e-5)
    along = (first*first - second*second + distance*distance) / (2*distance)
    bend = (pole - unit * pole.dot(unit)).normalized()
    return a + unit * along + bend * math.sqrt(max(0, first*first-along*along)), max(0, dist-first-second)


def flat(name, value, rough=.73):
    if not isinstance(value, tuple):
        value = (value, value, value)
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*value, 1)
    mat.use_nodes = True
    shader = mat.node_tree.nodes["Principled BSDF"]
    shader.inputs["Base Color"].default_value = (*value, 1)
    shader.inputs["Roughness"].default_value = rough
    shader.inputs["Specular IOR Level"].default_value = .23
    return mat


def finish(obj, name, material, parent=None):
    obj.name = name
    if material:
        obj.data.materials.append(material)
    if parent:
        obj.parent = parent
    return obj


def empty(name, parent=None, loc=(0, 0, 0)):
    obj = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(obj)
    obj.parent, obj.location = parent, loc
    obj.empty_display_size = .026
    obj.rotation_mode = "QUATERNION"
    return obj


def box(name, loc, dim, mat, bevel=.018, parent=None):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    obj = bpy.context.object
    obj.dimensions = dim
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if bevel:
        mod = obj.modifiers.new("Rounded machined corners", "BEVEL")
        mod.width, mod.segments = bevel, 3
        if hasattr(obj.data, "use_auto_smooth"):
            obj.data.use_auto_smooth = True
        obj.modifiers.new("Face normals", "WEIGHTED_NORMAL")
    return finish(obj, name, mat, parent)


def oval(name, loc, radii, mat, parent=None):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=16, location=loc)
    obj = bpy.context.object
    obj.scale = radii
    for polygon in obj.data.polygons:
        polygon.use_smooth = True
    return finish(obj, name, mat, parent)


def rod(name, a, b, radii, mat, parent=None, vertices=20):
    a, b = Vector(a), Vector(b)
    delta = b-a
    bpy.ops.mesh.primitive_cone_add(vertices=vertices, radius1=radii[0], radius2=radii[1],
                                    depth=delta.length, location=(a+b)/2)
    obj = bpy.context.object
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = delta.to_track_quat("Z", "Y")
    mod = obj.modifiers.new("Soft edge", "BEVEL")
    mod.width, mod.segments = min(.008, min(radii)*.3), 2
    if hasattr(obj.data, "use_auto_smooth"):
        obj.data.use_auto_smooth = True
    obj.modifiers.new("Face normals", "WEIGHTED_NORMAL")
    return finish(obj, name, mat, parent)


def setup(camera):
    sc = build_clay_set(camera)
    sc.name = "Studio clay"
    sc.frame_set(1)

    actual_cap = sc.objects["PLAY cap"]
    actual_glyph = sc.objects["PLAY glyph"]
    driver = empty("PLAY Cap | front plane")
    driver.location = CAP_REST
    bpy.context.view_layer.update()
    for obj in (actual_cap, actual_glyph):
        world = obj.matrix_world.copy()
        obj.animation_data_clear()
        obj.parent = driver
        obj.matrix_world = world
    glass = sc.objects["CRT Screen"]
    tree = glass.active_material.node_tree
    tree.animation_data_clear()
    sc["floor_m"] = FLOOR
    sc["cap_rest_m"], sc["cap_axis"], sc["cap_travel_m"] = list(CAP_REST), list(CAP_AXIS), TRAVEL
    sc["acting_arm"] = "R"
    return sc, driver, glass, tree.nodes["Principled BSDF"].inputs["Emission Strength"]


def character():
    scene = bpy.context.scene
    stage_objects = set(scene.objects)
    clay = flat("Studio actor | untextured neutral clay", .38)
    inset = flat("Studio actor | recessed neutral clay", .175)
    base = empty("Studio actor | pelvis")
    torso = empty("Studio actor | torso")
    oval("Belly shell", (0, .003, .271), (.287, .305, .235), clay, torso)
    oval("Waist joint", (0, 0, .084), (.182, .166, .124), inset, torso)
    oval("Chest emblem", (0, .291, .286), (.070, .020, .071), clay, torso)
    oval("Inset chest emblem", (0, .309, .286), (.041, .011, .041), inset, torso)
    head = empty("Studio actor | head gimbal")
    oval("Large dome", (0, 0, .059), (.291, .291, .224), clay, head)
    oval("Face inset", (0, .241, .075), (.214, .062, .107), inset, head)
    for direction in (-1, 1):
        oval("Inset eye", (direction*.079, .296, .083), (.045, .016, .036), clay, head)
        oval("Dome side plate", (direction*.265, .015, .037), (.037, .091, .082), clay, head)
        a = (direction*.146, .001, .224)
        b = (direction*.174, .002, .348)
        rod("Antenna stalk", a, b, (.028, .022), clay, head)
        for k in (-1, 1):
            c = (direction*.187 + k*.032, .004, .418)
            rod("Fork tine", b, c, (.013, .011), clay, head, vertices=12)
            oval("Tine cap", c, (.011, .011, .011), clay, head)
    limbs = {}
    for side in ("L", "R"):
        boot = empty(f"{side} | boot sole")
        box(f"{side} flat sole", (0, 0, .021), (SOLE[0], SOLE[1], .042), inset, .006, boot)
        box(f"{side} boot shell", (0, -.005, .065), (.153, .156, .095), clay, .030, boot)
        oval(f"{side} boot cuff", (0, -.024, .106), (.065, .062, .058), clay, boot)
        thigh = empty(f"{side} | upper leg")
        rod(f"{side} thigh guard", (0, 0, .012), (0, 0, THIGH-.012), (.060, .052), clay, thigh)
        shin = empty(f"{side} | lower leg")
        oval(f"{side} knee socket", (0, 0, 0), (.058, .058, .058), inset, shin)
        rod(f"{side} shin guard", (0, 0, .019), (0, 0, SHIN-.015), (.053, .057), clay, shin)
        arm = empty(f"{side} | upper arm")
        oval(f"{side} shoulder shell", (0, 0, .025), (.086, .079, .096), clay, arm)
        rod(f"{side} upper arm guard", (0, 0, .037), (0, 0, UPPER-.015), (.064, .055), clay, arm)
        fore = empty(f"{side} | forearm")
        oval(f"{side} elbow socket", (0, 0, 0), (.059, .059, .059), inset, fore)
        rod(f"{side} forearm guard", (0, 0, .017), (0, 0, FOREARM-.005), (.063, .071), clay, fore)
        hand = empty(f"{side} | hand")
        oval(f"{side} wrist socket", (0, 0, 0), (.045, .045, .046), inset, hand)
        box(f"{side} pressure pad", (0, 0, .035), (.116, .089, .068), clay, .025, hand)
        if side == "R":
            empty("Hand contact witness", hand, (0, 0, PAD_REACH))
        limbs[side] = (boot, thigh, shin, arm, fore, hand)
    for obj in scene.objects:
        if obj not in stage_objects:
            obj["studio_actor"] = True
    return base, torso, head, limbs


def rotate(obj, rotation):
    # Keep quaternion signs continuous across neighbouring samples.
    if obj.rotation_quaternion.dot(rotation) < 0:
        rotation.negate()
    obj.rotation_quaternion = rotation


def place(obj, location, rotation=None):
    obj.location = location
    if rotation is not None:
        rotate(obj, rotation)


def segment(obj, a, b):
    place(obj, a, (b-a).to_track_quat("Z", "Y"))


def pose(frame, scene_parts, actor, missed):
    _, cap, _, _ = scene_parts
    base, torso, head, limbs = actor
    distance, sway, height = channel(ROOT_KEYS, frame)
    yaw, lean, side_lean, look = channel(ACTING, frame)
    pos = START + FORWARD*distance + RIGHT*sway + Vector((0, 0, height))
    base.location = pos
    forward = Vector((math.cos(yaw), math.sin(yaw), 0))
    right = Vector((forward.y, -forward.x, 0))
    orient = Quaternion((0, 0, 1), yaw - math.pi/2)
    body_rot = orient @ Quaternion((1, 0, 0), -lean) @ Quaternion((0, 1, 0), side_lean)
    place(torso, pos, body_rot)
    head_pos = pos + body_rot @ Vector((0, 0, .473))
    relative = (SCREEN - head_pos).normalized()
    relaxed_yaw = yaw - .15
    look_yaw = math.atan2(relative.y, relative.x)
    blend, depth, across, up = channel(HAND, frame)
    pitch = math.asin(relative.z)*look - .18*blend*(1-look)
    head_rot = Quaternion((0, 0, 1), (1-look)*relaxed_yaw + look*look_yaw - math.pi/2) @ Quaternion((1, 0, 0), pitch)
    place(head, head_pos, head_rot)
    wear = 0.0
    pad_point = None
    for side, (boot, thigh, shin, arm, fore, hand) in limbs.items():
        sign = -1 if side == "L" else 1
        sole, boot_rot, ankle, _, _ = foot(side, frame)
        place(boot, sole, boot_rot)
        hip = pos + RIGHT*(sign*.10)
        knee, leg_error = joint(hip, ankle, THIGH, SHIN, FORWARD + RIGHT*(sign*.19))
        segment(thigh, hip, knee)
        segment(shin, knee, ankle)
        shoulder = pos + body_rot @ Vector((sign*.27, .003, .427))
        walk_weight = 1.0 - smooth(min(1, max(0, (frame-126)/23)))
        opposite = foot("R" if side == "L" else "L", frame)[0]
        swing = .67*(opposite-pos).dot(FORWARD)*walk_weight
        wrist_idle = shoulder + forward*(.032 + swing) + right*(sign*.128) + Vector((0, 0, -.345))
        finger_idle = (forward*.24 + Vector((0, 0, -1))).normalized()
        hand_dir = finger_idle
        wrist = wrist_idle
        if side == "R":
            misses_above = smooth(min(1, max(0, (frame-181)/9))) * smooth(min(1, max(0, (211-frame)/6)))
            offset_z = up - (.155 * misses_above if missed else 0)
            target = PAD_TARGET + CAP_AXIS*depth + CAP_SIDE*across + Vector((0, 0, offset_z))
            hand_dir = finger_idle.lerp(CAP_AXIS, blend).normalized()
            wrist = wrist_idle.lerp(target - hand_dir*PAD_REACH, blend)
            pad_point = wrist + hand_dir*PAD_REACH
        else:
            wrist += forward*(-.11*blend) + right*(-.028*blend) + Vector((0, 0, .016*blend))
        elbow, arm_error = joint(shoulder, wrist, UPPER, FOREARM, right*(sign*.7) + Vector((0, 0, -1)))
        segment(arm, shoulder, elbow)
        segment(fore, elbow, wrist)
        place(hand, wrist, hand_dir.to_track_quat("Z", "Y"))
        wear = max(wear, leg_error, arm_error)

    offset = pad_point - PAD_TARGET
    normal = offset.dot(CAP_AXIS)
    tangent = (offset - CAP_AXIS*normal).length
    if tangent < .021 and normal >= -1e-6:
        cap.location = CAP_REST + CAP_AXIS*min(TRAVEL, max(0, normal))
    else:
        cap.location = CAP_REST
    return wear, (cap.location - CAP_REST).dot(CAP_AXIS)


def bake(scene_parts, actor, mode, path):
    sc, cap, glass, strength = scene_parts
    base, torso, head, limbs = actor
    objects = [base, torso, head, cap, *[item for limb in limbs.values() for item in limb]]
    sparse = {row[0] for row in ROOT_KEYS + ACTING + HAND}
    for _, swings in STEPS.values():
        for start, end, _ in swings:
            sparse.update((start, start+3, (start+end)//2, end-3, end))
    frames = sorted(sparse) if mode == "blocking" else range(1, FRAME_END+1)
    max_overreach = 0
    powered = False
    for frame in frames:
        overreach, travel = pose(frame, scene_parts, actor, mode == "miss")
        max_overreach = max(max_overreach, overreach)
        powered |= travel >= TRAVEL - 1e-5
        for obj in objects:
            obj.keyframe_insert(data_path="location", frame=frame)
            if obj not in (base, cap):
                obj.keyframe_insert(data_path="rotation_quaternion", frame=frame)
        strength.default_value = 1.38 if powered else 0
        strength.keyframe_insert(data_path="default_value", frame=frame)
    if max_overreach > .006:
        raise ValueError(f"unreachable joint in {mode}: {max_overreach:.6f} m")
    sc["max_unreachable_m"] = max_overreach
    sc["curve_stage"] = mode
    sc["reference_height_m"] = REST_HEIGHT_M
    for obj in objects:
        for curve in obj.animation_data.action.fcurves:
            for key in curve.keyframe_points:
                key.interpolation = "CONSTANT" if mode == "blocking" else "BEZIER"
                key.handle_left_type = key.handle_right_type = "AUTO_CLAMPED"
    node_tree = glass.active_material.node_tree
    for curve in node_tree.animation_data.action.fcurves:
        for key in curve.keyframe_points:
            key.interpolation = "CONSTANT"
    for frame, caption in [
        (1, "Approach"), (141, "Final plant"), (149, "Both feet load"),
        (177, "Anticipation"), (191, "Contact"), (195, "Bottom / screen"),
        (204, "Release"), (212, "Recoil"), (237, "Look at the screen"),
    ]:
        sc.timeline_markers.new(caption, frame=frame)
    sc.frame_set(1)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=True)
    return {"scene": path.name, "mode": mode, "keyed_frames": len(frames),
                      "max_ik_overreach_m": max_overreach}
