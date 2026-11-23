"""bv.assets.orm_pack: pack AO / roughness / metallic into one RGBA ORM PNG with a roughness safety floor (stdlib only).

Port of scr-blender-orm-pack (VG-0240): R=AO, G=roughness (byte >= ASSET_ORM_ROUGH_MIN_BYTE so the GGX alpha^2 term never
reaches zero), B=metallic, A=full. Inputs are 8-bit PNG maps (gray / RGB / RGBA, first channel used) or constants in 0..1000 permille.
Exit codes: 0 pass | 2 usage/bad PNG/dims mismatch | 5 roughness below floor (validate-only) | no stage rc 1.
Receipt schema bvqa.asset-orm-pack/v1, sorted canonical JSON, bound to input_sha256 and prev_sha256.
"""
# @module {"id":"bv.assets.orm_pack","layer":4,"owner":"BASSETS"}
import argparse
import hashlib
import json
import struct
import sys
import zlib

try:  # limits.py is INT-owned; the fallback mirrors the requested row ASSET_ORM_ROUGH_MIN_BYTE.
    from blender_visual_qa.assets.limits import ASSET_ORM_ROUGH_MIN_BYTE
except ImportError:  # pragma: no cover
    ASSET_ORM_ROUGH_MIN_BYTE = 11

SCHEMA = "bvqa.asset-orm-pack/v1"
RC_OK, RC_USAGE, RC_FAIL = 0, 2, 5
PNG_SIG = b"\x89PNG\r\n\x1a\n"
BYTE_MAX = 255
PERMILLE = 1000
DEFAULT_SIZE = 256
MAX_SIDE = 16384
CHANNELS_BY_TYPE = {0: 1, 2: 3, 6: 4}
ZLIB_LEVEL = 6


class OrmError(Exception):
    pass


def _chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)


