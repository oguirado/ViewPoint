"""User-supplied production frames for the noise oracle.

Adapter captures keep the strict RGB8 contract of blender_visual_qa.drive.png. A frame the user
passes with --frame comes straight from a renderer, so this module also reads grey, grey+alpha
and RGBA PNGs at 8 or 16 bits. It keeps the high byte of 16-bit samples and drops alpha without
compositing, as `ffmpeg -pix_fmt rgb24` does, then re-encodes the pixels as RGB8.
"""
import hashlib
import struct
import zlib

from blender_visual_qa.drive.png import PNG_SIG, encode

CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}
NAMES = {0: "grey", 2: "rgb", 4: "grey_alpha", 6: "rgba"}
HINT = "; convert it first, e.g. ffmpeg -i frame.png -pix_fmt rgb24 frame_rgb8.png"


def _bad(why):
    raise ValueError("frame_png_unsupported:" + why + HINT)


def _chunks(data):
    if not data.startswith(PNG_SIG):
        _bad("PNG signature")
    pos, phase = 8, 0
    while pos < len(data):
        if pos + 12 > len(data):
            _bad("truncated chunk")
        n, kind = struct.unpack(">I4s", data[pos:pos+8])
        if pos + n + 12 > len(data):
            _bad("chunk length")
        payload = data[pos+8:pos+8+n]
        if zlib.crc32(kind + payload) & 0xffffffff != struct.unpack(">I", data[pos+8+n:pos+12+n])[0]:
            _bad("chunk CRC")
        pos += n + 12
        # Same structure rules as the strict capture decoder: consecutive IDAT, empty final IEND.
        if kind == b"IDAT":
            if phase == 2:
                _bad("nonconsecutive IDAT")
            phase = 1
        elif kind == b"IEND":
            if n or pos != len(data):
                _bad("IEND/trailing data")
        elif phase == 1:
            phase = 2
        yield kind, payload
        if kind == b"IEND":
            return
    _bad("missing IEND")


def production_rgb8(data):
    """Return (RGB8 PNG bytes, receipt fields) for a production frame PNG."""
    header, idat = None, []
    for kind, payload in _chunks(data):
        if header is None and kind != b"IHDR":
            _bad("IHDR must be first")
        if kind == b"IHDR":
            if header is not None or len(payload) != 13:
                _bad("IHDR")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            idat.append(payload)
        elif not kind[0] & 32:
            if kind == b"PLTE":
                _bad("palette PNG")
            elif kind != b"IEND":
                _bad("unknown critical chunk")
    if header is None or not idat:
        _bad("missing chunks")
    w, h, depth, color, compression, filt, interlace = header
    if not 1 <= w <= 16384 or not 1 <= h <= 16384 or w*h > 16777216:
        _bad("dimensions")
    if color not in CHANNELS:
        _bad("colour type %d" % color)
    if depth not in (8, 16):
        _bad("bit depth %d" % depth)
    if (compression, filt, interlace) != (0, 0, 0):
        _bad("interlaced or nonstandard PNG")
    channels, size = CHANNELS[color], depth // 8
    bpp = channels * size
    stride = w * bpp
    expected = h * (stride + 1)
    try:
        dec = zlib.decompressobj()
        raw = dec.decompress(b"".join(idat), expected + 1)
    except zlib.error as exc:
        _bad("zlib " + str(exc))
    if len(raw) != expected or not dec.eof or dec.unused_data or dec.unconsumed_tail:
        _bad("decompressed size")
    rgb, prev = bytearray(), bytearray(stride)
    for y in range(h):
        f = raw[y*(stride+1)]
        if f > 4:
            _bad("filter")
        row = bytearray(raw[y*(stride+1)+1:(y+1)*(stride+1)])
        for i in range(stride):
            a = row[i-bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i-bpp] if i >= bpp else 0
            p = a + b - c
            predictor = a if abs(p-a) <= abs(p-b) and abs(p-a) <= abs(p-c) else b if abs(p-b) <= abs(p-c) else c
            row[i] = (row[i] + (0, a, b, (a+b)//2, predictor)[f]) & 255
        prev = row
        # High byte of each sample (PNG is big-endian), then colour channels only.
        samples = row[::size]
        if color == 2:
            rgb.extend(samples)
        elif color == 6:
            for x in range(w):
                rgb.extend(samples[x*4:x*4+3])
        else:
            for x in range(w):
                rgb.extend(bytes((samples[x*channels],)) * 3)
    info = {"source_sha256": hashlib.sha256(data).hexdigest(), "width": w, "height": h,
            "colour": NAMES[color], "bit_depth": depth,
            "alpha": "dropped" if color in (4, 6) else "none",
            "converted": (color, depth) != (2, 8)}
    return encode(w, h, bytes(rgb)), info
