# @module {"id":"wa.learn.mw_guidance","layer":4,"owner":"LEARN","deps":["wa.scripts.types","wa.perceive.vision_bridge"],"book":"HB-WA-s4-learn"}
"""MWG1: explicit trusted RAM-frame guidance; no input or learning authority.

Origin: MEMORYWEB_BUILD_ARCH MWG1 and mwg1_w.md. All ceilings are supplied by
trusted composition [TARGET]. Geometry is device pixels, never inferred CSS.
A mini-demo callback attests an independently recorded, custody-validated demo;
its opaque local reference is not a symbolic recording synthesized here.
"""
from dataclasses import dataclass, fields
import re

from wender.scripts.types import AdmissionCheck, Reason, Truth
from wender.perceive.vision_bridge import changed_regions


# @anchor:wa.learn.mw_guidance.GuidancePolicy
@dataclass(frozen=True)
class GuidancePolicy:
    quiet_ns: int
    poll_ns: int
    max_reperceptions: int
    max_events: int
    max_regions: int
    metadata_bytes: int
    frame_peak_bytes: int
    frame_retained_bytes: int


# @anchor:wa.learn.mw_guidance.GuidanceFrame
@dataclass(frozen=True)
class GuidanceFrame:
    scope: object
    authority: object
    surface: object
    window: str
    sequence: int
    capture_start_ns: int
    capture_end_ns: int
    width: int
    height: int
    frame: object
    custody: AdmissionCheck
    allocation: object


# @anchor:wa.learn.mw_guidance.GuidanceEvidence
@dataclass(frozen=True)
class GuidanceEvidence:
    step_seq: int
    time_ns: int
    box_px: tuple[int, int, int, int]
    classification: str
    before_sequence: int
    after_sequence: int
    demo_ref: str | None


# @anchor:wa.learn.mw_guidance.GuidanceDecision
@dataclass(frozen=True)
class GuidanceDecision:
    disposition: str
    reason: Reason | None
    evidence: tuple[GuidanceEvidence, ...]


# @anchor:wa.learn.mw_guidance.GuidanceOwner
@dataclass(frozen=True)
class GuidanceOwner:
    """Immutable trusted callback binding. No dynamic loading from corpus data.

    capture(step_state, *, deadline_ns) -> GuidanceFrame
    check(frame, step_state) -> AdmissionCheck
    expected_regions(step_state) -> tuple[xyxy, ...], or None when unknown
    reperceive(frame, step_state, *, deadline_ns) -> AdmissionCheck
    mini_demo(evidence, step_state, *, deadline_ns) -> str | None
    release(frame, *, deadline_ns) -> AdmissionCheck

    mini_demo is a provenance attestation by the registered recorder owner: it
    may return only a reference to a complete, validated recorder evidence pack.
    None explicitly records recorder unavailability without inventing evidence.
    """
    policy: GuidancePolicy
    adapter: object
    capture: object
    check: object
    expected_regions: object
    reperceive: object
    mini_demo: object
    release: object


def _yes(value):
    return type(value) is AdmissionCheck and value.result is Truth.TRUE and value.reason is None


# @anchor:wa.learn.mw_guidance.validate_owner
def validate_owner(owner):
    if (type(owner) is not GuidanceOwner or type(owner.policy) is not GuidancePolicy or
            owner.adapter is None or not callable(getattr(owner.adapter, 'changed_regions', None)) or
            not all(callable(getattr(owner, name)) for name in
                    ('capture', 'check', 'expected_regions', 'reperceive', 'mini_demo', 'release')) or
            not all(type(getattr(owner.policy, f.name)) is int and getattr(owner.policy, f.name) > 0
                    for f in fields(GuidancePolicy))):
        return AdmissionCheck(Truth.FALSE, Reason.TYPE)
    return AdmissionCheck(Truth.TRUE, None)


def _box(box, width, height):
    return (type(box) in (list, tuple) and len(box) == 4 and all(type(x) is int for x in box) and
            0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)


def _local_ref(value):
    return value is None or (type(value) is str and
                             re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value) is not None)


# @anchor:wa.learn.mw_guidance.validate_frame
def validate_frame(frame, step_state, owner):
    run = step_state.run
    valid = (type(frame) is GuidanceFrame and frame.scope == run.scope and
             frame.authority == run.authority and frame.surface is not None and
             type(frame.window) is str and bool(frame.window) and
             all(type(getattr(frame, name)) is int and getattr(frame, name) >= 0 for name in
                 ('sequence', 'capture_start_ns', 'capture_end_ns', 'width', 'height')) and
             frame.width > 0 and frame.height > 0 and frame.capture_start_ns <= frame.capture_end_ns and
             frame.capture_start_ns >= (step_state.guidance_start_ns if
                 step_state.guidance_start_ns is not None else step_state.start_ns) and
             frame.width * frame.height * 3 <= owner.policy.frame_retained_bytes and
             frame.allocation is not None and _yes(frame.custody) and
             getattr(frame.frame, 'width', None) == frame.width and
             getattr(frame.frame, 'height', None) == frame.height)
    if not valid or not _yes(owner.check(frame, step_state)):
        return AdmissionCheck(Truth.FALSE, Reason.STALE)
    return AdmissionCheck(Truth.TRUE, None)


