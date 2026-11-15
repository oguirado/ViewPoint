"""Integer-only capture journal and content addressing."""
import hashlib
import json
from pathlib import Path

FRAME_CAP = 600
STILL_CAP = 30


class DriveError(Exception):
    def __init__(self, code, reason, rc=2):
        super().__init__(reason)
        self.code, self.reason, self.rc = code, str(reason), rc

    def result(self):
        return {"code": self.code, "rc": self.rc, "reason": self.reason}


def nofloat(value):
    if isinstance(value, float):
        raise DriveError("E_FLOAT", "JSON float")
    if isinstance(value, dict):
        for v in value.values():
            nofloat(v)
    elif isinstance(value, list):
        for v in value:
            nofloat(v)


def canonical(value):
    nofloat(value)
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load(path):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise DriveError("E_SCHEMA", "duplicate key:" + k)
            d[k] = v
        return d
    try:
        value = json.loads(Path(path).read_text(), object_pairs_hook=pairs,
                           parse_constant=lambda x: (_ for _ in ()).throw(DriveError("E_FLOAT", x)))
    except (ValueError, UnicodeError) as e:
        raise DriveError("E_SCHEMA", str(e)) from e
    nofloat(value)
    return value


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(canonical(value))
    tmp.replace(path)


def integer(v, lo, hi, name):
    if type(v) is not int or not lo <= v <= hi:
        raise DriveError("E_SCHEMA", name)
    return v


def text(v, name):
    if not isinstance(v, str) or not v or "\x00" in v:
        raise DriveError("E_SCHEMA", name)
    return v


def frame_list(j):
    f = j.get("frames")
    if not isinstance(f, dict):
        raise DriveError("E_SCHEMA", "frames")
    if set(f) == {"list"}:
        frames = f["list"]
        if not isinstance(frames, list):
            raise DriveError("E_SCHEMA", "frames.list")
    elif set(f) == {"start", "end", "step"}:
        start = integer(f["start"], 0, 1048575, "frames.start")
        end = integer(f["end"], start, 1048575, "frames.end")
        step = integer(f["step"], 1, 1048575, "frames.step")
        if (end - start) // step + 1 > FRAME_CAP:
            raise DriveError("E_BUDGET", "frames", 6)
        frames = list(range(start, end + 1, step))
    else:
        raise DriveError("E_SCHEMA", "frames fields")
    if len(frames) > FRAME_CAP:
        raise DriveError("E_BUDGET", "frames", 6)
    if not frames:
        raise DriveError("E_SCHEMA", "empty frames")
    for v in frames:
        integer(v, 0, 1048575, "frame")
    if any(a >= b for a, b in zip(frames, frames[1:])):
        raise DriveError("E_SCHEMA", "frames not increasing")
    return frames


