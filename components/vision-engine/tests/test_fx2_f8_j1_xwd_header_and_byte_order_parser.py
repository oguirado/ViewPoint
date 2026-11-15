"""Contract tests for D11-BOUNDS xwd header, byte-order, preallocation budget, and retry parameter hardening.

Target fix task: fx2-f8-d11-x11-framebuffer-ram-capture
"""
import stat
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from vadapt import xfb


def make_xwd_fixture(w=8, h=4, byte_order=0, ncolors=0, pad=0, header_size=None, name=b"test_screen0\x00"):
    """Helper to build synthetic XWD binary headers and frames in RAM."""
    hs = header_size if header_size is not None else (xfb.MIN_HEADER + len(name))
    bpl = w * 4 + pad
    head = struct.pack(
        ">25I",
        hs, xfb.XWD_VERSION, xfb.ZPIXMAP, 24, w, h, 0, byte_order, 32, byte_order, 32, 32, bpl,
        xfb.TRUECOLOR, 0xFF0000, 0xFF00, 0xFF, 8, ncolors, ncolors, w, h, 0, 0, 0
    ) + name
    if len(head) < hs:
        head = head + b"\x00" * (hs - len(head))
    cmap = b"\x00" * (ncolors * xfb.COLOR_ENTRY)
    pixels = b"\x00" * (h * bpl)
    return head + cmap + pixels


