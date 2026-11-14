# @module {"id":"wa.tests.mw_recovery_unit","layer":4,"owner":"ARCH","deps":["wa.learn.mw_lifecycle","wa.learn.mw_repair","wa.tools.mw_watch"],"book":"HB-WA-s4-learn"}
"""Synthetic metadata and independent trusted fake owners; no live UI or credentials.

Each negative asserts a public API predicate/stage. MWP6_MUTANT selects one AST
check deletion in a subprocess; the audit requires an assertion failure, not an
import error. Numeric fixtures are synthetic [TARGET], results are [MEASURED].
"""
import ast
from contextlib import nullcontext
import copy
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from tools import memoryweb_gate as g
from tools import mw_watch as watch
from wender.learn import mw_lifecycle as life, mw_repair as repair
from wender.scripts.types import AdmissionCheck, Truth, Reason
from tests.test_memoryweb_gate_unit import _fixture, _step, _pred, _explanation

YES = AdmissionCheck(Truth.TRUE, None)
NO = AdmissionCheck(Truth.UNKNOWN, Reason.MISSING)
BUNDLE = g.strict_load((g.DOCS / 'mw.v1.schema.json').read_bytes())


def _raw(value):
    return (g.canon(value) + '\n').encode()


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


class _Resources:
    def __init__(self):
        self.lock = threading.Lock()
        self.allowed = YES
        self.drained = YES
        self.held = 0
        self.log = []
        self.blocked = False

    def admit(self, policy):
        return YES if not self.blocked and policy.peak_bytes >= policy.generation_bytes + policy.state_bytes + policy.journal_bytes + policy.patch_bytes else NO

    def reserve(self, kind, amount):
        with self.lock:
            self.log.append(('reserve', amount))
            if self.held > 0:
                return NO
            if self.allowed is YES:
                self.held += 1
            return self.allowed

    def cancel(self, duration):
        self.log.append(('cancel', duration))
        return YES

    def drain(self, duration):
        self.log.append(('drain', duration))
        if self.drained is not YES: self.blocked = True
        return self.drained

    def release(self):
        with self.lock:
            self.log.append(('release', 0))
            if self.held != 1:
                return NO
            self.held -= 1
            return YES


class _StateOwner:
    def __init__(self, state, policy):
        self.key, self.policy = state.key, policy
        self.saved = life.encode_state(state, policy=policy)
        self.allowed, self.durable = YES, YES
        self.saves = 0

    def check(self, key):
        return self.allowed if key == self.key else NO

    def save(self, raw, expected_digest):
        if _hash(self.saved) != expected_digest or self.durable is not YES:
            return NO
        self.saved = raw
        self.saves += 1
        return YES


class _JournalOwner:
    def __init__(self, binding):
        self.binding, self.suffix, self.allowed = binding, None, YES

    def check(self, binding):
        return self.allowed if binding == self.binding else NO

    @staticmethod
    def digest(event):
        payload = {k: v for k, v in event.items() if k != 'digest'}
        return _hash(b'synthetic-native-owner\x00' + g.canon(payload).encode())

    def authenticate(self, event, binding):
        return YES if self.check(binding) is YES and event['digest'] == self.digest(event) else NO

    def recover(self, raw, suffix):
        self.suffix = (raw, suffix)
        return YES


class _EvidenceOwner:
    def __init__(self, key):
        self.key, self.rows, self.reconciled, self.allowed = key, (), NO, YES

    def check(self, key):
        return self.allowed if key == self.key else NO

    def progress(self, state, journal):
        return self.rows

    def authenticate(self, evidence, state, journal):
        return YES if evidence.predicate in ('checkpoint-a', 'checkpoint-b') and evidence.provenance == 'observation-a' else NO

    def recovery(self, state, journal):
        return self.reconciled

    def finish(self, outcome, state, attempt):
        return YES if outcome == attempt else NO


class _RepairOwner(_StateOwner):
    def __init__(self, state, policy, base, root, context):
        super().__init__(state, policy)
        self.raw, self.root, self.validation_context = base, root, context
        self.staging_root = root
        self.image, self.store_count, self.calls = None, 0, 0
        self.run_owners = SimpleNamespace(mode='shadow')
        self.limits = object()
        self.passes = ['a', 'b']
        self.supported, self.authorized, self.nominal, self.controls = YES, YES, True, True
        self.frozen = True

    def candidate_identity(self, state):
        return 'candidate-' + str(state.candidate_count + 1)

    def base(self, digest, limit):
        return self.raw

    def active_pin(self):
        return 'active-generation'

    def store_preimage(self, raw, digest):
        if self.image is not None:
            raise AssertionError('first touch overwritten')
        self.image = raw
        self.store_count += 1
        return 'undo'

    def preimage(self, ref, limit):
        return self.image

    def diagnosis(self, evidence):
        return dict(classification='workflow_fault', nominal_pass=self.nominal,
                    mutants={'wrong-target': False, 'missing-postcondition': False, 'malformed-evidence': False}, check=YES)

    def support(self, base, raw, state):
        return self.supported

    def shadow_limits(self, state, policy, started):
        return YES

    def shadow_authorization(self, state, identity):
        return self.authorized

    def shadow_executor(self, path, *, bundle, owners, limits):
        assert owners is self.run_owners and limits is self.limits and bundle is BUNDLE
        self.calls += 1
        return SimpleNamespace(rc=0, summary={'evaluation': None})

    def evaluation(self, run, state):
        view = {k: getattr(state, k) for k in ('suite_digest', 'oracle_digest', 'profile_digest', 'support_digest')}
        if not self.frozen:
            view['profile_digest'] = 'f' * 64
        return {**view, 'check': YES, 'passing': self.passes,
                'protected': self.controls, 'support': self.controls, 'privacy': self.controls, 'nominal': True}


class _StoreOwner:
    def __init__(self, root, context):
        self.root, self.bundle, self.base_context = root, BUNDLE, context
        self.clock = lambda: 0
        self.allowed, self.durability = YES, YES
        self.admission = YES

    def writer(self):
        return nullcontext()  # independent fake single-thread owner lease

    def attest(self):
        return self.durability

    def check(self, pin):
        return self.allowed

    def read_check(self, pin):
        return YES

    def pin(self, selector):
        return self

    def admit(self, candidate, expected_pin, retained):
        return self.admission

    def context(self, stage):
        return replace(self.base_context, root=stage)


