# @module {"id":"wa.learn.mw_handlers","layer":4,"owner":"LEARN","deps":["wa.scripts.types"],"book":"HB-WA-s4-learn"}
"""Bounded semantic signals over authenticated metadata, without image access."""
from dataclasses import dataclass
import re
import unicodedata
from wender.scripts.types import Reason

_IDS = {'mw.ocr.text_present.v1': 'mark_label',
        'mw.ocr.text_absent.v1': 'mark_label', 'mw.frame.stable.v1': 'state_signal'}


# @anchor:wa.s4.mw_handlers.SignalSpec
@dataclass(frozen=True, repr=False)
class SignalSpec:
    capability_id: str
    kind: str
    phrase: str | None
    region_px: tuple | None
    k: int | None
    stable_ns: int | None


# @anchor:wa.s4.mw_handlers.SignalFrame
@dataclass(frozen=True, repr=False)
class SignalFrame:
    sequence: int
    capture_start_ns: int
    capture_end_ns: int
    width: int
    height: int
    frame_sha256: str
    marks: tuple
    ocr_complete: bool


# @anchor:wa.s4.mw_handlers.SignalRefused
class SignalRefused(ValueError):
    # @anchor:wa.s4.mw_handlers.SignalRefused.__init__
    def __init__(self, reason=Reason.MISSING):
        self.reason = reason
        super().__init__('signal_refused')


def _require(condition, reason=Reason.SCHEMA):
    if not condition:
        raise SignalRefused(reason)


def _normal(text):
    return ' '.join(unicodedata.normalize('NFC', text.casefold()).split())


# @anchor:wa.s4.mw_handlers.parse_signal
def parse_signal(predicate, *, metadata_bytes, string_bytes):
    _require(type(metadata_bytes) is int and metadata_bytes > 0 and
             type(string_bytes) is int and string_bytes > 0, Reason.CAPACITY)
    _require(type(predicate) is dict and set(predicate) ==
             {'kind', 'capability_id', 'comparison', 'snapshot_ref', 'bindings', 'evidence_refs'} and
             type(predicate.get('capability_id')) is str and predicate.get('capability_id') in _IDS)
    refs = predicate['evidence_refs']
    _require(type(refs) is list and len(refs) <= metadata_bytes and
             all(type(v) is str and len(v) <= metadata_bytes for v in refs), Reason.CAPACITY)
    _require(sum(len(v.encode()) for v in refs) <= metadata_bytes, Reason.CAPACITY)
    cid = predicate['capability_id']
    _require(predicate.get('kind') == _IDS[cid] and predicate.get('comparison') == 'truth' and
             predicate.get('snapshot_ref') is None)
    bindings = predicate.get('bindings')
    _require(type(bindings) is list and len(bindings) <= 2)
    stable = cid == 'mw.frame.stable.v1'
    allowed = {'k': 'integer', 'stable_ms': 'integer'} if stable else {'phrase': 'string', 'region_px': 'integer_box'}
    values = {}
    for binding in bindings:
        _require(type(binding) is dict and set(binding) == {'name', 'type', 'value'})
        name = binding['name']
        _require(type(name) is str and name in allowed and name not in values and binding['type'] == allowed[name])
        value = binding['value']
        if allowed[name] == 'integer':
            _require(type(value) is int and 0 < value <= 2**63 - 1)
        elif name == 'phrase':
            _require(type(value) is str and len(value) <= min(string_bytes, metadata_bytes, 4096), Reason.CAPACITY)
            _require(len(value.encode('utf-8')) <= min(string_bytes, metadata_bytes, 4096), Reason.CAPACITY)
            value = _normal(value)
            _require(bool(value))
        else:
            _require(type(value) is list and len(value) == 4 and all(type(v) is int for v in value))
            _require(0 <= value[0] < value[2] and 0 <= value[1] < value[3])
            value = tuple(value)
        values[name] = value
    if stable:
        _require(values.get('k', 3) >= 2)
        return SignalSpec(cid, _IDS[cid], None, None, values.get('k', 3), values.get('stable_ms', 2000) * 1_000_000)
    _require('phrase' in values)
    return SignalSpec(cid, _IDS[cid], values['phrase'], values.get('region_px'), None, None)


