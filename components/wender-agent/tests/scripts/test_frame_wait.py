# @module {"id":"wa.tests.scripts.test_frame_wait","layer":3,"owner":"ACT","deps":["wa.scripts.frame_wait","wa.scripts.types","wa.tests.scripts.test_predicates"],"book":"HB-WA-s2-act"}
"""Synthetic public-port controls; no production cancellation proof claimed."""
import dataclasses as _dc
from pathlib import Path as _Path
import sys as _sys
import types as _types
import unittest as _unittest
from unittest import mock as _mock
from wender.scripts import frame_wait as _w, types as _t
from tests.scripts.test_predicates import _fixture

_YES = _t.AdmissionCheck(_t.Truth.TRUE, None)
_NO = _t.AdmissionCheck(_t.Truth.FALSE, _t.Reason.MISSING)
_MS = 1000000


# @anchor:wa.tests.scripts.test_frame_wait._sample
def _sample(identity='p', truth=_t.Truth.TRUE, end=20):
    f = _fixture(truth=truth)
    r = f['registry']
    obs = r.register(_dc.replace(f['observation'], publication_identity=identity,
                                 capture_start_ns=0, capture_end_ns=end))
    fact = r.register(_dc.replace(f['fact'], observation=obs))
    payload = r.register(_dc.replace(f['payload'], facts=(fact,)))
    binding = r.register(_dc.replace(f['binding'], observation=obs, payload=payload))
    f.update(binding=binding, payload=payload)
    f['args'].update(evidence=(binding,), observation=obs)
    return f


# @anchor:wa.tests.scripts.test_frame_wait._Host
class _Host:
    # @anchor:wa.tests.scripts.test_frame_wait._Host.__init__
    def __init__(self, samples=None):
        self.f = _sample()
        self.samples = [self.f] if samples is None else samples
        self.now = 0
        self.held = []
        self.released = []
        self.calls = 0
        self.boundaries = []
        self.authority = self.install = self.quiet = self.drain = True
        self.throw = self.late = self.stuck = False
        self.cp = _t.CancellationPolicy(1000*_MS, 'escalate', 'join', 'synthetic',
                                      1100*_MS, 1200*_MS, 4, 10000)
        self.holder = _t.Holder('waiter', _t.Purpose.QUERY, self.f['scope'], 100,
                               self.f['handle'], 1000*_MS, self.f['handle'], self.cp)
        ref = _t.RegistryRef('cancel', 'test-incarnation', 7, self.f['handle'])
        self.contract = _t.HolderContract('waiter', _t.Purpose.QUERY, self.f['scope'],
                                         100, 1000*_MS, ref, ref, self.cp)
        self.budget = _t.WaitPolicy(100, 100, 4, 100*_MS, 50, 100, 20)
        self.wait = _w.FrameWait(observations=self, scheduler=self, control=self,
            cancellation=self, holder_contract=self.contract, contexts=self,
            quiet=self, boundary=self, clock=lambda: self.now, max_notification_bytes=128)

    # @anchor:wa.tests.scripts.test_frame_wait._Host.check_installation
    def check_installation(self, *args):
        return _YES if self.install else _NO

    # @anchor:wa.tests.scripts.test_frame_wait._Host.check
    def check(self, *args, **kwargs):
        if 'quiet_window_ns' in kwargs:
            return _YES if self.quiet else _NO
        if 'scope' in kwargs:
            return _YES if args[0] in [f['args']['observation'] for f in self.samples if f] else _NO
        return _YES if self.authority and args[0] is self.holder and args[1] is self.contract else _NO

    # @anchor:wa.tests.scripts.test_frame_wait._Host.wait_until_ns
    def wait_until_ns(self, boundary_ns, *, cancel):
        self.boundaries.append(boundary_ns)
        if not self.stuck:
            self.now = boundary_ns

    # @anchor:wa.tests.scripts.test_frame_wait._Host.acquire
    def acquire(self, after_identity, *, deadline_ns, cancel):
        self.calls += 1
        if self.throw:
            raise RuntimeError('private-canary-error')
        if self.late:
            self.now = deadline_ns + 1
        f = self.samples[min(self.calls-1, len(self.samples)-1)]
        if f is None:
            return _w._refusal(_t.Reason.MISSING)
        self.active = f
        token = _t.PrivateRef('reader', 'test', str(self.calls))
        self.held.append(token)
        return _t.AcquisitionResult(f['args']['observation'], None,
            _t.AcquisitionDisposition.ACQUIRED, _t.Ownership.TRANSIENT, token,
            None, _t.InputDisposition.NOT_SENT)

    # @anchor:wa.tests.scripts.test_frame_wait._Host.release
    def release(self, token):
        if not any(token is owned for owned in self.held):
            return _NO
        self.held = [owned for owned in self.held if owned is not token]
        self.released.append(token)
        return _YES

    # @anchor:wa.tests.scripts.test_frame_wait._Host.context
    def context(self, predicate, observation):
        # Predicate retained by the host is the caller's exact object. All
        # observations and evidence rows retain their genuine registry identity.
        f = self.active
        r = f['registry']
        r.register(predicate)
        binding = r.register(_dc.replace(f['binding'], predicate=predicate))
        return f['args'] | {'evidence': (binding,)}

    # @anchor:wa.tests.scripts.test_frame_wait._Host.request
    def request(self, contract):
        self.proof = _t.CancellationResult(True, self.drain, False, False,
            self.drain, (), contract.cancellation_endpoint if self.drain else None,
            None if self.drain else _t.MissingReason.INCOMPLETE)
        return self.proof

    # @anchor:wa.tests.scripts.test_frame_wait._Host.verify
    def verify(self, contract, proof):
        return _YES if self.drain and contract is self.contract and proof is self.proof else _NO

    # @anchor:wa.tests.scripts.test_frame_wait._Host.run
    def run(self):
        return self.wait.wait_until(self.f['node'], self.f['scope'], self.budget,
                                    100*_MS, self.holder)


