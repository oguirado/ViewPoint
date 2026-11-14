# @module {"id":"wa.tests.test_mw_handlers_unit","layer":4,"owner":"LEARN","deps":["wa.learn.mw_handlers","wa.perceive.vision_bridge"],"book":"HB-WA-s4-learn"}
"""Positive controls and falsifying fixtures through the semantic public API."""
from dataclasses import replace
import inspect
import textwrap
import unittest
from unittest.mock import patch
from wender.learn import mw_handlers as h
from wender.perceive.vision_bridge import Mark


def _predicate(cid='mw.ocr.text_present.v1', **bindings):
    if not bindings and cid != 'mw.frame.stable.v1':
        bindings = {'phrase': 'hello world'}
    return dict(kind='state_signal' if cid == 'mw.frame.stable.v1' else 'mark_label',
                capability_id=cid, comparison='truth', snapshot_ref=None, evidence_refs=[],
                bindings=[dict(name=k, type='string' if k == 'phrase' else
                               'integer_box' if k == 'region_px' else 'integer', value=v)
                          for k, v in bindings.items()])


def _mark(i, text, x, y=0, w=10, height=10):
    return Mark(i, x, y, w, height, 'text', 1000, text, 1000, None, None)


def _frame(**changes):
    return replace(h.SignalFrame(1, 1, 2, 200, 100, 'a' * 64,
                   (_mark(1, 'hello', 0), _mark(2, 'world', 20)), True), **changes)


