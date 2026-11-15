"""RAM-only WDEMO controls and gate-removal mutants through public APIs."""
import contextlib
from dataclasses import replace
import io
import inspect
import json
import os
from pathlib import Path
import struct
import shutil
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch
import zlib
import numpy as np
from wender.learn import demonstrations as d
from wender.learn import demo_replay as r
from wender.perceive.srf_identity import SrfIdentitySurface
from tools import cu_demo as cli
from tools import reseal


# @anchor:wa.learn.demo_learning_tests.chunk
def chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)


# @anchor:wa.learn.demo_learning_tests.png
def png(a, *, raw=None, dimensions=None):
    h, w = a.shape[:2] if dimensions is None else dimensions
    data = b''.join(b'\0' + row.tobytes() for row in a) if raw is None else raw
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(data)) + chunk(b'IEND', b''))


# @anchor:wa.learn.demo_learning_tests.Clock
class Clock:
    # @anchor:wa.learn.demo_learning_tests.Clock.__init__
    def __init__(self):
        self.now = 1_000_000_000
        self.jump = 0

    def __call__(self):
        self.now += self.jump
        return self.now

    # @anchor:wa.learn.demo_learning_tests.Clock.pace
    def pace(self, seconds):
        self.now += int(seconds * 1_000_000_000)


