"""Backend-dispatching driver with visioncore.pipeline.analyze's signature (RT1: the sealed driver is analyze)."""
from visioncore import canon, pipeline

from . import backend, dual


def _jb(o):
    return "".join(canon.dumps(x) for x in o).encode()


def compare_runs(a, b):
    """Every tile array, region list, link, global_mv, frame row and GOP file must match byte for byte."""
    dual.assert_same("frames_sha256", str(a.frames_sha256), str(b.frames_sha256))
    dual.assert_same("stages", repr(sorted(a.stages.items())), repr(sorted(b.stages.items())))
    dual.assert_same("n_frames", len(a.tiles), len(b.tiles))
    for k, (x, y) in enumerate(zip(a.tiles, b.tiles)):
        dual.assert_same("tiles[%d]" % k, x, y)
    for k in range(len(a.tiles)):
        dual.assert_same("regions[%d]" % k, _jb(a.regions[k]), _jb(b.regions[k]))
        dual.assert_same("links[%d]" % k, _jb([a.links[k]]), _jb([b.links[k]]))
    dual.assert_seq_same("global_mv", [repr(x) for x in a.global_mv], [repr(x) for x in b.global_mv])
    dual.assert_seq_same("frame_rows", [_jb([x]) for x in a.frame_rows], [_jb([x]) for x in b.frame_rows])
    for name in sorted(set(a.gop_files) | set(b.gop_files)):
        dual.assert_same("gop:" + name, a.gop_files.get(name, b""), b.gop_files.get(name, b""))
    ba, bb = a._blobs(), b._blobs()  # everything run.write(out_dir) would publish, manifest included
    for name in sorted(set(ba) | set(bb)):
        dual.assert_same("blob:" + name, ba.get(name, b""), bb.get(name, b""))


def run(src, params=None, hints=None, out_dir=None, workers=1, cpus=None, mode=None, env=None, lib=None):
    """mode None -> backend.resolve(env). python: sealed analyze untouched. native: kernels installed scoped.
    dual_verify: python reference then native, compared; the python run is returned (and written to out_dir)."""
    mode = backend.resolve(env) if mode is None else mode
    if mode not in backend.MODES:
        raise backend.VNativeError("E_VN_BACKEND", {"value": str(mode)[:40]})
    if mode == "python":
        return pipeline.analyze(src, params, hints, out_dir, workers, cpus)
    if workers is not None and workers > 1:  # RT2 fused describe_frame replaces the fork path; until then refuse
        raise backend.VNativeError("E_VN_WORKERS", {"workers": workers, "mode": mode})
    backend.check_available(mode, lib)
    if mode == "native":
        with backend.installed():
            return pipeline.analyze(src, params, hints, out_dir, workers, cpus)
    frames = list(src)  # sources may be one-shot; both legs see the same frames
    view = _ListSource(src, frames)
    with backend.reference():  # original kernels captured at import, never the patched globals
        ref = pipeline.analyze(view, params, hints, out_dir, workers, cpus)
    with backend.installed():
        nat = pipeline.analyze(view, params, hints, None, workers, cpus)
    compare_runs(ref, nat)
    return ref


class _ListSource:
    def __init__(self, src, frames):
        self.meta, self._f = src.meta, frames
        self.raw_array = getattr(src, "raw_array", None)

    def __len__(self):
        return len(self._f)

    def __iter__(self):
        return iter(self._f)