# @anchor:wa.s4.mw_handlers.TestSignals
class TestSignals(unittest.TestCase):
    def _parse(self, predicate):
        return h.parse_signal(predicate, metadata_bytes=4096, string_bytes=4096)

    def _evaluate(self, predicate=None, frame=None):
        owner = h.SignalHandlers(max_marks=64, metadata_bytes=4096)
        return owner.evaluate(self._parse(predicate or _predicate()), frame or _frame(),
                              now_ns=10, deadline_ns=100)

    # @anchor:wa.s4.mw_handlers.TestSignals.test_closed_binding
    def test_closed_binding(self):
        good = _predicate()
        self.assertTrue(self._evaluate(good))
        bad = [dict(good, kind='unknown'), dict(good, kind='state_signal'),
               dict(good, capability_id='unknown'), dict(good, capability_id=[]), dict(good, comparison='changed'),
               dict(good, snapshot_ref='other'), dict(good, bindings=good['bindings'] * 2),
               _predicate(other='x'), _predicate('mw.frame.stable.v1', k=True), _predicate('mw.frame.stable.v1', stable_ms=True),
               dict(good, bindings=[dict(name='phrase', type='integer', value='hello world')]),
               _predicate('mw.frame.stable.v1', k=1), _predicate('mw.frame.stable.v1', stable_ms=0)]
        for predicate in bad:
            with self.subTest(predicate=predicate), self.assertRaises(h.SignalRefused):
                self._parse(predicate)

    # @anchor:wa.s4.mw_handlers.TestSignals.test_phrase_normalization
    def test_phrase_normalization(self):
        for text in ('CAFÉ', 'cafe\u0301'):
            self.assertTrue(self._evaluate(_predicate(phrase=text), _frame(marks=(_mark(1, 'café', 0),))))
        for text in ('  ', 'x' * 4097, 'é' * 2049):
            with self.assertRaises(h.SignalRefused):
                self._parse(_predicate(phrase=text))

    # @anchor:wa.s4.mw_handlers.TestSignals.test_joining
    def test_joining(self):
        self.assertTrue(self._evaluate())
        self.assertTrue(self._evaluate(frame=_frame(marks=(_mark(1, 'hello', 0), _mark(2, 'world', 25)))))
        for marks in ((_mark(1, 'hello', 0), _mark(2, 'world', 20, 20)),
                      (_mark(1, 'hello', 0), _mark(2, 'world', 26)),
                      (_mark(1, 'helloworld', 0),), (_mark(1, 'hello worlds', 0),)):
            self.assertFalse(self._evaluate(frame=_frame(marks=marks)))
        self.assertTrue(self._evaluate(frame=_frame(marks=tuple(reversed(_frame().marks)))))

    # @anchor:wa.s4.mw_handlers.TestSignals.test_region
    def test_region(self):
        self.assertTrue(self._evaluate(_predicate(phrase='hello world', region_px=[0, 0, 30, 10])))
        for box in ([0, 0, 29, 10], [40, 0, 100, 20]):
            self.assertFalse(self._evaluate(_predicate(phrase='hello world', region_px=box)))
        for box in ([0, 0, 0, 10], [True, 0, 30, 10], [0, 0, 201, 100]):
            with self.assertRaises(h.SignalRefused):
                self._evaluate(_predicate(phrase='hello', region_px=box))

    # @anchor:wa.s4.mw_handlers.TestSignals.test_absence
    def test_absence(self):
        predicate = _predicate('mw.ocr.text_absent.v1')
        self.assertTrue(self._evaluate(predicate, _frame(marks=(_mark(1, 'unrelated', 0),))))
        self.assertTrue(self._evaluate(predicate, _frame(marks=(), ocr_complete=True)))
        self.assertFalse(self._evaluate(predicate))
        for frame in (_frame(ocr_complete=False), _frame(marks=(), ocr_complete=False),
                      _frame(marks=(replace(_mark(1, 'unrelated', 0), missing_reason='truncated'),))):
            with self.assertRaises(h.SignalRefused):
                self._evaluate(predicate, frame)

    # @anchor:wa.s4.mw_handlers.TestSignals.test_capped_ocr
    def test_capped_ocr(self):
        try:
            self.assertTrue(self._evaluate(frame=_frame(ocr_complete=False)))
        except h.SignalRefused:
            self.fail("Capped matching presence must remain provable")
        self.assertFalse(self._evaluate(frame=_frame(marks=(_mark(1, 'other', 0),))))
        for cid in ('mw.ocr.text_present.v1', 'mw.ocr.text_absent.v1'):
            with self.assertRaises(h.SignalRefused) as caught:
                self._evaluate(_predicate(cid), _frame(marks=(_mark(1, 'other', 0),), ocr_complete=False))
            self.assertEqual(caught.exception.reason, h.Reason.MISSING)
        with self.assertRaises(h.SignalRefused):
            self._evaluate(_predicate('mw.ocr.text_absent.v1'), _frame(ocr_complete=False))

    # @anchor:wa.s4.mw_handlers.TestSignals.test_geometry_and_resources
    def test_geometry_and_resources(self):
        self.assertTrue(self._evaluate())
        with self.assertRaises(h.SignalRefused): self._evaluate(frame=_frame(frame_sha256='bad'))
        with self.assertRaises(h.SignalRefused): self._evaluate(frame=_frame(marks=(object(),)))
        for marks in ((_mark(1, 'hello', 0), _mark(1, 'world', 20)),
                      (_mark(1, 'hello', 0), _mark(2, 'world', 5)),
                      (_mark(1, 'hello', -1),), (_mark(1, 'hello', True),),
                      (_mark(1, 'x' * 4097, 0),), tuple(_mark(i, 'x', 0) for i in range(65))):
            with self.assertRaises(h.SignalRefused):
                self._evaluate(frame=_frame(marks=marks))
        bounded = h.SignalHandlers(max_marks=64, metadata_bytes=20000)
        marks = tuple(_mark(i, 'x', i, w=1, height=1) for i in range(65))
        with self.assertRaises(h.SignalRefused):
            bounded.evaluate(self._parse(_predicate(phrase='x')), _frame(marks=marks), now_ns=10, deadline_ns=100)
        self.assertTrue(bounded.evaluate(self._parse(_predicate(phrase='x')), _frame(marks=marks[:64]), now_ns=10, deadline_ns=100))
        owner = h.SignalHandlers(max_marks=1, metadata_bytes=257)
        with self.assertRaises(h.SignalRefused):
            owner.evaluate(self._parse(_predicate(phrase='xx')), _frame(marks=(_mark(1, 'xx', 0),)), now_ns=10, deadline_ns=100)
        self.assertTrue(owner.evaluate(self._parse(_predicate(phrase='x')),
                       _frame(marks=(_mark(1, 'x', 0),)), now_ns=10, deadline_ns=100))

    # @anchor:wa.s4.mw_handlers.TestSignals.test_stability
    def test_stability(self):
        spec = self._parse(_predicate('mw.frame.stable.v1', k=3, stable_ms=2))
        owner = h.SignalHandlers(max_marks=64, metadata_bytes=4096)
        def feed(seq, stamp, digest='a' * 64, selected=spec):
            return owner.evaluate(selected, _frame(sequence=seq, capture_start_ns=stamp,
                                  capture_end_ns=stamp, frame_sha256=digest),
                                  now_ns=stamp, deadline_ns=100_000_000)
        self.assertFalse(feed(1, 1))
        self.assertFalse(feed(2, 2_000_001)) # duration without count
        self.assertTrue(feed(3, 3_000_001))
        with self.assertRaises(h.SignalRefused): feed(3, 4_000_001)
        self.assertFalse(feed(4, 4_000_001))
        self.assertFalse(feed(5, 4_000_002))
        self.assertFalse(feed(6, 4_000_003)) # count without duration
        # Text acquisitions feed the same streak and changed frames reset it.
        self.assertTrue(feed(7, 5_000_001, 'b' * 64, self._parse(_predicate())))
        self.assertFalse(feed(8, 6_000_001))
        self.assertFalse(feed(9, 7_000_001))
        self.assertTrue(feed(10, 8_000_001))
        owner.invalidate()
        self.assertFalse(feed(11, 9_000_001))
        owner.clear()
        self.assertFalse(feed(12, 10_000_001))
        with self.assertRaises(h.SignalRefused):
            owner.evaluate(spec, _frame(sequence=13, frame_sha256='bad'), now_ns=10, deadline_ns=100)
        self.assertFalse(feed(14, 11_000_001))

    # @anchor:wa.s4.mw_handlers.TestSignals.test_deadline
    def test_deadline(self):
        self.assertTrue(self._evaluate())
        for now in (100, 101):
            with self.assertRaises(h.SignalRefused):
                h.SignalHandlers(max_marks=64, metadata_bytes=4096).evaluate(
                    self._parse(_predicate()), _frame(), now_ns=now, deadline_ns=100)

    # @anchor:wa.s4.mw_handlers.TestSignals.test_gate_mutants
    def test_gate_mutants(self):
        # Each mutation runs the same public-API control, without modifying disk.
        mutations = [
            (h.SignalHandlers._evaluate, 'else present', 'else True', 'test_capped_ocr'),
            (h.SignalHandlers._evaluate, "spec.capability_id == 'mw.ocr.text_absent.v1' or not present", 'True', 'test_capped_ocr'),
            (h.SignalHandlers._evaluate, 'frame.ocr_complete is True', 'True', 'test_capped_ocr'),
            (h.parse_signal, "predicate.get('kind') == _IDS[cid]", 'True', 'test_closed_binding'),
            (h.parse_signal, 'len(value.encode(\'utf-8\')) <= min(string_bytes, metadata_bytes, 4096)', 'True', 'test_phrase_normalization'),
            (h.SignalHandlers._evaluate, '4 * abs(mark.y + mark.h - baseline) <= min(height, mark.h)', 'True', 'test_joining'),
            (h.SignalHandlers._evaluate, '2 * gap <= 3 * min(previous.h, mark.h)', 'True', 'test_joining'),
            (h.SignalHandlers._evaluate, 'm.x + m.w <= region[2]', 'True', 'test_region'),
            (h.SignalHandlers._evaluate, 'frame.ocr_complete is True', 'True', 'test_absence'),
            (h.SignalHandlers._evaluate, 'mark.id not in ids', 'True', 'test_geometry_and_resources'),
            (h.SignalHandlers._evaluate, 'self.streak[4] >= spec.k', 'True', 'test_stability'),
            (h.SignalHandlers._evaluate, 'frame.capture_end_ns - self.streak[1] >= spec.stable_ns', 'True', 'test_stability'),
            (h.SignalHandlers._evaluate, 'now < deadline', 'True', 'test_deadline'),
            (h.parse_signal, 'name not in values', 'True', 'test_closed_binding'),
            (h.parse_signal, "binding['type'] == allowed[name]", 'True', 'test_closed_binding'),
            (h.parse_signal, 'type(value) is int', 'True', 'test_closed_binding'),
            (h.parse_signal, '_require(bool(value))', '_require(True)', 'test_phrase_normalization'),
            (h.SignalHandlers._evaluate, 'max(a.x, b.x) < min(a.x + a.w, b.x + b.w)', 'False', 'test_geometry_and_resources'),
            (h.SignalHandlers._evaluate, 'len(frame.marks) <= self.max_marks', 'True', 'test_geometry_and_resources'),
            (h.SignalHandlers._evaluate, 'size <= self.metadata_bytes', 'True', 'test_geometry_and_resources'),
            (h.SignalHandlers._evaluate, 'frame.sequence > old[3]', 'True', 'test_stability'),
            (h.SignalHandlers._evaluate, 'old[0] != frame.frame_sha256', 'False', 'test_stability'),
            (h.SignalHandlers._evaluate, 'return not present if', 'return True if', 'test_absence'),
            (h.SignalHandlers._evaluate, "r'(?!\\w)'", "''", 'test_joining'),
            (h.SignalHandlers._evaluate, "re.fullmatch('[0-9a-f]{64}', frame.frame_sha256) is not None", 'True', 'test_geometry_and_resources'),
            (h.SignalHandlers.invalidate, 'self.streak = None', "self.streak = getattr(self, 'streak', None)", 'test_stability'),

        ]
        for function, old, new, test in mutations:
            with self.subTest(gate=old):
                source = textwrap.dedent(inspect.getsource(function))
                self.assertIn(old, source)
                namespace = dict(vars(h))
                exec(compile(source.replace(old, new), '<signal-mutant>', 'exec'), namespace)
                target = h if function is h.parse_signal else h.SignalHandlers
                with patch.object(target, function.__name__, namespace[function.__name__]):
                    result = unittest.TestResult()
                    TestSignals(test).run(result)
                self.assertTrue(result.failures, (old, result.errors))
                self.assertFalse(result.errors, (old, result.errors))
