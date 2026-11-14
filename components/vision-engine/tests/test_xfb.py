"""vadapt.xfb: strict XWD validation, canonical frame hash, consistent reads (D11, U-DEC-0043).

The conformance vectors in docs/spec/d11_xfb_vectors.json are regenerated from build_xwd() and must match exactly:
os-cu-0 implements the Go RamCapture against the same file. The live check additionally requires
VADAPT_RUN_LIVE_XVFB=1; the default gate is entirely offline.
"""
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vadapt import xfb

VECTORS = Path(__file__).resolve().parents[1] / "docs" / "spec" / "d11_xfb_vectors.json"


def build_xwd(w, h, pixel, *, name=b"Xvfb main window\x00", ncolors=0, byte_order=0, depth=24, bpp=32, fmt=2,
              vclass=4, masks=(0xFF0000, 0xFF00, 0xFF), pad=0, version=7):
    """Synthetic XWD: pixel(x, y) -> (r, g, b). byte_order 0 stores B,G,R,X (LSB first) like Xvfb on little-endian."""
    hs = xfb.MIN_HEADER + len(name)
    bpl = w * 4 + pad
    head = struct.pack(">25I", hs, version, fmt, depth, w, h, 0, byte_order, 32, byte_order, 32, bpp, bpl, vclass,
                       masks[0], masks[1], masks[2], 8, ncolors, ncolors, w, h, 0, 0, 0) + name
    body = bytearray(ncolors * xfb.COLOR_ENTRY)
    for y in range(h):
        row = bytearray(bpl)
        for x in range(w):
            r, g, b = pixel(x, y)
            row[x * 4:x * 4 + 4] = bytes((b, g, r, 0)) if byte_order == 0 else bytes((0, r, g, b))
        body += row
    return head + bytes(body)


def grad(x, y):
    return ((x * 37 + y) % 256, (y * 53 + 7) % 256, (x * y) % 256)


CASES = [
    ("lsb_8x4", dict(w=8, h=4)),
    ("msb_8x4", dict(w=8, h=4, byte_order=1)),
    ("padded_rows_5x3", dict(w=5, h=3, pad=8)),
    ("colormap_256_6x2", dict(w=6, h=2, ncolors=256)),
    ("short_name_header_7x3", dict(w=7, h=3, name=b"x\x00")),
]
BAD = [
    ("bpp24", dict(w=4, h=2, bpp=24), "E_XWD_FORMAT"),
    ("depth32", dict(w=4, h=2, depth=32), "E_XWD_FORMAT"),
    ("xy_pixmap", dict(w=4, h=2, fmt=1), "E_XWD_FORMAT"),
    ("pseudocolor", dict(w=4, h=2, vclass=3), "E_XWD_VISUAL"),
    ("swapped_masks", dict(w=4, h=2, masks=(0xFF, 0xFF00, 0xFF0000)), "E_XWD_VISUAL"),
    ("version6", dict(w=4, h=2, version=6), "E_XWD_VERSION"),
]


def build_vectors():
    good = []
    for name, kw in CASES:
        kw = dict(kw)
        buf = build_xwd(kw.pop("w"), kw.pop("h"), grad, **kw)
        hd = xfb.validate_frame(buf)
        good.append({"name": name, "xwd_sha256": hashlib.sha256(buf).hexdigest(), "size": len(buf),
                     "width": hd.width, "height": hd.height, "frame_sha256": xfb.frame_sha256(buf, hd)})
    bad = []
    for name, kw, code in BAD:
        bad.append({"name": name, "code": code, "params": {k: v for k, v in kw.items() if k not in ("w", "h")}})
    return {"schema": "d11.xfb.vectors/v1", "pixel_fn": "r=(x*37+y)%256 g=(y*53+7)%256 b=(x*y)%256",
            "good": good, "bad": bad,
            "size_rules": ["extra trailing byte -> E_XWD_SIZE", "one missing byte -> E_XWD_SIZE"],
            "torn_rule": "accept a frame only after two consecutive byte-identical reads; E_TORN after 8 reads"}


