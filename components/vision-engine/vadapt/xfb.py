"""vadapt.xfb - exact X-framebuffer frames (D11, operator ruling U-DEC-0043 / os-cu-0 U-RUL-0007).

Reference for the OpenSuperficies RamCapture: a capsule runs `Xvfb -fbdir <tmpfs> -nocursor`, so its screen is an XWD
image in RAM (Xvfb_screen0). A reader takes a CONSISTENT frame (two consecutive identical reads: Xvfb writes in place
and about 5 % of raw reads are torn, measured in visual-qa-0/v12p/d11) and ships it raw. The host validates the XWD
strictly and keeps frames only in vadapt.ramws. Never an encoded or lossy source (xpra stream), never disk.

frame_sha256 hashes canonical RGB24 row-major pixels, so it is independent of the XWD header layout and equals the hash
of an X GetImage of the same screen (bit-exact 20/20 in the probe when Xvfb runs with -nocursor).

CLI (the in-capsule reader reference): python3 -m vadapt.xfb read <Xvfb_screen0> > frame.xwd-on-stdout
rc 0 frame written, 2 usage, 5 E_XWD_* or E_TORN.
"""
import hashlib
import os
import stat
import struct
import sys
from collections import namedtuple

XWD_VERSION = 7
ZPIXMAP = 2
TRUECOLOR = 4
DIRECTCOLOR = 5
COLOR_ENTRY = 12
HEADER_FIELDS = 25
MIN_HEADER = HEADER_FIELDS * 4
MAX_SIDE = 16384
TORN_TRIES = 8
# A finite compatibility default for the legacy ``xfb read <path>`` command.
# Trusted capture callers must supply their configured frame cap explicitly.
MAX_FRAME_BYTES = 256 * 1024 * 1024

Header = namedtuple("Header", "header_size width height depth bpp bytes_per_line byte_order ncolors visual_class "
                              "red_mask green_mask blue_mask pixels_offset size")


class XwdError(Exception):
    """Typed XWD validation, limit, source and consistency failure (``code``)."""

    def __init__(self, code, msg=""):
        super().__init__("%s %s" % (code, msg))
        self.code = code


def parse_header(buf):
    """Strictly parse an XWD header for a 24-bit TrueColor ZPixmap at 32 bpp. Raises XwdError."""
    if len(buf) < MIN_HEADER:
        raise XwdError("E_XWD_SHORT", "len %d < %d" % (len(buf), MIN_HEADER))
    f = struct.unpack(">%dI" % HEADER_FIELDS, bytes(buf[:MIN_HEADER]))
    hs, ver, fmt, depth, w, h = f[0], f[1], f[2], f[3], f[4], f[5]
    byte_order, bpp, bpl, vclass = f[7], f[11], f[12], f[13]
    rmask, gmask, bmask, ncolors = f[14], f[15], f[16], f[19]
    if ver != XWD_VERSION:
        raise XwdError("E_XWD_VERSION", "file_version %d" % ver)
    if hs < MIN_HEADER:
        raise XwdError("E_XWD_SHORT", "header_size %d" % hs)
    if fmt != ZPIXMAP or depth != 24 or bpp != 32:
        raise XwdError("E_XWD_FORMAT", "format %d depth %d bpp %d (need ZPixmap 24/32)" % (fmt, depth, bpp))
    if byte_order not in (0, 1) or f[9] not in (0, 1):
        raise XwdError("E_XWD_BYTE_ORDER", "byte_order and bitmap_bit_order must be 0 or 1")
    if vclass not in (TRUECOLOR, DIRECTCOLOR) or (rmask, gmask, bmask) != (0xFF0000, 0xFF00, 0xFF):
        raise XwdError("E_XWD_VISUAL", "class %d masks %06x/%06x/%06x" % (vclass, rmask, gmask, bmask))
    if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE) or bpl < w * 4 or bpl % 4:
        raise XwdError("E_XWD_GEOMETRY", "%dx%d bytes_per_line %d" % (w, h, bpl))
    off = hs + ncolors * COLOR_ENTRY
    return Header(hs, w, h, depth, bpp, bpl, byte_order, ncolors, vclass, rmask, gmask, bmask, off, off + h * bpl)


def validate_frame(buf, width=None, height=None):
    """Header plus EXACT total size (no missing and no extra bytes); optional expected geometry. Returns the Header."""
    hd = parse_header(buf)
    if (width is not None and hd.width != width) or (height is not None and hd.height != height):
        raise XwdError("E_XWD_GEOMETRY", "got %dx%d want %sx%s" % (hd.width, hd.height, width, height))
    if len(buf) != hd.size:
        raise XwdError("E_XWD_SIZE", "len %d != %d" % (len(buf), hd.size))
    return hd


def rgb24(buf, hd=None):
    """Canonical RGB24 row-major bytes (width*height*3). Pixel order follows byte_order: 0 = LSB first (B,G,R,X)."""
    validated = validate_frame(buf)
    if hd is not None and hd != validated:
        raise XwdError("E_XWD_GEOMETRY", "cached header does not match validated frame")
    hd = validated
    w3 = hd.width * 3
    out = bytearray(w3 * hd.height)
    mv = memoryview(buf)
    if hd.byte_order == 0:
        r, g, b = 2, 1, 0
    else:
        r, g, b = 1, 2, 3
    for y in range(hd.height):
        row = mv[hd.pixels_offset + y * hd.bytes_per_line: hd.pixels_offset + y * hd.bytes_per_line + hd.width * 4]
        o = y * w3
        out[o:o + w3:3] = row[r::4]
        out[o + 1:o + w3:3] = row[g::4]
        out[o + 2:o + w3:3] = row[b::4]
    return bytes(out)


