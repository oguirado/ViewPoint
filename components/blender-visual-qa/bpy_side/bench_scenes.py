"""Procedural calibration bench scenes B1..B7 (no assets), BDESIGN_TRUTH 2.5.

Run INSIDE Blender:
  blender -b --factory-startup -noaudio --python bpy_side/bench_scenes.py -- \
      --bench B1 --out <scratch>/B1.blend [--res 640x360] [--frames 8] [--samples 4] [--seed k]
Writes <out>.blend and <out stem>.scene.json (bvqa.scene/v1 subset: pass_index maps, animated[]).
Ortho camera, 1 BU = 32 px (ortho_scale = W/32), emission materials (light independent, cheap).
--seed k (W10S): deterministic per-bench scene jitter from SplitMix64 seeded with (bench_no << 32) ^ k: integer-px
object offsets (velocities, sizes and timings unchanged, so the semantic class and truth labels stay), emission
colours (+-0.08), light energy (+-15 %) and position, camera micro-offset (+-2 px). The first jittered object sits at
an image offset (relative to the camera) that is injective in k mod 169 (scene_slot), so two seeds of one plan give
disjoint captures by construction. Seeded B6 adds a lit diffuse plane under an area light (noise-bearing still
probe). The manifest gains scene_seed + jitter. --seed absent: the scene is the pre-W10S golden.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decode_exr import RANK_TYPES  # noqa: E402

BENCHES = ["B1", "B1x", "B2", "B3", "B4", "B4f", "B4n", "B5", "B5o", "B6", "B7", "B8", "BS"]
DEFAULT_FRAMES = {"B4n": 32}  # every other bench defaults to 8; B4/B4f reach 60 with --frames 60
PX = 1.0 / 32.0  # BU per pixel
FILTER_WIDTH = 0.01  # px; renderer id carries "-box"
M64 = (1 << 64) - 1
SEED_MAX = 1 << 31
SLOT_SIDE, SLOT_MUL, SLOT_BENCH = 13, 37, 11  # scene_slot grid (+-6 px) and multipliers (37 coprime to 169)
JIT = None  # Jitter of the scene being built (None: golden scene)


def splitmix64(x):
    """SplitMix64 step (Steele, Lea, Flood 2014) -> (state, 64-bit output); integer only, platform independent."""
    x = (x + 0x9E3779B97F4A7C15) & M64
    z = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
    return x, z ^ (z >> 31)


def scene_slot(bench_no, k):
    """Image offset (px, relative to the camera) of the first jittered object: injective in k mod 169."""
    i = (SLOT_MUL * k + SLOT_BENCH * bench_no) % (SLOT_SIDE * SLOT_SIDE)
    return i % SLOT_SIDE - SLOT_SIDE // 2, i // SLOT_SIDE - SLOT_SIDE // 2


class Jitter:
    """Seeded scene parameters of one bench; every draw is recorded in the manifest (jitter block)."""

    def __init__(self, bench, seed):
        self.bench_no, self.seed = BENCHES.index(bench) + 1, int(seed)
        self.state = ((self.bench_no << 32) ^ self.seed) & M64
        self.cam = (self.randint(-2, 2), self.randint(-2, 2))
        self.objects, self.colours, self.lights = {}, {}, {}

    def randint(self, lo, hi):
        self.state, z = splitmix64(self.state)
        return lo + z % (hi - lo + 1)

    def place(self, name):
        if self.objects:
            d = (self.randint(-4, 4), self.randint(-4, 4))
        else:
            rel = scene_slot(self.bench_no, self.seed)
            d = (rel[0] + self.cam[0], rel[1] + self.cam[1])
        self.objects[name] = d
        return d

    def colour(self, name, rgb):
        out = tuple(min(0.95, max(0.05, round(v + 0.02 * self.randint(-4, 4), 6))) for v in rgb)
        self.colours[name] = list(out)
        return out

    def light(self, name):
        """-> (energy permille 850..1150, dx px, dy px)."""
        v = (self.randint(850, 1150), self.randint(-16, 16), self.randint(-16, 16))
        self.lights[name] = list(v)
        return v

    def record(self):
        return {"prng": "splitmix64", "seed": self.seed, "camera_px": list(self.cam),
                "objects": {k: list(v) for k, v in self.objects.items()}, "colours": self.colours, "lights": self.lights}


def obj_off(name):
    """Seeded integer-px offset of object `name` ((0, 0) for the golden scene or a non-jittered object)."""
    return JIT.objects.get(name, (0, 0)) if JIT is not None else (0, 0)


def cam_off():
    return JIT.cam if JIT is not None else (0, 0)


class Ctx:
    def __init__(self, bpy, nframes):
        self.bpy, self.nframes = bpy, nframes
        self.animated = []
        self.temporal = []
        self.coverage = []
        self.cuts = []
        self.lit = []
        self.seed_mode = "fixed"

    def note(self, frame, kind, name):
        self.animated.append({"frame": int(frame), "kind": kind, "name": name})


    def temporal_note(self, name, kind, a, b):
        self.temporal.append({"name": name, "kind": kind, "frames": [int(a), int(b)]})


def coverage_note(self, name, ppm, a, b):
    self.coverage.append({"name": name, "ppm": int(ppm), "frames": [int(a), int(b)]})


Ctx.coverage_note = coverage_note


def reset(bpy):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    return bpy.context.scene


def emission_material(bpy, name, color, strength=1.0, checker=None):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.name = "Emission"
    em.inputs["Strength"].default_value = strength
    if checker:
        ck = nt.nodes.new("ShaderNodeTexChecker")
        ck.inputs["Scale"].default_value = checker
        ck.inputs["Color1"].default_value = (*color, 1)
        ck.inputs["Color2"].default_value = (0.05, 0.05, 0.05, 1)
        nt.links.new(ck.outputs["Color"], em.inputs["Color"])
    else:
        em.inputs["Color"].default_value = (*color, 1)
    nt.links.new(em.outputs[0], out.inputs[0])
    return m


def add_box(bpy, name, loc_px, size_px, color, strength=1.0, checker=None, plane=False, jit=True):
    if JIT is not None and jit:
        d = JIT.place(name)
        loc_px = (loc_px[0] + d[0], loc_px[1] + d[1]) + tuple(loc_px[2:])
        color = JIT.colour(name, color)
    if plane:
        bpy.ops.mesh.primitive_plane_add(size=1)
    else:
        bpy.ops.mesh.primitive_cube_add(size=1)
    o = bpy.context.active_object
    o.name = name
    o.location = (loc_px[0] * PX, loc_px[1] * PX, loc_px[2] * PX if len(loc_px) > 2 else 0.0)
    o.scale = (size_px[0] * PX, size_px[1] * PX, size_px[2] * PX if len(size_px) > 2 else 1.0)
    o.data.name = name + "_mesh"
    o.data.materials.append(emission_material(bpy, "m_" + name, color, strength, checker))
    return o


def set_interp(idblock, mode):
    ad = idblock.animation_data
    if ad and ad.action:
        for fc in ad.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = mode


def key_loc(o, frame, x_px, y_px, mode="LINEAR"):
    d = obj_off(o.name)
    o.location.x, o.location.y = (x_px + d[0]) * PX, (y_px + d[1]) * PX
    o.keyframe_insert("location", index=0, frame=frame)
    o.keyframe_insert("location", index=1, frame=frame)
    set_interp(o, mode)


def key_strength(mat, frame, value, mode="CONSTANT"):
    sock = mat.node_tree.nodes["Emission"].inputs["Strength"]
    sock.default_value = value
    sock.keyframe_insert("default_value", frame=frame)
    set_interp(mat.node_tree, mode)


def make_camera(bpy, w, ortho=True):
    cam = bpy.data.cameras.new("cam")
    o = bpy.data.objects.new("cam", cam)
    bpy.context.scene.collection.objects.link(o)
    o.location = (cam_off()[0] * PX, cam_off()[1] * PX, 10)
    o.rotation_euler = (0, 0, 0)
    if ortho:
        cam.type = "ORTHO"
        cam.ortho_scale = w * PX
    else:
        cam.type = "PERSP"
        cam.lens = 35
    bpy.context.scene.camera = o
    return o


def b1(c, w, h):
    """6 rigid boxes of 48x48 px (>= 2s + 2*EDGE_RING at s16, so RIGID tiles exist), 3 rows x 2 cols, 1..16 px/f."""
    bpy = c.bpy
    cols = ((16, 8, 5), (3, 2, 1))  # column 0 holds the fast boxes (more travel room)
    for ci, col in enumerate(cols):
        for ri, v in enumerate(col):
            i = ci * 3 + ri
            y = h / 2 - (ri + 0.5) * h / 3
            x0 = -w / 2 + 32 if ci == 0 else 24
            o = add_box(bpy, "b1_box%d" % v, (x0, y), (48, 48), (0.9, 0.4 + 0.08 * i, 0.2),
                        checker=(8.0 if i % 2 else None))
            key_loc(o, 1, x0, y)
            key_loc(o, c.nframes, x0 + v * (c.nframes - 1), y)
            c.note(1, "transform", o.name)


def b1x(c, w, h):
    """Fast boxes at 48/64/80 px/frame (MOTION_D_MAX 64: <= 64 MOVED, > 64 not). Travel stops at the right edge."""
    bpy = c.bpy
    for ri, v in enumerate((48, 64, 80)):
        y = h / 2 - (ri + 0.5) * h / 3
        x0 = -w / 2 + 48
        k = max(1, min(c.nframes - 1, int((w - 96) // v)))  # frames of travel that stay on screen
        o = add_box(bpy, "b1x_box%d" % v, (x0, y), (48, 48), (0.9, 0.5 - 0.1 * ri, 0.2 + 0.2 * ri),
                    checker=(8.0 if ri % 2 == 0 else None))
        key_loc(o, 1, x0, y)
        key_loc(o, 1 + k, x0 + v * k, y)
        c.note(1, "transform", o.name)


def b2(c, w, h):
    bpy = c.bpy
    a = add_box(bpy, "b2_slideA", (-40, 20), (60, 40), (0.2, 0.7, 0.3), plane=True)
    b = add_box(bpy, "b2_slideB", (40, 10), (60, 40), (0.8, 0.2, 0.6), plane=True)
    b.location.z = 0.1
    key_loc(a, 1, -40, 20)
    key_loc(a, c.nframes, -40 + 4 * (c.nframes - 1), 20)
    key_loc(b, 1, 40, 10)
    key_loc(b, c.nframes, 40 - 4 * (c.nframes - 1), 10)
    for o in (a, b):
        c.note(1, "transform", o.name)
    app = add_box(bpy, "b2_appear", (0, -50), (30, 30), (0.9, 0.9, 0.1), plane=True)
    van = add_box(bpy, "b2_vanish", (-60, -50), (30, 30), (0.1, 0.8, 0.9), plane=True)
    mid = max(2, c.nframes // 2)
    for o, before, after in ((app, True, False), (van, False, True)):
        o.hide_render = before
        o.keyframe_insert("hide_render", frame=1)
        o.hide_render = after
        o.keyframe_insert("hide_render", frame=mid)
        set_interp(o, "CONSTANT")
        c.note(mid, "visibility", o.name)


def b3(c, w, h):
    bpy = c.bpy
    steps = [0.25, 0.5, 1, 2, 4, 8, 16]
    n = len(steps)
    for i, s in enumerate(steps):
        x = -w / 2 + (i + 0.5) * w / n
        o = add_box(bpy, "b3_emit%02d" % i, (x, h / 4), (w / n - 6, 40), (0.2, 0.2, 0.2), plane=True)
        m = o.data.materials[0]
        key_strength(m, 1, 1.0)
        for f in range(2, c.nframes + 1):
            key_strength(m, f, s if f >= 2 else 1.0)
        c.note(2, "material", o.name)
    # lit Principled plane + stepped point light
    pl = add_box(bpy, "b3_lit", (0, -h / 4), (w - 20, 40), (0.5, 0.5, 0.5), plane=True)
    mat = pl.data.materials[0]
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bsdf.outputs[0], out.inputs[0])
    e, dx, dy = JIT.light("b3_light") if JIT is not None else (1000, 0, 0)
    bpy.ops.object.light_add(type="POINT", location=(dx * PX, dy * PX, 3))
    lt = bpy.context.active_object
    lt.name = "b3_light"
    for f in range(1, c.nframes + 1):
        lt.data.energy = (50.0 if f < 3 else 400.0) * e / 1000
        lt.data.keyframe_insert("energy", frame=f)
    set_interp(lt.data, "CONSTANT")
    c.note(3, "light", lt.name)
    c.lit.append("b3_lit")


def b4(c, w, h):
    bpy = c.bpy
    for i, p in enumerate((2, 3, 4)):
        x = -w / 2 + (i + 0.5) * w / 6
        o = add_box(bpy, "b4_flick_p%d" % p, (x, h / 4), (w / 6 - 8, 40), (0.7, 0.7, 0.7), plane=True)
        m = o.data.materials[0]
        for f in range(1, c.nframes + 1):
            key_strength(m, f, 1.0 if (f - 1) % p < p // 2 else 0.2)
            if f > 1:
                c.note(f, "material", o.name)
        c.temporal_note(o.name, "flicker", 2, c.nframes)
    fz = add_box(bpy, "b4_freeze", (-w / 4, -h / 4), (20, 20), (0.2, 0.6, 0.9))
    stop = max(2, c.nframes // 2)
    key_loc(fz, 1, -w / 4, -h / 4)
    key_loc(fz, stop, -w / 4 + 4 * (stop - 1), -h / 4)
    c.note(1, "transform", fz.name)
    c.temporal_note(fz.name, "frozen", stop + 1, c.nframes)
    cr = add_box(bpy, "b4_creep", (0, -h / 4), (20, 20), (0.9, 0.6, 0.2))
    key_loc(cr, 1, 0, -h / 4)
    key_loc(cr, c.nframes, (c.nframes - 1) / 8.0, -h / 4)
    c.note(1, "transform", cr.name)
    c.temporal_note(cr.name, "drift", 2, c.nframes)
    for i, v in enumerate((0.5, 0.25)):
        o = add_box(bpy, "b4_ramp%d" % i, (w / 4 + i * 40, -h / 4), (30, 30), (0.3, 0.3, 0.3), plane=True)
        m = o.data.materials[0]
        for f in range(1, c.nframes + 1):  # sawtooth, period 8: the ramp never saturates the 8-bit beauty at 60 f
            key_strength(m, f, 0.2 + v * ((f - 1) % 8), "LINEAR")
            if f > 1:  # strength changes radiance on every frame (the reset frame included)
                c.note(f, "material", o.name)
        c.temporal_note(o.name, "drift", 2, c.nframes)


def seg_bounds(n, stop):
    """Split frames stop+1..n into three consecutive segments [(a, b)] (each at least one frame when room exists)."""
    span = max(1, n - stop)
    out = []
    for k in range(3):
        a = stop + 1 + (k * span) // 3
        b = stop + ((k + 1) * span) // 3
        out.append((min(a, n), min(max(a, b), n)))
    return out


def b4f(c, w, h):
    """One actor freezes at stop; afterwards one mover at a time moves over ~3 %, 8 %, 15 % of the screen (FROZEN_COV_PPM)."""
    bpy = c.bpy
    n = c.nframes
    stop = max(2, n // 4)
    fz = add_box(bpy, "b4f_freeze", (-w / 4, h / 4), (20, 20), (0.2, 0.6, 0.9))
    key_loc(fz, 1, -w / 4, h / 4)
    key_loc(fz, stop, -w / 4 + 4 * (stop - 1), h / 4)
    c.note(1, "transform", fz.name)
    c.temporal_note(fz.name, "frozen", stop + 1, n)
    for k, (ppm, (a, b)) in enumerate(zip((30000, 80000, 150000), seg_bounds(n, stop))):
        side = int(max(8, min(min(w, h) - 8, round((ppm * w * h / 1e6) ** 0.5))))
        name = "b4f_mover%02d" % (ppm // 10000)
        o = add_box(bpy, name, (w / 4, -h / 8), (side, side), (0.9, 0.6 - 0.1 * k, 0.2), checker=6.0)
        key_loc(o, 1, w / 4, -h / 8)
        key_loc(o, n, w / 4 + 2 * (n - 1), -h / 8)
        c.note(1, "transform", o.name)
        for hide, fr in ((a > 1, 1), (False, a)) + (((True, b + 1),) if b < n else ()):
            o.hide_render = hide
            o.keyframe_insert("hide_render", frame=fr)
        set_interp(o, "CONSTANT")
        if a > 1:
            c.note(a, "visibility", name)
        if b < n:
            c.note(b + 1, "visibility", name)
        c.coverage_note(name, ppm, a, b)


def b4n(c, w, h):
    """Static geometry, per-frame seed noise only (FLICKER/DRIFT false-positive control); 32 frames by default."""
    bpy = c.bpy
    for i in range(4):
        add_box(bpy, "b4n_box%d" % i, (-w / 3 + i * w / 4.5, (-1) ** i * h / 6), (36, 36),
                (0.3 + 0.15 * i, 0.5, 0.7 - 0.1 * i), checker=(5.0 if i % 2 else None))
    c.seed_mode = "per_frame"


def b8(c, w, h):
    """Camera cut at frame 4 (marker-bound second camera, whole screen changes) and a half-screen cut."""
    bpy = c.bpy
    n = c.nframes
    cam_a = bpy.context.scene.camera
    cam_a.name = "cam_a"
    off = 2 * w  # px: camera B looks at a disjoint set of boxes
    for i, x in enumerate((-w / 4, w / 4)):
        add_box(bpy, "b8_a%d" % i, (x, 0), (50, 50), (0.9, 0.3 + 0.3 * i, 0.2), checker=(6.0 if i else None))
        add_box(bpy, "b8_b%d" % i, (off + x, 0), (50, 50), (0.2, 0.3 + 0.3 * i, 0.9), checker=(6.0 if i == 0 else None))
    cam_b = make_camera(bpy, w)
    cam_b.name = "cam_b"
    cam_b.location.x = (off + cam_off()[0]) * PX
    bpy.context.scene.camera = cam_a
    cut = min(4, n)
    sc = bpy.context.scene
    sc.timeline_markers.new("cut_a", frame=1).camera = cam_a
    if cut > 1:
        sc.timeline_markers.new("cut_b", frame=cut).camera = cam_b
    half = min(n, max(cut + 1, (n * 3) // 4))
    hp = add_box(bpy, "b8_half", (w / 4, 0), (w / 2, h), (0.8, 0.8, 0.2), plane=True, checker=4.0, jit=False)
    hp.location.z = 0.2
    hp.hide_render = True
    hp.keyframe_insert("hide_render", frame=1)
    if half > 1:
        hp.hide_render = False
        hp.keyframe_insert("hide_render", frame=half)
    set_interp(hp, "CONSTANT")
    if cut > 1:
        c.note(cut, "camera", "cam_b")
        c.cuts.append({"frame": cut, "kind": "full"})
    if half > 1:
        c.note(half, "visibility", hp.name)
        c.cuts.append({"frame": half, "kind": "half"})


def b5(c, w, h, ortho=False):
    bpy = c.bpy
    for i, x in enumerate((-w / 4, 0, w / 4)):
        add_box(bpy, "b5_obj%d" % i, (x, 0), (30, 50), (0.3 + 0.2 * i, 0.5, 0.8),
                checker=(6.0 if i == 1 else None))
    cam = bpy.context.scene.camera
    cx, cy = cam_off()[0] * PX, cam_off()[1] * PX
    if ortho:
        for f in (1, c.nframes):
            cam.location.x = 3 * (f - 1) * PX + cx
            cam.keyframe_insert("location", index=0, frame=f)
        set_interp(cam, "LINEAR")
    else:
        cam.data.type = "PERSP"
        cam.location = (cx, cy, 12)
        cam.keyframe_insert("location", frame=1)
        cam.location = (0.8 + cx, 0.3 + cy, 11)
        cam.keyframe_insert("location", frame=c.nframes)
        set_interp(cam, "LINEAR")
    c.note(1, "camera", cam.name)


def b6(c, w, h):
    bpy = c.bpy
    for i in range(3):
        add_box(bpy, "b6_box%d" % i, (-w / 4 + i * w / 4, 0), (40, 40), (0.8, 0.3 + 0.2 * i, 0.3),
                checker=(5.0 if i == 1 else None))
    if JIT is not None:  # W10S still probe: lit diffuse plane under an area light, so the per-frame seed bears noise
        pl = add_box(bpy, "b6_lit", (0, 0, -32), (w - 16, h - 16), JIT.colour("b6_lit", (0.5, 0.5, 0.5)), plane=True,
                     jit=False)
        nt = pl.data.materials[0].node_tree
        nt.nodes.clear()
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.inputs["Base Color"].default_value = (*JIT.colours["b6_lit"], 1)
        nt.links.new(bsdf.outputs[0], nt.nodes.new("ShaderNodeOutputMaterial").inputs[0])
        e, dx, dy = JIT.light("b6_light")
        bpy.ops.object.light_add(type="AREA", location=(dx * PX, dy * PX, 3))
        lt = bpy.context.active_object
        lt.name = "b6_light"
        lt.data.size, lt.data.energy = 2.0, 300.0 * e / 1000
        c.lit.append("b6_lit")
    c.seed_mode = "per_frame"


def b7(c, w, h):
    bpy = c.bpy
    # plane 256 px wide; checker scale 32 -> square 8 px, period 16 px; moves 16 px/frame (full period)
    o = add_box(bpy, "b7_alias", (-w / 4, 0), (256, 80), (0.9, 0.9, 0.9), plane=True, checker=32.0)
    key_loc(o, 1, -w / 4, 0)
    key_loc(o, c.nframes, -w / 4 + 16 * (c.nframes - 1), 0)
    c.note(1, "transform", o.name)


def bs(c, w, h):
    """Sign probe (T-TR-SIGN/T-TR-YFLIP): one box top-left, +3 px x and +2 px up (world +y) per frame."""
    bpy = c.bpy
    o = add_box(bpy, "bs_box", (-w / 4, h / 4), (24, 24), (0.9, 0.5, 0.1))
    key_loc(o, 1, -w / 4, h / 4)
    key_loc(o, c.nframes, -w / 4 + 3 * (c.nframes - 1), h / 4 + 2 * (c.nframes - 1))
    c.note(1, "transform", o.name)


def assign_pass_index(bpy, scene):
    # hide_render-keyed objects are render-visible at some frame: include every mesh
    obs = sorted((o for o in scene.objects if o.type in RANK_TYPES), key=lambda o: o.name)
    for i, o in enumerate(obs):
        o.pass_index = 1 + i
    mats = sorted(bpy.data.materials, key=lambda m: m.name)
    for i, m in enumerate(mats):
        m.pass_index = 1 + i
    return obs, mats


def build(bench, out, res=(640, 360), nframes=8, samples=4, motion_blur=False, seed=None):
    import bpy
    global JIT
    JIT = Jitter(bench, seed) if seed is not None else None
    scene = reset(bpy)
    w, h = res
    scene.render.engine = "CYCLES"
    cy = scene.cycles
    cy.device, cy.samples, cy.seed = "CPU", samples, 0
    cy.use_animated_seed = cy.use_denoising = cy.use_adaptive_sampling = False
    cy.filter_width = FILTER_WIDTH  # box-like pixel filter: matches the 1-spp nearest-sample truth id pass (DIAG_DNOI RC3)
    scene.render.resolution_x, scene.render.resolution_y = w, h
    scene.render.resolution_percentage = 100
    scene.render.fps = 24
    scene.render.use_motion_blur = motion_blur
    scene.render.threads_mode, scene.render.threads = "FIXED", 4
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_depth = "8"
    scene.render.dither_intensity = 0.0
    scene.view_settings.view_transform = "Standard"
    scene.frame_start, scene.frame_end = 1, nframes
    world = bpy.data.worlds.new("World")
    world.use_nodes = False
    world.color = (0, 0, 0)
    scene.world = world
    c = Ctx(bpy, nframes)
    make_camera(bpy, w, ortho=(bench != "B5"))
    {"B1": b1, "B1x": b1x, "B2": b2, "B3": b3, "B4": b4, "B4f": b4f, "B4n": b4n, "B5": b5, "B6": b6, "B7": b7,
     "B8": b8, "BS": bs, "B5o": lambda c, w, h: b5(c, w, h, ortho=True)}[bench](c, w, h)
    obs, mats = assign_pass_index(bpy, scene)
    manifest = {
        "schema": "bvqa.scene/v1", "bench": bench, "res": [w, h], "frame_range": [1, nframes], "fps": [24, 1],
        "engine": "CYCLES", "seed_mode": c.seed_mode, "filter_width": FILTER_WIDTH,
        "objects": [{"name": o.name, "pass_index": o.pass_index} for o in obs],
        "materials": [{"name": m.name, "pass_index": m.pass_index} for m in mats],
        "animated": sorted(c.animated, key=lambda d: (d["frame"], d["name"], d["kind"])),
        "temporal": sorted(c.temporal, key=lambda d: (d["frames"][0], d["name"])),
        "lit": sorted(c.lit),
    }
    if c.coverage:
        manifest["coverage"] = sorted(c.coverage, key=lambda d: (d["frames"][0], d["name"]))
    if c.cuts:
        manifest["cuts"] = sorted(c.cuts, key=lambda d: d["frame"])
    if JIT is not None:
        manifest["scene_seed"], manifest["jitter"] = JIT.seed, JIT.record()
    bpy.ops.wm.save_as_mainfile(filepath=out, check_existing=False)
    stem = out[:-6] if out.endswith(".blend") else out
    with open(stem + ".scene.json", "w") as fh:
        json.dump(manifest, fh, sort_keys=True, separators=(",", ":"))
    return manifest


def main(argv):
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="bench_scenes.py")
    p.add_argument("--bench", required=True, choices=BENCHES)
    p.add_argument("--out", required=True)
    p.add_argument("--res", default="640x360")
    p.add_argument("--frames", type=int, default=None, help="default 8 (B4n: 32)")
    p.add_argument("--samples", type=int, default=4)
    p.add_argument("--motion-blur", action="store_true", help="negative-test scenes only")
    p.add_argument("--seed", type=int, default=None, help="W10S scene jitter seed k (absent: golden scene)")
    a = p.parse_args(argv)
    if a.seed is not None and not 0 <= a.seed < SEED_MAX:
        p.error("--seed must be in [0, 2^31)")
    w, h = (int(v) for v in a.res.lower().split("x"))
    m = build(a.bench, os.path.abspath(a.out), (w, h), a.frames or DEFAULT_FRAMES.get(a.bench, 8), a.samples, a.motion_blur,
              a.seed)
    print("BENCH_OK %s objects=%d" % (a.bench, len(m["objects"])))


if __name__ == "__main__":
    main(sys.argv)