class TestXfb(unittest.TestCase):
    def test_lsb_and_msb_give_the_same_canonical_hash(self):
        a = build_xwd(8, 4, grad)
        b = build_xwd(8, 4, grad, byte_order=1, name=b"other header\x00", ncolors=16)
        self.assertNotEqual(a, b)
        self.assertEqual(xfb.frame_sha256(a), xfb.frame_sha256(b))

    def test_rgb24_exact_pixels(self):
        buf = build_xwd(5, 3, grad, pad=8)
        rgb = xfb.rgb24(buf)
        self.assertEqual(len(rgb), 5 * 3 * 3)
        self.assertEqual(tuple(rgb[(2 * 5 + 4) * 3:(2 * 5 + 4) * 3 + 3]), grad(4, 2))

    def test_rgb24_rejects_forged_cached_header_before_output_allocation(self):
        raw = build_xwd(2, 2, grad)
        forged = xfb.validate_frame(raw)._replace(width=xfb.MAX_SIDE, height=xfb.MAX_SIDE)
        with patch.object(xfb, "bytearray", create=True) as output:
            with self.assertRaises(xfb.XwdError) as caught:
                xfb.rgb24(raw, forged)
            self.assertEqual(caught.exception.code, "E_XWD_GEOMETRY")
            output.assert_not_called()

    def test_rgb24_rechecks_full_frame_size_with_cached_header(self):
        raw = bytearray(build_xwd(2, 2, grad))
        struct.pack_into(">I", raw, 4 * 4, xfb.MAX_SIDE)
        struct.pack_into(">I", raw, 5 * 4, xfb.MAX_SIDE)
        struct.pack_into(">I", raw, 12 * 4, xfb.MAX_SIDE * 4)
        header = xfb.parse_header(raw)
        with patch.object(xfb, "bytearray", create=True) as output:
            with self.assertRaises(xfb.XwdError) as caught:
                xfb.rgb24(raw, header)
            self.assertEqual(caught.exception.code, "E_XWD_SIZE")
            output.assert_not_called()

    def test_size_must_be_exact(self):
        buf = build_xwd(4, 2, grad)
        for bad in (buf + b"\x00", buf[:-1]):
            with self.assertRaises(xfb.XwdError) as cm:
                xfb.validate_frame(bad)
            self.assertEqual(cm.exception.code, "E_XWD_SIZE")

    def test_rejects_non_raw_or_wrong_visual(self):
        for name, kw, code in BAD:
            kw = dict(kw)
            buf = build_xwd(kw.pop("w"), kw.pop("h"), grad, **kw)
            with self.assertRaises(xfb.XwdError, msg=name) as cm:
                xfb.validate_frame(buf)
            self.assertEqual(cm.exception.code, code, name)

    def test_geometry_check(self):
        buf = build_xwd(4, 2, grad)
        xfb.validate_frame(buf, 4, 2)
        with self.assertRaises(xfb.XwdError) as cm:
            xfb.validate_frame(buf, 1280, 720)
        self.assertEqual(cm.exception.code, "E_XWD_GEOMETRY")

    def test_consistent_read_skips_torn_reads(self):
        good = build_xwd(4, 2, grad)
        torn = good[:-4] + b"\xff\xff\xff\xff"
        seq = iter([torn, good, good])
        self.assertEqual(xfb.consistent_read(lambda: next(seq)), good)

    def test_consistent_read_gives_up(self):
        n = [0]

        def changing():
            n[0] += 1
            return bytes([n[0] % 256])
        with self.assertRaises(xfb.XwdError) as cm:
            xfb.consistent_read(changing)
        self.assertEqual(cm.exception.code, "E_TORN")
        self.assertEqual(n[0], xfb.TORN_TRIES)

    def test_vectors_file_matches(self):
        self.assertEqual(json.loads(VECTORS.read_text()), json.loads(json.dumps(build_vectors())))

    def test_vector_generation_is_repeatable(self):
        self.assertEqual(build_vectors(), build_vectors())

    def test_rejects_unknown_byte_orders(self):
        for field in (7, 9):
            for order in (2, 0xFFFFFFFF):
                with self.subTest(field=field, order=order):
                    raw = bytearray(build_xwd(2, 2, grad))
                    struct.pack_into(">I", raw, field * 4, order)
                    with self.assertRaises(xfb.XwdError) as caught:
                        xfb.validate_frame(raw)
                    self.assertEqual(caught.exception.code, "E_XWD_BYTE_ORDER")

    def test_bad_retry_counts_do_not_read_or_open(self):
        for tries in (True, False, -1, 0, 1, 2.0, "2", None):
            read = Mock()
            with self.subTest(tries=tries), patch.object(xfb.os, "open") as opened:
                for operation in (lambda: xfb.consistent_read(read, tries),
                                  lambda: xfb.read_screen("unused", tries)):
                    with self.assertRaises(xfb.XwdError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "E_XWD_TRIES")
                read.assert_not_called()
                opened.assert_not_called()

    def test_bad_limits_do_not_stat_or_open(self):
        for limit in (True, False, -1, 0, 2.0, "2", None):
            with self.subTest(limit=limit), patch.object(xfb.os, "open") as opened, \
                    patch.object(xfb.os, "fstat") as fstated:
                for operation in (lambda: xfb.read_screen_fd(123, max_bytes=limit),
                                  lambda: xfb.read_screen("unused", max_bytes=limit)):
                    with self.assertRaises(xfb.XwdError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "E_XWD_LIMIT")
                opened.assert_not_called()
                fstated.assert_not_called()

    def test_mutable_reader_cannot_reuse_one_object_as_consistency(self):
        shared = bytearray(b"a")

        def changing():
            shared[0] += 1
            return shared

        with self.assertRaises(xfb.XwdError) as caught:
            xfb.consistent_read(changing, tries=3)
        self.assertEqual(caught.exception.code, "E_TORN")
        self.assertEqual(shared, b"d")
        stable = xfb.consistent_read(lambda: shared, tries=2)
        self.assertIs(type(stable), bytes)
        shared[0] = 0
        self.assertEqual(stable, b"d")

    def test_generic_reader_limit_uses_memoryview_byte_count(self):
        import array
        view = memoryview(array.array("I", [0, 0]))
        with self.assertRaises(xfb.XwdError) as caught:
            xfb.consistent_read(lambda: view, max_bytes=view.nbytes - 1)
        self.assertEqual(caught.exception.code, "E_XWD_LIMIT")

    @staticmethod
    def source_stat(size, *, ino=7, mode=stat.S_IFREG | 0o600):
        return SimpleNamespace(st_dev=3, st_ino=ino, st_size=size, st_mode=mode)

    def test_fd_limit_checked_before_snapshot_and_equality_allowed(self):
        raw = build_xwd(2, 2, grad)
        with patch.object(xfb.os, "fstat", return_value=self.source_stat(len(raw))), \
                patch.object(xfb.os, "pread", return_value=raw) as pread:
            with self.assertRaises(xfb.XwdError) as caught:
                xfb.read_screen_fd(123, max_bytes=len(raw) - 1)
            self.assertEqual(caught.exception.code, "E_XWD_LIMIT")
            pread.assert_not_called()
            self.assertEqual(xfb.read_screen_fd(123, max_bytes=len(raw), width=2, height=2), raw)
            self.assertEqual(pread.call_args_list, [((123, len(raw), 0),), ((123, len(raw), 0),)])

    def test_fd_rejects_short_and_nonregular_before_snapshot(self):
        for source, code in ((self.source_stat(0), "E_XWD_SHORT"),
                             (self.source_stat(200, mode=stat.S_IFIFO), "E_XWD_SOURCE")):
            with self.subTest(code=code), patch.object(xfb.os, "fstat", return_value=source), \
                    patch.object(xfb.os, "pread") as pread:
                with self.assertRaises(xfb.XwdError) as caught:
                    xfb.read_screen_fd(123, max_bytes=200)
                self.assertEqual(caught.exception.code, code)
                pread.assert_not_called()

    def test_fd_identity_change_after_snapshot_is_rejected(self):
        raw = build_xwd(2, 2, grad)
        original = self.source_stat(len(raw))
        replaced = self.source_stat(len(raw), ino=8)
        with patch.object(xfb.os, "fstat", side_effect=[original, original, replaced]), \
                patch.object(xfb.os, "pread", return_value=raw) as pread:
            with self.assertRaises(xfb.XwdError) as caught:
                xfb.read_screen_fd(123, max_bytes=len(raw))
            self.assertEqual(caught.exception.code, "E_XWD_SOURCE")
            self.assertEqual(pread.call_count, 1)

    def test_fd_short_pread_is_rejected(self):
        raw = build_xwd(2, 2, grad)
        with patch.object(xfb.os, "fstat", return_value=self.source_stat(len(raw))), \
                patch.object(xfb.os, "pread", return_value=raw[:-1]):
            with self.assertRaises(xfb.XwdError) as caught:
                xfb.read_screen_fd(123, max_bytes=len(raw))
            self.assertEqual(caught.exception.code, "E_XWD_SIZE")

    def test_fd_growth_above_limit_checked_before_second_read(self):
        raw = build_xwd(2, 2, grad)
        original = self.source_stat(len(raw))
        grown = self.source_stat(len(raw) + 1)
        with patch.object(xfb.os, "fstat", side_effect=[original, original, original, grown]), \
                patch.object(xfb.os, "pread", return_value=raw) as pread:
            with self.assertRaises(xfb.XwdError) as caught:
                xfb.read_screen_fd(123, max_bytes=len(raw))
            self.assertEqual(caught.exception.code, "E_XWD_LIMIT")
            self.assertEqual(pread.call_count, 1)

    def test_fd_unstable_samples_use_exact_attempt_count(self):
        first = build_xwd(2, 2, grad)
        second = first[:-1] + b"\xff"
        with patch.object(xfb.os, "fstat", return_value=self.source_stat(len(first))), \
                patch.object(xfb.os, "pread", side_effect=[first, second, first]) as pread:
            with self.assertRaises(xfb.XwdError) as caught:
                xfb.read_screen_fd(123, max_bytes=len(first), tries=3)
            self.assertEqual(caught.exception.code, "E_TORN")
            self.assertEqual(pread.call_count, 3)

    def test_fd_cancellation_before_return_discards_valid_sample(self):
        raw = build_xwd(2, 2, grad)
        check = Mock(side_effect=[None, None, RuntimeError("cancelled")])
        with patch.object(xfb.os, "fstat", return_value=self.source_stat(len(raw))), \
                patch.object(xfb.os, "pread", return_value=raw) as pread:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                xfb.read_screen_fd(123, max_bytes=len(raw), before_read=check)
            self.assertEqual(pread.call_count, 2)
            self.assertEqual(check.call_count, 3)

    def test_source_errors_are_typed(self):
        with patch.object(xfb.os, "open", side_effect=FileNotFoundError(2, "missing")):
            with self.assertRaises(xfb.XwdError) as caught:
                xfb.read_screen("missing")
            self.assertEqual(caught.exception.code, "E_XWD_SOURCE")

    def test_fifo_path_opens_nonblocking_then_rejects_before_read(self):
        def open_fifo(path, flags):
            self.assertTrue(flags & os.O_NONBLOCK, "opening a FIFO without O_NONBLOCK could hang")
            return 123

        with patch.object(xfb.os, "open", side_effect=open_fifo), \
                patch.object(xfb.os, "fstat", return_value=self.source_stat(200, mode=stat.S_IFIFO)), \
                patch.object(xfb.os, "pread") as pread, patch.object(xfb.os, "close") as close:
            with self.assertRaises(xfb.XwdError) as caught:
                xfb.read_screen("fifo", max_bytes=200)
            self.assertEqual(caught.exception.code, "E_XWD_SOURCE")
            pread.assert_not_called()
            close.assert_called_once_with(123)

    def test_legacy_cli_argv_and_raw_stdout_preserved(self):
        raw = build_xwd(2, 2, grad)
        output = SimpleNamespace(buffer=io.BytesIO())
        with patch.object(xfb, "read_screen", return_value=raw) as reader, \
                patch.object(xfb.sys, "stdout", output), patch.object(xfb.sys, "stderr", io.StringIO()) as error:
            self.assertEqual(xfb.main(["read", "Xvfb_screen0"]), 0)
            reader.assert_called_once_with("Xvfb_screen0")
            self.assertEqual(output.buffer.getvalue(), raw)
            self.assertEqual(error.getvalue(), "")

    @unittest.skipUnless(os.environ.get("VADAPT_RUN_LIVE_XVFB") == "1" and
                         shutil.which("Xvfb") and shutil.which("xwd") and os.path.isdir("/dev/shm"),
                         "live Xvfb opt-in: VADAPT_RUN_LIVE_XVFB=1 plus Xvfb and xwd required")
    def test_real_xvfb_framebuffer_equals_getimage(self):
        from vadapt import ramws
        self.assertIn(ramws.is_ram_fs("/dev/shm"), ramws.RAM_FS)
        d = "/dev/shm/xfb_test_%d" % os.getpid()
        os.makedirs(d)
        disp = ":%d" % (90 + os.getpid() % 9)
        xv = subprocess.Popen(["Xvfb", disp, "-screen", "0", "320x200x24", "-fbdir", d, "-nocursor", "-nolisten", "tcp"],
                              stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.0)
            env = dict(os.environ, DISPLAY=disp)
            fb = xfb.read_screen(os.path.join(d, "Xvfb_screen0"))
            gi = subprocess.run(["xwd", "-root", "-silent"], env=env, capture_output=True, check=True).stdout
            self.assertEqual(xfb.frame_sha256(fb), xfb.frame_sha256(gi))
        finally:
            xv.terminate()
            xv.wait(5)
            for x in os.listdir(d) if os.path.isdir(d) else ():
                os.remove(os.path.join(d, x))
            if os.path.isdir(d):
                os.rmdir(d)


if __name__ == "__main__":
    unittest.main()
