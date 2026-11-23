"""Clean-room RGB8 PNG codec (non-interlaced colour type 2, 8-bit) under AGPL-3.0-only."""
import struct
import zlib

from .journal import DriveError

PNG_SIG = b"\x89PNG\r\n\x1a\n"
_MAX_DIM = 16384
_MAX_PIXELS = 16777216


def _fail(reason):
    raise DriveError("E_DECODE", reason)


def chunk(kind, data):
    """Build a single PNG chunk with length and CRC-32."""
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


# @anchor:bv.drive.png.encode
def encode(w, h, rgb):
    """Encode packed RGB8 bytes into a non-interlaced filter-0 PNG byte string."""
    if w < 1 or h < 1 or len(rgb) != w * h * 3:
        _fail("pixel size")
    stride = w * 3
    rows = b"".join(b"\0" + rgb[y * stride:(y + 1) * stride] for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(rows, level=6)
    return PNG_SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _paeth(left, up, up_left):
    est = left + up - up_left
    dl = abs(est - left)
    du = abs(est - up)
    dul = abs(est - up_left)
    if dl <= du and dl <= dul:
        return left
    if du <= dul:
        return up
    return up_left


def _unfilter_rows(raw, w, h):
    stride = w * 3
    out = bytearray(h * stride)
    prev = bytearray(stride)
    step = stride + 1
    for y in range(h):
        base = y * step
        filt = raw[base]
        if filt > 4:
            _fail("filter")
        cur = bytearray(raw[base + 1:base + step])
        if filt == 1:
            for i in range(3, stride):
                cur[i] = (cur[i] + cur[i - 3]) & 0xFF
        elif filt == 2:
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif filt == 3:
            for i in range(stride):
                left = cur[i - 3] if i >= 3 else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filt == 4:
            for i in range(stride):
                left = cur[i - 3] if i >= 3 else 0
                up_left = prev[i - 3] if i >= 3 else 0
                cur[i] = (cur[i] + _paeth(left, prev[i], up_left)) & 0xFF
        out[y * stride:(y + 1) * stride] = cur
        prev = cur
    return bytes(out)


def decode(data):
    """Decode and strictly validate a non-interlaced 8-bit RGB PNG byte string."""
    if not data.startswith(PNG_SIG):
        _fail("PNG signature")
    pos = len(PNG_SIG)
    ihdr = None
    idat_parts = []
    seen_idat = False
    after_idat = False
    ended = False
    total_len = len(data)

    while pos < total_len:
        if pos + 12 > total_len:
            _fail("truncated chunk")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end_payload = pos + 8 + length
        if end_payload + 4 > total_len:
            _fail("chunk length")
        payload = data[pos + 8:end_payload]
        expected_crc = struct.unpack(">I", data[end_payload:end_payload + 4])[0]
        if (zlib.crc32(kind + payload) & 0xFFFFFFFF) != expected_crc:
            _fail("chunk CRC")
        pos = end_payload + 4

        if ihdr is None and kind != b"IHDR":
            _fail("IHDR must be first")
        if kind == b"IHDR":
            if ihdr is not None or length != 13:
                _fail("IHDR")
            w, h, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", payload)
            if not (1 <= w <= _MAX_DIM and 1 <= h <= _MAX_DIM and w * h <= _MAX_PIXELS):
                _fail("dimensions")
            if (depth, color, comp, filt, interlace) != (8, 2, 0, 0, 0):
                _fail("RGB8 non-interlaced required")
            ihdr = (w, h)
        elif kind == b"IDAT":
            if after_idat:
                _fail("nonconsecutive IDAT")
            seen_idat = True
            idat_parts.append(payload)
        elif kind == b"IEND":
            if length != 0 or not seen_idat or pos != total_len:
                _fail("IEND/trailing data")
            ended = True
            break
        else:
            is_critical = (kind[0] & 32) == 0
            if is_critical and kind != b"PLTE":
                _fail("unknown critical chunk")
            if seen_idat:
                after_idat = True

    if not ended or ihdr is None:
        _fail("missing chunks")
    w, h = ihdr
    expected_bytes = h * (w * 3 + 1)
    try:
        decomp = zlib.decompressobj()
        raw = decomp.decompress(b"".join(idat_parts), expected_bytes + 1)
        if len(raw) != expected_bytes or not decomp.eof or decomp.unused_data or decomp.unconsumed_tail:
            _fail("decompressed size/stream")
    except zlib.error as exc:
        _fail(str(exc))

    return w, h, _unfilter_rows(raw, w, h)
