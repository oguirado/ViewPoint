"""C2 end-to-end CLI checks with stdlib PNGs held in anonymous RAM files."""
import contextlib
import hashlib
import json
import os
import struct
import subprocess
import tempfile
from pathlib import Path
from unittest import mock
import sys
import unittest
import zlib


def png_bytes(width=96, height=80, rect=None, color=b'\xff\xff\xff', rects=()):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))
    pixels = bytearray(width * height * 3)
    for x, y, w, h in ((rect,) if rect else ()) + tuple(rects):
        for row in range(y, y + h):
            offset = (row * width + x) * 3
            pixels[offset:offset+w*3] = color * w
    raw = b''.join(b'\0' + pixels[y*width*3:(y+1)*width*3] for y in range(height))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')), bytes(pixels)


@contextlib.contextmanager
def ram_png(data):
    fd = os.memfd_create('cu-test-png')
    try:
        os.write(fd, data)
        yield fd, '/proc/self/fd/%d' % fd
    finally:
        os.close(fd)


class DescribeCLI(unittest.TestCase):
    def run_cli(self, frame=None, previous=None, extra=()):
        if frame is None:
            frame = png_bytes()[0]
        with contextlib.ExitStack() as stack:
            fd, path = stack.enter_context(ram_png(frame))
            args = [sys.executable, '-m', 'vadapt.cu_describe', '--frame', path, '--json']
            fds = [fd]
            if previous is not None:
                prev_fd, prev_path = stack.enter_context(ram_png(previous))
                fds.append(prev_fd)
                args += ['--prev', prev_path]
            return subprocess.run(args + list(extra), capture_output=True, text=True,
                                  pass_fds=fds, timeout=30)

    def success(self, **kwargs):
        result = self.run_cli(**kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(len(result.stdout.splitlines()), 1)
        return json.loads(result.stdout)

    def test_identical(self):
        result = self.success(previous=png_bytes()[0])
        self.assertIs(result['changed'], False)
        self.assertEqual(result['changed_regions'], [])
        self.assertEqual(result['crops'], [])

    def test_rectangle(self):
        result = self.success(frame=png_bytes(rect=(24, 24, 24, 16))[0],
                              previous=png_bytes()[0])
        self.assertIs(result['changed'], True)
        self.assertEqual(len(result['changed_regions']), 1)
        r = result['changed_regions'][0]
        self.assertLessEqual(r['x'], 24)
        self.assertLessEqual(r['y'], 24)
        self.assertGreaterEqual(r['x'] + r['w'], 48)
        self.assertGreaterEqual(r['y'] + r['h'], 40)
        self.assertEqual(result['crops'][0]['reason'], 'changed')

    def test_mask(self):
        result = self.success(frame=png_bytes(rect=(24, 24, 24, 16))[0],
                              previous=png_bytes()[0], extra=('--mask', '24,24,24,16'))
        self.assertIs(result['changed'], False)
        self.assertEqual(result['changed_regions'], [])
        self.assertNotEqual(result['frame_sha256'], hashlib.sha256(png_bytes()[1]).hexdigest())

    def test_partial_and_repeated_masks(self):
        frame = png_bytes(rect=(24, 24, 24, 16))[0]
        previous = png_bytes()[0]
        result = self.success(frame=frame, previous=previous, extra=('--mask', '24,24,12,16'))
        self.assertTrue(result['changed'])
        result = self.success(frame=frame, previous=previous,
                              extra=('--mask', '24,24,12,16', '--mask', '36,24,12,16', '999,999,8,8'))
        self.assertFalse(result['changed'])

    def test_small_exact_change(self):
        result = self.success(frame=png_bytes(rect=(25, 25, 1, 1), color=b'\x01\0\0')[0],
                              previous=png_bytes()[0])
        self.assertTrue(result['changed'])

    def test_deterministic(self):
        kwargs = dict(frame=png_bytes(rect=(24, 24, 24, 16))[0], previous=png_bytes()[0])
        first, second = self.success(**kwargs), self.success(**kwargs)
        first.pop('timing_us')
        second.pop('timing_us')
        self.assertEqual(first, second)

    def test_bad_input(self):
        for kwargs in (dict(frame=b'not png'), dict(frame=png_bytes()[0][:-12]),
                       dict(previous=png_bytes(width=48)[0]),
                       dict(extra=('--mask', '0,0,0,1')),
                       dict(extra=('--mask', 'oops')),
                       dict(extra=('--unknown',))):
            with self.subTest(kwargs=kwargs):
                result = self.run_cli(**kwargs)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertTrue(result.stderr)

    def test_missing_input(self):
        result = subprocess.run([sys.executable, '-m', 'vadapt.cu_describe',
                                 '--frame', '/proc/self/fd/999999', '--json'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_schema_and_no_previous(self):
        result = self.success()
        self.assertEqual(set(result), {'schema', 'frame_sha256', 'width', 'height',
                                      'changed', 'changed_regions', 'crops', 'timing_us'})
        self.assertEqual(result['schema'], 've.cu_describe/v1')
        self.assertEqual(result['frame_sha256'], hashlib.sha256(png_bytes()[1]).hexdigest())
        self.assertEqual((result['width'], result['height']), (96, 80))
        self.assertIsNone(result['changed'])
        self.assertIsInstance(result['timing_us'], int)
        self.assertGreaterEqual(result['timing_us'], 0)

    def test_many_regions_are_bounded(self):
        rects = [(x, y, 8, 8) for y in range(16, 400, 80) for x in range(16, 400, 80)]
        result = self.success(frame=png_bytes(512, 512, rects=rects)[0],
                              previous=png_bytes(512, 512)[0])
        self.assertTrue(result['changed'])
        self.assertEqual(len(result['changed_regions']), 16)
        self.assertEqual(len(result['crops']), 8)
        keys = [(-r['score'], r['y'], r['x'], r['h'], r['w'])
                for r in result['changed_regions']]
        self.assertEqual(keys, sorted(keys))

    def test_region_schema_limits_order(self):
        result = self.success(frame=png_bytes(rect=(24, 24, 24, 16))[0], previous=png_bytes()[0])
        self.assertLessEqual(len(result['changed_regions']), 16)
        self.assertLessEqual(len(result['crops']), 8)
        for r in result['changed_regions']:
            self.assertEqual(set(r), {'x', 'y', 'w', 'h', 'score'})
            self.assertIsInstance(r['score'], float)
            for k in ('x', 'y', 'w', 'h'):
                self.assertIsInstance(r[k], int)
        for r in result['crops']:
            self.assertEqual(set(r), {'x', 'y', 'w', 'h', 'reason'})
        scores = [r['score'] for r in result['changed_regions']]
        self.assertEqual(scores, sorted(scores, reverse=True))


class SetOfMarks(unittest.TestCase):
    run_cli = DescribeCLI.run_cli
    success = DescribeCLI.success
    """C2.1 grounding candidates and bitmap output, all images stay in RAM."""

    def setUp(self):
        root = Path('/dev/shm/vqa-cu')
        root.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix='ve-test-', dir=root)
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / 'marks.png'

    def marked(self, frame):
        return self.success(frame=frame, extra=('--marks-out', str(self.output)))

    def assert_covers(self, marks, rect):
        x, y, w, h = rect
        matches = [m for m in marks if m['x'] <= x and m['y'] <= y
                   and m['x'] + m['w'] >= x+w and m['y'] + m['h'] >= y+h]
        self.assertEqual(len(matches), 1, (rect, marks))
        return matches[0]

    def test_buttons_separate_and_cover(self):
        rects = [(10, 20, 40, 24), (54, 20, 44, 24), (102, 20, 40, 24)]
        data = png_bytes(160, 80, rects=rects)[0]
        result = self.marked(data)
        marks = result['marks']
        ids = [self.assert_covers(marks, rect)['id'] for rect in rects]
        self.assertEqual(len(set(ids)), len(rects))
        self.assertEqual([m['id'] for m in marks], list(range(1, len(marks)+1)))
        self.assertEqual(result['marks_png'], str(self.output))
        for mark in marks:
            self.assertEqual(set(mark), {'id', 'x', 'y', 'w', 'h', 'kind', 'score'})
            self.assertIn(mark['kind'], {'button', 'tab', 'field', 'row', 'icon', 'text', 'unknown'})
            self.assertIsInstance(mark['score'], float)
            self.assertGreaterEqual(mark['score'], 0)
            self.assertLessEqual(mark['score'], 1)
        from visioncore.decode import png
        annotated = png(str(self.output))
        raw = png(data)
        self.assertEqual(annotated.shape, raw.shape)
        self.assertNotEqual(annotated.tobytes(), raw.tobytes())
        # Label 1 is white-on-black and the outline has a yellow inner stroke.
        mark = marks[0]
        self.assertEqual(annotated[mark['y'], mark['x']].tolist(), [0, 0, 0])
        self.assertEqual(annotated[mark['y']+1, mark['x']+2].tolist(), [255, 255, 255])
        self.assertEqual(annotated[mark['y']+8, mark['x']+1].tolist(), [255, 255, 0])

    def test_marks_deterministic(self):
        frame = png_bytes(160, 80, rects=((12, 20, 40, 24), (56, 20, 40, 24)))[0]
        first = self.marked(frame)
        encoded = self.output.read_bytes()
        second = self.marked(frame)
        self.assertEqual(encoded, self.output.read_bytes())
        first.pop('timing_us')
        second.pop('timing_us')
        self.assertEqual(first, second)

    def test_no_flag_preserves_c2(self):
        from vadapt.cu_describe import describe
        frame = png_bytes(rect=(24, 24, 24, 16))[0]
        with ram_png(frame) as (_, path), mock.patch(
                'vadapt.cu_describe.time.perf_counter_ns', return_value=123):
            plain = describe(path)
            explicit = describe(path, marks_out=None)
            marked = describe(path, marks_out=str(self.output))
        self.assertEqual(plain, explicit)
        marked.pop('marks')
        marked.pop('marks_png')
        self.assertEqual(plain, marked)
        self.assertNotIn('marks', self.success(frame=frame))

    def test_real_top_bar_or_replica(self):
        index = Path('/dev/shm/vqa-ramws/opensuperficies/os-cu-0/recorded_holdings_v1.json')
        frame = None
        if index.is_file():
            for record in json.loads(index.read_text())['frames']:
                path = Path(record['path'])
                if path.is_file():
                    frame = path.read_bytes()
                    break
        if frame is None:
            # Replica has the same viewport and neighbouring control geometry.
            frame = png_bytes(1280, 900, rects=((9, 63, 74, 21), (87, 63, 42, 21),
                              (133, 63, 51, 21), (188, 63, 56, 21),
                              (248, 63, 48, 21), (300, 63, 55, 21), (359, 63, 68, 21)))[0]
        result = self.marked(frame)
        band = [m for m in result['marks'] if m['y'] <= 80 < m['y']+m['h']
                and m['y'] <= 85 and m['y']+m['h'] >= 70]
        self.assertGreaterEqual(len(band), 4, result['marks'])
        self.assertLessEqual(len(result['marks']), 24)
        # Each actual neighbouring navigation center gets its own mark.
        centers = [108, 158, 216, 272]
        grounded = [[m['id'] for m in band if m['x'] <= x < m['x']+m['w']]
                    for x in centers]
        self.assertTrue(all(len(ids) == 1 for ids in grounded), grounded)
        self.assertEqual(len({ids[0] for ids in grounded}), len(centers))

    def test_top_bar_replica_when_recording_absent(self):
        with mock.patch('pathlib.Path.is_file', return_value=False):
            self.test_real_top_bar_or_replica()

    def test_bound_empty_and_off_ram_refusal(self):
        self.assertEqual(self.marked(png_bytes()[0])['marks'], [])
        rects = [(x, y, 20, 14) for y in range(10, 200, 25) for x in range(10, 300, 30)]
        self.assertEqual(len(self.marked(png_bytes(320, 240, rects=rects)[0])['marks']), 24)
        bad = self.run_cli(extra=('--marks-out', '/tmp/cu-forbidden.png'))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, '')
        # Resolving symlinks must not permit a RAM-looking path to escape.
        link = self.output.parent / 'escape'
        link.symlink_to('/tmp', target_is_directory=True)
        bad = self.run_cli(extra=('--marks-out', str(link / 'cu-forbidden.png')))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, '')

    def test_bad_input_preserves_output(self):
        self.output.write_bytes(b'caller-owned sentinel')
        bad = self.run_cli(frame=b'not a png', extra=('--marks-out', str(self.output)))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(self.output.read_bytes(), b'caller-owned sentinel')
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])


    def test_schema_and_determinism_on_real_capture(self):  # @anchor:va.cu_tests.test_schema_and_determinism_on_real_capture
        from vadapt.cu_describe import describe
        frame = png_bytes(1280, 900, rects=((9, 63, 74, 21), (87, 63, 42, 21),
                          (133, 63, 51, 21), (188, 63, 56, 21), (248, 63, 48, 21)))[0]
        previous = png_bytes(1280, 900)[0]
        keys = {'schema', 'frame_sha256', 'width', 'height', 'changed',
                'changed_regions', 'crops', 'timing_us'}
        plain = self.success(frame=frame, previous=previous)
        self.assertEqual(set(plain), keys)
        kwargs = dict(frame=frame, previous=previous,
                      extra=('--marks-out', str(self.output)))
        first = self.success(**kwargs)
        encoded = self.output.read_bytes()
        second = self.success(**kwargs)
        self.assertEqual(encoded, self.output.read_bytes())
        self.assertTrue(first['changed'])
        self.assertGreaterEqual(len(first['marks']), 4)
        self.assertEqual(set(first), keys | {'marks', 'marks_png'})
        region_keys = {'x', 'y', 'w', 'h', 'score'}
        mark_keys = {'id', 'x', 'y', 'w', 'h', 'kind', 'score'}
        for region in first['changed_regions']:
            self.assertEqual(set(region), region_keys)
        for mark in first['marks']:
            self.assertEqual(set(mark), mark_keys)
        # Negative controls: each forbidden default extension breaks its predicate.
        for value, expected, field in ((first, keys | {'marks', 'marks_png'}, 's4_adjacency'),
                (first['changed_regions'][0], region_keys, 's4'),
                (first['marks'][0], mark_keys, 'rim_promille')):
            with self.assertRaises(AssertionError):
                self.assertEqual(set(dict(value, **{field: None})), expected)
        with ram_png(frame) as (_, path), ram_png(previous) as (_, prev):
            explicit = describe(path, prev, marks_out=self.output,
                                s4_attrs=False, marks_extra=False)
        for result in (first, second, explicit):
            result.pop('timing_us')
        self.assertEqual(first, second)
        self.assertEqual(first, explicit)
        self.assertEqual(encoded, self.output.read_bytes())

    def _assert_integer_evidence(self, value):
        if isinstance(value, dict):
            for item in value.values():
                self._assert_integer_evidence(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_integer_evidence(item)
        else:
            self.assertIn(type(value), (int, str, bool))

    def test_dense_row_badge_stagger_and_s4_attrs(self):  # @anchor:va.cu_tests.test_dense_row_badge_stagger_and_s4_attrs
        import numpy as np
        from visioncore.decode import png
        from visioncore import regions
        from vadapt.cu_describe import describe
        from vadapt.cu_marks import _badge_corners, _classify_kind, annotate, candidates
        from vnative.adapters.cu_regions import pair_regions, pair_regions_plus
        frame = png_bytes(320, 180, rects=[(20, y, 80, 14) for y in (20, 44, 68, 92)])[0]
        first = self.marked(frame)
        encoded = self.output.read_bytes()
        second = self.marked(frame)
        self.assertEqual(encoded, self.output.read_bytes())
        first.pop('timing_us')
        second.pop('timing_us')
        self.assertEqual(first, second)
        self.assertEqual(len(first['marks']), 4)

        def assert_badges(image):
            for index, mark in enumerate(first['marks']):
                x, y, w = (mark[k] for k in ('x', 'y', 'w'))
                lx = x + w - 6 if index % 2 else x
                other = x + 1 if index % 2 else x + w - 2
                self.assertEqual(image[y, lx].tolist(), [0, 0, 0])
                self.assertTrue(np.any(np.all(image[y+1:y+6, lx+1:lx+5] == 255, axis=2)))
                self.assertEqual(image[y+1, other].tolist(), [255, 255, 0])

        assert_badges(png(encoded))
        with mock.patch('vadapt.cu_marks._badge_corners', return_value=['top-left'] * 4):
            with self.assertRaises(AssertionError):
                assert_badges(png(annotate(png(frame), first['marks'])))
        single = png(png_bytes(180, 90, rects=((10, 20, 40, 24),
                     (54, 20, 44, 24), (102, 20, 40, 24)))[0])
        single_marks = candidates(single)
        self.assertEqual([m['kind'] for m in single_marks], ['button'] * 3)
        single_image = png(annotate(single, single_marks))
        for mark in single_marks:
            x, y = mark['x'], mark['y']
            self.assertEqual(single_image[y, x].tolist(), [0, 0, 0])
            self.assertEqual(single_image[y+1, x+2].tolist(), [255, 255, 255])
            self.assertEqual(single_image[y+8, x+1].tolist(), [255, 255, 0])
        self.assertEqual(_badge_corners([]), [])
        # Dense runs restart after a gap; the threshold includes exactly 32 px.
        marks = [dict(x=10, y=y, w=40, h=14) for y in (0, 32, 65, 97)]
        self.assertEqual(_badge_corners(marks), ['top-left', 'top-right', 'top-left', 'top-right'])
        cases = [((40, 24, 500, 0, 0, 0), 'button'),
                 ((60, 20, 500, 0, 0, 0), 'tab'),
                 ((120, 20, 500, 40, 0, 0), 'field'),
                 ((120, 20, 500, 41, 0, 0), 'row'),
                 ((20, 20, 500, 0, 0, 0), 'icon'),
                 ((40, 24, 499, 100, 0, 100), 'text'),
                 ((40, 24, 499, 99, 0, 100), 'unknown'),
                 ((120, 20, 200, 0, 0, 0), 'row'),
                 ((120, 20, 199, 0, 0, 0), 'unknown'),
                 ((40, 24, 499, 180, 200, 0), 'text')]
        self.assertEqual({_classify_kind(*args) for args, _ in cases},
                         {'button', 'tab', 'field', 'row', 'icon', 'text', 'unknown'})
        for args, expected in cases:
            self.assertEqual(_classify_kind(*args), expected)
        with self.assertRaises(AssertionError):
            self.assertEqual({'button', 'unknown'}, {_classify_kind(*a) for a, _ in cases})
        # Positive and negative threshold controls use the actual candidate path.
        simple = png(png_bytes(160, 80, rect=(10, 20, 40, 24))[0])
        extra = candidates(simple, extra=True)
        self.assertEqual(extra[0]['kind'], 'button')
        # The edge kernel expands a 40x24 fill to 42x26; rim has 132 hits.
        self.assertEqual(extra[0]['rim_promille'], (1000 * 132) // (2 * (42 + 26)))
        self.assertNotIn('rim_promille', candidates(simple)[0])
        for mark in candidates(png(frame), extra=True):
            self.assertIs(type(mark['rim_promille']), int)
            self.assertEqual(mark['rim_promille'] >= 500, mark['score'] >= 0.5)
        current = png_bytes(320, 180, rects=((20, 20, 24, 16), (220, 120, 24, 16)))[0]
        previous = png_bytes(320, 180)[0]
        raw, old = png(current), png(previous)
        records = pair_regions(old, raw)
        self.assertEqual(len(records), 2)
        self.assertEqual(pair_regions_plus(old, old), ([], []))
        self.assertEqual(pair_regions_plus(old, raw), (records, regions.adjacency(records)))
        # Nonempty graph control prevents a vacuous adjacency test.
        adjacent = [dict(id='a', spans=[['0000', '0000']]),
                    dict(id='b', spans=[['0001', '0001']])]
        with mock.patch('vnative.adapters.cu_regions.pair_regions', return_value=adjacent):
            self.assertEqual(pair_regions_plus(old, raw)[1], [['a', 'b', 1]])
        crops = self.output.parent / 'crops'
        crops.mkdir()
        result = self.success(frame=current, previous=previous, extra=(
            '--marks-out', str(self.output), '--s4-attrs', '--crop-dir', str(crops)))
        self.assertEqual(result['s4_adjacency'], regions.adjacency(records))
        for region in result['changed_regions']:
            record = next(r for r in records if r['bbox_px'] == [region[k] for k in ('x', 'y', 'w', 'h')])
            expected = {k: record[k] for k in ('group', 'cls', 'kind', 'mv', 'edge_ppm',
                                               'r_max', 'n_tiles', 'shade_only')}
            self.assertEqual(region['s4'], expected)
            self._assert_integer_evidence(region['s4'])
        self._assert_integer_evidence(result['s4_adjacency'])
        for mark in result['marks']:
            self._assert_integer_evidence({k: v for k, v in mark.items() if k != 'score'})
        with self.assertRaises(AssertionError):
            self._assert_integer_evidence(dict(result['changed_regions'][0]['s4'], edge_ppm=1.0))
        for crop, path in zip(result['crops'], result['crop_pngs']):
            self.assertEqual(Path(path).parent, crops)
            np.testing.assert_array_equal(png(path), raw[crop['y']:crop['y']+crop['h'],
                                                        crop['x']:crop['x']+crop['w']])
        self.assertEqual(len(result['crop_pngs']), len(result['crops']))
        with ram_png(current) as (_, path):
            self.assertEqual(describe(path, s4_attrs=True)['s4_adjacency'], [])
            empty = describe(path, path, crop_dir=crops, s4_attrs=True)
            self.assertEqual(empty['crop_pngs'], [])
            self.assertEqual(empty['s4_adjacency'], [])
        only_extra = self.success(frame=current, extra=(
            '--marks-out', str(self.output), '--marks-extra'))
        self.assertIn('rim_promille', only_extra['marks'][0])
        self.assertNotIn('s4_adjacency', only_extra)
        self._check_crop_refusals(current, previous, crops)

    def _check_crop_refusals(self, current, previous, crops):
        self.output.write_bytes(b'marks-sentinel')
        target = crops / 'crop_0.png'
        target.write_bytes(b'crop-sentinel')
        link = self.output.parent / 'escape-crops'
        link.symlink_to('/tmp', target_is_directory=True)
        base = ('--marks-out', str(self.output), '--crop-dir', str(crops))
        cases = [dict(frame=b'bad', previous=previous, extra=base),
                 dict(frame=current, previous=png_bytes(48, 80)[0], extra=base),
                 dict(frame=current, previous=previous, extra=(
                     '--marks-out', str(self.output), '--crop-dir', '/tmp')),
                 dict(frame=current, previous=previous, extra=(
                     '--marks-out', str(self.output), '--crop-dir', str(link))),
                 dict(frame=current, previous=previous, extra=(
                     '--marks-out', '/tmp/cu-bad.png', '--crop-dir', str(crops)))]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs['extra']):
                result = self.run_cli(**kwargs)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(self.output.read_bytes(), b'marks-sentinel')
                self.assertEqual(target.read_bytes(), b'crop-sentinel')
                self.assertFalse(list(self.output.parent.glob('.cu-*')))
                self.assertFalse(list(crops.glob('.cu-*')))
                self.assertFalse(list(crops.glob('*.tmp')))


    def test_crop_staging_failure_preserves_targets(self):  # @anchor:va.cu_tests.test_crop_staging_failure_preserves_targets
        from vadapt.cu_marks import write_crops
        from visioncore.decode import png
        dest = self.output.parent
        targets = [dest / ('crop_%d.png' % i) for i in range(2)]
        for target in targets:
            target.write_bytes(b'crop-sentinel')
        crops = [dict(x=0, y=0, w=8, h=8), dict(x=8, y=8, w=8, h=8)]
        original = tempfile.mkstemp
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('injected stage failure')
            return original(*args, **kwargs)

        with mock.patch('vadapt.cu_marks.tempfile.mkstemp', side_effect=fail_second):
            with self.assertRaises(OSError):
                write_crops(dest, png(png_bytes()[0]), crops)
        self.assertEqual([p.read_bytes() for p in targets], [b'crop-sentinel'] * 2)
        self.assertEqual(list(dest.glob('.cu-crop-*')), [])
        paths = write_crops(dest, png(png_bytes()[0]), crops)
        self.assertEqual(paths, [str(p) for p in targets])
        self.assertEqual([png(p).shape for p in paths], [(8, 8, 3)] * 2)


def _run_som_method(name):
    case = SetOfMarks(name)
    case.setUp()
    try:
        getattr(case, name)()
    finally:
        case.doCleanups()


def test_schema_and_determinism_on_real_capture():  # @anchor:va.cu_test_nodes.test_schema_and_determinism_on_real_capture
    _run_som_method('test_schema_and_determinism_on_real_capture')


def test_buttons_separate_and_cover():  # @anchor:va.cu_test_nodes.test_buttons_separate_and_cover
    _run_som_method('test_buttons_separate_and_cover')


def test_dense_row_badge_stagger_and_s4_attrs():  # @anchor:va.cu_test_nodes.test_dense_row_badge_stagger_and_s4_attrs
    _run_som_method('test_dense_row_badge_stagger_and_s4_attrs')


if __name__ == '__main__':
    unittest.main()
