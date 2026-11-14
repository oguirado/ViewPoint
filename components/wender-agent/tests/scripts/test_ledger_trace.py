# @module {"id":"wa.tests.scripts.test_ledger_trace","layer":2,"owner":"LEARN","deps":["wa.learn.script_ledger","wa.learn.dream_journal","wa.scripts.types","wa.tests.scripts.support","wa.tools.build_code_index","wa.tools.route_code"],"book":"HB-WA-s3-remember"}
"""AUDIT public API controls. Synthetic host evidence is never production proof."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wender.learn import script_ledger as L
from wender.learn import dream_journal as J
from wender.scripts import types as T
from tests.scripts.support import make_fixture, FakeClock, CountedCallback


def _yes(*args):
    return T.AdmissionCheck(T.Truth.TRUE, None)


def _no(*args):
    return T.AdmissionCheck(T.Truth.FALSE, T.Reason.MISSING)


# @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests
class LedgerTraceTests(unittest.TestCase):
    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.setUp
    def setUp(self):
        self.f = make_fixture()
        self.clock = FakeClock(100)
        self.policy = L.LedgerPolicy(8388608, 8, 262144, 32768, 4194304,
                                     32, 128, 64, 8192, 16, 10000)
        self.trusted_events = []
        self.trusted_seals = []
        self.resolved = object()
        self.reconciled = object()
        self.owner = self._owner()
        self.intent = T.StepIntent(self.f['identity'], 'step', 0, T.ActionKind.CLICK,
            self.f['selector'], (), self.f['post'], self.f['scope'], self.f['authority'])
        self.input = CountedCallback()
        self.tmp = tempfile.TemporaryDirectory(dir='/dev/shm')
        self.addCleanup(self.tmp.cleanup)
        self.root = os.open(self.tmp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.root)
        self.missing = T.MeasuredValue(None, 'count', T.MissingRef(None, T.Reason.MISSING), T.Reason.MISSING)

    def _owner(self):
        return L.LedgerOwner(producer=self.f['handle'], run_id='run', episode_id='episode',
            artifact=self.f['identity'], authority=self.f['authority'], policy=self.policy,
            clock=lambda: self.clock.now_ns, check_intent=_yes,
            check_event=lambda e: _yes() if any(e is x for x in self.trusted_events) else _no(),
            check_seal=lambda s: _yes() if any(s is x for x in self.trusted_seals) else _no(),
            check_recovery=lambda s, e: _yes() if e is self.resolved else _no(),
            check_reconciliation=lambda s, e: _yes() if e is self.reconciled else _no())

    def _reserve(self, owner=None, intent=None):
        return L.reserve(owner or self.owner, intent or self.intent, 131072, 131072,
                         self.f['scope'], self.f['authority'])

    def _event(self, token, state, *, owner=None, approve=True, **overrides):
        owner = owner or self.owner
        disp = T.InputDisposition.APPLIED if state in (T.EventState.APPLIED, T.EventState.VERIFIED) else (
            T.InputDisposition.INPUT_UNCERTAIN if state is T.EventState.INPUT_UNCERTAIN else T.InputDisposition.NOT_SENT)
        values = dict(state=state, disposition=disp,
            verification=T.Truth.TRUE if state is T.EventState.VERIFIED else T.Truth.UNKNOWN,
            observations=(), reason=None, usage=self.missing, cost=self.missing,
            exit_code=0 if state is T.EventState.VERIFIED else None,
            score_q16=1 if state is T.EventState.VERIFIED else None)
        values.update(overrides)
        event = owner.event(token, **values)
        if approve:
            self.trusted_events.append(event)
        return event

    def _append(self, token, state, **kwargs):
        event = self._event(token, state, **kwargs)
        self.assertEqual(L.append(self.owner, token, event).result, T.Truth.TRUE)
        return event

    def _complete(self):
        token = self._reserve()
        self._append(token, T.EventState.ADMITTED)
        self.input()
        self._append(token, T.EventState.APPLIED)
        self._append(token, T.EventState.VERIFIED)
        events, head, _, _ = self.owner.snapshot()
        seal = L.EpisodeSeal('wa.script.seal/v1', self.f['handle'], 'run', 'episode',
            self.f['identity'], self.f['scope'], len(events), head, True, self.f['terminal'],
            self.f['authority'], (self.f['identity'].plan,) * 5,
            self.f['handle'], self.f['handle'])
        self.trusted_seals.append(seal)
        self.assertEqual(L.seal_episode(self.owner, seal).result, T.Truth.TRUE)
        return token, seal

    def _reject(self, reason, call):
        try:
            call()
        except T.ContractError as caught:
            self.assertIs(caught.reason, reason)
            self.assertNotIn('PRIVATE_CANARY', str(caught))
        except Exception as caught:
            self.fail('public API lost typed rejection: ' + type(caught).__name__)
        else:
            self.fail('public API accepted negative fixture')

    def _envelope(self):
        events, head, _, _ = self.owner.snapshot()
        result, parent = [], '0' * 64
        for i, event in enumerate(events):
            digest = hashlib.sha256(bytes.fromhex(parent) + L._canonical(event)).hexdigest()
            result.append(dict(index=i, parent=parent, head=digest, event=L._wire(event)))
            parent = digest
        return dict(schema='wa.script.episode/v1', head=head, events=result)

    def _load(self, seal, payload=None, *, head=None, custody=_yes, allocation=_yes):
        raw = json.dumps(self._envelope() if payload is None else payload).encode() if type(payload) is not bytes else payload
        path = Path(self.tmp.name) / 'episode'
        path.write_bytes(raw)
        with path.open('rb') as handle:
            return J.load_script_episode(handle.fileno(), owner=self.owner, seal=seal,
                head=seal.chain_head if head is None else head, grant=self.resolved,
                custody_check=custody, allocation_check=allocation)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_event
    def test_g_event(self):
        token, seal = self._complete()
        events = self.owner.snapshot()[0]
        before = len(events)
        self.assertEqual(L.append(self.owner, token, events[-1]).result, T.Truth.TRUE)
        self.assertEqual(len(self.owner.snapshot()[0]), before)
        self.assertIsNone(events[-1].usage.value)
        self.assertIs(events[-1].usage.reason, T.Reason.MISSING)
        self.assertEqual(len(self._load(seal)), before)
        altered = replace(events[-1], score_q16=2)
        self._reject(T.Reason.CONFLICT, lambda: L.append(self.owner, token, altered))
        self._reject(T.Reason.RECONCILIATION, self._reserve)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_capacity_before_input
    def test_capacity_before_input(self):
        self.policy = replace(self.policy, queue_slots=1)
        self.owner = self._owner()
        self._reserve()
        self._reject(T.Reason.CAPACITY, lambda: self._reserve(intent=replace(self.intent, step_id='other')))
        self.assertEqual(len(self.input.calls), 0)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_aggregate_before_input
    def test_aggregate_before_input(self):
        self.policy = replace(self.policy, ledger_bytes=100000)
        self.owner = self._owner()
        self._reject(T.Reason.CAPACITY, self._reserve)
        self.assertEqual(len(self.input.calls), 0)
        self.assertLess(self.owner.snapshot()[2], self.policy.ledger_bytes)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_forged_reservation
    def test_forged_reservation(self):
        token = self._reserve()
        event = self._event(token, T.EventState.ADMITTED)
        self._reject(T.Reason.FOREIGN, lambda: L.append(self.owner, replace(token), event))
        self.assertEqual(L.append(self.owner, token, event).result, T.Truth.TRUE)
        self._reject(T.Reason.CONFLICT, self._reserve)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_forged_event
    def test_forged_event(self):
        token = self._reserve()
        event = self._event(token, T.EventState.ADMITTED)
        self._reject(T.Reason.FOREIGN, lambda: L.append(self.owner, token, replace(event)))
        self.assertEqual(L.append(self.owner, token, event).result, T.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_post_input_failure
    def test_post_input_failure(self):
        token = self._reserve()
        self._append(token, T.EventState.ADMITTED)
        event = self._event(token, T.EventState.INPUT_UNCERTAIN, approve=False)
        self.input()
        result = L.append(self.owner, token, event)
        self.assertIs(result.reason, T.Reason.RECONCILIATION)
        self._reject(T.Reason.RECONCILIATION, lambda: self._reserve(intent=replace(self.intent, step_id='retry')))
        self.assertEqual(len(self.input.calls), 1)
        self.assertTrue(self.owner._reservations[id(token)]['consumed'])
        self.assertIs(self.owner._reservations[id(token)]['disposition'], T.InputDisposition.INPUT_UNCERTAIN)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_ordering
    def test_ordering(self):
        token = self._reserve()
        event = self._event(token, T.EventState.VERIFIED)
        self.assertIs(L.append(self.owner, token, event).reason, T.Reason.RECONCILIATION)
        self.assertEqual(len(self.owner.snapshot()[0]), 0)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_deadline
    def test_deadline(self):
        token = self._reserve()
        self._append(token, T.EventState.ADMITTED)
        event = self._event(token, T.EventState.APPLIED)
        self.input()
        self.clock.advance(self.policy.deadline_ns)
        self.assertIs(L.append(self.owner, token, event).reason, T.Reason.RECONCILIATION)
        self.assertEqual(len(self.input.calls), 1)
        self.assertEqual(len(self.owner.snapshot()[0]), 1)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_missing_policy
    def test_missing_policy(self):
        self.policy = None
        self._reject(T.Reason.MISSING, self._owner)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_journal
    def test_g_journal(self):
        _, seal = self._complete()
        good = self._load(seal)
        self.assertIsInstance(good, tuple)
        for field in ('exit_code', 'score_q16'):
            for invalid in ('absent', None, True, 'PRIVATE_CANARY'):
                with self.subTest(field=field, invalid=invalid):
                    envelope = self._envelope()
                    if invalid == 'absent':
                        del envelope['events'][-1]['event'][field]
                        reason = T.Reason.SCHEMA
                    else:
                        envelope['events'][-1]['event'][field] = invalid
                        reason = T.Reason.CONFLICT
                    self._reject(reason, lambda: self._load(seal, envelope))
        self.assertEqual(self._load(seal), good)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_head_and_seal
    def test_head_and_seal(self):
        _, seal = self._complete()
        self._reject(T.Reason.FOREIGN, lambda: self._load(replace(seal)))
        self._reject(T.Reason.MISSING, lambda: self._load(seal, head='0' * 64))
        rewritten = replace(seal, chain_head='f' * 64)
        self._reject(T.Reason.FOREIGN, lambda: self._load(rewritten))
        self.assertEqual(len(self._load(seal)), 3)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_incomplete_tail
    def test_incomplete_tail(self):
        _, seal = self._complete()
        envelope = self._envelope()
        envelope['events'].pop()
        self._reject(T.Reason.MISSING, lambda: self._load(seal, envelope))
        envelope = self._envelope()
        envelope['events'][1]['parent'] = 'f' * 64
        self._reject(T.Reason.CONFLICT, lambda: self._load(seal, envelope))
        envelope = self._envelope()
        envelope['events'][-1]['head'] = 'f' * 64
        self._reject(T.Reason.CONFLICT, lambda: self._load(seal, envelope))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_strict_json
    def test_strict_json(self):
        _, seal = self._complete()
        for raw, reason in ((b'{"schema":1,"schema":2}', T.Reason.CONFLICT),
                            (b'{"x":NaN}', T.Reason.TYPE), (b'{"x":1.2}', T.Reason.TYPE),
                            (b'[]', T.Reason.TYPE), (b'\xff', T.Reason.SCHEMA),
                            (b'{"x":"\\ud800"}', T.Reason.TYPE),
                            (b'{}\n{}', T.Reason.SCHEMA)):
            with self.subTest(raw=raw):
                self._reject(reason, lambda: self._load(seal, raw))
        self.assertEqual(len(self._load(seal)), 3)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_bounded_ingress
    def test_bounded_ingress(self):
        _, seal = self._complete()
        raw = json.dumps(self._envelope()).encode() + b' ' * self.policy.ingress_bytes
        real_read = os.read
        with patch.object(L._os, 'read', wraps=real_read) as reads:
            self._reject(T.Reason.BOUNDS, lambda: self._load(seal, raw))
            self.assertEqual(reads.call_count, 0)
        deep = b'{"x":' + b'[' * (self.policy.depth + 1) + b'0' + b']' * (self.policy.depth + 1) + b'}'
        self._reject(T.Reason.BOUNDS, lambda: self._load(seal, deep))
        self._reject(T.Reason.MISSING, lambda: self._load(seal, custody=_no))
        self._reject(T.Reason.MISSING, lambda: self._load(seal, allocation=_no))
        self.assertEqual(len(self._load(seal)), 3)

    def _spool(self, name, raw):
        path = Path(self.tmp.name) / name
        with path.open('xb') as f:
            f.write(raw)
        return path

    def _collect(self, names, token, event, custody=None):
        def check(fd, name):
            s = os.stat(name, dir_fd=fd, follow_symlinks=False)
            return s.st_dev, s.st_ino, s.st_uid
        return L.consolidate(self.owner, root_fd=self.root, names=tuple(names),
            custody_check=custody or check, resolve_record=lambda envelope: (token, event))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_spool
    def test_g_spool(self):
        token = self._reserve()
        event = self._event(token, T.EventState.ADMITTED)
        good = json.dumps(dict(schema='wa.script.spool/v1', event_id=event.event_id)).encode()
        self._spool('a-poison', b'{"x":NaN}')
        self._spool('b-good', good)
        result = self._collect(('a-poison', 'b-good'), token, event)
        self.assertIs(result[0].reason, T.Reason.TYPE)
        self.assertIs(result[1].result, T.Truth.TRUE)
        self.assertEqual(len(self.owner.snapshot()[0]), 1)
        self.assertIs(self._collect(('b-good',), token, event)[0].result, T.Truth.TRUE)
        self.assertEqual(len(self.owner.snapshot()[0]), 1)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_spool_custody
    def test_spool_custody(self):
        token = self._reserve()
        event = self._event(token, T.EventState.ADMITTED)
        self._spool('good', json.dumps(dict(schema='wa.script.spool/v1', event_id=event.event_id)).encode())
        os.symlink('good', Path(self.tmp.name) / 'link')
        self.assertIs(self._collect(('link',), token, event)[0].reason, T.Reason.FOREIGN)
        self.assertIs(self._collect(('good',), token, event, lambda *a: (0, 0, 0))[0].reason, T.Reason.FOREIGN)
        real_open = os.open
        def swapped(name, *args, **kwargs):
            if name == 'good':
                p = Path(self.tmp.name)
                (p / 'good').rename(p / 'old')
                (p / 'good').write_bytes(b'{}')
            return real_open(name, *args, **kwargs)
        with patch.object(L._os, 'open', swapped):
            self.assertIs(self._collect(('good',), token, event)[0].reason, T.Reason.FOREIGN)
        self.assertEqual(len(self.owner.snapshot()[0]), 0)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_spool_shapes
    def test_spool_shapes(self):
        token = self._reserve()
        event = self._event(token, T.EventState.ADMITTED)
        for index, (raw, reason) in enumerate(((b'[]', T.Reason.TYPE),
                (b'{"schema":"wa.script.spool/v1","event_id":[]}', T.Reason.TYPE),
                (b'{}', T.Reason.SCHEMA),
                (b' ' * (self.policy.ingress_bytes + 1), T.Reason.BOUNDS),
                (b'{"x":' + b'[' * 33 + b'0' + b']' * 33 + b'}', T.Reason.BOUNDS))):
            name = str(index)
            self._spool(name, raw)
            self.assertIs(self._collect((name,), token, event)[0].reason, reason)
        self._reject(T.Reason.BOUNDS, lambda: self._collect(('x',) * 17, token, event))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_recover
    def test_g_recover(self):
        _, seal = self._complete()
        self.owner = self._owner()  # Surviving seal never transfers RAM authority.
        self._reject(T.Reason.FOREIGN, lambda: self._load(seal))
        for passive in (None, {'promoted': True}, seal, b'PRIVATE_CANARY'):
            self._reject(T.Reason.MISSING, lambda: L.recover(self.owner, scope=self.f['scope'], evidence=passive, reconciliation=self.reconciled))
            self._reject(T.Reason.RECONCILIATION, self._reserve)
        self.assertIs(L.recover(self.owner, scope=self.f['scope'], evidence=self.resolved, reconciliation=self.reconciled).result, T.Truth.TRUE)
        _, rebuilt = self._complete()
        self.assertEqual(len(self._load(rebuilt)), 3)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_restart_uncertain
    def test_g_restart_uncertain(self):
        self._reject(T.Reason.RECONCILIATION, lambda: L.recover(self.owner, scope=None, evidence=self.resolved, reconciliation=self.reconciled))
        self._reject(T.Reason.RECONCILIATION, self._reserve)
        self.owner._episode = 'renamed'
        self.clock.advance(10)
        self._reject(T.Reason.MISSING, lambda: L.recover(self.owner, scope=self.f['scope'], evidence='abandoned', reconciliation=self.reconciled))
        self._reject(T.Reason.RECONCILIATION, self._reserve)
        self.assertIs(L.recover(self.owner, scope=self.f['scope'], evidence=self.resolved, reconciliation=self.reconciled).result, T.Truth.TRUE)
        self._reserve()

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_compact
    def test_g_compact(self):
        token = self._reserve()
        event = self._append(token, T.EventState.ADMITTED)
        generation = self.owner.snapshot()[3]
        for proof in ('queued', 'lineage', 'unknown-roots'):
            self._reject(T.Reason.MISSING, lambda: self.owner.retire(generation=generation, proof=proof, check_drain=_no))
        self.assertEqual(len(self.owner.snapshot()[0]), 1)
        self.assertIs(self.owner.retire(generation=generation, proof=self.resolved, check_drain=_yes).result, T.Truth.TRUE)
        self._reject(T.Reason.RECONCILIATION, self._reserve)
        self._reject(T.Reason.FOREIGN, lambda: L.append(self.owner, token, event))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_compact_generation
    def test_compact_generation(self):
        token = self._reserve()
        generation = self.owner.snapshot()[3]
        self._append(token, T.EventState.ADMITTED)
        self._reject(T.Reason.STALE, lambda: self.owner.retire(generation=generation,
            proof=self.resolved, check_drain=_yes))
        self.assertEqual(len(self.owner.snapshot()[0]), 1)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_g_declassify
    def test_g_declassify(self):
        token = self._reserve()
        observation = replace(self.f['observation'], producer_identity='PRIVATE_CANARY')
        event = self._append(token, T.EventState.ADMITTED, observations=(observation,))
        destination = next(iter(T.ExportDestination))
        purpose = next(iter(T.Purpose))
        approved = lambda e, d, p: _yes() if d is destination and p is purpose else _no()
        result = L.project_receipt(self.owner, event, destination=destination, purpose=purpose,
                                   approve=approved, tokenizer=lambda s: 30)
        self.assertNotIn(b'PRIVATE_CANARY', result)
        self.assertNotIn(b'producer', result)
        self.assertEqual(json.loads(result)['state'], 'admitted')
        for tokenizer, reason in ((None, T.Reason.MISSING), (lambda s: 121, T.Reason.BOUNDS),
                                  (lambda s: True, T.Reason.BOUNDS)):
            self._reject(reason, lambda: L.project_receipt(self.owner, event, destination=destination,
                purpose=purpose, approve=approved, tokenizer=tokenizer))
        self._reject(T.Reason.MISSING, lambda: L.project_receipt(self.owner, event,
            destination=destination, purpose=purpose, approve=_no, tokenizer=lambda s: 1))
        self._reject(T.Reason.BOUNDS, lambda: L.project_receipt(self.owner, event,
            destination=destination, purpose=purpose, approve=approved, tokenizer=lambda s: 41, failure=True))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_seal_authentication
    def test_seal_authentication(self):
        token = self._reserve()
        self._append(token, T.EventState.ADMITTED)
        self._append(token, T.EventState.APPLIED)
        self._append(token, T.EventState.VERIFIED)
        events, head, _, _ = self.owner.snapshot()
        seal = L.EpisodeSeal('wa.script.seal/v1', self.f['handle'], 'run', 'episode',
            self.f['identity'], self.f['scope'], len(events), head, True, self.f['terminal'],
            self.f['authority'], (self.f['identity'].plan,) * 5, self.f['handle'], self.f['handle'])
        self._reject(T.Reason.FOREIGN, lambda: self._load(seal))
        self._reject(T.Reason.MISSING, lambda: L.seal_episode(self.owner, seal))
        self.trusted_seals.append(seal)
        self.assertIs(L.seal_episode(self.owner, seal).result, T.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_refund
    def test_refund(self):
        token, _ = self._complete()
        before = self.owner.snapshot()[2]
        refund = self.owner.release_unused(token)
        self.assertGreater(refund, 0)
        self.assertGreater(self.owner.snapshot()[2], 0)
        self.assertEqual(before - refund, self.owner.snapshot()[2])
        self.assertEqual(self.owner.release_unused(token), 0)
        self._reject(T.Reason.CONFLICT, lambda: self._event(token, T.EventState.ADMITTED))
        self.assertTrue(self.owner._reservations[id(token)]['consumed'])

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_stalled_seal
    def test_stalled_seal(self):
        token = self._reserve()
        self._append(token, T.EventState.ADMITTED)
        self._append(token, T.EventState.APPLIED)
        self._append(token, T.EventState.VERIFIED)
        events, head, _, _ = self.owner.snapshot()
        seal = L.EpisodeSeal('wa.script.seal/v1', self.f['handle'], 'run', 'episode',
            self.f['identity'], self.f['scope'], len(events), head, True, self.f['terminal'],
            self.f['authority'], (self.f['identity'].plan,) * 5, self.f['handle'], self.f['handle'])
        def stalled(s):
            self.clock.advance(self.policy.deadline_ns)
            return _yes()
        self.owner._check_seal = stalled
        self._reject(T.Reason.DEADLINE, lambda: L.seal_episode(self.owner, seal))
        self._reject(T.Reason.FOREIGN, lambda: self.owner.acknowledge_seal(seal, self.f['scope']))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_identity_and_provenance
    def test_identity_and_provenance(self):
        self._reject(T.Reason.STALE, lambda: L.reserve(self.owner, self.intent, 1, 1,
            replace(self.f['scope'], scope_revision=2), self.f['authority']))
        token = self._reserve()
        event = self._event(token, T.EventState.ADMITTED, approve=False)
        self._reject(T.Reason.MISSING, lambda: L.append(self.owner, token, event))
        self.trusted_events.append(event)
        self.assertIs(L.append(self.owner, token, event).result, T.Truth.TRUE)
        self._reject(T.Reason.MISSING, lambda: replace(event, state=T.EventState.VERIFIED, verification=T.Truth.TRUE, exit_code=None, score_q16=1))
        self._reject(T.Reason.TYPE, lambda: replace(event, score_q16=True))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_ingress_shapes_and_peak
    def test_ingress_shapes_and_peak(self):
        _, seal = self._complete()
        for raw in (b'{"x":[' + b'0,' * 64 + b'0]}',
                    b'{' + b','.join(('"k%d":0' % i).encode() for i in range(129)) + b'}',
                    b'{"x":"' + b'x' * 8193 + b'"}'):
            self._reject(T.Reason.BOUNDS, lambda: self._load(seal, raw))
        before = self.owner.snapshot()[2]
        self.owner._policy = replace(self.policy, ledger_bytes=before + 1)
        self._reject(T.Reason.CAPACITY, lambda: self._load(seal))
        self.assertEqual(self.owner.snapshot()[2], before)
        self.owner._policy = self.policy
        self.assertEqual(len(self._load(seal)), 3)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_journal_exact_coordinates
    def test_journal_exact_coordinates(self):
        _, seal = self._complete()
        for field, value in (('index', True), ('parent', 'f' * 64), ('head', 'f' * 64)):
            envelope = self._envelope()
            envelope['events'][0][field] = value
            self._reject(T.Reason.CONFLICT, lambda: self._load(seal, envelope))
        envelope = self._envelope()
        envelope['extra'] = 'PRIVATE_CANARY'
        self._reject(T.Reason.SCHEMA, lambda: self._load(seal, envelope))
        self.assertEqual(len(self._load(seal)), 3)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_seal_bindings
    def test_seal_bindings(self):
        _, seal = self._complete()
        self._reject(T.Reason.MISSING, lambda: replace(seal, complete=False))
        self._reject(T.Reason.MISSING, lambda: replace(seal, digests=()))
        self._reject(T.Reason.TYPE, lambda: replace(seal, expected_count=True))
        self._reject(T.Reason.STALE, lambda: replace(seal, scope=replace(self.f['scope'], scope_revision=2)))
        self._reject(T.Reason.FOREIGN, lambda: self.owner.acknowledge_seal(seal,
            replace(self.f['scope'], scope_revision=2)))
        self.assertIs(self.owner.acknowledge_seal(seal, self.f['scope']).result, T.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_wrong_destination
    def test_wrong_destination(self):
        token = self._reserve()
        event = self._append(token, T.EventState.ADMITTED)
        destinations = tuple(T.ExportDestination)
        purpose = next(iter(T.Purpose))
        approved = lambda e, d, p: _yes() if d is destinations[0] else _no()
        self._reject(T.Reason.MISSING, lambda: L.project_receipt(self.owner, event,
            destination=destinations[-1], purpose=purpose, approve=approved, tokenizer=lambda s: 1))
        self.assertTrue(L.project_receipt(self.owner, event, destination=destinations[0],
            purpose=purpose, approve=approved, tokenizer=lambda s: 1))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_backend_audit_outage
    def test_backend_audit_outage(self):
        token = self._reserve()
        self._append(token, T.EventState.ADMITTED)
        event = self._event(token, T.EventState.APPLIED)
        self.input()
        def failed(e):
            raise OSError('PRIVATE_CANARY')
        self.owner._check_event = failed
        result = L.append(self.owner, token, event)
        self.assertIs(result.reason, T.Reason.RECONCILIATION)
        self._reject(T.Reason.RECONCILIATION, self._reserve)
        self.assertEqual(len(self.input.calls), 1)
        self.assertNotIn('PRIVATE_CANARY', repr(result))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_independent_reconciliation
    def test_independent_reconciliation(self):
        self._reject(T.Reason.MISSING, lambda: L.recover(self.owner, scope=self.f['scope'],
            evidence=self.resolved, reconciliation=None))
        self._reject(T.Reason.RECONCILIATION, self._reserve)
        self.assertIs(L.recover(self.owner, scope=self.f['scope'], evidence=self.resolved,
            reconciliation=self.reconciled).result, T.Truth.TRUE)
        self._reserve()

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_spool_wrong_owner
    def test_spool_wrong_owner(self):
        token = self._reserve()
        event = self._event(token, T.EventState.ADMITTED)
        self._spool('good', json.dumps(dict(schema='wa.script.spool/v1', event_id=event.event_id)).encode())
        real_stat = os.stat
        def foreign(*a, **kw):
            result = real_stat(*a, **kw)
            values = list(result)
            values[4] += 1
            return os.stat_result(values)
        real_fstat = os.fstat
        def foreign_fd(fd):
            result = real_fstat(fd)
            if fd == self.root:
                return result
            values = list(result)
            values[4] += 1
            return os.stat_result(values)
        with patch.object(L._os, 'stat', foreign), patch.object(L._os, 'fstat', foreign_fd):
            self.assertIs(self._collect(('good',), token, event)[0].reason, T.Reason.FOREIGN)
        self.assertIs(self._collect(('good',), token, event)[0].result, T.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_growing_descriptor
    def test_growing_descriptor(self):
        _, seal = self._complete()
        real_read = os.read
        total = [0]
        def grows(fd, size):
            total[0] += 1
            return b' ' * size
        with patch.object(L._os, 'read', grows):
            self._reject(T.Reason.BOUNDS, lambda: self._load(seal))
        self.assertEqual(total[0], 1)
        self.assertEqual(len(self._load(seal)), 3)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_receipt_transforms
    def test_receipt_transforms(self):
        import base64
        canary = 'PRIVATE_CANARY'
        variants = (canary, hashlib.sha256(canary.encode()).hexdigest(),
                    base64.b64encode(canary.encode()).decode(), 'pixels:' + canary)
        token = self._reserve()
        observation = replace(self.f['observation'], producer_identity=variants[0],
            provenance=variants[1], association=T.MissingRef(variants[2], None),
            projection=T.MissingRef(variants[3], None))
        event = self._append(token, T.EventState.ADMITTED, observations=(observation,))
        result = L.project_receipt(self.owner, event, destination=next(iter(T.ExportDestination)),
            purpose=next(iter(T.Purpose)), approve=_yes, tokenizer=lambda s: 30)
        for value in variants:
            self.assertNotIn(value.encode(), result)
        self.assertEqual(set(json.loads(result)), {'schema', 'event_ref', 'disposition', 'state', 'reason'})

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_seal_exact_identity
    def test_seal_exact_identity(self):
        token = self._reserve()
        self._append(token, T.EventState.ADMITTED)
        self._append(token, T.EventState.APPLIED)
        self._append(token, T.EventState.VERIFIED)
        events, head, _, _ = self.owner.snapshot()
        seal = L.EpisodeSeal('wa.script.seal/v1', self.f['handle'], 'run', 'episode',
            self.f['identity'], self.f['scope'], len(events), head, True, self.f['terminal'],
            self.f['authority'], (self.f['identity'].plan,) * 5, self.f['handle'], self.f['handle'])
        for changes in (dict(run_id='foreign'), dict(episode_id='foreign'), dict(artifact=replace(self.f['identity'], revision=2)),
                        dict(producer=replace(self.f['handle'], incarnation='foreign'))):
            self._reject(T.Reason.FOREIGN, lambda: L.seal_episode(self.owner, replace(seal, **changes)))
        for changes in (dict(expected_count=4), dict(chain_head='f' * 64)):
            self._reject(T.Reason.CONFLICT, lambda: L.seal_episode(self.owner, replace(seal, **changes)))
        for changes in (dict(provenance=replace(self.f['handle'], token='foreign')),
                        dict(digests=(T.PlanDigest('sha256', 'f' * 64),) * 5)):
            self._reject(T.Reason.MISSING, lambda: L.seal_episode(self.owner, replace(seal, **changes)))
        self.trusted_seals.append(seal)
        self.assertIs(L.seal_episode(self.owner, seal).result, T.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_capacity_churn
    def test_capacity_churn(self):
        admitted = []
        for index in range(16):
            try:
                token = self._reserve(intent=replace(self.intent, step_id='step%d' % index))
            except T.ContractError as error:
                self.assertIs(error.reason, T.Reason.CAPACITY)
                break
            admitted.append(token)
            self._append(token, T.EventState.REFUSED)
        else:
            self.fail('capacity exhaustion did not backpressure')
        self.assertGreater(len(admitted), 0)
        before = self.owner.snapshot()
        self._reject(T.Reason.MISSING, lambda: self.owner.retire(generation=before[3],
            proof='successor-negative-lineage', check_drain=_no))
        self.assertEqual(self.owner.snapshot(), before)
        self._reject(T.Reason.CAPACITY, lambda: self._reserve(intent=replace(self.intent, step_id='overflow')))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_retire_pending
    def test_retire_pending(self):
        token = self._reserve()
        event = self._event(token, T.EventState.ADMITTED)
        generation = self.owner.snapshot()[3]
        self._reject(T.Reason.CONFLICT, lambda: self.owner.retire(generation=generation,
            proof=self.resolved, check_drain=_yes))
        self.assertIs(L.append(self.owner, token, event).result, T.Truth.TRUE)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_callback_privacy
    def test_callback_privacy(self):
        _, seal = self._complete()
        def bad(*args):
            raise OSError('PRIVATE_CANARY')
        self._reject(T.Reason.UNKNOWN, lambda: self._load(seal, custody=bad))
        self._reject(T.Reason.UNKNOWN, lambda: self._load(seal, allocation=bad))
        event = self.owner.snapshot()[0][-1]
        self._reject(T.Reason.UNKNOWN, lambda: L.project_receipt(self.owner, event,
            destination=next(iter(T.ExportDestination)), purpose=next(iter(T.Purpose)),
            approve=bad, tokenizer=lambda s: 1))
        self._reject(T.Reason.UNKNOWN, lambda: L.project_receipt(self.owner, event,
            destination=next(iter(T.ExportDestination)), purpose=next(iter(T.Purpose)),
            approve=_yes, tokenizer=bad))
        self.assertEqual(len(self._load(seal)), 3)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_callback_generation
    def test_callback_generation(self):
        first = self._reserve()
        event = self._event(first, T.EventState.ADMITTED)
        second = self._reserve(intent=replace(self.intent, step_id='second'))
        other = self._event(second, T.EventState.ADMITTED)
        def reentrant(e):
            if e is event:
                self.assertIs(L.append(self.owner, second, other).result, T.Truth.TRUE)
            return _yes()
        self.owner._check_event = reentrant
        self._reject(T.Reason.STALE, lambda: L.append(self.owner, first, event))
        self.assertEqual(self.owner.snapshot()[0], (other,))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_s3_anchor_routing
    def test_s3_anchor_routing(self):
        from tools import build_code_index as builder, route_code as router
        root = Path(__file__).resolve().parents[2]
        owned = ('wender/learn/script_ledger.py', 'wender/learn/dream_journal.py',
                 'tests/scripts/test_ledger_trace.py')
        self.assertEqual([row for row in builder.check_missing_anchors(root)
                          if row['file'] in owned], [])
        index = builder.build_memory_index(root)
        rows = [r for r in index['anchors'] if r['file'] in owned]
        self.assertTrue(rows)
        target = Path(self.tmp.name) / 'routing-index'
        target.mkdir()
        for name, raw in index['files'].items():
            (target / name).write_bytes(raw)
        loaded = router.load_index(root, target)
        for row in rows:
            self.assertEqual(row['stage'], 'HB-WA-s3-remember')
            self.assertIn(row['file'], str(router.lookup_anchor(loaded, row['id'])))
        # Negative control: nonexistent anchors must abstain even in a fresh index.
        result = router.lookup_anchor(loaded, 'wa.learn.script_ledger.AUSENTE')
        self.assertNotIn('wender/learn/script_ledger.py', str(result))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_terminal_conflict
    def test_terminal_conflict(self):
        token = self._reserve()
        self._append(token, T.EventState.ADMITTED)
        self._append(token, T.EventState.REFUSED)
        event = self._event(token, T.EventState.CANCELLED)
        self._reject(T.Reason.CONFLICT, lambda: L.append(self.owner, token, event))
        self.assertEqual(len(self.owner.snapshot()[0]), 2)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_projection_failure_after_input
    def test_projection_failure_after_input(self):
        token = self._reserve()
        self._append(token, T.EventState.ADMITTED)
        self.input()
        self._reject(T.Reason.RECONCILIATION, lambda: self._event(token,
            T.EventState.INPUT_UNCERTAIN, observations=['PRIVATE_CANARY']))
        self.assertTrue(self.owner._reservations[id(token)]['consumed'])
        self.assertIs(self.owner._reservations[id(token)]['disposition'], T.InputDisposition.INPUT_UNCERTAIN)
        self._reject(T.Reason.RECONCILIATION, self._reserve)
        self.assertEqual(len(self.input.calls), 1)

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_signed_failure_evidence
    def test_signed_failure_evidence(self):
        token = self._reserve()
        event = self._append(token, T.EventState.REFUSED, exit_code=-1, score_q16=-2)
        self.assertEqual((event.exit_code, event.score_q16), (-1, -2))
        self._reject(T.Reason.TYPE, lambda: replace(event, exit_code=True))

    # @anchor:wa.tests.scripts.test_ledger_trace.LedgerTraceTests.test_spec_control_mutants
    def test_spec_control_mutants(self):
        """Remove individual checks in memory; each named API test must fail.

        Require an assertion failure, not a setup/import/runtime error. Source
        files are never mutated. Both modules are restored after every mutant.
        """
        import ast
        import sys
        import types
        import wender.learn as package
        cases = (
            ('projection-consumed', 'L', 'record.update', 'test_projection_failure_after_input'),
            ('usage-null', 'L', 'usage-null', 'test_g_event'),
            ('missing-policy', 'L', 'type(authority) is _t.AuthorityRevision and type(policy) is LedgerPolicy', 'test_missing_policy'),
            ('dedup-conflict', 'L', 'canonical != old', 'test_g_event'),
            ('consumed', 'L', "record['consumed']", 'test_post_input_failure'),
            ('uncertain-disposition', 'L', "record['disposition']", 'test_post_input_failure'),
            ('deny-watermark', 'L', 'self._closed = self._deny = True', 'test_g_compact'),
            ('receipt-allowlist', 'L', 'result = {', 'test_receipt_transforms'),
            ('queue', 'L', "len(self._reservations) < self._policy.queue_slots", 'test_capacity_before_input'),
            ('deadline', 'L', 'self._clock() < self._policy.deadline_ns', 'test_deadline'),
            ('seal-deadline', 'L', 'self._clock() < self._policy.deadline_ns', 'test_stalled_seal'),
            ('aggregate', 'L', 'charge + self._charged <= self._policy.ledger_bytes', 'test_aggregate_before_input'),
            ('reservation', 'L', "record is not None and record['token'] is token", 'test_forged_reservation'),
            ('mint', 'L', "self._minted.get(id(event)) is event", 'test_forged_event'),
            ('terminal-conflict', 'L', "event.state in transitions.get(prior, ())", 'test_terminal_conflict'),
            ('ordering', 'L', "event.state in transitions.get(prior, ())", 'test_ordering'),
            ('scope', 'L', 'intent.artifact == self._artifact', 'test_identity_and_provenance'),
            ('event-provenance', 'L', 'self._check_event, event', 'test_identity_and_provenance'),
            ('seal-provenance', 'L', 'self._check_seal, seal', 'test_seal_authentication'),
            ('seal-membership', 'L', 'any(s is seal for s in self._seals)', 'test_head_and_seal'),
            ('reconciliation', 'L', 'self._check_recovery, scope, evidence', 'test_g_recover'),
            ('surface-reconciliation', 'L', 'self._check_reconciliation, scope, reconciliation', 'test_independent_reconciliation'),
            ('restart-scope', 'L', 'type(scope) is _t.ScopeIdentity', 'test_g_restart_uncertain'),
            ('pending-record', 'L', "not any(r.get('pending')", 'test_retire_pending'),
            ('drain', 'L', 'check_drain, self._producer, proof', 'test_g_compact'),
            ('root-generation', 'L', 'generation == self._generation', 'test_compact_generation'),
            ('spool-link-custody', 'L', 'spool-link-custody', 'test_spool_custody'),
            ('depth', 'L', '<= policy.depth', 'test_spool_shapes'),
            ('finite-integer', 'L', 'parse_float', 'test_strict_json'),
            ('spool-event-id', 'L', "type(envelope['event_id']) is int", 'test_spool_shapes'),
            ('spool-identity', 'L', 'identity == expected', 'test_spool_custody'),
            ('spool-owner', 'L', 'before.st_uid == root.st_uid', 'test_spool_wrong_owner'),
            ('growth', 'L', 'count <= policy.ingress_bytes', 'test_growing_descriptor'),
            ('seal-identities', 'L', 'seal.producer == self._producer', 'test_seal_exact_identity'),
            ('seal-count-head', 'L', 'seal.expected_count == len(self._events)', 'test_seal_exact_identity'),
            ('batch', 'L', 'len(names) <= policy.batch', 'test_spool_shapes'),
            ('duplicate-key', 'L', 'key not in result', 'test_strict_json'),
            ('object-envelope', 'L', 'type(value) is dict', 'test_strict_json'),
            ('descriptor-size', 'L', 'info.st_size <= policy.ingress_bytes', 'test_bounded_ingress'),
            ('sequence-bound', 'L', 'len(v) <= policy.sequence', 'test_ingress_shapes_and_peak'),
            ('member-bound', 'L', 'policy.members', 'test_ingress_shapes_and_peak'),
            ('string-bound', 'L', 'length <= policy.string_bytes', 'test_ingress_shapes_and_peak'),
            ('declassify', 'L', 'approve, event, destination, purpose', 'test_wrong_destination'),
            ('tokenizer', 'L', 'callable(tokenizer)', 'test_g_declassify'),
            ('tokens', 'L', '0 <= count <=', 'test_g_declassify'),
            ('complete', 'L', 'self.expected_count > 0 and self.complete', 'test_seal_bindings'),
            ('digest-count', 'L', 'len(self.digests) == 5', 'test_seal_bindings'),
            ('payload-closed', 'J', 'set(payload) == set(expected)', 'test_g_journal'),
            ('payload-exact', 'J', 'ledger._canonical(payload) ==', 'test_g_journal'),
            ('head', 'J', 'head == seal.chain_head', 'test_head_and_seal'),
            ('custody', 'J', 'custody_check, fd', 'test_bounded_ingress'),
            ('allocation', 'J', 'allocation_check, grant, policy.ingress_peak_bytes', 'test_bounded_ingress'),
            ('decode-peak', 'J', 'owner._charged + policy.ingress_peak_bytes', 'test_ingress_shapes_and_peak'),
            ('count', 'J', 'len(records) == seal.expected_count', 'test_incomplete_tail'),
            ('coordinates', 'J', "type(record['index']) is int", 'test_journal_exact_coordinates'),
            ('chain-head', 'J', "record['head'] == previous", 'test_incomplete_tail'),
            ('envelope-closed', 'J', "set(envelope) == {'schema', 'head', 'events'}", 'test_journal_exact_coordinates'),
        )
        for label, target, needle, method in cases:
            with self.subTest(mutant=label):
                original = L if target == 'L' else J
                source = Path(original.__file__).read_text()
                tree = ast.parse(source)
                changed = []
                for node in ast.walk(tree):
                    if label == 'projection-consumed' and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'update' and 'consumed=True' in (ast.get_source_segment(source, node) or ''):
                        node.func = ast.Name('dict', ast.Load())
                        node.args, node.keywords = [], []
                        changed.append(node)
                    if label == 'usage-null' and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'RunEvent':
                        for i, arg in enumerate(node.args):
                            if isinstance(arg, ast.Name) and arg.id == 'usage':
                                node.args[i] = ast.parse("_t.MeasuredValue(0, 'count', _t.MissingRef('invented', None), None)", mode='eval').body
                                changed.append(node)
                    if label == 'spool-link-custody' and isinstance(node, ast.Attribute) and node.attr == 'O_NOFOLLOW':
                        node.attr = 'O_RDONLY'
                        changed.append(node)
                    if label == 'finite-integer' and isinstance(node, ast.Call) and any(k.arg == 'parse_float' for k in node.keywords):
                        node.keywords = [k for k in node.keywords if k.arg not in ('parse_float', 'parse_constant')]
                        changed.append(node)
                    if label == 'dedup-conflict' and isinstance(node, ast.If) and ast.get_source_segment(source, node.test) == needle:
                        node.test = ast.Constant(False)
                        changed.append(node)
                    elif label in ('consumed', 'uncertain-disposition') and isinstance(node, ast.Assign) and ast.get_source_segment(source, node.targets[0]) == needle:
                        node.value = ast.Constant(False) if label == 'consumed' else ast.Attribute(ast.Attribute(ast.Name('_t', ast.Load()), 'InputDisposition', ast.Load()), 'NOT_SENT', ast.Load())
                        changed.append(node)
                    elif label == 'deny-watermark' and isinstance(node, ast.Assign) and ast.get_source_segment(source, node) == needle:
                        node.value = ast.Constant(False)
                        changed.append(node)
                    elif label == 'receipt-allowlist' and isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) and ast.get_source_segment(source, node).startswith(needle) and "'cu.receipt/v1'" in ast.get_source_segment(source, node):
                        node.value.keys.append(ast.Constant('private'))
                        node.value.values.append(ast.Call(ast.Name('_wire', ast.Load()), [ast.Name('event', ast.Load())], []))
                        changed.append(node)
                    if not isinstance(node, ast.Call):
                        continue
                    name = node.func.id if isinstance(node.func, ast.Name) else (
                        node.func.attr if isinstance(node.func, ast.Attribute) else '')
                    if name not in ('_need', '_checked', '_host_check'):
                        continue
                    segment = ast.get_source_segment(source, node) or ''
                    link_guard = label == 'spool-link-custody' and ('_stat.S_ISREG(before.st_mode)' in segment or 'identity == expected' in segment)
                    if needle in segment or link_guard:
                        if name == '_need':
                            node.args[0] = ast.Constant(True)
                        else:
                            node.func = ast.Attribute(ast.Name('T', ast.Load()), 'AdmissionCheck', ast.Load())
                            node.args = [ast.Attribute(ast.Attribute(ast.Name('T', ast.Load()), 'Truth', ast.Load()), 'TRUE', ast.Load()), ast.Constant(None)]
                        changed.append(node)
                self.assertEqual(len(changed), 2 if label in ('member-bound', 'depth') else (3 if label == 'spool-link-custody' else 1), (label, len(changed)))
                ast.fix_missing_locations(tree)
                mutant = types.ModuleType(original.__name__)
                mutant.__file__ = original.__file__
                mutant.T = T
                with patch.dict(sys.modules, {original.__name__: mutant}), patch.object(
                        package, original.__name__.split('.')[-1], mutant):
                    exec(compile(tree, original.__file__, 'exec'), mutant.__dict__)
                    with patch.dict(globals(), {target: mutant}):
                        result = unittest.TestResult()
                        LedgerTraceTests(method).run(result)
                    self.assertTrue(result.failures, (label, 'survived', result.errors))
                print('MUTANT', label, 'KILLED', method)


if __name__ == '__main__':
    unittest.main()
