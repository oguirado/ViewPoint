# @module {"id":"wa.learn.demo_replay","layer":4,"owner":"LEARN","deps":["wa.learn.demonstrations","wa.learn.script_live","wa.perceive.srf_identity"],"book":"HB-WA-s4-learn"}
"""Explicitly selected episode replay; visual transitions confer no semantic authority.

No AX, OCR, model, store, admission, or SCRIPT objects are used. Unsupported
native paste remains unavailable unless an operator supplies a bounded adapter.
"""
from dataclasses import dataclass, fields
import math
import os
from pathlib import Path
import time
import numpy as np
from wender.learn import demonstrations as d
from wender.learn.script_live import LegacyDispatchOwner
from wender.perceive.srf_identity import SrfIdentitySurface


# @anchor:wa.learn.demo_replay.ReplayPolicy
@dataclass(frozen=True)
class ReplayPolicy:
    limits: d.DemoLimits = d.DemoLimits()
    steps: int = 128
    displacement: int = 48
    ncc_ppm: int = 920_000
    margin_ppm: int = 50_000
    pixel_threshold: int = 12
    coverage_ppm: int = 500_000
    iou_ppm: int = 500_000
    similarity_ppm: int = 950_000
    step_ns: int = 2_000_000_000
    # Wait for a click target when the operator also waited (Stitch generation: 95-176 s) [MEASURED, stitch-s1-demo2].
    wait_ns: int = 300_000_000_000
    wait_poll_ns: int = 1_000_000_000
    poll_ns: int = 50_000_000
    text_bytes: int = 4096
    total_text_bytes: int = 16_384


# @anchor:wa.learn.demo_replay.LocateResult
@dataclass(frozen=True)
class LocateResult:
    point: object = None
    translation: object = None
    score: object = None
    runner_up: object = None
    margin: object = None
    failure_code: object = None

    # @anchor:wa.learn.demo_replay.LocateResult.__post_init__
    def __post_init__(self):
        _scores(self, ('score', 'runner_up', 'margin'))
        for name in ('point', 'translation'):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, tuple(value))


# @anchor:wa.learn.demo_replay.VerifyResult
@dataclass(frozen=True)
class VerifyResult:
    verified: bool
    score: object = None
    failure_code: object = None

    # @anchor:wa.learn.demo_replay.VerifyResult.__post_init__
    def __post_init__(self):
        _scores(self, ('score',))


# @anchor:wa.learn.demo_replay.StepReceipt
@dataclass(frozen=True)
class StepReceipt:
    step_id: int
    disposition: str
    located_score: object = None
    margin: object = None
    verify_score: object = None
    elapsed_ns: object = None
    failure_code: object = None

    # @anchor:wa.learn.demo_replay.StepReceipt.__post_init__
    def __post_init__(self):
        _scores(self, ('located_score', 'margin', 'verify_score'))


# @anchor:wa.learn.demo_replay.ReplayResult
@dataclass(frozen=True)
class ReplayResult:
    status: str
    failed_step: object
    receipts: tuple
    input_uncertain: bool = False

    # @anchor:wa.learn.demo_replay.ReplayResult.__post_init__
    def __post_init__(self):
        object.__setattr__(self, 'receipts', tuple(self.receipts))


def _scores(record, names):
    d._require(all(getattr(record, name) is None or (type(getattr(record, name)) is int
                   and -1_000_000 <= getattr(record, name) <= 2_000_000) for name in names), 'NONINTEGER_SCORE')


def _policy(p):
    d._require(type(p) is ReplayPolicy, 'INVALID_POLICY')
    d._limits(p.limits)
    defaults = ReplayPolicy()
    for f in fields(p):
        if f.name == 'limits':
            continue
        v, bound = getattr(p, f.name), getattr(defaults, f.name)
        if f.name in ('ncc_ppm', 'margin_ppm', 'coverage_ppm', 'iou_ppm', 'similarity_ppm'):
            d._require(d._integer(v, bound, 1_000_000), 'INVALID_POLICY')
        elif f.name == 'poll_ns':
            d._require(d._integer(v, bound, defaults.step_ns), 'INVALID_POLICY')
        elif f.name == 'pixel_threshold':
            d._require(v == 12 and type(v) is int, 'INVALID_POLICY')
        elif f.name == 'displacement':
            d._require(d._integer(v, 0, bound), 'INVALID_POLICY')
        else:
            d._require(d._integer(v, 1, bound), 'INVALID_POLICY')


def _rgb(a, limits):
    d._require(type(a) is np.ndarray and a.dtype == np.uint8 and a.ndim == 3 and a.shape[2] == 3,
               'INVALID_PIXELS')
    h, w = a.shape[:2]
    d._require(0 < w <= limits.axis and 0 < h <= limits.axis and w * h <= limits.pixels, 'IMAGE_BUDGET')
    d._require(w * h * 64 <= limits.memory_bytes, 'MEMORY_BUDGET')