def frame_sha256(buf, hd=None):
    """sha256 of the canonical RGB24 pixels: header-independent, comparable with any exact source of the same screen."""
    return hashlib.sha256(rgb24(buf, hd)).hexdigest()


def _check_read_limits(tries, max_bytes):
    if type(tries) is not int or tries < 2:
        raise XwdError("E_XWD_TRIES", "tries must be an exact integer >= 2")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise XwdError("E_XWD_LIMIT", "max_bytes must be a positive exact integer")


def consistent_read(read_fn, tries=TORN_TRIES, *, max_bytes=MAX_FRAME_BYTES):
    """Return immutable bytes after two identical snapshots, within ``tries`` reads.

    Counts exclude no hidden extra reads. A mutable returned buffer is copied
    immediately, so changing the same object cannot masquerade as stability.
    The callback owns its own allocation bound; fd readers additionally check
    fstat *before* allocating each snapshot. Invalid limits call no callback.
    """
    _check_read_limits(tries, max_bytes)

    def snapshot():
        raw = read_fn()
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise XwdError("E_XWD_SOURCE", "read_fn must return a byte buffer")
        size = raw.nbytes if isinstance(raw, memoryview) else len(raw)
        if size > max_bytes:
            raise XwdError("E_XWD_LIMIT", "snapshot exceeds max_bytes")
        return raw if isinstance(raw, bytes) else bytes(raw)

    prev = snapshot()
    for _ in range(tries - 1):
        cur = snapshot()
        if cur == prev:
            return cur
        prev = cur
    raise XwdError("E_TORN", "no two consecutive identical reads in %d" % tries)


def _bounded_stat(fd, max_bytes):
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise XwdError("E_XWD_SOURCE", "fstat failed (errno %s)" % exc.errno) from exc
    if not stat.S_ISREG(st.st_mode):
        raise XwdError("E_XWD_SOURCE", "source must be a regular file")
    if st.st_size > max_bytes:
        raise XwdError("E_XWD_LIMIT", "source exceeds max_bytes")
    if st.st_size < MIN_HEADER:
        raise XwdError("E_XWD_SHORT", "source shorter than XWD header")
    return st


def read_screen_fd(fd, *, max_bytes, tries=TORN_TRIES, width=None, height=None, before_read=None):
    """Read one bounded, validated XWD from a caller-owned regular-file fd.

    A positive exact-integer ``max_bytes`` is mandatory. fstat is checked before
    every pread allocation and afterwards; device, inode and size must stay
    fixed. No mmap is used, avoiding SIGBUS if a writer truncates a live file.
    ``before_read`` is called before each snapshot and before successful return,
    allowing cancellation/expiry exceptions to abort without publication.
    The caller pins trusted path identity/geometry across separate calls.
    """
    _check_read_limits(tries, max_bytes)
    initial = _bounded_stat(fd, max_bytes)
    identity = (initial.st_dev, initial.st_ino, initial.st_size)

    def check_source():
        st = _bounded_stat(fd, max_bytes)
        if (st.st_dev, st.st_ino, st.st_size) != identity:
            raise XwdError("E_XWD_SOURCE", "source identity or size changed")

    def snapshot():
        if before_read is not None:
            before_read()
        check_source()
        try:
            raw = os.pread(fd, initial.st_size, 0)
        except OSError as exc:
            raise XwdError("E_XWD_SOURCE", "pread failed (errno %s)" % exc.errno) from exc
        check_source()
        if len(raw) != initial.st_size:
            raise XwdError("E_XWD_SIZE", "short source snapshot")
        return raw

    buf = consistent_read(snapshot, tries, max_bytes=max_bytes)
    validate_frame(buf, width, height)
    if before_read is not None:
        before_read()
    check_source()
    return buf


def read_screen(path, tries=TORN_TRIES, *, max_bytes=MAX_FRAME_BYTES, width=None, height=None):
    """Bounded, consistent XWD bytes; legacy positional ``path, tries`` retained.

    This low-level reader does not attest RAM storage or ``-nocursor``. Trusted
    source policy belongs to ``x11_ramcap``. Symlink leaves are refused where
    O_NOFOLLOW is supported; the capture layer also anchors parent directories.
    """
    _check_read_limits(tries, max_bytes)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) |
                     getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise XwdError("E_XWD_SOURCE", "open failed (errno %s)" % exc.errno) from exc
    try:
        return read_screen_fd(fd, max_bytes=max_bytes, tries=tries, width=width, height=height)
    finally:
        os.close(fd)


def store(ws, name, buf):
    """Keep one validated frame in a vadapt.ramws workspace (RAM only); returns its canonical sha256."""
    hd = validate_frame(buf)
    ws.reserve(len(buf))
    with open(ws.path(name), "wb") as f:
        f.write(buf)
    return frame_sha256(buf, hd)


def main(argv):
    if len(argv) != 2 or argv[0] != "read":
        sys.stderr.write("usage: python3 -m vadapt.xfb read <Xvfb_screen0>\n")
        return 2
    try:
        buf = read_screen(argv[1])
    except XwdError as e:
        sys.stderr.write("%s\n" % e)
        return 5
    sys.stdout.buffer.write(buf)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
