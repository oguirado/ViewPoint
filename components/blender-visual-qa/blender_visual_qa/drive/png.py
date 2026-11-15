"""Strict RGB8 PNG decoding, adapted from the source listed in PROVENANCE.md.

Only the capture contract's non-interlaced colour type 2 is accepted.
"""
import struct
import zlib
from .journal import DriveError

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def encode(w, h, rgb):
    if len(rgb) != w * h * 3 or w < 1 or h < 1:
        raise DriveError("E_DECODE", "pixel size")
    scan = b"".join(b"\0" + rgb[y*w*3:(y+1)*w*3] for y in range(h))
    return (PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scan, level=6)) + chunk(b"IEND", b""))


def decode(data):
    def bad(why):
        raise DriveError("E_DECODE", why)
    if not data.startswith(PNG_SIG):
        bad("PNG signature")
    pos, header, ended, idat, phase = 8, None, False, [], 0
    while pos < len(data):
        if pos + 12 > len(data):
            bad("truncated chunk")
        n, kind = struct.unpack(">I4s", data[pos:pos+8])
        if pos + n + 12 > len(data):
            bad("chunk length")
        payload = data[pos+8:pos+8+n]
        crc = struct.unpack(">I", data[pos+8+n:pos+12+n])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            bad("chunk CRC")
        pos += n + 12
        if header is None and kind != b"IHDR":
            bad("IHDR must be first")
        if kind == b"IHDR":
            if header is not None or n != 13:
                bad("IHDR")
            header = struct.unpack(">IIBBBBB", payload)
            w, h, depth, color, compression, filt, interlace = header
            if not 1 <= w <= 16384 or not 1 <= h <= 16384 or w*h > 16777216:
                bad("dimensions")
            if (depth, color, compression, filt, interlace) != (8, 2, 0, 0, 0):
                bad("RGB8 non-interlaced required")
        elif kind == b"IDAT":
            if phase == 2:
                bad("nonconsecutive IDAT")
            phase = 1
            idat.append(payload)
        elif kind == b"IEND":
            if n or not idat or pos != len(data):
                bad("IEND/trailing data")
            ended = True
            break
        else:
            if not kind[0] & 32 and kind != b"PLTE":
                bad("unknown critical chunk")
            if phase == 1:
                phase = 2
    if not ended or header is None:
        bad("missing chunks")
    expected = h * (w*3 + 1)
    try:
        dec = zlib.decompressobj()
        raw = dec.decompress(b"".join(idat), expected + 1)
        if len(raw) != expected or not dec.eof or dec.unused_data or dec.unconsumed_tail:
            bad("decompressed size/stream")
    except zlib.error as e:
        bad(str(e))
    stride, out, prev = w*3, bytearray(), bytearray(w*3)
    for y in range(h):
        f = raw[y*(stride+1)]
        if f > 4:
            bad("filter")
        row = bytearray(raw[y*(stride+1)+1:(y+1)*(stride+1)])
        for i in range(stride):
            a = row[i-3] if i >= 3 else 0
            b = prev[i]
            c = prev[i-3] if i >= 3 else 0
            p = a + b - c
            predictor = a if abs(p-a) <= abs(p-b) and abs(p-a) <= abs(p-c) else b if abs(p-b) <= abs(p-c) else c
            row[i] = (row[i] + (0, a, b, (a+b)//2, predictor)[f]) & 255
        out.extend(row)
        prev = row
    return w, h, bytes(out)