def _box(b, w, h):
    return (type(b) is tuple and len(b) == 4 and all(d._integer(v) for v in b)
            and b[0] < b[2] <= w and b[1] < b[3] <= h)


def _target(t, w, h, limits):
    d._require(type(t) is d.TargetDescriptor and type(t.recorded_point) is tuple
               and len(t.recorded_point) == 2 and d._integer(t.recorded_point[0], 0, w - 1)
               and d._integer(t.recorded_point[1], 0, h - 1), 'POINT_INTEGER')
    total = 0
    for crop, cw, ch in ((t.small, 64, 40), (t.context, 160, 96)):
        d._require(type(crop) is d.TemplateCrop and _box(crop.bbox_xyxy, w, h), 'CROP_GEOMETRY')
        x0, y0, x1, y1 = crop.bbox_xyxy
        _rgb(crop.pixels, limits)
        d._require(crop.pixels.shape == (y1 - y0, x1 - x0, 3) and x1 - x0 <= cw and y1 - y0 <= ch
                   and type(crop.point_offset_xy) is tuple and len(crop.point_offset_xy) == 2
                   and all(d._integer(v) for v in crop.point_offset_xy)
                   and crop.point_offset_xy == (t.recorded_point[0] - x0, t.recorded_point[1] - y0)
                   and 0 <= crop.point_offset_xy[0] < x1 - x0 and 0 <= crop.point_offset_xy[1] < y1 - y0,
                   'CROP_GEOMETRY')
        gray = _gray(crop.pixels)
        n, total, squared = _stats(gray)
        d._require(n * squared > total * total, 'DEGENERATE_TEMPLATE')
        total += crop.pixels.nbytes
    d._require(total <= limits.retained_bytes, 'RETAINED_BUDGET')
    d._require(type(t.mark) is d.MarkDescriptor, 'INVALID_DESCRIPTOR')
    if t.mark.missing_reason is None:
        b = t.mark.box
        d._require(len(b) == 4 and all(d._integer(v) for v in b) and b[2] > 0 and b[3] > 0
                   and b[0] + b[2] <= w and b[1] + b[3] <= h
                   and t.mark.relative_xy == ((t.recorded_point[0] - b[0], b[2]),
                                              (t.recorded_point[1] - b[1], b[3]))
                   and all(0 <= n < den for n, den in t.mark.relative_xy), 'INVALID_DESCRIPTOR')
    return total


def _expected(e, w, h, limits):
    d._require(type(e) is d.ExpectedChange and e.missing_reason is None and _box(e.bbox, w, h), 'CHANGE_UNOBSERVABLE')
    x0, y0, x1, y1 = e.bbox
    _rgb(e.after_crop, limits)
    d._require(type(e.mask) is np.ndarray and e.mask.dtype == np.bool_ and e.mask.shape == (y1 - y0, x1 - x0)
               and e.after_crop.shape == (y1 - y0, x1 - x0, 3) and bool(e.mask.any()), 'INVALID_DESCRIPTOR')
    return e.mask.nbytes + e.after_crop.nbytes


PPM = 1_000_000


def _gray(rgb):
    # Integer BT.601 luminance. Values <=255; edges <=510. For at most
    # DemoLimits.pixels samples, sums of squares/products fit signed int64.
    rgb = rgb.astype(np.int64)
    return (rgb[:, :, 0] * 299 + rgb[:, :, 1] * 587 + rgb[:, :, 2] * 114) // 1000


def _stats(a):
    d._require(a.size <= d.DemoLimits().pixels and a.dtype == np.int64
               and int(a.min()) >= 0 and int(a.max()) <= 510, 'WORK_BUDGET')
    return a.size, int(a.sum()), int(np.sum(a * a))


