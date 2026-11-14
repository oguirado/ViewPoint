"""Byte-exact comparison of python vs native outputs."""
import numpy as np

from .backend import VNativeError


def _div(name, idx, field):
    return VNativeError("E_VN_DIVERGE", {"name": name, "first_index": idx, "field": field})


def assert_same(name, a, b):
    """dtype, shape, bytes. Raises E_VN_DIVERGE {name, first_index, field}; field is a dtype field, 'dtype', 'shape', 'bytes' or 'value'."""
    if isinstance(a, np.ndarray) and isinstance(b, np.ndarray):
        if a.dtype != b.dtype:
            raise _div(name, -1, "dtype")
        if a.shape != b.shape:
            raise _div(name, -1, "shape")
        if a.tobytes() == b.tobytes():
            return
        if a.dtype.names:
            best = None
            for f in a.dtype.names:
                d = np.nonzero((a[f] != b[f]).reshape(len(a), -1).any(axis=1))[0] if a.ndim else np.array([0])
                if len(d) and (best is None or int(d[0]) < best[0]):
                    best = (int(d[0]), f)
            if best:
                raise _div(name, best[0], best[1])
        fa, fb = a.reshape(-1).view(np.uint8), b.reshape(-1).view(np.uint8)
        raise _div(name, int(np.nonzero(fa != fb)[0][0]) // max(a.dtype.itemsize, 1), "bytes")
    if isinstance(a, (bytes, bytearray)) and isinstance(b, (bytes, bytearray)):
        if bytes(a) == bytes(b):
            return
        n = min(len(a), len(b))
        i = next((k for k in range(n) if a[k] != b[k]), n)
        raise _div(name, i, "bytes")
    if a != b:
        raise _div(name, -1, "value")


def assert_seq_same(name, a, b):
    """Per-item comparison of two sequences; E_VN_DIVERGE first_index is the first differing item (length mismatch: min len)."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            raise _div(name, i, "value")
    if len(a) != len(b):
        raise _div(name, min(len(a), len(b)), "length")
