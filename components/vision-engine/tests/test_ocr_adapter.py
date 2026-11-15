"""VE2 predicates: missingness, exact integer geometry and RAM-only clean pixels."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from eval.gui_synth.font import draw
from eval.v2x.gm_textline import _segment_lines
from visioncore.decode import encode_png, png
from vadapt import cu_describe, cu_ocr
from vadapt.cu_marks import annotate, candidates

_HEADER = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext'
_READY = dict(ok=True, missing_reason=None, binary='/fixture/tesseract', tessdata_dir='/fixture/data', lang='eng')


def _row(text='STEP', conf='96.54', x=5, y=5, w=10, h=5, num=1, line=1, level=5):
    return '\t'.join(map(str, (level, 1, 1, 1, line, num, x, y, w, h, conf, text)))


def _mark(x=0, y=0, w=80, h=40, ident=1):
    return dict(id=ident, x=x, y=y, w=w, h=h, kind='unknown', score=0)


@contextlib.contextmanager
def _frame(rgb):
    fd = os.memfd_create('ve2-test-png')
    try:
        os.write(fd, encode_png(rgb))
        yield '/proc/self/fd/%d' % fd, fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _workspace():
    # Existing CU writer law requires caller-owned RAM output parents.
    Path('/dev/shm/vqa-cu').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir='/dev/shm/vqa-cu', prefix='ve2-') as directory:
        yield Path(directory)


def _no_floats(case, value):
    case.assertNotIsInstance(value, float)
    if isinstance(value, dict):
        for child in value.values():
            _no_floats(case, child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _no_floats(case, child)


class TestOcrAdapter(unittest.TestCase):  # @anchor:va.test_ocr_adapter.TestOcrAdapter
    def test_sanitize_and_exact_confidence(self):  # @anchor:va.test_ocr_adapter.test_sanitize_and_exact_confidence
        self.assertEqual(cu_ocr.sanitize_ocr_text(' e\u0301\tA|B\\C '), 'é A\\|B\\\\C')
        for raw in ('', ' \n\t', '\u2003'):
            self.assertIsNone(cu_ocr.sanitize_ocr_text(raw))
        for raw in ('é' * 100, 'a' * 63 + '|', 'a' * 63 + '\\'):
            text = cu_ocr.sanitize_ocr_text(raw)
            self.assertLessEqual(len(text.encode()), 64)
            self.assertFalse((len(text) - len(text.rstrip('\\'))) % 2)
        for raw, expected in (('96.54', 965), ('100.0', 1000), ('0.0', 0),
                              ('100.99', 1000), ('-1', None), ('-1.0', None), ('', None)):
            self.assertEqual(cu_ocr.conf_to_promille(raw), expected)
        for raw in ('NaN', '1e2', '1.2.3', '+1'):
            with self.assertRaises(ValueError):
                cu_ocr.conf_to_promille(raw)

    def test_preflight_affirmative_readiness_and_failures(self):  # @anchor:va.test_ocr_adapter.test_preflight_affirmative_readiness_and_failures
        with _workspace() as root:
            binary = root / 'tesseract'
            binary.write_text('fixture, never executed')
            binary.chmod(0o700)
            data = root / 'eng.traineddata'
            with mock.patch.object(cu_ocr.subprocess, 'run') as run:
                self.assertEqual(cu_ocr.preflight(root, binary=binary)['missing_reason'], 'tessdata_absent')
                data.touch()
                self.assertEqual(cu_ocr.preflight(root, binary=binary)['missing_reason'], 'tessdata_absent')
                run.assert_not_called()
                data.write_bytes(b'fixture')
                before = os.environ.copy()
                run.return_value = subprocess.CompletedProcess([], 0, 'List of languages (1):\neng\n', '')
                result = cu_ocr.preflight(root, binary=binary, timeout_s=30)
                self.assertTrue(result['ok'])
                self.assertIsNone(result['missing_reason'])
                self.assertEqual(run.call_args.kwargs['timeout'], 5)
                self.assertEqual(run.call_args.kwargs['env']['TESSDATA_PREFIX'], str(root))
                self.assertEqual(os.environ, before)
                for stdout, rc in (('List:\nspa\n', 0), ('List:\neng\n', 1), ('eng', 0)):
                    run.return_value = subprocess.CompletedProcess([], rc, stdout, '')
                    self.assertFalse(cu_ocr.preflight(root, binary=binary)['ok'])
                run.side_effect = subprocess.TimeoutExpired('probe', 5)
                self.assertEqual(cu_ocr.preflight(root, binary=binary)['missing_reason'], 'tessdata_absent')
            binary.chmod(0o600)
            self.assertEqual(cu_ocr.preflight(root, binary=binary)['missing_reason'], 'tessdata_absent')

    def test_tessdata_absent_fails_closed_with_gm_textline_fallback(self):  # @anchor:va.test_ocr_adapter.test_tessdata_absent_fails_closed_with_gm_textline_fallback
        rgb = np.zeros((60, 120, 3), dtype=np.uint8)
        rgb[10:14, 5:65:2] = 255
        mark = _mark(w=100, h=40)
        with _workspace() as root:
            for empty in (False, True):
                if empty:
                    (root / 'eng.traineddata').touch()
                with mock.patch.object(cu_ocr.shutil, 'which', return_value=sys.executable), mock.patch.object(cu_ocr.subprocess, 'run') as run:
                    result = cu_ocr.annotate_marks(rgb, [mark], tessdata_dir=root)[0]
                    run.assert_not_called()
                self.assertIsNone(result['ocr_text'])
                self.assertIsNone(result['ocr_score_promille'])
                self.assertEqual(result['missing_reason'], 'tessdata_absent')
                raw = _segment_lines(rgb[:40, :100, 0].astype(np.int16), edge_thresh=5)
                expected = [dict(y0=t.start_row, h=t.height, line_hash=t.line_hash) for t in raw[:8]]
                self.assertEqual(result['lines'], expected)
                self.assertEqual(result['n_lines'], len(raw))
                self.assertEqual(result['visual_kind'], 'textline')
                with _frame(rgb) as (path, _), mock.patch.object(cu_describe, 'candidates', return_value=[mark]):
                    described = cu_describe.describe(path, marks_out=root / 'marks.png', ocr_marks=True, tessdata_dir=root)
                self.assertEqual(described['marks'][0], result)
            cli_rgb = rgb.copy()
            cli_rgb[35:50, 10:70] = 255
            with _frame(cli_rgb) as (path, fd):
                proc = subprocess.run([sys.executable, '-B', '-m', 'vadapt.cu_describe', '--frame', path,
                                       '--marks-out', str(root / 'cli.png'), '--ocr-marks', '--json'],
                                      pass_fds=(fd,), env=dict(os.environ, TESSDATA_PREFIX=str(root)),
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(json.loads(proc.stdout)['marks'])
                for record in json.loads(proc.stdout)['marks']:
                    self.assertIsNone(record['ocr_text'])
                    self.assertIsNone(record['ocr_score_promille'])
                    self.assertEqual(record['missing_reason'], 'tessdata_absent')

    def test_ocr_tsv_promille_and_r10_closed_box_intersection(self):  # @anchor:va.test_ocr_adapter.test_ocr_tsv_promille_and_r10_closed_box_intersection
        tsv = '\n'.join((_HEADER, _row(), _row('ZERO', '0.0', x=14, y=9, w=1, h=1, num=2),
                         _row('FULL', '100.0', x=100), _row('sentinel', '-1'),
                         _row('sentinel2', '-1.0'), _row(w=0), _row(h=-1), _row(' ', '90')))
        words = cu_ocr.parse_tsv(tsv)
        self.assertEqual([w['conf_promille'] for w in words], [965, 0, 1000])
        self.assertEqual(words[0]['box'], (5, 5, 14, 9))
        _no_floats(self, words)
        rgb = np.zeros((60, 120, 3), dtype=np.uint8)
        joined = cu_ocr.words_for_marks(words, [_mark(x=14, y=9, w=1, h=1)], rgb)[0]
        self.assertEqual(joined['ocr_text'], 'STEP ZERO')
        self.assertEqual(joined['ocr_score_promille'], 482)
        self.assertIsNone(joined['missing_reason'])
        outside = cu_ocr.words_for_marks(words, [_mark(x=15, y=10, w=1, h=1)], rgb)[0]
        self.assertIsNone(outside['ocr_text'])
        self.assertEqual(outside['missing_reason'], 'ocr_empty')
        self.assertEqual(len(cu_ocr.parse_tsv(tsv, max_rows=1)), 1)
        for broken in ('bad header', _HEADER + '\n5\t1', _HEADER + '\n' + _row(conf='oops')):
            with self.assertRaises(ValueError):
                cu_ocr.parse_tsv(broken)
        escaped = cu_ocr.parse_tsv(_HEADER + '\n' + _row('A|B\\C'))
        self.assertEqual(cu_ocr.words_for_marks(escaped, [_mark()], rgb)[0]['ocr_text'], 'A\\|B\\\\C')

    def test_single_full_frame_pass_clean_pixels_and_cli_flag(self):  # @anchor:va.test_ocr_adapter.test_single_full_frame_pass_clean_pixels_and_cli_flag
        rgb = np.zeros((80, 160, 3), dtype=np.uint8)
        rgb[20:40, 20:80] = 255
        marks = [_mark(x=19, y=19, w=62, h=22, ident=i) for i in range(1, 25)]
        reply = subprocess.CompletedProcess([], 0, (_HEADER + '\n' + _row(x=25, y=25)).encode(), b'')
        with _workspace() as root, _frame(rgb) as (path, _):
            with mock.patch.object(cu_ocr, 'preflight', return_value=_READY), mock.patch.object(cu_ocr.subprocess, 'run', return_value=reply) as run, mock.patch.object(cu_describe, 'candidates', return_value=marks):
                result = cu_describe.describe(path, marks_out=root / 'marks.png', ocr_marks=True)
            run.assert_called_once()
            call = run.call_args
            self.assertEqual(call.args[0][1:3], ['stdin', 'stdout'])
            self.assertIn('tessedit_create_tsv=1', call.args[0])
            self.assertEqual(call.kwargs['input'], encode_png(rgb))
            self.assertNotEqual(call.kwargs['input'], (root / 'marks.png').read_bytes())
            self.assertEqual(result['frame_sha256'], hashlib.sha256(rgb.tobytes()).hexdigest())
            self.assertEqual(len(result['marks']), 24)
            self.assertEqual(result['marks'][0]['ocr_text'], 'STEP')
            self.assertEqual(result['marks'][0]['ocr_score_promille'], 965)
            self.assertTrue(all(m['ocr_text'] is None for m in result['marks'][1:]))
            with mock.patch.object(cu_describe, 'describe', return_value={'schema': 'fixture'}) as describe, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cu_describe.main(['--frame', path, '--marks-out', str(root / 'other.png'), '--ocr-marks', '--json']), 0)
                self.assertIs(describe.call_args.kwargs['ocr_marks'], True)
        with self.assertRaises(ValueError):
            cu_describe.describe('/not/read', ocr_marks=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cu_describe.main(['--frame', '/not/read', '--ocr-marks', '--json']), 2)

    def test_subprocess_failures_and_nameless_filter(self):  # @anchor:va.test_ocr_adapter.test_subprocess_failures_and_nameless_filter
        rgb = np.zeros((40, 80, 3), dtype=np.uint8)
        marks = [_mark(ident=1), _mark(ident=2)]
        fixtures = [(subprocess.CompletedProcess([], 1, b'', b'Error opening data file'), 'tessdata_absent'),
                    (subprocess.CompletedProcess([], 1, b'', b'Could not initialize tesseract'), 'tessdata_absent'),
                    (subprocess.CompletedProcess([], 1, b'', b'failure'), 'ocr_unavailable'),
                    (subprocess.CompletedProcess([], 0, b'broken TSV', b''), 'ocr_unavailable'),
                    (subprocess.TimeoutExpired('ocr', 10), 'ocr_unavailable'),
                    (BrokenPipeError(), 'ocr_unavailable'),
                    (subprocess.CompletedProcess([], 0, _HEADER.encode(), b''), 'ocr_empty')]
        for fixture, reason in fixtures:
            runner = mock.Mock(**({'side_effect': fixture} if isinstance(fixture, Exception) else {'return_value': fixture}))
            with mock.patch.object(cu_ocr, 'preflight', return_value=_READY):
                result = cu_ocr.annotate_marks(rgb, marks, nameless_ids={1}, runner=runner, timeout_s=50)
            self.assertEqual(result[0]['missing_reason'], reason)
            self.assertIsNone(result[0]['ocr_text'])
            self.assertIsNone(result[0]['ocr_score_promille'])
            self.assertEqual(result[1]['missing_reason'], 'named_by_ax')
            runner.assert_called_once()
            self.assertEqual(runner.call_args.kwargs['timeout'], 5)
        words = cu_ocr.parse_tsv(_HEADER + '\n' + _row())
        result = cu_ocr.words_for_marks(words, marks, rgb, nameless_ids={1})
        self.assertEqual(result[0]['ocr_text'], 'STEP')
        self.assertIsNone(result[1]['ocr_text'])
        self.assertEqual(result[1]['missing_reason'], 'named_by_ax')
        with mock.patch.object(cu_ocr, 'preflight', return_value=dict(_READY, ok=False, missing_reason='tessdata_absent')):
            self.assertEqual(cu_ocr.annotate_marks(rgb, marks, nameless_ids=set())[0]['missing_reason'], 'named_by_ax')
        for bad in ('marks.png', rgb.astype(np.int16), rgb[:, :, 0], np.zeros((2, 2, 4), dtype=np.uint8)):
            with self.assertRaises(ValueError):
                cu_ocr.annotate_marks(bad, marks)

    def test_visual_kind_classification_and_line_hash_stability(self):  # @anchor:va.test_ocr_adapter.test_visual_kind_classification_and_line_hash_stability
        rgb = np.zeros((80, 200, 3), dtype=np.uint8)
        draw(rgb, 'STEP STEP STEP STEP', 8, 10, scale=2)
        draw(rgb, 'PAUSE PAUSE PAUSE', 8, 40, scale=2)
        text = cu_ocr.fallback_mark_lines(rgb, _mark(w=200, h=80))
        self.assertEqual(text['visual_kind'], 'textline')
        self.assertEqual(text['n_lines'], 2)
        self.assertEqual(text, cu_ocr.fallback_mark_lines(rgb.copy(), _mark(w=200, h=80)))
        glyph_rgb = np.zeros((40, 100, 3), dtype=np.uint8)
        draw(glyph_rgb, 'STEP STEP', 8, 10, scale=1)
        # A clipped two-row glyph profile avoids the kernel's known quantization
        # collisions on tall lines; the original hashing kernel stays unchanged.
        glyph_rgb[12:] = 0
        before = cu_ocr.fallback_mark_lines(glyph_rgb, _mark(w=100, h=40))
        glyph_rgb[10, 10] ^= 255
        self.assertNotEqual(before['lines'], cu_ocr.fallback_mark_lines(glyph_rgb, _mark(w=100, h=40))['lines'])
        for w, h, expected in ((160, 30, 'pill'), (24, 24, 'icon_btn'), (120, 80, 'canvas_ctrl')):
            crop = np.zeros((h, w, 3), dtype=np.uint8)
            crop[[0, -1], :] = 255
            crop[:, [0, -1]] = 255
            self.assertEqual(cu_ocr.fallback_mark_lines(crop, _mark(w=w, h=h))['visual_kind'], expected)
        many = np.zeros((40, 80, 3), dtype=np.uint8)
        many[::3, 5:65:2] = 255
        fb = cu_ocr.fallback_mark_lines(many, _mark())
        self.assertEqual(fb['n_lines'], 8)
        self.assertEqual(len(fb['lines']), 8)
        _no_floats(self, fb)

    def test_storage_law_no_disk_frames_outside_shm(self):  # @anchor:va.test_ocr_adapter.test_storage_law_no_disk_frames_outside_shm
        rgb = np.zeros((80, 160, 3), dtype=np.uint8)
        rgb[20:40, 20:80] = 255
        original_mkstemp = tempfile.mkstemp
        def checked_mkstemp(*args, **kwargs):
            self.assertTrue(Path(kwargs['dir']).resolve().is_relative_to('/dev/shm/vqa-cu'))
            return original_mkstemp(*args, **kwargs)
        with _workspace() as root, _frame(rgb) as (path, fd):
            with mock.patch.object(cu_ocr, 'preflight', return_value=_READY), mock.patch.object(cu_ocr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, _HEADER.encode(), b'')), mock.patch.object(tempfile, 'mkstemp', side_effect=checked_mkstemp) as mk, mock.patch.object(tempfile, 'NamedTemporaryFile', side_effect=AssertionError('unexpected disk temp')):
                cu_describe.describe(path, marks_out=root / 'marks.png', ocr_marks=True)
                self.assertTrue(mk.called)
            # An invalid destination and a symlink escape must preserve sentinels.
            with tempfile.TemporaryDirectory(dir=root.parent.parent, prefix='ve2-outside-') as outside:
                sentinel = Path(outside) / 'sentinel.png'
                sentinel.write_bytes(b'caller-owned')
                (root / 'escape').symlink_to(outside, target_is_directory=True)
                for destination in (sentinel, root / 'escape' / 'sentinel.png'):
                    proc = subprocess.run([sys.executable, '-B', '-m', 'vadapt.cu_describe', '--frame', path,
                                           '--marks-out', str(destination), '--ocr-marks', '--json'],
                                          pass_fds=(fd,), capture_output=True, timeout=30)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(sentinel.read_bytes(), b'caller-owned')
                self.assertEqual(sorted(p.name for p in Path(outside).iterdir()), ['sentinel.png'])
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['escape', 'marks.png'])

    def test_default_cu_describe_byte_identical_when_flag_off(self):  # @anchor:va.test_ocr_adapter.test_default_cu_describe_byte_identical_when_flag_off
        rgb = np.zeros((80, 160, 3), dtype=np.uint8)
        rgb[20:40, 20:80] = 255
        keys = {'schema', 'frame_sha256', 'width', 'height', 'changed', 'changed_regions', 'crops', 'timing_us'}
        with _workspace() as root, _frame(rgb) as (path, _), mock.patch.object(cu_describe.time, 'perf_counter_ns', return_value=1000), mock.patch.object(cu_describe, 'ocr_annotate_marks', side_effect=AssertionError('default enabled OCR')):
            self.assertEqual(set(cu_describe.describe(path)), keys)
            off = cu_describe.describe(path, marks_out=root / 'marks.png')
            explicit = cu_describe.describe(path, marks_out=root / 'marks.png', ocr_marks=False)
            self.assertEqual(json.dumps(off, sort_keys=True), json.dumps(explicit, sort_keys=True))
            self.assertTrue(off['marks'])
            for mark in off['marks']:
                self.assertEqual(set(mark), {'id', 'x', 'y', 'w', 'h', 'kind', 'score'})

    def test_live_tesseract_with_local_tessdata_or_skip(self):  # @anchor:va.test_ocr_adapter.test_live_tesseract_with_local_tessdata_or_skip
        td = os.environ.get('TESSDATA_PREFIX', '')
        if not cu_ocr.preflight(tessdata_dir=td)['ok']:
            self.skipTest('local executable/language readiness AUSENTE')
        rgb = np.full((180, 240, 3), 255, dtype=np.uint8)
        marks = []
        for i, text in enumerate(('STEP', 'PAUSE', 'SEND')):
            y = i * 55 + 5
            draw(rgb, text, 20, y+10, color=(0, 0, 0), scale=3)
            marks.append(_mark(x=10, y=y, w=210, h=45, ident=i+1))
        a = cu_ocr.annotate_marks(rgb, marks, tessdata_dir=td)
        self.assertEqual(a, cu_ocr.annotate_marks(rgb, marks, tessdata_dir=td))
        for record in a:
            if record['ocr_text'] is None:
                self.assertEqual(record['missing_reason'], 'ocr_empty')
                self.assertIsNone(record['ocr_score_promille'])
            else:
                self.assertIsInstance(record['ocr_text'], str)
                self.assertIs(type(record['ocr_score_promille']), int)
                self.assertIsNone(record['missing_reason'])
        self.assertTrue(any(r['ocr_text'] for r in a), a)

    def test_clean_digest_named_exclusion_and_word_winner(self):  # @anchor:va.test_ocr_adapter.test_clean_digest_named_exclusion_and_word_winner
        rgb = np.zeros((40, 80, 3), dtype=np.uint8)
        marks = [_mark(ident=2), _mark(w=20, h=20, ident=1)]
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        words = cu_ocr.parse_tsv(_HEADER + '\n' + _row())
        with mock.patch.object(cu_ocr, 'run_full_frame_tsv', return_value=dict(ocr_ok=True, missing_reason=None, words=words)) as run:
            output = cu_ocr.annotate_marks(rgb, marks, clean_sha256=digest)
            self.assertIsNone(output[0]['ocr_text'])
            self.assertEqual(output[1]['ocr_text'], 'STEP')
            run.assert_called_once()
            run.reset_mock()
            named = cu_ocr.annotate_marks(rgb, marks, named_mark_ids=(1, 2))
            run.assert_not_called()
            self.assertTrue(all(m['missing_reason'] == 'named_by_ax' for m in named))
            changed = png(annotate(rgb, marks))
            with self.assertRaises(ValueError):
                cu_ocr.annotate_marks(changed, marks, clean_sha256=digest)
            run.assert_not_called()
        self.assertEqual(cu_ocr.parse_conf_promille('96.59'), (965, False))
        self.assertEqual(cu_ocr.parse_conf_promille('-1'), (None, True))
        with self.assertRaises(ValueError):
            cu_ocr.parse_conf_promille('')
        self.assertEqual(cu_ocr.parse_tsv(_HEADER + '\n' + _row(x=-1)), [])
        # Narrow glyph crops retain deterministic evidence below the usual edge floor.
        narrow = np.zeros((12, 12, 3), dtype=np.uint8)
        narrow[3:7, ::2] = 255
        fallback = cu_ocr.fallback_mark_lines(narrow, _mark(w=12, h=12))
        self.assertEqual(fallback['n_lines'], 1)
        self.assertEqual(fallback['line_hash'], fallback['lines'][0]['line_hash'])
        self.assertEqual(fallback, cu_ocr.fallback_mark_lines(narrow.copy(), _mark(w=12, h=12)))
