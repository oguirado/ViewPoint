# @module {"id":"wa.tests.mw_guidance_unit","layer":4,"owner":"LEARN","deps":["wa.learn.mw_guidance","wa.learn.mw_workflow","wa.learn.mw_learn"],"book":"HB-WA-s4-learn"}
"""MWG1 public API predicates, RAM recorder control and removal mutants.

All fixture ceilings are explicit [TARGET]. No frame is persisted outside RAM.
MWG1_MUTANT applies one in-memory removal; normal test runs never mutate code.
"""
import copy
from dataclasses import fields, replace
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tools import memoryweb_gate as gate
from wender.learn import mw_guidance as m
from wender.learn import mw_workflow as w
from wender.perceive.vision_adapter import Frame
from wender.perceive.vision_bridge import ChangedRegion
from tests import test_mw_run_unit as base
from tests import test_mw_learn_unit as learning

YES, NO = base.YES, base.NO
POLICY = m.GuidancePolicy(1_000_000, 1, 4, 8, 4, 8192, 4096, 300)
REGION = ChangedRegion(6, 6, 2, 2, 1.0)

# target module, exact source removal, replacement, discriminating public test.
MUTANTS = {
    'policy': ('m', 'type(getattr(owner.policy, f.name)) is int and getattr(owner.policy, f.name) > 0', 'True', 'test_policy'),
    'custody': ('m', 'not _yes(owner.check(frame, step_state))', 'False', 'test_frame_scope'),
    'scope': ('m', 'frame.scope == run.scope', 'True', 'test_frame_scope'),
    'uncertain': ('m', "step_state.run.uncertain or step_state.dispatch_disposition == 'INPUT_UNCERTAIN'", 'False', 'test_classifier_uncertain'),
    'overlap': ('m', 'if any(a[0] < b[2]', 'if False and any(a[0] < b[2]', 'test_own_effect'),
    'quiet': ('m', 'quiet_boundary_ns - quiet_start_ns < policy.quiet_ns', 'False', 'test_quiet'),
    'expected': ('m', "if type(expected_regions) is not tuple or not expected_regions or not all(", 'if False and (type(expected_regions) is not tuple or not expected_regions) or not all(', 'test_expected_unknown'),
    'regions': ('m', 'len(regions) > policy.max_regions', 'False', 'test_region_budget'),
    'geometry': ('m', 'if not _box(box, after.width, after.height):', 'if False:', 'test_geometry'),
    'payload': ('m', "set(value) == {'time_ns', 'box_px', 'classification',", "{'time_ns', 'box_px', 'classification',", 'test_payload_closed'),
    'reference': ('m', 'if len(ref) > 400:', 'if False:', 'test_reference_bound'),
    'reperceptions': ('w', 'state.guidance_reperceptions < policy.max_reperceptions', 'True', 'test_reperception_budget'),
    'events': ('w', 'state.guidance_events + len(decision.evidence) <= policy.max_events', 'True', 'test_event_budget'),
    'bytes': ('w', 'state.guidance_bytes + size <= policy.metadata_bytes', 'True', 'test_byte_budget'),
    'acquisitions': ('w', '_need(state.acquisitions < owners.limits.acquisitions)\n', 'pass\n', 'test_acquisition_budget'),
    'release': ('w', '_need(_admitted(owners.guidance.release(frame, deadline_ns=owners.clock() + owners.limits.drain_ns)))', 'owners.guidance.release(frame, deadline_ns=owners.clock() + owners.limits.drain_ns)', 'test_release_failure'),
    'restart': ('w', 'state.resume_blocked = True\n        if not is_guidance', 'state.resume_blocked = False\n        if not is_guidance', 'test_guidance_restart'),
    'intent': ('w', "not outstanding and guidance.validate_payload(payload)", 'guidance.validate_payload(payload)', 'test_outstanding_intent'),
    'phase': ('w', "event['phase'] == ('delivery' if event['kind'] in ('intent', 'complete') else 'terminal')", 'True', 'test_journal_integrity'),
    'revalidate': ('w', '_need(_admitted(guidance.validate_frame(frame, ss, owner)))', 'pass', 'test_final_custody'),
    'frozen': ('w', 'unchanged and _admitted(refreshed)', '_admitted(refreshed)', 'test_frozen_state'),
    'divert': ('w', 'if outcome is _GUIDANCE_RECHECK:', 'if False:', 'test_callback_sentinel'),
    'semantic': ('w', "_need(_predicate(predicate, state, owners, ss.deadline_ns) is Truth.TRUE, 'LOW_CONFIDENCE')", 'pass', 'test_broken_precondition'),
}
MUTANTS.update({
    'postcondition': ('w', "if truth is Truth.TRUE:\n                passed = True", "if True:\n                passed = True", 'test_postcondition_and_busy'),
    'busy': ('w', 'if busy is not None and not state.busy_seen:', 'if False:', 'test_postcondition_and_busy'),
    'grounding': ('w', 'ss.target = None\n        ss.consecutive = 0', 'ss.consecutive = 0', 'test_grounding_invalidation'),
    'stability': ('w', 'ss.consecutive = 0\n        # Do not touch', 'pass\n        # Do not touch', 'test_stability_reset'),
    'snapshot': ('w', 'and snapshots == state.snapshots', 'and True', 'test_snapshot_mutation'),
    'authorization': ('w', '_admitted(owners.authorization.check(phase, state.workflow, state.scope, state.authority))', 'True', 'test_authority_change'),
    'progress': ('w', "        ss.target = None\n        ss.consecutive = 0", "        state.last_progress_ns = owners.clock()\n        ss.target = None\n        ss.consecutive = 0", 'test_postinput_completion'),
    'journal_auth': ('w', 'and _admitted(owners.journal.authenticate(event))', '', 'test_journal_integrity'),
    'journal_torn': ('w', "if raw and not raw.endswith(b'\\n'):", 'if False:', 'test_journal_integrity'),
    'journal_duplicate': ('w', "_need(box not in guidance_boxes, 'TORN', 3)", 'pass', 'test_journal_integrity'),
    'journal_keys': ('w', "_need(set(event) == keys | ({'guidance'} if is_guidance else set()), 'TORN', 3)", 'pass', 'test_journal_extra_key'),
    'snapshot_semantic': ('w', 'answer = bool(value - old.value)', 'answer = bool(value)', 'test_snapshot_negative'),
    'preinput_satisfaction': ('w', '_admitted(owners.capabilities.satisfaction(step, ss))', 'True', 'test_unknown_satisfaction'),
    'uncertain_runner': ('w', "_need(not state.uncertain and ss.dispatch_disposition != 'INPUT_UNCERTAIN', 'INPUT_UNCERTAIN')", 'pass', 'test_uncertain_verification'),
    'journal_capacity': ('w', 'state.journal_seq + reserve_count <= owners.limits.event_count', 'True', 'test_journal_capacity'),
})
MUTANTS.update({
    'receipt_closed': ('gate', "if schema.get('additionalProperties') is False and value.keys() - props.keys():", 'if False:', 'test_closed_receipt'),
    'receipt_reference': ('gate', "len(value) > schema.get('maxLength', sys.maxsize)", 'False', 'test_closed_receipt'),
    'receipt_disposition': ('gate', "_require(disposition in groups, 'COUNTS_INVENTORY')", 'pass', 'test_closed_receipt'),
    'mini_manifest': ('learn', "manifest['steps_file'] == 'steps.json' and manifest['steps_sha256'] == hashlib.sha256(projection.payload).hexdigest()", 'True', 'test_recorder_provenance'),
    'local_reference': ('m', "re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value) is not None", 'True', 'test_privacy'),
    'peak_memory': ('w', 'policy.frame_peak_bytes <= limits.peak_bytes', 'True', 'test_memory_budgets'),
    'retained_memory': ('w', '2 * policy.frame_retained_bytes <= limits.retained_bytes', 'True', 'test_memory_budgets'),
    'stuck': ('w', 'now - state.run.last_progress_ns >= 3 * owners.limits.no_progress_ns', 'False', 'test_stuck'),
    'repeat': ('w', "_need(ss.iteration < repeat['max_iterations'], 'STUCK')", 'pass', 'test_repeat_bound'),
})
MUTANTS.update({
    'released_reference': ('w', 'state.current_step.guidance_baseline = None', 'pass', 'test_preinput_completion'),
    'pair_replay': ('w', "_need(pair not in state.guidance_pairs, 'TORN', 3)", 'pass', 'test_frame_pair_replay'),
})
MUTANTS.update({
    'current_guard': ('w', 'if _condition_skip(step, ss, owners):', 'if False:', 'test_current_guard'),
})
if os.environ.get('MWG1_MUTANT'):
    module_name, old, new, _ = MUTANTS[os.environ['MWG1_MUTANT']]
    module = {'m': m, 'w': w, 'gate': gate, 'learn': learning.w}[module_name]
    source = Path(module.__file__).read_text()
    if old not in source:
        raise RuntimeError('mutant anchor absent')
    source = source.replace(old, new)
    if os.environ['MWG1_MUTANT'] == 'stuck':
        source = source.replace("_need(now - state.last_progress_ns < 3 * owners.limits.no_progress_ns, 'STUCK')", 'pass')
    if os.environ['MWG1_MUTANT'] == 'journal_torn':
        source = source.replace("line == (gate.canon(event) + '\\n').encode()", 'True')
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    POLICY = m.GuidancePolicy(1_000_000, 1, 4, 8, 4, 8192, 4096, 300)