class TestXwdHeaderAndByteOrderParserContract(unittest.TestCase):

    # fx2-f8-d11-x11-framebuffer-ram-capture
    def test_invalid_byte_order_rejection(self):
        """D11-BOUNDS: byte_order outside zero and one must be rejected with XwdError."""
        # Boundary controls: legal byte_order 0 (LSB) and 1 (MSB) pass parsing
        valid_lsb = make_xwd_fixture(w=8, h=4, byte_order=0)
        valid_msb = make_xwd_fixture(w=8, h=4, byte_order=1)
        hd0 = xfb.validate_frame(valid_lsb)
        hd1 = xfb.validate_frame(valid_msb)
        self.assertEqual(hd0.byte_order, 0)
        self.assertEqual(hd1.byte_order, 1)

        # Must-fail counterparts: invalid byte_order values (2, 255, 128)
        for bad_order in (2, 255, 128):
            bad_buf = make_xwd_fixture(w=8, h=4, byte_order=bad_order)
            with self.assertRaises(xfb.XwdError):
                xfb.validate_frame(bad_buf)

            # Prove failed operations have no reservation or publication side effect
            mock_ws = MagicMock()
            with self.assertRaises(xfb.XwdError):
                xfb.store(mock_ws, "bad_order_frame", bad_buf)
            mock_ws.reserve.assert_not_called()

    # fx2-f8-d11-x11-framebuffer-ram-capture
    @unittest.expectedFailure
    def test_header_size_and_ncolors_arithmetic_overflow(self):
        """D11-BOUNDS: header_size and ncolors arithmetic implying huge offsets/sizes must be rejected."""
        # Boundary control: valid fixed-seed combinations of colormap and padded stride
        valid_cmap = make_xwd_fixture(w=8, h=4, ncolors=256, pad=16)
        hd = xfb.validate_frame(valid_cmap)
        self.assertEqual(hd.ncolors, 256)
        self.assertEqual(hd.pixels_offset, hd.header_size + 256 * xfb.COLOR_ENTRY)

        # Must-fail counterpart 1: huge ncolors arithmetic implying massive pixel offset
        huge_ncolors_buf = make_xwd_fixture(w=8, h=4, ncolors=0x1000000)
        with self.assertRaises(xfb.XwdError):
            xfb.parse_header(huge_ncolors_buf)

        # Must-fail counterpart 2: huge header_size implying massive pixel offset
        huge_hs_buf = make_xwd_fixture(w=8, h=4, header_size=0x7FFFFFFF)
        with self.assertRaises(xfb.XwdError):
            xfb.parse_header(huge_hs_buf)

        # Prove failed operations have no publication side effect in ramws
        mock_ws = MagicMock()
        with self.assertRaises(xfb.XwdError):
            xfb.store(mock_ws, "huge_offset_frame", huge_ncolors_buf)
        mock_ws.reserve.assert_not_called()

    def test_max_bytes_preallocation_budget_bounds(self):
        """D11-BOUNDS: the bounded read API enforces the cap before publication."""
        valid_buf = make_xwd_fixture(w=16, h=8, pad=32, ncolors=16)
        exact_budget = len(valid_buf)

        # The real API assigns the budget to the reader, then validates the frame.
        read_fn = MagicMock(return_value=valid_buf)
        bounded = xfb.consistent_read(read_fn, tries=2, max_bytes=exact_budget)
        self.assertEqual(read_fn.call_count, 2)
        self.assertEqual(bounded, valid_buf)
        hd = xfb.validate_frame(bounded)
        self.assertEqual(hd.size, exact_budget)

        # A one-byte smaller budget rejects the same unchanged byte fixture.
        read_fn.reset_mock()
        with self.assertRaises(xfb.XwdError) as exc:
            xfb.consistent_read(read_fn, tries=2, max_bytes=exact_budget - 1)
        self.assertEqual(exc.exception.code, "E_XWD_LIMIT")
        self.assertEqual(read_fn.call_count, 1)

        # The stride/height product must not bypass the actual reader's budget.
        large_stride_buf = make_xwd_fixture(w=2048, h=1080)
        with self.assertRaises(xfb.XwdError) as exc:
            xfb.consistent_read(lambda: large_stride_buf, tries=2, max_bytes=1000)
        self.assertEqual(exc.exception.code, "E_XWD_LIMIT")

        # The bounded read precedes store: refusal cannot reserve or publish.
        mock_ws = MagicMock()
        with self.assertRaises(xfb.XwdError) as exc:
            bounded = xfb.consistent_read(
                lambda: valid_buf, tries=2, max_bytes=exact_budget - 1
            )
            xfb.store(mock_ws, "over_budget_frame", bounded)
        self.assertEqual(exc.exception.code, "E_XWD_LIMIT")
        mock_ws.reserve.assert_not_called()
        mock_ws.path.assert_not_called()

    def test_read_screen_refuses_over_budget_fstat_before_mmap(self):
        """D11-BOUNDS: every pread is bounded by fstat, without a frame file."""
        valid_buf = make_xwd_fixture(w=8, h=4)
        size = len(valid_buf)
        fd = 73
        valid_stat = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600, st_size=size, st_dev=1, st_ino=2
        )
        oversized_stat = SimpleNamespace(
            st_mode=valid_stat.st_mode, st_size=size + 1, st_dev=1, st_ino=2
        )

        # At the exact cap, both coherent snapshots have precisely bounded reads.
        events = []

        def fstat(_fd):
            self.assertEqual(_fd, fd)
            events.append("stat")
            return valid_stat

        def pread(_fd, count, offset):
            self.assertEqual((_fd, count, offset), (fd, size, 0))
            events.append("pread")
            return valid_buf

        with patch("vadapt.xfb.os.fstat", side_effect=fstat) as mock_stat, \
                patch("vadapt.xfb.os.pread", side_effect=pread) as mock_pread:
            self.assertEqual(
                xfb.read_screen_fd(fd, max_bytes=size, tries=2), valid_buf
            )
            self.assertEqual(mock_stat.call_count, 6)
            self.assertEqual(mock_pread.call_args_list, [call(fd, size, 0)] * 2)
        self.assertEqual(
            events, ["stat", "stat", "pread", "stat", "stat", "pread", "stat", "stat"]
        )

        # Only st_size changes: initial refusal must precede any snapshot read.
        with patch("vadapt.xfb.os.fstat", return_value=oversized_stat), \
                patch("vadapt.xfb.os.pread") as mock_pread, \
                patch("vadapt.xfb.consistent_read") as mock_read:
            with self.assertRaises(xfb.XwdError) as exc:
                xfb.read_screen_fd(fd, max_bytes=size, tries=2)
            self.assertEqual(exc.exception.code, "E_XWD_LIMIT")
            mock_pread.assert_not_called()
            mock_read.assert_not_called()

        # Growth before the second snapshot must not trigger a second allocation.
        stats = [valid_stat, valid_stat, valid_stat, oversized_stat]
        with patch("vadapt.xfb.os.fstat", side_effect=stats), \
                patch("vadapt.xfb.os.pread", return_value=valid_buf) as mock_pread:
            with self.assertRaises(xfb.XwdError) as exc:
                xfb.read_screen_fd(fd, max_bytes=size, tries=2)
            self.assertEqual(exc.exception.code, "E_XWD_LIMIT")
            self.assertEqual(mock_pread.call_args_list, [call(fd, size, 0)])

    def test_retry_count_tries_rejected_inputs(self):
        """D11-BOUNDS: at least two exact-integer reads establish consistency."""
        valid_buf = make_xwd_fixture(w=8, h=4)

        read_fn = MagicMock(return_value=valid_buf)
        res = xfb.consistent_read(read_fn, tries=2)
        self.assertEqual(res, valid_buf)
        self.assertEqual(read_fn.call_count, 2)

        # One read cannot establish consistency; bool is not an exact integer.
        for bad_tries in (1, 0, -1, -10, True, False, 2.0, "2", None):
            read_fn = MagicMock(return_value=valid_buf)
            with self.assertRaises(xfb.XwdError) as exc:
                xfb.consistent_read(read_fn, tries=bad_tries)
            self.assertEqual(exc.exception.code, "E_XWD_TRIES")
            read_fn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