def _ncc(a, template, stats):
    n, sa, aa = _stats(a)
    nt, sb, bb = stats
    d._require(n == nt, 'INVALID_DESCRIPTOR')
    # Reduction bounds above cover int64; all large products below are Python ints.
    numerator = n * int(np.sum(a * template)) - sa * sb
    av, bv = n * aa - sa * sa, n * bb - sb * sb
    if av <= 0 or bv <= 0:
        return -PPM
    product = av * bv
    scaled = numerator * PPM
    # Exact floor of the signed rational NCC: isqrt never rounds a pass upward.
    magnitude = math.isqrt(scaled * scaled // product)
    exact = magnitude * magnitude * product == scaled * scaled
    return magnitude if scaled >= 0 else -magnitude - (not exact)


def _edges(rgb):
    g = _gray(rgb)
    e = np.zeros_like(g)
    e[:, 1:] += np.abs(np.diff(g, axis=1))
    e[1:, :] += np.abs(np.diff(g, axis=0))
    return e


# @anchor:wa.learn.demo_replay._search
def _search(transform, frame_rgb, target, crops, window, policy, deadline_ns, clock):
    dx0, dy0, dx1, dy1 = window
    templates = []
    for crop in crops:
        a = transform(crop.pixels)
        stats = _stats(a)
        n, total, squared = stats
        d._require(n * squared > total * total, 'DEGENERATE_TEMPLATE')
        templates.append((a, stats))
    gray = transform(frame_rgb)
    best, runner, translation = -PPM - 1, None, None
    # Each translation is a bounded tile; no candidate-by-template tensor.
    for dy in range(dy0, dy1 + 1):
        for dx in range(dx0, dx1 + 1):
            d._require(clock() < deadline_ns, 'DEADLINE')
            scores = []
            for crop, (template, norm) in zip(crops, templates):
                x0, y0, x1, y1 = crop.bbox_xyxy
                scores.append(_ncc(gray[y0 + dy:y1 + dy, x0 + dx:x1 + dx], template, norm))
            d._require(all(type(s) is int and -PPM <= s <= PPM for s in scores), 'NONFINITE_SCORE')
            score = min(scores)
            if score > best:
                best, runner, translation = score, (best if translation is not None else None), (int(dx), int(dy))
            elif runner is None or score > runner:
                runner = score
    runner_value = runner
    # Runner upper bound: floor+1 except at the exact extreme. A single candidate
    # has no margin evidence; uniqueness follows its count, not a synthetic score.
    margin = best - min(PPM, runner + 1) if runner is not None else None
    if best < policy.ncc_ppm:
        return LocateResult(score=best, runner_up=runner_value, margin=margin, failure_code='LOW_CONFIDENCE')
    if (margin is not None and margin < policy.margin_ppm) or (runner_value is not None and min(PPM, runner_value + 1) >= policy.ncc_ppm):
        return LocateResult(score=best, runner_up=runner_value, margin=margin, failure_code='AMBIGUOUS_TARGET')
    dx, dy = translation
    x0, y0, _, _ = target.small.bbox_xyxy
    ox, oy = target.small.point_offset_xy
    d._require(clock() < deadline_ns, 'DEADLINE')
    return LocateResult((int(x0 + ox + dx), int(y0 + oy + dy)), translation, best, runner_value, margin)


# @anchor:wa.learn.demo_replay.locate
def locate(frame_rgb, target, *, policy, deadline_ns, clock):
    try:
        _policy(policy)
        _rgb(frame_rgb, policy.limits)
        h, w = frame_rgb.shape[:2]
        _target(target, w, h, policy.limits)
        d._require(d._integer(deadline_ns) and clock() < deadline_ns, 'DEADLINE')
        crops = (target.small, target.context)
        delta = policy.displacement
        dx0 = max(-delta, *(-c.bbox_xyxy[0] for c in crops))
        dy0 = max(-delta, *(-c.bbox_xyxy[1] for c in crops))
        dx1 = min(delta, *(w - c.bbox_xyxy[2] for c in crops))
        dy1 = min(delta, *(h - c.bbox_xyxy[3] for c in crops))
        count = (dx1 - dx0 + 1) * (dy1 - dy0 + 1)
        pixels = sum(c.pixels.shape[0] * c.pixels.shape[1] for c in crops)
        d._require(0 < count <= policy.limits.candidates and count * pixels <= policy.limits.comparisons, 'WORK_BUDGET')
        d._require(w * h * 64 + pixels * 64 <= policy.limits.memory_bytes, 'MEMORY_BUDGET')
        result = None
        # Gray NCC first; then edge NCC, which ignores fill changes such as a hover state captured in the demo
        # [MEASURED, stitch-s1-demo2 seq 85: hovered "Copy code" pill scored 0.89 in gray].
        for transform in (_gray, _edges):
            result = _search(transform, frame_rgb, target, crops, (dx0, dy0, dx1, dy1), policy, deadline_ns, clock)
            if result.failure_code != 'LOW_CONFIDENCE':
                break
        return result
    except ValueError as e:
        return LocateResult(failure_code=str(e))


# @anchor:wa.learn.demo_replay.verify_change
def verify_change(before_rgb, after_rgb, expected, *, translation, policy):
    try:
        _policy(policy)
        _rgb(before_rgb, policy.limits)
        _rgb(after_rgb, policy.limits)
        d._require(before_rgb.shape == after_rgb.shape, 'FRAME_DIMENSIONS')
        h, w = before_rgb.shape[:2]
        _expected(expected, w, h, policy.limits)
        d._require(type(translation) is tuple and len(translation) == 2
                   and all(type(v) is int and abs(v) <= policy.displacement for v in translation), 'POINT_INTEGER')
        dx, dy = translation
        a, b, c, f = expected.bbox
        box = (a + dx, b + dy, c + dx, f + dy)
        d._require(_box(box, w, h), 'CROP_GEOMETRY')
        a, b, c, f = box
        changed = np.any(np.abs(after_rgb.astype(np.int16) - before_rgb.astype(np.int16)) > policy.pixel_threshold, axis=2)
        covered = int(np.count_nonzero(changed[b:f, a:c] & expected.mask))
        support = int(np.count_nonzero(expected.mask))
        ys, xs = np.nonzero(changed)
        d._require(len(xs) > 0, 'CHANGE_MISMATCH')
        actual = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        ix = max(0, min(c, actual[2]) - max(a, actual[0]))
        iy = max(0, min(f, actual[3]) - max(b, actual[1]))
        intersection = ix * iy
        union = (c - a) * (f - b) + (actual[2] - actual[0]) * (actual[3] - actual[1]) - intersection
        error = int(np.abs(after_rgb[b:f, a:c].astype(np.int16) - expected.after_crop.astype(np.int16)).sum(dtype=np.int64))
        maximum = expected.after_crop.size * 255
        similarity = maximum - error
        score = similarity * PPM // maximum
        ok = (covered * PPM >= policy.coverage_ppm * support and intersection * PPM >= policy.iou_ppm * union
              and similarity * PPM >= policy.similarity_ppm * maximum)
        return VerifyResult(ok, score, None if ok else 'CHANGE_MISMATCH')
    except ValueError as e:
        return VerifyResult(False, failure_code=str(e))


def _canonical_key(key):
    d._require(type(key) is str and 0 < len(key) <= 400, 'KEY_POLICY')
    return 'escape' if key.casefold() in ('esc', 'escape') else key


# @anchor:wa.learn.demo_replay.KeyPolicy
@dataclass(frozen=True)
class KeyPolicy:
    identity: str
    window: str
    forbidden_keys: tuple

    # @anchor:wa.learn.demo_replay.KeyPolicy.__post_init__
    def __post_init__(self):
        d._require(type(self.identity) is str and type(self.window) is str
                   and bool(self.identity) and bool(self.window), 'KEY_POLICY')
        d._require(type(self.forbidden_keys) in (tuple, list) and len(self.forbidden_keys) <= 128, 'KEY_POLICY')
        object.__setattr__(self, 'forbidden_keys', tuple(sorted(set(_canonical_key(k) for k in self.forbidden_keys))))


# @anchor:wa.learn.demo_replay.DemoIdentitySurface
class DemoIdentitySurface:
    """Run-owned adapter; selection copied at construction and checked under dispatch.

    paste_input optionally overrides ASCII type input: (text, *, timeout).
    The frozen identity transport has no native paste mapping. An arbitrary
    injected identity transport cannot prove timeouts and is refused.
    """

    # @anchor:wa.learn.demo_replay.DemoIdentitySurface.__init__
    def __init__(self, identity, *, selected_steps, policy=ReplayPolicy(), clock=time.monotonic_ns,
                 pace=time.sleep, paste_input=None, key_policy=None, text_encoding='ascii'):
        _policy(policy)
        d._require(type(identity) is SrfIdentitySurface and identity.transport is None
                   and identity.trace is None and isinstance(identity.timeout, (int, float))
                   and not isinstance(identity.timeout, bool) and math.isfinite(identity.timeout)
                   and identity.timeout > 0, 'TRANSPORT_UNAVAILABLE')
        d._require(text_encoding in ('ascii', 'utf-8') and type(text_encoding) is str, 'TEXT_ENCODING')
        self._text_encoding = text_encoding
        d._require(key_policy is None or (type(key_policy) is KeyPolicy
                   and key_policy.identity == identity.identity and key_policy.window == identity.window), 'KEY_POLICY')
        self._key_policy = None if key_policy is None else KeyPolicy(key_policy.identity, key_policy.window, key_policy.forbidden_keys)
        self._identity = identity
        self._original_timeout = identity.timeout
        self._selected = tuple(selected_steps)
        self._policy = policy
        self._clock, self._pace, self._paste = clock, pace, paste_input
        self._owner = LegacyDispatchOwner(self)
        self._used = False
        self._deadline = None
        self._binding = None
        self._frame_binding = None
        self._attached = False

    # @anchor:wa.learn.demo_replay.DemoIdentitySurface.policy
    @property
    def policy(self):
        return self._policy

    # @anchor:wa.learn.demo_replay.DemoIdentitySurface.clock
    def clock(self):
        return self._clock()

    # @anchor:wa.learn.demo_replay.DemoIdentitySurface.close
    def close(self):
        self._owner.close()
        self._identity.timeout = self._original_timeout
        if self._attached:
            self._identity.stop()
            self._attached = False

    def _check_key(self, key):
        policy = self._key_policy
        if policy is not None:
            d._require((policy.identity, policy.window) == (self._identity.identity, self._identity.window), 'KEY_POLICY')
            code = 'ESCAPE_UNSAFE' if _canonical_key(key) == 'escape' else 'KEY_POLICY'
            d._require(_canonical_key(key) not in policy.forbidden_keys, code)

    def _timeout(self, deadline):
        remaining = min(deadline, self._deadline) - self.clock()
        d._require(remaining > 0, 'DEADLINE')
        self._identity.timeout = min(self._original_timeout, remaining / 1_000_000_000)
        return self._identity.timeout

    def _frame(self, deadline):
        self._timeout(deadline)
        frame = self._identity.frame()
        d._require(type(frame) is dict and type(frame.get('window')) is dict
                   and (frame['window'].get('id'), frame.get('width'), frame.get('height')) == self._binding
                   and frame['window'].get('w') == self._binding[1]
                   and frame['window'].get('h') == self._binding[2], 'WINDOW_BINDING')
        d._require(d._integer(frame.get('seq')) and d._integer(frame.get('capture_mono_ns')), 'FRAME_FRESHNESS')
        d._require(frame['capture_mono_ns'] <= self.clock(), 'FRAME_FRESHNESS')
        if self._frame_binding is not None:
            d._require(frame['seq'] > self._frame_binding[0]
                       and frame['capture_mono_ns'] >= self._frame_binding[1], 'FRAME_FRESHNESS')
        path = Path(frame.get('path', ''))
        d._require(path.is_absolute() and path == path.resolve() and path.is_relative_to('/dev/shm'), 'FILE_CUSTODY')
        fd = d._directory(path.parent)
        try:
            d._ram_fd(fd)
            rgb, _ = d._frame(fd, path.name, self.policy.limits, self._binding, deadline_ns=time.monotonic_ns() + max(0, min(deadline, self._deadline) - self.clock()))
        finally:
            os.close(fd)
        self._frame_binding = (frame['seq'], frame['capture_mono_ns'])
        self._timeout(deadline)
        return frame, rgb

    def _send(self, step, frame, located, text, deadline):
        # @anchor:wa.learn.demo_replay.DemoIdentitySurface._send.send
        def send():
            # Selection, frame and deadline checks share LegacyDispatchOwner's lock.
            d._require(any(step is s for s in self._selected), 'SELECTION')
            self._timeout(deadline)
            d._require(self._frame_binding == (frame['seq'], frame['capture_mono_ns'])
                       and (frame['window']['id'], frame['width'], frame['height']) == self._binding, 'FRAME_FRESHNESS')
            e = step.event
            if e.kind == 'key':
                self._check_key(e.keysym)
            if e.kind == 'click':
                point = located.point
                d._require(type(point) is tuple and len(point) == 2
                           and d._integer(point[0], 0, e.width - 1) and d._integer(point[1], 0, e.height - 1)
                           and located.failure_code is None and located.translation is not None, 'POINT_INTEGER')
                result = self._identity._call('click', point=f'{point[0]},{point[1]}', button=e.button)
            elif e.kind in _GESTURES:
                point = located.point
                d._require(type(point) is tuple and len(point) == 2
                           and d._integer(point[0], 0, e.width - 1) and d._integer(point[1], 0, e.height - 1)
                           and located.translation is not None, 'POINT_INTEGER')
                if e.kind == 'scroll':
                    extra = {'modifier': e.modifiers[0]} if e.modifiers else {}
                    result = self._identity._call('scroll', point=f'{point[0]},{point[1]}', dir=e.direction, **extra)
                else:
                    x2 = min(e.width - 1, max(0, e.x2 + point[0] - e.x))
                    y2 = min(e.height - 1, max(0, e.y2 + point[1] - e.y))
                    d._require((x2, y2) != point, 'POINT_INTEGER')
                    result = self._identity._call('drag', span=f'{point[0]},{point[1]}:{x2},{y2}', button=e.button or 1)
            elif e.kind == 'key' and text is None:
                keysym = '+'.join((*e.modifiers, e.keysym))
                result = self._identity._call('key', keysym=keysym)
            elif e.kind in ('text', 'key'):   # a key here is a recorded ctrl+v slot carrying caller text
                d._require(self._paste is None or callable(self._paste), 'TRANSPORT_UNAVAILABLE')
                result = (self._paste(text, timeout=self._identity.timeout) if self._paste is not None
                          else self._identity._call('type', text=text))
            else:
                raise ValueError('UNSUPPORTED_ACTION')
            result = self._identity._validated_input(result, e.kind)
            d._require(d._integer(result['input_mono_ns'])
                       and frame['capture_mono_ns'] <= result['input_mono_ns'] <= self.clock(), 'INPUT_RECEIPT')
            self._timeout(deadline)
            return result
        return self._owner.dispatch(self, {'kind': step.event.kind}, send)


def _preflight(surface, steps, texts):
    p = surface.policy
    _policy(p)
    d._require(surface._owner.check_ready() and not surface._used and type(steps) is tuple and 0 < len(steps) <= p.steps, 'SELECTION')
    d._require(len(steps) == len(surface._selected) and all(a is b for a, b in zip(steps, surface._selected)), 'SELECTION')
    d._require(type(texts) is dict, 'TEXT_SLOT')
    d._require(all(type(k) is int for k in texts), 'TEXT_SLOT')
    binding, previous, previous_mono, retained, text_bytes = None, None, None, 0, 0
    text_ids = set()
    for s in steps:
        d._require(type(s) is d.DemoStep and d._integer(s.step_id) and s.step_id == s.event.seq, 'INVALID_STEP')
        d._require(previous is None or s.step_id > previous, 'SELECTION')
        previous = s.step_id
        e = s.event
        d._validate_event(e, p.limits)
        d._require(previous_mono is None or e.mono_ns >= previous_mono, 'RECORDING_ORDER')
        previous_mono = e.mono_ns
        current = (e.window_id, e.width, e.height)
        binding = binding or current
        d._require(current == binding and e.window_id == surface._identity.window, 'WINDOW_BINDING')
        d._require(e.kind in _REPLAYABLE and (not e.modifiers or e.kind == 'key'
                   or (e.kind == 'scroll' and len(e.modifiers) == 1 and e.modifiers[0] in ('ctrl', 'shift'))), 'UNSUPPORTED_ACTION')
        if e.kind == 'key':
            surface._check_key(e.keysym)
            if s.step_id in texts:
                # A recorded ctrl+v is where the operator pasted host text; replay pastes the caller's text there.
                d._require(e.keysym in ('v', 'V') and tuple(e.modifiers) == ('ctrl',), 'TEXT_SLOT')
        d._require(s.replayable is True and s.refusal_code is None, s.refusal_code or 'INVALID_STEP')
        if not (e.kind in _GESTURES and s.expected_change.missing_reason is not None):
            retained += _expected(s.expected_change, e.width, e.height, p.limits)
        if e.kind in _POINTED:
            retained += _target(s.target, e.width, e.height, p.limits)
            d._require(s.target.recorded_point == (e.x, e.y), 'CROP_GEOMETRY')
            crops = (s.target.small, s.target.context)
            delta = p.displacement
            nx = min(delta, *(e.width - c.bbox_xyxy[2] for c in crops)) - max(-delta, *(-c.bbox_xyxy[0] for c in crops)) + 1
            ny = min(delta, *(e.height - c.bbox_xyxy[3] for c in crops)) - max(-delta, *(-c.bbox_xyxy[1] for c in crops)) + 1
            comparisons = nx * ny * sum(c.pixels.shape[0] * c.pixels.shape[1] for c in crops)
            d._require(nx * ny <= p.limits.candidates and comparisons <= p.limits.comparisons, 'WORK_BUDGET')
        else:
            d._require(s.target is None, 'INVALID_DESCRIPTOR')
        d._require(retained <= p.limits.retained_bytes, 'RETAINED_BUDGET')
        d._require(e.width * e.height * 64 + retained <= p.limits.memory_bytes, 'MEMORY_BUDGET')
        if e.kind == 'text' or (e.kind == 'key' and s.step_id in texts):
            text_ids.add(s.step_id)
            value = texts.get(s.step_id)
            encoded = d.validate_text_slot(value, expected_length=e.text_len if e.kind == 'text' else None, max_bytes=p.text_bytes)
            try:
                value.encode(surface._text_encoding, errors='strict')
            except UnicodeError:
                d._require(False, 'TEXT_ENCODING')
            n = len(encoded)
            text_bytes += n
            d._require(text_bytes <= p.total_text_bytes, 'TEXT_BUDGET')
            d._require(surface._paste is None or callable(surface._paste), 'TRANSPORT_UNAVAILABLE')
    d._require(set(texts) == text_ids, 'TEXT_SLOT')
    largest_frame = max(s.event.width * s.event.height for s in steps)
    largest_crops = max((sum(c.pixels.shape[0] * c.pixels.shape[1] for c in (s.target.small, s.target.context))
                         if s.target is not None else 0) for s in steps)
    # Reserve retained evidence and the full compressed/decode/NCC envelope together.
    d._require(largest_frame * 64 + retained + largest_crops * 64 + p.limits.png_bytes * 4 <= p.limits.memory_bytes,
               'MEMORY_BUDGET')
    return binding


_POINTED = ('click', 'scroll', 'drag')
_GESTURES = ('scroll', 'drag')
_REPLAYABLE = ('click', 'key', 'text', 'scroll', 'drag')


# @anchor:wa.learn.demo_replay._satisfied
def _end_state_share(frame_rgb, expected, policy, point=None):
    # Share of the demo's changed pixels that already show the demo's after state (None: not measurable).
    # With a point, only changed pixels within the context crop around it count (tooltips and panels elsewhere
    # change too) [MEASURED, stitch-s1-demo2 seq 81: 18 % mismatch over the full box, 6 % locally].
    if type(expected) is not d.ExpectedChange or expected.missing_reason is not None or not expected.mask.any():
        return None
    x0, y0, x1, y1 = expected.bbox
    region = frame_rgb[y0:y1, x0:x1].astype(np.int16)
    if region.shape != expected.after_crop.shape:
        return None
    mask = expected.mask
    if point is not None:
        ys, xs = np.mgrid[y0:y1, x0:x1]
        mask = mask & (np.abs(xs - point[0]) <= 80) & (np.abs(ys - point[1]) <= 48)
        if int(np.count_nonzero(mask)) < 30:
            return None
    diff = np.abs(region - expected.after_crop.astype(np.int16)).max(axis=2)[mask]
    return (diff.size - int(np.count_nonzero(diff > policy.pixel_threshold))) * PPM // diff.size if diff.size else None


def _satisfied(frame_rgb, step, policy):
    share = _end_state_share(frame_rgb, step.expected_change, policy, (step.event.x, step.event.y))
    return share is not None and share >= 900_000


# @anchor:wa.learn.demo_replay._reacted
def _reacted(before_rgb, after_rgb, expected, policy):
    # State signal for content-dependent steps: drop pixels whose CURRENT before differs from the demo's before
    # (content that is not the demo's: another screen, other code), then require that the UI changed on at
    # least half of the demo's remaining changed pixels (the same region reacted to the input).
    if (type(expected) is not d.ExpectedChange or expected.missing_reason is not None
            or expected.before_crop is None or not expected.mask.any()):
        return None
    x0, y0, x1, y1 = expected.bbox
    now = before_rgb[y0:y1, x0:x1].astype(np.int16)
    if now.shape != expected.before_crop.shape:
        return None
    t = policy.pixel_threshold
    content = np.any(np.abs(now - expected.before_crop.astype(np.int16)) > t, axis=2)
    mask = expected.mask & ~content
    if int(np.count_nonzero(mask)) < 50:
        return None
    changed = np.any(np.abs(after_rgb[y0:y1, x0:x1].astype(np.int16) - now) > t, axis=2)
    count, support = int(np.count_nonzero(changed & mask)), int(np.count_nonzero(mask))
    share = count * PPM // support
    return VerifyResult(True, share) if count * 2 >= support else None


# @anchor:wa.learn.demo_replay._motion
def _motion(before_rgb, after_rgb, policy):
    changed = np.any(np.abs(after_rgb.astype(np.int16) - before_rgb.astype(np.int16)) > policy.pixel_threshold, axis=2)
    count = int(np.count_nonzero(changed))
    share = count * PPM // changed.size
    ok = count * 1000 >= changed.size
    return VerifyResult(ok, share, None if ok else 'CHANGE_MISMATCH')


# @anchor:wa.learn.demo_replay.replay
def replay(surface, steps, *, texts, at_recorded=frozenset()):
    if type(surface) is not DemoIdentitySurface:
        return ReplayResult('REFUSED', None, (StepReceipt(0, 'REFUSED', failure_code='TRANSPORT_UNAVAILABLE'),))
    receipts, current, located, verified, started = [], None, None, None, surface.clock()
    step_start = started
    sending = False
    try:
        steps = tuple(steps)
        surface._binding = _preflight(surface, steps, texts)
        # Copy the text map once; values are consumed only at their send boundary.
        texts = dict(texts)
        surface._used = True
        surface._deadline = started + surface.policy.limits.operation_ns
        surface._attached = True
        surface._identity.start()
        previous_mono = None
        next_gaps = {a.step_id: b.event.mono_ns - a.event.mono_ns for a, b in zip(steps, steps[1:])}
        for step in steps:
            current, located, verified = step.step_id, None, None
            step_start = surface.clock()
            p = surface.policy
            gap = 0 if previous_mono is None else step.event.mono_ns - previous_mono
            previous_mono = step.event.mono_ns
            deadline = min(surface._deadline, step_start + p.step_ns)
            wait_deadline = min(surface._deadline, step_start + min(p.wait_ns, 2 * gap)) if gap > p.step_ns else deadline
            frame, before = surface._frame(deadline)
            if step.event.kind == 'click' and step.step_id in at_recorded:
                # Caller-reviewed: this click targets generated content (a new screen) whose pixels differ from
                # the demo; act at the recorded point [MEASURED, Stitch replay R5].
                located = LocateResult((step.event.x, step.event.y), (0, 0))
            elif step.event.kind == 'click' and _satisfied(before, step, p):
                # The demo's end state for this step is already on screen (tool already active, screen already
                # selected) [MEASURED, stitch-s1-demo2 replay seq 81/82]: clicking would not reproduce the change.
                receipts.append(StepReceipt(current, 'ALREADY_SATISFIED', None, None, None, surface.clock() - step_start))
                continue
            if located is not None:
                pass
            elif step.event.kind in _GESTURES:
                located = locate(before, step.target, policy=p, deadline_ns=deadline, clock=surface.clock)
                if located.failure_code is not None:
                    # Pans/zooms/scrolls act on an area, not a control: fall back to the recorded point.
                    located = LocateResult((step.event.x, step.event.y), (0, 0))
            elif step.event.kind == 'click':
                while True:
                    located = locate(before, step.target, policy=p, deadline_ns=max(deadline, surface.clock() + p.step_ns // 2)
                                     if wait_deadline > deadline else deadline, clock=surface.clock)
                    if (located.failure_code != 'LOW_CONFIDENCE'
                            or surface.clock() + p.wait_poll_ns >= wait_deadline):
                        break
                    surface._pace(p.wait_poll_ns / 1_000_000_000)
                    deadline = min(surface._deadline, surface.clock() + p.step_ns)
                    frame, before = surface._frame(deadline)
                d._require(located.failure_code is None, located.failure_code)
                deadline = min(surface._deadline, max(deadline, surface.clock() + p.step_ns // 2))
            else:
                located = LocateResult(translation=(0, 0))
            if step.step_id in texts:
                # Clipboard paste of long text takes > 2 s [MEASURED, Stitch S2 run R9].
                deadline = min(surface._deadline, max(deadline, surface.clock() + 5 * p.step_ns))
            sending = True
            completion = surface._send(step, frame, located, texts.pop(step.step_id, None), deadline)
            sending = False
            # Verify for as long as the operator waited before the next event (modals load, menus animate)
            # [MEASURED, stitch-s1-demo2 seq 84: View Code modal took > 2 s; the operator waited 4.6 s].
            next_gap = next_gaps.get(step.step_id, 0)
            poll = p.poll_ns
            if next_gap > p.step_ns:
                deadline = min(surface._deadline, max(deadline, surface.clock() + min(p.wait_ns, next_gap * 3 // 2)))
                poll = max(poll, min(p.wait_poll_ns, (deadline - surface.clock()) // p.limits.acquisitions))
            for _ in range(surface.policy.limits.acquisitions - 1):
                d._require(deadline - surface.clock() > poll, 'DEADLINE')
                surface._timeout(deadline)
                surface._pace(poll / 1_000_000_000)
                after_frame, after = surface._frame(deadline)
                d._require(after_frame['capture_mono_ns'] > completion['input_mono_ns'], 'FRAME_FRESHNESS')
                if step.event.kind in _GESTURES:
                    verified = _motion(before, after, p)
                else:
                    verified = verify_change(before, after, step.expected_change, translation=located.translation, policy=surface.policy)
                    if not verified.verified and located.translation == (0, 0):
                        # Postcondition: the demo's end state is on screen even if this input changed nothing
                        # (state already reached) [MEASURED, stitch-s1-demo2 seq 82: 79 % of changed pixels].
                        share = _end_state_share(after, step.expected_change, p)
                        if share is not None and share >= 750_000:
                            verified = VerifyResult(True, share)
                    if not verified.verified:
                        verified = _reacted(before, after, step.expected_change, p) or verified
                if verified.verified:
                    break
            d._require(verified is not None and verified.verified, 'CHANGE_MISMATCH')
            surface._timeout(deadline)
            receipts.append(StepReceipt(current, 'VISUAL_TRANSITION_VERIFIED', located.score, located.margin,
                                        verified.score, surface.clock() - step_start))
        return ReplayResult('VISUAL_TRANSITIONS_VERIFIED', None, tuple(receipts))
    except Exception as e:
        if sending:
            surface._owner.uncertain({'step': current})
        # Never serialize external exceptions (which may contain caller text/paths).
        code = str(e) if type(e) is ValueError and str(e) in _CODES else 'INPUT_UNCERTAIN' if sending else 'REPLAY_UNAVAILABLE'
        if code == 'REPLAY_UNAVAILABLE' and verified is not None and not verified.verified:
            code = 'CHANGE_MISMATCH'   # the step budget ran out inside a frame read while the change was still unverified
        elif code == 'REPLAY_UNAVAILABLE' and surface._deadline is not None and surface.clock() >= deadline:
            code = 'DEADLINE'
        receipts.append(StepReceipt(current if current is not None else 0, 'STOPPED' if current is not None else 'REFUSED',
                                    located.score if located else None, located.margin if located else None,
                                    verified.score if verified else None, max(0, surface.clock() - step_start), code))
        return ReplayResult('STOPPED' if current is not None else 'REFUSED', current, tuple(receipts), sending)
    finally:
        surface.close()


_CODES = frozenset(('INVALID_LIMITS', 'INVALID_POLICY', 'INVALID_PIXELS', 'IMAGE_BUDGET', 'MEMORY_BUDGET',
                    'POINT_INTEGER', 'CROP_GEOMETRY', 'RETAINED_BUDGET', 'INVALID_DESCRIPTOR', 'DEADLINE',
                    'WORK_BUDGET', 'DEGENERATE_TEMPLATE', 'NONFINITE_SCORE', 'LOW_CONFIDENCE', 'AMBIGUOUS_TARGET',
                    'CHANGE_UNOBSERVABLE', 'CHANGE_MISMATCH', 'FRAME_DIMENSIONS', 'WINDOW_BINDING', 'FILE_CUSTODY',
                    'FILE_BUDGET', 'FILE_CHANGED', 'FRAME_FRESHNESS', 'INPUT_RECEIPT', 'SELECTION', 'TEXT_SLOT',
                    'KEY_POLICY', 'RAM_REQUIRED', 'NONINTEGER_SCORE', 'INVALID_STEP', 'UNSUPPORTED_ACTION', 'ESCAPE_UNSAFE', 'TEXT_ENCODING', 'TEXT_BUDGET',
                    'KEY_VOCABULARY', 'CLOSED_SCHEMA', 'TEXT_LENGTH', 'EVENT_INTEGER', 'RECORDING_ORDER', 'TRANSPORT_UNAVAILABLE'))
