# @module {"id":"va.kernels","layer":4,"owner":"ARCH","deps":["vc.s0","vc.s1","vc.s2","vc.s4"],"book":"HB-VC-s2-change","stage":"S2"}
"""Native kernel dispatch for S0-S4 hot paths with automatic fallback to sealed Python reference."""
import contextlib
import importlib.util
from pathlib import Path

from visioncore import change as _py_change
from visioncore import desc as _py_desc
from visioncore import regions as _py_regions
from vnative import backend as _backend
from vnative import run as _vrun

__all__ = []

DEFAULT_LIB_PATH = _backend.NATIVE_LIB

_REF_DESC = {
    "luma_of_padded": _py_desc.luma_of_padded,
    "_compute": _py_desc._compute,
    "ycocg": _py_desc.ycocg,
    "isqrt": _py_desc.isqrt,
}
_REF_CHANGE = {
    "_tile_sad_windows": _py_change._tile_sad_windows,
    "_tile_sad": _py_change._tile_sad,
    "_tile_sad_clip": _py_change._tile_sad_clip,
    "_axis_shift": _py_change._axis_shift,
    "global_vector": _py_change.global_vector,
    "_tile_sums": _py_change._tile_sums,
    "_islands": _py_change._islands,
    "island_vector": _py_change.island_vector,
}
_REF_REGIONS = {
    "_islands": _py_regions._islands,
    "regions_of": _py_regions.regions_of,
    "adjacency": _py_regions.adjacency,
    "match_regions": _py_regions.match_regions,
}

_ACTIVE_LIB_PATH = DEFAULT_LIB_PATH
_CACHED_PATH = None
_CACHED_MOD = None


def _load_so(lib_path):
    p = Path(lib_path) if lib_path is not None else Path(_ACTIVE_LIB_PATH)
    if not p.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("visioncore_native", str(p))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def get_native_module(lib_path=None):
    """Return the loaded `visioncore_native` module, or `None` if absent/unavailable."""
    global _CACHED_PATH, _CACHED_MOD
    target = Path(_ACTIVE_LIB_PATH if lib_path is None else lib_path)
    if not target.is_file():
        if lib_path is None and _CACHED_PATH == target:
            _CACHED_MOD = None
        return None
    try:
        key = (str(target), target.stat().st_mtime_ns, target.stat().st_size)
    except OSError:
        return None
    if _CACHED_PATH == key and _CACHED_MOD is not None:
        return _CACHED_MOD
    mod = _load_so(target)
    if lib_path is None:
        _CACHED_PATH = key
        _CACHED_MOD = mod
    return mod


def is_native_available(lib_path=None):
    """Return True iff the compiled `visioncore_native` extension can be loaded."""
    return get_native_module(lib_path) is not None


@contextlib.contextmanager
def use_library(lib_path):
    """Temporarily point adapter dispatch at `lib_path` (e.g. a missing path to test Python fallback)."""
    global _ACTIVE_LIB_PATH, _CACHED_PATH, _CACHED_MOD
    prev_path, prev_key, prev_mod = _ACTIVE_LIB_PATH, _CACHED_PATH, _CACHED_MOD
    _ACTIVE_LIB_PATH = Path(lib_path) if lib_path is not None else Path("__absent__.so")
    _CACHED_PATH = None
    _CACHED_MOD = None
    try:
        yield
    finally:
        _ACTIVE_LIB_PATH, _CACHED_PATH, _CACHED_MOD = prev_path, prev_key, prev_mod


@contextlib.contextmanager
def fallback_scope():
    """Force adapter dispatch to use the sealed Python reference implementations."""
    with use_library(Path("__absent_visioncore_native__.so")):
        yield


@contextlib.contextmanager
def mutant_scope(name):
    """Activate a named Rust mutation in `visioncore_native` for the scope of the block."""
    mod = get_native_module()
    if mod is None:
        raise RuntimeError("visioncore_native module is not available")
    prev = mod.get_mutant()
    mod.set_mutant(name)
    try:
        yield mod
    finally:
        mod.set_mutant(prev)


# ============================================================================
# Brief 1: SAD windows with SIMD
# ============================================================================

def _tile_sad_windows(y_cur, y_prev, c, s, tx, ty):
    mod = get_native_module()
    if mod is not None:
        return mod._tile_sad_windows(y_cur, y_prev, c, s, tx, ty)
    return _REF_CHANGE["_tile_sad_windows"](y_cur, y_prev, c, s, tx, ty)


def _tile_sad(y_cur, y_prev, c, s, gh, gw, tx, ty):
    mod = get_native_module()
    if mod is not None:
        return mod._tile_sad(y_cur, y_prev, c, s, gh, gw, tx, ty)
    return _REF_CHANGE["_tile_sad"](y_cur, y_prev, c, s, gh, gw, tx, ty)


def _tile_sad_clip(y_cur, y_prev, c, s, gh, gw, tx, ty):
    mod = get_native_module()
    if mod is not None:
        return mod._tile_sad_clip(y_cur, y_prev, c, s, gh, gw, tx, ty)
    return _REF_CHANGE["_tile_sad_clip"](y_cur, y_prev, c, s, gh, gw, tx, ty)