# @anchor:wa.tests.mw_recovery_unit.TestRecovery
class TestRecovery(unittest.TestCase):
    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.setUp
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.resources = _Resources()
        self.policy = life.RecoveryPolicy(journal_bytes=65536, line_bytes=4096, records=100,
            state_bytes=16384, patch_bytes=4096, patch_operations=16, evidence_rows=100,
            generation_bytes=262144, generation_files=32, retained_generations=20,
            peak_bytes=8388608, poll_ns=1, attempt_ns=10, deadline_ns=1000,
            cancel_ns=10, drain_ns=10, resources=self.resources)
        self.key = life.RecoveryKey('scope', 'authority', 'site', 'lineage', 'episode', life.policy_digest(self.policy))
        self.state = life.WatchState(self.key, 0, 'generation', 'b' * 64, 'journal')
        self.binding = watch.JournalBinding(self.key, 'generation', 'b' * 64, 'journal', (1,), 'run')
        self.journal_owner = _JournalOwner(self.binding)
        self.evidence = _EvidenceOwner(self.key)
        self.owner = _StateOwner(self.state, self.policy)
        self.context = g.ValidationContext(self.root, capability_check=lambda *a: g.GateResult(0, 'PASS'),
            evidence_check=lambda *a: g.GateResult(0, 'PASS'), review_check=lambda *a: g.GateResult(0, 'PASS'),
            chain_check=lambda *a: g.GateResult(0, 'PASS'))
        (self.root / 'source.md').write_bytes(_explanation())
        self.workflow = _fixture('workflow')
        self.workflow.update(id='candidate', steps=[_step()], status='draft', baseline_id=None)
        self.workflow['steps'][0].update(locate_wait_ms=10, locate_poll_ms=2)
        self.workflow['provenance'].update(source='human', source_ref='source.md', quote_sha256=None)
        self.workflow['oracle'].update(postconditions=[_pred()])
        self.base = _raw(self.workflow)
        self.repair_state = life.RepairState(self.key, _hash(self.base), None, None, 0, (), ('a',),
                                            ('a', 'b', 'c'), *(['c' * 64] * 4))
        self.repair_owner = _RepairOwner(self.repair_state, self.policy, self.base, self.root, self.context)

    def _journal(self, kinds=('intent', 'complete', 'verified')):
        records, prev = [], None
        for n, kind in enumerate(kinds, 1):
            event = dict(seq=n, prev=prev, kind=kind, step_seq=1,
                         phase='delivery' if kind in ('intent', 'complete') else 'terminal')
            event['digest'] = self.journal_owner.digest(event)
            records.append(_raw(event))
            prev = event['digest']
        return b''.join(records)

    def _read(self, raw=None):
        return watch.read_native_journal(self._journal() if raw is None else raw,
                    owner=self.journal_owner, binding=self.binding, policy=self.policy)

    def _observe(self, state=None, journal=None):
        return watch.observe(self.state if state is None else state, self._read() if journal is None else journal,
                    evidence_owner=self.evidence, policy=self.policy, clock=lambda: 0)

    def _patch(self, path='/steps/0/locate_poll_ms', old=2, value=1):
        return _raw([dict(op='test', path=path, value=old), dict(op='replace', path=path, value=value)])

    def _repair(self, state=None, patch=None):
        return repair.repair_candidate(self.repair_state if state is None else state,
            self._patch() if patch is None else patch, owners=self.repair_owner, bundle=BUNDLE,
            policy=self.policy, clock=lambda: 0)

    def _life(self, *, paired=False, dream=False, status='shadow'):
        w = copy.deepcopy(self.workflow)
        w.update(status=status)
        w['oracle'].update(planted_mutant_ids=['wrong-target', 'missing-postcondition', 'malformed-evidence'],
                           mutants_total=3, mutants_killed=3, receipt_refs=['receipt.md'])
        cohort = dict(task_ids=['task-a', 'task-b', 'task-c'], fixture_ids=['nominal', 'portable'])
        w['evaluation_profile'].update(kind='paired_replacement_v1' if paired else 'initial_task_v1',
            fixture_ids=cohort['fixture_ids'], cohort_sha256=_hash(g.canon(cohort).encode()),
            baseline_workflow_sha256='d' * 64 if paired else None)
        w['baseline_id'] = 'incumbent' if paired else None
        trials = [dict(task_id=t, authorized=True, live=True, supported=True, required_pass=True,
                       reviewed=True, semantic_verification=True, regression=False,
                       candidate=dict(task_pass=1, fixtures={'nominal': True, 'portable': True}, tokens_used=0),
                       baseline=dict(task_pass=1, fixtures={'nominal': True, 'portable': False}, tokens_used=0))
                  for t in cohort['task_ids']]
        view = dict(nominal_pass=True, killed_mutant_ids=w['oracle']['planted_mutant_ids'], protected_pass=True,
            supported=True, task_scope='read_only', admission_runs=[dict(id=i, independent=True, verified=True, grounded=True,
            reproduced_ppm=1000000, required_segments=[True]) for i in ('admission-a', 'admission-b')],
            cohort=cohort, trials=trials, profile_sha256=_hash(g.canon(w['evaluation_profile']).encode()),
            baseline_eligible=True, baseline_id=w['baseline_id'], baseline_workflow_sha256='d' * 64)
        if dream:
            w['admission_basis'] = 'dream'
            view['episodes'] = [dict(id=i, closed=True, supported=True, tokens_base=100, tokens_cand=80,
                                     pass_base=1, pass_cand=1) for i in ('episode-a', 'episode-b', 'episode-c')]
        else:
            w['admission_basis'] = 'operator_demo'
        self.life_view = view
        self.life_owners = SimpleNamespace(clock=lambda: 0, check=lambda e: YES, bundle=BUNDLE,
            context=replace(self.context, evidence_load=lambda *a: self.life_view), validation_root=self.root)
        return w

    def _transition(self, w, target='promoted'):
        return life.evaluate_transition(_raw(w), target, evidence='evidence', owners=self.life_owners, policy=self.policy)

    def _store(self):
        root = self.root / 'store'; root.mkdir()
        owner = _StoreOwner(root, self.context)
        old = root / 'old'; old.mkdir(); (old / 'artifacts').mkdir()
        (old / 'artifacts' / 'old.json').write_bytes(b'{}\n')
        manifest = _raw(dict(schema='mwp6.generation.v1', files={'old.json': _hash(b'{}\n')}))
        (old / 'manifest.json').write_bytes(manifest)
        selector = _raw(dict(schema='mwp6.selector.v1', generation='old', manifest_digest=_hash(manifest), revision=0))
        (root / 'selector.json').write_bytes(selector)
        pin = life.GenerationPin('old', _hash(manifest), 0, owner)
        site = _fixture('site'); site['id'] = 'site'
        site['provenance'].update(source='human', source_ref='source.md', quote_sha256=None)
        w = copy.deepcopy(self.workflow); w['site_id'] = 'site'
        w['fallback'] = dict(kind='ask_operator', workflow_id=None)
        candidate = {'sites/site/site.json': _raw(site), 'sites/site/source.md': _explanation(),
                     'sites/site/candidate.json': _raw(w)}
        return owner, pin, candidate, selector

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_strict_records
    def test_strict_records(self):
        raw = life.encode_state(self.state, policy=self.policy)
        self.assertEqual(life.decode_state(raw, policy=self.policy), self.state)
        for corrupt in (raw.replace(b'"generation":0', b'"generation":true'),
                        raw.replace(b'"generation":0', b'"generation":0.0'),
                        raw.replace(b'"generation":0', b'"generation":0,"generation":0'),
                        raw.replace(b'"generation":0', b'"generation":0,"unknown":1'),
                        raw.replace(b'"scope"', b'"scope\\n"'), raw + b'\xff'):
            with self.subTest(corrupt=corrupt[:20]), self.assertRaises(ValueError):
                life.decode_state(corrupt, policy=self.policy)
        with self.assertRaisesRegex(ValueError, 'STATE_COUNTER'):
            life.decode_state(raw.replace(b'"generation":0', b'"generation":true'), policy=self.policy)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_policy_missing_boolean_and_caps
    def test_policy_missing_boolean_and_caps(self):
        with self.assertRaises(TypeError):
            life.RecoveryPolicy()
        for kwargs in ({'records': True}, {'journal_bytes': 0}, {'attempts': 4}, {'candidates': 6}, {'mutants': 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(self.policy, **kwargs)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_native_integrity
    def test_native_integrity(self):
        self.assertEqual(self._read().count, 3)
        event = g.strict_load(self._journal().splitlines()[0])
        event['digest'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'EVENT_AUTH'):
            self._read(_raw(event))
        for field, value, stage in (('seq', 2, 'EVENT_CHAIN'), ('phase', 'terminal', 'EVENT_PHASE'),
                                    ('step_seq', True, 'EVENT_PHASE'), ('step_seq', 2, 'EVENT_PHASE'),
                                    ('kind', 'wrong', 'EVENT_PHASE')):
            bad = g.strict_load(self._journal().splitlines()[0]); bad[field] = value
            bad['digest'] = self.journal_owner.digest(bad)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, stage):
                self._read(_raw(bad))
        self.journal_owner.allowed = True
        with self.assertRaisesRegex(ValueError, 'JOURNAL_BINDING'):
            self._read()

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_native_pairing
    def test_native_pairing(self):
        self.assertFalse(self._read().outstanding)
        with self.assertRaisesRegex(ValueError, 'COMPLETE_PAIR'):
            self._read(self._journal(('complete',)))
        with self.assertRaisesRegex(ValueError, 'INTENT_PAIR'):
            self._read(self._journal(('intent', 'intent')))

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_torn_suffix_and_barrier
    def test_torn_suffix_and_barrier(self):
        raw = self._journal() + b'{"seq":4}'  # valid JSON lacking final LF
        journal = self._read(raw)
        state, decision = self._observe(journal=journal)
        self.assertTrue(state.barrier)
        self.assertEqual((decision.rc, decision.verdict), (3, 'TORN'))
        self.assertEqual(self.journal_owner.suffix, (self._journal(), b'{"seq":4}'))
        self.assertNotIn('seq', repr(decision))
        self.assertEqual(self._observe()[1].rc, 0)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_input_uncertainty
    def test_input_uncertainty(self):
        state, decision = self._observe(journal=self._read(self._journal(('intent',))))
        self.assertEqual(decision.reason, Reason.RECONCILIATION)
        self.assertTrue(state.barrier)
        self.assertEqual(watch.begin_attempt(state, state_owner=self.owner, policy=self.policy, clock=lambda: 0)[1].verdict,
                         'INPUT_UNCERTAIN')
        self.assertEqual(self.owner.saves, 0)
        self.assertFalse(self._observe()[0].barrier)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_prefix_substitution_and_rotation
    def test_prefix_substitution_and_rotation(self):
        state, _ = self._observe()
        with self.assertRaisesRegex(ValueError, 'PREFIX_SUBSTITUTION'):
            self._observe(state, self._read(b''))
        rotated = replace(self._read(), binding=replace(self.binding, journal_generation='new'))
        with self.assertRaisesRegex(ValueError, 'PREFIX_SUBSTITUTION'):
            self._observe(state, rotated)
        self.evidence.reconciled = YES
        self.assertEqual(self._observe(state, rotated)[0].journal_generation, 'new')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_persistent_attempts
    def test_persistent_attempts(self):
        state, decision = watch.begin_attempt(self.state, state_owner=self.owner, policy=self.policy, clock=lambda: 0)
        self.assertEqual(decision.verdict, 'ADMITTED')
        loaded = life.decode_state(self.owner.saved, policy=self.policy)
        self.assertEqual((loaded.no_progress, loaded.attempts), (1, ('attempt-1',)))
        self.assertEqual(watch.begin_attempt(loaded, state_owner=self.owner, policy=self.policy, clock=lambda: 0)[1].rc, 5)
        self.assertEqual(state, loaded)
        self.owner.durable = NO
        with self.assertRaisesRegex(ValueError, 'DURABLE_STATE'):
            watch.begin_attempt(self.state, state_owner=self.owner, policy=self.policy, clock=lambda: 0)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_progress_reset_and_dedup
    def test_progress_reset_and_dedup(self):
        state = replace(self.state, no_progress=2)
        self.assertEqual(self._observe(state)[0].no_progress, 2)  # bare verified event and polls
        self.evidence.rows = (watch.ProgressEvidence('receipt-a', 'checkpoint-a', self.key, 'b' * 64, 'cp', 'observation-a'),)
        grown, _ = self._observe(state)
        self.assertEqual((grown.no_progress, grown.progress), (0, ('checkpoint-a',)))
        retried = replace(grown, no_progress=2)
        self.assertEqual(self._observe(retried)[0].no_progress, 2)
        self.evidence.rows = (replace(self.evidence.rows[0], predicate='checkpoint-b'),)
        self.assertEqual(self._observe(retried)[0].progress, ('checkpoint-a',))
        self.evidence.rows = (replace(self.evidence.rows[0], identity='receipt-b', predicate='checkpoint-a'),)
        self.assertEqual(self._observe(retried)[0].no_progress, 2)  # same predicate, fresh receipt
        self.evidence.rows = (replace(self.evidence.rows[0], predicate='crop-change'),)
        with self.assertRaisesRegex(ValueError, 'PROGRESS_AUTH'):
            self._observe(retried)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_attempt_exhaustion
    def test_attempt_exhaustion(self):
        state = self.state
        for n in range(3):
            state, decision = watch.begin_attempt(state, state_owner=self.owner, policy=self.policy, clock=lambda: 0)
            self.assertEqual(decision.rc, 0)
            self.assertEqual(state.no_progress, n + 1)
            state, _ = watch.record_attempt(state, state.attempts[-1], evidence_owner=self.evidence,
                        state_owner=self.owner, policy=self.policy, clock=lambda: 0)
        state = life.decode_state(self.owner.saved, policy=self.policy)
        self.assertEqual(state.no_progress, 3)
        state = replace(state, quarantine=False)  # contrast explicitly tests cap separately
        self.owner.saved = life.encode_state(state, policy=self.policy)
        state, decision = watch.begin_attempt(state, state_owner=self.owner, policy=self.policy, clock=lambda: 0)
        self.assertEqual(decision.verdict, 'STUCK')
        self.assertEqual(len(state.attempts), 3)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_scope_authority_and_deadline
    def test_scope_authority_and_deadline(self):
        self.assertEqual(self._observe()[1].rc, 0)
        for key in (replace(self.key, scope='other-profile'), replace(self.key, authority='stale'),
                    replace(self.key, site_id='other-origin')):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'OBSERVATION_BINDING'):
                self._observe(journal=replace(self._read(), binding=replace(self.binding, key=key)))
        with self.assertRaisesRegex(ValueError, '^DEADLINE$'):
            watch.begin_attempt(self.state, state_owner=self.owner, policy=self.policy, clock=lambda: 1000)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_resource_admission_and_bounds
    def test_resource_admission_and_bounds(self):
        self.resources.allowed = NO
        with self.assertRaisesRegex(ValueError, 'RESOURCE_ADMISSION'):
            self._read(b'invalid')
        self.assertEqual(self.journal_owner.suffix, None)
        self.resources.allowed = YES
        with self.assertRaisesRegex(ValueError, 'JOURNAL_BYTES'):
            self._read(b' ' * 65537)
        self._read()
        self.assertEqual(self.resources.held, 0)
        self.assertEqual(self.resources.log[0][0], 'reserve')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_cancellation_drain
    def test_cancellation_drain(self):
        self.resources.log.clear()
        self.resources.drained = NO
        with self.assertRaisesRegex(ValueError, 'DRAIN'):
            self._read()
        self.assertEqual(self.resources.held, 1)
        self.assertFalse(any(x[0] == 'release' for x in self.resources.log))
        self.resources.drained = YES
        with self.assertRaisesRegex(ValueError, 'AGGREGATE_PEAK'):
            self._read()
        self.resources.blocked = False
        with self.assertRaisesRegex(ValueError, 'RESOURCE_ADMISSION'):
            self._read()
        self.assertEqual(self.resources.held, 1)
        self.assertIs(self.resources.cancel(self.policy.cancel_ns), YES)
        self.assertIs(self.resources.drain(self.policy.drain_ns), YES)
        self.assertIs(self.resources.release(), YES)
        self._read()
        self.assertEqual(self.resources.held, 0)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_diagnosis_nonvacuity
    def test_diagnosis_nonvacuity(self):
        self.assertEqual(repair.diagnose('evidence', owner=self.repair_owner, policy=self.policy), 'workflow_fault')
        self.repair_owner.nominal = False
        with self.assertRaisesRegex(ValueError, 'ORACLE_QUALIFICATION'):
            repair.diagnose('evidence', owner=self.repair_owner, policy=self.policy)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_patch_confinement
    def test_patch_confinement(self):
        self.assertEqual(g.strict_load(repair.apply_patch(self.base, self._patch(), policy=self.policy))['steps'][0]['locate_poll_ms'], 1)
        for path in ('/oracle', '/steps/0/action', '/policy', '/stats/margin_bound', '/steps/0/snapshots',
                     '/script_ref', '/steps/0/verify', '/steps/0/budget'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'PATCH_PATH'):
                repair.apply_patch(self.base, self._patch(path), policy=self.policy)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_pointer_parsing
    def test_pointer_parsing(self):
        for path in ('/steps/00/target', '/steps/-1/target', '/steps/0/ta~2rget',
                     '/steps/../target', '/steps/0/target/', '/steps/0/target\n'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'PATCH_PATH'):
                repair.apply_patch(self.base, self._patch(path), policy=self.policy)
        for op in ('add', 'remove', 'move', 'copy'):
            with self.subTest(op=op), self.assertRaisesRegex(ValueError, 'PATCH_OPERATION'):
                repair.apply_patch(self.base, _raw([dict(op=op, path='/steps/0/target', value=None)]), policy=self.policy)
        with self.assertRaisesRegex(ValueError, 'EXPECTED_VALUE_TEST'):
            repair.apply_patch(self.base, _raw([dict(op='replace', path='/steps/0/target', value=None)]), policy=self.policy)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_timing_envelope
    def test_timing_envelope(self):
        for value in (True, 0, 3):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'TIMING_ENVELOPE'):
                repair.apply_patch(self.base, self._patch(value=value), policy=self.policy)
        with self.assertRaisesRegex(ValueError, 'PATCH_PREIMAGE'):
            repair.apply_patch(self.base, self._patch(old=99), policy=self.policy)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_candidate_real_gate
    def test_candidate_real_gate(self):
        state, decision, raw = self._repair()
        self.assertEqual((decision.verdict, self.repair_owner.calls), ('CANDIDATE', 1))
        self.assertEqual(state.passing, ('a', 'b'))
        self.assertEqual(self.repair_owner.raw, self.base)
        for changed in ('extra', 'id', 'target'):
            w = copy.deepcopy(self.workflow)
            if changed == 'extra': w['extra'] = True
            elif changed == 'id': w['id'] += '\n'
            else: w['steps'][0]['target'] = {'rung': 'invalid'}
            raw = _raw(w)
            state = replace(self.repair_state, base_digest=_hash(raw))
            self.repair_owner = _RepairOwner(state, self.policy, raw, self.root, self.context)
            updated, decision, candidate = self._repair(state)
            self.assertEqual((decision.verdict, self.repair_owner.calls), ('REJECTED', 0))
            self.assertEqual(updated.candidate_count, 1)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_shadow_custody
    def test_shadow_custody(self):
        for mode, authorized in (('promoted', YES), ('shadow', NO), ('shadow', True)):
            owner = self.repair_owner = _RepairOwner(self.repair_state, self.policy, self.base, self.root, self.context)
            owner.run_owners.mode, owner.authorized = mode, authorized
            self.assertEqual(self._repair()[1].verdict, 'REJECTED')
            self.assertEqual(owner.calls, 0)
        owner.authorized = YES
        state = life.decode_state(owner.saved, policy=self.policy)
        self.assertEqual(self._repair(state)[1].verdict, 'CANDIDATE')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_repair_budget_restart_and_first_touch
    def test_repair_budget_restart_and_first_touch(self):
        state = self.repair_state
        self.repair_owner.passes = ['a']  # no growth: all attempts remain charged
        for n in range(5):
            state, decision, _ = self._repair(state)
            self.assertEqual(decision.verdict, 'REJECTED')
            state = life.decode_state(self.repair_owner.saved, policy=self.policy)
            self.assertEqual(state.candidate_count, n + 1)
        count = self.repair_owner.calls
        state, decision, _ = self._repair(state)
        self.assertEqual((decision.verdict, self.repair_owner.calls), ('REPAIR_EXHAUSTED', count))
        self.assertEqual(self.repair_owner.store_count, 1)
        self.assertEqual(state.preimage_digest, _hash(self.base))

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_first_touch_undo_hash
    def test_first_touch_undo_hash(self):
        state, _, _ = self._repair()
        self.repair_owner.image = b'changed'
        with self.assertRaisesRegex(ValueError, 'UNDO_HASH'):
            self._repair(state)
        self.assertEqual(self.repair_owner.store_count, 1)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_strict_pass_growth
    def test_strict_pass_growth(self):
        for passing in (['a'], ['b'], ['a', 'unknown']):
            self.repair_owner = _RepairOwner(self.repair_state, self.policy, self.base, self.root, self.context)
            self.repair_owner.passes = passing
            self.assertEqual(self._repair()[1].verdict, 'REJECTED')
        self.repair_owner = _RepairOwner(self.repair_state, self.policy, self.base, self.root, self.context)
        self.assertEqual(self._repair()[1].verdict, 'CANDIDATE')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_frozen_repair_and_controls
    def test_frozen_repair_and_controls(self):
        for field in ('frozen', 'controls'):
            self.repair_owner = _RepairOwner(self.repair_state, self.policy, self.base, self.root, self.context)
            setattr(self.repair_owner, field, False)
            self.assertEqual(self._repair()[1].verdict, 'REJECTED')
        self.repair_owner = _RepairOwner(self.repair_state, self.policy, self.base, self.root, self.context)
        self.assertEqual(self._repair()[1].verdict, 'CANDIDATE')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_demo_admission
    def test_demo_admission(self):
        w = self._life(status='draft')
        self.assertEqual(self._transition(w, 'shadow').new_lifecycle, 'shadow')
        self.life_view['admission_runs'][1]['id'] = 'admission-a'
        with self.assertRaisesRegex(ValueError, 'DEMO_INDEPENDENCE'):
            self._transition(w, 'shadow')
        self.life_view['admission_runs'][1]['id'] = 'admission-b'
        self.life_view['admission_runs'][0]['required_segments'] = []
        with self.assertRaisesRegex(ValueError, 'DEMO_REPRODUCTION'):
            self._transition(w, 'shadow')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_mutating_demo_supervision
    def test_mutating_demo_supervision(self):
        w = self._life(status='draft')
        self.life_view['task_scope'] = 'project_mutating'
        with self.assertRaisesRegex(ValueError, 'SUPERVISED'):
            self._transition(w, 'shadow')
        self.life_view['admission_runs'][0]['operator_demo'] = True
        self.life_view['admission_runs'][1].update(supervised=True, authorized=True)
        self.assertEqual(self._transition(w, 'shadow').reason, None)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_dream_admission
    def test_dream_admission(self):
        w = self._life(dream=True, status='draft')
        result = self._transition(w, 'shadow')
        self.assertEqual(dict(result.metrics)['min_r_e'], 2)
        self.assertEqual(dict(result.metrics)['saved_tokens_bps'], 2000)
        for value in (None, 0, True):
            self.life_view['episodes'][0]['tokens_base'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'DREAM_DENOMINATOR'):
                self._transition(w, 'shadow')
        self.life_view['episodes'][0]['tokens_base'] = 100
        for change in ({'pass_cand': 0}, {'tokens_cand': 90}):
            original = self.life_view['episodes'][0].copy()
            self.life_view['episodes'][0].update(change)
            with self.assertRaisesRegex(ValueError, 'DREAM_REWARD'):
                self._transition(w, 'shadow')
            self.life_view['episodes'][0] = original

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_mutant_nonvacuity
    def test_mutant_nonvacuity(self):
        w = self._life()
        self.assertEqual(self._transition(w).reason, None)
        for changes in ({'nominal_pass': False}, {'killed_mutant_ids': ['wrong-target', 'missing-postcondition']}):
            old = self.life_view.copy(); self.life_view.update(changes)
            with self.assertRaisesRegex(ValueError, 'MUTANTS'):
                self._transition(w)
            self.life_view = old

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_frozen_cohort
    def test_frozen_cohort(self):
        w = self._life()
        self.assertEqual(dict(self._transition(w).metrics)['margin_mass'], 3)
        self.life_view['trials'][1]['task_id'] = 'task-a'
        with self.assertRaisesRegex(ValueError, 'TASK_INVENTORY'):
            self._transition(w)
        self.life_view['trials'][1]['task_id'] = 'task-b'
        self.life_view['profile_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'FROZEN_COHORT'):
            self._transition(w)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_initial_promotion_all_pass
    def test_initial_promotion_all_pass(self):
        w = self._life()
        self.assertEqual(dict(self._transition(w).metrics)['margin_gain'], 30000)
        self.life_view['trials'][0]['candidate']['task_pass'] = 0
        with self.assertRaisesRegex(ValueError, 'ALL_PASS'):
            self._transition(w)
        w = self._life(); w['baseline_id'] = 'invented'
        with self.assertRaisesRegex(ValueError, 'BASELINE'):
            self._transition(w)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_paired_promotion
    def test_paired_promotion(self):
        w = self._life(paired=True)
        self.assertEqual(dict(self._transition(w).metrics)['margin_gain'], 3)
        self.life_view['baseline_eligible'] = False
        with self.assertRaisesRegex(ValueError, 'INCUMBENT'):
            self._transition(w)
        self.life_view['baseline_eligible'] = True
        for t in self.life_view['trials']:
            t['baseline']['fixtures']['portable'] = True
        with self.assertRaisesRegex(ValueError, 'MARGIN'):
            self._transition(w)
        self.life_view['trials'][0]['candidate']['tokens_used'] = None
        with self.assertRaisesRegex(ValueError, 'TIED_TOKENS'):
            self._transition(w)
        w = self._life(paired=True)
        self.life_view['trials'][0]['candidate']['fixtures']['nominal'] = False
        with self.assertRaisesRegex(ValueError, 'REGRESSION'):
            self._transition(w)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_final_semantics
    def test_final_semantics(self):
        w = self._life()
        self.assertEqual(self._transition(w).reason, None)
        for flag in ('live', 'required_pass', 'semantic_verification', 'authorized'):
            self.life_view['trials'][0][flag] = False
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, 'FINAL_SEMANTICS'):
                self._transition(w)
            self.life_view['trials'][0][flag] = True

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_retirement_and_outage_classification
    def test_retirement_and_outage_classification(self):
        owner = self.owner
        owner.clock = lambda: 0
        owner.outcome = lambda receipt, key: dict(id=receipt[0], kind=receipt[1], check=YES)
        state = self.state
        for n in range(3):
            state = life.record_online_outcome(state, ('run-' + str(n), 'failure'), owners=owner, policy=self.policy)
            state = life.decode_state(owner.saved, policy=self.policy)
            self.assertEqual(state.handoff, n == 2)
        self.assertEqual(len(state.online_failures), 3)
        self.assertEqual(life.record_online_outcome(state, ('run-2', 'failure'), owners=owner, policy=self.policy), state)
        state = life.record_online_outcome(state, ('outage', 'outage'), owners=owner, policy=self.policy)
        self.assertEqual(len(state.online_failures), 3)
        self.assertTrue(state.barrier)
        state = life.record_online_outcome(state, ('success', 'success'), owners=owner, policy=self.policy)
        self.assertEqual(state.online_failures, ())
        self.assertTrue(state.barrier)
        self.assertTrue(state.retired)
        self.assertFalse(state.routable)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_outage_never_counts_as_failure
    def test_outage_never_counts_as_failure(self):
        owner = self.owner; owner.clock = lambda: 0
        owner.outcome = lambda receipt, key: dict(id=receipt, kind='cancelled', check=YES)
        state = life.record_online_outcome(self.state, 'cancel', owners=owner, policy=self.policy)
        self.assertEqual(state.online_failures, ())
        self.assertTrue(state.quarantine)
        self.assertFalse(state.handoff)
        owner.outcome = lambda receipt, key: dict(id=receipt, kind='success', check=True)
        with self.assertRaisesRegex(ValueError, 'OUTCOME_AUTH'):
            life.record_online_outcome(state, 'false-success', owners=owner, policy=self.policy)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_privacy_closed_decisions_and_state
    def test_privacy_closed_decisions_and_state(self):
        self.assertNotIn('diagnostic', life.Decision.__dataclass_fields__)
        with self.assertRaisesRegex(ValueError, 'DECISION'):
            life.Decision('observe', None, 0, 'raw AX password')
        raw = life.encode_state(self.state, policy=self.policy)
        with self.assertRaisesRegex(ValueError, 'STATE_SCHEMA'):
            life.decode_state(raw.replace(b'"generation":0', b'"generation":0,"pixels":[1,2]'), policy=self.policy)
        decision = self._observe()[1]
        self.assertEqual(decision.evidence_refs, ())
        self.assertEqual(decision.verdict, 'OBSERVED')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_cli_no_owner
    def test_cli_no_owner(self):
        self.assertEqual(watch.main(['--journal', '/unused', '--state', '/unused', '--policy', '/unused']), 5)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_atomic_publication_and_pinned_read
    def test_atomic_publication_and_pinned_read(self):
        owner, pin, candidate, selector = self._store()
        self.assertEqual(life.read_generation(pin, owners=owner, policy=self.policy), {'old.json': b'{}\n'})
        result = life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy)
        self.assertTrue(result.committed)
        selected = life.pin_generation(owners=owner, policy=self.policy)
        self.assertEqual(life.read_generation(selected, owners=owner, policy=self.policy), candidate)
        self.assertEqual(life.read_generation(pin, owners=owner, policy=self.policy), {'old.json': b'{}\n'})
        with self.assertRaisesRegex(ValueError, 'EXPECTED_PIN'):
            life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_prepublication_faults
    def test_prepublication_faults(self):
        owner, pin, candidate, selector = self._store()
        faults = ('staging_creation', 'workflow_write', 'workflow_sync', 'manifest_write', 'manifest_sync',
                  'directory_sync', 'validation', 'hash_reread', 'authority_selector_recheck', 'selector_replacement')
        for fault in faults:
            def hook(stage):
                if stage == fault:
                    raise OSError('synthetic crash')
            with self.subTest(fault=fault), self.assertRaises(OSError):
                life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy, fault_hook=hook)
            self.assertEqual((owner.root / 'selector.json').read_bytes(), selector)
            self.assertEqual(life.read_generation(pin, owners=owner, policy=self.policy), {'old.json': b'{}\n'})

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_postpublication_fault
    def test_postpublication_fault(self):
        owner, pin, candidate, _ = self._store()
        def hook(stage):
            if stage == 'postpublication': raise OSError('synthetic crash')
        result = life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy, fault_hook=hook)
        self.assertTrue(result.committed)
        self.assertEqual(result.reason, Reason.RECONCILIATION)
        selected = life.pin_generation(owners=owner, policy=self.policy)
        self.assertEqual(life.read_generation(selected, owners=owner, policy=self.policy), candidate)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_atomic_rechecks_hash_and_containment
    def test_atomic_rechecks_hash_and_containment(self):
        owner, pin, candidate, selector = self._store()
        owner.durability = NO
        with self.assertRaisesRegex(ValueError, 'STORE_AUTHORITY'):
            life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy)
        owner.durability = YES
        with self.assertRaisesRegex(ValueError, 'GENERATION_CONTENT'):
            life.commit_generation({'../escape.json': b'{}'}, expected_pin=pin, owners=owner, policy=self.policy)
        def hook(stage):
            if stage == 'hash_reread':
                directory = next(owner.root.glob('generation-*'))
                (directory / 'artifacts' / 'sites' / 'site' / 'candidate.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'FILE_HASH'):
            life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy, fault_hook=hook)
        self.assertEqual((owner.root / 'selector.json').read_bytes(), selector)
        def stale(stage):
            if stage == 'authority_selector_recheck': owner.allowed = NO
        with self.assertRaisesRegex(ValueError, 'PUBLICATION_RECHECK'):
            life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy, fault_hook=stale)
        self.assertEqual((owner.root / 'selector.json').read_bytes(), selector)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_frozen_policy_and_aggregate_peak
    def test_frozen_policy_and_aggregate_peak(self):
        self.assertEqual(self._observe()[1].rc, 0)
        self.binding = replace(self.binding, key=replace(self.key, policy_digest='f' * 64))
        self.journal_owner.binding = self.binding
        with self.assertRaisesRegex(ValueError, 'FROZEN_POLICY'):
            self._read()
        self.policy = replace(self.policy, peak_bytes=1)
        with self.assertRaisesRegex(ValueError, 'AGGREGATE_PEAK'):
            self._read()

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_trace_to_draft
    def test_trace_to_draft(self):
        w = self._life(status='draft')
        self.life_view = {k: True for k in ('closed_manifest', 'reviewed_explanation', 'grounded_steps',
                          'capabilities', 'explicit_oracle', 'explicit_fallback', 'privacy_approved')}
        self.assertEqual(self._transition(w, 'draft').prior_lifecycle, 'trace')
        self.life_view['privacy_approved'] = False
        with self.assertRaisesRegex(ValueError, 'TRACE_ADMISSION'):
            self._transition(w, 'draft')
        w = self._life(status='draft', dream=True)
        episodes = self.life_view['episodes']
        self.life_view = {k: True for k in ('closed_manifest', 'reviewed_explanation', 'grounded_steps',
                          'capabilities', 'explicit_oracle', 'explicit_fallback', 'privacy_approved')}
        self.life_view['episodes'] = episodes
        self.assertEqual(self._transition(w, 'draft').prior_lifecycle, 'trace')
        self.life_view['episodes'] = episodes[:2]
        with self.assertRaisesRegex(ValueError, 'TRACE_DREAM'):
            self._transition(w, 'draft')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_fallback_reconciliation
    def test_fallback_reconciliation(self):
        w = self._life()
        promoted = self._transition(w).workflow_bytes
        w['status'] = 'promoted'
        self.life_view.update(epoch_closed=True, failure_confirmed=True, reconciled=True,
            baseline_eligible=True, baseline_workflow_sha256=_hash(promoted))
        self.life_owners.baseline = lambda digest, limit: promoted
        self.assertEqual(self._transition(w, 'fallback').new_lifecycle, 'fallback')
        self.life_view['reconciled'] = False
        with self.assertRaisesRegex(ValueError, 'FALLBACK_RECONCILIATION'):
            self._transition(w, 'fallback')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_retirement_transition
    def test_retirement_transition(self):
        w = self._life()
        self.life_view.update(online_outcomes=[dict(id='run-a', kind='failure'), dict(id='run-b', kind='failure')],
                              routability_expired=True)
        with self.assertRaisesRegex(ValueError, 'RETIREMENT'):
            self._transition(w, 'retired')
        self.life_view['online_outcomes'].append(dict(id='run-c', kind='failure'))
        self.assertEqual(self._transition(w, 'retired').new_lifecycle, 'retired')
        self.life_view['online_outcomes'][1]['kind'] = 'success'
        with self.assertRaisesRegex(ValueError, 'RETIREMENT'):
            self._transition(w, 'retired')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_shadow_rejects_duplicate_irreversible_tasks
    def test_shadow_rejects_duplicate_irreversible_tasks(self):
        self.repair_owner.authorized = NO  # independent owner denies reused irreversible item
        state, decision, _ = self._repair()
        self.assertEqual((decision.verdict, self.repair_owner.calls), ('REJECTED', 0))
        self.repair_owner.authorized = YES
        self.assertEqual(self._repair(state)[1].verdict, 'CANDIDATE')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_repair_deadline
    def test_repair_deadline(self):
        times = iter((0, 0, 20))
        state, decision, raw = repair.repair_candidate(self.repair_state, self._patch(), owners=self.repair_owner,
                    bundle=BUNDLE, policy=self.policy, clock=lambda: next(times))
        self.assertEqual((decision.verdict, raw, state.candidate_count), ('REJECTED', None, 1))
        self.assertEqual(self.repair_owner.active_pin(), 'active-generation')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_repair_support_and_privacy
    def test_repair_support_and_privacy(self):
        self.repair_owner.supported = NO  # changed target lacks approved grounding/privacy
        target = _fixture('t2')
        target.update(locale='en', rel_box_bps=[0, 0, 1000, 1000])
        state, decision, raw = self._repair(patch=self._patch('/steps/0/target', None, target))
        self.assertEqual((decision.verdict, raw, self.repair_owner.calls), ('REJECTED', None, 0))
        self.repair_owner.supported = YES
        self.assertEqual(self._repair(state)[1].verdict, 'CANDIDATE')

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_transition_semantic_owner_required
    def test_transition_semantic_owner_required(self):
        w = self._life()
        self.life_owners.context = replace(self.life_owners.context, evidence_check=lambda *a: True)
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_AUTH'):
            self._transition(w)
        self.life_owners.context = replace(self.life_owners.context, evidence_check=lambda *a: g.GateResult(0, 'PASS'))
        self.assertEqual(self._transition(w).reason, None)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_generation_validation_and_symlinks
    def test_generation_validation_and_symlinks(self):
        owner, pin, candidate, selector = self._store()
        invalid = dict(candidate)
        invalid['sites/site/candidate.json'] = _raw({**g.strict_load(invalid['sites/site/candidate.json']), 'extra': True})
        with self.assertRaisesRegex(ValueError, 'GENERATION_GATE'):
            life.commit_generation(invalid, expected_pin=pin, owners=owner, policy=self.policy)
        self.assertEqual((owner.root / 'selector.json').read_bytes(), selector)
        path = owner.root / 'old' / 'artifacts' / 'old.json'
        path.unlink(); path.symlink_to(self.root / 'source.md')
        with self.assertRaises(OSError):
            life.read_generation(pin, owners=owner, policy=self.policy)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_attempt_completion_proof
    def test_attempt_completion_proof(self):
        state, _ = watch.begin_attempt(self.state, state_owner=self.owner, policy=self.policy, clock=lambda: 0)
        with self.assertRaisesRegex(ValueError, 'ATTEMPT_COMPLETION'):
            watch.record_attempt(state, 'unknown', evidence_owner=self.evidence, state_owner=self.owner,
                                 policy=self.policy, clock=lambda: 0)
        loaded = life.decode_state(self.owner.saved, policy=self.policy)
        self.assertTrue(loaded.barrier)
        self.assertFalse(watch.record_attempt(state, state.attempts[-1], evidence_owner=self.evidence,
            state_owner=self.owner, policy=self.policy, clock=lambda: 0)[0].barrier)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_exact_bound_allocation
    def test_exact_bound_allocation(self):
        raw = self._journal()
        self.policy = replace(self.policy, journal_bytes=len(raw))
        self.binding = replace(self.binding, key=replace(self.key, policy_digest=life.policy_digest(self.policy)))
        self.journal_owner.binding = self.binding
        self.assertEqual(self._read(raw).count, 3)
        with self.assertRaisesRegex(ValueError, 'JOURNAL_BYTES'):
            self._read(raw + b'\n')
        self.assertEqual(self.resources.held, 0)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_cli_owned_polling
    def test_cli_owned_polling(self):
        journal_path, state_path = self.root / 'journal.jsonl', self.root / 'state.json'
        journal_path.write_bytes(self._journal())
        state_path.write_bytes(self.owner.saved)
        decisions, sleeps = [], []
        owners = SimpleNamespace(policy=lambda path: self.policy, journal=self.journal_owner, binding=self.binding,
            evidence=self.evidence, state=self.owner, clock=lambda: 0, sleep=lambda n: sleeps.append(n),
            decision=lambda d: decisions.append(d) or YES)
        argv = ['--journal', str(journal_path), '--state', str(state_path), '--policy', str(self.root / 'policy.json'), '--polls', '2']
        start = len(self.resources.log)
        self.assertEqual(watch.main(argv, owners=owners), 0)
        self._one_reservation(start)
        self.assertEqual(len(decisions), 2)
        self.assertEqual(sleeps, [self.policy.poll_ns])
        self.assertEqual(life.decode_state(self.owner.saved, policy=self.policy).no_progress, 0)
        self.assertEqual(watch.main(argv[:-1] + ['0'], owners=owners), 2)
        direct = subprocess.run([sys.executable, 'tools/mw_watch.py', *argv], capture_output=True, text=True)
        self.assertEqual(direct.returncode, 5)
        self.assertNotIn('Traceback', direct.stderr)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_altered_authenticated_prefix
    def test_altered_authenticated_prefix(self):
        state, _ = self._observe()
        substituted = self._journal(('intent', 'complete', 'failed'))
        with self.assertRaisesRegex(ValueError, 'PREFIX_SUBSTITUTION'):
            self._observe(state, self._read(substituted))
        self.assertEqual(self._observe(state)[0].prefix_digest, state.prefix_digest)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_identifier_trailing_lf
    def test_identifier_trailing_lf(self):
        raw = life.encode_state(self.state, policy=self.policy)
        with self.assertRaisesRegex(ValueError, 'STATE_PIN'):
            life.decode_state(raw.replace(b'"source_generation":"generation"',
                                          b'"source_generation":"generation\\n"'), policy=self.policy)
        self.assertEqual(life.decode_state(raw, policy=self.policy), self.state)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_target_patch_positive
    def test_target_patch_positive(self):
        target = _fixture('t2')
        target.update(locale='en', rel_box_bps=[0, 0, 1000, 1000])
        state, decision, raw = self._repair(patch=self._patch('/steps/0/target', None, target))
        self.assertEqual(decision.verdict, 'CANDIDATE')
        self.assertEqual(g.strict_load(raw)['steps'][0]['target'], target)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_surviving_oracle_mutant
    def test_surviving_oracle_mutant(self):
        owner = self.repair_owner
        original = owner.diagnosis('x')
        original['mutants']['wrong-target'] = True
        owner.diagnosis = lambda e: original
        with self.assertRaisesRegex(ValueError, 'ORACLE_QUALIFICATION'):
            repair.diagnose('x', owner=owner, policy=self.policy)
        original['mutants']['wrong-target'] = False
        self.assertEqual(repair.diagnose('x', owner=owner, policy=self.policy), 'workflow_fault')

    def _one_reservation(self, start):
        self.assertEqual([name for name, _ in self.resources.log[start:]],
                         ['reserve', 'cancel', 'drain', 'release'])
        self.assertEqual(self.resources.held, 0)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_composed_capacity
    def test_composed_capacity(self):
        owner, pin, candidate, _ = self._store()
        def held(*args, **kwargs):
            self.assertEqual(self.resources.held, 1)
        original = self.repair_owner.shadow_executor
        def shadow(*args, **kwargs):
            held()
            return original(*args, **kwargs)
        self.repair_owner.shadow_executor = shadow
        start = len(self.resources.log)
        self.assertEqual(self._repair()[1].verdict, 'CANDIDATE')
        self.assertEqual(self.repair_owner.active_pin(), 'active-generation')
        self._one_reservation(start)
        start = len(self.resources.log)
        result = life.commit_generation(candidate, expected_pin=pin, owners=owner,
                                        policy=self.policy, fault_hook=held)
        self.assertTrue(result.committed)
        self._one_reservation(start)
        original_check = owner.read_check
        owner.read_check = lambda pin: held() or original_check(pin)
        start = len(self.resources.log)
        selected = life.pin_generation(owners=owner, policy=self.policy)
        self._one_reservation(start)
        self.assertEqual(life.read_generation(selected, owners=owner, policy=self.policy), candidate)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_standalone_capacity
    def test_standalone_capacity(self):
        owner, pin, _, _ = self._store()
        raw = life.encode_state(self.state, policy=self.policy)
        journal = self._read()
        calls = [lambda: life.decode_state(raw, policy=self.policy),
                 lambda: life.read_generation(pin, owners=owner, policy=self.policy),
                 lambda: repair.diagnose('evidence', owner=self.repair_owner, policy=self.policy),
                 lambda: repair.apply_patch(self.base, self._patch(), policy=self.policy),
                 lambda: self._read(),
                 lambda: watch.observe(self.state, journal, evidence_owner=self.evidence,
                                       policy=self.policy, clock=lambda: 0)]
        for call in calls:
            start = len(self.resources.log)
            self.assertIsNotNone(call())
            self._one_reservation(start)
            self.resources.allowed = NO
            start = len(self.resources.log)
            with self.assertRaises(life._Refusal) as caught:
                call()
            self.assertEqual(caught.exception.gate, 'RESOURCE_ADMISSION')
            self.assertEqual([n for n, _ in self.resources.log[start:]], ['reserve'])
            self.assertEqual(self.resources.held, 0)
            self.resources.allowed = YES

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_concurrent_capacity
    def test_concurrent_capacity(self):
        owner, pin, candidate, selector = self._store()
        entered, leave = threading.Event(), threading.Event()
        errors = []
        saved = self.owner.saved
        def hook(stage):
            self.assertEqual(self.resources.held, 1)
            if stage == 'staging_creation':
                entered.set()
                self.assertTrue(leave.wait(5))
        def run():
            try:
                life.commit_generation(candidate, expected_pin=pin, owners=owner,
                                       policy=self.policy, fault_hook=hook)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            start = len(self.resources.log)
            with self.assertRaises(life._Refusal) as caught:
                life.decode_state(saved, policy=self.policy)
            self.assertEqual(caught.exception.gate, 'RESOURCE_ADMISSION')
            self.assertIs(caught.exception.reason, Reason.CAPACITY)
            self.assertEqual([n for n, _ in self.resources.log[start:]], ['reserve'])
            self.assertEqual(self.resources.held, 1)
            self.assertEqual(self.owner.saved, saved)
            self.assertEqual((owner.root / 'selector.json').read_bytes(), selector)
        finally:
            leave.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(life.decode_state(saved, policy=self.policy), self.state)
        self.assertEqual(self.resources.held, 0)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_cleanup_exception_custody
    def test_cleanup_exception_custody(self):
        with self.assertRaises(life._Refusal) as caught:
            self._read(b' ' * (self.policy.journal_bytes + 1))
        self.assertEqual(caught.exception.gate, 'JOURNAL_BYTES')
        self.assertEqual(self.resources.held, 0)
        self.resources.drained = NO
        with self.assertRaises(life._Refusal) as caught:
            self._read(b' ' * (self.policy.journal_bytes + 1))
        self.assertEqual(caught.exception.gate, 'DRAIN')
        self.assertIsInstance(caught.exception.__cause__, life._Refusal)
        self.assertEqual(caught.exception.__cause__.gate, 'JOURNAL_BYTES')
        self.assertEqual(self.resources.held, 1)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_cleanup_controls
    def test_cleanup_controls(self):
        for evidence in (NO, AdmissionCheck(Truth.FALSE, Reason.CAPACITY)):
            with patch.object(self.resources, 'cancel', return_value=evidence):
                start = len(self.resources.log)
                with self.assertRaisesRegex(life._Refusal, 'DRAIN'):
                    self._read()
                self.assertEqual(self.resources.held, 1)
                self.assertFalse(any(n == 'release' for n, _ in self.resources.log[start:]))
            self.assertIs(self.resources.cancel(self.policy.cancel_ns), YES)
            self.assertIs(self.resources.drain(self.policy.drain_ns), YES)
            self.assertIs(self.resources.release(), YES)
        self.resources.drained = AdmissionCheck(Truth.FALSE, Reason.CAPACITY)
        with self.assertRaisesRegex(life._Refusal, 'DRAIN'):
            self._read()
        self.assertEqual(self.resources.held, 1)
        self.resources.drained = YES
        self.resources.blocked = False
        self.assertIs(self.resources.cancel(self.policy.cancel_ns), YES)
        self.assertIs(self.resources.drain(self.policy.drain_ns), YES)
        self.assertIs(self.resources.release(), YES)
        self.assertIs(self.resources.release(), NO)
        self.assertEqual(self.resources.held, 0)
        with patch.object(self.resources, 'release', return_value=NO):
            with self.assertRaisesRegex(life._Refusal, 'RELEASE'):
                self._read()
        self.assertEqual(self.resources.held, 1)
        self.assertIs(self.resources.release(), YES)
        class Interrupted(BaseException):
            pass
        original = Interrupted()
        with patch.object(self.journal_owner, 'check', side_effect=original):
            with self.assertRaises(Interrupted) as caught:
                self._read()
            self.assertIs(caught.exception, original)
            self.assertEqual(self.resources.held, 0)
            self.resources.drained = NO
            with self.assertRaisesRegex(life._Refusal, 'DRAIN') as caught:
                self._read()
            self.assertIs(caught.exception.__cause__, original)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_staging_maintenance_contract
    def test_staging_maintenance_contract(self):
        contract = ' '.join(life.commit_generation.__doc__.split())
        for obligation in (
                'every immediate nonsymlink directory',
                'incomplete crashed staging',
                'Crashed staging remains private, never deleted automatically',
                'exclude concurrent writers',
                'verify the selector and selected manifest',
                'trusted confirmation',
                'neither selected, pinned nor required for rollback/evidence retention',
                'Only then may that confirmed abandoned directory be removed',
                'If custody cannot be established, retain it and stop',
                'do not increase limits or alter the selector',
                'AUSENTE — no inventar'):
            with self.subTest(obligation=obligation):
                self.assertIn(obligation, contract)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_spec_control_mutants
    def test_spec_control_mutants(self):
        for identity, (_module, _function, _gate, test) in MUTANTS.items():
            program = ('from tests import test_mw_recovery_unit as t; '
                       't._install_mutant(' + repr(identity) + '); '
                       'import unittest; unittest.main(module=t, argv=["mutant", "TestRecovery.' + test + '"])')
            result = subprocess.run([sys.executable, '-c', program], text=True, capture_output=True, timeout=30)
            output = result.stdout + result.stderr
            with self.subTest(mutant=identity):
                self.assertNotEqual(result.returncode, 0, 'SURVIVED ' + identity)
                self.assertIn('FAILED (', output, output[-1200:])
                self.assertIn('Ran 1 test', output, output[-1200:])
            print('MUTANT ' + identity + (' KILLED' if result.returncode and 'FAILED (' in output else 'SURVIVED'), flush=True)


    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_startup_barrier_needs_reconciliation
    def test_startup_barrier_needs_reconciliation(self):
        state, _ = self._observe(journal=self._read(self._journal(('intent',))))
        with self.assertRaisesRegex(ValueError, 'RESTART_BARRIER'):
            self._observe(state)
        self.evidence.reconciled = YES
        self.assertFalse(self._observe(state)[0].barrier)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_native_closed_canonical_records
    def test_native_closed_canonical_records(self):
        raw = self._journal().splitlines(keepends=True)[0]
        event = g.strict_load(raw)
        with self.assertRaisesRegex(ValueError, 'EVENT_KEYS'):
            self._read(_raw({**event, 'unknown': 1}))
        with self.assertRaisesRegex(ValueError, 'EVENT_CHAIN'):
            self._read(raw.replace(b':', b': ', 1))
        for bad in (raw.replace(b'"seq":1', b'"seq":1.0'),
                    raw.replace(b'"seq":1', b'"seq":1,"seq":1'), raw[:-1] + b'\xff\n'):
            with self.assertRaises(ValueError): self._read(bad)
        self.assertEqual(self._read(raw).count, 1)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_unknown_baseline_outcome
    def test_unknown_baseline_outcome(self):
        w = self._life(paired=True)
        self.assertEqual(self._transition(w).reason, None)
        self.life_view['trials'][0]['baseline']['fixtures']['nominal'] = None
        with self.assertRaisesRegex(ValueError, 'BASELINE_OUTCOMES'):
            self._transition(w)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_frozen_verifier_support_authorization
    def test_frozen_verifier_support_authorization(self):
        w = self._life()
        pins = dict(verifier_digest='a' * 64, support_digest='b' * 64, authorization_digest='c' * 64)
        self.life_view.update(pins)
        def check(kind, artifact, related):
            if kind == 'lifecycle':
                return g.GateResult(0, 'PASS') if all(related[0].get(k) == v for k, v in pins.items()) else g.GateResult(5, 'PIN')
            return g.GateResult(0, 'PASS')
        self.life_owners.context = replace(self.life_owners.context, evidence_check=check)
        self.assertEqual(self._transition(w).reason, None)
        for key, pin in pins.items():
            self.life_view[key] = 'f' * 64
            with self.assertRaisesRegex(ValueError, 'EVIDENCE_AUTH'):
                self._transition(w)
            self.life_view[key] = pin

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_stats_recomputed_and_boolean_refused
    def test_stats_recomputed_and_boolean_refused(self):
        w = self._life()
        w['stats'].update(margin_gain=1, margin_mass=1, online_shadow_runs=900, online_fail_streak=900)
        metrics = dict(self._transition(w).metrics)
        self.assertEqual((metrics['margin_gain'], metrics['margin_mass'], metrics['online_shadow_runs']), (30000, 3, 3))
        self.assertIsNone(metrics['online_fail_streak'])  # no authenticated online history supplied
        w['stats']['online_shadow_runs'] = True
        with self.assertRaisesRegex(ValueError, 'STATS_TYPES'):
            self._transition(w)

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_final_semantic_negative_fixtures
    def test_final_semantic_negative_fixtures(self):
        for fixture in ('preexisting-download', 'partial-step-success', 'quiet-incomplete-screen'):
            w = self._life()
            self.life_view['trials'][0]['semantic_verification'] = False
            with self.subTest(fixture=fixture), self.assertRaisesRegex(ValueError, 'FINAL_SEMANTICS'):
                self._transition(w)
        w = self._life()
        self.assertEqual(self._transition(w).reason, None)


    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_generation_retention_limit
    def test_generation_retention_limit(self):
        owner, pin, candidate, selector = self._store()
        self.policy = replace(self.policy, retained_generations=1)
        with self.assertRaisesRegex(ValueError, 'GENERATION_RETENTION'):
            life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy)
        self.assertEqual((owner.root / 'selector.json').read_bytes(), selector)
        (owner.root / 'abandoned-stage').mkdir()
        self.policy = replace(self.policy, retained_generations=2)
        with self.assertRaisesRegex(ValueError, 'GENERATION_RETENTION'):
            life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy)
        self.assertEqual((owner.root / 'selector.json').read_bytes(), selector)
        self.assertTrue((owner.root / 'abandoned-stage').is_dir())
        self.policy = replace(self.policy, retained_generations=3)
        self.assertTrue(life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy).committed)


    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_durability_before_publication
    def test_durability_before_publication(self):
        owner, pin, candidate, selector = self._store()
        synced, publication = set(), []
        original_sync, original_replace = os.fsync, os.replace
        def _sync(fd):
            info = os.fstat(fd)
            synced.add((info.st_dev, info.st_ino))
            original_sync(fd)
        def _replace(src, dst):
            stage = Path(src).parent
            required = [p for p in stage.rglob('*') if p.is_file() or p.is_dir()] + [stage, owner.root]
            identities = {(p.stat().st_dev, p.stat().st_ino) for p in required}
            self.assertTrue(identities <= synced, 'publication preceded file/directory durability')
            self.assertEqual((owner.root / 'selector.json').read_bytes(), selector)
            publication.append(True)
            original_replace(src, dst)
        with patch.object(life.os, 'fsync', _sync), patch.object(life.os, 'replace', _replace):
            result = life.commit_generation(candidate, expected_pin=pin, owners=owner, policy=self.policy)
        self.assertTrue(result.committed)
        self.assertEqual(publication, [True])

    # @anchor:wa.tests.mw_recovery_unit.TestRecovery.test_each_repair_control
    def test_each_repair_control(self):
        for control in ('protected', 'support', 'privacy', 'nominal'):
            owner = self.repair_owner = _RepairOwner(self.repair_state, self.policy, self.base, self.root, self.context)
            original = owner.evaluation
            owner.evaluation = lambda run, state: {**original(run, state), control: False}
            with self.subTest(control=control):
                self.assertEqual(self._repair()[1].verdict, 'REJECTED')
                self.assertEqual(owner.active_pin(), 'active-generation')
        self.repair_owner = _RepairOwner(self.repair_state, self.policy, self.base, self.root, self.context)
        self.assertEqual(self._repair()[1].verdict, 'CANDIDATE')


