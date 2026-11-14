"""Unit and CLI tests for B2 ve-zerocopy: raw RGB, ndarray, and ring inputs in vadapt.cu_describe."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib

import numpy as np

from vadapt import cu_describe, ramws, rtring
from visioncore.decode import png
from visioncore.errors import VqaError


def make_png_and_rgb(width=96, height=80, rect=None, color=b'\xff\xff\xff', rects=()):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))

    pixels = bytearray(width * height * 3)
    for idx in range(0, len(pixels), 3):
        pixels[idx] = (idx // 3) & 0x1f
        pixels[idx + 1] = ((idx // 3) * 3) & 0x1f
        pixels[idx + 2] = ((idx // 3) * 7) & 0x1f
    for x, y, w, h in ((rect,) if rect else ()) + tuple(rects):
        for row in range(y, y + h):
            offset = (row * width + x) * 3
            pixels[offset:offset + w * 3] = color * w
    raw_scanlines = b''.join(b'\0' + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    encoded = (b'\x89PNG\r\n\x1a\n'
               + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(raw_scanlines))
               + chunk(b'IEND', b''))
    rgb_bytes = bytes(pixels)
    rgb_array = np.frombuffer(rgb_bytes, dtype=np.uint8).copy().reshape(height, width, 3)
    return encoded, rgb_bytes, rgb_array


@contextlib.contextmanager
def ram_png(data):
    fd = os.memfd_create('cu-raw-test-png')
    try:
        os.write(fd, data)
        yield fd, '/proc/self/fd/%d' % fd
    finally:
        os.close(fd)


class TestCuDescribeRaw(unittest.TestCase):
    def setUp(self):
        self.assertIn(ramws.is_ram_fs('/dev/shm'), ramws.RAM_FS)
        self.shm_dir = Path(tempfile.mkdtemp(prefix='cu_raw_ut_', dir='/dev/shm'))
        self.cu_root = Path('/dev/shm/vqa-cu')
        self.cu_root.mkdir(exist_ok=True)
        self.cu_dir = Path(tempfile.mkdtemp(prefix='cu_raw_marks_', dir=self.cu_root))
        self.ws = ramws.open_run(
            'c2raw', 'r0', 4 << 20, tool='cu_describe_raw', root=self.shm_dir,
            statvfs=lambda _: types.SimpleNamespace(f_bavail=ramws.FREE_MIN + (16 << 20), f_frsize=1),
        )

    def tearDown(self):
        self.ws.close()
        shutil.rmtree(self.cu_dir, ignore_errors=True)
        shutil.rmtree(self.shm_dir, ignore_errors=True)

    def run_cli(self, *args, pass_fds=()):
        return subprocess.run(
            [sys.executable, '-m', 'vadapt.cu_describe', *args, '--json'],
            capture_output=True, text=True, pass_fds=pass_fds, timeout=30,
        )

    def test_raw_size_mismatch_refused(self):
        """M1 target: exact w*h*3 byte size is enforced; both oversized and undersized raw files are refused."""
        _, rgb_bytes, _ = make_png_and_rgb(96, 80, rect=(16, 16, 24, 16))
        exact_path = self.shm_dir / 'exact.rgb'
        exact_path.write_bytes(rgb_bytes)
        plus_one = self.shm_dir / 'plus_one.rgb'
        plus_one.write_bytes(rgb_bytes + b'\x00')
        rgba_like = self.shm_dir / 'rgba_like.rgb'
        rgba_like.write_bytes(rgb_bytes + (b'\xff' * (96 * 80)))
        minus_one = self.shm_dir / 'minus_one.rgb'
        minus_one.write_bytes(rgb_bytes[:-1])
        empty_raw = self.shm_dir / 'empty.rgb'
        empty_raw.write_bytes(b'')
        marks_sentinel = self.cu_dir / 'marks.png'
        marks_sentinel.write_bytes(b'sentinel-marks')

        # Positive control: exact byte count succeeds.
        ok = cu_describe.describe(frame_raw=f'{exact_path},96,80')
        self.assertEqual((ok['width'], ok['height']), (96, 80))

        # Mismatched file sizes or dimensions must fail with E_VC_FRAME_SIZE (and CLI rc 2).
        bad_specs = [
            f'{plus_one},96,80',
            f'{rgba_like},96,80',
            f'{minus_one},96,80',
            f'{empty_raw},96,80',
            f'{exact_path},48,80',
            f'{exact_path},96,40',
        ]
        for spec in bad_specs:
            with self.subTest(spec=spec):
                with self.assertRaises(VqaError) as ctx:
                    cu_describe.describe(frame_raw=spec, marks_out=str(marks_sentinel))
                self.assertEqual(ctx.exception.code, 'E_VC_FRAME_SIZE')
                self.assertEqual(ctx.exception.rc, 3)
                proc = self.run_cli('--frame-raw', spec, '--marks-out', str(marks_sentinel))
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, '')
                self.assertTrue(proc.stderr)

        # Also refuse prev_raw size mismatch and preserve caller-owned output.
        with self.assertRaises(VqaError) as ctx:
            cu_describe.describe(frame_raw=f'{exact_path},96,80', prev_raw=f'{plus_one},96,80',
                                 marks_out=str(marks_sentinel))
        self.assertEqual(ctx.exception.code, 'E_VC_FRAME_SIZE')
        self.assertEqual(marks_sentinel.read_bytes(), b'sentinel-marks')

    def test_raw_escape_refused(self):
        """M2 target: /dev/shm confinement refuses '..', '.', relative paths, /tmp paths, and symlinks."""
        _, rgb_bytes, rgb_array = make_png_and_rgb(64, 48)
        sub = self.shm_dir / 'sub'
        sub.mkdir()
        valid_raw = self.shm_dir / 'valid.rgb'
        valid_raw.write_bytes(rgb_bytes)

        writer = rtring.RingWriter(self.ws, 2, 64, 48, rel='ring_ok/frames.rgb24')
        self.addCleanup(writer.close)
        writer.publish(rgb_array, 0)

        with tempfile.TemporaryDirectory(prefix='cu_raw_outside_') as tmp_outside:
            outside_raw = Path(tmp_outside) / 'outside.rgb'
            outside_raw.write_bytes(rgb_bytes)
            symlink_to_outside = self.shm_dir / 'link_outside.rgb'
            symlink_to_outside.symlink_to(outside_raw)
            symlink_to_inside = self.shm_dir / 'link_inside.rgb'
            symlink_to_inside.symlink_to(valid_raw)
            symlink_dir = self.shm_dir / 'link_dir'
            symlink_dir.symlink_to(tmp_outside, target_is_directory=True)
            ring_link = self.shm_dir / 'link_ring.rgb24'
            ring_link.symlink_to(writer.path)

            escape_raw_specs = [
                f'{sub}/../valid.rgb,64,48',
                f'/dev/shm/../dev/shm/{self.shm_dir.name}/valid.rgb,64,48',
                f'/dev/shm/./{self.shm_dir.name}/valid.rgb,64,48',
                f'/dev/shm/../tmp/{outside_raw.name},64,48',
                f'{outside_raw},64,48',
                'relative/valid.rgb,64,48',
                f'{symlink_to_outside},64,48',
                f'{symlink_to_inside},64,48',
                f'{symlink_dir}/outside.rgb,64,48',
            ]
            for spec in escape_raw_specs:
                with self.subTest(raw_spec=spec):
                    with self.assertRaises(VqaError) as ctx:
                        cu_describe.describe(frame_raw=spec)
                    self.assertEqual(ctx.exception.code, 'E_IO')
                    proc = self.run_cli('--frame-raw', spec)
                    self.assertEqual(proc.returncode, 2)
                    self.assertEqual(proc.stdout, '')

            escape_ring_specs = [
                f'{ writer.path.parent }/../{ writer.path.parent.name }/frames.rgb24,0',
                f'/tmp/not_shm.rgb24,0',
                f'{ring_link},0',
                'relative_ring.rgb24,0',
            ]
            for spec in escape_ring_specs:
                with self.subTest(ring_spec=spec):
                    with self.assertRaises(VqaError) as ctx:
                        cu_describe.describe(ring=spec)
                    self.assertEqual(ctx.exception.code, 'E_IO')
                    proc = self.run_cli('--ring', spec)
                    self.assertEqual(proc.returncode, 2)
                    self.assertEqual(proc.stdout, '')

    def test_raw_digest_matches_png_digest(self):
        """M3 target: frame_sha256 is computed over raw RGB24 bytes and matches across PNG, raw, ndarray, and ring."""
        png_data, rgb_bytes, rgb_array = make_png_and_rgb(96, 80, rect=(20, 16, 32, 24))
        prev_png, prev_rgb_bytes, prev_array = make_png_and_rgb(96, 80)
        expected_sha = hashlib.sha256(rgb_bytes).hexdigest()
        png_file_sha = hashlib.sha256(png_data).hexdigest()
        self.assertNotEqual(expected_sha, png_file_sha)

        raw_path = self.shm_dir / 'frame.rgb'
        raw_path.write_bytes(rgb_bytes)
        prev_raw_path = self.shm_dir / 'prev.rgb'
        prev_raw_path.write_bytes(prev_rgb_bytes)

        writer = rtring.RingWriter(self.ws, 4, 96, 80, rel='ring_digest/frames.rgb24')
        self.addCleanup(writer.close)
        writer.publish(prev_array, 0)
        writer.publish(rgb_array, 1)

        with ram_png(png_data) as (fd_cur, png_path), ram_png(prev_png) as (fd_prev, prev_png_path):
            from_png = cu_describe.describe(png_path, prev_png_path, masks=((20, 16, 32, 24),))
            from_arr = cu_describe.describe(rgb_array, prev_array, masks=((20, 16, 32, 24),))
            from_raw = cu_describe.describe(
                frame_raw=f'{raw_path},96,80', prev_raw=f'{prev_raw_path},96,80',
                masks=((20, 16, 32, 24),),
            )
            from_ring = cu_describe.describe(
                ring=f'{writer.path},1', prev_ring=f'{writer.path},0',
                masks=((20, 16, 32, 24),),
            )
            cli_png = json.loads(self.run_cli('--frame', png_path, '--prev', prev_png_path, pass_fds=(fd_cur, fd_prev)).stdout)
            cli_raw = json.loads(self.run_cli('--frame-raw', f'{raw_path},96,80', '--prev-raw', f'{prev_raw_path},96,80').stdout)
            cli_ring = json.loads(self.run_cli('--ring', f'{writer.path},1', '--prev-ring', f'{writer.path},0').stdout)

        for res in (from_png, from_arr, from_raw, from_ring, cli_png, cli_raw, cli_ring):
            self.assertEqual(res['frame_sha256'], expected_sha)
            self.assertNotEqual(res['frame_sha256'], png_file_sha)

    def test_ndarray_and_raw_cli_parity(self):
        """M4 target: ndarray and --frame-raw/--prev-raw match PNG schema, regions, crops, s4_attrs, and marks."""
        prev_png, prev_bytes, prev_arr = make_png_and_rgb(
            160, 96, rects=((12, 20, 40, 24), (60, 20, 40, 24))
        )
        cur_png, cur_bytes, cur_arr = make_png_and_rgb(
            160, 96, rects=((12, 20, 40, 24), (60, 20, 40, 24), (112, 48, 32, 20))
        )
        raw_cur = self.shm_dir / 'cur.rgb'
        raw_cur.write_bytes(cur_bytes)
        raw_prev = self.shm_dir / 'prev.rgb'
        raw_prev.write_bytes(prev_bytes)

        marks_png_out = self.cu_dir / 'marks_png.png'
        marks_raw_out = self.cu_dir / 'marks_raw.png'
        marks_arr_out = self.cu_dir / 'marks_arr.png'
        crops_png_dir = self.cu_dir / 'crops_png'
        crops_raw_dir = self.cu_dir / 'crops_raw'
        crops_arr_dir = self.cu_dir / 'crops_arr'
        for d in (crops_png_dir, crops_raw_dir, crops_arr_dir):
            d.mkdir()

        # Non-contiguous + read-only view should be handled cleanly without mutating caller array.
        padded = np.zeros((96, 320, 3), dtype=np.uint8)
        padded[:, ::2, :] = cur_arr
        noncontig_view = padded[:, ::2, :]
        noncontig_view.setflags(write=False)

        with ram_png(cur_png) as (fd_c, p_cur), ram_png(prev_png) as (fd_p, p_prev):
            ref = json.loads(self.run_cli(
                '--frame', p_cur, '--prev', p_prev,
                '--s4-attrs', '--marks-extra',
                '--marks-out', str(marks_png_out), '--crop-dir', str(crops_png_dir),
                pass_fds=(fd_c, fd_p),
            ).stdout)
            got_raw = json.loads(self.run_cli(
                '--frame-raw', f'{raw_cur},160,96', '--prev-raw', f'{raw_prev},160,96',
                '--s4-attrs', '--marks-extra',
                '--marks-out', str(marks_raw_out), '--crop-dir', str(crops_raw_dir),
            ).stdout)
            got_arr = cu_describe.describe(
                noncontig_view, prev_arr,
                marks_out=str(marks_arr_out), crop_dir=str(crops_arr_dir),
                s4_attrs=True, marks_extra=True,
            )

        for item in (ref, got_raw, got_arr):
            self.assertIsInstance(item.pop('timing_us'), int)
            item.pop('marks_png')
            item.pop('crop_dir')
            item['crop_pngs'] = [Path(p).name for p in item['crop_pngs']]

        self.assertTrue(ref['changed'])
        self.assertGreaterEqual(len(ref['changed_regions']), 1)
        self.assertEqual(got_raw, ref)
        self.assertEqual(got_arr, ref)
        self.assertEqual(marks_raw_out.read_bytes(), marks_png_out.read_bytes())
        self.assertEqual(marks_arr_out.read_bytes(), marks_png_out.read_bytes())
        for name in ref['crop_pngs']:
            np.testing.assert_array_equal(png(str(crops_raw_dir / name)), png(str(crops_png_dir / name)))
            np.testing.assert_array_equal(png(str(crops_arr_dir / name)), png(str(crops_png_dir / name)))

        # Invalid ndarray shapes/dtypes must be refused with E_VC_FRAME_SHAPE.
        for bad_arr in (
            np.zeros((80, 96), dtype=np.uint8),
            np.zeros((80, 96, 4), dtype=np.uint8),
            np.zeros((80, 96, 3), dtype=np.float32),
            np.zeros((0, 96, 3), dtype=np.uint8),
        ):
            with self.subTest(shape=bad_arr.shape, dtype=str(bad_arr.dtype)):
                with self.assertRaises(VqaError) as ctx:
                    cu_describe.describe(bad_arr)
                self.assertEqual(ctx.exception.code, 'E_VC_FRAME_SHAPE')

    def test_ring_input_and_stale_refused(self):
        """M5 target: --ring/<path>,<index> reads seqlock slots and refuses lapped, absent, in-flight, or corrupt slots."""
        f0_png, _, f0_arr = make_png_and_rgb(96, 80)
        f1_png, _, f1_arr = make_png_and_rgb(96, 80, rect=(24, 24, 24, 16))
        f2_png, _, f2_arr = make_png_and_rgb(96, 80, rect=(48, 32, 16, 16))

        writer = rtring.RingWriter(self.ws, 2, 96, 80, rel='ring_live/frames.rgb24')
        self.addCleanup(writer.close)
        writer.publish(f0_arr, 0)
        writer.publish(f1_arr, 1)

        with ram_png(f0_png) as (_, p0), ram_png(f1_png) as (_, p1):
            expected = cu_describe.describe(p1, p0, s4_attrs=True)
        got_cli = json.loads(self.run_cli(
            '--ring', f'{writer.path},1', '--prev-ring', f'{writer.path},0', '--s4-attrs'
        ).stdout)
        expected.pop('timing_us')
        got_cli.pop('timing_us')
        self.assertEqual(got_cli, expected)

        # Direct rtring.Frame zero-copy view also works while valid, and fails once overwritten.
        reader = rtring.RingReader(writer.path, root='/dev/shm')
        self.addCleanup(reader.close)
        frame0 = reader.acquire(0)
        frame1 = reader.acquire(1)
        try:
            got_view = cu_describe.describe(frame1, frame0, s4_attrs=True)
            got_view.pop('timing_us')
            self.assertEqual(got_view, expected)

            # Publishing render_index=2 in a 2-slot ring laps slot 0 (render_index 0).
            writer.publish(f2_arr, 2)
            self.assertFalse(frame0.valid())
            with self.assertRaises(VqaError) as ctx:
                cu_describe.describe(frame0)
            self.assertEqual(ctx.exception.code, 'E_STALE')
        finally:
            del frame0
            del frame1

        # Lapped index 0 and absent index 99 are refused with E_STALE (rc 4) / CLI rc 2.
        for stale_idx in (0, 99):
            with self.subTest(stale_idx=stale_idx):
                with self.assertRaises(VqaError) as ctx:
                    cu_describe.describe(ring=f'{writer.path},{stale_idx}')
                self.assertEqual(ctx.exception.code, 'E_STALE')
                self.assertEqual(ctx.exception.rc, 4)
                proc = self.run_cli('--ring', f'{writer.path},{stale_idx}')
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, '')

        # In-flight (odd seq) slot is refused with E_STALE.
        inflight_view = writer.begin(3)
        try:
            with self.assertRaises(VqaError) as ctx:
                cu_describe.describe(ring=f'{writer.path},3')
            self.assertEqual(ctx.exception.code, 'E_STALE')
            inflight_view[...] = f1_arr
        finally:
            del inflight_view
            writer.commit()

        # Corrupted payload CRC32 is refused with E_STALE.
        writer._slot_view(3 % writer.slots)[0, 0, 0] ^= 0xFF
        with self.assertRaises(VqaError) as ctx:
            cu_describe.describe(ring=f'{writer.path},3')
        self.assertEqual(ctx.exception.code, 'E_STALE')


if __name__ == '__main__':
    unittest.main()