# @anchor:wa.s4.mw_handlers.SignalHandlers
class SignalHandlers:
    # @anchor:wa.s4.mw_handlers.SignalHandlers.__init__
    def __init__(self, *, max_marks, metadata_bytes):
        _require(type(max_marks) is int and 0 < max_marks <= 64 and
                 type(metadata_bytes) is int and metadata_bytes > 0, Reason.CAPACITY)
        self.max_marks, self.metadata_bytes = max_marks, metadata_bytes
        self.invalidate()

    # @anchor:wa.s4.mw_handlers.SignalHandlers.invalidate
    def invalidate(self):
        self.streak = None

    # @anchor:wa.s4.mw_handlers.SignalHandlers.clear
    def clear(self):
        self.invalidate()

    # @anchor:wa.s4.mw_handlers.SignalHandlers.evaluate
    def evaluate(self, spec, frame, *, now_ns, deadline_ns):
        try:
            return self._evaluate(spec, frame, now_ns, deadline_ns)
        except SignalRefused:
            self.invalidate()
            raise
        except Exception:
            self.invalidate()
            raise SignalRefused(Reason.TYPE) from None

    def _evaluate(self, spec, frame, now, deadline):
        _require(type(spec) is SignalSpec and spec.capability_id in _IDS)
        _require(type(frame) is SignalFrame and all(type(v) is int for v in
                 (now, deadline, frame.sequence, frame.capture_start_ns, frame.capture_end_ns, frame.width, frame.height)))
        _require(now < deadline, Reason.DEADLINE)
        _require(frame.sequence >= 0 and 0 <= frame.capture_start_ns <= frame.capture_end_ns <= now and
                 frame.width > 0 and frame.height > 0 and type(frame.frame_sha256) is str and
                 re.fullmatch('[0-9a-f]{64}', frame.frame_sha256) is not None)
        _require(type(frame.marks) is tuple and len(frame.marks) <= self.max_marks, Reason.CAPACITY)
        size = 0
        ids = set()
        for mark in frame.marks:
            _require(all(type(v) is int for v in (mark.id, mark.x, mark.y, mark.w, mark.h)) and
                     mark.id >= 0 and mark.id not in ids and mark.x >= 0 and mark.y >= 0 and mark.w > 0 and
                     mark.h > 0 and mark.x + mark.w <= frame.width and mark.y + mark.h <= frame.height)
            ids.add(mark.id)
            _require(type(mark.ocr_text) is str and mark.missing_reason is None and
                     type(mark.ocr_score_promille) is int and 0 <= mark.ocr_score_promille <= 1000, Reason.MISSING)
            _require(len(mark.ocr_text) <= self.metadata_bytes, Reason.CAPACITY)
            size += len(mark.ocr_text.encode()) + 256
            _require(size <= self.metadata_bytes, Reason.CAPACITY)
        # Detect conflicting boxes before filtering to a region.
        for i, a in enumerate(frame.marks):
            for b in frame.marks[i + 1:]:
                _require(not (max(a.x, b.x) < min(a.x + a.w, b.x + b.w) and
                              max(a.y, b.y) < min(a.y + a.h, b.y + b.h)))
        old = self.streak
        if old is not None:
            _require(frame.sequence > old[3] and frame.capture_end_ns > old[2], Reason.STALE)
        if old is None or old[0] != frame.frame_sha256:
            self.streak = (frame.frame_sha256, frame.capture_end_ns, frame.capture_end_ns, frame.sequence, 1)
        else:
            self.streak = (old[0], old[1], frame.capture_end_ns, frame.sequence, old[4] + 1)
        if spec.capability_id == 'mw.frame.stable.v1':
            return self.streak[4] >= spec.k and frame.capture_end_ns - self.streak[1] >= spec.stable_ns
        region = spec.region_px
        if region is not None:
            _require(region[2] <= frame.width and region[3] <= frame.height)
        marks = [m for m in frame.marks if region is None or
                 (region[0] <= m.x and region[1] <= m.y and m.x + m.w <= region[2] and m.y + m.h <= region[3])]
        lines = []
        for mark in sorted(marks, key=lambda m: (m.y + m.h, m.x, m.id)):
            for baseline, height, row in lines:
                if 4 * abs(mark.y + mark.h - baseline) <= min(height, mark.h):
                    row.append(mark)
                    break
            else:
                lines.append((mark.y + mark.h, mark.h, [mark]))
        segments = []
        for _, _, row in lines:
            previous = None
            words = []
            for mark in sorted(row, key=lambda m: (m.x, m.id)):
                gap = mark.x - (previous.x + previous.w) if previous is not None else 0
                if previous is not None and not (gap >= 0 and 2 * gap <= 3 * min(previous.h, mark.h)):
                    segments.append(_normal(' '.join(words)))
                    words = []
                words.append(mark.ocr_text)
                previous = mark
            segments.append(_normal(' '.join(words)))
        pattern = r'(?<!\w)' + re.escape(spec.phrase) + r'(?!\w)'
        present = any(re.search(pattern, segment) is not None for segment in segments)
        if spec.capability_id == 'mw.ocr.text_absent.v1' or not present:
            _require(frame.ocr_complete is True, Reason.MISSING)
        return not present if spec.capability_id == 'mw.ocr.text_absent.v1' else present