class _Adapter:
    def __init__(self, fixture):
        self.fixture = fixture

    # @anchor:wa.tests.mw_guidance_unit._Adapter.changed_regions
    def changed_regions(self, before, after):
        if self.fixture.failure == 'diff':
            raise RuntimeError('PRIVATE_PIXEL_CANARY')
        return self.fixture.changes.get(after.data[0], ())


class _Guidance:
    def __init__(self, fixture, changes):
        self.f = fixture
        self.changes = changes
        self.held = []
        self.released = []
        self.captured = []
        self.refreshed = []
        self.progress_samples = []
        self.failure = None
        self.denied = False
        self.foreign = False
        self.demo = None
        self.hook = None
        self.check_hook = None
        self.expected = ((0, 0, 3, 3),)
        self.owner = m.GuidanceOwner(POLICY, _Adapter(self), self.capture, self.check,
            self.expected_regions, self.reperceive, self.mini_demo, self.release)

    # @anchor:wa.tests.mw_guidance_unit._Guidance.capture
    def capture(self, ss, *, deadline_ns):
        if self.failure == 'capture':
            raise RuntimeError('PRIVATE_CAPTURE_CANARY')
        self.f.clock.sleep(1_000_000)
        seq = len(self.captured) + 1
        allocation = object()
        frame = m.GuidanceFrame('foreign' if self.foreign else 'scope', 'authority', self.f.surface,
            'w', seq, self.f.clock(), self.f.clock(), 10, 10, Frame(10, 10, bytes([seq % 256]) * 300), YES, allocation)
        self.progress_samples.append(ss.run.last_progress_ns)
        self.held.append(allocation)
        self.captured.append((ss, frame, deadline_ns))
        return frame

    # @anchor:wa.tests.mw_guidance_unit._Guidance.check
    def check(self, frame, ss):
        if self.check_hook:
            return self.check_hook(frame, ss)
        return NO if self.denied else YES

    # @anchor:wa.tests.mw_guidance_unit._Guidance.expected_regions
    def expected_regions(self, ss):
        return self.expected

    # @anchor:wa.tests.mw_guidance_unit._Guidance.reperceive
    def reperceive(self, frame, ss, *, deadline_ns):
        self.refreshed.append((ss, frame, deadline_ns, ss.input_ns, ss.run.last_progress_ns))
        if self.failure == 'reperceive':
            raise RuntimeError('PRIVATE_OCR_CANARY')
        if self.hook:
            return self.hook(frame, ss)
        self.f.observations.values['satisfied'] = [True]
        self.f.observations.values['post'] = [True]
        return YES

    # @anchor:wa.tests.mw_guidance_unit._Guidance.mini_demo
    def mini_demo(self, evidence, ss, *, deadline_ns):
        if self.failure == 'export':
            raise RuntimeError('PRIVATE_SLOT_CANARY')
        return self.demo

    # @anchor:wa.tests.mw_guidance_unit._Guidance.release
    def release(self, frame, *, deadline_ns):
        if self.failure == 'release':
            return NO
        if frame.allocation in self.held:
            self.held.remove(frame.allocation)
            self.released.append(frame.sequence)
        return YES