# ============================================================================
# Brief 2: Motion global_vector, _axis_shift, _tile_sums, _islands, island_vector
# ============================================================================

def _axis_shift(cur, prev, dmax):
    mod = get_native_module()
    if mod is not None:
        return mod._axis_shift(cur, prev, dmax)
    return _REF_CHANGE["_axis_shift"](cur, prev, dmax)


def global_vector(y_cur, y_prev, s):
    mod = get_native_module()
    if mod is not None:
        return mod.global_vector(y_cur, y_prev, s)
    return _REF_CHANGE["global_vector"](y_cur, y_prev, s)


def _tile_sums(absdiff, s, gh, gw):
    mod = get_native_module()
    if mod is not None:
        return mod._tile_sums(absdiff, s, gh, gw)
    return _REF_CHANGE["_tile_sums"](absdiff, s, gh, gw)


def _islands(mask_or_cells):
    mod = get_native_module()
    if mod is not None:
        return mod._islands(mask_or_cells)
    if isinstance(mask_or_cells, dict):
        return _REF_REGIONS["_islands"](mask_or_cells)
    return _REF_CHANGE["_islands"](mask_or_cells)


def island_vector(lab_mask, y_cur, y_prev, s):
    mod = get_native_module()
    if mod is not None:
        return mod.island_vector(lab_mask, y_cur, y_prev, s)
    return _REF_CHANGE["island_vector"](lab_mask, y_cur, y_prev, s)


# ============================================================================
# Brief 3: S1 luma_of_padded, _compute, ycocg, isqrt, describe_padded_tiles
# ============================================================================

def luma_of_padded(padded):
    mod = get_native_module()
    if mod is not None:
        return mod.luma_of_padded(padded)
    return _REF_DESC["luma_of_padded"](padded)


def ycocg(rgb):
    mod = get_native_module()
    if mod is not None:
        return mod.ycocg(rgb)
    return _REF_DESC["ycocg"](rgb)


def isqrt(v):
    mod = get_native_module()
    if mod is not None:
        return mod.isqrt(v)
    return _REF_DESC["isqrt"](v)


def _compute(out, idx, padded, grid, p, y_plane):
    mod = get_native_module()
    if mod is not None:
        return mod._compute(out, idx, padded, grid, p, y_plane)
    return _REF_DESC["_compute"](out, idx, padded, grid, p, y_plane)


def describe_padded_tiles(padded, grid):
    mod = get_native_module()
    if mod is not None:
        return mod.describe_padded_tiles(padded, grid)
    tiles, _ = _py_desc.describe_padded(padded, grid)
    return tiles


# ============================================================================
# Brief 4: S4 region islands, regions_of, adjacency, match_regions
# ============================================================================

def s4_islands(cells):
    mod = get_native_module()
    if mod is not None:
        return mod.s4_islands(cells)
    return _REF_REGIONS["_islands"](cells)


def regions_of(tiles, W, H, frame):
    mod = get_native_module()
    if mod is not None:
        return mod.regions_of(tiles, W, H, frame)
    return _REF_REGIONS["regions_of"](tiles, W, H, frame)


def adjacency(regions):
    mod = get_native_module()
    if mod is not None:
        return mod.adjacency(regions)
    return _REF_REGIONS["adjacency"](regions)


def match_regions(prev, cur, global_mv=(0, 0), s=16):
    mod = get_native_module()
    if mod is not None:
        return mod.match_regions(prev, cur, global_mv, s)
    return _REF_REGIONS["match_regions"](prev, cur, global_mv=global_mv, s=s)


# ============================================================================
# Scoped installation and dual_verify orchestration
# ============================================================================

@contextlib.contextmanager
def installed(lib_path=None):
    """Install native S1/S2/S4 kernels into visioncore modules for the scope of the block,
    falling back cleanly to the sealed Python reference if the native library is absent."""
    mod = get_native_module(lib_path)
    if mod is None:
        yield False
        return

    saved_change = {k: getattr(_py_change, k) for k in _REF_CHANGE}
    saved_regions = {k: getattr(_py_regions, k) for k in _REF_REGIONS}
    saved_desc_extra = {"ycocg": _py_desc.ycocg, "isqrt": _py_desc.isqrt}
    old_real, old_kernels = _backend.KERNELS_REAL, dict(_backend.KERNELS)

    table = {
        "luma_of_padded": mod.luma_of_padded,
        "_compute": mod._compute,
        "grid.pad": mod.pad,
        "words_eq": mod.words_eq,
    }
    _backend.KERNELS_REAL = True
    _backend.KERNELS = table
    try:
        with _backend.installed(table):
            _py_desc.ycocg = mod.ycocg
            _py_desc.isqrt = mod.isqrt
            _py_change._tile_sad_windows = mod._tile_sad_windows
            _py_change._tile_sad = mod._tile_sad
            _py_change._tile_sad_clip = mod._tile_sad_clip
            _py_change._axis_shift = mod._axis_shift
            _py_change.global_vector = mod.global_vector
            _py_change._tile_sums = mod._tile_sums
            _py_change._islands = mod._islands
            _py_change.island_vector = mod.island_vector
            _py_regions._islands = mod.s4_islands
            _py_regions.regions_of = mod.regions_of
            _py_regions.adjacency = mod.adjacency
            _py_regions.match_regions = mod.match_regions
            try:
                yield True
            finally:
                for k, fn in saved_desc_extra.items():
                    setattr(_py_desc, k, fn)
                for k, fn in saved_change.items():
                    setattr(_py_change, k, fn)
                for k, fn in saved_regions.items():
                    setattr(_py_regions, k, fn)
    finally:
        _backend.KERNELS_REAL = old_real
        _backend.KERNELS = old_kernels