def validate(j):
    nofloat(j)
    if not isinstance(j, dict) or j.get("schema") != "bvqa.journal/v1":
        raise DriveError("E_SCHEMA", "journal schema")
    required = {"schema", "scene", "view_layer", "camera", "res", "fps", "frames", "seed", "seed_mode", "ops", "animated"}
    if not required <= set(j) or set(j) - required - {"budgets"}:
        raise DriveError("E_SCHEMA", "journal fields")
    for k in ("scene", "view_layer", "camera"):
        text(j[k], k)
    for k in ("res", "fps"):
        if not isinstance(j[k], list) or len(j[k]) != 2:
            raise DriveError("E_SCHEMA", k)
        for v in j[k]:
            integer(v, 1, 16384 if k == "res" else 100000, k)
    if j["res"][0] * j["res"][1] > 16777216:
        raise DriveError("E_BUDGET", "resolution", 6)
    integer(j["seed"], 0, (1 << 20) - 1, "seed")
    if j["seed_mode"] not in ("fixed", "per_frame"):
        raise DriveError("E_SCHEMA", "seed_mode")
    frame_list(j)
    if not isinstance(j["ops"], list) or not isinstance(j["animated"], list):
        raise DriveError("E_SCHEMA", "ops/animated")
    still_count = 0
    for op in j["ops"]:
        if not isinstance(op, dict):
            raise DriveError("E_SCHEMA", "op")
        kind = op.get("op")
        allowed = {"obj_key": {"op", "obj", "frame", "loc_um", "loc_mm", "rot_mdeg", "scale_ppm", "interp"},
                   "camera_path": {"op", "keys", "interp"}, "hide": {"op", "obj", "frame", "render"},
                   "still": {"op", "count"}}
        if kind not in allowed or set(op) - allowed[kind]:
            raise DriveError("E_SCHEMA", "op fields")
        if kind == "still":
            still_count += 1
            integer(op.get("count"), 0, STILL_CAP, "still.count")
            if still_count > 1:
                raise DriveError("E_SCHEMA", "duplicate still")
            continue
        if kind == "hide":
            text(op.get("obj"), "obj")
            integer(op.get("frame"), 0, 1048575, "op.frame")
            if type(op.get("render")) is not bool:
                raise DriveError("E_SCHEMA", "hide.render")
            continue
        if op.get("interp") not in ("LINEAR", "CONSTANT", "BEZIER"):
            raise DriveError("E_SCHEMA", "interp")
        keys = op.get("keys") if kind == "camera_path" else [op]
        if kind == "obj_key":
            text(op.get("obj"), "obj")
        if not isinstance(keys, list) or not keys:
            raise DriveError("E_SCHEMA", "keys")
        prev = -1
        for key in keys:
            if not isinstance(key, dict) or (kind == "camera_path" and set(key) - {"frame", "loc_um", "loc_mm", "rot_mdeg"}):
                raise DriveError("E_SCHEMA", "key fields")
            frame = integer(key.get("frame"), 0, 1048575, "key.frame")
            if frame <= prev:
                raise DriveError("E_SCHEMA", "keys not increasing")
            prev = frame
            if "loc_um" in key and "loc_mm" in key:
                raise DriveError("E_SCHEMA", "ambiguous location")
            touched = False
            for prop in ("loc_um", "loc_mm", "rot_mdeg", "scale_ppm"):
                if prop in key:
                    touched = True
                    if not isinstance(key[prop], list) or len(key[prop]) != 3:
                        raise DriveError("E_SCHEMA", prop)
                    for v in key[prop]:
                        integer(v, -(1 << 53), (1 << 53), prop)
            if not touched:
                raise DriveError("E_SCHEMA", "empty key")
    for item in j["animated"]:
        if not isinstance(item, dict) or set(item) != {"frame", "kind", "name"}:
            raise DriveError("E_SCHEMA", "animated")
        integer(item["frame"], 0, 1048575, "animated.frame")
        text(item["name"], "animated.name")
        if item["kind"] not in ("transform", "material", "light", "visibility", "camera"):
            raise DriveError("E_SCHEMA", "animated.kind")
    if "budgets" in j:
        if not isinstance(j["budgets"], dict) or set(j["budgets"]) != {"tris_object", "tris_scene"}:
            raise DriveError("E_SCHEMA", "budgets")
        for v in j["budgets"].values():
            integer(v, 0, 1 << 53, "budget")
    return j


# @anchor:bv.journal.cycles_seed
# R-B9: duplicated in bpy_side/drive_common.py until the shared canon owner lands it.
def cycles_seed(bench_no, k, ri):
    for v, bits in ((bench_no, 24), (k, 20), (ri, 20)):
        integer(v, 0, (1 << bits) - 1, "seed coordinate")
    mask = (1 << 64) - 1
    x = (((bench_no << 40) | (k << 20) | ri) * 0x9E3779B97F4A7C15) & mask
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & mask
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & mask
    return (x ^ (x >> 31)) & 0x7FFFFFFF


def seed_for(j, ri):
    return cycles_seed(0, j["seed"], 0 if j["seed_mode"] == "fixed" else ri)
