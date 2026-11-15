"""R1 ops: bpy control scripts as text (Track P, claude-fable-5-2). Pure functions, no bpy import here.

    mark_script(point_path)                    -> RW_RESULT {"op":"mark","path","bytes","digest"}
    restore_script(point_path, load_ui=True)   -> RW_RESULT {"op":"restore","path","load_ui","digest"}
    digest_script()                            -> RW_RESULT {"op":"digest","digest"}

The scripts are data: any broker that owns a bridge whose job namespace holds `bpy` can dispatch them
(ref_broker/bridge.py does; rw-build.md section 6). Each script is self-contained: digest.py is
embedded verbatim, nothing is imported from this repository inside Blender. The last stdout line is
`RW_RESULT <one-line json>` (CONTRACT.md section 4); extra keys `via` and `ms` are informative.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "digest.py"), encoding="utf-8") as _f:
    DIGEST_SRC = _f.read()

RESULT_PREFIX = "RW_RESULT "    # same literal as schema.RESULT_PREFIX (test_ckpt pins the equality)

_PRELUDE = DIGEST_SRC + r'''

# ---- control-script helpers (ops.py prelude) ----
import json as _rw_json
import os as _rw_os
import time as _rw_time

_rw_t0 = _rw_time.monotonic()


def _rw_result(obj):
    obj["ms"] = int((_rw_time.monotonic() - _rw_t0) * 1000)
    return "RW_RESULT " + _rw_json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _rw_update(bpy):
    """Evaluate pending transforms so matrix_world is the same before a save and after a load."""
    try:
        for _w in bpy.data.window_managers[0].windows:
            _w.view_layer.update()
    except Exception:
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass


def _rw_call(bpy, fn):
    """Run a file operator from the bridge timer: plain call first, then with a window override."""
    try:
        res = fn()
        via = "direct"
    except RuntimeError:
        win = bpy.data.window_managers[0].windows[0]
        with bpy.context.temp_override(window=win):
            res = fn()
        via = "override"
    if "FINISHED" not in res:
        raise RuntimeError("file operator returned %r" % (res,))
    return via


def _rw_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
'''


def mark_script(point_path):
    """Save a copy of the current file state to point_path (tmp + rename; bpy.data.filepath unchanged)."""
    return _PRELUDE + r'''
_p = %r
_d = _rw_os.path.dirname(_p)
_rw_os.makedirs(_d, exist_ok=True)
_tmp = _rw_os.path.join(_d, ".tmp." + _rw_os.path.basename(_p))
_rw_update(bpy)
_via = _rw_call(bpy, lambda: bpy.ops.wm.save_as_mainfile(filepath=_tmp, copy=True, check_existing=False, compress=False))
_rw_os.replace(_tmp, _p)
print(_rw_result({"op": "mark", "path": _p, "bytes": _rw_os.path.getsize(_p), "digest": digest_v2(bpy), "via": _via}))
''' % (str(point_path),)


def restore_script(point_path, load_ui=True, sha256=None):
    """Load point_path into the running Blender. With `sha256` the file is re-hashed inside Blender
    right before the load (CKPT_CORRUPT -> job ERROR, nothing loaded)."""
    return _PRELUDE + r'''
_p = %r
_ui = %r
_want = %r
if not _rw_os.path.isfile(_p):
    raise RuntimeError("CKPT_MISSING " + _p)
if _want is not None and _rw_sha256(_p) != _want:      # SHA CHECK (in-Blender)
    raise RuntimeError("CKPT_CORRUPT " + _p)
_via = _rw_call(bpy, lambda: bpy.ops.wm.open_mainfile(filepath=_p, load_ui=_ui, use_scripts=False))
_rw_update(bpy)
print(_rw_result({"op": "restore", "path": _p, "load_ui": _ui, "digest": digest_v2(bpy), "via": _via}))
''' % (str(point_path), bool(load_ui), sha256)


def digest_script():
    return _PRELUDE + r'''
_rw_update(bpy)
print(_rw_result({"op": "digest", "digest": digest_v2(bpy)}))
'''