# @anchor:wa.tests.mw_guidance_unit.TestGuidance
class TestGuidance(unittest.TestCase):
    def _fixture(self, changes=None, *, satisfied=False):
        f = base.TestWorkflow('test_nominal_real_gate')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f._click()
        step = f.workflow['steps'][0]
        step['action']['delivery_budget_ms'] = 50
        step['already_satisfied'] = base._predicate('satisfied') if satisfied else None
        step['verify']['predicate'] = base._predicate('post')
        f.observations.values['satisfied'] = [False]
        h = _Guidance(f, changes or {})
        f.owners = replace(f.owners, guidance=h.owner)
        return f, h

    def _run(self, f, h, *, rc=0, sends=0, events=None):
        result = f._run()
        self.assertEqual(result.rc, rc, result)
        self.assertEqual(len(f.transport.sent), sends)
        self.assertFalse(h.held)
        for ss, _, _ in h.captured:
            self.assertIsNone(ss.guidance_baseline)
        entries = [gate.strict_load(line) for line in f.journal.raw.splitlines()]
        guidance = [row for row in entries if row['kind'] == 'operator_guidance']
        if events is not None:
            self.assertEqual(len(guidance), events)
        for row in guidance:
            self.assertEqual(row['phase'], 'terminal')
            self.assertTrue(m.validate_payload(row['guidance']))
            self.assertIs(f.journal.authenticate(row).result, w.Truth.TRUE)
        for ss, _, deadline in h.captured:
            self.assertLessEqual(deadline, ss.deadline_ns)
            self.assertEqual(ss.verify_deadline_ns, 101_000_000)
        return result

    def _classify(self, *, sent=True, regions=(REGION,), expected=((0, 0, 3, 3),), quiet=(2_000_000, 4_000_000)):
        f, h = self._fixture()
        run = w.RunState(b'', 'a'*64, f.workflow, 'scope', 'authority', {}, last_input_ns=1 if sent else None)
        ss = w.StepState(1, run, 100_000_000, 100_000_000, 100_000_000, start_ns=0,
                         input_ns=1 if sent else None, dispatch_disposition='SENT' if sent else 'NOT_SENT')
        before = m.GuidanceFrame('scope', 'authority', f.surface, 'w', 1, 4_000_000, 4_000_000,
                                 10, 10, Frame(10, 10, b'a'*300), YES, object())
        after = replace(before, sequence=2, capture_start_ns=5_000_000, capture_end_ns=5_000_000)
        return m.classify_guidance(before, after, step_state=ss, policy=POLICY, regions=regions,
                                  expected_regions=expected, quiet_start_ns=quiet[0], quiet_boundary_ns=quiet[1])

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_preinput_completion
    def test_preinput_completion(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        result = self._run(f, h, events=1)
        self.assertEqual(result.summary['steps_skipped_satisfied'], 1)
        self.assertIn('operator_guidance:before_input:1:', result.summary['evidence_refs'][0])
        self.assertIsNone(gate.strict_load(f.journal.raw.splitlines()[0])['guidance']['demo_ref'])
        self.assertTrue(f.dispatch.check_ready())

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_stale_foreign
    def test_stale_foreign(self):
        for attribute in ('denied', 'foreign'):
            with self.subTest(attribute=attribute):
                f, h = self._fixture({2: (REGION,)}, satisfied=True)
                setattr(h, attribute, True)
                self._run(f, h, rc=5, events=0)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_frame_scope
    def test_frame_scope(self):
        f, h = self._fixture()
        run = w.RunState(b'', '', {}, 'scope', 'authority', {})
        ss = w.StepState(1, run, 100_000_000, 100_000_000, 100_000_000, start_ns=0)
        frame = h.capture(ss, deadline_ns=100_000_000)
        self.assertIs(m.validate_frame(frame, ss, h.owner).result, w.Truth.TRUE)
        self.assertIs(m.validate_frame(replace(frame, scope='foreign'), ss, h.owner).result, w.Truth.FALSE)
        h.denied = True
        self.assertIs(m.validate_frame(frame, ss, h.owner).result, w.Truth.FALSE)
        h.release(frame, deadline_ns=f.clock())

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_policy
    def test_policy(self):
        for field in fields(POLICY):
            for value in (True, 0, -1, None):
                with self.subTest(field=field.name, value=value):
                    f, h = self._fixture()
                    f.owners = replace(f.owners, guidance=replace(h.owner, policy=replace(POLICY, **{field.name: value})))
                    self.assertIs(m.validate_owner(f.owners.guidance).result, w.Truth.FALSE)
                    result = self._run(f, h, rc=5, events=0)
                    self.assertEqual(result.verdict_code, 'OUT_OF_SUPPORT')
        self.assertIs(m.validate_owner(h.owner).result, w.Truth.TRUE)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_own_effect
    def test_own_effect(self):
        self.assertEqual(self._classify(regions=(ChangedRegion(1, 1, 2, 2, 1.0),)).disposition, 'ambiguous')
        f, h = self._fixture({4: (ChangedRegion(1, 1, 2, 2, 1.0),)})
        self._run(f, h, sends=1, events=0)
        self.assertEqual(self._classify().disposition, 'guidance')

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_quiet
    def test_quiet(self):
        self.assertEqual(self._classify(quiet=(3_999_999, 4_000_000)).disposition, 'ambiguous')
        self.assertEqual(self._classify(quiet=(None, None)).disposition, 'ambiguous')
        self.assertEqual(self._classify().disposition, 'guidance')

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_expected_unknown
    def test_expected_unknown(self):
        for expected in ((), None, ((0, 0, True, 3),)):
            with self.subTest(expected=expected):
                self.assertEqual(self._classify(expected=expected).disposition, 'ambiguous')
        self.assertEqual(self._classify().disposition, 'guidance')

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_classifier_uncertain
    def test_classifier_uncertain(self):
        f, h = self._fixture()
        run = w.RunState(b'', '', {}, 'scope', 'authority', {}, uncertain=True)
        ss = w.StepState(1, run, 10, 10, 10, dispatch_disposition='INPUT_UNCERTAIN')
        decision = m.classify_guidance(None, None, step_state=ss, policy=POLICY, regions=(),
                expected_regions=None, quiet_start_ns=None, quiet_boundary_ns=None)
        self.assertIs(decision.reason, w.Reason.RECONCILIATION)
        self.assertTrue(run.uncertain)
        self.assertEqual(self._classify().disposition, 'guidance')

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_postinput_completion
    def test_postinput_completion(self):
        f, h = self._fixture({6: (REGION,)})
        f.observations.values['post'] = [False]
        result = self._run(f, h, sends=1, events=1)
        self.assertEqual(result.summary['steps_verified'], 1)
        ss, _, _, input_ns, progress = h.refreshed[0]
        self.assertIsNotNone(input_ns)
        self.assertEqual(input_ns, ss.input_ns)
        self.assertEqual(progress, 1_000_000)
        self.assertEqual(h.progress_samples[6], 1_000_000)
        self.assertFalse(ss.run.uncertain)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_postcondition_and_busy
    def test_postcondition_and_busy(self):
        for busy in (False, True):
            f, h = self._fixture({6: (REGION,)})
            f.observations.values['post'] = [False]
            if busy:
                f.workflow['steps'][0]['verify']['busy_predicate'] = base._predicate('busy')
                f.observations.values['busy'] = [False]
            if not busy:
                h.hook = lambda frame, ss: YES
            self._run(f, h, rc=5, sends=1, events=1)
            self.assertFalse(h.refreshed[0][0].run.uncertain)
        f, h = self._fixture({6: (REGION,)})
        f.observations.values['post'] = [False]
        self._run(f, h, sends=1, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_callback_sentinel
    def test_callback_sentinel(self):
        f, h = self._fixture({3: (REGION,)}, satisfied=True)
        result = self._run(f, h, events=1)
        self.assertTrue(f.dispatch.check_ready())
        self.assertEqual(result.summary['steps_skipped_satisfied'], 1)
        self.assertIsNone(h.refreshed[0][0].input_ns)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_grounding
    def test_grounding(self):
        f, h = self._fixture({3: (REGION,)})
        self._run(f, h, sends=1, events=1)
        self.assertEqual(f.capabilities.targets, 2)
        self.assertTrue(f.dispatch.check_ready())
        f, h = self._fixture({3: (REGION,)})
        def revoke(frame, ss):
            f.capabilities.no_focus = True
            f.authorization.denied = 'all'
            return YES
        h.hook = revoke
        self._run(f, h, rc=5, sends=0)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_broken_precondition
    def test_broken_precondition(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        f.workflow['steps'][0]['preconditions'] = [base._predicate('required')]
        f.observations.values['required'] = [False]
        self._run(f, h, rc=5, events=1)
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_broken_bound
    def test_broken_bound(self):
        f, h = self._fixture({3: (REGION,), 4: (REGION,), 5: (REGION,), 6: (REGION,), 7: (REGION,)})
        h.hook = lambda frame, ss: YES
        result = self._run(f, h, rc=5, sends=0)
        self.assertIn(result.verdict_code, ('STUCK', 'OUT_OF_SUPPORT', 'DEADLINE'))
        self.assertFalse(h.refreshed[0][0].run.uncertain)
        self.assertEqual(h.refreshed[0][0].run.last_progress_ns, 1_000_000)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_region_budget
    def test_region_budget(self):
        self.assertEqual(self._classify(regions=(REGION,)*5).disposition, 'refused')
        self.assertEqual(self._classify().disposition, 'guidance')

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_geometry
    def test_geometry(self):
        for region in (ChangedRegion(-1, 0, 2, 2, 1.0), ChangedRegion(0, 0, 20, 2, 1.0),
                       ChangedRegion(True, 0, 2, 2, 1.0)):
            self.assertEqual(self._classify(regions=(region,)).disposition, 'refused')
        self.assertEqual(self._classify().evidence[0].box_px, (6, 6, 8, 8))

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_reperception_budget
    def test_reperception_budget(self):
        f, h = self._fixture({3: (REGION,), 4: (REGION,)})
        h.owner = replace(h.owner, policy=replace(POLICY, max_reperceptions=1))
        f.owners = replace(f.owners, guidance=h.owner)
        self._run(f, h, rc=5, events=1)
        self.assertEqual(len(h.refreshed), 1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_event_budget
    def test_event_budget(self):
        f, h = self._fixture({3: (REGION,), 4: (REGION,)})
        h.owner = replace(h.owner, policy=replace(POLICY, max_events=1))
        f.owners = replace(f.owners, guidance=h.owner)
        self._run(f, h, rc=5, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_byte_budget
    def test_byte_budget(self):
        f, h = self._fixture({2: (REGION,)})
        h.owner = replace(h.owner, policy=replace(POLICY, metadata_bytes=1))
        f.owners = replace(f.owners, guidance=h.owner)
        self._run(f, h, rc=5, events=0)
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_acquisition_budget
    def test_acquisition_budget(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        f.limits = replace(f.limits, acquisitions=1)
        f.owners = replace(f.owners, limits=f.limits)
        self._run(f, h, rc=5, events=0)
        self.assertEqual(len(h.captured), 1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_reservation_before_producer
    def test_reservation_before_producer(self):
        for name in ('guidance_peak', 'guidance_retained', 'guidance_diff_peak', 'guidance_metadata'):
            f, h = self._fixture({2: (REGION,)}, satisfied=True)
            f.resources.denied = name
            self._run(f, h, rc=5, events=0)
            if name in ('guidance_peak', 'guidance_retained'):
                self.assertFalse(h.captured)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_cleanup_failures
    def test_cleanup_failures(self):
        for failure in ('capture', 'diff', 'reperceive', 'export'):
            f, h = self._fixture({2: (REGION,)}, satisfied=True)
            h.failure = failure
            result = self._run(f, h, rc=5)
            self.assertNotIn('PRIVATE_', repr(result))
            self.assertNotIn(b'PRIVATE_', f.journal.raw)
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_release_failure
    def test_release_failure(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        h.failure = 'release'
        result = f._run()
        self.assertEqual(result.rc, 5)
        self.assertTrue(h.held)
        self.assertNotEqual((f.journal.summary or {}).get('rc'), 0)
        h.failure = None
        for _, frame, _ in h.captured:
            h.release(frame, deadline_ns=f.clock())
        self.assertFalse(h.held)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_frozen_state
    def test_frozen_state(self):
        for field in ('deadline_ns', 'locate_deadline_ns', 'verify_deadline_ns', 'iteration'):
            f, h = self._fixture({2: (REGION,)}, satisfied=True)
            def renew(frame, ss, field=field):
                setattr(ss, field, getattr(ss, field) + 100_000_000)
                return YES
            h.hook = renew
            self._run(f, h, rc=5, events=0)
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_snapshots
    def test_snapshots(self):
        f, h = self._fixture({3: (REGION,)})
        predicate = base._predicate('history', comparison='set_grew', snapshot='before')
        f.workflow['steps'][0]['capture_before'] = [dict(name='before', observe=base._predicate('history'))]
        f.workflow['steps'][0]['verify'].update(mode='side_effect', predicate=predicate)
        f.observations.values['history'] = [frozenset({'old'}), frozenset({'old', 'new'})]
        result = self._run(f, h, sends=1, events=1)
        self.assertEqual(result.rc, 0)
        self.assertEqual(h.refreshed[0][0].run.snapshots['before'].observation.value, frozenset({'old'}))

    def _payload(self):
        return dict(time_ns=5, box_px=[1, 1, 2, 2], classification='before_input',
                    before_sequence=1, after_sequence=2, demo_ref=None)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_payload_closed
    def test_payload_closed(self):
        payload = self._payload()
        self.assertTrue(m.validate_payload(payload))
        for bad in (dict(payload, pixels='CANARY'), dict(payload, time_ns=True), dict(payload, box_px=[0, 0, 0, 1]),
                    dict(payload, demo_ref='/tmp/frame.png'), dict(payload, classification='human_identity'),
                    dict(payload, after_sequence=1)):
            self.assertFalse(m.validate_payload(bad))

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_reference_bound
    def test_reference_bound(self):
        evidence = m.GuidanceEvidence(1, 1, (0, 0, 1, 1), 'before_input', 1, 2, None)
        self.assertLessEqual(len(m.evidence_reference(evidence, 'a'*64)), 400)
        with self.assertRaises(ValueError):
            m.evidence_reference(replace(evidence, step_seq=10**400), 'a'*64)

    def _journal_row(self, f, *, kind='operator_guidance', phase='terminal', payload=None, seq=1, prev=None):
        event = dict(seq=seq, prev=prev, kind=kind, step_seq=1, phase=phase)
        if kind == 'operator_guidance':
            event['guidance'] = self._payload() if payload is None else payload
        event['digest'] = f.journal.digest(gate.canon(event).encode(), prev)
        return (gate.canon(event) + '\n').encode()

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_guidance_restart
    def test_guidance_restart(self):
        f, h = self._fixture()
        f.journal.raw = self._journal_row(f)
        result = self._run(f, h, rc=5, events=1)
        self.assertEqual(result.verdict_code, 'OUT_OF_SUPPORT')
        self.assertFalse(h.captured)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_journal_integrity
    def test_journal_integrity(self):
        for kind in ('extra', 'phase', 'digest', 'torn', 'duplicate'):
            f, h = self._fixture()
            payload = self._payload()
            if kind == 'extra':
                payload['ocr'] = 'PRIVATE_CANARY'
            raw = self._journal_row(f, phase='delivery' if kind == 'phase' else 'terminal', payload=payload)
            if kind == 'digest':
                raw = raw.replace(b'"time_ns":5', b'"time_ns":6')
            if kind == 'torn':
                raw = raw[:-1]
            if kind == 'duplicate':
                previous = gate.strict_load(raw)['digest']
                raw += self._journal_row(f, seq=2, prev=previous)
            f.journal.raw = raw
            result = f._run()
            self.assertEqual(result.verdict_code, 'TORN', (kind, result))
            self.assertEqual(f.transport.sent, [])
            self.assertFalse(h.held)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_outstanding_intent
    def test_outstanding_intent(self):
        f, h = self._fixture()
        first = self._journal_row(f, kind='intent', phase='delivery')
        f.journal.raw = first + self._journal_row(f, seq=2, prev=gate.strict_load(first)['digest'])
        self.assertEqual(f._run().verdict_code, 'TORN')
        self.assertEqual(f.transport.sent, [])
        self.assertFalse(h.held)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_closed_receipt
    def test_closed_receipt(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        result = self._run(f, h, events=1)
        defs = gate.compile_bundle(base.BUNDLE)
        self.assertTrue(gate.conforms(result.summary, defs['run'], defs))
        for bad in (dict(result.summary, operator_guidance=True), dict(result.summary, evidence_refs=['x'*401])):
            self.assertFalse(gate.conforms(bad, defs['run'], defs))
        inventory = copy.deepcopy(f.journal.inventory)
        inventory['steps'][0]['disposition'] = 'guided'
        f.journal.inventory = inventory
        self.assertEqual(gate.validate_file(Path(f.journal.selected), base.BUNDLE,
                                           context=f.journal.context()).category, 'COUNTS_INVENTORY')

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_privacy
    def test_privacy(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        h.pixels = b'PRIVATE_PIXEL_CANARY'
        h.ocr = 'PRIVATE_OCR_CANARY'
        h.slot = 'PRIVATE_SLOT_CANARY'
        h.path = '/PRIVATE_PATH_CANARY.png'
        result = self._run(f, h, events=1)
        self.assertNotIn('PRIVATE_', repr(result))
        self.assertNotIn(b'PRIVATE_', f.journal.raw)
        for ref in ('/tmp/demo', 'slot secret', 'x'*81):
            f, h = self._fixture({2: (REGION,)}, satisfied=True)
            h.demo = ref
            self._run(f, h, rc=5, events=0)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_final_custody
    def test_final_custody(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        calls = {}
        def check(frame, ss):
            calls[frame.sequence] = calls.get(frame.sequence, 0) + 1
            return NO if frame.sequence == 2 and calls[2] >= 3 else YES
        h.check_hook = check
        self._run(f, h, rc=5, events=0)
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_recorder_provenance
    def test_recorder_provenance(self):
        # Registered recorder owner validates real RAM recording before issuing ID.
        record = learning.LearnTests('test_positive_genuine_gate_and_no_effects')
        record.setUp()
        self.addCleanup(record.doCleanups)
        registry = {}
        def export(evidence, ss, *, deadline_ns):
            pack = learning.w.build_evidence(record.inputs, policy=record.policy,
                    owners=record.owners, describer=record.describer, clock=lambda: 0)
            registry['ram-recording'] = (record.inputs, pack)
            return 'ram-recording'
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        f.owners = replace(f.owners, guidance=replace(h.owner, mini_demo=export))
        result = self._run(f, h, events=1)
        self.assertTrue(result.summary['evidence_refs'][0].endswith(':ram-recording'))
        self.assertTrue(registry['ram-recording'][1].steps)
        self.assertIsInstance(registry['ram-recording'][0], learning.w.LearnInputs)
        for corrupt in ('missing_recording', 'manifest'):
            f, h = self._fixture({2: (REGION,)}, satisfied=True)
            f.owners = replace(f.owners, guidance=replace(h.owner, mini_demo=export))
            if corrupt == 'missing_recording':
                events = record.demo / 'events.jsonl'
                saved = events.read_bytes()
                events.unlink()
                self._run(f, h, rc=5, events=0)
                events.write_bytes(saved)
            else:
                path = record.root / 'manifest.json'
                obj = gate.strict_load(path.read_bytes())
                obj['steps_sha256'] = '0'*64
                path.write_text(gate.canon(obj) + '\n')
                self._run(f, h, rc=5, events=0)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_authority_change
    def test_authority_change(self):
        f, h = self._fixture()
        def check(frame, ss):
            if frame.sequence >= 3:
                f.authorization.denied = 'send'
            return YES
        h.check_hook = check
        self._run(f, h, rc=5, events=0)
        f, h = self._fixture()
        self._run(f, h, sends=1, events=0)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_grounding_invalidation
    def test_grounding_invalidation(self):
        f, h = self._fixture({3: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)
        self.assertIsNone(h.refreshed[0][0].target)
        self.assertEqual(f.capabilities.targets, 1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_stability_reset
    def test_stability_reset(self):
        # First two post-input polls establish quiet; third is a guidance change.
        f, h = self._fixture({6: (REGION,)})
        f.workflow['steps'][0]['verify']['consecutive_polls'] = 4
        f.observations.values['post'] = [True, True, True, True, True]
        self._run(f, h, sends=1, events=1)
        self.assertGreaterEqual(f.observations.calls.count('post'), 7)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_snapshot_mutation
    def test_snapshot_mutation(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        def overwrite(frame, ss):
            ss.run.snapshots['fake'] = object()
            return YES
        h.hook = overwrite
        self._run(f, h, rc=5, events=0)
        self.assertEqual(h.refreshed[0][0].run.snapshots, {})

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_snapshot_negative
    def test_snapshot_negative(self):
        f, h = self._fixture({3: (REGION,)})
        step = f.workflow['steps'][0]
        step['capture_before'] = [dict(name='downloads', observe=base._predicate('downloads'))]
        step['verify']['predicate'] = base._predicate('downloads', 'set_grew', 'downloads')
        f.observations.values['downloads'] = [frozenset({'old'})]
        self._run(f, h, rc=5, sends=1, events=1)
        f, h = self._fixture({3: (REGION,)})
        self._run(f, h, sends=1, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_unknown_satisfaction
    def test_unknown_satisfaction(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        f.capabilities.no_satisfaction = True
        self._run(f, h, rc=5, events=1)
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_uncertain_transport
    def test_uncertain_transport(self):
        f, h = self._fixture({4: (REGION,)})
        f.transport.explode = True
        result = self._run(f, h, rc=5, sends=1, events=0)
        self.assertEqual(result.verdict_code, 'INPUT_UNCERTAIN')
        self.assertFalse(f.dispatch.check_ready())
        self.assertEqual(len(h.captured), 3)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_journal_extra_key
    def test_journal_extra_key(self):
        f, h = self._fixture()
        row = gate.strict_load(self._journal_row(f))
        row.pop('digest')
        row['pixels'] = 'PRIVATE_CANARY'
        row['digest'] = f.journal.digest(gate.canon(row).encode(), None)
        f.journal.raw = (gate.canon(row) + '\n').encode()
        self.assertEqual(f._run().verdict_code, 'TORN')
        self.assertEqual(f.transport.sent, [])

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_journal_capacity
    def test_journal_capacity(self):
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        f.limits = replace(f.limits, event_count=4)
        f.owners = replace(f.owners, limits=f.limits)
        self._run(f, h, rc=5, events=0)
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_memory_budgets
    def test_memory_budgets(self):
        for limits in (replace(base.w.WorkflowLimits(65536,16,8,1000,8192,4096,1024,100,100000,
                      4096,8192,1000000,1000000,100000000,1000000000), peak_bytes=299),
                      replace(base.w.WorkflowLimits(65536,16,8,1000,8192,4096,1024,100,100000,
                      4096,8192,1000000,1000000,100000000,1000000000), retained_bytes=599)):
            f, h = self._fixture()
            f.limits = limits
            f.owners = replace(f.owners, limits=limits)
            self._run(f, h, rc=5, events=0)
            self.assertFalse(h.captured)
        f, h = self._fixture({2: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_multi_regions
    def test_multi_regions(self):
        f, h = self._fixture({2: (REGION, ChangedRegion(3, 3, 1, 1, 1.0))}, satisfied=True)
        result = self._run(f, h, events=2)
        self.assertEqual(len(result.summary['evidence_refs']), 2)
        self.assertEqual(len(h.refreshed), 1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_uncertain_verification
    def test_uncertain_verification(self):
        f, h = self._fixture()
        run = w.RunState(b'', '', f.workflow, 'scope', 'authority', {}, uncertain=True,
                         last_progress_ns=f.clock())
        ss = w.StepState(1, run, 100_000_000, 100_000_000, 100_000_000,
                         start_ns=f.clock(), dispatch_disposition='INPUT_UNCERTAIN')
        try:
            with self.assertRaises(Exception) as caught:
                w.verify_step(f.workflow['steps'][0], state=ss, owners=f.owners)
            self.assertEqual(getattr(caught.exception, 'code', None), 'INPUT_UNCERTAIN')
            self.assertEqual(f.transport.sent, [])
            self.assertTrue(run.uncertain)
            self.assertFalse(h.captured)
        finally:
            for _, frame, _ in h.captured:
                h.release(frame, deadline_ns=f.clock())

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_stuck
    def test_stuck(self):
        f, h = self._fixture()
        f.workflow['steps'][0]['verify']['deadline_ms'] = 900
        f.observations.values['post'] = [False]
        result = f._run()
        self.assertEqual(result.verdict_code, 'STUCK')
        self.assertEqual(len(f.transport.sent), 1)
        self.assertFalse(h.held)
        self.assertLess(f.clock(), 900_000_000)
        self.assertFalse(h.captured[0][0].run.uncertain)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_repeat_bound
    def test_repeat_bound(self):
        f, h = self._fixture()
        f.workflow['steps'][0]['repeat'] = dict(while_predicate=base._predicate('while'), max_iterations=1)
        f.observations.values['while'] = [True, True, False]
        self.assertEqual(f._run().verdict_code, 'STUCK')
        self.assertEqual(len(f.transport.sent), 1)
        self.assertFalse(h.held)
        f, h = self._fixture()
        f.workflow['steps'][0]['repeat'] = dict(while_predicate=base._predicate('while'), max_iterations=1)
        f.observations.values['while'] = [True, False]
        self._run(f, h, sends=1, events=0)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance._repeat_fixture
    def _repeat_fixture(self, values, *, guided=True, broken=False):
        """Repeat controls use unchanged fixture budgets; counts are [TARGET]."""
        f, h = self._fixture({6: (REGION,)} if guided else None)
        step = f.workflow['steps'][0]
        step['repeat'] = dict(while_predicate=base._predicate('while'), max_iterations=2)
        f.observations.values['while'] = list(values)
        f.observations.values['post'] = [True]
        if broken:
            step['preconditions'].append(base._predicate('guidance-pre'))
            f.observations.values['guidance-pre'] = [True]
        interceptions, dispatches = [], []

        def refresh(frame, ss):
            interceptions.append((frame.sequence, ss.iteration, len(f.transport.sent),
                ss.input_ns, ss.dispatch_disposition, ss.deadline_ns,
                ss.locate_deadline_ns, ss.verify_deadline_ns, ss.run.last_progress_ns))
            if broken:
                f.observations.values['guidance-pre'] = [False]
            return YES

        h.hook = refresh

        def check(frame, ss):
            if interceptions and len(f.transport.sent) == 1:
                self.assertEqual(ss.input_ns, interceptions[0][3])
            return YES

        h.check_hook = check
        send = f.transport.send

        def record_send(action, target):
            ss = h.captured[-1][0]
            dispatches.append((ss.deadline_ns, ss.locate_deadline_ns, ss.verify_deadline_ns))
            receipt = send(action, target)
            # Advance the synthetic clock through one unchanged polling interval.
            # Otherwise the first verify capture is coalesced with the send poll.
            f.clock.sleep(POLICY.poll_ns)
            return receipt

        self.enterContext(patch.object(f.transport, 'send', side_effect=record_send))
        return f, h, interceptions, dispatches

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance._assert_repeat_control
    def _assert_repeat_control(self, f, h, interceptions, dispatches, *, iteration, guided=True):
        ss = h.captured[-1][0]
        self.assertEqual(ss.iteration, iteration)
        self.assertIs(ss.run.uncertain, False)
        self.assertFalse(ss.run.guidance_frames)
        self.assertLessEqual(ss.run.retained, f.limits.retained_bytes)
        self.assertEqual(ss.run.guidance_events, int(guided))
        self.assertEqual(ss.run.guidance_reperceptions, int(guided))
        self.assertLessEqual(ss.run.guidance_events, POLICY.max_events)
        self.assertLessEqual(ss.run.guidance_reperceptions, POLICY.max_reperceptions)
        self.assertLessEqual(ss.run.guidance_bytes, POLICY.metadata_bytes)
        deadlines = (ss.deadline_ns, ss.locate_deadline_ns, ss.verify_deadline_ns)
        self.assertTrue(dispatches)
        self.assertTrue(all(value == deadlines for value in dispatches))
        self.assertEqual(len(interceptions), int(guided))
        self.assertEqual(f.observations.calls.count('while'), (4 if guided else 3) if iteration == 2 else 2)
        self.assertEqual(f.observations.calls.count('target'), 3 if guided and iteration == 2 else 2)
        if guided:
            seq, completed, sends, input_ns, disposition, *frozen = interceptions[0]
            self.assertEqual((seq, completed, sends), (6, 1, 1))
            self.assertIsNotNone(input_ns)
            self.assertEqual(disposition, 'SENT')
            self.assertEqual(deadlines, tuple(frozen[:3]))
            # The next capture follows the callback, before any new input.
            if iteration == 2:
                self.assertEqual(h.progress_samples[6], frozen[3])
            if iteration == 1:
                self.assertEqual(ss.run.last_progress_ns, frozen[3])
                self.assertEqual((ss.iteration, len(f.transport.sent)), (completed, sends))
                self.assertEqual(ss.input_ns, input_ns)
        return ss

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_guidance_repeat_second_iteration
    def test_guidance_repeat_second_iteration(self):
        f, h, interceptions, dispatches = self._repeat_fixture([True, True, True, False])
        result = self._run(f, h, rc=0, sends=2, events=1)
        self.assertEqual(result.verdict_code, 'VERIFIED')
        ss = self._assert_repeat_control(f, h, interceptions, dispatches, iteration=2)
        self.assertEqual(ss.classification, 'verified')
        self.assertEqual(result.summary['steps_verified'], 1)
        self.assertEqual(result.summary['steps_skipped_satisfied'], 0)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_guidance_repeat_stuck_true
    def test_guidance_repeat_stuck_true(self):
        f, h, interceptions, dispatches = self._repeat_fixture([True])
        result = self._run(f, h, rc=5, sends=2, events=1)
        self.assertEqual(result.verdict_code, 'STUCK')
        self._assert_repeat_control(f, h, interceptions, dispatches, iteration=2)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_guidance_repeat_broken_precondition
    def test_guidance_repeat_broken_precondition(self):
        f, h, interceptions, dispatches = self._repeat_fixture([True], broken=True)
        result = self._run(f, h, rc=5, sends=1, events=1)
        self.assertEqual(result.verdict_code, 'LOW_CONFIDENCE')
        self._assert_repeat_control(f, h, interceptions, dispatches, iteration=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_repeat_second_iteration_without_guidance
    def test_repeat_second_iteration_without_guidance(self):
        f, h, interceptions, dispatches = self._repeat_fixture([True, True, False], guided=False)
        result = self._run(f, h, rc=0, sends=2, events=0)
        self.assertEqual(result.verdict_code, 'VERIFIED')
        ss = self._assert_repeat_control(f, h, interceptions, dispatches, iteration=2, guided=False)
        self.assertEqual(ss.classification, 'verified')
        self.assertEqual(result.summary['steps_verified'], 1)
        self.assertEqual(result.summary['steps_skipped_satisfied'], 0)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_paste_sentinel
    def test_paste_sentinel(self):
        f, h = self._fixture({3: (REGION,)}, satisfied=True)
        f._paste()
        f.slots.transport = 'owned_clipboard'
        result = self._run(f, h, events=1)
        self.assertEqual(result.summary['steps_skipped_satisfied'], 1)
        self.assertEqual(f.slots.cleared_count, 1)
        self.assertTrue(f.dispatch.check_ready())
        self.assertIsNone(h.refreshed[0][0].input_ns)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_unknown_truth
    def test_unknown_truth(self):
        f, h = self._fixture({3: (REGION,)}, satisfied=True)
        def refresh(frame, ss):
            now = f.clock()
            f.observations.values['satisfied'] = [w.ObservationValue('scope', 'authority', 'unknown', now, now,
                'satisfied', 'v1', 'boolean', 'count', True, False, w.Reason.MISSING)]
            return YES
        h.hook = refresh
        result = self._run(f, h, rc=5, events=1)
        self.assertEqual(result.verdict_code, 'LOW_CONFIDENCE')
        f, h = self._fixture({3: (REGION,)}, satisfied=True)
        self._run(f, h, events=1)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_frame_metadata
    def test_frame_metadata(self):
        f, h = self._fixture()
        run = w.RunState(b'', '', {}, 'scope', 'authority', {})
        ss = w.StepState(1, run, 100_000_000, 100_000_000, 100_000_000, start_ns=f.clock())
        frame = h.capture(ss, deadline_ns=100_000_000)
        self.assertIs(m.validate_frame(frame, ss, h.owner).result, w.Truth.TRUE)
        for changes in ({'sequence': True}, {'capture_end_ns': 0}, {'width': True},
                        {'authority': 'foreign'}, {'allocation': None}, {'custody': NO},
                        {'window': ''}, {'frame': None}):
            with self.subTest(changes=changes):
                self.assertIs(m.validate_frame(replace(frame, **changes), ss, h.owner).result, w.Truth.FALSE)
        h.release(frame, deadline_ns=f.clock())
        self.assertFalse(h.held)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_earlier_input_quiet
    def test_earlier_input_quiet(self):
        f, h = self._fixture()
        run = w.RunState(b'', '', {}, 'scope', 'authority', {}, last_input_ns=1)
        ss = w.StepState(1, run, 100_000_000, 100_000_000, 100_000_000, start_ns=0)
        before = h.capture(ss, deadline_ns=100_000_000)
        after = h.capture(ss, deadline_ns=100_000_000)
        def classify(start, boundary):
            return m.classify_guidance(before, after, step_state=ss, policy=POLICY, regions=(REGION,),
                expected_regions=None, quiet_start_ns=start, quiet_boundary_ns=boundary)
        self.assertEqual(classify(None, None).disposition, 'ambiguous')
        self.assertEqual(classify(2, 3).disposition, 'ambiguous')
        self.assertEqual(classify(2, 1_000_002).disposition, 'guidance')
        for _, frame, _ in h.captured:
            h.release(frame, deadline_ns=f.clock())
        self.assertFalse(h.held)

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_frame_pair_replay
    def test_frame_pair_replay(self):
        f, h = self._fixture()
        first = self._journal_row(f)
        second = self._journal_row(f, kind='failed', seq=2, prev=gate.strict_load(first)['digest'])
        payload = dict(self._payload(), box_px=[3, 3, 4, 4])
        f.journal.raw = first + second + self._journal_row(f, seq=3, prev=gate.strict_load(second)['digest'], payload=payload)
        self.assertEqual(f._run().verdict_code, 'TORN')
        self.assertEqual(f.transport.sent, [])
        self.assertFalse(h.held)
        f, h = self._fixture({2: (REGION, ChangedRegion(3, 3, 1, 1, 1.0))}, satisfied=True)
        self._run(f, h, events=2)
        self.assertEqual(f._run().verdict_code, 'OUT_OF_SUPPORT')
        self.assertEqual(f.transport.sent, [])

    # @anchor:wa.tests.mw_guidance_unit.TestGuidance.test_current_guard
    def test_current_guard(self):
        f, h = self._fixture({3: (REGION,)})
        f.authorization.segments = {}
        f.workflow['steps'][0].update(when=base._predicate('guard'), protected=False)
        f.observations.values['guard'] = [True]
        def refresh(frame, ss):
            f.observations.values['guard'] = [False]
            return YES
        h.hook = refresh
        result = self._run(f, h, events=1)
        self.assertEqual(result.summary['steps_skipped_condition'], 1)
        f, h = self._fixture({3: (REGION,)})
        self._run(f, h, sends=1, events=1)