# @anchor:wa.learn.mw_guidance.classify_guidance
def classify_guidance(before, after, *, step_state, policy, regions, expected_regions,
                      quiet_start_ns, quiet_boundary_ns):
    """Pure metadata classifier; quiet witnesses are observed unchanged pairs.

    Caller validates frame custody and dimensions before invoking perception.
    Unknown/overlapping effects stay ambiguous; no pixel subtraction is allowed.
    """
    refused = lambda reason: GuidanceDecision('refused', reason, ())
    if (type(policy) is not GuidancePolicy or not all(type(getattr(policy, f.name)) is int and
            getattr(policy, f.name) > 0 for f in fields(GuidancePolicy))):
        return refused(Reason.TYPE)
    if step_state.run.uncertain or step_state.dispatch_disposition == 'INPUT_UNCERTAIN':
        return refused(Reason.RECONCILIATION)
    if (type(before) is not GuidanceFrame or type(after) is not GuidanceFrame or
            not all(type(getattr(f, k)) is int and getattr(f, k) >= 0 for f in (before, after)
                    for k in ('sequence', 'capture_start_ns', 'capture_end_ns', 'width', 'height')) or
            not (before.scope == after.scope == step_state.run.scope and
                 before.authority == after.authority == step_state.run.authority and
                 before.surface is after.surface and before.window == after.window and
                 (before.width, before.height) == (after.width, after.height) and
                 before.sequence < after.sequence and
                 before.capture_start_ns <= before.capture_end_ns <= after.capture_start_ns <= after.capture_end_ns and
                 _yes(before.custody) and _yes(after.custody))):
        return refused(Reason.STALE)
    if type(regions) not in (list, tuple) or len(regions) > policy.max_regions:
        return refused(Reason.CAPACITY)
    boxes = []
    for region in regions:
        if not all(type(getattr(region, k, None)) is int for k in ('x', 'y', 'w', 'h')):
            return refused(Reason.TYPE)
        box = (region.x, region.y, region.x + region.w, region.y + region.h)
        if not _box(box, after.width, after.height):
            return refused(Reason.BOUNDS)
        boxes.append(box)
    if not boxes:
        return GuidanceDecision('quiet', None, ())
    if step_state.input_ns is None:
        if step_state.dispatch_disposition != 'NOT_SENT':
            return refused(Reason.RECONCILIATION)
        prior = step_state.run.last_input_ns
        if prior is not None and (quiet_boundary_ns is None or quiet_boundary_ns <= prior or
                                  quiet_start_ns is None or quiet_start_ns <= prior or
                                  quiet_boundary_ns - quiet_start_ns < policy.quiet_ns):
            return GuidanceDecision('ambiguous', None, ())
        category = 'before_input'
    else:
        if (step_state.dispatch_disposition != 'SENT' or quiet_start_ns is None or
                quiet_boundary_ns is None or quiet_start_ns <= step_state.input_ns or
                quiet_boundary_ns - quiet_start_ns < policy.quiet_ns or
                before.capture_end_ns < quiet_boundary_ns):
            return GuidanceDecision('ambiguous', None, ())
        if type(expected_regions) is not tuple or not expected_regions or not all(
                _box(box, after.width, after.height) for box in expected_regions):
            return GuidanceDecision('ambiguous', None, ())
        if any(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
               for a in boxes for b in expected_regions):
            return GuidanceDecision('ambiguous', None, ())
        category = 'outside_expected_after_quiet'
    return GuidanceDecision('guidance', None, tuple(GuidanceEvidence(
        step_state.seq, after.capture_end_ns, box, category, before.sequence, after.sequence, None)
        for box in boxes))


# @anchor:wa.learn.mw_guidance.validate_payload
def validate_payload(value):
    """Closed authenticated journal payload; dimensions are checked at capture."""
    return (type(value) is dict and set(value) == {'time_ns', 'box_px', 'classification',
            'before_sequence', 'after_sequence', 'demo_ref'} and
            all(type(value[k]) is int and value[k] >= 0 for k in
                ('time_ns', 'before_sequence', 'after_sequence')) and
            value['before_sequence'] < value['after_sequence'] and
            _box(value['box_px'], 2147483647, 2147483647) and
            value['classification'] in ('before_input', 'outside_expected_after_quiet') and
            _local_ref(value['demo_ref']))


# @anchor:wa.learn.mw_guidance.evidence_reference
def evidence_reference(evidence, digest):
    value = dict(time_ns=evidence.time_ns, box_px=evidence.box_px,
                 classification=evidence.classification, before_sequence=evidence.before_sequence,
                 after_sequence=evidence.after_sequence, demo_ref=evidence.demo_ref)
    if (not validate_payload(value) or type(evidence.step_seq) is not int or evidence.step_seq < 1 or
            type(digest) is not str or re.fullmatch('[0-9a-f]{64}', digest) is None):
        raise ValueError('OUT_OF_SUPPORT')
    ref = 'operator_guidance:' + ':'.join((evidence.classification, str(evidence.step_seq),
            str(evidence.time_ns), ','.join(map(str, evidence.box_px)), digest))
    if evidence.demo_ref is not None:
        ref += ':' + evidence.demo_ref
    if len(ref) > 400:
        raise ValueError('OUT_OF_SUPPORT')
    return ref
