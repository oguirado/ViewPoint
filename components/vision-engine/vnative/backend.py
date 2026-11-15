"""Backend resolution + scoped kernel registry. VISIONCORE_BACKEND is read only here (g_native_scope)."""
import contextlib
import os
import threading
from pathlib import Path

from visioncore import desc

ENV = "VISIONCORE_BACKEND"
MODES = ("python", "native", "dual_verify")
NATIVE_LIB = Path(__file__).resolve().parent.parent / "build" / "native" / "visioncore_native.abi3.so"
KERNELS_REAL = False  # RT1: KERNELS are Python identity shims; RT2 sets True when they call the Rust library

_ORIG = {"luma_of_padded": desc.luma_of_padded, "_compute": desc._compute}


class VNativeError(Exception):
    def __init__(self, code, detail=None):
        self.code, self.detail = code, dict(detail or {})
        super().__init__("%s: %s" % (code, self.detail))


def _shim_luma(padded):
    return _ORIG["luma_of_padded"](padded)


def _shim_compute(out, idx, padded, grid, p, y_plane):
    return _ORIG["_compute"](out, idx, padded, grid, p, y_plane)


# grid.pad and words_eq are RT2 kernel slots (fused describe_frame); None = still Python, never swapped.
KERNELS = {"luma_of_padded": _shim_luma, "_compute": _shim_compute, "grid.pad": None, "words_eq": None}
RT2_SLOTS = ("grid.pad", "words_eq")

_LOCK = threading.RLock()  # held for the whole installed()/reference() scope: one patch state per process
_STACK = []
_BASE = {}


def originals():
    return dict(_ORIG)


def resolve(env=None):
    """env mapping (default os.environ) -> 'python' | 'native' | 'dual_verify'; unset/empty -> python."""
    v = (os.environ if env is None else env).get(ENV)
    if v is None or v == "":
        return "python"
    if v not in MODES:
        raise VNativeError("E_VN_BACKEND", {"value": str(v)[:40]})
    return v


def check_available(mode, lib=None):
    """native needs the built library (never a silent fallback); dual_verify also needs KERNELS_REAL (else it would compare python with its own shim)."""
    need = mode in ("native", "dual_verify")
    lib = NATIVE_LIB if lib is None else Path(lib)
    if mode == "dual_verify" and not KERNELS_REAL:  # identity shims would compare python with itself
        raise VNativeError("E_VN_NATIVE_MISSING", {"lib": str(lib), "kernels_real": False})
    if need and not lib.is_file():
        raise VNativeError("E_VN_NATIVE_MISSING", {"lib": str(lib)})


def _swap(table):
    """Set desc.<name> for every _ORIG name present in table with a callable; return the previous values."""
    saved = {n: getattr(desc, n) for n in _ORIG}
    for n in _ORIG:
        if table.get(n) is not None:
            setattr(desc, n, table[n])
    return saved


def _scope(table):
    token = object()
    with _LOCK:
        saved = _swap(table)
        if not _STACK:
            _BASE.update(saved)
        _STACK.append(token)
        try:
            yield
        finally:
            lifo = _STACK[-1] is token
            _STACK.remove(token)
            for n, f in (_BASE if not _STACK else saved if lifo else {}).items():
                setattr(desc, n, f)
            if not lifo:
                raise VNativeError("E_VN_SCOPE", {"reason": "non-LIFO exit"})


@contextlib.contextmanager
def installed(kernels=None):
    """Swap desc.luma_of_padded / desc._compute for the scope of the block under the process-wide RLock;
    restored in finally. A non-LIFO exit restores and raises E_VN_SCOPE."""
    yield from _scope(KERNELS if kernels is None else kernels)


@contextlib.contextmanager
def reference():
    """Scope in which desc runs the ORIGINAL kernels captured at import (the dual_verify reference leg).
    Refuses (E_VN_SCOPE) when desc is patched by anything other than a live installed() scope."""
    with _LOCK:
        if not _STACK and any(getattr(desc, n) is not _ORIG[n] for n in _ORIG):
            raise VNativeError("E_VN_SCOPE", {"reason": "desc patched outside installed()"})
        yield from _scope(_ORIG)