def encode_png(width, height, rgba):
    """rgba: bytes of length width*height*4 -> 8-bit RGBA PNG, filter 0."""
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), ZLIB_LEVEL)) + _chunk(b"IEND", b"")


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data):
    """-> (width, height, channels, pixels bytes interleaved). 8-bit non-interlaced gray/RGB/RGBA only."""
    if data[:len(PNG_SIG)] != PNG_SIG:
        raise OrmError("not a PNG")
    pos, idat, hdr = len(PNG_SIG), bytearray(), None
    while pos + 8 <= len(data):
        length, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise OrmError("truncated chunk")
        pos += 12 + length
        if tag == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if hdr is None:
        raise OrmError("no IHDR")
    width, height, depth, ctype, _comp, _filt, interlace = hdr
    if depth != 8 or ctype not in CHANNELS_BY_TYPE or interlace != 0 or not width or not height:
        raise OrmError("unsupported PNG (need 8-bit gray/RGB/RGBA, non-interlaced)")
    if width > MAX_SIDE or height > MAX_SIDE:
        raise OrmError("PNG too large")
    ch = CHANNELS_BY_TYPE[ctype]
    stride = width * ch
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise OrmError("bad IDAT: %s" % exc)
    if len(raw) != height * (stride + 1):
        raise OrmError("IDAT size mismatch")
    out, prev = bytearray(), bytearray(stride)
    for y in range(height):
        ft = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if ft == 0:
            pass
        elif ft in (1, 2, 3, 4):
            for i in range(stride):
                a = row[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pred = (a, b, (a + b) >> 1, _paeth(a, b, c))[ft - 1]
                row[i] = (row[i] + pred) & BYTE_MAX
        else:
            raise OrmError("bad filter type")
        out += row
        prev = row
    return width, height, ch, bytes(out)


def _const_byte(text):
    """Constant map value in permille (0..1000) -> byte, round half up."""
    try:
        v = int(text)
    except ValueError:
        raise OrmError("bad constant %r (integer permille expected)" % text)
    v = min(max(v, 0), PERMILLE)
    return (v * BYTE_MAX + PERMILLE // 2) // PERMILLE


def load_map(spec):
    """-> (width, height, bytes per pixel, source bytes); width/height None for a constant."""
    try:
        return (None, None, _const_byte(spec), b"const:" + spec.encode())
    except OrmError:
        pass
    try:
        with open(spec, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise OrmError("cannot read %s: %s" % (spec, exc))
    w, h, ch, px = decode_png(data)
    return (w, h, px[0::ch], data)


def pack_orm(ao, rough, metal, width, height, floor=None):
    """ao/rough/metal: per-pixel bytes (len w*h). Returns (rgba bytes, clamped_count)."""
    floor = ASSET_ORM_ROUGH_MIN_BYTE if floor is None else floor
    n = width * height
    out = bytearray(n * 4)
    clamped = 0
    for i in range(n):
        g = rough[i]
        if g < floor:
            g = floor
            clamped += 1
        out[i * 4:i * 4 + 4] = bytes((ao[i], g, metal[i], BYTE_MAX))
    return bytes(out), clamped


def validate_orm(data, floor=None):
    """-> (violations, first_bad_pixel or None, width, height) for an ORM PNG."""
    floor = ASSET_ORM_ROUGH_MIN_BYTE if floor is None else floor
    w, h, ch, px = decode_png(data)
    if ch < 3:
        raise OrmError("ORM needs RGB(A), got %d channel(s)" % ch)
    bad = [i for i, g in enumerate(px[1::ch]) if g < floor]
    return len(bad), (bad[0] if bad else None), w, h


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"


def make_receipt(rc, body, input_sha256, prev_sha256):
    rec = dict(body)
    rec.update({"schema": SCHEMA, "rc": rc, "input_sha256": input_sha256, "prev_sha256": prev_sha256})
    return rec


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _size(text):
    try:
        w, h = text.lower().split("x")
        w, h = int(w), int(h)
    except ValueError:
        raise OrmError("bad --size %r (WxH)" % text)
    if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE):
        raise OrmError("--size out of range")
    return w, h


def run(argv):
    """CLI core. Returns (rc, receipt_or_None). Prints nothing; main() prints the canonical receipt."""
    ap = argparse.ArgumentParser(prog="orm-pack", description="pack AO/roughness/metallic into an ORM PNG with roughness floor")
    ap.add_argument("--ao", default="1000", help="PNG map or integer permille")
    ap.add_argument("--rough", default="500", help="PNG map or integer permille")
    ap.add_argument("--metal", default="0", help="PNG map or integer permille")
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", default=None, help="WxH for all-constant packs (default 256x256)")
    ap.add_argument("--validate-only", action="store_true", help="check the existing --out ORM PNG against the floor")
    ap.add_argument("--prev-receipt", default=None, help="receipt file whose sha256 is chained as prev_sha256")
    ap.add_argument("--receipt", default=None, help="write the receipt here")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return RC_USAGE, None
    try:
        prev = ""
        if a.prev_receipt:
            with open(a.prev_receipt, "rb") as fh:
                prev = _sha(fh.read())
        if a.validate_only:
            with open(a.out, "rb") as fh:
                raw = fh.read()
            bad, first, w, h = validate_orm(raw)
            rc = RC_FAIL if bad else RC_OK
            body = {"mode": "validate", "width": w, "height": h, "floor_byte": ASSET_ORM_ROUGH_MIN_BYTE,
                    "violations": bad, "first_bad_pixel": first, "status": "FAIL" if bad else "PASS"}
            rec = make_receipt(rc, body, _sha(raw), prev)
        else:
            maps = [load_map(spec) for spec in (a.ao, a.rough, a.metal)]
            srcs = [m[3] for m in maps]
            dims = {(m[0], m[1]) for m in maps if m[0] is not None}
            if len(dims) > 1:
                raise OrmError("map dimensions differ: %s" % sorted(dims))
            if dims:
                (w, h), = dims
            else:
                w, h = _size(a.size) if a.size else (DEFAULT_SIZE, DEFAULT_SIZE)
            n = w * h
            planes = [bytes([m[2]]) * n if m[0] is None else m[2] for m in maps]
            rgba, clamped = pack_orm(planes[0], planes[1], planes[2], w, h)
            png = encode_png(w, h, rgba)
            with open(a.out, "wb") as fh:
                fh.write(png)
            # re-read what was written: the floor must hold in the file, not only in memory
            bad, first, _w, _h = validate_orm(png)
            rc = RC_FAIL if bad else RC_OK
            body = {"mode": "pack", "width": w, "height": h, "floor_byte": ASSET_ORM_ROUGH_MIN_BYTE,
                    "rough_clamped_px": clamped, "violations": bad, "out_sha256": _sha(png),
                    "channel_min": [min(planes[0]), min(rgba[1::4]), min(planes[2])],
                    "status": "FAIL" if bad else "PASS"}
            rec = make_receipt(rc, body, _sha(b"\0".join(srcs)), prev)
    except (OSError, OrmError) as exc:
        sys.stderr.write("orm-pack: %s\n" % (exc,))
        return RC_USAGE, None
    if a.receipt:
        with open(a.receipt, "w") as fh:
            fh.write(canon(rec))
    return rc, rec


def main(argv=None):
    rc, rec = run(sys.argv[1:] if argv is None else argv)
    if rec is not None:
        sys.stdout.write(canon(rec))
    return rc


if __name__ == "__main__":
    sys.exit(main())
