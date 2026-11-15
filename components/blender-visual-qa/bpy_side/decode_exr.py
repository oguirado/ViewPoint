"""Decode Cycles pass EXRs to quantised .npy files (BDESIGN_TRUTH 2.1).

Pure module: stdlib + numpy only, no bpy import at any depth (tools/gates.py
g_bpy_boundary proves it by AST), so truth/decode_worker.py may import it.
Inside Blender the caller has already imported bpy; load_exr_rgba takes it from
sys.modules instead of importing it.
"""
import hashlib
import os
import sys

import numpy as np

# key -> (pass socket name, file suffix)
PASSES = {
    "ob": "IndexOB",
    "ma": "IndexMA",
    "mv": "Vector",
    "z": "Depth",
    "n": "Normal",
    "uv": "UV",
}
BG_Z = 0xFFFFFFFF
# object types that take a pass index; hidden objects included (IMPL-GAP-10, one set for passes.py and bench_scenes.py)
RANK_TYPES = ("MESH", "CURVE", "SURFACE", "META", "FONT")
TILE_MIN_PX, CELL_MAX, AXIS_MAX, DIM_MAX, TILE_MAX_PX = 16, 4096, 255, 8192, 65536


class TruthError(Exception):
    def __init__(self, name, msg, rc=2):
        super().__init__("%s: %s" % (name, msg))
        self.name, self.msg, self.rc = name, msg, rc


def grid_ident(w, h):
    """Pure-int mirror of visioncore.layout.grid_ident (R1 rule): 'grid:WxH:sS' (no visioncore inside Blender)."""
    if not (isinstance(w, int) and isinstance(h, int)) or not (1 <= w <= DIM_MAX and 1 <= h <= DIM_MAX):
        raise TruthError("E_TRUTH_ARGS", "grid: dimension outside 1..8192: %r x %r" % (w, h))
    s = TILE_MIN_PX
    while s <= TILE_MAX_PX:
        gw, gh = -(-w // s), -(-h // s)
        if gw * gh <= CELL_MAX and max(gw, gh) <= AXIS_MAX:
            return "grid:%dx%d:s%d" % (w, h, s)
        s <<= 1
    raise TruthError("E_TRUTH_ARGS", "grid: no tile size fits %dx%d" % (w, h))


def _rnd(x):
    """Round half away from zero (float64 in, float64 out)."""
    x = np.asarray(x, dtype=np.float64)
    return np.sign(x) * np.floor(np.abs(x) + 0.5)


def _flip(a):
    return np.ascontiguousarray(a[::-1])


def quant_index(rgba, valid, label):
    x = rgba[..., 0].astype(np.float64)
    if not np.all(np.isfinite(x)):
        raise TruthError("E_TRUTH_QUANT", "%s: non-finite index" % label)
    r = _rnd(x)
    if r.min() < 0 or r.max() > 65535:
        raise TruthError("E_TRUTH_QUANT", "%s: index out of u16 range" % label)
    r = r.astype(np.uint16)
    allowed = np.array(sorted(set(int(v) for v in valid) | {0}), dtype=np.uint16)
    if not np.all(np.isin(r, allowed)):
        bad = sorted(set(int(v) for v in np.unique(r[~np.isin(r, allowed)])))
        raise TruthError("E_TRUTH_QUANT", "%s: values %s not in manifest set" % (label, bad[:8]))
    return _flip(r)


def quant_motion(rgba):
    v = rgba.astype(np.float64)
    dx = _rnd(-v[..., 0] * 16)
    dy = _rnd(v[..., 1] * 16)
    mv = np.stack([dx, dy], axis=-1)
    return _flip(np.clip(mv, -32768, 32767).astype(np.int16))


def quant_depth(rgba):
    z = rgba[..., 0].astype(np.float64)
    if np.any(np.isnan(z)):
        raise TruthError("E_TRUTH_QUANT", "depth: NaN")
    out = np.minimum(_rnd(z * 1000.0), float(2 ** 32 - 2))
    out = np.where(z >= 1e9, float(BG_Z), np.maximum(out, 0.0))
    return _flip(out.astype(np.uint32))


def quant_normal(rgba):
    n = _rnd(rgba[..., :3].astype(np.float64) * 127)
    return _flip(np.clip(n, -127, 127).astype(np.int8))


def quant_uv(rgba):
    uv = _rnd(rgba[..., :2].astype(np.float64) * 65535)
    return _flip(np.clip(uv, 0, 65535).astype(np.uint16))


def exr_path(exr_dir, pass_name, frame):
    return os.path.join(exr_dir, "%s_%04d.exr" % (pass_name, frame))


def load_exr_rgba(path, colorspace="Non-Color"):
    if colorspace != "Non-Color":
        raise TruthError("E_TRUTH_QUANT", "EXR must be loaded Non-Color, got %r" % colorspace)
    bpy = sys.modules.get("bpy")
    if bpy is None:
        raise TruthError("E_TRUTH_QUANT", "load_exr_rgba needs bpy loaded by the caller (run inside Blender)")
    img = bpy.data.images.load(path, check_existing=False)
    try:
        img.colorspace_settings.name = colorspace
        img.alpha_mode = "CHANNEL_PACKED"
        w, h = img.size
        buf = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
    finally:
        bpy.data.images.remove(img)
    return buf.reshape(h, w, 4)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_arrays(exr_dir, frame, valid_ob, valid_ma, with_uv=True, colorspace="Non-Color"):
    """Return {key: ndarray} for one frame (no file writes)."""
    out = {}
    for key, pname in PASSES.items():
        if key == "uv" and not with_uv:
            continue
        rgba = load_exr_rgba(exr_path(exr_dir, pname, frame), colorspace)
        if key == "ob":
            out[key] = quant_index(rgba, valid_ob, "IndexOB")
        elif key == "ma":
            out[key] = quant_index(rgba, valid_ma, "IndexMA")
        elif key == "mv":
            out[key] = quant_motion(rgba)
        elif key == "z":
            out[key] = quant_depth(rgba)
        elif key == "n":
            out[key] = quant_normal(rgba)
        else:
            out[key] = quant_uv(rgba)
    return out


def write_frame(arrays, truth_dir, frame):
    """np.save each array atomically; return {key: sha256 of the .npy bytes}."""
    shas = {}
    for key, arr in arrays.items():
        final = os.path.join(truth_dir, "f%06d.%s.npy" % (frame, key))
        tmp = final + ".tmp"
        with open(tmp, "wb") as f:
            np.save(f, arr, allow_pickle=False)
        os.replace(tmp, final)
        shas[key] = sha256_file(final)
    return shas


def decode_frame(exr_dir, frame, truth_dir, valid_ob, valid_ma, with_uv=True, delete_exr=True):
    arrays = decode_arrays(exr_dir, frame, valid_ob, valid_ma, with_uv)
    shas = write_frame(arrays, truth_dir, frame)
    if delete_exr:
        for key, pname in PASSES.items():
            p = exr_path(exr_dir, pname, frame)
            if os.path.exists(p):
                os.remove(p)
    return shas


def arrays_equal(a, b):
    return a.keys() == b.keys() and all(
        a[k].dtype == b[k].dtype and a[k].shape == b[k].shape and np.array_equal(a[k], b[k]) for k in a)
