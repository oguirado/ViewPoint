# @module {"id":"wa.tests.mw_live_evidence_unit","layer":4,"owner":"LEARN","deps":["wa.learn.mw_live_evidence"],"book":"HB-WA-s4-learn"}
"""Synthetic RAM controls through public owner and actual workflow composition.
All fixture byte/time ceilings are [TARGET], not deployment settings.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import itertools
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from tests import test_mw_owners_srf_unit as composition
from tests import test_mw_run_unit as base
from wender.learn import mw_live_evidence as m
from wender.learn import mw_locate_ai as ai
from wender.learn import mw_workflow as w
from wender.learn.model_client import MuseOutput
from wender.perceive.vision_bridge import DescribeResult, Mark

YES, NO = base.YES, base.NO


def _png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 10, 10, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress((b'\0' + b'\0' * 30) * 10)) + chunk(b'IEND', b'')


class _Describer:
    def __init__(self):
        self.change = lambda d: d
        self.clean = True
        self.calls = 0

    # @anchor:wa.learn.mw_live_evidence.fixture.Describer.describe
    def describe(self, frame, *, marks_out, ocr_marks, deadline_ns):
        self.calls += 1
        assert ocr_marks is True
        marks_out.write_bytes(_png())
        return self.change(DescribeResult('fixture', hashlib.sha256(b'\0' * 300).hexdigest(),
            10, 10, None, (), (), 0, (Mark(1, 2, 2, 2, 2, 'button', 1.0, 'Go', 900, None, None),), str(marks_out)))

    # @anchor:wa.learn.mw_live_evidence.fixture.Describer.drain
    def drain(self):
        return YES if self.clean else NO


class _Model:
    def __init__(self):
        self.calls = 0
        self.answer = 1
        self.clean = True
        self.callback = lambda e: None
        self.paths = []

    # @anchor:wa.learn.mw_live_evidence.fixture.Model.generate
    def generate(self, payload, evidence, policy, deadline, clock):
        self.calls += 1
        self.payload = payload
        self.paths = [evidence.approved_frame_path, evidence.approved_marks_path]
        assert all(p.exists() for p in self.paths)
        self.callback(evidence)
        return MuseOutput({'mark_id': self.answer}, 0)

    # @anchor:wa.learn.mw_live_evidence.fixture.Model.cancel_and_drain
    def cancel_and_drain(self, policy, clock):
        return YES if self.clean else NO


# @anchor:wa.learn.mw_live_evidence.fixture.ScopeRunner
class _ScopeRunner:
    # @anchor:wa.learn.mw_live_evidence.fixture.ScopeRunner.__init__
    def __init__(self, reason=None):
        self.reason, self.units, self.drained = reason, [], []
        self.clean = True
        self.before_prepare = lambda: None
        self.deadlines = []

    # @anchor:wa.learn.mw_live_evidence.fixture.ScopeRunner.prepare
    def prepare(self, *, policy, locator_policy, clock, deadline_ns):
        self.before_prepare()
        self.policy = policy
        self.deadlines.append(deadline_ns)
        return self.reason

    # @anchor:wa.learn.mw_live_evidence.fixture.ScopeRunner.argv
    def argv(self, command, *, unit):
        self.units.append(unit)
        runner = m._OCRScopeRunner()
        runner.runner, runner.policy = '/resolved/systemd-run', self.policy
        return runner.argv(command, unit=unit)

    # @anchor:wa.learn.mw_live_evidence.fixture.ScopeRunner.drain
    def drain(self, *, unit, child, locator_policy, clock, deadline_ns):
        self.drained.append(unit)
        if child is not None:
            try:
                child.wait(timeout=max(0, deadline_ns - clock()) / 1_000_000_000)
            except Exception:
                return NO
        return YES if self.clean else NO


# @anchor:wa.learn.mw_live_evidence.fixture.ScopeChild
class _ScopeChild:
    # @anchor:wa.learn.mw_live_evidence.fixture.ScopeChild.__init__
    def __init__(self, stdout=b'json', stderr=b'', rc=0):
        self.pid, self.returncode = 123456789, rc
        self.hang = False
        pipes = []
        for data in (stdout, stderr):
            read_fd, write_fd = os.pipe()
            os.write(write_fd, data)
            os.close(write_fd)
            pipes.append(os.fdopen(read_fd, 'rb', buffering=0))
        self.stdout, self.stderr = pipes

    # @anchor:wa.learn.mw_live_evidence.fixture.ScopeChild.wait
    def wait(self, timeout):
        if self.hang:
            raise m.subprocess.TimeoutExpired('PRIVATE_CANARY', timeout)
        return self.returncode

    # @anchor:wa.learn.mw_live_evidence.fixture.ScopeChild.poll
    def poll(self):
        return self.returncode


# @anchor:wa.learn.mw_live_evidence.TestLiveEvidence
class TestLiveEvidence(unittest.TestCase):
    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.setUp
    def setUp(self):
        self.f = composition.TestComposition('test_factory_arguments')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.b = self.f.b
        Path('/dev/shm/vqa-cu').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir='/dev/shm/vqa-cu')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.png'
        self.source.write_bytes(_png())
        self.source.chmod(0o600)
        self.p = ai.LocatePolicy(2, 8, 2048, 8192, 1024, 1024, 4096, 65536, 16384,
                                 100000000, 10000, 1000000, 1000000, 1000000000)
        decode = m.DemoLimits(1, 1, 1, 4096, 10, 100, 4096, 32768, 1, 1, 1, 100000000)
        self.c = m.LiveEvidencePolicy(decode, 8192, 1024, 1048576, 4096)
        self.trust = m.LiveEvidenceTrust('rev', ('en',), None, lambda **k: YES,
                                        lambda **k: YES, lambda **k: YES, ('PRIVATE_CANARY',))
        self.ports = replace(self.f.ports, limits=replace(self.f.ports.limits, retained_bytes=131072, peak_bytes=2097152))
        self.target = dict(rung='T2', mark_kind='button', ocr_label='Go', locale='en',
                           rel_box_bps=[0, 0, 5000, 5000], capability_id='synthetic-v1')
        self.b._click()
        self.b.workflow['steps'][0]['action'].update(path='coordinates', button=1)
        self.b.workflow['steps'][0]['target'] = self.target
        self.describer, self.model = _Describer(), _Model()
        self.slot = {}
        self.owner = self._owner()
        self.accounting = ai.LocateAccounting(None)
        self.caps = m._Capabilities(self.ports.capabilities, self.owner, self.accounting)
        self.owner.capability_owner = self.caps
        self.state = self.b._verification_state()
        self.state.seq = 1
        self.state.start_ns = self.b.clock()
        self.state.input_ns = None
        self.state.run.workflow = self.b.workflow
        self.state.run.current_step = self.state
        self.ports.capabilities.target = lambda *a: None
        self.caps.target(self.target, self.state, self.state.locate_deadline_ns)
        self.frames = 0
        self.surface = type('Surface', (), {'window': 'w1', 'frame': lambda _: self._frame()})()
        self.slot['surface'] = self.surface
        self.addCleanup(self._release)

    def _owner(self):
        return m.LiveEvidenceOwner(ports=self.ports, locator_policy=self.p, evidence_policy=self.c,
            trust=self.trust, workspace=self.root, surface_slot=self.slot, describer=self.describer)

    def _frame(self):
        self.frames += 1
        return dict(path=str(self.source), seq=self.frames, width=10, height=10,
                    window=dict(id='w1', w=10, h=10))

    def _release(self):
        self.describer.clean = True
        for a in self.owner.allocations.values():
            self.owner.release(a.evidence, self.p.absolute_deadline_ns)
        self.owner._cleanup_partial()

    def _acquire(self):
        return self.owner.acquire(self.target, self.state, self.p, self.state.locate_deadline_ns)

    def _ground(self, evidence):
        return self.owner.ground(self.target, evidence, evidence.description.marks[0], (3, 3),
                                 self.state, self.state.locate_deadline_ns)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_current_capture_grounding_and_lifetime
    def test_current_capture_grounding_and_lifetime(self):
        e = self._acquire()
        g = self._ground(e)
        self.assertEqual(self.owner.validate_target(self.target, g, e, self.state), YES)
        self.assertEqual(g.point, (3, 3))
        self.assertIs(g.observation, e.observation)
        self.assertEqual(e.observation.capture_start_ns, self.state.start_ns)
        self.assertTrue(e.approved_frame_path.exists())
        self.assertEqual(self.owner.release(e, self.state.deadline_ns), YES)
        self.assertEqual(self.owner.release(e, self.state.deadline_ns), YES)
        self.assertFalse(e.approved_frame_path.exists())
        self.assertNotEqual(self.owner.revalidate(e, self.state, self.state.deadline_ns), YES)
        self.assertNotEqual(self.owner.release(replace(e), self.state.deadline_ns), YES)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_configuration_before_activity
    def test_configuration_before_activity(self):
        cases = [(replace(self.c, ve_stdout_bytes=True), self.trust, self.p),
                 (self.c, replace(self.trust, support_check=None), self.p),
                 (self.c, self.trust, replace(self.p, retained_bytes=1))]
        for c, trust, p in cases:
            with self.subTest(config=c), self.assertRaises(Exception):
                m.LiveEvidenceOwner(ports=self.ports, locator_policy=p, evidence_policy=c, trust=trust,
                    workspace=self.root, surface_slot={}, describer=self.describer)
        self.assertEqual(self.frames, 0)
        self.assertEqual(self.describer.calls, 0)
        ports = replace(self.ports, limits=replace(self.ports.limits, retained_bytes=self.p.retained_bytes))
        with self.assertRaisesRegex(ValueError, 'budgets'):
            m.LiveEvidenceOwner(ports=ports, locator_policy=self.p, evidence_policy=self.c, trust=self.trust,
                workspace=self.root, surface_slot={}, describer=self.describer)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_predispatch_and_primary
    def test_predispatch_and_primary(self):
        self.assertEqual(self.owner.classify(self.target, self.state), 'miss')
        for field, value in [('uncertain', True), ('last_input_ns', self.b.clock())]:
            old = getattr(self.state.run, field)
            setattr(self.state.run, field, value)
            if field == 'uncertain':
                self.assertEqual(self.owner.classify(self.target, self.state), 'terminal')
            else:
                with self.assertRaises(ValueError): self._acquire()
            setattr(self.state.run, field, old)
        for field, value in [('input_ns', 1), ('dispatch_disposition', 'SENT')]:
            old = getattr(self.state, field)
            setattr(self.state, field, value)
            self.assertEqual(self.owner.classify(self.target, self.state), 'terminal')
            setattr(self.state, field, old)
        for target in [dict(self.target, locale='xx'), dict(self.target, rung='T3')]:
            self.assertEqual(self.owner.classify(target, self.state), 'terminal')
        self.ports.capabilities.target = lambda *a: (_ for _ in ()).throw(RuntimeError('PRIVATE_CANARY'))
        with self.assertRaises(RuntimeError): self.caps.target(self.target, self.state, self.state.deadline_ns)
        self.assertEqual(self.owner.classify(self.target, self.state), 'terminal')
        with self.assertRaises(ValueError): self.caps.target(self.target, replace(self.state, run=object()), 100)
        self.assertEqual(self.describer.calls, 0)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_reservation_precedes_capture
    def test_reservation_precedes_capture(self):
        self.b.resources.denied = 'live_evidence'
        with self.assertRaisesRegex(ValueError, 'reservation'): self._acquire()
        self.assertEqual((self.frames, self.describer.calls, self.model.calls), (0, 0, 0))
        self.b.resources.denied = None
        self.assertIs(type(self._acquire()), ai.LocateEvidence)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_inventory_negative_matrix
    def test_inventory_negative_matrix(self):
        mutations = [lambda d: {}, lambda d: replace(d, marks=()), lambda d: replace(d, mark_cap_reached=True),
            lambda d: replace(d, marks=d.marks * 2),
            lambda d: replace(d, marks=(replace(d.marks[0], id=True),)),
            lambda d: replace(d, marks=(replace(d.marks[0], x=-1),)),
            lambda d: replace(d, marks=(replace(d.marks[0], w=100),)),
            lambda d: replace(d, marks=(replace(d.marks[0], ocr_score_promille=None),)),
            lambda d: replace(d, marks=(replace(d.marks[0], missing_reason='ocr_unavailable'),)),
            lambda d: replace(d, marks=(replace(d.marks[0], ocr_text='PRIVATE_CANARY'),)),
            lambda d: replace(d, marks=(replace(d.marks[0], ocr_text='é' * 2000),)),
            lambda d: replace(d, frame_sha256=hashlib.sha256(_png()).hexdigest()),
            lambda d: replace(d, width=11)]
        for change in mutations:
            self.describer.change = change
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'acquisition_refused'):
                self._acquire()
            self.assertIsNone(self.owner.partial)
            self.assertEqual(list(self.root.glob('live-*')), [])
        self.describer.change = lambda d: d
        self._acquire()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_containment_and_support
    def test_containment_and_support(self):
        for change in [lambda d: replace(d, marks=(replace(d.marks[0], x=7),)),
                       lambda d: replace(d, marks=d.marks + (replace(d.marks[0], id=2),)),
                       lambda d: replace(d, marks=(replace(d.marks[0], ocr_text='Other'),))]:
            self.describer.change = change
            e = self._acquire()
            with self.assertRaisesRegex(ValueError, 'containment'): self._ground(e)
            self.owner.release(e, self.state.deadline_ns)
            self.owner.allocations.clear()
        self.describer.change = lambda d: d
        e = self._acquire()
        for admission in (NO, m._NO):
            self.owner.trust = replace(self.trust, support_check=lambda **k: admission)
            with self.assertRaisesRegex(ValueError, 'support'): self._ground(e)
        self.owner.trust = self.trust
        g = self._ground(e)
        for forged in [replace(g), replace(g, point=(0, 0)), replace(g, support_owner=object()),
                       replace(g, observation=replace(g.observation))]:
            self.assertNotEqual(self.owner.validate_target(self.target, forged, e, self.state), YES)
        self.assertEqual(self.owner.validate_target(self.target, g, e, self.state), YES)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_revalidation_and_export
    def test_revalidation_and_export(self):
        self.owner.trust = replace(self.trust, export_check=lambda **k: NO)
        with self.assertRaises(ValueError): self._acquire()
        self.owner.trust = self.trust
        e = self._acquire()
        self.assertEqual(self.owner.revalidate(e, self.state, self.state.deadline_ns), YES)
        self.owner.trust = replace(self.trust, custody_check=lambda **k: NO)
        self.assertNotEqual(self.owner.revalidate(e, self.state, self.state.deadline_ns), YES)
        self.owner.trust = self.trust
        e.approved_frame_path.write_bytes(b'changed')
        self.assertNotEqual(self.owner.revalidate(e, self.state, self.state.deadline_ns), YES)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_partial_custody
    def test_partial_custody(self):
        self.describer.change = lambda d: {}
        self.describer.clean = False
        with self.assertRaises(ValueError): self._acquire()
        self.assertIsNotNone(self.owner.partial)
        self.assertNotEqual(self.owner.validate_policy(self.p, self.state), YES)
        self.assertTrue(list(self.root.glob('live-*')))
        self.describer.clean = True
        self.assertEqual(self.owner._cleanup_partial(), YES)
        self.owner.partial = None
        self.assertFalse(list(self.root.glob('live-*')))

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_workspace_and_source
    def test_workspace_and_source(self):
        self.root.chmod(0o755)
        self.assertNotEqual(self.owner.validate_policy(self.p, self.state), YES)
        self.root.chmod(0o700)
        linked = self.root / 'linked.png'
        linked.symlink_to(self.source)
        original = self._frame
        self._frame = lambda: dict(original(), path=str(linked))
        with self.assertRaises(ValueError): self._acquire()
        self._frame = original
        self._acquire()

    def _run_live(self):
        self.b._write()
        def factory(**kwargs):
            self.f.factory_calls.append(kwargs)
            surface = composition._Surface(**kwargs)
            surface.frame = self._frame
            return surface
        return m.run_live_workflow(self.f.invocation, bundle=base.BUNDLE, ports=self.ports,
            locator_policy=self.p, evidence_policy=self.c, trust=self.trust, workspace=self.root,
            executable='unused', surface_factory=factory, describer=self.describer, locate_transport=self.model)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_real_composition_relocation
    def test_real_composition_relocation(self):
        result = self._run_live()
        self.assertEqual(result.result.rc, 0, result)
        self.assertEqual(result.result.summary['model_calls'], 1)
        self.assertEqual(result.accounting.model_calls, result.result.summary['model_calls'])
        self.assertIsNone(result.result.summary['tokens_used'])
        self.assertIsNone(result.accounting.tokens_used)
        self.assertEqual([op for op, _ in self.f.native.calls if op != 'frame'], ['click'])
        self.assertEqual(len(self.f.factory_calls), 1)
        self.assertIsNone(self.ports.actor_lock.holder)
        self.assertTrue(all(not p.exists() for p in self.model.paths))

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_real_composition_refusal
    def test_real_composition_refusal(self):
        self.model.answer = None
        result = self._run_live()
        self.assertNotEqual(result.result.rc, 0)
        self.assertEqual(result.result.summary['model_calls'], 1)
        self.assertEqual(result.accounting.model_calls, result.result.summary['model_calls'])
        self.assertIsNone(result.result.summary['tokens_used'])
        self.assertIsNone(result.accounting.tokens_used)
        self.assertFalse(self.f.native.calls)
        self.assertIsNone(self.ports.actor_lock.holder)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_announcement_before_surface
    def test_announcement_before_surface(self):
        for change in ['blank', 'false', 'unknown']:
            old_inv, old_ports = self.f.invocation, self.ports
            if change == 'blank': self.f.invocation = replace(old_inv, live_ack='')
            else: self.ports = replace(old_ports, announce_gate=lambda *a, **k: NO if change == 'unknown' else m._NO)
            with self.assertRaises(ValueError): self._run_live()
            self.assertFalse(self.f.factory_calls)
            self.f.invocation, self.ports = old_inv, old_ports

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_three_guard_removal_mutants
    def test_three_guard_removal_mutants(self):
        original = m._need
        for gate in ('reservation', 'geometry', 'rgb_binding'):
            def assert_rejection():
                if gate == 'reservation': self.b.resources.denied = 'live_evidence'
                if gate == 'geometry': self.describer.change = lambda d: replace(d, marks=(replace(d.marks[0], x=-1),))
                if gate == 'rgb_binding': self.describer.change = lambda d: replace(d, frame_sha256='wrong')
                with self.assertRaises(ValueError): self._acquire()
            assert_rejection()
            def bypass(ok, label):
                if label != gate: original(ok, label)
            with patch.object(m, '_need', bypass), self.assertRaises(AssertionError):
                assert_rejection()
            self._release()
            self.owner.allocations.clear()
            self.owner.partial = None
            self.b.resources.denied = None
            self.describer.change = lambda d: d

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_ordinary_identity_fail_closed
    def test_ordinary_identity_fail_closed(self):
        self.ports.capabilities.target = lambda target, state, deadline: w.GroundedTarget(
            (3, 3), None, self.b.observations.acquire(base._predicate('ordinary'), deadline),
            self.ports.scope, self.ports.authority, YES, self.ports.capabilities, deadline, (0, 0))
        value = self.caps.target(self.target, self.state, self.state.deadline_ns)
        self.assertEqual(self.caps.validate_target(self.target, value, self.state), YES)
        self.assertNotEqual(self.caps.validate_target(self.target, replace(value), self.state), YES)
        self.assertNotEqual(self.caps.validate_target(dict(self.target, ocr_label='Other'), value, self.state), YES)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_current_capture_negatives
    def test_current_capture_negatives(self):
        original = self._frame
        for mutate in [lambda f: dict(f, window=dict(id='foreign', w=10, h=10)),
                       lambda f: dict(f, width=True), lambda f: dict(f, seq=True)]:
            self._frame = lambda: mutate(original())
            with self.assertRaisesRegex(ValueError, 'acquisition_refused'): self._acquire()
            self.assertEqual(self.describer.calls, 0)
        self._frame = original
        e = self._acquire()
        self.owner.release(e, self.state.deadline_ns)
        self._frame = lambda: dict(original(), seq=self.owner.sequence)
        with self.assertRaises(ValueError): self._acquire()
        self.assertEqual(self.describer.calls, 1)
        self._frame = original
        self.state.start_ns = self.b.clock() + 1
        with self.assertRaises(ValueError): self._acquire()
        self.assertEqual(self.describer.calls, 1)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_model_selection_revalidation_and_support
    def test_model_selection_revalidation_and_support(self):
        for answer in (None, 99):
            self.model.answer = answer
            result = self._run_live()
            self.assertEqual(result.result.summary['model_calls'], 1)
            self.assertEqual(result.accounting.model_calls, result.result.summary['model_calls'])
            self.assertNotEqual(result.result.rc, 0)
            self.assertFalse(self.f.native.calls)
            self.assertTrue(all(not p.exists() for p in self.model.paths))
        self.model.answer = 1
        self.model.callback = lambda e: e.approved_frame_path.write_bytes(b'changed')
        result = self._run_live()
        self.assertNotEqual(result.result.rc, 0)
        self.assertFalse(self.f.native.calls)
        self.model.callback = lambda e: None
        self.trust = replace(self.trust, support_check=lambda **k: NO)
        result = self._run_live()
        self.assertNotEqual(result.result.rc, 0)
        self.assertEqual(result.result.summary['model_calls'], 1)
        self.assertEqual(result.accounting.model_calls, result.result.summary['model_calls'])
        self.assertFalse(self.f.native.calls)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_postcondition_is_independent
    def test_postcondition_is_independent(self):
        predicate = self.b.workflow['steps'][0]['verify']['predicate']['capability_id']
        self.b.observations.values[predicate] = [False]
        result = self._run_live()
        self.assertNotEqual(result.result.rc, 0)
        self.assertEqual(result.result.summary['model_calls'], 1)
        self.assertEqual(result.accounting.model_calls, result.result.summary['model_calls'])
        self.assertEqual([op for op, _ in self.f.native.calls], ['click'])

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_cleanup_failure_retains_actor
    def test_cleanup_failure_retains_actor(self):
        self.model.clean = False
        with self.assertRaises(ValueError): self._run_live()
        self.assertIsNotNone(self.ports.actor_lock.holder)
        self.assertTrue(all(p.exists() for p in self.model.paths))
        key = (self.f.invocation.container, self.f.invocation.window)
        locator = composition.m._LOCATOR_CUSTODY[key]
        self.model.clean = True
        self.assertTrue(locator._drain())
        self.assertTrue(all(not p.exists() for p in self.model.paths))
        composition.m._LOCATOR_CUSTODY.pop(key)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_actor_exclusion_and_reopen
    def test_actor_exclusion_and_reopen(self):
        def concurrent(e):
            before = len(self.f.factory_calls)
            with self.assertRaises(ValueError): self._run_live()
            self.assertEqual(len(self.f.factory_calls), before)
        self.model.callback = concurrent
        self.assertEqual(self._run_live().result.rc, 0)
        self.model.callback = lambda e: None
        self.ports = replace(self.ports, journal=base._Journal(self.b.root))
        self.assertEqual(self._run_live().result.rc, 0)
        self.assertEqual(len(self.f.factory_calls), 2)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_supported_action_restrictions
    def test_supported_action_restrictions(self):
        for updates in (dict(kind='double_click'), dict(path='dom')):
            old = self.b.workflow['steps'][0]['action'].copy()
            self.b.workflow['steps'][0]['action'].update(updates)
            result = self._run_live()
            self.assertNotEqual(result.result.rc, 0)
            self.assertEqual(self.model.calls, 0)
            self.assertFalse(self.f.native.calls)
            self.b.workflow['steps'][0]['action'] = old

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_unknown_admissions_and_privacy
    def test_unknown_admissions_and_privacy(self):
        for callback in ('export_check', 'custody_check'):
            self.owner.trust = replace(self.trust, **{callback: lambda **k: NO})
            with self.assertRaises(ValueError): self._acquire()
            self.assertFalse(list(self.root.glob('live-*')))
        self.owner.trust = self.trust
        self.target['ocr_label'] = 'PRIVATE_CANARY'
        self.caps.target(self.target, self.state, self.state.deadline_ns)
        with self.assertRaises(ValueError): self._acquire()
        self.assertEqual(self.model.calls, 0)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_public_api_guard_mutants
    def test_public_api_guard_mutants(self):
        gates = ('configuration', 'trust', 'budgets', 'workspace_root', 'sequence', 'capture',
                 'freshness', 'inventory', 'ocr', 'privacy', 'metadata', 'admission', 'containment',
                 'support', 'registry', 'binding', 'image_identity', 'custody', 'export', 'grounded_identity')
        def negative(case, gate):
            if gate in ('configuration', 'trust', 'budgets', 'workspace_root'):
                if gate == 'configuration': case.c = replace(case.c, ve_stderr_bytes=True)
                if gate == 'trust': case.trust = replace(case.trust, support_check=None)
                if gate == 'budgets': case.c = replace(case.c, ve_process_bytes=case.ports.limits.peak_bytes)
                if gate == 'workspace_root':
                    # Actual RAM, private ownership: only the required root is wrong.
                    tmp = tempfile.TemporaryDirectory(dir='/dev/shm')
                    case.addCleanup(tmp.cleanup)
                    case.root = Path(tmp.name)
                with case.assertRaises(Exception): case._owner()
                case.assertEqual(case.frames, 0)
                return
            if gate == 'sequence': case.owner.sequence = 100
            if gate == 'capture':
                original = case._frame
                case._frame = lambda: dict(original(), window=dict(id='wrong', w=10, h=10))
            if gate == 'freshness': case.state.run.last_input_ns = case.b.clock()
            if gate == 'inventory': case.describer.change = lambda d: replace(d, mark_cap_reached=True)
            if gate == 'ocr': case.describer.change = lambda d: replace(d, marks=(replace(d.marks[0], ocr_score_promille=None),))
            if gate == 'privacy': case.describer.change = lambda d: replace(d, marks=(replace(d.marks[0], ocr_text='PRIVATE_CANARY'),))
            if gate == 'metadata': case.owner.config = replace(case.c, metadata_bytes=1)
            if gate == 'admission': case.owner.trust = replace(case.trust, export_check=lambda **k: NO)
            if gate in ('sequence', 'capture', 'freshness', 'inventory', 'ocr', 'privacy', 'metadata', 'admission'):
                with case.assertRaises(ValueError): case._acquire()
                case.assertEqual(case.model.calls, 0)
                return
            if gate == 'containment': case.describer.change = lambda d: replace(d, marks=(replace(d.marks[0], x=7),))
            e = case._acquire()
            if gate in ('containment', 'support'):
                if gate == 'support': case.owner.trust = replace(case.trust, support_check=lambda **k: NO)
                with case.assertRaises(ValueError): case._ground(e)
                return
            g = case._ground(e)
            if gate == 'registry':
                # Same exact evidence but an unregistered replacement registry record.
                e.allocation.released = True
            if gate == 'binding': case.state.run.scope = 'foreign'
            if gate == 'image_identity': e.approved_frame_path.write_bytes(_png() + b'changed')
            if gate == 'custody': case.owner.trust = replace(case.trust, custody_check=lambda **k: NO)
            if gate == 'export': case.owner.trust = replace(case.trust, export_check=lambda **k: NO)
            if gate == 'grounded_identity': e.allocation.grounded[id(g)] = (replace(g), e.description.marks[0])
            case.assertNotEqual(case.owner.validate_target(case.target, g, e, case.state), YES)
        for gate in gates:
            for mutant in (False, True):
                case = TestLiveEvidence('test_current_capture_grounding_and_lifetime')
                case.setUp()
                original = m._need
                def bypass(ok, label):
                    if label != gate: original(ok, label)
                try:
                    with self.subTest(gate=gate, mutant=mutant):
                        if mutant:
                            with patch.object(m, '_need', bypass), self.assertRaises(AssertionError, msg='surviving mutant:' + gate):
                                negative(case, gate)
                        else:
                            negative(case, gate)
                finally:
                    case.doCleanups()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_real_ocr_smoke
    @unittest.skipUnless(os.environ.get('VQA_VE_ROOT') and __import__('shutil').which('tesseract'),
                         'opt-in: needs VQA_VE_ROOT and tesseract')
    def test_real_ocr_smoke(self):
        # Real child process (judge W25 fix 2): a RAM frame with known text must yield an OCR mark.
        import shutil, time
        from PIL import Image, ImageDraw, ImageFont
        ws = Path(tempfile.mkdtemp(prefix='smoke-', dir='/dev/shm/vqa-cu'))
        try:
            frame = ws / 'frame.png'
            img = Image.new('RGB', (480, 120), 'white')
            draw = ImageDraw.Draw(img)
            draw.rectangle((20, 30, 220, 90), outline='black', width=2)
            draw.text((40, 44), 'Download', fill='black', font=ImageFont.load_default(size=32))
            img.save(frame)
            frame.chmod(0o600)
            marks = ws / 'marks.png'
            marks.write_bytes(b'')
            marks.chmod(0o600)
            policy = replace(self.p, drain_ns=10_000_000_000, image_bytes=8 * 1024 ** 2, peak_bytes=self.p.peak_bytes + 64 * 1024 ** 2)
            adapter = m.LiveOCRDescriber(evidence_policy=replace(self.c, ve_stdout_bytes=1048576, ve_stderr_bytes=65536,
                                                                 ve_process_bytes=4 * 1024 ** 3, ocr_control_ns=10_000_000_000),
                                         locator_policy=policy, clock=time.monotonic_ns, ve_root=os.environ['VQA_VE_ROOT'])
            result = adapter.describe(frame, marks_out=ws / 'marks.png', ocr_marks=True,
                                      deadline_ns=time.monotonic_ns() + 60 * 10 ** 9)
            # The real toolchain is reachable from the bounded child: marks come back and none reports a missing
            # OCR toolchain (on real Stitch frames 20/24 marks carry text [MEASURED, W25 conductor check]).
            self.assertTrue(result.marks)
            self.assertFalse({getattr(mk, 'missing_reason', None) for mk in result.marks}
                             & {'tessdata_absent', 'ocr_unavailable'})
            record = adapter.last_execution
            self.assertIn(record.mode, ('systemd_scope', 'direct'))
            if record.mode == 'systemd_scope':
                self.assertIsNone(record.fallback_reason)
                self.assertGreater(record.tasks_max, 0)
                self.assertGreater(record.memory_max_bytes, 0)
            else:
                self.assertIn(record.fallback_reason, m._FALLBACKS)
                self.assertIsNone(record.tasks_max)
                self.assertIsNone(record.memory_max_bytes)
            self.assertIsNone(adapter.child)
            self.assertIsNone(adapter.pending_scope)
            self.assertIsNone(adapter.scope_runner.pending)
            self.assertIsNone(adapter.scope_runner.helper)
            self.assertFalse(adapter.poisoned)
            summary = adapter.execution_summary()
            self.assertEqual((summary.scope_attempts, summary.direct_attempts),
                             (1, 0) if record.mode == 'systemd_scope' else (0, 1))
            self.assertEqual(sum(n for _, n in summary.fallback_counts), summary.direct_attempts)
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_bounded_ocr_adapter_without_processes
    def test_bounded_ocr_adapter_without_processes(self):
        import subprocess
        # @anchor:wa.learn.mw_live_evidence.fixture.Child
        class Child:
            def __init__(self, stdout, stderr, rc=0, joined=True):
                self.pid, self.returncode, self.joined = 123456789, rc, joined
                pipes = []
                for data in (stdout, stderr):
                    read_fd, write_fd = os.pipe()
                    os.write(write_fd, data)
                    os.close(write_fd)
                    pipes.append(os.fdopen(read_fd, 'rb', buffering=0))
                self.stdout, self.stderr = pipes
            # @anchor:wa.learn.mw_live_evidence.fixture.Child.wait
            def wait(self, timeout):
                if not self.joined: raise subprocess.TimeoutExpired('private', timeout)
                return self.returncode
        def missing_group(*args):
            raise ProcessLookupError
        for reason, (label, stdout, stderr, rc, joined) in itertools.product((None, 'runner_absent'), (
            ('positive', b'json', b'', 0, True),
            ('stdout_bound', b'x' * 32, b'', 0, True),
            ('stderr_bound', b'', b'PRIVATE_CANARY' * 2, 0, True),
            ('exit', b'json', b'PRIVATE_CANARY', 1, True),
            ('join', b'json', b'', 0, False),
            ('drain_refusal', b'json', b'', 0, True))):
            child = Child(stdout, stderr, rc, joined)
            marks_path = self.root / 'marks.png'
            marks_path.write_bytes(b'')
            marks_path.chmod(0o600)
            adapter = m.LiveOCRDescriber(evidence_policy=replace(self.c, ve_stdout_bytes=16, ve_stderr_bytes=16),
                locator_policy=self.p, clock=self.b.clock, ve_root=self.root, scope_runner=_ScopeRunner(reason))
            description = DescribeResult('fixture', 'digest', 10, 10, None, (), (), 0, (), str(self.root / 'marks.png'))
            real_drain = adapter.drain
            try:
                with patch.object(adapter, 'drain', side_effect=lambda: m._NO if label == 'drain_refusal' else real_drain()), \
                     self.subTest(label=label), patch.object(m.subprocess, 'Popen', return_value=child) as launch, \
                     patch.object(m.os, 'killpg', side_effect=missing_group), \
                     patch.object(m, '_parse_description', return_value=description) as parser:
                    if label == 'positive':
                        self.assertIs(adapter.describe(self.source, marks_out=self.root / 'marks.png',
                            ocr_marks=True, deadline_ns=self.state.deadline_ns), description)
                        parser.assert_called_once_with(b'json', ocr_requested=True)
                        args, kwargs = launch.call_args
                        self.assertIn('--ocr-marks', args[0])
                        self.assertIn('--json', args[0])
                        self.assertNotIn('PRIVATE_CANARY', str(args))
                        with patch.object(m.resource, 'setrlimit') as bounds:
                            kwargs['preexec_fn']()
                            self.assertEqual(bounds.call_count, 3)
                            # The OCR child must be able to spawn tesseract (judge W25 fix 1).
                            self.assertFalse(any(c.args[0] == m.resource.RLIMIT_NPROC for c in bounds.call_args_list))
                    else:
                        with self.assertRaises(ValueError) as error:
                            adapter.describe(self.source, marks_out=self.root / 'marks.png',
                                ocr_marks=True, deadline_ns=self.state.deadline_ns)
                        self.assertNotIn('PRIVATE_CANARY', str(error.exception))
                        if label in ('join', 'drain_refusal'): self.assertIs(adapter.child, child)
                    if joined and label != 'drain_refusal': self.assertIsNone(adapter.child)
            finally:
                child.stdout.close()
                child.stderr.close()

    @contextmanager
    def _scope_fixture(self, *, runner=None, stdout=b'json', stderr=b'', rc=0):
        runner = _ScopeRunner() if runner is None else runner
        child = _ScopeChild(stdout, stderr, rc)
        marks = self.root / 'marks.png'
        marks.write_bytes(b'')
        marks.chmod(0o600)
        adapter = m.LiveOCRDescriber(evidence_policy=replace(self.c, ve_stdout_bytes=16, ve_stderr_bytes=16),
            locator_policy=self.p, clock=self.b.clock, ve_root=self.root, scope_runner=runner)
        description = DescribeResult('fixture', 'digest', 10, 10, None, (), (), 0, (), str(marks))
        def missing(*args):
            raise ProcessLookupError
        def call(deadline=None):
            return adapter.describe(self.source, marks_out=marks, ocr_marks=True,
                deadline_ns=self.state.deadline_ns if deadline is None else deadline)
        try:
            with patch.object(m.subprocess, 'Popen', return_value=child) as launch, \
                 patch.object(m.os, 'killpg', side_effect=missing), \
                 patch.object(m, '_parse_description', return_value=description) as parser:
                yield adapter, runner, child, launch, parser, description, call
        finally:
            child.stdout.close()
            child.stderr.close()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_command_and_effective_limits
    def test_scope_command_and_effective_limits(self):
        with self._scope_fixture() as (adapter, runner, child, launch, parser, description, call):
            self.assertIs(call(), description)
            argv = launch.call_args.args[0]
            unit = runner.units[0]
            self.assertEqual(argv[:12], ['/resolved/systemd-run', '--user', '--scope', '--quiet', '--collect',
                '--no-ask-password', '--slice-inherit', '--unit=' + unit, '--description=MemoryWeb-OCR',
                '--property=TasksMax=32', '--property=MemoryMax=' + str(self.c.ve_process_bytes), '--'])
            self.assertEqual(argv[12:], [adapter.engine.python, '-m', 'vadapt.cu_describe', '--frame',
                str(self.source), '--marks-out', str(self.root / 'marks.png'), '--ocr-marks', '--json'])
            self.assertNotIn('--wait', argv)
            parser.assert_called_once_with(b'json', ocr_requested=True)
            self.assertEqual(adapter.last_execution, m.OCRExecutionRecord('systemd_scope', None, 32, self.c.ve_process_bytes))
            self.assertEqual(runner.drained, [unit])
            self.assertIsNone(adapter.pending_scope)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_fallback_recorded
    def test_fallback_recorded(self):
        for reason in m._FALLBACKS:
            with self.subTest(reason=reason), self._scope_fixture(runner=_ScopeRunner(reason)) as f:
                adapter, runner, child, launch, parser, description, call = f
                self.assertIs(call(), description)
                self.assertEqual(launch.call_args.args[0], [adapter.engine.python, '-m', 'vadapt.cu_describe',
                    '--frame', str(self.source), '--marks-out', str(self.root / 'marks.png'), '--ocr-marks', '--json'])
                self.assertEqual(adapter.last_execution, m.OCRExecutionRecord('direct', reason, None, None))
                self.assertEqual(adapter.execution_summary().direct_attempts, 1)
                self.assertEqual(dict(adapter.execution_summary().fallback_counts)[reason], 1)
        for args in [('other', None, None, None), ('direct', None, None, None),
                     ('direct', 'unknown', None, None), ('direct', 'runner_absent', 32, 100),
                     ('systemd_scope', None, True, 100), ('systemd_scope', None, 32, 0)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                m.OCRExecutionRecord(*args)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_failure_never_retries_direct
    def test_scope_failure_never_retries_direct(self):
        for failure in ('exit', 'launch'):
            with self.subTest(failure=failure), self._scope_fixture(rc=1) as f:
                adapter, runner, child, launch, parser, description, call = f
                if failure == 'launch':
                    launch.side_effect = OSError('PRIVATE_CANARY')
                with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_refused$'):
                    call()
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(adapter.execution_summary().scope_attempts, 1)
                self.assertEqual(adapter.execution_summary().direct_attempts, 0)
                self.assertEqual(runner.drained, runner.units)
                parser.assert_not_called()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_rlimits_and_environment
    def test_scope_rlimits_and_environment(self):
        for reason in (None, 'runner_absent'):
            with patch.dict(os.environ, {'PRIVATE_CANARY': 'secret'}), self._scope_fixture(runner=_ScopeRunner(reason)) as f:
                adapter, runner, child, launch, parser, description, call = f
                call()
                kw = launch.call_args.kwargs
                with patch.object(m.resource, 'setrlimit') as limits:
                    kw['preexec_fn']()
                self.assertEqual([c.args for c in limits.call_args_list], [
                    (m.resource.RLIMIT_FSIZE, (self.p.image_bytes,) * 2),
                    (m.resource.RLIMIT_AS, (self.c.ve_process_bytes,) * 2),
                    (m.resource.RLIMIT_CORE, (0, 0))])
                self.assertNotIn('PRIVATE_CANARY', kw['env'])
                self.assertLessEqual(set(kw['env']), {'PATH', 'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
                    'MKL_NUM_THREADS', 'TESSDATA_PREFIX'})
                self.assertEqual(kw['cwd'], self.root)
                self.assertEqual(kw['stdin'], m.subprocess.DEVNULL)
                self.assertIs(kw['start_new_session'], True)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_output_bounds
    def test_scope_output_bounds(self):
        for reason in (None, 'runner_absent'):
            for out, err, valid in ((b'json', b'', True), (b'x' * 17, b'', False),
                                    (b'json', b'PRIVATE_CANARY' * 2, False)):
                with self.subTest(reason=reason, valid=valid), self._scope_fixture(
                        runner=_ScopeRunner(reason), stdout=out, stderr=err) as f:
                    adapter, runner, child, launch, parser, description, call = f
                    if valid:
                        self.assertIs(call(), description)
                    else:
                        with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_refused$'):
                            call()
                        parser.assert_not_called()
                    self.assertIsNone(adapter.child)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_deadline
    def test_scope_deadline(self):
        for stage in ('entry', 'probe', 'child', 'parse', 'positive'):
            with self.subTest(stage=stage), self._scope_fixture() as f:
                adapter, runner, child, launch, parser, description, call = f
                if stage == 'positive':
                    self.assertIs(call(), description)
                    self.assertEqual(runner.deadlines, [self.state.deadline_ns])
                elif stage == 'entry':
                    with self.assertRaises(ValueError):
                        call(adapter.clock())
                    launch.assert_not_called()
                    self.assertFalse(runner.deadlines)
                elif stage == 'probe':
                    runner.before_prepare = lambda: setattr(adapter, 'clock', lambda: self.state.deadline_ns)
                    with self.assertRaises(ValueError):
                        call()
                    launch.assert_not_called()
                elif stage == 'parse':
                    def slow_parse(*args, **kw):
                        adapter.clock = lambda: self.state.deadline_ns
                        return description
                    parser.side_effect = slow_parse
                    with self.assertRaises(ValueError):
                        call()
                else:
                    child.hang = True
                    with self.assertRaises(ValueError):
                        call()
                    self.assertEqual(launch.call_count, 1)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_drain_descendants
    def test_scope_drain_descendants(self):
        for clean in (False, True):
            with self.subTest(clean=clean), self._scope_fixture() as f:
                adapter, runner, child, launch, parser, description, call = f
                runner.clean = clean
                if clean:
                    self.assertIs(call(), description)
                    self.assertIsNone(adapter.child)
                else:
                    with self.assertRaisesRegex(ValueError, 'ocr_join'):
                        call()
                    self.assertIs(adapter.child, child)
                    self.assertEqual(adapter.pending_scope, runner.units[0])
                    self.assertFalse(child.stdout.closed)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_drain_retry
    def test_scope_drain_retry(self):
        with self._scope_fixture() as f:
            adapter, runner, child, launch, parser, description, call = f
            runner.clean = False
            with self.assertRaisesRegex(ValueError, 'ocr_join'):
                call()
            with self.assertRaisesRegex(ValueError, 'ocr_state'):
                call()
            self.assertEqual(launch.call_count, 1)
            runner.clean = True
            self.assertEqual(adapter.drain(), YES)
            self.assertIsNone(adapter.child)
            self.assertIsNone(adapter.pending_scope)
            self.assertFalse(adapter.poisoned)
            self.assertEqual(adapter.drain(), YES)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_unit_isolation
    def test_scope_unit_isolation(self):
        units = []
        for _ in range(2):
            with self._scope_fixture() as f:
                adapter, runner, child, launch, parser, description, call = f
                call()
                units.extend(runner.units)
                self.assertEqual(runner.drained, runner.units)
        self.assertEqual(len(set(units)), 2)
        self.assertTrue(all(unit.startswith('memoryweb-ocr-') and unit.endswith('.scope') for unit in units))

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_configuration_before_activity
    def test_scope_configuration_before_activity(self):
        for value in (True, False, 0, -1, 1.0):
            with self.subTest(value=value), patch.object(m.subprocess, 'Popen') as launch, \
                 patch.object(m.shutil, 'which') as which, self.assertRaises(ValueError):
                m.LiveOCRDescriber(evidence_policy=replace(self.c, ocr_tasks_max=value),
                    locator_policy=self.p, clock=self.b.clock, ve_root=self.root)
            launch.assert_not_called()
            which.assert_not_called()
        with self._scope_fixture() as f:
            adapter, runner, child, launch, parser, description, call = f
            with patch.object(m.os, 'getuid', return_value=0), patch.object(m, '_ram_path'), self.assertRaises(ValueError):
                call()
            launch.assert_not_called()
            self.assertFalse(runner.deadlines)
            self.assertIs(call(), description)

    @contextmanager
    def _production_scope_fixture(self, fault=None):
        runner = m._OCRScopeRunner()
        calls = []
        state = {'stopped': False}
        loaded = {'LoadState': 'loaded', 'ActiveState': 'active', 'ControlGroup': '/fixture.slice/probe.scope',
                  'TasksCurrent': '1', 'TasksMax': '32', 'MemoryMax': str(self.c.ve_process_bytes),
                  'Slice': 'fixture.slice'}
        absent = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'ControlGroup': '',
                  'TasksCurrent': '[not set]', 'TasksMax': 'infinity', 'MemoryMax': 'infinity', 'Slice': ''}
        def command(argv, **kw):
            calls.append((argv, kw['deadline_ns']))
            if 'hook' in state:
                result = state['hook'](argv, kw)
                if result is not None:
                    return result
            if fault == 'control_hang' and 'stop' in argv:
                raise m.subprocess.TimeoutExpired('PRIVATE_CANARY', 1)
            if '--property=Version' in argv:
                if fault == 'manager_exception':
                    raise ValueError('PRIVATE_CANARY')
                if fault == 'manager':
                    return 1, b''
                return 0, b'garbage' if fault == 'malformed_manager' else b'Version=257\n'
            if 'show' in argv:
                if fault == 'query_failure':
                    return 1, '\n'.join(k + '=' + v for k, v in absent.items()).encode()
                data = dict(absent if state['stopped'] else loaded)
                if fault == 'descendant' and state['stopped']:
                    data = dict(loaded)
                if fault == 'malformed_scope':
                    data['TasksCurrent'] = 'PRIVATE_CANARY'
                if fault == 'property':
                    data['TasksMax'] = 'infinity'
                if fault == 'memory_property':
                    data['MemoryMax'] = 'infinity'
                if fault == 'slice':
                    data['Slice'] = 'unrelated.slice'
                return 0, '\n'.join(k + '=' + v for k, v in data.items()).encode()
            if 'stop' in argv:
                state['stopped'] = True
            return 0, b''
        original_read = Path.read_text
        def read(path, *args, **kw):
            if str(path) == '/proc/self/cgroup':
                return '0::/fixture.slice/parent.scope\n'
            if str(path).endswith('/probe.scope/pids.max'):
                return '31' if fault == 'effective' else '32'
            if str(path).endswith('/probe.scope/memory.max'):
                return 'max' if fault == 'memory_effective' else str(self.c.ve_process_bytes)
            return original_read(path, *args, **kw)
        def which(name):
            return None if fault == 'absent' else '/resolved/' + name
        with self._scope_fixture(runner=runner) as f, \
             patch.object(runner, '_command', side_effect=command), \
             patch.object(m.shutil, 'which', side_effect=which), patch.object(m, '_ocr_child_env', return_value={}), \
             patch.object(Path, 'read_text', read):
            yield f, calls, state

    @contextmanager
    def _controlled_scope_fixture(self, fault=None):
        with self._production_scope_fixture(fault) as (f, calls, state):
            probe = _ScopeChild(b'', b'')
            try:
                f[3].side_effect = [probe, f[2]]
                yield f, calls, state, probe
            finally:
                probe.stdout.close()
                probe.stderr.close()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_control_budget_configuration
    def test_scope_control_budget_configuration(self):
        for value in (0, -1, True, False, 1.0, '100', None):
            with self.subTest(value=value), patch.object(m, 'SubprocessVisionEngine') as engine, \
                 patch.object(m.shutil, 'which') as which, patch.object(m.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_configuration$'):
                    m.LiveOCRDescriber(evidence_policy=replace(self.c, ocr_control_ns=value),
                        locator_policy=self.p, clock=self.b.clock, ve_root=self.root)
                engine.assert_not_called()
                which.assert_not_called()
                launch.assert_not_called()
                policy = self.c
                try:
                    self.c = replace(policy, ocr_control_ns=value)
                    with self.assertRaises(ValueError):
                        self._owner()
                finally:
                    self.c = policy
        self.assertEqual(self.c.ocr_control_ns, 10_000_000_000)
        for value in (1, self.c.ocr_control_ns):
            adapter = m.LiveOCRDescriber(evidence_policy=replace(self.c, ocr_control_ns=value),
                locator_policy=self.p, clock=self.b.clock, ve_root=self.root)
            self.assertEqual(adapter.policy.ocr_control_ns, value)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_control_budget_independent
    def test_scope_control_budget_independent(self):
        with self._controlled_scope_fixture() as (f, calls, state, probe):
            adapter, runner, child, launch, parser, description, call = f
            tick = [self.b.clock()]
            budget = self.p.drain_ns * 100
            adapter.policy = replace(self.c, ocr_control_ns=budget)
            adapter.clock = lambda: tick[0]
            def slow(argv, kw):
                self.assertGreater(kw['deadline_ns'] - tick[0], self.p.drain_ns)
                tick[0] += self.p.drain_ns * 2
                self.assertLess(tick[0], kw['deadline_ns'])
            state['hook'] = slow
            self.assertIs(call(tick[0] + budget * 10), description)
            manager_deadline = calls[0][1]
            probe_cleanup = next(d for argv, d in calls if 'kill' in argv)
            self.assertEqual(manager_deadline, self.b.clock() + budget)
            self.assertGreater(probe_cleanup, manager_deadline)
            self.assertGreater(calls[-1][1], probe_cleanup)
            self.assertFalse(adapter.poisoned)

    def _scope_collect_race(self, action):
        for form in ('absent', 'inactive', 'failed', 'live', 'active', 'cgroup', 'tasks', 'absent_active', 'absent_cgroup', 'malformed', 'query_failure',
                     'exception', 'unjoined', 'expired'):
            with self.subTest(action=action, form=form), self._controlled_scope_fixture() as (f, calls, state, probe):
                adapter, runner, child, launch, parser, description, call = f
                failed = [False]
                tick = [self.b.clock()]
                adapter.clock = lambda: tick[0]
                def command(argv, kw):
                    if action in argv and not failed[0]:
                        failed[0] = True
                        if form == 'unjoined':
                            runner.helper = probe
                        if form == 'expired':
                            tick[0] = kw['deadline_ns']
                        if form in ('exception', 'unjoined', 'expired'):
                            raise OSError('PRIVATE_CANARY')
                        return 1, b'PRIVATE_CANARY'
                    if failed[0] and 'show' in argv:
                        if form == 'query_failure':
                            return 1, b'PRIVATE_CANARY'
                        if form == 'malformed':
                            return 0, b'PRIVATE_CANARY'
                        loaded = form not in ('absent', 'exception', 'absent_active', 'absent_cgroup')
                        data = dict(LoadState='loaded' if loaded else 'not-found',
                            ActiveState='active' if form in ('live', 'active', 'absent_active') else ('failed' if form == 'failed' else 'inactive'),
                            ControlGroup='/fixture.scope' if form in ('live', 'cgroup', 'absent_cgroup') else '',
                            TasksCurrent='1' if form in ('live', 'tasks') else '0',
                            TasksMax='32', MemoryMax=str(self.c.ve_process_bytes), Slice='fixture.slice')
                        return 0, '\n'.join(k + '=' + v for k, v in data.items()).encode()
                state['hook'] = command
                if form in ('absent', 'inactive', 'failed', 'exception'):
                    self.assertIs(call(), description)
                    self.assertIsNone(runner.pending)
                    self.assertIsNone(adapter.pending_scope)
                    self.assertFalse(adapter.poisoned)
                else:
                    with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_join$'):
                        call()
                    self.assertTrue(adapter.poisoned)
                    self.assertIsNotNone(runner.pending)
                    parser.assert_not_called()
                    self.assertEqual(launch.call_count, 1)
                self.assertTrue(failed[0])

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_kill_after_collect
    def test_scope_kill_after_collect(self):
        self._scope_collect_race('kill')

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_stop_after_collect
    def test_scope_stop_after_collect(self):
        self._scope_collect_race('stop')

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_probe_failure_clean_fallback
    def test_scope_probe_failure_clean_fallback(self):
        for failure in ('probe', 'launch', 'diagnostic'):
            with self.subTest(failure=failure), self._controlled_scope_fixture() as (f, calls, state, probe):
                adapter, runner, child, launch, parser, description, call = f
                failed = [False]
                def command(argv, kw):
                    if 'show' in argv and '--property=Version' not in argv and not failed[0]:
                        failed[0] = True
                        raise OSError('PRIVATE_CANARY')
                if failure == 'probe':
                    state['hook'] = command
                elif failure == 'launch':
                    launch.side_effect = [OSError('PRIVATE_CANARY'), child]
                else:
                    probe.stderr.close()
                launches = [0]
                def checked_launch(*args, **kwargs):
                    launches[0] += 1
                    if launches[0] == 1:
                        if failure == 'launch':
                            raise OSError('PRIVATE_CANARY')
                        return probe
                    self.assertIsNone(runner.pending)
                    self.assertFalse(adapter.poisoned)
                    return child
                launch.side_effect = checked_launch
                self.assertIs(call(), description)
                self.assertEqual(adapter.last_execution, m.OCRExecutionRecord('direct', 'scope_unavailable', None, None))
                self.assertEqual(adapter.execution_summary(), m.OCRExecutionSummary(0, 1,
                    (('runner_absent', 0), ('manager_unavailable', 0), ('scope_unavailable', 1))))
                self.assertFalse(adapter.poisoned)
                self.assertIsNone(runner.pending)
                self.assertIsNone(runner.helper)
                self.assertIsNone(adapter.child)
                parser.assert_called_once_with(b'json', ocr_requested=True)
                self.assertEqual(launch.call_count, 2)
                self.assertNotIn('--scope', launch.call_args.args[0])

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_probe_failure_unproven_cleanup
    def test_scope_probe_failure_unproven_cleanup(self):
        for fault in ('descendant', 'query_failure', 'malformed_scope'):
            with self.subTest(fault=fault), self._controlled_scope_fixture(fault) as (f, calls, state, probe):
                adapter, runner, child, launch, parser, description, call = f
                with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_join$'):
                    call()
                self.assertTrue(adapter.poisoned)
                self.assertIsNotNone(runner.pending)
                self.assertIsNone(adapter.last_execution)
                self.assertEqual(launch.call_count, 1)
                parser.assert_not_called()
                self.assertFalse(probe.stdout.closed)
                self.assertEqual(len(calls), 7 if fault == 'descendant' else 3)
                # Recovery is explicit and receives its own budget only on this later call.
                state['stopped'] = True
                with patch.object(runner, '_show', return_value=dict(LoadState='not-found', ActiveState='inactive', ControlGroup='')):
                    self.assertEqual(adapter.drain(), YES)
                self.assertIsNone(runner.pending)
                self.assertFalse(adapter.poisoned)
        self.test_scope_probe_failure_clean_fallback()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_control_latency
    def test_scope_control_latency(self):
        for cleanup, expired in ((False, False), (True, False), (True, True)):
            with self.subTest(cleanup=cleanup, expired=expired), self._controlled_scope_fixture() as (f, calls, state, probe):
                adapter, runner, child, launch, parser, description, call = f
                tick = [self.b.clock()]
                budget = self.p.drain_ns * 100
                adapter.policy = replace(self.c, ocr_control_ns=budget)
                adapter.clock = lambda: tick[0]
                first = [True]
                deadline = tick[0] + budget * (1 if expired else 10)
                def slow(argv, kw):
                    if 'show' in argv and '--property=Version' not in argv:
                        if first[0] or not cleanup:
                            first[0] = False
                            tick[0] = kw['deadline_ns'] + 1
                            if not cleanup:
                                raise m.subprocess.TimeoutExpired('PRIVATE_CANARY', 1)
                state['hook'] = slow
                if cleanup and not expired:
                    self.assertIs(call(deadline), description)
                    self.assertEqual(adapter.last_execution.fallback_reason, 'scope_unavailable')
                else:
                    with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_' + ('refused' if cleanup else 'join') + '$'):
                        call(deadline)
                    self.assertIsNone(adapter.last_execution)
                    self.assertEqual(launch.call_count, 1)
                    parser.assert_not_called()
                self.assertEqual(adapter.poisoned, not cleanup)
                self.assertEqual(runner.pending is None, cleanup)
                self.assertEqual(calls[0][1], self.b.clock() + budget)
                if cleanup:
                    self.assertTrue(probe.stdout.closed)
                else:
                    self.assertFalse(probe.stdout.closed)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_probe_join_required
    def test_scope_probe_join_required(self):
        for joined in (False, True):
            with self.subTest(joined=joined), self._controlled_scope_fixture() as (f, calls, state, probe):
                adapter, runner, child, launch, parser, description, call = f
                probe.hang = not joined
                if joined:
                    self.assertIs(call(), description)
                    self.assertIsNone(runner.pending)
                else:
                    with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_join$'):
                        call()
                    self.assertIsNotNone(runner.pending)
                    self.assertTrue(adapter.poisoned)
                    self.assertEqual(launch.call_count, 1)
                    parser.assert_not_called()
                    self.assertFalse(probe.stdout.closed)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_control_error_privacy
    def test_scope_control_error_privacy(self):
        for fault in ('malformed_manager', 'malformed_scope', 'control_hang', 'query_failure', 'manager_exception'):
            with self.subTest(fault=fault), self._controlled_scope_fixture(fault) as (f, calls, state, probe):
                with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_(refused|join)$') as error:
                    f[-1]()
                self.assertNotIn('PRIVATE_CANARY', str(error.exception))
                self.assertIsNone(error.exception.__cause__)
                f[4].assert_not_called()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_probe_cleanup
    def test_scope_probe_cleanup(self):
        for fault in (None, 'absent', 'manager', 'property', 'effective', 'memory_property', 'memory_effective', 'slice', 'descendant', 'query_failure',
                      'control_hang', 'malformed_manager', 'malformed_scope'):
            with self.subTest(fault=fault), self._production_scope_fixture(fault) as (f, calls, state):
                adapter, runner, child, launch, parser, description, call = f
                # The harmless probe and OCR use separate children/pipes.
                probe = _ScopeChild(b'', b'')
                self.addCleanup(probe.stdout.close)
                self.addCleanup(probe.stderr.close)
                launch.side_effect = [probe, child] if fault not in ('absent', 'manager', 'malformed_manager') else [child]
                if fault in ('descendant', 'query_failure', 'control_hang', 'malformed_manager', 'malformed_scope'):
                    with self.assertRaises(ValueError):
                        call()
                    self.assertIsNone(adapter.last_execution)
                    parser.assert_not_called()
                    self.assertLessEqual(launch.call_count, 1)
                    if fault != 'malformed_manager':
                        self.assertTrue(adapter.poisoned)
                        self.assertIsNotNone(runner.pending)
                else:
                    self.assertIs(call(), description)
                    reason = {'absent': 'runner_absent', 'manager': 'manager_unavailable',
                              'property': 'scope_unavailable', 'effective': 'scope_unavailable', 'memory_property': 'scope_unavailable',
                              'memory_effective': 'scope_unavailable', 'slice': 'scope_unavailable'}.get(fault)
                    self.assertEqual(adapter.last_execution.fallback_reason, reason)
                    self.assertEqual(adapter.last_execution.mode, 'direct' if reason else 'systemd_scope')
                    self.assertIsNone(runner.pending)
                    if fault not in ('absent', 'manager'):
                        self.assertTrue(state['stopped'])
                        controls = [args for args, _ in calls if 'kill' in args or 'stop' in args]
                        self.assertTrue(controls)
                        self.assertTrue(all(args[-1].startswith('memoryweb-ocr-') for args in controls))

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_workflow_result_execution_summary
    def test_workflow_result_execution_summary(self):
        self.describer.execution_summary = lambda: m.OCRExecutionSummary(0, 0, ())
        self.assertIsNone(self._run_live().ocr_execution)
        # Exercise the actual wrapper and its exact built-in reporter, with native composition fake.
        with self._scope_fixture(runner=_ScopeRunner('runner_absent')) as f:
            reported, runner, child, launch, parser, description, call = f
            call()
        self.describer = reported
        with patch.object(m, 'run_workflow', return_value=w.RunResult(0, 'fixture')), \
             patch.object(m, 'open_srf_owners_with_locator') as opened:
            opened.return_value.__enter__.return_value = object()
            result = self._run_live()
        self.assertEqual(result.ocr_execution, m.OCRExecutionSummary(0, 1,
            (('runner_absent', 1), ('manager_unavailable', 0), ('scope_unavailable', 0))))

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_summary_accumulates
    def test_scope_summary_accumulates(self):
        with self._scope_fixture(runner=_ScopeRunner('runner_absent')) as f:
            adapter, runner, child, launch, parser, description, call = f
            call()
            for reason in ('manager_unavailable', None):
                next_child = _ScopeChild()
                self.addCleanup(next_child.stdout.close)
                self.addCleanup(next_child.stderr.close)
                launch.return_value = next_child
                runner.reason = reason
                self.assertIs(call(), description)
            self.assertEqual(adapter.execution_summary(), m.OCRExecutionSummary(1, 2,
                (('runner_absent', 1), ('manager_unavailable', 1), ('scope_unavailable', 0))))

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_helper_bounds
    def test_scope_helper_bounds(self):
        for out, err, hang, good in ((b'Version=257\n', b'', False, True),
                (b'x' * 17, b'', False, False), (b'', b'PRIVATE_CANARY' * 2, False, False),
                (b'Version=257\n', b'', True, False)):
            with self.subTest(good=good, hang=hang), self._scope_fixture(
                    runner=m._OCRScopeRunner(), stdout=out, stderr=err) as f:
                adapter, runner, child, launch, parser, description, call = f
                child.hang = hang
                with patch.object(m.shutil, 'which', side_effect=lambda name: '/resolved/' + name), \
                     patch.object(m, '_ocr_child_env', return_value={}):
                    # A valid manager reply then malformed show response is still a refusal;
                    # no malformed/overflowing/helper-timeout probe authorizes an OCR launch.
                    with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_(refused|join)$'):
                        call()
                self.assertIsNone(adapter.last_execution)
                parser.assert_not_called()
                with patch.object(m.resource, 'setrlimit') as limits:
                    launch.call_args.kwargs['preexec_fn']()
                self.assertEqual([c.args for c in limits.call_args_list], [
                    (m.resource.RLIMIT_AS, (self.c.ve_process_bytes // (self.c.ocr_tasks_max + 2),) * 2),
                    (m.resource.RLIMIT_FSIZE, (0, 0)), (m.resource.RLIMIT_CORE, (0, 0))])
                self.assertLessEqual(set(launch.call_args.kwargs['env']), {'XDG_RUNTIME_DIR'})
                if hang:
                    self.assertTrue(adapter.poisoned)
                    self.assertIs(runner.helper, child)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_metadata_budget
    def test_scope_metadata_budget(self):
        with patch.object(m.subprocess, 'Popen') as launch, self.assertRaisesRegex(ValueError, 'ocr_metadata'):
            m.LiveOCRDescriber(evidence_policy=replace(self.c, metadata_bytes=1),
                locator_policy=self.p, clock=self.b.clock, ve_root=self.root)
        launch.assert_not_called()
        with self._scope_fixture() as f:
            self.assertIs(f[-1](), f[-2])

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_shared_metadata_budget
    def test_scope_shared_metadata_budget(self):
        baseline = len(m._fingerprint(self.target).encode()) + len('Go') + len('button') + 256 + 2048
        adapter = m.LiveOCRDescriber(evidence_policy=self.c, locator_policy=self.p,
            clock=self.b.clock, ve_root=self.root, scope_runner=_ScopeRunner('runner_absent'))
        self.owner.describer = adapter
        self.owner.config = replace(self.c, metadata_bytes=baseline)
        with patch.object(adapter, 'describe', side_effect=self.describer.describe):
            with self.assertRaises(ValueError):
                self._acquire()
            self.owner.config = self.c
            self.assertIsNotNone(self._acquire())

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_cleanup_shared_deadline
    def test_scope_cleanup_shared_deadline(self):
        with self._production_scope_fixture() as (f, calls, state):
            adapter, runner, child, launch, parser, description, call = f
            probe = _ScopeChild(b'', b'')
            self.addCleanup(probe.stdout.close)
            self.addCleanup(probe.stderr.close)
            launch.side_effect = [probe, child]
            tick = [self.b.clock()]
            def clock():
                tick[0] += 1
                return tick[0]
            adapter.clock = clock
            self.assertIs(call(), description)
            cleanup = [(args, deadline) for args, deadline in calls if 'kill' in args or 'stop' in args]
            self.assertEqual(len(cleanup), 2)
            self.assertEqual(len({deadline for _, deadline in cleanup}), 1)
            self.assertTrue(all(deadline < tick[0] + self.c.ocr_control_ns for _, deadline in cleanup))
            probe_deadline = cleanup[0][1]
            after_manager = [deadline for args, deadline in calls if '--property=Version' not in args]
            self.assertEqual(len(set(after_manager)), 3)
            self.assertIn(probe_deadline, after_manager)
            self.assertGreater(probe_deadline, self.b.clock() + self.c.ocr_control_ns)
            self.assertEqual([d for args, d in calls if '--property=Version' in args],
                             [self.state.deadline_ns])

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_parser_refusal_and_binding
    def test_scope_parser_refusal_and_binding(self):
        for reason, fault in itertools.product((None, 'runner_absent'), ('parser', 'marks_path')):
            with self.subTest(reason=reason, fault=fault), self._scope_fixture(runner=_ScopeRunner(reason)) as f:
                adapter, runner, child, launch, parser, description, call = f
                if fault == 'parser':
                    parser.side_effect = ValueError('PRIVATE_CANARY')
                else:
                    parser.return_value = replace(description, marks_png=str(self.source))
                with self.assertRaisesRegex(ValueError, '^LIVE_EVIDENCE:ocr_refused$'):
                    call()
                self.assertIsNone(adapter.child)
                self.assertIsNone(adapter.pending_scope)
                self.assertTrue(child.stdout.closed)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_repository_controls
    def test_scope_repository_controls(self):
        from types import SimpleNamespace
        from tests.test_repo_conventions import TestConventions, TestHyperbooks, anc, reseal
        def inventory():
            result = m.subprocess.run(['git', '-c', 'status.relativePaths=true', 'status', '--short', '--untracked-files=all', '--', '.'],
                capture_output=True, text=True, check=True, cwd=Path(__file__).resolve().parents[1])
            self.assertLessEqual({row[3:] for row in result.stdout.splitlines()},
                {'wender/learn/mw_live_evidence.py', 'tests/test_mw_live_evidence_unit.py'} | {
                    'tests/test_cu_scripts_runtime_unit.py',
                    'tests/test_fx2_f3_harvest_production.py',
                    'tests/test_mw_live_evidence_unit.py',
                    'tests/test_mw_profiles_unit.py',
                    'tests/test_w8u_wa_state_unit.py',
                    'tests/test_ws_s3_u2_blender.py',
                    'tests/test_ws_s3_u2_blender_bpy.py',
                    'tests/test_ws_s3_u3_sculpt_skills.py',
                    'wender/learn/sculpt_replay.py',
                    'wender/perceive/blender.py',
                    'wender/perceive/blender_bpy.py',
                    'wender/remember/sculpt.py',
                    'wender/remember/sculpt_store.py',
                    'wender/remember/skills.py',
                    'wender/state/blender.py',
                    'wender/state/schema.py',
                    'wender/state/wender.blender-scene.v1.schema.json',
                    'wender/state/wender.state.v1.schema.json',
                })
        inventory()
        def forbidden_edit():
            for path in ('wender/scripts/types.py', 'wender/remember/sculpt_unapproved.py'):
                with patch.object(m.subprocess, 'run', return_value=SimpleNamespace(stdout=' M ' + path + '\n')):
                    with self.assertRaises(AssertionError):
                        inventory()
        forbidden_edit()
        with patch.object(self, 'assertLessEqual', return_value=None), self.assertRaises(AssertionError):
            forbidden_edit()
        convention = TestConventions()
        books = TestHyperbooks()
        books.setUp()
        cases = [
            (convention, convention.test_anchors_unique,
             lambda: patch.object(anc, 'scan', return_value=([{'id': 'duplicate'}] * 30, {}))),
            (convention, convention.test_no_image_files,
             lambda: patch.object(Path, 'rglob', return_value=iter([Path('injected.png')]))),
            (books, books.test_manifests_fresh, lambda: patch.object(reseal, 'dump', return_value='STALE')),
        ]
        for case, check, fixture in cases:
            check()
            def negative():
                with fixture(), self.assertRaises(AssertionError):
                    check()
            negative()
            # Delete the assertion used by the repository gate; its negative oracle must now fail.
            with patch.object(case, 'assertEqual', return_value=None), self.assertRaises(AssertionError):
                negative()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_removal_mutants
    def test_scope_removal_mutants(self):
        import inspect
        import textwrap
        mutations = [
            (m.LiveOCRDescriber, 'describe', 'if reason is None:', 'if False:', 'test_scope_command_and_effective_limits'),
            (m.LiveOCRDescriber, 'describe',
             "self.last_execution = OCRExecutionRecord('direct', reason, None, None)",
             'self.last_execution = None', 'test_fallback_recorded'),
            (m._OCRScopeRunner, 'argv', "'--property=TasksMax=' + str(self.policy.ocr_tasks_max)",
             "'--property=TasksMax=999'", 'test_scope_command_and_effective_limits'),
            (m.LiveOCRDescriber, 'drain', "_need(_yes(self.scope_runner.drain(unit=self.pending_scope, child=self.child,\n                locator_policy=self.locator, clock=self.clock, deadline_ns=deadline)), 'ocr_join')",
             'pass', 'test_scope_drain_descendants'),
        ]
        mutations.extend([
            (m._OCRScopeRunner, 'drain',
             "self._command([self.control, '--user', *args], **kw)",
             "self._command([self.control, '--user', *args], locator_policy=locator_policy, clock=clock, deadline_ns=clock() + locator_policy.drain_ns)",
             'test_scope_cleanup_shared_deadline'),
            (m.LiveOCRDescriber, 'describe', "_need(result.marks_png == str(marks_out), 'marks_path')",
             'pass', 'test_scope_parser_refusal_and_binding'),
            (m.LiveOCRDescriber, 'describe', 'env = _ocr_child_env()', 'env = dict(os.environ)',
             'test_scope_rlimits_and_environment'),
            (m.LiveOCRDescriber, 'describe', 'resource.setrlimit(resource.RLIMIT_CORE, (0, 0))',
             'resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))',
             'test_scope_rlimits_and_environment'),
            (m._OCRScopeRunner, '_limits', 'resource.setrlimit(resource.RLIMIT_AS, (allowance,) * 2)',
             'pass', 'test_scope_helper_bounds'),
            (m.LiveEvidenceOwner, 'acquire',
             'metadata += _ocr_metadata(self.config, self.describer._attempts, self.describer._fallback_counts)',
             'pass', 'test_scope_shared_metadata_budget'),
            (m, 'run_live_workflow',
             'ocr_execution=owner.describer.execution_summary() if type(owner.describer) is LiveOCRDescriber else None',
             'ocr_execution=None', 'test_workflow_result_execution_summary'),
            (m.LiveOCRDescriber, 'drain', 'self.poisoned = True',
             'self.child = None; self.poisoned = True', 'test_scope_drain_descendants'),
            (m._OCRScopeRunner, 'argv', "'--property=MemoryMax=' + str(self.policy.ve_process_bytes)",
             "'--property=MemoryMax=999'", 'test_scope_command_and_effective_limits'),
            (m._OCRScopeRunner, 'argv', "'--slice-inherit',", "'--wait',", 'test_scope_command_and_effective_limits'),
            (m._OCRScopeRunner, 'prepare', "data['TasksMax'] == str(policy.ocr_tasks_max)", 'True', 'test_scope_probe_cleanup'),
            (m._OCRScopeRunner, 'prepare', "data['MemoryMax'] == str(policy.ve_process_bytes)", 'True', 'test_scope_probe_cleanup'),
            (m._OCRScopeRunner, 'prepare', "data['Slice'] == slices[-1]", 'True', 'test_scope_probe_cleanup'),
            (m._OCRScopeRunner, 'prepare', "(root / 'pids.max').read_text().strip() == str(policy.ocr_tasks_max)",
             'True', 'test_scope_probe_cleanup'),
            (m._OCRScopeRunner, 'prepare', "(root / 'memory.max').read_text().strip() == str(policy.ve_process_bytes)",
             'True', 'test_scope_probe_cleanup'),
            (m.LiveOCRDescriber, 'describe', 'resource.setrlimit(resource.RLIMIT_AS, (p.ve_process_bytes,) * 2)',
             'pass', 'test_scope_rlimits_and_environment'),
            (m.LiveOCRDescriber, 'describe', 'resource.setrlimit(resource.RLIMIT_FSIZE, (self.locator.image_bytes,) * 2)',
             'pass', 'test_scope_rlimits_and_environment'),
            (m.LiveOCRDescriber, 'describe', 'resource.setrlimit(resource.RLIMIT_CORE, (0, 0))',
             'pass', 'test_scope_rlimits_and_environment'),
            (m.LiveOCRDescriber, 'describe', "unit = 'memoryweb-ocr-' + uuid.uuid4().hex + '.scope'",
             "unit = 'memoryweb-ocr-' + 'a' * 32 + '.scope'", 'test_scope_unit_isolation'),
            (m.LiveOCRDescriber, 'describe', "_need(self.child.returncode == 0, 'ocr_exit')", 'pass',
             'test_scope_failure_never_retries_direct'),
        ])
        mutations.extend([
            (m._OCRScopeRunner, 'prepare', 'cleanup_deadline = clock() + policy.ocr_control_ns',
             'cleanup_deadline = clock() + locator_policy.drain_ns', 'test_scope_control_budget_independent'),
            (m.LiveOCRDescriber, 'drain', 'deadline = self.clock() + self.policy.ocr_control_ns',
             'deadline = self.clock() + self.locator.drain_ns', 'test_scope_control_budget_independent'),
            (m._OCRScopeRunner, 'prepare', 'min(deadline_ns, clock() + policy.ocr_control_ns)',
             'deadline_ns', 'test_scope_control_budget_independent'),
            (m._OCRScopeRunner, 'prepare', 'min(deadline_ns, clock() + policy.ocr_control_ns)',
             'clock() + policy.ocr_control_ns', 'test_scope_cleanup_shared_deadline'),
            (m._OCRScopeRunner, 'prepare', 'except Exception:\n        available = False',
             'except Exception:\n        raise', 'test_scope_probe_failure_clean_fallback'),
            (m._OCRScopeRunner, 'prepare', "_need(_yes(self.drain(unit=unit, child=self.pending[1], locator_policy=locator_policy,\n            clock=clock, deadline_ns=cleanup_deadline)), 'ocr_join')",
             'self.drain(unit=unit, child=self.pending[1], locator_policy=locator_policy, clock=clock, deadline_ns=cleanup_deadline)',
             'test_scope_probe_failure_unproven_cleanup'),
            (m._OCRScopeRunner, 'drain', 'if rc != 0:\n                return _YES if self._empty(unit, **kw) else _NO',
             'if rc != 0:\n                return _NO', 'test_scope_kill_after_collect'),
            (m._OCRScopeRunner, 'drain', 'if rc != 0:\n                return _YES if self._empty(unit, **kw) else _NO',
             'if rc != 0:\n                return _NO', 'test_scope_stop_after_collect'),
            (m._OCRScopeRunner, 'drain', 'if rc != 0:\n                return _YES if self._empty(unit, **kw) else _NO',
             'if rc != 0:\n                return _YES', 'test_scope_kill_after_collect'),
            (m._OCRScopeRunner, 'drain', 'if rc != 0:\n                return _YES if self._empty(unit, **kw) else _NO',
             'if rc != 0:\n                return _YES', 'test_scope_stop_after_collect'),
            (m._OCRScopeRunner, 'drain', 'self.helper is not None or clock() >= deadline_ns',
             'False', 'test_scope_kill_after_collect'),
            (m.LiveOCRDescriber, '__init__', ", 'ocr_control_ns'", '', 'test_scope_control_budget_configuration'),
            (m._OCRScopeRunner, 'prepare', 'self.pending = None', 'pass', 'test_scope_probe_failure_clean_fallback'),
            (m.LiveOCRDescriber, 'describe', 'self.poisoned = False', 'self.poisoned = True',
             'test_scope_probe_failure_clean_fallback'),
            (m._OCRScopeRunner, 'prepare', "_need(clock() < control_deadline, 'ocr_deadline')", 'pass',
             'test_scope_control_latency'),
            (m.LiveOCRDescriber, 'describe', "raise ValueError('LIVE_EVIDENCE:ocr_refused') from None",
             'raise', 'test_scope_control_error_privacy'),
        ])
        mutations.extend([
            (m._OCRScopeRunner, '_empty', "data['TasksCurrent'] == '0'", 'True', 'test_scope_kill_after_collect'),
            (m._OCRScopeRunner, '_empty', "data['ActiveState'] in ('inactive', 'failed')", 'True', 'test_scope_kill_after_collect'),
            (m._OCRScopeRunner, '_empty', "_need(not data['ControlGroup'], 'ocr_scope_query')", 'pass', 'test_scope_kill_after_collect'),
            (m._OCRScopeRunner, '_empty', "data['ActiveState'] == 'inactive' and data['ControlGroup'] == ''", 'True', 'test_scope_kill_after_collect'),
            (m._OCRScopeRunner, 'drain', 'self.helper is not None or clock() >= deadline_ns',
             'clock() >= deadline_ns', 'test_scope_stop_after_collect'),
            (m._OCRScopeRunner, 'drain', 'self.helper is not None or clock() >= deadline_ns',
             'self.helper is not None', 'test_scope_stop_after_collect'),
            (m._OCRScopeRunner, 'drain', '_ocr_group_join(child, clock=clock, deadline_ns=deadline_ns)',
             'pass', 'test_scope_probe_join_required'),
            (m.LiveEvidenceOwner, '_configuration', "if k != 'decode_limits'", "if k not in ('decode_limits', 'ocr_control_ns')",
             'test_scope_control_budget_configuration'),
        ])
        mutations.extend([
            (m._OCRScopeRunner, 'drain', 'self._empty(unit, **kw)',
             'self._empty(unit, locator_policy=locator_policy, clock=clock, deadline_ns=clock() + self.policy.ocr_control_ns)',
             'test_scope_cleanup_shared_deadline'),
            (m.LiveOCRDescriber, 'describe', "raise ValueError('LIVE_EVIDENCE:ocr_refused') from None",
             "raise ValueError('PRIVATE_CANARY') from None", 'test_scope_control_error_privacy'),
        ])
        mutations.extend([
            (m.LiveOCRDescriber, 'describe',
             'if self.poisoned and type(self.scope_runner) is _OCRScopeRunner and self.scope_runner.pending is not None:',
             'if False:', 'test_scope_probe_failure_unproven_cleanup'),
        ])
        for cls, name, before, after, test in mutations:
            source = textwrap.dedent(inspect.getsource(getattr(cls, name)))
            self.assertIn(before, source)
            namespace = {}
            exec(compile(source.replace(before, after), '<scope-removal-mutant>', 'exec'), m.__dict__, namespace)
            # Public API controls pass before each mutation, then must fail under the mutant.
            for mutant in (False, True):
                case = TestLiveEvidence(test)
                case.setUp()
                try:
                    with self.subTest(test=test, mutant=mutant, replacement=before):
                        if mutant:
                            with patch.object(cls, name, namespace[name]), self.assertRaises((AssertionError, ValueError)):
                                getattr(case, test)()
                        else:
                            getattr(case, test)()
                finally:
                    case.doCleanups()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_scope_gate_mutants
    def test_scope_gate_mutants(self):
        original = m._need
        gates = [
            ('ocr_record', 'test_fallback_recorded'),
            ('ocr_configuration', 'test_scope_configuration_before_activity'),
            ('ocr_child_enforcement', 'test_scope_configuration_before_activity'),
            ('ocr_metadata', 'test_scope_metadata_budget'),
            ('ocr_state', 'test_scope_drain_retry'),
            ('ocr_deadline', 'test_scope_deadline'),
            ('ocr_output_bound', 'test_scope_output_bounds'),
            ('ocr_scope_query', 'test_scope_probe_cleanup'),
            ('ocr_scope_alive', 'test_scope_probe_cleanup'),
            ('ocr_manager_probe', 'test_scope_probe_cleanup'),
        ]
        for gate, test in gates:
            case = TestLiveEvidence(test)
            case.setUp()
            def bypass(ok, name):
                if name != gate:
                    original(ok, name)
            try:
                with self.subTest(gate=gate), patch.object(m, '_need', side_effect=bypass), \
                     self.assertRaises((AssertionError, ValueError)):
                    getattr(case, test)()
            finally:
                case.doCleanups()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_delete_only_owned_identity
    def test_delete_only_owned_identity(self):
        e = self._acquire()
        path = e.approved_marks_path
        original = self.root / 'retained-original'
        path.rename(original)
        path.write_bytes(_png())
        path.chmod(0o600)
        self.assertNotEqual(self.owner.release(e, self.state.deadline_ns), YES)
        self.assertTrue(path.exists())
        path.unlink()
        original.rename(path)
        self.assertEqual(self.owner.release(e, self.state.deadline_ns), YES)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_ram_owner_copy_mutation_and_persistent_source
    def test_ram_owner_copy_mutation_and_persistent_source(self):
        original_frame = self._frame
        persistent = self.b.root / 'empty-nonvisual-fixture'
        persistent.write_bytes(b'')
        self._frame = lambda: dict(original_frame(), path=str(persistent))
        with self.assertRaises(ValueError): self._acquire()
        self._frame = original_frame
        real_fstat = m.os.fstat
        def foreign(fd):
            values = list(real_fstat(fd))
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        with patch.object(m.os, 'fstat', side_effect=foreign), self.assertRaises(ValueError):
            self._acquire()
        real_read = m.os.read
        changed = False
        def mutation(fd, amount):
            nonlocal changed
            data = real_read(fd, amount)
            if not changed:
                changed = True
                self.source.write_bytes(_png() + b'changed')
            return data
        with patch.object(m.os, 'read', side_effect=mutation), self.assertRaises(ValueError):
            self._acquire()
        self.source.write_bytes(_png())
        self.assertEqual(self.describer.calls, 0)
        self._acquire()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_binding_assignment_mutant
    def test_binding_assignment_mutant(self):
        import inspect
        import textwrap
        source = textwrap.dedent(inspect.getsource(m._Capabilities.target))
        source = source.replace('self.owner.run = state.run', 'pass')
        namespace = {}
        exec(compile(source, '<binding-assignment-mutant>', 'exec'), m.__dict__, namespace)
        with patch.object(m._Capabilities, 'target', namespace['target']):
            result = self._run_live()
        self.assertNotEqual(result.result.rc, 0)
        self.assertEqual(result.accounting.model_calls, 0)
        self.assertFalse(self.f.native.calls)
        # The positive public API control is test_real_composition_relocation;
        # removing the binding causes its assertions to fail.
        case = TestLiveEvidence('test_real_composition_relocation')
        case.setUp()
        try:
            with patch.object(m._Capabilities, 'target', namespace['target']), self.assertRaises(AssertionError):
                case.test_real_composition_relocation()
        finally:
            case.doCleanups()


    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_integrated_primary_outcomes
    def test_integrated_primary_outcomes(self):
        for code in ('LOW_CONFIDENCE', 'AMBIGUOUS_TARGET', 'LAYOUT_INVARIANT'):
            case = TestLiveEvidence('test_real_composition_relocation')
            case.setUp()
            try:
                def primary(*args):
                    raise w.LocationFailure(code)
                case.ports.capabilities.target = primary
                case.test_real_composition_relocation()
            finally:
                case.doCleanups()
        for error in (RuntimeError('PRIVATE_CANARY'), ValueError('PRIVATE_CANARY')):
            case = TestLiveEvidence('test_real_composition_refusal')
            case.setUp()
            try:
                def primary(*args):
                    raise error
                case.ports.capabilities.target = primary
                result = case._run_live()
                self.assertNotEqual(result.result.rc, 0)
                self.assertEqual(result.result.summary['model_calls'], 0)
                self.assertEqual(case.model.calls, 0)
                self.assertFalse(case.f.native.calls)
                self.assertNotIn('PRIVATE_CANARY', str(result))
            finally:
                case.doCleanups()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_prebound_accounting_and_foreign_owner
    def test_prebound_accounting_and_foreign_owner(self):
        self.owner.run = None
        self.assertIs(self.accounting.run, self.state.run)
        self.caps.target(self.target, self.state, self.state.deadline_ns)
        self.assertIs(self.owner.run, self.state.run)
        self.assertEqual(self.owner.classify(self.target, self.state), 'miss')
        self.owner.run = object()
        with self.assertRaisesRegex(ValueError, 'evidence_run'):
            self.caps.target(self.target, self.state, self.state.deadline_ns)
        self.assertEqual((self.frames, self.model.calls), (0, 0))


    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_capture_future_end_and_input_boundary
    def test_capture_future_end_and_input_boundary(self):
        now = self.b.clock()
        self.state.run.last_input_ns = now - 1
        e = self._acquire()
        self.assertEqual(e.observation.capture_start_ns, self.state.start_ns)
        self.owner.release(e, self.state.deadline_ns)
        for last_input in (now, now + 1):
            self.state.run.last_input_ns = last_input
            with self.assertRaisesRegex(ValueError, 'acquisition_refused'):
                self._acquire()
        self.state.run.last_input_ns = None
        times = iter((now, now, now, now + 1, now))
        self.owner.ports = replace(self.ports, clock=lambda: next(times, now))
        with self.assertRaisesRegex(ValueError, 'acquisition_refused'):
            self._acquire()
        self.assertEqual(self.describer.calls, 1)
        self.assertEqual(self.model.calls, 0)

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_model_wrong_label_and_revocation
    def test_model_wrong_label_and_revocation(self):
        self.describer.change = lambda d: replace(d, marks=d.marks + (
            replace(d.marks[0], id=2, ocr_text='Other'),))
        self.model.answer = 2
        result = self._run_live()
        self.assertEqual(result.result.summary['model_calls'], 1)
        self.assertNotEqual(result.result.rc, 0)
        self.assertFalse(self.f.native.calls)
        self.model.answer = 1
        for revoke in ('custody', 'authority'):
            case = TestLiveEvidence('test_real_composition_relocation')
            case.setUp()
            try:
                def during_model(evidence):
                    if revoke == 'authority':
                        evidence.allocation.step.run.authority = 'foreign'
                    else:
                        # Trusted decision changes only after acquisition admission.
                        object.__setattr__(case.trust, 'custody_check', lambda **k: NO)
                case.model.callback = during_model
                result = case._run_live()
                self.assertEqual(result.accounting.model_calls, 1)
                self.assertNotEqual(result.result.rc, 0)
                self.assertFalse(case.f.native.calls)
            finally:
                case.doCleanups()

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_additional_control_mutants
    def test_additional_control_mutants(self):
        import inspect
        import textwrap
        rows = (
            (m._Capabilities, 'target', "_need(self.owner.run is state.run, 'evidence_run')", 'pass',
             'test_prebound_accounting_and_foreign_owner', m),
            (m._Capabilities, 'target', 'except LocationFailure as exc:', 'except () as exc:',
             'test_integrated_primary_outcomes', m),
            (m.LiveEvidenceOwner, '_delete', 'if callable(drain) and not _yes(drain()):', 'if False:',
             'test_partial_custody', m),
            (m._Transport, 'cancel_and_drain', 'if not _yes(self.original.cancel_and_drain(policy, clock)):', 'if False:',
             'test_cleanup_failure_retains_actor', m),
            (composition.m._EvidenceLease, 'release', 'if self.accepted is evidence:', 'if False:',
             'test_real_composition_relocation', composition.m),
            (w, 'verify_step', 'if truth is Truth.TRUE:', 'if True:',
             'test_postcondition_is_independent', w),
        )
        rows += (
            (composition.m, '_open_srf_owners',
             '_admit(ports.announce_gate(invocation, scope=ports.scope, authority=ports.authority,\n                                 now_ns=ports.clock(), absolute_deadline_ns=ports.limits.deadline_ns))',
             'pass', 'test_announcement_before_surface', composition.m),
            (composition.m, '_validate', 'and bool(invocation.live_ack.strip())', '',
             'test_announcement_before_surface', composition.m),
            (composition.m, '_open_srf_owners', '_need(key not in _CANONICAL)', 'pass',
             'test_canonical_actor_gate', composition.m),
            (composition.m._Capabilities, 'supports',
             "if action['kind'] == 'double_click' or (action['kind'] != 'wait_signal' and action['path'] != 'coordinates'):",
             'if False:', 'test_public_supported_action_gate', composition.m),
        )
        for owner, method, old, new, test, module in rows:
            source = textwrap.dedent(inspect.getsource(getattr(owner, method)))
            self.assertIn(old, source)
            namespace = dict(vars(module))
            exec(compile(source.replace(old, new), '<live-evidence-control-mutant>', 'exec'), namespace)
            result = unittest.TestResult()
            with self.subTest(mutant=method, predicate=test), patch.object(owner, method, namespace[method]):
                TestLiveEvidence(test).run(result)
            self.assertTrue(result.failures, (test, result.errors))
        original = m._need
        for gate, test in (('state', 'test_predispatch_and_primary'),
                           ('freshness', 'test_capture_future_end_and_input_boundary'),
                           ('ocr_output_bound', 'test_bounded_ocr_adapter_without_processes'),
                           ('ocr_exit', 'test_bounded_ocr_adapter_without_processes'),
                           ('ocr_join', 'test_bounded_ocr_adapter_without_processes')):
            def bypass(ok, label):
                if label != gate:
                    original(ok, label)
            result = unittest.TestResult()
            with self.subTest(gate=gate), patch.object(m, '_need', bypass):
                TestLiveEvidence(test).run(result)
            self.assertTrue(result.failures, (gate, result.errors))


    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_public_supported_action_gate
    def test_public_supported_action_gate(self):
        for updates in ({}, dict(kind='double_click'), dict(path='dom')):
            old = self.b.workflow['steps'][0]['action'].copy()
            self.b.workflow['steps'][0]['action'].update(updates)
            with composition.m.open_srf_owners(self.f.invocation, ports=self.ports,
                                               surface_factory=composition._Surface) as owners:
                answer = owners.capabilities.supports(self.b.workflow)
            if updates:
                self.assertNotEqual(answer, YES)
            else:
                self.assertEqual(answer, YES)
            self.b.workflow['steps'][0]['action'] = old

    # @anchor:wa.learn.mw_live_evidence.TestLiveEvidence.test_canonical_actor_gate
    def test_canonical_actor_gate(self):
        # Isolate the canonical gate from the independent actor lock. The real
        # lock plus clean reopen are covered by test_actor_exclusion_and_reopen.
        from unittest.mock import Mock
        ports = replace(self.ports, actor_lock=Mock(spec=composition.ActorLock))
        opened = False
        try:
            with composition.m.open_srf_owners(self.f.invocation, ports=ports,
                                               surface_factory=composition._Surface):
                try:
                    with composition.m.open_srf_owners(self.f.invocation, ports=ports,
                                                       surface_factory=composition._Surface):
                        opened = True
                except ValueError:
                    pass
        except (ValueError, KeyError):
            if not opened:
                raise
        self.assertFalse(opened)
        with composition.m.open_srf_owners(self.f.invocation, ports=ports,
                                           surface_factory=composition._Surface):
            pass
