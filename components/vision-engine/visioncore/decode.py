# @module {"id":"vc.decode","layer":1,"stage":"L1","owner":"ARCH","deps":["vc.errors"],"book":"HB-VC-a0-arch"}
"""@module vc.decode L1 - PNG -> uint8 (zlib + numpy unfilter, L-29 fix) and rgb24 raw memmap (SPEC_ARCH §4.4)."""
import importlib
import struct
import zlib

import numpy as np

from .errors import VqaError

_SIG = b"\x89PNG\r\n\x1a\n"


def _bad(reason):
    return VqaError("E_VC_PNG", {"reason": reason})


def _chunks(data):
    pos = len(_SIG)
    while pos + 12 <= len(data):
        ln, typ = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + ln]
        crc = struct.unpack_from(">I", data, pos + 8 + ln)[0] if pos + 12 + ln <= len(data) else None
        if crc is None or zlib.crc32(typ + body) & 0xFFFFFFFF != crc:
            raise _bad("bad chunk crc/length")
        yield typ, body
        pos += 12 + ln
        if typ == b"IEND":
            return


def _wavefront(a, H, W, bpp):
    """Unfilter every row at once (needed when Average/Paeth rows are present). Pixel (x,y) depends only on
    (x-1,y), (x,y-1), (x-1,y-1), so all pixels of an anti-diagonal u = x+y are independent: W+H-1 numpy steps
    instead of W*H*bpp scalar steps. a = uint8[H, 1+W*bpp] filtered rows. -> uint8[H, W, bpp]."""
    ft = a[:, 0]
    if int(ft.max()) > 4:
        raise _bad("filter type %d" % int(ft.max()))
    types = set(np.unique(ft).tolist())
    yy, xx = np.indices((H, W))
    U = W + H - 1
    raw = np.zeros((U + 2, H + 1, bpp), dtype=np.uint8)            # skewed: raw[u+2, y+1] = pixel (u-y, y)
    raw[xx + yy + 2, yy + 1] = a[:, 1:].reshape(H, W, bpp)
    S = np.zeros_like(raw)
    f_all = ft.astype(np.int16)[:, None]
    for u in range(U):
        y0, y1 = max(0, u - W + 1), min(H - 1, u) + 1
        A = S[u + 1, y0 + 1:y1 + 1].astype(np.int16)
        B = S[u + 1, y0:y1].astype(np.int16)
        pr = None
        f = f_all[y0:y1]
        if 4 in types:
            C = S[u, y0:y1].astype(np.int16)
            bc, ac = B - C, A - C
            pa, pb, pc = np.abs(bc), np.abs(ac), np.abs(bc + ac)
            pr = np.where((pa <= pb) & (pa <= pc), A, np.where(pb <= pc, B, C))
            if len(types) > 1:
                pr = np.where(f == 4, pr, 0)
        for t, val in ((3, lambda: (A + B) >> 1), (2, lambda: B), (1, lambda: A)):
            if t in types:
                v = np.where(f == t, val(), 0)
                pr = v if pr is None else pr + v
        cur = raw[u + 2, y0 + 1:y1 + 1]
        S[u + 2, y0 + 1:y1 + 1] = cur if pr is None else (cur + pr.astype(np.uint8))
    return S[xx + yy + 2, yy + 1]


