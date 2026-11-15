"""Pure stdlib helpers shared by the owned in-Blender drive scripts."""
import argparse
import hashlib
import json
import os
import re
import sys

GEOMETRY = {"MESH", "CURVE", "SURFACE", "META", "FONT"}


class DriveError(Exception):
    def __init__(self, code, reason, rc=2):
        super().__init__(reason)
        self.code, self.reason, self.rc = code, str(reason), rc


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DriveError("E_USAGE", message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def load_json(path):
    def reject_float(v):
        raise DriveError("E_FLOAT", "JSON float")
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise DriveError("E_SCHEMA", "duplicate key:" + k)
            result[k] = v
        return result
    try:
        with open(path) as f:
            return json.load(f, parse_float=reject_float, parse_constant=reject_float, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as e:
        raise DriveError("E_SCHEMA", str(e)) from e


def write(path, value):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(str(path) + ".tmp", "wb") as f:
        f.write(canonical(value))
    os.replace(str(path) + ".tmp", path)


def emit(value):
    line = canonical(value).decode().strip()
    print(line, flush=True)
    print(line, file=sys.stderr, flush=True)


# @anchor:bpy.drive.cycles_seed
# R-B9. Pure-stdlib twin of drive.journal.cycles_seed; equality tested on host.
def cycles_seed(bench_no, k, ri):
    for v, bits in ((bench_no, 24), (k, 20), (ri, 20)):
        if type(v) is not int or not 0 <= v < (1 << bits):
            raise DriveError("E_SCHEMA", "seed coordinate")
    mask = (1 << 64) - 1
    x = (((bench_no << 40) | (k << 20) | ri) * 0x9E3779B97F4A7C15) & mask
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & mask
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & mask
    return (x ^ (x >> 31)) & 0x7FFFFFFF


def assign_indices(bpy):
    objects = sorted((o for o in bpy.data.objects if o.type in GEOMETRY), key=lambda o: o.name)
    mats = sorted(bpy.data.materials, key=lambda m: m.name)
    if len(objects) > 65535 or len(mats) > 65535:
        raise DriveError("E_SCENE", "too_many_objects")
    for group in (objects, mats):
        for index, item in enumerate(group, 1):
            item.pass_index = index
    return objects, mats


def hint_name(obj):
    slug = re.sub(r"[^a-z0-9_]", "_", obj.name.lower())[:31] or "object"
    return slug + "_" + ("%08x" % obj.pass_index)


def warnings(bpy, scene):
    result = set()
    for obj in bpy.data.objects:
        ad = obj.animation_data
        if ad and ad.drivers:
            result.add("py_driver:" + obj.name)
        for mod in obj.modifiers:
            cache = getattr(mod, "point_cache", None)
            if cache is None:
                cache = getattr(getattr(mod, "domain_settings", None), "point_cache", None)
            if mod.type in {"CLOTH", "FLUID", "SOFT_BODY", "PARTICLE_SYSTEM", "DYNAMIC_PAINT"}:
                if cache is None or not cache.is_baked:
                    result.add("unbaked_sim:" + obj.name)
        for particle in obj.particle_systems:
            if not particle.point_cache.is_baked:
                result.add("unbaked_sim:" + obj.name)
    rw = scene.rigidbody_world
    if rw and not rw.point_cache.is_baked:
        result.add("unbaked_sim:rigidbody_world")
    for name in ("frame_change_pre", "frame_change_post", "render_pre", "render_post", "depsgraph_update_pre", "depsgraph_update_post"):
        for h in getattr(bpy.app.handlers, name, []):
            if getattr(h, "__module__", "").startswith("__main__"):
                result.add("handler:" + getattr(h, "__name__", name))
    for image in bpy.data.images:
        if image.source == "FILE" and not image.packed_file and image.filepath:
            if not os.path.isfile(bpy.path.abspath(image.filepath)):
                result.add("missing_image:" + image.name)
    if getattr(bpy.app, "autoexec_fail", False):
        result.add("autoexec_blocked")
    return sorted(result)


def select_scene(bpy, journal):
    scene = bpy.data.scenes.get(journal["scene"])
    if scene is None:
        raise DriveError("E_SCENE", "unknown scene")
    camera = bpy.data.objects.get(journal["camera"])
    if camera is None or camera.type != "CAMERA" or camera.name not in scene.objects:
        raise DriveError("E_SCENE", "no_camera")
    layer = scene.view_layers.get(journal["view_layer"])
    if layer is None:
        raise DriveError("E_SCHEMA", "view_layer")
    scene.camera = camera
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = layer
    return scene


def run(entry):
    try:
        result = entry()
        emit({"code": "OK", "rc": 0, **(result or {})})
    except DriveError as e:
        emit({"code": e.code, "rc": e.rc, "reason": e.reason})
        raise SystemExit(e.rc)
    except Exception as e:
        emit({"code": "E_DRIVE", "rc": 2, "reason": "py_exception", "detail": str(e)})
        raise SystemExit(2)