# @anchor:wa.learn.demo_learning_tests.DemoTests
class DemoTests(unittest.TestCase):
    # @anchor:wa.learn.demo_learning_tests.DemoTests.setUp
    def setUp(self):
        root = Path('/dev/shm/vqa-ramws')
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root, prefix='wdemo-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.before = np.random.default_rng(27).integers(0, 256, (120, 192, 3), dtype=np.uint8)
        self.after = self.before.copy()
        self.after[50:60, 90:100] = 255 - self.after[50:60, 90:100]
        self.policy = r.ReplayPolicy(displacement=3)
        self.clock = Clock()
        self.inputs = []
        self.record()

    # @anchor:wa.learn.demo_learning_tests.DemoTests.row
    def row(self, seq=1, kind='click', **updates):
        row = dict(seq=seq, mono_ns=seq, window={'id': 'w1', 'w': 192, 'h': 120}, kind=kind,
                   x=96, y=60, button=1, x2=None, y2=None, dir=None, keysym=None,
                   text_len=None, modifiers=[])
        if kind in ('key', 'text'):
            row.update(x=None, y=None, button=None)
        if kind == 'key':
            row['keysym'] = 'Return'
        if kind == 'text':
            row['text_len'] = 6
        if kind == 'drag':
            row.update(x2=110, y2=60)
        if kind == 'scroll':
            row.update(button=None, dir='down')
        row.update(updates)
        return row

    # @anchor:wa.learn.demo_learning_tests.DemoTests.record
    def record(self, rows=None):
        rows = rows or [self.row()]
        (self.root / 'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        for row in rows:
            (self.root / f"{row['seq']}_before.png").write_bytes(png(self.before))
            (self.root / f"{row['seq']}_after.png").write_bytes(png(self.after))
        (self.root / 'final.png').write_bytes(png(self.after))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.load
    def load(self, limits=None):
        return d.load_demo(self.root, limits=limits or d.DemoLimits())

    # @anchor:wa.learn.demo_learning_tests.DemoTests.steps
    def steps(self):
        return d.segment_demo(self.load())

    # @anchor:wa.learn.demo_learning_tests.DemoTests.refuses_load
    def refuses_load(self, code, limits=None):
        with self.assertRaisesRegex(ValueError, '^' + code + '$'):
            self.load(limits)
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.surface
    def surface(self, steps, *, frames=None, paste=None, send_error=None, completion=None, key_policy=None, text_encoding='ascii'):
        identity = SrfIdentitySurface(identity='demo', container='demo', window='w1', ram_dir=self.root)
        frames = list(frames or [self.before, self.after] * len(steps))
        counter = [0]

        # @anchor:wa.learn.demo_learning_tests.DemoTests.surface.frame
        def frame():
            counter[0] += 1
            rgb = frames[min(counter[0] - 1, len(frames) - 1)]
            path = self.root / 'live.png'
            path.write_bytes(png(rgb))
            return dict(path=str(path), seq=counter[0], width=192, height=120,
                        window={'id': 'w1', 'w': 192, 'h': 120}, capture_mono_ns=self.clock())

        # @anchor:wa.learn.demo_learning_tests.DemoTests.surface.send
        def send(kind, **payload):
            self.inputs.append((kind, payload))
            if send_error:
                raise RuntimeError(send_error)
            return completion if completion is not None else {'focus_verified': True, 'input_mono_ns': self.clock()}

        identity.frame = frame
        identity._call = send
        surface = r.DemoIdentitySurface(identity, selected_steps=steps, policy=self.policy,
                                        clock=self.clock, pace=self.clock.pace, paste_input=paste,
                                        key_policy=key_policy, text_encoding=text_encoding)
        self.addCleanup(surface.close)
        return surface

    # @anchor:wa.learn.demo_learning_tests.DemoTests.run_steps
    def run_steps(self, steps=None, **kwargs):
        steps = self.steps() if steps is None else steps
        surface = self.surface(steps, **kwargs)
        result = r.replay(surface, steps, texts={})
        self.assertFalse(surface._identity.started)
        self.assertFalse(surface._owner.check_ready())
        return result

    # @anchor:wa.learn.demo_learning_tests.DemoTests.assert_code
    def assert_code(self, result, code, count=0):
        self.assertEqual(result.receipts[-1].failure_code, code)
        self.assertEqual(len(self.inputs), count)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_complete_recording_and_immutable_descriptors
    def test_complete_recording_and_immutable_descriptors(self):
        rec = self.load()
        self.assertEqual(len(rec.events), 1)
        steps = d.segment_demo(rec)
        self.assertTrue(steps[0].replayable)
        self.assertEqual(steps[0].target.mark.missing_reason, 'MARKS_UNAVAILABLE')
        with self.assertRaises(ValueError):
            steps[0].target.small.pixels.setflags(write=True)
        self.assertNotIn('array', repr(steps))
        result = self.run_steps(steps)
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs, [('click', {'point': '96,60', 'button': 1})])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_closed_schema
    def test_closed_schema(self):
        for updates in ({'unknown': 1}, {'text': 'PRIVATE'}, {'window': {'id': 'w1', 'w': 192, 'h': 120, 'url': 'PRIVATE'}}):
            with self.subTest(updates=updates):
                self.record([self.row(**updates)])
                self.refuses_load('CLOSED_SCHEMA')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_duplicate_json
    def test_duplicate_json(self):
        p = self.root / 'events.jsonl'
        p.write_text(p.read_text().replace('"seq": 1', '"seq": 1,"seq": 1'))
        self.refuses_load('CLOSED_SCHEMA')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_malformed_json
    def test_malformed_json(self):
        for data in (b'{', b'\xff', b'{"seq":NaN}', b'null', b'[]'):
            (self.root / 'events.jsonl').write_bytes(data)
            self.refuses_load('CLOSED_SCHEMA')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_boolean_and_fractional_coordinate
    def test_boolean_and_fractional_coordinate(self):
        for value in (True, 1.5, -1, 192):
            self.record([self.row(x=value)])
            self.refuses_load('POINT_INTEGER')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_event_integer
    def test_event_integer(self):
        for updates in ({'seq': True}, {'mono_ns': -1}, {'mono_ns': 2**63}):
            self.record([self.row(**updates)])
            self.refuses_load('EVENT_INTEGER')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_oversized_dimensions
    def test_oversized_dimensions(self):
        self.record([self.row(window={'id': 'w1', 'w': 99999, 'h': 120})])
        self.refuses_load('IMAGE_BUDGET')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_png_decompression_bomb
    def test_png_decompression_bomb(self):
        (self.root / '1_before.png').write_bytes(png(self.before, raw=b'\0' * (120 * (192 * 3 + 1) + 1000)))
        self.refuses_load('PNG_DECOMPRESSION')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_png_structure
    def test_png_structure(self):
        p = self.root / '1_before.png'
        good = p.read_bytes()
        for bad, code in ((good[:-1], 'PNG_FORMAT'), (good + b'junk', 'PNG_FORMAT'),
                           (good[:29] + bytes([good[29] ^ 1]) + good[30:], 'PNG_CRC'),
                           (png(self.before, raw=b'\5' + b'\0' * (120 * (192 * 3 + 1) - 1)), 'PNG_FILTER')):
            p.write_bytes(bad)
            self.refuses_load(code)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_png_unsupported
    def test_png_unsupported(self):
        good = png(self.before)
        unsupported = good[:8] + chunk(b'IHDR', struct.pack('>IIBBBBB', 192, 120, 8, 2, 0, 0, 1)) + good[33:]
        (self.root / '1_before.png').write_bytes(unsupported)
        self.refuses_load('PNG_UNSUPPORTED')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_excessive_rows
    def test_excessive_rows(self):
        self.record([self.row(1), self.row(2)])
        self.refuses_load('EVENT_BUDGET', replace(d.DemoLimits(), events=1))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_line_bound
    def test_line_bound(self):
        self.refuses_load('EVENT_BUDGET', replace(d.DemoLimits(), event_line=100))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_file_bound
    def test_file_bound(self):
        self.refuses_load('FILE_BUDGET', replace(d.DemoLimits(), event_file=10))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_near_boundary_limits
    def test_near_boundary_limits(self):
        size = (self.root / 'events.jsonl').stat().st_size
        rec = self.load(replace(d.DemoLimits(), events=1, axis=192, pixels=192 * 120, event_file=size, event_line=size - 1))
        self.assertEqual(rec.binding, ('w1', 192, 120))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_symlink_and_missing
    def test_symlink_and_missing(self):
        p = self.root / '1_before.png'
        p.unlink()
        p.symlink_to(self.root / '1_after.png')
        self.refuses_load('FILE_CUSTODY')
        p.unlink()
        for name in ('1_before.png', '1_after.png', 'final.png'):
            self.record()
            (self.root / name).unlink()
            self.refuses_load('FILE_CUSTODY')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_outside_ram
    def test_outside_ram(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm') as outside:
            for item in self.root.iterdir():
                shutil.copyfile(item, Path(outside) / item.name)
            with self.assertRaisesRegex(ValueError, '^FILE_CUSTODY$'):
                d.load_demo(outside, limits=d.DemoLimits())

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_recording_order
    def test_recording_order(self):
        for rows in ([self.row(1), self.row(1)], [self.row(1, mono_ns=4), self.row(2, mono_ns=3)]):
            self.record(rows)
            self.refuses_load('RECORDING_ORDER')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_window_binding
    def test_window_binding(self):
        self.record([self.row(1), self.row(2, window={'id': 'other', 'w': 192, 'h': 120})])
        self.refuses_load('WINDOW_BINDING')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_frame_dimensions
    def test_frame_dimensions(self):
        (self.root / '1_before.png').write_bytes(png(self.before[:100]))
        self.refuses_load('FRAME_DIMENSIONS')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_final_integrity
    def test_final_integrity(self):
        (self.root / 'final.png').write_bytes(png(self.before))
        self.refuses_load('FINAL_MISMATCH')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_final_informative
    def test_final_informative(self):
        (self.root / 'final.png').write_bytes(png(self.before))
        rec = d.load_demo(self.root, limits=d.DemoLimits(), final_check='informative')
        self.assertFalse(rec.final_matches)
        self.assertTrue(d.segment_demo(rec))
        with self.assertRaisesRegex(ValueError, '^INVALID_LIMITS$'):
            d.load_demo(self.root, limits=d.DemoLimits(), final_check='loose')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_record_changed
    def test_record_changed(self):
        rec = self.load()
        (self.root / '1_before.png').write_bytes(png(self.after))
        with self.assertRaisesRegex(ValueError, '^FILE_CHANGED$'):
            d.segment_demo(rec)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.locate
    def locate(self, image=None, target=None, policy=None):
        return r.locate(self.before if image is None else image, target or self.steps()[0].target,
                        policy=policy or self.policy, deadline_ns=self.clock() + 1_000_000_000, clock=self.clock)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_unique_translation
    def test_unique_translation(self):
        image = np.zeros_like(self.before)
        image[2:, 3:] = self.before[:-2, :-3]
        result = self.locate(image)
        self.assertIsNone(result.failure_code)
        self.assertEqual(result.point, (99, 62))
        self.assertEqual(result.translation, (3, 2))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_repeated_target
    def test_repeated_target(self):
        # Periodic texture makes distinct adjacent translations equally valid.
        a = np.random.default_rng(3).integers(0, 256, (2, 2, 3), dtype=np.uint8)
        self.before = np.tile(a, (60, 96, 1))
        self.after = self.before.copy()
        self.after[50:60, 90:100] ^= 255
        self.record()
        self.assert_code(self.run_steps(), 'AMBIGUOUS_TARGET')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_low_confidence
    def test_low_confidence(self):
        unrelated = np.random.default_rng(7).integers(0, 256, self.before.shape, dtype=np.uint8)
        self.assert_code(self.run_steps(frames=[unrelated]), 'LOW_CONFIDENCE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_degenerate_template
    def test_degenerate_template(self):
        self.before[:] = 0
        self.record()
        self.assert_code(self.run_steps(), 'DEGENERATE_TEMPLATE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_nonfinite_score
    def test_nonfinite_score(self):
        with patch.object(r, '_ncc', return_value=float('nan')):
            self.assert_code(self.run_steps(), 'NONFINITE_SCORE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_crop_offset
    def test_crop_offset(self):
        self.record([self.row(x=2, y=3)])
        result = self.locate()
        self.assertEqual(result.point, (2, 3))
        s = self.steps()[0]
        bad = replace(s.target.small, point_offset_xy=(True, 3))
        self.assertEqual(self.locate(target=replace(s.target, small=bad)).failure_code, 'CROP_GEOMETRY')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_context_disagreement
    def test_context_disagreement(self):
        target = self.steps()[0].target
        image = np.random.default_rng(9).integers(0, 256, self.before.shape, dtype=np.uint8)
        x0, y0, x1, y1 = target.small.bbox_xyxy
        image[y0:y1, x0:x1] = target.small.pixels
        self.assertEqual(self.locate(image).failure_code, 'LOW_CONFIDENCE')
        self.assertIsNone(self.locate().failure_code)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.verify
    def verify(self, after, expected=None):
        return r.verify_change(self.before, after, expected or self.steps()[0].expected_change,
                               translation=(0, 0), policy=self.policy)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_change_absent
    def test_change_absent(self):
        self.assertFalse(self.verify(self.before).verified)
        self.assertTrue(self.verify(self.after).verified)
        self.assert_code(self.run_steps(frames=[self.before]), 'CHANGE_MISMATCH', 1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_unobservable
    def test_unobservable(self):
        self.after = self.before.copy()
        self.record()
        self.assert_code(self.run_steps(), 'CHANGE_UNOBSERVABLE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_change_coverage
    def test_change_coverage(self):
        altered = self.before.copy()
        # Bounding-box corners changed, but almost all expected pixels unchanged.
        altered[50, 90] = self.after[50, 90]
        altered[59, 99] = self.after[59, 99]
        expected = self.steps()[0].expected_change
        # Preserve identical after-crop so only coverage fails.
        expected = replace(expected, after_crop=altered[50:60, 90:100])
        result = self.verify(altered, expected)
        self.assertFalse(result.verified)
        self.assertEqual(result.failure_code, 'CHANGE_MISMATCH')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_change_iou
    def test_change_iou(self):
        altered = self.after.copy()
        altered[0:20, 0:20] ^= 255
        result = self.verify(altered)
        self.assertFalse(result.verified)
        self.assertEqual(result.failure_code, 'CHANGE_MISMATCH')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_change_similarity
    def test_change_similarity(self):
        altered = self.after.copy()
        altered[50:60, 90:100] = 0
        result = self.verify(altered)
        self.assertFalse(result.verified)
        self.assertEqual(result.failure_code, 'CHANGE_MISMATCH')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_freshness
    def test_freshness(self):
        steps = self.steps()
        for key, value, code in (('seq', 0, 'FRAME_FRESHNESS'), ('capture_mono_ns', None, 'FRAME_FRESHNESS'),
                                  ('capture_mono_ns', 1, 'FRAME_FRESHNESS'), ('window', {'id': 'wrong'}, 'WINDOW_BINDING')):
            self.inputs.clear()
            surface = self.surface(steps)
            original = surface._identity.frame
            count = [0]
            # @anchor:wa.learn.demo_learning_tests.DemoTests.test_freshness.frame
            def frame():
                count[0] += 1
                f = original()
                if count[0] > 1:
                    f[key] = value
                return f
            surface._identity.frame = frame
            result = r.replay(surface, steps, texts={})
            self.assert_code(result, code, 1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_integer_dispatch
    def test_integer_dispatch(self):
        for point in ((True, 60), (96.0, 60), (96.5, 60)):
            steps = self.steps()
            with patch.object(r, 'locate', return_value=r.LocateResult(point, (0, 0), 1, 0, 1)):
                self.assert_code(self.run_steps(steps), 'POINT_INTEGER')
            self.inputs.clear()

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_first_failure_stops
    def test_first_failure_stops(self):
        self.record([self.row(1), self.row(2)])
        self.assert_code(self.run_steps(frames=[self.before]), 'CHANGE_MISMATCH', 1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_two_steps_positive
    def test_two_steps_positive(self):
        self.record([self.row(1), self.row(2)])
        result = self.run_steps()
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(len(self.inputs), 2)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_uncertain
    def test_uncertain(self):
        self.record([self.row(1), self.row(2)])
        result = self.run_steps(send_error='SECRET_PRIVATE_CANARY')
        self.assert_code(result, 'INPUT_UNCERTAIN', 1)
        self.assertTrue(result.input_uncertain)
        self.assertNotIn('SECRET', json.dumps(cli.receipt_json(result)))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_malformed_completion
    def test_malformed_completion(self):
        result = self.run_steps(completion={'focus_verified': True, 'input_mono_ns': True})
        self.assertTrue(result.input_uncertain)
        self.assertEqual(len(self.inputs), 1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_text_slots
    def test_text_slots(self):
        self.record([self.row(kind='text')])
        steps = self.steps()
        for value, code in ((None, 'TEXT_SLOT'), ('short', 'TEXT_SLOT'), ('秘密秘密秘密', 'TEXT_ENCODING')):
            surface = self.surface(steps, paste=lambda *_a, **_k: None)
            result = r.replay(surface, steps, texts={} if value is None else {1: value})
            self.assert_code(result, code)
            self.assertNotIn('秘密', json.dumps(cli.receipt_json(result), ensure_ascii=False))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_default_text_actuator
    def test_default_text_actuator(self):
        self.record([self.row(kind='text', text_len=6)])
        steps = self.steps()
        result = r.replay(self.surface(steps), steps, texts={1: 'CANARY'})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs, [('type', {'text': 'CANARY'})])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_ctrl_v_text_slot
    def test_ctrl_v_text_slot(self):
        # An operator paste-text row is recorded as ctrl+v; replay pastes the caller text there.
        self.record([self.row(kind='key', keysym='v', modifiers=['ctrl'])])
        steps = self.steps()
        result = r.replay(self.surface(steps), steps, texts={1: 'HELLO prompt'})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs, [('type', {'text': 'HELLO prompt'})])
        self.inputs.clear()
        self.record([self.row(kind='key')])
        steps = self.steps()
        self.assert_code(r.replay(self.surface(steps), steps, texts={1: 'x'}), 'TEXT_SLOT')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_wait_for_target
    def test_wait_for_target(self):
        # The operator waited 10 s before clicking (generation); replay waits for the target to appear.
        self.record([self.row(1), self.row(2, mono_ns=10_000_000_000)])
        steps = self.steps()
        blank = np.zeros_like(self.before)
        frames = [self.before, self.after, blank, blank, blank, self.before, self.after]
        result = r.replay(self.surface(steps, frames=frames), steps, texts={})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(len([i for i in self.inputs if i[0] == 'click']), 2)
        frames = [self.before, self.after] + [blank] * 40
        self.inputs.clear()
        self.assert_code(r.replay(self.surface(steps, frames=frames), steps, texts={}), 'LOW_CONFIDENCE', count=1)
        self.assertEqual(len([i for i in self.inputs if i[0] == 'click']), 1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_text_positive
    def test_text_positive(self):
        self.record([self.row(kind='text')])
        steps = self.steps()
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_text_positive.paste
        def paste(text, *, timeout):
            self.assertGreater(timeout, 0)
            self.inputs.append(('paste', text))
            return {'focus_verified': True, 'input_mono_ns': self.clock()}
        surface = self.surface(steps, paste=paste)
        result = r.replay(surface, steps, texts={1: 'CANARY'})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs, [('paste', 'CANARY')])
        self.assertNotIn('CANARY', json.dumps(cli.receipt_json(result)))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_text_transport_absent
    def test_text_transport_absent(self):
        self.record([self.row(kind='text')])
        steps = self.steps()
        surface = self.surface(steps)
        surface._paste = False
        self.assert_code(r.replay(surface, steps, texts={1: 'CANARY'}), 'TRANSPORT_UNAVAILABLE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_text_budgets
    def test_text_budgets(self):
        self.record([self.row(kind='text')])
        steps = self.steps()
        self.policy = replace(self.policy, text_bytes=5)
        surface = self.surface(steps, paste=lambda *_a, **_k: None)
        self.assert_code(r.replay(surface, steps, texts={1: 'CANARY'}), 'TEXT_BUDGET')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_unsupported_actions
    def test_unsupported_actions(self):
        for kind in ('dblclick',):
            self.record([self.row(1), self.row(2, kind=kind)])
            self.assert_code(self.run_steps(), 'UNSUPPORTED_ACTION')
        self.record([self.row(kind='paste')])
        self.refuses_load('UNSUPPORTED_ACTION')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_gesture_replay
    def test_gesture_replay(self):
        # Wheel and drag rows replay through input --scroll / --drag and verify by motion.
        self.record([self.row(1, kind='scroll', modifiers=['ctrl']), self.row(2, kind='drag')])
        self.assertEqual(self.run_steps().status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual([i[0] for i in self.inputs], ['scroll', 'drag'])
        self.assertEqual(self.inputs[0][1], {'point': '96,60', 'dir': 'down', 'modifier': 'ctrl'})
        self.assertEqual(self.inputs[1][1], {'span': '96,60:110,60', 'button': 1})
        self.inputs.clear()
        self.record([self.row(1, kind='scroll')])
        self.assert_code(self.run_steps(frames=[self.before] * 60), 'CHANGE_MISMATCH', count=1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_reacted_state_signal
    def test_reacted_state_signal(self):
        # Same region reacts but shows other content than the demo: accepted by the state signal.
        self.record()
        steps = self.steps()
        other = self.before.copy()
        other[50:60, 90:100] = ((self.before[50:60, 90:100].astype(np.int16) + 77) % 256).astype(np.uint8)
        self.assertEqual(r.replay(self.surface(steps, frames=[self.before, other]), steps, texts={}).status,
                         'VISUAL_TRANSITIONS_VERIFIED')
        self.inputs.clear()
        self.assert_code(r.replay(self.surface(steps, frames=[self.before] * 60), steps, texts={}),
                         'CHANGE_MISMATCH', count=1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_at_recorded
    def test_at_recorded(self):
        # A caller-reviewed click on generated content acts at the recorded point without a template match.
        self.record()
        steps = self.steps()
        blank = np.zeros_like(self.before)
        self.assert_code(r.replay(self.surface(steps, frames=[blank] * 4), steps, texts={}), 'LOW_CONFIDENCE')
        result = r.replay(self.surface(steps, frames=[blank, self.after]), steps, texts={}, at_recorded=frozenset({1}))
        self.assertEqual([i[0] for i in self.inputs], ['click'])
        self.assertEqual(self.inputs[0][1]['point'], '96,60')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_already_satisfied
    def test_already_satisfied(self):
        # The demo's end state is already on screen: no click, the step is reported as satisfied.
        self.record()
        result = self.run_steps(frames=[self.after] * 4)
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(result.receipts[0].disposition, 'ALREADY_SATISFIED')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_escape
    def test_escape(self):
        for key in ('Escape', 'Esc', 'escape', 'esc', 'ESC', 'ESCAPE'):
            self.record([self.row(kind='key', keysym=key)])
            self.assert_code(self.run_steps(key_policy=r.KeyPolicy('demo', 'w1', ('Escape',))), 'ESCAPE_UNSAFE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_key_vocabulary
    def test_key_vocabulary(self):
        for updates in ({'keysym': 'rm -rf'}, {'modifiers': ['ctrl', 'ctrl']}, {'modifiers': ['unknown']}):
            self.record([self.row(kind='key', **updates)])
            self.refuses_load('KEY_VOCABULARY')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_allowed_key
    def test_allowed_key(self):
        self.record([self.row(kind='key', keysym='Return')])
        result = self.run_steps()
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs, [('key', {'keysym': 'Return'})])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_selection
    def test_selection(self):
        self.record([self.row(1), self.row(2)])
        steps = self.steps()
        for ids in ('9', '1,1', '2,1'):
            with self.assertRaisesRegex(ValueError, '^SELECTION$'):
                cli._select(steps, ids)
        self.assertEqual(cli._select(steps, '2'), (steps[1],))
        self.assertEqual(cli._select(steps, None), steps)
        surface = self.surface(steps)
        self.assert_code(r.replay(surface, steps[::-1], texts={}), 'SELECTION')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_duplicate_record_selection
    def test_duplicate_record_selection(self):
        steps = self.steps() * 2
        self.assert_code(self.run_steps(steps), 'SELECTION')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_deadline
    def test_deadline(self):
        self.clock.jump = 300_000_000
        self.assert_code(self.run_steps(), 'DEADLINE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_near_deadline
    def test_near_deadline(self):
        self.policy = replace(self.policy, step_ns=60_000_000)
        self.assertEqual(self.run_steps().status, 'VISUAL_TRANSITIONS_VERIFIED')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_work_budget
    def test_work_budget(self):
        self.policy = replace(self.policy, limits=replace(d.DemoLimits(), candidates=1))
        self.assert_code(self.run_steps(), 'WORK_BUDGET')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_memory_budget
    def test_memory_budget(self):
        limits = replace(d.DemoLimits(), memory_bytes=1000)
        self.refuses_load('MEMORY_BUDGET', limits)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_retained_budget
    def test_retained_budget(self):
        rec = self.load(replace(d.DemoLimits(), retained_bytes=1000))
        with self.assertRaisesRegex(ValueError, '^RETAINED_BUDGET$'):
            d.segment_demo(rec)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_forged_record_revalidated
    def test_forged_record_revalidated(self):
        steps = self.steps()
        bad = replace(steps[0], event=replace(steps[0].event, x=True))
        self.assert_code(self.run_steps((bad,)), 'POINT_INTEGER')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_invalid_policy
    def test_invalid_policy(self):
        with self.assertRaisesRegex(ValueError, '^INVALID_POLICY$'):
            r.DemoIdentitySurface(
                SrfIdentitySurface(identity='demo', container='demo', window='w1'), selected_steps=(),
                policy=replace(self.policy, margin_ppm=0))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_deadline_transport
    def test_deadline_transport(self):
        identity = SrfIdentitySurface(identity='demo', container='demo', window='w1', transport=lambda *_: {})
        with self.assertRaisesRegex(ValueError, '^TRANSPORT_UNAVAILABLE$'):
            r.DemoIdentitySurface(identity, selected_steps=self.steps())

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_cleanup_describe_and_decode
    def test_cleanup_describe_and_decode(self):
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_cleanup_describe_and_decode.Broken
        class Broken:
            # @anchor:wa.learn.demo_learning_tests.DemoTests.test_cleanup_describe_and_decode.Broken.describe
            def describe(self, *_a, **_k):
                raise RuntimeError('PRIVATE_PATH')
        rec = self.load()
        descriptors = d.segment_demo(rec, describer=Broken())
        self.assertEqual(descriptors[0].target.mark.missing_reason, 'DESCRIBE_UNAVAILABLE')
        count = len(os.listdir('/proc/self/fd'))
        (self.root / '1_before.png').write_bytes(b'invalid')
        for _ in range(3):
            self.refuses_load('PNG_FORMAT')
        self.assertEqual(len(os.listdir('/proc/self/fd')), count)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_marks
    def test_marks(self):
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_marks.Marks
        class Marks:
            # @anchor:wa.learn.demo_learning_tests.DemoTests.test_marks.Marks.describe
            def describe(self, frame, *, prev):
                self.last = (frame, prev)
                return {'marks': [{'id': 7, 'x': 80, 'y': 40, 'w': 40, 'h': 40}]}
        marks = Marks()
        steps = d.segment_demo(self.load(), describer=marks)
        self.assertEqual(steps[0].target.mark.relative_xy, ((16, 40), (20, 40)))
        self.assertEqual(marks.last[1], None)
        with patch.object(marks, 'describe', return_value={'marks': [{'x': 80, 'y': 40, 'w': 40, 'h': 40}] * 2}):
            self.assertEqual(d.segment_demo(self.load(), describer=marks)[0].target.mark.missing_reason, 'MARK_AMBIGUOUS')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_summary_privacy
    def test_summary_privacy(self):
        steps = self.steps()
        output = json.dumps(cli.summarize(steps))
        for canary in ('/dev/shm', 'pixels', 'sha256', 'array', 'base64'):
            self.assertNotIn(canary, output)
        result = self.run_steps(steps)
        receipt = cli.receipt_json(result)
        self.assertLess(len(json.dumps(receipt['steps'][0]).encode()), 4096)
        self.assertIsInstance(receipt['steps'][0]['located_score_ppm'], int)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_cli
    def test_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main([str(self.root), '--summarize']), 0)
        self.assertEqual(json.loads(out.getvalue())['status'], 'SUMMARY')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main([str(self.root), '--replay']), 2)
        self.assertEqual(json.loads(out.getvalue())['failure_code'], 'EXPLICIT_IDENTITY_REQUIRED')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main([str(self.root), '--summarize', '--replay'])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_legacy
    def test_legacy(self):
        for fn, args, message in ((d.segment, (None,), 'v0 stub: vision-engine segmentation'),
                                  (d.label_intent, (None,), 'v0 stub: multimodal intent labelling'),
                                  (d.map_to_actions, (None,), 'v0 stub: reasoner -> structured actions'),
                                  (d.replay_and_verify, (None, None), 'v0 stub: headless replay + verification')):
            with self.assertRaisesRegex(NotImplementedError, '^' + message.replace('+', r'\+') + '$'):
                fn(*args)
        self.assertEqual(d.reproduced_share_ppm([{'reproduced': 1, 'verified': True}]), 0)
        self.assertEqual(d.reproduced_share_ppm([{'reproduced': True, 'verified': True}]), 1_000_000)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_window_dimension_change
    def test_window_dimension_change(self):
        self.record([self.row(1), self.row(2, window={'id': 'w1', 'w': 191, 'h': 120})])
        self.refuses_load('WINDOW_BINDING')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_timestamp_tie_positive
    def test_timestamp_tie_positive(self):
        self.record([self.row(1, mono_ns=5), self.row(2, mono_ns=5)])
        self.assertEqual(len(self.load().events), 2)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_png_dimensions_before_allocation
    def test_png_dimensions_before_allocation(self):
        (self.root / '1_before.png').write_bytes(png(self.before, dimensions=(999999, 192)))
        with patch.object(np, 'empty', side_effect=AssertionError('allocation before validation')):
            self.refuses_load('IMAGE_BUDGET')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_png_filters_and_rgba_positive
    def test_png_filters_and_rgba_positive(self):
        for channels in (3, 4):
            image = self.before if channels == 3 else np.concatenate(
                (self.before, np.full((120, 192, 1), 123, np.uint8)), axis=2)
            raw = bytearray()
            for y in range(image.shape[0]):
                method = y % 5
                raw.append(method)
                row = image[y].reshape(-1)
                previous = image[y - 1].reshape(-1) if y else np.zeros_like(row)
                for x in range(len(row)):
                    a = int(row[x - channels]) if x >= channels else 0
                    b = int(previous[x])
                    c = int(previous[x - channels]) if x >= channels else 0
                    q = a + b - c
                    distances = (abs(q - a), abs(q - b), abs(q - c))
                    predictor = (0, a, b, (a + b) // 2, (a, b, c)[distances.index(min(distances))])[method]
                    raw.append((int(row[x]) - predictor) & 255)
            data = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 192, 120, 8, 2 if channels == 3 else 6, 0, 0, 0))
                    + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
            (self.root / '1_before.png').write_bytes(data)
            self.assertEqual(len(self.load().events), 1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_file_changed_during_read
    def test_file_changed_during_read(self):
        original = os.fstat
        calls = [0]
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_file_changed_during_read.fstat
        def fstat(fd):
            st = original(fd)
            calls[0] += 1
            if calls[0] == 3:
                # @anchor:wa.learn.demo_learning_tests.DemoTests.test_file_changed_during_read.fstat.Changed
                class Changed:
                    st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns = st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns + 1, st.st_ctime_ns
                return Changed()
            return st
        with patch.object(os, 'fstat', fstat):
            self.refuses_load('FILE_CHANGED')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_all_descriptors_preflight
    def test_all_descriptors_preflight(self):
        self.record([self.row(1), self.row(2)])
        steps = self.steps()
        second = steps[1]
        flat = replace(second.target.small, pixels=np.zeros_like(second.target.small.pixels))
        second = replace(second, target=replace(second.target, small=flat))
        self.assert_code(self.run_steps((steps[0], second)), 'DEGENERATE_TEMPLATE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_aggregate_text_budget
    def test_aggregate_text_budget(self):
        self.record([self.row(1, kind='text'), self.row(2, kind='text')])
        steps = self.steps()
        self.policy = replace(self.policy, total_text_bytes=10)
        surface = self.surface(steps, paste=lambda *_a, **_k: None)
        self.assert_code(r.replay(surface, steps, texts={1: 'CANARY', 2: 'CANARY'}), 'TEXT_BUDGET')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_aggregate_retained_budget
    def test_aggregate_retained_budget(self):
        self.record([self.row(1), self.row(2)])
        steps = self.steps()
        one = (steps[0].target.small.pixels.nbytes + steps[0].target.context.pixels.nbytes
               + steps[0].expected_change.mask.nbytes + steps[0].expected_change.after_crop.nbytes)
        self.policy = replace(self.policy, limits=replace(d.DemoLimits(), retained_bytes=one + 1))
        self.assert_code(self.run_steps(steps), 'RETAINED_BUDGET')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_acquisition_bound
    def test_acquisition_bound(self):
        self.policy = replace(self.policy, limits=replace(d.DemoLimits(), acquisitions=2))
        surface = self.surface(self.steps(), frames=[self.before])
        result = r.replay(surface, surface._selected, texts={})
        self.assert_code(result, 'CHANGE_MISMATCH', 1)
        self.assertEqual(surface._frame_binding[0], 2)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_public_cli_selection
    def test_public_cli_selection(self):
        self.record([self.row(1), self.row(2)])
        args = [str(self.root), '--replay', '--identity', 'demo', '--container', 'demo', '--window', 'w1']
        for value in ('9', '1,1', '2,1'):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(args + ['--allow-steps', value]), 2)
            self.assertEqual(json.loads(out.getvalue())['failure_code'], 'SELECTION')
            self.assertEqual(self.inputs, [])
        steps = self.steps()[1:]
        self.assertEqual(self.run_steps(steps).status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(len(self.inputs), 1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_text_file_privacy
    def test_text_file_privacy(self):
        path = self.root / 'caller-text.json'
        path.write_text('{"1":"SECRET_TEXT_CANARY","1":"SECRET_TEXT_CANARY"}')
        args = [str(self.root), '--replay', '--identity', 'demo', '--container', 'demo', '--window', 'w1',
                '--paste-text-file', str(path)]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(args), 2)
        output = out.getvalue()
        self.assertEqual(json.loads(output)['failure_code'], 'CLOSED_SCHEMA')
        self.assertNotIn('SECRET', output)
        self.assertNotIn(str(path), output)
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_verify_integer_translation
    def test_verify_integer_translation(self):
        expected = self.steps()[0].expected_change
        result = r.verify_change(self.before, self.after, expected, translation=(True, 0), policy=self.policy)
        self.assertFalse(result.verified)
        self.assertEqual(result.failure_code, 'POINT_INTEGER')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_no_reuse_after_run
    def test_no_reuse_after_run(self):
        steps = self.steps()
        surface = self.surface(steps)
        self.assertEqual(r.replay(surface, steps, texts={}).status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assert_code(r.replay(surface, steps, texts={}), 'SELECTION', 1)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_poll_deadline_before_sleep
    def test_poll_deadline_before_sleep(self):
        self.policy = replace(self.policy, step_ns=40_000_000)
        surface = self.surface(self.steps())
        with patch.object(surface, '_pace') as pace:
            result = r.replay(surface, surface._selected, texts={})
        self.assert_code(result, 'DEADLINE', 1)
        pace.assert_not_called()

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_nonfinite_policy
    def test_nonfinite_policy(self):
        with self.assertRaisesRegex(ValueError, '^INVALID_POLICY$'):
            r.DemoIdentitySurface(SrfIdentitySurface(identity='demo', container='demo', window='w1'),
                                  selected_steps=self.steps(), policy=replace(self.policy, ncc_ppm=float('nan')))


    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_pointer_modifier_refusal
    def test_pointer_modifier_refusal(self):
        self.record([self.row(modifiers=['ctrl'])])
        self.assert_code(self.run_steps(), 'UNSUPPORTED_ACTION')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_forged_timestamp_order
    def test_forged_timestamp_order(self):
        self.record([self.row(1, mono_ns=5), self.row(2, mono_ns=5)])
        first, second = self.steps()
        second = replace(second, event=replace(second.event, mono_ns=4))
        self.assert_code(self.run_steps((first, second)), 'RECORDING_ORDER')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_final_event_cap
    def test_final_event_cap(self):
        (self.root / 'events.jsonl').write_text((json.dumps(self.row()) + '\n') * 501)
        self.refuses_load('EVENT_BUDGET')
        self.assertEqual(d.DemoLimits().events, 500)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_live_frame_missing_causal_evidence
    def test_live_frame_missing_causal_evidence(self):
        steps = self.steps()
        surface = self.surface(steps)
        original = surface._identity.frame
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_live_frame_missing_causal_evidence.frame
        def frame():
            data = original()
            data.pop('capture_mono_ns')
            return data
        surface._identity.frame = frame
        self.assert_code(r.replay(surface, steps, texts={}), 'FRAME_FRESHNESS')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_dispatch_checks_inside_owner_callback
    def test_dispatch_checks_inside_owner_callback(self):
        steps = self.steps()
        surface = self.surface(steps)
        original = surface._owner.dispatch
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_dispatch_checks_inside_owner_callback.dispatch
        def dispatch(adapter, action, send):
            # Simulate an authorization change after grounding but before callback.
            surface._selected = ()
            return original(adapter, action, send)
        surface._owner.dispatch = dispatch
        self.assert_code(r.replay(surface, steps, texts={}), 'SELECTION')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_dispatch_deadline_inside_callback
    def test_dispatch_deadline_inside_callback(self):
        steps = self.steps()
        surface = self.surface(steps)
        original = surface._owner.dispatch
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_dispatch_deadline_inside_callback.dispatch
        def dispatch(adapter, action, send):
            self.clock.now += 3_000_000_000
            return original(adapter, action, send)
        surface._owner.dispatch = dispatch
        self.assert_code(r.replay(surface, steps, texts={}), 'DEADLINE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_dispatch_frame_binding_inside_callback
    def test_dispatch_frame_binding_inside_callback(self):
        steps = self.steps()
        surface = self.surface(steps)
        original = surface._owner.dispatch
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_dispatch_frame_binding_inside_callback.dispatch
        def dispatch(adapter, action, send):
            surface._frame_binding = (-1, -1)
            return original(adapter, action, send)
        surface._owner.dispatch = dispatch
        self.assert_code(r.replay(surface, steps, texts={}), 'FRAME_FRESHNESS')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_deadline_loader_and_segmenter
    def test_deadline_loader_and_segmenter(self):
        self.refuses_load('DEADLINE', replace(d.DemoLimits(), operation_ns=1))
        recording = self.load()
        recording = replace(recording, limits=replace(d.DemoLimits(), operation_ns=1))
        with self.assertRaisesRegex(ValueError, '^DEADLINE$'):
            d.segment_demo(recording)


    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_negative_input_receipt
    def test_negative_input_receipt(self):
        result = self.run_steps(completion={'focus_verified': True, 'input_mono_ns': -1})
        self.assert_code(result, 'INPUT_RECEIPT', 1)
        self.assertTrue(result.input_uncertain)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_focus_receipt
    def test_focus_receipt(self):
        result = self.run_steps(completion={'focus_verified': False, 'input_mono_ns': self.clock()})
        self.assert_code(result, 'INPUT_UNCERTAIN', 1)
        self.assertTrue(result.input_uncertain)


    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_limits_and_policy_closed_budgets
    def test_limits_and_policy_closed_budgets(self):
        from dataclasses import fields
        for f in fields(d.DemoLimits()):
            self.refuses_load('INVALID_LIMITS', replace(d.DemoLimits(), **{f.name: True}))
        identity = SrfIdentitySurface(identity='demo', container='demo', window='w1')
        for f in fields(r.ReplayPolicy()):
            if f.name == 'limits':
                continue
            with self.assertRaisesRegex(ValueError, '^INVALID_POLICY$'):
                r.DemoIdentitySurface(identity, selected_steps=self.steps(),
                                      policy=replace(self.policy, **{f.name: True}))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_comparison_work_budget
    def test_comparison_work_budget(self):
        self.policy = replace(self.policy, limits=replace(d.DemoLimits(), comparisons=1))
        self.assert_code(self.run_steps(), 'WORK_BUDGET')


    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_unrelated_animation
    def test_unrelated_animation(self):
        animated = self.before.copy()
        animated[0:10, 0:10] ^= 255
        result = self.verify(animated)
        self.assertFalse(result.verified)
        self.assertEqual(result.failure_code, 'CHANGE_MISMATCH')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_text_error_privacy
    def test_text_error_privacy(self):
        self.record([self.row(kind='text')])
        steps = self.steps()
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_text_error_privacy.paste
        def paste(text, *, timeout):
            self.inputs.append(('paste', text))
            raise RuntimeError(text)
        surface = self.surface(steps, paste=paste)
        result = r.replay(surface, steps, texts={1: 'SECRET'})
        self.assert_code(result, 'INPUT_UNCERTAIN', 1)
        self.assertNotIn('SECRET', json.dumps(cli.receipt_json(result)))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_default_text_aggregate_cap
    def test_default_text_aggregate_cap(self):
        self.record([self.row(seq, kind='text', text_len=4096) for seq in range(1, 6)])
        steps = self.steps()
        surface = self.surface(steps, paste=lambda *_a, **_k: None)
        self.assert_code(r.replay(surface, steps, texts={seq: 'x' * 4096 for seq in range(1, 6)}), 'TEXT_BUDGET')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_default_text_boundary_positive
    def test_default_text_boundary_positive(self):
        self.record([self.row(kind='text', text_len=4096)])
        steps = self.steps()
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_default_text_boundary_positive.paste
        def paste(text, *, timeout):
            self.inputs.append(('paste', len(text)))
            return {'focus_verified': True, 'input_mono_ns': self.clock()}
        surface = self.surface(steps, paste=paste)
        self.assertEqual(r.replay(surface, steps, texts={1: 'x' * 4096}).status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs, [('paste', 4096)])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_policy_range_and_step_cap
    def test_policy_range_and_step_cap(self):
        identity = SrfIdentitySurface(identity='demo', container='demo', window='w1')
        for updates in ({'displacement': 49}, {'poll_ns': 49_000_000}, {'ncc_ppm': 919999},
                        {'margin_ppm': 49999}, {'iou_ppm': 499999}, {'coverage_ppm': 499999},
                        {'similarity_ppm': 949999}, {'step_ns': 2_000_000_001}, {'steps': 129}):
            with self.assertRaisesRegex(ValueError, '^INVALID_POLICY$'):
                r.DemoIdentitySurface(identity, selected_steps=self.steps(), policy=replace(self.policy, **updates))
        steps = self.steps() * 129
        self.assert_code(self.run_steps(steps), 'SELECTION')


    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_two_accepted_candidates
    def test_two_accepted_candidates(self):
        count = [0]
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_two_accepted_candidates.ncc
        def ncc(*_args):
            count[0] += 1
            return 1_000_000 if count[0] <= 2 else 940_000 if count[0] <= 4 else 0
        with patch.object(r, '_ncc', ncc):
            self.assert_code(self.run_steps(), 'AMBIGUOUS_TARGET')


    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_schema_irrelevant_fields
    def test_schema_irrelevant_fields(self):
        for updates in ({'keysym': 'PRIVATE'}, {'x2': 1}, {'dir': 'up'}, {'button': True}, {'modifiers': None}):
            self.record([self.row(**updates)])
            self.refuses_load('CLOSED_SCHEMA')
        self.record([self.row(kind='text', x=1)])
        self.refuses_load('CLOSED_SCHEMA')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_text_length_metadata
    def test_text_length_metadata(self):
        for value in (True, 0, -1, 4097):
            self.record([self.row(kind='text', text_len=value)])
            self.refuses_load('TEXT_LENGTH')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_combined_memory_envelope
    def test_combined_memory_envelope(self):
        steps = self.steps()
        self.policy = replace(self.policy, limits=replace(d.DemoLimits(), memory_bytes=2_000_000))
        self.assert_code(self.run_steps(steps), 'MEMORY_BUDGET')


    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_receipt_cap
    def test_receipt_cap(self):
        result = r.ReplayResult('REFUSED', None, (r.StepReceipt(1, 'REFUSED', failure_code='x' * 5000),))
        with self.assertRaisesRegex(ValueError, '^RECEIPT_BUDGET$'):
            cli.receipt_json(result)
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_strict_pixel_threshold
    def test_strict_pixel_threshold(self):
        self.after = self.before.copy()
        value = int(self.after[50, 90, 0])
        self.after[50, 90, 0] = value + 12 if value <= 243 else value - 12
        self.record()
        self.assert_code(self.run_steps(), 'CHANGE_UNOBSERVABLE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_verify_pixel_threshold
    def test_verify_pixel_threshold(self):
        after = self.before.copy()
        value = int(after[50, 90, 0])
        after[50, 90, 0] = value + 12 if value <= 242 else value - 12
        expected = d.ExpectedChange((90, 50, 91, 51), np.ones((1, 1), bool), after[50:51, 90:91])
        self.assertFalse(self.verify(after, expected).verified)
        after[50, 90, 0] = value + 13 if value <= 242 else value - 13
        expected = replace(expected, after_crop=after[50:51, 90:91])
        self.assertTrue(self.verify(after, expected).verified)
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_global_layout_movement
    def test_global_layout_movement(self):
        after = np.roll(self.before, 1, axis=1)
        after[50:60, 90:100] = self.after[50:60, 90:100]
        result = self.verify(after)
        self.assertFalse(result.verified)
        self.assertEqual(result.failure_code, 'CHANGE_MISMATCH')
        self.assertEqual(self.inputs, [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_later_text_preflight
    def test_later_text_preflight(self):
        self.record([self.row(1), self.row(2, kind='text')])
        self.assert_code(self.run_steps(), 'TEXT_SLOT')



    # @anchor:wa.learn.demo_learning_tests.DemoTests.shipped
    def shipped(self, *, after=True, reason='operator', kind='click'):
        row = self.row(kind=kind, window='w1', before='001_before.png',
                       after='001_after.png', reason=None)
        stop = dict(row, seq=2, mono_ns=2, kind='stop', x=None, y=None,
                    button=None, keysym=None, text_len=None, before=None, after=None, reason=reason)
        (self.root / 'events.jsonl').write_text(json.dumps(row) + '\n' + json.dumps(stop) + '\n')
        (self.root / '001_before.png').write_bytes(png(self.before))
        if after:
            (self.root / '001_after.png').write_bytes(png(self.after))
        (self.root / 'final.png').unlink()
        (self.root / 'final_w1.png').write_bytes(png(self.after))
        return row, stop

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_shipped_recorder
    def test_shipped_recorder(self):
        for reason in ('operator', 'limit', 'recorder_exited'):
            if not (self.root / 'final.png').exists():
                (self.root / 'final.png').write_bytes(png(self.after))
            self.shipped(reason=reason)
            steps = self.steps()
            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0].event.window_id, 'w1')
            self.assertTrue(steps[0].replayable)
            self.assertEqual(self.run_steps(steps).status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(len(self.inputs), 3)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_shipped_event_cap
    def test_shipped_event_cap(self):
        row, stop = self.shipped()
        self.assertEqual(len(d.load_demo(self.root, limits=replace(d.DemoLimits(), events=1)).events), 1)
        (self.root / 'events.jsonl').write_text(json.dumps(row) + '\n' + json.dumps(dict(row, seq=2)) + '\n' + json.dumps(dict(stop, seq=3)))
        self.refuses_load('EVENT_BUDGET', replace(d.DemoLimits(), events=1))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_shipped_missing_after
    def test_shipped_missing_after(self):
        self.shipped(after=False)
        recording = self.load()
        # A late asynchronous frame cannot upgrade the already loaded snapshot.
        (self.root / '001_after.png').write_bytes(png(self.after))
        steps = d.segment_demo(recording)
        self.assertEqual(steps[0].refusal_code, 'CHANGE_UNOBSERVABLE')
        self.assert_code(self.run_steps(steps), 'CHANGE_UNOBSERVABLE')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_shipped_custody
    def test_shipped_custody(self):
        row, stop = self.shipped()
        (self.root / '002_before.png').write_bytes(png(self.before))
        for name in ('../001_before.png', '/dev/shm/001_before.png', '002_before.png', '001_after.png'):
            (self.root / 'events.jsonl').write_text(json.dumps(dict(row, before=name)) + '\n' + json.dumps(stop))
            self.refuses_load('FILE_CUSTODY')
        (self.root / 'events.jsonl').write_text(json.dumps(row) + '\n' + json.dumps(stop))
        (self.root / '001_after.png').unlink()
        (self.root / '001_after.png').symlink_to(self.root / '1_after.png')
        self.refuses_load('FILE_CUSTODY')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_shipped_stop_validation
    def test_shipped_stop_validation(self):
        row, stop = self.shipped()
        for bad in (dict(stop, reason='unknown'), dict(stop, before='../secret'),
                    dict(stop, x=0), dict(stop, text='CANARY')):
            (self.root / 'events.jsonl').write_text(json.dumps(row) + '\n' + json.dumps(bad))
            self.refuses_load('CLOSED_SCHEMA')
        (self.root / 'events.jsonl').write_text(json.dumps(row) + '\n' + json.dumps(stop) + '\n' + json.dumps(dict(stop, seq=3, mono_ns=3)))
        self.refuses_load('RECORDING_ORDER')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_timeout_resets_between_steps
    def test_timeout_resets_between_steps(self):
        self.record([self.row(seq=1, kind='key'), self.row(seq=2, kind='key')])
        steps = self.steps()
        surface = self.surface(steps)
        original = surface._identity.frame
        timeouts = []
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_timeout_resets_between_steps.frame
        def frame():
            timeouts.append(surface._identity.timeout)
            result = original()
            if len(timeouts) == 2:
                self.clock.now += 1_800_000_000
            return result
        surface._identity.frame = frame
        result = r.replay(surface, steps, texts={})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(len(self.inputs), 2)
        self.assertGreater(timeouts[2], 1.9)
        self.assertEqual(surface._identity.timeout, surface._original_timeout)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_cli_text_replay
    def test_cli_text_replay(self):
        self.shipped(kind='text')
        steps = self.steps()
        text_file = self.root / 'caller.json'
        text_file.write_text(json.dumps({'1': 'CANARY'}))
        surface = self.surface(steps)
        real_init = r.DemoIdentitySurface.__init__
        # @anchor:wa.learn.demo_learning_tests.DemoTests.test_cli_text_replay.adapter
        def adapter(instance, identity, *, selected_steps, policy, paste_input=None, key_policy=None, text_encoding='ascii'):
            real_init(instance, identity, selected_steps=selected_steps, policy=policy,
                      clock=self.clock, pace=self.clock.pace, paste_input=paste_input, key_policy=key_policy, text_encoding=text_encoding)
        args = [str(self.root), '--replay', '--identity', 'demo', '--container', 'demo',
                '--window', 'w1', '--paste-text-file', str(text_file)]
        with patch.object(cli, 'SrfIdentitySurface', return_value=surface._identity), patch.object(r.DemoIdentitySurface, '__init__', adapter):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = cli.main(args)
        self.assertEqual(status, 0)
        # The CLI pastes caller text (rich editors corrupt per-character typing) [MEASURED, Stitch S5].
        self.assertEqual(self.inputs, [('type', {'text': 'CANARY', 'paste': True})])
        self.assertNotIn('CANARY', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['status'], 'VISUAL_TRANSITIONS_VERIFIED')


# Gate-removal controls execute the same public-API fixture under a single mutant.
# The normal suite must pass; each mutant must make its named fixture fail.

    # @anchor:wa.learn.demo_learning_tests.DemoTests.projection
    def projection(self, steps=None, **kwargs):
        return d.project_demo_steps(self.steps() if steps is None else steps,
                                    demo_id='demo-1', site_id='site-1', **kwargs)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.export_cli
    def export_cli(self, extra=(), describer=None):
        from types import SimpleNamespace
        describer = describer or SimpleNamespace(_marks_dir=None, engine=SimpleNamespace(timeout=None),
                                                 describe=lambda *_a, **_k: {'marks': []})
        output = io.StringIO()
        with patch.object(cli, 'BridgeDescriber', return_value=describer), contextlib.redirect_stdout(output):
            status = cli.main([str(self.root), '--export-steps', str(self.root / 'steps.json'),
                               '--demo-id', 'demo-1', '--site-id', 'site-1', *extra])
        return status, output.getvalue()

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_closed_projection
    def test_export_closed_projection(self):
        projection = self.projection()
        document = projection.document
        self.assertEqual(set(document), {'schema', 'demo_id', 'site_id', 'steps'})
        self.assertEqual(document['schema'], 'mw.demo_steps.v1')
        self.assertEqual(set(document['steps'][0]), {'seq', 'kind', 'button', 'recorded_point_px',
                         'modifiers', 'key_name', 'text_len', 'gap_before_ms', 'gap_after_ms',
                         'expected_change_box_px', 'mark'})
        self.assertEqual(document['steps'][0]['recorded_point_px'], [96, 60])
        self.assertEqual(document['steps'][0]['expected_change_box_px'], [90, 50, 100, 60])
        # Injected forbidden keys into a caller copy never enter the immutable payload.
        for key in ('text', 'pixels', 'text_slot', 'at_recorded'):
            document['steps'][0][key] = 'CANARY'
        self.assertNotEqual(document, projection.document)
        self.assertNotIn('CANARY', projection.payload.decode())
        with self.assertRaises(Exception):
            projection.payload = b'bad'
        self.assertEqual(projection.payload, (json.dumps(projection.document, sort_keys=True,
                         separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode())

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_ids
    def test_export_ids(self):
        steps = self.steps()
        for value in ('demo\n', '../private', 'Upper', '', 'a' * 64):
            for field in ('demo_id', 'site_id'):
                ids = dict(demo_id='demo-1', site_id='site-1')
                ids[field] = value
                with self.assertRaisesRegex(ValueError, '^EXPORT_ID$'):
                    d.project_demo_steps(steps, **ids)
        self.assertEqual(self.projection().document['site_id'], 'site-1')
        self.assertEqual(self.export_cli(('--demo-id', 'x\n'))[0], 2)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_integer_types
    def test_export_integer_types(self):
        step = self.steps()[0]
        for bad in (replace(step, step_id=0, event=replace(step.event, seq=0)),
                    replace(step, event=replace(step.event, x=True)),
                    replace(step, event=replace(step.event, mono_ns=1.5)),
                    replace(step, expected_change=replace(step.expected_change, bbox=(True, 50, 100, 60)))):
            with self.assertRaises(ValueError):
                self.projection((bad,))
        self.record([self.row(1, x=0, y=0), self.row(2, x=191, y=119)])
        rows = self.projection().document['steps']
        self.assertEqual([row['recorded_point_px'] for row in rows], [[0, 0], [191, 119]])
        self.assertTrue(all(type(v) is int for row in rows for v in row['recorded_point_px']))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_event_mapping
    def test_export_event_mapping(self):
        rows = [self.row(1), self.row(2, button=3), self.row(3, kind='dblclick'),
                self.row(4, kind='text'), self.row(5, kind='key'),
                self.row(6, kind='scroll'), self.row(7, kind='drag')]
        self.record(rows)
        projection = self.projection()
        exported = projection.document['steps']
        self.assertEqual([s['kind'] for s in exported],
                         ['click', 'right_click', 'double_click', 'paste', 'key', 'scroll', 'drag'])
        self.assertEqual(exported[1]['button'], 3)
        self.assertEqual(exported[3]['text_len'], 6)
        self.assertIsNone(exported[3]['recorded_point_px'])
        self.assertEqual(exported[4]['key_name'], 'Return')
        self.assertIn((6, 'SCROLL_DIRECTION_OMITTED'), projection.omissions)
        self.assertIn((7, 'DRAG_ENDPOINT_OMITTED'), projection.omissions)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_recorded_gaps
    def test_export_recorded_gaps(self):
        self.record([self.row(1, mono_ns=1), self.row(2, mono_ns=2_100_001),
                     self.row(3, mono_ns=9_900_001), self.row(4, mono_ns=9_900_001)])
        steps = self.steps()
        rows = self.projection(steps).document['steps']
        self.assertEqual([s['gap_before_ms'] for s in rows], [0, 2, 7, 0])
        self.assertEqual([s['gap_after_ms'] for s in rows], [2, 7, 0, 0])
        with self.assertRaisesRegex(ValueError, '^RECORDING_ORDER$'):
            self.projection((steps[0], replace(steps[1], event=replace(steps[1].event, mono_ns=0))))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_slots_and_at_recorded
    def test_export_slots_and_at_recorded(self):
        self.record([self.row(1), self.row(2, kind='text'),
                     self.row(3, kind='key', keysym='v', modifiers=['ctrl'])])
        steps = self.steps()
        projection = self.projection(steps, at_recorded=frozenset((1,)))
        self.assertEqual(projection.text_slots, ((2, 'slot-2'), (3, 'slot-3')))
        self.assertEqual(projection.at_recorded, (1,))
        self.assertNotIn('at_recorded', projection.document)
        with self.assertRaisesRegex(ValueError, '^SELECTION$'):
            self.projection(steps, at_recorded=frozenset((2,)))
        status, output = self.export_cli(('--at-recorded', '1'))
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)['at_recorded'], [1])
        self.assertNotIn('T1', output)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_missing_evidence
    def test_export_missing_evidence(self):
        from types import SimpleNamespace
        valid = {'id': 7, 'kind': 'button', 'x': 90, 'y': 50, 'w': 20, 'h': 20}
        describer = SimpleNamespace(describe=lambda *_a, **_k: {'marks': [valid]})
        steps = d.segment_demo(self.load(), describer=describer)
        mark = self.projection(steps).document['steps'][0]['mark']
        self.assertEqual(set(mark), {'mark_id', 'kind', 'box_px', 'relative_xy_bps',
                                    'ocr_text', 'ocr_score_promille', 'missing_reason'})
        self.assertEqual(mark['mark_id'], '7')
        self.assertEqual(mark['kind'], 'button')
        self.assertEqual(mark['box_px'], [90, 50, 110, 70])   # D2 [x0, y0, x1, y1]
        self.assertEqual(mark['relative_xy_bps'], [3000, 5000])
        self.assertIsNone(mark['ocr_text'])
        self.assertEqual(mark['missing_reason'], 'OCR_UNAVAILABLE')
        with patch.object(describer, 'describe', side_effect=RuntimeError('SECRET')):
            projection = self.projection(d.segment_demo(self.load(), describer=describer))
        self.assertIsNone(projection.document['steps'][0]['mark'])
        self.assertIn((1, 'DESCRIBE_UNAVAILABLE'), projection.omissions)
        self.assertNotIn('SECRET', str(projection))
        self.after = self.before.copy()
        self.record()
        projection = self.projection()
        self.assertIsNone(projection.document['steps'][0]['expected_change_box_px'])
        self.assertIn((1, 'CHANGE_UNOBSERVABLE'), projection.omissions)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_float_mark_rejected
    def test_export_float_mark_rejected(self):
        from types import SimpleNamespace
        mark = dict(id=7, kind='field', x=90, y=50, w=20, h=20, score=0.97,
                    ocr_text='SECRET', path='/private/SECRET')
        describer = SimpleNamespace(describe=lambda *_a, **_k: {'marks': [mark]})
        projection = self.projection(d.segment_demo(self.load(), describer=describer))
        exported = projection.document['steps'][0]['mark']
        self.assertIsNone(exported['ocr_score_promille'])
        self.assertIsNone(exported['ocr_text'])
        self.assertEqual(exported['kind'], 'field')
        self.assertEqual(exported['missing_reason'], 'OCR_UNAVAILABLE')
        self.assertNotIn('0.97', projection.payload.decode())
        self.assertNotIn('SECRET', str(projection))
        mark['kind'] = 'SECRET'
        projection = self.projection(d.segment_demo(self.load(), describer=describer))
        self.assertIsNone(projection.document['steps'][0]['mark'])
        self.assertIn((1, 'MARK_KIND_UNAVAILABLE'), projection.omissions)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_cap
    def test_export_cap(self):
        base = self.steps()[0]
        def steps(count):
            return tuple(replace(base, step_id=i, event=replace(base.event, seq=i)) for i in range(1, count + 1))
        last = None
        for count in range(1, 501):
            try:
                result = self.projection(steps(count))
            except ValueError as error:
                self.assertEqual(str(error), 'EXPORT_BUDGET')
                break
            last = result
        else:
            self.fail('oversized document was accepted')
        self.assertLessEqual(len(last.payload), 65_536)
        self.assertEqual(len(last.document['steps']), count - 1)
        self.assertEqual(last.payload[-1:], b'\n')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_no_dispatch
    def test_export_no_dispatch(self):
        with patch.object(cli, 'SrfIdentitySurface', side_effect=AssertionError('live construction')) as identity:
            status, _ = self.export_cli()
        self.assertEqual(status, 0)
        identity.assert_not_called()
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.export_cli(('--paste-text-file', '/PRIVATE'))[0], 2)
        self.assertEqual(self.inputs, [])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.export_cli(('--replay',))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_atomic_custody
    def test_export_atomic_custody(self):
        destination = self.root / 'steps.json'
        destination.write_bytes(b'old')
        self.assertEqual(self.export_cli()[0], 0)
        self.assertEqual(json.loads(destination.read_bytes())['schema'], 'mw.demo_steps.v1')
        destination.write_bytes(b'old')
        with patch.object(cli.os, 'write', side_effect=OSError('SECRET')):
            self.assertEqual(self.export_cli()[0], 2)
        self.assertEqual(destination.read_bytes(), b'old')
        self.assertEqual(list(self.root.glob('.demo-*')), [])
        destination.unlink()
        protected = self.root / 'protected'
        protected.write_bytes(b'protected')
        destination.symlink_to(protected)
        self.assertEqual(self.export_cli()[0], 2)
        self.assertTrue(destination.is_symlink())
        self.assertEqual(protected.read_bytes(), b'protected')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_unicode_slots
    def test_unicode_slots(self):
        value = 'España € 秘密\n\tASCII'
        self.assertEqual(d.validate_text_slot(value, expected_length=len(value), max_bytes=4096), value.encode())
        self.assertEqual(d.validate_text_slot('e\u0301', expected_length=2, max_bytes=4096), 'e\u0301'.encode())
        for bad in ('x\0', 'x\x1b', 'x\ud800', 'x\u202e', 'x\u2066', 'x\x7f', 'x\x85'):
            with self.assertRaisesRegex(ValueError, '^TEXT_ENCODING$'):
                d.validate_text_slot(bad, expected_length=None, max_bytes=4096)
        for bad in (None, b'ASCII', True):
            with self.assertRaisesRegex(ValueError, '^TEXT_SLOT$'):
                d.validate_text_slot(bad, expected_length=None, max_bytes=4096)
        with self.assertRaisesRegex(ValueError, '^TEXT_SLOT$'):
            d.validate_text_slot(value, expected_length=1, max_bytes=4096)
        self.record([self.row(kind='text', text_len=len(value))])
        steps = self.steps()
        result = r.replay(self.surface(steps, text_encoding='utf-8'), steps, texts={1: value})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs[-1], ('type', {'text': value}))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_backend_encoding_preflight
    def test_backend_encoding_preflight(self):
        self.record([self.row(1), self.row(2, kind='text', text_len=1)])
        steps = self.steps()
        result = r.replay(self.surface(steps), steps, texts={2: '€'})
        self.assert_code(result, 'TEXT_ENCODING')
        result = r.replay(self.surface(steps, text_encoding='utf-8'), steps, texts={2: '€'})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs[-1], ('type', {'text': '€'}))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_unicode_byte_budgets
    def test_unicode_byte_budgets(self):
        self.assertEqual(len(d.validate_text_slot('é' * 2048, expected_length=2048, max_bytes=4096)), 4096)
        with self.assertRaisesRegex(ValueError, '^TEXT_BUDGET$'):
            d.validate_text_slot('é' * 2049, expected_length=2049, max_bytes=4096)
        self.record([self.row(i, kind='text', text_len=2048) for i in range(1, 6)])
        steps = self.steps()
        result = r.replay(self.surface(steps, text_encoding='utf-8'), steps, texts={i: 'é' * 2048 for i in range(1, 6)})
        self.assert_code(result, 'TEXT_BUDGET')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_key_policy_isolation
    def test_key_policy_isolation(self):
        self.record([self.row(kind='key', keysym='Escape')])
        steps = self.steps()
        keys = ['ESC']
        policy = r.KeyPolicy('demo', 'w1', keys)
        keys.clear()
        self.assertEqual(policy.forbidden_keys, ('escape',))
        self.assert_code(r.replay(self.surface(steps, key_policy=policy), steps, texts={}), 'ESCAPE_UNSAFE')
        for identity, window in (('other', 'w1'), ('demo', 'other')):
            with self.assertRaisesRegex(ValueError, '^KEY_POLICY$'):
                self.surface(steps, key_policy=r.KeyPolicy(identity, window, ('Escape',)))
        result = r.replay(self.surface(steps), steps, texts={})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs, [('key', {'keysym': 'Escape'})])
        self.inputs.clear()
        self.record([self.row(kind='key')])
        steps = self.steps()
        self.assert_code(r.replay(self.surface(steps, key_policy=r.KeyPolicy('demo', 'w1', ('Return',))), steps, texts={}), 'KEY_POLICY')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_key_policy_dispatch_recheck
    def test_key_policy_dispatch_recheck(self):
        self.record([self.row(kind='key', keysym='Escape')])
        steps = self.steps()
        surface = self.surface(steps)
        original = surface._owner.dispatch
        def dispatch(owner_surface, action, send):
            surface._key_policy = r.KeyPolicy('demo', 'w1', ('Escape',))
            return original(owner_surface, action, send)
        with patch.object(surface._owner, 'dispatch', dispatch):
            self.assert_code(r.replay(surface, steps, texts={}), 'ESCAPE_UNSAFE')
        result = r.replay(self.surface(steps), steps, texts={})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(len(self.inputs), 1)
        self.inputs.clear()
        self.record([self.row(kind='key', keysym='v', modifiers=['ctrl'])])
        steps = self.steps()
        surface = self.surface(steps)
        original = surface._owner.dispatch
        def deny_paste(owner_surface, action, send):
            surface._key_policy = r.KeyPolicy('demo', 'w1', ('v',))
            return original(owner_surface, action, send)
        with patch.object(surface._owner, 'dispatch', deny_paste):
            self.assert_code(r.replay(surface, steps, texts={1: 'A'}), 'KEY_POLICY')
        result = r.replay(self.surface(steps), steps, texts={1: 'A'})
        self.assertEqual(result.status, 'VISUAL_TRANSITIONS_VERIFIED')
        self.assertEqual(self.inputs, [('type', {'text': 'A'})])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_integer_score_sources
    def test_integer_score_sources(self):
        result = self.run_steps()
        receipt = result.receipts[0]
        self.assertEqual(receipt.located_score, 1_000_000)
        self.assertEqual(receipt.verify_score, 1_000_000)
        self.assertTrue(all(type(v) is int for v in (receipt.located_score, receipt.margin, receipt.verify_score)))
        self.assertEqual(cli.receipt_json(result)['steps'][0]['verify_score_ppm'], 1_000_000)
        for value in (0.97, float('nan'), True):
            with patch.object(r, '_ncc', return_value=value):
                self.assert_code(self.run_steps(), 'NONFINITE_SCORE', count=1)
            for factory in (lambda: r.LocateResult(score=value), lambda: r.VerifyResult(True, value),
                            lambda: r.StepReceipt(1, 'REFUSED', verify_score=value)):
                with self.assertRaisesRegex(ValueError, '^NONINTEGER_SCORE$'):
                    factory()
        from types import SimpleNamespace
        bad = SimpleNamespace(step_id=1, disposition='REFUSED', located_score=0.97, margin=None,
                              verify_score=None, elapsed_ns=0, failure_code=None)
        with self.assertRaisesRegex(ValueError, '^NONINTEGER_SCORE$'):
            cli.receipt_json(r.ReplayResult('REFUSED', None, (bad,)))

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_integer_score_thresholds
    def test_integer_score_thresholds(self):
        self.policy = replace(self.policy, displacement=0)
        result = self.locate()
        self.assertEqual(result.score, 1_000_000)
        self.assertIsNone(result.runner_up)
        self.assertIsNone(result.margin)
        for value, accepted in ((920_000, True), (919_999, False)):
            with patch.object(r, '_ncc', return_value=value):
                result = self.locate()
            self.assertEqual(result.failure_code is None, accepted)
        self.policy = replace(self.policy, displacement=1)
        # 969999.9 vs 920000.1 cannot manufacture a 50000ppm margin.
        count = [0]
        def scores(*_args):
            count[0] += 1
            return 969_999 if count[0] <= 2 else 920_000 if count[0] <= 4 else 0
        with patch.object(r, '_ncc', scores):
            self.assertEqual(self.locate().failure_code, 'AMBIGUOUS_TARGET')
        # Neither runner reaches the confidence threshold. Only the conservative
        # margin can distinguish the insufficient floor-gap from its boundary.
        for best, accepted in ((960_000, False), (960_001, True)):
            count = [0]
            def quantized(*_args):
                count[0] += 1
                return best if count[0] <= 2 else 910_000 if count[0] <= 4 else 0
            with patch.object(r, '_ncc', quantized):
                self.assertEqual(self.locate().failure_code is None, accepted)
        self.policy = replace(self.policy, displacement=3)
        self.test_repeated_target()
        self.test_degenerate_template()

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_ram_mount_proof
    def test_ram_mount_proof(self):
        original = Path.read_text
        def mount(kind):
            def read(path, *args, **kwargs):
                value = original(path, *args, **kwargs)
                if str(path) == '/proc/self/mountinfo':
                    value = value.replace(' - tmpfs ', ' - ' + kind + ' ')
                return value
            return read
        self.assertEqual(len(self.load().events), 1)
        for kind in ('ext4', 'unknown'):
            with patch.object(Path, 'read_text', mount(kind)):
                self.refuses_load('RAM_REQUIRED')
        steps = self.steps()
        with patch.object(Path, 'read_text', mount('ext4')):
            self.assert_code(r.replay(self.surface(steps), steps, texts={}), 'RAM_REQUIRED')
        def mismatched(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if str(path) == '/proc/self/mountinfo':
                lines = []
                for line in value.splitlines():
                    row = line.split()
                    if row[row.index('-') + 1] == 'tmpfs':
                        row[2] = '999:999'
                    lines.append(' '.join(row))
                return '\n'.join(lines)
            return value
        with patch.object(Path, 'read_text', mismatched):
            self.refuses_load('RAM_REQUIRED')
        def missing(path, *args, **kwargs):
            if str(path).startswith('/proc/self/fdinfo/'):
                return 'pos: 0\n'
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', missing):
            self.refuses_load('RAM_REQUIRED')

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_privacy
    def test_export_privacy(self):
        from types import SimpleNamespace
        mark = dict(id=3, kind='button', x=90, y=50, w=20, h=20, text='SECRET_CANARY',
                    path='/PRIVATE_CANARY', pixels='PIXEL_CANARY', score=0.97)
        describer = SimpleNamespace(_marks_dir=None, engine=SimpleNamespace(timeout=None),
                                    describe=lambda *_a, **_k: {'marks': [mark], 'SECRET_CANARY': 'PIXEL_CANARY'})
        status, output = self.export_cli(describer=describer)
        self.assertEqual(status, 0)
        payload = (self.root / 'steps.json').read_text()
        self.assertEqual(json.loads(payload)['steps'][0]['mark']['kind'], 'button')
        for canary in ('SECRET_CANARY', 'PRIVATE_CANARY', 'PIXEL_CANARY', str(self.root)):
            self.assertNotIn(canary, payload + output)
        with patch.object(describer, 'describe', side_effect=RuntimeError('SECRET_CANARY')):
            status, output = self.export_cli(describer=describer)
        self.assertEqual(status, 0)
        self.assertIn('DESCRIBE_UNAVAILABLE', output)
        self.assertNotIn('SECRET_CANARY', output)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_deadline_cleanup
    def test_export_deadline_cleanup(self):
        from types import SimpleNamespace
        for phase in ('success', 'describe', 'projection', 'write'):
            directory = tempfile.TemporaryDirectory(dir=self.root)
            self.addCleanup(directory.cleanup)
            location = Path(directory.name)
            describer = SimpleNamespace(_marks_dir=directory, engine=SimpleNamespace(timeout=None))
            elapsed = [False]
            real_clock = cli.time.monotonic_ns
            start = real_clock()
            def clock():
                return start + d.DemoLimits().operation_ns * 2 if elapsed[0] else real_clock()
            def describe(*_args, **_kwargs):
                self.assertGreater(describer.engine.timeout, 0)
                if phase == 'describe':
                    elapsed[0] = True
                return {'marks': []}
            describer.describe = describe
            original = d.project_demo_steps
            def project(*args, **kwargs):
                result = original(*args, **kwargs)
                if phase == 'projection':
                    elapsed[0] = True
                return result
            write = os.write
            def writer(*args):
                if phase == 'write':
                    elapsed[0] = True
                return write(*args)
            with patch.object(cli.time, 'monotonic_ns', clock), patch.object(d, 'project_demo_steps', project), patch.object(os, 'write', writer):
                status, output = self.export_cli(describer=describer)
            self.assertEqual(status, 0 if phase == 'success' else 2, phase)
            if phase != 'success':
                self.assertEqual(json.loads(output)['failure_code'], 'DEADLINE')
            self.assertFalse(location.exists())
            self.assertTrue(self.root.exists())
            self.assertEqual(list(self.root.glob('.demo-*')), [])

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_reseal_negative_control
    def test_reseal_negative_control(self):
        from tools import reseal
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            root = Path(temporary)
            book = root / 'book'
            book.mkdir()
            (root / 'COMPANION_FORMAT.json').write_text(json.dumps({'expected_books': ['book']}))
            (book / 'book.md').write_text('original')
            (book / 'manifest.json').write_text(reseal.dump(reseal.manifest_for(book)))
            with patch.object(reseal, 'HB', root), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reseal.main(['--check']), 0)
                (book / 'book.md').write_text('changed')
                self.assertEqual(reseal.main(['--check']), 4)

    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_export_serialization_guards
    def test_export_serialization_guards(self):
        steps = self.steps()
        self.assertEqual(self.projection(steps).document['steps'][0]['recorded_point_px'], [96, 60])
        original = d._export_box
        for value in (True, 1.5, float('nan')):
            def corrupted(box, length):
                return [value, 60] if length == 2 else original(box, length)
            with patch.object(d, '_export_box', corrupted):
                with self.assertRaisesRegex(ValueError, '^EXPORT_INTEGER$'):
                    self.projection(steps)
        constrained = replace(d.DemoLimits(), memory_bytes=1)
        with patch.object(d, 'DemoLimits', return_value=constrained):
            with self.assertRaisesRegex(ValueError, '^MEMORY_BUDGET$'):
                self.projection(steps)


    # @anchor:wa.learn.demo_learning_tests.DemoTests.test_cli_key_policy_file
    def test_cli_key_policy_file(self):
        self.record([self.row(kind='key')])
        path = self.root / 'policy.json'
        identity = self.surface(self.steps())._identity
        original = r.DemoIdentitySurface.__init__
        def adapter(instance, identity, **kwargs):
            original(instance, identity, clock=self.clock, pace=self.clock.pace, **kwargs)
        def invoke():
            output = io.StringIO()
            with patch.object(cli, 'SrfIdentitySurface', return_value=identity), patch.object(r.DemoIdentitySurface, '__init__', adapter), contextlib.redirect_stdout(output):
                status = cli.main([str(self.root), '--replay', '--identity', 'demo', '--container', 'demo',
                                   '--window', 'w1', '--key-policy-file', str(path)])
            return status, json.loads(output.getvalue())
        valid = dict(identity='demo', window='w1', forbidden_keys=['Escape'])
        path.write_text(json.dumps(valid))
        self.assertEqual(invoke()[0], 0)
        self.assertEqual(self.inputs, [('key', {'keysym': 'Return'})])
        self.inputs.clear()
        for value in (dict(valid, forbidden_keys=['Return']), dict(valid, identity='other'),
                      dict(valid, window='other'), dict(valid, text='SECRET')):
            path.write_text(json.dumps(value))
            status, output = invoke()
            self.assertEqual(status, 2)
            code = output.get('failure_code') or output.get('steps', [{}])[-1].get('failure_code')
            self.assertEqual(code, 'KEY_POLICY')
            self.assertEqual(self.inputs, [])
        saved = self.root / 'saved-policy.json'
        saved.write_text(json.dumps(valid))
        path.unlink()
        path.symlink_to(saved)
        status, output = invoke()
        self.assertEqual(status, 2)
        self.assertEqual(output['failure_code'], 'FILE_CUSTODY')
        self.assertEqual(self.inputs, [])



REQUIRE_MUTANTS = {
    'TEXT_LENGTH': 'test_text_length_metadata', 'INVALID_LIMITS': 'test_limits_and_policy_closed_budgets', 'CLOSED_SCHEMA': 'test_closed_schema', 'POINT_INTEGER': 'test_boolean_and_fractional_coordinate',
    'EVENT_INTEGER': 'test_event_integer', 'IMAGE_BUDGET': 'test_oversized_dimensions',
    'PNG_DECOMPRESSION': 'test_png_decompression_bomb', 'PNG_FORMAT': 'test_png_structure',
    'PNG_CRC': 'test_png_structure', 'PNG_FILTER': 'test_png_structure', 'PNG_UNSUPPORTED': 'test_png_unsupported',
    'EVENT_BUDGET': 'test_excessive_rows', 'FILE_BUDGET': 'test_file_bound',
    'FILE_CUSTODY': 'test_outside_ram', 'RECORDING_ORDER': 'test_recording_order',
    'WINDOW_BINDING': 'test_window_binding', 'FRAME_DIMENSIONS': 'test_frame_dimensions',
    'FINAL_MISMATCH': 'test_final_integrity', 'FILE_CHANGED': 'test_record_changed',
    'DEGENERATE_TEMPLATE': 'test_degenerate_template', 'NONFINITE_SCORE': 'test_nonfinite_score',
    'CROP_GEOMETRY': 'test_crop_offset', 'CHANGE_UNOBSERVABLE': 'test_unobservable',
    'FRAME_FRESHNESS': 'test_freshness', 'TEXT_SLOT': 'test_text_slots', 'TEXT_ENCODING': 'test_text_slots',
    'TEXT_BUDGET': 'test_text_budgets', 'UNSUPPORTED_ACTION': 'test_unsupported_actions',
    'ESCAPE_UNSAFE': 'test_escape', 'KEY_VOCABULARY': 'test_key_vocabulary', 'SELECTION': 'test_selection',
    'DEADLINE': 'test_deadline', 'WORK_BUDGET': 'test_work_budget', 'MEMORY_BUDGET': 'test_memory_budget',
    'RETAINED_BUDGET': 'test_retained_budget', 'INVALID_POLICY': 'test_invalid_policy',
    'TRANSPORT_UNAVAILABLE': 'test_deadline_transport',
}
EXTRA_REQUIRE_MUTANTS = (
    ('EXPLICIT_IDENTITY_REQUIRED', 'test_cli', 'explicit_identity'),
    ('RECEIPT_BUDGET', 'test_receipt_cap', 'receipt_budget'),
    ('TEXT_SLOT', 'test_later_text_preflight', 'all_text_preflight'),
    ('MEMORY_BUDGET', 'test_combined_memory_envelope', 'combined_memory'),
    ('SELECTION', 'test_dispatch_checks_inside_owner_callback', 'selection_callback'),
    ('DEADLINE', 'test_dispatch_deadline_inside_callback', 'deadline_callback'),
    ('FRAME_FRESHNESS', 'test_dispatch_frame_binding_inside_callback', 'frame_callback'),
    ('POINT_INTEGER', 'test_integer_dispatch', 'integer_callback'),
    ('INPUT_RECEIPT', 'test_negative_input_receipt', 'input_receipt'),
    ('RECORDING_ORDER', 'test_forged_timestamp_order', 'timestamp_revalidation'),
    ('DEGENERATE_TEMPLATE', 'test_all_descriptors_preflight', 'whole_selection_preflight'),
    ('RETAINED_BUDGET', 'test_aggregate_retained_budget', 'aggregate_descriptors'),
    ('TEXT_BUDGET', 'test_aggregate_text_budget', 'aggregate_text'),
    ('DEADLINE', 'test_deadline_loader_and_segmenter', 'decode_deadline'),
    ('DEADLINE', 'test_poll_deadline_before_sleep', 'poll_deadline'),
    ('CLOSED_SCHEMA', 'test_text_file_privacy', 'text_file_privacy'),
    ('UNSUPPORTED_ACTION', 'test_pointer_modifier_refusal', 'unsupported_modifiers'),
)
SOURCE_MUTANTS = (
    (d, 'load_demo', '0 < count <= limits.events + 1', '0 < count <= limits.events', 'test_shipped_event_cap', 'terminal_row_budget'),

    (r.DemoIdentitySurface, '_timeout', 'min(self._original_timeout, remaining /', 'min(self._identity.timeout, remaining /', 'test_timeout_resets_between_steps', 'timeout_reset'),
    (r.DemoIdentitySurface, '_send', "else self._identity._call('type', text=text)", 'else None', 'test_default_text_actuator', 'default_text_actuator'),
    (d, '_frame_name', "and int(name.split('_')[0]) == seq", 'and True', 'test_shipped_custody', 'recorder_frame_sequence'),
    (d, 'load_demo', "_require(not stopped, 'RECORDING_ORDER')", 'pass', 'test_shipped_stop_validation', 'terminal_stop'),
    (d, 'load_demo', "row['reason'] in ('operator', 'limit', 'recorder_exited')", 'True', 'test_shipped_stop_validation', 'stop_reason'),
    (d, 'segment_demo', "if after_name in hashes:", 'if True:', 'test_shipped_missing_after', 'async_after'),
    (d, 'load_demo', "final_name = f'final_{binding[0]}.png'", "final_name = 'final.png'", 'test_shipped_recorder', 'recorder_final'),

    (cli, 'main', 'parser.add_mutually_exclusive_group(required=True)', 'parser', 'test_cli', 'exclusive_modes'),
    (d, '_owned', 'return np.frombuffer(a.tobytes(), dtype=a.dtype).reshape(a.shape)', 'return a', 'test_complete_recording_and_immutable_descriptors', 'immutable_pixels'),
    (d, 'segment_demo', '> 12, axis=2)', '> 0, axis=2)', 'test_strict_pixel_threshold', 'mask_pixel_threshold'),
    (r, 'verify_change', '> policy.pixel_threshold, axis=2)', '> 0, axis=2)', 'test_verify_pixel_threshold', 'verify_pixel_threshold'),
    (r, 'verify_change', 'intersection * PPM >= policy.iou_ppm * union', 'True', 'test_global_layout_movement', 'global_layout'),
    (r, '_search', ' or (runner_value is not None and min(PPM, runner_value + 1) >= policy.ncc_ppm)', '', 'test_two_accepted_candidates', 'exactly_one_candidate'),
    (d, '_read', 'os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK', 'os.O_RDONLY | os.O_NONBLOCK', 'test_symlink_and_missing', 'symlink_custody'),
    (r, 'verify_change', 'return VerifyResult(ok, score,', 'return VerifyResult(True, score,', 'test_unrelated_animation', 'unrelated_animation'),
    (r, 'replay', "else 'INPUT_UNCERTAIN' if sending else 'REPLAY_UNAVAILABLE'", 'else str(e)', 'test_text_error_privacy', 'error_privacy'),
    (r, 'replay', 'range(surface.policy.limits.acquisitions - 1)', 'range(39)', 'test_acquisition_bound', 'acquisition_budget'),
    (r.DemoIdentitySurface, '_send', 'result = self._identity._validated_input(result, e.kind)', 'result = result', 'test_focus_receipt', 'completion_focus'),
    (d, 'segment_demo', "mark = MarkDescriptor(missing_reason='DESCRIBE_UNAVAILABLE')", 'mark = MarkDescriptor()', 'test_cleanup_describe_and_decode', 'describe_cleanup'),
    (d, '_mark', "'MARK_AMBIGUOUS' if matches else 'MARKS_UNAVAILABLE'", "'MARKS_UNAVAILABLE'", 'test_marks', 'mark_ambiguity'),
    (r, '_search', 'ox, oy = target.small.point_offset_xy', 'ox, oy = (target.small.pixels.shape[1] // 2, target.small.pixels.shape[0] // 2)', 'test_crop_offset', 'edge_offset'),
    (cli, 'summarize', "return {'status': 'SUMMARY',", "return {'path': '/dev/shm/PRIVATE', 'status': 'SUMMARY',", 'test_summary_privacy', 'summary_privacy'),
    (r, '_search', 'if best < policy.ncc_ppm:', 'if False:', 'test_low_confidence', 'confidence'),
    (r, '_search', 'if (margin is not None and margin < policy.margin_ppm) or (runner_value is not None and min(PPM, runner_value + 1) >= policy.ncc_ppm):', 'if False:', 'test_repeated_target', 'uniqueness'),
    (r, '_search', 'score = min(scores)', 'score = max(scores)', 'test_context_disagreement', 'context'),
    (r, 'verify_change', 'covered * PPM >= policy.coverage_ppm * support', 'True', 'test_change_coverage', 'coverage'),
    (r, 'verify_change', 'intersection * PPM >= policy.iou_ppm * union', 'True', 'test_change_iou', 'iou'),
    (r, 'verify_change', 'similarity * PPM >= policy.similarity_ppm * maximum', 'True', 'test_change_similarity', 'similarity'),
    (r, 'replay', "d._require(verified is not None and verified.verified, 'CHANGE_MISMATCH')", 'pass', 'test_first_failure_stops', 'postcheck'),
    (r, 'replay', 'tuple(receipts), sending)', 'tuple(receipts), False)', 'test_uncertain', 'uncertainty'),
    (r, 'replay', 'surface.close()', 'pass', 'test_complete_recording_and_immutable_descriptors', 'cleanup'),
    (d, 'reproduced_share_ppm', 's.get("reproduced") is True', 's.get("reproduced")', 'test_legacy', 'legacy'),
)


# Each row removes or corrupts one release check; the public-API control must fail.
MWP0_SOURCE_MUTANTS = (
    (d, 'project_demo_steps', "dict(schema='mw.demo_steps.v1',", "dict(text='SECRET', schema='mw.demo_steps.v1',", 'test_export_closed_projection', 'closed_projection.root'),
    (d, 'project_demo_steps', 'rows.append(dict(seq=e.seq,', "rows.append(dict(pixels='SECRET', seq=e.seq,", 'test_export_closed_projection', 'closed_projection.pixels'),
    (d, 'project_demo_steps', 'rows.append(dict(seq=e.seq,', "rows.append(dict(text_slot='SECRET', seq=e.seq,", 'test_export_closed_projection', 'closed_projection.slot'),
    (d, '_export_id', 're.fullmatch(', 're.match(', 'test_export_ids', 'ids.fullmatch'),
    (d, 'project_demo_steps', '_export_id(demo_id)', 'pass', 'test_export_ids', 'ids.demo'),
    (d, 'project_demo_steps', '_export_id(site_id)', 'pass', 'test_export_ids', 'ids.site'),
    (d, 'project_demo_steps', "_require(_integer(step.step_id, 1) and step.step_id == e.seq, 'EXPORT_INTEGER')", 'pass', 'test_export_integer_types', 'integer_types.seq'),
    (d, 'project_demo_steps', '_validate_event(e, DemoLimits())', 'pass', 'test_export_integer_types', 'integer_types.event'),
    (d, '_export_box', 'return list(value)', 'return [False] * length', 'test_export_integer_types', 'integer_types.box'),
    (d, 'project_demo_steps', "kind = 'right_click'", "kind = 'click'", 'test_export_event_mapping', 'event_mapping.secondary'),
    (d, 'project_demo_steps', "'dblclick': 'double_click'", "'dblclick': 'click'", 'test_export_event_mapping', 'event_mapping.double'),
    (d, 'project_demo_steps', "'text': 'paste'", "'text': 'text'", 'test_export_event_mapping', 'event_mapping.paste'),
    (d, 'project_demo_steps', "_require(previous is None or (e.seq > previous.seq and e.mono_ns >= previous.mono_ns), 'RECORDING_ORDER')", 'pass', 'test_export_recorded_gaps', 'recorded_gaps.order'),
    (d, 'project_demo_steps', '(e.mono_ns - previous.mono_ns) // 1_000_000', '100', 'test_export_recorded_gaps', 'recorded_gaps.fixed'),
    (d, 'project_demo_steps', "rows[-1]['gap_after_ms'] = gap", "rows[-1]['gap_after_ms'] = 0", 'test_export_recorded_gaps', 'recorded_gaps.following'),
    (d, 'project_demo_steps', "f'slot-{e.seq}'", "'slot-1'", 'test_export_slots_and_at_recorded', 'slots_and_at_recorded.ids'),
    (d, 'project_demo_steps', 'tuple(sorted(at_recorded))', '()', 'test_export_slots_and_at_recorded', 'slots_and_at_recorded.selection'),
    (d, 'project_demo_steps', "and s.event.kind == 'click' for s in steps", 'for s in steps', 'test_export_slots_and_at_recorded', 'slots_and_at_recorded.click_only'),
    (d, 'segment_demo', "mark = MarkDescriptor(missing_reason='DESCRIBE_UNAVAILABLE')", "mark = MarkDescriptor(7, (90, 50, 20, 20), ((6, 20), (10, 20)), None, 'button')", 'test_export_missing_evidence', 'missing_evidence.fabrication'),
    (d, 'segment_demo', "mark = MarkDescriptor(missing_reason='DESCRIBE_UNAVAILABLE')", 'mark = MarkDescriptor()', 'test_export_missing_evidence', 'missing_evidence.reason'),
    (d, 'project_demo_steps', 'box_px=[mb[0], mb[1], mb[0] + mb[2], mb[1] + mb[3]],', 'box_px=[0, 0, 1, 1],', 'test_export_missing_evidence', 'missing_evidence.geometry'),
    (d, 'project_demo_steps', 'ocr_score_promille=None', 'ocr_score_promille=970', 'test_export_float_mark_rejected', 'float_mark_rejected.score'),
    (d, 'project_demo_steps', 'ocr_text=None', "ocr_text='SECRET'", 'test_export_float_mark_rejected', 'float_mark_rejected.text'),
    (d, 'project_demo_steps', "missing_reason='OCR_UNAVAILABLE'", 'missing_reason=None', 'test_export_float_mark_rejected', 'float_mark_rejected.reason'),
    (d, '_export_payload', "_require(len(payload) <= 65_536, 'EXPORT_BUDGET')", 'pass', 'test_export_cap', 'cap.removal'),
    (d, '_export_payload', 'len(payload) <= 65_536', 'len(payload) <= 650_000', 'test_export_cap', 'cap.raised'),
    (d, '_export_payload', 'return payload', 'return payload[:100]', 'test_export_cap', 'cap.truncation'),
    (cli, 'main', "if args.export_steps:\n            selected", "if args.export_steps:\n            SrfIdentitySurface()\n            selected", 'test_export_no_dispatch', 'no_dispatch.identity'),
    (cli, 'main', 'args.paste_text_file is None and args.allow_steps is None', 'args.allow_steps is None', 'test_export_no_dispatch', 'no_dispatch.paste_file'),
    (cli, 'main', 'parser.add_mutually_exclusive_group(required=True)', 'parser', 'test_export_no_dispatch', 'no_dispatch.exclusivity'),
    (cli, '_atomic_export', "d._require(stat.S_ISREG(target.st_mode), 'FILE_CUSTODY')", 'pass', 'test_export_atomic_custody', 'atomic_custody.symlink'),
    (cli, '_atomic_export', 'os.replace(temporary, destination.name, src_dir_fd=parent, dst_dir_fd=parent)', 'os.unlink(temporary, dir_fd=parent)', 'test_export_atomic_custody', 'atomic_custody.replace'),
    (cli, '_atomic_export', 'count = os.write(fd, payload[offset:])', "os.replace(temporary, destination.name, src_dir_fd=parent, dst_dir_fd=parent)\n                count = os.write(fd, payload[offset:])", 'test_export_atomic_custody', 'atomic_custody.partial'),
    (d, 'validate_text_slot', "_require(type(value) is str, 'TEXT_SLOT')", 'pass', 'test_unicode_slots', 'unicode_slots.type'),
    (d, 'validate_text_slot', "_require(expected_length is None or (_integer(expected_length) and len(value) == expected_length), 'TEXT_SLOT')", 'pass', 'test_unicode_slots', 'unicode_slots.length'),
    (d, 'validate_text_slot', 'payload = value.encode(', "payload = value.replace('e\u0301', 'é').encode(", 'test_unicode_slots', 'unicode_slots.normalization'),
    (r, '_preflight', "value.encode(surface._text_encoding, errors='strict')", "value.encode('utf-8', errors='strict')", 'test_backend_encoding_preflight', 'backend_encoding_preflight.encoding'),
    (r, '_preflight', "d._require(False, 'TEXT_ENCODING')", 'pass', 'test_backend_encoding_preflight', 'backend_encoding_preflight.refusal'),
    (r.DemoIdentitySurface, '__init__', 'self._text_encoding = text_encoding', "self._text_encoding = 'utf-8'", 'test_backend_encoding_preflight', 'backend_encoding_preflight.declaration'),
    (d, 'validate_text_slot', "_require(len(payload) <= max_bytes, 'TEXT_BUDGET')", 'pass', 'test_unicode_byte_budgets', 'unicode_byte_budgets.slot'),
    (r, '_preflight', "d._require(text_bytes <= p.total_text_bytes, 'TEXT_BUDGET')", 'pass', 'test_unicode_byte_budgets', 'unicode_byte_budgets.aggregate'),
    (r, '_preflight', 'text_bytes += n', 'text_bytes += len(value)', 'test_unicode_byte_budgets', 'unicode_byte_budgets.characters'),
    (r.DemoIdentitySurface, '__init__', 'key_policy.identity == identity.identity and key_policy.window == identity.window', 'True', 'test_key_policy_isolation', 'key_policy_isolation.binding'),
    (r.KeyPolicy, '__post_init__', "tuple(sorted(set(_canonical_key(k) for k in self.forbidden_keys)))", 'self.forbidden_keys', 'test_key_policy_isolation', 'key_policy_isolation.copy'),
    (r.DemoIdentitySurface, '__init__', 'self._key_policy = None if key_policy is None else KeyPolicy(key_policy.identity, key_policy.window, key_policy.forbidden_keys)', 'self._key_policy = None', 'test_key_policy_isolation', 'key_policy_isolation.preflight'),
    (r.DemoIdentitySurface, '_send', 'self._check_key(e.keysym)', 'pass', 'test_key_policy_dispatch_recheck', 'key_policy_dispatch_recheck.callback'),
    (r.DemoIdentitySurface, '_check_key', 'policy = self._key_policy', 'policy = None', 'test_key_policy_dispatch_recheck', 'key_policy_dispatch_recheck.custody'),
    (r.DemoIdentitySurface, '_check_key', "d._require(_canonical_key(key) not in policy.forbidden_keys, code)", 'pass', 'test_key_policy_dispatch_recheck', 'key_policy_dispatch_recheck.membership'),
    (r, '_scores', "d._require(all(getattr(record, name) is None or (type(getattr(record, name)) is int\n                   and -1_000_000 <= getattr(record, name) <= 2_000_000) for name in names), 'NONINTEGER_SCORE')", 'pass', 'test_integer_score_sources', 'integer_score_sources.record'),
    (r, '_search', "d._require(all(type(s) is int and -PPM <= s <= PPM for s in scores), 'NONFINITE_SCORE')", 'pass', 'test_integer_score_sources', 'integer_score_sources.source'),
    (cli, '_ppm', 'return value', 'return None if value is None else value * 1_000_000', 'test_integer_score_sources', 'integer_score_sources.transport'),
    (r, '_search', 'margin = best - min(PPM, runner + 1) if runner is not None else None', 'margin = best - runner if runner is not None else None', 'test_integer_score_thresholds', 'integer_score_thresholds.rounding'),
    (r, '_search', 'if best < policy.ncc_ppm:', 'if False:', 'test_integer_score_thresholds', 'integer_score_thresholds.confidence'),
    (r, '_search', 'margin = best - min(PPM, runner + 1) if runner is not None else None', 'margin = best - runner if runner is not None else 2_000_000', 'test_integer_score_thresholds', 'integer_score_thresholds.missing'),
    (r, '_search', 'if (margin is not None and margin < policy.margin_ppm) or (runner_value is not None and min(PPM, runner_value + 1) >= policy.ncc_ppm):', 'if False:', 'test_integer_score_thresholds', 'integer_score_thresholds.unique'),
    (d, '_root', '_ram_fd(fd)', 'pass', 'test_ram_mount_proof', 'ram_mount_proof.root'),
    (d, '_ram_fd', "row[row.index('-') + 1] in ('tmpfs', 'ramfs')", 'True', 'test_ram_mount_proof', 'ram_mount_proof.filesystem'),
    (d, '_ram_fd', '(major, minor) == (os.major(device), os.minor(device))', 'True', 'test_ram_mount_proof', 'ram_mount_proof.device'),
    (d, 'project_demo_steps', 'ocr_text=None', "ocr_text='SECRET_CANARY'", 'test_export_privacy', 'privacy.text'),
    (d, 'project_demo_steps', "dict(schema='mw.demo_steps.v1',", "dict(path='/PRIVATE_CANARY', schema='mw.demo_steps.v1',", 'test_export_privacy', 'privacy.path'),
    (cli, 'main', "dict(status='EXPORTED', text_slots=projection.text_slots,", "dict(pixels='PIXEL_CANARY', status='EXPORTED', text_slots=projection.text_slots,", 'test_export_privacy', 'privacy.status'),
    (cli, '_deadline', "d._require(time.monotonic_ns() < deadline, 'DEADLINE')", 'pass', 'test_export_deadline_cleanup', 'deadline_cleanup.deadline'),
    (cli, 'main', 'describer._marks_dir.cleanup()', 'pass', 'test_export_deadline_cleanup', 'deadline_cleanup.workspace'),
    (cli, '_atomic_export', "if opened:\n            os.unlink(temporary, dir_fd=parent)", 'if False:\n            os.unlink(temporary, dir_fd=parent)', 'test_export_deadline_cleanup', 'deadline_cleanup.temporary'),
)
MWP0_SOURCE_MUTANTS += (
    (d, '_symbolic_value', '(str, int)', '(str, int, float, bool)', 'test_export_serialization_guards', 'serialization_guards.types'),
    (d, '_symbolic_value', 'return all(_symbolic_value(item) for item in value)', 'return True', 'test_export_serialization_guards', 'serialization_guards.recursion'),
    (d, 'project_demo_steps', "_require(len(steps) * 8192 + 65_536 * 4 <= DemoLimits().memory_bytes, 'MEMORY_BUDGET')", 'pass', 'test_export_serialization_guards', 'serialization_guards.memory'),
)
MWP0_SOURCE_MUTANTS += (
    (cli, '_key_policy', "d._require(type(value) is dict and value.keys() == {'identity', 'window', 'forbidden_keys'}, 'KEY_POLICY')", 'pass', 'test_cli_key_policy_file', 'cli_key_policy_file.schema'),
    (cli, '_key_policy', 'policy = r.KeyPolicy(**value)', "policy = r.KeyPolicy(identity, window, value['forbidden_keys'])", 'test_cli_key_policy_file', 'cli_key_policy_file.binding'),
    (cli, '_key_policy', 'policy = r.KeyPolicy(**value)', "policy = r.KeyPolicy(value['identity'], value['window'], ())", 'test_cli_key_policy_file', 'cli_key_policy_file.restrictions'),
)
MWP0_SOURCE_MUTANTS += (
    (reseal, 'main', 'return 4 if (stale and "--check" in argv) else 0', 'return 0', 'test_reseal_negative_control', 'reseal_negative_control.exit'),
    (reseal, 'main', 'if have != want:', 'if False:', 'test_reseal_negative_control', 'reseal_negative_control.comparison'),
    (reseal, 'manifest_for', 'hashlib.sha256(p.read_bytes()).hexdigest()', "hashlib.sha256(b'constant').hexdigest()", 'test_reseal_negative_control', 'reseal_negative_control.digest'),
)
SOURCE_MUTANTS += MWP0_SOURCE_MUTANTS


# @anchor:wa.learn.demo_learning_tests.ControlMutants
class ControlMutants(unittest.TestCase):
    # @anchor:wa.learn.demo_learning_tests.ControlMutants.test_gate_removal_mutants
    def test_gate_removal_mutants(self):
        outcomes = []
        cases = [(code, test, code) for code, test in REQUIRE_MUTANTS.items()] + list(EXTRA_REQUIRE_MUTANTS)
        controls = {test for _code, test, _label in cases} | {row[4] for row in SOURCE_MUTANTS}
        for test in sorted(controls):
            baseline = unittest.TestResult()
            DemoTests(test).run(baseline)
            self.assertTrue(baseline.wasSuccessful(), (test, baseline.failures, baseline.errors))
        for code, test, label in cases:
            original = d._require
            # @anchor:wa.learn.demo_learning_tests.ControlMutants.test_gate_removal_mutants.unchecked
            def unchecked(ok, reason):
                if reason != code:
                    original(ok, reason)
            with patch.object(d, '_require', unchecked):
                result = unittest.TestResult()
                DemoTests(test).run(result)
            outcomes.append((label, not result.wasSuccessful()))
        for module, name, old, new, test, label in SOURCE_MUTANTS:
            source = textwrap.dedent(inspect.getsource(getattr(module, name)))
            self.assertIn(old, source)
            namespace = dict(getattr(module, name).__globals__)
            exec(compile(source.replace(old, new, 1), '<wdemo-mutant>', 'exec'), namespace)
            compiled = namespace[name]
            # Keep the real module globals so public-API fixture patches reach the mutant.
            # A detached globals copy would bypass patched adapters and give false kills.
            mutant = types.FunctionType(compiled.__code__, getattr(module, name).__globals__,
                                        name, compiled.__defaults__, compiled.__closure__)
            mutant.__kwdefaults__ = compiled.__kwdefaults__
            with patch.object(module, name, mutant):
                result = unittest.TestResult()
                DemoTests(test).run(result)
            outcomes.append((label, not result.wasSuccessful()))
        self.assertEqual([label for label, killed in outcomes if not killed], [])
        print('WDEMO_MUTANTS ' + json.dumps(dict(outcomes), sort_keys=True))


if __name__ == '__main__':
    unittest.main()
