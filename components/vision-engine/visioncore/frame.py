# @module {"id":"vc.frame","layer":0,"stage":"L0","owner":"ARCH","deps":["vc.canon","vc.errors","vc.layout"],"book":"HB-VC-a0-arch"}
"""@module vc.frame L0 - Frame, FrameSource and the vqa.frame/v1 manifest (SPEC_ARCH §4). Stdlib only: pixels are any
C-contiguous uint8 (H,W,3) buffer object exposing shape/dtype/flags/tobytes (a numpy array)."""
import hashlib
import re

from . import canon, layout
from .errors import VqaError

SCHEMA = "vqa.frame/v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SCOPE = re.compile(r"^[a-z0-9_]{1,48}$")
_STORAGE = ("png_dir", "rgb24_raw")
U32 = (1 << 32) - 1


class Frame:  # @anchor:vc.frame.Frame
    """One decoded frame: rgb read-only uint8 (H,W,3) C-contiguous; sha256 over the decoded rgb24 bytes."""
    __slots__ = ("i", "t_us", "render_index", "rgb", "sha256")

    def __init__(self, i, t_us, render_index, rgb, sha256=None):
        shape = getattr(rgb, "shape", None)
        if shape is None or len(shape) != 3 or shape[2] != 3 or str(rgb.dtype) != "uint8" or not rgb.flags["C_CONTIGUOUS"]:
            raise VqaError("E_VC_FRAME_SHAPE", {"i": i, "shape": list(shape or ())})
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        if sha256 is not None and sha256 != digest:
            raise VqaError("E_VC_FRAME_SHA", {"i": i})
        try:
            rgb.flags.writeable = False
        except ValueError:
            pass
        self.i, self.t_us, self.render_index, self.rgb, self.sha256 = i, t_us, render_index, rgb, digest


class MemoryFrameSource:  # @anchor:vc.frame.MemoryFrameSource
    """FrameSource over in-memory Frames: .meta (manifest minus frames), iteration, len."""

    def __init__(self, frames, meta):
        self._frames = list(frames)
        self.meta = dict(meta)

    def __iter__(self):
        return iter(self._frames)

    def __len__(self):
        return len(self._frames)


def frames_sha256(frame_shas):  # @anchor:vc.frame.frames_sha256
    """sha256 of the canonical list of per-frame sha256 (determinism contract, SPEC_ARCH §3)."""
    return canon.sha256_obj(list(frame_shas))


def _int(o, key, lo, hi, code="E_VC_FRAME_SCHEMA", ctx=None):
    v = o.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise VqaError(code, {"field": key, "value": repr(v)[:40], "ctx": ctx})
    return v


def validate_manifest(m):  # @anchor:vc.frame.validate_manifest
    """Validate a vqa.frame/v1 manifest dict; raises VqaError, returns m."""
    if not isinstance(m, dict) or m.get("schema") != SCHEMA:
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": "schema"})
    W = _int(m, "width", 1, 1 << 30, "E_VC_FRAME_SCHEMA")
    H = _int(m, "height", 1, 1 << 30, "E_VC_FRAME_SCHEMA")
    if W > layout.DIM_MAX or H > layout.DIM_MAX:
        raise VqaError("E_VC_FRAME_SHAPE", {"width": W, "height": H})
    layout.grid_params(W, H)
    if m.get("channels") != 3 or m.get("pixel_format") != "rgb24":
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": "channels/pixel_format"})
    count = _int(m, "count", 1, U32)
    if m.get("storage") not in _STORAGE:
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": "storage"})
    fr = m.get("frames")
    if not isinstance(fr, list) or len(fr) != count:
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": "frames", "reason": "length != count"})
    last_t = -1
    has_ri = [f.get("render_index") is not None for f in fr if isinstance(f, dict)]
    if any(has_ri) and not all(has_ri):  # I14: all frames carry render_index or none (dense index i then; synth exemption)
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": "render_index", "reason": "present on some frames only"})
    for k, f in enumerate(fr):
        if not isinstance(f, dict):
            raise VqaError("E_VC_FRAME_SCHEMA", {"field": "frames[%d]" % k})
        if f.get("i") != k:
            raise VqaError("E_VC_FRAME_SCHEMA", {"field": "i", "ctx": k})
        t = _int(f, "t_us", 0, (1 << 64) - 1, ctx=k)
        if t < last_t:
            raise VqaError("E_VC_FRAME_SCHEMA", {"field": "t_us", "reason": "decreasing", "ctx": k})
        last_t = t
        if f.get("render_index") is not None:
            _int(f, "render_index", 0, U32, ctx=k)
        for key in ("sha256", "file_sha256"):
            if not isinstance(f.get(key), str) or not _HEX64.match(f[key]):
                raise VqaError("E_VC_FRAME_SCHEMA", {"field": key, "ctx": k})
    sc = m.get("scope")
    if not isinstance(sc, dict) or not all(isinstance(sc.get(x), str) and _SCOPE.match(sc[x]) for x in ("app", "scene")):
        raise VqaError("E_VC_FRAME_SCHEMA", {"field": "scope"})
    return m


def write_manifest(path, m):  # @anchor:vc.frame.write_manifest
    validate_manifest(m)
    canon.write_atomic(path, canon.dumps(m))


def read_manifest(path):  # @anchor:vc.frame.read_manifest
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})
    return validate_manifest(canon.loads(text))