def run_dual_verify(src, params=None, hints=None, out_dir=None, lib_path=None):
    """Run `vnative.run.run(..., mode='dual_verify')` with all native S1/S2/S4 kernels active
    on the native leg and the sealed Python reference on the reference leg."""
    target = Path(_ACTIVE_LIB_PATH if lib_path is None else lib_path)
    mod = get_native_module(target)
    if mod is None:
        raise _backend.VNativeError("E_VN_NATIVE_MISSING", {"lib": str(target), "kernels_real": False})

    saved_change = {k: getattr(_py_change, k) for k in _REF_CHANGE}
    saved_regions = {k: getattr(_py_regions, k) for k in _REF_REGIONS}

    # Dispatch S2/S4 through a check on whether `desc._compute` is currently patched by `backend.installed()`
    # so the `dual_verify` native leg uses Rust S2/S4 and the `backend.reference()` leg uses sealed Python S2/S4.
    def _is_native_leg():
        return _py_desc._compute is not _REF_DESC["_compute"]

    _py_change._tile_sad_windows = (
        lambda *a, **kw: mod._tile_sad_windows(*a, **kw)
        if _is_native_leg()
        else _REF_CHANGE["_tile_sad_windows"](*a, **kw)
    )
    _py_change._tile_sad = (
        lambda *a, **kw: mod._tile_sad(*a, **kw)
        if _is_native_leg()
        else _REF_CHANGE["_tile_sad"](*a, **kw)
    )
    _py_change._tile_sad_clip = (
        lambda *a, **kw: mod._tile_sad_clip(*a, **kw)
        if _is_native_leg()
        else _REF_CHANGE["_tile_sad_clip"](*a, **kw)
    )
    _py_change._axis_shift = (
        lambda *a, **kw: mod._axis_shift(*a, **kw)
        if _is_native_leg()
        else _REF_CHANGE["_axis_shift"](*a, **kw)
    )
    _py_change.global_vector = (
        lambda *a, **kw: mod.global_vector(*a, **kw)
        if _is_native_leg()
        else _REF_CHANGE["global_vector"](*a, **kw)
    )
    _py_change._tile_sums = (
        lambda *a, **kw: mod._tile_sums(*a, **kw)
        if _is_native_leg()
        else _REF_CHANGE["_tile_sums"](*a, **kw)
    )
    _py_change._islands = (
        lambda *a, **kw: mod._islands(*a, **kw)
        if _is_native_leg()
        else _REF_CHANGE["_islands"](*a, **kw)
    )
    _py_change.island_vector = (
        lambda *a, **kw: mod.island_vector(*a, **kw)
        if _is_native_leg()
        else _REF_CHANGE["island_vector"](*a, **kw)
    )
    _py_regions._islands = (
        lambda *a, **kw: mod.s4_islands(*a, **kw)
        if _is_native_leg()
        else _REF_REGIONS["_islands"](*a, **kw)
    )
    _py_regions.regions_of = (
        lambda *a, **kw: mod.regions_of(*a, **kw)
        if _is_native_leg()
        else _REF_REGIONS["regions_of"](*a, **kw)
    )
    _py_regions.adjacency = (
        lambda *a, **kw: mod.adjacency(*a, **kw)
        if _is_native_leg()
        else _REF_REGIONS["adjacency"](*a, **kw)
    )
    _py_regions.match_regions = (
        lambda *a, **kw: mod.match_regions(*a, **kw)
        if _is_native_leg()
        else _REF_REGIONS["match_regions"](*a, **kw)
    )

    old_real, old_kernels = _backend.KERNELS_REAL, dict(_backend.KERNELS)
    _backend.KERNELS_REAL = True
    _backend.KERNELS = {
        "luma_of_padded": mod.luma_of_padded,
        "_compute": mod._compute,
        "grid.pad": mod.pad,
        "words_eq": mod.words_eq,
    }
    try:
        return _vrun.run(src, params=params, hints=hints, out_dir=out_dir, mode="dual_verify", lib=target)
    finally:
        _backend.KERNELS_REAL = old_real
        _backend.KERNELS = old_kernels
        for k, fn in saved_change.items():
            setattr(_py_change, k, fn)
        for k, fn in saved_regions.items():
            setattr(_py_regions, k, fn)