# Each entry identifies a specific guard and a public-API test. "if:*" controls
# delete a branch; "assign:*" and "keyword:*" remove a progress/accounting check.
MUTANTS = {
    'strict-release': ('test', '_Resources.release', 'if:self.held != 1', 'test_cleanup_controls'),
    'standalone-observe': ('watch', 'observe', 'remove-capacity', 'test_standalone_capacity'),
    'standalone-read_native_journal': ('watch', 'read_native_journal', 'remove-capacity', 'test_standalone_capacity'),
    'standalone-apply_patch': ('repair', 'apply_patch', 'remove-capacity', 'test_standalone_capacity'),
    'standalone-diagnose': ('repair', 'diagnose', 'remove-capacity', 'test_standalone_capacity'),
    'standalone-read_generation': ('life', 'read_generation', 'remove-capacity', 'test_standalone_capacity'),
    'standalone-decode_state': ('life', 'decode_state', 'remove-capacity', 'test_standalone_capacity'),
    'maintenance-safety': ('life', 'commit_generation', 'unsafe-maintenance-doc', 'test_staging_maintenance_contract'),
    'cli-decode': ('watch', 'main', 'public:local._decode_state', 'test_cli_owned_polling'),
    'cli-journal': ('watch', 'main', 'public:_read_native_journal', 'test_cli_owned_polling'),
    'cli-observe': ('watch', 'main', 'public:_observe', 'test_cli_owned_polling'),
    'repair-diagnose': ('repair', 'repair_candidate', 'public:_diagnose', 'test_composed_capacity'),
    'repair-patch': ('repair', 'repair_candidate', 'public:_apply_patch', 'test_composed_capacity'),
    'commit-read': ('life', 'commit_generation', 'public:_read_generation', 'test_composed_capacity'),
    'pin-read': ('life', 'pin_generation', 'public:_read_generation', 'test_composed_capacity'),
    'cleanup-cause': ('life', '_capacity', 'remove-cause', 'test_cleanup_exception_custody'),
    'body-error': ('watch', '_read_native_journal', 'JOURNAL_BYTES', 'test_cleanup_exception_custody'),
    'crashed-retention': ('life', 'commit_generation', 'ignore-crashed', 'test_generation_retention_limit'),
    'cleanup-release': ('life', '_capacity', 'RELEASE', 'test_cleanup_controls'),
    'premature-release': ('life', 'commit_generation', 'early-release', 'test_composed_capacity'),
    'strict-held': ('test', '_Resources.reserve', 'if:self.held > 0', 'test_concurrent_capacity'),
    'strict-reconciliation': ('test', '_Resources.reserve', 'if:self.held > 0', 'test_cancellation_drain'),
    'standalone-admission': ('life', '_capacity', 'RESOURCE_ADMISSION', 'test_standalone_capacity'),

    'strict-counter': ('life', 'WatchState.__post_init__', 'STATE_COUNTER', 'test_strict_records'),
    'strict-fields': ('life', '_decode_state', 'STATE_SCHEMA', 'test_privacy_closed_decisions_and_state'),
    'identifier-lf': ('life', 'WatchState.__post_init__', 'STATE_PIN', 'test_identifier_trailing_lf'),
    'protocol-caps': ('life', 'RecoveryPolicy.__post_init__', 'PROTOCOL_CAPS', 'test_policy_missing_boolean_and_caps'),
    'file-durability': ('life', '_write', 'remove-fsync', 'test_durability_before_publication'),
    'directory-durability': ('life', '_sync_dir', 'remove-fsync', 'test_durability_before_publication'),
    'generation-retention': ('life', 'commit_generation', 'GENERATION_RETENTION', 'test_generation_retention_limit'),
    'native-keys': ('watch', '_read_native_journal', 'EVENT_KEYS', 'test_native_closed_canonical_records'),
    'startup-barrier': ('watch', '_observe', 'RESTART_BARRIER', 'test_startup_barrier_needs_reconciliation'),
    'stats-types': ('life', 'evaluate_transition', 'STATS_TYPES', 'test_stats_recomputed_and_boolean_refused'),
    'baseline-unknown': ('life', '_metrics', 'BASELINE_OUTCOMES', 'test_unknown_baseline_outcome'),
    'evidence-auth': ('life', '_load_evidence', 'EVIDENCE_AUTH', 'test_frozen_verifier_support_authorization'),
    'in-place-write': ('life', 'commit_generation', 'in-place-write', 'test_prepublication_faults'),
    'publication-receipt': ('life', 'commit_generation', 'committed-false', 'test_postpublication_fault'),
    'native-auth': ('watch', '_read_native_journal', 'EVENT_AUTH', 'test_native_integrity'),
    'native-chain': ('watch', '_read_native_journal', 'EVENT_CHAIN', 'test_native_integrity'),
    'native-phase': ('watch', '_read_native_journal', 'EVENT_PHASE', 'test_native_integrity'),
    'native-pair': ('watch', '_read_native_journal', 'COMPLETE_PAIR', 'test_native_pairing'),
    'torn': ('watch', '_read_native_journal', 'assign:torn', 'test_torn_suffix_and_barrier'),
    'input-uncertainty': ('watch', '_observe', 'if:journal.outstanding', 'test_input_uncertainty'),
    'prefix': ('watch', '_observe', 'PREFIX_SUBSTITUTION', 'test_prefix_substitution_and_rotation'),
    'durable-attempt': ('life', '_state_save', 'DURABLE_STATE', 'test_persistent_attempts'),
    'progress-growth': ('watch', '_observe', 'keyword:no_progress', 'test_progress_reset_and_dedup'),
    'evidence-dedup': ('watch', '_observe', 'if:e.identity not in consumed', 'test_progress_reset_and_dedup'),
    'attempt-cap': ('watch', 'begin_attempt', 'if:state.no_progress >= policy.attempts', 'test_attempt_exhaustion'),
    'attempt-proof': ('watch', 'record_attempt', 'ATTEMPT_COMPLETION', 'test_attempt_completion_proof'),
    'scope': ('watch', '_observe', 'OBSERVATION_BINDING', 'test_scope_authority_and_deadline'),
    'authority': ('watch', '_read_native_journal', 'JOURNAL_BINDING', 'test_native_integrity'),
    'deadline': ('life', '_time', 'DEADLINE', 'test_scope_authority_and_deadline'),
    'peak': ('life', '_capacity', 'AGGREGATE_PEAK', 'test_frozen_policy_and_aggregate_peak'),
    'reservation': ('life', '_capacity', 'RESOURCE_ADMISSION', 'test_resource_admission_and_bounds'),
    'input-bound': ('watch', '_read_native_journal', 'JOURNAL_BYTES', 'test_exact_bound_allocation'),
    'drain': ('life', '_capacity', 'DRAIN', 'test_cancellation_drain'),
    'oracle': ('repair', '_diagnose', 'ORACLE_QUALIFICATION', 'test_diagnosis_nonvacuity'),
    'patch-confinement': ('repair', '_apply_patch', 'PATCH_PATH', 'test_patch_confinement'),
    'patch-operation': ('repair', '_apply_patch', 'PATCH_OPERATION', 'test_pointer_parsing'),
    'patch-test': ('repair', '_apply_patch', 'EXPECTED_VALUE_TEST', 'test_pointer_parsing'),
    'timing-envelope': ('repair', '_apply_patch', 'TIMING_ENVELOPE', 'test_timing_envelope'),
    'candidate-gate': ('repair', 'repair_candidate', 'CANDIDATE_GATE', 'test_candidate_real_gate'),
    'shadow-custody': ('repair', 'repair_candidate', 'SHADOW_CUSTODY', 'test_shadow_custody'),
    'repair-cap': ('repair', 'repair_candidate', 'if:state.candidate_count >= policy.candidates or state.quarantine', 'test_repair_budget_restart_and_first_touch'),
    'undo': ('repair', 'repair_candidate', 'UNDO_HASH', 'test_first_touch_undo_hash'),
    'pass-growth': ('repair', 'repair_candidate', 'STRICT_PASS_GROWTH', 'test_strict_pass_growth'),
    'frozen-repair': ('repair', 'repair_candidate', 'FROZEN_EVALUATION', 'test_frozen_repair_and_controls'),
    'repair-controls': ('repair', 'repair_candidate', 'REPAIR_CONTROLS', 'test_frozen_repair_and_controls'),
    'repair-deadline': ('repair', 'repair_candidate', 'CANDIDATE_DEADLINE', 'test_repair_deadline'),
    'repair-support': ('repair', 'repair_candidate', 'PATCH_SUPPORT', 'test_repair_support_and_privacy'),
    'demo': ('life', '_metrics', 'DEMO_INDEPENDENCE', 'test_demo_admission'),
    'segments': ('life', '_metrics', 'DEMO_REPRODUCTION', 'test_demo_admission'),
    'supervised': ('life', '_metrics', 'SUPERVISED', 'test_mutating_demo_supervision'),
    'dream-denominator': ('life', '_metrics', 'DREAM_DENOMINATOR', 'test_dream_admission'),
    'dream-reward': ('life', '_metrics', 'DREAM_REWARD', 'test_dream_admission'),
    'mutant-nonvacuity': ('life', '_metrics', 'MUTANTS', 'test_mutant_nonvacuity'),
    'cohort': ('life', '_metrics', 'FROZEN_COHORT', 'test_frozen_cohort'),
    'task-inventory': ('life', '_metrics', 'TASK_INVENTORY', 'test_frozen_cohort'),
    'initial-all-pass': ('life', '_metrics', 'ALL_PASS', 'test_initial_promotion_all_pass'),
    'baseline': ('life', '_metrics', 'BASELINE', 'test_initial_promotion_all_pass'),
    'incumbent': ('life', '_metrics', 'INCUMBENT', 'test_paired_promotion'),
    'margin': ('life', '_metrics', 'MARGIN', 'test_paired_promotion'),
    'tied-tokens': ('life', '_metrics', 'TIED_TOKENS', 'test_paired_promotion'),
    'regression': ('life', '_metrics', 'REGRESSION', 'test_paired_promotion'),
    'final-semantics': ('life', '_metrics', 'FINAL_SEMANTICS', 'test_final_semantics'),
    'retirement': ('life', 'evaluate_transition', 'RETIREMENT', 'test_retirement_transition'),
    'failure-classification': ('life', 'record_online_outcome', 'if:kind == \'failure\'', 'test_outage_never_counts_as_failure'),
    'outcome-auth': ('life', 'record_online_outcome', 'OUTCOME_AUTH', 'test_outage_never_counts_as_failure'),
    'privacy': ('life', 'Decision.__post_init__', 'DECISION', 'test_privacy_closed_decisions_and_state'),
    'expected-pin': ('life', 'commit_generation', 'EXPECTED_PIN', 'test_atomic_publication_and_pinned_read'),
    'manifest-hash': ('life', '_read_generation', 'FILE_HASH', 'test_atomic_rechecks_hash_and_containment'),
    'publication-recheck': ('life', 'commit_generation', 'PUBLICATION_RECHECK', 'test_atomic_rechecks_hash_and_containment'),
    'store-durability': ('life', 'commit_generation', 'STORE_AUTHORITY', 'test_atomic_rechecks_hash_and_containment'),
    'generation-validation': ('life', 'commit_generation', 'GENERATION_GATE', 'test_generation_validation_and_symlinks'),
    'trace': ('life', 'evaluate_transition', 'TRACE_ADMISSION', 'test_trace_to_draft'),
    'trace-dream': ('life', 'evaluate_transition', 'TRACE_DREAM', 'test_trace_to_draft'),
    'fallback': ('life', 'evaluate_transition', 'FALLBACK_RECONCILIATION', 'test_fallback_reconciliation'),
    'policy-freeze': ('life', '_policy_key', 'FROZEN_POLICY', 'test_frozen_policy_and_aggregate_peak'),
}