def unfilter(raw, H, stride, bpp):  # @anchor:vc.decode.unfilter
    """raw = H rows of (1 filter byte + stride bytes) -> uint8[H, stride]. Filters 0-2 are numpy row ops (Sub is a
    wrapping cumsum over pixels); when any row uses Average/Paeth the whole image goes through the anti-diagonal
    wavefront (_wavefront)."""
    a = np.frombuffer(raw, dtype=np.uint8).reshape(H, stride + 1)
    if bool(((a[:, 0] == 3) | (a[:, 0] == 4)).any()):
        return np.ascontiguousarray(_wavefront(a, H, stride // bpp, bpp)).reshape(H, stride)
    out = np.zeros((H, stride), dtype=np.uint8)
    zero = np.zeros(stride, dtype=np.uint8)
    for y in range(H):
        ft = int(a[y, 0])
        row = a[y, 1:].copy()
        prev = out[y - 1] if y else zero
        if ft == 0:
            pass
        elif ft == 1:
            row = np.cumsum(row.reshape(-1, bpp), axis=0, dtype=np.uint8).reshape(-1)
        elif ft == 2:
            row = row + prev
        else:
            raise _bad("filter type %d" % ft)
        out[y] = row
    return out


def png(src):  # @anchor:vc.decode.png
    """PNG path or bytes -> uint8[H,W,3] (colour types 2/6, depth 8, non-interlaced; alpha dropped)."""
    if isinstance(src, (bytes, bytearray)):
        data = bytes(src)
    else:
        try:
            with open(src, "rb") as fh:
                data = fh.read()
        except OSError as e:
            raise VqaError("E_IO", {"path": str(src), "reason": str(e)[:80]})
    if data[:8] != _SIG:
        raise _bad("not a png")
    ihdr, idat, iend = None, [], False
    for typ, body in _chunks(data):
        if typ == b"IEND":
            iend = True
        elif typ == b"IHDR":
            ihdr = body
        elif typ == b"IDAT":
            idat.append(body)
    if ihdr is None or len(ihdr) != 13 or not idat:
        raise _bad("missing IHDR/IDAT")
    if not iend:
        raise _bad("missing IEND")
    W, H, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr)
    if depth != 8 or ctype not in (2, 6) or interlace != 0 or comp != 0 or filt != 0:
        raise _bad("unsupported depth=%d colour=%d interlace=%d" % (depth, ctype, interlace))
    if not (1 <= W <= 8192 and 1 <= H <= 8192):
        raise _bad("dimensions %dx%d" % (W, H))
    bpp = 3 if ctype == 2 else 4
    stride = W * bpp
    want = H * (stride + 1)
    try:
        d = zlib.decompressobj()
        raw = d.decompress(b"".join(idat), want + 1)    # bounded: an inflate bomb stops at want+1 bytes
    except zlib.error as e:
        raise _bad("zlib: %s" % str(e)[:60])
    if len(raw) != want or d.unconsumed_tail or not d.eof:
        raise _bad("inflated size mismatch")
    img = unfilter(raw, H, stride, bpp).reshape(H, W, bpp)
    return np.ascontiguousarray(img[:, :, :3])


def encode_png(rgb):  # @anchor:vc.decode.encode_png
    """uint8[H,W,3] -> PNG bytes (colour type 2, filter 0, zlib level 6). Deterministic for fixed zlib."""
    H, W, _ = rgb.shape
    raw = np.concatenate([np.zeros((H, 1), dtype=np.uint8), rgb.reshape(H, W * 3)], axis=1).tobytes()

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)

    return _SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def rgb24_raw(path, width, height, count):  # @anchor:vc.decode.rgb24_raw
    """Zero-copy read-only memmap of count raw rgb24 frames -> uint8[count,H,W,3]."""
    try:
        mm = np.memmap(path, dtype=np.uint8, mode="r")
    except (OSError, ValueError) as e:
        raise VqaError("E_IO", {"path": str(path), "reason": str(e)[:80]})
    if mm.size != count * height * width * 3:
        raise VqaError("E_VC_FRAME_SIZE", {"path": str(path), "bytes": int(mm.size)})
    return mm.reshape(count, height, width, 3)


# ------------------------------------------------------------------ optional fast path (CDESIGN_CENG F1)
_GUARD = None      # None = unchecked, True = cv2 bit-equal on the first frame, False = disabled for this run
_CV2 = False       # False = not probed yet, None = unavailable, else the cv2 module


def _cv2():
    global _CV2
    if _CV2 is False:
        try:
            _CV2 = importlib.import_module("cv2")
        except Exception:
            _CV2 = None
    return _CV2


def reset_guard():  # @anchor:vc.decode.reset_guard
    """Forget the one-time equality verdict (start of a new run)."""
    global _GUARD
    _GUARD = None


def fast_state():  # @anchor:vc.decode.fast_state
    """'unchecked' | 'on' | 'off' (off = cv2 missing or equality check negative)."""
    return "unchecked" if _GUARD is None else ("on" if _GUARD else "off")


def _simple_png(data):
    """True iff data is structurally what png() accepts AND carries only IHDR/IDAT/IEND chunks (ancillary chunks such
    as gAMA/iCCP/tRNS can make other decoders disagree). Raises nothing: any doubt -> False."""
    try:
        if data[:8] != _SIG:
            return False
        ihdr, iend = None, False
        for typ, body in _chunks(data):
            if typ == b"IHDR":
                ihdr = body
            elif typ == b"IEND":
                iend = True
            elif typ != b"IDAT":
                return False
        if not iend or ihdr is None or len(ihdr) != 13:
            return False
        W, H, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr)
        return (depth == 8 and ctype in (2, 6) and interlace == 0 and comp == 0 and filt == 0
                and 1 <= W <= 8192 and 1 <= H <= 8192)
    except Exception:
        return False


def _cv2_decode(data):
    cv = _cv2()
    if cv is None:
        return None
    try:
        bgr = cv.imdecode(np.frombuffer(data, dtype=np.uint8), cv.IMREAD_COLOR)
        if bgr is None or bgr.ndim != 3 or bgr.shape[2] != 3 or bgr.dtype != np.uint8:
            return None
        return np.ascontiguousarray(bgr[:, :, ::-1])
    except Exception:
        return None


def png_fast(src):  # @anchor:vc.decode.png_fast
    """Same contract and result as png(). Uses cv2.imdecode only when cv2 is importable, the PNG is plain
    (_simple_png) and the first frame of the run was bit-equal to png(); otherwise png() (the canonical decoder,
    which also raises every refusal)."""
    global _GUARD
    if _GUARD is False:
        return png(src)
    if isinstance(src, (bytes, bytearray)):
        data = bytes(src)
    else:
        try:
            with open(src, "rb") as fh:
                data = fh.read()
        except OSError as e:
            raise VqaError("E_IO", {"path": str(src), "reason": str(e)[:80]})
    if not _simple_png(data):
        return png(data)
    if _GUARD is None:
        ref = png(data)
        fast = _cv2_decode(data)
        if fast is None:
            _GUARD = False
        else:
            _GUARD = bool(fast.shape == ref.shape and np.array_equal(fast, ref))
        return ref
    fast = _cv2_decode(data)
    return fast if fast is not None else png(data)