# @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait
class TestFrameWait(_unittest.TestCase):
    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_semantics
    def test_semantics(self):
        for truth in (_t.Truth.TRUE, _t.Truth.FALSE, _t.Truth.UNKNOWN):
            h = _Host([_sample(truth=truth)])
            result = h.run()
            self.assertEqual(result.observation is not None, truth is _t.Truth.TRUE)
            if result.observation is not None:
                self.assertEqual(len(h.held), 1)
                self.assertIs(h.release(result.owner_token), _YES)
            self.assertEqual(h.held, [])
            self.assertIs(result.input_disposition, _t.InputDisposition.NOT_SENT)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_quiet
    def test_quiet(self):
        h = _Host()
        h.quiet = False
        result = h.run()
        self.assertIsNone(result.observation)
        self.assertEqual(h.held, [])
        h = _Host()
        self.assertIsNotNone(h.run().observation)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_pacing
    def test_pacing(self):
        h = _Host([_sample('a', _t.Truth.FALSE), _sample('a', _t.Truth.FALSE),
                   _sample('b', _t.Truth.FALSE), _sample('last')])
        result = h.run()
        self.assertIsNotNone(result.observation)
        self.assertEqual(h.boundaries, [33333334, 66666667, 100*_MS])
        self.assertEqual(h.wait.attempt_count, 4)
        self.assertEqual(h.wait.accepted_publication_count, 3)
        self.assertEqual(len(h.held), 1)
        h.release(result.owner_token)
        self.assertEqual(h.held, [])

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_duplicate_conflict
    def test_duplicate_conflict(self):
        first = _sample('same', _t.Truth.FALSE)
        h = _Host([first, _sample('same', end=21)])
        result = h.run()
        self.assertIs(result.reason, _t.Reason.CONFLICT)
        self.assertEqual(h.calls, 2)
        self.assertEqual(h.held, [])
        h = _Host([first, first])
        self.assertIsNone(h.run().observation)
        self.assertEqual(h.wait.accepted_publication_count, 1)
        self.assertEqual(h.held, [])

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_late
    def test_late(self):
        h = _Host()
        h.late = True
        result = h.run()
        self.assertIs(result.reason, _t.Reason.DEADLINE)
        self.assertEqual(h.held, [])
        h = _Host()
        self.assertIsNotNone(h.run().observation)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_capture_deadline
    def test_capture_deadline(self):
        h = _Host([_sample(end=101*_MS)])
        self.assertIs(h.run().reason, _t.Reason.STALE)
        self.assertEqual(h.held, [])
        h = _Host([_sample(end=100*_MS)])
        self.assertIsNotNone(h.run().observation)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_nonadvancing
    def test_nonadvancing(self):
        h = _Host([_sample(truth=_t.Truth.FALSE)])
        h.stuck = True
        self.assertIs(h.run().reason, _t.Reason.DEADLINE)
        self.assertEqual(h.calls, 1)
        self.assertEqual(h.held, [])
        h = _Host()
        self.assertIsNotNone(h.run().observation)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_authority
    def test_authority(self):
        h = _Host()
        h.authority = False
        self.assertIs(h.run().reason, _t.Reason.MISSING)
        self.assertEqual(h.calls, 0)
        h = _Host()
        self.assertIsNotNone(h.run().observation)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_hang_proof
    def test_hang_proof(self):
        for drain in (False, True):
            h = _Host()
            h.throw, h.drain = True, drain
            result = h.run()
            self.assertIs(result.reason, _t.Reason.UNKNOWN)
            self.assertEqual(h.wait.service_stopped, not drain)
            self.assertNotIn('canary', repr(result))
            self.assertEqual(h.held, [])

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_next_ownership
    def test_next_ownership(self):
        h = _Host()
        result = h.wait.next_observation('', 100*_MS, h.holder)
        self.assertEqual(len(h.held), 1)
        self.assertIs(result.owner_token, h.held[0])
        h.release(result.owner_token)
        result = h.wait.next_observation('p', 100*_MS, h.holder)
        self.assertIsNone(result.observation)
        self.assertEqual(h.held, [])

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_missing_counts
    def test_missing_counts(self):
        h = _Host([None, None, None, _sample()])
        self.assertIsNotNone(h.run().observation)
        self.assertEqual(h.wait.attempt_count, 4)
        self.assertEqual(h.wait.accepted_publication_count, 1)
        h = _Host([None])
        self.assertIsNone(h.run().observation)
        self.assertEqual(h.wait.attempt_count, 4)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_args
    def test_args(self):
        h = _Host()
        for deadline in (True, 0, None):
            self.assertIsNone(h.wait.next_observation('', deadline, h.holder).observation)
        self.assertIsNone(h.wait.wait_until(h.f['node'], h.f['scope'], True, 100*_MS, h.holder).observation)
        self.assertIsNotNone(h.run().observation)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_release_failure
    def test_release_failure(self):
        h = _Host([_sample(truth=_t.Truth.FALSE)])
        with _mock.patch.object(h, 'release', return_value=_NO) as release:
            result = h.run()
        self.assertIs(result.reason, _t.Reason.RECONCILIATION)
        self.assertEqual(release.call_count, 1)
        self.assertTrue(h.wait.service_stopped)
        self.assertEqual(h.wait.outstanding(), tuple(h.held))
        self.assertEqual(len(h.held), 1)
        calls = h.calls
        self.assertIs(h.run().reason, _t.Reason.RECONCILIATION)
        self.assertEqual(h.calls, calls)
        h = _Host([_sample(truth=_t.Truth.FALSE)])
        h.run()
        self.assertEqual(h.wait.outstanding(), ())
        self.assertEqual(h.held, [])

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_boundary_loss
    def test_boundary_loss(self):
        h = _Host()
        h.install = False
        self.assertIs(h.run().reason, _t.Reason.MISSING)
        self.assertEqual(h.calls, 0)
        h = _Host()
        self.assertIsNotNone(h.run().observation)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_reentrant
    def test_reentrant(self):
        h = _Host()
        acquire = h.acquire
        nested = []
        def reader(*args, **kwargs):
            nested.append(h.wait.next_observation('', 100*_MS, h.holder))
            return acquire(*args, **kwargs)
        with _mock.patch.object(h, 'acquire', side_effect=reader):
            result = h.run()
        self.assertIsNotNone(result.observation)
        self.assertIs(nested[0].reason, _t.Reason.CONFLICT)
        self.assertEqual(h.calls, 1)
        h.release(result.owner_token)
        self.assertEqual(h.held, [])

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_holder_association
    def test_holder_association(self):
        h = _Host()
        foreign = _dc.replace(h.holder)
        self.assertIs(h.wait.next_observation('', 100*_MS, foreign).reason, _t.Reason.MISSING)
        self.assertEqual(h.calls, 0)
        self.assertIsNotNone(h.run().observation)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_cancelled
    def test_cancelled(self):
        h = _Host()
        with _mock.patch.object(h, 'acquire', return_value=_w._refusal(_t.Reason.CANCELLED)):
            result = h.run()
        self.assertIs(result.disposition, _t.AcquisitionDisposition.CANCELLED)
        self.assertFalse(h.wait.service_stopped)
        self.assertEqual(h.held, [])
        h = _Host()
        h.drain = False
        with _mock.patch.object(h, 'acquire', return_value=_w._refusal(_t.Reason.CANCELLED)):
            result = h.run()
        self.assertTrue(h.wait.service_stopped)
        self.assertIs(result.reason, _t.Reason.CANCELLED)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_ticks
    def test_ticks(self):
        events = (0, 2, 2, 5, 9)
        due, cursor = _w.due_events(events, 0, 5, cursor=0)
        self.assertEqual((due, cursor), ((0, 2, 2), 3))
        self.assertEqual(_w.due_events(events, 5, 5, cursor=cursor), ((), cursor))
        self.assertEqual(_w.due_events(events, 5, 5, cursor=0), ((), 0))
        self.assertEqual(_w.due_events(events, 5, 10, cursor=cursor), ((5, 9), 5))
        self.assertEqual(_w.due_events(events, 0, 10, cursor=cursor), ((5, 9), 5))
        with self.assertRaises(_t.ContractError):
            _w.due_events((2, 1), 0, 5, cursor=0)
        with self.assertRaises(_t.ContractError):
            _w.due_events(events, 0, True, cursor=0)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_immediate_true
    def test_immediate_true(self):
        h = _Host()
        self.assertIsNotNone(h.run().observation)
        self.assertEqual(h.now, 0)
        self.assertEqual(h.boundaries, [])
        self.assertEqual(h.calls, 1)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_explicit_evaluator_bounds
    def test_explicit_evaluator_bounds(self):
        for key in ('max_nodes', 'max_evidence_rows', 'max_inventory_rows'):
            for value in (None, True, 0, -1):
                h = _Host()
                context = h.context
                def invalid(*args):
                    return context(*args) | {key: value}
                with _mock.patch.object(h, 'context', side_effect=invalid), _mock.patch.object(
                        _w._predicates, 'evaluate', wraps=_w._predicates.evaluate) as evaluate:
                    self.assertIs(h.run().reason, _t.Reason.BOUNDS)
                self.assertEqual(evaluate.call_count, 0)
                self.assertEqual(h.held, [])
        self.assertIsNotNone(_Host().run().observation)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_notification_storm
    def test_notification_storm(self):
        h = _Host([_sample('first', _t.Truth.FALSE)])
        original = h.wait_until_ns
        def storm(boundary_ns, *, cancel):
            for index in range(1000):
                h.wait.notify(str(index))
            self.assertEqual(h.wait._latest_identity, '999')
            if boundary_ns == 100*_MS:
                h.samples = [_sample('final')]
                h.wait.notify('final')
            original(boundary_ns, cancel=cancel)
        with _mock.patch.object(h, 'wait_until_ns', side_effect=storm):
            result = h.run()
        self.assertIsNotNone(result.observation)
        self.assertEqual(result.observation.publication_identity, 'final')
        self.assertEqual(h.wait.attempt_count, 4)
        self.assertEqual(h.wait.accepted_publication_count, 2)
        self.assertEqual(h.now, 100*_MS)
        self.assertEqual(len(h.held), 1)
        h.release(result.owner_token)
        with self.assertRaises(_t.ContractError):
            h.wait.notify('x' * 129)
        h = _Host([_sample('first', _t.Truth.FALSE)])
        self.assertIsNone(h.run().observation)
        self.assertEqual(h.wait.attempt_count, 4)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_real_settle_signals
    def test_real_settle_signals(self):
        from tests.test_p1_wa_settle_clock_unit import AreaBControls
        for mode in ('good', 'busy', 'missing', 'wrong-owner', 'exception'):
            h, sc = _Host(), AreaBControls()
            sc.setUp()
            h.wait._settle = sc.coordinator
            h.wait._boundary = _types.SimpleNamespace(check_installation=lambda *a: _YES,
                                                      check_settle=lambda *a: _YES)
            def signals():
                if mode == 'exception':
                    raise RuntimeError('private-canary-provider')
                snapshot = sc.snapshot(inflight=1 if mode == 'busy' else 0)
                if mode == 'missing':
                    snapshot.pop(next(iter(snapshot)))
                if mode == 'wrong-owner':
                    name = next(iter(snapshot))
                    snapshot[name] = _dc.replace(snapshot[name], action_id='foreign')
                return snapshot
            if mode == 'good':
                result = h.wait.settle_effect(sc.adapter, sc.profile, sc.config, signals,
                    cancel=h.holder, scope=h.f['scope'])
                self.assertEqual(result.action_id, sc.adapter.action_id)
            else:
                with self.assertRaises(_t.ContractError) as caught:
                    h.wait.settle_effect(sc.adapter, sc.profile, sc.config, signals,
                        cancel=h.holder, scope=h.f['scope'])
                self.assertNotIn('canary', repr(caught.exception))
                self.assertIsNone(caught.exception.__context__)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_store_cancellation_token
    def test_store_cancellation_token(self):
        from tests.scripts.test_execution import _RuntimeHost
        for mode in ('good', 'foreign', 'unacknowledged'):
            with _mock.patch('tests.scripts.test_execution._fw', _w):
                host = _RuntimeHost()
            frame = host.frames
            frame._store, frame._lease = host.store, host.lease
            proof = host.sh.request(host.holder_contract)
            if mode == 'foreign':
                proof = _dc.replace(proof, released_tokens=(host.sh.evidence,))
                host.sh.proofs[id(proof)] = (proof, host.holder_contract)
            elif mode == 'unacknowledged':
                proof = _t.CancellationResult(True, False, False, False, False, (), None,
                                               _t.MissingReason.INCOMPLETE)
            with _mock.patch.object(host.cancellation, 'request', return_value=proof), _mock.patch.object(
                    host.frames._observations, 'acquire', side_effect=RuntimeError('private-canary')):
                result = frame.next_observation('', 1000, host.holder)
            self.assertIsNone(result.observation)
            self.assertEqual(frame.service_stopped, mode != 'good')
            # A foreign proof cannot remove this lease's exact registered token.
            if mode == 'good':
                with self.assertRaises(_t.ContractError):
                    host.store.check_lease(host.lease, host.plan.identity, host.scope,
                        'episode', host.lease.process_incarnation, host.lease.retained_owner)
            else:
                self.assertIs(host.store.check_lease(host.lease, host.plan.identity, host.scope,
                    'episode', host.lease.process_incarnation, host.lease.retained_owner).result, _t.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_supervised_blocked_reader
    def test_supervised_blocked_reader(self):
        import threading
        for joined in (True, False):
            h = _Host()
            gate, started = threading.Event(), threading.Event()
            def worker():
                started.set()
                gate.wait()
            thread = threading.Thread(target=worker, daemon=True)
            request = h.request
            def acquire(*args, **kwargs):
                thread.start()
                self.assertTrue(started.wait(1))
                h.now = 100*_MS + 1
                return _w._refusal(_t.Reason.DEADLINE)
            def cancel(contract):
                if joined:
                    gate.set()
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                h.drain = joined and not thread.is_alive()
                return request(contract)
            try:
                with _mock.patch.object(h, 'acquire', side_effect=acquire), _mock.patch.object(
                        h, 'request', side_effect=cancel):
                    result = h.run()
                self.assertIs(result.reason, _t.Reason.DEADLINE)
                self.assertEqual(h.wait.service_stopped, not joined)
                self.assertEqual(thread.is_alive(), not joined)
                self.assertEqual(h.held, [])
            finally:
                gate.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())

    # @anchor:wa.tests.scripts.test_frame_wait.TestFrameWait.test_mutants
    def test_mutants(self):
        source = _Path(_w.__file__).read_text()
        cases = (
            ('store-cancel', '_true(self._store.complete_holder_cancellation(self._lease, proof))', 'pass', 'test_store_cancellation_token'),
            ('explicit-bounds', '_need(type(context.get(key)) is int and context[key] > 0, _t.Reason.BOUNDS)', 'pass', 'test_explicit_evaluator_bounds'),
            ('immediate', 'if budget is not None and state.attempts:', 'if budget is not None:', 'test_immediate_true'),
            ('notification-bound', "and len(publication_identity.encode('utf-8')) <= self._max_notification_bytes", 'and True', 'test_notification_storm'),
            ('semantic', 'evaluation.value is _t.Truth.TRUE and ', '', 'test_semantics'),
            ('quiet', 'and type(quiet) is _t.AdmissionCheck and quiet.result is _t.Truth.TRUE', '', 'test_quiet'),
            ('pacing', 'max(spacing, (duration + remaining - 1) // remaining)', 'spacing', 'test_pacing'),
            ('duplicates', '_need(observation == previous, _t.Reason.CONFLICT)', 'pass', 'test_duplicate_conflict'),
            ('deadline', '_need(self._now() <= deadline, _t.Reason.DEADLINE)', 'pass', 'test_late'),
            ('capture', 'observation.capture_end_ns <= deadline', 'True', 'test_capture_deadline'),
            ('advance-clock', '_need(self._now() >= boundary, _t.Reason.DEADLINE)', 'pass', 'test_nonadvancing'),
            ('authority', '_true(self._control.check(cancel, self._contract, scope, deadline))', 'pass', 'test_authority'),
            ('hang', 'self.service_stopped = True\n\n    # @anchor:wa.scripts.frame_wait.FrameWait._run', 'self.service_stopped = False\n\n    # @anchor:wa.scripts.frame_wait.FrameWait._run', 'test_hang_proof'),
            ('release', 'self._observations.release(result.owner_token)', '_t.AdmissionCheck(_t.Truth.TRUE, None)', 'test_semantics'),
            ('half-open', 'events[end] < current_tick', 'events[end] <= current_tick', 'test_ticks'),
            ('cursor', 'start = cursor', 'start = 0', 'test_ticks'),
            ('equality-only', 'events[end] < current_tick', 'events[end] == previous_tick', 'test_ticks'),
            ('zero-advance', 'if previous_tick == current_tick:', 'if False:', 'test_ticks'),
            ('release-retain', 'self._failed_release = result', 'self._failed_release = None', 'test_release_failure'),
            ('boundary-proof', '_true(self._boundary.check_installation(self._observations, self._scheduler, self._control))', 'pass', 'test_boundary_loss'),
        )
        for label, old, new, test in cases:
            with self.subTest(mutant=label):
                self.assertEqual(source.count(old), 1)
                module = _types.ModuleType('wender.scripts._frame_wait_mutant')
                module.__package__ = 'wender.scripts'
                _sys.modules[module.__name__] = module
                try:
                    exec(compile(source.replace(old, new, 1), '<frame-mutant>', 'exec'), module.__dict__)
                    result = _unittest.TestResult()
                    with _mock.patch(__name__ + '._w', module):
                        TestFrameWait(test).run(result)
                    self.assertTrue(result.failures, label + ' did not fail assertions')
                    self.assertFalse(result.errors, result.errors)
                    print('MUTANT frame-' + label + ' KILLED ' + test)
                finally:
                    del _sys.modules[module.__name__]