def _install_mutant(identity):
    module_name, qualified, target, _test = MUTANTS[identity]
    module = {'life': life, 'watch': watch, 'repair': repair, 'test': sys.modules[__name__]}[module_name]
    tree = ast.parse(Path(module.__file__).read_text())
    parts = qualified.split('.')
    container = tree
    for name in parts:
        node = next(n for n in container.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name)
        container = node
    changed = []

    class RemoveGuard(ast.NodeTransformer):
        def visit_Constant(self, node):
            if target == 'unsafe-maintenance-doc' and isinstance(node.value, str) and 'neither selected, pinned' in node.value:
                node.value = node.value.replace('neither selected, pinned nor required', 'unused')
                changed.append(True)
            return node

        def visit_Expr(self, node):
            if target == 'remove-fsync' and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == 'os.fsync':
                changed.append(True)
                return ast.Pass()
            return self.generic_visit(node)

        def visit_Raise(self, node):
            if target == 'remove-cause' and node.cause is not None:
                node.cause = None
                changed.append(True)
            return self.generic_visit(node)

        def visit_With(self, node):
            if target == 'remove-capacity':
                changed.append(True)
                return node.body
            if target == 'early-release':
                node.body.insert(0, ast.parse('policy.resources.release()').body[0])
                changed.append(True)
            return self.generic_visit(node)

        def visit_Call(self, node):
            if target.startswith('public:') and ast.unparse(node.func) == target[7:]:
                name = target[7:]
                public = name.replace('local._', 'local.') if name.startswith('local.') else name[1:]
                node.func = ast.parse(public, mode='eval').body
                changed.append(True)
            if target == 'in-place-write' and isinstance(node.func, ast.Name) and node.func.id == '_write' and ast.unparse(node.args[0]) == 'path':
                node.args[0] = ast.parse("root / expected_pin.generation / 'artifacts' / 'old.json'", mode='eval').body
                changed.append(True)
            if target == 'committed-false' and isinstance(node.func, ast.Name) and node.func.id == 'TransitionResult':
                node.args[5] = ast.Constant(False); changed.append(True)
            if isinstance(node.func, (ast.Name, ast.Attribute)) and (
                getattr(node.func, 'id', None) == '_need' or getattr(node.func, 'attr', None) == '_need') and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == target:
                node.args[0] = ast.Constant(True); changed.append(True)
            return self.generic_visit(node)

        def visit_If(self, node):
            if target.startswith('if:') and ast.unparse(node.test) == target[3:]:
                if identity == 'failure-classification':
                    node.test = ast.parse("kind != 'success'", mode='eval').body
                elif identity == 'evidence-dedup':
                    node.test = ast.Constant(True)
                else:
                    node.test = ast.Constant(False)
                changed.append(True)
            return self.generic_visit(node)

        def visit_AugAssign(self, node):
            if target == 'ignore-crashed' and ast.unparse(node.target) == 'retained':
                node.value = ast.parse("entry.is_dir(follow_symlinks=False) and (Path(entry.path) / 'manifest.json').is_file()", mode='eval').body
                changed.append(True)
            return self.generic_visit(node)

        def visit_Assign(self, node):
            if target == 'assign:torn' and any(isinstance(n, ast.Name) and n.id == 'torn' for n in node.targets):
                node.value = ast.Constant(False); changed.append(True)
            return self.generic_visit(node)

        def visit_keyword(self, node):
            if target == 'keyword:no_progress' and node.arg == 'no_progress':
                node.value = ast.Constant(0); changed.append(True)
            return self.generic_visit(node)

    node = RemoveGuard().visit(node)
    if not changed:
        raise AssertionError('mutation target missing: ' + identity)
    code = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace = {}
    exec(compile(code, module.__file__, 'exec'), module.__dict__, namespace)
    function = namespace[node.name]
    if target == 'in-place-write':
        original_write = module._write
        def unsafe_write(path, raw):
            if path.name == 'old.json': path.write_bytes(raw)
            else: original_write(path, raw)
        module._write = unsafe_write
    if len(parts) == 2:
        setattr(getattr(module, parts[0]), parts[1], function)
    else:
        setattr(module, qualified, function)
